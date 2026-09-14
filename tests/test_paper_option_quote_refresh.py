from __future__ import annotations

import asyncio
import threading

import pytest

from aef_terminal.ui.services.paper_option_quote_refresh import (
    run_paper_option_quote_refresh_loop,
)


def test_option_quote_physical_refresh_is_settled_before_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    released = threading.Event()

    def physical_refresh() -> None:
        started.set()
        release.wait(timeout=1.0)
        completed.set()

    async def run() -> None:
        task = asyncio.create_task(
            run_paper_option_quote_refresh_loop(
                poll_seconds=0.1,
                server_sleeping=lambda: False,
                set_demand=lambda _rows: 0,
                refresh_demanded=physical_refresh,
                release_all=released.set,
                on_iteration_error=lambda _exc: None,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert completed.is_set()
    assert released.is_set()


def test_option_quote_refresh_loop_releases_demand_on_shutdown() -> None:
    demands: list[list[dict[str, object]]] = []
    released = threading.Event()

    async def run() -> None:
        task = asyncio.create_task(
            run_paper_option_quote_refresh_loop(
                poll_seconds=0.1,
                server_sleeping=lambda: False,
                set_demand=lambda rows: demands.append(list(rows)) or len(rows),
                refresh_demanded=lambda: None,
                release_all=released.set,
                on_iteration_error=lambda _exc: None,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert demands[-1] == []
    assert released.is_set()
