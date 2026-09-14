from __future__ import annotations
import asyncio
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.runtime.async_tasks import run_physical_thread_call


async def _reset_ibkr_async_sessions_owned() -> None:
    from aef_terminal.data.ibkr.session import _reset_ibkr_quarantined_sessions

    failures: list[Exception] = []
    for _attempt in range(2):
        failures = []
        try:
            _reset_ibkr_async_sessions_direct()
        except Exception as exc:
            failures.append(exc)
        # asyncio transports publish connection_lost on the next owner-loop
        # turn. Global quarantine cleanup then runs on its physical worker so
        # a stopped foreign loop is never pumped by an unowned daemon thread.
        await asyncio.sleep(0)
        try:
            await run_physical_thread_call(_reset_ibkr_quarantined_sessions)
        except Exception as exc:
            failures.append(exc)
        if not failures:
            return
    raise ExceptionGroup("IBKR session teardown failed", failures)


def _reset_ibkr_async_sessions_direct() -> None:
    from aef_terminal.data.ibkr.session import (
        _reset_ibkr_async_chart_session,
        _reset_ibkr_async_option_quote_session,
        _reset_ibkr_async_quote_session,
        _reset_ibkr_async_session,
    )

    published_resets = (
        _reset_ibkr_async_quote_session,
        _reset_ibkr_async_option_quote_session,
        _reset_ibkr_async_chart_session,
        _reset_ibkr_async_session,
    )
    failed_resets = []
    for reset in published_resets:
        try:
            reset()
        except Exception:
            failed_resets.append(reset)

    failures: list[Exception] = []
    if failed_resets:
        for reset in failed_resets:
            try:
                reset()
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise ExceptionGroup("IBKR manager session teardown failed", failures)


async def _reset_ibkr_async_sessions_async(timeout: float = 5.0) -> None:
    """Reset async IBKR sessions without blocking the caller event loop."""
    await ibkr_market_data_manager.run_coroutine(
        "control",
        "ibkr async reset",
        _reset_ibkr_async_sessions_owned,
        timeout=timeout,
    )


def _reset_ibkr_async_sessions_threadsafe(timeout: float = 5.0) -> None:
    ibkr_market_data_manager.run_coroutine_blocking(
        "control",
        "ibkr async reset",
        _reset_ibkr_async_sessions_owned,
        timeout=timeout,
    )


def _ensure_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
