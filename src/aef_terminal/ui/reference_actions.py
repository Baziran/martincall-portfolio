from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.data.instrument_identity import (
    current_futures_contract_id,
    futures_root,
    instrument_is_futures_root,
    instrument_key,
    instrument_provider,
    provider_contract_id,
    provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    require_exact_instrument_id_sequence,
    require_provider_identity,
)
from aef_terminal.data.futures_lifecycle import (
    current_futures_contract_record,
    futures_route_transition,
    resolve_and_persist_current_contract,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.settings_contract import require_watchlist_display_mode
from aef_terminal.ui.reference_services import (
    materialize_watchlist_instruments,
    materialize_watchlist_instrument,
    queue_watchlist_session_refresh,
)
from aef_terminal.ui.routers.error_payloads import build_action_error_response
from aef_terminal.watchlist_contract import WatchlistOrderError


@dataclass(frozen=True)
class ReferenceActionDeps:
    reconcile_client_settings: Callable[[dict[str, Any], dict[str, dict[str, Any]], int], None]
    reconcile_gex_scheduler_settings: Callable[[Any, bool, int], dict[str, Any]]
    reconcile_option_target_caps_settings: Callable[[Any, bool, int], dict[str, float]]
    data_provider_catalog: Callable[[], list[dict[str, Any]]]
    search_provider_instruments: Callable[[str, str], list[dict[str, Any]]]
    bind_provider_instrument: Callable[[str, str], dict[str, Any] | None]
    bind_provider_future_root: Callable[[str, str, dict[str, Any] | None], dict[str, Any] | None]
    store_factory: Callable[[], Any]
    refresh_quote_routes: Callable[..., Any]


def reference_storage_unavailable(**extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code="REFERENCE_STORAGE_NOT_CONFIGURED",
        category="reference",
        retryable=False,
        error="storage not configured",
        **extra,
    )


def reference_validation_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code=code,
        category="reference",
        retryable=False,
        error=message,
        **extra,
    )


def _watchlist_store(deps: ReferenceActionDeps) -> Any | None:
    return deps.store_factory()


def _current_watchlist_snapshot(
    store: Any,
) -> tuple[list[dict[str, Any]], int]:
    rows, snapshot_version_value = store.read_watchlist_snapshot()
    if not isinstance(rows, list):
        raise ValueError("Persisted watchlist snapshot rows must be a list")
    if not isinstance(snapshot_version_value, int) or isinstance(snapshot_version_value, bool):
        raise ValueError("Persisted watchlist snapshot version must be an integer")
    snapshot_version = snapshot_version_value
    if snapshot_version < 0:
        raise ValueError("Persisted watchlist snapshot version must be non-negative")
    validated_rows: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("Persisted watchlist row must be an object")
        if (
            item.get("watchlist_version") is not None
            and int(item["watchlist_version"]) != snapshot_version
        ):
            raise ValueError("Persisted watchlist row version must match its snapshot version")
        validated_rows.append(dict(item))
    materialized = materialize_watchlist_instruments(validated_rows)
    for materialized_item in materialized:
        require_provider_identity(materialized_item)
        if instrument_is_futures_root(materialized_item) and not current_futures_contract_id(
            materialized_item
        ):
            existing_session = materialized_item.get("session")
            materialized_item["session"] = {
                **(existing_session if isinstance(existing_session, dict) else {}),
                "provider": instrument_provider(materialized_item),
                "calendar": "unknown",
                "status": "unknown",
                "code": "FUTURES_CURRENT_CONTRACT_UNRESOLVED",
            }
    return materialized, snapshot_version


def current_watchlist(store: Any) -> tuple[list[dict[str, Any]], int]:
    return _current_watchlist_snapshot(store)


def current_watchlist_response(deps: ReferenceActionDeps) -> dict[str, Any]:
    try:
        store = _watchlist_store(deps)
        if store is None:
            return reference_storage_unavailable()
        rows, watchlist_version = _current_watchlist_snapshot(store)
        return {
            "ok": True,
            "items": rows,
            "watchlist_version": watchlist_version,
        }
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_WATCHLIST_READ_ERROR",
            category="reference",
            retryable=True,
            error=exc,
        )


def refresh_watchlist_current_contracts(
    deps: ReferenceActionDeps, *, providers: set[str] | None = None
) -> dict[str, Any]:
    try:
        store = _watchlist_store(deps)
        if store is None:
            return reference_storage_unavailable()
        watchlist, _watchlist_version = current_watchlist(store)
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_WATCHLIST_READ_ERROR",
            category="reference",
            retryable=True,
            error=exc,
        )
    updated: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    provider_filter = {item for item in providers or set() if isinstance(item, str) and item}
    for item in watchlist if isinstance(watchlist, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            materialized_item = materialize_watchlist_instrument(dict(item))
        except Exception as exc:
            key = instrument_key(item)
            provider = instrument_provider(item)
            errors.append(
                {
                    "instrument_id": (
                        item.get("instrument_id")
                        if isinstance(item.get("instrument_id"), str)
                        else ""
                    ),
                    "instrument_key": key,
                    "provider": provider,
                    "error": str(exc),
                }
            )
            continue
        root = futures_root(materialized_item)
        provider = instrument_provider(materialized_item)
        key = instrument_key(materialized_item)
        if not provider or not root:
            continue
        if provider_filter and provider not in provider_filter:
            continue
        try:
            bound = resolve_and_persist_current_contract(
                store,
                materialized_item,
            )
        except Exception as exc:
            errors.append(
                {
                    "instrument_id": qualified_instrument_id(materialized_item),
                    "instrument_key": key,
                    "provider": provider,
                    "error": str(exc),
                }
            )
            continue
        if not bound:
            skipped.append(
                {
                    "instrument_key": key,
                    "provider": provider,
                    "reason": "provider did not return a root binding",
                }
            )
            continue
        contract_key = current_futures_contract_id(bound)
        if not contract_key:
            skipped.append(
                {
                    "instrument_key": key,
                    "provider": provider,
                    "reason": "binding has no current contract",
                }
            )
            continue
        route_instrument(bound, expected_source=provider)
        updated.append({"instrument_key": key, "provider": provider, "contract_key": contract_key})
    if updated:
        # The durable futures table has no membership-version increment. Force
        # one post-batch reconciliation so a retry also repairs a prior
        # publication failure after the provider write already committed.
        deps.refresh_quote_routes(
            store,
            expected_watchlist_version=_watchlist_version,
            route_generation_changed=True,
        )
    return {
        "ok": not errors,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "count": len(updated),
    }


def registered_provider_keys(deps: ReferenceActionDeps) -> set[str]:
    return {
        item["key"]
        for item in deps.data_provider_catalog()
        if isinstance(item.get("key"), str) and item["key"]
    }


def search_reference_instruments_payload(
    deps: ReferenceActionDeps, payload: dict[str, Any]
) -> dict[str, Any]:
    provider = str(payload.get("provider") or "").strip().lower()
    query = str(payload.get("query") or "")
    if not provider:
        return reference_validation_error("REFERENCE_PROVIDER_REQUIRED", "provider is required")
    if provider not in registered_provider_keys(deps):
        return reference_validation_error(
            "REFERENCE_PROVIDER_UNKNOWN", f"Unsupported data provider: {provider}"
        )
    if not query:
        return reference_validation_error("REFERENCE_QUERY_REQUIRED", "instrument name is required")
    seen_identities: set[str] = set()
    matches: list[dict[str, Any]] = []
    raw_candidates: list[dict[str, Any]] = []
    provider_error: Exception | None = None
    try:
        raw_candidates.extend(deps.search_provider_instruments(provider, query))
    except Exception as exc:
        provider_error = exc
    for raw in raw_candidates:
        try:
            qualified = require_provider_identity(raw, provider=provider)
        except ValueError:
            continue
        key = instrument_key(qualified)
        if not key:
            continue
        candidate_identity = qualified_instrument_id(qualified)
        if candidate_identity in seen_identities:
            continue
        display = str(qualified.get("display") or key).strip()
        route_symbol = provider_symbol(qualified, provider)
        contract_id = provider_contract_id(qualified)
        future_root = futures_root(qualified)
        identity = (
            qualified.get("contract_identity")
            if isinstance(qualified.get("contract_identity"), dict)
            else {}
        )
        current_contract = (
            identity.get("current_contract")
            if isinstance(identity.get("current_contract"), dict)
            else {}
        )
        seen_identities.add(candidate_identity)
        matches.append(
            {
                "instrument_id": candidate_identity,
                "instrument_key": key,
                "display": display,
                "name": str(qualified.get("name") or display or key).strip(),
                "provider": provider,
                "provider_symbol": route_symbol,
                "provider_contract_id": contract_id,
                "asset_class": str(
                    qualified.get("asset_class") or identity.get("asset_class") or ""
                ),
                "identity_scope": str(identity.get("identity_scope") or ""),
                "root": future_root,
                "exchange": str(identity.get("exchange") or ""),
                "primary_exchange": str(identity.get("primary_exchange") or ""),
                "currency": str(identity.get("currency") or ""),
                "trading_class": str(identity.get("trading_class") or ""),
                "current_contract": dict(current_contract),
                "con_id": identity.get("con_id") or qualified.get("con_id"),
            }
        )
    if provider_error is not None and not matches:
        return build_action_error_response(
            code="REFERENCE_PROVIDER_SEARCH_ERROR",
            category="reference",
            retryable=True,
            error=provider_error,
            provider=provider,
        )
    limited_matches = matches[:20]
    return {
        "ok": True,
        "provider": provider,
        "query": query,
        "matches": limited_matches,
        "count": len(limited_matches),
    }


def add_reference_instrument_payload(
    deps: ReferenceActionDeps, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        selected_instrument_id = require_exact_identity_text(
            payload.get("instrument_id"), field="instrument_id"
        )
        contract_id = require_exact_identity_text(
            payload.get("provider_contract_id", ""),
            field="provider_contract_id",
            allow_empty=True,
        )
    except ValueError as exc:
        return reference_validation_error(
            "REFERENCE_INSTRUMENT_ID_REQUIRED",
            str(exc),
        )
    raw_provider = payload.get("provider")
    provider = raw_provider if isinstance(raw_provider, str) else ""
    raw_identity_scope = payload.get("identity_scope")
    identity_scope = raw_identity_scope if isinstance(raw_identity_scope, str) else ""
    raw_asset_class = payload.get("asset_class")
    asset_class = raw_asset_class if isinstance(raw_asset_class, str) else ""
    raw_root = payload.get("root", "")
    future_root = (
        raw_root
        if isinstance(raw_root, str) and identity_scope == "root" and asset_class == "future"
        else ""
    )
    if not provider:
        return reference_validation_error("REFERENCE_PROVIDER_REQUIRED", "provider is required")
    if provider and provider not in registered_provider_keys(deps):
        return reference_validation_error(
            "REFERENCE_PROVIDER_UNKNOWN", f"Unsupported data provider: {provider}"
        )
    if not contract_id and not future_root:
        return reference_validation_error(
            "REFERENCE_PROVIDER_CONTRACT_REQUIRED",
            "provider contract id or futures root is required",
            provider=provider,
        )
    try:
        if future_root:
            rebound = deps.bind_provider_future_root(provider, future_root, payload)
            unsupported_identity = f"futures root: {future_root}"
        else:
            rebound = deps.bind_provider_instrument(provider, contract_id)
            unsupported_identity = f"provider contract: {contract_id}"
        if rebound is None:
            return reference_validation_error(
                "REFERENCE_PROVIDER_REBIND_FAILED",
                f"Provider could not confirm selected {unsupported_identity}",
                provider=provider,
                instrument_id=selected_instrument_id,
            )
        qualified = require_provider_identity(dict(rebound), provider=provider)
        rebound_instrument_id = qualified_instrument_id(qualified)
        if rebound_instrument_id != selected_instrument_id:
            return reference_validation_error(
                "REFERENCE_INSTRUMENT_ID_MISMATCH",
                "Provider rebind did not return the selected instrument_id",
                provider=provider,
                instrument_id=selected_instrument_id,
                rebound_instrument_id=rebound_instrument_id,
            )
        instrument = materialize_watchlist_instrument(qualified)
    except ValueError as exc:
        return reference_validation_error(
            "REFERENCE_PROVIDER_REBIND_INVALID",
            str(exc),
            provider=provider,
            instrument_id=selected_instrument_id,
        )
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_PROVIDER_SEARCH_ERROR",
            category="reference",
            retryable=True,
            error=exc,
            provider=provider,
            instrument_id=selected_instrument_id,
        )
    saved_instrument_id = selected_instrument_id
    try:
        store = _watchlist_store(deps)
        if store is None:
            return reference_storage_unavailable()
        current_contract_record = None
        route_transition = None
        if instrument_is_futures_root(instrument):
            current_contract_record = current_futures_contract_record(instrument)
            previous_instrument = store.lookup_instrument(saved_instrument_id)
            if (
                isinstance(previous_instrument, dict)
                and current_futures_contract_id(previous_instrument)
                and current_futures_contract_id(previous_instrument)
                != current_futures_contract_id(instrument)
            ):
                route_transition = futures_route_transition(previous_instrument, instrument)
        selected_watchlist_version = store.select_watchlist_instrument(
            instrument,
            current_futures_contract=current_contract_record,
            route_transition=route_transition,
        )
        if (
            not isinstance(selected_watchlist_version, int)
            or isinstance(selected_watchlist_version, bool)
            or selected_watchlist_version < 0
        ):
            raise RuntimeError("WATCHLIST_SELECTION_VERSION_INVALID")
        saved, watchlist_version = current_watchlist(store)
        if watchlist_version < selected_watchlist_version:
            raise RuntimeError(
                "WATCHLIST_SNAPSHOT_STALE_AFTER_SELECTION "
                f"selected_version={selected_watchlist_version} snapshot_version={watchlist_version}"
            )
        deps.refresh_quote_routes(
            store,
            expected_watchlist_version=watchlist_version,
        )
        saved_item = next(
            (item for item in saved if item.get("instrument_id") == saved_instrument_id),
            None,
        )
        if not isinstance(saved_item, dict):
            raise RuntimeError(
                f"WATCHLIST_INSTRUMENT_READ_AFTER_WRITE_FAILED instrument_id={saved_instrument_id}"
            )
        materialized_item = dict(saved_item)
        queue_watchlist_session_refresh(materialized_item)
        return {
            "ok": True,
            "instrument": materialized_item,
            "count": len(saved),
            "watchlist_version": watchlist_version,
        }
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_INSTRUMENT_SAVE_ERROR",
            category="reference",
            retryable=True,
            error=exc,
        )


def delete_reference_instrument_payload(
    deps: ReferenceActionDeps, instrument_id: str
) -> dict[str, Any]:
    try:
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
    except ValueError as exc:
        return reference_validation_error("REFERENCE_INSTRUMENT_ID_REQUIRED", str(exc))
    try:
        store = _watchlist_store(deps)
        if store is None:
            return reference_storage_unavailable()
        retirement = store.remove_instrument_from_watchlist(identity)
        deps.reconcile_client_settings(
            retirement.client_settings,
            retirement.client_mutation_orders,
            retirement.settings_revision,
        )
        deps.reconcile_gex_scheduler_settings(
            retirement.gex_scheduler_setting,
            retirement.gex_scheduler_present,
            retirement.settings_revision,
        )
        deps.reconcile_option_target_caps_settings(
            retirement.option_target_caps_setting,
            retirement.option_target_caps_present,
            retirement.settings_revision,
        )
        watchlist, watchlist_version = current_watchlist(store)
        deps.refresh_quote_routes(
            store,
            expected_watchlist_version=watchlist_version,
        )
        return {
            "ok": True,
            "count": len(watchlist),
            "watchlist_version": watchlist_version,
        }
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_INSTRUMENT_DELETE_ERROR",
            category="reference",
            retryable=True,
            error=exc,
        )


def update_watchlist_presentation_payload(
    deps: ReferenceActionDeps,
    instrument_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        if not isinstance(payload, dict) or set(payload) != {"display_mode"}:
            raise ValueError("WATCHLIST_PRESENTATION_PAYLOAD_INVALID")
        display_mode = require_watchlist_display_mode(payload.get("display_mode"))
    except ValueError as exc:
        return reference_validation_error(
            "REFERENCE_WATCHLIST_PRESENTATION_INVALID",
            str(exc),
        )
    try:
        store = _watchlist_store(deps)
        if store is None:
            return reference_storage_unavailable()
        presentation = store.set_watchlist_presentation(identity, display_mode)
        return {
            "ok": True,
            "instrument_id": identity,
            "presentation": presentation,
        }
    except KeyError:
        return reference_validation_error(
            "REFERENCE_WATCHLIST_INSTRUMENT_UNKNOWN",
            "instrument_id is not in the current watchlist",
            instrument_id=identity,
        )
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_WATCHLIST_PRESENTATION_SAVE_ERROR",
            category="reference",
            retryable=True,
            error=exc,
            instrument_id=identity,
        )


def reorder_reference_instruments_payload(
    deps: ReferenceActionDeps, payload: dict[str, Any]
) -> dict[str, Any]:
    raw_order = payload.get("order") if isinstance(payload, dict) else None
    if not isinstance(raw_order, list):
        return reference_validation_error("REFERENCE_ORDER_REQUIRED", "order must be a list")
    try:
        requested = list(require_exact_instrument_id_sequence(raw_order, allow_empty=True))
    except (TypeError, ValueError) as exc:
        return reference_validation_error("REFERENCE_ORDER_INVALID", str(exc))
    try:
        seen = set(requested)
        store = _watchlist_store(deps)
        if store is None:
            return reference_storage_unavailable()
        current, _watchlist_version = current_watchlist(store)
        by_key = {qualified_instrument_id(item): item for item in current}
        unknown = [key for key in requested if key not in by_key]
        if unknown:
            return reference_validation_error(
                "REFERENCE_ORDER_UNKNOWN_INSTRUMENT_ID",
                "order contains instrument_ids outside the current watchlist",
                instrument_ids=unknown,
            )
        ordered = [by_key[key] for key in requested]
        ordered.extend(item for item in current if qualified_instrument_id(item) not in seen)
        raw_version = payload.get("expected_version")
        if raw_version in (None, ""):
            return reference_validation_error(
                "REFERENCE_ORDER_VERSION_REQUIRED", "watchlist version is required"
            )
        expected_version = int(raw_version)
        ordered, watchlist_version = store.reorder_watchlist(
            [qualified_instrument_id(item) for item in ordered],
            expected_version=expected_version,
        )
        deps.refresh_quote_routes(
            store,
            expected_watchlist_version=watchlist_version,
        )
        return {
            "ok": True,
            "count": len(ordered),
            "order": [qualified_instrument_id(item) for item in ordered],
            "watchlist_version": watchlist_version,
        }
    except WatchlistOrderError as exc:
        if exc.code == "WATCHLIST_ORDER_STALE":
            return reference_validation_error(
                "REFERENCE_ORDER_STALE",
                "watchlist order changed; reload before reordering",
                expected_version=exc.expected_version,
                current_version=exc.current_version,
            )
        return reference_validation_error("REFERENCE_ORDER_INVALID", exc.code)
    except ValueError as exc:
        return reference_validation_error("REFERENCE_ORDER_INVALID", str(exc))
    except Exception as exc:
        return build_action_error_response(
            code="REFERENCE_INSTRUMENT_ORDER_SAVE_ERROR",
            category="reference",
            retryable=True,
            error=exc,
        )
