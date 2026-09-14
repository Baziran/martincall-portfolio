from __future__ import annotations

import asyncio
import logging
import sys
from bisect import bisect_left
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from aef_terminal.data.provider_contract import (
    HistoryContractResolution,
    HistoryRepairAdmission,
    HistoryRepairIntent,
    HistoryRepairPriority,
    HistoryRequestAdmissionIdentity,
    HistoryRequestUnsupportedError,
    InstrumentRoute,
)
from aef_terminal.data.providers import (
    async_load_provider_bars,
    load_provider_bars,
    read_provider_history_page,
    route_instrument,
)
from aef_terminal.domain import Bar
from aef_terminal.engine.history_repair import ConfirmedHistoryGap, confirmed_history_gap_ranges
from aef_terminal.data.provider_sessions import provider_session_bar_slot_coverage_between
from aef_terminal.runtime.derived_timeframes import derive_chart_bars
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    chart_bars_updated_generation,
    require_chart_bars_generation,
    wait_for_chart_bars_stable,
)
from aef_terminal.runtime.timeframes import (
    ChartHistoryRangeError as ChartHistoryRangeError,
    HistoryRangeWindow,
    chart_history_range_window,
    canonical_bar_source_interval,
    interval_bucket,
    interval_seconds,
)
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts
from aef_terminal.runtime.async_tasks import (
    run_physical_executor_call,
    run_physical_thread_call,
)
from aef_terminal.data.history_coverage import (
    HistoryCoverageContract,
    HistoryCoverageReceipt,
    current_history_coverage_contract,
    history_coverage_union,
    history_range_is_covered,
)
from aef_terminal.ui.services.chart_stream_gap_repair import (
    history_repair_outcome_payload,
    latest_chart_history_repair_outcome,
    schedule_chart_cached_gap_repair,
    select_chart_history_repair,
)


def _require_canonical_generation(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("canonical_generation must be a non-negative integer")


@dataclass(frozen=True)
class ConfirmedChartHistoryLoad:
    bars: tuple[Bar, ...]
    warning: str
    coverage: dict[str, Any]
    canonical_generation: int

    def __post_init__(self) -> None:
        _require_ordered_bar_tuple(self.bars, field="confirmed chart history bars")
        if not isinstance(self.warning, str):
            raise TypeError("confirmed chart history warning must be a string")
        if not isinstance(self.coverage, dict):
            raise TypeError("confirmed chart history coverage must be a mapping")
        _require_canonical_generation(self.canonical_generation)


@dataclass(frozen=True, slots=True)
class ConfirmedChartHistoryPage:
    bars: tuple[Bar, ...]
    next_before_ts: datetime | None
    has_more: bool
    canonical_generation: int

    def __post_init__(self) -> None:
        _require_ordered_bar_tuple(self.bars, field="confirmed chart history page bars")
        if type(self.has_more) is not bool:
            raise TypeError("confirmed chart history page has_more must be a boolean")
        if self.next_before_ts is not None and (
            not isinstance(self.next_before_ts, datetime)
            or self.next_before_ts.tzinfo is None
            or self.next_before_ts.utcoffset() is None
        ):
            raise ValueError("confirmed chart history page cursor must be timezone-aware")
        if self.has_more is False and self.next_before_ts is not None:
            raise ValueError("completed confirmed chart history page cannot carry a cursor")
        _require_canonical_generation(self.canonical_generation)


def _require_ordered_bar_tuple(bars: object, *, field: str) -> tuple[Bar, ...]:
    if not isinstance(bars, tuple) or any(not isinstance(bar, Bar) for bar in bars):
        raise TypeError(f"{field} must be a Bar tuple")
    if any(right.ts <= left.ts for left, right in zip(bars, bars[1:], strict=False)):
        raise ValueError(f"{field} must be strictly timestamp-ordered")
    return bars


@dataclass(frozen=True, slots=True)
class _ExactCoveragePlan:
    requested_range: str
    requested_start: datetime
    requested_end: datetime
    request_identity: HistoryRequestAdmissionIdentity | None
    contract: HistoryCoverageContract | None
    receipts: tuple[HistoryCoverageReceipt, ...]
    uncovered: tuple[tuple[datetime, datetime], ...]
    supported: bool
    error_code: str | None = None
    audit_checked: bool = False

    @property
    def complete(self) -> bool:
        return bool(
            self.contract is not None
            and not self.error_code
            and history_range_is_covered(
                self.receipts,
                starts_at=self.requested_start,
                ends_at=self.requested_end,
            )
        )


_inflight_lock = asyncio.Lock()
_ChartCacheKey = tuple[
    str,
    str,
    str,
    str,
    str,
    bool,
    datetime | None,
    datetime | None,
]
_InflightLoad = tuple[asyncio.Task[ConfirmedChartHistoryLoad], int, int]
_inflight_tasks: dict[_ChartCacheKey, _InflightLoad] = {}
_chart_history_accepting = True
_CachedHistoryResult = tuple[float, int, int, int, ConfirmedChartHistoryLoad]
_result_cache: dict[_ChartCacheKey, _CachedHistoryResult] = {}
_cache_generation: dict[_ChartCacheKey, int] = {}
_RESULT_CACHE_TTL_SECONDS = 11.5
_RESULT_CACHE_MAX_ENTRIES = 128
_RESULT_CACHE_MAX_BARS = 96_000
_RESULT_CACHE_MAX_BYTES = 64 * 1024 * 1024
_RESULT_CACHE_ESTIMATED_BYTES_PER_BAR = 512
_HISTORY_PAGE_STATEMENT_TIMEOUT_MS = 1500
_HISTORY_PAGE_LOCK_TIMEOUT_MS = 250
_RESULT_CACHE_ESTIMATED_ENTRY_OVERHEAD_BYTES = 64 * 1024
_LOAD_DEADLINE_GRACE_SECONDS = 2.0
_OWNED_TASK_CANCELLATION_MARGIN_SECONDS = 1.0
_AUTOMATIC_SCHEDULE_SCAN_MAX_SPAN = timedelta(days=8)
_BROAD_HISTORY_PREFIX_MIN_SPAN = timedelta(days=7)
_LOGGER = logging.getLogger(__name__)


def start_chart_history_runtime() -> None:
    global _chart_history_accepting
    if _inflight_tasks:
        raise RuntimeError("CHART_HISTORY_START_WITH_ACTIVE_TASKS")
    _result_cache.clear()
    _cache_generation.clear()
    _chart_history_accepting = True


async def shutdown_chart_history_runtime() -> None:
    global _chart_history_accepting
    _chart_history_accepting = False
    async with _inflight_lock:
        tasks = tuple({owned[0] for owned in _inflight_tasks.values()})
        for task in tasks:
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    async with _inflight_lock:
        _inflight_tasks.clear()
        _result_cache.clear()
        _cache_generation.clear()


async def _run_chart_history_blocking(
    operation: Callable[[], Any],
    *,
    executor: Executor | None,
) -> Any:
    if executor is None:
        return await run_physical_thread_call(operation)
    return await run_physical_executor_call(executor, operation)


def _elapsed_coverage_window(
    window: HistoryRangeWindow,
    interval: str,
    *,
    now: datetime | None = None,
) -> HistoryRangeWindow:
    """Project a chart read window onto fully elapsed provider buckets only."""

    starts_at = window.starts_at
    if starts_at is None:
        raise ValueError("bounded chart history requires an exact start")
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    ends_at = min(
        window.ends_at.astimezone(UTC),
        interval_bucket(current, interval),
    )
    if ends_at <= starts_at.astimezone(UTC):
        raise ValueError("chart history has no fully elapsed coverage range")
    return HistoryRangeWindow(
        range_key=window.range_key,
        starts_at=starts_at.astimezone(UTC),
        ends_at=ends_at,
    )


def _result_cache_weight(
    result: ConfirmedChartHistoryLoad,
) -> tuple[int, int]:
    """Return a bounded-cost cache weight without walking every retained bar."""

    bar_count = len(result.bars)
    estimated_bytes = (
        _RESULT_CACHE_ESTIMATED_ENTRY_OVERHEAD_BYTES
        + sys.getsizeof(result)
        + sys.getsizeof(result.bars)
        + sys.getsizeof(result.warning)
        + sys.getsizeof(result.coverage)
        + len(result.warning or "") * 4
        + bar_count * _RESULT_CACHE_ESTIMATED_BYTES_PER_BAR
    )
    return bar_count, max(int(estimated_bytes), 0)


def _result_cache_entry(
    cached_at: float,
    event_generation: int,
    result: ConfirmedChartHistoryLoad,
) -> _CachedHistoryResult:
    bar_count, estimated_bytes = _result_cache_weight(result)
    return (
        float(cached_at),
        int(event_generation),
        bar_count,
        estimated_bytes,
        result,
    )


def _prune_result_cache(now: float) -> None:
    """Eagerly enforce freshness and absolute cache memory/work budgets."""

    expired = [
        key
        for key, cached in _result_cache.items()
        if now - float(cached[0]) > _RESULT_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _result_cache.pop(key, None)
        if key not in _inflight_tasks:
            _cache_generation.pop(key, None)

    total_bars = sum(cached[2] for cached in _result_cache.values())
    total_bytes = sum(cached[3] for cached in _result_cache.values())
    overflow_entries = max(len(_result_cache) - _RESULT_CACHE_MAX_ENTRIES, 0)
    if (
        overflow_entries <= 0
        and total_bars <= _RESULT_CACHE_MAX_BARS
        and total_bytes <= _RESULT_CACHE_MAX_BYTES
    ):
        return

    for key, cached in sorted(_result_cache.items(), key=lambda item: item[1][0]):
        if (
            overflow_entries <= 0
            and total_bars <= _RESULT_CACHE_MAX_BARS
            and total_bytes <= _RESULT_CACHE_MAX_BYTES
        ):
            break
        _result_cache.pop(key, None)
        if key not in _inflight_tasks:
            _cache_generation.pop(key, None)
        overflow_entries = max(overflow_entries - 1, 0)
        total_bars -= cached[2]
        total_bytes -= cached[3]


def confirmed_chart_bars_cache_key(
    provider: str,
    instrument: dict[str, Any],
    interval: str,
    range_: str,
    *,
    refresh_provider: bool = False,
    window: HistoryRangeWindow | None = None,
) -> _ChartCacheKey:
    route = route_instrument(instrument, expected_source=provider)
    return (
        route.provider,
        route.instrument_id,
        route.fingerprint,
        str(interval),
        str(range_),
        bool(refresh_provider),
        window.starts_at if window is not None else None,
        window.ends_at if window is not None else None,
    )


def _coverage_contract(
    route: InstrumentRoute,
    interval: str,
    identity: HistoryRequestAdmissionIdentity,
) -> HistoryCoverageContract:
    if identity.contract_resolution is not HistoryContractResolution.EXACT:
        raise ValueError("exact coverage requires an exact provider contract identity")
    return current_history_coverage_contract(
        provider=route.provider,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        timeframe=interval,
        request_mode=identity.request_mode,
        request_type=identity.request_type.value,
        provider_source=identity.provider_source,
        provider_contract_id=identity.provider_contract_id,
        provider_contract_type=identity.provider_contract_type,
        data_type=identity.data_type,
    )


def _repair_capability_plan(
    *,
    route: InstrumentRoute,
    interval: str,
    range_: str,
    window: HistoryRangeWindow,
) -> _ExactCoveragePlan:
    """Build repair capability metadata without a storage coverage read."""

    requested_start = window.starts_at
    if requested_start is None:
        raise ValueError("bounded chart history requires an exact start")
    if not route.adapter.capabilities.gap_repair:
        return _ExactCoveragePlan(
            requested_range=range_,
            requested_start=requested_start,
            requested_end=window.ends_at,
            request_identity=None,
            contract=None,
            receipts=(),
            uncovered=(),
            supported=False,
        )
    try:
        identity = route.adapter.history_request_identity(route.instrument)
    except HistoryRequestUnsupportedError as exc:
        return _ExactCoveragePlan(
            requested_range=range_,
            requested_start=requested_start,
            requested_end=window.ends_at,
            request_identity=None,
            contract=None,
            receipts=(),
            uncovered=(),
            supported=False,
            error_code=exc.code,
        )
    contract = (
        _coverage_contract(route, interval, identity)
        if (
            identity.contract_resolution is HistoryContractResolution.EXACT
            and route.adapter.capabilities.exact_history_snapshot_authority
        )
        else None
    )
    return _ExactCoveragePlan(
        requested_range=range_,
        requested_start=requested_start,
        requested_end=window.ends_at,
        request_identity=identity,
        contract=contract,
        receipts=(),
        uncovered=(),
        supported=True,
    )


def _confirmed_timestamps(
    bars: Sequence[Bar],
    *,
    interval: str,
    starts_at: datetime,
    ends_at: datetime,
) -> tuple[datetime, ...]:
    step_seconds = interval_seconds(interval)
    timestamps: set[datetime] = set()
    for bar in bars:
        if bar.closed is False:
            continue
        timestamp = bar.ts.astimezone(UTC)
        if (
            timestamp.microsecond
            or int(timestamp.timestamp()) % step_seconds
            or not starts_at <= timestamp < ends_at
        ):
            continue
        timestamps.add(timestamp)
    return tuple(sorted(timestamps))


def _contiguous_schedule_gap_ranges(
    expected_slots: Sequence[datetime],
    *,
    starts_at: datetime,
    ends_at: datetime,
    interval: str,
) -> tuple[tuple[datetime, datetime], ...]:
    step = timedelta(seconds=interval_seconds(interval))
    slots = sorted(
        {
            value.astimezone(UTC)
            for value in expected_slots or ()
            if isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
            and starts_at <= value.astimezone(UTC) < ends_at
        }
    )
    if not slots:
        return ()
    ranges: list[tuple[datetime, datetime]] = []
    range_start = slots[0]
    cursor = slots[0] + step
    for slot in slots[1:]:
        if slot != cursor:
            ranges.append((range_start, cursor))
            range_start = slot
        cursor = slot + step
    ranges.append((range_start, min(cursor, ends_at)))
    return tuple(ranges)


def _schedule_qualified_gap_ranges(
    gaps: tuple[ConfirmedHistoryGap, ...],
    *,
    route: InstrumentRoute,
    interval: str,
    store: Any,
) -> tuple[tuple[datetime, datetime], ...]:
    """Classify scheduled-market gaps from one bounded materialized-schedule read.

    Gaps outside the bounded newest schedule scan, or any unverified/truncated
    schedule result, remain unknown and are skipped.  Automatic scheduled-market
    repair requires a positive provider schedule fact; explicit refresh and stale
    tail demands use their separate bounded provider-history paths.  The schedule
    never fabricates bars.
    """

    if not gaps:
        return ()
    schedule_ends_at = max(gap.ends_at for gap in gaps)
    schedule_starts_at = max(
        min(gap.starts_at for gap in gaps),
        schedule_ends_at - _AUTOMATIC_SCHEDULE_SCAN_MAX_SPAN,
    )
    step_seconds = interval_seconds(interval)
    schedule_slot_limit = (
        int((schedule_ends_at - schedule_starts_at).total_seconds() // step_seconds) + 1
    )
    try:
        coverage = provider_session_bar_slot_coverage_between(
            schedule_starts_at,
            schedule_ends_at,
            interval,
            store=store,
            instrument=route.instrument,
            limit=schedule_slot_limit,
        )
    except Exception:
        coverage = None
    if coverage is None or (coverage.schedule_state != "verified" or coverage.truncated):
        return ()

    expected_slots = tuple(
        sorted(
            {
                value.astimezone(UTC)
                for value in coverage.expected_slots
                if isinstance(value, datetime)
                and value.tzinfo is not None
                and value.utcoffset() is not None
                and schedule_starts_at <= value.astimezone(UTC) < schedule_ends_at
            }
        )
    )
    candidates: list[tuple[datetime, datetime]] = []
    for gap in gaps:
        if gap.starts_at < schedule_starts_at or gap.ends_at > schedule_ends_at:
            continue
        first = bisect_left(expected_slots, gap.starts_at)
        last = bisect_left(expected_slots, gap.ends_at, lo=first)
        candidates.extend(
            _contiguous_schedule_gap_ranges(
                expected_slots[first:last],
                starts_at=gap.starts_at,
                ends_at=gap.ends_at,
                interval=interval,
            )
        )
    return tuple(candidates)


def _automatic_snapshot_repair_candidates(
    *,
    route: InstrumentRoute,
    interval: str,
    bars: Sequence[Bar],
    plan: _ExactCoveragePlan,
    store: Any,
) -> tuple[tuple[datetime, datetime], ...]:
    """Qualify automatic repair demands without reconstructing a calendar.

    Empty history and a broad missing prefix are explicit DB-history demands.
    Internal timestamp discontinuities are automatic gaps for a continuous provider,
    for a sub-seven-day candidate whose materialized provider schedule verifies exact
    expected-open slots, and for a broad seven-day-or-longer history hole. Unknown or
    truncated schedule coverage is not a gap fact. A normal chart load never treats an
    unknown tail, overnight, or weekend as a gap.
    """

    timestamps = _confirmed_timestamps(
        bars,
        interval=interval,
        starts_at=plan.requested_start,
        ends_at=plan.requested_end,
    )
    if not timestamps:
        return ((plan.requested_start, plan.requested_end),)

    continuous = route.adapter.continuous_session(route.instrument) is True
    candidates: list[tuple[datetime, datetime]] = []
    prefix_span = timestamps[0] - plan.requested_start
    if prefix_span > timedelta(0) and (continuous or prefix_span >= _BROAD_HISTORY_PREFIX_MIN_SPAN):
        candidates.append((plan.requested_start, timestamps[0]))

    internal_gaps = confirmed_history_gap_ranges(
        bars,
        interval,
        requested_from=plan.requested_start,
        requested_to=plan.requested_end,
        include_prefix=False,
        include_tail=False,
    )
    if continuous:
        candidates.extend((gap.starts_at, gap.ends_at) for gap in internal_gaps)
        return tuple(candidates)

    scheduled_gaps: list[ConfirmedHistoryGap] = []
    for gap in internal_gaps:
        gap_span = gap.ends_at - gap.starts_at
        if gap_span >= _BROAD_HISTORY_PREFIX_MIN_SPAN:
            candidates.append((gap.starts_at, gap.ends_at))
            continue
        scheduled_gaps.append(gap)
    candidates.extend(
        _schedule_qualified_gap_ranges(
            tuple(scheduled_gaps),
            route=route,
            interval=interval,
            store=store,
        )
    )
    return tuple(sorted(candidates, key=lambda candidate: candidate[:2]))


def _candidate_dedupe_receipts(
    *,
    route: InstrumentRoute,
    plan: _ExactCoveragePlan,
    candidates: tuple[tuple[datetime, datetime], ...],
    store: Any,
) -> tuple[HistoryCoverageReceipt, ...] | None:
    """Read one indexed candidate envelope only when durable dedupe is required."""

    if (
        not candidates
        or plan.contract is None
        or route.adapter.capabilities.exact_history_snapshot_authority is not True
    ):
        return None
    starts_at = min(candidate[0] for candidate in candidates)
    ends_at = max(candidate[1] for candidate in candidates)
    receipts = tuple(
        store.read_history_coverage(
            plan.contract,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    )
    if any(
        not isinstance(receipt, HistoryCoverageReceipt) or receipt.contract != plan.contract
        for receipt in receipts
    ):
        raise TypeError("HISTORY_COVERAGE_RECEIPT_CONTRACT_MISMATCH")
    return receipts


def _select_snapshot_repair_intent(
    *,
    route: InstrumentRoute,
    interval: str,
    bars: Sequence[Bar],
    plan: _ExactCoveragePlan,
    store: Any,
    canonical_generation: int,
    explicit_refresh: bool,
    explicit_demand: HistoryRangeWindow | None = None,
) -> HistoryRepairIntent | None:
    """Plan from an explicit refresh or a concrete DB-series absence.

    Generic chart work is driven by the returned confirmed series.  A concrete
    candidate may perform one bounded receipt read for repair dedupe only; receipts
    never gate calculation.
    """

    if explicit_demand is not None:
        if explicit_demand.starts_at is None:
            raise ValueError("explicit history repair demand requires an exact start")
        candidate_from = max(plan.requested_start, explicit_demand.starts_at)
        candidate_to = min(plan.requested_end, explicit_demand.ends_at)
        candidates = ((candidate_from, candidate_to),) if candidate_to > candidate_from else ()
        dedupe_receipts = ()
    elif explicit_refresh:
        candidates = ((plan.requested_start, plan.requested_end),)
        dedupe_receipts = ()
    else:
        candidates = _automatic_snapshot_repair_candidates(
            route=route,
            interval=interval,
            bars=bars,
            plan=plan,
            store=store,
        )
        dedupe_receipts = _candidate_dedupe_receipts(
            route=route,
            plan=plan,
            candidates=candidates,
            store=store,
        )
    for candidate_from, candidate_to in reversed(candidates):
        intent = select_chart_history_repair(
            route=route,
            interval=interval,
            requested_from=candidate_from,
            requested_to=candidate_to,
            canonical_generation=canonical_generation,
            priority=HistoryRepairPriority.FOREGROUND,
            store=store,
            classify_small_recent_gap=False,
            dedupe_receipts=dedupe_receipts,
        )
        if intent is not None:
            return intent
    return None


def _repair_payload(
    admission: HistoryRepairAdmission | None,
    *,
    status: str,
    error_code: str | None = None,
    terminal_outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if terminal_outcome is not None:
        return {
            "attempted": True,
            "phase": "terminal",
            "status": str(terminal_outcome["status"]),
            "code": terminal_outcome.get("code"),
            "newly_admitted": False,
            "completion_subscribed": False,
            "retry_after_seconds": 0.0,
            "requested_from": terminal_outcome["requested_from"],
            "requested_to": terminal_outcome["requested_to"],
            "target_from": terminal_outcome["target_from"],
            "target_to": terminal_outcome["target_to"],
            "remaining_from": terminal_outcome["remaining_from"],
            "remaining_to": terminal_outcome["remaining_to"],
            "priority": None,
            "terminal_outcome": dict(terminal_outcome),
        }
    if admission is None:
        return {
            "attempted": False,
            "phase": "idle",
            "status": status,
            "code": error_code,
            "newly_admitted": False,
            "completion_subscribed": False,
            "retry_after_seconds": 0.0,
            "target_from": None,
            "target_to": None,
            "remaining_from": None,
            "remaining_to": None,
        }
    intent = admission.intent
    return {
        "attempted": bool(admission.newly_admitted),
        "phase": "admission",
        "status": admission.status.value,
        "code": None,
        "newly_admitted": admission.newly_admitted,
        "completion_subscribed": admission.completion_subscribed,
        "retry_after_seconds": admission.retry_after_seconds,
        "requested_from": intent.starts_at.isoformat(),
        "requested_to": intent.ends_at.isoformat(),
        "target_from": intent.target_starts_at.isoformat(),
        "target_to": intent.target_ends_at.isoformat(),
        "remaining_from": intent.target_starts_at.isoformat(),
        "remaining_to": intent.ends_at.isoformat(),
        "priority": intent.priority.name.lower(),
    }


def _history_coverage_payload(
    plan: _ExactCoveragePlan,
    bars: Sequence[Bar],
    repair: dict[str, Any],
    *,
    route: InstrumentRoute,
    interval: str,
) -> dict[str, Any]:
    bar_times = sorted(bar.ts.astimezone(UTC) for bar in bars)
    complete = plan.complete if plan.audit_checked else False
    if not plan.audit_checked:
        state = "not_checked"
    elif complete:
        state = "complete"
    elif plan.receipts:
        state = "partial"
    else:
        state = "unknown"
    pending = plan.uncovered[-1] if plan.audit_checked and plan.uncovered else None
    union = history_coverage_union(plan.receipts) if plan.audit_checked else ()
    verified_from = None
    if complete:
        verified_from = plan.requested_start.isoformat()
    elif union and union[0][0] <= plan.requested_start:
        verified_from = plan.requested_start.isoformat()
    absence_verification_supported = bool(plan.supported and plan.contract is not None)
    absence_error_code = None
    if not absence_verification_supported:
        absence_error_code = plan.error_code or (
            "PROVIDER_ABSENCE_VERIFICATION_UNAVAILABLE"
            if plan.supported
            else "PROVIDER_EXACT_HISTORY_RANGE_UNAVAILABLE"
        )
    repair_from = repair.get("requested_from")
    repair_to = repair.get("requested_to")
    return {
        "provider": route.provider,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": interval,
        "requested_range": plan.requested_range,
        "requested_start": plan.requested_start.isoformat(),
        "requested_end": plan.requested_end.isoformat(),
        "state": state,
        "provider_range_complete": complete,
        "verified_from": verified_from,
        "pending_from": pending[0].isoformat() if pending else None,
        "pending_to": pending[1].isoformat() if pending else None,
        "truncated": False,
        "has_older": True if pending is not None else None,
        "display_state": "loaded" if bar_times else "empty",
        "loaded_from": bar_times[0].isoformat() if bar_times else None,
        "loaded_to": bar_times[-1].isoformat() if bar_times else None,
        "error_code": plan.error_code,
        "bar_repair": {
            "supported": plan.supported,
            "phase": str(repair.get("phase") or "idle"),
            "status": str(repair.get("status") or "unknown"),
            "error_code": repair.get("code"),
            "requested_from": repair_from,
            "requested_to": repair_to,
            "target_from": repair.get("target_from"),
            "target_to": repair.get("target_to"),
            "remaining_from": repair.get("remaining_from"),
            "remaining_to": repair.get("remaining_to"),
        },
        "absence_verification": {
            "supported": absence_verification_supported,
            "state": (
                state
                if absence_verification_supported and plan.audit_checked
                else "not_checked"
                if absence_verification_supported
                else "unsupported"
            ),
            "error_code": absence_error_code,
            "requested_from": plan.requested_start.isoformat(),
            "requested_to": plan.requested_end.isoformat(),
        },
        "repair": repair,
    }


async def _load_confirmed_chart_bars(
    provider: str,
    instrument: dict[str, Any],
    interval: str,
    range_: str,
    timeout: float,
    *,
    store: Any | None,
    refresh_provider: bool,
    executor: Executor | None,
    window: HistoryRangeWindow | None = None,
    repair_window: HistoryRangeWindow | None = None,
    load_generation: int | None = None,
    deadline: float | None = None,
) -> ConfirmedChartHistoryLoad:
    requested_window = chart_history_range_window(range_, interval, window=window)
    source_interval = canonical_bar_source_interval(interval)
    coverage_window = _elapsed_coverage_window(requested_window, source_interval)
    repair_demand = (
        _elapsed_coverage_window(repair_window, source_interval)
        if repair_window is not None
        else None
    )
    route = route_instrument(instrument, expected_source=provider)
    snapshot_generation = (
        load_generation
        if load_generation is not None
        else chart_bars_updated_generation(
            interval,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
    )
    require_chart_bars_generation(
        snapshot_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    loop = asyncio.get_running_loop()
    requested_timeout = max(float(timeout), 0.1)
    if deadline is None:
        deadline = loop.time() + requested_timeout + _LOAD_DEADLINE_GRACE_SECONDS

    async def load_db_bars() -> tuple[list[Bar], str]:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("CONFIRMED_CHART_HISTORY_DEADLINE_EXCEEDED")
        stage_timeout = min(requested_timeout, remaining)
        if executor is None:
            return await asyncio.wait_for(
                async_load_provider_bars(
                    route,
                    source_interval,
                    range_,
                    stage_timeout,
                    live_refresh=False,
                    store=store,
                    window=requested_window,
                ),
                timeout=remaining,
            )
        return await asyncio.wait_for(
            _run_chart_history_blocking(
                partial(
                    load_provider_bars,
                    route,
                    source_interval,
                    range_,
                    stage_timeout,
                    live_refresh=False,
                    store=store,
                    window=requested_window,
                ),
                executor=executor,
            ),
            timeout=remaining,
        )

    source_bars, warning = await load_db_bars()
    bars = derive_chart_bars(
        source_bars,
        interval,
        as_of=requested_window.ends_at,
    )
    require_chart_bars_generation(
        snapshot_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    plan = _repair_capability_plan(
        route=route,
        interval=source_interval,
        range_=range_,
        window=coverage_window,
    )

    intent = None
    terminal_outcome = None
    repair_state_error: Exception | None = None
    generation_current = (
        chart_bars_updated_generation(
            interval,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        == snapshot_generation
    )
    if plan.supported and generation_current:
        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("CONFIRMED_CHART_HISTORY_DEADLINE_EXCEEDED")
            operation = partial(
                _select_snapshot_repair_intent,
                route=route,
                interval=source_interval,
                bars=source_bars,
                plan=plan,
                store=store,
                canonical_generation=snapshot_generation,
                explicit_refresh=refresh_provider,
                explicit_demand=repair_demand,
            )
            intent = await asyncio.wait_for(
                _run_chart_history_blocking(operation, executor=executor),
                timeout=remaining,
            )
            latest_outcome = (
                latest_chart_history_repair_outcome(intent) if intent is not None else None
            )
            terminal_outcome = (
                history_repair_outcome_payload(latest_outcome)
                if latest_outcome is not None
                else None
            )
        except Exception as exc:
            repair_state_error = exc

    if not plan.supported:
        repair = _repair_payload(
            None,
            status="not_supported",
            error_code=plan.error_code or "PROVIDER_HISTORY_REPAIR_UNAVAILABLE",
        )
    elif not generation_current:
        repair = _repair_payload(
            None,
            status="stale_snapshot",
            error_code="PROVIDER_HISTORY_SNAPSHOT_STALE",
        )
    elif repair_state_error is not None:
        _LOGGER.warning(
            "history repair state failed provider=%s instrument_id=%s interval=%s range=%s: %s",
            route.provider,
            route.instrument_id,
            interval,
            range_,
            repair_state_error,
        )
        repair = _repair_payload(
            None,
            status="error",
            error_code="PROVIDER_HISTORY_REPAIR_STATE_FAILED",
        )
    else:
        try:
            admission = (
                schedule_chart_cached_gap_repair(
                    intent,
                    store_factory=lambda: store,
                    instrument=route.instrument,
                )
                if intent is not None
                else None
            )
            repair = _repair_payload(
                admission,
                status="not_needed" if intent is None else "not_admitted",
                terminal_outcome=terminal_outcome,
            )
        except Exception as exc:
            _LOGGER.warning(
                "history repair admission failed provider=%s instrument_id=%s interval=%s range=%s: %s",
                route.provider,
                route.instrument_id,
                interval,
                range_,
                exc,
            )
            repair = _repair_payload(
                None,
                status="error",
                error_code="PROVIDER_HISTORY_REPAIR_ADMISSION_FAILED",
            )

    require_chart_bars_generation(
        snapshot_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    return ConfirmedChartHistoryLoad(
        tuple(bars),
        warning,
        _history_coverage_payload(
            plan,
            bars,
            repair,
            route=route,
            interval=interval,
        ),
        snapshot_generation,
    )


async def _load_generation_consistent_chart_bars(
    provider: str,
    instrument: dict[str, Any],
    interval: str,
    range_: str,
    timeout: float,
    *,
    store_factory: Callable[[], Any],
    refresh_provider: bool,
    executor: Executor | None,
    window: HistoryRangeWindow,
    repair_window: HistoryRangeWindow | None,
    deadline: float,
) -> ConfirmedChartHistoryLoad:
    route = route_instrument(instrument, expected_source=provider)
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        generation = await wait_for_chart_bars_stable(
            interval,
            route.fingerprint,
            max(deadline - loop.time(), 0.0),
            instrument_id=route.instrument_id,
        )
        if loop.time() >= deadline:
            raise TimeoutError("CONFIRMED_CHART_HISTORY_DEADLINE_EXCEEDED")
        if attempt == 0:
            store = await _run_chart_history_blocking(store_factory, executor=executor)
        try:
            return await _load_confirmed_chart_bars(
                route.provider,
                route.instrument,
                interval,
                range_,
                timeout,
                store=store,
                refresh_provider=refresh_provider,
                executor=executor,
                window=window,
                repair_window=repair_window,
                load_generation=generation,
                deadline=deadline,
            )
        except ChartBarsGenerationChanged:
            if attempt > 0:
                raise
    raise AssertionError("unreachable confirmed chart history retry state")


def _finish_inflight_load(
    key: _ChartCacheKey,
    task: asyncio.Task[ConfirmedChartHistoryLoad],
    cache_generation: int,
    _initial_event_generation: int,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Own bounded task cleanup and exact-revision cache publication independently of waiters."""

    current = _inflight_tasks.get(key)
    try:
        result = task.result()
    except asyncio.CancelledError, Exception:
        result = None
    if current is None or current[0] is not task:
        return
    _inflight_tasks.pop(key, None)
    if result is None or _cache_generation.get(key, 0) != cache_generation:
        if key not in _result_cache:
            _cache_generation.pop(key, None)
        return
    (
        _provider,
        instrument_id,
        route_fingerprint,
        interval,
        _range,
        _refresh,
        _starts_at,
        _ends_at,
    ) = key
    result_generation = result.canonical_generation
    try:
        require_chart_bars_generation(
            result_generation,
            interval,
            route_fingerprint,
            instrument_id=instrument_id,
        )
    except ChartBarsGenerationChanged:
        if key not in _result_cache:
            _cache_generation.pop(key, None)
        return
    _result_cache[key] = _result_cache_entry(loop.time(), result_generation, result)
    _prune_result_cache(loop.time())


async def coalesced_load_confirmed_chart_bars(
    provider: str,
    instrument: dict[str, Any],
    interval: str,
    range_: str,
    timeout: float,
    store_factory: Callable[[], Any],
    *,
    refresh_provider: bool = False,
    executor: Executor | None = None,
    window: HistoryRangeWindow | None = None,
    repair_window: HistoryRangeWindow | None = None,
) -> ConfirmedChartHistoryLoad:
    """Return the DB snapshot and schedule exact-range repair without provider waiting."""

    if not _chart_history_accepting:
        raise RuntimeError("CHART_HISTORY_RUNTIME_STOPPING")

    requested_window = chart_history_range_window(range_, interval, window=window)
    requested_repair_window = (
        chart_history_range_window(range_, interval, window=repair_window)
        if repair_window is not None
        else None
    )
    route = route_instrument(instrument, expected_source=provider)
    loop = asyncio.get_running_loop()
    operation_timeout = max(float(timeout), 0.1)
    deadline = loop.time() + operation_timeout + _LOAD_DEADLINE_GRACE_SECONDS
    # Cache hits have the same stability contract as physical reads. Waiting
    # never holds the coalescing lock and never renews the request budget.
    event_generation = await wait_for_chart_bars_stable(
        interval,
        route.fingerprint,
        max(deadline - loop.time(), 0.0),
        instrument_id=route.instrument_id,
    )
    if not _chart_history_accepting:
        raise RuntimeError("CHART_HISTORY_RUNTIME_STOPPING")
    if requested_repair_window is not None:
        return await asyncio.wait_for(
            _load_generation_consistent_chart_bars(
                route.provider,
                route.instrument,
                interval,
                range_,
                timeout,
                store_factory=store_factory,
                refresh_provider=refresh_provider,
                executor=executor,
                window=requested_window,
                repair_window=requested_repair_window,
                deadline=deadline,
            ),
            timeout=(max(deadline - loop.time(), 0.0) + _OWNED_TASK_CANCELLATION_MARGIN_SECONDS),
        )
    key = confirmed_chart_bars_cache_key(
        route.provider,
        route.instrument,
        interval,
        range_,
        refresh_provider=refresh_provider,
        window=requested_window,
    )
    now = loop.time()
    _prune_result_cache(now)
    cached = _result_cache.get(key)
    if (
        cached is not None
        and cached[1] == event_generation
        and now - cached[0] <= _RESULT_CACHE_TTL_SECONDS
    ):
        try:
            require_chart_bars_generation(
                event_generation,
                interval,
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
        except ChartBarsGenerationChanged:
            pass
        else:
            return cached[4]

    async with _inflight_lock:
        if not _chart_history_accepting:
            raise RuntimeError("CHART_HISTORY_RUNTIME_STOPPING")
        now = loop.time()
        _prune_result_cache(now)
        cached = _result_cache.get(key)
        event_generation = chart_bars_updated_generation(
            interval,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        if (
            cached is not None
            and cached[1] == event_generation
            and now - cached[0] <= _RESULT_CACHE_TTL_SECONDS
        ):
            try:
                require_chart_bars_generation(
                    event_generation,
                    interval,
                    route.fingerprint,
                    instrument_id=route.instrument_id,
                )
            except ChartBarsGenerationChanged:
                pass
            else:
                return cached[4]
        existing = _inflight_tasks.get(key)
        if existing is not None:
            task, cache_generation, load_event_generation = existing
        else:
            load_event_generation = event_generation
            task = asyncio.create_task(
                asyncio.wait_for(
                    _load_generation_consistent_chart_bars(
                        route.provider,
                        route.instrument,
                        interval,
                        range_,
                        timeout,
                        store_factory=store_factory,
                        refresh_provider=refresh_provider,
                        executor=executor,
                        window=requested_window,
                        repair_window=None,
                        deadline=deadline,
                    ),
                    timeout=(
                        max(deadline - loop.time(), 0.0) + _OWNED_TASK_CANCELLATION_MARGIN_SECONDS
                    ),
                ),
                name=f"confirmed-chart-bars:{route.provider}:{interval}:{range_}",
            )
            cache_generation = _cache_generation.get(key, 0)
            _inflight_tasks[key] = (
                task,
                cache_generation,
                load_event_generation,
            )
            task.add_done_callback(
                lambda completed, cache_key=key, generation=cache_generation, canonical_generation=load_event_generation, owner_loop=loop: (
                    _finish_inflight_load(
                        cache_key,
                        completed,
                        generation,
                        canonical_generation,
                        owner_loop,
                    )
                )
            )
    return await asyncio.shield(task)


def _load_confirmed_chart_history_page(
    route: Any,
    interval: str,
    before_ts: datetime,
    limit: int,
    canonical_generation: int,
    *,
    store_factory: Callable[[], Any],
) -> ConfirmedChartHistoryPage:
    require_chart_bars_generation(
        canonical_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    with postgres_operation_timeouts(
        statement_timeout_ms=_HISTORY_PAGE_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=_HISTORY_PAGE_LOCK_TIMEOUT_MS,
    ):
        store = store_factory()
        page = read_provider_history_page(
            route,
            interval,
            before_ts=before_ts,
            limit=limit,
            store=store,
        )
    require_chart_bars_generation(
        canonical_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    return ConfirmedChartHistoryPage(
        bars=page.bars,
        next_before_ts=page.next_before_ts,
        has_more=page.has_more,
        canonical_generation=canonical_generation,
    )


async def load_confirmed_chart_history_page(
    instrument: dict[str, Any],
    interval: str,
    *,
    before_ts: datetime,
    limit: int,
    expected_canonical_generation: int,
    store_factory: Callable[[], Any],
    timeout: float = 2.0,
) -> ConfirmedChartHistoryPage:
    """Return one bounded, generation-fenced local history page."""

    if before_ts.tzinfo is None or before_ts.utcoffset() is None:
        raise ValueError("before_ts must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if (
        isinstance(expected_canonical_generation, bool)
        or not isinstance(expected_canonical_generation, int)
        or expected_canonical_generation < 0
    ):
        raise ValueError("expected canonical generation must be a non-negative integer")

    route = route_instrument(instrument)
    canonical_generation = require_chart_bars_generation(
        expected_canonical_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    return await asyncio.wait_for(
        run_physical_thread_call(
            _load_confirmed_chart_history_page,
            route,
            interval,
            before_ts.astimezone(UTC),
            limit,
            canonical_generation,
            store_factory=store_factory,
        ),
        timeout=max(float(timeout), 0.1),
    )
