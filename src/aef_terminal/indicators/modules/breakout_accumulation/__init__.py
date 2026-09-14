from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    unavailable_indicator_result,
)
from aef_terminal.indicators.control_specs import _control, indicator_table_controls
from aef_terminal.indicators.defaults import breakout_mode_thresholds
from aef_terminal.indicators.modules.breakout_accumulation.contracts import (
    BREAKOUT_ACCUMULATION_VERSION,
    BREAKOUT_OVERLAY_COMPACT_LIMIT,
    HIGH_EDGE_EVENT_CODES,
    LONG_EVENT_KEYS,
    LOW_EDGE_EVENT_CODES,
    MID_EVENT_CODES,
    SHORT_EVENT_KEYS,
    BreakoutAccumulationParams,
    build_params as build_params,
    price_on,
)
from aef_terminal.indicators.modules.breakout_accumulation.overlays import (
    append_breakout_item as _append_breakout_item,
    breakout_event_overlays as _breakout_event_overlays,
    breakout_level_overlays as _breakout_level_overlays,
)
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import (
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    previous_provider_session_levels,
    provider_session_bounds_for_bar,
    provider_session_closing_window,
)
from aef_terminal.features.volume import adaptive_rvol_series, resolve_bar_rvol
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime import overlays as pine_overlays
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional as safe_round
from aef_terminal.runtime.mtf import confirmed_intrabar_parent_preview
from aef_terminal.runtime.signal_state import SignalState, lifecycle_from_signals
from aef_terminal.signals.trade_plan import normalize_trade_plan


_COMPACT_ROW_FIELDS = (
    "action_code",
    "call_score",
    "call_watch",
    "code",
    "confirmed",
    "direction",
    "entry",
    "event",
    "event_code",
    "event_type",
    "lifecycle",
    "lower",
    "mid",
    "price",
    "put_score",
    "put_watch",
    "range_atr",
    "range_kind",
    "range_label",
    "range_start_ts",
    "raw_action",
    "render_label",
    "rvol",
    "rvol_source",
    "score",
    "setup_code",
    "state_code",
    "stop",
    "target",
    "target_mode",
    "trap_put",
    "trigger_type",
    "ts",
    "upper",
)

_ROW_FIELDS = (
    "action_code",
    "barcode_score",
    "call_score",
    "call_watch",
    "code",
    "coil_score",
    "confirmed",
    "direction",
    "entry",
    "entry_visible",
    "event",
    "event_code",
    "event_type",
    "flags",
    "important",
    "lifecycle",
    "lower",
    "lower_conf",
    "lower_source",
    "lower_pivot_hits",
    "lower_width",
    "mid",
    "noise_breakout_veto",
    "overlay_group",
    "plan_visible",
    "pointer",
    "price",
    "put_score",
    "put_watch",
    "range_atr",
    "range_kind",
    "range_label",
    "range_start_ts",
    "raw_action",
    "render_label",
    "rvol",
    "rvol_source",
    "score",
    "signal",
    "source",
    "state_code",
    "setup_code",
    "stop",
    "stop_visible",
    "symbol",
    "target",
    "target_conservative",
    "target_mode",
    "target_visible",
    "timeframe",
    "tick",
    "trap_put",
    "trigger_type",
    "ts",
    "upper",
    "upper_conf",
    "upper_source",
    "upper_pivot_hits",
    "upper_width",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)

INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "breakout_accumulation",
        "Breakout/Accumulation",
        "range, coil, traps, retests, continuation levels",
        {
            "version": BREAKOUT_ACCUMULATION_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "levels": [],
        },
        "breakoutAccumulation",
        "aef_terminal.indicators.modules.breakout_accumulation:breakout_accumulation",
        pipeline_order=10,
        signal_source="breakout_accumulation",
        candidate_score_floor_ref="strong_watch_floor",
        renderer_kind="generic",
        renderer_ref="generic_overlay_renderer",
        renderer_primitives=("box", "line", "label", "table"),
        renderer_placements=("price", "table"),
        renderer_table_label="BRK/ACC",
        overlay_layer="levels",
        overlay_filter_ref="breakout_accumulation",
        score_family="trend",
        empirical_power=1.10,
        usefulness=1.05,
        state_key="breakoutAccumulation",
        calc_key="barRadarCalcEnabled",
        visible_key="barRadarVisible",
        chart_control_id="bar-radar-toggle",
        process_control_id="bar-radar-process",
        table_setting_id="bar-radar-table-position",
        api_enabled_key="bar_enabled",
        manager_order=80,
        runtime_order=10,
        default_calc=False,
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe="1m",
                history_bars=64,
                role="lower_timeframe_confirmation",
            ),
        ),
        table_contract="indicator-table-v1",
        runtime_payload_contract={
            "latest": _ROW_FIELDS,
            "series": _ROW_FIELDS,
            "events": _ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"limit": 240, "fields": _COMPACT_ROW_FIELDS},
            "events": {"limit": 80, "fields": _COMPACT_ROW_FIELDS},
            "overlays": {"limit": BREAKOUT_OVERLAY_COMPACT_LIMIT, "fields": "compact_contract"},
        },
        controls=(
            _control(
                "bands",
                "Bands",
                "toggle",
                True,
                "barRadarBands",
                element_id="bar-radar-bands",
                compact=True,
            ),
            _control(
                "rangeBox",
                "Box",
                "toggle",
                True,
                "barRadarRangeBox",
                element_id="bar-radar-box",
                compact=True,
            ),
            _control(
                "levels",
                "Levels",
                "toggle",
                True,
                "barRadarLevels",
                element_id="bar-radar-levels",
                compact=True,
            ),
            _control(
                "rangeLevels",
                "Range",
                "toggle",
                False,
                "barRadarRangeLevels",
                element_id="bar-radar-range-levels",
            ),
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "barRadarLabels",
                element_id="bar-radar-labels",
                compact=True,
            ),
            _control(
                "rangeLabels",
                "Range labels",
                "toggle",
                False,
                "barRadarRangeLabels",
                element_id="bar-radar-range-labels",
            ),
            _control(
                "maxLabels",
                "Max labels",
                "number",
                16,
                "barRadarMaxLabels",
                element_id="bar-radar-max-labels",
                minimum=8,
                maximum=28,
                step=1,
            ),
            _control(
                "lineBars",
                "Line bars",
                "number",
                28,
                "barRadarLineBars",
                element_id="bar-radar-line-bars",
                minimum=5,
                maximum=120,
                step=1,
            ),
            _control(
                "lineWidth",
                "Line width",
                "number",
                2,
                "barRadarLineWidth",
                element_id="bar-radar-line-width",
                minimum=1,
                maximum=5,
                step=1,
            ),
            _control(
                "lineStyle",
                "Line style",
                "select",
                "dotted",
                "barRadarLineStyle",
                element_id="bar-radar-line-style",
                options=("solid", "dashed", "dotted"),
            ),
            _control(
                "tableBg",
                "Table bg",
                "color",
                "#1f2a36",
                "barRadarTableBg",
                element_id="bar-radar-table-bg",
            ),
            _control(
                "tableText",
                "Table text",
                "color",
                "#ffffff",
                "barRadarTableText",
                element_id="bar-radar-table-text",
            ),
            *indicator_table_controls("barRadar", "bar-radar"),
            _control(
                "coilLen",
                "Coil",
                "number",
                12,
                "barRadarCoilLen",
                element_id="bar-radar-coil-len",
                api_key="bar_coil_len",
                param_key="coil_len",
                minimum=6,
                maximum=80,
                step=1,
                action="load_apply",
            ),
            _control(
                "barcodeLen",
                "Barcode",
                "number",
                36,
                "barRadarBarcodeLen",
                element_id="bar-radar-barcode-len",
                api_key="bar_barcode_len",
                param_key="barcode_len",
                minimum=24,
                maximum=160,
                step=1,
                action="load_apply",
            ),
            _control(
                "operatorMaxBars",
                "Operator",
                "number",
                10,
                "barRadarOperatorMaxBars",
                element_id="bar-radar-operator-bars",
                api_key="bar_operator_max_bars",
                param_key="operator_max_bars",
                minimum=2,
                maximum=40,
                step=1,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.breakout_accumulation:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.breakout_accumulation:build_params",
    ui_js_assets=("client.js",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = breakout_accumulation
    params = ctx.params("breakout_accumulation")
    confirmed_micro_preview_bars = confirmed_intrabar_parent_preview(
        ctx.confirmed_bars,
        ctx.confirmed_bar_context.get("1m", ()),
        ctx.confirmed_bar_context_quality.get("1m"),
        "1m",
    )
    preview_bars = (
        confirmed_micro_preview_bars
        if confirmed_micro_preview_bars
        else ctx.live_signal_bars
        if ctx.live_preview_active
        else ()
    )
    return IndicatorExecutionSpec(
        id="breakout_accumulation",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=(
            (
                lambda: calculate(
                    ctx.confirmed_bars,
                    profile=ctx.instrument_profile,
                    params=params,
                    features=ctx.features,
                    vwap_session=ctx.vwap_session,
                )
            )
            if ctx.vwap_session is not None and ctx.vwap_session.available
            else (
                lambda: unavailable_indicator_result(
                    INDICATOR_MODULE.spec.empty_result,
                    ctx.vwap_session,
                )
            )
        ),
        preview_calculate=(
            (
                lambda: calculate(
                    preview_bars,
                    profile=ctx.instrument_profile,
                    params=params,
                    features=(None if confirmed_micro_preview_bars else ctx.features),
                    vwap_session=ctx.vwap_session,
                    preview_only=True,
                )
            )
            if preview_bars
            else None
        ),
        preview_event_ts=(preview_bars[-1].ts.isoformat() if preview_bars else ""),
        params=params,
        runtime_params=ctx.runtime_params,
    )


clamp = pine.clamp
ema_series = pine.ema_series
atr_series = pine.atr_rma_series
vwap_series = pine.vwap_series


def score_above(value: float, level: float, span: float, points: float) -> float:
    return clamp((value - level) / max(span, 0.0001), 0.0, 1.0) * points


def score_below(value: float, level: float, span: float, points: float) -> float:
    return clamp((level - value) / max(span, 0.0001), 0.0, 1.0) * points


def pivot_count_near(prices: Sequence[float], level: float | None, tolerance: float) -> int:
    if not price_on(level):
        return 0
    return sum(1 for price in prices if abs(price - level) <= tolerance)


def event_direction(event_key: str) -> str:
    if event_key in LONG_EVENT_KEYS:
        return "long"
    if event_key in SHORT_EVENT_KEYS:
        return "short"
    return "flat"


def signal_action_for(
    event_key: str,
    *,
    armed: bool,
    watching: bool,
    op_can_enter: bool,
    op_active: bool,
) -> tuple[str, str]:
    if event_key == "exit":
        return "WAIT", "EXIT"
    if op_can_enter or event_key in {
        "entry",
        "go_up",
        "go_down",
        "trap_call",
        "trap_put",
        "cont_call",
        "cont_put",
        "rot_call",
        "rot_put",
        "rb_call",
        "rb_put",
    }:
        return "GO", "GO"
    if op_active and event_key not in {"fail", "no_follow", "rot_stall"}:
        return "GO", "FOLLOW"
    if event_key in {"arm_up", "arm_down", "arm_both"} or armed:
        return "ARM", "ARM"
    if event_key or watching:
        return "WATCH", "WATCH"
    return "WAIT", "WAIT"


def signal_direction_for(
    *,
    event_key: str,
    candidate_scenario: int,
    cancel_direction: str,
    active_scenario: int,
    fail_up: bool,
    fail_down: bool,
    no_follow_call: bool,
    no_follow_put: bool,
    rot_stall_call: bool,
    rot_stall_put: bool,
) -> Direction:
    typed_direction = event_direction(event_key)
    if typed_direction == "long":
        return Direction.LONG
    if typed_direction == "short":
        return Direction.SHORT
    if event_key == "entry":
        return (
            Direction.LONG
            if candidate_scenario > 0
            else Direction.SHORT
            if candidate_scenario < 0
            else Direction.FLAT
        )
    if event_key == "exit":
        return Direction(cancel_direction)
    if event_key == "fail":
        return Direction.SHORT if fail_up else Direction.LONG if fail_down else Direction.FLAT
    if event_key == "no_follow":
        return (
            Direction.SHORT
            if no_follow_call
            else Direction.LONG
            if no_follow_put
            else Direction.FLAT
        )
    if event_key == "rot_stall":
        return (
            Direction.SHORT
            if rot_stall_call
            else Direction.LONG
            if rot_stall_put
            else Direction.FLAT
        )
    if active_scenario > 0:
        return Direction.LONG
    if active_scenario < 0:
        return Direction.SHORT
    return Direction.FLAT


def event_anchor(
    event_code: str,
    direction: Direction,
    bar: Bar,
    upper: float | None,
    lower: float | None,
    mid: float | None,
) -> float:
    if event_code == "far_stop_put" and price_on(upper):
        return max(upper, bar.high, bar.close)
    if event_code == "far_stop_call" and price_on(lower):
        return min(lower, bar.low, bar.close)
    if event_code in LOW_EDGE_EVENT_CODES and price_on(lower):
        return lower
    if event_code in HIGH_EDGE_EVENT_CODES and price_on(upper):
        return upper
    if (
        direction == Direction.LONG
        and event_code
        and event_code not in {"midline_fail", "break_fail", "no_follow"}
    ):
        return max(upper or bar.close, bar.close)
    if (
        direction == Direction.SHORT
        and event_code
        and event_code not in {"midline_fail", "break_fail", "no_follow"}
    ):
        return min(lower or bar.close, bar.close)
    if event_code in MID_EVENT_CODES and price_on(mid):
        return mid
    return bar.close


@dataclass(frozen=True, slots=True)
class _OperatorCandidateFacts:
    trap_call: bool
    trap_put: bool
    cont_call: bool
    cont_put: bool
    join_call: bool
    join_put: bool
    rot_call: bool
    rot_put: bool
    rb_call: bool
    rb_put: bool
    missed_trap_call: bool
    missed_trap_put: bool
    missed_ret_call: bool
    missed_ret_put: bool
    missed_join_call: bool
    missed_join_put: bool
    missed_rot_call: bool
    missed_rot_put: bool
    missed_rb_call: bool
    missed_rb_put: bool
    retry_call: bool
    retry_put: bool
    lower_level: float | None
    upper_level: float | None
    rev_call_stop: float | None
    rev_put_stop: float | None
    cont_call_entry: float | None
    cont_put_entry: float | None
    cont_call_stop: float | None
    cont_put_stop: float | None
    call_watch: float | None
    put_watch: float | None
    join_call_stop: float | None
    join_put_stop: float | None


@dataclass(frozen=True, slots=True)
class _OperatorCandidate:
    scenario: int = 0
    entry: float | None = None
    stop: float | None = None
    code: str = ""


def _operator_candidate_stage(facts: _OperatorCandidateFacts) -> _OperatorCandidate:
    candidates = (
        (
            facts.trap_call and not facts.missed_trap_call,
            _OperatorCandidate(2, facts.lower_level, facts.rev_call_stop, "trap_call"),
        ),
        (
            facts.trap_put and not facts.missed_trap_put,
            _OperatorCandidate(-2, facts.upper_level, facts.rev_put_stop, "trap_put"),
        ),
        (
            facts.cont_call and not facts.missed_ret_call,
            _OperatorCandidate(
                5,
                facts.cont_call_entry,
                facts.cont_call_stop,
                "second_call" if facts.retry_call else "retest_call",
            ),
        ),
        (
            facts.cont_put and not facts.missed_ret_put,
            _OperatorCandidate(
                -5,
                facts.cont_put_entry,
                facts.cont_put_stop,
                "second_put" if facts.retry_put else "retest_put",
            ),
        ),
        (
            facts.join_call and not facts.missed_join_call,
            _OperatorCandidate(1, facts.call_watch, facts.join_call_stop, "join_call"),
        ),
        (
            facts.join_put and not facts.missed_join_put,
            _OperatorCandidate(-1, facts.put_watch, facts.join_put_stop, "join_put"),
        ),
        (
            facts.rot_call and not facts.missed_rot_call,
            _OperatorCandidate(4, facts.lower_level, facts.rev_call_stop, "rotation_call"),
        ),
        (
            facts.rot_put and not facts.missed_rot_put,
            _OperatorCandidate(-4, facts.upper_level, facts.rev_put_stop, "rotation_put"),
        ),
        (
            facts.rb_call and not facts.missed_rb_call,
            _OperatorCandidate(3, facts.lower_level, facts.rev_call_stop, "rebound_call"),
        ),
        (
            facts.rb_put and not facts.missed_rb_put,
            _OperatorCandidate(-3, facts.upper_level, facts.rev_put_stop, "rebound_put"),
        ),
    )
    return next((candidate for active, candidate in candidates if active), _OperatorCandidate())


@dataclass(frozen=True, slots=True)
class _JoinStops:
    call: float | None
    put: float | None


def _join_stop_stage(
    *,
    upper_level: float | None,
    lower_level: float | None,
    bar_low: float,
    bar_high: float,
    safe_atr: float,
    join_stop_atr: float,
    rev_stop_buffer_atr: float,
) -> _JoinStops:
    stop_atr = max(float(join_stop_atr), float(rev_stop_buffer_atr))
    return _JoinStops(
        call=(
            min(
                float(upper_level) - safe_atr * stop_atr,
                bar_low - safe_atr * rev_stop_buffer_atr,
            )
            if price_on(upper_level)
            else None
        ),
        put=(
            max(
                float(lower_level) + safe_atr * stop_atr,
                bar_high + safe_atr * rev_stop_buffer_atr,
            )
            if price_on(lower_level)
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class _TargetProjection:
    price: float | None
    mode: str
    conservative: bool


def _target_projection_stage(
    *,
    scenario: int,
    range_ready: bool,
    squeeze_ratio: float,
    range_size: float | None,
    range_mid: float | None,
    upper_level: float | None,
    lower_level: float | None,
    close: float,
    safe_atr: float,
    params: BreakoutAccumulationParams,
) -> _TargetProjection:
    conservative = range_ready and squeeze_ratio < 0.55
    if not range_ready or not price_on(upper_level) or not price_on(lower_level):
        return _TargetProjection(None, "none", conservative)
    raw_move = max(
        (range_size or 0.0) * params.target_range_mult,
        safe_atr * params.target_min_atr,
    )
    target_move = (
        min(raw_move, safe_atr * max(params.target_min_atr, 1.15)) if conservative else raw_move
    )
    if scenario in {1, 5}:
        return _TargetProjection(
            float(upper_level) + target_move,
            "continuation_measured_move",
            conservative,
        )
    if scenario in {-1, -5}:
        return _TargetProjection(
            float(lower_level) - target_move,
            "continuation_measured_move",
            conservative,
        )
    if scenario in {2, 3, 4}:
        price = range_mid if price_on(range_mid) and close < float(range_mid) else upper_level
        mode = "range_rotation_midline_then_edge" if scenario == 4 else "rebound_midline_then_edge"
        return _TargetProjection(price, mode, conservative)
    if scenario in {-2, -3, -4}:
        price = range_mid if price_on(range_mid) and close > float(range_mid) else lower_level
        mode = "range_rotation_midline_then_edge" if scenario == -4 else "rebound_midline_then_edge"
        return _TargetProjection(price, mode, conservative)
    return _TargetProjection(None, "none", conservative)


def _new_event_stage(
    flags: dict[str, bool],
    previous_flags: dict[str, bool],
    *,
    retry_call: bool,
    retry_put: bool,
    cancel_code: str | None,
    entry_code: str | None,
) -> tuple[str, str]:
    event_codes = (
        ("trap_call", "trap_call"),
        ("trap_put", "trap_put"),
        ("cont_call", "second_call" if retry_call else "retest_call"),
        ("cont_put", "second_put" if retry_put else "retest_put"),
        ("go_up", "go_call"),
        ("go_down", "go_put"),
        ("probe_up", "probe_call"),
        ("probe_down", "probe_put"),
        ("rot_call", "rotation_call"),
        ("rot_put", "rotation_put"),
        ("rb_call", "rebound_call"),
        ("rb_put", "rebound_put"),
        ("arm_up", "ready_call"),
        ("arm_down", "ready_put"),
        ("arm_both", "ready_both"),
        ("barcode_active", "wide_range"),
        ("barcode_forming", "forming_range"),
        ("coil", "coil"),
        ("wide_call", "far_stop_call"),
        ("wide_put", "far_stop_put"),
        ("rot_stall", "midline_fail"),
        ("fail", "break_fail"),
        ("no_follow", "no_follow"),
    )
    event_key, event_code = next(
        (
            (key, code)
            for key, code in event_codes
            if flags[key] and not previous_flags.get(key, False)
        ),
        ("", ""),
    )
    if cancel_code:
        return "exit", cancel_code
    if entry_code and not event_key:
        return "entry", entry_code
    return event_key, event_code


def _state_codes_stage(
    *,
    cancel_code: str | None,
    entry_code: str | None,
    active_code: str | None,
    event_code: str,
    armed_up: bool,
    armed_down: bool,
    barcode_watch: bool,
    barcode_mature: bool,
    coil_active: bool,
) -> tuple[str, str, str]:
    if cancel_code:
        return "exit", "exit_wait", cancel_code
    if entry_code:
        return "enter", "enter_now", entry_code
    if active_code:
        return "hold", "hold", active_code
    if event_code:
        return event_code, event_code, event_code
    if armed_up:
        return "ready_call", "call_level_ready", "ready_call"
    if armed_down:
        return "ready_put", "put_level_ready", "ready_put"
    if barcode_watch:
        state = "wide_range" if barcode_mature else "forming_range"
        return state, "edge_only", state
    if coil_active:
        return "watch", "watch_edges", "coil"
    return "wait", "wait", ""


@dataclass(frozen=True, slots=True)
class _BreakTrackingState:
    direction: int
    level: float | None
    index: int | None
    accepted: bool
    mfe_max_atr: float
    fail_direction: int
    fail_level: float | None
    fail_index: int | None


def _advance_break_tracking(
    state: _BreakTrackingState,
    *,
    index: int,
    break_mfe_atr: float,
    accepted: bool,
    failed: bool,
    fail_direction: int,
    go_up: bool,
    go_down: bool,
    call_watch: float | None,
    put_watch: float | None,
) -> _BreakTrackingState:
    next_state = _BreakTrackingState(
        state.direction,
        state.level,
        state.index,
        state.accepted or accepted,
        break_mfe_atr if state.direction else state.mfe_max_atr,
        state.fail_direction,
        state.fail_level,
        state.fail_index,
    )
    if failed:
        next_state = _BreakTrackingState(
            0,
            state.level,
            state.index,
            False,
            0.0,
            fail_direction,
            state.level,
            index,
        )
    if go_up:
        next_state = _BreakTrackingState(
            1,
            call_watch,
            index,
            False,
            0.0,
            next_state.fail_direction,
            next_state.fail_level,
            next_state.fail_index,
        )
    if go_down:
        next_state = _BreakTrackingState(
            -1,
            put_watch,
            index,
            False,
            0.0,
            next_state.fail_direction,
            next_state.fail_level,
            next_state.fail_index,
        )
    return next_state


@dataclass(frozen=True, slots=True)
class _RotationTrackingState:
    direction: int
    index: int | None
    mid: float | None
    mid_reached: bool


def _advance_rotation_tracking(
    state: _RotationTrackingState,
    *,
    index: int,
    range_mid: float | None,
    rot_call: bool,
    rot_put: bool,
    bar: Bar,
) -> _RotationTrackingState:
    next_state = state
    if rot_call:
        next_state = _RotationTrackingState(1, index, range_mid, False)
    if rot_put:
        next_state = _RotationTrackingState(-1, index, range_mid, False)
    reached = next_state.mid_reached
    if next_state.direction == 1 and price_on(next_state.mid) and bar.high >= next_state.mid:
        reached = True
    if next_state.direction == -1 and price_on(next_state.mid) and bar.low <= next_state.mid:
        reached = True
    return _RotationTrackingState(
        next_state.direction,
        next_state.index,
        next_state.mid,
        reached,
    )


@dataclass(frozen=True, slots=True)
class _OperatorRuntimeState:
    scenario: int
    entry: float | None
    stop: float | None
    start_index: int | None
    name: str


def _advance_operator_state(
    state: _OperatorRuntimeState,
    candidate: _OperatorCandidate,
    *,
    index: int,
    stop_hit: bool,
    expired: bool,
    opposite: bool,
) -> tuple[_OperatorRuntimeState, bool, bool, str]:
    cancel_now = state.scenario != 0 and (stop_hit or expired or opposite)
    cancel_code = (
        "stop_hit" if stop_hit else "opposite" if opposite else "timeout" if expired else "cancel"
    )
    next_state = _OperatorRuntimeState(0, None, None, None, "") if cancel_now else state
    can_enter = candidate.scenario != 0 and next_state.scenario == 0
    if can_enter:
        next_state = _OperatorRuntimeState(
            candidate.scenario,
            candidate.entry,
            candidate.stop,
            index,
            candidate.code,
        )
    return next_state, cancel_now, can_enter, cancel_code


@dataclass(frozen=True, slots=True)
class _SignalPlanStage:
    action: ActionPhase
    trigger: float | None
    stop: float | None
    target: float | None
    blocked_reason: str


def _signal_plan_stage(
    *,
    action: ActionPhase,
    raw_action: str,
    direction: Direction,
    trigger: float | None,
    stop: float | None,
    target: float | None,
    call_watch: float | None,
    put_watch: float | None,
) -> _SignalPlanStage:
    next_action = action
    next_trigger = trigger
    next_stop = stop
    next_target = target
    blocked_reason = ""
    if next_action in {ActionPhase.ARM, ActionPhase.GO} and not price_on(next_trigger):
        if direction == Direction.LONG and price_on(call_watch):
            next_trigger = call_watch
        elif direction == Direction.SHORT and price_on(put_watch):
            next_trigger = put_watch
    if direction == Direction.FLAT:
        next_trigger = next_stop = next_target = None
    elif price_on(next_trigger) and price_on(next_stop) and price_on(next_target):
        plan = normalize_trade_plan(direction, next_trigger, next_stop, next_target)
        if plan.get("coherent"):
            next_trigger = plan.get("entry")
            next_stop = plan.get("stop")
            next_target = plan.get("target")
        else:
            blocked_reason = str(plan.get("blocked_reason") or "incoherent_trade_plan")
            if next_action == ActionPhase.GO:
                next_action = ActionPhase.WATCH
            next_target = None
    if raw_action == "FOLLOW" and next_action == ActionPhase.GO:
        next_action = ActionPhase.WAIT
    return _SignalPlanStage(
        next_action,
        next_trigger,
        next_stop,
        next_target,
        blocked_reason,
    )


@dataclass(frozen=True, slots=True)
class _SessionRangeStage:
    key: str
    opening_range_high: float | None
    opening_range_low: float | None
    initial_balance_high: float | None
    initial_balance_low: float | None
    in_opening_range: bool
    in_late_window: bool


def _session_range_stage(
    *,
    bar: Bar,
    session: ProviderSessionReset,
    current_key: str | None,
    opening_range_high: float | None,
    opening_range_low: float | None,
    initial_balance_high: float | None,
    initial_balance_low: float | None,
) -> _SessionRangeStage | None:
    key = session.key_for_bar(bar)
    if key != current_key:
        opening_range_high = opening_range_low = None
        initial_balance_high = initial_balance_low = None
    bounds = provider_session_bounds_for_bar(session, bar)
    if bounds is None:
        return None
    elapsed = bar.ts - bounds[0]
    in_opening_range = timedelta(0) <= elapsed < timedelta(minutes=30)
    in_initial_balance = timedelta(0) <= elapsed < timedelta(minutes=60)
    if in_opening_range:
        opening_range_high = (
            bar.high if opening_range_high is None else max(opening_range_high, bar.high)
        )
        opening_range_low = (
            bar.low if opening_range_low is None else min(opening_range_low, bar.low)
        )
    if in_initial_balance:
        initial_balance_high = (
            bar.high if initial_balance_high is None else max(initial_balance_high, bar.high)
        )
        initial_balance_low = (
            bar.low if initial_balance_low is None else min(initial_balance_low, bar.low)
        )
    return _SessionRangeStage(
        key,
        opening_range_high,
        opening_range_low,
        initial_balance_high,
        initial_balance_low,
        in_opening_range,
        provider_session_closing_window(session, bar.ts, minutes=30),
    )


def _update_pivot_history(
    *,
    index: int,
    bars: Sequence[Bar],
    highs: Sequence[float],
    lows: Sequence[float],
    params: BreakoutAccumulationParams,
    high_prices: list[float],
    high_times: list[datetime],
    low_prices: list[float],
    low_times: list[datetime],
) -> None:
    pivot_index = index - params.pivot_right_bars
    if pivot_index >= params.pivot_left_bars:
        window_start = pivot_index - params.pivot_left_bars
        window_end = min(len(bars), pivot_index + params.pivot_right_bars + 1)
        high_window = highs[window_start:window_end]
        low_window = lows[window_start:window_end]
        if high_window and highs[pivot_index] >= max(high_window):
            high_prices.append(highs[pivot_index])
            high_times.append(bars[pivot_index].ts)
        if low_window and lows[pivot_index] <= min(low_window):
            low_prices.append(lows[pivot_index])
            low_times.append(bars[pivot_index].ts)
    cutoff = bars[index].ts - timedelta(days=max(params.pivot_lookback_days, 1))
    while high_times and (high_times[0] < cutoff or len(high_times) > 160):
        high_times.pop(0)
        high_prices.pop(0)
    while low_times and (low_times[0] < cutoff or len(low_times) > 160):
        low_times.pop(0)
        low_prices.pop(0)


@dataclass(frozen=True, slots=True)
class _RangeLevelsStage:
    upper: float | None
    lower: float | None
    upper_source: str
    lower_source: str


def _range_levels_stage(
    *,
    raw_upper: float | None,
    raw_lower: float | None,
    opening_range_high: float | None,
    opening_range_low: float | None,
    initial_balance_high: float | None,
    initial_balance_low: float | None,
    previous_high: float | None,
    previous_low: float | None,
    tolerance: float,
    in_opening_range: bool,
) -> _RangeLevelsStage:
    upper, lower = raw_upper, raw_lower
    upper_source, lower_source = "range", "range"
    if (
        price_on(initial_balance_high)
        and price_on(upper)
        and abs(initial_balance_high - upper) <= tolerance
    ):
        upper, upper_source = initial_balance_high, "initial_balance_high"
    elif (
        price_on(opening_range_high)
        and price_on(upper)
        and not in_opening_range
        and abs(opening_range_high - upper) <= tolerance
    ):
        upper, upper_source = opening_range_high, "opening_range_high"
    elif price_on(previous_high) and price_on(upper) and abs(previous_high - upper) <= tolerance:
        upper, upper_source = previous_high, "previous_day_high"
    if (
        price_on(initial_balance_low)
        and price_on(lower)
        and abs(initial_balance_low - lower) <= tolerance
    ):
        lower, lower_source = initial_balance_low, "initial_balance_low"
    elif (
        price_on(opening_range_low)
        and price_on(lower)
        and not in_opening_range
        and abs(opening_range_low - lower) <= tolerance
    ):
        lower, lower_source = opening_range_low, "opening_range_low"
    elif price_on(previous_low) and price_on(lower) and abs(previous_low - lower) <= tolerance:
        lower, lower_source = previous_low, "previous_day_low"
    return _RangeLevelsStage(upper, lower, upper_source, lower_source)


@dataclass(frozen=True, slots=True)
class _BarcodeStatistics:
    crosses: int
    upper_touches: int
    lower_touches: int
    net_drift: float


def _barcode_statistics(
    *,
    index: int,
    length: int,
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    high: float | None,
    low: float | None,
    mid: float | None,
    size: float | None,
    ready: bool,
    safe_atr: float,
) -> _BarcodeStatistics:
    crosses = upper_touches = lower_touches = 0
    if ready and mid is not None and high is not None and low is not None:
        for position in range(max(1, index - length + 1), index + 1):
            if (closes[position] > mid >= closes[position - 1]) or (
                closes[position] < mid <= closes[position - 1]
            ):
                crosses += 1
            if highs[position] >= high - safe_atr * 0.35:
                upper_touches += 1
            if lows[position] <= low + safe_atr * 0.35:
                lower_touches += 1
    net_drift = (
        abs(closes[index] - closes[max(index - length, 0)]) / size
        if size is not None and size > 0
        else 1.0
    )
    return _BarcodeStatistics(crosses, upper_touches, lower_touches, net_drift)


def breakout_accumulation(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    params: BreakoutAccumulationParams | None = None,
    features: dict[str, Any] | None = None,
    vwap_session: ProviderSessionReset | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    params = params or BreakoutAccumulationParams()
    if not bars:
        return {
            "version": BREAKOUT_ACCUMULATION_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "levels": [],
        }
    if vwap_session is None or not vwap_session.available:
        return unavailable_indicator_result(INDICATOR_MODULE.spec.empty_result, vwap_session)

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    ranges = [max(bar.high - bar.low, 0.000001) for bar in bars]
    bodies = [abs(bar.close - bar.open) for bar in bars]
    atr_fast = atr_series(bars, 10)
    ema_fast = ema_series(closes, params.ema_fast_len)
    ema_slow = ema_series(closes, params.ema_slow_len)
    vwap = vwap_series(bars, reset=vwap_session.key_for_bar)
    try:
        prev_highs, prev_lows = previous_provider_session_levels(bars, vwap_session)
    except ValueError:
        return unavailable_indicator_result(
            INDICATOR_MODULE.spec.empty_result,
            ProviderSessionReset(
                available=False,
                source=vwap_session.source,
                reason_code="provider_session_levels_unavailable",
                calendar=vwap_session.calendar,
            ),
        )
    profile_obj = profile
    profile = profile_obj.label
    coil_len = max(params.coil_len, profile_obj.bar_coil_min)
    barcode_len = max(params.barcode_len, profile_obj.bar_barcode_min)
    coil_highs = pine.rolling_high_series(highs, coil_len)
    coil_lows = pine.rolling_low_series(lows, coil_len)
    barcode_highs = pine.rolling_high_series(highs, barcode_len)
    barcode_lows = pine.rolling_low_series(lows, barcode_len)
    close_stdev20 = pine.rolling_stdev_series(closes, 20)
    volume_avg = pine.rolling_mean_series(volumes, params.vol_len)
    volume_short_avg = pine.rolling_mean_series(volumes, 8)
    adaptive_rvol = adaptive_rvol_series(bars, params.vol_len)
    body_avg = pine.rolling_mean_series(bodies, 8)
    profile_range_max = profile_obj.bar_range_max_atr
    profile_squeeze_max = profile_obj.bar_squeeze_max
    arm_score, go_score, rev_score_need = breakout_mode_thresholds(params.trade_mode)
    active_late_rvol = profile_obj.bar_late_rvol
    active_confirm_atr = profile_obj.breakout_confirm_atr(params.confirm_atr)
    level_near_atr = profile_obj.bar_level_near_atr

    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    signal_states: list[SignalState] = []
    or_high: float | None = None
    or_low: float | None = None
    ib_high: float | None = None
    ib_low: float | None = None
    current_day: str | None = None
    last_break_dir = 0
    last_break_level: float | None = None
    last_break_index: int | None = None
    break_accepted_state = False
    break_mfe_max_atr = 0.0
    last_fail_dir = 0
    last_fail_level: float | None = None
    last_fail_index: int | None = None
    last_rot_dir = 0
    last_rot_index: int | None = None
    last_rot_mid: float | None = None
    rot_mid_reached = False
    op_scenario = 0
    op_entry: float | None = None
    op_stop: float | None = None
    op_start_index: int | None = None
    op_name = ""
    previous_flags: dict[str, bool] = {}
    upper_touch_history: list[bool] = []
    lower_touch_history: list[bool] = []
    pivot_high_prices: list[float] = []
    pivot_high_times: list[datetime] = []
    pivot_low_prices: list[float] = []
    pivot_low_times: list[datetime] = []

    for index, bar in enumerate(bars):
        session_stage = _session_range_stage(
            bar=bar,
            session=vwap_session,
            current_key=current_day,
            opening_range_high=or_high,
            opening_range_low=or_low,
            initial_balance_high=ib_high,
            initial_balance_low=ib_low,
        )
        if session_stage is None:
            return unavailable_indicator_result(
                INDICATOR_MODULE.spec.empty_result,
                ProviderSessionReset(
                    available=False,
                    source=vwap_session.source,
                    reason_code="provider_session_bounds_unavailable",
                    calendar=vwap_session.calendar,
                ),
            )
        current_day = session_stage.key
        or_high = session_stage.opening_range_high
        or_low = session_stage.opening_range_low
        ib_high = session_stage.initial_balance_high
        ib_low = session_stage.initial_balance_low
        in_or = session_stage.in_opening_range
        in_late = session_stage.in_late_window

        # Используем более доступный ATR (fast), если медленный еще не прогрелся (warmup)
        safe_atr = max(atr_fast[index] or (bar.high - bar.low), 0.000001)
        _update_pivot_history(
            index=index,
            bars=bars,
            highs=highs,
            lows=lows,
            params=params,
            high_prices=pivot_high_prices,
            high_times=pivot_high_times,
            low_prices=pivot_low_prices,
            low_times=pivot_low_times,
        )

        raw_range_high = coil_highs[index]
        raw_range_low = coil_lows[index]
        range_high = raw_range_high
        range_low = raw_range_low
        range_ready = price_on(range_high) and price_on(range_low) and range_high > range_low
        range_mid = (
            (range_high + range_low) * 0.5
            if (price_on(range_high) and price_on(range_low))
            else None
        )
        range_size = range_high - range_low if range_ready else None
        range_atr = range_size / safe_atr if range_size and range_size > 0 else 999.0
        ema_slope_atr = abs(ema_slow[index] - ema_slow[max(index - 5, 0)]) / safe_atr

        barcode_high = barcode_highs[index]
        barcode_low = barcode_lows[index]
        barcode_ready = (
            price_on(barcode_high) and price_on(barcode_low) and barcode_high > barcode_low
        )
        barcode_mid = (barcode_high + barcode_low) * 0.5 if barcode_ready else None
        barcode_size = barcode_high - barcode_low if barcode_ready else None
        barcode_range_atr = barcode_size / safe_atr if barcode_size and barcode_size > 0 else 999.0
        barcode_stats = _barcode_statistics(
            index=index,
            length=barcode_len,
            closes=closes,
            highs=highs,
            lows=lows,
            high=barcode_high,
            low=barcode_low,
            mid=barcode_mid,
            size=barcode_size,
            ready=barcode_ready,
            safe_atr=safe_atr,
        )
        barcode_crosses = barcode_stats.crosses
        barcode_upper_touches = barcode_stats.upper_touches
        barcode_lower_touches = barcode_stats.lower_touches
        barcode_touch_balance = min(barcode_upper_touches, barcode_lower_touches)
        barcode_net_drift = barcode_stats.net_drift
        barcode_min_atr = profile_range_max * 1.18
        barcode_max_atr = profile_range_max * profile_obj.bar_barcode_max_mult
        barcode_need_cross = max(4.0, barcode_len * 0.10)
        barcode_score = (
            clamp(
                score_above(barcode_range_atr, barcode_min_atr, barcode_min_atr * 0.75, 22.0)
                + score_below(barcode_range_atr, barcode_max_atr, barcode_max_atr * 0.45, 14.0)
                + score_above(float(barcode_crosses), barcode_need_cross, barcode_need_cross, 24.0)
                + score_above(float(barcode_touch_balance), 2.0, 3.0, 22.0)
                + score_below(barcode_net_drift, 0.52, 0.42, 12.0)
                + score_below(ema_slope_atr, 0.72, 0.72, 6.0),
                0.0,
                100.0,
            )
            if barcode_ready
            else 0.0
        )
        barcode_mature = (
            barcode_ready
            and barcode_range_atr >= barcode_min_atr
            and barcode_range_atr <= barcode_max_atr
            and barcode_score
            >= (
                68.0
                if params.trade_mode == "Strict"
                else 62.0
                if params.trade_mode == "Balanced"
                else 56.0
            )
        )
        barcode_forming = (
            barcode_ready
            and not barcode_mature
            and barcode_range_atr >= barcode_min_atr * 0.82
            and barcode_range_atr <= barcode_max_atr
            and barcode_score
            >= (
                50.0
                if params.trade_mode == "Strict"
                else 45.0
                if params.trade_mode == "Balanced"
                else 40.0
            )
            and barcode_crosses >= 2
            and barcode_touch_balance >= 1
        )
        barcode_watch = barcode_mature or barcode_forming

        level_tol = safe_atr * level_near_atr
        level_stage = _range_levels_stage(
            raw_upper=range_high,
            raw_lower=range_low,
            opening_range_high=or_high,
            opening_range_low=or_low,
            initial_balance_high=ib_high,
            initial_balance_low=ib_low,
            previous_high=prev_highs[index],
            previous_low=prev_lows[index],
            tolerance=level_tol,
            in_opening_range=in_or,
        )
        upper_level = level_stage.upper
        lower_level = level_stage.lower
        upper_source = level_stage.upper_source
        lower_source = level_stage.lower_source

        upper_conf = (
            1
            + int(
                price_on(or_high)
                and price_on(upper_level)
                and abs(or_high - upper_level) <= level_tol
            )
            + int(
                price_on(ib_high)
                and price_on(upper_level)
                and abs(ib_high - upper_level) <= level_tol
            )
            + int(
                price_on(prev_highs[index])
                and price_on(upper_level)
                and abs(prev_highs[index] - upper_level) <= level_tol
            )
        )
        lower_conf = (
            1
            + int(
                price_on(or_low)
                and price_on(lower_level)
                and abs(or_low - lower_level) <= level_tol
            )
            + int(
                price_on(ib_low)
                and price_on(lower_level)
                and abs(ib_low - lower_level) <= level_tol
            )
            + int(
                price_on(prev_lows[index])
                and price_on(lower_level)
                and abs(prev_lows[index] - lower_level) <= level_tol
            )
        )
        pivot_tol = safe_atr * params.pivot_near_atr
        upper_pivot_hits = pivot_count_near(pivot_high_prices, upper_level, pivot_tol) + min(
            pivot_count_near(pivot_low_prices, upper_level, pivot_tol), 2
        )
        lower_pivot_hits = pivot_count_near(pivot_low_prices, lower_level, pivot_tol) + min(
            pivot_count_near(pivot_high_prices, lower_level, pivot_tol), 2
        )
        upper_power_width = min(5, 2 + upper_pivot_hits)
        lower_power_width = min(5, 2 + lower_pivot_hits)

        bb_width = close_stdev20[index] * 4.0
        kc_width = safe_atr * 3.0
        squeeze_ratio = bb_width / max(kc_width, 0.000001)
        vol_avg = volume_avg[index]
        has_vol = volumes[index] > 0 and vol_avg > 0
        is_latest = index == len(bars) - 1
        rvol, rvol_source = resolve_bar_rvol(
            index,
            adaptive_series=adaptive_rvol,
            features=features,
            is_latest=is_latest,
        )
        clean_rvol = rvol * 0.50 if profile_obj.etf_option and in_or else rvol
        vol_short = volume_short_avg[index]
        vol_long = volume_avg[index]
        vol_fade = has_vol and vol_short < vol_long and clean_rvol <= 1.20
        close_pos = (
            clamp((bar.close - range_low) / range_size, 0.0, 1.0)
            if range_ready and range_size and range_size > 0
            else 0.5
        )
        vwap_dist_atr = abs(bar.close - vwap[index]) / safe_atr
        body_atr = body_avg[index] / safe_atr
        higher_lows = (
            index >= 2 and bar.low > lows[index - 1] and lows[index - 1] >= lows[index - 2]
        )
        lower_highs = (
            index >= 2 and bar.high < highs[index - 1] and highs[index - 1] <= highs[index - 2]
        )
        top_pressure = close_pos >= 0.68 and bar.close >= ema_fast[index]
        bot_pressure = close_pos <= 0.32 and bar.close <= ema_fast[index]

        range_score = score_below(range_atr, profile_range_max, profile_range_max * 0.75, 26.0)
        squeeze_score = score_below(squeeze_ratio, profile_squeeze_max, 0.70, 20.0)
        flat_score = score_below(ema_slope_atr, 0.45, 0.45, 10.0)
        vwap_score = score_below(vwap_dist_atr, 1.10, 1.10, 7.0)
        body_score = score_below(body_atr, 0.58, 0.50, 10.0)
        vol_fade_score = 9.0 if vol_fade else 6.0 if has_vol and clean_rvol < 1.65 else 2.0
        edge_pressure_score = 7.0 if top_pressure or bot_pressure else 0.0
        coil_score = clamp(
            range_score
            + squeeze_score
            + flat_score
            + vwap_score
            + body_score
            + vol_fade_score
            + edge_pressure_score
            + (11.0 if range_atr <= profile_range_max else 0.0),
            0.0,
            100.0,
        )

        upper_dist_atr = (upper_level - bar.close) / safe_atr if price_on(upper_level) else 999.0
        lower_dist_atr = (bar.close - lower_level) / safe_atr if price_on(lower_level) else 999.0
        proximity_up = (
            score_below(abs(upper_dist_atr), params.pre_break_atr, params.pre_break_atr, 16.0)
            if -active_confirm_atr <= upper_dist_atr <= params.pre_break_atr
            else 0.0
        )
        proximity_down = (
            score_below(abs(lower_dist_atr), params.pre_break_atr, params.pre_break_atr, 16.0)
            if -active_confirm_atr <= lower_dist_atr <= params.pre_break_atr
            else 0.0
        )
        last3_body = [
            abs(bars[j].close - bars[j].open) for j in range(max(0, index - 2), index + 1)
        ]
        last3_body_max_atr = max(last3_body) / safe_atr if last3_body else 0.0
        approach_not_chase = last3_body_max_atr <= profile_obj.bar_approach_body_atr and not (
            clean_rvol > active_late_rvol
            and ranges[index] > safe_atr * profile_obj.bar_impulse_range_atr
        )
        approach_up_ok = (
            approach_not_chase
            and upper_dist_atr <= params.pre_break_atr * 1.25
            and (higher_lows or bar.close >= ema_fast[index])
        )
        approach_down_ok = (
            approach_not_chase
            and lower_dist_atr <= params.pre_break_atr * 1.25
            and (lower_highs or bar.close <= ema_fast[index])
        )
        approach_up_score = (
            7.0
            if approach_up_ok
            else -9.0
            if upper_dist_atr <= params.pre_break_atr and not approach_not_chase
            else 0.0
        )
        approach_down_score = (
            7.0
            if approach_down_ok
            else -9.0
            if lower_dist_atr <= params.pre_break_atr and not approach_not_chase
            else 0.0
        )
        pressure_up = (
            close_pos * 22.0
            + (10.0 if top_pressure else 0.0)
            + (8.0 if higher_lows else 0.0)
            + min(upper_conf * 4.0, 14.0)
        )
        pressure_down = (
            (1.0 - close_pos) * 22.0
            + (10.0 if bot_pressure else 0.0)
            + (8.0 if lower_highs else 0.0)
            + min(lower_conf * 4.0, 14.0)
        )
        energy = (
            14.0
            if has_vol and clean_rvol >= params.min_go_rvol and clean_rvol <= active_late_rvol
            else 5.0
            if has_vol and clean_rvol > active_late_rvol
            else score_above(clean_rvol, 0.55, 0.45, 10.0)
            if has_vol
            else 8.0
        )
        impulse_late_up = price_on(upper_level) and (
            bar.close > upper_level + safe_atr * params.late_move_atr
            or (
                bar.close > upper_level
                and clean_rvol > active_late_rvol
                and ranges[index] > safe_atr * 1.15
            )
        )
        impulse_late_down = price_on(lower_level) and (
            bar.close < lower_level - safe_atr * params.late_move_atr
            or (
                bar.close < lower_level
                and clean_rvol > active_late_rvol
                and ranges[index] > safe_atr * 1.15
            )
        )
        option_window_up = (
            16.0
            if not impulse_late_up and -active_confirm_atr <= upper_dist_atr <= params.pre_break_atr
            else 8.0
            if not impulse_late_up
            and params.pre_break_atr < upper_dist_atr <= params.pre_break_atr * 2.0
            else 0.0
        )
        option_window_down = (
            16.0
            if not impulse_late_down
            and -active_confirm_atr <= lower_dist_atr <= params.pre_break_atr
            else 8.0
            if not impulse_late_down
            and params.pre_break_atr < lower_dist_atr <= params.pre_break_atr * 2.0
            else 0.0
        )
        session_penalty = 8.0 if in_late else 0.0
        candle_body = max(abs(bar.close - bar.open), 0.000001)
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        es_wick_rejection_up = (
            profile_obj.es_touch_break
            and price_on(upper_level)
            and bar.high > upper_level
            and upper_wick > candle_body * 1.5
            and clean_rvol >= params.min_go_rvol
        )
        es_wick_rejection_down = (
            profile_obj.es_touch_break
            and price_on(lower_level)
            and bar.low < lower_level
            and lower_wick > candle_body * 1.5
            and clean_rvol >= params.min_go_rvol
        )
        barcode_setup_score = (
            min(barcode_score * 0.34, 26.0)
            if barcode_mature
            else min(barcode_score * 0.20, 14.0)
            if barcode_forming
            else 0.0
        )
        prob_up = clamp(
            coil_score * 0.38
            + pressure_up * 0.26
            + energy
            + 8.0
            + proximity_up
            + option_window_up
            + approach_up_score
            + barcode_setup_score
            - session_penalty
            - (25.0 if es_wick_rejection_up else 0.0),
            0.0,
            100.0,
        )
        prob_down = clamp(
            coil_score * 0.38
            + pressure_down * 0.26
            + energy
            + 8.0
            + proximity_down
            + option_window_down
            + approach_down_score
            + barcode_setup_score
            - session_penalty
            - (25.0 if es_wick_rejection_down else 0.0),
            0.0,
            100.0,
        )
        coil_active = (
            range_ready
            and coil_score
            >= (
                44.0
                if params.trade_mode == "Early"
                else 52.0
                if params.trade_mode == "Balanced"
                else 64.0
            )
            and range_atr <= profile_range_max * 1.35
        )
        setup_active = coil_active or barcode_watch
        arm_gap = (
            7.0
            if params.trade_mode == "Early"
            else 8.0
            if params.trade_mode == "Balanced"
            else 10.0
        )
        arm_up_raw = (
            setup_active
            and prob_up >= arm_score
            and -active_confirm_atr <= upper_dist_atr <= params.pre_break_atr
        )
        arm_down_raw = (
            setup_active
            and prob_down >= arm_score
            and -active_confirm_atr <= lower_dist_atr <= params.pre_break_atr
        )
        armed_both = (
            setup_active
            and (arm_up_raw or arm_down_raw)
            and prob_up >= arm_score - 3.0
            and prob_down >= arm_score - 3.0
            and abs(prob_up - prob_down) < arm_gap
        )
        armed_up = arm_up_raw and not armed_both and prob_up > prob_down + arm_gap
        armed_down = arm_down_raw and not armed_both and prob_down > prob_up + arm_gap
        # Отрисовываем "рельсы" всегда, когда есть уровень, не дожидаясь range_ready (Squeeze/Coil)
        call_watch = upper_level + safe_atr * active_confirm_atr if price_on(upper_level) else None
        put_watch = lower_level - safe_atr * active_confirm_atr if price_on(lower_level) else None
        noise_breakout_veto = has_vol and clean_rvol < 0.80
        go_up_close = (
            price_on(call_watch)
            and bar.close > call_watch
            and bar.close > bar.open
            and prob_up >= go_score
            and (not has_vol or clean_rvol >= params.min_go_rvol)
            and not noise_breakout_veto
        )
        go_down_close = (
            price_on(put_watch)
            and bar.close < put_watch
            and bar.close < bar.open
            and prob_down >= go_score
            and (not has_vol or clean_rvol >= params.min_go_rvol)
            and not noise_breakout_veto
        )
        go_up_touch = (
            profile_obj.es_touch_break
            and price_on(call_watch)
            and bar.high > call_watch
            and prob_up >= go_score
            and clean_rvol >= params.min_go_rvol
            and not es_wick_rejection_up
            and not noise_breakout_veto
        )
        go_down_touch = (
            profile_obj.es_touch_break
            and price_on(put_watch)
            and bar.low < put_watch
            and prob_down >= go_score
            and clean_rvol >= params.min_go_rvol
            and not es_wick_rejection_down
            and not noise_breakout_veto
        )
        raw_go_up = go_up_touch or go_up_close
        raw_go_down = go_down_touch or go_down_close
        bar_pos = clamp((bar.close - bar.low) / max(bar.high - bar.low, 0.000001), 0.0, 1.0)
        same_bar_reject_up = (
            raw_go_up
            and upper_wick >= candle_body * 1.25
            and price_on(upper_level)
            and bar.close <= upper_level + safe_atr * 0.02
        )
        same_bar_reject_down = (
            raw_go_down
            and lower_wick >= candle_body * 1.25
            and price_on(lower_level)
            and bar.close >= lower_level - safe_atr * 0.02
        )
        both_sweep = raw_go_up and raw_go_down
        sweep_allow_up = both_sweep and prob_up > prob_down + 12.0 and bar_pos >= 0.80
        sweep_allow_down = both_sweep and prob_down > prob_up + 12.0 and bar_pos <= 0.20
        go_up = raw_go_up and not same_bar_reject_up and (not both_sweep or sweep_allow_up)
        go_down = raw_go_down and not same_bar_reject_down and (not both_sweep or sweep_allow_down)
        go_up_probe = raw_go_up and not go_up
        go_down_probe = raw_go_down and not go_down
        # Pine allows this path unconditionally when micro-assist is disabled.
        # Linda/VSA already gates volume quality, so do not require RVOL twice here.
        join_call = go_up and approach_up_ok
        join_put = go_down and approach_down_ok

        prev_break_dir = last_break_dir
        prev_break_level = last_break_level
        prev_break_index = last_break_index
        break_age = index - prev_break_index if prev_break_index is not None else 999
        break_mfe_this_atr = (
            (bar.high - prev_break_level) / safe_atr
            if prev_break_dir == 1 and price_on(prev_break_level)
            else (prev_break_level - bar.low) / safe_atr
            if prev_break_dir == -1 and price_on(prev_break_level)
            else 0.0
        )
        break_mfe_atr = max(break_mfe_max_atr, break_mfe_this_atr) if prev_break_dir else 0.0
        accepted_call = (
            prev_break_dir == 1
            and 0 < break_age <= params.accept_bars
            and price_on(prev_break_level)
            and bar.close > prev_break_level
            and (bar.low >= prev_break_level - safe_atr * 0.10 or break_mfe_atr >= 0.35)
        )
        accepted_put = (
            prev_break_dir == -1
            and 0 < break_age <= params.accept_bars
            and price_on(prev_break_level)
            and bar.close < prev_break_level
            and (bar.high <= prev_break_level + safe_atr * 0.10 or break_mfe_atr >= 0.35)
        )
        break_accepted_now = break_accepted_state or accepted_call or accepted_put
        no_follow_call = (
            prev_break_dir == 1
            and not break_accepted_now
            and params.no_follow_bars <= break_age <= 8
            and price_on(prev_break_level)
            and bar.close > prev_break_level
            and bar.close < prev_break_level + safe_atr * 0.20
            and break_mfe_atr < 0.22
        )
        no_follow_put = (
            prev_break_dir == -1
            and not break_accepted_now
            and params.no_follow_bars <= break_age <= 8
            and price_on(prev_break_level)
            and bar.close < prev_break_level
            and bar.close > prev_break_level - safe_atr * 0.20
            and break_mfe_atr < 0.22
        )
        fail_up = (
            prev_break_dir == 1
            and prev_break_index is not None
            and index > prev_break_index
            and index - prev_break_index <= 8
            and price_on(prev_break_level)
            and bar.close < prev_break_level
        )
        fail_down = (
            prev_break_dir == -1
            and prev_break_index is not None
            and index > prev_break_index
            and index - prev_break_index <= 8
            and price_on(prev_break_level)
            and bar.close > prev_break_level
        )
        break_tracking = _advance_break_tracking(
            _BreakTrackingState(
                last_break_dir,
                last_break_level,
                last_break_index,
                break_accepted_state,
                break_mfe_max_atr,
                last_fail_dir,
                last_fail_level,
                last_fail_index,
            ),
            index=index,
            break_mfe_atr=break_mfe_atr,
            accepted=accepted_call or accepted_put,
            failed=fail_up or fail_down or no_follow_call or no_follow_put,
            fail_direction=1 if fail_up or no_follow_call else -1,
            go_up=go_up,
            go_down=go_down,
            call_watch=call_watch,
            put_watch=put_watch,
        )
        last_break_dir = break_tracking.direction
        last_break_level = break_tracking.level
        last_break_index = break_tracking.index
        break_accepted_state = break_tracking.accepted
        break_mfe_max_atr = break_tracking.mfe_max_atr
        last_fail_dir = break_tracking.fail_direction
        last_fail_level = break_tracking.fail_level
        last_fail_index = break_tracking.fail_index

        retest_tol = safe_atr * params.accept_retest_atr
        retest_call = (
            price_on(prev_break_level)
            and prev_break_dir == 1
            and break_accepted_now
            and 0 < break_age <= params.range_judge_bars + params.accept_bars + 2
            and bar.low <= prev_break_level + retest_tol
            and bar.close > prev_break_level
            and bar.close >= bar.open
            and not fail_up
            and not no_follow_call
        )
        retest_put = (
            price_on(prev_break_level)
            and prev_break_dir == -1
            and break_accepted_now
            and 0 < break_age <= params.range_judge_bars + params.accept_bars + 2
            and bar.high >= prev_break_level - retest_tol
            and bar.close < prev_break_level
            and bar.close <= bar.open
            and not fail_down
            and not no_follow_put
        )
        retest_call_stop = prev_break_level - retest_tol if price_on(prev_break_level) else None
        retest_put_stop = prev_break_level + retest_tol if price_on(prev_break_level) else None
        fail_age = index - last_fail_index if last_fail_index is not None else 999
        retry_call = (
            price_on(last_fail_level)
            and last_fail_dir == 1
            and 0 < fail_age <= params.range_judge_bars + params.no_follow_bars + 3
            and go_up
            and bar.close > last_fail_level
            and prob_up > prob_down + 6.0
            and not same_bar_reject_up
        )
        retry_put = (
            price_on(last_fail_level)
            and last_fail_dir == -1
            and 0 < fail_age <= params.range_judge_bars + params.no_follow_bars + 3
            and go_down
            and bar.close < last_fail_level
            and prob_down > prob_up + 6.0
            and not same_bar_reject_down
        )
        retry_call_stop = last_fail_level - retest_tol if price_on(last_fail_level) else None
        retry_put_stop = last_fail_level + retest_tol if price_on(last_fail_level) else None
        cont_call = retest_call or retry_call
        cont_put = retest_put or retry_put
        if retry_call or retry_put:
            last_fail_dir = 0

        upper_level_touch = (
            range_ready
            and price_on(upper_level)
            and bar.high >= upper_level - safe_atr * params.rebound_touch_atr
        )
        lower_level_touch = (
            range_ready
            and price_on(lower_level)
            and bar.low <= lower_level + safe_atr * params.rebound_touch_atr
        )
        upper_edge_reject = (
            upper_level_touch
            and price_on(upper_level)
            and bar.close < upper_level
            and upper_wick >= candle_body * params.rebound_wick_body
        )
        lower_edge_reject = (
            lower_level_touch
            and price_on(lower_level)
            and bar.close > lower_level
            and lower_wick >= candle_body * params.rebound_wick_body
        )
        upper_touches_recent = sum(upper_touch_history[-params.touch_memory_bars :])
        lower_touches_recent = sum(lower_touch_history[-params.touch_memory_bars :])
        upper_level_fresh = upper_touches_recent < 1
        lower_level_fresh = lower_touches_recent < 1
        upper_level_used = upper_touches_recent >= 3
        lower_level_used = lower_touches_recent >= 3
        upper_touch_score = 8.0 if upper_level_fresh else -14.0 if upper_level_used else 0.0
        lower_touch_score = 8.0 if lower_level_fresh else -14.0 if lower_level_used else 0.0
        upper_stretch_atr = max(
            (bar.high - vwap[index]) / safe_atr, (bar.high - ema_slow[index]) / safe_atr
        )
        lower_stretch_atr = max(
            (vwap[index] - bar.low) / safe_atr, (ema_slow[index] - bar.low) / safe_atr
        )
        compact_reject_body = candle_body <= ranges[index] * 0.42
        upper_absorption = upper_edge_reject and compact_reject_body and clean_rvol >= 1.15
        lower_absorption = lower_edge_reject and compact_reject_body and clean_rvol >= 1.15
        range_rotation_base = (
            range_ready
            and range_size
            and range_size > 0
            and range_atr >= profile_range_max * 0.70
            and range_atr <= profile_range_max * 2.85
        )
        range_rotation_call = bool(
            range_rotation_base and (barcode_watch or upper_touches_recent >= 1)
        )
        range_rotation_put = bool(
            range_rotation_base and (barcode_watch or lower_touches_recent >= 1)
        )
        upper_extreme_score = (
            score_above(upper_stretch_atr, params.extreme_stretch_atr, 1.15, 12.0)
            + upper_touch_score
            + (5.0 if close_pos >= 0.82 else 0.0)
            + (16.0 if upper_absorption else 0.0)
            if upper_edge_reject
            else 0.0
        )
        lower_extreme_score = (
            score_above(lower_stretch_atr, params.extreme_stretch_atr, 1.15, 12.0)
            + lower_touch_score
            + (5.0 if close_pos <= 0.18 else 0.0)
            + (16.0 if lower_absorption else 0.0)
            if lower_edge_reject
            else 0.0
        )
        reverse_vol_ok = not has_vol or clean_rvol >= max(0.70, params.min_go_rvol * 0.70)
        rev_stop_buffer = safe_atr * params.rev_stop_buffer_atr
        rev_call_stop = bar.low - rev_stop_buffer
        rev_put_stop = bar.high + rev_stop_buffer
        rev_call_risk_atr = abs(bar.close - rev_call_stop) / safe_atr
        rev_put_risk_atr = abs(rev_put_stop - bar.close) / safe_atr
        rev_call_stop_score = score_below(
            rev_call_risk_atr, params.max_rev_stop_atr, params.max_rev_stop_atr, 12.0
        )
        rev_put_stop_score = score_below(
            rev_put_risk_atr, params.max_rev_stop_atr, params.max_rev_stop_atr, 12.0
        )
        rev_call_risk_ok = rev_call_risk_atr <= params.max_rev_stop_atr
        rev_put_risk_ok = rev_put_risk_atr <= params.max_rev_stop_atr
        barcode_put_fade_penalty = (
            10.0
            if barcode_mature and not upper_absorption and not fail_up and not go_up_probe
            else 0.0
        )
        barcode_call_fade_penalty = (
            10.0
            if barcode_mature and not lower_absorption and not fail_down and not go_down_probe
            else 0.0
        )
        rev_put_score = clamp(
            coil_score * 0.22
            + (30.0 if fail_up or go_up_probe else 0.0)
            + (20.0 if upper_edge_reject else 0.0)
            + upper_extreme_score
            + rev_put_stop_score
            + (9.0 if bar.close < ema_fast[index] else 0.0)
            + (8.0 if range_ready and price_on(range_mid) and bar.close < range_mid else 0.0)
            + (10.0 if reverse_vol_ok else 0.0)
            - barcode_put_fade_penalty,
            0.0,
            100.0,
        )
        rev_call_score = clamp(
            coil_score * 0.22
            + (30.0 if fail_down or go_down_probe else 0.0)
            + (20.0 if lower_edge_reject else 0.0)
            + lower_extreme_score
            + rev_call_stop_score
            + (9.0 if bar.close > ema_fast[index] else 0.0)
            + (8.0 if range_ready and price_on(range_mid) and bar.close > range_mid else 0.0)
            + (10.0 if reverse_vol_ok else 0.0)
            - barcode_call_fade_penalty,
            0.0,
            100.0,
        )
        short_rebound_base = (
            not go_up
            and upper_edge_reject
            and reverse_vol_ok
            and rev_put_risk_ok
            and rev_put_score >= rev_score_need
        )
        long_rebound_base = (
            not go_down
            and lower_edge_reject
            and reverse_vol_ok
            and rev_call_risk_ok
            and rev_call_score >= rev_score_need
        )
        wide_put = (
            not go_up
            and upper_edge_reject
            and reverse_vol_ok
            and not rev_put_risk_ok
            and rev_put_score >= rev_score_need - 8.0
        )
        wide_call = (
            not go_down
            and lower_edge_reject
            and reverse_vol_ok
            and not rev_call_risk_ok
            and rev_call_score >= rev_score_need - 8.0
        )
        rev_put_raw = short_rebound_base and (fail_up or go_up_probe)
        rev_call_raw = long_rebound_base and (fail_down or go_down_probe)
        rot_put_raw = (
            range_rotation_put
            and not go_up
            and upper_edge_reject
            and reverse_vol_ok
            and rev_put_risk_ok
            and rev_put_score >= rev_score_need - 8.0
            and not fail_up
            and not go_up_probe
        )
        rot_call_raw = (
            range_rotation_call
            and not go_down
            and lower_edge_reject
            and reverse_vol_ok
            and rev_call_risk_ok
            and rev_call_score >= rev_score_need - 8.0
            and not fail_down
            and not go_down_probe
        )
        rb_put_raw = short_rebound_base and not fail_up and not rot_put_raw
        rb_call_raw = long_rebound_base and not fail_down and not rot_call_raw
        rev_conflict = (
            (rev_call_raw or rb_call_raw or rot_call_raw)
            and (rev_put_raw or rb_put_raw or rot_put_raw)
            and abs(rev_call_score - rev_put_score) < 8.0
        )
        rev_put = (
            rev_put_raw
            and not rev_conflict
            and (
                not (rev_call_raw or rb_call_raw or rot_call_raw)
                or rev_put_score > rev_call_score + 5.0
            )
        )
        rev_call = (
            rev_call_raw
            and not rev_conflict
            and (
                not (rev_put_raw or rb_put_raw or rot_put_raw)
                or rev_call_score > rev_put_score + 5.0
            )
        )
        rot_put = (
            rot_put_raw
            and not rev_conflict
            and not rev_put
            and (
                not (rev_call_raw or rb_call_raw or rot_call_raw)
                or rev_put_score > rev_call_score + 5.0
            )
        )
        rot_call = (
            rot_call_raw
            and not rev_conflict
            and not rev_call
            and (
                not (rev_put_raw or rb_put_raw or rot_put_raw)
                or rev_call_score > rev_put_score + 5.0
            )
        )
        rb_put = (
            rb_put_raw
            and not rev_conflict
            and not rev_put
            and not rot_put
            and (
                not (rev_call_raw or rb_call_raw or rot_call_raw)
                or rev_put_score > rev_call_score + 5.0
            )
        )
        rb_call = (
            rb_call_raw
            and not rev_conflict
            and not rev_call
            and not rot_call
            and (
                not (rev_put_raw or rb_put_raw or rot_put_raw)
                or rev_call_score > rev_put_score + 5.0
            )
        )
        trap_call = rev_call
        trap_put = rev_put
        rotation_tracking = _advance_rotation_tracking(
            _RotationTrackingState(
                last_rot_dir,
                last_rot_index,
                last_rot_mid,
                rot_mid_reached,
            ),
            index=index,
            range_mid=range_mid,
            rot_call=rot_call,
            rot_put=rot_put,
            bar=bar,
        )
        last_rot_dir = rotation_tracking.direction
        last_rot_index = rotation_tracking.index
        last_rot_mid = rotation_tracking.mid
        rot_mid_reached = rotation_tracking.mid_reached
        rot_age = index - last_rot_index if last_rot_index is not None else 999
        rot_stall_call = (
            last_rot_dir == 1
            and rot_age >= params.range_judge_bars
            and not rot_mid_reached
            and price_on(last_rot_mid)
            and bar.close < last_rot_mid
        )
        rot_stall_put = (
            last_rot_dir == -1
            and rot_age >= params.range_judge_bars
            and not rot_mid_reached
            and price_on(last_rot_mid)
            and bar.close > last_rot_mid
        )

        cont_call_entry = (
            prev_break_level if retest_call else last_fail_level if retry_call else None
        )
        cont_put_entry = prev_break_level if retest_put else last_fail_level if retry_put else None
        cont_call_stop = (
            retest_call_stop if retest_call else retry_call_stop if retry_call else None
        )
        cont_put_stop = retest_put_stop if retest_put else retry_put_stop if retry_put else None
        missed_join_call = (
            join_call
            and price_on(call_watch)
            and (bar.close - call_watch) / safe_atr > params.missed_atr
        )
        missed_join_put = (
            join_put
            and price_on(put_watch)
            and (put_watch - bar.close) / safe_atr > params.missed_atr
        )
        missed_ret_call = (
            cont_call
            and price_on(cont_call_entry)
            and (bar.close - cont_call_entry) / safe_atr > params.missed_atr
        )
        missed_ret_put = (
            cont_put
            and price_on(cont_put_entry)
            and (cont_put_entry - bar.close) / safe_atr > params.missed_atr
        )
        missed_trap_call = (
            trap_call
            and price_on(lower_level)
            and (bar.close - lower_level) / safe_atr > params.missed_atr
        )
        missed_trap_put = (
            trap_put
            and price_on(upper_level)
            and (upper_level - bar.close) / safe_atr > params.missed_atr
        )
        missed_rot_call = (
            rot_call
            and price_on(lower_level)
            and (bar.close - lower_level) / safe_atr > params.missed_atr
        )
        missed_rot_put = (
            rot_put
            and price_on(upper_level)
            and (upper_level - bar.close) / safe_atr > params.missed_atr
        )
        missed_rb_call = (
            rb_call
            and price_on(lower_level)
            and (bar.close - lower_level) / safe_atr > params.missed_atr
        )
        missed_rb_put = (
            rb_put
            and price_on(upper_level)
            and (upper_level - bar.close) / safe_atr > params.missed_atr
        )
        join_stops = _join_stop_stage(
            upper_level=upper_level,
            lower_level=lower_level,
            bar_low=bar.low,
            bar_high=bar.high,
            safe_atr=safe_atr,
            join_stop_atr=params.join_stop_atr,
            rev_stop_buffer_atr=params.rev_stop_buffer_atr,
        )
        candidate = _operator_candidate_stage(
            _OperatorCandidateFacts(
                trap_call=trap_call,
                trap_put=trap_put,
                cont_call=cont_call,
                cont_put=cont_put,
                join_call=join_call,
                join_put=join_put,
                rot_call=rot_call,
                rot_put=rot_put,
                rb_call=rb_call,
                rb_put=rb_put,
                missed_trap_call=missed_trap_call,
                missed_trap_put=missed_trap_put,
                missed_ret_call=missed_ret_call,
                missed_ret_put=missed_ret_put,
                missed_join_call=missed_join_call,
                missed_join_put=missed_join_put,
                missed_rot_call=missed_rot_call,
                missed_rot_put=missed_rot_put,
                missed_rb_call=missed_rb_call,
                missed_rb_put=missed_rb_put,
                retry_call=retry_call,
                retry_put=retry_put,
                lower_level=lower_level,
                upper_level=upper_level,
                rev_call_stop=rev_call_stop,
                rev_put_stop=rev_put_stop,
                cont_call_entry=cont_call_entry,
                cont_put_entry=cont_put_entry,
                cont_call_stop=cont_call_stop,
                cont_put_stop=cont_put_stop,
                call_watch=call_watch,
                put_watch=put_watch,
                join_call_stop=join_stops.call,
                join_put_stop=join_stops.put,
            )
        )
        op_candidate = candidate.scenario
        op_candidate_entry = candidate.entry
        op_candidate_stop = candidate.stop
        op_candidate_code = candidate.code

        op_long = op_scenario > 0
        op_short = op_scenario < 0
        op_cancel_direction = "long" if op_long else "short" if op_short else "flat"
        op_cancel_stop = op_stop
        op_stop_hit = (op_long and price_on(op_stop) and bar.low <= op_stop) or (
            op_short and price_on(op_stop) and bar.high >= op_stop
        )
        op_expired = (
            op_scenario != 0
            and op_start_index is not None
            and index - op_start_index > max(params.operator_max_bars, 2)
        )
        op_opposite = (
            op_scenario != 0
            and op_candidate != 0
            and ((op_scenario > 0 and op_candidate < 0) or (op_scenario < 0 and op_candidate > 0))
        )
        operator_state, op_cancel_now, op_can_enter, op_cancel_code = _advance_operator_state(
            _OperatorRuntimeState(
                op_scenario,
                op_entry,
                op_stop,
                op_start_index,
                op_name,
            ),
            candidate,
            index=index,
            stop_hit=op_stop_hit,
            expired=op_expired,
            opposite=op_opposite,
        )
        op_scenario = operator_state.scenario
        op_entry = operator_state.entry
        op_stop = operator_state.stop
        op_start_index = operator_state.start_index
        op_name = operator_state.name
        op_active = op_scenario != 0
        target_scenario = (
            op_scenario
            if op_active
            else op_candidate
            if op_can_enter
            else 5
            if cont_call
            else -5
            if cont_put
            else 1
            if armed_up or go_up
            else -1
            if armed_down or go_down
            else 2
            if trap_call
            else -2
            if trap_put
            else 4
            if rot_call
            else -4
            if rot_put
            else 3
            if rb_call
            else -3
            if rb_put
            else 0
        )
        target_projection = _target_projection_stage(
            scenario=target_scenario,
            range_ready=range_ready,
            squeeze_ratio=squeeze_ratio,
            range_size=range_size,
            range_mid=range_mid,
            upper_level=upper_level,
            lower_level=lower_level,
            close=bar.close,
            safe_atr=safe_atr,
            params=params,
        )
        op_target = target_projection.price
        target_mode = target_projection.mode
        target_conservative = target_projection.conservative

        flags = {
            "coil": coil_active,
            "barcode_forming": barcode_forming,
            "barcode_active": barcode_mature,
            "arm_both": armed_both,
            "arm_up": armed_up,
            "arm_down": armed_down,
            "go_up": go_up,
            "go_down": go_down,
            "probe_up": go_up_probe,
            "probe_down": go_down_probe,
            "trap_call": trap_call,
            "trap_put": trap_put,
            "cont_call": cont_call,
            "cont_put": cont_put,
            "rot_call": rot_call,
            "rot_put": rot_put,
            "rb_call": rb_call,
            "rb_put": rb_put,
            "wide_call": wide_call,
            "wide_put": wide_put,
            "rot_stall": rot_stall_call or rot_stall_put,
            "fail": fail_up or fail_down,
            "no_follow": no_follow_call or no_follow_put,
        }
        event_key, event_code = _new_event_stage(
            flags,
            previous_flags,
            retry_call=retry_call,
            retry_put=retry_put,
            cancel_code=op_cancel_code if op_cancel_now else None,
            entry_code=op_candidate_code if op_can_enter else None,
        )

        state_code, action_code, setup_code = _state_codes_stage(
            cancel_code=op_cancel_code if op_cancel_now else None,
            entry_code=op_candidate_code if op_can_enter else None,
            active_code=op_name if op_active else None,
            event_code=event_code,
            armed_up=armed_up,
            armed_down=armed_down,
            barcode_watch=barcode_watch,
            barcode_mature=barcode_mature,
            coil_active=coil_active,
        )

        score = max(prob_up, prob_down, rev_call_score, rev_put_score)
        signal_direction = signal_direction_for(
            event_key=event_key,
            candidate_scenario=op_candidate,
            cancel_direction=op_cancel_direction,
            active_scenario=op_scenario if op_active else 0,
            fail_up=fail_up,
            fail_down=fail_down,
            no_follow_call=no_follow_call,
            no_follow_put=no_follow_put,
            rot_stall_call=rot_stall_call,
            rot_stall_put=rot_stall_put,
        )
        signal_action, raw_signal_action = signal_action_for(
            event_key,
            armed=armed_up or armed_down or armed_both,
            watching=barcode_watch or coil_active,
            op_can_enter=op_can_enter,
            op_active=op_active,
        )
        event_price = (
            op_cancel_stop
            if event_key == "exit" and price_on(op_cancel_stop)
            else event_anchor(
                event_code,
                signal_direction,
                bar,
                upper_level,
                lower_level,
                range_mid,
            )
        )
        signal_score = (
            max(prob_up, rev_call_score)
            if signal_direction == Direction.LONG
            else max(prob_down, rev_put_score)
            if signal_direction == Direction.SHORT
            else score
        )
        active_entry = op_entry if op_active else op_candidate_entry
        active_stop = op_stop if op_active else op_candidate_stop
        signal_trigger = (
            active_entry
            if price_on(active_entry)
            else event_price
            if signal_direction != Direction.FLAT
            else None
        )
        plan_stage = _signal_plan_stage(
            action=ActionPhase(signal_action),
            raw_action=raw_signal_action,
            direction=signal_direction,
            trigger=signal_trigger,
            stop=active_stop,
            target=op_target,
            call_watch=call_watch,
            put_watch=put_watch,
        )
        signal_action = plan_stage.action
        signal_trigger = plan_stage.trigger
        active_stop = plan_stage.stop
        op_target = plan_stage.target
        signal_blocked_reason = plan_stage.blocked_reason
        signal_state = SignalState(
            source="breakout_accumulation",
            action=signal_action,
            raw_action=raw_signal_action,
            direction=signal_direction,
            score=signal_score,
            confirmed=bar.closed,
            source_tf=bar.timeframe,
            trigger=signal_trigger,
            stop=active_stop,
            target=op_target,
            invalidation=active_stop,
            reason=action_code,
            blocked_reason=signal_blocked_reason,
            code=event_code or state_code,
        )
        signal_states.append(signal_state)
        if preview_only and not is_latest:
            previous_flags = flags
            upper_touch_history.append(bool(upper_level_touch))
            lower_touch_history.append(bool(lower_level_touch))
            continue
        plan_visible = (
            signal_direction != Direction.FLAT
            and (
                signal_action in {ActionPhase.ARM, ActionPhase.GO} or raw_signal_action == "FOLLOW"
            )
            and (op_active or op_can_enter or price_on(active_entry))
        )
        target_visible = plan_visible and price_on(op_target) and target_scenario != 0
        stop_visible = plan_visible and price_on(active_stop)
        item = {
            "ts": bar.ts.isoformat(),
            "state_code": state_code,
            "action_code": action_code,
            "setup_code": setup_code,
            "raw_action": raw_signal_action,
            "event": event_key,
            "event_code": event_code,
            "direction": signal_direction.value,
            "score": round(score, 2),
            "call_score": round(prob_up, 2),
            "put_score": round(prob_down, 2),
            "coil_score": round(coil_score, 2),
            "barcode_score": round(barcode_score, 2),
            "range_atr": round(range_atr, 4),
            "rvol": round(clean_rvol, 4),
            "rvol_source": rvol_source,
            "noise_breakout_veto": bool(noise_breakout_veto),
            "target_conservative": bool(target_conservative),
            "upper": safe_round(upper_level),
            "lower": safe_round(lower_level),
            "mid": safe_round(range_mid),
            "call_watch": safe_round(call_watch),
            "put_watch": safe_round(put_watch),
            "target": safe_round(op_target),
            "target_visible": bool(target_visible),
            "target_mode": target_mode,
            "stop": safe_round(op_stop if op_active else op_candidate_stop),
            "stop_visible": bool(stop_visible),
            "entry": safe_round(op_entry if op_active else op_candidate_entry),
            "entry_visible": bool(plan_visible and price_on(active_entry)),
            "plan_visible": bool(plan_visible),
            "range_start_ts": bars[
                max(0, index - (barcode_len if barcode_watch else coil_len))
            ].ts.isoformat(),
            "range_kind": "barcode" if barcode_watch else "coil" if coil_active else "range",
            "upper_source": upper_source,
            "lower_source": lower_source,
            "upper_conf": upper_conf,
            "lower_conf": lower_conf,
            "upper_pivot_hits": upper_pivot_hits,
            "lower_pivot_hits": lower_pivot_hits,
            "upper_width": upper_power_width,
            "lower_width": lower_power_width,
            "confirmed": bool(bar.closed),
            **indicator_fact_payload(
                scenario="breakout_accumulation",
                setup=setup_code or None,
                trigger_event={"code": event_code or state_code},
                supporting={"code": "range_ready"} if range_ready else None,
                opposing={"code": "low_rvol_breakout_veto"} if noise_breakout_veto else None,
                quality={"code": "score"},
                metrics={
                    "state": state_code,
                    "action": action_code,
                    "event": event_code,
                    "direction": signal_direction.value,
                    "target_mode": target_mode if price_on(op_target) else "",
                    "range_atr": _metric_number(range_atr, suffix=" ATR"),
                    "score": _metric_number(score, digits=0),
                    "call": _metric_number(prob_up, digits=0),
                    "put": _metric_number(prob_down, digits=0),
                    "coil": _metric_number(coil_score, digits=0),
                    "barcode": _metric_number(barcode_score, digits=0),
                    "rvol": _metric_number(clean_rvol),
                    "stop": _metric_number(op_stop if op_active else op_candidate_stop, digits=4),
                    "entry": _metric_number(
                        op_candidate_entry if op_can_enter else op_entry, digits=4
                    ),
                    "target": _metric_number(op_target, digits=4),
                },
            ),
            "signal": signal_state.as_dict(),
        }
        _append_breakout_item(
            item=item,
            event_key=event_key,
            event_code=event_code,
            event_price=event_price,
            bar=bar,
            series=series,
            events=events,
        )
        previous_flags = flags
        upper_touch_history.append(bool(upper_level_touch))
        lower_touch_history.append(bool(lower_level_touch))

    latest = series[-1]
    lifecycle = lifecycle_from_signals(
        source="breakout_accumulation",
        bars=bars,
        signals=signal_states,
        atr_values=atr_fast,
        max_age_bars=max(params.operator_max_bars, 2),
    )
    latest["lifecycle"] = lifecycle.as_dict() if lifecycle else None
    overlay_items: list[dict[str, Any]] = []
    lifecycle_dict = lifecycle.as_dict() if lifecycle else None
    latest["lifecycle"] = lifecycle_dict
    if preview_only:
        return {
            "version": BREAKOUT_ACCUMULATION_VERSION,
            "profile": profile,
            "series": series,
            "events": events,
            "latest": latest,
            "levels": [],
            "overlays": [],
        }
    overlay_items.extend(_breakout_event_overlays(bars, events))
    levels, level_overlays = _breakout_level_overlays(latest, last_bar=bars[-1])
    overlay_items.extend(level_overlays)
    call_score = float(latest["call_score"])
    put_score = float(latest["put_score"])
    plan_visible = (
        latest.get("plan_visible") is not False
        and latest.get("entry_visible") is not False
        and price_on(latest.get("entry"))
    )
    table_item = pine_overlays.table(
        table_id="breakout_accumulation_decision",
        direction=str(latest.get("direction") or ""),
        tone=str(latest.get("direction") or "warning"),
        role="breakout_accumulation_decision_table",
        model_ref="breakout_accumulation",
        model={
            "state_code": latest.get("state_code") or "wait",
            "action_code": latest.get("action_code") or "wait",
            "setup_code": latest.get("setup_code") or "",
            "event_code": latest.get("event_code") or "",
            "direction": latest.get("direction") or "flat",
            "call_score": call_score,
            "put_score": put_score,
            "coil_score": latest.get("coil_score"),
            "range_atr": latest.get("range_atr"),
            "upper": latest.get("upper"),
            "lower": latest.get("lower"),
            "call_watch": latest.get("call_watch"),
            "put_watch": latest.get("put_watch"),
            "plan": {
                "visible": plan_visible,
                "entry": latest.get("entry") if plan_visible else None,
                "target": latest.get("target")
                if latest.get("target_visible") is not False
                else None,
                "stop": latest.get("stop") if latest.get("stop_visible") is not False else None,
            },
        },
        fact_fields=indicator_fact_payload(
            scenario="breakout_accumulation_decision",
            setup=latest.get("setup_code") or None,
            trigger_event={"code": latest.get("event_code") or latest.get("state_code") or "wait"},
            metrics={
                "event": latest.get("event_code") or "",
                "state": latest.get("state_code") or "wait",
                "call": _metric_number(call_score, digits=0),
                "put": _metric_number(put_score, digits=0),
                "range_low": _metric_number(latest.get("lower")),
                "range_high": _metric_number(latest.get("upper")),
            },
        ),
    )
    table_item["retention"] = "active"
    overlay_items.append(table_item)
    return {
        "version": BREAKOUT_ACCUMULATION_VERSION,
        "profile": profile,
        "series": [latest] if latest is not None else [],
        "events": events,
        "latest": latest,
        "levels": [level for level in levels if level["price"] is not None],
        "overlays": overlay_items,
    }
