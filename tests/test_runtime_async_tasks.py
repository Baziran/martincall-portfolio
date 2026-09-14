from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from aef_terminal.runtime.async_tasks import run_physical_executor_call


def test_physical_executor_call_defers_cancellation_until_worker_finishes() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def operation() -> str:
        started.set()
        assert release.wait(1.0)
        finished.set()
        return "done"

    async def run_probe() -> None:
        task = asyncio.create_task(run_physical_executor_call(executor, operation))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run_probe())
    finally:
        release.set()
        executor.shutdown(wait=True)

    assert finished.is_set()


def test_physical_executor_call_settles_worker_before_timeout_is_visible() -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def operation() -> str:
        started.set()
        assert release.wait(1.0)
        return "late"

    async def run_probe() -> None:
        task = asyncio.create_task(
            asyncio.wait_for(
                run_physical_executor_call(executor, operation),
                timeout=0.02,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert task.done() is False

        release.set()
        with pytest.raises(TimeoutError):
            await task

    try:
        asyncio.run(run_probe())
    finally:
        release.set()
        executor.shutdown(wait=True)
