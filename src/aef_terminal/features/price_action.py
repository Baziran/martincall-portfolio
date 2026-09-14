from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aef_terminal.domain import Bar
from aef_terminal.runtime.pine import (
    atr_rma_series,
    bar_anatomy as shared_bar_anatomy,
    clamp,
    rolling_high,
    rolling_low,
)


@dataclass(frozen=True)
class CandleAnatomy:
    index: int
    range: float
    body: float
    body_signed: float
    body_share: float
    close_pos: float
    upper_wick: float
    lower_wick: float
    upper_share: float
    lower_share: float
    is_inside: bool = False


@dataclass(frozen=True)
class SweepFact:
    index: int
    direction: str
    level: float
    wick_share: float
    close_pos: float
    qualified: bool
    score: float


@dataclass(frozen=True)
class ImpulseFact:
    index: int
    direction: str
    move_atr: float
    range_atr: float
    close_pos: float
    body_share: float
    qualified: bool
    displacement: float
    score: float


@dataclass(frozen=True)
class SfpState:
    index: int
    bull: bool
    bear: bool
    bull_raw: bool
    bear_raw: bool
    micro_bull: bool
    micro_bear: bool
    support_level: float | None
    resistance_level: float | None
    bull_level: float | None
    bear_level: float | None
    lower_wick_atr: float
    upper_wick_atr: float
    close_pos: float
    score_bull: float
    score_bear: float


def candle_anatomy(bar: Bar, index: int = 0) -> CandleAnatomy:
    anatomy = shared_bar_anatomy(bar)
    return CandleAnatomy(
        index=index,
        range=anatomy.rng,
        body=anatomy.body,
        body_signed=anatomy.body_signed,
        body_share=anatomy.body_share,
        close_pos=anatomy.close_pos,
        upper_wick=anatomy.upper,
        lower_wick=anatomy.lower,
        upper_share=anatomy.upper_share,
        lower_share=anatomy.lower_share,
    )


def candle_anatomies(bars: Sequence[Bar]) -> list[CandleAnatomy]:
    return [candle_anatomy(bar, index) for index, bar in enumerate(bars)]


def displacement_feature(
    bars: Sequence[Bar],
    lookback: int = 4,
) -> dict[str, float]:
    """Return causal window efficiency for the latest supplied bar."""

    if not bars:
        return {}
    window_len = max(int(lookback), 2)
    if len(bars) >= window_len:
        window = bars[-window_len:]
        net_move = abs(window[-1].close - window[0].open)
        total_range = max(bar.high for bar in window) - min(bar.low for bar in window)
        return {"displacement": net_move / max(total_range, 0.000001)}
    return {"displacement": candle_anatomy(bars[-1]).body_share}


def sweep_facts(
    bars: Sequence[Bar],
    lookback: int = 20,
    min_wick_share: float = 0.28,
    bull_close_pos: float = 0.55,
    bear_close_pos: float = 0.45,
) -> list[SweepFact | None]:
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    facts: list[SweepFact | None] = []
    for index, bar in enumerate(bars):
        anatomy = candle_anatomy(bar, index)

        prior_high = rolling_high(highs, index, lookback)
        prior_low = rolling_low(lows, index, lookback)
        fact: SweepFact | None = None
        if (
            prior_high is not None
            and bar.high > prior_high
            and bar.close < prior_high
            and anatomy.upper_share >= min_wick_share
            and anatomy.close_pos <= bear_close_pos
        ):
            score = clamp(
                55.0 + anatomy.upper_share * 35.0 + (bear_close_pos - anatomy.close_pos) * 18.0,
                0.0,
                99.0,
            )
            fact = SweepFact(
                index=index,
                direction="short",
                level=prior_high,
                wick_share=anatomy.upper_share,
                close_pos=anatomy.close_pos,
                qualified=True,
                score=score,
            )
        elif (
            prior_low is not None
            and bar.low < prior_low
            and bar.close > prior_low
            and anatomy.lower_share >= min_wick_share
            and anatomy.close_pos >= bull_close_pos
        ):
            score = clamp(
                55.0 + anatomy.lower_share * 35.0 + (anatomy.close_pos - bull_close_pos) * 18.0,
                0.0,
                99.0,
            )
            fact = SweepFact(
                index=index,
                direction="long",
                level=prior_low,
                wick_share=anatomy.lower_share,
                close_pos=anatomy.close_pos,
                qualified=True,
                score=score,
            )
        facts.append(fact)
    return facts


def impulse_facts(
    bars: Sequence[Bar],
    atr_len: int = 14,
    move_len: int = 3,
    min_move_atr: float = 1.25,
    min_body_share: float = 0.45,
) -> list[ImpulseFact]:
    atr_values = atr_rma_series(bars, atr_len)
    facts: list[ImpulseFact] = []
    for index, bar in enumerate(bars):
        anatomy = candle_anatomy(bar, index)
        atr = max(atr_values[index], 0.000001)
        base = bars[max(0, index - move_len)]
        move_atr = (bar.close - base.close) / atr
        range_atr = anatomy.range / atr

        # Displacement = Сочетание силы движения, размера тела и "чистоты" закрытия
        displacement = clamp((abs(move_atr) / 1.5) * anatomy.body_share * 1.2, 0.0, 1.0)

        direction = "long" if move_atr > 0 else "short" if move_atr < 0 else "flat"
        qualified = (
            abs(move_atr) >= min_move_atr
            and anatomy.body_share >= min_body_share
            and (
                anatomy.close_pos >= 0.60
                if direction == "long"
                else anatomy.close_pos <= 0.40
                if direction == "short"
                else False
            )
        )
        # Усиленный скоринг с учетом displacement
        score = clamp(
            abs(move_atr) / max(min_move_atr, 0.05) * 40.0
            + anatomy.body_share * 30.0
            + displacement * 20.0
            + range_atr * 9.0,
            0.0,
            99.0,
        )

        facts.append(
            ImpulseFact(
                index=index,
                direction=direction,
                move_atr=move_atr,
                range_atr=range_atr,
                close_pos=anatomy.close_pos,
                body_share=anatomy.body_share,
                qualified=qualified,
                displacement=displacement,
                score=score,
            )
        )
    return facts


def sfp_state_series(
    bars: Sequence[Bar],
    atr_values: Sequence[float],
    lookback: int = 20,
    micro_lookback: int = 3,
    min_wick_atr: float = 0.12,
    bull_close_pos: float = 0.55,
    bear_close_pos: float = 0.45,
    micro_bull_close_pos: float = 0.63,
    micro_bear_close_pos: float = 0.37,
) -> list[SfpState]:
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    out: list[SfpState] = []
    for index, bar in enumerate(bars):
        anatomy = candle_anatomy(bar, index)
        atr = max(float(atr_values[index]) if index < len(atr_values) else anatomy.range, 0.000001)
        prior_low = rolling_low(lows, index, lookback)
        prior_high = rolling_high(highs, index, lookback)
        micro_low = rolling_low(lows, index, micro_lookback)
        micro_high = rolling_high(highs, index, micro_lookback)
        lower_wick_atr = anatomy.lower_wick / atr
        upper_wick_atr = anatomy.upper_wick / atr
        bull_raw = (
            prior_low is not None
            and bar.low < prior_low
            and bar.close > prior_low
            and lower_wick_atr >= min_wick_atr
            and anatomy.close_pos >= bull_close_pos
        )
        bear_raw = (
            prior_high is not None
            and bar.high > prior_high
            and bar.close < prior_high
            and upper_wick_atr >= min_wick_atr
            and anatomy.close_pos <= bear_close_pos
        )
        micro_bull = (
            micro_low is not None
            and bar.low < micro_low
            and bar.close > micro_low
            and lower_wick_atr >= min_wick_atr * 0.75
            and anatomy.close_pos >= micro_bull_close_pos
            and bar.close > bar.open
        )
        micro_bear = (
            micro_high is not None
            and bar.high > micro_high
            and bar.close < micro_high
            and upper_wick_atr >= min_wick_atr * 0.75
            and anatomy.close_pos <= micro_bear_close_pos
            and bar.close < bar.open
        )
        score_bull = clamp(
            50.0 + lower_wick_atr * 28.0 + (anatomy.close_pos - bull_close_pos) * 35.0, 0.0, 99.0
        )
        score_bear = clamp(
            50.0 + upper_wick_atr * 28.0 + (bear_close_pos - anatomy.close_pos) * 35.0, 0.0, 99.0
        )
        out.append(
            SfpState(
                index=index,
                bull=bull_raw or micro_bull,
                bear=bear_raw or micro_bear,
                bull_raw=bull_raw,
                bear_raw=bear_raw,
                micro_bull=micro_bull,
                micro_bear=micro_bear,
                support_level=prior_low,
                resistance_level=prior_high,
                bull_level=prior_low if bull_raw else micro_low if micro_bull else None,
                bear_level=prior_high if bear_raw else micro_high if micro_bear else None,
                lower_wick_atr=lower_wick_atr,
                upper_wick_atr=upper_wick_atr,
                close_pos=anatomy.close_pos,
                score_bull=score_bull,
                score_bear=score_bear,
            )
        )
    return out
