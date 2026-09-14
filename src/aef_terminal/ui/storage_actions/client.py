from __future__ import annotations

from typing import Any

from aef_terminal.alerts.runtime_contract import validate_price_alert
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.storage_actions.core import (
    StorageActionDeps,
    storage_validation_error,
    store_or_unavailable,
)


def client_storage_payload(
    deps: StorageActionDeps,
    *,
    instrument_id: str = "",
    route_fingerprint: str = "",
    interval: str = "5m",
    settings: bool = True,
    drawings: bool = True,
    alerts: bool = True,
) -> dict[str, Any]:
    interval = str(interval or "").strip()
    try:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="STORAGE_INSTRUMENT_ID",
            allow_empty=True,
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="STORAGE_ROUTE_FINGERPRINT",
            allow_empty=True,
        )
    except ValueError as exc:
        return storage_validation_error(
            "STORAGE_INSTRUMENT_IDENTITY_INVALID",
            str(exc),
            settings={},
            drawings=[],
            alerts=[],
        )
    if (drawings or alerts) and (not instrument_id or not route_fingerprint):
        return storage_validation_error(
            "STORAGE_INSTRUMENT_IDENTITY_REQUIRED",
            "instrument_id and route_fingerprint are required when loading instrument state",
            settings={},
            drawings=[],
            alerts=[],
        )
    store = store_or_unavailable(deps, settings={}, drawings=[], alerts=[])
    if isinstance(store, dict):
        return store
    route = None
    if drawings or alerts:
        try:
            route = route_instrument(lookup_runtime_instrument(instrument_id))
        except ValueError as exc:
            return storage_validation_error("STORAGE_INSTRUMENT_IDENTITY_INVALID", str(exc))
        if route.fingerprint != route_fingerprint:
            return storage_validation_error(
                "STORAGE_INSTRUMENT_ROUTE_MISMATCH",
                "requested storage route does not match the qualified instrument",
            )
        symbol = str(route.instrument.get("display") or route.instrument_key or "").strip()
    else:
        symbol = ""
    client_settings: dict[str, Any] = {}
    settings_mutations: list[dict[str, Any]] = []
    settings_mutation_changed_at_ms = 0
    settings_revision = 0
    if settings:
        (
            stored_settings,
            stored_mutation_orders,
            settings_mutation_changed_at_ms,
            settings_revision,
        ) = store.read_settings_snapshot("client")
        stored_settings = dict(stored_settings)
        client_settings = {str(key): value for key, value in stored_settings.items()}
        if not isinstance(stored_mutation_orders, dict) or set(stored_mutation_orders) != set(
            client_settings
        ):
            raise RuntimeError("SETTINGS_MUTATION_SNAPSHOT_INVALID")
        settings_mutations = [
            {"key": key, **stored_mutation_orders[key]} for key in client_settings
        ]
        if (
            max(
                (int(order.get("changed_at_ms", -1)) for order in stored_mutation_orders.values()),
                default=0,
            )
            != settings_mutation_changed_at_ms
        ):
            raise RuntimeError("SETTINGS_MUTATION_SNAPSHOT_INVALID")

    price_alerts: list[dict[str, Any]] = []
    if alerts:
        price_alerts = deps.require_unique_price_alerts(
            store.read_price_alerts(
                instrument_id,
                interval,
                route_fingerprint=route_fingerprint,
            ),
            timeframe=interval,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            provider=route.provider,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
        )
        for alert in price_alerts:
            validate_price_alert(alert)
    drawing_items: list[dict[str, Any]] = []
    if drawings:
        drawing_items = deps.normalize_drawing_anchors(
            store,
            instrument_id,
            interval,
            store.read_drawings(
                instrument_id,
                interval,
                route_fingerprint=route_fingerprint,
            ),
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route_fingerprint,
        )
    return {
        "ok": True,
        "symbol": symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider": route.provider if route is not None else "",
        "provider_contract_id": route.adapter.session_contract_id(route.instrument)
        if route is not None
        else "",
        "interval": interval,
        "settings": client_settings,
        "settings_mutations": settings_mutations,
        "settings_mutation_changed_at_ms": settings_mutation_changed_at_ms,
        "settings_revision": settings_revision,
        "drawings": drawing_items,
        "alerts": price_alerts,
    }
