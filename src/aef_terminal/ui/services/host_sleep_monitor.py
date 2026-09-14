from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import threading
import time

from aef_terminal.runtime.async_tasks import run_physical_thread_call


async def run_host_sleep_monitor_loop(
    *,
    check_seconds: float,
    gap_threshold_seconds: float,
    state_lock: threading.Lock,
    get_state: Callable[[], tuple[float, datetime]],
    set_state: Callable[[float, datetime], None],
    on_gap_detected: Callable[[datetime, datetime, float], None],
) -> None:
    while True:
        await asyncio.sleep(check_seconds)
        now_mono = time.monotonic()
        now_wall = datetime.now(tz=UTC)
        with state_lock:
            previous_mono, previous_wall = get_state()
            set_state(now_mono, now_wall)
        wall_gap = max((now_wall - previous_wall).total_seconds(), 0.0)
        loop_gap = max(now_mono - previous_mono, 0.0)
        effective_gap = max(wall_gap, loop_gap)
        if effective_gap >= gap_threshold_seconds:
            await run_physical_thread_call(
                on_gap_detected,
                previous_wall,
                now_wall,
                effective_gap,
            )
