from __future__ import annotations

import asyncio
import time
from typing import Any

from aef_terminal.ui.services.market_analysis_worker import run_market_analysis_worker_loop


def test_market_analysis_worker_schedules_due_job() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        wanted: dict[str, dict[str, Any]] = {
            "job-1": {
                "wanted_at": time.monotonic(),
                "payload": {
                    "source": "ibkr",
                    "symbol": "ES",
                    "interval": "5m",
                    "range": "1d",
                    "signal_range": "3d",
                    "show_visuals": True,
                },
            }
        }
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {}
        cache: dict[str, dict[str, Any]] = {}
        spawned: list[tuple[str, dict[str, Any]]] = []

        def request_identity(payload: dict[str, Any]) -> tuple[Any, ...]:
            return (payload.get("symbol"), payload.get("interval"))

        def spawn_job(key: str, payload: dict[str, Any]) -> asyncio.Task[Any]:
            spawned.append((key, payload))

            async def job() -> None:
                await asyncio.Event().wait()

            return asyncio.create_task(job())

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=2,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cache=cache,
                request_identity=request_identity,
                refresh_seconds=lambda _payload: 2.0,
                spawn_job=spawn_job,
            )
        )
        await asyncio.sleep(0.12)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        assert spawned == [("job-1", wanted["job-1"]["payload"])]
        assert "job-1" in tasks

    asyncio.run(run())


def test_market_analysis_worker_wakes_immediately_for_canonical_generation() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        wake_queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        payload = {"symbol": "ES", "interval": "5m"}
        wanted = {
            "job-1": {
                "wanted_at": time.monotonic(),
                "payload": payload,
            }
        }
        tasks: dict[str, asyncio.Task[Any]] = {}
        spawned = asyncio.Event()

        def spawn_job(_key: str, _payload: dict[str, Any]) -> asyncio.Task[Any]:
            spawned.set()
            return asyncio.create_task(asyncio.Event().wait())

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=30.0,
                wanted_ttl_seconds=90.0,
                max_concurrent=1,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities={},
                cache={},
                request_identity=lambda item: (
                    item.get("symbol"),
                    item.get("interval"),
                ),
                refresh_seconds=lambda _payload: 10.0,
                spawn_job=spawn_job,
                wake_queue=wake_queue,
            )
        )
        await asyncio.sleep(0)
        wake_queue.put_nowait(None)
        await asyncio.wait_for(spawned.wait(), timeout=0.2)

        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    asyncio.run(run())


def test_market_analysis_worker_coalesces_until_latest_generation_settles() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        payload = {"symbol": "ES", "interval": "5m"}
        wanted = {
            "job-1": {
                "wanted_at": time.monotonic(),
                "not_before": time.monotonic() + 0.15,
                "payload": payload,
            }
        }
        tasks: dict[str, asyncio.Task[Any]] = {}
        spawned = asyncio.Event()

        def spawn_job(_key: str, _payload: dict[str, Any]) -> asyncio.Task[Any]:
            spawned.set()
            return asyncio.create_task(asyncio.Event().wait())

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.02,
                wanted_ttl_seconds=90.0,
                max_concurrent=1,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities={},
                cache={},
                request_identity=lambda item: (
                    item.get("symbol"),
                    item.get("interval"),
                ),
                refresh_seconds=lambda _payload: 10.0,
                spawn_job=spawn_job,
            )
        )
        await asyncio.sleep(0.08)
        assert not spawned.is_set()
        await asyncio.wait_for(spawned.wait(), timeout=0.2)

        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    asyncio.run(run())


def test_market_analysis_worker_drops_stale_wanted() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        wanted: dict[str, dict[str, Any]] = {
            "stale": {"wanted_at": time.monotonic() - 120.0, "payload": {"symbol": "ES"}},
        }
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {}
        cache: dict[str, dict[str, Any]] = {}

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=2,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cache=cache,
                request_identity=lambda payload: (payload.get("symbol"),),
                refresh_seconds=lambda _payload: 2.0,
                spawn_job=lambda key, payload: asyncio.create_task(asyncio.sleep(0)),
            )
        )
        await asyncio.sleep(0.12)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        assert "stale" not in wanted

    asyncio.run(run())


def test_market_analysis_worker_cancels_job_when_demand_expires() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        wanted: dict[str, dict[str, Any]] = {
            "stale": {
                "wanted_at": time.monotonic() - 120.0,
                "payload": {"symbol": "ES"},
            },
        }
        cancellation_requested = asyncio.Event()
        release_cancellation = asyncio.Event()

        async def active_job() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_requested.set()
                await release_cancellation.wait()
                raise

        active_task = asyncio.create_task(active_job())
        tasks: dict[str, asyncio.Task[Any]] = {"stale": active_task}
        identities: dict[str, tuple[Any, ...]] = {"stale": ("ES",)}
        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=2,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cache={},
                request_identity=lambda payload: (payload.get("symbol"),),
                refresh_seconds=lambda _payload: 2.0,
                spawn_job=lambda key, payload: asyncio.create_task(asyncio.sleep(0)),
            )
        )
        await asyncio.wait_for(cancellation_requested.wait(), timeout=0.5)

        assert "stale" not in wanted
        assert tasks["stale"] is active_task
        assert identities["stale"] == ("ES",)

        release_cancellation.set()
        await asyncio.gather(active_task, return_exceptions=True)
        await asyncio.sleep(0.1)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        assert "stale" not in tasks
        assert "stale" not in identities
        assert active_task.cancelled()

    asyncio.run(run())


def test_market_analysis_worker_cleans_completed_tasks() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        wanted: dict[str, dict[str, Any]] = {}
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        tasks: dict[str, asyncio.Task[Any]] = {"done": completed}
        identities: dict[str, tuple[Any, ...]] = {"done": ("ES", "5m")}
        cache: dict[str, dict[str, Any]] = {}

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=2,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cache=cache,
                request_identity=lambda payload: (payload.get("symbol"),),
                refresh_seconds=lambda _payload: 2.0,
                spawn_job=lambda key, payload: asyncio.create_task(asyncio.sleep(0)),
            )
        )
        await asyncio.sleep(0.12)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        assert tasks == {}
        assert identities == {}

    asyncio.run(run())


def test_market_analysis_worker_does_not_refresh_ready_versioned_snapshot() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        payload = {
            "source": "ibkr",
            "symbol": "ES",
            "interval": "5m",
            "market_version": {"latest_ts": "2026-06-30T10:00:00+00:00"},
        }
        wanted: dict[str, dict[str, Any]] = {
            "job-1": {
                "wanted_at": time.monotonic(),
                "payload": payload,
            }
        }
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {}
        cache: dict[str, dict[str, Any]] = {
            "job-1": {"status": "ready", "updated_monotonic": time.monotonic()},
        }
        spawned: list[str] = []

        def spawn_job(key: str, _payload: dict[str, Any]) -> asyncio.Task[Any]:
            spawned.append(key)
            return asyncio.create_task(asyncio.sleep(0))

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=2,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cache=cache,
                request_identity=lambda item: (item.get("symbol"), item.get("interval")),
                refresh_seconds=lambda _payload: 0.01,
                spawn_job=spawn_job,
            )
        )
        await asyncio.sleep(0.12)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        assert spawned == []

    asyncio.run(run())


def test_market_analysis_worker_refreshes_stale_versioned_snapshot() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        payload = {
            "source": "ibkr",
            "symbol": "ES",
            "interval": "5m",
            "market_version": {"latest_ts": "2026-06-30T10:00:00+00:00"},
        }
        wanted: dict[str, dict[str, Any]] = {
            "job-1": {
                "wanted_at": time.monotonic(),
                "payload": payload,
            }
        }
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {}
        cache: dict[str, dict[str, Any]] = {
            "job-1": {"status": "ready", "updated_monotonic": time.monotonic() - 120.0},
        }
        spawned: list[str] = []

        def spawn_job(key: str, _payload: dict[str, Any]) -> asyncio.Task[Any]:
            spawned.append(key)

            async def job() -> None:
                await asyncio.Event().wait()

            return asyncio.create_task(job())

        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=2,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cache=cache,
                request_identity=lambda item: (item.get("symbol"), item.get("interval")),
                refresh_seconds=lambda _payload: 10.0,
                spawn_job=spawn_job,
                versioned_refresh_seconds=60.0,
            )
        )
        await asyncio.sleep(0.12)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        assert spawned == ["job-1"]

    asyncio.run(run())


def test_market_analysis_worker_keeps_stable_event_driven_snapshot() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        payload = {
            "source": "ibkr",
            "symbol": "ES",
            "interval": "5m",
            "market_version": {"latest_ts": "2026-06-30T10:00:00+00:00"},
        }
        wanted = {
            "job-1": {
                "wanted_at": time.monotonic(),
                "payload": payload,
            }
        }
        spawned: list[str] = []
        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.05,
                wanted_ttl_seconds=90.0,
                max_concurrent=1,
                server_sleeping=lambda: False,
                lock=lock,
                wanted=wanted,
                tasks={},
                identities={},
                cache={
                    "job-1": {
                        "status": "ready",
                        "updated_monotonic": time.monotonic() - 3600.0,
                    }
                },
                request_identity=lambda item: (
                    item.get("symbol"),
                    item.get("interval"),
                ),
                refresh_seconds=lambda _payload: 10.0,
                spawn_job=lambda key, _payload: (
                    spawned.append(key) or asyncio.create_task(asyncio.sleep(0))
                ),
                periodic_refresh_required=lambda _payload: False,
            )
        )
        await asyncio.sleep(0.12)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        assert spawned == []

    asyncio.run(run())


def test_market_analysis_worker_does_not_retry_terminal_worker_failure() -> None:
    async def run() -> None:
        now = time.monotonic()
        spawned: list[str] = []
        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.02,
                wanted_ttl_seconds=90.0,
                max_concurrent=1,
                server_sleeping=lambda: False,
                lock=asyncio.Lock(),
                wanted={
                    "job-1": {
                        "wanted_at": now,
                        "payload": {"symbol": "ES", "interval": "5m"},
                    }
                },
                tasks={},
                identities={},
                cache={
                    "job-1": {
                        "status": "error",
                        "error_code": "MARKET_ANALYSIS_WORKER_UNAVAILABLE",
                        "retryable": False,
                        "updated_monotonic": now,
                    }
                },
                request_identity=lambda item: (
                    item.get("symbol"),
                    item.get("interval"),
                ),
                refresh_seconds=lambda _payload: 0.01,
                spawn_job=lambda key, _payload: (
                    spawned.append(key) or asyncio.create_task(asyncio.sleep(0))
                ),
            )
        )
        await asyncio.sleep(0.08)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        assert spawned == []

    asyncio.run(run())


def test_market_analysis_worker_backs_off_retryable_failure() -> None:
    async def run() -> None:
        now = time.monotonic()
        spawned: list[str] = []
        tasks: dict[str, asyncio.Task[Any]] = {}
        cache = {
            "job-1": {
                "status": "error",
                "retryable": True,
                "updated_monotonic": now,
            }
        }
        worker = asyncio.create_task(
            run_market_analysis_worker_loop(
                tick_seconds=0.02,
                wanted_ttl_seconds=90.0,
                max_concurrent=1,
                server_sleeping=lambda: False,
                lock=asyncio.Lock(),
                wanted={
                    "job-1": {
                        "wanted_at": now,
                        "payload": {"symbol": "ES", "interval": "5m"},
                    }
                },
                tasks=tasks,
                identities={},
                cache=cache,
                request_identity=lambda item: (
                    item.get("symbol"),
                    item.get("interval"),
                ),
                refresh_seconds=lambda _payload: 0.2,
                spawn_job=lambda key, _payload: (
                    spawned.append(key) or asyncio.create_task(asyncio.Event().wait())
                ),
            )
        )
        await asyncio.sleep(0.08)
        assert spawned == []
        cache["job-1"]["updated_monotonic"] = time.monotonic() - 1.0
        await asyncio.sleep(0.08)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

        assert spawned == ["job-1"]

    asyncio.run(run())
