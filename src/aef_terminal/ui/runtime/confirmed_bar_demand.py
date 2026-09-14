from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
)
from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.data.providers import route_instrument
from aef_terminal.indicators.registry import (
    enabled_confirmed_bar_context_requests,
)
from aef_terminal.runtime.chart_events import (
    chart_bars_updated_generation,
    wait_for_chart_bars_updated,
)
from aef_terminal.runtime.timeframes import interval_minutes
from aef_terminal.ui.runtime.chart_stream import chart_stream
from aef_terminal.ui.services.chart_stream_coordinator import (
    ChartStreamCoordinator,
    ChartStreamCoordinatorDeps,
)
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
    market_analysis_effects,
)


CONFIRMED_BAR_DEMAND_RANGE = "1d"
RESEARCH_CAPTURE_PARENT_HISTORY_BARS = 512
CONFIRMED_BAR_CALLBACK_RETRY_INITIAL_SECONDS = 0.1
CONFIRMED_BAR_CALLBACK_RETRY_MAX_SECONDS = 5.0

_LOGGER = logging.getLogger("aef_terminal.ui.confirmed_bar_demand")


@dataclass(frozen=True)
class _DesiredConfirmedBarStream:
    route: InstrumentRoute
    timeframe: str
    history_bars: int
    consumers: tuple[str, ...]


@dataclass
class _ConfirmedBarStreamLease:
    coordinator_key: tuple[str, str, str]
    client_key: tuple[str, str, str, str]
    coordinator: ChartStreamCoordinator
    consumer: Any
    history_bars: int
    last_generation: int
    watch_task: asyncio.Task[None] | None = None


class ConfirmedBarDemandRuntime:
    """Own demand-only consumers of the canonical chart coordinator."""

    def __init__(self) -> None:
        self._deps: ChartStreamCoordinatorDeps | None = None
        self._leases: dict[
            tuple[str, str, str],
            _ConfirmedBarStreamLease,
        ] = {}
        self._lock = asyncio.Lock()
        self._on_generation: (
            Callable[
                [str, str, str, int],
                Awaitable[Any],
            ]
            | None
        ) = None

    def configure(
        self,
        deps: ChartStreamCoordinatorDeps,
        *,
        on_generation: Callable[
            [str, str, str, int],
            Awaitable[Any],
        ]
        | None = None,
    ) -> None:
        self._deps = deps
        self._on_generation = on_generation

    def _require_deps(self) -> ChartStreamCoordinatorDeps:
        if self._deps is None:
            raise RuntimeError("confirmed bar demand runtime dependencies are not configured")
        return self._deps

    @staticmethod
    def _desired(
        payloads: tuple[dict[str, Any], ...],
    ) -> dict[tuple[str, str, str], _DesiredConfirmedBarStream]:
        desired: dict[
            tuple[str, str, str],
            _DesiredConfirmedBarStream,
        ] = {}
        for payload in payloads:
            instrument = payload.get("instrument")
            indicator_params = payload.get("indicator_params")
            if not isinstance(instrument, dict):
                continue
            try:
                route = route_instrument(
                    instrument,
                    expected_source=str(payload.get("source") or ""),
                )
                instrument_id = require_exact_identity_text(
                    payload.get("instrument_id"),
                    field="instrument_id",
                )
                fingerprint = require_exact_identity_text(
                    payload.get("route_fingerprint"),
                    field="route_fingerprint",
                )
                effects = market_analysis_effects(payload)
            except TypeError, ValueError:
                continue
            if (
                route.instrument_id != instrument_id
                or route.fingerprint != fingerprint
                or not route.adapter.capabilities.chart_stream
            ):
                continue
            parent_timeframe = str(payload.get("interval") or "").strip()
            if not parent_timeframe:
                continue
            try:
                parent_minutes = interval_minutes(parent_timeframe)
            except TypeError, ValueError:
                continue
            if effects == MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE:
                parent_key = chart_stream.coordinator_key(
                    route.instrument_id,
                    parent_timeframe,
                    route.fingerprint,
                )
                desired[parent_key] = _DesiredConfirmedBarStream(
                    route=route,
                    timeframe=parent_timeframe,
                    history_bars=RESEARCH_CAPTURE_PARENT_HISTORY_BARS,
                    consumers=("research_capture",),
                )
            for timeframe, request_consumers in enabled_confirmed_bar_context_requests(
                indicator_params if isinstance(indicator_params, dict) else {}
            ).items():
                try:
                    context_minutes = interval_minutes(timeframe)
                except TypeError, ValueError:
                    continue
                if context_minutes >= parent_minutes:
                    continue
                key = chart_stream.coordinator_key(
                    route.instrument_id,
                    timeframe,
                    route.fingerprint,
                )
                history_bars = max(
                    request.history_bars for _indicator_id, request in request_consumers
                )
                indicator_ids = tuple(
                    sorted({indicator_id for indicator_id, _request in request_consumers})
                )
                current = desired.get(key)
                desired[key] = _DesiredConfirmedBarStream(
                    route=route,
                    timeframe=timeframe,
                    history_bars=max(
                        history_bars,
                        current.history_bars if current is not None else 0,
                    ),
                    consumers=tuple(
                        sorted(
                            {
                                *indicator_ids,
                                *(current.consumers if current else ()),
                            }
                        )
                    ),
                )
        return desired

    async def _release(
        self,
        key: tuple[str, str, str],
        lease: _ConfirmedBarStreamLease,
    ) -> None:
        watch_task = lease.watch_task
        if watch_task is not None and not watch_task.done():
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
        released = await chart_stream.release_coordinator(
            lease.coordinator_key,
            lease.coordinator,
            lease.consumer,
        )
        chart_stream.internal_stream_finished(lease.client_key)
        if released:
            await lease.coordinator.stop()
        self._leases.pop(key, None)

    async def _watch_canonical_generations(
        self,
        key: tuple[str, str, str],
        lease: _ConfirmedBarStreamLease,
    ) -> None:
        instrument_id, route_fingerprint, timeframe = key
        pending_generation: int | None = None
        retry_seconds = CONFIRMED_BAR_CALLBACK_RETRY_INITIAL_SECONDS
        while True:
            if pending_generation is None:
                generation, _events = await wait_for_chart_bars_updated(
                    timeframe,
                    route_fingerprint,
                    lease.last_generation,
                    60.0,
                    instrument_id=instrument_id,
                )
            else:
                generation = pending_generation
            if generation <= lease.last_generation:
                pending_generation = None
                retry_seconds = CONFIRMED_BAR_CALLBACK_RETRY_INITIAL_SECONDS
                continue
            callback = self._on_generation
            if callback is None:
                lease.last_generation = generation
                pending_generation = None
                retry_seconds = CONFIRMED_BAR_CALLBACK_RETRY_INITIAL_SECONDS
                continue
            try:
                await callback(
                    instrument_id,
                    route_fingerprint,
                    timeframe,
                    generation,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                pending_generation = generation
                _LOGGER.exception(
                    "confirmed bar generation callback failed key=%s generation=%s",
                    key,
                    generation,
                )
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(
                    retry_seconds * 2.0,
                    CONFIRMED_BAR_CALLBACK_RETRY_MAX_SECONDS,
                )
                continue
            lease.last_generation = generation
            pending_generation = None
            retry_seconds = CONFIRMED_BAR_CALLBACK_RETRY_INITIAL_SECONDS

    async def reconcile(
        self,
        payloads: tuple[dict[str, Any], ...],
    ) -> None:
        deps = self._require_deps()
        desired = self._desired(payloads)
        async with self._lock:
            for key, lease in tuple(self._leases.items()):
                task = lease.coordinator.task
                if key not in desired or (task is not None and task.done()):
                    await self._release(key, lease)

            for key, demand in desired.items():
                existing = self._leases.get(key)
                if existing is not None:
                    existing.coordinator.ensure_live_tail_bars(demand.history_bars)
                    existing.history_bars = max(
                        existing.history_bars,
                        demand.history_bars,
                    )
                    continue

                def create_coordinator(
                    generation: int,
                    *,
                    requested: _DesiredConfirmedBarStream = demand,
                ) -> ChartStreamCoordinator:
                    return ChartStreamCoordinator(
                        route=requested.route,
                        interval=requested.timeframe,
                        generation=generation,
                        deps=deps,
                        live_tail_bars=requested.history_bars,
                    )

                initial_generation = chart_bars_updated_generation(
                    demand.timeframe,
                    demand.route.fingerprint,
                    instrument_id=demand.route.instrument_id,
                )
                coordinator, consumer, created = chart_stream.acquire_coordinator(
                    key,
                    create_coordinator,
                    range_=CONFIRMED_BAR_DEMAND_RANGE,
                    since_ts="",
                    deliver=False,
                    live_tail_bars=demand.history_bars,
                )
                client_key = chart_stream.stream_key(
                    demand.route.instrument_id,
                    demand.timeframe,
                    CONFIRMED_BAR_DEMAND_RANGE,
                    demand.route.fingerprint,
                )
                chart_stream.internal_stream_started(client_key)
                lease = _ConfirmedBarStreamLease(
                    coordinator_key=key,
                    client_key=client_key,
                    coordinator=coordinator,
                    consumer=consumer,
                    history_bars=demand.history_bars,
                    last_generation=initial_generation,
                )
                self._leases[key] = lease
                if self._on_generation is not None:
                    lease.watch_task = asyncio.create_task(
                        self._watch_canonical_generations(key, lease),
                        name=(
                            f"confirmed-bar-generation:{demand.route.provider}:{demand.timeframe}"
                        ),
                    )
                if created:
                    coordinator.start()

    async def shutdown(self) -> None:
        async with self._lock:
            for key, lease in tuple(self._leases.items()):
                try:
                    await self._release(key, lease)
                except Exception:
                    _LOGGER.exception(
                        "confirmed bar demand release failed key=%s",
                        key,
                    )
            self._leases.clear()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "streams": len(self._leases),
            "leases": [
                {
                    "instrument_id": key[0],
                    "route_fingerprint": key[1],
                    "timeframe": key[2],
                    "history_bars": lease.history_bars,
                }
                for key, lease in self._leases.items()
            ],
        }


confirmed_bar_demand = ConfirmedBarDemandRuntime()
