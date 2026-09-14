from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from aef_terminal.domain import (
    ActionPhase,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.signals.trade_plan import trade_plan_rr


def apply_global_min_rr_gate(
    candidates: Sequence[SignalCandidate],
    min_rr: float | None,
) -> tuple[list[SignalCandidate], list[SignalCandidate], dict[str, Any]]:
    threshold = exact_finite_number_or_none(min_rr)
    if threshold is None:
        threshold = 0.0
    if threshold <= 0:
        return list(candidates), list(candidates), {"enabled": False, "min_rr": None, "blocked": []}

    executable: list[SignalCandidate] = []
    annotated: list[SignalCandidate] = []
    blocked: list[dict[str, Any]] = []
    for candidate in candidates:
        details = dict(candidate.details or {})
        rr_value = exact_finite_number_or_none(details.get("rr"))
        plan_complete = bool(details.get("plan_complete"))
        plan_coherent = bool(details.get("plan_coherent"))
        if plan_complete and plan_coherent and rr_value is not None and rr_value < threshold:
            reason = f"RR {rr_value:.2f} below global min {threshold:.2f}"
            details.update(
                {
                    "blocked": True,
                    "blocked_reason": reason,
                    "blocked_by_min_rr": True,
                    "global_min_rr": threshold,
                    "rr_gate_status": "blocked",
                }
            )
            annotated_candidate = replace(candidate, details=details)
            annotated.append(annotated_candidate)
            blocked.append(
                {
                    "name": candidate.name,
                    "direction": candidate.direction.value,
                    "score": round(float(candidate.score), 2),
                    "level": candidate.level,
                    "rr": round(rr_value, 4),
                    "min_rr": threshold,
                    "reason": candidate.reason,
                }
            )
            continue
        if plan_complete and plan_coherent:
            details.update(
                {"blocked_by_min_rr": False, "global_min_rr": threshold, "rr_gate_status": "passed"}
            )
            candidate = replace(candidate, details=details)
        elif not plan_complete:
            details.update({"global_min_rr": threshold, "rr_gate_status": "pending_plan"})
            candidate = replace(candidate, details=details)
        executable.append(candidate)
        annotated.append(candidate)
    return executable, annotated, {"enabled": True, "min_rr": threshold, "blocked": blocked}


def decision_rr(decision: ScenarioDecision) -> float | None:
    effective_stop = decision.stop if decision.stop is not None else decision.invalidation
    return trade_plan_rr(decision.direction, decision.trigger, effective_stop, decision.target)


def apply_global_min_rr_to_decision(
    decision: ScenarioDecision, min_rr: float | None
) -> ScenarioDecision:
    threshold = exact_finite_number_or_none(min_rr)
    if threshold is None:
        threshold = 0.0
    if threshold <= 0 or decision.direction == Direction.FLAT:
        return decision
    rr = decision_rr(decision)
    if rr is None or rr >= threshold:
        return decision
    return ScenarioDecision(
        kind=ScenarioKind.WAIT,
        direction=Direction.FLAT,
        confidence=0.0,
        action=ActionPhase.BLOCK,
        trigger=None,
        stop=None,
        target=None,
        invalidation=None,
        reasons=[
            *decision.reasons,
            f"decision rejected: RR {rr:.2f} below global min {threshold:.2f}",
        ],
        source="risk_reward_gate",
        trigger_event=DomainFact(
            "rr_below_minimum",
            {"actual": round(rr, 4), "minimum": threshold},
        ),
        reason_codes=[*decision.reason_codes, "rr_below_minimum"],
    )
