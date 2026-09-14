from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def run_tick_live_restore_loop(
    *,
    startup_delay_seconds: float,
    interval_seconds: float,
    restore_once: Callable[[str], Awaitable[object]],
    reason: str = "startup/monitor",
) -> None:
    await asyncio.sleep(max(float(startup_delay_seconds), 0.0))
    while True:
        await restore_once(reason)
        await asyncio.sleep(max(float(interval_seconds), 1.0))
