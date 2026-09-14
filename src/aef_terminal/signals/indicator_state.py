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
from aef_terminal.features.volume import resolve_rvol_context
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, score_action
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.signals.candidates import scenario_kind_from_any
from aef_terminal.signals.direction import direction_from_any
from aef_terminal.signals.trade_plan import coherent_trade_plan, trade_plan_quality


def _scenario_kind(value: Any) -> ScenarioKind:
    return scenario_kind_from_any(value)


def _indicator_name(source: str, code: str, action: str) -> str:
    from aef_terminal.indicators.registry import resolve_indicator_signal_name

    return resolve_indicator_signal_name(source, code, action)


def _signal_payload(
    indicator: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    latest = indicator.get("latest")
    if not isinstance(latest, dict):
        return None, None
    signal = latest.get("signal")
    return latest, signal if isinstance(signal, dict) else None


def indicator_state_promotion_trace(
    indicator: dict[str, Any],
    *,
    source: str,
    score_floor: float = DEFAULT_INDICATOR_SETTINGS.score.watch,
    atr_value: float | None = None,
    min_target_atr: float = 1.0,
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest, signal = _signal_payload(indicator)
    trace: dict[str, Any] = {
        "source": source,
        "promoted": False,
        "reject_reason": None,
        "action_source": "indicator_state",
        "score_floor": round(float(score_floor), 2),
        "score": None,
        "score_band": None,
        "action": None,
        "producer_phase": None,
        "code": None,
        "rvol_source": None,
        "canonical_rvol": None,
    }
    if features:
        canonical_rvol, rvol_source = resolve_rvol_context(features)
        trace["canonical_rvol"] = canonical_rvol
        trace["rvol_source"] = rvol_source
    if latest is None or signal is None:
        trace["reject_reason"] = "missing_signal"
        return trace

    lifecycle = latest.get("lifecycle") if isinstance(latest.get("lifecycle"), dict) else {}
    if bool(latest.get("obsolete") or signal.get("obsolete") or lifecycle.get("obsolete")):
        trace["reject_reason"] = "obsolete_signal"
        return trace

    direction = direction_from_any(signal.get("direction"))
    action = str(signal.get("action") or "WAIT").upper()
    blocked = bool(signal.get("blocked"))
    producer_phase = normalize_phase(action, blocked=blocked)
    score = exact_finite_number_or_none(signal.get("score"))
    if score is None:
        trace["reject_reason"] = "missing_score"
        return trace
    code = str(signal.get("code") or latest.get("code") or latest.get("state") or "")
    trace.update(
        {
            "score": round(score, 2),
            "score_band": score_action(score),
            "action": action,
            "producer_phase": producer_phase.value,
            "code": code,
        }
    )
    if direction == Direction.FLAT:
        trace["reject_reason"] = "flat_direction"
        return trace
    if blocked:
        trace["reject_reason"] = "blocked_signal"
        return trace
    if producer_phase is ActionPhase.WAIT:
        trace["reject_reason"] = "wait_action"
        return trace
    if producer_phase not in CANDIDATE_ENTRY_PHASES:
        trace["reject_reason"] = "non_candidate_action"
        return trace
    if score < score_floor:
        trace["reject_reason"] = "below_score_floor"
        return trace

    trigger = exact_finite_number_or_none(signal.get("trigger"))
    stop = exact_finite_number_or_none(signal.get("stop"))
    if stop is None:
        stop = exact_finite_number_or_none(signal.get("invalidation"))
    target = exact_finite_number_or_none(signal.get("target"))
    if not coherent_trade_plan(direction, trigger, stop, target):
        trace["reject_reason"] = "incoherent_trade_plan"
        return trace
    if trigger is not None and target is not None and atr_value is not None:
        min_move = abs(float(atr_value)) * max(float(min_target_atr), 0.0)
        if min_move > 0 and abs(target - trigger) < min_move:
            trace["reject_reason"] = "target_too_close"
            return trace

    trace["promoted"] = True
    trace["reject_reason"] = None
    return trace


def signal_candidates_from_indicator_state(
    indicator: dict[str, Any],
    *,
    source: str,
    score_floor: float = DEFAULT_INDICATOR_SETTINGS.score.watch,
    atr_value: float | None = None,
    min_target_atr: float = 1.0,
    features: dict[str, Any] | None = None,
) -> list[SignalCandidate]:
    """Promote an indicator's shared SignalState into the central decision layer.

    Pine indicators often used their own tables as the final decision surface. In
    MartinCall the recommendation panel is shared, so actionable per-indicator
    SignalState records must also become ScenarioDecision candidates.
    """
    trace = indicator_state_promotion_trace(
        indicator,
        source=source,
        score_floor=score_floor,
        atr_value=atr_value,
        min_target_atr=min_target_atr,
        features=features,
    )
    indicator["candidate_promotion"] = trace
    if not trace.get("promoted"):
        return []

    latest, signal = _signal_payload(indicator)
    assert latest is not None and signal is not None
    direction = direction_from_any(signal.get("direction"))
    action = str(signal.get("action") or "WAIT").upper()
    producer_phase = normalize_phase(action, blocked=bool(signal.get("blocked")))
    score = float(trace["score"])
    trigger = exact_finite_number_or_none(signal.get("trigger"))
    stop = exact_finite_number_or_none(signal.get("stop"))
    if stop is None:
        stop = exact_finite_number_or_none(signal.get("invalidation"))
    target = exact_finite_number_or_none(signal.get("target"))
    level = trigger
    if level is None:
        level = exact_finite_number_or_none(latest.get("price"))
    if level is None:
        level = exact_finite_number_or_none(latest.get("level"))
    code = str(trace.get("code") or "")
    kind = _scenario_kind(signal.get("kind") or latest.get("kind"))
    raw_trigger_event = signal.get("trigger_event") or latest.get("trigger_event")
    if not isinstance(raw_trigger_event, dict):
        return []
    trigger_event = DomainFact.from_mapping(raw_trigger_event)
    reason_code = str(signal.get("reason_code") or latest.get("reason_code") or trigger_event.code)

    existing_details = latest.get("details")
    if not isinstance(existing_details, dict):
        existing_details = {}

    quality = trade_plan_quality(
        direction,
        trigger=trigger,
        stop=stop,
        target=target,
        invalid=signal.get("invalidation"),
    )
    signal_trade_plan = (
        signal.get("trade_plan") if isinstance(signal.get("trade_plan"), dict) else None
    )
    signal_actionable = bool(
        signal.get("signal_actionable") or (signal_trade_plan or {}).get("actionable")
    )
    details = {
        **existing_details,
        **quality,
        "action": action,
        "raw_action": signal.get("raw_action"),
        "code": code,
        "trade_plan": signal_trade_plan,
        "signal_actionable": signal_actionable,
        "severity": latest.get("severity")
        or (indicator.get("alert_lifecycle") or {}).get("severity")
        or "medium",
        "source": source,
        "ts": latest.get("ts"),
        "score_band": trace.get("score_band"),
        "rvol_source": trace.get("rvol_source"),
    }
    return [
        SignalCandidate(
            name=_indicator_name(source, code, action),
            direction=direction,
            score=score,
            level=level,
            reason=reason_code,
            trigger_event=trigger_event,
            details=details,
            kind=kind,
            source=source,
            role=str(signal.get("role") or latest.get("role") or code or ""),
            reason_code=reason_code,
            producer_phase=producer_phase,
            finality=CandidateFinality.CONFIRMED,
        )
    ]
