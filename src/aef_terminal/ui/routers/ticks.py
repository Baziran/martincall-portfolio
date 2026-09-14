from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.ui.tick_services import (
    tick_context_response,
    tick_history_response,
    tick_live_post_response,
    tick_profile_history_response,
)


@dataclass(frozen=True)
class TickRouterDeps:
    store_factory: Callable[[], Any]
    live_status: Callable[[], dict[str, Any]]
    set_live_enabled: Callable[..., Awaitable[dict[str, Any]]]


def create_tick_router(deps: TickRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/ticks/context")
    def tick_context(
        instrument_id: str,
        expected_route_fingerprint: str,
        start: str | None = None,
        end: str | None = None,
        bucket: str = "1 minute",
        price_step: float | None = None,
        levels: str | None = None,
        limit: int = 160,
        debug: bool = False,
    ) -> dict[str, Any]:
        return tick_context_response(
            deps.store_factory(),
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            start=start,
            end=end,
            bucket=bucket,
            price_step=price_step,
            levels=levels,
            limit=limit,
            live_status=deps.live_status,
            include_telemetry=debug,
        )

    @router.get("/api/ticks/history")
    def tick_history(
        instrument_id: str,
        expected_route_fingerprint: str,
        start: str | None = None,
        end: str | None = None,
        timeframe: str = "5m",
        limit: int = 5000,
    ) -> dict[str, Any]:
        return tick_history_response(
            deps.store_factory(),
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            start=start,
            end=end,
            timeframe=timeframe,
            limit=limit,
        )

    @router.get("/api/ticks/profile-history")
    def tick_profile_history(
        instrument_id: str,
        expected_route_fingerprint: str,
        start: str | None = None,
        end: str | None = None,
        timeframe: str = "5m",
        price_step: float | None = None,
        limit: int = 50000,
    ) -> dict[str, Any]:
        return tick_profile_history_response(
            deps.store_factory(),
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            start=start,
            end=end,
            timeframe=timeframe,
            price_step=price_step,
            limit=limit,
        )

    @router.get("/api/ticks/live")
    def api_ticks_live_status() -> dict[str, Any]:
        return {"ok": True, "live": deps.live_status()}

    @router.post("/api/ticks/live")
    async def api_ticks_live(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"enabled", "instrument_id", "expected_route_fingerprint"}
            or not isinstance(payload.get("enabled"), bool)
        ):
            return tick_live_post_response(
                {
                    "enabled": False,
                    "running": False,
                    "status": "error",
                    "last_error": (
                        "TICK_LIVE_PAYLOAD_INVALID: expected exact enabled:boolean, "
                        "instrument_id and expected_route_fingerprint fields"
                    ),
                },
                error_code="TICK_LIVE_PAYLOAD_INVALID",
                retryable=False,
            )
        data = payload
        enabled = data["enabled"]
        try:
            instrument_id = require_exact_identity_text(
                data.get("instrument_id", ""),
                field="instrument_id",
                allow_empty=not enabled,
            )
            route_fingerprint = require_exact_identity_text(
                data.get("expected_route_fingerprint", ""),
                field="route_fingerprint",
                allow_empty=not enabled,
            )
        except ValueError as exc:
            return tick_live_post_response(
                {
                    "enabled": False,
                    "running": False,
                    "status": "error",
                    "last_error": str(exc),
                },
                error_code="TICK_LIVE_PAYLOAD_INVALID",
                retryable=False,
            )
        try:
            live = await deps.set_live_enabled(
                enabled,
                instrument_id,
                route_fingerprint,
            )
        except Exception as exc:
            live = {
                "enabled": False,
                "running": False,
                "status": "error",
                "last_error": str(exc).strip() or "tick live failed to start",
                "error": {
                    "code": "TICK_LIVE_START_FAILED",
                    "retryable": True,
                    "message": str(exc).strip() or "tick live failed to start",
                },
            }
        live_error = live.get("error")
        if (
            isinstance(live_error, dict)
            and isinstance(live_error.get("code"), str)
            and bool(live_error["code"])
            and isinstance(live_error.get("retryable"), bool)
        ):
            error_code = live_error["code"]
            retryable = live_error["retryable"]
        else:
            error_code = None
            retryable = None
        return {
            **tick_live_post_response(
                live,
                error_code=error_code,
                retryable=retryable,
            ),
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
        }

    return router
