from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from typing import Any

from fastapi import APIRouter, Query, WebSocket

from aef_terminal.ui.routers.error_payloads import build_stream_status_payload
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision, QuoteRouteSnapshot
from aef_terminal.ui.services.chart_stream_ws import ChartStreamWsDeps, run_chart_stream
from aef_terminal.ui.services.gex_stream_ws import GexStreamWsDeps, run_gex_stream
from aef_terminal.ui.services.option_board_stream_ws import (
    OptionBoardWsDeps,
    run_option_board_stream,
)
from aef_terminal.ui.services.quote_stream_ws import QuoteStreamWsDeps, run_quote_stream


@dataclass(frozen=True)
class StreamRouterDeps:
    logger: Logger
    store_factory: Callable[[], Any]
    apply_provider_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    set_quote_stream_wanted: Callable[[int, Any], None]
    quote_stream_started: Callable[[], int]
    quote_stream_finished: Callable[[], int]
    screener_bases: Callable[[list[dict[str, Any]], str], dict[str, dict]]
    screener_rows: Callable[..., list[dict[str, Any]]]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]
    record_quote_execution_snapshots: Callable[[list[dict[str, Any]]], None]
    fast_indicator_snapshots: Callable[
        [list[dict[str, Any]], str],
        list[dict[str, Any]] | None,
    ]
    quote_stream_seconds: float
    quote_stream_cleanup_grace_seconds: float
    websocket_heartbeat_seconds: float
    chart_stream_key: Callable[[str, str, str, str], tuple[str, str, str, str]]
    chart_stream_started: Callable[[tuple[str, str, str, str]], int]
    chart_stream_finished: Callable[[tuple[str, str, str, str]], int]
    stream_bar_payload: Callable[..., dict[str, Any]]
    stream_bar_signature: Callable[[dict[str, Any] | None], tuple]
    parse_iso_ts: Callable[[str | None], datetime | None]
    parse_stream_ts: Callable[[dict[str, Any] | None], datetime | None]
    chart_recovery_snapshot: Callable[
        [list[Any], datetime | None, str, dict[str, Any], tuple[str, ...]],
        Any,
    ]
    record_chart_execution_snapshot: Callable[..., None]
    chart_stream_poll_seconds: float
    lookup_runtime_instrument: Callable[[str], dict[str, Any]]


def create_stream_router(deps: StreamRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/quotes")
    async def quote_stream(
        websocket: WebSocket,
        routes: str,
        interval: str = "5m",
    ) -> None:
        """Push provider-owned quote snapshots from shared persistent subscriptions."""

        await run_quote_stream(
            websocket,
            routes,
            interval,
            QuoteStreamWsDeps(
                logger=deps.logger,
                store_factory=deps.store_factory,
                apply_provider_runtime_settings_async=deps.apply_provider_runtime_settings_async,
                server_sleeping=deps.server_sleeping,
                quote_route_snapshot=deps.quote_route_snapshot,
                set_quote_stream_wanted=deps.set_quote_stream_wanted,
                quote_stream_started=deps.quote_stream_started,
                quote_stream_finished=deps.quote_stream_finished,
                screener_bases=deps.screener_bases,
                screener_rows=deps.screener_rows,
                quote_cache_for_instruments=deps.quote_cache_for_instruments,
                record_quote_execution_snapshots=deps.record_quote_execution_snapshots,
                fast_indicator_snapshots=deps.fast_indicator_snapshots,
                quote_stream_seconds=deps.quote_stream_seconds,
                quote_stream_cleanup_grace_seconds=deps.quote_stream_cleanup_grace_seconds,
                websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
                stream_status_payload=build_stream_status_payload,
            ),
        )

    @router.websocket("/ws/chart")
    async def chart_stream(
        websocket: WebSocket,
        instrument_id: str,
        expected_route_fingerprint: str,
        interval: str = "5m",
        range_: str = Query("5d", alias="range"),
        since_ts: str = "",
        consumer_role: str = "primary",
        tail_bars: int | None = None,
    ) -> None:
        """Push chart bars from the local DB and enqueue broker repair in the backend."""
        await run_chart_stream(
            websocket,
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            interval=interval,
            range_=range_,
            since_ts=since_ts,
            consumer_role=consumer_role,
            tail_bars=tail_bars,
            deps=ChartStreamWsDeps(
                logger=deps.logger,
                store_factory=deps.store_factory,
                apply_provider_runtime_settings_async=deps.apply_provider_runtime_settings_async,
                server_sleeping=deps.server_sleeping,
                websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
                chart_stream_key=deps.chart_stream_key,
                chart_stream_started=deps.chart_stream_started,
                chart_stream_finished=deps.chart_stream_finished,
                stream_bar_payload=deps.stream_bar_payload,
                stream_bar_signature=deps.stream_bar_signature,
                parse_iso_ts=deps.parse_iso_ts,
                parse_stream_ts=deps.parse_stream_ts,
                chart_recovery_snapshot=deps.chart_recovery_snapshot,
                record_chart_execution_snapshot=deps.record_chart_execution_snapshot,
                chart_stream_poll_seconds=deps.chart_stream_poll_seconds,
                lookup_runtime_instrument=deps.lookup_runtime_instrument,
            ),
        )

    @router.websocket("/ws/gex")
    async def gex_stream(
        websocket: WebSocket,
        instrument_id: str,
        expected_route_fingerprint: str,
    ) -> None:
        """Push compact frames from the backend-owned GEX subscription."""
        await run_gex_stream(
            websocket,
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            deps=GexStreamWsDeps(
                apply_provider_runtime_settings_async=deps.apply_provider_runtime_settings_async,
                server_sleeping=deps.server_sleeping,
                store_factory=deps.store_factory,
                websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
            ),
        )

    @router.websocket("/ws/options-board")
    async def option_board_stream(
        websocket: WebSocket,
        instrument_id: str,
        expected_route_fingerprint: str,
        expiry_mode: str = "hybrid",
    ) -> None:
        """Push a bounded exact-contract option price board on demand."""

        await run_option_board_stream(
            websocket,
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            expiry_mode=expiry_mode,
            deps=OptionBoardWsDeps(
                apply_provider_runtime_settings_async=(deps.apply_provider_runtime_settings_async),
                server_sleeping=deps.server_sleeping,
                websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
                lookup_runtime_instrument=deps.lookup_runtime_instrument,
                store_factory=deps.store_factory,
            ),
        )

    return router
