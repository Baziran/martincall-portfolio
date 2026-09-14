from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.context_cache import CachedAnalysisContext, get_cached_analysis_context
from aef_terminal.indicators.runtime import IndicatorRuntimeParams
from aef_terminal.indicators.runtime_params import build_indicator_runtime_params
from aef_terminal.indicators.defaults import indicator_defaults_from_params, score_promotion_floors
from aef_terminal.indicators.registry import (
    indicator_ids_for_shared_context,
)
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason, is_provisional_bar
from aef_terminal.runtime.bar_series import require_ordered_bar_list
from aef_terminal.runtime.instruments import InstrumentProfile, resolve_instrument_profile
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.pine import PineContext
from aef_terminal.runtime.timeframes import interval_minutes
from aef_terminal.engine.sentiment import tick_flow_bias_from_context

from aef_terminal.engine.analyze.features import VixContextInput, vix_features_from_context
from aef_terminal.features.price_action import displacement_feature
from aef_terminal.engine.analyze.mtf import (
    confirmed_bar_context_quality,
    mtf_context_quality,
    structure_bars_window,
    trim_mtf_context_to_parent,
)
from aef_terminal.features.context import FeatureContext
from aef_terminal.features.provider_session import ProviderSessionReset, provider_vwap_session


@dataclass(frozen=True)
class AnalyzeInputBundle:
    bars: list[Bar]
    latest: Bar
    confirmed_bars: list[Bar]
    analysis_latest: Bar
    live_signal_bars: list[Bar]
    instrument_profile: InstrumentProfile
    instrument_id: str
    route_fingerprint: str
    provider: str
    price_increment: float | None
    analysis_as_of_utc: datetime
    vwap_session: ProviderSessionReset
    indicator_params: dict[str, Any]
    global_defaults: Any
    runtime_params: IndicatorRuntimeParams
    cached_context: CachedAnalysisContext
    feature_context: FeatureContext
    live_feature_context: FeatureContext
    structure_feature_context: FeatureContext
    features: dict[str, Any]
    atr_value: float
    security_context: dict[str, list[Bar]]
    security_context_quality: dict[str, dict[str, Any]]
    confirmed_bar_context: dict[str, list[Bar]]
    confirmed_bar_context_slots: dict[str, ProviderBarSlotSequence]
    confirmed_bar_context_quality: dict[str, dict[str, Any]]
    structure_bars: list[Bar]
    structure_offset: int
    manual_channel_drawings: list[Mapping[str, Any]]
    watch_floor: float
    strong_watch_floor: float
    structure_floor: float
    tick_flow: dict[str, Any] | None
    tick_bias: dict[str, Any] | None
    confirmed_slots: ProviderBarSlotSequence | None


def live_bars_differ_from_confirmed(
    confirmed_bars: Sequence[Bar], live_signal_bars: Sequence[Bar]
) -> bool:
    if len(live_signal_bars) != len(confirmed_bars):
        return True
    return any(left != right for left, right in zip(live_signal_bars, confirmed_bars, strict=True))


def resolve_analysis_as_of_utc(
    bars: Sequence[Bar],
    analysis_as_of_utc: datetime | None,
) -> datetime:
    """Resolve one explicit execution clock or the deterministic latest-bar end."""

    resolved = analysis_as_of_utc
    if resolved is None:
        if not bars:
            raise ValueError("analysis_as_of_utc requires bars when no outer clock is provided")
        latest = bars[-1]
        resolved = latest.ts + timedelta(minutes=max(interval_minutes(latest.timeframe), 1))
    if (
        not isinstance(resolved, datetime)
        or resolved.tzinfo is None
        or resolved.utcoffset() is None
    ):
        raise ValueError("analysis_as_of_utc must be timezone-aware")
    return resolved.astimezone(UTC)


def admit_replay_confirmed_bars(
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
    field: str,
) -> list[Bar]:
    """Admit one causal exact-route series for offline replay.

    A provisional suffix is valid input state but is not observable by replay.
    Every confirmed bar must carry exact provenance for the selected instrument;
    this also admits canonically derived timeframes through their typed
    ``DETERMINISTIC_AGGREGATION`` provenance.
    """

    prepared = list(bars)
    if not prepared:
        return []
    if any(not isinstance(bar, Bar) for bar in prepared):
        raise TypeError(f"{field} must contain only Bar values")
    timeframe = prepared[0].timeframe
    require_ordered_bar_list(prepared, timeframe=timeframe, field=field)
    route = route_instrument(instrument)
    confirmed: list[Bar] = []
    provisional_tail_started = False
    for index, bar in enumerate(prepared):
        if is_provisional_bar(bar):
            provisional_tail_started = True
            continue
        if provisional_tail_started:
            raise ValueError(
                "REPLAY_CONFIRMED_AFTER_PROVISIONAL "
                f"field={field} index={index} ts={bar.ts.isoformat()}"
            )
        reject_reason = authoritative_bar_reject_reason(
            route.provider,
            bar,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
        )
        if reject_reason is not None:
            raise ValueError(
                "REPLAY_BAR_ADMISSION_REJECTED "
                f"field={field} index={index} ts={bar.ts.isoformat()} reason={reject_reason}"
            )
        confirmed.append(bar)
    return confirmed


def build_analyze_inputs(
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
    indicator_params: dict[str, Any] | None = None,
    mtf_context: dict[str, Sequence[Bar]] | None = None,
    mtf_context_slots: Mapping[str, ProviderBarSlotSequence] | None = None,
    mtf_quality: Mapping[str, Mapping[str, Any]] | None = None,
    tick_flow: dict[str, Any] | None = None,
    bar_slots: ProviderBarSlotSequence | None = None,
    analysis_as_of_utc: datetime | None = None,
    vix_context_input: VixContextInput | None = None,
) -> AnalyzeInputBundle:
    ctx = PineContext.from_bars(bars)
    bars_list = list(ctx.bars)
    latest = ctx.latest
    resolved_analysis_as_of = resolve_analysis_as_of_utc(bars_list, analysis_as_of_utc)
    route = route_instrument(instrument)
    instrument_profile = resolve_instrument_profile(instrument)
    confirmed_bars = list(ctx.confirmed_bars)
    analysis_latest = confirmed_bars[-1] if confirmed_bars else latest
    params = indicator_params or {}
    global_defaults = indicator_defaults_from_params(
        params.get("global_defaults") if isinstance(params.get("global_defaults"), dict) else None
    )
    feature_extra: dict[str, Any] = {}
    feature_extra.update(vix_features_from_context(vix_context_input))
    feature_extra.update(displacement_feature(confirmed_bars))

    security_context: dict[str, list[Bar]] = {}
    security_context_quality: dict[str, dict[str, Any]] = {}
    confirmed_bar_context: dict[str, list[Bar]] = {}
    confirmed_bar_slots: dict[str, ProviderBarSlotSequence] = {}
    confirmed_bar_quality: dict[str, dict[str, Any]] = {}
    context_cutoff = resolved_analysis_as_of
    security_timeframes = tuple(
        dict.fromkeys(
            [
                *(mtf_context or {}),
                *(mtf_context_slots or {}),
                *(mtf_quality or {}),
            ]
        )
    )
    for timeframe in security_timeframes:
        context_bars = (mtf_context or {}).get(timeframe) or ()
        context_slots = (mtf_context_slots or {}).get(timeframe)
        if context_slots is not None:
            if not isinstance(context_slots, ProviderBarSlotSequence):
                raise TypeError("mtf_context_slots values must be ProviderBarSlotSequence")
            if context_slots.authoritative is not True:
                raise ValueError("mtf_context_slots values must be authoritative")
            if len(context_slots) != len(context_bars):
                raise ValueError("mtf_context_slots must align exactly with mtf_context")
        slots_align = bool(
            context_slots is not None
            and context_slots.authoritative is True
            and len(context_slots) == len(context_bars)
        )
        context_minutes = max(interval_minutes(timeframe), 1)
        due_provisional_count = sum(
            1
            for bar in context_bars
            if (
                is_provisional_bar(bar)
                and context_cutoff is not None
                and bar.ts.astimezone(UTC) + timedelta(minutes=context_minutes) <= context_cutoff
            )
        )
        admitted_indexes = [
            index
            for index, bar in enumerate(context_bars)
            if (
                not is_provisional_bar(bar)
                and (
                    context_cutoff is None
                    or (
                        bar.ts.astimezone(UTC) + timedelta(minutes=context_minutes)
                        <= context_cutoff
                    )
                )
            )
        ]
        confirmed_context = [context_bars[index] for index in admitted_indexes]
        confirmed_context_slots = (
            ProviderBarSlotSequence(
                (context_slots[index] for index in admitted_indexes),
                schedule_state=context_slots.schedule_state,
            )
            if slots_align
            else None
        )
        trimmed_context = trim_mtf_context_to_parent(
            confirmed_bars,
            confirmed_context,
            timeframe,
        )
        upstream_quality = dict((mtf_quality or {}).get(timeframe) or {})
        if due_provisional_count:
            upstream_quality.update(
                {
                    "provider_complete": False,
                    "due_provisional_bar_count": due_provisional_count,
                }
            )
        if confirmed_context or upstream_quality:
            confirmed_bar_context[timeframe] = confirmed_context
            if confirmed_context_slots is not None:
                confirmed_bar_slots[timeframe] = confirmed_context_slots
            confirmed_bar_quality[timeframe] = confirmed_bar_context_quality(
                confirmed_context,
                upstream_quality,
                timeframe,
                context_slots=confirmed_context_slots,
                slots_required=mtf_context_slots is not None,
            )
        if trimmed_context or upstream_quality:
            security_context[timeframe] = trimmed_context
            security_context_quality[timeframe] = mtf_context_quality(
                confirmed_bars,
                trimmed_context,
                timeframe,
            )
            security_context_quality[timeframe].update(upstream_quality)

    live_signal_bars = list(confirmed_bars)
    if confirmed_bars and (latest.ts != analysis_latest.ts or latest.closed is False):
        if live_signal_bars and live_signal_bars[-1].ts == latest.ts:
            live_signal_bars[-1] = latest
        else:
            live_signal_bars.append(latest)
    vwap_session = provider_vwap_session(instrument, live_signal_bars)

    if bar_slots is not None:
        if not isinstance(bar_slots, ProviderBarSlotSequence):
            raise TypeError("bar_slots must be ProviderBarSlotSequence or None")
        if len(bar_slots) != len(bars_list):
            raise ValueError("bar_slots must align exactly with analysis bars")
    slot_axis_authoritative = bool(bar_slots is not None and bar_slots.authoritative)
    confirmed_slot_rows = (
        [
            slot
            for bar, slot in zip(bars_list, bar_slots, strict=False)
            if not is_provisional_bar(bar)
        ]
        if bar_slots is not None and slot_axis_authoritative
        else []
    )
    confirmed_slots = None
    if (
        bar_slots is not None
        and slot_axis_authoritative
        and len(confirmed_slot_rows) == len(confirmed_bars)
    ):
        confirmed_slots = ProviderBarSlotSequence(
            confirmed_slot_rows,
            schedule_state=bar_slots.schedule_state,
        )
    manual_channels_raw = params.get("manual_channels")
    manual_channel_drawings = (
        [item for item in manual_channels_raw if isinstance(item, Mapping)]
        if isinstance(manual_channels_raw, Sequence)
        and not isinstance(manual_channels_raw, (str, bytes))
        else []
    )
    runtime_params = build_indicator_runtime_params(params, global_defaults)
    tick_flow_consumers = indicator_ids_for_shared_context("tick_flow")
    active_tick_flow = (
        tick_flow
        if any(
            runtime_params.enabled.get(indicator_id, False) for indicator_id in tick_flow_consumers
        )
        and isinstance(tick_flow, dict)
        else None
    )
    structure_bars, structure_offset = structure_bars_window(confirmed_bars)
    cached_context = get_cached_analysis_context(
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        confirmed_bars=confirmed_bars,
        live_signal_bars=live_signal_bars,
        features_extra=feature_extra,
        defaults=global_defaults,
        instrument_profile=instrument_profile,
        structure_bars=structure_bars,
        structure_offset=structure_offset,
    )
    feature_context = cached_context.feature_context
    live_feature_context = cached_context.live_feature_context
    structure_feature_context = cached_context.structure_feature_context
    atr_value = feature_context.latest_atr
    features = dict(feature_context.values)
    tick_bias = tick_flow_bias_from_context(active_tick_flow, confirmed_bars)
    if tick_bias:
        features["tick_bias"] = tick_bias
    promotion_floors = score_promotion_floors(global_defaults)
    return AnalyzeInputBundle(
        bars=bars_list,
        latest=latest,
        confirmed_bars=confirmed_bars,
        analysis_latest=analysis_latest,
        live_signal_bars=live_signal_bars,
        instrument_profile=instrument_profile,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        provider=route.provider,
        price_increment=route.price_increment,
        analysis_as_of_utc=resolved_analysis_as_of,
        vwap_session=vwap_session,
        indicator_params=params,
        global_defaults=global_defaults,
        runtime_params=runtime_params,
        cached_context=cached_context,
        feature_context=feature_context,
        live_feature_context=live_feature_context,
        structure_feature_context=structure_feature_context,
        features=features,
        atr_value=atr_value,
        security_context=security_context,
        security_context_quality=security_context_quality,
        confirmed_bar_context=confirmed_bar_context,
        confirmed_bar_context_slots=confirmed_bar_slots,
        confirmed_bar_context_quality=confirmed_bar_quality,
        structure_bars=structure_bars,
        structure_offset=structure_offset,
        manual_channel_drawings=manual_channel_drawings,
        watch_floor=promotion_floors.watch,
        strong_watch_floor=promotion_floors.strong_watch,
        structure_floor=promotion_floors.structure,
        tick_flow=active_tick_flow,
        tick_bias=tick_bias,
        confirmed_slots=confirmed_slots,
    )
