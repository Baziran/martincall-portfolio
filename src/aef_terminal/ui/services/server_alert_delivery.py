from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from aef_terminal.alerts.delivery_contract import (
    TELEGRAM_DELIVERY_ATTEMPT_LEASE_MS,
    telegram_delivery_due,
    telegram_delivery_patch,
    validate_telegram_delivery_runtime,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.ui.runtime.quote_stream import QuoteRouteSnapshot

SERVER_ALERT_DELIVERY_BATCH_SIZE = 2


class ServerAlertDeliveryDeps(Protocol):
    store_factory: Callable[[], Any]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    server_pending_price_alerts_snapshot: Callable[[Any], list[dict[str, Any]]]
    send_server_telegram_alert: Callable[[dict[str, Any]], dict[str, Any]]


def _claim_send_finalize_price_telegram_delivery(
    store: Any,
    sender: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    alert: dict[str, Any],
    alert_id: str,
    instrument_id: str,
    route_fingerprint: str,
    pending_payload: dict[str, Any],
    delivery_payload: dict[str, Any],
    now_ms: int,
) -> bool:
    """Own one claimed delivery through its physical terminal transition."""

    attempt_at = max(
        now_ms,
        alert["telegramClaimedAt"] + 1,
        alert.get("telegramLastAttemptAt", 0) + 1,
    )
    claim_patch = {
        "telegramDeliveryStatus": "delivering",
        "telegramClaimedAt": attempt_at,
        "telegramNextRetryAt": attempt_at + TELEGRAM_DELIVERY_ATTEMPT_LEASE_MS,
    }
    validate_telegram_delivery_runtime(
        {**alert, **claim_patch},
        error_prefix="SERVER_ALERT",
    )
    claimed = store.update_price_alert_payload_exact(
        alert_id,
        instrument_id,
        route_fingerprint,
        claim_patch,
        expected_rearmed_at=alert.get("rearmedAt"),
        check_generation=True,
        expected_pending_payload=pending_payload,
        expected_fields={
            "telegramDeliveryStatus": alert.get("telegramDeliveryStatus"),
            "telegramClaimedAt": alert.get("telegramClaimedAt"),
            "telegramNextRetryAt": alert.get("telegramNextRetryAt"),
        },
    )
    if not isinstance(claimed, dict):
        return False
    validate_telegram_delivery_runtime(claimed, error_prefix="SERVER_ALERT")
    result = sender(delivery_payload)
    store.update_price_alert_payload_exact(
        alert_id,
        instrument_id,
        route_fingerprint,
        telegram_delivery_patch(
            result,
            delivery_payload,
            now_ms=now_ms,
            current_retry_count=claimed["telegramRetryCount"],
            pending_since=claimed["telegramPendingSince"],
        ),
        expected_rearmed_at=claimed.get("rearmedAt"),
        check_generation=True,
        expected_pending_payload=pending_payload,
        expected_fields={
            "telegramDeliveryStatus": claimed.get("telegramDeliveryStatus"),
            "telegramClaimedAt": claimed.get("telegramClaimedAt"),
            "telegramNextRetryAt": claimed.get("telegramNextRetryAt"),
        },
    )
    return True


async def persist_price_alert_delivery(
    store: Any,
    alert: dict[str, Any],
    pending_payload: dict[str, Any],
    state_patch: dict[str, Any],
    *,
    now_ms: int,
) -> bool:
    """Persist one fired alert and its durable delivery intent without network I/O."""

    try:
        validate_telegram_delivery_runtime(alert, error_prefix="SERVER_ALERT")
        alert_id = require_exact_identity_text(alert.get("id"), field="SERVER_ALERT_ID")
        instrument_id = require_exact_identity_text(
            alert.get("instrument_id"),
            field="SERVER_ALERT_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            alert.get("route_fingerprint"),
            field="SERVER_ALERT_ROUTE_FINGERPRINT",
        )
        retry_count = alert["telegramRetryCount"]
    except KeyError, TypeError, ValueError:
        return False
    pending_patch = {
        **state_patch,
        "telegramDeliveryStatus": "pending",
        "telegramPendingPayload": dict(pending_payload),
        "telegramPendingSince": now_ms,
        "telegramNextRetryAt": now_ms,
        "telegramRetryCount": retry_count,
        "telegramClaimedAt": 0,
        "telegramLastError": "",
    }
    validate_telegram_delivery_runtime(
        pending_patch,
        error_prefix="SERVER_ALERT",
    )
    persisted = await run_physical_thread_call(
        store.update_price_alert_payload_exact,
        alert_id,
        instrument_id,
        route_fingerprint,
        pending_patch,
        expected_rearmed_at=alert.get("rearmedAt"),
        check_generation=True,
        require_active=True,
        expected_fields={
            "lastFiredAt": alert.get("lastFiredAt"),
            "telegramPendingPayload": alert.get("telegramPendingPayload"),
        },
    )
    if not isinstance(persisted, dict):
        return False
    validate_telegram_delivery_runtime(persisted, error_prefix="SERVER_ALERT")
    return True


async def process_pending_price_telegram_deliveries(
    deps: ServerAlertDeliveryDeps,
    store: Any,
    alerts: list[dict[str, Any]],
    route_snapshot: QuoteRouteSnapshot,
    now_ms: int,
    *,
    limit: int | None = None,
) -> int:
    delivered = 0
    attempted = 0
    for alert in alerts:
        if limit is not None and attempted >= max(int(limit), 0):
            break
        if not isinstance(alert, dict) or not telegram_delivery_due(alert, now_ms):
            continue
        pending_payload = alert.get("telegramPendingPayload")
        alert_id = str(alert.get("id") or "")
        if not isinstance(pending_payload, dict) or not alert_id:
            continue
        try:
            instrument_id = require_exact_identity_text(
                alert.get("instrument_id"),
                field="SERVER_ALERT_INSTRUMENT_ID",
            )
            route_fingerprint = require_exact_identity_text(
                alert.get("route_fingerprint"),
                field="SERVER_ALERT_ROUTE_FINGERPRINT",
            )
        except ValueError:
            continue
        entry = route_snapshot.entries.get(instrument_id)
        if entry is None or entry.route_fingerprint != route_fingerprint:
            continue
        delivery_payload = {
            **pending_payload,
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
        }
        attempted += 1
        delivered += bool(
            await run_physical_thread_call(
                _claim_send_finalize_price_telegram_delivery,
                store,
                deps.send_server_telegram_alert,
                alert=alert,
                alert_id=alert_id,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                pending_payload=pending_payload,
                delivery_payload=delivery_payload,
                now_ms=now_ms,
            )
        )
    return delivered


async def process_server_alert_delivery_tick(
    deps: ServerAlertDeliveryDeps,
) -> None:
    """Drain a bounded durable Telegram outbox independently of alert evaluation."""

    store = deps.store_factory()
    if store is None:
        return
    alerts = deps.server_pending_price_alerts_snapshot(store)
    if not alerts:
        return
    route_snapshot = deps.quote_route_snapshot()
    now_ms = int(time.time() * 1000)
    await process_pending_price_telegram_deliveries(
        deps,
        store,
        alerts,
        route_snapshot,
        now_ms,
        limit=SERVER_ALERT_DELIVERY_BATCH_SIZE,
    )
