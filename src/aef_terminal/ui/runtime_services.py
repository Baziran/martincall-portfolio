from collections.abc import Sequence
from typing import Any

from aef_terminal.data.gex.constants import GEX_REQUEST_STRIKE_COUNT, GEX_SCHEDULER_INTERVAL_MINUTES
from aef_terminal.data.instrument_identity import require_exact_instrument_id_sequence
from aef_terminal.settings_contract import (
    GEX_SCHEDULER_SETTING_KEY,
    TELEGRAM_INTERACTIVE_SETTING_KEY,
    invalid_server_setting_value_keys,
)


def coerce_ibkr_port(value: Any) -> int | None:
    try:
        port = int(str(value).strip())
    except TypeError, ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def telegram_interactive_runtime_settings(
    *,
    env_enabled: bool,
    configured: bool,
    raw_setting: object = None,
    settings_error: Exception | str | None = None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "enabled": bool(env_enabled),
        "configured": bool(configured),
        "source": "env",
    }
    if settings_error is not None:
        return {
            **settings,
            "enabled": False,
            "source": "unavailable",
            "settings_error": str(settings_error),
        }
    if raw_setting is not None:
        if invalid_server_setting_value_keys({TELEGRAM_INTERACTIVE_SETTING_KEY: raw_setting}):
            raise ValueError("TELEGRAM_INTERACTIVE_SETTING_INVALID")
        if not isinstance(raw_setting, dict):
            raise ValueError("TELEGRAM_INTERACTIVE_SETTING_INVALID")
        settings["enabled"] = raw_setting["enabled"]
        settings["source"] = "server"
    return settings


def gex_scheduler_runtime_settings(
    *,
    env_enabled: bool,
    env_instrument_ids: Sequence[str],
    raw_setting: object = None,
    settings_error: Exception | str | None = None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "enabled": bool(env_enabled),
        "instrument_ids": list(
            require_exact_instrument_id_sequence(env_instrument_ids, allow_empty=True)
        ),
        "policy": {
            "interval_minutes": GEX_SCHEDULER_INTERVAL_MINUTES,
            "strike_count": GEX_REQUEST_STRIKE_COUNT,
        },
        "source": "env",
    }
    if settings_error is not None:
        return {
            **settings,
            "enabled": False,
            "instrument_ids": [],
            "source": "unavailable",
            "settings_error": str(settings_error),
        }
    if raw_setting is not None:
        if invalid_server_setting_value_keys({GEX_SCHEDULER_SETTING_KEY: raw_setting}):
            raise ValueError("GEX_SCHEDULER_SETTING_INVALID")
        if not isinstance(raw_setting, dict):
            raise ValueError("GEX_SCHEDULER_SETTING_INVALID")
        settings["enabled"] = raw_setting["enabled"]
        settings["instrument_ids"] = list(
            require_exact_instrument_id_sequence(raw_setting["instrument_ids"])
        )
        settings["source"] = "server"
    return settings
