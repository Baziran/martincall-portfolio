import ast
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from importlib import import_module
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from functools import partial
from unittest.mock import Mock

import pytest

from aef_terminal.indicators import contracts as indicator_contracts
from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
    StrategyMode,
)
from aef_terminal.engine.analyze import (
    analyze_bars as _analyze_bars,
    attach_indicator_status,
    empty_indicator_result,
)
from aef_terminal.engine.analyze import pipeline as analyze_pipeline
from aef_terminal.engine import context_cache as context_cache_module
from aef_terminal.engine.context_cache import (
    clear_analysis_context_cache,
    get_cached_analysis_context as _get_cached_analysis_context,
)
from aef_terminal.engine.analyze.inputs import build_analyze_inputs
from aef_terminal.engine.indicator_adapters import (
    IndicatorRunContext as _IndicatorRunContext,
    indicator_adapters,
    indicator_execution_specs,
)
from aef_terminal.engine.analyze.indicator_runtime import promote_pipeline_candidates, run_indicator
from aef_terminal.engine.analyze.indicator_runtime import run_pipeline_indicator
from aef_terminal.engine.decision import DecisionContext
from aef_terminal.engine.indicator_policy import indicator_candidate_score_floor
from aef_terminal.engine.trade_setup import TRADE_SETUP_AUTHORITY_CONTRACT
from aef_terminal.engine.vsa_context import (
    VsaVolumeParams,
    build_vsa_volume_context,
)
from aef_terminal.indicators.runtime_params import build_indicator_runtime_params
from aef_terminal.engine.indicator_pipeline import (
    execute_indicator_pipeline,
    execute_indicator_spec,
)
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorModuleAdapter,
    IndicatorRuntimeParams,
    calc_enabled,
)
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.engine.serialization import (
    serialize_bars,
    serialize_candidate,
)
from aef_terminal.features.chart_guides import build_chart_guides
from aef_terminal.features.context import build_feature_context as _build_feature_context
from aef_terminal.features.vsa import vsa_facts
from aef_terminal.indicators.contracts import (
    INDICATOR_CONTRACT_VERSION,
    OVERLAY_CONTRACT_VERSION,
    normalize_indicator_result,
    validate_indicator_result_contract,
)
from aef_terminal.indicators.domain_facts import indicator_fact_payload, metric_number
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    indicator_defaults_from_params,
    label_importance_floor,
)
from aef_terminal.indicators.registry import (
    INDICATOR_REGISTRY,
    indicator_connection_manifest,
    enabled_confirmed_bar_context_requests,
    indicator_empty_result,
    indicator_ids,
    indicator_ids_for_output_contract,
    indicator_ids_for_shared_context,
    indicator_manifest,
    indicator_modules,
    indicator_pipeline_specs,
    indicator_score_meta,
    indicator_stage_ids,
    paper_tradable_indicator_ids,
    resolve_indicator_signal_name,
)
from aef_terminal.indicators.scoring import normalize_indicator_score_meta
from aef_terminal.indicators.module_discovery import (
    discover_indicator_modules,
    indicator_module_asset_paths,
    indicator_module_catalog,
)
from aef_terminal.indicators.module_contract import (
    OVERLAY_COMPACT_FIELDS,
    OVERLAY_RUNTIME_FIELDS,
    RUNTIME_PAYLOAD_SCHEMA_FIELDS,
    normalize_runtime_payload_compact_contract,
    normalize_runtime_payload_contract,
    normalize_service_payload_contract,
)
from aef_terminal.indicators.scaffold import indicator_module_template, validate_indicator_id
from aef_terminal.indicators.settings_schema import global_settings_schema
from aef_terminal.indicators.modules.martin_carlo import MartinCarloParams, martin_carlo
from aef_terminal.indicators.modules.trade_setup_engine import (
    INDICATOR_MODULE as TRADE_SETUP_ENGINE_MODULE,
    TRADE_SETUP_ENGINE_EVENT_FIELDS,
)
from aef_terminal.indicators.modules.wolfe_structure import wolfe_structure
from aef_terminal.runtime.signal_state import DEFAULT_LIFECYCLE_LABELS
from aef_terminal.signals.quality import apply_global_min_rr_gate
from aef_terminal.ui.market_indicator_params import (
    market_indicator_params,
    server_alert_indicator_params,
)
from aef_terminal.runtime.instruments import PROFILES
from aef_terminal.runtime.stable_hash import stable_hash
from tests.provider_payloads import (
    explicit_vwap_session_for_bars,
    ibkr_stock_payload,
    instrument_with_bar_sessions,
)


ES_PROFILE = PROFILES["ES"]
build_feature_context = partial(_build_feature_context, instrument_profile=ES_PROFILE)


def _runtime_params(
    *,
    enabled: dict[str, bool] | None = None,
    by_indicator: dict[str, object] | None = None,
    errors: dict[str, str] | None = None,
) -> IndicatorRuntimeParams:
    enabled_values = dict(enabled or {})
    parameter_values = {indicator_id: {} for indicator_id in enabled_values} | dict(
        by_indicator or {}
    )
    return IndicatorRuntimeParams(
        enabled=enabled_values,
        by_indicator=parameter_values,
        errors=dict(errors or {}),
        strategy_mode=StrategyMode.BALANCED,
    )


def IndicatorRunContext(*args, **kwargs):
    kwargs.setdefault("instrument_profile", ES_PROFILE)
    confirmed_bars = kwargs.get("confirmed_bars") or (args[0] if args else [])
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(confirmed_bars))
    return _IndicatorRunContext(*args, **kwargs)


def get_cached_analysis_context(**kwargs):
    kwargs.setdefault("instrument_profile", ES_PROFILE)
    kwargs.setdefault("instrument_id", "ibkr|contract|756733")
    kwargs.setdefault("route_fingerprint", "ibkr|contract|756733")
    return _get_cached_analysis_context(**kwargs)


def _bars(count: int = 40) -> list[Bar]:
    base = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    return [
        Bar(
            "SPY",
            base + timedelta(minutes=index),
            100 + index * 0.1,
            101 + index * 0.1,
            99 + index * 0.1,
            100.5 + index * 0.1,
            1000 + index,
            "1m",
        )
        for index in range(count)
    ]


def analyze_bars(bars, *args, **kwargs):
    kwargs.setdefault(
        "instrument",
        instrument_with_bar_sessions(ibkr_stock_payload("SPY"), bars),
    )
    return _analyze_bars(bars, *args, **kwargs)


def _validate_registered_indicator_result(indicator_id: str, result: dict[str, object]) -> None:
    manifest = indicator_manifest()[indicator_id]
    validate_indicator_result_contract(
        result,
        name=indicator_id,
        renderer_contract=manifest["renderer_contract"],
        runtime_payload_contract=manifest["runtime_payload_contract"],
    )


def test_indicator_result_contract_normalizes_optional_fields() -> None:
    result = normalize_indicator_result({"latest": {"state": "WAIT"}}, name="demo")

    assert result["contract"] == INDICATOR_CONTRACT_VERSION
    assert result["id"] == "demo"
    assert result["series"] == []
    assert result["events"] == []
    assert result["overlays"] == []
    assert result["signals"] == []


def test_runtime_payload_contract_does_not_repeat_fact_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars(2)
    result = attach_indicator_status(
        "demo",
        normalize_indicator_result(
            {
                "series": [{"ts": bars[-1].ts.isoformat(), "state": "OK"}],
                "events": [{"ts": bars[-1].ts.isoformat(), "code": "OK"}],
                "overlays": [
                    {
                        "type": "marker",
                        "ts": bars[-1].ts.isoformat(),
                        "price": bars[-1].close,
                        "code": "OK",
                    }
                ],
                "latest": {"state": "OK"},
                "signals": [{"code": "OK"}],
            },
            name="demo",
        ),
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )
    fact_validator = Mock(wraps=indicator_contracts._validate_fact_contract)
    monkeypatch.setattr(indicator_contracts, "_validate_fact_contract", fact_validator)

    validate_indicator_result_contract(
        result,
        name="demo",
        runtime_payload_contract=normalize_runtime_payload_contract(
            {
                "latest": ("state",),
                "series": ("ts", "state"),
                "events": ("ts", "code"),
                "overlays": ("type", "ts", "price", "code", "contract", "source"),
                "signals": ("source", "code", "details", "overlays"),
            }
        ),
    )

    assert [call.kwargs["section"] for call in fact_validator.call_args_list] == [
        "latest",
        "series item",
        "events item",
        "overlay",
        "signal",
    ]


def test_production_code_does_not_import_prototypes() -> None:
    production_root = Path("src/aef_terminal")
    violations: list[str] = []
    for path in production_root.rglob("*.py"):
        if "__pycache__" in path.parts or "prototypes" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "aef_terminal.prototypes" or module.startswith(
                    "aef_terminal.prototypes."
                ):
                    violations.append(f"{path}:{node.lineno}:{module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name == "aef_terminal.prototypes" or name.startswith(
                        "aef_terminal.prototypes."
                    ):
                        violations.append(f"{path}:{node.lineno}:{name}")

    assert violations == []


def test_domain_and_runtime_layers_do_not_import_ui_application_layer() -> None:
    roots = (
        "alerts",
        "data",
        "engine",
        "features",
        "indicators",
        "runtime",
        "signals",
        "storage",
    )
    service_adapter_modules = {
        ref.split(":", 1)[0]
        for module in indicator_modules()
        if module.spec.pipeline_stage == "ui" and module.spec.renderer_kind == "service"
        for ref in (module.service_ref, module.router_ref)
        if ref
    }
    violations: list[str] = []
    for root in roots:
        for path in (Path("src/aef_terminal") / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            package_module = ".".join(path.with_suffix("").parts[1:])
            if path.name == "__init__.py":
                package_module = ".".join(path.parent.parts[1:])
            declared_service_adapter = (
                root == "indicators" and package_module in service_adapter_modules
            )
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if not declared_service_adapter and (
                        module == "aef_terminal.ui" or module.startswith("aef_terminal.ui.")
                    ):
                        violations.append(f"{path}:{node.lineno}:{module}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.name
                        if not declared_service_adapter and (
                            name == "aef_terminal.ui" or name.startswith("aef_terminal.ui.")
                        ):
                            violations.append(f"{path}:{node.lineno}:{name}")

    assert violations == []


def test_indicator_registry_is_single_source_for_status_and_pipeline() -> None:
    pipeline = indicator_pipeline_specs()
    manifest = indicator_manifest()
    modules = {module.id: module for module in indicator_modules()}

    assert set(pipeline) == set(INDICATOR_REGISTRY)
    assert set(manifest) == set(INDICATOR_REGISTRY)
    assert set(modules) == set(INDICATOR_REGISTRY)
    assert set(indicator_ids()) == set(INDICATOR_REGISTRY)
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        module = modules[indicator_id]
        assert spec.ui_key
        assert spec.calculate_ref
        assert spec.empty_result
        assert manifest[indicator_id]["label"] == spec.label
        assert pipeline[indicator_id].id == indicator_id
        assert pipeline[indicator_id].group in {"primary", "structure", "post_decision", "service"}
        assert pipeline[indicator_id].pipeline_stage == spec.pipeline_stage
        assert pipeline[indicator_id].candidate_promoter_ref == module.candidate_promoter_ref
        assert pipeline[indicator_id].candidate_score_floor_ref == spec.candidate_score_floor_ref
        assert pipeline[indicator_id].candidate_score_floor_min == spec.candidate_score_floor_min
        assert manifest[indicator_id]["pipeline_stage"] in {
            "primary",
            "structure",
            "setup",
            "post_decision",
            "ui",
        }
        assert manifest[indicator_id]["pipeline_order"] == spec.pipeline_order
        assert manifest[indicator_id]["execution_lanes"] == list(spec.execution_lanes)
        assert manifest[indicator_id]["fast_trigger_refs"] == list(spec.fast_trigger_refs)
        assert manifest[indicator_id]["snapshot_order"] == list(INDICATOR_REGISTRY).index(
            indicator_id
        )
        assert manifest[indicator_id]["calculate_ref"] == spec.calculate_ref
        assert manifest[indicator_id]["params_builder_ref"] == module.params_builder_ref
        assert manifest[indicator_id]["candidate_promoter_ref"] == module.candidate_promoter_ref
        if spec.pipeline_stage == "ui":
            assert manifest[indicator_id]["adapter_ref"] == ""
            assert (
                manifest[indicator_id]["service_ref"]
                or manifest[indicator_id]["overlay_collect"] is False
                or "terminal_core_service"
                in manifest[indicator_id]["renderer_contract"]["service_requirements"]
            )
            assert spec.module_type == "ui-only"
        else:
            assert manifest[indicator_id]["adapter_ref"]
            assert manifest[indicator_id]["service_ref"] == ""
        assert manifest[indicator_id]["depends_on"] == list(spec.depends_on)
        assert manifest[indicator_id]["required_dependencies"] == list(spec.required_dependencies)
        assert manifest[indicator_id]["optional_context"] == list(spec.optional_context)
        assert manifest[indicator_id]["shared_context_refs"] == list(spec.shared_context_refs)
        assert manifest[indicator_id]["confirmed_bar_context"] == [
            {
                "timeframe": request.timeframe,
                "history_bars": request.history_bars,
                "role": request.role,
            }
            for request in spec.confirmed_bar_context
        ]
        assert manifest[indicator_id]["signal_name_resolver_ref"] == spec.signal_name_resolver_ref
        assert manifest[indicator_id]["input_contract"] == spec.input_contract
        assert (
            manifest[indicator_id]["candidate_score_floor_param"]
            == spec.candidate_score_floor_param
        )
        assert manifest[indicator_id]["candidate_score_floor_ref"] == spec.candidate_score_floor_ref
        assert manifest[indicator_id]["candidate_score_floor_min"] == spec.candidate_score_floor_min
        assert manifest[indicator_id]["output_contract"] == spec.output_contract
        assert manifest[indicator_id]["status_contract"] == spec.status_contract
        assert manifest[indicator_id]["overlay_contract"] == spec.overlay_contract
        assert manifest[indicator_id]["overlay_layer"] == spec.overlay_layer
        assert manifest[indicator_id]["overlay_filter_ref"] == spec.overlay_filter_ref
        assert manifest[indicator_id]["overlay_collect"] is spec.overlay_collect
        assert manifest[indicator_id]["signal_contract"] == spec.signal_contract
        assert manifest[indicator_id]["runtime_payload_contract"] == {
            key: list(value)
            for key, value in normalize_runtime_payload_contract(
                spec.runtime_payload_contract,
                spec.runtime_payload_schema_refs,
            ).items()
        }
        assert manifest[indicator_id]["runtime_payload_schema_refs"] == list(
            spec.runtime_payload_schema_refs
        )
        assert manifest[indicator_id]["runtime_payload_compact"] == {
            section: dict(policy) for section, policy in spec.runtime_payload_compact.items()
        }
        for section, policy in spec.runtime_payload_compact.items():
            fields_ref = policy.get("fields")
            assert fields_ref in (None, "runtime_contract", "compact_contract") or isinstance(
                fields_ref, (list, tuple)
            ), indicator_id
        assert manifest[indicator_id]["runtime_payload_compact_contract"] == {
            key: list(value)
            for key, value in normalize_runtime_payload_compact_contract(
                spec.runtime_payload_compact_contract,
                spec.runtime_payload_schema_refs,
            ).items()
        }
        assert manifest[indicator_id]["service_payload_contract"] == {
            key: list(value)
            for key, value in normalize_service_payload_contract(
                spec.service_payload_contract,
                spec.service_payload_schema_refs,
            ).items()
        }
        assert manifest[indicator_id]["service_payload_schema_refs"] == list(
            spec.service_payload_schema_refs
        )
        for section, compact_fields in manifest[indicator_id][
            "runtime_payload_compact_contract"
        ].items():
            assert section in manifest[indicator_id]["runtime_payload_contract"], indicator_id
            assert set(compact_fields) <= set(
                manifest[indicator_id]["runtime_payload_contract"][section]
            ), indicator_id
        assert manifest[indicator_id]["renderer_kind"] in {"generic", "custom", "service"}
        assert manifest[indicator_id]["renderer_ref"]
        assert manifest[indicator_id]["renderer_contract"] == {
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
        }
        assert manifest[indicator_id]["overlay_layer"] in {
            "background",
            "zones",
            "levels",
            "signals",
            "tables",
            "foreground",
        }
        assert isinstance(manifest[indicator_id]["overlay_filter_ref"], str)
        assert isinstance(manifest[indicator_id]["overlay_collect"], bool)
        assert manifest[indicator_id]["module_type"] in {"signal", "context", "visual", "ui-only"}
        assert manifest[indicator_id]["ui"]["state_key"] == spec.state_key
        assert manifest[indicator_id]["ui"]["calc_key"] == spec.calc_key
        assert manifest[indicator_id]["ui"]["visible_key"] == spec.visible_key
        assert manifest[indicator_id]["ui"]["process_control_id"] == spec.process_control_id
        assert manifest[indicator_id]["ui"]["process_effect_ref"] == spec.process_effect_ref
        assert manifest[indicator_id]["ui"]["derived_state_refs"] == list(spec.derived_state_refs)
        assert manifest[indicator_id]["ui"]["runtime_state_ref"] == spec.runtime_state_ref
        assert manifest[indicator_id]["ui"]["settings_status_ref"] == spec.settings_status_ref
        assert manifest[indicator_id]["ui"]["api_enabled_key"] == spec.api_enabled_key
        assert manifest[indicator_id]["ui"]["api_visible_key"] == spec.api_visible_key
        assert manifest[indicator_id]["ui"]["settings_scope"] in {"instrument", "global"}
        assert isinstance(manifest[indicator_id]["ui"]["manager_order"], int)
        assert isinstance(manifest[indicator_id]["ui"]["runtime_order"], int)
        assert isinstance(manifest[indicator_id]["ui"]["default_calc"], bool)
        assert isinstance(manifest[indicator_id]["ui"]["default_visible"], bool)
        assert isinstance(manifest[indicator_id]["controls"], list)
        assert len(manifest[indicator_id]["controls"]) == len(spec.controls)
        for control in manifest[indicator_id]["controls"]:
            assert control["key"]
            assert control["label"]
            assert control["control_type"] in {"toggle", "number", "select", "color"}
            assert control["storage_key"]
            assert isinstance(control["api_key"], str)
            assert isinstance(control["param_key"], str)
            assert control["scope"] in {"instrument", "global"}
            assert control["action"] in {"apply", "load", "load_apply", "render", "none"}
            assert isinstance(control["effect_ref"], str)
            assert isinstance(control["compact"], bool)
            assert isinstance(control["option_labels"], list)
            assert not control["option_labels"] or len(control["option_labels"]) == len(
                control["options"]
            )
        assert manifest[indicator_id]["score_family"] in {
            "trend",
            "volume",
            "structure",
            "reversal",
            "options",
            "forecast",
            "decision",
        }
        assert 0.0 < manifest[indicator_id]["empirical_power"] <= 2.0
        assert 0.0 < manifest[indicator_id]["usefulness"] <= 2.0
        assert manifest[indicator_id]["source"] == "empirical_seed"
        assert (
            manifest[indicator_id]["score_weight"]
            == indicator_score_meta(indicator_id)["score_weight"]
        )

    schema = global_settings_schema()
    assert set(schema["indicator_controls"]) == set(INDICATOR_REGISTRY)
    assert schema["indicator_controls"]["linda_volume"] == manifest["linda_volume"]["controls"]
    assert any(
        control["storage_key"] == "barRadarCoilLen"
        for control in schema["indicator_controls"]["breakout_accumulation"]
    )
    catalog = schema["indicator_module_catalog"]
    assert set(catalog) == {"package", "installed", "available", "invalid", "skipped"}
    assert catalog["package"] == "aef_terminal.indicators.modules"
    assert {
        indicator_id for entry in catalog["installed"] for indicator_id in entry["indicator_ids"]
    } == set(INDICATOR_REGISTRY)


def test_overlay_runtime_compact_uses_registry_contract() -> None:
    manifest = indicator_manifest()
    assert manifest["breakout_accumulation"]["runtime_payload_compact"]["overlays"]["limit"] == 96
    assert manifest["obvious_failure"]["runtime_payload_compact"]["overlays"]["limit"] == 160
    assert manifest["impulse_fib"]["runtime_payload_compact"]["overlays"]["limit"] == 122
    assert len(OVERLAY_RUNTIME_FIELDS) == len(set(OVERLAY_RUNTIME_FIELDS))
    assert len(OVERLAY_COMPACT_FIELDS) == len(set(OVERLAY_COMPACT_FIELDS))
    assert set(OVERLAY_COMPACT_FIELDS) < set(OVERLAY_RUNTIME_FIELDS)
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        overlay_policy = spec.runtime_payload_compact.get("overlays")
        if overlay_policy is None:
            continue
        assert overlay_policy.get("fields") == "compact_contract", indicator_id
        assert "overlay_contract" in spec.runtime_payload_schema_refs, indicator_id
        overlay_fields = manifest[indicator_id]["runtime_payload_contract"]["overlays"]
        compact_fields = manifest[indicator_id]["runtime_payload_compact_contract"]["overlays"]
        assert set(OVERLAY_RUNTIME_FIELDS).issubset(overlay_fields), indicator_id
        assert set(OVERLAY_COMPACT_FIELDS).issubset(compact_fields), indicator_id
        assert set(compact_fields).issubset(overlay_fields), indicator_id

    overlay_fields = manifest["breakout_accumulation"]["runtime_payload_contract"]["overlays"]
    overlay_compact_fields = manifest["breakout_accumulation"]["runtime_payload_compact_contract"][
        "overlays"
    ]
    assert "trade_plan" in overlay_fields
    assert "signal" in overlay_fields
    assert "signal_overlay" in overlay_fields
    assert "signal_overlay" in overlay_compact_fields
    assert "trade_plan" not in overlay_compact_fields
    assert "signal" not in overlay_compact_fields
    assert "has_trade_plan" not in overlay_fields
    assert "has_trade_plan" not in overlay_compact_fields
    for field in (
        "bar_slot",
        "start_bar_slot",
        "end_bar_slot",
        "end_anchor_bar_slot",
        "origin_end_bar_slot",
    ):
        assert field not in overlay_fields
        assert field not in overlay_compact_fields
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        if (
            spec.renderer_kind == "service"
            or spec.overlay_collect is False
            or not spec.renderer_primitives
        ):
            continue
        assert manifest[indicator_id]["runtime_payload_contract"]["overlays"], indicator_id
    for field in (
        "type",
        "ts",
        "start_ts",
        "end_ts",
        "end_anchor_ts",
        "end_bar_offset",
        "retention",
        "price",
        "label",
        "tooltip",
        "contract",
        "glyph_kind",
        "wall_direction",
        "range_label",
        "overlay_group",
        "pointer",
        "tick",
        "deletable",
        "delete_icon",
        "scenario",
        "setup",
        "trigger_event",
        "evidence",
        "risk",
        "quality",
        "fact_groups",
        "metrics",
    ):
        assert field in overlay_fields
        assert field in overlay_compact_fields


def test_indicator_runtime_compact_rows_use_summary_contracts() -> None:
    manifest = indicator_manifest()
    compact_row_indicators = (
        "linda_volume",
        "impulse_fib",
        "breakout_accumulation",
    )

    for indicator_id in compact_row_indicators:
        runtime_contract = manifest[indicator_id]["runtime_payload_contract"]
        compact_contract = manifest[indicator_id]["runtime_payload_compact_contract"]
        compact_policy = manifest[indicator_id]["runtime_payload_compact"]
        for section in ("series", "events"):
            fields_ref = compact_policy[section]["fields"]
            compact_fields = (
                compact_contract[section]
                if fields_ref == "compact_contract"
                else runtime_contract[section]
                if fields_ref == "runtime_contract"
                else list(fields_ref)
            )
            assert set(compact_fields) < set(runtime_contract[section]), indicator_id
            assert "ts" in compact_fields, indicator_id
            assert "score" in compact_fields, indicator_id
            assert "signal" not in compact_fields, indicator_id
            assert "metrics" not in compact_fields, indicator_id


def test_indicator_runtime_contract_declares_structured_signal_payload_fields() -> None:
    manifest = indicator_manifest()

    breakout_series = manifest["breakout_accumulation"]["runtime_payload_contract"]["series"]
    for field in (
        "signal",
        "rvol_source",
        "range_label",
        "overlay_group",
        "render_label",
        "lifecycle",
        "pointer",
        "tick",
    ):
        assert field in breakout_series

    for indicator_id in ("breakout_accumulation", "impulse_fib", "trade_setup_engine"):
        assert manifest[indicator_id]["runtime_payload_contract"]["latest"], indicator_id

    for field in (
        "signal",
        "lifecycle",
        "scenario",
        "setup",
        "trigger_event",
        "evidence",
        "risk",
        "quality",
        "fact_groups",
        "metrics",
        "trade_plan",
    ):
        assert field in manifest["trade_setup_engine"]["runtime_payload_contract"]["latest"]
    for field in (
        "action_card",
        "action_reason_code",
        "blocked_reason",
        "raw_action",
        "setup_mode",
        "setup_type",
        "trend_context",
    ):
        assert field in manifest["trade_setup_engine"]["runtime_payload_contract"]["overlays"]
    assert "badge_facts" in manifest["trade_setup_engine"]["runtime_payload_contract"]["overlays"]
    for field in (
        "action",
        "action_reason_code",
        "blocked_reason",
        "raw_action",
        "trend_context",
    ):
        assert field in manifest["trade_setup_engine"]["runtime_payload_contract"]["latest"]
    for field in ("signal", "lifecycle", "continuation_pattern", "alert_lifecycle"):
        assert field in manifest["impulse_fib"]["runtime_payload_contract"]["latest"]
    for field in (
        "signal",
        "scenario",
        "setup",
        "trigger_event",
        "evidence",
        "risk",
        "quality",
        "fact_groups",
        "metrics",
        "trade_plan",
    ):
        assert field in manifest["smc_channels"]["runtime_payload_contract"]["latest"]

    obvious_latest = manifest["obvious_failure"]["runtime_payload_contract"]["latest"]
    assert obvious_latest
    for field in ("scenario", "trigger_event", "trade_plan", "signal_actionable"):
        assert field in obvious_latest

    obvious_signals = manifest["obvious_failure"]["runtime_payload_contract"]["signals"]
    for field in (
        "action",
        "blocked",
        "blocked_reason",
        "confirmed",
        "invalidation",
        "kind",
        "overlays",
        "plan_coherent",
        "plan_complete",
        "raw_action",
        "reward",
        "risk",
        "rr",
        "signal_actionable",
        "source_tf",
        "trade_plan",
    ):
        assert field in obvious_signals


def test_trade_setup_engine_package_owns_its_runtime_and_authority_contracts() -> None:
    module = import_module("aef_terminal.indicators.modules.trade_setup_engine")
    module_path = Path(module.__file__ or "")
    manifest = indicator_manifest()["trade_setup_engine"]

    assert module_path.name == "__init__.py"
    assert module_path.parent.name == "trade_setup_engine"
    assert TRADE_SETUP_ENGINE_MODULE.spec is INDICATOR_REGISTRY["trade_setup_engine"]
    assert manifest["output_contract"] == TRADE_SETUP_AUTHORITY_CONTRACT
    assert indicator_ids_for_output_contract(TRADE_SETUP_AUTHORITY_CONTRACT) == (
        "trade_setup_engine",
    )
    assert manifest["ui"]["default_calc"] is False
    assert manifest["shared_context_refs"] == ["vsa_breakout_facts"]
    assert {
        "vsa_breakout_context",
        "vsa_breakout_gate",
    } <= set(manifest["runtime_payload_contract"]["latest"])
    assert manifest["runtime_payload_contract"]["latest"] == list(
        dict.fromkeys(
            [
                *normalize_runtime_payload_contract(None)["latest"],
                *TRADE_SETUP_ENGINE_EVENT_FIELDS,
            ]
        )
    )
    assert "trade_setup_engine_event" not in RUNTIME_PAYLOAD_SCHEMA_FIELDS


def test_trade_setup_engine_separates_contract_scoring_and_presentation() -> None:
    package_root = Path("src/aef_terminal/indicators/modules/trade_setup_engine")
    facade = (package_root / "__init__.py").read_text(encoding="utf-8")
    contracts = (package_root / "contracts.py").read_text(encoding="utf-8")
    scoring = (package_root / "scoring.py").read_text(encoding="utf-8")
    presentation = (package_root / "presentation.py").read_text(encoding="utf-8")

    assert "class TradeSetupEngineParams" in contracts
    assert "def _score_momentum_candidate" not in contracts
    assert "def _score_momentum_candidate" in scoring
    assert "def _best_candidate" not in scoring
    assert "def _build_overlays" not in scoring
    assert "def _build_overlays" in presentation
    assert "def _score_momentum_candidate" not in presentation
    assert "def trade_setup_engine" in facade
    assert "def _best_candidate" in facade
    assert "def _plan_from_setup" in facade
    assert "def _score_momentum_candidate" not in facade
    assert "def _build_overlays" not in facade


def test_gex_dynamics_is_engine_owned_not_an_indicator_module() -> None:
    manifest = indicator_manifest()
    discovered_ids = {module.id for module in discover_indicator_modules()}
    catalog = indicator_module_catalog()
    catalog_indicator_ids = {
        indicator_id for entry in catalog["installed"] for indicator_id in entry["indicator_ids"]
    }
    manager_controls = global_settings_schema()["indicator_controls"]

    assert "gex_dynamics" not in discovered_ids
    assert "gex_dynamics" not in INDICATOR_REGISTRY
    assert "gex_dynamics" not in manifest
    assert "gex_dynamics" not in catalog_indicator_ids
    assert "gex_dynamics" not in manager_controls
    assert not any("gex" in schema_id.lower() for schema_id in RUNTIME_PAYLOAD_SCHEMA_FIELDS)
    assert "gex_dynamics" not in DEFAULT_LIFECYCLE_LABELS
    assert indicator_ids_for_output_contract("gex-dynamics-v1") == ()


def test_vsa_volume_is_engine_owned_not_an_indicator_module() -> None:
    manifest = indicator_manifest()
    discovered_ids = {module.id for module in discover_indicator_modules()}
    catalog = indicator_module_catalog()
    catalog_indicator_ids = {
        indicator_id for entry in catalog["installed"] for indicator_id in entry["indicator_ids"]
    }
    manager_controls = global_settings_schema()["indicator_controls"]

    assert "vsa_volume" not in discovered_ids
    assert "vsa_volume" not in INDICATOR_REGISTRY
    assert "vsa_volume" not in manifest
    assert "vsa_volume" not in catalog_indicator_ids
    assert "vsa_volume" not in manager_controls
    assert "vsa_volume_row" not in RUNTIME_PAYLOAD_SCHEMA_FIELDS


def test_indicator_shared_context_consumers_are_registry_declared() -> None:
    assert indicator_ids_for_shared_context("vsa_facts", stage="primary") == (
        "market_spotlight",
        "linda_volume",
    )
    assert indicator_ids_for_shared_context("vix_context", stage="primary") == (
        "market_spotlight",
        "linda_volume",
    )
    assert indicator_ids_for_shared_context("market_series", stage="primary") == (
        "wisdom_of_crowd",
        "market_spotlight",
        "linda_volume",
    )
    assert indicator_ids_for_shared_context(
        "vsa_breakout_facts",
        stage="setup",
    ) == ("trade_setup_engine",)
    assert indicator_ids_for_shared_context("vsa_breakout_facts") == ("trade_setup_engine",)
    assert indicator_ids_for_shared_context("option_targets") == ("option_reversal",)
    assert indicator_ids_for_shared_context("option_flow") == ("absorption_trap",)
    assert indicator_ids_for_shared_context("tick_flow") == ("tick_flow",)


def test_indicator_modules_declare_backend_api_param_mapping() -> None:
    manifest = indicator_manifest()
    expected_enabled_keys = {
        "breakout_accumulation": "bar_enabled",
        "market_spotlight": "market_spotlight_enabled",
        "linda_volume": "linda_enabled",
        "impulse_fib": "impulse_enabled",
        "trade_setup_engine": "setup_engine_enabled",
        "smc_channels": "smc_enabled",
        "w5_structure": "w5_enabled",
        "wolfe_structure": "wolfe_enabled",
        "option_reversal": "option_reversal_enabled",
        "martin_carlo": "martin_carlo_enabled",
        "ai_third_opinion": "ai_third_opinion_enabled",
        "tick_flow": "tick_flow_enabled",
    }
    expected_control_keys = {
        "bar_coil_len",
        "bar_barcode_len",
        "bar_operator_max_bars",
        "impulse_min_score",
        "impulse_fib_lookback",
        "impulse_fib_projection_bars",
        "impulse_fib_bars",
        "setup_engine_min_score",
        "setup_engine_watch_score",
        "setup_engine_enable_momentum",
        "setup_engine_enable_mean_reversion",
        "market_spotlight_table_style",
        "linda_min_score",
        "linda_indian_min_score",
        "linda_enable_grail",
        "linda_enable_indians",
        "linda_enable_turtle_soup",
        "linda_enable_turtle_soup_plus_one",
        "linda_enable_eighty_twenty",
        "linda_enable_the_anti",
        "linda_enable_momentum_pinball",
        "linda_enable_hv_squeeze",
        "linda_enable_adx_gapper",
        "smc_pivot_len",
        "smc_ob_min_displacement_atr",
        "w5_mode",
        "w5_min_score",
        "wolfe_pivot_len",
        "wolfe_confirmed_limit",
        "wolfe_show_candidates",
        "wolfe_show_price_target",
        "wolfe_line_opacity",
        "obvious_failure_zones",
        "obvious_failure_labels",
        "obvious_failure_lookback",
        "obvious_failure_min_score",
        "martin_carlo_mode",
        "martin_carlo_engine",
        "martin_carlo_lookback",
        "martin_carlo_horizon",
        "martin_carlo_simulations",
        "martin_carlo_bins",
        "martin_carlo_nn_bias",
        "martin_carlo_structural_bias",
        "martin_carlo_regime_filter",
        "martin_carlo_impulse_bias",
        "martin_carlo_option_bias",
        "ai_third_opinion_lookback",
        "ai_third_opinion_external",
        "ai_third_opinion_refresh_mode",
        "ai_third_opinion_provider",
        "ai_third_opinion_model",
        "ai_third_opinion_lens",
        "ai_third_opinion_channel_context",
    }

    assert {
        indicator_id: manifest[indicator_id]["ui"]["api_enabled_key"]
        for indicator_id in expected_enabled_keys
    } == expected_enabled_keys
    assert manifest["martin_carlo"]["ui"]["api_visible_key"] == "martin_carlo_visible"
    assert manifest["ai_third_opinion"]["ui"]["api_visible_key"] == "ai_third_opinion_visible"
    assert manifest["ai_third_opinion"]["ui"]["process_effect_ref"] == "ai_third_opinion_gate"
    assert manifest["ai_third_opinion"]["ui"]["default_calc"] is False
    assert manifest["ai_third_opinion"]["extensions"] == {
        "snapshot_enricher_ref": (
            "aef_terminal.indicators.modules.ai_third_opinion.service:"
            "enrich_snapshot_with_ai_third_opinion"
        ),
        "router_ref": "aef_terminal.indicators.modules.ai_third_opinion.router:create_router",
        "ui_js_assets": ["client.js"],
        "ui_css_assets": ["styles.css"],
        "sidebar": {
            "id": "ai",
            "title": "AI Advisor",
            "mount_id": "ai-third-opinion-discuss",
            "icon_svg": (
                '<path d="M12 3 14 8l5 2-5 2-2 5-2-5-5-2 5-2 2-5Z"/>'
                '<path d="m18 16 .8 2.2L21 19l-2.2.8L18 22l-.8-2.2L15 19l2.2-.8L18 16Z"/>'
            ),
            "order": 250,
        },
    }
    assert (
        manifest["ai_third_opinion"]["renderer_contract"]["table"]["animation_ref"]
        == "table_thinking"
    )
    ai_controls = {control["key"]: control for control in manifest["ai_third_opinion"]["controls"]}
    assert ai_controls["refresh_mode"]["scope"] == "global"
    assert ai_controls["provider"]["scope"] == "instrument"
    assert ai_controls["provider"]["effect_ref"] == "ai_third_opinion_model_options"
    assert "codex" in ai_controls["provider"]["options"]
    assert ai_controls["model"]["scope"] == "instrument"
    assert ai_controls["model"]["effect_ref"] == "ai_third_opinion_model_options"
    assert {"gpt-5.6-terra", "gpt-5.6-sol"}.issubset(ai_controls["model"]["options"])
    api_keys = {
        control["api_key"]
        for item in manifest.values()
        for control in item["controls"]
        if control["api_key"]
    }
    assert expected_control_keys.issubset(api_keys)
    expected_param_keys = {
        "breakout_accumulation": {"coil_len", "barcode_len", "operator_max_bars"},
        "martin_carlo": {"mode", "engine", "price_bins", "nn_bias", "regime_filter"},
        "ai_third_opinion": {
            "lookback",
            "external",
            "refresh_mode",
            "provider",
            "model",
            "lens",
            "channel_context",
        },
        "linda_volume": {"enable_turtle_soup", "enable_turtle_soup_plus_one", "min_score"},
        "wolfe_structure": {"pivot_len", "confirmed_limit", "show_candidates", "line_opacity"},
        "w5_structure": {"mode", "min_score"},
    }
    manifest_param_keys = {
        indicator_id: {
            control["param_key"]
            for control in manifest[indicator_id]["controls"]
            if control["api_key"]
        }
        for indicator_id in manifest
    }
    for indicator_id, param_keys in expected_param_keys.items():
        assert param_keys.issubset(manifest_param_keys[indicator_id])


def test_market_indicator_params_are_built_from_registry_manifest() -> None:
    params = market_indicator_params(
        global_atr_len=14,
        global_rvol_len=30,
        global_ema_pullback=20,
        global_ema_fast=21,
        global_ema_slow=55,
        global_ema_magnet=233,
        global_score_pre=42.0,
        global_score_watch=58.0,
        global_score_arm=70.0,
        global_score_go=78.0,
        global_rvol_low=0.85,
        global_rvol_elevated=1.10,
        global_rvol_high=1.20,
        global_rvol_climax=1.60,
        strategy_mode=StrategyMode.BALANCED,
        signal_min_rr=1.5,
        manual_channel_payload=[],
        indicator_query_params={
            "bar_enabled": "false",
            "bar_coil_len": "20",
            "linda_enable_turtle_soup": "true",
            "wolfe_confirmed_limit": "9",
            "martin_carlo_bins": "18",
            "w5_mode": "swing",
            "w5_min_score": "64",
            "tick_flow_enabled": "true",
            "absorption_synergy_matrix": "false",
            "gex_dynamics_enabled": "true",
        },
    )

    assert params["breakout_accumulation"]["enabled"] is False
    assert params["breakout_accumulation"]["coil_len"] == 20
    assert params["linda_volume"]["enable_turtle_soup"] is True
    assert params["wolfe_structure"]["confirmed_limit"] == 3
    assert params["martin_carlo"]["price_bins"] == 18
    assert "bins" not in params["martin_carlo"]
    assert params["w5_structure"]["mode"] == "swing"
    assert params["w5_structure"]["min_score"] == 64
    assert params["tick_flow"]["enabled"] is True
    assert params["absorption_trap"]["synergy_matrix"] is False
    assert "gex_dynamics" not in params


def test_server_alert_indicator_params_use_persisted_global_defaults() -> None:
    params = server_alert_indicator_params(
        {
            "aef:indicator:global:globalAtrLen": "9",
            "aef:indicator:global:globalRvolLen": "44",
            "aef:indicator:global:globalEmaMagnet": "300",
            "aef:indicator:global:globalScoreArm": "76",
        }
    )
    defaults = indicator_defaults_from_params(params["global_defaults"])

    assert defaults.atr_len == 9
    assert defaults.rvol_len == 44
    assert defaults.ema.magnet == 300
    assert defaults.score.arm == 76.0


def test_indicator_modules_declare_overlay_rendering_contracts() -> None:
    manifest = indicator_manifest()

    assert manifest["breakout_accumulation"]["overlay_layer"] == "levels"
    assert manifest["breakout_accumulation"]["overlay_filter_ref"] == "breakout_accumulation"
    assert manifest["market_spotlight"]["overlay_filter_ref"] == "generic_overlay_filter"
    assert manifest["smc_channels"]["overlay_layer"] == "zones"
    assert manifest["smc_channels"]["overlay_filter_ref"] == "smc_channels"
    assert manifest["impulse_fib"]["overlay_filter_ref"] == "impulse_fib"
    assert manifest["linda_volume"]["overlay_filter_ref"] == "linda_volume"
    assert manifest["trade_setup_engine"]["overlay_filter_ref"] == "generic_overlay_filter"
    assert manifest["obvious_failure"]["overlay_filter_ref"] == "generic_overlay_filter"
    assert manifest["obvious_failure"]["candidate_score_floor_param"] == "min_score"
    assert manifest["option_reversal"]["overlay_layer"] == "foreground"
    assert manifest["market_spotlight"]["renderer_contract"]["primitives"] == [
        "table",
        "line",
    ]
    assert manifest["market_spotlight"]["renderer_contract"]["placements"] == [
        "table",
        "price",
    ]
    assert manifest["breakout_accumulation"]["renderer_contract"]["placements"] == [
        "price",
        "table",
    ]
    assert manifest["impulse_fib"]["renderer_contract"]["primitives"] == [
        "box",
        "line",
        "label",
        "marker",
    ]
    assert manifest["linda_volume"]["renderer_kind"] == "generic"
    assert manifest["linda_volume"]["renderer_contract"]["custom_requirements"] == []
    assert (
        "terminal_core_service"
        in manifest["tick_flow"]["renderer_contract"]["service_requirements"]
    )
    assert (
        "broker_tick_context" in manifest["tick_flow"]["renderer_contract"]["service_requirements"]
    )
    assert all(
        "candle_proxy" not in requirement
        for requirement in manifest["tick_flow"]["renderer_contract"]["service_requirements"]
    )
    assert manifest["wolfe_structure"]["overlay_collect"] is True
    assert manifest["martin_carlo"]["renderer_kind"] == "custom"
    assert manifest["martin_carlo"]["renderer_contract"]["ref"] == "martin_carlo_forecast"
    assert manifest["martin_carlo"]["renderer_contract"]["primitives"] == ["custom"]
    assert (
        "forecast_payload" in manifest["martin_carlo"]["renderer_contract"]["custom_requirements"]
    )
    assert "forecast" in manifest["martin_carlo"]["runtime_payload_contract"]["latest"]
    assert "payload" in manifest["martin_carlo"]["runtime_payload_contract"]["overlays"]
    assert "renderer_ref" in manifest["martin_carlo"]["runtime_payload_contract"]["overlays"]
    assert "render_key" in manifest["martin_carlo"]["runtime_payload_contract"]["overlays"]
    assert manifest["martin_carlo"]["overlay_collect"] is True
    assert "tone" in manifest["option_reversal"]["runtime_payload_contract"]["overlays"]
    assert "role" in manifest["option_reversal"]["runtime_payload_contract"]["overlays"]
    assert "accent" in manifest["ai_third_opinion"]["runtime_payload_contract"]["overlays"]
    assert manifest["w5_structure"]["runtime_payload_contract"]["overlays"] == list(
        OVERLAY_RUNTIME_FIELDS
    )
    assert "trigger_state" in manifest["w5_structure"]["runtime_payload_contract"]["latest"]
    assert "trade_plan" in manifest["w5_structure"]["runtime_payload_contract"]["events"]
    assert "structure_mode" in manifest["smc_channels"]["runtime_payload_contract"]["latest"]
    assert "metrics" in manifest["smc_channels"]["runtime_payload_contract"]["latest"]
    assert "signal" in manifest["smc_channels"]["runtime_payload_contract"]["events"]
    assert "metrics" in manifest["smc_channels"]["runtime_payload_contract"]["events"]
    assert "projected_p5" in manifest["wolfe_structure"]["runtime_payload_contract"]["latest"]
    assert "reason_code" in manifest["wolfe_structure"]["runtime_payload_contract"]["latest"]
    assert "metrics" in manifest["wolfe_structure"]["runtime_payload_contract"]["latest"]
    assert "signal" in manifest["wolfe_structure"]["runtime_payload_contract"]["events"]
    assert manifest["wolfe_structure"]["runtime_payload_contract"]["overlays"] == list(
        OVERLAY_RUNTIME_FIELDS
    )
    assert "projected_p5" in manifest["wolfe_structure"]["runtime_payload_contract"]["paths"]
    assert "end_anchor_ts" in manifest["wolfe_structure"]["runtime_payload_contract"]["paths"]
    assert "end_bar_offset" in manifest["wolfe_structure"]["runtime_payload_contract"]["paths"]
    assert "metrics" in manifest["wolfe_structure"]["runtime_payload_contract"]["paths"]
    assert "signal" in manifest["wolfe_structure"]["runtime_payload_contract"]["candidates"]


def test_indicator_modules_are_discovered_and_registered_by_contract() -> None:
    modules = indicator_modules()
    manifest = indicator_manifest()

    assert [module.id for module in modules] == list(INDICATOR_REGISTRY)
    assert [module.spec.pipeline_order for module in modules] == sorted(
        module.spec.pipeline_order for module in modules
    )
    assert "tick_flow" in INDICATOR_REGISTRY
    assert INDICATOR_REGISTRY["tick_flow"].pipeline_stage == "ui"
    assert INDICATOR_REGISTRY["tick_flow"].module_type == "ui-only"
    assert manifest["tick_flow"]["service_ref"] == ""
    assert manifest["tick_flow"]["extensions"]["router_ref"] == ""
    assert manifest["tick_flow"]["renderer_kind"] == "service"
    assert manifest["tick_flow"]["runtime_payload_schema_refs"] == []
    assert manifest["tick_flow"]["service_payload_schema_refs"] == ["tick_flow_context_v1"]
    assert "delta" in manifest["tick_flow"]["service_payload_contract"]["top_level"]
    assert "cumulative_delta" in manifest["tick_flow"]["service_payload_contract"]["delta"]
    assert "value_area" in manifest["tick_flow"]["service_payload_contract"]["aggregates"]
    assert "live" in manifest["tick_flow"]["service_payload_contract"]["stats"]
    assert manifest["tick_flow"]["ui"]["process_effect_ref"] == "tick_flow_context"
    assert manifest["tick_flow"]["ui"]["api_enabled_key"] == "tick_flow_enabled"
    assert {
        control["key"]: control["effect_ref"]
        for control in manifest["tick_flow"]["controls"]
        if control["effect_ref"]
    } == {
        "live": "tick_flow_live",
        "recentDelta": "tick_flow_recent_delta",
        "recentDeltaSeconds": "tick_flow_recent_delta_seconds",
        "lookbackMinutes": "tick_flow_context_reload",
    }
    assert "channel_master" in INDICATOR_REGISTRY
    assert INDICATOR_REGISTRY["channel_master"].pipeline_stage == "primary"
    assert INDICATOR_REGISTRY["channel_master"].module_type == "context"
    assert INDICATOR_REGISTRY["channel_master"].candidate_promoter == "channel_master"
    assert INDICATOR_REGISTRY["channel_master"].paper_tradable is False
    assert manifest["channel_master"]["adapter_ref"].startswith(
        "aef_terminal.indicators.modules.channel_master:"
    )
    assert manifest["channel_master"]["candidate_promoter_ref"].startswith(
        "aef_terminal.indicators.modules.channel_master:"
    )
    assert manifest["channel_master"]["service_ref"] == ""
    assert manifest["channel_master"]["runtime_payload_schema_refs"] == []
    assert manifest["channel_master"]["service_payload_schema_refs"] == []
    assert manifest["channel_master"]["service_payload_contract"] == {}
    assert manifest["channel_master"]["overlay_collect"] is False
    assert manifest["channel_master"]["output_contract"] == "channel-master-v3"
    assert manifest["channel_master"]["confirmed_bar_context"] == []
    assert (
        "interaction_model" in manifest["channel_master"]["runtime_payload_contract"]["top_level"]
    )
    assert (
        "research_observations"
        in manifest["channel_master"]["runtime_payload_contract"]["top_level"]
    )
    assert manifest["channel_master"]["ui"]["api_enabled_key"] == "channel_master_enabled"
    assert manifest["channel_master"]["ui"]["default_calc"] is False
    assert manifest["channel_master"]["ui"]["show_in_runtime"] is False
    assert manifest["smc_channels"]["optional_context"] == [
        "market_spotlight",
        "channel_master",
    ]
    assert manifest["trade_setup_engine"]["optional_context"] == ["market_spotlight"]
    assert "channel_master" in manifest["ai_third_opinion"]["optional_context"]
    assert manifest["market_spotlight"]["ui"]["derived_state_refs"] == ["table_position_state"]
    assert manifest["option_reversal"]["ui"]["derived_state_refs"] == ["table_position_state"]
    assert manifest["w5_structure"]["ui"]["derived_state_refs"] == []
    assert manifest["tick_flow"]["ui"]["derived_state_refs"] == []
    assert manifest["trade_setup_engine"]["ui"]["runtime_state_ref"] == ""


def test_indicator_table_controls_share_one_presentation_contract() -> None:
    manifest = indicator_manifest()
    expected_positions = [
        "bottom",
        "top",
        "top-left",
        "top-right",
        "bottom-left",
        "bottom-right",
        "dock",
        "off",
    ]
    table_control_ids = {
        indicator_id
        for indicator_id, spec in manifest.items()
        if any(control["key"] == "tablePosition" for control in spec["controls"])
    }

    assert table_control_ids == {
        "absorption_trap",
        "ai_third_opinion",
        "breakout_accumulation",
        "market_spotlight",
        "option_drift",
        "option_reversal",
        "wisdom_of_crowd",
    }
    for indicator_id in table_control_ids:
        controls = {control["key"]: control for control in manifest[indicator_id]["controls"]}
        assert controls["tablePosition"]["options"] == expected_positions
        assert controls["tablePosition"]["api_key"] == ""
        assert controls["tablePosition"]["action"] == "render"
        assert controls["tableOpacity"]["default"] == 90
        assert controls["tableOpacity"]["minimum"] == 65
        assert controls["tableOpacity"]["maximum"] == 100
        assert controls["tableOpacity"]["step"] == 5
        assert controls["tableOpacity"]["api_key"] == ""
        assert controls["tableOpacity"]["action"] == "render"


def test_indicator_module_contracts_are_validated_at_registry_boundary() -> None:
    modules = indicator_modules()
    module_ids = {module.id for module in modules}
    seen_control_ids: set[str] = set()
    primitive_ids = {
        "box",
        "custom",
        "line",
        "marker",
        "label",
        "table",
        "vertical_text_label",
        "signal_tick",
        "structure_paths",
        "tick_volume_profile",
        "tick_delta_labels",
    }
    placement_ids = {"price", "table", "profile", "settings"}
    derived_state_refs = {
        "table_position_state",
    }

    assert len(module_ids) == len(modules)
    for module in modules:
        spec = module.spec
        assert spec.id == module.id
        assert spec.id in INDICATOR_REGISTRY
        assert spec.label
        assert spec.calculates
        assert spec.calculate_ref
        assert spec.ui_key
        assert spec.state_key
        assert spec.calc_key
        assert spec.visible_key or spec.module_type == "ui-only"
        assert spec.renderer_ref or spec.renderer_kind == "generic"
        assert set(spec.renderer_primitives).issubset(primitive_ids)
        assert set(spec.renderer_placements).issubset(placement_ids)
        assert set(spec.derived_state_refs).issubset(derived_state_refs)
        assert spec.renderer_table_header_width >= 14
        if spec.renderer_kind == "custom":
            assert spec.custom_renderer_requirements
            assert not spec.service_renderer_requirements
        if spec.renderer_kind == "service":
            assert spec.service_renderer_requirements
            assert not spec.custom_renderer_requirements
        assert spec.pipeline_order >= 0
        assert spec.manager_order >= 0
        assert spec.runtime_order >= 0
        assert not (
            {spec.id} & set(spec.depends_on + spec.optional_context + spec.required_dependencies)
        )
        assert set(spec.required_dependencies).issubset(spec.depends_on)
        for dependency_id in spec.depends_on + spec.required_dependencies:
            assert dependency_id in INDICATOR_REGISTRY
        if spec.pipeline_stage == "ui":
            assert spec.module_type == "ui-only"
            if module.service_ref:
                package_module = spec.calculate_ref.split(":", 1)[0]
                service_module = module.service_ref.split(":", 1)[0]
                assert service_module == package_module or service_module.startswith(
                    f"{package_module}."
                )
            assert not module.adapter_ref
        else:
            assert module.adapter_ref
            assert not module.service_ref
        control_keys = [control.key for control in spec.controls]
        assert len(control_keys) == len(set(control_keys))
        for control in spec.controls:
            assert control.storage_key
            assert control.state_key
            if control.element_id:
                assert control.element_id not in seen_control_ids
                seen_control_ids.add(control.element_id)
            if control.control_type == "select":
                assert control.options
                assert str(control.default) in control.options
                assert not control.option_labels or len(control.option_labels) == len(
                    control.options
                )
            if control.control_type == "number":
                assert control.minimum is not None
                assert control.maximum is not None
                assert control.minimum <= control.default <= control.maximum


def test_indicator_module_files_do_not_duplicate_module_declarations_or_top_level_functions() -> (
    None
):
    skip_files = {
        "__init__.py",
        "contracts.py",
        "control_specs.py",
        "defaults.py",
        "module_contract.py",
        "module_discovery.py",
        "module_validation.py",
        "registry.py",
        "refs.py",
        "scoring.py",
        "settings_schema.py",
    }
    for path in sorted(Path("src/aef_terminal/indicators").glob("*.py")):
        if path.name in skip_files:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        module_declarations = [
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
            and target.id in {"INDICATOR_MODULE", "INDICATOR_MODULES"}
        ]
        function_names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]

        assert len(module_declarations) <= 1, path
        assert len(function_names) == len(set(function_names)), path


def test_indicator_package_layout_keeps_runtime_modules_and_helpers_separate() -> None:
    root = Path("src/aef_terminal/indicators")
    modules_root = root / "modules"
    allowed_root_files = {
        "__init__.py",
        "contracts.py",
        "control_specs.py",
        "defaults.py",
        "module_contract.py",
        "module_discovery.py",
        "module_validation.py",
        "registry.py",
        "refs.py",
        "runtime.py",
        "runtime_params.py",
        "scaffold.py",
        "scoring.py",
        "settings_schema.py",
        "settings_resolution.py",
        "service_contract.py",
        "domain_facts.py",
    }
    root_files = {path.name for path in root.glob("*.py")}

    assert root_files == allowed_root_files
    assert modules_root.is_dir()
    assert not (root / "linda").exists()
    assert list(modules_root.glob("_linda_*.py")) == []
    assert {path.name for path in modules_root.glob("*.py")} == {
        "__init__.py",
        "_structure_common.py",
    }
    catalog = indicator_module_catalog()
    assert catalog["available"] == []
    assert catalog["invalid"] == []
    linda_root = modules_root / "linda_volume"
    assert {
        "action.py",
        "candidates.py",
        "plan.py",
        "playbook_contract.py",
        "setups.py",
    } <= {path.name for path in linda_root.glob("*.py")}


def test_indicator_runtime_primitives_do_not_resolve_through_registry() -> None:
    runtime_source = Path("src/aef_terminal/indicators/runtime.py").read_text(encoding="utf-8")
    module_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/aef_terminal/indicators/modules").glob("*/__init__.py")
    )

    assert "aef_terminal.indicators.registry" not in runtime_source
    assert "calculate_function" not in runtime_source
    assert "calculate_function" not in module_sources


def test_indicator_scaffold_generates_discoverable_module(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "demo_indicators"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    source = indicator_module_template(
        "demo_signal",
        label="Demo Signal",
        package_name="demo_indicators",
        pipeline_order=321,
    )
    module_dir = package_dir / "demo_signal"
    module_dir.mkdir()
    (module_dir / "__init__.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    discover_indicator_modules.cache_clear()
    try:
        modules = discover_indicator_modules("demo_indicators")
    finally:
        discover_indicator_modules.cache_clear()

    assert [module.id for module in modules] == ["demo_signal"]
    module = modules[0]
    assert module.spec.label == "Demo Signal"
    assert module.spec.pipeline_order == 321
    assert module.spec.calculate_ref == "demo_indicators.demo_signal:demo_signal"
    assert module.adapter_ref == "demo_indicators.demo_signal:build_execution_spec"
    assert module.spec.process_control_id == "demo-signal-process"
    assert module.spec.default_calc is False
    assert 'ctx.params("demo_signal")' in source
    assert "aef_terminal.engine" not in source


def test_indicator_scaffold_cli_creates_package_directory(tmp_path) -> None:
    output_dir = tmp_path / "modules"
    env = {**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "scripts/scaffold_indicator.py",
            "demo_package",
            "--output-dir",
            str(output_dir),
        ],
        cwd=".",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    init_path = output_dir / "demo_package" / "__init__.py"
    assert result.returncode == 0, result.stderr
    assert init_path.is_file()
    assert result.stdout.strip().endswith("demo_package/__init__.py")
    assert not (output_dir / "demo_package.py").exists()


def test_indicator_scaffold_rejects_invalid_ids() -> None:
    assert validate_indicator_id("demo_signal") == "demo_signal"
    for invalid in ("DemoSignal", "demo-signal", "demo__signal", "_demo", "demo_"):
        try:
            validate_indicator_id(invalid)
        except ValueError:
            continue
        raise AssertionError(f"invalid indicator id accepted: {invalid}")


def test_low_coupling_indicators_are_removable_startup_packages() -> None:
    modules = {module.id: module for module in indicator_modules()}
    modules_root = Path("src/aef_terminal/indicators/modules")
    low_ids = ("obvious_failure", "w5_structure", "wolfe_structure")

    for indicator_id in low_ids:
        package_root = modules_root / indicator_id
        module = modules[indicator_id]
        assert (package_root / "__init__.py").is_file()
        assert not (modules_root / f"{indicator_id}.py").exists()
        assert module.spec.default_calc is False
        assert module.params_builder_ref.startswith(
            f"aef_terminal.indicators.modules.{indicator_id}:"
        )
        assert module.spec.runtime_payload_schema_refs == ("overlay_contract",)

    assert modules["w5_structure"].candidate_promoter_ref.startswith(
        "aef_terminal.indicators.modules.w5_structure:"
    )
    assert modules["wolfe_structure"].candidate_promoter_ref.startswith(
        "aef_terminal.indicators.modules.wolfe_structure:"
    )

    core_sources = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/aef_terminal/indicators/runtime_params.py",
            "src/aef_terminal/indicators/runtime.py",
            "src/aef_terminal/indicators/module_contract.py",
            "src/aef_terminal/engine/context_cache.py",
            "src/aef_terminal/engine/analyze/inputs.py",
            "src/aef_terminal/engine/analyze/structure.py",
            "src/aef_terminal/engine/analyze/indicator_runtime.py",
            "src/aef_terminal/signals/primitives.py",
        )
    )
    for indicator_id in low_ids:
        assert f"aef_terminal.indicators.modules.{indicator_id}" not in core_sources
    assert "include_w5" not in core_sources
    assert "w5_candidates" not in core_sources
    assert "effective_wolfe_pivot" not in core_sources


def test_rounded_reversal_is_a_manifest_driven_non_trading_structure_module() -> None:
    module = {item.id: item for item in indicator_modules()}["rounded_reversal"]
    spec = module.spec
    manifest = indicator_manifest()["rounded_reversal"]

    assert Path("src/aef_terminal/indicators/modules/rounded_reversal/__init__.py").is_file()
    assert not Path("src/aef_terminal/prototypes/rounded_reversal/__init__.py").exists()
    assert spec.group == "structure"
    assert spec.pipeline_stage == "structure"
    assert spec.pipeline_order == 105
    assert spec.execution_lanes == ("primary",)
    assert spec.candidate_promoter == "none"
    assert spec.paper_tradable is False
    assert spec.default_calc is False
    assert spec.default_visible is True
    assert spec.show_in_runtime is False
    assert spec.renderer_kind == "generic"
    assert spec.renderer_ref == "generic_overlay_renderer"
    assert spec.renderer_primitives == ("box", "line")
    assert spec.empty_result["version"] == "2.0-dual-track-adaptive"
    assert spec.empty_result["track_states"] == []
    assert spec.optional_context == (
        "absorption_trap",
        "tick_flow",
        "channel_master",
        "market_spotlight",
    )
    assert spec.shared_context_refs == (
        "vsa_facts",
        "provider_mtf_quality",
    )
    assert [
        (
            request.timeframe,
            request.history_bars,
            request.role,
        )
        for request in spec.confirmed_bar_context
    ] == [("1m", 256, "independent_scan")]
    assert module.ui_js_assets == ()
    assert module.ui_css_assets == ()
    controls = {control["key"]: control for control in manifest["controls"]}
    assert controls["trackMode"]["options"] == ["both", "fast", "slow"]
    assert controls["trackMode"]["storage_key"] == "roundedReversalTrackMode"
    assert controls["sensitivity"]["options"] == ["early", "balanced", "strict"]
    assert controls["sensitivity"]["storage_key"] == "roundedReversalSensitivity"
    assert controls["mtfRejections"]["storage_key"] == "roundedReversalMtfRejections"
    assert controls["microMinRejections"]["storage_key"] == ("roundedReversalMicroMinRejections")
    assert manifest["ui"]["calc_key"] == ("roundedReversalCalcEnabled")
    assert manifest["ui"]["visible_key"] == "roundedReversalVisible"
    assert manifest["ui"]["process_control_id"] == ("rounded-reversal-process")
    assert manifest["runtime_payload_schema_refs"] == ["overlay_contract"]
    assert "track_states" in manifest["runtime_payload_contract"]["top_level"]
    assert {"track", "available_at_ts", "adaptive_thresholds", "gate_margins"} <= set(
        manifest["runtime_payload_contract"]["track_states"]
    )
    assert manifest["runtime_payload_compact"]["track_states"] == {
        "limit": 2,
        "fields": "runtime_contract",
    }
    assert "rounded_reversal" not in paper_tradable_indicator_ids()
    assert "rounded_reversal" in indicator_ids_for_shared_context("vsa_facts")
    assert "rounded_reversal" in indicator_ids_for_shared_context("provider_mtf_quality")


def test_rounded_reversal_package_separates_contract_calculation_and_adapter() -> None:
    package_root = Path("src/aef_terminal/indicators/modules/rounded_reversal")
    facade = (package_root / "__init__.py").read_text(encoding="utf-8")
    contracts = (package_root / "contracts.py").read_text(encoding="utf-8")
    calculation = (package_root / "calculation.py").read_text(encoding="utf-8")
    adaptive = (package_root / "adaptive.py").read_text(encoding="utf-8")
    fast = (package_root / "fast.py").read_text(encoding="utf-8")

    assert "class RoundedReversalParams" in contracts
    assert "def build_params" in contracts
    assert "def rounded_reversal" not in contracts
    assert "def rounded_reversal" in calculation
    assert "def adaptive_rejection_summary" in adaptive
    assert "def fast_rounded_reversal" in fast
    assert "class RoundedReversalParams" not in calculation
    assert "def build_execution_spec" not in calculation
    assert "def build_execution_spec" in facade
    assert "def _rounded_reversal_with_confirmed_context" in facade
    assert "def rounded_reversal" not in facade
    assert "class RoundedReversalParams" not in facade


def test_confirmed_bar_context_requests_follow_indicator_calc_state() -> None:
    disabled = enabled_confirmed_bar_context_requests(
        {
            "breakout_accumulation": {"enabled": False},
            "linda_volume": {"enabled": False},
            "market_spotlight": {"enabled": False},
            "obvious_failure": {"enabled": False},
            "rounded_reversal": {"enabled": False},
            "trade_setup_engine": {"enabled": False},
            "w5_structure": {"enabled": False},
        }
    )
    assert disabled == {}

    enabled = enabled_confirmed_bar_context_requests(
        {
            "breakout_accumulation": {"enabled": True},
            "linda_volume": {"enabled": True},
            "market_spotlight": {"enabled": True},
            "obvious_failure": {"enabled": True},
            "rounded_reversal": {"enabled": True},
            "trade_setup_engine": {"enabled": True},
            "w5_structure": {"enabled": True},
        }
    )
    assert set(enabled) == {"1m"}
    assert {indicator_id for indicator_id, _request in enabled["1m"]} == {
        "breakout_accumulation",
        "linda_volume",
        "market_spotlight",
        "obvious_failure",
        "rounded_reversal",
        "trade_setup_engine",
        "w5_structure",
    }
    assert {request.role for _indicator_id, request in enabled["1m"]} == {
        "independent_scan",
        "lower_timeframe_confirmation",
    }


def test_obvious_failure_uses_confirmed_1m_only_as_parent_preview() -> None:
    from aef_terminal.indicators.modules.obvious_failure import (
        build_execution_spec as build_obvious_failure_execution_spec,
    )

    base = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    parent_bars = [
        Bar(
            "ES",
            base + timedelta(minutes=index * 5),
            100.0,
            101.0,
            99.0,
            100.1,
            1000.0,
            "5m",
        )
        for index in range(30)
    ]
    micro_bars = [
        Bar(
            "ES",
            base + timedelta(minutes=index),
            100.0,
            101.0,
            99.0,
            100.1,
            1000.0,
            "1m",
        )
        for index in range(150)
    ]
    micro_bars.extend(
        [
            Bar(
                "ES",
                base + timedelta(minutes=150),
                100.8,
                102.2,
                100.4,
                101.7,
                2600.0,
                "1m",
            ),
            Bar(
                "ES",
                base + timedelta(minutes=151),
                101.5,
                101.6,
                99.8,
                100.3,
                2800.0,
                "1m",
            ),
        ]
    )
    raw_params = {
        "obvious_failure": {
            "enabled": True,
            "lookback": 24,
            "min_score": 60,
        }
    }
    defaults = indicator_defaults_from_params({})
    runtime_params = build_indicator_runtime_params(raw_params, defaults)
    ctx = IndicatorRunContext(
        confirmed_bars=parent_bars,
        live_signal_bars=parent_bars,
        latest=parent_bars[-1],
        analysis_latest=parent_bars[-1],
        runtime_params=runtime_params,
        indicator_params=raw_params,
        features={},
        indicator_bundle={},
        confirmed_bar_context={"1m": micro_bars},
        confirmed_bar_context_quality={
            "1m": {
                "ok": True,
                "provider_complete": True,
            }
        },
    )
    spec = build_obvious_failure_execution_spec(ctx)
    promoted_latest_actions: list[str] = []

    def promoter(
        _indicator_id: str,
        indicator: dict[str, object],
    ) -> list[SignalCandidate]:
        latest = indicator["latest"]
        assert isinstance(latest, dict)
        signal = latest["signal"]
        assert isinstance(signal, dict)
        promoted_latest_actions.append(str(signal["action"]))
        return []

    result, promoted = execute_indicator_spec(
        spec,
        runner=run_pipeline_indicator,
        promoter=promoter,
    )

    assert promoted == []
    assert promoted_latest_actions == ["WAIT"]
    assert result["latest"]["signal"]["action"] == "WAIT"
    assert result["preview"]["signal"]["action"] == "GO"
    assert result["preview"]["signal"]["source_tf"] == "1m"
    assert result["preview"]["range"]["lookback"] == 120
    assert result["preview"]["confirmed"] is False
    assert result["preview_events"][-1]["confirmed"] is False
    assert result["status"]["state_code"] == "live_preview_signal"


def test_intrabar_preview_consumers_use_confirmed_1m_parent_bar(
    monkeypatch,
) -> None:
    import aef_terminal.indicators.modules.breakout_accumulation as module
    import aef_terminal.indicators.modules.linda_volume as linda_module
    import aef_terminal.indicators.modules.market_spotlight as spotlight_module
    import aef_terminal.indicators.modules.trade_setup_engine as setup_module

    base = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    parent_bars = [
        Bar(
            "ES",
            base,
            100.0,
            101.0,
            99.0,
            100.0,
            1000.0,
            "5m",
        )
    ]
    quote_preview = Bar(
        "ES",
        base + timedelta(minutes=5),
        100.0,
        120.0,
        80.0,
        119.0,
        9000.0,
        "5m",
        closed=False,
    )
    micro_bars = [
        Bar(
            "ES",
            base + timedelta(minutes=index),
            100.0 + index,
            101.0 + index,
            99.0 + index,
            100.5 + index,
            100.0,
            "1m",
        )
        for index in (5, 6, 7)
    ]
    calls: list[tuple[list[Bar], dict[str, object]]] = []

    def calculate(bars, **kwargs):
        calls.append((list(bars), dict(kwargs)))
        return {
            "version": "test",
            "series": [],
            "events": [],
            "latest": {
                "ts": bars[-1].ts.isoformat(),
                "state": "WAIT",
            },
        }

    monkeypatch.setattr(module, "breakout_accumulation", calculate)
    monkeypatch.setattr(linda_module, "linda_volume", calculate)
    monkeypatch.setattr(spotlight_module, "market_spotlight", calculate)
    monkeypatch.setattr(setup_module, "trade_setup_engine", calculate)
    params = SimpleNamespace(zones=True, labels=True, plan=True)
    ctx = IndicatorRunContext(
        confirmed_bars=parent_bars,
        live_signal_bars=[*parent_bars, quote_preview],
        latest=quote_preview,
        analysis_latest=parent_bars[-1],
        runtime_params=_runtime_params(
            enabled={
                "breakout_accumulation": True,
                "linda_volume": True,
                "market_spotlight": True,
                "trade_setup_engine": True,
            },
            by_indicator={
                indicator_id: params
                for indicator_id in (
                    "breakout_accumulation",
                    "linda_volume",
                    "market_spotlight",
                    "trade_setup_engine",
                )
            },
        ),
        indicator_params={"breakout_accumulation": {"enabled": True}},
        features={"canonical": "parent"},
        live_preview_active=True,
        confirmed_bar_context={"1m": micro_bars},
        confirmed_bar_context_quality={"1m": {"ok": True}},
    )

    spec = module.build_execution_spec(ctx)
    assert spec.preview_calculate is not None
    spec.preview_calculate()

    preview_bars, preview_kwargs = calls[-1]
    assert preview_bars[:-1] == parent_bars
    assert preview_bars[-1].source == "confirmed-context:1m"
    assert preview_bars[-1].close == micro_bars[-1].close
    assert preview_bars[-1].close != quote_preview.close
    assert preview_bars[-1].closed is False
    assert preview_kwargs["features"] is None
    assert preview_kwargs["preview_only"] is True
    assert spec.preview_event_ts == micro_bars[0].ts.isoformat()

    spotlight_spec = spotlight_module.build_execution_spec(ctx)
    assert spotlight_spec.preview_calculate_with_context is not None
    spotlight_spec.preview_calculate_with_context(
        {"impulse_fib": {"latest": {"state": "confirmed"}}}
    )

    spotlight_preview_bars, spotlight_kwargs = calls[-1]
    assert spotlight_preview_bars[:-1] == parent_bars
    assert spotlight_preview_bars[-1].closed is False
    assert spotlight_kwargs["facts"] is None
    assert spotlight_kwargs["market_series"] is None
    assert spotlight_kwargs["indicator_context"] == {
        "impulse_fib": {"latest": {"state": "confirmed"}}
    }

    linda_spec = linda_module.build_execution_spec(ctx)
    assert linda_spec.preview_calculate_with_context is not None
    spotlight_preview = {
        "market_context": {"direction": "short"},
    }
    linda_spec.preview_calculate_with_context(
        {
            "market_spotlight": {
                "market_context": {"direction": "long"},
                "preview": spotlight_preview,
            }
        }
    )

    linda_preview_bars, linda_kwargs = calls[-1]
    assert linda_preview_bars[:-1] == parent_bars
    assert linda_preview_bars[-1].closed is False
    assert linda_kwargs["facts"] is None
    assert linda_kwargs["market_series"] is None
    assert linda_kwargs["market_session"] is spotlight_preview
    assert linda_kwargs["preview_only"] is True

    setup_spec = setup_module.build_execution_spec(ctx)
    assert setup_spec.preview_calculate is not None
    setup_spec.preview_calculate()

    setup_preview_bars, setup_kwargs = calls[-1]
    assert setup_preview_bars[:-1] == parent_bars
    assert setup_preview_bars[-1].closed is False
    assert setup_kwargs["vsa_breakout_facts"] is None
    assert setup_kwargs["preview_only"] is True
    assert setup_spec.preview_event_ts == micro_bars[0].ts.isoformat()


def test_context_sensitive_intrabar_previews_execute_real_calculations() -> None:
    import aef_terminal.indicators.modules.linda_volume as linda_module
    import aef_terminal.indicators.modules.market_spotlight as spotlight_module
    import aef_terminal.indicators.modules.obvious_failure as obvious_module
    import aef_terminal.indicators.modules.trade_setup_engine as setup_module

    base = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    parent_bars = [
        Bar(
            "ES",
            base + timedelta(minutes=index * 5),
            100.0 + index * 0.02,
            100.8 + index * 0.02,
            99.5 + index * 0.02,
            100.3 + index * 0.02,
            1000.0 + index,
            "5m",
        )
        for index in range(260)
    ]
    next_parent_ts = parent_bars[-1].ts + timedelta(minutes=5)
    micro_bars = [
        Bar(
            "ES",
            next_parent_ts + timedelta(minutes=index),
            105.4 + index * 0.1,
            105.8 + index * 0.1,
            105.1 + index * 0.1,
            105.6 + index * 0.1,
            250.0 + index,
            "1m",
        )
        for index in range(3)
    ]
    raw_params = {
        "market_spotlight": {"enabled": True},
        "linda_volume": {"enabled": True},
        "obvious_failure": {"enabled": True},
        "trade_setup_engine": {"enabled": True},
    }
    defaults = indicator_defaults_from_params({})
    runtime_params = build_indicator_runtime_params(raw_params, defaults)
    feature_context = build_feature_context(parent_bars, defaults=defaults)
    ctx = IndicatorRunContext(
        confirmed_bars=parent_bars,
        live_signal_bars=parent_bars,
        latest=parent_bars[-1],
        analysis_latest=parent_bars[-1],
        runtime_params=runtime_params,
        indicator_params=raw_params,
        features=feature_context.values,
        feature_context=feature_context,
        indicator_bundle={},
        confirmed_bar_context={"1m": micro_bars},
        confirmed_bar_context_quality={"1m": {"ok": True}},
        shared={},
    )

    spotlight_spec = spotlight_module.build_execution_spec(ctx)
    assert spotlight_spec.preview_calculate_with_context is not None
    spotlight_preview = spotlight_spec.preview_calculate_with_context({})
    assert spotlight_preview["latest"]["ts"] == next_parent_ts.isoformat()
    assert spotlight_preview["latest"]["market_context"]["closed"] is False

    linda_spec = linda_module.build_execution_spec(ctx)
    assert linda_spec.preview_calculate_with_context is not None
    linda_preview = linda_spec.preview_calculate_with_context(
        {
            "market_spotlight": {
                "preview": spotlight_preview["latest"],
            }
        }
    )
    assert linda_preview["latest"]["ts"] == next_parent_ts.isoformat()

    setup_spec = setup_module.build_execution_spec(ctx)
    assert setup_spec.preview_calculate is not None
    setup_preview = setup_spec.preview_calculate()
    assert setup_preview["latest"]["ts"] == next_parent_ts.isoformat()
    assert setup_preview["latest"]["signal"]["source"] == "trade_setup_engine"

    inactive_ctx = replace(
        ctx,
        confirmed_bar_context={},
        confirmed_bar_context_quality={},
    )
    assert (
        spotlight_module.build_execution_spec(inactive_ctx).preview_calculate_with_context is None
    )
    assert linda_module.build_execution_spec(inactive_ctx).preview_calculate_with_context is None
    assert setup_module.build_execution_spec(inactive_ctx).preview_calculate is None
    assert obvious_module.build_execution_spec(inactive_ctx).preview_calculate is None


def test_standard_indicators_are_drop_in_packages_with_private_contracts() -> None:
    modules = {module.id: module for module in indicator_modules()}
    modules_root = Path("src/aef_terminal/indicators/modules")
    module_contract_source = Path("src/aef_terminal/indicators/module_contract.py").read_text(
        encoding="utf-8"
    )
    package_js_assets = {
        "breakout_accumulation": ("client.js",),
        "impulse_fib": ("client.js",),
        "option_reversal": ("client.js",),
        "smc_channels": ("client.js",),
        "trade_setup_engine": (),
    }
    package_css_assets = {
        "breakout_accumulation": (),
        "impulse_fib": (),
        "option_reversal": (),
        "smc_channels": (),
        "trade_setup_engine": (),
    }
    obsolete_shared_schema_ids = {
        "breakout_accumulation": "breakout_accumulation_row",
        "impulse_fib": "impulse_fib_row",
        "option_reversal": "option_reversal_row",
        "smc_channels": "smc_channels_event",
        "trade_setup_engine": "trade_setup_engine_event",
    }

    for indicator_id, js_assets in package_js_assets.items():
        package_root = modules_root / indicator_id
        module = modules[indicator_id]
        assert package_root.is_dir()
        assert (package_root / "__init__.py").is_file()
        assert not (modules_root / f"{indicator_id}.py").exists()
        assert module.spec.default_calc is False
        assert module.params_builder_ref.startswith(
            f"aef_terminal.indicators.modules.{indicator_id}:"
        )
        assert module.spec.runtime_payload_schema_refs == ("overlay_contract",)
        assert module.spec.runtime_payload_contract
        assert module.ui_js_assets == js_assets
        assert module.ui_css_assets == package_css_assets[indicator_id]
        for asset in (*js_assets, *package_css_assets[indicator_id]):
            assert (package_root / asset).is_file()
        assert obsolete_shared_schema_ids[indicator_id] not in module_contract_source

    assert modules["option_reversal"].spec.show_in_runtime is False
    assert modules["option_reversal"].spec.table_contract == "indicator-table-v1"
    assert modules["option_reversal"].spec.renderer_placements == ("price", "table")


def test_market_spotlight_and_linda_are_drop_in_packages_with_private_contracts() -> None:
    modules = {module.id: module for module in indicator_modules()}
    modules_root = Path("src/aef_terminal/indicators/modules")
    module_contract_source = Path("src/aef_terminal/indicators/module_contract.py").read_text(
        encoding="utf-8"
    )
    package_assets = {
        "market_spotlight": (("client.js",), ("styles.css",)),
        "linda_volume": (("client.js",), ()),
    }

    for indicator_id, (js_assets, css_assets) in package_assets.items():
        package_root = modules_root / indicator_id
        module = modules[indicator_id]

        assert package_root.is_dir()
        assert (package_root / "__init__.py").is_file()
        assert not (modules_root / f"{indicator_id}.py").exists()
        assert module.spec.default_calc is False
        assert module.spec.runtime_payload_schema_refs == ("overlay_contract",)
        assert module.spec.runtime_payload_contract
        assert module.params_builder_ref.startswith(
            f"aef_terminal.indicators.modules.{indicator_id}:"
        )
        assert module.ui_js_assets == js_assets
        assert module.ui_css_assets == css_assets
        for asset in (*js_assets, *css_assets):
            assert (package_root / asset).is_file()

    assert modules["linda_volume"].spec.optional_context == ("market_spotlight",)
    assert modules["linda_volume"].candidate_promoter_ref == (
        "aef_terminal.indicators.modules.linda_volume.candidates:"
        "signal_candidates_from_linda_indicator"
    )
    assert modules["linda_volume"].spec.runtime_payload_compact_contract
    assert not Path("src/aef_terminal/signals/linda_candidates.py").exists()
    assert "market_context_row" not in module_contract_source
    assert "linda_volume_row" not in module_contract_source


def test_martin_carlo_is_a_drop_in_package_with_private_contracts() -> None:
    modules = {module.id: module for module in indicator_modules()}
    modules_root = Path("src/aef_terminal/indicators/modules")
    package_root = modules_root / "martin_carlo"
    module = modules["martin_carlo"]
    manifest = indicator_manifest()["martin_carlo"]
    module_contract_source = Path("src/aef_terminal/indicators/module_contract.py").read_text(
        encoding="utf-8"
    )
    indicator_contract_source = Path("src/aef_terminal/indicators/contracts.py").read_text(
        encoding="utf-8"
    )
    analysis_store_source = Path("src/aef_terminal/ui/services/market_analysis_store.py").read_text(
        encoding="utf-8"
    )

    assert package_root.is_dir()
    assert (package_root / "__init__.py").is_file()
    assert (package_root / "client.js").is_file()
    assert not (modules_root / "martin_carlo.py").exists()
    assert module.spec.default_calc is False
    assert module.spec.optional_context == (
        "impulse_fib",
        "absorption_trap",
        "w5_structure",
        "wolfe_structure",
        "option_reversal",
    )
    assert module.spec.analysis_refresh_interval_seconds is None
    assert module.params_builder_ref.startswith("aef_terminal.indicators.modules.martin_carlo:")
    assert module.ui_js_assets == ("client.js",)
    assert module.ui_css_assets == ()
    assert module.spec.runtime_payload_schema_refs == ("overlay_contract",)
    assert module.spec.runtime_payload_contract
    assert module.spec.renderer_kind == "custom"
    assert module.spec.renderer_ref == "martin_carlo_forecast"
    assert module.spec.renderer_primitives == ("custom",)
    assert manifest["renderer_contract"]["ref"] == "martin_carlo_forecast"
    assert manifest["renderer_contract"]["primitives"] == ["custom"]
    assert "forecast" in manifest["runtime_payload_contract"]["latest"]
    assert "renderer_ref" in manifest["runtime_payload_contract"]["overlays"]
    assert "payload" in manifest["runtime_payload_contract"]["overlays"]
    assert "render_key" in manifest["runtime_payload_contract"]["overlays"]
    assert "payload" in manifest["runtime_payload_compact_contract"]["overlays"]
    assert manifest["extensions"]["ui_js_assets"] == ["client.js"]
    assert manifest["analysis_refresh_interval_seconds"] is None
    assert "MARTIN_CARLO_LATEST_FIELDS" not in module_contract_source
    assert "martin_carlo_latest" not in module_contract_source
    assert "martin_carlo_forecast" not in indicator_contract_source
    assert "custom" in indicator_contracts.ALLOWED_OVERLAY_TYPES
    assert indicator_contracts.OVERLAY_TYPE_PLACEMENTS["custom"] == "price"
    assert "martin_carlo" not in analysis_store_source


@pytest.mark.parametrize(
    ("removed_id", "survivor_id", "survivor_assets"),
    (
        ("market_spotlight", "linda_volume", ("client.js",)),
        ("linda_volume", "market_spotlight", ("client.js", "styles.css")),
    ),
)
def test_market_spotlight_and_linda_packages_are_independently_removable(
    tmp_path: Path,
    removed_id: str,
    survivor_id: str,
    survivor_assets: tuple[str, ...],
) -> None:
    copied_src = tmp_path / "src"
    copied_package = copied_src / "aef_terminal"
    shutil.copytree(Path("src/aef_terminal"), copied_package)
    shutil.rmtree(copied_package / "indicators" / "modules" / removed_id)
    script = f"""
from pathlib import Path

from aef_terminal.indicators.module_discovery import (
    discover_indicator_modules,
    indicator_module_asset_paths,
)
from aef_terminal.indicators.registry import (
    INDICATOR_REGISTRY,
    indicator_connection_manifest,
    indicator_manifest,
    indicator_stage_ids,
)

removed_id = {removed_id!r}
survivor_id = {survivor_id!r}
survivor_assets = {survivor_assets!r}
discovered_ids = {{module.id for module in discover_indicator_modules()}}
assert removed_id not in discovered_ids
assert survivor_id in discovered_ids
assert removed_id not in INDICATOR_REGISTRY
assert survivor_id in INDICATOR_REGISTRY
manifest = indicator_manifest()
assert survivor_id in manifest
for asset_name in survivor_assets:
    asset_kind = "css" if asset_name.endswith(".css") else "js"
    expected = (
        Path({str(copied_package)!r})
        / "indicators"
        / "modules"
        / survivor_id
        / asset_name
    ).resolve()
    assert expected in indicator_module_asset_paths(asset_kind)

if removed_id == "market_spotlight":
    assert manifest["linda_volume"]["optional_context"] == ["market_spotlight"]
    linda = indicator_connection_manifest(
        {{"linda_volume": {{"enabled": True}}}},
        group="primary",
    )["linda_volume"]
    dependency = next(
        item
        for item in linda["connection"]["dependencies"]
        if item["id"] == "market_spotlight"
    )
    assert dependency == {{
        "id": "market_spotlight",
        "enabled": False,
        "registered": False,
        "required": False,
        "optional_context": True,
        "linked": False,
    }}
    assert linda["enabled"] is True
    assert linda["dependencies_ready"] is True
    assert linda["connected"] is True

    from datetime import UTC, datetime, timedelta

    from aef_terminal.domain import Bar
    from aef_terminal.engine.analyze.indicator_runtime import run_pipeline_indicator
    from aef_terminal.engine.indicator_adapters import (
        IndicatorRunContext,
        indicator_execution_specs,
    )
    from aef_terminal.engine.indicator_pipeline import execute_indicator_pipeline
    from aef_terminal.engine.vsa_context import VsaVolumeParams
    from aef_terminal.features.context import build_feature_context
    from aef_terminal.features.provider_session import ProviderSessionReset
    from aef_terminal.features.vsa import vsa_facts
    from aef_terminal.indicators.defaults import indicator_defaults_from_params
    from aef_terminal.indicators.runtime_params import build_indicator_runtime_params
    from aef_terminal.runtime.instruments import PROFILES

    start = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    bars = [
        Bar(
            "ES",
            start + timedelta(minutes=index),
            5000 + index * 0.2,
            5001 + index * 0.2,
            4999 + index * 0.2,
            5000.5 + index * 0.2,
            1000 + index,
            "1m",
        )
        for index in range(120)
    ]
    defaults = indicator_defaults_from_params({{}})
    settings = {{
        indicator_id: {{"enabled": indicator_id == "linda_volume"}}
        for indicator_id in indicator_stage_ids("primary")
    }}
    runtime_params = build_indicator_runtime_params(settings, defaults)
    feature_context = build_feature_context(
        bars,
        instrument_profile=PROFILES["ES"],
        defaults=defaults,
    )
    vwap_session = ProviderSessionReset(
        available=True,
        source="test",
        reason_code="",
        calendar="continuous_24_7",
    )
    context = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=settings,
        features=feature_context.values,
        instrument_profile=PROFILES["ES"],
        vwap_session=vwap_session,
        shared={{
            "vsa_facts": vsa_facts(
                bars,
                VsaVolumeParams(),
                vwap_session=vwap_session,
            ),
            "vix_context": None,
            "market_series": None,
        }},
    )
    specs = indicator_execution_specs(context, stage="primary")
    assert [spec.id for spec in specs] == ["linda_volume"]
    result = execute_indicator_pipeline(
        specs,
        runner=run_pipeline_indicator,
        promoter=lambda *_args: [],
    )
    assert result.indicators["linda_volume"]["series"]
    assert isinstance(result.indicators["linda_volume"]["latest"], dict)
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(copied_src),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_martin_carlo_package_is_physically_removable(
    tmp_path: Path,
) -> None:
    copied_src = tmp_path / "src"
    copied_package = copied_src / "aef_terminal"
    shutil.copytree(Path("src/aef_terminal"), copied_package)
    shutil.rmtree(copied_package / "indicators" / "modules" / "martin_carlo")
    script = f"""
import asyncio
from pathlib import Path

from aef_terminal.indicators.module_discovery import (
    discover_indicator_modules,
    indicator_module_asset_paths,
)
from aef_terminal.indicators.registry import (
    INDICATOR_REGISTRY,
    indicator_connection_manifest,
    indicator_manifest,
    indicator_stage_ids,
)
from aef_terminal.ui.asset_services import (
    martincall_html_async,
    martincall_js_source_sync,
)
from aef_terminal.ui.services.market_analysis_store import (
    market_analysis_refresh_seconds,
)

discovered_ids = {{module.id for module in discover_indicator_modules()}}
assert "martin_carlo" not in discovered_ids
assert "martin_carlo" not in INDICATOR_REGISTRY
assert "martin_carlo" not in indicator_manifest()
assert "martin_carlo" not in indicator_stage_ids("post_decision")
assert all(
    path.parent
    != (
        Path({str(copied_package)!r})
        / "indicators"
        / "modules"
        / "martin_carlo"
    ).resolve()
    for path in indicator_module_asset_paths("js")
)

connections = indicator_connection_manifest(
    {{"ai_third_opinion": {{"enabled": True}}}},
    stage="post_decision",
)
advisor = connections["ai_third_opinion"]
dependency = next(
    item
    for item in advisor["connection"]["dependencies"]
    if item["id"] == "martin_carlo"
)
assert dependency == {{
    "id": "martin_carlo",
    "enabled": False,
    "registered": False,
    "required": False,
    "optional_context": True,
    "linked": False,
}}
assert advisor["enabled"] is True
assert advisor["dependencies_ready"] is True
assert advisor["connected"] is True

stale_payload = {{
    "indicator_params": {{
        "martin_carlo": {{"enabled": True}},
    }},
}}
assert market_analysis_refresh_seconds(
    stale_payload,
    default_seconds=10.0,
) == 10.0

html = asyncio.run(martincall_html_async())
js = martincall_js_source_sync()
assert 'id="martin-carlo-' not in html
assert '"martin_carlo":{{' not in js
assert "martin_carlo_forecast" not in js
assert "drawMartinCarloForecastOverlay" not in js
assert "maybeScheduleMartinCarloAnalysisRefresh" not in js
assert "martinCarloAttentionMessages" not in js
assert 'registerIndicatorProcessEffect("martin_carlo_refresh_reset"' not in js
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(copied_src),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_channel_master_is_a_removable_backend_owned_composite_package() -> None:
    modules = {module.id: module for module in indicator_modules()}
    module = modules["channel_master"]
    package_root = Path("src/aef_terminal/indicators/modules/channel_master")
    manifest = indicator_manifest()["channel_master"]

    assert package_root.is_dir()
    assert (package_root / "__init__.py").is_file()
    assert (package_root / "client.js").is_file()
    assert not Path("src/aef_terminal/indicators/modules/channel_master.py").exists()
    assert not Path("src/aef_terminal/features/manual_channels.py").exists()
    assert module.ui_js_assets == ("client.js",)
    assert module.ui_css_assets == ()
    assert module.spec.pipeline_stage == "primary"
    assert module.spec.module_type == "context"
    assert module.spec.default_calc is False
    assert module.adapter_ref.startswith("aef_terminal.indicators.modules.channel_master:")
    assert module.candidate_promoter_ref.startswith(
        "aef_terminal.indicators.modules.channel_master:"
    )
    assert indicator_module_asset_paths("js").count((package_root / "client.js").resolve()) == 1
    assert manifest["extensions"]["ui_js_assets"] == ["client.js"]
    assert manifest["runtime_payload_schema_refs"] == []
    assert manifest["service_payload_schema_refs"] == []
    assert manifest["service_payload_contract"] == {}
    assert {"decision_price", "primary_path", "no_fade"} <= set(
        manifest["runtime_payload_contract"]["latest"]
    )
    top_level = set(manifest["runtime_payload_contract"]["top_level"])
    assert {
        "analysis_logical_index",
        "analysis_ts",
        "decision_anchor_ts",
        "decision_available_at",
        "decision_bar_offset",
    } <= top_level
    assert "analysis_slot" not in top_level
    assert {
        "definition_revision",
        "slope_per_confirmed_bar",
    } <= set(manifest["runtime_payload_contract"]["channels"])

    core_sources = {
        "context_cache": Path("src/aef_terminal/engine/context_cache.py").read_text(
            encoding="utf-8"
        ),
        "analyze_inputs": Path("src/aef_terminal/engine/analyze/inputs.py").read_text(
            encoding="utf-8"
        ),
        "analyze_bars": Path("src/aef_terminal/engine/analyze/bars.py").read_text(encoding="utf-8"),
        "asset_services": Path("src/aef_terminal/ui/asset_services.py").read_text(encoding="utf-8"),
        "template": Path("src/aef_terminal/ui/assets/templates/index.html").read_text(
            encoding="utf-8"
        ),
        "core_state": Path("src/aef_terminal/ui/assets/js/10-core-state.js").read_text(
            encoding="utf-8"
        ),
        "drawing_geometry": Path("src/aef_terminal/ui/assets/js/60-drawing-geometry.js").read_text(
            encoding="utf-8"
        ),
        "drawing_tools": Path("src/aef_terminal/ui/assets/js/60-drawing-tools.js").read_text(
            encoding="utf-8"
        ),
        "drawing_storage": Path("src/aef_terminal/ui/assets/js/60-drawing-storage.js").read_text(
            encoding="utf-8"
        ),
    }
    assert "aef_terminal.features.manual_channels" not in "\n".join(core_sources.values())
    assert '"manual_channels": inputs.manual_channels' not in core_sources["analyze_bars"]
    assert "signal_candidates_from_manual_channels" not in core_sources["analyze_bars"]
    assert "60-channel-master.js" not in core_sources["asset_services"]
    assert "Channel Master" not in core_sources["template"]
    assert "channelMaster:" not in core_sources["core_state"]
    for source in (
        core_sources["drawing_geometry"],
        core_sources["drawing_tools"],
    ):
        assert "maybeDrawChannelMaster" not in source
        assert "beginChannelMasterArrowSelection" not in source
        assert "flushChannelMasterArrowSelection" not in source
    assert "scheduleManualChannelAnalysisReload" not in core_sources["drawing_storage"]


def test_calc_enabled_fails_closed_for_an_uninstalled_indicator_id() -> None:
    context = SimpleNamespace(
        runtime_params=_runtime_params(enabled={"martin_carlo": True, "ai_third_opinion": True})
    )

    assert calc_enabled(context, "martin_carlo") is True
    assert calc_enabled(context, "option_reversal") is False


def test_indicator_module_catalog_reports_installed_available_and_invalid_modules(
    tmp_path, monkeypatch
) -> None:
    package_dir = tmp_path / "catalog_indicators"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "demo_signal.py").write_text(
        indicator_module_template(
            "demo_signal",
            label="Demo Signal",
            package_name="catalog_indicators",
            pipeline_order=321,
        ),
        encoding="utf-8",
    )
    (package_dir / "empty_candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package_dir / "_helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    package_signal = package_dir / "package_signal"
    package_signal.mkdir()
    package_source = indicator_module_template(
        "package_signal",
        label="Package Signal",
        package_name="catalog_indicators",
        pipeline_order=322,
    )
    package_source = package_source.replace(
        "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
        (
            "from aef_terminal.indicators.module_contract import "
            "IndicatorModule, IndicatorSidebarSpec, IndicatorSpec"
        ),
    ).replace(
        '    adapter_ref="catalog_indicators.package_signal:build_execution_spec",',
        (
            '    adapter_ref="catalog_indicators.package_signal:build_execution_spec",\n'
            '    ui_js_assets=("client.js",),\n'
            '    ui_css_assets=("styles.css",),\n'
            "    sidebar=IndicatorSidebarSpec(\n"
            '        id="package", title="Package", mount_id="package-signal-mount"\n'
            "    ),"
        ),
    )
    (package_signal / "__init__.py").write_text(package_source, encoding="utf-8")
    (package_signal / "client.js").write_text(
        "window.packageSignalLoaded = true;\n", encoding="utf-8"
    )
    (package_signal / "styles.css").write_text(
        ".package-signal { display: block; }\n", encoding="utf-8"
    )
    (package_dir / "broken_candidate.py").write_text(
        "raise RuntimeError('catalog boom')\n", encoding="utf-8"
    )
    (package_dir / "bad_contract.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec('bad_signal', 'Bad Signal', 'missing calculate ref', {}, 'badSignal', '', state_key='badSignal', calc_key='badCalc')",
                ")",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_compact_schema.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_compact_schema',",
                "        'Bad Compact Schema',",
                "        'demo',",
                "        {},",
                "        ui_key='badCompactSchema',",
                "        calculate_ref='catalog_indicators.bad_compact_schema:calculate',",
                "        state_key='badCompactSchema',",
                "        calc_key='badCompactSchemaCalc',",
                "        runtime_payload_compact={'overlays': {'fields': 'compact_contract'}},",
                "    )",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_compact_contract.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_compact_contract',",
                "        'Bad Compact Contract',",
                "        'demo',",
                "        {},",
                "        ui_key='badCompactContract',",
                "        calculate_ref='catalog_indicators.bad_compact_contract:calculate',",
                "        state_key='badCompactContract',",
                "        calc_key='badCompactContractCalc',",
                "        runtime_payload_compact={'ghost': {'fields': 'contract'}},",
                "    )",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_explicit_compact_contract.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_explicit_compact_contract',",
                "        'Bad Explicit Compact Contract',",
                "        'demo',",
                "        {},",
                "        ui_key='badExplicitCompactContract',",
                "        calculate_ref='catalog_indicators.bad_explicit_compact_contract:calculate',",
                "        state_key='badExplicitCompactContract',",
                "        calc_key='badExplicitCompactContractCalc',",
                "        runtime_payload_contract={",
                "            'series': ('code',),",
                "            'events': ('code',),",
                "            'latest': ('code',),",
                "        },",
                "        runtime_payload_compact_contract={",
                "            'series': ['code'],",
                "            'events': (),",
                "            'latest': ('ghost',),",
                "        },",
                "    )",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_builder_ref.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_builder_ref',",
                "        'Bad Builder Ref',",
                "        'demo',",
                "        {},",
                "        ui_key='badBuilderRef',",
                "        calculate_ref='catalog_indicators.bad_builder_ref:calculate',",
                "        state_key='badBuilderRef',",
                "        calc_key='badBuilderRefCalc',",
                "    ),",
                "    params_builder_ref='catalog_indicators.bad_builder_ref:missing_builder',",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_promoter_signature.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_promoter_signature',",
                "        'Bad Promoter Signature',",
                "        'demo',",
                "        {},",
                "        ui_key='badPromoterSignature',",
                "        calculate_ref='catalog_indicators.bad_promoter_signature:calculate',",
                "        state_key='badPromoterSignature',",
                "        calc_key='badPromoterSignatureCalc',",
                "    ),",
                "    candidate_promoter_ref='catalog_indicators.bad_promoter_signature:promote',",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
                "def promote(indicator):",
                "    return []",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_builder_result.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_builder_result',",
                "        'Bad Builder Result',",
                "        'demo',",
                "        {},",
                "        ui_key='badBuilderResult',",
                "        calculate_ref='catalog_indicators.bad_builder_result:calculate',",
                "        state_key='badBuilderResult',",
                "        calc_key='badBuilderResultCalc',",
                "    ),",
                "    params_builder_ref='catalog_indicators.bad_builder_result:build_params',",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
                "def build_params(_raw, _defaults):",
                "    return object()",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_export.py").write_text(
        "INDICATOR_MODULE = object()\n",
        encoding="utf-8",
    )
    (package_dir / "mixed_export.py").write_text(
        "\n".join(
            [
                "from catalog_indicators.demo_signal import INDICATOR_MODULE as VALID",
                "INDICATOR_MODULES = (VALID, object())",
            ]
        ),
        encoding="utf-8",
    )
    (package_dir / "bad_empty_result.py").write_text(
        "\n".join(
            [
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
                "INDICATOR_MODULE = IndicatorModule(",
                "    IndicatorSpec(",
                "        'bad_empty_result',",
                "        'Bad Empty Result',",
                "        'demo',",
                "        {'series': 'not-a-list'},",
                "        ui_key='badEmptyResult',",
                "        calculate_ref='catalog_indicators.bad_empty_result:calculate',",
                "        state_key='badEmptyResult',",
                "        calc_key='badEmptyResultCalc',",
                "    ),",
                "    adapter_ref='catalog_indicators.bad_empty_result:build_execution_spec',",
                ")",
                "def calculate(*_args, **_kwargs):",
                "    return {}",
                "def build_execution_spec(_ctx):",
                "    return None",
            ]
        ),
        encoding="utf-8",
    )
    bad_indicator_id_source = indicator_module_template(
        "bad_indicator_id",
        label="Bad Indicator Id",
        package_name="catalog_indicators",
        pipeline_order=323,
    ).replace('id="bad_indicator_id",', 'id="Bad-Indicator",', 1)
    (package_dir / "bad_indicator_id.py").write_text(
        bad_indicator_id_source,
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    catalog = indicator_module_catalog("catalog_indicators")

    assert catalog["package"] == "catalog_indicators"
    assert any(
        entry["module"] == "catalog_indicators.demo_signal"
        and entry["indicator_ids"] == ("demo_signal",)
        for entry in catalog["installed"]
    )
    assert any(
        entry["module"] == "catalog_indicators.package_signal"
        and entry["indicator_ids"] == ("package_signal",)
        for entry in catalog["installed"]
    )
    assert any(
        entry["module"] == "catalog_indicators.empty_candidate"
        and entry["reason"] == "no_indicator_module"
        for entry in catalog["available"]
    )
    assert any(
        entry["module"] == "catalog_indicators._helper" and entry["reason"] == "internal"
        for entry in catalog["skipped"]
    )
    assert all(entry["module"] != "catalog_indicators._helper" for entry in catalog["available"])
    assert all(entry["module"] != "catalog_indicators._helper" for entry in catalog["invalid"])
    assert any(
        entry["module"] == "catalog_indicators.broken_candidate"
        and entry["reason"] == "import_error"
        and "catalog boom" in entry["detail"]
        for entry in catalog["invalid"]
    )
    discover_indicator_modules.cache_clear()
    try:
        discovered_ids = {module.id for module in discover_indicator_modules("catalog_indicators")}
        assert discovered_ids == {"demo_signal", "package_signal"}
        assert indicator_module_asset_paths("js", "catalog_indicators") == (
            (package_signal / "client.js").resolve(),
        )
        assert indicator_module_asset_paths("css", "catalog_indicators") == (
            (package_signal / "styles.css").resolve(),
        )
    finally:
        discover_indicator_modules.cache_clear()
    assert any(
        entry["module"] == "catalog_indicators.bad_contract"
        and entry["indicator_ids"] == ("bad_signal",)
        and entry["reason"] == "invalid_contract"
        and "missing_calculate_ref" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_compact_schema"
        and entry["indicator_ids"] == ("bad_compact_schema",)
        and entry["reason"] == "invalid_contract"
        and "unknown_runtime_payload_compact_schema_section:overlays" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_compact_contract"
        and entry["indicator_ids"] == ("bad_compact_contract",)
        and entry["reason"] == "invalid_contract"
        and "unknown_runtime_payload_compact_fields_ref:ghost:contract" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_explicit_compact_contract"
        and entry["indicator_ids"] == ("bad_explicit_compact_contract",)
        and entry["reason"] == "invalid_contract"
        and "runtime_payload_compact_contract_fields_not_tuple:series" in entry["detail"]
        and "empty_runtime_payload_compact_contract_fields:events" in entry["detail"]
        and ("runtime_payload_compact_contract_fields_not_in_runtime_contract:latest:ghost")
        in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_builder_ref"
        and entry["indicator_ids"] == ("bad_builder_ref",)
        and entry["reason"] == "invalid_contract"
        and "unresolvable_params_builder_ref:AttributeError" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_promoter_signature"
        and entry["indicator_ids"] == ("bad_promoter_signature",)
        and entry["reason"] == "invalid_contract"
        and "unsupported_candidate_promoter_ref_signature" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_builder_result"
        and entry["indicator_ids"] == ("bad_builder_result",)
        and entry["reason"] == "invalid_contract"
        and "invalid_params_builder_result:TypeError" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_export"
        and entry["reason"] == "invalid_contract"
        and "malformed_export:TypeError" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.mixed_export"
        and entry["reason"] == "invalid_contract"
        and "contains non-IndicatorModule values" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_empty_result"
        and entry["indicator_ids"] == ("bad_empty_result",)
        and entry["reason"] == "invalid_contract"
        and "invalid_empty_result:ValueError" in entry["detail"]
        for entry in catalog["invalid"]
    )
    assert any(
        entry["module"] == "catalog_indicators.bad_indicator_id"
        and entry["indicator_ids"] == ("Bad-Indicator",)
        and entry["reason"] == "invalid_contract"
        and "invalid_id" in entry["detail"]
        for entry in catalog["invalid"]
    )


def test_indicator_discovery_quarantines_dom_conflicts_and_hard_dependents(
    tmp_path,
    monkeypatch,
) -> None:
    package_name = "quarantine_indicators"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")

    for indicator_id in ("collision_a", "collision_b"):
        source = indicator_module_template(
            indicator_id,
            package_name=package_name,
        )
        source = source.replace(
            "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec",
            (
                "from aef_terminal.indicators.control_specs import _control\n"
                "from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec"
            ),
        ).replace(
            "        default_calc=False,\n",
            (
                "        default_calc=False,\n"
                "        controls=(_control("
                f"'{indicator_id}', 'Shared', 'toggle', True, "
                f"'{indicator_id}Setting', element_id='shared-plugin-control'"
                "),),\n"
            ),
        )
        (package_dir / f"{indicator_id}.py").write_text(
            source,
            encoding="utf-8",
        )

    hard_source = indicator_module_template(
        "hard_consumer",
        package_name=package_name,
    ).replace(
        '        signal_source="hard_consumer",\n',
        ('        signal_source="hard_consumer",\n        depends_on=("collision_a",),\n'),
    )
    (package_dir / "hard_consumer.py").write_text(
        hard_source,
        encoding="utf-8",
    )

    optional_source = indicator_module_template(
        "optional_consumer",
        package_name=package_name,
    ).replace(
        '        signal_source="optional_consumer",\n',
        (
            '        signal_source="optional_consumer",\n'
            '        optional_context=("collision_a",),\n'
        ),
    )
    (package_dir / "optional_consumer.py").write_text(
        optional_source,
        encoding="utf-8",
    )

    for indicator_id, dependency_id in (
        ("cycle_a", "cycle_b"),
        ("cycle_b", "cycle_a"),
    ):
        source = indicator_module_template(
            indicator_id,
            package_name=package_name,
        ).replace(
            f'        signal_source="{indicator_id}",\n',
            (
                f'        signal_source="{indicator_id}",\n'
                f'        depends_on=("{dependency_id}",),\n'
            ),
        )
        (package_dir / f"{indicator_id}.py").write_text(
            source,
            encoding="utf-8",
        )

    standalone_source = indicator_module_template(
        "standalone",
        package_name=package_name,
    )
    (package_dir / "standalone.py").write_text(
        standalone_source,
        encoding="utf-8",
    )

    for indicator_id in ("renderer_collision_a", "renderer_collision_b"):
        renderer_package = package_dir / indicator_id
        renderer_package.mkdir()
        source = (
            indicator_module_template(
                indicator_id,
                package_name=package_name,
            )
            .replace(
                f'        signal_source="{indicator_id}",\n',
                (
                    f'        signal_source="{indicator_id}",\n'
                    "        custom_renderer=True,\n"
                    '        renderer_kind="custom",\n'
                    '        renderer_ref="shared_custom_renderer",\n'
                    '        renderer_primitives=("custom",),\n'
                    '        custom_renderer_requirements=("price_overlay_provider",),\n'
                ),
            )
            .replace(
                f'    adapter_ref="{package_name}.{indicator_id}:build_execution_spec",\n',
                (
                    f'    adapter_ref="{package_name}.{indicator_id}:build_execution_spec",\n'
                    '    ui_js_assets=("client.js",),\n'
                ),
            )
        )
        (renderer_package / "__init__.py").write_text(
            source,
            encoding="utf-8",
        )
        (renderer_package / "client.js").write_text(
            "",
            encoding="utf-8",
        )

    missing_renderer_package = package_dir / "missing_renderer_ref"
    missing_renderer_package.mkdir()
    missing_renderer_source = (
        indicator_module_template(
            "missing_renderer_ref",
            package_name=package_name,
        )
        .replace(
            '        signal_source="missing_renderer_ref",\n',
            (
                '        signal_source="missing_renderer_ref",\n'
                "        custom_renderer=True,\n"
                '        renderer_kind="custom",\n'
                '        renderer_primitives=("custom",),\n'
                '        custom_renderer_requirements=("price_overlay_provider",),\n'
            ),
        )
        .replace(
            (f'    adapter_ref="{package_name}.missing_renderer_ref:build_execution_spec",\n'),
            (
                f'    adapter_ref="{package_name}.missing_renderer_ref:'
                'build_execution_spec",\n'
                '    ui_js_assets=("client.js",),\n'
            ),
        )
    )
    (missing_renderer_package / "__init__.py").write_text(
        missing_renderer_source,
        encoding="utf-8",
    )
    (missing_renderer_package / "client.js").write_text(
        "",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    discover_indicator_modules.cache_clear()
    try:
        modules = discover_indicator_modules(package_name)
        catalog = indicator_module_catalog(package_name)
    finally:
        discover_indicator_modules.cache_clear()

    assert {module.id for module in modules} == {
        "optional_consumer",
        "standalone",
    }
    invalid_by_id = {
        indicator_id: entry
        for entry in catalog["invalid"]
        for indicator_id in entry["indicator_ids"]
    }
    assert invalid_by_id["collision_a"]["reason"] == "duplicate_dom_id"
    assert invalid_by_id["collision_b"]["reason"] == "duplicate_dom_id"
    assert invalid_by_id["hard_consumer"]["reason"] == "unknown_dependency"
    assert invalid_by_id["cycle_a"]["reason"] == "dependency_cycle"
    assert invalid_by_id["cycle_b"]["reason"] == "dependency_cycle"
    assert invalid_by_id["renderer_collision_a"]["reason"] == "duplicate_custom_renderer_ref"
    assert invalid_by_id["renderer_collision_b"]["reason"] == "duplicate_custom_renderer_ref"
    assert invalid_by_id["missing_renderer_ref"]["reason"] == "invalid_contract"
    assert "missing_custom_overlay_renderer_ref" in invalid_by_id["missing_renderer_ref"]["detail"]
    assert {
        indicator_id for entry in catalog["installed"] for indicator_id in entry["indicator_ids"]
    } == {"optional_consumer", "standalone"}


def test_indicator_adapter_refs_are_declared_by_indicator_modules() -> None:
    for module in indicator_modules():
        if module.spec.pipeline_stage == "ui":
            if module.service_ref:
                package_module = module.spec.calculate_ref.split(":", 1)[0]
                service_module = module.service_ref.split(":", 1)[0]
                assert service_module == package_module or service_module.startswith(
                    f"{package_module}."
                )
            continue
        assert module.adapter_ref.startswith("aef_terminal.indicators.")
        assert "engine.indicator_adapters" not in module.adapter_ref
        module_name, function_name = module.adapter_ref.split(":", 1)
        adapter_module = import_module(module_name)
        assert callable(getattr(adapter_module, function_name)), module.id
        if module.spec.signal_name_resolver_ref:
            resolver_module_name, resolver_function_name = (
                module.spec.signal_name_resolver_ref.split(":", 1)
            )
            resolver_module = import_module(resolver_module_name)
            assert callable(getattr(resolver_module, resolver_function_name)), module.id


def test_indicator_package_does_not_import_terminal_or_engine_layers() -> None:
    service_adapter_modules = {
        ref.split(":", 1)[0]
        for module in indicator_modules()
        if module.spec.pipeline_stage == "ui" and module.spec.renderer_kind == "service"
        for ref in (module.service_ref, module.router_ref)
        if ref
    }
    violations: list[str] = []
    for path in Path("src/aef_terminal/indicators").rglob("*.py"):
        package_module = ".".join(path.with_suffix("").parts[1:])
        if path.name == "__init__.py":
            package_module = ".".join(path.parent.parts[1:])
        forbidden_prefixes = (
            "aef_terminal.engine",
            "aef_terminal.storage",
            *(() if package_module in service_adapter_modules else ("aef_terminal.ui",)),
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_name = str(node.module or "")
                if module_name.startswith(forbidden_prefixes):
                    violations.append(f"{path}:{node.lineno}:{module_name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{path}:{node.lineno}:{alias.name}")

    assert violations == []


def test_runtime_package_does_not_import_engine_or_ui_layers() -> None:
    forbidden_prefixes = ("aef_terminal.engine", "aef_terminal.ui")
    violations: list[str] = []
    for path in Path("src/aef_terminal/runtime").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_name = str(node.module or "")
                if module_name.startswith(forbidden_prefixes):
                    violations.append(f"{path}:{node.lineno}:{module_name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{path}:{node.lineno}:{alias.name}")

    assert violations == []


def test_runtime_params_core_is_indicator_agnostic() -> None:
    builder_source = Path("src/aef_terminal/indicators/runtime_params.py").read_text(
        encoding="utf-8"
    )
    contract_source = Path("src/aef_terminal/indicators/runtime.py").read_text(encoding="utf-8")

    assert tuple(IndicatorRuntimeParams.__dataclass_fields__) == (
        "enabled",
        "by_indicator",
        "errors",
        "strategy_mode",
    )
    assert "class IndicatorRuntimeParams" in contract_source
    assert "getattr(self.runtime_params" not in contract_source
    assert "aef_terminal.indicators.modules" not in builder_source
    assert "params_attr" not in builder_source
    assert "module.params_builder_ref" in builder_source
    assert "resolve_ref(module.params_builder_ref)" in builder_source


def test_runtime_params_consumers_do_not_capability_probe_typed_contract() -> None:
    violations: list[str] = []
    for path in Path("src/aef_terminal").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
            ):
                continue
            target = node.args[0]
            if (
                isinstance(target, ast.Name)
                and target.id == "runtime_params"
                or isinstance(target, ast.Attribute)
                and target.attr == "runtime_params"
            ):
                violations.append(f"{path}:{node.lineno}")

    assert not violations


def test_indicator_registry_reuses_the_canonical_dynamic_ref_resolver() -> None:
    source = Path("src/aef_terminal/indicators/registry.py").read_text(encoding="utf-8")

    assert "def _resolve_signal_name_ref" not in source
    assert "resolver = resolve_ref(spec.signal_name_resolver_ref)" in source


def test_module_param_builders_match_declared_control_defaults() -> None:
    for module in indicator_modules():
        if not module.params_builder_ref:
            continue
        declared_defaults = {
            control.param_key: control.default
            for control in module.spec.controls
            if control.param_key
        }
        builder = resolve_ref(module.params_builder_ref)
        assert builder({}, DEFAULT_INDICATOR_SETTINGS) == builder(
            declared_defaults,
            DEFAULT_INDICATOR_SETTINGS,
        ), module.id


def test_stable_hash_uses_object_state_not_object_identity() -> None:
    first = SimpleNamespace(mode="scalp", thresholds={"watch": 55.0})
    second = SimpleNamespace(mode="scalp", thresholds={"watch": 55.0})
    cyclic: list[object] = []
    cyclic.append(cyclic)

    assert stable_hash(first) == stable_hash(second)
    assert stable_hash(None) != stable_hash({})
    with pytest.raises(TypeError, match="is not a stable-hash value"):
        stable_hash(object())
    with pytest.raises(TypeError, match="cyclic stable-hash value"):
        stable_hash(cyclic)


def test_invalid_module_runtime_params_are_isolated_to_indicator_status() -> None:
    bars = _bars(40)
    raw = {
        "obvious_failure": {"enabled": True, "lookback": "invalid"},
        "trade_setup_engine": {"enabled": False},
    }
    runtime_params = build_indicator_runtime_params(
        raw,
        DEFAULT_INDICATOR_SETTINGS,
    )
    context = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=raw,
        features={},
    )

    specs = indicator_execution_specs(context, stage="setup")
    error_spec = next(spec for spec in specs if spec.id == "obvious_failure")
    result = run_pipeline_indicator(
        error_spec.id,
        input_bars=error_spec.input_bars,
        analysis_bar=error_spec.analysis_bar,
        mode=error_spec.mode,
        calculate=error_spec.calculate,
        runtime_params=error_spec.runtime_params,
        params=error_spec.params,
    )

    assert "obvious_failure" in runtime_params.errors
    assert runtime_params.for_indicator("obvious_failure").lookback == 24
    assert result["status"]["state_code"] == "error"
    assert "runtime params are invalid" in result["status"]["last_error"]


def test_invalid_adapter_result_is_isolated_to_indicator_status(
    monkeypatch,
) -> None:
    bars = _bars(40)
    raw = {
        "obvious_failure": {"enabled": True},
        "trade_setup_engine": {"enabled": False},
    }
    runtime_params = build_indicator_runtime_params(
        raw,
        DEFAULT_INDICATOR_SETTINGS,
    )
    context = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=raw,
        features={},
    )
    monkeypatch.setattr(
        "aef_terminal.engine.indicator_adapters.indicator_adapters",
        lambda _stage: {
            "obvious_failure": IndicatorModuleAdapter(
                id="obvious_failure",
                stage="setup",
                build_spec=lambda _ctx: object(),
            )
        },
    )

    specs = indicator_execution_specs(context, stage="setup")
    error_spec = next(spec for spec in specs if spec.id == "obvious_failure")
    result = run_pipeline_indicator(
        error_spec.id,
        input_bars=error_spec.input_bars,
        analysis_bar=error_spec.analysis_bar,
        mode=error_spec.mode,
        calculate=error_spec.calculate,
        runtime_params=error_spec.runtime_params,
        params=error_spec.params,
    )

    assert result["status"]["state_code"] == "error"
    assert "expected IndicatorExecutionSpec" in result["status"]["last_error"]


def test_invalid_params_hash_stays_inside_indicator_error_boundary() -> None:
    calculated = False

    def calculate() -> dict[str, object]:
        nonlocal calculated
        calculated = True
        return {}

    result = run_indicator(
        "obvious_failure",
        input_bars=_bars(1),
        analysis_bar=_bars(1)[-1],
        mode="confirmed",
        calculate=calculate,
        params=object(),
    )

    assert calculated is False
    assert result["status"]["state_code"] == "error"
    assert result["status"]["params_hash"] is None
    assert "stable-hash value" in result["status"]["last_error"]


def test_indicator_registry_drives_empty_results_and_runtime_process_map() -> None:
    defaults = indicator_defaults_from_params({"ema": {"fast": 13, "slow": 34}})
    runtime_params = build_indicator_runtime_params(
        {"vsa_volume": {"enabled": False}},
        defaults,
    )

    assert set(runtime_params.enabled) == set(INDICATOR_REGISTRY)
    assert set(runtime_params.by_indicator) == set(INDICATOR_REGISTRY)
    assert "vsa_volume" not in runtime_params.enabled
    assert "vsa_volume" not in runtime_params.by_indicator
    with pytest.raises(KeyError, match="vsa_volume"):
        runtime_params.for_indicator("vsa_volume")
    assert not hasattr(runtime_params, "vsa")
    assert runtime_params.enabled["market_spotlight"] is False
    assert runtime_params.enabled["linda_volume"] is False
    assert runtime_params.enabled["trade_setup_engine"] is False
    setup_params = runtime_params.for_indicator("trade_setup_engine")
    assert setup_params.ema_fast_len == 13
    assert setup_params.ema_slow_len == 34

    first = indicator_empty_result("wolfe_structure")
    second = indicator_empty_result("wolfe_structure")
    first["settings"]["pivot_len"] = 99
    assert second["settings"]["pivot_len"] == 3


def test_w5_runtime_params_hash_includes_consumed_global_defaults() -> None:
    bars = _bars(1)
    first_params = build_indicator_runtime_params(
        {},
        indicator_defaults_from_params({"atr_len": 10}),
    ).for_indicator("w5_structure")
    second_params = build_indicator_runtime_params(
        {},
        indicator_defaults_from_params({"atr_len": 24}),
    ).for_indicator("w5_structure")

    first = run_pipeline_indicator(
        "w5_structure",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        calculate=lambda: {},
        runtime_params=_runtime_params(enabled={"w5_structure": True}),
        params=first_params,
    )
    second = run_pipeline_indicator(
        "w5_structure",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        calculate=lambda: {},
        runtime_params=_runtime_params(enabled={"w5_structure": True}),
        params=second_params,
    )

    assert first_params.defaults.atr_len == 10
    assert second_params.defaults.atr_len == 24
    assert first["status"]["params_hash"] != second["status"]["params_hash"]


def test_w5_runtime_min_score_drives_generic_candidate_floor() -> None:
    defaults = indicator_defaults_from_params({})
    runtime_params = build_indicator_runtime_params(
        {"w5_structure": {"min_score": 64}},
        defaults,
    )

    assert (
        indicator_candidate_score_floor(
            "w5_structure",
            runtime_params,
            strong_watch_floor=60.0,
            structure_floor=62.0,
            default_floor=62.0,
        )
        == 64.0
    )


def test_indicator_registry_calculate_refs_are_importable() -> None:
    manifest = indicator_manifest()
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        module_name, function_name = spec.calculate_ref.split(":", 1)
        module = import_module(module_name)
        calculate = getattr(module, function_name)
        assert callable(calculate), indicator_id
        ref_key = "service_ref" if spec.pipeline_stage == "ui" else "adapter_ref"
        ref = manifest[indicator_id][ref_key]
        if spec.pipeline_stage == "ui" and not ref:
            continue
        ref_module_name, ref_function_name = ref.split(":", 1)
        ref_module = import_module(ref_module_name)
        assert callable(getattr(ref_module, ref_function_name)), indicator_id
        module_contract = next(
            module for module in indicator_modules() if module.id == indicator_id
        )
        for extension_ref in (
            module_contract.params_builder_ref,
            module_contract.candidate_promoter_ref,
        ):
            if not extension_ref:
                continue
            extension_module_name, extension_function_name = extension_ref.split(":", 1)
            extension_module = import_module(extension_module_name)
            assert callable(getattr(extension_module, extension_function_name)), indicator_id


def test_pipeline_indicator_runs_when_selected_by_specs() -> None:
    bars = _bars(4)
    called = False

    def calculate() -> dict[str, object]:
        nonlocal called
        called = True
        return {"latest": {"state": "READY"}}

    result = run_pipeline_indicator(
        "option_reversal",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        calculate=calculate,
        runtime_params=_runtime_params(enabled={"option_reversal": True}),
    )

    assert called is True
    assert result["status"]["mode"] == "confirmed"
    assert result["latest"]["state"] == "READY"


def test_indicator_status_exposes_structured_live_preview_flag() -> None:
    bars = _bars(4)
    result = attach_indicator_status(
        "breakout_accumulation",
        {"preview_events": [{"ts": bars[-1].ts.isoformat(), "action_code": "BO"}]},
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
    )

    assert result["status"]["preview_active"] is True
    assert result["status"]["state_code"] == "live_preview_signal"
    assert result["status"]["health"] == "signal"
    assert result["status"]["has_signal"] is True
    assert result["status"]["blocked_signal"] is False
    assert result["status"]["preview_event_count"] == 1
    assert result["status"]["trigger_event"] == {"code": "indicator_live_preview", "count": 1}

    context_preview = attach_indicator_status(
        "market_spotlight",
        {
            "preview": {
                "ts": bars[-1].ts.isoformat(),
                "state": "directional_flow",
            }
        },
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
    )

    assert context_preview["status"]["preview_active"] is True
    assert context_preview["status"]["state_code"] == "no_signal"
    assert context_preview["status"]["has_signal"] is False


def test_context_and_setup_runtime_schemas_declare_latest_domain_facts() -> None:
    manifest = indicator_manifest()
    spotlight_latest = set(manifest["market_spotlight"]["runtime_payload_contract"]["latest"])
    setup_latest = set(manifest["trade_setup_engine"]["runtime_payload_contract"]["latest"])

    assert {"flow_state", "side_lock", "state_direction", "strategy_phase"} <= spotlight_latest
    assert {"reason_code", "role"} <= setup_latest


def test_option_reversal_declares_monte_carlo_bridge_runtime_payload() -> None:
    contract = indicator_manifest()["option_reversal"]["runtime_payload_contract"]

    assert "monte_carlo_bridge" in contract["latest"]
    assert "option_side" in contract["latest"]
    assert "signal" in contract["latest"]
    assert contract["series"] == []
    assert contract["signals"] == []
    assert "option_side" in contract["events"]
    assert "option_side" in contract["overlays"]
    assert "details" in contract["events"]


def test_primary_pipeline_vsa_cooldown_uses_confirmed_bars_for_execution() -> None:
    source = Path("src/aef_terminal/engine/analyze/pipeline.py").read_text(encoding="utf-8")

    assert "apply_vsa_fuel_sfp_cooldown(\n        candidates,\n        confirmed_bars," in source
    assert (
        "apply_vsa_fuel_sfp_cooldown(\n        candidates,\n        live_signal_bars," not in source
    )


def test_primary_pipeline_uses_typed_defaults_and_profile_for_vsa_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars(40)
    profile = PROFILES["SPY"]
    observed: dict[str, VsaVolumeParams] = {}
    real_vsa_facts = analyze_pipeline.vsa_facts
    real_build_context = analyze_pipeline.build_vsa_volume_context

    def capture_facts(
        input_bars,
        params,
        *,
        vwap_session=None,
        confirmed_slots=None,
    ):
        observed["facts"] = params
        observed["confirmed_slots"] = confirmed_slots
        return real_vsa_facts(
            input_bars,
            params,
            vwap_session=vwap_session,
            confirmed_slots=confirmed_slots,
        )

    def capture_context(
        input_bars,
        *,
        profile,
        params=None,
        facts=None,
        breakout_facts=None,
        confirmed_slots=None,
        vwap_session=None,
    ):
        observed["context"] = params
        observed["breakout_facts"] = breakout_facts
        return real_build_context(
            input_bars,
            profile=profile,
            params=params,
            facts=facts,
            breakout_facts=breakout_facts,
            confirmed_slots=confirmed_slots,
            vwap_session=vwap_session,
        )

    monkeypatch.setattr(analyze_pipeline, "vsa_facts", capture_facts)
    monkeypatch.setattr(
        analyze_pipeline,
        "build_vsa_volume_context",
        capture_context,
    )
    monkeypatch.setattr(
        analyze_pipeline,
        "indicator_ids_for_shared_context",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        analyze_pipeline,
        "indicator_execution_specs",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        analyze_pipeline,
        "execute_indicator_pipeline",
        lambda *_args, **_kwargs: SimpleNamespace(
            indicators={},
            candidates=[],
            timings_ms={},
        ),
    )
    monkeypatch.setattr(
        analyze_pipeline,
        "apply_vsa_fuel_sfp_cooldown",
        lambda candidates, *_args, **_kwargs: (candidates, None),
    )

    _, _, context, _, shared = analyze_pipeline._calculate_primary_signal_indicators(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=_runtime_params(),
        global_defaults=DEFAULT_INDICATOR_SETTINGS,
        indicator_params={
            "vsa_volume": {
                "atr_len": 2,
                "vol_len": 3,
                "importance_floor": 1.0,
                "use_profile_thresholds": False,
                "impulse_rvol": 9.0,
                "fuel_rvol": 9.5,
                "fuel_range_atr": 4.0,
            }
        },
        instrument_profile=profile,
        instrument_id="test:instrument",
        route_fingerprint="test-route-fingerprint",
        provider="ibkr",
        analysis_as_of_utc=bars[-1].ts + timedelta(minutes=1),
        quality={"signals_ok": True},
        vwap_session=explicit_vwap_session_for_bars(bars),
        features={},
        feature_context=SimpleNamespace(defaults=DEFAULT_INDICATOR_SETTINGS),
        option_flow=None,
        tick_flow=None,
        manual_channel_drawings=(),
        confirmed_slots=None,
        atr_value=1.0,
        strong_watch_floor=70.0,
        structure_floor=65.0,
        score_overrides={},
    )

    assert observed["facts"] is observed["context"]
    assert observed["confirmed_slots"] is None
    assert observed["facts"].atr_len == DEFAULT_INDICATOR_SETTINGS.atr_len
    assert observed["facts"].vol_len == DEFAULT_INDICATOR_SETTINGS.rvol_len
    assert observed["facts"].importance_floor == label_importance_floor(
        DEFAULT_INDICATOR_SETTINGS.score
    )
    assert observed["facts"].impulse_rvol == profile.vsa_impulse_rvol
    assert observed["facts"].fuel_rvol == profile.vsa_fuel_rvol
    assert observed["facts"].fuel_range_atr == profile.fuel_range_atr
    assert context["params"]["impulse_rvol"] == observed["facts"].impulse_rvol
    assert context["params"]["fuel_rvol"] == observed["facts"].fuel_rvol
    assert context["params"]["fuel_range_atr"] == observed["facts"].fuel_range_atr
    assert len(shared["vsa_facts"]) == len(bars)
    assert len(shared["vsa_breakout_facts"]) == len(bars)
    assert observed["breakout_facts"] is shared["vsa_breakout_facts"]


def test_vsa_core_is_always_on_and_rejects_unconfirmed_input() -> None:
    bars = _bars(40)
    snapshot = analyze_bars(
        bars,
        indicator_params={"vsa_volume": {"enabled": False}},
    )

    assert "vsa_volume" not in snapshot["indicators"]
    assert snapshot["vsa_volume"]["status"]["mode"] == "confirmed"
    assert snapshot["vsa_volume"]["status"]["bar_count"] == len(bars)
    assert snapshot["vsa_volume"]["status"]["analysis_ts"] == bars[-1].ts.isoformat()
    assert len(snapshot["vsa_volume"]["series"]) == len(bars)

    forming_bar = Bar(
        bars[-1].symbol,
        bars[-1].ts + timedelta(minutes=1),
        bars[-1].close,
        bars[-1].close + 1.0,
        bars[-1].close - 1.0,
        bars[-1].close + 0.25,
        bars[-1].volume,
        bars[-1].timeframe,
        bars[-1].source,
        closed=False,
    )
    rejected = build_vsa_volume_context(
        [*bars, forming_bar],
        profile=ES_PROFILE,
    )

    assert rejected["availability"]["reason_code"] == "unconfirmed_bar_input"
    assert rejected["status"]["state"] == "error"
    assert rejected["status"]["last_error"] == ("VSA core accepts exchange-confirmed bars only")


def test_indicator_adapters_import_without_analyze_package_ordering() -> None:
    env = {**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", "import aef_terminal.engine.indicator_adapters"],
        cwd=".",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_app_first_import_keeps_service_indicators_registered() -> None:
    env = {**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import aef_terminal.ui.app; "
                "from aef_terminal.indicators.module_discovery import "
                "indicator_module_catalog; "
                "from aef_terminal.indicators.registry import "
                "INDICATOR_REGISTRY; "
                "assert 'tick_flow' in INDICATOR_REGISTRY, "
                "indicator_module_catalog()['invalid']"
            ),
        ],
        cwd=".",
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_disabled_indicators_are_excluded_from_execution_specs() -> None:
    from aef_terminal.engine.indicator_adapters import (
        IndicatorRunContext,
        indicator_execution_specs,
    )

    params_by_indicator = {"breakout_accumulation": SimpleNamespace()}
    ctx = IndicatorRunContext(
        confirmed_bars=[],
        live_signal_bars=[],
        latest=None,
        analysis_latest=None,
        runtime_params=_runtime_params(
            enabled={"breakout_accumulation": True},
            by_indicator=params_by_indicator,
        ),
        indicator_params={},
        instrument_profile=ES_PROFILE,
        features={},
    )
    specs = indicator_execution_specs(ctx, stage="primary")
    assert "vsa_volume" not in {spec.id for spec in specs}
    assert "breakout_accumulation" in {spec.id for spec in specs}


def test_every_backend_indicator_connects_to_execution_specs_when_calc_is_enabled() -> None:
    bars = _bars(120)
    defaults = indicator_defaults_from_params({})
    feature_context = build_feature_context(bars, defaults=defaults)
    backend_ids = [
        indicator_id
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.pipeline_stage != "ui"
    ]

    for indicator_id in backend_ids:
        enabled_params = {item_id: {"enabled": item_id == indicator_id} for item_id in backend_ids}
        runtime_params = build_indicator_runtime_params(enabled_params, defaults)
        ctx = IndicatorRunContext(
            confirmed_bars=bars,
            live_signal_bars=bars,
            latest=bars[-1],
            analysis_latest=bars[-1],
            runtime_params=runtime_params,
            indicator_params=enabled_params,
            features=feature_context.values,
            option_flow={},
            tick_flow={},
            structure_bars=bars,
            security_context={},
            security_context_quality={},
            line_width=1.0,
            show_geometry=True,
            feature_context=feature_context,
            pivot_context=feature_context.pivot_context,
            atr_value=feature_context.latest_atr,
            indicator_bundle={},
            display={"zones": True, "labels": True, "plan": True},
            decision=None,
            decision_candidates=[],
            quality={},
            option_targets=[],
            shared={"vsa_facts": [], "market_series": None, "vix_context": None},
        )

        specs = indicator_execution_specs(
            ctx, stage=INDICATOR_REGISTRY[indicator_id].pipeline_stage
        )
        matched = [spec for spec in specs if spec.id == indicator_id]

        assert matched, indicator_id
        assert matched[0].input_bars
        assert matched[0].analysis_bar == bars[-1]


def test_every_backend_indicator_is_excluded_from_execution_specs_when_calc_is_disabled() -> None:
    bars = _bars(120)
    defaults = indicator_defaults_from_params({})
    feature_context = build_feature_context(bars, defaults=defaults)
    runtime_params = build_indicator_runtime_params(
        {
            indicator_id: {"enabled": False}
            for indicator_id, spec in INDICATOR_REGISTRY.items()
            if spec.pipeline_stage != "ui"
        },
        defaults,
    )
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params={},
        features=feature_context.values,
        option_flow={},
        tick_flow={},
        structure_bars=bars,
        security_context={},
        security_context_quality={},
        feature_context=feature_context,
        pivot_context=feature_context.pivot_context,
        atr_value=feature_context.latest_atr,
        indicator_bundle={},
        display={"zones": True, "labels": True, "plan": True},
        quality={},
        option_targets=[],
        shared={"vsa_facts": [], "market_series": None, "vix_context": None},
    )

    for stage in ("primary", "structure", "setup", "post_decision"):
        assert indicator_execution_specs(ctx, stage=stage) == []


def test_indicator_connection_manifest_resolves_enabled_set_by_group() -> None:
    connections = indicator_connection_manifest(
        {
            "breakout_accumulation": {"enabled": True},
            "linda_volume": {"enabled": "false"},
            "smc_channels": {"enabled": True},
            "indicator_scores": {
                "breakout_accumulation": {
                    "empirical_power": 1.4,
                    "usefulness": 1.2,
                    "sample_size": 42,
                    "source": "two_month_sample",
                }
            },
        },
        group="primary",
    )

    assert set(connections) == set(indicator_ids("primary"))
    assert connections["linda_volume"]["connected"] is False
    assert connections["breakout_accumulation"]["connected"] is True
    assert connections["breakout_accumulation"]["score_weight"] == 1.68
    assert connections["breakout_accumulation"]["source"] == "two_month_sample"
    assert connections["breakout_accumulation"]["sample_size"] == 42
    assert connections["breakout_accumulation"]["connection"] == {
        "registry_id": "breakout_accumulation",
        "pipeline_stage": "primary",
        "input_contract": "bars+features",
        "output_contract": "indicator-result-v1",
        "depends_on": [],
        "required_dependencies": [],
        "optional_context": [],
        "dependencies": [],
        "dependencies_ready": True,
        "score_weight": 1.68,
        "score_source": "two_month_sample",
        "score_sample_size": 42,
        "enabled": True,
    }
    assert "smc_channels" not in connections

    setup_connections = indicator_connection_manifest(stage="setup")
    assert set(setup_connections) == {"obvious_failure", "trade_setup_engine"}
    assert setup_connections["obvious_failure"]["enabled"] is False
    assert setup_connections["obvious_failure"]["connection"]["optional_context"] == [
        "market_spotlight",
        "smc_channels",
    ]
    assert setup_connections["trade_setup_engine"]["connection"]["pipeline_stage"] == "setup"
    assert setup_connections["trade_setup_engine"]["connection"]["optional_context"] == [
        "market_spotlight"
    ]
    assert setup_connections["trade_setup_engine"]["enabled"] is False
    assert setup_connections["trade_setup_engine"]["connected"] is False

    post_decision_connections = indicator_connection_manifest(stage="post_decision")
    assert post_decision_connections["martin_carlo"]["connection"]["depends_on"] == []
    assert post_decision_connections["martin_carlo"]["connection"]["required_dependencies"] == []
    martin_optional_context = [
        "impulse_fib",
        "absorption_trap",
        "w5_structure",
        "wolfe_structure",
        "option_reversal",
    ]
    assert (
        post_decision_connections["martin_carlo"]["connection"]["optional_context"]
        == martin_optional_context
    )
    dependencies = post_decision_connections["martin_carlo"]["connection"]["dependencies"]
    assert [item["id"] for item in dependencies] == martin_optional_context
    assert all(item["required"] is False for item in dependencies)
    assert all(item["optional_context"] is True for item in dependencies)
    assert all(item["enabled"] is False for item in dependencies)
    assert all(item["linked"] is False for item in dependencies)
    assert {item["id"]: item["registered"] for item in dependencies} == {
        "impulse_fib": True,
        "absorption_trap": True,
        "w5_structure": True,
        "wolfe_structure": True,
        "option_reversal": True,
    }
    assert post_decision_connections["martin_carlo"]["connected"] is False
    assert post_decision_connections["ai_third_opinion"]["connection"]["optional_context"] == [
        "market_spotlight",
        "trade_setup_engine",
        "option_reversal",
        "martin_carlo",
        "channel_master",
    ]
    assert post_decision_connections["ai_third_opinion"]["connected"] is False

    optional_context_missing = indicator_connection_manifest(
        {
            "option_reversal": {"enabled": False},
            "martin_carlo": {"enabled": True},
            "market_spotlight": {"enabled": False},
            "linda_volume": {"enabled": True},
        },
        stage="post_decision",
    )
    assert optional_context_missing["option_reversal"]["connected"] is False
    assert optional_context_missing["martin_carlo"]["enabled"] is True
    assert optional_context_missing["martin_carlo"]["dependencies_ready"] is True
    assert optional_context_missing["martin_carlo"]["connected"] is True
    optional_dependencies = optional_context_missing["martin_carlo"]["connection"]["dependencies"]
    assert [item["id"] for item in optional_dependencies] == martin_optional_context
    assert {item["id"]: item["registered"] for item in optional_dependencies} == {
        "impulse_fib": True,
        "absorption_trap": True,
        "w5_structure": True,
        "wolfe_structure": True,
        "option_reversal": True,
    }
    assert all(item["enabled"] is False for item in optional_dependencies)
    assert all(item["required"] is False for item in optional_dependencies)
    assert all(item["optional_context"] is True for item in optional_dependencies)
    assert all(item["linked"] is False for item in optional_dependencies)

    linda_links = indicator_connection_manifest(
        {"market_spotlight": {"enabled": True}, "linda_volume": {"enabled": True}}, group="primary"
    )
    assert linda_links["linda_volume"]["connection"]["dependencies"] == [
        {
            "id": "market_spotlight",
            "enabled": True,
            "registered": True,
            "required": False,
            "optional_context": True,
            "linked": True,
        }
    ]


def test_channel_master_optional_consumers_run_solo_and_link_when_enabled() -> None:
    disabled_params = {
        "channel_master": {"enabled": False},
        "smc_channels": {"enabled": True},
        "ai_third_opinion": {"enabled": True},
    }
    enabled_params = {
        **disabled_params,
        "channel_master": {"enabled": True},
    }

    for stage, consumer_id in (
        ("structure", "smc_channels"),
        ("post_decision", "ai_third_opinion"),
    ):
        solo = indicator_connection_manifest(
            disabled_params,
            stage=stage,
        )[consumer_id]
        linked = indicator_connection_manifest(
            enabled_params,
            stage=stage,
        )[consumer_id]
        solo_dependency = next(
            item for item in solo["connection"]["dependencies"] if item["id"] == "channel_master"
        )
        linked_dependency = next(
            item for item in linked["connection"]["dependencies"] if item["id"] == "channel_master"
        )

        assert solo["enabled"] is True
        assert solo["dependencies_ready"] is True
        assert solo["connected"] is True
        assert solo_dependency == {
            "id": "channel_master",
            "enabled": False,
            "registered": True,
            "required": False,
            "optional_context": True,
            "linked": False,
        }
        assert linked["connected"] is True
        assert linked_dependency == {
            **solo_dependency,
            "enabled": True,
            "linked": True,
        }


def test_indicator_scoring_contract_normalizes_empirical_overrides() -> None:
    meta = normalize_indicator_score_meta(
        "demo",
        seed_family="trend",
        seed_power=1.0,
        seed_usefulness=1.0,
        overrides={
            "demo": {
                "score_family": "unknown",
                "empirical_power": 4.0,
                "usefulness": 1.25,
                "sample_size": 137,
                "source": "two_month_sample",
            }
        },
    )

    assert meta == {
        "score_family": "trend",
        "empirical_power": 1.0,
        "usefulness": 1.25,
        "score_weight": 1.25,
        "source": "two_month_sample",
        "sample_size": 137,
    }


def test_primary_indicator_adapters_cover_registry_and_preserve_order() -> None:
    bars = _bars(80)
    defaults = indicator_defaults_from_params({})
    indicator_settings = {
        "breakout_accumulation": {"enabled": True},
        "impulse_fib": {"enabled": True},
        "market_spotlight": {"enabled": True},
        "linda_volume": {"enabled": True},
    }
    runtime_params = build_indicator_runtime_params(indicator_settings, defaults)
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=indicator_settings,
        features=build_feature_context(bars, defaults=defaults).values,
        shared={"vsa_facts": []},
    )

    specs = indicator_execution_specs(ctx, stage="primary")
    ids = [spec.id for spec in specs]
    expected_ids = [
        indicator_id
        for indicator_id in indicator_stage_ids("primary")
        if runtime_params.enabled.get(indicator_id, False)
    ]
    assert set(ids) == set(expected_ids)
    assert ids.index("market_spotlight") < ids.index("linda_volume")
    assert specs[ids.index("linda_volume")].depends_on == ("market_spotlight",)
    assert "trade_setup_engine" not in indicator_stage_ids("primary")
    assert set(indicator_adapters("primary")) == set(indicator_stage_ids("primary"))


def test_linda_volume_executes_solo_when_market_spotlight_is_disabled() -> None:
    bars = _bars(120)
    defaults = indicator_defaults_from_params({})
    indicator_settings = {
        indicator_id: {"enabled": indicator_id == "linda_volume"}
        for indicator_id in indicator_stage_ids("primary")
    }
    runtime_params = build_indicator_runtime_params(indicator_settings, defaults)
    feature_context = build_feature_context(bars, defaults=defaults)
    vwap_session = explicit_vwap_session_for_bars(bars)
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=indicator_settings,
        features=feature_context.values,
        vwap_session=vwap_session,
        shared={
            "vsa_facts": vsa_facts(
                bars,
                VsaVolumeParams(),
                vwap_session=vwap_session,
            ),
            "vix_context": None,
            "market_series": None,
        },
    )

    specs = indicator_execution_specs(ctx, stage="primary")
    assert [spec.id for spec in specs] == ["linda_volume"]
    assert specs[0].depends_on == ()

    result = execute_indicator_pipeline(
        specs,
        runner=run_pipeline_indicator,
        promoter=lambda *_args: [],
    )

    linda = result.indicators["linda_volume"]
    assert linda["status"]["state_code"] != "error"
    assert linda["status"]["health"] != "error"
    assert linda["series"]
    assert isinstance(linda["latest"], dict)


def test_legacy_gex_indicator_params_cannot_activate_engine_owned_context() -> None:
    bars = _bars(80)
    defaults = indicator_defaults_from_params({})
    legacy_params = {
        "global_defaults": {},
        "gex_dynamics": {"enabled": True, "capture_mode": "live"},
    }
    runtime_params = build_indicator_runtime_params(legacy_params, defaults)
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=legacy_params,
        features=build_feature_context(bars, defaults=defaults).values,
        shared={"vsa_facts": []},
    )

    assert "gex_dynamics" not in runtime_params.enabled
    assert "gex_dynamics" not in runtime_params.by_indicator
    assert "gex_dynamics" not in [
        spec.id for spec in indicator_execution_specs(ctx, stage="primary")
    ]

    inactive_snapshot = analyze_bars(
        bars,
        indicator_params=legacy_params,
        gex_history=[],
    )
    assert "gex_dynamics" not in inactive_snapshot

    active_snapshot = analyze_bars(
        bars,
        indicator_params=legacy_params,
        gex_history=[],
        gex_context_active=True,
        gex_capture_mode="live",
    )
    dynamics = active_snapshot["gex_dynamics"]

    assert set(dynamics) == {"contract", "version", "latest", "status"}
    assert dynamics["contract"] == "gex-dynamics-v1"
    assert dynamics["version"] == "3.0-engine"
    assert dynamics["status"]["state"] == "ready"
    assert dynamics["status"]["capture_mode"] == "live"
    assert {
        "series",
        "events",
        "candidates",
        "overlays",
    }.isdisjoint(dynamics)
    assert "gex_dynamics" not in active_snapshot["indicators"]
    assert all(
        candidate.get("name") != "gex_dynamics" for candidate in active_snapshot["candidates"]
    )


def test_structure_and_setup_indicator_adapters_cover_registry_stages() -> None:
    bars = _bars(80)
    defaults = indicator_defaults_from_params({})
    structure_settings = {
        "smc_channels": {"enabled": True},
        "rounded_reversal": {"enabled": True},
        "w5_structure": {"enabled": True},
        "wolfe_structure": {"enabled": True},
        "trade_setup_engine": {"enabled": True},
    }
    runtime_params = build_indicator_runtime_params(
        structure_settings,
        defaults,
    )
    feature_context = build_feature_context(bars, defaults=defaults)
    structure_ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=structure_settings,
        features=feature_context.values,
        structure_bars=bars,
        feature_context=feature_context,
        pivot_context=feature_context.pivot_context,
        atr_value=feature_context.latest_atr,
    )
    setup_ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params=structure_settings,
        features=feature_context.values,
        indicator_bundle={},
    )

    assert [
        spec.id for spec in indicator_execution_specs(structure_ctx, stage="structure")
    ] == list(indicator_stage_ids("structure"))
    assert [spec.id for spec in indicator_execution_specs(setup_ctx, stage="setup")] == [
        "trade_setup_engine"
    ]
    obvious_runtime_params = build_indicator_runtime_params(
        {"obvious_failure": {"enabled": True}},
        defaults,
    )
    obvious_setup_ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=obvious_runtime_params,
        indicator_params={"obvious_failure": {"enabled": True}},
        features=feature_context.values,
        indicator_bundle={},
    )
    assert [spec.id for spec in indicator_execution_specs(obvious_setup_ctx, stage="setup")] == [
        "obvious_failure"
    ]
    both_settings = {
        "obvious_failure": {"enabled": True},
        "trade_setup_engine": {"enabled": True},
    }
    both_runtime_params = build_indicator_runtime_params(both_settings, defaults)
    both_setup_ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=both_runtime_params,
        indicator_params=both_settings,
        features=feature_context.values,
        indicator_bundle={},
    )
    assert [spec.id for spec in indicator_execution_specs(both_setup_ctx, stage="setup")] == [
        "obvious_failure",
        "trade_setup_engine",
    ]
    assert set(indicator_adapters("structure")) == set(indicator_stage_ids("structure"))
    assert set(indicator_adapters("setup")) == set(indicator_stage_ids("setup"))


def test_post_decision_indicator_adapters_resolve_dependencies() -> None:
    bars = _bars(80)
    defaults = indicator_defaults_from_params({})
    runtime_params = build_indicator_runtime_params(
        {
            "martin_carlo": {"enabled": True},
            "option_reversal": {"enabled": True},
            "ai_third_opinion": {"enabled": True},
        },
        defaults,
    )
    feature_context = build_feature_context(bars, defaults=defaults)
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params={},
        features=feature_context.values,
        indicator_bundle={},
        decision=None,
        decision_candidates=[],
        quality={},
        option_targets=[],
    )

    specs = indicator_execution_specs(ctx, stage="post_decision")
    assert [spec.id for spec in specs] == ["option_reversal", "martin_carlo", "ai_third_opinion"]
    assert specs[-2].depends_on == ("option_reversal",)
    assert specs[-1].depends_on == ("option_reversal", "martin_carlo")
    assert set(indicator_adapters("post_decision")) == set(indicator_stage_ids("post_decision"))


def test_post_decision_advisory_indicators_run_solo_when_optional_context_disabled() -> None:
    bars = _bars(80)
    defaults = indicator_defaults_from_params({})
    runtime_params = build_indicator_runtime_params(
        {
            "martin_carlo": {"enabled": True},
            "option_reversal": {"enabled": False},
            "ai_third_opinion": {"enabled": True},
        },
        defaults,
    )
    feature_context = build_feature_context(bars, defaults=defaults)
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params={},
        features=feature_context.values,
        indicator_bundle={},
        decision=None,
        decision_candidates=[],
        quality={},
        option_targets=[],
    )

    specs = indicator_execution_specs(ctx, stage="post_decision")
    assert [spec.id for spec in specs] == ["martin_carlo", "ai_third_opinion"]
    assert specs[0].depends_on == ()
    assert specs[1].depends_on == ("martin_carlo",)

    result = execute_indicator_pipeline(
        specs,
        runner=lambda indicator_id, **kwargs: {"id": indicator_id, "latest": {"state": "OK"}},
        promoter=lambda *_args: [],
    )
    assert set(result.indicators) == {"martin_carlo", "ai_third_opinion"}


def test_paper_tradable_indicator_ids_come_from_registry_metadata() -> None:
    tradable = paper_tradable_indicator_ids()

    assert "linda_volume" in tradable
    assert "trade_setup_engine" in tradable
    assert "vsa_volume" not in tradable
    assert "martin_carlo" not in tradable


def test_promoted_candidates_carry_indicator_score_metadata_without_rescoring() -> None:
    indicator = {
        "latest": {
            "signal": {
                "direction": "long",
                "action": "GO",
                "score": 82,
                "trigger": 100,
                "stop": 98,
                "target": 104,
                "code": "SETUP_BO",
                "kind": "transit",
                "trigger_event": {"code": "setup_momentum_breakout"},
            }
        }
    }

    candidates = promote_pipeline_candidates(
        "trade_setup_engine",
        indicator,
        score_floor=50,
        atr_value=1.0,
        features={},
        score_overrides={
            "trade_setup_engine": {
                "empirical_power": 1.5,
                "usefulness": 1.2,
                "source": "two_month_sample",
            }
        },
    )

    assert len(candidates) == 1
    assert candidates[0].score == 82
    assert candidates[0].details["indicator_score_meta"]["source"] == "two_month_sample"
    assert candidates[0].details["indicator_score_weight"] == 1.8


def test_linda_candidate_promotion_dispatches_through_package_ref() -> None:
    indicator = {
        "latest": {
            "code": "VW_RECLAIM",
            "state": "VW_RECLAIM",
            "price": 100.0,
            "signal": {
                "code": "VW_RECLAIM",
                "direction": "long",
                "action": "ARM",
                "score": 82.0,
                "trigger": 100.0,
                "stop": 99.0,
                "target": 102.0,
                "kind": "fade",
                "trigger_event": {"code": "vwap_reclaim"},
            },
        },
        "playbook_setups": {"active": False},
    }

    candidates = promote_pipeline_candidates(
        "linda_volume",
        indicator,
        score_floor=56.0,
        atr_value=1.0,
        features={},
    )

    assert len(candidates) == 1
    assert candidates[0].name == "linda_fade"
    assert candidates[0].source == "linda_volume"
    assert indicator["playbook_candidate_promotion"]["reject_reason"] == "inactive"


def test_indicator_registry_resolves_signal_names() -> None:
    canonical_ref = "aef_terminal.indicators.domain_facts:resolve_signal_name"
    assert INDICATOR_REGISTRY["impulse_fib"].signal_name_resolver_ref == canonical_ref
    assert INDICATOR_REGISTRY["trade_setup_engine"].signal_name_resolver_ref == (
        "aef_terminal.indicators.modules.trade_setup_engine:resolve_signal_name"
    )
    assert INDICATOR_REGISTRY["linda_volume"].signal_name_resolver_ref == (
        "aef_terminal.indicators.modules.linda_volume.playbook_contract:resolve_signal_name"
    )
    assert resolve_indicator_signal_name("linda_volume", "VW_RECLAIM", "ARM") == "linda_fade"
    assert resolve_indicator_signal_name("linda_volume", "PB_UP", "ARM") == "linda_continuation"
    assert resolve_indicator_signal_name("linda_volume", "TS_UP", "ARM") == "linda_fade"
    assert resolve_indicator_signal_name("linda_volume", "PIN_UP", "GO") == "linda_continuation"
    assert resolve_indicator_signal_name("impulse_fib", "FIB-L", "WATCH") == "impulse_pullback"
    assert resolve_indicator_signal_name("impulse_fib", "FIB-L", "GO") == "impulse"
    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_BO", "GO")
        == "setup_momentum_breakout"
    )
    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_BD", "GO")
        == "setup_momentum_breakdown"
    )
    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_BD_WATCH", "WATCH")
        == "setup_momentum_breakdown"
    )
    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_MR", "GO")
        == "setup_mean_reversion"
    )
    assert resolve_indicator_signal_name("smc_structure", "SWEEP", "ARM") == "smc_structure"


def test_indicator_pipeline_executor_runs_specs_and_promotes_in_order() -> None:
    bars = _bars(4)
    calls: list[str] = []

    def runner(indicator_id: str, **kwargs: object) -> dict[str, object]:
        calls.append(indicator_id)
        calculate = kwargs["calculate"]
        assert callable(calculate)
        calculated = calculate()
        assert isinstance(calculated, dict)
        return {
            **calculated,
            "id": indicator_id,
            "latest": {"state": "READY"},
            "params": kwargs.get("params"),
        }

    def promoter(indicator_id: str, indicator: dict[str, object]) -> list[SignalCandidate]:
        return [
            SignalCandidate(
                indicator_id,
                Direction.LONG,
                70,
                100,
                str(indicator["latest"]),
                DomainFact("pipeline_candidate"),
                finality=CandidateFinality.CONFIRMED,
            )
        ]

    result = execute_indicator_pipeline(
        [
            IndicatorExecutionSpec(
                "first",
                bars,
                bars[-1],
                "confirmed",
                lambda: {},
                params={"a": 1},
                runtime_params=_runtime_params(enabled={"first": True}),
            ),
            IndicatorExecutionSpec(
                "second",
                bars,
                bars[-1],
                "disabled",
                lambda: {},
                runtime_params=_runtime_params(enabled={"second": True}),
                promote=False,
            ),
        ],
        runner=runner,
        promoter=promoter,
    )

    assert calls == ["first", "second"]
    assert list(result.indicators) == ["first", "second"]
    assert [candidate.name for candidate in result.candidates] == ["first"]
    assert result.indicators["first"]["params"] == {"a": 1}


def test_indicator_execution_spec_accepts_one_real_calculation_contract() -> None:
    bars = _bars(2)

    def runner(indicator_id: str, **kwargs: object) -> dict[str, object]:
        calculate = kwargs["calculate"]
        assert callable(calculate)
        return {"id": indicator_id, **calculate()}

    context_only = IndicatorExecutionSpec(
        id="context_only",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        runtime_params=_runtime_params(enabled={"context_only": True}),
        calculate_with_context=lambda context: {"dependency_ids": sorted(context)},
        preview_calculate_with_context=lambda context: {
            "latest": {
                "dependency_ids": sorted(context),
                "state": "PREVIEW",
            },
            "events": [],
        },
        promote=False,
    )
    indicator, candidates = execute_indicator_spec(
        context_only,
        runner=runner,
        promoter=lambda *_args: [],
        context={"upstream": {"id": "upstream"}},
    )

    assert indicator["dependency_ids"] == ["upstream"]
    assert indicator["preview"]["dependency_ids"] == ["upstream"]
    assert indicator["preview"]["state"] == "PREVIEW"
    assert indicator["preview"]["confirmed"] is False
    assert candidates == []

    with pytest.raises(ValueError, match="must provide exactly one"):
        IndicatorExecutionSpec(
            id="missing",
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="confirmed",
            runtime_params=_runtime_params(enabled={"missing": True}),
        )
    with pytest.raises(ValueError, match="must provide exactly one"):
        IndicatorExecutionSpec(
            id="ambiguous",
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="confirmed",
            runtime_params=_runtime_params(enabled={"ambiguous": True}),
            calculate=lambda: {},
            calculate_with_context=lambda _context: {},
        )
    with pytest.raises(ValueError, match="at most one preview"):
        IndicatorExecutionSpec(
            id="ambiguous_preview",
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="confirmed",
            runtime_params=_runtime_params(enabled={"ambiguous_preview": True}),
            calculate=lambda: {},
            preview_calculate=lambda: {},
            preview_calculate_with_context=lambda _context: {},
        )


def test_indicator_pipeline_executor_applies_postprocess() -> None:
    bars = _bars(4)
    calls: list[str] = []

    def runner(indicator_id: str, **kwargs: object) -> dict[str, object]:
        calls.append(indicator_id)
        calculate = kwargs["calculate"]
        assert callable(calculate)
        calculated = calculate()
        assert isinstance(calculated, dict)
        return {"id": indicator_id, "value": 1, **calculated}

    result = execute_indicator_pipeline(
        [
            IndicatorExecutionSpec(
                "fresh",
                bars,
                bars[-1],
                "confirmed",
                lambda: {},
                runtime_params=_runtime_params(enabled={"fresh": True}),
                postprocess=lambda indicator: {**indicator, "value": 2},
            )
        ],
        runner=runner,
        promoter=lambda indicator_id, indicator: [
            SignalCandidate(
                indicator_id,
                Direction.LONG,
                70,
                100,
                "ok",
                DomainFact("pipeline_candidate"),
                finality=CandidateFinality.CONFIRMED,
            )
        ],
    )

    assert calls == ["fresh"]
    assert result.indicators["fresh"]["value"] == 2
    assert [candidate.name for candidate in result.candidates] == ["fresh"]


@pytest.mark.parametrize("failing_hook", ("postprocess", "preview", "promoter"))
def test_indicator_pipeline_isolates_package_hook_errors(
    failing_hook: str,
) -> None:
    bars = _bars(4)

    def fail_hook(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError(f"{failing_hook} boom")

    def promoter(
        indicator_id: str,
        _indicator: dict[str, object],
    ) -> list[SignalCandidate]:
        if indicator_id == "obvious_failure" and failing_hook == "promoter":
            raise RuntimeError("promoter boom")
        return []

    result = execute_indicator_pipeline(
        [
            IndicatorExecutionSpec(
                "obvious_failure",
                bars,
                bars[-1],
                "confirmed",
                lambda: {},
                runtime_params=_runtime_params(enabled={"obvious_failure": True}),
                postprocess=fail_hook if failing_hook == "postprocess" else None,
                preview_calculate=fail_hook if failing_hook == "preview" else None,
            ),
            IndicatorExecutionSpec(
                "w5_structure",
                bars,
                bars[-1],
                "confirmed",
                lambda: {},
                runtime_params=_runtime_params(enabled={"w5_structure": True}),
                promote=False,
            ),
        ],
        runner=run_pipeline_indicator,
        promoter=promoter,
    )

    assert set(result.indicators) == {"obvious_failure", "w5_structure"}
    assert result.indicators["obvious_failure"]["status"]["state_code"] == "error"
    assert result.indicators["w5_structure"]["status"]["state_code"] != "error"
    assert result.candidates == []


def test_indicator_result_contract_normalizes_overlays_and_signals() -> None:
    result = normalize_indicator_result(
        {
            "overlays": [
                {"type": "box", "layer": "zones", "price": 101},
                {"type": "unknown", "layer": "bad", "label": "fallback"},
            ],
            "signals": [
                {
                    "code": "TEST",
                    "overlays": [{"type": "line", "price": 100, "signal": {"action": "GO"}}],
                },
            ],
        },
        name="demo",
    )

    assert result["overlays"][0]["contract"] == OVERLAY_CONTRACT_VERSION
    assert result["overlays"][0]["source"] == "demo"
    assert result["overlays"][1]["type"] == "unknown"
    assert result["overlays"][1]["layer"] == "bad"
    assert result["signals"][0]["details"] == {}
    assert result["signals"][0]["overlays"][0]["contract"] == OVERLAY_CONTRACT_VERSION
    assert result["signals"][0]["overlays"][0]["signal_overlay"] is True


@pytest.mark.parametrize(
    "raw, error",
    [
        ({"overlays": None}, "field overlays must be a list"),
        ({"signals": {}}, "field signals must be a list"),
        ({"overlays": [None, 42, "bad"]}, "overlay item 0 must be a mapping"),
        ({"signals": [None]}, "signal item 0 must be a mapping"),
        (
            {"signals": [{"overlays": [None]}]},
            "signal overlay item 0 must be a mapping",
        ),
    ],
)
def test_indicator_result_normalization_rejects_malformed_raw_containers(
    raw: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        normalize_indicator_result(raw, name="demo")


def test_malformed_indicator_result_is_isolated_as_runtime_error() -> None:
    bars = _bars(3)

    result = run_indicator(
        "obvious_failure",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        calculate=lambda: {"overlays": [None], "signals": [None]},
    )

    assert result["overlays"] == []
    assert result["signals"] == []
    assert result["status"]["state_code"] == "error"
    assert "overlay item 0 must be a mapping" in result["status"]["last_error"]


def test_invalid_overlay_layer_is_not_silently_removed() -> None:
    bars = _bars(3)

    result = run_indicator(
        "obvious_failure",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        calculate=lambda: {
            "overlays": [
                {
                    "type": "label",
                    "layer": "not-a-layer",
                    "ts": bars[-1].ts.isoformat(),
                    "price": bars[-1].close,
                    "label": "bad",
                }
            ]
        },
    )

    assert result["overlays"] == []
    assert result["status"]["state_code"] == "error"
    assert "overlay has invalid layer" in result["status"]["last_error"]


def test_domain_fact_contract_is_canonical_and_rejects_display_rescue_fields() -> None:
    fact = DomainFact("breakout_confirmed", {"direction": "long", "score": 84.0})

    assert fact.as_dict() == {
        "code": "breakout_confirmed",
        "direction": "long",
        "score": 84.0,
    }
    with pytest.raises(ValueError, match="lowercase canonical"):
        DomainFact("BREAKOUT CONFIRMED")
    with pytest.raises(ValueError, match="lowercase canonical"):
        DomainFact("_breakout_confirmed")
    with pytest.raises(ValueError, match="legacy or display"):
        DomainFact("breakout_confirmed", {"message": "parse me"})
    with pytest.raises(ValueError, match="must contain a canonical code"):
        DomainFact.from_mapping({"direction": "long"})


def test_indicator_fact_payload_accepts_only_typed_fact_shapes() -> None:
    payload = indicator_fact_payload(
        scenario="breakout",
        setup="momentum",
        trigger_event=DomainFact("breakout_confirmed", {"direction": "long"}),
        supporting={"code": "volume_confirmed"},
        risk={"code": "stop_invalidation", "blocks": [{"code": "trend_conflict"}]},
        quality={"code": "score", "value": 86},
        fact_groups=[{"kind": "confirmation", "items": [{"code": "close_confirmed"}]}],
    )

    assert payload["trigger_event"] == {"code": "breakout_confirmed", "direction": "long"}
    assert payload["evidence"]["supporting"] == [{"code": "volume_confirmed"}]
    assert payload["risk"]["blocks"] == [{"code": "trend_conflict"}]
    with pytest.raises(TypeError, match="DomainFact or canonical fact mapping"):
        indicator_fact_payload(trigger_event="breakout confirmed")
    with pytest.raises(TypeError, match="sequence of facts"):
        indicator_fact_payload(supporting="volume confirmed")
    with pytest.raises(ValueError, match="display or undeclared fields"):
        indicator_fact_payload(fact_groups={"kind": "legacy", "rows": [["display row"]]})


def test_indicator_metric_number_uses_one_typed_fact_contract() -> None:
    assert metric_number(12.345, digits=3) == {"value": 12.345, "digits": 3}
    assert metric_number(None, suffix=" ATR") == {
        "value": None,
        "digits": 2,
        "suffix": " ATR",
    }


def test_decision_context_is_the_strict_immutable_decision_boundary() -> None:
    bars = tuple(_bars(3))
    context = DecisionContext(
        candidates=(),
        bars=bars,
        atr_value=1.25,
        strategy_mode=StrategyMode.BALANCED,
        option_flow={"option_rvol": 2.0},
        instrument_profile=ES_PROFILE,
        price_increment=0.25,
    )

    assert context.bars == bars
    assert context.atr_value == 1.25
    with pytest.raises(TypeError):
        context.option_flow["option_rvol"] = 1.0  # type: ignore[index]
    with pytest.raises(TypeError, match="tuple of SignalCandidate"):
        DecisionContext(
            candidates=[],  # type: ignore[arg-type]
            bars=bars,
            atr_value=1.0,
            strategy_mode=StrategyMode.BALANCED,
            option_flow=None,
            instrument_profile=ES_PROFILE,
            price_increment=0.25,
        )
    with pytest.raises(ValueError, match="strictly chronological"):
        DecisionContext(
            candidates=(),
            bars=(bars[1], bars[0]),
            atr_value=1.0,
            strategy_mode=StrategyMode.BALANCED,
            option_flow=None,
            instrument_profile=ES_PROFILE,
            price_increment=0.25,
        )


def test_indicator_contract_rejects_nested_signal_legacy_trigger_strings() -> None:
    bars = _bars(3)
    with pytest.raises(ValueError, match="latest signal has invalid indicator facts"):
        attach_indicator_status(
            "demo",
            normalize_indicator_result(
                {"latest": {"signal": {"trigger_event": "legacy display trigger"}}},
                name="demo",
            ),
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="test",
        )


def test_indicator_contract_validates_structured_trade_plan_payloads() -> None:
    bars = _bars(4)
    result = attach_indicator_status(
        "demo",
        normalize_indicator_result(
            {
                "overlays": [
                    {
                        "type": "label",
                        "ts": bars[-1].ts.isoformat(),
                        "price": bars[-1].close,
                        "label": "DEMO",
                        "trade_plan": {
                            "source": "demo",
                            "direction": "long",
                            "entry": 100.0,
                            "trigger": 100.0,
                            "stop": 98.0,
                            "target": 104.0,
                            "complete": True,
                            "coherent": True,
                            "actionable": True,
                        },
                    }
                ],
                "signals": [
                    {
                        "source": "demo",
                        "trade_plan": {
                            "source": "demo",
                            "direction": "short",
                            "entry": 100.0,
                            "trigger": 100.0,
                            "stop": 102.0,
                            "target": 96.0,
                            "complete": True,
                            "coherent": True,
                            "actionable": True,
                        },
                    }
                ],
            },
            name="demo",
        ),
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )

    validate_indicator_result_contract(result, name="demo")


def test_indicator_contract_rejects_runtime_payload_shape_mismatches() -> None:
    bars = _bars(4)
    result = attach_indicator_status(
        "demo",
        normalize_indicator_result(
            {
                "series": [{"ts": bars[-1].ts.isoformat(), "state": "OK"}],
                "events": [{"ts": bars[-1].ts.isoformat(), "code": "OK"}],
                "latest": {"state": "OK"},
                "overlays": [
                    {"type": "line", "ts": bars[-1].ts.isoformat(), "price": bars[-1].close},
                ],
                "signals": [
                    {
                        "code": "OK",
                        "overlays": [
                            {
                                "type": "label",
                                "ts": bars[-1].ts.isoformat(),
                                "price": bars[-1].close,
                                "label": "OK",
                            }
                        ],
                    },
                ],
            },
            name="demo",
        ),
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )
    validate_indicator_result_contract(result, name="demo")

    bad_latest = {**result, "latest": "bad"}
    try:
        validate_indicator_result_contract(bad_latest, name="demo")
    except ValueError as exc:
        assert "field latest must be a mapping or null" in str(exc)
    else:
        raise AssertionError("invalid latest contract accepted")

    bad_series = {**result, "series": ["bad"]}
    try:
        validate_indicator_result_contract(bad_series, name="demo")
    except ValueError as exc:
        assert "series item must be a mapping" in str(exc)
    else:
        raise AssertionError("invalid series contract accepted")

    bad_overlay = {**result, "overlays": [{"type": "line", "contract": OVERLAY_CONTRACT_VERSION}]}
    try:
        validate_indicator_result_contract(bad_overlay, name="demo")
    except ValueError as exc:
        assert "line overlay missing renderer fields" in str(exc)
    else:
        raise AssertionError("invalid overlay renderer contract accepted")

    malformed_projected_overlay = {
        **result,
        "overlays": [
            {
                "type": "line",
                "contract": OVERLAY_CONTRACT_VERSION,
                "start_ts": bars[-1].ts.isoformat(),
                "end_ts": bars[-1].ts.isoformat(),
                "end_anchor_ts": bars[-1].ts.isoformat(),
                "end_bar_offset": 0,
                "y1": bars[-1].close,
                "y2": bars[-1].close,
            },
        ],
    }
    try:
        validate_indicator_result_contract(malformed_projected_overlay, name="demo")
    except ValueError as exc:
        assert "projected_end" in str(exc)
    else:
        raise AssertionError("ambiguous or malformed projected endpoint accepted")

    invalid_renderer_values = {
        **result,
        "overlays": [
            {
                "type": "box",
                "contract": OVERLAY_CONTRACT_VERSION,
                "start_ts": "2026-01-01 14:30:00",
                "end_ts": bars[-1].ts.isoformat(),
                "top": float("nan"),
                "bottom": bars[-1].low,
            },
        ],
    }
    with pytest.raises(ValueError, match="invalid UTC timestamps"):
        validate_indicator_result_contract(invalid_renderer_values, name="demo")

    invalid_renderer_values["overlays"][0]["start_ts"] = bars[-1].ts.isoformat()
    with pytest.raises(ValueError, match="non-finite renderer values"):
        validate_indicator_result_contract(invalid_renderer_values, name="demo")

    invalid_renderer_values["overlays"][0]["top"] = bars[-1].high
    invalid_renderer_values["overlays"][0]["retention"] = "forever"
    with pytest.raises(ValueError, match="invalid retention"):
        validate_indicator_result_contract(invalid_renderer_values, name="demo")

    invalid_renderer_values["overlays"][0]["retention"] = "active"
    invalid_renderer_values["overlays"][0]["start_bar_slot"] = "yes"
    with pytest.raises(ValueError, match="schedule-ranked bar slots are forbidden"):
        validate_indicator_result_contract(invalid_renderer_values, name="demo")

    invalid_signal_overlay = {
        **result,
        "overlays": [{**result["overlays"][0], "signal_overlay": "true"}],
    }
    with pytest.raises(ValueError, match="signal_overlay must be a boolean"):
        validate_indicator_result_contract(invalid_signal_overlay, name="demo")

    bad_glyph_only = {
        **result,
        "overlays": [
            {
                "type": "label",
                "contract": OVERLAY_CONTRACT_VERSION,
                "ts": bars[-1].ts.isoformat(),
                "price": bars[-1].close,
                "label": "GO",
                "glyph_only": True,
            },
        ],
    }
    try:
        validate_indicator_result_contract(bad_glyph_only, name="demo")
    except ValueError as exc:
        assert "label overlay missing renderer fields" in str(exc)
        assert "glyph" in str(exc)
    else:
        raise AssertionError("glyph-only overlay without typed glyph accepted")

    typed_glyph_only = {
        **result,
        "overlays": [
            {
                "type": "label",
                "contract": OVERLAY_CONTRACT_VERSION,
                "ts": bars[-1].ts.isoformat(),
                "price": bars[-1].close,
                "code": "setup_momentum_breakout",
                "glyph_only": True,
            },
        ],
    }
    validate_indicator_result_contract(typed_glyph_only, name="demo")

    renderer_owned_table = {
        **result,
        "overlays": [
            {
                "type": "table",
                "contract": OVERLAY_CONTRACT_VERSION,
                "table": {"model_ref": "market_spotlight"},
            },
        ],
    }
    validate_indicator_result_contract(renderer_owned_table, name="demo")

    bad_type = {
        **result,
        "overlays": [
            {
                "type": "bad_shape",
                "contract": OVERLAY_CONTRACT_VERSION,
                "ts": bars[-1].ts.isoformat(),
                "price": bars[-1].close,
                "label": "BAD",
            },
        ],
    }
    try:
        validate_indicator_result_contract(bad_type, name="demo")
    except ValueError as exc:
        assert "overlay has invalid type" in str(exc)
    else:
        raise AssertionError("invalid overlay type accepted")

    try:
        validate_indicator_result_contract(
            result,
            name="demo",
            renderer_contract={"primitives": ["label"], "placements": ["price"]},
        )
    except ValueError as exc:
        assert "line overlay not declared by renderer primitives" in str(exc)
    else:
        raise AssertionError("undeclared renderer primitive accepted")

    table_result = attach_indicator_status(
        "demo",
        normalize_indicator_result(
            {
                "overlays": [
                    {"type": "table", "table": {"columns": []}},
                ],
            },
            name="demo",
        ),
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )
    try:
        validate_indicator_result_contract(
            table_result,
            name="demo",
            renderer_contract={"primitives": ["table"], "placements": ["price"]},
        )
    except ValueError as exc:
        assert "table overlay requires renderer placement table" in str(exc)
    else:
        raise AssertionError("invalid renderer placement accepted")

    try:
        validate_indicator_result_contract(
            {**result, "unexpected_payload": True},
            name="demo",
            runtime_payload_contract={"top_level": [], "latest": ["state"]},
        )
    except ValueError as exc:
        assert "result has undeclared runtime payload fields" in str(exc)
    else:
        raise AssertionError("undeclared top-level runtime payload field accepted")

    try:
        validate_indicator_result_contract(
            result,
            name="demo",
            runtime_payload_contract={
                "top_level": [
                    "series",
                    "events",
                    "overlays",
                    "latest",
                    "settings",
                    "signals",
                    "contract",
                    "id",
                    "status",
                ],
                "latest": ["ts"],
                "series": ["ts", "state"],
                "events": ["ts", "code"],
                "signals": ["code", "overlays", "details"],
            },
        )
    except ValueError as exc:
        assert "latest has undeclared runtime payload fields" in str(exc)
    else:
        raise AssertionError("undeclared latest runtime payload field accepted")

    custom_result = {
        **result,
        "custom_rows": [{"ts": bars[-1].ts.isoformat(), "state": "OK", "extra": True}],
    }
    try:
        validate_indicator_result_contract(
            custom_result,
            name="demo",
            runtime_payload_contract={
                "top_level": [
                    "series",
                    "events",
                    "overlays",
                    "latest",
                    "settings",
                    "signals",
                    "contract",
                    "id",
                    "status",
                    "custom_rows",
                ],
                "custom_rows": ["ts", "state"],
            },
        )
    except ValueError as exc:
        assert "custom_rows has undeclared runtime payload fields" in str(exc)
    else:
        raise AssertionError("undeclared custom runtime payload field accepted")


def test_serialize_candidate_preserves_scenario_kind() -> None:
    payload = serialize_candidate(
        SignalCandidate(
            "demo",
            Direction.SHORT,
            71.2,
            100.0,
            "fade",
            DomainFact("fade"),
            details={"indicator_score_weight": 1.8},
            kind=ScenarioKind.FADE,
            producer_phase=ActionPhase.WATCH,
            finality=CandidateFinality.CONFIRMED,
        )
    )

    assert payload["kind"] == "fade"
    assert payload["producer_phase"] == "WATCH"
    assert payload["score"] == 71.2
    assert payload["raw_score"] == 71.2
    assert payload["reliability_weight"] == 1.25
    assert payload["final_rank_score"] == 89.0
    assert payload["status"] == {
        "stage": "execution",
        "confirmation": "confirmed",
        "finality": "confirmed",
        "execution_candidate": True,
        "decision_eligible": True,
        "reason": "confirmed",
    }


def test_serialize_candidate_exposes_preview_status_contract() -> None:
    payload = serialize_candidate(
        SignalCandidate(
            "sfp",
            Direction.LONG,
            68.0,
            100.0,
            "live sweep",
            DomainFact("live_sweep"),
            details={"live_bar": True, "execution_candidate": False},
            kind=ScenarioKind.FADE,
            finality=CandidateFinality.PROVISIONAL,
        )
    )

    assert payload["status"] == {
        "stage": "preview",
        "confirmation": "provisional",
        "finality": "provisional",
        "execution_candidate": False,
        "decision_eligible": False,
        "reason": "live_bar",
    }
    assert payload["producer_phase"] is None


def test_attached_empty_indicator_results_satisfy_strict_contract() -> None:
    bars = _bars(8)
    for indicator_id in INDICATOR_REGISTRY:
        result = attach_indicator_status(
            indicator_id,
            empty_indicator_result(indicator_id),
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="test",
        )
        _validate_registered_indicator_result(indicator_id, result)


def test_run_indicator_returns_contract_result_on_calculation_error() -> None:
    bars = _bars(8)

    def fail() -> dict[str, object]:
        raise RuntimeError("indicator failed")

    result = run_indicator(
        "impulse_fib",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
        calculate=fail,
        params={"enabled": True},
    )

    _validate_registered_indicator_result("impulse_fib", result)
    assert result["status"]["state_code"] == "error"
    assert result["status"]["trigger_event"] == {"code": "indicator_error"}
    assert result["status"]["last_error"] == "indicator failed"


def test_martin_carlo_forecast_payload_is_registered_runtime_contract() -> None:
    bars = _bars(45)
    params = MartinCarloParams(
        lookback=20,
        horizon=5,
        simulations=30,
        price_bins=6,
        paths=4,
        candle_bias=False,
        nn_bias=False,
        structural_bias=False,
        regime_filter=False,
        impulse_bias=False,
        option_bias=False,
    )
    result = run_indicator(
        "martin_carlo",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
        calculate=lambda: martin_carlo(bars, params=params),
        params=params,
    )

    _validate_registered_indicator_result("martin_carlo", result)
    assert result["latest"]["forecast"]["kind"] == "martin_carlo_forecast"
    assert result["overlays"][0]["type"] == "custom"
    assert result["overlays"][0]["renderer_ref"] == "martin_carlo_forecast"
    assert result["overlays"][0]["payload"]["kind"] == "martin_carlo_forecast"
    assert result["overlays"][0]["render_key"]
    assert result["series"] == []
    assert result["events"] == []
    assert result["signals"] == []
    assert INDICATOR_REGISTRY["martin_carlo"].candidate_promoter == "none"
    assert INDICATOR_REGISTRY["martin_carlo"].paper_tradable is False

    for missing_field in ("renderer_ref", "payload", "render_key"):
        invalid_overlay = dict(result["overlays"][0])
        invalid_overlay.pop(missing_field)
        invalid = {**result, "overlays": [invalid_overlay]}
        with pytest.raises(ValueError):
            _validate_registered_indicator_result("martin_carlo", invalid)

    for field, invalid_value in (
        ("renderer_ref", 42),
        ("render_key", 42),
        ("payload", {"invalid": float("nan")}),
    ):
        invalid_overlay = {
            **result["overlays"][0],
            field: invalid_value,
        }
        invalid = {**result, "overlays": [invalid_overlay]}
        with pytest.raises(ValueError):
            _validate_registered_indicator_result("martin_carlo", invalid)

    invalid_renderer = {
        **result,
        "overlays": [
            {
                **result["overlays"][0],
                "renderer_ref": "unregistered_forecast",
            }
        ],
    }
    with pytest.raises(ValueError):
        _validate_registered_indicator_result("martin_carlo", invalid_renderer)

    obsolete_type = {
        **result,
        "overlays": [
            {
                **result["overlays"][0],
                "type": "martin_carlo_forecast",
            }
        ],
    }
    with pytest.raises(ValueError):
        _validate_registered_indicator_result("martin_carlo", obsolete_type)


def test_wolfe_structure_path_payload_is_registered_runtime_contract() -> None:
    base = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    prices = [
        101,
        100,
        99,
        97,
        95,
        94,
        96,
        98,
        102,
        105,
        108,
        105,
        101,
        97,
        93,
        90,
        92,
        94,
        96,
        98,
        100,
        102,
        104,
    ]
    bars = [
        Bar("SPY", base + timedelta(minutes=index), price, price + 1, price - 1, price, 1000, "1m")
        for index, price in enumerate(prices)
    ]
    result = run_indicator(
        "wolfe_structure",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
        calculate=lambda: wolfe_structure(bars, pivot_len=2),
        params={"pivot_len": 2},
    )

    _validate_registered_indicator_result("wolfe_structure", result)
    assert result["latest"]["projected_p5"]
    assert result["latest"]["signal"]["source"] == "wolfe_structure"
    assert result["overlays"]
    assert {item["control_key"] for item in result["overlays"]} >= {"heuristicPath"}
    pivot_markers = [item for item in result["overlays"] if item.get("role") == "structure_pivot"]
    assert {item.get("pivot_number") for item in pivot_markers} >= {1, 2, 3, 4}
    assert {item.get("structure_phase") for item in pivot_markers} == {"path"}
    assert all("marker_shape" not in item and "label" not in item for item in pivot_markers)
    projected_w5 = next(
        item
        for item in result["overlays"]
        if item.get("geometry_segment") == "four_five"
        and item.get("role") == "wolfe_projected_pivot"
    )
    assert projected_w5["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert projected_w5["end_bar_offset"] >= 1
    assert "end_ts" not in projected_w5
    assert projected_w5["label"] == "W5"
    assert projected_w5["tone"] == "info"
    assert projected_w5["style"] == "dotted"
    assert projected_w5["width"] == 0.8
    assert projected_w5["opacity"] == 0.75
    assert any(item.get("geometry_segment") == "two_four" for item in result["overlays"])
    target = next(item for item in result["overlays"] if item.get("role") == "wolfe_target")
    assert target["geometry_segment"] == "epa"
    assert target["control_keys"] == ["priceTarget", "heuristicPath"]
    assert target["label"] == "W EPA"
    assert target["tone"] == "warning"
    assert target["style"] == "dotted"
    assert target["width"] == 0.85
    assert target["opacity"] == 0.75
    assert target["arrow_head"] is True
    assert target["arrow_size"] == 8
    assert all(item["tone"] == "info" for item in pivot_markers)


def test_analyze_bars_outputs_registered_indicator_contracts() -> None:
    snapshot = analyze_bars(
        _bars(80), indicator_params={"global_defaults": {}, "martin_carlo": {"enabled": False}}
    )

    assert set(snapshot["indicators"]) <= set(indicator_ids())
    assert "gex_dynamics" not in snapshot["indicators"]
    assert "martin_carlo" not in snapshot["indicators"]
    assert "ai_third_opinion" not in snapshot["indicators"]
    for indicator_id, result in snapshot["indicators"].items():
        _validate_registered_indicator_result(indicator_id, result)


def test_ai_third_opinion_outputs_advisory_context_only() -> None:
    snapshot = analyze_bars(
        _bars(80),
        indicator_params={
            "global_defaults": {},
            "martin_carlo": {"enabled": False},
            "option_reversal": {"enabled": False},
            "ai_third_opinion": {"enabled": True, "visible": True, "lookback": 32},
        },
    )

    result = snapshot["indicators"]["ai_third_opinion"]
    _validate_registered_indicator_result("ai_third_opinion", result)
    assert result["latest"]["phase"] in {
        "trend_up",
        "trend_down",
        "range",
        "compression",
        "reversal_risk",
        "unclear",
    }
    assert result["latest"]["market_view"] in {"long", "short", "wait"}
    assert result["latest"]["headline"]
    assert result["latest"]["pattern"]
    assert result["latest"]["trigger_event"]["code"]
    assert "trigger_label" not in result["latest"]
    assert result["latest"]["connector"]["authoritative"] is False
    assert result["latest"]["connector"]["provider"] == "openai"
    assert result["latest"]["connector"]["model"] == "default"
    assert result["latest"]["lens"] == "direct"
    assert result["latest"]["ai_market_view"] == result["latest"]["market_view"]
    assert result["latest"]["connector"]["health"] == {
        "state": "local",
        "label": "LOCAL",
        "detail": "off",
    }
    assert result["latest"]["connector"]["channel_context_requested"] is True
    assert result["latest"]["connector"]["response_bars_ago"] is None
    assert result["latest"]["signal"]["blocked"] is True
    assert result["latest"]["signal"]["blocked_reason"] == "advisory_context_only"
    assert result["table"]["ai_status"] == "local_advisory"
    assert result["table"]["connector_health"] == {
        "state": "local",
        "label": "LOCAL",
        "detail": "off",
    }
    assert result["table"]["ai_thinking"] is False
    assert result["table"]["thinking_ref"] == "ai_third_opinion_discussion"
    assert result["table"]["columns"][3]["cells"] == ["AI", "LOCAL", "local"]
    assert result["table"]["columns"][4]["cells"] == ["HLTH", "LOCAL", "off"]
    assert result["table"]["columns"][-1]["cells"] == ["ASK", "SEND", "context"]
    assert result["table"]["columns"][-1]["action"] == "ai-third-opinion-discuss-send"
    assert result["signals"] == []
    assert all(candidate["name"] != "ai_third_opinion" for candidate in snapshot["candidates"])

    external_snapshot = analyze_bars(
        _bars(80),
        indicator_params={
            "global_defaults": {},
            "martin_carlo": {"enabled": False},
            "option_reversal": {"enabled": False},
            "ai_third_opinion": {"enabled": True, "visible": True, "external": True},
        },
    )
    external_result = external_snapshot["indicators"]["ai_third_opinion"]
    assert external_result["table"]["ai_status"] == "pending_external_worker"
    assert external_result["latest"]["connector"]["provider"] == "openai"
    assert external_result["table"]["connector_health"] == {
        "state": "pending",
        "label": "PEND",
        "detail": "call",
    }
    assert external_result["table"]["ai_thinking"] is True
    assert external_result["table"]["thinking_ref"] == "ai_third_opinion_discussion"
    assert external_result["table"]["columns"][3]["cells"] == ["AI", "PEND", "waiting"]
    assert external_result["table"]["columns"][4]["cells"] == ["HLTH", "PEND", "call"]
    assert external_result["table"]["columns"][-1]["action"] == "ai-third-opinion-discuss-send"

    manual_snapshot = analyze_bars(
        _bars(80),
        indicator_params={
            "global_defaults": {},
            "martin_carlo": {"enabled": False},
            "option_reversal": {"enabled": False},
            "ai_third_opinion": {
                "enabled": True,
                "visible": True,
                "external": True,
                "refresh_mode": "manual",
            },
        },
    )
    manual_result = manual_snapshot["indicators"]["ai_third_opinion"]
    assert manual_result["latest"]["connector"]["status"] == "manual_refresh"
    assert manual_result["latest"]["connector"]["external_requested"] is False
    assert manual_result["latest"]["connector"]["external_enabled"] is True
    assert manual_result["latest"]["connector"]["refresh_mode"] == "manual"
    assert manual_result["table"]["connector_health"] == {
        "state": "manual",
        "label": "MAN",
        "detail": "ask",
    }
    assert manual_result["table"]["ai_thinking"] is False
    assert manual_result["table"]["thinking_ref"] == "ai_third_opinion_discussion"
    assert manual_result["table"]["columns"][3]["cells"] == ["AI", "MAN", "manual"]
    assert manual_result["table"]["columns"][4]["cells"] == ["HLTH", "MAN", "ask"]


def test_ai_third_opinion_contrarian_lens_keeps_raw_ai_view() -> None:
    snapshot = analyze_bars(
        _bars(80),
        indicator_params={
            "global_defaults": {},
            "martin_carlo": {"enabled": False},
            "option_reversal": {"enabled": False},
            "ai_third_opinion": {"enabled": True, "visible": True, "lens": "contrarian"},
        },
    )

    latest = snapshot["indicators"]["ai_third_opinion"]["latest"]
    table = snapshot["indicators"]["ai_third_opinion"]["table"]

    assert latest["lens"] == "contrarian"
    assert latest["ai_market_view"] in {"long", "short", "wait"}
    if latest["ai_market_view"] == "long":
        assert latest["market_view"] == "short"
    elif latest["ai_market_view"] == "short":
        assert latest["market_view"] == "long"
    else:
        assert latest["market_view"] == "wait"
    assert table["columns"][0]["cells"][2].startswith("AI ")
    assert "context_lines" not in latest
    assert latest["metrics"]["raw_ai_view"] in {"LONG", "SHORT", "WAIT"}
    assert latest["metrics"]["lens"] == "contrarian"
    assert latest["trigger_event"]


def test_analyze_bars_outputs_backend_chart_guides() -> None:
    plain_snapshot = analyze_bars(
        _bars(40),
        indicator_params={
            "global_defaults": {"ema": {"pullback": 13, "slow": 34, "magnet": 89}},
            "martin_carlo": {"enabled": False},
        },
    )
    plain_guide_meta = plain_snapshot["chart_guides"]["meta"]
    assert plain_guide_meta["bar_count"] == len(plain_snapshot["bars"])
    assert plain_guide_meta["calculation_bar_count"] == len(plain_snapshot["bars"])
    assert len(plain_guide_meta["generation"]) == 16
    assert "indicator_timings_ms" not in plain_snapshot["meta"]

    snapshot = analyze_bars(
        _bars(40),
        indicator_params={
            "global_defaults": {"ema": {"pullback": 13, "slow": 34, "magnet": 89}},
            "martin_carlo": {"enabled": False},
        },
        include_telemetry=True,
    )

    guides = snapshot["chart_guides"]
    assert guides["lengths"] == {"pullback": 13, "trend": 34, "magnet": 89}
    assert set(guides["ema"]) == {"13", "34", "89"}
    assert len(guides["ema"]["89"]) == len(snapshot["bars"])
    assert len(guides["vwap"]) == len(snapshot["bars"])
    assert guides["latest"]["ema_magnet"] is not None
    assert guides["latest"]["ema_pullback"] is not None
    assert guides["latest"]["ema_trend"] is not None
    assert guides["latest"]["vwap"] is not None
    assert guides["latest"]["atr14"] is not None
    assert guides["vwap"][-1]["vwap"] is not None
    assert guides["meta"]["bar_count"] == len(snapshot["bars"])
    assert guides["meta"]["compute_ms"] >= 0
    assert isinstance(snapshot["meta"]["indicator_timings_ms"], dict)
    assert all(value >= 0 for value in snapshot["meta"]["indicator_timings_ms"].values())


def test_5m_ema233_uses_deep_db_warmup_without_expanding_display_axis() -> None:
    from aef_terminal.engine.analysis_db import chart_guide_calculation_bars

    base = datetime(2026, 7, 6, 8, 0, tzinfo=UTC)
    deep_bars = [
        Bar(
            "SPY",
            base + timedelta(minutes=index * 5),
            100 + index * 0.02,
            100.4 + index * 0.02,
            99.6 + index * 0.02,
            100.1 + index * 0.02,
            1000 + index,
            "5m",
        )
        for index in range(750)
    ]
    display_bars = deep_bars[-200:]
    committed_after_display = Bar(
        "SPY",
        display_bars[-1].ts + timedelta(minutes=5),
        200.0,
        201.0,
        199.0,
        200.5,
        1500,
        "5m",
    )
    calculation_bars = chart_guide_calculation_bars(
        display_bars,
        display_bars,
        interval="5m",
        warmup_bars=[*deep_bars, committed_after_display],
    )
    display_axis = serialize_bars(display_bars)
    instrument = instrument_with_bar_sessions(ibkr_stock_payload("SPY"), calculation_bars)
    feature_context = _build_feature_context(
        calculation_bars,
        instrument_profile=ES_PROFILE,
    )
    guides = build_chart_guides(
        calculation_bars,
        symbol="SPY",
        feature_context=feature_context,
        instrument=instrument,
        display_axis=display_axis,
    )

    assert len(calculation_bars) == 750
    assert calculation_bars[-1].ts == display_bars[-1].ts
    assert len(display_axis) == len(display_bars) == 200
    assert len(guides["meta"]["generation"]) == 16
    assert guides["ema"]["233"] == feature_context.ema_magnet[-200:]
    assert guides["ema_status"]["magnet"] == {
        "available": True,
        "warmup_complete": True,
        "warmup_status": "complete",
        "reason": "",
        "length": 233,
        "required_bar_count": 233,
        "target_warmup_bar_count": 699,
        "calculation_bar_count": 750,
    }
    shallow_context = _build_feature_context(display_bars, instrument_profile=ES_PROFILE)
    assert guides["ema"]["233"][-1] != shallow_context.ema_magnet[-1]


def test_5m_ema233_fails_closed_below_minimum_without_hiding_shorter_emas() -> None:
    base = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
    bars = [
        Bar(
            "SPY",
            base + timedelta(minutes=index * 5),
            100 + index * 0.01,
            100.3 + index * 0.01,
            99.7 + index * 0.01,
            100.1 + index * 0.01,
            1000 + index,
            "5m",
        )
        for index in range(200)
    ]
    feature_context = _build_feature_context(bars, instrument_profile=ES_PROFILE)
    guides = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=feature_context,
        instrument=instrument_with_bar_sessions(ibkr_stock_payload("SPY"), bars),
        magnet_warmup_failed=True,
    )

    assert all(value is None for value in guides["ema"]["233"])
    assert any(value is not None for value in guides["ema"]["20"])
    assert any(value is not None for value in guides["ema"]["55"])
    assert guides["latest"]["ema_magnet"] is None
    assert guides["ema_status"]["magnet"]["available"] is False
    assert guides["ema_status"]["magnet"]["warmup_complete"] is False
    assert guides["ema_status"]["magnet"]["warmup_status"] == "failed"
    assert guides["ema_status"]["magnet"]["reason"] == "insufficient_calculation_bars"


def test_5m_ema233_reports_limited_warmup_without_hiding_the_line() -> None:
    base = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    bars = [
        Bar(
            "SPY",
            base + timedelta(minutes=index * 5),
            100 + index * 0.01,
            100.3 + index * 0.01,
            99.7 + index * 0.01,
            100.1 + index * 0.01,
            1000 + index,
            "5m",
        )
        for index in range(400)
    ]
    feature_context = _build_feature_context(bars, instrument_profile=ES_PROFILE)
    guides = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=feature_context,
        instrument=instrument_with_bar_sessions(ibkr_stock_payload("SPY"), bars),
    )

    assert guides["ema"]["233"][-1] is not None
    assert guides["ema"]["20"][-1] is not None
    assert guides["ema"]["55"][-1] is not None
    assert guides["ema_status"]["magnet"] == {
        "available": True,
        "warmup_complete": False,
        "warmup_status": "limited",
        "reason": "limited_warmup",
        "length": 233,
        "required_bar_count": 233,
        "target_warmup_bar_count": 699,
        "calculation_bar_count": 400,
    }


def test_chart_context_uses_provider_intervals_and_bar_time() -> None:
    bars = _bars(40)
    instrument = ibkr_stock_payload("SPY")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "ibkr_trading_hours",
        "trading_intervals": [
            {
                "session_date": "2026-01-01",
                "opens_at": "2026-01-01T14:30:00+00:00",
                "closes_at": "2026-01-01T22:00:00+00:00",
                "status": "open",
            }
        ],
        "liquid_intervals": [
            {
                "session_date": "2026-01-01",
                "opens_at": "2026-01-01T14:30:00+00:00",
                "closes_at": "2026-01-01T22:00:00+00:00",
                "status": "open",
            }
        ],
    }

    guides = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=build_feature_context(bars),
        instrument=instrument,
    )

    context = guides["context"]
    assert context["day"]["key"] == "2026-01-01"
    assert context["sessions"]["liquid"]["count"] == len(bars)
    assert context["openingRange"]["startTs"] == "2026-01-01T14:30:00+00:00"
    assert context["openingRange"]["endTs"] == "2026-01-01T15:00:00+00:00"
    assert context["openingRange"]["sessionEndTs"] == "2026-01-01T21:00:00+00:00"
    assert context["openingRange"]["complete"] is True
    assert context["openingRange"]["source"] == "provider_new_york_range_context"
    assert context["openingRange"]["authority"] == "display_only"
    assert context["openingRange"]["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    ("session_date", "opening_start", "session_end", "early_start", "early_end"),
    [
        (
            "2026-01-05",
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            datetime(2026, 1, 5, 21, 0, tzinfo=UTC),
            datetime(2026, 1, 5, 8, 0, tzinfo=UTC),
            datetime(2026, 1, 5, 10, 0, tzinfo=UTC),
        ),
        (
            "2026-07-06",
            datetime(2026, 7, 6, 13, 30, tzinfo=UTC),
            datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 7, 15, tzinfo=UTC),
            datetime(2026, 7, 6, 13, 15, tzinfo=UTC),
        ),
    ],
)
def test_chart_context_selects_liquid_segment_containing_new_york_open(
    session_date: str,
    opening_start: datetime,
    session_end: datetime,
    early_start: datetime,
    early_end: datetime,
) -> None:
    bars = [
        Bar(
            "VIX",
            opening_start + timedelta(minutes=index),
            20.0 + index * 0.01,
            20.5 + index * 0.01,
            19.5 - index * 0.01,
            20.1 + index * 0.01,
            1000 + index,
            "1m",
        )
        for index in range(31)
    ]
    instrument = ibkr_stock_payload("VIX")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "ibkr_trading_hours",
        "trading_intervals": [
            {
                "session_date": session_date,
                "opens_at": early_start.isoformat(),
                "closes_at": (opening_start + timedelta(hours=8)).isoformat(),
                "status": "open",
            }
        ],
        "liquid_intervals": [
            {
                "session_date": session_date,
                "opens_at": early_start.isoformat(),
                "closes_at": early_end.isoformat(),
                "status": "open",
            },
            {
                "session_date": session_date,
                "opens_at": opening_start.isoformat(),
                "closes_at": (opening_start + timedelta(hours=7, minutes=30)).isoformat(),
                "status": "open",
            },
        ],
    }

    context = build_chart_guides(
        bars,
        symbol="VIX",
        feature_context=build_feature_context(bars),
        instrument=instrument,
    )["context"]
    opening_range = context["openingRange"]
    assert opening_range["startTs"] == opening_start.isoformat()
    assert opening_range["endTs"] == (opening_start + timedelta(minutes=30)).isoformat()
    assert opening_range["sessionEndTs"] == session_end.isoformat()
    assert opening_range["high"] == max(bar.high for bar in bars[:30])
    assert opening_range["low"] == min(bar.low for bar in bars[:30])
    assert opening_range["complete"] is True
    assert opening_range["authority"] == "display_only"


def test_chart_context_groups_split_provider_session_and_uses_previous_session_date() -> None:
    points = [
        (datetime(2026, 1, 4, 9, 0, tzinfo=UTC), 90.0),
        (datetime(2026, 1, 5, 9, 0, tzinfo=UTC), 100.0),
        (datetime(2026, 1, 5, 13, 0, tzinfo=UTC), 120.0),
        (datetime(2026, 1, 6, 9, 0, tzinfo=UTC), 200.0),
    ]
    bars = [Bar("SPY", ts, price, price, price, price, 1.0, "1m") for ts, price in points]
    intervals = [
        {
            "session_date": "2026-01-04",
            "opens_at": "2026-01-04T08:00:00+00:00",
            "closes_at": "2026-01-04T10:00:00+00:00",
            "status": "open",
        },
        {
            "session_date": "2026-01-05",
            "opens_at": "2026-01-05T08:00:00+00:00",
            "closes_at": "2026-01-05T10:00:00+00:00",
            "status": "open",
        },
        {
            "session_date": "2026-01-05",
            "opens_at": "2026-01-05T12:00:00+00:00",
            "closes_at": "2026-01-05T14:00:00+00:00",
            "status": "open",
        },
        {
            "session_date": "2026-01-06",
            "opens_at": "2026-01-06T08:00:00+00:00",
            "closes_at": "2026-01-06T10:00:00+00:00",
            "status": "open",
        },
    ]
    instrument = ibkr_stock_payload("SPY")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "ibkr_trading_hours",
        "trading_intervals": intervals,
        "liquid_intervals": intervals,
    }

    context = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=build_feature_context(bars),
        instrument=instrument,
    )["context"]

    assert context["day"]["key"] == "2026-01-06"
    assert context["day"]["count"] == 1
    assert context["previousDay"]["key"] == "2026-01-05"
    assert context["previousDay"]["count"] == 2
    assert context["previousDay"]["open"] == 100.0
    assert context["previousDay"]["close"] == 120.0


def test_new_york_range_fails_closed_without_provider_liquid_schedule() -> None:
    bars = _bars(10)
    instrument = ibkr_stock_payload("SPY")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "unknown",
        "trading_intervals": [],
        "liquid_intervals": [],
    }

    guides = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=build_feature_context(bars),
        instrument=instrument,
    )

    assert "openingRange" not in guides["context"]
    assert guides["vwap_status"]["available"] is False
    assert guides["vwap_status"]["reason_code"] == "provider_trading_intervals_missing"
    assert guides["latest"]["vwap"] is None
    assert all(item["vwap"] is None and item["sigma"] is None for item in guides["vwap"])


def test_chart_vwap_keeps_exact_recent_session_when_older_history_is_uncovered() -> None:
    bars = _bars(8)
    covered_bars = bars[4:]
    instrument = ibkr_stock_payload("SPY")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "ibkr_trading_hours",
        "trading_intervals": [
            {
                "status": "open",
                "session_date": covered_bars[0].ts.astimezone(UTC).date().isoformat(),
                "opens_at": covered_bars[0].ts.isoformat(),
                "closes_at": (covered_bars[-1].ts + timedelta(minutes=1)).isoformat(),
            }
        ],
        "liquid_intervals": [],
    }

    guides = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=build_feature_context(bars),
        instrument=instrument,
    )

    assert all(item == {"vwap": None, "sigma": None} for item in guides["vwap"][:4])
    assert all(
        item["vwap"] is not None and item["sigma"] is not None for item in guides["vwap"][4:]
    )
    assert guides["latest"]["vwap"] == guides["vwap"][-1]["vwap"]
    assert guides["vwap_status"] == {
        "available": True,
        "source": "provider_trading_intervals",
        "reason_code": "partial_provider_session_coverage",
        "calendar": "ibkr_trading_hours",
        "coverage": "partial",
        "covered_bar_count": 4,
        "bar_count": 8,
    }


def test_partial_chart_vwap_groups_disjoint_provider_intervals_and_resets_next_session() -> None:
    points = [
        (datetime(2026, 1, 5, 7, 59, tzinfo=UTC), 50.0),
        (datetime(2026, 1, 5, 9, 0, tzinfo=UTC), 100.0),
        (datetime(2026, 1, 5, 13, 0, tzinfo=UTC), 200.0),
        (datetime(2026, 1, 6, 9, 0, tzinfo=UTC), 300.0),
    ]
    bars = [Bar("SPY", ts, price, price, price, price, 1.0, "1m") for ts, price in points]
    instrument = ibkr_stock_payload("SPY")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "ibkr_trading_hours",
        "trading_intervals": [
            {
                "session_date": "2026-01-05",
                "opens_at": "2026-01-05T08:00:00+00:00",
                "closes_at": "2026-01-05T10:00:00+00:00",
                "status": "open",
            },
            {
                "session_date": "2026-01-05",
                "opens_at": "2026-01-05T12:00:00+00:00",
                "closes_at": "2026-01-05T14:00:00+00:00",
                "status": "open",
            },
            {
                "session_date": "2026-01-06",
                "opens_at": "2026-01-06T08:00:00+00:00",
                "closes_at": "2026-01-06T10:00:00+00:00",
                "status": "open",
            },
        ],
        "liquid_intervals": [],
    }

    guides = build_chart_guides(
        bars,
        symbol="SPY",
        feature_context=build_feature_context(bars),
        instrument=instrument,
    )

    assert [point["vwap"] for point in guides["vwap"]] == [None, 100.0, 150.0, 300.0]
    assert guides["vwap_status"]["coverage"] == "partial"
    assert guides["vwap_status"]["covered_bar_count"] == 3


def test_vwap_dependent_indicators_are_typed_blocked_when_provider_session_is_unknown() -> None:
    bars = _bars(40)
    instrument = ibkr_stock_payload("SPY")
    instrument["session"] = {
        "provider": "ibkr",
        "calendar": "unknown",
        "trading_intervals": [],
        "liquid_intervals": [],
    }

    snapshot = _analyze_bars(
        bars,
        instrument=instrument,
        indicator_params={
            "breakout_accumulation": {"enabled": True},
            "linda_volume": {"enabled": True},
            "market_spotlight": {"enabled": True},
            "martin_carlo": {"enabled": False},
        },
    )

    for indicator_id in ("breakout_accumulation", "linda_volume", "market_spotlight"):
        indicator = snapshot["indicators"][indicator_id]
        assert indicator["availability"]["state"] == "blocked"
        assert indicator["status"]["state_code"] == "blocked_context"
        assert indicator["status"]["health"] == "blocked"
    assert snapshot["vsa_volume"]["availability"]["state"] == "degraded"
    assert snapshot["vsa_volume"]["status"]["state"] == "degraded"
    assert "vsa_volume" not in snapshot["indicators"]


def test_feature_context_builds_shared_series() -> None:
    context = build_feature_context(_bars())

    assert context.latest_atr > 0
    assert context.latest_atr_sma > 0
    assert context.latest_rvol > 0
    assert len(context.atr_sma_values) == 40
    assert len(context.ema_pullback) == 40
    assert len(context.ema_fast) == 40
    assert context.values["instrument_profile"]
    assert not any(key.startswith("manual_channel_") for key in context.values)


def test_live_bars_differ_from_confirmed_detects_tail_updates() -> None:
    from aef_terminal.engine.analyze.inputs import live_bars_differ_from_confirmed

    confirmed = _bars()
    live = list(confirmed)
    assert live_bars_differ_from_confirmed(confirmed, live) is False
    live[-1] = Bar(
        symbol=live[-1].symbol,
        ts=live[-1].ts,
        open=live[-1].open,
        high=live[-1].high + 0.5,
        low=live[-1].low,
        close=live[-1].close + 0.25,
        volume=live[-1].volume,
        timeframe=live[-1].timeframe,
        closed=False,
    )
    assert live_bars_differ_from_confirmed(confirmed, live) is True


def test_tick_flow_context_is_consumed_only_when_registry_calc_is_enabled() -> None:
    inputs_source = Path("src/aef_terminal/engine/analyze/inputs.py").read_text(encoding="utf-8")
    bars = _bars()
    instrument = instrument_with_bar_sessions(ibkr_stock_payload("SPY"), bars)
    tick_flow = {
        "decision_eligible": True,
        "source": "broker_ticks",
        "delta": [
            {
                "ts": bars[-10].ts.isoformat(),
                "total_volume": 1000,
                "net_delta": 200,
                "trade_count": 50,
            },
            {
                "ts": bars[-1].ts.isoformat(),
                "total_volume": 3000,
                "net_delta": 1200,
                "trade_count": 100,
            },
        ],
    }

    disabled = build_analyze_inputs(
        bars,
        instrument=instrument,
        indicator_params={"tick_flow": {"enabled": False}},
        tick_flow=tick_flow,
    )
    enabled = build_analyze_inputs(
        bars,
        instrument=instrument,
        indicator_params={"tick_flow": {"enabled": True}},
        tick_flow=tick_flow,
    )

    assert disabled.tick_flow is None
    assert disabled.tick_bias is None
    assert enabled.tick_flow == tick_flow
    assert enabled.tick_bias is not None
    assert 'enabled.get("tick_flow"' not in inputs_source
    assert 'indicator_ids_for_shared_context("tick_flow")' in inputs_source


def test_confirmed_child_slots_follow_provisional_and_as_of_filtering() -> None:
    base = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    parent_bars = [
        Bar(
            "SPY",
            base + timedelta(minutes=index * 5),
            100 + index * 0.1,
            101 + index * 0.1,
            99 + index * 0.1,
            100.5 + index * 0.1,
            1000 + index,
            "5m",
        )
        for index in range(40)
    ]
    current_start = parent_bars[-1].ts + timedelta(minutes=5)
    micro_bars = [
        Bar(
            "SPY",
            current_start + timedelta(minutes=index),
            104.0,
            104.5,
            103.5,
            104.25,
            100.0,
            "1m",
            closed=index != 1,
        )
        for index in range(3)
    ]
    micro_slots = ProviderBarSlotSequence(
        (100, 101, 102),
        schedule_state="verified",
    )
    instrument = instrument_with_bar_sessions(
        ibkr_stock_payload("SPY"),
        parent_bars,
    )
    quality = {
        "1m": {
            "provider_complete": True,
        }
    }

    inputs = build_analyze_inputs(
        parent_bars,
        instrument=instrument,
        mtf_context={"1m": micro_bars},
        mtf_context_slots={"1m": micro_slots},
        mtf_quality=quality,
        analysis_as_of_utc=current_start + timedelta(minutes=3),
    )

    assert inputs.confirmed_bar_context["1m"] == [
        micro_bars[0],
        micro_bars[2],
    ]
    assert inputs.confirmed_bar_context_slots["1m"] == [100, 102]
    assert inputs.confirmed_bar_context_slots["1m"].schedule_state == "verified"
    assert inputs.confirmed_bar_context_quality["1m"]["ok"] is False
    assert inputs.confirmed_bar_context_quality["1m"]["due_provisional_bar_count"] == 1
    assert inputs.confirmed_bar_context_quality["1m"]["warning"] == (
        "provider bar finality pending"
    )

    before_forming_close = build_analyze_inputs(
        parent_bars,
        instrument=instrument,
        mtf_context={"1m": micro_bars},
        mtf_context_slots={"1m": micro_slots},
        mtf_quality=quality,
        analysis_as_of_utc=current_start + timedelta(minutes=1),
    )

    assert before_forming_close.confirmed_bar_context["1m"] == [micro_bars[0]]
    assert before_forming_close.confirmed_bar_context_slots["1m"] == [100]
    assert before_forming_close.confirmed_bar_context_quality["1m"]["ok"] is True

    missing_axis = build_analyze_inputs(
        parent_bars,
        instrument=instrument,
        mtf_context={"1m": micro_bars},
        mtf_context_slots={},
        mtf_quality=quality,
        analysis_as_of_utc=current_start + timedelta(minutes=3),
    )

    assert missing_axis.confirmed_bar_context_slots == {}
    assert missing_axis.confirmed_bar_context_quality["1m"]["ok"] is False
    assert missing_axis.confirmed_bar_context_quality["1m"]["warning"] == (
        "provider bar-slot axis unavailable"
    )


def test_analysis_context_cache_reuses_common_contexts() -> None:
    bars = _bars()
    clear_analysis_context_cache()

    first = get_cached_analysis_context(
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )
    second = get_cached_analysis_context(
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )

    assert second is first
    assert second.feature_context is first.feature_context
    assert second.live_feature_context is first.live_feature_context
    assert second.live_feature_context is second.feature_context
    assert second.structure_feature_context is first.structure_feature_context


@pytest.mark.parametrize("fails", [False, True])
def test_analysis_context_cache_coalesces_workers_without_blocking_other_keys(
    monkeypatch, fails
) -> None:
    clear_analysis_context_cache()
    bars = _bars()
    kwargs = dict(
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )
    started, joined, release = threading.Event(), threading.Event(), threading.Event()
    build = context_cache_module.build_feature_context
    calls = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    def slow_build(*args, **build_kwargs):
        if build_kwargs.get("extra") == {"demo": 1}:
            calls.append(1)
            if len(calls) == 1:
                started.set()
                assert release.wait(3)
                if fails:
                    raise ValueError("context build failed")
        return build(*args, **build_kwargs)

    monkeypatch.setattr(context_cache_module, "Future", ObservedFuture)
    monkeypatch.setattr(context_cache_module, "build_feature_context", slow_build)
    with ThreadPoolExecutor(max_workers=3) as workers:
        try:
            first = workers.submit(get_cached_analysis_context, **kwargs)
            assert started.wait(2)
            second = workers.submit(get_cached_analysis_context, **kwargs)
            assert joined.wait(2)
            independent = workers.submit(
                get_cached_analysis_context, **{**kwargs, "features_extra": {"demo": 2}}
            )
            assert independent.result(2).feature_context is not None
            release.set()
            if fails:
                for future in (first, second):
                    with pytest.raises(ValueError, match="context build failed"):
                        future.result(2)
                retried = get_cached_analysis_context(**kwargs)
                assert get_cached_analysis_context(**kwargs) is retried
                assert len(calls) == 2
            else:
                assert first.result(2) is second.result(2)
                assert get_cached_analysis_context(**kwargs) is first.result(2)
                assert len(calls) == 1
        finally:
            release.set()
    clear_analysis_context_cache()


def test_analysis_context_cache_clear_fences_a_running_build(monkeypatch) -> None:
    clear_analysis_context_cache()
    bars = _bars()
    kwargs = dict(
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra=None,
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )
    started, release = threading.Event(), threading.Event()
    build = context_cache_module.build_feature_context
    calls = []

    def slow_first_build(*args, **build_kwargs):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        return build(*args, **build_kwargs)

    monkeypatch.setattr(context_cache_module, "build_feature_context", slow_first_build)
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            first = workers.submit(get_cached_analysis_context, **kwargs)
            assert started.wait(2)
            clear_analysis_context_cache()
            replacement = workers.submit(get_cached_analysis_context, **kwargs).result(2)
            release.set()
            assert first.result(2) is not replacement
            assert get_cached_analysis_context(**kwargs) is replacement
        finally:
            release.set()
    clear_analysis_context_cache()


def test_analysis_context_cache_isolates_provider_qualified_identity() -> None:
    bars = _bars()
    clear_analysis_context_cache()

    first = get_cached_analysis_context(
        instrument_id="ibkr|contract|756733",
        route_fingerprint="ibkr|contract|756733",
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )
    second = get_cached_analysis_context(
        instrument_id="ibkr|contract|999999",
        route_fingerprint="ibkr|contract|999999",
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )

    assert second is not first


def test_analysis_context_cache_invalidates_an_earlier_confirmed_bar_revision() -> None:
    bars = _bars()
    clear_analysis_context_cache()

    first = get_cached_analysis_context(
        confirmed_bars=bars,
        live_signal_bars=bars,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=bars,
        structure_offset=0,
    )
    corrected = list(bars)
    earlier = corrected[5]
    corrected[5] = Bar(
        earlier.symbol,
        earlier.ts,
        earlier.open,
        earlier.high + 1.0,
        earlier.low,
        earlier.close,
        earlier.volume + 100.0,
        earlier.timeframe,
        earlier.source,
        earlier.closed,
        earlier.state,
    )
    second = get_cached_analysis_context(
        confirmed_bars=corrected,
        live_signal_bars=corrected,
        features_extra={"demo": 1},
        defaults=DEFAULT_INDICATOR_SETTINGS,
        structure_bars=corrected,
        structure_offset=0,
    )

    assert second is not first
    assert second.key != first.key


def test_w5_calc_gate_has_one_registry_owned_candidate_path(monkeypatch) -> None:
    bars = _bars(80)
    detector_calls: list[str] = []
    trigger = bars[-1].close
    raw_candidate = SignalCandidate(
        "w5_exhaustion",
        Direction.LONG,
        80.0,
        trigger,
        "w5_test",
        DomainFact("w5_test"),
        kind=ScenarioKind.FADE,
        source="w5",
        role="exhaustion",
        finality=CandidateFinality.CONFIRMED,
    )
    monkeypatch.setattr(
        "aef_terminal.signals.primitives.detect_sfp",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.w5_structure.detect_w5_exhaustion",
        lambda *_args, **_kwargs: detector_calls.append("w5") or [raw_candidate],
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.w5_structure.w5_structure",
        lambda *_args, **_kwargs: {
            "version": "test",
            "events": [],
            "latest": {
                "ts": bars[-1].ts.isoformat(),
                "direction": "long",
                "action": "GO",
                "score": 80.0,
                "trigger": trigger,
                "stop": trigger - 1.0,
                "target": trigger + 2.0,
                "trigger_event": {"code": "w5_test"},
                "candidate_finality": "confirmed",
            },
            "overlays": [],
        },
    )
    disabled_params = {
        indicator_id: {"enabled": False}
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.pipeline_stage != "ui"
    }
    disabled_params["global_defaults"] = {}
    enabled_params = {
        indicator_id: dict(values) for indicator_id, values in disabled_params.items()
    }
    enabled_params["w5_structure"] = {"enabled": True}
    clear_analysis_context_cache()

    disabled = analyze_bars(bars, indicator_params=disabled_params)
    enabled = analyze_bars(bars, indicator_params=enabled_params)
    disabled_w5 = [
        candidate for candidate in disabled["candidates"] if candidate["name"] == "w5_exhaustion"
    ]
    enabled_w5 = [
        candidate for candidate in enabled["candidates"] if candidate["name"] == "w5_exhaustion"
    ]

    assert disabled_w5 == []
    assert len(enabled_w5) == 1
    assert enabled_w5[0]["trigger_event"] == {"code": "w5_test"}
    assert detector_calls == ["w5"]
    clear_analysis_context_cache()


def test_analysis_context_cache_evicts_by_entry_limit(monkeypatch) -> None:
    clear_analysis_context_cache()
    monkeypatch.setattr(context_cache_module, "_CONTEXT_CACHE_MAX", 3)
    try:
        for index in range(5):
            bars = _bars()
            latest = bars[-1]
            bars[-1] = Bar(
                latest.symbol,
                latest.ts,
                latest.open,
                max(latest.high, latest.close + index),
                latest.low,
                latest.close + index,
                latest.volume,
                latest.timeframe,
            )
            get_cached_analysis_context(
                confirmed_bars=bars,
                live_signal_bars=bars,
                features_extra={"demo": index},
                defaults=DEFAULT_INDICATOR_SETTINGS,
                structure_bars=bars,
                structure_offset=0,
            )
        assert context_cache_module.analysis_context_cache_stats()["entries"] == 3
    finally:
        clear_analysis_context_cache()


def test_signal_quality_gate_blocks_low_rr_candidate() -> None:
    candidate = SignalCandidate(
        "demo",
        Direction.LONG,
        80,
        100,
        "demo",
        DomainFact("demo"),
        details={"plan_complete": True, "plan_coherent": True, "rr": 0.8},
        finality=CandidateFinality.CONFIRMED,
    )

    executable, annotated, gate = apply_global_min_rr_gate([candidate], 1.25)

    assert executable == []
    assert gate["blocked"]
    assert annotated[0].details["blocked_by_min_rr"] is True


def test_global_settings_schema_exposes_presets() -> None:
    schema = global_settings_schema()

    assert schema["version"] == 1
    assert any(field["key"] == "atr_len" for field in schema["fields"])
    assert "conservative" in schema["presets"]
