from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from typing import Any

from aef_terminal.runtime.async_tasks import run_physical_thread_call


async def run_paper_option_quote_refresh_loop(
    *,
    poll_seconds: float,
    server_sleeping: Callable[[], bool],
    set_demand: Callable[[list[dict[str, Any]]], int],
    refresh_demanded: Callable[[], Any],
    release_all: Callable[[], None],
    on_iteration_error: Callable[[BaseException], None],
) -> None:
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or not math.isfinite(float(poll_seconds))
        or float(poll_seconds) < 0.1
    ):
        raise ValueError("paper option quote refresh poll_seconds must be finite and at least 0.1")
    interval = float(poll_seconds)
    released_for_sleep = False
    try:
        while True:
            await asyncio.sleep(interval)
            if server_sleeping():
                set_demand([])
                if not released_for_sleep:
                    try:
                        await run_physical_thread_call(release_all)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        on_iteration_error(exc)
                    else:
                        released_for_sleep = True
                continue
            released_for_sleep = False
            try:
                await run_physical_thread_call(refresh_demanded)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                on_iteration_error(exc)
    finally:
        set_demand([])
        await run_physical_thread_call(release_all)


async def run_paper_option_quote_refresh_runtime(
    *,
    server_sleeping: Callable[[], bool],
    on_iteration_error: Callable[[BaseException], None],
) -> None:
    from aef_terminal.ui.paper.option_market import (
        refresh_demanded_paper_option_quotes,
        release_all_paper_option_quotes,
        set_paper_option_quote_demand,
    )

    await run_paper_option_quote_refresh_loop(
        poll_seconds=0.5,
        server_sleeping=server_sleeping,
        set_demand=set_paper_option_quote_demand,
        refresh_demanded=refresh_demanded_paper_option_quotes,
        release_all=release_all_paper_option_quotes,
        on_iteration_error=on_iteration_error,
    )
