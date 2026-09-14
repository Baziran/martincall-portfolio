from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    previous_provider_session_levels,
    provider_session_active_minute,
)
from aef_terminal.features.vsa_contracts import VsaFact, event_direction
from aef_terminal.runtime import pine

TERMINAL_NONE = "none"
TERMINAL_EXH = "clx_exh"

TERMINAL_SCORE_FLOOR = 72.0
TERMINAL_MOVE_ATR = 0.45
TERMINAL_TREND_AGE = 8
TERMINAL_MOVE_STRONG = 0.65
TERMINAL_SPREAD_REL = 1.50
TERMINAL_RANGE_ATR = 1.0
TERMINAL_RVOL = 2.0
TERMINAL_RVOL_EXTREME = 2.4
TERMINAL_VOL_RANK = 92.0
TERMINAL_VOL_WINDOW = 30
TERMINAL_CONFIRM_RVOL_LOW = 1.05
TERMINAL_CONFIRM_RVOL_PUSH = 1.30
TERMINAL_CONFIRM_BODY_SHARE = 0.60
TERMINAL_CONFIRM_SPREAD_MAX = 0.95
TERMINAL_H1_BARS = 12
TERMINAL_LEVEL_ATR = 0.35
TERMINAL_VWAP_STDEV_MULT = 2.0
TERMINAL_FUEL_LOOKBACK_START = 3
TERMINAL_FUEL_LOOKBACK_END = 12

VSA_PRIMARY_CODES = frozenset(
    {
        "UPTHRUST",
        "SPRING",
        "EXH_DN",
        "EXH_UP",
        "ABS_LVL",
        "ABS",
        "FUEL_UP",
        "FUEL_DN",
        "IMP_UP",
        "IMP_DN",
    }
)

VSA_BREAKOUT_CONTEXT_CONTRACT = "vsa-breakout-context-v1"
VSA_BREAKOUT_POLICY_SPY = "spy-research-v1"
VSA_BREAKOUT_POLICY_QQQ = "qqq-research-v1"
VSA_BREAKOUT_POLICY_NEUTRAL = "neutral"


@dataclass(frozen=True)
class VsaBreakoutFact:
    """Causal VSA disposition for the exact next confirmed 5m bar."""

    index: int
    bar: Bar
    signal_index: int | None
    signal_ts: str
    signal_code: str
    prior_trend: str
    long_break: bool
    short_break: bool
    long_disposition: str
    short_disposition: str
    short_tier: str
    short_setup_type: str
    long_reason_code: str
    short_reason_code: str
    policy: str
    availability_state: str
    reason_code: str


def primary_code_from_fact(fact: VsaFact) -> str:
    if not fact.signal_allowed:
        return ""
    if fact.upthrust:
        return "UPTHRUST"
    if fact.spring:
        return "SPRING"
    if fact.up_exhaust:
        return "EXH_DN"
    if fact.dn_exhaust:
        return "EXH_UP"
    if fact.absorption_level:
        return "ABS_LVL"
    if fact.absorption:
        return "ABS"
    if fact.buy_fuel:
        return "FUEL_UP"
    if fact.sell_fuel:
        return "FUEL_DN"
    if fact.up_impulse:
        return "IMP_UP"
    if fact.dn_impulse:
        return "IMP_DN"
    return ""


def _bar_close_pos(bar: Bar) -> float:
    rng = max(bar.high - bar.low, 1e-9)
    return (bar.close - bar.low) / rng


def causal_provider_session_volume_ratio(
    bars: Sequence[Bar],
    session: ProviderSessionReset,
) -> list[float]:
    """Compare each bar with earlier observations at its provider-session minute."""

    slot_totals: dict[int, float] = defaultdict(float)
    slot_counts: dict[int, int] = defaultdict(int)
    ratios: list[float] = []
    for bar in bars:
        slot = provider_session_active_minute(session, bar.ts)
        volume = float(bar.volume)
        prior_count = slot_counts[slot]
        average = slot_totals[slot] / prior_count if prior_count else volume
        ratios.append(volume / max(average, 1.0))
        slot_totals[slot] += volume
        slot_counts[slot] = prior_count + 1
    return ratios


def vsa_prior_trend(
    bars: Sequence[Bar],
    atr_values: Sequence[float],
    ema20: Sequence[float],
    index: int,
) -> str:
    """Return the causal prior-trend label used by VSA breakout research."""

    if index < 12:
        return "unknown"
    move_atr = pine.safe_div(
        bars[index].close - bars[index - 12].close,
        max(float(atr_values[index]), 0.0001),
    )
    ema_slope = float(ema20[index]) - float(ema20[index - 5])
    if move_atr >= 0.45 and ema_slope > 0:
        return "uptrend"
    if move_atr <= -0.45 and ema_slope < 0:
        return "downtrend"
    if abs(move_atr) < 0.25:
        return "range"
    return "mixed"


def vsa_breakout_trend_alignment(trend: str, direction: str) -> str:
    if trend == "unknown":
        return "unknown"
    if direction == "long":
        if trend == "uptrend":
            return "with_trend"
        if trend == "downtrend":
            return "counter_trend"
        return "neutral"
    if trend == "downtrend":
        return "with_trend"
    if trend == "uptrend":
        return "counter_trend"
    return "neutral"


def vsa_breakout_policy(profile_key: str) -> str:
    normalized = str(profile_key or "").strip().upper()
    if normalized == "SPY":
        return VSA_BREAKOUT_POLICY_SPY
    if normalized == "QQQ":
        return VSA_BREAKOUT_POLICY_QQQ
    return VSA_BREAKOUT_POLICY_NEUTRAL


def vsa_breakout_facts(
    bars: Sequence[Bar],
    facts: Sequence[VsaFact],
    *,
    profile_key: str,
    vwap_session: ProviderSessionReset | None,
    confirmed_slots: Sequence[int] | None = None,
) -> list[VsaBreakoutFact]:
    """Build one replay-safe breakout fact for every confirmed parent bar."""

    if len(bars) != len(facts) or any(
        fact.index != index or fact.bar != bar
        for index, (fact, bar) in enumerate(zip(facts, bars, strict=False))
    ):
        raise ValueError("VSA breakout facts require the exact aligned VSA fact series")
    if confirmed_slots is not None and len(confirmed_slots) != len(bars):
        raise ValueError("VSA breakout facts require the exact aligned provider-slot series")
    slots = list(confirmed_slots) if confirmed_slots is not None else None
    policy = vsa_breakout_policy(profile_key)
    ema20 = pine.ema_series([bar.close for bar in bars], 20)
    atr_values = [fact.atr for fact in facts]
    out: list[VsaBreakoutFact] = []
    for index, bar in enumerate(bars):
        availability_state = "ready"
        reason_code = "ok"
        signal_index: int | None = index - 1 if index else None
        signal = facts[signal_index] if signal_index is not None else None
        signal_bar = bars[signal_index] if signal_index is not None else None
        exact_next_bar = bool(signal_bar is not None)
        if signal_bar is not None:
            if str(signal_bar.timeframe) != "5m" or str(bar.timeframe) != "5m":
                exact_next_bar = False
                availability_state = "unavailable"
                reason_code = "unsupported_parent_timeframe"
            elif vwap_session is None or not vwap_session.available:
                exact_next_bar = False
                availability_state = "blocked"
                reason_code = "provider_session_unavailable"
            elif slots is None:
                exact_next_bar = False
                availability_state = "blocked"
                reason_code = "provider_slot_axis_unavailable"
            elif vwap_session.key_for_bar(signal_bar) != vwap_session.key_for_bar(bar):
                exact_next_bar = False
                availability_state = "unavailable"
                reason_code = "next_bar_crosses_session"
            elif (bar.ts - signal_bar.ts).total_seconds() != 300.0 or int(slots[index]) - int(
                slots[signal_index]
            ) != 5:
                exact_next_bar = False
                availability_state = "unavailable"
                reason_code = "next_provider_slot_missing"
        else:
            availability_state = "warming"
            reason_code = "no_prior_bar"

        signal_code = (
            primary_code_from_fact(signal) if signal is not None and exact_next_bar else ""
        )
        prior_trend = (
            vsa_prior_trend(
                bars,
                atr_values,
                ema20,
                signal_index,
            )
            if signal_index is not None and exact_next_bar
            else "unknown"
        )
        long_break = bool(
            signal_code
            and signal_bar is not None
            and exact_next_bar
            and float(bar.high) > float(signal_bar.high)
        )
        short_break = bool(
            signal_code
            and signal_bar is not None
            and exact_next_bar
            and float(bar.low) < float(signal_bar.low)
        )
        long_disposition = "neutral"
        short_disposition = "neutral"
        short_tier = ""
        short_setup_type = ""
        long_reason_code = ""
        short_reason_code = ""
        policy_eligible = availability_state == "ready"
        if long_break and short_break:
            availability_state = "unavailable"
            reason_code = "ambiguous_two_sided_break"
            policy_eligible = False

        if (
            policy_eligible
            and policy == VSA_BREAKOUT_POLICY_SPY
            and long_break
            and signal_code in {"UPTHRUST", "ABS", "ABS_LVL"}
        ):
            long_disposition = "avoid"
            long_reason_code = f"vsa_spy_{signal_code.lower()}_long_break_avoid"
        if (
            policy_eligible
            and policy == VSA_BREAKOUT_POLICY_SPY
            and short_break
            and signal_code == "EXH_UP"
            and prior_trend == "downtrend"
        ):
            short_disposition = "supported"
            short_tier = "a"
            short_setup_type = "momentum_breakout"
            short_reason_code = "vsa_spy_exh_up_downtrend_short_break"
        elif (
            policy_eligible
            and policy == VSA_BREAKOUT_POLICY_SPY
            and short_break
            and signal_code == "ABS"
            and prior_trend == "uptrend"
        ):
            short_disposition = "supported"
            short_tier = "b"
            short_setup_type = "mean_reversion"
            short_reason_code = "vsa_spy_abs_uptrend_short_break"
        elif (
            policy_eligible
            and policy == VSA_BREAKOUT_POLICY_QQQ
            and short_break
            and signal_code == "EXH_DN"
            and prior_trend == "downtrend"
        ):
            short_disposition = "supported"
            short_tier = "a"
            short_setup_type = "momentum_breakout"
            short_reason_code = "vsa_qqq_exh_dn_downtrend_short_break"

        out.append(
            VsaBreakoutFact(
                index=index,
                bar=bar,
                signal_index=(signal_index if exact_next_bar and signal_code else None),
                signal_ts=(
                    signal_bar.ts.isoformat()
                    if (signal_bar is not None and exact_next_bar and signal_code)
                    else ""
                ),
                signal_code=signal_code,
                prior_trend=prior_trend,
                long_break=long_break,
                short_break=short_break,
                long_disposition=long_disposition,
                short_disposition=short_disposition,
                short_tier=short_tier,
                short_setup_type=short_setup_type,
                long_reason_code=long_reason_code,
                short_reason_code=short_reason_code,
                policy=policy,
                availability_state=availability_state,
                reason_code=reason_code,
            )
        )
    return out


def vsa_breakout_fact_payload(
    fact: VsaBreakoutFact,
) -> dict[str, Any]:
    return {
        "contract": VSA_BREAKOUT_CONTEXT_CONTRACT,
        "ts": fact.bar.ts.isoformat(),
        "signal_index": fact.signal_index,
        "signal_ts": fact.signal_ts,
        "signal_code": fact.signal_code,
        "prior_trend": fact.prior_trend,
        "long_break": fact.long_break,
        "short_break": fact.short_break,
        "long_disposition": fact.long_disposition,
        "short_disposition": fact.short_disposition,
        "short_tier": fact.short_tier,
        "short_setup_type": fact.short_setup_type,
        "long_reason_code": fact.long_reason_code,
        "short_reason_code": fact.short_reason_code,
        "policy": fact.policy,
        "availability_state": fact.availability_state,
        "reason_code": fact.reason_code,
        "authority": "context_only",
    }


def _vwap_distance_stdev(
    bars: Sequence[Bar], vwap: Sequence[float], length: int = 20
) -> list[float]:
    out: list[float] = []
    for index, bar in enumerate(bars):
        start = max(0, index - length + 1)
        window = [bars[i].close - vwap[i] for i in range(start, index + 1)]
        if len(window) < 2:
            out.append(0.0)
            continue
        mean = sum(window) / len(window)
        variance = sum((value - mean) ** 2 for value in window) / len(window)
        out.append(variance**0.5)
    return out


def _level_touch(bar: Bar, level: float, atr: float) -> bool:
    tolerance = atr * TERMINAL_LEVEL_ATR
    return bool(abs(bar.high - level) <= tolerance or (bar.high > level and bar.close < level))


def _level_support_touch(bar: Bar, level: float, atr: float) -> bool:
    tolerance = atr * TERMINAL_LEVEL_ATR
    return bool(abs(bar.low - level) <= tolerance or (bar.low < level and bar.close > level))


def _near_htf_resistance(
    bars: Sequence[Bar],
    index: int,
    *,
    atr: float,
    prev_high: float | None,
    h1_high: float | None,
    vwap_level: float | None,
    vwap_stdev: float,
) -> bool:
    bar = bars[index]
    if prev_high is not None and _level_touch(bar, prev_high, atr):
        return True
    if h1_high is not None and _level_touch(bar, h1_high, atr):
        return True
    if vwap_level is not None and vwap_stdev > 0:
        upper = vwap_level + vwap_stdev * TERMINAL_VWAP_STDEV_MULT
        if _level_touch(bar, upper, atr):
            return True
    return False


def _near_htf_support(
    bars: Sequence[Bar],
    index: int,
    *,
    atr: float,
    prev_low: float | None,
    h1_low: float | None,
    vwap_level: float | None,
    vwap_stdev: float,
) -> bool:
    bar = bars[index]
    if prev_low is not None and _level_support_touch(bar, prev_low, atr):
        return True
    if h1_low is not None and _level_support_touch(bar, h1_low, atr):
        return True
    if vwap_level is not None and vwap_stdev > 0:
        lower = vwap_level - vwap_stdev * TERMINAL_VWAP_STDEV_MULT
        if _level_support_touch(bar, lower, atr):
            return True
    return False


def _trend_ages(bars: Sequence[Bar], ema20: Sequence[float]) -> tuple[list[int], list[int]]:
    up_age = 0
    dn_age = 0
    up_ages: list[int] = []
    dn_ages: list[int] = []
    for index, bar in enumerate(bars):
        if index and bar.close > ema20[index] and bar.close >= bars[index - 1].close:
            up_age += 1
            dn_age = 0
        elif index and bar.close < ema20[index] and bar.close <= bars[index - 1].close:
            dn_age += 1
            up_age = 0
        else:
            up_age = 0
            dn_age = 0
        up_ages.append(up_age)
        dn_ages.append(dn_age)
    return up_ages, dn_ages


def _move_atr(
    bars: Sequence[Bar], atr_values: Sequence[float], index: int, lookback: int = 12
) -> float:
    if index < lookback:
        return 0.0
    atr = max(float(atr_values[index]), 1e-4)
    return (bars[index].close - bars[index - lookback].close) / atr


def _same_direction_fuel_in_lookback(
    facts: Sequence[VsaFact],
    index: int,
    *,
    fuel_up: bool,
    start: int,
    end: int,
) -> bool:
    for offset in range(start, end + 1):
        look = index - offset
        if look < 0:
            break
        source = facts[look]
        if fuel_up and source.buy_fuel:
            return True
        if not fuel_up and source.sell_fuel:
            return True
    return False


def _fuel_late_trend(fact: VsaFact, up_age: int, dn_age: int) -> bool:
    return bool(
        (fact.buy_fuel and up_age >= TERMINAL_TREND_AGE and not fact.upthrust)
        or (fact.sell_fuel and dn_age >= 4 and not fact.spring)
    )


def _ultra_volume(
    bars: Sequence[Bar],
    index: int,
    fact: VsaFact,
    *,
    tod_ratio: float,
) -> bool:
    if fact.vol_rank >= TERMINAL_VOL_RANK or fact.rvol >= TERMINAL_RVOL_EXTREME:
        return True
    if tod_ratio >= 2.0 and fact.rvol >= 1.8:
        return True
    if fact.rvol < TERMINAL_RVOL:
        return False
    start = max(0, index - TERMINAL_VOL_WINDOW + 1)
    window = [float(bars[i].volume) for i in range(start, index + 1)]
    return not window or float(bars[index].volume) >= max(window) * 0.95


def _classic_buying_climax_bar(fact: VsaFact, bar: Bar) -> bool:
    if not (
        fact.signal_allowed
        and fact.up_exhaust
        and bar.close >= bar.open
        and fact.upper_share >= 0.25
    ):
        return False
    wide_body = fact.body_share >= 0.30 and 0.45 <= fact.close_pos <= 0.82
    pin_reject = fact.body_share >= 0.18 and fact.close_pos <= 0.58 and fact.upper_share >= 0.35
    return bool(
        (wide_body or pin_reject)
        and fact.spread_rel >= TERMINAL_SPREAD_REL
        and fact.range_atr >= TERMINAL_RANGE_ATR
    )


def _classic_selling_climax_bar(fact: VsaFact, bar: Bar) -> bool:
    if not (
        fact.signal_allowed
        and fact.dn_exhaust
        and bar.close < bar.open
        and fact.lower_share >= 0.25
    ):
        return False
    wide_body = fact.body_share >= 0.30 and 0.18 <= fact.close_pos <= 0.55
    pin_reject = fact.body_share >= 0.18 and fact.close_pos >= 0.42 and fact.lower_share >= 0.35
    return bool(
        (wide_body or pin_reject)
        and fact.spread_rel >= TERMINAL_SPREAD_REL
        and fact.range_atr >= TERMINAL_RANGE_ATR
    )


def _confirm_pressure_release(
    confirm_bar: Bar, confirm_fact: VsaFact, *, buying_climax: bool
) -> bool:
    narrow = confirm_fact.spread_rel <= TERMINAL_CONFIRM_SPREAD_MAX
    low_volume = confirm_fact.rvol < TERMINAL_CONFIRM_RVOL_LOW
    if buying_climax:
        return bool(
            confirm_bar.close < confirm_bar.open
            and _bar_close_pos(confirm_bar) <= 0.40
            and narrow
            and low_volume
        )
    return bool(
        confirm_bar.close >= confirm_bar.open
        and _bar_close_pos(confirm_bar) >= 0.60
        and narrow
        and low_volume
    )


def _confirm_absorption_push(
    confirm_bar: Bar, confirm_fact: VsaFact, *, buying_climax: bool
) -> bool:
    if buying_climax:
        return bool(
            confirm_bar.close < confirm_bar.open
            and confirm_fact.rvol >= TERMINAL_CONFIRM_RVOL_PUSH
            and confirm_fact.body_share >= TERMINAL_CONFIRM_BODY_SHARE
            and _bar_close_pos(confirm_bar) <= 0.35
        )
    return bool(
        confirm_bar.close >= confirm_bar.open
        and confirm_fact.rvol >= TERMINAL_CONFIRM_RVOL_PUSH
        and confirm_fact.body_share >= TERMINAL_CONFIRM_BODY_SHARE
        and _bar_close_pos(confirm_bar) >= 0.65
    )


def _confirm_after_buying_climax(clx_bar: Bar, confirm_bar: Bar, confirm_fact: VsaFact) -> bool:
    _ = clx_bar
    return _confirm_pressure_release(
        confirm_bar, confirm_fact, buying_climax=True
    ) or _confirm_absorption_push(
        confirm_bar,
        confirm_fact,
        buying_climax=True,
    )


def _confirm_after_selling_climax(clx_bar: Bar, confirm_bar: Bar, confirm_fact: VsaFact) -> bool:
    _ = clx_bar
    return _confirm_pressure_release(
        confirm_bar, confirm_fact, buying_climax=False
    ) or _confirm_absorption_push(
        confirm_bar,
        confirm_fact,
        buying_climax=False,
    )


def _mature_uptrend(
    *,
    facts: Sequence[VsaFact],
    index: int,
    move: float,
    ema_slope: float,
    up_age: int,
    dn_age: int,
    fuel_late_trend: bool,
) -> bool:
    effort = bool(
        fuel_late_trend
        or _same_direction_fuel_in_lookback(
            facts,
            index,
            fuel_up=True,
            start=TERMINAL_FUEL_LOOKBACK_START,
            end=TERMINAL_FUEL_LOOKBACK_END,
        )
        or move >= TERMINAL_MOVE_STRONG
    )
    sustained = up_age >= TERMINAL_TREND_AGE or move >= TERMINAL_MOVE_STRONG
    return bool(move >= TERMINAL_MOVE_ATR and ema_slope > 0 and sustained and effort)


def _mature_downtrend(
    *,
    facts: Sequence[VsaFact],
    index: int,
    move: float,
    ema_slope: float,
    up_age: int,
    dn_age: int,
    fuel_late_trend: bool,
) -> bool:
    effort = bool(
        fuel_late_trend
        or _same_direction_fuel_in_lookback(
            facts,
            index,
            fuel_up=False,
            start=TERMINAL_FUEL_LOOKBACK_START,
            end=TERMINAL_FUEL_LOOKBACK_END,
        )
        or move <= -TERMINAL_MOVE_STRONG
    )
    sustained = dn_age >= TERMINAL_TREND_AGE or move <= -TERMINAL_MOVE_STRONG
    return bool(move <= -TERMINAL_MOVE_ATR and ema_slope < 0 and sustained and effort)


def _resolve_terminal_markers(
    facts: Sequence[VsaFact],
    bars: Sequence[Bar],
    *,
    up_ages: Sequence[int],
    dn_ages: Sequence[int],
    atr_values: Sequence[float],
    ema20: Sequence[float],
    prev_highs: Sequence[float | None],
    prev_lows: Sequence[float | None],
    vwap: Sequence[float],
    vwap_stdev: Sequence[float],
    tod_volume_ratio: Sequence[float],
    vwap_session: ProviderSessionReset,
    confirmed_slots: Sequence[int],
) -> dict[int, tuple[str, str]]:
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    terminal_by_index: dict[int, tuple[str, str]] = {}
    for index in range(1, len(facts)):
        clx_index = index - 1
        clx_fact = facts[clx_index]
        clx_bar = bars[clx_index]
        confirm_fact = facts[index]
        confirm_bar = bars[index]
        if (
            str(clx_bar.timeframe) != "5m"
            or str(confirm_bar.timeframe) != "5m"
            or (confirm_bar.ts - clx_bar.ts).total_seconds() != 300.0
            or int(confirmed_slots[index]) - int(confirmed_slots[clx_index]) != 5
            or vwap_session.key_for_bar(clx_bar) != vwap_session.key_for_bar(confirm_bar)
        ):
            continue
        atr = max(float(atr_values[clx_index]), 1e-4)
        if clx_fact.score < TERMINAL_SCORE_FLOOR:
            continue
        if not _ultra_volume(bars, clx_index, clx_fact, tod_ratio=tod_volume_ratio[clx_index]):
            continue

        move = _move_atr(bars, atr_values, clx_index)
        ema_slope = ema20[clx_index] - ema20[clx_index - 5] if clx_index >= 5 else 0.0
        fuel_late_trend = _fuel_late_trend(clx_fact, up_ages[clx_index], dn_ages[clx_index])
        h1_high = pine.rolling_high(highs, clx_index, TERMINAL_H1_BARS)
        h1_low = pine.rolling_low(lows, clx_index, TERMINAL_H1_BARS)

        if (
            _classic_buying_climax_bar(clx_fact, clx_bar)
            and (
                clx_fact.near_resistance
                or _near_htf_resistance(
                    bars,
                    clx_index,
                    atr=atr,
                    prev_high=prev_highs[clx_index],
                    h1_high=h1_high,
                    vwap_level=vwap[clx_index],
                    vwap_stdev=vwap_stdev[clx_index],
                )
            )
            and _mature_uptrend(
                facts=facts,
                index=clx_index,
                move=move,
                ema_slope=ema_slope,
                up_age=up_ages[clx_index],
                dn_age=dn_ages[clx_index],
                fuel_late_trend=fuel_late_trend,
            )
            and _confirm_after_buying_climax(clx_bar, confirm_bar, confirm_fact)
        ):
            terminal_by_index[index] = (TERMINAL_EXH, "short")
        elif (
            _classic_selling_climax_bar(clx_fact, clx_bar)
            and (
                clx_fact.near_support
                or _near_htf_support(
                    bars,
                    clx_index,
                    atr=atr,
                    prev_low=prev_lows[clx_index],
                    h1_low=h1_low,
                    vwap_level=vwap[clx_index],
                    vwap_stdev=vwap_stdev[clx_index],
                )
            )
            and _mature_downtrend(
                facts=facts,
                index=clx_index,
                move=move,
                ema_slope=ema_slope,
                up_age=up_ages[clx_index],
                dn_age=dn_ages[clx_index],
                fuel_late_trend=fuel_late_trend,
            )
            and _confirm_after_selling_climax(clx_bar, confirm_bar, confirm_fact)
        ):
            terminal_by_index[index] = (TERMINAL_EXH, "long")
    return terminal_by_index


def apply_terminal_markers(
    facts: list[VsaFact],
    bars: Sequence[Bar],
    *,
    vwap_session: ProviderSessionReset | None,
    confirmed_slots: Sequence[int] | None = None,
) -> list[VsaFact]:
    if not facts:
        return facts
    closes = [bar.close for bar in bars]
    ema20 = pine.ema_series(closes, 20)
    atr_values = [fact.atr for fact in facts]
    up_ages, dn_ages = _trend_ages(bars, ema20)
    if confirmed_slots is not None and len(confirmed_slots) != len(bars):
        raise ValueError("VSA terminal markers require the exact aligned provider-slot series")
    if vwap_session is None or not vwap_session.available or confirmed_slots is None:
        return [
            fact.__class__(
                **{
                    **fact.__dict__,
                    "fuel_late_trend": _fuel_late_trend(fact, up_ages[index], dn_ages[index]),
                }
            )
            for index, fact in enumerate(facts)
        ]
    prev_highs, prev_lows = previous_provider_session_levels(
        bars,
        vwap_session,
    )
    vwap = pine.vwap_series(bars, reset=vwap_session.key_for_bar)
    vwap_stdev = _vwap_distance_stdev(bars, vwap)
    tod_volume_ratio = causal_provider_session_volume_ratio(bars, vwap_session)
    terminal_by_index = _resolve_terminal_markers(
        facts,
        bars,
        up_ages=up_ages,
        dn_ages=dn_ages,
        atr_values=atr_values,
        ema20=ema20,
        prev_highs=prev_highs,
        prev_lows=prev_lows,
        vwap=vwap,
        vwap_stdev=vwap_stdev,
        tod_volume_ratio=tod_volume_ratio,
        vwap_session=vwap_session,
        confirmed_slots=confirmed_slots,
    )
    enriched: list[VsaFact] = []
    for index, fact in enumerate(facts):
        terminal_kind, terminal_direction = terminal_by_index.get(index, (TERMINAL_NONE, "flat"))
        fuel_late_trend = _fuel_late_trend(fact, up_ages[index], dn_ages[index])
        enriched.append(
            fact.__class__(
                **{
                    **fact.__dict__,
                    "terminal_climax": terminal_kind != TERMINAL_NONE,
                    "terminal_kind": terminal_kind,
                    "terminal_direction": terminal_direction,
                    "fuel_late_trend": fuel_late_trend,
                }
            )
        )
    return enriched


def vsa_role_for_fact(fact: VsaFact) -> str:
    if fact.terminal_climax:
        return "terminal_climax"
    if fact.buy_fuel or fact.sell_fuel:
        return "fuel"
    code = fact.primary_code
    if code in {"FUEL_UP", "FUEL_DN", "IMP_UP", "IMP_DN"}:
        return "impulse"
    if code in {"EXH_UP", "EXH_DN", "SPRING", "UPTHRUST"}:
        return "reversal_test"
    if code in {"ABS", "ABS_LVL"}:
        return "absorption"
    return "neutral"


def series_item_from_fact(fact: VsaFact, *, importance_floor: float = 72.0) -> dict[str, Any]:
    base_code = fact.primary_code
    terminal_code = (
        str(fact.terminal_kind or "").upper()
        if fact.terminal_climax and fact.terminal_kind != TERMINAL_NONE
        else ""
    )
    code = base_code or terminal_code
    fuel = bool(fact.buy_fuel or fact.sell_fuel)
    item = {
        "ts": fact.bar.ts.isoformat(),
        "rvol": round(fact.rvol, 4),
        "vol_rank": round(fact.vol_rank, 2),
        "vol_z": round(fact.vol_z, 4),
        "move_atr": round(fact.move_atr, 4),
        "range_atr": round(fact.range_atr, 4),
        "spread_rel": round(fact.spread_rel, 4),
        "score": round(fact.score, 2),
        "code": code,
        "base_code": base_code,
        "structure_text_ref": "vsa_volume",
        "structure_text_role": "vsa",
        "direction": fact.terminal_direction
        if fact.terminal_climax
        else (event_direction(code) if code else "flat"),
        "important": bool(fact.terminal_climax or (code and fact.score >= importance_floor)),
        "fuel": fuel,
        "fuel_late_trend": fact.fuel_late_trend,
        "terminal_climax": fact.terminal_climax,
        "terminal_kind": fact.terminal_kind,
        "terminal_direction": fact.terminal_direction,
        "vsa_role": vsa_role_for_fact(fact),
        "close_auction": fact.close_auction,
        "spring": fact.spring,
        "upthrust": fact.upthrust,
        "absorption": fact.absorption,
        "absorption_level": fact.absorption_level,
        "absorption_role": fact.absorption_role,
        "near_resistance": fact.near_resistance,
        "near_support": fact.near_support,
        "reason_code": (
            f"vsa_{str(fact.terminal_kind).lower()}"
            if fact.terminal_climax
            else f"vsa_{code.lower()}"
            if code
            else "vsa_wait"
        ),
    }
    return item


VSA_REVERSAL_SHORT_CODES = frozenset({"UPTHRUST", "EXH_DN"})
VSA_REVERSAL_LONG_CODES = frozenset({"SPRING", "EXH_UP"})
VSA_IMPULSE_CODES = frozenset({"FUEL_UP", "FUEL_DN", "IMP_UP", "IMP_DN"})
VSA_ABSORPTION_CODES = frozenset({"ABS", "ABS_LVL"})


def vsa_series_from_facts(
    facts: Sequence[VsaFact],
    *,
    importance_floor: float = 72.0,
) -> list[dict[str, Any]]:
    return [series_item_from_fact(fact, importance_floor=importance_floor) for fact in facts]


def latest_vsa_context_item(series: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not series:
        return {}
    for item in reversed(series):
        if item.get("code") or item.get("terminal_climax"):
            return item
    return series[-1]


def vsa_event_code(item: dict[str, Any] | None) -> str:
    return str((item or {}).get("code") or "").upper()


def bars_since_vsa_code(series: Sequence[dict[str, Any]], code: str) -> int | None:
    target = str(code or "").upper()
    for offset, item in enumerate(reversed(series)):
        if vsa_event_code(item) == target:
            return offset
    return None


def combined_reversal_short(*, vsa_code: str, context_code: str) -> bool:
    return vsa_code in VSA_REVERSAL_SHORT_CODES or context_code in {"NO_DEMAND", "VW_REJECT"}


def combined_reversal_long(*, vsa_code: str, context_code: str) -> bool:
    return vsa_code in VSA_REVERSAL_LONG_CODES or context_code in {"NO_SUPPLY", "VW_RECLAIM"}
