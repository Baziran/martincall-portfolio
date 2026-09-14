from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from aef_terminal.domain import SignalCandidate
from aef_terminal.indicators.contracts import normalize_indicator_preview_result
from aef_terminal.indicators.runtime import IndicatorExecutionSpec


@dataclass(frozen=True)
class IndicatorPipelineResult:
    indicators: dict[str, dict[str, Any]]
    candidates: list[SignalCandidate]
    timings_ms: dict[str, float] = field(default_factory=dict)


IndicatorRunner = Callable[..., dict[str, Any]]
CandidatePromoter = Callable[[str, dict[str, Any]], list[SignalCandidate]]


def execute_indicator_spec(
    spec: IndicatorExecutionSpec,
    *,
    runner: IndicatorRunner,
    promoter: CandidatePromoter,
    context: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[SignalCandidate]]:
    dependency_context = context or {}
    if spec.calculate_with_context is not None:

        def calculate_base() -> dict[str, Any]:
            return spec.calculate_with_context(dependency_context)
    else:
        assert spec.calculate is not None
        calculate_base = spec.calculate

    def calculate_with_hooks() -> dict[str, Any]:
        indicator = calculate_base()
        if not isinstance(indicator, dict):
            raise TypeError(f"{spec.id} returned {type(indicator).__name__}, expected dict")
        if spec.postprocess is not None:
            indicator = spec.postprocess(indicator)
            if not isinstance(indicator, dict):
                raise TypeError(
                    f"{spec.id} postprocess returned {type(indicator).__name__}, expected dict"
                )
        if spec.preview_calculate_with_context is not None:

            def preview_calculate() -> dict[str, Any]:
                return spec.preview_calculate_with_context(dependency_context)
        else:
            preview_calculate = spec.preview_calculate
        if preview_calculate is not None:
            raw_preview = preview_calculate()
            preview = normalize_indicator_preview_result(
                raw_preview,
                name=spec.id,
                event_ts=spec.preview_event_ts,
            )
            if preview["latest"] is not None:
                indicator["preview"] = preview["latest"]
            indicator["preview_events"] = preview["events"]
        return indicator

    indicator = runner(
        spec.id,
        input_bars=spec.input_bars,
        analysis_bar=spec.analysis_bar,
        mode=spec.mode,
        calculate=calculate_with_hooks,
        runtime_params=spec.runtime_params,
        params=spec.params,
    )
    if not isinstance(indicator, dict):
        raise TypeError(f"{spec.id} runner returned {type(indicator).__name__}, expected dict")
    status = indicator.get("status")
    if isinstance(status, dict) and (
        status.get("state_code") == "error" or status.get("health") == "error"
    ):
        return indicator, []
    if not spec.promote:
        return indicator, []
    promoted = promoter(spec.id, indicator)
    if not isinstance(promoted, list) or any(
        not isinstance(candidate, SignalCandidate) for candidate in promoted
    ):
        raise TypeError(f"{spec.id} promoter must return list[SignalCandidate]")
    promotion = indicator.get("candidate_promotion")
    if isinstance(promotion, dict) and isinstance(indicator.get("status"), dict):
        indicator["status"]["candidate_promotion"] = promotion
    return indicator, promoted


def execute_indicator_pipeline(
    specs: Sequence[IndicatorExecutionSpec],
    *,
    runner: IndicatorRunner,
    promoter: CandidatePromoter,
) -> IndicatorPipelineResult:
    indicators: dict[str, dict[str, Any]] = {}
    candidates: list[SignalCandidate] = []
    timings_ms: dict[str, float] = {}
    for spec in specs:
        started = perf_counter()
        try:
            missing_deps = [
                dependency_id
                for dependency_id in spec.depends_on
                if dependency_id not in indicators
            ]
            if missing_deps:
                raise ValueError(
                    f"Indicator {spec.id} depends on missing pipeline results: "
                    f"{', '.join(missing_deps)}"
                )
            indicator, promoted = execute_indicator_spec(
                spec,
                runner=runner,
                promoter=promoter,
                context=indicators,
            )
        except Exception as exc:
            detail = f"{spec.id} pipeline hook failed: {type(exc).__name__}: {exc}"

            def raise_indicator_error(
                detail: str = detail,
            ) -> dict[str, Any]:
                raise ValueError(detail)

            indicator = runner(
                spec.id,
                input_bars=spec.input_bars,
                analysis_bar=spec.analysis_bar,
                mode=spec.mode,
                calculate=raise_indicator_error,
                runtime_params=spec.runtime_params,
                params=spec.params,
            )
            promoted = []
        indicators[spec.id] = indicator
        candidates.extend(promoted)
        timings_ms[spec.id] = round((perf_counter() - started) * 1000.0, 3)
    return IndicatorPipelineResult(
        indicators=indicators, candidates=candidates, timings_ms=timings_ms
    )
