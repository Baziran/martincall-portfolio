from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, time, timedelta
from threading import Condition, RLock
from time import monotonic
from typing import Any

from aef_terminal.data.economic_calendar import load_economic_calendar_snapshot


_SUCCESS_CACHE_SECONDS = 6 * 60 * 60
_RETRY_CACHE_SECONDS = 15 * 60
_HISTORY_DAYS = 7
_FUTURE_DAYS = 21


class EconomicCalendarRuntime:
    def __init__(
        self,
        *,
        loader: Callable[..., dict[str, Any]] = load_economic_calendar_snapshot,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._loader = loader
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._loading = False
        self._cache_key: tuple[str, str] | None = None
        self._cached_payload: dict[str, Any] | None = None
        self._expires_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        now = self._clock().astimezone(UTC)
        start_date = now.date() - timedelta(days=_HISTORY_DAYS)
        end_date = now.date() + timedelta(days=_FUTURE_DAYS + 1)
        start = datetime.combine(start_date, time.min, tzinfo=UTC)
        end = datetime.combine(end_date, time.min, tzinfo=UTC)
        cache_key = (start.date().isoformat(), end.date().isoformat())
        with self._lock:
            while True:
                if (
                    self._cache_key == cache_key
                    and self._cached_payload is not None
                    and self._monotonic_clock() < self._expires_at
                ):
                    return deepcopy(self._cached_payload)
                if not self._loading:
                    self._loading = True
                    break
                self._condition.wait()

        try:
            payload = self._loader(start=start, end=end, now=now)
        except BaseException:
            with self._lock:
                self._loading = False
                self._condition.notify_all()
            raise
        ttl = _SUCCESS_CACHE_SECONDS if payload.get("status") == "ok" else _RETRY_CACHE_SECONDS
        with self._lock:
            self._cache_key = cache_key
            self._cached_payload = deepcopy(payload)
            self._expires_at = self._monotonic_clock() + ttl
            self._loading = False
            self._condition.notify_all()
        return deepcopy(payload)

    def reset(self) -> None:
        with self._lock:
            self._cache_key = None
            self._cached_payload = None
            self._expires_at = 0.0


_RUNTIME = EconomicCalendarRuntime()


def economic_calendar_snapshot() -> dict[str, Any]:
    return _RUNTIME.snapshot()
