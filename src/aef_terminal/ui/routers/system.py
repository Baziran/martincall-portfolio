import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.ui.routers.error_payloads import build_action_error_response, build_error_payload


@dataclass(frozen=True)
class SystemRouterDeps:
    health_status: Callable[[], dict[str, Any]]
    readiness_status: Callable[[], dict[str, Any]]
    system_status: Callable[..., dict[str, Any]]
    memory_status: Callable[..., dict[str, Any]]
    set_server_sleeping: Callable[[bool, str], dict[str, Any]]
    disconnect_ibkr_sessions: Callable[..., dict[str, Any]]
    ibkr_status: Callable[[], dict[str, Any]]
    apply_ibkr_runtime_settings: Callable[[], dict[str, Any]]
    backend_restart_record: Callable[[Request | None, dict[str, Any] | None], dict[str, Any]]
    flush_log_handlers: Callable[[], None]
    now_iso: Callable[[], str]


def create_system_router(deps: SystemRouterDeps) -> APIRouter:
    router = APIRouter()

    def restart_process_after_response() -> None:
        deps.flush_log_handlers()
        os.kill(os.getpid(), signal.SIGTERM)

    @router.get("/health")
    def health() -> dict[str, Any]:
        return deps.health_status()

    @router.get("/api/health")
    def api_health() -> dict[str, Any]:
        return health()

    @router.get("/ready")
    def ready() -> JSONResponse:
        payload = deps.readiness_status()
        return JSONResponse(
            payload,
            status_code=200 if payload.get("status") == "ok" else 503,
        )

    @router.get("/api/ready")
    def api_ready() -> JSONResponse:
        return ready()

    @router.get("/api/system")
    async def api_system(details: bool = False) -> dict[str, Any]:
        try:
            return await run_physical_thread_call(deps.system_status, details=details)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=build_error_payload(
                    code="SYSTEM_STATUS_ERROR",
                    category="system",
                    retryable=True,
                    error=exc,
                ),
            ) from exc

    @router.get("/api/system/memory")
    async def api_system_memory(include_objects: bool = False) -> dict[str, Any]:
        try:
            return await run_physical_thread_call(
                deps.memory_status,
                include_objects=include_objects,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=build_error_payload(
                    code="SYSTEM_MEMORY_ERROR",
                    category="system",
                    retryable=True,
                    error=exc,
                ),
            ) from exc

    @router.post("/api/system/sleep")
    def api_system_sleep(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        payload = payload or {}
        reason = str(payload.get("reason") or "manual sleep").strip()
        sleep = deps.set_server_sleeping(True, reason)
        try:
            ibkr_status = deps.disconnect_ibkr_sessions(timeout=2.0)
            disconnect_error = ""
        except Exception as exc:
            ibkr_status = deps.ibkr_status()
            disconnect_error = str(exc) or exc.__class__.__name__
        return {
            "ok": not bool(disconnect_error),
            "message": "Server sleep enabled. IBKR polling and streams are paused.",
            "sleep": sleep,
            "ibkr": ibkr_status,
            "disconnect_error": disconnect_error,
            **(
                build_action_error_response(
                    code="SYSTEM_SLEEP_DISCONNECT_FAILED",
                    category="system",
                    retryable=True,
                    error=disconnect_error,
                )
                if disconnect_error
                else {}
            ),
        }

    @router.post("/api/system/wake")
    def api_system_wake(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        payload = payload or {}
        reason = str(payload.get("reason") or "manual wake").strip()
        return {
            "ok": True,
            "message": "Server is awake. IBKR polling can resume.",
            "sleep": deps.set_server_sleeping(False, reason),
        }

    @router.post("/api/system/restart")
    def api_system_restart(
        request: Request, payload: dict[str, Any] | None = Body(default=None)
    ) -> JSONResponse:
        record = deps.backend_restart_record(request, payload)
        return JSONResponse(
            {
                "ok": True,
                "restart": record,
                "message": "Backend restart requested. Docker restart policy should bring the container back up.",
            },
            background=BackgroundTask(restart_process_after_response),
        )

    @router.get("/api/ibkr/status")
    def api_ibkr_status() -> dict[str, Any]:
        try:
            deps.apply_ibkr_runtime_settings()
            return {"checked_at": deps.now_iso(), **deps.ibkr_status()}
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=build_error_payload(
                    code="IBKR_STATUS_ERROR",
                    category="broker",
                    retryable=True,
                    error=exc,
                ),
            ) from exc

    return router
