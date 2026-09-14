from __future__ import annotations

from typing import Any

from aef_terminal.domain import (
    CANDIDATE_ENTRY_PHASES,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
    normalize_phase,
)
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, score_action
from .playbook_contract import (
    DEFAULT_PLAYBOOK_POLICY,
    PlaybookPromotionPolicy,
    PRODUCTION_LINDA_DEDUPE_CODES,
    playbook_promotion_preview,
    playbook_scenario_kind,
    playbook_signal_name,
    playbook_theory_summary,
)
from aef_terminal.signals.direction import direction_from_any
from aef_terminal.signals.indicator_state import signal_candidates_from_indicator_state
from aef_terminal.signals.trade_plan import coherent_trade_plan, float_or_none, trade_plan_quality


def _scenario_kind(value: Any) -> ScenarioKind:
    key = str(value or "").strip().lower()
    if key in {"fade", "reversal", "mean_reversion"}:
        return ScenarioKind.FADE
    if key in {"transit", "breakout", "continuation"}:
        return ScenarioKind.TRANSIT
    return ScenarioKind.WAIT


def _production_playbook_keys(candidates: list[SignalCandidate]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for candidate in candidates:
        code = str(candidate.details.get("code") or "").upper()
        if code not in PRODUCTION_LINDA_DEDUPE_CODES:
            continue
        if candidate.direction == Direction.FLAT:
            continue
        kind = candidate.kind
        if kind == ScenarioKind.WAIT:
            continue
        keys.add((candidate.direction.value.lower(), kind.value.lower()))
    return keys


def playbook_setup_promotion_trace(
    setup: dict[str, Any],
    *,
    source: str = "linda_volume",
    score_floor: float = DEFAULT_INDICATOR_SETTINGS.score.watch,
    atr_value: float | None = None,
    min_target_atr: float = 1.0,
    policy: PlaybookPromotionPolicy = DEFAULT_PLAYBOOK_POLICY,
    production_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    preview = playbook_promotion_preview(
        setup,
        policy=policy,
        score_floor=score_floor,
        production_keys=production_keys,
    )
    score = float_or_none(setup.get("score"))
    trace: dict[str, Any] = {
        "source": source,
        "promoted": False,
        "reject_reason": None,
        "action_source": "linda_playbook",
        "score_floor": round(float(score_floor), 2),
        "score": round(score, 2) if score is not None else None,
        "score_band": score_action(score) if score is not None else None,
        "action": str(setup.get("action") or "WAIT").upper(),
        "code": str(setup.get("code") or ""),
        "preview": preview,
    }
    if not preview.get("eligible"):
        reasons = preview.get("reject_reasons") or []
        trace["reject_reason"] = str(reasons[0]) if reasons else "not_eligible"
        return trace

    direction = direction_from_any(setup.get("direction"))
    trigger = float_or_none(setup.get("trigger"))
    stop = float_or_none(setup.get("stop"))
    target = float_or_none(setup.get("target"))
    if direction == Direction.FLAT:
        trace["reject_reason"] = "flat_direction"
        return trace
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


def playbook_setup_to_candidate(
    setup: dict[str, Any],
    *,
    source: str = "linda_volume",
    score_floor: float = DEFAULT_INDICATOR_SETTINGS.score.watch,
    atr_value: float | None = None,
    min_target_atr: float = 1.0,
    policy: PlaybookPromotionPolicy = DEFAULT_PLAYBOOK_POLICY,
    production_keys: set[tuple[str, str]] | None = None,
) -> SignalCandidate | None:
    trace = playbook_setup_promotion_trace(
        setup,
        source=source,
        score_floor=score_floor,
        atr_value=atr_value,
        min_target_atr=min_target_atr,
        policy=policy,
        production_keys=production_keys,
    )
    setup["candidate_promotion"] = trace
    if not trace.get("promoted"):
        return None

    direction = direction_from_any(setup.get("direction"))
    code = str(setup.get("code") or "")
    action = str(setup.get("action") or "WAIT").upper()
    producer_phase = normalize_phase(action)
    if producer_phase not in CANDIDATE_ENTRY_PHASES:
        return None
    score = float(trace["score"])
    trigger = float_or_none(setup.get("trigger"))
    stop = float_or_none(setup.get("stop"))
    target = float_or_none(setup.get("target"))
    level = trigger
    kind = _scenario_kind(playbook_scenario_kind(code))
    theory = playbook_theory_summary(code)
    raw_trigger_event = setup.get("trigger_event")
    if not isinstance(raw_trigger_event, dict):
        return None
    trigger_event = DomainFact.from_mapping(raw_trigger_event)
    trigger_event_code = trigger_event.code
    reason_code = str(
        setup.get("reason_code") or code.lower() or trigger_event_code or "linda_playbook"
    )
    quality = trade_plan_quality(direction, trigger=trigger, stop=stop, target=target)
    details = {
        **quality,
        "action": action,
        "raw_action": action,
        "code": code,
        "theory": theory,
        "role": setup.get("role"),
        "source": source,
        "action_source": "linda_playbook",
        "score_band": trace.get("score_band"),
        "playbook": True,
        "decision_eligible": True,
        "trigger": trigger,
        "stop": stop,
        "target": target,
        "ts": setup.get("ts"),
    }
    return SignalCandidate(
        name=playbook_signal_name(code),
        direction=direction,
        score=score,
        level=level,
        reason=reason_code,
        trigger_event=trigger_event,
        details=details,
        kind=kind,
        source=source,
        role=str(setup.get("role") or code or ""),
        reason_code=reason_code,
        producer_phase=producer_phase,
        finality=CandidateFinality.CONFIRMED,
    )


def signal_candidates_from_playbook_setups(
    indicator: dict[str, Any],
    *,
    source: str = "linda_volume",
    score_floor: float = DEFAULT_INDICATOR_SETTINGS.score.watch,
    atr_value: float | None = None,
    min_target_atr: float = 1.0,
    policy: PlaybookPromotionPolicy = DEFAULT_PLAYBOOK_POLICY,
    production_keys: set[tuple[str, str]] | None = None,
) -> list[SignalCandidate]:
    playbook = indicator.get("playbook_setups")
    if not isinstance(playbook, dict) or not playbook.get("active"):
        indicator["playbook_candidate_promotion"] = {
            "promoted": False,
            "reject_reason": "inactive",
            "items": [],
        }
        return []
    if not policy.decision_eligible and not policy.shadow_candidates:
        indicator["playbook_candidate_promotion"] = {
            "promoted": False,
            "reject_reason": "playbook_isolated",
            "items": [],
        }
        return []

    items = playbook.get("items")
    if not isinstance(items, list):
        indicator["playbook_candidate_promotion"] = {
            "promoted": False,
            "reject_reason": "missing_items",
            "items": [],
        }
        return []

    traces: list[dict[str, Any]] = []
    candidates: list[SignalCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = playbook_setup_to_candidate(
            item,
            source=source,
            score_floor=score_floor,
            atr_value=atr_value,
            min_target_atr=min_target_atr,
            policy=policy,
            production_keys=production_keys,
        )
        trace = item.get("candidate_promotion")
        if isinstance(trace, dict):
            traces.append(trace)
        if candidate is not None:
            candidates.append(candidate)

    indicator["playbook_candidate_promotion"] = {
        "promoted": bool(candidates),
        "reject_reason": None
        if candidates
        else (traces[-1].get("reject_reason") if traces else "no_items"),
        "items": traces,
    }
    return candidates


def signal_candidates_from_linda_indicator(
    indicator: dict[str, Any],
    *,
    source: str = "linda_volume",
    score_floor: float = DEFAULT_INDICATOR_SETTINGS.score.watch,
    atr_value: float | None = None,
    min_target_atr: float = 1.0,
    features: dict[str, Any] | None = None,
    policy: PlaybookPromotionPolicy = DEFAULT_PLAYBOOK_POLICY,
) -> list[SignalCandidate]:
    production = signal_candidates_from_indicator_state(
        indicator,
        source=source,
        score_floor=score_floor,
        atr_value=atr_value,
        min_target_atr=min_target_atr,
        features=features,
    )
    production_keys = _production_playbook_keys(production)
    playbook = signal_candidates_from_playbook_setups(
        indicator,
        source=source,
        score_floor=score_floor,
        atr_value=atr_value,
        min_target_atr=min_target_atr,
        policy=policy,
        production_keys=production_keys,
    )
    if not playbook:
        return production
    if not production:
        return playbook
    return production + playbook
