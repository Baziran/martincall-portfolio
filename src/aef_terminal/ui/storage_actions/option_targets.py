from __future__ import annotations

from typing import Any

from aef_terminal.data.gex.option_target_contract import OptionTargetCreateResult
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.provider_contract import OptionsProvider
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.snapshot.db_context import (
    require_provider_chart_axis_point,
)
from aef_terminal.ui.storage_actions.core import (
    StorageActionDeps,
    storage_validation_error,
    store_or_unavailable,
)
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument


def _cancel_option_target_quote(
    deps: StorageActionDeps,
    route: Any,
    target_payload: dict[str, Any],
    target_id: str,
) -> dict[str, Any]:
    canonical_payload = target_payload.get("payload")
    quote_payload = (
        canonical_payload.get("intent")
        if isinstance(canonical_payload, dict) and isinstance(canonical_payload.get("intent"), dict)
        else None
    )
    try:
        if not isinstance(quote_payload, dict):
            raise ValueError("option target canonical intent is required")
        stored_instrument_id = require_exact_identity_text(
            target_payload.get("instrument_id"),
            field="STORAGE_INSTRUMENT_ID",
        )
        stored_fingerprint = require_exact_identity_text(
            target_payload.get("route_fingerprint"),
            field="STORAGE_ROUTE_FINGERPRINT",
        )
        if stored_instrument_id != route.instrument_id or stored_fingerprint != route.fingerprint:
            raise ValueError("option target route fingerprint mismatch")
        if not route.adapter.capabilities.options or not isinstance(
            route.adapter,
            OptionsProvider,
        ):
            raise ValueError(f"OPTION_TARGET_PROVIDER_UNSUPPORTED provider={route.provider}")
        return route.adapter.cancel_option_quote(
            route.instrument,
            {
                "sec_type": quote_payload.get("sec_type"),
                "con_id": quote_payload.get("con_id"),
                "exchange": quote_payload.get("exchange"),
            },
            consumer_id=f"option-point:{target_id}",
        )
    except Exception as exc:
        if deps.logger is not None:
            deps.logger.warning("Option target quote cancel failed for %s: %s", target_id, exc)
        return {
            "ok": False,
            "cancelled": False,
            "message": str(exc) or exc.__class__.__name__,
        }


def option_target_drawings_payload(
    deps: StorageActionDeps,
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any]:
    try:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="STORAGE_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="STORAGE_ROUTE_FINGERPRINT",
        )
    except ValueError as exc:
        return storage_validation_error("STORAGE_INSTRUMENT_IDENTITY_INVALID", str(exc), items=[])
    try:
        route = route_instrument(lookup_runtime_instrument(instrument_id))
    except ValueError as exc:
        return storage_validation_error("STORAGE_INSTRUMENT_IDENTITY_INVALID", str(exc), items=[])
    if route.fingerprint != route_fingerprint:
        return storage_validation_error(
            "STORAGE_INSTRUMENT_ROUTE_MISMATCH",
            "option target route does not match the qualified instrument",
            items=[],
        )
    store = store_or_unavailable(deps, items=[])
    if isinstance(store, dict):
        return store
    return {
        "ok": True,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "items": [
            row["payload"]
            for row in store.read_option_targets(
                route_pairs=[(route.instrument_id, route.fingerprint)],
            )
        ],
    }


def _mutate_option_target_payload(
    deps: StorageActionDeps,
    payload: dict[str, Any],
    *,
    operation: str,
    target_id: str | None = None,
) -> dict[str, Any]:
    if operation not in {"create", "update"}:
        raise ValueError("OPTION_TARGET_MUTATION_OPERATION_INVALID")
    if operation == "update" and target_id is None:
        raise ValueError("OPTION_TARGET_ID_REQUIRED")
    try:
        instrument_id = require_exact_identity_text(
            payload.get("instrument_id"),
            field="STORAGE_INSTRUMENT_ID",
        )
        expected_fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="STORAGE_ROUTE_FINGERPRINT",
        )
    except ValueError as exc:
        return storage_validation_error("STORAGE_INSTRUMENT_TIMEFRAME_REQUIRED", str(exc))
    interval_value = payload.get("timeframe")
    interval = interval_value if isinstance(interval_value, str) else ""
    if not instrument_id or not expected_fingerprint or not interval:
        return storage_validation_error(
            "STORAGE_INSTRUMENT_TIMEFRAME_REQUIRED",
            "instrument_id, route_fingerprint and timeframe are required",
        )
    store = store_or_unavailable(deps)
    if isinstance(store, dict):
        return store
    try:
        route = route_instrument(lookup_runtime_instrument(instrument_id))
    except ValueError as exc:
        return storage_validation_error("STORAGE_INSTRUMENT_IDENTITY_INVALID", str(exc))
    if route.fingerprint != expected_fingerprint:
        return storage_validation_error(
            "STORAGE_INSTRUMENT_ROUTE_MISMATCH",
            "option target route does not match the qualified instrument",
        )
    point = payload.get("point")
    target_at = parse_gex_timestamp(point.get("ts")) if isinstance(point, dict) else None
    if target_at is None:
        return storage_validation_error(
            "OPTION_TARGET_CHART_POINT_INVALID",
            "option target requires an exact timestamp",
        )
    try:
        canonical_target_slot = require_provider_chart_axis_point(
            store,
            route.instrument,
            interval,
            target_at,
        )
    except ValueError as exc:
        return storage_validation_error(
            "OPTION_TARGET_CHART_POINT_INVALID",
            str(exc),
        )
    option_container = payload.get("payload")
    intent = (
        option_container.get("intent")
        if isinstance(option_container, dict) and isinstance(option_container.get("intent"), dict)
        else None
    )
    if not isinstance(intent, dict):
        return storage_validation_error(
            "OPTION_TARGET_INTENT_REQUIRED",
            "option target payload.intent is required",
        )
    if target_id is not None and payload.get("id") not in (None, target_id):
        return storage_validation_error(
            "OPTION_TARGET_ID_MISMATCH",
            "option target payload id does not match the request path",
        )
    item_payload = {
        **payload,
        "point": {
            **point,
            "barSlot": canonical_target_slot,
        },
        **({"id": target_id} if target_id is not None else {}),
    }
    write_kwargs = {
        "route_fingerprint": expected_fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
    }
    evicted: tuple[dict[str, Any], ...] = ()
    if operation == "create":
        create_result = store.create_option_target_intent(
            instrument_id,
            route.instrument_key,
            interval,
            item_payload,
            **write_kwargs,
        )
        if not isinstance(create_result, OptionTargetCreateResult):
            raise TypeError("OPTION_TARGET_CREATE_RESULT_INVALID")
        item = create_result.item
        evicted = create_result.evicted
    else:
        item = store.update_option_target_intent(
            instrument_id,
            route.instrument_key,
            interval,
            item_payload,
            **write_kwargs,
        )
    quote_cancels = [
        _cancel_option_target_quote(
            deps,
            route,
            evicted_payload,
            require_exact_identity_text(
                evicted_payload.get("id"),
                field="STORAGE_OPTION_TARGET_ID",
            ),
        )
        for evicted_payload in evicted
    ]
    return {
        "ok": True,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "item": item,
        "evicted_ids": [str(row["id"]) for row in evicted],
        "quote_cancels": quote_cancels,
    }


def create_option_target_payload(
    deps: StorageActionDeps,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return _mutate_option_target_payload(deps, payload, operation="create")


def update_option_target_payload(
    deps: StorageActionDeps,
    target_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return _mutate_option_target_payload(
        deps,
        payload,
        operation="update",
        target_id=target_id,
    )


def delete_option_target_payload(
    deps: StorageActionDeps,
    target_id: str,
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any]:
    instrument_id = require_exact_identity_text(
        instrument_id,
        field="STORAGE_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="STORAGE_ROUTE_FINGERPRINT",
    )
    target_id = require_exact_identity_text(
        target_id,
        field="STORAGE_OPTION_TARGET_ID",
    )
    route = route_instrument(lookup_runtime_instrument(instrument_id))
    if route.fingerprint != route_fingerprint:
        return storage_validation_error(
            "STORAGE_INSTRUMENT_ROUTE_MISMATCH",
            "option target route does not match the qualified instrument",
        )
    store = store_or_unavailable(deps)
    if isinstance(store, dict):
        return store
    target_payload = None
    for row in store.read_option_targets(
        route_pairs=[(instrument_id, route_fingerprint)],
    ):
        if str(row.get("id") or "") == str(target_id):
            target_payload = row.get("payload")
            break
    deleted = store.delete_option_target(
        instrument_id,
        route_fingerprint,
        target_id,
    )
    quote_cancel: dict[str, Any] = {"ok": True, "cancelled": False}
    if deleted and isinstance(target_payload, dict):
        quote_cancel = _cancel_option_target_quote(deps, route, target_payload, target_id)
    return {
        "ok": True,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "deleted": deleted,
        "quote_cancel": quote_cancel,
    }
