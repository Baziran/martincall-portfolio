from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time

import pytest

from aef_terminal import settings_contract
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.runtime.chart_events import (
    begin_chart_bars_write,
    end_chart_bars_write,
    publish_chart_bars_updated,
)
from aef_terminal.ui import app as ui_app
from aef_terminal.ui import market_actions
from aef_terminal.ui import market_analysis_runtime
from aef_terminal.ui.paper import sync as paper_sync
from aef_terminal.ui.routers import market as market_router
from aef_terminal.ui.services import market_analysis_job
from aef_terminal.ui.services import market_analysis_process, market_analysis_store
from aef_terminal.ui.services import memory_diagnostics
from aef_terminal.ui.services.market_analysis_runtime_service import (
    MarketAnalysisRuntimeService,
)
from aef_terminal.ui.services import market_analysis_runtime_service
from tests.provider_payloads import (
    coinbase_btc_payload,
    ibkr_future_payload,
    ibkr_stock_payload,
)


def _ready_cache(snapshot: dict, **metadata) -> dict:
    snapshot_bytes = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    return {
        "status": "ready",
        "snapshot_json": snapshot_bytes,
        "size_bytes": len(snapshot_bytes),
        **metadata,
    }


def _response_payload(response) -> dict:
    return json.loads(response.body)


@pytest.fixture(autouse=True)
def _isolated_market_analysis_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> MarketAnalysisRuntimeService:
    runtime = MarketAnalysisRuntimeService()
    monkeypatch.setattr(market_analysis_runtime, "_RUNTIME", runtime)
    monkeypatch.setattr(
        market_analysis_process,
        "_PROCESS_RUNTIME",
        market_analysis_process.MarketAnalysisProcessRuntime(),
    )
    return runtime


def test_market_analysis_runtime_lives_outside_app_and_market_route_uses_cache() -> None:
    app_source = inspect.getsource(ui_app)
    market_runtime_source = inspect.getsource(market_analysis_runtime)
    market_service_source = inspect.getsource(MarketAnalysisRuntimeService)
    market_source = inspect.getsource(market_router)
    market_actions_source = inspect.getsource(market_actions)
    memory_diagnostics_source = inspect.getsource(memory_diagnostics)
    paper_sync_source = inspect.getsource(paper_sync)

    assert "CACHE:" not in market_runtime_source
    assert "TASKS:" not in market_runtime_source
    assert "_RUNTIME = MarketAnalysisRuntimeService()" in market_runtime_source
    assert "self._cache:" in market_service_source
    assert "self._tasks:" in market_service_source
    assert "self._wanted:" in market_service_source
    assert "market_analysis_runtime._RUNTIME._cache" not in memory_diagnostics_source
    assert "market_analysis_runtime._RUNTIME._cache" not in paper_sync_source
    assert "async def worker_loop(" in market_runtime_source
    assert "def build_analysis_payload(" in market_runtime_source
    assert "async def _market_analysis_worker_loop()" not in app_source
    assert "async def _register_market_analysis_wanted" not in app_source
    assert '@router.get("/api/market/analysis")' in market_source
    assert "return await market_response(" in market_source
    assert "async_build_market_snapshot_from_db" in market_actions_source
    assert "coalesced_load_confirmed_chart_bars" in market_actions_source
    assert "coalesced_load_chart_bars_from_db" not in market_actions_source
    assert "read_cached_quotes_async" not in market_actions_source
    assert "quote_cache_for_instruments" not in market_actions_source
    assert "include_chart_projection=False" in inspect.getsource(market_analysis_process)
    assert 'snapshot.pop("chart_guides", None)' in inspect.getsource(
        market_analysis_process.compact_analysis_snapshot_for_cache
    )
    assert "default=str" not in inspect.getsource(market_analysis_process)
    assert "default=str" not in inspect.getsource(market_analysis_store.market_analysis_key)
    assert inspect.getsource(market_analysis_process).count("create_subprocess_exec") == 1
    assert "create_subprocess_exec" not in inspect.getsource(
        market_analysis_process.run_market_analysis_process
    )
    assert market_analysis_runtime.diagnostics()["process_workers"] == {
        "status": "not_started",
        "worker_count": 1,
        "busy_workers": 0,
        "workers": [],
        "restart_required": False,
    }


def test_market_analysis_wanted_drives_confirmed_bar_demand_lifecycle() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        reconciled: list[tuple[dict, ...]] = []
        shutdown_calls = 0

        async def reconcile(payloads: tuple[dict, ...]) -> None:
            reconciled.append(payloads)

        async def shutdown_demands() -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1

        runtime.configure(
            market_analysis_runtime.MarketAnalysisRuntimeDeps(
                client_settings_snapshot=dict,
                reconcile_confirmed_bar_demands=reconcile,
                shutdown_confirmed_bar_demands=shutdown_demands,
            )
        )
        instrument = ibkr_stock_payload("SPY", con_id=991001)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)

        assert await runtime.register_wanted(key, payload, "client-1", 1) == "queued"
        assert reconciled == [(payload,)]

        await runtime.shutdown()
        assert shutdown_calls == 1

    asyncio.run(scenario())


def test_market_analysis_client_leases_release_only_last_consumer() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        reconciled: list[tuple[dict, ...]] = []

        async def reconcile(payloads: tuple[dict, ...]) -> None:
            reconciled.append(payloads)

        runtime.configure(
            market_analysis_runtime.MarketAnalysisRuntimeDeps(
                client_settings_snapshot=dict,
                reconcile_confirmed_bar_demands=reconcile,
            )
        )
        instrument = ibkr_stock_payload("SPY", con_id=991011)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)

        assert await runtime.register_wanted(key, payload, "client-a", 1) == "queued"
        assert await runtime.register_wanted(key, payload, "client-b", 1) == "queued"
        assert runtime.diagnostics()["client_leases"] == 2

        assert await runtime.release_client_lease("client-a", 1) is True
        assert key in runtime._wanted
        assert reconciled[-1] == (payload,)
        assert await runtime.release_client_lease("client-a", 1) is True

        assert await runtime.release_client_lease("client-b", 1) is True
        assert key not in runtime._wanted
        assert reconciled[-1] == ()
        await runtime.shutdown()

    asyncio.run(scenario())


def test_provider_session_refresh_revokes_only_exact_route_analysis_leases() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        reconciled: list[tuple[dict, ...]] = []

        async def reconcile(payloads: tuple[dict, ...]) -> None:
            reconciled.append(payloads)

        runtime.configure(
            market_analysis_runtime.MarketAnalysisRuntimeDeps(
                client_settings_snapshot=dict,
                reconcile_confirmed_bar_demands=reconcile,
            )
        )
        from aef_terminal.data.providers import route_instrument

        spy = ibkr_stock_payload("SPY", con_id=991012)
        qqq = ibkr_stock_payload("QQQ", con_id=991013)
        routes = [route_instrument(spy), route_instrument(qqq)]
        payloads = [
            runtime.build_analysis_payload(
                source=route.provider,
                instrument_id=route.instrument_id,
                interval="5m",
                range_="1d",
                signal_range="1d",
                show_visuals=True,
                indicator_params={"rounded_reversal": {"enabled": True}},
                instrument=instrument,
            )
            for route, instrument in zip(routes, (spy, qqq), strict=True)
        ]
        keys = [runtime.analysis_key(payload) for payload in payloads]
        assert await runtime.register_wanted(keys[0], payloads[0], "client-spy", 1) == "queued"
        assert await runtime.register_wanted(keys[1], payloads[1], "client-qqq", 1) == "queued"
        runtime._cache[keys[0]] = _ready_cache({"meta": {}}, updated_monotonic=1.0)
        runtime._cache[keys[1]] = _ready_cache({"meta": {}}, updated_monotonic=1.0)

        invalidated = await runtime.invalidate_provider_session_route(
            routes[0].instrument_id,
            routes[0].fingerprint,
        )

        assert invalidated == 1
        assert keys[0] not in runtime._wanted
        assert keys[0] not in runtime._cache
        assert keys[1] in runtime._wanted
        assert keys[1] in runtime._cache
        assert (
            await runtime.renew_client_lease(
                "client-spy",
                1,
                keys[0],
                routes[0].instrument_id,
                routes[0].fingerprint,
            )
            is False
        )
        assert (
            await runtime.renew_client_lease(
                "client-qqq",
                1,
                keys[1],
                routes[1].instrument_id,
                routes[1].fingerprint,
            )
            is True
        )
        assert reconciled[-1] == (payloads[1],)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_non_channel_parent_active_write_rearms_without_cache_publish() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService(
            generation_settle_seconds=0.5,
        )
        instrument = ibkr_stock_payload("SPY", con_id=991021)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={},
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        assert (
            await runtime.register_wanted(
                key,
                payload,
                "client-active-write",
                1,
            )
            == "queued"
        )
        captured = runtime._wanted[key]["payload"]
        guard = begin_chart_bars_write(
            captured["parent_canonical_generation"],
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        try:
            await runtime.run_analysis_job(key, captured)
        finally:
            end_chart_bars_write(guard)

        assert key in runtime._wanted
        assert runtime._wanted[key]["not_before"] > time.monotonic()
        assert key not in runtime._cache
        await runtime.shutdown()

    asyncio.run(scenario())


def test_non_channel_parent_generation_advance_supersedes_wanted_version() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991022)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={},
            market_version={"window_hash": "captured-window"},
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        assert (
            await runtime.register_wanted(
                key,
                payload,
                "client-generation-advance",
                1,
            )
            == "queued"
        )
        captured = runtime._wanted[key]["payload"]
        runtime._cache[key] = _ready_cache(
            {"stale": True},
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            interval="5m",
            parent_canonical_generation=captured["parent_canonical_generation"],
            market_version={"window_hash": "captured-window"},
            updated_monotonic=time.monotonic(),
        )
        advanced_generation = publish_chart_bars_updated(
            route.provider,
            "5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            reason="test_parent_generation_advance",
        )
        assert advanced_generation > captured["parent_canonical_generation"]
        stale_response = await runtime.snapshot(
            key,
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
        )
        assert stale_response["status"] == "queued"
        assert key not in runtime._cache

        await runtime.run_analysis_job(key, captured)

        assert key not in runtime._wanted
        assert key not in runtime._cache
        await runtime.shutdown()

    asyncio.run(scenario())


def test_channel_master_does_not_create_retained_research_demand() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        runtime.configure(
            market_analysis_runtime.MarketAnalysisRuntimeDeps(
                client_settings_snapshot=dict,
            )
        )
        instrument = ibkr_future_payload("ES", con_id=991013)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "channel_master": {"enabled": True},
                "manual_channels": [{"id": "channel-1"}],
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        await runtime.register_wanted(key, payload, "channel-client", 1)

        assert await runtime.release_client_lease("channel-client", 1) is True
        assert key not in runtime._wanted
        assert "channel_research_scopes" not in runtime.diagnostics()
        assert (
            await runtime.confirmed_bar_context_advanced(
                route.instrument_id,
                route.fingerprint,
                "1m",
                1,
            )
            == 0
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_research_capture_parent_generation_rearms_exact_analysis() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        runtime.configure(
            market_analysis_runtime.MarketAnalysisRuntimeDeps(
                client_settings_snapshot=dict,
            )
        )
        instrument = ibkr_future_payload("ES", con_id=991014)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=False,
            indicator_params={
                "channel_master": {"enabled": True},
            },
            instrument=instrument,
            analysis_effects="research_capture",
        )
        key = runtime.analysis_key(payload)
        await runtime.register_wanted(key, payload, "research-client", 1)
        runtime._cache[key] = _ready_cache({})
        next_generation = int(payload["parent_canonical_generation"]) + 1

        assert (
            await runtime.confirmed_bar_context_advanced(
                route.instrument_id,
                route.fingerprint,
                "5m",
                next_generation,
            )
            == 1
        )
        assert runtime._wanted[key]["payload"]["parent_canonical_generation"] == next_generation
        assert key not in runtime._cache
        await runtime.shutdown()

    asyncio.run(scenario())


def test_market_analysis_client_lease_renews_exact_scope_and_expires() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService(wanted_ttl_seconds=0.1)
        instrument = ibkr_stock_payload("SPY", con_id=991012)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={},
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        await runtime.register_wanted(key, payload, "client-a", 1)

        assert (
            await runtime.renew_client_lease(
                "client-a",
                1,
                key,
                route.instrument_id,
                f"{route.fingerprint}:wrong",
            )
            is False
        )
        assert (
            await runtime.renew_client_lease(
                "client-a",
                1,
                key,
                route.instrument_id,
                route.fingerprint,
            )
            is True
        )

        sequence, renewed_at = runtime._client_leases_by_key[key]["client-a"]
        runtime._client_leases_by_key[key]["client-a"] = (
            sequence,
            renewed_at - 1.0,
        )
        assert (
            await runtime.renew_client_lease(
                "client-a",
                1,
                key,
                route.instrument_id,
                route.fingerprint,
            )
            is False
        )
        assert key not in runtime._wanted
        assert runtime.diagnostics()["client_leases"] == 0
        await runtime.shutdown()

    asyncio.run(scenario())


def test_market_analysis_client_lease_sequence_rejects_late_release_and_registration() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991013)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={},
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)

        assert await runtime.register_wanted(key, payload, "client-a", 1) == "queued"
        assert await runtime.release_client_lease("client-a", 1) is True
        assert await runtime.register_wanted(key, payload, "client-a", 1) == "superseded"
        assert key not in runtime._wanted

        assert await runtime.release_client_lease("client-a", 2) is True
        assert await runtime.register_wanted(key, payload, "client-a", 2) == "superseded"
        assert key not in runtime._wanted

        assert await runtime.register_wanted(key, payload, "client-a", 3) == "queued"
        assert await runtime.release_client_lease("client-a", 2) is False
        assert key in runtime._wanted
        assert (
            await runtime.renew_client_lease(
                "client-a",
                3,
                key,
                route.instrument_id,
                route.fingerprint,
            )
            is True
        )
        await runtime.shutdown()

    asyncio.run(scenario())


def test_confirmed_context_generation_invalidates_only_exact_wanted_route() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991002)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        await runtime.register_wanted(key, payload, "client-1", 1)
        runtime._cache[key] = _ready_cache({})
        active_task = asyncio.create_task(asyncio.Event().wait())
        runtime._tasks[key] = active_task
        runtime._task_identities[key] = runtime.analysis_request_identity(payload)

        assert (
            await runtime.confirmed_bar_context_advanced(
                route.instrument_id,
                route.fingerprint,
                "1m",
                1,
            )
            == 1
        )
        assert (
            await runtime.confirmed_bar_context_advanced(
                route.instrument_id,
                route.fingerprint,
                "1m",
                1,
            )
            == 0
        )
        assert key not in runtime._cache
        assert runtime._wanted[key]["payload"]["confirmed_bar_context_generations"] == {"1m": 1}
        assert runtime._wanted[key]["not_before"] > time.monotonic()
        assert active_task.cancelling()
        await asyncio.gather(active_task, return_exceptions=True)

        runtime._cache[key] = _ready_cache({})
        assert (
            await runtime.confirmed_bar_context_advanced(
                route.instrument_id,
                f"{route.fingerprint}:different",
                "1m",
                1,
            )
            == 0
        )
        assert key in runtime._cache

    asyncio.run(scenario())


def test_register_wanted_rejects_cache_from_prior_confirmed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991003)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        runtime._cache[key] = _ready_cache(
            {},
            updated_monotonic=time.monotonic(),
            confirmed_bar_context_generations={"1m": 1},
        )
        monkeypatch.setattr(
            market_analysis_runtime_service,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: 2,
        )

        assert await runtime.register_wanted(key, payload, "client-1", 1) == "queued"
        assert key not in runtime._cache
        assert runtime._wanted[key]["payload"]["confirmed_bar_context_generations"] == {"1m": 2}

    asyncio.run(scenario())


def test_confirmed_generation_settles_persistent_worker_and_rearms_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991004)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        third_started = asyncio.Event()

        class BlockingPersistentRuntime:
            worker_count = 1

            def __init__(self) -> None:
                self.requests: list[dict] = []
                self.releases: list[asyncio.Event] = []
                self.settled: list[asyncio.Event] = []

            async def execute(
                self,
                request: bytes,
                *,
                timeout_seconds: float,
            ) -> market_analysis_process.MarketAnalysisProcessResult:
                assert timeout_seconds > 0
                index = len(self.requests)
                self.requests.append(json.loads(request))
                release = asyncio.Event()
                settled = asyncio.Event()
                self.releases.append(release)
                self.settled.append(settled)
                (first_started, second_started, third_started)[index].set()
                await release.wait()
                settled.set()
                return market_analysis_process.MarketAnalysisProcessResult(
                    snapshot_bytes=b"{}",
                    research_capture_bytes=None,
                )

        process_runtime = BlockingPersistentRuntime()
        monkeypatch.setattr(market_analysis_process, "_PROCESS_RUNTIME", process_runtime)
        assert await runtime.register_wanted(key, payload, "client-1", 1) == "queued"
        worker = asyncio.create_task(runtime.worker_loop(server_sleeping=lambda: False))
        runtime._wake_worker()
        try:
            await asyncio.wait_for(first_started.wait(), timeout=0.5)

            context_generation = publish_chart_bars_updated(
                route.provider,
                "1m",
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                reason="test_context_advance",
            )
            advance = asyncio.create_task(
                runtime.confirmed_bar_context_advanced(
                    route.instrument_id,
                    route.fingerprint,
                    "1m",
                    context_generation,
                )
            )
            await asyncio.sleep(0)
            assert not advance.done()
            assert not process_runtime.settled[0].is_set()
            process_runtime.releases[0].set()
            assert await asyncio.wait_for(advance, timeout=0.5) == 1
            assert process_runtime.settled[0].is_set()

            await asyncio.wait_for(second_started.wait(), timeout=0.75)
            assert process_runtime.requests[1]["payload"]["confirmed_bar_context_generations"] == {
                "1m": context_generation
            }

            superseding_payload = runtime.build_analysis_payload(
                source=route.provider,
                instrument_id=route.instrument_id,
                interval="5m",
                range_="1d",
                signal_range="1d",
                show_visuals=True,
                indicator_params={
                    "rounded_reversal": {"enabled": True},
                },
                include_telemetry=True,
                instrument=instrument,
            )
            superseding_key = runtime.analysis_key(superseding_payload)
            assert superseding_key != key
            assert (
                await runtime.register_wanted(
                    superseding_key,
                    superseding_payload,
                    "client-1",
                    2,
                )
                == "queued"
            )
            await asyncio.sleep(0)
            assert not third_started.is_set()
            process_runtime.releases[1].set()
            await asyncio.wait_for(third_started.wait(), timeout=0.75)
            assert process_runtime.settled[1].is_set()
            assert process_runtime.requests[2]["payload"]["confirmed_bar_context_generations"] == {
                "1m": context_generation
            }
        finally:
            for release in process_runtime.releases:
                release.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await runtime.shutdown()

    asyncio.run(scenario())


def test_request_controller_rearms_physical_settlement_for_same_job_retry() -> None:
    async def scenario() -> None:
        controller = market_analysis_process.MarketAnalysisRequestController()
        controller._claim_current_task()
        controller._mark_finished()
        await asyncio.wait_for(controller.wait_finished(), timeout=0.1)

        controller._claim_current_task()
        retry_settled = asyncio.create_task(controller.wait_finished())
        await asyncio.sleep(0)
        assert retry_settled.done() is False

        controller._mark_finished()
        await asyncio.wait_for(retry_settled, timeout=0.1)

    asyncio.run(scenario())


def test_market_analysis_process_runtime_reuses_one_prestarted_worker() -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime()
        await runtime.start()
        process = runtime._workers[0].process
        assert process is not None
        assert process.returncode is None
        await runtime.start()
        assert runtime._workers[0].process is process
        assert runtime.diagnostics() == {
            "status": "running",
            "worker_count": 1,
            "busy_workers": 0,
            "workers": [{"pid": process.pid, "returncode": None, "busy": False}],
            "restart_required": False,
        }

        for _attempt in range(2):
            with pytest.raises(RuntimeError, match="KeyError"):
                await runtime.execute(b"{}", timeout_seconds=5.0)
            assert runtime._workers[0].process is process
            assert process.returncode is None

        await runtime.shutdown()
        assert process.returncode == 0
        assert runtime.diagnostics() == {
            "status": "stopped",
            "worker_count": 1,
            "busy_workers": 0,
            "workers": [],
            "restart_required": True,
        }
        with pytest.raises(
            market_analysis_process.MarketAnalysisWorkerUnavailable,
            match="application restart",
        ) as unavailable:
            await runtime.execute(b"{}", timeout_seconds=5.0)
        assert unavailable.value.retryable is False
        assert unavailable.value.error_code == "MARKET_ANALYSIS_WORKER_UNAVAILABLE"
        error_item = market_analysis_store.build_market_analysis_error_item(
            unavailable.value,
            instrument_id="coinbase|contract|BTC-USD",
            route_fingerprint="coinbase|contract|BTC-USD",
        )
        assert error_item["error_code"] == "MARKET_ANALYSIS_WORKER_UNAVAILABLE"
        assert error_item["retryable"] is False

    asyncio.run(scenario())


def test_market_analysis_process_runtime_defers_cancellation_until_exchange_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime()
        runtime._require_loop()
        process = type("RunningProcess", (), {"returncode": None})()
        runtime._workers = [market_analysis_process._AnalysisProcessWorker(process)]
        runtime._started = True
        started = asyncio.Event()
        release = asyncio.Event()

        async def exchange(_process, _request):
            started.set()
            await release.wait()
            return market_analysis_process.MarketAnalysisProcessResult(
                snapshot_bytes=b"{}",
                research_capture_bytes=None,
            )

        monkeypatch.setattr(runtime, "_exchange", exchange)
        task = asyncio.create_task(runtime.execute(b"{}", timeout_seconds=5.0))
        await asyncio.wait_for(started.wait(), timeout=0.5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime._failed is False

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, 9, True, 1.5, "2"])
def test_analysis_worker_count_rejects_invalid_values(value) -> None:
    with pytest.raises(ValueError, match="AEF_MARKET_ANALYSIS_WORKERS"):
        market_analysis_process.MarketAnalysisProcessRuntime(worker_count=value)


def test_analysis_worker_count_is_shared_with_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEF_MARKET_ANALYSIS_WORKERS", "2")
    runtime = market_analysis_process.MarketAnalysisProcessRuntime()
    monkeypatch.setattr(market_analysis_process, "_PROCESS_RUNTIME", runtime)
    captured = {}

    async def worker_loop(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        market_analysis_runtime_service, "run_market_analysis_worker_loop", worker_loop
    )
    asyncio.run(MarketAnalysisRuntimeService().worker_loop(server_sleeping=lambda: False))
    assert captured["max_concurrent"] == runtime.worker_count == 2


def test_analysis_pool_parallelism_is_bounded_and_cancellation_physically_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime(worker_count=2)
        await runtime.start()
        processes = [worker.process for worker in runtime._workers]
        releases = [asyncio.Event() for _ in range(3)]
        starts = [asyncio.Event() for _ in range(3)]
        owners = {}

        async def exchange(process, request):
            index = int(request)
            owners[index] = process.pid
            starts[index].set()
            await releases[index].wait()
            return market_analysis_process.MarketAnalysisProcessResult(snapshot_bytes=request)

        monkeypatch.setattr(runtime, "_exchange", exchange)
        tasks = [
            asyncio.create_task(runtime.execute(str(i).encode(), timeout_seconds=5))
            for i in range(3)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(starts[0].wait(), starts[1].wait()), timeout=1)
            assert owners[0] != owners[1]
            assert runtime.diagnostics()["busy_workers"] == 2
            assert not starts[2].is_set()
            tasks[0].cancel()
            await asyncio.sleep(0)
            tasks[0].cancel()
            await asyncio.sleep(0)
            assert not tasks[0].done()
            assert not starts[2].is_set()
            releases[0].set()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            await asyncio.wait_for(starts[2].wait(), timeout=1)
            assert owners[2] == owners[0]
            releases[1].set()
            releases[2].set()
            assert [result.snapshot_bytes for result in await asyncio.gather(*tasks[1:])] == [
                b"1",
                b"2",
            ]
            assert runtime.diagnostics()["busy_workers"] == 0
            await runtime.start()
            assert [worker.process for worker in runtime._workers] == processes
        finally:
            for release in releases:
                release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runtime.shutdown()
        assert all(process.returncode == 0 for process in processes)

    asyncio.run(scenario())


def test_analysis_pool_protocol_failure_fences_sibling_and_queued_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime(worker_count=2)
        await runtime.start()
        starts = [asyncio.Event(), asyncio.Event()]
        fail = asyncio.Event()
        finish = asyncio.Event()
        calls = []

        async def exchange(_process, request):
            calls.append(request)
            index = int(request)
            starts[index].set()
            if index == 0:
                await fail.wait()
                raise ValueError("broken framing")
            await finish.wait()
            return market_analysis_process.MarketAnalysisProcessResult(snapshot_bytes=b"{}")

        monkeypatch.setattr(runtime, "_exchange", exchange)
        tasks = [
            asyncio.create_task(runtime.execute(str(i).encode(), timeout_seconds=5))
            for i in range(3)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in starts)), timeout=1)
            fail.set()
            for task in [tasks[0], tasks[2]]:
                with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
                    await task
            assert not tasks[1].done()
            finish.set()
            with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
                await tasks[1]
            assert calls == [b"0", b"1"]
            assert runtime.diagnostics()["status"] == "failed"
            with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
                await runtime.start()
        finally:
            fail.set()
            finish.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await runtime.shutdown()

    asyncio.run(scenario())


def test_analysis_pool_shutdown_closes_admission_before_waiting_for_busy_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime()
        await runtime.start()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def exchange(_process, request):
            calls.append(request)
            started.set()
            await release.wait()
            return market_analysis_process.MarketAnalysisProcessResult(snapshot_bytes=b"{}")

        monkeypatch.setattr(runtime, "_exchange", exchange)
        active = asyncio.create_task(runtime.execute(b"active", timeout_seconds=5))
        await asyncio.wait_for(started.wait(), timeout=1)
        queued = asyncio.create_task(runtime.execute(b"queued", timeout_seconds=5))
        await asyncio.sleep(0)
        closing = asyncio.create_task(runtime.shutdown())
        try:
            with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
                await queued
            assert not closing.done()
            release.set()
            await active
            await closing
            assert calls == [b"active"]
            assert runtime.diagnostics()["status"] == "stopped"
        finally:
            release.set()
            await asyncio.gather(active, queued, closing, return_exceptions=True)

    asyncio.run(scenario())


def test_analysis_pool_partial_start_failure_reaps_prestarted_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime(worker_count=2)
        real_spawn = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            if processes:
                raise OSError("cannot start second worker")
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(OSError, match="second worker"):
            await runtime.start()
        assert len(processes) == 1 and processes[0].returncode is not None
        assert runtime.diagnostics()["restart_required"]
        with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
            await runtime.start()
        await runtime.shutdown()

    asyncio.run(scenario())


def test_analysis_pool_idle_exit_prevents_admission_to_healthy_sibling() -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime(worker_count=2)
        await runtime.start()
        process = runtime._workers[0].process
        process.kill()
        await process.wait()
        try:
            assert runtime.diagnostics()["status"] == "exited"
            assert runtime.diagnostics()["restart_required"]
            with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
                await runtime.execute(b"{}", timeout_seconds=5)
            assert runtime.diagnostics()["status"] == "failed"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_analysis_pool_timeout_physically_reaps_worker_and_latches_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = market_analysis_process.MarketAnalysisProcessRuntime()
        await runtime.start()
        process = runtime._workers[0].process

        async def exchange(_process, _request):
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime, "_exchange", exchange)
        try:
            with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable) as error:
                await runtime.execute(b"{}", timeout_seconds=1)
            assert error.value.outcome == "timeout"
            assert process.returncode is not None
            assert runtime.diagnostics()["status"] == "failed"
            with pytest.raises(market_analysis_process.MarketAnalysisWorkerUnavailable):
                await runtime.start()
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_stale_confirmed_context_job_cannot_publish_cache_or_sync_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991005)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        await runtime.register_wanted(key, payload, "client-1", 1)
        captured_payload = dict(runtime._wanted[key]["payload"])
        started = asyncio.Event()
        release = asyncio.Event()
        paper_admissions: list[bool] = []

        async def delayed_job(
            job_key: str,
            _payload: dict,
            **kwargs,
        ) -> None:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            paper_admissions.append(kwargs["auto_paper_trading_enabled"](route.instrument_id))
            kwargs["cache"][job_key] = _ready_cache(
                {"meta": {"timeframe": "5m"}},
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                updated_monotonic=1.0,
            )

        monkeypatch.setattr(
            market_analysis_runtime_service,
            "run_market_analysis_job",
            delayed_job,
        )
        task = asyncio.create_task(runtime.run_analysis_job(key, captured_payload))
        runtime._tasks[key] = task
        runtime._task_identities[key] = runtime.analysis_request_identity(captured_payload)
        await started.wait()
        await runtime.confirmed_bar_context_advanced(
            route.instrument_id,
            route.fingerprint,
            "1m",
            1,
        )
        release.set()
        await task

        assert paper_admissions == [False]
        assert key not in runtime._cache

    asyncio.run(scenario())


def test_global_confirmed_generation_fences_job_before_watcher_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991006)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={
                "rounded_reversal": {"enabled": True},
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        await runtime.register_wanted(key, payload, "client-1", 1)
        captured_payload = dict(runtime._wanted[key]["payload"])
        started = asyncio.Event()
        release = asyncio.Event()
        paper_admissions: list[bool] = []

        async def delayed_job(
            job_key: str,
            _payload: dict,
            **kwargs,
        ) -> None:
            started.set()
            await release.wait()
            paper_admissions.append(kwargs["auto_paper_trading_enabled"](route.instrument_id))
            kwargs["cache"][job_key] = _ready_cache(
                {"meta": {"timeframe": "5m"}},
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                updated_monotonic=1.0,
            )

        monkeypatch.setattr(
            market_analysis_runtime_service,
            "run_market_analysis_job",
            delayed_job,
        )
        task = asyncio.create_task(runtime.run_analysis_job(key, captured_payload))
        runtime._tasks[key] = task
        runtime._task_identities[key] = runtime.analysis_request_identity(captured_payload)
        await started.wait()
        generation = publish_chart_bars_updated(
            route.provider,
            "1m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            reason="test_global_fence",
        )
        assert generation > captured_payload["confirmed_bar_context_generations"]["1m"]
        release.set()
        await task

        assert paper_admissions == [False]
        assert key not in runtime._cache

    asyncio.run(scenario())


def test_market_analysis_runtime_shutdown_cancels_owned_jobs_and_clears_state() -> None:
    async def run() -> None:
        runtime = market_analysis_runtime._RUNTIME
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        runtime._tasks["job"] = task
        runtime._task_identities["job"] = ("identity",)
        runtime._wanted["job"] = {"payload": {}}
        runtime._cache["job"] = _ready_cache({})

        await market_analysis_runtime.shutdown()

        assert task.cancelled()
        assert runtime.diagnostics()["cache_entries"] == 0
        assert runtime.diagnostics()["wanted_entries"] == 0
        assert runtime.diagnostics()["task_entries"] == 0
        assert runtime.diagnostics()["identity_entries"] == 0

    asyncio.run(run())


def test_auto_paper_trading_permission_is_explicit_per_instrument_and_fail_closed(
    caplog,
) -> None:
    instrument_id = "ibkr|future_root|ES|CME|USD|ES"
    disabled_instrument_id = "coinbase|contract|BTC-USD"
    missing_instrument_id = "ibkr|stock|SPY"
    settings = {
        settings_contract.instrument_paper_auto_trading_setting_key(instrument_id): "true",
        settings_contract.instrument_paper_auto_trading_setting_key(
            disabled_instrument_id
        ): "false",
    }
    market_analysis_runtime.configure_market_analysis_runtime_deps(
        market_analysis_runtime.MarketAnalysisRuntimeDeps(
            client_settings_snapshot=lambda: dict(settings)
        )
    )

    assert market_analysis_runtime.auto_paper_trading_enabled(instrument_id) is True
    assert market_analysis_runtime.auto_paper_trading_enabled(disabled_instrument_id) is False
    assert market_analysis_runtime.auto_paper_trading_enabled(missing_instrument_id) is False

    def broken_settings_reader() -> dict:
        raise RuntimeError("settings unavailable")

    market_analysis_runtime.configure_market_analysis_runtime_deps(
        market_analysis_runtime.MarketAnalysisRuntimeDeps(
            client_settings_snapshot=broken_settings_reader
        )
    )
    caplog.set_level(logging.ERROR, logger=market_analysis_runtime.__name__)

    assert market_analysis_runtime.auto_paper_trading_enabled(instrument_id) is False
    assert "auto paper trading disabled" in caplog.text
    assert f"instrument_id={instrument_id}" in caplog.text


def test_market_analysis_client_cancellation_does_not_cancel_shared_job() -> None:
    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        blocker = asyncio.Event()
        shared_job = asyncio.create_task(blocker.wait())
        market_analysis_runtime._RUNTIME._tasks["shared"] = shared_job
        market_analysis_runtime._RUNTIME._task_identities["shared"] = (
            "coinbase",
            "coinbase|contract|BTC-USD",
            "coinbase|contract|BTC-USD",
            "5m",
            "5d",
            "5d",
            True,
        )
        request = asyncio.create_task(
            market_analysis_runtime.snapshot(
                "shared",
                wait_seconds=30.0,
                instrument_id="coinbase|contract|BTC-USD",
                expected_route_fingerprint="coinbase|contract|BTC-USD",
            )
        )
        await asyncio.sleep(0)
        request.cancel()

        response = await request

        assert response["status"] == "running"
        assert not shared_job.cancelled()
        shared_job.cancel()
        try:
            await shared_job
        except asyncio.CancelledError:
            pass
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()

    asyncio.run(run())


def test_market_analysis_status_is_exact_scope_and_rejects_key_scope_mismatch() -> None:
    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        key = "btc-analysis"
        btc_instrument_id = "coinbase|contract|BTC-USD"
        btc_route = "coinbase|contract|BTC-USD"
        market_analysis_runtime._RUNTIME._wanted[key] = {
            "payload": {
                "source": "coinbase",
                "instrument_id": btc_instrument_id,
                "route_fingerprint": btc_route,
                "interval": "5m",
                "range": "5d",
                "signal_range": "5d",
                "show_visuals": True,
            }
        }
        try:
            queued = await market_analysis_runtime.snapshot(
                key,
                instrument_id=btc_instrument_id,
                expected_route_fingerprint=btc_route,
            )
            assert queued["status"] == "queued"
            assert queued["instrument_id"] == btc_instrument_id
            assert queued["route_fingerprint"] == btc_route

            mismatch = await market_analysis_runtime.snapshot(
                key,
                instrument_id="ibkr|contract|756733",
                expected_route_fingerprint="ibkr|contract|756733",
            )
            assert mismatch["status"] == "error"
            assert mismatch["error"]["code"] == "MARKET_ANALYSIS_SCOPE_MISMATCH"
            assert mismatch["instrument_id"] == btc_instrument_id
            assert mismatch["route_fingerprint"] == btc_route

            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache[key] = _ready_cache(
                {
                    "ok": True,
                    "meta": {
                        "instrument_id": btc_instrument_id,
                        "route_fingerprint": btc_route,
                    },
                },
                instrument_id=btc_instrument_id,
                route_fingerprint=btc_route,
            )
            cached_mismatch = await market_analysis_runtime.snapshot(
                key,
                instrument_id="ibkr|contract|756733",
                expected_route_fingerprint="ibkr|contract|756733",
            )
            assert cached_mismatch["status"] == "error"
            assert cached_mismatch["error"]["code"] == "MARKET_ANALYSIS_SCOPE_MISMATCH"
            cached_ready = await market_analysis_runtime.snapshot(
                key,
                instrument_id=btc_instrument_id,
                expected_route_fingerprint=btc_route,
            )
            assert _response_payload(cached_ready)["ok"] is True

            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._cache["unscoped-analysis"] = _ready_cache(
                {"ok": True, "meta": {}}
            )
            unscoped = await market_analysis_runtime.snapshot(
                "unscoped-analysis",
                instrument_id=btc_instrument_id,
                expected_route_fingerprint=btc_route,
            )
            assert unscoped["status"] == "missing"
            assert unscoped["ok"] is False

            market_analysis_runtime._RUNTIME._cache.clear()
            missing = await market_analysis_runtime.snapshot(
                "missing-analysis",
                instrument_id=btc_instrument_id,
                expected_route_fingerprint=btc_route,
            )
            assert missing["status"] == "missing"
            assert missing["instrument_id"] == btc_instrument_id
            assert missing["route_fingerprint"] == btc_route
            missing_key = await market_analysis_runtime.snapshot(
                "",
                instrument_id=btc_instrument_id,
                expected_route_fingerprint=btc_route,
            )
            assert missing_key["status"] == "missing"
            assert missing_key["instrument_id"] == btc_instrument_id
            assert missing_key["route_fingerprint"] == btc_route
        finally:
            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._tasks.clear()
            market_analysis_runtime._RUNTIME._task_identities.clear()

    asyncio.run(run())


def test_market_analysis_wanted_replaces_stale_same_chart_keys() -> None:
    market_analysis_runtime._RUNTIME._wanted.clear()
    market_analysis_runtime._RUNTIME._cache.clear()
    market_analysis_runtime._RUNTIME._tasks.clear()
    market_analysis_runtime._RUNTIME._task_identities.clear()
    base_payload = {
        "source": "ibkr",
        "instrument_id": "ibkr|future_root|ES|CME|USD|",
        "route_fingerprint": "ibkr|future_root|ES|CME|USD||current:11004968",
        "symbol": "ES",
        "interval": "5m",
        "range": "3d",
        "signal_range": "3d",
        "show_visuals": True,
        "indicator_params": {"martin_carlo": {"enabled": True, "price_bins": 20}},
    }
    first_key = market_analysis_runtime.analysis_key(base_payload)
    asyncio.run(
        market_analysis_runtime.register_wanted(
            first_key,
            base_payload,
            "client-1",
            1,
        )
    )
    market_analysis_runtime._RUNTIME._cache[first_key] = _ready_cache(
        {"meta": {"analysis_key": first_key}},
        updated_monotonic=1.0,
    )
    updated_payload = {
        **base_payload,
        "indicator_params": {"martin_carlo": {"enabled": True, "price_bins": 8}},
    }
    second_key = market_analysis_runtime.analysis_key(updated_payload)
    asyncio.run(
        market_analysis_runtime.register_wanted(
            second_key,
            updated_payload,
            "client-1",
            2,
        )
    )

    assert first_key not in market_analysis_runtime._RUNTIME._wanted
    assert first_key not in market_analysis_runtime._RUNTIME._cache
    assert second_key in market_analysis_runtime._RUNTIME._wanted
    assert (
        market_analysis_runtime._RUNTIME.analysis_requires_periodic_refresh(updated_payload)
        is False
    )
    assert (
        market_analysis_runtime.analysis_refresh_seconds(updated_payload)
        == market_analysis_runtime.REFRESH_SECONDS
    )
    assert indicator_manifest()["martin_carlo"]["analysis_refresh_interval_seconds"] is None
    assert "martin_carlo" not in inspect.getsource(
        market_analysis_store.market_analysis_refresh_seconds
    )
    assert (
        market_analysis_runtime.analysis_refresh_seconds(
            {**updated_payload, "indicator_params": {}}
        )
        == market_analysis_runtime.REFRESH_SECONDS
    )
    assert market_analysis_runtime.REFRESH_SECONDS == 10.0


def test_market_analysis_cache_ignores_client_version_only() -> None:
    cached = {
        "parent_canonical_generation": 3,
        "confirmed_bar_context_generations": {"1m": 7},
        "market_version": {
            "latest_ts": "2026-08-03T10:00:00+00:00",
            "window_hash": "window-a",
            "client": "quote-a",
        },
    }
    payload = {
        "parent_canonical_generation": 3,
        "confirmed_bar_context_generations": {"1m": 7},
        "market_version": {
            "latest_ts": "2026-08-03T10:00:00+00:00",
            "window_hash": "window-a",
            "client": "quote-b",
        },
    }

    assert (
        MarketAnalysisRuntimeService._cache_matches_payload_context(
            cached,
            payload,
        )
        is True
    )
    assert (
        MarketAnalysisRuntimeService._cache_matches_payload_context(
            cached,
            {**payload, "parent_canonical_generation": 4},
        )
        is False
    )
    assert (
        MarketAnalysisRuntimeService._cache_matches_payload_context(
            cached,
            {
                **payload,
                "market_version": {
                    **payload["market_version"],
                    "window_hash": "window-b",
                },
            },
        )
        is False
    )


def test_registered_analysis_refresh_reuses_exact_lease_without_chart_rebuild() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991009)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={"martin_carlo": {"enabled": True}},
            market_version={
                "latest_ts": "2026-08-03T10:00:00+00:00",
                "window_hash": "window-a",
                "client": "quote-a",
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        assert (
            await runtime.register_wanted(
                key,
                payload,
                "client-1",
                4,
            )
            == "queued"
        )
        cached = _ready_cache({}, updated_monotonic=time.monotonic())
        runtime._cache[key] = cached
        previous_wanted_at = runtime._wanted[key]["wanted_at"]

        assert (
            await runtime.refresh_client_analysis(
                "client-1",
                4,
                key,
                route.instrument_id,
                route.fingerprint,
                "quote-b",
            )
            is True
        )
        assert runtime._wanted[key]["wanted_at"] >= previous_wanted_at
        assert runtime._wanted[key]["payload"]["market_version"] == {
            "latest_ts": "2026-08-03T10:00:00+00:00",
            "window_hash": "window-a",
            "client": "quote-b",
        }
        assert runtime._cache[key] is cached
        assert runtime._worker_wake.qsize() == 1

        cached["updated_monotonic"] = time.monotonic() - runtime.versioned_refresh_seconds - 1.0
        assert (
            await runtime.refresh_client_analysis(
                "client-1",
                4,
                key,
                route.instrument_id,
                route.fingerprint,
                "quote-c",
            )
            is True
        )
        assert key not in runtime._cache

        assert (
            await runtime.refresh_client_analysis(
                "client-1",
                3,
                key,
                route.instrument_id,
                route.fingerprint,
                "stale",
            )
            is False
        )
        assert (
            await runtime.refresh_client_analysis(
                "client-1",
                4,
                key,
                route.instrument_id,
                f"{route.fingerprint}:other",
                "wrong-route",
            )
            is False
        )
        assert runtime._wanted[key]["payload"]["market_version"]["client"] == "quote-c"
        await runtime.shutdown()

    asyncio.run(scenario())


def test_same_key_market_version_supersedes_running_analysis() -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991006)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={},
            market_version={
                "latest_ts": "2026-08-03T10:00:00+00:00",
                "window_hash": "window-a",
                "client": "client-a",
            },
            instrument=instrument,
        )
        key = runtime.analysis_key(payload)
        assert await runtime.register_wanted(key, payload, "client-1", 1) == "queued"

        active_task = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        runtime._tasks[key] = active_task
        runtime._task_identities[key] = runtime.analysis_request_identity(payload)
        client_only = {
            **payload,
            "market_version": {
                **payload["market_version"],
                "client": "client-b",
            },
        }
        assert (
            await runtime.register_wanted(
                key,
                client_only,
                "client-1",
                2,
            )
            == "running"
        )
        assert active_task.cancelling() == 0

        latest = {
            **client_only,
            "market_version": {
                **client_only["market_version"],
                "window_hash": "window-b",
            },
        }
        assert runtime.analysis_key(latest) == key
        assert (
            runtime.analysis_key(
                {
                    **latest,
                    "parent_canonical_generation": (latest["parent_canonical_generation"] + 1),
                }
            )
            == key
        )
        assert (
            await runtime.register_wanted(
                key,
                latest,
                "client-1",
                3,
            )
            == "running"
        )
        assert active_task.cancelling()
        assert runtime._wanted[key]["payload"] == latest

        await asyncio.gather(active_task, return_exceptions=True)
        await runtime.shutdown()

    asyncio.run(scenario())


def test_market_analysis_job_rejects_result_superseded_during_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        runtime = MarketAnalysisRuntimeService()
        instrument = ibkr_stock_payload("SPY", con_id=991007)
        from aef_terminal.data.providers import route_instrument

        route = route_instrument(instrument)
        payload = runtime.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval="5m",
            range_="1d",
            signal_range="1d",
            show_visuals=True,
            indicator_params={},
            market_version={
                "latest_ts": "2026-08-03T10:00:00+00:00",
                "window_hash": "window-a",
            },
            instrument=instrument,
        )
        latest = {
            **payload,
            "market_version": {
                **payload["market_version"],
                "window_hash": "window-b",
            },
        }
        key = runtime.analysis_key(payload)
        started = asyncio.Event()
        release = asyncio.Event()

        async def build_stale_result(
            job_key: str,
            job_payload: dict,
            *,
            cache: dict,
            **_kwargs,
        ) -> None:
            started.set()
            await release.wait()
            cache[job_key] = _ready_cache(
                {"ok": True},
                instrument_id=job_payload["instrument_id"],
                route_fingerprint=job_payload["route_fingerprint"],
                market_version=dict(job_payload["market_version"]),
            )

        monkeypatch.setattr(
            market_analysis_runtime_service,
            "run_market_analysis_job",
            build_stale_result,
        )
        assert await runtime.register_wanted(key, payload, "client-1", 1) == "queued"
        task = asyncio.create_task(runtime.run_analysis_job(key, payload))
        runtime._tasks[key] = task
        runtime._task_identities[key] = runtime.analysis_request_identity(payload)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        async with runtime._lock:
            runtime._wanted[key]["payload"] = latest
        release.set()
        await asyncio.wait_for(task, timeout=0.5)

        assert key not in runtime._cache
        assert runtime._wanted[key]["payload"] == latest
        assert key not in runtime._tasks
        assert key not in runtime._task_identities
        await runtime.shutdown()

    asyncio.run(scenario())


def test_market_analysis_cache_ttl_cannot_precede_versioned_refresh() -> None:
    runtime = MarketAnalysisRuntimeService(
        cache_ttl_seconds=45.0,
        refresh_seconds=10.0,
        versioned_refresh_seconds=60.0,
    )

    assert runtime.versioned_refresh_seconds == 60.0
    assert runtime.cache_ttl_seconds == 60.0


def test_market_analysis_refresh_uses_change_owner_cadence() -> None:
    runtime = MarketAnalysisRuntimeService()
    base = {
        "indicator_params": {},
        "gex_context_active": False,
    }

    assert runtime.analysis_requires_periodic_refresh(base) is False
    assert runtime.analysis_refresh_seconds(base) == 10.0
    assert runtime.analysis_requires_periodic_refresh({**base, "gex_context_active": True}) is True
    assert runtime.analysis_refresh_seconds({**base, "gex_context_active": True}) == 300.0
    absorption_params = {
        **base,
        "indicator_params": {
            "absorption_trap": {"enabled": True},
        },
    }
    assert runtime.analysis_requires_periodic_refresh(absorption_params) is True
    assert runtime.analysis_refresh_seconds(absorption_params) == 300.0
    assert (
        runtime.analysis_requires_periodic_refresh(
            {
                **base,
                "indicator_params": {
                    "tick_flow": {"enabled": True},
                },
            }
        )
        is True
    )
    assert (
        runtime.analysis_refresh_seconds(
            {
                **base,
                "indicator_params": {
                    "tick_flow": {"enabled": True},
                },
            }
        )
        == 10.0
    )
    martin_carlo = {
        **base,
        "indicator_params": {
            "martin_carlo": {"enabled": True},
        },
    }
    assert runtime.analysis_requires_periodic_refresh(martin_carlo) is False
    assert runtime.analysis_refresh_seconds(martin_carlo) == 10.0
    option_reversal = {
        **base,
        "indicator_params": {
            "option_reversal": {"enabled": True},
        },
    }
    assert runtime.analysis_requires_periodic_refresh(option_reversal) is False
    assert runtime.analysis_refresh_seconds(option_reversal) == 10.0
    assert (
        runtime.analysis_requires_periodic_refresh({**option_reversal, "gex_context_active": True})
        is True
    )
    assert (
        runtime.analysis_refresh_seconds({**option_reversal, "gex_context_active": True}) == 300.0
    )


def test_active_market_analysis_cache_is_not_expired_by_wall_clock() -> None:
    runtime = MarketAnalysisRuntimeService(cache_ttl_seconds=60.0)
    key = "active-versioned-analysis"
    runtime._cache[key] = _ready_cache({}, updated_monotonic=1.0)
    runtime._wanted[key] = {"payload": {"market_version": {"v": 1}}}

    runtime._trim_cache_locked(1000.0)
    assert key in runtime._cache

    runtime._wanted.clear()
    runtime._trim_cache_locked(1000.0)
    assert key not in runtime._cache


def test_market_analysis_job_drops_superseded_result() -> None:
    market_analysis_runtime._RUNTIME._wanted.clear()
    market_analysis_runtime._RUNTIME._cache.clear()
    market_analysis_runtime._RUNTIME._tasks.clear()
    market_analysis_runtime._RUNTIME._task_identities.clear()
    payload = {
        "source": "ibkr",
        "instrument_id": "ibkr|future_root|ES|CME|USD|",
        "route_fingerprint": "ibkr|future_root|ES|CME|USD||current:11004968",
        "symbol": "ES",
        "interval": "5m",
        "range": "3d",
        "signal_range": "3d",
        "show_visuals": True,
        "indicator_params": {},
    }
    key = market_analysis_runtime.analysis_key(payload)
    market_analysis_runtime._RUNTIME._task_identities[key] = (
        market_analysis_runtime.analysis_request_identity(payload)
    )

    asyncio.run(market_analysis_runtime.run_analysis_job(key, payload))

    assert key not in market_analysis_runtime._RUNTIME._task_identities
    assert key not in market_analysis_runtime._RUNTIME._cache


def test_market_analysis_job_failure_is_logged_and_published(caplog, monkeypatch) -> None:
    async def fail_analysis(_key, _payload):
        raise RuntimeError("analysis failed")

    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        instrument = coinbase_btc_payload()
        payload = market_analysis_runtime.build_analysis_payload(
            source="coinbase",
            instrument_id="coinbase|contract|BTC-USD",
            interval="5m",
            range_="5d",
            signal_range="5d",
            show_visuals=True,
            indicator_params={},
            instrument=instrument,
        )
        key = market_analysis_runtime.analysis_key(payload)
        market_analysis_runtime._RUNTIME._wanted[key] = {"payload": payload}
        market_analysis_runtime._RUNTIME._task_identities[key] = (
            market_analysis_runtime.analysis_request_identity(payload)
        )
        try:
            await market_analysis_runtime.run_analysis_job(key, payload)

            cached = market_analysis_runtime._RUNTIME._cache[key]
            assert cached["status"] == "error"
            assert cached["message"] == "analysis failed"
            assert cached["instrument_id"] == "coinbase|contract|BTC-USD"
            assert cached["route_fingerprint"] == "coinbase|contract|BTC-USD"
            assert key not in market_analysis_runtime._RUNTIME._tasks
            assert key not in market_analysis_runtime._RUNTIME._task_identities
        finally:
            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._tasks.clear()
            market_analysis_runtime._RUNTIME._task_identities.clear()

    monkeypatch.setattr(
        market_analysis_job,
        "run_market_analysis_process",
        fail_analysis,
    )
    caplog.set_level(logging.WARNING, logger=market_analysis_runtime.__name__)
    asyncio.run(run())

    assert "provider=market_analysis" in caplog.text
    assert "symbol=BTC-USD" in caplog.text
    assert "instrument_id=coinbase|contract|BTC-USD" in caplog.text
    assert "analysis failed" in caplog.text


def test_market_analysis_snapshot_waits_on_canonical_task_once() -> None:
    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        try:
            key = "wait-ready"
            analysis_payload = {
                "source": "coinbase",
                "instrument_id": "coinbase|contract|BTC-USD",
                "route_fingerprint": "coinbase|contract|BTC-USD",
                "interval": "5m",
                "range": "5d",
                "signal_range": "5d",
                "show_visuals": True,
            }
            market_analysis_runtime._RUNTIME._wanted[key] = {"payload": analysis_payload}

            async def complete() -> None:
                await asyncio.sleep(0.01)
                async with market_analysis_runtime._RUNTIME._lock:
                    market_analysis_runtime._RUNTIME._cache[key] = _ready_cache(
                        {"ok": True, "meta": {"analysis_key": key}}
                    )

            task = asyncio.create_task(complete())
            market_analysis_runtime._RUNTIME._tasks[key] = task
            payload = await market_analysis_runtime.snapshot(
                key,
                wait_seconds=0.2,
                instrument_id="coinbase|contract|BTC-USD",
                expected_route_fingerprint="coinbase|contract|BTC-USD",
            )
            await task

            ready_payload = _response_payload(payload)
            assert ready_payload["ok"] is True
            assert ready_payload["meta"]["analysis_key"] == key
        finally:
            active_tasks = tuple(market_analysis_runtime._RUNTIME._tasks.values())
            for active_task in active_tasks:
                if not active_task.done():
                    active_task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._tasks.clear()
            market_analysis_runtime._RUNTIME._task_identities.clear()

    asyncio.run(run())


def test_market_analysis_snapshot_surfaces_escaped_task_failure(caplog) -> None:
    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        key = "escaped-failure"
        payload = {
            "source": "coinbase",
            "instrument_id": "coinbase|contract|BTC-USD",
            "route_fingerprint": "coinbase|contract|BTC-USD",
            "interval": "5m",
            "range": "5d",
            "signal_range": "5d",
            "show_visuals": True,
        }
        market_analysis_runtime._RUNTIME._wanted[key] = {"payload": payload}
        market_analysis_runtime._RUNTIME._task_identities[key] = (
            market_analysis_runtime.analysis_request_identity(payload)
        )

        async def fail_outside_job_boundary() -> None:
            raise RuntimeError("escaped shared task failure")

        task = asyncio.create_task(fail_outside_job_boundary())
        market_analysis_runtime._RUNTIME._tasks[key] = task
        try:
            response = await market_analysis_runtime.snapshot(
                key,
                wait_seconds=0.2,
                instrument_id=payload["instrument_id"],
                expected_route_fingerprint=payload["route_fingerprint"],
            )

            assert response["status"] == "error"
            assert response["error"]["code"] == "MARKET_ANALYSIS_FAILED"
            assert "escaped shared task failure" in response["message"]
            assert key not in market_analysis_runtime._RUNTIME._tasks
            assert key not in market_analysis_runtime._RUNTIME._task_identities
            assert market_analysis_runtime._RUNTIME._cache[key]["status"] == "error"
        finally:
            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._tasks.clear()
            market_analysis_runtime._RUNTIME._task_identities.clear()

    caplog.set_level(logging.WARNING, logger=market_analysis_runtime.__name__)
    asyncio.run(run())

    assert "op=market_analysis_snapshot_wait" in caplog.text
    assert "escaped shared task failure" in caplog.text


def test_market_analysis_snapshot_rereads_replacement_after_stale_task_failure(
    caplog,
) -> None:
    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        key = "replaced-failure"
        payload = {
            "source": "coinbase",
            "instrument_id": "coinbase|contract|BTC-USD",
            "route_fingerprint": "coinbase|contract|BTC-USD",
            "interval": "5m",
            "range": "5d",
            "signal_range": "5d",
            "show_visuals": True,
        }
        identity = market_analysis_runtime.analysis_request_identity(payload)
        market_analysis_runtime._RUNTIME._wanted[key] = {"payload": payload}
        market_analysis_runtime._RUNTIME._task_identities[key] = identity
        release_failure = asyncio.Event()
        replacement_tasks: list[asyncio.Task[None]] = []

        async def publish_replacement() -> None:
            async with market_analysis_runtime._RUNTIME._lock:
                market_analysis_runtime._RUNTIME._cache[key] = _ready_cache(
                    {
                        "ok": True,
                        "meta": {"analysis_key": key, "generation": "replacement"},
                    },
                    instrument_id=payload["instrument_id"],
                    route_fingerprint=payload["route_fingerprint"],
                )
                if market_analysis_runtime._RUNTIME._tasks.get(key) is asyncio.current_task():
                    market_analysis_runtime._RUNTIME._tasks.pop(key, None)
                    market_analysis_runtime._RUNTIME._task_identities.pop(key, None)

        async def fail_after_replacement() -> None:
            await release_failure.wait()
            replacement = asyncio.create_task(publish_replacement())
            replacement_tasks.append(replacement)
            async with market_analysis_runtime._RUNTIME._lock:
                market_analysis_runtime._RUNTIME._tasks[key] = replacement
                market_analysis_runtime._RUNTIME._task_identities[key] = identity
            await replacement
            raise RuntimeError("stale task failure")

        old_task = asyncio.create_task(fail_after_replacement())
        market_analysis_runtime._RUNTIME._tasks[key] = old_task
        try:
            snapshot_task = asyncio.create_task(
                market_analysis_runtime.snapshot(
                    key,
                    wait_seconds=0.2,
                    instrument_id=payload["instrument_id"],
                    expected_route_fingerprint=payload["route_fingerprint"],
                )
            )
            await asyncio.sleep(0)
            release_failure.set()
            response = await snapshot_task
            if replacement_tasks:
                await asyncio.gather(*replacement_tasks)

            response_payload = _response_payload(response)
            assert response_payload["ok"] is True
            assert response_payload["meta"]["generation"] == "replacement"
            assert market_analysis_runtime._RUNTIME._cache[key]["status"] == "ready"
        finally:
            pending = [task for task in (old_task, *replacement_tasks) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._tasks.clear()
            market_analysis_runtime._RUNTIME._task_identities.clear()

    caplog.set_level(logging.WARNING, logger=market_analysis_runtime.__name__)
    asyncio.run(run())

    assert "stale task failure" not in caplog.text


def test_market_analysis_snapshot_rereads_when_shared_task_is_cancelled() -> None:
    async def run() -> None:
        market_analysis_runtime._RUNTIME._wanted.clear()
        market_analysis_runtime._RUNTIME._cache.clear()
        market_analysis_runtime._RUNTIME._tasks.clear()
        market_analysis_runtime._RUNTIME._task_identities.clear()
        key = "cancelled-shared-task"
        payload = {
            "source": "coinbase",
            "instrument_id": "coinbase|contract|BTC-USD",
            "route_fingerprint": "coinbase|contract|BTC-USD",
            "interval": "5m",
            "range": "5d",
            "signal_range": "5d",
            "show_visuals": True,
        }
        identity = market_analysis_runtime.analysis_request_identity(payload)
        market_analysis_runtime._RUNTIME._wanted[key] = {"payload": payload}
        market_analysis_runtime._RUNTIME._task_identities[key] = identity

        async def old_job() -> None:
            await asyncio.Event().wait()

        async def replacement_job() -> None:
            async with market_analysis_runtime._RUNTIME._lock:
                market_analysis_runtime._RUNTIME._cache[key] = _ready_cache(
                    {
                        "ok": True,
                        "meta": {
                            "analysis_key": key,
                            "generation": "replacement",
                        },
                    },
                    instrument_id=payload["instrument_id"],
                    route_fingerprint=payload["route_fingerprint"],
                )

        old_task = asyncio.create_task(old_job())
        market_analysis_runtime._RUNTIME._tasks[key] = old_task
        replacement_task: asyncio.Task[None] | None = None
        try:
            snapshot_task = asyncio.create_task(
                market_analysis_runtime.snapshot(
                    key,
                    wait_seconds=0.2,
                    instrument_id=payload["instrument_id"],
                    expected_route_fingerprint=payload["route_fingerprint"],
                )
            )
            await asyncio.sleep(0)
            replacement_task = asyncio.create_task(replacement_job())
            async with market_analysis_runtime._RUNTIME._lock:
                market_analysis_runtime._RUNTIME._tasks[key] = replacement_task
                market_analysis_runtime._RUNTIME._task_identities[key] = identity
            old_task.cancel()

            response = await snapshot_task
            await replacement_task

            response_payload = _response_payload(response)
            assert response_payload["ok"] is True
            assert response_payload["meta"]["generation"] == "replacement"
        finally:
            pending = [
                task
                for task in (old_task, replacement_task)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            market_analysis_runtime._RUNTIME._wanted.clear()
            market_analysis_runtime._RUNTIME._cache.clear()
            market_analysis_runtime._RUNTIME._tasks.clear()
            market_analysis_runtime._RUNTIME._task_identities.clear()

    asyncio.run(run())
