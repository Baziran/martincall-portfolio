from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.features.vsa_classify import VSA_ABSORPTION_CODES, VSA_IMPULSE_CODES
from aef_terminal.indicators.domain_facts import indicator_fact_payload, metric_number
from aef_terminal.runtime.math_utils import round_optional
from aef_terminal.runtime.signal_state import SignalState


@dataclass(frozen=True)
class LindaTableInputs:
    latest_state: str
    plan_signal: SignalState
    plan_dir: Direction
    plan: Mapping[str, Any]
    plan_code: str
    latest_item: Mapping[str, Any]
    last_indian: Mapping[str, Any] | None
    entry_conflict: bool
    ct_watch_long: bool
    ct_watch_short: bool
    flow_conflict: bool
    side_veto_long: bool
    side_veto_short: bool
    poi_gate_active: bool
    poi_gate_long: bool
    poi_gate_short: bool
    entry_long_confirm: bool
    entry_short_confirm: bool
    entry_long_warn: bool
    entry_short_warn: bool
    micro_long_break: bool
    micro_short_break: bool
    micro_long_watch: bool
    micro_short_watch: bool
    micro_context: Mapping[str, Any]
    is_grail_buy: bool
    is_grail_sell: bool
    grail_buy_rr: float
    grail_sell_rr: float
    liq_relong_active: bool
    liq_reshort_active: bool
    liq_reclaim_long_active: bool
    liq_reclaim_short_active: bool
    trap_ready: bool
    trap_execution: bool
    trap_direction: str
    trap_context: Mapping[str, Any]
    vsa_fuel_latest: bool
    vsa_climax_latest: bool
    vsa_context_code: str
    context_code: str
    context_dir: str
    runaway_day_up: bool
    runaway_day_dn: bool
    vwap_upper_2_ride: bool
    vwap_lower_2_ride: bool
    contra_short_locked: bool
    contra_long_locked: bool
    vsa_ctx: Mapping[str, Any]
    vsa_latest: Mapping[str, Any]
    vix_put_watch: bool
    vix_context: Mapping[str, Any]
    liquidity_context: Mapping[str, Any]
    flow_bias: int
    opp_exh_long: bool
    opp_exh_short: bool
    poi_context: Mapping[str, Any]
    latest_bar: Bar
    latest_vwap: float
    ema20_slope_atr: float
    ema233_slope_atr: float


@dataclass(frozen=True)
class LindaTableResult:
    state: dict[str, Any]
    table: dict[str, Any]


def build_linda_table(inputs: LindaTableInputs) -> LindaTableResult:
    plan_action = inputs.plan_signal.action.value
    plan_direction = (
        inputs.plan_dir.value if inputs.plan_dir in {Direction.LONG, Direction.SHORT} else "flat"
    )
    execute_long = (
        plan_action == "GO"
        and inputs.plan_dir == Direction.LONG
        and not inputs.side_veto_long
        and inputs.poi_gate_long
        and not inputs.flow_conflict
        and not inputs.entry_short_confirm
    )
    execute_short = (
        plan_action == "GO"
        and inputs.plan_dir == Direction.SHORT
        and not inputs.side_veto_short
        and inputs.poi_gate_short
        and not inputs.flow_conflict
        and not inputs.entry_long_confirm
    )
    armed_long = (
        plan_action == "ARM"
        and inputs.plan_dir == Direction.LONG
        and not inputs.side_veto_long
        and inputs.poi_gate_long
        and not inputs.flow_conflict
    )
    armed_short = (
        plan_action == "ARM"
        and inputs.plan_dir == Direction.SHORT
        and not inputs.side_veto_short
        and inputs.poi_gate_short
        and not inputs.flow_conflict
    )
    indian_count = int(inputs.last_indian.get("indian_count") or 0) if inputs.last_indian else 0
    entry_context: dict[str, Any] = {
        "state": "wait",
        "detail_code": inputs.latest_state,
        "direction": plan_direction,
        "level": None,
        "relation": None,
        "rr": None,
        "indian_count": indian_count,
    }
    if inputs.ct_watch_long or inputs.ct_watch_short:
        entry_context.update(
            state="countertrend_watch",
            detail_code="wait_reclaim",
            direction="long" if inputs.ct_watch_long else "short",
        )
    elif inputs.entry_conflict or inputs.flow_conflict:
        entry_context.update(
            state="conflict",
            detail_code="entry_conflict" if inputs.entry_conflict else "flow_conflict",
            direction="flat",
        )
    elif inputs.side_veto_long and inputs.plan_dir == Direction.LONG:
        entry_context.update(state="side_veto", detail_code="need_unlock", direction="long")
    elif inputs.side_veto_short and inputs.plan_dir == Direction.SHORT:
        entry_context.update(state="side_veto", detail_code="need_unlock", direction="short")
    elif inputs.plan_dir == Direction.LONG and not inputs.poi_gate_long:
        entry_context.update(state="poi_not_hot", detail_code="long_not_hot", direction="long")
    elif inputs.plan_dir == Direction.SHORT and not inputs.poi_gate_short:
        entry_context.update(state="poi_not_hot", detail_code="short_not_hot", direction="short")
    elif execute_long or execute_short:
        entry_context.update(
            state="execute",
            detail_code="execute_from_level",
            direction="long" if execute_long else "short",
            level=round_optional(inputs.plan.get("trigger")),
        )
    elif armed_long or armed_short:
        entry_context.update(
            state="armed",
            detail_code="wait_reclaim" if armed_long else "wait_reject",
            direction="long" if armed_long else "short",
            level=round_optional(inputs.plan.get("trigger")),
        )
    elif inputs.micro_long_break or inputs.micro_short_break:
        entry_context.update(
            state="micro_break",
            detail_code="wait_retest",
            direction="long" if inputs.micro_long_break else "short",
        )
    elif inputs.micro_long_watch or inputs.micro_short_watch:
        micro_entry_level = (
            inputs.micro_context.get("high")
            if inputs.micro_long_watch
            else inputs.micro_context.get("low")
        )
        entry_context.update(
            state="micro_watch",
            detail_code="range_boundary",
            direction="long" if inputs.micro_long_watch else "short",
            level=round_optional(micro_entry_level),
            relation="above" if inputs.micro_long_watch else "below",
        )
    elif inputs.poi_gate_active and (inputs.entry_long_confirm or inputs.entry_short_confirm):
        entry_context.update(
            state="poi_confirmed",
            detail_code="confirmed",
            direction="long" if inputs.entry_long_confirm else "short",
        )
    elif inputs.poi_gate_active and (inputs.entry_long_warn or inputs.entry_short_warn):
        entry_context.update(
            state="poi_watch",
            detail_code="watch",
            direction="long" if inputs.entry_long_warn else "short",
        )
    elif inputs.is_grail_buy or inputs.is_grail_sell:
        entry_context.update(
            state="grail",
            detail_code="risk_reward",
            direction="long" if inputs.is_grail_buy else "short",
            rr=round_optional(inputs.grail_buy_rr if inputs.is_grail_buy else inputs.grail_sell_rr),
        )
    elif inputs.liq_relong_active or inputs.liq_reshort_active:
        entry_context.update(
            state="liquidity_reentry",
            detail_code="liquidity_hold" if inputs.liq_relong_active else "liquidity_reject",
            direction="long" if inputs.liq_relong_active else "short",
        )
    elif inputs.liq_reclaim_long_active or inputs.liq_reclaim_short_active:
        entry_context.update(
            state="liquidity_reclaim",
            detail_code="hold_or_reject",
            direction="long" if inputs.liq_reclaim_long_active else "short",
        )
    elif inputs.trap_ready:
        entry_context.update(
            state="trap_armed" if inputs.trap_execution else "trap_watch",
            detail_code="trap_level",
            direction=inputs.trap_direction,
            level=round_optional(inputs.trap_context.get("level")),
        )
    elif inputs.vsa_fuel_latest or inputs.vsa_climax_latest:
        entry_context.update(
            state="climax_watch" if inputs.vsa_climax_latest else "fuel_watch",
            detail_code="terminal" if inputs.vsa_climax_latest else "no_chase",
            direction=inputs.context_dir,
        )
    elif bool(inputs.plan.get("blocked")) and inputs.vsa_context_code in VSA_IMPULSE_CODES:
        entry_context.update(
            state="impulse_pullback_wait",
            detail_code="wait_pullback",
            direction=inputs.context_dir,
        )
    elif inputs.context_code in {"PB_UP", "PB_DN"}:
        entry_context.update(
            state="pullback_watch",
            detail_code="indian_pullback" if inputs.last_indian else "pullback",
            direction=inputs.context_dir,
        )
    elif inputs.vsa_context_code in {"SPRING", "UPTHRUST", "EXH_UP", "EXH_DN"}:
        entry_context.update(
            state="trigger_watch",
            detail_code="reclaim_or_reject",
            direction=inputs.context_dir,
        )

    risk_value: Any = None
    risk_direction = "flat"
    if (
        inputs.runaway_day_up
        or inputs.vwap_upper_2_ride
        or inputs.contra_short_locked
        or inputs.side_veto_short
    ):
        risk_state = "no_short"
        risk_detail_code = (
            "side_veto"
            if inputs.side_veto_short
            else "band_runaway"
            if inputs.runaway_day_up or inputs.vwap_upper_2_ride
            else "trend_lock"
        )
        risk_tone = "danger"
        risk_blocks = ("short",)
        risk_direction = "short"
    elif (
        inputs.runaway_day_dn
        or inputs.vwap_lower_2_ride
        or inputs.contra_long_locked
        or inputs.side_veto_long
    ):
        risk_state = "no_long"
        risk_detail_code = (
            "side_veto"
            if inputs.side_veto_long
            else "band_runaway"
            if inputs.runaway_day_dn or inputs.vwap_lower_2_ride
            else "trend_lock"
        )
        risk_tone = "danger"
        risk_blocks = ("long",)
        risk_direction = "long"
    elif inputs.vsa_climax_latest or bool(inputs.vsa_ctx.get("terminal_climax")):
        risk_state = "climax"
        risk_detail_code = str(
            inputs.vsa_latest.get("terminal_kind")
            or inputs.vsa_ctx.get("terminal_kind")
            or "terminal"
        )
        risk_tone = "warn"
        risk_blocks = ()
    elif inputs.vsa_fuel_latest or bool(inputs.vsa_ctx.get("fuel")):
        risk_state = "fuel"
        risk_detail_code = "fuel"
        risk_tone = "warn"
        risk_blocks = ()
    elif inputs.trap_ready:
        risk_state = "countertrend_trap"
        risk_detail_code = "trap_ready"
        risk_tone = "warn"
        risk_blocks = ()
        risk_direction = inputs.trap_direction
    elif bool(inputs.trap_context.get("active")):
        risk_state = "shift"
        risk_detail_code = "trap_shift"
        risk_tone = "warn"
        risk_blocks = ()
        risk_direction = inputs.trap_direction
    elif inputs.vix_put_watch:
        risk_state = "vix"
        risk_detail_code = str(inputs.vix_context.get("state") or "vix_watch")
        risk_tone = "warn"
        risk_blocks = ()
        risk_direction = "short"
    elif bool(inputs.liquidity_context.get("price_in_void")):
        risk_state = "void"
        risk_detail_code = str(inputs.liquidity_context.get("void_direction") or "void")
        risk_tone = "warn"
        risk_blocks = ()
        risk_direction = str(inputs.liquidity_context.get("void_direction") or "flat")
    elif (
        inputs.latest_item.get("range_atr", 0)
        and float(inputs.latest_item.get("range_atr") or 0) >= 1.8
    ):
        risk_state = "wide_range"
        risk_detail_code = "large_range"
        risk_tone = "warn"
        risk_blocks = ()
        risk_value = round_optional(inputs.latest_item.get("range_atr"))
    elif inputs.latest_item.get("rvol", 0) and float(inputs.latest_item.get("rvol") or 0) < 0.85:
        risk_state = "thin_volume"
        risk_detail_code = "low_rvol"
        risk_tone = "warn"
        risk_blocks = ()
        risk_value = round_optional(inputs.latest_item.get("rvol"))
    elif abs(inputs.flow_bias) <= 1:
        risk_state = "chop"
        risk_detail_code = "mixed_flow"
        risk_tone = "warn"
        risk_blocks = ()
    else:
        risk_state = "clear"
        risk_detail_code = "rvol"
        risk_tone = "clear"
        risk_blocks = ()
        risk_value = round_optional(inputs.latest_item.get("rvol"))
    risk_context = {
        "state": risk_state,
        "detail_code": risk_detail_code,
        "tone": risk_tone,
        "blocks": list(risk_blocks),
        "direction": risk_direction,
        "value": risk_value,
    }

    poi_price_in_zone = bool(
        inputs.poi_context.get("hot")
        or inputs.poi_context.get("in_core")
        or inputs.poi_context.get("in_deep")
    )
    opponent_context: dict[str, Any] = {
        "state": "none",
        "detail_code": "no_edge",
        "trigger": None,
        "relation": None,
        "direction": "flat",
        "exhaustion_long": bool(inputs.opp_exh_long),
        "exhaustion_short": bool(inputs.opp_exh_short),
        "price_in_poi": poi_price_in_zone,
        "poi_active": inputs.poi_gate_active,
        "poi_deep": bool(inputs.poi_context.get("in_deep")),
        "indian_count": indian_count,
    }
    if inputs.opp_exh_long or inputs.opp_exh_short:
        opponent_context.update(
            state="smart_money",
            detail_code="exhaustion",
            trigger=round_optional(
                inputs.latest_bar.high if inputs.opp_exh_long else inputs.latest_bar.low
            ),
            relation="above" if inputs.opp_exh_long else "below",
            direction="long" if inputs.opp_exh_long else "short",
        )
    elif poi_price_in_zone:
        opponent_context.update(
            state="poi_active",
            detail_code="poi_edge",
            direction=str(inputs.poi_context.get("direction") or "flat"),
        )
    elif inputs.poi_gate_active:
        opponent_context.update(
            state="poi_wait",
            detail_code="wait_poi",
            direction=str(inputs.poi_context.get("direction") or "flat"),
        )
    elif (
        inputs.vsa_context_code in VSA_ABSORPTION_CODES
        or inputs.vsa_fuel_latest
        or bool(inputs.vsa_ctx.get("fuel"))
    ):
        opponent_context.update(
            state="absorption_watch",
            detail_code="watch_edge",
            direction=inputs.context_dir,
        )
    elif inputs.last_indian:
        opponent_context.update(
            state="indian_wait",
            detail_code="indian_pullback",
            direction=str(inputs.last_indian.get("direction") or "flat"),
        )

    plan_stop = float(inputs.plan["stop"]) if inputs.plan.get("stop") is not None else None
    poi_deep_bottom = (
        float(inputs.poi_context["deep_bottom"])
        if inputs.poi_context.get("deep_bottom") is not None
        else None
    )
    poi_deep_top = (
        float(inputs.poi_context["deep_top"])
        if inputs.poi_context.get("deep_top") is not None
        else None
    )
    invalidation_context: dict[str, Any] = {
        "state": "vwap",
        "relation": None,
        "blocks_side": None,
        "level": round_optional(inputs.latest_vwap),
        "direction": "flat",
        "source": "vwap",
    }
    if inputs.plan_dir == Direction.LONG and plan_stop is not None:
        invalidation_context.update(
            state="plan_stop",
            relation="below",
            level=round_optional(plan_stop),
            direction="long",
            source="plan",
        )
    elif inputs.plan_dir == Direction.SHORT and plan_stop is not None:
        invalidation_context.update(
            state="plan_stop",
            relation="above",
            level=round_optional(plan_stop),
            direction="short",
            source="plan",
        )
    elif inputs.micro_long_watch or inputs.micro_long_break:
        invalidation_context.update(
            state="micro_range",
            relation="below",
            level=round_optional(inputs.micro_context.get("low")),
            direction="long",
            source="micro",
        )
    elif inputs.micro_short_watch or inputs.micro_short_break:
        invalidation_context.update(
            state="micro_range",
            relation="above",
            level=round_optional(inputs.micro_context.get("high")),
            direction="short",
            source="micro",
        )
    elif (
        inputs.poi_gate_active
        and str(inputs.poi_context.get("direction") or "flat") == "long"
        and poi_deep_bottom is not None
    ):
        invalidation_context.update(
            state="poi_boundary",
            relation="below",
            level=round_optional(poi_deep_bottom),
            direction="long",
            source="poi",
        )
    elif (
        inputs.poi_gate_active
        and str(inputs.poi_context.get("direction") or "flat") == "short"
        and poi_deep_top is not None
    ):
        invalidation_context.update(
            state="poi_boundary",
            relation="above",
            level=round_optional(poi_deep_top),
            direction="short",
            source="poi",
        )
    elif inputs.runaway_day_up or inputs.vwap_upper_2_ride or inputs.contra_short_locked:
        invalidation_context.update(
            state="side_block",
            relation="reclaim",
            blocks_side="short",
            direction="short",
            source="session",
        )
    elif inputs.runaway_day_dn or inputs.vwap_lower_2_ride or inputs.contra_long_locked:
        invalidation_context.update(
            state="side_block",
            relation="reclaim",
            blocks_side="long",
            direction="long",
            source="session",
        )

    table_metrics = {
        "action": plan_action,
        "code": inputs.plan_code or "",
        "rvol": metric_number(inputs.latest_item.get("rvol")),
        "ema20_slope_atr": metric_number(inputs.ema20_slope_atr, suffix=" ATR"),
        "ema233_slope_atr": metric_number(inputs.ema233_slope_atr, suffix=" ATR"),
    }
    table_state = {
        "entry": entry_context,
        "risk": risk_context,
        "opponent": opponent_context,
        "invalidation": invalidation_context,
        "action": plan_action,
        "reason_code": str(
            inputs.plan.get("reason_code") or inputs.latest_item.get("reason_code") or "linda_wait"
        ),
        "code": inputs.plan_code,
        "direction": plan_direction,
        "metrics": table_metrics,
    }
    table = {
        "model_ref": "linda_volume",
        "model": table_state,
        "direction": table_state["direction"],
        "tone": risk_tone,
        **indicator_fact_payload(
            scenario="linda_theory",
            trigger_event={"code": f"linda_{plan_action.lower()}"},
            supporting={
                "code": "linda_plan_reason",
                "reason_code": table_state["reason_code"],
            },
            context=(
                {
                    "code": "indian_pullback_stage",
                    "count": indian_count,
                    "direction": str(inputs.last_indian.get("direction") or "flat"),
                }
                if inputs.last_indian
                else None
            ),
            risk={
                "code": "linda_risk_state",
                "state": risk_state,
                "detail_code": risk_detail_code,
                "direction": risk_direction,
                "tone": risk_tone,
                "blocks": [{"code": str(code)} for code in risk_blocks],
            },
            metrics=table_metrics,
        ),
    }
    return LindaTableResult(state=table_state, table=table)
