from __future__ import annotations

from typing import Any

from aef_terminal.ui.system_status import SystemStatusDeps, SystemStatusService

_SYSTEM_STATUS_SERVICE: SystemStatusService | None = None


def configure_system_status_runtime(deps: SystemStatusDeps) -> None:
    global _SYSTEM_STATUS_SERVICE
    _SYSTEM_STATUS_SERVICE = SystemStatusService(deps)


def _system_status_service() -> SystemStatusService:
    if _SYSTEM_STATUS_SERVICE is None:
        raise RuntimeError("system status runtime is not configured")
    return _SYSTEM_STATUS_SERVICE


def app_runtime_status() -> dict[str, Any]:
    return _system_status_service().app_runtime_status()


def system_status_snapshot(*, details: bool = True) -> dict[str, Any]:
    return _system_status_service().snapshot(details=details)
