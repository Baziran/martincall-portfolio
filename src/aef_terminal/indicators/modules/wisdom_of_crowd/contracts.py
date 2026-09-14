"""Typed contracts for the Wisdom of Crowd trend-continuation prototype."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults
from aef_terminal.runtime.math_utils import bool_param, clamp_float, clamp_int


WISDOM_OF_CROWD_VERSION = "0.2-prototype"
WISDOM_OF_CROWD_SOURCE = "wisdom_of_crowd"


@dataclass(frozen=True, slots=True)
class WisdomOfCrowdParams:
    """Small, deliberately inspectable parameter surface for the prototype."""

    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    ema_fast_len: int = DEFAULT_INDICATOR_SETTINGS.ema.pullback
    ema_slow_len: int = 50
    ema_magnet_len: int = DEFAULT_INDICATOR_SETTINGS.ema.magnet
    slope_lookback: int = 5
    pullback_bars: int = 5
    session_min_bars: int = 5
    structure_bars: int = 5
    cooldown_bars: int = 3
    pullback_tolerance_atr: float = 0.20
    pullback_break_atr: float = 0.25
    min_slope_atr: float = 0.02
    max_extension_atr: float = 0.90
    min_stop_atr: float = 0.50
    stop_buffer_atr: float = 0.12
    reward_risk: float = 2.0
    watch_score: float = DEFAULT_INDICATOR_SETTINGS.score.watch
    arm_score: float = DEFAULT_INDICATOR_SETTINGS.score.arm
    go_score: float = DEFAULT_INDICATOR_SETTINGS.score.go
    max_age_bars: int = 18
    trail_atr: float = 1.10
    labels: bool = True

    @property
    def required_bars(self) -> int:
        return max(
            self.ema_slow_len + self.slope_lookback,
            self.atr_len + self.slope_lookback,
            self.structure_bars + self.pullback_bars + 2,
        )


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> WisdomOfCrowdParams:
    watch_score = clamp_float(raw.get("watch_score"), defaults.score.watch, 40.0, 90.0)
    arm_score = clamp_float(raw.get("arm_score"), defaults.score.arm, watch_score, 95.0)
    go_score = clamp_float(raw.get("go_score"), defaults.score.go, arm_score, 99.0)
    return WisdomOfCrowdParams(
        atr_len=defaults.atr_len,
        ema_fast_len=defaults.ema.pullback,
        ema_slow_len=clamp_int(raw.get("ema_slow_len"), 50, 20, 200),
        ema_magnet_len=defaults.ema.magnet,
        slope_lookback=clamp_int(raw.get("slope_lookback"), 5, 2, 20),
        pullback_bars=clamp_int(raw.get("pullback_bars"), 5, 2, 12),
        session_min_bars=clamp_int(raw.get("session_min_bars"), 5, 1, 30),
        structure_bars=clamp_int(raw.get("structure_bars"), 5, 3, 12),
        cooldown_bars=clamp_int(raw.get("cooldown_bars"), 3, 1, 12),
        pullback_tolerance_atr=clamp_float(raw.get("pullback_tolerance_atr"), 0.20, 0.0, 0.75),
        pullback_break_atr=clamp_float(raw.get("pullback_break_atr"), 0.25, 0.0, 1.0),
        min_slope_atr=clamp_float(raw.get("min_slope_atr"), 0.02, 0.0, 0.30),
        max_extension_atr=clamp_float(raw.get("max_extension_atr"), 0.90, 0.25, 2.50),
        min_stop_atr=clamp_float(raw.get("min_stop_atr"), 0.50, 0.20, 2.0),
        stop_buffer_atr=clamp_float(raw.get("stop_buffer_atr"), 0.12, 0.0, 0.75),
        reward_risk=clamp_float(raw.get("reward_risk"), 2.0, 1.0, 5.0),
        watch_score=watch_score,
        arm_score=arm_score,
        go_score=go_score,
        max_age_bars=clamp_int(raw.get("max_age_bars"), 18, 3, 60),
        trail_atr=clamp_float(raw.get("trail_atr"), 1.10, 0.5, 4.0),
        labels=bool_param(raw.get("labels"), True),
    )


def resolve_signal_name(_source: str, _code: str, _action: str) -> str:
    return "crowd_trend_continuation"


ROW_FIELDS = (
    "action",
    "adx",
    "blocked_reason",
    "code",
    "direction",
    "distance_fast_atr",
    "distance_vwap_atr",
    "ema_fast",
    "ema_fast_slope_atr",
    "ema_slow",
    "ema_slow_slope_atr",
    "extension_atr",
    "lifecycle",
    "price",
    "pullback_age_bars",
    "pullback_level",
    "reclaim_confirmed",
    "regime_code",
    "rr",
    "score",
    "session_age_bars",
    "signal",
    "source",
    "state_code",
    "stop",
    "structure_score",
    "target",
    "trigger",
    "ts",
    "vwap",
    "scenario",
    "setup",
    "trigger_event",
    "evidence",
    "risk",
    "quality",
    "fact_groups",
    "metrics",
    "trade_plan",
)


COMPACT_ROW_FIELDS = (
    "action",
    "code",
    "direction",
    "price",
    "pullback_age_bars",
    "reclaim_confirmed",
    "regime_code",
    "score",
    "state_code",
    "stop",
    "target",
    "trigger",
    "ts",
)


__all__ = [
    "COMPACT_ROW_FIELDS",
    "ROW_FIELDS",
    "WISDOM_OF_CROWD_SOURCE",
    "WISDOM_OF_CROWD_VERSION",
    "WisdomOfCrowdParams",
    "build_params",
    "resolve_signal_name",
]
