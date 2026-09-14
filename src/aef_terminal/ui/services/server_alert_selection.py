from __future__ import annotations

from typing import Any

from aef_terminal.alerts.runtime_contract import validate_price_alert
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument


def server_alert_instrument_snapshot(
    current_instruments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(current_instruments, list):
        raise RuntimeError("SERVER_ALERT_ROUTE_SNAPSHOT_INVALID")
    instrument_by_id: dict[str, dict[str, Any]] = {}
    for instrument in current_instruments:
        if not isinstance(instrument, dict):
            raise RuntimeError("SERVER_ALERT_ROUTE_SNAPSHOT_INVALID")
        route = route_instrument(instrument)
        if route.instrument_id in instrument_by_id:
            raise RuntimeError(f"SERVER_ALERT_INSTRUMENT_DUPLICATE: {route.instrument_id}")
        instrument_by_id[route.instrument_id] = instrument
    return current_instruments, instrument_by_id


def require_active_price_alerts(alerts: object) -> list[dict[str, Any]]:
    """Validate the complete active projection without manufacturing a subset."""

    if not isinstance(alerts, list):
        raise RuntimeError("SERVER_ALERT_ACTIVE_SNAPSHOT_INVALID")
    validated: list[dict[str, Any]] = []
    for index, alert in enumerate(alerts):
        if not isinstance(alert, dict):
            raise RuntimeError(f"SERVER_ALERT_ACTIVE_ROW_INVALID: {index}")
        try:
            validate_price_alert(alert)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"SERVER_ALERT_ACTIVE_ROW_INVALID: {index}") from exc
        if alert["enabled"] is not True or alert["armed"] is not True:
            raise RuntimeError(f"SERVER_ALERT_ACTIVE_ROW_INERT: {index}")
        validated.append(alert)
    return validated


def monitor_instrument_ids(
    alerts: list[dict[str, Any]],
    current_instruments: list[dict[str, Any]],
) -> list[str]:
    current_fingerprints: dict[str, str] = {}
    for instrument in current_instruments:
        route = route_instrument(instrument)
        if route.instrument_id in current_fingerprints:
            raise ValueError(f"Duplicate current instrument route: {route.instrument_id}")
        current_fingerprints[route.instrument_id] = route.fingerprint

    instrument_ids: set[str] = set()
    for alert in alerts:
        instrument_id = require_exact_identity_text(
            alert.get("instrument_id"),
            field="SERVER_ALERT_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            alert.get("route_fingerprint"),
            field="SERVER_ALERT_ROUTE_FINGERPRINT",
        )
        if current_fingerprints.get(instrument_id) == route_fingerprint:
            instrument_ids.add(instrument_id)
    return sorted(instrument_ids)
