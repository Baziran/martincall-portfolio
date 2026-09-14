from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any

import pytest

import aef_terminal.data.adapters._tinvest.quotes as quotes
from aef_terminal.data.adapters._tinvest.quotes import (
    TInvestQuoteError,
    TInvestQuotePollingRuntime,
    fetch_tinvest_live_quotes,
)
from aef_terminal.ui.quote_helpers import quote_envelope


class _LastPriceType(Enum):
    LAST_PRICE_UNSPECIFIED = 0
    LAST_PRICE_EXCHANGE = 1
    LAST_PRICE_DEALER = 2


class _GetLastPricesRequest:
    calls: list[dict[str, Any]] = []

    def __init__(
        self,
        *,
        instrument_id: list[str],
        last_price_type: _LastPriceType,
    ) -> None:
        payload = {
            "instrument_id": list(instrument_id),
            "last_price_type": last_price_type,
        }
        type(self).calls.append(payload)
        self.instrument_id = list(instrument_id)
        self.last_price_type = last_price_type


class _MarketData:
    def __init__(self, response: object, *, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.calls: list[object] = []
        self.completed = False

    async def get_last_prices(self, request: object) -> object:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.completed = True
        return self.response


def _instrument(uid: str, *, asset_class: str = "stock") -> dict[str, Any]:
    return {
        "instrument_id": f"tinvest|contract|{uid}",
        "instrument_key": f"key:{uid}",
        "provider": "tinvest",
        "provider_symbol": uid,
        "provider_contract_id": uid,
        "asset_class": asset_class,
        "contract_identity": {
            "provider": "tinvest",
            "provider_contract_id": uid,
            "instrument_uid": uid,
            "asset_class": asset_class,
            "identity_scope": "contract",
        },
    }


def _last_price(
    uid: object,
    *,
    units: object = 100,
    nano: object = 250_000_000,
    observed_at: object = datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    last_price_type: object = _LastPriceType.LAST_PRICE_EXCHANGE,
) -> object:
    return SimpleNamespace(
        instrument_uid=uid,
        price=SimpleNamespace(units=units, nano=nano),
        time=observed_at,
        last_price_type=last_price_type,
    )


def _services(response: object, *, delay: float = 0.0) -> tuple[object, _MarketData]:
    market_data = _MarketData(response, delay=delay)
    return SimpleNamespace(market_data=market_data), market_data


def _install_sdk_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    _GetLastPricesRequest.calls.clear()
    monkeypatch.setattr(
        quotes,
        "_sdk_last_price_contracts",
        lambda: (_GetLastPricesRequest, _LastPriceType),
    )


def test_batch_uses_exact_uid_exchange_request_and_maps_reordered_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_contract(monkeypatch)
    exact_uid = " Exact UID "
    second_uid = "uid-B"
    instruments = [_instrument(exact_uid), _instrument(second_uid, asset_class="future")]
    provider_time = datetime(2026, 8, 13, 13, 0, tzinfo=timezone(timedelta(hours=3)))
    response = SimpleNamespace(
        last_prices=[
            _last_price(second_uid, units=-1, nano=-500_000_000, observed_at=provider_time),
            _last_price(exact_uid, observed_at=provider_time),
        ]
    )
    services, market_data = _services(response)
    received_at = datetime(2026, 8, 13, 10, 0, 1, tzinfo=UTC)
    clock_calls = 0

    def receipt_clock() -> datetime:
        nonlocal clock_calls
        assert market_data.completed is True
        clock_calls += 1
        return received_at

    live_map, warning = asyncio.run(
        fetch_tinvest_live_quotes(
            services,
            instruments,
            timeout=0.5,
            receipt_clock=receipt_clock,
        )
    )

    assert warning == ""
    assert clock_calls == 1
    assert len(market_data.calls) == 1
    request = market_data.calls[0]
    assert isinstance(request, _GetLastPricesRequest)
    assert _GetLastPricesRequest.calls == [
        {
            "instrument_id": [exact_uid, second_uid],
            "last_price_type": _LastPriceType.LAST_PRICE_EXCHANGE,
        }
    ]
    assert request.instrument_id == [exact_uid, second_uid]
    assert request.last_price_type is _LastPriceType.LAST_PRICE_EXCHANGE
    assert set(live_map) == {
        f"tinvest|contract|{exact_uid}",
        f"tinvest|contract|{second_uid}",
    }
    first = live_map[f"tinvest|contract|{exact_uid}"]
    assert first == {
        "instrument_id": f"tinvest|contract|{exact_uid}",
        "route_fingerprint": f"tinvest|contract|{exact_uid}",
        "provider_symbol": exact_uid,
        "provider_contract_id": exact_uid,
        "instrument_uid": exact_uid,
        "provider": "tinvest",
        "price": 100.25,
        "price_source": "last",
        "last": 100.25,
        "bid": None,
        "ask": None,
        "ts": "2026-08-13T10:00:00+00:00",
        "provider_ts": "2026-08-13T10:00:00+00:00",
        "last_provider_ts": "2026-08-13T10:00:00+00:00",
        "received_at": "2026-08-13T10:00:01+00:00",
        "bid_ask_received_at": None,
        "time_basis": "provider_event",
        "source": "tinvest:last-price-exchange",
        "entitlement": "broker",
        "market_data_entitlement": "live",
        "is_delayed": False,
        "contract": exact_uid,
        "message": "",
    }
    assert live_map[f"tinvest|contract|{second_uid}"]["price"] == -1.5
    envelope = quote_envelope(
        first,
        received_at=received_at,
        stale_after_seconds=5.0,
    )
    assert envelope["status"] == "live"
    assert envelope["is_stale"] is False
    assert envelope["price"] == envelope["last"] == 100.25
    assert envelope["time_basis"] == "provider_event"


def test_repeated_provider_event_keeps_provider_time_and_stamps_each_real_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_contract(monkeypatch)
    instrument = _instrument("uid-1")
    provider_time = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    response = SimpleNamespace(last_prices=[_last_price("uid-1", observed_at=provider_time)])
    services, _market_data = _services(response)
    receipts = iter(
        (
            datetime(2026, 8, 13, 10, 0, 1, tzinfo=UTC),
            datetime(2026, 8, 13, 10, 0, 2, tzinfo=UTC),
        )
    )

    first, _warning = asyncio.run(
        fetch_tinvest_live_quotes(services, [instrument], receipt_clock=lambda: next(receipts))
    )
    second, _warning = asyncio.run(
        fetch_tinvest_live_quotes(services, [instrument], receipt_clock=lambda: next(receipts))
    )

    first_row = first[instrument["instrument_id"]]
    second_row = second[instrument["instrument_id"]]
    assert first_row["ts"] == second_row["ts"] == provider_time.isoformat()
    assert first_row["provider_ts"] == second_row["provider_ts"] == provider_time.isoformat()
    assert first_row["last_provider_ts"] == second_row["last_provider_ts"]
    assert first_row["received_at"] == "2026-08-13T10:00:01+00:00"
    assert second_row["received_at"] == "2026-08-13T10:00:02+00:00"
    stale_envelope = quote_envelope(
        second_row,
        received_at=datetime(2026, 8, 13, 10, 0, 2, tzinfo=UTC),
        stale_after_seconds=0.5,
    )
    assert stale_envelope["status"] == "stale"
    assert stale_envelope["is_stale"] is True
    assert stale_envelope["age_seconds"] == 2.0


@pytest.mark.parametrize(
    "response",
    [
        None,
        object(),
        SimpleNamespace(last_prices=None),
        SimpleNamespace(last_prices="not-a-sequence"),
    ],
)
def test_malformed_response_container_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(response)

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_RESPONSE_INVALID"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


def test_omitted_uid_keeps_exact_rows_and_reports_the_missing_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(SimpleNamespace(last_prices=[_last_price("uid-1")]))

    live_map, warning = asyncio.run(
        fetch_tinvest_live_quotes(
            services,
            [_instrument("uid-1"), _instrument("uid-2")],
        )
    )

    assert set(live_map) == {"tinvest|contract|uid-1"}
    assert live_map["tinvest|contract|uid-1"]["instrument_uid"] == "uid-1"
    assert warning.endswith("exact instrument UID(s): uid-2")


def test_duplicate_uid_fails_the_whole_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1"), _last_price("uid-1")])
    )

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_UID_DUPLICATE.*uid-1"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


def test_wrong_uid_never_falls_back_to_figi_or_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sdk_contract(monkeypatch)
    wrong = _last_price("wrong-uid")
    wrong.figi = "uid-1"
    wrong.ticker = "uid-1"
    services, _market_data = _services(SimpleNamespace(last_prices=[wrong]))

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_UID_UNEXPECTED.*wrong-uid"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


@pytest.mark.parametrize(
    ("units", "nano"),
    [
        (True, 0),
        ("1", 0),
        (1, True),
        (1, 1_000_000_000),
        (1, -1),
        (-1, 1),
        (10**400, 0),
    ],
)
def test_invalid_quotation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    units: object,
    nano: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1", units=units, nano=nano)])
    )

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_QUOTATION_INVALID"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


@pytest.mark.parametrize(
    "observed_at",
    [
        None,
        "2026-08-13T10:00:00Z",
        datetime(2026, 8, 13, 10, 0),
    ],
)
def test_invalid_provider_timestamp_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    observed_at: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1", observed_at=observed_at)])
    )

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_TIMESTAMP_INVALID"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


@pytest.mark.parametrize(
    "last_price_type",
    [
        None,
        1,
        _LastPriceType.LAST_PRICE_UNSPECIFIED,
        _LastPriceType.LAST_PRICE_DEALER,
    ],
)
def test_non_exchange_or_untyped_price_type_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    last_price_type: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1", last_price_type=last_price_type)])
    )

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_PRICE_TYPE_INVALID"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


@pytest.mark.parametrize("uid", [None, 1, ""])
def test_invalid_response_uid_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    uid: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(SimpleNamespace(last_prices=[_last_price(uid)]))

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_UID_INVALID"):
        asyncio.run(fetch_tinvest_live_quotes(services, [_instrument("uid-1")]))


@pytest.mark.parametrize(
    "received_at",
    [
        None,
        "2026-08-13T10:00:00Z",
        datetime(2026, 8, 13, 10, 0),
    ],
)
def test_invalid_receipt_timestamp_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    received_at: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(SimpleNamespace(last_prices=[_last_price("uid-1")]))

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_RECEIVED_AT_INVALID"):
        asyncio.run(
            fetch_tinvest_live_quotes(
                services,
                [_instrument("uid-1")],
                receipt_clock=lambda: received_at,  # type: ignore[return-value]
            )
        )


@pytest.mark.parametrize("field", ["provider_symbol", "instrument_uid"])
def test_route_uid_mismatch_is_rejected_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    instrument = _instrument("uid-1")
    if field == "provider_symbol":
        instrument[field] = "alias"
    else:
        instrument["contract_identity"][field] = "alias"
    services, market_data = _services(SimpleNamespace(last_prices=[_last_price("uid-1")]))
    monkeypatch.setattr(
        quotes,
        "_sdk_last_price_contracts",
        lambda: pytest.fail("SDK request must not be constructed for an invalid route"),
    )

    with pytest.raises(TInvestQuoteError, match="TINVEST_QUOTE_ROUTE_UID_MISMATCH"):
        asyncio.run(fetch_tinvest_live_quotes(services, [instrument]))
    assert market_data.calls == []


def test_provider_call_is_bounded_by_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sdk_contract(monkeypatch)
    services, _market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1")]),
        delay=0.05,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(
            fetch_tinvest_live_quotes(
                services,
                [_instrument("uid-1")],
                timeout=0.001,
            )
        )


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), "1"])
def test_invalid_timeout_is_rejected_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
    timeout: object,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, market_data = _services(SimpleNamespace(last_prices=[_last_price("uid-1")]))

    with pytest.raises(ValueError, match="TINVEST_QUOTE_TIMEOUT_INVALID"):
        asyncio.run(
            fetch_tinvest_live_quotes(
                services,
                [_instrument("uid-1")],
                timeout=timeout,  # type: ignore[arg-type]
            )
        )
    assert market_data.calls == []


def test_polling_runtime_reuses_one_service_transport_until_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_contract(monkeypatch)
    services, market_data = _services(SimpleNamespace(last_prices=[_last_price("uid-1")]))
    opened_tokens: list[object] = []
    exits = 0

    @asynccontextmanager
    async def open_services(token: object):
        nonlocal exits
        opened_tokens.append(token)
        try:
            yield services
        finally:
            exits += 1

    monkeypatch.setattr(quotes, "open_tinvest_services", open_services)
    runtime = TInvestQuotePollingRuntime("secret-token", timeout=0.5)

    async def scenario() -> tuple[dict[str, dict[str, Any]], str]:
        first = await runtime.fetch_quotes([_instrument("uid-1")])
        second = await runtime.fetch_quotes([_instrument("uid-1")])
        assert first[1] == second[1] == ""
        assert first[0]["tinvest|contract|uid-1"]["price"] == 100.25
        assert second[0]["tinvest|contract|uid-1"]["price"] == 100.25
        assert exits == 0
        await runtime.aclose()
        return second

    live_map, warning = asyncio.run(scenario())

    assert opened_tokens == ["secret-token"]
    assert len(market_data.calls) == 2
    assert exits == 1
    assert warning == ""
    assert live_map["tinvest|contract|uid-1"]["price"] == 100.25


def test_polling_runtime_timeout_closes_transport_before_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_contract(monkeypatch)
    slow_services, slow_market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1")]),
        delay=0.05,
    )
    ready_services, ready_market_data = _services(
        SimpleNamespace(last_prices=[_last_price("uid-1")])
    )
    services = iter((slow_services, ready_services))
    opens = 0
    exits = 0

    @asynccontextmanager
    async def open_services(_token: object):
        nonlocal opens, exits
        opens += 1
        try:
            yield next(services)
        finally:
            exits += 1

    monkeypatch.setattr(quotes, "open_tinvest_services", open_services)
    runtime = TInvestQuotePollingRuntime("secret-token", timeout=0.001)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await runtime.fetch_quotes([_instrument("uid-1")])
        assert slow_market_data.completed is False
        assert (opens, exits) == (1, 1)
        live_map, warning = await runtime.fetch_quotes([_instrument("uid-1")])
        assert warning == ""
        assert live_map["tinvest|contract|uid-1"]["price"] == 100.25
        assert ready_market_data.completed is True
        assert (opens, exits) == (2, 1)
        await runtime.aclose()

    asyncio.run(scenario())
    assert (opens, exits) == (2, 2)


def test_polling_runtime_cancellation_drains_rpc_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sdk_contract(monkeypatch)
    started = asyncio.Event()
    rpc_cancelled = asyncio.Event()
    transport_closed = asyncio.Event()

    class BlockingMarketData:
        async def get_last_prices(self, _request: object) -> object:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                rpc_cancelled.set()
                raise

    services = SimpleNamespace(market_data=BlockingMarketData())

    @asynccontextmanager
    async def open_services(_token: object):
        try:
            yield services
        finally:
            transport_closed.set()

    monkeypatch.setattr(quotes, "open_tinvest_services", open_services)
    runtime = TInvestQuotePollingRuntime("secret-token")

    async def scenario() -> None:
        task = asyncio.create_task(runtime.fetch_quotes([_instrument("uid-1")]))
        await asyncio.wait_for(started.wait(), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rpc_cancelled.is_set()
        assert transport_closed.is_set()
        await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), "1"])
def test_polling_runtime_rejects_invalid_timeout_before_opening_services(
    monkeypatch: pytest.MonkeyPatch,
    timeout: object,
) -> None:
    monkeypatch.setattr(
        quotes,
        "open_tinvest_services",
        lambda _token: pytest.fail("invalid timeout must not open provider services"),
    )

    with pytest.raises(ValueError, match="TINVEST_QUOTE_TIMEOUT_INVALID"):
        TInvestQuotePollingRuntime(
            "secret-token",
            timeout=timeout,  # type: ignore[arg-type]
        )


def test_polling_runtime_empty_batch_does_not_open_transport_or_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        quotes,
        "open_tinvest_services",
        lambda _token: pytest.fail("empty batches must not open provider services"),
    )
    monkeypatch.setattr(
        quotes,
        "_sdk_last_price_contracts",
        lambda: pytest.fail("empty batches must not construct an SDK request"),
    )

    runtime = TInvestQuotePollingRuntime("unused")
    assert asyncio.run(runtime.fetch_quotes([])) == ({}, "")
    assert asyncio.run(
        fetch_tinvest_live_quotes(
            SimpleNamespace(),
            [],
            receipt_clock=lambda: pytest.fail("empty batches must not stamp receipt time"),
        )
    ) == ({}, "")
