from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aef_terminal.data.providers import (
    default_data_source,
    load_provider_bars,
    normalize_provider_key,
    provider_data_policy,
    route_instrument,
)
from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.domain import Bar
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.telemetry import log_structured_error
from aef_terminal.engine.analysis_db import (
    EMA233_5M_WARMUP_BARS,
    EMA233_5M_WARMUP_RANGE,
    chart_guide_calculation_bars,
)
from aef_terminal.engine.snapshot_constants import SNAPSHOT_SLOW_LOG_SECONDS
from aef_terminal.engine.data_quality import (
    data_quality_report,
    serialize_data_quality_report,
)
from aef_terminal.engine.vsa_context import (
    align_vsa_volume_series,
    build_vsa_volume_context_for_instrument,
)
from aef_terminal.engine.serialization import (
    serialize_bars,
)

from aef_terminal.engine.analyze.bars import analyze_bars
from aef_terminal.engine.analyze.inputs import build_analyze_inputs, resolve_analysis_as_of_utc
from aef_terminal.engine.analyze.constants import _LOGGER
from aef_terminal.engine.analyze.features import _json_safe
from aef_terminal.engine.snapshot.range import _same_bar_series
from aef_terminal.engine.snapshot.db_context import (
    snapshot_db_context,
    chart_future_axis_payload,
    provider_chart_axis_slots,
    snapshot_futures_roll_events,
)
from aef_terminal.engine.snapshot.read_context import SnapshotReadContext
from aef_terminal.features.chart_guides import build_chart_guides
from aef_terminal.features.context import build_feature_context
from aef_terminal.features.provider_session import provider_vwap_session
from aef_terminal.indicators.defaults import indicator_defaults_from_params
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.overlays import futures_roll_event_overlays


def _apply_futures_roll_overlays(
    snapshot: dict[str, Any],
    *,
    bars: Sequence[Bar],
    roll_events: Sequence[dict[str, Any]],
) -> None:
    overlays = futures_roll_event_overlays(bars=bars, roll_events=roll_events)
    if not overlays:
        return
    snapshot.setdefault("overlays", []).extend(overlays)


def _replace_vsa_chart_series(
    snapshot: dict[str, Any],
    *,
    calculation_bars: Sequence[Bar],
    display_axis: Sequence[dict[str, Any]],
    instrument: dict[str, Any],
    indicator_params: dict[str, Any],
    confirmed_slots: Any | None,
) -> None:
    vsa_payload = snapshot.get("vsa_volume")
    if not isinstance(vsa_payload, dict):
        return
    display_result = build_vsa_volume_context_for_instrument(
        calculation_bars,
        instrument,
        indicator_params=indicator_params,
        confirmed_slots=confirmed_slots,
    )
    aligned = align_vsa_volume_series(
        display_result.get("series") if isinstance(display_result.get("series"), list) else [],
        display_axis,
    )
    canonical_series = (
        vsa_payload.get("series") if isinstance(vsa_payload.get("series"), list) else []
    )
    canonical_by_ts = {
        str(item.get("ts") or ""): dict(item)
        for item in canonical_series
        if isinstance(item, dict) and str(item.get("ts") or "")
    }
    aligned = [
        {
            **item,
            **canonical_by_ts.get(str(item.get("ts") or ""), {}),
            "ts": item.get("ts"),
            "bar_slot": item.get("bar_slot"),
            "authoritative": item.get("authoritative"),
            "availability_state": item.get("availability_state"),
            **({} if str(item.get("ts") or "") in canonical_by_ts else {"breakout_context": None}),
        }
        for item in aligned
    ]
    resolved = {**vsa_payload, "series": aligned}
    status = resolved.get("status")
    if isinstance(status, dict):
        resolved["status"] = {**status, "series_count": len(aligned)}
    snapshot["vsa_volume"] = resolved


@dataclass(frozen=True, slots=True)
class _PreparedChartPayload:
    bars: tuple[Bar, ...]
    serialized_bars: tuple[dict[str, Any], ...]
    calculation_bars: tuple[Bar, ...]
    quality: dict[str, Any]
    future_axis: dict[str, Any] | None
    confirmed_slots: Any | None
    same_series: bool
    magnet_warmup_failed: bool


def _analyze_preloaded_snapshot(
    *,
    bars: Sequence[Bar],
    instrument: dict[str, Any],
    indicator_params: dict[str, Any],
    mtf_context: dict[str, list[Bar]],
    mtf_context_slots: dict[str, Any],
    mtf_quality: dict[str, dict[str, Any]],
    bar_slots: ProviderBarSlotSequence | None,
    analysis_as_of_utc: datetime,
    gex_history: list[dict[str, Any]],
    gex_context_active: bool,
    gex_capture_mode: GexCaptureMode,
    option_targets: list[dict[str, Any]],
    option_flow: dict[str, Any] | None,
    tick_flow: dict[str, Any] | None,
    delay_grace: int | float,
    include_telemetry: bool,
    analyze_inputs: Any,
    store: Any | None,
    provider_key: str,
) -> dict[str, Any]:
    return analyze_bars(
        bars,
        display_symbol=instrument["display"],
        indicator_params=indicator_params,
        mtf_context=mtf_context,
        mtf_context_slots=mtf_context_slots,
        mtf_quality=mtf_quality,
        bar_slots=bar_slots,
        analysis_as_of_utc=analysis_as_of_utc,
        gex_history=gex_history,
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
        option_targets=option_targets,
        option_flow=option_flow,
        tick_flow=tick_flow,
        data_delay_grace_seconds=delay_grace,
        include_telemetry=include_telemetry,
        inputs=analyze_inputs,
        data_quality_store=store,
        instrument=instrument,
        data_provider=provider_key,
        serialize_output=False,
    )


def _apply_prepared_chart_payload(
    snapshot: dict[str, Any],
    prepared: _PreparedChartPayload,
    *,
    instrument: dict[str, Any],
    indicator_params: dict[str, Any],
    include_telemetry: bool,
) -> None:
    if (
        prepared.magnet_warmup_failed
        or not prepared.same_series
        or not _same_bar_series(prepared.calculation_bars, prepared.bars)
    ):
        chart_feature_context = build_feature_context(
            prepared.calculation_bars,
            instrument_profile=resolve_instrument_profile(instrument),
            defaults=indicator_defaults_from_params(
                indicator_params.get("global_defaults")
                if isinstance(indicator_params.get("global_defaults"), dict)
                else None
            ),
        )
        snapshot["chart_guides"] = build_chart_guides(
            prepared.calculation_bars,
            symbol=instrument["display"],
            feature_context=chart_feature_context,
            instrument=instrument,
            display_axis=prepared.serialized_bars,
            context_bars=prepared.bars,
            include_telemetry=include_telemetry,
            magnet_warmup_failed=prepared.magnet_warmup_failed,
        )
    if not prepared.same_series:
        _replace_vsa_chart_series(
            snapshot,
            calculation_bars=prepared.calculation_bars,
            display_axis=prepared.serialized_bars,
            instrument=instrument,
            indicator_params=indicator_params,
            confirmed_slots=prepared.confirmed_slots,
        )
    snapshot["bars"] = list(prepared.serialized_bars)
    snapshot["future_axis"] = prepared.future_axis
    snapshot["meta"]["chart_data_quality"] = serialize_data_quality_report(prepared.quality)


def _finalize_snapshot(
    snapshot: dict[str, Any],
    *,
    chart_bars: Sequence[Bar],
    signal_bars: Sequence[Bar],
    chart_range: str,
    signal_range: str,
    roll_events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    snapshot["meta"]["chart_range"] = chart_range
    snapshot["meta"]["signal_range"] = signal_range
    snapshot["meta"]["chart_bar_count"] = len(chart_bars) if chart_bars else len(signal_bars)
    snapshot["meta"]["signal_bar_count"] = len(signal_bars)
    _apply_futures_roll_overlays(
        snapshot,
        bars=chart_bars if chart_bars else signal_bars,
        roll_events=roll_events,
    )
    if signal_range != chart_range:
        snapshot["meta"]["signal_note"] = (
            f"Signals calculated on {signal_range}; chart shows {chart_range}"
        )
    return _json_safe(snapshot)


@dataclass(frozen=True, slots=True)
class _SnapshotAssemblyPlan:
    read_context: SnapshotReadContext
    instrument: dict[str, str]
    route: InstrumentRoute
    input_chart_bars: tuple[Bar, ...]
    input_chart_matches_analysis: bool
    indicator_params: dict[str, Any]
    provider_key: str
    analysis_as_of_utc: datetime


def _prepare_snapshot_assembly(
    *,
    instrument: dict[str, str],
    chart_symbol: str,
    interval: str,
    range_: str,
    bars: Sequence[Bar],
    indicator_params: dict[str, Any] | None,
    gex_context_active: bool,
    gex_capture_mode: GexCaptureMode,
    show_visuals: bool,
    chart_bars: Sequence[Bar] | None,
    data_provider: str,
    store: Any | None,
    analysis_as_of_utc: datetime | None,
) -> _SnapshotAssemblyPlan:
    input_bars = list(bars)
    input_chart_bars = tuple(chart_bars or ())
    effective_indicator_params = dict(indicator_params or {})
    effective_indicator_params.setdefault("show_visuals", show_visuals)
    provider_key = normalize_provider_key(data_provider)
    route = route_instrument(instrument, expected_source=provider_key)
    resolved_analysis_as_of = resolve_analysis_as_of_utc(
        input_bars,
        analysis_as_of_utc,
    )
    return _SnapshotAssemblyPlan(
        read_context=SnapshotReadContext(
            store=store,
            symbol=chart_symbol,
            interval=interval,
            range_=range_,
            bars=input_bars,
            provider_key=provider_key,
            instrument=instrument,
            indicator_params=effective_indicator_params,
            gex_context_active=gex_context_active,
            gex_capture_mode=gex_capture_mode,
            analysis_as_of_utc=resolved_analysis_as_of,
        ),
        instrument=instrument,
        route=route,
        input_chart_bars=input_chart_bars,
        input_chart_matches_analysis=bool(input_chart_bars)
        and _same_bar_series(input_chart_bars, input_bars),
        indicator_params=effective_indicator_params,
        provider_key=provider_key,
        analysis_as_of_utc=resolved_analysis_as_of,
    )


def _assemble_prefetched_snapshot(
    plan: _SnapshotAssemblyPlan,
    *,
    chart_range: str | None,
    signal_range: str | None,
    include_telemetry: bool,
) -> dict[str, Any]:
    read_ctx = plan.read_context
    (
        bar_slots,
        mtf_context,
        mtf_context_slots,
        mtf_quality,
    ) = read_ctx.get_db_context()
    bars = list(read_ctx.bars)
    gex_history = read_ctx.get_gex_history()
    option_flow = read_ctx.get_option_flow()
    tick_flow = read_ctx.get_tick_flow()
    option_targets = read_ctx.get_option_targets()
    analyze_inputs = build_analyze_inputs(
        bars,
        instrument=plan.instrument,
        indicator_params=plan.indicator_params,
        mtf_context=mtf_context,
        mtf_context_slots=mtf_context_slots,
        mtf_quality=mtf_quality,
        tick_flow=tick_flow,
        bar_slots=bar_slots,
        analysis_as_of_utc=plan.analysis_as_of_utc,
        vix_context_input=read_ctx.get_vix_context_input(),
    )
    delay_grace = provider_data_policy(plan.provider_key).tail_delay_grace_seconds
    snapshot = _analyze_preloaded_snapshot(
        bars=bars,
        instrument=plan.instrument,
        indicator_params=plan.indicator_params,
        mtf_context=mtf_context,
        mtf_context_slots=mtf_context_slots,
        mtf_quality=mtf_quality,
        bar_slots=bar_slots,
        analysis_as_of_utc=plan.analysis_as_of_utc,
        gex_history=gex_history,
        gex_context_active=read_ctx.gex_context_active,
        gex_capture_mode=read_ctx.gex_capture_mode,
        option_targets=option_targets,
        option_flow=option_flow,
        tick_flow=tick_flow,
        delay_grace=delay_grace,
        include_telemetry=include_telemetry,
        analyze_inputs=analyze_inputs,
        store=read_ctx.store,
        provider_key=plan.provider_key,
    )
    chart_bars_list = (
        list(bars) if plan.input_chart_matches_analysis else list(plan.input_chart_bars)
    )
    if chart_bars_list:
        same_chart_series = _same_bar_series(chart_bars_list, bars)
        if same_chart_series:
            chart_slots = bar_slots
            chart_quality = dict(snapshot["meta"].get("data_quality") or {})
        else:
            (
                chart_bars_list,
                chart_slots,
                _chart_mtf_context,
                _chart_mtf_context_slots,
                _chart_mtf_quality,
            ) = snapshot_db_context(
                read_ctx.store,
                read_ctx.symbol,
                read_ctx.interval,
                chart_range or read_ctx.range_,
                chart_bars_list,
                provider_key=plan.provider_key,
                instrument=plan.instrument,
                include_mtf=False,
                analysis_as_of_utc=plan.analysis_as_of_utc,
            )
            same_chart_series = _same_bar_series(chart_bars_list, bars)
            chart_quality = data_quality_report(
                chart_bars_list,
                read_ctx.interval,
                now_utc=plan.analysis_as_of_utc,
                tail_delay_grace_seconds=delay_grace,
                store=read_ctx.store,
                instrument=plan.instrument,
            )
        serialized_chart_bars = serialize_bars(
            chart_bars_list,
            chart_slots,
            session_reset=provider_vwap_session(plan.instrument, chart_bars_list),
        )
        chart_calculation_bars = chart_guide_calculation_bars(
            bars,
            chart_bars_list,
            interval=read_ctx.interval,
        )
        magnet_warmup_failed = False
        if (
            read_ctx.interval == "5m"
            and read_ctx.store is not None
            and len(chart_calculation_bars) < EMA233_5M_WARMUP_BARS
        ):
            try:
                warmup_bars, _warning = load_provider_bars(
                    plan.route,
                    read_ctx.interval,
                    EMA233_5M_WARMUP_RANGE,
                    0.0,
                    store=read_ctx.store,
                )
            except Exception as exc:
                magnet_warmup_failed = True
                log_structured_error(
                    _LOGGER,
                    provider=plan.provider_key,
                    symbol=read_ctx.symbol,
                    interval=read_ctx.interval,
                    range_=EMA233_5M_WARMUP_RANGE,
                    op="read_chart_guide_warmup",
                    error=exc,
                    event="chart_guide_warmup_failed",
                    instrument_id=qualified_instrument_id(plan.instrument),
                )
            else:
                chart_calculation_bars = chart_guide_calculation_bars(
                    bars,
                    chart_bars_list,
                    interval=read_ctx.interval,
                    warmup_bars=warmup_bars,
                )
        confirmed_slots = None
        if not same_chart_series:
            try:
                confirmed_slots = provider_chart_axis_slots(
                    read_ctx.store,
                    plan.instrument,
                    read_ctx.interval,
                    chart_calculation_bars,
                )
            except Exception:
                confirmed_slots = None
        future_axis = chart_future_axis_payload(
            read_ctx.store,
            read_ctx.interval,
            serialized_chart_bars,
            provider_key=plan.provider_key,
            instrument=plan.instrument,
        )
        _apply_prepared_chart_payload(
            snapshot,
            _PreparedChartPayload(
                bars=tuple(chart_bars_list),
                serialized_bars=tuple(serialized_chart_bars),
                calculation_bars=tuple(chart_calculation_bars),
                quality=chart_quality,
                future_axis=future_axis,
                confirmed_slots=confirmed_slots,
                same_series=same_chart_series,
                magnet_warmup_failed=magnet_warmup_failed,
            ),
            instrument=plan.instrument,
            indicator_params=plan.indicator_params,
            include_telemetry=include_telemetry,
        )
    displayed_bars = chart_bars_list if chart_bars_list else bars
    roll_events = snapshot_futures_roll_events(
        read_ctx.store,
        instrument=plan.instrument,
        bars=displayed_bars,
        provider_key=plan.provider_key,
    )
    return _finalize_snapshot(
        snapshot,
        chart_bars=chart_bars_list,
        signal_bars=bars,
        chart_range=chart_range or read_ctx.range_,
        signal_range=signal_range or read_ctx.range_,
        roll_events=roll_events,
    )


def analyze_market_bars_for_snapshot(
    instrument: dict[str, str],
    chart_symbol: str,
    interval: str,
    range_: str,
    bars: Sequence[Bar],
    indicator_params: dict[str, Any] | None = None,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
    show_visuals: bool = True,
    chart_bars: Sequence[Bar] | None = None,
    chart_range: str | None = None,
    signal_range: str | None = None,
    data_provider: str = default_data_source(),
    include_telemetry: bool = False,
    store: Any | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    plan = _prepare_snapshot_assembly(
        instrument=instrument,
        chart_symbol=chart_symbol,
        interval=interval,
        range_=range_,
        bars=bars,
        indicator_params=indicator_params,
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
        show_visuals=show_visuals,
        chart_bars=chart_bars,
        data_provider=data_provider,
        store=store,
        analysis_as_of_utc=analysis_as_of_utc,
    )
    plan.read_context.prefetch_sync()
    result = _assemble_prefetched_snapshot(
        plan,
        chart_range=chart_range,
        signal_range=signal_range,
        include_telemetry=include_telemetry,
    )
    elapsed = time.perf_counter() - started
    if elapsed >= SNAPSHOT_SLOW_LOG_SECONDS:
        log_structured_error(
            _LOGGER,
            provider=str(data_provider or "unknown"),
            symbol=chart_symbol,
            interval=interval,
            range_=range_,
            op="analyze_market_bars_for_snapshot",
            error=f"slow snapshot assembly {elapsed:.3f}s",
            level="debug",
            event="analyze_market_bars_slow",
        )
    return result


async def async_analyze_market_bars_for_snapshot(
    instrument: dict[str, str],
    chart_symbol: str,
    interval: str,
    range_: str,
    bars: Sequence[Bar],
    indicator_params: dict[str, Any] | None = None,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
    show_visuals: bool = True,
    chart_bars: Sequence[Bar] | None = None,
    chart_range: str | None = None,
    signal_range: str | None = None,
    data_provider: str = default_data_source(),
    include_telemetry: bool = False,
    store: Any | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    plan = _prepare_snapshot_assembly(
        instrument=instrument,
        chart_symbol=chart_symbol,
        interval=interval,
        range_=range_,
        bars=bars,
        indicator_params=indicator_params,
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
        show_visuals=show_visuals,
        chart_bars=chart_bars,
        data_provider=data_provider,
        store=store,
        analysis_as_of_utc=analysis_as_of_utc,
    )
    await plan.read_context.async_prefetch()
    return await run_physical_thread_call(
        _assemble_prefetched_snapshot,
        plan,
        chart_range=chart_range,
        signal_range=signal_range,
        include_telemetry=include_telemetry,
    )
