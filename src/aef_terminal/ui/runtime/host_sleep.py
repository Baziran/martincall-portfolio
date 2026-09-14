from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.settings_contract import HOST_SLEEP_SETTING_KEY
from aef_terminal.ui.runtime.constants import (
    HOST_SLEEP_CHECK_SECONDS,
    HOST_SLEEP_GAP_SECONDS,
)
from aef_terminal.ui.services.host_sleep_monitor import run_host_sleep_monitor_loop

_LOGGER = logging.getLogger(__name__)

_HOST_SLEEP_LOCK = threading.Lock()
_HOST_SLEEP_LAST_CHECK = time.monotonic()
_HOST_SLEEP_LAST_WALL = datetime.now(tz=UTC)
_HOST_SLEEP_LAST_GAP: dict[str, Any] | None = None


@dataclass(frozen=True)
class HostSleepDeps:
    store_factory: Callable[[], Any]


_DEPS: HostSleepDeps | None = None


def configure_host_sleep_deps(deps: HostSleepDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> HostSleepDeps:
    if _DEPS is None:
        raise RuntimeError("host sleep dependencies are not configured")
    return _DEPS


def persist_host_sleep_gap(record: dict[str, Any]) -> None:
    store = _deps().store_factory()
    if store is None:
        return
    try:
        store.upsert_setting("server", HOST_SLEEP_SETTING_KEY, record)
    except Exception as exc:
        _LOGGER.warning("Failed to persist host sleep gap: %s", exc)


def host_sleep_status() -> dict[str, Any]:
    last_gap = dict(_HOST_SLEEP_LAST_GAP) if isinstance(_HOST_SLEEP_LAST_GAP, dict) else None
    last_check = _HOST_SLEEP_LAST_WALL
    return {
        "monitoring": True,
        "check_seconds": HOST_SLEEP_CHECK_SECONDS,
        "gap_threshold_seconds": HOST_SLEEP_GAP_SECONDS,
        "last_check_at": last_check.isoformat(),
        "last_gap": last_gap,
    }


def record_host_sleep_gap(
    previous_wall: datetime,
    now_wall: datetime,
    gap_seconds: float,
    *,
    server_sleeping: Callable[[], bool],
    persist: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    global _HOST_SLEEP_LAST_GAP
    record = {
        "detected_at": now_wall.isoformat(),
        "previous_check_at": previous_wall.isoformat(),
        "wall_gap_seconds": round(float(gap_seconds), 3),
        "expected_check_seconds": HOST_SLEEP_CHECK_SECONDS,
        "gap_over_expected_seconds": round(
            max(float(gap_seconds) - HOST_SLEEP_CHECK_SECONDS, 0.0), 3
        ),
        "reason": "host_sleep_or_event_loop_pause",
        "server_sleeping": server_sleeping(),
    }
    with _HOST_SLEEP_LOCK:
        _HOST_SLEEP_LAST_GAP = record
    if persist is not None:
        persist(record)
    else:
        persist_host_sleep_gap(record)
    _LOGGER.warning(
        "HOST_SLEEP_RESUME_DETECTED wall_gap=%.3fs over_expected=%.3fs server_sleeping=%s",
        record["wall_gap_seconds"],
        record["gap_over_expected_seconds"],
        record["server_sleeping"],
    )
    return record


async def host_sleep_monitor_loop(
    *,
    on_gap_detected: Callable[[datetime, datetime, float], dict[str, Any]] | None = None,
    server_sleeping: Callable[[], bool] | None = None,
) -> None:
    gap_handler = on_gap_detected
    if gap_handler is None:
        if server_sleeping is None:
            from aef_terminal.ui.runtime.server_sleep import server_sleeping as _server_sleeping

            server_sleeping = _server_sleeping

        def _default_gap_handler(
            previous_wall: datetime, now_wall: datetime, gap_seconds: float
        ) -> dict[str, Any]:
            return record_host_sleep_gap(
                previous_wall, now_wall, gap_seconds, server_sleeping=server_sleeping
            )

        gap_handler = _default_gap_handler

    def _get_state() -> tuple[float, datetime]:
        return _HOST_SLEEP_LAST_CHECK, _HOST_SLEEP_LAST_WALL

    def _set_state(now_mono: float, now_wall: datetime) -> None:
        global _HOST_SLEEP_LAST_CHECK, _HOST_SLEEP_LAST_WALL
        _HOST_SLEEP_LAST_CHECK = now_mono
        _HOST_SLEEP_LAST_WALL = now_wall

    await run_host_sleep_monitor_loop(
        check_seconds=HOST_SLEEP_CHECK_SECONDS,
        gap_threshold_seconds=HOST_SLEEP_GAP_SECONDS,
        state_lock=_HOST_SLEEP_LOCK,
        get_state=_get_state,
        set_state=_set_state,
        on_gap_detected=gap_handler,
    )
