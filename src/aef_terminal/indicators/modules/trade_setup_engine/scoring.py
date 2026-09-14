"""Pure Trade Setup candidate scoring and context gates."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.features.price_action import candle_anatomy
from aef_terminal.features.vsa_classify import VsaBreakoutFact, vsa_breakout_fact_payload
from aef_terminal.runtime import pine

from .contracts import (
    SETUP_TRANSITION_EVENTS,
    TradeSetupEngineParams,
    _ActiveSetup,
    _SetupCandidate,
)


def _setup_transition_event(
    setup: _ActiveSetup,
    bar: Bar,
    event: str,
    *,
    action_reason_code: str,
) -> dict[str, Any]:
    if event not in SETUP_TRANSITION_EVENTS:
        raise ValueError(f"unknown Trade Setup Engine transition event: {event}")
    return {
        "ts": bar.ts.isoformat(),
        "event": event,
        "setup_type": setup.setup_type,
        "direction": setup.direction.value,
        "score": setup.score,
        "reason_code": setup.reason_code,
        "action_reason_code": action_reason_code,
    }


def _direction_flips(
    closes: Sequence[float],
    window: int,
    *,
    end_index: int | None = None,
) -> int:
    end = len(closes) - 1 if end_index is None else min(end_index, len(closes) - 1)
    if end < 2:
        return 0
    start = max(0, end - window + 1)
    flips = 0
    for idx in range(start + 2, end + 1):
        prev_move = closes[idx - 1] - closes[idx - 2]
        move = closes[idx] - closes[idx - 1]
        if prev_move == 0 or move == 0:
            continue
        if (prev_move > 0) > (move > 0):
            flips += 1
    return flips


def _rvol_slope(
    rvol_values: Sequence[float],
    window: int = 5,
    *,
    end_index: int | None = None,
) -> float:
    end = len(rvol_values) - 1 if end_index is None else min(end_index, len(rvol_values) - 1)
    start = end - window + 1
    if start < 0:
        return 0.0
    return (float(rvol_values[end]) - float(rvol_values[start])) / max(
        window - 1,
        1,
    )


def _grind_score(bars: Sequence[Bar], index: int, direction: Direction, window: int) -> float:
    start = max(0, index - window + 1)
    segment = bars[start : index + 1]
    if len(segment) < 4:
        return 0.0
    bodies = [candle_anatomy(bar).body_share for bar in segment]
    avg_body = sum(bodies) / len(bodies)
    grind_quality = pine.clamp(1.0 - avg_body * 1.35, 0.0, 1.0)
    if direction == Direction.LONG:
        higher_lows = sum(
            1
            for idx in range(1, len(segment))
            if float(segment[idx].low) >= float(segment[idx - 1].low) * 0.999
        )
        trend_quality = higher_lows / max(len(segment) - 1, 1)
    else:
        lower_highs = sum(
            1
            for idx in range(1, len(segment))
            if float(segment[idx].high) <= float(segment[idx - 1].high) * 1.001
        )
        trend_quality = lower_highs / max(len(segment) - 1, 1)
    return pine.clamp((grind_quality * 0.55 + trend_quality * 0.45) * 100.0, 0.0, 100.0)


def _grind_score_series(
    bars: Sequence[Bar],
    direction: Direction,
    window: int,
    *,
    body_shares: Sequence[float] | None = None,
) -> list[float]:
    if body_shares is None:
        body_shares = [candle_anatomy(bar).body_share for bar in bars]
    body_prefix = [0.0]
    relation_prefix = [0]
    for index, bar in enumerate(bars):
        body_prefix.append(body_prefix[-1] + float(body_shares[index]))
        relation = 0
        if index:
            previous = bars[index - 1]
            relation = int(
                float(bar.low) >= float(previous.low) * 0.999
                if direction == Direction.LONG
                else float(bar.high) <= float(previous.high) * 1.001
            )
        relation_prefix.append(relation_prefix[-1] + relation)

    out: list[float] = []
    for index in range(len(bars)):
        start = max(0, index - window + 1)
        count = index - start + 1
        if count < 4:
            out.append(0.0)
            continue
        average_body = (body_prefix[index + 1] - body_prefix[start]) / count
        grind_quality = pine.clamp(1.0 - average_body * 1.35, 0.0, 1.0)
        relation_count = relation_prefix[index + 1] - relation_prefix[start + 1]
        trend_quality = relation_count / max(count - 1, 1)
        out.append(
            pine.clamp(
                (grind_quality * 0.55 + trend_quality * 0.45) * 100.0,
                0.0,
                100.0,
            )
        )
    return out


def _volume_build_score(rvol_values: Sequence[float], index: int) -> float:
    slope = _rvol_slope(rvol_values, 5, end_index=index)
    latest = float(rvol_values[index]) if index < len(rvol_values) else 1.0
    return pine.clamp((max(slope, 0.0) * 28.0) + (latest - 1.0) * 22.0, 0.0, 100.0)


def _trend_context(
    direction: Direction,
    *,
    close: float,
    ema_fast: float | None,
    ema_slow: float | None,
) -> dict[str, Any]:
    if ema_fast is None or ema_slow is None:
        return {
            "basis": "ema_fast_slow",
            "trend_direction": Direction.FLAT.value,
            "alignment": "neutral",
            "price_relation": "unknown",
            "score": 50.0,
        }
    fast = float(ema_fast)
    slow = float(ema_slow)
    price = float(close)
    spread = fast - slow
    trend_direction = (
        Direction.LONG if spread > 0 else Direction.SHORT if spread < 0 else Direction.FLAT
    )
    price_relation = "above_fast" if price > fast else "below_fast" if price < fast else "at_fast"
    if trend_direction == Direction.FLAT or direction == Direction.FLAT:
        alignment = "neutral"
    elif trend_direction != direction:
        alignment = "countertrend"
    elif (
        direction == Direction.LONG
        and price >= fast
        or direction == Direction.SHORT
        and price <= fast
    ):
        alignment = "aligned"
    else:
        alignment = "pullback"
    if direction == Direction.LONG:
        score_aligned = price >= fast >= slow
        separation = (fast - slow) / max(abs(slow), 1e-9)
    else:
        score_aligned = price <= fast <= slow
        separation = (slow - fast) / max(abs(slow), 1e-9)
    return {
        "basis": "ema_fast_slow",
        "trend_direction": trend_direction.value,
        "alignment": alignment,
        "price_relation": price_relation,
        "score": pine.clamp(
            (70.0 if score_aligned else 35.0) + separation * 180.0,
            0.0,
            100.0,
        ),
    }


def _structure_confluence_score(direction: Direction, features: dict[str, Any]) -> float:
    displacement = float(features.get("displacement") or 0.0)
    body_share = float(features.get("body_share") or 0.0)
    close_pos = float(features.get("close_pos") or 0.5)
    if direction == Direction.LONG:
        bias = close_pos * 0.55 + (1.0 - body_share) * 0.20 + min(displacement, 1.5) * 0.25
    else:
        bias = (1.0 - close_pos) * 0.55 + (1.0 - body_share) * 0.20 + min(displacement, 1.5) * 0.25
    return pine.clamp(bias * 100.0, 0.0, 100.0)


def _spike_score(
    bars: Sequence[Bar], index: int, direction: Direction, atr: float, window: int, mult: float
) -> float:
    start = max(0, index - window + 1)
    segment = bars[start : index + 1]
    if len(segment) < 2 or atr <= 0:
        return 0.0
    move = float(segment[-1].close) - float(segment[0].close)
    signed_move = move if direction == Direction.LONG else -move
    ratio = signed_move / atr
    if ratio < mult * 0.65:
        return 0.0
    return pine.clamp((ratio / max(mult, 0.1)) * 55.0, 0.0, 100.0)


def _exhaustion_score(rvol_values: Sequence[float], index: int) -> float:
    if index < 4:
        return 0.0
    recent = [float(rvol_values[idx]) for idx in range(index - 3, index + 1)]
    peak = max(recent[:-1])
    latest = recent[-1]
    if peak < 1.25:
        return 0.0
    drop = peak - latest
    return pine.clamp(drop * 55.0 + (peak - 1.0) * 20.0, 0.0, 100.0)


def _chop_score(closes: Sequence[float], index: int, window: int) -> float:
    flips = _direction_flips(closes, window, end_index=index)
    return pine.clamp((flips / max(window - 2, 1)) * 125.0, 0.0, 100.0)


def _rejection_score(bar: Bar, direction: Direction, atr: float) -> float:
    span = max(float(bar.high) - float(bar.low), 1e-9)
    if direction == Direction.SHORT:
        upper_wick = float(bar.high) - max(float(bar.open), float(bar.close))
        return pine.clamp(
            (upper_wick / span) * 100.0
            + (float(bar.high) - float(bar.close)) / max(atr, 1e-9) * 18.0,
            0.0,
            100.0,
        )
    lower_wick = min(float(bar.open), float(bar.close)) - float(bar.low)
    return pine.clamp(
        (lower_wick / span) * 100.0 + (float(bar.close) - float(bar.low)) / max(atr, 1e-9) * 18.0,
        0.0,
        100.0,
    )


def _score_momentum_candidate(
    bars: Sequence[Bar],
    index: int,
    direction: Direction,
    *,
    atr: float,
    rvol_values: Sequence[float],
    ema_fast: Sequence[float],
    ema_slow: Sequence[float],
    features: dict[str, Any],
    params: TradeSetupEngineParams,
    grind_values: Sequence[float] | None = None,
) -> _SetupCandidate | None:
    grind = (
        float(grind_values[index])
        if grind_values is not None
        else _grind_score(bars, index, direction, params.grind_len)
    )
    volume = _volume_build_score(rvol_values, index)
    trend_context = _trend_context(
        direction,
        close=float(bars[index].close),
        ema_fast=ema_fast[index] if index < len(ema_fast) else None,
        ema_slow=ema_slow[index] if index < len(ema_slow) else None,
    )
    trend = float(trend_context["score"])
    structure = _structure_confluence_score(direction, features)
    score = grind * 0.28 + volume * 0.28 + trend * 0.24 + structure * 0.20
    bar = bars[index]
    pad = atr * params.zone_atr
    if direction == Direction.LONG:
        zone_bottom = max(float(bar.low), float(bar.close) - pad)
        zone_top = max(float(bar.high), zone_bottom + pad * 0.5)
    else:
        zone_top = min(float(bar.high), float(bar.close) + pad)
        zone_bottom = min(float(bar.low), zone_top - pad * 0.5)
    return _SetupCandidate(
        setup_type="momentum_breakout",
        direction=direction,
        score=score,
        zone_top=zone_top,
        zone_bottom=zone_bottom,
        reason_code="setup_momentum_breakout"
        if direction == Direction.LONG
        else "setup_momentum_breakdown",
        metrics={
            "grind": grind,
            "volume": volume,
            "trend": trend,
            "formation_trend_context": trend_context,
            "structure": structure,
            "grade": "a" if score >= params.min_score else "watch",
        },
    )


def _score_mean_reversion_candidate(
    bars: Sequence[Bar],
    index: int,
    direction: Direction,
    *,
    atr: float,
    rvol_values: Sequence[float],
    closes: Sequence[float],
    params: TradeSetupEngineParams,
) -> _SetupCandidate | None:
    bar = bars[index]
    spike_dir = Direction.SHORT if direction == Direction.LONG else Direction.LONG
    spike = _spike_score(bars, index, spike_dir, atr, params.spike_len, params.spike_atr_mult)
    exhaustion = _exhaustion_score(rvol_values, index)
    choppy = _chop_score(closes, index, params.chop_len)
    rejection = _rejection_score(bar, direction, atr)
    score = spike * 0.30 + exhaustion * 0.24 + choppy * 0.20 + rejection * 0.26
    pad = atr * params.zone_atr
    if direction == Direction.SHORT:
        zone_top = float(bar.high)
        zone_bottom = zone_top - pad
    else:
        zone_bottom = float(bar.low)
        zone_top = zone_bottom + pad
    return _SetupCandidate(
        setup_type="mean_reversion",
        direction=direction,
        score=score,
        zone_top=zone_top,
        zone_bottom=zone_bottom,
        reason_code="setup_mean_reversion",
        metrics={
            "spike": spike,
            "exhaustion": exhaustion,
            "chop": choppy,
            "rejection": rejection,
            "grade": "a" if score >= params.min_score else "watch",
        },
    )


def _market_context_from_bundle(
    indicator_bundle: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    bundle = indicator_bundle or {}
    spotlight = (
        bundle.get("market_spotlight") if isinstance(bundle.get("market_spotlight"), dict) else {}
    )
    context = spotlight.get("market_context") if isinstance(spotlight, dict) else {}
    return context if isinstance(context, dict) else {}


def _market_context_gate(
    direction: Direction, setup_type: str, market_context: dict[str, Any] | None
) -> dict[str, Any]:
    if direction == Direction.FLAT or not isinstance(market_context, dict):
        return {"blocked": False, "reason_code": "", "score_adjust": 0.0}
    side_gate = (
        market_context.get("side_gate") if isinstance(market_context.get("side_gate"), dict) else {}
    )
    flow_state = (
        market_context.get("flow_state")
        if isinstance(market_context.get("flow_state"), dict)
        else {}
    )
    direction_value = direction.value
    veto = bool(
        (direction == Direction.LONG and side_gate.get("veto_long"))
        or (direction == Direction.SHORT and side_gate.get("veto_short"))
    )
    dominant = str(
        flow_state.get("dominant_direction") or market_context.get("direction") or "flat"
    )
    pullback_allowed = bool(flow_state.get("pullback_allowed"))
    countertrend_direction = str(flow_state.get("countertrend_direction") or "flat")
    if veto:
        if (
            setup_type == "mean_reversion"
            and pullback_allowed
            and countertrend_direction == direction_value
        ):
            return {
                "blocked": False,
                "reason_code": "market_spotlight_pullback_allowed",
                "score_adjust": -6.0,
            }
        return {
            "blocked": True,
            "reason_code": f"market_spotlight_veto_{direction_value}",
            "score_adjust": -100.0,
        }
    if dominant == direction_value and setup_type == "momentum_breakout":
        return {
            "blocked": False,
            "reason_code": "market_spotlight_flow_aligned",
            "score_adjust": 4.0,
        }
    if (
        dominant
        and dominant != "flat"
        and dominant != direction_value
        and setup_type == "mean_reversion"
    ):
        return {
            "blocked": False,
            "reason_code": "market_spotlight_countertrend_watch",
            "score_adjust": -4.0,
        }
    return {"blocked": False, "reason_code": "", "score_adjust": 0.0}


def _vsa_breakout_gate(
    direction: Direction,
    setup_type: str,
    fact: VsaBreakoutFact | None,
) -> dict[str, Any]:
    neutral = {
        "active": False,
        "blocked": False,
        "reason_code": "",
        "score_adjust": 0.0,
        "tier": "",
        "context": {},
    }
    if fact is None or fact.availability_state != "ready" or direction == Direction.FLAT:
        return neutral
    context = vsa_breakout_fact_payload(fact)
    if (
        direction == Direction.LONG
        and setup_type == "momentum_breakout"
        and fact.long_break
        and fact.long_disposition == "avoid"
    ):
        return {
            **neutral,
            "active": True,
            "blocked": True,
            "reason_code": (fact.long_reason_code or "vsa_long_break_avoid"),
            "score_adjust": -100.0,
            "context": context,
        }
    if (
        direction == Direction.SHORT
        and fact.short_break
        and fact.short_disposition == "supported"
        and fact.short_setup_type == setup_type
    ):
        tier = str(fact.short_tier or "").lower()
        return {
            **neutral,
            "active": True,
            "reason_code": (fact.short_reason_code or "vsa_short_break_supported"),
            "score_adjust": 8.0 if tier == "a" else 6.0,
            "tier": tier,
            "context": context,
        }
    return neutral
