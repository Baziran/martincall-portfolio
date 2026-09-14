from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except TypeError, ValueError:
        return default
    return out if math.isfinite(out) else default


def float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except TypeError, ValueError:
        return None
    return out if math.isfinite(out) else None


def exact_finite_number_or_none(value: Any) -> float | None:
    """Return a finite built-in numeric scalar without coercing text or bool."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def is_exact_finite_number(value: Any) -> bool:
    return exact_finite_number_or_none(value) is not None


def clamp_float(value: Any, default: float, low: float, high: float) -> float:
    return clamp(safe_float(value, default), low, high)


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        out = int(value)
    except TypeError, ValueError:
        out = default
    return max(low, min(out, high))


def round_optional(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return default if abs(denominator) < 1e-12 else numerator / denominator


def log_returns(values: Sequence[float]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(values, values[1:], strict=False):
        if previous > 0 and current > 0:
            value = math.log(current / previous)
            if math.isfinite(value):
                returns.append(value)
    return returns


def sample_stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(variance, 0.0))
