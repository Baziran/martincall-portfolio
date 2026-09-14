from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, ScenarioKind
from aef_terminal.indicators.defaults import ScoreDefaults
from .action import (
    LINDA_CONFIRMATION_CODES,
    linda_decision_action,
)
from aef_terminal.runtime import overlays
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.signal_state import SignalState
from aef_terminal.signals.direction import direction_from_any


LINDA_STATE_SCORE_BOOSTS: dict[str, float] = {
    "PB_UP": 8.0,
    "PB_DN": 8.0,
    "VW_RECLAIM": 4.0,
    "VW_REJECT": 4.0,
    "NO_SUPPLY": 3.0,
    "NO_DEMAND": 3.0,
}


def scenario_kind(code: str) -> ScenarioKind:
    state = str(code or "").upper()
    if state in {"PB_UP", "PB_DN"}:
        return ScenarioKind.TRANSIT
    if state in {"NO_SUPPLY", "NO_DEMAND", "VW_RECLAIM", "VW_REJECT"}:
        return ScenarioKind.FADE
    return ScenarioKind.WAIT


def vwap_signal_overlay(
    *,
    bar: Bar,
    price: float,
    direction: str,
    side: str,
    role: str,
    tone: str = "",
    fact_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    item = overlays.label(
        bar=bar,
        price=price,
        lines=[],
        direction=direction,
        side=side,
        role=role,
        fact_fields=fact_fields,
    )
    item["label_style"] = "text"
    item["pointer"] = False
    item["code"] = role.upper()
    item["raw_action"] = "WATCH"
    item["action"] = "WATCH"
    item["overlay_role"] = "vwap_signal"
    item["compact_label"] = "V2" if "_2_" in role else "VW"
    if tone:
        item["tone"] = tone
    return item


def direction_for_state(state: str) -> str:
    if state in {"PB_UP", "VW_RECLAIM", "NO_SUPPLY"}:
        return "long"
    if state in {"PB_DN", "VW_REJECT", "NO_DEMAND"}:
        return "short"
    return "flat"


def score_for_state(base_score: float, state: str, trend_bonus: bool = False) -> float:
    boost = LINDA_STATE_SCORE_BOOSTS.get(state, 0.0)
    return pine.clamp(base_score + boost + (3.0 if trend_bonus else 0.0), 0.0, 99.0)


def plan_for_latest(
    latest_item: dict[str, Any],
    bar: Bar,
    atr: float,
    flow: str,
) -> dict[str, Any]:
    _ = flow
    code = str(latest_item.get("code") or "")
    direction = str(latest_item.get("direction") or "flat")
    signal_dir = direction_from_any(direction)

    if signal_dir == Direction.FLAT:
        return {
            "action": "WAIT",
            "direction": "flat",
            "trigger": None,
            "stop": None,
            "target": None,
            "reason_code": "no_directional_edge",
        }

    is_long = signal_dir == Direction.LONG
    confirmation_style = code in LINDA_CONFIRMATION_CODES
    if confirmation_style:
        trigger = bar.high if is_long else bar.low
        action = "ARM"
        rr = 1.35
        reason_code = "await_reclaim_rejection_confirmation"
    elif code in {"PB_UP", "PB_DN"}:
        trigger = bar.close
        action = "WATCH"
        rr = 1.55
        reason_code = "continuation_pullback"
    else:
        trigger = bar.close
        action = "WATCH"
        rr = 1.25
        reason_code = "linda_flow_watch"

    stop = bar.low - atr * 0.18 if is_long else bar.high + atr * 0.18
    risk = max(abs(trigger - stop), atr * 0.18, 0.000001)
    target = trigger + risk * rr if is_long else trigger - risk * rr
    return {
        "action": action,
        "direction": signal_dir.value,
        "trigger": _round(trigger),
        "stop": _round(stop),
        "target": _round(target),
        "reason_code": reason_code,
        "blocked": False,
        "blocked_reason": "",
    }


def signal_for_item(
    item: dict[str, Any],
    bar: Bar,
    atr: float,
    min_label_score: int,
    bands: ScoreDefaults,
) -> SignalState:
    code = str(item.get("code") or "")
    kind = scenario_kind(code)
    direction = direction_from_any(str(item.get("direction") or "flat"))
    score = float(item["score"])
    if direction == Direction.FLAT:
        return SignalState(
            source="linda_volume",
            action=ActionPhase.WAIT,
            direction=Direction.FLAT,
            score=score,
            confirmed=bar.closed,
            source_tf=bar.timeframe,
            code=code,
            kind=kind,
        )

    confirmation_style = code in LINDA_CONFIRMATION_CODES
    action = linda_decision_action(
        code=code,
        score=score,
        min_label_score=min_label_score,
        bands=bands,
    )
    is_long = direction == Direction.LONG
    trigger = (
        bar.high if confirmation_style and is_long else bar.low if confirmation_style else bar.close
    )
    stop = bar.low - atr * 0.18 if is_long else bar.high + atr * 0.18
    risk = max(abs(trigger - stop), atr * 0.18, 0.000001)
    rr = 1.55 if code in {"PB_UP", "PB_DN"} else 1.35 if confirmation_style else 1.25
    target = trigger + risk * rr if is_long else trigger - risk * rr
    return SignalState(
        source="linda_volume",
        action=ActionPhase(action),
        raw_action=action,
        direction=direction,
        score=score,
        confirmed=bar.closed,
        source_tf=bar.timeframe,
        trigger=trigger,
        stop=stop,
        target=target,
        invalidation=stop,
        reason=str(item.get("reason_code") or (f"linda_{code.lower()}" if code else "linda_wait")),
        blocked_reason="",
        code=code,
        kind=kind,
    )
