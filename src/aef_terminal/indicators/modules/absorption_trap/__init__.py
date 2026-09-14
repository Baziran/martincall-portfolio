from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
)
from aef_terminal.indicators.control_specs import _control, indicator_table_controls
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    IndicatorDefaults,
    score_action,
)
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.runtime import overlays, pine
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import bool_param, round_optional as _round
from aef_terminal.runtime.mtf import confirmed_intrabar_parent_preview
from aef_terminal.runtime.signal_state import SignalState, lifecycle_from_signals
from aef_terminal.runtime.timeframes import parse_aware_utc_ts


ABSORPTION_TRAP_VERSION = "0.2-python-confirmed-1m-preview"
GO_SCORE = DEFAULT_INDICATOR_SETTINGS.score.go

_COMPACT_ROW_FIELDS = (
    "action",
    "attack_ts",
    "code",
    "der",
    "der_status",
    "direction",
    "event",
    "label",
    "level",
    "mode",
    "price",
    "rvol",
    "score",
    "state",
    "stop",
    "target",
    "trigger",
    "ts",
)

_ROW_FIELDS = (
    "action",
    "attack_index",
    "attack_max_bars",
    "attack_ts",
    "call_rate_per_minute",
    "call_volume_delta",
    "code",
    "der",
    "der_status",
    "direction",
    "distance_ema233_atr",
    "event",
    "label",
    "lifecycle",
    "level",
    "level_lookback",
    "min_pierce_atr",
    "min_score",
    "mode",
    "option_flow_bias",
    "option_flow_conflict",
    "option_flow_override",
    "option_rvol",
    "pressure_divergence",
    "pressure_ratio",
    "pressure_source",
    "price",
    "progress_atr",
    "put_rate_per_minute",
    "put_volume_delta",
    "repeat_tolerance_atr",
    "rvol",
    "score",
    "signal",
    "state",
    "stop",
    "target",
    "trend_slope_atr",
    "trigger",
    "ts",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "absorption_trap",
        "Absorption Trap",
        "repeated level attack, tick/proxy effort decay and reclaim trigger",
        {
            "version": ABSORPTION_TRAP_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "table": None,
            "overlays": [],
        },
        "absorptionTrap",
        "aef_terminal.indicators.modules.absorption_trap:absorption_trap",
        pipeline_order=80,
        signal_source="absorption_trap",
        candidate_score_floor_ref="structure_floor",
        optional_context=("tick_flow",),
        shared_context_refs=("option_flow",),
        score_family="reversal",
        empirical_power=1.15,
        usefulness=1.10,
        state_key="absorptionTrap",
        calc_key="absorptionTrapCalcEnabled",
        visible_key="absorptionTrapVisible",
        chart_control_id="absorption-trap-toggle",
        process_control_id="absorption-trap-process",
        derived_state_refs=("table_position_state",),
        table_setting_id="absorption-trap-table-position",
        api_enabled_key="absorption_enabled",
        manager_order=50,
        runtime_order=80,
        default_calc=False,
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe="1m",
                history_bars=64,
                role="lower_timeframe_confirmation",
            ),
        ),
        table_contract="indicator-table-v1",
        overlay_layer="signals",
        overlay_filter_ref="generic_overlay_filter",
        renderer_primitives=("line", "label", "table"),
        renderer_placements=("price", "table"),
        renderer_table_label="Absorption Trap",
        runtime_payload_contract={
            "latest": _ROW_FIELDS,
            "series": _ROW_FIELDS,
            "events": _ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"limit": 240, "fields": _COMPACT_ROW_FIELDS},
            "events": {"limit": 80, "fields": _COMPACT_ROW_FIELDS},
            "overlays": {"limit": 80, "fields": "compact_contract"},
        },
        controls=(
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "absorptionTrapLabels",
                element_id="absorption-trap-labels",
                compact=True,
            ),
            _control(
                "levels",
                "Levels",
                "toggle",
                True,
                "absorptionTrapLevels",
                element_id="absorption-trap-levels",
                compact=True,
            ),
            *indicator_table_controls("absorptionTrap", "absorption-trap"),
            _control(
                "mode",
                "Mode",
                "select",
                "Balanced",
                "absorptionTrapMode",
                element_id="absorption-trap-mode",
                api_key="absorption_mode",
                param_key="mode",
                options=("Balanced", "Conservative"),
                action="load",
            ),
            _control(
                "synergyMatrix",
                "Edge",
                "toggle",
                True,
                "tickFlowInstitutionalEdge",
                element_id="absorption-trap-synergy",
                api_key="absorption_synergy_matrix",
                param_key="synergy_matrix",
                action="load_apply",
            ),
            _control(
                "minScore",
                "Min",
                "number",
                66,
                "absorptionTrapMinScore",
                element_id="absorption-trap-min-score",
                api_key="absorption_min_score",
                param_key="min_score",
                minimum=50,
                maximum=90,
                step=1,
                action="load",
            ),
            _control(
                "levelLookback",
                "Lookback",
                "number",
                24,
                "absorptionTrapLevelLookback",
                element_id="absorption-trap-lookback",
                api_key="absorption_level_lookback",
                param_key="level_lookback",
                minimum=8,
                maximum=80,
                step=1,
                action="load",
            ),
            _control(
                "attackWindow",
                "Window",
                "number",
                8,
                "absorptionTrapAttackWindow",
                element_id="absorption-trap-window",
                api_key="absorption_attack_window",
                param_key="attack_max_bars",
                minimum=2,
                maximum=24,
                step=1,
                action="load",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.absorption_trap:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.absorption_trap:build_params",
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = absorption_trap
    params = ctx.params("absorption_trap")
    confirmed_micro_preview_bars = confirmed_intrabar_parent_preview(
        ctx.confirmed_bars,
        ctx.confirmed_bar_context.get("1m", ()),
        ctx.confirmed_bar_context_quality.get("1m"),
        "1m",
    )
    return IndicatorExecutionSpec(
        id="absorption_trap",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=lambda: calculate(
            ctx.confirmed_bars,
            profile=ctx.instrument_profile,
            params=params,
            option_flow=ctx.option_flow,
            tick_flow=ctx.tick_flow,
        ),
        preview_calculate=(
            (
                lambda: calculate(
                    confirmed_micro_preview_bars,
                    profile=ctx.instrument_profile,
                    params=params,
                    option_flow=ctx.option_flow,
                    tick_flow=ctx.tick_flow,
                    preview_only=True,
                )
            )
            if confirmed_micro_preview_bars
            else None
        ),
        preview_event_ts=(
            confirmed_micro_preview_bars[-1].ts.isoformat() if confirmed_micro_preview_bars else ""
        ),
        params=params,
        runtime_params=ctx.runtime_params,
    )


@dataclass(frozen=True)
class AbsorptionTrapParams:
    level_lookback: int = 24
    attack_max_bars: int = 8
    cooldown_bars: int = 4
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    rvol_len: int = 20
    min_pierce_atr: float = 0.05
    repeat_tolerance_atr: float = 0.35
    invalidation_atr: float = 0.55
    pressure_divergence_ratio: float = 0.86
    min_score: float = 66.0
    max_events: int = 36
    line_bars: int = 24
    mode: str = "Balanced"
    conservative_level_lookback: int = 48
    conservative_attack_max_bars: int = 12
    conservative_min_pierce_atr: float = 0.15
    conservative_min_score: float = 78.0
    conservative_repeat_tolerance_atr: float = 0.08
    min_reclaim_atr: float = 0.02
    structure_stop_bars: int = 8
    trend_slope_lookback: int = 12
    max_counter_trend_slope_atr: float = 0.35
    max_counter_trend_distance_atr: float = 2.40
    option_flow_min_rate: float = 1.0
    option_flow_min_rvol: float = 1.25
    option_flow_dominance: float = 1.25
    min_significant_delta: float = 50.0
    synergy_matrix: bool = True


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> AbsorptionTrapParams:
    return AbsorptionTrapParams(
        level_lookback=int(raw.get("level_lookback", AbsorptionTrapParams.level_lookback)),
        attack_max_bars=int(raw.get("attack_max_bars", AbsorptionTrapParams.attack_max_bars)),
        atr_len=defaults.atr_len,
        min_score=float(raw.get("min_score", AbsorptionTrapParams.min_score)),
        mode=str(raw.get("mode", AbsorptionTrapParams.mode)),
        option_flow_min_rate=float(
            raw.get(
                "option_flow_min_rate",
                AbsorptionTrapParams.option_flow_min_rate,
            )
        ),
        option_flow_min_rvol=float(
            raw.get(
                "option_flow_min_rvol",
                AbsorptionTrapParams.option_flow_min_rvol,
            )
        ),
        option_flow_dominance=float(
            raw.get(
                "option_flow_dominance",
                AbsorptionTrapParams.option_flow_dominance,
            )
        ),
        min_significant_delta=float(
            raw.get(
                "min_significant_delta",
                AbsorptionTrapParams.min_significant_delta,
            )
        ),
        synergy_matrix=bool_param(raw.get("synergy_matrix"), True),
    )


def _volume_pressure(bar: Bar, side: str, tick_pressure: dict[str, float] | None = None) -> float:
    if tick_pressure and float(tick_pressure.get("total_volume") or 0.0) > 0:
        if side == "high":
            return max(float(tick_pressure.get("buy_volume") or 0.0), 0.0)
        return max(float(tick_pressure.get("sell_volume") or 0.0), 0.0)
    anatomy = pine.bar_anatomy(bar)
    volume = bar.volume
    if side == "high":
        return volume * (0.20 + 0.80 * anatomy.close_pos)
    return volume * (0.20 + 0.80 * (1.0 - anatomy.close_pos))


def _parse_tick_ts(value: Any) -> datetime | None:
    return parse_aware_utc_ts(value)


def _bar_step(bars: Sequence[Bar]) -> timedelta:
    if len(bars) < 2:
        return timedelta(minutes=5)
    return max(bars[-1].ts - bars[-2].ts, timedelta(minutes=1))


def _tick_pressure_by_bar(
    bars: Sequence[Bar], tick_flow: dict[str, Any] | None
) -> list[dict[str, float] | None]:
    if not isinstance(tick_flow, dict) or tick_flow.get("decision_eligible") is not True:
        return [None for _ in bars]
    rows = tick_flow.get("delta") if isinstance(tick_flow, dict) else None
    if not isinstance(rows, list) or not bars:
        return [None for _ in bars]
    times = [bar.ts for bar in bars]
    step = _bar_step(bars)
    buckets: list[dict[str, float]] = [
        {"total_volume": 0.0, "net_delta": 0.0, "buy_volume": 0.0, "sell_volume": 0.0} for _ in bars
    ]
    index = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _parse_tick_ts(row.get("ts"))
        if ts is None or ts < times[0] or ts >= times[-1] + step:
            continue
        while index < len(times) - 1 and times[index + 1] <= ts:
            index += 1
        end = times[index + 1] if index < len(times) - 1 else times[index] + step
        if ts < times[index] or ts >= end:
            continue
        total = max(float(row.get("total_volume") or 0.0), 0.0)
        net = float(row.get("net_delta") or 0.0)
        buy = max((total + net) / 2.0, 0.0)
        sell = max((total - net) / 2.0, 0.0)
        buckets[index]["total_volume"] += total
        buckets[index]["net_delta"] += net
        buckets[index]["buy_volume"] += buy
        buckets[index]["sell_volume"] += sell
    return [bucket if bucket["total_volume"] > 0 else None for bucket in buckets]


def _calculate_delta_efficiency(
    bar: Bar, tick_pressure: dict[str, float] | None, min_delta: float
) -> dict[str, Any]:
    """Compute Delta Efficiency Ratio (DER) and detect anomalies."""
    if not tick_pressure:
        return {"der": 0.0, "der_status": "NORMAL"}

    net_delta = float(tick_pressure.get("net_delta") or 0.0)
    price_change = float(bar.close - bar.open)

    # Significant volume threshold to avoid noise
    if abs(net_delta) < min_delta:
        return {"der": 0.0, "der_status": "NORMAL"}

    # DER = (Close - Open) / Net Delta
    der = pine.safe_div(price_change, net_delta, 0.0)

    status = "NORMAL"
    # Anomaly 1 (Short Squeeze Risk): Price is rising but Net Delta is negative
    if price_change > 0 and net_delta < 0:
        status = "SHORT_SQUEEZE_RISK"
    # Anomaly 2 (Long Liquidation / Distribution): Price is dropping but Net Delta is positive
    elif price_change < 0 and net_delta > 0:
        status = "LONG_LIQUIDATION_RISK"

    return {"der": round(der, 4), "der_status": status}


def _rvol(volumes: Sequence[float], index: int, length: int) -> float:
    previous_index = max(index - 1, 0)
    baseline = pine.mean_at(volumes, previous_index, length)
    return pine.safe_div(volumes[index], max(baseline, 1.0), 1.0)


def _conservative_mode(params: AbsorptionTrapParams) -> bool:
    return str(params.mode or "").strip().lower() in {
        "conservative",
        "strict",
        "careful",
        "cautious",
        "осторожный",
    }


def _effective_level_lookback(params: AbsorptionTrapParams) -> int:
    value = int(params.level_lookback)
    if _conservative_mode(params):
        value = max(value, int(params.conservative_level_lookback))
    return value


def _effective_attack_max_bars(params: AbsorptionTrapParams) -> int:
    value = int(params.attack_max_bars)
    if _conservative_mode(params):
        value = max(value, int(params.conservative_attack_max_bars))
    return value


def _effective_min_pierce_atr(params: AbsorptionTrapParams) -> float:
    value = float(params.min_pierce_atr)
    if _conservative_mode(params):
        value = max(value, float(params.conservative_min_pierce_atr))
    return value


def _effective_min_score(params: AbsorptionTrapParams) -> float:
    value = float(params.min_score)
    if _conservative_mode(params):
        value = max(value, float(params.conservative_min_score))
    return value


def _wall_line_bars(params: AbsorptionTrapParams) -> int:
    return 6


def _effective_repeat_tolerance_atr(params: AbsorptionTrapParams, atr: float, tick: float) -> float:
    value = max(float(params.repeat_tolerance_atr), 0.02)
    if _conservative_mode(params):
        value = max(float(params.conservative_repeat_tolerance_atr), tick / max(atr, 0.000001))
    return value


def _state_signal(
    *,
    bar: Bar,
    action: str,
    raw_action: str,
    direction: Direction,
    score: float,
    trigger: float | None,
    stop: float | None,
    target: float | None,
    reason_code: str,
    code: str,
) -> dict[str, Any]:
    payload = SignalState(
        source="absorption_trap",
        action=ActionPhase(action),
        raw_action=raw_action,
        direction=direction,
        score=pine.clamp(score, 0.0, 99.0),
        confirmed=bar.closed,
        source_tf=bar.timeframe,
        trigger=trigger,
        stop=stop,
        target=target,
        invalidation=stop,
        reason=reason_code,
        code=code,
    ).as_dict()
    payload["reason_code"] = payload.pop("reason")
    return payload


def _score_retest(
    *,
    side: str,
    attack: dict[str, Any],
    bar: Bar,
    atr: float,
    rvol_value: float,
    pressure: float,
    repeat_tolerance_atr: float,
    params: AbsorptionTrapParams,
) -> dict[str, Any]:
    level = float(attack["level"])
    first_extreme = float(attack["extreme"])
    first_pressure = max(float(attack["pressure"]), 1.0)
    current_extreme = bar.high if side == "high" else bar.low
    first_extension = abs(first_extreme - level)
    current_extension = abs(current_extreme - level)
    progress_atr = abs(current_extension - first_extension) / max(atr, 0.000001)
    pressure_ratio = pressure / first_pressure
    pressure_divergence = pressure_ratio <= params.pressure_divergence_ratio
    anatomy = pine.bar_anatomy(bar)
    wick_share = anatomy.upper_share if side == "high" else anatomy.lower_share
    reclaimed = bar.close <= level if side == "high" else bar.close >= level
    reclaim_depth = (level - bar.close) if side == "high" else (bar.close - level)
    reclaim_depth_atr = reclaim_depth / max(atr, 0.000001)

    progress_score = pine.clamp(
        (repeat_tolerance_atr - progress_atr) / max(repeat_tolerance_atr, 0.000001),
        0.0,
        1.0,
    )
    reclaim_score = pine.clamp(reclaim_depth / max(atr * 0.22, 0.000001), 0.0, 1.0)
    effort_score = pine.clamp((rvol_value - 0.75) / 1.25, 0.0, 1.0)
    wick_score = pine.clamp((wick_share - 0.22) / 0.42, 0.0, 1.0)
    second_extreme_bonus = 1.0 if current_extension >= first_extension else 0.0

    score = (
        24.0
        + (16.0 if reclaimed else 0.0)
        + 14.0 * progress_score
        + (18.0 if pressure_divergence else 0.0)
        + 12.0 * effort_score
        + 10.0 * wick_score
        + 5.0 * second_extreme_bonus
        + 6.0 * reclaim_score
    )
    return {
        "score": pine.clamp(score, 0.0, 99.0),
        "pressure_ratio": pressure_ratio,
        "pressure_divergence": pressure_divergence,
        "progress_atr": progress_atr,
        "wick_share": wick_share,
        "reclaimed": reclaimed,
        "reclaim_depth_atr": reclaim_depth_atr,
        "repeat_tolerance_atr": repeat_tolerance_atr,
    }


def _event_payload(
    *,
    side: str,
    bars: Sequence[Bar],
    index: int,
    bar: Bar,
    attack: dict[str, Any],
    score_data: dict[str, Any],
    rvol_value: float,
    atr: float,
    params: AbsorptionTrapParams,
    tick: float,
    option_flow: dict[str, Any] | None = None,
    include_facts: bool = True,
) -> dict[str, Any]:
    flow_bias = _option_flow_override(side, option_flow, params)
    direction = Direction.SHORT if side == "high" else Direction.LONG
    level = float(attack["level"])
    trigger = float(bar.close)
    stop_window = bars[max(0, index - max(int(params.structure_stop_bars), 1) + 1) : index + 1]
    conservative = _conservative_mode(params)
    stop_basis_code = "atr_buffer"
    if direction == Direction.SHORT:
        structural_high = max(
            [float(attack["extreme"]), *(item.high for item in stop_window), bar.high]
        )
        stop = (
            structural_high + tick
            if conservative
            else max(float(attack["extreme"]), bar.high) + atr * 0.15
        )
        stop_basis_code = "cascade_high_1_tick" if conservative else stop_basis_code
        risk = max(stop - trigger, atr * 0.35)
        target = trigger - risk * 1.35
        code = "ABS_HIGH"
        label = "ABS↓"
        state = "FAILED_HIGH"
        reason_code = "repeated_high_absorbed_reclaim_below"
    else:
        structural_low = min(
            [float(attack["extreme"]), *(item.low for item in stop_window), bar.low]
        )
        stop = (
            structural_low - tick
            if conservative
            else min(float(attack["extreme"]), bar.low) - atr * 0.15
        )
        stop_basis_code = "cascade_low_1_tick" if conservative else stop_basis_code
        risk = max(trigger - stop, atr * 0.35)
        target = trigger + risk * 1.35
        code = "ABS_LOW"
        label = "ABS↑"
        state = "FAILED_LOW"
        reason_code = "repeated_low_absorbed_reclaim_above"
    score = min(float(score_data["score"]), 99.0)
    action = score_action(score)
    signal = _state_signal(
        bar=bar,
        action=action,
        raw_action=action,
        direction=direction,
        score=score,
        trigger=trigger,
        stop=stop,
        target=target,
        reason_code=reason_code,
        code=code,
    )
    pressure_source = str(score_data.get("pressure_source") or "candle_proxy")
    pressure_context_code = (
        "tick_delta_pressure" if pressure_source == "tick" else "candle_proxy_delta_pressure"
    )
    return {
        "ts": bar.ts.isoformat(),
        "state": state,
        "code": code,
        "event": label,
        "label": label,
        "direction": direction.value,
        "level": _round(level),
        "price": _round(trigger),
        "score": round(score, 2),
        "rvol": round(rvol_value, 3),
        "pressure_ratio": round(float(score_data["pressure_ratio"]), 4),
        "pressure_divergence": bool(score_data["pressure_divergence"]),
        "pressure_source": pressure_source,
        "option_flow_override": "",
        "option_flow_bias": flow_bias or "",
        "option_flow_conflict": bool(flow_bias),
        "call_volume_delta": _round(_option_flow_number(option_flow, "call_volume_delta")),
        "put_volume_delta": _round(_option_flow_number(option_flow, "put_volume_delta")),
        "call_rate_per_minute": _round(_option_flow_number(option_flow, "call_rate_per_minute")),
        "put_rate_per_minute": _round(_option_flow_number(option_flow, "put_rate_per_minute")),
        "option_rvol": _round(_option_flow_number(option_flow, "option_rvol")),
        "progress_atr": round(float(score_data["progress_atr"]), 4),
        "attack_ts": attack["ts"],
        "attack_index": attack["index"],
        "trigger": _round(trigger),
        "stop": _round(stop),
        "target": _round(target),
        **(
            indicator_fact_payload(
                scenario="absorption_trap_high" if side == "high" else "absorption_trap_low",
                trigger_event={"code": reason_code},
                supporting={"code": reason_code},
                risk={
                    "code": stop_basis_code,
                    "stop": _round(stop),
                    "tick": _round(tick),
                },
                quality={"code": "score"},
                context={"code": pressure_context_code},
                trade_plan={
                    "source": "absorption_trap",
                    "direction": direction.value,
                    "trigger": _round(trigger),
                    "entry": _round(trigger),
                    "stop": _round(stop),
                    "target": _round(target),
                    "invalidation": _round(stop),
                    "complete": True,
                    "coherent": (stop < trigger < target)
                    if direction == Direction.LONG
                    else (target < trigger < stop),
                    "actionable": True,
                },
                metrics={
                    "score": _metric_number(score, digits=0),
                    "direction": direction.value.upper(),
                    "option_flow_conflict": bool(flow_bias),
                    "level": _metric_number(level),
                    "rvol": _metric_number(rvol_value),
                    "pressure_ratio": _metric_number(score_data["pressure_ratio"], suffix="x"),
                    "progress_atr": _metric_number(score_data["progress_atr"], suffix=" ATR"),
                    "stop": _metric_number(stop),
                    "target": _metric_number(target),
                    "option_call_rate_per_minute": _metric_number(
                        _option_flow_number(option_flow, "call_rate_per_minute")
                    ),
                    "option_put_rate_per_minute": _metric_number(
                        _option_flow_number(option_flow, "put_rate_per_minute")
                    ),
                    "option_rvol": _metric_number(_option_flow_number(option_flow, "option_rvol")),
                },
            )
            if include_facts
            else {}
        ),
        "signal": signal,
    }


def _option_flow_number(option_flow: dict[str, Any] | None, key: str) -> float | None:
    if not isinstance(option_flow, dict):
        return None
    value = option_flow.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _option_flow_is_strong(
    primary_rate: float,
    secondary_rate: float,
    rvol: float | None,
    params: AbsorptionTrapParams,
) -> bool:
    if primary_rate < max(float(params.option_flow_min_rate), 0.0):
        return False
    if primary_rate < secondary_rate * max(float(params.option_flow_dominance), 1.0):
        return False
    return rvol is not None and rvol >= max(float(params.option_flow_min_rvol), 0.0)


def _option_flow_override(
    side: str,
    option_flow: dict[str, Any] | None,
    params: AbsorptionTrapParams,
) -> str | None:
    call_rate = _option_flow_number(option_flow, "call_rate_per_minute")
    put_rate = _option_flow_number(option_flow, "put_rate_per_minute")
    if call_rate is None or put_rate is None:
        return None
    call_rate = max(call_rate, 0.0)
    put_rate = max(put_rate, 0.0)
    rvol = _option_flow_number(option_flow, "option_rvol")
    if side == "high" and _option_flow_is_strong(call_rate, put_rate, rvol, params):
        return "bullish"
    if side == "low" and _option_flow_is_strong(put_rate, call_rate, rvol, params):
        return "bearish"
    return None


def _has_go_reclaim(score_data: dict[str, Any], params: AbsorptionTrapParams) -> bool:
    score = float(score_data["score"])
    required_score = max(_effective_min_score(params), GO_SCORE)
    if _conservative_mode(params) and float(score_data["reclaim_depth_atr"]) < float(
        params.min_reclaim_atr
    ):
        return False
    return bool(score_data["reclaimed"]) and score >= required_score


def _trend_context(
    *,
    bars: Sequence[Bar],
    ema233: Sequence[float],
    atr_values: Sequence[float],
    index: int,
    params: AbsorptionTrapParams,
) -> dict[str, Any]:
    bar = bars[index]
    atr = max(float(atr_values[index]), 0.000001)
    lookback = max(int(params.trend_slope_lookback), 2)
    start = max(0, index - lookback)
    ema_now = float(ema233[index])
    ema_then = float(ema233[start])
    slope_atr = (ema_now - ema_then) / atr
    distance_atr = (bar.close - ema_now) / atr
    strong_up = slope_atr >= float(params.max_counter_trend_slope_atr) or distance_atr >= float(
        params.max_counter_trend_distance_atr
    )
    strong_down = slope_atr <= -float(params.max_counter_trend_slope_atr) or distance_atr <= -float(
        params.max_counter_trend_distance_atr
    )
    return {
        "ema233": ema_now,
        "slope_atr": slope_atr,
        "distance_atr": distance_atr,
        "strong_up": strong_up,
        "strong_down": strong_down,
    }


def _rounding_structure_ok(
    *,
    side: str,
    bars: Sequence[Bar],
    index: int,
    attack: dict[str, Any],
    atr: float,
    tick: float,
) -> bool:
    bar = bars[index]
    prev = bars[index - 1] if index else bar
    first_extreme = float(attack["extreme"])
    if side == "high":
        lower_high = bar.high <= first_extreme - tick or (
            bar.high <= first_extreme + tick and bar.close < prev.close
        )
        return lower_high and bar.close <= float(attack["level"])
    higher_low = bar.low >= first_extreme + tick or (
        bar.low >= first_extreme - tick and bar.close > prev.close
    )
    return higher_low and bar.close >= float(attack["level"])


def _conservative_block_code(
    *,
    side: str,
    bars: Sequence[Bar],
    index: int,
    attack: dict[str, Any],
    atr: float,
    trend: dict[str, Any],
    params: AbsorptionTrapParams,
    tick: float,
) -> str | None:
    if not _conservative_mode(params):
        return None
    structure_ok = _rounding_structure_ok(
        side=side, bars=bars, index=index, attack=attack, atr=atr, tick=tick
    )
    if side == "high" and trend["strong_up"] and not structure_ok:
        return "strong_uptrend_without_lower_high"
    if side == "low" and trend["strong_down"] and not structure_ok:
        return "strong_downtrend_without_higher_low"
    return None


def _blocked_signal(
    item: dict[str, Any],
    bar: Bar,
    reason_code: str,
    *,
    include_facts: bool = True,
) -> dict[str, Any]:
    direction = (
        Direction.LONG
        if item.get("direction") == "long"
        else Direction.SHORT
        if item.get("direction") == "short"
        else Direction.FLAT
    )
    signal = _state_signal(
        bar=bar,
        action="BLOCK",
        raw_action="GO",
        direction=direction,
        score=float(item["score"]),
        trigger=None,
        stop=None,
        target=None,
        reason_code=reason_code,
        code="ABS_TRAP_BLOCKED",
    )
    signal["blocked"] = True
    signal["blocked_reason"] = reason_code
    return {
        **item,
        "state": "BLOCKED_TREND",
        "raw_action": "GO",
        "action": "BLOCK",
        "blocked": True,
        "blocked_reason": reason_code,
        **(
            indicator_fact_payload(
                scenario="absorption_trap",
                trigger_event={"code": "blocked_trend"},
                opposing={"code": reason_code},
                metrics=item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
            )
            if include_facts
            else {}
        ),
        "signal": signal,
    }


def _idle_item(bar: Bar) -> dict[str, Any]:
    return {
        "ts": bar.ts.isoformat(),
        "state": "IDLE",
        "direction": Direction.FLAT.value,
        "score": 0.0,
        "level": None,
        "signal": _state_signal(
            bar=bar,
            action="WAIT",
            raw_action="WAIT",
            direction=Direction.FLAT,
            score=0.0,
            trigger=None,
            stop=None,
            target=None,
            reason_code="waiting_for_level_attack",
            code="ABS_TRAP",
        ),
    }


def _table_for(latest: dict[str, Any]) -> dict[str, Any]:
    direction = (
        Direction.LONG
        if latest.get("direction") == "long"
        else Direction.SHORT
        if latest.get("direction") == "short"
        else Direction.FLAT
    )
    tone = (
        "positive"
        if direction == Direction.LONG
        else "negative"
        if direction == Direction.SHORT
        else "warning"
    )
    pressure = latest.get("pressure_ratio")
    mode = str(latest.get("mode") or "Balanced")
    effective_read = {
        "parts": (
            {"kind": "integer", "prefix": "L", "value": latest.get("level_lookback")},
            {"kind": "integer", "prefix": "W", "value": latest.get("attack_max_bars")},
            {"kind": "integer", "prefix": "S", "value": latest.get("min_score")},
        ),
    }
    plan_entry = {
        "kind": "price",
        "prefix": "E ",
        "value": latest.get("trigger"),
        "digits": 2,
        "empty": "-",
    }
    plan_exit = {
        "parts": (
            {
                "kind": "price",
                "prefix": "S ",
                "value": latest.get("stop"),
                "digits": 2,
                "empty": "-",
            },
            {
                "kind": "price",
                "prefix": "T ",
                "value": latest.get("target"),
                "digits": 2,
                "empty": "-",
            },
        ),
    }
    return overlays.table(
        table_id="absorption_trap",
        title="Absorption Trap",
        columns=[
            ["MODE", mode, effective_read],
            ["STATE", latest.get("state") or "IDLE", latest.get("event") or "-"],
            [
                "LEVEL",
                {"kind": "price", "value": latest.get("level"), "digits": 2, "empty": "-"},
                {"kind": "integer", "value": latest.get("score"), "empty": "-"},
            ],
            [
                "FLOW",
                "Div" if latest.get("pressure_divergence") else "No div",
                {"kind": "fixed", "value": pressure, "suffix": "x", "digits": 2, "empty": "-"},
            ],
            ["PLAN", plan_entry, plan_exit],
        ],
        tone=tone,
        direction=direction,
    )["table"]


def absorption_trap(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    params: AbsorptionTrapParams | None = None,
    option_flow: dict[str, Any] | None = None,
    tick_flow: dict[str, Any] | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    params = params or AbsorptionTrapParams()
    if not bars:
        return {
            "version": ABSORPTION_TRAP_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "table": None,
            "overlays": [],
        }

    conservative = _conservative_mode(params)
    tick = max(float(profile.tick_size), 0.000001)
    lookback = max(_effective_level_lookback(params), 6)
    max_age = max(_effective_attack_max_bars(params), 2)
    cooldown_bars = max(int(params.cooldown_bars), 0)
    atr_values = pine.atr_rma_series(bars, max(int(params.atr_len), 2))
    ema233 = pine.ema_series([bar.close for bar in bars], 233)
    tick_pressures = _tick_pressure_by_bar(bars, tick_flow)
    volumes = [
        max(
            float(
                tick_pressures[index].get("total_volume")
                if tick_pressures[index]
                else bar.volume or 0.0
            ),
            0.0,
        )
        for index, bar in enumerate(bars)
    ]
    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    overlay_items: list[dict[str, Any]] = []
    signal_states: list[SignalState] = []
    high_attack: dict[str, Any] | None = None
    low_attack: dict[str, Any] | None = None
    high_cooldown = 0
    low_cooldown = 0

    for index, bar in enumerate(bars):
        emit_public = not preview_only or index == len(bars) - 1
        atr = max(float(atr_values[index]), 0.000001)
        rvol_value = _rvol(volumes, index, params.rvol_len)
        trend = _trend_context(
            bars=bars, ema233=ema233, atr_values=atr_values, index=index, params=params
        )
        item = _idle_item(bar)
        tick_pressure = tick_pressures[index]
        eff_data = _calculate_delta_efficiency(
            bar, tick_pressure, float(params.min_significant_delta)
        )
        item.update(eff_data)
        item.update(
            {
                "mode": "Conservative" if conservative else "Balanced",
                "level_lookback": lookback,
                "attack_max_bars": max_age,
                "min_score": _effective_min_score(params),
                "min_pierce_atr": _effective_min_pierce_atr(params),
            }
        )
        high_timed_out = False
        low_timed_out = False

        if high_cooldown > 0:
            high_cooldown -= 1
        if low_cooldown > 0:
            low_cooldown -= 1

        if index >= lookback:
            previous = bars[index - lookback : index]
            resistance = max(prev.high for prev in previous)
            support = min(prev.low for prev in previous)
            min_pierce = atr * max(_effective_min_pierce_atr(params), 0.0)
            repeat_tolerance_atr = _effective_repeat_tolerance_atr(params, atr, tick)
            repeat_tolerance = atr * repeat_tolerance_atr
            invalidation = atr * max(float(params.invalidation_atr), 0.05)

            if high_attack is not None:
                age = index - int(high_attack["index"])
                invalid = bar.close > float(high_attack["level"]) + invalidation
                expired = age > max_age
                if invalid or expired:
                    high_attack = None
                    high_timed_out = expired
                elif age >= 1 and bar.high >= float(high_attack["extreme"]) - repeat_tolerance:
                    pressure = _volume_pressure(bar, "high", tick_pressure)
                    score_data = _score_retest(
                        side="high",
                        attack=high_attack,
                        bar=bar,
                        atr=atr,
                        rvol_value=rvol_value,
                        pressure=pressure,
                        repeat_tolerance_atr=repeat_tolerance_atr,
                        params=params,
                    )
                    score_data["pressure_source"] = "tick" if tick_pressure else "candle_proxy"
                    attack_state = "SECOND_TOP_ATTACK" if conservative else "SECOND_ATTACK"
                    if side_state := ("_DIVERGENCE" if score_data["pressure_divergence"] else ""):
                        attack_state = f"{attack_state}{side_state}"
                    item = {
                        **item,
                        "state": attack_state,
                        "direction": Direction.SHORT.value,
                        "level": _round(float(high_attack["level"])),
                        "score": round(float(score_data["score"]), 2),
                        "rvol": round(rvol_value, 3),
                        "pressure_ratio": round(float(score_data["pressure_ratio"]), 4),
                        "pressure_divergence": bool(score_data["pressure_divergence"]),
                        "progress_atr": round(float(score_data["progress_atr"]), 4),
                        "repeat_tolerance_atr": round(float(score_data["repeat_tolerance_atr"]), 4),
                        "trend_slope_atr": round(float(trend["slope_atr"]), 4),
                        "distance_ema233_atr": round(float(trend["distance_atr"]), 4),
                        "pressure_source": "tick" if tick_pressure else "candle_proxy",
                    }
                    if _has_go_reclaim(score_data, params):
                        block_reason = _conservative_block_code(
                            side="high",
                            bars=bars,
                            index=index,
                            attack=high_attack,
                            atr=atr,
                            trend=trend,
                            params=params,
                            tick=tick,
                        )
                        if not block_reason and item.get("der_status") == "SHORT_SQUEEZE_RISK":
                            block_reason = "der_short_squeeze_risk"
                            if emit_public:
                                events.append(
                                    {
                                        "ts": bar.ts.isoformat(),
                                        "state": "BLOCKED_ANOMALY",
                                        "der_status": item.get("der_status"),
                                    }
                                )
                                anomaly_label = overlays.label(
                                    bar=bar,
                                    price=bar.high,
                                    lines=[],
                                    direction=Direction.SHORT,
                                    fact_fields=indicator_fact_payload(
                                        scenario="der_anomaly",
                                        opposing={"code": "aggressive_sellers_trapped"},
                                        trigger_event={"code": "momentum_long_wait_exhaustion"},
                                    ),
                                    role="absorption_trap",
                                )
                                anomaly_label["code"] = "DER_ANOMALY"
                                anomaly_label["action"] = "BLOCK"
                                anomaly_label["control_key"] = "labels"
                                overlay_items.append(anomaly_label)
                        if block_reason:
                            item = _blocked_signal(
                                item,
                                bar,
                                block_reason,
                                include_facts=emit_public,
                            )
                            high_attack = None
                            high_cooldown = cooldown_bars
                        else:
                            event = _event_payload(
                                side="high",
                                bars=bars,
                                index=index,
                                bar=bar,
                                attack=high_attack,
                                score_data=score_data,
                                rvol_value=rvol_value,
                                atr=atr,
                                params=params,
                                tick=tick,
                                option_flow=option_flow,
                                include_facts=emit_public,
                            )
                            item = {**item, **event}
                            if emit_public:
                                events.append(event)
                                wall_label = overlays.label(
                                    bar=bar,
                                    price=bar.high,
                                    lines=[],
                                    direction=Direction.SHORT,
                                    fact_fields=copy_indicator_facts(event),
                                    role="absorption_trap",
                                )
                                wall_label["code"] = "ABS_TRAP"
                                wall_label["action"] = "GO"
                                wall_label["glyph_kind"] = "absorption_wall"
                                wall_label["wall_direction"] = "down"
                                wall_label["control_key"] = "labels"
                                signal_payload = (
                                    event.get("signal")
                                    if isinstance(event.get("signal"), dict)
                                    else item.get("signal")
                                )
                                if isinstance(signal_payload, dict):
                                    overlays.bind_signal_trade_plan(
                                        wall_label, signal_payload, source="absorption_trap"
                                    )
                                overlay_items.append(wall_label)
                                wall_line = overlays.line(
                                    start=bar,
                                    bars_forward=_wall_line_bars(params),
                                    price=float(event["level"]),
                                    label_text="",
                                    direction=Direction.SHORT,
                                    fact_fields=copy_indicator_facts(event),
                                    width=1.35,
                                    opacity=None,
                                    style="wavy",
                                    role="absorption_trap",
                                )
                                wall_line["control_key"] = "levels"
                                overlay_items.append(wall_line)
                            high_attack = None
                            high_cooldown = cooldown_bars
                    elif age >= max_age:
                        high_attack = None
                        high_timed_out = True
                        item = _idle_item(bar)
                elif age >= max_age:
                    high_attack = None
                    high_timed_out = True

            if low_attack is not None:
                age = index - int(low_attack["index"])
                invalid = bar.close < float(low_attack["level"]) - invalidation
                expired = age > max_age
                if invalid or expired:
                    low_attack = None
                    low_timed_out = expired
                elif age >= 1 and bar.low <= float(low_attack["extreme"]) + repeat_tolerance:
                    pressure = _volume_pressure(bar, "low", tick_pressure)
                    score_data = _score_retest(
                        side="low",
                        attack=low_attack,
                        bar=bar,
                        atr=atr,
                        rvol_value=rvol_value,
                        pressure=pressure,
                        repeat_tolerance_atr=repeat_tolerance_atr,
                        params=params,
                    )
                    score_data["pressure_source"] = "tick" if tick_pressure else "candle_proxy"
                    attack_state = "SECOND_LOW_ATTACK" if conservative else "SECOND_ATTACK"
                    if side_state := ("_DIVERGENCE" if score_data["pressure_divergence"] else ""):
                        attack_state = f"{attack_state}{side_state}"
                    item = {
                        **item,
                        "state": attack_state,
                        "direction": Direction.LONG.value,
                        "level": _round(float(low_attack["level"])),
                        "score": round(float(score_data["score"]), 2),
                        "rvol": round(rvol_value, 3),
                        "pressure_ratio": round(float(score_data["pressure_ratio"]), 4),
                        "pressure_divergence": bool(score_data["pressure_divergence"]),
                        "progress_atr": round(float(score_data["progress_atr"]), 4),
                        "repeat_tolerance_atr": round(float(score_data["repeat_tolerance_atr"]), 4),
                        "trend_slope_atr": round(float(trend["slope_atr"]), 4),
                        "distance_ema233_atr": round(float(trend["distance_atr"]), 4),
                        "pressure_source": "tick" if tick_pressure else "candle_proxy",
                    }
                    if _has_go_reclaim(score_data, params):
                        block_reason = _conservative_block_code(
                            side="low",
                            bars=bars,
                            index=index,
                            attack=low_attack,
                            atr=atr,
                            trend=trend,
                            params=params,
                            tick=tick,
                        )
                        if not block_reason and item.get("der_status") == "LONG_LIQUIDATION_RISK":
                            block_reason = "der_long_liquidation_risk"
                            if emit_public:
                                events.append(
                                    {
                                        "ts": bar.ts.isoformat(),
                                        "state": "BLOCKED_ANOMALY",
                                        "der_status": item.get("der_status"),
                                    }
                                )
                                anomaly_label = overlays.label(
                                    bar=bar,
                                    price=bar.low,
                                    lines=[],
                                    direction=Direction.LONG,
                                    fact_fields=indicator_fact_payload(
                                        scenario="der_anomaly",
                                        opposing={"code": "aggressive_buyers_trapped"},
                                        trigger_event={"code": "momentum_short_wait_exhaustion"},
                                    ),
                                    role="absorption_trap",
                                )
                                anomaly_label["code"] = "DER_ANOMALY"
                                anomaly_label["action"] = "BLOCK"
                                anomaly_label["control_key"] = "labels"
                                overlay_items.append(anomaly_label)
                        if block_reason:
                            item = _blocked_signal(
                                item,
                                bar,
                                block_reason,
                                include_facts=emit_public,
                            )
                            low_attack = None
                            low_cooldown = cooldown_bars
                        else:
                            event = _event_payload(
                                side="low",
                                bars=bars,
                                index=index,
                                bar=bar,
                                attack=low_attack,
                                score_data=score_data,
                                rvol_value=rvol_value,
                                atr=atr,
                                params=params,
                                tick=tick,
                                option_flow=option_flow,
                                include_facts=emit_public,
                            )
                            item = {**item, **event}
                            if emit_public:
                                events.append(event)
                                wall_label = overlays.label(
                                    bar=bar,
                                    price=bar.low,
                                    lines=[],
                                    direction=Direction.LONG,
                                    fact_fields=copy_indicator_facts(event),
                                    role="absorption_trap",
                                )
                                wall_label["code"] = "ABS_TRAP"
                                wall_label["action"] = "GO"
                                wall_label["glyph_kind"] = "absorption_wall"
                                wall_label["wall_direction"] = "up"
                                wall_label["control_key"] = "labels"
                                signal_payload = (
                                    event.get("signal")
                                    if isinstance(event.get("signal"), dict)
                                    else item.get("signal")
                                )
                                if isinstance(signal_payload, dict):
                                    overlays.bind_signal_trade_plan(
                                        wall_label, signal_payload, source="absorption_trap"
                                    )
                                overlay_items.append(wall_label)
                                wall_line = overlays.line(
                                    start=bar,
                                    bars_forward=_wall_line_bars(params),
                                    price=float(event["level"]),
                                    label_text="",
                                    direction=Direction.LONG,
                                    fact_fields=copy_indicator_facts(event),
                                    width=1.35,
                                    opacity=None,
                                    style="wavy",
                                    role="absorption_trap",
                                )
                                wall_line["control_key"] = "levels"
                                overlay_items.append(wall_line)
                            low_attack = None
                            low_cooldown = cooldown_bars
                    elif age >= max_age:
                        low_attack = None
                        low_timed_out = True
                        item = _idle_item(bar)
                elif age >= max_age:
                    low_attack = None
                    low_timed_out = True

            if conservative and item.get("state") == "IDLE" and high_attack is not None:
                item = {
                    **item,
                    "state": "WAIT_TOP_RETEST",
                    "direction": Direction.SHORT.value,
                    "level": _round(float(high_attack["level"])),
                    "score": 45.0,
                    "rvol": round(rvol_value, 3),
                    "pressure_ratio": 1.0,
                    "repeat_tolerance_atr": round(float(repeat_tolerance_atr), 4),
                    **(
                        indicator_fact_payload(
                            scenario="conservative_absorption",
                            trigger_event={"code": "first_high_attack_registered"},
                            context={"code": "waiting_second_high_attack"},
                        )
                        if emit_public
                        else {}
                    ),
                }

            if conservative and item.get("state") == "IDLE" and low_attack is not None:
                item = {
                    **item,
                    "state": "WAIT_LOW_RETEST",
                    "direction": Direction.LONG.value,
                    "level": _round(float(low_attack["level"])),
                    "score": 45.0,
                    "rvol": round(rvol_value, 3),
                    "pressure_ratio": 1.0,
                    "repeat_tolerance_atr": round(float(repeat_tolerance_atr), 4),
                    **(
                        indicator_fact_payload(
                            scenario="conservative_absorption",
                            trigger_event={"code": "first_low_attack_registered"},
                            context={"code": "waiting_second_low_attack"},
                        )
                        if emit_public
                        else {}
                    ),
                }

            if (
                not high_timed_out
                and high_attack is None
                and high_cooldown <= 0
                and bar.high >= resistance + min_pierce
            ):
                pressure = _volume_pressure(bar, "high", tick_pressure)
                high_attack = {
                    "index": index,
                    "ts": bar.ts.isoformat(),
                    "level": resistance,
                    "extreme": bar.high,
                    "pressure": pressure,
                    "pressure_source": "tick" if tick_pressure else "candle_proxy",
                    "rvol": rvol_value,
                }
                item = {
                    **item,
                    "state": "FIRST_ATTACK",
                    "direction": Direction.SHORT.value,
                    "level": _round(resistance),
                    "score": 45.0,
                    "rvol": round(rvol_value, 3),
                    "pressure_ratio": 1.0,
                }

            if (
                not low_timed_out
                and low_attack is None
                and low_cooldown <= 0
                and bar.low <= support - min_pierce
            ):
                pressure = _volume_pressure(bar, "low", tick_pressure)
                low_attack = {
                    "index": index,
                    "ts": bar.ts.isoformat(),
                    "level": support,
                    "extreme": bar.low,
                    "pressure": pressure,
                    "pressure_source": "tick" if tick_pressure else "candle_proxy",
                    "rvol": rvol_value,
                }
                if item.get("state") == "IDLE":
                    item = {
                        **item,
                        "state": "FIRST_ATTACK",
                        "direction": Direction.LONG.value,
                        "level": _round(support),
                        "score": 45.0,
                        "rvol": round(rvol_value, 3),
                        "pressure_ratio": 1.0,
                    }

        signal_dict = item.get("signal")
        state_name = str(item.get("state") or "")
        signal_action = (
            str(signal_dict.get("action") or "").upper() if isinstance(signal_dict, dict) else ""
        )
        preserve_signal = signal_action == "GO" or state_name in {"FAILED_HIGH", "FAILED_LOW"}
        if not isinstance(signal_dict, dict) or (
            not preserve_signal
            and (state_name == "FIRST_ATTACK" or state_name.startswith("SECOND_"))
        ):
            direction = (
                Direction.LONG
                if item.get("direction") == "long"
                else Direction.SHORT
                if item.get("direction") == "short"
                else Direction.FLAT
            )
            raw_action = "WAIT"
            item["signal"] = _state_signal(
                bar=bar,
                action=raw_action,
                raw_action=raw_action,
                direction=direction,
                score=float(item["score"]),
                trigger=None,
                stop=None,
                target=None,
                reason_code="watching_repeated_auction_test",
                code="ABS_TRAP",
            )
        signal_data = item.get("signal") if isinstance(item.get("signal"), dict) else {}
        signal_states.append(
            SignalState(
                source="absorption_trap",
                action=ActionPhase(str(signal_data.get("action") or "WAIT")),
                raw_action=str(signal_data.get("raw_action") or "WAIT"),
                direction=Direction.LONG
                if signal_data.get("direction") == "long"
                else Direction.SHORT
                if signal_data.get("direction") == "short"
                else Direction.FLAT,
                score=float(signal_data["score"]),
                confirmed=bar.closed,
                source_tf=bar.timeframe,
                trigger=signal_data.get("trigger"),
                stop=signal_data.get("stop"),
                target=signal_data.get("target"),
                invalidation=signal_data.get("invalidation"),
                reason=str(signal_data.get("reason_code") or ""),
                code=str(signal_data.get("code") or "ABS_TRAP"),
            )
        )
        if not preview_only or index == len(bars) - 1:
            series.append(item)

    if len(events) > params.max_events:
        events = events[-params.max_events :]
    if preview_only:
        events = [event for event in events if event.get("ts") == bars[-1].ts.isoformat()]
        overlay_items = []
    if len(overlay_items) > params.max_events * 2:
        overlay_items = overlay_items[-params.max_events * 2 :]
    latest = series[-1] if series else None
    table = _table_for(latest) if latest and not preview_only else None
    if table is not None:
        overlay_items.append(
            {
                "type": "table",
                "id": "absorption_trap",
                "control_key": "table",
                "table": table,
                **copy_indicator_facts(latest),
            }
        )
    lifecycle = lifecycle_from_signals(
        source="absorption_trap",
        bars=bars,
        signals=signal_states,
        atr_values=atr_values,
        max_age_bars=max(params.attack_max_bars * 2, 6),
        trail_atr=0.9,
    )
    if latest is not None and lifecycle is not None:
        latest["lifecycle"] = lifecycle.as_dict()
    return {
        "version": ABSORPTION_TRAP_VERSION,
        "params": {
            "level_lookback": lookback,
            "attack_max_bars": max_age,
            "min_pierce_atr": _effective_min_pierce_atr(params),
            "repeat_tolerance_atr": _effective_repeat_tolerance_atr(
                params,
                max(float(atr_values[-1]), 0.000001),
                tick,
            ),
            "min_score": _effective_min_score(params),
            "mode": "Conservative" if conservative else "Balanced",
            "structure_stop_bars": params.structure_stop_bars,
            "pressure_source": "tick" if any(tick_pressures) else "candle_proxy",
            "synergy_matrix": params.synergy_matrix,
        },
        "series": series[-240:],
        "events": events,
        "latest": latest,
        "table": table,
        "overlays": overlay_items,
    }
