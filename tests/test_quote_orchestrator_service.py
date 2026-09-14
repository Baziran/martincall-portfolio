from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from datetime import UTC, datetime

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_contract import QuoteSnapshotRead
from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.ui import app as ui_app
from aef_terminal.ui import app_runtime_wiring
from aef_terminal.ui import background_loops_runtime
from aef_terminal.ui import bootstrap as ui_bootstrap
from aef_terminal.ui.routers import streams as streams_router
from aef_terminal.ui.services import quote_orchestrator as quote_orchestrator_service
from aef_terminal.ui.services.quote_orchestrator import (
    QuoteProviderPoller,
    run_quote_orchestrator_loop,
)
from tests.provider_payloads import coinbase_btc_payload, ibkr_stock_payload, quote_route_snapshot
from tests.source_contracts import python_source_contains


def quote_route_reader(instruments):
    snapshot = quote_route_snapshot(instruments)
    return lambda: snapshot


def test_quote_orchestrator_syncs_watchlist_without_provisional_bar_ingest() -> None:
    app_source = inspect.getsource(ui_app)
    loop_source = inspect.getsource(background_loops_runtime.quote_orchestrator_loop)
    runtime_source = inspect.getsource(background_loops_runtime)
    bootstrap_source = inspect.getsource(ui_bootstrap.start_server_alert_monitor)
    runtime_wiring_source = inspect.getsource(app_runtime_wiring)
    orchestrator_source = Path("src/aef_terminal/ui/services/quote_orchestrator.py").read_text()
    quote_stream_source = Path("src/aef_terminal/ui/services/quote_stream_ws.py").read_text()
    stream_source = inspect.getsource(streams_router)

    assert "_persist_quote_bars_async" not in app_source
    assert "QuoteBarIngestor" not in app_source
    assert "quote_bar_ingest_symbols" not in app_source
    assert "async def _quote_orchestrator_loop" not in app_source
    assert "run_quote_orchestrator_runtime" in loop_source
    assert "QuoteProviderPoller(" in runtime_source
    assert "live_quote_polling_providers()" in runtime_source
    assert "create_quote_polling_runtime()" in runtime_source
    assert "fetch_quotes=runtime.fetch_quotes" in runtime_source
    assert "close=runtime.aclose" in runtime_source
    assert "quote_route_snapshot=deps.quote_route_snapshot" in runtime_source
    assert (
        "quote_subscription_generation=ibkr_adapter.quote_subscription_generation" in runtime_source
    )
    assert "sync_ibkr_subscriptions=ibkr_adapter.async_sync_quote_subscriptions" in runtime_source
    assert "cached_quotes=ibkr_adapter.cached_quotes" in runtime_source
    assert "poll_seconds=QUOTE_STREAM_SECONDS" in runtime_source
    assert "snapshot_persist_seconds=QUOTE_SNAPSHOT_SECONDS" in runtime_source
    assert python_source_contains(
        bootstrap_source,
        'BACKGROUND_TASKS.ensure("quote_orchestrator", deps.quote_orchestrator_loop',
    )
    assert bootstrap_source.index(
        "await deps.start_market_analysis_process_runtime()"
    ) < bootstrap_source.index('BACKGROUND_TASKS.ensure(\n        "quote_orchestrator"')
    assert (
        "quote_orchestrator_loop=background_loops_runtime.quote_orchestrator_loop"
        in runtime_wiring_source
    )
    assert "subscription_sync_task = asyncio.create_task(" in orchestrator_source
    assert "sync_ibkr_subscriptions(" in orchestrator_source
    assert "cached_quotes(quote_routes, after_sequence=ibkr_after_sequence)" in orchestrator_source
    assert "max(poll_seconds, 1.0)" not in orchestrator_source
    assert 'on_cleanup_error("cache_read", "ibkr", exc)' in orchestrator_source
    assert 'on_cleanup_error("snapshot_persist", "storage", exc)' in orchestrator_source
    assert 'op=f"quote_orchestrator_{stage}"' in orchestrator_source
    assert 'op="quote_orchestrator_cleanup"' not in orchestrator_source
    assert "persist_quote_bars" not in orchestrator_source
    assert "quote_cache_for_instruments" in stream_source
    assert "after_revision=" in quote_stream_source
    assert "next_rows_refresh_at" not in quote_stream_source
    assert "sync_quote_subscriptions_async" not in stream_source
    assert "read_cached_quotes_async" not in stream_source


def test_quote_orchestrator_sync_key_includes_provider_subscription_generation() -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)

    live_key = quote_orchestrator_service._quote_item_sync_key(
        route_instrument(instrument),
        subscription_generation="market_data_type:1",
    )
    delayed_key = quote_orchestrator_service._quote_item_sync_key(
        route_instrument(instrument),
        subscription_generation="market_data_type:3",
    )

    assert live_key != delayed_key
    assert "market_data_type:1" in live_key
    assert "market_data_type:3" in delayed_key


def test_quote_orchestrator_syncs_and_reads_ibkr_cache_with_instrument_identity() -> None:
    async def run() -> None:
        cache_updates: list[tuple[dict[tuple[str, str], object], str]] = []
        cache_event = asyncio.Event()
        sync_calls: list[list[object]] = []
        read_calls: list[list[dict[str, object]]] = []
        state = {"last_sync_key": ""}
        spx = ibkr_stock_payload("SPX", con_id=416904, asset_class="index", sec_type="IND")

        async def sync_ibkr_subscriptions(items: list[object], timeout: float) -> None:
            assert timeout == 1.0
            sync_calls.append(list(items))

        def cached_quotes(
            routes,
            *,
            after_sequence=None,
        ) -> QuoteSnapshotRead:
            read_calls.append([route.instrument for route in routes])
            return QuoteSnapshotRead(1, {route_instrument(spx).fingerprint: {"price": 7489.6}})

        def store_quote_cache(live_map: dict[tuple[str, str], object], warning: str) -> None:
            cache_updates.append((dict(live_map or {}), warning))
            if state["last_sync_key"]:
                cache_event.set()

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: state["last_sync_key"],
                set_last_sync_key=lambda value: state.__setitem__("last_sync_key", value),
                quote_route_snapshot=quote_route_reader([spx]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=sync_ibkr_subscriptions,
                cached_quotes=cached_quotes,
                provider_pollers=(),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=lambda _live_map, _instruments: asyncio.sleep(0),
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        await asyncio.wait_for(cache_event.wait(), timeout=1.0)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert len(sync_calls) == 1 and len(sync_calls[0]) == 1
        selected_route = route_instrument(sync_calls[0][0])
        assert selected_route.instrument_id == qualified_instrument_id(spx)
        assert selected_route.fingerprint == route_instrument(spx).fingerprint
        assert read_calls
        assert all(call == sync_calls[0] for call in read_calls)
        assert cache_updates[0] == (
            {(qualified_instrument_id(spx), route_instrument(spx).fingerprint): {"price": 7489.6}},
            "",
        )
        assert "416904" in state["last_sync_key"]

    asyncio.run(run())


def test_quote_orchestrator_surfaces_read_failure_to_cache_warning() -> None:
    async def run() -> None:
        cache_updates: list[tuple[dict[tuple[str, str], object], str]] = []
        cleanup_errors: list[tuple[str, str, str]] = []
        cache_event = asyncio.Event()
        sync_calls: list[list[str]] = []
        state = {"last_sync_key": ""}

        async def sync_ibkr_subscriptions(instruments: list[dict], timeout: float) -> None:
            assert timeout == 1.0
            sync_calls.append([item["provider_symbol"] for item in instruments])

        def cached_quotes(
            _routes,
            *,
            after_sequence=None,
        ) -> dict[str, object]:
            raise RuntimeError("read-failed")

        def store_quote_cache(live_map: dict[tuple[str, str], object], warning: str) -> None:
            cache_updates.append((dict(live_map or {}), warning))
            if cleanup_errors and sync_calls:
                cache_event.set()

        async def persist_quote_snapshots(
            _live_map: dict[str, object],
            _instruments: list[dict[str, object]],
        ) -> None:
            return None

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: state["last_sync_key"],
                set_last_sync_key=lambda value: state.__setitem__("last_sync_key", value),
                quote_route_snapshot=quote_route_reader([ibkr_stock_payload("SPY")]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=sync_ibkr_subscriptions,
                cached_quotes=cached_quotes,
                provider_pollers=(),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=persist_quote_snapshots,
                on_cleanup_error=lambda stage, provider, exc: cleanup_errors.append(
                    (stage, provider, str(exc))
                ),
                on_loop_error=lambda _exc: None,
            )
        )
        await asyncio.wait_for(cache_event.wait(), timeout=1.0)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert sync_calls == [["SPY"]]
        assert cache_updates
        _live_map, warning = cache_updates[0]
        assert "IBKR quote snapshot read failed" in warning
        assert cleanup_errors
        assert cleanup_errors[0] == ("cache_read", "ibkr", "read-failed")

    asyncio.run(run())


def test_quote_orchestrator_preserves_cached_quotes_when_subscription_sync_fails() -> None:
    async def run() -> None:
        cache_updates: list[tuple[dict[tuple[str, str], object], str]] = []
        cache_event = asyncio.Event()
        cleanup_errors: list[tuple[str, str, str]] = []
        loop_errors: list[str] = []
        state = {"last_sync_key": ""}

        async def sync_ibkr_subscriptions(_instruments: list[dict], timeout: float) -> None:
            assert timeout == 1.0
            raise RuntimeError("ES contract resolution timed out")

        def cached_quotes(
            _routes,
            *,
            after_sequence=None,
        ) -> QuoteSnapshotRead:
            spy = ibkr_stock_payload("SPY")
            return QuoteSnapshotRead(1, {route_instrument(spy).fingerprint: {"price": 750.0}})

        def store_quote_cache(live_map: dict[tuple[str, str], object], warning: str) -> None:
            cache_updates.append((dict(live_map or {}), warning))
            if "subscription sync failed" in warning:
                cache_event.set()

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: state["last_sync_key"],
                set_last_sync_key=lambda value: state.__setitem__("last_sync_key", value),
                quote_route_snapshot=quote_route_reader([ibkr_stock_payload("SPY")]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=sync_ibkr_subscriptions,
                cached_quotes=cached_quotes,
                provider_pollers=(),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=lambda _live_map, _instruments: asyncio.sleep(0),
                on_cleanup_error=lambda stage, provider, exc: cleanup_errors.append(
                    (stage, provider, str(exc))
                ),
                on_loop_error=lambda exc: loop_errors.append(str(exc)),
            )
        )
        await asyncio.wait_for(cache_event.wait(), timeout=1.0)
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

        assert state["last_sync_key"] == ""
        assert cleanup_errors
        assert cleanup_errors[0] == (
            "subscription_sync",
            "ibkr",
            "ES contract resolution timed out",
        )
        assert loop_errors == []
        spy = ibkr_stock_payload("SPY")
        assert cache_updates[0][0] == {
            (qualified_instrument_id(spy), route_instrument(spy).fingerprint): {"price": 750.0}
        }
        assert "IBKR quote subscription sync failed" in cache_updates[-1][1]

    asyncio.run(run())


def test_quote_orchestrator_cleans_subscriptions_when_sleeping() -> None:
    async def run() -> None:
        sync_calls: list[list[str]] = []
        cache_updates: list[tuple[dict[tuple[str, str], object], str]] = []
        state = {"last_sync_key": "SPY"}
        cleanup_called = asyncio.Event()
        state_cleared = asyncio.Event()

        async def sync_ibkr_subscriptions(symbols: list[str], timeout: float) -> None:
            assert timeout == 1.0
            sync_calls.append(list(symbols))
            cleanup_called.set()

        def store_quote_cache(live_map: dict[tuple[str, str], object], warning: str) -> None:
            cache_updates.append((dict(live_map or {}), warning))

        def set_last_sync_key(value: str) -> None:
            state["last_sync_key"] = value
            if not value:
                state_cleared.set()

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: True,
                get_last_sync_key=lambda: state["last_sync_key"],
                set_last_sync_key=set_last_sync_key,
                quote_route_snapshot=quote_route_reader([ibkr_stock_payload("SPY")]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=sync_ibkr_subscriptions,
                cached_quotes=lambda _routes, **_kwargs: QuoteSnapshotRead(0, {}),
                provider_pollers=(),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=lambda _live_map, _instruments: asyncio.sleep(0),
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        await asyncio.wait_for(cleanup_called.wait(), timeout=1.0)
        await asyncio.wait_for(state_cleared.wait(), timeout=1.0)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert sync_calls == [[]]
        assert state["last_sync_key"] == ""
        assert cache_updates
        assert cache_updates[0][1] == "Server sleeping: live quote polling paused."

    asyncio.run(run())


def test_ibkr_quote_adapter_reads_owner_published_snapshot_without_async_lane(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY")
    seen: dict[str, object] = {}

    def fake_cached_quotes(routes, *, after_sequence=None):
        seen["routes"] = routes
        seen["after_sequence"] = after_sequence
        return QuoteSnapshotRead(1, {route_instrument(instrument).fingerprint: {"price": 750.0}})

    monkeypatch.setattr(
        "aef_terminal.data.ibkr.quotes.cached_quotes",
        fake_cached_quotes,
    )

    result = route_instrument(instrument).adapter.cached_quotes(
        [route_instrument(instrument)],
        after_sequence=0,
    )

    assert result == QuoteSnapshotRead(
        1, {route_instrument(instrument).fingerprint: {"price": 750.0}}
    )
    assert seen == {"routes": [route_instrument(instrument)], "after_sequence": 0}


def test_ibkr_gex_refresh_reuses_canonical_underlying_quote(monkeypatch) -> None:
    instrument = ibkr_stock_payload("SPY")
    adapter = route_instrument(instrument).adapter
    seen: dict[str, object] = {}
    observed_at = datetime.now(tz=UTC).isoformat()
    quote = {
        "price": 751.25,
        "price_source": "bid_ask_mid",
        "bid": 751.0,
        "ask": 751.5,
        "provider_ts": None,
        "received_at": observed_at,
        "time_basis": "client_receive",
        "status": "live",
        "is_stale": False,
        "is_delayed": False,
        "market_data_type": 1,
        "market_data_entitlement": "live",
    }

    def fake_cached_quotes(routes):
        seen["quote_instruments"] = [route.instrument for route in routes]
        return QuoteSnapshotRead(1, {route_instrument(instrument).fingerprint: quote})

    async def fake_gex_context(*, instrument, **kwargs):
        seen["instrument"] = instrument
        seen["gex_kwargs"] = kwargs
        return {"ok": True, "asset": instrument["provider_symbol"]}

    monkeypatch.setattr(adapter, "cached_quotes", fake_cached_quotes)
    monkeypatch.setattr(
        "aef_terminal.data.gex.context.async_gex_context",
        fake_gex_context,
    )

    result = asyncio.run(adapter.async_load_gex(instrument, enabled=True, refresh=True))

    assert result == {"ok": True, "asset": "SPY"}
    assert seen["quote_instruments"] == [instrument]
    assert seen["instrument"] == instrument
    assert seen["gex_kwargs"]["underlying_quote"] == quote
    assert "underlying_spot" not in seen["gex_kwargs"]


def test_ibkr_live_gex_reuses_canonical_underlying_quote(monkeypatch) -> None:
    instrument = ibkr_stock_payload("SPY")
    adapter = route_instrument(instrument).adapter
    seen: dict[str, object] = {}
    observed_at = datetime.now(tz=UTC).isoformat()
    quote = {
        "price": 751.5,
        "price_source": "bid_ask_mid",
        "bid": 751.25,
        "ask": 751.75,
        "provider_ts": None,
        "received_at": observed_at,
        "time_basis": "client_receive",
        "status": "live",
        "is_stale": False,
        "is_delayed": False,
        "market_data_type": 1,
        "market_data_entitlement": "live",
    }

    def fake_cached_quotes(routes):
        seen["quote_instruments"] = [route.instrument for route in routes]
        return QuoteSnapshotRead(1, {route_instrument(instrument).fingerprint: quote})

    async def fake_live_gex_context(*, instrument, **kwargs):
        seen["instrument"] = instrument
        seen["gex_kwargs"] = kwargs
        return {"ok": True, "asset": instrument["provider_symbol"]}

    monkeypatch.setattr(adapter, "cached_quotes", fake_cached_quotes)
    monkeypatch.setattr(
        "aef_terminal.data.gex.live.async_live_gex_context",
        fake_live_gex_context,
    )

    result = asyncio.run(adapter.async_load_live_gex(instrument, enabled=True))

    assert result == {"ok": True, "asset": "SPY"}
    assert seen["quote_instruments"] == [instrument]
    assert seen["instrument"] == instrument
    assert seen["gex_kwargs"]["underlying_quote"] == quote
    assert "underlying_spot" not in seen["gex_kwargs"]


def test_quote_orchestrator_publishes_non_ibkr_provider_quotes_without_ibkr_subscription() -> None:
    async def run() -> None:
        cache_updates: list[tuple[dict[tuple[str, str], object], str]] = []
        cache_event = asyncio.Event()
        sync_calls: list[list[str]] = []
        state = {"last_sync_key": ""}

        async def sync_ibkr_subscriptions(instruments: list[dict], timeout: float) -> None:
            sync_calls.append([route_instrument(item).instrument_id for item in instruments])

        def store_quote_cache(live_map: dict[tuple[str, str], object], warning: str) -> None:
            cache_updates.append((dict(live_map or {}), warning))
            if len(live_map or {}) == 1:
                cache_event.set()

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: state["last_sync_key"],
                set_last_sync_key=lambda value: state.__setitem__("last_sync_key", value),
                quote_route_snapshot=quote_route_reader([coinbase_btc_payload()]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=sync_ibkr_subscriptions,
                cached_quotes=lambda _routes, **_kwargs: QuoteSnapshotRead(0, {}),
                provider_pollers=(
                    QuoteProviderPoller(
                        provider="coinbase",
                        fetch_quotes=lambda instruments: asyncio.sleep(
                            0,
                            result=(
                                {
                                    route_instrument(item).fingerprint: {"price": 62125.0}
                                    for item in instruments
                                },
                                "",
                            ),
                        ),
                        close=lambda: asyncio.sleep(0),
                    ),
                ),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=lambda _live_map, _instruments: asyncio.sleep(0),
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        await asyncio.wait_for(cache_event.wait(), timeout=2.5)
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

        assert sync_calls == []
        assert cache_updates
        live_map, warning = next(update for update in cache_updates if len(update[0]) == 1)
        assert warning == ""
        btc = coinbase_btc_payload()
        assert (
            live_map[(qualified_instrument_id(btc), route_instrument(btc).fingerprint)]["price"]
            == 62125.0
        )

    asyncio.run(run())


def test_provider_poll_and_close_errors_keep_exact_provider_identity() -> None:
    async def run() -> None:
        instrument = coinbase_btc_payload()
        errors: list[tuple[str, str, str]] = []

        async def fail_fetch(_instruments: list[dict]) -> tuple[dict[str, object], str]:
            raise TimeoutError("provider deadline")

        async def fail_close() -> None:
            raise RuntimeError("transport close")

        poller = QuoteProviderPoller(
            provider="coinbase",
            fetch_quotes=fail_fetch,
            close=fail_close,
        )
        live_map, warnings = await quote_orchestrator_service._fetch_provider_quotes(
            [(poller, [instrument])],
            {},
            lambda stage, provider, exc: errors.append((stage, provider, str(exc))),
        )
        await quote_orchestrator_service._close_provider_pollers(
            [poller],
            lambda stage, provider, exc: errors.append((stage, provider, str(exc))),
        )

        assert live_map == {}
        assert warnings == ["COINBASE quote fetch failed: provider deadline"]
        assert errors == [
            ("provider_poll", "coinbase", "provider deadline"),
            ("provider_close", "coinbase", "transport close"),
        ]

    asyncio.run(run())


def test_quote_orchestrator_does_not_block_ibkr_cache_on_slow_provider_poll() -> None:
    async def run() -> None:
        provider_started = asyncio.Event()
        provider_release = asyncio.Event()
        cache_event = asyncio.Event()
        cache_updates: list[dict[tuple[str, str], object]] = []
        spy = ibkr_stock_payload("SPY")
        btc = coinbase_btc_payload()

        async def fetch_provider(
            instruments: list[dict],
        ) -> tuple[dict[str, object], str]:
            provider_started.set()
            await provider_release.wait()
            return (
                {route_instrument(item).fingerprint: {"price": 223000.0} for item in instruments},
                "",
            )

        def cached_quotes(
            _routes,
            *,
            after_sequence=None,
        ) -> QuoteSnapshotRead:
            return QuoteSnapshotRead(1, {route_instrument(spy).fingerprint: {"price": 750.0}})

        def store_quote_cache(live_map: dict[tuple[str, str], object], _warning: str) -> None:
            cache_updates.append(dict(live_map or {}))
            cache_event.set()

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: "",
                set_last_sync_key=lambda _value: None,
                quote_route_snapshot=quote_route_reader([spy, btc]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=lambda _items, **_kwargs: asyncio.sleep(0),
                cached_quotes=cached_quotes,
                provider_pollers=(
                    QuoteProviderPoller(
                        provider="coinbase",
                        fetch_quotes=fetch_provider,
                        close=lambda: asyncio.sleep(0),
                    ),
                ),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=lambda _live_map, _instruments: asyncio.sleep(0),
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        try:
            await asyncio.wait_for(cache_event.wait(), timeout=0.5)
            await asyncio.wait_for(provider_started.wait(), timeout=0.5)
            assert not provider_release.is_set()
            assert cache_updates[0] == {
                (qualified_instrument_id(spy), route_instrument(spy).fingerprint): {"price": 750.0}
            }
        finally:
            provider_release.set()
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)

    asyncio.run(run())


def test_quote_orchestrator_hot_publication_is_independent_of_subscription_sync() -> None:
    async def run() -> None:
        sync_started = asyncio.Event()
        sync_release = asyncio.Event()
        published = asyncio.Event()
        publication_times: list[float] = []
        persistence_calls = 0
        spy = ibkr_stock_payload("SPY")

        async def sync_ibkr_subscriptions(
            _instruments: list[dict],
            timeout: float,
        ) -> None:
            assert timeout == 1.0
            sync_started.set()
            await sync_release.wait()

        def store_quote_cache(
            _live_map: dict[tuple[str, str], object],
            _warning: str,
        ) -> None:
            publication_times.append(asyncio.get_running_loop().time())
            if len(publication_times) >= 3:
                published.set()

        async def persist_quote_snapshots(_live_map, _instruments) -> None:
            nonlocal persistence_calls
            persistence_calls += 1

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.02,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: "",
                set_last_sync_key=lambda _value: None,
                quote_route_snapshot=quote_route_reader([spy]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=sync_ibkr_subscriptions,
                cached_quotes=lambda _routes, **_kwargs: QuoteSnapshotRead(
                    1, {route_instrument(spy).fingerprint: {"price": 750.0}}
                ),
                provider_pollers=(),
                store_quote_cache=store_quote_cache,
                persist_quote_snapshots=persist_quote_snapshots,
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        try:
            await asyncio.wait_for(sync_started.wait(), timeout=0.2)
            await asyncio.wait_for(published.wait(), timeout=0.2)
            assert not sync_release.is_set()
            assert publication_times[2] - publication_times[0] < 0.12
            assert persistence_calls == 1
        finally:
            sync_release.set()
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)

    asyncio.run(run())


def test_quote_orchestrator_settles_inflight_snapshot_persistence_before_cancellation() -> None:
    async def run() -> None:
        cache_event = asyncio.Event()
        persist_started = asyncio.Event()
        persist_release = asyncio.Event()
        persist_finished = asyncio.Event()
        spy = ibkr_stock_payload("SPY")

        async def persist_quote_snapshots(_live_map, _instruments) -> None:
            persist_started.set()
            await persist_release.wait()
            persist_finished.set()

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: "",
                set_last_sync_key=lambda _value: None,
                quote_route_snapshot=quote_route_reader([spy]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=lambda _items, **_kwargs: asyncio.sleep(0),
                cached_quotes=lambda _routes, **_kwargs: QuoteSnapshotRead(
                    1, {route_instrument(spy).fingerprint: {"price": 750.0}}
                ),
                provider_pollers=(),
                store_quote_cache=lambda _live_map, _warning: cache_event.set(),
                persist_quote_snapshots=persist_quote_snapshots,
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        await asyncio.wait_for(cache_event.wait(), timeout=0.5)
        await asyncio.wait_for(persist_started.wait(), timeout=0.5)
        loop_task.cancel()
        await asyncio.sleep(0)

        assert not loop_task.done()
        assert not persist_finished.is_set()

        persist_release.set()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        assert persist_finished.is_set()

    asyncio.run(run())


def test_quote_orchestrator_keeps_slow_snapshot_persistence_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sleep = asyncio.sleep
    real_wait_for = asyncio.wait_for

    class FastTimeoutAsyncio:
        """Compress the removed two-second logical deadline for the regression."""

        def __getattr__(self, name: str):
            return getattr(asyncio, name)

        async def sleep(self, _delay: float) -> None:
            await real_sleep(0.001)

        async def wait_for(self, awaitable, timeout: float):
            return await real_wait_for(awaitable, timeout=min(float(timeout), 0.005))

    monkeypatch.setattr(
        quote_orchestrator_service,
        "asyncio",
        FastTimeoutAsyncio(),
    )

    async def run() -> None:
        cache_event = asyncio.Event()
        persist_release = asyncio.Event()
        persist_calls = 0
        persist_cancellations = 0
        spy = ibkr_stock_payload("SPY")

        async def persist_quote_snapshots(_live_map, _instruments) -> None:
            nonlocal persist_calls, persist_cancellations
            persist_calls += 1
            try:
                await persist_release.wait()
            except asyncio.CancelledError:
                persist_cancellations += 1
                raise

        loop_task = asyncio.create_task(
            run_quote_orchestrator_loop(
                startup_delay_seconds=0.0,
                poll_seconds=0.01,
                snapshot_persist_seconds=5.0,
                server_sleeping=lambda: False,
                get_last_sync_key=lambda: "",
                set_last_sync_key=lambda _value: None,
                quote_route_snapshot=quote_route_reader([spy]),
                quote_subscription_generation=lambda: "market_data_type:1",
                sync_ibkr_subscriptions=lambda _items, **_kwargs: asyncio.sleep(0),
                cached_quotes=lambda _routes, **_kwargs: QuoteSnapshotRead(
                    1, {route_instrument(spy).fingerprint: {"price": 750.0}}
                ),
                provider_pollers=(),
                store_quote_cache=lambda _live_map, _warning: cache_event.set(),
                persist_quote_snapshots=persist_quote_snapshots,
                on_cleanup_error=lambda _stage, _provider, _exc: None,
                on_loop_error=lambda _exc: None,
            )
        )
        await real_wait_for(cache_event.wait(), timeout=0.5)
        await real_sleep(0.03)

        assert persist_calls == 1
        assert persist_cancellations == 0

        loop_task.cancel()
        persist_release.set()
        await asyncio.gather(loop_task, return_exceptions=True)

    asyncio.run(run())
