from __future__ import annotations

import math
from typing import Any

from aef_terminal.alerts.delivery_contract import (
    TELEGRAM_CANCELLED_DELIVERY_STATE,
    TELEGRAM_DELIVERY_RUNTIME_FIELDS,
    validate_telegram_delivery_runtime,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.engine.common import parse_aware_utc_ts


PRICE_ALERT_RUNTIME_TRANSITION_FIELDS = (
    "armed",
    "fired",
    "cooldownUntil",
    "rearmedAt",
    "lastFiredAt",
    "lastFiredLevel",
    "lastTelegramStatus",
    "lastTelegramOk",
    "telegramDeliveryStatus",
    "telegramNextRetryAt",
    "telegramRetryCount",
    "touchDirection",
    "lastEventTs",
    "lastFiredEventTs",
)
PRICE_ALERT_SERVER_DEFINITION_FIELDS = ("level_source",)
PRICE_ALERT_LEVEL_SOURCE_TYPES = frozenset(
    {"fixed_price", "ema233_touch", "vsa_fuel", "gex_snapshot"}
)
PRICE_ALERT_SERVER_RUNTIME_FIELDS = (
    *PRICE_ALERT_RUNTIME_TRANSITION_FIELDS,
    "lastSeenPrice",
    "lastSeenLevel",
    "lastTolerance",
    "lastTelegramDetail",
    "telegramPendingPayload",
    "telegramPendingSince",
    "telegramClaimedAt",
    "telegramLastAttemptAt",
    "telegramLastError",
    "wasTouching",
)
PRICE_ALERT_DIRECTIONS = frozenset({"cross", "above", "below"})
PRICE_ALERT_KINDS = frozenset({"price", "ema233_touch", "vsa_fuel"})
PRICE_ALERT_TOUCH_DIRECTIONS = frozenset({"from_above", "from_below", "touch"})
PRICE_ALERT_FORBIDDEN_FIELDS = frozenset(
    {
        "level",
        "drawingAnchor",
        "lastAnchorBarSlot",
        "opacity",
        "deletable",
        "delete_icon",
        "label_handle",
        "interactive",
        "dragging",
        "deleted",
        "deletedAt",
    }
)

_PRICE_ALERT_BOOLEAN_RUNTIME_FIELDS = frozenset({"armed", "fired", "wasTouching"})
_PRICE_ALERT_MILLISECOND_RUNTIME_FIELDS = frozenset({"cooldownUntil", "rearmedAt", "lastFiredAt"})
_PRICE_ALERT_ISO_RUNTIME_FIELDS = frozenset({"lastEventTs", "lastFiredEventTs"})
_PRICE_ALERT_NUMBER_RUNTIME_FIELDS = frozenset(
    {"lastFiredLevel", "lastSeenPrice", "lastSeenLevel", "lastTolerance"}
)
_PRICE_ALERT_SPECIAL_RUNTIME_FIELDS = frozenset({"touchDirection"})
_PRICE_ALERT_VALIDATED_RUNTIME_FIELDS = (
    _PRICE_ALERT_BOOLEAN_RUNTIME_FIELDS
    | _PRICE_ALERT_MILLISECOND_RUNTIME_FIELDS
    | _PRICE_ALERT_ISO_RUNTIME_FIELDS
    | _PRICE_ALERT_NUMBER_RUNTIME_FIELDS
    | _PRICE_ALERT_SPECIAL_RUNTIME_FIELDS
    | frozenset(TELEGRAM_DELIVERY_RUNTIME_FIELDS)
)
if _PRICE_ALERT_VALIDATED_RUNTIME_FIELDS != frozenset(PRICE_ALERT_SERVER_RUNTIME_FIELDS):
    raise RuntimeError("PRICE_ALERT_SERVER_RUNTIME_FIELDS validator coverage is incomplete")


def _validate_price_alert_level_source(alert_id: str, kind: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source")
    source_type = value.get("type")
    if not isinstance(source_type, str) or source_type not in PRICE_ALERT_LEVEL_SOURCE_TYPES:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.type")
    dynamic = value.get("dynamic")
    if not isinstance(dynamic, bool):
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.dynamic")
    expected_types = {
        "price": frozenset({"fixed_price", "gex_snapshot"}),
        "ema233_touch": frozenset({"ema233_touch"}),
        "vsa_fuel": frozenset({"vsa_fuel"}),
    }[kind]
    expected_dynamic = source_type in {"ema233_touch", "vsa_fuel"}
    if source_type not in expected_types or dynamic is not expected_dynamic:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source contract")

    if source_type != "gex_snapshot":
        if set(value) != {"type", "dynamic"}:
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source fields")
        return

    required_fields = {
        "type",
        "dynamic",
        "level_ref",
        "level_kind",
        "label",
        "snapshot_captured_at",
        "snapshot_version",
        "snapshot_source",
        "snapshot_capture_mode",
        "snapshot_market_data_entitlement",
        "snapshot_decision_authoritative",
        "snapshot_age_minutes_at_creation",
    }
    if set(value) != required_fields:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source fields")
    for field in (
        "level_ref",
        "level_kind",
        "label",
        "snapshot_version",
        "snapshot_source",
    ):
        require_exact_identity_text(
            value.get(field),
            field=f"PRICE_ALERT_LEVEL_SOURCE_{field.upper()}",
        )
    captured_at = value.get("snapshot_captured_at")
    if not isinstance(captured_at, str) or parse_aware_utc_ts(captured_at) is None:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.snapshot_captured_at")
    if value.get("snapshot_capture_mode") != "request":
        raise ValueError(
            f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.snapshot_capture_mode"
        )
    if value.get("snapshot_market_data_entitlement") != "live":
        raise ValueError(
            f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.snapshot_market_data_entitlement"
        )
    if value.get("snapshot_decision_authoritative") is not True:
        raise ValueError(
            f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.snapshot_decision_authoritative"
        )
    age_minutes = value.get("snapshot_age_minutes_at_creation")
    if (
        isinstance(age_minutes, bool)
        or not isinstance(age_minutes, (int, float))
        or not math.isfinite(age_minutes)
        or age_minutes < 0
    ):
        raise ValueError(
            f"PRICE_ALERT_FIELD_INVALID: {alert_id} level_source.snapshot_age_minutes_at_creation"
        )


def validate_price_alert_definition(alert: dict[str, Any]) -> None:
    """Reject a malformed canonical price-alert definition."""

    if not isinstance(alert, dict):
        raise ValueError("PRICE_ALERT_ROW_INVALID: alert must be an object")
    alert_id = require_exact_identity_text(alert.get("id"), field="PRICE_ALERT_ID")
    for field in (
        "instrument_id",
        "route_fingerprint",
        "provider",
        "provider_contract_id",
        "symbol",
        "timeframe",
    ):
        require_exact_identity_text(
            alert.get(field),
            field=f"PRICE_ALERT_{field.upper()}",
        )
    forbidden = sorted(PRICE_ALERT_FORBIDDEN_FIELDS.intersection(alert))
    if forbidden:
        raise ValueError(f"PRICE_ALERT_FIELD_FORBIDDEN: {forbidden[0]}")
    kind = alert.get("kind")
    if kind not in PRICE_ALERT_KINDS:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} kind")
    if not isinstance(alert.get("label"), str):
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} label")
    direction = alert.get("direction")
    if not isinstance(direction, str) or direction not in PRICE_ALERT_DIRECTIONS:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} direction")
    price = alert.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} price")
    if not isinstance(alert.get("enabled"), bool):
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} enabled")
    for timestamp_field in ("rearmedAt", "createdAt"):
        timestamp = alert.get(timestamp_field)
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} {timestamp_field}")
    for numeric_field in ("toleranceAtr", "tolerancePoints"):
        number = alert.get(numeric_field)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number < 0
        ):
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} {numeric_field}")
    rearm_minutes = alert.get("rearmMinutes")
    if (
        isinstance(rearm_minutes, bool)
        or not isinstance(rearm_minutes, int)
        or rearm_minutes not in {15, 30, 60}
    ):
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} rearmMinutes")
    _validate_price_alert_level_source(alert_id, kind, alert.get("level_source"))


def validate_price_alert_runtime(alert: dict[str, Any]) -> None:
    """Reject a malformed canonical persisted price-alert runtime."""

    if not isinstance(alert, dict):
        raise ValueError("PRICE_ALERT_ROW_INVALID: alert must be an object")
    alert_id = require_exact_identity_text(alert.get("id"), field="PRICE_ALERT_ID")
    required_fields = {
        "armed",
        "fired",
        "cooldownUntil",
        "rearmedAt",
        *TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    missing_fields = sorted(required_fields - alert.keys())
    if missing_fields:
        raise ValueError(
            f"PRICE_ALERT_FIELD_INVALID: {alert_id} incomplete runtime "
            f"({', '.join(missing_fields)})"
        )
    for field in _PRICE_ALERT_BOOLEAN_RUNTIME_FIELDS:
        if field in alert and not isinstance(alert[field], bool):
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} {field}")
    for field in _PRICE_ALERT_MILLISECOND_RUNTIME_FIELDS:
        if field not in alert:
            continue
        value = alert[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} {field}")
    for field in _PRICE_ALERT_ISO_RUNTIME_FIELDS:
        if field not in alert:
            continue
        value = alert[field]
        if not isinstance(value, str) or parse_aware_utc_ts(value) is None:
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} {field}")
    for field in _PRICE_ALERT_NUMBER_RUNTIME_FIELDS:
        if field not in alert:
            continue
        value = alert[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (field == "lastTolerance" and value < 0)
        ):
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} {field}")
    if "touchDirection" in alert:
        touch_direction = alert["touchDirection"]
        if (
            not isinstance(touch_direction, str)
            or touch_direction not in PRICE_ALERT_TOUCH_DIRECTIONS
        ):
            raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {alert_id} touchDirection")

    pending_payload = alert.get("telegramPendingPayload")
    expected_pending_fields = None
    if isinstance(pending_payload, dict):
        expected_pending_fields = {
            "alert_id": alert_id,
            "alert_type": "price",
            "instrument_id": require_exact_identity_text(
                alert.get("instrument_id"),
                field="PRICE_ALERT_INSTRUMENT_ID",
            ),
            "route_fingerprint": require_exact_identity_text(
                alert.get("route_fingerprint"),
                field="PRICE_ALERT_ROUTE_FINGERPRINT",
            ),
            "symbol": require_exact_identity_text(
                alert.get("symbol"),
                field="PRICE_ALERT_SYMBOL",
            ),
            "timeframe": require_exact_identity_text(
                alert.get("timeframe"),
                field="PRICE_ALERT_TIMEFRAME",
            ),
        }
    validate_telegram_delivery_runtime(
        alert,
        error_prefix="PRICE_ALERT",
        expected_pending_fields=expected_pending_fields,
    )


def validate_price_alert(alert: dict[str, Any]) -> None:
    """Reject any malformed canonical persisted price-alert object."""

    validate_price_alert_definition(alert)
    validate_price_alert_runtime(alert)
