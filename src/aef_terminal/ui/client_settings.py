from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from aef_terminal.ui.reference_services import (
    selected_instruments as reference_selected_instruments,
)


@dataclass(frozen=True)
class ClientSettingsDeps:
    store_factory: Callable[[], Any]


_DEPS: ClientSettingsDeps | None = None
_RUNTIME_LOCK = threading.RLock()
_RUNTIME_SETTINGS: dict[str, Any] | None = None
_RUNTIME_MUTATION_ORDERS: dict[str, tuple[int, str, int]] = {}
_RUNTIME_KEY_REVISIONS: dict[str, int] = {}
_RUNTIME_REVISION: int | None = None
_RUNTIME_SNAPSHOT_REVISION: int | None = None
_MAX_SAFE_SETTINGS_REVISION = 9_007_199_254_740_991


def configure_client_settings_deps(deps: ClientSettingsDeps) -> None:
    global _DEPS, _RUNTIME_SETTINGS, _RUNTIME_REVISION, _RUNTIME_SNAPSHOT_REVISION
    _DEPS = deps
    with _RUNTIME_LOCK:
        _RUNTIME_SETTINGS = None
        _RUNTIME_REVISION = None
        _RUNTIME_SNAPSHOT_REVISION = None
        _RUNTIME_MUTATION_ORDERS.clear()
        _RUNTIME_KEY_REVISIONS.clear()


def _deps() -> ClientSettingsDeps:
    if _DEPS is None:
        raise RuntimeError("client settings dependencies are not configured")
    return _DEPS


def _postgres_store():
    return _deps().store_factory()


def read_client_settings_snapshot() -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    int,
    int,
]:
    store = _postgres_store()
    if store is None:
        raise RuntimeError("CLIENT_SETTINGS_STORAGE_REQUIRED")
    return store.read_settings_snapshot("client")


def _require_settings_revision(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= _MAX_SAFE_SETTINGS_REVISION
    ):
        raise ValueError("CLIENT_SETTINGS_RUNTIME_REVISION_INVALID")
    return value


def _snapshot_mutation_order(value: Any) -> tuple[int, str, int] | None:
    if not isinstance(value, dict) or set(value) != {
        "changed_at_ms",
        "writer_id",
        "sequence",
    }:
        raise TypeError("CLIENT_SETTINGS_RUNTIME_MUTATION_ORDER_INVALID")
    changed_at_ms = value.get("changed_at_ms")
    writer_id = value.get("writer_id")
    sequence = value.get("sequence")
    if changed_at_ms == 0 and writer_id is None and sequence == 0:
        return None
    if (
        not isinstance(changed_at_ms, int)
        or isinstance(changed_at_ms, bool)
        or not 0 < changed_at_ms <= _MAX_SAFE_SETTINGS_REVISION
        or not isinstance(writer_id, str)
        or not writer_id
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 0 < sequence <= _MAX_SAFE_SETTINGS_REVISION
    ):
        raise ValueError("CLIENT_SETTINGS_RUNTIME_MUTATION_ORDER_INVALID")
    return changed_at_ms, writer_id, sequence


def _prepare_settings_snapshot(
    settings: dict[str, Any],
    mutation_orders: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, tuple[int, str, int]]]:
    if not isinstance(settings, dict):
        raise TypeError("CLIENT_SETTINGS_RUNTIME_PAYLOAD_INVALID")
    if not isinstance(mutation_orders, dict) or set(mutation_orders) != set(settings):
        raise TypeError("CLIENT_SETTINGS_RUNTIME_MUTATION_SNAPSHOT_INVALID")
    if any(not isinstance(key, str) or not key for key in settings):
        raise TypeError("CLIENT_SETTINGS_RUNTIME_PAYLOAD_INVALID")
    prepared_orders = {
        key: order
        for key, raw_order in mutation_orders.items()
        if (order := _snapshot_mutation_order(raw_order)) is not None
    }
    return deepcopy(settings), prepared_orders


def publish_client_settings_snapshot(
    settings: dict[str, Any],
    mutation_orders: dict[str, dict[str, Any]],
    settings_revision: int,
) -> None:
    """Replace the process-local projection after reading PostgreSQL authority."""

    prepared_settings, prepared_orders = _prepare_settings_snapshot(settings, mutation_orders)
    revision = _require_settings_revision(settings_revision)
    global _RUNTIME_SETTINGS, _RUNTIME_REVISION, _RUNTIME_SNAPSHOT_REVISION
    with _RUNTIME_LOCK:
        _RUNTIME_SETTINGS = prepared_settings
        _RUNTIME_REVISION = revision
        _RUNTIME_SNAPSHOT_REVISION = revision
        _RUNTIME_MUTATION_ORDERS.clear()
        _RUNTIME_MUTATION_ORDERS.update(prepared_orders)
        _RUNTIME_KEY_REVISIONS.clear()
        _RUNTIME_KEY_REVISIONS.update({key: revision for key in prepared_settings})


def _publish_client_setting_value_locked(
    key: str,
    value: Any,
    *,
    mutation_order: tuple[int, str, int] | None,
    settings_revision: int,
) -> None:
    if _RUNTIME_SETTINGS is None:
        raise RuntimeError("CLIENT_SETTINGS_RUNTIME_UNAVAILABLE")
    current_revision = _RUNTIME_KEY_REVISIONS.get(
        key,
        _RUNTIME_SNAPSHOT_REVISION if _RUNTIME_SNAPSHOT_REVISION is not None else -1,
    )
    if settings_revision < current_revision:
        return
    if settings_revision == current_revision:
        if key not in _RUNTIME_SETTINGS or _RUNTIME_SETTINGS[key] != value:
            raise RuntimeError("CLIENT_SETTINGS_RUNTIME_REVISION_MISMATCH")
        if _RUNTIME_MUTATION_ORDERS.get(key) != mutation_order:
            raise RuntimeError("CLIENT_SETTINGS_RUNTIME_REVISION_MISMATCH")
        return
    _RUNTIME_SETTINGS[key] = deepcopy(value)
    _RUNTIME_KEY_REVISIONS[key] = settings_revision
    if mutation_order is None:
        _RUNTIME_MUTATION_ORDERS.pop(key, None)
    else:
        _RUNTIME_MUTATION_ORDERS[key] = mutation_order


def publish_client_settings_patch(
    settings: dict[str, Any],
    *,
    settings_revision: int,
) -> None:
    """Apply exact server-owned values after their serialized durable commit."""

    if not isinstance(settings, dict):
        raise TypeError("CLIENT_SETTINGS_RUNTIME_PAYLOAD_INVALID")
    revision = _require_settings_revision(settings_revision)
    global _RUNTIME_REVISION
    with _RUNTIME_LOCK:
        if _RUNTIME_SETTINGS is None:
            raise RuntimeError("CLIENT_SETTINGS_RUNTIME_UNAVAILABLE")
        for key, value in settings.items():
            if not isinstance(key, str) or not key:
                raise TypeError("CLIENT_SETTINGS_RUNTIME_PAYLOAD_INVALID")
            _publish_client_setting_value_locked(
                key,
                value,
                mutation_order=None,
                settings_revision=revision,
            )
        _RUNTIME_REVISION = max(_RUNTIME_REVISION or 0, revision)


def publish_client_settings_mutations(
    mutations: dict[str, dict[str, Any]],
    *,
    settings_revision: int,
) -> None:
    """Project browser mutations under their canonical per-key durable order."""

    if not isinstance(mutations, dict):
        raise TypeError("CLIENT_SETTINGS_RUNTIME_MUTATIONS_INVALID")
    revision = _require_settings_revision(settings_revision)
    prepared: dict[str, tuple[Any, tuple[int, str, int]]] = {}
    for key, mutation in mutations.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(mutation, dict)
            or set(mutation) != {"outcome", "value", "changed_at_ms", "writer_id", "sequence"}
            or mutation.get("outcome") not in {"applied", "replayed", "rejected"}
        ):
            raise TypeError("CLIENT_SETTINGS_RUNTIME_MUTATIONS_INVALID")
        changed_at_ms = mutation.get("changed_at_ms")
        writer_id = mutation.get("writer_id")
        sequence = mutation.get("sequence")
        if (
            not isinstance(changed_at_ms, int)
            or isinstance(changed_at_ms, bool)
            or not 0 < changed_at_ms <= _MAX_SAFE_SETTINGS_REVISION
            or not isinstance(writer_id, str)
            or not writer_id
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 < sequence <= _MAX_SAFE_SETTINGS_REVISION
        ):
            raise ValueError("CLIENT_SETTINGS_RUNTIME_MUTATIONS_INVALID")
        prepared[key] = (
            deepcopy(mutation.get("value")),
            (changed_at_ms, writer_id, sequence),
        )
    global _RUNTIME_REVISION
    with _RUNTIME_LOCK:
        if _RUNTIME_SETTINGS is None:
            raise RuntimeError("CLIENT_SETTINGS_RUNTIME_UNAVAILABLE")
        for key, (value, order) in prepared.items():
            _publish_client_setting_value_locked(
                key,
                value,
                mutation_order=order,
                settings_revision=revision,
            )
        _RUNTIME_REVISION = max(_RUNTIME_REVISION or 0, revision)


def reconcile_client_settings_snapshot(
    settings: dict[str, Any],
    mutation_orders: dict[str, dict[str, Any]],
    settings_revision: int,
) -> None:
    """Reconcile values and absences from one authoritative PostgreSQL snapshot."""

    prepared_settings, prepared_orders = _prepare_settings_snapshot(settings, mutation_orders)
    revision = _require_settings_revision(settings_revision)
    global _RUNTIME_REVISION, _RUNTIME_SNAPSHOT_REVISION
    with _RUNTIME_LOCK:
        if _RUNTIME_SETTINGS is None:
            raise RuntimeError("CLIENT_SETTINGS_RUNTIME_UNAVAILABLE")
        for key in set(_RUNTIME_SETTINGS).union(prepared_settings):
            current_revision = _RUNTIME_KEY_REVISIONS.get(
                key,
                _RUNTIME_SNAPSHOT_REVISION if _RUNTIME_SNAPSHOT_REVISION is not None else -1,
            )
            if revision < current_revision:
                continue
            if key in prepared_settings:
                _publish_client_setting_value_locked(
                    key,
                    prepared_settings[key],
                    mutation_order=prepared_orders.get(key),
                    settings_revision=revision,
                )
                continue
            if revision == current_revision and key in _RUNTIME_SETTINGS:
                raise RuntimeError("CLIENT_SETTINGS_RUNTIME_REVISION_MISMATCH")
            _RUNTIME_SETTINGS.pop(key, None)
            _RUNTIME_MUTATION_ORDERS.pop(key, None)
            _RUNTIME_KEY_REVISIONS[key] = revision
        _RUNTIME_REVISION = max(_RUNTIME_REVISION or 0, revision)
        _RUNTIME_SNAPSHOT_REVISION = max(_RUNTIME_SNAPSHOT_REVISION or 0, revision)


def client_settings_snapshot() -> dict[str, Any]:
    """Return the initialized non-blocking settings projection for runtime consumers."""

    with _RUNTIME_LOCK:
        if _RUNTIME_SETTINGS is None:
            raise RuntimeError("CLIENT_SETTINGS_RUNTIME_UNAVAILABLE")
        return deepcopy(_RUNTIME_SETTINGS)


def selected_instruments(instrument_ids: Sequence[str] | None = None) -> list[dict]:
    store = _postgres_store()
    if store is None:
        raise RuntimeError("WATCHLIST_STORAGE_REQUIRED")
    watchlist, _watchlist_version = store.read_watchlist_snapshot()
    if not isinstance(watchlist, list):
        raise RuntimeError("WATCHLIST_SNAPSHOT_INVALID")
    selected = reference_selected_instruments(
        watchlist,
        instrument_ids,
    )
    return selected
