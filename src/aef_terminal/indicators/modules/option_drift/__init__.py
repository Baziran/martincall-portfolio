from __future__ import annotations

from typing import Any

from aef_terminal.indicators.control_specs import _control, indicator_table_controls
from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec


OPTION_DRIFT_VERSION = "0.7-theory-tooltips"


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "option_drift",
        "Option Drift",
        "advisory option-volume and delta-adjusted price drift from persisted GEX snapshots",
        {
            "version": OPTION_DRIFT_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
        },
        "optionDrift",
        "aef_terminal.indicators.modules.option_drift:option_drift_indicator",
        group="service",
        pipeline_stage="ui",
        pipeline_order=140,
        candidate_promoter="none",
        paper_tradable=False,
        renderer_kind="service",
        renderer_ref="option_drift_table",
        renderer_primitives=("table",),
        renderer_placements=("table",),
        service_renderer_requirements=(
            "service_router",
            "persisted_gex_snapshot_history",
            "current_live_gex_frame_snapshot",
            "server_heuristic_projection",
        ),
        renderer_table_label="Option Drift",
        renderer_table_header_width=24,
        input_contract="gex-option-snapshots-and-live-minute-frames-v1",
        output_contract="option-drift-advisory-v2",
        table_contract="indicator-table-v1",
        overlay_layer="tables",
        overlay_collect=False,
        score_family="options",
        empirical_power=0.35,
        usefulness=0.55,
        notes=(
            "Advisory only. This is a volume/price heuristic over persisted GEX snapshots, "
            "plus a bounded one-minute buffer while an existing live GEX session is active. "
            "It is not a trade-print feed or an input to candidates, decisions, paper trading, "
            "or execution. Removing the package removes the feature."
        ),
        module_type="ui-only",
        settings_scope="instrument",
        state_key="optionDrift",
        calc_key="optionDriftCalcEnabled",
        visible_key="optionDriftVisible",
        chart_control_id="option-drift-toggle",
        process_control_id="option-drift-process",
        process_effect_ref="option_drift_reload",
        derived_state_refs=("table_position_state",),
        runtime_state_ref="option_drift_state",
        table_setting_id="option-drift-table-position",
        api_enabled_key="option_drift_enabled",
        api_visible_key="option_drift_visible",
        manager_order=140,
        runtime_order=140,
        default_calc=False,
        default_visible=True,
        service_payload_contract={
            "top_level": (
                "ok",
                "version",
                "instrument_id",
                "route_fingerprint",
                "capture_mode",
                "source",
                "as_of",
                "revision",
                "state_code",
                "advisory_only",
                "decision_eligible",
                "metrics",
                "quality",
                "series",
            ),
            "metrics": (
                "call_drift",
                "put_drift",
                "balance",
                "call_gross_premium",
                "put_gross_premium",
                "gross_premium",
                "balance_ratio",
                "chop_score",
                "confidence",
                "price_efficiency",
                "two_sided_ratio",
                "covered_volume_ratio",
                "counter_volume_coverage",
                "crossings",
                "recent_cross",
            ),
            "quality": (
                "snapshots",
                "accepted_intervals",
                "rejected_intervals",
                "corrected_volume",
                "corrected_contract_observations",
                "latest_age_seconds",
                "market_data_entitlement",
                "heuristic_basis",
                "live_minute_active",
                "live_minute_samples",
                "target_interval_seconds",
            ),
            "series": (
                "ts",
                "spot",
                "call_drift",
                "put_drift",
                "balance",
            ),
        },
        controls=(
            _control(
                "captureMode",
                "History",
                "select",
                "live",
                "optionDriftCaptureMode",
                element_id="option-drift-capture-mode",
                options=("live", "request"),
                action="render",
                effect_ref="option_drift_capture_mode",
                compact=True,
                scope="instrument",
            ),
            *indicator_table_controls(
                "optionDrift",
                "option-drift",
                scope="instrument",
            ),
        ),
    ),
    service_ref="aef_terminal.indicators.modules.option_drift.service:configure_service",
    router_ref="aef_terminal.indicators.modules.option_drift.service:create_router",
    ui_js_assets=("client.js",),
)


def option_drift_indicator(
    *_args: Any,
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "version": OPTION_DRIFT_VERSION,
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
        "status": {
            "state_code": "service",
            "mode": "ui-only",
            "preview_active": True,
        },
    }
