from __future__ import annotations

from typing import Any

from aef_terminal.domain import (
    CANDIDATE_ENTRY_PHASES,
    ActionPhase,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
    normalize_phase,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.signals.direction import direction_from_any
from aef_terminal.signals.trade_plan import coherent_trade_plan, trade_plan_quality


def scenario_kind_from_any(value: Any, default: ScenarioKind = ScenarioKind.WAIT) -> ScenarioKind:
    if isinstance(value, ScenarioKind):
        return value
    text = str(value or "").strip().lower()
    for kind in ScenarioKind:
        if text == kind.value:
            return kind
    return default


def candidate_from_trade_plan_item(
    item: dict[str, Any],
    *,
    name: str,
    score_floor: float,
    trigger_keys: tuple[str, ...] = ("trigger", "price", "level"),
    stop_keys: tuple[str, ...] = ("stop", "invalid", "invalidation"),
    target_keys: tuple[str, ...] = ("target",),
    direction_key: str = "direction",
    details: dict[str, Any] | None = None,
    kind: ScenarioKind | None = None,
    source: str = "",
    role: str = "",
    finality: CandidateFinality,
) -> SignalCandidate | None:
    producer_phase: ActionPhase | None = None
    if "action" in item:
        producer_phase = normalize_phase(item.get("action"))
        if producer_phase not in CANDIDATE_ENTRY_PHASES:
            return None
    direction = direction_from_any(item.get(direction_key))
    if direction == Direction.FLAT:
        return None
    score = exact_finite_number_or_none(item.get("score"))
    if score is None or score < score_floor:
        return None

    def first_float(keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = exact_finite_number_or_none(item.get(key))
            if value is not None:
                return value
        return None

    trigger = first_float(trigger_keys)
    stop = first_float(stop_keys)
    target = first_float(target_keys)
    if not coherent_trade_plan(direction, trigger, stop, target):
        return None
    raw_trigger_event = item.get("trigger_event")
    if not isinstance(raw_trigger_event, dict):
        return None
    trigger_event = DomainFact.from_mapping(raw_trigger_event)
    reason_code = str(item.get("reason_code") or trigger_event.code)
    quality = trade_plan_quality(
        direction,
        trigger=trigger,
        stop=stop,
        target=target,
        invalid=first_float(("invalid", "invalidation")),
    )
    payload = {**quality, **(details or {})}
    if item.get("ts") is not None:
        payload.setdefault("ts", item.get("ts"))
    return SignalCandidate(
        name=name,
        direction=direction,
        score=score,
        level=trigger,
        reason=reason_code,
        trigger_event=trigger_event,
        details=payload,
        kind=kind or scenario_kind_from_any(item.get("kind")),
        source=source,
        role=role,
        reason_code=reason_code,
        producer_phase=producer_phase,
        finality=finality,
    )
