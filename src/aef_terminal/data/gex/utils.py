from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any


def _expiration_date(value: str) -> date | None:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _median_positive(values: Sequence[float]) -> float | None:
    positive = sorted(value for value in values if value > 0)
    if not positive:
        return None
    mid = len(positive) // 2
    if len(positive) % 2:
        return positive[mid]
    return (positive[mid - 1] + positive[mid]) / 2.0


def parse_gex_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _strike_step(strikes: Sequence[dict[str, Any]]) -> float:
    values = sorted(
        {
            _num(row.get("strike"))
            for row in strikes
            if finite_number_or_none(row.get("strike")) is not None
        }
    )
    diffs = [right - left for left, right in zip(values, values[1:], strict=False) if right > left]
    if not diffs:
        return 1.0
    diffs.sort()
    return diffs[len(diffs) // 2]


GEX_KIND_CLASS = {
    "CALL_WALL": "call",
    "PUT_WALL": "put",
    "POS_GAMMA_NODE": "positive_node",
    "NEG_GAMMA_NODE": "negative_node",
    "GEX_NODE": "neutral_node",
    "FLIP": "flip",
}


def gex_kind_class(kind: Any) -> str:
    return GEX_KIND_CLASS.get(kind, "unknown") if isinstance(kind, str) else "unknown"


def _same_price(left: float | None, right: float | None) -> bool:
    return (
        left is not None
        and right is not None
        and math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-6)
    )


def finite_number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    output = float(value)
    if not math.isfinite(output):
        return None
    return output


def _num(value: Any) -> float:
    return finite_number_or_none(value) or 0.0


def _clip(value: float, low: float, high: float) -> float:
    return min(max(float(value), float(low)), float(high))
