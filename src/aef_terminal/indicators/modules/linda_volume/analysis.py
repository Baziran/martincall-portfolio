from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.vsa_classify import (
    VSA_ABSORPTION_CODES,
    combined_reversal_long,
    combined_reversal_short,
)

from .params import LindaGrailTuning, LindaVolumeParams


def grail_blockers(
    *,
    developing: bool,
    checks: Sequence[tuple[str, bool]],
) -> list[str]:
    if developing:
        return []
    return [name for name, ok in checks if not ok]


@dataclass(frozen=True)
class LindaGrailInputs:
    latest_bar: Bar
    highs: Sequence[float]
    lows: Sequence[float]
    latest_atr: float
    latest_ema20: float
    latest_ema233: float
    latest_adx: float
    latest_volume_avg: float
    params: LindaVolumeParams
    tuning: LindaGrailTuning
    full_bull: bool
    full_bear: bool
    mtf_bull_ok: bool
    mtf_bear_ok: bool
    vsa_fuel_latest: bool
    fresh_long_initiative: bool
    fresh_short_initiative: bool
    latest_indian_count: int
    m15_bull_score: int
    m15_bear_score: int
    contra_long_locked: bool
    contra_short_locked: bool
    side_veto_long: bool
    side_veto_short: bool


@dataclass(frozen=True)
class LindaGrailAnalysis:
    too_far: bool
    long_stop: float
    short_stop: float
    long_target: float
    short_target: float
    buy_rr: float
    sell_rr: float
    buy_developing: bool
    sell_developing: bool
    is_buy: bool
    is_sell: bool
    buy_blockers: tuple[str, ...]
    sell_blockers: tuple[str, ...]


def analyze_grail(inputs: LindaGrailInputs) -> LindaGrailAnalysis:
    too_far = abs(inputs.latest_bar.close - inputs.latest_ema233) > inputs.latest_atr * 4.0
    target_atr = inputs.latest_atr * max(inputs.tuning.space_coeff, 1.0)
    prior_high = (
        max(inputs.highs[-21:-1])
        if len(inputs.highs) >= 21
        else max(inputs.highs[:-1] or inputs.highs)
    )
    prior_low = (
        min(inputs.lows[-21:-1]) if len(inputs.lows) >= 21 else min(inputs.lows[:-1] or inputs.lows)
    )
    long_stop = (
        min(inputs.latest_ema20, inputs.latest_bar.low)
        - inputs.latest_atr * inputs.tuning.stop_mult
    )
    short_stop = (
        max(inputs.latest_ema20, inputs.latest_bar.high)
        + inputs.latest_atr * inputs.tuning.stop_mult
    )
    long_target = (
        prior_high if inputs.latest_bar.close < prior_high else inputs.latest_bar.close + target_atr
    )
    short_target = (
        prior_low if inputs.latest_bar.close > prior_low else inputs.latest_bar.close - target_atr
    )
    long_risk = max(inputs.latest_bar.close - long_stop, 0.000001)
    short_risk = max(short_stop - inputs.latest_bar.close, 0.000001)
    buy_rr = max(long_target - inputs.latest_bar.close, 0.0) / long_risk
    sell_rr = max(inputs.latest_bar.close - short_target, 0.0) / short_risk
    buy_checks = (
        ("adx", inputs.latest_adx > inputs.params.indian_adx_min),
        (
            "ema20_retest",
            inputs.latest_bar.low <= inputs.latest_ema20
            and inputs.latest_bar.close > inputs.latest_ema20,
        ),
        (
            "volume",
            inputs.latest_bar.volume > inputs.latest_volume_avg * inputs.tuning.grail_vol_mult,
        ),
        ("trend", inputs.full_bull),
        ("mtf", inputs.mtf_bull_ok),
        ("fuel_clear", not inputs.vsa_fuel_latest),
        ("distance", not too_far),
        ("rr", buy_rr >= inputs.tuning.grail_min_rr),
        ("initiative", inputs.fresh_long_initiative),
        (
            "indian_stage",
            inputs.latest_indian_count < 2 or inputs.m15_bull_score >= 5,
        ),
        ("contra_lock", not inputs.contra_long_locked),
        ("side_veto", not inputs.side_veto_long),
    )
    sell_checks = (
        ("adx", inputs.latest_adx > inputs.params.indian_adx_min),
        (
            "ema20_retest",
            inputs.latest_bar.high >= inputs.latest_ema20
            and inputs.latest_bar.close < inputs.latest_ema20,
        ),
        (
            "volume",
            inputs.latest_bar.volume > inputs.latest_volume_avg * inputs.tuning.grail_vol_mult,
        ),
        ("trend", inputs.full_bear),
        ("mtf", inputs.mtf_bear_ok),
        ("fuel_clear", not inputs.vsa_fuel_latest),
        ("distance", not too_far),
        ("rr", sell_rr >= inputs.tuning.grail_min_rr),
        ("initiative", inputs.fresh_short_initiative),
        (
            "indian_stage",
            inputs.latest_indian_count < 2 or inputs.m15_bear_score >= 5,
        ),
        ("contra_lock", not inputs.contra_short_locked),
        ("side_veto", not inputs.side_veto_short),
    )
    buy_developing = all(ok for _, ok in buy_checks)
    sell_developing = all(ok for _, ok in sell_checks)
    return LindaGrailAnalysis(
        too_far=too_far,
        long_stop=long_stop,
        short_stop=short_stop,
        long_target=long_target,
        short_target=short_target,
        buy_rr=buy_rr,
        sell_rr=sell_rr,
        buy_developing=buy_developing,
        sell_developing=sell_developing,
        is_buy=bool(inputs.params.enable_grail and buy_developing and inputs.latest_bar.closed),
        is_sell=bool(inputs.params.enable_grail and sell_developing and inputs.latest_bar.closed),
        buy_blockers=tuple(grail_blockers(developing=buy_developing, checks=buy_checks)),
        sell_blockers=tuple(grail_blockers(developing=sell_developing, checks=sell_checks)),
    )


@dataclass(frozen=True)
class LindaExhaustionInputs:
    bars: Sequence[Bar]
    series: Sequence[Mapping[str, Any]]
    vsa_series: Sequence[Mapping[str, Any]]
    latest_rvol: float
    anatomy_upper_share: float
    anatomy_lower_share: float
    anatomy_close_pos: float
    poi_context: Mapping[str, Any]
    context_code: str
    vsa_context_code: str
    params: LindaVolumeParams
    vsa_fuel_latest: bool
    too_far: bool
    vwap_touch_upper_2: Sequence[bool]
    vwap_touch_lower_2: Sequence[bool]
    latest_upper_1: float
    latest_lower_1: float
    latest_upper_2: float
    latest_lower_2: float
    vwap_upper_2_reversal_valid: bool
    vwap_lower_2_reversal_valid: bool
    trend_day_bull: bool
    trend_day_bear: bool
    full_bull: bool
    full_bear: bool
    runaway_day_up: bool
    runaway_day_dn: bool
    latest_atr: float


@dataclass(frozen=True)
class LindaExhaustionAnalysis:
    prev_rvol: float
    prev2_rvol: float
    poi_long_hot: bool
    poi_short_hot: bool
    opp_exh_long: bool
    opp_exh_short: bool
    upper_volume_exhaustion: bool
    upper_location_exhaustion: bool
    lower_location_exhaustion: bool
    bull_exhaust_score: int
    bear_exhaust_score: int
    bull_exhaust_candidate: bool
    bear_exhaust_candidate: bool
    clx_ct_long: bool
    clx_ct_short: bool
    reversal_warning: bool


def analyze_exhaustion(
    inputs: LindaExhaustionInputs,
) -> LindaExhaustionAnalysis:
    latest_bar = inputs.bars[-1]
    previous_bar = inputs.bars[-2] if len(inputs.bars) >= 2 else latest_bar
    prev_rvol = (
        float(inputs.series[-2].get("rvol") or 0.0)
        if len(inputs.series) >= 2
        else inputs.latest_rvol
    )
    prev2_rvol = (
        float(inputs.series[-3].get("rvol") or 0.0) if len(inputs.series) >= 3 else prev_rvol
    )
    seller_rvol_fading = (
        latest_bar.close < previous_bar.close or latest_bar.close < latest_bar.open
    ) and inputs.latest_rvol < prev_rvol <= prev2_rvol
    buyer_rvol_fading = (
        latest_bar.close > previous_bar.close or latest_bar.close > latest_bar.open
    ) and inputs.latest_rvol < prev_rvol <= prev2_rvol
    poi_long_hot = bool(inputs.poi_context.get("hot")) and (
        inputs.poi_context.get("direction") == "long"
    )
    poi_short_hot = bool(inputs.poi_context.get("hot")) and (
        inputs.poi_context.get("direction") == "short"
    )
    opp_exh_long = bool(
        poi_long_hot
        and latest_bar.closed
        and (inputs.context_code == "NO_SUPPLY" or seller_rvol_fading)
    )
    opp_exh_short = bool(
        poi_short_hot
        and latest_bar.closed
        and (inputs.context_code == "NO_DEMAND" or buyer_rvol_fading)
    )
    bear_pinbar = inputs.anatomy_upper_share >= 0.36 and inputs.anatomy_close_pos <= 0.45
    bull_pinbar = inputs.anatomy_lower_share >= 0.36 and inputs.anatomy_close_pos >= 0.55
    upper_volume_exhaustion = bool(
        inputs.vsa_fuel_latest
        or inputs.vsa_context_code in VSA_ABSORPTION_CODES
        or inputs.latest_rvol >= max(inputs.params.fuel_rvol * 0.85, 1.45)
    )
    upper_location_exhaustion = bool(
        inputs.too_far or inputs.vwap_touch_upper_2[-1] or latest_bar.close >= inputs.latest_upper_2
    )
    lower_location_exhaustion = bool(
        inputs.too_far or inputs.vwap_touch_lower_2[-1] or latest_bar.close <= inputs.latest_lower_2
    )
    upper_reversal_trigger = bool(
        combined_reversal_short(
            vsa_code=inputs.vsa_context_code,
            context_code=inputs.context_code,
        )
        or inputs.vwap_upper_2_reversal_valid
        or bear_pinbar
    )
    lower_reversal_trigger = bool(
        combined_reversal_long(
            vsa_code=inputs.vsa_context_code,
            context_code=inputs.context_code,
        )
        or inputs.vwap_lower_2_reversal_valid
        or bull_pinbar
    )
    bull_exhaust_score = (
        int(upper_volume_exhaustion)
        + int(upper_location_exhaustion)
        + int(upper_reversal_trigger)
        + int(inputs.vwap_upper_2_reversal_valid)
    )
    bear_exhaust_score = (
        int(upper_volume_exhaustion)
        + int(lower_location_exhaustion)
        + int(lower_reversal_trigger)
        + int(inputs.vwap_lower_2_reversal_valid)
    )
    bull_exhaust_candidate = bool(
        latest_bar.closed
        and (inputs.trend_day_bull or inputs.full_bull)
        and bull_exhaust_score >= 3
        and upper_reversal_trigger
        and (not inputs.runaway_day_up or latest_bar.close < inputs.latest_upper_1)
    )
    bear_exhaust_candidate = bool(
        latest_bar.closed
        and (inputs.trend_day_bear or inputs.full_bear)
        and bear_exhaust_score >= 3
        and lower_reversal_trigger
        and (not inputs.runaway_day_dn or latest_bar.close > inputs.latest_lower_1)
    )
    clx_source: Mapping[str, Any] | None = None
    clx_bar: Bar | None = None
    for offset in range(1, min(6, len(inputs.vsa_series))):
        item = inputs.vsa_series[-1 - offset]
        if not bool(item.get("fuel")):
            continue
        clx_source = item
        clx_bar = inputs.bars[-1 - offset]
        break
    clx_ct_short = False
    clx_ct_long = False
    if clx_source and clx_bar:
        clx_mid = (clx_bar.high + clx_bar.low) * 0.5
        clx_was_up = (
            str(clx_source.get("direction") or "") == "long" or clx_bar.close >= clx_bar.open
        )
        clx_ct_short = bool(
            clx_was_up
            and latest_bar.closed
            and latest_bar.high <= clx_bar.high + inputs.latest_atr * 0.15
            and (
                latest_bar.close < clx_mid
                or latest_bar.close < previous_bar.low
                or inputs.vwap_upper_2_reversal_valid
                or combined_reversal_short(
                    vsa_code=inputs.vsa_context_code,
                    context_code=inputs.context_code,
                )
                or bear_pinbar
            )
        )
        clx_ct_long = bool(
            not clx_was_up
            and latest_bar.closed
            and latest_bar.low >= clx_bar.low - inputs.latest_atr * 0.15
            and (
                latest_bar.close > clx_mid
                or latest_bar.close > previous_bar.high
                or inputs.vwap_lower_2_reversal_valid
                or combined_reversal_long(
                    vsa_code=inputs.vsa_context_code,
                    context_code=inputs.context_code,
                )
                or bull_pinbar
            )
        )
    reversal_warning = bool(
        latest_bar.closed
        and inputs.vsa_fuel_latest
        and (inputs.trend_day_bull or inputs.trend_day_bear)
        and not (bull_exhaust_candidate or bear_exhaust_candidate)
    )
    return LindaExhaustionAnalysis(
        prev_rvol=prev_rvol,
        prev2_rvol=prev2_rvol,
        poi_long_hot=poi_long_hot,
        poi_short_hot=poi_short_hot,
        opp_exh_long=opp_exh_long,
        opp_exh_short=opp_exh_short,
        upper_volume_exhaustion=upper_volume_exhaustion,
        upper_location_exhaustion=upper_location_exhaustion,
        lower_location_exhaustion=lower_location_exhaustion,
        bull_exhaust_score=bull_exhaust_score,
        bear_exhaust_score=bear_exhaust_score,
        bull_exhaust_candidate=bull_exhaust_candidate,
        bear_exhaust_candidate=bear_exhaust_candidate,
        clx_ct_long=clx_ct_long,
        clx_ct_short=clx_ct_short,
        reversal_warning=reversal_warning,
    )
