"""Confirmed-one-minute fast path for Rounded Reversal v2."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from math import isfinite
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.features.smc import SmcParams, smc_snapshot
from aef_terminal.indicators.domain_facts import indicator_fact_payload
from aef_terminal.runtime import overlays, pine
from aef_terminal.runtime.bar_quality import is_provisional_bar

from .adaptive import adaptive_rejection_summary, sensitivity_value, soft_up
from .contracts import (
    ROUNDED_REVERSAL_MICRO_TIMEFRAME,
    ROUNDED_REVERSAL_SOURCE,
    ROUNDED_REVERSAL_VERSION,
    RoundedReversalParams,
    _FAST_ACTIVE_STATES,
    _FAST_TERMINAL_STATES,
)


_FAST_TRIGGER_CODES = {
    "WATCH": "rounded_top_fast_watch",
    "ARMED": "rounded_top_fast_armed",
    "TRIGGERED": "rounded_top_fast_triggered",
    "RETEST_CONFIRMED": "rounded_top_fast_retest_confirmed",
    "MISSED": "rounded_top_fast_missed",
    "INVALIDATED": "rounded_top_fast_invalidated",
    "EXPIRED": "rounded_top_fast_expired",
}


def _confirmed_contiguous_suffix(bars: Sequence[Bar]) -> list[Bar]:
    confirmed: list[Bar] = []
    for bar in bars:
        if is_provisional_bar(bar):
            break
        confirmed.append(bar)
    if not confirmed:
        return []
    expected = timedelta(minutes=1)
    start = 0
    for index in range(1, len(confirmed)):
        if confirmed[index].ts - confirmed[index - 1].ts != expected:
            start = index
    return confirmed[start:]


def _fact_fields(latest: Mapping[str, Any]) -> dict[str, Any]:
    state = str(latest.get("state") or "WATCH")
    levels = latest.get("levels") if isinstance(latest.get("levels"), Mapping) else {}
    metrics = latest.get("metrics") if isinstance(latest.get("metrics"), Mapping) else {}
    cluster = (
        latest.get("rejection_cluster")
        if isinstance(latest.get("rejection_cluster"), Mapping)
        else {}
    )
    supporting: list[dict[str, Any]] = [
        {
            "code": "fast_mature_up_leg",
            "rise_atr": metrics.get("rise_atr"),
        },
        {
            "code": "fast_adaptive_rejection_mass",
            "cluster_score": cluster.get("cluster_score"),
            "evidence_mass": cluster.get("evidence_mass"),
        },
    ]
    if isinstance(latest.get("armed_index"), int):
        supporting.append(
            {
                "code": "fast_micro_trigger_frozen",
                "price": levels.get("trigger"),
            }
        )
    if state in {"TRIGGERED", "RETEST_CONFIRMED"}:
        supporting.append(
            {
                "code": "fast_micro_structure_close_break",
                "price": levels.get("trigger"),
            }
        )
    if state == "RETEST_CONFIRMED":
        supporting.append({"code": "fast_trigger_retest_rejection"})
    opposing: list[dict[str, Any]] = []
    if state == "MISSED":
        opposing.append({"code": "fast_first_close_too_extended"})
    elif state == "INVALIDATED":
        opposing.append({"code": "fast_apex_close_invalidation"})
    elif state == "EXPIRED":
        opposing.append({"code": "fast_generation_expired"})
    return indicator_fact_payload(
        scenario="rounded_top_fast",
        setup="rounded_reversal_fast",
        trigger_event={"code": _FAST_TRIGGER_CODES[state]},
        supporting=supporting,
        opposing=opposing,
        context=[
            {"code": "confirmed_one_minute_only"},
            {
                "code": "fast_generation_reanchors_before_trigger",
                "generation_id": latest.get("generation_id"),
            },
        ],
        risk={
            "code": "fast_apex_close_invalidation",
            "price": levels.get("structural_invalidation"),
        },
        quality={
            "code": "rounded_top_fast_quality",
            "value": latest.get("score"),
        },
        fact_groups=[
            {
                "kind": "rounded_top_fast_structure",
                "items": supporting,
            }
        ],
        metrics=dict(metrics),
    )


def _overlays(
    bars: Sequence[Bar],
    latest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    state = str(latest.get("state") or "")
    if state not in _FAST_ACTIVE_STATES:
        return []
    levels = latest.get("levels") if isinstance(latest.get("levels"), Mapping) else {}
    anchors = latest.get("anchors") if isinstance(latest.get("anchors"), Mapping) else {}
    apex = anchors.get("apex") if isinstance(anchors.get("apex"), Mapping) else {}
    cluster = (
        latest.get("rejection_cluster")
        if isinstance(latest.get("rejection_cluster"), Mapping)
        else {}
    )
    facts = _fact_fields(latest)
    tone = "negative" if state in {"TRIGGERED", "RETEST_CONFIRMED"} else "warning"
    zone = overlays.box(
        start_ts=str(cluster.get("first_ts") or apex.get("ts") or bars[-1].ts.isoformat()),
        end_ts=bars[-1].ts.isoformat(),
        top=float(levels["apex"]),
        bottom=float(levels["rejection_zone_bottom"]),
        tone=tone,
        opacity=0.0,
        interactive=True,
        role="rounded_reversal_fast_zone",
        direction=Direction.SHORT,
        fact_fields=facts,
    )
    zone.update(
        {
            "source": ROUNDED_REVERSAL_SOURCE,
            "style": "dashed",
            "width": 1.4 if state in {"TRIGGERED", "RETEST_CONFIRMED"} else 1.0,
            "border_opacity": 0.82 if state in {"TRIGGERED", "RETEST_CONFIRMED"} else 0.50,
            "animated_border": bool(latest.get("event_on_latest"))
            and state in {"TRIGGERED", "RETEST_CONFIRMED"},
            "event_code": _FAST_TRIGGER_CODES[state],
        }
    )
    items = [zone]
    trigger = levels.get("trigger")
    if isinstance(trigger, (int, float)) and isfinite(float(trigger)):
        line = overlays.line(
            start_ts=str(apex.get("ts") or bars[-1].ts.isoformat()),
            end_ts=bars[-1].ts.isoformat(),
            price=float(trigger),
            label_text=None,
            tone=tone,
            width=1.2,
            opacity=0.78,
            interactive=True,
            style="dotted",
            role="rounded_reversal_fast_trigger",
            direction=Direction.SHORT,
            fact_fields=facts,
        )
        line.update(
            {
                "source": ROUNDED_REVERSAL_SOURCE,
                "event_code": _FAST_TRIGGER_CODES[state],
            }
        )
        items.append(line)
    return items


def fast_rounded_reversal(
    bars: Sequence[Bar],
    *,
    tick_size: float,
    params: RoundedReversalParams,
) -> dict[str, Any]:
    """Evaluate one reanchoring fast-top generation from closed one-minute bars."""

    empty = {
        "version": ROUNDED_REVERSAL_VERSION,
        "settings": {
            "track": "fast",
            "sensitivity": params.sensitivity,
            "pivot_len": params.fast_pivot_len,
            "max_pattern_age": params.fast_max_pattern_age,
        },
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
        "signals": [],
    }
    confirmed = _confirmed_contiguous_suffix(bars)
    required = max(params.fast_rise_lookback // 2, params.fast_pivot_len * 2 + 4, 10)
    if len(confirmed) < required:
        return {
            **empty,
            "availability": {
                "state": "blocked",
                "reason_code": "rounded_reversal_fast_warmup",
                "required_bars": required,
                "available_bars": len(confirmed),
            },
        }
    if any(bar.timeframe != ROUNDED_REVERSAL_MICRO_TIMEFRAME for bar in confirmed):
        return {
            **empty,
            "availability": {
                "state": "blocked",
                "reason_code": "rounded_reversal_fast_timeframe_mismatch",
            },
        }

    working = confirmed[-max(int(params.fast_scan_bars), required) :]
    atr_values = pine.atr_rma_series(working, params.atr_len)
    current_index = len(working) - 1
    apex_start = max(0, current_index - int(params.fast_max_pattern_age))
    apex_index = max(
        range(apex_start, current_index + 1),
        key=lambda index: (float(working[index].high), -index),
    )
    base_start = max(0, apex_index - int(params.fast_rise_lookback))
    if apex_index - base_start < 3:
        return empty
    base_index = min(
        range(base_start, apex_index),
        key=lambda index: (float(working[index].low), index),
    )
    apex_price = float(working[apex_index].high)
    base_price = float(working[base_index].low)
    apex_atr = max(float(atr_values[apex_index]), 1e-9)
    rise_atr = (apex_price - base_price) / apex_atr
    rise_floor = sensitivity_value(
        params.sensitivity,
        early=params.fast_min_rise_atr * 0.80,
        balanced=params.fast_min_rise_atr,
        strict=params.fast_min_rise_atr * 1.25,
    )
    if rise_atr < rise_floor:
        return empty

    window_start = max(base_index, apex_index - int(params.fast_wick_before))
    window_end = min(current_index, apex_index + int(params.fast_wick_after))
    cluster_threshold = sensitivity_value(
        params.sensitivity,
        early=0.68,
        balanced=0.78,
        strict=0.88,
    )
    strong_threshold = sensitivity_value(
        params.sensitivity,
        early=max(params.adaptive_strong_bar_score - 0.10, 0.25),
        balanced=params.adaptive_strong_bar_score,
        strict=min(params.adaptive_strong_bar_score + 0.08, 0.98),
    )
    cluster = adaptive_rejection_summary(
        working[window_start : window_end + 1],
        atr_values=atr_values[window_start : window_end + 1],
        apex_price=apex_price,
        apex_atr=apex_atr,
        tick_size=float(tick_size),
        target_wick_share=params.micro_min_wick_share,
        target_wick_atr=params.micro_min_wick_atr,
        target_close_pos=params.max_rejection_close_pos,
        level_tolerance_atr=params.level_tolerance_atr,
        level_tolerance_ticks=params.level_tolerance_ticks,
        target_mass=params.fast_target_mass,
        max_scored_bars=4,
        cluster_threshold=cluster_threshold,
        strong_bar_threshold=strong_threshold,
        min_support_bars=1,
        source_timeframe=ROUNDED_REVERSAL_MICRO_TIMEFRAME,
    )
    cluster_ready_local = cluster.pop("ready_local_index", None)
    cluster_ready_index = (
        window_start + int(cluster_ready_local) if isinstance(cluster_ready_local, int) else None
    )
    watch_threshold = sensitivity_value(
        params.sensitivity,
        early=0.38,
        balanced=0.48,
        strict=0.58,
    )
    watch_ready = bool(
        float(cluster["cluster_score"]) >= watch_threshold
        and float(cluster["strongest_bar_score"]) >= strong_threshold * 0.70
    )
    if not watch_ready:
        return empty

    structure = smc_snapshot(
        working,
        SmcParams(
            pivot_len=params.fast_pivot_len,
            break_buffer_atr=0.0,
            max_events=120,
            max_boxes=4,
        ),
    )
    pivot_lows = [
        item
        for item in structure.get("pivots", [])
        if isinstance(item, Mapping)
        and item.get("type") == -1
        and isinstance(item.get("index"), int)
        and base_index <= int(item["index"]) < apex_index
    ]
    trigger_pivot = pivot_lows[-1] if pivot_lows else None
    trigger_level = (
        float(trigger_pivot["price"])
        if isinstance(trigger_pivot, Mapping)
        and isinstance(trigger_pivot.get("price"), (int, float))
        else None
    )
    trigger_ready_index = (
        int(trigger_pivot["index"]) + int(params.fast_pivot_len)
        if isinstance(trigger_pivot, Mapping)
        else None
    )
    armed_index = (
        max(int(cluster_ready_index), int(trigger_ready_index))
        if isinstance(cluster_ready_index, int) and isinstance(trigger_ready_index, int)
        else None
    )

    structural_events = [
        item
        for item in structure.get("events", [])
        if isinstance(item, Mapping)
        and armed_index is not None
        and item.get("direction") == Direction.SHORT.value
        and item.get("event_code") in {"change_of_character", "break_of_structure"}
        and isinstance(item.get("index"), int)
        and int(item["index"]) > apex_index
        and int(item["index"]) >= armed_index
        and (
            trigger_level is None
            or not isinstance(item.get("price"), (int, float))
            or abs(float(item["price"]) - trigger_level)
            <= max(float(tick_size) * 2.0, apex_atr * 0.20)
        )
    ]
    trigger_event = structural_events[0] if structural_events else None
    trigger_index = int(trigger_event["index"]) if isinstance(trigger_event, Mapping) else None
    invalidation_buffer = max(
        float(tick_size) * int(params.invalidation_ticks),
        apex_atr * float(params.invalidation_atr),
    )
    invalidation = apex_price + invalidation_buffer
    invalidation_index: int | None = None
    if trigger_index is not None:
        for index in range(trigger_index + 1, len(working)):
            if float(working[index].close) > invalidation:
                invalidation_index = index
                break

    retest_index: int | None = None
    missed = False
    if trigger_index is not None and trigger_level is not None:
        trigger_atr = max(float(atr_values[trigger_index]), 1e-9)
        entry_distance = (trigger_level - float(working[trigger_index].close)) / trigger_atr
        missed = entry_distance > float(params.fast_max_entry_distance_atr)
        tolerance = trigger_atr * float(params.fast_retest_tolerance_atr)
        for index in range(trigger_index + 1, len(working)):
            bar = working[index]
            if (
                float(bar.high) >= trigger_level - tolerance
                and float(bar.close) < trigger_level
                and float(bar.close) < float(bar.open)
            ):
                retest_index = index
                break

    apex_age = current_index - apex_index
    if invalidation_index is not None:
        state = "INVALIDATED"
        transition_index = invalidation_index
    elif trigger_index is not None and missed:
        state = "MISSED"
        transition_index = trigger_index
    elif retest_index is not None:
        state = "RETEST_CONFIRMED"
        transition_index = retest_index
    elif trigger_index is not None:
        state = "TRIGGERED"
        transition_index = trigger_index
    elif apex_age >= int(params.fast_max_pattern_age):
        state = "EXPIRED"
        transition_index = current_index
    elif armed_index is not None and current_index >= armed_index:
        state = "ARMED"
        transition_index = armed_index
    else:
        state = "WATCH"
        material = cluster.get("observations") or []
        first_material_ts = (
            str(material[0].get("ts"))
            if material and isinstance(material[0], Mapping)
            else working[apex_index].ts.isoformat()
        )
        transition_index = next(
            (index for index, bar in enumerate(working) if bar.ts.isoformat() == first_material_ts),
            apex_index,
        )

    previous_apex_index = (
        max(
            range(apex_start, current_index),
            key=lambda index: (float(working[index].high), -index),
        )
        if current_index > apex_start
        else None
    )
    reanchored_from = None
    if (
        apex_index == current_index
        and isinstance(previous_apex_index, int)
        and apex_price > float(working[previous_apex_index].high)
        and current_index - previous_apex_index <= int(params.fast_max_pattern_age)
    ):
        reanchored_from = (
            f"rounded-top-fast:{working[previous_apex_index].ts.isoformat()}:"
            f"{float(working[previous_apex_index].high):.6f}"
        )
        transition_index = current_index

    generation_id = f"fast:{working[apex_index].ts.isoformat()}:{apex_price:.6f}"
    pattern_id = f"rounded-top-{generation_id}"
    shape_score = soft_up(rise_atr, rise_floor, max(rise_floor * 0.40, 0.25))
    structure_score = 1.0 if trigger_index is not None else 0.60 if armed_index is not None else 0.0
    total_score = pine.clamp(
        shape_score * 25.0 + float(cluster["cluster_score"]) * 45.0 + structure_score * 30.0,
        0.0,
        99.0,
    )
    availability_index = max(transition_index, apex_index)
    available_at = working[availability_index].ts + timedelta(minutes=1)
    trigger_code = (
        "rounded_top_fast_reanchored"
        if reanchored_from is not None and transition_index == current_index
        else _FAST_TRIGGER_CODES[state]
    )
    latest: dict[str, Any] = {
        "track": "fast",
        "pattern_id": pattern_id,
        "generation_id": generation_id,
        "reanchored_from_pattern_id": reanchored_from,
        "state": state,
        "state_code": state.lower(),
        "trigger_state": trigger_code,
        "direction": Direction.SHORT.value,
        "score": round(total_score, 2),
        "transition_index": transition_index,
        "transition_ts": working[transition_index].ts.isoformat(),
        "available_at_ts": available_at.isoformat(),
        "event_on_latest": transition_index == current_index,
        "active": state in _FAST_ACTIVE_STATES,
        "terminal": state in _FAST_TERMINAL_STATES,
        "armed_index": armed_index,
        "breakdown_index": trigger_index,
        "anchors": {
            "base": {
                "index": base_index,
                "ts": working[base_index].ts.isoformat(),
                "price": round(base_price, 6),
            },
            "apex": {
                "index": apex_index,
                "ts": working[apex_index].ts.isoformat(),
                "price": round(apex_price, 6),
            },
        },
        "geometry": {
            "rise_bars": apex_index - base_index,
            "height_atr": round(rise_atr, 4),
            "shape_score": round(shape_score, 4),
        },
        "rejection_cluster": cluster,
        "rollover": {},
        "levels": {
            "apex": round(apex_price, 6),
            "origin_base": round(base_price, 6),
            "trigger": round(trigger_level, 6) if trigger_level is not None else None,
            "neckline": round(trigger_level, 6) if trigger_level is not None else None,
            "break_level": round(trigger_level, 6) if trigger_level is not None else None,
            "structural_invalidation": round(invalidation, 6),
            "theoretical_target": (
                round(trigger_level - (apex_price - trigger_level), 6)
                if trigger_level is not None
                else None
            ),
            "rejection_zone_top": round(apex_price, 6),
            "rejection_zone_bottom": cluster.get("zone_bottom"),
            "effective_rejection_zone_top": round(apex_price, 6),
            "effective_rejection_zone_bottom": cluster.get("zone_bottom"),
        },
        "metrics": {
            "rise_atr": round(rise_atr, 4),
            "pattern_age_bars": apex_age,
            "cluster_score": cluster.get("cluster_score"),
            "trigger_distance_atr": (
                round(
                    (trigger_level - float(working[trigger_index].close))
                    / max(float(atr_values[trigger_index]), 1e-9),
                    4,
                )
                if trigger_index is not None and trigger_level is not None
                else 0.0
            ),
        },
        "adaptive_thresholds": {
            "sensitivity": params.sensitivity,
            "rise_floor_atr": round(rise_floor, 4),
            **dict(cluster.get("adaptive_thresholds") or {}),
        },
        "gate_margins": {
            "rise_atr": round(rise_atr - rise_floor, 4),
            "cluster_score": round(
                float(cluster["cluster_score"]) - cluster_threshold,
                4,
            ),
        },
        "evidence_groups": [
            {
                "group": "auction_rejection",
                "score": cluster.get("cluster_score"),
            },
            {
                "group": "micro_structure",
                "score": round(structure_score, 4),
            },
        ],
    }
    latest.update(_fact_fields(latest))
    event = None
    if latest["event_on_latest"]:
        event = {
            **latest,
            "id": f"{pattern_id}:{trigger_code}:{latest['transition_ts']}",
            "ts": latest["transition_ts"],
            "source": ROUNDED_REVERSAL_SOURCE,
            "event_type": "rounded_reversal_fast_stage",
            "code": trigger_code,
            "price": round(float(working[-1].close), 6),
            "level": latest["levels"].get("trigger"),
        }
    return {
        **empty,
        "series": [latest],
        "events": [event] if event is not None else [],
        "latest": latest,
        "overlays": _overlays(working, latest),
    }


__all__ = ["fast_rounded_reversal"]
