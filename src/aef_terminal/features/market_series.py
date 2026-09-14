from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.context import FeatureContext
from aef_terminal.features.market_context import mtf_trend_context, vwap_band_series
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.runtime import pine


@dataclass(frozen=True)
class MarketSeriesBlock:
    ema_pullback: list[float]
    ema_trend: list[float]
    ema_magnet: list[float]
    vwap: list[float]
    vwap_sigma: list[float]
    vwap_upper_1: list[float]
    vwap_lower_1: list[float]
    vwap_upper_2: list[float]
    vwap_lower_2: list[float]
    atr_sma: list[float]
    adx: list[float]
    mtf_context: dict[str, Any]
    vwap_context: dict[str, Any]


def build_market_series_block(
    bars: list[Bar],
    *,
    ema_pullback_len: int,
    ema_trend_len: int,
    ema_magnet_len: int,
    atr_len: int,
    vwap_session: ProviderSessionReset,
    feature_context: FeatureContext | None = None,
) -> MarketSeriesBlock:
    if not vwap_session.available:
        raise ValueError(
            f"Market series requires provider VWAP session: {vwap_session.reason_code}"
        )
    closes = [bar.close for bar in bars]
    reuse = feature_context is not None and len(feature_context.bars) == len(bars)
    if reuse and feature_context.defaults.atr_len == atr_len:
        ema_pullback = feature_context.ema_pullback
        ema_magnet = feature_context.ema_magnet
        atr_sma = feature_context.atr_sma_values
    else:
        ema_pullback = pine.ema_series(closes, ema_pullback_len)
        ema_magnet = pine.ema_series(closes, ema_magnet_len)
        atr_sma = pine.atr_sma_series(bars, atr_len)
    if reuse and feature_context.defaults.ema.slow == ema_trend_len:
        ema_trend = feature_context.ema_slow
    else:
        ema_trend = pine.ema_series(closes, ema_trend_len)
    vwap = pine.vwap_series(bars, reset=vwap_session.key_for_bar)
    vwap_sigma, vwap_upper_1, vwap_lower_1, vwap_upper_2, vwap_lower_2 = vwap_band_series(
        bars,
        reset=vwap_session.key_for_bar,
    )
    _, _, adx_values = pine.dmi_series(bars, 14, 14)
    return MarketSeriesBlock(
        ema_pullback=ema_pullback,
        ema_trend=ema_trend,
        ema_magnet=ema_magnet,
        vwap=vwap,
        vwap_sigma=vwap_sigma,
        vwap_upper_1=vwap_upper_1,
        vwap_lower_1=vwap_lower_1,
        vwap_upper_2=vwap_upper_2,
        vwap_lower_2=vwap_lower_2,
        atr_sma=atr_sma,
        adx=adx_values,
        mtf_context=mtf_trend_context(bars, vwap_reset=vwap_session.key_for_bar),
        vwap_context=vwap_session.status_payload(),
    )
