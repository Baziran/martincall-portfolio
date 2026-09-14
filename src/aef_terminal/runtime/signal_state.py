from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, ScenarioKind
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import exact_finite_number_or_none, float_or_none
from aef_terminal.runtime.timeframes import parse_aware_utc_ts
from aef_terminal.signals.trade_plan import (
    complete_coherent_trade_plan,
    normalize_trade_plan,
    trade_plan_payload,
)


class LifecycleState(str, Enum):
    """Canonical internal state for one trade-plan lifecycle."""

    WAIT = "WAIT"
    WAIT_ENTRY = "WAIT_ENTRY"
    FOLLOW = "FOLLOW"
    RIDE = "RIDE"
    TARGET_HIT = "TP"
    STOP_HIT = "STOP"
    TARGET_AND_STOP_HIT = "TP/SL"
    TRAIL_STOP = "TRAIL"
    REVERSED = "REV"
    EXPIRED = "EXPIRED"

    @property
    def active(self) -> bool:
        return self in {
            LifecycleState.WAIT_ENTRY,
            LifecycleState.FOLLOW,
            LifecycleState.RIDE,
        }

    @property
    def terminal(self) -> bool:
        return self in {
            LifecycleState.TARGET_HIT,
            LifecycleState.STOP_HIT,
            LifecycleState.TARGET_AND_STOP_HIT,
            LifecycleState.TRAIL_STOP,
            LifecycleState.REVERSED,
            LifecycleState.EXPIRED,
        }


def lifecycle_state_from_value(value: object) -> LifecycleState | None:
    """Admit only an exact canonical lifecycle wire value."""

    if isinstance(value, LifecycleState):
        return value
    if not isinstance(value, str):
        return None
    try:
        return LifecycleState(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class LifecycleCloseEvent:
    state: LifecycleState
    exit_price: float | None
    closed_ts: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.state, LifecycleState) or not self.state.terminal:
            raise TypeError("LifecycleCloseEvent.state must be terminal")
        if self.exit_price is not None and exact_finite_number_or_none(self.exit_price) is None:
            raise TypeError("LifecycleCloseEvent.exit_price must be finite or None")
        if self.closed_ts is not None and (
            not isinstance(self.closed_ts, str) or parse_aware_utc_ts(self.closed_ts) is None
        ):
            raise TypeError("LifecycleCloseEvent.closed_ts must be an aware ISO string or None")

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "exit_reason": self.state.value,
            "exit_price": _round(self.exit_price),
            "closed_ts": self.closed_ts,
        }


@dataclass(frozen=True)
class SignalState:
    source: str
    action: ActionPhase
    direction: Direction
    score: float
    raw_action: str = "WAIT"
    confirmed: bool = True
    source_tf: str | None = None
    trigger: float | None = None
    stop: float | None = None
    target: float | None = None
    invalidation: float | None = None
    reason: str = ""
    blocked_reason: str = ""
    code: str = ""
    kind: ScenarioKind = ScenarioKind.WAIT

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("SignalState.source must be an exact non-empty string")
        if not isinstance(self.action, ActionPhase):
            raise TypeError("SignalState.action must be an ActionPhase")
        if not isinstance(self.raw_action, str):
            raise TypeError("SignalState.raw_action must be a string")
        if not isinstance(self.direction, Direction):
            raise TypeError("SignalState.direction must be a Direction")
        if not isinstance(self.kind, ScenarioKind):
            raise TypeError("SignalState.kind must be a ScenarioKind")
        if type(self.confirmed) is not bool:
            raise TypeError("SignalState.confirmed must be a boolean")
        if self.source_tf is not None and not isinstance(self.source_tf, str):
            raise TypeError("SignalState.source_tf must be a string or None")
        if exact_finite_number_or_none(self.score) is None:
            raise TypeError("SignalState.score must be a finite number")
        for value in (self.trigger, self.stop, self.target, self.invalidation):
            if value is not None and exact_finite_number_or_none(value) is None:
                raise TypeError("SignalState price fields must be finite numbers or None")
        for value in (self.reason, self.blocked_reason, self.code):
            if not isinstance(value, str):
                raise TypeError("SignalState text fields must be strings")

    @property
    def blocked(self) -> bool:
        return self.action is ActionPhase.BLOCK

    @property
    def actionable(self) -> bool:
        return (
            self.action in {ActionPhase.GO, ActionPhase.ARM, ActionPhase.WATCH}
            and self.direction != Direction.FLAT
            and complete_coherent_trade_plan(self.trigger, self.stop, self.target, self.direction)
        )

    @property
    def trade_plan(self) -> dict[str, Any]:
        return trade_plan_payload(
            source=self.source,
            direction=self.direction,
            entry=self.trigger,
            stop=self.stop,
            target=self.target,
            invalidation=self.invalidation,
            action=self.action.value,
            raw_action=self.raw_action,
            score=self.score,
            actionable=self.actionable,
            reason=self.reason,
            blocked_reason=self.blocked_reason,
            code=self.code,
            kind=self.kind,
        )

    def as_dict(self) -> dict[str, Any]:
        plan = self.trade_plan
        return {
            "source": self.source,
            "action": self.action.value,
            "raw_action": self.raw_action,
            "direction": self.direction.value,
            "kind": self.kind.value,
            "score": round(self.score, 2),
            "confirmed": self.confirmed,
            "source_tf": self.source_tf,
            "trigger": _round(self.trigger),
            "stop": _round(self.stop),
            "target": _round(self.target),
            "invalidation": _round(self.invalidation),
            "reason": self.reason,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "code": self.code,
            "plan_complete": plan.get("complete"),
            "plan_coherent": plan.get("coherent"),
            "signal_actionable": plan.get("actionable"),
            "trade_plan": plan,
        }


@dataclass(frozen=True)
class PlanLifecycle:
    source: str
    state: LifecycleState
    direction: Direction
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    trail: float | None = None
    opened_ts: str | None = None
    filled_ts: str | None = None
    closed_ts: str | None = None
    exit_price: float | None = None
    exit_reason: LifecycleState | None = None
    age_bars: int = 0
    filled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("PlanLifecycle.source must be a string")
        if not isinstance(self.state, LifecycleState):
            raise TypeError("PlanLifecycle.state must be a LifecycleState")
        if not isinstance(self.direction, Direction):
            raise TypeError("PlanLifecycle.direction must be a Direction")
        if self.direction == Direction.FLAT and self.state is not LifecycleState.WAIT:
            raise ValueError("Only WAIT PlanLifecycle may have a flat direction")
        if type(self.age_bars) is not int or self.age_bars < 0:
            raise ValueError("PlanLifecycle.age_bars must be a non-negative integer")
        if type(self.filled) is not bool:
            raise TypeError("PlanLifecycle.filled must be a boolean")
        for value in (self.entry, self.stop, self.target, self.trail, self.exit_price):
            if value is not None and exact_finite_number_or_none(value) is None:
                raise TypeError("PlanLifecycle price fields must be finite numbers or None")
        for value in (self.opened_ts, self.filled_ts, self.closed_ts):
            if value is not None and (
                not isinstance(value, str) or parse_aware_utc_ts(value) is None
            ):
                raise TypeError("PlanLifecycle timestamps must be aware ISO strings or None")
        if self.exit_reason is not None:
            if not isinstance(self.exit_reason, LifecycleState):
                raise TypeError("PlanLifecycle.exit_reason must be a LifecycleState or None")
            if not self.exit_reason.terminal or self.exit_reason is not self.state:
                raise ValueError("PlanLifecycle.exit_reason must match its terminal state")
        elif self.state.terminal:
            raise ValueError("Terminal PlanLifecycle state requires exit_reason")
        if not self.state.terminal and (self.exit_price is not None or self.closed_ts is not None):
            raise ValueError("Non-terminal PlanLifecycle cannot carry close fields")
        if self.state in {LifecycleState.WAIT, LifecycleState.WAIT_ENTRY} and self.filled:
            raise ValueError("WAIT/WAIT_ENTRY PlanLifecycle cannot be filled")
        if self.state in {LifecycleState.FOLLOW, LifecycleState.RIDE} and not self.filled:
            raise ValueError("FOLLOW/RIDE PlanLifecycle must be filled")
        if not self.filled and self.filled_ts is not None:
            raise ValueError("Unfilled PlanLifecycle cannot carry filled_ts")

    @property
    def active(self) -> bool:
        return self.state.active

    @property
    def trade_plan(self) -> dict[str, Any]:
        return trade_plan_payload(
            source=self.source,
            direction=self.direction,
            entry=self.entry,
            stop=self.stop,
            target=self.target,
            trail=self.trail,
            action=self.state.value,
            state=self.state.value,
            actionable=self.active,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "state": self.state.value,
            "direction": self.direction.value,
            "entry": _round(self.entry),
            "stop": _round(self.stop),
            "target": _round(self.target),
            "trail": _round(self.trail),
            "opened_ts": self.opened_ts,
            "filled_ts": self.filled_ts,
            "closed_ts": self.closed_ts,
            "exit_price": _round(self.exit_price),
            "exit_reason": self.exit_reason.value if self.exit_reason is not None else "",
            "age_bars": self.age_bars,
            "filled": self.filled,
            "active": self.active,
            "trade_plan": self.trade_plan,
        }


def plan_lifecycle_from_wire(value: object) -> PlanLifecycle | None:
    """Decode one canonical lifecycle payload, rejecting contradictory state."""

    if not isinstance(value, Mapping):
        return None
    raw_contract_invalid = value.get("contract_invalid", False)
    if type(raw_contract_invalid) is not bool or raw_contract_invalid:
        return None
    state = lifecycle_state_from_value(value.get("state"))
    active = value.get("active")
    if state is None or type(active) is not bool or active != state.active:
        return None

    raw_exit_reason = value.get("exit_reason")
    exit_reason = (
        None
        if raw_exit_reason is None or raw_exit_reason == ""
        else lifecycle_state_from_value(raw_exit_reason)
    )
    if state.terminal:
        if exit_reason is not state:
            return None
    elif exit_reason is not None:
        return None
    raw_obsolete = value.get("obsolete", False)
    if type(raw_obsolete) is not bool or (raw_obsolete and not state.terminal):
        return None

    raw_direction = value.get("direction", Direction.FLAT.value)
    if isinstance(raw_direction, Direction):
        direction = raw_direction
    elif isinstance(raw_direction, str):
        try:
            direction = Direction(raw_direction)
        except ValueError:
            return None
    else:
        return None
    if direction == Direction.FLAT and state is not LifecycleState.WAIT:
        return None

    numeric: dict[str, float | None] = {}
    for field in ("entry", "stop", "target", "trail", "exit_price"):
        raw = value.get(field)
        parsed = exact_finite_number_or_none(raw)
        if raw is not None and parsed is None:
            return None
        numeric[field] = parsed

    timestamps: dict[str, str | None] = {}
    for field in ("opened_ts", "filled_ts", "closed_ts"):
        raw = value.get(field)
        parsed = parse_aware_utc_ts(raw) if isinstance(raw, str) else None
        if raw is not None and parsed is None:
            return None
        timestamps[field] = parsed.isoformat() if parsed is not None else None
    if not state.terminal and (
        numeric["exit_price"] is not None or timestamps["closed_ts"] is not None
    ):
        return None

    raw_age_bars = value.get("age_bars", 0)
    if type(raw_age_bars) is not int or raw_age_bars < 0:
        return None
    raw_filled = value.get("filled", False)
    if type(raw_filled) is not bool:
        return None
    if state in {LifecycleState.WAIT, LifecycleState.WAIT_ENTRY} and raw_filled:
        return None
    if state in {LifecycleState.FOLLOW, LifecycleState.RIDE} and not raw_filled:
        return None
    if not raw_filled and timestamps["filled_ts"] is not None:
        return None
    raw_source = value.get("source", "lifecycle")
    if not isinstance(raw_source, str):
        return None

    try:
        return PlanLifecycle(
            source=raw_source,
            state=state,
            direction=direction,
            entry=numeric["entry"],
            stop=numeric["stop"],
            target=numeric["target"],
            trail=numeric["trail"],
            opened_ts=timestamps["opened_ts"],
            filled_ts=timestamps["filled_ts"],
            closed_ts=timestamps["closed_ts"],
            exit_price=numeric["exit_price"],
            exit_reason=exit_reason,
            age_bars=raw_age_bars,
            filled=raw_filled,
        )
    except TypeError, ValueError:
        return None


DEFAULT_LIFECYCLE_LABELS: dict[str, str] = {
    "breakout_accumulation": "BAR",
    "linda_volume": "Linda",
    "impulse_fib": "Impulse",
    "absorption_trap": "AbsTrap",
    "w5_structure": "W5",
    "wolfe_structure": "Wolfe",
}


def _round(value: float | None, digits: int = 4) -> float | None:
    numeric = float_or_none(value)
    return None if numeric is None else round(numeric, digits)


def direction_from_value(value: object) -> Direction:
    if isinstance(value, Direction):
        return value
    if isinstance(value, str):
        try:
            return Direction(value)
        except ValueError:
            pass
    return Direction.FLAT


def direction_from_int(value: int) -> Direction:
    if value > 0:
        return Direction.LONG
    if value < 0:
        return Direction.SHORT
    return Direction.FLAT


def action_from_thresholds(score: float, *, watch: float, go: float) -> ActionPhase:
    if score >= go:
        return ActionPhase.GO
    if score >= watch:
        return ActionPhase.WATCH
    return ActionPhase.WAIT


def plan_from_signal(
    *,
    source: str,
    bar: Bar,
    direction: Direction,
    score: float,
    watch_score: float,
    go_score: float,
    trigger: float | None,
    stop: float | None,
    target: float | None,
    code: str,
    reason: str,
    gate_ok: bool = True,
    blocked_reason: str = "",
    confirmed: bool | None = None,
    kind: ScenarioKind = ScenarioKind.WAIT,
) -> SignalState:
    raw_phase = (
        action_from_thresholds(score, watch=watch_score, go=go_score)
        if direction != Direction.FLAT
        else ActionPhase.WAIT
    )
    plan_blocked = blocked_reason
    norm_trigger = float_or_none(trigger)
    norm_stop = float_or_none(stop)
    norm_target = float_or_none(target)
    if (
        direction != Direction.FLAT
        and trigger is not None
        and stop is not None
        and target is not None
    ):
        plan = normalize_trade_plan(direction, trigger, stop, target, bar=bar)
        if plan.get("coherent"):
            norm_trigger = plan.get("entry")
            norm_stop = plan.get("stop")
            norm_target = plan.get("target")
        else:
            plan_blocked = str(
                plan.get("blocked_reason") or plan_blocked or "incoherent trade plan"
            )
            if raw_phase is ActionPhase.GO:
                raw_phase = ActionPhase.WATCH
    action = ActionPhase.BLOCK if raw_phase is ActionPhase.GO and not gate_ok else raw_phase
    if plan_blocked and raw_phase is ActionPhase.GO:
        action = ActionPhase.WATCH
    return SignalState(
        source=source,
        action=action,
        raw_action=raw_phase.value,
        direction=direction,
        score=pine.clamp(score, 0.0, 99.0),
        confirmed=bar.closed if confirmed is None else confirmed,
        source_tf=bar.timeframe,
        trigger=norm_trigger,
        stop=norm_stop,
        target=norm_target,
        invalidation=norm_stop,
        reason=reason,
        blocked_reason=plan_blocked
        if action in {ActionPhase.BLOCK, ActionPhase.WATCH} and plan_blocked
        else (blocked_reason if action is ActionPhase.BLOCK else ""),
        code=code,
        kind=kind,
    )


def _touched(bar: Bar, price: float | None) -> bool:
    price_value = float_or_none(price)
    return price_value is not None and bar.low <= price_value <= bar.high


def resolve_plan_bar_close_event(
    *,
    direction: Direction,
    stop: float | None,
    target: float | None,
    bar: Bar,
    trail: float | None = None,
    bar_started_after_plan: bool = True,
) -> LifecycleCloseEvent | None:
    """Resolve one plan terminal fact from one authoritative bar.

    The carried protective level is evaluated before any close-derived trail is
    calculated by the caller.  A bar that opens beyond a terminal level proves
    closure but not an exact fill, while a bar spanning both sides of the plan
    remains the typed ``TP/SL`` ambiguity.
    """

    if not isinstance(direction, Direction):
        raise TypeError("direction must be a Direction")
    if not isinstance(bar, Bar):
        raise TypeError("bar must be a Bar")
    if type(bar_started_after_plan) is not bool:
        raise TypeError("bar_started_after_plan must be a boolean")
    if direction is Direction.FLAT:
        return None

    stop_value = float_or_none(stop)
    target_value = float_or_none(target)
    trail_value = float_or_none(trail)
    protective_value = stop_value
    protective_state = LifecycleState.STOP_HIT
    if trail_value is not None:
        trail_is_stricter = (
            direction is Direction.LONG
            and (stop_value is None or trail_value > stop_value)
            and (target_value is None or trail_value < target_value)
        ) or (
            direction is Direction.SHORT
            and (stop_value is None or trail_value < stop_value)
            and (target_value is None or trail_value > target_value)
        )
        if trail_is_stricter:
            protective_value = trail_value
            protective_state = LifecycleState.TRAIL_STOP

    def close_event(
        state: LifecycleState,
        exit_price: float | None,
    ) -> LifecycleCloseEvent:
        return LifecycleCloseEvent(
            state=state,
            exit_price=exit_price,
            closed_ts=bar.ts.isoformat(),
        )

    if not bar_started_after_plan:
        close = float(bar.close)
        target_closed = bool(
            target_value is not None
            and (
                (direction is Direction.LONG and close >= target_value)
                or (direction is Direction.SHORT and close <= target_value)
            )
        )
        protective_closed = bool(
            protective_value is not None
            and (
                (direction is Direction.LONG and close <= protective_value)
                or (direction is Direction.SHORT and close >= protective_value)
            )
        )
        if target_closed and protective_closed:
            return close_event(LifecycleState.TARGET_AND_STOP_HIT, None)
        if protective_closed:
            return close_event(protective_state, None)
        if target_closed:
            return close_event(LifecycleState.TARGET_HIT, None)
        return None

    open_price = float(bar.open)
    target_gap = bool(
        target_value is not None
        and (
            (direction is Direction.LONG and open_price > target_value)
            or (direction is Direction.SHORT and open_price < target_value)
        )
    )
    protective_gap = bool(
        protective_value is not None
        and (
            (direction is Direction.LONG and open_price < protective_value)
            or (direction is Direction.SHORT and open_price > protective_value)
        )
    )
    if target_gap and protective_gap:
        return close_event(LifecycleState.TARGET_AND_STOP_HIT, None)
    if protective_gap:
        return close_event(protective_state, None)
    if target_gap:
        return close_event(LifecycleState.TARGET_HIT, None)

    target_touched = _touched(bar, target_value)
    protective_touched = _touched(bar, protective_value)
    if target_touched and protective_touched:
        return close_event(LifecycleState.TARGET_AND_STOP_HIT, None)
    if protective_touched:
        return close_event(protective_state, protective_value)
    if target_touched:
        return close_event(LifecycleState.TARGET_HIT, target_value)
    return None


def next_plan_trail(
    *,
    direction: Direction,
    carried_trail: float | None,
    stop: float | None,
    close: float,
    atr: float,
    trail_atr: float,
) -> float | None:
    """Return the protective trail that becomes authoritative next bar."""

    if not isinstance(direction, Direction):
        raise TypeError("direction must be a Direction")
    if direction is Direction.FLAT:
        return None
    close_value = float_or_none(close)
    atr_value = float_or_none(atr)
    multiplier = float_or_none(trail_atr)
    if close_value is None or atr_value is None or multiplier is None:
        return None
    trail_base = float_or_none(carried_trail)
    if trail_base is None:
        trail_base = float_or_none(stop)
    if trail_base is None:
        trail_base = close_value
    distance = max(atr_value, 0.0) * max(multiplier, 0.0)
    if direction is Direction.LONG:
        return max(trail_base, close_value - distance)
    return min(trail_base, close_value + distance)


def active_lifecycle_from_indicators(
    indicators: dict[str, dict[str, Any]],
    *,
    labels: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return the strongest active lifecycle published by indicator modules."""

    label_map = {**DEFAULT_LIFECYCLE_LABELS, **(labels or {})}
    active_items: list[dict[str, Any]] = []
    for source, indicator in indicators.items():
        if not isinstance(indicator, dict):
            continue
        latest = indicator.get("latest")
        if not isinstance(latest, dict):
            continue
        lifecycle = latest.get("lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        admitted = plan_lifecycle_from_wire(lifecycle)
        if admitted is None or not admitted.active:
            continue
        direction = admitted.direction
        if direction == Direction.FLAT:
            continue
        entry = admitted.entry
        stop = admitted.stop
        target = admitted.target
        if not complete_coherent_trade_plan(entry, stop, target, direction):
            continue
        signal = latest.get("signal") if isinstance(latest.get("signal"), dict) else None
        if signal is None:
            continue
        score = exact_finite_number_or_none(signal.get("score"))
        if score is None:
            continue
        canonical = replace(admitted, source=str(source)).as_dict()
        active_items.append(
            {
                **lifecycle,
                **canonical,
                "label": str(lifecycle.get("label") or label_map.get(source, source)),
                "direction": direction.value,
                "score": round(score, 2),
            }
        )
    if not active_items:
        return None
    return max(
        active_items,
        key=lambda item: (item["score"], -int(item.get("age_bars") or 0)),
    )


def lifecycle_from_signals(
    *,
    source: str,
    bars: Sequence[Bar],
    signals: Sequence[SignalState],
    atr_values: Sequence[float] | None = None,
    max_age_bars: int = 18,
    trail_atr: float = 1.1,
) -> PlanLifecycle | None:
    if not bars or not signals:
        return None

    offset = max(0, len(bars) - len(signals))
    active: dict[str, Any] | None = None
    last_closed: PlanLifecycle | None = None

    for signal_index, signal in enumerate(signals):
        bar_index = offset + signal_index
        if bar_index >= len(bars):
            break
        bar = bars[bar_index]
        atr_input = (
            float_or_none(atr_values[bar_index])
            if atr_values is not None and bar_index < len(atr_values)
            else None
        )
        atr = (
            atr_input
            if atr_input is not None and atr_input > 0
            else max(bar.high - bar.low, 0.000001)
        )

        if active is not None:
            age = bar_index - int(active["opened_index"])
            active["age_bars"] = age
            if not active["filled"] and age > max_age_bars:
                last_closed = PlanLifecycle(
                    source=source,
                    state=LifecycleState.EXPIRED,
                    direction=active["direction"],
                    entry=active["entry"],
                    stop=active["stop"],
                    target=active["target"],
                    trail=active["trail"],
                    opened_ts=active["opened_ts"],
                    closed_ts=bar.ts.isoformat(),
                    age_bars=age,
                    filled=False,
                    exit_reason=LifecycleState.EXPIRED,
                )
                active = None

        if active is not None:
            direction = active["direction"]
            entry = active["entry"]
            stop = active["stop"]
            target = active["target"]

            filled_before_bar = bool(active["filled"])
            if not filled_before_bar and age > 0 and _touched(bar, entry):
                active["filled"] = True
                active["filled_ts"] = bar.ts.isoformat()
                active["trail"] = stop

            if filled_before_bar and age > 0:
                close_event = resolve_plan_bar_close_event(
                    direction=direction,
                    stop=stop,
                    target=target,
                    trail=float_or_none(active.get("trail")),
                    bar=bar,
                )
                exit_state = close_event.state if close_event is not None else None
                exit_price = close_event.exit_price if close_event is not None else None

                opposite = signal.action in {
                    ActionPhase.GO,
                    ActionPhase.ARM,
                } and signal.direction not in {
                    Direction.FLAT,
                    direction,
                }
                if exit_state is None and opposite:
                    exit_state = LifecycleState.REVERSED
                    exit_price = bar.close

                if exit_state is not None:
                    last_closed = PlanLifecycle(
                        source=source,
                        state=exit_state,
                        direction=direction,
                        entry=entry,
                        stop=stop,
                        target=target,
                        trail=active["trail"],
                        opened_ts=active["opened_ts"],
                        filled_ts=active.get("filled_ts"),
                        closed_ts=bar.ts.isoformat(),
                        exit_price=exit_price,
                        exit_reason=exit_state,
                        age_bars=age,
                        filled=True,
                    )
                    active = None
                else:
                    active["trail"] = next_plan_trail(
                        direction=direction,
                        carried_trail=float_or_none(active.get("trail")),
                        stop=stop,
                        close=bar.close,
                        atr=atr,
                        trail_atr=trail_atr,
                    )

        if (
            active is None
            and signal.action in {ActionPhase.GO, ActionPhase.ARM}
            and signal.direction != Direction.FLAT
        ):
            entry = float_or_none(signal.trigger)
            if entry is None:
                entry = float_or_none(bar.close)
            stop = float_or_none(signal.stop)
            if stop is None:
                stop = float_or_none(signal.invalidation)
            target = float_or_none(signal.target)
            if not complete_coherent_trade_plan(entry, stop, target, signal.direction):
                continue
            filled = signal.action is ActionPhase.GO
            active = {
                "direction": signal.direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "trail": stop,
                "opened_index": bar_index,
                "opened_ts": bar.ts.isoformat(),
                "filled": filled,
                "filled_ts": bar.ts.isoformat() if filled else None,
                "age_bars": 0,
            }

    if active is not None:
        return PlanLifecycle(
            source=source,
            state=(LifecycleState.FOLLOW if active["filled"] else LifecycleState.WAIT_ENTRY),
            direction=active["direction"],
            entry=active["entry"],
            stop=active["stop"],
            target=active["target"],
            trail=active["trail"],
            opened_ts=active["opened_ts"],
            filled_ts=active.get("filled_ts"),
            age_bars=int(active["age_bars"]),
            filled=bool(active["filled"]),
        )
    return last_closed
