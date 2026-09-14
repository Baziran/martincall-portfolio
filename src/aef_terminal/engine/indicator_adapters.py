from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from aef_terminal.domain import Bar
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorModuleAdapter,
    IndicatorRunContext,
    calc_enabled,
)
from aef_terminal.indicators.registry import (
    IndicatorExecutionLane,
    IndicatorPipelineStage,
    indicator_depends_on,
    indicator_modules,
    indicator_optional_context,
    indicator_required_dependencies,
    indicator_lane_ids,
)


def indicator_adapters(
    stage: IndicatorPipelineStage,
    *,
    lane: IndicatorExecutionLane = "primary",
) -> dict[str, IndicatorModuleAdapter]:
    adapters: dict[str, IndicatorModuleAdapter] = {}
    for module in indicator_modules():
        if (
            module.spec.pipeline_stage != stage
            or lane not in module.spec.execution_lanes
            or not module.adapter_ref
        ):
            continue
        builder = resolve_ref(module.adapter_ref)
        adapters[module.id] = IndicatorModuleAdapter(id=module.id, stage=stage, build_spec=builder)
    return adapters


def _order_specs_by_dependencies(
    specs: list[IndicatorExecutionSpec],
) -> list[IndicatorExecutionSpec]:
    pending = {spec.id: spec for spec in specs}
    ordered: list[IndicatorExecutionSpec] = []
    while pending:
        ready = [
            spec_id
            for spec_id, spec in pending.items()
            if all(dep not in pending for dep in spec.depends_on)
        ]
        if not ready:
            unresolved = ", ".join(pending)
            raise ValueError(f"Indicator adapter dependencies cannot be resolved: {unresolved}")
        for spec_id in ready:
            ordered.append(pending.pop(spec_id))
    return ordered


def _dependency_ordered_specs(
    ctx: IndicatorRunContext,
    specs: list[IndicatorExecutionSpec],
) -> list[IndicatorExecutionSpec]:
    available = {spec.id for spec in specs}
    out: list[IndicatorExecutionSpec] = []
    for spec in specs:
        deps: list[str] = []
        for dep_id in (
            *indicator_depends_on(spec.id),
            *indicator_required_dependencies(spec.id),
            *indicator_optional_context(spec.id),
            *spec.depends_on,
        ):
            if dep_id not in available or dep_id in deps:
                continue
            if dep_id in spec.depends_on or calc_enabled(ctx, dep_id):
                deps.append(dep_id)
        out.append(replace(spec, depends_on=tuple(deps)))
    return _order_specs_by_dependencies(out)


def _indicator_error_spec(
    ctx: IndicatorRunContext,
    indicator_id: str,
    detail: str,
) -> IndicatorExecutionSpec:
    def raise_indicator_error() -> dict[str, object]:
        raise ValueError(f"{indicator_id}: {detail}")

    return IndicatorExecutionSpec(
        id=indicator_id,
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=raise_indicator_error,
        params=ctx.params(indicator_id),
        runtime_params=ctx.runtime_params,
        promote=False,
    )


def _validate_adapter_spec(
    ctx: IndicatorRunContext,
    indicator_id: str,
    spec: IndicatorExecutionSpec,
) -> IndicatorExecutionSpec:
    if not isinstance(spec, IndicatorExecutionSpec):
        raise TypeError(f"adapter returned {type(spec).__name__}, expected IndicatorExecutionSpec")
    if spec.id != indicator_id:
        raise ValueError(f"adapter returned mismatched id {spec.id!r}")
    if not isinstance(spec.input_bars, Sequence) or isinstance(spec.input_bars, (str, bytes)):
        raise TypeError("adapter input_bars must be a bar sequence")
    if spec.input_bars and (
        not isinstance(spec.input_bars[0], Bar) or not isinstance(spec.input_bars[-1], Bar)
    ):
        raise TypeError("adapter input_bars must contain Bar values")
    if spec.analysis_bar is not None and not isinstance(spec.analysis_bar, Bar):
        raise TypeError("adapter analysis_bar must be Bar or None")
    if not isinstance(spec.mode, str) or not spec.mode.strip():
        raise TypeError("adapter mode must be a non-empty string")
    if not isinstance(spec.promote, bool):
        raise TypeError("adapter promote must be bool")
    if not isinstance(spec.depends_on, tuple) or any(
        not isinstance(dependency_id, str) or not dependency_id.strip()
        for dependency_id in spec.depends_on
    ):
        raise TypeError("adapter depends_on must be a tuple of indicator ids")
    declared_dependencies = {
        *indicator_depends_on(indicator_id),
        *indicator_required_dependencies(indicator_id),
        *indicator_optional_context(indicator_id),
    }
    undeclared = sorted(set(spec.depends_on) - declared_dependencies)
    if indicator_id in spec.depends_on:
        raise ValueError("adapter cannot depend on itself")
    if undeclared:
        raise ValueError(f"adapter returned undeclared dependencies: {', '.join(undeclared)}")
    if spec.runtime_params is not ctx.runtime_params:
        raise ValueError("adapter must preserve the request runtime params")
    return spec


def indicator_execution_specs(
    ctx: IndicatorRunContext,
    *,
    stage: IndicatorPipelineStage,
    lane: IndicatorExecutionLane = "primary",
) -> list[IndicatorExecutionSpec]:
    adapters = (
        indicator_adapters(stage) if lane == "primary" else indicator_adapters(stage, lane=lane)
    )
    specs: list[IndicatorExecutionSpec] = []
    runtime_param_errors = ctx.runtime_params.errors
    for indicator_id in indicator_lane_ids(lane, stage=stage):
        if not calc_enabled(ctx, indicator_id):
            continue
        runtime_param_error = runtime_param_errors.get(indicator_id)
        if runtime_param_error:
            specs.append(
                _indicator_error_spec(
                    ctx,
                    indicator_id,
                    f"runtime params are invalid: {runtime_param_error}",
                )
            )
            continue
        adapter = adapters.get(indicator_id)
        if adapter is None:
            continue
        try:
            spec = adapter.build_spec(ctx)
            if spec is not None:
                spec = _validate_adapter_spec(ctx, indicator_id, spec)
        except Exception as exc:
            specs.append(
                _indicator_error_spec(
                    ctx,
                    indicator_id,
                    f"adapter failed: {type(exc).__name__}: {exc}",
                )
            )
            continue
        if spec is not None:
            specs.append(spec)
    return _dependency_ordered_specs(ctx, specs)
