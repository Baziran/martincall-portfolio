from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number,
)
from aef_terminal.indicators.modules.breakout_accumulation.contracts import (
    BREAKOUT_OVERLAY_HISTORY_LIMIT,
    price_on,
)
from aef_terminal.runtime import overlays as pine_overlays
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import float_or_none, round_optional as safe_round


def append_breakout_item(
    *,
    item: dict[str, Any],
    event_key: str,
    event_code: str,
    event_price: float | None,
    bar: Bar,
    series: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    if event_key in {"wide_call", "wide_put"}:
        item["tick"] = False
        item["pointer"] = False
    if event_key == "exit":
        item["render_label"] = False
    if event_key in {"barcode_active", "barcode_forming", "coil"}:
        item["range_label"] = True
        item["overlay_group"] = "range_label"
    series.append(item)
    if event_key:
        events.append(
            {
                **item,
                "symbol": bar.symbol,
                "timeframe": bar.timeframe,
                "trigger_type": "indicator_event",
                "source": "breakout_accumulation",
                "event_type": "formation",
                "code": event_code,
                "price": safe_round(event_price),
                "important": event_key not in {"coil", "barcode_forming"},
            }
        )


def breakout_event_overlays(
    bars: Sequence[Bar],
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    bar_by_ts = {bar.ts.isoformat(): bar for bar in bars}
    overlays: list[dict[str, Any]] = []
    visible_events = [event for event in events if event.get("render_label", True)]
    for event in visible_events[-BREAKOUT_OVERLAY_HISTORY_LIMIT:]:
        event_bar = bar_by_ts.get(str(event.get("ts") or "")) or bars[-1]
        price = event.get("price")
        if not price_on(price):
            continue
        direction_value = str(event.get("direction") or "").lower()
        direction = (
            Direction.LONG
            if direction_value == "long"
            else Direction.SHORT
            if direction_value == "short"
            else Direction.FLAT
        )
        item = pine_overlays.label(
            bar=event_bar,
            price=float(price),
            direction=direction,
            role="breakout_accumulation_event",
            fact_fields=copy_indicator_facts(event),
        )
        item.update(
            {
                "retention": "history",
                "event": event.get("event"),
                "event_code": event.get("event_code"),
                "code": event.get("code"),
                "compact_label": str(event.get("code") or "BAR")
                .replace("_", " ")
                .split()[0][:3]
                .upper(),
                "label_score": True,
                "raw_action": event.get("raw_action"),
                "range_label": event.get("range_label") is True,
                "overlay_group": event.get("overlay_group"),
                "no_tick": event.get("tick") is False,
                "pointer": event.get("pointer"),
            }
        )
        signal_payload = event.get("signal") if isinstance(event.get("signal"), dict) else None
        if signal_payload:
            pine_overlays.bind_signal_trade_plan(
                item, signal_payload, source="breakout_accumulation"
            )
        else:
            item["source"] = "breakout_accumulation"
            item["action_code"] = event.get("action_code")
            item["score"] = event.get("score")
        overlays.append(item)
    return overlays


def breakout_level_overlays(
    latest: dict[str, Any],
    *,
    last_bar: Bar,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    levels = [
        {
            "name": "breakout_range_upper",
            "price": latest["upper"],
            "kind": "resistance",
            "role": "upper",
        },
        {
            "name": "breakout_range_lower",
            "price": latest["lower"],
            "kind": "support",
            "role": "lower",
        },
        {"name": "breakout_range_mid", "price": latest["mid"], "kind": "fair", "role": "mid"},
        {
            "name": "breakout_call_watch",
            "price": latest["call_watch"],
            "kind": "trigger",
            "role": "call",
        },
        {
            "name": "breakout_put_watch",
            "price": latest["put_watch"],
            "kind": "trigger",
            "role": "put",
        },
    ]
    projection_bars = 28
    fact_fields = indicator_fact_payload(
        scenario="breakout_accumulation_levels",
        metrics={
            "upper": metric_number(latest.get("upper")),
            "lower": metric_number(latest.get("lower")),
            "call_watch": metric_number(latest.get("call_watch")),
            "put_watch": metric_number(latest.get("put_watch")),
        },
    )
    overlays: list[dict[str, Any]] = []
    range_start_ts = str(latest.get("range_start_ts") or "")
    if range_start_ts and price_on(latest.get("upper")) and price_on(latest.get("lower")):
        overlays.append(
            {
                "type": "box",
                "retention": "active",
                "source": "breakout_accumulation",
                "start_ts": range_start_ts,
                **pine_overlays.projected_end(last_bar, projection_bars),
                "top": safe_round(latest["upper"]),
                "bottom": safe_round(latest["lower"]),
                "tone": "warning",
                "role": "breakout_accumulation_range_box",
                "overlay_group": "range_box",
                **fact_fields,
            }
        )
    range_lines = (
        (
            latest.get("upper"),
            "High",
            "above",
            "positive",
            "dashed",
            "breakout_accumulation_range_high",
        ),
        (
            latest.get("lower"),
            "Low",
            "below",
            "negative",
            "dashed",
            "breakout_accumulation_range_low",
        ),
        (latest.get("mid"), "M", "above", "neutral", "dotted", "breakout_accumulation_range_mid"),
    )
    for price, label, label_side, tone, style, role in range_lines:
        if not price_on(price):
            continue
        item = pine_overlays.line(
            start=last_bar,
            bars_forward=projection_bars,
            price=float(price),
            label_text=label,
            tone=tone,
            width=1.0,
            style=style,
            role=role,
            label_side=label_side,
            fact_fields=fact_fields,
        )
        item.update({"overlay_group": "range_level", "range_label": True})
        item["retention"] = "active"
        overlays.append(item)
    watch_lines = (
        (
            latest.get("call_watch"),
            latest.get("call_score"),
            "CALL",
            "above",
            "positive",
            "breakout_accumulation_call_watch",
        ),
        (
            latest.get("put_watch"),
            latest.get("put_score"),
            "PUT",
            "below",
            "negative",
            "breakout_accumulation_put_watch",
        ),
    )
    for price, score, label, label_side, tone, role in watch_lines:
        if not price_on(price):
            continue
        numeric_score = float_or_none(score)
        if numeric_score is None:
            continue
        item = pine_overlays.line(
            start=last_bar,
            bars_forward=projection_bars,
            price=float(price),
            label_text=f"{label} {int(numeric_score + 0.5)}",
            tone=tone,
            width=pine.clamp(1.0 + numeric_score / 34.0, 1.0, 5.0),
            style="dashed",
            role=role,
            label_side=label_side,
            fact_fields=fact_fields,
        )
        item["overlay_group"] = "watch_level"
        item["score"] = round(numeric_score, 2)
        item["retention"] = "active"
        overlays.append(item)
    return levels, overlays
