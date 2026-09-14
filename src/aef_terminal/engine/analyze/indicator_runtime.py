from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from aef_terminal.domain import Bar, SignalCandidate
from aef_terminal.indicators.registry import indicator_pipeline_spec, indicator_score_meta
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.indicators.runtime import IndicatorRuntimeParams
from aef_terminal.signals.indicator_state import signal_candidates_from_indicator_state
from aef_terminal.engine.analyze.indicator_status import (
    _params_hash,
    attach_indicator_status,
    empty_indicator_result,
)


def _attach_indicator_score_meta(
    indicator_id: str,
    candidates: list[SignalCandidate],
    score_overrides: dict[str, Any] | None = None,
) -> list[SignalCandidate]:
    if not candidates:
        return candidates
    score_meta = indicator_score_meta(indicator_id, score_overrides)
    return [
        replace(
            candidate,
            details={
                **candidate.details,
                "indicator_score_meta": score_meta,
                "indicator_score_weight": score_meta["score_weight"],
            },
        )
        for candidate in candidates
    ]


def run_indicator(
    name: str,
    *,
    input_bars: Sequence[Bar],
    analysis_bar: Bar | None,
    mode: str,
    calculate: Any,
    params: Any = None,
) -> dict[str, Any]:
    started_at = datetime.now(tz=UTC)
    params_hash: str | None = None
    try:
        params_hash = _params_hash(params)
        result = calculate()
        if not isinstance(result, dict):
            raise TypeError(f"{name} returned {type(result).__name__}, expected dict")
        return attach_indicator_status(
            name,
            result,
            input_bars=input_bars,
            analysis_bar=analysis_bar,
            mode=mode,
            started_at=started_at,
            params_hash=params_hash,
        )
    except Exception as exc:
        return attach_indicator_status(
            name,
            empty_indicator_result(name),
            input_bars=input_bars,
            analysis_bar=analysis_bar,
            mode=mode,
            started_at=started_at,
            params_hash=params_hash,
            error=exc,
        )


def run_pipeline_indicator(
    indicator_id: str,
    *,
    input_bars: Sequence[Bar],
    analysis_bar: Bar | None,
    mode: str,
    calculate: Any,
    runtime_params: IndicatorRuntimeParams,
    params: Any = None,
) -> dict[str, Any]:
    if not isinstance(runtime_params, IndicatorRuntimeParams):
        raise TypeError("pipeline runtime_params must be IndicatorRuntimeParams")
    if runtime_params.enabled.get(indicator_id) is not True:
        raise ValueError(f"{indicator_id} must be Calc-enabled in runtime_params")
    resolved_params = params
    if resolved_params is None:
        resolved_params = runtime_params.for_indicator(indicator_id)
    return run_indicator(
        indicator_id,
        input_bars=input_bars,
        analysis_bar=analysis_bar,
        mode=mode,
        calculate=calculate,
        params=resolved_params,
    )


def promote_pipeline_candidates(
    indicator_id: str,
    indicator: dict[str, Any],
    *,
    score_floor: float,
    atr_value: float | None,
    features: dict[str, Any] | None = None,
    score_overrides: dict[str, Any] | None = None,
) -> list[SignalCandidate]:
    spec = indicator_pipeline_spec(indicator_id)
    score_meta = indicator_score_meta(indicator_id, score_overrides)
    if spec.candidate_promoter_ref:
        promoter = resolve_ref(spec.candidate_promoter_ref)
        promoted = promoter(
            indicator,
            score_floor=score_floor,
            atr_value=atr_value,
            features=features,
        )
        return _attach_indicator_score_meta(indicator_id, promoted, score_overrides)
    if spec.candidate_promoter == "none":
        indicator["candidate_promotion"] = {
            "source": indicator_id,
            "promoted": False,
            "reject_reason": "promoter_disabled",
            "action_source": spec.candidate_promoter,
            "indicator_score_meta": score_meta,
        }
        return []
    return _attach_indicator_score_meta(
        indicator_id,
        signal_candidates_from_indicator_state(
            indicator,
            source=spec.signal_source,
            score_floor=score_floor,
            atr_value=atr_value,
            min_target_atr=spec.min_target_atr,
            features=features,
        ),
        score_overrides,
    )
