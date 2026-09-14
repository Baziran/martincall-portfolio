from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aef_terminal.domain import Bar
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    provider_session_bounds_for_bar,
    provider_session_intervals,
)
from aef_terminal.features.vsa_classify import combined_reversal_long, combined_reversal_short
from aef_terminal.runtime.math_utils import round_optional
from aef_terminal.runtime import pine
from aef_terminal.runtime.instruments import InstrumentProfile


_NEW_YORK_TIMEZONE = ZoneInfo("America/New_York")
_NEW_YORK_SESSION_OPEN = time(hour=9, minute=30)
_NEW_YORK_SESSION_CLOSE = time(hour=16)


def vwap_band_series(
    bars: Sequence[Bar],
    *,
    reset: Callable[[Bar], str],
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    sigmas: list[float] = []
    upper_1: list[float] = []
    lower_1: list[float] = []
    upper_2: list[float] = []
    lower_2: list[float] = []
    current_key: str | None = None
    volume_sum = 0.0
    pv_sum = 0.0
    p2v_sum = 0.0
    for bar in bars:
        key = reset(bar)
        if key != current_key:
            current_key = key
            volume_sum = 0.0
            pv_sum = 0.0
            p2v_sum = 0.0
        volume = bar.volume
        if volume > 0:
            typical = (bar.high + bar.low + bar.close) / 3.0
            volume_sum += volume
            pv_sum += typical * volume
            p2v_sum += typical * typical * volume
        base = pine.safe_div(pv_sum, volume_sum, bar.close) if volume_sum > 0 else bar.close
        variance = (
            max(pine.safe_div(p2v_sum, volume_sum, base * base) - base * base, 0.0)
            if volume_sum > 0
            else 0.0
        )
        sigma = variance**0.5
        sigmas.append(sigma)
        upper_1.append(base + sigma)
        lower_1.append(base - sigma)
        upper_2.append(base + sigma * 2.0)
        lower_2.append(base - sigma * 2.0)
    return sigmas, upper_1, lower_1, upper_2, lower_2


def true_count(values: Sequence[bool], length: int) -> int:
    if not values:
        return 0
    return sum(1 for value in values[-max(1, length) :] if value)


def micro_range_context(
    bars: Sequence[Bar],
    atr_values: Sequence[float],
    *,
    direction: str = "flat",
    context_ok: bool = False,
    length: int = 5,
    tight_atr: float = 0.90,
    min_tick: float = 0.01,
) -> dict[str, Any]:
    """Pine v51-style tight micro range watch/break helper."""

    length = max(2, int(length))
    if len(bars) <= length:
        return {
            "active": False,
            "ready": False,
            "direction": "flat",
            "watch": False,
            "break": False,
            "high": None,
            "low": None,
            "range_atr": None,
        }
    latest = bars[-1]
    atr = max(float(atr_values[-1]) if atr_values else 0.0, 0.000001)
    prior = bars[-length - 1 : -1]
    high = max(bar.high for bar in prior)
    low = min(bar.low for bar in prior)
    range_points = max(high - low, float(min_tick))
    range_atr = range_points / atr
    tight = range_points <= atr * max(float(tight_atr), 0.01)
    normalized_direction = str(direction or "flat").strip().lower()
    if normalized_direction not in {"long", "short"}:
        normalized_direction = "flat"
    bullish_break = (
        normalized_direction == "long" and latest.close > high and latest.close > latest.open
    )
    bearish_break = (
        normalized_direction == "short" and latest.close < low and latest.close < latest.open
    )
    ready = bool(context_ok and tight and normalized_direction != "flat")
    broken = bool(ready and latest.closed and (bullish_break or bearish_break))
    return {
        "active": bool(ready),
        "ready": bool(ready),
        "direction": normalized_direction if ready else "flat",
        "watch": bool(ready and not broken),
        "break": broken,
        "high": round_optional(high),
        "low": round_optional(low),
        "range_atr": round_optional(range_atr, 2),
        "length": length,
        "tight": bool(tight),
        "tight_atr": float(tight_atr),
    }


def side_veto_context(
    *,
    long_block_lock: bool = False,
    short_block_lock: bool = False,
    long_unlock: bool = False,
    short_unlock: bool = False,
    long_contra_allowed: bool = False,
    short_contra_allowed: bool = False,
) -> dict[str, Any]:
    """Directional side gate matching Linda v51's veto/unlock semantics."""

    veto_long = bool(long_block_lock and not long_unlock and not long_contra_allowed)
    veto_short = bool(short_block_lock and not short_unlock and not short_contra_allowed)
    if veto_long and not veto_short:
        preferred = "short"
    elif veto_short and not veto_long:
        preferred = "long"
    else:
        preferred = "flat"
    return {
        "veto_long": veto_long,
        "veto_short": veto_short,
        "long_block_lock": bool(long_block_lock),
        "short_block_lock": bool(short_block_lock),
        "long_unlock": bool(long_unlock),
        "short_unlock": bool(short_unlock),
        "long_contra_allowed": bool(long_contra_allowed),
        "short_contra_allowed": bool(short_contra_allowed),
        "preferred_direction": preferred,
    }


def poi_entry_gate_context(
    *,
    poi_active: bool = False,
    poi_direction: str = "flat",
    poi_hot: bool = False,
    long_bypass: bool = False,
    short_bypass: bool = False,
    long_confirm_trigger: bool = False,
    short_confirm_trigger: bool = False,
    long_warn_trigger: bool = False,
    short_warn_trigger: bool = False,
) -> dict[str, Any]:
    """Entry gate for active POI zones with explicit bypass triggers."""

    normalized_direction = str(poi_direction or "flat").strip().lower()
    if normalized_direction not in {"long", "short"}:
        normalized_direction = "flat"
    active = bool(poi_active)
    long_hot = bool(poi_hot and normalized_direction == "long")
    short_hot = bool(poi_hot and normalized_direction == "short")
    long_allowed = bool((not active) or long_hot or long_bypass)
    short_allowed = bool((not active) or short_hot or short_bypass)
    return {
        "active": active,
        "direction": normalized_direction if active else "flat",
        "hot": bool(poi_hot),
        "long": {
            "allowed": long_allowed,
            "hot": long_hot,
            "bypass": bool(long_bypass),
            "confirm": bool(long_allowed and long_confirm_trigger),
            "warn": bool(long_allowed and long_warn_trigger),
            "blocked_reason": "" if long_allowed else "poi_not_hot",
        },
        "short": {
            "allowed": short_allowed,
            "hot": short_hot,
            "bypass": bool(short_bypass),
            "confirm": bool(short_allowed and short_confirm_trigger),
            "warn": bool(short_allowed and short_warn_trigger),
            "blocked_reason": "" if short_allowed else "poi_not_hot",
        },
    }


def strategy_banner_context(
    *,
    execute_long: bool = False,
    execute_short: bool = False,
    armed_long: bool = False,
    armed_short: bool = False,
    entry_long_confirm: bool = False,
    entry_short_confirm: bool = False,
    impulse_confirmed: bool = False,
    impulse_developing: bool = False,
    impulse_direction: str = "flat",
    impulse_class: str = "off",
    impulse_action: str = "wait",
    price: float | None = None,
    flow_bias: int | float = 0,
) -> dict[str, Any]:
    """Reusable GO/ARM/IMP banner state for indicator decision tables."""

    direction = "flat"
    phase = "off"
    directive = "wait_for_context"
    normalized_impulse_direction = str(impulse_direction or "flat").strip().lower()
    if normalized_impulse_direction not in {"long", "short"}:
        normalized_impulse_direction = "flat"
    if execute_long or execute_short:
        direction = "long" if execute_long else "short"
        phase = "execute"
        directive = "enter_from_level_retest_if_stretched"
    elif armed_long or armed_short or (entry_long_confirm != entry_short_confirm):
        direction = "long" if armed_long or entry_long_confirm else "short"
        phase = "armed"
        directive = "prepare_plan_wait_confirmation"
    elif impulse_confirmed:
        direction = normalized_impulse_direction
        phase = "impulse_confirmed"
        directive = str(impulse_action or "wait_pullback")
    elif impulse_developing:
        direction = normalized_impulse_direction
        phase = "impulse_developing"
        directive = "wait_closed_bar_confirmation"
    return {
        "active": phase != "off",
        "phase": phase,
        "direction": direction if direction in {"long", "short", "flat"} else "flat",
        "directive": directive,
        "price": round_optional(price),
        "flow_bias": flow_bias,
        "entry_confirm": {"long": bool(entry_long_confirm), "short": bool(entry_short_confirm)},
        "execute": {"long": bool(execute_long), "short": bool(execute_short)},
        "armed": {"long": bool(armed_long), "short": bool(armed_short)},
        "impulse": {
            "confirmed": bool(impulse_confirmed),
            "developing": bool(impulse_developing),
            "class": impulse_class,
            "directive": impulse_action,
        },
    }


def provider_opening_range_context(
    bars: Sequence[Bar],
    *,
    provider_session: ProviderSessionReset | None,
    duration_minutes: int = 30,
    max_timeframe_minutes: int = 30,
) -> dict[str, Any]:
    unavailable = {
        "active": False,
        "ready": False,
        "high": None,
        "low": None,
        "day": None,
    }
    if not bars:
        return {**unavailable, "reason_code": "provider_session_bars_missing"}
    if provider_session is None or not provider_session.available:
        return {
            **unavailable,
            "reason_code": (
                provider_session.reason_code
                if provider_session is not None
                else "provider_session_missing"
            ),
        }
    latest = bars[-1]
    tf_minutes = timeframe_minutes(latest.timeframe)
    try:
        session_key = provider_session.key_for_bar(latest)
    except ValueError as exc:
        return {**unavailable, "reason_code": str(exc)}
    if tf_minutes > max_timeframe_minutes:
        return {
            **unavailable,
            "supported": False,
            "in_opening": False,
            "day": session_key,
            "reason_code": "opening_range_timeframe_unsupported",
        }
    bounds = provider_session_bounds_for_bar(provider_session, latest)
    if bounds is None:
        return {
            **unavailable,
            "supported": True,
            "in_opening": False,
            "day": session_key,
            "reason_code": "provider_session_bounds_unavailable",
        }
    start_at, session_close = bounds
    end_at = min(
        start_at + timedelta(minutes=max(int(duration_minutes), 1)),
        session_close,
    )
    current_session_bars: list[Bar] = []
    for bar in bars:
        try:
            if provider_session.key_for_bar(bar) == session_key:
                current_session_bars.append(bar)
        except ValueError:
            continue
    opening_bars = [bar for bar in current_session_bars if start_at <= bar.ts < end_at]
    in_opening = start_at <= latest.ts < end_at
    if not opening_bars:
        return {
            **unavailable,
            "supported": True,
            "in_opening": in_opening,
            "day": session_key,
            "reason_code": "provider_opening_range_bars_missing",
        }
    high = max(bar.high for bar in opening_bars)
    low = min(bar.low for bar in opening_bars)
    ready = latest.ts + timedelta(minutes=max(tf_minutes, 1)) >= end_at
    previous = current_session_bars[-2] if len(current_session_bars) >= 2 else latest
    above = latest.close > high
    below = latest.close < low
    inside = low <= latest.close <= high
    break_up = previous.close <= high < latest.close
    break_down = previous.close >= low > latest.close
    bars_since_up: int | None = None
    bars_since_down: int | None = None
    bars_since_inside: int | None = None
    for offset, bar in enumerate(reversed(current_session_bars)):
        prior_index = len(current_session_bars) - 1 - offset - 1
        if (
            bars_since_up is None
            and bar.close > high
            and prior_index >= 0
            and current_session_bars[prior_index].close <= high
        ):
            bars_since_up = offset
        if (
            bars_since_down is None
            and bar.close < low
            and prior_index >= 0
            and current_session_bars[prior_index].close >= low
        ):
            bars_since_down = offset
        if bars_since_inside is None and low <= bar.close <= high:
            bars_since_inside = offset
        if (
            bars_since_up is not None
            and bars_since_down is not None
            and bars_since_inside is not None
        ):
            break
    runaway_window = max(16, min(480, round(480 / max(tf_minutes, 1))))
    no_retest_up = bool(
        above
        and bars_since_up is not None
        and bars_since_up <= runaway_window
        and (bars_since_inside is None or bars_since_inside > bars_since_up)
    )
    no_retest_down = bool(
        below
        and bars_since_down is not None
        and bars_since_down <= runaway_window
        and (bars_since_inside is None or bars_since_inside > bars_since_down)
    )
    return {
        "active": True,
        "ready": bool(ready),
        "supported": True,
        "in_opening": bool(in_opening),
        "day": session_key,
        "high": round_optional(high),
        "low": round_optional(low),
        "mid": round_optional((high + low) / 2.0),
        "session": "provider_opening_range",
        "timezone": "UTC",
        "start_ts": start_at.isoformat(),
        "end_ts": end_at.isoformat(),
        "duration_minutes": (end_at - start_at).total_seconds() / 60.0,
        "source": provider_session.source,
        "reason_code": "",
        "above": bool(above),
        "below": bool(below),
        "inside": bool(inside),
        "break_up": bool(break_up),
        "break_down": bool(break_down),
        "bars_since_break_up": bars_since_up,
        "bars_since_break_down": bars_since_down,
        "no_retest_up": no_retest_up,
        "no_retest_down": no_retest_down,
    }


def provider_new_york_range_context(
    bars: Sequence[Bar],
    *,
    instrument: Mapping[str, Any] | None,
    duration_minutes: int = 30,
    max_timeframe_minutes: int = 30,
) -> dict[str, Any]:
    """Return the provider-confirmed New York cash-session opening range.

    The named 09:30-16:00 America/New_York display window is admitted only when
    the provider's liquid schedule contains its opening timestamp.  The exact
    provider close may shorten the display window, while a later provider close
    never extends it past 16:00 New York time.
    """

    unavailable = {
        "active": False,
        "ready": False,
        "high": None,
        "low": None,
        "day": None,
    }
    if not bars:
        return {**unavailable, "reason_code": "new_york_range_bars_missing"}

    latest = bars[-1]
    timeframe = timeframe_minutes(latest.timeframe)
    session_day = latest.ts.astimezone(_NEW_YORK_TIMEZONE).date()
    if timeframe > max_timeframe_minutes:
        return {
            **unavailable,
            "supported": False,
            "in_opening": False,
            "day": session_day.isoformat(),
            "reason_code": "new_york_range_timeframe_unsupported",
        }

    session_start = datetime.combine(
        session_day,
        _NEW_YORK_SESSION_OPEN,
        tzinfo=_NEW_YORK_TIMEZONE,
    ).astimezone(UTC)
    scheduled_close = datetime.combine(
        session_day,
        _NEW_YORK_SESSION_CLOSE,
        tzinfo=_NEW_YORK_TIMEZONE,
    ).astimezone(UTC)
    liquid_intervals = provider_session_intervals(instrument, "liquid_intervals")
    matching_intervals = [
        interval
        for interval in liquid_intervals
        if interval.session_date == session_day and interval.contains(session_start)
    ]
    if len(matching_intervals) != 1:
        return {
            **unavailable,
            "supported": True,
            "in_opening": False,
            "day": session_day.isoformat(),
            "reason_code": (
                "provider_new_york_liquid_interval_missing"
                if not matching_intervals
                else "provider_new_york_liquid_interval_ambiguous"
            ),
        }

    provider_interval = matching_intervals[0]
    session_end = min(scheduled_close, provider_interval.closes_at)
    if session_end <= session_start:
        return {
            **unavailable,
            "supported": True,
            "in_opening": False,
            "day": session_day.isoformat(),
            "reason_code": "provider_new_york_liquid_interval_invalid",
        }

    opening_end = min(
        session_start + timedelta(minutes=max(int(duration_minutes), 1)),
        session_end,
    )
    opening_bars = [bar for bar in bars if session_start <= bar.ts < opening_end]
    in_opening = session_start <= latest.ts < opening_end
    if not opening_bars:
        return {
            **unavailable,
            "supported": True,
            "in_opening": in_opening,
            "day": session_day.isoformat(),
            "reason_code": "provider_new_york_opening_bars_missing",
        }

    high = max(bar.high for bar in opening_bars)
    low = min(bar.low for bar in opening_bars)
    ready = latest.ts + timedelta(minutes=max(timeframe, 1)) >= opening_end
    return {
        "active": True,
        "ready": bool(ready),
        "supported": True,
        "in_opening": bool(in_opening),
        "day": session_day.isoformat(),
        "high": round_optional(high),
        "low": round_optional(low),
        "mid": round_optional((high + low) / 2.0),
        "session": "new_york_cash",
        "timezone": _NEW_YORK_TIMEZONE.key,
        "start_ts": session_start.isoformat(),
        "end_ts": opening_end.isoformat(),
        "session_end_ts": session_end.isoformat(),
        "duration_minutes": (opening_end - session_start).total_seconds() / 60.0,
        "source": "provider_liquid_intervals",
        "reason_code": "",
    }


def impulse_poi_context(
    bars: Sequence[Bar],
    *,
    impulse_confirmed: Sequence[bool],
    impulse_aligned: Sequence[bool],
    impulse_fuel: Sequence[bool],
    vwap_values: Sequence[float],
    runaway_flags: Sequence[bool] | None = None,
    expire_bars: int = 30,
) -> dict[str, Any]:
    if not bars:
        return {"active": False, "direction": "flat", "core_top": None, "core_bottom": None}
    latest_index = len(bars) - 1
    runaway_flags = runaway_flags or []
    source_index: int | None = None
    for index in range(latest_index, -1, -1):
        if latest_index - index > expire_bars:
            break
        confirmed = bool(impulse_confirmed[index]) if index < len(impulse_confirmed) else False
        aligned = bool(impulse_aligned[index]) if index < len(impulse_aligned) else False
        fuel_bar = bool(impulse_fuel[index]) if index < len(impulse_fuel) else False
        if confirmed and aligned and not fuel_bar:
            source_index = index
            break
    if source_index is None:
        return {"active": False, "direction": "flat", "core_top": None, "core_bottom": None}

    impulse_bar = bars[source_index]
    latest = bars[-1]
    direction = "long" if impulse_bar.close >= impulse_bar.open else "short"
    spread = max(impulse_bar.high - impulse_bar.low, 0.000001)
    shallow = bool(runaway_flags[source_index]) if source_index < len(runaway_flags) else False
    core_a = 0.236 if shallow else 0.5
    core_b = 0.382 if shallow else 0.618
    if direction == "long":
        z_a = impulse_bar.high - spread * core_a
        z_b = impulse_bar.high - spread * core_b
        z_deep_a = impulse_bar.high - spread * 0.618
        z_deep_b = impulse_bar.high - spread * 0.786
    else:
        z_a = impulse_bar.low + spread * core_a
        z_b = impulse_bar.low + spread * core_b
        z_deep_a = impulse_bar.low + spread * 0.618
        z_deep_b = impulse_bar.low + spread * 0.786
    core_top = max(z_a, z_b)
    core_bottom = min(z_a, z_b)
    deep_top = max(z_deep_a, z_deep_b)
    deep_bottom = min(z_deep_a, z_deep_b)
    atr = max((impulse_bar.high - impulse_bar.low) * 0.25, 0.000001)
    vwap = float(vwap_values[source_index]) if source_index < len(vwap_values) else None
    if vwap is not None and core_bottom - atr * 0.2 <= vwap <= core_top + atr * 0.2:
        core_top = max(core_top, vwap)
        core_bottom = min(core_bottom, vwap)
    zone_top = max(core_top, deep_top)
    zone_bottom = min(core_bottom, deep_bottom)
    in_core = latest.low <= core_top and latest.high >= core_bottom
    in_deep = latest.low <= deep_top and latest.high >= deep_bottom
    invalid = (direction == "long" and latest.close < zone_bottom) or (
        direction == "short" and latest.close > zone_top
    )
    active = not invalid and latest_index - source_index <= expire_bars
    return {
        "active": bool(active),
        "direction": direction if active else "flat",
        "source_index": source_index,
        "age_bars": latest_index - source_index,
        "core_top": round_optional(core_top),
        "core_bottom": round_optional(core_bottom),
        "deep_top": round_optional(deep_top),
        "deep_bottom": round_optional(deep_bottom),
        "top": round_optional(zone_top),
        "bottom": round_optional(zone_bottom),
        "in_core": bool(active and in_core),
        "in_deep": bool(active and in_deep),
        "hot": bool(active and (in_core or in_deep)),
        "invalid": bool(invalid),
        "shallow": bool(shallow),
        "expire_bars": expire_bars,
    }


def liquidity_void_context(
    bars: Sequence[Bar],
    atr_values: Sequence[float],
    rvol_values: Sequence[float],
    *,
    fvg_age_bars: int = 8,
    void_stale_bars: int = 40,
    sweep_lookback: int = 8,
    min_void_atr: float = 0.20,
    min_rvol: float = 1.05,
    min_tick: float = 0.01,
) -> dict[str, Any]:
    if len(bars) < 3:
        return {
            "long_context": False,
            "short_context": False,
            "bull_fvg_age": None,
            "bear_fvg_age": None,
            "bull_void_age": None,
            "bear_void_age": None,
            "price_in_void": False,
            "void_direction": "flat",
            "void_top": None,
            "void_bottom": None,
            "sweep_low": False,
            "sweep_high": False,
            "reclaim_long": False,
            "reclaim_short": False,
            "reason_codes": ["warmup"],
        }

    last_bull_fvg: int | None = None
    last_bear_fvg: int | None = None
    last_bull_void: int | None = None
    last_bear_void: int | None = None
    active_void_dir = 0
    active_void_top: float | None = None
    active_void_bottom: float | None = None
    active_void_start: int | None = None

    for index in range(2, len(bars)):
        bar = bars[index]
        prev = bars[index - 1]
        two_back = bars[index - 2]
        atr = max(float(atr_values[index]) if index < len(atr_values) else 0.0, 0.000001)
        rvol = float(rvol_values[index]) if index < len(rvol_values) else 0.0
        void_min = max(atr * min_void_atr, min_tick * 3.0)

        bull_fvg = bar.low > two_back.high and prev.close > two_back.high
        bear_fvg = bar.high < two_back.low and prev.close < two_back.low
        if bull_fvg:
            last_bull_fvg = index
            gap = bar.low - two_back.high
            directional_confirm = bar.close >= bar.open or bar.close > prev.close
            if gap >= void_min and rvol >= min_rvol and directional_confirm:
                last_bull_void = index
                active_void_dir = 1
                active_void_bottom = two_back.high
                active_void_top = bar.low
                active_void_start = index
        if bear_fvg:
            last_bear_fvg = index
            gap = two_back.low - bar.high
            directional_confirm = bar.close < bar.open or bar.close < prev.close
            if gap >= void_min and rvol >= min_rvol and directional_confirm:
                last_bear_void = index
                active_void_dir = -1
                active_void_bottom = bar.high
                active_void_top = two_back.low
                active_void_start = index

        if active_void_dir and active_void_start is not None and active_void_start != index:
            stale = index - active_void_start > void_stale_bars
            filled = (
                active_void_dir > 0
                and active_void_bottom is not None
                and bar.low <= active_void_bottom
            ) or (
                active_void_dir < 0 and active_void_top is not None and bar.high >= active_void_top
            )
            if stale or filled:
                active_void_dir = 0
                active_void_top = None
                active_void_bottom = None
                active_void_start = None

    latest_index = len(bars) - 1
    latest = bars[-1]
    prev = bars[-2]
    latest_rvol = float(rvol_values[-1]) if rvol_values else 0.0
    prior_window = bars[max(0, latest_index - sweep_lookback) : latest_index]
    sweep_low = (
        len(prior_window) >= max(2, min(3, sweep_lookback))
        and latest.low < min(bar.low for bar in prior_window)
        and latest.close > prev.low
        and latest_rvol >= min_rvol
    )
    sweep_high = (
        len(prior_window) >= max(2, min(3, sweep_lookback))
        and latest.high > max(bar.high for bar in prior_window)
        and latest.close < prev.high
        and latest_rvol >= min_rvol
    )
    price_in_void = (
        active_void_dir != 0
        and active_void_bottom is not None
        and active_void_top is not None
        and active_void_bottom <= latest.close <= active_void_top
    )
    bull_fvg_age = latest_index - last_bull_fvg if last_bull_fvg is not None else None
    bear_fvg_age = latest_index - last_bear_fvg if last_bear_fvg is not None else None
    bull_void_age = latest_index - last_bull_void if last_bull_void is not None else None
    bear_void_age = latest_index - last_bear_void if last_bear_void is not None else None
    fresh_bull_fvg = bull_fvg_age is not None and bull_fvg_age <= fvg_age_bars
    fresh_bear_fvg = bear_fvg_age is not None and bear_fvg_age <= fvg_age_bars
    fresh_bull_void = bull_void_age is not None and bull_void_age <= fvg_age_bars
    fresh_bear_void = bear_void_age is not None and bear_void_age <= fvg_age_bars
    long_context = (
        fresh_bull_fvg or fresh_bull_void or (price_in_void and active_void_dir > 0) or sweep_low
    )
    short_context = (
        fresh_bear_fvg or fresh_bear_void or (price_in_void and active_void_dir < 0) or sweep_high
    )
    reclaim_long = sweep_low and latest.close >= latest.open
    reclaim_short = sweep_high and latest.close < latest.open
    reason_codes = [
        "bull_fvg" if fresh_bull_fvg else "",
        "bear_fvg" if fresh_bear_fvg else "",
        "bull_void" if fresh_bull_void else "",
        "bear_void" if fresh_bear_void else "",
        "inside_void" if price_in_void else "",
        "sell_side_sweep" if sweep_low else "",
        "buy_side_sweep" if sweep_high else "",
    ]
    active_reason_codes = [code for code in reason_codes if code] or ["no_fresh_liquidity_context"]
    return {
        "long_context": bool(long_context),
        "short_context": bool(short_context),
        "bull_fvg_age": bull_fvg_age,
        "bear_fvg_age": bear_fvg_age,
        "bull_void_age": bull_void_age,
        "bear_void_age": bear_void_age,
        "price_in_void": bool(price_in_void),
        "void_direction": "long"
        if active_void_dir > 0
        else "short"
        if active_void_dir < 0
        else "flat",
        "void_top": round_optional(active_void_top),
        "void_bottom": round_optional(active_void_bottom),
        "sweep_low": bool(sweep_low),
        "sweep_high": bool(sweep_high),
        "reclaim_long": bool(reclaim_long),
        "reclaim_short": bool(reclaim_short),
        "reason_codes": active_reason_codes,
    }


def vix_risk_context(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    features: dict[str, Any] | None = None,
    atr_values: Sequence[float] | None = None,
    rvol_values: Sequence[float] | None = None,
) -> dict[str, Any]:
    if not bars:
        return {
            "active": False,
            "valid": False,
            "state": "n/a",
            "risk_off": False,
            "risk_on": False,
            "shock": False,
        }
    active = profile.key in {"ES", "SPY", "QQQ", "SPX_INDEX"}
    if not active:
        return {
            "active": False,
            "valid": False,
            "state": "off",
            "risk_off": False,
            "risk_on": False,
            "shock": False,
        }

    features = features or {}
    explicit_state = str(features.get("vix_state") or "").strip().lower()
    if explicit_state:
        risk_off = explicit_state in {
            "risk-off",
            "risk-off shock",
            "vix hedge",
            "vix up",
            "hedge",
            "up",
        }
        risk_on = explicit_state in {"risk-on", "vix relief", "relief", "down"}
        shock = explicit_state in {"vix hedge", "shock", "risk-off shock"}
        return {
            "active": True,
            "valid": risk_off or risk_on or explicit_state in {"flat", "neutral", "vix neutral"},
            "state": "risk-off" if risk_off else "risk-on" if risk_on else "flat",
            "risk_off": risk_off,
            "risk_on": risk_on,
            "shock": shock,
            "proxy": False,
            "source": "features",
        }

    price = _float_feature(features, "vix_price")
    previous = _float_feature(features, "vix_previous")
    ema = _float_feature(features, "vix_ema")
    sma = _float_feature(features, "vix_sma")
    stdev = _float_feature(features, "vix_stdev")
    valid = price is not None and ema is not None
    z_score = (
        (price - sma) / max(stdev or 0.0, 0.01)
        if price is not None and sma is not None and stdev is not None
        else None
    )
    pct_change = (
        (price - previous) / max(previous, 0.01)
        if price is not None and previous is not None
        else None
    )
    risk_off = bool(
        valid
        and price is not None
        and ema is not None
        and price > ema
        and (previous is None or price > previous)
    )
    risk_on = bool(
        valid
        and price is not None
        and ema is not None
        and price < ema
        and (previous is None or price < previous)
    )
    shock = bool(
        valid
        and (
            (z_score is not None and z_score > 1.0)
            or (pct_change is not None and pct_change > 0.025)
        )
    )
    return {
        "active": True,
        "valid": bool(valid),
        "state": "risk-off" if risk_off else "risk-on" if risk_on else "flat" if valid else "n/a",
        "risk_off": risk_off,
        "risk_on": risk_on,
        "shock": shock,
        "price": round_optional(price),
        "previous": round_optional(previous),
        "ema": round_optional(ema),
        "z": round_optional(z_score),
        "pct_change": round_optional(pct_change),
        "proxy": False,
        "source": "features" if valid else "none",
    }


def trap_shift_context(
    bars: Sequence[Bar],
    atr_values: Sequence[float],
    rvol_values: Sequence[float],
    *,
    full_bull: bool,
    full_bear: bool,
    mtf_direction: str = "flat",
    liquidity: dict[str, Any] | None = None,
    latest_code: str = "",
    vsa_code: str = "",
    breakout_lookback: int = 8,
    min_rvol: float = 2.0,
    min_range_atr: float = 1.05,
) -> dict[str, Any]:
    if len(bars) < breakout_lookback + 2:
        return {
            "active": False,
            "ready": False,
            "execution": False,
            "direction": "flat",
            "reason_code": "warmup",
        }
    latest = bars[-1]
    atr = max(float(atr_values[-1]) if atr_values else 0.0, 0.000001)
    rvol = float(rvol_values[-1]) if rvol_values else 0.0
    anatomy = pine.bar_anatomy(latest)
    is_up = latest.close >= latest.open
    range_atr = (latest.high - latest.low) / atr
    directional_close = anatomy.close_pos >= 0.62 if is_up else anatomy.close_pos <= 0.38
    prior = bars[-breakout_lookback - 1 : -1]
    previous_high = max(bar.high for bar in prior)
    previous_low = min(bar.low for bar in prior)
    breakout_up = latest.high > previous_high and latest.close > previous_high
    breakout_down = latest.low < previous_low and latest.close < previous_low
    liquidity = liquidity or {}
    sweep_break = bool(liquidity.get("sweep_high")) if is_up else bool(liquidity.get("sweep_low"))
    mtf_dir = str(mtf_direction or "flat")
    impulse_like = (
        rvol >= min_rvol
        and range_atr >= min_range_atr
        and directional_close
        and (breakout_up or breakout_down or sweep_break)
    )
    against_context = (is_up and (full_bear or mtf_dir == "short")) or (
        (not is_up) and (full_bull or mtf_dir == "long")
    )
    aligned = (is_up and full_bull and mtf_dir != "short") or (
        (not is_up) and full_bear and mtf_dir != "long"
    )
    trap = impulse_like and against_context and not aligned
    trap_up = trap and is_up and latest.high > previous_high and latest.close < previous_high
    trap_down = trap and (not is_up) and latest.low < previous_low and latest.close > previous_low
    reversal_code_short = combined_reversal_short(
        vsa_code=str(vsa_code or "").upper(), context_code=str(latest_code or "").upper()
    )
    reversal_code_long = combined_reversal_long(
        vsa_code=str(vsa_code or "").upper(), context_code=str(latest_code or "").upper()
    )
    direction = "short" if trap and is_up else "long" if trap else "flat"
    execution = bool(
        (direction == "short" and reversal_code_short)
        or (direction == "long" and reversal_code_long)
    )
    level = (
        previous_high
        if trap_up
        else previous_low
        if trap_down
        else latest.high
        if direction == "short"
        else latest.low
        if direction == "long"
        else None
    )
    return {
        "active": bool(trap),
        "ready": bool((trap_up or trap_down) or execution),
        "execution": execution,
        "direction": direction,
        "trap_up": bool(trap_up),
        "trap_down": bool(trap_down),
        "level": round_optional(level),
        "rvol": round_optional(rvol),
        "range_atr": round_optional(range_atr),
        "against_context": bool(against_context),
        "reason_code": "impulse_against_context" if trap else "no_impulse_trap_shift",
    }


def _float_feature(features: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in features:
            continue
        try:
            return float(features[key])
        except TypeError, ValueError:
            continue
    return None


def timeframe_minutes(timeframe: str) -> int:
    text = (timeframe or "5m").strip().lower()
    try:
        if text.endswith("m"):
            return max(1, int(text[:-1] or "1"))
        if text.endswith("h"):
            return max(1, int(text[:-1] or "1") * 60)
        if text.endswith("d"):
            return max(1, int(text[:-1] or "1") * 1440)
        return max(1, int(text))
    except ValueError:
        return 5


def mtf_trend_context(
    bars: Sequence[Bar],
    *,
    vwap_reset: Callable[[Bar], str],
) -> dict[str, Any]:
    tf_minutes = timeframe_minutes(bars[-1].timeframe) if bars else 5
    if not bars or tf_minutes > 15:
        return {
            "active": False,
            "direction": "flat",
            "bull_score": 0,
            "bear_score": 0,
            "filter_bull_ok": True,
            "filter_bear_ok": True,
            "reason_code": "inactive_timeframe",
        }
    m15_bars = [
        bar
        for bar in pine.collapse_bars(
            bars,
            "15m",
            symbol=bars[-1].symbol,
            source_name=bars[-1].source,
        )
        if bar.closed
    ]
    if len(m15_bars) < 4:
        return {
            "active": True,
            "direction": "flat",
            "bull_score": 0,
            "bear_score": 0,
            "filter_bull_ok": True,
            "filter_bear_ok": True,
            "reason_code": "warmup",
        }
    closes = [bar.close for bar in m15_bars]
    ema = pine.ema_series(closes, 20)
    vwap = pine.vwap_series(m15_bars, reset=vwap_reset)
    cur = m15_bars[-1]
    prev = m15_bars[-2]
    prev_mid = (prev.high + prev.low) / 2.0
    ema_prev2 = ema[-3]
    vwap_prev2 = vwap[-3]
    bull_score = (
        int(cur.close > vwap[-1])
        + int(cur.close > ema[-1])
        + int(ema[-1] > ema_prev2)
        + int(vwap[-1] > vwap_prev2)
        + int(cur.close > prev_mid or cur.high > prev.high)
    )
    bear_score = (
        int(cur.close < vwap[-1])
        + int(cur.close < ema[-1])
        + int(ema[-1] < ema_prev2)
        + int(vwap[-1] < vwap_prev2)
        + int(cur.close < prev_mid or cur.low < prev.low)
    )
    direction = "long" if bull_score >= 4 else "short" if bear_score >= 4 else "flat"
    return {
        "active": True,
        "direction": direction,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "filter_bull_ok": direction != "short",
        "filter_bear_ok": direction != "long",
        "close": round_optional(cur.close),
        "vwap": round_optional(vwap[-1]),
        "ema": round_optional(ema[-1]),
        "reason_code": "score",
    }
