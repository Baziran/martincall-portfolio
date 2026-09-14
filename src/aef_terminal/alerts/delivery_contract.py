from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aef_terminal.engine.common import parse_aware_utc_ts

TELEGRAM_DELIVERY_RETRY_BASE_MS = 30_000
TELEGRAM_DELIVERY_RETRY_MAX_MS = 5 * 60_000
TELEGRAM_DELIVERY_ATTEMPT_LEASE_MS = 60_000

TELEGRAM_DELIVERY_STATUSES = frozenset({"cancelled", "pending", "delivering", "sent"})
TELEGRAM_RESULT_STATUSES = frozenset(
    {
        "disabled",
        "duplicate",
        "empty_message",
        "not_configured",
        "send_failed",
        "sent",
        "telegram_error",
        "telegram_http_error",
    }
)
TELEGRAM_SUCCESS_RESULT_STATUSES = frozenset({"duplicate", "sent"})

TELEGRAM_DELIVERY_RUNTIME_FIELDS = (
    "lastTelegramStatus",
    "lastTelegramOk",
    "lastTelegramDetail",
    "telegramDeliveryStatus",
    "telegramPendingPayload",
    "telegramPendingSince",
    "telegramNextRetryAt",
    "telegramRetryCount",
    "telegramClaimedAt",
    "telegramLastAttemptAt",
    "telegramLastError",
)
TELEGRAM_CANCELLED_DELIVERY_STATE = {
    "telegramDeliveryStatus": "cancelled",
    "telegramPendingPayload": None,
    "telegramPendingSince": 0,
    "telegramNextRetryAt": 0,
    "telegramRetryCount": 0,
    "telegramClaimedAt": 0,
    "telegramLastError": "",
}
_TELEGRAM_DELIVERY_STATE_FIELDS = frozenset(
    {
        "telegramDeliveryStatus",
        "telegramPendingPayload",
        "telegramPendingSince",
        "telegramNextRetryAt",
        "telegramRetryCount",
        "telegramClaimedAt",
        "telegramLastError",
    }
)
_TELEGRAM_RESULT_FIELDS = frozenset(TELEGRAM_DELIVERY_RUNTIME_FIELDS) - (
    _TELEGRAM_DELIVERY_STATE_FIELDS
)
_TELEGRAM_PENDING_COMMON_FIELDS = frozenset(
    {
        "alert_id",
        "alert_type",
        "instrument_id",
        "route_fingerprint",
        "symbol",
        "timeframe",
        "kind",
        "direction",
        "current_price",
        "source",
        "ts",
        "trigger_event_at",
        "message",
    }
)
_TELEGRAM_PENDING_PRICE_FIELDS = _TELEGRAM_PENDING_COMMON_FIELDS | {
    "label",
    "level_source",
    "price",
}


def validate_telegram_delivery_runtime(
    item: dict[str, Any],
    *,
    error_prefix: str,
    expected_pending_fields: Mapping[str, str] | None = None,
) -> None:
    """Validate the canonical persisted Telegram delivery state without coercion."""

    if not isinstance(item, dict):
        raise ValueError(f"{error_prefix}_ROW_INVALID: runtime must be an object")

    present_delivery_fields = _TELEGRAM_DELIVERY_STATE_FIELDS.intersection(item)
    if present_delivery_fields and present_delivery_fields != _TELEGRAM_DELIVERY_STATE_FIELDS:
        missing = sorted(_TELEGRAM_DELIVERY_STATE_FIELDS - present_delivery_fields)
        raise ValueError(
            f"{error_prefix}_FIELD_INVALID: incomplete Telegram delivery state ({', '.join(missing)})"
        )

    present_result_fields = _TELEGRAM_RESULT_FIELDS.intersection(item)
    if present_result_fields and present_result_fields != _TELEGRAM_RESULT_FIELDS:
        missing = sorted(_TELEGRAM_RESULT_FIELDS - present_result_fields)
        raise ValueError(
            f"{error_prefix}_FIELD_INVALID: incomplete Telegram result state ({', '.join(missing)})"
        )

    if present_result_fields:
        result_status = item["lastTelegramStatus"]
        if not isinstance(result_status, str) or result_status not in TELEGRAM_RESULT_STATUSES:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: lastTelegramStatus")
        result_ok = item["lastTelegramOk"]
        if not isinstance(result_ok, bool):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: lastTelegramOk")
        if result_ok != (result_status in TELEGRAM_SUCCESS_RESULT_STATUSES):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: inconsistent Telegram result status")
        if not isinstance(item["lastTelegramDetail"], str):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: lastTelegramDetail")
        last_attempt_at = item["telegramLastAttemptAt"]
        if (
            isinstance(last_attempt_at, bool)
            or not isinstance(last_attempt_at, int)
            or last_attempt_at <= 0
        ):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramLastAttemptAt")

    if not present_delivery_fields:
        return

    delivery_status = item["telegramDeliveryStatus"]
    if not isinstance(delivery_status, str) or delivery_status not in TELEGRAM_DELIVERY_STATUSES:
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramDeliveryStatus")
    for field in (
        "telegramPendingSince",
        "telegramNextRetryAt",
        "telegramRetryCount",
        "telegramClaimedAt",
    ):
        value = item[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: {field}")
    if not isinstance(item["telegramLastError"], str):
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramLastError")

    pending_payload = item["telegramPendingPayload"]
    if delivery_status in {"pending", "delivering"}:
        if (
            delivery_status == "pending"
            and present_result_fields
            and item["lastTelegramOk"] is True
        ):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: inconsistent Telegram delivery status")
        if not isinstance(pending_payload, dict):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload")
        if item["telegramPendingSince"] <= 0:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingSince")
        if item["telegramNextRetryAt"] <= 0:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramNextRetryAt")
        if delivery_status == "delivering":
            if item["telegramClaimedAt"] <= 0:
                raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramClaimedAt")
            if item["telegramNextRetryAt"] <= item["telegramClaimedAt"]:
                raise ValueError(f"{error_prefix}_FIELD_INVALID: invalid Telegram delivery lease")
        elif item["telegramClaimedAt"] != 0:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramClaimedAt")
    else:
        if delivery_status == "sent" and (
            not present_result_fields or item["lastTelegramOk"] is not True
        ):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: incomplete sent Telegram state")
        if pending_payload is not None:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload")
        for field in (
            "telegramPendingSince",
            "telegramNextRetryAt",
            "telegramRetryCount",
            "telegramClaimedAt",
        ):
            if item[field] != 0:
                raise ValueError(f"{error_prefix}_FIELD_INVALID: {field}")
        if item["telegramLastError"]:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramLastError")
        return

    alert_type = pending_payload.get("alert_type")
    if alert_type != "price" or set(pending_payload) != _TELEGRAM_PENDING_PRICE_FIELDS:
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload")
    for field in (
        "alert_id",
        "alert_type",
        "instrument_id",
        "route_fingerprint",
        "symbol",
        "timeframe",
        "kind",
        "source",
        "ts",
        "message",
    ):
        value = pending_payload[field]
        if not isinstance(value, str) or (field != "message" and not value):
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.{field}")
    if pending_payload["source"] != "MartinCall backend":
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.source")
    if parse_aware_utc_ts(pending_payload["ts"]) is None:
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.ts")
    trigger_event_at = pending_payload["trigger_event_at"]
    if (
        isinstance(trigger_event_at, bool)
        or not isinstance(trigger_event_at, int)
        or trigger_event_at <= 0
    ):
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.trigger_event_at")
    current_price = pending_payload["current_price"]
    if (
        isinstance(current_price, bool)
        or not isinstance(current_price, (int, float))
        or current_price != current_price
        or current_price in (float("inf"), float("-inf"))
    ):
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.current_price")
    direction = pending_payload["direction"]
    if not isinstance(pending_payload["label"], str):
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.label")
    if not isinstance(pending_payload["level_source"], dict):
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.level_source")
    level = pending_payload["price"]
    allowed_directions = {"above", "below", "cross"}
    if not isinstance(direction, str) or direction not in allowed_directions:
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.direction")
    if (
        isinstance(level, bool)
        or not isinstance(level, (int, float))
        or level != level
        or level in (float("inf"), float("-inf"))
    ):
        raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.price")
    for field, expected in dict(expected_pending_fields or {}).items():
        if pending_payload.get(field) != expected:
            raise ValueError(f"{error_prefix}_FIELD_INVALID: telegramPendingPayload.{field}")


def telegram_delivery_due(item: dict[str, Any], now_ms: int) -> bool:
    validate_telegram_delivery_runtime(item, error_prefix="TELEGRAM_DELIVERY")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        raise ValueError("TELEGRAM_DELIVERY_FIELD_INVALID: now_ms")
    pending = item.get("telegramPendingPayload")
    if not isinstance(pending, dict):
        return False
    return item["telegramNextRetryAt"] <= now_ms


def telegram_delivery_patch(
    result: dict[str, Any],
    pending_payload: dict[str, Any],
    *,
    now_ms: int,
    current_retry_count: int = 0,
    pending_since: Any = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("TELEGRAM_RESULT_ROW_INVALID: result must be an object")
    ok = result.get("ok")
    if not isinstance(ok, bool):
        raise ValueError("TELEGRAM_RESULT_FIELD_INVALID: ok")
    status = result.get("status")
    if (
        not isinstance(status, str)
        or status not in TELEGRAM_RESULT_STATUSES
        or ok != (status in TELEGRAM_SUCCESS_RESULT_STATUSES)
    ):
        raise ValueError("TELEGRAM_RESULT_FIELD_INVALID: status")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
        raise ValueError("TELEGRAM_RESULT_FIELD_INVALID: now_ms")
    if (
        isinstance(current_retry_count, bool)
        or not isinstance(current_retry_count, int)
        or current_retry_count < 0
    ):
        raise ValueError("TELEGRAM_RESULT_FIELD_INVALID: current_retry_count")
    if pending_since is not None and (
        isinstance(pending_since, bool) or not isinstance(pending_since, int) or pending_since <= 0
    ):
        raise ValueError("TELEGRAM_RESULT_FIELD_INVALID: pending_since")
    validate_telegram_delivery_runtime(
        {
            "telegramDeliveryStatus": "pending",
            "telegramPendingPayload": pending_payload,
            "telegramPendingSince": now_ms,
            "telegramNextRetryAt": now_ms,
            "telegramRetryCount": current_retry_count,
            "telegramClaimedAt": 0,
            "telegramLastError": "",
        },
        error_prefix="TELEGRAM_RESULT",
    )
    detail = result.get("detail") or result.get("message") or result.get("code") or ""
    patch: dict[str, Any] = {
        "lastTelegramStatus": status,
        "lastTelegramOk": ok,
        "lastTelegramDetail": str(detail),
        "telegramLastAttemptAt": now_ms,
    }
    if ok:
        patch.update(
            {
                "telegramDeliveryStatus": "sent",
                "telegramPendingPayload": None,
                "telegramPendingSince": 0,
                "telegramNextRetryAt": 0,
                "telegramRetryCount": 0,
                "telegramClaimedAt": 0,
                "telegramLastError": "",
            }
        )
        validate_telegram_delivery_runtime(patch, error_prefix="TELEGRAM_DELIVERY")
        return patch
    retry_count = current_retry_count + 1
    delay_ms = min(
        TELEGRAM_DELIVERY_RETRY_BASE_MS * (2 ** min(max(0, retry_count - 1), 4)),
        TELEGRAM_DELIVERY_RETRY_MAX_MS,
    )
    patch.update(
        {
            "telegramDeliveryStatus": "pending",
            "telegramPendingPayload": dict(pending_payload),
            "telegramPendingSince": now_ms if pending_since is None else pending_since,
            "telegramNextRetryAt": now_ms + delay_ms,
            "telegramRetryCount": retry_count,
            "telegramClaimedAt": 0,
            "telegramLastError": str(detail or status),
        }
    )
    validate_telegram_delivery_runtime(patch, error_prefix="TELEGRAM_DELIVERY")
    return patch
