from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data.provider_contract import (
    HistoryContractResolution,
    HistoryRepairAdmission,
    HistoryRepairAdmissionStatus,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairOutcomeStatus,
    HistoryRepairPriority,
    HistoryRequestAdmissionIdentity,
    HistoryRequestUnsupportedError,
    ProviderHistoryTerminal,
)
from aef_terminal.data.providers import route_instrument as qualify_route
from aef_terminal.domain import BarProviderRequest
from aef_terminal.data.history_coverage import (
    HistoryCoverageReceipt,
    current_history_coverage_contract,
)
from aef_terminal.ui.services import chart_stream_gap_repair
from tests.provider_payloads import coinbase_btc_payload


START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 2, tzinfo=UTC)


def _identity() -> HistoryRequestAdmissionIdentity:
    return HistoryRequestAdmissionIdentity(
        request_contract_version=1,
        admission_contract_version=1,
        request_mode="get_candles_exchange",
        request_type=BarProviderRequest.HISTORICAL,
        provider_source="EXCHANGE",
        provider_contract_id="uid-1",
        provider_contract_type="INSTRUMENT_UID",
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


def _route(adapter: Any, *, fingerprint: str = "route-1") -> SimpleNamespace:
    instrument = {"provider": "tinvest", "instrument_id": "tinvest:uid-1"}
    return SimpleNamespace(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        fingerprint=fingerprint,
        instrument=instrument,
        adapter=adapter,
    )


@pytest.mark.parametrize(
    ("status", "terminal", "settled"),
    [
        (HistoryRepairOutcomeStatus.COMMITTED, ProviderHistoryTerminal.COMPLETE, True),
        (HistoryRepairOutcomeStatus.NO_CHANGE, ProviderHistoryTerminal.COMPLETE, True),
        (HistoryRepairOutcomeStatus.COMMITTED, ProviderHistoryTerminal.ROLLING_COMPLETE, True),
        (HistoryRepairOutcomeStatus.INCOMPLETE, ProviderHistoryTerminal.ROLLING_PARTIAL, False),
        (HistoryRepairOutcomeStatus.INCOMPLETE, ProviderHistoryTerminal.INCOMPLETE, False),
        (HistoryRepairOutcomeStatus.COMMIT_FAILED, ProviderHistoryTerminal.COMPLETE, False),
        (HistoryRepairOutcomeStatus.FETCH_FAILED, None, False),
    ],
)
def test_terminal_progress_preserves_unsettled_chunk_and_response_bounds(status, terminal, settled):
    chunk_start = START + timedelta(hours=12)
    intent = HistoryRepairIntent(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        route_fingerprint="route-1",
        timeframe="5m",
        starts_at=chunk_start,
        ends_at=END,
        target_starts_at=START,
        target_ends_at=END,
        canonical_generation=7,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0,
        request_identity=_identity(),
    )
    outcome = HistoryRepairOutcome(
        status=status,
        intent=intent,
        terminal=terminal,
        written_bars=1,
        response_starts_at=chunk_start,
        response_ends_at=END,
    )
    payload = chart_stream_gap_repair.history_repair_outcome_payload(outcome)
    assert payload["remaining_from"] == START.isoformat()
    assert payload["remaining_to"] == (chunk_start if settled else END).isoformat()
    assert payload["response_from"] == chunk_start.isoformat()
    assert payload["response_to"] == END.isoformat()
    final = replace(outcome, intent=replace(intent, starts_at=START))
    final_payload = chart_stream_gap_repair.history_repair_outcome_payload(final)
    assert final_payload["remaining_from"] == (None if settled else START.isoformat())
    assert final_payload["remaining_to"] == (None if settled else END.isoformat())


def test_exact_selector_uses_provider_bound_and_retains_full_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(gap_repair=True),
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(hours=2),
    )
    route = _route(adapter)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )

    intent = chart_stream_gap_repair.select_chart_history_repair(
        route=route,
        interval="5m",
        requested_from=START,
        requested_to=END,
        canonical_generation=7,
    )

    assert intent is not None
    assert intent.starts_at == END - timedelta(hours=2)
    assert intent.ends_at == END
    assert intent.canonical_generation == 7
    assert intent.target_starts_at == START
    assert intent.target_ends_at == END
    assert intent.request_identity == _identity()
    assert intent.priority is HistoryRepairPriority.FOREGROUND


def test_exact_selector_does_not_consult_a_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(gap_repair=True),
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: pytest.fail("schedule must be opt-in"),
    )

    intent = chart_stream_gap_repair.select_chart_history_repair(
        route=route,
        interval="5m",
        requested_from=START,
        requested_to=END,
        canonical_generation=0,
    )

    assert intent is not None
    assert intent.starts_at == START
    assert intent.ends_at == END


@pytest.mark.parametrize(
    ("schedule_state", "expected_slots", "expected_intent"),
    (
        ("verified", (), False),
        ("verified", (START,), True),
        ("unknown", (), True),
    ),
)
def test_small_recent_gap_uses_only_materialized_provider_schedule_to_classify(
    monkeypatch: pytest.MonkeyPatch,
    schedule_state: str,
    expected_slots: tuple[datetime, ...],
    expected_intent: bool,
) -> None:
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(gap_repair=True),
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: SimpleNamespace(
            schedule_state=schedule_state,
            expected_slots=expected_slots,
            truncated=False,
        ),
    )

    intent = chart_stream_gap_repair.select_chart_history_repair(
        route=route,
        interval="5m",
        requested_from=END - timedelta(hours=1),
        requested_to=END,
        canonical_generation=0,
        store=object(),
        classify_small_recent_gap=True,
        now=END + timedelta(minutes=5),
    )

    assert (intent is not None) is expected_intent


def test_coinbase_empty_mirror_receipt_suppresses_same_bounded_candidate() -> None:
    route = qualify_route(coinbase_btc_payload())
    identity = route.adapter.history_request_identity(route.instrument)
    contract = current_history_coverage_contract(
        provider=route.provider,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        timeframe="5m",
        request_mode=identity.request_mode,
        request_type=identity.request_type.value,
        provider_source=identity.provider_source,
        provider_contract_id=identity.provider_contract_id,
        provider_contract_type=identity.provider_contract_type,
        data_type=identity.data_type,
    )
    receipt = HistoryCoverageReceipt(
        contract=contract,
        covered_from=START,
        covered_to=END,
        response_count=0,
        completed_at=END + timedelta(seconds=1),
        provider_limit=300,
    )

    class Store:
        receipts: tuple[HistoryCoverageReceipt, ...] = ()

        def __init__(self) -> None:
            self.reads: list[tuple[datetime, datetime]] = []

        def read_history_coverage(self, actual_contract, *, starts_at, ends_at):
            assert actual_contract == contract
            self.reads.append((starts_at, ends_at))
            return self.receipts

    store = Store()
    first = chart_stream_gap_repair.select_chart_history_repair(
        route=route,
        interval="5m",
        requested_from=START,
        requested_to=END,
        canonical_generation=0,
        store=store,
    )
    assert first is not None

    store.receipts = (receipt,)
    repeated = chart_stream_gap_repair.select_chart_history_repair(
        route=route,
        interval="5m",
        requested_from=START,
        requested_to=END,
        canonical_generation=1,
        store=store,
    )

    assert repeated is None
    assert store.reads == [(START, END), (START, END)]


def test_rolling_selector_never_reads_or_creates_exact_coverage() -> None:
    class Store:
        def read_history_coverage(self, *_args, **_kwargs):
            pytest.fail("rolling provider response cannot consult exact coverage")

    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        history_request_identity=lambda _instrument: _rolling_identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=30),
    )
    route = _route(adapter)

    intent = chart_stream_gap_repair.select_chart_history_repair(
        route=route,
        interval="5m",
        requested_from=START,
        requested_to=END,
        canonical_generation=2,
        store=Store(),
    )

    assert intent is not None
    assert intent.request_identity.contract_resolution is (
        HistoryContractResolution.PROVIDER_RESPONSE
    )
    assert intent.starts_at == START
    assert intent.ends_at == END


def test_exact_selector_returns_none_for_provider_without_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(capabilities=SimpleNamespace(gap_repair=False))
    route = _route(adapter)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )

    assert (
        chart_stream_gap_repair.select_chart_history_repair(
            route=route,
            interval="5m",
            requested_from=START,
            requested_to=END,
            canonical_generation=0,
        )
        is None
    )


def test_exact_selector_returns_none_for_unsupported_route_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unsupported(_instrument):
        raise HistoryRequestUnsupportedError(
            provider="tinvest",
            code="HISTORY_ROUTE_UNSUPPORTED",
        )

    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(gap_repair=True),
        history_request_identity=unsupported,
    )
    route = _route(adapter)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )

    assert (
        chart_stream_gap_repair.select_chart_history_repair(
            route=route,
            interval="5m",
            requested_from=START,
            requested_to=END,
            canonical_generation=0,
        )
        is None
    )


def test_schedule_boundary_only_enqueues_typed_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    intent = HistoryRepairIntent(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        route_fingerprint="route-1",
        timeframe="5m",
        starts_at=START,
        ends_at=END,
        request_identity=_identity(),
        canonical_generation=11,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
    )

    def schedule(**kwargs):
        candidate = kwargs["intent"]
        observed.update(candidate=candidate, timeout=kwargs["timeout"], kwargs=kwargs)
        return HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=candidate,
            newly_admitted=True,
        )

    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(exact_history_snapshot_authority=True),
    )
    route = _route(adapter)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "schedule_provider_history_repair",
        schedule,
    )
    store_calls = 0

    def store_factory() -> object:
        nonlocal store_calls
        store_calls += 1
        return object()

    admission = chart_stream_gap_repair.schedule_chart_cached_gap_repair(
        intent,
        store_factory=store_factory,
        instrument=route.instrument,
        repair_timeout_seconds=4.0,
    )

    assert admission.status is HistoryRepairAdmissionStatus.SCHEDULED
    assert observed["candidate"] is intent
    assert observed["timeout"] == 4.0
    assert observed["kwargs"]["store_factory"] is store_factory
    assert observed["kwargs"]["adapter"] is adapter
    assert observed["kwargs"]["commit"] is chart_stream_gap_repair.commit_history_repair_result
    assert store_calls == 0


def test_schedule_boundary_rejects_stale_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent = HistoryRepairIntent(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        route_fingerprint="route-1",
        timeframe="5m",
        starts_at=START,
        ends_at=END,
        request_identity=_identity(),
        canonical_generation=0,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
    )
    route = _route(SimpleNamespace(), fingerprint="route-2")
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )

    with pytest.raises(ValueError, match="HISTORY_REPAIR_ROUTE_MISMATCH"):
        chart_stream_gap_repair.schedule_chart_cached_gap_repair(
            intent,
            store_factory=object,
            instrument=route.instrument,
        )
