from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aef_terminal.domain import SignalCandidate
from aef_terminal.engine.indicator_adapters import IndicatorRunContext, indicator_execution_specs
from aef_terminal.engine.indicator_pipeline import execute_indicator_pipeline
from aef_terminal.engine.indicator_policy import indicator_candidate_score_floor
from aef_terminal.indicators.scoring import indicator_score_overrides

from aef_terminal.engine.analyze.indicator_runtime import (
    promote_pipeline_candidates,
    run_pipeline_indicator,
)
from aef_terminal.engine.analyze.inputs import AnalyzeInputBundle


def structure_run_context_with_bundle(
    inputs: AnalyzeInputBundle,
    *,
    indicator_bundle: Mapping[str, dict[str, Any]],
    shared: Mapping[str, Any],
) -> IndicatorRunContext:
    structure_bars = inputs.structure_bars
    return IndicatorRunContext(
        confirmed_bars=structure_bars,
        live_signal_bars=structure_bars,
        latest=structure_bars[-1] if structure_bars else None,
        analysis_latest=structure_bars[-1] if structure_bars else None,
        runtime_params=inputs.runtime_params,
        indicator_params=inputs.indicator_params,
        instrument_profile=inputs.instrument_profile,
        analysis_as_of_utc=inputs.analysis_as_of_utc,
        vwap_session=inputs.vwap_session,
        features=inputs.features,
        structure_bars=structure_bars,
        structure_offset=inputs.structure_offset,
        security_context=inputs.security_context,
        security_context_quality=inputs.security_context_quality,
        confirmed_bar_context=inputs.confirmed_bar_context,
        confirmed_bar_context_slots=inputs.confirmed_bar_context_slots,
        confirmed_bar_context_quality=inputs.confirmed_bar_context_quality,
        line_width=float(inputs.indicator_params.get("line_width", 1.0)),
        show_geometry=bool(inputs.indicator_params.get("show_geometry", True)),
        feature_context=inputs.structure_feature_context,
        pivot_context=inputs.structure_feature_context.pivot_context,
        atr_value=inputs.atr_value,
        indicator_bundle=dict(indicator_bundle),
        tick_flow=inputs.tick_flow,
        shared=dict(shared),
    )


def run_structure_indicator_pipeline(
    inputs: AnalyzeInputBundle,
    *,
    indicator_bundle: Mapping[str, dict[str, Any]],
    shared: Mapping[str, Any],
    score_overrides: dict[str, Any] | None = None,
    timings: dict[str, float] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[SignalCandidate]]:
    specs = indicator_execution_specs(
        structure_run_context_with_bundle(
            inputs,
            indicator_bundle=indicator_bundle,
            shared=shared,
        ),
        stage="structure",
    )
    if not specs:
        return {}, []
    resolved_score_overrides = score_overrides or indicator_score_overrides(inputs.indicator_params)
    pipeline = execute_indicator_pipeline(
        specs,
        runner=run_pipeline_indicator,
        promoter=lambda indicator_id, indicator: promote_pipeline_candidates(
            indicator_id,
            indicator,
            score_floor=indicator_candidate_score_floor(
                indicator_id,
                inputs.runtime_params,
                strong_watch_floor=inputs.strong_watch_floor,
                structure_floor=inputs.structure_floor,
                default_floor=inputs.structure_floor,
            ),
            atr_value=inputs.atr_value,
            features=inputs.features,
            score_overrides=resolved_score_overrides,
        ),
    )
    if timings is not None:
        timings.update(pipeline.timings_ms)
    return pipeline.indicators, pipeline.candidates
