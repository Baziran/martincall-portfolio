from __future__ import annotations

import math


def tinvest_quotation_value(value: object) -> float:
    """Convert one canonical T-Invest Quotation without changing signed facts."""

    units = getattr(value, "units", None)
    nano = getattr(value, "nano", None)
    if (
        isinstance(units, bool)
        or not isinstance(units, int)
        or isinstance(nano, bool)
        or not isinstance(nano, int)
        or abs(nano) > 999_999_999
        or (units > 0 and nano < 0)
        or (units < 0 and nano > 0)
    ):
        raise ValueError("TINVEST_QUOTATION_INVALID")
    try:
        result = float(units) + float(nano) / 1_000_000_000.0
    except OverflowError as exc:
        raise ValueError("TINVEST_QUOTATION_INVALID") from exc
    if not math.isfinite(result):
        raise ValueError("TINVEST_QUOTATION_INVALID")
    return result


__all__ = ["tinvest_quotation_value"]
