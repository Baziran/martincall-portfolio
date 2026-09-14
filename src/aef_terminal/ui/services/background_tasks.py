from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TaskPolicy:
    factory: Callable[[], Coroutine[Any, Any, Any]]
    restart_on_unexpected_exit: bool
    restart_delay_seconds: float
    restart_window_seconds: float
    max_restarts_per_window: int


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._policies: dict[str, _TaskPolicy] = {}
        self._restart_timers: dict[str, asyncio.TimerHandle] = {}
        self._restart_history: dict[str, deque[float]] = {}
        self._stopping: set[str] = set()

    def ensure(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        restart_on_unexpected_exit: bool = True,
        restart_delay_seconds: float = 1.0,
        restart_window_seconds: float = 60.0,
        max_restarts_per_window: int = 3,
    ) -> None:
        self._stopping.discard(name)
        self._policies[name] = _TaskPolicy(
            factory=factory,
            restart_on_unexpected_exit=bool(restart_on_unexpected_exit),
            restart_delay_seconds=max(float(restart_delay_seconds), 0.0),
            restart_window_seconds=max(float(restart_window_seconds), 1.0),
            max_restarts_per_window=max(int(max_restarts_per_window), 1),
        )
        task = self._tasks.get(name)
        if task is None or task.done():
            self._start_task(name)

    def _start_task(self, name: str) -> None:
        policy = self._policies.get(name)
        if policy is None:
            return
        task = asyncio.create_task(policy.factory(), name=f"bg:{name}")
        self._tasks[name] = task
        task.add_done_callback(
            lambda done_task, task_name=name: self._handle_done(task_name, done_task)
        )

    def _handle_done(self, name: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(name) is not task:
            return
        if name in self._stopping:
            return
        policy = self._policies.get(name)
        if policy is None:
            return
        if task.cancelled():
            _LOGGER.error("Background task %s was cancelled unexpectedly", name)
        else:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                _LOGGER.error("Background task %s was cancelled unexpectedly", name)
            else:
                if error is None:
                    _LOGGER.error("Background task %s exited unexpectedly", name)
                else:
                    _LOGGER.exception("Background task %s crashed", name, exc_info=error)
        if not policy.restart_on_unexpected_exit:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        history = self._restart_history.setdefault(name, deque())
        cutoff = now - policy.restart_window_seconds
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= policy.max_restarts_per_window:
            _LOGGER.error(
                "Background task %s exceeded restart budget (%s in %.1fs)",
                name,
                policy.max_restarts_per_window,
                policy.restart_window_seconds,
            )
            return
        history.append(now)
        timer = self._restart_timers.pop(name, None)
        if timer is not None:
            timer.cancel()
        self._restart_timers[name] = loop.call_later(
            policy.restart_delay_seconds,
            lambda: self._restart_task(name),
        )

    def _restart_task(self, name: str) -> None:
        self._restart_timers.pop(name, None)
        if name in self._stopping:
            return
        task = self._tasks.get(name)
        if task is not None and not task.done():
            return
        self._start_task(name)

    async def cancel(self, name: str) -> None:
        self._stopping.add(name)
        timer = self._restart_timers.pop(name, None)
        if timer is not None:
            timer.cancel()
        task = self._tasks.pop(name, None)
        if task is None:
            self._policies.pop(name, None)
            self._restart_history.pop(name, None)
            self._stopping.discard(name)
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Task may have already failed before explicit cancellation.
            pass
        finally:
            self._policies.pop(name, None)
            self._restart_history.pop(name, None)
            self._stopping.discard(name)

    async def cancel_all(self, names: list[str] | None = None) -> None:
        targets = list(names) if names is not None else list(self._tasks.keys())
        for name in targets:
            await self.cancel(name)

    def is_running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()
