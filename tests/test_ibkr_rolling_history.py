from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import aef_terminal.data.adapters.ibkr as ibkr_adapter
import aef_terminal.data.ibkr.bars as ibkr_bars
import aef_terminal.data.ibkr.rolling_history as rolling_history
from aef_terminal.data.ibkr.rolling_history import (
    IbkrRollingHistoryRequest,
    IbkrRollingHistoryResult,
    IbkrRollingHistoryTerminal,
    _validate_response,
    fetch_ibkr_rolling_history,
    ibkr_rolling_duration,
)
from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.data.provider_contract import (
    HistoryContractResolution,
    HistoryRepairIntent,
    HistoryRepairPriority,
    ProviderHistoryTerminal,
)
from aef_terminal.data.providers import get_provider, route_instrument
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance, BarState
from aef_terminal.ui.services import chart_history
from tests.provider_payloads import ibkr_future_payload


_START = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _instrument() -> dict:
    return ibkr_future_payload("ES")


def _request(*, requested_at: datetime | None = None) -> IbkrRollingHistoryRequest:
    instrument = _instrument()
    return IbkrRollingHistoryRequest(
        instrument_id=instrument["instrument_id"],
        route_fingerprint=route_fingerprint(instrument),
        provider_symbol="ES",
        timeframe="5m",
        starts_at=_START,
        ends_at=_START + timedelta(minutes=10),
        requested_at=requested_at or _START + timedelta(days=7),
    )


def _bar(
    ts: datetime,
    *,
    contract_id: str = "611744635",
    state: BarState = BarState.CONFIRMED,
) -> Bar:
    instrument = _instrument()
    return Bar(
        symbol="ES",
        ts=ts,
        open=5300.0,
        high=5301.0,
        low=5299.0,
        close=5300.5,
        volume=10.0,
        timeframe="5m",
        source=f"ibkr:historical:TRADES:contract={contract_id}",
        closed=state is BarState.CONFIRMED,
        state=state,
        provenance=BarProvenance(
            provider="ibkr",
            instrument_id=instrument["instrument_id"],
            route_fingerprint=route_fingerprint(instrument),
            request_type=BarProviderRequest.HISTORICAL,
            provider_contract_id=contract_id,
            provider_contract_type="CONTFUT",
            data_type="TRADES",
        ),
    )


def test_rolling_validator_admits_only_confirmed_target_bars() -> None:
    request = _request()
    result = _validate_response(
        request,
        [
            _bar(request.starts_at - timedelta(minutes=5)),
            _bar(request.starts_at),
            _bar(request.starts_at + timedelta(minutes=5)),
            _bar(request.ends_at),
        ],
        ("611744635", "CONTFUT"),
    )

    assert result.terminal is IbkrRollingHistoryTerminal.TARGET_CONTAINED
    assert [bar.ts for bar in result.authoritative_bars] == [
        request.starts_at,
        request.starts_at + timedelta(minutes=5),
    ]
    assert result.response_starts_at == request.starts_at - timedelta(minutes=5)
    assert result.response_ends_at == request.ends_at + timedelta(minutes=5)
    assert result.resolved_provider_contract_id == "611744635"


def test_rolling_validator_types_partial_empty_finality_and_identity_drift() -> None:
    request = _request()
    partial = _validate_response(
        request,
        [_bar(request.starts_at + timedelta(minutes=5)), _bar(request.ends_at)],
        ("611744635", "CONTFUT"),
    )
    empty = _validate_response(request, [], ("611744635", "CONTFUT"))
    finality = _validate_response(
        request,
        [
            _bar(request.starts_at - timedelta(minutes=5)),
            _bar(request.starts_at, state=BarState.AWAITING_PROVIDER_CONFIRMATION),
            _bar(request.ends_at),
        ],
        ("611744635", "CONTFUT"),
    )
    malformed = _validate_response(
        request,
        [_bar(request.starts_at, contract_id="999")],
        ("611744635", "CONTFUT"),
    )

    assert partial.terminal is IbkrRollingHistoryTerminal.TARGET_NOT_CONTAINED
    assert partial.error_code == "IBKR_ROLLING_HISTORY_TARGET_NOT_CONTAINED"
    assert empty.terminal is IbkrRollingHistoryTerminal.EMPTY
    assert empty.authoritative_bars == ()
    assert finality.terminal is IbkrRollingHistoryTerminal.FINALITY_UNKNOWN
    assert finality.authoritative_bars == ()
    assert malformed.terminal is IbkrRollingHistoryTerminal.MALFORMED
    assert malformed.authoritative_bars == ()


def test_rolling_transport_uses_empty_end_anchor_and_week_duration(monkeypatch) -> None:
    request = _request()
    instrument = _instrument()
    captured: dict[str, object] = {}

    class FakeFeed:
        history_response_contract_identity = ("611744635", "CONTFUT")

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def _bars_async_owned(self):
            return []

    async def run_coroutine(lane, label, factory, *, timeout):
        captured.update(lane=lane, label=label, manager_timeout=timeout)
        return await factory()

    manager = type("Manager", (), {"run_coroutine": staticmethod(run_coroutine)})()
    monkeypatch.setattr(
        rolling_history,
        "ibkr_history_runtime_contracts",
        lambda: (FakeFeed, manager),
    )

    result = asyncio.run(fetch_ibkr_rolling_history(request, timeout=4.0, instrument=instrument))

    assert ibkr_rolling_duration(request) == "9 D"
    assert result.terminal is IbkrRollingHistoryTerminal.EMPTY
    assert captured["end_datetime"] == ""
    assert captured["duration"] == "9 D"
    assert captured["lane"] == "history"
    assert captured["manager_timeout"] == 4.0


def test_ibkr_adapter_maps_root_response_without_exact_coverage(monkeypatch) -> None:
    provider = get_provider("ibkr")
    instrument = _instrument()
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
        cooldown_seconds=0.0,
    )

    async def fetch(request, *, timeout, instrument):
        assert timeout == 3.0
        assert request.starts_at == intent.starts_at
        return IbkrRollingHistoryResult(
            request=request,
            terminal=IbkrRollingHistoryTerminal.TARGET_CONTAINED,
            authoritative_bars=(
                _bar(request.starts_at),
                _bar(request.starts_at + timedelta(minutes=5)),
            ),
            resolved_provider_contract_id="611744635",
            response_starts_at=request.starts_at,
            response_ends_at=request.ends_at,
        )

    monkeypatch.setattr(ibkr_adapter, "fetch_ibkr_rolling_history", fetch)

    result = asyncio.run(provider.async_fetch_history(intent, 3.0, instrument=instrument))

    assert identity.contract_resolution is HistoryContractResolution.PROVIDER_RESPONSE
    assert result.terminal is ProviderHistoryTerminal.ROLLING_COMPLETE
    assert len(result.authoritative_bars) == 2
    assert result.completions == ()
    assert result.resolved_provider_contract_id == "611744635"


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "60m"])
@pytest.mark.parametrize("age", [timedelta(hours=25), timedelta(days=7), timedelta(days=365)])
def test_rolling_duration_reserves_the_partial_provider_day(timeframe, age) -> None:
    request = replace(
        _request(),
        timeframe=timeframe,
        ends_at=_START + timedelta(hours=1),
        requested_at=_START + age,
    )
    count, unit = ibkr_rolling_duration(request).split()
    days = int(count) * (365 if unit == "Y" else 1)
    assert unit in {"D", "Y"}
    assert days <= 365 if unit == "D" else int(count) <= 7
    # Reserve today's incomplete session in duration sizing. Returned provider
    # bounds remain the final authority, not this elapsed-time allowance.
    assert timedelta(days=days - 1) > age


def test_rolling_feed_covers_target_across_partial_current_session(monkeypatch) -> None:
    """Replay the observed IBKR duration boundary through the real feed/validator."""
    instrument = _instrument()
    request = replace(
        _request(),
        starts_at=datetime(2026, 8, 26, 9, 25, tzinfo=UTC),
        ends_at=datetime(2026, 8, 26, 21, tzinfo=UTC),
        requested_at=datetime(2026, 8, 28, 0, 19, tzinfo=UTC),
    )
    calls = []
    contract = SimpleNamespace(conId=611744635, secType="CONTFUT")

    class FakeIB:
        async def reqHistoricalDataAsync(self, selected, **kwargs):
            assert selected is contract
            calls.append(kwargs)
            # Read-only IBKR observations: 2 D starts at Aug 26 22:00, while
            # 3 D starts at Aug 25 22:00. No normalization or row cap caused it.
            count, unit = kwargs["durationStr"].split()
            assert unit == "D"
            starts_at = datetime(2026, 8, 27, 22, tzinfo=UTC) - timedelta(days=int(count) - 1)
            ends_at = datetime(2026, 8, 28, 0, 20, tzinfo=UTC)
            rows = []
            ts = starts_at
            while ts < ends_at:
                if ts.hour != 21:
                    rows.append(
                        SimpleNamespace(date=ts, open=100, high=101, low=99, close=100, volume=0)
                    )
                ts += timedelta(minutes=5)
            return rows

    ib = FakeIB()

    async def connected(*_args):
        return ib

    async def qualified(*_args, **_kwargs):
        return contract

    class Watchdog:
        def __init__(self, **_kwargs):
            pass

        def attach_ib_update_event(self, _ib):
            return lambda: None

        def start(self):
            pass

        def beat(self):
            pass

        async def cancel(self):
            pass

    async def run_coroutine(_lane, _label, factory, *, timeout):
        return await factory()

    monkeypatch.setattr(ibkr_bars, "_connected_ib_async", connected)
    monkeypatch.setattr(ibkr_bars, "_qualified_contract_async", qualified)
    monkeypatch.setattr(ibkr_bars, "_AsyncIbkrActivityWatchdog", Watchdog)
    monkeypatch.setattr(
        rolling_history,
        "ibkr_history_runtime_contracts",
        lambda: (ibkr_bars.IbkrBarFeed, SimpleNamespace(run_coroutine=run_coroutine)),
    )
    result = asyncio.run(fetch_ibkr_rolling_history(request, timeout=5, instrument=instrument))

    assert len(calls) == 1
    assert calls[0] == {
        "endDateTime": "",
        "durationStr": "3 D",
        "barSizeSetting": "5 mins",
        "whatToShow": "TRADES",
        "useRTH": False,
        "formatDate": 2,
        "keepUpToDate": False,
        "timeout": 0,
    }
    assert result.terminal is IbkrRollingHistoryTerminal.TARGET_CONTAINED
    assert result.response_starts_at == datetime(2026, 8, 25, 22, tzinfo=UTC)
    assert result.response_ends_at == datetime(2026, 8, 28, 0, 20, tzinfo=UTC)
    assert [bar.ts for bar in result.authoritative_bars] == [
        request.starts_at + timedelta(minutes=5 * index) for index in range(139)
    ]
    assert all(bar.closed and bar.volume == 0 for bar in result.authoritative_bars)


def test_rolling_request_rejects_more_than_supported_end_now_age() -> None:
    request = _request()

    with pytest.raises(ValueError, match="RANGE_INVALID"):
        replace(
            request,
            requested_at=request.starts_at
            + rolling_history.IBKR_ROLLING_HISTORY_MAX_AGE
            + timedelta(seconds=1),
        )


def test_week_internal_root_hole_selects_rolling_repair_without_exact_coverage() -> None:
    route = route_instrument(_instrument(), expected_source="ibkr")
    requested_start = datetime(2026, 8, 1, tzinfo=UTC)
    requested_end = datetime(2026, 8, 11, tzinfo=UTC)
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="14d",
        window=chart_history.HistoryRangeWindow(
            "14d",
            requested_start,
            requested_end,
        ),
    )

    class Store:
        def read_history_coverage(self, *_args, **_kwargs):
            pytest.fail("rolling repair must not read exact coverage")

    first_bar = requested_start
    next_bar = requested_start + timedelta(days=8)
    intent = chart_history._select_snapshot_repair_intent(
        route=route,
        interval="5m",
        bars=(
            _bar(first_bar),
            _bar(next_bar),
        ),
        plan=plan,
        store=Store(),
        canonical_generation=5,
        explicit_refresh=False,
    )

    assert plan.supported is True
    assert plan.contract is None
    assert intent is not None
    assert intent.request_identity.contract_resolution is (
        HistoryContractResolution.PROVIDER_RESPONSE
    )
    assert intent.target_starts_at == first_bar + timedelta(minutes=5)
    assert intent.target_ends_at == next_bar
