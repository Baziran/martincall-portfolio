from __future__ import annotations

from aef_terminal.runtime.timeframes import parse_aware_utc_ts


def utc_daypart(value: object) -> str:
    parsed = parse_aware_utc_ts(value)
    if parsed is None:
        return "unknown"
    hour = parsed.hour
    if hour < 7:
        return "utc_00_07"
    if hour < 13:
        return "utc_07_13"
    if hour < 21:
        return "utc_13_21"
    return "utc_21_24"
