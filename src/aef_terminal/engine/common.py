from __future__ import annotations

from math import isfinite

from aef_terminal.runtime.pine import (
    bar_index_at_or_before as bar_index_at_or_before,
)
from aef_terminal.runtime.timeframes import interval_minutes as interval_minutes
from aef_terminal.runtime.timeframes import (
    parse_aware_utc_ts as parse_aware_utc_ts,
)


def round_to_tick(value: float, tick: float) -> float:
    if (
        isinstance(tick, bool)
        or not isinstance(tick, (int, float))
        or not isfinite(float(tick))
        or float(tick) <= 0
    ):
        raise ValueError("PRICE_INCREMENT_INVALID")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError("PRICE_VALUE_INVALID")
    return round(float(value) / float(tick)) * float(tick)


def complete_trade_plan(entry: float | None, stop: float | None, target: float | None) -> bool:
    return entry is not None and stop is not None and target is not None


def maybe_round(value: float | None, tick: float | None = None) -> float | None:
    if value is None:
        return None
    if tick is None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
        ):
            raise ValueError("PRICE_VALUE_INVALID")
        return round(float(value), 10)
    return round_to_tick(value, tick)
