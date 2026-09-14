from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace

import pytest

from aef_terminal.data.adapters._tinvest import history
from aef_terminal.data.adapters._tinvest.history import (
    TInvestHistoryRequest,
    TInvestHistoryTerminal,
    fetch_tinvest_history,
    plan_tinvest_history_chunks,
)
from aef_terminal.domain import BarProviderRequest, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason


class _Interval(Enum):
    CANDLE_INTERVAL_1_MIN = 1
    CANDLE_INTERVAL_5_MIN = 2
    CANDLE_INTERVAL_15_MIN = 3
    CANDLE_INTERVAL_HOUR = 4


class _Source(Enum):
    CANDLE_SOURCE_UNSPECIFIED = 0
    CANDLE_SOURCE_EXCHANGE = 1
    CANDLE_SOURCE_INCLUDE_WEEKEND = 3


class _Request:
    def __init__(self, **kwargs: object) -> None:
        self.fields = dict(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)


class _MarketData:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    async def get_candles(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)


def _request(
    *,
    timeframe: str = "5m",
    starts_at: datetime = datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
    ends_at: datetime = datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
) -> TInvestHistoryRequest:
    return TInvestHistoryRequest(
        instrument_uid=" Exact-Uid ",
        instrument_id="tinvest|contract| Exact-Uid ",
        route_fingerprint="tinvest|contract| Exact-Uid ",
        provider_symbol=" Exact-Uid ",
        timeframe=timeframe,
        starts_at=starts_at,
        ends_at=ends_at,
    )


def _quotation(value: float) -> SimpleNamespace:
    units = int(value)
    nano = round((value - units) * 1_000_000_000)
    return SimpleNamespace(units=units, nano=nano)


def _candle(
    ts: datetime,
    *,
    complete: bool = True,
    source: _Source = _Source.CANDLE_SOURCE_EXCHANGE,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.5,
    volume: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        time=ts,
        open=_quotation(open_),
        high=_quotation(high),
        low=_quotation(low),
        close=_quotation(close),
        volume=volume,
        is_complete=complete,
        candle_source=source,
    )


@pytest.fixture(autouse=True)
def _fake_sdk_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        history,
        "_sdk_history_contracts",
        lambda: (_Request, _Interval, _Source),
    )


def test_history_request_preserves_exact_identity_and_normalizes_only_time_zone() -> None:
    plus_three = timezone(timedelta(hours=3))
    request = _request(
        starts_at=datetime(2026, 8, 6, 10, 0, tzinfo=plus_three),
        ends_at=datetime(2026, 8, 6, 11, 0, tzinfo=plus_three),
    )

    assert request.instrument_uid == " Exact-Uid "
    assert request.provider_symbol == " Exact-Uid "
    assert request.starts_at == datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
    assert request.ends_at == datetime(2026, 8, 6, 8, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="TINVEST_HISTORY_TIMEFRAME_UNSUPPORTED"):
        _request(timeframe="1h")
    with pytest.raises(ValueError, match="TINVEST_HISTORY_START_UNALIGNED"):
        _request(starts_at=datetime(2026, 8, 6, 7, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="TINVEST_HISTORY_RANGE_INVALID"):
        _request(
            starts_at=datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
            ends_at=datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
        )

    one_slot = _request(
        starts_at=datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
        ends_at=datetime(2026, 8, 6, 7, 5, tzinfo=UTC),
    )
    assert one_slot.ends_at - one_slot.starts_at == timedelta(minutes=5)


def test_history_chunks_are_exact_non_overlapping_and_preserve_one_slot_tail() -> None:
    request = _request(
        timeframe="60m",
        starts_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
        ends_at=datetime(2026, 8, 2, 1, 0, tzinfo=UTC),
    )

    chunks = plan_tinvest_history_chunks(request)

    assert [(chunk.starts_at, chunk.ends_at) for chunk in chunks] == [
        (
            datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 2, 0, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 2, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 2, 1, 0, tzinfo=UTC),
        ),
    ]
    assert all(chunk.ends_at - chunk.starts_at <= timedelta(days=1) for chunk in chunks)
    assert all(chunk.request_limit is None for chunk in chunks)
    assert all(chunk.provider_limit == 2_400 for chunk in chunks)


def test_complete_history_uses_exact_exchange_request_without_limit_and_admits_flat_bar() -> None:
    request = _request()
    market_data = _MarketData(
        [
            SimpleNamespace(
                candles=[
                    _candle(
                        datetime(2026, 8, 6, 7, 5, tzinfo=UTC),
                        open_=100.0,
                        high=100.0,
                        low=100.0,
                        close=100.0,
                        volume=0,
                    ),
                    _candle(datetime(2026, 8, 6, 7, 0, tzinfo=UTC)),
                ]
            )
        ]
    )

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=market_data),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.COMPLETE
    assert result.response_count == 2
    assert result.authoritative_count == 2
    assert result.provisional_count == 0
    assert [bar.ts for bar in result.authoritative_bars] == [
        datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
        datetime(2026, 8, 6, 7, 5, tzinfo=UTC),
    ]
    flat = result.authoritative_bars[1]
    assert flat.volume == 0.0
    assert flat.state is BarState.CONFIRMED
    assert flat.provenance is not None
    assert flat.provenance.provider == "tinvest"
    assert flat.provenance.instrument_id == request.instrument_id
    assert flat.provenance.route_fingerprint == request.route_fingerprint
    assert flat.provenance.request_type is BarProviderRequest.HISTORICAL
    assert flat.provenance.provider_contract_id == request.instrument_uid
    assert flat.provenance.provider_contract_type == "INSTRUMENT_UID"
    assert flat.provenance.data_type == "TRADES"
    assert (
        authoritative_bar_reject_reason(
            "tinvest",
            flat,
            instrument_id=request.instrument_id,
            route_fingerprint=request.route_fingerprint,
            provider_contract_id=request.instrument_uid,
            provider_contract_type="INSTRUMENT_UID",
            data_type="TRADES",
        )
        is None
    )

    sdk_request = market_data.requests[0]
    assert sdk_request.instrument_id == request.instrument_uid
    assert sdk_request.from_ == request.starts_at
    assert sdk_request.to == request.ends_at
    assert sdk_request.interval is _Interval.CANDLE_INTERVAL_5_MIN
    assert sdk_request.candle_source_type is _Source.CANDLE_SOURCE_EXCHANGE
    assert "limit" not in sdk_request.fields
    assert result.chunks[0].request.source == "EXCHANGE"
    assert result.chunks[0].request.request_limit is None


def test_history_rejects_untyped_integer_exchange_source() -> None:
    request = _request()
    candle = _candle(datetime(2026, 8, 6, 7, 0, tzinfo=UTC))
    candle.candle_source = _Source.CANDLE_SOURCE_EXCHANGE.value

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=_MarketData([SimpleNamespace(candles=[candle])])),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.MALFORMED
    assert result.error_code == "TINVEST_HISTORY_SOURCE_INVALID"
    assert result.response_count == 1


def test_empty_success_is_typed_complete_with_exact_cardinality() -> None:
    request = _request()
    market_data = _MarketData([SimpleNamespace(candles=[])])

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=market_data),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.COMPLETE
    assert result.response_count == 0
    assert result.authoritative_bars == ()
    assert result.provisional_bars == ()
    assert result.chunks[0].response_count == 0


def test_incomplete_response_separates_authoritative_and_provisional_bars() -> None:
    request = _request()
    market_data = _MarketData(
        [
            SimpleNamespace(
                candles=[
                    _candle(datetime(2026, 8, 6, 7, 0, tzinfo=UTC)),
                    _candle(
                        datetime(2026, 8, 6, 7, 5, tzinfo=UTC),
                        complete=False,
                    ),
                ]
            )
        ]
    )

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=market_data),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.INCOMPLETE
    assert result.authoritative_count == 1
    assert result.provisional_count == 1
    provisional = result.provisional_bars[0]
    assert provisional.closed is False
    assert provisional.state is BarState.FORMING
    assert authoritative_bar_reject_reason("tinvest", provisional) == "provisional_bar"
    assert result.chunks[0].terminal is TInvestHistoryTerminal.INCOMPLETE


@pytest.mark.parametrize(
    ("candles", "error_code"),
    [
        (
            [
                _candle(datetime(2026, 8, 6, 7, 0, tzinfo=UTC)),
                _candle(datetime(2026, 8, 6, 7, 0, tzinfo=UTC)),
            ],
            "TINVEST_HISTORY_DUPLICATE_TIMESTAMP",
        ),
        (
            [_candle(datetime(2026, 8, 6, 8, 0, tzinfo=UTC))],
            "TINVEST_HISTORY_TIMESTAMP_OUT_OF_RANGE",
        ),
        (
            [_candle(datetime(2026, 8, 6, 7, 1, tzinfo=UTC))],
            "TINVEST_HISTORY_TIMESTAMP_UNALIGNED",
        ),
        (
            [
                _candle(
                    datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
                    source=_Source.CANDLE_SOURCE_INCLUDE_WEEKEND,
                )
            ],
            "TINVEST_HISTORY_SOURCE_NOT_EXCHANGE",
        ),
        (
            [
                _candle(
                    datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
                    high=99.0,
                    low=101.0,
                )
            ],
            "TINVEST_HISTORY_CANDLE_INVALID",
        ),
    ],
)
def test_malformed_elements_fail_the_whole_response_closed(
    candles: list[object],
    error_code: str,
) -> None:
    request = _request()
    market_data = _MarketData([SimpleNamespace(candles=candles)])

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=market_data),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.MALFORMED
    assert result.error_code == error_code
    assert result.authoritative_bars == ()
    assert result.provisional_bars == ()
    assert result.chunks[-1].terminal is TInvestHistoryTerminal.MALFORMED
    assert result.chunks[-1].response_count == len(candles)


def test_invalid_response_shape_is_typed_malformed_with_unknown_cardinality() -> None:
    request = _request()
    market_data = _MarketData([SimpleNamespace(candles=None)])

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=market_data),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.MALFORMED
    assert result.error_code == "TINVEST_HISTORY_RESPONSE_INVALID"
    assert result.response_count is None


def test_multi_chunk_history_preserves_exact_union_and_cardinality() -> None:
    request = _request(
        timeframe="60m",
        starts_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
        ends_at=datetime(2026, 8, 2, 1, 0, tzinfo=UTC),
    )
    market_data = _MarketData(
        [
            SimpleNamespace(candles=[_candle(datetime(2026, 8, 1, 0, 0, tzinfo=UTC))]),
            SimpleNamespace(candles=[_candle(datetime(2026, 8, 2, 0, 0, tzinfo=UTC))]),
        ]
    )

    result = asyncio.run(
        fetch_tinvest_history(
            SimpleNamespace(market_data=market_data),
            request,
            timeout=1.0,
        )
    )

    assert result.terminal is TInvestHistoryTerminal.COMPLETE
    assert result.response_count == 2
    assert [(item.from_, item.to) for item in market_data.requests] == [
        (request.starts_at, datetime(2026, 8, 2, 0, 0, tzinfo=UTC)),
        (datetime(2026, 8, 2, 0, 0, tzinfo=UTC), request.ends_at),
    ]
    assert all("limit" not in item.fields for item in market_data.requests)
