"""Worst-case ("floor") accounting. This module is the loss guarantee.

Model
-----
Each market settles YES ($1) or NO ($0). With signed net position q (long YES > 0) the
settlement cash flow is q in YES and 0 in NO. Cash already reflects every past trade.

A resting order is assumed to be filled by a perfectly informed adversary exactly when the
fill hurts us:
  * a sell of s at price a hurts in YES:  +a*s now, -s at settlement -> -(1-a)*s
  * a buy  of s at price b hurts in NO:   -b*s now, 0 at settlement  -> -b*s
(plus an optional taker-fee allowance per contract).

    floor_m  = min( q_m - Σ_sells (1-a+f)*s ,  -Σ_buys (b+f)*s )
    floor    = cash + Σ_m floor_m

Markets are treated as independent (every combination of outcomes is allowed), which can
only over-state risk for a mutually exclusive event. The engine guarantees, before every
order, that `floor >= limit` where

    limit = week_anchor + external_inflows - weekly_loss_limit + safety_buffer

and week_anchor is the position floor (no orders) at the start of the week. Hence the
realised loss for the week cannot exceed weekly_loss_limit - safety_buffer no matter what
the market does to us, provided the exchange honours limit prices.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Exposure:
    market: str
    is_buy: bool          # buys YES (BUY_LONG / SELL_SHORT)
    price: float          # YES price
    qty: float            # contracts
    fee: float = 0.0      # worst-case fee per contract


def fee_per_contract(coeff: float, price: float) -> float:
    return max(0.0, coeff) * price * (1.0 - price)


def market_branches(q: float, exposures: Iterable[Exposure]) -> tuple[float, float]:
    yes, no = q, 0.0
    for e in exposures:
        if e.qty <= 0:
            continue
        if e.is_buy:
            no -= (e.price + e.fee) * e.qty
        else:
            yes -= (1.0 - e.price + e.fee) * e.qty
    return yes, no


def market_floor(q: float, exposures: Iterable[Exposure]) -> float:
    return min(market_branches(q, exposures))


def group(exposures: Iterable[Exposure]) -> dict[str, list[Exposure]]:
    g: dict[str, list[Exposure]] = defaultdict(list)
    for e in exposures:
        g[e.market].append(e)
    return g


def account_floor(cash: float, positions: dict[str, float], exposures: Iterable[Exposure]) -> float:
    g = group(exposures)
    total = cash
    for m in set(positions) | set(g):
        total += market_floor(positions.get(m, 0.0), g.get(m, ()))
    return total


def position_floor(cash: float, positions: dict[str, float]) -> float:
    return cash + sum(min(q, 0.0) for q in positions.values())


def max_order_size(q: float, market_exposures: Iterable[Exposure], is_buy: bool, price: float,
                   fee: float, min_market_floor: float) -> float:
    """Largest size of a new order that keeps floor_m >= min(min_market_floor, current floor_m).

    If the market is already below the target, only orders that do not lower floor_m at all
    are allowed (e.g. selling down a long position).
    """
    yes, no = market_branches(q, market_exposures)
    cur = min(yes, no)
    target = min(min_market_floor, cur)
    if is_buy:
        per = price + fee
        if yes < target - 1e-12:
            return 0.0
        room = no - target
    else:
        per = 1.0 - price + fee
        if no < target - 1e-12:
            return 0.0
        room = yes - target
    if per <= 0:
        return math.inf
    return max(0.0, room / per)
