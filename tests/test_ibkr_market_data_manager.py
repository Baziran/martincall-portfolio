from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.domain import Bar
from aef_terminal.data import provider_history as loader_patchable
from aef_terminal.data.adapters import ibkr_bars as ibkr_loader
from aef_terminal.data.adapters import ibkr_live
from aef_terminal.data.ibkr import quotes as ibkr_quotes
from aef_terminal.data.ibkr.manager import IbkrMarketDataManager, ibkr_market_data_manager
from aef_terminal.data.ibkr.runtime import _IBKR_RUNTIME
from aef_terminal.data.providers import get_provider
from aef_terminal.runtime.metrics import reset_runtime_metrics, runtime_metrics_snapshot
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


def ibkr_identity(key: str = "ES") -> dict:
    return ibkr_future_payload(key)


def test_market_data_manager_serializes_same_lane_jobs() -> None:
    manager = IbkrMarketDataManager(lanes=("history",))
    running = 0
    max_running = 0
    order: list[str] = []

    async def job(name: str) -> str:
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        order.append(f"start:{name}")
        await asyncio.sleep(0.01)
        order.append(f"end:{name}")
        running -= 1
        return name

    async def run_jobs() -> list[str]:
        return await asyncio.gather(
            manager.run_coroutine("history", "first", lambda: job("first")),
            manager.run_coroutine("history", "second", lambda: job("second")),
        )

    assert asyncio.run(run_jobs()) == ["first", "second"]
    assert max_running == 1
    assert order == ["start:first", "end:first", "start:second", "end:second"]
    status = manager.status()["lanes"]["history"]
    assert status["completed"] == 2
    assert status["pending"] == 0
    assert status["running"] is False


def test_first_manager_loop_start_does_not_block_the_caller_event_loop(
    monkeypatch,
) -> None:
    manager = IbkrMarketDataManager(lanes=("control",))
    started = threading.Event()
    release = threading.Event()
    heartbeats: list[str] = []

    async def run_probe() -> str:
        caller_loop = asyncio.get_running_loop()

        def slow_loop_start():
            started.set()
            assert release.wait(timeout=0.5)
            return caller_loop

        monkeypatch.setattr(manager, "ensure_loop", slow_loop_start)

        async def heartbeat() -> None:
            while not started.is_set():
                await asyncio.sleep(0)
            heartbeats.append("alive")
            release.set()

        result, _ = await asyncio.gather(
            manager.run_coroutine(
                "control",
                "first startup",
                lambda: asyncio.sleep(0, result="ok"),
            ),
            heartbeat(),
        )
        return result

    assert asyncio.run(run_probe()) == "ok"

    assert heartbeats == ["alive"]


def test_first_manager_loop_start_defers_cancellation_until_physical_start_settles(
    monkeypatch,
) -> None:
    manager = IbkrMarketDataManager(lanes=("control",))
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def scenario() -> None:
        caller_loop = asyncio.get_running_loop()

        def slow_loop_start():
            started.set()
            assert release.wait(timeout=2)
            finished.set()
            return caller_loop

        monkeypatch.setattr(manager, "ensure_loop", slow_loop_start)
        task = asyncio.create_task(
            manager.run_coroutine(
                "control",
                "cancelled startup",
                lambda: asyncio.sleep(0, result="unexpected"),
            )
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() is True

    asyncio.run(scenario())


def test_market_data_manager_records_runtime_metrics() -> None:
    reset_runtime_metrics()
    manager = IbkrMarketDataManager(lanes=("quote",))

    async def ok_job() -> str:
        await asyncio.sleep(0)
        return "ok"

    async def failed_job() -> str:
        raise RuntimeError("boom")

    assert asyncio.run(manager.run_coroutine("quote", "ok", ok_job)) == "ok"
    with pytest.raises(RuntimeError):
        asyncio.run(manager.run_coroutine("quote", "failed", failed_job))

    snapshot = runtime_metrics_snapshot()
    histogram_names = {
        (item["name"], item["labels"].get("lane")) for item in snapshot["histograms"]
    }
    error_counters = [
        item for item in snapshot["counters"] if item["name"] == "ibkr_lane_errors_total"
    ]

    assert ("ibkr_lane_wait_seconds", "quote") in histogram_names
    assert ("ibkr_lane_duration_seconds", "quote") in histogram_names
    assert error_counters == [
        {
            "name": "ibkr_lane_errors_total",
            "value": 1.0,
            "labels": {"code": "IBKR_LANE_ERROR", "lane": "quote"},
        }
    ]
    reset_runtime_metrics()


def test_market_data_manager_clears_active_lane_error_after_recovery() -> None:
    manager = IbkrMarketDataManager(lanes=("options",))

    async def failed_job() -> str:
        raise RuntimeError("temporary option failure")

    async def recovered_job() -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="temporary option failure"):
        asyncio.run(manager.run_coroutine("options", "failed option request", failed_job))

    failed = manager.status()["lanes"]["options"]
    assert failed["last_error"] == "temporary option failure"
    assert failed["error"]["code"] == "IBKR_LANE_ERROR"

    assert (
        asyncio.run(manager.run_coroutine("options", "recovered option request", recovered_job))
        == "ok"
    )

    recovered = manager.status()["lanes"]["options"]
    assert recovered["errors"] == 1
    assert recovered["last_error"] == ""
    assert "error" not in recovered


def test_market_data_manager_recreates_lane_lock_per_event_loop() -> None:
    manager = IbkrMarketDataManager(lanes=("quote",))
    stale_loop = asyncio.new_event_loop()
    stale_lock = asyncio.Lock()
    manager._lane_locks["quote"] = (stale_loop, stale_lock)

    async def job() -> str:
        return "ok"

    try:
        assert asyncio.run(manager.run_coroutine("quote", "loop one", lambda: job())) == "ok"
    finally:
        stale_loop.close()
    current_loop, current_lock = manager._lane_locks["quote"]

    assert current_loop is not stale_loop
    assert current_lock is not stale_lock
    assert manager.status()["lanes"]["quote"]["completed"] == 1


def test_market_data_manager_does_not_block_live_lanes_behind_history() -> None:
    manager = IbkrMarketDataManager(lanes=("quote", "history"), priority_lanes=("quote", "history"))
    order: list[str] = []
    history_started = threading.Event()
    quote_started = threading.Event()

    async def history_job() -> str:
        order.append("start:history")
        history_started.set()
        await asyncio.sleep(0.03)
        order.append("end:history")
        return "history"

    async def quote_job() -> str:
        order.append("start:quote")
        quote_started.set()
        await asyncio.sleep(0)
        order.append("end:quote")
        return "quote"

    async def run_jobs() -> list[str]:
        history_task = asyncio.create_task(manager.run_coroutine("history", "history", history_job))
        await asyncio.to_thread(history_started.wait, 1.0)
        quote_task = asyncio.create_task(manager.run_coroutine("quote", "quote", quote_job))
        await asyncio.to_thread(quote_started.wait, 0.02)
        assert quote_started.is_set()
        return await asyncio.gather(history_task, quote_task)

    assert asyncio.run(run_jobs()) == ["history", "quote"]
    assert order == ["start:history", "start:quote", "end:quote", "end:history"]


def test_market_data_manager_does_not_block_live_chart_behind_history() -> None:
    manager = IbkrMarketDataManager(
        lanes=("chart", "quote", "history"), priority_lanes=("chart", "quote", "history")
    )
    order: list[str] = []
    history_started = threading.Event()
    release_history = threading.Event()
    chart_started = threading.Event()

    async def history_job() -> str:
        order.append("start:history")
        history_started.set()
        while not release_history.is_set():
            await asyncio.sleep(0.01)
        order.append("end:history")
        return "history"

    async def chart_job() -> str:
        order.append("start:chart")
        chart_started.set()
        await asyncio.sleep(0)
        order.append("end:chart")
        return "chart"

    async def run_jobs() -> list[str]:
        history_task = asyncio.create_task(manager.run_coroutine("history", "history", history_job))
        await asyncio.to_thread(history_started.wait, 1.0)
        chart_task = asyncio.create_task(manager.run_coroutine("chart", "chart", chart_job))
        await asyncio.to_thread(chart_started.wait, 1.0)
        assert chart_started.is_set()
        release_history.set()
        return await asyncio.gather(history_task, chart_task)

    assert asyncio.run(run_jobs()) == ["history", "chart"]
    assert order == [
        "start:history",
        "start:chart",
        "end:chart",
        "end:history",
    ]


def test_market_data_manager_history_waits_for_pending_quote() -> None:
    manager = IbkrMarketDataManager(
        lanes=("chart", "quote", "history"), priority_lanes=("chart", "quote", "history")
    )
    order: list[str] = []
    chart_started = threading.Event()
    release_chart = threading.Event()
    quote_started = threading.Event()
    history_started = threading.Event()

    async def chart_job() -> str:
        order.append("start:chart")
        chart_started.set()
        while not release_chart.is_set():
            await asyncio.sleep(0.01)
        order.append("end:chart")
        return "chart"

    async def quote_job() -> str:
        order.append("start:quote")
        quote_started.set()
        await asyncio.sleep(0.03)
        order.append("end:quote")
        return "quote"

    async def history_job() -> str:
        order.append("start:history")
        history_started.set()
        await asyncio.sleep(0)
        order.append("end:history")
        return "history"

    async def run_jobs() -> list[str]:
        chart_task = asyncio.create_task(manager.run_coroutine("chart", "chart", chart_job))
        await asyncio.to_thread(chart_started.wait, 1.0)
        quote_task = asyncio.create_task(manager.run_coroutine("quote", "quote", quote_job))
        await asyncio.sleep(0)
        history_task = asyncio.create_task(manager.run_coroutine("history", "history", history_job))
        await asyncio.sleep(0.03)
        assert not quote_started.is_set()
        assert not history_started.is_set()
        release_chart.set()
        await asyncio.to_thread(quote_started.wait, 1.0)
        await asyncio.sleep(0.005)
        assert not history_started.is_set()
        return await asyncio.gather(chart_task, quote_task, history_task)

    assert asyncio.run(run_jobs()) == ["chart", "quote", "history"]
    assert order == [
        "start:chart",
        "end:chart",
        "start:quote",
        "end:quote",
        "start:history",
        "end:history",
    ]


def test_market_data_manager_reports_pending_lane_age() -> None:
    manager = IbkrMarketDataManager(lanes=("chart", "history"), priority_lanes=("chart", "history"))
    chart_started = threading.Event()
    release_chart = threading.Event()

    async def chart_job() -> str:
        chart_started.set()
        while not release_chart.is_set():
            await asyncio.sleep(0.01)
        return "chart"

    async def history_job() -> str:
        return "history"

    async def run_jobs() -> None:
        chart_task = asyncio.create_task(manager.run_coroutine("chart", "chart", chart_job))
        await asyncio.to_thread(chart_started.wait, 1.0)
        history_task = asyncio.create_task(manager.run_coroutine("history", "history", history_job))
        await asyncio.sleep(0.02)
        status = manager.status()["lanes"]["history"]
        assert status["pending"] == 1
        assert status["pending_since"]
        assert status["oldest_pending_seconds"] >= 0
        release_chart.set()
        await asyncio.gather(chart_task, history_task)

    asyncio.run(run_jobs())
    status = manager.status()["lanes"]["history"]
    assert status["pending"] == 0
    assert status["pending_since"] == ""
    assert status["oldest_pending_seconds"] is None


def test_market_data_manager_default_prioritizes_live_chart_and_quotes() -> None:
    manager = IbkrMarketDataManager()

    assert manager._priority_lanes == ("chart", "quote", "history")
    assert manager._lane_priority == {"chart": 0, "quote": 1, "history": 2}
    assert manager.status()["exclusive_groups"] == {}
    assert manager.status()["pacing"]["intervals"]["chart"] == 0.0
    assert manager.status()["pacing"]["intervals"]["quote"] == 0.0
    assert (
        manager.status()["pacing"]["intervals"]["gex"]
        > manager.status()["pacing"]["intervals"]["history"]
    )


def test_market_data_manager_exposes_and_clears_running_timeout() -> None:
    manager = IbkrMarketDataManager(lanes=("gex",), priority_lanes=())
    started = threading.Event()
    release = threading.Event()

    async def job() -> str:
        started.set()
        await asyncio.to_thread(release.wait, 1.0)
        return "ok"

    async def run_job() -> tuple[dict[str, object], dict[str, object]]:
        task = asyncio.create_task(manager.run_coroutine("gex", "manual refresh", job, timeout=1.0))
        assert await asyncio.to_thread(started.wait, 1.0)
        running = manager.status()["lanes"]["gex"]
        release.set()
        assert await task == "ok"
        return running, manager.status()["lanes"]["gex"]

    running, completed = asyncio.run(run_job())

    assert running["running"] is True
    assert 0 < float(running["running_timeout_seconds"]) <= 1.0
    assert completed["running"] is False
    assert completed["running_timeout_seconds"] is None


def test_market_data_manager_timeout_retains_lane_until_sync_work_finishes() -> None:
    manager = IbkrMarketDataManager(
        lanes=("gex",),
        priority_lanes=(),
        pacing_intervals={"gex": 0.0},
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def physical_job() -> str:
        started.set()
        assert release.wait(1.0)
        finished.set()
        return "late"

    async def run_probe() -> None:
        task = asyncio.create_task(
            manager.run_sync(
                "gex",
                "bounded physical request",
                physical_job,
                timeout=0.05,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        await asyncio.sleep(0.15)

        assert task.done() is False
        running = manager.status()["lanes"]["gex"]
        assert running["running"] is True

        release.set()
        with pytest.raises(TimeoutError):
            await task

    asyncio.run(run_probe())

    assert finished.is_set()
    completed = manager.status()["lanes"]["gex"]
    assert completed["running"] is False
    assert completed["errors"] == 1
    assert completed["error"]["code"] == "IBKR_LANE_TIMEOUT"


def test_market_data_manager_cancellation_waits_for_physical_work() -> None:
    manager = IbkrMarketDataManager(
        lanes=("gex",),
        priority_lanes=(),
        pacing_intervals={"gex": 0.0},
    )
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def physical_job() -> str:
        started.set()
        assert release.wait(1.0)
        finished.set()
        return "settled"

    async def run_probe() -> None:
        task = asyncio.create_task(
            manager.run_sync(
                "gex",
                "cancelled physical request",
                physical_job,
                timeout=1.0,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False
        assert manager.status()["lanes"]["gex"]["running"] is True

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_probe())

    assert finished.is_set()
    assert manager.status()["lanes"]["gex"]["running"] is False


def test_market_data_manager_paces_lower_priority_ibkr_requests() -> None:
    manager = IbkrMarketDataManager(
        lanes=("chart", "options", "gex"),
        priority_lanes=("chart",),
        pacing_intervals={"options": 0.03, "gex": 0.03},
    )
    starts: list[tuple[str, datetime]] = []

    async def job(name: str) -> str:
        starts.append((name, datetime.now(tz=UTC)))
        await asyncio.sleep(0)
        return name

    async def run_jobs() -> list[str]:
        return await asyncio.gather(
            manager.run_coroutine("options", "option request", lambda: job("options")),
            manager.run_coroutine("gex", "gex request", lambda: job("gex")),
        )

    assert sorted(asyncio.run(run_jobs())) == ["gex", "options"]
    assert len(starts) == 2
    elapsed = abs((starts[1][1] - starts[0][1]).total_seconds())
    assert elapsed >= 0.025


def test_market_data_manager_does_not_pace_live_chart_lane() -> None:
    manager = IbkrMarketDataManager(
        lanes=("chart", "options"),
        priority_lanes=("chart",),
        pacing_intervals={"options": 0.08},
    )
    starts: list[tuple[str, datetime]] = []
    option_started = threading.Event()

    async def option_job() -> str:
        starts.append(("options", datetime.now(tz=UTC)))
        option_started.set()
        await asyncio.sleep(0.05)
        return "options"

    async def chart_job() -> str:
        starts.append(("chart", datetime.now(tz=UTC)))
        return "chart"

    async def run_jobs() -> list[str]:
        option_task = asyncio.create_task(
            manager.run_coroutine("options", "option request", option_job)
        )
        await asyncio.to_thread(option_started.wait, 1.0)
        chart_task = asyncio.create_task(manager.run_coroutine("chart", "chart request", chart_job))
        return await asyncio.gather(option_task, chart_task)

    assert asyncio.run(run_jobs()) == ["options", "chart"]
    started = dict(starts)
    assert (started["chart"] - started["options"]).total_seconds() < 0.08


def test_market_data_manager_blocking_timeout_settles_owner_loop_task() -> None:
    manager = IbkrMarketDataManager(lanes=("options",))
    started = threading.Event()
    cancelled = threading.Event()

    async def hanging_job() -> str:
        started.set()
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late"

    with pytest.raises(TimeoutError, match="IBKR options lane timed out"):
        manager.run_coroutine_blocking("options", "hung option quote", hanging_job, timeout=0.05)

    assert started.wait(1.0)
    assert cancelled.wait(1.0)
    status = manager.status()["lanes"]["options"]
    assert status["running"] is False
    assert status["cancelled"] == 0
    assert status["errors"] == 1
    assert status["error"]["code"] == "IBKR_LANE_TIMEOUT"


def test_market_data_manager_timeout_covers_exclusive_group_wait() -> None:
    manager = IbkrMarketDataManager(
        lanes=("chart", "history"),
        priority_lanes=("chart", "history"),
        exclusive_groups={"historical": ("chart", "history")},
    )
    history_started = threading.Event()
    release_history = threading.Event()

    async def run_probe() -> None:
        async def history_job() -> str:
            history_started.set()
            while not release_history.is_set():
                await asyncio.sleep(0.01)
            return "history"

        async def chart_job() -> str:
            return "chart"

        history_task = asyncio.create_task(
            manager.run_coroutine("history", "held history", history_job)
        )
        assert await asyncio.to_thread(history_started.wait, 1.0)
        with pytest.raises(TimeoutError):
            await manager.run_coroutine("chart", "blocked chart reconnect", chart_job, timeout=0.05)
        release_history.set()
        assert await history_task == "history"

    asyncio.run(run_probe())

    status = manager.status()["lanes"]["chart"]
    assert status["pending"] == 0
    assert status["running"] is False
    assert status["errors"] == 1
    assert status["error"]["code"] == "IBKR_LANE_TIMEOUT"


def test_runtime_status_reports_manager_lane_as_async_busy() -> None:
    started = threading.Event()
    release = threading.Event()

    async def job() -> str:
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "done"

    async def run_probe() -> dict[str, object]:
        task = asyncio.create_task(
            ibkr_market_data_manager.run_coroutine(
                "history",
                "status probe",
                lambda: job(),
            )
        )
        try:
            await asyncio.to_thread(started.wait, 1.0)
            status = ibkr_quotes.runtime_status()
        finally:
            release.set()
        assert await task == "done"
        return status

    status = asyncio.run(run_probe())

    assert status["history_lock_busy"] is True
    assert not {
        "history_connected_async",
        "history_lock_busy_async",
        "history_lock_busy_sync",
        "chart_connected_async",
        "chart_streams_async",
        "chart_lock_busy_async",
        "quote_connected_async",
        "quote_connected_sync",
        "quote_lock_busy_async",
        "quote_lock_busy_sync",
    }.intersection(status)
    assert status["history_request"] == "status probe"
    assert status["market_data_manager"]["lanes"]["history"]["running"] is True


def test_runtime_status_exposes_exact_connection_progress() -> None:
    runtime = _IBKR_RUNTIME
    runtime.set_connection_in_progress("quote", True)
    try:
        status = ibkr_quotes.runtime_status()
    finally:
        runtime.set_connection_in_progress("quote", False)

    assert status["connection_in_progress"] == {
        "history": False,
        "chart": False,
        "quote": True,
        "option_quote": False,
    }


def _bar(index: int, close: float, *, source: str = "ibkr:db-cache") -> Bar:
    return Bar(
        symbol="ES",
        ts=datetime(2026, 1, 1, 9, 30, tzinfo=UTC) + timedelta(minutes=index),
        open=close - 0.25,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=1000,
        timeframe="5m",
        source=source,
    )


def test_history_loaders_never_create_ibkr_feed(monkeypatch) -> None:
    cached = [_bar(index, 100 + index) for index in range(40)]

    class FakeStore:
        initialize_calls = 0

        def initialize(self) -> None:
            self.initialize_calls += 1

    store = FakeStore()
    monkeypatch.setattr(loader_patchable, "read_provider_history", lambda *_args, **_kwargs: cached)
    monkeypatch.setattr(
        ibkr_live,
        "ibkr_keep_requested_cache_window",
        lambda _store, _interval, _range, bars, _warnings: list(bars),
    )
    assert not hasattr(ibkr_loader, "create_ibkr_bar_feed")
    assert not hasattr(ibkr_loader, "IbkrBarFeed")

    instrument = ibkr_identity()
    adapter = get_provider("ibkr")
    sync_bars, sync_warning = ibkr_loader.load_ibkr_bars(
        instrument,
        "5m",
        "1mo",
        1.0,
        live_refresh=True,
        store=store,
        adapter=adapter,
    )
    async_bars, async_warning = asyncio.run(
        ibkr_loader.async_load_ibkr_bars(
            instrument,
            "5m",
            "1mo",
            1.0,
            live_refresh=True,
            store=store,
            adapter=adapter,
        )
    )

    assert sync_bars[-1].source == "ibkr:db-cache"
    assert async_bars[-1].source == "ibkr:db-cache"
    assert sync_warning == ""
    assert async_warning == ""
    assert store.initialize_calls == 0


def test_chart_stream_selects_canonical_async_storage_route() -> None:
    stock = ibkr_stock_payload("SPY")
    assert (
        ibkr_loader._select_ibkr_live_bar_storage_route_async(ibkr_identity())
        is ibkr_loader._write_ibkr_futures_continuous_bars_async
    )
    assert (
        ibkr_loader._select_ibkr_live_bar_storage_route_async(stock)
        is ibkr_loader._write_ibkr_provider_live_bars_async
    )


def test_normalize_ibkr_lane_error_maps_timeout() -> None:
    from aef_terminal.data.ibkr.manager import normalize_ibkr_lane_error

    payload = normalize_ibkr_lane_error(TimeoutError("history timed out"))
    assert payload["code"] == "IBKR_LANE_TIMEOUT"
    assert payload["category"] == "broker"
    assert payload["retryable"] is True


def test_market_data_manager_reports_structured_lane_error() -> None:
    manager = IbkrMarketDataManager(lanes=("history",))

    async def failing_job() -> str:
        raise RuntimeError("gateway unavailable")

    async def run() -> dict[str, object]:
        with pytest.raises(RuntimeError, match="gateway unavailable"):
            await manager.run_coroutine("history", "probe", failing_job)
        return manager.status()["lanes"]["history"]

    lane = asyncio.run(run())
    assert lane["errors"] == 1
    assert lane["error"]["code"] == "IBKR_LANE_ERROR"
    assert lane["error"]["category"] == "broker"
    assert "gateway unavailable" in lane["error"]["message"]
