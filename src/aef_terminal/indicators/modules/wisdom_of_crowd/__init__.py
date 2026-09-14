"""Startup-discovered Wisdom of Crowd trend-continuation prototype."""

from __future__ import annotations

from aef_terminal.features.market_series import MarketSeriesBlock
from aef_terminal.indicators.control_specs import _control, indicator_table_controls
from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    shared_market_series,
    unavailable_indicator_result,
)

from .calculation import wisdom_of_crowd
from .contracts import (
    COMPACT_ROW_FIELDS,
    ROW_FIELDS,
    WISDOM_OF_CROWD_VERSION,
    WisdomOfCrowdParams,
    build_params,
    resolve_signal_name,
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "wisdom_of_crowd",
        "Мудрость толпы",
        "trend alignment, controlled pullback and confirmed continuation reclaim",
        {
            "version": WISDOM_OF_CROWD_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
        },
        "wisdomOfCrowd",
        "aef_terminal.indicators.modules.wisdom_of_crowd:wisdom_of_crowd",
        group="primary",
        pipeline_stage="primary",
        pipeline_order=25,
        signal_source="wisdom_of_crowd",
        signal_name_resolver_ref=(
            "aef_terminal.indicators.modules.wisdom_of_crowd:resolve_signal_name"
        ),
        candidate_score_floor_param="watch_score",
        candidate_score_floor_min=50.0,
        min_target_atr=1.0,
        score_family="trend",
        empirical_power=0.50,
        usefulness=0.80,
        notes="Unvalidated prototype; keep Calc off until replay and forward-paper evidence exists.",
        module_type="signal",
        shared_context_refs=("market_series",),
        state_key="wisdomOfCrowd",
        calc_key="wisdomOfCrowdCalcEnabled",
        visible_key="wisdomOfCrowdVisible",
        chart_control_id="wisdom-of-crowd-toggle",
        process_control_id="wisdom-of-crowd-process",
        derived_state_refs=("table_position_state",),
        table_setting_id="wisdom-of-crowd-table-position",
        api_enabled_key="wisdom_of_crowd_enabled",
        api_visible_key="wisdom_of_crowd_visible",
        manager_order=25,
        runtime_order=25,
        default_calc=False,
        default_visible=True,
        table_contract="indicator-table-v1",
        renderer_kind="generic",
        renderer_ref="generic_overlay_renderer",
        renderer_primitives=("label", "table"),
        renderer_placements=("price", "table"),
        renderer_table_label="Мудрость толпы",
        overlay_layer="signals",
        overlay_filter_ref="generic_overlay_filter",
        runtime_payload_contract={
            "top_level": ("settings",),
            "latest": ROW_FIELDS,
            "series": ROW_FIELDS,
            "events": ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact_contract={
            "series": COMPACT_ROW_FIELDS,
            "events": COMPACT_ROW_FIELDS,
        },
        runtime_payload_compact={
            "series": {"limit": 240, "fields": "compact_contract"},
            "events": {"limit": 80, "fields": "compact_contract"},
            "overlays": {"limit": 81, "fields": "compact_contract"},
        },
        controls=(
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "wisdomOfCrowdLabels",
                element_id="wisdom-of-crowd-labels",
                compact=True,
            ),
            *indicator_table_controls("wisdomOfCrowd", "wisdom-of-crowd"),
            _control(
                "goScore",
                "GO",
                "number",
                78,
                "wisdomOfCrowdGoScore",
                element_id="wisdom-of-crowd-go-score",
                api_key="wisdom_of_crowd_go_score",
                param_key="go_score",
                minimum=60,
                maximum=95,
                step=1,
                action="load_apply",
            ),
            _control(
                "pullbackBars",
                "Pullback",
                "number",
                5,
                "wisdomOfCrowdPullbackBars",
                element_id="wisdom-of-crowd-pullback-bars",
                api_key="wisdom_of_crowd_pullback_bars",
                param_key="pullback_bars",
                minimum=2,
                maximum=12,
                step=1,
                action="load_apply",
            ),
            _control(
                "pullbackToleranceAtr",
                "PB ATR",
                "number",
                0.20,
                "wisdomOfCrowdPullbackToleranceAtr",
                element_id="wisdom-of-crowd-pullback-tolerance",
                api_key="wisdom_of_crowd_pullback_tolerance_atr",
                param_key="pullback_tolerance_atr",
                minimum=0.0,
                maximum=0.75,
                step=0.05,
                action="load_apply",
            ),
            _control(
                "maxExtensionAtr",
                "No chase",
                "number",
                0.90,
                "wisdomOfCrowdMaxExtensionAtr",
                element_id="wisdom-of-crowd-max-extension",
                api_key="wisdom_of_crowd_max_extension_atr",
                param_key="max_extension_atr",
                minimum=0.25,
                maximum=2.50,
                step=0.05,
                action="load_apply",
            ),
            _control(
                "rewardRisk",
                "R:R",
                "number",
                2,
                "wisdomOfCrowdRewardRisk",
                element_id="wisdom-of-crowd-reward-risk",
                api_key="wisdom_of_crowd_reward_risk",
                param_key="reward_risk",
                minimum=1.0,
                maximum=5.0,
                step=0.25,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.wisdom_of_crowd:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.wisdom_of_crowd:build_params",
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    params = ctx.params("wisdom_of_crowd")
    if not isinstance(params, WisdomOfCrowdParams):
        raise TypeError("wisdom_of_crowd runtime params must be WisdomOfCrowdParams")
    shared_block = shared_market_series(ctx)
    reusable_block = (
        shared_block
        if isinstance(shared_block, MarketSeriesBlock) and params.ema_slow_len == 50
        else None
    )

    def calculate() -> dict[str, object]:
        if ctx.vwap_session is None or not ctx.vwap_session.available:
            return unavailable_indicator_result(
                INDICATOR_MODULE.spec.empty_result,
                ctx.vwap_session,
            )
        return wisdom_of_crowd(
            ctx.confirmed_bars,
            params=params,
            vwap_session=ctx.vwap_session,
            feature_context=ctx.feature_context,
            market_series=reusable_block,
        )

    return IndicatorExecutionSpec(
        id="wisdom_of_crowd",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=calculate,
        params=params,
        runtime_params=ctx.runtime_params,
    )


__all__ = [
    "INDICATOR_MODULE",
    "WISDOM_OF_CROWD_VERSION",
    "WisdomOfCrowdParams",
    "build_execution_spec",
    "build_params",
    "resolve_signal_name",
    "wisdom_of_crowd",
]
