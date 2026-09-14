from __future__ import annotations

from typing import Any
from uuid import UUID

from aef_terminal.settings_contract import (
    invalid_browser_client_setting_keys,
    invalid_browser_client_setting_value_keys,
    invalid_client_setting_value_keys,
)
from aef_terminal.ui.storage_actions.core import (
    StorageActionDeps,
    storage_validation_error,
    store_or_unavailable,
)


def save_client_settings_payload(
    deps: StorageActionDeps, payload: dict[str, Any]
) -> dict[str, Any]:
    if set(payload) != {"writer_id", "mutations"}:
        return storage_validation_error(
            "SETTINGS_PAYLOAD_INVALID",
            "settings payload must contain exactly writer_id and mutations",
        )
    raw_mutations = payload.get("mutations")
    if not isinstance(raw_mutations, list):
        return storage_validation_error(
            "SETTINGS_PAYLOAD_INVALID",
            "settings mutations must be a list",
        )
    if not raw_mutations:
        return storage_validation_error("SETTINGS_PAYLOAD_EMPTY", "no settings supplied")
    writer_id = payload.get("writer_id")
    try:
        parsed_writer_id = UUID(writer_id) if isinstance(writer_id, str) else None
    except ValueError:
        parsed_writer_id = None
    if (
        parsed_writer_id is None
        or parsed_writer_id.version != 4
        or str(parsed_writer_id) != writer_id
    ):
        return storage_validation_error(
            "SETTINGS_MUTATION_ORDER_INVALID",
            "writer_id must be a canonical UUIDv4",
        )
    values: dict[str, Any] = {}
    mutation_orders: dict[str, tuple[int, int]] = {}
    for mutation in raw_mutations:
        if not isinstance(mutation, dict) or set(mutation) != {
            "key",
            "value",
            "changed_at_ms",
            "sequence",
        }:
            return storage_validation_error(
                "SETTINGS_PAYLOAD_INVALID",
                "each settings mutation must contain exact key, value, changed_at_ms and sequence",
            )
        key = mutation.get("key")
        changed_at_ms = mutation.get("changed_at_ms")
        sequence = mutation.get("sequence")
        if (
            not isinstance(key, str)
            or not key
            or key in values
            or not isinstance(changed_at_ms, int)
            or isinstance(changed_at_ms, bool)
            or not 0 < changed_at_ms <= 9_007_199_254_740_991
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 < sequence <= 9_007_199_254_740_991
        ):
            return storage_validation_error(
                "SETTINGS_MUTATION_ORDER_INVALID",
                "settings mutation keys must be unique and ordering fields positive safe integers",
            )
        values[key] = mutation.get("value")
        mutation_orders[key] = (changed_at_ms, sequence)
    invalid_keys = invalid_browser_client_setting_keys(values)
    if invalid_keys:
        return storage_validation_error(
            "SETTINGS_KEY_FORBIDDEN",
            "client setting key is outside the current browser-writable contract",
            count=0,
            keys=invalid_keys,
        )
    invalid_value_keys = invalid_browser_client_setting_value_keys(values)
    if invalid_value_keys:
        return storage_validation_error(
            "SETTINGS_VALUE_INVALID",
            "client setting value is outside the current typed contract",
            count=0,
            keys=invalid_value_keys,
        )
    store = store_or_unavailable(deps)
    if isinstance(store, dict):
        return store
    try:
        mutation_results, settings_revision = store.upsert_settings(
            "client",
            values,
            mutation_writer_id=writer_id,
            mutation_orders=mutation_orders,
        )
    except ValueError as exc:
        return storage_validation_error(
            "SETTINGS_VALUE_INVALID",
            str(exc),
            count=0,
        )
    if (
        not isinstance(mutation_results, dict)
        or set(mutation_results) != set(values)
        or not isinstance(settings_revision, int)
        or isinstance(settings_revision, bool)
        or not 0 < settings_revision <= 9_007_199_254_740_991
    ):
        raise RuntimeError("SETTINGS_MUTATION_ACK_INVALID")
    expected_result_keys = {
        "outcome",
        "value",
        "changed_at_ms",
        "writer_id",
        "sequence",
    }
    authoritative_values: dict[str, Any] = {}
    for key, result in mutation_results.items():
        result_writer_id = result.get("writer_id") if isinstance(result, dict) else None
        try:
            result_writer = UUID(result_writer_id) if isinstance(result_writer_id, str) else None
        except ValueError:
            result_writer = None
        if (
            not isinstance(result, dict)
            or set(result) != expected_result_keys
            or result.get("outcome") not in {"applied", "replayed", "rejected"}
            or not isinstance(result.get("changed_at_ms"), int)
            or isinstance(result.get("changed_at_ms"), bool)
            or not 0 < result.get("changed_at_ms") <= 9_007_199_254_740_991
            or not isinstance(result.get("sequence"), int)
            or isinstance(result.get("sequence"), bool)
            or not 0 < result.get("sequence") <= 9_007_199_254_740_991
            or result_writer is None
            or result_writer.version != 4
            or str(result_writer) != result_writer_id
        ):
            raise RuntimeError(f"SETTINGS_MUTATION_ACK_INVALID key={key!r}")
        authoritative_values[key] = result["value"]
        submitted_changed_at_ms, submitted_sequence = mutation_orders[key]
        submitted_order = (submitted_changed_at_ms, writer_id, submitted_sequence)
        authoritative_order = (
            result["changed_at_ms"],
            result_writer_id,
            result["sequence"],
        )
        if result["outcome"] in {"applied", "replayed"}:
            if authoritative_order != submitted_order or result["value"] != values[key]:
                raise RuntimeError(f"SETTINGS_MUTATION_ACK_INVALID key={key!r}")
        elif authoritative_order <= submitted_order:
            raise RuntimeError(f"SETTINGS_MUTATION_ACK_INVALID key={key!r}")
    invalid_authoritative_values = invalid_client_setting_value_keys(authoritative_values)
    if invalid_authoritative_values:
        raise RuntimeError(
            f"SETTINGS_MUTATION_ACK_VALUE_INVALID keys={invalid_authoritative_values!r}"
        )
    applied_keys = {
        key for key, result in mutation_results.items() if result["outcome"] == "applied"
    }
    deps.publish_client_settings_mutations(
        mutation_results,
        settings_revision=settings_revision,
    )
    deps.apply_ibkr_runtime_settings(authoritative_values)
    return {
        "ok": True,
        "count": len(applied_keys),
        "settings_revision": settings_revision,
        "mutations": [{"key": key, **mutation_results[key]} for key in values],
    }
