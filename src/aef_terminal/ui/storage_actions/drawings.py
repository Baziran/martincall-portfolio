from __future__ import annotations

from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.storage_actions.core import (
    StorageActionDeps,
    storage_validation_error,
    store_or_unavailable,
)
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument


def save_drawings_payload(deps: StorageActionDeps, payload: dict[str, Any]) -> dict[str, Any]:
    retired_fields = sorted({"symbol", "source"}.intersection(payload))
    if retired_fields:
        return storage_validation_error(
            "STORAGE_DRAWINGS_PAYLOAD_INVALID",
            f"retired drawing payload fields are forbidden: {', '.join(retired_fields)}",
        )
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
        return storage_validation_error("STORAGE_DRAWINGS_PAYLOAD_INVALID", str(exc))
    interval = str(payload.get("interval") or "").strip()
    drawings = payload.get("drawings")
    allow_mass_delete = payload.get("allow_mass_delete", False)
    deleted_ids_raw = payload.get("deleted_ids")
    if (
        not isinstance(allow_mass_delete, bool)
        or not isinstance(deleted_ids_raw, list)
        or any(not isinstance(item, str) or not item for item in deleted_ids_raw)
        or len(set(deleted_ids_raw)) != len(deleted_ids_raw)
    ):
        return storage_validation_error(
            "STORAGE_DRAWINGS_PAYLOAD_INVALID",
            "allow_mass_delete must be boolean and deleted_ids must contain exact IDs",
        )
    deleted_ids = set(deleted_ids_raw)
    if (
        not instrument_id
        or not expected_fingerprint
        or not interval
        or not isinstance(drawings, list)
    ):
        return storage_validation_error(
            "STORAGE_DRAWINGS_PAYLOAD_INVALID",
            "instrument_id, route_fingerprint, interval and drawings[] are required",
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
            "persisted drawing route does not match the qualified instrument",
        )
    presentation_symbol = route.instrument_key
    try:
        existing_drawings = deps.normalize_drawing_anchors(
            store,
            instrument_id,
            interval,
            store.read_drawings(
                instrument_id,
                interval,
                route_fingerprint=expected_fingerprint,
            ),
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=expected_fingerprint,
        )
        next_drawings = deps.normalize_drawing_anchors(
            store,
            instrument_id,
            interval,
            drawings,
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=expected_fingerprint,
            require_resolved=True,
        )
        if any(
            not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows)
            for rows in (existing_drawings, next_drawings)
        ):
            raise ValueError("DRAWING_ROWS_INVALID: hydration returned malformed rows")
    except (TypeError, ValueError) as exc:
        return storage_validation_error("STORAGE_DRAWINGS_PAYLOAD_INVALID", str(exc))
    existing_count = len(existing_drawings)
    next_count = len(next_drawings)
    existing_ids = {item["id"] for item in existing_drawings}
    next_ids = {item["id"] for item in next_drawings}
    removed_ids = existing_ids - next_ids
    unexplained_removed = removed_ids - deleted_ids
    if not allow_mass_delete and existing_count > 1 and next_count == 0:
        return {
            "ok": False,
            "count": existing_count,
            "message": f"blocked drawing full clear without explicit clear action: {existing_count} -> {next_count}",
            "existing_count": existing_count,
            "next_count": next_count,
        }
    if not allow_mass_delete and unexplained_removed:
        return {
            "ok": False,
            "count": existing_count,
            "message": f"blocked unexplained drawing deletion: {len(unexplained_removed)} object(s)",
            "existing_count": existing_count,
            "next_count": next_count,
            "removed_ids": sorted(unexplained_removed)[:12],
        }
    if (
        not allow_mass_delete
        and existing_count > 2
        and existing_count - next_count > max(len(deleted_ids), 1)
    ):
        return {
            "ok": False,
            "count": existing_count,
            "message": f"blocked suspicious drawings shrink: {existing_count} -> {next_count}",
            "existing_count": existing_count,
            "next_count": next_count,
        }
    try:
        store.replace_drawings(
            instrument_id,
            presentation_symbol,
            interval,
            next_drawings,
            route_fingerprint=expected_fingerprint,
            provider=route.provider,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
        )
    except (TypeError, ValueError) as exc:
        return storage_validation_error("STORAGE_DRAWINGS_PAYLOAD_INVALID", str(exc))
    return {
        "ok": True,
        "symbol": presentation_symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": expected_fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "interval": interval,
        "count": len(next_drawings),
    }
