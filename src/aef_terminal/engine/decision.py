from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from aef_terminal.domain import (
    ActionPhase,
    Bar,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
    StrategyMode,
    domain_frozen_value,
)
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.engine.common import round_to_tick
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.signal_state import (
    LifecycleState,
    resolve_plan_bar_close_event,
)


OPTION_FLOW_TRANSIT_MIN_RVOL = 1.8
OPTION_FLOW_TRANSIT_MAX_OPPOSITE_RATE_RATIO = 1.25
PROJECTED_LEVEL_CONFIDENCE_FACTOR = 0.92

# Existing ranking policy, in score points rather than calibrated probabilities.
# Strategy multipliers prefer the matching scenario family; FADE includes reversals.
# These are heuristic policy values, not claimed backtest estimates.
DECISION_CONFIDENCE_CAP = 99.0
DECISION_ARM_SCORE = 62.0
DECISION_GO_SCORE = 78.0
MEAN_REVERSION_FADE_WEIGHT = 1.14
MEAN_REVERSION_TRANSIT_WEIGHT = 0.82
MEAN_REVERSION_WAIT_WEIGHT = 0.96
BREAKOUT_TRANSIT_WEIGHT = 1.12
BREAKOUT_FADE_WEIGHT = 0.76
BREAKOUT_WAIT_WEIGHT = 0.95


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Immutable input boundary for one deterministic decision-loop evaluation."""

    candidates: tuple[SignalCandidate, ...]
    bars: tuple[Bar, ...]
    atr_value: float
    strategy_mode: StrategyMode
    option_flow: Mapping[str, object] | None
    instrument_profile: InstrumentProfile
    price_increment: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(candidate, SignalCandidate) for candidate in self.candidates
        ):
            raise TypeError("DecisionContext.candidates must be a tuple of SignalCandidate values")
        if not isinstance(self.bars, tuple) or any(not isinstance(bar, Bar) for bar in self.bars):
            raise TypeError("DecisionContext.bars must be a tuple of Bar values")
        if any(previous.ts >= current.ts for previous, current in zip(self.bars, self.bars[1:])):
            raise ValueError("DecisionContext.bars must be strictly chronological and unique")
        if isinstance(self.atr_value, bool) or not isinstance(self.atr_value, (int, float)):
            raise TypeError("DecisionContext.atr_value must be a finite non-negative number")
        atr_value = float(self.atr_value)
        if not isfinite(atr_value) or atr_value < 0:
            raise ValueError("DecisionContext.atr_value must be a finite non-negative number")
        if not isinstance(self.strategy_mode, StrategyMode):
            raise TypeError("DecisionContext.strategy_mode must be a StrategyMode")
        if not isinstance(self.instrument_profile, InstrumentProfile):
            raise TypeError("DecisionContext.instrument_profile must be an InstrumentProfile")
        price_increment = self.price_increment
        if price_increment is not None and (
            isinstance(price_increment, bool)
            or not isinstance(price_increment, (int, float))
            or not isfinite(float(price_increment))
            or float(price_increment) <= 0
        ):
            raise ValueError("DecisionContext.price_increment must be a finite positive number")
        if self.option_flow is not None and not isinstance(self.option_flow, Mapping):
            raise TypeError("DecisionContext.option_flow must be a mapping or None")
        object.__setattr__(self, "atr_value", atr_value)
        if price_increment is not None:
            object.__setattr__(self, "price_increment", float(price_increment))
        if self.option_flow is not None:
            typed_option_flow = domain_frozen_value(self.option_flow, field_name="option_flow")
            assert isinstance(typed_option_flow, Mapping)
            object.__setattr__(self, "option_flow", typed_option_flow)


def _option_flow_number(option_flow: Mapping[str, object] | None, key: str) -> float | None:
    if not isinstance(option_flow, Mapping):
        return None
    value = option_flow.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _option_flow_transit_veto(
    direction: Direction,
    option_flow: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(option_flow, Mapping):
        return None
    rvol = _option_flow_number(option_flow, "option_rvol")
    if rvol is None:
        return {"code": "option_flow_invalid", "metrics": {"missing": "option_rvol"}}
    if rvol < OPTION_FLOW_TRANSIT_MIN_RVOL:
        return {
            "code": "option_rvol_below_minimum",
            "metrics": {
                "option_rvol": rvol,
                "minimum_option_rvol": OPTION_FLOW_TRANSIT_MIN_RVOL,
            },
        }
    if direction == Direction.LONG:
        primary = _option_flow_number(option_flow, "call_rate_per_minute")
        secondary = _option_flow_number(option_flow, "put_rate_per_minute")
        primary_name = "call_rate_per_minute"
    elif direction == Direction.SHORT:
        primary = _option_flow_number(option_flow, "put_rate_per_minute")
        secondary = _option_flow_number(option_flow, "call_rate_per_minute")
        primary_name = "put_rate_per_minute"
    else:
        return None
    if primary is None or secondary is None:
        return {
            "code": "option_flow_invalid",
            "metrics": {"missing": primary_name if primary is None else "opposite_rate_per_minute"},
        }
    if primary <= 0:
        return {
            "code": "option_primary_rate_not_positive",
            "metrics": {primary_name: primary, "option_rvol": rvol},
        }
    if secondary > primary * OPTION_FLOW_TRANSIT_MAX_OPPOSITE_RATE_RATIO:
        return {
            "code": "option_opposite_rate_dominant",
            "metrics": {
                "primary_rate_per_minute": primary,
                "opposite_rate_per_minute": secondary,
                "dominance_ratio": secondary / max(primary, 1e-9),
            },
        }
    return None


def _directional_price_ok(
    direction: Direction, trigger: float | None, value: float | None, *, target: bool
) -> bool:
    if trigger is None or value is None:
        return False
    if direction == Direction.SHORT:
        return value < trigger if target else value > trigger
    if direction == Direction.LONG:
        return value > trigger if target else value < trigger
    return False


def _complete_coherent_plan(
    direction: Direction, trigger: float | None, stop: float | None, target: float | None
) -> bool:
    if trigger is None or stop is None or target is None:
        return False
    if direction == Direction.LONG:
        return stop < trigger < target
    if direction == Direction.SHORT:
        return target < trigger < stop
    return False


def _plan_breached(
    direction: Direction,
    stop: float | None,
    latest: Bar,
    *,
    opened_ts: object = None,
) -> bool:
    if stop is None:
        return False
    opened_at = parse_aware_utc_ts(opened_ts)
    bar_started_after_plan = bool(latest.closed and opened_at is not None and latest.ts > opened_at)
    close_event = resolve_plan_bar_close_event(
        direction=direction,
        stop=stop,
        target=None,
        bar=latest,
        bar_started_after_plan=bar_started_after_plan,
    )
    return bool(
        close_event is not None
        and close_event.state in {LifecycleState.STOP_HIT, LifecycleState.TRAIL_STOP}
    )


def _strategy_weight(candidate: SignalCandidate, strategy_mode: StrategyMode) -> float:
    if candidate.kind == ScenarioKind.FADE:
        is_fade, is_breakout = True, False
    elif candidate.kind == ScenarioKind.TRANSIT:
        is_fade, is_breakout = False, True
    else:
        is_fade, is_breakout = False, False

    if strategy_mode == StrategyMode.MEAN_REVERSION:
        return (
            MEAN_REVERSION_FADE_WEIGHT
            if is_fade
            else MEAN_REVERSION_TRANSIT_WEIGHT
            if is_breakout
            else MEAN_REVERSION_WAIT_WEIGHT
        )
    if strategy_mode == StrategyMode.BREAKOUT:
        return (
            BREAKOUT_TRANSIT_WEIGHT
            if is_breakout
            else BREAKOUT_FADE_WEIGHT
            if is_fade
            else BREAKOUT_WAIT_WEIGHT
        )
    return 1.0


def _indicator_reliability_weight(candidate: SignalCandidate) -> float:
    weight = float_or_none(candidate.details.get("indicator_score_weight"))
    if weight is None:
        return 1.0
    return max(0.75, min(weight, 1.25))


def _candidate_rank_score(candidate: SignalCandidate, strategy_mode: StrategyMode) -> float:
    return (
        candidate.score
        * _strategy_weight(candidate, strategy_mode)
        * _indicator_reliability_weight(candidate)
    )


def choose_decision(context: DecisionContext) -> ScenarioDecision:
    if not isinstance(context, DecisionContext):
        raise TypeError("choose_decision requires a DecisionContext")
    candidates = tuple(
        candidate for candidate in context.candidates if candidate.decision_authoritative
    )
    bars = context.bars
    atr_value = context.atr_value
    strategy_mode = context.strategy_mode
    option_flow = context.option_flow
    instrument_profile = context.instrument_profile
    price_increment = context.price_increment
    if not candidates:
        return ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.WAIT,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[],
            source="decision",
            trigger_event=DomainFact("no_candidate"),
            reason_codes=["no_candidate"],
        )
    best = max(candidates, key=lambda item: _candidate_rank_score(item, strategy_mode))
    confidence = min(_candidate_rank_score(best, strategy_mode), DECISION_CONFIDENCE_CAP)

    # Projected levels carry slightly less confidence than confirmed levels.
    if best.details.get("is_ghost"):
        confidence *= PROJECTED_LEVEL_CONFIDENCE_FACTOR

    score_action = (
        ActionPhase.WATCH
        if confidence < DECISION_ARM_SCORE
        else ActionPhase.ARM
        if confidence < DECISION_GO_SCORE
        else ActionPhase.GO
    )
    action = score_action
    if best.producer_phase in {ActionPhase.CANDIDATE, ActionPhase.WATCH}:
        action = ActionPhase.WATCH
    elif best.producer_phase is ActionPhase.ARM and action is ActionPhase.GO:
        action = ActionPhase.ARM
    producer_phase_capped = action != score_action

    kind = best.kind

    if kind is not ScenarioKind.WAIT and price_increment is None:
        return ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.BLOCK,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[],
            source=best.source or best.name,
            trigger_event=DomainFact(
                "price_increment_unavailable",
                {"candidate": best.name},
            ),
            reason_codes=[
                best.reason_code,
                f"strategy_mode_{strategy_mode.value}",
                "price_increment_unavailable",
            ],
            metrics={"price_increment_available": False},
        )

    details_trigger = float_or_none(best.details.get("trigger"))
    details_stop = float_or_none(best.details.get("stop"))
    details_invalidation = float_or_none(best.details.get("invalid"))
    if details_stop is None:
        details_stop = details_invalidation
    details_target = float_or_none(best.details.get("target"))
    trigger = details_trigger if details_trigger is not None else best.level
    stop = None
    target = None
    invalidation = None
    if kind is not ScenarioKind.WAIT and bars and atr_value is not None:
        latest = bars[-1]
        profile = instrument_profile

        is_fade = kind == ScenarioKind.FADE
        assert price_increment is not None
        risk_pad = profile.calculate_stop_distance(
            atr_value,
            price_increment=price_increment,
        )
        prior = bars[-2] if len(bars) > 1 else latest

        if best.direction == Direction.SHORT:
            trigger = trigger if trigger is not None else latest.close
            invalidation = max(float(prior.high), float(trigger)) + risk_pad
            stop = invalidation
            target = profile.calculate_target_price(best.direction, trigger, stop, is_fade)
        elif best.direction == Direction.LONG:
            trigger = trigger if trigger is not None else latest.close
            invalidation = min(float(prior.low), float(trigger)) - risk_pad
            stop = invalidation
            target = profile.calculate_target_price(best.direction, trigger, stop, is_fade)
        if _directional_price_ok(best.direction, trigger, details_stop, target=False):
            stop = details_stop
        if _directional_price_ok(best.direction, trigger, details_invalidation, target=False):
            invalidation = details_invalidation
        if _directional_price_ok(best.direction, trigger, details_target, target=True):
            target = details_target
    if best.direction in {Direction.LONG, Direction.SHORT} and price_increment is not None:
        trigger = None if trigger is None else round_to_tick(trigger, price_increment)
        stop = None if stop is None else round_to_tick(stop, price_increment)
        target = None if target is None else round_to_tick(target, price_increment)
        invalidation = (
            None if invalidation is None else round_to_tick(invalidation, price_increment)
        )
    source = best.source or best.name
    trigger_event = best.trigger_event
    reason_codes = [
        best.reason_code,
        f"strategy_mode_{strategy_mode.value}",
    ]
    if producer_phase_capped:
        reason_codes.append("producer_phase_ceiling")
    metrics: dict[str, object] = {
        "candidate_score": float(best.score),
        "rank_score": float(confidence),
        "strategy_mode": strategy_mode.value,
        "score_action": score_action.value,
        "producer_phase": (best.producer_phase.value if best.producer_phase is not None else None),
        "price_increment": price_increment,
    }
    if kind == ScenarioKind.WAIT:
        return ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.WAIT,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[],
            source=source,
            trigger_event=trigger_event,
            reason_codes=[*reason_codes, "missing_actionable_scenario_kind"],
            metrics=metrics,
        )
    if not _complete_coherent_plan(best.direction, trigger, stop, target):
        return ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.WAIT,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[],
            source=source,
            trigger_event=trigger_event,
            reason_codes=[*reason_codes, "incomplete_or_incoherent_trade_plan"],
            metrics=metrics,
        )
    veto = (
        _option_flow_transit_veto(best.direction, option_flow)
        if kind == ScenarioKind.TRANSIT and action is ActionPhase.GO
        else None
    )
    if veto:
        reason_codes.append(str(veto.get("code") or "option_flow_veto"))
        veto_metrics = veto.get("metrics")
        if isinstance(veto_metrics, dict):
            metrics.update(veto_metrics)
        return ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.WAIT,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[],
            source=source,
            trigger_event=trigger_event,
            reason_codes=reason_codes,
            metrics=metrics,
        )
    candidate_trade_plan = best.details.get("trade_plan")
    candidate_opened_ts = best.details.get("ts")
    if candidate_opened_ts is None and isinstance(candidate_trade_plan, Mapping):
        candidate_opened_ts = candidate_trade_plan.get("opened_ts")
    if (
        bars
        and stop is not None
        and _plan_breached(
            best.direction,
            stop,
            bars[-1],
            opened_ts=candidate_opened_ts,
        )
    ):
        return ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.WAIT,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            reasons=[],
            source=source,
            trigger_event=trigger_event,
            reason_codes=[*reason_codes, "plan_stop_breached"],
            metrics={**metrics, "stop": stop},
        )
    return ScenarioDecision(
        kind=kind,
        direction=best.direction,
        confidence=confidence,
        action=action,
        trigger=trigger,
        stop=stop,
        target=target,
        invalidation=invalidation,
        reasons=[],
        source=source,
        trigger_event=trigger_event,
        reason_codes=reason_codes,
        metrics=metrics,
    )
