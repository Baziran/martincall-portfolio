from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any
from uuid import UUID

import orjson
from fastapi import WebSocket

from aef_terminal.runtime.metrics import increment_metric, observe_metric


_WEBSOCKET_HOT_JSON_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="websocket-hot-json",
)
_WEBSOCKET_BULK_JSON_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="websocket-bulk-json",
)
_WEBSOCKET_HOT_ENCODE_SLOTS = threading.BoundedSemaphore(32)
_WEBSOCKET_BULK_ENCODE_SLOTS = threading.BoundedSemaphore(8)
_WEBSOCKET_HOT_DEADLINE_SECONDS = 2.0
_WEBSOCKET_BULK_DEADLINE_SECONDS = 1.0


def _project_stream_wire_value(value: Any, *, path: str = "payload") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{path} contains a timezone-naive datetime")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            projected[key] = _project_stream_wire_value(item, path=f"{path}.{key}")
        return projected
    if isinstance(value, (list, tuple)):
        return [
            _project_stream_wire_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported runtime type {type(value).__name__}")


def _encode_stream_payload(payload: dict[str, Any]) -> tuple[str, int]:
    encoded = orjson.dumps(_project_stream_wire_value(payload))
    return encoded.decode("utf-8"), len(encoded)


async def _bounded_encode_stream_payload(
    payload: dict[str, Any],
    *,
    bulk: bool,
) -> tuple[str, int]:
    executor = _WEBSOCKET_BULK_JSON_EXECUTOR if bulk else _WEBSOCKET_HOT_JSON_EXECUTOR
    slots = _WEBSOCKET_BULK_ENCODE_SLOTS if bulk else _WEBSOCKET_HOT_ENCODE_SLOTS
    if not slots.acquire(blocking=False):
        raise TimeoutError("websocket JSON encoder saturated")
    try:
        future = executor.submit(_encode_stream_payload, payload)
    except Exception:
        slots.release()
        raise
    future.add_done_callback(lambda _completed: slots.release())
    return await asyncio.wrap_future(future)


async def send_stream_json(websocket: WebSocket, payload: dict[str, Any], stream: str) -> None:
    started_at = time.perf_counter()
    message_type = str(payload.get("type") or "unknown") if isinstance(payload, dict) else "unknown"
    bulk = bool(
        payload.get("recovery") or message_type in {"quote_snapshot", "gex_history_snapshot"}
    )
    deadline = _WEBSOCKET_BULK_DEADLINE_SECONDS if bulk else _WEBSOCKET_HOT_DEADLINE_SECONDS

    async def encode_and_send() -> None:
        send_text = getattr(websocket, "send_text", None)
        if callable(send_text):
            encode_started_at = time.perf_counter()
            encoded, encoded_bytes = await _bounded_encode_stream_payload(payload, bulk=bulk)
            observe_metric(
                "websocket_encode_seconds",
                time.perf_counter() - encode_started_at,
                stream=stream,
                message_type=message_type,
            )
            observe_metric(
                "websocket_payload_bytes",
                encoded_bytes,
                stream=stream,
                message_type=message_type,
            )
            await send_text(encoded)
            return
        await websocket.send_json(_project_stream_wire_value(payload))

    try:
        await asyncio.wait_for(encode_and_send(), timeout=deadline)
        increment_metric(
            "websocket_messages_total", stream=stream, message_type=message_type, status="ok"
        )
    except TimeoutError:
        increment_metric(
            "websocket_messages_total", stream=stream, message_type=message_type, status="timeout"
        )
        raise
    except Exception:
        increment_metric(
            "websocket_messages_total", stream=stream, message_type=message_type, status="error"
        )
        raise
    finally:
        observe_metric("websocket_send_seconds", time.perf_counter() - started_at, stream=stream)
