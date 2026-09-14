from __future__ import annotations

import asyncio
import copy
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from math import isfinite
from threading import Lock
from time import monotonic
from typing import Any

from aef_terminal.data.instrument_identity import (
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import (
    HistoryContractResolution,
    HistoryRepairAdmission,
    HistoryRepairAdmissionStatus,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairOutcomeStatus,
    HistoryRepairPriority,
    HistoryRepairProvider,
    HistoryRequestAdmissionIdentity,
    ProviderHistoryFetchResult,
    ProviderHistoryTerminal,
)
from aef_terminal.runtime.chart_events import chart_bars_updated_generation
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.timeframes import interval_minutes
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
    HistoryCoverageReceipt,
    current_history_coverage_contract,
)


_LOGGER = logging.getLogger(__name__)

HistoryFetch = Callable[..., Awaitable[ProviderHistoryFetchResult]]
HistoryCommit = Callable[..., Awaitable[HistoryRepairOutcome]]
HistoryRepairCompletion = Callable[[HistoryRepairOutcome], None]
_HistoryRepairCursorKey = tuple[object, ...]
_HistoryRepairKey = tuple[object, ...]
_MAX_PENDING_REPAIRS_PER_PROVIDER = 32
_MAX_COMPLETION_CALLBACKS_PER_REPAIR = 32
_MAX_RETAINED_HISTORY_REPAIR_OUTCOMES = 256
_FAILURE_BACKOFF_SECONDS = 5 * 60.0


def _repair_key(intent: HistoryRepairIntent) -> _HistoryRepairKey:
    return (
        intent.provider,
        intent.instrument_id,
        intent.route_fingerprint,
        intent.timeframe,
        intent.starts_at,
        intent.ends_at,
        intent.target_starts_at,
        intent.target_ends_at,
        intent.request_identity,
        intent.canonical_generation,
    )


def _repair_cursor_key(intent: HistoryRepairIntent) -> _HistoryRepairCursorKey:
    return (
        intent.provider,
        intent.instrument_id,
        intent.route_fingerprint,
        intent.timeframe,
        intent.starts_at,
        intent.ends_at,
        intent.target_starts_at,
        intent.target_ends_at,
        intent.request_identity,
    )


def _continuation_request(
    request: _PendingHistoryRepair,
    outcome: HistoryRepairOutcome,
) -> _PendingHistoryRepair | None:
    """Advance one successful bounded chunk through its immutable target envelope."""

    intent = request.intent
    if intent.request_identity.contract_resolution is HistoryContractResolution.PROVIDER_RESPONSE:
        return None
    target_start = intent.target_starts_at
    progressed = bool(
        outcome.status is HistoryRepairOutcomeStatus.COMMITTED
        or outcome.written_bars
        or outcome.deleted_bars
        or outcome.coverage_expanded
    )
    if (
        outcome.status
        not in {
            HistoryRepairOutcomeStatus.COMMITTED,
            HistoryRepairOutcomeStatus.NO_CHANGE,
        }
        or not progressed
        or target_start is None
        or intent.starts_at <= target_start
    ):
        return None
    chunk_span = intent.ends_at - intent.starts_at
    next_end = intent.starts_at
    next_start = max(target_start, next_end - chunk_span)
    next_generation = (
        outcome.generation if outcome.generation is not None else intent.canonical_generation
    )
    next_intent = replace(
        intent,
        starts_at=next_start,
        ends_at=next_end,
        canonical_generation=next_generation,
    )
    return replace(
        request,
        task_key=_repair_key(next_intent),
        intent=next_intent,
    )


def _terminal_no_progress(outcome: HistoryRepairOutcome) -> bool:
    return (
        outcome.status
        in {
            HistoryRepairOutcomeStatus.NO_CHANGE,
            HistoryRepairOutcomeStatus.INCOMPLETE,
            HistoryRepairOutcomeStatus.MALFORMED,
        }
        and outcome.written_bars == 0
        and outcome.deleted_bars == 0
        and not outcome.coverage_expanded
    )


def _instrument_matches_intent(
    instrument: dict[str, Any],
    intent: HistoryRepairIntent,
) -> bool:
    try:
        qualified = require_provider_identity(
            instrument,
            provider=intent.provider,
        )
    except Exception:
        return False
    return (
        qualified.get("instrument_id") == intent.instrument_id
        and route_fingerprint(qualified) == intent.route_fingerprint
    )


async def _lookup_persisted_instrument(
    store: Any,
    intent: HistoryRepairIntent,
) -> dict[str, Any] | None:
    persisted = await run_physical_thread_call(store.lookup_instrument, intent.instrument_id)
    if not isinstance(persisted, dict) or not _instrument_matches_intent(
        persisted,
        intent,
    ):
        return None
    return require_provider_identity(persisted, provider=intent.provider)


def _current_generation(intent: HistoryRepairIntent) -> int:
    return chart_bars_updated_generation(
        intent.timeframe,
        intent.route_fingerprint,
        instrument_id=intent.instrument_id,
    )


def _coverage_receipts(
    result: ProviderHistoryFetchResult,
) -> tuple[HistoryCoverageReceipt, ...]:
    intent = result.intent
    identity = intent.request_identity
    if identity.contract_resolution is not HistoryContractResolution.EXACT:
        raise ValueError("HISTORY_REPAIR_EXACT_COVERAGE_IDENTITY_REQUIRED")
    if (
        identity.request_contract_version != HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION
        or identity.admission_contract_version != HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION
    ):
        raise ValueError("HISTORY_REPAIR_COVERAGE_CONTRACT_VERSION_MISMATCH")
    contract = current_history_coverage_contract(
        provider=intent.provider,
        instrument_id=intent.instrument_id,
        route_fingerprint=intent.route_fingerprint,
        timeframe=intent.timeframe,
        request_mode=identity.request_mode,
        request_type=identity.request_type.value,
        provider_source=identity.provider_source,
        provider_contract_id=identity.provider_contract_id,
        provider_contract_type=identity.provider_contract_type,
        data_type=identity.data_type,
    )
    return tuple(
        HistoryCoverageReceipt(
            contract=contract,
            covered_from=completion.starts_at,
            covered_to=completion.ends_at,
            response_count=completion.response_count,
            completed_at=completion.completed_at,
            provider_limit=completion.provider_limit,
        )
        for completion in result.completions
    )


async def execute_history_repair(
    *,
    provider: str,
    exact_snapshot_authority: bool,
    instrument: dict[str, Any],
    intent: HistoryRepairIntent,
    timeout: float,
    fetch: HistoryFetch,
    commit: HistoryCommit,
    store_factory: Callable[[], Any] | None,
) -> HistoryRepairOutcome:
    """Fetch one exact range and submit its immutable result to the commit owner."""

    if not isinstance(exact_snapshot_authority, bool):
        raise TypeError("HISTORY_REPAIR_SNAPSHOT_AUTHORITY_BOOL_REQUIRED")
    if provider != intent.provider or not _instrument_matches_intent(instrument, intent):
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STALE_ROUTE,
            intent=intent,
            error_code="HISTORY_REPAIR_ROUTE_MISMATCH",
        )
    if not callable(store_factory):
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STORAGE_FAILED,
            intent=intent,
            error_code="HISTORY_REPAIR_STORE_FACTORY_REQUIRED",
        )
    if not callable(commit):
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.COMMIT_FAILED,
            intent=intent,
            error_code="HISTORY_REPAIR_COMMIT_REQUIRED",
        )
    try:
        store = await run_physical_thread_call(store_factory)
        persisted = await _lookup_persisted_instrument(store, intent)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STORAGE_FAILED,
            intent=intent,
            error_code=str(exc) or exc.__class__.__name__,
        )
    if persisted is None:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STALE_ROUTE,
            intent=intent,
            error_code="HISTORY_REPAIR_PERSISTED_ROUTE_STALE",
        )
    generation = _current_generation(intent)
    if generation != intent.canonical_generation:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STALE_GENERATION,
            intent=intent,
            generation=generation,
        )
    try:
        result = await fetch(
            intent,
            timeout,
            instrument=copy.deepcopy(persisted),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.FETCH_FAILED,
            intent=intent,
            error_code=str(exc) or exc.__class__.__name__,
        )
    if not isinstance(result, ProviderHistoryFetchResult) or result.intent != intent:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.MALFORMED,
            intent=intent,
            error_code="HISTORY_REPAIR_TYPED_RESULT_MISMATCH",
        )
    if result.terminal is ProviderHistoryTerminal.MALFORMED:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.MALFORMED,
            intent=intent,
            terminal=result.terminal,
            error_code=result.error_code,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )
    try:
        persisted_after = await _lookup_persisted_instrument(store, intent)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STORAGE_FAILED,
            intent=intent,
            terminal=result.terminal,
            error_code=str(exc) or exc.__class__.__name__,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )
    if persisted_after is None:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STALE_ROUTE,
            intent=intent,
            terminal=result.terminal,
            error_code="HISTORY_REPAIR_PERSISTED_ROUTE_STALE",
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )
    generation = _current_generation(intent)
    if generation != intent.canonical_generation:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.STALE_GENERATION,
            intent=intent,
            terminal=result.terminal,
            generation=generation,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )
    coverage_receipts: tuple[HistoryCoverageReceipt, ...] = ()
    if result.terminal is ProviderHistoryTerminal.COMPLETE and exact_snapshot_authority:
        try:
            coverage_receipts = _coverage_receipts(result)
        except Exception as exc:
            return HistoryRepairOutcome(
                status=HistoryRepairOutcomeStatus.MALFORMED,
                intent=intent,
                terminal=result.terminal,
                error_code=str(exc) or exc.__class__.__name__,
                response_starts_at=result.response_starts_at,
                response_ends_at=result.response_ends_at,
            )
    elif (
        result.terminal is ProviderHistoryTerminal.ROLLING_COMPLETE
        and not result.authoritative_bars
    ):
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.NO_CHANGE,
            intent=intent,
            terminal=result.terminal,
            generation=generation,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )
    elif result.terminal is not ProviderHistoryTerminal.COMPLETE and not result.authoritative_bars:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.INCOMPLETE,
            intent=intent,
            terminal=result.terminal,
            error_code=result.error_code,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )

    try:
        outcome = await commit(
            instrument=copy.deepcopy(persisted_after),
            intent=intent,
            terminal=result.terminal,
            bars=result.authoritative_bars,
            coverage_receipts=coverage_receipts,
            store_factory=store_factory,
            error_code=result.error_code,
        )
        if not isinstance(outcome, HistoryRepairOutcome) or outcome.intent != intent:
            raise TypeError("HISTORY_REPAIR_COMMIT_OUTCOME_MISMATCH")
        return replace(
            outcome,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.COMMIT_FAILED,
            intent=intent,
            terminal=result.terminal,
            error_code=str(exc) or exc.__class__.__name__,
            response_starts_at=result.response_starts_at,
            response_ends_at=result.response_ends_at,
        )


@dataclass(frozen=True, slots=True)
class _PendingHistoryRepair:
    task_key: _HistoryRepairKey
    exact_snapshot_authority: bool
    instrument: dict[str, Any]
    intent: HistoryRepairIntent
    timeout: float
    fetch: HistoryFetch
    commit: HistoryCommit
    store_factory: Callable[[], Any] | None
    loop: asyncio.AbstractEventLoop


class HistoryRepairOrchestrator:
    """One bounded foreground-first exact-range lane per provider."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._tasks: dict[_HistoryRepairKey, asyncio.Task[None]] = {}
        self._next_allowed: dict[_HistoryRepairKey, float] = {}
        self._awaiting_generation: dict[_HistoryRepairCursorKey, int] = {}
        self._pending: dict[
            str,
            OrderedDict[_HistoryRepairKey, _PendingHistoryRepair],
        ] = {}
        self._callbacks: dict[
            _HistoryRepairKey,
            list[HistoryRepairCompletion],
        ] = {}
        self._outcomes: OrderedDict[
            _HistoryRepairKey,
            HistoryRepairOutcome,
        ] = OrderedDict()

    def _remember_outcome_locked(self, outcome: HistoryRepairOutcome) -> None:
        key = _repair_key(outcome.intent)
        self._outcomes.pop(key, None)
        self._outcomes[key] = outcome
        while len(self._outcomes) > _MAX_RETAINED_HISTORY_REPAIR_OUTCOMES:
            self._outcomes.popitem(last=False)

    def latest_outcome(
        self,
        intent: HistoryRepairIntent,
    ) -> HistoryRepairOutcome | None:
        if not isinstance(intent, HistoryRepairIntent):
            raise TypeError("history repair intent must be typed")
        key = _repair_key(intent)
        with self._lock:
            outcome = self._outcomes.get(key)
            if outcome is not None:
                self._outcomes.move_to_end(key)
            return outcome

    def _subscribe_locked(
        self,
        task_key: _HistoryRepairKey,
        callback: HistoryRepairCompletion | None,
    ) -> bool:
        if callback is None:
            return False
        callbacks = self._callbacks.setdefault(task_key, [])
        if len(callbacks) >= _MAX_COMPLETION_CALLBACKS_PER_REPAIR:
            return False
        callbacks.append(callback)
        return True

    @staticmethod
    def _prioritize_pending(
        pending: OrderedDict[_HistoryRepairKey, _PendingHistoryRepair],
    ) -> None:
        ordered = sorted(
            pending.items(),
            key=lambda item: int(item[1].intent.priority),
        )
        pending.clear()
        pending.update(ordered)

    def _start_locked(self, request: _PendingHistoryRepair) -> None:
        task = request.loop.create_task(
            self._run_request(request),
            name=(f"history-repair:{request.intent.provider}:{request.intent.timeframe}"),
        )
        self._tasks[request.task_key] = task

    def _pop_next_locked(self, provider: str) -> _PendingHistoryRepair | None:
        pending = self._pending.get(provider)
        if not pending:
            self._pending.pop(provider, None)
            return None
        _key, request = pending.popitem(last=False)
        if not pending:
            self._pending.pop(provider, None)
        return request

    async def _run_request(self, request: _PendingHistoryRepair) -> None:
        outcome: HistoryRepairOutcome
        cancelled = False
        continuation: _PendingHistoryRepair | None = None
        try:
            outcome = await execute_history_repair(
                provider=request.intent.provider,
                exact_snapshot_authority=request.exact_snapshot_authority,
                instrument=request.instrument,
                intent=request.intent,
                timeout=request.timeout,
                fetch=request.fetch,
                commit=request.commit,
                store_factory=request.store_factory,
            )
        except asyncio.CancelledError:
            cancelled = True
            outcome = HistoryRepairOutcome(
                status=HistoryRepairOutcomeStatus.CANCELLED,
                intent=request.intent,
            )
            raise
        except Exception as exc:
            outcome = HistoryRepairOutcome(
                status=HistoryRepairOutcomeStatus.FETCH_FAILED,
                intent=request.intent,
                error_code=str(exc) or exc.__class__.__name__,
            )
            _LOGGER.exception(
                "history_repair_failed provider=%s instrument_id=%s timeframe=%s",
                request.intent.provider,
                request.intent.instrument_id,
                request.intent.timeframe,
            )
        finally:
            if not cancelled:
                continuation = _continuation_request(request, outcome)
            callbacks: list[tuple[HistoryRepairCompletion, HistoryRepairOutcome]] = []
            with self._lock:
                self._remember_outcome_locked(outcome)
                if _terminal_no_progress(outcome):
                    self._awaiting_generation[_repair_cursor_key(request.intent)] = (
                        request.intent.canonical_generation
                    )
                cooldown = request.intent.cooldown_seconds
                if outcome.status in {
                    HistoryRepairOutcomeStatus.FETCH_FAILED,
                    HistoryRepairOutcomeStatus.STORAGE_FAILED,
                    HistoryRepairOutcomeStatus.COMMIT_FAILED,
                    HistoryRepairOutcomeStatus.COMMIT_QUEUE_FULL,
                    HistoryRepairOutcomeStatus.MALFORMED,
                }:
                    cooldown = max(cooldown, _FAILURE_BACKOFF_SECONDS)
                self._next_allowed[request.task_key] = monotonic() + cooldown
                if self._tasks.get(request.task_key) is asyncio.current_task():
                    self._tasks.pop(request.task_key, None)
                callbacks.extend(
                    (callback, outcome) for callback in self._callbacks.pop(request.task_key, ())
                )
                if cancelled:
                    pending = self._pending.pop(request.intent.provider, OrderedDict())
                    for pending_key, pending_request in pending.items():
                        cancelled_outcome = HistoryRepairOutcome(
                            status=HistoryRepairOutcomeStatus.CANCELLED,
                            intent=pending_request.intent,
                        )
                        self._remember_outcome_locked(cancelled_outcome)
                        callbacks.extend(
                            (callback, cancelled_outcome)
                            for callback in self._callbacks.pop(pending_key, ())
                        )
                else:
                    next_request = continuation or self._pop_next_locked(request.intent.provider)
                    if next_request is not None:
                        self._start_locked(next_request)
            for callback, completion in callbacks:
                try:
                    callback(completion)
                except Exception:
                    _LOGGER.exception(
                        "history_repair_completion_callback_failed provider=%s "
                        "instrument_id=%s timeframe=%s",
                        request.intent.provider,
                        request.intent.instrument_id,
                        request.intent.timeframe,
                    )

    def schedule(
        self,
        *,
        provider: str,
        exact_snapshot_authority: bool,
        instrument: dict[str, Any],
        intent: HistoryRepairIntent,
        timeout: float,
        fetch: HistoryFetch,
        commit: HistoryCommit,
        store_factory: Callable[[], Any] | None,
        on_complete: HistoryRepairCompletion | None = None,
    ) -> HistoryRepairAdmission:
        if not isinstance(exact_snapshot_authority, bool):
            raise TypeError("HISTORY_REPAIR_SNAPSHOT_AUTHORITY_BOOL_REQUIRED")
        if provider != intent.provider or not _instrument_matches_intent(instrument, intent):
            raise ValueError("HISTORY_REPAIR_ROUTE_MISMATCH")
        if not callable(fetch):
            raise TypeError("HISTORY_REPAIR_FETCH_REQUIRED")
        if not callable(commit):
            raise TypeError("HISTORY_REPAIR_COMMIT_REQUIRED")
        if not callable(store_factory):
            raise TypeError("HISTORY_REPAIR_STORE_FACTORY_REQUIRED")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise ValueError("HISTORY_REPAIR_TIMEOUT_INVALID")
        loop = asyncio.get_running_loop()
        task_key = _repair_key(intent)
        cursor_key = _repair_cursor_key(intent)
        request = _PendingHistoryRepair(
            task_key=task_key,
            exact_snapshot_authority=exact_snapshot_authority,
            instrument=copy.deepcopy(instrument),
            intent=intent,
            timeout=float(timeout),
            fetch=fetch,
            commit=commit,
            store_factory=store_factory,
            loop=loop,
        )
        now = monotonic()
        with self._lock:
            chart_scope = cursor_key[:4]
            for blocked_key, blocked_generation in tuple(self._awaiting_generation.items()):
                if (
                    blocked_key[:4] == chart_scope
                    and blocked_generation < intent.canonical_generation
                ):
                    self._awaiting_generation.pop(blocked_key, None)
            blocked_generation = self._awaiting_generation.get(cursor_key)
            if blocked_generation is not None and intent.canonical_generation <= blocked_generation:
                return HistoryRepairAdmission(
                    status=(HistoryRepairAdmissionStatus.AWAITING_GENERATION_ADVANCE),
                    intent=intent,
                )
            for existing_key, existing_task in tuple(self._tasks.items()):
                if existing_task.done():
                    self._tasks.pop(existing_key, None)
            for expired_key, deadline in tuple(self._next_allowed.items()):
                if deadline <= now and expired_key not in self._tasks:
                    self._next_allowed.pop(expired_key, None)
            current = self._tasks.get(task_key)
            if current is not None and not current.done():
                subscribed = self._subscribe_locked(task_key, on_complete)
                return HistoryRepairAdmission(
                    status=HistoryRepairAdmissionStatus.RUNNING,
                    intent=intent,
                    completion_subscribed=subscribed,
                )
            pending = self._pending.get(provider)
            if pending is not None and task_key in pending:
                existing = pending[task_key]
                if intent.priority < existing.intent.priority:
                    pending[task_key] = replace(
                        existing,
                        intent=intent,
                        timeout=min(existing.timeout, float(timeout)),
                        fetch=fetch,
                        commit=commit,
                        store_factory=store_factory,
                    )
                    self._prioritize_pending(pending)
                subscribed = self._subscribe_locked(task_key, on_complete)
                return HistoryRepairAdmission(
                    status=HistoryRepairAdmissionStatus.QUEUED,
                    intent=intent,
                    completion_subscribed=subscribed,
                )
            next_allowed = self._next_allowed.get(task_key, 0.0)
            if now < next_allowed:
                return HistoryRepairAdmission(
                    status=HistoryRepairAdmissionStatus.THROTTLED,
                    intent=intent,
                    retry_after_seconds=max(next_allowed - now, 0.0),
                )
            active_for_provider = any(key[0] == provider for key in self._tasks)
            if active_for_provider:
                pending = self._pending.setdefault(provider, OrderedDict())
                if len(pending) >= _MAX_PENDING_REPAIRS_PER_PROVIDER:
                    evicted_key = None
                    if intent.priority is HistoryRepairPriority.FOREGROUND:
                        evicted_key = next(
                            (
                                key
                                for key, candidate in reversed(pending.items())
                                if candidate.intent.priority is HistoryRepairPriority.BACKGROUND
                            ),
                            None,
                        )
                    if evicted_key is None:
                        return HistoryRepairAdmission(
                            status=HistoryRepairAdmissionStatus.QUEUE_FULL,
                            intent=intent,
                        )
                    evicted = pending.pop(evicted_key)
                    superseded = HistoryRepairOutcome(
                        status=HistoryRepairOutcomeStatus.SUPERSEDED,
                        intent=evicted.intent,
                    )
                    self._remember_outcome_locked(superseded)
                    for callback in self._callbacks.pop(evicted_key, ()):
                        loop.call_soon(callback, superseded)
                pending[task_key] = request
                self._prioritize_pending(pending)
                self._outcomes.pop(task_key, None)
                subscribed = self._subscribe_locked(task_key, on_complete)
                return HistoryRepairAdmission(
                    status=HistoryRepairAdmissionStatus.QUEUED,
                    intent=intent,
                    newly_admitted=True,
                    completion_subscribed=subscribed,
                )
            subscribed = self._subscribe_locked(task_key, on_complete)
            self._outcomes.pop(task_key, None)
            self._start_locked(request)
        return HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=intent,
            newly_admitted=True,
            completion_subscribed=subscribed,
        )


HISTORY_REPAIR_ORCHESTRATOR = HistoryRepairOrchestrator()


def schedule_provider_history_repair(
    *,
    provider: str,
    exact_snapshot_authority: bool,
    adapter: HistoryRepairProvider,
    instrument: dict[str, Any],
    intent: HistoryRepairIntent,
    timeout: float,
    commit: HistoryCommit,
    store_factory: Callable[[], Any] | None,
    on_complete: HistoryRepairCompletion | None = None,
) -> HistoryRepairAdmission:
    """Validate one adapter request and admit it to the process-owned lane."""

    qualified = require_provider_identity(instrument, provider=provider)
    if not isinstance(adapter, HistoryRepairProvider):
        raise TypeError(f"{provider.upper()} history repair contract is not implemented")
    request_identity = adapter.history_request_identity(qualified)
    if not isinstance(request_identity, HistoryRequestAdmissionIdentity):
        raise TypeError(f"{provider.upper()} history request identity must be typed")
    if intent.request_identity != request_identity:
        raise ValueError(f"{provider.upper()} history request identity changed")
    max_span = adapter.history_request_max_span(qualified, intent.timeframe)
    if (
        not isinstance(max_span, timedelta)
        or max_span <= timedelta(0)
        or max_span.microseconds
        or max_span < timedelta(minutes=max(interval_minutes(intent.timeframe), 1))
    ):
        raise ValueError(f"{provider.upper()} history request bound must be canonical")
    if intent.ends_at - intent.starts_at > max_span:
        raise ValueError(f"{provider.upper()} history request exceeds provider bound")
    return HISTORY_REPAIR_ORCHESTRATOR.schedule(
        provider=provider,
        exact_snapshot_authority=exact_snapshot_authority,
        instrument=qualified,
        intent=intent,
        timeout=timeout,
        fetch=adapter.async_fetch_history,
        commit=commit,
        store_factory=store_factory,
        on_complete=on_complete,
    )
