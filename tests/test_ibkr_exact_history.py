from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aef_terminal.data.providers import get_provider

import aef_terminal.data.adapters.ibkr as ibkr_adapter
import aef_terminal.data.ibkr.exact_history as exact_history
from aef_terminal.data.ibkr import bars as ibkr_bars_module
from aef_terminal.data.ibkr.bars import IbkrBarFeed
from aef_terminal.data.ibkr.exact_history import (
    IbkrHistoryRequest,
    IbkrHistoryResult,
    IbkrHistoryTerminal,
    _validate_response,
    fetch_ibkr_exact_history,
)
from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.data.provider_contract import (
    CanonicalHistoryStorageKind,
    HistoryContractResolution,
    HistoryRepairIntent,
    HistoryRepairPriority,
    HistoryRequestUnsupportedError,
    ProviderHistoryTerminal,
)
from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarProvenance,
    BarState,
)
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


_START = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _request() -> IbkrHistoryRequest:
    return IbkrHistoryRequest(
        provider_contract_id="756733",
        provider_contract_type="STK",
        instrument_id="ibkr|contract|756733",
        route_fingerprint="ibkr|contract|756733",
        provider_symbol="SPY",
        timeframe="5m",
        starts_at=_START,
        ends_at=_START + timedelta(minutes=10),
    )


def _bar(ts: datetime, *, state: BarState = BarState.CONFIRMED) -> Bar:
    return Bar(
        symbol="SPY",
        ts=ts,
        open=500.0,
        high=501.0,
        low=499.0,
        close=500.5,
        volume=1000.0,
        timeframe="5m",
        source="ibkr:historical:TRADES:contract=756733",
        closed=state is BarState.CONFIRMED,
        state=state,
        provenance=BarProvenance(
            provider="ibkr",
            instrument_id="ibkr|contract|756733",
            route_fingerprint="ibkr|contract|756733",
            request_type=BarProviderRequest.HISTORICAL,
            provider_contract_id="756733",
            provider_contract_type="STK",
            data_type="TRADES",
        ),
    )


def test_ibkr_terminal_response_accepts_empty_and_exact_confirmed_bars() -> None:
    request = _request()
    complete = _validate_response(
        request,
        [_bar(request.starts_at + timedelta(minutes=5)), _bar(request.starts_at)],
    )
    empty = _validate_response(request, [])

    assert complete.terminal is IbkrHistoryTerminal.COMPLETE
    assert complete.response_count == 2
    assert [bar.ts for bar in complete.authoritative_bars] == [
        request.starts_at,
        request.starts_at + timedelta(minutes=5),
    ]
    assert empty.terminal is IbkrHistoryTerminal.COMPLETE
    assert empty.response_count == 0


def test_ibkr_unknown_finality_is_incomplete_and_identity_drift_is_malformed() -> None:
    request = _request()
    incomplete = _validate_response(
        request,
        [
            _bar(request.starts_at),
            _bar(
                request.starts_at + timedelta(minutes=5),
                state=BarState.AWAITING_PROVIDER_CONFIRMATION,
            ),
        ],
    )
    malformed = _validate_response(
        request,
        [replace(_bar(request.starts_at), provenance=None)],
    )

    assert incomplete.terminal is IbkrHistoryTerminal.INCOMPLETE
    assert [bar.ts for bar in incomplete.authoritative_bars] == [request.starts_at]
    assert incomplete.error_code == "IBKR_HISTORY_FINALITY_UNKNOWN"
    assert malformed.terminal is IbkrHistoryTerminal.MALFORMED
    assert malformed.authoritative_bars == ()
    assert malformed.error_code == "IBKR_HISTORY_BAR_IDENTITY_INVALID"


def test_ibkr_exact_history_filters_valid_provider_range_overrun() -> None:
    request = _request()
    result = _validate_response(
        request,
        [
            replace(_bar(request.starts_at), ts=request.starts_at - timedelta(minutes=5)),
            _bar(request.starts_at),
            replace(_bar(request.starts_at), ts=request.ends_at),
        ],
    )

    assert result.terminal is IbkrHistoryTerminal.COMPLETE
    assert result.authoritative_bars == (_bar(request.starts_at),)
    assert result.response_count == 1


def test_ibkr_exact_history_types_timestamp_and_duplicate_failures() -> None:
    request = _request()
    unaligned = _validate_response(
        request,
        [replace(_bar(request.starts_at), ts=request.starts_at + timedelta(seconds=1))],
    )
    duplicate = _validate_response(
        request,
        [_bar(request.starts_at), _bar(request.starts_at)],
    )

    assert unaligned.error_code == "IBKR_HISTORY_BAR_TIMESTAMP_INVALID"
    assert duplicate.error_code == "IBKR_HISTORY_BAR_DUPLICATE"


def test_ibkr_exact_transport_uses_one_bounded_manager_request(monkeypatch) -> None:
    request = _request()
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    captured: dict[str, object] = {}

    class FakeFeed:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def _bars_async_owned(self):
            return []

    async def run_coroutine(lane, label, factory, *, timeout):
        captured.update(lane=lane, label=label, manager_timeout=timeout)
        return await factory()

    manager = type("Manager", (), {"run_coroutine": staticmethod(run_coroutine)})()
    monkeypatch.setattr(
        exact_history,
        "ibkr_history_runtime_contracts",
        lambda: (FakeFeed, manager),
    )

    result = asyncio.run(fetch_ibkr_exact_history(request, timeout=4.0, instrument=instrument))

    assert result.terminal is IbkrHistoryTerminal.COMPLETE
    assert captured["end_datetime"] == request.ends_at
    assert captured["duration"] == "600 S"
    assert captured["lane"] == "history"
    assert captured["manager_timeout"] == 4.0


def test_ibkr_provider_uses_rolling_root_identity_and_rejects_midpoint_routes() -> None:
    provider = get_provider("ibkr")
    future_root = ibkr_future_payload("ES")

    root_identity = provider.history_request_identity(future_root)

    assert root_identity.contract_resolution is HistoryContractResolution.PROVIDER_RESPONSE
    assert root_identity.provider_contract_id == ""
    assert root_identity.provider_contract_type == "CONTFUT"
    assert root_identity.provider_source == "IBKR_ROLLING_END_NOW"
    assert json.loads(root_identity.request_mode)["mode"] == "rolling_end_now"

    exact_identity = provider.history_request_identity(ibkr_stock_payload("SPY", con_id=756733))
    assert json.loads(exact_identity.request_mode)["range_normalization"] == (
        "filter_valid_provider_overrun_v1"
    )

    with pytest.raises(
        HistoryRequestUnsupportedError,
        match="IBKR_HISTORY_FUTURE_ROOT_EXACT_RANGE_UNSUPPORTED",
    ):
        provider._history_contract_identity(future_root)

    cash = ibkr_stock_payload(
        "EURUSD",
        con_id=12087792,
        asset_class="forex",
        sec_type="CASH",
    )
    with pytest.raises(
        HistoryRequestUnsupportedError,
        match="IBKR_HISTORY_TRADES_REQUIRED",
    ):
        provider.history_request_identity(cash)

    exact_future = ibkr_stock_payload(
        "ESU6",
        con_id=620730920,
        asset_class="future",
        sec_type="FUT",
    )
    exact_future["contract_identity"]["identity_scope"] = "contract"
    assert provider.history_request_identity(exact_future).provider_contract_type == "FUT"
    history_route = provider.canonical_history_route(exact_future)
    assert history_route is not None
    assert history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTRACT
    assert history_route.contract_key == "620730920"


def test_ibkr_exact_history_identity_separates_session_scope() -> None:
    provider = get_provider("ibkr")
    all_session = ibkr_stock_payload("SPY", con_id=756733)
    regular_session = ibkr_stock_payload(
        "SPX",
        con_id=416904,
        asset_class="index",
        sec_type="IND",
    )

    all_session_scope = json.loads(provider.history_request_identity(all_session).request_mode)
    regular_session_scope = json.loads(
        provider.history_request_identity(regular_session).request_mode
    )

    assert all_session_scope == {
        "currency": "USD",
        "exchange": "SMART",
        "mode": "req_historical_data",
        "primary_exchange": "",
        "range_normalization": "filter_valid_provider_overrun_v1",
        "use_rth": False,
    }
    assert regular_session_scope == {
        "currency": "USD",
        "exchange": "SMART",
        "mode": "req_historical_data",
        "primary_exchange": "",
        "range_normalization": "filter_valid_provider_overrun_v1",
        "use_rth": True,
    }


def test_ibkr_exact_future_uses_explicit_historical_end_anchor(
    monkeypatch,
) -> None:
    exact_future = ibkr_stock_payload(
        "ESU6",
        con_id=620730920,
        asset_class="future",
        sec_type="FUT",
    )
    exact_future["contract_identity"]["identity_scope"] = "contract"
    captured: dict[str, object] = {}

    class FakeIb:
        async def reqHistoricalDataAsync(self, _contract, **kwargs):
            captured.update(kwargs)
            return []

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

    async def connected(*_args):
        return FakeIb()

    async def qualified(*_args, **_kwargs):
        return SimpleNamespace(conId=620730920, secType="FUT", symbol="ES")

    monkeypatch.setattr(ibkr_bars_module, "_connected_ib_async", connected)
    monkeypatch.setattr(ibkr_bars_module, "_qualified_contract_async", qualified)
    monkeypatch.setattr(ibkr_bars_module, "_AsyncIbkrActivityWatchdog", FakeWatchdog)
    ends_at = _START + timedelta(minutes=10)

    feed = IbkrBarFeed(
        "5m",
        "1d",
        timeout=30.0,
        end_datetime=ends_at,
        duration="600 S",
        instrument=exact_future,
    )
    result = asyncio.run(feed._bars_async_owned())

    assert result == []
    assert captured["endDateTime"] == ends_at
    assert captured["durationStr"] == "600 S"
    assert captured["keepUpToDate"] is False
    assert feed.history_response_contract_identity == ("620730920", "FUT")


def test_ibkr_adapter_translates_terminal_end_proof_without_numeric_limit(
    monkeypatch,
) -> None:
    provider = get_provider("ibkr")
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    identity = provider.history_request_identity(instrument)
    intent = HistoryRepairIntent(
        provider="ibkr",
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

    async def fetch(request, *, timeout, instrument):
        assert timeout == 3.0
        return IbkrHistoryResult(
            request=request,
            terminal=IbkrHistoryTerminal.COMPLETE,
            authoritative_bars=(),
            response_count=0,
        )

    monkeypatch.setattr(ibkr_adapter, "fetch_ibkr_exact_history", fetch)

    result = asyncio.run(provider.async_fetch_history(intent, 3.0, instrument=instrument))

    assert provider.capabilities.gap_repair is True
    assert provider.capabilities.exact_history_snapshot_authority is True
    assert result.terminal is ProviderHistoryTerminal.COMPLETE
    assert result.completions[0].response_count == 0
    assert result.completions[0].request_limit is None
    assert result.completions[0].provider_limit is None
