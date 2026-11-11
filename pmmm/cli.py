"""Command line entry point.

  python -m pmmm check       read-only: auth, clock, markets, books, preview (not place) an order
  python -m pmmm status      read-only: balance, positions, open orders, worst-case floor vs limit
  python -m pmmm run         trade live
  python -m pmmm cancel-all  cancel every open order in the event's markets
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .api import PolymarketClient
from .auth import Signer
from .config import load_config
from .engine import Engine
from .journal import Journal, week_of
from .strategy import INTENT_BUY_LONG
from .util import fmt_step, iso_utc
from .ws import WsClient

log = logging.getLogger("pmmm")


def setup_logging(state_dir: str, verbose: bool) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(Path(state_dir) / "mm.log")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def make_client(cfg) -> PolymarketClient:
    signer = Signer(os.environ.get("POLYMARKET_KEY_ID", ""), os.environ.get("POLYMARKET_SECRET_KEY", ""))
    return PolymarketClient(signer, cfg.api_base, cfg.gateway_base, rps=cfg.max_requests_per_s)


def make_engine(cfg, client, init_week=False) -> Engine:
    return Engine(cfg, client, Journal(Path(cfg.state_dir) / "journal.jsonl"), init_week=init_week)


async def cmd_run(cfg, args) -> int:
    client = make_client(cfg)
    eng = make_engine(cfg, client, init_week=args.init_week)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.stop.set)
    try:
        if time.time() >= cfg.trading_end_ts:
            log.error("trading_end_utc %s is in the past; nothing to do", cfg.trading_end_utc)
            return 1
        await eng.bootstrap()
        signer = client.signer
        ws_m = WsClient(cfg.ws_base, "/v1/ws/markets", signer,
                        [("SUBSCRIPTION_TYPE_MARKET_DATA", eng.slugs), ("SUBSCRIPTION_TYPE_TRADE", eng.slugs)],
                        eng.on_market_message, eng.on_market_state, name="markets")
        ws_p = WsClient(cfg.ws_base, "/v1/ws/private", signer,
                        [("SUBSCRIPTION_TYPE_ORDER", eng.slugs), ("SUBSCRIPTION_TYPE_POSITION", eng.slugs),
                         ("SUBSCRIPTION_TYPE_ACCOUNT_BALANCE", [])],
                        eng.on_private_message, eng.on_private_state, name="private")
        log.warning("LIVE TRADING %s until %s | weekly loss cap $%.2f (buffer $%.2f)",
                    cfg.event_slug, cfg.trading_end_utc, cfg.risk.weekly_loss_limit, cfg.risk.safety_buffer)
        tasks = [asyncio.create_task(c) for c in (
            ws_m.run(eng.stop), ws_p.run(eng.stop), eng.resync_loop(), eng.inflow_loop(),
            eng.book_loop(), eng.tick_loop(), eng.event_loop())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if t.exception():
                log.error("task failed: %r", t.exception())
        eng.stop.set()
        await asyncio.wait(pending, timeout=10)
    except Exception:
        log.exception("fatal error")
    finally:
        try:
            await eng.shutdown()
        finally:
            await client.close()
    return 2 if eng.halted else 0


async def cmd_status(cfg, args) -> int:
    client = make_client(cfg)
    eng = make_engine(cfg, client)
    try:
        await eng.load_markets()
        await eng.resync()
        wid, _ = week_of(time.time())
        rec = eng.journal.find_anchor(wid)
        print(f"week {wid}  cash ${eng.cash:.2f}")
        print("positions:", {k: v for k, v in eng.positions.items()} or "none")
        live = [o for o in eng.orders.values() if o.state != "terminal"]
        print(f"open orders in event: {len(live)}  foreign open orders: {len(eng.foreign)}")
        f = eng.floor()
        print(f"worst-case floor (positions + open orders): ${f:.2f}")
        if rec:
            eng.anchor, eng.anchor_ts = float(rec["anchor"]), float(rec["anchor_ts"])
            await eng.refresh_inflows()
            lim = eng.limit()
            print(f"anchor ${eng.anchor:.2f}  inflows ${eng.inflows:.2f}  limit ${lim:.2f}  "
                  f"remaining worst-case budget ${f - lim:.2f}")
        else:
            print("no anchor recorded for this week yet (first `run` will need --init-week)")
    finally:
        await client.close()
    return 0


async def cmd_cancel_all(cfg, args) -> int:
    client = make_client(cfg)
    eng = make_engine(cfg, client)
    try:
        await eng.load_markets()
        print(await client.cancel_all(eng.slugs))
    finally:
        await client.close()
    return 0


async def cmd_check(cfg, args) -> int:
    client = make_client(cfg)
    eng = make_engine(cfg, client)
    ok = True
    try:
        await eng.load_markets()
        print(f"event {cfg.event_slug}: {len(eng.models)} markets")
        skew = client.clock_skew_s
        print(f"clock skew vs server: {skew:+.2f}s" if skew is not None else "clock skew: unknown")
        for slug, m in eng.models.items():
            eng._apply_book(await client.get_book(slug))
            print(f"  {m.title:<18} tick {m.tick} minQty {m.min_qty} fee {m.fee_coeff} "
                  f"state {m.state} bid {m.bids[:1]} ask {m.offers[:1]}")
        bal = await client.balances()
        print(f"auth OK. currentBalance {bal.get('currentBalance')} buyingPower {bal.get('buyingPower')}")
        await eng.resync()  # GET with query params: validates path-only request signing
        print(f"positions {eng.positions}  open orders {len(eng.orders)}")
        acts = await client.activities(["ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT"])
        print(f"activities OK ({len(acts.get('activities') or [])} recent deposits visible)")
        slug = next(iter(eng.models))
        m = eng.models[slug]
        body = {"marketSlug": slug, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": fmt_step(0.01, m.tick), "currency": "USD"},
                "quantity": float(fmt_step(max(m.min_qty, 1.0), m.min_qty)),
                "tif": "TIME_IN_FORCE_GOOD_TILL_DATE", "goodTillTime": iso_utc(time.time() + 60),
                "intent": INTENT_BUY_LONG, "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "participateDontInitiate": True}
        try:
            print("preview (NOT placed):", await client.preview_order(body))
        except Exception as e:
            ok = False
            print(f"PREVIEW FAILED: {e}\n  -> the GTD/post-only order format was not accepted; do not run live.")
        # WebSocket handshake + subscription check.
        got: dict[str, bool] = {}

        async def probe(path, subs, key):
            stop = asyncio.Event()

            def on_msg(msg):
                got[key] = True
                print(f"  {key} ws message keys: {list(msg)[:4]}")
                stop.set()
            ws = WsClient(cfg.ws_base, path, client.signer, subs, on_msg, name=key)
            try:
                await asyncio.wait_for(ws.run(stop), timeout=10)
            except asyncio.TimeoutError:
                pass
        await probe("/v1/ws/markets", [("SUBSCRIPTION_TYPE_MARKET_DATA", eng.slugs)], "markets")
        await probe("/v1/ws/private", [("SUBSCRIPTION_TYPE_ORDER", eng.slugs)], "private")
        for k in ("markets", "private"):
            if not got.get(k):
                print(f"WARNING: no {k} websocket data within 10s")
                ok = ok and k == "markets"  # market data has a REST fallback; private does not
    finally:
        await client.close()
    print("CHECK PASSED" if ok else "CHECK FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pmmm", description="Polymarket US market maker")
    p.add_argument("command", choices=["run", "status", "check", "cancel-all"])
    p.add_argument("--config", default="config.toml")
    p.add_argument("--init-week", action="store_true",
                   help="record this week's loss anchor now (first run of the week / first ever run)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config(args.config if Path(args.config).exists() else None)
    setup_logging(cfg.state_dir, args.verbose)
    fn = {"run": cmd_run, "status": cmd_status, "check": cmd_check, "cancel-all": cmd_cancel_all}[args.command]
    return asyncio.run(fn(cfg, args))
