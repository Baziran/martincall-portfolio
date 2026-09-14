from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
import json
import logging
from typing import Any, Literal, Protocol

from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_contract import InstrumentRoute, QuoteSnapshotRead
from aef_terminal.runtime.async_tasks import settle_physical_task
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric
from aef_terminal.runtime.telemetry import log_structured_error
from aef_terminal.ui.runtime.quote_stream import QuoteRouteSnapshot

_QUOTE_ORCHESTRATOR_BREAKER_FAILURES = 3
_QUOTE_ORCHESTRATOR_BREAKER_COOLDOWN_SECONDS = 15.0

QuoteCleanupStage = Literal[
    "cache_read",
    "provider_poll",
    "provider_close",
    "snapshot_persist",
    "subscription_cleanup",
    "subscription_sync",
]
QuoteCleanupErrorHandler = Callable[[QuoteCleanupStage, str, BaseException], None]


@dataclass
class QuoteOrchestratorState:
    last_sync_key: str = ""


@dataclass(frozen=True)
class QuoteProviderPoller:
    provider: str
    fetch_quotes: Callable[
        [list[dict[str, Any]]],
        Awaitable[tuple[dict[str, Any], str]],
    ]
    close: Callable[[], Awaitable[None]]


class QuoteCacheReader(Protocol):
    def __call__(
        self,
        routes: Sequence[InstrumentRoute],
        *,
        after_sequence: int | None = None,
    ) -> QuoteSnapshotRead: ...


@dataclass(frozen=True)
class QuoteOrchestratorRuntimeDeps:
    state: QuoteOrchestratorState
    logger: logging.Logger
    startup_delay_seconds: float
    poll_seconds: float
    snapshot_persist_seconds: float
    server_sleeping: Callable[[], bool]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    provider_pollers: tuple[QuoteProviderPoller, ...]
    quote_subscription_generation: Callable[[], str]
    sync_ibkr_subscriptions: Callable[[list[Any], float], Awaitable[Any]]
    cached_quotes: QuoteCacheReader
    store_quote_cache: Callable[[dict[tuple[str, str], Any] | None, str], None]
    persist_quote_snapshots: Callable[[dict[str, Any], Sequence[dict[str, Any]]], Awaitable[None]]


@dataclass
class _SimpleCircuitBreaker:
    label: str
    failures: int = 0
    open_until: float = 0.0
    last_error: str = ""

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def fail(self, now: float, error: BaseException) -> None:
        self.failures += 1
        self.last_error = str(error)
        if self.failures >= _QUOTE_ORCHESTRATOR_BREAKER_FAILURES:
            self.open_until = now + _QUOTE_ORCHESTRATOR_BREAKER_COOLDOWN_SECONDS

    def ok(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.last_error = ""


async def run_quote_orchestrator_runtime(deps: QuoteOrchestratorRuntimeDeps) -> None:
    state = deps.state

    def get_last_sync_key() -> str:
        return state.last_sync_key

    def set_last_sync_key(value: str) -> None:
        state.last_sync_key = value

    def on_cleanup_error(
        stage: QuoteCleanupStage,
        provider: str,
        exc: BaseException,
    ) -> None:
        log_structured_error(
            deps.logger,
            provider=provider,
            symbol="",
            interval="",
            range_="",
            op=f"quote_orchestrator_{stage}",
            error=exc,
            stage=stage,
        )

    def on_loop_error(exc: BaseException) -> None:
        log_structured_error(
            deps.logger,
            provider="quote",
            symbol="",
            interval="",
            range_="",
            op="quote_orchestrator_loop",
            error=exc,
        )

    await run_quote_orchestrator_loop(
        startup_delay_seconds=deps.startup_delay_seconds,
        poll_seconds=deps.poll_seconds,
        snapshot_persist_seconds=deps.snapshot_persist_seconds,
        server_sleeping=deps.server_sleeping,
        get_last_sync_key=get_last_sync_key,
        set_last_sync_key=set_last_sync_key,
        quote_route_snapshot=deps.quote_route_snapshot,
        provider_pollers=deps.provider_pollers,
        quote_subscription_generation=deps.quote_subscription_generation,
        sync_ibkr_subscriptions=deps.sync_ibkr_subscriptions,
        cached_quotes=deps.cached_quotes,
        store_quote_cache=deps.store_quote_cache,
        persist_quote_snapshots=deps.persist_quote_snapshots,
        on_cleanup_error=on_cleanup_error,
        on_loop_error=on_loop_error,
    )


async def run_quote_orchestrator_loop(
    *,
    startup_delay_seconds: float,
    poll_seconds: float,
    server_sleeping: Callable[[], bool],
    get_last_sync_key: Callable[[], str],
    set_last_sync_key: Callable[[str], None],
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot],
    quote_subscription_generation: Callable[[], str],
    sync_ibkr_subscriptions: Callable[[list[Any], float], Awaitable[Any]],
    cached_quotes: QuoteCacheReader,
    store_quote_cache: Callable[[dict[tuple[str, str], Any] | None, str], None],
    persist_quote_snapshots: Callable[[dict[str, Any], Sequence[dict[str, Any]]], Awaitable[None]],
    on_cleanup_error: QuoteCleanupErrorHandler,
    on_loop_error: Callable[[BaseException], None],
    snapshot_persist_seconds: float,
    provider_pollers: Sequence[QuoteProviderPoller] = (),
    provider_poll_seconds: float = 1.0,
) -> None:
    ibkr_breaker = _SimpleCircuitBreaker("ibkr_read_cached_quotes")
    ibkr_sync_breaker = _SimpleCircuitBreaker("ibkr_sync_quote_subscriptions")
    provider_breakers = {
        str(poller.provider or "").strip().lower(): _SimpleCircuitBreaker(
            f"{str(poller.provider or '').strip().lower()}_live_quotes"
        )
        for poller in provider_pollers
        if str(poller.provider or "").strip()
    }
    provider_poll_task: asyncio.Task[tuple[dict[tuple[str, str], Any], list[str]]] | None = None
    subscription_sync_task: asyncio.Task[Any] | None = None
    subscription_sync_stage: QuoteCleanupStage = "subscription_sync"
    subscription_sync_target_key = ""
    snapshot_persist_task: asyncio.Task[None] | None = None
    provider_quote_cache: dict[tuple[str, str], Any] = {}
    provider_warning_cache: list[str] = []
    subscription_warning = ""
    next_provider_poll_at = 0.0
    next_snapshot_persist_at = 0.0
    publication_seconds = max(float(poll_seconds), 0.01)
    slow_provider_seconds = max(float(provider_poll_seconds), 1.0)
    persistence_seconds = max(float(snapshot_persist_seconds), 1.0)
    set_metric("quote_cache_publication_interval_seconds", publication_seconds)
    next_cycle_delay = max(float(startup_delay_seconds), 0.0)
    selected_snapshot: QuoteRouteSnapshot | None = None
    selected_routes: dict[str, InstrumentRoute] = {}
    selected_sleeping: bool | None = None
    quote_routes: tuple[InstrumentRoute, ...] = ()
    quote_items: list[dict[str, Any]] = []
    provider_instrument_sets: list[tuple[QuoteProviderPoller, list[dict[str, Any]]]] = []
    provider_route_keys: set[tuple[str, str]] = set()
    persist_instruments: list[dict[str, Any]] = []
    selected_subscription_generation: str | None = None
    desired_sync_key = ""
    ibkr_after_sequence: int | None = None
    ibkr_quote_cache: dict[str, dict[str, Any]] = {}
    force_publication = True
    while True:
        loop = asyncio.get_running_loop()
        cycle_started: float | None = None
        cycle_status = "ok"
        try:
            # Keep the inter-cycle wait inside the lifecycle cancellation
            # boundary so every exit settles already-started durable work.
            await asyncio.sleep(next_cycle_delay)
            cycle_started = loop.time()
            if snapshot_persist_task is not None and snapshot_persist_task.done():
                try:
                    snapshot_persist_task.result()
                except Exception as exc:
                    on_cleanup_error("snapshot_persist", "storage", exc)
                snapshot_persist_task = None
                next_snapshot_persist_at = loop.time() + persistence_seconds
            if subscription_sync_task is not None and subscription_sync_task.done():
                try:
                    subscription_sync_task.result()
                except Exception as exc:
                    ibkr_sync_breaker.fail(loop.time(), exc)
                    on_cleanup_error(subscription_sync_stage, "ibkr", exc)
                    action = (
                        "cleanup" if subscription_sync_stage == "subscription_cleanup" else "sync"
                    )
                    subscription_warning = f"IBKR quote subscription {action} failed: {exc}"
                else:
                    ibkr_sync_breaker.ok()
                    set_last_sync_key(subscription_sync_target_key)
                    subscription_warning = ""
                subscription_sync_task = None

            sleeping = server_sleeping()
            snapshot = quote_route_snapshot()
            if snapshot.generation <= 0:
                raise RuntimeError("QUOTE_ROUTE_SNAPSHOT_REQUIRED")
            selection_changed = snapshot is not selected_snapshot or sleeping != selected_sleeping
            if selection_changed:
                selected_routes = {
                    instrument_id: (
                        selected_routes[instrument_id]
                        if selected_snapshot is not None
                        and instrument_id in selected_routes
                        and entry is selected_snapshot.entries.get(instrument_id)
                        else route_instrument(entry.wire_instrument())
                    )
                    for instrument_id, entry in snapshot.entries.items()
                    if not sleeping
                }
                routes = sorted(
                    selected_routes.values(),
                    key=lambda route: (route.instrument_id, route.fingerprint),
                )
                quote_routes = tuple(route for route in routes if route.provider == "ibkr")
                quote_items = [route.instrument for route in quote_routes]
                provider_instrument_sets = [
                    (
                        poller,
                        [route.instrument for route in routes if route.provider == poller.provider],
                    )
                    for poller in provider_pollers
                ]
                poll_providers = {poller.provider for poller in provider_pollers}
                provider_route_keys = {
                    (route.instrument_id, route.fingerprint)
                    for route in routes
                    if route.provider in poll_providers
                }
                persist_instruments = [
                    route.instrument
                    for route in routes
                    if route.provider == "ibkr" or route.provider in poll_providers
                ]
                provider_quote_cache = {
                    key: value
                    for key, value in provider_quote_cache.items()
                    if key in provider_route_keys
                }
                ibkr_quote_cache = {}
                ibkr_after_sequence = None
                selected_snapshot = snapshot
                selected_sleeping = sleeping
                force_publication = True
            cache_updates: dict[tuple[str, str], dict[str, Any]] = {}
            if not provider_route_keys:
                provider_warning_cache = []
            if provider_poll_task is not None and provider_poll_task.done():
                try:
                    completed_map, provider_warning_cache = provider_poll_task.result()
                    provider_quote_cache.update(
                        (key, value)
                        for key, value in completed_map.items()
                        if key in provider_route_keys
                    )
                    cache_updates.update(
                        (key, value)
                        for key, value in completed_map.items()
                        if key in provider_route_keys
                    )
                except Exception as exc:
                    on_cleanup_error("provider_poll", "quote", exc)
                    provider_warning_cache = [f"Provider quote poll failed: {exc}"]
                provider_poll_task = None
                next_provider_poll_at = loop.time() + slow_provider_seconds
            now = loop.time()
            if (
                not sleeping
                and provider_poll_task is None
                and provider_route_keys
                and now >= next_provider_poll_at
            ):
                provider_poll_task = asyncio.create_task(
                    _fetch_provider_quotes(
                        provider_instrument_sets,
                        provider_breakers,
                        on_cleanup_error,
                    ),
                    name="provider-quote-poll",
                )
                next_provider_poll_at = now + slow_provider_seconds

            if quote_items:
                raw_subscription_generation = quote_subscription_generation()
                if (
                    not isinstance(raw_subscription_generation, str)
                    or not raw_subscription_generation
                ):
                    raise ValueError("QUOTE_SUBSCRIPTION_GENERATION_REQUIRED")
                if (
                    selection_changed
                    or raw_subscription_generation != selected_subscription_generation
                ):
                    desired_sync_key = ",".join(
                        _quote_item_sync_key(
                            route,
                            subscription_generation=raw_subscription_generation,
                        )
                        for route in quote_routes
                    )
                    selected_subscription_generation = raw_subscription_generation
            else:
                desired_sync_key = ""
            if desired_sync_key != get_last_sync_key() and subscription_sync_task is None:
                if ibkr_sync_breaker.allow(now):
                    subscription_sync_stage = (
                        "subscription_sync" if desired_sync_key else "subscription_cleanup"
                    )
                    subscription_sync_target_key = desired_sync_key
                    subscription_sync_task = asyncio.create_task(
                        sync_ibkr_subscriptions(
                            quote_items,
                            timeout=1.0,
                        ),
                        name="ibkr-quote-subscription-sync",
                    )
                else:
                    subscription_warning = (
                        "IBKR quote subscription sync breaker is open; "
                        "preserving active subscriptions."
                    )

            warning_bits: list[str] = []
            if subscription_warning:
                warning_bits.append(subscription_warning)
            live_map: dict[str, Any] = {}
            next_ibkr_sequence = ibkr_after_sequence
            if not sleeping and quote_items:
                if ibkr_breaker.allow(now):
                    try:
                        read = cached_quotes(quote_routes, after_sequence=ibkr_after_sequence)
                        live_map = read.quotes if read.changed else ibkr_quote_cache
                        next_ibkr_sequence = read.sequence
                        if read.changed:
                            cache_updates.update(
                                ((route.instrument_id, route.fingerprint), quote)
                                for route in quote_routes
                                if (quote := live_map.get(route.fingerprint))
                                and (
                                    force_publication
                                    or quote != ibkr_quote_cache.get(route.fingerprint)
                                )
                            )
                        ibkr_breaker.ok()
                    except Exception as exc:
                        ibkr_breaker.fail(now, exc)
                        on_cleanup_error("cache_read", "ibkr", exc)
                        warning_bits.append(f"IBKR quote snapshot read failed: {exc}")
                else:
                    warning_bits.append(
                        "IBKR quote snapshot read breaker is open; "
                        "using last published quote snapshot."
                    )
            if force_publication:
                cache_updates.update(
                    ((route.instrument_id, route.fingerprint), quote)
                    for route in quote_routes
                    if (quote := live_map.get(route.fingerprint))
                )
                cache_updates.update(provider_quote_cache)
            if sleeping:
                warning_bits.insert(
                    0,
                    "Server sleeping: live quote polling paused.",
                )
                cache_updates = {}
            merged_warning = "; ".join(
                bit for bit in [*warning_bits, *provider_warning_cache] if bit
            )
            unchanged_snapshot = not sleeping and bool(live_map or provider_quote_cache)
            store_quote_cache(
                cache_updates if cache_updates or not unchanged_snapshot else None,
                merged_warning,
            )
            if live_map:
                ibkr_quote_cache = live_map
            ibkr_after_sequence = next_ibkr_sequence
            force_publication = False
            increment_metric(
                "quote_cache_publications_total",
                status="sleeping" if sleeping else "ok",
            )

            if not sleeping and snapshot_persist_task is None and now >= next_snapshot_persist_at:
                snapshot_live_map = {
                    **{route_key: dict(quote) for route_key, quote in live_map.items()},
                    **{
                        route_key: dict(quote)
                        for (_instrument_id, route_key), quote in provider_quote_cache.items()
                    },
                }
                snapshot_persist_task = asyncio.create_task(
                    persist_quote_snapshots(
                        snapshot_live_map,
                        persist_instruments,
                    ),
                    name="quote-snapshot-persist",
                )
                next_snapshot_persist_at = now + persistence_seconds
        except asyncio.CancelledError as cancellation:
            if provider_poll_task is not None:
                provider_poll_task.cancel()
                await asyncio.gather(provider_poll_task, return_exceptions=True)
            await _close_provider_pollers(provider_pollers, on_cleanup_error)
            if subscription_sync_task is not None:
                subscription_sync_task.cancel()
                await asyncio.gather(
                    subscription_sync_task,
                    return_exceptions=True,
                )
            if snapshot_persist_task is not None:
                persist_outcome = await settle_physical_task(
                    snapshot_persist_task,
                    deferred_cancellation=cancellation,
                )
                persist_error = persist_outcome.error
                if persist_outcome.task_cancelled:
                    persist_error = RuntimeError("QUOTE_SNAPSHOT_PERSIST_TASK_CANCELLED")
                if persist_error is not None:
                    on_cleanup_error("snapshot_persist", "storage", persist_error)
                raise persist_outcome.cancellation or cancellation
            raise
        except Exception as exc:
            cycle_status = "error"
            force_publication = True
            ibkr_after_sequence = None
            store_quote_cache({}, str(exc))
            on_loop_error(exc)
            increment_metric("quote_cache_publications_total", status="error")
        finally:
            if cycle_started is not None:
                cycle_seconds = max(loop.time() - cycle_started, 0.0)
                observe_metric(
                    "quote_cache_publication_cycle_seconds",
                    cycle_seconds,
                    status=cycle_status,
                )
        if cycle_started is not None:
            next_cycle_delay = max(
                publication_seconds - (loop.time() - cycle_started),
                0.0,
            )


def _quote_item_sync_key(
    route: InstrumentRoute,
    *,
    subscription_generation: str,
) -> str:
    return json.dumps(
        [
            route.instrument_id,
            route.fingerprint,
            subscription_generation,
        ],
        separators=(",", ":"),
    )


async def _fetch_provider_quotes(
    provider_instrument_sets: Sequence[tuple[QuoteProviderPoller, list[dict[str, Any]]]],
    provider_breakers: dict[str, _SimpleCircuitBreaker],
    on_cleanup_error: QuoteCleanupErrorHandler,
) -> tuple[dict[tuple[str, str], Any], list[str]]:
    qualified_live_map: dict[tuple[str, str], Any] = {}
    warnings: list[str] = []
    pending: list[tuple[str, _SimpleCircuitBreaker, list[dict[str, Any]], Awaitable[Any]]] = []
    for poller, instruments in provider_instrument_sets:
        if not instruments:
            continue
        provider = str(poller.provider or "").strip().lower()
        if not provider:
            continue
        breaker = provider_breakers.setdefault(
            provider, _SimpleCircuitBreaker(f"{provider}_live_quotes")
        )
        now = asyncio.get_running_loop().time()
        if not breaker.allow(now):
            warnings.append(f"{provider.upper()} quote fetch breaker is open.")
            continue
        pending.append((provider, breaker, instruments, poller.fetch_quotes(instruments)))
    results = await asyncio.gather(
        *(pending_item[3] for pending_item in pending),
        return_exceptions=True,
    )
    for (provider, breaker, instruments, _request), outcome in zip(pending, results, strict=True):
        if isinstance(outcome, BaseException):
            breaker.fail(asyncio.get_running_loop().time(), outcome)
            on_cleanup_error("provider_poll", provider, outcome)
            provider_map, provider_warning = {}, f"{provider.upper()} quote fetch failed: {outcome}"
        else:
            provider_map, provider_warning = outcome
            breaker.ok()
        for instrument in instruments:
            quote = provider_map.get(route_fingerprint(instrument))
            if isinstance(quote, dict):
                qualified_live_map[
                    (
                        qualified_instrument_id(instrument),
                        route_fingerprint(instrument),
                    )
                ] = dict(quote)
        if provider_warning:
            warnings.append(provider_warning)
    return qualified_live_map, warnings


async def _close_provider_pollers(
    provider_pollers: Sequence[QuoteProviderPoller],
    on_cleanup_error: QuoteCleanupErrorHandler,
) -> None:
    outcomes = await asyncio.gather(
        *(poller.close() for poller in provider_pollers),
        return_exceptions=True,
    )
    for poller, outcome in zip(provider_pollers, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            provider = str(poller.provider or "").strip().lower() or "quote"
            on_cleanup_error("provider_close", provider, outcome)
