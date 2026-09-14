from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from aef_terminal.domain import Bar, CandidateFinality, Direction, SignalCandidate
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.signal_state import (
    LifecycleCloseEvent,
    LifecycleState,
    PlanLifecycle,
    direction_from_value,
    plan_lifecycle_from_wire,
    resolve_plan_bar_close_event,
)
from aef_terminal.signals.trade_plan import coherent_trade_plan


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _plan_value(details: Mapping[str, Any], *keys: str) -> float | None:
    trade_plan = _nested_mapping(details.get("trade_plan"))
    for key in keys:
        value = float_or_none(details.get(key))
        if value is not None:
            return value
        value = float_or_none(trade_plan.get(key))
        if value is not None:
            return value
    return None


def _candidate_plan(candidate: SignalCandidate) -> tuple[float | None, float | None, float | None]:
    details = _nested_mapping(candidate.details)
    entry = _plan_value(details, "trigger", "entry")
    if entry is None:
        entry = float_or_none(candidate.level)
    stop = _plan_value(details, "stop", "invalid", "invalidation")
    target = _plan_value(details, "target")
    return entry, stop, target


def _bars_after_signal(bars: Sequence[Bar], ts_value: Any) -> list[Bar]:
    opened_ts = parse_aware_utc_ts(ts_value)
    if opened_ts is None:
        return []
    return [bar for bar in bars if bar.ts.astimezone(opened_ts.tzinfo) > opened_ts]


def trade_plan_close_event(
    *,
    direction: Direction,
    entry: float | None,
    stop: float | None,
    target: float | None,
    bars: Sequence[Bar],
    opened_ts: Any = None,
) -> LifecycleCloseEvent | None:
    if not bars or not coherent_trade_plan(direction, entry, stop, target):
        return None
    assert stop is not None and target is not None
    scan_bars = _bars_after_signal(bars, opened_ts)
    if not scan_bars:
        return resolve_plan_bar_close_event(
            direction=direction,
            stop=stop,
            target=target,
            bar=bars[-1],
            bar_started_after_plan=False,
        )

    for bar in scan_bars:
        close_event = resolve_plan_bar_close_event(
            direction=direction,
            stop=stop,
            target=target,
            bar=bar,
        )
        if close_event is not None:
            return close_event
    return None


def annotate_candidate_lifecycle(
    candidate: SignalCandidate, bars: Sequence[Bar]
) -> SignalCandidate:
    details = _nested_mapping(candidate.details)
    entry, stop, target = _candidate_plan(candidate)
    close_event = trade_plan_close_event(
        direction=candidate.direction,
        entry=entry,
        stop=stop,
        target=target,
        bars=bars,
        opened_ts=details.get("ts") or details.get("opened_ts"),
    )
    if close_event is None:
        return candidate
    lifecycle = PlanLifecycle(
        source=candidate.name,
        state=close_event.state,
        direction=candidate.direction,
        entry=entry,
        stop=stop,
        target=target,
        opened_ts=(
            str(details.get("ts") or details.get("opened_ts"))
            if details.get("ts") or details.get("opened_ts")
            else None
        ),
        closed_ts=close_event.closed_ts,
        exit_price=close_event.exit_price,
        exit_reason=close_event.state,
    ).as_dict()
    lifecycle["obsolete"] = True
    return replace(
        candidate,
        finality=CandidateFinality.EXPIRED,
        details={
            **dict(details),
            "obsolete": True,
            "obsolete_reason": close_event.state.value.lower(),
            "lifecycle": lifecycle,
        },
    )


def annotate_signal_candidate_lifecycles(
    candidates: Sequence[SignalCandidate],
    bars: Sequence[Bar],
) -> list[SignalCandidate]:
    return [annotate_candidate_lifecycle(candidate, bars) for candidate in candidates]


def active_signal_candidates(candidates: Sequence[SignalCandidate]) -> list[SignalCandidate]:
    return [
        candidate for candidate in candidates if candidate.finality is not CandidateFinality.EXPIRED
    ]


def expire_lifecycle_payload(lifecycle: dict[str, Any], bars: Sequence[Bar]) -> dict[str, Any]:
    admitted = plan_lifecycle_from_wire(lifecycle)
    if admitted is None:
        raw_source = lifecycle.get("source")
        quarantined = PlanLifecycle(
            source=raw_source if isinstance(raw_source, str) else "lifecycle",
            state=LifecycleState.WAIT,
            direction=direction_from_value(lifecycle.get("direction")),
        ).as_dict()
        return {
            **lifecycle,
            **quarantined,
            "obsolete": True,
            "contract_invalid": True,
            "obsolete_reason": "contract_invalid",
        }
    canonical = {**lifecycle, **admitted.as_dict()}
    if not admitted.active:
        if admitted.state.terminal:
            return {
                **canonical,
                "obsolete": True,
            }
        return canonical
    if not bars:
        return canonical
    close_event = trade_plan_close_event(
        direction=admitted.direction,
        entry=admitted.entry,
        stop=admitted.stop,
        target=admitted.target,
        bars=bars,
        opened_ts=admitted.opened_ts,
    )
    if close_event is None:
        return canonical
    closed = replace(
        admitted,
        state=close_event.state,
        closed_ts=close_event.closed_ts,
        exit_price=close_event.exit_price,
        exit_reason=close_event.state,
    )
    return {
        **canonical,
        **closed.as_dict(),
        "obsolete": True,
    }


def _mark_signal_obsolete(
    latest: dict[str, Any],
    signal: dict[str, Any],
    *,
    direction: Direction,
    entry: float | None,
    stop: float | None,
    target: float | None,
    close_event: LifecycleCloseEvent | None = None,
    contract_invalid: bool = False,
) -> None:
    contract_invalid = contract_invalid or close_event is None
    reason = "contract_invalid" if contract_invalid else close_event.state.value.lower()
    raw_source = signal.get("source")
    source = raw_source if isinstance(raw_source, str) else "signal"
    if contract_invalid:
        lifecycle_payload = PlanLifecycle(
            source=source,
            state=LifecycleState.WAIT,
            direction=direction,
        ).as_dict()
        lifecycle_payload["contract_invalid"] = True
    else:
        lifecycle_payload = PlanLifecycle(
            source=source,
            state=close_event.state,
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            closed_ts=close_event.closed_ts,
            exit_price=close_event.exit_price,
            exit_reason=close_event.state,
        ).as_dict()
    lifecycle_payload["obsolete"] = True
    latest["signal"] = {
        **signal,
        "action": "WAIT",
        "raw_action": signal.get("raw_action") or signal.get("action"),
        "blocked": True,
        "blocked_reason": f"obsolete_{reason}",
        "obsolete": True,
        "lifecycle": lifecycle_payload,
    }
    latest["obsolete"] = True
    latest["obsolete_reason"] = reason


def _directional_signal(signal: Mapping[str, Any]) -> Direction:
    return direction_from_value(signal.get("direction"))


def _expire_latest_signal(latest: dict[str, Any], bars: Sequence[Bar]) -> bool:
    signal = latest.get("signal")
    if not isinstance(signal, dict):
        return False
    direction = _directional_signal(signal)
    entry = float_or_none(signal.get("trigger"))
    stop = float_or_none(signal.get("stop"))
    target = float_or_none(signal.get("target"))
    close_event = trade_plan_close_event(
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        bars=bars,
        opened_ts=signal.get("ts") or latest.get("ts"),
    )
    if close_event is None:
        return False
    _mark_signal_obsolete(
        latest,
        signal,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        close_event=close_event,
    )
    return True


def _mark_status_obsolete(indicator: dict[str, Any], latest: dict[str, Any]) -> None:
    status = indicator.get("status")
    if not isinstance(status, dict):
        return
    reason = str(latest.get("obsolete_reason") or "closed")
    status.update(
        {
            "state_code": "obsolete",
            "health": "ok",
            "reason_code": "signal_obsolete",
            "trigger_event": {
                "code": "signal_obsolete",
                "exit_reason": reason,
            },
            "has_signal": False,
            "blocked_signal": False,
            "obsolete_signal": True,
        }
    )


def expire_indicator_lifecycles(indicators: dict[str, dict[str, Any]], bars: Sequence[Bar]) -> None:
    for indicator in indicators.values():
        if not isinstance(indicator, dict):
            continue
        latest = indicator.get("latest")
        if not isinstance(latest, dict):
            continue
        signal_expired = _expire_latest_signal(latest, bars)
        lifecycle = latest.get("lifecycle")
        if isinstance(lifecycle, dict):
            expired_lifecycle = expire_lifecycle_payload(lifecycle, bars)
            latest["lifecycle"] = expired_lifecycle
            if expired_lifecycle.get("obsolete"):
                latest["obsolete"] = True
                contract_invalid = bool(expired_lifecycle.get("contract_invalid"))
                latest["obsolete_reason"] = str(
                    expired_lifecycle.get("obsolete_reason")
                    or expired_lifecycle.get("exit_reason")
                    or "closed"
                ).lower()
                signal = latest.get("signal")
                if isinstance(signal, dict) and not signal.get("obsolete"):
                    signal_direction = _directional_signal(signal)
                    lifecycle_direction = direction_from_value(expired_lifecycle.get("direction"))
                    admitted = plan_lifecycle_from_wire(expired_lifecycle)
                    terminal_state = (
                        admitted.state if admitted is not None and admitted.state.terminal else None
                    )
                    contract_invalid = bool(
                        contract_invalid
                        or terminal_state is None
                        or signal_direction == Direction.FLAT
                        or lifecycle_direction == Direction.FLAT
                        or signal_direction != lifecycle_direction
                    )
                    _mark_signal_obsolete(
                        latest,
                        signal,
                        direction=signal_direction,
                        entry=float_or_none(signal.get("trigger")),
                        stop=float_or_none(signal.get("stop")),
                        target=float_or_none(signal.get("target")),
                        close_event=(
                            LifecycleCloseEvent(
                                state=terminal_state,
                                exit_price=admitted.exit_price,
                                closed_ts=admitted.closed_ts,
                            )
                            if admitted is not None and terminal_state is not None
                            else None
                        ),
                        contract_invalid=contract_invalid,
                    )
        if signal_expired or bool(
            (latest.get("lifecycle") if isinstance(latest.get("lifecycle"), dict) else {}).get(
                "obsolete"
            )
        ):
            _mark_status_obsolete(indicator, latest)
