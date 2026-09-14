"""Internal validation and quarantine rules for startup-discovered indicator packages."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import signature
from math import isfinite
from pathlib import Path
from typing import Any

from aef_terminal.indicators.control_specs import IndicatorControlSpec
from aef_terminal.indicators.contracts import (
    normalize_indicator_result,
    validate_indicator_result_contract,
)
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    IndicatorModule,
    IndicatorSidebarSpec,
    RUNTIME_PAYLOAD_SCHEMA_FIELDS,
    SERVICE_PAYLOAD_SCHEMA_FIELDS,
    normalize_runtime_payload_compact_contract,
    normalize_runtime_payload_contract,
    validate_indicator_id,
)
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.runtime.stable_hash import canonical_hash_value
from aef_terminal.runtime.timeframes import CHART_HISTORY_INTERVALS


@dataclass
class _IndicatorQuarantine:
    reason: str
    details: set[str]


# section: one-package-contract-validation
def _module_contract_errors(module: IndicatorModule) -> tuple[str, ...]:
    spec = module.spec
    errors: list[str] = []
    if module.id != spec.id:
        errors.append(f"id_mismatch:{module.id}!={spec.id}")
    if not isinstance(spec.id, str) or not spec.id.strip():
        errors.append("missing_id")
    else:
        try:
            canonical_id = validate_indicator_id(spec.id)
        except ValueError:
            errors.append("invalid_id")
        else:
            if canonical_id != spec.id:
                errors.append("invalid_id")
    if not isinstance(spec.label, str) or not spec.label.strip():
        errors.append("missing_label")
    if not isinstance(spec.calculate_ref, str) or not spec.calculate_ref.strip():
        errors.append("missing_calculate_ref")
    if not isinstance(spec.ui_key, str) or not spec.ui_key.strip():
        errors.append("missing_ui_key")
    if not isinstance(spec.state_key, str) or not spec.state_key.strip():
        errors.append("missing_state_key")
    if not isinstance(spec.calc_key, str) or not spec.calc_key.strip():
        errors.append("missing_calc_key")
    if spec.pipeline_stage == "ui":
        if module.adapter_ref:
            errors.append("ui_module_has_adapter_ref")
    elif not str(module.adapter_ref or "").strip():
        errors.append("missing_adapter_ref")
    refresh_interval = spec.analysis_refresh_interval_seconds
    if refresh_interval is not None and (
        isinstance(refresh_interval, bool)
        or not isinstance(refresh_interval, (int, float))
        or not isfinite(float(refresh_interval))
        or float(refresh_interval) <= 0.0
    ):
        errors.append("invalid_analysis_refresh_interval_seconds")
    if not isinstance(spec.execution_lanes, tuple):
        errors.append("execution_lanes_not_tuple")
    else:
        if not spec.execution_lanes:
            errors.append("missing_execution_lane")
        if len(spec.execution_lanes) != len(set(spec.execution_lanes)):
            errors.append("duplicate_execution_lanes")
        if any(lane not in {"primary", "fast", "micro"} for lane in spec.execution_lanes):
            errors.append("invalid_execution_lane")
        if "primary" not in spec.execution_lanes and spec.pipeline_stage != "ui":
            errors.append("pipeline_indicator_missing_primary_lane")
    if not isinstance(spec.fast_trigger_refs, tuple):
        errors.append("fast_trigger_refs_not_tuple")
    else:
        if len(spec.fast_trigger_refs) != len(set(spec.fast_trigger_refs)):
            errors.append("duplicate_fast_trigger_refs")
        if any(ref not in {"option_target_sample"} for ref in spec.fast_trigger_refs):
            errors.append("invalid_fast_trigger_ref")
        if spec.fast_trigger_refs and "fast" not in spec.execution_lanes:
            errors.append("fast_trigger_without_fast_lane")
        if "fast" in spec.execution_lanes and not spec.fast_trigger_refs:
            errors.append("fast_lane_missing_trigger")
    if spec.renderer_kind not in {"generic", "custom", "service"}:
        errors.append("invalid_renderer_kind")
    if not isinstance(spec.renderer_primitives, tuple):
        errors.append("renderer_primitives_not_tuple")
    if not isinstance(spec.renderer_placements, tuple):
        errors.append("renderer_placements_not_tuple")
    custom_overlay_primitive = (
        isinstance(spec.renderer_primitives, tuple) and "custom" in spec.renderer_primitives
    )
    if custom_overlay_primitive:
        if spec.renderer_kind != "custom":
            errors.append("custom_overlay_requires_custom_renderer_kind")
        if not isinstance(spec.renderer_ref, str) or not spec.renderer_ref.strip():
            errors.append("missing_custom_overlay_renderer_ref")
        if not module.ui_js_assets:
            errors.append("custom_overlay_missing_ui_js_asset")
    if spec.renderer_kind == "custom" and not spec.custom_renderer_requirements:
        errors.append("missing_custom_renderer_requirements")
    if spec.renderer_kind == "service" and not spec.service_renderer_requirements:
        errors.append("missing_service_renderer_requirements")
    if spec.pipeline_stage == "ui" and spec.renderer_kind == "service":
        package_module = spec.calculate_ref.split(":", 1)[0]
        service_module = str(module.service_ref or "").split(":", 1)[0]
        terminal_core_service = "terminal_core_service" in spec.service_renderer_requirements
        if service_module:
            if terminal_core_service:
                errors.append("terminal_core_service_cannot_declare_package_service_ref")
            elif not (
                service_module == package_module or service_module.startswith(f"{package_module}.")
            ):
                errors.append("service_ref_outside_package")
        elif not terminal_core_service:
            errors.append("missing_service_ref")
        router_module = str(module.router_ref or "").split(":", 1)[0]
        if router_module and terminal_core_service:
            errors.append("terminal_core_service_cannot_declare_package_router_ref")
        if router_module and not (
            router_module == package_module or router_module.startswith(f"{package_module}.")
        ):
            errors.append("router_ref_outside_package")
    for field_name in (
        "adapter_ref",
        "params_builder_ref",
        "candidate_promoter_ref",
        "service_ref",
        "snapshot_enricher_ref",
        "router_ref",
    ):
        ref = str(getattr(module, field_name) or "")
        if ref and (ref.count(":") != 1 or not all(part.strip() for part in ref.split(":"))):
            errors.append(f"invalid_{field_name}")
    for field_name in ("calculate_ref", "signal_name_resolver_ref"):
        ref = str(getattr(spec, field_name) or "")
        if ref and (ref.count(":") != 1 or not all(part.strip() for part in ref.split(":"))):
            errors.append(f"invalid_{field_name}")

    resolved_targets: dict[str, Any] = {}
    python_refs = {
        "calculate_ref": spec.calculate_ref,
        "signal_name_resolver_ref": spec.signal_name_resolver_ref,
        "adapter_ref": module.adapter_ref,
        "params_builder_ref": module.params_builder_ref,
        "candidate_promoter_ref": module.candidate_promoter_ref,
        "service_ref": module.service_ref,
        "snapshot_enricher_ref": module.snapshot_enricher_ref,
        "router_ref": module.router_ref,
    }
    for field_name, raw_ref in python_refs.items():
        ref = str(raw_ref or "")
        if not ref or f"invalid_{field_name}" in errors:
            continue
        try:
            target = resolve_ref(ref)
        except Exception as exc:
            errors.append(f"unresolvable_{field_name}:{type(exc).__name__}")
            continue
        if not callable(target):
            errors.append(f"non_callable_{field_name}")
            continue
        resolved_targets[field_name] = target

    callable_contracts = (
        ("params_builder_ref", ({}, DEFAULT_INDICATOR_SETTINGS), {}),
        ("adapter_ref", (object(),), {}),
        ("service_ref", (object(),), {}),
        ("router_ref", (), {}),
        (
            "candidate_promoter_ref",
            ({},),
            {"score_floor": 0.0, "atr_value": None, "features": {}},
        ),
    )
    for field_name, positional, keywords in callable_contracts:
        target = resolved_targets.get(field_name)
        if target is None:
            continue
        try:
            signature(target).bind(*positional, **keywords)
        except TypeError, ValueError:
            errors.append(f"unsupported_{field_name}_signature")
            continue
        if field_name == "params_builder_ref":
            try:
                canonical_hash_value(target(*positional, **keywords))
            except Exception as exc:
                errors.append(f"invalid_params_builder_result:{type(exc).__name__}:{exc}")
    for field_name, suffix in (("ui_js_assets", ".js"), ("ui_css_assets", ".css")):
        assets = getattr(module, field_name)
        if not isinstance(assets, tuple):
            errors.append(f"{field_name}_not_tuple")
            continue
        for asset in assets:
            path = Path(str(asset))
            if not str(asset) or path.is_absolute() or ".." in path.parts or path.suffix != suffix:
                errors.append(f"invalid_{field_name}:{asset}")
    sidebar = module.sidebar
    if sidebar is not None:
        if not isinstance(sidebar, IndicatorSidebarSpec):
            errors.append("invalid_sidebar")
        else:
            if not sidebar.id or not sidebar.id.replace("-", "_").isidentifier():
                errors.append("invalid_sidebar_id")
            if not sidebar.title.strip():
                errors.append("missing_sidebar_title")
            if not sidebar.mount_id.strip():
                errors.append("missing_sidebar_mount_id")
    for schema_ref in spec.runtime_payload_schema_refs:
        if schema_ref not in RUNTIME_PAYLOAD_SCHEMA_FIELDS:
            errors.append(f"unknown_runtime_payload_schema_ref:{schema_ref}")
    for schema_ref in spec.service_payload_schema_refs:
        if schema_ref not in SERVICE_PAYLOAD_SCHEMA_FIELDS:
            errors.append(f"unknown_service_payload_schema_ref:{schema_ref}")
    for field_name in ("depends_on", "required_dependencies", "optional_context"):
        values = getattr(spec, field_name)
        if not isinstance(values, tuple):
            errors.append(f"{field_name}_not_tuple")
            continue
        if spec.id in values:
            errors.append(f"self_{field_name}:{spec.id}")
        if len(values) != len(set(values)):
            errors.append(f"duplicate_{field_name}")
    if isinstance(spec.required_dependencies, tuple) and isinstance(
        spec.depends_on,
        tuple,
    ):
        undeclared_required = set(spec.required_dependencies) - set(spec.depends_on)
        if undeclared_required:
            errors.append(
                f"required_dependencies_not_in_depends_on:{','.join(sorted(undeclared_required))}"
            )
    if not isinstance(spec.confirmed_bar_context, tuple):
        errors.append("confirmed_bar_context_not_tuple")
    else:
        requested_timeframes: list[str] = []
        for request in spec.confirmed_bar_context:
            if not isinstance(request, ConfirmedBarContextRequest):
                errors.append("invalid_confirmed_bar_context_request")
                continue
            timeframe = str(request.timeframe or "").strip().lower()
            requested_timeframes.append(timeframe)
            if timeframe not in CHART_HISTORY_INTERVALS:
                errors.append(f"unsupported_confirmed_bar_context_timeframe:{timeframe}")
            if (
                isinstance(request.history_bars, bool)
                or not isinstance(request.history_bars, int)
                or not 32 <= request.history_bars <= 4096
            ):
                errors.append(f"invalid_confirmed_bar_context_history_bars:{timeframe}")
            if request.role not in {
                "independent_scan",
                "lower_timeframe_confirmation",
            }:
                errors.append(f"invalid_confirmed_bar_context_role:{request.role}")
        if len(requested_timeframes) != len(set(requested_timeframes)):
            errors.append("duplicate_confirmed_bar_context_timeframe")
    if not isinstance(spec.controls, tuple):
        errors.append("controls_not_tuple")
    else:
        controls = [
            control for control in spec.controls if isinstance(control, IndicatorControlSpec)
        ]
        if len(controls) != len(spec.controls):
            errors.append("invalid_control")
        control_keys = [control.key for control in controls]
        if len(control_keys) != len(set(control_keys)):
            errors.append("duplicate_control_key")
    runtime_contract = normalize_runtime_payload_contract(
        spec.runtime_payload_contract,
        spec.runtime_payload_schema_refs,
    )
    explicit_compact_contract = spec.runtime_payload_compact_contract
    if not isinstance(explicit_compact_contract, dict):
        errors.append("runtime_payload_compact_contract_not_dict")
        explicit_compact_contract = {}
    for section, fields in explicit_compact_contract.items():
        section_key = str(section)
        if not isinstance(section, str) or not section.strip():
            errors.append(f"invalid_runtime_payload_compact_contract_section:{section_key}")
            continue
        if not isinstance(fields, tuple):
            errors.append(f"runtime_payload_compact_contract_fields_not_tuple:{section_key}")
            continue
        normalized_fields = tuple(
            field for field in fields if isinstance(field, str) and field.strip()
        )
        if len(normalized_fields) != len(fields):
            errors.append(f"invalid_runtime_payload_compact_contract_field:{section_key}")
            continue
        if not normalized_fields:
            errors.append(f"empty_runtime_payload_compact_contract_fields:{section_key}")
            continue
        runtime_fields = runtime_contract.get(section_key)
        if runtime_fields is None:
            errors.append(f"unknown_runtime_payload_compact_contract_section:{section_key}")
            continue
        undeclared_fields = set(normalized_fields) - set(runtime_fields)
        if undeclared_fields:
            errors.append(
                "runtime_payload_compact_contract_fields_not_in_runtime_contract:"
                f"{section_key}:{','.join(sorted(undeclared_fields))}"
            )
    compact_contract = normalize_runtime_payload_compact_contract(
        explicit_compact_contract,
        spec.runtime_payload_schema_refs,
    )
    for section, policy in spec.runtime_payload_compact.items():
        if not isinstance(policy, dict):
            errors.append(f"invalid_runtime_payload_compact_policy:{section}")
            continue
        fields = policy.get("fields")
        if fields == "runtime_contract" and section not in runtime_contract:
            errors.append(f"unknown_runtime_payload_compact_contract_section:{section}")
        if fields == "compact_contract" and not compact_contract.get(section):
            errors.append(f"unknown_runtime_payload_compact_schema_section:{section}")
        if isinstance(fields, str) and fields not in {"runtime_contract", "compact_contract"}:
            errors.append(f"unknown_runtime_payload_compact_fields_ref:{section}:{fields}")
    try:
        empty_result = normalize_indicator_result(spec.empty_result, name=spec.id)
        empty_result["status"] = {
            "id": spec.id,
            "state_code": "stale",
            "health": "stale",
            "reason_code": "no_input_bars",
            "trigger_event": {"code": "indicator_input_missing"},
            "mode": "confirmed",
            "bar_count": 0,
            "calculated_at": "startup-validation",
        }
        validate_indicator_result_contract(
            empty_result,
            name=spec.id,
            renderer_contract={
                "ref": spec.renderer_ref,
                "primitives": list(spec.renderer_primitives),
                "placements": list(spec.renderer_placements),
            },
            runtime_payload_contract={key: list(value) for key, value in runtime_contract.items()},
        )
    except Exception as exc:
        errors.append(f"invalid_empty_result:{type(exc).__name__}:{exc}")
    return tuple(errors)


def _record_quarantine(
    quarantined: dict[str, _IndicatorQuarantine],
    package_name: str,
    reason: str,
    detail: str,
) -> None:
    current = quarantined.get(package_name)
    if current is None:
        quarantined[package_name] = _IndicatorQuarantine(
            reason=reason,
            details={detail} if detail else set(),
        )
        return
    if detail:
        current.details.add(detail)


# section: cross-package-dependencies-and-quarantine
def _same_stage_dependency_cycles(
    modules: tuple[IndicatorModule, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    module_ids = {module.id for module in modules}
    stage_by_id = {module.id: module.spec.pipeline_stage for module in modules}
    by_stage: dict[str, dict[str, set[str]]] = {}
    for module in modules:
        spec = module.spec
        by_stage.setdefault(spec.pipeline_stage, {})[spec.id] = {
            dependency_id
            for dependency_id in (
                *spec.depends_on,
                *spec.required_dependencies,
                *spec.optional_context,
            )
            if dependency_id in module_ids and stage_by_id[dependency_id] == spec.pipeline_stage
        }

    cycles: set[tuple[str, tuple[str, ...]]] = set()
    for stage, graph in by_stage.items():
        reachable = {
            indicator_id: set(dependencies) for indicator_id, dependencies in graph.items()
        }
        changed = True
        while changed:
            changed = False
            for indicator_id, reached in reachable.items():
                expanded = set(reached)
                for dependency_id in tuple(reached):
                    expanded.update(reachable.get(dependency_id, ()))
                if expanded != reached:
                    reachable[indicator_id] = expanded
                    changed = True
        for indicator_id, reached in reachable.items():
            cycle = tuple(
                sorted(
                    other_id
                    for other_id in reached
                    if indicator_id in reachable.get(other_id, set())
                )
            )
            if cycle:
                cycles.add((stage, cycle))
    return tuple(sorted(cycles))


def _validate_discovered_modules(
    modules_by_package: dict[str, tuple[IndicatorModule, ...]],
) -> dict[str, _IndicatorQuarantine]:
    quarantined: dict[str, _IndicatorQuarantine] = {}

    for package_name, modules in modules_by_package.items():
        detail = _module_contract_detail(modules)
        if detail:
            _record_quarantine(
                quarantined,
                package_name,
                "invalid_contract",
                detail,
            )

    id_claims: dict[str, list[str]] = {}
    for package_name, modules in modules_by_package.items():
        if package_name in quarantined:
            continue
        for module in modules:
            id_claims.setdefault(module.id, []).append(package_name)
    for indicator_id, claimants in id_claims.items():
        if len(claimants) < 2:
            continue
        owners = ",".join(sorted(set(claimants)))
        detail = f"duplicate_indicator_id:{indicator_id}:{owners}"
        for package_name in set(claimants):
            _record_quarantine(
                quarantined,
                package_name,
                "duplicate_indicator_id",
                detail,
            )

    dom_claims: dict[str, list[tuple[str, str, str]]] = {}
    state_claims: dict[str, list[tuple[str, str]]] = {}
    sidebar_claims: dict[str, list[tuple[str, str]]] = {}
    custom_renderer_claims: dict[str, list[tuple[str, str]]] = {}
    for package_name, modules in modules_by_package.items():
        if package_name in quarantined:
            continue
        for module in modules:
            spec = module.spec
            state_key = str(spec.state_key or "").strip()
            if state_key:
                state_claims.setdefault(state_key, []).append((package_name, spec.id))
            if isinstance(spec.renderer_primitives, tuple) and "custom" in spec.renderer_primitives:
                renderer_ref = str(spec.renderer_ref or "").strip()
                if renderer_ref:
                    custom_renderer_claims.setdefault(renderer_ref, []).append(
                        (package_name, spec.id)
                    )
            for field_name, value in (
                ("chart_control_id", spec.chart_control_id),
                ("process_control_id", spec.process_control_id),
            ):
                dom_id = str(value or "").strip()
                if dom_id:
                    dom_claims.setdefault(dom_id, []).append((package_name, spec.id, field_name))
            if module.sidebar is not None:
                sidebar_id = str(module.sidebar.id or "").strip()
                if sidebar_id:
                    sidebar_claims.setdefault(sidebar_id, []).append((package_name, spec.id))
                mount_id = str(module.sidebar.mount_id or "").strip()
                if mount_id:
                    dom_claims.setdefault(mount_id, []).append(
                        (package_name, spec.id, "sidebar_mount_id")
                    )
            for control in spec.controls:
                element_id = str(control.element_id or "").strip()
                if element_id:
                    dom_claims.setdefault(element_id, []).append(
                        (package_name, spec.id, f"control:{control.key}")
                    )

    for dom_id, claims in dom_claims.items():
        allowed_shared_toggle = (
            len(claims) == 2
            and claims[0][:2] == claims[1][:2]
            and {claims[0][2], claims[1][2]} == {"chart_control_id", "process_control_id"}
        )
        if allowed_shared_toggle or len(claims) < 2:
            continue
        owners = ",".join(
            f"{indicator_id}.{field_name}" for _, indicator_id, field_name in sorted(claims)
        )
        detail = f"duplicate_dom_id:{dom_id}:{owners}"
        for package_name, _, _ in claims:
            _record_quarantine(
                quarantined,
                package_name,
                "duplicate_dom_id",
                detail,
            )

    for field_name, claims_by_key in (
        ("state_key", state_claims),
        ("sidebar_id", sidebar_claims),
        ("custom_renderer_ref", custom_renderer_claims),
    ):
        for key, claims in claims_by_key.items():
            if len(claims) < 2:
                continue
            owners = ",".join(indicator_id for _, indicator_id in sorted(claims))
            detail = f"duplicate_{field_name}:{key}:{owners}"
            for package_name, _ in claims:
                _record_quarantine(
                    quarantined,
                    package_name,
                    f"duplicate_{field_name}",
                    detail,
                )

    while True:
        active_modules = tuple(
            module
            for package_name, modules in modules_by_package.items()
            if package_name not in quarantined
            for module in modules
        )
        active_ids = {module.id for module in active_modules}
        missing_dependency_packages: set[str] = set()
        for package_name, modules in modules_by_package.items():
            if package_name in quarantined:
                continue
            for module in modules:
                missing = sorted(
                    {
                        dependency_id
                        for dependency_id in (
                            *module.spec.depends_on,
                            *module.spec.required_dependencies,
                        )
                        if dependency_id not in active_ids
                    }
                )
                for dependency_id in missing:
                    _record_quarantine(
                        quarantined,
                        package_name,
                        "unknown_dependency",
                        f"{module.id}:unknown_dependency:{dependency_id}",
                    )
                    missing_dependency_packages.add(package_name)
        if missing_dependency_packages:
            continue

        cycles = _same_stage_dependency_cycles(active_modules)
        if not cycles:
            break
        package_by_indicator = {
            module.id: package_name
            for package_name, modules in modules_by_package.items()
            if package_name not in quarantined
            for module in modules
        }
        for stage, indicator_ids in cycles:
            detail = f"same_stage_dependency_cycle:{stage}:{','.join(indicator_ids)}"
            for indicator_id in indicator_ids:
                package_name = package_by_indicator[indicator_id]
                _record_quarantine(
                    quarantined,
                    package_name,
                    "dependency_cycle",
                    detail,
                )

    return quarantined


def _module_contract_detail(modules: tuple[IndicatorModule, ...]) -> str:
    details: list[str] = []
    for module in modules:
        try:
            errors = _module_contract_errors(module)
        except Exception as exc:  # pragma: no cover - malformed third-party contract.
            errors = (f"contract_validation_error:{type(exc).__name__}:{exc}",)
        if errors:
            details.append(f"{module.id}: {', '.join(errors)}")
    return "; ".join(details)
