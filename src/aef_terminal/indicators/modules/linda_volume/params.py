from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from aef_terminal.features.vsa import VsaParams
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    IndicatorDefaults,
    label_importance_floor,
)
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import bool_param
from aef_terminal.runtime.timeframes import adapt_bars


@dataclass(frozen=True)
class LindaVolumeParams(VsaParams):
    use_profile_thresholds: bool = True
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    ema_pullback_len: int = DEFAULT_INDICATOR_SETTINGS.ema.pullback
    ema_trend_len: int = 50
    ema_magnet_len: int = DEFAULT_INDICATOR_SETTINGS.ema.magnet
    min_label_score: int = 82
    pullback_rvol: float = 1.05
    pullback_atr_tolerance: float = 0.18
    no_demand_rvol: float = 0.78
    indian_min_trend_bars: int = 1
    indian_adx_min: float = 24.0
    indian_volume_mult: float = 0.8
    indian_min_score: float = 72.0
    indian_use_volume_filter: bool = False
    indian_require_extension: bool = True
    indian_extension_atr: float = 0.35
    indian_extension_lookback: int = 8
    indian_spacing_bars: int = 5
    enable_grail: bool = True
    enable_indians: bool = True
    enable_turtle_soup: bool = False
    enable_turtle_soup_plus_one: bool = False
    enable_eighty_twenty: bool = False
    enable_the_anti: bool = False
    enable_momentum_pinball: bool = False
    enable_hv_squeeze: bool = False
    enable_adx_gapper: bool = False
    score_watch: float = DEFAULT_INDICATOR_SETTINGS.score.watch
    score_arm: float = DEFAULT_INDICATOR_SETTINGS.score.arm
    score_go: float = DEFAULT_INDICATOR_SETTINGS.score.go


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> LindaVolumeParams:
    importance_floor = label_importance_floor(defaults.score)
    return LindaVolumeParams(
        atr_len=defaults.atr_len,
        ema_pullback_len=defaults.ema.pullback,
        ema_magnet_len=defaults.ema.magnet,
        min_label_score=float(raw.get("min_score", LindaVolumeParams.min_label_score)),
        use_profile_thresholds=bool_param(
            raw.get("use_profile_thresholds"),
            True,
        ),
        indian_adx_min=float(raw.get("indian_adx_min", LindaVolumeParams.indian_adx_min)),
        indian_extension_atr=float(
            raw.get(
                "indian_extension_atr",
                LindaVolumeParams.indian_extension_atr,
            )
        ),
        indian_extension_lookback=int(
            raw.get(
                "indian_extension_lookback",
                LindaVolumeParams.indian_extension_lookback,
            )
        ),
        indian_min_score=float(raw.get("indian_min_score", importance_floor)),
        enable_grail=bool_param(raw.get("enable_grail"), True),
        enable_indians=bool_param(raw.get("enable_indians"), True),
        enable_turtle_soup=bool_param(raw.get("enable_turtle_soup"), False),
        enable_turtle_soup_plus_one=bool_param(
            raw.get("enable_turtle_soup_plus_one"),
            False,
        ),
        enable_eighty_twenty=bool_param(
            raw.get("enable_eighty_twenty"),
            False,
        ),
        enable_the_anti=bool_param(raw.get("enable_the_anti"), False),
        enable_momentum_pinball=bool_param(
            raw.get("enable_momentum_pinball"),
            False,
        ),
        enable_hv_squeeze=bool_param(raw.get("enable_hv_squeeze"), False),
        enable_adx_gapper=bool_param(raw.get("enable_adx_gapper"), False),
        score_watch=defaults.score.watch,
        score_arm=defaults.score.arm,
        score_go=defaults.score.go,
    )


@dataclass(frozen=True)
class LindaGrailTuning:
    grail_vol_mult: float
    grail_min_rr: float
    stop_mult: float
    space_coeff: float


LINDA_GRAIL_TUNING: dict[str, LindaGrailTuning] = {
    "CRYPTO": LindaGrailTuning(1.60, 1.80, 0.25, 0.65),
    "SPY": LindaGrailTuning(1.20, 1.40, 0.18, 0.45),
    "QQQ": LindaGrailTuning(1.40, 1.60, 0.18, 0.55),
    "ES": LindaGrailTuning(1.30, 1.50, 0.15, 0.50),
    "GOLD": LindaGrailTuning(1.40, 1.60, 0.20, 0.55),
    "OIL": LindaGrailTuning(1.50, 1.70, 0.22, 0.60),
    "CUSTOM": LindaGrailTuning(1.30, 1.50, 0.15, 0.50),
}


def grail_tuning(profile: InstrumentProfile) -> LindaGrailTuning:
    return LINDA_GRAIL_TUNING.get(profile.key, LINDA_GRAIL_TUNING["CUSTOM"])


def adapt_linda_bars(base: int, timeframe: str) -> int:
    return adapt_bars(base, timeframe, source_minutes=5)


def profile_adjusted_params(
    profile: InstrumentProfile,
    params: LindaVolumeParams,
) -> tuple[LindaVolumeParams, str]:
    if not params.use_profile_thresholds:
        return params, profile.label
    return (
        replace(
            params,
            impulse_rvol=profile.vsa_impulse_rvol,
            fuel_rvol=profile.vsa_fuel_rvol,
            fuel_range_atr=profile.fuel_range_atr,
            pullback_rvol=profile.vsa_pullback_rvol,
            indian_adx_min=profile.indian_adx_min,
            indian_volume_mult=profile.indian_volume_mult,
            indian_extension_atr=profile.indian_extension_atr,
        ),
        profile.label,
    )
