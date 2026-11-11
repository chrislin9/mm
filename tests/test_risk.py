import math
import random

import pytest

from pmmm import risk
from pmmm.risk import Exposure, account_floor, market_floor, max_order_size, position_floor


def E(buy, px, qty, m="m", fee=0.0):
    return Exposure(m, buy, px, qty, fee)


def test_long_position_floor_counts_premium_as_lost():
    # Bought 10 @ 0.40: cash already down $4; worst case the contracts are worth 0.
    assert position_floor(96.0, {"m": 10}) == pytest.approx(96.0)
    # Short 10 @ 0.40: cash up $4, worst case owe $10.
    assert position_floor(104.0, {"m": -10}) == pytest.approx(94.0)


def test_resting_orders_worst_case():
    # bid 10 @ 0.45 -> lose 4.5 in NO; ask 10 @ 0.55 -> lose 4.5 in YES.
    assert market_floor(0, [E(True, 0.45, 10)]) == pytest.approx(-4.5)
    assert market_floor(0, [E(False, 0.55, 10)]) == pytest.approx(-4.5)
    # Both sides: only one branch can happen, worst is still -4.5 (not -9).
    assert market_floor(0, [E(True, 0.45, 10), E(False, 0.55, 10)]) == pytest.approx(-4.5)


def test_selling_down_a_long_is_risk_free():
    yes, no = risk.market_branches(10, [E(False, 0.5, 10)])
    assert (yes, no) == pytest.approx((5.0, 0.0))
    assert market_floor(10, [E(False, 0.5, 10)]) == pytest.approx(0.0)


def test_fee_allowance():
    assert market_floor(0, [E(True, 0.5, 10, fee=0.02)]) == pytest.approx(-5.2)


def test_account_floor_sums_markets():
    f = account_floor(50.0, {"a": 4, "b": -2}, [E(True, 0.3, 5, "a"), E(False, 0.9, 10, "c")])
    # a: min(4, -1.5) = -1.5 ; b: -2 ; c: -(0.1*10) = -1
    assert f == pytest.approx(50 - 1.5 - 2 - 1)


def test_max_order_size_respects_target_exactly():
    rng = random.Random(1)
    for _ in range(2000):
        q = rng.choice([0.0, rng.uniform(-20, 20)])
        exps = [E(rng.random() < 0.5, round(rng.uniform(0.01, 0.99), 2), rng.uniform(0, 10)) for _ in range(rng.randint(0, 3))]
        cur = market_floor(q, exps)
        target = cur - rng.uniform(-1, 5)
        buy = rng.random() < 0.5
        px = round(rng.uniform(0.01, 0.99), 2)
        s = max_order_size(q, exps, buy, px, 0.0, target)
        if math.isinf(s):
            continue
        new = market_floor(q, exps + [E(buy, px, s)])
        assert new >= min(target, cur) - 1e-9
        # And it is tight: a bit more would breach (when s > 0 and binding).
        if s > 0:
            more = market_floor(q, exps + [E(buy, px, s + 0.01)])
            assert more < min(target, cur) + 1e-6 or more >= cur - 1e-12


def test_max_order_size_allows_reducing_when_over_limit():
    # Long 10, already "over" budget (target above current floor): still may sell down.
    s = max_order_size(10, [], False, 0.5, 0.0, 5.0)
    assert s == pytest.approx(20.0)  # selling up to 20 never lowers the floor below 0 (=current)
    # ...but may not add to the long.
    assert max_order_size(10, [], True, 0.5, 0.0, 5.0) == 0.0
