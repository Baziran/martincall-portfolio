from __future__ import annotations

from collections.abc import Mapping

from aef_terminal.settings_contract import (
    global_indicator_setting_key,
    instrument_indicator_mode_setting_key,
    instrument_indicator_setting_key,
)


def indicator_bool_value(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def indicator_calc_enabled_from_settings(
    settings: Mapping[str, object] | None,
    *,
    instrument_id: str,
    indicator_id: str,
) -> bool:
    """Resolve Calc from the manifest-owned persisted scope and active mode."""

    from aef_terminal.indicators.registry import INDICATOR_REGISTRY

    values = settings if isinstance(settings, Mapping) else {}
    spec = INDICATOR_REGISTRY[indicator_id]
    mode_value = values.get(
        instrument_indicator_mode_setting_key(instrument_id),
        "regular",
    )
    mode = mode_value if mode_value in {"gex", "regular"} else "regular"
    if spec.settings_scope == "global":
        setting_key = global_indicator_setting_key(spec.calc_key)
    elif spec.settings_scope == "instrument":
        setting_key = instrument_indicator_setting_key(
            instrument_id,
            spec.calc_key,
            mode=mode,
        )
    else:
        raise ValueError(
            "INDICATOR_SETTINGS_SCOPE_INVALID "
            f"indicator_id={indicator_id} scope={spec.settings_scope!r}"
        )
    return indicator_bool_value(values.get(setting_key), spec.default_calc)
