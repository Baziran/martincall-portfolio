from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.runtime import storage_deadlines
from aef_terminal.ui.gex_projection import project_gex_history_for_chart
from aef_terminal.ui.services import gex_stream_ws
from tests.provider_payloads import ibkr_future_payload


ES_INSTRUMENT_ID = qualified_instrument_id(ibkr_future_payload("ES"))


def _canonical_comparison_scope(
    capture_mode: str,
    *,
    strike_count: int = 21,
    futures_options: bool = True,
) -> dict:
    strike_ladder = [7500.0 + index * 5.0 for index in range(strike_count)]
    return {
        "capture_mode": capture_mode,
        "strike_count": strike_count,
        "strike_ladder": strike_ladder,
        "contract_con_ids": list(range(1, strike_count * 2 + 1)),
        "expiries": ["20260724"],
        "futures_options": futures_options,
        "series": [
            {
                "expiry": "20260724",
                "trading_class": "ES",
                "exchange": "CME",
                "multiplier": 50.0,
            }
        ],
        "risk_free_rate": 0.04,
        "dividend_yield": 0.0,
        "market_data_entitlement": "live",
    }


def _canonical_level(price: float = 7575.0, *, spot: float = 7570.0) -> dict:
    return {
        "price": price,
        "kind": "CALL_WALL",
        "kind_class": "call",
        "strength": 1.0,
        "power_class": "EXTREME",
        "selection_rank": 1,
        "net_gex": 800_000.0,
        "call_gex": 1_000_000.0,
        "put_gex": -200_000.0,
        "abs_gex": 1_200_000.0,
        "abs_flow_1pt": 1_200_000.0 / (spot * 0.01),
        "distance_from_spot": price - spot,
        "zone_half_width": 0.25,
        "spot_side": "above" if price > spot else "below" if price < spot else "inside",
        "option_volume_context": {
            "current": {
                "call_volume": None,
                "put_volume": None,
                "total_volume": None,
                "call_oi": None,
                "put_oi": None,
                "total_oi": None,
                "turnover": None,
                "rank": None,
            },
            "event": None,
        },
    }


class _WebSocket:
    def __init__(self) -> None:
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True


def _deps() -> gex_stream_ws.GexStreamWsDeps:
    async def apply_settings() -> dict:
        return {}

    return gex_stream_ws.GexStreamWsDeps(
        apply_provider_runtime_settings_async=apply_settings,
        server_sleeping=lambda: True,
        store_factory=object,
        websocket_heartbeat_seconds=30.0,
    )


def test_gex_stream_producer_is_shared_and_stops_only_after_last_consumer(monkeypatch) -> None:
    stops: list[str] = []
    loads: list[str] = []
    history_loads: list[str] = []

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, instrument, **_kwargs):
            loads.append(instrument["symbol"])
            return {
                "ok": True,
                "spot": 7570.0,
                "levels": [],
                "live": {"frame_seq": len(loads)},
            }

        async def async_stop_live_gex(self, instrument) -> None:
            stops.append(instrument["symbol"])

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument={"symbol": "ES"},
        instrument_key="ES",
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "lookup_runtime_instrument",
        lambda instrument_id: {"symbol": "ES"} if instrument_id == ES_INSTRUMENT_ID else None,
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(
        gex_stream_ws, "send_stream_json", lambda *_args, **_kwargs: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "get_gex_history_series",
        lambda asset, **_kwargs: history_loads.append(asset) or [],
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "read_gex_snapshot_rows",
        lambda *_args, **_kwargs: [],
    )

    async def scenario() -> None:
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        deps = _deps()
        deps = gex_stream_ws.GexStreamWsDeps(
            apply_provider_runtime_settings_async=deps.apply_provider_runtime_settings_async,
            server_sleeping=lambda: False,
            store_factory=deps.store_factory,
            websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
        )
        first = gex_stream_ws._acquire_gex_stream_producer(
            instrument_id=ES_INSTRUMENT_ID,
            provider_symbol="ES",
            route=route,
            store=object(),
            deps=deps,
        )
        second = gex_stream_ws._acquire_gex_stream_producer(
            instrument_id=ES_INSTRUMENT_ID,
            provider_symbol="ES",
            route=route,
            store=object(),
            deps=deps,
        )
        assert first is second
        assert first.consumers == 2
        for _ in range(50):
            if first.history_ready and loads:
                break
            await asyncio.sleep(0.001)
        assert loads == ["ES"]
        assert history_loads == ["ES"]
        await gex_stream_ws._release_gex_stream_producer(first)
        assert gex_stream_ws._GEX_STREAM_PRODUCERS == {(ES_INSTRUMENT_ID, "route-v1"): second}
        assert stops == []
        await gex_stream_ws._release_gex_stream_producer(second)
        assert gex_stream_ws._GEX_STREAM_PRODUCERS == {}

    asyncio.run(scenario())

    assert stops == ["ES"]


def test_gex_stream_reconnect_during_cleanup_does_not_overlap_live_generations() -> None:
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()
    stops: list[str] = []

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {"ok": True, "spot": 7570.0, "levels": [], "live": {"frame_seq": 1}}

        async def async_stop_live_gex(self, instrument) -> None:
            stops.append(instrument["symbol"])
            stop_started.set()
            await allow_stop.wait()

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument={"symbol": "ES"},
        instrument_key="ES",
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    async def scenario() -> None:
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        deps = _deps()
        first = gex_stream_ws._acquire_gex_stream_producer(
            instrument_id=ES_INSTRUMENT_ID,
            provider_symbol="ES",
            route=route,
            store=object(),
            deps=deps,
        )
        release_task = asyncio.create_task(gex_stream_ws._release_gex_stream_producer(first))
        await asyncio.wait_for(stop_started.wait(), timeout=0.5)

        reconnected = gex_stream_ws._acquire_gex_stream_producer(
            instrument_id=ES_INSTRUMENT_ID,
            provider_symbol="ES",
            route=route,
            store=object(),
            deps=deps,
        )
        assert reconnected is first
        assert first.closing is True

        allow_stop.set()
        await release_task
        assert first.closing is False
        assert first.consumers == 1
        assert first.task is not None and not first.task.done()
        assert gex_stream_ws._GEX_STREAM_PRODUCERS[first.key] is first

        await gex_stream_ws._release_gex_stream_producer(reconnected)

    asyncio.run(scenario())

    assert stops == ["ES", "ES"]


def test_gex_stream_release_defers_cancellation_through_provider_stop() -> None:
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {"ok": True, "spot": 7570.0, "levels": [], "live": {"frame_seq": 1}}

        async def async_stop_live_gex(self, _instrument) -> None:
            stop_started.set()
            await allow_stop.wait()

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument={"symbol": "ES"},
        instrument_key="ES",
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    async def scenario() -> None:
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        producer = gex_stream_ws._acquire_gex_stream_producer(
            instrument_id=ES_INSTRUMENT_ID,
            provider_symbol="ES",
            route=route,
            store=object(),
            deps=_deps(),
        )
        release_task = asyncio.create_task(gex_stream_ws._release_gex_stream_producer(producer))
        try:
            await asyncio.wait_for(stop_started.wait(), timeout=0.5)
            release_task.cancel()
            await asyncio.sleep(0)

            assert not release_task.done()
            assert producer.closing is True
        finally:
            allow_stop.set()

        with pytest.raises(asyncio.CancelledError):
            await release_task
        assert producer.closing is False
        assert producer.task is None
        assert gex_stream_ws._GEX_STREAM_PRODUCERS == {}

    asyncio.run(scenario())


def test_gex_stream_producer_preserves_data_owned_persistence_status(monkeypatch) -> None:
    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {
                "ok": True,
                "source": "gex:ibkr-live",
                "spot": 7570.0,
                "levels": [_canonical_level()],
                "request_meta": {"refresh_mode": "live", "strike_count": 9},
                "raw": {"contracts": [{"con_id": 1}]},
                "age_minutes": 0.1,
                "stale_minutes": 5,
                "call_wall": 7575.0,
                "put_wall": 7565.0,
                "net_gex": 800_000.0,
                "option_activity": {"scope": "selected_chain"},
                "live": {
                    "frame_seq": 12,
                    "publishable": True,
                    "persisted_bucket": "2026-07-10T14:00:00+00:00",
                    "persist_interval_seconds": 300,
                    "persistence_ok": True,
                    "persistence_error": "",
                },
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument={"symbol": "ES"},
        instrument_key="ES",
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store="store",
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "lookup_runtime_instrument",
        lambda instrument_id: {"symbol": "ES"} if instrument_id == ES_INSTRUMENT_ID else None,
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 0)

    async def scenario() -> None:
        task = asyncio.create_task(
            gex_stream_ws._run_gex_stream_producer(
                producer,
                gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: False,
                    store_factory=object,
                    websocket_heartbeat_seconds=30.0,
                ),
            )
        )
        try:
            for _ in range(20):
                if producer.frame is not None:
                    break
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert producer.frame["live"]["persisted_bucket"] == "2026-07-10T14:00:00+00:00"
    assert producer.frame["live"]["persist_interval_seconds"] == 300
    assert producer.frame["live"]["persistence_ok"] is True
    assert producer.frame["live"]["persistence_error"] == ""
    assert (
        not {
            "request_meta",
            "raw",
            "age_minutes",
            "stale_minutes",
            "call_wall",
            "put_wall",
            "net_gex",
            "option_activity",
        }
        & producer.frame.keys()
    )
    assert "profile" not in producer.frame
    assert "nearest" not in producer.frame


def test_gex_stream_keeps_realtime_frame_when_data_layer_reports_persistence_failure(
    monkeypatch,
) -> None:
    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {
                "ok": True,
                "source": "gex:ibkr-live",
                "spot": 7570.0,
                "levels": [_canonical_level()],
                "live": {
                    "frame_seq": 13,
                    "publishable": True,
                    "persistence_ok": False,
                    "persistence_error": "database unavailable",
                },
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument={"symbol": "ES"},
        instrument_key="ES",
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store="store",
    )
    monkeypatch.setattr(
        gex_stream_ws, "lookup_runtime_instrument", lambda _symbol: {"symbol": "ES"}
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 0)

    async def scenario() -> None:
        task = asyncio.create_task(
            gex_stream_ws._run_gex_stream_producer(
                producer,
                gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: False,
                    store_factory=object,
                    websocket_heartbeat_seconds=30.0,
                ),
            )
        )
        try:
            for _ in range(20):
                if producer.frame is not None:
                    break
                await asyncio.sleep(0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert producer.frame is not None
    assert producer.frame["live"]["persistence_ok"] is False
    assert producer.frame["live"]["persistence_error"] == "database unavailable"
    assert producer.error == ""
    assert "raw" not in producer.frame


def test_gex_stream_sends_live_frame_while_history_is_loading_and_cancels_cleanly(
    monkeypatch,
) -> None:
    messages: list[dict] = []
    history_deadlines: list[tuple[int, int] | None] = []
    projection_calls: list[list[dict]] = []
    frame_sent = asyncio.Event()
    history_started = threading.Event()
    history_release = threading.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {
                "ok": True,
                "spot": 7570.0,
                "levels": [_canonical_level()],
                "live": {"frame_seq": 1},
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    def slow_history(*_args, **_kwargs):
        history_deadlines.append(storage_deadlines.POSTGRES_OPERATION_TIMEOUTS.get())
        history_started.set()
        history_release.wait(timeout=2.0)
        return []

    async def capture(_websocket, payload, _stream_name):
        messages.append(payload)
        if payload.get("type") == "gex_frame":
            frame_sent.set()

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.01)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 1)
    monkeypatch.setattr(
        gex_stream_ws, "lookup_runtime_instrument", lambda _instrument_id: instrument
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "read_gex_snapshot_rows", slow_history)
    monkeypatch.setattr(gex_stream_ws, "get_gex_history_series", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        gex_stream_ws,
        "project_gex_history_for_chart",
        lambda history: projection_calls.append(history) or [],
    )
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        nonlocal sleeping
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        deps = gex_stream_ws.GexStreamWsDeps(
            apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
            server_sleeping=lambda: sleeping,
            store_factory=object,
            websocket_heartbeat_seconds=30.0,
        )
        reconnected = None
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=deps,
            )
        )
        try:
            await asyncio.wait_for(frame_sent.wait(), timeout=0.5)
            assert history_started.is_set()
            frame = next(message for message in messages if message.get("type") == "gex_frame")
            assert frame["levels"] == [_canonical_level()]
            assert "history" not in frame
            assert frame["history_status"] == {
                "status": "loading",
                "stale": False,
                "message": "",
            }
            assert "degraded" not in frame

            sleeping = True
            producer = gex_stream_ws._GEX_STREAM_PRODUCERS[(ES_INSTRUMENT_ID, "route-v1")]
            for _ in range(500):
                if producer.closing:
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("GEX producer did not enter physical cleanup")
            assert not stream_task.done()
            reconnected = gex_stream_ws._acquire_gex_stream_producer(
                instrument_id=ES_INSTRUMENT_ID,
                provider_symbol="ES",
                route=route,
                store=object(),
                deps=deps,
            )
            assert reconnected is producer
            history_release.set()
            await asyncio.wait_for(stream_task, timeout=0.5)
        finally:
            history_release.set()
            if not stream_task.done():
                stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
            if reconnected is not None and reconnected.consumers:
                await gex_stream_ws._release_gex_stream_producer(reconnected)

    asyncio.run(scenario())
    assert history_deadlines == [
        (
            gex_stream_ws._GEX_HISTORY_STATEMENT_TIMEOUT_MS,
            gex_stream_ws._GEX_HISTORY_LOCK_TIMEOUT_MS,
        )
    ]
    assert projection_calls == []


@pytest.mark.parametrize(
    ("history_result", "history_error", "expected_status", "expected_degraded"),
    [
        ([], None, "ok", False),
        (None, RuntimeError("history database unavailable"), "error", True),
    ],
)
def test_gex_stream_reports_completed_history_status_without_hiding_live_frame(
    monkeypatch,
    history_result,
    history_error,
    expected_status,
    expected_degraded,
) -> None:
    messages: list[dict] = []
    terminal_status_sent = asyncio.Event()
    live_frame_sent = asyncio.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")
    frame_seq = 0

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            nonlocal frame_seq
            frame_seq += 1
            return {
                "ok": True,
                "spot": 7570.0,
                "levels": [_canonical_level()],
                "live": {"frame_seq": frame_seq},
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    def load_history(*_args, **_kwargs):
        if history_error is not None:
            raise history_error
        return history_result

    async def capture(_websocket, payload, _stream_name):
        messages.append(payload)
        if payload.get("type") == "gex_frame":
            live_frame_sent.set()
        if (
            payload.get("type") in {"gex_history_snapshot", "gex_history_delta"}
            and payload.get("history_status", {}).get("status") == expected_status
        ):
            terminal_status_sent.set()

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.01)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 1)
    monkeypatch.setattr(
        gex_stream_ws, "lookup_runtime_instrument", lambda _instrument_id: instrument
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(
        gex_stream_ws,
        "read_gex_snapshot_rows",
        load_history,
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "get_gex_history_series",
        lambda *_args, **_kwargs: history_result or [],
    )
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        nonlocal sleeping
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: sleeping,
                    store_factory=object,
                    websocket_heartbeat_seconds=30.0,
                ),
            )
        )
        await asyncio.wait_for(terminal_status_sent.wait(), timeout=0.5)
        await asyncio.wait_for(live_frame_sent.wait(), timeout=0.5)
        sleeping = True
        await asyncio.wait_for(stream_task, timeout=0.5)

    asyncio.run(scenario())

    assert any(
        message.get("type") in {"gex_history_snapshot", "gex_history_delta"} for message in messages
    )
    history_message = next(
        message
        for message in messages
        if message.get("type") in {"gex_history_snapshot", "gex_history_delta"}
        and message.get("history_status", {}).get("status") == expected_status
    )
    frame = next(message for message in messages if message.get("type") == "gex_frame")
    assert frame["levels"] == [_canonical_level()]
    assert history_message["history_status"]["message"] == (
        "history database unavailable" if history_error is not None else ""
    )
    assert (history_message["history_status"]["status"] == "error") is expected_degraded


def test_gex_material_frame_key_ignores_only_acquisition_clock_fields() -> None:
    frame = {
        "ok": True,
        "captured_at": "2026-08-03T06:30:00+00:00",
        "request_seconds": 0.2,
        "spot": 7570.0,
        "levels": [_canonical_level()],
        "live": {
            "frame_seq": 10,
            "last_frame_at": "2026-08-03T06:30:00+00:00",
            "publishable": True,
        },
    }
    replay = {
        **frame,
        "captured_at": "2026-08-03T06:30:02+00:00",
        "request_seconds": 0.4,
        "live": {
            **frame["live"],
            "frame_seq": 11,
            "last_frame_at": "2026-08-03T06:30:02+00:00",
        },
    }

    assert gex_stream_ws._gex_material_frame_key(frame) == gex_stream_ws._gex_material_frame_key(
        replay
    )
    replay["spot"] = 7570.25
    assert gex_stream_ws._gex_material_frame_key(frame) != gex_stream_ws._gex_material_frame_key(
        replay
    )


def test_gex_stream_heartbeats_when_only_live_frame_sequence_changes(monkeypatch) -> None:
    messages: list[dict] = []
    heartbeat_sent = asyncio.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")
    frame_seq = 0

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            nonlocal frame_seq
            frame_seq += 1
            return {
                "ok": True,
                "spot": 7570.0,
                "levels": [],
                "live": {"frame_seq": frame_seq},
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    async def capture(_websocket, payload, _stream_name):
        messages.append(payload)
        if payload.get("type") == "gex_heartbeat":
            heartbeat_sent.set()

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.01)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 0)
    monkeypatch.setattr(
        gex_stream_ws, "lookup_runtime_instrument", lambda _instrument_id: instrument
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        nonlocal sleeping
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: sleeping,
                    store_factory=object,
                    websocket_heartbeat_seconds=0.02,
                ),
            )
        )
        await asyncio.wait_for(heartbeat_sent.wait(), timeout=0.5)
        sleeping = True
        await asyncio.wait_for(stream_task, timeout=0.5)

    asyncio.run(scenario())

    assert [message["type"] for message in messages].count("gex_frame") == 1
    heartbeat = next(message for message in messages if message["type"] == "gex_heartbeat")
    assert heartbeat["frame_seq"] >= 2


def test_gex_stream_preserves_typed_status_and_heartbeats_before_first_frame(
    monkeypatch,
) -> None:
    messages: list[dict] = []
    heartbeat_sent = asyncio.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")
    retry_at = "2026-07-25T12:00:00+00:00"

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {
                "ok": False,
                "status": "unavailable",
                "message": "GEX live option universe rolled over.",
                "error": {
                    "code": "GEX_OPTION_UNIVERSE_UNAVAILABLE",
                    "category": "gex",
                    "retryable": True,
                    "reason": "OPTION_UNIVERSE_ROLLOVER",
                    "diagnostics": {
                        "option_universe_expires_at": "2026-07-25T11:59:59+00:00",
                    },
                    "message": "GEX live option universe rolled over.",
                },
                "backoff": {
                    "active": True,
                    "retry_at": retry_at,
                    "retry_in_seconds": 60.0,
                    "reason": "OPTION_UNIVERSE_ROLLOVER",
                },
                "live": {"enabled": True, "active": False},
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    async def capture(_websocket, payload, _stream_name):
        messages.append(payload)
        if payload.get("type") == "gex_heartbeat" and payload.get("frame_available") is False:
            heartbeat_sent.set()

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.01)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 0)
    monkeypatch.setattr(
        gex_stream_ws,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        nonlocal sleeping
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: sleeping,
                    store_factory=object,
                    websocket_heartbeat_seconds=0.02,
                ),
            )
        )
        await asyncio.wait_for(heartbeat_sent.wait(), timeout=0.5)
        sleeping = True
        await asyncio.wait_for(stream_task, timeout=0.5)

    asyncio.run(scenario())

    status = next(message for message in messages if message["type"] == "gex_status")
    assert status["status"] == "unavailable"
    assert status["error"]["code"] == "GEX_OPTION_UNIVERSE_UNAVAILABLE"
    assert status["reason"] == "OPTION_UNIVERSE_ROLLOVER"
    assert status["diagnostics"] == {
        "option_universe_expires_at": "2026-07-25T11:59:59+00:00",
    }
    assert status["backoff"] == {
        "active": True,
        "retry_at": retry_at,
        "retry_in_seconds": 60.0,
        "reason": "OPTION_UNIVERSE_ROLLOVER",
    }
    heartbeat = next(
        message
        for message in messages
        if message["type"] == "gex_heartbeat" and message.get("frame_available") is False
    )
    assert heartbeat["status_revision"] > 0
    assert not any(message["type"] == "gex_frame" for message in messages)


def test_gex_stream_heartbeat_continues_during_offthread_route_validation(monkeypatch) -> None:
    messages: list[dict] = []
    heartbeat_sent = asyncio.Event()
    route_check_started = threading.Event()
    release_route_check = threading.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")
    lookup_count = 0

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {
                "ok": True,
                "spot": 7570.0,
                "levels": [],
                "live": {"frame_seq": 1},
            }

        async def async_stop_live_gex(self, _instrument) -> None:
            return None

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )

    def lookup(_instrument_id: str) -> dict[str, object]:
        nonlocal lookup_count
        lookup_count += 1
        if lookup_count > 1:
            route_check_started.set()
            release_route_check.wait(timeout=2.0)
        return instrument

    async def capture(_websocket, payload, _stream_name):
        messages.append(payload)
        if payload.get("type") == "gex_heartbeat":
            heartbeat_sent.set()

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.005)
    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_ROUTE_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 0)
    monkeypatch.setattr(gex_stream_ws, "lookup_runtime_instrument", lookup)
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        nonlocal sleeping
        gex_stream_ws._GEX_STREAM_PRODUCERS.clear()
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: sleeping,
                    store_factory=object,
                    websocket_heartbeat_seconds=0.015,
                ),
            )
        )
        try:
            assert await asyncio.to_thread(route_check_started.wait, 0.5)
            await asyncio.wait_for(heartbeat_sent.wait(), timeout=0.5)
        finally:
            release_route_check.set()
            sleeping = True
            await asyncio.wait_for(stream_task, timeout=0.5)

    asyncio.run(scenario())

    assert lookup_count >= 2
    assert any(message.get("type") == "gex_heartbeat" for message in messages)


def test_gex_producer_cancellation_settles_physical_route_lookup(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    instrument = ibkr_future_payload("ES")

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            raise AssertionError("route validation must finish before GEX collection")

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store=object(),
        history_ready=True,
        route_checked_at=0.0,
    )

    def blocking_lookup(_instrument_id: str) -> dict:
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return instrument

    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 0)
    monkeypatch.setattr(gex_stream_ws, "lookup_runtime_instrument", blocking_lookup)
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)

    async def scenario() -> None:
        deps = _deps()
        active_deps = gex_stream_ws.GexStreamWsDeps(
            apply_provider_runtime_settings_async=deps.apply_provider_runtime_settings_async,
            server_sleeping=lambda: False,
            store_factory=deps.store_factory,
            websocket_heartbeat_seconds=deps.websocket_heartbeat_seconds,
        )
        task = asyncio.create_task(gex_stream_ws._run_gex_stream_producer(producer, active_deps))
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


def test_gex_history_update_returns_the_revision_captured_before_send(monkeypatch) -> None:
    sent: list[dict] = []
    route = SimpleNamespace(fingerprint="route-v1")
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store=object(),
        history_revision=3,
        history_ready=True,
        wire_history=[{"captured_at": "2026-07-21T10:00:00+00:00"}],
    )

    async def capture(_websocket, payload, _stream_name):
        sent.append(payload)
        producer.history_revision = 4
        producer.wire_history = [{"captured_at": "2026-07-21T10:05:00+00:00"}]

    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    sent_revision = asyncio.run(
        gex_stream_ws._send_gex_history_update(
            _WebSocket(),
            producer,
            last_history_revision=-1,
        )
    )

    assert sent_revision == 3
    assert sent == [
        {
            "type": "gex_history_snapshot",
            "provider_symbol": "ES",
            "instrument_id": ES_INSTRUMENT_ID,
            "route_fingerprint": "route-v1",
            "history_status": {"status": "ok", "stale": False, "message": ""},
            "history_revision": 3,
            "history": [{"captured_at": "2026-07-21T10:00:00+00:00"}],
        }
    ]


def test_gex_frame_advertises_only_the_history_revision_delivered_to_client(monkeypatch) -> None:
    messages: list[dict] = []
    second_frame_sent = asyncio.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store=object(),
        frame={
            "ok": True,
            "spot": 7570.0,
            "levels": [_canonical_level()],
            "live": {"frame_seq": 1},
        },
        frame_seq=1,
        revision=1,
        history_revision=1,
        history_ready=True,
        wire_history=[{"captured_at": "2026-07-21T10:00:00+00:00"}],
    )

    async def capture(_websocket, payload, _stream_name):
        nonlocal sleeping
        messages.append(payload)
        if payload.get("type") == "gex_history_snapshot" and payload.get("history_revision") == 1:
            producer.frame = {
                **producer.frame,
                "live": {"frame_seq": 2},
            }
            producer.frame_seq = 2
            producer.revision = 2
            producer.history_revision = 2
            producer.wire_history = [{"captured_at": "2026-07-21T10:05:00+00:00"}]
        elif payload.get("type") == "gex_history_snapshot" and payload.get("history_revision") == 2:
            producer.history_revision = 3
            producer.wire_history = [{"captured_at": "2026-07-21T10:10:00+00:00"}]
        elif (
            payload.get("type") == "gex_frame"
            and payload.get("live", {}).get("frame_seq") == 2
            and payload.get("history_revision") == 2
        ):
            sleeping = True
            second_frame_sent.set()

    async def release(_producer) -> None:
        return None

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.001)
    monkeypatch.setattr(
        gex_stream_ws,
        "GEX_STREAM_PUBLICATION_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        gex_stream_ws, "lookup_runtime_instrument", lambda _instrument_id: instrument
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "_acquire_gex_stream_producer", lambda **_kwargs: producer)
    monkeypatch.setattr(gex_stream_ws, "_release_gex_stream_producer", release)
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
                    server_sleeping=lambda: sleeping,
                    store_factory=object,
                    websocket_heartbeat_seconds=30.0,
                ),
            )
        )
        await asyncio.wait_for(second_frame_sent.wait(), timeout=0.5)
        await asyncio.wait_for(stream_task, timeout=0.5)

    asyncio.run(scenario())

    initial_frame_index = next(
        index for index, message in enumerate(messages) if message.get("type") == "gex_frame"
    )
    initial_history_index = next(
        index
        for index, message in enumerate(messages)
        if message.get("type") == "gex_history_snapshot" and message.get("history_revision") == 1
    )
    assert initial_frame_index < initial_history_index
    second_frame = next(
        message
        for message in messages
        if message.get("type") == "gex_frame"
        and message.get("live", {}).get("frame_seq") == 2
        and message.get("history_revision") == 2
    )
    assert second_frame["history_revision"] == 2
    assert producer.history_revision == 3


def test_gex_stream_sends_history_without_live_frame_during_transport_error(
    monkeypatch,
) -> None:
    messages: list[dict] = []
    history_sent = asyncio.Event()
    sleeping = False
    instrument = ibkr_future_payload("ES")

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store=object(),
        transport_status={
            "ok": False,
            "status": "error",
            "message": "broker lane unavailable",
        },
        transport_status_revision=1,
        history_revision=2,
        history_ready=True,
        wire_history=[{"captured_at": "2026-07-21T10:00:00+00:00"}],
    )

    async def capture(_websocket, payload, _stream_name):
        nonlocal sleeping
        messages.append(payload)
        if payload.get("type") == "gex_history_snapshot":
            sleeping = True
            history_sent.set()

    async def release(_producer) -> None:
        return None

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.001)
    monkeypatch.setattr(
        gex_stream_ws,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(
        gex_stream_ws,
        "_acquire_gex_stream_producer",
        lambda **_kwargs: producer,
    )
    monkeypatch.setattr(gex_stream_ws, "_release_gex_stream_producer", release)
    monkeypatch.setattr(gex_stream_ws, "send_stream_json", capture)

    async def scenario() -> None:
        stream_task = asyncio.create_task(
            gex_stream_ws.run_gex_stream(
                _WebSocket(),
                instrument_id=ES_INSTRUMENT_ID,
                expected_route_fingerprint="route-v1",
                deps=gex_stream_ws.GexStreamWsDeps(
                    apply_provider_runtime_settings_async=lambda: asyncio.sleep(
                        0,
                        result={},
                    ),
                    server_sleeping=lambda: sleeping,
                    store_factory=object,
                    websocket_heartbeat_seconds=30.0,
                ),
            )
        )
        await asyncio.wait_for(history_sent.wait(), timeout=0.5)
        await asyncio.wait_for(stream_task, timeout=0.5)

    asyncio.run(scenario())

    assert any(message.get("type") == "gex_status" for message in messages)
    history = next(message for message in messages if message.get("type") == "gex_history_snapshot")
    assert history["history_revision"] == 2
    assert history["history"] == [{"captured_at": "2026-07-21T10:00:00+00:00"}]
    assert not any(message.get("type") == "gex_frame" for message in messages)


def test_gex_producer_keeps_history_out_of_current_frame(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")

    class Adapter:
        capabilities = SimpleNamespace(gex=True)

        async def async_load_live_gex(self, _instrument, **_kwargs):
            return {
                "ok": True,
                "spot": 7570.0,
                "levels": [_canonical_level()],
                "live": {"frame_seq": 1},
            }

    route = SimpleNamespace(
        adapter=Adapter(),
        instrument=instrument,
        instrument_key="ES",
        instrument_id=ES_INSTRUMENT_ID,
        provider="ibkr",
        provider_symbol="ES",
        fingerprint="route-v1",
    )
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=route,
        store=object(),
    )
    deps = gex_stream_ws.GexStreamWsDeps(
        apply_provider_runtime_settings_async=lambda: asyncio.sleep(0, result={}),
        server_sleeping=lambda: False,
        store_factory=object,
        websocket_heartbeat_seconds=30.0,
    )

    async def load_history(_producer):
        row = {
            "captured_at": "2026-07-21T10:00:00+00:00",
            "timestamp_unix_ms": 1,
        }
        return gex_stream_ws._GexHistoryReadModel(
            snapshot_rows=[],
            history=[row],
            wire_history=[row],
        )

    monkeypatch.setattr(gex_stream_ws, "GEX_STREAM_FRAME_SECONDS", 0.001)
    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 1)
    monkeypatch.setattr(
        gex_stream_ws, "lookup_runtime_instrument", lambda _instrument_id: instrument
    )
    monkeypatch.setattr(gex_stream_ws, "route_instrument", lambda _instrument: route)
    monkeypatch.setattr(gex_stream_ws, "_load_gex_history_read_model", load_history)

    async def scenario() -> None:
        task = asyncio.create_task(gex_stream_ws._run_gex_stream_producer(producer, deps))
        try:
            for _ in range(100):
                if producer.frame and producer.history_revision == 1:
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("separate history snapshot was not committed")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert producer.frame_seq == 1
    assert producer.history_revision == 1
    assert producer.history == [
        {
            "captured_at": "2026-07-21T10:00:00+00:00",
            "timestamp_unix_ms": 1,
        }
    ]
    assert "history" not in producer.frame


def test_gex_history_stream_projection_keeps_dense_tail_and_full_level_contract() -> None:
    start = datetime(2026, 7, 18, 12, tzinfo=UTC)
    history: list[dict] = []
    for index in range(49 * 12):
        captured_at = start + timedelta(minutes=index * 5)
        history.append(
            {
                "captured_at": captured_at.isoformat(),
                "timestamp_unix_ms": int(captured_at.timestamp() * 1000),
                "valid_until_unix_ms": int((captured_at + timedelta(hours=1)).timestamp() * 1000),
                "capture_mode": "live",
                "source": "gex:ibkr-live",
                "capture_revision": captured_at.isoformat(),
                "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
                "instrument_id": ES_INSTRUMENT_ID,
                "route_fingerprint": "route-v1",
                "provider_symbol": "ES",
                "comparison_scope": _canonical_comparison_scope("live", strike_count=9),
                "market_data_entitlement": "live",
                "open_interest_as_of": "previous_settlement",
                "frame_complete": True,
                "decision_authoritative": True,
                "spot": 7570.0,
                "gamma_flip": None,
                "global_gamma_regime": "POSITIVE_ESTIMATE",
                "net_gex": 1_000_000.0,
                "option_activity": {"scope": "selected_chain"},
                "option_activity_status": "READY",
                "levels": [
                    {
                        **_canonical_level(7500.0 + index),
                        "option_volume_context": {
                            "current": {
                                "call_volume": 100,
                                "put_volume": 80,
                                "total_volume": 180,
                                "call_oi": 500,
                                "put_oi": 400,
                                "total_oi": 900,
                                "turnover": 0.2,
                                "rank": 1,
                            },
                            "event": None,
                        },
                    }
                ],
            }
        )

    projected = project_gex_history_for_chart(history)

    assert 180 <= len(projected) <= 205
    assert projected[-1]["captured_at"] == history[-1]["captured_at"]
    assert all(
        row["valid_until_unix_ms"] - row["timestamp_unix_ms"] == 5 * 60_000
        for row in projected[:-1]
    )
    assert projected[-1]["valid_until_unix_ms"] - projected[-1]["timestamp_unix_ms"] == 60 * 60_000
    interval_rows = [row for row in projected if row["projection_mode"] == "interval"]
    sample_rows = [row for row in projected if row["projection_mode"] == "sample"]
    newest_ms = projected[-1]["timestamp_unix_ms"]
    assert interval_rows
    assert sample_rows
    assert all(row["projection_bucket_minutes"] is None for row in interval_rows)
    assert all(newest_ms - row["timestamp_unix_ms"] <= 6 * 60 * 60_000 for row in interval_rows)
    assert {row["projection_bucket_minutes"] for row in sample_rows} == {15, 30}
    assert all(newest_ms - row["timestamp_unix_ms"] > 6 * 60 * 60_000 for row in sample_rows)
    assert all(
        row["valid_until_unix_ms"] - row["timestamp_unix_ms"] == 5 * 60_000 for row in sample_rows
    )
    expected_wire_fields = {
        "captured_at",
        "timestamp_unix_ms",
        "capture_mode",
        "source",
        "capture_revision",
        "option_universe_expires_at",
        "market_data_entitlement",
        "open_interest_as_of",
        "comparison_scope",
        "spot",
        "gamma_flip",
        "levels",
        "valid_until_unix_ms",
        "projection_mode",
        "projection_bucket_minutes",
    }
    assert all(set(row) == expected_wire_fields for row in projected)
    context = projected[-1]["levels"][0]["option_volume_context"]
    assert context == history[-1]["levels"][0]["option_volume_context"]
    previous_projection = project_gex_history_for_chart(history[:-1])
    append_delta = gex_stream_ws._gex_history_delta(
        previous_projection,
        projected,
        base_revision=1,
        revision=2,
    )
    assert len(append_delta["upserts"]) <= 5
    assert len(append_delta["removed_history_keys"]) <= 3


def test_gex_history_projection_rejects_corrupt_rows_instead_of_hiding_them() -> None:
    with pytest.raises(ValueError, match="mapping rows"):
        project_gex_history_for_chart([None])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="positive timestamp"):
        project_gex_history_for_chart(
            [{"captured_at": "2026-07-21T10:00:00+00:00", "timestamp_unix_ms": "1"}]
        )

    row = {
        "captured_at": "2026-07-21T10:00:00+00:00",
        "timestamp_unix_ms": 1784628000000,
        "valid_until_unix_ms": 1784628300000,
        "source": "gex:ibkr",
        "capture_mode": "request",
        "capture_revision": "2026-07-21T10:00:00+00:00",
        "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
        "market_data_entitlement": "live",
        "open_interest_as_of": "previous_settlement",
        "spot": 5000.0,
        "gamma_flip": None,
        "comparison_scope": _canonical_comparison_scope("request", futures_options=False),
        "levels": [None],
    }
    with pytest.raises(ValueError, match="mapping levels"):
        project_gex_history_for_chart([row])  # type: ignore[list-item]


def test_gex_history_delta_is_revisioned_and_removes_expired_rows() -> None:
    previous = [
        {
            "captured_at": "2026-07-21T10:00:00+00:00",
            "timestamp_unix_ms": 1,
            "source": "gex:ibkr",
            "capture_mode": "request",
        },
        {
            "captured_at": "2026-07-21T10:05:00+00:00",
            "timestamp_unix_ms": 2,
            "source": "gex:ibkr",
            "capture_mode": "request",
        },
    ]
    current = [
        {**previous[1], "spot": 10},
        {
            "captured_at": "2026-07-21T10:10:00+00:00",
            "timestamp_unix_ms": 3,
            "source": "gex:ibkr",
            "capture_mode": "request",
        },
    ]

    delta = gex_stream_ws._gex_history_delta(
        previous,
        current,
        base_revision=7,
        revision=8,
    )

    assert delta["base_history_revision"] == 7
    assert delta["history_revision"] == 8
    assert delta["removed_history_keys"] == ['["gex:ibkr","request","2026-07-21T10:00:00+00:00"]']
    assert delta["upserts"] == current


def test_gex_history_delta_keeps_request_and_live_rows_at_same_timestamp() -> None:
    captured_at = "2026-07-21T10:00:00+00:00"
    request = {
        "captured_at": captured_at,
        "timestamp_unix_ms": 1,
        "source": "gex:ibkr",
        "capture_mode": "request",
        "spot": 10,
    }
    live = {
        **request,
        "source": "gex:ibkr-live",
        "capture_mode": "live",
        "spot": 11,
    }

    delta = gex_stream_ws._gex_history_delta(
        [request, live],
        [request, {**live, "spot": 12}],
        base_revision=1,
        revision=2,
    )

    assert delta["removed_history_keys"] == []
    assert delta["upserts"] == [{**live, "spot": 12}]


def test_gex_stream_history_bootstraps_once_then_reads_only_overlapping_tail(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    request_source, live_source = gex_stream_ws.GEX_CHART_HISTORY_SOURCES
    old_row = {
        "captured_at": (now - timedelta(minutes=10)).isoformat(),
        "source": request_source,
        "value": "old",
    }
    latest_row = {
        "captured_at": (now - timedelta(minutes=5)).isoformat(),
        "source": live_source,
        "value": "initial",
    }
    replacement = {**latest_row, "value": "updated"}
    new_row = {
        "captured_at": now.isoformat(),
        "source": live_source,
        "value": "new",
    }
    reads: list[dict] = []

    def read_rows(start, end, **kwargs):
        reads.append({"start": start, "end": end, **kwargs})
        return [old_row, latest_row] if len(reads) == 1 else [replacement, new_row]

    monkeypatch.setattr(gex_stream_ws, "GEX_CHART_HISTORY_HOURS", 1)
    monkeypatch.setattr(gex_stream_ws, "read_gex_snapshot_rows", read_rows)
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=SimpleNamespace(fingerprint="route-v1"),
        store=object(),
    )

    bootstrap = gex_stream_ws._read_gex_stream_snapshot_rows(
        producer,
        now_utc=now,
    )
    producer.history_snapshot_rows = bootstrap
    incremental = gex_stream_ws._read_gex_stream_snapshot_rows(
        producer,
        now_utc=now,
    )

    assert reads[0]["start"] == now - timedelta(hours=1)
    assert reads[1]["start"] == now - timedelta(minutes=10)
    assert reads[1]["limit"] < reads[0]["limit"]
    assert incremental == [old_row, replacement, new_row]


def test_gex_stream_history_skips_projection_when_incremental_tail_is_unchanged(
    monkeypatch,
) -> None:
    now = datetime.now(tz=UTC)
    source = gex_stream_ws.GEX_CHART_HISTORY_SOURCES[0]
    snapshot_row = {
        "captured_at": (now - timedelta(minutes=5)).isoformat(),
        "source": source,
    }
    history_row = {"captured_at": snapshot_row["captured_at"], "source": source}
    producer = gex_stream_ws._GexStreamProducer(
        key=(ES_INSTRUMENT_ID, "route-v1"),
        instrument_id=ES_INSTRUMENT_ID,
        provider_symbol="ES",
        route=SimpleNamespace(fingerprint="route-v1"),
        store=object(),
        history=[history_row],
        wire_history=[history_row],
        history_snapshot_rows=[snapshot_row],
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "read_gex_snapshot_rows",
        lambda *_args, **_kwargs: [dict(snapshot_row)],
    )
    monkeypatch.setattr(
        gex_stream_ws,
        "get_gex_history_series",
        lambda *_args, **_kwargs: pytest.fail("unchanged tail must not rebuild history"),
    )

    result = asyncio.run(gex_stream_ws._load_gex_history_read_model(producer))

    assert result.snapshot_rows == [snapshot_row]
    assert result.history is producer.history
    assert result.wire_history is producer.wire_history


def test_gex_history_refresh_publishes_recomputed_lane_intervals() -> None:
    previous = [
        {
            "captured_at": "2026-07-21T10:00:00+00:00",
            "timestamp_unix_ms": 1,
            "spot": 5000,
            "valid_until": "2026-07-21T10:10:00+00:00",
            "valid_until_unix_ms": 10,
            "duration_minutes": 10,
        },
        {
            "captured_at": "2026-07-21T10:05:00+00:00",
            "timestamp_unix_ms": 2,
            "spot": 5001,
            "valid_until": "2026-07-21T10:15:00+00:00",
            "valid_until_unix_ms": 15,
            "duration_minutes": 10,
        },
    ]
    refreshed = [
        {
            **previous[0],
            "valid_until": "2026-07-21T10:10:30+00:00",
            "valid_until_unix_ms": 11,
            "duration_minutes": 10.5,
        },
        dict(previous[1]),
    ]

    assert gex_stream_ws._gex_history_read_model_changed(previous, refreshed) is True
    refreshed[0]["spot"] = 5001
    assert gex_stream_ws._gex_history_read_model_changed(previous, refreshed) is True
