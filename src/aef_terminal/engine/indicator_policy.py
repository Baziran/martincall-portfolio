from __future__ import annotations

from collections.abc import Mapping

from aef_terminal.indicators.registry import indicator_pipeline_spec
from aef_terminal.indicators.runtime import IndicatorRuntimeParams


def indicator_candidate_score_floor(
    indicator_id: str,
    runtime_params: IndicatorRuntimeParams,
    *,
    strong_watch_floor: float,
    structure_floor: float,
    default_floor: float,
) -> float:
    spec = indicator_pipeline_spec(indicator_id)
    ref = str(spec.candidate_score_floor_ref or "").strip()
    if ref == "zero":
        return 0.0
    if ref == "strong_watch_floor":
        return strong_watch_floor
    if ref == "structure_floor":
        return structure_floor
    if spec.candidate_score_floor_param:
        params = runtime_params.for_indicator(indicator_id)
        value = (
            params.get(spec.candidate_score_floor_param, default_floor)
            if isinstance(params, Mapping)
            else getattr(params, spec.candidate_score_floor_param, default_floor)
        )
        try:
            return max(
                float(spec.candidate_score_floor_min),
                min(float(value), 95.0),
            )
        except TypeError, ValueError:
            return default_floor
    return default_floor
