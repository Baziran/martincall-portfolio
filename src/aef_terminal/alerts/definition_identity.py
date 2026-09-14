from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from aef_terminal.alerts.runtime_contract import (
    PRICE_ALERT_DIRECTIONS,
    validate_price_alert_definition,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text


def _plain_definition_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_definition_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_definition_value(item) for item in value]
    return value


def price_alert_definition_identity(alert: Mapping[str, Any]) -> str:
    """Return the canonical exact identity of one price-alert definition."""

    payload = _plain_definition_value(alert)
    if not isinstance(payload, dict):
        raise TypeError("price alert definition must be an object")
    validate_price_alert_definition(payload)
    instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="PRICE_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="PRICE_ALERT_ROUTE_FINGERPRINT",
    )
    timeframe = require_exact_identity_text(
        payload.get("timeframe"),
        field="PRICE_ALERT_TIMEFRAME",
    )
    kind = payload["kind"]
    direction = payload["direction"]
    if direction not in PRICE_ALERT_DIRECTIONS:
        raise ValueError(f"unsupported price alert direction: {direction or '-'}")
    price_key = "" if kind in {"ema233_touch", "vsa_fuel"} else format(payload["price"], ".15g")
    tolerance_atr_key = f"{payload['toleranceAtr']:.3f}"
    tolerance_points_key = f"{payload['tolerancePoints']:.2f}"
    return json.dumps(
        [
            instrument_id,
            route_fingerprint,
            timeframe,
            kind,
            direction,
            price_key,
            tolerance_atr_key,
            tolerance_points_key,
        ],
        separators=(",", ":"),
    )


def require_unique_price_alert_definitions(alerts: Iterable[Mapping[str, Any]]) -> None:
    """Fail closed when two alert rows own the same exact definition."""

    alert_id_by_definition: dict[str, str] = {}
    for alert in alerts:
        definition_identity = price_alert_definition_identity(alert)
        alert_id = require_exact_identity_text(alert.get("id"), field="PRICE_ALERT_ID")
        existing_id = alert_id_by_definition.get(definition_identity)
        if existing_id is not None:
            raise ValueError(f"PRICE_ALERT_SEMANTIC_DUPLICATE: {existing_id}, {alert_id}")
        alert_id_by_definition[definition_identity] = alert_id
