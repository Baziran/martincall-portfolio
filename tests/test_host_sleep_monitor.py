from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.ui.services.host_sleep_monitor import run_host_sleep_monitor_loop


class _StopMonitor(Exception):
    pass


def test_host_sleep_gap_persistence_runs_off_the_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    previous_wall = datetime.now(tz=UTC) - timedelta(seconds=5)
    callback_thread = 0

    def on_gap_detected(_previous: datetime, _current: datetime, _gap: float) -> None:
        nonlocal callback_thread
        callback_thread = threading.get_ident()
        raise _StopMonitor

    async def run() -> None:
        with pytest.raises(_StopMonitor):
            await run_host_sleep_monitor_loop(
                check_seconds=0.0,
                gap_threshold_seconds=1.0,
                state_lock=threading.Lock(),
                get_state=lambda: (time.monotonic() - 5.0, previous_wall),
                set_state=lambda _monotonic, _wall: None,
                on_gap_detected=on_gap_detected,
            )

    asyncio.run(run())

    assert callback_thread != event_loop_thread


def test_host_sleep_cancellation_waits_for_physical_gap_persistence() -> None:
    previous_wall = datetime.now(tz=UTC) - timedelta(seconds=5)
    started = threading.Event()
    release = threading.Event()

    def on_gap_detected(_previous: datetime, _current: datetime, _gap: float) -> None:
        started.set()
        assert release.wait(timeout=1.0)

    async def run() -> None:
        task = asyncio.create_task(
            run_host_sleep_monitor_loop(
                check_seconds=0.0,
                gap_threshold_seconds=1.0,
                state_lock=threading.Lock(),
                get_state=lambda: (time.monotonic() - 5.0, previous_wall),
                set_state=lambda _monotonic, _wall: None,
                on_gap_detected=on_gap_detected,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
