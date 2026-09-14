from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aef_terminal.indicators.module_discovery import indicator_module_catalog
from aef_terminal.indicators.registry import indicator_manifest


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    default: float | int | bool
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    storage_key: str = ""


GLOBAL_DEFAULT_FIELDS: tuple[SettingField, ...] = (
    SettingField("atr_len", "ATR", 14, 2, 100, 1, "globalAtrLen"),
    SettingField("rvol_len", "RVOL", 30, 2, 200, 1, "globalRvolLen"),
    SettingField("ema_pullback", "EMA PB", 20, 2, 300, 1, "globalEmaPullback"),
    SettingField("ema_fast", "EMA fast", 21, 2, 300, 1, "globalEmaFast"),
    SettingField("ema_slow", "EMA slow", 55, 2, 400, 1, "globalEmaSlow"),
    SettingField("ema_magnet", "EMA mag", 233, 2, 1000, 1, "globalEmaMagnet"),
    SettingField("score_pre", "PRE", 42, 0, 99, 1, "globalScorePre"),
    SettingField("score_watch", "WATCH", 58, 0, 99, 1, "globalScoreWatch"),
    SettingField("score_arm", "ARM", 70, 0, 99, 1, "globalScoreArm"),
    SettingField("score_go", "GO", 78, 0, 99, 1, "globalScoreGo"),
    SettingField("rvol_low", "RVOL low", 0.85, 0, 5, 0.05, "globalRvolLow"),
    SettingField("rvol_elevated", "RVOL +", 1.10, 0, 5, 0.05, "globalRvolElevated"),
    SettingField("rvol_high", "RVOL high", 1.20, 0, 5, 0.05, "globalRvolHigh"),
    SettingField("rvol_climax", "RVOL clim", 1.60, 0, 10, 0.05, "globalRvolClimax"),
)


GLOBAL_DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {},
    "scalp": {
        "score_watch": 56,
        "score_arm": 68,
        "score_go": 76,
    },
    "conservative": {
        "score_watch": 62,
        "score_arm": 74,
        "score_go": 82,
        "rvol_low": 0.90,
        "rvol_elevated": 1.15,
    },
    "research": {
        "score_watch": 50,
        "score_arm": 64,
        "score_go": 72,
    },
}


def global_settings_schema() -> dict[str, Any]:
    return {
        "version": 1,
        "fields": [field.__dict__ for field in GLOBAL_DEFAULT_FIELDS],
        "presets": GLOBAL_DEFAULT_PRESETS,
        "indicator_controls": {
            indicator_id: item["controls"] for indicator_id, item in indicator_manifest().items()
        },
        "indicator_module_catalog": indicator_module_catalog(),
    }
