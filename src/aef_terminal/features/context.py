from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.candles import candle_features
from aef_terminal.features.speed import speed_features
from aef_terminal.features.volume import volume_features
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults
from aef_terminal.runtime import pine
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.pivots import PivotContext


@dataclass(frozen=True)
class FeatureContext:
    bars: list[Bar]
    instrument_profile: InstrumentProfile
    defaults: IndicatorDefaults = DEFAULT_INDICATOR_SETTINGS
    values: dict[str, Any] = field(default_factory=dict)
    atr_values: list[float] = field(default_factory=list)
    atr_sma_values: list[float] = field(default_factory=list)
    rvol_values: list[float] = field(default_factory=list)
    ema_pullback: list[float] = field(default_factory=list)
    ema_fast: list[float] = field(default_factory=list)
    ema_slow: list[float] = field(default_factory=list)
    ema_magnet: list[float] = field(default_factory=list)
    pivot_context: PivotContext | None = None

    @property
    def latest_atr(self) -> float:
        return max(float(self.atr_values[-1]), 0.000001) if self.atr_values else 1.0

    @property
    def latest_rvol(self) -> float:
        return float(self.rvol_values[-1]) if self.rvol_values else 1.0

    @property
    def latest_atr_sma(self) -> float:
        return (
            max(float(self.atr_sma_values[-1]), 0.000001)
            if self.atr_sma_values
            else self.latest_atr
        )


def build_feature_context(
    bars: list[Bar],
    *,
    instrument_profile: InstrumentProfile,
    defaults: IndicatorDefaults = DEFAULT_INDICATOR_SETTINGS,
    extra: dict[str, Any] | None = None,
    live_price: float | None = None,
) -> FeatureContext:
    working_bars = list(bars)
    if live_price is not None and working_bars and working_bars[-1].closed is False:
        latest = working_bars[-1]
        working_bars[-1] = replace(
            latest,
            high=max(float(latest.high), float(live_price)),
            low=min(float(latest.low), float(live_price)),
            close=float(live_price),
        )
    bars = working_bars
    values: dict[str, Any] = {}
    if bars:
        values.update(candle_features(bars))
        atr_values = pine.atr_rma_series(bars, defaults.atr_len)
        atr_sma_values = pine.atr_sma_series(bars, defaults.atr_len)
        latest_atr = max(float(atr_values[-1]), 0.000001) if atr_values else 1.0
        values.update(volume_features(bars))
        values.update(speed_features(bars, latest_atr))
        values["instrument_profile"] = instrument_profile.key
    else:
        atr_values = []
        atr_sma_values = []
    closes = [bar.close for bar in bars]
    rvol_values = pine.relative_volume_series(bars, defaults.rvol_len) if bars else []
    ema_pullback = pine.ema_series(closes, defaults.ema.pullback) if closes else []
    ema_fast = pine.ema_series(closes, defaults.ema.fast) if closes else []
    ema_slow = pine.ema_series(closes, defaults.ema.slow) if closes else []
    ema_magnet = pine.ema_series(closes, defaults.ema.magnet) if closes else []
    if extra:
        values.update(extra)
    return FeatureContext(
        bars=list(bars),
        instrument_profile=instrument_profile,
        defaults=defaults,
        values=values,
        atr_values=atr_values,
        atr_sma_values=atr_sma_values,
        rvol_values=rvol_values,
        ema_pullback=ema_pullback,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        ema_magnet=ema_magnet,
        pivot_context=PivotContext(list(bars)),
    )
