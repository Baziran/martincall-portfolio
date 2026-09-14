from __future__ import annotations

from typing import Any

from aef_terminal.settings_contract import (
    GEX_SCHEDULER_SETTING_KEY,
    invalid_server_setting_value_keys,
    require_option_target_caps,
)
from aef_terminal.ui.routers.error_payloads import build_action_error_response


def gex_storage_unavailable(**extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code="GEX_STORAGE_NOT_CONFIGURED",
        category="gex",
        retryable=False,
        error="storage not configured",
        **extra,
    )


def gex_validation_error(
    code: str,
    message: str,
    *,
    category: str = "gex",
    retryable: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    return build_action_error_response(
        code=code,
        category=category,
        retryable=retryable,
        error=message,
        **extra,
    )


def save_gex_scheduler_settings(deps: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if invalid_server_setting_value_keys({GEX_SCHEDULER_SETTING_KEY: payload}):
        return gex_validation_error(
            "GEX_SCHEDULER_SETTING_INVALID",
            "scheduler setting must contain exactly enabled:boolean and a non-empty typed instrument_ids list",
        )
    enabled = payload["enabled"]
    instrument_ids = list(payload["instrument_ids"])
    try:
        saved = deps.save_gex_scheduler_settings(
            {"enabled": enabled, "instrument_ids": instrument_ids}
        )
        return {"ok": True, **saved}
    except RuntimeError as exc:
        if str(exc) == "GEX_SCHEDULER_SETTINGS_STORAGE_REQUIRED":
            return gex_storage_unavailable()
        return build_action_error_response(
            code="GEX_SCHEDULER_SAVE_ERROR",
            category="gex",
            retryable=True,
            error=exc,
        )
    except Exception as exc:
        return build_action_error_response(
            code="GEX_SCHEDULER_SAVE_ERROR",
            category="gex",
            retryable=True,
            error=exc,
        )


def save_option_settings(deps: Any, payload: dict[str, Any]) -> dict[str, Any]:
    raw_caps = (
        payload.get("premium_caps") if isinstance(payload.get("premium_caps"), dict) else payload
    )
    if not isinstance(raw_caps, dict):
        return gex_validation_error(
            "OPTION_PREMIUM_CAPS_REQUIRED",
            "premium_caps object is required",
            category="options",
        )
    try:
        caps = require_option_target_caps(raw_caps)
    except (TypeError, ValueError) as exc:
        return gex_validation_error(
            "OPTION_PREMIUM_CAPS_INVALID",
            str(exc),
            category="options",
        )
    try:
        saved = deps.save_option_target_caps_settings(caps)
        return {"ok": True, "premium_caps": saved}
    except RuntimeError as exc:
        if str(exc) == "OPTION_TARGET_SETTINGS_STORAGE_REQUIRED":
            return build_action_error_response(
                code="OPTION_STORAGE_NOT_CONFIGURED",
                category="options",
                retryable=False,
                error="storage not configured",
            )
        return build_action_error_response(
            code="OPTION_SETTINGS_SAVE_ERROR",
            category="options",
            retryable=True,
            error=exc,
        )
    except ValueError as exc:
        return gex_validation_error(
            "OPTION_PREMIUM_CAPS_INVALID",
            str(exc),
            category="options",
        )
    except Exception as exc:
        return build_action_error_response(
            code="OPTION_SETTINGS_SAVE_ERROR",
            category="options",
            retryable=True,
            error=exc,
        )
