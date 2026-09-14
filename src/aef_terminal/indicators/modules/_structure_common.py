from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS
from aef_terminal.runtime import pine
from aef_terminal.runtime.pivots import alternating_pivot_points


def _atr_value(bars: Sequence[Bar], length: int = DEFAULT_INDICATOR_SETTINGS.atr_len) -> float:
    if not bars:
        return 1.0
    return max(pine.atr_rma_series(bars, length)[-1], 1e-9)


def _pivot_points(bars: Sequence[Bar], left: int = 3, right: int = 3) -> list[dict[str, Any]]:
    return alternating_pivot_points(bars, left=left, right=right)
