from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aef_terminal.domain import StrategyMode
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.indicators.settings_schema import GLOBAL_DEFAULT_FIELDS
from aef_terminal.indicators.settings_resolution import indicator_bool_value as _bool_value
from aef_terminal.settings_contract import global_indicator_setting_key
from aef_terminal.ui.paper.config import paper_min_rr as resolve_paper_min_rr


def _paper_min_rr(value: object) -> float:
    return resolve_paper_min_rr(value)


def _query_value(
    params: Mapping[str, object],
    key: str,
    default: bool | int | float | str,
) -> object:
    value = params.get(key)
    return default if value is None else value


def _number_value(
    value: object,
    default: int | float,
    minimum: int | float | None,
    maximum: int | float | None,
) -> int | float:
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return default
    if minimum is not None:
        parsed = max(float(minimum), parsed)
    if maximum is not None:
        parsed = min(float(maximum), parsed)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(round(parsed))
    return parsed


def _select_value(value: object, default: str, options: list[str]) -> str:
    text = str(value).strip()
    for option in options:
        if text == option or text.lower() == option.lower():
            return option
    return default


def _control_value(control: Mapping[str, Any], raw_value: object) -> bool | int | float | str:
    default = control["default"]
    control_type = control["control_type"]
    if control_type == "toggle":
        return _bool_value(raw_value, bool(default))
    if control_type == "number":
        return _number_value(raw_value, default, control.get("minimum"), control.get("maximum"))
    if control_type == "select":
        return _select_value(raw_value, str(default), list(control.get("options") or ()))
    return str(raw_value if raw_value is not None else default)


def server_alert_indicator_params(settings: Mapping[str, object] | None) -> dict[str, Any]:
    values = settings if isinstance(settings, Mapping) else {}
    resolved = {
        field.key: values.get(
            global_indicator_setting_key(field.storage_key),
            field.default,
        )
        for field in GLOBAL_DEFAULT_FIELDS
    }

    return {
        "global_defaults": {
            "atr_len": resolved["atr_len"],
            "rvol_len": resolved["rvol_len"],
            "ema": {
                "pullback": resolved["ema_pullback"],
                "fast": resolved["ema_fast"],
                "slow": resolved["ema_slow"],
                "magnet": resolved["ema_magnet"],
            },
            "score": {
                "pre": resolved["score_pre"],
                "watch": resolved["score_watch"],
                "arm": resolved["score_arm"],
                "go": resolved["score_go"],
            },
            "rvol": {
                "low": resolved["rvol_low"],
                "elevated": resolved["rvol_elevated"],
                "high": resolved["rvol_high"],
                "climax": resolved["rvol_climax"],
            },
        }
    }


def market_indicator_params(
    *,
    global_atr_len: int,
    global_rvol_len: int,
    global_ema_pullback: int,
    global_ema_fast: int,
    global_ema_slow: int,
    global_ema_magnet: int,
    global_score_pre: float,
    global_score_watch: float,
    global_score_arm: float,
    global_score_go: float,
    global_rvol_low: float,
    global_rvol_elevated: float,
    global_rvol_high: float,
    global_rvol_climax: float,
    strategy_mode: StrategyMode,
    signal_min_rr: float,
    manual_channel_payload: list | None = None,
    manual_channel_canonical_generation: int | None = None,
    indicator_query_params: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    raw_params: Mapping[str, object] = indicator_query_params or {}
    params: dict[str, Any] = {
        "global_defaults": {
            "atr_len": global_atr_len,
            "rvol_len": global_rvol_len,
            "ema": {
                "pullback": global_ema_pullback,
                "fast": global_ema_fast,
                "slow": global_ema_slow,
                "magnet": global_ema_magnet,
            },
            "score": {
                "pre": global_score_pre,
                "watch": global_score_watch,
                "arm": global_score_arm,
                "go": global_score_go,
            },
            "rvol": {
                "low": global_rvol_low,
                "elevated": global_rvol_elevated,
                "high": global_rvol_high,
                "climax": global_rvol_climax,
            },
        },
        "signal_quality": {
            "min_rr": _paper_min_rr(signal_min_rr),
        },
        "decision": {
            "strategy_mode": strategy_mode.value,
        },
    }
    if manual_channel_payload is not None:
        params["manual_channels"] = manual_channel_payload
    if manual_channel_canonical_generation is not None:
        params["manual_channel_canonical_generation"] = manual_channel_canonical_generation

    for indicator_id, spec in indicator_manifest().items():
        ui = spec["ui"]
        if spec["module_type"] == "ui-only" and not ui.get("api_enabled_key"):
            continue
        section: dict[str, Any] = {}
        enabled_key = ui.get("api_enabled_key") or ""
        if enabled_key:
            section["enabled"] = _bool_value(
                _query_value(raw_params, enabled_key, ui["default_calc"]),
                bool(ui["default_calc"]),
            )
        visible_key = ui.get("api_visible_key") or ""
        if visible_key:
            section["visible"] = _bool_value(
                _query_value(raw_params, visible_key, ui["default_visible"]),
                bool(ui["default_visible"]),
            )
        for control in spec["controls"]:
            api_key = control.get("api_key") or ""
            if not api_key:
                continue
            param_key = control.get("param_key") or control["key"]
            section[str(param_key)] = _control_value(
                control,
                _query_value(raw_params, api_key, control["default"]),
            )
        if section:
            params[indicator_id] = section

    return params
