"""Provider-neutral contracts contributed by indicator background services."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IndicatorServiceContext:
    logger: logging.Logger
    store_factory: Callable[[], Any]
    server_sleeping: Callable[[], bool]
    client_settings_snapshot: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class IndicatorServiceTask:
    name: str
    factory: Callable[[], Coroutine[Any, Any, Any]]
    restart_delay_seconds: float = 1.0
    restart_window_seconds: float = 60.0
    max_restarts_per_window: int = 3


@dataclass(frozen=True)
class IndicatorServiceContribution:
    tasks: tuple[IndicatorServiceTask, ...] = ()
    shutdown: Callable[[], Awaitable[Any]] | None = None
