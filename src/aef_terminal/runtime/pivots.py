from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aef_terminal.domain import Bar


def alternating_pivot_points(
    bars: Sequence[Bar], left: int = 3, right: int = 3
) -> list[dict[str, Any]]:
    pivots: list[dict[str, Any]] = []
    if len(bars) < left + right + 2:
        return pivots
    for index in range(left, len(bars) - right):
        window = bars[index - left : index + right + 1]
        bar = bars[index]
        is_high = bar.high >= max(item.high for item in window)
        is_low = bar.low <= min(item.low for item in window)
        if not is_high and not is_low:
            continue
        pivot_type = (
            1
            if is_high and not is_low
            else -1
            if is_low and not is_high
            else 1
            if bar.close < bar.open
            else -1
        )
        price = bar.high if pivot_type == 1 else bar.low
        if pivots and pivots[-1]["type"] == pivot_type:
            previous = pivots[-1]
            replace = price > previous["price"] if pivot_type == 1 else price < previous["price"]
            if replace:
                pivots[-1] = {
                    "index": index,
                    "ts": bar.ts.isoformat(),
                    "price": price,
                    "type": pivot_type,
                }
            continue
        pivots.append(
            {"index": index, "ts": bar.ts.isoformat(), "price": price, "type": pivot_type}
        )
    return pivots


@dataclass
class PivotContext:
    bars: Sequence[Bar]
    _alternating_cache: dict[tuple[int, int], list[dict[str, Any]]] = field(default_factory=dict)

    def alternating(self, left: int = 3, right: int = 3) -> list[dict[str, Any]]:
        key = (max(int(left), 1), max(int(right), 1))
        cached = self._alternating_cache.get(key)
        if cached is None:
            cached = alternating_pivot_points(self.bars, left=key[0], right=key[1])
            self._alternating_cache[key] = cached
        return [dict(item) for item in cached]
