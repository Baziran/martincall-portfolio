from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.settings_contract import SERVER_SLEEP_SETTING_KEY

_LOGGER = logging.getLogger(__name__)

_SERVER_SLEEP_LOCK = threading.Lock()
_SERVER_SLEEPING = False
_SERVER_SLEEP_REASON = ""
_SERVER_SLEEP_CHANGED_AT = datetime.now(tz=UTC)


@dataclass(frozen=True)
class ServerSleepDeps:
    store_factory: Callable[[], Any]
    publish_client_settings_patch: Callable[[dict[str, Any], int], None]


_DEPS: ServerSleepDeps | None = None


def configure_server_sleep_deps(deps: ServerSleepDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> ServerSleepDeps:
    if _DEPS is None:
        raise RuntimeError("server sleep dependencies are not configured")
    return _DEPS


def server_sleep_status() -> dict[str, Any]:
    changed_at = _SERVER_SLEEP_CHANGED_AT
    return {
        "sleeping": bool(_SERVER_SLEEPING),
        "reason": _SERVER_SLEEP_REASON,
        "changed_at": changed_at.isoformat(),
        "duration_seconds": max((datetime.now(tz=UTC) - changed_at).total_seconds(), 0.0),
    }


def server_sleeping() -> bool:
    return bool(_SERVER_SLEEPING)


def persist_server_sleep_status(status: dict[str, Any]) -> None:
    store = _deps().store_factory()
    if store is None:
        return
    try:
        settings_revision = store.upsert_setting("client", SERVER_SLEEP_SETTING_KEY, status)
        _deps().publish_client_settings_patch(
            {SERVER_SLEEP_SETTING_KEY: status},
            settings_revision=settings_revision,
        )
    except Exception as exc:
        _LOGGER.warning("Failed to persist server sleep state: %s", exc)


def set_server_sleeping(
    sleeping: bool,
    reason: str = "",
    *,
    persist: bool = True,
    clear_quote_wanted: Callable[[], None] | None = None,
) -> dict[str, Any]:
    global _SERVER_SLEEPING, _SERVER_SLEEP_REASON, _SERVER_SLEEP_CHANGED_AT
    with _SERVER_SLEEP_LOCK:
        if _SERVER_SLEEPING != bool(sleeping):
            _SERVER_SLEEP_CHANGED_AT = datetime.now(tz=UTC)
        _SERVER_SLEEPING = bool(sleeping)
        _SERVER_SLEEP_REASON = reason.strip() or ("manual" if sleeping else "wake")
        if sleeping and clear_quote_wanted is not None:
            clear_quote_wanted()
        status = server_sleep_status()
        if persist:
            persist_server_sleep_status(status)
        return status
