from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from aef_terminal.indicators.control_specs import IndicatorControlSpec, IndicatorSettingsScope
from aef_terminal.indicators.domain_facts import INDICATOR_FACT_FIELD_KEYS


IndicatorGroup = Literal["primary", "structure", "post_decision", "service"]
IndicatorPipelineStage = Literal["primary", "structure", "setup", "post_decision", "ui"]
IndicatorExecutionLane = Literal["primary", "fast", "micro"]
IndicatorFastTriggerRef = Literal["option_target_sample"]
IndicatorRendererKind = Literal["generic", "custom", "service"]
IndicatorScoreFamily = Literal[
    "trend", "volume", "structure", "reversal", "options", "forecast", "decision"
]
IndicatorModuleType = Literal["signal", "context", "visual", "ui-only"]
IndicatorOverlayLayer = Literal["background", "zones", "levels", "signals", "tables", "foreground"]
ConfirmedBarContextRole = Literal[
    "independent_scan",
    "lower_timeframe_confirmation",
]

_INDICATOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")


INDICATOR_FACT_RUNTIME_FIELDS = tuple(sorted(INDICATOR_FACT_FIELD_KEYS))


def validate_indicator_id(indicator_id: str) -> str:
    value = str(indicator_id or "").strip()
    if not _INDICATOR_ID_RE.fullmatch(value) or "__" in value:
        raise ValueError(
            "indicator id must be lower_snake_case with letters, numbers and single underscores"
        )
    return value


@dataclass(frozen=True)
class ConfirmedBarContextRequest:
    """Provider-confirmed closed bars required while an indicator is active."""

    timeframe: str
    history_bars: int
    role: ConfirmedBarContextRole


# This is the serialization allowlist. Per-renderer required fields remain owned by contracts.py.
OVERLAY_RUNTIME_FIELDS = (
    "type",
    "renderer_ref",
    "payload",
    "render_key",
    "retention",
    "layer",
    "id",
    "ts",
    "start_ts",
    "end_ts",
    "end_anchor_ts",
    "end_bar_offset",
    "price",
    "top",
    "bottom",
    "y1",
    "y2",
    "level",
    "lines",
    "side",
    "anchor",
    "anchor_price_mode",
    "bg",
    "border",
    "color",
    "accent",
    "text",
    "tone",
    "fill_mode",
    "label",
    "compact_label",
    "label_score",
    "tooltip",
    "style",
    "width",
    "opacity",
    "border_opacity",
    "pattern_opacity",
    "animated_border",
    "interactive",
    "label_handle",
    "label_position",
    "label_side",
    "label_font_size",
    "label_gap_px",
    "label_anchor",
    "label_style",
    "marker_shape",
    "marker_radius",
    "badge_lines",
    "badge_facts",
    "pattern",
    "pattern_color",
    "line_variant",
    "arrow_head",
    "arrow_size",
    "glyph",
    "glyph_kind",
    "glyph_only",
    "wall_direction",
    "no_tick",
    "tick",
    "pointer",
    "control_key",
    "control_keys",
    "deletable",
    "delete_icon",
    "table",
    "direction",
    "role",
    "zone_kind",
    "level_kind",
    "level_name",
    "geometry_segment",
    "pivot_number",
    "structure_phase",
    "source",
    "source_tf",
    "event",
    "event_code",
    "code",
    "action",
    "action_code",
    "action_reason_code",
    "action_card",
    "raw_action",
    "blocked_reason",
    "range_label",
    "overlay_group",
    "score",
    "rvol",
    "vol_rank",
    "vol_z",
    "move_atr",
    "range_atr",
    "spread_rel",
    "base_code",
    "fuel",
    "terminal_climax",
    "terminal_kind",
    "terminal_direction",
    "absorption_role",
    "pb_stage",
    "indian_count",
    "indian_stage",
    "indian_advice",
    "trend_star",
    "setup_summary",
    "candidate_reason",
    "structure_text_role",
    "overlay_role",
    "entry",
    "stop",
    "target",
    "signal",
    "signal_overlay",
    "setup_mode",
    "setup_type",
    "setup_code",
    "state_code",
    "trend_context",
    *INDICATOR_FACT_RUNTIME_FIELDS,
    "smc_state",
    "smc_lifecycle",
    "origin_end_ts",
    "fill_ts",
    "contract",
    "advisor_visible",
)

OVERLAY_COMPACT_EXCLUDED_FIELDS = ("trade_plan", "signal")
OVERLAY_COMPACT_FIELDS = tuple(
    field for field in OVERLAY_RUNTIME_FIELDS if field not in OVERLAY_COMPACT_EXCLUDED_FIELDS
)


RUNTIME_PAYLOAD_SCHEMA_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "overlay_contract": {
        "overlays": OVERLAY_RUNTIME_FIELDS,
    },
}

RUNTIME_PAYLOAD_COMPACT_SCHEMA_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "overlay_contract": {
        "overlays": OVERLAY_COMPACT_FIELDS,
    },
}

TICK_FLOW_CONTEXT_TOP_LEVEL_FIELDS = (
    "ok",
    "instrument_id",
    "symbol",
    "route_fingerprint",
    "start",
    "end",
    "bucket",
    "price_step",
    "delta",
    "profile",
    "aggregates",
    "stats",
    "error",
    "message",
)

TICK_FLOW_DELTA_FIELDS = (
    "ts",
    "total_volume",
    "net_delta",
    "trade_count",
    "cumulative_delta",
    "delta_ratio",
)

TICK_FLOW_PROFILE_FIELDS = (
    "price",
    "total_volume",
    "buy_volume",
    "sell_volume",
    "net_delta",
    "trade_count",
    "normalized",
    "delta_ratio",
)

TICK_FLOW_AGGREGATE_FIELDS = (
    "recent_delta",
    "value_area",
    "poc",
    "high_volume_prices",
    "imbalance_groups",
    "level_deltas",
)

TICK_FLOW_STATS_FIELDS = (
    "source",
    "total_volume",
    "profile_total_volume",
    "net_delta",
    "trade_count",
    "profile_levels",
    "profile_max_volume",
    "delta_ratio",
    "last_delta_ts",
    "aggregates_compute_ms",
    "live",
)

SERVICE_PAYLOAD_SCHEMA_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "tick_flow_context_v1": {
        "top_level": TICK_FLOW_CONTEXT_TOP_LEVEL_FIELDS,
        "delta": TICK_FLOW_DELTA_FIELDS,
        "profile": TICK_FLOW_PROFILE_FIELDS,
        "aggregates": TICK_FLOW_AGGREGATE_FIELDS,
        "stats": TICK_FLOW_STATS_FIELDS,
    },
}


def _runtime_payload_contract() -> dict[str, tuple[str, ...]]:
    return {
        "top_level": (
            "alerts",
            "availability",
            "boxes",
            "candidate_promotion",
            "candidates",
            "core_version",
            "fvg_zones",
            "hypotheses",
            "last_event",
            "levels",
            "lifecycle",
            "market_context",
            "order_blocks",
            "params",
            "paths",
            "pivots",
            "plan",
            "playbook_candidate_promotion",
            "playbook_setups",
            "profile",
            "preview",
            "preview_events",
            "rebound",
            "structure_mode",
            "table",
            "version",
            "warnings",
        ),
        "latest": (),
        "series": (),
        "events": (),
        "signals": (),
        "status": (
            "id",
            "label",
            "calculates",
            "state_code",
            "health",
            "reason_code",
            "trigger_event",
            "preview_active",
            "has_signal",
            "blocked_signal",
            "candidate_promotion",
            "mode",
            "bar_count",
            "analysis_ts",
            "input_first_ts",
            "input_last_ts",
            "calculated_at",
            "elapsed_ms",
            "event_count",
            "preview_event_count",
            "series_count",
            "recent_event_count",
            "recent_blocked_count",
            "last_event_ts",
            "latest_state",
            "params_hash",
            "last_error",
        ),
    }


def normalize_runtime_payload_contract(
    contract: dict[str, tuple[str, ...]] | None,
    schema_refs: tuple[str, ...] = (),
) -> dict[str, tuple[str, ...]]:
    merged = _runtime_payload_contract()
    for schema_ref in schema_refs:
        for key, values in RUNTIME_PAYLOAD_SCHEMA_FIELDS.get(schema_ref, {}).items():
            merged[key] = tuple(dict.fromkeys([*merged.get(key, ()), *values]))
    if not isinstance(contract, dict):
        return merged
    for key, values in contract.items():
        section = str(key)
        if not isinstance(values, tuple):
            values = tuple(str(item) for item in values) if isinstance(values, (list, set)) else ()
        merged[section] = tuple(dict.fromkeys([*merged.get(section, ()), *values]))
    return merged


def normalize_runtime_payload_compact_contract(
    contract: dict[str, tuple[str, ...]] | None = None,
    schema_refs: tuple[str, ...] = (),
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for schema_ref in schema_refs:
        for key, values in RUNTIME_PAYLOAD_COMPACT_SCHEMA_FIELDS.get(schema_ref, {}).items():
            merged[key] = tuple(dict.fromkeys([*merged.get(key, ()), *values]))
    if not isinstance(contract, dict):
        return merged
    for key, values in contract.items():
        section = str(key)
        if not isinstance(values, tuple):
            values = tuple(str(item) for item in values) if isinstance(values, (list, set)) else ()
        merged[section] = tuple(dict.fromkeys([*merged.get(section, ()), *values]))
    return merged


def normalize_service_payload_contract(
    contract: dict[str, tuple[str, ...]] | None,
    schema_refs: tuple[str, ...] = (),
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, tuple[str, ...]] = {}
    for schema_ref in schema_refs:
        for key, values in SERVICE_PAYLOAD_SCHEMA_FIELDS.get(schema_ref, {}).items():
            merged[key] = tuple(dict.fromkeys([*merged.get(key, ()), *values]))
    if not isinstance(contract, dict):
        return merged
    for key, values in contract.items():
        if not isinstance(values, tuple):
            values = tuple(str(item) for item in values) if isinstance(values, (list, set)) else ()
        merged[str(key)] = tuple(dict.fromkeys([*merged.get(str(key), ()), *values]))
    return merged


@dataclass(frozen=True)
class IndicatorSpec:
    id: str
    label: str
    calculates: str
    empty_result: dict[str, object]
    ui_key: str = ""
    calculate_ref: str = ""
    group: IndicatorGroup = "primary"
    pipeline_stage: IndicatorPipelineStage = "primary"
    pipeline_order: int = 500
    execution_lanes: tuple[IndicatorExecutionLane, ...] = ("primary",)
    fast_trigger_refs: tuple[IndicatorFastTriggerRef, ...] = ()
    analysis_refresh_interval_seconds: float | None = None
    signal_source: str = ""
    signal_name_resolver_ref: str = ""
    candidate_promoter: str = "indicator_state"
    candidate_score_floor_param: str = ""
    candidate_score_floor_ref: str = ""
    candidate_score_floor_min: float = 50.0
    min_target_atr: float = 1.0
    custom_renderer: bool = False
    renderer_kind: IndicatorRendererKind = "generic"
    renderer_ref: str = ""
    renderer_primitives: tuple[str, ...] = ("box", "line", "marker", "label", "table")
    renderer_placements: tuple[str, ...] = ("price",)
    custom_renderer_requirements: tuple[str, ...] = ()
    service_renderer_requirements: tuple[str, ...] = ()
    renderer_table_label: str = ""
    renderer_table_header_width: int = 14
    renderer_table_icon: str = ""
    renderer_table_animation_ref: str = ""
    paper_tradable: bool = True
    depends_on: tuple[str, ...] = ()
    required_dependencies: tuple[str, ...] = ()
    optional_context: tuple[str, ...] = ()
    shared_context_refs: tuple[str, ...] = ()
    confirmed_bar_context: tuple[ConfirmedBarContextRequest, ...] = ()
    input_contract: str = "bars+features"
    output_contract: str = "indicator-result-v1"
    status_contract: str = "indicator-status-v1"
    table_contract: str = ""
    overlay_contract: str = "overlay-contract-v1"
    overlay_layer: IndicatorOverlayLayer = "signals"
    overlay_filter_ref: str = "generic_overlay_filter"
    overlay_collect: bool = True
    signal_contract: str = "indicator-signal-v1"
    runtime_payload_contract: dict[str, tuple[str, ...]] = field(
        default_factory=_runtime_payload_contract
    )
    runtime_payload_schema_refs: tuple[str, ...] = ()
    runtime_payload_compact: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_payload_compact_contract: dict[str, tuple[str, ...]] = field(default_factory=dict)
    service_payload_contract: dict[str, tuple[str, ...]] = field(default_factory=dict)
    service_payload_schema_refs: tuple[str, ...] = ()
    score_family: IndicatorScoreFamily = "structure"
    empirical_power: float = 1.0
    usefulness: float = 1.0
    notes: str = ""
    module_type: IndicatorModuleType = "signal"
    settings_scope: IndicatorSettingsScope = "instrument"
    state_key: str = ""
    calc_key: str = ""
    visible_key: str = ""
    chart_control_id: str = ""
    process_control_id: str = ""
    process_effect_ref: str = ""
    derived_state_refs: tuple[str, ...] = ()
    runtime_state_ref: str = ""
    settings_status_ref: str = ""
    table_setting_id: str = ""
    api_enabled_key: str = ""
    api_visible_key: str = ""
    manager_order: int = 500
    runtime_order: int = 500
    default_calc: bool = True
    default_visible: bool = True
    show_in_manager: bool = True
    show_in_runtime: bool = True
    controls: tuple[IndicatorControlSpec, ...] = ()


@dataclass(frozen=True)
class IndicatorSidebarSpec:
    id: str
    title: str
    mount_id: str
    icon_svg: str = ""
    order: int = 500


@dataclass(frozen=True)
class IndicatorModule:
    spec: IndicatorSpec
    adapter_ref: str = ""
    params_builder_ref: str = ""
    candidate_promoter_ref: str = ""
    service_ref: str = ""
    snapshot_enricher_ref: str = ""
    router_ref: str = ""
    ui_js_assets: tuple[str, ...] = ()
    ui_css_assets: tuple[str, ...] = ()
    sidebar: IndicatorSidebarSpec | None = None

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass(frozen=True)
class IndicatorPipelineSpec:
    id: str
    group: IndicatorGroup
    pipeline_stage: IndicatorPipelineStage
    signal_source: str
    candidate_promoter: str
    min_target_atr: float
    candidate_promoter_ref: str = ""
    candidate_score_floor_param: str = ""
    candidate_score_floor_ref: str = ""
    candidate_score_floor_min: float = 50.0
    custom_renderer: bool = False
