from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body

from aef_terminal.alerts.telegram import (
    send_telegram_message,
    telegram_config_status,
)
from aef_terminal.settings_contract import (
    TELEGRAM_INTERACTIVE_SETTING_KEY,
    invalid_server_setting_value_keys,
)
from aef_terminal.ui.routers.error_payloads import build_action_error_response


@dataclass(frozen=True)
class TelegramRouterDeps:
    set_telegram_interactive_enabled: Callable[[bool], dict[str, Any]]
    paper_telegram_feed_status: Callable[[], dict[str, Any]]
    telegram_interactive_status: Callable[[], dict[str, Any]]
    store_factory: Callable[[], Any]
    now_iso: Callable[[], str]


def create_telegram_router(deps: TelegramRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/alerts/telegram/status")
    def api_telegram_status() -> dict[str, Any]:
        return {
            "checked_at": deps.now_iso(),
            **telegram_config_status(),
            "interactive": deps.telegram_interactive_status(),
            "paper_feed": deps.paper_telegram_feed_status(),
        }

    @router.put("/api/alerts/telegram/interactive")
    def api_telegram_interactive_update(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if invalid_server_setting_value_keys({TELEGRAM_INTERACTIVE_SETTING_KEY: payload}):
            return {
                **build_action_error_response(
                    code="TELEGRAM_INTERACTIVE_SETTING_INVALID",
                    category="telegram",
                    retryable=False,
                    error="interactive setting must contain exactly enabled:boolean",
                ),
                "interactive": deps.telegram_interactive_status(),
            }
        enabled = payload["enabled"]
        store = deps.store_factory()
        if store is None:
            return {
                **build_action_error_response(
                    code="TELEGRAM_STORAGE_NOT_CONFIGURED",
                    category="telegram",
                    retryable=False,
                    error="storage not configured",
                ),
                "interactive": deps.telegram_interactive_status(),
            }
        try:
            return {
                "ok": True,
                "checked_at": deps.now_iso(),
                "interactive": deps.set_telegram_interactive_enabled(enabled),
            }
        except Exception as exc:
            return {
                **build_action_error_response(
                    code="TELEGRAM_INTERACTIVE_SAVE_ERROR",
                    category="telegram",
                    retryable=True,
                    error=exc,
                ),
                "interactive": deps.telegram_interactive_status(),
            }

    @router.post("/api/alerts/telegram/test")
    def api_telegram_test(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        payload = payload or {}
        message = str(
            payload.get("message")
            or "MartinCall Telegram test: backend notifications are connected."
        )
        return {"checked_at": deps.now_iso(), **send_telegram_message(message, force=True)}

    return router
