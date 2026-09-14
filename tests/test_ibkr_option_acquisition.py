from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import aef_terminal.data.ibkr.gex as ibkr_gex
import aef_terminal.data.ibkr.option_acquisition as ibkr_option_acquisition
from aef_terminal.data.gex.live import _live_request
from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
from aef_terminal.data.ibkr.option_contracts import (
    IbkrOptionSeriesExpiryFact,
    read_persisted_ibkr_option_expiry_facts,
)
from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    route_fingerprint,
)
from tests.ibkr_option_test_support import ibkr_option as _ibkr_option
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


_ES = ibkr_future_payload("ES")
_SPY = ibkr_stock_payload("SPY", con_id=756733)


def _instrument_id(instrument: dict[str, Any]) -> str:
    return qualified_instrument_id(instrument)


def _route_key(instrument: dict[str, Any]) -> str:
    return route_fingerprint(instrument)


@pytest.fixture(autouse=True)
def _isolate_option_qualification_caches():
    ibkr_option_acquisition._IBKR_OPTION_CHAIN_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_SERIES_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_EXPIRY_FACT_CACHE.clear()
    yield
    ibkr_option_acquisition._IBKR_OPTION_CHAIN_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_SERIES_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_EXPIRY_FACT_CACHE.clear()


def test_gex_live_contract_bootstrap_uses_async_ib_methods(monkeypatch) -> None:
    async def fake_current_future_async(_ib, *, instrument):
        return SimpleNamespace(
            symbol=instrument["provider_symbol"],
            secType="FUT",
            exchange="CME",
            currency="EUR",
            conId=1001,
        )

    monkeypatch.setattr(
        ibkr_option_acquisition, "_current_future_contract_async", fake_current_future_async
    )

    class FakeIb:
        def qualifyContracts(self, *_args):
            raise AssertionError("live GEX must not call sync qualifyContracts")

        def reqSecDefOptParams(self, *_args):
            raise AssertionError("live GEX must not call sync reqSecDefOptParams")

        async def qualifyContractsAsync(self, *_contracts):
            raise AssertionError("exact option series must not call qualifyContractsAsync")

        async def reqSecDefOptParamsAsync(self, *_args):
            return [
                SimpleNamespace(
                    exchange="CME",
                    tradingClass="ES",
                    multiplier="50",
                    expirations=["20990717", "20990720"],
                    strikes=[7520, 7540, 7560, 7580, 7600, 7620, 7640, 7660, 7680],
                )
            ]

        async def reqContractDetailsAsync(self, query):
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(
                        conId=(int(query.lastTradeDateOrContractMonth[-2:]) * 10_000)
                        + index
                        + (1_000 if right == "C" else 2_000),
                        symbol="ES",
                        secType="FOP",
                        lastTradeDateOrContractMonth=query.lastTradeDateOrContractMonth,
                        strike=float(strike),
                        right=right,
                        exchange="CME",
                        tradingClass="ES",
                        multiplier="50",
                        currency=query.currency,
                    ),
                    lastTradeTime="16:00",
                    timeZoneId="America/Chicago",
                )
                for index, strike in enumerate(
                    (7520, 7540, 7560, 7580, 7600, 7620, 7640, 7660, 7680)
                )
                for right in ("C", "P")
            ]

    async def run() -> None:
        ib = FakeIb()
        underlying = await ibkr_option_acquisition._qualified_live_underlying(
            ib,
            instrument=ibkr_future_payload("ES"),
        )
        request = _live_request(
            instrument=ibkr_future_payload("ES"),
            provider_runtime=ibkr_gex_provider_runtime,
        )
        contracts, meta = await ibkr_option_acquisition._build_live_option_contracts(
            ib,
            "ES",
            underlying,
            7572.0,
            request,
        )
        assert len(contracts) == 36
        assert meta["qualified_strike_count"] == 9
        assert meta["qualified_pair_count"] == 18
        assert meta["qualified_contracts"] == 36
        assert {contract.currency for contract in contracts} == {"EUR"}

    asyncio.run(run())


def test_gex_request_hybrid_replaces_provider_confirmed_expired_series_before_selection(
    monkeypatch,
) -> None:
    request_now = datetime(2026, 7, 29, 3, 57, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return request_now if tz is None else request_now.astimezone(tz)

    monkeypatch.setattr(ibkr_gex, "datetime", FrozenDateTime)
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E4B",
        multiplier="50",
        expirations=["20260728", "20260729", "20260730"],
        strikes=[7405.0],
    )
    requested_expiries: list[str] = []

    def option_details(query):
        expiry = str(query.lastTradeDateOrContractMonth)
        requested_expiries.append(expiry)
        return [
            SimpleNamespace(
                contract=SimpleNamespace(
                    conId=int(expiry[-2:]) * 10 + index,
                    symbol="ES",
                    secType="FOP",
                    lastTradeDateOrContractMonth=expiry,
                    strike=7405.0,
                    right=right,
                    exchange="CME",
                    tradingClass="E4B",
                    multiplier="50",
                    currency="USD",
                ),
                realExpirationDate=expiry,
                lastTradeTime="15:00",
                timeZoneId="America/Chicago",
            )
            for index, right in enumerate(("C", "P"), start=1)
        ]

    class FakeIb:
        def reqSecDefOptParams(self, *_args):
            return [chain]

        def reqContractDetails(self, query):
            return option_details(query)

    underlying = SimpleNamespace(
        conId=101,
        symbol="ES",
        secType="FUT",
        exchange="CME",
        currency="USD",
    )
    request = replace(
        ibkr_gex_provider_runtime.request_config(instrument=_ES, mode="manual"),
        strike_count=1,
        max_expirations=2,
        max_contracts=4,
    )

    contracts, meta = ibkr_gex._build_option_contracts(
        FakeIb(),
        "ES",
        underlying,
        7405.0,
        request,
    )

    assert {contract.expiry for contract in contracts} == {
        "20260729",
        "20260730",
    }
    assert requested_expiries == ["20260728", "20260729", "20260730"]
    assert meta["requested_expiries"] == [
        "20260728",
        "20260729",
        "20260730",
    ]
    assert meta["selected_expiries"] == ["20260729", "20260730"]
    assert meta["expired_contracts_excluded"] == 2
    assert meta["expired_series_excluded"] == 1
    assert meta["qualified_contracts"] == 4
    assert meta["qualified_pair_count"] == 2


def test_gex_contract_builder_revalidates_fresh_clock_after_provider_io(
    monkeypatch,
) -> None:
    before_expiry = datetime(2026, 7, 28, 19, 59, 59, tzinfo=UTC)
    exact_expiry = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)

    class BoundaryDateTime(datetime):
        current = before_expiry

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(ibkr_gex, "datetime", BoundaryDateTime)
    chain = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260728"],
        strikes=[600.0],
    )

    class FakeIb:
        def reqSecDefOptParams(self, *_args):
            return [chain]

        def reqContractDetails(self, query):
            BoundaryDateTime.current = exact_expiry
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(
                        conId=index,
                        symbol="SPY",
                        secType="OPT",
                        lastTradeDateOrContractMonth="20260728",
                        strike=600.0,
                        right=right,
                        exchange="SMART",
                        tradingClass="SPY",
                        multiplier="100",
                        currency="USD",
                    ),
                    realExpirationDate="20260728",
                    lastTradeTime="16:00",
                    timeZoneId="America/New_York",
                )
                for index, right in enumerate(("C", "P"), start=1)
            ]

    request = replace(
        ibkr_gex_provider_runtime.request_config(instrument=_SPY, mode="manual"),
        strike_count=1,
        max_expirations=1,
        max_contracts=2,
    )
    underlying = SimpleNamespace(
        conId=756733,
        symbol="SPY",
        secType="STK",
        exchange="SMART",
        currency="USD",
    )

    with pytest.raises(ibkr_gex.IbkrOptionUniverseUnavailableError) as caught:
        ibkr_gex._build_option_contracts(
            FakeIb(),
            "SPY",
            underlying,
            600.0,
            request,
        )

    assert caught.value.reason == "OPTION_UNIVERSE_ROLLOVER"
    assert caught.value.diagnostics["expired_contracts_excluded"] == 2


def test_gex_live_hybrid_reuses_confirmed_expiry_fact_across_details_cache_boundary(
    monkeypatch,
) -> None:
    before_expiry = datetime(2026, 7, 28, 19, 59, tzinfo=UTC)
    after_expiry = datetime(2026, 7, 29, 3, 57, tzinfo=UTC)

    class FrozenDateTime(datetime):
        current = before_expiry

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(
        ibkr_option_acquisition,
        "datetime",
        FrozenDateTime,
    )
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E4B",
        multiplier="50",
        expirations=["20260728", "20260729", "20260730"],
        strikes=[7405.0],
    )
    post_boundary = False
    expired_exact_lookups = 0

    class FakeIb:
        async def reqSecDefOptParamsAsync(self, *_args):
            return [chain]

        async def reqContractDetailsAsync(self, query):
            nonlocal expired_exact_lookups
            expiry = str(query.lastTradeDateOrContractMonth)
            exact_lookup = int(getattr(query, "conId", 0) or 0) > 0
            if post_boundary and expiry == "20260728" and exact_lookup:
                expired_exact_lookups += 1
            if post_boundary and expiry == "20260728":
                return []
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(
                        conId=int(expiry[-2:]) * 10 + index,
                        symbol="ES",
                        secType="FOP",
                        lastTradeDateOrContractMonth=expiry,
                        strike=7405.0,
                        right=right,
                        exchange="CME",
                        tradingClass="E4B",
                        multiplier="50",
                        currency="USD",
                    ),
                    realExpirationDate=expiry,
                    lastTradeTime="15:00",
                    timeZoneId="America/Chicago",
                    liquidHours="",
                    tradingHours="",
                )
                for index, right in enumerate(("C", "P"), start=1)
            ]

    underlying = SimpleNamespace(
        conId=101,
        symbol="ES",
        secType="FUT",
        exchange="CME",
        currency="USD",
    )
    request = replace(
        _live_request(instrument=_ES, provider_runtime=ibkr_gex_provider_runtime),
        strike_count=1,
        max_expirations=2,
        max_contracts=4,
    )

    async def run() -> None:
        nonlocal post_boundary
        ib = FakeIb()
        seeded, _meta = await ibkr_option_acquisition._build_live_option_contracts(
            ib,
            "ES",
            underlying,
            7405.0,
            request,
        )
        assert {contract.expiry for contract in seeded} == {
            "20260728",
            "20260729",
        }

        ibkr_option_acquisition._IBKR_OPTION_SERIES_CACHE.clear()
        post_boundary = True
        FrozenDateTime.current = after_expiry
        active, meta = await ibkr_option_acquisition._build_live_option_contracts(
            ib,
            "ES",
            underlying,
            7405.0,
            request,
        )
        assert {contract.expiry for contract in active} == {
            "20260729",
            "20260730",
        }
        assert meta["selected_expiries"] == ["20260729", "20260730"]
        assert meta["expired_contracts_excluded"] == 2
        assert meta["expired_series_excluded"] == 1

        strict_request = replace(
            request,
            expiry_mode="0dte",
            max_expirations=1,
            max_contracts=2,
        )
        with pytest.raises(ibkr_gex.IbkrOptionUniverseUnavailableError) as caught:
            await ibkr_option_acquisition._build_live_option_contracts(
                ib,
                "ES",
                underlying,
                7405.0,
                strict_request,
            )
        assert caught.value.reason == "NO_ACTIVE_0DTE"

    asyncio.run(run())
    assert expired_exact_lookups == 0


def test_ibkr_option_expiry_facts_survive_process_cache_loss_and_skip_expired_series(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 29, 3, 57, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    class Store:
        def read_gex_option_expiry_facts(
            self,
            instrument_id,
            route_fingerprint,
        ):
            assert instrument_id == _instrument_id(_ES)
            assert route_fingerprint == _route_key(_ES)
            return [
                {
                    "expiry": "20260728",
                    "trading_class": "E4B",
                    "exchange": "CME",
                    "multiplier": "50",
                    "expiry_at_values": ["2026-07-28T20:00:00+00:00"],
                    "contract_count": 2,
                }
            ]

    store = Store()
    expiry_facts = read_persisted_ibkr_option_expiry_facts(
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        store=store,
    )
    assert expiry_facts == (
        IbkrOptionSeriesExpiryFact(
            expiry="20260728",
            trading_class="E4B",
            exchange="CME",
            multiplier=50.0,
            expiry_at=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
            contract_count=2,
        ),
    )

    monkeypatch.setattr(
        ibkr_option_acquisition,
        "datetime",
        FrozenDateTime,
    )
    requested_expiries: list[str] = []
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E4B",
        multiplier="50",
        expirations=["20260728", "20260729", "20260730"],
        strikes=[7405.0],
    )

    class FakeIb:
        async def reqSecDefOptParamsAsync(self, *_args):
            return [chain]

        async def reqContractDetailsAsync(self, query):
            expiry = str(query.lastTradeDateOrContractMonth)
            requested_expiries.append(expiry)
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(
                        conId=int(expiry[-2:]) * 10 + index,
                        symbol="ES",
                        secType="FOP",
                        lastTradeDateOrContractMonth=expiry,
                        strike=7405.0,
                        right=right,
                        exchange="CME",
                        tradingClass="E4B",
                        multiplier="50",
                        currency="USD",
                    ),
                    realExpirationDate=expiry,
                    lastTradeTime="15:00",
                    timeZoneId="America/Chicago",
                    liquidHours="",
                    tradingHours="",
                )
                for index, right in enumerate(("C", "P"), start=1)
            ]

    request = replace(
        _live_request(instrument=_ES, provider_runtime=ibkr_gex_provider_runtime),
        strike_count=1,
        max_expirations=2,
        max_contracts=4,
        option_expiry_facts=expiry_facts,
    )
    contracts, meta = asyncio.run(
        ibkr_option_acquisition._build_live_option_contracts(
            FakeIb(),
            "ES",
            SimpleNamespace(
                conId=101,
                symbol="ES",
                secType="FUT",
                exchange="CME",
                currency="USD",
            ),
            7405.0,
            request,
        )
    )

    assert "20260728" not in requested_expiries
    assert {contract.expiry for contract in contracts} == {
        "20260729",
        "20260730",
    }
    assert meta["expired_series_excluded"] == 1
    assert meta["expired_contracts_excluded"] == 2


def test_ibkr_option_expiry_fact_storage_conflicts_fail_closed() -> None:
    class Store:
        def initialize(self) -> None:
            return None

        def read_gex_option_expiry_facts(self, *_args):
            return [
                {
                    "expiry": "20260728",
                    "trading_class": "E4B",
                    "exchange": "CME",
                    "multiplier": "50",
                    "expiry_at_values": [
                        "2026-07-28T20:00:00+00:00",
                        "2026-07-28T21:00:00+00:00",
                    ],
                    "contract_count": 2,
                }
            ]

    with pytest.raises(RuntimeError, match="conflict"):
        read_persisted_ibkr_option_expiry_facts(
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=Store(),
        )


def test_gex_exact_series_rejects_incomplete_call_put_pairs() -> None:
    chain = SimpleNamespace(
        exchange="SMART",
        tradingClass="QQQ",
        multiplier="100",
        strikes=[717.0, 717.5, 718.0],
    )
    details = [
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=index,
                secType="OPT",
                symbol="QQQ",
                lastTradeDateOrContractMonth=expiry,
                strike=strike,
                right=right,
                tradingClass="QQQ",
                exchange="SMART",
                multiplier="100",
                currency="USD",
            ),
            lastTradeTime="16:00",
            timeZoneId="America/New_York",
        )
        for index, (expiry, strike, right) in enumerate(
            (
                ("20260715", 717.0, "C"),
                ("20260715", 717.0, "P"),
                ("20260715", 717.5, "C"),
            ),
            start=1,
        )
    ]

    with pytest.raises(RuntimeError, match="incomplete Call/Put pair"):
        ibkr_option_acquisition._contracts_from_option_series_details(
            details,
            currency="USD",
            chain=chain,
            futures_options=False,
            expiration="20260715",
        )


def test_gex_exact_series_rejects_empty_contract_details() -> None:
    chain = SimpleNamespace(
        exchange="SMART",
        tradingClass="QQQ",
        multiplier="100",
        strikes=[717.0],
    )

    with pytest.raises(RuntimeError, match="returned no contracts"):
        ibkr_option_acquisition._contracts_from_option_series_details(
            [],
            currency="USD",
            chain=chain,
            futures_options=False,
            expiration="20260715",
        )


def test_gex_exact_series_does_not_use_symbol_text_as_identity_proof() -> None:
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="EW2",
        multiplier="50",
        strikes=[7575.0],
    )
    details = [
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=index,
                secType="FOP",
                symbol="ESU6",
                lastTradeDateOrContractMonth="20260715",
                strike=7575.0,
                right=right,
                tradingClass="EW2",
                exchange="CME",
                multiplier="50",
                currency="USD",
            ),
            lastTradeTime="16:00",
            timeZoneId="America/Chicago",
        )
        for index, right in enumerate(("C", "P"), start=1)
    ]

    contracts = ibkr_option_acquisition._contracts_from_option_series_details(
        details,
        currency="USD",
        chain=chain,
        futures_options=True,
        expiration="20260715",
    )

    assert [contract.con_id for contract in contracts] == [1, 2]
    assert {contract.symbol for contract in contracts} == {"ESU6"}


def test_exact_option_details_select_market_rule_for_exact_exchange() -> None:
    chain = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        strikes=[500.0],
    )
    details = [
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=index,
                secType="OPT",
                symbol="SPY",
                lastTradeDateOrContractMonth="20260809",
                strike=500.0,
                right=right,
                tradingClass="SPY",
                exchange="SMART",
                multiplier="100",
                currency="USD",
            ),
            validExchanges="CBOE,SMART",
            marketRuleIds="26,27",
            minTick=0.01,
            lastTradeTime="16:00",
            timeZoneId="America/New_York",
        )
        for index, right in enumerate(("C", "P"), start=1)
    ]

    contracts = ibkr_option_acquisition._contracts_from_option_series_details(
        details,
        currency="USD",
        chain=chain,
        futures_options=False,
        expiration="20260809",
    )

    assert {contract.market_rule_id for contract in contracts} == {27}
    assert {contract.minimum_tick for contract in contracts} == {0.01}
    assert all(not contract.price_increments for contract in contracts)


def test_option_market_rule_attaches_complete_provider_price_schedule() -> None:
    contracts = [
        replace(_ibkr_option(con_id=1, strike=500.0, right="C"), market_rule_id=27),
        replace(_ibkr_option(con_id=2, strike=500.0, right="P"), market_rule_id=27),
    ]

    class FakeIb:
        async def reqMarketRuleAsync(self, market_rule_id):
            assert market_rule_id == 27
            return [
                SimpleNamespace(lowEdge=0.0, increment=0.05),
                SimpleNamespace(lowEdge=3.0, increment=0.10),
            ]

    attached, meta = asyncio.run(
        ibkr_option_acquisition._attach_option_price_increments_async(
            FakeIb(),
            contracts,
        )
    )

    assert meta["price_rule_status"] == "complete"
    assert meta["price_rule_ids"] == [27]
    assert meta["price_rule_failed_ids"] == []
    assert [increment.to_payload() for increment in attached[0].price_increments] == [
        {"low_edge": 0.0, "increment": 0.05},
        {"low_edge": 3.0, "increment": 0.1},
    ]


def test_option_market_rule_failure_has_no_local_price_fallback() -> None:
    contract = replace(
        _ibkr_option(con_id=1, strike=500.0, right="C"),
        market_rule_id=27,
    )

    class FakeIb:
        async def reqMarketRuleAsync(self, _market_rule_id):
            return []

    attached, meta = asyncio.run(
        ibkr_option_acquisition._attach_option_price_increments_async(
            FakeIb(),
            [contract],
        )
    )

    assert meta["price_rule_status"] == "unavailable"
    assert meta["price_rule_failed_ids"] == [27]
    assert attached[0].price_increments == ()


def test_gex_exact_series_accepts_provider_qualified_strikes_newer_than_chain_descriptor() -> None:
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E4C",
        multiplier="50",
        strikes=[8400.0],
    )
    details = [
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=index,
                secType="FOP",
                symbol="ES",
                lastTradeDateOrContractMonth="20260722",
                strike=strike,
                right=right,
                tradingClass="E4C",
                exchange="CME",
                multiplier="50",
                currency="USD",
            ),
            lastTradeTime="16:00",
            timeZoneId="America/Chicago",
        )
        for index, (strike, right) in enumerate(
            (
                (8400.0, "C"),
                (8400.0, "P"),
                (8450.0, "C"),
                (8450.0, "P"),
            ),
            start=1,
        )
    ]

    contracts = ibkr_option_acquisition._contracts_from_option_series_details(
        details,
        currency="USD",
        chain=chain,
        futures_options=True,
        expiration="20260722",
    )

    assert [(contract.strike, contract.right.value) for contract in contracts] == [
        (8400.0, "C"),
        (8400.0, "P"),
        (8450.0, "C"),
        (8450.0, "P"),
    ]


def test_gex_exact_series_rejects_contract_details_from_another_provider_series() -> None:
    chain = SimpleNamespace(
        exchange="SMART",
        tradingClass="QQQ",
        multiplier="100",
        strikes=[717.0],
    )
    details = [
        SimpleNamespace(
            contract=SimpleNamespace(
                conId=index,
                secType="OPT",
                symbol=symbol,
                lastTradeDateOrContractMonth="20260715",
                strike=717.0,
                right=right,
                tradingClass=trading_class,
                exchange=exchange,
                multiplier=multiplier,
                currency=currency,
            ),
            lastTradeTime="16:00",
            timeZoneId="America/New_York",
        )
        for index, (
            symbol,
            trading_class,
            exchange,
            multiplier,
            currency,
            right,
        ) in enumerate(
            (
                ("QQQ", "QQQ", "SMART", "100", "USD", "C"),
                ("QQQ", "QQQ", "SMART", "100", "USD", "P"),
                ("SPY", "QQQ", "SMART", "100", "USD", "C"),
                ("QQQ", "SPY", "SMART", "100", "USD", "P"),
                ("QQQ", "QQQ", "CBOE", "100", "USD", "C"),
                ("QQQ", "QQQ", "SMART", "50", "USD", "P"),
                ("QQQ", "QQQ", "SMART", "100", "EUR", "C"),
            ),
            start=1,
        )
    ]

    with pytest.raises(RuntimeError, match="disagreed with the exact requested"):
        ibkr_option_acquisition._contracts_from_option_series_details(
            details,
            currency="USD",
            chain=chain,
            futures_options=False,
            expiration="20260715",
        )


def test_gex_exact_series_rejects_duplicate_strike_right_contracts() -> None:
    exact = _ibkr_option(
        con_id=1,
        strike=717.0,
        right="C",
        expiry="20260715",
        symbol="QQQ",
        sec_type="OPT",
        exchange="SMART",
        multiplier="100",
    )

    with pytest.raises(ValueError, match="duplicate strike/right"):
        ibkr_option_acquisition._select_global_option_series_contracts(
            [[exact, replace(exact, con_id=2)]],
            717.0,
            21,
            42,
        )
