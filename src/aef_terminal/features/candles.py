from collections.abc import Sequence
from typing import TypedDict

from aef_terminal.domain import Bar
from aef_terminal.runtime.pine import bar_anatomy, clamp


class CandleFeatures(TypedDict, total=False):
    range: float
    body: float
    body_share: float
    upper_share: float
    lower_share: float
    close_pos: float
    bull_engulf: bool
    bear_engulf: bool
    is_inside: bool
    is_doji: bool
    hammer: bool
    shooting_star: bool
    rejection_score: float


def candle_features(bars: Sequence[Bar]) -> CandleFeatures:
    if not bars:
        return {}

    bar = bars[-1]
    anatomy = bar_anatomy(bar)
    prev = bars[-2] if len(bars) >= 2 else None

    bull_engulf = False
    bear_engulf = False
    if prev is not None:
        prev_body = abs(prev.close - prev.open)
        bull_engulf = (
            bar.close > bar.open
            and prev.close < prev.open
            and bar.open <= prev.close
            and bar.close >= prev.open
            and anatomy.body >= prev_body * 0.8
        )
        bear_engulf = (
            bar.close < bar.open
            and prev.close > prev.open
            and bar.open >= prev.close
            and bar.close <= prev.open
            and anatomy.body >= prev_body * 0.8
        )

    # 1. Детекция Inside Bar (текущий бар внутри предыдущего)
    is_inside = False
    if prev is not None:
        is_inside = bar.high <= prev.high and bar.low >= prev.low

    # 2. Детекция Pinbars / Rejection
    # Hammer (Молот) - длинный нижний фитиль, закрытие в верхней части
    hammer = anatomy.lower_share >= 0.35 and anatomy.close_pos >= 0.60
    # Shooting Star (Падающая звезда) - длинный верхний фитиль, закрытие в нижней части
    shooting_star = anatomy.upper_share >= 0.35 and anatomy.close_pos <= 0.40

    # 3. Доджи (неопределенность)
    is_doji = anatomy.body_share <= 0.15

    # 4. Rejection Score (0-99) - оценка силы "отвержения" уровня
    rejection_score = 0.0
    if hammer:
        rejection_score = clamp(
            anatomy.lower_share * 100.0 + (anatomy.close_pos - 0.5) * 40.0, 0.0, 99.0
        )
    elif shooting_star:
        rejection_score = clamp(
            anatomy.upper_share * 100.0 + (0.5 - anatomy.close_pos) * 40.0, 0.0, 99.0
        )

    return {
        "range": anatomy.rng,
        "body": anatomy.body,
        "body_share": anatomy.body_share,
        "upper_share": anatomy.upper_share,
        "lower_share": anatomy.lower_share,
        "close_pos": anatomy.close_pos,
        "bull_engulf": bull_engulf,
        "bear_engulf": bear_engulf,
        "is_inside": is_inside,
        "is_doji": is_doji,
        "hammer": hammer,
        "shooting_star": shooting_star,
        "rejection_score": rejection_score,
    }
