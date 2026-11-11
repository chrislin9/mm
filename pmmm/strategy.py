"""Fair value, adverse-selection signals and quote construction.

Principles (moontowermeta / Jane Street style):
  * Being filled is information. Evaluate a quote in the worlds where it trades. We track
    markouts of our own fills and widen or stop when fills are followed by moves against us.
  * Fair value comes from robust, hard-to-spoof inputs: depth-filtered book (our own orders
    removed), traded prices, taker-flow direction, and the cross-market "sum of siblings"
    identity Σ P(eliminated_i) ≈ expected eliminations.
  * When the model may be stale (jumps, big prints, event-wide moves, approaching the live
    show) be fast and pull first. Re-enter later.
  * Inventory is managed with a reservation-price skew plus size shading, and inventory is
    measured in worst-case dollars, not contracts.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .config import StrategyConfig
from .util import ceil_to, floor_to

INTENT_BUY_LONG = "ORDER_INTENT_BUY_LONG"      # buy YES (open/increase long)
INTENT_SELL_LONG = "ORDER_INTENT_SELL_LONG"    # sell YES (reduce long)
INTENT_BUY_SHORT = "ORDER_INTENT_BUY_SHORT"    # sell YES (open/increase short)
INTENT_SELL_SHORT = "ORDER_INTENT_SELL_SHORT"  # buy YES (reduce short)
BUY_YES_INTENTS = {INTENT_BUY_LONG, INTENT_SELL_SHORT}


def intent_is_buy(intent: str | None, outcome_side: str | None = None, action: str | None = None) -> bool | None:
    if intent in BUY_YES_INTENTS:
        return True
    if intent in (INTENT_SELL_LONG, INTENT_BUY_SHORT):
        return False
    if outcome_side and action:
        yes = outcome_side == "OUTCOME_SIDE_YES"
        buy = action == "ORDER_ACTION_BUY"
        return yes == buy
    return None


@dataclass
class Quote:
    is_buy: bool
    price: float
    qty: float
    intent: str


def _decay(dt: float, halflife: float) -> float:
    if halflife <= 0:
        return 0.0
    return math.exp(-math.log(2) * max(0.0, dt) / halflife)


@dataclass
class MarketModel:
    slug: str
    title: str
    tick: float
    min_qty: float
    fee_coeff: float
    cfg: StrategyConfig
    # book
    bids: list = field(default_factory=list)     # [(px, qty)] best first
    offers: list = field(default_factory=list)
    book_ts: float = 0.0
    ws_live: bool = False
    state: str = "UNKNOWN"
    shares_traded: float | None = None
    # derived each observe()
    ext_bid: float | None = None
    ext_ask: float | None = None
    f_bid: float | None = None
    f_ask: float | None = None
    mid: float | None = None
    slow_mid: float | None = None
    last_obs_ts: float = 0.0
    vol: float = 0.0
    fv: float | None = None
    # trades / flow
    flow: float = 0.0
    flow_ts: float = 0.0
    trade_px: float | None = None
    trade_ts: float = 0.0
    # defences
    pause_until: float = 0.0
    pause_reason: str = ""
    side_pause_until: dict = field(default_factory=lambda: {True: 0.0, False: 0.0})
    side_fade_until: dict = field(default_factory=lambda: {True: 0.0, False: 0.0})
    markout: float = 0.0
    markout_n: int = 0
    pending_markouts: deque = field(default_factory=deque)
    fill_times: deque = field(default_factory=deque)
    last_fill_ts: float = 0.0
    # dynamic market selection
    listed_ts: float | None = None          # when the market opened (for the lifetime prior)
    notional_total: float | None = None     # exchange's cumulative $ traded
    prior_rate: float | None = None         # lifetime $/hour, frozen at first sight
    activity_start: float | None = None     # when we started observing live volume
    stat_volume: deque = field(default_factory=deque)   # (ts, $) from cumulative stats deltas
    trade_volume: deque = field(default_factory=deque)  # (ts, $) from the trade stream
    own_volume: deque = field(default_factory=deque)    # (ts, $) our own fills
    eligible: bool = False
    eligible_reason: str = "warming up"
    candidate_since: float | None = None

    # ------------------------------------------------------------------ inputs
    def update_book(self, bids, offers, state: str | None, shares_traded: float | None, now: float,
                    notional_traded: float | None = None) -> None:
        if now < self.book_ts:
            return  # never let an older (e.g. CDN-cached) snapshot overwrite a newer book
        self.bids = sorted(((p, q) for p, q in bids if q > 0), key=lambda x: -x[0])
        self.offers = sorted(((p, q) for p, q in offers if q > 0), key=lambda x: x[0])
        if state:
            self.state = state
        self.book_ts = now
        shares_delta = None
        if shares_traded is not None:
            if self.shares_traded is not None:
                shares_delta = shares_traded - self.shares_traded
                if shares_delta >= self.cfg.big_trade_contracts:
                    self.pause(now, self.cfg.jump_cooldown_s, f"volume burst {shares_delta:.0f}")
            self.shares_traded = shares_traded
        self._record_volume(now, notional_traded, shares_delta)

    def _record_volume(self, now: float, notional: float | None, shares_delta: float | None) -> None:
        if self.activity_start is None:
            self.activity_start = now
        if notional is not None:
            if self.notional_total is None:
                if self.listed_ts is not None and self.prior_rate is None:
                    self.prior_rate = notional / max(1.0, (now - self.listed_ts) / 3600)
            elif notional > self.notional_total:
                self.stat_volume.append((now, notional - self.notional_total))
            self.notional_total = notional
        elif shares_delta is not None and shares_delta > 0:
            px = self.mid if self.mid is not None else (self.trade_px if self.trade_px is not None else 0.5)
            self.stat_volume.append((now, shares_delta * px))

    def on_trade(self, now: float, px: float, qty: float, taker_is_buy: bool | None) -> None:
        c = self.cfg
        a = _decay(now - self.trade_ts, c.trade_halflife_s) if self.trade_px is not None else 0.0
        self.trade_px = px if self.trade_px is None else a * self.trade_px + (1 - a) * px
        self.trade_ts = now
        if taker_is_buy is not None:
            self.flow = self.flow * _decay(now - self.flow_ts, c.flow_halflife_s) + (qty if taker_is_buy else -qty)
            self.flow_ts = now
        if qty >= c.big_trade_contracts:
            self.pause(now, c.jump_cooldown_s, f"big trade {qty:.0f}")
        self.trade_volume.append((now, px * qty))

    def on_own_fill(self, now: float, is_buy: bool, px: float, qty: float) -> None:
        c = self.cfg
        # Our own fills show up in the exchange's volume; they must not make a market look
        # "active" (otherwise being picked off would attract us to the market doing it).
        self.own_volume.append((now, px * qty))
        self.side_pause_until[is_buy] = max(self.side_pause_until[is_buy], now + c.fill_cooldown_s)
        self.side_fade_until[is_buy] = max(self.side_fade_until[is_buy], now + c.fade_s)
        self.pending_markouts.append((now, is_buy, px))
        self.fill_times.append(now)
        self.last_fill_ts = now
        while self.fill_times and now - self.fill_times[0] > 60:
            self.fill_times.popleft()
        if len(self.fill_times) > c.max_fills_per_min:
            self.pause(now, c.jump_cooldown_s * 2, "fill rate")

    def on_unexplained_fill(self, now: float) -> None:
        for side in (True, False):
            self.side_pause_until[side] = max(self.side_pause_until[side], now + self.cfg.fill_cooldown_s)
        self.last_fill_ts = now

    def pause(self, now: float, secs: float, reason: str) -> None:
        if now + secs > self.pause_until:
            self.pause_until = now + secs
            self.pause_reason = reason

    # ------------------------------------------------------------------ derived
    @staticmethod
    def _levels_ex_own(levels, own: dict[float, float]):
        for px, qty in levels:
            ext = qty - own.get(round(px, 6), 0.0)
            if ext > 1e-9:
                yield px, ext

    def observe(self, now: float, own_bids: dict[float, float], own_asks: dict[float, float]) -> float | None:
        """Recompute book-derived values. Returns the jump size if a jump was detected."""
        c = self.cfg
        ext_b = list(self._levels_ex_own(self.bids, own_bids))
        ext_a = list(self._levels_ex_own(self.offers, own_asks))
        self.ext_bid = ext_b[0][0] if ext_b else None
        self.ext_ask = ext_a[0][0] if ext_a else None
        self.f_bid = self._depth_px(ext_b, c.depth_filter_contracts)
        self.f_ask = self._depth_px(ext_a, c.depth_filter_contracts)
        mid = None
        crossed = self.ext_bid is not None and self.ext_ask is not None and self.ext_bid >= self.ext_ask - 1e-9
        if crossed:
            # A crossed external book means our book state is wrong (or the venue is in an
            # auction/odd state). Either way, do not trust it.
            self.f_bid = self.f_ask = None
        if self.f_bid is not None and self.f_ask is not None and self.f_ask - self.f_bid <= c.max_book_spread + 1e-9:
            mid = (self.f_bid + self.f_ask) / 2
        jump = None
        if mid is not None:
            if self.slow_mid is None:
                self.slow_mid = mid
            elif abs(mid - self.slow_mid) >= c.jump_threshold - 1e-9:
                jump = abs(mid - self.slow_mid)
                self.pause(now, c.jump_cooldown_s, f"jump {self.slow_mid:.3f}->{mid:.3f}")
                self.slow_mid = mid
            else:
                a = 1 - _decay(now - self.last_obs_ts, c.slow_mid_halflife_s)
                self.slow_mid += a * (mid - self.slow_mid)
            if self.mid is not None:
                self.vol = 0.9 * self.vol + 0.1 * abs(mid - self.mid)
        self.mid = mid
        self.last_obs_ts = now
        return jump

    @staticmethod
    def _depth_px(levels, depth: float) -> float | None:
        cum = 0.0
        for px, q in levels:
            cum += q
            if cum >= depth - 1e-9:
                return px
        return None

    def sibling_estimate(self) -> float | None:
        """Probability estimate usable in Σ P = E even when one side of the book is empty."""
        if self.mid is not None:
            return self.mid
        b = self.f_bid if self.f_bid is not None else self.ext_bid
        a = self.f_ask if self.f_ask is not None else self.ext_ask
        if a is None and b is None:
            return None
        return ((b or 0.0) + (a if a is not None else 1.0)) / 2

    def fair_value(self, now: float, sibling_fv: float | None) -> float | None:
        c = self.cfg
        if self.mid is None:
            self.fv = None
            return None
        w_s = c.sibling_weight if sibling_fv is not None else 0.0
        w_t = c.trade_weight if (self.trade_px is not None and now - self.trade_ts < 4 * c.trade_halflife_s) else 0.0
        fv = (1 - w_s - w_t) * self.mid + w_s * (sibling_fv or 0.0) + w_t * (self.trade_px or 0.0)
        flow = self.flow * _decay(now - self.flow_ts, c.flow_halflife_s)
        fv += c.flow_max_shift * math.tanh(flow / max(c.flow_scale_contracts, 1e-9))
        # Fair value never leaves the depth-filtered market.
        fv = min(max(fv, self.f_bid), self.f_ask)
        self.fv = min(max(fv, self.tick), 1 - self.tick)
        return self.fv

    def evaluate_markouts(self, now: float) -> None:
        c = self.cfg
        while self.pending_markouts and now - self.pending_markouts[0][0] >= c.markout_horizon_s:
            ts, is_buy, px = self.pending_markouts.popleft()
            ref = self.fv if self.fv is not None else self.mid
            if ref is None:
                continue
            mo = (ref - px) if is_buy else (px - ref)
            self.markout = mo if self.markout_n == 0 else (1 - c.markout_alpha) * self.markout + c.markout_alpha * mo
            self.markout_n += 1
            if self.markout_n >= 3 and self.markout <= c.tox_pause_markout:
                self.pause(now, c.tox_pause_s, f"toxic markouts {self.markout:+.3f}")

    # ------------------------------------------------------------------ market selection
    def activity_rate(self, now: float) -> float:
        """Estimated $/hour traded by others: lifetime prior blended with a live rolling window.

            rate = (prior_rate * W + live_$) / (W + live_hours)

        so at startup it equals the lifetime average and then adapts to what we observe.
        Live volume is max(stats-derived, trade-stream) minus our own fills.
        """
        c = self.cfg
        lookback = c.activity_lookback_h * 3600
        for dq in (self.stat_volume, self.trade_volume, self.own_volume):
            while dq and now - dq[0][0] > lookback:
                dq.popleft()
        live_h = min(max(0.0, now - (self.activity_start or now)), lookback) / 3600
        live = max(sum(v for _, v in self.stat_volume), sum(v for _, v in self.trade_volume))
        live = max(0.0, live - sum(v for _, v in self.own_volume))
        w = c.activity_prior_weight_h
        return ((self.prior_rate or 0.0) * w + live) / max(w + live_h, 1e-9)

    def update_eligibility(self, now: float) -> bool:
        """Quote only where we can sit inside a real spread AND others actually trade.

        Dead or one-tick markets mostly fill us when someone knows more than we do.
        Entering needs both conditions to hold for `eligibility_dwell_s`; leaving is
        immediate, and uses looser thresholds (hysteresis) so we don't flap.
        """
        c = self.cfg
        if not c.dynamic_market_selection:
            self.eligible, self.eligible_reason = True, "selection off"
            return True
        spread = (self.f_ask - self.f_bid) if (self.f_bid is not None and self.f_ask is not None) else None
        rate = self.activity_rate(now)
        sp = "no two-sided depth" if spread is None else f"spread {spread:.2f}"
        if self.eligible:
            if spread is None or spread < c.exit_quote_spread - 1e-9:
                self.eligible, self.eligible_reason = False, sp
            elif rate < c.min_notional_per_hour * c.activity_exit_ratio:
                self.eligible, self.eligible_reason = False, f"activity ${rate:.2f}/h"
            if not self.eligible:
                self.candidate_since = None
            return self.eligible
        if spread is None or spread < c.min_quote_spread - 1e-9:
            self.candidate_since, self.eligible_reason = None, sp
        elif rate < c.min_notional_per_hour:
            self.candidate_since, self.eligible_reason = None, f"activity ${rate:.2f}/h"
        else:
            if self.candidate_since is None:
                self.candidate_since = now
            if now - self.candidate_since >= c.eligibility_dwell_s:
                self.eligible, self.eligible_reason = True, f"{sp}, ${rate:.2f}/h"
            else:
                self.eligible_reason = "confirming"
        return self.eligible

    def tox_mult(self) -> float:
        c = self.cfg
        if self.markout_n == 0:
            return 1.0
        return min(c.tox_max_mult, 1.0 + c.tox_k * max(0.0, -self.markout) / self.tick)


@dataclass
class QuoteInputs:
    fv: float
    tick: float
    min_qty: float
    ext_bid: float | None
    ext_ask: float | None
    q: float
    vol: float = 0.0
    tox_mult: float = 1.0
    time_mult: float = 1.0
    bid_fade_ticks: int = 0
    ask_fade_ticks: int = 0
    bid_allowed: bool = True
    ask_allowed: bool = True
    reduce_only: bool = False
    max_order_risk: float = 1.0
    max_market_risk: float = 2.5


def inventory_ratio(q: float, fv: float, max_market_risk: float) -> float:
    """Signed inventory in worst-case dollars relative to the per-market limit."""
    if q > 0:
        return q * fv / max_market_risk
    if q < 0:
        return q * (1 - fv) / max_market_risk
    return 0.0


def compute_quotes(inp: QuoteInputs, c: StrategyConfig) -> tuple[Quote | None, Quote | None]:
    t = inp.tick
    x = inventory_ratio(inp.q, inp.fv, inp.max_market_risk)
    xs = max(-1.0, min(1.0, x))
    hs = max(c.min_half_spread, c.base_half_spread * inp.tox_mult * inp.time_mult + c.vol_mult * inp.vol)
    r = inp.fv - c.inventory_skew * xs * hs

    bid_px = floor_to(r - hs - inp.bid_fade_ticks * t, t)
    ask_px = ceil_to(r + hs + inp.ask_fade_ticks * t, t)

    bid_reduces = inp.q < 0
    ask_reduces = inp.q > 0
    bid_px = min(bid_px, floor_to(inp.fv - (c.reduce_edge if bid_reduces else c.min_edge), t))
    ask_px = max(ask_px, ceil_to(inp.fv + (c.reduce_edge if ask_reduces else c.min_edge), t))

    # Never improve the external book by more than one tick (no reason to give edge away).
    if inp.ext_bid is not None:
        bid_px = min(bid_px, round(inp.ext_bid + t, 9))
    if inp.ext_ask is not None:
        ask_px = max(ask_px, round(inp.ext_ask - t, 9))
    # Post-only: never cross the external book.
    if inp.ext_ask is not None:
        bid_px = min(bid_px, round(inp.ext_ask - t, 9))
    if inp.ext_bid is not None:
        ask_px = max(ask_px, round(inp.ext_bid + t, 9))

    bid = ask = None
    lo, hi = max(c.min_price, 0.01), min(c.max_price, 0.99)

    if inp.bid_allowed and lo - 1e-9 <= bid_px <= hi + 1e-9:
        if bid_reduces:
            size, intent = abs(inp.q), INTENT_SELL_SHORT
        elif not inp.reduce_only and x < 1.0:
            size = inp.max_order_risk / bid_px * max(0.0, min(1.0, 1.0 - x))
            intent = INTENT_BUY_LONG
        else:
            size, intent = 0.0, INTENT_BUY_LONG
        size = floor_to(size, inp.min_qty)
        if size >= inp.min_qty - 1e-12:
            bid = Quote(True, bid_px, size, intent)

    if inp.ask_allowed and lo - 1e-9 <= ask_px <= hi + 1e-9:
        if ask_reduces:
            size, intent = inp.q, INTENT_SELL_LONG
        elif not inp.reduce_only and x > -1.0:
            size = inp.max_order_risk / (1 - ask_px) * max(0.0, min(1.0, 1.0 + x))
            intent = INTENT_BUY_SHORT
        else:
            size, intent = 0.0, INTENT_BUY_SHORT
        size = floor_to(size, inp.min_qty)
        if size >= inp.min_qty - 1e-12:
            ask = Quote(False, ask_px, size, intent)

    if bid and ask and bid.price >= ask.price - 1e-9:
        # Can only happen with extreme skew/edge settings; keep the inventory-reducing side.
        if inp.q > 0:
            bid = None
        elif inp.q < 0:
            ask = None
        else:
            bid = ask = None
    return bid, ask
