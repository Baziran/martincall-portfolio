from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from aef_terminal.domain import Bar, ScenarioDecision, SignalCandidate
from aef_terminal.engine.indicator_pipeline import execute_indicator_pipeline
from aef_terminal.engine.indicator_adapters import IndicatorRunContext, indicator_execution_specs
from aef_terminal.engine.indicator_policy import indicator_candidate_score_floor
from aef_terminal.engine.vsa_context import (
    build_vsa_volume_context,
    build_vsa_volume_params,
)
from aef_terminal.features.vsa import vsa_facts
from aef_terminal.features.vsa_classify import vsa_breakout_facts
from aef_terminal.features.context import FeatureContext
from aef_terminal.features.market_context import vix_risk_context
from aef_terminal.features.market_series import build_market_series_block
from aef_terminal.features.provider_session import ProviderSessionReset

from aef_terminal.engine.analyze.context import apply_vsa_fuel_sfp_cooldown
from aef_terminal.engine.analyze.inputs import AnalyzeInputBundle, live_bars_differ_from_confirmed
from aef_terminal.engine.analyze.indicator_runtime import (
    promote_pipeline_candidates,
    run_pipeline_indicator,
)
from aef_terminal.engine.analyze.structure import run_structure_indicator_pipeline
from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.indicators.registry import indicator_ids_for_shared_context
from aef_terminal.indicators.runtime import IndicatorRuntimeParams
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.mtf import ProviderBarSlotSequence


def _any_indicator_enabled(
    runtime_params: IndicatorRuntimeParams, indicator_ids: Sequence[str]
) -> bool:
    return any(
        bool(runtime_params.enabled.get(indicator_id, False)) for indicator_id in indicator_ids
    )


def _calculate_primary_signal_indicators(
    *,
    confirmed_bars: Sequence[Bar],
    live_signal_bars: Sequence[Bar],
    latest: Bar,
    analysis_latest: Bar,
    runtime_params: IndicatorRuntimeParams,
    global_defaults: IndicatorDefaults,
    indicator_params: dict[str, Any],
    instrument_profile: InstrumentProfile,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    analysis_as_of_utc: datetime,
    quality: dict[str, Any],
    vwap_session: ProviderSessionReset,
    features: dict[str, Any],
    feature_context: FeatureContext,
    option_flow: dict[str, Any] | None,
    tick_flow: dict[str, Any] | None,
    manual_channel_drawings: Sequence[Mapping[str, Any]],
    confirmed_slots: ProviderBarSlotSequence | None,
    confirmed_bar_context: dict[str, list[Bar]] | None = None,
    confirmed_bar_context_slots: dict[str, ProviderBarSlotSequence] | None = None,
    confirmed_bar_context_quality: dict[str, dict[str, Any]] | None = None,
    atr_value: float,
    strong_watch_floor: float,
    structure_floor: float,
    score_overrides: dict[str, Any],
    timings: dict[str, float] | None = None,
) -> tuple[
    dict[str, dict[str, Any]],
    list[SignalCandidate],
    dict[str, Any],
    dict[str, Any] | None,
    dict[str, Any],
]:
    vsa_params = build_vsa_volume_params(
        global_defaults,
        instrument_profile,
    )
    shared_vsa_facts = (
        vsa_facts(
            confirmed_bars,
            vsa_params,
            vwap_session=vwap_session,
            confirmed_slots=confirmed_slots,
        )
        if confirmed_bars
        else []
    )
    shared_vsa_breakout_facts = (
        vsa_breakout_facts(
            confirmed_bars,
            shared_vsa_facts,
            profile_key=instrument_profile.key,
            vwap_session=vwap_session,
            confirmed_slots=confirmed_slots,
        )
        if confirmed_bars
        else []
    )
    vsa_context = build_vsa_volume_context(
        confirmed_bars,
        profile=instrument_profile,
        params=vsa_params,
        facts=shared_vsa_facts,
        breakout_facts=shared_vsa_breakout_facts,
        confirmed_slots=confirmed_slots,
        vwap_session=vwap_session,
    )
    needs_market_series = _any_indicator_enabled(
        runtime_params,
        indicator_ids_for_shared_context("market_series", stage="primary"),
    )
    needs_shared_vix = _any_indicator_enabled(
        runtime_params,
        indicator_ids_for_shared_context("vix_context", stage="primary"),
    )
    shared_vix_context: dict[str, Any] | None = None
    shared_market_series = None
    if confirmed_bars and vwap_session.available and (needs_market_series or needs_shared_vix):
        defaults = feature_context.defaults
        shared_market_series = build_market_series_block(
            list(confirmed_bars),
            ema_pullback_len=defaults.ema.pullback,
            ema_trend_len=50,
            ema_magnet_len=defaults.ema.magnet,
            atr_len=defaults.atr_len,
            vwap_session=vwap_session,
            feature_context=feature_context,
        )
    if (
        confirmed_bars
        and needs_shared_vix
        and shared_vsa_facts
        and shared_market_series is not None
    ):
        shared_vix_context = vix_risk_context(
            confirmed_bars,
            profile=instrument_profile,
            features=features,
            atr_values=shared_market_series.atr_sma,
            rvol_values=[fact.rvol for fact in shared_vsa_facts],
        )
    shared_context = {
        "vsa_facts": shared_vsa_facts,
        "vsa_breakout_facts": shared_vsa_breakout_facts,
        "vix_context": shared_vix_context,
        "market_series": shared_market_series,
    }
    adapter_context = IndicatorRunContext(
        confirmed_bars=confirmed_bars,
        live_signal_bars=live_signal_bars,
        latest=latest,
        analysis_latest=analysis_latest,
        runtime_params=runtime_params,
        indicator_params=indicator_params,
        instrument_profile=instrument_profile,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        provider=provider,
        analysis_as_of_utc=analysis_as_of_utc,
        quality=quality,
        manual_channel_drawings=manual_channel_drawings,
        confirmed_slots=confirmed_slots,
        confirmed_bar_context=confirmed_bar_context or {},
        confirmed_bar_context_slots=confirmed_bar_context_slots or {},
        confirmed_bar_context_quality=(confirmed_bar_context_quality or {}),
        vwap_session=vwap_session,
        feature_context=feature_context,
        atr_value=atr_value,
        live_preview_active=(
            (latest.ts != analysis_latest.ts or not latest.closed)
            and live_bars_differ_from_confirmed(confirmed_bars, live_signal_bars)
        ),
        features=features,
        option_flow=option_flow,
        tick_flow=tick_flow,
        shared=shared_context,
        indicator_bundle={},
    )
    primary_specs = indicator_execution_specs(adapter_context, stage="primary")
    pipeline = execute_indicator_pipeline(
        primary_specs,
        runner=run_pipeline_indicator,
        promoter=lambda indicator_id, indicator: promote_pipeline_candidates(
            indicator_id,
            indicator,
            score_floor=indicator_candidate_score_floor(
                indicator_id,
                runtime_params,
                strong_watch_floor=strong_watch_floor,
                structure_floor=structure_floor,
                default_floor=structure_floor,
            ),
            atr_value=atr_value,
            features=features,
            score_overrides=score_overrides,
        ),
    )
    if timings is not None:
        timings.update(pipeline.timings_ms)
    indicators = pipeline.indicators
    candidates = pipeline.candidates
    candidates, vsa_fuel_sfp_cooldown = apply_vsa_fuel_sfp_cooldown(
        candidates,
        confirmed_bars,
        vsa_context=vsa_context,
        cooldown_bars=3,
    )

    return (
        indicators,
        candidates,
        vsa_context,
        vsa_fuel_sfp_cooldown,
        shared_context,
    )


def _calculate_structure_signal_indicators(
    *,
    inputs: AnalyzeInputBundle,
    indicator_bundle: dict[str, dict[str, Any]],
    shared: Mapping[str, Any],
    score_overrides: dict[str, Any],
    timings: dict[str, float] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[SignalCandidate]]:
    return run_structure_indicator_pipeline(
        inputs,
        indicator_bundle=indicator_bundle,
        shared=shared,
        score_overrides=score_overrides,
        timings=timings,
    )


def _setup_candidate_score_floor(
    indicator_id: str, runtime_params: IndicatorRuntimeParams, default_floor: float
) -> float:
    return indicator_candidate_score_floor(
        indicator_id,
        runtime_params,
        strong_watch_floor=default_floor,
        structure_floor=default_floor,
        default_floor=default_floor,
    )


def _calculate_setup_stage_indicators(
    *,
    confirmed_bars: Sequence[Bar],
    analysis_latest: Bar,
    analysis_as_of_utc: datetime,
    runtime_params: IndicatorRuntimeParams,
    indicator_params: dict[str, Any],
    features: dict[str, Any],
    indicator_bundle: dict[str, dict[str, Any]],
    shared: Mapping[str, Any],
    confirmed_bar_context: dict[str, list[Bar]],
    confirmed_bar_context_slots: dict[str, ProviderBarSlotSequence],
    confirmed_bar_context_quality: dict[str, dict[str, Any]],
    instrument_profile: InstrumentProfile,
    structure_floor: float,
    atr_value: float,
    score_overrides: dict[str, Any],
    timings: dict[str, float] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[SignalCandidate]]:
    adapter_context = IndicatorRunContext(
        confirmed_bars=confirmed_bars,
        live_signal_bars=confirmed_bars,
        latest=analysis_latest,
        analysis_latest=analysis_latest,
        runtime_params=runtime_params,
        indicator_params=indicator_params,
        instrument_profile=instrument_profile,
        analysis_as_of_utc=analysis_as_of_utc,
        features=features,
        atr_value=atr_value,
        indicator_bundle=indicator_bundle,
        shared=dict(shared),
        confirmed_bar_context=confirmed_bar_context,
        confirmed_bar_context_slots=confirmed_bar_context_slots,
        confirmed_bar_context_quality=confirmed_bar_context_quality,
    )
    pipeline = execute_indicator_pipeline(
        indicator_execution_specs(adapter_context, stage="setup"),
        runner=run_pipeline_indicator,
        promoter=lambda indicator_id, indicator: promote_pipeline_candidates(
            indicator_id,
            indicator,
            score_floor=_setup_candidate_score_floor(indicator_id, runtime_params, structure_floor),
            atr_value=atr_value,
            features=features,
            score_overrides=score_overrides,
        ),
    )
    if timings is not None:
        timings.update(pipeline.timings_ms)
    return pipeline.indicators, pipeline.candidates


def _calculate_post_decision_indicators(
    *,
    confirmed_bars: Sequence[Bar],
    analysis_latest: Bar,
    analysis_as_of_utc: datetime,
    decision: ScenarioDecision,
    decision_candidates: Sequence[SignalCandidate],
    features: dict[str, Any],
    indicator_bundle: dict[str, dict[str, Any]],
    instrument_profile: InstrumentProfile,
    quality: dict[str, Any],
    vsa_context: dict[str, Any],
    runtime_params: IndicatorRuntimeParams,
    indicator_params: dict[str, Any],
    option_targets: Sequence[dict[str, Any]] | None,
    confirmed_bar_context: dict[str, list[Bar]],
    confirmed_bar_context_slots: dict[str, ProviderBarSlotSequence],
    confirmed_bar_context_quality: dict[str, dict[str, Any]],
    atr_value: float,
    score_overrides: dict[str, Any],
    timings: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    adapter_context = IndicatorRunContext(
        confirmed_bars=confirmed_bars,
        live_signal_bars=confirmed_bars,
        latest=analysis_latest,
        analysis_latest=analysis_latest,
        runtime_params=runtime_params,
        indicator_params=indicator_params,
        instrument_profile=instrument_profile,
        analysis_as_of_utc=analysis_as_of_utc,
        features=features,
        atr_value=atr_value,
        indicator_bundle=indicator_bundle,
        decision=decision,
        decision_candidates=decision_candidates,
        quality=quality,
        option_targets=option_targets,
        confirmed_bar_context=confirmed_bar_context,
        confirmed_bar_context_slots=confirmed_bar_context_slots,
        confirmed_bar_context_quality=confirmed_bar_context_quality,
        shared={"vsa_volume": vsa_context},
    )
    pipeline = execute_indicator_pipeline(
        indicator_execution_specs(adapter_context, stage="post_decision"),
        runner=run_pipeline_indicator,
        promoter=lambda indicator_id, indicator: promote_pipeline_candidates(
            indicator_id,
            indicator,
            score_floor=0.0,
            atr_value=None,
            features=features,
            score_overrides=score_overrides,
        ),
    )
    if timings is not None:
        timings.update(pipeline.timings_ms)
    return pipeline.indicators
