"""Canonical durable settings vocabulary, key routing, and value validation.

For task-scoped reading, search for ``# section:`` and open only the owning block.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
    require_exact_instrument_id_sequence,
)
from aef_terminal.runtime.math_utils import is_exact_finite_number


# section: scopes-and-watchlist-presentation
CLIENT_SETTINGS_SCOPE = "client"
SERVER_SETTINGS_SCOPE = "server"
WATCHLIST_PRESENTATION_SCOPE = "watchlist_presentation"
WATCHLIST_DISPLAY_MODES = frozenset({"classic", "trend"})
CURRENT_SETTINGS_SCOPE_ORDER = (
    CLIENT_SETTINGS_SCOPE,
    SERVER_SETTINGS_SCOPE,
    WATCHLIST_PRESENTATION_SCOPE,
)
CURRENT_SETTINGS_SCOPES = frozenset(CURRENT_SETTINGS_SCOPE_ORDER)

SERVER_SLEEP_SETTING_KEY = "aef:serverSleep"
TICK_LIVE_SETTING_KEY = "aef:tickLive"
IBKR_PORT_SETTING_KEY = "aef:ibkrPort"
PAPER_TELEGRAM_SETTING_KEY = "aef:paperTelegramFeed"
PAPER_AUTO_TRADING_INSTRUMENT_SETTING_NAME = "paperAutoTrading"
HOST_SLEEP_SETTING_KEY = "aef:hostSleepLast"
GEX_SCHEDULER_SETTING_KEY = "aef:gexScheduler"
OPTION_TARGET_CAPS_SETTING_KEY = "aef:optionTargetCaps"
TELEGRAM_INTERACTIVE_SETTING_KEY = "aef:telegramInteractive"
BACKEND_RESTART_SETTING_KEY = "aef:backendRestartLast"
GEX_DIVIDEND_YIELD_SETTING_PREFIX = "aef:gexDividendYield:"
MTF_LENS_WIDTH_SETTING_KEY = "aef:mtfLensWidth"
MTF_LENS_HEIGHT_SETTING_KEY = "aef:mtfLensHeight"
MTF_LENS_ANCHOR_SETTING_KEY = "aef:mtfLensAnchor"
MTF_LENS_ANCHORS = frozenset({"bottom-left", "bottom-right", "top-left", "top-right"})


def require_watchlist_display_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in WATCHLIST_DISPLAY_MODES:
        raise ValueError("WATCHLIST_DISPLAY_MODE_INVALID")
    return value


def watchlist_presentation_payload(
    value: Any = None,
    *,
    allow_default_revision: bool = True,
) -> dict[str, Any]:
    if value is None:
        return {"display_mode": "classic", "revision": 0}
    if not isinstance(value, dict) or set(value) != {"display_mode", "revision"}:
        raise ValueError("WATCHLIST_PRESENTATION_INVALID")
    display_mode = require_watchlist_display_mode(value.get("display_mode"))
    revision = value.get("revision")
    minimum_revision = 0 if allow_default_revision else 1
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < minimum_revision:
        raise ValueError("WATCHLIST_PRESENTATION_REVISION_INVALID")
    return {"display_mode": display_mode, "revision": revision}


# section: durable-key-vocabulary
_SERVER_SCALAR_SETTING_KEYS = frozenset(
    {
        HOST_SLEEP_SETTING_KEY,
        GEX_SCHEDULER_SETTING_KEY,
        OPTION_TARGET_CAPS_SETTING_KEY,
        TELEGRAM_INTERACTIVE_SETTING_KEY,
        BACKEND_RESTART_SETTING_KEY,
    }
)

_BOOLEAN_STRING_VALUES = frozenset({"false", "true"})
_BROWSER_CLIENT_BOOLEAN_SETTING_KEYS = frozenset(
    {
        "aef:drawingHidden",
        "aef:drawingMagnet",
        "aef:economicCalendar",
        "aef:followLatest",
        "aef:paperEdgeGate",
        "aef:paperShowTradesOnChart",
        PAPER_TELEGRAM_SETTING_KEY,
        "aef:rightGapManual",
        "aef:showBidAskOnCandle",
        "aef:showCandleGrid",
        "aef:showHorizontalGrid",
        "aef:showVerticalGrid",
    }
)
_BROWSER_CLIENT_ENUM_SETTING_VALUES = {
    "aef:workspaceDockSide": frozenset({"left", "right"}),
    "aef:gexSidebarPlacement": frozenset({"chart", "dock"}),
    "aef:chartViewMode": frozenset({"advisor", "classic"}),
    "aef:cursorMode": frozenset({"informative", "normal"}),
    "aef:healthVisualizer": frozenset({"candles", "off", "snake"}),
    "aef:indicatorLabelStyle": frozenset({"box", "text"}),
    MTF_LENS_ANCHOR_SETTING_KEY: MTF_LENS_ANCHORS,
    "aef:motionPreference": frozenset({"off", "reduced", "system"}),
    "aef:paperMinRr": frozenset({"0.75", "1.25", "1.5", "2"}),
    "aef:signalDensity": frozenset({"focus", "minimal", "research"}),
    "aef:signalsRange": frozenset({"1d", "2d", "3d", "7d"}),
    "aef:tradeSetupRuntimeLayout": frozenset({"compact", "full"}),
    "aef:uiShape": frozenset({"flat", "rounded", "tv-black", "tv-black-flat", "tv-flat"}),
}
_BROWSER_CLIENT_INTEGER_SETTING_RANGES = {
    IBKR_PORT_SETTING_KEY: (1, 65_535),
    MTF_LENS_HEIGHT_SETTING_KEY: (190, 1_800),
    MTF_LENS_WIDTH_SETTING_KEY: (320, 2_400),
    "aef:paperEntryLabelFrameOffset": (-600, 1_600),
    "aef:rightGapBars": (0, 720),
    "aef:sessionPriceOpacity": (0, 8),
    "aef:sessionVolumeOpacity": (0, 10),
    "aef:gexSidebarWidth": (154, 420),
    "aef:workspaceDockWidth": (220, 640),
    "aef:sideWidth": (260, 10_000),
    "aef:volumeHeight": (80, 10_000),
}
_BROWSER_CLIENT_NUMBER_SETTING_RANGES = {
    "aef:priceShift": (-5.0, 5.0),
    "aef:priceZoom": (0.08, 12.0),
}
_BROWSER_CLIENT_SCALAR_SETTING_KEYS = frozenset(
    _BROWSER_CLIENT_BOOLEAN_SETTING_KEYS
    | _BROWSER_CLIENT_ENUM_SETTING_VALUES.keys()
    | _BROWSER_CLIENT_INTEGER_SETTING_RANGES.keys()
    | _BROWSER_CLIENT_NUMBER_SETTING_RANGES.keys()
)
_CLIENT_SCALAR_SETTING_KEYS = _BROWSER_CLIENT_SCALAR_SETTING_KEYS | {
    SERVER_SLEEP_SETTING_KEY,
    TICK_LIVE_SETTING_KEY,
}
_WORKSPACE_SLOTS = frozenset({"1", "2", "3", "4"})
_WORKSPACE_SCALAR_SETTING_KEYS = frozenset(
    {
        "gexFocusMode",
        "instrumentId",
        "sideTab",
        "theme",
        "timeframe",
        "viewPreset",
    }
)
_WORKSPACE_TUPLE_SETTING_PREFIXES = frozenset({"barsVisible", "range"})
_WORKSPACE_TIMEFRAME_RANGES = {
    "1m": frozenset({"5d", "7d", "14d", "31d"}),
    "3m": frozenset({"5d", "7d", "14d", "31d", "2mo", "3mo"}),
    "5m": frozenset({"5d", "7d", "14d", "31d", "2mo", "3mo", "6mo"}),
    "15m": frozenset({"5d", "7d", "14d", "31d", "2mo", "3mo", "6mo", "1y"}),
    "60m": frozenset({"5d", "7d", "14d", "31d", "2mo", "3mo", "6mo", "1y", "2y", "5y"}),
}
_WORKSPACE_ENUM_SETTING_VALUES = {
    "sideTab": frozenset({"alerts", "go", "indicators", "instruments"}),
    "theme": frozenset({"dark", "light"}),
    "timeframe": frozenset(_WORKSPACE_TIMEFRAME_RANGES),
    "viewPreset": frozenset({"alerts", "gex", "mobile", "trading"}),
}
_INDICATOR_MODES = frozenset({"gex", "regular"})
_GLOBAL_INDICATOR_BOOLEAN_KEYS = frozenset(
    {
        "ema20Enabled",
        "ema233Enabled",
        "ema50Enabled",
        "emaAllEnabled",
        "nyRangeEnabled",
        "nyRangeLines",
        "prevDayLevelsEnabled",
        "vsaVolumeAvg",
        "vsaVolumeCandleColors",
        "vsaVolumeLabels",
        "vsaVolumePriceMarks",
        "vsaVolumeVisible",
        "vsaVolumeVolumeColors",
        "vwapBands",
        "vwapEnabled",
    }
)
_GLOBAL_INDICATOR_COLOR_KEYS = frozenset(
    {"ema20Color", "ema50Color", "ema233Color", "vwapBandColor", "vwapColor"}
)
_GLOBAL_INDICATOR_ENUM_SETTING_VALUES = {
    "ema233AlertRearmMinutes": frozenset({"15", "30", "60"}),
    "ema233Style": frozenset({"dashed", "dotted", "solid"}),
    "ema233Width": frozenset({"1", "2", "3", "4"}),
    "nyRangeOpacity": frozenset({"8", "14", "20", "28"}),
    "nyRangeStyle": frozenset({"dashed", "dotted", "solid"}),
    "vsaVolumeRenderHours": frozenset({"2", "4", "6", "12"}),
    "vwapBandCount": frozenset({"1", "2", "3"}),
    "vwapStyle": frozenset({"dashed", "dotted", "solid"}),
    "vwapWidth": frozenset({"1", "2", "3"}),
}
_INSTRUMENT_INDICATOR_BOOLEAN_KEYS = frozenset(
    {
        "gexContextEnabled",
        "gexContextProfile",
        "gexContextZones",
        "gexDynamicsVisible",
        "optionTargetsEnabled",
        "optionTargetsFastStatus",
        "optionTargetsPulse",
    }
)
_INSTRUMENT_INDICATOR_ENUM_SETTING_VALUES = {
    "gexContextDisplayLevels": frozenset({"11", "15", "21"}),
    "gexContextHistoryView": frozenset({"last", "1d", "2d"}),
    "gexContextMode": frozenset({"live", "request"}),
    "gexContextProfileStyle": frozenset({"expiry", "ladder", "mini"}),
    "gexContextZoneStyle": frozenset({"lines", "off", "strike", "zones"}),
    "strategyMode": frozenset({"balanced", "breakout", "mean_reversion"}),
}
_INSTRUMENT_INDICATOR_ORDER_KEY = "indicatorSettingsOrder"

# section: canonical-scalar-codecs
_CANONICAL_INTEGER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_CANONICAL_NUMBER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z")
_CANONICAL_COLOR_PATTERN = re.compile(r"#[0-9a-f]{6}\Z")


def _canonical_string_array(value: str, *, length: int) -> list[str] | None:
    try:
        items = json.loads(value)
    except TypeError, json.JSONDecodeError:
        return None
    if (
        not isinstance(items, list)
        or len(items) != length
        or any(not isinstance(item, str) or not item for item in items)
        or value != json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    ):
        return None
    return items


def _is_exact_instrument_id(value: str) -> bool:
    try:
        require_exact_identity_text(value, field="instrument_id")
    except ValueError:
        return False
    return True


def _canonical_decimal_string(value: Any) -> Decimal | None:
    if (
        not isinstance(value, str)
        or value == "-0"
        or _CANONICAL_NUMBER_PATTERN.fullmatch(value) is None
    ):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _is_canonical_integer_string(
    value: Any,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> bool:
    if (
        not isinstance(value, str)
        or value == "-0"
        or _CANONICAL_INTEGER_PATTERN.fullmatch(value) is None
    ):
        return False
    number = int(value)
    return (minimum is None or number >= minimum) and (maximum is None or number <= maximum)


def _is_canonical_number_string(
    value: Any,
    *,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    step: float | int | None = None,
) -> bool:
    number = _canonical_decimal_string(value)
    if number is None:
        return False
    lower = Decimal(str(minimum)) if minimum is not None else None
    upper = Decimal(str(maximum)) if maximum is not None else None
    if (lower is not None and number < lower) or (upper is not None and number > upper):
        return False
    if step is None:
        return True
    step_decimal = Decimal(str(step))
    base = lower if lower is not None else Decimal(0)
    return step_decimal > 0 and (number - base) % step_decimal == 0


def _is_canonical_color_string(value: Any) -> bool:
    return isinstance(value, str) and _CANONICAL_COLOR_PATTERN.fullmatch(value) is not None


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _setting_key_label(value: Any) -> str:
    return value if isinstance(value, str) else repr(value)


# section: key-builders-parsers-and-membership
def require_current_settings_scope(value: Any) -> str:
    if not isinstance(value, str) or value not in CURRENT_SETTINGS_SCOPES:
        raise ValueError("STORAGE_SETTINGS_SCOPE_INVALID")
    return value


def _workspace_setting_name(value: str) -> str | None:
    parts = value.split(":", 3)
    if len(parts) != 4 or parts[:2] != ["aef", "workspace"]:
        return None
    if parts[2] not in _WORKSPACE_SLOTS:
        return None
    return parts[3]


@lru_cache(maxsize=1)
def _indicator_keys_by_scope() -> dict[str, frozenset[str]]:
    from aef_terminal.indicators.registry import INDICATOR_REGISTRY
    from aef_terminal.indicators.settings_schema import GLOBAL_DEFAULT_FIELDS

    keys: dict[str, set[str]] = {
        "global": set(
            _GLOBAL_INDICATOR_BOOLEAN_KEYS
            | _GLOBAL_INDICATOR_COLOR_KEYS
            | _GLOBAL_INDICATOR_ENUM_SETTING_VALUES.keys()
            | {field.storage_key for field in GLOBAL_DEFAULT_FIELDS}
            | {"globalDefaultPreset"}
        ),
        "instrument": set(
            _INSTRUMENT_INDICATOR_BOOLEAN_KEYS
            | _INSTRUMENT_INDICATOR_ENUM_SETTING_VALUES.keys()
            | {_INSTRUMENT_INDICATOR_ORDER_KEY}
        ),
    }
    for spec in INDICATOR_REGISTRY.values():
        scope_keys = keys[spec.settings_scope]
        scope_keys.update(key for key in (spec.calc_key, spec.visible_key) if key)
        for control in spec.controls:
            if control.storage_key:
                keys[control.scope].add(control.storage_key)
    return {scope: frozenset(values) for scope, values in keys.items()}


def _instrument_scalar_setting_ref(value: str) -> tuple[str, str] | None:
    if not value.startswith("aef:instrument:"):
        return None
    instrument_setting = value.removeprefix("aef:instrument:")
    suffix = next(
        (
            candidate
            for candidate in (
                "indicatorMode",
                PAPER_AUTO_TRADING_INSTRUMENT_SETTING_NAME,
                "paperRiskPrefs",
            )
            if instrument_setting.endswith(f":{candidate}")
        ),
        "",
    )
    if not suffix:
        return None
    encoded_scope = instrument_setting.removesuffix(f":{suffix}")
    route_scope = _canonical_string_array(encoded_scope, length=1)
    if route_scope is None or not _is_exact_instrument_id(route_scope[0]):
        return None
    return suffix, route_scope[0]


def instrument_paper_auto_trading_setting_key(instrument_id: str) -> str:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    encoded_scope = json.dumps([identity], ensure_ascii=False, separators=(",", ":"))
    return f"aef:instrument:{encoded_scope}:{PAPER_AUTO_TRADING_INSTRUMENT_SETTING_NAME}"


def instrument_indicator_mode_setting_key(instrument_id: str) -> str:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    encoded_scope = json.dumps([identity], ensure_ascii=False, separators=(",", ":"))
    return f"aef:instrument:{encoded_scope}:indicatorMode"


def instrument_indicator_setting_key(
    instrument_id: str,
    setting_key: str,
    *,
    mode: str = "regular",
) -> str:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    exact_setting_key = str(setting_key).strip()
    exact_mode = str(mode).strip().lower()
    if (
        not exact_setting_key
        or ":" in exact_setting_key
        or exact_setting_key not in _indicator_keys_by_scope()["instrument"]
    ):
        raise ValueError("INSTRUMENT_INDICATOR_SETTING_KEY_INVALID")
    if exact_mode not in _INDICATOR_MODES:
        raise ValueError("INDICATOR_SETTING_MODE_INVALID")
    encoded_scope = json.dumps(
        [identity, exact_mode],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"aef:instrument:{encoded_scope}:indicator:{exact_setting_key}"


def global_indicator_setting_key(setting_key: str) -> str:
    exact_setting_key = str(setting_key).strip()
    if (
        not exact_setting_key
        or ":" in exact_setting_key
        or exact_setting_key not in _indicator_keys_by_scope()["global"]
    ):
        raise ValueError("GLOBAL_INDICATOR_SETTING_KEY_INVALID")
    return f"aef:indicator:global:{exact_setting_key}"


def instrument_ids_for_indicator_setting(
    values: Mapping[Any, Any],
    setting_key: str,
    *,
    expected_value: str = "true",
) -> tuple[str, ...]:
    """Return exact instruments carrying the value in their active UI mode."""

    exact_setting_key = str(setting_key).strip()
    if (
        not exact_setting_key
        or ":" in exact_setting_key
        or exact_setting_key not in _indicator_keys_by_scope()["instrument"]
    ):
        raise ValueError("INSTRUMENT_INDICATOR_SETTING_KEY_INVALID")
    if not isinstance(values, Mapping):
        raise TypeError("INSTRUMENT_INDICATOR_SETTINGS_PAYLOAD_INVALID")
    return tuple(
        sorted(
            {
                indicator_ref[2]
                for raw_key, raw_value in values.items()
                if isinstance(raw_key, str)
                and raw_value == expected_value
                and (indicator_ref := _indicator_setting_ref(raw_key)) is not None
                and indicator_ref[0] == "instrument"
                and indicator_ref[1] == exact_setting_key
                and indicator_ref[2]
                and indicator_ref[3]
                == values.get(
                    instrument_indicator_mode_setting_key(indicator_ref[2]),
                    "regular",
                )
            }
        )
    )


def _indicator_setting_ref(value: str) -> tuple[str, str, str, str] | None:
    if value.startswith("aef:instrument:"):
        instrument_setting = value.removeprefix("aef:instrument:")
        encoded_scope, separator, indicator_key = instrument_setting.rpartition(":indicator:")
        if (
            not separator
            or not indicator_key
            or indicator_key != indicator_key.strip()
            or ":" in indicator_key
        ):
            return None
        route_scope = _canonical_string_array(encoded_scope, length=2)
        if (
            route_scope is None
            or route_scope[1] not in _INDICATOR_MODES
            or not _is_exact_instrument_id(route_scope[0])
        ):
            return None
        return "instrument", indicator_key, route_scope[0], route_scope[1]
    if value.startswith("aef:indicator:"):
        parts = value.split(":")
        if (
            len(parts) == 4
            and parts[:3] == ["aef", "indicator", "global"]
            and bool(parts[3])
            and parts[3] == parts[3].strip()
        ):
            return "global", parts[3], "", ""
        return None
    return None


def is_current_client_setting(key: Any) -> bool:
    """Return whether a durable client key belongs to the current storage contract."""

    if not isinstance(key, str):
        return False
    value = key
    if value in _CLIENT_SCALAR_SETTING_KEYS:
        return True
    if value.startswith("aef:workspace:"):
        workspace_setting = _workspace_setting_name(value)
        if workspace_setting is None:
            return False
        if workspace_setting in _WORKSPACE_SCALAR_SETTING_KEYS:
            return True
        prefix, separator, encoded_scope = workspace_setting.partition(":")
        if not separator or prefix not in _WORKSPACE_TUPLE_SETTING_PREFIXES:
            return False
        scope = _canonical_string_array(encoded_scope, length=2)
        return (
            scope is not None
            and _is_exact_instrument_id(scope[0])
            and scope[1] in _WORKSPACE_TIMEFRAME_RANGES
        )
    if _instrument_scalar_setting_ref(value) is not None:
        return True
    indicator_ref = _indicator_setting_ref(value)
    return (
        indicator_ref is not None
        and indicator_ref[1] in _indicator_keys_by_scope()[indicator_ref[0]]
    )


def is_browser_writable_client_setting(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    value = key
    if value in _CLIENT_SCALAR_SETTING_KEYS:
        return value in _BROWSER_CLIENT_SCALAR_SETTING_KEYS
    return is_current_client_setting(value)


def browser_client_settings_contract_manifest() -> dict[str, Any]:
    """Return the generated browser key grammar from the canonical contract."""

    indicator_keys = _indicator_keys_by_scope()
    return {
        "version": 2,
        "current_scalar_keys": sorted(_CLIENT_SCALAR_SETTING_KEYS),
        "browser_scalar_keys": sorted(_BROWSER_CLIENT_SCALAR_SETTING_KEYS),
        "workspace_slots": sorted(_WORKSPACE_SLOTS),
        "workspace_scalar_keys": sorted(_WORKSPACE_SCALAR_SETTING_KEYS),
        "workspace_tuple_prefixes": sorted(_WORKSPACE_TUPLE_SETTING_PREFIXES),
        "workspace_timeframes": sorted(_WORKSPACE_TIMEFRAME_RANGES),
        "instrument_scalar_names": sorted(
            {
                "indicatorMode",
                PAPER_AUTO_TRADING_INSTRUMENT_SETTING_NAME,
                "paperRiskPrefs",
            }
        ),
        "indicator_modes": sorted(_INDICATOR_MODES),
        "indicator_keys": {
            scope: sorted(indicator_keys[scope]) for scope in ("global", "instrument")
        },
    }


def invalid_persisted_client_setting_keys(keys: Iterable[Any]) -> list[str]:
    return sorted(_setting_key_label(key) for key in keys if not is_current_client_setting(key))


def invalid_browser_client_setting_keys(keys: Iterable[Any]) -> list[str]:
    return sorted(
        _setting_key_label(key) for key in keys if not is_browser_writable_client_setting(key)
    )


def _server_setting_route(key: Any) -> tuple[str, str] | None:
    if not isinstance(key, str) or not key.startswith(GEX_DIVIDEND_YIELD_SETTING_PREFIX):
        return None
    route = _canonical_string_array(
        key.removeprefix(GEX_DIVIDEND_YIELD_SETTING_PREFIX),
        length=2,
    )
    if route is None or not _is_exact_instrument_id(route[0]) or not _is_non_empty_string(route[1]):
        return None
    return route[0], route[1]


def is_current_server_setting(key: Any) -> bool:
    return isinstance(key, str) and (
        key in _SERVER_SCALAR_SETTING_KEYS or _server_setting_route(key) is not None
    )


def invalid_persisted_server_setting_keys(keys: Iterable[Any]) -> list[str]:
    return sorted(_setting_key_label(key) for key in keys if not is_current_server_setting(key))


def _instrument_ids_exist(
    instrument_ids: Iterable[str],
    instrument_exists: Callable[[str], bool] | None,
) -> bool:
    return instrument_exists is None or all(
        instrument_exists(instrument_id) for instrument_id in instrument_ids
    )


# section: typed-value-contracts
def require_option_target_caps(value: Any) -> dict[str, float]:
    """Validate the exact persisted per-instrument option premium-cap object."""

    if not isinstance(value, dict):
        raise TypeError("option premium caps must be a typed object")
    caps: dict[str, float] = {}
    for instrument_id, raw_cap in value.items():
        if not isinstance(instrument_id, str) or not instrument_id:
            raise ValueError("option premium cap keys must be exact instrument_id strings")
        require_exact_identity_text(instrument_id, field="instrument_id")
        if not is_exact_finite_number(raw_cap):
            raise ValueError(f"option premium cap must be finite: instrument_id={instrument_id}")
        cap = float(raw_cap)
        if not 0.01 <= cap <= 999.0:
            raise ValueError(
                f"option premium cap must be in the 0.01..999 range: instrument_id={instrument_id}"
            )
        caps[instrument_id] = raw_cap
    return caps


@lru_cache(maxsize=1)
def _indicator_value_contract() -> dict[str, Any]:
    from aef_terminal.indicators.registry import INDICATOR_REGISTRY
    from aef_terminal.indicators.settings_schema import (
        GLOBAL_DEFAULT_FIELDS,
        GLOBAL_DEFAULT_PRESETS,
    )

    boolean_keys: dict[str, set[str]] = {"global": set(), "instrument": set()}
    controls: dict[str, dict[str, Any]] = {"global": {}, "instrument": {}}
    for spec in INDICATOR_REGISTRY.values():
        boolean_keys[spec.settings_scope].update(
            key for key in (spec.calc_key, spec.visible_key) if key
        )
        for control in spec.controls:
            if not control.storage_key:
                continue
            current = controls[control.scope].get(control.storage_key)
            if current is not None and current != control:
                raise RuntimeError(
                    "INDICATOR_SETTING_VALUE_CONTRACT_CONFLICT "
                    f"scope={control.scope!r} key={control.storage_key!r}"
                )
            controls[control.scope][control.storage_key] = control
    return {
        "boolean_keys": {scope: frozenset(keys) for scope, keys in boolean_keys.items()},
        "controls": controls,
        "global_defaults": {field.storage_key: field for field in GLOBAL_DEFAULT_FIELDS},
        "global_presets": frozenset(GLOBAL_DEFAULT_PRESETS),
        "manager_ids": frozenset(
            indicator_id
            for indicator_id, spec in INDICATOR_REGISTRY.items()
            if spec.show_in_manager and spec.process_control_id
        ),
    }


def _is_indicator_order_value(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    indicator_ids = value.split(",")
    expected_ids = _indicator_value_contract()["manager_ids"]
    actual_ids = frozenset(indicator_ids)
    return (
        all(indicator_id and indicator_id == indicator_id.strip() for indicator_id in indicator_ids)
        and len(indicator_ids) == len(set(indicator_ids))
        and actual_ids.issubset(expected_ids)
    )


def _is_indicator_setting_value(scope: str, setting_key: str, value: Any) -> bool:
    contract = _indicator_value_contract()
    if setting_key in contract["boolean_keys"][scope]:
        return isinstance(value, str) and value in _BOOLEAN_STRING_VALUES
    control = contract["controls"][scope].get(setting_key)
    if control is not None:
        if control.control_type == "toggle":
            return isinstance(value, str) and value in _BOOLEAN_STRING_VALUES
        if control.control_type == "number":
            return _is_canonical_number_string(
                value,
                minimum=control.minimum,
                maximum=control.maximum,
                step=control.step,
            )
        if control.control_type == "select":
            return isinstance(value, str) and value in control.options
        if control.control_type == "color":
            return _is_canonical_color_string(value)
        return False
    if scope == "global":
        if setting_key in _GLOBAL_INDICATOR_BOOLEAN_KEYS:
            return isinstance(value, str) and value in _BOOLEAN_STRING_VALUES
        if setting_key in _GLOBAL_INDICATOR_COLOR_KEYS:
            return _is_canonical_color_string(value)
        if setting_key in _GLOBAL_INDICATOR_ENUM_SETTING_VALUES:
            return (
                isinstance(value, str)
                and value in _GLOBAL_INDICATOR_ENUM_SETTING_VALUES[setting_key]
            )
        if setting_key == "globalDefaultPreset":
            return isinstance(value, str) and value in contract["global_presets"]
        field = contract["global_defaults"].get(setting_key)
        return field is not None and _is_canonical_number_string(
            value,
            minimum=field.minimum,
            maximum=field.maximum,
            step=field.step,
        )
    if scope == "instrument":
        if setting_key in _INSTRUMENT_INDICATOR_BOOLEAN_KEYS:
            return isinstance(value, str) and value in _BOOLEAN_STRING_VALUES
        if setting_key in _INSTRUMENT_INDICATOR_ENUM_SETTING_VALUES:
            return (
                isinstance(value, str)
                and value in _INSTRUMENT_INDICATOR_ENUM_SETTING_VALUES[setting_key]
            )
        if setting_key == _INSTRUMENT_INDICATOR_ORDER_KEY:
            return _is_indicator_order_value(value)
    return False


def _is_paper_risk_prefs_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        prefs = json.loads(value)
        canonical = json.dumps(
            prefs,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except TypeError, ValueError:
        return False
    if canonical != value or not isinstance(prefs, dict):
        return False
    if list(prefs) != [
        "orderType",
        "qty",
        "useStopLoss",
        "stopPoints",
        "useTarget",
        "targetPoints",
    ]:
        return False
    return (
        prefs["orderType"] in {"limit", "market", "stop"}
        and is_exact_finite_number(prefs["qty"])
        and 0.1 <= float(prefs["qty"]) <= 100.0
        and isinstance(prefs["useStopLoss"], bool)
        and is_exact_finite_number(prefs["stopPoints"])
        and 0.01 <= float(prefs["stopPoints"]) <= 500.0
        and isinstance(prefs["useTarget"], bool)
        and is_exact_finite_number(prefs["targetPoints"])
        and 0.01 <= float(prefs["targetPoints"]) <= 1_000.0
    )


def _is_browser_scalar_setting_value(key: str, value: Any) -> bool:
    if key in _BROWSER_CLIENT_BOOLEAN_SETTING_KEYS:
        return isinstance(value, str) and value in _BOOLEAN_STRING_VALUES
    if key in _BROWSER_CLIENT_ENUM_SETTING_VALUES:
        return isinstance(value, str) and value in _BROWSER_CLIENT_ENUM_SETTING_VALUES[key]
    if key in _BROWSER_CLIENT_INTEGER_SETTING_RANGES:
        minimum, maximum = _BROWSER_CLIENT_INTEGER_SETTING_RANGES[key]
        return _is_canonical_integer_string(value, minimum=minimum, maximum=maximum)
    if key in _BROWSER_CLIENT_NUMBER_SETTING_RANGES:
        minimum, maximum = _BROWSER_CLIENT_NUMBER_SETTING_RANGES[key]
        return _is_canonical_number_string(value, minimum=minimum, maximum=maximum)
    return False


def _is_workspace_setting_value(
    setting_name: str,
    value: Any,
    *,
    instrument_exists: Callable[[str], bool] | None,
) -> bool:
    if setting_name == "gexFocusMode":
        return isinstance(value, str) and value in _BOOLEAN_STRING_VALUES
    if setting_name == "instrumentId":
        return (
            isinstance(value, str)
            and (not value or _is_exact_instrument_id(value))
            and (not value or _instrument_ids_exist((value,), instrument_exists))
        )
    if setting_name in _WORKSPACE_ENUM_SETTING_VALUES:
        return isinstance(value, str) and value in _WORKSPACE_ENUM_SETTING_VALUES[setting_name]
    prefix, separator, encoded_scope = setting_name.partition(":")
    if not separator or prefix not in _WORKSPACE_TUPLE_SETTING_PREFIXES:
        return False
    route_scope = _canonical_string_array(encoded_scope, length=2)
    if route_scope is None or route_scope[1] not in _WORKSPACE_TIMEFRAME_RANGES:
        return False
    if not _instrument_ids_exist((route_scope[0],), instrument_exists):
        return False
    if prefix == "barsVisible":
        return _is_canonical_integer_string(value, minimum=24, maximum=10_000)
    return isinstance(value, str) and value in _WORKSPACE_TIMEFRAME_RANGES[route_scope[1]]


def invalid_client_setting_value_keys(
    values: Mapping[Any, Any],
    *,
    instrument_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    invalid: list[str] = []
    for key, value in values.items():
        if key == SERVER_SLEEP_SETTING_KEY:
            if (
                not isinstance(value, dict)
                or set(value) != {"sleeping", "reason", "changed_at", "duration_seconds"}
                or not isinstance(value.get("sleeping"), bool)
                or not isinstance(value.get("reason"), str)
                or not _is_non_empty_string(value.get("changed_at"))
                or not is_exact_finite_number(value.get("duration_seconds"))
                or float(value["duration_seconds"]) < 0.0
            ):
                invalid.append(_setting_key_label(key))
        elif key == TICK_LIVE_SETTING_KEY:
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "enabled",
                    "instrument_id",
                    "route_fingerprint",
                    "source",
                    "updated_at",
                }
                or not isinstance(value.get("enabled"), bool)
                or not isinstance(value.get("instrument_id"), str)
                or not isinstance(value.get("route_fingerprint"), str)
                or not _is_non_empty_string(value.get("source"))
                or not _is_non_empty_string(value.get("updated_at"))
            ):
                invalid.append(_setting_key_label(key))
                continue
            instrument_id = value["instrument_id"]
            route_fingerprint = value["route_fingerprint"]
            if (
                (value["enabled"] and (not instrument_id or not route_fingerprint))
                or (instrument_id and not _is_exact_instrument_id(instrument_id))
                or (
                    instrument_id and not _instrument_ids_exist((instrument_id,), instrument_exists)
                )
            ):
                invalid.append(_setting_key_label(key))
        elif isinstance(key, str) and key in _BROWSER_CLIENT_SCALAR_SETTING_KEYS:
            if not _is_browser_scalar_setting_value(key, value):
                invalid.append(key)
        elif isinstance(key, str) and (workspace_setting := _workspace_setting_name(key)):
            if not _is_workspace_setting_value(
                workspace_setting,
                value,
                instrument_exists=instrument_exists,
            ):
                invalid.append(key)
        elif isinstance(key, str) and (instrument_scalar := _instrument_scalar_setting_ref(key)):
            setting_name, instrument_id = instrument_scalar
            if (
                not _instrument_ids_exist((instrument_id,), instrument_exists)
                or (
                    setting_name == "indicatorMode"
                    and (not isinstance(value, str) or value not in _INDICATOR_MODES)
                )
                or (
                    setting_name == PAPER_AUTO_TRADING_INSTRUMENT_SETTING_NAME
                    and (not isinstance(value, str) or value not in _BOOLEAN_STRING_VALUES)
                )
                or (setting_name == "paperRiskPrefs" and not _is_paper_risk_prefs_value(value))
            ):
                invalid.append(key)
        elif isinstance(key, str) and (indicator_ref := _indicator_setting_ref(key)):
            scope, indicator_key, instrument_id, _mode = indicator_ref
            if (
                indicator_key not in _indicator_keys_by_scope()[scope]
                or (
                    instrument_id and not _instrument_ids_exist((instrument_id,), instrument_exists)
                )
                or not _is_indicator_setting_value(scope, indicator_key, value)
            ):
                invalid.append(key)
        else:
            invalid.append(_setting_key_label(key))
    return sorted(set(invalid))


def invalid_browser_client_setting_value_keys(
    values: Mapping[Any, Any],
    *,
    instrument_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    return invalid_client_setting_value_keys(
        values,
        instrument_exists=instrument_exists,
    )


def invalid_server_setting_value_keys(
    values: Mapping[Any, Any],
    *,
    instrument_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    invalid: list[str] = []
    for key, value in values.items():
        if key == GEX_SCHEDULER_SETTING_KEY:
            try:
                instrument_ids = require_exact_instrument_id_sequence(
                    value.get("instrument_ids") if isinstance(value, dict) else None
                )
            except TypeError, ValueError:
                instrument_ids = ()
            if (
                not isinstance(value, dict)
                or set(value) != {"enabled", "instrument_ids"}
                or not isinstance(value.get("enabled"), bool)
                or not isinstance(value.get("instrument_ids"), list)
                or not instrument_ids
                or not _instrument_ids_exist(instrument_ids, instrument_exists)
            ):
                invalid.append(_setting_key_label(key))
        elif key == OPTION_TARGET_CAPS_SETTING_KEY:
            try:
                caps = require_option_target_caps(value)
            except TypeError, ValueError:
                invalid.append(_setting_key_label(key))
                continue
            if not _instrument_ids_exist(caps, instrument_exists):
                invalid.append(_setting_key_label(key))
        elif key == TELEGRAM_INTERACTIVE_SETTING_KEY:
            if (
                not isinstance(value, dict)
                or set(value) != {"enabled"}
                or not isinstance(value.get("enabled"), bool)
            ):
                invalid.append(_setting_key_label(key))
        elif key == HOST_SLEEP_SETTING_KEY:
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "detected_at",
                    "previous_check_at",
                    "wall_gap_seconds",
                    "expected_check_seconds",
                    "gap_over_expected_seconds",
                    "reason",
                    "server_sleeping",
                }
                or not _is_non_empty_string(value.get("detected_at"))
                or not _is_non_empty_string(value.get("previous_check_at"))
                or not is_exact_finite_number(value.get("wall_gap_seconds"))
                or float(value.get("wall_gap_seconds", -1.0)) < 0.0
                or not is_exact_finite_number(value.get("expected_check_seconds"))
                or float(value.get("expected_check_seconds", 0.0)) <= 0.0
                or not is_exact_finite_number(value.get("gap_over_expected_seconds"))
                or float(value.get("gap_over_expected_seconds", -1.0)) < 0.0
                or value.get("reason") != "host_sleep_or_event_loop_pause"
                or not isinstance(value.get("server_sleeping"), bool)
            ):
                invalid.append(_setting_key_label(key))
        elif key == BACKEND_RESTART_SETTING_KEY:
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "requested_at",
                    "reason",
                    "source",
                    "caller",
                    "app",
                    "connections",
                    "tick_live",
                    "ibkr",
                    "gex",
                }
                or not _is_non_empty_string(value.get("requested_at"))
                or not _is_non_empty_string(value.get("reason"))
                or not _is_non_empty_string(value.get("source"))
                or any(
                    not isinstance(value.get(section), dict)
                    for section in (
                        "caller",
                        "app",
                        "connections",
                        "tick_live",
                        "ibkr",
                        "gex",
                    )
                )
            ):
                invalid.append(_setting_key_label(key))
        else:
            route = _server_setting_route(key)
            if route is None:
                invalid.append(_setting_key_label(key))
                continue
            instrument_id, route_fingerprint = route
            if (
                not isinstance(value, dict)
                or set(value)
                != {
                    "provider_symbol",
                    "instrument_id",
                    "route_fingerprint",
                    "value",
                    "source",
                    "updated_at",
                    "sample_count",
                }
                or not _is_non_empty_string(value.get("provider_symbol"))
                or value.get("instrument_id") != instrument_id
                or value.get("route_fingerprint") != route_fingerprint
                or not is_exact_finite_number(value.get("value"))
                or not 0.0001 <= float(value.get("value", 0.0)) <= 0.08
                or value.get("source") != "broker_pv_dividend"
                or not _is_non_empty_string(value.get("updated_at"))
                or not isinstance(value.get("sample_count"), int)
                or isinstance(value.get("sample_count"), bool)
                or int(value.get("sample_count", 0)) <= 0
                or not _instrument_ids_exist((instrument_id,), instrument_exists)
            ):
                invalid.append(_setting_key_label(key))
    return sorted(set(invalid))


# section: instrument-reference-and-retirement
def _client_setting_instrument_ids(key: Any, value: Any) -> tuple[str, ...]:
    if not isinstance(key, str):
        return ()
    workspace_setting = _workspace_setting_name(key)
    if workspace_setting == "instrumentId":
        return (value,) if isinstance(value, str) and value else ()
    if workspace_setting is not None:
        prefix, separator, encoded_scope = workspace_setting.partition(":")
        if separator and prefix in _WORKSPACE_TUPLE_SETTING_PREFIXES:
            scope = _canonical_string_array(encoded_scope, length=2)
            return (scope[0],) if scope is not None else ()
    instrument_scalar = _instrument_scalar_setting_ref(key)
    if instrument_scalar is not None:
        return (instrument_scalar[1],)
    indicator_ref = _indicator_setting_ref(key)
    if indicator_ref is not None and indicator_ref[2]:
        return (indicator_ref[2],)
    if key == TICK_LIVE_SETTING_KEY and isinstance(value, dict):
        instrument_id = value.get("instrument_id")
        return (instrument_id,) if isinstance(instrument_id, str) and instrument_id else ()
    return ()


def client_setting_keys_for_instrument(
    values: Mapping[Any, Any],
    instrument_id: str,
) -> list[str]:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    return sorted(
        key
        for key, value in values.items()
        if isinstance(key, str) and identity in _client_setting_instrument_ids(key, value)
    )


def invalid_client_setting_instrument_keys(
    values: Mapping[Any, Any],
    *,
    instrument_exists: Callable[[str], bool] | None,
) -> list[str]:
    if instrument_exists is None:
        return []
    return sorted(
        _setting_key_label(key)
        for key, value in values.items()
        if any(
            not instrument_exists(instrument_id)
            for instrument_id in _client_setting_instrument_ids(key, value)
        )
    )


def server_setting_mutations_without_instrument(
    values: Mapping[Any, Any],
    instrument_id: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Build exact setting updates/deletes when one watchlist identity is retired."""

    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    updates: dict[str, dict[str, Any]] = {}
    deletes: list[str] = []
    for key, value in values.items():
        if key == GEX_SCHEDULER_SETTING_KEY and isinstance(value, dict):
            raw_ids = value.get("instrument_ids")
            if isinstance(raw_ids, list) and identity in raw_ids:
                remaining = [item for item in raw_ids if item != identity]
                if remaining:
                    updates[key] = {**value, "instrument_ids": remaining}
                else:
                    deletes.append(key)
        elif key == OPTION_TARGET_CAPS_SETTING_KEY and isinstance(value, dict):
            if identity in value:
                remaining_caps = {
                    key_identity: cap
                    for key_identity, cap in value.items()
                    if key_identity != identity
                }
                if remaining_caps:
                    updates[key] = remaining_caps
                else:
                    deletes.append(key)
        else:
            route = _server_setting_route(key)
            if route is not None and route[0] == identity:
                deletes.append(key)
    return updates, sorted(set(deletes))


def invalid_workspace_instrument_selection_keys(
    values: Mapping[Any, Any],
    *,
    instrument_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Validate typed workspace selections without resolving names or rewriting IDs."""

    invalid: list[str] = []
    for key, instrument_id in values.items():
        if not isinstance(key, str):
            continue
        setting_key = key
        if _workspace_setting_name(setting_key) != "instrumentId":
            continue
        if not isinstance(instrument_id, str):
            invalid.append(setting_key)
            continue
        if instrument_id and instrument_exists is not None and not instrument_exists(instrument_id):
            invalid.append(setting_key)
    return sorted(invalid)
