from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import Executor
from dataclasses import dataclass
from functools import partial
from typing import Any, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class PhysicalTaskOutcome(Generic[T]):
    result: T | None = None
    error: Exception | None = None
    cancellation: asyncio.CancelledError | None = None
    timed_out: bool = False
    task_cancelled: bool = False


async def settle_physical_task(
    task: asyncio.Future[T],
    *,
    timeout: float | None = None,
    deferred_cancellation: asyncio.CancelledError | None = None,
) -> PhysicalTaskOutcome[T]:
    """Wait for non-cancellable physical work before exposing cancellation.

    ``asyncio.to_thread`` and executor futures keep running after their asyncio
    waiter is cancelled.  This primitive records caller cancellation, removes
    it temporarily, and owns the physical future until its exact outcome is
    known.  A logical timeout is telemetry only; it never abandons the worker.
    """

    if timeout is not None and float(timeout) <= 0:
        raise ValueError("physical task timeout must be positive")
    cancellation = deferred_cancellation
    timed_out = False
    current_task = asyncio.current_task()
    if cancellation is not None and current_task is not None:
        while current_task.cancelling():
            current_task.uncancel()
    try:
        if timeout is None:
            result = await asyncio.shield(task)
        else:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=float(timeout),
            )
    except TimeoutError:
        timed_out = True
    except asyncio.CancelledError as exc:
        if task.cancelled() and not (current_task is not None and current_task.cancelling()):
            return PhysicalTaskOutcome(task_cancelled=True)
        cancellation = exc
        if current_task is not None:
            while current_task.cancelling():
                current_task.uncancel()
    except Exception as exc:
        return PhysicalTaskOutcome(error=exc)
    else:
        return PhysicalTaskOutcome(result=result)

    while True:
        if task.cancelled():
            return PhysicalTaskOutcome(
                cancellation=cancellation,
                timed_out=timed_out,
                task_cancelled=True,
            )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled() and not (current_task is not None and current_task.cancelling()):
                return PhysicalTaskOutcome(
                    cancellation=cancellation,
                    timed_out=timed_out,
                    task_cancelled=True,
                )
            cancellation = cancellation or exc
            if current_task is not None:
                while current_task.cancelling():
                    current_task.uncancel()
        except Exception as exc:
            return PhysicalTaskOutcome(
                error=exc,
                cancellation=cancellation,
                timed_out=timed_out,
            )
        else:
            return PhysicalTaskOutcome(
                result=result,
                cancellation=cancellation,
                timed_out=timed_out,
            )


async def run_physical_thread_call(
    operation: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run one blocking call without releasing its owner before physical completion."""

    return await run_cancellation_deferred(
        asyncio.to_thread(operation, *args, **kwargs),
        task_cancelled_error="PHYSICAL_THREAD_TASK_CANCELLED",
    )


async def run_physical_executor_call(
    executor: Executor,
    operation: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run one explicit-executor call without abandoning its physical future."""

    future = asyncio.get_running_loop().run_in_executor(
        executor,
        partial(operation, *args, **kwargs),
    )
    return await await_cancellation_deferred_task(
        future,
        task_cancelled_error="PHYSICAL_EXECUTOR_TASK_CANCELLED",
    )


async def run_cancellation_deferred(
    operation: Coroutine[Any, Any, T],
    *,
    task_cancelled_error: str,
) -> T:
    """Own one asynchronous operation until it reaches a terminal outcome."""

    task = asyncio.create_task(operation)
    return await await_cancellation_deferred_task(
        task,
        task_cancelled_error=task_cancelled_error,
    )


async def await_cancellation_deferred_task(
    task: asyncio.Future[T],
    *,
    task_cancelled_error: str,
) -> T:
    """Wait for an existing owned task before exposing caller cancellation."""

    outcome = await settle_physical_task(task)
    if outcome.error is not None:
        if outcome.cancellation is not None:
            raise outcome.cancellation from outcome.error
        raise outcome.error
    if outcome.task_cancelled:
        if outcome.cancellation is not None:
            raise outcome.cancellation
        raise RuntimeError(task_cancelled_error)
    if outcome.cancellation is not None:
        raise outcome.cancellation
    return task.result()
