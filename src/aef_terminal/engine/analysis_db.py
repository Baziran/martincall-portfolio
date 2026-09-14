from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from typing import Any

from aef_terminal.data.provider_sessions import continuous_provider_bar_slot
from aef_terminal.data.providers import load_provider_bars, route_instrument
from aef_terminal.domain import Bar
from aef_terminal.runtime.bar_series import merge_history_candidates_by_authority
from aef_terminal.runtime.mtf import ProviderBarSlotSequence


EMA233_5M_WARMUP_BARS = 233 * 3
EMA233_5M_WARMUP_RANGE = "14d"


def finer_timeframes(interval: str) -> list[str]:
    if interval == "3m":
        return ["1m"]
    if interval == "5m":
        return ["1m"]
    if interval == "15m":
        return ["5m", "1m"]
    if interval == "60m":
        return ["15m", "5m", "1m"]
    return []


def chart_guide_calculation_bars(
    signal_bars: Sequence[Bar],
    chart_bars: Sequence[Bar],
    *,
    interval: str,
    warmup_bars: Sequence[Bar] = (),
) -> list[Bar]:
    """Build the chart-guide axis with chart revisions and 5m EMA233 warmup."""

    chart_tail_ts = (
        max((bar.ts.astimezone(UTC) for bar in chart_bars), default=None)
        if interval == "5m"
        else None
    )
    candidates: list[tuple[int, Sequence[Bar]]] = [
        (0, chart_bars),
        (
            1,
            [
                bar
                for bar in signal_bars
                if chart_tail_ts is None or bar.ts.astimezone(UTC) <= chart_tail_ts
            ],
        ),
    ]
    merged = merge_history_candidates_by_authority(candidates)
    if interval == "5m" and len(merged) < EMA233_5M_WARMUP_BARS and warmup_bars:
        prefix_boundary = merged[0].ts.astimezone(UTC) if merged else None
        candidates.append(
            (
                2,
                [
                    bar
                    for bar in warmup_bars
                    if prefix_boundary is None or bar.ts.astimezone(UTC) < prefix_boundary
                ],
            )
        )
        merged = merge_history_candidates_by_authority(candidates)
    return merged


def read_provider_mtf_context(
    store: Any | None,
    instrument: dict[str, Any],
    interval: str,
    range_: str,
    *,
    timeframes: Sequence[str] | None = None,
) -> dict[str, list[Bar]]:
    if store is None:
        return {}
    context: dict[str, list[Bar]] = {}
    route = route_instrument(instrument)
    requested_timeframes = (
        tuple(str(item) for item in timeframes)
        if timeframes is not None
        else tuple(finer_timeframes(interval))
    )
    for timeframe in requested_timeframes:
        bars, _warning = load_provider_bars(
            route,
            timeframe,
            range_,
            0.0,
            store=store,
        )
        if bars:
            context[timeframe] = bars
    return context


def provider_backed_bar_slots(
    store: Any | None,
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
) -> ProviderBarSlotSequence | None:
    route = route_instrument(instrument)
    if not bars:
        return None
    continuous_session = route.adapter.continuous_session(route.instrument)
    if store is None:
        if continuous_session:
            return ProviderBarSlotSequence(
                (continuous_provider_bar_slot(bar.ts, bar.timeframe) for bar in bars),
                schedule_state="continuous",
            )
        return None
    raise RuntimeError("FULL_SNAPSHOT_CONTEXT_REQUIRED_FOR_PROVIDER_BAR_SLOTS")
