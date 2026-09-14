from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_sessions import ProviderSessionBarSlotCoverage
from aef_terminal.ui.services import trading_hours_refresh
from aef_terminal.ui.services.closed_session_maintenance_models import (
    ClosedSessionMaintenanceCycle,
    ClosedSessionMaintenanceSettings,
    ClosedSessionMaintenanceState,
    ProviderRequestCompletionOutcome,
)
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


def _closed_quality() -> dict[str, Any]:
    return {
        "status": "gap+market_closed",
        "market_closed": True,
        "session_unknown": False,
        "schedule_state": "verified",
        "repair_schedule_state": "verified",
        "gaps": [
            {
                "from": "2026-07-17T20:45:00+00:00",
                "to": "2026-07-17T20:55:00+00:00",
                "missing_bars": 1,
                "repair_missing_bars": 1,
                "repair_missing_slots": ["2026-07-17T20:50:00+00:00"],
            }
        ],
    }


@pytest.fixture(autouse=True)
def _reset_schedule_singleflight(monkeypatch: pytest.MonkeyPatch) -> None:
    trading_hours_refresh._trading_hours_fetch_tasks.clear()
    trading_hours_refresh._trading_hours_fetch_results.clear()
    monkeypatch.setattr(
        trading_hours_refresh,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: ProviderSessionBarSlotCoverage(
            "verified",
            (),
            False,
            None,
        ),
    )


def test_adjacent_sessions_do_not_admit_unknown_current_provider_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES", con_id=517)
    open_state_calls: list[bool] = []
    fetch_calls: list[bool] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_trading_hours(self, *, instrument: dict[str, Any]) -> dict[str, Any]:
            return {"age_seconds": 60.0}

        def read_trading_session_open_state(self, *_args, **_kwargs) -> bool:
            open_state_calls.append(True)
            return False

    async def refresh_schedule(*_args: Any, **_kwargs: Any) -> bool:
        fetch_calls.append(True)
        return True

    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: ProviderSessionBarSlotCoverage(
            "unknown",
            (),
            False,
            None,
        ),
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "fetch_and_store_trading_hours",
        refresh_schedule,
    )

    cycle = asyncio.run(
        trading_hours_refresh.refresh_trading_hours_once(
            trading_hours_refresh.TradingHoursRefreshDeps(
                store_factory=Store,
                server_sleeping=lambda: False,
                selected_instruments=lambda: [instrument],
                logger_warning=lambda *_args, **_kwargs: None,
            )
        )
    )

    assert fetch_calls == [True]
    assert open_state_calls == []
    assert cycle.schedule_ready_routes == 0
    assert cycle.unknown_routes == 1
    assert cycle.schedule_fetch_failures == 1


def test_provider_admission_budget_is_rolling_and_uses_production_defaults() -> None:
    defaults = ClosedSessionMaintenanceSettings()
    assert defaults.admission_window_seconds == 12 * 60 * 60
    assert defaults.max_provider_admissions_per_window == 32
    assert defaults.max_provider_admissions_per_cycle == 1
    assert defaults.failure_threshold == 3
    assert defaults.breaker_backoff_seconds == 6 * 60 * 60

    settings = ClosedSessionMaintenanceSettings(
        admission_window_seconds=12 * 60 * 60,
        max_provider_admissions_per_window=2,
    )
    state = ClosedSessionMaintenanceState(settings=settings)
    state.record_provider_request("ibkr", now=0.0)
    state.record_provider_request("ibkr", now=60.0)

    blocked = state.provider_admission_gate("ibkr", now=120.0)
    assert blocked["admit"] is False
    assert blocked["blocker_reason"] == "provider_window_budget_exhausted"
    assert blocked["admitted_requests"] == 2
    assert blocked["remaining_requests"] == 0

    rolled = state.provider_admission_gate("ibkr", now=12 * 60 * 60 + 1.0)
    assert rolled["admit"] is True
    assert rolled["admitted_requests"] == 1
    assert rolled["remaining_requests"] == 1


def test_provider_circuit_breaker_classifies_completion_results_and_expires() -> None:
    settings = ClosedSessionMaintenanceSettings(
        failure_threshold=3,
        breaker_backoff_seconds=6 * 60 * 60,
    )
    state = ClosedSessionMaintenanceState(settings=settings)

    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.FAILURE,
            now=10.0,
        )
        is ProviderRequestCompletionOutcome.FAILURE
    )
    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.NEUTRAL,
            now=11.0,
        )
        is ProviderRequestCompletionOutcome.NEUTRAL
    )
    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.FAILURE,
            now=12.0,
        )
        is ProviderRequestCompletionOutcome.FAILURE
    )
    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.FAILURE,
            now=13.0,
        )
        is ProviderRequestCompletionOutcome.FAILURE
    )

    opened = state.provider_admission_gate("ibkr", now=14.0)
    assert opened["failure_streak"] == 3
    assert opened["breaker_open"] is True
    assert opened["blocker_reason"] == "provider_circuit_open"

    expired = state.provider_admission_gate(
        "ibkr",
        now=13.0 + 6 * 60 * 60,
    )
    assert expired["breaker_open"] is False
    assert expired["failure_streak"] == 0

    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.FAILURE,
            now=30_000.0,
        )
        is ProviderRequestCompletionOutcome.FAILURE
    )
    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.NEUTRAL,
            now=30_000.5,
        )
        is ProviderRequestCompletionOutcome.NEUTRAL
    )
    assert (
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.SUCCESS,
            now=30_001.0,
        )
        is ProviderRequestCompletionOutcome.SUCCESS
    )
    assert state.provider_admission_gate("ibkr", now=30_002.0)["failure_streak"] == 0


def test_provider_circuit_breaker_rejects_untyped_completion_payloads() -> None:
    state = ClosedSessionMaintenanceState()

    with pytest.raises(TypeError, match="PROVIDER_REQUEST_COMPLETION_OUTCOME_REQUIRED"):
        state.record_provider_request_completion(  # type: ignore[arg-type]
            "ibkr",
            {"status": "error", "reason": "cancelled"},
        )

    assert state.provider_admission_gate("ibkr")["failure_streak"] == 0


def test_provider_connection_recovery_opens_one_fresh_schedule_probe() -> None:
    state = ClosedSessionMaintenanceState()
    for observed_at in (10.0, 11.0, 12.0):
        state.record_provider_request("ibkr", now=observed_at)
        state.record_provider_request_completion(
            "ibkr",
            ProviderRequestCompletionOutcome.FAILURE,
            now=observed_at,
        )
    state.record_provider_schedule_retry("ibkr", 60.0, now=12.0)
    assert state.provider_admission_gate("ibkr", now=13.0)["breaker_open"] is True

    ibkr_key = ("ibkr", "instrument:es", "route:es", "2026-08-17", "2026-08-24")
    tinvest_key = (
        "tinvest",
        "instrument:sber",
        "route:sber",
        "2026-08-17",
        "2026-08-24",
    )
    trading_hours_refresh._trading_hours_fetch_results[ibkr_key] = (999.0, False)
    trading_hours_refresh._trading_hours_fetch_results[tinvest_key] = (999.0, False)
    wakeup_event = asyncio.Event()

    trading_hours_refresh.recover_provider_trading_hours_after_connection(
        "ibkr",
        maintenance_state=state,
        wakeup_event=wakeup_event,
    )

    recovered = state.provider_admission_gate("ibkr", now=13.0)
    assert recovered["breaker_open"] is False
    assert recovered["failure_streak"] == 0
    assert recovered["admitted_requests"] == 3
    assert state.provider_schedule_retry_remaining("ibkr", now=13.0) == 0.0
    assert ibkr_key not in trading_hours_refresh._trading_hours_fetch_results
    assert tinvest_key in trading_hours_refresh._trading_hours_fetch_results
    assert wakeup_event.is_set() is True


def test_provider_connection_recovery_wakes_idle_schedule_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wakeup_event = asyncio.Event()
    calls = 0
    cycle = ClosedSessionMaintenanceCycle(
        status="ok",
        checked_at="2026-08-17T04:45:00+00:00",
        work_remaining=False,
    )

    async def refresh_once(_deps: object) -> ClosedSessionMaintenanceCycle:
        nonlocal calls
        calls += 1
        if calls == 1:
            asyncio.get_running_loop().call_soon(wakeup_event.set)
            return cycle
        raise asyncio.CancelledError

    monkeypatch.setattr(trading_hours_refresh, "refresh_trading_hours_once", refresh_once)
    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=object,
        server_sleeping=lambda: False,
        selected_instruments=lambda: [],
        logger_warning=lambda *_args, **_kwargs: None,
        wakeup_event=wakeup_event,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            trading_hours_refresh.run_trading_hours_refresh_loop(
                deps,
                startup_delay_seconds=0.0,
            )
        )

    assert calls == 2
    assert wakeup_event.is_set() is False


def test_schedule_fetches_are_max_one_per_provider_cycle_and_round_robin_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instruments = [
        ibkr_future_payload("ES", con_id=701),
        ibkr_future_payload("GC", con_id=702, exchange="COMEX"),
    ]
    routes = sorted(
        (route_instrument(instrument) for instrument in instruments),
        key=lambda route: (route.instrument_id, route.fingerprint),
    )
    refreshed: set[str] = set()
    fetched: list[str] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_trading_hours(self, *, instrument: dict[str, Any]):
            instrument_id = route_instrument(instrument).instrument_id
            return {"age_seconds": 60.0} if instrument_id in refreshed else None

        def read_trading_session_open_state(
            self,
            _ts,
            _session_type: str,
            *,
            instrument: dict[str, Any],
        ) -> bool | None:
            instrument_id = route_instrument(instrument).instrument_id
            return False if instrument_id in refreshed else None

    async def fetch_schedule(_store, route, **_kwargs):
        fetched.append(route.instrument_id)
        refreshed.add(route.instrument_id)
        return True

    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "fetch_and_store_trading_hours",
        fetch_schedule,
    )
    settings = ClosedSessionMaintenanceSettings(intervals=("5m",))
    state = ClosedSessionMaintenanceState(settings=settings)
    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=Store,
        server_sleeping=lambda: False,
        selected_instruments=lambda: list(instruments),
        logger_warning=lambda *_args, **_kwargs: None,
        maintenance_settings=settings,
        maintenance_state=state,
    )

    first = asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))
    second = asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))

    assert fetched == [routes[0].instrument_id, routes[1].instrument_id]
    assert first.schedule_fetch_attempts == 1
    assert first.schedule_cycle_deferred == 1
    assert first.work_remaining is True
    assert second.schedule_fetch_attempts == 1
    assert second.cached_verified_schedules == 1
    assert second.work_remaining is False
    assert state.provider_schedule_round_robin_cursor["ibkr"] == (
        routes[1].instrument_id,
        routes[1].fingerprint,
    )
    assert state.provider_admission_gate("ibkr")["admitted_requests"] == 2


def test_active_chart_schedule_demand_precedes_background_round_robin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = ibkr_stock_payload("VIX", con_id=703, asset_class="index", sec_type="IND")
    active = ibkr_future_payload("ES", con_id=704)
    active_route = route_instrument(active)
    fetched: list[str] = []

    class Store:
        def read_trading_hours(self, *, instrument: dict[str, Any]):
            return None

    async def fetch_schedule(_store, route, **_kwargs):
        fetched.append(route.instrument_id)
        return False

    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "fetch_and_store_trading_hours",
        fetch_schedule,
    )
    settings = ClosedSessionMaintenanceSettings(intervals=("5m",))
    state = ClosedSessionMaintenanceState(settings=settings)

    cycle = asyncio.run(
        trading_hours_refresh.refresh_trading_hours_once(
            trading_hours_refresh.TradingHoursRefreshDeps(
                store_factory=Store,
                server_sleeping=lambda: False,
                selected_instruments=lambda: [background, active],
                active_chart_streams=lambda: [(active_route.instrument_id, "5m", "5d", 1)],
                logger_warning=lambda *_args, **_kwargs: None,
                maintenance_settings=settings,
                maintenance_state=state,
            )
        )
    )

    assert fetched == [active_route.instrument_id]
    assert cycle.schedule_fetch_attempts == 1
    assert cycle.schedule_retry_deferred == 1
    assert cycle.work_remaining is True


def test_post_fetch_unknown_trips_breaker_and_keeps_retry_work_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES", con_id=704)
    fetch_calls = 0

    class Store:
        def initialize(self) -> None:
            return None

        def read_trading_hours(self, *, instrument: dict[str, Any]):
            return None

        def read_trading_session_open_state(self, *_args, **_kwargs) -> None:
            return None

    async def fetch_schedule(*_args, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return True

    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "fetch_and_store_trading_hours",
        fetch_schedule,
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "provider_session_bar_slot_coverage_between",
        lambda *_args, **_kwargs: ProviderSessionBarSlotCoverage(
            "unknown",
            (),
            False,
            None,
        ),
    )
    settings = ClosedSessionMaintenanceSettings(
        intervals=("5m",),
        failure_threshold=3,
    )
    state = ClosedSessionMaintenanceState(settings=settings)
    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=Store,
        server_sleeping=lambda: False,
        selected_instruments=lambda: [instrument],
        logger_warning=lambda *_args, **_kwargs: None,
        maintenance_settings=settings,
        maintenance_state=state,
    )

    first = asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))
    retry_blocked = asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))
    assert first.schedule_fetch_failures == 1
    assert first.work_remaining is True
    assert retry_blocked.schedule_retry_deferred == 1
    assert retry_blocked.work_remaining is True
    assert fetch_calls == 1

    state.clear_provider_schedule_retry("ibkr")
    asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))
    state.clear_provider_schedule_retry("ibkr")
    third_failure = asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))
    breaker_blocked = asyncio.run(trading_hours_refresh.refresh_trading_hours_once(deps))

    assert third_failure.provider_safety["ibkr"]["failure_streak"] == 3
    assert third_failure.provider_safety["ibkr"]["breaker_open"] is True
    assert breaker_blocked.schedule_safety_deferred == 1
    assert breaker_blocked.provider_schedule_block_reasons == {"ibkr": "provider_circuit_open"}
    assert breaker_blocked.work_remaining is False
    assert fetch_calls == 3


def test_schedule_refresh_uses_one_bounded_key_for_gate_cache_and_force_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        instrument = ibkr_future_payload("ES", con_id=707)
        route = route_instrument(instrument)
        required_at = datetime(2026, 7, 23, 8, 15, tzinfo=UTC)
        fetch_calls = 0

        class Store:
            def read_trading_hours(self, *, instrument: dict[str, Any]):
                return None

        async def fetch_once(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal fetch_calls
            fetch_calls += 1
            return False

        monkeypatch.setattr(
            trading_hours_refresh,
            "_fetch_and_store_trading_hours_once",
            fetch_once,
        )
        deps = trading_hours_refresh.TradingHoursRefreshDeps(
            store_factory=Store,
            server_sleeping=lambda: False,
            selected_instruments=lambda: [instrument],
            logger_warning=lambda *_args, **_kwargs: None,
        )
        provider_contract_id = route.adapter.session_contract_id(route.instrument)

        first = await trading_hours_refresh._refresh_instrument_trading_hours(
            deps,
            Store(),
            provider_contract_id,
            route,
            force_refresh=False,
            required_at=required_at,
            provider_fetch_blocker="",
        )
        shared = await trading_hours_refresh._refresh_instrument_trading_hours(
            deps,
            Store(),
            provider_contract_id,
            route,
            force_refresh=False,
            required_at=required_at,
            provider_fetch_blocker="",
        )
        forced = await trading_hours_refresh._refresh_instrument_trading_hours(
            deps,
            Store(),
            provider_contract_id,
            route,
            force_refresh=True,
            required_at=required_at,
            provider_fetch_blocker="",
        )

        bounded_key = trading_hours_refresh._trading_hours_fetch_key(
            route,
            starts_at=required_at,
            ends_at=required_at + timedelta(days=7),
        )
        unbounded_key = trading_hours_refresh._trading_hours_fetch_key(route)
        assert first["fetch_started"] is True
        assert shared["fetch_started"] is False
        assert float(shared["retry_after_seconds"]) > 0
        assert forced["fetch_started"] is True
        assert fetch_calls == 2
        assert bounded_key in trading_hours_refresh._trading_hours_fetch_results
        assert unbounded_key not in trading_hours_refresh._trading_hours_fetch_results

    asyncio.run(run())


def test_active_chart_schedule_refresh_repairs_first_persistent_axis_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        instrument = ibkr_future_payload("ES", con_id=709)
        route = route_instrument(instrument)
        axis_anchor = datetime(2026, 5, 3, 22, tzinfo=UTC)
        gap_start = datetime(2026, 8, 11, 21, tzinfo=UTC)
        required_at = datetime(2026, 8, 17, 6, 35, tzinfo=UTC)
        fetched = False
        fetch_windows: list[tuple[datetime, datetime]] = []

        class Store:
            def read_trading_hours(self, *, instrument: dict[str, Any]):
                return {"age_seconds": 60.0}

            def read_trading_session_axis_anchor(self, **_kwargs: Any) -> datetime:
                return axis_anchor

        def schedule_coverage(
            starts_at: datetime,
            ends_at: datetime,
            _interval: str,
            **_kwargs: Any,
        ) -> ProviderSessionBarSlotCoverage:
            if starts_at == required_at and ends_at == required_at + timedelta(minutes=1):
                return ProviderSessionBarSlotCoverage("verified", (), False, None)
            assert starts_at == axis_anchor
            return ProviderSessionBarSlotCoverage(
                "verified" if fetched else "unknown",
                (),
                False,
                None,
                verified_until=None if fetched else gap_start,
            )

        async def fetch_schedule(
            _store: Any,
            _route: Any,
            *,
            starts_at: datetime,
            ends_at: datetime,
            **_kwargs: Any,
        ) -> bool:
            nonlocal fetched
            fetch_windows.append((starts_at, ends_at))
            fetched = True
            return True

        monkeypatch.setattr(
            trading_hours_refresh,
            "provider_session_bar_slot_coverage_between",
            schedule_coverage,
        )
        monkeypatch.setattr(
            trading_hours_refresh,
            "fetch_and_store_trading_hours",
            fetch_schedule,
        )
        deps = trading_hours_refresh.TradingHoursRefreshDeps(
            store_factory=Store,
            server_sleeping=lambda: False,
            selected_instruments=lambda: [instrument],
            logger_warning=lambda *_args, **_kwargs: None,
        )

        result = await trading_hours_refresh._refresh_instrument_trading_hours(
            deps,
            Store(),
            route.adapter.session_contract_id(route.instrument),
            route,
            force_refresh=False,
            require_chart_axis=True,
            required_at=required_at,
            provider_fetch_blocker="",
        )

        assert fetch_windows == [(gap_start, required_at + timedelta(days=7))]
        assert result["schedule_state"] == "verified"
        assert result["chart_axis_state"] == "verified"
        assert result["status"] == "schedule_fetch_verified"

    asyncio.run(run())


def test_superseded_schedule_fetch_is_not_cached_and_keeps_refresh_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    async def run() -> None:
        instrument = ibkr_future_payload("ES", con_id=708)
        route = route_instrument(instrument)
        starts_at = datetime(2026, 7, 23, 8, 15, tzinfo=UTC)
        ends_at = starts_at + timedelta(days=7)
        writes = 0

        class Store:
            def upsert_trading_hours(self, *_args: Any, **_kwargs: Any) -> None:
                nonlocal writes
                writes += 1

        async def fetch_schedule(
            _adapter,
            _instrument,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            sessions_module.mark_provider_trading_hours_stale(
                instrument=instrument,
            )
            return {
                "time_zone": "America/New_York",
                "trading_hours": "20260723:0400-20260723:2000",
                "liquid_hours": "20260723:0930-20260723:1600",
            }

        monkeypatch.setattr(
            type(route.adapter),
            "fetch_trading_schedule",
            fetch_schedule,
        )
        sessions_module.mark_provider_trading_hours_stale(
            instrument=instrument,
        )
        key = trading_hours_refresh._trading_hours_fetch_key(
            route,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        try:
            with pytest.raises(trading_hours_refresh.TradingHoursRefreshSuperseded):
                await trading_hours_refresh.fetch_and_store_trading_hours(
                    Store(),
                    route,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            await asyncio.sleep(0)
            assert writes == 0
            assert key not in trading_hours_refresh._trading_hours_fetch_results
            assert (
                route.instrument_id,
                route.fingerprint,
            ) in sessions_module.peek_pending_provider_trading_hours_refresh(route.provider)
        finally:
            current_generation = sessions_module.provider_trading_hours_refresh_generation(
                route.provider,
                route.instrument_id,
                route.fingerprint,
            )
            sessions_module.acknowledge_provider_trading_hours_refresh(
                route.provider,
                route.instrument_id,
                route.fingerprint,
                expected_generation=current_generation,
            )

    asyncio.run(run())


def test_schedule_fetch_settles_store_and_acknowledgement_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.data import provider_sessions as sessions_module

    async def run() -> None:
        instrument = ibkr_future_payload("ES", con_id=710)
        route = route_instrument(instrument)
        starts_at = datetime(2026, 7, 24, 8, 15, tzinfo=UTC)
        ends_at = starts_at + timedelta(days=7)
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        class Store:
            def upsert_trading_hours(self, *_args: Any, **_kwargs: Any) -> None:
                started.set()
                assert release.wait(timeout=1.0)
                completed.set()

        async def fetch_schedule(
            _adapter,
            _instrument,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {
                "time_zone": "America/New_York",
                "trading_hours": "20260724:0400-20260724:2000",
                "liquid_hours": "20260724:0930-20260724:1600",
            }

        monkeypatch.setattr(
            type(route.adapter),
            "fetch_trading_schedule",
            fetch_schedule,
        )
        sessions_module.mark_provider_trading_hours_stale(instrument=instrument)
        key = trading_hours_refresh._trading_hours_fetch_key(
            route,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        task = asyncio.create_task(
            trading_hours_refresh.fetch_and_store_trading_hours(
                Store(),
                route,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 1.0)
            task.cancel()
            await asyncio.sleep(0)
            assert task.done() is False
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            assert completed.is_set()
            assert key not in trading_hours_refresh._trading_hours_fetch_tasks
            assert trading_hours_refresh._trading_hours_fetch_results[key][1] is True
            assert (
                route.instrument_id,
                route.fingerprint,
            ) not in sessions_module.peek_pending_provider_trading_hours_refresh(route.provider)
        finally:
            release.set()
            current_generation = sessions_module.provider_trading_hours_refresh_generation(
                route.provider,
                route.instrument_id,
                route.fingerprint,
            )
            sessions_module.acknowledge_provider_trading_hours_refresh(
                route.provider,
                route.instrument_id,
                route.fingerprint,
                expected_generation=current_generation,
            )

    asyncio.run(run())


def test_superseded_schedule_cycle_stays_active_without_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES", con_id=709)

    class Store:
        def initialize(self) -> None:
            return None

    async def refresh_superseded(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "refresh_superseded",
            "schedule_state": "unknown",
            "fetch_started": True,
            "provider_result": None,
            "retry_after_seconds": 0.0,
        }

    monkeypatch.setattr(
        trading_hours_refresh,
        "_refresh_instrument_trading_hours",
        refresh_superseded,
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    settings = ClosedSessionMaintenanceSettings(intervals=("5m",))
    state = ClosedSessionMaintenanceState(settings=settings)
    cycle = asyncio.run(
        trading_hours_refresh.refresh_trading_hours_once(
            trading_hours_refresh.TradingHoursRefreshDeps(
                store_factory=Store,
                server_sleeping=lambda: False,
                selected_instruments=lambda: [instrument],
                logger_warning=lambda *_args, **_kwargs: None,
                maintenance_settings=settings,
                maintenance_state=state,
            )
        )
    )

    assert cycle.schedule_fetch_attempts == 1
    assert cycle.schedule_fetch_failures == 0
    assert cycle.failures == 0
    assert cycle.work_remaining is True
    assert state.provider_schedule_retry_remaining("ibkr") == 0.0
    assert state.provider_safety_snapshot()["ibkr"]["failure_streak"] == 0


def test_schedule_fetch_exception_is_exactly_reported_and_keeps_retry_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES", con_id=710)
    route = route_instrument(instrument)
    warnings: list[tuple[object, ...]] = []

    async def refresh_failed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "schedule_fetch_exception",
            "schedule_state": "unknown",
            "fetch_started": True,
            "provider_result": None,
            "error": "IBKR schedule transport reset",
            "retry_after_seconds": 60.0,
        }

    monkeypatch.setattr(
        trading_hours_refresh,
        "_refresh_instrument_trading_hours",
        refresh_failed,
    )
    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    settings = ClosedSessionMaintenanceSettings(intervals=("5m",))
    state = ClosedSessionMaintenanceState(settings=settings)
    cycle = asyncio.run(
        trading_hours_refresh.refresh_trading_hours_once(
            trading_hours_refresh.TradingHoursRefreshDeps(
                store_factory=object,
                server_sleeping=lambda: False,
                selected_instruments=lambda: [instrument],
                logger_warning=lambda *args: warnings.append(args),
                maintenance_settings=settings,
                maintenance_state=state,
            )
        )
    )

    assert cycle.work_remaining is True
    assert cycle.schedule_fetch_failures == 1
    assert cycle.schedule_errors == [
        {
            "provider": "ibkr",
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "provider_contract_id": route.adapter.session_contract_id(route.instrument),
            "status": "schedule_fetch_exception",
            "message": "IBKR schedule transport reset",
        }
    ]
    assert cycle.as_dict()["schedule_errors"] == cycle.schedule_errors
    assert warnings[-1][-1] == "IBKR schedule transport reset"


def test_cached_verified_schedule_does_not_reset_shared_failure_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES", con_id=705)
    settings = ClosedSessionMaintenanceSettings(intervals=("5m",))
    state = ClosedSessionMaintenanceState(settings=settings)
    state.record_provider_request_completion("ibkr", ProviderRequestCompletionOutcome.FAILURE)
    state.record_provider_request_completion("ibkr", ProviderRequestCompletionOutcome.FAILURE)

    class Store:
        def initialize(self) -> None:
            return None

        def read_trading_hours(self, *, instrument: dict[str, Any]):
            return {"age_seconds": 60.0}

        def read_trading_session_open_state(self, *_args, **_kwargs) -> bool:
            return False

    monkeypatch.setattr(
        trading_hours_refresh,
        "peek_pending_provider_trading_hours_refresh",
        lambda _provider: (),
    )
    cycle = asyncio.run(
        trading_hours_refresh.refresh_trading_hours_once(
            trading_hours_refresh.TradingHoursRefreshDeps(
                store_factory=Store,
                server_sleeping=lambda: False,
                selected_instruments=lambda: [instrument],
                logger_warning=lambda *_args, **_kwargs: None,
                maintenance_settings=settings,
                maintenance_state=state,
            )
        )
    )

    assert cycle.cached_verified_schedules == 1
    assert cycle.schedule_fetch_attempts == 0
    assert state.provider_admission_gate("ibkr")["failure_streak"] == 2


def test_pending_schedule_under_breaker_uses_idle_loop_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    cycle = ClosedSessionMaintenanceCycle(
        status="ok",
        checked_at="2026-07-19T00:00:00+00:00",
        circuit_deferred_repairs=1,
        work_remaining=False,
        provider_schedule_block_reasons={"ibkr": "provider_circuit_open"},
    )

    class Store:
        def initialize(self) -> None:
            return None

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    async def refresh_once(_deps):
        return cycle

    monkeypatch.setattr(trading_hours_refresh.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(trading_hours_refresh, "refresh_trading_hours_once", refresh_once)
    monkeypatch.setattr(
        trading_hours_refresh,
        "pending_provider_trading_hours_refresh_count",
        lambda: 1,
    )
    settings = ClosedSessionMaintenanceSettings(
        active_poll_seconds=30.0,
        idle_poll_seconds=1800.0,
    )
    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=Store,
        server_sleeping=lambda: False,
        selected_instruments=lambda: [],
        logger_warning=lambda *_args, **_kwargs: None,
        maintenance_settings=settings,
        now_utc=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            trading_hours_refresh.run_trading_hours_refresh_loop(
                deps,
                startup_delay_seconds=0.0,
            )
        )

    assert sleeps == [0.0, 1800.0]


def test_pending_schedule_retry_uses_active_loop_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    cycle = ClosedSessionMaintenanceCycle(
        status="ok",
        checked_at="2026-08-17T05:01:02+00:00",
        schedule_retry_deferred=1,
        work_remaining=True,
        provider_schedule_block_reasons={"ibkr": "provider_schedule_retry_not_before"},
    )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    async def refresh_once(_deps: object) -> ClosedSessionMaintenanceCycle:
        return cycle

    monkeypatch.setattr(trading_hours_refresh.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(trading_hours_refresh, "refresh_trading_hours_once", refresh_once)
    settings = ClosedSessionMaintenanceSettings(
        active_poll_seconds=30.0,
        idle_poll_seconds=1800.0,
    )
    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=object,
        server_sleeping=lambda: False,
        selected_instruments=lambda: [],
        logger_warning=lambda *_args, **_kwargs: None,
        maintenance_settings=settings,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            trading_hours_refresh.run_trading_hours_refresh_loop(
                deps,
                startup_delay_seconds=0.0,
            )
        )

    assert sleeps == [0.0, 30.0]


def test_rollover_cancellation_waits_for_physical_lifecycle_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES", con_id=811)
    started = threading.Event()
    release = threading.Event()

    def resolve_current_contract(_store: object, current: dict[str, Any]) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=1.0)
        return current

    monkeypatch.setattr(trading_hours_refresh, "futures_root", lambda _instrument: "ES")
    monkeypatch.setattr(
        trading_hours_refresh,
        "current_contract_is_stale",
        lambda _instrument: True,
    )
    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=object,
        server_sleeping=lambda: False,
        selected_instruments=lambda: [instrument],
        logger_warning=lambda *_args, **_kwargs: None,
        resolve_current_futures_contract=resolve_current_contract,
    )

    async def run() -> None:
        task = asyncio.create_task(trading_hours_refresh.refresh_trading_hours_once(deps))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_committed_schedule_publishes_runtime_route_before_analysis_invalidation() -> None:
    instrument = ibkr_future_payload("ES", con_id=812)
    route = route_instrument(instrument)
    store = object()
    calls: list[tuple[str, object]] = []

    def refresh_routes(current_store: object, **kwargs: Any) -> None:
        assert current_store is store
        assert kwargs == {"route_generation_changed": True}
        calls.append(("routes", current_store))

    async def invalidate_analysis(instrument_id: str, route_fingerprint: str) -> int:
        assert (instrument_id, route_fingerprint) == (
            route.instrument_id,
            route.fingerprint,
        )
        calls.append(("analysis", route_fingerprint))
        return 1

    deps = trading_hours_refresh.TradingHoursRefreshDeps(
        store_factory=lambda: store,
        server_sleeping=lambda: False,
        selected_instruments=lambda: [instrument],
        logger_warning=lambda *_args, **_kwargs: None,
        refresh_quote_routes=refresh_routes,
        invalidate_market_analysis_route=invalidate_analysis,
    )

    asyncio.run(
        trading_hours_refresh._publish_runtime_trading_schedule(
            deps,
            store,
            route,
        )
    )

    assert calls == [("routes", store), ("analysis", route.fingerprint)]
