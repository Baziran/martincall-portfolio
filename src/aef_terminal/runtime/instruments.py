from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from aef_terminal.data.instrument_identity import (
    instrument_asset_class,
    instrument_key,
    require_provider_identity,
)

if TYPE_CHECKING:
    from aef_terminal.domain import Direction


@dataclass(frozen=True)
class InstrumentProfile:
    key: str
    label: str
    family: str
    tick_size: float  # Analysis-only tolerance; never a provider/execution increment.
    bar_coil_min: int
    bar_barcode_min: int
    bar_range_max_atr: float
    bar_squeeze_max: float
    bar_late_rvol: float
    bar_level_near_atr: float
    bar_approach_body_atr: float
    bar_impulse_range_atr: float
    bar_barcode_max_mult: float
    bar_confirm_min_atr: float | None = None
    bar_confirm_max_atr: float | None = None
    etf_option: bool = False
    es_touch_break: bool = False
    w5_watch: int = 58
    w5_arm: int = 70
    w5_runaway: int = 75
    wolfe_pivot_len: int = 3
    vsa_impulse_rvol: float = 1.55
    vsa_fuel_rvol: float = 2.20
    vsa_pullback_rvol: float = 1.05
    fuel_range_atr: float = 1.15
    indian_adx_min: float = 24.0
    indian_volume_mult: float = 0.8
    indian_extension_atr: float = 0.35
    stop_pad_atr: float = 0.18  # Множитель ATR для отступа стоп-лосса
    target_r_fade: float = 1.35  # Целевой RR для сделок на отбой
    target_r_transit: float = 1.80  # Целевой RR для сделок на пробой
    is_futures: bool = False  # Флаг для сессий фьючерсов (CME)
    is_index: bool = False  # Флаг для индексов (S&P, Nasdaq)

    def calculate_stop_distance(self, atr: float, *, price_increment: float) -> float:
        atr_value = max(float(atr or 0.0), 0.0)
        increment = float(price_increment)
        if increment <= 0:
            raise ValueError("PRICE_INCREMENT_INVALID")
        base = max(atr_value * self.stop_pad_atr, increment * 2)
        if self.is_futures:
            base = max(base, atr_value * 0.28, increment * 8)
        return base

    def calculate_target_price(
        self,
        direction: Direction | str,
        trigger: float,
        stop: float,
        is_fade: bool,
    ) -> float:
        risk = abs(float(trigger) - float(stop))
        rr = self.target_r_fade if is_fade else self.target_r_transit
        dir_val = getattr(direction, "value", direction)
        normalized = str(dir_val or "").lower()
        if normalized == "short":
            return float(trigger) - (risk * rr)
        return float(trigger) + (risk * rr)

    def breakout_confirm_atr(self, base: float) -> float:
        value = base
        if self.bar_confirm_min_atr is not None:
            value = max(value, self.bar_confirm_min_atr)
        if self.bar_confirm_max_atr is not None:
            value = min(value, self.bar_confirm_max_atr)
        return value

    def as_meta(self) -> dict[str, object]:
        return asdict(self)


PROFILES: dict[str, InstrumentProfile] = {
    "CRYPTO": InstrumentProfile(
        key="CRYPTO",
        label="BTC / Crypto",
        family="crypto",
        tick_size=1.0,
        bar_coil_min=14,
        bar_barcode_min=60,
        bar_range_max_atr=3.80,
        bar_squeeze_max=1.30,
        bar_late_rvol=2.35,
        bar_confirm_min_atr=0.05,
        bar_level_near_atr=0.28,
        bar_approach_body_atr=1.30,
        bar_impulse_range_atr=1.45,
        bar_barcode_max_mult=3.40,
        w5_watch=62,
        w5_arm=76,
        w5_runaway=82,
        wolfe_pivot_len=5,
        vsa_impulse_rvol=2.40,
        vsa_fuel_rvol=3.00,
        vsa_pullback_rvol=1.20,
        fuel_range_atr=1.25,
        indian_adx_min=26.0,
        indian_volume_mult=1.00,
        indian_extension_atr=0.42,
        stop_pad_atr=0.25,
        target_r_fade=1.50,
        target_r_transit=2.50,
        is_futures=False,
    ),
    "ES": InstrumentProfile(
        key="ES",
        label="ES",
        family="index_future",
        tick_size=0.25,
        bar_coil_min=10,
        bar_barcode_min=36,
        bar_range_max_atr=3.15,
        bar_squeeze_max=1.12,
        bar_late_rvol=2.05,
        bar_confirm_max_atr=0.02,
        bar_level_near_atr=0.20,
        bar_approach_body_atr=1.00,
        bar_impulse_range_atr=1.20,
        bar_barcode_max_mult=2.85,
        es_touch_break=True,
        w5_watch=58,
        w5_arm=70,
        w5_runaway=75,
        wolfe_pivot_len=3,
        vsa_impulse_rvol=2.00,
        vsa_fuel_rvol=2.50,
        vsa_pullback_rvol=1.05,
        fuel_range_atr=1.15,
        indian_adx_min=24.0,
        indian_volume_mult=0.80,
        indian_extension_atr=0.35,
        stop_pad_atr=0.32,
        target_r_fade=1.35,
        target_r_transit=1.85,
        is_futures=True,
        is_index=True,
    ),
    "SPY": InstrumentProfile(
        key="SPY",
        label="SPY",
        family="equity_etf",
        tick_size=0.01,
        bar_coil_min=12,
        bar_barcode_min=36,
        bar_range_max_atr=2.85,
        bar_squeeze_max=1.08,
        bar_late_rvol=1.95,
        bar_level_near_atr=0.30,
        bar_approach_body_atr=1.00,
        bar_impulse_range_atr=1.20,
        bar_barcode_max_mult=2.85,
        etf_option=True,
        w5_watch=58,
        w5_arm=70,
        w5_runaway=75,
        vsa_impulse_rvol=1.80,
        vsa_fuel_rvol=2.40,
        vsa_pullback_rvol=1.00,
        fuel_range_atr=1.10,
        indian_adx_min=22.0,
        indian_volume_mult=0.75,
        indian_extension_atr=0.32,
        stop_pad_atr=0.12,
        target_r_fade=1.25,
        target_r_transit=1.65,
        is_futures=False,
        is_index=True,
    ),
    "QQQ": InstrumentProfile(
        key="QQQ",
        label="QQQ",
        family="tech_index",
        tick_size=0.01,
        bar_coil_min=10,
        bar_barcode_min=36,
        bar_range_max_atr=3.20,
        bar_squeeze_max=1.18,
        bar_late_rvol=2.15,
        bar_confirm_min_atr=0.06,
        bar_level_near_atr=0.30,
        bar_approach_body_atr=1.15,
        bar_impulse_range_atr=1.20,
        bar_barcode_max_mult=3.10,
        etf_option=True,
        w5_watch=60,
        w5_arm=72,
        w5_runaway=77,
        vsa_impulse_rvol=2.10,
        vsa_fuel_rvol=2.60,
        vsa_pullback_rvol=1.10,
        fuel_range_atr=1.20,
        indian_adx_min=25.0,
        indian_volume_mult=0.85,
        indian_extension_atr=0.38,
        stop_pad_atr=0.15,
        target_r_fade=1.30,
        target_r_transit=1.80,
        is_futures=False,
        is_index=True,
    ),
    "SPX_INDEX": InstrumentProfile(
        key="SPX_INDEX",
        label="SPX",
        family="equity_index",
        tick_size=0.01,
        bar_coil_min=12,
        bar_barcode_min=36,
        bar_range_max_atr=2.85,
        bar_squeeze_max=1.08,
        bar_late_rvol=1.95,
        bar_level_near_atr=0.30,
        bar_approach_body_atr=1.00,
        bar_impulse_range_atr=1.20,
        bar_barcode_max_mult=2.85,
        etf_option=True,
        w5_watch=58,
        w5_arm=70,
        w5_runaway=75,
        vsa_impulse_rvol=1.80,
        vsa_fuel_rvol=2.40,
        vsa_pullback_rvol=1.00,
        fuel_range_atr=1.10,
        indian_adx_min=22.0,
        indian_volume_mult=0.75,
        indian_extension_atr=0.32,
        stop_pad_atr=0.12,
        target_r_fade=1.25,
        target_r_transit=1.65,
        is_futures=False,
        is_index=True,
    ),
    "EQUITY_STOCK": InstrumentProfile(
        key="EQUITY_STOCK",
        label="Stock",
        family="equity_stock",
        tick_size=0.01,
        bar_coil_min=12,
        bar_barcode_min=36,
        bar_range_max_atr=3.00,
        bar_squeeze_max=1.10,
        bar_late_rvol=2.00,
        bar_level_near_atr=0.30,
        bar_approach_body_atr=1.05,
        bar_impulse_range_atr=1.25,
        bar_barcode_max_mult=2.95,
        w5_watch=58,
        w5_arm=70,
        w5_runaway=76,
        vsa_impulse_rvol=2.00,
        vsa_fuel_rvol=2.50,
        vsa_pullback_rvol=1.05,
        fuel_range_atr=1.15,
        indian_adx_min=24.0,
        indian_volume_mult=0.80,
        indian_extension_atr=0.35,
        stop_pad_atr=0.14,
        target_r_fade=1.25,
        target_r_transit=1.75,
        is_futures=False,
        is_index=False,
    ),
    "FUTURE": InstrumentProfile(
        key="FUTURE",
        label="Future",
        family="future",
        tick_size=0.01,
        bar_coil_min=12,
        bar_barcode_min=36,
        bar_range_max_atr=3.00,
        bar_squeeze_max=1.08,
        bar_late_rvol=1.95,
        bar_level_near_atr=0.30,
        bar_approach_body_atr=1.00,
        bar_impulse_range_atr=1.20,
        bar_barcode_max_mult=2.85,
        vsa_impulse_rvol=1.90,
        vsa_fuel_rvol=2.40,
        vsa_pullback_rvol=1.05,
        fuel_range_atr=1.15,
        indian_adx_min=24.0,
        indian_volume_mult=0.80,
        indian_extension_atr=0.35,
        is_futures=True,
    ),
    "GOLD": InstrumentProfile(
        key="GOLD",
        label="Gold / GC",
        family="metal_future",
        tick_size=0.10,
        bar_coil_min=12,
        bar_barcode_min=40,
        bar_range_max_atr=3.05,
        bar_squeeze_max=1.14,
        bar_late_rvol=1.95,
        bar_level_near_atr=0.24,
        bar_approach_body_atr=1.05,
        bar_impulse_range_atr=1.25,
        bar_barcode_max_mult=2.95,
        w5_watch=56,
        w5_arm=68,
        w5_runaway=75,
        wolfe_pivot_len=2,
        vsa_impulse_rvol=2.20,
        vsa_fuel_rvol=2.70,
        vsa_pullback_rvol=1.10,
        fuel_range_atr=1.20,
        indian_adx_min=23.0,
        indian_volume_mult=0.90,
        indian_extension_atr=0.38,
        stop_pad_atr=0.18,
        target_r_fade=1.35,
        target_r_transit=1.90,
        is_futures=True,
    ),
    "OIL": InstrumentProfile(
        key="OIL",
        label="Crude / CL",
        family="energy_future",
        tick_size=0.01,
        bar_coil_min=14,
        bar_barcode_min=44,
        bar_range_max_atr=3.45,
        bar_squeeze_max=1.22,
        bar_late_rvol=2.20,
        bar_level_near_atr=0.26,
        bar_approach_body_atr=1.10,
        bar_impulse_range_atr=1.35,
        bar_barcode_max_mult=3.15,
        w5_watch=60,
        w5_arm=72,
        w5_runaway=78,
        vsa_impulse_rvol=2.30,
        vsa_fuel_rvol=2.80,
        vsa_pullback_rvol=1.15,
        fuel_range_atr=1.25,
        indian_adx_min=25.0,
        indian_volume_mult=1.00,
        indian_extension_atr=0.42,
        stop_pad_atr=0.20,
        target_r_fade=1.40,
        target_r_transit=2.00,
        is_futures=True,
    ),
    "CUSTOM": InstrumentProfile(
        key="CUSTOM",
        label="Custom",
        family="custom",
        tick_size=0.01,
        bar_coil_min=12,
        bar_barcode_min=36,
        bar_range_max_atr=3.00,
        bar_squeeze_max=1.08,
        bar_late_rvol=1.95,
        bar_level_near_atr=0.30,
        bar_approach_body_atr=1.00,
        bar_impulse_range_atr=1.20,
        bar_barcode_max_mult=2.85,
        vsa_impulse_rvol=1.90,
        vsa_fuel_rvol=2.40,
        vsa_pullback_rvol=1.05,
        fuel_range_atr=1.15,
        indian_adx_min=24.0,
        indian_volume_mult=0.80,
        indian_extension_atr=0.35,
    ),
}

EQUITY_STOCK_INSTRUMENT_KEYS = {
    "AAPL",
    "AMZN",
    "GOOG",
    "GOOGL",
    "META",
    "MSFT",
    "NVDA",
    "PLTR",
    "TSLA",
}

_PROFILE_BY_INSTRUMENT_KEY = {
    "BTC": "CRYPTO",
    "ETH": "CRYPTO",
    "ES": "ES",
    "SPY": "SPY",
    "QQQ": "QQQ",
    "SPX": "SPX_INDEX",
    "GC": "GOLD",
    "CL": "OIL",
    **{key: "EQUITY_STOCK" for key in EQUITY_STOCK_INSTRUMENT_KEYS},
}

_PROFILE_BY_ASSET_CLASS = {
    "crypto": "CRYPTO",
    "stock": "EQUITY_STOCK",
    "equity": "EQUITY_STOCK",
    "future": "FUTURE",
}


def resolve_instrument_profile(instrument: dict[str, Any]) -> InstrumentProfile:
    qualified = require_provider_identity(instrument)
    configured_key = str(qualified.get("profile") or "").strip().upper()
    if configured_key:
        profile = PROFILES.get(configured_key)
        if profile is None:
            raise ValueError(
                f"INSTRUMENT_PROFILE_UNKNOWN profile={configured_key} instrument_key={instrument_key(qualified)}"
            )
        return profile
    profile_key = _PROFILE_BY_INSTRUMENT_KEY.get(instrument_key(qualified))
    if profile_key is None:
        profile_key = _PROFILE_BY_ASSET_CLASS.get(instrument_asset_class(qualified), "CUSTOM")
    return PROFILES[profile_key]
