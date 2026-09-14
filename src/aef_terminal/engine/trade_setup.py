from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from aef_terminal.domain import Bar, Direction, DomainFact, ScenarioDecision, SignalCandidate
from aef_terminal.engine.common import (
    complete_trade_plan,
    maybe_round,
    round_to_tick,
)
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.presentation import (
    build_trade_setup_action_card,
)
from aef_terminal.signals.direction import opposite_direction
from aef_terminal.signals.trade_plan import (
    coherent_trade_plan,
    normalize_trade_plan,
    trade_plan_rr,
)


TRADE_SETUP_AUTHORITY_CONTRACT = "trade-setup-authority-v1"


def _trade_setup_authority_payload(
    *,
    state: str,
    ready: bool,
    provider_ids: Sequence[str],
    reason_code: str,
) -> dict[str, Any]:
    exact_provider_ids = list(
        dict.fromkeys(
            provider_id
            for raw_provider_id in provider_ids
            if (provider_id := str(raw_provider_id or "").strip())
        )
    )
    return {
        "contract": TRADE_SETUP_AUTHORITY_CONTRACT,
        "state": state,
        "ready": ready,
        "provider_id": exact_provider_ids[0] if len(exact_provider_ids) == 1 else "",
        "provider_ids": exact_provider_ids,
        "reason_code": reason_code,
    }


def trade_setup_execution_authority(
    provider_ids: Sequence[str],
    *,
    enabled: Mapping[str, bool],
    indicator_results: Mapping[str, Any],
) -> dict[str, Any]:
    exact_provider_ids = tuple(
        dict.fromkeys(
            provider_id
            for raw_provider_id in provider_ids
            if (provider_id := str(raw_provider_id or "").strip())
        )
    )
    if not exact_provider_ids:
        return _trade_setup_authority_payload(
            state="unavailable",
            ready=False,
            provider_ids=(),
            reason_code="trade_setup_authority_unavailable",
        )
    if len(exact_provider_ids) != 1:
        return _trade_setup_authority_payload(
            state="ambiguous",
            ready=False,
            provider_ids=exact_provider_ids,
            reason_code="trade_setup_authority_ambiguous",
        )

    provider_id = exact_provider_ids[0]
    if enabled.get(provider_id) is not True:
        return _trade_setup_authority_payload(
            state="disabled",
            ready=False,
            provider_ids=exact_provider_ids,
            reason_code="trade_setup_authority_disabled",
        )

    result = indicator_results.get(provider_id)
    status = result.get("status") if isinstance(result, Mapping) else None
    if not isinstance(status, Mapping):
        return _trade_setup_authority_payload(
            state="error",
            ready=False,
            provider_ids=exact_provider_ids,
            reason_code="trade_setup_authority_error",
        )
    state_code = str(status.get("state_code") or "")
    health = str(status.get("health") or "")
    if (
        not state_code
        or not health
        or state_code in {"error", "stale", "blocked_context"}
        or health in {"error", "stale"}
    ):
        return _trade_setup_authority_payload(
            state="error",
            ready=False,
            provider_ids=exact_provider_ids,
            reason_code="trade_setup_authority_error",
        )
    return _trade_setup_authority_payload(
        state="ready",
        ready=True,
        provider_ids=exact_provider_ids,
        reason_code="trade_setup_authority_ready",
    )


def blocked_trade_setup_execution_authority(
    reason_code: str,
    *,
    provider_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return _trade_setup_authority_payload(
        state="blocked",
        ready=False,
        provider_ids=provider_ids,
        reason_code=str(reason_code or "trade_setup_authority_blocked"),
    )


def trade_setup_execution_authority_ready(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    provider_id = value.get("provider_id")
    provider_ids = value.get("provider_ids")
    return (
        value.get("contract") == TRADE_SETUP_AUTHORITY_CONTRACT
        and value.get("state") == "ready"
        and value.get("ready") is True
        and isinstance(provider_id, str)
        and bool(provider_id)
        and isinstance(provider_ids, list)
        and provider_ids == [provider_id]
        and value.get("reason_code") == "trade_setup_authority_ready"
    )


def build_blocked_trade_setup_card(
    *,
    execution_authority: Mapping[str, Any],
    reason_code: str,
    watch_event: str,
    signal_source: str = "decision",
) -> dict[str, Any]:
    if (
        not isinstance(signal_source, str)
        or not signal_source
        or signal_source != signal_source.strip()
    ):
        raise ValueError("Trade Setup signal_source must be a non-empty exact string")
    authority = dict(execution_authority)
    trigger_event = DomainFact(
        reason_code,
        {
            "authority_state": str(authority.get("state") or "blocked"),
            "authority_reason_code": str(authority.get("reason_code") or reason_code),
        },
    )
    payload = {
        "ok": False,
        "action": "BLOCK",
        "side": Direction.FLAT.value,
        "kind": "wait",
        "signal_source": signal_source,
        "trigger_event": trigger_event.as_dict(),
        "quality": 0.0,
        "level": None,
        "plan": {
            "entry": None,
            "trigger": None,
            "stop": None,
            "target": None,
            "invalidation": None,
            "rr": None,
        },
        "confluence": [],
        "watch": [{"event": watch_event}],
        "blocked": [reason_code],
        "management": {"active": False},
        "position_coach": {"active": False, "mode": "wait", "directive": "wait"},
        "correction_watch": {"active": False},
        "continuation_pattern": {"active": False},
        "pullback_reentry": {"active": False},
        "metrics": {
            "quality": 0.0,
            "rr": None,
            "authority_state": str(authority.get("state") or "blocked"),
        },
        "execution_authority": authority,
    }
    payload["action_card"] = build_trade_setup_action_card(payload).as_dict()
    return payload


def _level_name(item: dict[str, Any], fallback: str) -> str:
    for key in ("name", "label", "role", "kind", "source"):
        value = item.get(key)
        if value:
            return str(value)
    return fallback


def _collect_indicator_levels(
    indicators: dict[str, Any],
    price_increment: float,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for source, payload in indicators.items():
        if not isinstance(payload, dict):
            continue
        for raw in payload.get("levels") or []:
            if not isinstance(raw, dict):
                continue
            price = float_or_none(raw.get("price", raw.get("level", raw.get("value"))))
            if price is None:
                continue
            collected.append(
                {
                    "name": _level_name(raw, source),
                    "price": round_to_tick(price, price_increment),
                    "kind": str(raw.get("kind") or raw.get("role") or "indicator"),
                    "source": source,
                    "weight": 1.0,
                }
            )
    return collected


def _candidate_level(
    candidate: SignalCandidate,
    price_increment: float,
) -> dict[str, Any] | None:
    if candidate.level is None:
        return None
    price = float_or_none(candidate.level)
    if price is None:
        return None
    return {
        "name": candidate.name,
        "price": round_to_tick(price, price_increment),
        "kind": candidate.direction.value,
        "source": "candidate",
        "weight": max(0.8, min(2.0, candidate.score / 50.0)),
    }


def _market_context(indicators: dict[str, Any]) -> dict[str, Any]:
    spotlight = (
        indicators.get("market_spotlight")
        if isinstance(indicators.get("market_spotlight"), dict)
        else {}
    )
    context = spotlight.get("market_context") if isinstance(spotlight, dict) else {}
    return context if isinstance(context, dict) else {}


def _impulse_continuation_pattern(indicators: dict[str, Any]) -> dict[str, Any]:
    impulse = (
        indicators.get("impulse_fib") if isinstance(indicators.get("impulse_fib"), dict) else {}
    )
    pattern = impulse.get("continuation_pattern") if isinstance(impulse, dict) else {}
    if not isinstance(pattern, dict) or not pattern.get("active"):
        latest = impulse.get("latest") if isinstance(impulse.get("latest"), dict) else {}
        pattern = latest.get("continuation_pattern") if isinstance(latest, dict) else {}
    return pattern if isinstance(pattern, dict) and pattern.get("active") else {"active": False}


def _market_pullback_reentry(indicators: dict[str, Any]) -> dict[str, Any]:
    context = _market_context(indicators)
    reentry = (
        context.get("pullback_reentry") if isinstance(context.get("pullback_reentry"), dict) else {}
    )
    return reentry if isinstance(reentry, dict) and reentry.get("active") else {"active": False}


def _market_entry_block(*, side: Direction, indicators: dict[str, Any]) -> str:
    if side not in {Direction.LONG, Direction.SHORT}:
        return ""
    context = _market_context(indicators)
    if not context:
        return ""
    side_gate = context.get("side_gate") if isinstance(context.get("side_gate"), dict) else {}
    entry = context.get("entry") if isinstance(context.get("entry"), dict) else {}
    if side == Direction.LONG and side_gate.get("veto_long"):
        return "market_spotlight_veto_long"
    if side == Direction.SHORT and side_gate.get("veto_short"):
        return "market_spotlight_veto_short"
    if bool(context.get("flow_conflict")) or str(entry.get("phase") or "").lower() == "blocked":
        return "market_spotlight_flow_conflict"
    return ""


def _continuation_watch_fact(pattern: dict[str, Any], side: Direction) -> dict[str, Any] | None:
    if not pattern.get("active"):
        return None
    direction = str(pattern.get("direction") or "flat")
    if direction != side.value:
        return None
    return {
        "event": "continuation_pattern_watch",
        "direction": direction,
        "pattern_type": str(pattern.get("type") or ""),
        "structure_kind": str(pattern.get("structure_kind") or ""),
        "phase": str(pattern.get("phase") or "watch").lower(),
        "quality": pattern.get("quality"),
        "metrics": {
            "break_level": pattern.get("break_level"),
            "invalidation": pattern.get("invalidation"),
            "target_1": pattern.get("target_1"),
            "target_2": pattern.get("target_2"),
        },
    }


def _pullback_reentry_watch_fact(reentry: dict[str, Any], side: Direction) -> dict[str, Any] | None:
    if not reentry.get("active"):
        return None
    direction = str(reentry.get("direction") or "flat")
    return {
        "event": "pullback_reentry_aligned"
        if direction == side.value
        else "pullback_reentry_opposite",
        "direction": direction,
        "current_side": side.value,
        "metrics": {
            "trigger": reentry.get("trigger"),
            "stop_reference": reentry.get("stop_reference"),
        },
    }


def _profit_protection_context(
    *,
    side: Direction,
    candidates: Sequence[SignalCandidate],
    indicators: dict[str, Any],
) -> dict[str, Any]:
    if side not in {Direction.LONG, Direction.SHORT}:
        return {"active": False}
    context = _market_context(indicators)
    if not context:
        return {"active": False}
    flow_state = context.get("flow_state") if isinstance(context.get("flow_state"), dict) else {}
    side_gate = context.get("side_gate") if isinstance(context.get("side_gate"), dict) else {}
    side_lock = str(context.get("side_lock") or "").upper()
    lock_aligned = (side == Direction.LONG and side_lock == "LONG") or (
        side == Direction.SHORT and side_lock == "SHORT"
    )
    context_aligned = str(context.get("direction") or "flat").lower() == side.value
    opposite = Direction.SHORT if side == Direction.LONG else Direction.LONG
    opposite_candidates = [
        candidate
        for candidate in candidates
        if candidate.direction == opposite and candidate.score >= 70.0
    ]
    has_opposite_pressure = bool(opposite_candidates)
    veto_opposite = bool(
        side_gate.get("veto_short") if opposite == Direction.SHORT else side_gate.get("veto_long")
    )
    protect = bool(
        (lock_aligned or context_aligned)
        and (
            has_opposite_pressure
            or flow_state.get("slowdown")
            or flow_state.get("chase_risk")
            or str(flow_state.get("directive") or "") in {"avoid_short", "avoid_long"}
        )
    )
    if not protect:
        return {"active": False}
    top = sorted(opposite_candidates, key=lambda item: item.score, reverse=True)[:3]
    reason_code = "opposite_context_pressure"
    if top:
        reason_code = "opposite_candidate_pressure"
    elif flow_state.get("slowdown"):
        reason_code = "market_slowdown"
    elif flow_state.get("chase_risk"):
        reason_code = "late_trend_chase_risk"
    return {
        "active": True,
        "mode": "profit_protection",
        "side": side.value,
        "opposite_side": opposite.value,
        "reason_code": reason_code,
        "veto_opposite": veto_opposite,
        "opposite": [
            {
                "source": item.name,
                "score": round(item.score, 1),
                "level": maybe_round(item.level),
                "kind": item.kind.value,
            }
            for item in top
        ],
        "directive": "trail_profit_do_not_add"
        if veto_opposite
        else "protect_profit_wait_confirmation",
    }


def _opposite_candidates(
    *,
    side: Direction,
    candidates: Sequence[SignalCandidate],
    min_score: float,
) -> list[SignalCandidate]:
    if side not in {Direction.LONG, Direction.SHORT}:
        return []
    opposite = opposite_direction(side)
    return sorted(
        [
            candidate
            for candidate in candidates
            if candidate.direction == opposite and candidate.score >= min_score
        ],
        key=lambda item: item.score,
        reverse=True,
    )


def _correction_watch_context(
    *,
    side: Direction,
    candidates: Sequence[SignalCandidate],
    indicators: dict[str, Any],
) -> dict[str, Any]:
    if side not in {Direction.LONG, Direction.SHORT}:
        return {"active": False}
    context = _market_context(indicators)
    flow_state = context.get("flow_state") if isinstance(context.get("flow_state"), dict) else {}
    side_lock = str(context.get("side_lock") or "").upper()
    context_direction = str(context.get("direction") or "flat").lower()
    lock_aligned = (side == Direction.LONG and side_lock == "LONG") or (
        side == Direction.SHORT and side_lock == "SHORT"
    )
    context_aligned = context_direction == side.value
    if context and not (lock_aligned or context_aligned):
        return {"active": False}

    opposite = opposite_direction(side)
    channel_reactions = [
        candidate
        for candidate in candidates
        if candidate.role == "channel_reaction"
        and candidate.direction == opposite
        and candidate.score >= 58.0
    ]
    pressure = _opposite_candidates(side=side, candidates=candidates, min_score=70.0)
    slowdown = bool(flow_state.get("slowdown") or flow_state.get("chase_risk"))
    if not channel_reactions and not (pressure and slowdown):
        return {"active": False}

    top_manual = sorted(channel_reactions, key=lambda item: item.score, reverse=True)[:1]
    top_pressure = [item for item in pressure if item.role != "channel_reaction"][:3]
    reason_codes: list[str] = []
    if top_manual:
        details = top_manual[0].details if isinstance(top_manual[0].details, dict) else {}
        reason_codes.append(
            "manual_channel_reclaim" if details.get("is_reclaim") else "manual_channel_touch"
        )
    if slowdown:
        reason_codes.append("flow_slowdown")
    if top_pressure:
        reason_codes.append("opposite_candidate_pressure")

    severity = "confirmed" if top_manual and (top_pressure or slowdown) else "early"
    return {
        "active": True,
        "mode": "correction_watch",
        "side": side.value,
        "correction_side": opposite.value,
        "severity": severity,
        "reason_codes": reason_codes or ["countertrend_pressure"],
        "manual_channel": [
            {
                "score": round(item.score, 1),
                "level": maybe_round(item.level),
                "role": str((item.details or {}).get("level_role") or ""),
                "reclaim": bool((item.details or {}).get("is_reclaim")),
            }
            for item in top_manual
        ],
        "pressure": [
            {
                "source": item.name,
                "score": round(item.score, 1),
                "level": maybe_round(item.level),
            }
            for item in top_pressure
        ],
        "directive": "trail_or_reduce" if severity == "confirmed" else "trail_prepare",
    }


def _position_coach_context(
    *,
    side: Direction,
    setup_action: str,
    management: dict[str, Any],
    correction_watch: dict[str, Any],
    indicators: dict[str, Any],
) -> dict[str, Any]:
    if side not in {Direction.LONG, Direction.SHORT}:
        return {"active": False, "mode": "wait", "directive": "wait"}
    context = _market_context(indicators)
    flow_state = context.get("flow_state") if isinstance(context.get("flow_state"), dict) else {}
    side_gate = context.get("side_gate") if isinstance(context.get("side_gate"), dict) else {}
    opposite = opposite_direction(side)
    veto_opposite = bool(
        side_gate.get("veto_short") if opposite == Direction.SHORT else side_gate.get("veto_long")
    )
    slowdown = bool(flow_state.get("slowdown") or flow_state.get("chase_risk"))
    correction_active = bool(correction_watch.get("active"))

    if correction_active and correction_watch.get("severity") == "confirmed":
        mode = "reduce"
        directive = "protect_profit_reduce_runner"
    elif management.get("active") or correction_active or slowdown:
        mode = "trail"
        directive = "trail_core_do_not_add" if veto_opposite else "trail_core_wait_confirmation"
    else:
        mode = "hold"
        directive = "hold_core"

    return {
        "active": True,
        "mode": mode,
        "side": side.value,
        "directive": directive,
        "veto_opposite": veto_opposite,
        "setup_action": setup_action,
    }


def _cluster_trade_setup_levels(
    levels: Sequence[dict[str, Any]],
    *,
    reference: float,
    latest_price: float,
    atr_value: float,
    price_increment: float,
) -> dict[str, Any] | None:
    valid = []
    for level in levels:
        price = float_or_none(level.get("price"))
        if price is None:
            continue
        valid.append({**level, "price": round_to_tick(price, price_increment)})
    if not valid:
        return None

    tolerance = max(
        price_increment * 4.0,
        max(atr_value, price_increment) * 0.14,
    )
    valid.sort(key=lambda item: item["price"])
    clusters: list[list[dict[str, Any]]] = []
    for level in valid:
        if not clusters or abs(level["price"] - clusters[-1][-1]["price"]) > tolerance:
            clusters.append([level])
        else:
            clusters[-1].append(level)

    best: dict[str, Any] | None = None
    for cluster in clusters:
        weighted_sum = sum(item["price"] * float(item.get("weight") or 1.0) for item in cluster)
        total_weight = sum(float(item.get("weight") or 1.0) for item in cluster)
        price = round_to_tick(
            weighted_sum / max(total_weight, 0.000001),
            price_increment,
        )
        distance_to_ref = abs(price - reference)
        distance_to_price = abs(price - latest_price)
        source_count = len({str(item.get("source") or item.get("name") or "") for item in cluster})
        score = total_weight + source_count * 0.7
        if atr_value > 0:
            score += max(0.0, 1.5 - distance_to_ref / atr_value)
            score += max(0.0, 0.8 - distance_to_price / (atr_value * 2.0))
        candidate = {
            "name": cluster[0].get("name") or "confluence level",
            "price": price,
            "kind": cluster[0].get("kind") or "confluence",
            "distance_atr": round(
                distance_to_ref / max(atr_value, price_increment),
                2,
            ),
            "score": round(score, 2),
            "sources": [
                {
                    "name": str(item.get("name") or item.get("source") or "level"),
                    "source": str(item.get("source") or "level"),
                    "price": round_to_tick(
                        float(item["price"]),
                        price_increment,
                    ),
                    "kind": str(item.get("kind") or "level"),
                }
                for item in cluster[:8]
            ],
        }
        if best is None or (
            distance_to_ref,
            -candidate["score"],
            distance_to_price,
        ) < (
            abs(float(best["price"]) - reference),
            -float(best["score"]),
            abs(float(best["price"]) - latest_price),
        ):
            best = candidate
    return best


def build_trade_setup_card(
    *,
    latest: Bar,
    decision: ScenarioDecision,
    candidates: Sequence[SignalCandidate],
    indicators: dict[str, Any],
    levels: Sequence[dict[str, Any]],
    option_flow: dict[str, Any] | None,
    data_quality: dict[str, Any],
    atr_value: float,
    price_increment: float | None,
    execution_authority: Mapping[str, Any],
) -> dict[str, Any]:
    signal_source = decision.source or "decision"
    if (
        not isinstance(signal_source, str)
        or not signal_source
        or signal_source != signal_source.strip()
    ):
        raise ValueError("ScenarioDecision.source must be a non-empty exact string")
    if not trade_setup_execution_authority_ready(execution_authority):
        reason_code = str(
            execution_authority.get("reason_code") or "trade_setup_authority_unavailable"
        )
        return build_blocked_trade_setup_card(
            execution_authority=execution_authority,
            reason_code=reason_code,
            watch_event="wait_for_trade_setup_authority",
            signal_source=signal_source,
        )
    if price_increment is None:
        raw_provider_ids = execution_authority.get("provider_ids")
        provider_ids = (
            tuple(item for item in raw_provider_ids if isinstance(item, str) and item)
            if isinstance(raw_provider_ids, list)
            else ()
        )
        reason_code = "price_increment_unavailable"
        return build_blocked_trade_setup_card(
            execution_authority=blocked_trade_setup_execution_authority(
                reason_code,
                provider_ids=provider_ids,
            ),
            reason_code=reason_code,
            watch_event="wait_for_provider_price_increment",
            signal_source=signal_source,
        )
    if (
        isinstance(price_increment, bool)
        or not isinstance(price_increment, (int, float))
        or not isfinite(float(price_increment))
        or float(price_increment) <= 0
    ):
        raise ValueError("Trade Setup price_increment must be a finite positive number")
    price_increment = float(price_increment)

    side = decision.direction
    entry = float_or_none(decision.trigger)
    stop = float_or_none(decision.stop)
    target = float_or_none(decision.target)
    invalidation = float_or_none(decision.invalidation)
    setup_action = decision.action.value
    market_blocks: list[str] = []
    if (
        side in {Direction.LONG, Direction.SHORT}
        and entry is not None
        and stop is not None
        and target is not None
        and coherent_trade_plan(side, entry, stop, target)
    ):
        normalized = normalize_trade_plan(
            side,
            entry,
            stop,
            target,
            bar=latest,
            atr_pad=max(atr_value * 0.35, price_increment * 4.0),
            use_market_entry=setup_action == "GO",
        )
        if normalized.get("coherent"):
            entry = float_or_none(normalized.get("entry"))
            stop = float_or_none(normalized.get("stop"))
            target = float_or_none(normalized.get("target"))
        elif setup_action == "GO":
            market_blocks.append("plan_not_executable_at_market")
            setup_action = "ARM"
    reference = entry if entry is not None else latest.close
    gathered: list[dict[str, Any]] = [
        {
            "name": str(level.get("name") or "level"),
            "price": level.get("price"),
            "kind": str(level.get("kind") or "level"),
            "source": "core",
            "weight": 1.0,
        }
        for level in levels
    ]
    gathered.extend(_collect_indicator_levels(indicators, price_increment))
    for candidate in candidates:
        level = _candidate_level(candidate, price_increment)
        if level is not None:
            gathered.append(level)
    if entry is not None:
        gathered.append(
            {
                "name": "decision trigger",
                "price": round_to_tick(entry, price_increment),
                "kind": side.value,
                "source": "decision",
                "weight": 2.4,
            }
        )
    if invalidation is not None:
        gathered.append(
            {
                "name": "risk invalidation",
                "price": round_to_tick(invalidation, price_increment),
                "kind": "risk",
                "source": "decision",
                "weight": 1.2,
            }
        )
    if target is not None:
        gathered.append(
            {
                "name": "target",
                "price": round_to_tick(target, price_increment),
                "kind": "target",
                "source": "decision",
                "weight": 1.0,
            }
        )

    confluence_level = _cluster_trade_setup_levels(
        gathered,
        reference=reference,
        latest_price=latest.close,
        atr_value=atr_value,
        price_increment=price_increment,
    )
    top_candidates = sorted(candidates, key=lambda item: item.score, reverse=True)[:3]
    confluence: list[dict[str, Any]] = [
        {
            "source": "decision",
            "trigger_event": decision.trigger_event.as_dict(),
            "score": round(decision.confidence, 1),
        }
    ]
    confluence.extend(
        {
            "source": candidate.name,
            "direction": candidate.direction.value,
            "level": maybe_round(candidate.level, price_increment),
            "trigger_event": candidate.trigger_event.as_dict(),
            "score": round(candidate.score, 1),
        }
        for candidate in top_candidates
    )
    if confluence_level:
        confluence.append(
            {
                "source": "level_cluster",
                "source_count": len(confluence_level["sources"]),
                "distance_atr": confluence_level["distance_atr"],
                "score": confluence_level["score"],
            }
        )
    if option_flow:
        call_rate_value = option_flow.get("call_rate_per_minute")
        put_rate_value = option_flow.get("put_rate_per_minute")
        call_rate = (
            float_or_none(call_rate_value)
            if isinstance(call_rate_value, (int, float)) and not isinstance(call_rate_value, bool)
            else None
        )
        put_rate = (
            float_or_none(put_rate_value)
            if isinstance(put_rate_value, (int, float)) and not isinstance(put_rate_value, bool)
            else None
        )
        if call_rate is not None and put_rate is not None:
            confluence.append(
                {
                    "source": "option_flow",
                    "call_rate_per_minute": round(call_rate, 2),
                    "put_rate_per_minute": round(put_rate, 2),
                    "score": 0.0,
                }
            )

    blocked: list[str] = list(market_blocks)
    if data_quality.get("signals_ok") is False:
        blocked.append("data_quality_block")
    if side == Direction.FLAT:
        blocked.append("no_directional_decision")
    market_entry_block = _market_entry_block(side=side, indicators=indicators)
    if market_entry_block and setup_action in {"GO", "ARM"}:
        blocked.append(market_entry_block)
        setup_action = "BLOCK"
    complete_plan = complete_trade_plan(entry, stop, target)
    rr = trade_plan_rr(side, entry, stop, target)
    coherent_plan = not complete_plan or rr is not None
    if not complete_plan:
        blocked.append("incomplete_trade_plan_geometry")
    elif not coherent_plan:
        blocked.append("incoherent_trade_plan_geometry")

    watch: list[dict[str, Any]] = []
    if side == Direction.LONG:
        watch = [
            {"event": "acceptance_above_trigger", "direction": "long"},
            {"event": "confirm_order_flow_and_absorption", "direction": "long"},
        ]
    elif side == Direction.SHORT:
        watch = [
            {"event": "rejection_below_trigger", "direction": "short"},
            {"event": "confirm_order_flow_and_absorption", "direction": "short"},
        ]
    else:
        watch = [{"event": "wait_for_directional_candidate"}]

    base_quality = max(0.0, min(100.0, decision.confidence))
    if confluence_level:
        base_quality += min(14.0, float(confluence_level["score"]) * 2.0)
    if rr is not None and rr >= 1.2:
        base_quality += min(8.0, rr * 2.0)
    if blocked:
        base_quality = min(base_quality, 45.0)

    raw_plan = {
        "entry": maybe_round(entry, price_increment),
        "trigger": maybe_round(entry, price_increment),
        "stop": maybe_round(stop, price_increment),
        "target": maybe_round(target, price_increment),
        "invalidation": maybe_round(invalidation, price_increment),
        "rr": rr,
    }
    plan = dict(raw_plan)
    if not coherent_plan:
        plan = {
            "entry": None,
            "trigger": None,
            "stop": None,
            "target": None,
            "invalidation": None,
            "rr": None,
            "raw": raw_plan,
            "coherent": False,
            "blocked_reason": "incoherent_trade_plan_geometry",
        }
    continuation_pattern = _impulse_continuation_pattern(indicators)
    pullback_reentry = _market_pullback_reentry(indicators)
    continuation_fact = _continuation_watch_fact(continuation_pattern, side)
    if continuation_fact:
        watch.insert(0, continuation_fact)
    reentry_fact = _pullback_reentry_watch_fact(pullback_reentry, side)
    if reentry_fact:
        watch.insert(0, reentry_fact)

    management = _profit_protection_context(side=side, candidates=candidates, indicators=indicators)
    correction_watch = _correction_watch_context(
        side=side, candidates=candidates, indicators=indicators
    )
    position_coach = _position_coach_context(
        side=side,
        setup_action=setup_action,
        management=management,
        correction_watch=correction_watch,
        indicators=indicators,
    )
    if correction_watch.get("active"):
        watch.insert(0, {"event": "correction_watch", "facts": correction_watch})
    if management.get("active"):
        watch.insert(0, {"event": "profit_protection", "facts": management})
        if setup_action in {"GO", "ARM"}:
            setup_action = "TRAIL"
    if position_coach.get("active") and not blocked:
        if position_coach.get("mode") in {"trail", "reduce"} and setup_action in {"GO", "ARM"}:
            setup_action = "TRAIL"
    management = {
        **management,
        "position_coach": position_coach,
        "correction_watch": correction_watch,
        "continuation_pattern": continuation_pattern,
        "pullback_reentry": pullback_reentry,
    }

    setup_payload = {
        "ok": not blocked,
        "action": setup_action,
        "side": side.value,
        "kind": decision.kind.value,
        "signal_source": signal_source,
        "trigger_event": decision.trigger_event.as_dict(),
        "quality": round(max(0.0, min(100.0, base_quality)), 1),
        "level": confluence_level,
        "plan": plan,
        "confluence": confluence[:6],
        "watch": watch,
        "blocked": blocked,
        "management": management,
        "position_coach": position_coach,
        "correction_watch": correction_watch,
        "continuation_pattern": continuation_pattern,
        "pullback_reentry": pullback_reentry,
        "metrics": {
            "quality": round(max(0.0, min(100.0, base_quality)), 1),
            "rr": rr,
            "atr": atr_value,
            "data_quality_status": str(data_quality.get("status") or "unknown"),
        },
        "execution_authority": dict(execution_authority),
    }
    action_card = build_trade_setup_action_card(setup_payload)
    setup_payload["action_card"] = action_card.as_dict()
    return setup_payload
