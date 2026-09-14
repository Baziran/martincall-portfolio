from __future__ import annotations

from copy import deepcopy
from typing import Any

from aef_terminal.indicators.control_specs import IndicatorControlSpec
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    IndicatorGroup,
    IndicatorExecutionLane,
    IndicatorModule,
    IndicatorPipelineSpec,
    IndicatorPipelineStage,
    IndicatorSpec,
    normalize_runtime_payload_compact_contract,
    normalize_runtime_payload_contract,
    normalize_service_payload_contract,
)
from aef_terminal.indicators.module_discovery import discover_indicator_modules
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.indicators.scoring import (
    indicator_score_overrides,
    normalize_indicator_score_meta,
)


INDICATOR_MODULES: dict[str, IndicatorModule] = {
    module.id: module for module in discover_indicator_modules()
}

INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    indicator_id: module.spec for indicator_id, module in INDICATOR_MODULES.items()
}


def indicator_modules() -> tuple[IndicatorModule, ...]:
    return tuple(INDICATOR_MODULES.values())


def indicator_module(indicator_id: str) -> IndicatorModule:
    return INDICATOR_MODULES[indicator_id]


def indicator_ids(group: IndicatorGroup | None = None) -> tuple[str, ...]:
    return tuple(
        indicator_id
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if group is None or spec.group == group
    )


def indicator_stage_ids(stage: IndicatorPipelineStage) -> tuple[str, ...]:
    return tuple(
        indicator_id
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.pipeline_stage == stage
    )


def indicator_lane_ids(
    lane: IndicatorExecutionLane,
    *,
    stage: IndicatorPipelineStage | None = None,
) -> tuple[str, ...]:
    return tuple(
        indicator_id
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if lane in spec.execution_lanes and (stage is None or spec.pipeline_stage == stage)
    )


def indicator_ids_for_output_contract(output_contract: str) -> tuple[str, ...]:
    contract = str(output_contract or "").strip()
    if not contract:
        return ()
    return tuple(
        indicator_id
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.output_contract == contract
    )


def indicator_depends_on(indicator_id: str) -> tuple[str, ...]:
    spec = INDICATOR_REGISTRY.get(indicator_id)
    return spec.depends_on if spec is not None else ()


def indicator_required_dependencies(indicator_id: str) -> tuple[str, ...]:
    spec = INDICATOR_REGISTRY.get(indicator_id)
    return spec.required_dependencies if spec is not None else ()


def indicator_optional_context(indicator_id: str) -> tuple[str, ...]:
    spec = INDICATOR_REGISTRY.get(indicator_id)
    return spec.optional_context if spec is not None else ()


def indicator_shared_context_refs(indicator_id: str) -> tuple[str, ...]:
    spec = INDICATOR_REGISTRY.get(indicator_id)
    return spec.shared_context_refs if spec is not None else ()


def indicator_confirmed_bar_context(
    indicator_id: str,
) -> tuple[ConfirmedBarContextRequest, ...]:
    spec = INDICATOR_REGISTRY.get(indicator_id)
    return spec.confirmed_bar_context if spec is not None else ()


def enabled_confirmed_bar_context_requests(
    raw: dict[str, Any] | None,
) -> dict[str, tuple[tuple[str, ConfirmedBarContextRequest], ...]]:
    """Return declared confirmed-bar consumers enabled by current Calc state."""

    indicator_params = raw if isinstance(raw, dict) else {}
    grouped: dict[
        str,
        list[tuple[str, ConfirmedBarContextRequest]],
    ] = {}
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        if not indicator_enabled(indicator_params, indicator_id):
            continue
        for request in spec.confirmed_bar_context:
            grouped.setdefault(request.timeframe, []).append((indicator_id, request))
    return {timeframe: tuple(consumers) for timeframe, consumers in grouped.items()}


def indicator_ids_for_shared_context(
    context_ref: str,
    *,
    stage: IndicatorPipelineStage | None = None,
) -> tuple[str, ...]:
    key = str(context_ref or "").strip()
    if not key:
        return ()
    return tuple(
        indicator_id
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if key in spec.shared_context_refs and (stage is None or spec.pipeline_stage == stage)
    )


def indicator_empty_result(indicator_id: str) -> dict[str, object]:
    spec = INDICATOR_REGISTRY.get(indicator_id)
    if spec is None:
        return {"series": [], "events": [], "latest": None}
    return deepcopy(spec.empty_result)


def paper_tradable_indicator_ids() -> frozenset[str]:
    return frozenset(
        indicator_id for indicator_id, spec in INDICATOR_REGISTRY.items() if spec.paper_tradable
    )


def indicator_score_meta(
    indicator_id: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = INDICATOR_REGISTRY[indicator_id]
    return normalize_indicator_score_meta(
        indicator_id,
        seed_family=spec.score_family,
        seed_power=spec.empirical_power,
        seed_usefulness=spec.usefulness,
        overrides=overrides,
    )


def _control_manifest(control: IndicatorControlSpec) -> dict[str, Any]:
    return {
        "key": control.key,
        "label": control.label,
        "control_type": control.control_type,
        "default": control.default,
        "storage_key": control.storage_key,
        "element_id": control.element_id,
        "state_key": control.state_key,
        "api_key": control.api_key,
        "param_key": control.param_key,
        "scope": control.scope,
        "minimum": control.minimum,
        "maximum": control.maximum,
        "step": control.step,
        "options": list(control.options),
        "option_labels": list(control.option_labels),
        "action": control.action,
        "effect_ref": control.effect_ref,
        "compact": control.compact,
    }


def indicator_manifest(
    score_overrides: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        indicator_id: {
            "id": indicator_id,
            "label": spec.label,
            "calculates": spec.calculates,
            "ui_key": spec.ui_key,
            "group": spec.group,
            "pipeline_stage": spec.pipeline_stage,
            "pipeline_order": spec.pipeline_order,
            "execution_lanes": list(spec.execution_lanes),
            "fast_trigger_refs": list(spec.fast_trigger_refs),
            "analysis_refresh_interval_seconds": spec.analysis_refresh_interval_seconds,
            "snapshot_order": index,
            "calculate_ref": spec.calculate_ref,
            "adapter_ref": module.adapter_ref,
            "params_builder_ref": module.params_builder_ref,
            "candidate_promoter_ref": module.candidate_promoter_ref,
            "service_ref": module.service_ref,
            "extensions": {
                "snapshot_enricher_ref": module.snapshot_enricher_ref,
                "router_ref": module.router_ref,
                "ui_js_assets": list(module.ui_js_assets),
                "ui_css_assets": list(module.ui_css_assets),
                "sidebar": (
                    {
                        "id": module.sidebar.id,
                        "title": module.sidebar.title,
                        "mount_id": module.sidebar.mount_id,
                        "icon_svg": module.sidebar.icon_svg,
                        "order": module.sidebar.order,
                    }
                    if module.sidebar is not None
                    else None
                ),
            },
            "signal_source": spec.signal_source or spec.id,
            "signal_name_resolver_ref": spec.signal_name_resolver_ref,
            "candidate_promoter": spec.candidate_promoter,
            "candidate_score_floor_param": spec.candidate_score_floor_param,
            "candidate_score_floor_ref": spec.candidate_score_floor_ref,
            "candidate_score_floor_min": spec.candidate_score_floor_min,
            "min_target_atr": spec.min_target_atr,
            "renderer_kind": spec.renderer_kind,
            "renderer_ref": spec.renderer_ref
            or ("generic_overlay_renderer" if spec.renderer_kind == "generic" else ""),
            "renderer_contract": {
                "kind": spec.renderer_kind,
                "ref": spec.renderer_ref
                or ("generic_overlay_renderer" if spec.renderer_kind == "generic" else ""),
                "primitives": list(spec.renderer_primitives),
                "placements": list(spec.renderer_placements),
                "custom_requirements": list(spec.custom_renderer_requirements),
                "service_requirements": list(spec.service_renderer_requirements),
                "table": {
                    "label": spec.renderer_table_label,
                    "header_width": spec.renderer_table_header_width,
                    "icon": spec.renderer_table_icon,
                    "animation_ref": spec.renderer_table_animation_ref,
                },
            },
            "custom_renderer": spec.custom_renderer,
            "paper_tradable": spec.paper_tradable,
            "module_type": spec.module_type,
            "ui": {
                "state_key": spec.state_key,
                "settings_scope": spec.settings_scope,
                "calc_key": spec.calc_key,
                "visible_key": spec.visible_key,
                "chart_control_id": spec.chart_control_id,
                "process_control_id": spec.process_control_id,
                "process_effect_ref": spec.process_effect_ref,
                "derived_state_refs": list(spec.derived_state_refs),
                "runtime_state_ref": spec.runtime_state_ref,
                "settings_status_ref": spec.settings_status_ref,
                "table_setting_id": spec.table_setting_id,
                "api_enabled_key": spec.api_enabled_key,
                "api_visible_key": spec.api_visible_key,
                "manager_order": spec.manager_order,
                "runtime_order": spec.runtime_order,
                "default_calc": spec.default_calc,
                "default_visible": spec.default_visible,
                "show_in_manager": spec.show_in_manager,
                "show_in_runtime": spec.show_in_runtime,
            },
            "controls": [_control_manifest(control) for control in spec.controls],
            "depends_on": list(spec.depends_on),
            "required_dependencies": list(spec.required_dependencies),
            "optional_context": list(spec.optional_context),
            "shared_context_refs": list(spec.shared_context_refs),
            "confirmed_bar_context": [
                {
                    "timeframe": request.timeframe,
                    "history_bars": request.history_bars,
                    "role": request.role,
                }
                for request in spec.confirmed_bar_context
            ],
            "input_contract": spec.input_contract,
            "output_contract": spec.output_contract,
            "status_contract": spec.status_contract,
            "table_contract": spec.table_contract,
            "overlay_contract": spec.overlay_contract,
            "overlay_layer": spec.overlay_layer,
            "overlay_filter_ref": spec.overlay_filter_ref,
            "overlay_collect": spec.overlay_collect,
            "signal_contract": spec.signal_contract,
            "runtime_payload_contract": {
                key: list(value)
                for key, value in normalize_runtime_payload_contract(
                    spec.runtime_payload_contract,
                    spec.runtime_payload_schema_refs,
                ).items()
            },
            "runtime_payload_schema_refs": list(spec.runtime_payload_schema_refs),
            "runtime_payload_compact": {
                str(section): dict(policy)
                for section, policy in spec.runtime_payload_compact.items()
            },
            "runtime_payload_compact_contract": {
                key: list(value)
                for key, value in normalize_runtime_payload_compact_contract(
                    spec.runtime_payload_compact_contract,
                    spec.runtime_payload_schema_refs,
                ).items()
            },
            "service_payload_contract": {
                key: list(value)
                for key, value in normalize_service_payload_contract(
                    spec.service_payload_contract,
                    spec.service_payload_schema_refs,
                ).items()
            },
            "service_payload_schema_refs": list(spec.service_payload_schema_refs),
            **indicator_score_meta(indicator_id, score_overrides),
            "empty_result_keys": sorted(spec.empty_result.keys()),
            "notes": spec.notes,
        }
        for index, (indicator_id, module) in enumerate(INDICATOR_MODULES.items())
        for spec in (module.spec,)
    }


def _indicator_link_ids(manifest_item: dict[str, Any]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for dependency_id in (
        *(manifest_item.get("depends_on") or []),
        *(manifest_item.get("optional_context") or []),
    ):
        key = str(dependency_id or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return tuple(ordered)


def indicator_connection_manifest(
    indicator_params: dict[str, Any] | None = None,
    *,
    group: IndicatorGroup | None = None,
    stage: IndicatorPipelineStage | None = None,
) -> dict[str, dict[str, Any]]:
    raw = indicator_params if isinstance(indicator_params, dict) else {}
    score_overrides = indicator_score_overrides(raw)
    manifest = indicator_manifest(score_overrides)
    out: dict[str, dict[str, Any]] = {}
    source_ids = indicator_stage_ids(stage) if stage is not None else indicator_ids(group)
    for indicator_id in source_ids:
        enabled = indicator_enabled(raw, indicator_id)
        hard_dependency_ids = {
            *manifest[indicator_id]["depends_on"],
            *manifest[indicator_id]["required_dependencies"],
        }
        dependencies = [
            {
                "id": dependency_id,
                "enabled": (
                    dependency_id in INDICATOR_REGISTRY and indicator_enabled(raw, dependency_id)
                ),
                "registered": dependency_id in INDICATOR_REGISTRY,
                "required": dependency_id in hard_dependency_ids,
                "optional_context": dependency_id
                in set(manifest[indicator_id]["optional_context"]),
                "linked": (
                    dependency_id in INDICATOR_REGISTRY and indicator_enabled(raw, dependency_id)
                ),
            }
            for dependency_id in _indicator_link_ids(manifest[indicator_id])
        ]
        dependencies_ready = all(
            bool(item["registered"] and item["enabled"])
            for item in dependencies
            if item["required"]
        )
        connection = {
            "registry_id": indicator_id,
            "pipeline_stage": manifest[indicator_id]["pipeline_stage"],
            "input_contract": manifest[indicator_id]["input_contract"],
            "output_contract": manifest[indicator_id]["output_contract"],
            "depends_on": manifest[indicator_id]["depends_on"],
            "required_dependencies": manifest[indicator_id]["required_dependencies"],
            "optional_context": manifest[indicator_id]["optional_context"],
            "dependencies": dependencies,
            "dependencies_ready": dependencies_ready,
            "score_weight": manifest[indicator_id]["score_weight"],
            "score_source": manifest[indicator_id]["source"],
            "enabled": enabled,
        }
        if "sample_size" in manifest[indicator_id]:
            connection["score_sample_size"] = manifest[indicator_id]["sample_size"]
        out[indicator_id] = {
            **manifest[indicator_id],
            "enabled": enabled,
            "dependencies_ready": dependencies_ready,
            "connected": enabled and dependencies_ready,
            "connection": connection,
        }
    return out


def indicator_enabled(raw: dict[str, Any], indicator_id: str) -> bool:
    params = raw.get(indicator_id)
    spec = INDICATOR_REGISTRY.get(indicator_id)
    default = spec.default_calc if spec is not None else True
    if not isinstance(params, dict):
        return default
    if "enabled" in params:
        value = params.get("enabled")
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() not in {"0", "false", "no", "off"}
    return default


def indicator_pipeline_specs() -> dict[str, IndicatorPipelineSpec]:
    return {
        key: IndicatorPipelineSpec(
            id=spec.id,
            group=spec.group,
            pipeline_stage=spec.pipeline_stage,
            signal_source=spec.signal_source or spec.id,
            candidate_promoter=spec.candidate_promoter,
            min_target_atr=spec.min_target_atr,
            candidate_promoter_ref=INDICATOR_MODULES[key].candidate_promoter_ref,
            candidate_score_floor_param=spec.candidate_score_floor_param,
            candidate_score_floor_ref=spec.candidate_score_floor_ref,
            candidate_score_floor_min=spec.candidate_score_floor_min,
            custom_renderer=spec.custom_renderer,
        )
        for key, spec in INDICATOR_REGISTRY.items()
    }


def indicator_pipeline_spec(indicator_id: str) -> IndicatorPipelineSpec:
    return indicator_pipeline_specs()[indicator_id]


def resolve_indicator_signal_name(source: str, code: str, action: str) -> str:
    spec = INDICATOR_REGISTRY.get(source)
    if spec is None or not spec.signal_name_resolver_ref:
        return source
    resolver = resolve_ref(spec.signal_name_resolver_ref)
    return str(resolver(source, code, action) or source)
