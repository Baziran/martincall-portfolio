from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body

from aef_terminal.ui.reference_actions import (
    ReferenceActionDeps,
    add_reference_instrument_payload,
    current_watchlist_response,
    delete_reference_instrument_payload,
    refresh_watchlist_current_contracts,
    reorder_reference_instruments_payload,
    search_reference_instruments_payload,
    update_watchlist_presentation_payload,
)


from aef_terminal.indicators.registry import indicator_connection_manifest, indicator_manifest


@dataclass(frozen=True)
class ReferenceRouterDeps:
    reconcile_client_settings: Callable[[dict[str, Any], dict[str, dict[str, Any]], int], None]
    reconcile_gex_scheduler_settings: Callable[[Any, bool, int], dict[str, Any]]
    reconcile_option_target_caps_settings: Callable[[Any, bool, int], dict[str, float]]
    data_provider_catalog: Callable[[], list[dict[str, Any]]]
    search_provider_instruments: Callable[[str, str], list[dict[str, Any]]]
    bind_provider_instrument: Callable[[str, str], dict[str, Any] | None]
    bind_provider_future_root: Callable[[str, str, dict[str, Any] | None], dict[str, Any] | None]
    store_factory: Callable[[], Any]
    refresh_quote_routes: Callable[..., Any]


def create_reference_router(deps: ReferenceRouterDeps) -> APIRouter:
    router = APIRouter()
    action_deps = ReferenceActionDeps(
        reconcile_client_settings=deps.reconcile_client_settings,
        reconcile_gex_scheduler_settings=deps.reconcile_gex_scheduler_settings,
        reconcile_option_target_caps_settings=deps.reconcile_option_target_caps_settings,
        data_provider_catalog=deps.data_provider_catalog,
        search_provider_instruments=deps.search_provider_instruments,
        bind_provider_instrument=deps.bind_provider_instrument,
        bind_provider_future_root=deps.bind_provider_future_root,
        store_factory=deps.store_factory,
        refresh_quote_routes=deps.refresh_quote_routes,
    )

    @router.get("/api/instruments")
    def instruments() -> dict[str, Any]:
        return current_watchlist_response(action_deps)

    @router.post("/api/instruments")
    def add_instrument(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return add_reference_instrument_payload(action_deps, payload)

    @router.post("/api/instruments/search")
    def search_instruments(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return search_reference_instruments_payload(action_deps, payload)

    @router.post("/api/instruments/contracts/refresh")
    def refresh_instrument_contracts() -> dict[str, Any]:
        return refresh_watchlist_current_contracts(action_deps)

    @router.delete("/api/instruments/{instrument_id}")
    def delete_instrument(instrument_id: str) -> dict[str, Any]:
        return delete_reference_instrument_payload(action_deps, instrument_id)

    @router.put("/api/instruments/{instrument_id}/presentation")
    def update_instrument_presentation(
        instrument_id: str, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        return update_watchlist_presentation_payload(action_deps, instrument_id, payload)

    @router.put("/api/instruments/order")
    def reorder_instruments(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return reorder_reference_instruments_payload(action_deps, payload)

    @router.get("/api/data-providers")
    def data_providers() -> list[dict[str, Any]]:
        return deps.data_provider_catalog()

    @router.get("/api/indicators/registry")
    def indicator_registry() -> dict[str, dict[str, Any]]:
        return indicator_manifest()

    @router.get("/api/indicators/connections")
    def indicator_connections(
        group: str | None = None, stage: str | None = None
    ) -> dict[str, dict[str, Any]]:
        normalized_group = str(group or "").strip()
        if normalized_group not in {"primary", "structure", "post_decision"}:
            normalized_group = ""
        normalized_stage = str(stage or "").strip()
        if normalized_stage not in {"primary", "structure", "setup", "post_decision"}:
            normalized_stage = ""
        return indicator_connection_manifest(
            group=None if normalized_stage else normalized_group or None,
            stage=normalized_stage or None,
        )

    return router
