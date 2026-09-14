from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aef_terminal.ui.services.market_analysis_store import (
    MarketAnalysisStateLock,
    revoke_market_analysis_wanted_locked,
)


async def run_market_analysis_worker_loop(
    *,
    tick_seconds: float,
    wanted_ttl_seconds: float,
    max_concurrent: int,
    server_sleeping: Callable[[], bool],
    lock: MarketAnalysisStateLock,
    wanted: dict[str, dict[str, Any]],
    tasks: dict[str, asyncio.Task[Any]],
    identities: dict[str, tuple[Any, ...]],
    cache: dict[str, dict[str, Any]],
    request_identity: Callable[[dict[str, Any]], tuple[Any, ...]],
    refresh_seconds: Callable[[dict[str, Any]], float],
    spawn_job: Callable[[str, dict[str, Any]], asyncio.Task[Any]],
    versioned_refresh_seconds: float = 60.0,
    periodic_refresh_required: Callable[
        [dict[str, Any]],
        bool,
    ]
    | None = None,
    reconcile_wanted: Callable[[bool], Awaitable[None]] | None = None,
    wake_queue: asyncio.Queue[None] | None = None,
    cancel_task: Callable[[str, asyncio.Task[Any]], None] | None = None,
    prune_wanted_locked: Callable[[float], None] | None = None,
) -> None:
    while True:
        if wake_queue is None:
            await asyncio.sleep(tick_seconds)
        else:
            try:
                await asyncio.wait_for(
                    wake_queue.get(),
                    timeout=tick_seconds,
                )
            except TimeoutError:
                pass
        if server_sleeping():
            if reconcile_wanted is not None:
                await reconcile_wanted(False)
            continue
        now = time.monotonic()
        async with lock:
            completed_tasks = [key for key, task in tasks.items() if task and task.done()]
            for key in completed_tasks:
                tasks.pop(key, None)
                identities.pop(key, None)
            if prune_wanted_locked is not None:
                prune_wanted_locked(now)
            else:
                stale_wanted = [
                    key
                    for key, item in wanted.items()
                    if now - float(item.get("wanted_at") or 0.0) > wanted_ttl_seconds
                ]
                for key in stale_wanted:
                    revoke_market_analysis_wanted_locked(
                        key,
                        wanted=wanted,
                        tasks=tasks,
                        identities=identities,
                        cancel_task=cancel_task,
                    )
        if reconcile_wanted is not None:
            await reconcile_wanted(True)
        async with lock:
            running = sum(1 for task in tasks.values() if task and not task.done())
            if running >= max_concurrent:
                continue
            running_identities = {
                identity
                for task_key, task in tasks.items()
                if task and not task.done()
                for identity in [identities.get(task_key)]
                if identity is not None
            }
            due: list[tuple[str, dict[str, Any]]] = []
            for key, item in wanted.items():
                if now < float(item.get("not_before") or 0.0):
                    continue
                if key in tasks and not tasks[key].done():
                    continue
                identity = request_identity(item.get("payload") or {})
                if identity in running_identities:
                    continue
                cached = cache.get(key)
                if cached and cached.get("status") == "error" and cached.get("retryable") is False:
                    continue
                cache_age = now - float((cached or {}).get("updated_monotonic") or 0.0)
                refresh = refresh_seconds(item.get("payload") or {})
                payload = item.get("payload")
                versioned = isinstance(payload, dict) and isinstance(
                    payload.get("market_version"),
                    dict,
                )
                periodic = (
                    periodic_refresh_required(payload)
                    if isinstance(payload, dict) and periodic_refresh_required is not None
                    else True
                )
                effective_refresh = (
                    max(versioned_refresh_seconds, refresh) if versioned else refresh
                )
                if cached and cached.get("status") == "error" and cache_age < effective_refresh:
                    continue
                if versioned and cached and cached.get("status") == "ready" and not periodic:
                    continue
                if (
                    versioned
                    and cached
                    and cached.get("status") == "ready"
                    and cache_age < effective_refresh
                ):
                    continue
                if not cached or cached.get("status") != "ready" or cache_age >= effective_refresh:
                    if isinstance(payload, dict):
                        due.append((key, payload))
                        running_identities.add(identity)
                if len(due) + running >= max_concurrent:
                    break
            for key, payload in due:
                identities[key] = request_identity(payload)
                tasks[key] = spawn_job(key, payload)
