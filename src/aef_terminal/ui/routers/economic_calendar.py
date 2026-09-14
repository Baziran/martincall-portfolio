from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException

from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.ui.routers.error_payloads import build_error_payload


@dataclass(frozen=True)
class EconomicCalendarRouterDeps:
    snapshot: Callable[[], dict[str, Any]]


def create_economic_calendar_router(deps: EconomicCalendarRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/economic-calendar")
    async def api_economic_calendar() -> dict[str, Any]:
        try:
            return await run_physical_thread_call(deps.snapshot)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=build_error_payload(
                    code="ECONOMIC_CALENDAR_UNAVAILABLE",
                    category="economic_calendar",
                    retryable=True,
                    error=exc,
                ),
            ) from exc

    return router
