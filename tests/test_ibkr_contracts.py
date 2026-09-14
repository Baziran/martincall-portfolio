import asyncio
import inspect
import logging
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import scripts.check_futures_contract_alignment as futures_alignment
from aef_terminal.data.ibkr import bars as ibkr_bars_module
from aef_terminal.data.ibkr import cache as ibkr_cache_module
from aef_terminal.data.ibkr import contracts as ibkr_contracts_module
from aef_terminal.data.ibkr import manager as ibkr_manager_module
from aef_terminal.data.ibkr import options as ibkr_options_module
from aef_terminal.data.ibkr import quotes as ibkr_quotes_module
from aef_terminal.data.ibkr import runtime as ibkr_runtime_module
from aef_terminal.data.ibkr import session as ibkr_session_module
from aef_terminal.data.ibkr import utils as ibkr_utils_module
from aef_terminal.data.ibkr.bars import IbkrBarFeed
from aef_terminal.data.ibkr.contracts import (
    _contract_for_instrument,
    _contract_from_instrument_identity,
    _duration_for_range,
    _ibkr_bar_provenance,
    _parse_ibkr_expiry,
    _quote_contract_for_instrument,
    _what_to_show,
    futures_contract_month_is_stale,
    futures_rollover_warning_for_contract,
)
from aef_terminal.data.ibkr.history import write_continuous_history
from aef_terminal.data.ibkr.quotes import _quote_from_ticker
from aef_terminal.data.ibkr.runtime import _IbkrRuntimeState
from aef_terminal.data.instrument_identity import qualified_instrument_id, route_fingerprint
from aef_terminal.data.provider_contract import CanonicalBarCommitReceipt
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.runtime.chart_events import (
    clear_live_chart_bars,
    live_chart_bars_snapshot,
    publish_live_chart_bars,
)
from aef_terminal.runtime.instruments import resolve_instrument_profile
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


# These contract tests intentionally compose the concrete IBKR owners they exercise.
# The production package does not provide a service-locator facade.
ibkr_module = SimpleNamespace(
    __name__=ibkr_runtime_module.__name__,
    _AsyncIbkrActivityWatchdog=ibkr_runtime_module._AsyncIbkrActivityWatchdog,
    _BoundedCache=ibkr_cache_module._BoundedCache,
    _IBKR_CLIENT_ID_IN_USE_BACKOFF_SECONDS=(
        ibkr_runtime_module._IBKR_CLIENT_ID_IN_USE_BACKOFF_SECONDS
    ),
    _IBKR_RUNTIME=ibkr_runtime_module._IBKR_RUNTIME,
    _cancel_chart_live_bars_async_owned=(ibkr_bars_module._cancel_chart_live_bars_async_owned),
    _cancel_option_quote_key=ibkr_options_module._cancel_option_quote_key,
    _chart_contract_for_instrument_async=(
        ibkr_contracts_module._chart_contract_for_instrument_async
    ),
    _chart_live_bars_async_owned=ibkr_bars_module._chart_live_bars_async_owned,
    _chart_stream_cache_key=ibkr_bars_module._chart_stream_cache_key,
    _connected_chart_ib_async=ibkr_session_module._connected_chart_ib_async,
    _connected_ib_async=ibkr_session_module._connected_ib_async,
    _connected_option_quote_ib_async=(ibkr_session_module._connected_option_quote_ib_async),
    _connected_quote_ib_async=ibkr_session_module._connected_quote_ib_async,
    _disconnect_owned_ibkr_session=ibkr_session_module._disconnect_owned_ibkr_session,
    _history_use_rth=ibkr_contracts_module._history_use_rth,
    _ibkr_connect_backoff_seconds=ibkr_session_module._ibkr_connect_backoff_seconds,
    _ibkr_session_ready=ibkr_session_module._ibkr_session_ready,
    _instrument_contract_cache_token=(ibkr_contracts_module._instrument_contract_cache_token),
    _live_option_quote_async_owned=ibkr_options_module._live_option_quote_async_owned,
    _market_data_type=ibkr_quotes_module._market_data_type,
    _option_quote_contract=ibkr_options_module._option_quote_contract,
    _option_quote_contract_key=ibkr_options_module._option_quote_contract_key,
    _qualified_contract_async=ibkr_contracts_module._qualified_contract_async,
    _quote_cache_key=ibkr_quotes_module._quote_cache_key,
    _quote_contract_for_instrument=ibkr_contracts_module._quote_contract_for_instrument,
    _quote_error_for_contract=ibkr_quotes_module._quote_error_for_contract,
    _quote_inputs=ibkr_quotes_module._quote_inputs,
    _record_ibkr_api_error=ibkr_quotes_module._record_ibkr_api_error,
    _record_live_quote_updates=ibkr_quotes_module._record_live_quote_updates,
    _record_quote_error=ibkr_quotes_module._record_quote_error,
    _reset_ibkr_async_option_quote_session=(
        ibkr_session_module._reset_ibkr_async_option_quote_session
    ),
    _reset_ibkr_async_quote_session=ibkr_session_module._reset_ibkr_async_quote_session,
    _reset_ibkr_async_session=ibkr_session_module._reset_ibkr_async_session,
    _safe_cancel_mkt_data=ibkr_utils_module._safe_cancel_mkt_data,
    _sync_quote_subscriptions_async_owned=(
        ibkr_quotes_module._sync_quote_subscriptions_async_owned
    ),
    cached_chart_live_bars=ibkr_bars_module.cached_chart_live_bars,
    cached_quotes=ibkr_quotes_module.cached_quotes,
    cancel_option_quote=ibkr_options_module.cancel_option_quote,
    cleanup_quarantined_sessions_async=(ibkr_session_module.cleanup_quarantined_sessions_async),
    connect_without_account_sync=ibkr_session_module.connect_without_account_sync,
    connect_without_account_sync_async=(ibkr_session_module.connect_without_account_sync_async),
    force_reconnect_async=ibkr_session_module.force_reconnect_async,
    futures_contract_is_unqualified=ibkr_contracts_module.futures_contract_is_unqualified,
    ibkr_market_data_manager=ibkr_manager_module.ibkr_market_data_manager,
    live_option_quote=ibkr_options_module.live_option_quote,
    probe_quotes=ibkr_quotes_module.probe_quotes,
    read_live_quote_trade_events=ibkr_quotes_module.read_live_quote_trade_events,
    runtime_status=ibkr_quotes_module.runtime_status,
    sync_quote_subscriptions_async=ibkr_quotes_module.sync_quote_subscriptions_async,
)


class _FakeIbEvent:
    def __iadd__(self, _handler):
        return self


class _FakeIbBase:
    errorEvent = _FakeIbEvent()

    def isConnected(self):
        return False


@pytest.fixture(autouse=True)
def _register_unit_test_future_specs() -> None:
    return None


@pytest.fixture(autouse=True)
def _clear_ibkr_bbo_observation_state() -> Iterator[None]:
    ibkr_module._IBKR_RUNTIME.clear_ticker_bbo_observations()
    yield
    ibkr_module._IBKR_RUNTIME.clear_ticker_bbo_observations()


def test_sync_ib_async_contract_import_uses_canonical_event_loop_gate() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import asyncio; "
                "from aef_terminal.data.ibkr.contracts import "
                "_contract_from_instrument_identity; "
                "from tests.provider_payloads import ibkr_stock_payload; "
                "asyncio.set_event_loop(None); "
                "contract = _contract_from_instrument_identity("
                "ibkr_stock_payload('AAPL', con_id=265598)); "
                "assert contract.conId == 265598"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _ibkr_instrument(
    symbol: str,
    *,
    con_id: int,
    sec_type: str,
    asset_class: str,
    exchange: str,
    currency: str = "USD",
    primary_exchange: str = "",
) -> dict[str, object]:
    if asset_class in {"future", "futures", "future_root"}:
        return ibkr_future_payload(symbol, con_id=con_id, exchange=exchange)
    payload = ibkr_stock_payload(
        symbol,
        con_id=con_id,
        asset_class=asset_class,
        sec_type=sec_type,
    )
    payload["contract_identity"].update(
        {
            "exchange": exchange,
            "primary_exchange": primary_exchange,
            "currency": currency,
        }
    )
    return payload


def _quote_cache_key_for(instrument: dict) -> tuple:
    return ibkr_module._quote_cache_key(
        "127.0.0.1",
        4002,
        1017,
        True,
        instrument=instrument,
    )


def test_parse_ibkr_expiry_requires_exact_trading_day() -> None:
    assert _parse_ibkr_expiry("202606") is None
    assert str(_parse_ibkr_expiry("20260619")) == "2026-06-19"
    assert _parse_ibkr_expiry("") is None


def test_ibkr_duration_supports_signal_ranges() -> None:
    assert _duration_for_range("2d") == "2 D"
    assert _duration_for_range("3d") == "3 D"
    assert _duration_for_range("7d") == "1 W"
    assert _duration_for_range("14d") == "2 W"
    assert _duration_for_range("31d") == "31 D"
    assert _duration_for_range("2mo") == "2 M"
    assert _duration_for_range("3y") == "3 Y"


def test_ibkr_quote_contract_uses_concrete_future_not_continuous() -> None:
    class FakeIb:
        def qualifyContracts(self, contract):
            if str(getattr(contract, "secType", "")) == "CONTFUT":
                return [
                    SimpleNamespace(
                        secType="CONTFUT",
                        symbol="ES",
                        localSymbol="ESU6",
                        lastTradeDateOrContractMonth="20260918",
                        exchange="CME",
                        currency="USD",
                        conId=649180671,
                    )
                ]
            return [
                SimpleNamespace(
                    secType="FUT",
                    symbol="ES",
                    exchange="CME",
                    currency="USD",
                    conId=649180671,
                    localSymbol="ESU6",
                    lastTradeDateOrContractMonth="20260918",
                    includeExpired=False,
                )
            ]

    selected = _quote_contract_for_instrument(FakeIb(), ibkr_future_payload("ES"))

    assert selected.symbol == "ES"
    assert selected.exchange == "CME"
    assert selected.lastTradeDateOrContractMonth
    assert selected.secType == "FUT"
    assert selected.includeExpired is False
    assert selected.localSymbol == "ESU6"


def test_ibkr_quote_contract_uses_provider_qualified_identity_for_index() -> None:
    instrument = _ibkr_instrument(
        "SPX", con_id=416904, sec_type="IND", asset_class="index", exchange="CBOE"
    )

    selected = _quote_contract_for_instrument(_FakeIbBase(), instrument)

    assert selected.conId == 416904
    assert selected.secType == "IND"
    assert selected.symbol == "SPX"
    assert selected.exchange == "CBOE"


def test_ibkr_quote_contract_rejects_non_future_instrument_without_identity() -> None:
    with pytest.raises(ValueError, match="INSTRUMENT_IDENTITY_INCOMPLETE"):
        _quote_contract_for_instrument(
            _FakeIbBase(),
            {"instrument_key": "SPX", "provider": "ibkr", "provider_symbol": "SPX"},
        )


def test_quote_payload_includes_resolved_contract_label() -> None:
    ticker = SimpleNamespace(
        bid=7585.75,
        ask=7586.0,
        last=7586.25,
        close=7497.5,
        bidSize=1,
        askSize=2,
        lastSize=3,
        marketDataType=1,
        time=datetime(2026, 6, 15, 4, 25, tzinfo=timezone.utc),
        contract=SimpleNamespace(
            secType="FUT",
            symbol="ES",
            localSymbol="ESU6",
            lastTradeDateOrContractMonth="202609",
        ),
    )
    ticker.marketPrice = lambda: 7585.875

    quote = _quote_from_ticker(ticker)

    assert quote["contract"] == "ESU6"
    assert quote["local_symbol"] == "ESU6"
    assert quote["contract_month"] == "202609"
    assert "reference_option_price" not in quote
    assert "reference_option_price_source" not in quote
    assert quote["market_data_type"] == 1
    assert quote["market_data_entitlement"] == "live"
    assert quote["is_delayed"] is False
    assert quote["price"] == 7585.875
    assert quote["price_source"] == "bid_ask_mid"
    assert quote["provider_ts"] is None
    assert quote["received_at"] == "2026-06-15T04:25:00+00:00"
    assert quote["time_basis"] == "client_receive"
    assert quote["ts"] == quote["received_at"]


def test_quote_payload_rejects_unqualified_futures_contract_price() -> None:
    ticker = SimpleNamespace(
        bid=None,
        ask=None,
        last=None,
        close=73.93,
        bidSize=None,
        askSize=None,
        lastSize=None,
        time=datetime(2026, 7, 12, 8, 33, tzinfo=timezone.utc),
        contract=SimpleNamespace(
            secType="FUT",
            symbol="ES",
            localSymbol="",
            lastTradeDateOrContractMonth="",
        ),
    )
    ticker.marketPrice = lambda: 73.93

    quote = _quote_from_ticker(ticker, "ES")

    assert quote["price"] is None
    assert quote["close"] is None
    assert "qualified concrete contract" in str(quote["message"])


def test_quote_payload_uses_provider_last_trade_timestamp_only_for_last() -> None:
    last_timestamp = datetime(2026, 7, 22, 11, 59, 58, tzinfo=UTC)
    received_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    ticker = SimpleNamespace(
        bid=10.0,
        ask=10.2,
        last=10.1,
        close=9.9,
        bidSize=1,
        askSize=2,
        lastSize=3,
        marketDataType=1,
        time=received_at,
        lastTimestamp=last_timestamp,
        contract=SimpleNamespace(secType="STK", symbol="SPY"),
        marketPrice=lambda: 10.1,
    )

    quote = _quote_from_ticker(ticker, "SPY")

    assert quote["price_source"] == "last"
    assert quote["provider_ts"] == last_timestamp.isoformat()
    assert quote["last_provider_ts"] == last_timestamp.isoformat()
    assert quote["received_at"] == received_at.isoformat()
    assert quote["bid_ask_received_at"] == received_at.isoformat()
    assert quote["time_basis"] == "provider_event"


@pytest.mark.parametrize("sec_type", ["OPT", "FOP"])
def test_option_quote_prefers_current_midpoint_over_older_last(sec_type: str) -> None:
    last_timestamp = datetime(2026, 7, 22, 11, 59, tzinfo=UTC)
    received_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    ticker = SimpleNamespace(
        bid=13.5,
        ask=13.7,
        last=13.4,
        close=12.9,
        bidSize=4,
        askSize=5,
        lastSize=2,
        marketDataType=1,
        time=received_at,
        lastTimestamp=last_timestamp,
        ticks=[
            SimpleNamespace(tickType=1, price=13.5, time=received_at),
            SimpleNamespace(tickType=2, price=13.7, time=received_at),
        ],
        contract=SimpleNamespace(secType=sec_type, localSymbol="TEST OPTION"),
        marketPrice=lambda: 13.4,
    )

    quote = _quote_from_ticker(ticker)

    assert quote["price"] == pytest.approx(13.6)
    assert quote["price_source"] == "bid_ask_mid"
    assert quote["provider_ts"] is None
    assert quote["received_at"] == received_at.isoformat()
    assert quote["time_basis"] == "client_receive"
    assert quote["ts"] == received_at.isoformat()


def test_option_bbo_clock_ignores_unrelated_ticker_packets() -> None:
    bbo_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    unrelated_at = bbo_at + timedelta(seconds=4)
    ticker = SimpleNamespace(
        bid=13.5,
        ask=13.7,
        last=13.4,
        close=12.9,
        bidSize=4,
        askSize=5,
        lastSize=2,
        marketDataType=1,
        time=bbo_at,
        lastTimestamp=bbo_at - timedelta(seconds=1),
        ticks=[
            SimpleNamespace(tickType=1, price=13.5, time=bbo_at),
            SimpleNamespace(tickType=2, price=13.7, time=bbo_at),
        ],
        contract=SimpleNamespace(secType="OPT", localSymbol="TEST OPTION"),
        marketPrice=lambda: 13.4,
    )

    initial = _quote_from_ticker(ticker)
    ticker.time = unrelated_at
    ticker.last = 13.45
    ticker.ticks = [SimpleNamespace(tickType=4, price=13.45, time=unrelated_at)]
    after_last = _quote_from_ticker(ticker)

    assert initial["bid_received_at"] == bbo_at.isoformat()
    assert initial["ask_received_at"] == bbo_at.isoformat()
    assert initial["bid_ask_received_at"] == bbo_at.isoformat()
    assert after_last["packet_received_at"] == unrelated_at.isoformat()
    assert after_last["bid_ask_received_at"] == bbo_at.isoformat()
    assert after_last["received_at"] == bbo_at.isoformat()
    assert after_last["price_source"] == "bid_ask_mid"


def test_quote_payload_keeps_unstamped_lone_last_display_only() -> None:
    received_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    ticker = SimpleNamespace(
        bid=None,
        ask=None,
        last=10.1,
        close=9.9,
        bidSize=None,
        askSize=None,
        lastSize=3,
        marketDataType=1,
        time=received_at,
        contract=SimpleNamespace(secType="STK", symbol="SPY"),
        marketPrice=lambda: 10.1,
    )

    quote = _quote_from_ticker(ticker, "SPY")

    assert quote["price"] == 10.1
    assert quote["price_source"] == "last"
    assert quote["provider_ts"] is None
    assert quote["received_at"] == received_at.isoformat()
    assert quote["time_basis"] is None
    assert quote["ts"] is None


def test_quote_payload_derives_delayed_entitlement_from_actual_ticker_type() -> None:
    ticker = SimpleNamespace(
        bid=10.0,
        ask=10.2,
        last=10.1,
        close=9.9,
        bidSize=1,
        askSize=2,
        lastSize=3,
        marketDataType=3,
        time=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        contract=SimpleNamespace(secType="STK", symbol="SPY"),
        marketPrice=lambda: 10.1,
    )

    quote = _quote_from_ticker(ticker, "SPY")

    assert quote["market_data_type"] == 3
    assert quote["market_data_entitlement"] == "delayed"
    assert quote["is_delayed"] is True


def test_stock_contracts_pin_primary_exchange_for_api_market_data() -> None:
    spy = _contract_for_instrument(
        _ibkr_instrument(
            "SPY",
            con_id=756733,
            sec_type="STK",
            asset_class="stock",
            exchange="SMART",
            primary_exchange="ARCA",
        )
    )
    qqq = _contract_for_instrument(
        _ibkr_instrument(
            "QQQ",
            con_id=320227571,
            sec_type="STK",
            asset_class="stock",
            exchange="SMART",
            primary_exchange="ISLAND",
        )
    )
    aapl = _contract_for_instrument(
        _ibkr_instrument(
            "AAPL",
            con_id=265598,
            sec_type="STK",
            asset_class="stock",
            exchange="SMART",
            primary_exchange="ISLAND",
        )
    )

    assert spy.exchange == "SMART"
    assert spy.primaryExchange == "ARCA"
    assert qqq.exchange == "SMART"
    assert qqq.primaryExchange == "ISLAND"
    assert aapl.exchange == "SMART"
    assert aapl.primaryExchange == "ISLAND"


def test_forex_contract_uses_idealpro_cash_pair() -> None:
    eur_usd = _contract_for_instrument(
        _ibkr_instrument(
            "EUR",
            con_id=12087792,
            sec_type="CASH",
            asset_class="forex",
            exchange="IDEALPRO",
            currency="USD",
        ),
    )

    assert eur_usd.secType == "CASH"
    assert eur_usd.symbol == "EUR"
    assert eur_usd.currency == "USD"
    assert eur_usd.exchange == "IDEALPRO"


def test_forex_midpoint_request_stays_outside_canonical_trades() -> None:
    forex = _ibkr_instrument(
        "EUR",
        con_id=12087792,
        sec_type="CASH",
        asset_class="forex",
        exchange="IDEALPRO",
        currency="USD",
    )
    stock = _ibkr_instrument(
        "SPY", con_id=756733, sec_type="STK", asset_class="stock", exchange="SMART"
    )

    assert _what_to_show(forex) == "MIDPOINT"
    assert route_instrument(forex).adapter.bar_data_type(forex) == "TRADES"
    assert _what_to_show(stock) == "TRADES"


def test_spx_profile_uses_equity_index_session() -> None:
    profile = resolve_instrument_profile(
        ibkr_stock_payload("SPX", asset_class="index", sec_type="IND")
    )

    assert profile.family == "equity_index"
    assert profile.is_futures is False
    assert profile.is_index is True


def test_ibkr_contract_from_instrument_identity_uses_provider_con_id() -> None:
    contract = _contract_from_instrument_identity(
        _ibkr_instrument(
            "SPX",
            con_id=416904,
            sec_type="IND",
            asset_class="index",
            exchange="SMART",
            primary_exchange="CBOE",
        )
    )

    assert contract.conId == 416904
    assert contract.secType == "IND"
    assert contract.symbol == "SPX"
    assert contract.exchange == "CBOE"
    assert contract.primaryExchange == "CBOE"


def test_ibkr_contract_rejects_whitespace_variant_of_opaque_provider_contract_id() -> None:
    instrument = _ibkr_instrument(
        "SPX",
        con_id=416904,
        sec_type="IND",
        asset_class="index",
        exchange="SMART",
    )
    instrument["provider_contract_id"] = " 416904 "
    instrument["instrument_id"] = "ibkr|contract| 416904 "
    instrument["contract_identity"]["provider_contract_id"] = " 416904 "

    with pytest.raises(ValueError, match="IBKR_CONTRACT_IDENTITY_INCOMPLETE"):
        _contract_from_instrument_identity(instrument)


def test_ibkr_runtime_index_history_uses_typed_rth_policy() -> None:
    index_instrument = _ibkr_instrument(
        "SPX",
        con_id=416904,
        sec_type="IND",
        asset_class="index",
        exchange="CBOE",
    )

    stock_instrument = _ibkr_instrument(
        "SPY", con_id=756733, sec_type="STK", asset_class="stock", exchange="SMART"
    )

    assert ibkr_module._history_use_rth(index_instrument) is True
    assert ibkr_module._history_use_rth(stock_instrument) is False


def test_stock_history_uses_the_same_provider_qualified_contract_identity() -> None:
    spy = _contract_for_instrument(
        _ibkr_instrument(
            "SPY",
            con_id=756733,
            sec_type="STK",
            asset_class="stock",
            exchange="SMART",
            primary_exchange="ARCA",
        )
    )
    qqq = _contract_for_instrument(
        _ibkr_instrument(
            "QQQ",
            con_id=320227571,
            sec_type="STK",
            asset_class="stock",
            exchange="SMART",
            primary_exchange="ISLAND",
        )
    )

    assert spy.exchange == "SMART"
    assert spy.primaryExchange == "ARCA"
    assert qqq.exchange == "SMART"
    assert qqq.primaryExchange == "ISLAND"


def test_ibkr_contract_cache_token_includes_exact_request_membership() -> None:
    common = {
        "symbol": "SPY",
        "con_id": 756733,
        "asset_class": "stock",
    }
    instruments = (
        _ibkr_instrument(
            **common,
            sec_type="STK",
            exchange="SMART",
            primary_exchange="ARCA",
            currency="USD",
        ),
        _ibkr_instrument(
            **common,
            sec_type="CASH",
            exchange="SMART",
            primary_exchange="ARCA",
            currency="USD",
        ),
        _ibkr_instrument(
            **common,
            sec_type="STK",
            exchange="ARCA",
            primary_exchange="ARCA",
            currency="USD",
        ),
        _ibkr_instrument(
            **common,
            sec_type="STK",
            exchange="SMART",
            primary_exchange="ISLAND",
            currency="USD",
        ),
        _ibkr_instrument(
            **common,
            sec_type="STK",
            exchange="SMART",
            primary_exchange="ARCA",
            currency="EUR",
        ),
    )

    assert len(
        {ibkr_module._instrument_contract_cache_token(instrument) for instrument in instruments}
    ) == len(instruments)


def test_async_history_contract_cache_does_not_cross_request_scope() -> None:
    qualification_requests: list[tuple[str, str, str, str]] = []

    class FakeIb:
        async def qualifyContractsAsync(self, contract):
            qualification_requests.append(
                (
                    contract.secType,
                    contract.exchange,
                    contract.primaryExchange,
                    contract.currency,
                )
            )
            return [
                SimpleNamespace(
                    conId=contract.conId,
                    secType=contract.secType,
                    exchange=contract.exchange,
                    primaryExchange=contract.primaryExchange,
                    currency=contract.currency,
                )
            ]

    smart_usd = _ibkr_instrument(
        "SPY",
        con_id=756733,
        sec_type="STK",
        asset_class="stock",
        exchange="SMART",
        primary_exchange="ARCA",
        currency="USD",
    )
    arca_eur = _ibkr_instrument(
        "SPY",
        con_id=756733,
        sec_type="STK",
        asset_class="stock",
        exchange="ARCA",
        currency="EUR",
    )
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_history_contract_cache.clear()

    async def run_probe() -> tuple[object, object, object]:
        args = ("127.0.0.1", 4002, 2517, True)
        first = await ibkr_module._qualified_contract_async(
            FakeIb(),
            *args,
            instrument=smart_usd,
        )
        replay = await ibkr_module._qualified_contract_async(
            FakeIb(),
            *args,
            instrument=smart_usd,
        )
        second = await ibkr_module._qualified_contract_async(
            FakeIb(),
            *args,
            instrument=arca_eur,
        )
        return first, replay, second

    try:
        first, replay, second = asyncio.run(run_probe())
    finally:
        runtime.async_history_contract_cache.clear()

    assert first is replay
    assert second is not first
    assert qualification_requests == [
        ("STK", "SMART", "ARCA", "USD"),
        ("STK", "ARCA", "", "EUR"),
    ]


def test_async_history_contract_qualification_rejects_scope_mismatch() -> None:
    class FakeIb:
        async def qualifyContractsAsync(self, contract):
            return [
                SimpleNamespace(
                    conId=contract.conId,
                    secType=contract.secType,
                    exchange="SMART",
                    primaryExchange=contract.primaryExchange,
                    currency=contract.currency,
                )
            ]

    instrument = _ibkr_instrument(
        "SPY",
        con_id=756733,
        sec_type="STK",
        asset_class="stock",
        exchange="ARCA",
        currency="EUR",
    )
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_history_contract_cache.clear()

    async def run_probe() -> None:
        with pytest.raises(
            RuntimeError,
            match="IBKR_QUALIFIED_CONTRACT_SCOPE_MISMATCH.*field=exchange",
        ):
            await ibkr_module._qualified_contract_async(
                FakeIb(),
                "127.0.0.1",
                4002,
                2517,
                True,
                instrument=instrument,
            )

    try:
        asyncio.run(run_probe())
        assert not runtime.async_history_contract_cache
    finally:
        runtime.async_history_contract_cache.clear()


def test_futures_contract_staleness_uses_actual_expiry_not_a_calendar_front_month_guess() -> None:
    active = SimpleNamespace(
        secType="FUT", lastTradeDateOrContractMonth="20260731", localSymbol="CLN6", symbol="CL"
    )
    expired = SimpleNamespace(
        secType="FUT", lastTradeDateOrContractMonth="20260630", localSymbol="CLM6", symbol="CL"
    )
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc).date()
    assert _parse_ibkr_expiry("202607") is None
    assert _parse_ibkr_expiry("20260731") == datetime(2026, 7, 31, tzinfo=timezone.utc).date()
    assert futures_contract_month_is_stale(active, as_of) is False
    assert futures_contract_month_is_stale(expired, as_of) is True


def test_futures_contract_is_unqualified_without_concrete_local_symbol() -> None:
    assert (
        ibkr_module.futures_contract_is_unqualified(
            SimpleNamespace(
                secType="FUT", symbol="ES", localSymbol="", lastTradeDateOrContractMonth=""
            ),
        )
        is True
    )
    assert (
        ibkr_module.futures_contract_is_unqualified(
            SimpleNamespace(symbol="ES", localSymbol="ESU6", lastTradeDateOrContractMonth="202609"),
        )
        is False
    )
    assert (
        ibkr_module.futures_contract_is_unqualified(
            SimpleNamespace(symbol="SPY", localSymbol="", lastTradeDateOrContractMonth=""),
        )
        is False
    )


def test_futures_rollover_warning_within_three_days() -> None:
    contract = SimpleNamespace(
        secType="FUT", lastTradeDateOrContractMonth="20260630", localSymbol="CLM6", symbol="CL"
    )
    expiry = _parse_ibkr_expiry("20260630")
    assert expiry is not None
    warning = futures_rollover_warning_for_contract(
        contract,
        as_of=expiry - timedelta(days=2),
        warn_days=3,
    )
    assert warning is not None
    assert warning["status"] == "rollover_due"
    assert warning["days_left"] == 2
    assert "CLM6" in str(warning["message"])


def test_quote_contract_resolves_current_future_contract_without_shared_cache(monkeypatch) -> None:
    calls: list[str] = []

    def fake_current_future_contract(ib, *, instrument):
        calls.append("resolve")
        return SimpleNamespace(
            symbol="CL",
            lastTradeDateOrContractMonth="202607",
            localSymbol="CLN6",
            secType="FUT",
        )

    class FakeIb:
        def qualifyContracts(self, contract):
            return [contract]

    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    monkeypatch.setattr(
        ibkr_contracts_module,
        "_current_future_contract",
        fake_current_future_contract,
    )

    selected = ibkr_module._quote_contract_for_instrument(FakeIb(), instrument)

    assert calls == ["resolve"]
    assert selected.localSymbol == "CLN6"


def test_drop_stale_cached_future_contract_marks_trading_hours_stale() -> None:
    from aef_terminal.data.ibkr.cache import _BoundedCache
    from aef_terminal.data.ibkr.contracts import _drop_stale_cached_future_contract
    from aef_terminal.data import provider_sessions as sessions_module

    sessions_module._PENDING_TRADING_HOURS_REFRESH.setdefault("ibkr", {}).clear()
    sessions_module._TRADING_HOURS_CACHE.clear()
    cache = _BoundedCache(maxsize=8)
    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    cache_key = (
        "127.0.0.1",
        4002,
        1017,
        True,
        ibkr_module._instrument_contract_cache_token(instrument),
    )
    cache[cache_key] = SimpleNamespace(
        secType="FUT",
        lastTradeDateOrContractMonth="20260630",
        localSymbol="CLM6",
        symbol="CL",
    )
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc).date()

    assert (
        _drop_stale_cached_future_contract(
            cache,
            cache_key,
            instrument=instrument,
            as_of=as_of,
        )
        is True
    )
    assert cache_key not in cache
    pending = set(sessions_module.peek_pending_provider_trading_hours_refresh("ibkr"))
    assert (instrument_id, route_key) in pending


def test_chart_stream_invalidation_matches_instrument_and_route_pair(monkeypatch) -> None:
    from aef_terminal.data.ibkr.contracts import _invalidate_chart_streams_for_route

    runtime = ibkr_module._IBKR_RUNTIME
    first_key = ("127.0.0.1", 4002, 2017, True, "first", "5m", "stream")
    second_key = ("127.0.0.1", 4002, 2017, True, "second", "5m", "stream")
    runtime.async_chart_streams[first_key] = object()
    runtime.async_chart_streams[second_key] = object()
    runtime.async_chart_stream_routes[first_key] = ("instrument-a", "shared-route", "5m")
    runtime.async_chart_stream_routes[second_key] = ("instrument-b", "shared-route", "5m")
    discarded: list[tuple] = []

    def discard(cache_key: tuple, *, cancel: bool) -> bool:
        assert cancel is True
        discarded.append(cache_key)
        runtime.async_chart_streams.pop(cache_key, None)
        runtime.async_chart_stream_routes.pop(cache_key, None)
        return True

    monkeypatch.setattr(runtime, "discard_chart_stream", discard)
    try:
        _invalidate_chart_streams_for_route("instrument-a", "shared-route")
        assert discarded == [first_key]
        assert second_key in runtime.async_chart_streams
    finally:
        runtime.async_chart_streams.pop(first_key, None)
        runtime.async_chart_streams.pop(second_key, None)
        runtime.async_chart_stream_routes.pop(first_key, None)
        runtime.async_chart_stream_routes.pop(second_key, None)


def test_mark_provider_trading_hours_stale_clears_memory_cache(monkeypatch) -> None:
    from aef_terminal.data.provider_sessions import (
        _TRADING_HOURS_CACHE,
        acknowledge_provider_trading_hours_refresh,
        mark_provider_trading_hours_stale,
        peek_pending_provider_trading_hours_refresh,
        provider_trading_hours_refresh_generation,
    )

    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    deleted: list[str] = []

    class FakeStore:
        def initialize(self) -> None:
            return None

        def delete_trading_hours(self, *, instrument: dict[str, Any]) -> bool:
            deleted.append(route_fingerprint(instrument))
            return True

    store = FakeStore()
    identity_key = (instrument_id, route_key)
    _TRADING_HOURS_CACHE[identity_key] = (0.0, {})
    mark_provider_trading_hours_stale(instrument=instrument, store=store)
    assert identity_key in peek_pending_provider_trading_hours_refresh("ibkr")
    assert route_key in deleted
    assert identity_key not in _TRADING_HOURS_CACHE
    refresh_generation = provider_trading_hours_refresh_generation(
        "ibkr",
        instrument_id,
        route_key,
    )
    assert acknowledge_provider_trading_hours_refresh(
        "ibkr",
        instrument_id,
        route_key,
        expected_generation=refresh_generation,
    )
    assert peek_pending_provider_trading_hours_refresh("ibkr") == ()


def test_trading_hours_invalidation_during_storage_read_cannot_republish_stale_row() -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    identity_key = (
        qualified_instrument_id(instrument),
        route_fingerprint(instrument),
    )
    read_started = threading.Event()
    release_read = threading.Event()
    results: list[dict[str, Any] | None] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_trading_hours(self, *, instrument):
            read_started.set()
            assert release_read.wait(timeout=1.0)
            return {"payload": {"provider_contract_id": "stale-contract"}}

    sessions_module.invalidate_provider_trading_hours_cache(instrument=instrument)
    reader = threading.Thread(
        target=lambda: results.append(
            sessions_module._cached_provider_trading_hours(
                store=Store(),
                instrument=instrument,
            )
        )
    )
    reader.start()
    assert read_started.wait(timeout=1.0)
    sessions_module.invalidate_provider_trading_hours_cache(instrument=instrument)
    release_read.set()
    reader.join(timeout=1.0)

    assert not reader.is_alive()
    assert results == [None]
    assert identity_key not in sessions_module._TRADING_HOURS_CACHE


def test_trading_hours_identity_tokens_are_not_coerced_or_trimmed() -> None:
    from aef_terminal.data.provider_sessions import (
        _PENDING_TRADING_HOURS_REFRESH,
        _TRADING_HOURS_REFRESH_EPOCH,
        _provider_contract_id_from_trading_hours,
        acknowledge_provider_trading_hours_refresh,
        peek_pending_provider_trading_hours_refresh,
    )

    pending = _PENDING_TRADING_HOURS_REFRESH.setdefault("ibkr", {})
    pending[("opaque instrument token", "opaque route token")] = 7
    _TRADING_HOURS_REFRESH_EPOCH[("ibkr", "opaque instrument token", "opaque route token")] = 7
    acknowledge_provider_trading_hours_refresh(
        "ibkr",
        "opaque instrument token",
        " opaque route token ",
        expected_generation=7,
    )
    assert (
        "opaque instrument token",
        "opaque route token",
    ) in peek_pending_provider_trading_hours_refresh("ibkr")
    with pytest.raises(ValueError, match="route_fingerprint"):
        acknowledge_provider_trading_hours_refresh(
            "ibkr",
            "opaque instrument token",
            123,  # type: ignore[arg-type]
            expected_generation=7,
        )
    assert (
        _provider_contract_id_from_trading_hours({"payload": {"provider_contract_id": 123}}) == ""
    )
    assert (
        _provider_contract_id_from_trading_hours({"payload": {"provider_contract_id": "00123"}})
        == "00123"
    )


def test_trading_hours_stale_ack_cannot_clear_newer_refresh_generation() -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    route = route_instrument(instrument)

    class Store:
        def initialize(self) -> None:
            raise AssertionError("pending trading hours must fail closed before storage read")

    sessions_module.mark_provider_trading_hours_stale(instrument=instrument)
    stale_generation = sessions_module.provider_trading_hours_refresh_generation(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    sessions_module.mark_provider_trading_hours_stale(instrument=instrument)
    current_generation = sessions_module.provider_trading_hours_refresh_generation(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )

    assert current_generation > stale_generation
    assert not sessions_module.acknowledge_provider_trading_hours_refresh(
        route.provider,
        route.instrument_id,
        route.fingerprint,
        expected_generation=stale_generation,
    )
    assert (
        route.instrument_id,
        route.fingerprint,
    ) in sessions_module.peek_pending_provider_trading_hours_refresh(route.provider)
    assert (
        sessions_module._cached_provider_trading_hours(
            store=Store(),
            instrument=instrument,
        )
        is None
    )
    assert sessions_module.acknowledge_provider_trading_hours_refresh(
        route.provider,
        route.instrument_id,
        route.fingerprint,
        expected_generation=current_generation,
    )


def test_provider_trading_hours_completed_state_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    now = time.monotonic()
    monkeypatch.setattr(sessions_module, "_TRADING_HOURS_STATE_MAX_COMPLETED_ROUTES", 2)
    monkeypatch.setattr(sessions_module, "_TRADING_HOURS_CACHE", {})
    monkeypatch.setattr(sessions_module, "_TRADING_HOURS_CACHE_EPOCH", {})
    monkeypatch.setattr(sessions_module, "_PENDING_TRADING_HOURS_REFRESH", {})
    monkeypatch.setattr(sessions_module, "_TRADING_HOURS_REFRESH_EPOCH", {})
    with sessions_module._TRADING_HOURS_LOCK:
        for index in range(3):
            route_key = (f"instrument-{index}", f"route-{index}")
            sessions_module._TRADING_HOURS_CACHE[route_key] = (now, None)
            sessions_module._TRADING_HOURS_CACHE_EPOCH[route_key] = index + 1
            sessions_module._TRADING_HOURS_REFRESH_EPOCH[("ibkr", *route_key)] = index + 1

        sessions_module._prune_provider_trading_hours_state_locked(time.monotonic())
        sizes = {
            "cache": len(sessions_module._TRADING_HOURS_CACHE),
            "cache_epochs": len(sessions_module._TRADING_HOURS_CACHE_EPOCH),
            "pending": sum(
                len(pending) for pending in sessions_module._PENDING_TRADING_HOURS_REFRESH.values()
            ),
            "refresh_epochs": len(sessions_module._TRADING_HOURS_REFRESH_EPOCH),
        }

    assert sizes == {"cache": 2, "cache_epochs": 2, "pending": 0, "refresh_epochs": 2}


def test_ibkr_historical_schedule_materializes_exact_provider_sessions_and_reported_bounds(
    monkeypatch,
) -> None:
    from aef_terminal.data.ibkr import trading_hours as trading_hours_module

    calls: dict[str, Any] = {}

    class FakeIb:
        async def reqContractDetailsAsync(self, contract):
            calls["contract_id"] = contract.conId
            calls["contract"] = {
                "sec_type": contract.secType,
                "exchange": contract.exchange,
                "local_symbol": contract.localSymbol,
            }
            return [
                SimpleNamespace(
                    contract=SimpleNamespace(conId=contract.conId, localSymbol="ESU6"),
                    timeZoneId="US/Central",
                    tradingHours="",
                    liquidHours="",
                )
            ]

        async def reqHistoricalScheduleAsync(self, contract, numDays, endDateTime="", useRTH=True):
            calls["historical"] = {
                "contract_id": contract.conId,
                "num_days": numDays,
                "end": endDateTime,
                "use_rth": useRTH,
            }
            if contract.conId == 26718738:
                return SimpleNamespace(
                    startDateTime="20260818-09:30:00",
                    endDateTime="20260824-03:56:10",
                    timeZone="US/Eastern",
                    sessions=[
                        SimpleNamespace(
                            startDateTime="20260818-09:30:00",
                            endDateTime="20260818-16:00:00",
                            refDate="20260818",
                        ),
                        SimpleNamespace(
                            startDateTime="20260824-09:30:00",
                            endDateTime="20260824-16:00:00",
                            refDate="20260824",
                        ),
                        SimpleNamespace(
                            startDateTime="20260828-09:30:00",
                            endDateTime="20260828-16:00:00",
                            refDate="20260828",
                        ),
                    ],
                )
            return SimpleNamespace(
                startDateTime="20260717-00:00:00",
                endDateTime="20260720-17:00:00",
                timeZone="US/Central",
                sessions=[
                    SimpleNamespace(
                        startDateTime="20260717-08:30:00",
                        endDateTime="20260717-16:00:00",
                        refDate="20260717",
                    ),
                    SimpleNamespace(
                        startDateTime="20260719-17:00:00",
                        endDateTime="20260720-16:00:00",
                        refDate="20260720",
                    ),
                ],
            )

    async def connected(*_args, **_kwargs):
        return FakeIb()

    async def run_coroutine(_lane, _description, operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(trading_hours_module, "_connected_ib_async", connected)
    monkeypatch.setattr(
        trading_hours_module,
        "_require_ibkr_instrument",
        lambda instrument: instrument,
    )
    monkeypatch.setattr(
        trading_hours_module,
        "_quote_contract_for_instrument",
        _quote_contract_for_instrument,
    )
    monkeypatch.setattr(
        trading_hours_module.ibkr_market_data_manager,
        "run_coroutine",
        run_coroutine,
    )

    starts_at = datetime(2026, 7, 17, tzinfo=UTC)
    ends_at = datetime(2026, 7, 20, 23, 59, tzinfo=UTC)
    instrument = _ibkr_instrument(
        "ES",
        con_id=649180671,
        sec_type="FUT",
        asset_class="future",
        exchange="CME",
    )
    payload = asyncio.run(
        trading_hours_module.fetch_trading_hours_async(
            instrument,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    )

    assert calls["contract_id"] == 649180671
    assert calls["contract"] == {
        "sec_type": "FUT",
        "exchange": "CME",
        "local_symbol": "ESU6",
    }
    assert calls["historical"]["contract_id"] == 649180671
    assert calls["historical"]["use_rth"] is False
    assert calls["historical"]["num_days"] >= 5
    assert calls["historical"]["end"] == ends_at
    assert payload["source"] == "ibkr:reqHistoricalSchedule"
    assert payload["time_zone"] == "US/Central"
    assert payload["provider_reported_schedule_start"] == "2026-07-17T05:00:00+00:00"
    assert payload["provider_reported_schedule_end"] == "2026-07-20T22:00:00+00:00"
    coverage_start = datetime.fromisoformat(payload["schedule_coverage_start"])
    coverage_end = datetime.fromisoformat(payload["schedule_coverage_end"])
    assert coverage_start == datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    assert coverage_end == datetime(2026, 7, 20, 22, 0, tzinfo=UTC)
    assert coverage_end < ends_at
    assert all(
        coverage_start
        <= datetime.fromisoformat(interval["opens_at"])
        < datetime.fromisoformat(interval["closes_at"])
        <= coverage_end
        for interval in payload["trading_intervals"]
    )
    ordered_bounds = [
        (
            datetime.fromisoformat(interval["opens_at"]),
            datetime.fromisoformat(interval["closes_at"]),
        )
        for interval in payload["trading_intervals"]
    ]
    assert ordered_bounds[0][0] == coverage_start
    assert ordered_bounds[-1][1] == coverage_end
    assert all(
        previous[1] == current[0]
        for previous, current in zip(ordered_bounds, ordered_bounds[1:], strict=False)
    )
    closed = [
        interval for interval in payload["trading_intervals"] if interval["status"] == "closed"
    ]
    assert [interval["session_date"] for interval in closed] == [
        "2026-07-17",
        "2026-07-17",
        "2026-07-18",
        "2026-07-19",
        "2026-07-20",
    ]
    assert all(interval["metadata"]["provider_closed_complement"] is True for interval in closed)
    assert all("provider_schedule_omission" not in interval["metadata"] for interval in closed)
    assert {
        interval["session_date"]
        for interval in payload["trading_intervals"]
        if interval["status"] == "open"
    } == {"2026-07-17", "2026-07-20"}
    assert {interval["session_type"] for interval in payload["trading_intervals"]} == {"trading"}

    index_payload = asyncio.run(
        trading_hours_module.fetch_trading_hours_async(
            _ibkr_instrument(
                "SPX",
                con_id=416904,
                sec_type="IND",
                asset_class="index",
                exchange="CBOE",
            ),
            starts_at=starts_at,
            ends_at=ends_at,
        )
    )

    assert calls["historical"]["use_rth"] is True
    assert {interval["session_type"] for interval in index_payload["trading_intervals"]} == {
        "liquid"
    }
    assert all(
        interval["metadata"]["use_rth"] is True for interval in index_payload["trading_intervals"]
    )

    tick_payload = asyncio.run(
        trading_hours_module.fetch_trading_hours_async(
            _ibkr_instrument(
                "TICK-NYSE",
                con_id=26718738,
                sec_type="IND",
                asset_class="index",
                exchange="NYSE",
            ),
            starts_at=datetime(2026, 8, 18, tzinfo=UTC),
            ends_at=datetime(2026, 8, 31, tzinfo=UTC),
        )
    )

    assert tick_payload["provider_reported_schedule_start"] == "2026-08-18T13:30:00+00:00"
    assert tick_payload["provider_reported_schedule_end"] == "2026-08-24T07:56:10+00:00"
    assert tick_payload["schedule_coverage_start"] == "2026-08-18T13:30:00+00:00"
    assert tick_payload["schedule_coverage_end"] == "2026-08-28T20:00:00+00:00"
    tick_bounds = [
        (
            datetime.fromisoformat(interval["opens_at"]),
            datetime.fromisoformat(interval["closes_at"]),
        )
        for interval in tick_payload["trading_intervals"]
    ]
    assert tick_bounds[0][0] == datetime(2026, 8, 18, 13, 30, tzinfo=UTC)
    assert tick_bounds[-1][1] == datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    assert all(
        previous[1] == current[0]
        for previous, current in zip(tick_bounds, tick_bounds[1:], strict=False)
    )
    assert {
        interval["session_date"]
        for interval in tick_payload["trading_intervals"]
        if interval["status"] == "open"
    } == {"2026-08-18", "2026-08-24", "2026-08-28"}


@pytest.mark.parametrize(
    ("sessions", "error_code"),
    [
        (
            [
                SimpleNamespace(
                    startDateTime="",
                    endDateTime="20260717-16:00:00",
                    refDate="20260717",
                )
            ],
            "IBKR_TRADING_SCHEDULE_SESSION_START_INVALID",
        ),
        (
            [
                SimpleNamespace(
                    startDateTime="20260717-08:30:00",
                    endDateTime="20260717-16:00:00",
                    refDate="",
                )
            ],
            "IBKR_TRADING_SCHEDULE_SESSION_REF_DATE_INVALID",
        ),
        (
            [
                SimpleNamespace(
                    startDateTime="20260717-08:30:00",
                    endDateTime="20260717-16:00:00",
                    refDate="20260717",
                ),
                SimpleNamespace(
                    startDateTime="20260717-08:30:00",
                    endDateTime="20260717-16:00:00",
                    refDate="20260717",
                ),
            ],
            "IBKR_TRADING_SCHEDULE_SESSION_DUPLICATE",
        ),
        (
            [
                SimpleNamespace(
                    startDateTime="20260717-08:30:00",
                    endDateTime="20260717-16:00:00",
                    refDate="20260717",
                ),
                SimpleNamespace(
                    startDateTime="20260717-15:59:00",
                    endDateTime="20260717-17:00:00",
                    refDate="20260717",
                ),
            ],
            "IBKR_TRADING_SCHEDULE_SESSION_OVERLAP",
        ),
    ],
)
def test_ibkr_historical_schedule_rejects_entire_invalid_snapshot(
    sessions,
    error_code,
) -> None:
    from aef_terminal.data.ibkr import trading_hours as trading_hours_module

    def parse_ibkr_datetime(raw_value: str) -> datetime:
        return datetime.strptime(raw_value, "%Y%m%d-%H:%M:%S")

    with pytest.raises(RuntimeError, match=error_code):
        trading_hours_module._parse_ibkr_historical_schedule(
            SimpleNamespace(
                startDateTime="20260717-00:00:00",
                endDateTime="20260718-00:00:00",
                sessions=sessions,
            ),
            parser=parse_ibkr_datetime,
            session_timezone=ZoneInfo("US/Central"),
            requested_start=datetime(2026, 7, 17, tzinfo=UTC),
            requested_end=datetime(2026, 7, 18, tzinfo=UTC),
            contract_id="649180671",
        )


def test_mark_provider_trading_hours_stale_logs_storage_failure(caplog) -> None:
    from aef_terminal.data.provider_sessions import (
        mark_provider_trading_hours_stale,
        peek_pending_provider_trading_hours_refresh,
    )

    class FakeStore:
        def initialize(self) -> None:
            return None

        def delete_trading_hours(self, *, instrument: dict[str, Any]) -> bool:
            raise RuntimeError(f"{route_fingerprint(instrument)}:locked")

    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    caplog.set_level(logging.ERROR, logger="aef_terminal.data.provider_sessions")
    mark_provider_trading_hours_stale(instrument=instrument, reason="test", store=FakeStore())

    assert (
        instrument_id,
        route_key,
    ) in peek_pending_provider_trading_hours_refresh("ibkr")
    assert "trading_hours_stale_storage_failed" in caplog.text
    assert "CL" in caplog.text


def test_quote_from_ticker_does_not_invent_calendar_rollover_state() -> None:
    ticker = SimpleNamespace(
        bid=76.6,
        ask=76.7,
        last=76.65,
        close=75.5,
        bidSize=1,
        askSize=1,
        lastSize=1,
        marketPrice=lambda: 76.65,
        contract=SimpleNamespace(
            lastTradeDateOrContractMonth="20260731", localSymbol="CLN6", symbol="CL"
        ),
    )
    fixed_now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("aef_terminal.data.ibkr.contracts.datetime", FixedDateTime)
        quote = _quote_from_ticker(ticker, "CL")
    finally:
        monkeypatch.undo()

    assert quote["contract"] == "CLN6"
    assert not quote.get("contract_rollover_new")
    assert not quote.get("contract_rollover_due")


def test_quote_from_ticker_includes_rollover_warning_for_near_expiry_contract() -> None:
    expiry = _parse_ibkr_expiry("20260630")
    assert expiry is not None
    ticker = SimpleNamespace(
        bid=70.0,
        ask=70.1,
        last=70.05,
        close=69.5,
        bidSize=1,
        askSize=1,
        lastSize=1,
        marketPrice=lambda: 70.05,
        contract=SimpleNamespace(
            secType="FUT", lastTradeDateOrContractMonth="20260630", localSymbol="CLM6", symbol="CL"
        ),
    )
    monkeypatch = pytest.MonkeyPatch()
    try:
        fixed_now = datetime.combine(
            expiry - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        )

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

        monkeypatch.setattr("aef_terminal.data.ibkr.contracts.datetime", FixedDateTime)
        quote = _quote_from_ticker(ticker, "CL")
    finally:
        monkeypatch.undo()

    assert quote["contract_rollover_due"] is True
    assert "CLM6" in str(quote["contract_rollover_warning"]["message"])


def test_ibkr_history_requests_include_extended_hours() -> None:
    source = inspect.getsource(IbkrBarFeed._bars_async_owned)
    assert "use_rth = _history_use_rth(self.instrument)" in source
    assert "useRTH=use_rth" in source
    assert "_AsyncIbkrActivityWatchdog" in source
    assert "idle-timeout" in source
    assert "history_timeout = max(float(self.timeout), 8.0)" in source
    assert "timeout=0" in source
    assert "except asyncio.CancelledError" in source
    assert "_reset_ibkr_async_session" in source
    chart_source = inspect.getsource(ibkr_module._chart_live_bars_async_owned)
    assert "_CHART_LIVE_INITIAL_TIMEOUT_SECONDS" in chart_source
    assert "timeout=request_timeout" in chart_source
    assert "contract=contract" in chart_source
    assert "asyncio.wait_for(ib.reqHistoricalDataAsync" not in chart_source


def test_ibkr_futures_chart_contract_is_qualified_continuous_history() -> None:
    seen: list[object] = []

    class FakeIb:
        async def qualifyContractsAsync(self, contract):
            seen.append(contract)
            return [
                SimpleNamespace(
                    conId=11004968,
                    secType=str(contract.secType),
                    symbol=str(contract.symbol),
                    exchange=str(contract.exchange),
                    primaryExchange=str(contract.primaryExchange),
                    currency=str(contract.currency),
                )
            ]

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_chart_contract_cache.clear()
    try:
        contract = asyncio.run(
            ibkr_module._chart_contract_for_instrument_async(
                FakeIb(),
                "127.0.0.1",
                4002,
                2017,
                True,
                instrument=ibkr_future_payload("ES"),
            )
        )
    finally:
        runtime.async_chart_contract_cache.clear()

    assert len(seen) == 1
    assert seen[0].secType == "CONTFUT"
    assert contract.secType == "CONTFUT"


def test_cached_chart_live_bars_reads_immutable_owner_loop_snapshot_without_broker_request() -> (
    None
):
    instrument = ibkr_future_payload("ES")
    expected = Bar(
        symbol="ES",
        ts=datetime(2026, 7, 15, 12, 5, tzinfo=timezone.utc),
        open=7600.0,
        high=7602.0,
        low=7599.0,
        close=7601.5,
        volume=25.0,
        timeframe="5m",
        source="ibkr:chart-live",
        closed=False,
    )
    instrument_id = qualified_instrument_id(instrument)
    fingerprint = route_fingerprint(instrument)
    generation = publish_live_chart_bars(
        "ibkr",
        "5m",
        instrument_id=instrument_id,
        route_fingerprint=fingerprint,
        bars=[expected],
        reason="test_owner_snapshot",
    )
    try:
        bars = ibkr_module.cached_chart_live_bars(
            "5m",
            "1d",
            host="127.0.0.1",
            port=4002,
            client_id=2017,
            readonly=True,
            instrument=instrument,
        )
    finally:
        clear_live_chart_bars(
            "5m",
            fingerprint,
            instrument_id=instrument_id,
        )

    assert bars == [expected]
    assert generation > 0
    closed_snapshot = live_chart_bars_snapshot(
        "5m",
        fingerprint,
        instrument_id=instrument_id,
    )
    assert closed_snapshot is not None
    assert closed_snapshot.reason == "stream_closed"
    assert closed_snapshot.bars == ()


def test_chart_stream_leases_share_one_range_independent_subscription(monkeypatch) -> None:
    instrument = ibkr_future_payload("ES")
    key_1d = ibkr_module._chart_stream_cache_key(
        "5m",
        "1d",
        "127.0.0.1",
        4002,
        2017,
        True,
        instrument=instrument,
    )
    key_5d = ibkr_module._chart_stream_cache_key(
        "5m",
        "5d",
        "127.0.0.1",
        4002,
        2017,
        True,
        instrument=instrument,
    )
    assert key_1d == key_5d

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_chart_stream_consumers[key_1d] = {"consumer-a", "consumer-b"}
    discarded: list[tuple[tuple, bool]] = []

    def discard_chart_stream(cache_key: tuple, *, cancel: bool) -> bool:
        discarded.append((cache_key, cancel))
        runtime.async_chart_stream_consumers.pop(cache_key, None)
        return True

    monkeypatch.setattr(runtime, "discard_chart_stream", discard_chart_stream)
    try:
        first_result = asyncio.run(
            ibkr_module._cancel_chart_live_bars_async_owned(
                "5m",
                "1d",
                host="127.0.0.1",
                port=4002,
                client_id=2017,
                readonly=True,
                instrument=instrument,
                consumer_id="consumer-a",
            )
        )
        assert first_result is True
        assert runtime.async_chart_stream_consumers[key_1d] == {"consumer-b"}
        assert discarded == []

        second_result = asyncio.run(
            ibkr_module._cancel_chart_live_bars_async_owned(
                "5m",
                "5d",
                host="127.0.0.1",
                port=4002,
                client_id=2017,
                readonly=True,
                instrument=instrument,
                consumer_id="consumer-b",
            )
        )
        assert second_result is True
        assert discarded == [(key_1d, True)]
    finally:
        runtime.async_chart_stream_consumers.pop(key_1d, None)


def test_chart_stream_cancel_retains_owner_when_session_reset_is_unconfirmed() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    key = (
        "127.0.0.1",
        4002,
        2017,
        True,
        "ibkr|future_root|ES|CME|USD|ES|current:1",
        "5m",
        "TRADES",
    )
    raw_bars = SimpleNamespace(reqId=77)

    class FakeIb:
        wrapper = SimpleNamespace(clientId=2017)

        def isConnected(self):
            return True

        def cancelHistoricalData(self, _bars):
            raise RuntimeError("cancel failed")

        def disconnect(self):
            raise RuntimeError("disconnect failed")

    session = FakeIb()
    runtime.async_chart_session = session
    runtime.async_chart_session_key = key[:4]
    runtime.async_chart_streams[key] = raw_bars

    try:
        with pytest.raises(RuntimeError, match="ownership retained"):
            runtime.discard_chart_stream(key, cancel=True)

        assert runtime.async_chart_session is session
        assert runtime.async_chart_streams[key] is raw_bars
    finally:
        runtime.async_chart_session = None
        runtime.async_chart_session_key = None
        runtime.async_chart_streams.pop(key, None)
        runtime.release_quarantined_session(session)


def test_chart_stream_cancel_marks_request_before_broker_cancel() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    key = (
        "127.0.0.1",
        4002,
        2017,
        True,
        "5m",
        "",
        "ibkr|future_root|ES|CME|USD|ES|current:1",
    )
    raw_bars = SimpleNamespace(reqId=78)
    marker_visible_during_cancel: list[bool] = []

    class FakeIb:
        wrapper = SimpleNamespace(clientId=2017)

        def isConnected(self):
            return True

        def cancelHistoricalData(self, bars):
            with runtime.planned_chart_cancel_lock:
                marker_visible_during_cancel.append(
                    bars.reqId in runtime.planned_chart_cancel_req_ids
                )

    runtime.clear_planned_chart_cancels()
    runtime.async_chart_session = FakeIb()
    runtime.async_chart_session_key = key[:4]
    runtime.async_chart_streams[key] = raw_bars
    try:
        assert runtime.discard_chart_stream(key, cancel=True) is True
        assert marker_visible_during_cancel == [True]
        assert runtime.consume_planned_chart_cancel(78) is True
    finally:
        runtime.async_chart_session = None
        runtime.async_chart_session_key = None
        runtime.async_chart_streams.pop(key, None)
        runtime.clear_planned_chart_cancels()


def test_ibkr_history_owner_timeout_is_bounded_to_broker_timeout(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run_coroutine(lane, label, factory, *, timeout=None):
        seen.update(lane=lane, label=label, factory=factory, timeout=timeout)
        return []

    monkeypatch.setattr(ibkr_module.ibkr_market_data_manager, "run_coroutine", fake_run_coroutine)
    feed = IbkrBarFeed(
        "5m",
        "2d",
        timeout=30.0,
        instrument=ibkr_future_payload("ES"),
    )

    result = asyncio.run(feed.bars_async())

    assert result == []
    assert feed.provider_symbol == "ES"
    assert not hasattr(feed, "symbol")
    assert seen["lane"] == "history"
    assert seen["label"] == "ES 5m 2d"
    assert seen["timeout"] == 40.0


def test_cancelled_ibkr_history_disconnects_owner_session(monkeypatch) -> None:
    disconnected = asyncio.Event()
    request_started = asyncio.Event()
    request_timeout: list[float] = []

    class FakeIb:
        def isConnected(self):
            return not disconnected.is_set()

        def disconnect(self):
            disconnected.set()

        async def reqHistoricalDataAsync(self, *_args, **kwargs):
            request_timeout.append(float(kwargs["timeout"]))
            request_started.set()
            await asyncio.Event().wait()

    class FakeWatchdog:
        timed_out = False

        def __init__(self, **_kwargs):
            pass

        def attach_ib_update_event(self, _ib):
            return lambda: None

        def start(self):
            return None

        def beat(self):
            return None

        async def cancel(self):
            return None

    fake_ib = FakeIb()

    async def fake_connected(*_args):
        return fake_ib

    async def fake_qualified(*_args, **_kwargs):
        return SimpleNamespace(conId=611744635, secType="CONTFUT", symbol="ES")

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_history_session = fake_ib
    runtime.async_history_session_key = ("127.0.0.1", 4002, 2517, True)
    monkeypatch.setattr(ibkr_bars_module, "_connected_ib_async", fake_connected)
    monkeypatch.setattr(ibkr_bars_module, "_qualified_contract_async", fake_qualified)
    monkeypatch.setattr(ibkr_bars_module, "_AsyncIbkrActivityWatchdog", FakeWatchdog)

    async def run_probe() -> None:
        task = asyncio.create_task(
            IbkrBarFeed(
                "5m",
                "2d",
                timeout=30.0,
                host="127.0.0.1",
                port=4002,
                client_id=2517,
                readonly=True,
                instrument=ibkr_future_payload("ES"),
            )._bars_async_owned()
        )
        await asyncio.wait_for(request_started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_probe())

    assert disconnected.is_set() is True
    assert request_timeout == [0.0]
    assert runtime.async_history_session is None
    assert runtime.async_history_session_key is None
    assert runtime.last_history_error == "IBKR history request cancelled (ES 5m 2d)"
    runtime.last_history_error = ""


def test_history_watchdog_hook_failure_resets_published_session(monkeypatch) -> None:
    disconnects: list[str] = []
    history_requests: list[str] = []

    class RejectingEvent:
        def __iadd__(self, _callback):
            raise RuntimeError("hook rejected")

    class FakeIb:
        updateEvent = RejectingEvent()

        def isConnected(self):
            return True

        def disconnect(self):
            disconnects.append("history")

        async def reqHistoricalDataAsync(self, *_args, **_kwargs):
            history_requests.append("started")
            return []

    fake_ib = FakeIb()

    async def fake_connected(*_args):
        return fake_ib

    async def fake_qualified(*_args, **_kwargs):
        return SimpleNamespace(conId=611744635, secType="CONTFUT", symbol="ES")

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_history_session = fake_ib
    runtime.async_history_session_key = ("127.0.0.1", 4002, 2517, True)
    monkeypatch.setattr(ibkr_bars_module, "_connected_ib_async", fake_connected)
    monkeypatch.setattr(ibkr_bars_module, "_qualified_contract_async", fake_qualified)

    with pytest.raises(
        RuntimeError,
        match="IBKR_HISTORY_WATCHDOG_UPDATE_HOOK_REQUIRED",
    ):
        asyncio.run(
            IbkrBarFeed(
                "5m",
                "2d",
                timeout=30.0,
                host="127.0.0.1",
                port=4002,
                client_id=2517,
                readonly=True,
                instrument=ibkr_future_payload("ES"),
            )._bars_async_owned()
        )

    assert disconnects == ["history"]
    assert history_requests == []
    assert runtime.async_history_session is None
    assert runtime.async_history_session_key is None
    runtime.last_history_error = ""


def test_history_watchdog_timeout_resets_owner_before_request_resumes(
    monkeypatch,
) -> None:
    disconnect_owner_checks: list[bool] = []
    resumed_owner_checks: list[bool] = []
    request_released = asyncio.Event()

    class FakeIb:
        connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            disconnect_owner_checks.append(runtime.async_history_session is self)
            self.connected = False
            request_released.set()

        async def reqHistoricalDataAsync(self, *_args, **_kwargs):
            await request_released.wait()
            resumed_owner_checks.append(runtime.async_history_session is self)
            raise ConnectionError("history transport closed")

    class TriggeringWatchdog:
        def __init__(self, *, on_timeout_disconnect, **_kwargs):
            self._on_timeout_disconnect = on_timeout_disconnect
            self.timed_out = False

        def attach_ib_update_event(self, _ib):
            return lambda: None

        def start(self):
            asyncio.get_running_loop().call_soon(self._timeout)

        def _timeout(self):
            self.timed_out = True
            self._on_timeout_disconnect()

        def beat(self):
            return None

        async def cancel(self):
            return None

    fake_ib = FakeIb()

    async def fake_connected(*_args):
        return fake_ib

    async def fake_qualified(*_args, **_kwargs):
        return SimpleNamespace(conId=611744635, secType="CONTFUT", symbol="ES")

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_history_session = fake_ib
    runtime.async_history_session_key = ("127.0.0.1", 4002, 2517, True)
    runtime.async_history_contract_cache["owned"] = object()
    monkeypatch.setattr(ibkr_bars_module, "_connected_ib_async", fake_connected)
    monkeypatch.setattr(ibkr_bars_module, "_qualified_contract_async", fake_qualified)
    monkeypatch.setattr(
        ibkr_bars_module,
        "_AsyncIbkrActivityWatchdog",
        TriggeringWatchdog,
    )

    async def run_probe() -> None:
        with pytest.raises(
            TimeoutError,
            match="IBKR async history request idle-timeout",
        ):
            await asyncio.wait_for(
                IbkrBarFeed(
                    "5m",
                    "2d",
                    timeout=30.0,
                    host="127.0.0.1",
                    port=4002,
                    client_id=2517,
                    readonly=True,
                    instrument=ibkr_future_payload("ES"),
                )._bars_async_owned(),
                timeout=1.0,
            )

    asyncio.run(run_probe())

    assert disconnect_owner_checks == [True]
    assert resumed_owner_checks == [False]
    assert runtime.async_history_session is None
    assert runtime.async_history_session_key is None
    assert not runtime.async_history_contract_cache
    runtime.last_history_error = ""


def test_history_session_raises_request_scoped_errors_on_new_and_reused_connection(
    monkeypatch,
) -> None:
    from ib_async import RequestError

    key = ("127.0.0.1", 4002, 2517, True)

    async def fake_connect_async(ib, *_args):
        return ib

    async def run_probe() -> None:
        connected = await ibkr_module._connected_ib_async(*key[:3], key[3], 0.1)

        assert connected.RaiseRequestErrors is True

        request = connected.wrapper.startReq(731, container=[])
        connected.wrapper.error(
            731,
            162,
            "Historical Market Data Service request cancelled",
            "",
        )
        with pytest.raises(RequestError) as raised:
            await request
        assert raised.value.reqId == 731
        assert raised.value.code == 162

        connected.RaiseRequestErrors = False
        connected.isConnected = lambda: True
        reused = await ibkr_module._connected_ib_async(*key[:3], key[3], 0.1)

        assert reused is connected
        assert reused.RaiseRequestErrors is True

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_history_session = None
    runtime.async_history_session_key = None
    runtime.api_errors.clear()
    monkeypatch.setattr(
        ibkr_session_module,
        "connect_without_account_sync_async",
        fake_connect_async,
    )
    try:
        asyncio.run(run_probe())
    finally:
        ibkr_module._reset_ibkr_async_session()
        runtime.api_errors.clear()


def test_ibkr_runtime_writes_provider_native_continuous_history() -> None:
    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    bar = Bar(
        symbol="CL",
        ts=datetime(2026, 6, 10, 12, 5, tzinfo=timezone.utc),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=12,
        timeframe="5m",
        source="ibkr:continuous:provider-history",
        closed=True,
        provenance=_ibkr_bar_provenance(
            request_type="historical",
            what_to_show="TRADES",
            instrument=instrument,
            contract=SimpleNamespace(conId=987654, secType="CONTFUT"),
        ),
    )
    writes: dict[str, object] = {}

    class Store:
        def write_futures_canonical_bars(self, bars, **kwargs):
            writes["canonical"] = (list(bars), dict(kwargs))
            return CanonicalBarCommitReceipt(
                revision_sequence=1,
                written=len(bars),
                acknowledged=tuple(bars),
            )

    result = write_continuous_history(
        Store(),
        [bar],
        instrument=instrument,
    )

    assert result.written == 1
    assert result.acknowledged == (bar,)
    assert writes["canonical"] == (
        [bar],
        {
            "provider": "ibkr",
            "instrument_id": qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX")),
            "route_fingerprint": route_fingerprint(ibkr_future_payload("CL", exchange="NYMEX")),
            "series_type": "provider_native",
            "roll_policy": "provider_managed",
            "metadata": {
                "identity_scope": "future_root",
                "provider_contract_type": "CONTFUT",
                "provider_contract_id": "987654",
                "data_type": "TRADES",
                "series_authority": "provider",
            },
            "revision_sequence": None,
        },
    )
    same_id_concrete_future = replace(
        bar,
        provenance=replace(
            bar.provenance,
            provider_contract_type="FUT",
        ),
    )
    with pytest.raises(
        ValueError,
        match="IBKR_CONTINUOUS_HISTORY_CONTRACT_TYPE_MISMATCH",
    ):
        write_continuous_history(
            Store(),
            [same_id_concrete_future],
            instrument=instrument,
        )


def test_futures_alignment_script_uses_canonical_storage_not_alias_history() -> None:
    source = inspect.getsource(futures_alignment)

    assert "ibkr_history_symbol_aliases" not in source
    assert "read_bars(" not in source
    assert "read_recent_provider_bars(" in source
    assert "read_latest_futures_contract_bar_boundaries(" in source
    assert "store._connect()" not in source
    assert "cached_quotes([route]).quotes.get(route.fingerprint)" in source
    assert "cached_quote(" not in source
    assert "require_exact_identity_text" in source
    assert "get('local_symbol') or" not in source
    assert 'get("local_symbol") or' not in source
    assert "current_resolved_at" not in source


def test_ibkr_async_history_watchdog_uses_activity_before_disconnect() -> None:
    async def run_probe() -> bool:
        disconnected = asyncio.Event()
        watchdog = ibkr_module._AsyncIbkrActivityWatchdog(
            label="ES 5m 1d",
            idle_timeout=0.12,
            on_timeout_disconnect=disconnected.set,
        )
        try:
            watchdog.start()
            for _ in range(4):
                await asyncio.sleep(0.04)
                watchdog.beat()
                assert disconnected.is_set() is False
            await asyncio.wait_for(disconnected.wait(), timeout=0.8)
            return watchdog.timed_out
        finally:
            await watchdog.cancel()

    assert asyncio.run(run_probe()) is True


def test_ibkr_history_watchdog_requires_the_update_hook() -> None:
    class RejectingEvent:
        def __iadd__(self, _callback):
            raise RuntimeError("hook rejected")

    ib = SimpleNamespace(updateEvent=RejectingEvent())
    watchdog = ibkr_module._AsyncIbkrActivityWatchdog(
        label="ES async",
        idle_timeout=1.0,
        on_timeout_disconnect=lambda: None,
    )

    with pytest.raises(
        RuntimeError,
        match="IBKR_HISTORY_WATCHDOG_UPDATE_HOOK_REQUIRED",
    ):
        watchdog.attach_ib_update_event(ib)


def test_ibkr_runtime_state_is_single_mutable_state_surface() -> None:
    assert not hasattr(ibkr_module._IBKR_RUNTIME, "history_lock")
    assert ibkr_module.ibkr_market_data_manager is not None
    assert not hasattr(ibkr_module._IBKR_RUNTIME, "ticker_cache")
    assert not hasattr(ibkr_module._IBKR_RUNTIME, "quote_session")
    assert hasattr(ibkr_module._IBKR_RUNTIME, "async_ticker_cache")


def test_quote_read_helpers_match_errors_only_by_exact_contract_id() -> None:
    snapshot_source = inspect.getsource(ibkr_module.cached_quotes)
    assert "async_ticker_cache" not in snapshot_source
    assert "read_quote_snapshots" in snapshot_source

    ticker = SimpleNamespace(contract=SimpleNamespace(conId=12345))
    exact_error = (datetime.now(tz=UTC), 354, "No subscription", "SPY", 12345)
    other_contract_error = (datetime.now(tz=UTC), 354, "No subscription", "QQQ", 67890)
    unidentified_subscription_error = (
        datetime.now(tz=UTC),
        354,
        "Requires additional subscription",
        "",
        0,
    )

    assert "No subscription" in ibkr_module._quote_error_for_contract(exact_error, ticker)
    assert ibkr_module._quote_error_for_contract(other_contract_error, ticker) == ""
    assert ibkr_module._quote_error_for_contract(unidentified_subscription_error, ticker) == ""


def test_quote_error_event_publishes_exact_contract_snapshot() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    instrument = _ibkr_instrument(
        "SPY",
        con_id=12345,
        sec_type="STK",
        asset_class="stock",
        exchange="SMART",
    )
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    cache_key = _quote_cache_key_for(instrument)
    ticker = SimpleNamespace(
        contract=SimpleNamespace(
            conId=12345,
            secType="STK",
            symbol="SPY",
            localSymbol="SPY",
        ),
        bid=-1.0,
        ask=-1.0,
        last=-1.0,
        close=-1.0,
        bidSize=0,
        askSize=0,
        lastSize=0,
        ticks=[],
        marketPrice=lambda: -1.0,
    )
    original_last_error = runtime.last_quote_error
    original_quote_errors = list(runtime.quote_errors)
    original_api_errors = list(runtime.api_errors)
    runtime.async_ticker_cache[cache_key] = ticker
    runtime.async_quote_ticker_routes[id(ticker)] = {
        "instrument_id": instrument_id,
        "provider_symbol": "SPY",
        "route_fingerprint": route_key,
    }
    try:
        ibkr_module._record_quote_error(
            12,
            354,
            "No subscription",
            ticker.contract,
        )

        quote = ibkr_module.cached_quotes([route_instrument(instrument)]).quotes[route_key]
        assert quote["price"] is None
        assert quote["message"] == "IBKR quote error 354: No subscription (SPY)"
    finally:
        runtime.async_ticker_cache.pop(cache_key, None)
        runtime.async_quote_ticker_routes.pop(id(ticker), None)
        runtime.discard_quote_snapshot((instrument_id, route_key))
        runtime.last_quote_error = original_last_error
        runtime.quote_errors[:] = original_quote_errors
        runtime.api_errors[:] = original_api_errors


def test_missing_quote_snapshot_does_not_inherit_another_contract_error() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    instrument = _ibkr_instrument(
        "QQQ",
        con_id=67890,
        sec_type="STK",
        asset_class="stock",
        exchange="SMART",
    )
    original_last_error = runtime.last_quote_error
    runtime.last_quote_error = (
        datetime.now(tz=UTC),
        354,
        "No subscription",
        "SPY",
        12345,
    )
    try:
        quote = ibkr_module.cached_quotes([route_instrument(instrument)]).quotes[
            route_fingerprint(instrument)
        ]

        assert quote["message"] == "IBKR quote is not subscribed yet"
    finally:
        runtime.last_quote_error = original_last_error


def test_threadsafe_async_session_reset_uses_manager_control_lane(monkeypatch) -> None:
    from aef_terminal.data.ibkr import event_loop as ibkr_event_loop_module

    calls: list[tuple[str, str, float | None]] = []
    direct_resets: list[str] = []

    def fake_run_blocking(lane, label, factory, *, timeout=None, **_kwargs):
        calls.append((lane, label, timeout))
        coro = factory()
        coro.close()

    monkeypatch.setattr(
        ibkr_event_loop_module.ibkr_market_data_manager,
        "run_coroutine_blocking",
        fake_run_blocking,
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_quote_session",
        lambda: direct_resets.append("quote"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_chart_session",
        lambda: direct_resets.append("chart"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_session",
        lambda: direct_resets.append("history"),
    )

    ibkr_event_loop_module._reset_ibkr_async_sessions_threadsafe(timeout=2.5)

    assert calls == [("control", "ibkr async reset", 2.5)]
    assert direct_resets == []


def test_reset_all_reconciles_published_owner_after_quarantine_retry(
    monkeypatch,
) -> None:
    from aef_terminal.data.ibkr import event_loop as ibkr_event_loop_module

    calls: list[str] = []
    quarantined_transport_closed = False

    def reset_quote():
        calls.append("quote")
        if not quarantined_transport_closed:
            raise RuntimeError("first quote teardown retained ownership")

    def reset_quarantine():
        nonlocal quarantined_transport_closed
        calls.append("quarantine")
        quarantined_transport_closed = True

    monkeypatch.setattr(ibkr_session_module, "_reset_ibkr_async_quote_session", reset_quote)
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_option_quote_session",
        lambda: calls.append("option"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_chart_session",
        lambda: calls.append("chart"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_session",
        lambda: calls.append("history"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_quarantined_sessions",
        reset_quarantine,
    )

    asyncio.run(ibkr_event_loop_module._reset_ibkr_async_sessions_owned())

    assert calls == [
        "quote",
        "option",
        "chart",
        "history",
        "quote",
        "quarantine",
        "quote",
        "option",
        "chart",
        "history",
        "quarantine",
    ]


def test_owned_reset_yields_before_retrying_closing_transport(monkeypatch) -> None:
    from aef_terminal.data.ibkr import event_loop as ibkr_event_loop_module

    attempts: list[int] = []

    def reset_direct():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("transport still closing")

    monkeypatch.setattr(
        ibkr_event_loop_module,
        "_reset_ibkr_async_sessions_direct",
        reset_direct,
    )

    asyncio.run(ibkr_event_loop_module._reset_ibkr_async_sessions_owned())

    assert attempts == [1, 2]


def test_async_session_reset_async_uses_manager_loop_without_blocking(monkeypatch) -> None:
    from aef_terminal.data.ibkr import event_loop as ibkr_event_loop_module

    calls: list[tuple[str, str, float | None]] = []
    direct_resets: list[str] = []

    async def fake_run_coroutine(lane, label, factory, *, timeout=None):
        calls.append((lane, label, timeout))
        await factory()

    monkeypatch.setattr(
        ibkr_event_loop_module.ibkr_market_data_manager, "run_coroutine", fake_run_coroutine
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_quote_session",
        lambda: direct_resets.append("quote"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_option_quote_session",
        lambda: direct_resets.append("option"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_chart_session",
        lambda: direct_resets.append("chart"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_session",
        lambda: direct_resets.append("history"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_quarantined_sessions",
        lambda: direct_resets.append("quarantine"),
    )

    asyncio.run(ibkr_event_loop_module._reset_ibkr_async_sessions_async(timeout=2.5))

    assert calls == [("control", "ibkr async reset", 2.5)]
    assert direct_resets == ["quote", "option", "chart", "history", "quarantine"]


def test_manager_session_reset_attempts_every_owner_before_raising(monkeypatch) -> None:
    from aef_terminal.data.ibkr import event_loop as ibkr_event_loop_module

    resets: list[str] = []

    def fail_quote() -> None:
        resets.append("quote")
        raise RuntimeError("quote close failed")

    monkeypatch.setattr(ibkr_session_module, "_reset_ibkr_async_quote_session", fail_quote)
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_option_quote_session",
        lambda: resets.append("option"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_chart_session",
        lambda: resets.append("chart"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_async_session",
        lambda: resets.append("history"),
    )
    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_quarantined_sessions",
        lambda: resets.append("quarantine"),
    )

    with pytest.raises(ExceptionGroup, match="IBKR manager session teardown failed"):
        ibkr_event_loop_module._reset_ibkr_async_sessions_direct()

    assert resets == [
        "quote",
        "option",
        "chart",
        "history",
        "quote",
    ]


def test_force_reconnect_async_uses_nonblocking_async_session_reset() -> None:
    source = inspect.getsource(ibkr_session_module._force_reconnect_owned)
    boundary_source = inspect.getsource(ibkr_module.force_reconnect_async)

    assert "_reset_ibkr_async_sessions_async" in source
    assert "_reset_ibkr_async_sessions_threadsafe" not in source
    assert "provider_runtime=ibkr_gex_provider_runtime" in source
    assert source.index("await async_stop_live_gex_context(") < source.index(
        "await _reset_ibkr_async_sessions_async"
    )
    assert "reset_connection_backoff()" in source
    assert "connect_backoff.clear()" not in source
    assert "run_cancellation_deferred(" in boundary_source
    assert "_force_reconnect_owned(" in boundary_source


def test_force_reconnect_settles_owned_transition_before_cancellation(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def reconnect_owned(**_kwargs: Any) -> dict[str, object]:
        nonlocal completed
        started.set()
        await release.wait()
        completed = True
        return {"ok": True}

    monkeypatch.setattr(ibkr_session_module, "_force_reconnect_owned", reconnect_owned)

    async def run() -> None:
        task = asyncio.create_task(ibkr_session_module.force_reconnect_async())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert completed is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert completed is True


def test_cancel_quote_subscription_can_target_async_runtime_cache() -> None:
    from aef_terminal.data.ibkr import quotes as ibkr_quotes_module

    runtime = ibkr_module._IBKR_RUNTIME
    key = (
        "127.0.0.1",
        4002,
        2017,
        True,
        route_fingerprint(ibkr_future_payload("ES")),
    )
    contract = SimpleNamespace(symbol="ES")
    async_ticker = SimpleNamespace(contract=contract, tickerId=71)

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={71: async_ticker})

        def cancelMktData(self, cancelled_contract):
            assert cancelled_contract is contract
            return True

    runtime.async_ticker_cache[key] = async_ticker
    runtime.async_quote_contract_cache[key] = contract
    runtime.async_option_quote_keys.add(key)
    try:
        ibkr_quotes_module._cancel_quote_subscription(
            FakeIb(),
            key,
            async_ticker,
            ticker_cache=runtime.async_ticker_cache,
            contract_cache=runtime.async_quote_contract_cache,
            option_quote_keys=runtime.async_option_quote_keys,
        )

        assert key not in runtime.async_ticker_cache
        assert key not in runtime.async_quote_contract_cache
        assert key not in runtime.async_option_quote_keys
    finally:
        runtime.async_ticker_cache.pop(key, None)
        runtime.async_quote_contract_cache.pop(key, None)
        runtime.async_option_quote_keys.discard(key)


def test_ambiguous_quote_cancel_resets_before_runtime_ownership_is_discarded(
    monkeypatch,
) -> None:
    from aef_terminal.data.ibkr import quotes as ibkr_quotes_module

    runtime = ibkr_module._IBKR_RUNTIME
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    key = ibkr_module._quote_cache_key(
        "127.0.0.1",
        4002,
        2117,
        True,
        instrument=instrument,
    )
    contract = SimpleNamespace(symbol="SPY")
    ticker = SimpleNamespace(contract=contract, tickerId=72)
    ownership_at_reset: list[bool] = []
    runtime.async_ticker_cache[key] = ticker
    runtime.async_quote_contract_cache[key] = contract
    monkeypatch.setattr(
        ibkr_quotes_module,
        "_safe_cancel_mkt_data",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        ibkr_quotes_module,
        "_reset_ibkr_async_quote_session",
        lambda: ownership_at_reset.append(
            key in runtime.async_ticker_cache and key in runtime.async_quote_contract_cache
        ),
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="quote cancellation state is ambiguous",
        ):
            ibkr_quotes_module._cancel_quote_subscription(
                SimpleNamespace(),
                key,
                ticker,
                ticker_cache=runtime.async_ticker_cache,
                contract_cache=runtime.async_quote_contract_cache,
                option_quote_keys=runtime.async_option_quote_keys,
            )

        assert ownership_at_reset == [True]
        assert key in runtime.async_ticker_cache
        assert key in runtime.async_quote_contract_cache
    finally:
        runtime.async_ticker_cache.pop(key, None)
        runtime.async_quote_contract_cache.pop(key, None)
        runtime.async_quote_ticker_routes.pop(id(ticker), None)
        runtime.async_option_quote_keys.discard(key)


def test_session_reset_closes_partial_transport_before_releasing_ownership() -> None:
    events: list[str] = []

    class FakeClient:
        def disconnect(self):
            events.append("transport_disconnect")

    class FakeWrapper:
        clientId = 2517

        def reset(self):
            events.append("wrapper_reset")

    class FakeDisconnectedEvent:
        def emit(self):
            events.append("disconnected_event")

    class FakeIb:
        client = FakeClient()
        wrapper = FakeWrapper()
        disconnectedEvent = FakeDisconnectedEvent()

        def disconnect(self):
            raise ConnectionError("API handshake incomplete")

    runtime = ibkr_module._IBKR_RUNTIME
    session = FakeIb()
    key = ("127.0.0.1", 4002, 2517, True)
    runtime.async_history_session = session
    runtime.async_history_session_key = key
    runtime.async_history_contract_cache["owned"] = object()

    ibkr_module._reset_ibkr_async_session()

    assert events == [
        "transport_disconnect",
        "disconnected_event",
        "wrapper_reset",
    ]
    assert runtime.async_history_session is None
    assert runtime.async_history_session_key is None
    assert not runtime.async_history_contract_cache


def test_session_reset_retains_ownership_when_transport_close_is_unconfirmed() -> None:
    class FakeIb:
        wrapper = SimpleNamespace(clientId=2518)

        def disconnect(self):
            raise ConnectionError("close failed")

    runtime = ibkr_module._IBKR_RUNTIME
    session = FakeIb()
    key = ("127.0.0.1", 4002, 2518, True)
    runtime.async_history_session = session
    runtime.async_history_session_key = key
    runtime.async_history_contract_cache["owned"] = object()

    try:
        with pytest.raises(RuntimeError, match="ownership retained"):
            ibkr_module._reset_ibkr_async_session()

        assert runtime.async_history_session is session
        assert runtime.async_history_session_key == key
        assert "owned" in runtime.async_history_contract_cache
    finally:
        runtime.async_history_session = None
        runtime.async_history_session_key = None
        runtime.async_history_contract_cache.clear()


def test_async_quote_subscriptions_use_owner_lane_and_cache(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def isConnected(self):
            return True

        def reqMktData(self, contract, *_args):
            calls.append(("reqMktData", contract.symbol))
            observed_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
            return SimpleNamespace(
                contract=contract,
                tickerId=22,
                bid=100.0,
                ask=100.25,
                last=100.25,
                close=99.5,
                bidSize=4,
                askSize=5,
                lastSize=2,
                time=observed_at,
                lastTimestamp=observed_at,
                marketDataType=1,
                marketPrice=lambda: 100.25,
            )

    async def fake_connected(*_args):
        ib = FakeIb()
        ib._aef_requested_market_data_type = ibkr_module._market_data_type()
        return ib

    ibkr_module._IBKR_RUNTIME.async_ticker_cache.clear()
    ibkr_module._IBKR_RUNTIME.clear_quote_snapshots()
    ibkr_module._IBKR_RUNTIME.async_quote_contract_cache.clear()
    monkeypatch.setattr(ibkr_quotes_module, "_connected_quote_ib_async", fake_connected)

    async def fake_quote_contract(_ib, *_args, instrument, **_kwargs):
        symbol = instrument["provider_symbol"]
        return SimpleNamespace(
            symbol=symbol, localSymbol=f"{symbol}U6", lastTradeDateOrContractMonth="202609"
        )

    monkeypatch.setattr(
        ibkr_quotes_module,
        "_quote_contract_for_instrument_async_cache",
        fake_quote_contract,
    )

    async def run_probe():
        result = await ibkr_module.sync_quote_subscriptions_async(
            [ibkr_future_payload("ES")],
            host="127.0.0.1",
            port=4002,
            client_id=1017,
            readonly=True,
        )
        ibkr_module._record_live_quote_updates(
            list(ibkr_module._IBKR_RUNTIME.async_ticker_cache.values())
        )
        quotes = ibkr_module.cached_quotes([route_instrument(ibkr_future_payload("ES"))]).quotes
        return result, quotes, ibkr_module.runtime_status()

    result, quotes, status = asyncio.run(run_probe())
    quote_route = route_fingerprint(ibkr_future_payload("ES"))

    assert result == {"requested": 1, "active": 1}
    assert calls == [("reqMktData", "ES")]
    assert quotes[quote_route]["price"] == 100.25
    assert status["quote_values"] >= 1
    assert status["quote_snapshot_entries"] >= 1
    assert status["quote_snapshot_sequence"] >= 1
    assert status["quote_snapshot_age_seconds"] is not None
    assert status["quote_lock_busy"] is False
    assert status["market_data_manager"]["lanes"]["quote"]["completed"] >= 1
    ibkr_module._IBKR_RUNTIME.discard_quote_snapshot(
        (qualified_instrument_id(ibkr_future_payload("ES")), quote_route)
    )


def test_async_quote_sync_preserves_success_when_one_contract_fails(monkeypatch) -> None:
    requested: list[str] = []
    disconnected: list[bool] = []

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def isConnected(self):
            return True

        def disconnect(self):
            disconnected.append(True)

        def reqMarketDataType(self, _market_data_type):
            return None

        def reqMktData(self, contract, *_args):
            requested.append(contract.symbol)
            return SimpleNamespace(
                contract=contract,
                tickerId=22,
                bid=100.0,
                ask=100.25,
                marketPrice=lambda: 100.25,
            )

    async def fake_connected(*_args):
        ib = FakeIb()
        ib._aef_requested_market_data_type = ibkr_module._market_data_type()
        return ib

    async def fake_quote_contract(_ib, *_args, instrument, **_kwargs):
        symbol = instrument["provider_symbol"]
        if symbol == "ES":
            raise TimeoutError("contract resolution timed out")
        return SimpleNamespace(symbol=symbol, secType="STK")

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_ticker_cache.clear()
    runtime.clear_quote_snapshots()
    runtime.async_quote_contract_cache.clear()
    runtime.async_option_quote_keys.clear()
    monkeypatch.setattr(ibkr_quotes_module, "_connected_quote_ib_async", fake_connected)
    monkeypatch.setattr(
        ibkr_quotes_module,
        "_quote_contract_for_instrument_async_cache",
        fake_quote_contract,
    )

    spy = _ibkr_instrument(
        "SPY",
        con_id=756733,
        sec_type="STK",
        asset_class="stock",
        exchange="SMART",
    )

    async def run_probe() -> None:
        with pytest.raises(RuntimeError, match="ES: contract resolution timed out"):
            await ibkr_module.sync_quote_subscriptions_async(
                [ibkr_future_payload("ES"), spy],
                timeout=0.1,
                host="127.0.0.1",
                port=4002,
                client_id=1017,
                readonly=True,
            )

    asyncio.run(run_probe())

    assert requested == ["SPY"]
    assert disconnected == []
    assert any(
        key[4] == ibkr_module._instrument_contract_cache_token(spy)
        for key in runtime.async_ticker_cache
    )
    assert {key[4] for key in runtime.last_quote_wanted_keys} == {
        ibkr_module._instrument_contract_cache_token(ibkr_future_payload("ES")),
        ibkr_module._instrument_contract_cache_token(spy),
    }
    runtime.async_ticker_cache.clear()
    runtime.clear_quote_snapshots()
    runtime.async_quote_contract_cache.clear()
    runtime.last_quote_wanted_keys.clear()


def test_live_option_quote_uses_async_option_owner_lane(monkeypatch) -> None:
    calls: list[str] = []
    quote_received_at = datetime.now(tz=UTC)
    contract_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12345, "exchange": "CME", "sec_type": "FOP"}
    )
    cache_key = ("127.0.0.1", 4002, 3517, True, contract_key)

    class FakeIb:
        def reqMktData(self, contract, *_args):
            calls.append(contract.localSymbol)
            return SimpleNamespace(
                contract=contract,
                tickerId=24,
                bid=20.75,
                ask=21.25,
                last=21.0,
                close=20.5,
                bidSize=4,
                askSize=5,
                lastSize=2,
                time=quote_received_at,
                lastTimestamp=quote_received_at - timedelta(minutes=1),
                ticks=[
                    SimpleNamespace(
                        tickType=1,
                        price=20.75,
                        time=quote_received_at,
                    ),
                    SimpleNamespace(
                        tickType=2,
                        price=21.25,
                        time=quote_received_at,
                    ),
                ],
                marketPrice=lambda: 21.0,
            )

    async def fake_connected(*_args):
        return FakeIb()

    ibkr_module._reset_ibkr_async_quote_session()
    ibkr_module._reset_ibkr_async_option_quote_session()
    ibkr_module._IBKR_RUNTIME.async_option_quote_contract_cache[cache_key] = SimpleNamespace(
        localSymbol="ES P4970",
        secType="FOP",
    )
    monkeypatch.setattr(
        ibkr_quotes_module,
        "_connected_quote_ib_async",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("watchlist quote socket must not be used")
        ),
    )
    monkeypatch.setattr(
        ibkr_options_module,
        "_connected_option_quote_ib_async",
        fake_connected,
    )

    quote = ibkr_module.live_option_quote(
        {
            "con_id": 12345,
            "exchange": "CME",
            "sec_type": "FOP",
            "local_symbol": "ES P4970",
            "asset": "ES",
            "expiry": "20260602",
            "right": "P",
            "strike": 4970,
        },
        host="127.0.0.1",
        port=4002,
        client_id=3517,
        readonly=True,
        timeout=0.2,
    )

    assert quote["bid"] == 20.75
    assert quote["ask"] == 21.25
    assert quote["price"] == 21.0
    assert quote["price_source"] == "bid_ask_mid"
    assert quote["time_basis"] == "client_receive"
    assert calls == ["ES P4970"]
    assert cache_key not in ibkr_module._IBKR_RUNTIME.async_ticker_cache
    assert cache_key in ibkr_module._IBKR_RUNTIME.async_option_ticker_cache
    assert cache_key not in ibkr_module._IBKR_RUNTIME.async_option_quote_keys


def test_option_quote_generation_is_checked_before_cached_ticker(monkeypatch) -> None:
    contract_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12346, "exchange": "CME", "sec_type": "FOP"}
    )
    cache_key = ("127.0.0.1", 4002, 3517, True, contract_key)
    old_ticker = SimpleNamespace(
        bid=99.0,
        ask=101.0,
        last=100.0,
        close=98.0,
        marketPrice=lambda: 100.0,
    )
    requests: list[tuple[int, str, str]] = []

    class FakeIb:
        def reqMktData(self, contract, *_args):
            requests.append(
                (
                    int(contract.conId),
                    str(contract.exchange),
                    str(contract.secType),
                )
            )
            return SimpleNamespace(
                contract=contract,
                tickerId=26,
                bid=20.75,
                ask=21.25,
                last=21.0,
                close=20.5,
                marketPrice=lambda: 21.0,
            )

    async def fake_connected(*_args):
        runtime.async_option_ticker_cache.clear()
        runtime.async_option_quote_contract_cache.clear()
        return FakeIb()

    runtime = ibkr_module._IBKR_RUNTIME
    monkeypatch.setattr(
        runtime,
        "async_option_ticker_cache",
        {cache_key: old_ticker},
    )
    monkeypatch.setattr(runtime, "async_option_quote_contract_cache", {})
    monkeypatch.setattr(
        ibkr_options_module,
        "_connected_option_quote_ib_async",
        fake_connected,
    )

    quote = asyncio.run(
        ibkr_module._live_option_quote_async_owned(
            {
                "con_id": 12346,
                "exchange": "CME",
                "sec_type": "FOP",
                "local_symbol": "ES C4980",
            },
            contract_key,
            0.2,
            "127.0.0.1",
            4002,
            3517,
            True,
        )
    )

    assert quote["price"] == 21.0
    assert requests == [(12346, "CME", "FOP")]


def test_option_quote_session_resets_when_market_data_generation_changes(
    monkeypatch,
) -> None:
    disconnects: list[str] = []

    class OldIb:
        _aef_requested_market_data_type = 1

        def isConnected(self):
            return True

        def disconnect(self):
            disconnects.append("old")

    async def fake_connect(ib, *_args):
        ib._aef_requested_market_data_type = 3
        return ib

    runtime = ibkr_module._IBKR_RUNTIME
    key = ("127.0.0.1", 4002, 3517, True)
    runtime.async_option_quote_session = OldIb()
    runtime.async_option_quote_session_key = key
    runtime.async_option_ticker_cache["old"] = object()
    runtime.async_option_quote_contract_cache["old"] = object()
    monkeypatch.setattr(ibkr_quotes_module, "_market_data_type", lambda: 3)
    monkeypatch.setattr(
        ibkr_session_module,
        "connect_without_account_sync_async",
        fake_connect,
    )

    session = asyncio.run(
        ibkr_module._connected_option_quote_ib_async(
            "127.0.0.1",
            4002,
            3517,
            True,
            0.2,
        )
    )

    assert disconnects == ["old"]
    assert session is runtime.async_option_quote_session
    assert session._aef_requested_market_data_type == 3
    assert not runtime.async_option_ticker_cache
    assert not runtime.async_option_quote_contract_cache
    ibkr_module._reset_ibkr_async_option_quote_session()


def test_live_option_quote_rejects_nested_legacy_contract_identity(monkeypatch) -> None:
    contracts: list[object] = []
    contract_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12345, "exchange": "CME", "sec_type": "FOP"}
    )
    cache_key = ("127.0.0.1", 4002, 3517, True, contract_key)

    class FakeIb:
        def reqMktData(self, contract, *_args):
            contracts.append(contract)
            return SimpleNamespace(
                contract=contract,
                tickerId=25,
                bid=20.75,
                ask=21.25,
                last=21.0,
                close=20.5,
                bidSize=4,
                askSize=5,
                lastSize=2,
                marketPrice=lambda: 21.0,
            )

    async def fake_connected(*_args):
        return FakeIb()

    ibkr_module._reset_ibkr_async_option_quote_session()
    monkeypatch.setattr(
        ibkr_options_module,
        "_connected_option_quote_ib_async",
        fake_connected,
    )

    with pytest.raises(ValueError, match="OPTION_PROVIDER_CONTRACT_ID_REQUIRED"):
        ibkr_module.live_option_quote(
            {
                "id": "target-1",
                "payload": {
                    "con_id": 12345,
                    "exchange": "CME",
                    "local_symbol": "ES P4970",
                    "expiry": "20260602",
                    "right": "P",
                    "strike": 4970,
                },
            },
            host="127.0.0.1",
            port=4002,
            client_id=3517,
            readonly=True,
            timeout=0.2,
        )

    assert contracts == []
    assert cache_key not in ibkr_module._IBKR_RUNTIME.async_option_ticker_cache


def test_live_option_quote_requires_provider_contract_id() -> None:
    with pytest.raises(ValueError, match="OPTION_PROVIDER_CONTRACT_ID_REQUIRED"):
        ibkr_module.live_option_quote(
            {
                "local_symbol": "ES P4970",
                "asset": "ES",
                "expiry": "20260602",
                "right": "P",
                "strike": 4970,
            },
            host="127.0.0.1",
            port=4002,
            client_id=3517,
            readonly=True,
            timeout=0.2,
        )


@pytest.mark.parametrize("contract_id", [" 12345", "12345 ", "0012345", "１２３４５"])
def test_option_quote_rejects_normalized_provider_contract_ids(contract_id: str) -> None:
    with pytest.raises(ValueError, match="OPTION_PROVIDER_CONTRACT_ID_REQUIRED"):
        ibkr_module._option_quote_contract_key(
            {"con_id": contract_id, "exchange": "CME", "sec_type": "FOP"}
        )


def test_option_quote_contract_and_cache_key_include_exact_provider_type() -> None:
    cme_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12345, "exchange": "CME", "sec_type": "FOP"}
    )
    smart_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12345, "exchange": "SMART", "sec_type": "FOP"}
    )
    opt_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12345, "exchange": "CME", "sec_type": "OPT"}
    )
    contract = ibkr_module._option_quote_contract(
        {"con_id": 12345, "exchange": "CME", "sec_type": "FOP"}
    )

    assert cme_key != smart_key
    assert cme_key != opt_key
    assert contract.conId == 12345
    assert contract.exchange == "CME"
    assert contract.secType == "FOP"


@pytest.mark.parametrize("sec_type", [None, "", "FUT", "fop", " FOP"])
def test_option_quote_requires_exact_provider_option_type(sec_type) -> None:
    with pytest.raises(ValueError, match="OPTION_PROVIDER_SEC_TYPE_REQUIRED"):
        ibkr_module._option_quote_contract_key(
            {"con_id": 12345, "exchange": "CME", "sec_type": sec_type}
        )


@pytest.mark.parametrize("timeout", [True, "0.2", 0, -0.1, float("inf")])
def test_live_option_quote_rejects_normalized_timeout(timeout) -> None:
    with pytest.raises(ValueError, match="OPTION_QUOTE_TIMEOUT_INVALID"):
        ibkr_module.live_option_quote(
            {"con_id": 12345, "exchange": "CME"},
            host="127.0.0.1",
            port=4002,
            client_id=3517,
            readonly=True,
            timeout=timeout,
        )


@pytest.mark.parametrize(
    "override",
    [
        {"host": ""},
        {"port": 0},
        {"port": "4002"},
        {"client_id": -1},
        {"client_id": "3517"},
        {"readonly": 1},
        {"readonly": "true"},
    ],
)
def test_live_option_quote_rejects_normalized_connection_values(override) -> None:
    kwargs = {
        "host": "127.0.0.1",
        "port": 4002,
        "client_id": 3517,
        "readonly": True,
        "timeout": 0.2,
        **override,
    }
    with pytest.raises(ValueError):
        ibkr_module.live_option_quote(
            {"con_id": 12345, "exchange": "CME"},
            **kwargs,
        )


def test_cancel_option_quote_cancels_async_option_session(monkeypatch) -> None:
    cancellations: list[str] = []
    contract = SimpleNamespace(
        conId=12345,
        exchange="CME",
        localSymbol="ES P4970",
    )
    ticker = SimpleNamespace(contract=contract, tickerId=24)
    contract_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12345, "exchange": "CME", "sec_type": "FOP"}
    )
    cache_key = ("127.0.0.1", 4002, 3517, True, contract_key)

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={24: ticker})

        def isConnected(self):
            return True

        def cancelMktData(self, cancelled_contract):
            cancellations.append(cancelled_contract.localSymbol)
            return True

        def disconnect(self):
            return None

    ibkr_module._reset_ibkr_async_option_quote_session()
    ibkr_module._IBKR_RUNTIME.async_option_quote_session = FakeIb()
    ibkr_module._IBKR_RUNTIME.async_option_quote_session_key = cache_key[:4]
    ibkr_module._IBKR_RUNTIME.async_option_ticker_cache[cache_key] = ticker
    ibkr_module._IBKR_RUNTIME.async_option_quote_contract_cache[cache_key] = contract
    monkeypatch.setattr(
        ibkr_module.ibkr_market_data_manager,
        "_last_paced_request_at",
        datetime.now(tz=UTC),
    )

    result = ibkr_module.cancel_option_quote(
        {"con_id": 12345, "exchange": "CME", "sec_type": "FOP"},
        host="127.0.0.1",
        port=4002,
        client_id=3517,
        readonly=True,
        timeout=0.2,
    )

    assert result == {"ok": True, "cancelled": True, "contract_key": contract_key}
    assert cancellations == ["ES P4970"]
    assert cache_key not in ibkr_module._IBKR_RUNTIME.async_option_ticker_cache
    assert cache_key not in ibkr_module._IBKR_RUNTIME.async_option_quote_contract_cache
    ibkr_module._reset_ibkr_async_option_quote_session()


def test_ambiguous_option_quote_cancel_resets_the_owned_session() -> None:
    contract = SimpleNamespace(
        conId=12346,
        exchange="CME",
        localSymbol="ES C4980",
    )
    ticker = SimpleNamespace(contract=contract, tickerId=25)
    contract_key = ibkr_module._option_quote_contract_key(
        {"con_id": 12346, "exchange": "CME", "sec_type": "FOP"}
    )
    cache_key = ("127.0.0.1", 4002, 3518, True, contract_key)
    disconnects: list[bool] = []

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={25: ticker})

        def isConnected(self):
            return True

        def cancelMktData(self, _contract):
            return False

        def disconnect(self):
            disconnects.append(True)

    runtime = ibkr_module._IBKR_RUNTIME
    ibkr_module._reset_ibkr_async_option_quote_session()
    runtime.async_option_quote_session = FakeIb()
    runtime.async_option_quote_session_key = cache_key[:4]
    runtime.async_option_ticker_cache[cache_key] = ticker
    runtime.async_option_quote_contract_cache[cache_key] = contract

    with pytest.raises(
        RuntimeError,
        match="option quote cancellation state is ambiguous",
    ):
        ibkr_module._cancel_option_quote_key(cache_key)

    assert disconnects == [True]
    assert runtime.async_option_quote_session is None
    assert cache_key not in runtime.async_option_ticker_cache
    assert cache_key not in runtime.async_option_quote_contract_cache


def test_client_id_in_use_connect_error_uses_long_backoff() -> None:
    error = RuntimeError(
        "Unable to connect as the client id is already in use. Retry with a unique client id."
    )

    assert (
        ibkr_module._ibkr_connect_backoff_seconds(error)
        == ibkr_module._IBKR_CLIENT_ID_IN_USE_BACKOFF_SECONDS
    )


def test_removed_history_and_repair_client_paths_cannot_return() -> None:
    assert not hasattr(ibkr_module, "fetch_historical_bars_once")
    assert not hasattr(ibkr_module, "ibkr_repair_client_id_candidates")
    assert not hasattr(ibkr_module._IBKR_RUNTIME, "repair_client_id")


def test_cached_quotes_reads_owner_published_snapshot() -> None:
    instrument = _ibkr_instrument(
        "SPY", con_id=12345, sec_type="STK", asset_class="stock", exchange="SMART"
    )
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    ticker = SimpleNamespace(
        bid=73400.0,
        ask=73402.0,
        last=73401.0,
        close=73350.0,
        bidSize=1,
        askSize=2,
        lastSize=3,
        marketPrice=lambda: 73401.0,
    )
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_quote_ticker_routes[id(ticker)] = {
        "instrument_id": instrument_id,
        "provider_symbol": "SPY",
        "route_fingerprint": route_key,
    }
    try:
        ibkr_module._record_live_quote_updates([ticker])
        quote = ibkr_module.cached_quotes([route_instrument(instrument)]).quotes[route_key]

        assert quote["price"] == 73401.0
        assert quote["last"] == 73401.0
        assert quote["bid"] == 73400.0
    finally:
        runtime.async_quote_ticker_routes.pop(id(ticker), None)
        runtime.discard_quote_snapshot((instrument_id, route_key))


def test_owner_quote_route_generation_prunes_without_refreshing_quote_data_age() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    stale_identity = ("instrument-a", "route-a")
    current_identity = ("instrument-a", "route-b")
    with runtime.quote_snapshot_lock:
        original_snapshots = {
            identity: dict(quote) for identity, quote in runtime.quote_snapshots.items()
        }
        original_routes = dict(runtime.quote_snapshot_routes)
        original_sequence = runtime.quote_snapshot_sequence
        original_updates = runtime.quote_snapshot_updates
        original_evictions = runtime.quote_snapshot_evictions
        original_updated_at = runtime.quote_snapshot_updated_at
        runtime.quote_snapshots.clear()
        runtime.quote_snapshot_routes.clear()
        runtime.quote_snapshot_sequence = 0
        runtime.quote_snapshot_updates = 0
        runtime.quote_snapshot_evictions = 0
        runtime.quote_snapshot_updated_at = None
    try:
        runtime.publish_quote_snapshots(
            {
                stale_identity: {
                    "price": 100.0,
                    "provider_symbol": "ESU6",
                }
            }
        )
        with runtime.quote_snapshot_lock:
            quote_data_updated_at = runtime.quote_snapshot_updated_at
            quote_sequence = runtime.quote_snapshot_sequence
            quote_evictions = runtime.quote_snapshot_evictions
        assert quote_data_updated_at is not None

        runtime.publish_quote_snapshots(
            {},
            active_routes={current_identity: "ESZ6"},
        )

        _sequence, snapshots = runtime.read_quote_snapshots([stale_identity, current_identity])
        assert snapshots == {}
        with runtime.quote_snapshot_lock:
            assert stale_identity not in runtime.quote_snapshot_routes
            assert runtime.quote_snapshot_routes[current_identity] == "ESZ6"
            assert runtime.quote_snapshot_updated_at == quote_data_updated_at
            assert runtime.quote_snapshot_sequence == quote_sequence + 1
            assert runtime.quote_snapshot_evictions == quote_evictions + 1

        runtime.discard_quote_snapshot(current_identity)
        with runtime.quote_snapshot_lock:
            assert runtime.quote_snapshot_updated_at == quote_data_updated_at
            assert runtime.quote_snapshot_sequence == quote_sequence + 2
            assert runtime.quote_snapshot_evictions == quote_evictions + 2

        runtime.publish_quote_snapshots(
            {},
            active_routes={current_identity: "ESZ6"},
        )
        with runtime.quote_snapshot_lock:
            sequence_before_clear = runtime.quote_snapshot_sequence
            evictions_before_clear = runtime.quote_snapshot_evictions
            assert runtime.quote_snapshot_updated_at == quote_data_updated_at
        runtime.clear_quote_snapshots()
        with runtime.quote_snapshot_lock:
            assert runtime.quote_snapshot_updated_at == quote_data_updated_at
            assert runtime.quote_snapshot_sequence == sequence_before_clear + 1
            assert runtime.quote_snapshot_evictions == evictions_before_clear + 1
    finally:
        with runtime.quote_snapshot_lock:
            runtime.quote_snapshots.clear()
            runtime.quote_snapshots.update(original_snapshots)
            runtime.quote_snapshot_routes.clear()
            runtime.quote_snapshot_routes.update(original_routes)
            runtime.quote_snapshot_sequence = original_sequence
            runtime.quote_snapshot_updates = original_updates
            runtime.quote_snapshot_evictions = original_evictions
            runtime.quote_snapshot_updated_at = original_updated_at


def test_owner_quote_snapshot_isolates_nested_payloads_from_writers_and_readers() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    identity = ("instrument-a", "route-a")
    quote = {
        "price": 100.0,
        "provider_symbol": "ESU6",
        "contract_rollover_warning": {"status": "rollover_due"},
    }
    with runtime.quote_snapshot_lock:
        original_snapshots = {key: dict(value) for key, value in runtime.quote_snapshots.items()}
        original_routes = dict(runtime.quote_snapshot_routes)
        original_sequence = runtime.quote_snapshot_sequence
        original_updates = runtime.quote_snapshot_updates
        original_evictions = runtime.quote_snapshot_evictions
        original_updated_at = runtime.quote_snapshot_updated_at
        runtime.quote_snapshots.clear()
        runtime.quote_snapshot_routes.clear()
    try:
        runtime.publish_quote_snapshots({identity: quote})
        quote["contract_rollover_warning"]["status"] = "mutated_writer"

        _sequence, first_read = runtime.read_quote_snapshots([identity])
        first_read[identity]["contract_rollover_warning"]["status"] = "mutated_reader"
        _sequence, second_read = runtime.read_quote_snapshots([identity])

        assert second_read[identity]["contract_rollover_warning"]["status"] == ("rollover_due")
    finally:
        with runtime.quote_snapshot_lock:
            runtime.quote_snapshots.clear()
            runtime.quote_snapshots.update(original_snapshots)
            runtime.quote_snapshot_routes.clear()
            runtime.quote_snapshot_routes.update(original_routes)
            runtime.quote_snapshot_sequence = original_sequence
            runtime.quote_snapshot_updates = original_updates
            runtime.quote_snapshot_evictions = original_evictions
            runtime.quote_snapshot_updated_at = original_updated_at


def test_owner_quote_snapshot_preserves_receipt_clocks_for_metadata_only_packets() -> None:
    runtime = _IbkrRuntimeState(16)
    identity = ("instrument-a", "route-a")
    first_received_at = "2026-07-28T10:00:00+00:00"
    metadata_received_at = "2026-07-28T10:00:01+00:00"
    price_received_at = "2026-07-28T10:00:02+00:00"
    quote = {
        "price": 100.5,
        "price_source": "bid_ask_mid",
        "bid": 100.0,
        "ask": 101.0,
        "last": 100.25,
        "close": 99.0,
        "provider_ts": None,
        "last_provider_ts": "2026-07-28T09:59:59+00:00",
        "time_basis": "client_receive",
        "received_at": first_received_at,
        "bid_ask_received_at": first_received_at,
        "bid_size": 1.0,
        "provider_symbol": "TEST",
    }
    runtime.publish_quote_snapshots({identity: quote})

    runtime.publish_quote_snapshots(
        {
            identity: {
                **quote,
                "received_at": metadata_received_at,
                "bid_ask_received_at": first_received_at,
                "bid_size": 2.0,
            }
        }
    )
    metadata_sequence, metadata_snapshot = runtime.read_quote_snapshots([identity])

    assert metadata_sequence == 2
    assert metadata_snapshot[identity]["bid_size"] == 2.0
    assert metadata_snapshot[identity]["received_at"] == first_received_at
    assert metadata_snapshot[identity]["bid_ask_received_at"] == first_received_at

    runtime.publish_quote_snapshots(
        {
            identity: {
                **quote,
                "price": 100.75,
                "ask": 101.5,
                "received_at": price_received_at,
                "bid_ask_received_at": price_received_at,
                "bid_size": 2.0,
            }
        }
    )
    price_sequence, price_snapshot = runtime.read_quote_snapshots([identity])

    assert price_sequence == 3
    assert price_snapshot[identity]["received_at"] == price_received_at
    assert price_snapshot[identity]["bid_ask_received_at"] == price_received_at


def test_live_quote_trade_event_hub_publishes_only_realtime_last_ticks() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    ticker = SimpleNamespace(
        ticks=[
            SimpleNamespace(
                tickType=1,
                price=73400.0,
                time=datetime(2026, 7, 16, 14, 47, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                tickType=4,
                price=73401.0,
                time=datetime(2026, 7, 16, 14, 47, 1, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                tickType=68,
                price=73399.0,
                time=datetime(2026, 7, 16, 14, 47, 2, tzinfo=timezone.utc),
            ),
        ]
    )
    with runtime.live_quote_trade_lock:
        original_events = list(runtime.live_quote_trade_events)
        original_sequence = runtime.live_quote_trade_sequence
        runtime.live_quote_trade_events.clear()
        start_sequence = runtime.live_quote_trade_sequence
    spy_instrument = _ibkr_instrument(
        "SPY",
        con_id=12345,
        sec_type="STK",
        asset_class="stock",
        exchange="SMART",
    )
    spy_instrument_id = qualified_instrument_id(spy_instrument)
    spy_route = route_fingerprint(spy_instrument)
    runtime.async_quote_ticker_routes[id(ticker)] = {
        "instrument_id": spy_instrument_id,
        "provider_symbol": "SPY",
        "route_fingerprint": spy_route,
    }
    try:
        ibkr_module._record_live_quote_updates([ticker])
        latest_sequence, events, overflowed = ibkr_module.read_live_quote_trade_events(
            start_sequence,
            [(spy_instrument_id, spy_route)],
        )
        assert runtime.live_quote_trade_events.maxlen == 8192
        assert latest_sequence == start_sequence + 1
        assert overflowed is False
        gateway_ts = events[0].pop("gateway_ts")
        assert datetime.fromisoformat(gateway_ts).tzinfo is not None
        assert events == [
            {
                "sequence": latest_sequence,
                "symbol": "SPY",
                "instrument_id": spy_instrument_id,
                "route_fingerprint": spy_route,
                "ts": "2026-07-16T14:47:01+00:00",
                "price": 73401.0,
                "source": "ibkr:last",
            }
        ]
        assert ibkr_module.read_live_quote_trade_events(-1, [(spy_instrument_id, spy_route)]) == (
            latest_sequence,
            [],
            False,
        )
        assert ibkr_module.read_live_quote_trade_events(
            latest_sequence,
            [(spy_instrument_id, spy_route)],
        ) == (
            latest_sequence,
            [],
            False,
        )
        assert ibkr_module.read_live_quote_trade_events(
            start_sequence,
            [("different-instrument", spy_route)],
        ) == (latest_sequence, [], False)
        qqq_instrument = _ibkr_instrument(
            "QQQ",
            con_id=67890,
            sec_type="STK",
            asset_class="stock",
            exchange="SMART",
        )
        assert ibkr_module.read_live_quote_trade_events(
            start_sequence,
            [
                (
                    qualified_instrument_id(qqq_instrument),
                    route_fingerprint(qqq_instrument),
                )
            ],
        ) == (latest_sequence, [], False)
    finally:
        runtime.async_quote_ticker_routes.pop(id(ticker), None)
        runtime.discard_quote_snapshot((spy_instrument_id, spy_route))
        with runtime.live_quote_trade_lock:
            runtime.live_quote_trade_events.clear()
            runtime.live_quote_trade_events.extend(original_events)
            runtime.live_quote_trade_sequence = original_sequence


def test_cached_quotes_ignores_negative_ibkr_price_sentinels() -> None:
    instrument = _ibkr_instrument(
        "SPY", con_id=12345, sec_type="STK", asset_class="stock", exchange="SMART"
    )
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    ticker = SimpleNamespace(
        bid=-1.0,
        ask=-1.0,
        last=-1.0,
        close=755.64,
        bidSize=0,
        askSize=0,
        lastSize=0,
        marketPrice=lambda: -1.0,
    )
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.async_quote_ticker_routes[id(ticker)] = {
        "instrument_id": instrument_id,
        "provider_symbol": "SPY",
        "route_fingerprint": route_key,
    }
    try:
        ibkr_module._record_live_quote_updates([ticker])
        quote = ibkr_module.cached_quotes([route_instrument(instrument)]).quotes[route_key]

        assert quote["price"] == 755.64
        assert quote["bid"] is None
        assert quote["ask"] is None
        assert quote["last"] is None
    finally:
        runtime.async_quote_ticker_routes.pop(id(ticker), None)
        runtime.discard_quote_snapshot((instrument_id, route_key))


def test_gex_lane_does_not_block_async_quote_sync(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def isConnected(self):
            return True

        def reqMktData(self, contract, *_args):
            calls.append(contract.symbol)
            return SimpleNamespace(
                contract=contract, tickerId=23, bid=101.0, ask=101.25, marketPrice=lambda: 101.25
            )

    async def fake_connected(*_args):
        ib = FakeIb()
        ib._aef_requested_market_data_type = ibkr_module._market_data_type()
        return ib

    async def hold_gex_lane() -> str:
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "done"

    ibkr_module._IBKR_RUNTIME.async_ticker_cache.clear()
    ibkr_module._IBKR_RUNTIME.clear_quote_snapshots()
    ibkr_module._IBKR_RUNTIME.async_quote_contract_cache.clear()
    monkeypatch.setattr(ibkr_quotes_module, "_connected_quote_ib_async", fake_connected)

    async def fake_quote_contract(_ib, *_args, instrument, **_kwargs):
        symbol = instrument["provider_symbol"]
        return SimpleNamespace(
            symbol=symbol, localSymbol=f"{symbol}U6", lastTradeDateOrContractMonth="202609"
        )

    monkeypatch.setattr(
        ibkr_quotes_module,
        "_quote_contract_for_instrument_async_cache",
        fake_quote_contract,
    )

    async def run_probe():
        gex_task = asyncio.create_task(
            ibkr_module.ibkr_market_data_manager.run_coroutine(
                "gex",
                "hung gex",
                lambda: hold_gex_lane(),
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        try:
            result = await ibkr_module.sync_quote_subscriptions_async(
                [ibkr_future_payload("ES")],
                host="127.0.0.1",
                port=4002,
                client_id=1017,
                readonly=True,
            )
            status = ibkr_module.runtime_status()
        finally:
            release.set()
        assert await gex_task == "done"
        return result, status

    result, status = asyncio.run(run_probe())

    assert result == {"requested": 1, "active": 1}
    assert calls == ["ES"]
    assert status["market_data_manager"]["lanes"]["gex"]["running"] is True
    assert status["quote_lock_busy"] is False


def test_contract_cache_is_bounded_and_lru() -> None:
    cache = ibkr_module._BoundedCache(maxsize=2)

    cache["first"] = 1
    cache["second"] = 2
    assert cache.get("first") == 1
    cache["third"] = 3

    assert list(cache.keys()) == ["first", "third"]
    assert cache.get("second") is None
    assert len(cache) == 2


def test_cancel_mkt_data_error_300_is_debug_only(monkeypatch, caplog) -> None:
    class IbCancelError(Exception):
        code = 300

    ticker = SimpleNamespace(contract=SimpleNamespace(symbol="SPY"), tickerId=11)

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={11: ticker})

        def cancelMktData(self, contract):
            raise IbCancelError("Can't find EId with tickerId")

    caplog.set_level(logging.DEBUG, logger=ibkr_module.__name__)

    cancelled = ibkr_module._safe_cancel_mkt_data(
        FakeIb(),
        ticker,
        ticker.contract,
    )

    assert cancelled is True
    assert "Ignored IBKR cancelMktData error 300" in caplog.text


def test_cancel_mkt_data_skips_broker_call_when_request_is_already_absent(caplog) -> None:
    calls: list[object] = []
    ticker = SimpleNamespace(contract=SimpleNamespace(symbol="SPY"))

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def cancelMktData(self, contract):
            calls.append(contract)
            raise AssertionError("absent broker request must not be cancelled again")

    caplog.set_level(logging.DEBUG, logger=ibkr_module.__name__)

    cancelled = ibkr_module._safe_cancel_mkt_data(
        FakeIb(),
        ticker,
        ticker.contract,
    )

    assert cancelled is True
    assert calls == []
    assert "cancellation already absent" in caplog.text


def test_quote_error_300_does_not_poison_quote_status(caplog) -> None:
    ibkr_module._IBKR_RUNTIME.quote_errors.clear()
    ibkr_module._IBKR_RUNTIME.last_quote_error = None
    caplog.set_level(logging.DEBUG, logger=ibkr_module.__name__)

    ibkr_module._record_quote_error(
        12, 300, "Can't find EId with tickerId", SimpleNamespace(symbol="SPY")
    )

    assert ibkr_module._IBKR_RUNTIME.quote_errors == []
    assert ibkr_module._IBKR_RUNTIME.last_quote_error is None
    assert "Ignored IBKR quote cancellation error 300" in caplog.text


def test_competing_live_session_is_reported_in_runtime_status() -> None:
    ibkr_module._IBKR_RUNTIME.quote_errors.clear()
    ibkr_module._IBKR_RUNTIME.api_errors.clear()
    ibkr_module._IBKR_RUNTIME.last_quote_error = None

    ibkr_module._record_quote_error(
        12,
        10197,
        "No market data during competing live session",
        SimpleNamespace(symbol="ES"),
    )

    status = ibkr_module.runtime_status()

    assert status["competing_session"] is True
    assert "competing market-data session" in status["live_issue"]
    assert status["recent_api_error_codes_60s"] == {"10197": 1}


def test_noisy_ibkr_api_stream_is_reported_in_runtime_status() -> None:
    ibkr_module._IBKR_RUNTIME.api_errors.clear()

    for index in range(10):
        ibkr_module._record_ibkr_api_error(
            "history",
            index,
            162,
            "Historical Market Data Service error message",
            SimpleNamespace(symbol="ES"),
        )

    status = ibkr_module.runtime_status()

    assert status["recent_api_error_count_60s"] == 10
    assert status["recent_api_error_codes_60s"] == {"162": 10}
    assert status["ibkr_flood_warning"].startswith("IBKR noisy stream")


def test_ibkr_runtime_status_separates_configured_requested_and_actual_market_data_types(
    monkeypatch,
) -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    quote_session = SimpleNamespace(
        isConnected=lambda: True,
        _aef_requested_market_data_type=3,
    )
    disconnected_history_session = SimpleNamespace(
        isConnected=lambda: False,
        _aef_requested_market_data_type=1,
    )
    quote_identity = ("ibkr|contract|756733", "exact-route")

    monkeypatch.setattr(runtime, "async_quote_session", quote_session)
    monkeypatch.setattr(runtime, "async_history_session", disconnected_history_session)
    monkeypatch.setattr(runtime, "async_chart_session", None)
    monkeypatch.setattr(runtime, "async_option_quote_session", None)
    monkeypatch.setattr(
        runtime,
        "quote_snapshots",
        {
            quote_identity: {
                "price": 100.1,
                "market_data_type": 1,
                "provider_symbol": "SPY",
            }
        },
    )
    monkeypatch.setattr(
        runtime,
        "quote_snapshot_routes",
        {quote_identity: "SPY"},
    )
    monkeypatch.setattr(runtime, "async_option_ticker_cache", {})
    monkeypatch.setattr(ibkr_quotes_module, "_market_data_type", lambda: 3)

    status = ibkr_module.runtime_status()

    assert "market_data_type" not in status
    assert status["configured_market_data_type"] == 3
    assert status["requested_market_data_types"] == {
        "history": None,
        "chart": None,
        "quote": 3,
        "option_quote": None,
    }
    assert status["actual_quote_market_data_types"] == [1]


def test_ibkr_runtime_status_keeps_healthy_lanes_live_for_transient_quarantine(
    monkeypatch,
) -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    baseline_quarantine = runtime.quarantined_sessions_snapshot()
    baseline_scopes = Counter(entry.session for entry in baseline_quarantine)
    baseline_persistent_sessions = sorted(
        {
            entry.session
            for entry in baseline_quarantine
            if entry.session
            in {
                "history",
                "chart",
                "quote",
                "option_quote",
                "tick_feed",
                "gex_live",
            }
        }
    )
    quote_session = SimpleNamespace(isConnected=lambda: True)
    quarantined_session = SimpleNamespace(isConnected=lambda: True)
    quote_identity = ("ibkr|contract|756733", "exact-route")
    monkeypatch.setattr(runtime, "async_quote_session", quote_session)
    monkeypatch.setattr(runtime, "async_history_session", None)
    monkeypatch.setattr(runtime, "async_chart_session", None)
    monkeypatch.setattr(runtime, "async_option_quote_session", None)
    monkeypatch.setattr(
        runtime,
        "quote_snapshots",
        {
            quote_identity: {
                "price": 100.1,
                "market_data_type": 1,
                "provider_symbol": "SPY",
            }
        },
    )
    monkeypatch.setattr(
        runtime,
        "quote_snapshot_routes",
        {quote_identity: "SPY"},
    )
    monkeypatch.setattr(runtime, "async_option_ticker_cache", {})
    runtime.retain_quarantined_session(
        quarantined_session,
        session="quote_probe",
        session_key=("127.0.0.1", 4002, 6107),
        connection_key=("127.0.0.1", 4002, 6107),
        owner_loop=None,
        owner_thread_id=None,
    )
    try:
        status = ibkr_module.runtime_status()

        assert status["status"] == "live"
        assert status["ok"] is True
        assert status["live_ok"] is True
        assert status["quote_connected"] is True
        assert status["quarantined_session_count"] == len(baseline_quarantine) + 1
        assert status["quarantined_session_scopes"]["quote_probe"] == (
            baseline_scopes["quote_probe"] + 1
        )
        assert status["quarantined_persistent_sessions"] == baseline_persistent_sessions
    finally:
        runtime.release_quarantined_session(quarantined_session)


def test_chart_error_dispatch_consumes_only_planned_cancel_acknowledgement() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.clear_planned_chart_cancels()
    delegated: list[tuple[int, int, str, str]] = []

    class Wrapper:
        def error(
            self,
            req_id: int,
            error_code: int,
            error_string: str,
            advanced_order_reject_json: str,
        ) -> None:
            delegated.append(
                (
                    req_id,
                    error_code,
                    error_string,
                    advanced_order_reject_json,
                )
            )

    wrapper = Wrapper()
    ibkr_session_module._install_chart_error_dispatch_policy(SimpleNamespace(wrapper=wrapper))
    try:
        runtime.mark_planned_chart_cancel(SimpleNamespace(reqId=47))

        wrapper.error(
            47,
            162,
            "Historical Market Data Service error message:API historical data query cancelled: 47",
            "",
        )

        assert delegated == []
        assert runtime.consume_planned_chart_cancel(47) is False
    finally:
        runtime.clear_planned_chart_cancels()


def test_chart_error_dispatch_delegates_unplanned_cancel() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.clear_planned_chart_cancels()
    delegated: list[tuple[int, int]] = []

    class Wrapper:
        def error(self, req_id, error_code, *_args) -> None:
            delegated.append((req_id, error_code))

    wrapper = Wrapper()
    ibkr_session_module._install_chart_error_dispatch_policy(SimpleNamespace(wrapper=wrapper))

    wrapper.error(
        48,
        162,
        "Historical Market Data Service error message:API historical data query cancelled: 48",
        "",
    )

    assert delegated == [(48, 162)]


def test_chart_error_dispatch_preserves_planned_marker_for_other_error_code() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.clear_planned_chart_cancels()
    delegated: list[tuple[int, int]] = []

    class Wrapper:
        def error(self, req_id, error_code, *_args) -> None:
            delegated.append((req_id, error_code))

    wrapper = Wrapper()
    ibkr_session_module._install_chart_error_dispatch_policy(SimpleNamespace(wrapper=wrapper))
    try:
        runtime.mark_planned_chart_cancel(SimpleNamespace(reqId=49))

        wrapper.error(49, 200, "unrelated broker error", "")

        assert delegated == [(49, 200)]
        assert runtime.consume_planned_chart_cancel(49) is True
    finally:
        runtime.clear_planned_chart_cancels()


def test_unplanned_chart_cancel_162_remains_recoverable_api_error() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    runtime.api_errors.clear()
    runtime.clear_planned_chart_cancels()

    ibkr_module._record_ibkr_api_error(
        "chart",
        48,
        162,
        "Historical Market Data Service error message:API historical data query cancelled: 48",
        SimpleNamespace(symbol="ES"),
    )

    assert [(source, code) for _, source, code, _, _ in runtime.api_errors] == [("chart", 162)]


def test_ibkr_history_recovery_advances_provider_connection_generation() -> None:
    runtime = ibkr_module._IBKR_RUNTIME
    original_errors = list(runtime.api_errors)
    original_generation = runtime.history_connection_generation_snapshot()
    try:
        ibkr_module._record_ibkr_api_error(
            "history",
            -1,
            1100,
            "Connectivity between IBKR and Trader Workstation has been lost.",
            None,
        )
        assert runtime.history_connection_generation_snapshot() == original_generation

        ibkr_module._record_ibkr_api_error(
            "history",
            -1,
            1102,
            "Connectivity between IBKR and Trader Workstation has been restored.",
            None,
        )

        assert runtime.history_connection_generation_snapshot() == original_generation + 1
        assert ibkr_module.runtime_status()["history_connection_generation"] == (
            original_generation + 1
        )
    finally:
        runtime.api_errors[:] = original_errors
        with runtime.connection_state_lock:
            runtime.history_connection_generation = original_generation


def test_connect_without_account_sync_cancels_account_updates() -> None:
    calls: list[tuple[str, tuple]] = []

    class FakeClient:
        def connect(self, *args, **kwargs):
            calls.append(("connect", args))

    class FakeIb:
        wrapper = SimpleNamespace()
        client = FakeClient()

        def reqMarketDataType(self, data_type):
            calls.append(("market_data_type", (data_type,)))

        def reqAccountUpdates(self, subscribe, account):
            calls.append(("account_updates", (subscribe, account)))

    ib = FakeIb()
    ibkr_module.connect_without_account_sync(ib, "127.0.0.1", 4002, 17, 1.0)

    assert ("account_updates", (False, "")) in calls
    assert ib._aef_requested_market_data_type == ibkr_module._market_data_type()


def test_connect_without_account_sync_closes_socket_when_market_data_type_request_fails() -> None:
    key = ("127.0.0.1", 4002, 6117)
    calls: list[str] = []

    class FakeClient:
        def connect(self, *_args, **_kwargs):
            calls.append("connect")

    class FakeIb:
        wrapper = SimpleNamespace()
        client = FakeClient()

        def reqMarketDataType(self, _data_type):
            raise RuntimeError("market-data-type rejected")

        def disconnect(self):
            calls.append("disconnect")

    ibkr_module._IBKR_RUNTIME.connect_backoff.pop(key, None)
    try:
        with pytest.raises(RuntimeError, match="market-data-type rejected"):
            ibkr_module.connect_without_account_sync(
                FakeIb(),
                key[0],
                key[1],
                key[2],
                1.0,
            )

        assert calls == ["connect", "disconnect"]
        assert key in ibkr_module._IBKR_RUNTIME.connect_backoff
    finally:
        ibkr_module._IBKR_RUNTIME.connect_backoff.pop(key, None)


def test_connect_without_account_sync_async_closes_socket_when_market_data_type_request_fails() -> (
    None
):
    key = ("127.0.0.1", 4002, 6217)
    calls: list[str] = []

    class FakeClient:
        async def connectAsync(self, *_args, **_kwargs):
            calls.append("connect")

    class FakeIb:
        wrapper = SimpleNamespace()
        client = FakeClient()

        def reqMarketDataType(self, _data_type):
            raise RuntimeError("async market-data-type rejected")

        def disconnect(self):
            calls.append("disconnect")

    ibkr_module._IBKR_RUNTIME.connect_backoff.pop(key, None)
    try:
        with pytest.raises(RuntimeError, match="async market-data-type rejected"):
            asyncio.run(
                ibkr_module.connect_without_account_sync_async(
                    FakeIb(),
                    key[0],
                    key[1],
                    key[2],
                    1.0,
                )
            )

        assert calls == ["connect", "disconnect"]
        assert key in ibkr_module._IBKR_RUNTIME.connect_backoff
    finally:
        ibkr_module._IBKR_RUNTIME.connect_backoff.pop(key, None)


def test_async_connect_admission_settles_physical_cleanup_before_cancellation(
    monkeypatch,
) -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    admission_started = threading.Event()
    admission_release = threading.Event()
    connect_called = False

    def blocking_admission(_key):
        admission_started.set()
        admission_release.wait(timeout=1.0)

    class FakeClient:
        async def connectAsync(self, *_args, **_kwargs):
            nonlocal connect_called
            connect_called = True

    ib = SimpleNamespace(wrapper=SimpleNamespace(), client=FakeClient())
    monkeypatch.setattr(
        ibkr_session_module,
        "_admit_ibkr_connection_key",
        blocking_admission,
    )

    async def run_probe():
        task = asyncio.create_task(
            ibkr_module.connect_without_account_sync_async(
                ib,
                "127.0.0.1",
                4002,
                6221,
                1.0,
            )
        )
        while not admission_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        admission_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_probe())
    assert connect_called is False


def test_cancelled_async_connect_closes_partially_opened_session() -> None:
    started = asyncio.Event()
    calls: list[str] = []

    class FakeClient:
        async def connectAsync(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        def disconnect(self):
            calls.append("client.disconnect")

    class FakeIb:
        wrapper = SimpleNamespace()
        client = FakeClient()

        def disconnect(self):
            calls.append("ib.disconnect")

    ib = FakeIb()

    async def run_probe():
        task = asyncio.create_task(
            ibkr_module.connect_without_account_sync_async(
                ib,
                "127.0.0.1",
                4002,
                6222,
                1.0,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_probe())
    assert calls == ["ib.disconnect", "client.disconnect"]
    assert ibkr_module._IBKR_RUNTIME.is_session_quarantined(ib) is False


def test_quarantine_cleanup_settles_physical_work_before_cancellation(
    monkeypatch,
) -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    cleanup_started = threading.Event()
    cleanup_release = threading.Event()

    def blocking_cleanup():
        cleanup_started.set()
        cleanup_release.wait(timeout=1.0)

    monkeypatch.setattr(
        ibkr_session_module,
        "_reset_ibkr_quarantined_sessions",
        blocking_cleanup,
    )

    async def run_probe():
        task = asyncio.create_task(ibkr_module.cleanup_quarantined_sessions_async(timeout=0.5))
        while not cleanup_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_probe())


def test_failed_local_session_is_quarantined_until_transport_close_succeeds() -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    key = ("127.0.0.1", 4002, 6227)

    class FakeClient:
        close_fails = True

        def connect(self, *_args, **_kwargs):
            return None

        def disconnect(self):
            if self.close_fails:
                raise RuntimeError("transport close failed")

    class FakeIb:
        wrapper = SimpleNamespace()
        client = FakeClient()

        def reqMarketDataType(self, _data_type):
            raise RuntimeError("market-data-type rejected")

        def disconnect(self):
            raise RuntimeError("session close failed")

    runtime = ibkr_module._IBKR_RUNTIME
    ib = FakeIb()
    runtime.connect_backoff.pop(key, None)
    try:
        with pytest.raises(RuntimeError, match="market-data-type rejected"):
            ibkr_module.connect_without_account_sync(
                ib,
                key[0],
                key[1],
                key[2],
                1.0,
            )

        entries = tuple(
            entry for entry in runtime.quarantined_sessions_snapshot() if entry.ib is ib
        )
        assert len(entries) == 1

        ib.client.close_fails = False
        assert ibkr_session_module._retry_quarantined_ibkr_session(entries[0])
        assert runtime.is_session_quarantined(ib) is False
    finally:
        runtime.release_quarantined_session(ib)
        runtime.connect_backoff.pop(key, None)


def test_disconnect_closes_live_transport_when_high_level_disconnect_is_noop() -> None:
    calls: list[str] = []

    class FakeTransport:
        closing = False

        def is_closing(self):
            return self.closing

    class FakeConnection:
        transport = FakeTransport()

    class FakeClient:
        conn = FakeConnection()

        def disconnect(self):
            calls.append("client.disconnect")
            self.conn.transport = None

    class FakeIb:
        client = FakeClient()

        def disconnect(self):
            calls.append("ib.disconnect")

    ib = FakeIb()

    assert (
        ibkr_module._disconnect_owned_ibkr_session(
            ib,
            session="test",
            session_key=("127.0.0.1", 4002, 6237),
        )
        is True
    )
    assert calls == ["ib.disconnect", "client.disconnect"]
    assert ibkr_module._IBKR_RUNTIME.is_session_quarantined(ib) is False


def test_disconnect_keeps_exact_key_reserved_while_transport_is_only_closing() -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    class FakeTransport:
        closing = False

        def is_closing(self):
            return self.closing

    connection = SimpleNamespace(transport=FakeTransport())

    def close_transport():
        connection.transport.closing = True

    session = SimpleNamespace(
        client=SimpleNamespace(conn=connection, disconnect=close_transport),
        disconnect=lambda: None,
        isConnected=lambda: True,
    )
    runtime = ibkr_module._IBKR_RUNTIME
    try:
        assert (
            ibkr_module._disconnect_owned_ibkr_session(
                session,
                session="quote_probe",
                session_key=("127.0.0.1", 4002, 6238),
            )
            is False
        )
        assert runtime.is_session_quarantined(session) is True
        assert ibkr_module._ibkr_session_ready(session) is False

        connection.transport = None
        entry = runtime.quarantined_sessions_for_connection(("127.0.0.1", 4002, 6238))[0]
        assert ibkr_session_module._retry_quarantined_ibkr_session(entry)
        assert runtime.is_session_quarantined(session) is False
    finally:
        runtime.release_quarantined_session(session)


def test_opaque_client_requires_direct_transport_disconnect_proof() -> None:
    calls: list[str] = []

    session = SimpleNamespace(
        client=SimpleNamespace(
            disconnect=lambda: calls.append("client.disconnect"),
        ),
        disconnect=lambda: calls.append("ib.disconnect"),
    )

    assert (
        ibkr_module._disconnect_owned_ibkr_session(
            session,
            session="test",
            session_key=("127.0.0.1", 4002, 6239),
        )
        is True
    )
    assert calls == ["ib.disconnect", "client.disconnect"]


def test_connect_fails_closed_while_exact_connection_key_is_quarantined() -> None:
    key = ("127.0.0.1", 4002, 6247)
    calls: list[str] = []

    class FakeTransport:
        def is_closing(self):
            return False

    class StuckConnection:
        transport = FakeTransport()

    class StuckClient:
        conn = StuckConnection()

        def connect(self, *_args, **_kwargs):
            calls.append("stuck.connect")

        def disconnect(self):
            calls.append("stuck.client.disconnect")

    class StuckIb:
        wrapper = SimpleNamespace()
        client = StuckClient()

        def reqMarketDataType(self, _data_type):
            raise RuntimeError("market-data-type rejected")

        def disconnect(self):
            calls.append("stuck.ib.disconnect")

        def isConnected(self):
            return True

    class ReplacementClient:
        def connect(self, *_args, **_kwargs):
            calls.append("replacement.connect")

    class ReplacementIb:
        wrapper = SimpleNamespace()
        client = ReplacementClient()

        def reqMarketDataType(self, _data_type):
            calls.append("replacement.market-data-type")

    runtime = ibkr_module._IBKR_RUNTIME
    stuck = StuckIb()
    replacement = ReplacementIb()
    runtime.connect_backoff.pop(key, None)
    try:
        with pytest.raises(RuntimeError, match="market-data-type rejected"):
            ibkr_module.connect_without_account_sync(
                stuck,
                key[0],
                key[1],
                key[2],
                1.0,
            )
        assert runtime.is_session_quarantined(stuck) is True
        assert ibkr_module._ibkr_session_ready(stuck) is False

        runtime.connect_backoff.pop(key, None)
        with pytest.raises(ConnectionError, match="quarantined session still owns"):
            ibkr_module.connect_without_account_sync(
                replacement,
                key[0],
                key[1],
                key[2],
                1.0,
            )
        assert "replacement.connect" not in calls

        stuck.client.conn.transport = None
        ibkr_module.connect_without_account_sync(
            replacement,
            key[0],
            key[1],
            key[2],
            1.0,
        )
        assert runtime.is_session_quarantined(stuck) is False
        assert "replacement.connect" in calls
    finally:
        runtime.release_quarantined_session(stuck)
        runtime.connect_backoff.pop(key, None)


def test_quarantine_retry_never_closes_active_transport_off_owner_lane() -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    calls: list[str] = []

    class FakeTransport:
        def is_closing(self):
            return False

    connection = SimpleNamespace(transport=FakeTransport())
    session = SimpleNamespace(
        client=SimpleNamespace(
            conn=connection,
            disconnect=lambda: calls.append("client.disconnect"),
        ),
        disconnect=lambda: calls.append("ib.disconnect"),
        _aef_connection_key=("127.0.0.1", 4002, 6257),
        _aef_owner_loop=None,
        _aef_owner_thread_id=threading.get_ident() + 1,
    )
    runtime = ibkr_module._IBKR_RUNTIME
    try:
        with pytest.raises(RuntimeError, match="owner event-loop/thread"):
            ibkr_module._disconnect_owned_ibkr_session(
                session,
                session="test",
                session_key=("127.0.0.1", 4002, 6257),
            )

        assert calls == []
        entry = runtime.quarantined_sessions_for_connection(("127.0.0.1", 4002, 6257))[0]
        assert entry.owner_thread_id == session._aef_owner_thread_id

        connection.transport = None
        assert ibkr_session_module._retry_quarantined_ibkr_session(entry)
        assert runtime.is_session_quarantined(session) is False
    finally:
        runtime.release_quarantined_session(session)


def test_quarantine_retry_pumps_stopped_owner_loop_from_another_worker() -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    owner_loop = asyncio.new_event_loop()
    calls: list[str] = []
    connection = SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: False))

    def close_transport():
        calls.append("client.disconnect")
        connection.transport = None

    session = SimpleNamespace(
        client=SimpleNamespace(conn=connection, disconnect=close_transport),
        disconnect=lambda: calls.append("ib.disconnect"),
        _aef_connection_key=("127.0.0.1", 4002, 6258),
        _aef_owner_loop=owner_loop,
        _aef_owner_thread_id=threading.get_ident() + 1,
    )
    runtime = ibkr_module._IBKR_RUNTIME
    try:
        with pytest.raises(RuntimeError, match="owner event-loop/thread"):
            ibkr_module._disconnect_owned_ibkr_session(
                session,
                session="instrument_search",
                session_key=("127.0.0.1", 4002, 6258),
            )
        assert calls == []

        entry = runtime.quarantined_sessions_for_connection(("127.0.0.1", 4002, 6258))[0]
        assert ibkr_session_module._retry_quarantined_ibkr_session(entry)

        assert calls == ["ib.disconnect", "client.disconnect"]
        assert runtime.is_session_quarantined(session) is False
    finally:
        runtime.release_quarantined_session(session)
        owner_loop.close()


def test_stopped_foreign_owner_loop_waits_for_quarantine_worker() -> None:
    from aef_terminal.data.ibkr import session as ibkr_session_module

    owner_loop = asyncio.new_event_loop()
    calls: list[str] = []
    connection = SimpleNamespace(transport=SimpleNamespace(is_closing=lambda: False))

    def close_transport():
        calls.append("client.disconnect")
        connection.transport = None

    session = SimpleNamespace(
        client=SimpleNamespace(conn=connection, disconnect=close_transport),
        disconnect=lambda: calls.append("ib.disconnect"),
        _aef_connection_key=("127.0.0.1", 4002, 6259),
        _aef_owner_loop=owner_loop,
        _aef_owner_thread_id=threading.get_ident() + 1,
    )
    runtime = ibkr_module._IBKR_RUNTIME
    try:
        with pytest.raises(RuntimeError, match="owner event-loop/thread"):
            ibkr_module._disconnect_owned_ibkr_session(
                session,
                session="instrument_search",
                session_key=("127.0.0.1", 4002, 6259),
            )
        entry = runtime.quarantined_sessions_for_connection(("127.0.0.1", 4002, 6259))[0]

        async def run_probe() -> None:
            with pytest.raises(RuntimeError, match="requires the quarantine worker"):
                ibkr_session_module._retry_quarantined_ibkr_session(entry)
            assert calls == []
            assert await asyncio.to_thread(
                ibkr_session_module._retry_quarantined_ibkr_session,
                entry,
            )

        asyncio.run(run_probe())

        assert calls == ["ib.disconnect", "client.disconnect"]
        assert runtime.is_session_quarantined(session) is False
    finally:
        runtime.release_quarantined_session(session)
        owner_loop.close()


def test_instrument_search_serializes_next_connect_through_prior_teardown(
    monkeypatch,
) -> None:
    from aef_terminal.data.ibkr import search as ibkr_search_module

    first_cleanup_started = threading.Event()
    release_first_cleanup = threading.Event()
    second_connected = threading.Event()
    connection_count = 0

    class FakeIb:
        def __init__(self, sequence):
            self.sequence = sequence

        def reqMatchingSymbols(self, _query):
            return []

    def fake_connect(*_args, **_kwargs):
        nonlocal connection_count
        connection_count += 1
        if connection_count == 2:
            second_connected.set()
        return FakeIb(connection_count)

    def fake_disconnect(ib, **_kwargs):
        if ib.sequence == 1:
            first_cleanup_started.set()
            release_first_cleanup.wait(timeout=1.0)
        return True

    monkeypatch.setattr(ibkr_search_module, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(
        ibkr_search_module,
        "_connect_lookup_session",
        fake_connect,
    )
    monkeypatch.setattr(
        ibkr_search_module,
        "_disconnect_owned_ibkr_session",
        fake_disconnect,
    )

    first = threading.Thread(
        target=lambda: ibkr_search_module.search_ibkr_instruments("SPY"),
        daemon=True,
    )
    second = threading.Thread(
        target=lambda: ibkr_search_module.search_ibkr_instruments("QQQ"),
        daemon=True,
    )
    first.start()
    assert first_cleanup_started.wait(timeout=1.0)
    second.start()
    try:
        assert second_connected.wait(timeout=0.05) is False
    finally:
        release_first_cleanup.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert second_connected.is_set() is True
    assert first.is_alive() is False
    assert second.is_alive() is False


def test_quote_session_requires_pending_ticker_event_hook(monkeypatch) -> None:
    disconnected: list[bool] = []
    backoff_key = ("127.0.0.1", 4002, 6317)

    class RejectingEvent:
        def __iadd__(self, _handler):
            raise RuntimeError("event registration rejected")

    class FakeIb:
        def __init__(self) -> None:
            self.errorEvent = _FakeIbEvent()
            self.pendingTickersEvent = RejectingEvent()

        def disconnect(self) -> None:
            disconnected.append(True)

    fake_ib = FakeIb()

    async def fake_connect(ib, *_args):
        ib._aef_requested_market_data_type = 1
        return ib

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.connect_backoff.pop(backoff_key, None)
    monkeypatch.setattr(runtime, "async_quote_session", None)
    monkeypatch.setattr(runtime, "async_quote_session_key", None)
    monkeypatch.setattr(
        ibkr_session_module,
        "connect_without_account_sync_async",
        fake_connect,
    )
    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(IB=lambda: fake_ib))

    with pytest.raises(
        RuntimeError,
        match="required event hooks could not be registered",
    ):
        asyncio.run(
            ibkr_module._connected_quote_ib_async(
                "127.0.0.1",
                4002,
                6317,
                True,
                1.0,
            )
        )

    assert disconnected == [True]
    assert runtime.async_quote_session is None
    assert runtime.connection_in_progress_snapshot()["quote"] is False
    assert backoff_key in runtime.connect_backoff
    runtime.connect_backoff.pop(backoff_key, None)


@pytest.mark.parametrize(
    (
        "session_name",
        "connector_name",
        "runtime_session_attr",
        "runtime_key_attr",
        "connection_state_name",
        "client_id",
    ),
    [
        (
            "history",
            "_connected_ib_async",
            "async_history_session",
            "async_history_session_key",
            "history",
            6321,
        ),
        (
            "chart",
            "_connected_chart_ib_async",
            "async_chart_session",
            "async_chart_session_key",
            "chart",
            6322,
        ),
        (
            "quote",
            "_connected_quote_ib_async",
            "async_quote_session",
            "async_quote_session_key",
            "quote",
            6323,
        ),
        (
            "option_quote",
            "_connected_option_quote_ib_async",
            "async_option_quote_session",
            "async_option_quote_session_key",
            "option_quote",
            6324,
        ),
    ],
)
def test_all_ibkr_sessions_abort_before_publish_when_required_error_hook_fails(
    monkeypatch,
    session_name,
    connector_name,
    runtime_session_attr,
    runtime_key_attr,
    connection_state_name,
    client_id,
) -> None:
    disconnected: list[bool] = []
    backoff_key = ("127.0.0.1", 4002, client_id)

    class RejectingEvent:
        def __iadd__(self, _handler):
            raise RuntimeError("required error hook rejected")

    class FakeIb:
        def __init__(self) -> None:
            self.errorEvent = RejectingEvent()
            self.pendingTickersEvent = _FakeIbEvent()
            self.wrapper = SimpleNamespace(error=lambda *_args: None)

        def disconnect(self) -> None:
            disconnected.append(True)

    fake_ib = FakeIb()

    async def fake_connect(ib, *_args):
        return ib

    runtime = ibkr_module._IBKR_RUNTIME
    runtime.connect_backoff.pop(backoff_key, None)
    monkeypatch.setattr(runtime, runtime_session_attr, None)
    monkeypatch.setattr(runtime, runtime_key_attr, None)
    if session_name == "chart":
        monkeypatch.setattr(runtime, "async_chart_streams", {})
    monkeypatch.setattr(
        ibkr_session_module,
        "connect_without_account_sync_async",
        fake_connect,
    )
    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(IB=lambda: fake_ib))

    try:
        with pytest.raises(
            RuntimeError,
            match="required event hooks could not be registered",
        ):
            asyncio.run(
                getattr(ibkr_module, connector_name)(
                    backoff_key[0],
                    backoff_key[1],
                    backoff_key[2],
                    True,
                    1.0,
                )
            )

        assert disconnected == [True]
        assert getattr(runtime, runtime_session_attr) is None
        assert runtime.connection_in_progress_snapshot()[connection_state_name] is False
        assert backoff_key in runtime.connect_backoff
        assert session_name in runtime.connect_backoff[backoff_key][1]
    finally:
        runtime.connect_backoff.pop(backoff_key, None)


def test_quote_sync_fails_before_subscription_mutation_when_market_data_type_switch_fails(
    monkeypatch,
) -> None:
    configured = 3
    cancellations: list[bool] = []
    previous_sync_at = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

    class FakeIb:
        _aef_requested_market_data_type = 1

        def reqMarketDataType(self, data_type):
            assert data_type == configured
            raise RuntimeError("market-data-type switch rejected")

    async def fake_connected(*_args):
        return FakeIb()

    instrument = ibkr_stock_payload("SPY", con_id=756733)
    normalized, instruments_by_route = ibkr_module._quote_inputs([instrument])
    runtime = ibkr_module._IBKR_RUNTIME
    monkeypatch.setattr(runtime, "last_quote_sync_at", previous_sync_at)
    monkeypatch.setattr(ibkr_quotes_module, "_market_data_type", lambda: configured)
    monkeypatch.setattr(ibkr_quotes_module, "_connected_quote_ib_async", fake_connected)
    monkeypatch.setattr(
        ibkr_quotes_module,
        "_cancel_stale_async_quote_subscriptions",
        lambda *_args: cancellations.append(True),
    )

    with pytest.raises(RuntimeError, match="could not apply requested market-data type 3"):
        asyncio.run(
            ibkr_module._sync_quote_subscriptions_async_owned(
                normalized,
                host="127.0.0.1",
                port=4002,
                client_id=6417,
                readonly=True,
                instruments_by_route=instruments_by_route,
            )
        )

    assert cancellations == []
    assert runtime.last_quote_sync_at == previous_sync_at


def test_quote_sync_resubscribes_existing_ticker_when_request_generation_changes(
    monkeypatch,
) -> None:
    configured = 3
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    normalized, instruments_by_route = ibkr_module._quote_inputs([instrument])
    route_key = normalized[0]
    cache_key = ibkr_module._quote_cache_key(
        "127.0.0.1",
        4002,
        6418,
        True,
        instrument=instrument,
    )
    contract = SimpleNamespace(symbol="SPY", secType="STK")
    previous_ticker = SimpleNamespace(
        contract=contract,
        tickerId=81,
        _aef_requested_market_data_type=1,
    )
    requested_types: list[int] = []
    cancellations: list[object] = []
    created_tickers: list[object] = []

    class FakeIb:
        _aef_requested_market_data_type = 1
        wrapper = SimpleNamespace(tickerIdMap={81: previous_ticker})

        def reqMarketDataType(self, data_type):
            requested_types.append(data_type)

        def cancelMktData(self, cancelled_contract):
            cancellations.append(cancelled_contract)
            return True

        def reqMktData(self, requested_contract, *_args):
            ticker = SimpleNamespace(
                contract=requested_contract,
                tickerId=82,
                marketPrice=lambda: 100.1,
            )
            created_tickers.append(ticker)
            return ticker

    ib = FakeIb()

    async def fake_connected(*_args):
        return ib

    async def fake_contract(*_args, **_kwargs):
        return contract

    runtime = ibkr_module._IBKR_RUNTIME
    monkeypatch.setattr(runtime, "async_ticker_cache", {cache_key: previous_ticker})
    monkeypatch.setattr(runtime, "async_quote_contract_cache", {cache_key: contract})
    monkeypatch.setattr(
        runtime,
        "async_quote_ticker_routes",
        {
            id(previous_ticker): {
                "provider_symbol": "SPY",
                "instrument_id": qualified_instrument_id(instrument),
                "route_fingerprint": route_key,
            }
        },
    )
    monkeypatch.setattr(runtime, "async_option_quote_keys", set())
    monkeypatch.setattr(runtime, "last_quote_wanted_keys", {cache_key})
    monkeypatch.setattr(ibkr_quotes_module, "_market_data_type", lambda: configured)
    monkeypatch.setattr(ibkr_quotes_module, "_connected_quote_ib_async", fake_connected)
    monkeypatch.setattr(
        ibkr_quotes_module,
        "_quote_contract_for_instrument_async_cache",
        fake_contract,
    )

    result = asyncio.run(
        ibkr_module._sync_quote_subscriptions_async_owned(
            normalized,
            host="127.0.0.1",
            port=4002,
            client_id=6418,
            readonly=True,
            instruments_by_route=instruments_by_route,
        )
    )

    assert result == {"requested": 1, "active": 1}
    assert requested_types == [configured]
    assert cancellations == [contract]
    assert len(created_tickers) == 1
    current_ticker = runtime.async_ticker_cache[cache_key]
    assert current_ticker is created_tickers[0]
    assert current_ticker._aef_requested_market_data_type == configured
    assert id(previous_ticker) not in runtime.async_quote_ticker_routes
    assert runtime.async_quote_ticker_routes[id(current_ticker)] == {
        "provider_symbol": "SPY",
        "instrument_id": qualified_instrument_id(instrument),
        "route_fingerprint": route_key,
    }


def test_quote_probe_preserves_typed_unavailable_result_when_socket_initialization_fails(
    monkeypatch,
) -> None:
    class FakeIb:
        errorEvent = _FakeIbEvent()

        def isConnected(self):
            return False

        def disconnect(self):
            return None

    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(IB=FakeIb))
    monkeypatch.setattr(
        ibkr_quotes_module,
        "connect_without_account_sync",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("market-data-type request rejected")),
    )

    instrument = ibkr_stock_payload("SPY", con_id=756733)
    route_key = route_fingerprint(instrument)
    result = ibkr_module.probe_quotes([instrument])

    assert result[route_key]["price"] is None
    assert result[route_key]["message"] == (
        "IBKR quote probe unavailable: market-data-type request rejected"
    )


@pytest.fixture(autouse=True)
def _enable_chart_live_lane_for_contract_tests(monkeypatch):
    from aef_terminal.data.ibkr import bars as ibkr_bars_module

    monkeypatch.setattr(ibkr_bars_module, "_CHART_LIVE_BARS_ENABLED", True)
