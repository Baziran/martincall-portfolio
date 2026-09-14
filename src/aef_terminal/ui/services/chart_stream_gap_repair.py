from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.provider_contract import (
    HistoryContractResolution,
    HistoryRepairAdmission,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairPriority,
    HistoryRequestUnsupportedError,
)
from aef_terminal.data.history_repair import (
    HISTORY_REPAIR_ORCHESTRATOR,
    schedule_provider_history_repair,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.history_repair import select_history_repair_intent
from aef_terminal.data.provider_sessions import provider_session_bar_slot_coverage_between
from aef_terminal.runtime.timeframes import interval_seconds
from aef_terminal.runtime.chart_commits import commit_history_repair_result
from aef_terminal.data.history_coverage import (
    HistoryCoverageReceipt,
    current_history_coverage_contract,
    history_uncovered_ranges,
)


CHART_STREAM_GAP_REPAIR_TIMEOUT = 30.0
_SMALL_HISTORY_GAP_MAX_SPAN = timedelta(days=1)
_SMALL_HISTORY_GAP_MAX_SLOTS = 24
_RECENT_HISTORY_GAP_MAX_AGE = timedelta(days=8)
_SMALL_HISTORY_GAP_SCHEDULE_SLOT_LIMIT = _SMALL_HISTORY_GAP_MAX_SLOTS + 1


def latest_chart_history_repair_outcome(
    intent: HistoryRepairIntent,
) -> HistoryRepairOutcome | None:
    """Return the bounded terminal outcome for this exact repair generation."""

    return HISTORY_REPAIR_ORCHESTRATOR.latest_outcome(intent)


def history_repair_outcome_payload(
    outcome: HistoryRepairOutcome,
) -> dict[str, Any]:
    if not isinstance(outcome, HistoryRepairOutcome):
        raise TypeError("history repair outcome must be typed")
    intent = outcome.intent
    remaining = outcome.remaining_range
    return {
        "status": outcome.status.value,
        "code": outcome.error_code,
        "requested_from": intent.starts_at.isoformat(),
        "requested_to": intent.ends_at.isoformat(),
        "target_from": intent.target_starts_at.isoformat(),
        "target_to": intent.target_ends_at.isoformat(),
        "remaining_from": remaining[0].isoformat() if remaining is not None else None,
        "remaining_to": remaining[1].isoformat() if remaining is not None else None,
        "response_from": (
            outcome.response_starts_at.isoformat() if outcome.response_starts_at else None
        ),
        "response_to": (outcome.response_ends_at.isoformat() if outcome.response_ends_at else None),
        "provider": intent.provider,
        "instrument_id": intent.instrument_id,
        "route_fingerprint": intent.route_fingerprint,
        "timeframe": intent.timeframe,
        "canonical_generation": intent.canonical_generation,
        "provider_terminal": (outcome.terminal.value if outcome.terminal is not None else None),
        "written_bars": outcome.written_bars,
        "deleted_bars": outcome.deleted_bars,
        "coverage_inserted": outcome.coverage_inserted,
        "coverage_expanded": outcome.coverage_expanded,
        "result_generation": outcome.generation,
    }


def select_chart_history_repair(
    *,
    route: Any,
    interval: str,
    requested_from: datetime,
    requested_to: datetime,
    canonical_generation: int,
    priority: HistoryRepairPriority = HistoryRepairPriority.FOREGROUND,
    store: Any | None = None,
    classify_small_recent_gap: bool = False,
    dedupe_receipts: tuple[HistoryCoverageReceipt, ...] | None = None,
    now: datetime | None = None,
) -> HistoryRepairIntent | None:
    """Select one provider-bounded chunk for a concrete repair demand.

    A materialized provider schedule may prove that a small recent candidate contains
    no expected open bucket.  Unknown, incomplete, open, large, and old candidates go
    directly to provider history; this boundary never reconstructs a local calendar.
    """

    if not route.adapter.capabilities.gap_repair:
        return None
    try:
        request_identity = route.adapter.history_request_identity(route.instrument)
    except HistoryRequestUnsupportedError:
        return None
    max_request_span = route.adapter.history_request_max_span(
        route.instrument,
        interval,
    )
    starts_at = requested_from.astimezone(UTC)
    ends_at = requested_to.astimezone(UTC)
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    small_gap_max_span = min(
        _SMALL_HISTORY_GAP_MAX_SPAN,
        timedelta(seconds=_SMALL_HISTORY_GAP_MAX_SLOTS * interval_seconds(interval)),
    )
    if (
        classify_small_recent_gap
        and store is not None
        and ends_at - starts_at <= small_gap_max_span
        and current - ends_at <= _RECENT_HISTORY_GAP_MAX_AGE
    ):
        try:
            schedule = provider_session_bar_slot_coverage_between(
                starts_at,
                ends_at,
                interval,
                store=store,
                instrument=route.instrument,
                limit=_SMALL_HISTORY_GAP_SCHEDULE_SLOT_LIMIT,
            )
        except Exception:
            schedule = None
        if schedule is not None and (
            schedule.schedule_state == "verified"
            and not schedule.truncated
            and not schedule.expected_slots
        ):
            return None
    if request_identity.contract_resolution is HistoryContractResolution.EXACT and getattr(
        route.adapter.capabilities,
        "exact_history_snapshot_authority",
        False,
    ):
        contract = current_history_coverage_contract(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe=interval,
            request_mode=request_identity.request_mode,
            request_type=request_identity.request_type.value,
            provider_source=request_identity.provider_source,
            provider_contract_id=request_identity.provider_contract_id,
            provider_contract_type=request_identity.provider_contract_type,
            data_type=request_identity.data_type,
        )
        receipts = dedupe_receipts
        if receipts is None and store is not None:
            receipts = tuple(
                store.read_history_coverage(
                    contract,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            )
        if receipts is not None:
            if any(
                not isinstance(receipt, HistoryCoverageReceipt) or receipt.contract != contract
                for receipt in receipts
            ):
                raise TypeError("HISTORY_COVERAGE_RECEIPT_CONTRACT_MISMATCH")
            uncovered = history_uncovered_ranges(
                receipts,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            if not uncovered:
                return None
            starts_at, ends_at = uncovered[-1]
    return select_history_repair_intent(
        provider=route.provider,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        timeframe=interval,
        requested_from=starts_at,
        requested_to=ends_at,
        request_identity=request_identity,
        canonical_generation=canonical_generation,
        priority=priority,
        cooldown_seconds=0.0,
        max_request_span=max_request_span,
    )


def schedule_chart_cached_gap_repair(
    intent: HistoryRepairIntent,
    *,
    store_factory: Callable[[], Any],
    instrument: dict[str, Any],
    repair_timeout_seconds: float = CHART_STREAM_GAP_REPAIR_TIMEOUT,
    on_complete: Callable[[HistoryRepairOutcome], None] | None = None,
) -> HistoryRepairAdmission:
    """Admit an already-planned exact range to the bounded provider lane.

    This realtime-safe boundary performs no provider or storage I/O. Provider work
    begins asynchronously in the process-owned history-repair orchestrator.
    """

    if not isinstance(intent, HistoryRepairIntent):
        raise TypeError("history repair intent must be typed")
    route = route_instrument(instrument, expected_source=intent.provider)
    if route.instrument_id != intent.instrument_id or route.fingerprint != intent.route_fingerprint:
        raise ValueError("HISTORY_REPAIR_ROUTE_MISMATCH")
    return schedule_provider_history_repair(
        provider=route.provider,
        exact_snapshot_authority=route.adapter.capabilities.exact_history_snapshot_authority,
        adapter=route.adapter,
        intent=intent,
        timeout=max(float(repair_timeout_seconds), 0.1),
        store_factory=store_factory,
        instrument=route.instrument,
        commit=commit_history_repair_result,
        on_complete=on_complete,
    )
