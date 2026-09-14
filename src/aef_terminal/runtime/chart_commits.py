from __future__ import annotations

import asyncio
import copy
import heapq
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryStorageKind,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairOutcomeStatus,
    ProviderHistoryTerminal,
)
from aef_terminal.data.providers import provider_key_for_source, route_instrument
from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarState,
    bar_ingress_revision_signature,
)
from aef_terminal.runtime.async_tasks import settle_physical_task
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    ChartBarsWriteGuard,
    begin_chart_bars_write,
    chart_bars_updated_generation,
    end_chart_bars_write,
    invalidate_chart_bars_write,
    publish_chart_bars_updated,
)
from aef_terminal.runtime.metrics import increment_metric, set_metric
from aef_terminal.runtime.timeframes import interval_seconds
from aef_terminal.storage.db_utils import database_error_is_transient
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts
from aef_terminal.data.history_coverage import (
    CanonicalHistoryCommitReceipt,
    HistoryCoverageReceipt,
)


ChartCommitKey = tuple[str, str, str]
CHART_COMMIT_TIMEOUT_SECONDS = 10.0
CHART_COMMIT_DEDUPE_TAIL = 256
HISTORY_COMMIT_QUEUE_LIMIT = 32


class HistoryCommitStatus(str, Enum):
    COMMITTED = "committed"
    NO_CHANGE = "no_change"
    STALE_GENERATION = "stale_generation"
    STALE_ROUTE = "stale_route"
    QUEUE_FULL = "queue_full"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HistoryCommitRequest:
    """Immutable provider result admitted to one canonical commit lane."""

    provider: str
    instrument_id: str
    route_fingerprint: str
    interval: str
    starts_at: datetime
    ends_at: datetime
    canonical_generation: int
    instrument: Mapping[str, Any]
    bars: tuple[Bar, ...]
    coverage_receipts: tuple[HistoryCoverageReceipt, ...] = ()

    def __post_init__(self) -> None:
        _validate_history_commit_identity(self)
        starts_at, ends_at = _history_commit_range(self)
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)
        _validate_history_commit_bars(self, starts_at, ends_at)
        ordered_receipts = _history_commit_receipts(self, starts_at, ends_at)
        object.__setattr__(self, "coverage_receipts", ordered_receipts)
        if not self.bars and not self.coverage_receipts:
            raise ValueError("HISTORY_COMMIT_EMPTY_NOOP_FORBIDDEN")


def _validate_history_commit_identity(request: HistoryCommitRequest) -> None:
    if not all(
        isinstance(value, str) and value and value == value.strip()
        for value in (
            request.provider,
            request.instrument_id,
            request.route_fingerprint,
            request.interval,
        )
    ):
        raise ValueError("HISTORY_COMMIT_EXACT_IDENTITY_REQUIRED")
    try:
        canonical_provider = provider_key_for_source(request.provider)
    except ValueError as exc:
        raise ValueError("HISTORY_COMMIT_PROVIDER_NOT_CANONICAL") from exc
    if canonical_provider != request.provider:
        raise ValueError("HISTORY_COMMIT_PROVIDER_NOT_CANONICAL")
    if (
        isinstance(request.canonical_generation, bool)
        or not isinstance(request.canonical_generation, int)
        or request.canonical_generation < 0
    ):
        raise ValueError("HISTORY_COMMIT_GENERATION_INVALID")
    if not isinstance(request.instrument, Mapping):
        raise TypeError("HISTORY_COMMIT_INSTRUMENT_MAPPING_REQUIRED")


def _history_commit_range(request: HistoryCommitRequest) -> tuple[datetime, datetime]:
    starts_at = request.starts_at
    ends_at = request.ends_at
    if (
        not isinstance(starts_at, datetime)
        or not isinstance(ends_at, datetime)
        or starts_at.tzinfo is None
        or ends_at.tzinfo is None
    ):
        raise ValueError("HISTORY_COMMIT_RANGE_UTC_REQUIRED")
    starts_at = starts_at.astimezone(UTC)
    ends_at = ends_at.astimezone(UTC)
    if ends_at <= starts_at:
        raise ValueError("HISTORY_COMMIT_RANGE_INVALID")
    step_seconds = interval_seconds(request.interval)
    if (
        int(starts_at.timestamp()) % step_seconds != 0
        or int(ends_at.timestamp()) % step_seconds != 0
        or starts_at.microsecond
        or ends_at.microsecond
    ):
        raise ValueError("HISTORY_COMMIT_RANGE_UNALIGNED")
    return starts_at, ends_at


def _validate_history_commit_bars(
    request: HistoryCommitRequest,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    if any(not isinstance(bar, Bar) for bar in request.bars):
        raise TypeError("HISTORY_COMMIT_BARS_TYPED_REQUIRED")
    bar_slots = tuple((bar.timeframe, bar.ts.astimezone(UTC)) for bar in request.bars)
    if len(set(bar_slots)) != len(bar_slots):
        raise ValueError("HISTORY_COMMIT_BAR_SLOTS_NOT_UNIQUE")
    for bar in request.bars:
        provenance = bar.provenance
        if (
            BarState(bar.state) is not BarState.CONFIRMED
            or not bar.closed
            or bar.timeframe != request.interval
            or not starts_at <= bar.ts.astimezone(UTC) < ends_at
            or provenance is None
            or provenance.request_type is not BarProviderRequest.HISTORICAL
            or provenance.provider != request.provider
            or provenance.instrument_id != request.instrument_id
            or provenance.route_fingerprint != request.route_fingerprint
        ):
            raise ValueError("HISTORY_COMMIT_BAR_CONTRACT_MISMATCH")


def _history_commit_receipts(
    request: HistoryCommitRequest,
    starts_at: datetime,
    ends_at: datetime,
) -> tuple[HistoryCoverageReceipt, ...]:
    if any(
        not isinstance(receipt, HistoryCoverageReceipt) for receipt in request.coverage_receipts
    ):
        raise TypeError("HISTORY_COMMIT_COVERAGE_TYPED_REQUIRED")
    ordered = tuple(
        sorted(
            request.coverage_receipts,
            key=lambda item: (item.covered_from, item.covered_to),
        )
    )
    cursor = starts_at
    for receipt in ordered:
        contract = receipt.contract
        if (
            contract.provider != request.provider
            or contract.instrument_id != request.instrument_id
            or contract.route_fingerprint != request.route_fingerprint
            or contract.timeframe != request.interval
            or receipt.covered_from != cursor
            or receipt.covered_to > ends_at
        ):
            raise ValueError("HISTORY_COMMIT_COVERAGE_CONTRACT_MISMATCH")
        cursor = receipt.covered_to
    if ordered and cursor != ends_at:
        raise ValueError("HISTORY_COMMIT_COVERAGE_RANGE_INCOMPLETE")
    return ordered


@dataclass(frozen=True, slots=True)
class HistoryCommitOutcome:
    status: HistoryCommitStatus
    receipt: CanonicalHistoryCommitReceipt | None = None
    generation: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryCommitHandle:
    accepted: bool
    completion: asyncio.Future[HistoryCommitOutcome]


@dataclass(slots=True)
class _PendingHistoryCommit:
    request: HistoryCommitRequest
    store_factory: Any
    completion: asyncio.Future[HistoryCommitOutcome]
    revision_sequence: int | None = None
    failures: int = 0


class _HistoryCommitTerminalError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _HistoryCommitStorageOutcomeUncertain(RuntimeError):
    def __init__(self, generation: int, error: Exception) -> None:
        self.generation = int(generation)
        self.error_type = type(error).__name__
        super().__init__(f"HISTORY_COMMIT_STORAGE_OUTCOME_UNCERTAIN:{self.error_type}")


def _history_storage_error_is_transient(error: Exception) -> bool:
    return database_error_is_transient(error)


def _validate_exact_history_bar_receipt(
    submitted: tuple[Bar, ...],
    receipt: CanonicalBarCommitReceipt,
    *,
    revision_sequence: int,
) -> tuple[tuple[Bar, ...], tuple[Bar, ...]]:
    if not isinstance(receipt, CanonicalBarCommitReceipt):
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_TYPED_BAR_RECEIPT_REQUIRED")
    if receipt.revision_sequence != revision_sequence:
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_REVISION_SEQUENCE_MISMATCH")
    if receipt.rejected:
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_CONFIRMED_BAR_REJECTED")
    if receipt.written > len(receipt.acknowledged):
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_WRITTEN_COUNT_INVALID")
    if any(not isinstance(bar, Bar) for bar in (*receipt.acknowledged, *receipt.superseded)):
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_BAR_OUTCOME_NOT_TYPED")

    submitted_signatures = tuple((bar.ts, bar_ingress_revision_signature(bar)) for bar in submitted)
    acknowledged_signatures = tuple(
        (bar.ts, bar_ingress_revision_signature(bar)) for bar in receipt.acknowledged
    )
    superseded_signatures = tuple(
        (bar.ts, bar_ingress_revision_signature(bar)) for bar in receipt.superseded
    )
    if len(set(acknowledged_signatures)) != len(acknowledged_signatures):
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_ACKNOWLEDGED_DUPLICATE")
    if len(set(superseded_signatures)) != len(superseded_signatures):
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_SUPERSEDED_DUPLICATE")
    acknowledged_set = set(acknowledged_signatures)
    superseded_set = set(superseded_signatures)
    submitted_set = set(submitted_signatures)
    if acknowledged_set & superseded_set:
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_RECEIPT_OUTCOME_OVERLAP")
    if acknowledged_set | superseded_set != submitted_set:
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_RECEIPT_OUTCOME_INCOMPLETE")
    return receipt.acknowledged, receipt.superseded


def _commit_history_result_under_write_guard(
    store: Any,
    request: HistoryCommitRequest,
    revision_sequence: int,
) -> tuple[Any, ChartBarsWriteGuard]:
    guard = begin_chart_bars_write(
        request.canonical_generation,
        request.interval,
        request.route_fingerprint,
        instrument_id=request.instrument_id,
    )
    try:
        route = route_instrument(
            dict(request.instrument),
            expected_source=request.provider,
        )
        history_route = route.adapter.canonical_history_route(route.instrument)
        if (
            history_route is not None
            and history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS
        ):
            if request.coverage_receipts:
                raise ValueError("HISTORY_COMMIT_CONTINUOUS_COVERAGE_FORBIDDEN")
            bar_receipt = route.adapter.commit_continuous_history_result(
                store,
                request.bars,
                instrument=route.instrument,
                revision_sequence=revision_sequence,
            )
            if not isinstance(bar_receipt, CanonicalBarCommitReceipt):
                raise TypeError("HISTORY_COMMIT_TYPED_BAR_RECEIPT_REQUIRED")
            if bar_receipt.rejected:
                raise ValueError("HISTORY_COMMIT_REJECTED_BARS")
            receipt = CanonicalHistoryCommitReceipt(
                bar_receipt=bar_receipt,
                coverage_inserted=0,
                coverage_expanded=False,
            )
        else:
            receipt = store.commit_history_result(
                request.bars,
                provider=request.provider,
                instrument=dict(request.instrument),
                revision_sequence=revision_sequence,
                coverage_receipts=request.coverage_receipts,
            )
    except BaseException as exc:
        generation = invalidate_chart_bars_write(
            guard,
            request.provider,
            reason="history_commit_storage_outcome_uncertain",
            repair={"commit_outcome": "uncertain"},
        )
        if isinstance(exc, Exception):
            raise _HistoryCommitStorageOutcomeUncertain(
                generation,
                exc,
            ) from exc
        raise
    return receipt, guard


@dataclass
class _ChartCommitLane:
    loop: asyncio.AbstractEventLoop
    adapter: Any
    instrument: dict[str, Any]
    interval: str
    store_factory: Any
    provider: str
    instrument_id: str
    route_fingerprint: str
    pending: dict[datetime, Bar] = field(default_factory=dict)
    revision_sequences: dict[datetime, int] = field(default_factory=dict)
    committed: dict[datetime, tuple[Any, ...]] = field(default_factory=dict)
    committed_slots: list[datetime] = field(default_factory=list)
    history_pending: deque[_PendingHistoryCommit] = field(default_factory=deque)
    history_turn: bool = False
    task: asyncio.Task[None] | None = None

    def remember_committed(
        self,
        ts: datetime,
        signature: tuple[Any, ...],
    ) -> None:
        if ts in self.committed:
            self.committed[ts] = signature
            return
        if len(self.committed) >= CHART_COMMIT_DEDUPE_TAIL:
            oldest = self.committed_slots[0]
            if ts <= oldest:
                return
            evicted = heapq.heapreplace(self.committed_slots, ts)
            self.committed.pop(evicted)
        else:
            heapq.heappush(self.committed_slots, ts)
        self.committed[ts] = signature


class _HistoryCommitGenerationChanged(RuntimeError):
    def __init__(self, generation: int) -> None:
        self.generation = int(generation)
        super().__init__(f"HISTORY_COMMIT_GENERATION_CHANGED:{self.generation}")


def _finalize_history_commit_receipt(
    lane: _ChartCommitLane,
    request: HistoryCommitRequest,
    receipt: Any,
    *,
    revision_sequence: int,
) -> tuple[CanonicalHistoryCommitReceipt, int]:
    if not isinstance(receipt, CanonicalHistoryCommitReceipt):
        raise _HistoryCommitTerminalError("HISTORY_COMMIT_TYPED_RECEIPT_REQUIRED")
    acknowledged, superseded = _validate_exact_history_bar_receipt(
        request.bars,
        receipt.bar_receipt,
        revision_sequence=revision_sequence,
    )
    for bar in (*acknowledged, *superseded):
        lane.remember_committed(
            bar.ts,
            bar_ingress_revision_signature(bar),
        )

    generation = chart_bars_updated_generation(
        request.interval,
        request.route_fingerprint,
        instrument_id=request.instrument_id,
    )
    if receipt.mutated:
        try:
            generation = publish_chart_bars_updated(
                request.provider,
                request.interval,
                instrument_id=request.instrument_id,
                route_fingerprint=request.route_fingerprint,
                reason="history_commit",
                bars=acknowledged,
                expected_generation=request.canonical_generation,
                repair={
                    "coverage_expanded": receipt.coverage_expanded,
                    "deleted_bars": receipt.bar_receipt.deleted,
                },
            )
        except Exception as exc:
            raise _HistoryCommitTerminalError(
                f"HISTORY_COMMIT_EVENT_PUBLICATION_FAILED:{type(exc).__name__}"
            ) from exc
        if generation <= 0:
            raise _HistoryCommitTerminalError("HISTORY_COMMIT_GENERATION_OWNERSHIP_LOST")
    elif generation != request.canonical_generation:
        raise _HistoryCommitGenerationChanged(generation)
    return receipt, generation


@dataclass(frozen=True, slots=True)
class ChartCommitShutdownOutcome:
    status: str
    pending_bars: int
    pending_history_commits: int
    pending_lanes: int
    task_errors: tuple[str, ...]


class _LiveCommitStageStatus(str, Enum):
    READY = "ready"
    RETRY = "retry"
    STOP = "stop"
    SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class _LiveCommitStage:
    status: _LiveCommitStageStatus
    failures: int
    value: Any = None


@dataclass(frozen=True, slots=True)
class _LiveCommitResult:
    missing: tuple[Bar, ...]
    deferred_cancel: asyncio.CancelledError | None


@dataclass(frozen=True, slots=True)
class _HistoryCommitStage:
    ready: bool
    keep_draining: bool
    value: Any = None


def _chart_commit_operation_timeouts() -> tuple[int, int]:
    statement_timeout_ms = max(2, int(CHART_COMMIT_TIMEOUT_SECONDS * 800))
    lock_timeout_ms = max(
        1,
        min(
            statement_timeout_ms - 1,
            2_000,
            int(CHART_COMMIT_TIMEOUT_SECONDS * 200),
        ),
    )
    return statement_timeout_ms, lock_timeout_ms


def _live_commit_receipt_outcomes(
    batch: list[Bar],
    receipt: CanonicalBarCommitReceipt,
    *,
    revision_sequence: int,
) -> tuple[tuple[Bar, ...], tuple[Bar, ...], tuple[Bar, ...]]:
    if not isinstance(receipt, CanonicalBarCommitReceipt):
        raise TypeError("CHART_COMMIT_TYPED_RECEIPT_REQUIRED")
    if receipt.revision_sequence != revision_sequence:
        raise RuntimeError(
            "CHART_COMMIT_REVISION_SEQUENCE_MISMATCH "
            f"expected={revision_sequence} actual={receipt.revision_sequence}"
        )
    if receipt.rejected:
        raise RuntimeError("CHART_COMMIT_CONFIRMED_BAR_REJECTED")
    acknowledged_signatures = {
        (bar.ts, bar_ingress_revision_signature(bar)) for bar in receipt.acknowledged
    }
    superseded_signatures = {
        (bar.ts, bar_ingress_revision_signature(bar)) for bar in receipt.superseded
    }
    if acknowledged_signatures & superseded_signatures:
        raise RuntimeError("CHART_COMMIT_RECEIPT_OUTCOME_OVERLAP")
    submitted_signatures = {(bar.ts, bar_ingress_revision_signature(bar)) for bar in batch}
    if not (acknowledged_signatures | superseded_signatures) <= submitted_signatures:
        raise RuntimeError("CHART_COMMIT_RECEIPT_UNKNOWN_REVISION")
    settled_signatures = acknowledged_signatures | superseded_signatures
    acknowledged = tuple(
        bar
        for bar in batch
        if (bar.ts, bar_ingress_revision_signature(bar)) in acknowledged_signatures
    )
    superseded = tuple(
        bar
        for bar in batch
        if (bar.ts, bar_ingress_revision_signature(bar)) in superseded_signatures
    )
    missing = tuple(
        bar
        for bar in batch
        if (bar.ts, bar_ingress_revision_signature(bar)) not in settled_signatures
    )
    return acknowledged, superseded, missing


def _pop_live_commit_batch(
    lane: _ChartCommitLane,
    revision_sequence: int,
) -> list[Bar]:
    slots = sorted(
        ts
        for ts, sequence in lane.revision_sequences.items()
        if sequence == revision_sequence and ts in lane.pending
    )
    batch = [lane.pending.pop(ts) for ts in slots]
    for ts in slots:
        lane.revision_sequences.pop(ts, None)
    return batch


def _requeue_live_commit_batch(
    lane: _ChartCommitLane,
    batch: tuple[Bar, ...] | list[Bar],
    revision_sequence: int,
) -> None:
    for bar in batch:
        if bar.ts not in lane.pending:
            lane.pending[bar.ts] = bar
            lane.revision_sequences[bar.ts] = revision_sequence


def _finalize_live_commit_receipt(
    lane: _ChartCommitLane,
    route: Any,
    batch: list[Bar],
    receipt: CanonicalBarCommitReceipt,
    write_guard: ChartBarsWriteGuard,
    *,
    revision_sequence: int,
    expected_generation: int,
) -> tuple[Bar, ...]:
    acknowledged, superseded, missing = _live_commit_receipt_outcomes(
        batch,
        receipt,
        revision_sequence=revision_sequence,
    )
    if receipt.written > 0:
        generation = publish_chart_bars_updated(
            lane.adapter.key,
            lane.interval,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            reason="canonical_commit",
            bars=receipt.acknowledged,
            expected_generation=expected_generation,
        )
        if generation <= 0:
            raise RuntimeError("CHART_COMMIT_GENERATION_OWNERSHIP_LOST")
    end_chart_bars_write(write_guard)
    for bar in (*acknowledged, *superseded):
        lane.remember_committed(bar.ts, bar_ingress_revision_signature(bar))
    if superseded:
        increment_metric(
            "chart_commit_superseded_total",
            len(superseded),
            provider=lane.adapter.key,
            interval=lane.interval,
        )
    return missing


class ChartCommitRuntime:
    """Process-owned, serialized canonical commits for each qualified stream."""

    def __init__(self) -> None:
        self._lanes: dict[ChartCommitKey, _ChartCommitLane] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = False
        self._shutdown_deadline: float | None = None
        self._retry_wake: asyncio.Event | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._accepting and self._loop is loop:
            return
        if any(lane.task is not None and not lane.task.done() for lane in self._lanes.values()):
            raise RuntimeError("CHART_COMMIT_RUNTIME_SHUTDOWN_IN_PROGRESS")
        if self._loop is not None and self._loop is not loop:
            active = any(
                lane.pending
                or lane.history_pending
                or (lane.task is not None and not lane.task.done())
                for lane in self._lanes.values()
            )
            if active:
                raise RuntimeError("CHART_COMMIT_RUNTIME_LOOP_MISMATCH")
            self._lanes.clear()
        self._loop = loop
        self._shutdown_deadline = None
        self._retry_wake = asyncio.Event()
        self._accepting = True
        for lane in self._lanes.values():
            lane.loop = loop
            if (lane.pending or lane.history_pending) and (lane.task is None or lane.task.done()):
                lane.task = self._start_lane_task(lane)

    async def shutdown(
        self,
        *,
        grace_seconds: float = CHART_COMMIT_TIMEOUT_SECONDS,
    ) -> ChartCommitShutdownOutcome:
        if grace_seconds < 0:
            raise ValueError("grace_seconds must be non-negative")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("CHART_COMMIT_RUNTIME_LOOP_MISMATCH")
        self._accepting = False
        self._shutdown_deadline = loop.time() + grace_seconds
        if self._retry_wake is not None:
            self._retry_wake.set()
        tasks = tuple(
            lane.task
            for lane in self._lanes.values()
            if lane.task is not None and not lane.task.done()
        )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        task_errors = tuple(
            f"{type(result).__name__}: {result}"
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        )
        pending_lanes = sum(
            bool(lane.pending or lane.history_pending) for lane in self._lanes.values()
        )
        pending_bars = sum(len(lane.pending) for lane in self._lanes.values())
        pending_history_commits = sum(len(lane.history_pending) for lane in self._lanes.values())
        return ChartCommitShutdownOutcome(
            status=(
                "drained"
                if pending_bars == 0 and pending_history_commits == 0 and not task_errors
                else "pending_rearm"
                if (pending_bars > 0 or pending_history_commits > 0) and not task_errors
                else "task_error"
            ),
            pending_bars=pending_bars,
            pending_history_commits=pending_history_commits,
            pending_lanes=pending_lanes,
            task_errors=task_errors,
        )

    def submit(
        self,
        adapter: Any,
        instrument: dict[str, Any],
        interval: str,
        store_factory: Any,
        bars: list[Bar],
    ) -> int:
        if not self._accepting:
            raise RuntimeError("CHART_COMMIT_RUNTIME_NOT_ACCEPTING")
        route = route_instrument(instrument, expected_source=adapter.key)
        confirmed = [bar for bar in bars if BarState(bar.state) is BarState.CONFIRMED]
        if not confirmed:
            return 0
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            raise RuntimeError("CHART_COMMIT_RUNTIME_LOOP_MISMATCH")
        key = (route.instrument_id, route.fingerprint, str(interval))
        lane = self._live_commit_lane(
            key,
            loop=loop,
            adapter=adapter,
            route=route,
            interval=str(interval),
            store_factory=store_factory,
        )
        accepted = 0
        for bar in confirmed:
            signature = bar_ingress_revision_signature(bar)
            if lane.committed.get(bar.ts) == signature:
                continue
            existing = lane.pending.get(bar.ts)
            if existing is not None and bar_ingress_revision_signature(existing) == signature:
                continue
            lane.pending[bar.ts] = bar
            lane.revision_sequences.pop(bar.ts, None)
            accepted += 1
        set_metric(
            "chart_commit_pending",
            len(lane.pending),
            provider=route.provider,
            interval=str(interval),
        )
        if (lane.pending or lane.history_pending) and (lane.task is None or lane.task.done()):
            lane.task = self._start_lane_task(lane)
        return accepted

    def _live_commit_lane(
        self,
        key: ChartCommitKey,
        *,
        loop: asyncio.AbstractEventLoop,
        adapter: Any,
        route: Any,
        interval: str,
        store_factory: Any,
    ) -> _ChartCommitLane:
        lane = self._lanes.get(key)
        if lane is not None:
            return lane
        lane = _ChartCommitLane(
            loop=loop,
            adapter=adapter,
            instrument=route.instrument,
            interval=interval,
            store_factory=store_factory,
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
        )
        self._lanes[key] = lane
        if len(self._lanes) > 512:
            stale_key = next(
                (
                    candidate
                    for candidate, candidate_lane in self._lanes.items()
                    if candidate != key
                    and not candidate_lane.pending
                    and not candidate_lane.history_pending
                    and (candidate_lane.task is None or candidate_lane.task.done())
                ),
                None,
            )
            if stale_key is not None:
                self._lanes.pop(stale_key, None)
        return lane

    def submit_history_commit(
        self,
        request: HistoryCommitRequest,
        store_factory: Any,
    ) -> HistoryCommitHandle:
        """Admit an immutable provider result without performing storage I/O."""

        if not self._accepting:
            raise RuntimeError("CHART_COMMIT_RUNTIME_NOT_ACCEPTING")
        if not isinstance(request, HistoryCommitRequest):
            raise TypeError("HISTORY_COMMIT_REQUEST_TYPED_REQUIRED")
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            raise RuntimeError("CHART_COMMIT_RUNTIME_LOOP_MISMATCH")
        route = route_instrument(
            dict(request.instrument),
            expected_source=request.provider,
        )
        if route.provider != request.provider:
            raise ValueError("HISTORY_COMMIT_PROVIDER_NOT_CANONICAL")
        key = (route.instrument_id, route.fingerprint, request.interval)
        if key != (
            request.instrument_id,
            request.route_fingerprint,
            request.interval,
        ):
            raise ValueError("HISTORY_COMMIT_ROUTE_MISMATCH")
        completion: asyncio.Future[HistoryCommitOutcome] = loop.create_future()
        lane = self._lanes.get(key)
        if lane is None:
            lane = _ChartCommitLane(
                loop=loop,
                adapter=route.adapter,
                instrument=copy.deepcopy(route.instrument),
                interval=request.interval,
                store_factory=store_factory,
                provider=route.provider,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
            )
            self._lanes[key] = lane
        if len(lane.history_pending) >= HISTORY_COMMIT_QUEUE_LIMIT:
            completion.set_result(HistoryCommitOutcome(status=HistoryCommitStatus.QUEUE_FULL))
            return HistoryCommitHandle(accepted=False, completion=completion)
        immutable_request = HistoryCommitRequest(
            provider=request.provider,
            instrument_id=request.instrument_id,
            route_fingerprint=request.route_fingerprint,
            interval=request.interval,
            starts_at=request.starts_at,
            ends_at=request.ends_at,
            canonical_generation=request.canonical_generation,
            instrument=copy.deepcopy(dict(request.instrument)),
            bars=tuple(request.bars),
            coverage_receipts=tuple(request.coverage_receipts),
        )
        lane.history_pending.append(
            _PendingHistoryCommit(
                request=immutable_request,
                store_factory=store_factory,
                completion=completion,
            )
        )
        lane.history_turn = True
        if lane.task is None or lane.task.done():
            lane.task = self._start_lane_task(lane)
        return HistoryCommitHandle(accepted=True, completion=completion)

    def _start_lane_task(
        self,
        lane: _ChartCommitLane,
    ) -> asyncio.Task[None]:
        return lane.loop.create_task(
            self._drain(lane),
            name=f"chart-commit:{lane.provider}:{lane.interval}",
        )

    async def _drain(
        self,
        lane: _ChartCommitLane,
    ) -> None:
        failures = 0
        while lane.pending or lane.history_pending:
            stage = await self._drain_lane_once(lane, failures)
            failures = stage.failures
            if stage.status is _LiveCommitStageStatus.STOP:
                return
        set_metric(
            "chart_commit_pending",
            0,
            provider=lane.adapter.key,
            interval=lane.interval,
        )

    async def _drain_lane_once(
        self,
        lane: _ChartCommitLane,
        failures: int,
    ) -> _LiveCommitStage:
        if lane.history_pending and (not lane.pending or lane.history_turn):
            if not await self._drain_history_once(lane):
                return _LiveCommitStage(_LiveCommitStageStatus.STOP, failures)
            lane.history_turn = False
            return _LiveCommitStage(_LiveCommitStageStatus.SUCCESS, failures)
        if self._shutdown_deadline is not None and lane.loop.time() >= self._shutdown_deadline:
            return _LiveCommitStage(_LiveCommitStageStatus.STOP, failures)
        store_stage = await self._live_commit_store(lane, failures)
        if store_stage.status is not _LiveCommitStageStatus.READY:
            return store_stage
        sequence_stage = await self._live_commit_sequence(
            lane,
            store_stage.value,
            store_stage.failures,
        )
        if sequence_stage.status is not _LiveCommitStageStatus.READY:
            return sequence_stage
        revision_sequence = int(sequence_stage.value)
        batch = _pop_live_commit_batch(lane, revision_sequence)
        if not batch:
            return _LiveCommitStage(
                _LiveCommitStageStatus.SUCCESS,
                sequence_stage.failures,
            )
        return await self._commit_live_batch_once(
            lane,
            store_stage.value,
            revision_sequence,
            batch,
            sequence_stage.failures,
        )

    async def _retry_live_commit(self, failures: int) -> bool:
        if self._shutdown_deadline is not None:
            return False
        delay = min(0.25 * (2 ** min(failures - 1, 5)), 5.0)
        return await self._wait_to_retry(delay)

    async def _live_commit_store(
        self,
        lane: _ChartCommitLane,
        failures: int,
    ) -> _LiveCommitStage:
        try:
            store = lane.store_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            increment_metric(
                "chart_commit_total",
                provider=lane.adapter.key,
                interval=lane.interval,
                status="store_factory_error",
            )
            status = (
                _LiveCommitStageStatus.RETRY
                if await self._retry_live_commit(failures)
                else _LiveCommitStageStatus.STOP
            )
            return _LiveCommitStage(status=status, failures=failures)
        return _LiveCommitStage(
            status=_LiveCommitStageStatus.READY,
            failures=failures,
            value=store,
        )

    async def _reserve_live_revision_sequence(
        self,
        lane: _ChartCommitLane,
        store: Any,
    ) -> tuple[int, asyncio.CancelledError | None]:
        statement_timeout_ms, lock_timeout_ms = _chart_commit_operation_timeouts()
        with postgres_operation_timeouts(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        ):
            reservation = asyncio.create_task(
                asyncio.to_thread(store.reserve_bar_revision_sequence),
                name=f"chart-commit-sequence:{lane.adapter.key}:{lane.interval}",
            )
        outcome = await settle_physical_task(
            reservation,
            timeout=CHART_COMMIT_TIMEOUT_SECONDS,
        )
        error = outcome.error
        if outcome.task_cancelled:
            error = RuntimeError("CANONICAL_COMMIT_TASK_CANCELLED")
        if error is not None:
            if outcome.cancellation is not None:
                raise outcome.cancellation from error
            raise error
        revision_sequence = int(outcome.result)
        if revision_sequence <= 0:
            raise RuntimeError("CANONICAL_BAR_REVISION_SEQUENCE_UNAVAILABLE")
        if outcome.timed_out:
            increment_metric(
                "chart_commit_total",
                provider=lane.adapter.key,
                interval=lane.interval,
                status="sequence_timeout",
            )
        return revision_sequence, outcome.cancellation

    async def _live_commit_sequence(
        self,
        lane: _ChartCommitLane,
        store: Any,
        failures: int,
    ) -> _LiveCommitStage:
        assigned = {
            sequence for ts, sequence in lane.revision_sequences.items() if ts in lane.pending
        }
        if assigned:
            return _LiveCommitStage(
                status=_LiveCommitStageStatus.READY,
                failures=failures,
                value=min(assigned),
            )
        snapshot = dict(lane.pending)
        try:
            revision_sequence, deferred_cancel = await self._reserve_live_revision_sequence(
                lane,
                store,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            increment_metric(
                "chart_commit_total",
                provider=lane.adapter.key,
                interval=lane.interval,
                status="sequence_error",
            )
            status = (
                _LiveCommitStageStatus.RETRY
                if await self._retry_live_commit(failures)
                else _LiveCommitStageStatus.STOP
            )
            return _LiveCommitStage(status=status, failures=failures)
        for ts, bar in snapshot.items():
            if lane.pending.get(ts) is bar:
                lane.revision_sequences[ts] = revision_sequence
        if deferred_cancel is not None:
            raise deferred_cancel
        return _LiveCommitStage(
            status=_LiveCommitStageStatus.READY,
            failures=failures,
            value=revision_sequence,
        )

    async def _execute_live_commit(
        self,
        lane: _ChartCommitLane,
        store: Any,
        revision_sequence: int,
        batch: list[Bar],
    ) -> _LiveCommitResult:
        write_guard: ChartBarsWriteGuard | None = None
        storage_attempt_started = False
        try:
            statement_timeout_ms, lock_timeout_ms = _chart_commit_operation_timeouts()
            with postgres_operation_timeouts(
                statement_timeout_ms=statement_timeout_ms,
                lock_timeout_ms=lock_timeout_ms,
            ):
                route = route_instrument(
                    lane.instrument,
                    expected_source=lane.adapter.key,
                )
                if (route.instrument_id, route.fingerprint) != (
                    lane.instrument_id,
                    lane.route_fingerprint,
                ):
                    raise RuntimeError("CHART_COMMIT_ROUTE_CHANGED")
                expected_generation = chart_bars_updated_generation(
                    lane.interval,
                    route.fingerprint,
                    instrument_id=route.instrument_id,
                )
                write_guard = begin_chart_bars_write(
                    expected_generation,
                    lane.interval,
                    route.fingerprint,
                    instrument_id=route.instrument_id,
                )
                storage_attempt_started = True
                attempt = asyncio.create_task(
                    lane.adapter.async_commit_chart_bars(
                        batch,
                        interval=lane.interval,
                        store=store,
                        instrument=lane.instrument,
                        revision_sequence=revision_sequence,
                    ),
                    name=f"chart-commit-attempt:{lane.adapter.key}:{lane.interval}",
                )
            outcome = await settle_physical_task(
                attempt,
                timeout=CHART_COMMIT_TIMEOUT_SECONDS,
            )
            error = outcome.error
            if outcome.task_cancelled:
                error = RuntimeError("CANONICAL_COMMIT_TASK_CANCELLED")
            if error is not None:
                if outcome.cancellation is not None:
                    raise outcome.cancellation from error
                raise error
            if outcome.timed_out:
                increment_metric(
                    "chart_commit_total",
                    provider=lane.adapter.key,
                    interval=lane.interval,
                    status="timeout",
                )
            receipt = outcome.result
            missing = _finalize_live_commit_receipt(
                lane,
                route,
                batch,
                receipt,
                write_guard,
                revision_sequence=revision_sequence,
                expected_generation=expected_generation,
            )
            write_guard = None
            return _LiveCommitResult(
                missing=missing,
                deferred_cancel=outcome.cancellation,
            )
        finally:
            if write_guard is not None:
                if storage_attempt_started:
                    invalidate_chart_bars_write(
                        write_guard,
                        lane.adapter.key,
                        reason="canonical_commit_outcome_uncertain",
                        repair={"commit_outcome": "uncertain"},
                    )
                else:
                    end_chart_bars_write(write_guard)

    async def _commit_live_batch_once(
        self,
        lane: _ChartCommitLane,
        store: Any,
        revision_sequence: int,
        batch: list[Bar],
        failures: int,
    ) -> _LiveCommitStage:
        try:
            result = await self._execute_live_commit(
                lane,
                store,
                revision_sequence,
                batch,
            )
        except asyncio.CancelledError:
            _requeue_live_commit_batch(lane, batch, revision_sequence)
            raise
        except Exception:
            failures += 1
            increment_metric(
                "chart_commit_total",
                provider=lane.adapter.key,
                interval=lane.interval,
                status="error",
            )
            _requeue_live_commit_batch(lane, batch, revision_sequence)
            status = (
                _LiveCommitStageStatus.RETRY
                if await self._retry_live_commit(failures)
                else _LiveCommitStageStatus.STOP
            )
            return _LiveCommitStage(status=status, failures=failures)
        if result.missing:
            failures += 1
            increment_metric(
                "chart_commit_total",
                provider=lane.adapter.key,
                interval=lane.interval,
                status="partial",
            )
            _requeue_live_commit_batch(lane, result.missing, revision_sequence)
            set_metric(
                "chart_commit_pending",
                len(lane.pending),
                provider=lane.adapter.key,
                interval=lane.interval,
            )
            if result.deferred_cancel is not None:
                raise result.deferred_cancel
            status = (
                _LiveCommitStageStatus.RETRY
                if await self._retry_live_commit(failures)
                else _LiveCommitStageStatus.STOP
            )
            return _LiveCommitStage(status=status, failures=failures)
        lane.history_turn = bool(lane.history_pending)
        increment_metric(
            "chart_commit_total",
            provider=lane.adapter.key,
            interval=lane.interval,
            status="ok",
        )
        set_metric(
            "chart_commit_pending",
            len(lane.pending),
            provider=lane.adapter.key,
            interval=lane.interval,
        )
        if result.deferred_cancel is not None:
            raise result.deferred_cancel
        return _LiveCommitStage(
            status=_LiveCommitStageStatus.SUCCESS,
            failures=0,
        )

    async def _drain_history_once(self, lane: _ChartCommitLane) -> bool:
        item = lane.history_pending[0]
        if self._shutdown_deadline is not None and lane.loop.time() >= self._shutdown_deadline:
            return False
        preparation = await self._prepare_history_commit(lane, item)
        if not preparation.ready:
            return preparation.keep_draining
        storage = await self._history_storage_attempt(
            lane,
            item,
            preparation.value,
        )
        if not storage.ready:
            return storage.keep_draining
        return self._finalize_history_storage_attempt(lane, item, storage.value)

    def _settle_stale_history_route(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
    ) -> None:
        lane.history_pending.popleft()
        self._settle_history_completion(
            item,
            HistoryCommitOutcome(status=HistoryCommitStatus.STALE_ROUTE),
        )

    def _settle_stale_history_generation(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
        generation: int,
    ) -> None:
        lane.history_pending.popleft()
        self._settle_history_completion(
            item,
            HistoryCommitOutcome(
                status=HistoryCommitStatus.STALE_GENERATION,
                generation=generation,
            ),
        )

    def _history_commit_scope_is_current(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
    ) -> bool:
        request = item.request
        try:
            route = route_instrument(
                dict(request.instrument),
                expected_source=request.provider,
            )
        except Exception:
            self._settle_stale_history_route(lane, item)
            return False
        request_scope = (
            request.instrument_id,
            request.route_fingerprint,
            request.interval,
        )
        if (
            route.instrument_id,
            route.fingerprint,
            request.interval,
        ) != request_scope or request_scope != (
            lane.instrument_id,
            lane.route_fingerprint,
            lane.interval,
        ):
            self._settle_stale_history_route(lane, item)
            return False
        generation = chart_bars_updated_generation(
            request.interval,
            request.route_fingerprint,
            instrument_id=request.instrument_id,
        )
        if generation != request.canonical_generation:
            self._settle_stale_history_generation(lane, item, generation)
            return False
        return True

    async def _reserve_history_revision_sequence(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
        store: Any,
    ) -> _HistoryCommitStage:
        request = item.request
        try:
            reservation = asyncio.create_task(
                asyncio.to_thread(store.reserve_bar_revision_sequence),
                name=f"history-commit-sequence:{request.provider}:{request.interval}",
            )
            outcome = await settle_physical_task(
                reservation,
                timeout=CHART_COMMIT_TIMEOUT_SECONDS,
            )
            if outcome.error is not None:
                raise outcome.error
            if outcome.task_cancelled:
                raise RuntimeError("HISTORY_COMMIT_SEQUENCE_TASK_CANCELLED")
            sequence = int(outcome.result)
            if sequence <= 0:
                self._fail_history_commit(
                    lane,
                    item,
                    code="HISTORY_COMMIT_SEQUENCE_UNAVAILABLE",
                )
                return _HistoryCommitStage(False, True)
            item.revision_sequence = sequence
            if outcome.cancellation is not None:
                raise outcome.cancellation
        except asyncio.CancelledError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            self._fail_history_commit(
                lane,
                item,
                code=f"HISTORY_COMMIT_SEQUENCE_CONTRACT_INVALID:{type(exc).__name__}",
            )
            return _HistoryCommitStage(False, True)
        except Exception as exc:
            if _history_storage_error_is_transient(exc):
                keep_draining = await self._retry_history_commit(item)
                return _HistoryCommitStage(False, keep_draining)
            self._fail_history_commit(
                lane,
                item,
                code=f"HISTORY_COMMIT_SEQUENCE_FAILED:{type(exc).__name__}",
            )
            return _HistoryCommitStage(False, True)
        return _HistoryCommitStage(True, True)

    async def _prepare_history_commit(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
    ) -> _HistoryCommitStage:
        if not self._history_commit_scope_is_current(lane, item):
            return _HistoryCommitStage(False, True)
        try:
            store = item.store_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            keep_draining = await self._retry_history_commit(item)
            return _HistoryCommitStage(False, keep_draining)
        if item.revision_sequence is None:
            reservation = await self._reserve_history_revision_sequence(lane, item, store)
            if not reservation.ready:
                return reservation
        if not self._history_commit_scope_is_current(lane, item):
            return _HistoryCommitStage(False, True)
        return _HistoryCommitStage(True, True, store)

    async def _run_history_storage_attempt(
        self,
        item: _PendingHistoryCommit,
        store: Any,
    ) -> tuple[Any, ChartBarsWriteGuard, asyncio.CancelledError | None]:
        request = item.request
        statement_timeout_ms, lock_timeout_ms = _chart_commit_operation_timeouts()
        with postgres_operation_timeouts(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        ):
            attempt = asyncio.create_task(
                asyncio.to_thread(
                    _commit_history_result_under_write_guard,
                    store,
                    request,
                    int(item.revision_sequence),
                ),
                name=f"history-commit-attempt:{request.provider}:{request.interval}",
            )
        outcome = await settle_physical_task(
            attempt,
            timeout=CHART_COMMIT_TIMEOUT_SECONDS,
        )
        if outcome.error is not None:
            if outcome.cancellation is not None:
                raise outcome.cancellation from outcome.error
            raise outcome.error
        if outcome.task_cancelled:
            raise RuntimeError("HISTORY_COMMIT_TASK_CANCELLED")
        result = outcome.result
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[1], ChartBarsWriteGuard)
        ):
            raise TypeError("HISTORY_COMMIT_WRITE_GUARD_RESULT_REQUIRED")
        receipt, write_guard = result
        return receipt, write_guard, outcome.cancellation

    async def _history_storage_attempt(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
        store: Any,
    ) -> _HistoryCommitStage:
        try:
            result = await self._run_history_storage_attempt(item, store)
        except asyncio.CancelledError:
            raise
        except ChartBarsGenerationChanged as exc:
            if exc.publication_in_progress:
                keep_draining = await self._retry_history_commit(item)
                return _HistoryCommitStage(False, keep_draining)
            self._settle_stale_history_generation(lane, item, exc.observed)
            return _HistoryCommitStage(False, True)
        except _HistoryCommitStorageOutcomeUncertain as exc:
            self._fail_history_commit(
                lane,
                item,
                code=f"HISTORY_COMMIT_STORAGE_OUTCOME_UNCERTAIN:{exc.error_type}",
                generation=exc.generation,
            )
            return _HistoryCommitStage(False, True)
        except (AttributeError, TypeError, ValueError) as exc:
            self._fail_history_commit(
                lane,
                item,
                code=f"HISTORY_COMMIT_STORAGE_CONTRACT_INVALID:{type(exc).__name__}",
            )
            return _HistoryCommitStage(False, True)
        except Exception as exc:
            if _history_storage_error_is_transient(exc):
                keep_draining = await self._retry_history_commit(item)
                return _HistoryCommitStage(False, keep_draining)
            self._fail_history_commit(
                lane,
                item,
                code=f"HISTORY_COMMIT_STORAGE_FAILED:{type(exc).__name__}",
            )
            return _HistoryCommitStage(False, True)
        return _HistoryCommitStage(True, True, result)

    def _finalize_history_storage_attempt(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
        result: tuple[Any, ChartBarsWriteGuard, asyncio.CancelledError | None],
    ) -> bool:
        request = item.request
        receipt, write_guard, deferred_cancel = result
        try:
            typed_receipt, generation = _finalize_history_commit_receipt(
                lane,
                request,
                receipt,
                revision_sequence=int(item.revision_sequence),
            )
            end_chart_bars_write(write_guard)
            write_guard = None
        except _HistoryCommitGenerationChanged as exc:
            observed_generation = invalidate_chart_bars_write(
                write_guard,
                request.provider,
                reason="history_commit_outcome_uncertain",
                repair={"commit_outcome": "uncertain"},
            )
            write_guard = None
            lane.history_pending.popleft()
            self._settle_history_completion(
                item,
                HistoryCommitOutcome(
                    status=HistoryCommitStatus.STALE_GENERATION,
                    receipt=(
                        receipt if isinstance(receipt, CanonicalHistoryCommitReceipt) else None
                    ),
                    generation=max(exc.generation, observed_generation),
                ),
            )
            if deferred_cancel is not None:
                raise deferred_cancel
            return True
        except _HistoryCommitTerminalError as exc:
            observed_generation = invalidate_chart_bars_write(
                write_guard,
                request.provider,
                reason="history_commit_outcome_uncertain",
                repair={"commit_outcome": "uncertain"},
            )
            write_guard = None
            self._fail_history_commit(
                lane,
                item,
                code=exc.code,
                receipt=(receipt if isinstance(receipt, CanonicalHistoryCommitReceipt) else None),
                generation=observed_generation,
            )
            return True
        finally:
            if write_guard is not None:
                invalidate_chart_bars_write(
                    write_guard,
                    request.provider,
                    reason="history_commit_outcome_uncertain",
                    repair={"commit_outcome": "uncertain"},
                )
        lane.history_pending.popleft()
        self._settle_history_completion(
            item,
            HistoryCommitOutcome(
                status=(
                    HistoryCommitStatus.COMMITTED
                    if typed_receipt.mutated
                    else HistoryCommitStatus.NO_CHANGE
                ),
                receipt=typed_receipt,
                generation=generation,
            ),
        )
        if deferred_cancel is not None:
            raise deferred_cancel
        return True

    async def _retry_history_commit(self, item: _PendingHistoryCommit) -> bool:
        item.failures += 1
        increment_metric(
            "history_commit_retry_total",
            provider=item.request.provider,
            interval=item.request.interval,
        )
        if self._shutdown_deadline is not None:
            return False
        delay = min(0.25 * (2 ** min(item.failures - 1, 5)), 5.0)
        return await self._wait_to_retry(delay)

    def _fail_history_commit(
        self,
        lane: _ChartCommitLane,
        item: _PendingHistoryCommit,
        *,
        code: str,
        receipt: CanonicalHistoryCommitReceipt | None = None,
        generation: int | None = None,
    ) -> None:
        if not lane.history_pending or lane.history_pending[0] is not item:
            raise RuntimeError("HISTORY_COMMIT_QUEUE_OWNERSHIP_LOST")
        lane.history_pending.popleft()
        increment_metric(
            "history_commit_terminal_total",
            provider=item.request.provider,
            interval=item.request.interval,
            code=code,
        )
        self._settle_history_completion(
            item,
            HistoryCommitOutcome(
                status=HistoryCommitStatus.FAILED,
                receipt=receipt,
                generation=generation,
                error_code=code,
            ),
        )

    @staticmethod
    def _settle_history_completion(
        item: _PendingHistoryCommit,
        outcome: HistoryCommitOutcome,
    ) -> None:
        if not item.completion.done():
            item.completion.set_result(outcome)

    async def _wait_to_retry(self, delay: float) -> bool:
        if self._shutdown_deadline is not None:
            return False
        wake = self._retry_wake
        if wake is None:
            raise RuntimeError("CHART_COMMIT_RUNTIME_NOT_STARTED")
        sleep_task = asyncio.create_task(asyncio.sleep(delay))
        wake_task = asyncio.create_task(wake.wait())
        try:
            done, pending = await asyncio.wait(
                (sleep_task, wake_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            pending = {task for task in (sleep_task, wake_task) if not task.done()}
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if wake_task in done:
            wake.clear()
        return self._shutdown_deadline is None


chart_commits = ChartCommitRuntime()


async def commit_history_repair_result(
    *,
    instrument: Mapping[str, Any],
    intent: HistoryRepairIntent,
    terminal: ProviderHistoryTerminal,
    bars: tuple[Bar, ...],
    coverage_receipts: tuple[HistoryCoverageReceipt, ...],
    store_factory: Any,
    error_code: str | None,
) -> HistoryRepairOutcome:
    """Submit one fetched provider range to the canonical serialized commit lane."""

    handle = chart_commits.submit_history_commit(
        HistoryCommitRequest(
            provider=intent.provider,
            instrument_id=intent.instrument_id,
            route_fingerprint=intent.route_fingerprint,
            interval=intent.timeframe,
            starts_at=intent.starts_at,
            ends_at=intent.ends_at,
            canonical_generation=intent.canonical_generation,
            instrument=copy.deepcopy(instrument),
            bars=bars,
            coverage_receipts=coverage_receipts,
        ),
        store_factory,
    )
    physical = await settle_physical_task(handle.completion)
    if physical.error is not None:
        if physical.cancellation is not None:
            raise physical.cancellation from physical.error
        raise physical.error
    if physical.task_cancelled:
        raise RuntimeError("HISTORY_COMMIT_HANDLE_CANCELLED")
    commit = physical.result
    if not isinstance(commit, HistoryCommitOutcome):
        raise TypeError("HISTORY_COMMIT_OUTCOME_TYPED_REQUIRED")
    status_map = {
        HistoryCommitStatus.COMMITTED: HistoryRepairOutcomeStatus.COMMITTED,
        HistoryCommitStatus.NO_CHANGE: HistoryRepairOutcomeStatus.NO_CHANGE,
        HistoryCommitStatus.STALE_GENERATION: HistoryRepairOutcomeStatus.STALE_GENERATION,
        HistoryCommitStatus.STALE_ROUTE: HistoryRepairOutcomeStatus.STALE_ROUTE,
        HistoryCommitStatus.QUEUE_FULL: HistoryRepairOutcomeStatus.COMMIT_QUEUE_FULL,
        HistoryCommitStatus.FAILED: HistoryRepairOutcomeStatus.COMMIT_FAILED,
    }
    receipt = commit.receipt
    status = status_map[commit.status]
    if status in {
        HistoryRepairOutcomeStatus.COMMITTED,
        HistoryRepairOutcomeStatus.NO_CHANGE,
    } and terminal in {
        ProviderHistoryTerminal.INCOMPLETE,
        ProviderHistoryTerminal.ROLLING_PARTIAL,
    }:
        status = HistoryRepairOutcomeStatus.INCOMPLETE
    outcome = HistoryRepairOutcome(
        status=status,
        intent=intent,
        terminal=terminal,
        written_bars=(receipt.bar_receipt.written if receipt is not None else 0),
        deleted_bars=(receipt.bar_receipt.deleted if receipt is not None else 0),
        coverage_inserted=(receipt.coverage_inserted if receipt is not None else 0),
        coverage_expanded=(receipt.coverage_expanded if receipt is not None else False),
        generation=commit.generation,
        error_code=error_code,
    )
    if physical.cancellation is not None:
        raise physical.cancellation
    return outcome
