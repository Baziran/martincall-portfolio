"""Presentation projection for Wisdom of Crowd typed state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.indicators.domain_facts import indicator_fact_payload
from aef_terminal.indicators.module_contract import INDICATOR_FACT_RUNTIME_FIELDS
from aef_terminal.runtime import overlays


_STATE_LABELS = {
    "no_trend": "WAIT",
    "session_warmup": "WARMUP",
    "trend_tracking": "FOLLOW",
    "extended": "NO CHASE",
    "pullback_armed": "PULLBACK",
    "reclaim_confirmed": "CONTINUE",
}


def _direction(value: object) -> Direction:
    try:
        return Direction(str(value))
    except ValueError:
        return Direction.FLAT


def build_overlays(
    bars: Sequence[Bar],
    latest: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
    *,
    labels: bool,
) -> list[dict[str, Any]]:
    if not bars or not isinstance(latest, Mapping):
        return []
    direction = _direction(latest.get("direction"))
    state_code = str(latest.get("state_code") or "no_trend")
    tone = (
        "positive"
        if direction is Direction.LONG
        else "negative"
        if direction is Direction.SHORT
        else "warning"
    )
    fact_fields = indicator_fact_payload(
        scenario=str(latest.get("scenario") or "trend_continuation"),
        setup=str(latest.get("setup") or "crowd_trend_filter"),
        trigger_event=(
            latest.get("trigger_event")
            if isinstance(latest.get("trigger_event"), Mapping)
            else {"code": "crowd_no_signal"}
        ),
        metrics=(latest.get("metrics") if isinstance(latest.get("metrics"), Mapping) else None),
    )
    items: list[dict[str, Any]] = []
    table = overlays.table(
        table_id="wisdom_of_crowd",
        title="Мудрость толпы",
        columns=(
            (
                "REGIME",
                str(latest.get("regime_code") or "flat"),
                _STATE_LABELS.get(state_code, state_code.upper()),
            ),
            ("ACTION", str(latest.get("action") or "WAIT"), int(latest.get("score") or 0)),
            (
                "TREND",
                {"kind": "fixed", "value": latest.get("ema_fast_slope_atr"), "digits": 2},
                {"kind": "fixed", "value": latest.get("adx"), "digits": 1},
            ),
            (
                "PLAN",
                {
                    "kind": "price",
                    "prefix": "E ",
                    "value": latest.get("trigger"),
                    "digits": 2,
                    "empty": "-",
                },
                {
                    "parts": (
                        {
                            "kind": "price",
                            "prefix": "S ",
                            "value": latest.get("stop"),
                            "digits": 2,
                            "empty": "-",
                        },
                        {
                            "kind": "price",
                            "prefix": "T ",
                            "value": latest.get("target"),
                            "digits": 2,
                            "empty": "-",
                        },
                    )
                },
            ),
        ),
        tone=tone,
        direction=direction,
        role="wisdom_of_crowd_table",
        fact_fields=fact_fields,
    )
    table["control_key"] = "tablePosition"
    table["retention"] = "active"
    table["layer"] = "tables"
    items.append(table)

    if labels:
        for event in events:
            if event.get("state_code") != "reclaim_confirmed":
                continue
            event_direction = _direction(event.get("direction"))
            event_bar = next(
                (bar for bar in reversed(bars) if bar.ts.isoformat() == event.get("ts")),
                None,
            )
            if event_bar is None:
                continue
            event_fact_fields = {
                field: event[field] for field in INDICATOR_FACT_RUNTIME_FIELDS if field in event
            }
            label = overlays.label(
                bar=event_bar,
                price=float(event.get("price") or event_bar.close),
                lines=("⇈" if event_direction is Direction.LONG else "⇊",),
                direction=event_direction,
                role="wisdom_of_crowd_signal",
                fact_fields={
                    **event_fact_fields,
                    "source": "wisdom_of_crowd",
                    "signal_overlay": True,
                    "state_code": "reclaim_confirmed",
                    "action": "GO",
                    "code": str(event.get("code") or "CROWD_CONTINUATION"),
                    "score": event.get("score"),
                    "entry": event.get("trigger"),
                    "stop": event.get("stop"),
                    "target": event.get("target"),
                },
            )
            label["retention"] = "history"
            label["layer"] = "signals"
            label["glyph_only"] = True
            label["no_tick"] = True
            items.append(label)
    return items


__all__ = ["build_overlays"]
