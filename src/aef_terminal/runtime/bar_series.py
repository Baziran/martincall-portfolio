from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from aef_terminal.domain import Bar, BarState


def require_ordered_bar_list(
    value: object,
    *,
    timeframe: str,
    field: str,
    require_confirmed: bool = False,
) -> list[Bar]:
    """Require one exact ordered bar series without coercing lookalikes."""

    if not isinstance(value, list) or any(not isinstance(bar, Bar) for bar in value):
        raise TypeError(f"{field} must be a Bar list")
    previous_ts: datetime | None = None
    for bar in value:
        if bar.timeframe != timeframe:
            raise ValueError(
                f"{field} timeframe mismatch: expected={timeframe} actual={bar.timeframe}"
            )
        if previous_ts is not None and bar.ts <= previous_ts:
            raise ValueError(f"{field} must be strictly timestamp-ordered")
        if require_confirmed and (
            bar.closed is not True or BarState(bar.state) is not BarState.CONFIRMED
        ):
            raise ValueError(f"{field} must contain only confirmed bars")
        previous_ts = bar.ts
    return value


def dedupe_bars_by_timestamp(bars: Sequence[Bar]) -> list[Bar]:
    deduped: dict[datetime, Bar] = {}
    for bar in sorted(bars, key=lambda item: item.ts):
        deduped[bar.ts.astimezone(UTC)] = bar
    return [deduped[ts] for ts in sorted(deduped)]


def merge_history_candidates_by_authority(
    candidates: Sequence[tuple[int, Sequence[Bar]]],
) -> list[Bar]:
    selected: dict[datetime, tuple[int, Bar]] = {}
    for priority, bars in sorted(candidates, key=lambda item: item[0]):
        for bar in sorted(bars, key=lambda item: item.ts):
            ts = bar.ts.astimezone(UTC)
            if ts not in selected:
                selected[ts] = (priority, bar)
    return [selected[ts][1] for ts in sorted(selected)]
