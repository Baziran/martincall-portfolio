from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from aef_terminal.ui.services import websocket_send


def _capture_metrics(monkeypatch):
    increments: list[tuple[str, dict]] = []
    observations: list[tuple[str, float, dict]] = []
    monkeypatch.setattr(
        websocket_send,
        "increment_metric",
        lambda name, **labels: increments.append((name, labels)),
    )
    monkeypatch.setattr(
        websocket_send,
        "observe_metric",
        lambda name, value, **labels: observations.append((name, value, labels)),
    )
    return increments, observations


def test_send_stream_json_uses_encoded_send_text_hot_path(monkeypatch) -> None:
    increments, observations = _capture_metrics(monkeypatch)

    class WebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_text(self, message: str) -> None:
            self.messages.append(message)

        async def send_bytes(self, _message: bytes) -> None:
            raise AssertionError("browser JSON transport must remain a text WebSocket frame")

        async def send_json(self, _payload) -> None:
            raise AssertionError("send_json fallback must not run when send_text is available")

    websocket = WebSocket()
    payload = {"type": "chart_bars", "price": 101.25, "label": "живой"}

    asyncio.run(websocket_send.send_stream_json(websocket, payload, "chart"))

    assert len(websocket.messages) == 1
    assert json.loads(websocket.messages[0]) == payload
    assert ": " not in websocket.messages[0]
    assert (
        "websocket_messages_total",
        {"stream": "chart", "message_type": "chart_bars", "status": "ok"},
    ) in increments
    assert any(
        name == "websocket_encode_seconds"
        and labels == {"stream": "chart", "message_type": "chart_bars"}
        for name, _value, labels in observations
    )
    assert any(
        name == "websocket_payload_bytes"
        and value == len(websocket.messages[0].encode("utf-8"))
        and labels == {"stream": "chart", "message_type": "chart_bars"}
        for name, value, labels in observations
    )
    assert any(
        name == "websocket_send_seconds" and labels == {"stream": "chart"}
        for name, _value, labels in observations
    )


def test_send_stream_json_falls_back_to_send_json_for_test_double(monkeypatch) -> None:
    increments, observations = _capture_metrics(monkeypatch)

    class WebSocket:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def send_json(self, payload: dict) -> None:
            self.payloads.append(payload)

    websocket = WebSocket()
    payload = {"type": "quote_snapshot", "rows": [{"price": 101.0}]}

    asyncio.run(websocket_send.send_stream_json(websocket, payload, "quotes"))

    assert websocket.payloads == [payload]
    assert (
        "websocket_messages_total",
        {"stream": "quotes", "message_type": "quote_snapshot", "status": "ok"},
    ) in increments
    assert not any(name == "websocket_payload_bytes" for name, _value, _labels in observations)
    assert not any(name == "websocket_encode_seconds" for name, _value, _labels in observations)


def test_gex_history_snapshot_uses_bulk_encoder_lane(monkeypatch) -> None:
    _capture_metrics(monkeypatch)
    bulk_values: list[bool] = []

    class WebSocket:
        async def send_text(self, _message: str) -> None:
            return None

    async def capture_encode(payload, *, bulk: bool):
        bulk_values.append(bulk)
        return json.dumps(payload), 2

    monkeypatch.setattr(websocket_send, "_bounded_encode_stream_payload", capture_encode)

    asyncio.run(
        websocket_send.send_stream_json(
            WebSocket(),
            {"type": "gex_history_snapshot", "history": []},
            "gex",
        )
    )

    assert bulk_values == [True]


def test_stream_encoder_projects_supported_transport_values_to_canonical_json() -> None:
    payload = {
        "type": "chart_bars",
        "label": "живой",
        "by_period": {"20": 101.25},
        "day": date(2026, 7, 19),
        "captured_at": datetime(2026, 7, 19, 12, 30, tzinfo=UTC),
        "request_id": UUID("12345678-1234-5678-1234-567812345678"),
    }

    encoded, encoded_bytes = websocket_send._encode_stream_payload(payload)

    assert isinstance(encoded, str)
    assert encoded_bytes == len(encoded.encode("utf-8"))
    assert json.loads(encoded) == {
        "type": "chart_bars",
        "label": "живой",
        "by_period": {"20": 101.25},
        "day": "2026-07-19",
        "captured_at": "2026-07-19T12:30:00+00:00",
        "request_id": "12345678-1234-5678-1234-567812345678",
    }


def test_stream_encoder_rejects_unsupported_runtime_objects() -> None:
    @dataclass(frozen=True)
    class Marker:
        value: int

    with pytest.raises(TypeError, match="unsupported runtime type Marker"):
        websocket_send._encode_stream_payload({"type": "chart_bars", "marker": Marker(7)})

    with pytest.raises(TypeError, match="non-string mapping key"):
        websocket_send._encode_stream_payload({"type": "chart_bars", "by_period": {20: 101.25}})

    with pytest.raises(ValueError, match="timezone-naive datetime"):
        websocket_send._encode_stream_payload(
            {"type": "chart_bars", "captured_at": datetime(2026, 7, 19, 12, 30)}
        )


def test_send_stream_json_surfaces_timeout_and_records_timeout_metric(monkeypatch) -> None:
    increments, observations = _capture_metrics(monkeypatch)
    observed_timeout: list[float] = []

    class WebSocket:
        encoded = ""

        def send_text(self, message: str):
            self.encoded = message

            async def pending_send() -> None:
                await asyncio.Event().wait()

            return pending_send()

    async def force_timeout(awaitable, timeout: float):
        observed_timeout.append(timeout)
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError("send deadline exceeded")

    monkeypatch.setattr(websocket_send.asyncio, "wait_for", force_timeout)
    websocket = WebSocket()

    with pytest.raises(TimeoutError, match="send deadline exceeded"):
        asyncio.run(
            websocket_send.send_stream_json(
                websocket,
                {"type": "chart_bars", "bars": []},
                "chart",
            )
        )

    assert websocket.encoded == ""
    assert observed_timeout == [websocket_send._WEBSOCKET_HOT_DEADLINE_SECONDS]
    assert (
        "websocket_messages_total",
        {"stream": "chart", "message_type": "chart_bars", "status": "timeout"},
    ) in increments
    assert any(
        name == "websocket_send_seconds" and labels == {"stream": "chart"}
        for name, _value, labels in observations
    )
