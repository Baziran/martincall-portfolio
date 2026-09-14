from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data.providers import (
    bind_provider_future_root,
    default_data_source,
    bind_provider_instrument,
    get_provider,
    live_quote_polling_providers,
    normalize_provider_key,
    provider_catalog,
    provider_data_policy_for_source,
    provider_db_providers,
    register_provider,
    route_instrument,
)
from aef_terminal.data.adapters.base import ProviderAdapterBase
from aef_terminal.data.provider_contract import (
    HistoryRepairIntent,
    HistoryRequestAdmissionIdentity,
    ProviderHistoryFetchResult,
    ProviderHistoryTerminal,
    ProviderCapabilities,
    ProviderDataPolicy,
    ProviderManifest,
    ProviderSessionScope,
)
from aef_terminal.domain import Bar, BarProviderRequest
from aef_terminal.engine import data_quality
from aef_terminal.engine.snapshot import assembly as snapshot_assembly
from aef_terminal.data.instrument_identity import (
    InstrumentIdentityError,
    provider_symbol,
    route_fingerprint,
)
from tests.provider_payloads import coinbase_btc_payload, ibkr_future_payload, ibkr_stock_payload
from aef_terminal.engine.snapshot import builder as snapshot_builder
from aef_terminal.engine.snapshot import chart_only as snapshot_chart_only
from aef_terminal.engine.snapshot import db_context as snapshot_db_context_module
from aef_terminal.engine.snapshot.db_context import snapshot_db_context
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
import aef_terminal.data.providers as providers_module
from aef_terminal.data.ibkr import search as ibkr_search
from aef_terminal.data.ibkr.search import ibkr_search_candidate
from aef_terminal.ui.reference_actions import (
    ReferenceActionDeps,
    search_reference_instruments_payload,
)


def _bar(symbol: str = "DEMO") -> Bar:
    return Bar(
        symbol=symbol,
        ts=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        timeframe="5m",
        source="demo:test",
        closed=True,
    )


def test_provider_registry_exposes_capabilities_and_db_providers() -> None:
    providers = {item["key"]: item for item in provider_catalog()}

    assert providers["ibkr"]["capabilities"]["gap_repair"] is True
    assert providers["ibkr"]["capabilities"]["exact_history_snapshot_authority"] is True
    assert providers["ibkr"]["capabilities"]["chart_stream"] is True
    assert providers["ibkr"]["capabilities"]["native_chart_stream"] is True
    assert providers["ibkr"]["capabilities"]["live_quote_stream"] is True
    assert providers["ibkr"]["capabilities"]["live_quote_polling"] is False
    assert providers["ibkr"]["capabilities"]["runtime_settings"] is True
    assert providers["ibkr"]["capabilities"]["instrument_search"] is True
    assert providers["ibkr"]["capabilities"]["instrument_binding"] is True
    assert providers["ibkr"]["db_providers"] == ["ibkr"]
    assert providers["ibkr"]["data_policy"]["tail_delay_grace_seconds"] == 0.0
    assert providers["ibkr"]["data_policy"]["authoritative_ohlcv"] is True
    assert providers["coinbase"]["capabilities"]["instrument_search"] is True
    assert providers["coinbase"]["capabilities"]["instrument_binding"] is True
    assert providers["coinbase"]["capabilities"]["chart_stream"] is True
    assert providers["coinbase"]["capabilities"]["native_chart_stream"] is False
    assert providers["coinbase"]["capabilities"]["chart_tail_polling"] is True
    assert providers["coinbase"]["capabilities"]["gap_repair"] is True
    assert providers["coinbase"]["capabilities"]["exact_history_snapshot_authority"] is True
    assert providers["coinbase"]["data_policy"]["chart_poll_seconds"] == 1.0
    assert providers["coinbase"]["data_policy"]["chart_request_timeout_seconds"] == 1.5
    assert providers["tinvest"]["capabilities"]["gap_repair"] is True
    assert providers["tinvest"]["capabilities"]["exact_history_snapshot_authority"] is True
    assert providers["tinvest"]["capabilities"]["live_quote_polling"] is True
    assert default_data_source() == "ibkr"
    with pytest.raises(ValueError, match="DATA_PROVIDER_REQUIRED"):
        normalize_provider_key("")
    assert set(live_quote_polling_providers()) == {"coinbase", "tinvest"}
    assert provider_data_policy_for_source("coinbase:BTC-USD").sparse_sessions is False
    with pytest.raises(ValueError, match="PROVIDER_SOURCE_UNKNOWN"):
        provider_data_policy_for_source("unknown:cache")


def test_instrument_route_projects_only_exact_provider_price_increment() -> None:
    stock = ibkr_stock_payload("SPY")
    stock["profile"] = "ES"
    stock["contract_identity"]["min_tick"] = 0.005
    assert route_instrument(stock).price_increment == 0.005

    future = ibkr_future_payload("ES")
    future["contract_identity"]["min_tick"] = 99.0
    future["contract_identity"]["current_contract"]["min_tick"] = 0.25
    assert route_instrument(future).price_increment == 0.25

    assert route_instrument(coinbase_btc_payload()).price_increment is None

    stock["contract_identity"]["min_tick"] = "0.01"
    with pytest.raises(
        InstrumentIdentityError,
        match=r"contract_identity\.min_tick must be an exact finite positive number",
    ):
        route_instrument(stock)


def test_unknown_provider_does_not_fallback_to_ibkr() -> None:
    with pytest.raises(ValueError, match="Unsupported data source"):
        get_provider("unknown-provider")

    with pytest.raises(ValueError, match="Unsupported data source"):
        route_instrument(
            {
                "instrument_id": "unknown-provider|contract|bad:1",
                "instrument_key": "BAD",
                "display": "BAD",
                "provider": "unknown-provider",
                "provider_symbol": "BAD",
                "provider_contract_id": "bad:1",
                "asset_class": "stock",
                "contract_identity": {
                    "provider": "unknown-provider",
                    "provider_contract_id": "bad:1",
                    "asset_class": "stock",
                },
            }
        )


def test_legacy_instrument_registry_is_removed() -> None:
    assert not Path("src/aef_terminal/engine/instrument_registry.py").exists()


def test_instrument_registry_rejects_implicit_identity_defaults() -> None:
    with pytest.raises(ValueError, match="INSTRUMENT_IDENTITY_INCOMPLETE"):
        provider_symbol({"key": "BAD"}, "ibkr")


def test_ibkr_search_candidate_uses_contract_description_identity() -> None:
    description = SimpleNamespace(
        contract=SimpleNamespace(
            symbol="RSU",
            secType="STK",
            exchange="SMART",
            primaryExchange="NASDAQ",
            currency="USD",
            conId=12345,
        ),
        longName="Research Solutions Inc",
    )

    candidate = ibkr_search_candidate(description)

    assert candidate is not None
    assert candidate["key"] == "RSU"
    assert candidate["provider"] == "ibkr"
    assert candidate["provider_symbol"] == "RSU"
    assert candidate["name"] == "Research Solutions Inc"
    assert candidate["contract_identity"]["sec_type"] == "STK"
    assert candidate["contract_identity"]["primary_exchange"] == "NASDAQ"
    assert candidate["contract_identity"]["con_id"] == 12345


def test_ibkr_search_dedupes_by_con_id_not_ticker(monkeypatch) -> None:
    descriptions = [
        SimpleNamespace(
            contract=SimpleNamespace(
                symbol="ABC",
                secType="STK",
                exchange="SMART",
                primaryExchange="NYSE",
                currency="USD",
                conId=1001,
            ),
            longName="ABC NYSE",
        ),
        SimpleNamespace(
            contract=SimpleNamespace(
                symbol="ABC",
                secType="STK",
                exchange="SMART",
                primaryExchange="NASDAQ",
                currency="USD",
                conId=1002,
            ),
            longName="ABC NASDAQ",
        ),
    ]

    class FakeIB:
        RequestTimeout = 0

        def reqMatchingSymbols(self, _query):
            return descriptions

        def isConnected(self):
            return False

        def disconnect(self):
            return None

    monkeypatch.setattr(ibkr_search, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(
        "aef_terminal.data.ibkr.session.connect_without_account_sync",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "ib_async", SimpleNamespace(IB=lambda: FakeIB()))

    results = ibkr_search.search_ibkr_instruments("ABC")

    assert [item["con_id"] for item in results] == [1001, 1002]
    assert [item["contract_identity"]["primary_exchange"] for item in results] == ["NYSE", "NASDAQ"]


def test_ibkr_search_includes_index_contracts(monkeypatch) -> None:
    descriptions = [
        SimpleNamespace(
            contract=SimpleNamespace(
                symbol="SPX",
                secType="IND",
                exchange="CBOE",
                primaryExchange="",
                currency="USD",
                conId=416904,
            ),
            longName="S&P 500 Index",
        )
    ]

    class FakeIB:
        RequestTimeout = 0

        def reqMatchingSymbols(self, _query):
            return descriptions

        def isConnected(self):
            return False

        def disconnect(self):
            return None

    monkeypatch.setattr(ibkr_search, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(
        "aef_terminal.data.ibkr.session.connect_without_account_sync",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "ib_async", SimpleNamespace(IB=lambda: FakeIB()))

    results = ibkr_search.search_ibkr_instruments("SPX")

    assert len(results) == 1
    assert results[0]["key"] == "SPX"
    assert results[0]["provider_contract_id"] == "416904"
    assert results[0]["asset_class"] == "index"
    assert results[0]["contract_identity"]["sec_type"] == "IND"


def test_ibkr_bind_instrument_requires_provider_contract_id() -> None:
    assert ibkr_search.bind_ibkr_instrument("RSU") is None
    assert ibkr_search.bind_ibkr_instrument(" 416904 ") is None
    source = inspect.getsource(ibkr_search.bind_ibkr_instrument)
    assert "reqContractDetails" in source
    assert "search_ibkr_instruments" not in source
    assert "_candidate_matches_key" not in inspect.getsource(ibkr_search)


def test_ibkr_search_includes_broker_discovered_future_roots(monkeypatch) -> None:
    descriptions = [
        SimpleNamespace(
            contract=SimpleNamespace(
                symbol="RTY",
                secType="FUT",
                exchange="CME",
                primaryExchange="",
                currency="USD",
                conId=98765,
                localSymbol="RTYU6",
                lastTradeDateOrContractMonth="20260918",
                tradingClass="RTY",
            ),
            longName="E-mini Russell 2000 futures",
        )
    ]

    class FakeIB:
        RequestTimeout = 0

        def reqMatchingSymbols(self, _query):
            return descriptions

        def isConnected(self):
            return False

        def disconnect(self):
            return None

    monkeypatch.setattr(ibkr_search, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(
        "aef_terminal.data.ibkr.session.connect_without_account_sync",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "ib_async", SimpleNamespace(IB=lambda: FakeIB()))

    results = ibkr_search.search_ibkr_instruments("RTY")

    assert len(results) == 1
    candidate = results[0]
    assert candidate is not None
    assert candidate["key"] == "RTY"
    assert candidate["provider_contract_id"] == ""
    assert candidate["contract_identity"]["identity_scope"] == "root"
    assert candidate["contract_identity"]["exchange"] == "CME"
    assert candidate["contract_identity"]["currency"] == "USD"
    assert candidate["contract_identity"]["con_id"] is None
    assert candidate["contract_identity"]["current_contract"]["con_id"] == 98765
    assert candidate["contract_identity"]["current_contract"]["local_symbol"] == "RTYU6"
    assert candidate["continuous_series"]["roll_source"] == "provider"


def test_ibkr_bind_future_root_uses_registered_root_spec_without_symbol_search(monkeypatch) -> None:
    contract = SimpleNamespace(
        symbol="ES",
        secType="FUT",
        exchange="CME",
        currency="USD",
        conId=649180671,
        localSymbol="ESU6",
        lastTradeDateOrContractMonth="20260918",
        tradingClass="ES",
    )

    class FakeIB:
        RequestTimeout = 0

        def isConnected(self):
            return False

        def disconnect(self):
            return None

        def reqMatchingSymbols(self, _query):
            raise AssertionError("registered IBKR futures roots must not depend on symbol search")

        def reqContractDetails(self, _contract):
            return [SimpleNamespace(minTick=0.25)]

    monkeypatch.setattr(ibkr_search, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(
        ibkr_search,
        "_discover_current_future_contract",
        lambda ib, instrument: contract,
    )
    monkeypatch.setattr(ibkr_search, "_parse_ibkr_expiry", lambda _expiry: None)
    monkeypatch.setattr(
        "aef_terminal.data.ibkr.session.connect_without_account_sync",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(__import__("sys").modules, "ib_async", SimpleNamespace(IB=lambda: FakeIB()))

    candidate = ibkr_search.bind_ibkr_future_root("ES", binding=ibkr_future_payload("ES"))

    assert candidate is not None
    assert candidate["key"] == "ES"
    assert candidate["contract_identity"]["identity_scope"] == "root"
    assert candidate["contract_identity"]["exchange"] == "CME"
    assert candidate["contract_identity"]["currency"] == "USD"
    assert candidate["contract_identity"]["current_contract"]["local_symbol"] == "ESU6"
    assert candidate["contract_identity"]["current_contract"]["provider_contract_id"] == "649180671"
    assert candidate["contract_identity"]["current_contract"]["min_tick"] == 0.25


def test_reference_search_keeps_future_root_when_query_matches_monthly_contract_symbol() -> None:
    future_root = {
        "instrument_id": "ibkr|future_root|RTY|CME|USD|",
        "key": "RTY",
        "instrument_key": "RTY",
        "display": "RTY",
        "name": "E-mini Russell 2000 futures",
        "provider": "ibkr",
        "provider_symbol": "RTY",
        "provider_contract_id": "",
        "asset_class": "future",
        "contract_identity": {
            "asset_class": "future",
            "provider": "ibkr",
            "root": "RTY",
            "exchange": "CME",
            "currency": "USD",
            "identity_scope": "root",
            "current_contract": {
                "contract_key": "RTYU6",
                "provider_contract_id": "98765",
                "local_symbol": "RTYU6",
                "con_id": 98765,
                "expiry": "20260918",
                "contract_month": "202609",
            },
        },
    }
    deps = ReferenceActionDeps(
        reconcile_client_settings=lambda *_args: None,
        reconcile_gex_scheduler_settings=lambda *_args: {},
        reconcile_option_target_caps_settings=lambda *_args: {},
        data_provider_catalog=lambda: [{"key": "ibkr"}],
        search_provider_instruments=lambda provider, query: [future_root],
        bind_provider_instrument=lambda provider, contract_id: None,
        bind_provider_future_root=lambda provider, root: None,
        store_factory=lambda: None,
        refresh_quote_routes=lambda _store, **_kwargs: None,
    )

    payload = search_reference_instruments_payload(deps, {"provider": "ibkr", "query": "RTYU6"})

    assert payload["ok"] is True
    assert payload["count"] == 1
    match = payload["matches"][0]
    assert match["instrument_key"] == "RTY"
    assert match["root"] == "RTY"
    assert match["identity_scope"] == "root"
    assert match["current_contract"]["local_symbol"] == "RTYU6"


def test_coinbase_provider_owned_binding_restores_watchlist_after_rebuild() -> None:
    coinbase_matches = providers_module.search_provider_instruments("coinbase", "BTC-USD")

    assert coinbase_matches[0]["key"] == "BTC"
    assert coinbase_matches[0]["provider_contract_id"] == "BTC-USD"
    assert providers_module.search_provider_instruments("coinbase", "BTC") == []
    assert providers_module.search_provider_instruments("coinbase", "BTC/USD") == []
    assert providers_module.search_provider_instruments("coinbase", "btc-usd") == []
    assert coinbase_matches[0]["session"] == {
        "provider": "coinbase",
        "calendar": "continuous_24_7",
        "family": "crypto",
        "timezone": "UTC",
    }
    assert (
        providers_module.bind_provider_instrument("coinbase", "BTC-USD")["provider_symbol"]
        == "BTC-USD"
    )
    assert providers_module.bind_provider_instrument("coinbase", "BTC") is None
    assert providers_module.bind_provider_instrument("coinbase", "BTC/USD") is None
    assert providers_module.bind_provider_instrument("coinbase", " BTC-USD ") is None
    assert providers_module.bind_provider_instrument("coinbase", "BTCUSD") is None
    assert providers_module.bind_provider_instrument("coinbase", "BTC/USD") is None
    assert providers_module.bind_provider_instrument("coinbase", "btc-usd") is None
    assert providers_module.bind_provider_instrument("coinbase", " BTC-USD ") is None
    renamed_coinbase_route = {
        **coinbase_matches[0],
        "key": "DISPLAY-ONLY",
        "instrument_key": "DISPLAY-ONLY",
        "display": "DISPLAY-ONLY",
    }
    assert (
        route_instrument(renamed_coinbase_route).fingerprint
        == route_instrument(coinbase_matches[0]).fingerprint
    )
    mismatched_coinbase_route = {
        **coinbase_matches[0],
        "contract_identity": {
            **coinbase_matches[0]["contract_identity"],
            "product_id": "ETH-USD",
        },
    }
    with pytest.raises(ValueError, match="PROVIDER_PRODUCT_ID_MISMATCH"):
        route_instrument(mismatched_coinbase_route)


def test_futures_instruments_expose_root_contract_and_continuous_series_boundaries() -> None:
    es = ibkr_future_payload("ES")
    assert es["instrument_key"] == "ES"
    assert es["contract_identity"] == {
        "asset_class": "future",
        "provider": "ibkr",
        "root": "ES",
        "exchange": "CME",
        "currency": "USD",
        "identity_scope": "root",
        "history_contract_mode": "continuous_future",
        "live_contract_mode": "provider_current_contract",
        "current_contract": es["contract_identity"]["current_contract"],
    }
    assert es["continuous_series"] == {
        "instrument_key": "ES",
        "provider": "ibkr",
        "provider_symbol": "ES",
        "series_type": "provider_bound_continuous",
        "roll_source": "provider",
    }

    crude = ibkr_future_payload("CL", exchange="NYMEX")
    assert crude["contract_identity"]["exchange"] == "NYMEX"
    assert crude["contract_identity"]["history_contract_mode"] == "continuous_future"
    assert crude["contract_identity"]["live_contract_mode"] == "provider_current_contract"


def test_snapshot_builder_uses_provider_adapter_contract() -> None:
    source = inspect.getsource(snapshot_builder)

    assert "load_provider_bars(" in source
    assert "async_load_provider_bars(" in source
    assert "async_load_ibkr_bars" not in source
    assert "load_ibkr_bars" not in source
    assert "async_load_moex_bars" not in source
    assert "load_moex_bars" not in source
    assert "async_ibkr" not in source


def test_virtual_three_minute_load_uses_one_minute_provider_source() -> None:
    calls: list[str] = []
    bars = [
        Bar(
            "BTC-USD",
            datetime(2026, 8, 8, 12, minute, tzinfo=UTC),
            100 + minute,
            101 + minute,
            99 + minute,
            100.5 + minute,
            1,
            "1m",
            "coinbase",
        )
        for minute in (0, 1, 2)
    ]

    class Adapter:
        async def async_load_bars(self, _instrument, interval, *_args, **_kwargs):
            calls.append(interval)
            return bars, ""

    route = SimpleNamespace(
        instrument={},
        adapter=Adapter(),
        provider="coinbase",
        instrument_id="coinbase|spot|BTC-USD",
        fingerprint="coinbase|spot|BTC-USD",
    )

    projected, warning = asyncio.run(
        providers_module.async_load_provider_bars(route, "3m", "5d", 1.0)
    )

    assert calls == ["1m"]
    assert warning == ""
    assert len(projected) == 1
    assert projected[0].timeframe == "3m"
    assert projected[0].open == bars[0].open
    assert projected[0].close == bars[-1].close


@pytest.mark.parametrize(
    ("result_factory", "error"),
    (
        (lambda bars: ([SimpleNamespace(ts=bars[0].ts)], ""), TypeError),
        (lambda bars: (bars, None), TypeError),
        (
            lambda bars: (
                [
                    Bar(
                        bars[0].symbol,
                        bars[0].ts,
                        bars[0].open,
                        bars[0].high,
                        bars[0].low,
                        bars[0].close,
                        bars[0].volume,
                        "1m",
                        bars[0].source,
                    )
                ],
                "",
            ),
            ValueError,
        ),
        (lambda bars: ([bars[1], bars[0]], ""), ValueError),
    ),
)
def test_provider_bar_load_rejects_malformed_adapter_results(
    result_factory,
    error: type[Exception],
) -> None:
    start = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    bars = [
        Bar("BTC-USD", start + timedelta(minutes=index * 5), 100, 101, 99, 100, 1, "5m")
        for index in range(2)
    ]

    class Adapter:
        def load_bars(self, *_args, **_kwargs):
            return result_factory(bars)

    route = SimpleNamespace(
        instrument={},
        adapter=Adapter(),
        provider="coinbase",
        instrument_id="coinbase|contract|BTC-USD",
        fingerprint="coinbase|contract|BTC-USD",
    )

    with pytest.raises(error):
        providers_module.load_provider_bars(route, "5m", "5d", 1.0)


def test_provider_bar_load_rejects_window_lookalikes_before_adapter_call() -> None:
    class Adapter:
        def load_bars(self, *_args, **_kwargs):
            raise AssertionError("malformed window must be rejected before provider work")

    route = SimpleNamespace(
        instrument={},
        adapter=Adapter(),
        provider="coinbase",
        instrument_id="coinbase|contract|BTC-USD",
        fingerprint="coinbase|contract|BTC-USD",
    )

    with pytest.raises(TypeError, match="HistoryRangeWindow"):
        providers_module.load_provider_bars(
            route,
            "5m",
            "5d",
            1.0,
            window=SimpleNamespace(range_key="5d"),
        )


def test_async_provider_bar_load_single_flight_is_scoped_to_exact_store() -> None:
    calls: list[object] = []
    release: asyncio.Event

    class Adapter:
        async def async_load_bars(
            self,
            _instrument,
            _interval,
            *_args,
            store=None,
            **_kwargs,
        ):
            calls.append(store)
            await release.wait()
            return [], str(id(store))

    route = SimpleNamespace(
        instrument={},
        adapter=Adapter(),
        provider="coinbase",
        instrument_id="coinbase|contract|BTC-USD",
        fingerprint="coinbase|contract|BTC-USD",
    )
    first_store = object()
    second_store = object()

    async def scenario() -> None:
        nonlocal release
        release = asyncio.Event()
        providers_module._INFLIGHT_BAR_LOADS.clear()
        first = asyncio.create_task(
            providers_module.async_load_provider_bars(
                route,
                "5m",
                "5d",
                1.0,
                store=first_store,
            )
        )
        second = asyncio.create_task(
            providers_module.async_load_provider_bars(
                route,
                "5m",
                "5d",
                1.0,
                store=second_store,
            )
        )
        for _ in range(10):
            if len(calls) == 2:
                break
            await asyncio.sleep(0)
        assert calls == [first_store, second_store]
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result[1] == str(id(first_store))
        assert second_result[1] == str(id(second_store))
        assert providers_module._INFLIGHT_BAR_LOADS == {}

    asyncio.run(scenario())


def test_async_provider_bar_load_single_flights_same_store() -> None:
    calls = 0
    release: asyncio.Event

    class Adapter:
        async def async_load_bars(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            await release.wait()
            return [], ""

    route = SimpleNamespace(
        instrument={},
        adapter=Adapter(),
        provider="coinbase",
        instrument_id="coinbase|contract|BTC-USD",
        fingerprint="coinbase|contract|BTC-USD",
    )
    store = object()

    async def scenario() -> None:
        nonlocal release
        release = asyncio.Event()
        providers_module._INFLIGHT_BAR_LOADS.clear()
        tasks = [
            asyncio.create_task(
                providers_module.async_load_provider_bars(
                    route,
                    "5m",
                    "5d",
                    1.0,
                    store=store,
                )
            )
            for _ in range(2)
        ]
        for _ in range(10):
            if calls == 1:
                break
            await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.gather(*tasks)
        assert providers_module._INFLIGHT_BAR_LOADS == {}

    asyncio.run(scenario())


def test_provider_bar_load_shutdown_drains_shielded_owner() -> None:
    started: asyncio.Event
    release: asyncio.Event

    class Adapter:
        async def async_load_bars(self, *_args, **_kwargs):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
                raise
            return [], ""

    route = SimpleNamespace(
        instrument={},
        adapter=Adapter(),
        provider="coinbase",
        instrument_id="coinbase|contract|BTC-USD",
        fingerprint="coinbase|contract|BTC-USD",
    )

    async def scenario() -> None:
        nonlocal started, release
        started = asyncio.Event()
        release = asyncio.Event()
        providers_module._INFLIGHT_BAR_LOADS.clear()
        providers_module.start_provider_bar_load_runtime()
        load = asyncio.create_task(
            providers_module.async_load_provider_bars(route, "5m", "5d", 1.0)
        )
        await started.wait()
        shutdown = asyncio.create_task(providers_module.shutdown_provider_bar_load_runtime())
        await asyncio.sleep(0)

        assert not shutdown.done()
        with pytest.raises(RuntimeError, match="PROVIDER_BAR_LOAD_RUNTIME_STOPPING"):
            await providers_module.async_load_provider_bars(route, "5m", "5d", 1.0)

        release.set()
        await shutdown
        await asyncio.gather(load, return_exceptions=True)
        assert providers_module._INFLIGHT_BAR_LOADS == {}
        providers_module.start_provider_bar_load_runtime()

    asyncio.run(scenario())


def test_provider_bar_load_contract_defaults_to_db_only() -> None:
    for provider_key in ("ibkr", "coinbase", "tinvest"):
        adapter = get_provider(provider_key)
        assert inspect.signature(adapter.load_bars).parameters["live_refresh"].default is False
        assert (
            inspect.signature(adapter.async_load_bars).parameters["live_refresh"].default is False
        )
    assert (
        inspect.signature(providers_module.async_load_provider_bars)
        .parameters["live_refresh"]
        .default
        is False
    )


def test_generic_snapshot_modules_use_provider_registry_defaults() -> None:
    for module in (
        snapshot_assembly,
        snapshot_builder,
        snapshot_chart_only,
        snapshot_db_context_module,
    ):
        source = inspect.getsource(module)
        assert "ibkr" not in source.lower()
        assert ' = "ibkr"' not in source
        assert ' or "ibkr"' not in source
        assert "default_data_source" in source or "normalize_provider_key" in source


def test_data_quality_sparse_sessions_are_provider_policy_driven() -> None:
    source = inspect.getsource(data_quality.sparse_session_source)

    assert "provider_data_policy_for_source" in source
    assert "MIX" not in source
    assert "SI" not in source
    assert "moex" not in source.lower()


def test_async_provider_loader_forwards_store_to_provider(monkeypatch) -> None:
    provider = get_provider("coinbase")
    store = object()
    captured: dict[str, Any] = {}

    async def fake_async_load_bars(*_args: Any, **kwargs: Any):
        captured.update(kwargs)
        return [], ""

    monkeypatch.setattr(provider, "async_load_bars", fake_async_load_bars)

    asyncio.run(
        route_instrument(coinbase_btc_payload()).adapter.async_load_bars(
            coinbase_btc_payload(),
            "5m",
            "1d",
            1.0,
            store=store,
        )
    )

    assert captured["store"] is store


def test_fake_provider_and_instrument_use_registry_contract(monkeypatch) -> None:
    class DemoProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="demo",
            name="Demo Provider",
            latency="test",
            status="test",
            requires=(),
            note="Registry conformance provider",
            db_providers=("demo",),
            source_prefixes=("demo:",),
            session_scope=ProviderSessionScope.INSTRUMENT,
            capabilities=ProviderCapabilities(
                live_quote_polling=True, gap_repair=True, read_only=True
            ),
            data_policy=ProviderDataPolicy(cache_ttl_seconds=7, sparse_sessions=True),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [_bar("DEMO_NATIVE")], ""

        async def async_load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [_bar("DEMO_NATIVE")], ""

        def create_quote_polling_runtime(self):
            provider_key = self.key

            class Runtime:
                async def fetch_quotes(
                    self, instruments: list[dict[str, Any]]
                ) -> tuple[dict[str, Any], str]:
                    return {
                        route_fingerprint(item): {"price": 123.0, "provider": provider_key}
                        for item in instruments
                    }, ""

                async def aclose(self) -> None:
                    return None

            return Runtime()

        def quote_close_base(
            self, instrument: dict[str, Any], quote: dict[str, Any]
        ) -> float | None:
            return float(quote["close"]) if quote.get("close") else None

        def history_session_scope(self, instrument: dict[str, Any]) -> str:
            _ = instrument
            return "trading"

        def search_instruments(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
            return [{"key": query, "provider": self.key}]

        def bind_instrument(self, provider_contract_id: str) -> dict[str, Any] | None:
            return {
                "key": "DEMO",
                "provider": self.key,
                "provider_contract_id": provider_contract_id,
            }

        def bind_future_root(
            self, root: str, binding: dict[str, Any] | None = None
        ) -> dict[str, Any] | None:
            _ = binding
            return {
                "key": root,
                "asset_class": "future",
                "contract_identity": {
                    "asset_class": "future",
                    "identity_scope": "root",
                    "root": root,
                },
            }

        def history_request_identity(
            self,
            instrument: dict[str, Any],
        ) -> HistoryRequestAdmissionIdentity:
            return HistoryRequestAdmissionIdentity(
                request_contract_version=1,
                admission_contract_version=1,
                request_mode="demo_history",
                request_type=BarProviderRequest.HISTORICAL,
                provider_source="EXCHANGE",
                provider_contract_id=instrument["provider_contract_id"],
                provider_contract_type="DEMO_ID",
                data_type="TRADES",
            )

        def history_request_max_span(
            self,
            instrument: dict[str, Any],
            timeframe: str,
        ) -> timedelta:
            _ = instrument, timeframe
            return timedelta(days=1)

        async def async_fetch_history(
            self,
            intent: HistoryRepairIntent,
            timeout: float,
            *,
            instrument: dict[str, Any],
        ) -> ProviderHistoryFetchResult:
            _ = timeout, instrument
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=ProviderHistoryTerminal.INCOMPLETE,
                authoritative_bars=(),
            )

    original_providers = dict(providers_module._PROVIDERS)
    original_source_index = dict(providers_module._SOURCE_PROVIDER_KEYS)
    try:
        register_provider(DemoProvider())
        instrument = {
            "instrument_id": "demo|contract|123",
            "key": "DEMO",
            "instrument_key": "DEMO",
            "display": "DEMO",
            "provider": "demo",
            "provider_symbol": "DEMO_NATIVE",
            "provider_contract_id": "123",
            "asset_class": "stock",
            "contract_identity": {
                "provider": "demo",
                "provider_contract_id": "123",
                "asset_class": "stock",
            },
        }
        assert provider_symbol(instrument, "demo") == "DEMO_NATIVE"
        assert "demo" in live_quote_polling_providers()
        assert provider_db_providers("demo") == ("demo",)
        assert provider_data_policy_for_source("demo:history").sparse_sessions is True
        route = route_instrument(instrument)
        assert route.provider_symbol == "DEMO_NATIVE"
        assert not hasattr(route, "symbol")
        opaque_route = route_instrument(
            {
                **instrument,
                "instrument_id": "demo|contract| opaque-id ",
                "provider_symbol": " opaque-route ",
                "provider_contract_id": " opaque-id ",
                "contract_identity": {
                    **instrument["contract_identity"],
                    "provider_contract_id": " opaque-id ",
                },
            }
        )
        assert opaque_route.instrument_id == "demo|contract| opaque-id "
        assert opaque_route.provider_symbol == " opaque-route "
        quote_runtime = route.adapter.create_quote_polling_runtime()
        quotes, warning = asyncio.run(quote_runtime.fetch_quotes([route.instrument]))
        asyncio.run(quote_runtime.aclose())
        assert warning == ""
        assert quotes[route.fingerprint]["provider"] == "demo"
        assert bind_provider_instrument("demo", "123") == {
            "key": "DEMO",
            "provider": "demo",
            "provider_contract_id": "123",
        }
        assert (
            bind_provider_future_root("demo", "DEF")["contract_identity"]["identity_scope"]
            == "root"
        )
        bars, warning = asyncio.run(
            route.adapter.async_load_bars(route.instrument, "5m", "1d", 1.0)
        )
        assert warning == ""
        assert bars[0].symbol == "DEMO_NATIVE"
        request_identity = route.adapter.history_request_identity(route.instrument)
        assert request_identity.request_mode == "demo_history"
        assert route.adapter.history_request_max_span(route.instrument, "5m") == timedelta(days=1)
    finally:
        providers_module._PROVIDERS.clear()
        providers_module._PROVIDERS.update(original_providers)
        providers_module._SOURCE_PROVIDER_KEYS.clear()
        providers_module._SOURCE_PROVIDER_KEYS.update(original_source_index)


def test_register_provider_rejects_declared_optional_contract_without_implementation() -> None:
    class BrokenGexProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="broken-gex",
            name="Broken GEX Provider",
            db_providers=("broken-gex",),
            capabilities=ProviderCapabilities(gex=True),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [], ""

        def search_instruments(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
            _ = limit
            return []

    with pytest.raises(TypeError, match="capability contract mismatch: gex"):
        register_provider(BrokenGexProvider())


def test_gap_repair_capability_requires_provider_owned_identity_bound_and_fetch() -> None:
    class BrokenGapProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="broken-gap",
            name="Broken Gap Provider",
            db_providers=("broken-gap",),
            session_scope=ProviderSessionScope.CONTINUOUS,
            capabilities=ProviderCapabilities(gap_repair=True),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [], ""

        def search_instruments(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
            _ = limit
            return []

        def history_request_identity(
            self,
            instrument: dict[str, Any],
        ) -> HistoryRequestAdmissionIdentity:
            _ = instrument
            raise NotImplementedError

    with pytest.raises(TypeError, match="capability contract mismatch: gap_repair"):
        register_provider(BrokenGapProvider())


def test_snapshot_authority_capability_requires_gap_repair_fetch() -> None:
    class BrokenSnapshotProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="broken-snapshot",
            name="Broken Snapshot Provider",
            db_providers=("broken-snapshot",),
            session_scope=ProviderSessionScope.CONTINUOUS,
            capabilities=ProviderCapabilities(
                exact_history_snapshot_authority=True,
            ),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [], ""

        def search_instruments(
            self,
            _query: str,
            limit: int = 20,
        ) -> list[dict[str, Any]]:
            _ = limit
            return []

    with pytest.raises(
        TypeError,
        match="capability contract mismatch: exact_history_snapshot_authority",
    ):
        register_provider(BrokenSnapshotProvider())


def test_register_provider_rejects_manifest_methods_inherited_from_default_adapter() -> None:
    class BrokenManifestProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="broken-manifest",
            name="Broken Manifest Provider",
            db_providers=("broken-manifest",),
            session_scope=ProviderSessionScope.CONTINUOUS,
            capabilities=ProviderCapabilities(
                instrument_search=True,
                instrument_binding=True,
                canonical_futures_history=True,
            ),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [], ""

        def search_instruments(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
            _ = limit
            return []

    with pytest.raises(
        TypeError,
        match="instrument_binding.*canonical_futures_history",
    ):
        register_provider(BrokenManifestProvider())


def test_instrument_session_provider_requires_owned_history_scope() -> None:
    class BrokenSessionProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="broken-session",
            name="Broken Session Provider",
            db_providers=("broken-session",),
            session_scope=ProviderSessionScope.INSTRUMENT,
            capabilities=ProviderCapabilities(),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [], ""

        def search_instruments(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
            _ = limit
            return []

    with pytest.raises(
        TypeError,
        match="capability contract mismatch: history_session_scope",
    ):
        register_provider(BrokenSessionProvider())


def test_failed_provider_registration_does_not_mutate_registry_or_source_index() -> None:
    class CollidingProvider(ProviderAdapterBase):
        manifest = ProviderManifest(
            key="colliding-provider",
            name="Colliding Provider",
            db_providers=("ibkr",),
            session_scope=ProviderSessionScope.CONTINUOUS,
            capabilities=ProviderCapabilities(),
            data_policy=ProviderDataPolicy(),
        )

        def load_bars(self, *_args: Any, **_kwargs: Any) -> tuple[list[Bar], str]:
            return [], ""

        def search_instruments(self, _query: str, limit: int = 20) -> list[dict[str, Any]]:
            _ = limit
            return []

    providers_before = dict(providers_module._PROVIDERS)
    source_index_before = dict(providers_module._SOURCE_PROVIDER_KEYS)

    with pytest.raises(ValueError, match="PROVIDER_SOURCE_COLLISION"):
        register_provider(CollidingProvider())

    assert providers_module._PROVIDERS == providers_before
    assert providers_module._SOURCE_PROVIDER_KEYS == source_index_before


def test_duplicate_provider_registration_is_rejected_without_mutation() -> None:
    providers_before = dict(providers_module._PROVIDERS)
    source_index_before = dict(providers_module._SOURCE_PROVIDER_KEYS)

    with pytest.raises(ValueError, match="DATA_PROVIDER_ALREADY_REGISTERED"):
        register_provider(providers_module.get_provider("ibkr"))

    assert providers_module._PROVIDERS == providers_before
    assert providers_module._SOURCE_PROVIDER_KEYS == source_index_before


def test_snapshot_db_context_uses_requested_provider_keys() -> None:
    class Store:
        providers: tuple[str, ...] | None = None

        def get_full_snapshot_context(self, **kwargs):
            self.providers = tuple(kwargs["providers"])
            return {
                "bars": list(kwargs["bars"]),
                "bar_slots": ProviderBarSlotSequence([10], schedule_state="continuous"),
                "mtf_context": {},
                "mtf_context_slots": {},
                "mtf_context_quality": {},
            }

    store = Store()
    snapshot_bars, slots, mtf, mtf_slots, mtf_quality = snapshot_db_context(
        store,
        "BTC-USD",
        "5m",
        "1d",
        [_bar()],
        provider_key="coinbase",
        instrument=coinbase_btc_payload(),
    )

    assert store.providers == ("coinbase",)
    assert snapshot_bars == [_bar()]
    assert slots == [10]
    assert mtf == {}
    assert mtf_slots == {}
    assert mtf_quality == {}


def test_snapshot_db_context_preserves_unknown_provider_axis_contract() -> None:
    class Store:
        def get_full_snapshot_context(self, **_kwargs):
            return {
                "bars": [_bar()],
                "bar_slots": None,
                "mtf_context": {},
                "mtf_context_slots": {},
                "mtf_context_quality": {},
            }

    (
        snapshot_bars,
        bar_slots,
        mtf_context,
        mtf_context_slots,
        mtf_quality,
    ) = snapshot_db_context(
        Store(),
        "BTC-USD",
        "5m",
        "1d",
        [_bar()],
        provider_key="coinbase",
        instrument=coinbase_btc_payload(),
    )

    assert snapshot_bars == [_bar()]
    assert bar_slots is None
    assert mtf_context == {}
    assert mtf_context_slots == {}
    assert mtf_quality == {}


def test_snapshot_db_context_rejects_untyped_storage_slot_axis() -> None:
    class Store:
        def get_full_snapshot_context(self, **_kwargs):
            return {
                "bars": [_bar()],
                "bar_slots": [10],
                "mtf_context": {},
                "mtf_context_slots": {},
                "mtf_context_quality": {},
            }

    with pytest.raises(TypeError, match="ProviderBarSlotSequence"):
        snapshot_db_context(
            Store(),
            "BTC-USD",
            "5m",
            "1d",
            [_bar()],
            provider_key="coinbase",
            instrument=coinbase_btc_payload(),
        )


def test_provider_canonical_futures_history_is_capability_owned() -> None:
    ibkr = get_provider("ibkr")
    assert ibkr.capabilities.canonical_futures_history is True

    ibkr_future = ibkr_future_payload("ES")
    ibkr_stock = ibkr_stock_payload("AAPL")
    assert route_instrument(ibkr_future).adapter.canonical_history_route(ibkr_future) is not None
    assert route_instrument(ibkr_stock).adapter.canonical_history_route(ibkr_stock) is None


def test_public_providers_advertise_only_implemented_exact_range_repair() -> None:
    for provider_key in ("ibkr", "coinbase"):
        adapter = get_provider(provider_key)
        assert adapter.capabilities.gap_repair is True
        assert "history_request_identity" in type(adapter).__dict__
        assert "history_request_max_span" in type(adapter).__dict__
        assert "async_fetch_history" in type(adapter).__dict__
        assert "history_repair_candidate" not in type(adapter).__dict__
        assert "_async_repair_history" not in type(adapter).__dict__
    assert get_provider("ibkr").capabilities.exact_history_snapshot_authority is True
    assert get_provider("coinbase").capabilities.exact_history_snapshot_authority is True
    assert get_provider("tinvest").capabilities.exact_history_snapshot_authority is True
