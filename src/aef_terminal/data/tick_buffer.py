from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from aef_terminal.config import AppConfig
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import provider_key_for_source
from aef_terminal.domain import Tick
from aef_terminal.runtime.async_tasks import settle_physical_task
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts


MAX_CONSECUTIVE_DB_ERRORS_BEFORE_DROP = 30
_TICK_FLUSH_STATEMENT_TIMEOUT_MS = 5_000
_TICK_FLUSH_LOCK_TIMEOUT_MS = 1_000
_TICK_PURGE_STATEMENT_TIMEOUT_MS = 30_000
_TICK_PURGE_LOCK_TIMEOUT_MS = 2_000
_TICK_INGEST_QUALITY_LOCK = threading.Lock()
_TICK_INGEST_QUALITY: dict[str, dict[str, Any]] = {}


def tick_ingest_quality(
    provider: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    identity = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    route_key = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    with _TICK_INGEST_QUALITY_LOCK:
        snapshot = dict(_TICK_INGEST_QUALITY.get(provider_key_for_source(provider)) or {})
    drop_events = [
        event
        for event in snapshot.get("drop_events") or ()
        if isinstance(event, dict)
        and event.get("instrument_id") == identity
        and event.get("route_fingerprint") == route_key
        and isinstance(event.get("start_at"), datetime)
        and isinstance(event.get("end_at"), datetime)
        and (start is None or event["end_at"] >= start)
        and (end is None or event["start_at"] <= end)
    ]
    drop_events.sort(key=lambda event: event["end_at"])
    dropped = sum(max(int(event.get("count") or 0), 0) for event in drop_events)
    last_drop = drop_events[-1] if drop_events else {}
    complete = not drop_events and int(snapshot.get("db_consecutive_errors") or 0) == 0
    return {
        "complete": complete,
        "dropped": dropped,
        "drop_in_window": bool(drop_events),
        "coverage": 1.0 if complete else None,
        "last_drop_at": last_drop["end_at"].isoformat() if last_drop else None,
        "last_drop_reason": str(last_drop.get("reason") or ""),
        "db_consecutive_errors": int(snapshot.get("db_consecutive_errors") or 0),
    }


class TickBulkStore(Protocol):
    def write_ticks(self, ticks: Sequence[Tick], provider: str) -> int: ...

    def purge_raw_ticks(
        self, older_than: datetime | None, provider: str, rollup_first: bool = True
    ) -> dict[str, int]: ...


class TickBatchWriteError(RuntimeError):
    """Typed storage outcome for deciding whether a micro-batch may be retried."""

    def __init__(
        self,
        message: str,
        *,
        commit_outcome: Literal["not_committed", "unknown"],
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.commit_outcome = commit_outcome
        self.retryable = bool(retryable)


class TickBulkAggregator:
    """Micro-batch tick writer.

    `add_tick` is intentionally cheap and never touches the database. The background
    flush loop copies buffered ticks into TimescaleDB in batches. `start()` captures
    the event-loop owner; producers on another thread may append under the buffer
    lock, but must wake that owner through `call_soon_threadsafe()`.
    """

    def __init__(
        self,
        store: TickBulkStore,
        provider: str,
        flush_interval: float = 1.0,
        max_batch: int = 500,
        max_flush_batches: int = 20,
        max_buffer: int = 100_000,
        retention_hours: float | None = None,
        purge_interval: float = 3600.0,
        db_workers: int = 4,
    ) -> None:
        self.store = store
        self.provider = provider_key_for_source(provider)
        self.flush_interval = max(float(flush_interval), 0.05)
        self.max_batch = max(int(max_batch), 1)
        self.max_flush_batches = max(int(max_flush_batches), 1)
        self.max_buffer = max(int(max_buffer), self.max_batch)
        self.retention_hours = max(
            float(
                retention_hours if retention_hours is not None else AppConfig().tick_retention_hours
            ),
            0.05,
        )
        self.purge_interval = max(float(purge_interval), 30.0)
        self.db_workers = max(1, min(int(db_workers), 8))
        # Deliberately unbounded at the container level: explicit max_buffer
        # checks own every rejection so deque can never evict a tick silently.
        self._buffer: deque[Tick] = deque()
        self._high_water_depth = 0
        self._lock = threading.Lock()
        self._flush_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._flush_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._written = 0
        self._accepted = 0
        self._dropped = 0
        self._last_drop_at: datetime | None = None
        self._last_drop_reason = ""
        self._drop_events: list[dict[str, Any]] = []
        self._last_flush_at: datetime | None = None
        self._last_purge_at: datetime | None = None
        self._last_purge: dict[str, int] = {
            "delta_rows": 0,
            "profile_rows": 0,
            "deleted_ticks": 0,
            "dropped_chunks": 0,
            "has_more": 0,
        }
        self._db_consecutive_errors = 0
        self._purge_consecutive_errors = 0
        self._last_purge_error = ""
        self._last_error = ""
        self._db_executor = ThreadPoolExecutor(
            max_workers=self.db_workers, thread_name_prefix="tick-db-writer"
        )
        self._db_executor_closed = False
        self._publish_ingest_quality()

    def _publish_ingest_quality(self) -> None:
        with _TICK_INGEST_QUALITY_LOCK:
            _TICK_INGEST_QUALITY[self.provider] = {
                "drop_events": tuple(dict(event) for event in self._drop_events),
                "db_consecutive_errors": self._db_consecutive_errors,
            }

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._closed = False
        if self._db_executor_closed:
            self._db_executor = ThreadPoolExecutor(
                max_workers=self.db_workers, thread_name_prefix="tick-db-writer"
            )
            self._db_executor_closed = False
        self._flush_event = asyncio.Event()
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        with self._lock:
            self._closed = True
        if self._flush_event is not None:
            self._flush_event.set()
        failure: BaseException | None = None
        try:
            if self._task is not None:
                await self._task
            await self._drain_for_shutdown()
        except BaseException as exc:
            failure = exc
        finally:
            if self.buffered_count() > 0:
                self._drop_buffered("shutdown_incomplete")
            self._db_executor.shutdown(wait=False, cancel_futures=False)
            self._db_executor_closed = True
            self._task = None
            self._flush_event = None
            self._loop = None
            self._loop_thread_id = None
        if failure is not None:
            raise failure

    async def _drain_for_shutdown(self) -> None:
        while self.buffered_count() > 0:
            if await self.flush_once() <= 0:
                self._drop_buffered("shutdown_flush_failed")
                return

    def add_tick(
        self,
        ts: datetime,
        route_fingerprint: str,
        price: float,
        volume: int,
        delta_sign: int,
        instrument_id: str,
        tick_type: str = "last",
        bid: float | None = None,
        ask: float | None = None,
        exchange: str = "",
        conditions: tuple[str, ...] | list[str] = (),
    ) -> bool:
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        exact_route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        tick = Tick(
            route_fingerprint=exact_route_fingerprint,
            ts=ts,
            price=price,
            volume=volume,
            delta_sign=delta_sign,
            instrument_id=exact_instrument_id,
            tick_type=tick_type,
            bid=bid,
            ask=ask,
            exchange=exchange,
            conditions=tuple(conditions),
        )
        should_signal = False
        with self._lock:
            if self._closed:
                self._record_drop_locked(1, "writer_stopped", ticks=(tick,))
                return False
            if len(self._buffer) >= self.max_buffer:
                self._record_drop_locked(1, "buffer_full", ticks=(tick,))
                return False
            self._buffer.append(tick)
            depth = len(self._buffer)
            if depth > self._high_water_depth:
                self._high_water_depth = depth
                set_metric("tick_buffer_high_water_depth", depth, provider=self.provider)
            self._accepted += 1
            increment_metric("tick_buffer_accepted_total", provider=self.provider)
            set_metric("tick_buffer_depth", depth, provider=self.provider)
            should_signal = depth >= self.max_batch
        flush_event = self._flush_event
        owner_loop = self._loop
        if (
            should_signal
            and flush_event is not None
            and owner_loop is not None
            and not owner_loop.is_closed()
        ):
            if threading.get_ident() == self._loop_thread_id:
                flush_event.set()
            else:
                try:
                    owner_loop.call_soon_threadsafe(flush_event.set)
                except RuntimeError:
                    # Loop shutdown owns the final synchronous drain in stop().
                    pass
        return True

    async def _flush_loop(self) -> None:
        while not self._closed:
            if self._flush_event is None:
                await asyncio.sleep(self.flush_interval)
            else:
                try:
                    await asyncio.wait_for(self._flush_event.wait(), timeout=self.flush_interval)
                except TimeoutError:
                    pass
                self._flush_event.clear()
            await self.flush_pending()
            await self.purge_once_if_due()

    async def flush_pending(self) -> int:
        total = 0
        for _ in range(self.max_flush_batches):
            written = await self.flush_once()
            if written <= 0:
                break
            total += written
            if self.buffered_count() <= 0:
                break
        if self.buffered_count() >= self.max_batch and self._flush_event is not None:
            self._flush_event.set()
        return total

    async def flush_once(self) -> int:
        async with self._flush_lock:
            batch = self._take_batch()
            if not batch:
                return 0
            started_at = datetime.now(tz=UTC)
            count: Any = None
            write_error: Exception | None = None
            cancellation: asyncio.CancelledError | None = None
            loop = asyncio.get_running_loop()
            with postgres_operation_timeouts(
                statement_timeout_ms=_TICK_FLUSH_STATEMENT_TIMEOUT_MS,
                lock_timeout_ms=_TICK_FLUSH_LOCK_TIMEOUT_MS,
            ):
                write_context = copy_context()
            write_future = loop.run_in_executor(
                self._db_executor,
                write_context.run,
                self.store.write_ticks,
                batch,
                self.provider,
            )
            try:
                write_outcome = await settle_physical_task(write_future)
                count = write_outcome.result
                write_error = write_outcome.error
                cancellation = write_outcome.cancellation
                if write_outcome.task_cancelled:
                    write_error = TickBatchWriteError(
                        "tick batch completion future was cancelled",
                        commit_outcome="unknown",
                        retryable=False,
                    )
            finally:
                observe_metric(
                    "tick_buffer_flush_seconds",
                    max((datetime.now(tz=UTC) - started_at).total_seconds(), 0.0),
                    provider=self.provider,
                )
            if write_error is not None:
                self._db_consecutive_errors += 1
                self._last_error = str(write_error) or write_error.__class__.__name__
                self._publish_ingest_quality()
                increment_metric("tick_buffer_flush_errors_total", provider=self.provider)
                retry_safe = bool(
                    isinstance(write_error, TickBatchWriteError)
                    and write_error.commit_outcome == "not_committed"
                    and write_error.retryable
                )
                if (
                    retry_safe
                    and self._db_consecutive_errors < MAX_CONSECUTIVE_DB_ERRORS_BEFORE_DROP
                ):
                    self._restore_batch(batch)
                elif retry_safe:
                    self._record_drop(len(batch), "db_error_circuit_open", ticks=batch)
                else:
                    drop_reason = (
                        "db_write_rejected"
                        if isinstance(write_error, TickBatchWriteError)
                        and write_error.commit_outcome == "not_committed"
                        else "db_commit_unknown"
                    )
                    self._record_drop(len(batch), drop_reason, ticks=batch)
                if cancellation is not None:
                    raise cancellation
                return 0
            if isinstance(count, bool) or not isinstance(count, int) or count != len(batch):
                self._db_consecutive_errors += 1
                self._last_error = (
                    f"tick batch acknowledgement mismatch: expected {len(batch)}, got {count!r}"
                )
                increment_metric("tick_buffer_flush_errors_total", provider=self.provider)
                increment_metric("tick_buffer_ack_mismatch_total", provider=self.provider)
                # The COPY contract is atomic, but an invalid acknowledgement leaves
                # the commit outcome unknown. Retrying could duplicate ticks, so fail
                # closed and expose the exact affected identities as typed loss.
                self._record_drop(len(batch), "db_ack_mismatch", ticks=batch)
                if cancellation is not None:
                    raise cancellation
                return 0
            recovered_from_db_error = self._db_consecutive_errors > 0
            self._written += count
            increment_metric("tick_buffer_written_total", count, provider=self.provider)
            set_metric("tick_buffer_depth", self.buffered_count(), provider=self.provider)
            self._db_consecutive_errors = 0
            self._last_error = self._last_purge_error
            if recovered_from_db_error:
                self._publish_ingest_quality()
            self._last_flush_at = datetime.now(tz=batch[-1].ts.tzinfo or UTC)
            if cancellation is not None:
                raise cancellation
            return count

    async def purge_once_if_due(self, force: bool = False) -> dict[str, int]:
        now = datetime.now(tz=UTC)
        if (
            not force
            and self._last_purge_at is not None
            and (now - self._last_purge_at).total_seconds() < self.purge_interval
        ):
            return self._last_purge
        cutoff = now - timedelta(hours=self.retention_hours)
        loop = asyncio.get_running_loop()
        with postgres_operation_timeouts(
            statement_timeout_ms=_TICK_PURGE_STATEMENT_TIMEOUT_MS,
            lock_timeout_ms=_TICK_PURGE_LOCK_TIMEOUT_MS,
        ):
            purge_context = copy_context()
        purge_future = loop.run_in_executor(
            self._db_executor,
            purge_context.run,
            self.store.purge_raw_ticks,
            cutoff,
            self.provider,
            True,
        )
        purge_outcome = await settle_physical_task(purge_future)
        purge_error = purge_outcome.error
        if purge_outcome.task_cancelled:
            purge_error = RuntimeError("tick purge completion future was cancelled")
        if purge_error is not None:
            self._record_purge_error(str(purge_error) or purge_error.__class__.__name__)
            if purge_outcome.cancellation is not None:
                raise purge_outcome.cancellation from purge_error
            return self._last_purge
        result = purge_outcome.result
        if not isinstance(result, dict):
            self._record_purge_error("tick purge acknowledgement must be an object")
            if purge_outcome.cancellation is not None:
                raise purge_outcome.cancellation
            return self._last_purge
        self._purge_consecutive_errors = 0
        self._last_purge_error = ""
        if self._db_consecutive_errors == 0:
            self._last_error = ""
        self._last_purge = {key: int(value or 0) for key, value in result.items()}
        has_more = bool(self._last_purge.get("has_more"))
        self._last_purge_at = None if has_more else now
        if has_more and self._flush_event is not None:
            self._flush_event.set()
        if purge_outcome.cancellation is not None:
            raise purge_outcome.cancellation
        return self._last_purge

    def _record_purge_error(self, message: str) -> None:
        self._purge_consecutive_errors += 1
        self._last_purge_error = str(message or "tick purge failed")
        self._last_error = self._last_purge_error
        increment_metric("tick_buffer_purge_errors_total", provider=self.provider)

    def _take_batch(self) -> list[Tick]:
        with self._lock:
            if not self._buffer:
                return []
            count = min(len(self._buffer), self.max_batch)
            batch = [self._buffer.popleft() for _ in range(count)]
            set_metric("tick_buffer_depth", len(self._buffer), provider=self.provider)
            set_metric("tick_buffer_flush_batch_size", count, provider=self.provider)
            set_metric(
                "tick_buffer_flush_batch_fill_ratio",
                count / max(float(self.max_batch), 1.0),
                provider=self.provider,
            )
            return batch

    def buffered_count(self) -> int:
        with self._lock:
            return len(self._buffer)

    def _restore_batch(self, batch: list[Tick]) -> None:
        with self._lock:
            # Restored ticks predate any ticks accepted while the DB call was in
            # flight, so they must be prepended oldest-first. Every tick that no
            # longer fits is recorded explicitly; accepted data may never vanish
            # from the buffer without typed loss accounting.
            room = max(self.max_buffer - len(self._buffer), 0)
            restored = batch[:room]
            not_restored = batch[room:]
            if restored:
                self._buffer.extendleft(reversed(restored))
                depth = len(self._buffer)
                if depth > self._high_water_depth:
                    self._high_water_depth = depth
                    set_metric("tick_buffer_high_water_depth", depth, provider=self.provider)
                set_metric("tick_buffer_depth", depth, provider=self.provider)
            if not_restored:
                self._record_drop_locked(
                    len(not_restored),
                    "restore_overflow",
                    ticks=not_restored,
                )

    def _record_drop(self, count: int, reason: str, *, ticks: Sequence[Tick] = ()) -> None:
        with self._lock:
            self._record_drop_locked(count, reason, ticks=ticks)

    def _drop_buffered(self, reason: str) -> None:
        with self._lock:
            ticks = tuple(self._buffer)
            self._buffer.clear()
            self._record_drop_locked(len(ticks), reason, ticks=ticks)

    def _record_drop_locked(self, count: int, reason: str, *, ticks: Sequence[Tick] = ()) -> None:
        dropped = max(int(count), 0)
        if dropped <= 0:
            return
        self._dropped += dropped
        increment_metric(
            "tick_buffer_dropped_total",
            dropped,
            provider=self.provider,
            reason=str(reason or "unknown"),
        )
        set_metric("tick_buffer_depth", len(self._buffer), provider=self.provider)
        self._last_drop_at = datetime.now(tz=UTC)
        self._last_drop_reason = str(reason or "unknown")
        route_losses: dict[tuple[str, str], dict[str, Any]] = {}
        for tick in ticks:
            identity_key = (tick.instrument_id, tick.route_fingerprint)
            tick_ts = tick.ts.astimezone(UTC)
            loss = route_losses.setdefault(
                identity_key,
                {"count": 0, "start_at": tick_ts, "end_at": tick_ts},
            )
            loss["count"] += 1
            loss["start_at"] = min(loss["start_at"], tick_ts)
            loss["end_at"] = max(loss["end_at"], tick_ts)
        if not route_losses:
            route_losses[("", "")] = {
                "count": dropped,
                "start_at": self._last_drop_at,
                "end_at": self._last_drop_at,
            }
        cutoff = self._last_drop_at - timedelta(hours=self.retention_hours)
        self._drop_events = [event for event in self._drop_events if event["end_at"] >= cutoff]
        for (instrument_id, route), loss in route_losses.items():
            previous = next(
                (
                    event
                    for event in reversed(self._drop_events)
                    if event.get("instrument_id") == instrument_id
                    and event["route_fingerprint"] == route
                    and event["reason"] == self._last_drop_reason
                    and loss["start_at"] <= event["end_at"] + timedelta(minutes=1)
                    and loss["end_at"] >= event["start_at"] - timedelta(minutes=1)
                ),
                None,
            )
            if previous is not None:
                self._drop_events.remove(previous)
                previous["start_at"] = min(previous["start_at"], loss["start_at"])
                previous["end_at"] = max(previous["end_at"], loss["end_at"])
                previous["count"] += loss["count"]
                self._drop_events.append(previous)
            else:
                self._drop_events.append(
                    {
                        "start_at": loss["start_at"],
                        "end_at": loss["end_at"],
                        "instrument_id": instrument_id,
                        "route_fingerprint": route,
                        "reason": self._last_drop_reason,
                        "count": loss["count"],
                    }
                )
        self._publish_ingest_quality()

    def _health(self, buffered: int) -> dict[str, Any]:
        buffer_ratio = buffered / max(float(self.max_buffer), 1.0)
        drop_ratio = self._dropped / max(float(self._accepted + self._dropped), 1.0)
        if self._dropped > 0:
            severity = "error"
            reason = self._last_drop_reason or "dropped_ticks"
            message = f"Dropped {self._dropped} tick(s); latest reason: {reason}."
        elif self._db_consecutive_errors >= MAX_CONSECUTIVE_DB_ERRORS_BEFORE_DROP:
            severity = "error"
            reason = "db_error_circuit_open"
            message = "Tick DB writer circuit is open; batches are being dropped."
        elif self._db_consecutive_errors > 0:
            severity = "warn"
            reason = "db_errors"
            message = f"Tick DB writer has {self._db_consecutive_errors} consecutive error(s)."
        elif self._purge_consecutive_errors > 0:
            severity = "warn"
            reason = "purge_errors"
            message = (
                f"Tick retention has {self._purge_consecutive_errors} consecutive purge error(s)."
            )
        elif buffer_ratio >= 0.8:
            severity = "warn"
            reason = "buffer_high"
            message = f"Tick buffer is {buffer_ratio:.0%} full."
        else:
            severity = "ok"
            reason = "ok"
            message = "Tick writer is healthy."
        return {
            "severity": severity,
            "reason": reason,
            "message": message,
            "buffer_ratio": round(buffer_ratio, 4),
            "drop_ratio": round(drop_ratio, 6),
            "circuit_breaker_threshold": MAX_CONSECUTIVE_DB_ERRORS_BEFORE_DROP,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            buffered = len(self._buffer)
            buffer_high_water = self._high_water_depth
        return {
            "provider": self.provider,
            "buffered": buffered,
            "accepted": self._accepted,
            "written": self._written,
            "dropped": self._dropped,
            "max_batch": self.max_batch,
            "max_flush_batches": self.max_flush_batches,
            "max_buffer": self.max_buffer,
            "buffer_high_water": buffer_high_water,
            "flush_interval": self.flush_interval,
            "retention_hours": self.retention_hours,
            "purge_interval": self.purge_interval,
            "last_flush_at": self._last_flush_at.isoformat() if self._last_flush_at else "",
            "last_drop_at": self._last_drop_at.isoformat() if self._last_drop_at else "",
            "last_drop_reason": self._last_drop_reason,
            "last_purge_at": self._last_purge_at.isoformat() if self._last_purge_at else "",
            "last_purge": self._last_purge,
            "db_consecutive_errors": self._db_consecutive_errors,
            "purge_consecutive_errors": self._purge_consecutive_errors,
            "last_purge_error": self._last_purge_error,
            "last_error": self._last_error,
            "health": self._health(buffered),
        }
