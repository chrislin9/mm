from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def _d(x: float) -> Decimal:
    # Round away float noise (0.46499999999) before snapping to a grid.
    return Decimal(f"{x:.9f}")


def floor_to(x: float, step: float) -> float:
    s = _d(step)
    return float((_d(x) / s).to_integral_value(rounding=ROUND_FLOOR) * s)


def ceil_to(x: float, step: float) -> float:
    s = _d(step)
    return float((_d(x) / s).to_integral_value(rounding=ROUND_CEILING) * s)


def decimals_of(step: float) -> int:
    exp = _d(step).normalize().as_tuple().exponent
    return max(0, -int(exp))


def fmt_step(x: float, step: float) -> str:
    return f"{x:.{decimals_of(step)}f}"


_FRAC = re.compile(r"\.(\d+)")


def parse_ts(s: str) -> float:
    """RFC3339 with optional nanoseconds -> epoch seconds."""
    s = s.strip().replace("Z", "+00:00")
    m = _FRAC.search(s)
    if m and len(m.group(1)) > 6:
        s = s[: m.start(1)] + m.group(1)[:6] + s[m.end(1):]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def fnum(x, default: float | None = None) -> float | None:
    """Parse numbers that may arrive as str, float, or {"value": "..."}."""
    if x is None:
        return default
    if isinstance(x, dict):
        x = x.get("value")
        if x is None:
            return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


_CAMEL = re.compile(r"_([a-z0-9])")


def camelize(obj):
    """Recursively convert snake_case keys to camelCase (WS docs show both styles)."""
    if isinstance(obj, dict):
        return {(_CAMEL.sub(lambda m: m.group(1).upper(), k) if isinstance(k, str) else k): camelize(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [camelize(v) for v in obj]
    return obj
