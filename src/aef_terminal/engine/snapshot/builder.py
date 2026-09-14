from __future__ import annotations

from datetime import datetime
from typing import Any

from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.data.providers import (
    async_load_provider_bars,
    default_data_source,
    load_provider_bars,
    route_instrument,
)

from aef_terminal.engine.snapshot.assembly import (
    analyze_market_bars_for_snapshot,
    async_analyze_market_bars_for_snapshot,
)
from aef_terminal.engine.snapshot.empty import empty_market_snapshot
from aef_terminal.engine.snapshot.range import (
    _bars_cover_range,
    _bars_inside_range,
    _join_warning,
    require_signal_range,
)
from aef_terminal.engine.snapshot.warnings import attach_provider_warning
from aef_terminal.runtime.timeframes import HistoryRangeWindow


def build_market_snapshot_from_db(
    source: str = default_data_source(),
    interval: str = "5m",
    range_: str = "1d",
    signal_range_: str | None = None,
    timeout: float = 8.0,
    indicator_params: dict[str, Any] | None = None,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
    show_visuals: bool = True,
    include_chart_projection: bool = True,
    include_telemetry: bool = False,
    store: Any | None = None,
    instrument: dict[str, Any] | None = None,
    window: HistoryRangeWindow | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(instrument, dict):
        raise ValueError("Market snapshot requires provider-qualified instrument identity")
    instrument = dict(instrument)
    route = route_instrument(instrument, expected_source=source)
    provider = route.provider
    chart_symbol = route.provider_symbol

    bars, provider_warning = load_provider_bars(
        route,
        interval,
        range_,
        timeout,
        live_refresh=False,
        store=store,
        window=window,
    )
    min_bars = 10 if interval == "60m" else 35
    if len(bars) >= min_bars:
        signal_range = require_signal_range(signal_range_)
        chart_signal_bars = (
            _bars_inside_range(bars, signal_range) if signal_range != range_ else list(bars)
        )
        if len(chart_signal_bars) >= min_bars and _bars_cover_range(bars, signal_range):
            analysis_bars = chart_signal_bars
        else:
            signal_bars, signal_warning = load_provider_bars(
                route,
                interval,
                signal_range,
                timeout,
                live_refresh=False,
                store=store,
            )
            provider_warning = _join_warning(provider_warning, signal_warning)
            if not signal_bars:
                raise ValueError(
                    "SIGNAL_RANGE_HISTORY_UNAVAILABLE "
                    f"range={signal_range} interval={interval} provider={provider}"
                )
            analysis_bars = signal_bars
        snapshot = analyze_market_bars_for_snapshot(
            instrument,
            chart_symbol,
            interval,
            signal_range,
            analysis_bars,
            indicator_params,
            gex_context_active=gex_context_active,
            gex_capture_mode=gex_capture_mode,
            show_visuals=show_visuals,
            chart_bars=bars if include_chart_projection else (),
            chart_range=range_,
            signal_range=signal_range,
            data_provider=provider,
            include_telemetry=include_telemetry,
            store=store,
            analysis_as_of_utc=analysis_as_of_utc,
        )
        attach_provider_warning(snapshot, provider_warning)
        return snapshot
    return empty_market_snapshot(
        instrument,
        interval,
        (
            f"{provider.upper()} returned only {len(bars)} bars for {chart_symbol} {interval} {range_}."
        ),
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
    )


async def async_build_market_snapshot_from_db(
    source: str = default_data_source(),
    interval: str = "5m",
    range_: str = "1d",
    signal_range_: str | None = None,
    timeout: float = 8.0,
    indicator_params: dict[str, Any] | None = None,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
    show_visuals: bool = True,
    include_chart_projection: bool = True,
    include_telemetry: bool = False,
    store: Any | None = None,
    instrument: dict[str, Any] | None = None,
    window: HistoryRangeWindow | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(instrument, dict):
        raise ValueError("Market snapshot requires provider-qualified instrument identity")
    instrument = dict(instrument)
    route = route_instrument(instrument, expected_source=source)
    provider = route.provider
    chart_symbol = route.provider_symbol

    bars, provider_warning = await async_load_provider_bars(
        route,
        interval,
        range_,
        timeout,
        live_refresh=False,
        store=store,
        window=window,
    )
    min_bars = 10 if interval == "60m" else 35
    if len(bars) >= min_bars:
        signal_range = require_signal_range(signal_range_)
        chart_signal_bars = (
            _bars_inside_range(bars, signal_range) if signal_range != range_ else list(bars)
        )
        if len(chart_signal_bars) >= min_bars and _bars_cover_range(bars, signal_range):
            analysis_bars = chart_signal_bars
        else:
            signal_bars, signal_warning = await async_load_provider_bars(
                route,
                interval,
                signal_range,
                timeout,
                live_refresh=False,
                store=store,
            )
            provider_warning = _join_warning(provider_warning, signal_warning)
            if not signal_bars:
                raise ValueError(
                    "SIGNAL_RANGE_HISTORY_UNAVAILABLE "
                    f"range={signal_range} interval={interval} provider={provider}"
                )
            analysis_bars = signal_bars
        snapshot = await async_analyze_market_bars_for_snapshot(
            instrument,
            chart_symbol,
            interval,
            signal_range,
            analysis_bars,
            indicator_params,
            gex_context_active=gex_context_active,
            gex_capture_mode=gex_capture_mode,
            show_visuals=show_visuals,
            chart_bars=bars if include_chart_projection else (),
            chart_range=range_,
            signal_range=signal_range,
            data_provider=provider,
            include_telemetry=include_telemetry,
            store=store,
            analysis_as_of_utc=analysis_as_of_utc,
        )
        attach_provider_warning(snapshot, provider_warning)
        return snapshot
    return empty_market_snapshot(
        instrument,
        interval,
        (
            f"{provider.upper()} async returned only {len(bars)} bars for {chart_symbol} {interval} {range_}."
        ),
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
    )
