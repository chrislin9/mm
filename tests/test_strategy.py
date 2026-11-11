import random

import pytest

from pmmm.config import StrategyConfig
from pmmm.strategy import (INTENT_BUY_LONG, INTENT_BUY_SHORT, INTENT_SELL_LONG, INTENT_SELL_SHORT,
                           MarketModel, QuoteInputs, compute_quotes)
from pmmm.util import ceil_to, floor_to, parse_ts


def test_rounding():
    assert floor_to(0.46499999999, 0.01) == 0.46
    assert ceil_to(0.4650000001, 0.01) == 0.47
    assert floor_to(0.47, 0.01) == 0.47
    assert ceil_to(0.47, 0.01) == 0.47
    assert parse_ts("2026-11-10T03:51:17.893847389Z") == pytest.approx(1794282677.893847, abs=1e-5)


def test_quotes_basic_properties_random():
    c = StrategyConfig()
    rng = random.Random(7)
    for _ in range(5000):
        fv = round(rng.uniform(0.02, 0.98), 3)
        eb = round(fv - rng.choice([0.01, 0.02, 0.03, 0.05]), 2) if rng.random() < 0.9 else None
        ea = round(fv + rng.choice([0.01, 0.02, 0.03, 0.05]), 2) if rng.random() < 0.9 else None
        q = rng.choice([0.0, rng.uniform(-10, 10)])
        inp = QuoteInputs(fv=fv, tick=0.01, min_qty=0.01, ext_bid=eb, ext_ask=ea, q=q,
                          vol=rng.uniform(0, 0.01), tox_mult=rng.uniform(1, 3), time_mult=rng.uniform(1, 2),
                          bid_fade_ticks=rng.randint(0, 1), ask_fade_ticks=rng.randint(0, 1),
                          reduce_only=rng.random() < 0.2)
        bid, ask = compute_quotes(inp, c)
        if bid:
            assert bid.is_buy and bid.qty >= 0.01
            assert 0.01 <= bid.price <= 0.99
            assert ea is None or bid.price < ea - 1e-9            # post-only
            edge = c.reduce_edge if q < 0 else c.min_edge
            assert bid.price <= fv - edge + 1e-9                  # never pay up through fair value
            assert bid.intent == (INTENT_SELL_SHORT if q < 0 else INTENT_BUY_LONG)
            if q < 0:
                assert bid.qty <= abs(q) + 1e-9                    # never flips in one order
            if inp.reduce_only:
                assert q < 0
        if ask:
            assert not ask.is_buy and 0.01 <= ask.price <= 0.99
            assert eb is None or ask.price > eb + 1e-9
            edge = c.reduce_edge if q > 0 else c.min_edge
            assert ask.price >= fv + edge - 1e-9
            assert ask.intent == (INTENT_SELL_LONG if q > 0 else INTENT_BUY_SHORT)
            if q > 0:
                assert ask.qty <= q + 1e-9
            if inp.reduce_only:
                assert q > 0
        if bid and ask:
            assert bid.price < ask.price


def test_inventory_skew_and_limit():
    c = StrategyConfig()
    base = dict(fv=0.50, tick=0.01, min_qty=0.01, ext_bid=0.45, ext_ask=0.55, max_order_risk=1.0, max_market_risk=2.5)
    b0, a0 = compute_quotes(QuoteInputs(q=0, **base), c)
    bl, al = compute_quotes(QuoteInputs(q=4, **base), c)     # long $2 of $2.5
    assert bl.price <= b0.price and al.price <= a0.price     # skewed down to shed inventory
    assert bl.qty < b0.qty                                   # buys shrink
    bmax, amax = compute_quotes(QuoteInputs(q=5, **base), c)  # at limit
    assert bmax is None and amax is not None and amax.intent == INTENT_SELL_LONG


def _model():
    return MarketModel("m", "M", 0.01, 0.01, 0.095, StrategyConfig())


def test_depth_filter_ignores_dust_and_own_orders():
    m = _model()
    m.update_book([(0.49, 0.5), (0.47, 20)], [(0.51, 2.0), (0.53, 30)], "MARKET_STATE_OPEN", None, 0.0)
    m.observe(0.0, {}, {round(0.51, 6): 2.0})   # our own 2.0 at 0.51 is excluded
    assert m.ext_bid == 0.49 and m.ext_ask == 0.53
    assert m.f_bid == 0.47 and m.f_ask == 0.53
    assert m.mid == pytest.approx(0.50)


def test_jump_pauses_market():
    m = _model()
    m.update_book([(0.47, 20)], [(0.49, 20)], "MARKET_STATE_OPEN", None, 0.0)
    assert m.observe(0.0, {}, {}) is None
    m.update_book([(0.55, 20)], [(0.57, 20)], "MARKET_STATE_OPEN", None, 1.0)
    assert m.observe(1.0, {}, {}) == pytest.approx(0.08)
    assert m.pause_until > 1.0


def test_fill_cooldown_and_toxicity():
    m = _model()
    m.update_book([(0.47, 20)], [(0.49, 20)], "MARKET_STATE_OPEN", None, 0.0)
    m.observe(0.0, {}, {})
    m.fair_value(0.0, None)
    m.on_own_fill(0.0, True, 0.47, 1)
    assert m.side_pause_until[True] > 0 and m.side_pause_until[False] == 0
    # Price then drops 3c: our buys were picked off.
    for i, t in enumerate((40.0, 80.0, 120.0)):
        m.on_own_fill(t - 35, True, 0.47, 1)
    m.update_book([(0.43, 20)], [(0.45, 20)], "MARKET_STATE_OPEN", None, 130.0)
    m.observe(130.0, {}, {})
    m.fair_value(130.0, None)
    m.evaluate_markouts(130.0)
    assert m.markout < 0 and m.tox_mult() > 1.0
    assert m.pause_until > 130.0  # toxic markouts pause the market


def _sel_model(**cfg_kw):
    c = StrategyConfig(**cfg_kw)
    m = MarketModel("m", "M", 0.01, 0.01, 0.095, c)
    m.listed_ts = 0.0
    return m


def _book(m, bid, ask, now, notional):
    m.update_book([(bid, 50)], [(ask, 50)], "MARKET_STATE_OPEN", None, now, notional)
    m.observe(now, {}, {})


def test_selection_prior_dwell_and_hysteresis():
    m = _sel_model()
    t0 = 100 * 3600.0                      # market open 100h, $500 lifetime -> $5/h prior
    _book(m, 0.44, 0.48, t0, 500.0)
    assert m.activity_rate(t0) == pytest.approx(5.0)
    assert not m.update_eligibility(t0)    # must hold for the dwell period first
    _book(m, 0.44, 0.48, t0 + 31, 500.0)
    assert m.update_eligibility(t0 + 31)
    _book(m, 0.44, 0.46, t0 + 40, 500.0)   # 2c: below entry, not below exit -> stays
    assert m.update_eligibility(t0 + 40)
    _book(m, 0.44, 0.45, t0 + 50, 500.0)   # 1c -> leaves immediately
    assert not m.update_eligibility(t0 + 50)
    _book(m, 0.44, 0.46, t0 + 60, 500.0)   # back to 2c: not enough to re-enter
    assert not m.update_eligibility(t0 + 100)


def test_selection_dead_market_and_own_fills_dont_count():
    m = _sel_model()
    t0 = 100 * 3600.0
    _book(m, 0.05, 0.10, t0, 8.0)          # $8 lifetime -> $0.08/h: dead despite 5c spread
    for k in range(1, 5):
        _book(m, 0.05, 0.10, t0 + 30 * k, 8.0)
    assert not m.update_eligibility(t0 + 120)
    assert "activity" in m.eligible_reason
    # We trade $20 there ourselves: volume rises, but it's ours, so still dead.
    m.on_own_fill(t0 + 130, True, 0.05, 400)
    _book(m, 0.05, 0.10, t0 + 131, 28.0)
    assert m.activity_rate(t0 + 131) < 1.0
    # Someone else trades $20 -> the live window lifts the estimate above $1/h.
    _book(m, 0.05, 0.10, t0 + 140, 48.0)
    assert m.activity_rate(t0 + 140) > 1.0


def test_selection_can_be_disabled():
    m = _sel_model(dynamic_market_selection=False)
    _book(m, 0.44, 0.45, 0.0, None)
    assert m.update_eligibility(0.0)
