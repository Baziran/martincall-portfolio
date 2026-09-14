from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

import aef_terminal.data.adapters._coinbase.history as coinbase_history
import aef_terminal.data.adapters.coinbase as coinbase_adapter
from aef_terminal.data.adapters._coinbase.history import (
    CoinbaseHistoryRequest,
    CoinbaseHistoryResult,
    CoinbaseHistoryTerminal,
    coinbase_history_request_max_span,
    fetch_coinbase_chart_tail,
    fetch_coinbase_history,
)
from aef_terminal.data.adapters.coinbase import CoinbaseDataProvider
from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.data.provider_contract import (
    HistoryRepairIntent,
    HistoryRepairPriority,
    ProviderHistoryTerminal,
)
from aef_terminal.domain import BarState
from tests.provider_payloads import coinbase_btc_payload


_START = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _request(*, starts_at: datetime = _START, ends_at: datetime | None = None):
    return CoinbaseHistoryRequest(
        product_id="BTC-USD",
        instrument_id="coinbase|contract|BTC-USD",
        route_fingerprint="coinbase|contract|BTC-USD",
        provider_symbol="BTC-USD",
        timeframe="5m",
        starts_at=starts_at,
        ends_at=ends_at or starts_at + timedelta(minutes=10),
    )


def _row(ts: datetime, price: float) -> list[float]:
    return [
        ts.timestamp(),
        price - 2.0,
        price + 2.0,
        price - 1.0,
        price + 1.0,
        10.0,
    ]


def test_coinbase_bounded_target_is_terminal_without_successor_probe(monkeypatch) -> None:
    request = _request()
    calls: list[tuple[datetime, datetime]] = []

    def request_candles(
        _product_id,
        *,
        starts_at,
        ends_at,
        timeframe,
        timeout,
    ):
        assert timeframe == "5m"
        assert 0 < timeout <= 2.0
        calls.append((starts_at, ends_at))
        return [
            _row(request.starts_at + timedelta(minutes=5), 102.0),
            _row(request.starts_at, 100.0),
        ]

    monkeypatch.setattr(coinbase_history, "_request_candles", request_candles)

    result = fetch_coinbase_history(
        request,
        timeout=2.0,
    )

    assert result.terminal is CoinbaseHistoryTerminal.COMPLETE
    assert result.response_count == 2
    assert [bar.ts for bar in result.authoritative_bars] == [
        request.starts_at,
        request.starts_at + timedelta(minutes=5),
    ]
    assert all(bar.state is BarState.CONFIRMED and bar.closed for bar in result.authoritative_bars)
    assert result.provisional_bars == ()
    assert calls == [(request.starts_at, request.ends_at)]


def test_coinbase_empty_exact_response_is_complete_without_successor(monkeypatch) -> None:
    calls = 0

    def request_candles(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(coinbase_history, "_request_candles", request_candles)

    result = fetch_coinbase_history(_request(), timeout=1.0)

    assert result.terminal is CoinbaseHistoryTerminal.COMPLETE
    assert result.response_count == 0
    assert result.authoritative_bars == ()
    assert result.provisional_bars == ()
    assert calls == 1


def test_coinbase_nonempty_unsaturated_response_keeps_all_returned_bars(
    monkeypatch,
) -> None:
    request = _request()

    def request_candles(_product_id, **_kwargs):
        return [
            _row(request.starts_at, 100.0),
            _row(request.starts_at + timedelta(minutes=5), 102.0),
        ]

    monkeypatch.setattr(coinbase_history, "_request_candles", request_candles)

    result = fetch_coinbase_history(
        request,
        timeout=1.0,
    )

    assert result.terminal is CoinbaseHistoryTerminal.COMPLETE
    assert [bar.ts for bar in result.authoritative_bars] == [
        request.starts_at,
        request.starts_at + timedelta(minutes=5),
    ]
    assert result.error_code is None


def test_coinbase_saturated_or_out_of_range_response_is_malformed(monkeypatch) -> None:
    request = _request(
        ends_at=_START + coinbase_history_request_max_span("5m"),
    )
    saturated = [
        _row(request.starts_at + timedelta(minutes=5 * index), 100.0 + index)
        for index in range(300)
    ]
    monkeypatch.setattr(
        coinbase_history,
        "_request_candles",
        lambda *_args, **_kwargs: saturated,
    )

    result = fetch_coinbase_history(request, timeout=1.0)

    assert result.terminal is CoinbaseHistoryTerminal.MALFORMED
    assert result.authoritative_bars == ()
    assert result.error_code == "COINBASE_HISTORY_RESPONSE_CARDINALITY_INVALID"
    assert coinbase_history_request_max_span("1m") == timedelta(minutes=299)


def test_coinbase_chart_tail_types_only_the_active_bucket_as_provisional(monkeypatch) -> None:
    request = _request(ends_at=_START + timedelta(minutes=15))
    monkeypatch.setattr(
        coinbase_history,
        "_request_candles",
        lambda *_args, **_kwargs: [
            _row(_START, 100.0),
            _row(_START + timedelta(minutes=5), 102.0),
            _row(_START + timedelta(minutes=10), 104.0),
        ],
    )

    result = fetch_coinbase_chart_tail(
        request,
        timeout=1.0,
        confirmed_before=_START + timedelta(minutes=10),
    )

    assert result.terminal is CoinbaseHistoryTerminal.INCOMPLETE
    assert [bar.ts for bar in result.authoritative_bars] == [
        _START,
        _START + timedelta(minutes=5),
    ]
    assert [bar.ts for bar in result.provisional_bars] == [_START + timedelta(minutes=10)]
    assert result.provisional_bars[0].state is BarState.FORMING
    assert result.provisional_bars[0].closed is False


def test_coinbase_adapter_exposes_typed_complete_exact_range(monkeypatch) -> None:
    provider = CoinbaseDataProvider()
    instrument = coinbase_btc_payload()
    identity = provider.history_request_identity(instrument)
    intent = HistoryRepairIntent(
        provider="coinbase",
        instrument_id=instrument["instrument_id"],
        route_fingerprint=route_fingerprint(instrument),
        timeframe="5m",
        starts_at=_START,
        ends_at=_START + timedelta(minutes=10),
        request_identity=identity,
        canonical_generation=0,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=60.0,
    )

    def fetch(request, *, timeout):
        assert timeout == 3.0
        return CoinbaseHistoryResult(
            request=request,
            terminal=CoinbaseHistoryTerminal.COMPLETE,
            authoritative_bars=(),
            provisional_bars=(),
            response_count=0,
        )

    monkeypatch.setattr(coinbase_adapter, "fetch_coinbase_history", fetch)

    result = asyncio.run(provider.async_fetch_history(intent, 3.0, instrument=instrument))

    assert provider.capabilities.gap_repair is True
    assert provider.capabilities.exact_history_snapshot_authority is True
    assert result.terminal is ProviderHistoryTerminal.COMPLETE
    assert result.completions[0].response_count == 0
    assert result.completions[0].provider_limit == 300
    assert not hasattr(provider, "create_bar_feed")


def test_coinbase_history_settles_started_http_work_before_cancellation(monkeypatch) -> None:
    provider = CoinbaseDataProvider()
    instrument = coinbase_btc_payload()
    intent = HistoryRepairIntent(
        provider="coinbase",
        instrument_id=instrument["instrument_id"],
        route_fingerprint=route_fingerprint(instrument),
        timeframe="5m",
        starts_at=_START,
        ends_at=_START + timedelta(minutes=10),
        request_identity=provider.history_request_identity(instrument),
        canonical_generation=0,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=60.0,
    )
    started = threading.Event()
    release = threading.Event()

    def fetch(request, *, timeout):
        assert timeout == 3.0
        started.set()
        assert release.wait(timeout=2.0)
        return CoinbaseHistoryResult(
            request=request,
            terminal=CoinbaseHistoryTerminal.COMPLETE,
            authoritative_bars=(),
            provisional_bars=(),
            response_count=0,
        )

    monkeypatch.setattr(coinbase_adapter, "fetch_coinbase_history", fetch)

    async def scenario() -> None:
        task = asyncio.create_task(provider.async_fetch_history(intent, 3.0, instrument=instrument))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
