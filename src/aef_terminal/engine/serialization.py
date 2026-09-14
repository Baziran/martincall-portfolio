from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.domain import (
    Bar,
    BarState,
    CandidateFinality,
    ScenarioDecision,
    SignalCandidate,
    domain_wire_value,
)
from aef_terminal.engine.common import (
    complete_trade_plan,
    interval_minutes,
    maybe_round,
)
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.signals.trade_plan import trade_plan_rr


def is_gap_placeholder_payload(payload: Any) -> bool:
    """Return whether a transport bar is a non-authoritative display slot."""

    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get("missing")
        or payload.get("data_gap")
        or payload.get("fill_forward")
        or str(payload.get("preview_kind") or "") == "gap_placeholder"
        or str(payload.get("source") or "") == "gap-placeholder"
    )


def serialize_bar(
    bar: Bar,
    *,
    session_reset: ProviderSessionReset | None = None,
) -> dict[str, Any]:
    # Keep the wire contract explicit: provider provenance is internal to
    # canonical admission, while the selected chart route owns UI identity.
    data: dict[str, Any] = {
        "symbol": bar.symbol,
        "ts": bar.ts.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "timeframe": bar.timeframe,
        "source": bar.source,
        "closed": bar.closed,
        "state": BarState(bar.state).value,
        "session_key": None,
    }
    if session_reset is not None and session_reset.available:
        data["session_key"] = session_reset.key_for_bar(bar)
    return data


def serialize_bars(
    bars: Sequence[Bar],
    bar_slots: ProviderBarSlotSequence | None = None,
    session_reset: ProviderSessionReset | None = None,
) -> list[dict[str, Any]]:
    """Serialize real bars on the optional provider-session display axis."""

    if bar_slots is not None:
        if not isinstance(bar_slots, ProviderBarSlotSequence):
            raise TypeError("bar_slots must be ProviderBarSlotSequence or None")
        if len(bar_slots) != len(bars):
            raise ValueError("bar_slots must align exactly with bars")
    if not bars:
        return []
    step = max(interval_minutes(bars[0].timeframe), 1)
    slots = list(bar_slots) if bar_slots is not None else []
    slot_authoritative = bool(bar_slots is not None and bar_slots.authoritative)
    slot_schedule_state = bar_slots.schedule_state if bar_slots is not None else "unknown"
    serialized: list[dict[str, Any]] = []
    last_emitted_slot: int | None = None
    for index, bar in enumerate(bars):
        current_slot = int(slots[index]) if index < len(slots) else index * step
        if last_emitted_slot is not None and current_slot <= last_emitted_slot:
            current_slot = last_emitted_slot + step
        data = serialize_bar(bar, session_reset=session_reset)
        data["bar_slot"] = int(current_slot)
        data["bar_slot_authoritative"] = slot_authoritative
        data["bar_slot_schedule_state"] = slot_schedule_state
        serialized.append(data)
        last_emitted_slot = current_slot
    return serialized


def candidate_status(candidate: SignalCandidate) -> dict[str, Any]:
    details = candidate.details if isinstance(candidate.details, Mapping) else {}
    finality = candidate.finality
    is_preview = finality is CandidateFinality.PROVISIONAL
    historical = finality in {CandidateFinality.RETAINED, CandidateFinality.EXPIRED}
    blocked_by_rr = bool(details.get("blocked_by_min_rr"))
    reason = finality.value
    if historical:
        reason = str(details.get("obsolete_reason") or finality.value)
    elif details.get("live_bar"):
        reason = "live_bar"
    elif blocked_by_rr:
        reason = "global_min_rr"
    return {
        "stage": "obsolete" if historical else ("preview" if is_preview else "execution"),
        "confirmation": finality.value,
        "finality": finality.value,
        "execution_candidate": candidate.decision_authoritative,
        "decision_eligible": candidate.decision_authoritative and not blocked_by_rr,
        "reason": reason,
    }


def serialize_candidate(
    candidate: SignalCandidate,
    *,
    price_increment: float | None = None,
) -> dict[str, Any]:
    raw_score = float(candidate.score)
    details = candidate.details if isinstance(candidate.details, Mapping) else {}
    reliability_weight = float_or_none(details.get("indicator_score_weight"))
    if reliability_weight is None:
        reliability_weight = 1.0
    reliability_weight = max(0.75, min(reliability_weight, 1.25))
    return {
        "name": candidate.name,
        "direction": candidate.direction.value,
        "score": round(candidate.score, 1),
        "raw_score": round(raw_score, 1),
        "reliability_weight": round(reliability_weight, 4),
        "final_rank_score": round(raw_score * reliability_weight, 2),
        "level": maybe_round(candidate.level, price_increment),
        "trigger_event": candidate.trigger_event.as_dict(),
        "reason_code": candidate.reason_code,
        "kind": candidate.kind.value,
        "producer_phase": (
            candidate.producer_phase.value if candidate.producer_phase is not None else None
        ),
        "status": candidate_status(candidate),
        "details": domain_wire_value(details, field_name="candidate.details"),
    }


def serialize_decision(
    decision: ScenarioDecision,
    *,
    price_increment: float | None = None,
) -> dict[str, Any]:
    effective_stop = decision.stop if decision.stop is not None else decision.invalidation
    raw_plan = {
        "trigger": maybe_round(decision.trigger, price_increment),
        "stop": maybe_round(decision.stop, price_increment),
        "target": maybe_round(decision.target, price_increment),
        "invalidation": maybe_round(decision.invalidation, price_increment),
    }
    complete_plan = complete_trade_plan(decision.trigger, effective_stop, decision.target)
    coherent_plan = (
        not complete_plan
        or trade_plan_rr(decision.direction, decision.trigger, effective_stop, decision.target)
        is not None
    )
    if not coherent_plan:
        return {
            "kind": decision.kind.value,
            "direction": decision.direction.value,
            "confidence": round(decision.confidence, 1),
            "action": "BLOCK",
            "trigger": None,
            "stop": None,
            "target": None,
            "invalidation": None,
            "source": decision.source or "decision",
            "trigger_event": decision.trigger_event.as_dict(),
            "reason_codes": [*decision.reason_codes, "incoherent_trade_plan_geometry"],
            "metrics": domain_wire_value(decision.metrics, field_name="decision.metrics"),
            "coherent": False,
            "raw_plan": raw_plan,
        }
    return {
        "kind": decision.kind.value,
        "direction": decision.direction.value,
        "confidence": round(decision.confidence, 1),
        "action": decision.action.value,
        "trigger": maybe_round(decision.trigger, price_increment),
        "stop": maybe_round(decision.stop, price_increment),
        "target": maybe_round(decision.target, price_increment),
        "invalidation": maybe_round(decision.invalidation, price_increment),
        "source": decision.source or "decision",
        "trigger_event": decision.trigger_event.as_dict(),
        "reason_codes": list(decision.reason_codes),
        "metrics": domain_wire_value(decision.metrics, field_name="decision.metrics"),
    }


def serialize_features(features: dict[str, Any], atr_value: float) -> dict[str, Any]:
    out = {
        key: round(value, 3) if isinstance(value, float) else value
        for key, value in features.items()
    }
    out["atr"] = round(atr_value, 3)
    return out
