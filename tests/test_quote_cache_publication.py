from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aef_terminal.data.ibkr import quotes as ibkr_quotes
from aef_terminal.data.ibkr import runtime as ibkr_runtime
from aef_terminal.data.provider_contract import QuoteSnapshotRead
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui import quote_helpers
from aef_terminal.ui.services import quote_orchestrator as orchestrator
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload

quote_runtime = importlib.import_module("aef_terminal.ui.runtime.quote_stream")


def make_runtime(instruments):
    runtime = quote_runtime.QuoteStreamRuntime()
    runtime.refresh_routes_from_store(
        SimpleNamespace(read_watchlist_snapshot=lambda: (instruments, 1))
    )
    return runtime


def live_quote(at, price=100.0):
    return {
        "price": price,
        "last": price,
        "price_source": "last",
        "time_basis": "provider_event",
        "provider_ts": at.isoformat(),
        "received_at": at.isoformat(),
        "market_data_entitlement": "live",
        "is_delayed": False,
    }


def install_clock(monkeypatch, initial):
    clock = [initial]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(quote_runtime, "datetime", Clock)
    monkeypatch.setattr(quote_helpers, "datetime", Clock)
    return clock


def test_provider_unchanged_read_does_not_copy_or_requalify_and_reset_is_visible(monkeypatch):
    owner = ibkr_runtime._IbkrRuntimeState(64)
    monkeypatch.setattr(ibkr_quotes, "_IBKR_RUNTIME", owner)
    route = route_instrument(ibkr_stock_payload("SPY"))
    identity = (route.instrument_id, route.fingerprint)
    owner.publish_quote_snapshots({identity: {"price": 100.0, "nested": [1]}})
    first = ibkr_quotes.cached_quotes([route])
    first.quotes[route.fingerprint]["nested"].append(2)
    isolated = ibkr_quotes.cached_quotes([route])
    assert isolated.quotes[route.fingerprint]["nested"] == [1]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unchanged provider snapshot must not copy or qualify")

    with monkeypatch.context() as m:
        m.setattr(ibkr_runtime, "deepcopy", forbidden)
        m.setattr(ibkr_quotes, "_require_ibkr_instrument", forbidden)
        for _ in range(100):
            read = ibkr_quotes.cached_quotes([route], after_sequence=first.sequence)
            assert read == QuoteSnapshotRead(first.sequence, {}, changed=False)
    owner.clear_quote_snapshots()
    reset = ibkr_quotes.cached_quotes([route], after_sequence=first.sequence)
    assert reset.changed and reset.sequence > first.sequence
    assert reset.quotes[route.fingerprint]["price"] is None
    with pytest.raises(ValueError, match="QUALIFIED_ROUTES"):
        ibkr_quotes.cached_quotes([route.instrument])


def test_unchanged_cache_ticks_skip_envelopes_but_publish_stale_and_recovery(monkeypatch):
    observed = datetime(2026, 8, 30, 12, tzinfo=UTC)
    clock = install_clock(monkeypatch, observed)
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    identity = (route.instrument_id, route.fingerprint)
    runtime = make_runtime([instrument])
    runtime.store_cache({identity: live_quote(observed)})
    revision = runtime.cache_for_instruments([instrument])[2]
    original = quote_runtime.quote_envelope
    calls = []

    def envelope(row, **kwargs):
        calls.append(row)
        return original(row, **kwargs)

    monkeypatch.setattr(quote_runtime, "quote_envelope", envelope)
    for step in range(1, 501):
        clock[0] = observed + timedelta(milliseconds=step * 10)
        runtime.store_cache(None)
    assert calls == []
    assert runtime.cache_for_instruments([instrument], after_revision=revision)[0] is None
    clock[0] += timedelta(microseconds=1)
    runtime.store_cache(None)
    assert len(calls) == 1
    stale, _, stale_revision = runtime.cache_for_instruments([instrument], after_revision=revision)
    assert stale[route.fingerprint]["status"] == "stale"
    assert stale_revision.generation == revision.generation + 1
    assert stale[route.fingerprint]["provider_ts"] == observed.isoformat()

    # A later provider receipt, even with the same numeric price, is not a replay.
    clock[0] += timedelta(seconds=1)
    runtime.store_cache({identity: live_quote(clock[0])})
    recovered, _, recovered_revision = runtime.cache_for_instruments([instrument])
    assert recovered[route.fingerprint]["status"] == "live"
    assert recovered_revision.generation == stale_revision.generation + 1
    assert recovered[route.fingerprint]["provider_ts"] == clock[0].isoformat()


def test_partial_row_updates_do_not_suppress_other_rows_freshness_or_failure(monkeypatch):
    observed = datetime(2026, 8, 30, 12, tzinfo=UTC)
    clock = install_clock(monkeypatch, observed)
    instruments = [ibkr_stock_payload("SPY", con_id=100), ibkr_stock_payload("QQQ", con_id=101)]
    routes = list(map(route_instrument, instruments))
    identities = [(r.instrument_id, r.fingerprint) for r in routes]
    runtime = make_runtime(instruments)
    quote = live_quote(observed)
    quote.update(
        bid=99.0, ask=101.0, bid_ask_received_at=(observed + timedelta(seconds=2)).isoformat()
    )
    runtime.store_cache({identities[0]: quote, identities[1]: live_quote(observed)})
    clock[0] += timedelta(seconds=6)
    runtime.store_cache({identities[1]: live_quote(clock[0], 105.0)})
    rows, _, revision = runtime.cache_for_instruments(instruments)
    assert rows[routes[0].fingerprint]["last_status"] == "stale"
    assert rows[routes[0].fingerprint]["bid_ask_status"] == "live"
    assert rows[routes[1].fingerprint]["status"] == "live"
    succeeded_at = runtime._cache_updated_at
    clock[0] += timedelta(seconds=2)
    runtime.store_cache({}, "provider disconnected")
    rows, warning, next_revision = runtime.cache_for_instruments(instruments)
    assert rows[routes[0].fingerprint]["bid_ask_status"] == "stale"
    assert warning == "provider disconnected"
    assert next_revision.generation > revision.generation
    assert runtime._cache_updated_at == succeeded_at


def test_freshness_future_boundary_clock_regression_and_retirement(monkeypatch):
    observed = datetime(2026, 8, 30, 12, tzinfo=UTC)
    clock = install_clock(monkeypatch, observed - timedelta(seconds=10))
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    identity = (route.instrument_id, route.fingerprint)
    runtime = make_runtime([instrument])
    runtime.store_cache({identity: live_quote(observed)})
    assert runtime._cache[identity]["status"] == "stale"
    clock[0] = observed - timedelta(seconds=5)
    runtime.store_cache(None)
    assert runtime._cache[identity]["status"] == "live"
    clock[0] = observed + timedelta(seconds=6)
    runtime.store_cache(None)
    assert runtime._cache[identity]["status"] == "stale"
    clock[0] = observed
    runtime.store_cache(None)
    assert runtime._cache[identity]["status"] == "live"
    runtime.refresh_routes_from_store(SimpleNamespace(read_watchlist_snapshot=lambda: ([], 2)))
    assert not runtime._cache_freshness_deadlines
    assert runtime._cache_next_freshness_at == float("inf")
    runtime.store_cache({identity: live_quote(clock[0])})
    assert not runtime._cache


def test_orchestrator_reuses_twelve_routes_and_publishes_next_changed_price(monkeypatch):
    async def run():
        instruments = [ibkr_stock_payload(f"S{index}", con_id=100 + index) for index in range(12)]
        runtime = make_runtime(instruments)
        owner = ibkr_runtime._IbkrRuntimeState(64)
        monkeypatch.setattr(ibkr_quotes, "_IBKR_RUNTIME", owner)
        routes = list(map(route_instrument, instruments))
        observed = datetime.now(tz=UTC)
        owner.publish_quote_snapshots(
            {(route.instrument_id, route.fingerprint): live_quote(observed) for route in routes}
        )
        routed = []
        original_route = orchestrator.route_instrument

        def qualify(item):
            routed.append(item)
            return original_route(item)

        monkeypatch.setattr(orchestrator, "route_instrument", qualify)
        publications = []
        reads = []
        state = {"sync": ""}
        done = asyncio.Event()
        changed_identity = (routes[0].instrument_id, routes[0].fingerprint)

        def read(items, *, after_sequence=None):
            result = ibkr_quotes.cached_quotes(items, after_sequence=after_sequence)
            reads.append(result.changed)
            return result

        def publish(rows, warning):
            runtime.store_cache(rows, warning)
            publications.append(rows)
            if len(publications) == 50:
                owner.publish_quote_snapshots({changed_identity: live_quote(observed, 101.0)})
            if len(publications) == 60:
                done.set()

        task = asyncio.create_task(
            orchestrator.run_quote_orchestrator_loop(
                startup_delay_seconds=0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5,
                server_sleeping=lambda: False,
                quote_route_snapshot=runtime.route_snapshot,
                get_last_sync_key=lambda: state["sync"],
                set_last_sync_key=lambda value: state.update(sync=value),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=lambda *_args, **_kwargs: asyncio.sleep(0),
                cached_quotes=read,
                store_quote_cache=publish,
                persist_quote_snapshots=lambda *_args: asyncio.sleep(0),
                on_cleanup_error=lambda *_args: None,
                on_loop_error=lambda exc: pytest.fail(str(exc)),
            )
        )
        try:
            await asyncio.wait_for(done.wait(), 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(routed) == 12
        assert sum(reads) == 2
        assert len(publications[0]) == 12
        assert all(rows is None for rows in publications[1:50])
        assert set(publications[50]) == {changed_identity}
        assert runtime._cache[changed_identity]["price"] == 101.0

    asyncio.run(run())


@pytest.mark.parametrize(
    "first, second",
    [
        (ibkr_stock_payload("SPY", con_id=100), ibkr_stock_payload("QQQ", con_id=101)),
        (
            ibkr_future_payload("ES", local_symbol="ESU6", con_id=11004968),
            ibkr_future_payload("ES", local_symbol="ESZ6", con_id=12000001),
        ),
    ],
)
def test_orchestrator_resets_cursor_on_route_change_and_publishes_warnings(
    monkeypatch, first, second
):
    async def run():
        runtime = make_runtime([first])
        owner = ibkr_runtime._IbkrRuntimeState(64)
        monkeypatch.setattr(ibkr_quotes, "_IBKR_RUNTIME", owner)
        routes = list(map(route_instrument, [first, second]))
        for route in routes:
            owner.publish_quote_snapshots(
                {(route.instrument_id, route.fingerprint): live_quote(datetime.now(tz=UTC))}
            )
        reads, publications = [], []
        state = {"sync": "", "generation": "market_data_type:1"}
        done = asyncio.Event()

        def read(items, *, after_sequence=None):
            reads.append(after_sequence)
            return ibkr_quotes.cached_quotes(items, after_sequence=after_sequence)

        def publish(rows, warning):
            runtime.store_cache(rows, warning)
            publications.append((rows, warning))
            if len(publications) == 3:
                runtime.refresh_routes_from_store(
                    SimpleNamespace(read_watchlist_snapshot=lambda: ([second], 2))
                )
            if len(publications) == 5:
                state["generation"] = "market_data_type:3"
            if "subscription sync failed" in warning:
                done.set()

        async def sync(*_args, **_kwargs):
            if state["generation"] == "market_data_type:3":
                raise RuntimeError("subscription error")

        task = asyncio.create_task(
            orchestrator.run_quote_orchestrator_loop(
                startup_delay_seconds=0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5,
                server_sleeping=lambda: False,
                quote_route_snapshot=runtime.route_snapshot,
                get_last_sync_key=lambda: state["sync"],
                set_last_sync_key=lambda value: state.update(sync=value),
                quote_subscription_generation=lambda: state["generation"],
                sync_ibkr_subscriptions=sync,
                cached_quotes=read,
                store_quote_cache=publish,
                persist_quote_snapshots=lambda *_args: asyncio.sleep(0),
                on_cleanup_error=lambda *_args: None,
                on_loop_error=lambda exc: pytest.fail(str(exc)),
            )
        )
        try:
            await asyncio.wait_for(done.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert reads[0] is None and reads[3] is None
        assert publications[3][0] is not None
        assert set(runtime._cache) == {(routes[1].instrument_id, routes[1].fingerprint)}
        assert publications[-1][0] is None
        assert runtime.cache_stats()["warning"].endswith("subscription error")

    asyncio.run(run())


def test_subscription_generation_reads_canonical_settings_without_rebuilding_config(monkeypatch):
    def forbidden():
        raise AssertionError("quote hot path must not rebuild the full application config")

    monkeypatch.setattr(ibkr_quotes, "AppConfig", forbidden)
    settings = SimpleNamespace(market_data_type=1)
    monkeypatch.setattr(ibkr_quotes, "ibkr_runtime_settings_snapshot", lambda: settings)
    assert ibkr_quotes.quote_subscription_generation() == "market_data_type:1"
    settings.market_data_type = 3
    assert ibkr_quotes.quote_subscription_generation() == "market_data_type:3"
