"""Causal trend-pullback-reclaim calculation for Wisdom of Crowd."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, DomainFact, ScenarioKind
from aef_terminal.features.context import FeatureContext
from aef_terminal.features.market_series import MarketSeriesBlock, build_market_series_block
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.indicators.domain_facts import indicator_fact_payload, metric_number
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional
from aef_terminal.runtime.signal_state import SignalState, lifecycle_from_signals

from .contracts import (
    WISDOM_OF_CROWD_SOURCE,
    WISDOM_OF_CROWD_VERSION,
    WisdomOfCrowdParams,
)
from .presentation import build_overlays


def _round(value: float | None, digits: int = 6) -> float | None:
    return round_optional(value, digits)


def _empty(params: WisdomOfCrowdParams) -> dict[str, Any]:
    return {
        "version": WISDOM_OF_CROWD_VERSION,
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
        "settings": {
            "required_bars": params.required_bars,
            "ema_fast_len": params.ema_fast_len,
            "ema_slow_len": params.ema_slow_len,
            "slope_lookback": params.slope_lookback,
            "pullback_bars": params.pullback_bars,
            "session_min_bars": params.session_min_bars,
            "pullback_tolerance_atr": params.pullback_tolerance_atr,
            "max_extension_atr": params.max_extension_atr,
            "reward_risk": params.reward_risk,
        },
    }


def _session_ages(
    bars: Sequence[Bar],
    session: ProviderSessionReset,
) -> list[int]:
    ages: list[int] = []
    previous_key = ""
    age = 0
    for bar in bars:
        key = session.key_for_bar(bar)
        age = age + 1 if key == previous_key else 1
        ages.append(age)
        previous_key = key
    return ages


def _structure_score(
    bars: Sequence[Bar],
    index: int,
    direction: Direction,
    window: int,
) -> float:
    start = max(0, index - window + 1)
    segment = bars[start : index + 1]
    if len(segment) < 2 or direction is Direction.FLAT:
        return 0.0
    if direction is Direction.LONG:
        aligned = sum(
            float(segment[position].low) >= float(segment[position - 1].low)
            for position in range(1, len(segment))
        )
    else:
        aligned = sum(
            float(segment[position].high) <= float(segment[position - 1].high)
            for position in range(1, len(segment))
        )
    return aligned / (len(segment) - 1) * 100.0


def _regime_direction(
    *,
    close: float,
    vwap: float,
    ema_fast: float,
    ema_slow: float,
    fast_slope_atr: float,
    slow_slope_atr: float,
    min_slope_atr: float,
) -> Direction:
    if (
        close > vwap
        and ema_fast > ema_slow
        and fast_slope_atr >= min_slope_atr
        and slow_slope_atr > 0.0
    ):
        return Direction.LONG
    if (
        close < vwap
        and ema_fast < ema_slow
        and fast_slope_atr <= -min_slope_atr
        and slow_slope_atr < 0.0
    ):
        return Direction.SHORT
    return Direction.FLAT


def _pullback_index(
    bars: Sequence[Bar],
    *,
    index: int,
    direction: Direction,
    atr_values: Sequence[float],
    ema_fast: Sequence[float],
    ema_slow: Sequence[float],
    vwap: Sequence[float],
    params: WisdomOfCrowdParams,
    earliest_index: int,
) -> tuple[int | None, float | None]:
    start = max(earliest_index, index - params.pullback_bars + 1)
    for candidate_index in range(index, start - 1, -1):
        atr = max(float(atr_values[candidate_index]), 1e-9)
        fast = float(ema_fast[candidate_index])
        slow = float(ema_slow[candidate_index])
        session_vwap = float(vwap[candidate_index])
        bar = bars[candidate_index]
        if direction is Direction.LONG:
            if fast <= slow:
                return None, None
            level = max(fast, session_vwap)
            touched = (
                float(bar.low) <= level + atr * params.pullback_tolerance_atr
                and float(bar.high) >= level - atr * params.pullback_tolerance_atr
            )
            held = float(bar.low) >= slow - atr * params.pullback_break_atr
        else:
            if fast >= slow:
                return None, None
            level = min(fast, session_vwap)
            touched = (
                float(bar.high) >= level - atr * params.pullback_tolerance_atr
                and float(bar.low) <= level + atr * params.pullback_tolerance_atr
            )
            held = float(bar.high) <= slow + atr * params.pullback_break_atr
        # A newer invalidation cancels the older pullback; do not search through it.
        if not held:
            return None, None
        if touched:
            return candidate_index, level
    return None, None


def _score(
    *,
    direction: Direction,
    fast_slope_atr: float,
    slow_slope_atr: float,
    separation_atr: float,
    adx: float,
    structure_score: float,
    extension_atr: float,
    pullback: bool,
    reclaim: bool,
    params: WisdomOfCrowdParams,
) -> float:
    if direction is Direction.FLAT:
        return 0.0
    sign = 1.0 if direction is Direction.LONG else -1.0
    signed_fast = fast_slope_atr * sign
    signed_slow = slow_slope_atr * sign
    value = 42.0
    value += pine.clamp(separation_atr / 0.60, 0.0, 1.0) * 12.0
    value += (
        pine.clamp(
            (signed_fast - params.min_slope_atr) / 0.25,
            0.0,
            1.0,
        )
        * 14.0
    )
    value += pine.clamp(signed_slow / 0.18, 0.0, 1.0) * 10.0
    value += pine.clamp((adx - 12.0) / 20.0, 0.0, 1.0) * 10.0
    value += pine.clamp(structure_score / 100.0, 0.0, 1.0) * 10.0
    value += (
        pine.clamp(
            1.0 - extension_atr / max(params.max_extension_atr, 1e-9),
            0.0,
            1.0,
        )
        * 8.0
    )
    value += 6.0 if pullback else 0.0
    value += 8.0 if reclaim else 0.0
    return pine.clamp(value, 0.0, 99.0)


def _trade_plan(
    bars: Sequence[Bar],
    *,
    index: int,
    pullback_index: int,
    direction: Direction,
    atr: float,
    params: WisdomOfCrowdParams,
) -> tuple[float, float, float]:
    bar = bars[index]
    entry = float(bar.close)
    if direction is Direction.LONG:
        structural_stop = (
            min(float(item.low) for item in bars[pullback_index : index + 1])
            - atr * params.stop_buffer_atr
        )
        risk = max(entry - structural_stop, atr * params.min_stop_atr)
        stop = entry - risk
        target = entry + risk * params.reward_risk
    else:
        structural_stop = (
            max(float(item.high) for item in bars[pullback_index : index + 1])
            + atr * params.stop_buffer_atr
        )
        risk = max(structural_stop - entry, atr * params.min_stop_atr)
        stop = entry + risk
        target = entry - risk * params.reward_risk
    return entry, stop, target


def _typed_facts(
    *,
    direction: Direction,
    state_code: str,
    trigger_event_code: str,
    price: float,
    vwap: float,
    ema_fast: float,
    ema_slow: float,
    fast_slope_atr: float,
    slow_slope_atr: float,
    adx: float,
    extension_atr: float,
    score: float,
    signal: SignalState,
) -> dict[str, Any]:
    supporting: list[DomainFact] = []
    opposing: list[DomainFact] = []
    if direction is not Direction.FLAT:
        supporting.extend(
            (
                DomainFact(
                    "crowd_price_vwap_aligned",
                    {"direction": direction.value, "price": price, "vwap": vwap},
                ),
                DomainFact(
                    "crowd_ema_stack_aligned",
                    {
                        "direction": direction.value,
                        "ema_fast": ema_fast,
                        "ema_slow": ema_slow,
                    },
                ),
                DomainFact(
                    "crowd_ema_slopes_aligned",
                    {
                        "direction": direction.value,
                        "fast_slope_atr": fast_slope_atr,
                        "slow_slope_atr": slow_slope_atr,
                    },
                ),
            )
        )
    if state_code == "extended":
        opposing.append(DomainFact("crowd_extension_excessive", {"extension_atr": extension_atr}))
    return indicator_fact_payload(
        scenario="trend_continuation",
        setup=(
            "crowd_pullback_reclaim"
            if state_code in {"pullback_armed", "reclaim_confirmed"}
            else "crowd_trend_filter"
        ),
        trigger_event=DomainFact(trigger_event_code),
        supporting=supporting,
        opposing=opposing,
        context=DomainFact("provider_session_vwap", {"available": True}),
        risk=(
            DomainFact(
                "crowd_structural_invalidation",
                {
                    "stop": signal.stop,
                    "blocks": opposing,
                },
            )
            if signal.stop is not None
            else None
        ),
        quality=DomainFact("crowd_setup_quality", {"score": score, "adx": adx}),
        metrics={
            "price": metric_number(price),
            "vwap": metric_number(vwap),
            "ema_fast": metric_number(ema_fast),
            "ema_slow": metric_number(ema_slow),
            "fast_slope_atr": metric_number(fast_slope_atr),
            "slow_slope_atr": metric_number(slow_slope_atr),
            "adx": metric_number(adx, digits=1),
            "extension_atr": metric_number(extension_atr),
            "score": metric_number(score, digits=0),
        },
        trade_plan=signal.trade_plan,
    )


def wisdom_of_crowd(
    bars: Sequence[Bar],
    *,
    params: WisdomOfCrowdParams | None = None,
    vwap_session: ProviderSessionReset,
    feature_context: FeatureContext | None = None,
    market_series: MarketSeriesBlock | None = None,
) -> dict[str, Any]:
    resolved = params or WisdomOfCrowdParams()
    result = _empty(resolved)
    if not bars:
        return result
    if not vwap_session.available:
        result["availability"] = {
            "state": "blocked",
            "reason_code": vwap_session.reason_code,
            "required_context": "provider_vwap_session",
            "vwap": vwap_session.status_payload(),
        }
        return result
    if len(bars) < resolved.required_bars:
        result["availability"] = {
            "state": "blocked",
            "reason_code": "wisdom_of_crowd_warmup",
            "required_bars": resolved.required_bars,
            "available_bars": len(bars),
        }
        return result

    block = market_series or build_market_series_block(
        list(bars),
        ema_pullback_len=resolved.ema_fast_len,
        ema_trend_len=resolved.ema_slow_len,
        ema_magnet_len=resolved.ema_magnet_len,
        atr_len=resolved.atr_len,
        vwap_session=vwap_session,
        feature_context=feature_context,
    )
    session_ages = _session_ages(bars, vwap_session)
    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    signals: list[SignalState] = []
    last_go_index = -resolved.cooldown_bars

    start_index = resolved.required_bars - 1
    for index in range(start_index, len(bars)):
        bar = bars[index]
        atr = max(float(block.atr_sma[index]), 1e-9)
        ema_fast = float(block.ema_pullback[index])
        ema_slow = float(block.ema_trend[index])
        vwap = float(block.vwap[index])
        adx = float(block.adx[index])
        fast_slope_atr = (
            ema_fast - float(block.ema_pullback[index - resolved.slope_lookback])
        ) / atr
        slow_slope_atr = (ema_slow - float(block.ema_trend[index - resolved.slope_lookback])) / atr
        direction = _regime_direction(
            close=float(bar.close),
            vwap=vwap,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            fast_slope_atr=fast_slope_atr,
            slow_slope_atr=slow_slope_atr,
            min_slope_atr=resolved.min_slope_atr,
        )
        regime_code = (
            "trend_up"
            if direction is Direction.LONG
            else "trend_down"
            if direction is Direction.SHORT
            else "transition"
        )
        structure_score = _structure_score(
            bars,
            index,
            direction,
            resolved.structure_bars,
        )
        reference = (
            max(ema_fast, vwap)
            if direction is Direction.LONG
            else min(ema_fast, vwap)
            if direction is Direction.SHORT
            else ema_fast
        )
        extension_atr = abs(float(bar.close) - reference) / atr
        earliest_pullback = max(
            start_index,
            last_go_index + resolved.cooldown_bars,
            index - session_ages[index] + 1,
        )
        prior_pullback_index, prior_pullback_level = (
            _pullback_index(
                bars,
                index=index - 1,
                direction=direction,
                atr_values=block.atr_sma,
                ema_fast=block.ema_pullback,
                ema_slow=block.ema_trend,
                vwap=block.vwap,
                params=resolved,
                earliest_index=earliest_pullback,
            )
            if direction is not Direction.FLAT and index > earliest_pullback
            else (None, None)
        )
        reclaim = bool(
            prior_pullback_index is not None
            and (
                (
                    direction is Direction.LONG
                    and float(bar.close) > float(bars[index - 1].high)
                    and float(bar.close) > reference
                    and float(bar.close) > float(bar.open)
                    and float(bar.low) >= ema_slow - atr * resolved.pullback_break_atr
                )
                or (
                    direction is Direction.SHORT
                    and float(bar.close) < float(bars[index - 1].low)
                    and float(bar.close) < reference
                    and float(bar.close) < float(bar.open)
                    and float(bar.high) <= ema_slow + atr * resolved.pullback_break_atr
                )
            )
        )
        current_pullback_index, current_pullback_level = (
            _pullback_index(
                bars,
                index=index,
                direction=direction,
                atr_values=block.atr_sma,
                ema_fast=block.ema_pullback,
                ema_slow=block.ema_trend,
                vwap=block.vwap,
                params=resolved,
                earliest_index=earliest_pullback,
            )
            if direction is not Direction.FLAT
            else (None, None)
        )
        pullback_index = prior_pullback_index if reclaim else current_pullback_index
        pullback_level = prior_pullback_level if reclaim else current_pullback_level
        pullback_age = index - pullback_index if pullback_index is not None else None
        score = _score(
            direction=direction,
            fast_slope_atr=fast_slope_atr,
            slow_slope_atr=slow_slope_atr,
            separation_atr=abs(ema_fast - ema_slow) / atr,
            adx=adx,
            structure_score=structure_score,
            extension_atr=extension_atr,
            pullback=pullback_index is not None,
            reclaim=reclaim,
            params=resolved,
        )

        blocked_reason = ""
        if session_ages[index] < resolved.session_min_bars:
            state_code = "session_warmup"
            action = ActionPhase.BLOCK
            blocked_reason = "crowd_provider_session_warmup"
            trigger_event_code = "crowd_session_warmup"
        elif direction is Direction.FLAT:
            state_code = "no_trend"
            action = ActionPhase.WAIT
            trigger_event_code = "crowd_trend_unavailable"
        elif extension_atr > resolved.max_extension_atr:
            state_code = "extended"
            action = ActionPhase.BLOCK
            blocked_reason = "crowd_no_chase_extension"
            trigger_event_code = "crowd_extension_blocked"
        elif reclaim and score >= resolved.go_score:
            state_code = "reclaim_confirmed"
            action = ActionPhase.GO
            trigger_event_code = "crowd_pullback_reclaim_confirmed"
            last_go_index = index
        elif pullback_index is not None and score >= resolved.arm_score:
            state_code = "pullback_armed"
            action = ActionPhase.ARM
            trigger_event_code = "crowd_pullback_zone_held"
        else:
            state_code = "trend_tracking"
            action = ActionPhase.WATCH if score >= resolved.watch_score else ActionPhase.WAIT
            trigger_event_code = "crowd_trend_alignment_confirmed"

        # ARM is setup context, not permission to enter before a closed-bar reclaim.
        # The shared candidate/lifecycle owners consume this single signal plan.
        trigger: float | None = None
        stop: float | None = None
        target: float | None = None
        if action is ActionPhase.GO:
            assert pullback_index is not None
            trigger, stop, target = _trade_plan(
                bars,
                index=index,
                pullback_index=pullback_index,
                direction=direction,
                atr=atr,
                params=resolved,
            )

        signal = SignalState(
            source=WISDOM_OF_CROWD_SOURCE,
            action=action,
            raw_action=action.value,
            direction=direction,
            score=score,
            confirmed=bool(bar.closed),
            source_tf=bar.timeframe,
            trigger=trigger,
            stop=stop,
            target=target,
            invalidation=stop,
            reason=trigger_event_code,
            blocked_reason=blocked_reason,
            code=(
                "CROWD_CONTINUATION_LONG"
                if direction is Direction.LONG
                else "CROWD_CONTINUATION_SHORT"
                if direction is Direction.SHORT
                else "CROWD_WAIT"
            ),
            kind=(ScenarioKind.TRANSIT if direction is not Direction.FLAT else ScenarioKind.WAIT),
        )
        signals.append(signal)
        facts = _typed_facts(
            direction=direction,
            state_code=state_code,
            trigger_event_code=trigger_event_code,
            price=float(bar.close),
            vwap=vwap,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            fast_slope_atr=fast_slope_atr,
            slow_slope_atr=slow_slope_atr,
            adx=adx,
            extension_atr=extension_atr,
            score=score,
            signal=signal,
        )
        row = {
            "ts": bar.ts.isoformat(),
            "source": WISDOM_OF_CROWD_SOURCE,
            "price": _round(float(bar.close)),
            "direction": direction.value,
            "regime_code": regime_code,
            "state_code": state_code,
            "action": action.value,
            "blocked_reason": blocked_reason,
            "code": signal.code,
            "score": _round(score, 2),
            "ema_fast": _round(ema_fast),
            "ema_slow": _round(ema_slow),
            "vwap": _round(vwap),
            "ema_fast_slope_atr": _round(fast_slope_atr, 4),
            "ema_slow_slope_atr": _round(slow_slope_atr, 4),
            "distance_fast_atr": _round((float(bar.close) - ema_fast) / atr, 4),
            "distance_vwap_atr": _round((float(bar.close) - vwap) / atr, 4),
            "extension_atr": _round(extension_atr, 4),
            "adx": _round(adx, 2),
            "structure_score": _round(structure_score, 2),
            "session_age_bars": session_ages[index],
            "pullback_age_bars": pullback_age,
            "pullback_level": _round(pullback_level),
            "reclaim_confirmed": reclaim,
            "trigger": _round(trigger),
            "stop": _round(stop),
            "target": _round(target),
            "rr": _round(resolved.reward_risk, 2) if trigger is not None else None,
            "signal": signal.as_dict(),
            "lifecycle": None,
            **facts,
        }
        series.append(row)
        previous_action = series[-2]["action"] if len(series) > 1 else "WAIT"
        if action in {ActionPhase.ARM, ActionPhase.GO} and (
            action is ActionPhase.GO or previous_action != action.value
        ):
            events.append(dict(row))

    lifecycle = lifecycle_from_signals(
        source=WISDOM_OF_CROWD_SOURCE,
        bars=bars,
        signals=signals,
        atr_values=block.atr_sma,
        max_age_bars=resolved.max_age_bars,
        trail_atr=resolved.trail_atr,
    )
    latest = series[-1] if series else None
    if latest is not None:
        latest["lifecycle"] = lifecycle.as_dict() if lifecycle is not None else None
    result.update(
        {
            "series": series,
            "events": events[-80:],
            "latest": latest,
            "overlays": build_overlays(
                bars,
                latest,
                events[-80:],
                labels=resolved.labels,
            ),
            "availability": {
                "state": "ready",
                "reason_code": "wisdom_of_crowd_ready",
                "required_bars": resolved.required_bars,
                "available_bars": len(bars),
                "vwap": vwap_session.status_payload(),
            },
        }
    )
    return result


__all__ = ["wisdom_of_crowd"]
