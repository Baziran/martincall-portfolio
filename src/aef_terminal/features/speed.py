from collections.abc import Sequence
from typing import TypedDict

from aef_terminal.domain import Bar


class SpeedFeatures(TypedDict, total=False):
    move_atr: float
    velocity_atr: float
    acceleration_atr: float
    path_efficiency: float
    pullback_atr: float
    speed_score: float


def speed_features(bars: Sequence[Bar], atr: float, lookback: int = 5) -> SpeedFeatures:
    if len(bars) < 3:
        return {
            "move_atr": 0.0,
            "velocity_atr": 0.0,
            "acceleration_atr": 0.0,
            "path_efficiency": 0.0,
            "pullback_atr": 0.0,
            "speed_score": 0.0,
        }

    atr = max(atr, 1e-9)
    current = bars[-1]

    # 1. Мгновенный импульс (последний бар)
    move_atr = (current.close - bars[-2].close) / atr

    # 2. Ускорение (изменение импульса)
    prev_move_atr = (bars[-2].close - bars[-3].close) / atr
    acceleration_atr = move_atr - prev_move_atr

    # 3. Анализ окна (Lookback)
    actual_lookback = min(len(bars), lookback)
    window = bars[-actual_lookback:]
    net = window[-1].close - window[0].open
    path = sum(abs(window[i].close - window[i - 1].close) for i in range(1, len(window)))

    # Эффективность (Kaufman Efficiency Ratio): 1.0 - идеальная линия, 0.0 - хаос/флэт
    path_efficiency = abs(net) / max(path, 1e-9)
    velocity_atr = (net / actual_lookback) / atr

    high = max(bar.high for bar in window)
    low = min(bar.low for bar in window)
    pullback_atr = (high - current.close) / atr if net > 0 else (current.close - low) / atr

    # 4. Итоговый Speed Score (0.0 - 5.0+)
    # Сочетает направленную силу (velocity) и чистоту движения (efficiency)
    speed_score = abs(velocity_atr) * path_efficiency * 5.0

    return {
        "move_atr": move_atr,
        "velocity_atr": velocity_atr,
        "acceleration_atr": acceleration_atr,
        "path_efficiency": path_efficiency,
        "pullback_atr": pullback_atr,
        "speed_score": speed_score,
    }
