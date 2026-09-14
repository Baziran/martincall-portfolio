from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.runtime import chart_events
from aef_terminal.ui.services import fast_indicator_runtime as fast_indicator_runtime_module
from aef_terminal.settings_contract import instrument_indicator_setting_key
from aef_terminal.ui.services.fast_indicator_runtime import (
    FastIndicatorRuntimeDeps,
    configure_fast_indicator_runtime,
    fast_indicator_runtime_status,
    fast_indicator_snapshots,
    option_target_samples_committed,
)
from aef_terminal.ui.services.recent_confirmed_bars import (
    clear_recent_confirmed_bar_cache,
    recent_confirmed_bar_cache_stats,
)
from tests.provider_payloads import ibkr_future_payload


@pytest.fixture(autouse=True)
def _clear_shared_recent_bar_cache() -> None:
    clear_recent_confirmed_bar_cache()


def _bars() -> list[Bar]:
    started_at = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    return [
        Bar(
            symbol="ES",
            ts=started_at + timedelta(minutes=index),
            open=98.0,
            high=98.5,
            low=97.5,
            close=98.0,
            volume=100.0,
            timeframe="1m",
            source="ibkr",
            closed=True,
        )
        for index in range(40)
    ]


def _target(instrument_id: str, route_fingerprint: str) -> dict:
    sampled_at = datetime.now(tz=UTC)
    return {
        "id": "opt-fast-1",
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "symbol": "ES",
        "timeframe": "5m",
        "point": {
            "price": 100.0,
            "ts": sampled_at.isoformat(),
        },
        "payload": {
            "intent": {
                "provider_symbol": "ES",
                "mode": "normal",
                "right": "C",
                "target_dte": "0dte",
                "sec_type": "FOP",
                "con_id": 12345,
                "local_symbol": "ES C100",
                "exchange": "CME",
                "expiry": "20260807",
                "strike": 100.0,
            },
            "market_sample": {
                "fair_price": 10.0,
                "fair_price_status": "ok",
                "reference_option_price": 8.0,
                "reference_option_price_source": "bid_ask_mid",
                "live_bid": 7.9,
                "live_ask": 8.1,
                "live_quote_status": "ok",
                "live_quote_entitlement": "live",
                "live_quote_ts": sampled_at.isoformat(),
                "live_quote_time_basis": "client_receive",
                "live_underlying_price": 100.0,
                "underlying_quote_price": 100.0,
                "underlying_quote_price_source": "bid_ask_mid",
                "underlying_quote_entitlement": "live",
                "underlying_quote_status": "live",
                "underlying_quote_ts": sampled_at.isoformat(),
                "underlying_quote_time_basis": "client_receive",
                "compression_sample_at": sampled_at.isoformat(),
            },
        },
    }


def test_fast_lane_runs_one_exact_target_from_committed_sample_and_caches_bars() -> None:
    instrument = ibkr_future_payload("ES", exchange="CME")
    route = route_instrument(instrument)
    target = _target(route.instrument_id, route.fingerprint)

    class Store:
        reads = 0

        def initialize(self) -> None:
            return None

        def read_recent_bars_batch(self, requests):
            Store.reads += 1
            assert requests[0][0] == "1m"
            assert requests[0][1] == "ibkr"
            return {(route.instrument_id, "1m"): _bars()}

    settings = {
        instrument_indicator_setting_key(
            route.instrument_id,
            "optionReversalCalcEnabled",
            mode="regular",
        ): "true",
    }
    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: settings,
            lookup_runtime_instrument=lambda instrument_id: (
                instrument
                if instrument_id == route.instrument_id
                else (_ for _ in ()).throw(ValueError("unexpected instrument"))
            ),
        )
    )
    committed = {
        (route.instrument_id, route.fingerprint, target["id"]): target,
    }

    asyncio.run(option_target_samples_committed(committed, store=Store()))
    snapshots = fast_indicator_snapshots([target], "5m")

    assert len(snapshots) == 1
    scope = snapshots[0]
    assert scope["authority"] == "advisory_only"
    assert scope["bar_timeframe"] == "1m"
    assert scope["bar_canonical_generation"] == chart_events.chart_bars_updated_generation(
        "1m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    assert set(scope["by_target_id"]) == {"opt-fast-1"}
    indicator = scope["by_target_id"]["opt-fast-1"]
    assert indicator["execution"] == {
        "lane": "fast",
        "authority": "advisory_only",
        "trigger": "option_target_sample",
    }
    assert indicator["latest"]["state"] == "BUY"
    assert indicator["latest"]["target"]["id"] == "opt-fast-1"
    assert indicator["latest"]["metrics"]["near_atr"] == 0.0
    assert indicator["events"] == []
    assert indicator["signals"] == []
    assert "research_observation" not in indicator

    next_target = {
        **target,
        "payload": {
            **target["payload"],
            "market_sample": {
                **target["payload"]["market_sample"],
                "live_bid": 7.8,
                "live_ask": 8.0,
                "reference_option_price": 7.9,
            },
        },
    }
    next_committed = {
        (route.instrument_id, route.fingerprint, target["id"]): next_target,
    }
    asyncio.run(option_target_samples_committed(next_committed, store=Store()))

    assert Store.reads == 1
    assert fast_indicator_snapshots([target], "5m") is None
    assert set(fast_indicator_snapshots([next_target], "5m")[0]["by_target_id"]) == {"opt-fast-1"}
    assert fast_indicator_runtime_status()["calculated_targets"] == 2
    assert fast_indicator_runtime_status()["shared_bar_context_loads"] == 1

    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: settings,
            lookup_runtime_instrument=lambda _instrument_id: instrument,
        )
    )
    reset_status = fast_indicator_runtime_status()
    assert reset_status["configured"] is True
    assert reset_status["scopes"] == 0
    assert reset_status["calculated_targets"] == 0
    assert reset_status["publication_generation"] == 0


def test_fast_lane_cached_context_fails_closed_during_canonical_write() -> None:
    instrument = ibkr_future_payload("ES", exchange="CME")
    route = route_instrument(instrument)
    target = _target(route.instrument_id, route.fingerprint)

    class Store:
        reads = 0

        def initialize(self) -> None:
            return None

        def read_recent_bars_batch(self, _requests):
            Store.reads += 1
            return {(route.instrument_id, "1m"): _bars()}

    settings = {
        instrument_indicator_setting_key(
            route.instrument_id,
            "optionReversalCalcEnabled",
            mode="regular",
        ): "true",
    }
    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: settings,
            lookup_runtime_instrument=lambda _instrument_id: instrument,
        )
    )
    committed = {
        (route.instrument_id, route.fingerprint, target["id"]): target,
    }
    asyncio.run(option_target_samples_committed(committed, store=Store()))
    assert Store.reads == 1
    generation = chart_events.chart_bars_updated_generation(
        "1m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    guard = chart_events.begin_chart_bars_write(
        generation,
        "1m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    try:
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
            asyncio.run(option_target_samples_committed(committed, store=Store()))
        assert active.value.write_in_progress is True
    finally:
        chart_events.end_chart_bars_write(guard)

    assert Store.reads == 1
    assert fast_indicator_runtime_status()["calculated_targets"] == 1


def test_fast_lane_does_not_cache_bars_if_write_starts_during_db_read() -> None:
    instrument = ibkr_future_payload("ES", exchange="CME")
    route = route_instrument(instrument)
    target = _target(route.instrument_id, route.fingerprint)
    generation = chart_events.chart_bars_updated_generation(
        "1m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    guards: list[chart_events.ChartBarsWriteGuard] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_recent_bars_batch(self, _requests):
            guards.append(
                chart_events.begin_chart_bars_write(
                    generation,
                    "1m",
                    route.fingerprint,
                    instrument_id=route.instrument_id,
                )
            )
            return {(route.instrument_id, "1m"): _bars()}

    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: {
                instrument_indicator_setting_key(
                    route.instrument_id,
                    "optionReversalCalcEnabled",
                    mode="regular",
                ): "true",
            },
            lookup_runtime_instrument=lambda _instrument_id: instrument,
        )
    )
    committed = {
        (route.instrument_id, route.fingerprint, target["id"]): target,
    }
    try:
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
            asyncio.run(option_target_samples_committed(committed, store=Store()))
        assert active.value.write_in_progress is True
    finally:
        for guard in guards:
            chart_events.end_chart_bars_write(guard)

    status = fast_indicator_runtime_status()
    assert recent_confirmed_bar_cache_stats()["entries"] == 0
    assert status["calculated_targets"] == 0


def test_fast_lane_calc_disabled_clears_scope_without_loading_bars() -> None:
    instrument = ibkr_future_payload("ES", exchange="CME")
    route = route_instrument(instrument)
    target = _target(route.instrument_id, route.fingerprint)

    class Store:
        def initialize(self) -> None:
            return None

        def read_recent_bars_batch(self, _requests):
            raise AssertionError("disabled fast lane must not read bars")

    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: {},
            lookup_runtime_instrument=lambda _instrument_id: instrument,
        )
    )
    asyncio.run(
        option_target_samples_committed(
            {(route.instrument_id, route.fingerprint, target["id"]): target},
            store=Store(),
        )
    )

    snapshot = fast_indicator_snapshots([target], "5m")[0]
    assert snapshot["indicator"] is None
    assert snapshot["by_target_id"] == {}
    assert recent_confirmed_bar_cache_stats()["entries"] == 0


def test_fast_lane_snapshot_fails_closed_after_source_generation_advances() -> None:
    instrument = ibkr_future_payload("ES", exchange="CME")
    route = route_instrument(instrument)
    target = _target(route.instrument_id, route.fingerprint)

    class Store:
        def read_recent_bars_batch(self, _requests):
            return {(route.instrument_id, "1m"): _bars()}

    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: {
                instrument_indicator_setting_key(
                    route.instrument_id,
                    "optionReversalCalcEnabled",
                    mode="regular",
                ): "true",
            },
            lookup_runtime_instrument=lambda _instrument_id: instrument,
        )
    )
    asyncio.run(
        option_target_samples_committed(
            {(route.instrument_id, route.fingerprint, target["id"]): target},
            store=Store(),
        )
    )
    scope = fast_indicator_snapshots([target], "5m")[0]
    generation = scope["bar_canonical_generation"]

    assert (
        chart_events.publish_chart_bars_updated(
            "ibkr",
            "1m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            expected_generation=generation,
        )
        == generation + 1
    )
    assert fast_indicator_snapshots([target], "5m") is None


def test_fast_lane_failed_multi_scope_batch_publishes_no_partial_state() -> None:
    first = ibkr_future_payload("ES", exchange="CME")
    second = ibkr_future_payload("NQ", exchange="CME")
    first_route = route_instrument(first)
    second_route = route_instrument(second)
    first_target = _target(first_route.instrument_id, first_route.fingerprint)
    second_target = {
        **_target(second_route.instrument_id, second_route.fingerprint),
        "id": "opt-fast-2",
        "symbol": "NQ",
    }

    class Store:
        def read_recent_bars_batch(self, _requests):
            return {(first_route.instrument_id, "1m"): _bars()}

    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=Store,
            client_settings_snapshot=lambda: {
                instrument_indicator_setting_key(
                    first_route.instrument_id,
                    "optionReversalCalcEnabled",
                    mode="regular",
                ): "true",
            },
            lookup_runtime_instrument=lambda instrument_id: (
                first
                if instrument_id == first_route.instrument_id
                else (_ for _ in ()).throw(RuntimeError("second scope failed"))
            ),
        )
    )

    with pytest.raises(RuntimeError, match="second scope failed"):
        asyncio.run(
            option_target_samples_committed(
                {
                    (first_route.instrument_id, first_route.fingerprint, first_target["id"]): (
                        first_target
                    ),
                    (
                        second_route.instrument_id,
                        second_route.fingerprint,
                        second_target["id"],
                    ): second_target,
                },
                store=Store(),
            )
        )

    status = fast_indicator_runtime_status()
    assert status["scopes"] == 0
    assert status["committed_batches"] == 0
    assert status["publication_generation"] == 0


def test_fast_lane_settles_calculation_worker_before_cancellation(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def calculate(*_args, **_kwargs) -> None:
        started.set()
        assert release.wait(1.0)
        finished.set()

    monkeypatch.setattr(
        fast_indicator_runtime_module,
        "_calculate_committed_samples",
        calculate,
    )

    async def run() -> None:
        task = asyncio.create_task(
            option_target_samples_committed(
                {("instrument", "route", "target"): {"id": "target"}},
                store=object(),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()

    assert finished.is_set()
