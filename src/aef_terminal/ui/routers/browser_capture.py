from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket

from aef_terminal.ui.services.browser_capture import run_browser_capture_socket


@dataclass(frozen=True)
class BrowserCaptureRouterDeps:
    lookup_runtime_instrument: Callable[[str], dict[str, Any]]


def create_browser_capture_router(deps: BrowserCaptureRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/browser-capture")
    async def browser_capture_stream(
        websocket: WebSocket,
        instrument_id: str,
        expected_route_fingerprint: str,
        timeframe: str,
        client_id: str,
    ) -> None:
        """Serve bounded Telegram capture requests to one exact browser tab."""

        await run_browser_capture_socket(
            websocket,
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            timeframe=timeframe,
            client_id=client_id,
            lookup_runtime_instrument=deps.lookup_runtime_instrument,
        )

    return router
