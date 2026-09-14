"""Typed contracts and bounded parameters for Adaptive Kernel Regression."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.runtime.math_utils import bool_param, clamp_float, clamp_int


ADAPTIVE_KERNEL_REGRESSION_SOURCE = "adaptive_kernel_regression"
ADAPTIVE_KERNEL_REGRESSION_VERSION = "1.3"
ADAPTIVE_KERNEL_ADAPTATION_STRENGTH = 200.0
ADAPTIVE_KERNEL_ENTRY_PLAN_WAIT_MINUTES = 60
ADAPTIVE_KERNEL_ENTRY_PLAN_TIMEFRAMES = ("3m", "5m")
ADAPTIVE_KERNEL_CALCULATION_MODES = ("confirmed", "provisional")
ADAPTIVE_KERNEL_LABEL_ANCHORS = ("high_low", "main", "bands")
ADAPTIVE_KERNEL_LABEL_ANCHOR_OPTIONS = ("High/Low", "Main Line", "Bands")


@dataclass(frozen=True)
class AdaptiveKernelRegressionParams:
    calculation_mode: str = "confirmed"
    lookback_window: int = 30
    base_bandwidth: float = 8.0
    adaptive_bandwidth: bool = True
    atr_len: int = 14
    output_smoothing: int = 3
    band_multiplier: float = 1.0
    band_lookback: int = 24
    band_smoothing: int = 5
    entry_plan_enabled: bool = False
    label_anchor: str = "main"
    label_offset_mult: float = 0.5
    render_bars: int = 240
    max_events: int = 80

    @property
    def required_bars(self) -> int:
        kernel_warmup = max(
            self.lookback_window,
            self.atr_len if self.adaptive_bandwidth else 1,
        )
        return kernel_warmup + self.band_lookback - 1


SERIES_FIELDS = (
    "center",
    "deviation_z",
    "effective_bandwidth",
    "index",
    "lower",
    "regime_age_bars",
    "regime_code",
    "residual",
    "residual_sigma",
    "state",
    "ts",
    "upper",
)


SERIES_COMPACT_FIELDS = (
    "center",
    "index",
    "lower",
    "residual",
    "state",
    "ts",
    "upper",
)


EVENT_FIELDS = (
    "bar_high",
    "bar_low",
    "center",
    "code",
    "confirmed",
    "deviation_z",
    "direction",
    "effective_bandwidth",
    "event_code",
    "index",
    "label_atr",
    "lower",
    "previous_state",
    "price",
    "source",
    "state",
    "ts",
    "upper",
)


def build_params(
    raw: Mapping[str, Any],
    _defaults: IndicatorDefaults,
) -> AdaptiveKernelRegressionParams:
    calculation_mode = (
        str(
            raw.get(
                "calculation_mode",
                AdaptiveKernelRegressionParams.calculation_mode,
            )
        )
        .strip()
        .lower()
    )
    if calculation_mode not in ADAPTIVE_KERNEL_CALCULATION_MODES:
        calculation_mode = AdaptiveKernelRegressionParams.calculation_mode
    label_anchor_value = (
        str(
            raw.get(
                "label_anchor",
                AdaptiveKernelRegressionParams.label_anchor,
            )
        )
        .strip()
        .lower()
    )
    label_anchor = label_anchor_value
    if label_anchor not in ADAPTIVE_KERNEL_LABEL_ANCHORS:
        label_anchor = AdaptiveKernelRegressionParams.label_anchor
    return AdaptiveKernelRegressionParams(
        calculation_mode=calculation_mode,
        lookback_window=clamp_int(
            raw.get("lookback_window"),
            AdaptiveKernelRegressionParams.lookback_window,
            10,
            300,
        ),
        base_bandwidth=clamp_float(
            raw.get("base_bandwidth"),
            AdaptiveKernelRegressionParams.base_bandwidth,
            0.5,
            64.0,
        ),
        adaptive_bandwidth=bool_param(
            raw.get("adaptive_bandwidth"),
            AdaptiveKernelRegressionParams.adaptive_bandwidth,
        ),
        atr_len=clamp_int(
            raw.get("atr_len"),
            AdaptiveKernelRegressionParams.atr_len,
            5,
            100,
        ),
        output_smoothing=clamp_int(
            raw.get("output_smoothing"),
            AdaptiveKernelRegressionParams.output_smoothing,
            1,
            20,
        ),
        band_multiplier=clamp_float(
            raw.get("band_multiplier"),
            AdaptiveKernelRegressionParams.band_multiplier,
            0.5,
            5.0,
        ),
        band_lookback=clamp_int(
            raw.get("band_lookback"),
            AdaptiveKernelRegressionParams.band_lookback,
            10,
            200,
        ),
        band_smoothing=clamp_int(
            raw.get("band_smoothing"),
            AdaptiveKernelRegressionParams.band_smoothing,
            1,
            20,
        ),
        entry_plan_enabled=bool_param(
            raw.get("entry_plan_enabled"),
            AdaptiveKernelRegressionParams.entry_plan_enabled,
        ),
        label_anchor=label_anchor,
        label_offset_mult=clamp_float(
            raw.get("label_offset_mult"),
            AdaptiveKernelRegressionParams.label_offset_mult,
            0.0,
            3.0,
        ),
        render_bars=clamp_int(
            raw.get("render_bars"),
            AdaptiveKernelRegressionParams.render_bars,
            60,
            240,
        ),
    )
