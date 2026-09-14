from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aef_terminal.runtime.math_utils import clamp_float, clamp_int
from aef_terminal.runtime import pine


@dataclass(frozen=True)
class EmaDefaults:
    pullback: int = 20
    fast: int = 21
    slow: int = 55
    magnet: int = 233


@dataclass(frozen=True)
class ScoreDefaults:
    pre: float = 42.0
    watch: float = 58.0
    arm: float = 70.0
    go: float = 78.0


@dataclass(frozen=True)
class RvolDefaults:
    low: float = 0.85
    elevated: float = 1.10
    high: float = 1.20
    climax: float = 1.60


@dataclass(frozen=True)
class IndicatorDefaults:
    atr_len: int = 14
    rvol_len: int = 30
    ema: EmaDefaults = field(default_factory=EmaDefaults)
    score: ScoreDefaults = field(default_factory=ScoreDefaults)
    rvol: RvolDefaults = field(default_factory=RvolDefaults)


DEFAULT_INDICATOR_SETTINGS = IndicatorDefaults()


def score_bands_with_thresholds(
    *,
    watch: float,
    arm: float,
    go: float,
) -> ScoreDefaults:
    """Build one score ladder while preserving the canonical PRE threshold."""

    return ScoreDefaults(
        pre=DEFAULT_INDICATOR_SETTINGS.score.pre,
        watch=watch,
        arm=arm,
        go=go,
    )


@dataclass(frozen=True)
class ScorePromotionFloors:
    watch: float
    strong_watch: float
    structure: float
    label_importance: float


@dataclass(frozen=True)
class BreakoutModeThresholdOffsets:
    arm: float
    go: float
    rev: float


_BREAKOUT_MODE_OFFSETS: dict[str, BreakoutModeThresholdOffsets] = {
    "early": BreakoutModeThresholdOffsets(arm=-18.0, go=-18.0, rev=-4.0),
    "balanced": BreakoutModeThresholdOffsets(arm=-10.0, go=-10.0, rev=4.0),
    "strict": BreakoutModeThresholdOffsets(arm=0.0, go=0.0, rev=14.0),
}


def breakout_mode_thresholds(
    trade_mode: str,
    bands: ScoreDefaults | None = None,
) -> tuple[float, float, float]:
    """Return (arm_score, go_score, rev_score_need) for breakout probability gates."""
    bands = bands or DEFAULT_INDICATOR_SETTINGS.score
    key = str(trade_mode or "balanced").strip().lower()
    offsets = _BREAKOUT_MODE_OFFSETS.get(key, _BREAKOUT_MODE_OFFSETS["balanced"])
    arm_score = pine.clamp(bands.arm + offsets.arm, bands.pre, 99.0)
    go_score = pine.clamp(bands.go + offsets.go, bands.watch, 99.0)
    rev_score_need = pine.clamp(bands.watch + offsets.rev, bands.pre, bands.go)
    return arm_score, go_score, rev_score_need


def score_action(
    score: float,
    bands: ScoreDefaults | None = None,
    *,
    go_floor: float | None = None,
    pre_action: str = "CANDIDATE",
) -> str:
    """Map a 0-99 score to the canonical CANDIDATE/WATCH/ARM/GO ladder."""
    bands = bands or DEFAULT_INDICATOR_SETTINGS.score
    value = pine.clamp(float(score), 0.0, 99.0)
    if go_floor is not None and value >= pine.clamp(float(go_floor), bands.watch, 99.0):
        return "GO"
    if value >= bands.go:
        return "GO"
    if value >= bands.arm:
        return "ARM"
    if value >= bands.watch:
        return "WATCH"
    if value >= bands.pre:
        return str(pre_action or "CANDIDATE").upper()
    return "WAIT"


def label_importance_floor(bands: ScoreDefaults | None = None) -> float:
    """Visibility/importance threshold used by VSA labels and SMC rebound plans."""
    bands = bands or DEFAULT_INDICATOR_SETTINGS.score
    return max(bands.arm + 2.0, bands.watch + 4.0)


def score_promotion_floors(defaults: IndicatorDefaults | None = None) -> ScorePromotionFloors:
    """Shared candidate-promotion floors derived from global score bands."""
    bands = (defaults or DEFAULT_INDICATOR_SETTINGS).score
    watch = bands.watch
    return ScorePromotionFloors(
        watch=watch,
        strong_watch=max(watch + 2.0, 60.0),
        structure=max(watch + 4.0, 62.0),
        label_importance=label_importance_floor(bands),
    )


def _ema_len(value: Any, default: int, low: int, high: int) -> int:
    try:
        out = int(value)
    except TypeError, ValueError:
        return default
    if out < low or out > high:
        return default
    return out


def indicator_defaults_from_params(params: dict[str, Any] | None) -> IndicatorDefaults:
    raw = params if isinstance(params, dict) else {}
    base = DEFAULT_INDICATOR_SETTINGS
    ema_raw = raw.get("ema") if isinstance(raw.get("ema"), dict) else {}
    score_raw = raw.get("score") if isinstance(raw.get("score"), dict) else {}
    rvol_raw = raw.get("rvol") if isinstance(raw.get("rvol"), dict) else {}
    return IndicatorDefaults(
        atr_len=clamp_int(raw.get("atr_len"), base.atr_len, 2, 100),
        rvol_len=clamp_int(raw.get("rvol_len"), base.rvol_len, 2, 200),
        ema=EmaDefaults(
            pullback=_ema_len(ema_raw.get("pullback"), base.ema.pullback, 5, 300),
            fast=_ema_len(ema_raw.get("fast"), base.ema.fast, 5, 300),
            slow=_ema_len(ema_raw.get("slow"), base.ema.slow, 10, 400),
            magnet=_ema_len(ema_raw.get("magnet"), base.ema.magnet, 50, 1000),
        ),
        score=ScoreDefaults(
            pre=clamp_float(score_raw.get("pre"), base.score.pre, 0.0, 99.0),
            watch=clamp_float(score_raw.get("watch"), base.score.watch, 0.0, 99.0),
            arm=clamp_float(score_raw.get("arm"), base.score.arm, 0.0, 99.0),
            go=clamp_float(score_raw.get("go"), base.score.go, 0.0, 99.0),
        ),
        rvol=RvolDefaults(
            low=clamp_float(rvol_raw.get("low"), base.rvol.low, 0.0, 5.0),
            elevated=clamp_float(rvol_raw.get("elevated"), base.rvol.elevated, 0.0, 5.0),
            high=clamp_float(rvol_raw.get("high"), base.rvol.high, 0.0, 5.0),
            climax=clamp_float(rvol_raw.get("climax"), base.rvol.climax, 0.0, 10.0),
        ),
    )
