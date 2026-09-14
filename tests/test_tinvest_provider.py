from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import aef_terminal.data.adapters._tinvest.provider as provider_module
from aef_terminal.data.adapters._tinvest.history import (
    TInvestHistoryChunkRequest,
    TInvestHistoryChunkResult,
    TInvestHistoryResult,
    TInvestHistoryTerminal,
)
from aef_terminal.data.adapters._tinvest.provider import TInvestDataProvider
from aef_terminal.data.adapters._tinvest.qualification import (
    TInvestInstrumentCandidate,
    tinvest_candidate_payload,
)
from aef_terminal.data.adapters._tinvest.schedule import TInvestScheduleRequest
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryStorageKind,
    HistoryRepairIntent,
    HistoryRepairPriority,
    ProviderHistoryTerminal,
    ProviderSessionScope,
    QuotePollingProvider,
    TradingScheduleProvider,
)
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance, BarState
from aef_terminal.runtime.timeframes import HistoryRangeWindow


def _instrument(*, uid: str, asset_class: str = "stock") -> dict[str, Any]:
    instrument_kind = (
        "INSTRUMENT_TYPE_FUTURES" if asset_class == "future" else "INSTRUMENT_TYPE_SHARE"
    )
    return tinvest_candidate_payload(
        TInvestInstrumentCandidate(
            instrument_uid=uid,
            position_uid=f"position:{uid}",
            figi=f"figi:{uid}",
            ticker="SiU6" if asset_class == "future" else "SBER",
            class_code="SPBFUT" if asset_class == "future" else "TQBR",
            instrument_type="futures" if asset_class == "future" else "share",
            instrument_kind=instrument_kind,
            asset_class=asset_class,
            name=f"Exact {uid}",
            api_trade_available=True,
            for_qualified_investor=False,
            weekend_trading_available=False,
            lot=1,
            first_1m_candle_at=datetime(2026, 1, 1, tzinfo=UTC),
            first_1d_candle_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


def _bar(
    instrument: dict[str, Any],
    ts: datetime,
    *,
    confirmed: bool = True,
) -> Bar:
    instrument_id = str(instrument["instrument_id"])
    contract_id = str(instrument["provider_contract_id"])
    return Bar(
        symbol=str(instrument["provider_symbol"]),
        ts=ts,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        timeframe="5m",
        source="tinvest:exchange",
        closed=confirmed,
        state=BarState.CONFIRMED if confirmed else BarState.FORMING,
        provenance=BarProvenance(
            provider="tinvest",
            instrument_id=instrument_id,
            route_fingerprint=instrument_id,
            request_type=BarProviderRequest.HISTORICAL,
            provider_contract_id=contract_id,
            provider_contract_type="INSTRUMENT_UID",
            data_type="TRADES",
        ),
    )


def test_manifest_exposes_only_the_implemented_public_runtime_modes() -> None:
    provider = TInvestDataProvider()

    assert provider.key == "tinvest"
    assert provider.db_providers == ("tinvest",)
    assert provider.manifest.source_prefixes == ("tinvest:",)
    assert provider.manifest.session_scope is ProviderSessionScope.INSTRUMENT
    assert provider.manifest.requires == ("AEF_TINVEST_TOKEN",)
    assert provider.capabilities.chart_stream is True
    assert provider.capabilities.chart_tail_polling is True
    assert provider.capabilities.native_chart_stream is False
    assert provider.capabilities.instrument_search is True
    assert provider.capabilities.instrument_binding is True
    assert provider.capabilities.gap_repair is True
    assert provider.capabilities.exact_history_snapshot_authority is True
    assert provider.capabilities.canonical_futures_history is True
    assert provider.capabilities.live_quote_stream is False
    assert provider.capabilities.live_quote_polling is True
    assert provider.capabilities.trading_hours is True
    assert provider.capabilities.gex is False
    assert provider.capabilities.options is False
    assert provider.capabilities.read_only is True
    assert provider.data_policy.authoritative_ohlcv is True
    assert provider.data_policy.sparse_sessions is True
    assert isinstance(provider, QuotePollingProvider)
    assert isinstance(provider, TradingScheduleProvider)


def test_quote_polling_runtime_is_created_with_exact_process_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TInvestDataProvider()
    created_tokens: list[str | None] = []
    runtime = object()

    def create_runtime(token: str | None) -> object:
        created_tokens.append(token)
        return runtime

    monkeypatch.setattr(TInvestDataProvider, "_token", staticmethod(lambda: "secret"))
    monkeypatch.setattr(provider_module, "TInvestQuotePollingRuntime", create_runtime)

    assert provider.create_quote_polling_runtime() is runtime
    assert created_tokens == ["secret"]


def test_fetch_trading_schedule_uses_exact_uid_and_private_typed_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid="schedule-uid", asset_class="future")
        starts_at = datetime(2026, 8, 13, tzinfo=UTC)
        ends_at = starts_at + timedelta(days=7)
        services = SimpleNamespace(instruments=object())
        opened_tokens: list[str | None] = []
        fetched: list[tuple[Any, TInvestScheduleRequest, float]] = []
        expected = {
            "schedule_format": "provider_declared_intervals",
            "schedule_coverage_start": starts_at.isoformat(),
            "schedule_coverage_end": ends_at.isoformat(),
            "trading_intervals": [],
        }

        @asynccontextmanager
        async def open_services(token: str | None):
            opened_tokens.append(token)
            yield services

        async def fetch_schedule(
            actual_services: Any,
            request: TInvestScheduleRequest,
            *,
            timeout: float,
        ) -> dict[str, Any]:
            fetched.append((actual_services, request, timeout))
            return expected

        monkeypatch.setattr(TInvestDataProvider, "_token", staticmethod(lambda: "secret"))
        monkeypatch.setattr(provider_module, "open_tinvest_services", open_services)
        monkeypatch.setattr(provider_module, "fetch_tinvest_schedule", fetch_schedule)

        result = await provider.fetch_trading_schedule(
            instrument,
            timeout=2.5,
            starts_at=starts_at,
            ends_at=ends_at,
        )

        assert result is expected
        assert opened_tokens == ["secret"]
        assert len(fetched) == 1
        actual_services, request, timeout = fetched[0]
        assert actual_services is services
        assert request.instrument_uid == "schedule-uid"
        assert request.starts_at == starts_at
        assert request.ends_at == ends_at
        assert timeout == 2.5

    asyncio.run(run())


def test_fetch_trading_schedule_requires_both_exact_range_bounds() -> None:
    provider = TInvestDataProvider()
    instrument = _instrument(uid="schedule-range")

    with pytest.raises(ValueError, match="TINVEST_SCHEDULE_EXACT_RANGE_REQUIRED"):
        asyncio.run(
            provider.fetch_trading_schedule(
                instrument,
                starts_at=datetime(2026, 8, 13, tzinfo=UTC),
            )
        )


def _repair_intent(
    provider: TInvestDataProvider,
    instrument: dict[str, Any],
) -> HistoryRepairIntent:
    starts_at = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    return HistoryRepairIntent(
        provider="tinvest",
        instrument_id=instrument["instrument_id"],
        route_fingerprint=instrument["instrument_id"],
        timeframe="5m",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=5),
        request_identity=provider.history_request_identity(instrument),
        canonical_generation=0,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
    )


def test_canonical_history_route_distinguishes_stock_from_exact_future() -> None:
    provider = TInvestDataProvider()
    stock = _instrument(uid="stock-uid")
    future = _instrument(uid="future-uid", asset_class="future")

    assert provider.canonical_history_route(stock) is None
    future_route = provider.canonical_history_route(future)
    assert future_route is not None
    assert future_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTRACT
    assert future_route.contract_key == "future-uid"
    assert future_route.series_type == ""
    assert future_route.roll_policy == ""


@pytest.mark.parametrize(
    ("terminal", "expected_terminal", "expected_error"),
    (
        (
            TInvestHistoryTerminal.COMPLETE,
            ProviderHistoryTerminal.COMPLETE,
            None,
        ),
        (TInvestHistoryTerminal.INCOMPLETE, ProviderHistoryTerminal.INCOMPLETE, None),
        (
            TInvestHistoryTerminal.MALFORMED,
            ProviderHistoryTerminal.MALFORMED,
            "TINVEST_HISTORY_RESPONSE_INVALID",
        ),
    ),
)
def test_fetch_history_translates_private_terminal_without_storage(
    monkeypatch: pytest.MonkeyPatch,
    terminal: TInvestHistoryTerminal,
    expected_terminal: ProviderHistoryTerminal,
    expected_error: str | None,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid=f"repair-{terminal.value}")
        intent = _repair_intent(provider, instrument)
        chunk_request = TInvestHistoryChunkRequest(
            intent.starts_at,
            intent.ends_at,
        )
        chunk_result = TInvestHistoryChunkResult(
            request=chunk_request,
            terminal=terminal,
            response_count=0,
            authoritative_count=0,
            provisional_count=(1 if terminal is TInvestHistoryTerminal.INCOMPLETE else 0),
            error_code=(
                "TINVEST_HISTORY_RESPONSE_INVALID"
                if terminal is TInvestHistoryTerminal.MALFORMED
                else None
            ),
        )
        opened = 0

        @asynccontextmanager
        async def open_services(_token: str | None):
            nonlocal opened
            opened += 1
            yield SimpleNamespace(market_data=object())

        async def fetch_history(_services: Any, request: Any, *, timeout: float):
            assert request.starts_at == intent.starts_at
            assert request.ends_at == intent.ends_at
            assert timeout == 2.5
            return TInvestHistoryResult(
                request=request,
                terminal=terminal,
                authoritative_bars=(),
                provisional_bars=(),
                chunks=(chunk_result,),
                error_code=chunk_result.error_code,
            )

        monkeypatch.setattr(provider_module, "open_tinvest_services", open_services)
        monkeypatch.setattr(provider_module, "fetch_tinvest_history", fetch_history)

        result = await provider.async_fetch_history(
            intent,
            2.5,
            instrument=instrument,
        )

        assert opened == 1
        assert result.intent is intent
        assert result.terminal is expected_terminal
        if terminal is TInvestHistoryTerminal.COMPLETE:
            assert len(result.completions) == 1
            assert result.completions[0].starts_at == intent.starts_at
            assert result.completions[0].ends_at == intent.ends_at
            assert result.completions[0].response_count == 0
            assert result.completions[0].provider_limit is None
        else:
            assert result.completions == ()
        assert result.authoritative_bars == ()
        assert result.error_code == expected_error

    asyncio.run(run())


def test_final_returned_bar_is_preserved_with_bounded_range_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid="repair-final-bar")
        intent = _repair_intent(provider, instrument)
        bar = _bar(instrument, intent.starts_at)

        @asynccontextmanager
        async def open_services(_token: str | None):
            yield SimpleNamespace(market_data=object())

        async def fetch_history(_services: Any, request: Any, *, timeout: float):
            assert timeout == 2.5
            chunk_request = TInvestHistoryChunkRequest(
                intent.starts_at,
                intent.ends_at,
            )
            return TInvestHistoryResult(
                request=request,
                terminal=TInvestHistoryTerminal.COMPLETE,
                authoritative_bars=(bar,),
                provisional_bars=(),
                chunks=(
                    TInvestHistoryChunkResult(
                        request=chunk_request,
                        terminal=TInvestHistoryTerminal.COMPLETE,
                        response_count=1,
                        authoritative_count=1,
                        provisional_count=0,
                    ),
                ),
                error_code=None,
            )

        monkeypatch.setattr(provider_module, "open_tinvest_services", open_services)
        monkeypatch.setattr(provider_module, "fetch_tinvest_history", fetch_history)

        result = await provider.async_fetch_history(
            intent,
            2.5,
            instrument=instrument,
        )

        assert result.terminal is ProviderHistoryTerminal.COMPLETE
        assert result.authoritative_bars == (bar,)
        assert len(result.completions) == 1
        assert result.completions[0].response_count == 1
        assert result.completions[0].provider_limit is None
        assert result.error_code is None

    asyncio.run(run())


def test_complete_history_rejects_inconsistent_terminal_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid="repair-invalid-terminal")
        intent = _repair_intent(provider, instrument)

        @asynccontextmanager
        async def open_services(_token: str | None):
            yield SimpleNamespace(market_data=object())

        async def fetch_history(_services: Any, request: Any, *, timeout: float):
            assert timeout == 2.5
            return TInvestHistoryResult(
                request=request,
                terminal=TInvestHistoryTerminal.COMPLETE,
                authoritative_bars=(),
                provisional_bars=(),
                chunks=(
                    TInvestHistoryChunkResult(
                        request=TInvestHistoryChunkRequest(
                            intent.starts_at,
                            intent.ends_at,
                        ),
                        terminal=TInvestHistoryTerminal.COMPLETE,
                        response_count=1,
                        authoritative_count=0,
                        provisional_count=0,
                    ),
                ),
                error_code=None,
            )

        monkeypatch.setattr(provider_module, "open_tinvest_services", open_services)
        monkeypatch.setattr(provider_module, "fetch_tinvest_history", fetch_history)

        with pytest.raises(
            RuntimeError,
            match="TINVEST_HISTORY_TERMINAL_RESULT_INVALID",
        ):
            await provider.async_fetch_history(
                intent,
                2.5,
                instrument=instrument,
            )

    asyncio.run(run())


def test_load_bars_is_db_first_and_never_opens_provider_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.data import provider_history

    provider = TInvestDataProvider()
    instrument = _instrument(uid="db-first-uid")
    store = object()
    window = HistoryRangeWindow(
        range_key="31d",
        starts_at=datetime(2026, 7, 1, tzinfo=UTC),
        ends_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    expected = [_bar(instrument, datetime(2026, 7, 31, 12, 0, tzinfo=UTC))]
    reads: list[tuple[Any, ...]] = []

    def read_provider_history(
        actual_store: Any,
        actual_instrument: dict[str, Any],
        interval: str,
        range_: str,
        *,
        adapter: Any,
        window: HistoryRangeWindow,
    ) -> list[Bar]:
        assert adapter is provider
        reads.append((actual_store, actual_instrument, interval, range_, window))
        return expected

    def forbidden_transport(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB-first load must not open T-Invest transport")

    monkeypatch.setattr(provider_history, "read_provider_history", read_provider_history)
    monkeypatch.setattr(provider_module, "open_tinvest_services", forbidden_transport)
    monkeypatch.setattr(provider_module, "fetch_tinvest_history", forbidden_transport)

    bars, warning = provider.load_bars(
        instrument,
        "5m",
        "31d",
        timeout=0.1,
        live_refresh=True,
        store=store,
        window=window,
    )

    assert bars is expected
    assert warning == ""
    assert reads == [(store, instrument, "5m", "31d", window)]


@pytest.mark.parametrize(
    ("terminal", "include_provisional"),
    [
        (TInvestHistoryTerminal.COMPLETE, False),
        (TInvestHistoryTerminal.INCOMPLETE, True),
    ],
)
def test_unary_tail_preserves_typed_complete_and_incomplete_results(
    monkeypatch: pytest.MonkeyPatch,
    terminal: TInvestHistoryTerminal,
    include_provisional: bool,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid=f"tail-{terminal.value}")
        current_bucket = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
        authoritative = _bar(instrument, current_bucket - timedelta(minutes=5))
        provisional = _bar(
            instrument,
            current_bucket,
            confirmed=False,
        )
        fetches: list[tuple[Any, Any, float]] = []
        opened_tokens: list[str | None] = []

        @asynccontextmanager
        async def open_services(token: str | None):
            opened_tokens.append(token)
            yield SimpleNamespace(market_data=object())

        async def fetch_history(services: Any, request: Any, *, timeout: float):
            fetches.append((services, request, timeout))
            return SimpleNamespace(
                terminal=terminal,
                authoritative_bars=(authoritative,),
                provisional_bars=(provisional,) if include_provisional else (),
                error_code=None,
            )

        monkeypatch.setattr(TInvestDataProvider, "_token", staticmethod(lambda: "secret"))
        monkeypatch.setattr(provider_module, "interval_bucket", lambda *_args: current_bucket)
        monkeypatch.setattr(provider_module, "open_tinvest_services", open_services)
        monkeypatch.setattr(provider_module, "fetch_tinvest_history", fetch_history)

        bars = await provider.async_chart_live_bars(
            "5m",
            "1d",
            timeout=1.25,
            tail=12,
            instrument=instrument,
            consumer_id="chart:test",
        )

        assert opened_tokens == ["secret"]
        assert len(fetches) == 1
        _services, request, request_timeout = fetches[0]
        assert request.instrument_uid == instrument["provider_contract_id"]
        assert request.instrument_id == instrument["instrument_id"]
        assert request.starts_at == current_bucket - timedelta(minutes=55)
        assert request.ends_at == current_bucket + timedelta(minutes=5)
        assert request.starts_at <= provisional.ts < request.ends_at
        future_bucket = provisional.ts + timedelta(minutes=5)
        assert request.ends_at == future_bucket
        assert not request.starts_at <= future_bucket < request.ends_at
        assert request_timeout == 1.25
        assert bars == ([authoritative, provisional] if include_provisional else [authoritative])

    asyncio.run(run())


def test_unary_tail_rejects_typed_malformed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid="tail-malformed")

        @asynccontextmanager
        async def open_services(_token: str | None):
            yield SimpleNamespace(market_data=object())

        async def fetch_history(*_args: Any, **_kwargs: Any):
            return SimpleNamespace(
                terminal=TInvestHistoryTerminal.MALFORMED,
                authoritative_bars=(),
                provisional_bars=(),
                error_code="TINVEST_HISTORY_RESPONSE_INVALID",
            )

        monkeypatch.setattr(provider_module, "open_tinvest_services", open_services)
        monkeypatch.setattr(provider_module, "fetch_tinvest_history", fetch_history)

        with pytest.raises(RuntimeError, match="TINVEST_HISTORY_RESPONSE_INVALID"):
            await provider.async_chart_live_bars(
                "5m",
                "1d",
                instrument=instrument,
                consumer_id="chart:test",
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("asset_class", "expected_method"),
    [
        ("stock", "ordinary"),
        ("future", "futures_contract"),
    ],
)
def test_commit_dispatches_to_exact_ordinary_or_futures_storage(
    monkeypatch: pytest.MonkeyPatch,
    asset_class: str,
    expected_method: str,
) -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid=f"commit-{asset_class}", asset_class=asset_class)
        bar = _bar(instrument, datetime(2026, 8, 7, 12, 0, tzinfo=UTC))
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        class Store:
            def write_bars(self, *args: Any, **kwargs: Any) -> CanonicalBarCommitReceipt:
                calls.append(("ordinary", args, kwargs))
                return CanonicalBarCommitReceipt(
                    revision_sequence=kwargs["revision_sequence"],
                    written=1,
                    acknowledged=(bar,),
                )

            def write_futures_contract_bars(
                self,
                *args: Any,
                **kwargs: Any,
            ) -> CanonicalBarCommitReceipt:
                calls.append(("futures_contract", args, kwargs))
                return CanonicalBarCommitReceipt(
                    revision_sequence=kwargs["revision_sequence"],
                    written=1,
                    acknowledged=(bar,),
                )

        receipt = await provider.async_commit_chart_bars(
            [bar],
            interval="5m",
            store=Store(),
            instrument=instrument,
            revision_sequence=41,
        )

        assert receipt.acknowledged == (bar,)
        assert len(calls) == 1
        method, args, kwargs = calls[0]
        assert method == expected_method
        assert kwargs["revision_sequence"] == 41
        if expected_method == "ordinary":
            assert args[:2] == ([bar], "tinvest")
            assert kwargs["instrument"] == instrument
        else:
            assert args == ([bar],)
            assert kwargs == {
                "provider": "tinvest",
                "instrument_id": instrument["instrument_id"],
                "route_fingerprint": instrument["instrument_id"],
                "contract_key": instrument["provider_contract_id"],
                "provider_contract_id": instrument["provider_contract_id"],
                "provider_contract_type": "INSTRUMENT_UID",
                "data_type": "TRADES",
                "revision_sequence": 41,
            }

    asyncio.run(run())


def test_identical_commit_replay_returns_typed_noop_receipt() -> None:
    async def run() -> None:
        provider = TInvestDataProvider()
        instrument = _instrument(uid="commit-replay")
        bar = _bar(instrument, datetime(2026, 8, 7, 12, 0, tzinfo=UTC))

        class Store:
            def initialize(self) -> None:
                return None

            def write_bars(
                self,
                bars: list[Bar],
                actual_provider: str,
                **kwargs: Any,
            ) -> CanonicalBarCommitReceipt:
                assert bars == [bar]
                assert actual_provider == "tinvest"
                return CanonicalBarCommitReceipt(
                    revision_sequence=kwargs["revision_sequence"],
                    written=0,
                    acknowledged=(bar,),
                )

        receipt = await provider.async_commit_chart_bars(
            [bar],
            interval="5m",
            store=Store(),
            instrument=instrument,
            revision_sequence=42,
        )

        assert receipt.written == 0
        assert receipt.acknowledged == (bar,)

    asyncio.run(run())
