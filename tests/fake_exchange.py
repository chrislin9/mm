"""In-memory adversarial exchange used to test the engine's loss guarantee.

The adversary knows the true outcome and preferentially fills every quote that is wrong.
The exchange also misbehaves: ambiguous creates (order created but client sees a timeout),
5xx, dropped/duplicated websocket messages, spoof orders, and price jumps.
"""
from __future__ import annotations

import random

from pmmm import risk
from pmmm.api import Ambiguous, BadRequest, NotFound, ServerError
from pmmm.strategy import intent_is_buy
from pmmm.util import fnum, parse_ts

T0 = parse_ts("2026-11-10T04:00:00Z")


class FakeExchange:
    def __init__(self, rng: random.Random, n_markets: int = 5, cash: float = 100.0, *,
                 p_ambiguous: float = 0.03, p_5xx: float = 0.02, p_drop_ws: float = 0.15,
                 p_dup_ws: float = 0.05, informed_rate: float = 0.35, noise_rate: float = 0.03,
                 spoof_rate: float = 0.05, jump_rate: float = 0.01, initial_positions=None,
                 invert_sign: bool = False, wide: set[int] = frozenset(),
                 lifetime_notional: dict[int, float] | None = None):
        self.rng = rng
        self.t = T0
        self.cash = cash
        self.slugs = [f"rtc-dwts-test-{i}" for i in range(n_markets)]
        elim = rng.randrange(n_markets)
        self.truth = {s: int(i == elim) for i, s in enumerate(self.slugs)}
        # Market belief starts uninformative-ish and drifts toward the truth.
        raw = [rng.uniform(0.05, 0.6) for _ in self.slugs]
        tot = sum(raw)
        self.mid = {s: min(0.95, max(0.03, r / tot)) for s, r in zip(self.slugs, raw)}
        self.pos: dict[str, float] = dict(initial_positions or {})
        self.orders: dict[str, dict] = {}
        self.dead: dict[str, dict] = {}
        self.spoof: dict[str, tuple[bool, float, float] | None] = {s: None for s in self.slugs}
        self.shares = {s: 0.0 for s in self.slugs}
        self.next_id = 1
        self.engine = None
        self.clock_skew_s = 0.0
        self.p_ambiguous, self.p_5xx = p_ambiguous, p_5xx
        self.p_drop_ws, self.p_dup_ws = p_drop_ws, p_dup_ws
        self.informed_rate, self.noise_rate = informed_rate, noise_rate
        self.spoof_rate, self.jump_rate = spoof_rate, jump_rate
        self.fills = 0
        self.invert_sign = invert_sign
        # Markets in `wide` have a 4c spread (levels start 2 ticks from mid); others 2c.
        self.wide = {self.slugs[i] for i in wide}
        ln = lifetime_notional or {}
        self.notional = {s: float(ln.get(i, 0.0)) for i, s in enumerate(self.slugs)}
        self.intents: dict[str, list[str]] = {s: [] for s in self.slugs}
        self.bad_fills = 0

    # ------------------------------------------------------------ truth helpers
    def true_floor(self) -> float:
        exps = [risk.Exposure(o["m"], o["buy"], o["px"], o["leaves"]) for o in self.orders.values()]
        return risk.account_floor(self.cash, self.pos, exps)

    def settle_value(self) -> float:
        return self.cash + sum(q * self.truth.get(m, 0) for m, q in self.pos.items())

    # ------------------------------------------------------------ ws emission
    def _emit(self, ex: dict) -> None:
        if self.engine is None or self.rng.random() < self.p_drop_ws:
            return
        msg = {"orderSubscriptionUpdate": {"execution": ex}}
        self.engine.on_private_message(msg)
        if self.rng.random() < self.p_dup_ws:
            self.engine.on_private_message(msg)

    def _exec(self, oid: str, o: dict, typ: str, last: float = 0.0, state: str = "ORDER_STATE_NEW") -> dict:
        return {"id": f"x{oid}-{typ}-{o['leaves']:.4f}", "type": typ, "lastShares": f"{last:.4f}",
                "order": {"id": oid, "marketSlug": o["m"], "leavesQuantity": o["leaves"], "state": state,
                          "intent": o["intent"]}}

    # ------------------------------------------------------------ book
    def _ext_levels(self, s: str):
        mid = self.mid[s]
        bids, asks = [], []
        k0 = 2 if s in self.wide else 1
        for k in range(k0, k0 + 5):
            bp, ap = round(mid - 0.01 * k, 2), round(mid + 0.01 * k, 2)
            if bp >= 0.01:
                bids.append([bp, self.rng.choice([0.5, 3, 10, 25])])
            if ap <= 0.99:
                asks.append([ap, self.rng.choice([0.5, 3, 10, 25])])
        sp = self.spoof[s]
        if sp:
            buy, px, qty = sp
            (bids if buy else asks).append([px, qty])
        return bids, asks

    def _merged(self, s: str):
        bids, asks = self._ext_levels(s)
        agg_b, agg_a = {}, {}
        for p, q in bids:
            agg_b[p] = agg_b.get(p, 0) + q
        for p, q in asks:
            agg_a[p] = agg_a.get(p, 0) + q
        for o in self.orders.values():
            if o["m"] == s:
                d = agg_b if o["buy"] else agg_a
                d[o["px"]] = d.get(o["px"], 0) + o["leaves"]
        return (sorted(agg_b.items(), key=lambda x: -x[0]), sorted(agg_a.items(), key=lambda x: x[0]))

    # ------------------------------------------------------------ client API
    async def get_event(self, slug):
        return {"slug": slug, "markets": [
            {"slug": s, "title": s[-6:], "active": True, "closed": False, "orderPriceMinTickSize": 0.01,
             "startDate": "2026-11-06T00:00:00Z",
             "minimumTradeQty": 0.01, "feeCoefficient": 0.095} for s in self.slugs]}

    async def get_book(self, slug):
        b, a = self._merged(slug)
        lv = lambda xs: [{"px": {"value": f"{p:.4f}"}, "qty": f"{q:.4f}"} for p, q in xs]
        return {"marketSlug": slug, "bids": lv(b), "offers": lv(a), "state": "MARKET_STATE_OPEN",
                "stats": {"sharesTraded": f"{self.shares[slug]:.2f}",
                          "notionalTraded": {"value": f"{self.notional[slug]:.4f}", "currency": "USD"}}}

    async def open_orders(self):
        if self.rng.random() < self.p_5xx:
            raise ServerError(503, "unavailable")
        return [{"id": i, "marketSlug": o["m"], "intent": o["intent"], "price": {"value": f"{o['px']:.2f}"},
                 "quantity": o["qty"], "leavesQuantity": o["leaves"], "state": "ORDER_STATE_NEW"}
                for i, o in self.orders.items()]

    async def get_order(self, oid):
        if oid in self.orders:
            return {"id": oid, "state": "ORDER_STATE_NEW", "leavesQuantity": self.orders[oid]["leaves"]}
        if oid in self.dead:
            return {"id": oid, "state": self.dead[oid]["state"], "leavesQuantity": 0}
        raise NotFound(404, "no such order")

    async def positions(self):
        sg = -1 if self.invert_sign else 1
        return {m: {"netPositionDecimal": f"{sg * q:.4f}"} for m, q in self.pos.items() if abs(q) > 1e-12}

    async def balances(self):
        return {"currency": "USD", "currentBalance": self.cash}

    async def activities(self, types, cursor=None, limit=100):
        return {"activities": [], "eof": True}

    async def create_order(self, body):
        if self.rng.random() < self.p_5xx:
            raise ServerError(502, "bad gateway")  # not created
        m = body["marketSlug"]
        px = float(body["price"]["value"])
        qty = float(body["quantity"])
        buy = intent_is_buy(body["intent"])
        assert body["participateDontInitiate"] is True
        assert body["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE"
        assert 0.01 <= px <= 0.99 and qty > 0
        self.intents[m].append(body["intent"])
        oid = f"o{self.next_id}"
        self.next_id += 1
        o = {"m": m, "buy": buy, "px": px, "qty": qty, "leaves": qty, "intent": body["intent"],
             "exp": parse_ts(body["goodTillTime"])}
        b, a = self._merged(m)
        # Post-only: reject if it would cross anything resting (including our own).
        if (buy and a and px >= a[0][0] - 1e-9) or (not buy and b and px <= b[0][0] + 1e-9):
            self.dead[oid] = {"state": "ORDER_STATE_REJECTED"}
            self._emit(self._exec(oid, o, "EXECUTION_TYPE_REJECTED", state="ORDER_STATE_REJECTED"))
            return {"id": oid}
        self.orders[oid] = o
        self._emit(self._exec(oid, o, "EXECUTION_TYPE_NEW"))
        if self.rng.random() < self.p_ambiguous:
            raise Ambiguous("read timeout")  # created, but the client doesn't know
        return {"id": oid}

    async def cancel_order(self, oid, slug):
        if oid not in self.orders:
            raise NotFound(404, "order not found")
        self._kill(oid, "EXECUTION_TYPE_CANCELED", "ORDER_STATE_CANCELED")
        return {}

    async def cancel_all(self, slugs):
        if self.rng.random() < self.p_5xx:
            raise ServerError(503, "unavailable")
        ids = [i for i, o in self.orders.items() if o["m"] in slugs]
        for i in ids:
            self._kill(i, "EXECUTION_TYPE_CANCELED", "ORDER_STATE_CANCELED")
        return {"canceledOrderIds": ids}

    def _kill(self, oid, typ, state):
        o = self.orders.pop(oid)
        self.dead[oid] = {"state": state}
        self._emit(self._exec(oid, o, typ, state=state))

    # ------------------------------------------------------------ simulation
    def advance(self, dt: float) -> None:
        self.t += dt
        rng = self.rng
        for s in self.slugs:
            target = self.truth[s]
            drift = 0.002 * (target - self.mid[s])
            shock = rng.gauss(0, 0.004)
            if rng.random() < self.jump_rate:
                shock += rng.choice([-1, 1]) * rng.uniform(0.04, 0.15)
            self.mid[s] = round(min(0.95, max(0.03, self.mid[s] + drift + shock)), 2)
            if rng.random() < self.spoof_rate:
                side = rng.random() < 0.5
                # A large order placed inside the spread to drag fair value, pulled later.
                self.spoof[s] = (side, self.mid[s], 200.0) if self.spoof[s] is None else None
        for oid, o in list(self.orders.items()):
            if o["exp"] <= self.t:
                self._kill(oid, "EXECUTION_TYPE_EXPIRED", "ORDER_STATE_EXPIRED")

    def adversary(self) -> None:
        rng = self.rng
        for oid, o in list(self.orders.items()):
            truth = self.truth[o["m"]]
            bad = (o["buy"] and truth == 0) or ((not o["buy"]) and truth == 1)
            if rng.random() < (self.informed_rate if bad else self.noise_rate):
                q = o["leaves"] if rng.random() < 0.6 else round(o["leaves"] * rng.uniform(0.1, 0.9), 2)
                q = max(0.01, min(q, o["leaves"]))
                self._fill(oid, q, bad)

    def _fill(self, oid: str, q: float, bad: bool) -> None:
        o = self.orders[oid]
        sign = 1 if o["buy"] else -1
        self.cash -= sign * o["px"] * q
        self.pos[o["m"]] = self.pos.get(o["m"], 0.0) + sign * q
        self.shares[o["m"]] += q
        self.notional[o["m"]] += q * o["px"]
        o["leaves"] = round(o["leaves"] - q, 6)
        self.fills += 1
        self.bad_fills += int(bad)
        if o["leaves"] <= 1e-9:
            self.orders.pop(oid)
            self.dead[oid] = {"state": "ORDER_STATE_FILLED"}
            self._emit(self._exec(oid, {**o, "leaves": 0.0}, "EXECUTION_TYPE_FILL", q, "ORDER_STATE_FILLED"))
        else:
            self._emit(self._exec(oid, o, "EXECUTION_TYPE_PARTIAL_FILL", q, "ORDER_STATE_PARTIALLY_FILLED"))
