from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.ui.services.chart_stream_messages import (
    CHART_STREAM_CONSUMER_ROLE_PRIMARY,
    ChartStreamConsumerRole,
)


class ChartStreamRuntime:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, str, str, str], int] = {}
        self._internal_streams: dict[tuple[str, str, str, str], int] = {}
        self._clients_lock = threading.Lock()
        self._active_chart: tuple[str, str, str, datetime] | None = None
        self._coordinators: dict[tuple[str, str, str], Any] = {}
        self._coordinator_generations: dict[tuple[str, str, str], int] = {}

    @staticmethod
    def stream_key(
        instrument_id: str,
        interval: str,
        range_: str,
        route_fingerprint: str,
    ) -> tuple[str, str, str, str]:
        return (
            require_exact_identity_text(instrument_id, field="instrument_id"),
            str(interval),
            str(range_ or "").strip().lower(),
            require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
        )

    @staticmethod
    def coordinator_key(
        instrument_id: str,
        interval: str,
        route_fingerprint: str,
    ) -> tuple[str, str, str]:
        key = (
            require_exact_identity_text(instrument_id, field="instrument_id"),
            require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
            str(interval or "").strip(),
        )
        if not all(key):
            raise ValueError("CHART_COORDINATOR_IDENTITY_REQUIRED")
        return key

    def acquire_coordinator(
        self,
        key: tuple[str, str, str],
        factory: Callable[[int], Any],
        *,
        range_: str,
        since_ts: str,
        deliver: bool = True,
        live_tail_bars: int = 64,
        consumer_role: ChartStreamConsumerRole = CHART_STREAM_CONSUMER_ROLE_PRIMARY,
        tail_bars: int | None = None,
    ) -> tuple[Any, Any, bool]:
        with self._clients_lock:
            current = self._coordinators.get(key)
            current_task = getattr(current, "task", None) if current is not None else None
            if current is not None and current_task is not None and current_task.done():
                self._coordinators.pop(key, None)
                current = None
            if current is not None:
                consumer = current.add_consumer(
                    range_,
                    since_ts,
                    deliver=deliver,
                    live_tail_bars=live_tail_bars,
                    consumer_role=consumer_role,
                    tail_bars=tail_bars,
                )
                return current, consumer, False
            generation = self._coordinator_generations.get(key, 0) + 1
            self._coordinator_generations[key] = generation
            if len(self._coordinator_generations) > 512:
                stale_key = next(
                    (
                        candidate
                        for candidate in self._coordinator_generations
                        if candidate not in self._coordinators and candidate != key
                    ),
                    None,
                )
                if stale_key is not None:
                    self._coordinator_generations.pop(stale_key, None)
            coordinator = factory(generation)
            self._coordinators[key] = coordinator
            consumer = coordinator.add_consumer(
                range_,
                since_ts,
                deliver=deliver,
                live_tail_bars=live_tail_bars,
                consumer_role=consumer_role,
                tail_bars=tail_bars,
            )
            return coordinator, consumer, True

    async def release_coordinator(
        self,
        key: tuple[str, str, str],
        coordinator: Any,
        consumer: Any,
        *,
        grace_seconds: float = 0.0,
    ) -> bool:
        grace = max(float(grace_seconds), 0.0)
        if grace:
            await asyncio.sleep(grace)
        with self._clients_lock:
            if self._coordinators.get(key) is not coordinator:
                return False
            remaining = coordinator.remove_consumer(consumer)
            if remaining:
                return False
            self._coordinators.pop(key, None)
            return True

    def stream_started(self, key: tuple[str, str, str, str]) -> int:
        with self._clients_lock:
            self._clients[key] = self._clients.get(key, 0) + 1
            return self._clients[key]

    def stream_finished(self, key: tuple[str, str, str, str]) -> int:
        with self._clients_lock:
            count = max(0, self._clients.get(key, 0) - 1)
            if count:
                self._clients[key] = count
            else:
                self._clients.pop(key, None)
            return count

    def client_count(self, key: tuple[str, str, str, str]) -> int:
        with self._clients_lock:
            return self._clients.get(key, 0)

    def internal_stream_started(
        self,
        key: tuple[str, str, str, str],
    ) -> int:
        with self._clients_lock:
            self._internal_streams[key] = self._internal_streams.get(key, 0) + 1
            return self._internal_streams[key]

    def internal_stream_finished(
        self,
        key: tuple[str, str, str, str],
    ) -> int:
        with self._clients_lock:
            count = max(0, self._internal_streams.get(key, 0) - 1)
            if count:
                self._internal_streams[key] = count
            else:
                self._internal_streams.pop(key, None)
            return count

    def connection_summary(self) -> tuple[int, int]:
        with self._clients_lock:
            chart_clients = sum(max(0, int(count or 0)) for count in self._clients.values())
            chart_streams = len(self._clients)
        return chart_clients, chart_streams

    def active_streams(self) -> list[tuple[str, str, str, int]]:
        with self._clients_lock:
            active = Counter(
                {
                    key: int(count or 0)
                    for key, count in self._clients.items()
                    if int(count or 0) > 0
                }
            )
            active.update(
                {
                    key: int(count or 0)
                    for key, count in self._internal_streams.items()
                    if int(count or 0) > 0
                }
            )
            return [(key[0], key[1], key[2], count) for key, count in active.items() if count > 0]

    def active_interval(self, default: str = "5m") -> str:
        with self._clients_lock:
            intervals = [str(key[1]) for key, count in self._clients.items() if int(count or 0) > 0]
            active = self._active_chart
        if not intervals:
            if active is not None and active[1]:
                return str(active[1])
            return default
        return str(Counter(intervals).most_common(1)[0][0])

    def note_active_chart(self, instrument_id: str, interval: str, range_: str) -> None:
        interval_value = str(interval or "").strip()
        if not interval_value:
            return
        key = (
            require_exact_identity_text(instrument_id, field="instrument_id"),
            interval_value,
            str(range_ or "").strip().lower(),
        )
        with self._clients_lock:
            self._active_chart = (key[0], key[1], str(range_), datetime.now(tz=UTC))

    def status(self) -> dict[str, object]:
        now = datetime.now(tz=UTC)
        with self._clients_lock:
            active = self._active_chart
            hot_coordinators = len(self._coordinators)
            streams = [
                {
                    "instrument_id": key[0],
                    "interval": str(key[1]),
                    "range": str(key[2]),
                    "route_fingerprint": key[3],
                    "clients": int(count or 0),
                }
                for key, count in self._clients.items()
                if int(count or 0) > 0
            ]
            internal_streams = [
                {
                    "instrument_id": key[0],
                    "interval": str(key[1]),
                    "range": str(key[2]),
                    "route_fingerprint": key[3],
                    "consumers": int(count or 0),
                }
                for key, count in self._internal_streams.items()
                if int(count or 0) > 0
            ]
        active_age = (now - active[3]).total_seconds() if active is not None else None
        return {
            "clients": sum(item["clients"] for item in streams),
            "streams": len(streams),
            "hot_coordinators": hot_coordinators,
            "active_streams": streams,
            "internal_streams": internal_streams,
            "internal_consumers": sum(item["consumers"] for item in internal_streams),
            "active_chart": {
                "instrument_id": active[0],
                "interval": active[1],
                "range": active[2],
                "noted_at": active[3].isoformat(),
                "age_seconds": max(active_age or 0.0, 0.0),
            }
            if active is not None
            else None,
        }


chart_stream = ChartStreamRuntime()
