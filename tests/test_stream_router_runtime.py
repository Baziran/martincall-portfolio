from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.domain import Bar
from aef_terminal.ui import stream_payload
from aef_terminal.ui.routers import streams as streams_router
from aef_terminal.ui.runtime.chart_stream import ChartStreamRuntime
from aef_terminal.ui.services import chart_stream_coordinator
from aef_terminal.ui.services import chart_stream_messages
from aef_terminal.ui.services import chart_stream_ws
from aef_terminal.ui.services import quote_stream_ws
from aef_terminal.ui.services.chart_stream_payload_tracker import ChartStreamPayloadTracker


def _bar_payload(
    ts: str,
    *,
    timeframe: str = "5m",
    close: float = 100.5,
    closed: bool = True,
    source: str = "ibkr",
) -> dict[str, Any]:
    return {
        "symbol": "ES",
        "ts": ts,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 10.0,
        "timeframe": timeframe,
        "source": source,
        "closed": closed,
        "state": "confirmed" if closed else "forming",
        "authoritative": closed,
        "commit_pending": False,
        "bar_slot_authoritative": False,
        "bar_slot_schedule_state": "unknown",
    }


def _chart_tracker(*, initial_ts: datetime | None = None) -> ChartStreamPayloadTracker:
    return ChartStreamPayloadTracker(
        parse_stream_ts=stream_payload.parse_stream_ts,
        stream_bar_signature=stream_payload.stream_bar_signature,
        initial_ts=initial_ts,
    )


def _chart_consumer(
    range_: str = "3d",
    *,
    tracker: ChartStreamPayloadTracker | None = None,
    deliver: bool = True,
    consumer_role: str = "primary",
    tail_bars: int = chart_stream_coordinator.CHART_STREAM_LIVE_TAIL_BARS,
    queue: asyncio.Queue[dict[str, Any]] | None = None,
) -> chart_stream_coordinator.ChartStreamConsumer:
    resolved_queue = queue if queue is not None else asyncio.Queue(maxsize=4)
    return chart_stream_coordinator.ChartStreamConsumer(
        range_=range_,
        queue=resolved_queue,
        tracker=tracker or _chart_tracker(),
        consumer_role=consumer_role,  # type: ignore[arg-type]
        tail_bars=tail_bars,
        deliver=deliver,
    )


def test_chart_stream_rejects_unsafe_history_before_route_or_coordinator() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.accepted = False
            self.messages: list[dict] = []

        async def accept(self) -> None:
            self.accepted = True

        async def send_json(self, payload: dict) -> None:
            self.messages.append(payload)

    async def scenario() -> None:
        websocket = FakeWebSocket()

        def unexpected_lookup(_instrument_id: str) -> dict:
            raise AssertionError("route lookup must not run for an unsafe range")

        await chart_stream_ws.run_chart_stream(
            websocket,
            instrument_id="instrument-v1",
            expected_route_fingerprint="route-v1",
            interval="1m",
            range_="2mo",
            deps=SimpleNamespace(lookup_runtime_instrument=unexpected_lookup),
        )

        assert websocket.accepted is True
        assert len(websocket.messages) == 1
        payload = websocket.messages[0]
        assert payload["error"]["code"] == "CHART_HISTORY_RANGE_TOO_LARGE"
        assert payload["error"]["retryable"] is False
        assert payload["requires_resubscribe"] is False

    asyncio.run(scenario())


def test_chart_stream_cancellation_settles_physical_route_lookup() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class FakeWebSocket:
        async def accept(self) -> None:
            return None

    def blocking_lookup(_instrument_id: str) -> dict:
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return {}

    async def scenario() -> None:
        task = asyncio.create_task(
            chart_stream_ws.run_chart_stream(
                FakeWebSocket(),
                instrument_id="instrument-v1",
                expected_route_fingerprint="route-v1",
                interval="1m",
                range_="1d",
                deps=SimpleNamespace(lookup_runtime_instrument=blocking_lookup),
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


def test_chart_coordinator_is_shared_by_qualified_route_and_timeframe_across_ranges() -> None:
    runtime = ChartStreamRuntime()
    key = runtime.coordinator_key("instrument-v1", "5m", "route-v1")
    created_generations: list[int] = []

    class FakeCoordinator:
        def __init__(self, generation: int) -> None:
            self.task = None
            self.generation = generation
            self.consumers: list[SimpleNamespace] = []

        def add_consumer(
            self,
            range_: str,
            since_ts: str,
            **kwargs: Any,
        ) -> SimpleNamespace:
            consumer = SimpleNamespace(
                range_=range_,
                since_ts=since_ts,
                **kwargs,
            )
            self.consumers.append(consumer)
            return consumer

        def remove_consumer(self, consumer: SimpleNamespace) -> int:
            if consumer in self.consumers:
                self.consumers.remove(consumer)
            return len(self.consumers)

    def factory(generation: int) -> FakeCoordinator:
        created_generations.append(generation)
        return FakeCoordinator(generation)

    first, first_consumer, first_created = runtime.acquire_coordinator(
        key, factory, range_="3d", since_ts=""
    )
    second, second_consumer, second_created = runtime.acquire_coordinator(
        key, factory, range_="14d", since_ts=""
    )

    assert key == ("instrument-v1", "route-v1", "5m")
    assert first_created is True
    assert second_created is False
    assert second is first
    assert len(first.consumers) == 2
    assert created_generations == [1]
    assert runtime.status()["hot_coordinators"] == 1
    assert asyncio.run(runtime.release_coordinator(key, first, first_consumer)) is False
    assert asyncio.run(runtime.release_coordinator(key, first, second_consumer)) is True

    replacement, replacement_consumer, replacement_created = runtime.acquire_coordinator(
        key, factory, range_="5d", since_ts=""
    )
    assert replacement_created is True
    assert replacement is not first
    assert replacement.generation == 2
    assert asyncio.run(runtime.release_coordinator(key, replacement, replacement_consumer)) is True


def test_chart_coordinator_release_grace_preserves_concurrent_reconnect_consumer() -> None:
    async def scenario() -> None:
        runtime = ChartStreamRuntime()
        key = runtime.coordinator_key("instrument-v1", "5m", "route-v1")

        class FakeCoordinator:
            task = None

            def __init__(self, generation: int) -> None:
                self.generation = generation
                self.consumers: list[object] = []

            def add_consumer(
                self,
                _range: str,
                _since_ts: str,
                **_kwargs: Any,
            ) -> object:
                consumer = object()
                self.consumers.append(consumer)
                return consumer

            def remove_consumer(self, consumer: object) -> int:
                if consumer in self.consumers:
                    self.consumers.remove(consumer)
                return len(self.consumers)

        first, first_consumer, created = runtime.acquire_coordinator(
            key,
            FakeCoordinator,
            range_="3d",
            since_ts="",
        )
        assert created is True
        release = asyncio.create_task(
            runtime.release_coordinator(key, first, first_consumer, grace_seconds=0.02)
        )
        await asyncio.sleep(0)
        assert first.consumers == [first_consumer]

        reconnected, reconnect_consumer, reconnect_created = runtime.acquire_coordinator(
            key,
            FakeCoordinator,
            range_="14d",
            since_ts="",
        )

        assert reconnected is first
        assert reconnect_created is False
        assert await release is False
        assert first.consumers == [reconnect_consumer]
        assert runtime.status()["hot_coordinators"] == 1
        assert await runtime.release_coordinator(key, first, reconnect_consumer) is True

    asyncio.run(scenario())


def test_internal_chart_streams_join_self_heal_without_becoming_clients() -> None:
    runtime = ChartStreamRuntime()
    key = runtime.stream_key(
        "instrument-v1",
        "1m",
        "1d",
        "route-v1",
    )

    runtime.internal_stream_started(key)

    assert runtime.connection_summary() == (0, 0)
    assert runtime.active_streams() == [("instrument-v1", "1m", "1d", 1)]
    assert runtime.status()["internal_consumers"] == 1
    assert runtime.internal_stream_finished(key) == 0
    assert runtime.active_streams() == []


def test_chart_coordinator_preserves_opaque_identity_tokens_exactly() -> None:
    source = inspect.getsource(ChartStreamRuntime.coordinator_key)

    assert ".strip()" not in source.split("str(interval", 1)[0]
    assert ChartStreamRuntime.coordinator_key(" instrument ", " 5m ", " route ") == (
        " instrument ",
        " route ",
        "5m",
    )


def test_chart_coordinator_expands_initial_provider_tail_from_stale_client_cursor() -> None:
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route=SimpleNamespace(
            provider="ibkr",
            instrument_id="instrument-v1",
            fingerprint="route-v1",
        ),
        interval="5m",
        generation=1,
        deps=SimpleNamespace(
            parse_iso_ts=lambda value: datetime.fromisoformat(value) if value else None,
            parse_stream_ts=lambda payload: (
                datetime.fromisoformat(payload["ts"]) if payload and payload.get("ts") else None
            ),
            stream_bar_signature=lambda payload: tuple(sorted((payload or {}).items())),
        ),
    )
    coordinator._schedule_checkpoint = lambda *_args, **_kwargs: None
    stale_cursor = (datetime.now(tz=UTC) - timedelta(hours=20)).isoformat()

    consumer = coordinator.add_consumer("5d", stale_cursor)

    assert consumer.tracker.last_sent_ts == datetime.fromisoformat(stale_cursor)
    assert 240 <= coordinator.live_tail_bars <= 500
    assert coordinator._stale_tail_repair_from["5d"] == (
        chart_stream_coordinator.interval_bucket(
            datetime.fromisoformat(stale_cursor) + timedelta(minutes=5),
            "5m",
        )
    )


def test_chart_bar_contract_rejects_placeholders_and_realtime_resolver_preserves_authority() -> (
    None
):
    ts = "2026-07-18T13:20:00+00:00"
    placeholder = {
        **_bar_payload(ts, closed=False, source="gap-placeholder"),
        "ts": ts,
        "preview_kind": "gap_placeholder",
        "missing": True,
        "bar_slot": 42,
        "authoritative": False,
    }
    provider = {
        **_bar_payload(ts, closed=False, source="ibkr:chart-live"),
        "preview_kind": "provider_bar",
        "authoritative": False,
    }
    confirmed = {
        **provider,
        "source": "ibkr:canonical-commit",
        "closed": True,
        "state": "confirmed",
        "authoritative": True,
        "canonical_revision": 7,
    }

    with pytest.raises(ValueError, match="gap placeholders"):
        chart_stream_messages.require_chart_stream_bar_payload(placeholder, interval="5m")
    assert chart_stream_coordinator._replace_bar_payload(provider, confirmed) == confirmed
    assert chart_stream_coordinator._replace_bar_payload(confirmed, provider) == confirmed


def test_chart_bar_resolver_and_signature_replace_expected_close_atomically() -> None:
    base = {
        "ts": "2026-07-18T13:30:00+00:00",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "source": "ibkr:chart-live",
        "closed": False,
        "state": "forming",
        "preview_kind": "provider_bar",
        "bar_slot": 42,
        "expected_close": "2026-07-18T14:00:00+00:00",
    }
    corrected = {
        **base,
        "expected_close": "2026-07-18T13:45:00+00:00",
    }

    assert stream_payload.stream_bar_signature(corrected) != stream_payload.stream_bar_signature(
        base
    )
    assert (
        chart_stream_coordinator._replace_bar_payload(base, corrected)["expected_close"]
        == corrected["expected_close"]
    )

    without_boundary = dict(corrected)
    without_boundary.pop("expected_close")
    resolved_without_boundary = chart_stream_coordinator._replace_bar_payload(
        corrected, without_boundary
    )
    assert "expected_close" not in resolved_without_boundary
    assert stream_payload.stream_bar_signature(
        resolved_without_boundary
    ) != stream_payload.stream_bar_signature(corrected)


def test_gap_placeholders_never_enter_chart_stream_frames() -> None:
    pending = {
        **_bar_payload(
            "2026-07-18T13:30:00+00:00",
            closed=False,
            source="gap-placeholder",
        ),
        "ts": "2026-07-18T13:30:00+00:00",
        "state": "awaiting_provider_confirmation",
        "preview_kind": "gap_placeholder",
        "missing": True,
        "authoritative": False,
        "availability_state": "pending",
        "fill_forward": True,
    }
    with pytest.raises(ValueError, match="gap placeholders"):
        chart_stream_messages.chart_bars_payload(
            source="ibkr:db-checkpoint",
            instrument_id="instrument-v1",
            route_fingerprint="route-v1",
            symbol="ES",
            provider_symbol="ESU6",
            interval="5m",
            range_="3d",
            bars=[pending],
        )


def test_chart_consumer_queue_is_bounded_and_retains_canonical_and_terminal_frames() -> None:
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=3)
    queue.put_nowait({"type": "heartbeat", "stream_seq": 1})
    queue.put_nowait(
        {
            "type": "chart_bars",
            "recovery": True,
            "canonical_revision": 0,
            "stream_seq": 2,
            "bars": [],
        }
    )
    queue.put_nowait(
        {
            "type": "chart_bars",
            "canonical_revision": 9,
            "stream_seq": 3,
            "bars": [{"authoritative": True}],
        }
    )

    chart_stream_coordinator.compact_chart_consumer_queue(
        queue,
        {
            "type": "chart_status",
            "requires_resubscribe": True,
            "stream_seq": 4,
        },
    )

    retained = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(retained) == 3
    assert [item["stream_seq"] for item in retained] == [2, 3, 4]
    assert any(item.get("canonical_revision") == 9 for item in retained)
    assert retained[-1]["requires_resubscribe"] is True

    repair_tracker = ChartStreamPayloadTracker(
        parse_stream_ts=stream_payload.parse_stream_ts,
        stream_bar_signature=stream_payload.stream_bar_signature,
    )
    repaired_slot = "2026-07-18T13:20:00+00:00"
    repair_tracker.invalidate_timestamps([repaired_slot])
    compacted: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    compacted.put_nowait(
        {
            "type": "chart_bars",
            "recovery": True,
            "canonical_revision": 0,
            "stream_seq": 1,
            "bars": [{"ts": repaired_slot}],
        }
    )
    chart_stream_coordinator.compact_chart_consumer_queue(
        compacted,
        {"type": "chart_bars", "canonical_revision": 2, "stream_seq": 2, "bars": []},
    )

    assert compacted.get_nowait()["canonical_revision"] == 2
    assert repair_tracker.invalidated_timestamps == (repaired_slot,)

    authoritative: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    authoritative.put_nowait(
        {
            "type": "chart_bars",
            "canonical_revision": 7,
            "stream_seq": 10,
            "bars": [{"ts": repaired_slot, "authoritative": True}],
        }
    )
    chart_stream_coordinator.compact_chart_consumer_queue(
        authoritative,
        {
            "type": "chart_bars",
            "canonical_revision": 7,
            "stream_seq": 11,
            "bars": [{"ts": repaired_slot, "authoritative": False}],
        },
    )

    retained_authoritative = authoritative.get_nowait()
    assert retained_authoritative["stream_seq"] == 10
    assert retained_authoritative["bars"][0]["authoritative"] is True

    empty_canonical: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
    empty_canonical.put_nowait(
        {
            "type": "chart_bars",
            "canonical_revision": 8,
            "recovery": True,
            "stream_seq": 20,
            "bars": [],
        }
    )
    chart_stream_coordinator.compact_chart_consumer_queue(
        empty_canonical,
        {
            "type": "chart_bars",
            "canonical_revision": 8,
            "stream_seq": 21,
            "bars": [{"ts": repaired_slot, "authoritative": False}],
        },
    )

    retained_empty_canonical = empty_canonical.get_nowait()
    assert retained_empty_canonical["stream_seq"] == 20
    assert retained_empty_canonical["bars"] == []


def test_chart_bar_protocol_carries_identity_ordering_quality_and_recovery_scope() -> None:
    live = chart_stream_messages.chart_bars_payload(
        source="ibkr:chart-live",
        instrument_id="instrument-v1",
        route_fingerprint="route-v1",
        symbol="ES",
        provider_symbol="ESU6",
        interval="5m",
        range_="3d",
        bars=[
            _bar_payload(
                "2026-07-18T13:20:00+00:00",
                closed=False,
                source="ibkr:chart-live",
            )
        ],
        stream_generation=4,
        stream_seq=11,
        expected_live_slot="2026-07-18T13:20:00+00:00",
        expected_live_close="2026-07-18T13:25:00+00:00",
    )
    recovery = chart_stream_messages.chart_bars_payload(
        source="ibkr:db-checkpoint",
        instrument_id="instrument-v1",
        route_fingerprint="route-v1",
        symbol="ES",
        provider_symbol="ESU6",
        interval="5m",
        range_="3d",
        bars=[_bar_payload("2026-07-18T13:15:00+00:00")],
        recovery=True,
        recovery_complete=True,
        recovery_scope="initial",
        stream_generation=4,
        stream_seq=12,
        canonical_revision=8,
        chart_data_quality={"signals_ok": False, "display_slots_pending": True},
        history_coverage={"state": "complete", "requested_range": "3d"},
        future_axis={
            "kind": "provider_session_future_axis",
            "timeframe": "5m",
            "schedule_revision": 1,
            "schedule_state": "verified",
            "anchor_ts": "2026-07-18T13:15:00+00:00",
            "requested_slots": 1,
            "complete": True,
            "slots": [
                {
                    "ts": "2026-07-18T13:20:00+00:00",
                    "bar_offset": 1,
                }
            ],
        },
    )

    assert live["phase"] == "live"
    assert live["consumer_role"] == "primary"
    assert live["stream_generation"] == 4
    assert live["stream_seq"] == 11
    assert live["expected_live_slot"] == "2026-07-18T13:20:00+00:00"
    assert live["expected_live_close"] == "2026-07-18T13:25:00+00:00"
    assert live["bars"][0]["instrument_id"] == "instrument-v1"
    assert live["bars"][0]["route_fingerprint"] == "route-v1"
    assert live["bars"][0]["provider_symbol"] == "ESU6"
    assert recovery["phase"] == "recovery_complete"
    assert recovery["recovery_scope"] == "initial"
    assert recovery["canonical_revision"] == 8
    assert "chart_data_quality" not in live
    assert recovery["chart_data_quality"]["display_slots_pending"] is True
    assert recovery["chart_quality_revision"] == 8
    assert recovery["history_coverage"] == {"state": "complete", "requested_range": "3d"}
    assert recovery["future_axis"]["slots"][0]["bar_offset"] == 1
    assert "data_quality" not in recovery


@pytest.mark.parametrize("revision", [True, -1, "8", 1.5])
def test_chart_bar_protocol_rejects_coercible_or_invalid_revisions(revision: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        chart_stream_messages.chart_bars_payload(
            source="ibkr:chart-live",
            instrument_id="instrument-v1",
            route_fingerprint="route-v1",
            symbol="ES",
            provider_symbol="ESU6",
            interval="5m",
            range_="3d",
            bars=[_bar_payload("2026-07-18T13:20:00+00:00")],
            canonical_revision=revision,  # type: ignore[arg-type]
        )


def test_chart_bar_protocol_rejects_malformed_rows_and_route_disagreement() -> None:
    with pytest.raises(ValueError, match="BAR_SIGNATURE_OPEN_MISSING"):
        chart_stream_messages.chart_bars_payload(
            source="ibkr:chart-live",
            instrument_id="instrument-v1",
            route_fingerprint="route-v1",
            symbol="ES",
            provider_symbol="ESU6",
            interval="5m",
            range_="3d",
            bars=[{"ts": "2026-07-18T13:20:00+00:00", "closed": True}],
        )

    mismatched = {
        **_bar_payload("2026-07-18T13:20:00+00:00"),
        "route_fingerprint": "stale-route",
    }
    with pytest.raises(ValueError, match="does not match its frame"):
        chart_stream_messages.chart_bars_payload(
            source="ibkr:chart-live",
            instrument_id="instrument-v1",
            route_fingerprint="route-v1",
            symbol="ES",
            provider_symbol="ESU6",
            interval="5m",
            range_="3d",
            bars=[mismatched],
        )


def test_chart_bar_protocol_rejects_partial_or_unordered_future_axis() -> None:
    base_axis = {
        "kind": "provider_session_future_axis",
        "timeframe": "5m",
        "schedule_revision": 1,
        "schedule_state": "verified",
        "anchor_ts": "2026-07-18T13:15:00+00:00",
        "requested_slots": 2,
        "complete": True,
    }
    with pytest.raises(ValueError, match="slots are invalid"):
        chart_stream_messages.require_chart_future_axis_payload(base_axis)
    with pytest.raises(ValueError, match="strictly ordered"):
        chart_stream_messages.require_chart_future_axis_payload(
            {
                **base_axis,
                "slots": [
                    {"ts": "2026-07-18T13:25:00+00:00", "bar_offset": 2},
                    {"ts": "2026-07-18T13:20:00+00:00", "bar_offset": 1},
                ],
            }
        )


def test_secondary_chart_consumer_profile_is_typed_and_bounded() -> None:
    assert (
        chart_stream_messages.require_chart_stream_consumer_role("secondary_candles")
        == "secondary_candles"
    )
    assert (
        chart_stream_coordinator.require_secondary_chart_tail_bars(None)
        == chart_stream_coordinator.CHART_STREAM_SECONDARY_TAIL_DEFAULT
    )
    assert chart_stream_coordinator.require_secondary_chart_tail_bars(300) == 300
    with pytest.raises(ValueError, match="CHART_STREAM_CONSUMER_ROLE_UNSUPPORTED"):
        chart_stream_messages.require_chart_stream_consumer_role("analysis")
    for unsupported_tail in (64, 299, 301, 500, 501):
        with pytest.raises(ValueError, match="CHART_STREAM_SECONDARY_TAIL_PROFILE_UNSUPPORTED"):
            chart_stream_coordinator.require_secondary_chart_tail_bars(unsupported_tail)
    with pytest.raises(ValueError, match="CHART_STREAM_SECONDARY_MEMBERSHIP_LIMIT_INVALID"):
        chart_stream_messages.require_secondary_tail_membership({"limit": 63, "timestamps": []})
    with pytest.raises(ValueError, match="CHART_STREAM_SECONDARY_MEMBERSHIP_TIMESTAMPS_INVALID"):
        chart_stream_messages.require_secondary_tail_membership(
            {"limit": 64, "timestamps": ["not-a-timestamp"]}
        )
    with pytest.raises(ValueError, match="CHART_STREAM_SECONDARY_TAIL_INVALID"):
        chart_stream_coordinator.require_secondary_chart_tail_bars(True)


def test_secondary_chart_request_bypasses_unbounded_primary_history_range() -> None:
    class ExpectedRouteLookup(RuntimeError):
        pass

    class FakeWebSocket:
        async def accept(self) -> None:
            return None

        async def send_json(self, _payload: dict) -> None:
            return None

    def lookup(_instrument_id: str) -> dict:
        raise ExpectedRouteLookup

    async def scenario() -> None:
        with pytest.raises(ExpectedRouteLookup):
            await chart_stream_ws.run_chart_stream(
                FakeWebSocket(),
                instrument_id="instrument-v1",
                expected_route_fingerprint="route-v1",
                interval="1m",
                range_="all",
                consumer_role="secondary_candles",
                deps=SimpleNamespace(lookup_runtime_instrument=lookup),
            )

    asyncio.run(scenario())


def test_secondary_tail_profile_rejects_in_range_cardinality_before_route_lookup() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, payload: dict) -> None:
            self.messages.append(payload)

    def unexpected_lookup(_instrument_id: str) -> dict:
        raise AssertionError("invalid secondary profile must not reach route lookup")

    async def scenario() -> None:
        for unsupported_tail in (64, 299, 301, 500):
            websocket = FakeWebSocket()
            await chart_stream_ws.run_chart_stream(
                websocket,
                instrument_id="instrument-v1",
                expected_route_fingerprint="route-v1",
                interval="1m",
                range_="1d",
                consumer_role="secondary_candles",
                tail_bars=unsupported_tail,
                deps=SimpleNamespace(lookup_runtime_instrument=unexpected_lookup),
            )

            assert len(websocket.messages) == 1
            status = websocket.messages[0]
            assert status["type"] == "chart_status"
            assert status["consumer_role"] == "secondary_candles"
            assert status["error"]["code"] == "CHART_STREAM_SECONDARY_TAIL_PROFILE_UNSUPPORTED"
            assert status["retry_in_seconds"] == 0
            assert status["interval"] == "1m"
            assert status["range"] == "1d"

    asyncio.run(scenario())


def test_secondary_chart_consumer_shares_coordinator_and_live_lease() -> None:
    runtime = ChartStreamRuntime()
    key = runtime.coordinator_key("instrument-v1", "1m", "route-v1")

    class FakeCoordinator:
        task = None

        def __init__(self, generation: int) -> None:
            self.generation = generation
            self.consumers: list[SimpleNamespace] = []

        def add_consumer(self, range_: str, since_ts: str, **kwargs: Any) -> SimpleNamespace:
            consumer = SimpleNamespace(range_=range_, since_ts=since_ts, **kwargs)
            self.consumers.append(consumer)
            return consumer

        def remove_consumer(self, consumer: SimpleNamespace) -> int:
            self.consumers.remove(consumer)
            return len(self.consumers)

    primary, primary_consumer, primary_created = runtime.acquire_coordinator(
        key,
        FakeCoordinator,
        range_="5d",
        since_ts="",
    )
    secondary, secondary_consumer, secondary_created = runtime.acquire_coordinator(
        key,
        FakeCoordinator,
        range_="1d",
        since_ts="",
        consumer_role="secondary_candles",
        live_tail_bars=300,
        tail_bars=300,
    )

    assert primary_created is True
    assert secondary_created is False
    assert secondary is primary
    assert secondary_consumer.consumer_role == "secondary_candles"
    assert secondary_consumer.tail_bars == 300
    assert primary.generation == 1
    assert asyncio.run(runtime.release_coordinator(key, primary, primary_consumer)) is False
    assert asyncio.run(runtime.release_coordinator(key, secondary, secondary_consumer)) is True


def test_removing_secondary_reduces_derived_provider_tail_to_remaining_primary() -> None:
    async def scenario() -> None:
        requested_tails: list[tuple[str, int]] = []

        async def read_live(interval: str, _range: str, **kwargs: Any) -> list[Bar]:
            requested_tails.append((interval, kwargs["tail"]))
            return []

        route = SimpleNamespace(
            provider="ibkr",
            instrument={"instrument_id": "instrument-v1"},
            instrument_id="instrument-v1",
            fingerprint="route-v1",
            adapter=SimpleNamespace(
                data_policy=SimpleNamespace(chart_request_timeout_seconds=1.0),
                async_chart_live_bars=read_live,
            ),
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route=route,
            interval="3m",
            generation=1,
            deps=SimpleNamespace(
                parse_iso_ts=stream_payload.parse_iso_ts,
                parse_stream_ts=stream_payload.parse_stream_ts,
                stream_bar_signature=stream_payload.stream_bar_signature,
            ),
        )
        coordinator._schedule_checkpoint = lambda *_args, **_kwargs: None
        primary = coordinator.add_consumer("1d", "", deliver=False)
        secondary = coordinator.add_consumer(
            "1d",
            "",
            deliver=False,
            consumer_role="secondary_candles",
            tail_bars=300,
        )

        assert coordinator.live_tail_bars == 300
        primary_tail_bars = primary.tail_bars
        assert chart_stream_coordinator.CHART_STREAM_LIVE_TAIL_BARS <= primary_tail_bars < 300
        await coordinator._start_or_poll_live()
        assert coordinator.remove_consumer(secondary) == 1
        assert coordinator.live_tail_bars == primary_tail_bars
        await coordinator._start_or_poll_live()

        assert requested_tails == [("1m", 902), ("1m", primary_tail_bars * 3 + 2)]
        assert coordinator.remove_consumer(primary) == 0

    asyncio.run(scenario())


def test_three_minute_native_live_lifecycle_cancels_exact_one_minute_lease(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        started: list[str] = []
        cancelled: list[str] = []

        class Adapter:
            capabilities = SimpleNamespace(
                native_chart_stream=True,
                chart_tail_polling=False,
                runtime_settings=False,
            )
            data_policy = SimpleNamespace(
                chart_request_timeout_seconds=1.0,
                chart_poll_seconds=1.0,
            )

            async def async_chart_live_bars(
                self,
                interval: str,
                _range: str,
                **_kwargs: Any,
            ) -> list[Bar]:
                started.append(interval)
                return []

            async def async_cancel_chart_live_bars(
                self,
                interval: str,
                _range: str,
                **_kwargs: Any,
            ) -> None:
                cancelled.append(interval)

        monkeypatch.setattr(
            chart_stream_coordinator,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: 0,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "live_chart_bars_snapshot",
            lambda *_args, **_kwargs: None,
        )
        route = SimpleNamespace(
            provider="ibkr",
            instrument={"display": "ES"},
            instrument_id="instrument-v1",
            fingerprint="route-v1",
            provider_symbol="ESU6",
            instrument_key="ES",
            adapter=Adapter(),
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route=route,
            interval="3m",
            generation=1,
            deps=SimpleNamespace(
                server_sleeping=lambda: True,
                chart_stream_poll_seconds=1.0,
                websocket_heartbeat_seconds=1.0,
                logger=SimpleNamespace(
                    warning=lambda *_args, **_kwargs: None,
                    debug=lambda *_args, **_kwargs: None,
                ),
            ),
        )
        queue: asyncio.Queue[dict] = asyncio.Queue()
        coordinator.consumers = {
            queue: _chart_consumer(
                range_="1d",
                deliver=False,
                tail_bars=64,
                queue=queue,
            )
        }

        await coordinator.run()

        assert started == ["1m"]
        assert cancelled == ["1m"]

    asyncio.run(scenario())


def test_secondary_checkpoint_profile_coalesces_without_primary_range_collision() -> None:
    async def scenario() -> None:
        release_checkpoint = asyncio.Event()
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route=SimpleNamespace(
                provider="ibkr",
                instrument_id="instrument-v1",
                fingerprint="route-v1",
            ),
            interval="1m",
            generation=1,
            deps=SimpleNamespace(
                parse_iso_ts=stream_payload.parse_iso_ts,
                parse_stream_ts=stream_payload.parse_stream_ts,
                stream_bar_signature=stream_payload.stream_bar_signature,
            ),
        )

        async def load_secondary(_tail_bars: int):
            await release_checkpoint.wait()
            return None

        coordinator._load_secondary_checkpoint = load_secondary
        primary = coordinator.add_consumer("1d", "", deliver=False)
        first = coordinator.add_consumer(
            "1d",
            "",
            deliver=False,
            consumer_role="secondary_candles",
            tail_bars=300,
        )
        second = coordinator.add_consumer(
            "5d",
            "",
            deliver=False,
            consumer_role="secondary_candles",
            tail_bars=300,
        )

        assert coordinator._consumer_checkpoint_key(primary) == "1d"
        assert coordinator._consumer_checkpoint_key(first) == ("secondary_candles", 300)
        assert coordinator._consumer_checkpoint_key(second) == ("secondary_candles", 300)
        assert set(coordinator._checkpoint_tasks) == {
            "1d",
            ("secondary_candles", 300),
        }
        assert ("secondary_candles", 300) not in coordinator._checkpoint_followups

        release_checkpoint.set()
        for task in coordinator._checkpoint_tasks.values():
            task.cancel()
        await asyncio.gather(*coordinator._checkpoint_tasks.values(), return_exceptions=True)

    asyncio.run(scenario())


def test_secondary_checkpoint_uses_exact_bounded_recent_provider_reader(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        event_loop_thread = threading.get_ident()
        reads: list[tuple[object, str, object, int, int]] = []
        route = SimpleNamespace(
            provider="ibkr",
            instrument={"instrument_id": "instrument-v1"},
            instrument_id="instrument-v1",
            fingerprint="route-v1",
        )
        closed = Bar(
            "ES",
            datetime(2026, 7, 18, 13, 18, tzinfo=UTC),
            100.0,
            101.0,
            99.0,
            100.5,
            10,
            "3m",
            "ibkr",
        )

        def read_recent(route_arg, timeframe, *, store, limit):
            reads.append((route_arg, timeframe, store, limit, threading.get_ident()))
            return [closed]

        monkeypatch.setattr(
            chart_stream_coordinator,
            "read_recent_provider_bars",
            read_recent,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: 9,
        )

        async def stable(*_args, **_kwargs):
            return 9

        monkeypatch.setattr(chart_stream_coordinator, "wait_for_chart_bars_stable", stable)
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route=route,
            interval="3m",
            generation=1,
            deps=SimpleNamespace(
                store_factory=lambda: "store-v1",
                stream_bar_payload=lambda bar, *, instrument: stream_payload.stream_bar_payload(
                    bar
                ),
            ),
        )

        result = await coordinator._load_secondary_checkpoint(300)

        snapshot, warning, profile, generation, coverage, required = result
        assert reads == [(route, "3m", "store-v1", 300, reads[0][4])]
        assert reads[0][4] != event_loop_thread
        assert len(snapshot.payloads) == 1
        assert warning == ""
        assert profile == ("secondary_candles", 300)
        assert generation == 9
        assert coverage == {}
        assert required == ()

    asyncio.run(scenario())


def test_secondary_chart_frames_are_compact_and_never_feed_execution_snapshot() -> None:
    route = SimpleNamespace(
        provider="ibkr",
        instrument={"display": "ES"},
        instrument_id="instrument-v1",
        fingerprint="route-v1",
        provider_symbol="ESU6",
        instrument_key="ES",
    )
    execution_snapshots: list[tuple] = []
    deps = SimpleNamespace(
        parse_iso_ts=stream_payload.parse_iso_ts,
        parse_stream_ts=stream_payload.parse_stream_ts,
        stream_bar_signature=stream_payload.stream_bar_signature,
        record_chart_execution_snapshot=lambda *args: execution_snapshots.append(args),
    )
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(route, "1m", 3, deps)
    coordinator._schedule_checkpoint = lambda *_args, **_kwargs: None
    primary = coordinator.add_consumer("5d", "")
    secondary = coordinator.add_consumer(
        "1d",
        "",
        consumer_role="secondary_candles",
        tail_bars=300,
    )
    payload = _bar_payload("2026-07-18T13:20:00+00:00", timeframe="1m")

    coordinator._publish_bar_payloads(
        [payload],
        source="ibkr:db-checkpoint",
        recovery=True,
        recovery_complete=True,
        recovery_scope="checkpoint",
        chart_data_quality={"signals_ok": True},
        history_coverage={"state": "complete"},
        future_axis={
            "kind": "provider_session_future_axis",
            "timeframe": "1m",
            "schedule_revision": 1,
            "schedule_state": "unknown",
            "anchor_ts": None,
            "requested_slots": 1,
            "complete": False,
            "slots": [],
        },
        secondary_tail_membership={
            "limit": 300,
            "timestamps": [payload["ts"]],
        },
    )

    primary_frame = primary.queue.get_nowait()
    secondary_frame = secondary.queue.get_nowait()
    assert primary_frame["consumer_role"] == "primary"
    assert primary_frame["chart_data_quality"] == {"signals_ok": True}
    assert primary_frame["history_coverage"] == {"state": "complete"}
    assert "future_axis" in primary_frame
    assert "secondary_tail_membership" not in primary_frame
    assert secondary_frame["consumer_role"] == "secondary_candles"
    assert "chart_data_quality" not in secondary_frame
    assert "history_coverage" not in secondary_frame
    assert "future_axis" not in secondary_frame
    assert secondary_frame["secondary_tail_membership"] == {
        "limit": 300,
        "timestamps": [payload["ts"]],
    }
    assert execution_snapshots == [
        (
            "instrument-v1",
            "1m",
            primary_frame["bars"],
            "ibkr:db-checkpoint",
            "route-v1",
        )
    ]


def test_secondary_recovery_reconciles_tail_membership_without_full_bar_retransmit(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        timestamps = [
            "2026-07-18T13:18:00+00:00",
            "2026-07-18T13:19:00+00:00",
            "2026-07-18T13:20:00+00:00",
        ]

        def payload(ts: str, close: float) -> dict[str, Any]:
            return {
                "symbol": "ES",
                "ts": ts,
                "open": close,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 10,
                "closed": True,
                "state": "confirmed",
                "timeframe": "1m",
                "source": "ibkr",
                "bar_slot": 0,
                "bar_slot_authoritative": False,
                "bar_slot_schedule_state": "unknown",
            }

        full_tail = tuple(payload(ts, 100.0 + index) for index, ts in enumerate(timestamps))
        after_delete = (full_tail[0], full_tail[2])
        route = SimpleNamespace(
            provider="ibkr",
            instrument={"display": "ES"},
            instrument_id="instrument-v1",
            fingerprint="route-v1",
            provider_symbol="ESU6",
            instrument_key="ES",
        )
        execution_snapshots: list[tuple] = []
        deps = SimpleNamespace(
            parse_iso_ts=stream_payload.parse_iso_ts,
            parse_stream_ts=stream_payload.parse_stream_ts,
            stream_bar_signature=stream_payload.stream_bar_signature,
            record_chart_execution_snapshot=lambda *args: execution_snapshots.append(args),
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(route, "1m", 3, deps)
        coordinator._schedule_checkpoint = lambda *_args, **_kwargs: None
        primary = coordinator.add_consumer("1d", "")
        secondary = coordinator.add_consumer(
            "1d",
            "2099-01-01T00:00:00+00:00",
            consumer_role="secondary_candles",
            tail_bars=300,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "require_chart_bars_generation",
            lambda expected, *_args, **_kwargs: expected,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "serialize_data_quality_report",
            lambda quality: dict(quality),
        )

        async def compact_result(payloads: tuple[dict[str, Any], ...], generation: int):
            return (
                chart_stream_coordinator.CompactChartRecoverySnapshot(payloads),
                "",
                ("secondary_candles", 300),
                generation,
                {},
                (),
            )

        first_task = asyncio.create_task(compact_result(full_tail, 7))
        await first_task
        coordinator._checkpoint_tasks[("secondary_candles", 300)] = first_task
        coordinator._flush_checkpoints()

        assert primary.queue.empty()
        initial = secondary.queue.get_nowait()
        assert [bar["ts"] for bar in initial["bars"]] == timestamps
        assert initial["recovery_scope"] == "initial"
        assert initial["secondary_tail_membership"] == {
            "limit": 300,
            "timestamps": timestamps,
        }
        secondary.tracker.remember(initial["bars"])
        assert execution_snapshots == []

        second_task = asyncio.create_task(compact_result(after_delete, 8))
        await second_task
        coordinator._checkpoint_tasks[("secondary_candles", 300)] = second_task
        coordinator._flush_checkpoints()

        recurring = secondary.queue.get_nowait()
        assert recurring["bars"] == []
        assert recurring["recovery_scope"] == "checkpoint"
        assert recurring["secondary_tail_membership"] == {
            "limit": 300,
            "timestamps": [timestamps[0], timestamps[2]],
        }
        assert primary.queue.empty()
        assert execution_snapshots == []

        async def primary_result():
            return (
                SimpleNamespace(
                    payloads=after_delete,
                    quality={"signals_ok": True},
                    future_axis=None,
                    expected_live_slot=None,
                    expected_live_close=None,
                ),
                "",
                "1d",
                8,
                {},
                (),
            )

        primary_task = asyncio.create_task(primary_result())
        await primary_task
        coordinator._checkpoint_tasks["1d"] = primary_task
        coordinator._flush_checkpoints()

        assert not primary.queue.empty()
        assert secondary.queue.empty()
        assert "secondary_tail_membership" not in primary.queue.get_nowait()

    asyncio.run(scenario())


def test_secondary_initial_checkpoint_remains_full_after_newest_live_bar_race() -> None:
    timestamps = [
        "2026-07-18T13:18:00+00:00",
        "2026-07-18T13:19:00+00:00",
        "2026-07-18T13:20:00+00:00",
    ]

    def confirmed_payload(ts: str, close: float) -> dict[str, Any]:
        return {
            "symbol": "ES",
            "ts": ts,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 10,
            "closed": True,
            "state": "confirmed",
            "timeframe": "1m",
            "source": "ibkr",
            "canonical_revision": 7,
            "authoritative": True,
            "commit_pending": False,
            "bar_slot": 0,
            "bar_slot_authoritative": False,
            "bar_slot_schedule_state": "unknown",
        }

    route = SimpleNamespace(
        provider="ibkr",
        instrument={"display": "ES"},
        instrument_id="instrument-v1",
        fingerprint="route-v1",
        provider_symbol="ESU6",
        instrument_key="ES",
    )
    executions: list[tuple] = []
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route,
        "1m",
        3,
        SimpleNamespace(
            parse_iso_ts=stream_payload.parse_iso_ts,
            parse_stream_ts=stream_payload.parse_stream_ts,
            stream_bar_signature=stream_payload.stream_bar_signature,
            record_chart_execution_snapshot=lambda *args: executions.append(args),
        ),
    )
    coordinator._schedule_checkpoint = lambda *_args, **_kwargs: None
    secondary = coordinator.add_consumer(
        "1d",
        "",
        consumer_role="secondary_candles",
        tail_bars=300,
    )
    forming_latest = {
        **confirmed_payload(timestamps[-1], 102.0),
        "closed": False,
        "state": "forming",
        "canonical_revision": 0,
        "authoritative": False,
    }
    full_tail = [confirmed_payload(ts, 100.0 + index) for index, ts in enumerate(timestamps)]
    membership = {"limit": 300, "timestamps": timestamps}

    coordinator._publish_bar_payloads(
        [forming_latest],
        source="ibkr:chart-live",
        target_checkpoint=("secondary_candles", 300),
    )
    live_frame = secondary.queue.get_nowait()
    secondary.tracker.remember(live_frame["bars"])
    assert secondary.tracker.last_sent_ts == datetime.fromisoformat(timestamps[-1])
    assert secondary.initial_recovery_pending is True

    coordinator._publish_bar_payloads(
        full_tail,
        source="ibkr:db-checkpoint",
        recovery=True,
        recovery_complete=True,
        recovery_scope="checkpoint",
        target_checkpoint=("secondary_candles", 300),
        secondary_tail_membership=membership,
        canonical_revision=7,
    )
    initial = secondary.queue.get_nowait()
    assert initial["recovery_scope"] == "initial"
    assert [bar["ts"] for bar in initial["bars"]] == timestamps
    secondary.tracker.remember(initial["bars"])
    assert secondary.tracker.last_sent_ts == datetime.fromisoformat(timestamps[-1])

    coordinator._publish_bar_payloads(
        full_tail,
        source="ibkr:db-checkpoint",
        recovery=True,
        recovery_complete=True,
        recovery_scope="checkpoint",
        target_checkpoint=("secondary_candles", 300),
        secondary_tail_membership=membership,
        canonical_revision=7,
    )
    recurring = secondary.queue.get_nowait()
    assert recurring["recovery_scope"] == "checkpoint"
    assert recurring["bars"] == []
    assert recurring["secondary_tail_membership"] == membership
    assert executions == []


def test_chart_status_and_heartbeat_are_enriched_for_each_exact_consumer_scope() -> None:
    route = SimpleNamespace(
        provider="ibkr",
        instrument={"display": "ES"},
        instrument_id="instrument-v1",
        fingerprint="route-v1",
        provider_symbol="ESU6",
        instrument_key="ES",
    )
    deps = SimpleNamespace(
        parse_iso_ts=stream_payload.parse_iso_ts,
        parse_stream_ts=stream_payload.parse_stream_ts,
        stream_bar_signature=stream_payload.stream_bar_signature,
    )
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(route, "1m", 3, deps)
    coordinator._schedule_checkpoint = lambda *_args, **_kwargs: None
    primary = coordinator.add_consumer("5d", "")
    secondary = coordinator.add_consumer(
        "1d",
        "",
        consumer_role="secondary_candles",
        tail_bars=300,
    )

    coordinator.publish(
        {
            "type": "chart_status",
            "source": "chart:route",
            "instrument_id": "wrong-instrument",
            "route_fingerprint": "wrong-route",
            "interval": "60m",
            "range": "31d",
        }
    )
    primary_status = primary.queue.get_nowait()
    secondary_status = secondary.queue.get_nowait()
    coordinator.publish({"type": "heartbeat", "source": "ibkr:chart-idle"})
    primary_heartbeat = primary.queue.get_nowait()
    secondary_heartbeat = secondary.queue.get_nowait()

    assert (
        primary_status["consumer_role"],
        primary_status["instrument_id"],
        primary_status["route_fingerprint"],
        primary_status["interval"],
        primary_status["range"],
    ) == ("primary", "instrument-v1", "route-v1", "1m", "5d")
    assert (
        secondary_status["consumer_role"],
        secondary_status["instrument_id"],
        secondary_status["route_fingerprint"],
        secondary_status["interval"],
        secondary_status["range"],
    ) == ("secondary_candles", "instrument-v1", "route-v1", "1m", "1d")
    assert (
        primary_heartbeat["consumer_role"],
        primary_heartbeat["instrument_id"],
        primary_heartbeat["route_fingerprint"],
        primary_heartbeat["interval"],
        primary_heartbeat["range"],
    ) == ("primary", "instrument-v1", "route-v1", "1m", "5d")
    assert (
        secondary_heartbeat["consumer_role"],
        secondary_heartbeat["instrument_id"],
        secondary_heartbeat["route_fingerprint"],
        secondary_heartbeat["interval"],
        secondary_heartbeat["range"],
    ) == ("secondary_candles", "instrument-v1", "route-v1", "1m", "1d")


def test_secondary_early_route_status_carries_requested_exact_scope(monkeypatch) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, payload: dict) -> None:
            self.messages.append(payload)

    route = SimpleNamespace(
        instrument_id="instrument-v1",
        fingerprint="route-actual",
        instrument_key="ES",
    )
    monkeypatch.setattr(chart_stream_ws, "route_instrument", lambda _instrument: route)

    async def scenario() -> None:
        websocket = FakeWebSocket()
        await chart_stream_ws.run_chart_stream(
            websocket,
            instrument_id="instrument-v1",
            expected_route_fingerprint="route-requested",
            interval="1m",
            range_="1d",
            consumer_role="secondary_candles",
            deps=SimpleNamespace(
                lookup_runtime_instrument=lambda _instrument_id: {},
                chart_stream_poll_seconds=1.0,
            ),
        )

        assert len(websocket.messages) == 1
        status = websocket.messages[0]
        assert status["type"] == "chart_status"
        assert status["consumer_role"] == "secondary_candles"
        assert status["instrument_id"] == "instrument-v1"
        assert status["route_fingerprint"] == "route-requested"
        assert status["interval"] == "1m"
        assert status["range"] == "1d"
        assert status["actual_route_fingerprint"] == "route-actual"

    asyncio.run(scenario())


def test_chart_coordinator_rejects_stale_future_axis_and_accepts_new_invalidation() -> None:
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route=SimpleNamespace(
            instrument_id="instrument-v1",
            fingerprint="route-v1",
        ),
        interval="5m",
        generation=1,
        deps=SimpleNamespace(
            parse_iso_ts=lambda value: datetime.fromisoformat(value) if value else None,
        ),
    )
    timestamp = "2026-07-18T13:20:00+00:00"
    verified_axis = {
        "kind": "provider_session_future_axis",
        "timeframe": "5m",
        "schedule_revision": 20,
        "schedule_state": "verified",
        "anchor_ts": "2026-07-18T13:15:00+00:00",
        "requested_slots": 1,
        "complete": True,
        "slots": [{"ts": timestamp, "bar_offset": 1}],
    }
    stale_unknown_axis = {
        "kind": "provider_session_future_axis",
        "timeframe": "5m",
        "schedule_revision": 10,
        "schedule_state": "unknown",
        "anchor_ts": "2026-07-18T13:15:00+00:00",
        "requested_slots": 1,
        "complete": False,
        "slots": [],
    }
    current_unknown_axis = {
        **stale_unknown_axis,
        "schedule_revision": 30,
    }

    coordinator._publish_bar_payloads(
        [],
        source="ibkr:db-checkpoint",
        canonical_revision=8,
        future_axis=verified_axis,
    )
    coordinator._publish_bar_payloads(
        [],
        source="ibkr:db-checkpoint",
        canonical_revision=9,
        future_axis=stale_unknown_axis,
    )

    assert coordinator._future_axis == verified_axis

    coordinator._publish_bar_payloads(
        [],
        source="ibkr:db-checkpoint",
        canonical_revision=1,
        future_axis=current_unknown_axis,
    )

    assert coordinator._future_axis == verified_axis

    coordinator._publish_bar_payloads(
        [],
        source="ibkr:db-checkpoint",
        canonical_revision=10,
        future_axis=current_unknown_axis,
    )

    assert coordinator._future_axis == current_unknown_axis
    accepted_axis = coordinator._future_axis
    coordinator._publish_bar_payloads(
        [],
        source="ibkr:db-checkpoint",
        canonical_revision=10,
        future_axis=current_unknown_axis,
    )
    assert coordinator._future_axis is accepted_axis


def test_chart_coordinator_publishes_unresolved_live_bar_without_schedule_io() -> None:
    timestamp = "2026-07-18T13:20:00+00:00"
    published: list[tuple[list[dict], dict]] = []
    store_calls = 0

    def store_factory() -> object:
        nonlocal store_calls
        store_calls += 1
        return object()

    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route=SimpleNamespace(
            provider="ibkr",
            instrument_id="instrument-v1",
            instrument={"instrument_id": "instrument-v1"},
            fingerprint="route-v1",
        ),
        interval="5m",
        generation=1,
        deps=SimpleNamespace(
            stream_bar_payload=lambda bar, *, instrument: {
                **stream_payload.stream_bar_payload(bar),
                "bar_slot": None,
                "bar_slot_authoritative": False,
                "bar_slot_schedule_state": "unknown",
            },
            store_factory=store_factory,
        ),
    )
    consumer = _chart_consumer()
    coordinator.consumers = {consumer.queue: consumer}
    coordinator._canonical_generation = 11
    coordinator._publish_bar_payloads = lambda payloads, **kwargs: published.append(
        (payloads, kwargs)
    )

    bar = Bar(
        "ES",
        datetime(2026, 7, 18, 13, 20, tzinfo=UTC),
        100.0,
        101.0,
        99.0,
        100.5,
        10.0,
        "5m",
        "ibkr",
        closed=False,
    )
    coordinator._handle_provider_bars(
        [bar],
        datetime(2026, 7, 18, 13, 20, 1, tzinfo=UTC),
    )

    assert store_calls == 0
    assert len(published) == 1
    bars, context = published[0]
    assert bars[0]["ts"] == timestamp
    assert bars[0]["bar_slot"] is None
    assert bars[0]["bar_slot_schedule_state"] == "unknown"
    assert context["source"] == "ibkr:chart-live"
    assert context["canonical_revision"] == 11


def test_confirmed_provider_revision_is_published_hot_then_handed_to_commit_lane(
    monkeypatch,
) -> None:
    submitted: list[tuple] = []
    published: list[tuple[list[dict], dict]] = []

    class CommitRuntime:
        @staticmethod
        def submit(*args):
            submitted.append(args)
            return 1

    monkeypatch.setattr(chart_stream_coordinator, "chart_commits", CommitRuntime())
    adapter = SimpleNamespace(key="ibkr")
    instrument = {"instrument_id": "instrument-v1"}
    route = SimpleNamespace(
        instrument=instrument,
        adapter=adapter,
        provider="ibkr",
        provider_symbol="ESU6",
        instrument_id="instrument-v1",
        instrument_key="ES",
        fingerprint="route-v1",
    )
    deps = SimpleNamespace(
        stream_bar_payload=lambda bar, *, instrument: {
            **stream_payload.stream_bar_payload(bar),
            "bar_slot": 105,
            "bar_slot_authoritative": True,
            "bar_slot_schedule_state": "verified",
        },
        store_factory=lambda: object(),
    )
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route=route,
        interval="5m",
        generation=3,
        deps=deps,
    )
    consumer = _chart_consumer()
    coordinator.consumers = {consumer.queue: consumer}
    coordinator._canonical_generation = 12
    coordinator._publish_bar_payloads = lambda payloads, **kwargs: published.append(
        (payloads, kwargs)
    )
    bar = Bar(
        "ES",
        datetime(2026, 7, 18, 13, 20, tzinfo=UTC),
        100.0,
        101.0,
        99.5,
        100.5,
        10,
        "5m",
        "ibkr",
    )

    coordinator._handle_provider_bars([bar], datetime(2026, 7, 18, 13, 20, 1, tzinfo=UTC))

    hot_bar = published[0][0][0]
    assert published[0][1]["source"] == "ibkr:chart-live"
    assert published[0][1]["canonical_revision"] == 12
    assert hot_bar["preview_kind"] == "provider_bar"
    assert hot_bar["state"] == "awaiting_provider_confirmation"
    assert hot_bar["commit_pending"] is True
    assert hot_bar["authoritative"] is False
    assert hot_bar["bar_slot"] == 105
    assert hot_bar["bar_slot_authoritative"] is True
    assert submitted == [(adapter, instrument, "5m", deps.store_factory, [bar])]


def test_three_minute_live_projection_publishes_before_one_minute_commit(monkeypatch) -> None:
    order: list[str] = []
    submitted: list[tuple] = []

    class CommitRuntime:
        @staticmethod
        def submit(*args):
            order.append("commit")
            submitted.append(args)
            return 1

    monkeypatch.setattr(chart_stream_coordinator, "chart_commits", CommitRuntime())
    adapter = SimpleNamespace(key="coinbase")
    instrument = {"instrument_id": "coinbase|spot|BTC-USD"}
    route = SimpleNamespace(
        instrument=instrument,
        adapter=adapter,
        provider="coinbase",
        provider_symbol="BTC-USD",
        instrument_id="coinbase|spot|BTC-USD",
        instrument_key="BTC-USD",
        fingerprint="coinbase|spot|BTC-USD",
    )
    deps = SimpleNamespace(
        stream_bar_payload=lambda bar, *, instrument: {
            **stream_payload.stream_bar_payload(bar),
            "bar_slot_authoritative": False,
            "bar_slot_schedule_state": "unknown",
        },
        store_factory=lambda: object(),
    )
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route=route,
        interval="3m",
        generation=1,
        deps=deps,
    )
    consumer = _chart_consumer()
    coordinator.consumers = {consumer.queue: consumer}
    published: list[list[dict]] = []

    def publish(payloads, **_kwargs):
        order.append("publish")
        published.append(payloads)

    coordinator._publish_bar_payloads = publish
    source_bars = [
        Bar(
            "BTC-USD",
            datetime(2026, 7, 18, 13, minute, tzinfo=UTC),
            100 + minute,
            101 + minute,
            99 + minute,
            100.5 + minute,
            1,
            "1m",
            "coinbase",
        )
        for minute in (18, 19, 20)
    ]

    coordinator._handle_provider_bars(
        source_bars,
        datetime(2026, 7, 18, 13, 21, 1, tzinfo=UTC),
    )

    assert order == ["publish", "commit"]
    assert len(published[0]) == 1
    assert published[0][0]["ts"] == "2026-07-18T13:18:00+00:00"
    assert published[0][0]["open"] == 118.0
    assert published[0][0]["close"] == 120.5
    assert submitted == [(adapter, instrument, "1m", deps.store_factory, source_bars)]


def test_canonical_commit_publishes_bar_then_forces_post_commit_quality_checkpoint(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []
    bar = Bar(
        "ES",
        datetime(2026, 7, 18, 13, 20, tzinfo=UTC),
        100.0,
        101.0,
        99.5,
        100.5,
        10,
        "5m",
        "ibkr",
    )
    route = SimpleNamespace(
        instrument={"display": "ES"},
        provider="ibkr",
        provider_symbol="ESU6",
        instrument_id="instrument-v1",
        instrument_key="ES",
        fingerprint="route-v1",
    )
    deps = SimpleNamespace(
        stream_bar_payload=lambda item, *, instrument: {
            **stream_payload.stream_bar_payload(item),
            "bar_slot": 105,
            "bar_slot_authoritative": True,
            "bar_slot_schedule_state": "verified",
        }
    )
    tracker = ChartStreamPayloadTracker(
        parse_stream_ts=stream_payload.parse_stream_ts,
        stream_bar_signature=stream_payload.stream_bar_signature,
        initial_ts=datetime(2026, 7, 18, 14, 5, tzinfo=UTC),
    )
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(route, "5m", 3, deps)
    consumer = _chart_consumer(tracker=tracker)
    coordinator.consumers = {consumer.queue: consumer}
    coordinator._publish_bar_payloads = lambda payloads, **kwargs: calls.append(("publish", kwargs))
    coordinator._schedule_checkpoint = lambda range_, *, force=False: calls.append(
        ("checkpoint", (range_, force))
    )
    monkeypatch.setattr(
        chart_stream_coordinator,
        "provider_vwap_session",
        lambda *_args, **_kwargs: SimpleNamespace(available=False),
    )
    event = chart_stream_coordinator.ChartBarsUpdatedEvent(
        provider="ibkr",
        instrument_id="instrument-v1",
        interval="5m",
        generation=8,
        route_fingerprint="route-v1",
        reason="canonical_commit",
        bars=(bar,),
        gap={},
        repair={},
    )

    coordinator._handle_canonical_events(7, 8, [event])

    assert calls[0][0] == "publish"
    assert calls[0][1]["canonical_revision"] == 8
    assert calls[1] == ("checkpoint", ("3d", True))
    assert tracker.invalidated_timestamps == (bar.ts.isoformat(),)


def test_coverage_expansion_event_forces_recovery_checkpoint_without_guessed_slots() -> None:
    tracker = ChartStreamPayloadTracker(
        parse_stream_ts=stream_payload.parse_stream_ts,
        stream_bar_signature=stream_payload.stream_bar_signature,
        initial_ts=datetime(2026, 7, 18, 14, 5, tzinfo=UTC),
    )
    route = SimpleNamespace(
        instrument={"display": "ES"},
        provider="ibkr",
        provider_symbol="ESU6",
        instrument_id="instrument-v1",
        instrument_key="ES",
        fingerprint="route-v1",
    )
    coordinator = chart_stream_coordinator.ChartStreamCoordinator(
        route,
        "5m",
        3,
        SimpleNamespace(parse_iso_ts=stream_payload.parse_iso_ts),
    )
    consumer = _chart_consumer(tracker=tracker)
    coordinator.consumers = {consumer.queue: consumer}
    checkpoints: list[tuple[str, bool]] = []
    coordinator._schedule_checkpoint = lambda range_, *, force=False: checkpoints.append(
        (range_, force)
    )
    event = chart_stream_coordinator.ChartBarsUpdatedEvent(
        provider="ibkr",
        instrument_id="instrument-v1",
        interval="5m",
        generation=9,
        route_fingerprint="route-v1",
        reason="history_coverage_commit",
        bars=(),
        gap={},
        repair={},
    )

    coordinator._handle_canonical_events(8, 9, [event])

    assert tracker.invalidated_timestamps == ()
    assert checkpoints == [("3d", True)]


def test_checkpoint_reads_exact_coverage_without_repairing_in_realtime_lane() -> None:
    router_source = inspect.getsource(streams_router.create_stream_router)
    websocket_source = inspect.getsource(chart_stream_ws.run_chart_stream)
    release_source = inspect.getsource(chart_stream_ws._release_chart_stream_coordinator)
    coordinator_source = inspect.getsource(chart_stream_coordinator.ChartStreamCoordinator)
    hot_source = inspect.getsource(
        chart_stream_coordinator.ChartStreamCoordinator._handle_provider_bars
    )
    checkpoint_source = inspect.getsource(
        chart_stream_coordinator.ChartStreamCoordinator._load_checkpoint
    )

    assert "await run_chart_stream(" in router_source
    assert "acquire_coordinator" in websocket_source
    assert "range_=range_" in websocket_source
    assert "asyncio.create_task" in websocket_source
    assert "_CHART_COORDINATOR_RELEASE_TASKS.add" in websocket_source
    assert "await_cancellation_deferred_task(" in websocket_source
    assert "await asyncio.shield(release_task)" not in websocket_source
    assert "await chart_stream.release_coordinator" in release_source
    assert "grace_seconds=grace_seconds" in release_source
    assert "await coordinator.stop()" in release_source
    assert "coalesced_load_confirmed_chart_bars" not in websocket_source
    assert "async_chart_live_bars" not in websocket_source
    assert "coalesced_load_confirmed_chart_bars" in checkpoint_source
    assert "run_physical_executor_call" in checkpoint_source
    assert "run_in_executor" not in checkpoint_source
    assert "chart_recovery_snapshot" in checkpoint_source
    assert "schedule_chart_cached_gap_repair" not in coordinator_source
    assert 'history_coverage.get("repair")' in coordinator_source
    assert "chart_commits.submit" in hot_source
    assert "await " not in hot_source
    assert "write_bars" not in hot_source
    publish_source = inspect.getsource(
        chart_stream_coordinator.ChartStreamCoordinator._publish_bar_payloads
    )
    assert "tracker.remember" not in publish_source
    assert 'await send_stream_json(websocket, payload, "chart")' in websocket_source
    assert "consumer.tracker.remember" in websocket_source


def test_chart_checkpoint_forwards_one_frozen_bounded_window(monkeypatch) -> None:
    async def scenario() -> None:
        observed: list[object] = []
        recovery_args: list[tuple] = []
        route = SimpleNamespace(
            provider="ibkr",
            instrument={"instrument_id": "instrument-v1"},
            instrument_id="instrument-v1",
            fingerprint="route-v1",
            adapter=SimpleNamespace(
                capabilities=SimpleNamespace(
                    native_chart_stream=True,
                    chart_tail_polling=False,
                )
            ),
        )
        deps = SimpleNamespace(
            store_factory=lambda: object(),
            parse_iso_ts=stream_payload.parse_iso_ts,
            chart_recovery_snapshot=lambda *args: recovery_args.append(args) or "snapshot",
        )

        async def load(*_args, **kwargs):
            observed.append(kwargs["window"])
            return SimpleNamespace(bars=[], warning="", coverage={}, canonical_generation=8)

        monkeypatch.setattr(
            chart_stream_coordinator,
            "coalesced_load_confirmed_chart_bars",
            load,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: 7,
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route=route,
            interval="5m",
            generation=1,
            deps=deps,
        )

        base_window = chart_stream_coordinator.chart_history_range_window("31d", "5m")
        assert base_window.starts_at is not None
        required_timestamp = (base_window.starts_at - timedelta(days=1)).isoformat()
        (
            snapshot,
            warning,
            range_,
            generation,
            coverage,
            required_timestamps,
        ) = await coordinator._load_checkpoint("31d", (required_timestamp,))

        assert (snapshot, warning, range_, generation, coverage, required_timestamps) == (
            "snapshot",
            "",
            "31d",
            8,
            {},
            (required_timestamp,),
        )
        assert len(observed) == 1
        window = observed[0]
        assert window.range_key == "31d"
        assert window.starts_at is not None
        assert window.starts_at <= base_window.starts_at - timedelta(days=1, minutes=5)
        assert window.starts_at < window.ends_at
        assert recovery_args[0][4] == (required_timestamp,)

    asyncio.run(scenario())


def test_stale_client_cursor_becomes_one_explicit_bounded_repair_demand(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        observed: list[object] = []
        route = SimpleNamespace(
            provider="ibkr",
            instrument={"instrument_id": "instrument-v1"},
            instrument_id="instrument-v1",
            fingerprint="route-v1",
            adapter=SimpleNamespace(
                capabilities=SimpleNamespace(
                    native_chart_stream=True,
                    chart_tail_polling=False,
                )
            ),
        )
        deps = SimpleNamespace(
            store_factory=lambda: object(),
            parse_iso_ts=stream_payload.parse_iso_ts,
            chart_recovery_snapshot=lambda *_args: "snapshot",
        )

        async def load(*_args, **kwargs):
            observed.append(kwargs["repair_window"])
            return SimpleNamespace(
                bars=[],
                warning="",
                canonical_generation=7,
                coverage={
                    "repair": {
                        "status": "scheduled",
                        "requested_from": kwargs["repair_window"].starts_at.isoformat(),
                    }
                },
            )

        monkeypatch.setattr(
            chart_stream_coordinator,
            "coalesced_load_confirmed_chart_bars",
            load,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: 7,
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route=route,
            interval="5m",
            generation=1,
            deps=deps,
        )
        stale_from = chart_stream_coordinator.interval_bucket(
            datetime.now(tz=UTC) - timedelta(hours=20),
            "5m",
        )
        coordinator._stale_tail_repair_from["5d"] = stale_from

        await coordinator._load_checkpoint("5d")

        assert len(observed) == 1
        repair_window = observed[0]
        assert repair_window.starts_at == stale_from
        assert repair_window.ends_at > repair_window.starts_at
        assert repair_window.ends_at <= chart_stream_coordinator.interval_bucket(
            datetime.now(tz=UTC),
            "5m",
        )
        assert "5d" not in coordinator._stale_tail_repair_from

    asyncio.run(scenario())


def test_checkpoint_forwards_exact_history_repair_state_without_schedule_gating(
    monkeypatch,
) -> None:
    async def run() -> None:
        published_repair: list[dict[str, object]] = []
        published_quality: list[dict[str, object]] = []
        latest_ts = datetime(2026, 7, 20, 13, 55, tzinfo=UTC)
        route = SimpleNamespace(
            instrument={"instrument_id": "instrument-v1"},
            provider="ibkr",
            provider_symbol="ESU6",
            instrument_id="instrument-v1",
            instrument_key="ES",
            fingerprint="route-v1",
        )
        deps = SimpleNamespace(
            store_factory=lambda: object(),
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(route, "5m", 3, deps)

        def publish(*_args, **kwargs):
            published_quality.append(kwargs["chart_data_quality"])
            published_repair.append(kwargs["gap_repair"])

        coordinator._publish_bar_payloads = publish
        monkeypatch.setattr(
            chart_stream_coordinator,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: 7,
        )
        monkeypatch.setattr(
            chart_stream_coordinator,
            "require_chart_bars_generation",
            lambda expected, *_args, **_kwargs: expected,
        )
        cases = (
            (
                {
                    "empty_history": True,
                    "market_closed": False,
                    "session_unknown": False,
                    "latest_ts": latest_ts,
                },
                {"status": "scheduled", "requested_from": "2026-07-19T00:00:00+00:00"},
            ),
            (
                {"empty_history": False, "market_closed": False, "session_unknown": False},
                {"status": "running"},
            ),
            (
                {"empty_history": False, "market_closed": True, "session_unknown": False},
                {"status": "not_needed"},
            ),
            (
                {"empty_history": False, "market_closed": False, "session_unknown": False},
                {"status": "stale_snapshot"},
            ),
        )
        for quality, repair in cases:

            async def checkpoint_result():
                return (
                    SimpleNamespace(
                        payloads=[],
                        quality=quality,
                        expected_live_slot=None,
                        expected_live_close=None,
                        future_axis=None,
                    ),
                    "",
                    "3d",
                    7,
                    {"state": "partial", "repair": repair},
                    (),
                )

            task = asyncio.create_task(checkpoint_result())
            await task
            coordinator._checkpoint_tasks["3d"] = task
            coordinator._flush_checkpoints()

        assert published_repair == [repair for _quality, repair in cases]
        assert published_quality[0]["latest_ts"] == latest_ts.isoformat()

    asyncio.run(run())


def test_forced_checkpoint_followup_survives_cancel_and_failure() -> None:
    async def run() -> None:
        route = SimpleNamespace(
            instrument={"display": "ES"},
            provider="ibkr",
            provider_symbol="ESU6",
            instrument_id="instrument-v1",
            instrument_key="ES",
            fingerprint="route-v1",
        )
        deps = SimpleNamespace(
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        )
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(route, "5m", 3, deps)
        consumer_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=4)
        coordinator.consumers = {
            consumer_queue: _chart_consumer(queue=consumer_queue),
        }

        pending = asyncio.create_task(asyncio.Event().wait())
        coordinator._checkpoint_tasks["3d"] = pending
        coordinator._schedule_checkpoint("3d", force=True)
        assert coordinator._checkpoint_followups == {"3d"}
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        rescheduled: list[tuple[str, bool]] = []
        coordinator._schedule_checkpoint = lambda range_, *, force=False: rescheduled.append(
            (range_, force)
        )
        coordinator._flush_checkpoints()

        async def fail_checkpoint():
            raise RuntimeError("checkpoint failed")

        failed = asyncio.create_task(fail_checkpoint())
        await asyncio.gather(failed, return_exceptions=True)
        coordinator._checkpoint_tasks["3d"] = failed
        coordinator._checkpoint_followups.add("3d")
        coordinator._flush_checkpoints()

        assert rescheduled == [("3d", True), ("3d", True)]

    asyncio.run(run())


@pytest.mark.parametrize("during_load", [False, True])
@pytest.mark.parametrize("writing", [False, True])
def test_checkpoint_generation_conflicts_share_bounded_transient_classification(
    monkeypatch, during_load, writing
) -> None:
    async def run() -> None:
        route = SimpleNamespace(
            instrument={"display": "ES"},
            provider="ibkr",
            provider_symbol="ESU6",
            instrument_id="instrument-transient",
            instrument_key="ES",
            fingerprint="route-v1",
        )
        warnings = []
        coordinator = chart_stream_coordinator.ChartStreamCoordinator(
            route,
            "5m",
            3,
            SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: warnings.append(args))),
        )
        queue = asyncio.Queue(maxsize=4)
        coordinator.consumers = {queue: _chart_consumer(queue=queue)}
        conflict = chart_stream_coordinator.ChartBarsGenerationChanged(
            expected=7,
            observed=7 if writing else 8,
            write_in_progress=writing,
        )

        async def checkpoint():
            if during_load:
                raise conflict
            return (SimpleNamespace(payloads=[]), "", "3d", 7, {}, ())

        def require(*_args, **_kwargs):
            raise conflict

        monkeypatch.setattr(chart_stream_coordinator, "require_chart_bars_generation", require)
        task = asyncio.create_task(checkpoint())
        await asyncio.gather(task, return_exceptions=True)
        coordinator._checkpoint_tasks["3d"] = task
        coordinator._checkpoint_followups.add("3d")
        now = chart_stream_coordinator.time.monotonic()
        coordinator._flush_checkpoints()
        coordinator._schedule_checkpoint("3d")
        assert not coordinator._checkpoint_tasks
        assert (
            coordinator._next_checkpoint_at["3d"]
            >= now + chart_stream_coordinator.CHART_STREAM_DB_CHECKPOINT_SECONDS
        )
        assert not warnings
        assert queue.empty()

    asyncio.run(run())


def test_quote_trade_candles_use_compact_sequenced_delta_not_full_quote_snapshot() -> None:
    module_source = inspect.getsource(quote_stream_ws)
    router_source = inspect.getsource(streams_router.create_stream_router)
    producer_source = "\n".join(
        (
            inspect.getsource(quote_stream_ws._run_quote_snapshot_producer),
            inspect.getsource(quote_stream_ws._QuoteProducerRuntime),
        )
    )
    event_reader_source = inspect.getsource(quote_stream_ws._read_quote_trade_events)
    transport_source = "\n".join(
        (
            inspect.getsource(quote_stream_ws.run_quote_stream),
            inspect.getsource(quote_stream_ws._QuoteStreamConsumer),
        )
    )
    quote_router_block = router_source.split('@router.websocket("/ws/quotes")', 1)[1].split(
        '@router.websocket("/ws/chart")',
        1,
    )[0]

    assert "await run_quote_stream(" in router_source
    assert "routes: str" in quote_router_block
    assert "instrument_ids" not in quote_router_block
    assert "expected_route_fingerprints" not in quote_router_block
    assert "parse_route_selection(routes)" in transport_source
    assert "refresh_instruments" in producer_source
    assert '",".join' not in producer_source
    assert "self.deps.record_quote_execution_snapshots(rows)" in producer_source
    assert "events, read_states = _read_quote_trade_events(" in producer_source
    assert "adapter.read_quote_trade_events(" in event_reader_source
    assert "routes_by_provider" in event_reader_source
    assert "retryable_providers=retryable_providers" in producer_source
    assert "if provider in retryable_providers:" in producer_source
    assert "self.producer.live_trade_sequences[provider] = read_sequence" in producer_source
    assert "route.adapter.capabilities.quote_trade_events" in module_source
    assert 'route.provider == "ibkr"' not in producer_source
    assert '"source": f"{route.provider}:trade-preview"' in module_source
    assert '"type": "live_candle_delta"' in transport_source
    assert "revision = self.producer.live_bars_revision" in transport_source
    assert '"sequence": revision' in transport_source
    assert '"loss_after_sequence": loss_sequence or None' in transport_source
    assert "if loss_sequence" in transport_source
    assert "list(self.producer.live_bars.values())" in transport_source
    assert '"bars": bars' in transport_source
    assert "self.producer.live_bar_revisions.get(identity, 0)" in transport_source
    assert '"type": "quote_snapshot"' in transport_source
    assert '"type": "quote_delta"' in transport_source
    assert '"type": "quote_heartbeat"' in transport_source
    assert '"type": "option_targets_snapshot"' in transport_source
    assert "quote_sequence += 1" in transport_source
    assert "_exact_quote_rows(rows, self.expected_row_identities)" in transport_source
    quote_snapshot_block = transport_source.split('"type": "quote_snapshot"', 1)[1]
    assert 'message["live_bars"]' not in quote_snapshot_block
    assert "record_quote_execution_snapshots(rows)" not in transport_source


def test_route_rollover_refreshes_qualified_catalog_before_resubscribe() -> None:
    chart_transport = inspect.getsource(chart_stream_ws.run_chart_stream)
    quote_transport = inspect.getsource(quote_stream_ws._quote_stream_consumer)
    browser = open(
        "src/aef_terminal/ui/assets/js/20-live-stream-connect.js",
        encoding="utf-8",
    ).read()

    assert '"actual_route_fingerprint": route.fingerprint' in chart_transport
    assert "route_fingerprint=expected" in chart_transport
    assert (
        'expected = expected_route_fingerprint if isinstance(expected_route_fingerprint, str) else ""'
        in chart_transport
    )
    assert 'str(expected_route_fingerprint or "")' not in chart_transport
    assert 'code="QUOTE_STREAM_ROUTE_CHANGED"' in quote_transport
    assert "requires_resubscribe=True" in quote_transport
    assert (
        'recoverStaleChartStream(message.message || "provider route changed", { refreshRoute: true })'
        in browser
    )
    assert 'if (message.type === "quote_status")' in browser
    assert 'if (typeof loadInstruments === "function") await loadInstruments();' in browser
