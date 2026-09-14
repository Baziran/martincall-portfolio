from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from aef_terminal.data.instrument_identity import InstrumentIdentityError
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui import wiring as ui_wiring
from aef_terminal.ui import runtime_instruments
from aef_terminal.ui.routers import browser_capture as browser_capture_router
from aef_terminal.ui.services import browser_capture
from tests.provider_payloads import ibkr_future_payload
from tests.source_contracts import python_source_contains


_VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _capture_client_id(sequence: int = 1) -> str:
    return f"mc-00000000-0000-4000-8000-{sequence:012x}"


class _RecordingLoop:
    def __init__(self) -> None:
        self.scheduled = threading.Event()

    def call_soon_threadsafe(self, callback, *args: Any) -> None:
        callback(*args)
        self.scheduled.set()


class _ClosedLoop:
    def call_soon_threadsafe(self, _callback, *_args: Any) -> None:
        raise RuntimeError("event loop is closed")


class _CaptureWebSocket:
    def __init__(
        self,
        *,
        origin: str = "http://terminal.test",
        host: str = "terminal.test",
    ) -> None:
        self.headers = {"origin": origin, "host": host}
        self.accepted = False
        self.messages: list[dict[str, Any]] = []
        self.message_sent = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.messages.append(payload)
        self.message_sent.set()

    async def close(self, *, code: int = 1000) -> None:
        self.closed_code = code

    async def receive(self) -> dict[str, Any]:
        await self.disconnect.wait()
        return {"type": "websocket.disconnect", "code": 1000}


class _CaptureErrorWebSocket:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self._sent = False

    async def receive(self) -> dict[str, Any]:
        if not self._sent:
            self._sent = True
            return {
                "type": "websocket.receive",
                "text": json.dumps(self._payload),
            }
        await asyncio.Future()
        raise AssertionError("unreachable")


def _binary_frame(request_id: str, content: bytes = _VALID_PNG) -> bytes:
    return (
        browser_capture.BROWSER_CAPTURE_BINARY_PREFIX + request_id.encode("ascii") + b":" + content
    )


def _register_session(
    broker: browser_capture.BrowserCaptureBroker,
    *,
    client_id: str,
    instrument_id: str = "instrument:exact",
    route_fingerprint: str = "route:exact",
    timeframe: str = "5m",
) -> tuple[Any, _RecordingLoop]:
    loop = _RecordingLoop()
    session = broker.register(
        client_id=client_id,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
        loop=loop,
    )
    return session, loop


def test_browser_capture_client_id_matches_the_page_uuid_v4_contract() -> None:
    client_id = _capture_client_id()

    assert browser_capture._require_capture_client_id(client_id) == client_id
    for invalid in (
        "mc-test-a1b2",
        client_id.removeprefix("mc-"),
        "mc-00000000-0000-1000-8000-000000000001",
        "mc-00000000-0000-4000-8000-000000000001 ",
    ):
        with pytest.raises(ValueError, match="BROWSER_CAPTURE_CLIENT_ID_INVALID"):
            browser_capture._require_capture_client_id(invalid)


@pytest.mark.parametrize("scope", sorted(browser_capture.BROWSER_CAPTURE_SCOPES))
def test_browser_capture_broker_round_trip_preserves_exact_scope_and_identity(
    scope: str,
) -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, loop = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:exact",
            scope,
            expected_route_fingerprint="route:exact",
            timeout=1.0,
        )
        assert loop.scheduled.wait(timeout=1.0)
        message = session.outbox.get_nowait()
        assert message == {
            "type": "capture_request",
            "request_id": message["request_id"],
            "scope": scope,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "timeframe": "5m",
        }

        broker.resolve_binary(
            session.session_id,
            _binary_frame(message["request_id"]),
        )
        result = requested.result(timeout=1.0)

    assert result is not None
    assert result["ok"] is True
    assert result["instrument_id"] == "instrument:exact"
    assert result["route_fingerprint"] == "route:exact"
    assert result["scope"] == scope
    assert result["timeframe"] == "5m"
    assert result["client_id"] == _capture_client_id()
    assert result["content"] == _VALID_PNG
    assert result["width"] == 1
    assert result["height"] == 1
    assert result["captured_at"].endswith("+00:00")


def test_browser_capture_broker_replaces_duplicate_tab_session() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    first, _ = _register_session(
        broker,
        client_id=_capture_client_id(),
        instrument_id="instrument:first",
    )
    replacement, _ = _register_session(
        broker,
        client_id=_capture_client_id(),
        instrument_id="instrument:second",
    )

    assert first.closed is True
    assert first.outbox.get_nowait() is None
    assert replacement.closed is False
    assert broker.session_count() == 1
    assert broker.session_count("instrument:first") == 0
    assert broker.session_count("instrument:second") == 1

    broker.unregister(replacement.session_id)
    broker.unregister(replacement.session_id)
    assert replacement.closed is True
    assert broker.session_count() == 0


def test_browser_capture_sender_closes_replaced_socket() -> None:
    async def scenario() -> None:
        broker = browser_capture.BrowserCaptureBroker()
        session = broker.register(
            client_id=_capture_client_id(),
            instrument_id="instrument:exact",
            route_fingerprint="route:exact",
            timeframe="5m",
            loop=asyncio.get_running_loop(),
        )
        websocket = _CaptureWebSocket()
        sender = asyncio.create_task(browser_capture._browser_capture_sender(websocket, session))

        broker.unregister(session.session_id)
        await asyncio.wait_for(sender, timeout=1.0)

        assert websocket.closed_code == 1000

    asyncio.run(scenario())


def test_browser_capture_unregister_releases_pending_request() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, loop = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:exact",
            "price",
            expected_route_fingerprint="route:exact",
            timeout=1.0,
        )
        assert loop.scheduled.wait(timeout=1.0)
        assert session.outbox.get_nowait()["type"] == "capture_request"

        broker.unregister(session.session_id)
        assert requested.result(timeout=1.0) == {
            "ok": False,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "scope": "price",
            "error_code": "BROWSER_CAPTURE_CONNECTION_CLOSED",
        }

    assert session.closed is True
    assert broker.session_count() == 0


def test_browser_capture_session_priority_is_focused_then_visible_then_recent() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    hidden, _ = _register_session(broker, client_id=_capture_client_id(1))
    visible, _ = _register_session(broker, client_id=_capture_client_id(2))
    focused, _ = _register_session(broker, client_id=_capture_client_id(3))

    broker.update_state(
        hidden.session_id,
        visible=False,
        focused=False,
        timeframe="5m",
    )
    broker.update_state(
        visible.session_id,
        visible=True,
        focused=False,
        timeframe="5m",
    )
    broker.update_state(
        focused.session_id,
        visible=False,
        focused=True,
        timeframe="5m",
    )

    assert broker._selected_session("instrument:exact", "route:exact") == (
        focused,
        None,
    )

    broker.update_state(
        focused.session_id,
        visible=False,
        focused=False,
        timeframe="5m",
    )
    assert broker._selected_session("instrument:exact", "route:exact") == (
        visible,
        None,
    )

    broker.update_state(
        visible.session_id,
        visible=False,
        focused=False,
        timeframe="5m",
    )
    assert broker._selected_session("instrument:exact", "route:exact") == (
        visible,
        None,
    )


def test_open_hidden_browser_capture_session_does_not_expire_without_heartbeat() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, _ = _register_session(
        broker,
        client_id=_capture_client_id(),
    )
    session.visible = False
    session.focused = False
    session.last_seen_at = -1_000_000.0

    assert broker._selected_session("instrument:exact", "route:exact") == (
        session,
        None,
    )


def test_browser_capture_selection_distinguishes_no_session_from_busy() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    assert broker._selected_session("instrument:exact", "route:exact") == (
        None,
        "BROWSER_CAPTURE_NO_SESSION",
    )

    session, _ = _register_session(
        broker,
        client_id=_capture_client_id(),
    )
    session.busy_request_id = "reserved"
    session.busy_started_at = session.last_seen_at
    assert broker._selected_session("instrument:exact", "route:exact") == (
        None,
        "BROWSER_CAPTURE_SESSION_BUSY",
    )

    session.busy_started_at = (
        session.last_seen_at - browser_capture.BROWSER_CAPTURE_BUSY_STALE_SECONDS - 1.0
    )
    assert broker._selected_session("instrument:exact", "route:exact") == (
        session,
        None,
    )


def test_browser_capture_timeout_keeps_tab_reserved_until_late_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, _ = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with caplog.at_level(logging.WARNING):
        assert broker.request(
            "instrument:exact",
            "price",
            expected_route_fingerprint="route:exact",
            timeout=0.001,
        ) == {
            "ok": False,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "scope": "price",
            "error_code": "BROWSER_CAPTURE_TIMEOUT",
        }
    request = session.outbox.get_nowait()
    assert session.busy_request_id == request["request_id"]
    assert broker.request(
        "instrument:exact",
        "terminal",
        expected_route_fingerprint="route:exact",
        timeout=0.001,
    ) == {
        "ok": False,
        "instrument_id": "instrument:exact",
        "route_fingerprint": "route:exact",
        "scope": "terminal",
        "error_code": "BROWSER_CAPTURE_SESSION_BUSY",
    }
    assert "BROWSER_CAPTURE_TIMEOUT" in caplog.text
    assert "instrument_id=instrument:exact" in caplog.text
    assert "scope=price" in caplog.text
    assert f"client_id={_capture_client_id()}" in caplog.text
    assert "elapsed_seconds=" in caplog.text

    broker.reject(
        session.session_id,
        request["request_id"],
        error_code="BROWSER_CAPTURE_SCOPE_CHANGED",
    )
    assert session.busy_request_id == ""
    assert session.busy_started_at == 0.0


def test_browser_capture_client_error_is_preserved_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, loop = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:exact",
            "terminal",
            expected_route_fingerprint="route:exact",
            timeout=1.0,
        )
        assert loop.scheduled.wait(timeout=1.0)
        message = session.outbox.get_nowait()
        with caplog.at_level(logging.WARNING):
            broker.reject(
                session.session_id,
                message["request_id"],
                error_code="BROWSER_CAPTURE_SVG_RENDER_FAILED",
            )
        assert requested.result(timeout=1.0) == {
            "ok": False,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "scope": "terminal",
            "error_code": "BROWSER_CAPTURE_SVG_RENDER_FAILED",
        }

    assert "BROWSER_CAPTURE_SVG_RENDER_FAILED" in caplog.text
    assert "instrument_id=instrument:exact" in caplog.text
    assert "scope=terminal" in caplog.text
    assert f"client_id={_capture_client_id()}" in caplog.text
    assert "elapsed_seconds=" in caplog.text
    assert session.busy_request_id == ""


def test_browser_capture_invalid_client_error_code_is_typed() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, loop = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:exact",
            "price",
            expected_route_fingerprint="route:exact",
            timeout=1.0,
        )
        assert loop.scheduled.wait(timeout=1.0)
        message = session.outbox.get_nowait()
        broker.reject(
            session.session_id,
            message["request_id"],
            error_code="not-a-protocol-code",
        )
        assert requested.result(timeout=1.0)["error_code"] == (
            "BROWSER_CAPTURE_CLIENT_ERROR_INVALID"
        )


def test_browser_capture_receiver_forwards_client_error_code() -> None:
    async def scenario() -> dict[str, Any]:
        broker = browser_capture.BrowserCaptureBroker()
        session = broker.register(
            client_id=_capture_client_id(),
            instrument_id="instrument:exact",
            route_fingerprint="route:exact",
            timeframe="5m",
            loop=asyncio.get_running_loop(),
        )
        requested = asyncio.create_task(
            asyncio.to_thread(
                broker.request,
                "instrument:exact",
                "price",
                expected_route_fingerprint="route:exact",
                timeout=1.0,
            )
        )
        message = await asyncio.wait_for(session.outbox.get(), timeout=1.0)
        websocket = _CaptureErrorWebSocket(
            {
                "type": "capture_error",
                "request_id": message["request_id"],
                "error_code": "BROWSER_CAPTURE_CANVAS_UNAVAILABLE",
            }
        )
        receiver = asyncio.create_task(
            browser_capture._browser_capture_receiver(
                websocket,  # type: ignore[arg-type]
                session,
                broker,
            )
        )
        result = await asyncio.wait_for(requested, timeout=1.0)
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        return result

    assert asyncio.run(scenario()) == {
        "ok": False,
        "instrument_id": "instrument:exact",
        "route_fingerprint": "route:exact",
        "scope": "price",
        "error_code": "BROWSER_CAPTURE_CANVAS_UNAVAILABLE",
    }


def test_browser_capture_pending_limit_has_typed_identity_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = browser_capture.BrowserCaptureBroker()
    monkeypatch.setattr(browser_capture, "BROWSER_CAPTURE_MAX_PENDING", 0)

    assert broker.request(
        "instrument:exact",
        "price",
        expected_route_fingerprint="route:exact",
    ) == {
        "ok": False,
        "instrument_id": "instrument:exact",
        "route_fingerprint": "route:exact",
        "scope": "price",
        "error_code": "BROWSER_CAPTURE_PENDING_LIMIT",
    }


def test_browser_capture_closed_loop_dispatch_has_typed_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session = broker.register(
        client_id=_capture_client_id(),
        instrument_id="instrument:exact",
        route_fingerprint="route:exact",
        timeframe="5m",
        loop=_ClosedLoop(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING):
        assert broker.request(
            "instrument:exact",
            "price",
            expected_route_fingerprint="route:exact",
        ) == {
            "ok": False,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "scope": "price",
            "error_code": "BROWSER_CAPTURE_DISPATCH_FAILED",
        }

    assert session.busy_request_id == ""
    assert session.busy_started_at == 0.0
    assert "BROWSER_CAPTURE_DISPATCH_FAILED" in caplog.text


def test_browser_capture_timeout_and_busy_lease_are_bounded() -> None:
    assert browser_capture.BROWSER_CAPTURE_REQUEST_TIMEOUT_SECONDS == 15.0
    assert (
        browser_capture.BROWSER_CAPTURE_BUSY_STALE_SECONDS
        > browser_capture.BROWSER_CAPTURE_REQUEST_TIMEOUT_SECONDS
    )


def test_browser_capture_rejects_invalid_scope_and_nonexact_identity_values() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    _register_session(broker, client_id=_capture_client_id())

    with pytest.raises(ValueError, match="BROWSER_CAPTURE_SCOPE_INVALID"):
        broker.request(
            "instrument:exact",
            "chart",
            expected_route_fingerprint="route:exact",
        )
    with pytest.raises(InstrumentIdentityError, match="exact string"):
        broker.request(  # type: ignore[arg-type]
            123,
            "price",
            expected_route_fingerprint="route:exact",
        )
    assert broker.request(
        "instrument:exact ",
        "price",
        expected_route_fingerprint="route:exact",
    ) == {
        "ok": False,
        "instrument_id": "instrument:exact ",
        "route_fingerprint": "route:exact",
        "scope": "price",
        "error_code": "BROWSER_CAPTURE_NO_SESSION",
    }
    assert broker.request(
        "instrument:exact",
        "price",
        expected_route_fingerprint="route:stale",
    ) == {
        "ok": False,
        "instrument_id": "instrument:exact",
        "route_fingerprint": "route:stale",
        "scope": "price",
        "error_code": "BROWSER_CAPTURE_NO_SESSION",
    }


def test_public_capture_request_uses_current_runtime_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    calls: list[tuple[str, str, str]] = []

    def lookup(instrument_id: str) -> dict[str, Any]:
        if instrument_id != route.instrument_id:
            raise AssertionError(instrument_id)
        return instrument

    monkeypatch.setattr(
        runtime_instruments,
        "lookup_runtime_instrument",
        lookup,
    )

    def request(
        instrument_id: str,
        scope: str,
        *,
        expected_route_fingerprint: str,
    ) -> dict[str, Any]:
        calls.append((instrument_id, scope, expected_route_fingerprint))
        return {"ok": True}

    monkeypatch.setattr(browser_capture.BROWSER_CAPTURE_BROKER, "request", request)

    assert browser_capture.request_browser_capture(route.instrument_id, "terminal") == {"ok": True}
    assert calls == [(route.instrument_id, "terminal", route.fingerprint)]


def test_browser_capture_png_validation_accepts_png_and_rejects_bad_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content, width, height = browser_capture._validated_png(_VALID_PNG)
    assert content == _VALID_PNG
    assert (width, height) == (1, 1)

    with pytest.raises(ValueError, match="BROWSER_CAPTURE_PNG_INVALID"):
        browser_capture._validated_png(b"not-a-png")
    with pytest.raises(ValueError, match="BROWSER_CAPTURE_PNG_INVALID"):
        browser_capture._validated_png(_VALID_PNG[:-12])
    corrupted_crc = bytearray(_VALID_PNG)
    corrupted_crc[44] ^= 0x01
    with pytest.raises(ValueError, match="BROWSER_CAPTURE_PNG_INVALID"):
        browser_capture._validated_png(bytes(corrupted_crc))

    invalid_dimensions = (
        _VALID_PNG[:16] + (1).to_bytes(4, "big") + (21).to_bytes(4, "big") + _VALID_PNG[24:]
    )
    with pytest.raises(ValueError, match="BROWSER_CAPTURE_DIMENSIONS_INVALID"):
        browser_capture._validated_png(invalid_dimensions)

    excessive_pixels = (
        _VALID_PNG[:16] + (3_000).to_bytes(4, "big") + (3_000).to_bytes(4, "big") + _VALID_PNG[24:]
    )
    with pytest.raises(ValueError, match="BROWSER_CAPTURE_DIMENSIONS_INVALID"):
        browser_capture._validated_png(excessive_pixels)

    monkeypatch.setattr(
        browser_capture,
        "BROWSER_CAPTURE_MAX_PNG_BYTES",
        len(_VALID_PNG) - 1,
    )
    with pytest.raises(ValueError, match="BROWSER_CAPTURE_PNG_INVALID"):
        browser_capture._validated_png(_VALID_PNG)


def test_invalid_png_frame_rejects_active_capture_without_leaking_busy_state() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, loop = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:exact",
            "price",
            expected_route_fingerprint="route:exact",
            timeout=1.0,
        )
        assert loop.scheduled.wait(timeout=1.0)
        message = session.outbox.get_nowait()

        broker.resolve_binary(
            session.session_id,
            _binary_frame(message["request_id"], b"not-a-png"),
        )
        assert requested.result(timeout=1.0) == {
            "ok": False,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "scope": "price",
            "error_code": "BROWSER_CAPTURE_PNG_INVALID",
        }

    assert session.busy_request_id == ""
    assert session.busy_started_at == 0.0


def test_malformed_binary_frame_returns_explicit_protocol_error() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    session, loop = _register_session(
        broker,
        client_id=_capture_client_id(),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:exact",
            "terminal",
            expected_route_fingerprint="route:exact",
            timeout=1.0,
        )
        assert loop.scheduled.wait(timeout=1.0)
        session.outbox.get_nowait()

        broker.resolve_binary(session.session_id, b"not-a-browser-capture-frame")
        assert requested.result(timeout=1.0) == {
            "ok": False,
            "instrument_id": "instrument:exact",
            "route_fingerprint": "route:exact",
            "scope": "terminal",
            "error_code": "BROWSER_CAPTURE_BINARY_FRAME_INVALID",
        }

    assert session.busy_request_id == ""
    assert session.busy_started_at == 0.0


def test_binary_frame_from_other_identity_cannot_consume_pending_capture() -> None:
    broker = browser_capture.BrowserCaptureBroker()
    expected, expected_loop = _register_session(
        broker,
        client_id=_capture_client_id(1),
        instrument_id="instrument:expected",
        route_fingerprint="route:expected",
    )
    other, _ = _register_session(
        broker,
        client_id=_capture_client_id(2),
        instrument_id="instrument:other",
        route_fingerprint="route:other",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        requested = executor.submit(
            broker.request,
            "instrument:expected",
            "price",
            expected_route_fingerprint="route:expected",
            timeout=1.0,
        )
        assert expected_loop.scheduled.wait(timeout=1.0)
        message = expected.outbox.get_nowait()
        frame = _binary_frame(message["request_id"])

        broker.resolve_binary(other.session_id, frame)
        assert message["request_id"] in broker._pending
        assert requested.done() is False

        broker.resolve_binary(expected.session_id, frame)
        result = requested.result(timeout=1.0)

    assert result is not None
    assert result["ok"] is True
    assert result["instrument_id"] == "instrument:expected"
    assert result["route_fingerprint"] == "route:expected"


def test_browser_capture_socket_registers_only_same_origin_exact_route() -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    broker = browser_capture.BrowserCaptureBroker()

    def lookup(instrument_id: str) -> dict[str, Any]:
        if instrument_id != route.instrument_id:
            raise AssertionError(instrument_id)
        return instrument

    async def scenario() -> None:
        websocket = _CaptureWebSocket()
        task = asyncio.create_task(
            browser_capture.run_browser_capture_socket(
                websocket,
                instrument_id=route.instrument_id,
                expected_route_fingerprint=route.fingerprint,
                timeframe="5m",
                client_id=_capture_client_id(),
                lookup_runtime_instrument=lookup,
                broker=broker,
            )
        )
        await asyncio.wait_for(websocket.message_sent.wait(), timeout=1.0)

        assert websocket.accepted is True
        assert websocket.messages == [
            {
                "type": "capture_ready",
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "timeframe": "5m",
            }
        ]
        assert broker.session_count(route.instrument_id) == 1

        websocket.disconnect.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert broker.session_count() == 0


def test_browser_capture_socket_drains_binary_worker_before_unregister() -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    resolve_started = threading.Event()
    release = threading.Event()
    resolve_finished = threading.Event()
    unregister_finished_states: list[bool] = []

    class Broker(browser_capture.BrowserCaptureBroker):
        def resolve_binary(self, _session_id: str, _payload: bytes) -> None:
            resolve_started.set()
            assert release.wait(timeout=2)
            resolve_finished.set()

        def unregister(self, session_id: str) -> None:
            unregister_finished_states.append(resolve_finished.is_set())
            super().unregister(session_id)

    class BinaryWebSocket(_CaptureWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.binary_sent = False

        async def receive(self) -> dict[str, Any]:
            if not self.binary_sent:
                self.binary_sent = True
                return {"type": "websocket.receive", "bytes": b"capture"}
            await asyncio.Future()
            raise AssertionError("unreachable")

    broker = Broker()

    async def scenario() -> None:
        task = asyncio.create_task(
            browser_capture.run_browser_capture_socket(
                BinaryWebSocket(),
                instrument_id=route.instrument_id,
                expected_route_fingerprint=route.fingerprint,
                timeframe="5m",
                client_id=_capture_client_id(),
                lookup_runtime_instrument=lambda _instrument_id: instrument,
                broker=broker,
            )
        )
        for _ in range(100):
            if resolve_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert resolve_started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert broker.session_count() == 1
        assert unregister_finished_states == []

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert resolve_finished.is_set() is True

    asyncio.run(scenario())
    assert broker.session_count() == 0
    assert unregister_finished_states == [True]


def test_browser_capture_socket_rejects_origin_and_route_identity_mismatch() -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)

    async def scenario() -> None:
        lookup_calls: list[str] = []

        def lookup(instrument_id: str) -> dict[str, Any]:
            lookup_calls.append(instrument_id)
            return instrument

        wrong_origin = _CaptureWebSocket(origin="https://attacker.test")
        await browser_capture.run_browser_capture_socket(
            wrong_origin,
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            timeframe="5m",
            client_id=_capture_client_id(),
            lookup_runtime_instrument=lookup,
            broker=browser_capture.BrowserCaptureBroker(),
        )
        assert wrong_origin.accepted is True
        assert wrong_origin.closed_code == 1008
        assert wrong_origin.messages == []
        assert lookup_calls == []

        wrong_route = _CaptureWebSocket()
        await browser_capture.run_browser_capture_socket(
            wrong_route,
            instrument_id=route.instrument_id,
            expected_route_fingerprint=f"{route.fingerprint} ",
            timeframe="5m",
            client_id=_capture_client_id(2),
            lookup_runtime_instrument=lookup,
            broker=browser_capture.BrowserCaptureBroker(),
        )
        assert wrong_route.accepted is True
        assert wrong_route.closed_code == 1008
        assert wrong_route.messages == []
        assert lookup_calls == [route.instrument_id]

    asyncio.run(scenario())


def test_browser_capture_uses_a_dedicated_websocket_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def run_socket(websocket: Any, **kwargs: Any) -> None:
        calls.append({"websocket": websocket, **kwargs})

    monkeypatch.setattr(
        browser_capture_router,
        "run_browser_capture_socket",
        run_socket,
    )

    def lookup(instrument_id: str) -> dict[str, Any]:
        return {"instrument_id": instrument_id}

    router = browser_capture_router.create_browser_capture_router(
        browser_capture_router.BrowserCaptureRouterDeps(
            lookup_runtime_instrument=lookup,
        )
    )
    routes = [
        route for route in router.routes if getattr(route, "path", "") == "/ws/browser-capture"
    ]
    assert len(routes) == 1
    wiring_source = inspect.getsource(ui_wiring.wire_app_routers)
    assert python_source_contains(
        wiring_source,
        "app.include_router(create_browser_capture_router(",
    )
    assert python_source_contains(
        wiring_source,
        "lookup_runtime_instrument=wiring.streams.instrument_lookup",
    )

    websocket = object()
    asyncio.run(
        routes[0].endpoint(
            websocket,
            instrument_id="instrument:exact",
            expected_route_fingerprint="route:exact",
            timeframe="5m",
            client_id=_capture_client_id(),
        )
    )
    assert calls == [
        {
            "websocket": websocket,
            "instrument_id": "instrument:exact",
            "expected_route_fingerprint": "route:exact",
            "timeframe": "5m",
            "client_id": _capture_client_id(),
            "lookup_runtime_instrument": lookup,
        }
    ]
