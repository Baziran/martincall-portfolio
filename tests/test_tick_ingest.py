import asyncio
import inspect
import threading
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aef_terminal.data import ibkr_tick_feed as tick_feed_module
from aef_terminal.data import tick_buffer as tick_buffer_module
from aef_terminal.data.ibkr_tick_feed import IbkrTickFeed, clean_price, parse_conditions
from aef_terminal.data.instrument_identity import qualified_instrument_id, route_fingerprint
from aef_terminal.data.tick_buffer import (
    TickBatchWriteError,
    TickBulkAggregator,
    tick_ingest_quality,
)
from aef_terminal.domain import Bar, BarProvenance, Tick
from aef_terminal.runtime import storage_deadlines
from aef_terminal.storage.postgres import (
    PostgresStore,
    safe_tick_bucket,
    safe_tick_history_interval,
)
from aef_terminal.storage.repos import ticks as tick_repo_module
from aef_terminal.storage.repos.bars import _storage_bar_allowed, _storage_bar_reject_reason
from aef_terminal.ui import app as ui_app
from aef_terminal.ui.tick_aggregates import tick_profile_imbalance_groups, tick_profile_value_area
from aef_terminal.ui.tick_services import tick_context_payload as _tick_context_payload
from aef_terminal.ui.runtime import tick_live as tick_live_runtime
from tests.provider_payloads import ibkr_future_payload
from tests.route_helpers import app_route_endpoint

ES_INSTRUMENT = ibkr_future_payload("ES")
NQ_INSTRUMENT = ibkr_future_payload("NQ")
ES_ID = qualified_instrument_id(ES_INSTRUMENT)
NQ_ID = qualified_instrument_id(NQ_INSTRUMENT)
ES_ROUTE = route_fingerprint(ES_INSTRUMENT)
NQ_ROUTE = route_fingerprint(NQ_INSTRUMENT)


@pytest.fixture(autouse=True)
def _reset_tick_ingest_quality() -> None:
    with tick_buffer_module._TICK_INGEST_QUALITY_LOCK:
        tick_buffer_module._TICK_INGEST_QUALITY.clear()


class FakeTickStore:
    def __init__(
        self,
        acknowledge: int | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.rows: list[Tick] = []
        self.acknowledge = acknowledge
        self.started = started
        self.release = release
        self.calls = 0
        self.lock = threading.Lock()

    def initialize(self) -> None:
        pass

    def write_ticks(self, ticks, provider="ibkr") -> int:
        with self.lock:
            self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
        with self.lock:
            self.rows.extend(ticks)
        return len(ticks) if self.acknowledge is None else self.acknowledge

    def purge_raw_ticks(
        self,
        _cutoff,
        provider="ibkr",
        rollup=True,
    ) -> dict[str, int]:
        return {
            "delta_rows": 0,
            "profile_rows": 0,
            "deleted_ticks": 0,
        }


class FailingTickStore:
    def __init__(
        self,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.started = started
        self.release = release
        self.error = error

    def write_ticks(self, ticks, provider="ibkr") -> int:
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
        if self.error is not None:
            raise self.error
        raise TickBatchWriteError(
            "db down",
            commit_outcome="not_committed",
            retryable=True,
        )


class DeadlineAwareTickStore:
    def __init__(
        self,
        *,
        purge_started: threading.Event | None = None,
        purge_release: threading.Event | None = None,
    ) -> None:
        self.deadlines: list[tuple[str, tuple[int, int] | None]] = []
        self.purge_started = purge_started
        self.purge_release = purge_release

    def write_ticks(self, ticks, provider="ibkr") -> int:
        self.deadlines.append(
            (
                threading.current_thread().name,
                storage_deadlines.POSTGRES_OPERATION_TIMEOUTS.get(),
            )
        )
        return len(ticks)

    def purge_raw_ticks(
        self,
        _cutoff,
        provider="ibkr",
        rollup=True,
    ) -> dict[str, int]:
        self.deadlines.append(
            (
                threading.current_thread().name,
                storage_deadlines.POSTGRES_OPERATION_TIMEOUTS.get(),
            )
        )
        if self.purge_started is not None:
            self.purge_started.set()
        if self.purge_release is not None:
            assert self.purge_release.wait(timeout=2)
        return {"delta_rows": 2, "profile_rows": 3, "deleted_ticks": 5}


def test_tick_writer_requires_explicit_provider_before_any_work() -> None:
    with pytest.raises(TypeError, match="provider"):
        TickBulkAggregator(FakeTickStore())
    with pytest.raises(TypeError, match="provider"):
        tick_repo_module.TicksRepoMixin().write_ticks([])
    with pytest.raises(TypeError, match="provider"):
        tick_repo_module.TicksRepoMixin().rollup_ticks()


def test_tick_db_workers_receive_physical_postgres_deadlines() -> None:
    async def run() -> None:
        store = DeadlineAwareTickStore()
        buffer = TickBulkAggregator(store, provider="ibkr", max_batch=1, flush_interval=60)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(
            now,
            ES_ROUTE,
            5000.25,
            3,
            1,
            instrument_id=ES_ID,
        )

        assert await buffer.flush_once() == 1
        assert await buffer.purge_once_if_due(force=True) == {
            "delta_rows": 2,
            "profile_rows": 3,
            "deleted_ticks": 5,
        }
        assert [deadlines for _thread_name, deadlines in store.deadlines] == [
            (
                tick_buffer_module._TICK_FLUSH_STATEMENT_TIMEOUT_MS,
                tick_buffer_module._TICK_FLUSH_LOCK_TIMEOUT_MS,
            ),
            (
                tick_buffer_module._TICK_PURGE_STATEMENT_TIMEOUT_MS,
                tick_buffer_module._TICK_PURGE_LOCK_TIMEOUT_MS,
            ),
        ]
        assert all(
            thread_name.startswith("tick-db-writer") for thread_name, _deadlines in store.deadlines
        )
        await buffer.stop()

    asyncio.run(run())


def test_tick_purge_settles_physical_worker_before_propagating_cancellation() -> None:
    async def run() -> None:
        purge_started = threading.Event()
        purge_release = threading.Event()
        buffer = TickBulkAggregator(
            DeadlineAwareTickStore(
                purge_started=purge_started,
                purge_release=purge_release,
            ),
            provider="ibkr",
            flush_interval=60,
        )

        purge_task = asyncio.create_task(buffer.purge_once_if_due(force=True))
        assert await asyncio.to_thread(purge_started.wait, 1)
        purge_task.cancel()
        await asyncio.sleep(0)
        assert purge_task.done() is False

        purge_release.set()
        with pytest.raises(asyncio.CancelledError):
            await purge_task
        assert buffer.status()["last_purge"] == {
            "delta_rows": 2,
            "profile_rows": 3,
            "deleted_ticks": 5,
        }
        await buffer.stop()

    asyncio.run(run())


def test_tick_purge_backlog_remains_due_until_bounded_batches_finish() -> None:
    class BacklogTickStore:
        def __init__(self) -> None:
            self.calls = 0

        def write_ticks(self, ticks, provider="ibkr") -> int:
            return len(ticks)

        def purge_raw_ticks(
            self,
            _cutoff,
            provider="ibkr",
            rollup=True,
        ) -> dict[str, int]:
            self.calls += 1
            return {
                "delta_rows": 1,
                "profile_rows": 1,
                "deleted_ticks": 50_000 if self.calls == 1 else 7,
                "has_more": 1 if self.calls == 1 else 0,
            }

    async def run() -> None:
        store = BacklogTickStore()
        buffer = TickBulkAggregator(store, provider="ibkr", purge_interval=3600)

        first = await buffer.purge_once_if_due(force=True)
        second = await buffer.purge_once_if_due()

        assert first["has_more"] == 1
        assert second["has_more"] == 0
        assert store.calls == 2
        await buffer.stop()

    asyncio.run(run())


def test_tick_purge_failure_remains_visible_until_retention_recovers() -> None:
    class RecoveringPurgeStore(FakeTickStore):
        def __init__(self) -> None:
            super().__init__()
            self.purge_fails = True

        def purge_raw_ticks(
            self,
            _cutoff,
            provider="ibkr",
            rollup=True,
        ) -> dict[str, int]:
            if self.purge_fails:
                raise RuntimeError("purge timed out")
            return {
                "delta_rows": 1,
                "profile_rows": 2,
                "deleted_ticks": 3,
                "has_more": 0,
            }

    async def run() -> None:
        store = RecoveringPurgeStore()
        buffer = TickBulkAggregator(store, provider="ibkr", max_batch=1, flush_interval=60)

        assert await buffer.purge_once_if_due(force=True) == {
            "delta_rows": 0,
            "profile_rows": 0,
            "deleted_ticks": 0,
            "dropped_chunks": 0,
            "has_more": 0,
        }
        failed = buffer.status()
        assert failed["db_consecutive_errors"] == 0
        assert failed["purge_consecutive_errors"] == 1
        assert failed["last_purge_error"] == "purge timed out"
        assert failed["last_error"] == "purge timed out"
        assert failed["health"]["severity"] == "warn"
        assert failed["health"]["reason"] == "purge_errors"

        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(
            now,
            ES_ROUTE,
            5000.25,
            1,
            1,
            instrument_id=ES_ID,
        )
        assert await buffer.flush_once() == 1
        assert buffer.status()["health"]["reason"] == "purge_errors"

        store.purge_fails = False
        assert (await buffer.purge_once_if_due(force=True))["deleted_ticks"] == 3
        recovered = buffer.status()
        assert recovered["purge_consecutive_errors"] == 0
        assert recovered["last_purge_error"] == ""
        assert recovered["last_error"] == ""
        assert recovered["health"]["severity"] == "ok"
        await buffer.stop()

    asyncio.run(run())


def test_tick_bulk_aggregator_flushes_micro_batch() -> None:
    async def run() -> None:
        store = FakeTickStore()
        buffer = TickBulkAggregator(store, provider="ibkr", max_batch=2, flush_interval=60)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
        assert buffer.add_tick(now, ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID)
        written = await buffer.flush_once()
        assert written == 2
        assert len(store.rows) == 2
        assert store.rows[0].delta_sign == 1
        assert buffer.status()["buffered"] == 0

    asyncio.run(run())


def test_tick_bulk_aggregator_wakes_owner_loop_from_foreign_producer_thread() -> None:
    async def run() -> None:
        store = FakeTickStore()
        buffer = TickBulkAggregator(store, provider="ibkr", max_batch=1, flush_interval=60)
        buffer.start()
        now = datetime(2026, 1, 1, tzinfo=UTC)

        accepted = await asyncio.to_thread(
            buffer.add_tick,
            now,
            ES_ROUTE,
            5000.25,
            3,
            1,
            ES_ID,
        )
        assert accepted is True
        for _ in range(100):
            if buffer.status()["written"] == 1:
                break
            await asyncio.sleep(0.01)

        assert buffer.status()["written"] == 1
        assert len(store.rows) == 1
        await buffer.stop()

    asyncio.run(run())


def test_tick_bulk_aggregator_rejects_non_exact_batch_acknowledgement() -> None:
    async def run() -> None:
        provider = "ibkr"
        store = FakeTickStore(acknowledge=1)
        buffer = TickBulkAggregator(store, provider=provider, max_batch=2, flush_interval=60)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
        assert buffer.add_tick(
            now + timedelta(seconds=1), ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID
        )

        assert await buffer.flush_once() == 0

        status = buffer.status()
        assert len(store.rows) == 2
        assert status["buffered"] == 0
        assert status["written"] == 0
        assert status["dropped"] == 2
        assert status["last_drop_reason"] == "db_ack_mismatch"
        assert status["db_consecutive_errors"] == 1
        assert status["last_error"] == "tick batch acknowledgement mismatch: expected 2, got 1"
        quality = tick_ingest_quality(
            provider,
            instrument_id=ES_ID,
            route_fingerprint=ES_ROUTE,
            start=now - timedelta(seconds=1),
        )
        assert quality["complete"] is False
        assert quality["dropped"] == 2
        assert quality["last_drop_reason"] == "db_ack_mismatch"

    asyncio.run(run())


def test_tick_bulk_aggregator_never_retries_unknown_commit_outcome() -> None:
    async def run() -> None:
        provider = "ibkr"
        buffer = TickBulkAggregator(
            FailingTickStore(error=RuntimeError("connection lost after COPY")),
            provider=provider,
            max_batch=2,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
        assert buffer.add_tick(
            now + timedelta(seconds=1), ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID
        )

        assert await buffer.flush_once() == 0

        status = buffer.status()
        assert status["buffered"] == 0
        assert status["written"] == 0
        assert status["dropped"] == 2
        assert status["last_drop_reason"] == "db_commit_unknown"
        assert status["db_consecutive_errors"] == 1
        quality = tick_ingest_quality(
            provider,
            instrument_id=ES_ID,
            route_fingerprint=ES_ROUTE,
            start=now - timedelta(seconds=1),
        )
        assert quality["complete"] is False
        assert quality["dropped"] == 2
        assert quality["last_drop_reason"] == "db_commit_unknown"

    asyncio.run(run())


def test_tick_bulk_aggregator_resolves_inflight_write_before_propagating_cancellation() -> None:
    async def run() -> None:
        started = threading.Event()
        release = threading.Event()
        store = FakeTickStore(started=started, release=release)
        buffer = TickBulkAggregator(
            store,
            provider="ibkr",
            max_batch=2,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
        assert buffer.add_tick(
            now + timedelta(seconds=1), ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID
        )

        flush_task = asyncio.create_task(buffer.flush_once())
        assert await asyncio.to_thread(started.wait, 1)
        flush_task.cancel()
        await asyncio.sleep(0)
        assert flush_task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await flush_task

        status = buffer.status()
        assert len(store.rows) == 2
        assert status["buffered"] == 0
        assert status["written"] == 2
        assert status["dropped"] == 0

    asyncio.run(run())


def test_tick_bulk_aggregator_serializes_public_flush_calls() -> None:
    async def run() -> None:
        started = threading.Event()
        release = threading.Event()
        store = FakeTickStore(started=started, release=release)
        buffer = TickBulkAggregator(
            store,
            provider="ibkr",
            max_batch=2,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(4):
            assert buffer.add_tick(
                now + timedelta(seconds=index),
                ES_ROUTE,
                5000.0 + index,
                1,
                1,
                instrument_id=ES_ID,
            )

        first = asyncio.create_task(buffer.flush_once())
        second = asyncio.create_task(buffer.flush_once())
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0)
        with store.lock:
            assert store.calls == 1
        release.set()

        assert await asyncio.gather(first, second) == [2, 2]
        assert [tick.price for tick in store.rows] == [5000.0, 5001.0, 5002.0, 5003.0]

    asyncio.run(run())


def test_tick_bulk_aggregator_failed_flush_restores_oldest_first_and_accounts_overflow() -> None:
    async def run() -> None:
        provider = "ibkr"
        started = threading.Event()
        release = threading.Event()
        buffer = TickBulkAggregator(
            FailingTickStore(started, release),
            provider=provider,
            max_batch=4,
            max_buffer=5,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        original_routes = (
            (ES_ID, ES_ROUTE),
            (NQ_ID, NQ_ROUTE),
            (ES_ID, ES_ROUTE),
            (NQ_ID, NQ_ROUTE),
        )
        for index, (instrument_id, route) in enumerate(original_routes):
            assert buffer.add_tick(
                now + timedelta(seconds=index),
                route,
                5000.0 + index,
                1,
                1,
                instrument_id=instrument_id,
            )

        flush_task = asyncio.create_task(buffer.flush_once())
        assert await asyncio.to_thread(started.wait, 1)
        for index in range(4):
            assert buffer.add_tick(
                now + timedelta(seconds=10 + index),
                ES_ROUTE,
                6000.0 + index,
                1,
                1,
                instrument_id=ES_ID,
            )
        release.set()

        assert await flush_task == 0
        assert [tick.price for tick in buffer._buffer] == [
            5000.0,
            6000.0,
            6001.0,
            6002.0,
            6003.0,
        ]
        status = buffer.status()
        assert status["buffered"] == 5
        assert status["accepted"] == 8
        assert status["written"] == 0
        assert status["dropped"] == 3
        assert status["last_drop_reason"] == "restore_overflow"
        es_quality = tick_ingest_quality(
            provider,
            instrument_id=ES_ID,
            route_fingerprint=ES_ROUTE,
            start=now,
        )
        nq_quality = tick_ingest_quality(
            provider,
            instrument_id=NQ_ID,
            route_fingerprint=NQ_ROUTE,
            start=now,
        )
        assert es_quality["complete"] is False
        assert es_quality["dropped"] == 1
        assert nq_quality["complete"] is False
        assert nq_quality["dropped"] == 2

    asyncio.run(run())


def test_tick_bulk_aggregator_restores_available_room_above_health_threshold() -> None:
    async def run() -> None:
        provider = "ibkr"
        started = threading.Event()
        release = threading.Event()
        buffer = TickBulkAggregator(
            FailingTickStore(started, release),
            provider=provider,
            max_batch=2,
            max_buffer=10,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.0, 1, 1, instrument_id=ES_ID)
        assert buffer.add_tick(
            now + timedelta(seconds=1),
            NQ_ROUTE,
            20000.0,
            1,
            1,
            instrument_id=NQ_ID,
        )

        flush_task = asyncio.create_task(buffer.flush_once())
        assert await asyncio.to_thread(started.wait, 1)
        for index in range(9):
            assert buffer.add_tick(
                now + timedelta(seconds=10 + index),
                ES_ROUTE,
                6000.0 + index,
                1,
                1,
                instrument_id=ES_ID,
            )
        release.set()

        assert await flush_task == 0
        assert [tick.price for tick in buffer._buffer] == [
            5000.0,
            6000.0,
            6001.0,
            6002.0,
            6003.0,
            6004.0,
            6005.0,
            6006.0,
            6007.0,
            6008.0,
        ]
        status = buffer.status()
        assert status["buffered"] == 10
        assert status["buffer_high_water"] == 10
        assert status["dropped"] == 1
        assert status["last_drop_reason"] == "restore_overflow"
        es_quality = tick_ingest_quality(
            provider,
            instrument_id=ES_ID,
            route_fingerprint=ES_ROUTE,
            start=now,
        )
        nq_quality = tick_ingest_quality(
            provider,
            instrument_id=NQ_ID,
            route_fingerprint=NQ_ROUTE,
            start=now,
        )
        assert es_quality["complete"] is False
        assert es_quality["dropped"] == 0
        assert es_quality["drop_in_window"] is False
        assert es_quality["db_consecutive_errors"] == 1
        assert nq_quality["complete"] is False
        assert nq_quality["dropped"] == 1

    asyncio.run(run())


def test_tick_bulk_aggregator_circuit_breaker_drops_after_repeated_db_errors() -> None:
    async def run() -> None:
        buffer = TickBulkAggregator(
            FailingTickStore(), provider="ibkr", max_batch=2, flush_interval=60
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
        assert buffer.add_tick(now, ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID)

        for _ in range(29):
            assert await buffer.flush_once() == 0
            assert buffer.status()["buffered"] == 2
        assert await buffer.flush_once() == 0

        status = buffer.status()
        assert status["buffered"] == 0
        assert status["accepted"] == 2
        assert status["dropped"] == 2
        assert status["last_drop_reason"] == "db_error_circuit_open"
        assert status["health"]["severity"] == "error"
        assert status["health"]["reason"] == "db_error_circuit_open"
        assert status["db_consecutive_errors"] == 30
        assert status["last_error"] == "db down"

    asyncio.run(run())


def test_tick_bulk_aggregator_status_warns_before_db_drop_threshold() -> None:
    async def run() -> None:
        buffer = TickBulkAggregator(
            FailingTickStore(), provider="ibkr", max_batch=2, flush_interval=60
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
        assert buffer.add_tick(now, ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID)

        assert await buffer.flush_once() == 0

        status = buffer.status()
        assert status["buffered"] == 2
        assert status["dropped"] == 0
        assert status["health"]["severity"] == "warn"
        assert status["health"]["reason"] == "db_errors"

    asyncio.run(run())


def test_tick_bulk_aggregator_status_reports_buffer_full_drop() -> None:
    buffer = TickBulkAggregator(
        FakeTickStore(), provider="ibkr", max_batch=2, max_buffer=2, flush_interval=60
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert buffer.add_tick(now, ES_ROUTE, 5000.25, 3, 1, instrument_id=ES_ID)
    assert buffer.add_tick(now, ES_ROUTE, 5000.00, 2, -1, instrument_id=ES_ID)
    assert buffer.add_tick(now, NQ_ROUTE, 4999.75, 1, -1, instrument_id=NQ_ID) is False

    status = buffer.status()
    assert status["buffered"] == 2
    assert status["accepted"] == 2
    assert status["dropped"] == 1
    assert status["last_drop_reason"] == "buffer_full"
    assert status["health"]["severity"] == "error"
    assert status["health"]["reason"] == "buffer_full"

    query_started_at = now - timedelta(seconds=1)
    es_quality = tick_ingest_quality(
        "ibkr",
        instrument_id=ES_ID,
        route_fingerprint=ES_ROUTE,
        start=query_started_at,
    )
    nq_quality = tick_ingest_quality(
        "ibkr",
        instrument_id=NQ_ID,
        route_fingerprint=NQ_ROUTE,
        start=query_started_at,
    )
    assert es_quality["complete"] is True
    assert es_quality["dropped"] == 0
    assert nq_quality["complete"] is False
    assert nq_quality["dropped"] == 1
    assert nq_quality["coverage"] is None


def test_tick_bulk_aggregator_drains_multiple_batches_per_wake() -> None:
    async def run() -> None:
        store = FakeTickStore()
        buffer = TickBulkAggregator(
            store, provider="ibkr", max_batch=2, max_flush_batches=3, flush_interval=60
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(5):
            assert buffer.add_tick(
                now,
                ES_ROUTE,
                5000.0 + index * 0.25,
                1,
                1,
                instrument_id=ES_ID,
            )

        written = await buffer.flush_pending()

        assert written == 5
        assert len(store.rows) == 5
        assert buffer.status()["buffered"] == 0
        assert buffer.status()["max_flush_batches"] == 3

    asyncio.run(run())


def test_tick_bulk_aggregator_shutdown_drains_beyond_realtime_batch_budget() -> None:
    async def run() -> None:
        store = FakeTickStore()
        buffer = TickBulkAggregator(
            store,
            provider="ibkr",
            max_batch=2,
            max_flush_batches=3,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(25):
            assert buffer.add_tick(
                now + timedelta(microseconds=index),
                ES_ROUTE,
                5000.0 + index * 0.25,
                1,
                1,
                instrument_id=ES_ID,
            )

        await buffer.stop()

        assert len(store.rows) == 25
        assert buffer.status()["accepted"] == 25
        assert buffer.status()["written"] == 25
        assert buffer.status()["buffered"] == 0
        assert buffer.status()["dropped"] == 0

    asyncio.run(run())


def test_tick_bulk_aggregator_shutdown_accounts_for_unwritable_backlog() -> None:
    async def run() -> None:
        buffer = TickBulkAggregator(
            FailingTickStore(),
            provider="ibkr",
            max_batch=2,
            max_flush_batches=1,
            flush_interval=60,
        )
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(3):
            assert buffer.add_tick(
                now + timedelta(microseconds=index),
                ES_ROUTE,
                5000.0 + index * 0.25,
                1,
                1,
                instrument_id=ES_ID,
            )

        await buffer.stop()

        status = buffer.status()
        assert status["accepted"] == 3
        assert status["written"] == 0
        assert status["buffered"] == 0
        assert status["dropped"] == 3
        assert status["last_drop_reason"] == "shutdown_flush_failed"
        assert (
            tick_ingest_quality(
                "ibkr",
                instrument_id=ES_ID,
                route_fingerprint=ES_ROUTE,
                start=now - timedelta(seconds=1),
            )["dropped"]
            == 3
        )

    asyncio.run(run())


def test_tick_bulk_aggregator_rejects_ticks_after_shutdown_with_typed_loss() -> None:
    async def run() -> None:
        buffer = TickBulkAggregator(FakeTickStore(), provider="ibkr", flush_interval=60)
        await buffer.stop()

        accepted = buffer.add_tick(
            datetime(2026, 1, 1, tzinfo=UTC),
            ES_ROUTE,
            5000.0,
            1,
            1,
            instrument_id=ES_ID,
        )

        assert accepted is False
        assert buffer.status()["last_drop_reason"] == "writer_stopped"
        assert buffer.status()["dropped"] == 1

    asyncio.run(run())


def test_tick_delta_classifier_uses_bidask_then_tick_test() -> None:
    instrument = ibkr_future_payload("ES")
    fingerprint = route_fingerprint(instrument)
    identity = (qualified_instrument_id(instrument), fingerprint)
    feed = IbkrTickFeed([instrument], FakeTickStore())
    assert feed._classify_delta(identity, 5000.25, bid=5000.0, ask=5000.25) == (1, "quote_test")
    assert feed._classify_delta(identity, 5000.0, bid=5000.0, ask=5000.25) == (-1, "quote_test")
    assert feed._classify_delta(identity, 5000.25, bid=None, ask=None) == (1, "tick_test")
    assert feed._classify_delta(identity, 5000.0, bid=None, ask=None) == (-1, "tick_test")


def test_ibkr_pending_tick_batch_orders_bidask_before_trade_at_same_provider_time() -> None:
    class TickByTickBidAsk:
        def __init__(self, ts: datetime) -> None:
            self.time = ts
            self.bidPrice = 5000.0
            self.askPrice = 5000.25

    class TickByTickAllLast:
        def __init__(self, ts: datetime) -> None:
            self.time = ts
            self.price = 5000.25
            self.size = 2
            self.tickAttribLast = None
            self.specialConditions = ""
            self.exchange = "CME"

    class PendingTicker:
        def __init__(self, packets) -> None:
            self.tickByTicks = list(packets)

    admitted: list[dict] = []
    instrument = ibkr_future_payload("ES")
    identity = (qualified_instrument_id(instrument), route_fingerprint(instrument))
    feed = IbkrTickFeed([instrument], FakeTickStore(), use_bidask=True)
    feed.aggregator = SimpleNamespace(
        add_tick=lambda **payload: admitted.append(payload) or True,
        status=lambda: {},
    )
    ts = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    trade_ticker = PendingTicker([TickByTickAllLast(ts)])
    bidask_ticker = PendingTicker([TickByTickBidAsk(ts)])
    feed._register_ticker_route(trade_ticker, identity)
    feed._register_ticker_route(bidask_ticker, identity)

    feed._on_pending_tickers({trade_ticker, bidask_ticker})

    assert len(admitted) == 1
    assert admitted[0]["tick_type"] == "all_last:quote_test"
    assert admitted[0]["delta_sign"] == 1
    assert admitted[0]["bid"] == 5000.0
    assert admitted[0]["ask"] == 5000.25


def test_ibkr_tick_classification_never_uses_future_or_regressed_bidask() -> None:
    class TickByTickBidAsk:
        def __init__(self, ts: datetime, bid: float, ask: float) -> None:
            self.time = ts
            self.bidPrice = bid
            self.askPrice = ask

    class TickByTickAllLast:
        def __init__(self, ts: datetime) -> None:
            self.time = ts
            self.price = 5000.25
            self.size = 2
            self.tickAttribLast = None
            self.specialConditions = ""
            self.exchange = "CME"

    admitted: list[dict] = []
    instrument = ibkr_future_payload("ES")
    identity = (qualified_instrument_id(instrument), route_fingerprint(instrument))
    feed = IbkrTickFeed([instrument], FakeTickStore(), use_bidask=True)
    feed.aggregator = SimpleNamespace(
        add_tick=lambda **payload: admitted.append(payload) or True,
        status=lambda: {},
    )
    ts = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    feed._handle_raw_tick(identity, TickByTickBidAsk(ts + timedelta(seconds=2), 5000.0, 5000.25))
    feed._handle_raw_tick(identity, TickByTickBidAsk(ts + timedelta(seconds=1), 4999.0, 4999.25))
    feed._handle_raw_tick(identity, TickByTickAllLast(ts))
    feed._handle_raw_tick(identity, TickByTickAllLast(ts + timedelta(seconds=2)))

    assert admitted[0]["bid"] is None
    assert admitted[0]["ask"] is None
    assert admitted[1]["bid"] == 5000.0
    assert admitted[1]["ask"] == 5000.25


def test_ibkr_pending_tick_batch_rejects_one_malformed_packet_and_continues() -> None:
    class TickByTickAllLast:
        def __init__(self, ts: datetime, size: object, price: object = 5000.25) -> None:
            self.time = ts
            self.price = price
            self.size = size
            self.tickAttribLast = None
            self.specialConditions = ""
            self.exchange = "CME"

    class PendingTicker:
        def __init__(self, packets) -> None:
            self.tickByTicks = list(packets)

    admitted: list[dict] = []
    instrument = ibkr_future_payload("ES")
    identity = (qualified_instrument_id(instrument), route_fingerprint(instrument))
    feed = IbkrTickFeed([instrument], FakeTickStore())
    feed.aggregator = SimpleNamespace(
        add_tick=lambda **payload: admitted.append(payload) or True,
        status=lambda: {},
    )
    ts = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    ticker = PendingTicker(
        [
            TickByTickAllLast(ts, 1.5),
            TickByTickAllLast(ts + timedelta(microseconds=1), 2),
        ]
    )
    feed._register_ticker_route(ticker, identity)

    feed._on_pending_tickers({ticker})

    assert [row["volume"] for row in admitted] == [2]
    assert feed.status()["rejected_ticks"] == 1
    assert feed.status()["last_admission_error"] == "IBKR_TICK_VOLUME_NOT_EXACT_INT"


@pytest.mark.parametrize("size", (float("nan"), float("inf"), 0, -1, True))
def test_ibkr_tick_provider_boundary_rejects_non_exact_positive_size(size: object) -> None:
    class TickByTickAllLast:
        time = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
        price = 5000.25
        tickAttribLast = None
        specialConditions = ""
        exchange = "CME"

    instrument = ibkr_future_payload("ES")
    identity = (qualified_instrument_id(instrument), route_fingerprint(instrument))
    feed = IbkrTickFeed([instrument], FakeTickStore())
    packet = TickByTickAllLast()
    packet.size = size

    with pytest.raises(tick_feed_module.IbkrTickAdmissionError):
        feed._handle_raw_tick(identity, packet)


def test_ibkr_tick_provider_boundary_rejects_naive_timestamp() -> None:
    class TickByTickAllLast:
        time = datetime(2026, 1, 1, 14, 0)
        price = 5000.25
        size = 1
        tickAttribLast = None
        specialConditions = ""
        exchange = "CME"

    instrument = ibkr_future_payload("ES")
    identity = (qualified_instrument_id(instrument), route_fingerprint(instrument))
    feed = IbkrTickFeed([instrument], FakeTickStore())

    with pytest.raises(
        tick_feed_module.IbkrTickAdmissionError,
        match="timestamp must be timezone-aware",
    ):
        feed._handle_raw_tick(identity, TickByTickAllLast())


def test_tick_feed_request_stop_uses_public_lifecycle_hook() -> None:
    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())
    feed._running = True

    feed.request_stop()

    assert feed.status()["running"] is False


def test_tick_feed_stop_requested_before_run_does_not_touch_storage_or_provider(
    monkeypatch,
) -> None:
    import ib_async

    provider_starts: list[bool] = []

    class PreinitializedStore(FakeTickStore):
        def initialize(self) -> None:
            raise AssertionError("tick feed must not initialize application storage")

    monkeypatch.setattr(
        ib_async,
        "IB",
        lambda: provider_starts.append(True) or SimpleNamespace(),
    )
    feed = IbkrTickFeed([ibkr_future_payload("ES")], PreinitializedStore())
    feed.request_stop()

    asyncio.run(feed.run_forever())
    assert provider_starts == []
    assert feed.status()["running"] is False


def test_tick_feed_stop_uses_canonical_owned_session_teardown(monkeypatch) -> None:
    disconnects: list[tuple[str, object]] = []
    aggregator_stops: list[bool] = []

    class FakeAggregator:
        async def stop(self):
            aggregator_stops.append(True)

    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())
    feed.ib = SimpleNamespace()
    feed.aggregator = FakeAggregator()

    monkeypatch.setattr(
        tick_feed_module,
        "_disconnect_owned_ibkr_session",
        lambda _ib, *, session, session_key: disconnects.append((session, session_key)) or True,
    )

    asyncio.run(feed.stop())

    assert disconnects == [
        (
            "tick_feed",
            (feed.host, feed.port, feed.client_id, feed.readonly),
        )
    ]
    assert aggregator_stops == [True]


def test_tick_feed_stop_surfaces_disconnect_failure_after_aggregator_stop(
    monkeypatch,
) -> None:
    aggregator_stops: list[bool] = []

    class FakeAggregator:
        async def stop(self):
            aggregator_stops.append(True)

    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())
    feed.ib = SimpleNamespace()
    feed.aggregator = FakeAggregator()

    def reject_disconnect(*_args, **_kwargs):
        raise RuntimeError("transport close failed")

    monkeypatch.setattr(
        tick_feed_module,
        "_disconnect_owned_ibkr_session",
        reject_disconnect,
    )

    with pytest.raises(ExceptionGroup, match="tick-feed shutdown failed"):
        asyncio.run(feed.stop())

    assert aggregator_stops == [True]


def test_tick_feed_startup_hook_failure_still_cleans_session_and_aggregator(
    monkeypatch,
) -> None:
    import ib_async

    disconnects: list[bool] = []
    aggregator_events: list[str] = []

    class RejectingEvent:
        def __iadd__(self, _handler):
            raise RuntimeError("hook rejected")

    class FakeAggregator:
        def start(self):
            aggregator_events.append("start")

        async def stop(self):
            aggregator_events.append("stop")

    fake_ib = SimpleNamespace(errorEvent=RejectingEvent())
    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())
    feed.aggregator = FakeAggregator()
    monkeypatch.setattr(ib_async, "IB", lambda: fake_ib)
    monkeypatch.setattr(
        tick_feed_module,
        "_disconnect_owned_ibkr_session",
        lambda *_args, **_kwargs: disconnects.append(True) or True,
    )

    with pytest.raises(RuntimeError, match="hook rejected"):
        asyncio.run(feed.run_forever())

    assert disconnects == [True]
    assert aggregator_events == ["start", "stop"]


def test_tick_feed_connect_failure_still_uses_canonical_cleanup(monkeypatch) -> None:
    import ib_async

    lifecycle: list[str] = []

    class AcceptingEvent:
        def __iadd__(self, _handler):
            return self

    class FakeAggregator:
        def start(self):
            lifecycle.append("aggregator.start")

        async def stop(self):
            lifecycle.append("aggregator.stop")

    async def reject_connect(*_args, **_kwargs):
        lifecycle.append("connect")
        raise ConnectionError("gateway unavailable")

    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())
    feed.aggregator = FakeAggregator()
    monkeypatch.setattr(
        ib_async,
        "IB",
        lambda: SimpleNamespace(errorEvent=AcceptingEvent()),
    )
    monkeypatch.setattr(
        tick_feed_module,
        "connect_without_account_sync_async",
        reject_connect,
    )
    monkeypatch.setattr(
        tick_feed_module,
        "_disconnect_owned_ibkr_session",
        lambda *_args, **_kwargs: lifecycle.append("disconnect") or True,
    )

    with pytest.raises(ConnectionError, match="gateway unavailable"):
        asyncio.run(feed.run_forever())

    assert lifecycle == [
        "aggregator.start",
        "connect",
        "disconnect",
        "aggregator.stop",
    ]


def test_tick_trigger_subscribes_only_all_last_by_default(monkeypatch) -> None:
    subscribed: list[tuple[str, str]] = []

    class FakeIb:
        def reqTickByTickData(self, contract, tick_type, **_kwargs):
            subscribed.append((contract.symbol, tick_type))
            return SimpleNamespace(tickByTicks=[])

    monkeypatch.setattr(
        tick_feed_module,
        "_quote_contract_for_instrument",
        lambda _ib, instrument: SimpleNamespace(symbol=instrument["provider_symbol"]),
    )
    feed = IbkrTickFeed(
        [ibkr_future_payload("ES"), ibkr_future_payload("GC", exchange="COMEX")],
        FakeTickStore(),
    )
    feed.ib = FakeIb()

    instrument = ibkr_future_payload("ES")
    feed._start_tick_subscriptions(
        "trigger ES",
        route_identities=[(qualified_instrument_id(instrument), route_fingerprint(instrument))],
    )

    assert subscribed == [("ES", "AllLast")]
    assert feed.status()["tick_subscriptions"] == 1
    assert feed.status()["use_bidask"] is False


def test_tick_trigger_subscribes_bidask_when_explicitly_enabled(monkeypatch) -> None:
    subscribed: list[tuple[str, str]] = []

    class FakeIb:
        def reqTickByTickData(self, contract, tick_type, **_kwargs):
            subscribed.append((contract.symbol, tick_type))
            return SimpleNamespace(tickByTicks=[])

    monkeypatch.setattr(
        tick_feed_module,
        "_quote_contract_for_instrument",
        lambda _ib, instrument: SimpleNamespace(symbol=instrument["provider_symbol"]),
    )
    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore(), use_bidask=True)
    feed.ib = FakeIb()

    instrument = ibkr_future_payload("ES")
    feed._start_tick_subscriptions(
        "trigger ES",
        route_identities=[(qualified_instrument_id(instrument), route_fingerprint(instrument))],
    )

    assert subscribed == [("ES", "AllLast"), ("ES", "BidAsk")]
    assert feed.status()["tick_subscriptions"] == 2
    assert feed.status()["use_bidask"] is True


def test_tick_trigger_does_not_duplicate_alllast_when_bidask_subscribe_fails(monkeypatch) -> None:
    subscribed: list[tuple[str, str]] = []

    class FakeIb:
        def reqTickByTickData(self, contract, tick_type, **_kwargs):
            subscribed.append((contract.symbol, tick_type))
            if tick_type == "BidAsk":
                raise RuntimeError("bidask unavailable")
            return SimpleNamespace(tickByTicks=[])

    monkeypatch.setattr(
        tick_feed_module,
        "_quote_contract_for_instrument",
        lambda _ib, instrument: SimpleNamespace(symbol=instrument["provider_symbol"]),
    )
    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore(), use_bidask=True)
    feed.ib = FakeIb()

    instrument = ibkr_future_payload("ES")
    es_route = route_fingerprint(instrument)
    identity = (qualified_instrument_id(instrument), es_route)
    feed._start_tick_subscriptions("trigger ES", route_identities=[identity])
    feed._start_tick_subscriptions("retry ES", route_identities=[identity])

    assert subscribed == [("ES", "AllLast"), ("ES", "BidAsk")]
    assert feed.status()["tick_subscriptions"] == 1
    assert (
        feed.status()["last_error"]
        == f"IBKR tick bidask subscribe failed for {identity}: bidask unavailable"
    )


@pytest.mark.parametrize("code", (2104, 2106, 2107, 2108, 2119, 2158))
def test_tick_feed_does_not_publish_ibkr_information_events_as_errors(
    code,
    caplog,
) -> None:
    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())

    feed._on_error(-1, code, "provider connection status")

    assert feed.status()["last_error"] == ""
    assert not [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "IBKR tick error" in record.message
    ]


def test_tick_feed_information_event_does_not_replace_real_error() -> None:
    feed = IbkrTickFeed([ibkr_future_payload("ES")], FakeTickStore())

    feed._on_error(-1, 2103, "market data farm disconnected")
    expected_error = feed.status()["last_error"]
    feed._on_error(-1, 2158, "security definition farm is OK")

    assert expected_error == "IBKR tick error 2103: market data farm disconnected"
    assert feed.status()["last_error"] == expected_error


def test_tick_live_status_exposes_buffer_health(monkeypatch) -> None:
    health = {"severity": "warn", "reason": "db_errors", "message": "writer has errors"}
    feed = SimpleNamespace(
        status=lambda: {
            "running": True,
            "display_keys": ["ES"],
            "route_fingerprints": [ES_ROUTE],
            "tick_subscriptions": 1,
            "last_error": "",
            "buffer": {"health": health, "dropped": 0},
        }
    )
    thread = SimpleNamespace(is_alive=lambda: True)
    connector = tick_live_runtime.tick_live_connector()
    monkeypatch.setattr(connector, "feed", feed)
    monkeypatch.setattr(connector, "thread", thread)
    monkeypatch.setattr(connector, "last_error", "")

    status = tick_live_runtime.tick_live_status()

    assert status["status"] == "live"
    assert status["health"] == health
    assert status["buffer"]["health"] == health


def test_tick_bucket_validation_blocks_sql_injection_shape() -> None:
    assert safe_tick_bucket("5 seconds") == "5 seconds"
    with pytest.raises(ValueError):
        safe_tick_bucket("1 minute'); drop table ticks; --")


def test_tick_history_interval_accepts_backtest_timeframes_only() -> None:
    assert safe_tick_history_interval("5m") == "5 minutes"
    with pytest.raises(ValueError, match="unsupported tick history interval"):
        safe_tick_history_interval("1h")
    with pytest.raises(ValueError):
        safe_tick_history_interval("5 minutes'); drop table ticks; --")


def test_tick_bar_history_uses_rollup_without_double_counting_raw_overlap() -> None:
    source = inspect.getsource(PostgresStore.read_tick_bar_history)
    assert "tick_delta_1m_rollup" in source
    assert "NOT EXISTS" in source
    assert "{timescaledb}.time_bucket(%s::interval, bucket)" in source


def test_tick_volume_profile_history_keeps_bucket_and_price_levels() -> None:
    source = inspect.getsource(PostgresStore.read_tick_volume_profile_history)
    assert "tick_volume_profile_5m_rollup" in source
    assert "{timescaledb}.time_bucket(%s::interval, bucket)" in source
    assert "{timescaledb}.time_bucket(%s::interval, t.ts)" in source
    assert "NOT EXISTS" in source
    assert "price" in source
    assert "buy_volume" in source
    assert "sell_volume" in source


def test_tick_schema_bootstraps_exact_route_identity_without_cleanup_paths() -> None:
    source = inspect.getsource(PostgresStore.initialize)

    assert "ingest_seq bigint GENERATED BY DEFAULT AS IDENTITY" in source
    assert "CREATE SEQUENCE IF NOT EXISTS ticks_ingest_seq_seq" not in source
    assert "nextval('ticks_ingest_seq_seq'::regclass)" not in source
    assert "ticks_route_ts_ingest_idx" in source
    assert "CREATE TABLE tick_delta_1m_rollup" in source
    assert "CREATE TABLE tick_volume_profile_5m_rollup" in source
    assert "CONSTRAINT ticks_provider_chk" in source
    assert "CONSTRAINT tick_delta_1m_rollup_provider_chk" in source
    assert "CONSTRAINT tick_volume_profile_5m_rollup_provider_chk" in source
    extension_ddl_removed = source.replace(
        "CREATE EXTENSION IF NOT EXISTS timescaledb", ""
    ).replace("CREATE EXTENSION IF NOT EXISTS pg_stat_statements", "")
    assert "IF NOT EXISTS" not in extension_ddl_removed
    assert "if_not_exists" not in source
    assert "remove_continuous_aggregate_policy" not in source
    assert "DROP MATERIALIZED VIEW" not in source
    assert "add_continuous_aggregate_policy" not in source
    assert "policy_refresh_continuous_aggregate" not in source


def test_tick_batch_writer_uses_atomic_postgres_copy_without_bar_cache_invalidation() -> None:
    source = inspect.getsource(PostgresStore.write_ticks)

    assert "with cur.copy(" in source
    assert "COPY ticks (" in source
    assert "FROM STDIN" in source
    assert "tick_copy.write_row(row)" in source
    assert "return len(rows)" in source
    assert "executemany" not in source
    assert "_bar_slots_cache.clear()" not in source
    assert "max(int(tick.volume), 0)" not in source
    assert "max(min(int(tick.delta_sign), 1), -1)" not in source


def test_tick_purge_rolls_up_deleted_batch_atomically() -> None:
    source = inspect.getsource(PostgresStore._rollup_and_purge_ticks)
    rollup_source = inspect.getsource(PostgresStore.rollup_ticks)
    purge_source = inspect.getsource(PostgresStore.purge_raw_ticks)

    assert "return self._rollup_and_purge_ticks(cutoff, provider=provider)" in rollup_source
    assert "return self._rollup_and_purge_ticks(" in purge_source
    assert "rollup=rollup_first" in purge_source
    assert "pg_advisory_xact_lock" in source
    assert "CREATE TEMP TABLE tick_purge_batch" in source
    assert 'cur.execute("ANALYZE tick_purge_batch")' in source
    assert "WITH next_bucket AS" in source
    assert "selected.bucket + INTERVAL '5 minutes'" in source
    assert "DELETE FROM ticks" in source
    assert "WITH deleted AS" not in source
    assert (
        "RETURNING provider, instrument_id, route_fingerprint, ts, price, volume, delta_sign"
        not in source
    )
    assert "ORDER BY ts ASC, ingest_seq ASC" in source
    assert "_TICK_PURGE_BATCH_LIMIT" not in source
    assert "raw.ingest_seq = batch.ingest_seq" in source
    assert "TICK_PURGE_CARDINALITY_MISMATCH" in source
    assert '"has_more": int(has_more)' in source
    assert "_drop_expired_empty_tick_chunks" in source
    assert "FROM tick_purge_batch" in source
    assert "tick_delta_1m_rollup.total_volume + EXCLUDED.total_volume" in source
    assert "tick_volume_profile_5m_rollup.total_volume + EXCLUDED.total_volume" in source

    empty_chunk_source = inspect.getsource(tick_repo_module._drop_expired_empty_tick_chunks)
    assert "SELECT EXISTS" in empty_chunk_source
    assert "WHERE ts < %s" in empty_chunk_source
    assert "drop_chunks" in empty_chunk_source


def test_bar_write_policy_preserves_closed_and_good_history() -> None:
    source = inspect.getsource(PostgresStore._write_bars_on_cursor)
    policy_source = inspect.getsource(_storage_bar_reject_reason)

    instrument = ibkr_future_payload("ES")
    closed = Bar(
        "ES",
        datetime(2026, 1, 1, tzinfo=UTC),
        100,
        101,
        99,
        100.5,
        1000,
        "1m",
        "arbitrary-advisory-source",
        closed=True,
        provenance=BarProvenance(
            provider="ibkr",
            instrument_id=qualified_instrument_id(instrument),
            route_fingerprint=route_fingerprint(instrument),
            request_type="historical",
            provider_contract_id="1",
            provider_contract_type="CONTFUT",
            data_type="TRADES",
        ),
    )
    live = Bar(
        "ES",
        datetime(2026, 1, 1, tzinfo=UTC),
        100,
        101,
        99,
        100.5,
        1000,
        "1m",
        "ibkr",
        closed=False,
    )
    quote_live = Bar(
        "ES",
        datetime(2026, 1, 1, tzinfo=UTC),
        100,
        101,
        99,
        100.5,
        1000,
        "1m",
        "ibkr:db-cache+quote-live",
        closed=False,
    )

    assert _storage_bar_allowed("ibkr", closed) is True
    assert _storage_bar_allowed("ibkr", live) is False
    assert _storage_bar_allowed("ibkr", quote_live) is False
    assert _storage_bar_allowed("coinbase", live) is False
    assert _storage_bar_allowed("coinbase", quote_live) is False
    assert _storage_bar_reject_reason("ibkr", closed) is None
    assert _storage_bar_reject_reason("ibkr:live", closed) is None
    assert _storage_bar_reject_reason("ibkr", live) == "provisional_bar"
    assert _storage_bar_reject_reason("ibkr:live", live) == "provisional_bar"
    assert _storage_bar_reject_reason("ibkr", quote_live) == "provisional_bar"
    with pytest.raises(ValueError, match="PROVIDER_SOURCE_UNKNOWN"):
        _storage_bar_reject_reason("unregistered-feed", closed)
    assert "provider_data_policy_for_source" in policy_source
    assert "provider_key_for_source" in policy_source
    assert "bar.source" not in policy_source
    assert "provider_key = str(provider" not in source
    assert "_AUTHORITATIVE_BAR_PROVIDERS" not in source
    assert "NOT (bars.closed = true AND EXCLUDED.closed = false)" in source
    assert "quote-ingest" not in source
    assert "EXCLUDED.revision_sequence > bars.revision_sequence" in source
    assert "revision_sequence = EXCLUDED.revision_sequence" in source
    assert "bars.open IS DISTINCT FROM EXCLUDED.open" in source
    assert "bars.source IS DISTINCT FROM EXCLUDED.source" in source
    assert "bars.closed IS DISTINCT FROM EXCLUDED.closed" in source


def test_tick_history_endpoint_returns_aggregated_backtest_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def initialize(self) -> None:
            return None

        def lookup_instrument(self, instrument_id):
            instrument = ibkr_future_payload("ES")
            return instrument if instrument_id == qualified_instrument_id(instrument) else None

        def read_tick_bar_history(
            self, instrument_id, route_key, start, end, timeframe="5m", limit=5000
        ):
            instrument = ibkr_future_payload("ES")
            assert instrument_id == qualified_instrument_id(instrument)
            assert route_key == route_fingerprint(instrument)
            assert timeframe == "15m"
            assert limit == 2
            assert start.tzinfo is not None
            assert end.tzinfo is not None
            return [
                {
                    "ts": "2026-06-04T13:30:00+00:00",
                    "total_volume": 100,
                    "net_delta": 25,
                    "trade_count": 10,
                    "delta_ratio": 0.25,
                },
                {
                    "ts": "2026-06-04T13:45:00+00:00",
                    "total_volume": 50,
                    "net_delta": -5,
                    "trade_count": 8,
                    "delta_ratio": -0.1,
                },
            ]

    monkeypatch.setattr(ui_app, "default_postgres_store", lambda: Store())

    instrument = ibkr_future_payload("ES")
    payload = app_route_endpoint("/api/ticks/history")(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
        start="2026-06-04T13:30:00+00:00",
        end="2026-06-04T14:00:00+00:00",
        timeframe="15m",
        limit=2,
    )

    assert payload["ok"] is True
    assert payload["provider"] == "ibkr"
    assert payload["stats"]["bars"] == 2
    assert payload["stats"]["total_volume"] == 150
    assert payload["stats"]["net_delta"] == 20
    assert payload["stats"]["delta_ratio"] == pytest.approx(20 / 150)


def test_tick_profile_history_endpoint_returns_interval_price_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def initialize(self) -> None:
            return None

        def lookup_instrument(self, instrument_id):
            instrument = ibkr_future_payload("ES")
            return instrument if instrument_id == qualified_instrument_id(instrument) else None

        def read_tick_volume_profile_history(
            self, instrument_id, route_key, start, end, timeframe="5m", price_step=0.25, limit=50000
        ):
            instrument = ibkr_future_payload("ES")
            assert instrument_id == qualified_instrument_id(instrument)
            assert route_key == route_fingerprint(instrument)
            assert timeframe == "5m"
            assert price_step == 0.25
            assert limit == 3
            assert start.tzinfo is not None
            assert end.tzinfo is not None
            return [
                {
                    "ts": "2026-06-04T13:30:00+00:00",
                    "price": 5300.0,
                    "total_volume": 100,
                    "buy_volume": 70,
                    "sell_volume": 30,
                    "net_delta": 40,
                    "trade_count": 10,
                    "delta_ratio": 0.4,
                },
                {
                    "ts": "2026-06-04T13:30:00+00:00",
                    "price": 5300.25,
                    "total_volume": 50,
                    "buy_volume": 10,
                    "sell_volume": 40,
                    "net_delta": -30,
                    "trade_count": 8,
                    "delta_ratio": -0.6,
                },
            ]

    monkeypatch.setattr(ui_app, "default_postgres_store", lambda: Store())

    instrument = ibkr_future_payload("ES")
    payload = app_route_endpoint("/api/ticks/profile-history")(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
        start="2026-06-04T13:30:00+00:00",
        end="2026-06-04T13:35:00+00:00",
        timeframe="5m",
        price_step=0.25,
        limit=3,
    )

    assert payload["ok"] is True
    assert payload["provider"] == "ibkr"
    assert payload["price_step"] == 0.25
    assert payload["stats"]["rows"] == 2
    assert payload["stats"]["buckets"] == 1
    assert payload["stats"]["prices"] == 2
    assert payload["stats"]["total_volume"] == 150
    assert payload["stats"]["net_delta"] == 10
    assert payload["stats"]["delta_ratio"] == pytest.approx(10 / 150)


def test_parse_conditions_normalizes_ibkr_special_conditions() -> None:
    assert parse_conditions("A; B,C") == ("A", "B", "C")


def test_tick_is_a_finite_immutable_exact_ingress_value() -> None:
    add_source = inspect.getsource(TickBulkAggregator.add_tick)
    assert "max(int(volume), 0)" not in add_source
    assert "max(min(int(delta_sign), 1), -1)" not in add_source
    source_conditions = ["pastLimit"]
    tick = Tick(
        instrument_id=ES_ID,
        route_fingerprint=ES_ROUTE,
        ts=datetime(2026, 1, 1, 17, 0, tzinfo=timezone(timedelta(hours=3))),
        price=-2.5,
        volume=3,
        delta_sign=-1,
        bid=-2.75,
        ask=-2.25,
        conditions=source_conditions,
    )
    source_conditions.append("mutated")

    assert tick.ts == datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert tick.price == -2.5
    assert tick.conditions == ("pastLimit",)
    with pytest.raises(ValueError, match="TICK_TIMESTAMP_NOT_AWARE"):
        Tick(ES_ROUTE, datetime(2026, 1, 1), 1.0, 1, 0, ES_ID)
    with pytest.raises(ValueError, match="TICK_PRICE_NOT_FINITE"):
        Tick(ES_ROUTE, datetime(2026, 1, 1, tzinfo=UTC), float("nan"), 1, 0, ES_ID)
    with pytest.raises(ValueError, match="TICK_BID_NOT_FINITE"):
        Tick(
            ES_ROUTE,
            datetime(2026, 1, 1, tzinfo=UTC),
            1.0,
            1,
            0,
            ES_ID,
            bid=float("inf"),
        )
    with pytest.raises(TypeError, match="TICK_VOLUME_NOT_EXACT_INT"):
        Tick(ES_ROUTE, datetime(2026, 1, 1, tzinfo=UTC), 1.0, 1.0, 0, ES_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="TICK_VOLUME_NEGATIVE"):
        Tick(ES_ROUTE, datetime(2026, 1, 1, tzinfo=UTC), 1.0, -1, 0, ES_ID)
    with pytest.raises(ValueError, match="TICK_DELTA_SIGN_INVALID"):
        Tick(ES_ROUTE, datetime(2026, 1, 1, tzinfo=UTC), 1.0, 1, 2, ES_ID)
    with pytest.raises(ValueError, match="TICK_TYPE_INVALID"):
        Tick(ES_ROUTE, datetime(2026, 1, 1, tzinfo=UTC), 1.0, 1, 0, ES_ID, tick_type="")
    with pytest.raises(ValueError, match="TICK_CONDITION_INVALID"):
        Tick(
            ES_ROUTE,
            datetime(2026, 1, 1, tzinfo=UTC),
            1.0,
            1,
            0,
            ES_ID,
            conditions=("",),
        )


def test_ibkr_tick_clean_price_is_finite_without_positive_price_guess() -> None:
    assert clean_price(float("nan")) is None
    assert clean_price(float("inf")) is None
    assert clean_price(float("-inf")) is None
    assert clean_price(None) is None
    assert clean_price(0) == 0.0
    assert clean_price(-37.63) == -37.63


def test_tick_context_payload_adds_cumulative_delta_and_profile_normalization() -> None:
    plain_payload = _tick_context_payload([], [])
    assert "aggregates_compute_ms" not in plain_payload["stats"]
    with pytest.raises(ValueError, match="total_volume must be an exact"):
        _tick_context_payload(
            [
                {
                    "ts": "2026-01-01T14:00:00+00:00",
                    "total_volume": "10",
                    "net_delta": 4,
                    "trade_count": 3,
                }
            ],
            [],
        )

    payload = _tick_context_payload(
        [
            {
                "ts": "2026-01-01T14:01:00+00:00",
                "total_volume": 6,
                "net_delta": -2,
                "trade_count": 2,
            },
            {
                "ts": "2026-01-01T14:00:00+00:00",
                "total_volume": 10,
                "net_delta": 4,
                "trade_count": 3,
            },
        ],
        [
            {
                "price": 5000.0,
                "total_volume": 20,
                "buy_volume": 12,
                "sell_volume": 8,
                "net_delta": 4,
                "trade_count": 5,
            },
            {
                "price": 5000.25,
                "total_volume": 10,
                "buy_volume": 2,
                "sell_volume": 8,
                "net_delta": -6,
                "trade_count": 4,
            },
        ],
        include_telemetry=True,
    )

    assert [row["cumulative_delta"] for row in payload["delta"]] == [4, 2]
    assert payload["delta"][0]["delta_ratio"] == 0.4
    assert payload["profile"][0]["normalized"] == 1.0
    assert payload["profile"][1]["delta_ratio"] == -0.6
    assert payload["stats"]["total_volume"] == 16
    assert payload["stats"]["net_delta"] == 2
    assert payload["stats"]["aggregates_compute_ms"] >= 0
    assert payload["aggregates"]["recent_delta"]["30"]["netDelta"] == -2
    assert payload["aggregates"]["recent_delta"]["30"]["totalVolume"] == 6
    assert payload["aggregates"]["poc"]["price"] == 5000.0
    assert payload["aggregates"]["value_area"]["vah"]["price"] == 5000.25
    assert payload["aggregates"]["value_area"]["val"]["price"] == 5000.0
    assert payload["aggregates"]["high_volume_prices"] == [5000.0, 5000.25]


def test_tick_profile_value_area_uses_two_price_expansion_from_poc() -> None:
    value_area = tick_profile_value_area(
        [
            {"price": 99.75, "total_volume": 199},
            {"price": 100.0, "total_volume": 1},
            {"price": 100.25, "total_volume": 200},
            {"price": 100.5, "total_volume": 50},
            {"price": 100.75, "total_volume": 45},
        ],
        coverage=0.70,
    )

    assert value_area is not None
    assert value_area["poc"]["price"] == 100.25
    assert value_area["val"]["price"] == 99.75
    assert value_area["vah"]["price"] == 100.25
    assert value_area["coverage"] >= 0.70


def test_tick_context_payload_adds_backend_footprint_aggregates() -> None:
    payload = _tick_context_payload(
        [],
        [
            {
                "price": 100.0,
                "total_volume": 10,
                "buy_volume": 90,
                "sell_volume": 10,
                "net_delta": 80,
                "trade_count": 5,
            },
            {
                "price": 100.25,
                "total_volume": 10,
                "buy_volume": 5,
                "sell_volume": 20,
                "net_delta": -15,
                "trade_count": 4,
            },
            {
                "price": 100.5,
                "total_volume": 10,
                "buy_volume": 4,
                "sell_volume": 90,
                "net_delta": -86,
                "trade_count": 7,
            },
        ],
    )

    groups = payload["aggregates"]["imbalance_groups"]
    assert {group["side"] for group in groups} == {"buy", "sell"}
    assert any(group["label"] == "B-IMB" and group["dominant"] >= 90 for group in groups)
    assert any(group["label"] == "S-IMB" and group["dominant"] >= 90 for group in groups)


def test_tick_profile_imbalance_groups_quantize_decimal_price_keys() -> None:
    groups = tick_profile_imbalance_groups(
        [
            {
                "price": 100.1,
                "total_volume": 10,
                "buy_volume": 2,
                "sell_volume": 30,
                "net_delta": -28,
            },
            {
                "price": 100.2,
                "total_volume": 10,
                "buy_volume": 100,
                "sell_volume": 5,
                "net_delta": 95,
            },
            {
                "price": 100.3,
                "total_volume": 10,
                "buy_volume": 2,
                "sell_volume": 5,
                "net_delta": -3,
            },
        ]
    )

    buy_group = next(group for group in groups if group["side"] == "buy")
    assert buy_group["dominant"] == 100
    assert buy_group["passive"] == 30
    assert buy_group["low"] == 100.2
    assert buy_group["high"] == 100.2


def test_tick_profile_aggregates_preserve_signed_and_zero_prices() -> None:
    profile = [
        {
            "price": -0.25,
            "total_volume": 20,
            "buy_volume": 2,
            "sell_volume": 30,
            "net_delta": -28,
            "trade_count": 4,
        },
        {
            "price": 0.0,
            "total_volume": 100,
            "buy_volume": 100,
            "sell_volume": 5,
            "net_delta": 95,
            "trade_count": 10,
        },
        {
            "price": 0.25,
            "total_volume": 10,
            "buy_volume": 2,
            "sell_volume": 5,
            "net_delta": -3,
            "trade_count": 2,
        },
        {
            "price": float("inf"),
            "total_volume": 1000,
            "buy_volume": 1000,
            "sell_volume": 0,
            "net_delta": 1000,
            "trade_count": 100,
        },
        {
            "price": "1e10000",
            "total_volume": 1000,
            "buy_volume": 1000,
            "sell_volume": 0,
            "net_delta": 1000,
            "trade_count": 100,
        },
    ]

    value_area = tick_profile_value_area(profile)
    groups = tick_profile_imbalance_groups(profile)
    payload = _tick_context_payload(
        [],
        profile,
        price_step=0.25,
        levels=[{"name": "Zero", "price": 0.0}],
    )

    assert value_area is not None
    assert value_area["poc"]["price"] == 0.0
    buy_group = next(group for group in groups if group["side"] == "buy")
    assert buy_group["low"] == 0.0
    assert buy_group["high"] == 0.0
    assert [row["price"] for row in payload["profile"]] == [-0.25, 0.0, 0.25]
    assert payload["aggregates"]["level_deltas"][0]["price"] == 0.0


def test_tick_context_payload_adds_backend_level_delta_summaries() -> None:
    payload = _tick_context_payload(
        [],
        [
            {
                "price": 100.0,
                "total_volume": 20,
                "buy_volume": 12,
                "sell_volume": 8,
                "net_delta": 4,
                "trade_count": 5,
            },
            {
                "price": 100.25,
                "total_volume": 30,
                "buy_volume": 25,
                "sell_volume": 5,
                "net_delta": 20,
                "trade_count": 9,
            },
            {
                "price": 100.5,
                "total_volume": 10,
                "buy_volume": 2,
                "sell_volume": 8,
                "net_delta": -6,
                "trade_count": 4,
            },
        ],
        price_step=0.25,
        levels=[{"name": "Trade Setup", "price": 100.25}],
    )

    level_delta = payload["aggregates"]["level_deltas"][0]
    assert level_delta["name"] == "Trade Setup"
    assert level_delta["price"] == 100.25
    assert level_delta["volume"] == 60
    assert level_delta["delta"] == 18
