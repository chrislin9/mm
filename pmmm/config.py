"""Configuration. Unknown keys are rejected so a typo can never silently disable a limit."""
from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import parse_ts


@dataclass
class RiskConfig:
    # Hard weekly cap on worst-case loss (USD). Week = Monday 00:00 America/New_York.
    weekly_loss_limit: float = 10.0
    # Held back from the cap to absorb rounding / fee rounding / unforeseen exchange quirks.
    safety_buffer: float = 0.50
    # Worst-case loss allowed inside any single market (positions + resting orders).
    max_market_risk: float = 4.00
    # Worst-case loss of any single new order.
    max_order_risk: float = 0.75
    # Terminal orders keep counting against risk until a full REST resync that started at
    # least this many seconds after the order died (lets the exchange ledger catch up).
    settle_lag_s: float = 2.0
    resync_interval_s: float = 2.0
    # No new orders if the last good resync is older than this; cancel everything at 2x.
    max_state_age_s: float = 8.0
    # Halt the process after this many consecutive order rejects.
    max_consecutive_rejects: int = 6
    # Mark-to-fair-value session drawdown that flips the bot into reduce-only mode.
    session_soft_stop: float = 5.0
    # Count a taker fee on every resting order in the worst case (paranoid; maker fee is 0).
    include_fee_allowance: bool = True
    # A cash increase larger than this with no fills/resolutions freezes trading until
    # deposits are re-read (so a deposit can never be mistaken for trading profit).
    unexplained_cash_jump: float = 0.25
    inflow_refresh_s: float = 300.0


@dataclass
class StrategyConfig:
    # --- fair value -------------------------------------------------------------------
    # Ignore book levels until this many contracts (excluding our own) have accumulated.
    # Defeats dust/penny-jump orders that try to drag our fair value.
    depth_filter_contracts: float = 5.0
    # Do not quote a market whose depth-filtered spread is wider than this.
    max_book_spread: float = 0.12
    # Weight on the cross-market "sum of siblings" consistency estimate.
    sibling_weight: float = 0.20
    # Expected number of eliminations this week (Σ P(eliminated_i) should equal this).
    expected_eliminations: float = 1.0
    # Weight on the EWMA of traded prices.
    trade_weight: float = 0.10
    trade_halflife_s: float = 120.0
    # Taker-flow signal: shifts fair value toward aggressive flow (in price units, max).
    flow_halflife_s: float = 60.0
    flow_scale_contracts: float = 50.0
    flow_max_shift: float = 0.01
    slow_mid_halflife_s: float = 60.0
    # --- spread ---------------------------------------------------------------------
    min_half_spread: float = 0.01
    base_half_spread: float = 0.015
    vol_mult: float = 1.0
    # Minimum distance from fair value for inventory-increasing quotes.
    min_edge: float = 0.01
    # Minimum distance from fair value for inventory-reducing quotes (0 = scratch is ok).
    reduce_edge: float = 0.0
    inventory_skew: float = 1.0
    min_price: float = 0.02
    max_price: float = 0.98
    # --- adverse selection defences -----------------------------------------------------
    jump_threshold: float = 0.04         # mid move vs slow mid that pulls the market
    jump_cooldown_s: float = 45.0
    event_jump_threshold: float = 0.08   # one move this big pulls the whole event
    event_jump_markets: int = 2          # or this many markets jumping within the window
    event_jump_window_s: float = 30.0
    event_cooldown_s: float = 180.0
    big_trade_contracts: float = 100.0   # a print this big pulls the market
    fill_cooldown_s: float = 20.0        # side is pulled after we are filled on it
    fade_s: float = 120.0                # ...then quoted this much wider for a while
    fade_ticks: int = 1
    max_fills_per_min: int = 4           # more than this -> pause the market
    markout_horizon_s: float = 30.0
    markout_alpha: float = 0.3
    tox_k: float = 0.5                   # spread multiplier per tick of adverse markout
    tox_max_mult: float = 3.0
    tox_pause_markout: float = -0.02     # average markout worse than this -> pause market
    tox_pause_s: float = 600.0
    # --- dynamic market selection --------------------------------------------------------
    # Quote a market only while (a) its depth-filtered spread leaves room to sit inside it with
    # edge and (b) other people actually trade it. Ineligible markets get reduce-only quotes
    # if we hold inventory there, otherwise nothing.
    dynamic_market_selection: bool = True
    min_quote_spread: float = 0.03       # enter at >= 3 ticks of filtered spread...
    exit_quote_spread: float = 0.02      # ...leave below 2 ticks
    min_notional_per_hour: float = 1.0   # enter at >= $1/h traded by others...
    activity_exit_ratio: float = 0.7     # ...leave below 70% of that
    activity_lookback_h: float = 3.0     # live volume window
    activity_prior_weight_h: float = 2.0 # weight (in hours) of the lifetime-average prior
    eligibility_dwell_s: float = 30.0    # entry conditions must hold this long
    # --- order management ----------------------------------------------------------------
    order_ttl_s: int = 120               # GTD expiry = dead-man switch if we crash
    refresh_before_expiry_s: float = 40.0
    min_requote_interval_s: float = 2.0
    requote_size_tolerance: float = 0.35
    book_stale_s: float = 15.0
    tick_s: float = 0.5


@dataclass
class Config:
    event_slug: str = "dwts-s35-elim-2026-11-10"
    # Stop quoting (cancel everything and exit) at this instant. The elimination is
    # decided live on air; being in the book while the show airs is pure adverse selection.
    trading_end_utc: str = "2026-11-10T23:00:00Z"
    # Spreads widen linearly over this many hours before trading_end.
    widen_start_hours: float = 6.0
    widen_max_mult: float = 2.0
    # Reduce-only for this many minutes before trading_end.
    wind_down_minutes: float = 60.0
    # Optional explicit allowlist / blocklist of market slugs within the event.
    include_markets: list[str] = field(default_factory=list)
    exclude_markets: list[str] = field(default_factory=list)
    api_base: str = "https://api.polymarket.us"
    gateway_base: str = "https://gateway.polymarket.us"
    ws_base: str = "wss://api.polymarket.us"
    state_dir: str = "state"
    kill_file: str = "KILL"
    max_requests_per_s: float = 8.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    @property
    def trading_end_ts(self) -> float:
        return parse_ts(self.trading_end_utc)

    def validate(self) -> None:
        r, s = self.risk, self.strategy
        problems = []
        if not (0 < r.weekly_loss_limit <= 10.0):
            problems.append("risk.weekly_loss_limit must be in (0, 10]")
        if not (0 <= r.safety_buffer < r.weekly_loss_limit):
            problems.append("risk.safety_buffer must be in [0, weekly_loss_limit)")
        if r.max_order_risk <= 0 or r.max_market_risk <= 0:
            problems.append("risk.max_order_risk / max_market_risk must be > 0")
        if r.max_order_risk > r.max_market_risk:
            problems.append("risk.max_order_risk must be <= risk.max_market_risk")
        if s.min_half_spread < 0.005:
            problems.append("strategy.min_half_spread too small")
        if s.min_edge < 0:
            problems.append("strategy.min_edge must be >= 0")
        if not (0.01 <= s.min_price < s.max_price <= 0.99):
            problems.append("strategy.min_price/max_price out of range")
        if s.order_ttl_s < 15 or s.refresh_before_expiry_s >= s.order_ttl_s:
            problems.append("strategy.order_ttl_s / refresh_before_expiry_s inconsistent")
        if s.exit_quote_spread > s.min_quote_spread or not (0 < s.activity_exit_ratio <= 1):
            problems.append("market selection exit thresholds must be looser than entry thresholds")
        if s.activity_lookback_h <= 0 or s.activity_prior_weight_h < 0 or s.min_notional_per_hour < 0:
            problems.append("market selection activity settings out of range")
        if not (0 <= s.sibling_weight + s.trade_weight < 1):
            problems.append("sibling_weight + trade_weight must be < 1")
        try:
            self.trading_end_ts
        except Exception:
            problems.append("trading_end_utc is not a valid timestamp")
        if problems:
            raise ValueError("Invalid config:\n  " + "\n  ".join(problems))


def _build(cls, data: dict, where: str):
    names = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(names)
    if unknown:
        raise ValueError(f"Unknown config keys in [{where}]: {sorted(unknown)}")
    kwargs = {}
    for k, v in data.items():
        if k == "risk":
            kwargs[k] = _build(RiskConfig, v, "risk")
        elif k == "strategy":
            kwargs[k] = _build(StrategyConfig, v, "strategy")
        else:
            kwargs[k] = v
    return cls(**kwargs)


def load_config(path: str | None) -> Config:
    if path is None:
        cfg = Config()
    else:
        with open(Path(path), "rb") as f:
            cfg = _build(Config, tomllib.load(f), "root")
    cfg.validate()
    return cfg
