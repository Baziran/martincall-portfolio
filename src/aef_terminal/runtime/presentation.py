from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aef_terminal.domain import (
    ActionPhase,
    Direction,
    DomainFact,
    ScenarioDecision,
    normalize_phase,
)
from aef_terminal.signals.trade_plan import plan_geometry_valid


class ActionTier(str, Enum):
    COMMAND = "command"
    PLAN = "plan"
    CONTEXT = "context"


def trade_plan_rejection_text(rejection: DomainFact | None) -> str:
    if rejection is None:
        return ""
    values = rejection.attributes
    if rejection.code == "stop_through_fill":
        return f"stop {values['stop']:g} through fill {values['fill_price']:g}"
    if rejection.code == "target_through_fill":
        return f"target {values['target']:g} through fill {values['fill_price']:g}"
    if rejection.code == "execution_rr_below_minimum":
        return f"RR {values['rr']:.2f} below min {values['minimum']:.2f} at fill"
    return {
        "flat_direction": "flat direction",
        "incomplete_trade_plan": "incomplete trade plan",
        "incoherent_trade_plan_at_fill": "trade plan incoherent at execution price",
        "invalid_minimum_reward_risk": "invalid minimum reward/risk",
        "invalid_trade_plan_risk": "invalid trade plan risk",
        "unrealistic_stop_distance_at_fill": "unrealistic stop distance at fill",
    }[rejection.code]


PHASE_PRIORITIES: dict[ActionPhase, int] = {
    ActionPhase.STOP_HIT: 980,
    ActionPhase.TARGET_HIT: 960,
    ActionPhase.IN: 940,
    ActionPhase.TRAIL: 920,
    ActionPhase.GO: 900,
    ActionPhase.ARM: 820,
    ActionPhase.BLOCK: 760,
    ActionPhase.CANDIDATE: 640,
    ActionPhase.WATCH: 520,
    ActionPhase.WAIT: 220,
}


@dataclass(frozen=True)
class ActionCard:
    source: str
    phase: str
    direction: str
    setup: str
    trigger_event: DomainFact
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    supporting_facts: tuple[DomainFact, ...] = field(default_factory=tuple)
    blocking_facts: tuple[DomainFact, ...] = field(default_factory=tuple)
    metrics: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    tier: str = ActionTier.CONTEXT.value
    blocked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.trigger_event, DomainFact):
            raise TypeError("ActionCard.trigger_event must be a DomainFact")
        if any(
            not isinstance(fact, DomainFact)
            for fact in (*self.supporting_facts, *self.blocking_facts)
        ):
            raise TypeError("ActionCard facts must be DomainFact values")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "phase": self.phase,
            "direction": self.direction,
            "setup": self.setup,
            "trigger_event": self.trigger_event.as_dict(),
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "supporting_facts": [fact.as_dict() for fact in self.supporting_facts],
            "blocking_facts": [fact.as_dict() for fact in self.blocking_facts],
            "metrics": dict(self.metrics),
            "priority": self.priority,
            "tier": self.tier,
            "blocked": self.blocked,
        }


def normalize_direction(direction: Direction | str | None) -> str:
    value = direction.value if isinstance(direction, Direction) else str(direction or "").lower()
    return value if value in {"long", "short"} else Direction.FLAT.value


def build_action_card(
    *,
    source: str,
    phase: str | ActionPhase,
    direction: Direction | str | None,
    setup: str,
    trigger_event: DomainFact,
    tier: ActionTier | str = ActionTier.CONTEXT,
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    supporting_facts: list[DomainFact] | None = None,
    blocking_facts: list[DomainFact] | None = None,
    metrics: dict[str, Any] | None = None,
    priority: int | None = None,
    blocked: bool = False,
) -> ActionCard:
    normalized_phase = (
        phase if isinstance(phase, ActionPhase) else normalize_phase(phase, blocked=blocked)
    )
    return ActionCard(
        source=str(source or "indicator"),
        phase=normalized_phase.value,
        direction=normalize_direction(direction),
        setup=str(setup or ""),
        trigger_event=trigger_event,
        entry=entry,
        stop=stop,
        target=target,
        supporting_facts=tuple(supporting_facts or []),
        blocking_facts=tuple(blocking_facts or []),
        metrics=dict(metrics or {}),
        priority=int(priority if priority is not None else PHASE_PRIORITIES[normalized_phase]),
        tier=tier.value if isinstance(tier, ActionTier) else str(tier or ActionTier.CONTEXT.value),
        blocked=bool(blocked),
    )


def build_decision_action_card(
    decision: ScenarioDecision,
    *,
    data_quality: dict[str, Any] | None = None,
) -> ActionCard:
    quality = data_quality or {}
    blocked = quality.get("signals_ok") is False
    phase = normalize_phase(decision.action, blocked=blocked)
    if decision.direction == Direction.FLAT and not blocked:
        phase = ActionPhase.WAIT
    entry = decision.trigger
    stop = decision.stop if decision.stop is not None else decision.invalidation
    target = decision.target
    blocking_facts = [
        DomainFact(str(code))
        for code in decision.reason_codes
        if str(code).startswith(("missing_", "incomplete_", "plan_", "option_"))
    ]
    if blocked:
        blocking_facts.append(
            DomainFact(
                "data_quality_block",
                {"quality_status": str(quality.get("status") or "unknown")},
            )
        )
    if not plan_geometry_valid(entry, stop, target, decision.direction):
        blocked = True
        phase = ActionPhase.BLOCK
        blocking_facts.append(DomainFact("incoherent_trade_plan_geometry"))
        entry = stop = target = None
    return build_action_card(
        source=decision.source or "decision",
        phase=phase,
        direction=decision.direction,
        setup=decision.kind.value,
        tier=ActionTier.COMMAND,
        trigger_event=decision.trigger_event,
        entry=entry,
        stop=stop,
        target=target,
        supporting_facts=[
            DomainFact(code)
            for code in decision.reason_codes
            if code not in {item.code for item in blocking_facts}
        ],
        blocking_facts=blocking_facts,
        metrics={"confidence": round(decision.confidence, 1), **decision.metrics},
        blocked=blocked,
        priority=1000 if phase in {ActionPhase.STOP_HIT, ActionPhase.TARGET_HIT} else None,
    )


def build_trade_setup_action_card(setup: dict[str, Any]) -> ActionCard:
    plan = setup.get("plan") if isinstance(setup.get("plan"), dict) else {}
    blocked_codes = setup.get("blocked") if isinstance(setup.get("blocked"), list) else []
    setup_metrics = setup.get("metrics") if isinstance(setup.get("metrics"), dict) else {}
    confluence = setup.get("confluence") if isinstance(setup.get("confluence"), list) else []
    supporting = [
        DomainFact(
            "confluence_source",
            {
                "source": str(item.get("source") or ""),
                "score": item.get("score"),
                "direction": item.get("direction"),
                "level": item.get("level"),
            },
        )
        for item in confluence
        if isinstance(item, dict) and item.get("source")
    ]
    raw_trigger_event = setup.get("trigger_event")
    if not isinstance(raw_trigger_event, dict):
        raise ValueError("trade setup action card requires typed trigger_event")
    return build_action_card(
        source="trade_setup",
        phase=setup.get("action") or ("BLOCK" if blocked_codes else "WAIT"),
        direction=setup.get("side"),
        setup=setup.get("kind") or "wait",
        tier=ActionTier.COMMAND,
        trigger_event=DomainFact.from_mapping(raw_trigger_event),
        entry=plan.get("entry") if isinstance(plan, dict) else None,
        stop=plan.get("stop") if isinstance(plan, dict) else None,
        target=plan.get("target") if isinstance(plan, dict) else None,
        supporting_facts=supporting,
        blocking_facts=[
            DomainFact(
                "data_quality_block",
                {"quality_status": str(setup_metrics.get("data_quality_status") or "unknown")},
            )
            if code == "data_quality_block"
            else DomainFact(str(code))
            for code in blocked_codes
        ],
        metrics={
            "quality": setup.get("quality"),
            "rr": plan.get("rr") if isinstance(plan, dict) else None,
        },
        blocked=bool(blocked_codes),
    )
