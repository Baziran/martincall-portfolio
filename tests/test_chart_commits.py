from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import aef_terminal.runtime.chart_commits as commit_module
from aef_terminal.data.adapters.coinbase import CoinbaseDataProvider
from aef_terminal.data.adapters.ibkr import IbkrDataProvider
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    HistoryRepairIntent,
    HistoryRepairPriority,
    ProviderHistoryTerminal,
    canonical_bar_commit_receipt,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarProvenance,
)
from aef_terminal.runtime import chart_events
from aef_terminal.runtime.chart_commits import (
    ChartCommitRuntime,
    HistoryCommitRequest,
    HistoryCommitStatus,
)
from aef_terminal.data.history_coverage import (
    CanonicalHistoryCommitReceipt,
    HistoryCoverageReceipt,
    current_history_coverage_contract,
)
from tests.provider_payloads import (
    coinbase_btc_payload,
    ibkr_future_payload,
    ibkr_stock_payload,
)


class _RevisionStore:
    _next_sequence = 0

    def reserve_bar_revision_sequence(self) -> int:
        type(self)._next_sequence += 1
        return type(self)._next_sequence


def _store_factory() -> _RevisionStore:
    return _RevisionStore()


def _receipt(
    bars: list[Bar],
    revision_sequence: int,
    *,
    superseded: list[Bar] | None = None,
    written: int | None = None,
) -> CanonicalBarCommitReceipt:
    return CanonicalBarCommitReceipt(
        revision_sequence=revision_sequence,
        written=len(bars) if written is None else written,
        acknowledged=tuple(bars),
        superseded=tuple(superseded or ()),
    )


def _bar(
    ts: datetime,
    *,
    close: float = 100.5,
    closed: bool = True,
    source: str = "ibkr:chart",
    instrument: dict[str, Any] | None = None,
) -> Bar:
    route = route_instrument(instrument, expected_source="ibkr") if instrument is not None else None
    return Bar(
        "SPY",
        ts,
        100,
        max(101, close),
        99,
        close,
        1000,
        "5m",
        source,
        closed=closed,
        provenance=(
            BarProvenance(
                provider="ibkr",
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                request_type="keep_up_to_date",
                provider_contract_id="test-contract",
                provider_contract_type=(
                    "CONTFUT" if route.instrument.get("asset_class") == "future" else "STK"
                ),
                data_type="TRADES",
            )
            if route is not None
            else None
        ),
    )


def _route(instrument: dict[str, Any], *, expected_source: str) -> SimpleNamespace:
    assert expected_source == instrument["provider"]
    return SimpleNamespace(
        instrument_id=instrument["instrument_id"],
        fingerprint=instrument["route_fingerprint"],
        provider=instrument["provider"],
        instrument=instrument,
    )


def _history_commit_request(
    instrument: dict[str, Any],
    *,
    canonical_generation: int,
    with_coverage: bool = True,
    bars: tuple[Bar, ...] = (),
) -> HistoryCommitRequest:
    route = route_instrument(instrument, expected_source=instrument["provider"])
    starts_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=5)
    receipts: tuple[HistoryCoverageReceipt, ...] = ()
    if with_coverage:
        contract = current_history_coverage_contract(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe="5m",
            request_mode="provider_historical_bars",
            request_type=BarProviderRequest.HISTORICAL.value,
            provider_source="EXCHANGE",
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
            provider_contract_type="STK",
            data_type=route.adapter.bar_data_type(route.instrument),
        )
        receipts = (
            HistoryCoverageReceipt(
                contract=contract,
                covered_from=starts_at,
                covered_to=ends_at,
                response_count=len(bars),
                provider_limit=1000,
                completed_at=ends_at,
            ),
        )
    return HistoryCommitRequest(
        provider=route.provider,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        interval="5m",
        starts_at=starts_at,
        ends_at=ends_at,
        canonical_generation=canonical_generation,
        instrument=route.instrument,
        bars=bars,
        coverage_receipts=receipts,
    )


def _historical_bar(
    instrument: dict[str, Any],
    ts: datetime,
) -> Bar:
    live = _bar(ts, instrument=instrument)
    assert live.provenance is not None
    return replace(
        live,
        provenance=replace(
            live.provenance,
            request_type=BarProviderRequest.HISTORICAL,
        ),
    )


def test_atomic_receipt_acknowledges_identical_and_types_newer_revision_as_superseded() -> None:
    ts = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    submitted = _bar(ts, close=100.5)
    identical = canonical_bar_commit_receipt(
        [submitted],
        [(submitted, 12)],
        revision_sequence=10,
        written=0,
    )
    newer = _bar(ts, close=101.0)
    superseded = canonical_bar_commit_receipt(
        [submitted],
        [(newer, 12)],
        revision_sequence=10,
        written=0,
    )

    assert identical.acknowledged == (submitted,)
    assert identical.superseded == ()
    assert superseded.acknowledged == ()
    assert superseded.superseded == (submitted,)
    with pytest.raises(RuntimeError, match="CANONICAL_BAR_REVISION_ORDER_VIOLATION"):
        canonical_bar_commit_receipt(
            [submitted],
            [(newer, 10)],
            revision_sequence=10,
            written=0,
        )


@pytest.mark.parametrize(
    ("expanded", "expected_status", "expected_generation_delta"),
    (
        (True, HistoryCommitStatus.COMMITTED, 1),
        (False, HistoryCommitStatus.NO_CHANGE, 0),
    ),
)
def test_history_commit_lane_owns_empty_coverage_generation(
    expanded: bool,
    expected_status: HistoryCommitStatus,
    expected_generation_delta: int,
) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload(
            "SPY",
            con_id=87654000 + expected_generation_delta,
        )
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )
        calls: list[tuple[Any, ...]] = []

        class Store(_RevisionStore):
            def commit_history_result(self, bars, **kwargs):
                with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
                    chart_events.require_chart_bars_generation(
                        seen,
                        "5m",
                        route.fingerprint,
                        instrument_id=route.instrument_id,
                    )
                assert active.value.write_in_progress is True
                calls.append((tuple(bars), kwargs))
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=1,
                    coverage_expanded=expanded,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        handle = runtime.submit_history_commit(request, Store)

        assert handle.accepted is True
        outcome = await handle.completion
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )
        assert outcome.status is expected_status
        assert len(calls) == 1
        assert calls[0][0] == ()
        assert calls[0][1]["coverage_receipts"] == request.coverage_receipts
        assert generation == seen + expected_generation_delta
        assert (
            chart_events.require_chart_bars_generation(
                generation,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == generation
        )
        assert len(events) == expected_generation_delta
        if events:
            assert events[0].reason == "history_commit"
            assert events[0].bars == ()

    asyncio.run(run())


def test_history_commit_retries_without_storage_while_authority_publication_is_active() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654123)
        route = route_instrument(instrument, expected_source="ibkr")
        generation = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=generation,
        )
        calls = 0

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                nonlocal calls
                calls += 1
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"],
                    ),
                    coverage_inserted=1,
                    coverage_expanded=True,
                )

        publication_guard = chart_events.begin_chart_bars_publication(
            (
                chart_events.ChartBarsGenerationScope(
                    instrument_id=route.instrument_id,
                    route_fingerprint=route.fingerprint,
                    interval="5m",
                    generation=generation,
                ),
            )
        )
        runtime = ChartCommitRuntime()
        runtime.start()
        handle = runtime.submit_history_commit(request, Store)
        try:
            await asyncio.sleep(0.05)
            assert handle.completion.done() is False
            assert calls == 0
        finally:
            chart_events.end_chart_bars_publication(publication_guard)

        outcome = await asyncio.wait_for(handle.completion, timeout=2.0)
        assert outcome.status is HistoryCommitStatus.COMMITTED
        assert outcome.generation == generation + 1
        assert calls == 1

    asyncio.run(run())


def test_history_commit_lane_publishes_delete_only_snapshot_mutation() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654011)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"],
                        deleted=2,
                    ),
                    coverage_inserted=0,
                    coverage_expanded=False,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        outcome = await runtime.submit_history_commit(request, Store).completion
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )

        assert outcome.status is HistoryCommitStatus.COMMITTED
        assert outcome.receipt is not None
        assert outcome.receipt.bar_receipt.deleted == 2
        assert generation == seen + 1
        assert events[-1].bars == ()
        assert events[-1].repair == {
            "coverage_expanded": False,
            "deleted_bars": 2,
        }

    asyncio.run(run())


def test_history_commit_lane_routes_ibkr_root_to_continuous_storage_owner() -> None:
    async def run() -> None:
        instrument = ibkr_future_payload("ES", con_id=649180671)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        starts_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        bar = _historical_bar(instrument, starts_at)
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
            with_coverage=False,
            bars=(bar,),
        )
        calls: list[tuple[tuple[Bar, ...], dict[str, Any]]] = []

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **_kwargs):
                raise AssertionError("continuous history must not use exact snapshot storage")

            def write_futures_canonical_bars(self, bars, **kwargs):
                calls.append((tuple(bars), dict(kwargs)))
                return CanonicalBarCommitReceipt(
                    revision_sequence=kwargs["revision_sequence"],
                    written=len(bars),
                    acknowledged=tuple(bars),
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        outcome = await runtime.submit_history_commit(request, Store).completion
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )

        assert outcome.status is HistoryCommitStatus.COMMITTED
        assert outcome.receipt is not None
        assert outcome.receipt.bar_receipt.written == 1
        assert len(calls) == 1
        assert calls[0][0] == (bar,)
        assert calls[0][1]["series_type"] == "provider_native"
        assert calls[0][1]["roll_policy"] == "provider_managed"
        assert calls[0][1]["revision_sequence"] > 0
        assert calls[0][1]["metadata"] == {
            "identity_scope": "future_root",
            "provider_contract_type": "CONTFUT",
            "provider_contract_id": "test-contract",
            "data_type": "TRADES",
            "series_authority": "provider",
        }
        assert generation == seen + 1
        assert events[-1].bars == (bar,)

    asyncio.run(run())


def test_history_commit_write_guard_survives_logical_storage_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "CHART_COMMIT_TIMEOUT_SECONDS", 0.01)
        instrument = ibkr_stock_payload("SPY", con_id=87654012)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )
        started = threading.Event()
        release = threading.Event()

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                started.set()
                assert release.wait(timeout=1.0)
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"],
                    ),
                    coverage_inserted=1,
                    coverage_expanded=True,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        handle = runtime.submit_history_commit(request, Store)
        assert await asyncio.to_thread(started.wait, 1.0)
        await asyncio.sleep(0.02)

        assert handle.completion.done() is False
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
            chart_events.require_chart_bars_generation(
                seen,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
        assert active.value.write_in_progress is True

        release.set()
        outcome = await asyncio.wait_for(handle.completion, timeout=1.0)
        assert outcome.status is HistoryCommitStatus.COMMITTED
        assert outcome.generation == seen + 1
        assert (
            chart_events.require_chart_bars_generation(
                seen + 1,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == seen + 1
        )

    asyncio.run(run())


def test_history_repair_commit_handle_settles_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654013)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )
        intent = HistoryRepairIntent(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe=request.interval,
            starts_at=request.starts_at,
            ends_at=request.ends_at,
            request_identity=route.adapter.history_request_identity(route.instrument),
            canonical_generation=seen,
            priority=HistoryRepairPriority.FOREGROUND,
            cooldown_seconds=60.0,
        )
        started = threading.Event()
        release = threading.Event()

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                started.set()
                assert release.wait(timeout=1.0)
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"],
                    ),
                    coverage_inserted=1,
                    coverage_expanded=True,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        monkeypatch.setattr(commit_module, "chart_commits", runtime)
        task = asyncio.create_task(
            commit_module.commit_history_repair_result(
                instrument=route.instrument,
                intent=intent,
                terminal=ProviderHistoryTerminal.COMPLETE,
                bars=request.bars,
                coverage_receipts=request.coverage_receipts,
                store_factory=Store,
                error_code=None,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            chart_events.chart_bars_updated_generation(
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == seen + 1
        )

    asyncio.run(run())


def test_history_commit_stale_generation_never_opens_storage() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654002)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )
        stores = 0

        def store_factory() -> _RevisionStore:
            nonlocal stores
            stores += 1
            return _RevisionStore()

        runtime = ChartCommitRuntime()
        runtime.start()
        handle = runtime.submit_history_commit(request, store_factory)
        chart_events.publish_chart_bars_updated(
            "ibkr",
            "5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            reason="test_generation_advance",
        )

        outcome = await handle.completion

        assert outcome.status is HistoryCommitStatus.STALE_GENERATION
        assert stores == 0

    asyncio.run(run())


@pytest.mark.parametrize("provider", ("IBKR", "not-registered"))
def test_history_commit_requires_canonical_provider_key(provider: str) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=87654003)
    request = _history_commit_request(instrument, canonical_generation=0)

    with pytest.raises(ValueError, match="HISTORY_COMMIT_PROVIDER_NOT_CANONICAL"):
        replace(request, provider=provider)


def test_history_pending_item_owns_its_exact_store_factory() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654004)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )
        calls: list[str] = []

        class Store(_RevisionStore):
            def __init__(self, name: str) -> None:
                self.name = name

            def commit_history_result(self, _bars, **kwargs):
                calls.append(self.name)
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=0,
                    coverage_expanded=False,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        first = runtime.submit_history_commit(request, lambda: Store("first"))
        second = runtime.submit_history_commit(request, lambda: Store("second"))

        assert (await first.completion).status is HistoryCommitStatus.NO_CHANGE
        assert (await second.completion).status is HistoryCommitStatus.NO_CHANGE
        assert calls == ["first", "second"]

    asyncio.run(run())


def test_history_commit_malformed_receipt_invalidates_and_stales_queued_read() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654005)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        starts_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        bad_request = _history_commit_request(
            instrument,
            canonical_generation=seen,
            with_coverage=False,
            bars=(_historical_bar(instrument, starts_at),),
        )
        next_request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )
        calls: list[str] = []

        class BadStore(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                calls.append("bad")
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=0,
                    coverage_expanded=False,
                )

        class GoodStore(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                calls.append("good")
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=0,
                    coverage_expanded=False,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        bad = runtime.submit_history_commit(bad_request, BadStore)
        good = runtime.submit_history_commit(next_request, GoodStore)

        bad_outcome = await bad.completion
        good_outcome = await good.completion
        assert bad_outcome.status is HistoryCommitStatus.FAILED
        assert bad_outcome.error_code == "HISTORY_COMMIT_RECEIPT_OUTCOME_INCOMPLETE"
        assert good_outcome.status is HistoryCommitStatus.STALE_GENERATION
        assert good_outcome.generation == seen + 1
        assert calls == ["bad"]
        assert (
            chart_events.require_chart_bars_generation(
                seen + 1,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == seen + 1
        )
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )
        assert generation == seen + 1
        assert events[-1].reason == "history_commit_outcome_uncertain"

    asyncio.run(run())


def test_history_commit_event_failure_invalidates_before_releasing_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654013)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(
            instrument,
            canonical_generation=seen,
        )

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=1,
                    coverage_expanded=True,
                )

        def fail_publication(*_args, **_kwargs):
            raise RuntimeError("event bus unavailable")

        monkeypatch.setattr(
            commit_module,
            "publish_chart_bars_updated",
            fail_publication,
        )
        runtime = ChartCommitRuntime()
        runtime.start()
        outcome = await runtime.submit_history_commit(request, Store).completion

        assert outcome.status is HistoryCommitStatus.FAILED
        assert outcome.error_code == ("HISTORY_COMMIT_EVENT_PUBLICATION_FAILED:RuntimeError")
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )
        assert generation == seen + 1
        assert outcome.generation == generation
        assert events[-1].reason == "history_commit_outcome_uncertain"
        assert (
            chart_events.require_chart_bars_generation(
                generation,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == generation
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error", "error_code"),
    (
        (
            ValueError("contract mismatch"),
            "HISTORY_COMMIT_STORAGE_OUTCOME_UNCERTAIN:ValueError",
        ),
        (
            RuntimeError("durability invariant"),
            "HISTORY_COMMIT_STORAGE_OUTCOME_UNCERTAIN:RuntimeError",
        ),
    ),
)
def test_history_commit_deterministic_storage_failure_is_terminal(
    error: Exception,
    error_code: str,
) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654006)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(instrument, canonical_generation=seen)
        calls = 0

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **_kwargs):
                nonlocal calls
                calls += 1
                raise error

        runtime = ChartCommitRuntime()
        runtime.start()
        outcome = await runtime.submit_history_commit(request, Store).completion

        assert outcome.status is HistoryCommitStatus.FAILED
        assert outcome.error_code == error_code
        assert outcome.generation == seen + 1
        assert calls == 1
        assert (
            chart_events.require_chart_bars_generation(
                seen + 1,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == seen + 1
        )

    asyncio.run(run())


def test_history_commit_storage_timeout_is_uncertain_and_not_retried() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654007)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(instrument, canonical_generation=seen)
        attempts: list[int] = []
        delays: list[float] = []

        class Store(_RevisionStore):
            def reserve_bar_revision_sequence(self) -> int:
                return 701

            def commit_history_result(self, _bars, **kwargs):
                attempts.append(kwargs["revision_sequence"])
                raise TimeoutError("commit acknowledgement lost")

        runtime = ChartCommitRuntime()
        runtime.start()

        async def record_retry(delay: float) -> bool:
            delays.append(delay)
            return True

        runtime._wait_to_retry = record_retry  # type: ignore[method-assign]
        outcome = await runtime.submit_history_commit(request, Store).completion

        assert outcome.status is HistoryCommitStatus.FAILED
        assert outcome.error_code == ("HISTORY_COMMIT_STORAGE_OUTCOME_UNCERTAIN:TimeoutError")
        assert outcome.generation == seen + 1
        assert attempts == [701]
        assert delays == []
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )
        assert generation == seen + 1
        assert events[-1].reason == "history_commit_storage_outcome_uncertain"

    asyncio.run(run())


def test_history_commit_generation_ownership_loss_is_terminal_not_retried() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654008)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        request = _history_commit_request(instrument, canonical_generation=seen)
        started = threading.Event()
        release = threading.Event()
        calls = 0

        class Store(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                nonlocal calls
                calls += 1
                started.set()
                assert release.wait(timeout=1.0)
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=1,
                    coverage_expanded=True,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        handle = runtime.submit_history_commit(request, Store)
        assert await asyncio.to_thread(started.wait, 1.0)
        chart_events.publish_chart_bars_updated(
            "ibkr",
            "5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            reason="test_generation_race",
        )
        release.set()

        outcome = await handle.completion
        assert outcome.status is HistoryCommitStatus.FAILED
        assert outcome.error_code == "HISTORY_COMMIT_GENERATION_OWNERSHIP_LOST"
        assert outcome.receipt is not None and outcome.receipt.mutated is True
        assert calls == 1

    asyncio.run(run())


def test_history_commit_gets_a_fair_turn_among_live_bar_work() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654009)
        route = route_instrument(instrument, expected_source="ibkr")
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        order: list[str] = []
        live_bar = _bar(
            datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            instrument=instrument,
        )

        class LiveStore(_RevisionStore):
            pass

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **kwargs):
                order.append("live")
                return _receipt(list(bars), kwargs["revision_sequence"], written=0)

        class HistoryStore(_RevisionStore):
            def commit_history_result(self, _bars, **kwargs):
                order.append("history")
                return CanonicalHistoryCommitReceipt(
                    bar_receipt=CanonicalBarCommitReceipt(
                        revision_sequence=kwargs["revision_sequence"]
                    ),
                    coverage_inserted=0,
                    coverage_expanded=False,
                )

        runtime = ChartCommitRuntime()
        runtime.start()
        assert runtime.submit(Adapter(), instrument, "5m", LiveStore, [live_bar]) == 1
        handle = runtime.submit_history_commit(
            _history_commit_request(instrument, canonical_generation=seen),
            HistoryStore,
        )
        lane = runtime._lanes[(route.instrument_id, route.fingerprint, "5m")]

        assert (await handle.completion).status is HistoryCommitStatus.NO_CHANGE
        assert lane.task is not None
        await lane.task
        assert order == ["history", "live"]

    asyncio.run(run())


def test_commit_submit_is_nonblocking_and_excludes_forming_bars(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        started = asyncio.Event()
        release = asyncio.Event()
        committed: list[list[Bar]] = []
        stores: list[_RevisionStore] = []

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(
                self,
                bars,
                *,
                interval,
                store,
                instrument,
                revision_sequence,
            ):
                assert interval == "5m"
                assert instrument["instrument_id"] == "ibkr|contract|commit-nonblocking"
                assert store is stores[0]
                with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
                    chart_events.require_chart_bars_generation(
                        seen,
                        "5m",
                        instrument["route_fingerprint"],
                        instrument_id=instrument["instrument_id"],
                    )
                assert active.value.write_in_progress is True
                committed.append(list(bars))
                started.set()
                await release.wait()
                return _receipt(list(bars), revision_sequence)

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-nonblocking",
            "route_fingerprint": "ibkr|SPY|commit-nonblocking",
        }
        ts = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        confirmed = _bar(
            ts,
            source="ibkr:keep_up_to_date:TRADES:contract=87654321:route=chart",
        )
        forming = _bar(ts + timedelta(minutes=5), closed=False)
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            instrument["route_fingerprint"],
            instrument_id=instrument["instrument_id"],
        )

        def store_factory() -> _RevisionStore:
            store = _RevisionStore()
            stores.append(store)
            return store

        accepted = runtime.submit(Adapter(), instrument, "5m", store_factory, [confirmed, forming])

        assert accepted == 1
        assert committed == []
        assert stores == []
        await started.wait()
        assert committed == [[confirmed]]
        release.set()
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await lane.task
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            instrument["route_fingerprint"],
            seen,
            instrument_id=instrument["instrument_id"],
        )
        assert generation == seen + 1
        assert (
            chart_events.require_chart_bars_generation(
                generation,
                "5m",
                instrument["route_fingerprint"],
                instrument_id=instrument["instrument_id"],
            )
            == generation
        )
        assert events[-1].bars == (confirmed,)

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure_phase",
    ("storage_error", "invalid_receipt", "event_failure"),
)
def test_live_uncertain_commit_invalidates_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        publication_calls = 0

        def fail_publication(*_args, **_kwargs):
            nonlocal publication_calls
            publication_calls += 1
            raise RuntimeError("event bus unavailable")

        if failure_phase == "event_failure":
            monkeypatch.setattr(
                commit_module,
                "publish_chart_bars_updated",
                fail_publication,
            )

        runtime = ChartCommitRuntime()
        runtime.start()
        calls = 0
        durable_attempt_observed = False

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **kwargs):
                nonlocal calls, durable_attempt_observed
                calls += 1
                if calls == 1:
                    durable_attempt_observed = True
                    if failure_phase == "storage_error":
                        raise ConnectionError("commit acknowledgement lost")
                    if failure_phase == "invalid_receipt":
                        return object()
                    return _receipt(
                        list(bars),
                        kwargs["revision_sequence"],
                        written=1,
                    )
                return _receipt(
                    list(bars),
                    kwargs["revision_sequence"],
                    written=0,
                )

        instrument = {
            "provider": "ibkr",
            "instrument_id": f"ibkr|contract|commit-recovery-{failure_phase}",
            "route_fingerprint": f"ibkr|SPY|commit-recovery-{failure_phase}",
        }
        bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            instrument["route_fingerprint"],
            instrument_id=instrument["instrument_id"],
        )

        async def retry_immediately(_delay: float) -> bool:
            return True

        runtime._wait_to_retry = retry_immediately  # type: ignore[method-assign]
        assert (
            runtime.submit(
                Adapter(),
                instrument,
                "5m",
                _store_factory,
                [bar],
            )
            == 1
        )
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await asyncio.wait_for(lane.task, timeout=1.0)

        assert durable_attempt_observed is True
        assert calls == 2
        assert publication_calls == (1 if failure_phase == "event_failure" else 0)
        assert lane.pending == {}
        generation, events = chart_events.chart_bars_updated_events_since(
            "5m",
            instrument["route_fingerprint"],
            seen,
            instrument_id=instrument["instrument_id"],
        )
        assert generation == seen + 1
        assert events[-1].reason == "canonical_commit_outcome_uncertain"
        assert events[-1].repair == {"commit_outcome": "uncertain"}
        assert (
            chart_events.require_chart_bars_generation(
                generation,
                "5m",
                instrument["route_fingerprint"],
                instrument_id=instrument["instrument_id"],
            )
            == generation
        )

    asyncio.run(run())


def test_commit_lane_deduplicates_confirmed_revision_and_accepts_correction(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        committed: list[list[Bar]] = []

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **_kwargs):
                committed.append(list(bars))
                return _receipt(list(bars), _kwargs["revision_sequence"])

        adapter = Adapter()
        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-revision",
            "route_fingerprint": "ibkr|SPY|commit-revision",
        }
        ts = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        original = _bar(ts)
        corrected = _bar(ts, close=100.75)

        assert runtime.submit(adapter, instrument, "5m", _store_factory, [original]) == 1
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await lane.task
        assert runtime.submit(adapter, instrument, "5m", _store_factory, [original]) == 0
        assert runtime.submit(adapter, instrument, "5m", _store_factory, [corrected]) == 1
        assert lane.task is not None
        await lane.task

        assert committed == [[original], [corrected]]

    asyncio.run(run())


def test_commit_lane_accepts_source_only_correction(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        committed: list[list[Bar]] = []

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **_kwargs):
                committed.append(list(bars))
                return _receipt(list(bars), _kwargs["revision_sequence"])

        adapter = Adapter()
        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-source-revision",
            "route_fingerprint": "ibkr|SPY|commit-source-revision",
        }
        ts = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        original = _bar(ts, source="ibkr:historical:TRADES:contract=1:route=old")
        corrected = _bar(ts, source="ibkr:historical:TRADES:contract=1:route=new")

        assert runtime.submit(adapter, instrument, "5m", _store_factory, [original]) == 1
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await lane.task
        assert runtime.submit(adapter, instrument, "5m", _store_factory, [corrected]) == 1
        assert lane.task is not None
        await lane.task

        assert committed == [[original], [corrected]]

    asyncio.run(run())


def test_commit_lane_accepts_provenance_only_correction(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        committed: list[list[Bar]] = []

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **_kwargs):
                committed.append(list(bars))
                return _receipt(list(bars), _kwargs["revision_sequence"])

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-provenance-revision",
            "route_fingerprint": "ibkr|SPY|commit-provenance-revision",
        }
        base = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        provenance_fields = {
            "provider": "ibkr",
            "instrument_id": instrument["instrument_id"],
            "route_fingerprint": instrument["route_fingerprint"],
            "provider_contract_id": "756733",
            "provider_contract_type": "STK",
            "data_type": "TRADES",
        }
        historical = replace(
            base,
            provenance=BarProvenance(
                request_type=BarProviderRequest.HISTORICAL,
                **provenance_fields,
            ),
        )
        keep_up_to_date = replace(
            base,
            provenance=BarProvenance(
                request_type=BarProviderRequest.KEEP_UP_TO_DATE,
                **provenance_fields,
            ),
        )

        assert (
            runtime.submit(
                Adapter(),
                instrument,
                "5m",
                _store_factory,
                [historical],
            )
            == 1
        )
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await lane.task
        assert (
            runtime.submit(
                Adapter(),
                instrument,
                "5m",
                _store_factory,
                [keep_up_to_date],
            )
            == 1
        )
        assert lane.task is not None
        await lane.task

        assert committed == [[historical], [keep_up_to_date]]

    asyncio.run(run())


def test_commit_lane_serializes_bar_arriving_during_inflight_write(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        committed: list[list[Bar]] = []

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **_kwargs):
                committed.append(list(bars))
                if len(committed) == 1:
                    first_started.set()
                    await release_first.wait()
                return _receipt(list(bars), _kwargs["revision_sequence"])

        adapter = Adapter()
        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-serialized",
            "route_fingerprint": "ibkr|SPY|commit-serialized",
        }
        ts = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        first = _bar(ts)
        second = _bar(ts + timedelta(minutes=5), close=101.5)

        assert runtime.submit(adapter, instrument, "5m", _store_factory, [first]) == 1
        await first_started.wait()
        assert runtime.submit(adapter, instrument, "5m", _store_factory, [second]) == 1
        release_first.set()
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await lane.task

        assert committed == [[first], [second]]
        assert lane.pending == {}

    asyncio.run(run())


def test_commit_lane_waits_for_physical_attempt_after_bounded_timeout(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        monkeypatch.setattr(commit_module, "CHART_COMMIT_TIMEOUT_SECONDS", 0.01)
        runtime = ChartCommitRuntime()
        runtime.start()
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **_kwargs):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return _receipt(list(bars), _kwargs["revision_sequence"])

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-timeout",
            "route_fingerprint": "ibkr|SPY|commit-timeout",
        }
        bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            instrument["route_fingerprint"],
            instrument_id=instrument["instrument_id"],
        )
        assert runtime.submit(Adapter(), instrument, "5m", _store_factory, [bar]) == 1
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await started.wait()
        await asyncio.sleep(0.02)
        assert calls == 1
        assert not lane.task.done()
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
            chart_events.require_chart_bars_generation(
                seen,
                "5m",
                instrument["route_fingerprint"],
                instrument_id=instrument["instrument_id"],
            )
        assert active.value.write_in_progress is True
        release.set()
        await asyncio.wait_for(lane.task, timeout=1.0)

        assert calls == 1
        assert lane.pending == {}
        generation = chart_events.chart_bars_updated_generation(
            "5m",
            instrument["route_fingerprint"],
            instrument_id=instrument["instrument_id"],
        )
        assert (
            chart_events.require_chart_bars_generation(
                generation,
                "5m",
                instrument["route_fingerprint"],
                instrument_id=instrument["instrument_id"],
            )
            == generation
        )
        assert lane.committed[bar.ts] == commit_module.bar_ingress_revision_signature(bar)

    asyncio.run(run())


def test_commit_lane_retries_until_every_submitted_revision_is_acknowledged(
    monkeypatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        calls: list[list[Bar]] = []
        sequences: list[int] = []

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **_kwargs):
                calls.append(list(bars))
                sequences.append(_kwargs["revision_sequence"])
                acknowledged = list(bars[:1]) if len(calls) == 1 else list(bars)
                return _receipt(
                    acknowledged,
                    _kwargs["revision_sequence"],
                    written=len(acknowledged),
                )

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-exact-ack",
            "route_fingerprint": "ibkr|SPY|commit-exact-ack",
        }
        first = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        second = _bar(datetime(2026, 7, 15, 12, 5, tzinfo=UTC), close=101.0)

        assert (
            runtime.submit(
                Adapter(),
                instrument,
                "5m",
                _store_factory,
                [first, second],
            )
            == 2
        )
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await asyncio.wait_for(lane.task, timeout=1.0)

        assert calls == [[first, second], [second]]
        assert sequences[0] == sequences[1]
        assert lane.pending == {}
        assert set(lane.committed) == {first.ts, second.ts}

    asyncio.run(run())


def test_commit_lane_dedupe_tail_stays_bounded_during_partial_retry(
    monkeypatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        retry_waiting = asyncio.Event()
        release_retry = asyncio.Event()
        calls = 0

        async def hold_retry(_delay: float) -> None:
            retry_waiting.set()
            await release_retry.wait()

        monkeypatch.setattr(commit_module.asyncio, "sleep", hold_retry)

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **kwargs):
                nonlocal calls
                calls += 1
                acknowledged = list(bars[:-1]) if calls == 1 else list(bars)
                return _receipt(
                    acknowledged,
                    kwargs["revision_sequence"],
                    written=len(acknowledged),
                )

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-bounded-dedupe",
            "route_fingerprint": "ibkr|SPY|commit-bounded-dedupe",
        }
        starts_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        bars = [
            _bar(
                starts_at + timedelta(minutes=5 * index),
                close=100.0 + index,
            )
            for index in range(commit_module.CHART_COMMIT_DEDUPE_TAIL + 44)
        ]

        assert runtime.submit(
            Adapter(),
            instrument,
            "5m",
            _store_factory,
            bars,
        ) == len(bars)
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await asyncio.wait_for(retry_waiting.wait(), timeout=1.0)

        assert len(lane.committed) == commit_module.CHART_COMMIT_DEDUPE_TAIL
        assert set(lane.committed) == {
            bar.ts for bar in bars[43 : commit_module.CHART_COMMIT_DEDUPE_TAIL + 43]
        }
        assert set(lane.committed_slots) == set(lane.committed)

        release_retry.set()
        await asyncio.wait_for(lane.task, timeout=1.0)

        assert len(lane.committed) == commit_module.CHART_COMMIT_DEDUPE_TAIL
        assert set(lane.committed) == {
            bar.ts for bar in bars[-commit_module.CHART_COMMIT_DEDUPE_TAIL :]
        }
        assert set(lane.committed_slots) == set(lane.committed)

    asyncio.run(run())


def test_commit_lane_drops_typed_superseded_revision_without_retry(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        calls = 0

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **kwargs):
                nonlocal calls
                calls += 1
                return CanonicalBarCommitReceipt(
                    revision_sequence=kwargs["revision_sequence"],
                    superseded=tuple(bars),
                )

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-superseded",
            "route_fingerprint": "ibkr|SPY|commit-superseded",
        }
        bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))

        assert runtime.submit(Adapter(), instrument, "5m", _store_factory, [bar]) == 1
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await asyncio.wait_for(lane.task, timeout=1.0)

        assert calls == 1
        assert lane.pending == {}
        assert lane.committed[bar.ts] == commit_module.bar_ingress_revision_signature(bar)

    asyncio.run(run())


def test_commit_runtime_does_not_create_lane_for_provisional_only_batch(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-provisional",
            "route_fingerprint": "ibkr|SPY|commit-provisional",
        }
        forming = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC), closed=False)

        assert runtime.submit(SimpleNamespace(key="ibkr"), instrument, "5m", object, [forming]) == 0
        assert runtime._lanes == {}

    asyncio.run(run())


def test_commit_runtime_admission_closes_during_shutdown(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-lifecycle",
            "route_fingerprint": "ibkr|SPY|commit-lifecycle",
        }
        bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        with pytest.raises(RuntimeError, match="CHART_COMMIT_RUNTIME_NOT_ACCEPTING"):
            runtime.submit(SimpleNamespace(key="ibkr"), instrument, "5m", object, [bar])

        runtime.start()
        outcome = await runtime.shutdown(grace_seconds=0)
        assert outcome.status == "drained"
        with pytest.raises(RuntimeError, match="CHART_COMMIT_RUNTIME_NOT_ACCEPTING"):
            runtime.submit(SimpleNamespace(key="ibkr"), instrument, "5m", object, [bar])

    asyncio.run(run())


def test_commit_shutdown_waits_for_physical_attempt_without_cancelling_it(
    monkeypatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = False

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **kwargs):
                nonlocal cancelled
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                return _receipt(list(bars), kwargs["revision_sequence"])

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-physical-shutdown",
            "route_fingerprint": "ibkr|SPY|commit-physical-shutdown",
        }
        bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        runtime.submit(Adapter(), instrument, "5m", _store_factory, [bar])
        await started.wait()
        shutdown_task = asyncio.create_task(runtime.shutdown(grace_seconds=0))
        await asyncio.sleep(0)

        assert not shutdown_task.done()
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            instrument["route_fingerprint"],
            instrument_id=instrument["instrument_id"],
        )
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
            chart_events.require_chart_bars_generation(
                seen,
                "5m",
                instrument["route_fingerprint"],
                instrument_id=instrument["instrument_id"],
            )
        assert active.value.write_in_progress is True
        with pytest.raises(RuntimeError, match="CHART_COMMIT_RUNTIME_NOT_ACCEPTING"):
            runtime.submit(Adapter(), instrument, "5m", _store_factory, [bar])
        release.set()
        outcome = await shutdown_task
        assert outcome.status == "drained"
        assert cancelled is False
        generation = chart_events.chart_bars_updated_generation(
            "5m",
            instrument["route_fingerprint"],
            instrument_id=instrument["instrument_id"],
        )
        assert (
            chart_events.require_chart_bars_generation(
                generation,
                "5m",
                instrument["route_fingerprint"],
                instrument_id=instrument["instrument_id"],
            )
            == generation
        )

    asyncio.run(run())


def test_commit_runtime_rearms_pending_lane_after_store_factory_failure(
    monkeypatch,
) -> None:
    async def run() -> None:
        monkeypatch.setattr(commit_module, "route_instrument", _route)
        runtime = ChartCommitRuntime()
        runtime.start()
        factory_called = asyncio.Event()
        storage_available = False
        committed: list[Bar] = []

        def store_factory() -> _RevisionStore:
            factory_called.set()
            if not storage_available:
                raise RuntimeError("storage unavailable")
            return _RevisionStore()

        class Adapter:
            key = "ibkr"

            async def async_commit_chart_bars(self, bars, **kwargs):
                committed.extend(bars)
                return _receipt(list(bars), kwargs["revision_sequence"])

        instrument = {
            "provider": "ibkr",
            "instrument_id": "ibkr|contract|commit-rearm",
            "route_fingerprint": "ibkr|SPY|commit-rearm",
        }
        bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        runtime.submit(Adapter(), instrument, "5m", store_factory, [bar])
        await factory_called.wait()
        outcome = await runtime.shutdown(grace_seconds=0)

        assert outcome.status == "pending_rearm"
        assert outcome.pending_bars == 1
        storage_available = True
        runtime.start()
        lane = next(iter(runtime._lanes.values()))
        assert lane.task is not None
        await lane.task
        assert committed == [bar]
        assert lane.pending == {}

    asyncio.run(run())


def test_ibkr_commit_contract_writes_only_confirmed_bars_without_publishing() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654321)
        route = route_instrument(instrument, expected_source="ibkr")
        adapter = IbkrDataProvider()
        writes: list[list[Bar]] = []

        class Store:
            def write_bars(
                self,
                bars,
                provider,
                *,
                instrument,
                revision_sequence,
            ) -> CanonicalBarCommitReceipt:
                assert provider == "ibkr"
                assert instrument["instrument_id"] == route.instrument_id
                writes.append(list(bars))
                return _receipt(list(bars), revision_sequence)

        ts = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
        confirmed = _bar(
            ts,
            source="ibkr:keep_up_to_date:TRADES:contract=87654321:route=chart",
            instrument=instrument,
        )
        forming = _bar(ts + timedelta(minutes=5), closed=False)
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )

        result = await adapter.async_commit_chart_bars(
            [confirmed, forming],
            interval="5m",
            store=Store(),
            instrument=instrument,
            revision_sequence=101,
        )
        generation, canonical_events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )

        assert result.acknowledged == (confirmed,)
        assert writes == [[confirmed]]
        assert generation == seen
        assert canonical_events == []

    asyncio.run(run())


def test_partial_provider_commit_returns_only_verified_revision_without_publishing() -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654325)
        route = route_instrument(instrument, expected_source="ibkr")
        first = _bar(
            datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            source="ibkr:keep_up_to_date:TRADES:contract=87654325:route=chart",
            instrument=instrument,
        )
        second = _bar(
            datetime(2026, 7, 15, 12, 5, tzinfo=UTC),
            source="ibkr:keep_up_to_date:TRADES:contract=87654325:route=chart",
            instrument=instrument,
        )

        class Store:
            def initialize(self) -> None:
                return None

            def write_bars(self, bars, *_args, **kwargs) -> CanonicalBarCommitReceipt:
                assert list(bars) == [first, second]
                return _receipt([first], kwargs["revision_sequence"], written=1)

        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )

        result = await IbkrDataProvider().async_commit_chart_bars(
            [first, second],
            interval="5m",
            store=Store(),
            instrument=instrument,
            revision_sequence=102,
        )
        generation, canonical_events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )

        assert result.acknowledged == (first,)
        assert generation == seen
        assert canonical_events == []

    asyncio.run(run())


def test_ibkr_identical_futures_commit_acknowledges_without_publishing() -> None:
    async def run() -> None:
        instrument = ibkr_future_payload("ES")
        route = route_instrument(instrument, expected_source="ibkr")
        writes = 0

        class Store:
            def initialize(self) -> None:
                return None

            def write_futures_canonical_bars(
                self,
                bars,
                **kwargs,
            ) -> CanonicalBarCommitReceipt:
                nonlocal writes
                writes += 1
                assert len(bars) == 1
                return _receipt(list(bars), kwargs["revision_sequence"], written=0)

        confirmed = _bar(
            datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            source="ibkr:keep_up_to_date:TRADES:contract=CONTFUT:route=chart",
            instrument=instrument,
        )
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )

        result = await IbkrDataProvider().async_commit_chart_bars(
            [confirmed],
            interval="5m",
            store=Store(),
            instrument=instrument,
            revision_sequence=103,
        )
        generation, canonical_events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )

        assert result.acknowledged == (confirmed,)
        assert writes == 1
        assert generation == seen
        assert canonical_events == []

    asyncio.run(run())


def test_coinbase_commit_uses_only_canonical_bar_storage_without_parallel_publish() -> None:
    async def run() -> None:
        instrument = coinbase_btc_payload()
        route = route_instrument(instrument, expected_source="coinbase")
        confirmed = Bar(
            "BTC",
            datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            118_000.0,
            118_250.0,
            117_900.0,
            118_100.0,
            25.0,
            "5m",
            "coinbase:exchange-candles",
            closed=True,
        )
        writes = 0

        class Store:
            def initialize(self) -> None:
                return None

            def write_bars(self, bars, *_args, **kwargs) -> CanonicalBarCommitReceipt:
                nonlocal writes
                writes += 1
                return _receipt(list(bars), kwargs["revision_sequence"], written=1)

        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )

        result = await CoinbaseDataProvider().async_commit_chart_bars(
            [confirmed],
            interval="5m",
            store=Store(),
            instrument=instrument,
            revision_sequence=104,
        )
        generation, canonical_events = chart_events.chart_bars_updated_events_since(
            "5m",
            route.fingerprint,
            seen,
            instrument_id=route.instrument_id,
        )

        assert result.acknowledged == (confirmed,)
        assert writes == 1
        assert generation == seen
        assert canonical_events == []

    asyncio.run(run())


def test_ibkr_invalid_provenance_is_not_committed_and_typed_correction_retries(
    monkeypatch,
) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY", con_id=87654323)
        route = route_instrument(instrument, expected_source="ibkr")
        invalid = _bar(
            datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
            source="ibkr:chart",
        )
        corrected = _bar(
            invalid.ts,
            source="ibkr:keep_up_to_date:TRADES:contract=87654323:route=chart",
            instrument=instrument,
        )
        retry_waiting = asyncio.Event()
        release_retry = asyncio.Event()
        writes: list[list[Bar]] = []

        async def hold_retry(_delay: float) -> None:
            retry_waiting.set()
            await release_retry.wait()

        monkeypatch.setattr(commit_module.asyncio, "sleep", hold_retry)

        class Store:
            def reserve_bar_revision_sequence(self) -> int:
                return 105

            def initialize(self) -> None:
                return None

            def write_bars(self, bars, *_args, **kwargs) -> CanonicalBarCommitReceipt:
                writes.append(list(bars))
                return _receipt(list(bars), kwargs["revision_sequence"])

        runtime = ChartCommitRuntime()
        runtime.start()
        adapter = IbkrDataProvider()
        assert runtime.submit(adapter, instrument, "5m", Store, [invalid]) == 1
        lane = runtime._lanes[(route.instrument_id, route.fingerprint, "5m")]
        assert lane.task is not None
        await asyncio.wait_for(retry_waiting.wait(), timeout=1.0)

        assert lane.committed == {}
        assert lane.pending[invalid.ts] == invalid
        assert runtime.submit(adapter, instrument, "5m", Store, [corrected]) == 1
        assert lane.pending[corrected.ts] == corrected

        release_retry.set()
        await asyncio.wait_for(lane.task, timeout=1.0)

        assert writes == [[corrected]]
        assert lane.pending == {}
        assert lane.committed[corrected.ts] == commit_module.bar_ingress_revision_signature(
            corrected
        )

    asyncio.run(run())


def test_ibkr_commit_rejects_invalid_provenance_with_typed_reason() -> None:
    instrument = ibkr_stock_payload("SPY", con_id=87654324)
    invalid = _bar(
        datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        source="ibkr:chart",
    )

    class Store:
        def initialize(self) -> None:
            raise AssertionError("rejected bars must fail before storage initialization")

    with pytest.raises(
        RuntimeError,
        match="CHART_COMMIT_BAR_REJECTED provider=ibkr reason=provider_provenance_missing",
    ):
        asyncio.run(
            IbkrDataProvider().async_commit_chart_bars(
                [invalid],
                interval="5m",
                store=Store(),
                instrument=instrument,
                revision_sequence=106,
            )
        )


def test_ibkr_commit_contract_rejects_missing_canonical_store() -> None:
    instrument = ibkr_stock_payload("SPY", con_id=87654322)
    bar = _bar(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))

    with pytest.raises(RuntimeError, match="CHART_COMMIT_STORE_REQUIRED"):
        asyncio.run(
            IbkrDataProvider().async_commit_chart_bars(
                [bar],
                interval="5m",
                store=None,
                instrument=instrument,
                revision_sequence=107,
            )
        )
