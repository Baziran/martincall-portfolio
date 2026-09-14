from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.async_tasks import (
    await_cancellation_deferred_task,
    run_physical_thread_call,
)
from aef_terminal.runtime.timeframes import (
    ChartHistoryRangeError,
    chart_history_range_window,
)
from aef_terminal.ui.runtime.chart_stream import chart_stream
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.routers.error_payloads import build_stream_status_payload
from aef_terminal.ui.services.chart_stream_coordinator import (
    CHART_STREAM_LIVE_TAIL_BARS,
    ChartStreamCoordinator,
    ChartStreamCoordinatorDeps,
    compact_chart_consumer_queue,
    require_secondary_chart_tail_bars,
)
from aef_terminal.ui.services.chart_stream_messages import (
    CHART_STREAM_CONSUMER_ROLE_PRIMARY,
    CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES,
    chart_status_payload,
    require_chart_stream_consumer_role,
)
from aef_terminal.ui.services.websocket_send import send_stream_json


_CHART_COORDINATOR_RELEASE_TASKS: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class ChartStreamWsDeps:
    logger: Logger
    store_factory: Callable[[], Any]
    apply_provider_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
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
    lookup_runtime_instrument: Callable[[str], dict[str, Any]] = lookup_runtime_instrument


async def _release_chart_stream_coordinator(
    coordinator_key: tuple[str, str, str],
    coordinator: ChartStreamCoordinator,
    consumer: Any,
    *,
    grace_seconds: float,
) -> None:
    released = await chart_stream.release_coordinator(
        coordinator_key,
        coordinator,
        consumer,
        grace_seconds=grace_seconds,
    )
    if released:
        await coordinator.stop()


async def run_chart_stream(
    websocket: WebSocket,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    interval: str = "5m",
    range_: str = "5d",
    since_ts: str = "",
    consumer_role: str = CHART_STREAM_CONSUMER_ROLE_PRIMARY,
    tail_bars: int | None = None,
    deps: ChartStreamWsDeps,
) -> None:
    """Attach one socket to the qualified route/timeframe realtime coordinator."""

    await websocket.accept()
    try:
        resolved_consumer_role = require_chart_stream_consumer_role(consumer_role)
        resolved_tail_bars = (
            require_secondary_chart_tail_bars(tail_bars)
            if resolved_consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
            else CHART_STREAM_LIVE_TAIL_BARS
        )
    except ValueError as exc:
        await send_stream_json(
            websocket,
            {
                **build_stream_status_payload(
                    status_type="chart_status",
                    source="chart:consumer-profile",
                    code=str(exc).split(" ", 1)[0],
                    category="chart_consumer",
                    retryable=False,
                    error=exc,
                    retry_in_seconds=0,
                    instrument_id=instrument_id,
                    route_fingerprint=expected_route_fingerprint,
                    interval=interval,
                    range=range_,
                    requires_resubscribe=False,
                ),
                "consumer_role": str(consumer_role or "").strip().lower(),
                "retry_in_seconds": 0,
            },
            "chart",
        )
        return
    try:
        requested_window = chart_history_range_window(
            "1d"
            if resolved_consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
            else range_,
            interval,
        )
    except ChartHistoryRangeError as exc:
        await send_stream_json(
            websocket,
            {
                **build_stream_status_payload(
                    status_type="chart_status",
                    source="chart:history-request",
                    code=exc.code,
                    category="chart_history",
                    retryable=False,
                    error=exc,
                    retry_in_seconds=0.1,
                    instrument_id=instrument_id,
                    route_fingerprint=expected_route_fingerprint,
                    interval=interval,
                    range=range_,
                    requires_resubscribe=False,
                ),
                "consumer_role": resolved_consumer_role,
            },
            "chart",
        )
        return
    interval = str(interval or "").strip().lower()
    range_ = (
        str(range_ or "").strip().lower() or "tail"
        if resolved_consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
        else requested_window.range_key
    )
    instrument = await run_physical_thread_call(deps.lookup_runtime_instrument, instrument_id)
    route = route_instrument(instrument)
    expected = expected_route_fingerprint if isinstance(expected_route_fingerprint, str) else ""
    if route.fingerprint != expected:
        await send_stream_json(
            websocket,
            {
                **build_stream_status_payload(
                    status_type="chart_status",
                    source="chart:route",
                    code="CHART_STREAM_ROUTE_CHANGED",
                    category="routing",
                    retryable=True,
                    error="Chart provider contract changed; reconnecting.",
                    retry_in_seconds=deps.chart_stream_poll_seconds,
                    instrument_id=route.instrument_id,
                    route_fingerprint=expected,
                    symbol=route.instrument_key,
                    interval=interval,
                    range=range_,
                    requires_resubscribe=True,
                ),
                "actual_route_fingerprint": route.fingerprint,
                "consumer_role": resolved_consumer_role,
            },
            "chart",
        )
        return
    if deps.server_sleeping():
        await send_stream_json(
            websocket,
            chart_status_payload(
                source="server:sleep",
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                interval=interval,
                range_=range_,
                message="Server sleeping: chart stream paused.",
                retry_in_seconds=0,
                consumer_role=resolved_consumer_role,
            ),
            "chart",
        )
        return
    if not route.adapter.capabilities.chart_stream:
        await send_stream_json(
            websocket,
            chart_status_payload(
                source=f"{route.provider}:chart-stream-disabled",
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                interval=interval,
                range_=range_,
                message=f"{route.provider.upper()} does not expose the server chart stream.",
                retry_in_seconds=0,
                consumer_role=resolved_consumer_role,
            ),
            "chart",
        )
        return

    coordinator_key = chart_stream.coordinator_key(
        route.instrument_id,
        interval,
        route.fingerprint,
    )

    def create_coordinator(generation: int) -> ChartStreamCoordinator:
        return ChartStreamCoordinator(
            route=route,
            interval=str(interval),
            generation=generation,
            deps=ChartStreamCoordinatorDeps(
                logger=deps.logger,
                store_factory=deps.store_factory,
                apply_provider_runtime_settings_async=deps.apply_provider_runtime_settings_async,
                server_sleeping=deps.server_sleeping,
                websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
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

    coordinator, consumer, created = chart_stream.acquire_coordinator(
        coordinator_key,
        create_coordinator,
        range_=range_,
        since_ts=since_ts,
        live_tail_bars=resolved_tail_bars,
        consumer_role=resolved_consumer_role,
        tail_bars=(
            resolved_tail_bars
            if resolved_consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
            else None
        ),
    )
    if created:
        coordinator.start()
    client_key = deps.chart_stream_key(route.instrument_id, interval, range_, route.fingerprint)
    tracked_primary_client = resolved_consumer_role == CHART_STREAM_CONSUMER_ROLE_PRIMARY
    if tracked_primary_client:
        deps.chart_stream_started(client_key)
    try:
        while True:
            if coordinator.task is not None and coordinator.task.done() and consumer.queue.empty():
                if not coordinator.task.cancelled():
                    error = coordinator.task.exception()
                    if error is not None:
                        if not coordinator.terminal_error_reported:
                            coordinator.terminal_error_reported = True
                            deps.logger.warning(
                                "chart coordinator failed provider=%s instrument_id=%s interval=%s: %s",
                                route.provider,
                                route.instrument_id,
                                interval,
                                error,
                            )
                        await send_stream_json(
                            websocket,
                            {
                                **build_stream_status_payload(
                                    status_type="chart_status",
                                    source="chart:coordinator",
                                    code="CHART_STREAM_COORDINATOR_ERROR",
                                    category="stream",
                                    retryable=True,
                                    error=error,
                                    retry_in_seconds=deps.chart_stream_poll_seconds,
                                    instrument_id=route.instrument_id,
                                    route_fingerprint=route.fingerprint,
                                    symbol=route.instrument_key,
                                    interval=interval,
                                    range=range_,
                                ),
                                "consumer_role": resolved_consumer_role,
                            },
                            "chart",
                        )
                return
            try:
                payload = await asyncio.wait_for(
                    consumer.queue.get(),
                    timeout=max(float(deps.websocket_heartbeat_seconds) * 2.0, 1.0),
                )
            except TimeoutError:
                continue
            bars = None
            if payload.get("type") == "chart_bars":
                bars = payload.get("bars")
                if not isinstance(bars, list):
                    raise RuntimeError("CHART_STREAM_FRAME_BARS_INVALID")
            await send_stream_json(websocket, payload, "chart")
            if bars is not None:
                consumer.tracker.remember(bars)
    except RuntimeError, WebSocketDisconnect:
        return
    finally:
        if tracked_primary_client:
            deps.chart_stream_finished(client_key)
        release_task = asyncio.create_task(
            _release_chart_stream_coordinator(
                coordinator_key,
                coordinator,
                consumer,
                grace_seconds=min(max(float(deps.chart_stream_poll_seconds), 0.0), 0.5),
            ),
            name=f"chart-coordinator-release:{route.provider}:{interval}",
        )
        _CHART_COORDINATOR_RELEASE_TASKS.add(release_task)
        release_task.add_done_callback(_CHART_COORDINATOR_RELEASE_TASKS.discard)
        await await_cancellation_deferred_task(
            release_task,
            task_cancelled_error="CHART_COORDINATOR_RELEASE_TASK_CANCELLED",
        )


__all__ = [
    "ChartStreamWsDeps",
    "compact_chart_consumer_queue",
    "run_chart_stream",
]
