from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import aef_terminal.data.history_repair as history_repair_module
import aef_terminal.runtime.chart_commits as chart_commits_module
from aef_terminal.data.adapters.base import ProviderAdapterBase
from aef_terminal.data.history_repair import (
    HistoryRepairOrchestrator,
    execute_history_repair,
    schedule_provider_history_repair,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    HistoryContractResolution,
    HistoryRangeCompletion,
    HistoryRepairAdmissionStatus,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairOutcomeStatus,
    HistoryRepairPriority,
    HistoryRequestAdmissionIdentity,
    ProviderHistoryFetchResult,
    ProviderHistoryTerminal,
    ProviderCapabilities,
    ProviderDataPolicy,
    ProviderManifest,
    ProviderSessionScope,
)
from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarProvenance,
    BarState,
)
from aef_terminal.engine.history_repair import (
    confirmed_history_gap_ranges,
    select_history_repair_intent,
)
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
    CanonicalHistoryCommitReceipt,
)


_END = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _instrument(contract_id: str = "123") -> dict[str, Any]:
    return {
        "instrument_id": f"demo|contract|{contract_id}",
        "key": "DEMO",
        "instrument_key": "DEMO",
        "display": "DEMO",
        "provider": "demo",
        "provider_symbol": "DEMO_NATIVE",
        "provider_contract_id": contract_id,
        "asset_class": "stock",
        "contract_identity": {
            "provider": "demo",
            "provider_contract_id": contract_id,
            "asset_class": "stock",
        },
    }


def _identity(contract_id: str = "123") -> HistoryRequestAdmissionIdentity:
    return HistoryRequestAdmissionIdentity(
        request_contract_version=HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
        admission_contract_version=HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
        request_mode="get_candles",
        request_type=BarProviderRequest.HISTORICAL,
        provider_source="EXCHANGE",
        provider_contract_id=contract_id,
        provider_contract_type="DEMO_ID",
        data_type="TRADES",
    )


def _rolling_identity() -> HistoryRequestAdmissionIdentity:
    return HistoryRequestAdmissionIdentity(
        request_contract_version=1,
        admission_contract_version=1,
        request_mode="rolling_end_now",
        request_type=BarProviderRequest.HISTORICAL,
        provider_source="PROVIDER_ROLLING_END_NOW",
        provider_contract_id="",
        provider_contract_type="CONTFUT",
        data_type="TRADES",
        contract_resolution=HistoryContractResolution.PROVIDER_RESPONSE,
    )


def _intent(
    *,
    starts_at: datetime | None = None,
    ends_at: datetime = _END,
    generation: int = 0,
    priority: HistoryRepairPriority = HistoryRepairPriority.FOREGROUND,
    cooldown_seconds: float = 0.0,
) -> HistoryRepairIntent:
    return HistoryRepairIntent(
        provider="demo",
        instrument_id="demo|contract|123",
        route_fingerprint="demo|contract|123",
        timeframe="5m",
        starts_at=starts_at or ends_at - timedelta(minutes=10),
        ends_at=ends_at,
        request_identity=_identity(),
        canonical_generation=generation,
        priority=priority,
        cooldown_seconds=cooldown_seconds,
    )


def _bar(ts: datetime) -> Bar:
    return Bar(
        symbol="DEMO_NATIVE",
        ts=ts,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        timeframe="5m",
        source="demo:exchange",
        closed=True,
        state=BarState.CONFIRMED,
        provenance=BarProvenance(
            provider="demo",
            instrument_id="demo|contract|123",
            route_fingerprint="demo|contract|123",
            request_type=BarProviderRequest.HISTORICAL,
            provider_contract_id="123",
            provider_contract_type="DEMO_ID",
            data_type="TRADES",
        ),
    )


def _rolling_bar(ts: datetime) -> Bar:
    return replace(
        _bar(ts),
        provenance=replace(
            _bar(ts).provenance,
            provider_contract_id="456",
            provider_contract_type="CONTFUT",
        ),
    )


def _completion(
    starts_at: datetime,
    ends_at: datetime,
    *,
    response_count: int,
) -> HistoryRangeCompletion:
    return HistoryRangeCompletion(
        starts_at=starts_at,
        ends_at=ends_at,
        response_count=response_count,
        completed_at=_END + timedelta(days=1),
        provider_source="EXCHANGE",
        provider_limit=100,
    )


def _complete_result(
    intent: HistoryRepairIntent,
    bars: tuple[Bar, ...] = (),
) -> ProviderHistoryFetchResult:
    return ProviderHistoryFetchResult(
        intent=intent,
        terminal=ProviderHistoryTerminal.COMPLETE,
        authoritative_bars=bars,
        completions=(
            _completion(
                intent.starts_at,
                intent.ends_at,
                response_count=len(bars),
            ),
        ),
    )


def test_history_repair_contract_is_exact_and_has_no_slot_or_schedule_fields() -> None:
    intent = _intent()

    assert intent.priority is HistoryRepairPriority.FOREGROUND
    assert intent.request_identity.request_type is BarProviderRequest.HISTORICAL
    for legacy in (
        "reason",
        "missing_bars",
        "missing_slots",
        "schedule_state",
        "provider_history_request",
        "dedupe_token",
    ):
        assert not hasattr(intent, legacy)

    with pytest.raises(ValueError, match="timeframe-aligned"):
        replace(intent, starts_at=intent.starts_at + timedelta(seconds=1))
    with pytest.raises(ValueError, match="non-negative"):
        replace(intent, canonical_generation=-1)
    future = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    future -= timedelta(minutes=future.minute % 5)
    with pytest.raises(ValueError, match="fully elapsed"):
        replace(intent, ends_at=future + timedelta(days=1))
    with pytest.raises(ValueError, match="deleted_bars must be non-negative"):
        HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.NO_CHANGE,
            intent=intent,
            deleted_bars=-1,
        )


def test_provider_result_requires_exact_contiguous_unsaturated_completions() -> None:
    intent = _intent()
    split = intent.starts_at + timedelta(minutes=5)
    bar = _bar(intent.starts_at)
    result = ProviderHistoryFetchResult(
        intent=intent,
        terminal=ProviderHistoryTerminal.COMPLETE,
        authoritative_bars=(bar,),
        completions=(
            _completion(intent.starts_at, split, response_count=1),
            _completion(split, intent.ends_at, response_count=0),
        ),
    )

    assert result.authoritative_bars == (bar,)
    assert result.completions[1].response_count == 0
    with pytest.raises(ValueError, match="cardinality"):
        replace(
            result,
            completions=(_completion(intent.starts_at, intent.ends_at, response_count=0),),
        )
    with pytest.raises(ValueError, match="cannot carry bars"):
        replace(
            result,
            terminal=ProviderHistoryTerminal.MALFORMED,
            completions=(),
            error_code="BAD_RESPONSE",
        )


def test_adapter_boundary_rejects_guessed_identity_and_oversized_range() -> None:
    class ExactProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="demo",
            name="Demo",
            db_providers=("demo",),
            session_scope=ProviderSessionScope.CONTINUOUS,
            capabilities=ProviderCapabilities(
                gap_repair=True,
                exact_history_snapshot_authority=True,
            ),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args, **_kwargs):
            return [], ""

        def search_instruments(self, _query: str, limit: int = 20):
            _ = limit
            return []

        def history_request_identity(self, instrument):
            _ = instrument
            return _identity()

        def history_request_max_span(self, instrument, timeframe):
            _ = instrument, timeframe
            return timedelta(hours=1)

        async def async_fetch_history(self, intent, timeout, *, instrument):
            _ = timeout, instrument
            return _complete_result(intent)

    provider = ExactProvider()
    guessed = replace(
        _identity(),
        request_mode="guessed_mode",
    )
    with pytest.raises(ValueError, match="identity changed"):
        schedule_provider_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            adapter=provider,
            intent=replace(_intent(), request_identity=guessed),
            timeout=1.0,
            instrument=_instrument(),
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
    with pytest.raises(ValueError, match="exceeds provider bound"):
        schedule_provider_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            adapter=provider,
            intent=_intent(starts_at=_END - timedelta(hours=2)),
            timeout=1.0,
            instrument=_instrument(),
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )


def test_planner_selects_bounded_tail_chunk_and_retains_concrete_envelope() -> None:
    starts_at = _END - timedelta(hours=4)

    intent = select_history_repair_intent(
        provider="demo",
        instrument_id="demo|contract|123",
        route_fingerprint="demo|contract|123",
        timeframe="5m",
        requested_from=starts_at,
        requested_to=_END,
        request_identity=_identity(),
        canonical_generation=7,
        priority=HistoryRepairPriority.BACKGROUND,
        cooldown_seconds=30.0,
        max_request_span=timedelta(hours=1),
    )

    assert intent is not None
    assert intent.starts_at == _END - timedelta(hours=1)
    assert intent.ends_at == _END
    assert intent.target_starts_at == starts_at
    assert intent.target_ends_at == _END
    assert intent.canonical_generation == 7
    assert intent.priority is HistoryRepairPriority.BACKGROUND


def test_gap_detector_returns_prefix_internal_and_tail_absences() -> None:
    starts_at = _END - timedelta(minutes=30)
    bars = (
        _bar(starts_at + timedelta(minutes=5)),
        _bar(starts_at + timedelta(minutes=15)),
    )

    gaps = confirmed_history_gap_ranges(
        bars,
        "5m",
        requested_from=starts_at,
        requested_to=_END,
    )

    assert tuple((gap.starts_at, gap.ends_at) for gap in gaps) == (
        (starts_at, starts_at + timedelta(minutes=5)),
        (starts_at + timedelta(minutes=10), starts_at + timedelta(minutes=15)),
        (starts_at + timedelta(minutes=20), _END),
    )


def test_gap_detector_rejects_timestamp_lookalikes_without_bar_contract() -> None:
    starts_at = _END - timedelta(minutes=30)

    with pytest.raises(TypeError, match="Bar values"):
        confirmed_history_gap_ranges(
            (SimpleNamespace(ts=starts_at, closed=True),),  # type: ignore[arg-type]
            "5m",
            requested_from=starts_at,
            requested_to=_END,
        )


class _Store:
    def __init__(self, instruments: list[dict[str, Any] | None] | None = None) -> None:
        self.instruments = list(instruments or [_instrument(), _instrument()])
        self.lookups = 0

    def lookup_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        assert instrument_id == "demo|contract|123"
        value = self.instruments[min(self.lookups, len(self.instruments) - 1)]
        self.lookups += 1
        return value


class _ChartCommits:
    def __init__(
        self,
        *,
        status: chart_commits_module.HistoryCommitStatus,
        deleted_bars: int = 0,
    ) -> None:
        self.status = status
        self.deleted_bars = deleted_bars
        self.requests: list[chart_commits_module.HistoryCommitRequest] = []

    def submit_history_commit(self, request, store_factory):
        assert callable(store_factory)
        self.requests.append(request)
        future = asyncio.get_running_loop().create_future()
        receipt = None
        if self.status in {
            chart_commits_module.HistoryCommitStatus.COMMITTED,
            chart_commits_module.HistoryCommitStatus.NO_CHANGE,
        }:
            receipt = CanonicalHistoryCommitReceipt(
                bar_receipt=CanonicalBarCommitReceipt(
                    revision_sequence=1,
                    written=len(request.bars),
                    acknowledged=request.bars,
                    deleted=self.deleted_bars,
                ),
                coverage_inserted=len(request.coverage_receipts),
                coverage_expanded=bool(request.coverage_receipts),
            )
        future.set_result(
            chart_commits_module.HistoryCommitOutcome(
                status=self.status,
                receipt=receipt,
                generation=request.canonical_generation + int(bool(receipt and receipt.mutated)),
            )
        )
        return SimpleNamespace(completion=future)


def test_execute_history_repair_cancellation_settles_physical_route_lookup() -> None:
    intent = _intent()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Store:
        @staticmethod
        def lookup_instrument(_instrument_id: str) -> dict[str, Any]:
            started.set()
            assert release.wait(timeout=2)
            finished.set()
            return _instrument()

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("cancelled repair must not start provider fetch")

    async def unexpected_commit(**_kwargs):
        raise AssertionError("cancelled repair must not submit a commit")

    async def scenario() -> None:
        task = asyncio.create_task(
            execute_history_repair(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=intent,
                timeout=3.0,
                fetch=unexpected_fetch,
                commit=unexpected_commit,
                store_factory=Store,
            )
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() is True

    asyncio.run(scenario())


def test_execute_complete_empty_rechecks_route_and_submits_chunk_coverage(
    monkeypatch,
) -> None:
    intent = _intent()
    store = _Store()
    commits = _ChartCommits(
        status=chart_commits_module.HistoryCommitStatus.COMMITTED,
        deleted_bars=2,
    )
    monkeypatch.setattr(chart_commits_module, "chart_commits", commits)
    monkeypatch.setattr(chart_commits_module, "provider_key_for_source", lambda value: value)
    monkeypatch.setattr(history_repair_module, "_current_generation", lambda _intent: 0)

    async def fetch(request, timeout, *, instrument):
        assert request is intent
        assert timeout == 3.0
        assert instrument == _instrument()
        return _complete_result(intent)

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: store,
        )
    )

    assert outcome.status is HistoryRepairOutcomeStatus.COMMITTED
    assert outcome.deleted_bars == 2
    assert outcome.coverage_inserted == 1
    assert outcome.coverage_expanded is True
    assert store.lookups == 2
    assert commits.requests[0].bars == ()
    assert len(commits.requests[0].coverage_receipts) == 1


@pytest.mark.parametrize("rolling", [False, True])
def test_execute_incomplete_commits_confirmed_bars_without_coverage(monkeypatch, rolling) -> None:
    intent = _intent()
    if rolling:
        intent = replace(intent, request_identity=_rolling_identity())
    store = _Store()
    commits = _ChartCommits(status=chart_commits_module.HistoryCommitStatus.COMMITTED)
    monkeypatch.setattr(chart_commits_module, "chart_commits", commits)
    monkeypatch.setattr(chart_commits_module, "provider_key_for_source", lambda value: value)
    monkeypatch.setattr(history_repair_module, "_current_generation", lambda _intent: 0)
    result = ProviderHistoryFetchResult(
        intent=intent,
        terminal=(
            ProviderHistoryTerminal.ROLLING_PARTIAL
            if rolling
            else ProviderHistoryTerminal.INCOMPLETE
        ),
        authoritative_bars=((_rolling_bar if rolling else _bar)(intent.starts_at),),
        error_code="FORMING_TAIL",
        resolved_provider_contract_id="456" if rolling else None,
        response_starts_at=intent.starts_at if rolling else None,
        response_ends_at=intent.ends_at if rolling else None,
    )

    async def fetch(*_args, **_kwargs):
        return result

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: store,
        )
    )

    assert outcome.status is HistoryRepairOutcomeStatus.INCOMPLETE
    assert outcome.written_bars == 1
    assert commits.requests[0].coverage_receipts == ()
    assert outcome.remaining_range == (intent.starts_at, intent.ends_at)
    assert outcome.error_code == "FORMING_TAIL"


def test_execute_rolling_history_commits_bars_without_receipts_and_empty_is_finite(
    monkeypatch,
) -> None:
    intent = replace(_intent(), request_identity=_rolling_identity())
    store = _Store()
    commits = _ChartCommits(status=chart_commits_module.HistoryCommitStatus.COMMITTED)
    monkeypatch.setattr(chart_commits_module, "chart_commits", commits)
    monkeypatch.setattr(chart_commits_module, "provider_key_for_source", lambda value: value)
    monkeypatch.setattr(history_repair_module, "_current_generation", lambda _intent: 0)
    result = ProviderHistoryFetchResult(
        intent=intent,
        terminal=ProviderHistoryTerminal.ROLLING_COMPLETE,
        authoritative_bars=(_rolling_bar(intent.starts_at),),
        resolved_provider_contract_id="456",
        response_starts_at=intent.starts_at,
        response_ends_at=intent.ends_at,
    )

    async def fetch(*_args, **_kwargs):
        return result

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: store,
        )
    )

    assert outcome.status is HistoryRepairOutcomeStatus.COMMITTED
    assert outcome.written_bars == 1
    assert commits.requests[0].coverage_receipts == ()
    assert outcome.response_starts_at == result.response_starts_at
    assert outcome.response_ends_at == result.response_ends_at
    assert outcome.remaining_range is None

    empty_result = replace(result, authoritative_bars=())

    async def fetch_empty(*_args, **_kwargs):
        return empty_result

    empty_outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch_empty,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: _Store(),
        )
    )

    assert empty_outcome.status is HistoryRepairOutcomeStatus.NO_CHANGE
    assert empty_outcome.terminal is ProviderHistoryTerminal.ROLLING_COMPLETE
    assert empty_outcome.remaining_range is None
    assert len(commits.requests) == 1


def test_rolling_partial_without_bars_stops_without_commit(monkeypatch) -> None:
    intent = replace(_intent(), request_identity=_rolling_identity())
    commits = _ChartCommits(status=chart_commits_module.HistoryCommitStatus.COMMITTED)
    monkeypatch.setattr(chart_commits_module, "chart_commits", commits)
    monkeypatch.setattr(history_repair_module, "_current_generation", lambda _intent: 0)
    result = ProviderHistoryFetchResult(
        intent=intent,
        terminal=ProviderHistoryTerminal.ROLLING_PARTIAL,
        authoritative_bars=(),
        resolved_provider_contract_id="456",
        error_code="PROVIDER_TARGET_NOT_CONTAINED",
        response_starts_at=intent.ends_at,
        response_ends_at=intent.ends_at + timedelta(hours=1),
    )

    async def fetch(*_args, **_kwargs):
        return result

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: _Store(),
        )
    )

    assert outcome.status is HistoryRepairOutcomeStatus.INCOMPLETE
    assert outcome.error_code == "PROVIDER_TARGET_NOT_CONTAINED"
    assert commits.requests == []
    assert outcome.remaining_range == (intent.starts_at, intent.ends_at)
    assert outcome.response_starts_at == result.response_starts_at
    assert outcome.response_ends_at == result.response_ends_at


def test_complete_fetch_without_snapshot_authority_stores_bars_without_coverage(
    monkeypatch,
) -> None:
    intent = _intent()
    store = _Store()
    commits = _ChartCommits(status=chart_commits_module.HistoryCommitStatus.COMMITTED)
    monkeypatch.setattr(chart_commits_module, "chart_commits", commits)
    monkeypatch.setattr(
        chart_commits_module,
        "provider_key_for_source",
        lambda value: value,
    )
    monkeypatch.setattr(history_repair_module, "_current_generation", lambda _intent: 0)
    result = _complete_result(intent, (_bar(intent.starts_at),))

    async def fetch(*_args, **_kwargs):
        return result

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=False,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: store,
        )
    )

    assert outcome.status is HistoryRepairOutcomeStatus.COMMITTED
    assert outcome.written_bars == 1
    assert outcome.coverage_inserted == 0
    assert outcome.coverage_expanded is False
    assert commits.requests[0].bars == result.authoritative_bars
    assert commits.requests[0].coverage_receipts == ()


def test_execute_malformed_or_stale_provider_result_never_reaches_commit(monkeypatch) -> None:
    intent = _intent()
    store = _Store()
    commits = _ChartCommits(status=chart_commits_module.HistoryCommitStatus.COMMITTED)
    monkeypatch.setattr(chart_commits_module, "chart_commits", commits)
    monkeypatch.setattr(chart_commits_module, "provider_key_for_source", lambda value: value)
    monkeypatch.setattr(history_repair_module, "_current_generation", lambda _intent: 0)
    malformed = ProviderHistoryFetchResult(
        intent=intent,
        terminal=ProviderHistoryTerminal.MALFORMED,
        authoritative_bars=(),
        error_code="BAD_RESPONSE",
    )

    async def fetch(*_args, **_kwargs):
        return malformed

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=fetch,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: store,
        )
    )
    assert outcome.status is HistoryRepairOutcomeStatus.MALFORMED
    assert commits.requests == []

    stale_store = _Store([_instrument(), None])

    async def complete(*_args, **_kwargs):
        return _complete_result(intent)

    outcome = asyncio.run(
        execute_history_repair(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=3.0,
            fetch=complete,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: stale_store,
        )
    )
    assert outcome.status is HistoryRepairOutcomeStatus.STALE_ROUTE
    assert commits.requests == []


def test_orchestrator_prioritizes_and_promotes_foreground_duplicate(monkeypatch) -> None:
    orchestrator = HistoryRepairOrchestrator()
    started: list[HistoryRepairIntent] = []
    gates: dict[datetime, asyncio.Event] = {}

    async def fake_execute(**kwargs):
        intent = kwargs["intent"]
        started.append(intent)
        await gates.setdefault(intent.starts_at, asyncio.Event()).wait()
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.NO_CHANGE,
            intent=intent,
        )

    monkeypatch.setattr(history_repair_module, "execute_history_repair", fake_execute)

    async def scenario() -> None:
        first = _intent(starts_at=_END - timedelta(minutes=30))
        background = _intent(
            starts_at=_END - timedelta(minutes=20),
            priority=HistoryRepairPriority.BACKGROUND,
        )
        foreground = _intent(starts_at=_END - timedelta(minutes=10))
        assert (
            orchestrator.schedule(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=first,
                timeout=1.0,
                fetch=lambda *_args, **_kwargs: None,
                commit=chart_commits_module.commit_history_repair_result,
                store_factory=lambda: object(),
            ).status
            is HistoryRepairAdmissionStatus.SCHEDULED
        )
        await asyncio.sleep(0)
        assert (
            orchestrator.schedule(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=background,
                timeout=2.0,
                fetch=lambda *_args, **_kwargs: None,
                commit=chart_commits_module.commit_history_repair_result,
                store_factory=lambda: object(),
            ).status
            is HistoryRepairAdmissionStatus.QUEUED
        )
        promoted = replace(background, priority=HistoryRepairPriority.FOREGROUND)
        assert (
            orchestrator.schedule(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=promoted,
                timeout=1.0,
                fetch=lambda *_args, **_kwargs: None,
                commit=chart_commits_module.commit_history_repair_result,
                store_factory=lambda: object(),
            ).status
            is HistoryRepairAdmissionStatus.QUEUED
        )
        orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=foreground,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        gates[first.starts_at].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert started[1].starts_at == promoted.starts_at
        assert started[1].priority is HistoryRepairPriority.FOREGROUND
        gates[promoted.starts_at].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert started[2].starts_at == foreground.starts_at
        gates[foreground.starts_at].set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_orchestrator_continues_after_empty_mirror_receipt_expands_coverage(
    monkeypatch,
) -> None:
    orchestrator = HistoryRepairOrchestrator()
    started: list[HistoryRepairIntent] = []
    intent = select_history_repair_intent(
        provider="demo",
        instrument_id="demo|contract|123",
        route_fingerprint="demo|contract|123",
        timeframe="5m",
        requested_from=_END - timedelta(hours=3),
        requested_to=_END,
        request_identity=_identity(),
        canonical_generation=4,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
        max_request_span=timedelta(hours=1),
    )
    assert intent is not None

    async def fake_execute(**kwargs):
        chunk = kwargs["intent"]
        started.append(chunk)
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.COMMITTED,
            intent=chunk,
            written_bars=0 if len(started) == 1 else 1,
            coverage_expanded=len(started) == 1,
            generation=chunk.canonical_generation,
        )

    monkeypatch.setattr(history_repair_module, "execute_history_repair", fake_execute)

    async def scenario() -> None:
        admission = orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        assert admission.status is HistoryRepairAdmissionStatus.SCHEDULED
        for _ in range(8):
            if len(started) == 3:
                break
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert [(chunk.starts_at, chunk.ends_at) for chunk in started] == [
        (_END - timedelta(hours=1), _END),
        (_END - timedelta(hours=2), _END - timedelta(hours=1)),
        (_END - timedelta(hours=3), _END - timedelta(hours=2)),
    ]
    assert all(chunk.target_starts_at == _END - timedelta(hours=3) for chunk in started)
    assert all(chunk.target_ends_at == _END for chunk in started)


def test_orchestrator_does_not_apply_exact_chunk_continuation_to_rolling_response(
    monkeypatch,
) -> None:
    orchestrator = HistoryRepairOrchestrator()
    started: list[HistoryRepairIntent] = []
    intent = select_history_repair_intent(
        provider="demo",
        instrument_id="demo|contract|123",
        route_fingerprint="demo|contract|123",
        timeframe="5m",
        requested_from=_END - timedelta(hours=3),
        requested_to=_END,
        request_identity=_rolling_identity(),
        canonical_generation=4,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
        max_request_span=timedelta(hours=1),
    )
    assert intent is not None

    async def fake_execute(**kwargs):
        chunk = kwargs["intent"]
        started.append(chunk)
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.COMMITTED,
            intent=chunk,
            terminal=ProviderHistoryTerminal.ROLLING_PARTIAL,
            written_bars=1,
            generation=chunk.canonical_generation + 1,
        )

    monkeypatch.setattr(history_repair_module, "execute_history_repair", fake_execute)

    async def scenario() -> None:
        admission = orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        assert admission.status is HistoryRepairAdmissionStatus.SCHEDULED
        for _ in range(4):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(started) == 1
    assert started[0].starts_at == _END - timedelta(hours=1)
    assert started[0].target_starts_at == _END - timedelta(hours=3)


def test_orchestrator_stops_large_envelope_after_exact_replay_without_progress(
    monkeypatch,
) -> None:
    orchestrator = HistoryRepairOrchestrator()
    started: list[HistoryRepairIntent] = []
    intent = select_history_repair_intent(
        provider="demo",
        instrument_id="demo|contract|123",
        route_fingerprint="demo|contract|123",
        timeframe="5m",
        requested_from=_END - timedelta(hours=2),
        requested_to=_END,
        request_identity=_identity(),
        canonical_generation=4,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
        max_request_span=timedelta(hours=1),
    )
    assert intent is not None

    async def fake_execute(**kwargs):
        chunk = kwargs["intent"]
        started.append(chunk)
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.NO_CHANGE,
            intent=chunk,
            generation=chunk.canonical_generation,
        )

    monkeypatch.setattr(history_repair_module, "execute_history_repair", fake_execute)

    async def scenario() -> None:
        admission = orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        assert admission.status is HistoryRepairAdmissionStatus.SCHEDULED
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert started == [intent]


@pytest.mark.parametrize(
    "outcome_status",
    (
        HistoryRepairOutcomeStatus.NO_CHANGE,
        HistoryRepairOutcomeStatus.INCOMPLETE,
        HistoryRepairOutcomeStatus.MALFORMED,
    ),
)
def test_orchestrator_terminal_no_progress_waits_for_generation_advance(
    monkeypatch,
    outcome_status,
) -> None:
    orchestrator = HistoryRepairOrchestrator()
    intent = _intent(cooldown_seconds=60.0)

    async def fake_execute(**kwargs):
        return HistoryRepairOutcome(
            status=outcome_status,
            intent=kwargs["intent"],
        )

    monkeypatch.setattr(history_repair_module, "execute_history_repair", fake_execute)

    async def scenario() -> None:
        first = orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        assert first.status is HistoryRepairAdmissionStatus.SCHEDULED
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        replay = orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=intent,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        assert replay.status is HistoryRepairAdmissionStatus.AWAITING_GENERATION_ADVANCE
        assert replay.retry_after_seconds == 0
        assert orchestrator.latest_outcome(intent).status is outcome_status

        advanced_intent = replace(intent, canonical_generation=1)
        assert orchestrator.latest_outcome(advanced_intent) is None
        advanced = orchestrator.schedule(
            provider="demo",
            exact_snapshot_authority=True,
            instrument=_instrument(),
            intent=advanced_intent,
            timeout=1.0,
            fetch=lambda *_args, **_kwargs: None,
            commit=chart_commits_module.commit_history_repair_result,
            store_factory=lambda: object(),
        )
        assert advanced.status is HistoryRepairAdmissionStatus.SCHEDULED
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_orchestrator_operational_failure_retries_after_bounded_cooldown(
    monkeypatch,
) -> None:
    clock = [100.0]
    orchestrator = HistoryRepairOrchestrator()
    intent = _intent(cooldown_seconds=0.0)

    async def fake_execute(**kwargs):
        return HistoryRepairOutcome(
            status=HistoryRepairOutcomeStatus.FETCH_FAILED,
            intent=kwargs["intent"],
            error_code="FETCH_TIMEOUT",
        )

    monkeypatch.setattr(history_repair_module, "execute_history_repair", fake_execute)
    monkeypatch.setattr(history_repair_module, "monotonic", lambda: clock[0])

    async def scenario() -> None:
        assert (
            orchestrator.schedule(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=intent,
                timeout=1.0,
                fetch=lambda *_args, **_kwargs: None,
                commit=chart_commits_module.commit_history_repair_result,
                store_factory=lambda: object(),
            ).status
            is HistoryRepairAdmissionStatus.SCHEDULED
        )
        assert orchestrator.latest_outcome(intent) is None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert (
            orchestrator.schedule(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=intent,
                timeout=1.0,
                fetch=lambda *_args, **_kwargs: None,
                commit=chart_commits_module.commit_history_repair_result,
                store_factory=lambda: object(),
            ).status
            is HistoryRepairAdmissionStatus.THROTTLED
        )
        failed = orchestrator.latest_outcome(intent)
        assert failed is not None
        assert failed.status is HistoryRepairOutcomeStatus.FETCH_FAILED
        assert failed.error_code == "FETCH_TIMEOUT"
        clock[0] += 5 * 60 + 1
        assert (
            orchestrator.schedule(
                provider="demo",
                exact_snapshot_authority=True,
                instrument=_instrument(),
                intent=intent,
                timeout=1.0,
                fetch=lambda *_args, **_kwargs: None,
                commit=chart_commits_module.commit_history_repair_result,
                store_factory=lambda: object(),
            ).status
            is HistoryRepairAdmissionStatus.SCHEDULED
        )
        assert orchestrator.latest_outcome(intent) is None
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_orchestrator_retains_only_bounded_exact_generation_outcomes() -> None:
    orchestrator = HistoryRepairOrchestrator()
    first = _intent(generation=0)
    last = first
    with orchestrator._lock:
        for generation in range(history_repair_module._MAX_RETAINED_HISTORY_REPAIR_OUTCOMES + 1):
            last = replace(first, canonical_generation=generation)
            orchestrator._remember_outcome_locked(
                HistoryRepairOutcome(
                    status=HistoryRepairOutcomeStatus.NO_CHANGE,
                    intent=last,
                )
            )

    assert orchestrator.latest_outcome(first) is None
    assert orchestrator.latest_outcome(last) is not None
