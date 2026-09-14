from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.runtime.ibkr_settings import (
    ibkr_runtime_settings_snapshot,
    publish_ibkr_runtime_port,
)
from aef_terminal.settings_contract import IBKR_PORT_SETTING_KEY, SERVER_SLEEP_SETTING_KEY
from aef_terminal.ui.runtime.server_sleep import set_server_sleeping
from aef_terminal.ui.runtime_services import coerce_ibkr_port


@dataclass(frozen=True)
class IbkrRuntimeDeps:
    load_client_settings_snapshot: Callable[
        [],
        tuple[dict[str, Any], dict[str, dict[str, Any]], int, int],
    ]
    publish_client_settings_snapshot: Callable[
        [dict[str, Any], dict[str, dict[str, Any]], int],
        None,
    ]
    clear_quote_wanted: Callable[[], None]


_DEPS: IbkrRuntimeDeps | None = None


def configure_ibkr_runtime_deps(deps: IbkrRuntimeDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> IbkrRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("IBKR runtime dependencies are not configured")
    return _DEPS


def apply_ibkr_runtime_settings(settings: dict | None = None) -> dict[str, Any]:
    """Apply persisted UI connection settings to the current backend process."""

    if settings is None:
        return ibkr_runtime_settings_snapshot().payload()
    port = coerce_ibkr_port(settings.get(IBKR_PORT_SETTING_KEY))
    snapshot = ibkr_runtime_settings_snapshot()
    snapshot = publish_ibkr_runtime_port(port if port is not None else snapshot.port)
    return snapshot.payload()


async def apply_ibkr_runtime_settings_async(settings: dict | None = None) -> dict[str, Any]:
    if settings is None:
        return apply_ibkr_runtime_settings()
    return apply_ibkr_runtime_settings(settings)


def apply_persisted_ibkr_runtime_settings() -> None:
    deps = _deps()
    settings, mutation_orders, _mutation_changed_at_ms, settings_revision = (
        deps.load_client_settings_snapshot()
    )
    deps.publish_client_settings_snapshot(settings, mutation_orders, settings_revision)
    apply_ibkr_runtime_settings(settings)
    persisted_sleep = settings.get(SERVER_SLEEP_SETTING_KEY)
    if isinstance(persisted_sleep, dict) and persisted_sleep.get("sleeping"):
        reason = str(persisted_sleep.get("reason") or "persisted sleep")
        set_server_sleeping(True, reason, persist=False, clear_quote_wanted=deps.clear_quote_wanted)
