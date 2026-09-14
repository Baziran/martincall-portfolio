from __future__ import annotations

from aef_terminal.ui.routers.telegram import TelegramRouterDeps, create_telegram_router


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_telegram_interactive_storage_unavailable() -> None:
    endpoint = _route_endpoint(
        create_telegram_router(_deps()), "/api/alerts/telegram/interactive", "PUT"
    )
    payload = endpoint({"enabled": True})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TELEGRAM_STORAGE_NOT_CONFIGURED"
    assert "interactive" in payload


def _deps(**overrides):
    defaults = {
        "set_telegram_interactive_enabled": lambda _enabled: {"enabled": False},
        "paper_telegram_feed_status": lambda: {},
        "telegram_interactive_status": lambda: {"enabled": False},
        "store_factory": lambda: None,
        "now_iso": lambda: "2026-01-01T00:00:00+00:00",
    }
    defaults.update(overrides)
    return TelegramRouterDeps(**defaults)


def test_telegram_interactive_rejects_noncanonical_setting() -> None:
    endpoint = _route_endpoint(
        create_telegram_router(_deps()),
        "/api/alerts/telegram/interactive",
        "PUT",
    )

    for request in ({"enabled": "false"}, {"enabled": True, "legacy": False}):
        payload = endpoint(request)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "TELEGRAM_INTERACTIVE_SETTING_INVALID"
