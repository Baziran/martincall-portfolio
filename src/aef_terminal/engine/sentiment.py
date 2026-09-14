from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from statistics import median
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, ScenarioDecision, SignalCandidate
from aef_terminal.engine.common import bar_index_at_or_before
from aef_terminal.engine.sentiment_weights import (
    contributor_weight_meta,
    resolve_candidate_weight,
    resolve_indicator_weight,
)
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import bool_param, exact_finite_number_or_none
from aef_terminal.runtime.signal_state import direction_from_value
from aef_terminal.indicators.domain_facts import indicator_fact_payload
from aef_terminal.runtime import overlays


INSTITUTIONAL_EDGE_SCORE = 95.0

# Tick-flow admission ratios are dimensionless: strength is recent delta divided
# by its median absolute baseline; delta ratio is signed delta divided by volume.
TICK_FLOW_STRENGTH_MIN = 1.8
TICK_FLOW_DELTA_RATIO_MIN = 0.08

# Tick-flow scoring is in sentiment points; the factors convert one ratio unit
# to points before the bonus and final score are capped on the 0-100 scale.
TICK_FLOW_SCORE_BASE = 50.0
TICK_FLOW_SCORE_MAX = 95.0
TICK_FLOW_STRENGTH_SCORE_FACTOR = 8.0
TICK_FLOW_DELTA_SCORE_FACTOR = 35.0
TICK_FLOW_SCORE_BONUS_MAX = 45.0


def _sentiment_score(value: Any) -> float | None:
    number = exact_finite_number_or_none(value)
    if number is None:
        return None
    return max(0.0, min(100.0, number))


def _indicator_sentiment_item(
    source: str, payload: dict[str, Any], weight: float
) -> dict[str, Any] | None:
    latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else {}
    signal = latest.get("signal")
    if not isinstance(signal, dict) or bool(signal.get("obsolete") or latest.get("obsolete")):
        return None
    trade_plan = signal.get("trade_plan") if isinstance(signal.get("trade_plan"), dict) else {}
    if trade_plan.get("complete") and trade_plan.get("coherent") is False:
        return None
    direction = direction_from_value(signal.get("direction"))
    try:
        action = ActionPhase(signal.get("action"))
    except TypeError, ValueError:
        return None
    if direction == Direction.FLAT or action is ActionPhase.WAIT:
        return None
    score = _sentiment_score(signal.get("score"))
    if score is None:
        return None
    return {
        "source": source,
        "direction": direction.value,
        "score": round(score, 1),
        **contributor_weight_meta(source, weight),
        "action": action.value,
        "role": "indicator",
    }


def _indicator_sentiment_role(payload: dict[str, Any]) -> str:
    latest = payload.get("latest") if isinstance(payload.get("latest"), dict) else {}
    return str(payload.get("sentiment_role") or latest.get("sentiment_role") or "").strip()


def _market_context_payload(indicators: dict[str, Any]) -> dict[str, Any] | None:
    for payload in indicators.values():
        if isinstance(payload, dict) and _indicator_sentiment_role(payload) == "market_context":
            return payload
    return None


def _evaluate_institutional_edge(
    indicators: dict[str, Any],
    *,
    vsa_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Intersect VSA + Linda context and Absorption Trap DER for premium institutional edges."""
    linda_payload = indicators.get("linda_volume") if isinstance(indicators, dict) else None
    vsa_payload = vsa_context if isinstance(vsa_context, dict) else None
    trap_payload = indicators.get("absorption_trap") if isinstance(indicators, dict) else None
    if not isinstance(trap_payload, dict):
        return None
    trap_params = trap_payload.get("params") if isinstance(trap_payload.get("params"), dict) else {}
    if bool_param(trap_params.get("synergy_matrix"), True) is False:
        return None

    linda_latest = (
        linda_payload.get("latest")
        if isinstance(linda_payload, dict) and isinstance(linda_payload.get("latest"), dict)
        else {}
    )
    vsa_latest = (
        vsa_payload.get("latest")
        if isinstance(vsa_payload, dict) and isinstance(vsa_payload.get("latest"), dict)
        else {}
    )
    vsa_signal = vsa_latest.get("signal") if isinstance(vsa_latest.get("signal"), dict) else {}
    trap_latest = trap_payload.get("latest") if isinstance(trap_payload.get("latest"), dict) else {}

    vsa_code = str(vsa_latest.get("code") or vsa_signal.get("code") or "").upper()
    linda_code = str(linda_latest.get("code") or "").upper()
    der_status = str(trap_latest.get("der_status") or "").upper()

    edge_ctx: dict[str, Any] | None = None
    edge_signal_code = vsa_code or linda_code
    if (
        vsa_code in {"UPTHRUST", "EXH_DN"} or linda_code in {"NO_DEMAND", "VW_REJECT"}
    ) and der_status == "LONG_LIQUIDATION_RISK":
        edge_ctx = {
            "edge": "SHORT",
            "source": "absorption_trap",
            "phase": "go",
            "action": ActionPhase.GO.value,
            "score": INSTITUTIONAL_EDGE_SCORE,
            "weight": resolve_indicator_weight("institutional_edge"),
            "signal_code": edge_signal_code,
            "der_status": der_status,
            "reason_code": "vsa_der_absorption",
        }
    elif (
        vsa_code in {"SPRING", "EXH_UP"} or linda_code in {"NO_SUPPLY", "VW_RECLAIM"}
    ) and der_status == "SHORT_SQUEEZE_RISK":
        edge_ctx = {
            "edge": "LONG",
            "source": "absorption_trap",
            "phase": "go",
            "action": ActionPhase.GO.value,
            "score": INSTITUTIONAL_EDGE_SCORE,
            "weight": resolve_indicator_weight("institutional_edge"),
            "signal_code": edge_signal_code,
            "der_status": der_status,
            "reason_code": "vsa_der_absorption",
        }
    if edge_ctx is None:
        return None

    market_context_payload = (
        _market_context_payload(indicators) if isinstance(indicators, dict) else None
    )
    market_context = _market_context_from_payload(market_context_payload)
    strategy = (
        market_context.get("strategy")
        if isinstance(market_context, dict) and isinstance(market_context.get("strategy"), dict)
        else {}
    )
    macro_direction = direction_from_value(
        market_context.get("direction") if isinstance(market_context, dict) else None
    )
    strategy_phase = str(strategy.get("phase") or "").strip().lower()
    strategy_active = bool(strategy.get("active")) and strategy_phase == "execute"
    if strategy_active:
        if edge_ctx.get("edge") == "SHORT" and macro_direction == Direction.LONG:
            edge_ctx["action"] = ActionPhase.WATCH.value
            edge_ctx["phase"] = "watch"
            edge_ctx["score"] = 60.0
        elif edge_ctx.get("edge") == "LONG" and macro_direction == Direction.SHORT:
            edge_ctx["action"] = ActionPhase.WATCH.value
            edge_ctx["phase"] = "watch"
            edge_ctx["score"] = 60.0
    return edge_ctx


def _institutional_edge_overlay(edge_ctx: dict[str, Any], bar: Bar) -> dict[str, Any] | None:
    edge = str(edge_ctx.get("edge") or "").upper()
    if edge not in {"LONG", "SHORT"}:
        return None
    is_long = edge == "LONG"
    direction = Direction.LONG if is_long else Direction.SHORT
    item = overlays.label(
        bar=bar,
        price=bar.low if is_long else bar.high,
        lines=[],
        direction=direction,
        fact_fields=indicator_fact_payload(
            scenario="institutional_edge",
            trigger_event={
                "code": "edge_absorption",
                "direction": direction.value,
                "phase": str(edge_ctx.get("phase") or "go"),
            },
            supporting=[
                {
                    "code": "signal_context",
                    "signal_code": str(edge_ctx.get("signal_code") or "unknown"),
                },
                {"code": "der_state", "status": str(edge_ctx.get("der_status") or "unknown")},
            ],
            opposing={
                "code": "aggressive_counterflow_trapped",
                "direction": "short" if is_long else "long",
            },
            context={"code": "micro_stop", "relation": "below" if is_long else "above"},
        ),
        role="edge_absorption_long" if is_long else "edge_absorption_short",
    )
    item["code"] = "EDGE_ABSORPTION"
    item["action"] = str(edge_ctx.get("action") or "WATCH")
    return item


def _market_context_from_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    # The producer's top-level domain context is authoritative. Table and latest
    # copies are projections and must not recover absent or malformed facts.
    context = payload.get("market_context")
    return context if isinstance(context, dict) else None


def _market_context_sentiment(
    source: str,
    payload: dict[str, Any] | None,
    weight: float,
) -> dict[str, Any] | None:
    context = _market_context_from_payload(payload)
    if not isinstance(context, dict):
        return None
    direction = direction_from_value(context.get("direction"))
    if direction == Direction.FLAT:
        return None
    score = _sentiment_score(context.get("score"))
    if score is None:
        return None
    strategy = context.get("strategy") if isinstance(context.get("strategy"), dict) else {}
    strategy_phase = str(strategy.get("phase") or "off").strip().lower()
    strategy_direction = direction_from_value(strategy.get("direction"))
    strategy_active = (
        bool(strategy.get("active"))
        and strategy_phase != "off"
        and strategy_direction != Direction.FLAT
    )
    strategy_conflict = bool(strategy_active and strategy_direction != direction)
    action = ActionPhase.WATCH
    adjusted_weight = float(weight)
    if strategy_active:
        if strategy_phase == "execute":
            action = ActionPhase.GO
            score = max(score, 86.0)
            if not strategy_conflict:
                adjusted_weight += 0.25
        elif strategy_phase == "armed":
            action = ActionPhase.ARM
            score = max(score, 78.0)
            if not strategy_conflict:
                adjusted_weight += 0.15
        elif strategy_phase == "impulse_confirmed":
            score = max(score, 72.0)
            if not strategy_conflict:
                adjusted_weight += 0.05
        elif strategy_phase == "impulse_developing":
            score = max(score, 68.0)
    return {
        "source": source,
        "direction": direction.value,
        "score": round(score, 1),
        **contributor_weight_meta(source, adjusted_weight),
        "action": action.value,
        "role": "market_context",
        "risk_lock": context.get("risk_lock") or "none",
        "side_lock": context.get("side_lock") or "CHECK",
        "warning_direction": context.get("warning_direction") or "flat",
        "strategy_active": strategy_active,
        "strategy_phase": strategy_phase,
        "strategy_direction": strategy_direction.value,
        "strategy_conflict": strategy_conflict,
    }


def _gate_reversal_warning_by_context(
    item: dict[str, Any], context: dict[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(context, dict):
        return item
    family = _sentiment_family(str(item.get("source") or ""))
    if family not in {"w5_structure", "wolfe_structure"}:
        return item
    context_direction = direction_from_value(context.get("direction"))
    item_direction = direction_from_value(item.get("direction"))
    if (
        context_direction == Direction.FLAT
        or item_direction == Direction.FLAT
        or context_direction == item_direction
    ):
        return item
    risk_lock = str(context.get("risk_lock") or "none").lower()
    side_lock = str(context.get("side_lock") or "CHECK").upper()
    blocked_short = item_direction == Direction.SHORT and (
        risk_lock == "no_short" or side_lock == "LONG"
    )
    blocked_long = item_direction == Direction.LONG and (
        risk_lock == "no_long" or side_lock == "SHORT"
    )
    strategy = context.get("strategy") if isinstance(context.get("strategy"), dict) else {}
    strategy_phase = str(strategy.get("phase") or "off").strip().lower()
    strategy_direction = direction_from_value(strategy.get("direction"))
    strategy_blocks = (
        bool(strategy.get("active"))
        and strategy_phase in {"execute", "armed"}
        and strategy_direction != Direction.FLAT
        and strategy_direction != item_direction
    )
    if not (blocked_short or blocked_long or strategy_blocks):
        return item
    gated = dict(item)
    gated["original_weight"] = gated["weight"]
    gated["weight"] = 0.0
    gated["action"] = ActionPhase.WATCH.value
    gated["blocked_by_context"] = True
    gated["blocked_by_strategy"] = strategy_blocks
    gated["context_direction"] = context_direction.value
    gated["strategy_phase"] = strategy_phase
    gated["strategy_direction"] = strategy_direction.value
    return gated


def tick_flow_bias_from_context(
    tick_flow: dict[str, Any] | None,
    bars: Sequence[Bar],
    *,
    window_bars: int = 5,
) -> dict[str, Any] | None:
    rows = tick_flow.get("delta") if isinstance(tick_flow, dict) else None
    if not rows or not bars or tick_flow.get("decision_eligible") is not True:
        return None
    clean_bars = [bar for bar in bars if bar.closed is not False]
    if len(clean_bars) < max(3, window_bars):
        return None
    buckets = [{"net_delta": 0.0, "total_volume": 0.0, "trade_count": 0.0} for _ in clean_bars]
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_ts = row.get("ts")
        if isinstance(raw_ts, datetime):
            ts = raw_ts
        else:
            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            except TypeError, ValueError:
                continue
        index = bar_index_at_or_before(clean_bars, ts)
        if index is None:
            continue
        net_delta = exact_finite_number_or_none(row.get("net_delta"))
        total_volume = exact_finite_number_or_none(row.get("total_volume"))
        trade_count = exact_finite_number_or_none(row.get("trade_count"))
        if net_delta is None or total_volume is None or trade_count is None:
            continue
        buckets[index]["net_delta"] += net_delta
        buckets[index]["total_volume"] += total_volume
        buckets[index]["trade_count"] += trade_count
    lookback = max(3, int(window_bars))
    recent = buckets[-lookback:]
    history = buckets[:-lookback] or buckets
    recent_delta = sum(item["net_delta"] for item in recent)
    recent_volume = sum(item["total_volume"] for item in recent)
    abs_history = [abs(item["net_delta"]) for item in history if abs(item["net_delta"]) > 0]
    baseline = median(abs_history) if abs_history else 0.0
    if baseline <= 0 or recent_volume <= 0:
        return None
    ratio = recent_delta / baseline
    delta_ratio = recent_delta / recent_volume
    direction = (
        Direction.LONG
        if ratio >= TICK_FLOW_STRENGTH_MIN and delta_ratio >= TICK_FLOW_DELTA_RATIO_MIN
        else Direction.SHORT
        if ratio <= -TICK_FLOW_STRENGTH_MIN and delta_ratio <= -TICK_FLOW_DELTA_RATIO_MIN
        else Direction.FLAT
    )
    score = (
        TICK_FLOW_SCORE_BASE
        if direction == Direction.FLAT
        else min(
            TICK_FLOW_SCORE_MAX,
            TICK_FLOW_SCORE_BASE
            + min(
                abs(ratio) * TICK_FLOW_STRENGTH_SCORE_FACTOR
                + abs(delta_ratio) * TICK_FLOW_DELTA_SCORE_FACTOR,
                TICK_FLOW_SCORE_BONUS_MAX,
            ),
        )
    )
    return {
        "source": tick_flow.get("source") or "tick_flow",
        "symbol": tick_flow.get("symbol"),
        "direction": direction.value,
        "score": round(score, 1),
        "window_bars": lookback,
        "net_delta": round(recent_delta, 2),
        "total_volume": round(recent_volume, 2),
        "delta_ratio": round(delta_ratio, 4),
        "median_abs_delta": round(baseline, 2),
        "strength": round(ratio, 3),
        "is_strong": direction != Direction.FLAT,
    }


def build_direction_sentiment(
    *,
    decision: ScenarioDecision,
    indicators: dict[str, Any],
    vsa_context: dict[str, Any] | None,
    candidates: Sequence[SignalCandidate],
    tick_bias: dict[str, Any] | None = None,
    instrument_profile: InstrumentProfile,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    edge_ctx = _evaluate_institutional_edge(
        indicators,
        vsa_context=vsa_context,
    )
    if edge_ctx:
        items.append(
            {
                "source": "institutional_edge",
                "role": "institutional_edge",
                "phase": edge_ctx["phase"],
                "direction": Direction.LONG.value
                if edge_ctx["edge"] == "LONG"
                else Direction.SHORT.value,
                "score": edge_ctx["score"],
                "weight": edge_ctx["weight"],
                "action": edge_ctx["action"],
                "code": edge_ctx["reason_code"],
                "signal_code": edge_ctx["signal_code"],
                "der_status": edge_ctx["der_status"],
            }
        )
    market_context = _market_context_from_payload(
        _market_context_payload(indicators) if isinstance(indicators, dict) else None
    )
    if decision.direction != Direction.FLAT and decision.action not in {
        ActionPhase.WAIT,
        ActionPhase.BLOCK,
    }:
        items.append(
            {
                "source": "decision",
                "direction": decision.direction.value,
                "score": round(max(0.0, min(100.0, decision.confidence)), 1),
                **contributor_weight_meta("decision", resolve_indicator_weight("decision")),
                "action": decision.action.value,
                "role": "decision",
            }
        )
    if isinstance(tick_bias, dict):
        tick_direction = direction_from_value(tick_bias.get("direction"))
        tick_score = _sentiment_score(tick_bias.get("score"))
        if tick_direction != Direction.FLAT and tick_score is not None:
            tick_weight = resolve_indicator_weight("tick_flow")
            items.append(
                {
                    "source": "tick_flow",
                    "direction": tick_direction.value,
                    "score": round(tick_score, 1),
                    **contributor_weight_meta("tick_flow", tick_weight),
                    "action": ActionPhase.WATCH.value,
                    "role": "tick_flow",
                }
            )
    for source, payload in indicators.items():
        if not isinstance(payload, dict):
            continue
        if _indicator_sentiment_role(payload) == "market_context":
            context_weight = resolve_indicator_weight(source)
            context_item = _market_context_sentiment(source, payload, context_weight)
            if context_item is not None:
                items.append(context_item)
            continue
        indicator_weight = resolve_indicator_weight(source)
        item = _indicator_sentiment_item(source, payload, indicator_weight)
        if item is not None:
            item = _gate_reversal_warning_by_context(item, market_context)
            items.append(item)
    existing_families = {_sentiment_family(str(item.get("source") or "")) for item in items}
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True)[:4]:
        if candidate.direction == Direction.FLAT:
            continue
        candidate_source = candidate.source or candidate.name
        family = _sentiment_family(candidate_source)
        if family in existing_families:
            continue
        existing_families.add(family)
        candidate_weight = resolve_candidate_weight(
            candidate.name, profile_key=instrument_profile.key
        )
        items.append(
            _gate_reversal_warning_by_context(
                {
                    "source": candidate_source,
                    "direction": candidate.direction.value,
                    "score": round(max(0.0, min(100.0, candidate.score)), 1),
                    **contributor_weight_meta(candidate.name, candidate_weight),
                    "action": ActionPhase.CANDIDATE.value,
                    "role": "candidate",
                },
                market_context,
            )
        )
    admitted_items: list[dict[str, Any]] = []
    long_power = 0.0
    short_power = 0.0
    total_weight = 0.0
    for item in items:
        score = _sentiment_score(item.get("score"))
        weight = exact_finite_number_or_none(item.get("weight"))
        if score is None or weight is None or weight < 0.0:
            continue
        admitted_item = {**item, "score": round(score, 1), "weight": weight}
        admitted_items.append(admitted_item)
        contribution = weight * (score / 100.0)
        total_weight += weight
        if item["direction"] == Direction.LONG.value:
            long_power += contribution
        elif item["direction"] == Direction.SHORT.value:
            short_power += contribution
    items = admitted_items
    score = 50.0 if total_weight <= 0 else 50.0 + ((long_power - short_power) / total_weight) * 50.0
    score = max(0.0, min(100.0, score))
    direction = "long" if score > 52.0 else "short" if score < 48.0 else "flat"
    payload = {
        "score": round(score, 1),
        "direction": direction,
        "long_power": round(long_power, 3),
        "short_power": round(short_power, 3),
        "contributors": sorted(
            items,
            key=lambda item: item["weight"] * item["score"],
            reverse=True,
        )[:12],
    }
    if edge_ctx:
        payload["institutional_edge"] = {
            "action": edge_ctx.get("action"),
            "phase": edge_ctx.get("phase"),
            "direction": str(edge_ctx.get("edge") or "").lower(),
            "reason_code": edge_ctx.get("reason_code"),
            "signal_code": edge_ctx.get("signal_code"),
            "der_status": edge_ctx.get("der_status"),
        }
    return payload


def _sentiment_family(source: str) -> str:
    return str(source or "").strip().lower()
