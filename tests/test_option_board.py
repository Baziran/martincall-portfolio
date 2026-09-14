from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aef_terminal.data.ibkr import option_acquisition
from aef_terminal.data.ibkr import options as ibkr_options
from aef_terminal.data.adapters.ibkr import IbkrDataProvider
from aef_terminal.data.ibkr.option_contracts import (
    IbkrOptionContract,
    IbkrOptionMarketDataSource,
    IbkrOptionSeriesExpiryFact,
)
from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import (
    QuoteSnapshotRead,
    OptionUniverseUnavailableError,
)
from aef_terminal.domain import OptionRight
from aef_terminal.ui.services import option_board_stream_ws, option_target_reprice
from tests.provider_payloads import ibkr_stock_payload


def _option_contract(
    *,
    con_id: int,
    right: OptionRight,
    strike: float = 600.0,
    expiry: str = "20991231",
    expiry_at: datetime | None = None,
) -> IbkrOptionContract:
    exact_expiry_at = expiry_at or datetime(2099, 12, 31, 21, 0, tzinfo=UTC)
    raw = SimpleNamespace(
        conId=con_id,
        secType="OPT",
        symbol="SPY",
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right.value,
        multiplier="100",
        tradingClass="SPY",
        exchange="SMART",
        currency="USD",
        localSymbol=f"SPY {right.value}{strike:g}",
    )
    return IbkrOptionContract(
        raw=raw,
        con_id=con_id,
        sec_type="OPT",
        symbol="SPY",
        expiry=expiry,
        expiry_at=exact_expiry_at,
        strike=strike,
        right=right,
        multiplier=100.0,
        trading_class="SPY",
        exchange="SMART",
        currency="USD",
        local_symbol=raw.localSymbol,
    )


def _ticker(
    contract: IbkrOptionContract,
    *,
    bid: float,
    ask: float,
    last: float,
) -> SimpleNamespace:
    observed_at = datetime.now(tz=UTC)
    return SimpleNamespace(
        contract=contract.raw,
        tickerId=contract.con_id,
        bid=bid,
        ask=ask,
        last=last,
        close=last - 0.1,
        bidSize=4,
        askSize=5,
        lastSize=2,
        time=observed_at,
        lastTimestamp=observed_at,
        marketDataType=1,
        marketPrice=lambda: (bid + ask) / 2.0,
    )


def test_option_board_delivery_fails_closed_at_exact_expiry() -> None:
    expiry_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    contract = _option_contract(
        con_id=1001,
        right=OptionRight.CALL,
        expiry="20260729",
        expiry_at=expiry_at,
    )

    with pytest.raises(OptionUniverseUnavailableError) as caught:
        ibkr_options._option_board_payload(
            instrument_id="ibkr|stock|756733",
            route_key="route-v1",
            provider_symbol="SPY",
            consumer_id="option-board:test",
            generation=1,
            spot=600.0,
            expiry_mode="hybrid",
            contracts=(contract,),
            quote_rows=[{"bid": 1.0, "ask": 1.1, "last": 1.05}],
            chain_meta={},
            physical_subscription_source="option-quote-pool",
            opened_subscription_count=1,
        )

    assert caught.value.reason == "OPTION_UNIVERSE_ROLLOVER"
    assert caught.value.diagnostics["expired_contracts_excluded"] == 1


def test_option_board_carries_preloaded_provider_expiry_facts(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    fact = IbkrOptionSeriesExpiryFact(
        expiry="20991231",
        trading_class="SPY",
        exchange="SMART",
        multiplier=100.0,
        expiry_at=datetime(2099, 12, 31, 21, 0, tzinfo=UTC),
        contract_count=2,
    )
    observed: list[tuple[IbkrOptionSeriesExpiryFact, ...]] = []

    async def fake_owned(_instrument, **kwargs):
        observed.append(kwargs["option_expiry_facts"])
        return {"ok": True}

    async def fake_run(_lane, _label, factory, **_kwargs):
        return await factory()

    monkeypatch.setattr(
        ibkr_options,
        "_option_board_snapshot_owned",
        fake_owned,
    )
    monkeypatch.setattr(
        ibkr_options.ibkr_market_data_manager,
        "run_coroutine",
        fake_run,
    )

    result = asyncio.run(
        ibkr_options.async_option_board_snapshot(
            instrument,
            consumer_id="option-board:test",
            spot=600.0,
            option_expiry_facts=(fact,),
        )
    )

    assert result == {"ok": True}
    assert observed == [(fact,)]


def test_option_board_spot_contract_accepts_signed_futures_only() -> None:
    assert ibkr_options._require_option_board_spot(-10.0, futures_options=True) == -10.0
    assert ibkr_options._require_option_board_spot(0.0, futures_options=True) == 0.0
    with pytest.raises(ValueError, match="OPTION_BOARD_SPOT_INVALID"):
        ibkr_options._require_option_board_spot(-10.0, futures_options=False)


def test_option_board_provider_spot_reference_accepts_exact_previous_close(
    monkeypatch,
) -> None:
    underlying = SimpleNamespace(conId=416904, secType="IND", symbol="SPX")
    ticker = SimpleNamespace(
        contract=underlying,
        bid=-1.0,
        ask=-1.0,
        last=-1.0,
        close=6485.25,
        bidSize=None,
        askSize=None,
        lastSize=None,
        time=None,
        lastTimestamp=None,
        marketDataType=1,
        marketPrice=lambda: float("nan"),
    )
    requests: list[tuple[object, str, bool, bool]] = []

    class Transport:
        wrapper = SimpleNamespace(tickerIdMap={})

        def cancelMktData(self, _contract):
            raise AssertionError("completed snapshot must not be cancelled again")

    def request(_ib, contract, generic_ticks, snapshot, regulatory_snapshot):
        requests.append((contract, generic_ticks, snapshot, regulatory_snapshot))
        return ticker

    monkeypatch.setattr(ibkr_options, "request_ibkr_market_data_ticker", request)

    result = asyncio.run(
        ibkr_options._option_board_provider_spot_reference(
            Transport(),
            underlying,
            futures_options=False,
            timeout=1.0,
        )
    )

    assert result == (6485.25, "previous_close", "live", "reference")
    assert requests == [(underlying, "", True, False)]


def test_option_board_underlying_reference_never_labels_old_last_as_current() -> None:
    now = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
    base = {
        "price_source": "last",
        "market_data_entitlement": "live",
    }

    assert (
        ibkr_options._option_board_spot_reference_state(
            {**base, "last_provider_ts": (now - timedelta(seconds=2)).isoformat()},
            now=now,
        )
        == "current"
    )
    assert (
        ibkr_options._option_board_spot_reference_state(
            {**base, "last_provider_ts": (now - timedelta(minutes=1)).isoformat()},
            now=now,
        )
        == "reference"
    )
    assert (
        ibkr_options._option_board_spot_reference_state(
            {**base, "price_source": "previous_close", "last_provider_ts": now.isoformat()},
            now=now,
        )
        == "reference"
    )


def test_ibkr_option_board_falls_back_to_provider_snapshot_when_watchlist_quote_is_absent(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPX", con_id=416904)
    provider = IbkrDataProvider()
    observed: list[dict[str, object]] = []

    async def snapshot(_instrument, **kwargs):
        observed.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(provider, "cached_quotes", lambda _routes: QuoteSnapshotRead(0, {}))
    monkeypatch.setattr(ibkr_options, "async_option_board_snapshot", snapshot)

    result = asyncio.run(
        provider.async_load_option_board(
            instrument,
            consumer_id="option-board:spx",
            expiry_mode="0dte",
            option_expiry_facts=(),
        )
    )

    assert result == {"ok": True}
    assert observed[0]["spot"] is None
    assert observed[0]["spot_price_source"] == ""
    assert observed[0]["spot_market_data_entitlement"] == "unknown"
    assert observed[0]["spot_reference_state"] == "reference"


def test_option_point_qualification_carries_persisted_provider_expiry_facts(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    store = object()
    fact = IbkrOptionSeriesExpiryFact(
        expiry="20991231",
        trading_class="SPY",
        exchange="SMART",
        multiplier=100.0,
        expiry_at=datetime(2099, 12, 31, 21, 0, tzinfo=UTC),
        contract_count=2,
    )
    observed: list[tuple[IbkrOptionSeriesExpiryFact, ...]] = []

    def persisted_facts(**kwargs):
        if kwargs["store"] is not store:
            raise AssertionError("Option Point used the wrong store")
        return (fact,)

    monkeypatch.setattr(
        ibkr_options,
        "read_persisted_ibkr_option_expiry_facts",
        persisted_facts,
    )

    async def fake_qualification(_instrument, **kwargs):
        observed.append(kwargs["option_expiry_facts"])
        return {"ok": True}

    monkeypatch.setattr(
        ibkr_options,
        "_qualified_option_contract_universe_async",
        fake_qualification,
    )
    monkeypatch.setattr(
        ibkr_options.ibkr_market_data_manager,
        "run_coroutine_blocking",
        lambda _lane, _label, factory, **_kwargs: asyncio.run(factory()),
    )

    result = ibkr_options.option_contract_universe(
        instrument,
        spot=600.0,
        dte="0dte",
        store=store,
    )

    assert result == {"ok": True}
    assert observed == [(fact,)]


def test_option_board_reuses_exact_live_gex_tickers_without_opening_lines(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    call = _option_contract(con_id=1001, right=OptionRight.CALL)
    put = _option_contract(con_id=1002, right=OptionRight.PUT)

    class Transport:
        def isConnected(self) -> bool:
            return True

    source = IbkrOptionMarketDataSource(
        source_id="live-gex",
        instrument_id=instrument_id,
        route_fingerprint=route_key,
        provider_symbol="SPY",
        expiry_mode="hybrid",
        contracts=(call, put),
        tickers=(
            _ticker(call, bid=4.9, ask=5.1, last=5.0),
            _ticker(put, bid=3.9, ask=4.1, last=4.0),
        ),
        chain_meta={
            "expirations": "20991231",
            "qualified_strike_count": 1,
            "qualified_pair_count": 1,
        },
        transport=Transport(),
        generation="live-gex:test",
    )
    runtime = ibkr_options._IBKR_RUNTIME
    runtime.async_option_market_data_sources.clear()
    runtime.register_option_market_data_source(source)
    monkeypatch.setattr(
        ibkr_options,
        "_ensure_option_board_subscription_owned",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("shared live source must not open board subscriptions")
        ),
    )

    try:
        snapshot = asyncio.run(
            ibkr_options._option_board_snapshot_owned(
                instrument,
                consumer_id="option-board:test",
                spot=600.25,
                expiry_mode="hybrid",
                host="127.0.0.1",
                port=4002,
                client_id=3517,
                readonly=True,
                timeout=1.0,
            )
        )
    finally:
        runtime.unregister_option_market_data_source(source)

    assert snapshot["physical_subscription_source"] == "live-gex"
    assert snapshot["opened_subscription_count"] == 0
    assert snapshot["reused_subscription_count"] == 2
    assert snapshot["status"] == "ready"
    assert snapshot["market_data_entitlement"] == "live"
    rows = snapshot["series"][0]["rows"]
    assert snapshot["series"][0]["target_dte"] == "1dte"
    assert len(rows) == 1
    assert rows[0]["strike"] == 600.0
    assert rows[0]["call"]["con_id"] == 1001
    assert rows[0]["call"]["mid"] == 5.0
    assert rows[0]["put"]["con_id"] == 1002
    assert rows[0]["put"]["mid"] == 4.0
    exact_fields = (
        "con_id",
        "expiry",
        "expiry_at",
        "strike",
        "right",
        "multiplier",
        "trading_class",
        "exchange",
        "currency",
        "local_symbol",
    )
    assert {field: rows[0]["call"][field] for field in exact_fields} == {
        "con_id": 1001,
        "expiry": "20991231",
        "expiry_at": "2099-12-31T21:00:00+00:00",
        "strike": 600.0,
        "right": "C",
        "multiplier": 100.0,
        "trading_class": "SPY",
        "exchange": "SMART",
        "currency": "USD",
        "local_symbol": "SPY C600",
    }


@pytest.mark.parametrize(
    "expiry_at",
    (
        datetime.now(tz=UTC) - timedelta(seconds=1),
        None,
    ),
)
def test_option_board_bypasses_stale_shared_gex_source_for_own_qualification(
    monkeypatch,
    expiry_at,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    call = _option_contract(
        con_id=1001,
        right=OptionRight.CALL,
        expiry="20260728",
        expiry_at=expiry_at,
    )
    if expiry_at is None:
        call = IbkrOptionContract(
            **{
                **call.__dict__,
                "expiry_at": None,
            }
        )

    class Transport:
        def isConnected(self) -> bool:
            return True

    source = IbkrOptionMarketDataSource(
        source_id="live-gex:stale",
        instrument_id=instrument_id,
        route_fingerprint=route_key,
        provider_symbol="SPY",
        expiry_mode="hybrid",
        contracts=(call,),
        tickers=(_ticker(call, bid=4.9, ask=5.1, last=5.0),),
        chain_meta={},
        transport=Transport(),
        generation="live-gex:stale",
    )
    own_builds: list[str] = []

    async def own_builder(*_args, **_kwargs):
        own_builds.append("called")
        raise RuntimeError("own option-board qualification reached")

    runtime = ibkr_options._IBKR_RUNTIME
    runtime.async_option_market_data_sources.clear()
    runtime.register_option_market_data_source(source)
    monkeypatch.setattr(
        ibkr_options,
        "_ensure_option_board_subscription_owned",
        own_builder,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="own option-board qualification reached",
        ):
            asyncio.run(
                ibkr_options._option_board_snapshot_owned(
                    instrument,
                    consumer_id="option-board:test",
                    spot=600.25,
                    expiry_mode="hybrid",
                    host="127.0.0.1",
                    port=4002,
                    client_id=3517,
                    readonly=True,
                    timeout=1.0,
                )
            )
    finally:
        runtime.unregister_option_market_data_source(source)

    assert own_builds == ["called"]


def test_option_board_series_publishes_typed_option_point_dte() -> None:
    expiry_at = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    call = _option_contract(
        con_id=1001,
        right=OptionRight.CALL,
        expiry="20260821",
        expiry_at=expiry_at,
    )
    put = _option_contract(
        con_id=1002,
        right=OptionRight.PUT,
        expiry="20260821",
        expiry_at=expiry_at,
    )
    quotes = [
        ibkr_options._option_board_quote_row(
            call,
            _ticker(call, bid=4.9, ask=5.1, last=5.0),
        ),
        ibkr_options._option_board_quote_row(
            put,
            _ticker(put, bid=3.9, ask=4.1, last=4.0),
        ),
    ]

    same_day = ibkr_options._option_board_series(
        (call, put),
        quotes,
        valuation_now=datetime(2026, 8, 21, 14, 0, tzinfo=UTC),
    )
    prior_day = ibkr_options._option_board_series(
        (call, put),
        quotes,
        valuation_now=datetime(2026, 8, 20, 14, 0, tzinfo=UTC),
    )

    assert same_day[0]["target_dte"] == "0dte"
    assert prior_day[0]["target_dte"] == "1dte"
    with pytest.raises(
        OptionUniverseUnavailableError,
        match="no active Option Point DTE",
    ):
        ibkr_options._option_board_series(
            (call, put),
            quotes,
            valuation_now=expiry_at,
        )


def test_option_board_requires_rebase_at_exact_expiry() -> None:
    expiry_at = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    contracts = tuple(
        _option_contract(
            con_id=index,
            right=right,
            strike=strike,
            expiry="20260821",
            expiry_at=expiry_at,
        )
        for index, (strike, right) in enumerate(
            (
                (590.0, OptionRight.CALL),
                (590.0, OptionRight.PUT),
                (595.0, OptionRight.CALL),
                (595.0, OptionRight.PUT),
                (600.0, OptionRight.CALL),
                (600.0, OptionRight.PUT),
                (605.0, OptionRight.CALL),
                (605.0, OptionRight.PUT),
                (610.0, OptionRight.CALL),
                (610.0, OptionRight.PUT),
            ),
            start=1,
        )
    )
    subscription = ibkr_options.IbkrOptionBoardSubscription(
        key=("instrument", "route", "option-board:test"),
        consumer_id="option-board:test",
        instrument_id="instrument",
        route_fingerprint="route",
        provider_symbol="SPY",
        expiry_mode="hybrid",
        spot=600.0,
        contracts=contracts,
        cache_keys=(),
        chain_meta={},
        opened_subscription_count=0,
    )

    assert (
        ibkr_options._option_board_requires_rebase(
            subscription,
            600.0,
            now=expiry_at - timedelta(seconds=1),
        )
        is False
    )
    assert (
        ibkr_options._option_board_requires_rebase(
            subscription,
            600.0,
            now=expiry_at,
        )
        is True
    )


def test_option_board_consumer_release_preserves_option_point_subscription() -> None:
    cancellations: list[int] = []
    contract = SimpleNamespace(
        conId=1001,
        secType="OPT",
        exchange="SMART",
        localSymbol="SPY C600",
    )
    ticker = SimpleNamespace(contract=contract, tickerId=1001)
    contract_key = ibkr_options._option_quote_contract_key(
        {"con_id": 1001, "exchange": "SMART", "sec_type": "OPT"}
    )
    cache_key = ("127.0.0.1", 4002, 3517, True, contract_key)

    class Transport:
        wrapper = SimpleNamespace(tickerIdMap={1001: ticker})

        def isConnected(self) -> bool:
            return True

        def cancelMktData(self, cancelled_contract) -> bool:
            cancellations.append(cancelled_contract.conId)
            return True

        def disconnect(self) -> None:
            return None

    runtime = ibkr_options._IBKR_RUNTIME
    ibkr_options._reset_ibkr_async_option_quote_session()
    runtime.async_option_quote_session = Transport()
    runtime.async_option_quote_session_key = cache_key[:4]
    runtime.async_option_ticker_cache[cache_key] = ticker
    runtime.async_option_quote_contract_cache[cache_key] = contract
    runtime.async_option_ticker_consumers[cache_key] = {
        ibkr_options.OPTION_POINT_QUOTE_CONSUMER_ID,
        "option-board:test",
    }

    try:
        assert (
            ibkr_options._release_option_ticker_consumer_key(
                cache_key,
                "option-board:test",
            )
            is False
        )
        assert cache_key in runtime.async_option_ticker_cache
        assert cancellations == []

        assert (
            ibkr_options._release_option_ticker_consumer_key(
                cache_key,
                ibkr_options.OPTION_POINT_QUOTE_CONSUMER_ID,
            )
            is True
        )
        assert cache_key not in runtime.async_option_ticker_cache
        assert cancellations == [1001]
    finally:
        ibkr_options._reset_ibkr_async_option_quote_session()


def test_option_point_consumer_reconciliation_releases_only_stale_points() -> None:
    active_contract = SimpleNamespace(conId=1001)
    stale_contract = SimpleNamespace(conId=1002)
    active_ticker = SimpleNamespace(contract=active_contract, tickerId=1001)
    stale_ticker = SimpleNamespace(contract=stale_contract, tickerId=1002)
    active_key = ("127.0.0.1", 4002, 3517, True, "OPT:1001:SMART")
    stale_key = ("127.0.0.1", 4002, 3517, True, "OPT:1002:SMART")
    cancellations: list[int] = []

    class Transport:
        wrapper = SimpleNamespace(tickerIdMap={1001: active_ticker, 1002: stale_ticker})

        def isConnected(self) -> bool:
            return True

        def cancelMktData(self, contract) -> bool:
            cancellations.append(contract.conId)
            return True

        def disconnect(self) -> None:
            return None

    runtime = ibkr_options._IBKR_RUNTIME
    ibkr_options._reset_ibkr_async_option_quote_session()
    runtime.async_option_quote_session = Transport()
    runtime.async_option_quote_session_key = active_key[:4]
    runtime.async_option_ticker_cache.update({active_key: active_ticker, stale_key: stale_ticker})
    runtime.async_option_quote_contract_cache.update(
        {active_key: active_contract, stale_key: stale_contract}
    )
    runtime.async_option_ticker_consumers.update(
        {
            active_key: {"option-point:active", "option-board:test"},
            stale_key: {"option-point:stale"},
        }
    )

    try:
        result = asyncio.run(
            ibkr_options._reconcile_option_point_quote_consumers_owned({"option-point:active"})
        )
        assert result == {"ok": True, "active": 1, "released": 1, "cancelled": 1}
        assert runtime.async_option_ticker_consumers[active_key] == {
            "option-point:active",
            "option-board:test",
        }
        assert stale_key not in runtime.async_option_ticker_consumers
        assert cancellations == [1002]
    finally:
        ibkr_options._reset_ibkr_async_option_quote_session()


def test_option_target_reconciliation_derives_consumers_from_storage(monkeypatch) -> None:
    observed = []

    class Adapter:
        key = "ibkr"

        def reconcile_option_quote_consumers(self, consumers):
            observed.append(set(consumers))
            return {"ok": True, "active": len(consumers)}

    monkeypatch.setattr(
        option_target_reprice,
        "option_provider_adapters",
        lambda: (Adapter(),),
    )

    result = option_target_reprice.reconcile_option_target_quote_consumers(
        [
            {"id": "row-id", "payload": {"provider": "ibkr", "id": "point-a"}},
            {"id": "point-b", "payload": {"provider": "ibkr"}},
            {"id": "ignored", "payload": {"provider": "unknown"}},
        ]
    )

    assert result == [{"ok": True, "active": 2}]
    assert observed == [{"option-point:point-a", "option-point:point-b"}]


def test_option_universe_absence_is_a_typed_provider_fact() -> None:
    with pytest.raises(OptionUniverseUnavailableError):
        option_acquisition._selected_chain_expirations(
            [],
            required_exchange="SMART",
            max_expirations=1,
        )


def test_option_board_stream_first_cold_frame_is_typed_and_route_scoped(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    messages: list[dict] = []
    sleeping = False
    adapter_calls = 0

    class Adapter:
        capabilities = SimpleNamespace(options=True)

        async def async_load_option_board(self, *_args, **_kwargs):
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("cold loading status must publish before provider acquisition")

        async def async_stop_option_board(self, *_args, **_kwargs):
            return {"ok": True}

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=instrument_id,
        provider="ibkr",
        provider_symbol="SPY",
        fingerprint="route-v1",
    )

    class WebSocket:
        accepted = False

        async def accept(self) -> None:
            self.accepted = True

    async def capture(_websocket, payload, _stream_name) -> None:
        nonlocal sleeping
        messages.append(dict(payload))
        sleeping = True

    monkeypatch.setattr(
        option_board_stream_ws,
        "OPTION_BOARD_STREAM_FRAME_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        option_board_stream_ws,
        "route_instrument",
        lambda _instrument: route,
    )
    monkeypatch.setattr(option_board_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
        websocket = WebSocket()
        await option_board_stream_ws.run_option_board_stream(
            websocket,
            instrument_id=instrument_id,
            expected_route_fingerprint="route-v1",
            expiry_mode="hybrid",
            deps=option_board_stream_ws.OptionBoardWsDeps(
                apply_provider_runtime_settings_async=lambda: asyncio.sleep(
                    0,
                    result={},
                ),
                server_sleeping=lambda: sleeping,
                websocket_heartbeat_seconds=30.0,
                lookup_runtime_instrument=lambda _instrument_id: instrument,
            ),
        )
        assert websocket.accepted is True

    asyncio.run(scenario())

    assert messages[0] == {
        "type": "option_board_status",
        "ok": True,
        "status": "loading",
        "message": "Qualifying the exact provider option universe.",
        "retryable": True,
        "provider_symbol": "SPY",
        "instrument_id": instrument_id,
        "route_fingerprint": "route-v1",
        "expiry_mode": "hybrid",
        "status_revision": 1,
    }
    assert adapter_calls == 0
    assert option_board_stream_ws._OPTION_BOARD_PRODUCERS == {}


def test_option_board_stream_coalesces_consumers_and_stops_once(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    stops: list[str] = []
    loads: list[str] = []

    class Adapter:
        capabilities = SimpleNamespace(options=True)

        async def async_load_option_board(
            self,
            _instrument,
            *,
            consumer_id,
            expiry_mode,
        ):
            loads.append(consumer_id)
            return {
                "ok": True,
                "status": "ready",
                "instrument_id": instrument_id,
                "route_fingerprint": "route-v1",
                "provider_symbol": "SPY",
                "consumer_id": consumer_id,
                "expiry_mode": expiry_mode,
                "series": [],
            }

        async def async_stop_option_board(
            self,
            _instrument,
            *,
            consumer_id,
        ):
            stops.append(consumer_id)
            return {"ok": True}

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=instrument_id,
        provider="ibkr",
        provider_symbol="SPY",
        fingerprint="route-v1",
    )
    deps = option_board_stream_ws.OptionBoardWsDeps(
        apply_provider_runtime_settings_async=lambda: asyncio.sleep(
            0,
            result={},
        ),
        server_sleeping=lambda: False,
        websocket_heartbeat_seconds=30.0,
        lookup_runtime_instrument=lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(
        option_board_stream_ws,
        "OPTION_BOARD_STREAM_FRAME_SECONDS",
        0.001,
    )

    async def scenario() -> None:
        option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
        first = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        second = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        assert first is second
        assert first.consumers == 2
        for _ in range(100):
            if first.snapshot is not None:
                break
            await asyncio.sleep(0.001)
        assert first.snapshot is not None

        await option_board_stream_ws._release_option_board_producer(first)
        assert stops == []
        await option_board_stream_ws._release_option_board_producer(second)

    asyncio.run(scenario())

    assert loads
    assert len(stops) == 1
    assert option_board_stream_ws._OPTION_BOARD_PRODUCERS == {}


def test_option_board_stream_uses_typed_unavailable_status(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)

    class Adapter:
        capabilities = SimpleNamespace(options=True)

        async def async_load_option_board(self, *_args, **_kwargs):
            raise OptionUniverseUnavailableError(
                "listed expiry is absent",
                reason="OPTION_UNIVERSE_ROLLOVER",
                diagnostics={"expired_contracts_excluded": 2},
            )

        async def async_stop_option_board(self, *_args, **_kwargs):
            return {"ok": True}

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=instrument_id,
        provider="ibkr",
        provider_symbol="SPY",
        fingerprint="route-v1",
    )
    deps = option_board_stream_ws.OptionBoardWsDeps(
        apply_provider_runtime_settings_async=lambda: asyncio.sleep(
            0,
            result={},
        ),
        server_sleeping=lambda: False,
        websocket_heartbeat_seconds=30.0,
        lookup_runtime_instrument=lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(
        option_board_stream_ws,
        "OPTION_BOARD_STREAM_FRAME_SECONDS",
        0.001,
    )

    async def scenario() -> dict:
        option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
        producer = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        for _ in range(100):
            if producer.status.get("status") == "unavailable":
                break
            await asyncio.sleep(0.001)
        status = dict(producer.status)
        await option_board_stream_ws._release_option_board_producer(producer)
        return status

    status = asyncio.run(scenario())

    assert status["status"] == "unavailable"
    assert status["ok"] is False
    assert status["message"] == "listed expiry is absent"
    assert status["reason"] == "OPTION_UNIVERSE_ROLLOVER"
    assert status["diagnostics"] == {"expired_contracts_excluded": 2}
    assert datetime.fromisoformat(status["retry_at"]) > datetime.now(tz=UTC)


def test_option_board_reconnect_during_cleanup_restarts_one_generation() -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()
    stops: list[str] = []

    class Adapter:
        capabilities = SimpleNamespace(options=True)

        async def async_load_option_board(
            self,
            _instrument,
            *,
            consumer_id,
            expiry_mode,
        ):
            return {
                "ok": True,
                "status": "ready",
                "instrument_id": instrument_id,
                "route_fingerprint": "route-v1",
                "provider_symbol": "SPY",
                "consumer_id": consumer_id,
                "expiry_mode": expiry_mode,
                "series": [],
            }

        async def async_stop_option_board(
            self,
            _instrument,
            *,
            consumer_id,
        ):
            stops.append(consumer_id)
            stop_started.set()
            await allow_stop.wait()
            return {"ok": True}

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=instrument_id,
        provider="ibkr",
        provider_symbol="SPY",
        fingerprint="route-v1",
    )
    deps = option_board_stream_ws.OptionBoardWsDeps(
        apply_provider_runtime_settings_async=lambda: asyncio.sleep(
            0,
            result={},
        ),
        server_sleeping=lambda: False,
        websocket_heartbeat_seconds=30.0,
        lookup_runtime_instrument=lambda _instrument_id: instrument,
    )

    async def scenario() -> None:
        option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
        first = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        release_task = asyncio.create_task(
            option_board_stream_ws._release_option_board_producer(first)
        )
        await asyncio.wait_for(stop_started.wait(), timeout=0.5)

        reconnected = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        assert reconnected is first
        assert first.closing is True

        allow_stop.set()
        await release_task
        assert first.closing is False
        assert first.consumers == 1
        assert first.task is not None and not first.task.done()
        assert option_board_stream_ws._OPTION_BOARD_PRODUCERS[first.key] is first

        await option_board_stream_ws._release_option_board_producer(reconnected)

    asyncio.run(scenario())

    assert len(stops) == 2


def test_option_board_release_defers_cancellation_through_provider_stop() -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()

    class Adapter:
        capabilities = SimpleNamespace(options=True)

        async def async_load_option_board(
            self,
            _instrument,
            *,
            consumer_id,
            expiry_mode,
        ):
            return {
                "ok": True,
                "status": "ready",
                "instrument_id": instrument_id,
                "route_fingerprint": "route-v1",
                "provider_symbol": "SPY",
                "consumer_id": consumer_id,
                "expiry_mode": expiry_mode,
                "series": [],
            }

        async def async_stop_option_board(
            self,
            _instrument,
            *,
            consumer_id: str,
        ):
            assert consumer_id
            stop_started.set()
            await allow_stop.wait()
            return {"ok": True}

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=instrument_id,
        provider="ibkr",
        provider_symbol="SPY",
        fingerprint="route-v1",
    )
    deps = option_board_stream_ws.OptionBoardWsDeps(
        apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
        server_sleeping=lambda: False,
        websocket_heartbeat_seconds=30.0,
        lookup_runtime_instrument=lambda _instrument_id: instrument,
    )

    async def scenario() -> None:
        option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
        producer = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        release_task = asyncio.create_task(
            option_board_stream_ws._release_option_board_producer(producer)
        )
        try:
            await asyncio.wait_for(stop_started.wait(), timeout=0.5)
            release_task.cancel()
            await asyncio.sleep(0)

            assert not release_task.done()
            assert producer.closing is True
        finally:
            allow_stop.set()

        with pytest.raises(asyncio.CancelledError):
            await release_task
        assert producer.closing is False
        assert producer.task is None
        assert option_board_stream_ws._OPTION_BOARD_PRODUCERS == {}

    asyncio.run(scenario())


def test_option_board_release_waits_for_expiry_fact_read_worker(monkeypatch) -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument_id = qualified_instrument_id(instrument)
    read_started = threading.Event()
    allow_read = threading.Event()
    read_finished = threading.Event()
    loads: list[str] = []
    stops: list[str] = []

    def blocking_read(**_kwargs):
        read_started.set()
        allow_read.wait(timeout=2.0)
        read_finished.set()
        return ()

    monkeypatch.setattr(
        "aef_terminal.data.ibkr.option_contracts.read_persisted_ibkr_option_expiry_facts",
        blocking_read,
    )

    class Adapter:
        capabilities = SimpleNamespace(options=True)

        async def async_load_option_board(self, *_args, **_kwargs):
            loads.append("loaded")
            raise AssertionError("cancelled producer must not start provider acquisition")

        async def async_stop_option_board(
            self,
            _instrument,
            *,
            consumer_id: str,
        ):
            stops.append(consumer_id)
            return {"ok": True}

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=instrument_id,
        provider="ibkr",
        provider_symbol="SPY",
        fingerprint="route-v1",
    )
    deps = option_board_stream_ws.OptionBoardWsDeps(
        apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
        server_sleeping=lambda: False,
        websocket_heartbeat_seconds=30.0,
        lookup_runtime_instrument=lambda _instrument_id: instrument,
        store_factory=object,
    )

    async def scenario() -> None:
        option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
        producer = option_board_stream_ws._acquire_option_board_producer(
            route=route,
            expiry_mode="hybrid",
            deps=deps,
        )
        assert await asyncio.to_thread(read_started.wait, 0.5)
        release_task = asyncio.create_task(
            option_board_stream_ws._release_option_board_producer(producer)
        )
        try:
            await asyncio.sleep(0)
            assert not release_task.done()
        finally:
            allow_read.set()
        await release_task

        assert producer.task is None
        assert option_board_stream_ws._OPTION_BOARD_PRODUCERS == {}

    try:
        asyncio.run(scenario())
    finally:
        allow_read.set()

    assert read_finished.is_set()
    assert loads == []
    assert len(stops) == 1
