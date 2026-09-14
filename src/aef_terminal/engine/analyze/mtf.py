from __future__ import annotations

from collections.abc import Sequence
from aef_terminal.domain import Bar
from aef_terminal.indicators.runtime import offset_indicator_indices as offset_indicator_indices
from aef_terminal.runtime import pine
from aef_terminal.runtime.mtf import (
    confirmed_bar_context_quality as confirmed_bar_context_quality,
    mtf_context_quality as mtf_context_quality,
    parent_context_cutoff as parent_context_cutoff,
    trim_mtf_context_to_parent as trim_mtf_context_to_parent,
)
from aef_terminal.engine.analyze.constants import STRUCTURE_MAX_BARS


def structure_bars_window(
    bars: Sequence[Bar], max_bars: int = STRUCTURE_MAX_BARS
) -> tuple[list[Bar], int]:
    if len(bars) <= max_bars:
        return list(bars), 0
    offset = len(bars) - max_bars
    return list(bars[-max_bars:]), offset


def atr(bars: Sequence[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return max(bars[-1].high - bars[-1].low, 0.25) if bars else 0.25
    start = max(1, len(bars) - period)
    ranges = []
    for index in range(start, len(bars)):
        bar = bars[index]
        previous = bars[index - 1]
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous.close),
                abs(bar.low - previous.close),
            )
        )
    return max(sum(ranges) / max(len(ranges), 1), 0.25)


bar_is_closed = pine.bar_is_closed
