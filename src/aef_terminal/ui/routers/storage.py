import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body

from aef_terminal.ui.alert_commands import execute_alert_command
from aef_terminal.ui.routers.error_payloads import build_action_error_response
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.storage_actions import (
    StorageActionDeps,
    client_storage_payload,
    create_option_target_payload,
    delete_option_target_payload,
    option_target_drawings_payload,
    save_client_settings_payload,
    save_drawings_payload,
    storage_unavailable_response,
    update_option_target_payload,
)

LOGGER = logging.getLogger(__name__)


def _storage_unavailable_response(**extra: Any) -> dict[str, Any]:
    return storage_unavailable_response(**extra)


@dataclass(frozen=True)
class StorageRouterDeps:
    store_factory: Callable[[], Any]
    apply_ibkr_runtime_settings: Callable[[dict[str, Any] | None], dict[str, Any]]
    publish_client_settings_mutations: Callable[[dict[str, dict[str, Any]], int], None]
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]]
    require_unique_price_alerts: Callable[..., list[dict[str, Any]]]


def create_storage_router(deps: StorageRouterDeps) -> APIRouter:
    router = APIRouter()
    action_deps = StorageActionDeps(
        store_factory=deps.store_factory,
        apply_ibkr_runtime_settings=deps.apply_ibkr_runtime_settings,
        publish_client_settings_mutations=deps.publish_client_settings_mutations,
        normalize_drawing_anchors=deps.normalize_drawing_anchors,
        require_unique_price_alerts=deps.require_unique_price_alerts,
        logger=LOGGER,
    )

    @router.get("/api/storage")
    def client_storage(
        instrument_id: str = "",
        route_fingerprint: str = "",
        interval: str = "5m",
        settings: bool = True,
        drawings: bool = True,
        alerts: bool = True,
    ) -> dict[str, Any]:
        try:
            return client_storage_payload(
                action_deps,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                interval=interval,
                settings=settings,
                drawings=drawings,
                alerts=alerts,
            )
        except Exception as exc:
            return build_action_error_response(
                code="STORAGE_READ_ERROR",
                category="storage",
                retryable=True,
                error=exc,
                settings={},
                drawings=[],
                alerts=[],
            )

    @router.put("/api/settings")
    def save_client_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return save_client_settings_payload(action_deps, payload)
        except Exception as exc:
            return build_action_error_response(
                code="SETTINGS_SAVE_ERROR",
                category="storage",
                retryable=True,
                error=exc,
                count=0,
            )

    @router.get("/api/option-targets")
    def option_target_drawings(
        instrument_id: str = "", route_fingerprint: str = ""
    ) -> dict[str, Any]:
        try:
            return option_target_drawings_payload(action_deps, instrument_id, route_fingerprint)
        except Exception as exc:
            return build_action_error_response(
                code="OPTION_TARGETS_READ_ERROR",
                category="storage",
                retryable=True,
                error=exc,
                items=[],
            )

    @router.post("/api/option-targets")
    def create_option_target(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return create_option_target_payload(action_deps, payload)
        except Exception as exc:
            return build_action_error_response(
                code="OPTION_TARGET_CREATE_ERROR",
                category="storage",
                retryable=True,
                error=exc,
            )

    @router.put("/api/option-targets/{target_id}")
    def update_option_target(target_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return update_option_target_payload(action_deps, target_id, payload)
        except Exception as exc:
            return build_action_error_response(
                code="OPTION_TARGET_UPDATE_ERROR",
                category="storage",
                retryable=True,
                error=exc,
            )

    @router.delete("/api/option-targets/{target_id}")
    def delete_option_target(
        target_id: str,
        instrument_id: str,
        route_fingerprint: str,
    ) -> dict[str, Any]:
        try:
            return delete_option_target_payload(
                action_deps,
                target_id,
                instrument_id,
                route_fingerprint,
            )
        except Exception as exc:
            return build_action_error_response(
                code="OPTION_TARGET_DELETE_ERROR",
                category="storage",
                retryable=True,
                error=exc,
            )

    @router.put("/api/drawings")
    def save_drawings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return save_drawings_payload(action_deps, payload)
        except Exception as exc:
            return build_action_error_response(
                code="DRAWINGS_SAVE_ERROR",
                category="storage",
                retryable=True,
                error=exc,
                count=0,
            )

    @router.post("/api/alerts/command")
    def price_alert_command(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        action = str(payload.get("action") or "")
        command_payload = payload.get("payload")
        if not isinstance(command_payload, dict):
            return build_action_error_response(
                code="ALERT_COMMAND_PAYLOAD_INVALID",
                category="storage",
                retryable=False,
                error="payload must be an object",
            )
        return execute_alert_command(
            action,
            command_payload,
            store_factory=deps.store_factory,
            instrument_lookup=lookup_runtime_instrument,
        )

    return router
