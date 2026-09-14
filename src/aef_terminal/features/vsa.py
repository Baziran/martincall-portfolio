from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import exp, log

from aef_terminal.domain import Bar
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    provider_session_closing_window,
)
from aef_terminal.features.vsa_contracts import VsaFact
from aef_terminal.runtime import pine


@dataclass(frozen=True)
class VsaParams:
    vol_len: int = 30
    regime_len: int = 180
    atr_len: int = 14
    impulse_atr: float = 0.65
    impulse_rvol: float = 1.15
    fuel_rvol: float = 1.80
    fuel_range_atr: float = 1.05
    fuel_vol_rank: float = 97.0
    fuel_vol_z: float = 2.10
    fuel_spread_rel: float = 1.05
    fuel_close_buy: float = 0.68
    fuel_close_sell: float = 0.32
    use_adaptive: bool = True
    compress_close: bool = True
    close_minutes: int = 5
    close_scale: float = 0.35
    mute_close_signals: bool = True


def clamp(value: float, low: float, high: float) -> float:
    return pine.clamp(value, low, high)


def sigmoid(value: float) -> float:
    return pine.sigmoid(value)


def sma(values: Sequence[float], index: int, length: int) -> float:
    return pine.mean_at(values, index, length)


def ema(values: Sequence[float], length: int) -> list[float]:
    return pine.ema_series(values, length)


def percent_rank(values: Sequence[float], index: int, length: int) -> float:
    return pine.percent_rank(values, index, length)


def atr_series(bars: Sequence[Bar], length: int) -> list[float]:
    return pine.atr_rma_series(bars, length)


def event_summary(code: str) -> str:
    return {
        "EXH_DN": "Upper-wick rejection on abnormal volume; fade evidence near resistance.",
        "EXH_UP": "Lower-wick rejection on abnormal volume; fade evidence near support.",
        "ABS_LVL": "High effort with poor progress at a local level; treat as absorption context and wait for edge confirmation.",
        "ABS": "High effort with poor progress; wait for the candle edge to break.",
        "FUEL_UP": "Directional buying fuel; continuation until rejected.",
        "FUEL_DN": "Directional selling fuel; continuation until rejected.",
        "IMP_UP": "Up impulse with abnormal volume.",
        "IMP_DN": "Down impulse with abnormal volume.",
        "UPTHRUST": "Buy-side sweep rejected; wait for rejection/hold before taking the short side.",
        "SPRING": "Sell-side sweep rejected; wait for reclaim/hold before taking the long side.",
    }.get(code, "Normal volume.")


def _primary_code(
    *,
    signal_allowed: bool,
    upthrust: bool,
    spring: bool,
    up_exhaust: bool,
    dn_exhaust: bool,
    absorption_level: bool,
    absorption: bool,
    buy_fuel: bool,
    sell_fuel: bool,
    up_impulse: bool,
    dn_impulse: bool,
) -> str:
    if not signal_allowed:
        return ""
    if upthrust:
        return "UPTHRUST"
    if spring:
        return "SPRING"
    if up_exhaust:
        return "EXH_DN"
    if dn_exhaust:
        return "EXH_UP"
    if absorption_level:
        return "ABS_LVL"
    if absorption:
        return "ABS"
    if buy_fuel:
        return "FUEL_UP"
    if sell_fuel:
        return "FUEL_DN"
    if up_impulse:
        return "IMP_UP"
    if dn_impulse:
        return "IMP_DN"
    return ""


def vsa_facts(
    bars: Sequence[Bar],
    params: VsaParams | None = None,
    *,
    vwap_session: ProviderSessionReset | None = None,
    confirmed_slots: Sequence[int] | None = None,
) -> list[VsaFact]:
    params = params or VsaParams()
    if not bars:
        return []

    volumes = [bar.volume for bar in bars]
    ranges = [max(float(bar.high - bar.low), 0.000001) for bar in bars]
    log_volumes = [log(max(volume, 1.0)) for volume in volumes]
    log_base = ema(log_volumes, params.vol_len)
    atr_values = pine.atr_rma_series(bars, params.atr_len)
    anatomies = pine.bar_anatomy_series(bars)
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    display_volumes: list[float] = []
    facts: list[VsaFact] = []

    for index, bar in enumerate(bars):
        previous = bars[index - 1] if index else bar
        anatomy = anatomies[index]
        atr = atr_values[index]
        rng = anatomy.rng
        body = anatomy.body
        body_share = anatomy.body_share
        close_pos = anatomy.close_pos
        upper_share = anatomy.upper_share
        lower_share = anatomy.lower_share
        close_auction = params.compress_close and provider_session_closing_window(
            vwap_session,
            bar.ts,
            minutes=params.close_minutes,
        )
        display_volume = volumes[index] * params.close_scale if close_auction else volumes[index]
        display_volumes.append(display_volume)
        display_avg = sma(display_volumes, index, params.vol_len)

        vol_avg = max(sma(volumes, index, params.vol_len), 1.0)
        vol_ok = volumes[index] > 0 and vol_avg > 0
        rvol_sma = volumes[index] / vol_avg if vol_ok else 1.0
        geo_avg = exp(log_base[index]) if index < len(log_base) else max(volumes[index], 1.0)
        log_dev = max(
            pine.stdev_at(log_volumes, index, params.regime_len)
            if min(index + 1, params.regime_len) > 1
            else 0.20,
            0.05,
        )
        vol_z = pine.safe_div(log_volumes[index] - log_base[index], log_dev)
        vol_rank = percent_rank(volumes, index, params.regime_len)
        rank_rvol = 0.50 + vol_rank / 100.0 * 1.75
        rvol = (
            rvol_sma * 0.45 + volumes[index] / max(geo_avg, 1.0) * 0.35 + rank_rvol * 0.20
            if params.use_adaptive
            else rvol_sma
        )
        move_atr = pine.safe_div(bar.close - previous.close, atr)
        range_atr = rng / atr
        range_avg = max(sma(ranges, index, params.vol_len), 0.000001)
        spread_rel = rng / range_avg

        impulse_vol = (
            rvol >= params.impulse_rvol or vol_rank >= 78.0 or vol_z >= 0.95
            if params.use_adaptive
            else rvol >= params.impulse_rvol
        )
        high_vol = (
            rvol >= params.impulse_rvol * 1.05 or vol_rank >= 88.0 or vol_z >= 1.25
            if params.use_adaptive
            else rvol >= params.impulse_rvol
        )
        fuel_rvol_hit = rvol >= params.fuel_rvol
        fuel_rank_hit = vol_rank >= params.fuel_vol_rank
        fuel_z_hit = vol_z >= params.fuel_vol_z
        if params.use_adaptive:
            fuel_vol = (
                fuel_rvol_hit and (fuel_rank_hit or fuel_z_hit)
            ) or rvol >= params.fuel_rvol * 1.20
        else:
            fuel_vol = fuel_rvol_hit
        up_impulse = move_atr >= params.impulse_atr and impulse_vol
        dn_impulse = move_atr <= -params.impulse_atr and impulse_vol
        wide_bar = range_atr >= params.fuel_range_atr and spread_rel >= params.fuel_spread_rel
        fuel_gate = fuel_vol and wide_bar
        signal_allowed = not (params.mute_close_signals and close_auction)
        exhaust_vol = high_vol and range_atr >= params.fuel_range_atr * 0.90
        up_exhaust = exhaust_vol and upper_share >= 0.35 and close_pos <= 0.58
        dn_exhaust = exhaust_vol and lower_share >= 0.35 and close_pos >= 0.42
        buy_fuel = (
            fuel_gate
            and bar.close >= bar.open
            and close_pos >= params.fuel_close_buy
            and not up_exhaust
        )
        sell_fuel = (
            fuel_gate
            and bar.close < bar.open
            and close_pos <= params.fuel_close_sell
            and not dn_exhaust
        )
        absorption = high_vol and spread_rel <= 0.85 and body_share <= 0.45
        prior_high = pine.rolling_high(highs, index, 24)
        prior_low = pine.rolling_low(lows, index, 24)
        near_resistance = prior_high is not None and (
            abs(bar.high - prior_high) <= atr * 0.25
            or (bar.high > prior_high and bar.close < prior_high)
        )
        near_support = prior_low is not None and (
            abs(bar.low - prior_low) <= atr * 0.25
            or (bar.low < prior_low and bar.close > prior_low)
        )
        absorption_level = absorption and (near_resistance or near_support)
        absorption_role = (
            "resistance" if near_resistance else "support" if near_support else "range"
        )
        spring = lower_share >= 0.35 and close_pos >= 0.58 and impulse_vol
        upthrust = upper_share >= 0.35 and close_pos <= 0.42 and impulse_vol

        v_score = (
            max(
                clamp((vol_rank - 55.0) / 40.0, 0.0, 1.0),
                clamp((vol_z - 0.50) / 2.10, 0.0, 1.0),
            )
            if params.use_adaptive
            else clamp((rvol - 1.0) / max(params.fuel_rvol - 1.0, 0.10), 0.0, 1.0)
        )
        range_score = clamp(range_atr / max(params.fuel_range_atr, 0.10), 0.0, 1.2)
        move_score = clamp(abs(move_atr) / max(params.impulse_atr, 0.05), 0.0, 1.2)
        wick_score = clamp(max(upper_share, lower_share) / 0.45, 0.0, 1.2)
        no_progress_score = clamp((0.95 - spread_rel) / 0.45, 0.0, 1.0)
        score = clamp(
            sigmoid(
                -2.15
                + v_score * 2.35
                + range_score * 0.95
                + move_score * 0.85
                + wick_score * 0.55
                + no_progress_score * 0.70
                + (0.35 if absorption_level else 0.0)
            )
            * 100.0,
            0.0,
            99.0,
        )

        primary_code = _primary_code(
            signal_allowed=signal_allowed,
            upthrust=upthrust,
            spring=spring,
            up_exhaust=up_exhaust,
            dn_exhaust=dn_exhaust,
            absorption_level=absorption_level,
            absorption=absorption,
            buy_fuel=buy_fuel,
            sell_fuel=sell_fuel,
            up_impulse=up_impulse,
            dn_impulse=dn_impulse,
        )

        facts.append(
            VsaFact(
                index=index,
                bar=bar,
                atr=atr,
                rng=rng,
                body=body,
                body_share=body_share,
                close_pos=close_pos,
                upper_share=upper_share,
                lower_share=lower_share,
                display_volume=display_volume,
                display_avg=display_avg,
                rvol=rvol,
                vol_rank=vol_rank,
                vol_z=vol_z,
                move_atr=move_atr,
                range_atr=range_atr,
                spread_rel=spread_rel,
                score=score,
                close_auction=close_auction,
                signal_allowed=signal_allowed,
                spring=spring,
                upthrust=upthrust,
                up_impulse=up_impulse,
                dn_impulse=dn_impulse,
                buy_fuel=buy_fuel,
                sell_fuel=sell_fuel,
                up_exhaust=up_exhaust,
                dn_exhaust=dn_exhaust,
                absorption=absorption,
                absorption_level=absorption_level,
                absorption_role=absorption_role,
                near_resistance=near_resistance,
                near_support=near_support,
                primary_code=primary_code,
                code=primary_code,
            )
        )

    from aef_terminal.features.vsa_classify import apply_terminal_markers

    return apply_terminal_markers(
        facts,
        bars,
        vwap_session=vwap_session,
        confirmed_slots=confirmed_slots,
    )
