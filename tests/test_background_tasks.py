from __future__ import annotations

import asyncio

from aef_terminal.ui.services.background_tasks import BackgroundTaskRegistry


def test_background_task_registry_ensure_and_cancel() -> None:
    async def run() -> None:
        registry = BackgroundTaskRegistry()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        registry.ensure("worker", worker)
        assert registry.is_running("worker")
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await registry.cancel("worker")
        assert not registry.is_running("worker")
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)

    asyncio.run(run())


def test_background_task_registry_cancel_all() -> None:
    async def run() -> None:
        registry = BackgroundTaskRegistry()

        async def noop() -> None:
            await asyncio.Event().wait()

        registry.ensure("a", noop)
        registry.ensure("b", noop)
        assert registry.is_running("a")
        assert registry.is_running("b")

        await registry.cancel_all(["a", "b"])
        assert not registry.is_running("a")
        assert not registry.is_running("b")

    asyncio.run(run())


def test_background_task_registry_restarts_failed_task() -> None:
    async def run() -> None:
        registry = BackgroundTaskRegistry()
        attempts = 0
        restarted = asyncio.Event()

        async def worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("boom")
            restarted.set()
            await asyncio.Event().wait()

        registry.ensure(
            "worker",
            worker,
            restart_delay_seconds=0.01,
            restart_window_seconds=1.0,
            max_restarts_per_window=2,
        )
        await asyncio.wait_for(restarted.wait(), timeout=1.0)
        assert attempts >= 2
        await registry.cancel("worker")

    asyncio.run(run())


def test_background_task_registry_restarts_unexpectedly_cancelled_task() -> None:
    async def run() -> None:
        registry = BackgroundTaskRegistry()
        attempts = 0
        restarted = asyncio.Event()

        async def worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            restarted.set()
            await asyncio.Event().wait()

        registry.ensure(
            "worker",
            worker,
            restart_delay_seconds=0.01,
            restart_window_seconds=1.0,
            max_restarts_per_window=2,
        )
        await asyncio.wait_for(restarted.wait(), timeout=1.0)
        assert attempts == 2
        await registry.cancel("worker")

    asyncio.run(run())


def test_background_task_registry_restarts_unexpected_normal_exit() -> None:
    async def run() -> None:
        registry = BackgroundTaskRegistry()
        attempts = 0
        restarted = asyncio.Event()

        async def worker() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return
            restarted.set()
            await asyncio.Event().wait()

        registry.ensure(
            "worker",
            worker,
            restart_delay_seconds=0.01,
            restart_window_seconds=1.0,
            max_restarts_per_window=2,
        )
        await asyncio.wait_for(restarted.wait(), timeout=1.0)
        assert attempts == 2
        await registry.cancel("worker")

    asyncio.run(run())


def test_background_task_registry_limits_restart_budget() -> None:
    async def run() -> None:
        registry = BackgroundTaskRegistry()
        attempts = 0

        async def worker() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("always-fail")

        registry.ensure(
            "worker",
            worker,
            restart_delay_seconds=0.01,
            restart_window_seconds=1.0,
            max_restarts_per_window=2,
        )
        await asyncio.sleep(0.2)
        # Initial run + at most 2 restarts within budget.
        assert attempts <= 3
        await registry.cancel("worker")

    asyncio.run(run())
