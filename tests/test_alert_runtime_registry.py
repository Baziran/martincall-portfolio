from __future__ import annotations

from tests.provider_payloads import quote_route_snapshot

import asyncio
import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.alerts.runtime_registry import AlertQuoteEvent, AlertRuntimeRegistry
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.runtime import chart_events
from aef_terminal.ui.alert_runtime_rules import alert_monitor_payload
from aef_terminal.ui import server_alert_runtime
from aef_terminal.ui.services.server_alert_monitor import (
    AlertCrossingBaseline,
    AlertCrossingScope,
    ServerAlertMonitorTickDeps,
    process_server_alert_monitor_tick,
    process_server_alert_quote_event,
)
from aef_terminal.ui.services.recent_confirmed_bars import RecentConfirmedBarContext
from aef_terminal.ui.services.server_alert_selection import require_active_price_alerts
from tests.provider_payloads import ibkr_future_payload


def _alert(
    alert_id: str = "alert-1",
    *,
    armed: bool = True,
    fired: bool = False,
    timeframe: str = "5m",
    price: float = 100.0,
) -> dict:
    return {
        "id": alert_id,
        "instrument_id": "instrument-1",
        "route_fingerprint": "route-1",
        "symbol": "ONE",
        "timeframe": timeframe,
        "provider": "ibkr",
        "provider_contract_id": "contract-1",
        "kind": "price",
        "label": "",
        "direction": "cross",
        "price": price,
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        "enabled": True,
        "armed": armed,
        "fired": fired,
        "cooldownUntil": 0,
        "rearmedAt": 1,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }


async def _empty_paper_snapshot(_store) -> tuple[list[dict], list[dict]]:
    return [], []


def _bar_alert_deps(
    *,
    alert: dict[str, Any],
    instrument: dict[str, Any],
    store: Any,
    dynamic_state: Callable[..., dict[str, Any] | None],
    vsa_state: Callable[..., dict[str, Any] | None],
    cross_hit: Callable[..., bool],
    crossing_baselines: dict[AlertCrossingScope, AlertCrossingBaseline] | None = None,
) -> ServerAlertMonitorTickDeps:
    route = route_instrument(instrument)
    timestamp = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    return ServerAlertMonitorTickDeps(
        store_factory=lambda: store,
        quote_route_snapshot=lambda snapshot=quote_route_snapshot([instrument]): snapshot,
        server_pending_price_alerts_snapshot=lambda _store: [],
        server_active_price_alerts_snapshot=lambda _store: [alert],
        active_paper_orders=lambda rows: rows,
        set_server_alert_wanted=lambda _ids: None,
        quote_cache_for_instruments=lambda *_args, **_kwargs: (
            {route.fingerprint: {"price": 101.0, "ts": timestamp}},
            "",
            None,
        ),
        logger=logging.getLogger(__name__),
        paper_order_execution_transition=lambda *_args, **_kwargs: {},
        screen_execution_price_snapshot=lambda *_args, **_kwargs: None,
        queue_paper_trade_telegram=lambda *_args, **_kwargs: None,
        quote_for_route=lambda quotes, fingerprint: quotes[fingerprint],
        server_alert_price_snapshot=lambda quote: {
            "price": quote["price"],
            "high": quote["price"],
            "low": quote["price"],
            "ts": quote["ts"],
            "source": "quote",
        },
        server_vsa_fuel_alert_state=vsa_state,
        send_server_telegram_alert=lambda _payload: {"ok": True},
        alert_monitor_payload=alert_monitor_payload,
        server_dynamic_alert_state=dynamic_state,
        server_alert_indicator_params=lambda: {},
        alert_freeze_active=lambda *_args, **_kwargs: False,
        crossing_baselines=crossing_baselines if crossing_baselines is not None else {},
        server_ema_touch_direction=lambda *_args, **_kwargs: "touch",
        ema_touch_rearm_ms=lambda _alert: 60_000,
        alert_cross_hit=cross_hit,
        server_alert_freeze_ms=lambda _timeframe: 60_000,
        paper_order_price_snapshot=lambda _order: None,
        paper_journal_active_snapshot=_empty_paper_snapshot,
        set_paper_option_quote_demand=lambda _entities: 0,
        server_active_price_alerts_for_route=lambda _instrument_id, _fingerprint: [alert],
    )


def test_active_alert_snapshot_rejects_inert_and_malformed_rows_as_a_whole() -> None:
    with pytest.raises(RuntimeError, match="ACTIVE_ROW_INERT"):
        require_active_price_alerts([_alert(armed=False, fired=True)])
    with pytest.raises(RuntimeError, match="ACTIVE_ROW_INVALID"):
        require_active_price_alerts([{"id": "partial-alert"}])


def test_registry_rejects_inert_publication_and_removes_only_the_exact_scope() -> None:
    registry = AlertRuntimeRegistry()
    registry.hydrate(
        [
            _alert("five-minute"),
            _alert("fifteen-minute", timeframe="15m"),
        ]
    )

    with pytest.raises(ValueError, match="PRICE_ALERT_RUNTIME_INERT_ROW"):
        registry.publish_committed(_alert("inert", armed=False, fired=False))

    registry.remove_scope_committed("instrument-1", "route-1", "5m")

    rows, _revision = registry.snapshot()
    assert [row["id"] for row in rows] == ["fifteen-minute"]


def test_dynamic_alert_monitor_uses_non_blocking_runtime_settings_projection() -> None:
    source = inspect.getsource(process_server_alert_monitor_tick)

    assert "deps.server_alert_indicator_params()" in source
    assert "to_thread(deps.server_alert_indicator_params)" not in source


def test_dynamic_alert_runtime_uses_shared_generation_fenced_bar_projection() -> None:
    source = inspect.getsource(server_alert_runtime._server_alert_recent_bars)

    assert "recent_confirmed_bar_context" in source
    assert "read_recent_provider_bars(" not in source
    assert "bar_cache" not in inspect.getsource(server_alert_runtime.server_dynamic_alert_state)


def test_dynamic_alert_runtime_coalesces_projection_by_canonical_bar_generation(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("COALESCED-EMA")
    route = route_instrument(instrument)
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": "ema233_touch",
        "level_source": {"type": "ema233_touch", "dynamic": True},
    }
    calls = {"projection": 0, "state": 0}
    projection = object()
    context = RecentConfirmedBarContext(
        generation=17,
        requested_limit=260,
        bars=(
            Bar(
                "COALESCED-EMA",
                datetime(2026, 8, 16, tzinfo=UTC),
                99.0,
                101.0,
                98.0,
                100.0,
                10.0,
                "5m",
                "ibkr",
            ),
        ),
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "_server_alert_recent_bars",
        lambda *_args, **_kwargs: context,
    )

    def build_projection(*_args, **_kwargs):
        calls["projection"] += 1
        return projection

    def build_state(actual_projection, *, current_price, **_kwargs):
        assert actual_projection is projection
        calls["state"] += 1
        return {
            "level": 100.0,
            "touching": current_price == 100.0,
            "tolerance": 0.5,
            "ema_length": 233,
            "atr_length": 14,
        }

    monkeypatch.setattr(server_alert_runtime, "ema_touch_guide_projection", build_projection)
    monkeypatch.setattr(
        server_alert_runtime,
        "ema_touch_guide_state_from_projection",
        build_state,
    )
    with server_alert_runtime._ALERT_PROJECTION_LOCK:
        server_alert_runtime._ALERT_PROJECTION_CACHE.clear()

    first = server_alert_runtime.server_dynamic_alert_state(
        object(),
        alert,
        99.0,
        instrument=instrument,
        indicator_params={"global_defaults": {}},
    )
    second = server_alert_runtime.server_dynamic_alert_state(
        object(),
        alert,
        100.0,
        instrument=instrument,
        indicator_params={"global_defaults": {}},
    )

    assert first is not None and first["touching"] is False
    assert second is not None and second["touching"] is True
    assert calls == {"projection": 1, "state": 2}


@pytest.mark.parametrize(
    ("kind", "expected_producer"),
    [("ema233_touch", "dynamic"), ("vsa_fuel", "vsa")],
)
def test_unavailable_bar_alert_never_falls_through_to_price_crossing(
    kind: str,
    expected_producer: str,
) -> None:
    instrument = ibkr_future_payload(f"NO-FALLBACK-{kind}")
    route = route_instrument(instrument)
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": kind,
        "level_source": {"type": kind, "dynamic": True},
    }
    calls = {"dynamic": 0, "vsa": 0, "cross": 0}

    def dynamic_state(*_args, **_kwargs):
        calls["dynamic"] += 1
        return None

    def vsa_state(*_args, **_kwargs):
        calls["vsa"] += 1
        return None

    def cross_hit(*_args, **_kwargs):
        calls["cross"] += 1
        raise AssertionError("bar-derived alert reached the generic price path")

    class Store:
        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            raise AssertionError("unavailable bar-derived state was persisted")

    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=Store(),
        dynamic_state=dynamic_state,
        vsa_state=vsa_state,
        cross_hit=cross_hit,
        crossing_baselines={
            (alert["id"], route.instrument_id, route.fingerprint): AlertCrossingBaseline(
                timeframe=alert["timeframe"],
                rearmed_at=alert["rearmedAt"],
                price=99.0,
            )
        },
    )

    asyncio.run(process_server_alert_monitor_tick(deps))

    assert calls == {
        "dynamic": int(expected_producer == "dynamic"),
        "vsa": int(expected_producer == "vsa"),
        "cross": 0,
    }


def test_bar_alert_holds_canonical_publication_guard_through_state_write() -> None:
    instrument = ibkr_future_payload("GUARDED-ALERT")
    route = route_instrument(instrument)
    generation = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": "ema233_touch",
        "level_source": {"type": "ema233_touch", "dynamic": True},
    }
    started = threading.Event()
    release = threading.Event()
    patches: list[dict[str, Any]] = []

    class Store:
        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            started.set()
            assert release.wait(timeout=5.0)
            patches.append(dict(_args[3]))
            return {**alert, **_args[3]}

    def dynamic_state(*_args, **_kwargs):
        return {
            "level": 100.0,
            "touching": False,
            "tolerance": 0.5,
            "ema_length": 233,
            "atr_length": 14,
            "bar_timeframe": "5m",
            "bar_canonical_generation": generation,
        }

    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=Store(),
        dynamic_state=dynamic_state,
        vsa_state=lambda *_args, **_kwargs: None,
        cross_hit=lambda *_args, **_kwargs: False,
    )

    async def scenario() -> None:
        task = asyncio.create_task(process_server_alert_monitor_tick(deps))
        assert await asyncio.to_thread(started.wait, 5.0)
        try:
            with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
                chart_events.begin_chart_bars_write(
                    generation,
                    "5m",
                    route.fingerprint,
                    instrument_id=route.instrument_id,
                )
            assert active.value.publication_in_progress is True
        finally:
            release.set()
        await task

    asyncio.run(scenario())

    assert len(patches) == 1
    guard = chart_events.begin_chart_bars_write(
        generation,
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    chart_events.end_chart_bars_write(guard)


def test_bar_alert_discards_state_from_a_stale_canonical_generation() -> None:
    instrument = ibkr_future_payload("STALE-ALERT")
    route = route_instrument(instrument)
    generation = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": "ema233_touch",
        "level_source": {"type": "ema233_touch", "dynamic": True},
    }
    chart_events.publish_chart_bars_updated(
        route.provider,
        "5m",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        reason="test_commit",
    )

    class Store:
        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            raise AssertionError("stale alert state was persisted")

    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=Store(),
        dynamic_state=lambda *_args, **_kwargs: {
            "level": 100.0,
            "touching": False,
            "tolerance": 0.5,
            "ema_length": 233,
            "atr_length": 14,
            "bar_timeframe": "5m",
            "bar_canonical_generation": generation,
        },
        vsa_state=lambda *_args, **_kwargs: None,
        cross_hit=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale bar alert reached the generic price path")
        ),
    )

    asyncio.run(process_server_alert_monitor_tick(deps))


def test_registry_rejects_definition_corruption_before_publication() -> None:
    registry = AlertRuntimeRegistry()

    with pytest.raises(ValueError, match="kind"):
        registry.hydrate([{**_alert(), "kind": "legacy-price"}])


def test_registry_hydrates_retained_rows_and_exact_replay_does_not_advance_revision() -> None:
    registry = AlertRuntimeRegistry()
    alert = _alert()

    assert registry.hydrate([alert]) == 1
    rows, revision = registry.snapshot()
    assert rows == [alert]
    assert revision == 1

    assert registry.publish_committed(alert) == 1
    assert registry.remove_committed("alert-1", "instrument-1", "route-1") == 2
    assert registry.snapshot() == ([], 2)


def test_registry_scope_contains_armed_and_fired_rows_but_not_other_timeframes() -> None:
    registry = AlertRuntimeRegistry()
    registry.hydrate(
        [
            _alert("armed"),
            _alert("fired", armed=False, fired=True, price=101.0),
            _alert("other", timeframe="15m"),
        ]
    )

    rows, revision = registry.scope_snapshot(
        route_pairs=[("instrument-1", "route-1")],
        timeframe="5m",
    )

    assert revision == 1
    assert [row["id"] for row in rows] == ["armed", "fired"]
    assert {row["id"] for row in registry.active_snapshot()} == {"armed", "other"}


def test_registry_unchanged_scope_never_materializes_alerts(monkeypatch) -> None:
    registry = AlertRuntimeRegistry()
    registry.hydrate([_alert()])
    scope = {"route_pairs": [("instrument-1", "route-1")], "timeframe": "5m"}
    rows, revision = registry.scope_snapshot(**scope)
    assert rows == [_alert()]

    def unexpected_copy(_row):
        raise AssertionError("unchanged registry must not materialize alerts")

    with monkeypatch.context() as context:
        context.setattr(registry, "_wire", unexpected_copy)
        for _ in range(1000):
            assert registry.scope_snapshot(**scope, after_revision=revision) == (None, revision)
    registry.remove_committed("alert-1", "instrument-1", "route-1")
    assert registry.scope_snapshot(**scope, after_revision=revision) == ([], revision + 1)


def test_registry_changed_scope_converts_only_matching_alerts(monkeypatch) -> None:
    registry = AlertRuntimeRegistry()
    registry.hydrate([_alert(), _alert("other", timeframe="15m")])
    copied = []
    original = registry._wire

    def wire(row):
        copied.append(row["id"])
        return original(row)

    monkeypatch.setattr(registry, "_wire", wire)
    scope = {"route_pairs": [("instrument-1", "route-1")], "timeframe": "5m"}
    rows, revision = registry.scope_snapshot(**scope)
    assert copied == ["alert-1"]
    rows[0]["level_source"]["type"] = "not-canonical"
    assert registry.scope_snapshot(**scope)[0] == [_alert()]
    registry.publish_committed(_alert(price=101.0))
    rows, changed = registry.scope_snapshot(**scope, after_revision=revision)
    assert changed == revision + 1
    assert rows[0]["price"] == 101.0
    # The same revision never hides a newly selected scope when the caller resets its cursor.
    assert registry.scope_snapshot(route_pairs=[("other", "route-1")], timeframe="5m") == (
        [],
        changed,
    )


def test_registry_rejects_duplicate_definition_during_hydration_and_publication() -> None:
    registry = AlertRuntimeRegistry()

    with pytest.raises(ValueError, match="PRICE_ALERT_SEMANTIC_DUPLICATE"):
        registry.hydrate([_alert("first"), _alert("duplicate")])

    registry.hydrate([_alert("first")])
    with pytest.raises(ValueError, match="PRICE_ALERT_SEMANTIC_DUPLICATE"):
        registry.publish_committed(_alert("duplicate"))

    rows, revision = registry.snapshot()
    assert [row["id"] for row in rows] == ["first"]
    assert revision == 1


def test_registry_quote_events_are_ordered_bounded_and_only_for_active_routes() -> None:
    registry = AlertRuntimeRegistry(max_quote_events=2)
    registry.hydrate([_alert()])

    registry.publish_quote_changes([(("unrelated", "route-x"), {"price": 1.0})])
    assert registry.read_quote_events(0) == ([], 0, False)

    for price in (99.0, 100.0, 101.0):
        registry.publish_quote_changes([(("instrument-1", "route-1"), {"price": price})])

    events, ceiling, overflowed = registry.read_quote_events(0)
    assert overflowed is True
    assert ceiling == 3
    assert [event.sequence for event in events] == [2, 3]
    assert [event.wire_quote()["price"] for event in events] == [100.0, 101.0]


def test_registry_revision_wait_wakes_on_committed_publication() -> None:
    registry = AlertRuntimeRegistry()
    registry.hydrate([_alert()])

    async def scenario() -> None:
        waiter = asyncio.create_task(registry.wait_for_revision(1))
        await asyncio.sleep(0)
        registry.publish_committed({**_alert(), "lastSeenPrice": 100.5})
        await asyncio.wait_for(waiter, timeout=0.1)

    asyncio.run(scenario())


def test_startup_hydration_replays_commits_that_arrive_after_db_snapshot() -> None:
    registry = AlertRuntimeRegistry()
    token = registry.begin_hydration()
    committed = {**_alert(), "lastSeenPrice": 101.0}
    registry.publish_committed(committed)

    registry.hydrate([_alert()], token=token)

    rows, _revision = registry.snapshot()
    assert rows == [committed]


def test_fixed_price_alert_consumes_each_quote_event_and_persists_only_the_fire(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "ES",
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
    }

    class Store:
        def __init__(self) -> None:
            self.patches: list[dict] = []

        def update_price_alert_payload_exact(self, *_args, **kwargs):
            patch = dict(_args[3])
            self.patches.append(patch)
            return {**alert, **patch}

    store = Store()
    crossing_baselines: dict[AlertCrossingScope, AlertCrossingBaseline] = {}
    deps = ServerAlertMonitorTickDeps(
        store_factory=lambda: store,
        quote_route_snapshot=lambda snapshot=quote_route_snapshot([instrument]): snapshot,
        server_pending_price_alerts_snapshot=lambda _store: [],
        server_active_price_alerts_snapshot=lambda _store: [alert],
        active_paper_orders=lambda rows: rows,
        set_server_alert_wanted=lambda _ids: None,
        quote_cache_for_instruments=lambda *_args, **_kwargs: ({}, "", None),
        logger=logging.getLogger(__name__),
        paper_order_execution_transition=lambda *_args, **_kwargs: {},
        screen_execution_price_snapshot=lambda *_args, **_kwargs: None,
        queue_paper_trade_telegram=lambda *_args, **_kwargs: None,
        quote_for_route=lambda quotes, fingerprint: quotes[fingerprint],
        server_alert_price_snapshot=lambda quote: {
            "price": quote["price"],
            "high": quote["price"],
            "low": quote["price"],
            "ts": quote["ts"],
            "source": "quote",
        },
        server_vsa_fuel_alert_state=lambda *_args, **_kwargs: None,
        send_server_telegram_alert=lambda _payload: {"ok": True},
        alert_monitor_payload=alert_monitor_payload,
        server_dynamic_alert_state=lambda *_args, **_kwargs: None,
        server_alert_indicator_params=lambda: {},
        alert_freeze_active=lambda *_args, **_kwargs: False,
        crossing_baselines=crossing_baselines,
        server_ema_touch_direction=lambda *_args, **_kwargs: "touch",
        ema_touch_rearm_ms=lambda _alert: 60_000,
        alert_cross_hit=lambda previous, current, target, _direction: previous < target <= current,
        server_alert_freeze_ms=lambda _timeframe: 60_000,
        paper_order_price_snapshot=lambda _order: None,
        paper_journal_active_snapshot=_empty_paper_snapshot,
        set_paper_option_quote_demand=lambda _entities: 0,
        server_active_price_alerts_for_route=lambda _instrument_id, _fingerprint: [alert],
    )
    timestamp = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    def repeated_route_work(*_args, **_kwargs):
        raise AssertionError("ordered quote evaluation must reuse the immutable exact route")

    import importlib

    monkeypatch.setattr(
        importlib.import_module("aef_terminal.ui.runtime.quote_stream"),
        "domain_wire_value",
        repeated_route_work,
    )
    monkeypatch.setattr(
        "aef_terminal.ui.services.server_alert_monitor.route_instrument", repeated_route_work
    )

    async def scenario() -> None:
        await process_server_alert_quote_event(
            deps,
            AlertQuoteEvent(
                1, route.instrument_id, route.fingerprint, {"price": 99.0, "ts": timestamp}
            ),
        )
        await process_server_alert_quote_event(
            deps,
            AlertQuoteEvent(
                2, route.instrument_id, route.fingerprint, {"price": 101.0, "ts": timestamp}
            ),
        )

    asyncio.run(scenario())

    baseline = crossing_baselines[("alert-1", route.instrument_id, route.fingerprint)]
    assert baseline == AlertCrossingBaseline(
        timeframe="5m",
        rearmed_at=1,
        price=101.0,
    )
    assert len(store.patches) == 1
    assert store.patches[0]["armed"] is False
    assert store.patches[0]["fired"] is True
    assert store.patches[0]["telegramDeliveryStatus"] == "pending"


def test_fixed_price_alert_does_not_consume_crossing_when_delivery_cas_rejects() -> None:
    instrument = ibkr_future_payload("NQ")
    route = route_instrument(instrument)
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
    }

    class Store:
        def __init__(self) -> None:
            self.patches: list[dict] = []

        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            self.patches.append(dict(_args[3]))
            return None

    store = Store()
    baseline_key = (alert["id"], route.instrument_id, route.fingerprint)
    crossing_baselines = {
        baseline_key: AlertCrossingBaseline(
            timeframe=alert["timeframe"],
            rearmed_at=alert["rearmedAt"],
            price=99.0,
        )
    }
    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=store,
        dynamic_state=lambda *_args, **_kwargs: None,
        vsa_state=lambda *_args, **_kwargs: None,
        cross_hit=lambda previous, current, target, _direction: previous < target <= current,
        crossing_baselines=crossing_baselines,
    )

    asyncio.run(
        process_server_alert_quote_event(
            deps,
            AlertQuoteEvent(
                1,
                route.instrument_id,
                route.fingerprint,
                {"price": 101.0, "ts": datetime(2026, 8, 15, 10, 0, tzinfo=UTC)},
            ),
        )
    )

    assert len(store.patches) == 1
    assert store.patches[0]["telegramDeliveryStatus"] == "pending"
    assert baseline_key not in crossing_baselines


def test_fixed_price_alert_rearm_discards_the_previous_generation_baseline() -> None:
    instrument = ibkr_future_payload("REARMED-CROSS")
    route = route_instrument(instrument)
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
    }

    class Store:
        def __init__(self) -> None:
            self.patches: list[dict[str, Any]] = []

        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            self.patches.append(dict(_args[3]))
            return {**alert, **_args[3]}

    store = Store()
    crossing_baselines: dict[AlertCrossingScope, AlertCrossingBaseline] = {}
    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=store,
        dynamic_state=lambda *_args, **_kwargs: None,
        vsa_state=lambda *_args, **_kwargs: None,
        cross_hit=lambda previous, current, target, _direction: previous < target <= current,
        crossing_baselines=crossing_baselines,
    )
    timestamp = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)

    async def scenario() -> None:
        await process_server_alert_quote_event(
            deps,
            AlertQuoteEvent(
                1, route.instrument_id, route.fingerprint, {"price": 99.0, "ts": timestamp}
            ),
        )
        alert["rearmedAt"] = 2
        await process_server_alert_quote_event(
            deps,
            AlertQuoteEvent(
                2, route.instrument_id, route.fingerprint, {"price": 101.0, "ts": timestamp}
            ),
        )

    asyncio.run(scenario())

    assert store.patches == []
    assert crossing_baselines[
        (alert["id"], route.instrument_id, route.fingerprint)
    ] == AlertCrossingBaseline(timeframe="5m", rearmed_at=2, price=101.0)


def test_ema_touch_edge_and_delivery_intent_commit_in_one_generation_fenced_patch() -> None:
    instrument = ibkr_future_payload("YM")
    route = route_instrument(instrument)
    generation = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": "ema233_touch",
        "level_source": {"type": "ema233_touch", "dynamic": True},
        "wasTouching": False,
        "touchDirection": "from_below",
    }

    class Store:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        def update_price_alert_payload_exact(self, *_args, **kwargs):
            patch = dict(_args[3])
            self.calls.append((patch, dict(kwargs)))
            return {**alert, **patch}

    store = Store()
    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=store,
        dynamic_state=lambda *_args, **_kwargs: {
            "level": 100.0,
            "touching": True,
            "tolerance": 0.5,
            "ema_length": 233,
            "atr_length": 14,
            "bar_timeframe": "5m",
            "bar_canonical_generation": generation,
        },
        vsa_state=lambda *_args, **_kwargs: None,
        cross_hit=lambda *_args, **_kwargs: False,
    )
    deps = replace(
        deps,
        server_ema_touch_direction=server_alert_runtime.server_ema_touch_direction,
    )

    asyncio.run(process_server_alert_monitor_tick(deps))

    assert len(store.calls) == 1
    patch, kwargs = store.calls[0]
    assert patch["wasTouching"] is True
    assert patch["touchDirection"] == "from_below"
    assert patch["fired"] is True
    assert patch["telegramDeliveryStatus"] == "pending"
    assert kwargs["expected_rearmed_at"] == alert["rearmedAt"]
    assert kwargs["check_generation"] is True
    assert kwargs["require_active"] is True


def test_vsa_event_consumption_and_delivery_intent_commit_in_one_patch() -> None:
    instrument = ibkr_future_payload("RTY")
    route = route_instrument(instrument)
    generation = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    alert = {
        **_alert(),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": "vsa_fuel",
        "level_source": {"type": "vsa_fuel", "dynamic": True},
    }

    class Store:
        def __init__(self) -> None:
            self.patches: list[dict] = []

        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            patch = dict(_args[3])
            self.patches.append(patch)
            return {**alert, **patch}

    store = Store()
    event_ts = "2026-08-15T09:55:00+00:00"
    deps = _bar_alert_deps(
        alert=alert,
        instrument=instrument,
        store=store,
        dynamic_state=lambda *_args, **_kwargs: None,
        vsa_state=lambda *_args, **_kwargs: {
            "level": 101.0,
            "touching": True,
            "event_ts": event_ts,
            "bar_timeframe": "5m",
            "bar_canonical_generation": generation,
        },
        cross_hit=lambda *_args, **_kwargs: False,
    )

    asyncio.run(process_server_alert_monitor_tick(deps))

    assert len(store.patches) == 1
    assert store.patches[0]["lastEventTs"] == event_ts
    assert store.patches[0]["lastFiredEventTs"] == event_ts
    assert store.patches[0]["telegramDeliveryStatus"] == "pending"
