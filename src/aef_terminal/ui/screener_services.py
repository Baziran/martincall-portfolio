from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.ui import screener_gap_repair
from aef_terminal.ui.route_selection import (
    parse_route_selection,
    resolve_route_selection,
    route_selection_json,
)
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision
from aef_terminal.ui.screener_bases import (
    load_screener_bases,
)
from aef_terminal.ui.screener_rows import screener_rows
from aef_terminal.ui.screener_trends import load_watchlist_trend_snapshot


@dataclass(frozen=True)
class ScreenerRuntimeDeps:
    store_factory: Callable[[], Any]
    apply_ibkr_runtime_settings_async: Callable[[], Awaitable[Any]]
    selected_instruments: Callable[..., list[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
    set_transient_quote_wanted: Callable[..., None]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]


_DEPS: ScreenerRuntimeDeps | None = None
watchlist_gap_quality = screener_gap_repair.watchlist_gap_quality
request_watchlist_recovery_tail = screener_gap_repair.request_watchlist_recovery_tail
_WatchlistTrendCacheKey = tuple[int, tuple[tuple[str, str], ...]]
_watchlist_trend_cache: dict[_WatchlistTrendCacheKey, dict[str, Any]] = {}
_watchlist_trend_inflight: dict[_WatchlistTrendCacheKey, asyncio.Task[dict[str, Any]]] = {}
_watchlist_trend_lock = asyncio.Lock()
_watchlist_trends_accepting = True
_WATCHLIST_TREND_CACHEABLE_STATUSES = frozenset({"ready", "insufficient_data"})
_WATCHLIST_TREND_CACHE_MAX_ENTRIES = 64
_WATCHLIST_TREND_BUCKET_SECONDS = 5 * 60


def start_screener_trend_runtime() -> None:
    global _watchlist_trends_accepting
    if _watchlist_trend_inflight:
        raise RuntimeError("SCREENER_TREND_START_WITH_ACTIVE_TASKS")
    _watchlist_trend_cache.clear()
    _watchlist_trends_accepting = True


async def shutdown_screener_trend_runtime() -> None:
    global _watchlist_trends_accepting
    _watchlist_trends_accepting = False
    async with _watchlist_trend_lock:
        tasks = tuple(set(_watchlist_trend_inflight.values()))
        for task in tasks:
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    async with _watchlist_trend_lock:
        _watchlist_trend_inflight.clear()
        _watchlist_trend_cache.clear()


def configure_screener_runtime_deps(deps: ScreenerRuntimeDeps) -> None:
    global _DEPS
    _DEPS = deps
    screener_gap_repair.configure_watchlist_gap_repair(store_factory=deps.store_factory)


def _deps() -> ScreenerRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("screener runtime dependencies are not configured")
    return _DEPS


def _postgres_store():
    return _deps().store_factory()


def screener_bases(instruments: list[dict], interval: str) -> dict[str, dict]:
    return load_screener_bases(instruments, interval, _postgres_store())


def _watchlist_trend_payload_cacheable(payload: dict[str, Any]) -> bool:
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return bool(rows) and all(
        isinstance(row, dict) and row.get("status") in _WATCHLIST_TREND_CACHEABLE_STATUSES
        for row in rows
    )


async def _build_watchlist_trend_snapshot(
    key: _WatchlistTrendCacheKey,
    instruments: list[dict[str, Any]],
    now: datetime,
    store_factory: Callable[[], Any],
) -> dict[str, Any]:
    owner = asyncio.current_task()

    def load() -> dict[str, Any]:
        return load_watchlist_trend_snapshot(
            instruments,
            store_factory(),
            now=now,
        )

    try:
        payload = await run_physical_thread_call(load)
        if _watchlist_trend_payload_cacheable(payload):
            async with _watchlist_trend_lock:
                for stale_key in tuple(_watchlist_trend_cache):
                    if stale_key[0] != key[0]:
                        _watchlist_trend_cache.pop(stale_key, None)
                _watchlist_trend_cache[key] = payload
                while len(_watchlist_trend_cache) > _WATCHLIST_TREND_CACHE_MAX_ENTRIES:
                    _watchlist_trend_cache.pop(next(iter(_watchlist_trend_cache)))
        return payload
    finally:
        async with _watchlist_trend_lock:
            if _watchlist_trend_inflight.get(key) is owner:
                _watchlist_trend_inflight.pop(key, None)


async def screener_trends_snapshot(routes: str) -> dict[str, Any]:
    if not _watchlist_trends_accepting:
        raise RuntimeError("SCREENER_TREND_RUNTIME_STOPPING")
    deps = _deps()
    requested_routes = tuple(
        sorted(parse_route_selection(routes), key=lambda route: route.identity)
    )
    instruments = resolve_route_selection(
        deps.selected_instruments,
        requested_routes,
    )
    now = datetime.now(tz=UTC)
    key: _WatchlistTrendCacheKey = (
        int(now.timestamp()) // _WATCHLIST_TREND_BUCKET_SECONDS,
        tuple(route.identity for route in requested_routes),
    )
    async with _watchlist_trend_lock:
        cached = _watchlist_trend_cache.get(key)
        if cached is not None:
            return cached
        task = _watchlist_trend_inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                _build_watchlist_trend_snapshot(
                    key,
                    instruments,
                    now,
                    deps.store_factory,
                ),
                name=f"watchlist-trends:{key[0]}:{len(requested_routes)}",
            )
            _watchlist_trend_inflight[key] = task
    return await asyncio.shield(task)


async def screener_snapshot(
    routes: str,
    interval: str = "5m",
) -> dict[str, Any]:
    deps = _deps()
    requested_routes = parse_route_selection(routes)
    await deps.apply_ibkr_runtime_settings_async()
    instruments = resolve_route_selection(
        deps.selected_instruments,
        requested_routes,
    )
    row_instruments = instruments
    live_instruments = row_instruments
    quote_wanted_ids = sorted(
        {qualified_instrument_id(instrument) for instrument in live_instruments}
    )
    bases = await run_physical_thread_call(screener_bases, row_instruments, interval)
    if deps.server_sleeping():
        _live_map, _live_warning, cache_revision = deps.quote_cache_for_instruments(
            live_instruments
        )
        rows = screener_rows(
            row_instruments,
            bases,
            {},
            "Server sleeping: live quote polling paused.",
            interval,
        )
    else:
        deps.set_transient_quote_wanted(
            f"screener:{route_selection_json(requested_routes)}",
            quote_wanted_ids,
            ttl_seconds=5.0,
        )
        live_map, live_warning, cache_revision = deps.quote_cache_for_instruments(live_instruments)
        rows = screener_rows(
            row_instruments,
            bases,
            live_map,
            live_warning,
            interval,
        )
    return {
        "cache_epoch": cache_revision.epoch,
        "cache_generation": cache_revision.generation,
        "rows": rows,
    }
