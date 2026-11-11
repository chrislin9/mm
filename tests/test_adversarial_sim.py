"""End-to-end: the real Engine against an adversarial in-memory exchange.

Invariant checked after every action, using the EXCHANGE's own books (not the engine's view):
    true worst-case floor  >=  week anchor - (weekly_loss_limit - safety_buffer)
and at settlement the realised loss must be within the weekly cap.
"""
import asyncio
import random

import pytest

from pmmm.config import Config
from pmmm.engine import Engine
from pmmm.journal import Journal
from pmmm.util import iso_utc

from .fake_exchange import T0, FakeExchange


async def drain():
    for _ in range(8):
        await asyncio.sleep(0)


async def run_sim(seed: int, tmp_path, steps: int = 1500, selection: bool = False, **kw):
    rng = random.Random(seed)
    ex = FakeExchange(rng, **kw)
    cfg = Config()
    cfg.event_slug = "test"
    cfg.trading_end_utc = iso_utc(T0 + 12 * 3600)
    cfg.state_dir = str(tmp_path)
    cfg.kill_file = str(tmp_path / "KILL")
    # Aggressive settings so the adversary gets maximum opportunity.
    cfg.strategy.min_requote_interval_s = 0.5
    cfg.strategy.fill_cooldown_s = 2.0
    cfg.strategy.jump_cooldown_s = 5.0
    cfg.strategy.event_cooldown_s = 10.0
    cfg.strategy.tox_pause_s = 20.0
    cfg.strategy.order_ttl_s = 30
    cfg.strategy.refresh_before_expiry_s = 10
    # These runs stress the risk guard, so trade every market (selection tested separately).
    cfg.strategy.dynamic_market_selection = selection
    cfg.risk.max_order_risk = 2.0
    cfg.risk.max_market_risk = 6.0
    cfg.validate()
    eng = Engine(cfg, ex, Journal(tmp_path / f"j{seed}.jsonl"), init_week=True, clock=lambda: ex.t)
    ex.engine = eng
    await eng.bootstrap()
    eng.on_private_state(True)
    eng.private_snapshot = True
    budget = cfg.risk.weekly_loss_limit - cfg.risk.safety_buffer
    hard_floor = eng.anchor - budget
    start_value = ex.settle_value()
    worst_seen = float("inf")
    for i in range(steps):
        ex.advance(0.5)
        for s in ex.slugs:
            eng._apply_book(await ex.get_book(s))
        await eng.step()
        await drain()
        f = ex.true_floor()
        worst_seen = min(worst_seen, f)
        assert f >= hard_floor - 1e-6, f"seed {seed} step {i}: true floor {f:.4f} < {hard_floor:.4f}"
        ex.adversary()
        await drain()
        if i % 4 == 0 or eng.resync_soon:
            eng.resync_soon = False
            try:
                await eng.resync()
            except Exception:
                pass
        if rng.random() < 0.01:  # private stream flaps
            eng.on_private_state(False)
            await eng.step()
            await drain()
            eng.on_private_state(True)
            eng.private_snapshot = True
        assert ex.true_floor() >= hard_floor - 1e-6
    ex.p_5xx = 0
    await eng.shutdown()
    loss = start_value - ex.settle_value()
    return ex, eng, loss, budget, worst_seen


@pytest.mark.parametrize("seed", range(12))
def test_loss_never_exceeds_weekly_cap(seed, tmp_path):
    ex, eng, loss, budget, worst = asyncio.run(run_sim(seed, tmp_path))
    assert loss <= budget + 1e-6, f"seed {seed}: settled loss {loss:.4f} > {budget}"
    assert ex.fills > 0, "engine never traded; test is vacuous"
    assert eng.halted is None, f"false halt: {eng.halted}"


def test_fully_informed_adversary_only(tmp_path):
    # Every fill we get is a bad one; the cap must still hold.
    ex, eng, loss, budget, worst = asyncio.run(
        run_sim(99, tmp_path, informed_rate=0.9, noise_rate=0.0, steps=2000))
    assert ex.bad_fills == ex.fills > 0
    assert loss <= budget + 1e-6


def test_preexisting_losing_position_is_respected(tmp_path):
    # Start the week already holding a short; floor accounting must include it.
    ex, eng, loss, budget, worst = asyncio.run(
        run_sim(5, tmp_path, initial_positions={"rtc-dwts-test-0": -3.0}))
    assert loss <= budget + 1e-6


def test_restart_does_not_reset_budget(tmp_path):
    async def go():
        rng = random.Random(3)
        ex = FakeExchange(rng, p_ambiguous=0, p_5xx=0)
        cfg = Config()
        cfg.trading_end_utc = iso_utc(T0 + 12 * 3600)
        j = Journal(tmp_path / "j.jsonl")
        e1 = Engine(cfg, ex, j, init_week=True, clock=lambda: ex.t)
        await e1.bootstrap()
        a1 = e1.anchor
        ex.cash -= 4.0  # lose $4 between runs
        e2 = Engine(cfg, ex, j, init_week=False, clock=lambda: ex.t)
        await e2.bootstrap()
        assert e2.anchor == a1  # anchor recovered from journal, loss still counted
        assert e2.floor() - e2.limit() == pytest.approx(9.5 - 4.0)
        # A fresh journal without --init-week refuses to start.
        e3 = Engine(cfg, ex, Journal(tmp_path / "empty.jsonl"), clock=lambda: ex.t)
        with pytest.raises(RuntimeError, match="init-week"):
            await e3.bootstrap()
    asyncio.run(go())


def test_inverted_position_sign_halts(tmp_path):
    ex, eng, loss, budget, worst = asyncio.run(
        run_sim(11, tmp_path, invert_sign=True, p_drop_ws=0.0, p_ambiguous=0.0, p_5xx=0.0))
    assert eng.halted and "sign" in eng.halted
    assert loss <= budget + 1e-6


def test_dynamic_selection_only_adds_risk_where_eligible(tmp_path):
    # market 0: wide + active (eligible)    market 1: wide + dead
    # market 2: tight + active              market 3: tight + dead      market 4: tight + dead
    ex, eng, loss, budget, worst = asyncio.run(run_sim(
        21, tmp_path, selection=True, steps=1200, p_ambiguous=0, p_5xx=0, jump_rate=0.0,
        wide={0, 1}, lifetime_notional={0: 400.0, 2: 400.0},
        initial_positions={"rtc-dwts-test-3": 2.0}))
    increasing = {"ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"}
    s = ex.slugs
    assert any(i in increasing for i in ex.intents[s[0]]), "eligible market never quoted"
    for m in s[1:]:
        assert not any(i in increasing for i in ex.intents[m]), f"{m} got risk-increasing orders"
    # Inventory in an ineligible market is still worked off with reducing quotes.
    assert "ORDER_INTENT_SELL_LONG" in ex.intents[s[3]]
    assert loss <= budget + 1e-6
