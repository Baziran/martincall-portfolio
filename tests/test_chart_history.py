from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data.provider_contract import (
    HistoryRepairAdmission,
    HistoryRepairAdmissionStatus,
    HistoryRepairIntent,
    HistoryRepairOutcome,
    HistoryRepairOutcomeStatus,
    HistoryRepairPriority,
    HistoryRequestAdmissionIdentity,
    HistoryRequestUnsupportedError,
)
from aef_terminal.domain import Bar, BarProviderRequest
from aef_terminal.runtime import chart_events
from aef_terminal.runtime.chart_events import ChartBarsGenerationChanged
from aef_terminal.data.history_coverage import (
    HistoryCoverageReceipt,
    current_history_coverage_contract,
)
from aef_terminal.ui.services import chart_history, chart_stream_gap_repair
from aef_terminal.ui.services.chart_history import (
    ConfirmedChartHistoryLoad,
    ConfirmedChartHistoryPage,
    coalesced_load_confirmed_chart_bars,
    load_confirmed_chart_history_page,
)


START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 2, tzinfo=UTC)
WINDOW = chart_history.HistoryRangeWindow("1d", START, END)


def _bar(
    timestamp: datetime,
    *,
    closed: bool = True,
    timeframe: str = "5m",
) -> Bar:
    return Bar(
        "TEST",
        timestamp,
        100.0,
        101.0,
        99.0,
        100.5,
        1.0,
        timeframe,
        "tinvest:test",
        closed=closed,
    )


def _identity() -> HistoryRequestAdmissionIdentity:
    return HistoryRequestAdmissionIdentity(
        request_contract_version=1,
        admission_contract_version=1,
        request_mode="get_candles_exchange",
        request_type=BarProviderRequest.HISTORICAL,
        provider_source="EXCHANGE",
        provider_contract_id="uid-1",
        provider_contract_type="INSTRUMENT_UID",
        data_type="TRADES",
    )


def _instrument() -> dict[str, str]:
    return {"provider": "tinvest", "instrument_id": "tinvest:uid-1"}


def _adapter() -> SimpleNamespace:
    return SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )


def _route(adapter: Any | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        fingerprint="route-1",
        instrument=_instrument(),
        adapter=adapter or _adapter(),
    )


def _contract():
    identity = _identity()
    return current_history_coverage_contract(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        route_fingerprint="route-1",
        timeframe="5m",
        request_mode=identity.request_mode,
        request_type=identity.request_type.value,
        provider_source=identity.provider_source,
        provider_contract_id=identity.provider_contract_id,
        provider_contract_type=identity.provider_contract_type,
        data_type=identity.data_type,
    )


def _receipt(starts_at: datetime, ends_at: datetime) -> HistoryCoverageReceipt:
    return HistoryCoverageReceipt(
        contract=_contract(),
        covered_from=starts_at,
        covered_to=ends_at,
        response_count=0,
        completed_at=END + timedelta(days=1),
    )


class _Store:
    def __init__(self, receipts: tuple[HistoryCoverageReceipt, ...] = ()) -> None:
        self.receipts = receipts
        self.reads: list[tuple[Any, datetime, datetime]] = []

    def read_history_coverage(
        self,
        contract: Any,
        *,
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[HistoryCoverageReceipt, ...]:
        self.reads.append((contract, starts_at, ends_at))
        return self.receipts


def _intent(*, generation: int = 0) -> HistoryRepairIntent:
    return HistoryRepairIntent(
        provider="tinvest",
        instrument_id="tinvest:uid-1",
        route_fingerprint="route-1",
        timeframe="5m",
        starts_at=START,
        ends_at=END,
        request_identity=_identity(),
        canonical_generation=generation,
        priority=HistoryRepairPriority.FOREGROUND,
        cooldown_seconds=0.0,
    )


def _history_result(
    bars: Any,
    warning: str = "",
    *,
    state: str = "unknown",
    canonical_generation: int = 0,
) -> ConfirmedChartHistoryLoad:
    return ConfirmedChartHistoryLoad(
        tuple(bars),
        warning,
        {
            "requested_range": "1d",
            "requested_start": START.isoformat(),
            "requested_end": END.isoformat(),
            "state": state,
            "repair": {"status": "not_requested"},
        },
        canonical_generation,
    )


def _patch_route(monkeypatch: pytest.MonkeyPatch, route: SimpleNamespace) -> None:
    monkeypatch.setattr(
        chart_history,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )


def _patch_chart_generation(
    monkeypatch: pytest.MonkeyPatch,
    getter: Any,
) -> None:
    monkeypatch.setattr(chart_history, "chart_bars_updated_generation", getter)

    def require(expected: int, *args: Any, **kwargs: Any) -> int:
        observed = getter(*args, **kwargs)
        if observed != expected:
            raise ChartBarsGenerationChanged(
                expected=expected,
                observed=observed,
            )
        return observed

    monkeypatch.setattr(chart_history, "require_chart_bars_generation", require)

    async def stable(*args: Any, **kwargs: Any) -> int:
        return getter(*args, **kwargs)

    monkeypatch.setattr(chart_history, "wait_for_chart_bars_stable", stable)


def test_confirmed_chart_history_contract_rejects_mutable_or_lookalike_bars() -> None:
    coverage: dict[str, Any] = {}
    bar = _bar(START)

    with pytest.raises(TypeError, match="Bar tuple"):
        ConfirmedChartHistoryLoad([bar], "", coverage, 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Bar tuple"):
        ConfirmedChartHistoryLoad(
            (SimpleNamespace(ts=START, closed=True),),  # type: ignore[arg-type]
            "",
            coverage,
            0,
        )
    with pytest.raises(ValueError, match="strictly timestamp-ordered"):
        ConfirmedChartHistoryPage(
            bars=(_bar(START + timedelta(minutes=5)), bar),
            next_before_ts=START,
            has_more=True,
            canonical_generation=0,
        )


def test_confirmed_chart_cache_key_uses_exact_route_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)

    assert chart_history.confirmed_chart_bars_cache_key(
        "tinvest",
        route.instrument,
        "5m",
        "1d",
        refresh_provider=True,
        window=WINDOW,
    ) == (
        "tinvest",
        "tinvest:uid-1",
        "route-1",
        "5m",
        "1d",
        True,
        START,
        END,
    )


def test_three_minute_chart_history_repairs_and_reads_one_minute_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=False,
            exact_history_snapshot_authority=False,
        )
    )
    route = _route(adapter)
    _patch_route(monkeypatch, route)
    loaded_intervals: list[str] = []
    source_bars = [
        Bar(
            "TEST",
            START + timedelta(minutes=minute),
            100 + minute,
            101 + minute,
            99 + minute,
            100.5 + minute,
            1,
            "1m",
            "tinvest",
        )
        for minute in (0, 2, 3, 4, 5)
    ]

    async def load(_route, interval, *_args, **_kwargs):
        loaded_intervals.append(interval)
        return source_bars, ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 0)

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            "tinvest",
            route.instrument,
            "3m",
            "1d",
            0.5,
            store=_Store(),
            refresh_provider=False,
            executor=None,
            window=WINDOW,
            load_generation=0,
        )
    )

    assert loaded_intervals == ["1m"]
    assert [bar.ts for bar in result.bars] == [START + timedelta(minutes=3)]
    assert all(bar.timeframe == "3m" and bar.closed for bar in result.bars)
    assert result.coverage["bar_repair"]["supported"] is False


def test_chart_history_loader_rejects_unsafe_range_before_store_factory() -> None:
    store_calls = 0

    def store_factory() -> object:
        nonlocal store_calls
        store_calls += 1
        return object()

    async def run() -> None:
        with pytest.raises(chart_history.ChartHistoryRangeError) as raised:
            await coalesced_load_confirmed_chart_bars(
                "tinvest",
                _instrument(),
                "1m",
                "2mo",
                0.5,
                store_factory=store_factory,
            )
        assert raised.value.code == "CHART_HISTORY_RANGE_TOO_LARGE"

    asyncio.run(run())
    assert store_calls == 0


def test_generic_load_does_not_read_receipts_and_explicit_refresh_is_concrete_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    store = _Store((_receipt(START, END),))
    _patch_route(monkeypatch, route)

    async def load(*_args, **_kwargs):
        return [], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 4)
    scheduled: list[HistoryRepairIntent] = []

    def schedule(intent: HistoryRepairIntent, **_kwargs: Any):
        scheduled.append(intent)
        return HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=intent,
            newly_admitted=True,
        )

    monkeypatch.setattr(chart_history, "schedule_chart_cached_gap_repair", schedule)

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            "tinvest",
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=store,
            refresh_provider=True,
            executor=None,
            window=WINDOW,
            load_generation=4,
        )
    )

    assert result.bars == ()
    assert result.coverage["state"] == "not_checked"
    assert result.coverage["provider_range_complete"] is False
    assert result.coverage["repair"]["status"] == "scheduled"
    assert result.coverage["bar_repair"]["status"] == "scheduled"
    assert result.coverage["absence_verification"]["supported"] is True
    assert result.canonical_generation == 4
    assert store.reads == []
    assert len(scheduled) == 1
    assert scheduled[0].target_starts_at == START
    assert scheduled[0].target_ends_at == END
    assert "publish_chart_bars_updated" not in inspect.getsource(chart_history)


def test_chart_history_generic_path_never_invokes_coverage_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    seen = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )

    class RacingStore(_Store):
        lease: Any | None = None

        def read_history_coverage(
            self,
            contract: Any,
            *,
            starts_at: datetime,
            ends_at: datetime,
        ) -> tuple[HistoryCoverageReceipt, ...]:
            receipts = super().read_history_coverage(
                contract,
                starts_at=starts_at,
                ends_at=ends_at,
            )
            self.lease = chart_events.begin_chart_bars_write(
                seen,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            return receipts

    store = RacingStore((_receipt(START, END),))
    _patch_route(monkeypatch, route)

    async def load(*_args: Any, **_kwargs: Any):
        return [_bar(START)], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            "tinvest",
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=store,
            refresh_provider=True,
            executor=None,
            window=WINDOW,
            load_generation=seen,
        )
    )
    assert result.canonical_generation == seen
    assert store.reads == []
    assert store.lease is None


def test_explicit_refresh_schedules_from_requested_range_without_receipt_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    covered_to = START + timedelta(hours=8)
    store = _Store((_receipt(START, covered_to),))
    _patch_route(monkeypatch, route)

    async def load(*_args, **_kwargs):
        return [_bar(covered_to - timedelta(minutes=5))], ""

    selected: dict[str, Any] = {}
    intent = replace(_intent(generation=6), starts_at=covered_to)

    def select(**kwargs):
        selected.update(kwargs)
        return intent

    def schedule(candidate, **_kwargs):
        return HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=candidate,
            newly_admitted=True,
        )

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 6)
    monkeypatch.setattr(chart_history, "select_chart_history_repair", select)
    monkeypatch.setattr(chart_history, "schedule_chart_cached_gap_repair", schedule)

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            "tinvest",
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=store,
            refresh_provider=True,
            executor=None,
            window=WINDOW,
            load_generation=6,
        )
    )

    assert result.coverage["state"] == "not_checked"
    assert result.coverage["pending_from"] is None
    assert result.coverage["pending_to"] is None
    assert result.coverage["repair"]["status"] == "scheduled"
    assert result.coverage["bar_repair"]["status"] == "scheduled"
    assert selected["requested_from"] == START
    assert selected["requested_to"] == END
    assert selected["canonical_generation"] == 6
    assert store.reads == []


def test_explicit_stale_tail_demand_selects_only_its_exact_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="1d",
        window=WINDOW,
    )
    demand_start = START + timedelta(hours=8)
    demand_end = END - timedelta(minutes=5)
    observed: dict[str, Any] = {}

    def select(**kwargs):
        observed.update(kwargs)
        return _intent(generation=9)

    monkeypatch.setattr(chart_history, "select_chart_history_repair", select)

    selected = chart_history._select_snapshot_repair_intent(
        route=route,
        interval="5m",
        bars=[_bar(START)],
        plan=plan,
        store=_Store(),
        canonical_generation=9,
        explicit_refresh=False,
        explicit_demand=chart_history.HistoryRangeWindow(
            "1d",
            demand_start,
            demand_end,
        ),
    )

    assert selected is not None
    assert observed["requested_from"] == demand_start
    assert observed["requested_to"] == demand_end
    assert observed["dedupe_receipts"] == ()


def test_sparse_two_month_session_history_ignores_nights_and_advances_to_open_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_start = datetime(2026, 6, 1, tzinfo=UTC)
    requested_end = datetime(2026, 8, 1, tzinfo=UTC)
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="3mo",
        window=chart_history.HistoryRangeWindow(
            "3mo",
            requested_start,
            requested_end,
        ),
    )
    night_only_bars = [_bar(requested_start + timedelta(days=offset)) for offset in range(0, 61, 3)]
    schedule_calls: list[tuple[datetime, datetime]] = []

    def schedule(starts_at, ends_at, *_args, **_kwargs):
        schedule_calls.append((starts_at, ends_at))
        return SimpleNamespace(
            schedule_state="unknown",
            expected_slots=(),
            truncated=False,
        )

    monkeypatch.setattr(
        chart_history,
        "provider_session_bar_slot_coverage_between",
        schedule,
    )
    quiet_store = _Store()

    assert (
        chart_history._select_snapshot_repair_intent(
            route=route,
            interval="5m",
            bars=night_only_bars,
            plan=plan,
            store=quiet_store,
            canonical_generation=3,
            explicit_refresh=False,
        )
        is None
    )
    assert schedule_calls == [
        (
            night_only_bars[-1].ts - timedelta(days=8),
            night_only_bars[-1].ts,
        )
    ]
    assert quiet_store.reads == []

    older_gap = datetime(2026, 7, 30, 14, 5, tzinfo=UTC)
    newer_gap = datetime(2026, 7, 31, 14, 5, tzinfo=UTC)
    bars_with_open_gaps = [
        *night_only_bars,
        _bar(older_gap - timedelta(minutes=5)),
        _bar(older_gap + timedelta(minutes=5)),
        _bar(newer_gap - timedelta(minutes=5)),
        _bar(newer_gap + timedelta(minutes=5)),
    ]
    schedule_calls.clear()

    def verified_open_schedule(starts_at, ends_at, *_args, **_kwargs):
        schedule_calls.append((starts_at, ends_at))
        assert starts_at == newer_gap + timedelta(minutes=5) - timedelta(days=8)
        assert ends_at == newer_gap + timedelta(minutes=5)
        return SimpleNamespace(
            schedule_state="verified",
            expected_slots=(older_gap, newer_gap),
            truncated=False,
        )

    monkeypatch.setattr(
        chart_history,
        "provider_session_bar_slot_coverage_between",
        verified_open_schedule,
    )
    store = _Store((_receipt(newer_gap, newer_gap + timedelta(minutes=5)),))

    intent = chart_history._select_snapshot_repair_intent(
        route=route,
        interval="5m",
        bars=bars_with_open_gaps,
        plan=plan,
        store=store,
        canonical_generation=3,
        explicit_refresh=False,
    )

    assert intent is not None
    assert intent.target_starts_at == older_gap
    assert intent.target_ends_at == older_gap + timedelta(minutes=5)
    assert schedule_calls == [
        (
            newer_gap + timedelta(minutes=5) - timedelta(days=8),
            newer_gap + timedelta(minutes=5),
        )
    ]
    assert len(store.reads) == 1
    assert store.reads[0][1:] == (
        older_gap,
        newer_gap + timedelta(minutes=5),
    )


def test_verified_schedule_repairs_medium_gap_across_closed_overnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    last_before_gap = datetime(2026, 8, 10, 16, 5, tzinfo=UTC)
    first_after_gap = datetime(2026, 8, 11, 8, tzinfo=UTC)
    requested_start = last_before_gap
    requested_end = first_after_gap + timedelta(minutes=5)
    first_missing = last_before_gap + timedelta(minutes=5)
    session_close = datetime(2026, 8, 11, tzinfo=UTC)
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="5d",
        window=chart_history.HistoryRangeWindow(
            "5d",
            requested_start,
            requested_end,
        ),
    )
    schedule_state = "verified"
    schedule_calls: list[tuple[datetime, datetime, int]] = []

    def schedule(
        starts_at: datetime,
        ends_at: datetime,
        *_args: Any,
        limit: int,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        schedule_calls.append((starts_at, ends_at, limit))
        return SimpleNamespace(
            schedule_state=schedule_state,
            expected_slots=tuple(
                first_missing + index * timedelta(minutes=5)
                for index in range(int((session_close - first_missing).total_seconds() // 300))
            ),
            truncated=False,
        )

    monkeypatch.setattr(
        chart_history,
        "provider_session_bar_slot_coverage_between",
        schedule,
    )
    bars = [
        _bar(last_before_gap),
        _bar(first_after_gap),
    ]

    assert chart_history._automatic_snapshot_repair_candidates(
        route=route,
        interval="5m",
        bars=bars,
        plan=plan,
        store=_Store(),
    ) == ((first_missing, session_close),)
    assert schedule_calls == [(first_missing, first_after_gap, 191)]

    schedule_state = "unknown"
    schedule_calls.clear()
    assert (
        chart_history._automatic_snapshot_repair_candidates(
            route=route,
            interval="5m",
            bars=bars,
            plan=plan,
            store=_Store(),
        )
        == ()
    )
    assert schedule_calls == [(first_missing, first_after_gap, 191)]


def test_thousand_small_gaps_use_one_schedule_read_and_choose_newest_open_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_start = datetime(2026, 7, 20, tzinfo=UTC)
    bars = [_bar(requested_start + index * timedelta(minutes=10)) for index in range(1_001)]
    requested_end = bars[-1].ts + timedelta(minutes=5)
    newest_gap = bars[-2].ts + timedelta(minutes=5)
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="31d",
        window=chart_history.HistoryRangeWindow(
            "31d",
            requested_start,
            requested_end,
        ),
    )
    schedule_calls: list[tuple[datetime, datetime, int]] = []

    def schedule(
        starts_at: datetime,
        ends_at: datetime,
        *_args: Any,
        limit: int,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        schedule_calls.append((starts_at, ends_at, limit))
        return SimpleNamespace(
            schedule_state="verified",
            expected_slots=(newest_gap,),
            truncated=False,
        )

    monkeypatch.setattr(
        chart_history,
        "provider_session_bar_slot_coverage_between",
        schedule,
    )
    store = _Store()

    intent = chart_history._select_snapshot_repair_intent(
        route=route,
        interval="5m",
        bars=bars,
        plan=plan,
        store=store,
        canonical_generation=8,
        explicit_refresh=False,
    )

    assert intent is not None
    assert intent.target_starts_at == newest_gap
    assert intent.target_ends_at == newest_gap + timedelta(minutes=5)
    assert schedule_calls == [
        (
            bars[0].ts + timedelta(minutes=5),
            newest_gap + timedelta(minutes=5),
            2_000,
        )
    ]
    assert len(store.reads) == 1


def test_two_month_daily_maintenance_gaps_only_use_verified_recent_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_start = datetime(2026, 6, 1, tzinfo=UTC)
    bars = [
        _bar(timestamp)
        for day in range(61)
        for timestamp in (
            requested_start + timedelta(days=day),
            requested_start + timedelta(days=day, hours=1, minutes=5),
        )
    ]
    requested_end = bars[-1].ts + timedelta(minutes=5)
    newest_gap = requested_start + timedelta(days=60, minutes=5)
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="3mo",
        window=chart_history.HistoryRangeWindow(
            "3mo",
            requested_start,
            requested_end,
        ),
    )
    schedule_state = "verified"
    schedule_calls: list[tuple[datetime, datetime, int]] = []

    def schedule(
        starts_at: datetime,
        ends_at: datetime,
        *_args: Any,
        limit: int,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        schedule_calls.append((starts_at, ends_at, limit))
        return SimpleNamespace(
            schedule_state=schedule_state,
            expected_slots=(
                tuple(newest_gap + index * timedelta(minutes=5) for index in range(12))
                if schedule_state == "verified"
                else ()
            ),
            truncated=False,
        )

    monkeypatch.setattr(
        chart_history,
        "provider_session_bar_slot_coverage_between",
        schedule,
    )

    assert chart_history._automatic_snapshot_repair_candidates(
        route=route,
        interval="5m",
        bars=bars,
        plan=plan,
        store=_Store(),
    ) == ((newest_gap, newest_gap + timedelta(hours=1)),)
    assert schedule_calls == [
        (
            newest_gap + timedelta(hours=1) - timedelta(days=8),
            newest_gap + timedelta(hours=1),
            2_305,
        )
    ]

    schedule_state = "unknown"
    schedule_calls.clear()
    assert (
        chart_history._automatic_snapshot_repair_candidates(
            route=route,
            interval="5m",
            bars=bars,
            plan=plan,
            store=_Store(),
        )
        == ()
    )
    assert len(schedule_calls) == 1


def test_twenty_day_internal_session_gap_is_direct_provider_mirror_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_start = datetime(2026, 7, 1, tzinfo=UTC)
    requested_end = datetime(2026, 8, 1, tzinfo=UTC)
    gap_start = requested_start + timedelta(minutes=10)
    gap_end = gap_start + timedelta(days=20)
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    plan = chart_history._repair_capability_plan(
        route=route,
        interval="5m",
        range_="31d",
        window=chart_history.HistoryRangeWindow(
            "31d",
            requested_start,
            requested_end,
        ),
    )
    monkeypatch.setattr(
        chart_history,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: pytest.fail(
            "large internal gaps must go directly to bounded provider history"
        ),
    )
    store = _Store()

    intent = chart_history._select_snapshot_repair_intent(
        route=route,
        interval="5m",
        bars=[
            _bar(requested_start),
            _bar(requested_start + timedelta(minutes=5)),
            _bar(gap_end),
        ],
        plan=plan,
        store=store,
        canonical_generation=4,
        explicit_refresh=False,
    )

    assert intent is not None
    assert intent.target_starts_at == gap_start
    assert intent.target_ends_at == gap_end
    assert intent.starts_at == gap_end - timedelta(days=1)
    assert intent.ends_at == gap_end
    assert len(store.reads) == 1
    assert store.reads[0][1:] == (gap_start, gap_end)


@pytest.mark.parametrize(
    ("interval", "range_key"),
    (("5m", "5d"), ("1m", "1d")),
)
def test_normal_checkpoint_does_not_turn_forming_tail_boundary_into_repair(
    monkeypatch: pytest.MonkeyPatch,
    interval: str,
    range_key: str,
) -> None:
    step = timedelta(minutes=int(interval[:-1]))
    current_bucket = chart_history.interval_bucket(datetime.now(tz=UTC), interval)
    last_closed = current_bucket - step
    recovery_end = current_bucket + step
    requested_window = chart_history.HistoryRangeWindow(
        range_key,
        current_bucket - timedelta(days=6 if range_key == "5d" else 2),
        recovery_end,
    )
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=False,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = SimpleNamespace(
        provider="coinbase",
        instrument_id="coinbase|contract|BTC-USD",
        fingerprint="coinbase|contract|BTC-USD",
        instrument={
            "provider": "coinbase",
            "instrument_id": "coinbase|contract|BTC-USD",
        },
        adapter=adapter,
    )
    _patch_route(monkeypatch, route)
    monkeypatch.setattr(
        chart_stream_gap_repair,
        "route_instrument",
        lambda *_args, **_kwargs: route,
    )
    _patch_chart_generation(monkeypatch, lambda *_args, **_kwargs: 12)
    loaded_windows: list[chart_history.HistoryRangeWindow] = []

    async def load(*_args: Any, **kwargs: Any):
        loaded_windows.append(kwargs["window"])
        return [_bar(last_closed, timeframe=interval)], ""

    selected: list[HistoryRepairIntent] = []
    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    monkeypatch.setattr(
        chart_history,
        "latest_chart_history_repair_outcome",
        lambda intent: selected.append(intent) or None,
    )
    monkeypatch.setattr(
        chart_history,
        "schedule_chart_cached_gap_repair",
        lambda intent, **_kwargs: HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.SCHEDULED,
            intent=intent,
            newly_admitted=True,
        ),
    )
    store = _Store()

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            route.provider,
            route.instrument,
            interval,
            range_key,
            0.5,
            store=store,
            refresh_provider=False,
            executor=None,
            window=requested_window,
            load_generation=12,
        )
    )

    assert selected == []
    assert loaded_windows == [requested_window]
    assert store.reads == []
    assert result.coverage["requested_end"] == current_bucket.isoformat()
    assert result.coverage["bar_repair"]["requested_to"] is None
    assert result.coverage["bar_repair"]["target_to"] is None
    assert result.coverage["bar_repair"]["status"] == "not_needed"


def test_returned_bar_repair_capability_is_separate_from_absence_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=False,
        ),
        continuous_session=lambda _instrument: False,
        history_request_identity=lambda _instrument: _identity(),
        history_request_max_span=lambda _instrument, _interval: timedelta(days=1),
    )
    route = _route(adapter)
    _patch_route(monkeypatch, route)
    monkeypatch.setattr(
        chart_history,
        "async_load_provider_bars",
        lambda *_args, **_kwargs: pytest.fail("async loader must be awaited"),
    )

    async def load(*_args: Any, **_kwargs: Any):
        return [_bar(START)], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 3)
    monkeypatch.setattr(
        chart_history,
        "select_chart_history_repair",
        lambda **_kwargs: None,
    )

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            route.provider,
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=_Store(),
            refresh_provider=False,
            executor=None,
            window=WINDOW,
            load_generation=3,
        )
    )

    assert result.coverage["bar_repair"]["supported"] is True
    assert result.coverage["bar_repair"]["status"] == "not_needed"
    assert result.coverage["absence_verification"] == {
        "supported": False,
        "state": "unsupported",
        "error_code": "PROVIDER_ABSENCE_VERIFICATION_UNAVAILABLE",
        "requested_from": START.isoformat(),
        "requested_to": END.isoformat(),
    }


def test_exact_range_unsupported_reports_provider_code_without_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = "IBKR_HISTORY_FUTURE_ROOT_EXACT_RANGE_UNSUPPORTED"

    def unsupported(_instrument: Any) -> Any:
        raise HistoryRequestUnsupportedError(provider="ibkr", code=code)

    adapter = SimpleNamespace(
        capabilities=SimpleNamespace(
            gap_repair=True,
            exact_history_snapshot_authority=True,
        ),
        history_request_identity=unsupported,
    )
    route = SimpleNamespace(
        provider="ibkr",
        instrument_id="ibkr:future-root",
        fingerprint="ibkr-route",
        instrument={"provider": "ibkr", "instrument_id": "ibkr:future-root"},
        adapter=adapter,
    )
    _patch_route(monkeypatch, route)

    async def load(*_args: Any, **_kwargs: Any):
        return [_bar(START)], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 4)
    monkeypatch.setattr(
        chart_history,
        "schedule_chart_cached_gap_repair",
        lambda *_args, **_kwargs: pytest.fail("unsupported exact range must not schedule"),
    )

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            route.provider,
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=_Store(),
            refresh_provider=True,
            executor=None,
            window=WINDOW,
            load_generation=4,
        )
    )

    assert result.coverage["bar_repair"]["supported"] is False
    assert result.coverage["bar_repair"]["phase"] == "idle"
    assert result.coverage["bar_repair"]["status"] == "not_supported"
    assert result.coverage["bar_repair"]["error_code"] == code
    assert result.coverage["bar_repair"]["requested_from"] is None
    assert result.coverage["bar_repair"]["requested_to"] is None
    assert result.coverage["absence_verification"]["error_code"] == code


@pytest.mark.parametrize(
    ("outcome_status", "error_code"),
    (
        (HistoryRepairOutcomeStatus.NO_CHANGE, "PROVIDER_RETURNED_NO_NEW_BARS"),
        (HistoryRepairOutcomeStatus.INCOMPLETE, "PROVIDER_RANGE_INCOMPLETE"),
        (HistoryRepairOutcomeStatus.MALFORMED, "PROVIDER_RESPONSE_MALFORMED"),
        (HistoryRepairOutcomeStatus.FETCH_FAILED, "PROVIDER_TIMEOUT"),
    ),
)
def test_next_chart_payload_exposes_terminal_repair_outcome(
    monkeypatch: pytest.MonkeyPatch,
    outcome_status: HistoryRepairOutcomeStatus,
    error_code: str,
) -> None:
    route = _route()
    store = _Store()
    intent = _intent(generation=5)
    _patch_route(monkeypatch, route)

    async def load(*_args: Any, **_kwargs: Any):
        return [_bar(START)], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 5)
    monkeypatch.setattr(
        chart_history,
        "select_chart_history_repair",
        lambda **_kwargs: intent,
    )
    scheduled: list[HistoryRepairIntent] = []

    def schedule(candidate: HistoryRepairIntent, **_kwargs: Any):
        scheduled.append(candidate)
        return HistoryRepairAdmission(
            status=HistoryRepairAdmissionStatus.AWAITING_GENERATION_ADVANCE,
            intent=candidate,
        )

    monkeypatch.setattr(
        chart_history,
        "schedule_chart_cached_gap_repair",
        schedule,
    )
    monkeypatch.setattr(
        chart_history,
        "latest_chart_history_repair_outcome",
        lambda candidate: HistoryRepairOutcome(
            status=outcome_status,
            intent=candidate,
            error_code=error_code,
        ),
    )

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            route.provider,
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=store,
            refresh_provider=True,
            executor=None,
            window=WINDOW,
            load_generation=5,
        )
    )

    assert result.coverage["bar_repair"] == {
        "supported": True,
        "phase": "terminal",
        "status": outcome_status.value,
        "error_code": error_code,
        "requested_from": START.isoformat(),
        "requested_to": END.isoformat(),
        "target_from": START.isoformat(),
        "target_to": END.isoformat(),
        "remaining_from": START.isoformat(),
        "remaining_to": END.isoformat(),
    }
    assert result.coverage["repair"]["terminal_outcome"]["canonical_generation"] == 5
    assert scheduled == [intent]


def test_stale_generation_fails_closed_without_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    store = _Store()
    _patch_route(monkeypatch, route)

    async def load(*_args, **_kwargs):
        return [_bar(START)], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 9)
    monkeypatch.setattr(
        chart_history,
        "select_chart_history_repair",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale snapshot must not plan repair")
        ),
    )

    with pytest.raises(
        ChartBarsGenerationChanged,
        match=r"expected=8 observed=9",
    ):
        asyncio.run(
            chart_history._load_confirmed_chart_bars(
                "tinvest",
                route.instrument,
                "5m",
                "1d",
                0.5,
                store=store,
                refresh_provider=True,
                executor=None,
                window=WINDOW,
                load_generation=8,
            )
        )


def test_generic_chart_load_keeps_db_bars_visible_without_coverage_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)

    async def load(*_args, **_kwargs):
        return [_bar(START)], "db warning"

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 0)

    result = asyncio.run(
        chart_history._load_confirmed_chart_bars(
            "tinvest",
            route.instrument,
            "5m",
            "1d",
            0.5,
            store=object(),
            refresh_provider=True,
            executor=None,
            window=WINDOW,
            load_generation=0,
        )
    )

    assert len(result.bars) == 1
    assert result.warning == "db warning"
    assert result.coverage["state"] == "not_checked"
    assert result.coverage["error_code"] is None


def test_coalesced_loader_retries_when_generation_changes_during_db_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    store = _Store((_receipt(START, END),))
    _patch_route(monkeypatch, route)
    chart_history._result_cache.clear()
    chart_history._inflight_tasks.clear()
    chart_history._cache_generation.clear()
    generation = 7
    calls = 0
    fresh_bar = _bar(END - timedelta(minutes=5))

    async def load(*_args, **_kwargs):
        nonlocal calls, generation
        calls += 1
        if calls == 1:
            generation = 8
            return [_bar(START)], "stale read"
        return [fresh_bar], ""

    monkeypatch.setattr(chart_history, "async_load_provider_bars", load)
    _patch_chart_generation(
        monkeypatch,
        lambda *_args, **_kwargs: generation,
    )

    async def run() -> ConfirmedChartHistoryLoad:
        return await coalesced_load_confirmed_chart_bars(
            "tinvest",
            route.instrument,
            "5m",
            "1d",
            0.5,
            store_factory=lambda: store,
            refresh_provider=False,
            window=WINDOW,
        )

    try:
        result = asyncio.run(run())
        assert calls == 2
        assert result.bars == (fresh_bar,)
        assert result.warning == ""
        assert result.canonical_generation == 8
    finally:
        chart_history._result_cache.clear()
        chart_history._inflight_tasks.clear()
        chart_history._cache_generation.clear()


def test_coalesced_loader_rejects_cached_result_during_active_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)
    monkeypatch.setattr(chart_history, "_LOAD_DEADLINE_GRACE_SECONDS", 0.0)
    chart_history._result_cache.clear()
    chart_history._inflight_tasks.clear()
    chart_history._cache_generation.clear()
    seen = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    cached_result = _history_result(
        [_bar(START)],
        canonical_generation=seen,
    )
    key = chart_history.confirmed_chart_bars_cache_key(
        route.provider,
        route.instrument,
        "5m",
        "1d",
        refresh_provider=False,
        window=WINDOW,
    )
    store_calls = 0

    def store_factory() -> _Store:
        nonlocal store_calls
        store_calls += 1
        return _Store()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        chart_history._result_cache[key] = chart_history._result_cache_entry(
            loop.time(),
            seen,
            cached_result,
        )
        lease = chart_events.begin_chart_bars_write(
            seen,
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        try:
            with pytest.raises(ChartBarsGenerationChanged) as raised:
                await coalesced_load_confirmed_chart_bars(
                    "tinvest",
                    route.instrument,
                    "5m",
                    "1d",
                    0.1,
                    store_factory=store_factory,
                    refresh_provider=False,
                    window=WINDOW,
                )
            assert raised.value.write_in_progress is True
        finally:
            chart_events.end_chart_bars_write(lease)

    try:
        asyncio.run(run())
        assert store_calls == 0
    finally:
        chart_history._result_cache.clear()
        chart_history._inflight_tasks.clear()
        chart_history._cache_generation.clear()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_coalesced_loader_waits_for_physical_write_settlement(
    monkeypatch: pytest.MonkeyPatch, cached: bool, changed: bool
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)
    chart_history._result_cache.clear()
    chart_history._inflight_tasks.clear()
    calls = []

    async def load(*_args, **kwargs):
        calls.append(kwargs["load_generation"])
        return _history_result([], canonical_generation=kwargs["load_generation"])

    monkeypatch.setattr(chart_history, "_load_confirmed_chart_bars", load)

    async def run() -> None:
        seen = chart_events.chart_bars_updated_generation(
            "5m", route.fingerprint, instrument_id=route.instrument_id
        )
        key = chart_history.confirmed_chart_bars_cache_key(
            route.provider, route.instrument, "5m", "1d", refresh_provider=False, window=WINDOW
        )
        if cached:
            chart_history._result_cache[key] = chart_history._result_cache_entry(
                asyncio.get_running_loop().time(),
                seen,
                _history_result([], canonical_generation=seen),
            )
        guard = chart_events.begin_chart_bars_write(
            seen, "5m", route.fingerprint, instrument_id=route.instrument_id
        )
        task = asyncio.create_task(
            coalesced_load_confirmed_chart_bars(
                route.provider,
                route.instrument,
                "5m",
                "1d",
                0.5,
                store_factory=lambda: object(),
                window=WINDOW,
            )
        )
        await asyncio.sleep(0)
        assert not task.done()
        assert calls == []
        if changed:
            expected = chart_events.invalidate_chart_bars_write(
                guard, route.provider, reason="test"
            )
        else:
            chart_events.end_chart_bars_write(guard)
            expected = seen
        result = await asyncio.wait_for(task, 1.0)
        assert result.canonical_generation == expected
        assert calls == ([] if cached and not changed else [expected])
        assert not chart_events._stable_waiters

    try:
        asyncio.run(run())
    finally:
        chart_history._result_cache.clear()
        chart_history._inflight_tasks.clear()
        chart_history._cache_generation.clear()


def test_generation_retry_waits_and_retains_one_absolute_deadline(monkeypatch) -> None:
    route = _route()
    _patch_route(monkeypatch, route)
    chart_history._result_cache.clear()
    calls = []
    guard = None
    first_read = asyncio.Event()

    async def load(*_args, **kwargs):
        nonlocal guard
        calls.append(kwargs)
        if len(calls) == 1:
            guard = chart_events.begin_chart_bars_write(
                kwargs["load_generation"],
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            first_read.set()
            raise ChartBarsGenerationChanged(
                expected=kwargs["load_generation"],
                observed=kwargs["load_generation"],
                write_in_progress=True,
            )
        return _history_result([], canonical_generation=kwargs["load_generation"])

    monkeypatch.setattr(chart_history, "_load_confirmed_chart_bars", load)

    async def run() -> None:
        task = asyncio.create_task(
            coalesced_load_confirmed_chart_bars(
                route.provider,
                route.instrument,
                "5m",
                "1d",
                0.5,
                store_factory=object,
                window=WINDOW,
            )
        )
        await first_read.wait()
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert not task.done()
        generation = chart_events.invalidate_chart_bars_write(guard, route.provider, reason="test")
        result = await asyncio.wait_for(task, 1.0)
        assert result.canonical_generation == generation
        assert len(calls) == 2
        assert calls[0]["deadline"] == calls[1]["deadline"]

    try:
        asyncio.run(run())
    finally:
        chart_history._result_cache.clear()
        chart_history._inflight_tasks.clear()
        chart_history._cache_generation.clear()


@pytest.mark.parametrize("shutdown", [False, True])
def test_chart_history_stable_wait_cannot_admit_work_after_cancel_or_shutdown(
    monkeypatch, shutdown
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)

    def no_storage():
        raise AssertionError("a stopped waiter must not initialize storage")

    async def run() -> None:
        generation = chart_events.chart_bars_updated_generation(
            "5m", route.fingerprint, instrument_id=route.instrument_id
        )
        guard = chart_events.begin_chart_bars_write(
            generation, "5m", route.fingerprint, instrument_id=route.instrument_id
        )
        task = asyncio.create_task(
            coalesced_load_confirmed_chart_bars(
                route.provider,
                route.instrument,
                "5m",
                "1d",
                0.5,
                store_factory=no_storage,
                window=WINDOW,
            )
        )
        await asyncio.sleep(0)
        assert not task.done()
        try:
            if shutdown:
                await chart_history.shutdown_chart_history_runtime()
            else:
                task.cancel()
        finally:
            chart_events.end_chart_bars_write(guard)
        try:
            with pytest.raises(RuntimeError if shutdown else asyncio.CancelledError):
                await task
            assert not chart_history._inflight_tasks
            assert not chart_events._stable_waiters
        finally:
            chart_history.start_chart_history_runtime()

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", [OSError("storage failed"), ChartBarsGenerationChanged(expected=0, observed=1)]
)
def test_generation_retry_is_finite_and_never_hides_storage_failure(monkeypatch, failure) -> None:
    route = _route()
    _patch_route(monkeypatch, route)
    calls = 0

    async def load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(chart_history, "_load_confirmed_chart_bars", load)

    async def run() -> None:
        with pytest.raises(type(failure)) as raised:
            await chart_history._load_generation_consistent_chart_bars(
                route.provider,
                route.instrument,
                "5m",
                "1d",
                0.5,
                store_factory=object,
                refresh_provider=False,
                executor=None,
                window=WINDOW,
                repair_window=None,
                deadline=asyncio.get_running_loop().time() + 1.0,
            )
        assert raised.value is failure

    asyncio.run(run())
    assert calls == (2 if isinstance(failure, ChartBarsGenerationChanged) else 1)


def test_coalesced_loader_single_flights_matching_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)
    chart_history._result_cache.clear()
    chart_history._inflight_tasks.clear()
    chart_history._cache_generation.clear()
    calls = 0
    release = asyncio.Event()

    async def fake_load(*_args, **_kwargs) -> ConfirmedChartHistoryLoad:
        nonlocal calls
        calls += 1
        await release.wait()
        return _history_result([], state="unknown", canonical_generation=3)

    monkeypatch.setattr(chart_history, "_load_confirmed_chart_bars", fake_load)
    _patch_chart_generation(monkeypatch, lambda *_a, **_k: 3)

    async def run() -> None:
        first = asyncio.create_task(
            coalesced_load_confirmed_chart_bars(
                "tinvest",
                route.instrument,
                "5m",
                "1d",
                0.5,
                store_factory=object,
                refresh_provider=True,
                window=WINDOW,
            )
        )
        second = asyncio.create_task(
            coalesced_load_confirmed_chart_bars(
                "tinvest",
                route.instrument,
                "5m",
                "1d",
                0.5,
                store_factory=object,
                refresh_provider=True,
                window=WINDOW,
            )
        )
        await asyncio.sleep(0)
        release.set()
        one, two = await asyncio.gather(first, second)
        assert one is two

    try:
        asyncio.run(run())
        assert calls == 1
    finally:
        chart_history._result_cache.clear()
        chart_history._inflight_tasks.clear()
        chart_history._cache_generation.clear()


def test_history_page_loader_does_not_create_a_parallel_result_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route()
    _patch_route(monkeypatch, route)
    _patch_chart_generation(monkeypatch, lambda *_args, **_kwargs: 4)
    calls = 0
    bar = _bar(START)

    def load_page(*_args, **_kwargs) -> ConfirmedChartHistoryPage:
        nonlocal calls
        calls += 1
        return ConfirmedChartHistoryPage(
            bars=(bar,),
            next_before_ts=START,
            has_more=True,
            canonical_generation=4,
        )

    monkeypatch.setattr(chart_history, "_load_confirmed_chart_history_page", load_page)

    async def run() -> None:
        await load_confirmed_chart_history_page(
            route.instrument,
            "5m",
            before_ts=END,
            limit=600,
            expected_canonical_generation=4,
            store_factory=object,
        )
        await load_confirmed_chart_history_page(
            route.instrument,
            "5m",
            before_ts=END,
            limit=600,
            expected_canonical_generation=4,
            store_factory=object,
        )

    asyncio.run(run())
    assert calls == 2


def test_chart_history_shutdown_drains_shielded_owner() -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def load() -> ConfirmedChartHistoryLoad:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
                raise
            raise AssertionError("unreachable")

        key = ("ibkr", "instrument", "route", "5m", "5d", False, None, None)
        task = asyncio.create_task(load())
        chart_history._inflight_tasks[key] = (task, 0, 0)
        await started.wait()
        shutdown = asyncio.create_task(chart_history.shutdown_chart_history_runtime())
        await asyncio.sleep(0)

        assert not shutdown.done()
        release.set()
        await shutdown
        assert chart_history._inflight_tasks == {}
        chart_history.start_chart_history_runtime()

    asyncio.run(run())


def test_chart_history_cancel_and_shutdown_settle_physical_store_initialization(
    monkeypatch,
) -> None:
    _patch_route(monkeypatch, _route())
    chart_history._result_cache.clear()
    started = threading.Event()
    release = threading.Event()
    settled = threading.Event()
    worker_threads = []

    def store_factory():
        worker_threads.append(threading.get_ident())
        started.set()
        try:
            assert release.wait(2.0)
            return object()
        finally:
            settled.set()

    async def run() -> None:
        task = asyncio.create_task(
            coalesced_load_confirmed_chart_bars(
                "tinvest",
                _instrument(),
                "5m",
                "1d",
                0.5,
                store_factory=store_factory,
                window=WINDOW,
            )
        )
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            assert worker_threads == [worker_threads[0]]
            assert worker_threads[0] != threading.get_ident()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            shutdown = asyncio.create_task(chart_history.shutdown_chart_history_runtime())
            await asyncio.sleep(0.01)
            assert not shutdown.done()
            assert not settled.is_set()
            release.set()
            await asyncio.wait_for(shutdown, 1.0)
            assert settled.is_set()
            assert not chart_history._inflight_tasks
        finally:
            release.set()
            chart_history.start_chart_history_runtime()

    asyncio.run(run())
