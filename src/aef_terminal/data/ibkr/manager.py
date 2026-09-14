from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, TypeVar

from aef_terminal.data.provider_contract import OptionUniverseUnavailableError
from aef_terminal.runtime.async_tasks import run_physical_thread_call, settle_physical_task
from aef_terminal.runtime.metrics import increment_metric, observe_metric

T = TypeVar("T")


def normalize_ibkr_lane_error(exc: BaseException, *, cancelled: bool = False) -> dict[str, Any]:
    if cancelled or isinstance(exc, asyncio.CancelledError):
        return {
            "code": "IBKR_LANE_CANCELLED",
            "category": "broker",
            "retryable": True,
            "message": "IBKR lane request cancelled",
        }
    if isinstance(exc, TimeoutError):
        message = str(exc).strip() or "IBKR lane request timed out"
        return {
            "code": "IBKR_LANE_TIMEOUT",
            "category": "broker",
            "retryable": True,
            "message": message,
        }
    if isinstance(exc, OptionUniverseUnavailableError):
        message = str(exc).strip() or "IBKR option universe is unavailable"
        return {
            "code": "IBKR_OPTION_UNIVERSE_UNAVAILABLE",
            "category": "options",
            "retryable": True,
            "reason": exc.reason,
            "diagnostics": dict(exc.diagnostics),
            "message": message,
        }
    message = str(exc).strip() or exc.__class__.__name__
    return {
        "code": "IBKR_LANE_ERROR",
        "category": "broker",
        "retryable": True,
        "message": message,
    }


@dataclass
class _LaneState:
    pending: int = 0
    pending_since: datetime | None = None
    running: bool = False
    running_label: str = ""
    running_since: datetime | None = None
    running_timeout_seconds: float | None = None
    completed: int = 0
    cancelled: int = 0
    errors: int = 0
    last_completed_at: datetime | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    last_error_code: str = ""
    last_error_category: str = ""
    last_error_retryable: bool = False
    last_cancelled_at: datetime | None = None


def _lane_error_fields(state: _LaneState) -> dict[str, Any]:
    if not state.last_error and not state.last_error_code:
        return {}
    payload = {
        "code": state.last_error_code,
        "category": state.last_error_category,
        "retryable": state.last_error_retryable,
        "message": state.last_error,
    }
    if state.last_cancelled_at is not None:
        payload["cancelled_at"] = state.last_cancelled_at.isoformat()
    return {"error": payload}


class IbkrMarketDataManager:
    """Own IBKR market-data work on one event loop with per-lane queues."""

    def __init__(
        self,
        lanes: tuple[str, ...] = ("chart", "quote", "history", "options", "gex", "control"),
        priority_lanes: tuple[str, ...] = ("chart", "quote", "history"),
        pacing_intervals: dict[str, float] | None = None,
        exclusive_groups: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._lanes = lanes
        self._priority_lanes = tuple(priority_lanes)
        self._lane_priority = {lane: index for index, lane in enumerate(priority_lanes)}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        self._lane_lock_guard = threading.Lock()
        self._state_lock = threading.RLock()
        self._lane_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
        self._caller_submission_gates: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._group_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
        self._states: dict[str, _LaneState] = {lane: _LaneState() for lane in lanes}
        self._exclusive_groups = dict(exclusive_groups or {})
        self._lane_group = {
            lane: group
            for group, group_lanes in self._exclusive_groups.items()
            for lane in group_lanes
        }
        self._pacing_intervals = {
            "chart": 0.0,
            "quote": 0.0,
            "history": 0.10,
            "options": 0.20,
            "gex": 0.35,
            "control": 0.0,
            **(pacing_intervals or {}),
        }
        self._pacing_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
        self._last_paced_request_at: datetime | None = None

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if (
                self._loop is not None
                and self._loop.is_running()
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return self._loop

            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run_loop() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=run_loop, name="ibkr-market-data", daemon=True)
            self._loop = loop
            self._thread = thread
            thread.start()
            if not ready.wait(timeout=2.0):
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
                thread.join(timeout=0.2)
                self._loop = None
                self._thread = None
                raise RuntimeError("IBKR manager event loop failed to start")
            return loop

    async def run_coroutine(
        self,
        lane: str,
        label: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
        apply_pacing: bool = True,
    ) -> T:
        current_loop = asyncio.get_running_loop()
        loop = self._loop
        if current_loop is loop:
            return await self._run_lane(
                lane,
                label,
                factory,
                timeout=timeout,
                apply_pacing=apply_pacing,
            )
        future: concurrent.futures.Future[T] | None = None
        run_on_current_loop = False
        gate = self._caller_submission_gate(current_loop)
        async with gate:
            loop = self._loop
            if loop is None or not loop.is_running():
                loop = await run_physical_thread_call(self.ensure_loop)
            if current_loop is loop:
                run_on_current_loop = True
            else:
                future = asyncio.run_coroutine_threadsafe(
                    self._run_lane(
                        lane,
                        label,
                        factory,
                        timeout=timeout,
                        apply_pacing=apply_pacing,
                    ),
                    loop,
                )
        if run_on_current_loop:
            return await self._run_lane(
                lane,
                label,
                factory,
                timeout=timeout,
                apply_pacing=apply_pacing,
            )
        if future is None:
            raise RuntimeError("IBKR manager failed to submit lane request")
        wrapped_future = asyncio.wrap_future(future)
        outcome = await settle_physical_task(wrapped_future)
        if outcome.error is not None:
            if outcome.cancellation is not None:
                raise outcome.cancellation from outcome.error
            raise outcome.error
        if outcome.task_cancelled:
            if outcome.cancellation is not None:
                raise outcome.cancellation
            raise asyncio.CancelledError()
        if outcome.cancellation is not None:
            raise outcome.cancellation
        return wrapped_future.result()

    def run_coroutine_blocking(
        self,
        lane: str,
        label: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float = 5.0,
        apply_pacing: bool = True,
    ) -> T:
        loop = self.ensure_loop()
        bounded_timeout = max(float(timeout), 0.1)
        future = asyncio.run_coroutine_threadsafe(
            self._run_lane(
                lane,
                label,
                factory,
                timeout=bounded_timeout,
                apply_pacing=apply_pacing,
            ),
            loop,
        )
        try:
            return future.result()
        except TimeoutError as exc:
            raise TimeoutError(f"IBKR {lane} lane timed out: {label}") from exc

    async def run_sync(
        self,
        lane: str,
        label: str,
        callback: Callable[[], T],
        *,
        timeout: float | None = None,
    ) -> T:
        async def run_physical_callback() -> T:
            task = asyncio.create_task(asyncio.to_thread(callback))
            outcome = await settle_physical_task(task)
            if outcome.cancellation is not None:
                raise outcome.cancellation
            if outcome.task_cancelled:
                raise asyncio.CancelledError()
            if outcome.error is not None:
                raise outcome.error
            return task.result()

        return await self.run_coroutine(
            lane,
            label,
            run_physical_callback,
            timeout=timeout,
        )

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "loop_running": bool(self._loop is not None and self._loop.is_running()),
                "thread_alive": bool(self._thread is not None and self._thread.is_alive()),
                "pacing": {
                    "intervals": dict(self._pacing_intervals),
                    "last_paced_request_at": self._last_paced_request_at.isoformat()
                    if self._last_paced_request_at
                    else "",
                },
                "exclusive_groups": {
                    group: list(lanes) for group, lanes in self._exclusive_groups.items()
                },
                "lanes": {
                    lane: {
                        "pending": state.pending,
                        "pending_since": state.pending_since.isoformat()
                        if state.pending_since
                        else "",
                        "oldest_pending_seconds": _elapsed_seconds(state.pending_since),
                        "running": state.running,
                        "running_label": state.running_label,
                        "running_since": state.running_since.isoformat()
                        if state.running_since
                        else "",
                        "running_seconds": _elapsed_seconds(state.running_since),
                        "running_timeout_seconds": state.running_timeout_seconds,
                        "completed": state.completed,
                        "cancelled": state.cancelled,
                        "errors": state.errors,
                        "last_completed_at": state.last_completed_at.isoformat()
                        if state.last_completed_at
                        else "",
                        "last_error": state.last_error,
                        "last_error_at": state.last_error_at.isoformat()
                        if state.last_error_at
                        else "",
                        **_lane_error_fields(state),
                    }
                    for lane, state in self._states.items()
                },
            }

    def _lane_lock(self, lane: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._lane_lock_guard:
            item = self._lane_locks.get(lane)
            if item is None or item[0] is not loop:
                lock = asyncio.Lock()
                self._lane_locks[lane] = (loop, lock)
                return lock
            return item[1]

    def _caller_submission_gate(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> asyncio.Lock:
        with self._lane_lock_guard:
            closed_loops = tuple(
                candidate for candidate in self._caller_submission_gates if candidate.is_closed()
            )
            for candidate in closed_loops:
                self._caller_submission_gates.pop(candidate, None)
            gate = self._caller_submission_gates.get(loop)
            if gate is None:
                gate = asyncio.Lock()
                self._caller_submission_gates[loop] = gate
            return gate

    def _shared_pacing_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._lane_lock_guard:
            if self._pacing_lock is None or self._pacing_lock[0] is not loop:
                lock = asyncio.Lock()
                self._pacing_lock = (loop, lock)
                return lock
            return self._pacing_lock[1]

    def _exclusive_group_lock(self, lane: str) -> asyncio.Lock | None:
        group = self._lane_group.get(lane)
        if not group:
            return None
        loop = asyncio.get_running_loop()
        with self._lane_lock_guard:
            item = self._group_locks.get(group)
            if item is None or item[0] is not loop:
                lock = asyncio.Lock()
                self._group_locks[group] = (loop, lock)
                return lock
            return item[1]

    async def _run_lane(
        self,
        lane: str,
        label: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
        apply_pacing: bool = True,
    ) -> T:
        acquired = False
        started = False
        queued_at = perf_counter()
        started_at: float | None = None
        succeeded = False
        group_lock: asyncio.Lock | None = None
        group_acquired = False
        deadline = (
            asyncio.get_running_loop().time() + max(float(timeout), 0.1)
            if timeout is not None
            else None
        )

        def remaining_timeout() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"IBKR {lane} lane timed out before start: {label}")
            return remaining

        self._mark_pending(lane, 1)
        try:
            priority_timeout = remaining_timeout()
            if priority_timeout is None:
                await self._wait_for_priority(lane)
            else:
                await asyncio.wait_for(self._wait_for_priority(lane), timeout=priority_timeout)
            lock = self._lane_lock(lane)
            acquire_timeout = remaining_timeout()
            if acquire_timeout is None:
                await lock.acquire()
            else:
                await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
            acquired = True
            group_lock = self._exclusive_group_lock(lane)
            if group_lock is not None:
                group_timeout = remaining_timeout()
                if group_timeout is None:
                    await group_lock.acquire()
                else:
                    await asyncio.wait_for(group_lock.acquire(), timeout=group_timeout)
                group_acquired = True
            if apply_pacing:
                await self._apply_pacing(lane)
            self._mark_start(lane, label, remaining_timeout())
            started = True
            started_at = perf_counter()
            observe_metric("ibkr_lane_wait_seconds", started_at - queued_at, lane=lane)
            work = factory()
            work_timeout = remaining_timeout()
            if work_timeout is None:
                result = await work
            else:
                result = await asyncio.wait_for(work, timeout=work_timeout)
            succeeded = True
            return result
        except asyncio.CancelledError:
            self._mark_cancelled(lane)
            increment_metric("ibkr_lane_cancelled_total", lane=lane)
            raise
        except Exception as exc:
            self._mark_error(lane, exc)
            structured = normalize_ibkr_lane_error(exc)
            increment_metric(
                "ibkr_lane_errors_total",
                lane=lane,
                code=structured.get("code") or "IBKR_LANE_ERROR",
            )
            raise
        finally:
            if started_at is not None:
                observe_metric("ibkr_lane_duration_seconds", perf_counter() - started_at, lane=lane)
            if group_acquired and group_lock is not None:
                group_lock.release()
            if acquired:
                if started:
                    self._mark_complete(lane, recovered=succeeded)
                else:
                    self._mark_pending(lane, -1)
                lock.release()
            else:
                self._mark_pending(lane, -1)

    async def _apply_pacing(self, lane: str) -> None:
        interval = max(float(self._pacing_intervals.get(lane, 0.0) or 0.0), 0.0)
        if interval <= 0:
            return
        lock = self._shared_pacing_lock()
        async with lock:
            now = datetime.now(tz=UTC)
            if self._last_paced_request_at is not None:
                elapsed = (now - self._last_paced_request_at).total_seconds()
                wait_seconds = interval - elapsed
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            self._last_paced_request_at = datetime.now(tz=UTC)

    async def _wait_for_priority(self, lane: str) -> None:
        lane_rank = self._lane_priority.get(lane)
        if lane_rank == 0:
            return
        blocking_lanes = (
            self._priority_lanes[:lane_rank] if lane_rank is not None else self._priority_lanes
        )
        while True:
            with self._state_lock:
                blocked = any(
                    priority != lane
                    and (self._state(priority).running or self._state(priority).pending > 0)
                    for priority in blocking_lanes
                )
            if not blocked:
                return
            await asyncio.sleep(0.01)

    def _state(self, lane: str) -> _LaneState:
        state = self._states.get(lane)
        if state is None:
            state = _LaneState()
            self._states[lane] = state
        return state

    def _mark_pending(self, lane: str, delta: int) -> None:
        with self._state_lock:
            state = self._state(lane)
            previous = state.pending
            state.pending = max(state.pending + delta, 0)
            if state.pending > 0 and previous <= 0:
                state.pending_since = datetime.now(tz=UTC)
            elif state.pending <= 0:
                state.pending_since = None

    def _mark_start(self, lane: str, label: str, timeout_seconds: float | None) -> None:
        with self._state_lock:
            state = self._state(lane)
            state.pending = max(state.pending - 1, 0)
            if state.pending <= 0:
                state.pending_since = None
            state.running = True
            state.running_label = str(label or lane)
            state.running_since = datetime.now(tz=UTC)
            state.running_timeout_seconds = timeout_seconds

    def _mark_complete(self, lane: str, *, recovered: bool) -> None:
        with self._state_lock:
            state = self._state(lane)
            state.running = False
            state.running_label = ""
            state.running_since = None
            state.running_timeout_seconds = None
            state.completed += 1
            state.last_completed_at = datetime.now(tz=UTC)
            if recovered:
                state.last_error = ""
                state.last_error_at = None
                state.last_error_code = ""
                state.last_error_category = ""
                state.last_error_retryable = False
                state.last_cancelled_at = None

    def _mark_cancelled(self, lane: str) -> None:
        now = datetime.now(tz=UTC)
        structured = normalize_ibkr_lane_error(asyncio.CancelledError(), cancelled=True)
        with self._state_lock:
            state = self._state(lane)
            state.cancelled += 1
            state.last_cancelled_at = now
            state.last_error = str(structured["message"])
            state.last_error_at = now
            state.last_error_code = str(structured["code"])
            state.last_error_category = str(structured["category"])
            state.last_error_retryable = bool(structured["retryable"])

    def _mark_error(self, lane: str, exc: Exception) -> None:
        structured = normalize_ibkr_lane_error(exc)
        with self._state_lock:
            state = self._state(lane)
            state.errors += 1
            state.last_error = str(structured["message"])
            state.last_error_at = datetime.now(tz=UTC)
            state.last_error_code = str(structured["code"])
            state.last_error_category = str(structured["category"])
            state.last_error_retryable = bool(structured["retryable"])


def _elapsed_seconds(started_at: datetime | None) -> float | None:
    if started_at is None:
        return None
    return round(max((datetime.now(tz=UTC) - started_at).total_seconds(), 0.0), 3)


ibkr_market_data_manager = IbkrMarketDataManager()
