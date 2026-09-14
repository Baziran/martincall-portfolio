from __future__ import annotations

from typing import Any
from math import isclose

from aef_terminal.domain import Bar, Direction, DomainFact, ScenarioKind
from aef_terminal.runtime.math_utils import float_or_none, round_optional


def _rr_below_minimum(rr: float, minimum: float) -> bool:
    # An R-derived target may differ from its exact ratio by floating-point roundoff.
    # This tolerance does not change prices or admit a materially lower RR.
    return rr < minimum and not isclose(rr, minimum, rel_tol=1e-12, abs_tol=0.0)


def plan_stop_breached(direction: Direction, stop: float | None, bar: Bar) -> bool:
    """True when the signal bar already traded through the stop (market-fill unsafe)."""

    stop_value = float_or_none(stop)
    if stop_value is None:
        return False
    if direction == Direction.SHORT:
        return float(bar.high) >= stop_value
    if direction == Direction.LONG:
        return float(bar.low) <= stop_value
    return False


def anchored_invalidation_stop(
    direction: Direction,
    structural: float,
    bar: Bar,
    *,
    atr_pad: float = 0.0,
) -> float:
    structural_value = float_or_none(structural)
    pad_value = float_or_none(atr_pad)
    if structural_value is None or pad_value is None:
        raise ValueError("structural price and atr_pad must be finite")
    pad = max(pad_value, 0.0)
    if direction == Direction.SHORT:
        return max(structural_value, float(bar.high), float(bar.close)) + pad
    if direction == Direction.LONG:
        return min(structural_value, float(bar.low), float(bar.close)) - pad
    return structural_value


def normalize_trade_plan(
    direction: Direction,
    entry: float | None,
    stop: float | None,
    target: float | None,
    *,
    bar: Bar | None = None,
    atr_pad: float = 0.0,
    min_rr: float | None = None,
    use_market_entry: bool = True,
) -> dict[str, Any]:
    """Anchor the stop and validate the plan without inventing a replacement target.

    A passed target or inadequate reward/risk blocks admission. Only the scenario
    producer may choose a new target from its structure or explicit R policy.
    """

    entry_value = float_or_none(entry)
    stop_value = float_or_none(stop)
    target_value = float_or_none(target)
    blocked_reason = ""
    pad_value = float_or_none(atr_pad)
    if pad_value is None:
        return {
            "entry": entry_value,
            "stop": stop_value,
            "target": target_value,
            "coherent": False,
            "blocked_reason": "invalid trade plan distance",
            "rr": None,
        }
    pad = max(pad_value, 0.0)

    if entry_value is None or stop_value is None or target_value is None:
        return {
            "entry": entry_value,
            "stop": stop_value,
            "target": target_value,
            "coherent": False,
            "blocked_reason": "incomplete trade plan",
            "rr": None,
        }

    if bar is not None:
        if use_market_entry:
            entry_value = float(bar.close)
        stop_value = anchored_invalidation_stop(direction, stop_value, bar, atr_pad=pad)

    if direction == Direction.SHORT:
        if stop_value <= entry_value:
            blocked_reason = "stop not above entry"
        elif target_value >= entry_value:
            blocked_reason = "target not below entry"
    elif direction == Direction.LONG:
        if stop_value >= entry_value:
            blocked_reason = "stop not below entry"
        elif target_value <= entry_value:
            blocked_reason = "target not above entry"
    else:
        blocked_reason = "flat direction"

    coherent = coherent_trade_plan(direction, entry_value, stop_value, target_value)
    if coherent and min_rr is not None:
        rr = reward_risk(direction, entry_value, stop_value, target_value)
        min_rr_value = float_or_none(min_rr)
        if min_rr_value is None:
            blocked_reason = "invalid minimum reward/risk"
            coherent = False
        elif rr is not None and _rr_below_minimum(rr, max(min_rr_value, 0.0)):
            blocked_reason = f"RR {rr:.2f} below min {min_rr_value:.2f}"
            coherent = False

    if not coherent and not blocked_reason:
        blocked_reason = "incoherent trade plan"
    if bar is not None and coherent and plan_stop_breached(direction, stop_value, bar):
        blocked_reason = "plan stop already breached on signal bar"
        coherent = False

    rr = reward_risk(direction, entry_value, stop_value, target_value)

    return {
        "entry": round_optional(entry_value),
        "stop": round_optional(stop_value),
        "target": round_optional(target_value),
        "coherent": coherent,
        "blocked_reason": blocked_reason,
        "rr": round(rr, 4) if rr is not None else None,
    }


def plan_stop_breached_at_price(
    direction: Direction, stop: float | None, price: float | None
) -> bool:
    stop_value = float_or_none(stop)
    price_value = float_or_none(price)
    if stop_value is None or price_value is None:
        return False
    if direction == Direction.SHORT:
        return price_value >= stop_value
    if direction == Direction.LONG:
        return price_value <= stop_value
    return False


def execution_plan_rejection(
    direction: Direction,
    *,
    fill_price: float | None,
    stop: float | None,
    target: float | None,
    min_rr: float | None = None,
) -> DomainFact | None:
    """Return typed rejection facts; only presentation turns them into screen text."""

    entry_value = float_or_none(fill_price)
    stop_value = float_or_none(stop)
    target_value = float_or_none(target)
    if entry_value is None or stop_value is None or target_value is None:
        return DomainFact("incomplete_trade_plan")
    if plan_stop_breached_at_price(direction, stop_value, entry_value):
        return DomainFact("stop_through_fill", {"stop": stop_value, "fill_price": entry_value})
    if not coherent_trade_plan(direction, entry_value, stop_value, target_value):
        return DomainFact("incoherent_trade_plan_at_fill")
    rr = reward_risk(direction, entry_value, stop_value, target_value)
    if min_rr is not None:
        minimum = float_or_none(min_rr)
        if minimum is None:
            return DomainFact("invalid_minimum_reward_risk")
        if rr is not None and _rr_below_minimum(rr, max(minimum, 0.0)):
            return DomainFact("execution_rr_below_minimum", {"rr": rr, "minimum": minimum})
    risk = abs(entry_value - stop_value)
    reward = abs(target_value - entry_value)
    if risk <= 0 or reward <= 0:
        return DomainFact("invalid_trade_plan_risk")
    if risk > max(abs(entry_value) * 0.20, reward * 25.0):
        return DomainFact("unrealistic_stop_distance_at_fill", {"risk": risk, "reward": reward})
    return None


def coherent_trade_plan(
    direction: Direction,
    trigger: float | None,
    stop: float | None,
    target: float | None,
) -> bool:
    trigger_value = float_or_none(trigger)
    stop_value = float_or_none(stop)
    target_value = float_or_none(target)
    if trigger_value is None or stop_value is None or target_value is None:
        return False
    if direction == Direction.LONG:
        return stop_value < trigger_value < target_value
    if direction == Direction.SHORT:
        return target_value < trigger_value < stop_value
    return False


def plan_geometry_valid(
    entry: float | None,
    stop: float | None,
    target: float | None,
    direction: Direction,
) -> bool:
    """Return True when the plan is incomplete or geometrically valid."""

    if entry is None or stop is None or target is None:
        return True
    return coherent_trade_plan(direction, entry, stop, target)


def complete_coherent_trade_plan(
    entry: float | None,
    stop: float | None,
    target: float | None,
    direction: Direction,
) -> bool:
    return (
        entry is not None
        and stop is not None
        and target is not None
        and coherent_trade_plan(direction, entry, stop, target)
    )


def reward_risk(
    direction: Direction,
    trigger: float | None,
    stop: float | None,
    target: float | None,
) -> float | None:
    trigger_value = float_or_none(trigger)
    stop_value = float_or_none(stop)
    target_value = float_or_none(target)
    if not coherent_trade_plan(direction, trigger_value, stop_value, target_value):
        return None
    assert trigger_value is not None and stop_value is not None and target_value is not None
    risk = abs(trigger_value - stop_value)
    reward = abs(target_value - trigger_value)
    if risk <= 0:
        return None
    return reward / risk


def trade_plan_rr(
    direction: Direction,
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> float | None:
    rr = reward_risk(direction, entry, stop, target)
    return round(rr, 2) if rr is not None else None


def trade_plan_payload(
    *,
    source: str,
    direction: Direction,
    entry: float | None,
    stop: float | None,
    target: float | None,
    invalidation: float | None = None,
    trail: float | None = None,
    action: str | None = None,
    raw_action: str | None = None,
    state: str | None = None,
    score: float | None = None,
    actionable: bool = False,
    reason: str = "",
    blocked_reason: str = "",
    code: str = "",
    kind: ScenarioKind = ScenarioKind.WAIT,
) -> dict[str, Any]:
    entry_value = float_or_none(entry)
    stop_value = float_or_none(stop)
    target_value = float_or_none(target)
    invalidation_value = float_or_none(invalidation)
    trail_value = float_or_none(trail)
    complete = entry_value is not None and stop_value is not None and target_value is not None
    coherent = plan_geometry_valid(entry_value, stop_value, target_value, direction)
    risk = (
        abs(entry_value - stop_value)
        if entry_value is not None and stop_value is not None
        else None
    )
    reward = (
        abs(target_value - entry_value)
        if entry_value is not None and target_value is not None
        else None
    )
    rr = reward / risk if reward is not None and risk is not None and risk > 0 else None
    effective_actionable = bool(actionable and complete and coherent)
    payload: dict[str, Any] = {
        "source": source,
        "direction": direction.value,
        "kind": kind.value,
        "entry": round_optional(entry_value),
        "trigger": round_optional(entry_value),
        "stop": round_optional(stop_value),
        "target": round_optional(target_value),
        "invalidation": round_optional(invalidation_value),
        "trail": round_optional(trail_value),
        "complete": complete,
        "coherent": coherent,
        "actionable": effective_actionable,
        "risk": round_optional(risk),
        "reward": round_optional(reward),
        "rr": round_optional(rr, 2),
    }
    if action is not None:
        payload["action"] = action
    if raw_action is not None:
        payload["raw_action"] = raw_action
    if state is not None:
        payload["state"] = state
    if score is not None:
        payload["score"] = round(float(score), 2)
    if reason:
        payload["reason"] = reason
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    if code:
        payload["code"] = code
    return payload


def trade_plan_quality(
    direction: Direction,
    *,
    trigger: Any,
    stop: Any = None,
    target: Any = None,
    invalid: Any = None,
    min_rr: float | None = None,
) -> dict[str, Any]:
    normalized_trigger = float_or_none(trigger)
    normalized_stop = float_or_none(stop if stop is not None else invalid)
    normalized_invalid = float_or_none(invalid if invalid is not None else stop)
    normalized_target = float_or_none(target)
    rr = reward_risk(direction, normalized_trigger, normalized_stop, normalized_target)
    complete = (
        normalized_trigger is not None
        and normalized_stop is not None
        and normalized_target is not None
    )
    coherent = rr is not None
    low_rr = False
    blocked_reason = ""
    if complete and not coherent:
        blocked_reason = "incoherent trade plan"
    elif rr is not None and min_rr is not None and _rr_below_minimum(rr, max(float(min_rr), 0.0)):
        low_rr = True
        blocked_reason = f"RR {rr:.2f} below min {float(min_rr):.2f}"
    elif not complete:
        blocked_reason = "incomplete trade plan"
    return {
        "trigger": normalized_trigger,
        "stop": normalized_stop,
        "target": normalized_target,
        "invalid": normalized_invalid,
        "rr": round(rr, 4) if rr is not None else None,
        "plan_complete": complete,
        "plan_coherent": coherent,
        "low_rr": low_rr,
        "blocked_reason": blocked_reason,
    }
