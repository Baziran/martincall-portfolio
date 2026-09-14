from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from aef_terminal.domain import Bar, bar_revision_signature
from aef_terminal.runtime.timeframes import range_start_utc

from aef_terminal.engine.snapshot.constants import (
    DEFAULT_SIGNAL_RANGE,
    SIGNAL_RANGE_VALUES,
    SignalRange,
)


def require_signal_range(
    range_: object | None,
    *,
    default: SignalRange = DEFAULT_SIGNAL_RANGE,
) -> SignalRange:
    if default not in SIGNAL_RANGE_VALUES:
        raise ValueError(f"SIGNAL_RANGE_DEFAULT_INVALID value={default!r}")
    if range_ is None:
        return default
    if not isinstance(range_, str) or range_ not in SIGNAL_RANGE_VALUES:
        raise ValueError(
            f"SIGNAL_RANGE_INVALID value={range_!r} allowed={','.join(sorted(SIGNAL_RANGE_VALUES))}"
        )
    return cast(SignalRange, range_)


def _bars_inside_range(bars: Sequence[Bar], range_: str) -> list[Bar]:
    start = range_start_utc(range_)
    if start is None:
        return list(bars)
    return [bar for bar in bars if bar.ts >= start]


def _bars_cover_range(bars: Sequence[Bar], range_: str) -> bool:
    start = range_start_utc(range_)
    return start is None or bool(bars and bars[0].ts <= start)


def _same_bar_series(left: Sequence[Bar], right: Sequence[Bar]) -> bool:
    if len(left) != len(right):
        return False
    return all(
        bar_revision_signature(lbar) == bar_revision_signature(rbar)
        for lbar, rbar in zip(left, right, strict=False)
    )


def _join_warning(left: str, right: str) -> str:
    if left == right or (left and right and right in {part.strip() for part in left.split(";")}):
        return left
    if left and right:
        return f"{left}; {right}"
    return left or right
