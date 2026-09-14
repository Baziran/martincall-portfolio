from __future__ import annotations

from typing import Any

from aef_terminal.alerts.definition_identity import (
    price_alert_definition_identity,
    require_unique_price_alert_definitions,
)
from aef_terminal.alerts.runtime_contract import (
    PRICE_ALERT_RUNTIME_TRANSITION_FIELDS,
    validate_price_alert,
    validate_price_alert_definition,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text


def price_alert_runtime_state(alert: dict[str, Any]) -> dict[str, Any]:
    """Project one stored alert onto its low-churn server runtime contract."""

    validate_price_alert(alert)
    alert_id = require_exact_identity_text(alert.get("id"), field="PRICE_ALERT_ID")
    instrument_id = require_exact_identity_text(
        alert.get("instrument_id"),
        field="PRICE_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        alert.get("route_fingerprint"),
        field="PRICE_ALERT_ROUTE_FINGERPRINT",
    )
    timeframe = require_exact_identity_text(
        alert.get("timeframe"),
        field="PRICE_ALERT_TIMEFRAME",
    )
    return {
        "id": alert_id,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "timeframe": timeframe,
        **{
            field: alert[field] for field in PRICE_ALERT_RUNTIME_TRANSITION_FIELDS if field in alert
        },
    }


def price_alert_client_projection(alert: dict[str, Any]) -> dict[str, Any]:
    """Attach the server-owned exact definition identity to a client projection."""

    validate_price_alert(alert)
    return {
        **alert,
        "definition_identity": price_alert_definition_identity(alert),
    }


def normalize_price_alert(
    alert: dict[str, Any],
    *,
    timeframe: str,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    provider_contract_id: str,
) -> dict[str, Any]:
    if not isinstance(alert, dict):
        raise ValueError("PRICE_ALERT_ROW_INVALID: alert must be an object")
    alert_id = require_exact_identity_text(alert.get("id"), field="PRICE_ALERT_ID")
    require_exact_identity_text(alert.get("symbol"), field="PRICE_ALERT_SYMBOL")
    timeframe = require_exact_identity_text(timeframe, field="PRICE_ALERT_TIMEFRAME")
    instrument_id = require_exact_identity_text(
        instrument_id,
        field="PRICE_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="PRICE_ALERT_ROUTE_FINGERPRINT",
    )
    provider = require_exact_identity_text(provider, field="PRICE_ALERT_PROVIDER")
    provider_contract_id = require_exact_identity_text(
        provider_contract_id,
        field="PRICE_ALERT_PROVIDER_CONTRACT_ID",
    )
    if alert.get("instrument_id") != instrument_id:
        raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {alert_id} instrument_id")
    if alert.get("route_fingerprint") != route_fingerprint:
        raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {alert_id} route_fingerprint")
    if alert.get("timeframe") != timeframe:
        raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {alert_id} timeframe")
    if alert.get("provider") != provider:
        raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {alert_id} provider")
    if alert.get("provider_contract_id") != provider_contract_id:
        raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {alert_id} provider_contract_id")
    out = dict(alert)
    out["instrument_id"] = instrument_id
    out["route_fingerprint"] = route_fingerprint
    out["provider"] = provider
    out["provider_contract_id"] = provider_contract_id
    validate_price_alert_definition(out)
    return out


def require_unique_price_alerts(
    alerts: list[dict[str, Any]],
    *,
    timeframe: str,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    provider_contract_id: str,
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    alert_ids: set[str] = set()
    for raw in alerts:
        alert = normalize_price_alert(
            raw,
            timeframe=timeframe,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            provider=provider,
            provider_contract_id=provider_contract_id,
        )
        alert_id = alert["id"]
        if alert_id in alert_ids:
            raise ValueError(f"PRICE_ALERT_ID_DUPLICATE: {alert_id}")
        alert_ids.add(alert_id)
        validate_price_alert(alert)
        validated.append(alert)
    require_unique_price_alert_definitions(validated)
    return [price_alert_client_projection(alert) for alert in validated]
