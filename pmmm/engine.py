"""Market-making engine: state, order management, risk gating, main loop."""
from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import risk
from .api import Ambiguous, AuthError, BadRequest, NotFound, PolymarketClient, RateLimited
from .config import Config
from .journal import Journal, week_of
from .strategy import MarketModel, Quote, QuoteInputs, compute_quotes, intent_is_buy
from .util import floor_to, fmt_step, fnum, iso_utc, parse_ts

log = logging.getLogger(__name__)

TERMINAL_EXEC = {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED",
                 "EXECUTION_TYPE_EXPIRED", "EXECUTION_TYPE_DONE_FOR_DAY"}
TERMINAL_STATES = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REPLACED",
                   "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"}
FILL_EXEC = {"EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"}
INFLOW_TYPES_STRICT = ["ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT"]
INFLOW_TYPES_LENIENT = ["ACTIVITY_TYPE_REFERRAL_BONUS", "ACTIVITY_TYPE_TRANSFER",
                        "ACTIVITY_TYPE_TAKER_FEE_REBATE", "ACTIVITY_TYPE_LIQUIDITY_PROGRAM"]
EXTERNAL_LEDGER = {"LEDGER_ENTRY_TYPE_DEPOSIT", "LEDGER_ENTRY_TYPE_CORRECTION",
                   "LEDGER_ENTRY_TYPE_MANUAL_ADJUSTMENT"}


class Halt(Exception):
    pass


@dataclass
class LiveOrder:
    key: str
    market: str
    is_buy: bool
    intent: str
    price: float
    qty: float
    created: float
    expires_at: float
    fee: float = 0.0
    id: str | None = None
    leaves: float | None = None
    # inflight | open | cancel_pending | suspect | unknown | terminal
    state: str = "inflight"
    terminal_at: float | None = None
    adopted: bool = False
    last_cancel_try: float = 0.0
    last_verify: float = 0.0

    def exposure(self) -> risk.Exposure:
        if self.adopted:
            # Unknown provenance: assume the worst price for risk purposes.
            return risk.Exposure(self.market, self.is_buy, 1.0 if self.is_buy else 0.0, self.qty, 0.0)
        return risk.Exposure(self.market, self.is_buy, self.price, self.qty, self.fee)

    def mark_terminal(self, now: float) -> None:
        if self.state != "terminal":
            self.state = "terminal"
            self.terminal_at = now

    @property
    def remaining(self) -> float:
        return self.leaves if self.leaves is not None else self.qty


class Engine:
    def __init__(self, cfg: Config, client: PolymarketClient, journal: Journal, *,
                 init_week: bool = False, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.client = client
        self.journal = journal
        self.init_week = init_week
        self.clock = clock
        self.stop = asyncio.Event()
        self.models: dict[str, MarketModel] = {}
        self.orders: dict[str, LiveOrder] = {}
        self.by_id: dict[str, LiveOrder] = {}
        self.pending_execs: dict[str, tuple[float, list]] = {}
        self.seen_execs: set[str] = set()
        self.foreign: list[risk.Exposure] = []
        self.positions: dict[str, float] = {}
        self.cash: float | None = None
        self.last_resync_ok = 0.0
        self.last_resync_start = 0.0
        self.resync_soon = False
        self.fills_since_resync: dict[str, float] = defaultdict(float)
        self.last_fill_ts = 0.0
        self.sign_mismatches = 0
        self.week_id: str | None = None
        self.anchor: float | None = None
        self.anchor_ts: float | None = None
        self.inflows = 0.0
        self.inflows_stale = True
        self.last_inflow_refresh = 0.0
        self.private_connected = False
        self.market_ws_up = False
        self.private_connected_at = 0.0
        self.private_snapshot = False
        self.halted: str | None = None
        self.reject_streak = 0
        self.event_pause_until = 0.0
        self.recent_jumps: list[tuple[float, str, float]] = []
        self.blocked: dict[str, dict] = {}
        self.last_place: dict[tuple[str, bool], float] = defaultdict(float)
        self.last_cancel_all = 0.0
        self.last_pull_reason = ""
        self.session_start_equity: float | None = None
        self.soft_stopped = False
        self.last_status_log = 0.0
        self._tasks: set[asyncio.Task] = set()
        self.kill_path = Path(cfg.kill_file)

    # ================================================================== helpers
    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    @property
    def slugs(self) -> list[str]:
        return list(self.models)

    def exposures(self, exclude: set[str] = frozenset()) -> list[risk.Exposure]:
        ex = [o.exposure() for k, o in self.orders.items() if k not in exclude]
        return ex + self.foreign

    def limit(self) -> float:
        r = self.cfg.risk
        return self.anchor + self.inflows - r.weekly_loss_limit + r.safety_buffer

    def floor(self, exclude: set[str] = frozenset()) -> float:
        return risk.account_floor(self.cash, self.positions, self.exposures(exclude))

    def state_fresh(self, now: float) -> bool:
        return (self.cash is not None and self.anchor is not None
                and now - self.last_resync_ok <= self.cfg.risk.max_state_age_s)

    def fee_for(self, model: MarketModel, price: float) -> float:
        if not self.cfg.risk.include_fee_allowance:
            return 0.0
        return risk.fee_per_contract(model.fee_coeff, price)

    def do_halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = reason
            log.critical("HALT: %s", reason)
            self.journal.write("halt", reason=reason)
        self.stop.set()

    # ================================================================== bootstrap
    async def load_markets(self) -> None:
        ev = await self.client.get_event(self.cfg.event_slug)
        if not ev:
            raise RuntimeError(f"event {self.cfg.event_slug} not found")
        s = self.cfg.strategy
        for m in ev.get("markets") or []:
            slug = m.get("slug")
            if not slug:
                continue
            if self.cfg.include_markets and slug not in self.cfg.include_markets:
                continue
            if slug in self.cfg.exclude_markets:
                continue
            if not m.get("active") or m.get("closed") or m.get("archived"):
                continue
            tick = fnum(m.get("orderPriceMinTickSize"), 0.01) or 0.01
            min_qty = fnum(m.get("minimumTradeQty"), 1.0) or 1.0
            fee = fnum(m.get("feeCoefficient"), 0.1)
            if slug in self.models:
                mm = self.models[slug]
                mm.tick, mm.min_qty, mm.fee_coeff = tick, min_qty, fee
            else:
                model = MarketModel(slug, m.get("title") or slug, tick, min_qty, fee, s)
                listed = m.get("startDate") or m.get("createdAt")
                if listed:
                    try:
                        model.listed_ts = parse_ts(listed)
                    except ValueError:
                        pass
                self.models[slug] = model
        if not self.models:
            raise RuntimeError("no tradable markets found in event")

    async def bootstrap(self) -> None:
        await self.load_markets()
        log.info("markets: %s", ", ".join(f"{m.title}({k})" for k, m in self.models.items()))
        skew = self.client.clock_skew_s
        if skew is not None and abs(skew) > 10:
            raise RuntimeError(f"local clock is off by {skew:+.1f}s; fix NTP before trading")
        await self._retry(lambda: self.client.cancel_all(self.slugs), "startup cancel_all")
        log.info("startup: cancelled any open orders in event markets")
        await asyncio.sleep(0.5)
        await self._retry(self.resync, "startup resync")
        now = self.clock()
        wid, _ = week_of(now)
        rec = self.journal.find_anchor(wid)
        if rec is not None:
            self.anchor, self.anchor_ts = float(rec["anchor"]), float(rec["anchor_ts"])
            log.info("week %s anchor %.2f (from journal)", wid, self.anchor)
        elif self.init_week or self.journal.has_any_anchor():
            self._set_anchor(wid, now, "init" if self.init_week else "new week")
        else:
            raise RuntimeError(
                f"No loss anchor recorded for week {wid}. If this is the first run this week, "
                "start once with --init-week. (Refusing to guess: a lost journal must not reset "
                "the weekly loss budget.)")
        self.week_id = wid
        await self._retry(self.refresh_inflows, "startup deposit check")
        for slug in self.slugs:
            try:
                self._apply_book(await self.client.get_book(slug))
            except Exception as e:
                log.warning("initial book %s failed: %s", slug, e)
        f, lim = self.floor(), self.limit()
        log.info("cash %.2f  floor %.2f  limit %.2f  headroom %.2f", self.cash, f, lim, f - lim)
        if f < lim:
            log.warning("worst-case floor already below this week's limit: reduce-only")

    @staticmethod
    async def _retry(fn, what: str, attempts: int = 5):
        for i in range(attempts):
            try:
                return await fn()
            except (AuthError, BadRequest):
                raise
            except Exception as e:
                if i == attempts - 1:
                    raise
                log.warning("%s failed (%s), retrying", what, e)
                await asyncio.sleep(min(0.5 * 2 ** i, 4))

    def _set_anchor(self, wid: str, now: float, reason: str) -> None:
        self.anchor = risk.position_floor(self.cash, self.positions)
        self.anchor_ts = now
        self.inflows = 0.0
        self.journal.write("anchor", week=wid, anchor=self.anchor, anchor_ts=now, reason=reason,
                           cash=self.cash, positions=self.positions)
        log.warning("week %s anchor set to %.2f (%s)", wid, self.anchor, reason)

    # ================================================================== resync
    async def resync(self) -> None:
        start = self.clock()
        fills_at_start = dict(self.fills_since_resync)
        oo = await self.client.open_orders()
        pos_raw = await self.client.positions()
        bal = await self.client.balances()
        cash = fnum(bal.get("currentBalance"))
        if cash is None:
            raise RuntimeError(f"balance response missing currentBalance: {bal}")
        positions: dict[str, float] = {}
        for slug, p in pos_raw.items():
            q = fnum(p.get("netPositionDecimal"), fnum(p.get("netPosition")))
            if q is None:
                raise RuntimeError(f"unparseable position for {slug}: {p}")
            if abs(q) > 1e-9:
                positions[slug] = q
        self._apply_open_orders(oo, start)
        self._check_position_changes(positions, start, fills_at_start)
        if self.cash is not None and cash - self.cash > self.cfg.risk.unexplained_cash_jump \
                and start - self.last_fill_ts > 15:
            log.warning("cash rose %.2f with no recent fills: re-reading deposits before trading",
                        cash - self.cash)
            self.inflows_stale = True
        self.positions, self.cash = positions, cash
        for m, v in fills_at_start.items():  # fills that arrived mid-resync carry over
            self.fills_since_resync[m] -= v
        self.last_resync_start = start
        self.last_resync_ok = self.clock()
        lag = self.cfg.risk.settle_lag_s
        for k, o in list(self.orders.items()):
            if o.state == "terminal" and o.terminal_at is not None and o.terminal_at + lag <= start:
                del self.orders[k]
                if o.id:
                    self.by_id.pop(o.id, None)
        for m, b in list(self.blocked.items()):
            if b.get("cancel_done_at") and start >= b["cancel_done_at"] + lag:
                log.info("%s unblocked after cleanup", m)
                del self.blocked[m]

    def _check_position_changes(self, new: dict[str, float], now: float, fills: dict[str, float]) -> None:
        if self.cash is None:
            return
        for m, model in self.models.items():
            old_q, new_q = self.positions.get(m, 0.0), new.get(m, 0.0)
            delta = new_q - old_q
            if abs(delta) < model.min_qty / 2:
                continue
            seen = fills.get(m, 0.0)
            tol = max(model.min_qty, 0.01 * abs(seen))
            if abs(seen) >= model.min_qty and abs(delta + seen) <= tol and abs(delta - seen) > tol:
                # Exactly the signature of an inverted sign convention (missed WS messages
                # make other mismatches common, so only this pattern counts).
                self.sign_mismatches += 1
                log.error("%s: position moved %+.2f but our fills say %+.2f", m, delta, seen)
                if self.sign_mismatches >= 2:
                    self.do_halt("position sign convention appears inverted")
                    return
            if abs(delta - seen) > tol:
                log.warning("%s: position changed %+.2f, fills seen %+.2f (missed fills?)", m, delta, seen)
                model.on_unexplained_fill(now)
                self.last_fill_ts = now

    def _apply_open_orders(self, oo: list[dict], start: float) -> None:
        seen: set[str] = set()
        foreign: list[risk.Exposure] = []
        for r in oo:
            oid = r.get("id")
            m = r.get("marketSlug") or (r.get("marketMetadata") or {}).get("slug")
            is_buy = intent_is_buy(r.get("intent"), r.get("outcomeSide"), r.get("action"))
            px = fnum(r.get("price"))
            qty = fnum(r.get("leavesQuantity"), fnum(r.get("quantity"))) or 0.0
            if is_buy is None or px is None:
                is_buy, px = True, 1.0  # unknown -> count as a worst-case buy
                if not oid:
                    continue
            if m not in self.models:
                # Not ours (e.g. a manual order elsewhere). Count it, worst-case priced.
                foreign.append(risk.Exposure(m or "?", is_buy, 1.0 if is_buy else 0.0, qty, 0.0))
                continue
            if not oid:
                continue
            seen.add(oid)
            o = self.by_id.get(oid)
            if o is None:
                o = self._match_unknown(m, is_buy, px, fnum(r.get("quantity"), qty))
                if o is not None:
                    o.id = oid
                    o.state = "open"
                    self.by_id[oid] = o
                    log.info("linked ambiguous order to %s", oid)
                else:
                    o = LiveOrder(key=uuid.uuid4().hex, market=m, is_buy=is_buy,
                                  intent=r.get("intent") or "", price=px, qty=qty, created=start,
                                  expires_at=math.inf, id=oid, state="open", adopted=True)
                    self.orders[o.key] = o
                    self.by_id[oid] = o
                    log.warning("adopted unknown open order %s in %s (will cancel)", oid, m)
            elif o.state == "terminal" and o.terminal_at is not None and o.terminal_at < start - 2:
                o.state, o.terminal_at = "suspect", None
            o.leaves = qty
        self.foreign = foreign
        for o in self.orders.values():
            if o.id and o.id not in seen and o.state in ("open", "cancel_pending") \
                    and o.created < start - 3:
                o.state = "suspect"

    def _match_unknown(self, m: str, is_buy: bool, px: float, qty: float) -> LiveOrder | None:
        for o in self.orders.values():
            if o.state == "unknown" and o.market == m and o.is_buy == is_buy \
                    and abs(o.price - px) < 1e-9 and abs(o.qty - qty) < 1e-6:
                return o
        return None

    async def refresh_inflows(self) -> None:
        total = 0.0
        for types, strict in ((INFLOW_TYPES_STRICT, True), (INFLOW_TYPES_LENIENT, False)):
            cursor = None
            for _ in range(20):
                resp = await self.client.activities(types, cursor)
                done = False
                for a in resp.get("activities") or []:
                    abc = a.get("accountBalanceChange") or {}
                    ts_s = abc.get("createTime") or abc.get("updateTime")
                    amt = fnum(abc.get("amount"))
                    if ts_s is None or amt is None:
                        if strict:
                            raise RuntimeError(f"cannot parse deposit activity: {a}")
                        log.warning("ignoring unparseable activity: %s", a)
                        continue
                    if parse_ts(ts_s) < self.anchor_ts:
                        done = True
                        continue
                    if abc.get("status") == "ACCOUNT_BALANCE_CHANGE_STATUS_REJECTED":
                        continue
                    if amt > 0:
                        total += amt
                cursor = resp.get("nextCursor")
                if done or resp.get("eof", True) or not cursor:
                    break
        if abs(total - self.inflows) > 1e-9:
            self.journal.write("inflows", week=self.week_id, inflows=total)
            log.info("external inflows since anchor: %.2f", total)
        self.inflows = total
        self.inflows_stale = False
        self.last_inflow_refresh = self.clock()

    # ================================================================== market data
    def _apply_book(self, md: dict, live: bool = False) -> None:
        slug = md.get("marketSlug")
        model = self.models.get(slug)
        if model is None:
            return
        def lv(levels):
            out = []
            for l in levels or []:
                p, q = fnum(l.get("px")), fnum(l.get("qty"))
                if p is not None and q is not None:
                    out.append((p, q))
            return out
        stats = md.get("stats") or {}
        # WS updates are live (age 0); REST snapshots carry their CDN cache age.
        ts = self.clock() - max(0.0, fnum(md.get("_age_s"), 0.0) or 0.0)
        model.update_book(lv(md.get("bids")), lv(md.get("offers")), md.get("state"),
                          fnum(stats.get("sharesTraded")), ts, fnum(stats.get("notionalTraded")))
        if live and self.market_ws_up:
            model.ws_live = True

    def on_market_message(self, msg: dict) -> None:
        if "marketData" in msg:
            self._apply_book(msg["marketData"], live=True)
        elif "trade" in msg:
            t = msg["trade"]
            model = self.models.get(t.get("marketSlug"))
            px, qty = fnum(t.get("price")), fnum(t.get("quantity"))
            if model is None or px is None or qty is None:
                return
            taker = t.get("taker") or {}
            model.on_trade(self.clock(), px, qty, intent_is_buy(taker.get("intent")))

    def on_market_state(self, up: bool) -> None:
        self.market_ws_up = up
        for model in self.models.values():
            model.ws_live = False

    def book_fresh(self, model: MarketModel, now: float) -> bool:
        # The markets stream may only push on change; a quiet book is still current while
        # the socket that delivered it is alive (keepalive pings detect death in <~10s).
        return (self.market_ws_up and model.ws_live) or now - model.book_ts <= self.cfg.strategy.book_stale_s

    # ================================================================== private stream
    def on_private_state(self, up: bool) -> None:
        self.private_connected = up
        self.private_snapshot = False
        if up:
            self.private_connected_at = self.clock()
            self.resync_soon = True

    def on_private_message(self, msg: dict) -> None:
        if "orderSubscriptionSnapshot" in msg:
            self.private_snapshot = True
            self.resync_soon = True
        elif "orderSubscriptionUpdate" in msg:
            ex = (msg["orderSubscriptionUpdate"] or {}).get("execution") or {}
            self.on_execution(ex)
        elif "positionSubscription" in msg:
            self.resync_soon = True
        elif "accountBalancesUpdate" in msg:
            bc = (msg["accountBalancesUpdate"] or {}).get("balanceChange") or {}
            if bc.get("entryType") in EXTERNAL_LEDGER:
                log.warning("external balance change (%s): freezing until deposits re-read",
                            bc.get("entryType"))
                self.inflows_stale = True
            self.resync_soon = True

    def on_execution(self, ex: dict) -> None:
        od = ex.get("order") or {}
        oid = od.get("id")
        if not oid:
            return
        o = self.by_id.get(oid)
        if o is None:
            if any(x.state == "inflight" for x in self.orders.values()):
                # May belong to an order whose create response hasn't returned yet.
                self.pending_execs.setdefault(oid, (self.clock(), []))[1].append(ex)
            else:
                m = od.get("marketSlug")
                if m in self.models and ex.get("type") in FILL_EXEC:
                    self.models[m].on_unexplained_fill(self.clock())
                self.resync_soon = True
            return
        self._apply_exec(o, ex)

    def _apply_exec(self, o: LiveOrder, ex: dict) -> None:
        now = self.clock()
        od = ex.get("order") or {}
        typ = ex.get("type")
        leaves = fnum(od.get("leavesQuantity"))
        if leaves is not None:
            o.leaves = leaves
        if typ in FILL_EXEC:
            eid = ex.get("id") or ex.get("tradeId") or f"{o.id}:{od.get('cumQuantity')}"
            if eid not in self.seen_execs:
                self.seen_execs.add(eid)
                qty = fnum(ex.get("lastShares"), 0.0) or 0.0
                model = self.models.get(o.market)
                if model is not None and qty > 0:
                    model.on_own_fill(now, o.is_buy, o.price, qty)
                self.fills_since_resync[o.market] += qty if o.is_buy else -qty
                self.last_fill_ts = now
                self.resync_soon = True
                self.journal.write("fill", market=o.market, order=o.id, buy=o.is_buy, px=o.price,
                                   qty=qty, aggressor=ex.get("aggressor"))
                log.info("FILL %s %s %.2f @ %.2f", o.market, "BUY" if o.is_buy else "SELL", qty, o.price)
        if typ == "EXECUTION_TYPE_NEW" and o.state in ("inflight", "open", "suspect"):
            o.state = "open"
            self.reject_streak = 0
        if typ == "EXECUTION_TYPE_REJECTED":
            self._note_reject(o, ex.get("text") or ex.get("orderRejectReason") or "rejected")
        if typ in TERMINAL_EXEC or od.get("state") in TERMINAL_STATES:
            o.mark_terminal(now)

    def _note_reject(self, o: LiveOrder, why: str) -> None:
        self.reject_streak += 1
        log.warning("order rejected %s %s @ %.2f: %s (streak %d)", o.market, o.intent, o.price, why,
                    self.reject_streak)
        self.journal.write("reject", market=o.market, intent=o.intent, px=o.price, qty=o.qty, why=str(why))
        if self.reject_streak >= self.cfg.risk.max_consecutive_rejects:
            self.do_halt(f"{self.reject_streak} consecutive rejects; last: {why}")

    # ================================================================== order actions
    def _order_body(self, o: LiveOrder, tick: float, min_qty: float) -> dict:
        return {
            "marketSlug": o.market,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": fmt_step(o.price, tick), "currency": "USD"},
            "quantity": float(fmt_step(o.qty, min_qty)),
            "tif": "TIME_IN_FORCE_GOOD_TILL_DATE",
            "goodTillTime": iso_utc(o.expires_at),
            "intent": o.intent,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "participateDontInitiate": True,
        }

    async def _place(self, o: LiveOrder) -> None:
        model = self.models[o.market]
        body = self._order_body(o, model.tick, model.min_qty)
        try:
            resp = await self.client.create_order(body)
        except (BadRequest, RateLimited) as e:
            o.mark_terminal(self.clock())
            if isinstance(e, BadRequest):
                self._note_reject(o, e.message)
            else:
                log.warning("rate limited on create")
            return
        except AuthError as e:
            o.mark_terminal(self.clock())
            self.do_halt(f"authentication failed: {e}")
            return
        except Exception as e:  # Ambiguous, ServerError, anything else: fate unknown
            log.error("order create outcome unknown (%s): treating as live until expiry", e)
            o.state = "unknown"
            self._block_market(o.market, f"ambiguous create: {e}")
            return
        oid = resp.get("id")
        if not oid:
            log.error("create returned no id: %s", resp)
            o.state = "unknown"
            self._block_market(o.market, "create returned no id")
            return
        o.id = oid
        self.by_id[oid] = o
        if o.state == "inflight":
            o.state = "open"
        self.journal.write("order", id=oid, market=o.market, intent=o.intent, px=o.price, qty=o.qty,
                           exp=o.expires_at)
        for ex in self.pending_execs.pop(oid, (0, []))[1]:
            self._apply_exec(o, ex)

    def _block_market(self, m: str, reason: str) -> None:
        if m in self.blocked and not self.blocked[m].get("cancel_done_at"):
            return
        self.blocked[m] = {"since": self.clock(), "cancel_done_at": None, "reason": reason}

        async def cleanup():
            for i in range(5):
                try:
                    await self.client.cancel_all([m])
                    self.blocked[m]["cancel_done_at"] = self.clock()
                    self.resync_soon = True
                    return
                except Exception as e:
                    log.warning("cleanup cancel_all %s failed: %s", m, e)
                    await asyncio.sleep(1 + i)
        self.spawn(cleanup())

    async def _cancel(self, o: LiveOrder) -> None:
        o.last_cancel_try = self.clock()
        if o.state == "open":
            o.state = "cancel_pending"
        try:
            await self.client.cancel_order(o.id, o.market)
        except (NotFound, BadRequest):
            if o.state != "terminal":
                o.state = "suspect"
        except Exception as e:
            log.warning("cancel %s failed: %s", o.id, e)

    async def _verify(self, o: LiveOrder) -> None:
        o.last_verify = self.clock()
        try:
            od = await self.client.get_order(o.id)
        except NotFound:
            o.mark_terminal(self.clock())
            return
        except Exception as e:
            log.warning("verify %s failed: %s", o.id, e)
            return
        st = od.get("state")
        if st in TERMINAL_STATES:
            leaves = fnum(od.get("leavesQuantity"))
            if leaves is not None:
                o.leaves = leaves
            o.mark_terminal(self.clock())
        elif st and o.state == "suspect":
            o.state = "open"

    def _maybe_cancel(self, o: LiveOrder, now: float) -> None:
        if o.id and o.state != "terminal" and now - o.last_cancel_try >= 2.0:
            o.last_cancel_try = now
            self.spawn(self._cancel(o))

    async def _cancel_all_managed(self) -> None:
        try:
            await self.client.cancel_all(self.slugs)
        except Exception as e:
            log.warning("cancel_all failed: %s", e)

    def pull_all(self, reason: str, now: float) -> None:
        live = [o for o in self.orders.values() if o.state != "terminal"]
        if reason != self.last_pull_reason:
            log.warning("quotes pulled: %s", reason)
            self.last_pull_reason = reason
        if not live:
            return
        if now - self.last_cancel_all >= 2.0:
            self.last_cancel_all = now
            for o in live:
                if o.state == "open":
                    o.state = "cancel_pending"
                    o.last_cancel_try = now
            self.spawn(self._cancel_all_managed())

    # ================================================================== quoting
    def own_levels(self, m: str) -> tuple[dict, dict]:
        b: dict[float, float] = defaultdict(float)
        a: dict[float, float] = defaultdict(float)
        for o in self.orders.values():
            if o.market == m and o.state != "terminal":
                (b if o.is_buy else a)[round(o.price, 6)] += o.remaining
        return b, a

    def budget_size(self, m: str, is_buy: bool, price: float, exclude: set[str]) -> float:
        r = self.cfg.risk
        model = self.models[m]
        exps = self.exposures(exclude)
        headroom = risk.account_floor(self.cash, self.positions, exps) - self.limit()
        mexps = [e for e in exps if e.market == m]
        q = self.positions.get(m, 0.0)
        cur = risk.market_floor(q, mexps)
        target = max(cur - headroom, -r.max_market_risk, cur - r.max_order_risk)
        return risk.max_order_size(q, mexps, is_buy, price, self.fee_for(model, price), target)

    def time_mult(self, now: float) -> float:
        h = (self.cfg.trading_end_ts - now) / 3600
        w = self.cfg.widen_start_hours
        if w <= 0 or h >= w:
            return 1.0
        return 1.0 + (self.cfg.widen_max_mult - 1.0) * (1.0 - max(h, 0.0) / w)

    def reduce_only(self, now: float) -> bool:
        return (self.soft_stopped
                or now >= self.cfg.trading_end_ts - self.cfg.wind_down_minutes * 60
                or self.floor() < self.limit())

    def desired_quotes(self, m: str, now: float, reduce_only: bool) -> dict[bool, Quote | None]:
        none = {True: None, False: None}
        model = self.models[m]
        s = self.cfg.strategy
        if model.state != "MARKET_STATE_OPEN" or not self.book_fresh(model, now):
            return none
        if model.fv is None or now < model.pause_until or now < self.event_pause_until or m in self.blocked:
            return none
        q = self.positions.get(m, 0.0)
        # Ineligible markets (dead or one-tick) still get inventory-reducing quotes.
        reduce_only = reduce_only or not model.eligible
        inp = QuoteInputs(
            fv=model.fv, tick=model.tick, min_qty=model.min_qty, ext_bid=model.ext_bid,
            ext_ask=model.ext_ask, q=q, vol=model.vol, tox_mult=model.tox_mult(),
            time_mult=self.time_mult(now),
            bid_fade_ticks=s.fade_ticks if now < model.side_fade_until[True] else 0,
            ask_fade_ticks=s.fade_ticks if now < model.side_fade_until[False] else 0,
            bid_allowed=now >= model.side_pause_until[True],
            ask_allowed=now >= model.side_pause_until[False],
            reduce_only=reduce_only, max_order_risk=self.cfg.risk.max_order_risk,
            max_market_risk=self.cfg.risk.max_market_risk)
        bid, ask = compute_quotes(inp, s)
        out = {}
        for side, qt in ((True, bid), (False, ask)):
            if qt is None:
                out[side] = None
                continue
            mine = {k for k, o in self.orders.items()
                    if o.market == m and o.is_buy == side and not o.adopted and o.state in ("open", "inflight")}
            size = floor_to(min(qt.qty, self.budget_size(m, side, qt.price, mine)), model.min_qty)
            out[side] = Quote(side, qt.price, size, qt.intent) if size >= model.min_qty - 1e-12 else None
        return out

    def reconcile_market(self, m: str, desired: dict[bool, Quote | None], now: float) -> None:
        s = self.cfg.strategy
        for o in self.orders.values():
            if o.market == m and o.adopted and o.state != "terminal":
                self._maybe_cancel(o, now)
        for side in (True, False):
            mine = [o for o in self.orders.values()
                    if o.market == m and o.is_buy == side and not o.adopted and o.state != "terminal"]
            if any(o.state == "inflight" for o in mine):
                continue
            d = desired.get(side)
            keep = None
            if d is not None:
                for o in mine:
                    if (o.state == "open" and o.intent == d.intent and abs(o.price - d.price) < 1e-9
                            and abs(o.remaining - d.qty) <= s.requote_size_tolerance * d.qty
                            and o.expires_at - now > s.refresh_before_expiry_s):
                        keep = o
                        break
            for o in mine:
                if o is not keep and o.state in ("open", "cancel_pending", "suspect"):
                    self._maybe_cancel(o, now)
            if d is not None and keep is None:
                self._maybe_place(m, d, now)

    def _maybe_place(self, m: str, d: Quote, now: float) -> None:
        s = self.cfg.strategy
        if now - self.last_place[(m, d.is_buy)] < s.min_requote_interval_s:
            return
        for o in self.orders.values():
            if o.market == m and o.is_buy != d.is_buy and o.state != "terminal":
                if (d.is_buy and o.price <= d.price + 1e-9) or (not d.is_buy and o.price >= d.price - 1e-9):
                    return  # would self-cross with our own resting/pending order
        model = self.models[m]
        size = floor_to(min(d.qty, self.budget_size(m, d.is_buy, d.price, set())), model.min_qty)
        if size < model.min_qty - 1e-12:
            return
        o = LiveOrder(key=uuid.uuid4().hex, market=m, is_buy=d.is_buy, intent=d.intent, price=d.price,
                      qty=size, created=now, expires_at=math.floor(now) + s.order_ttl_s,
                      fee=self.fee_for(model, d.price))
        # Registered (and therefore counted as risk) before the request is sent.
        self.orders[o.key] = o
        self.last_place[(m, d.is_buy)] = now
        self.spawn(self._place(o))

    # ================================================================== tick
    async def step(self) -> None:
        now = self.clock()
        c, s = self.cfg, self.cfg.strategy
        if self.kill_path.exists():
            log.warning("kill file %s present: stopping", self.kill_path)
            self.stop.set()
            return
        if now >= c.trading_end_ts:
            log.warning("trading window over (%s): stopping", c.trading_end_utc)
            self.stop.set()
            return
        wid, _ = week_of(now)
        if self.week_id and wid != self.week_id and self.state_fresh(now):
            self._set_anchor(wid, now, "week rollover")
            self.week_id = wid

        for oid in [k for k, (ts, _) in self.pending_execs.items() if now - ts > 30]:
            self.pending_execs.pop(oid, None)
            self.resync_soon = True
        for o in self.orders.values():
            if o.state == "unknown" and now > o.expires_at + 1:
                o.mark_terminal(now)
            elif o.state in ("open", "cancel_pending") and now > o.expires_at + 5:
                o.state = "suspect"
            if o.state == "suspect" and o.id and now - o.last_verify >= 3:
                o.last_verify = now
                self.spawn(self._verify(o))

        for m, model in self.models.items():
            ob, oa = self.own_levels(m)
            j = model.observe(now, ob, oa)
            if j is not None:
                log.warning("%s jump %.3f: pulled for %.0fs", m, j, s.jump_cooldown_s)
                self.recent_jumps.append((now, m, j))
            was = model.eligible
            if model.update_eligibility(now) != was:
                log.info("%s %s quoting: %s", model.title,
                         "STARTS" if model.eligible else "STOPS (reduce-only)", model.eligible_reason)
                self.journal.write("eligibility", market=m, eligible=model.eligible,
                                   reason=model.eligible_reason)
        self.recent_jumps = [x for x in self.recent_jumps if now - x[0] <= s.event_jump_window_s]
        if self.recent_jumps and (max(x[2] for x in self.recent_jumps) >= s.event_jump_threshold
                                  or len({x[1] for x in self.recent_jumps}) >= s.event_jump_markets):
            if now >= self.event_pause_until:
                log.warning("event-wide move detected: pulling all quotes for %.0fs", s.event_cooldown_s)
            self.event_pause_until = now + s.event_cooldown_s
            self.recent_jumps.clear()

        ests = {m: md.sibling_estimate() for m, md in self.models.items()}
        sib: dict[str, float] = {}
        if all(v is not None for v in ests.values()):
            tot = sum(ests.values())
            if tot > 0:
                sib = {m: v * s.expected_eliminations / tot for m, v in ests.items()}
        for m, model in self.models.items():
            model.fair_value(now, sib.get(m))
            model.evaluate_markouts(now)

        ok, reason = self.health(now)
        if not ok:
            self.pull_all(reason, now)
            return
        self.last_pull_reason = ""
        self._session_check(now)
        ro = self.reduce_only(now)
        for m in self.models:
            self.reconcile_market(m, self.desired_quotes(m, now, ro), now)
        if now - self.last_status_log >= 30:
            self.last_status_log = now
            self.log_status(now)

    def health(self, now: float) -> tuple[bool, str]:
        if self.halted:
            return False, f"halted: {self.halted}"
        if not self.private_connected:
            return False, "private stream down"
        if not self.private_snapshot and now - self.private_connected_at < 3:
            return False, "private stream warming up"
        if not self.state_fresh(now):
            return False, "account state stale"
        if self.inflows_stale:
            return False, "deposits need re-reading"
        return True, ""

    def _session_check(self, now: float) -> None:
        eq = self.cash
        for m, q in self.positions.items():
            model = self.models.get(m)
            ref = model.fv if model is not None and model.fv is not None else None
            eq += q * ref if ref is not None else min(q, 0.0)
        if self.session_start_equity is None:
            self.session_start_equity = eq
            return
        dd = self.session_start_equity - eq
        if not self.soft_stopped and dd >= self.cfg.risk.session_soft_stop:
            self.soft_stopped = True
            log.warning("session drawdown %.2f >= soft stop: reduce-only from now on", dd)
            self.journal.write("soft_stop", drawdown=dd)

    def log_status(self, now: float) -> None:
        f, lim = self.floor(), self.limit()
        live = [o for o in self.orders.values() if o.state != "terminal"]
        log.info("STATUS cash %.2f floor %.2f limit %.2f headroom %.2f orders %d pos %s",
                 self.cash, f, lim, f - lim, len(live),
                 {m.rsplit("-", 1)[-1]: round(q, 2) for m, q in self.positions.items() if m in self.models})
        for m, md in self.models.items():
            if md.fv is not None:
                log.info("  %-14s fv %.3f bbo %s/%s tox %.2f act $%.2f/h %s%s", md.title[:14], md.fv,
                         md.ext_bid, md.ext_ask, md.tox_mult(), md.activity_rate(now),
                         "QUOTING" if md.eligible else f"idle({md.eligible_reason})",
                         f" PAUSED({md.pause_reason})" if now < md.pause_until else "")

    # ================================================================== loops
    async def _sleep(self, secs: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass

    async def tick_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.step()
            except Halt as e:
                self.do_halt(str(e))
            except Exception:
                log.exception("tick error")
                self.pull_all("tick error", self.clock())
            await self._sleep(self.cfg.strategy.tick_s)

    async def resync_loop(self) -> None:
        last = 0.0
        while not self.stop.is_set():
            now = self.clock()
            if now - last >= self.cfg.risk.resync_interval_s or (self.resync_soon and now - last >= 0.5):
                self.resync_soon = False
                last = now
                try:
                    await self.resync()
                except Exception as e:
                    log.warning("resync failed: %s", e)
            await self._sleep(0.25)

    async def inflow_loop(self) -> None:
        while not self.stop.is_set():
            now = self.clock()
            if self.inflows_stale or now - self.last_inflow_refresh >= self.cfg.risk.inflow_refresh_s:
                try:
                    await self.refresh_inflows()
                except Exception as e:
                    log.warning("inflow refresh failed: %s", e)
                    await self._sleep(5)
            await self._sleep(1)

    async def book_loop(self) -> None:
        """REST fallback for markets without live WS data. Gateway books are CDN-cached for up
        to 30s, so they usually arrive already stale and only keep the model warm; quoting
        resumes once live data is flowing again."""
        while not self.stop.is_set():
            now = self.clock()
            stale = [(m.book_ts, slug) for slug, m in self.models.items() if not self.book_fresh(m, now)]
            if stale:
                _, slug = min(stale)
                try:
                    self._apply_book(await self.client.get_book(slug))
                except Exception as e:
                    log.debug("book poll %s: %s", slug, e)
            await self._sleep(2.0)

    async def event_loop(self) -> None:
        while not self.stop.is_set():
            await self._sleep(300)
            if self.stop.is_set():
                return
            try:
                await self.load_markets()
            except Exception as e:
                log.warning("event refresh failed: %s", e)

    async def shutdown(self) -> None:
        self.stop.set()
        for i in range(6):
            try:
                await self.client.cancel_all(self.slugs)
                break
            except Exception as e:
                log.error("shutdown cancel_all failed (%d): %s", i, e)
                await asyncio.sleep(1 + i)
        await asyncio.sleep(1.0)
        try:
            left = [o for o in await self.client.open_orders() if o.get("marketSlug") in self.models]
            for o in left:
                try:
                    await self.client.cancel_order(o["id"], o["marketSlug"])
                except Exception as e:
                    log.error("could not cancel %s: %s", o.get("id"), e)
            if left:
                log.error("%d orders were still open at shutdown; cancel requested individually", len(left))
            else:
                log.info("shutdown: no open orders remain")
        except Exception as e:
            log.error("shutdown verification failed: %s (GTD orders expire within %ss)", e,
                      self.cfg.strategy.order_ttl_s)
        for t in list(self._tasks):
            t.cancel()
        try:
            await self.resync()
            log.info("final: cash %.2f floor %.2f limit %.2f", self.cash, self.floor(), self.limit())
        except Exception:
            pass
        self.journal.write("shutdown", halted=self.halted)
