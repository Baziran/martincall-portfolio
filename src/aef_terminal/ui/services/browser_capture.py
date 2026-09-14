from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
import zlib
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.async_tasks import run_physical_thread_call, settle_physical_task
from aef_terminal.ui.services.websocket_send import send_stream_json


_LOGGER = logging.getLogger(__name__)

BROWSER_CAPTURE_SCOPES = frozenset({"price", "terminal"})
BROWSER_CAPTURE_REQUEST_TIMEOUT_SECONDS = 15.0
BROWSER_CAPTURE_MAX_PNG_BYTES = 9_500_000
BROWSER_CAPTURE_MAX_PIXELS = 7_000_000
BROWSER_CAPTURE_MAX_TEXT_FRAME_BYTES = 4_096
BROWSER_CAPTURE_BUSY_STALE_SECONDS = 30.0
BROWSER_CAPTURE_MAX_SESSIONS = 32
BROWSER_CAPTURE_MAX_PENDING = 4
BROWSER_CAPTURE_BINARY_PREFIX = b"MCAP1:"
_BROWSER_CAPTURE_BINARY_SEPARATOR = b":"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TIMEFRAME_PATTERN = re.compile(r"[1-9][0-9]{0,3}[mhdw]\Z")
_ERROR_CODE_PATTERN = re.compile(r"BROWSER_CAPTURE_[A-Z0-9_]{1,96}\Z")


@dataclass
class _BrowserCaptureSession:
    session_id: str
    client_id: str
    instrument_id: str
    route_fingerprint: str
    timeframe: str
    loop: asyncio.AbstractEventLoop
    outbox: asyncio.Queue[dict[str, Any] | None]
    connected_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)
    visible: bool = False
    focused: bool = False
    busy_request_id: str = ""
    busy_started_at: float = 0.0
    closed: bool = False


@dataclass
class _PendingBrowserCapture:
    request_id: str
    session_id: str
    client_id: str
    instrument_id: str
    route_fingerprint: str
    scope: str
    future: Future[dict[str, Any]]
    requested_at: float = field(default_factory=time.monotonic)


def _require_capture_scope(value: object) -> str:
    if not isinstance(value, str) or value not in BROWSER_CAPTURE_SCOPES:
        raise ValueError("BROWSER_CAPTURE_SCOPE_INVALID")
    return value


def _require_capture_client_id(value: object) -> str:
    client_id = str(value or "")
    raw_uuid = client_id.removeprefix("mc-") if client_id.startswith("mc-") else ""
    try:
        parsed = uuid.UUID(raw_uuid)
    except ValueError, AttributeError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != raw_uuid:
        raise ValueError("BROWSER_CAPTURE_CLIENT_ID_INVALID")
    return client_id


def _require_capture_timeframe(value: object) -> str:
    timeframe = str(value or "")
    if _TIMEFRAME_PATTERN.fullmatch(timeframe) is None:
        raise ValueError("BROWSER_CAPTURE_TIMEFRAME_INVALID")
    return timeframe


def _require_capture_request_id(value: object) -> str:
    request_id = str(value or "")
    try:
        parsed = uuid.UUID(request_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("BROWSER_CAPTURE_REQUEST_ID_INVALID") from exc
    if parsed.version != 4 or str(parsed) != request_id:
        raise ValueError("BROWSER_CAPTURE_REQUEST_ID_INVALID")
    return request_id


def _require_capture_error_code(value: object) -> str:
    if not isinstance(value, str) or _ERROR_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError("BROWSER_CAPTURE_CLIENT_ERROR_INVALID")
    return value


def _capture_failure_result(
    *,
    instrument_id: str,
    route_fingerprint: str,
    scope: str,
    error_code: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "scope": scope,
        "error_code": error_code,
    }


def _validated_png(payload: bytes) -> tuple[bytes, int, int]:
    if (
        not isinstance(payload, bytes)
        or len(payload) < 24
        or len(payload) > BROWSER_CAPTURE_MAX_PNG_BYTES
        or not payload.startswith(_PNG_SIGNATURE)
        or payload[12:16] != b"IHDR"
        or payload[-12:-8] != b"\x00\x00\x00\x00"
        or payload[-8:-4] != b"IEND"
    ):
        raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if (
        width <= 0
        or height <= 0
        or width + height > 10_000
        or width * height > BROWSER_CAPTURE_MAX_PIXELS
        or max(width / height, height / width) > 20
    ):
        raise ValueError("BROWSER_CAPTURE_DIMENSIONS_INVALID")
    offset = len(_PNG_SIGNATURE)
    chunk_count = 0
    seen_idat = False
    while offset < len(payload):
        chunk_count += 1
        if chunk_count > 512 or offset + 12 > len(payload):
            raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
        chunk_length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type_start = offset + 4
        chunk_data_end = chunk_type_start + 4 + chunk_length
        chunk_end = chunk_data_end + 4
        if chunk_end > len(payload):
            raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
        chunk_type = payload[chunk_type_start : chunk_type_start + 4]
        if chunk_count == 1 and (chunk_type != b"IHDR" or chunk_length != 13):
            raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
        expected_crc = int.from_bytes(payload[chunk_data_end:chunk_end], "big")
        actual_crc = zlib.crc32(payload[chunk_type_start:chunk_data_end]) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
        if chunk_type == b"IDAT":
            seen_idat = True
        if chunk_type == b"IEND":
            if chunk_length != 0 or not seen_idat or chunk_end != len(payload):
                raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
            break
        offset = chunk_end
    else:
        raise ValueError("BROWSER_CAPTURE_PNG_INVALID")
    return payload, width, height


def _browser_capture_same_origin(websocket: WebSocket) -> bool:
    origin = str(websocket.headers.get("origin") or "").strip()
    host = str(websocket.headers.get("host") or "").strip().lower()
    if not origin or not host:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host


def _parse_capture_binary_frame(payload: bytes) -> tuple[str, bytes, int, int]:
    header_size = len(BROWSER_CAPTURE_BINARY_PREFIX) + 36 + 1
    if (
        not isinstance(payload, bytes)
        or len(payload) <= header_size
        or not payload.startswith(BROWSER_CAPTURE_BINARY_PREFIX)
        or payload[header_size - 1 : header_size] != _BROWSER_CAPTURE_BINARY_SEPARATOR
    ):
        raise ValueError("BROWSER_CAPTURE_BINARY_FRAME_INVALID")
    request_start = len(BROWSER_CAPTURE_BINARY_PREFIX)
    request_id = _require_capture_request_id(
        payload[request_start : request_start + 36].decode("ascii", errors="strict")
    )
    image, width, height = _validated_png(payload[header_size:])
    return request_id, image, width, height


class BrowserCaptureBroker:
    """Thread-safe bridge between Telegram workers and active browser tabs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, _BrowserCaptureSession] = {}
        self._pending: dict[str, _PendingBrowserCapture] = {}

    def register(
        self,
        *,
        client_id: str,
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str,
        loop: asyncio.AbstractEventLoop,
    ) -> _BrowserCaptureSession:
        session = _BrowserCaptureSession(
            session_id=str(uuid.uuid4()),
            client_id=_require_capture_client_id(client_id),
            instrument_id=require_exact_identity_text(
                instrument_id,
                field="BROWSER_CAPTURE_INSTRUMENT_ID",
            ),
            route_fingerprint=require_exact_identity_text(
                route_fingerprint,
                field="BROWSER_CAPTURE_ROUTE_FINGERPRINT",
            ),
            timeframe=_require_capture_timeframe(timeframe),
            loop=loop,
            outbox=asyncio.Queue(),
        )
        with self._lock:
            duplicate_ids = [
                current.session_id
                for current in self._sessions.values()
                if current.client_id == session.client_id
            ]
        for duplicate_id in duplicate_ids:
            self.unregister(duplicate_id)
        with self._lock:
            if len(self._sessions) >= BROWSER_CAPTURE_MAX_SESSIONS:
                raise RuntimeError("BROWSER_CAPTURE_SESSION_LIMIT")
            self._sessions[session.session_id] = session
        return session

    def unregister(self, session_id: str) -> None:
        request_ids: list[str] = []
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            session.closed = True
            for request_id, pending in list(self._pending.items()):
                if pending.session_id != session_id:
                    continue
                request_ids.append(request_id)
        try:
            session.loop.call_soon_threadsafe(session.outbox.put_nowait, None)
        except RuntimeError:
            pass
        for request_id in request_ids:
            self._fail_request(
                request_id,
                error_code="BROWSER_CAPTURE_CONNECTION_CLOSED",
                release_session=False,
            )

    def update_state(
        self,
        session_id: str,
        *,
        visible: bool,
        focused: bool,
        timeframe: object,
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.closed:
                return
            session.visible = bool(visible)
            session.focused = bool(focused)
            session.timeframe = _require_capture_timeframe(timeframe)
            session.last_seen_at = time.monotonic()

    def _selected_session(
        self,
        instrument_id: str,
        route_fingerprint: str,
    ) -> tuple[_BrowserCaptureSession | None, str | None]:
        now = time.monotonic()
        candidates = [
            session
            for session in self._sessions.values()
            if (
                not session.closed
                and session.instrument_id == instrument_id
                and session.route_fingerprint == route_fingerprint
            )
        ]
        if not candidates:
            return None, "BROWSER_CAPTURE_NO_SESSION"
        for session in candidates:
            if (
                session.busy_request_id
                and now - session.busy_started_at > BROWSER_CAPTURE_BUSY_STALE_SECONDS
                and session.busy_request_id not in self._pending
            ):
                session.busy_request_id = ""
                session.busy_started_at = 0.0
        candidates = [session for session in candidates if not session.busy_request_id]
        if not candidates:
            return None, "BROWSER_CAPTURE_SESSION_BUSY"
        return (
            max(
                candidates,
                key=lambda session: (
                    session.focused,
                    session.visible,
                    session.last_seen_at,
                    session.connected_at,
                    session.client_id,
                ),
            ),
            None,
        )

    def request(
        self,
        instrument_id: str,
        scope: str,
        *,
        expected_route_fingerprint: str,
        timeout: float = BROWSER_CAPTURE_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="BROWSER_CAPTURE_INSTRUMENT_ID",
        )
        exact_scope = _require_capture_scope(scope)
        exact_route_fingerprint = require_exact_identity_text(
            expected_route_fingerprint,
            field="BROWSER_CAPTURE_ROUTE_FINGERPRINT",
        )
        timeout_seconds = float(timeout)
        if not (timeout_seconds > 0 and timeout_seconds < float("inf")):
            timeout_seconds = 0.1
        timeout_seconds = min(
            max(timeout_seconds, 0.1),
            BROWSER_CAPTURE_REQUEST_TIMEOUT_SECONDS,
        )
        request_id = str(uuid.uuid4())
        future: Future[dict[str, Any]] = Future()
        with self._lock:
            if len(self._pending) >= BROWSER_CAPTURE_MAX_PENDING:
                return _capture_failure_result(
                    instrument_id=exact_instrument_id,
                    route_fingerprint=exact_route_fingerprint,
                    scope=exact_scope,
                    error_code="BROWSER_CAPTURE_PENDING_LIMIT",
                )
            session, selection_error = self._selected_session(
                exact_instrument_id,
                exact_route_fingerprint,
            )
            if session is None:
                return _capture_failure_result(
                    instrument_id=exact_instrument_id,
                    route_fingerprint=exact_route_fingerprint,
                    scope=exact_scope,
                    error_code=selection_error or "BROWSER_CAPTURE_NO_SESSION",
                )
            pending = _PendingBrowserCapture(
                request_id=request_id,
                session_id=session.session_id,
                client_id=session.client_id,
                instrument_id=exact_instrument_id,
                route_fingerprint=exact_route_fingerprint,
                scope=exact_scope,
                future=future,
            )
            self._pending[request_id] = pending
            session.busy_request_id = request_id
            session.busy_started_at = time.monotonic()
            message = {
                "type": "capture_request",
                "request_id": request_id,
                "scope": exact_scope,
                "instrument_id": session.instrument_id,
                "route_fingerprint": session.route_fingerprint,
                "timeframe": session.timeframe,
            }
        try:
            session.loop.call_soon_threadsafe(session.outbox.put_nowait, message)
        except RuntimeError:
            failure = self._fail_request(
                request_id,
                error_code="BROWSER_CAPTURE_DISPATCH_FAILED",
                release_session=True,
            )
            _LOGGER.warning(
                "Browser capture dispatch failed: error_code=%s instrument_id=%s "
                "scope=%s client_id=%s elapsed_seconds=%.3f",
                "BROWSER_CAPTURE_DISPATCH_FAILED",
                exact_instrument_id,
                exact_scope,
                session.client_id,
                time.monotonic() - pending.requested_at,
            )
            return failure or _capture_failure_result(
                instrument_id=exact_instrument_id,
                route_fingerprint=exact_route_fingerprint,
                scope=exact_scope,
                error_code="BROWSER_CAPTURE_DISPATCH_FAILED",
            )
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            # Keep the session reserved until its late result/error arrives. This
            # preserves the one-in-flight invariant without blocking any event loop.
            failure = self._fail_request(
                request_id,
                error_code="BROWSER_CAPTURE_TIMEOUT",
                release_session=False,
            )
            _LOGGER.warning(
                "Browser capture timed out: error_code=%s instrument_id=%s "
                "scope=%s client_id=%s elapsed_seconds=%.3f",
                "BROWSER_CAPTURE_TIMEOUT",
                exact_instrument_id,
                exact_scope,
                session.client_id,
                time.monotonic() - pending.requested_at,
            )
            return failure or _capture_failure_result(
                instrument_id=exact_instrument_id,
                route_fingerprint=exact_route_fingerprint,
                scope=exact_scope,
                error_code="BROWSER_CAPTURE_TIMEOUT",
            )
        except Exception:
            failure = self._fail_request(
                request_id,
                error_code="BROWSER_CAPTURE_INTERNAL_ERROR",
                release_session=True,
            )
            _LOGGER.exception(
                "Browser capture failed unexpectedly: error_code=%s "
                "instrument_id=%s scope=%s client_id=%s elapsed_seconds=%.3f",
                "BROWSER_CAPTURE_INTERNAL_ERROR",
                exact_instrument_id,
                exact_scope,
                session.client_id,
                time.monotonic() - pending.requested_at,
            )
            return failure or _capture_failure_result(
                instrument_id=exact_instrument_id,
                route_fingerprint=exact_route_fingerprint,
                scope=exact_scope,
                error_code="BROWSER_CAPTURE_INTERNAL_ERROR",
            )

    def _fail_request(
        self,
        request_id: str,
        *,
        error_code: str,
        release_session: bool,
    ) -> dict[str, Any] | None:
        future: Future[dict[str, Any]] | None = None
        result: dict[str, Any] | None = None
        with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return None
            session = self._sessions.get(pending.session_id)
            if release_session and session is not None and session.busy_request_id == request_id:
                session.busy_request_id = ""
                session.busy_started_at = 0.0
            future = pending.future
            result = _capture_failure_result(
                instrument_id=pending.instrument_id,
                route_fingerprint=pending.route_fingerprint,
                scope=pending.scope,
                error_code=error_code,
            )
        if future is not None and result is not None and not future.done():
            future.set_result(result)
        return result

    def reject(
        self,
        session_id: str,
        request_id: object,
        *,
        error_code: object,
    ) -> None:
        try:
            exact_request_id = _require_capture_request_id(request_id)
        except ValueError:
            return
        try:
            exact_error_code = _require_capture_error_code(error_code)
        except ValueError:
            exact_error_code = "BROWSER_CAPTURE_CLIENT_ERROR_INVALID"
        pending: _PendingBrowserCapture | None = None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.busy_request_id != exact_request_id:
                return
            session.last_seen_at = time.monotonic()
            pending = self._pending.get(exact_request_id)
            session.busy_request_id = ""
            session.busy_started_at = 0.0
        self._fail_request(
            exact_request_id,
            error_code=exact_error_code,
            release_session=False,
        )
        if pending is not None:
            _LOGGER.warning(
                "Browser capture client error: error_code=%s instrument_id=%s "
                "scope=%s client_id=%s elapsed_seconds=%.3f",
                exact_error_code,
                pending.instrument_id,
                pending.scope,
                pending.client_id,
                time.monotonic() - pending.requested_at,
            )

    def resolve_binary(self, session_id: str, payload: bytes) -> None:
        try:
            request_id, image, width, height = _parse_capture_binary_frame(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            try:
                error_code = _require_capture_error_code(str(exc))
            except ValueError:
                error_code = "BROWSER_CAPTURE_BINARY_FRAME_INVALID"
            with self._lock:
                session = self._sessions.get(session_id)
                active_request_id = session.busy_request_id if session else ""
            if active_request_id:
                self.reject(
                    session_id,
                    active_request_id,
                    error_code=error_code,
                )
            return
        future: Future[dict[str, Any]] | None = None
        result: dict[str, Any] | None = None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.closed:
                return
            session.last_seen_at = time.monotonic()
            pending = self._pending.get(request_id)
            if pending is None:
                if session.busy_request_id == request_id:
                    session.busy_request_id = ""
                    session.busy_started_at = 0.0
                return
            if pending.session_id != session_id:
                return
            self._pending.pop(request_id, None)
            if session.busy_request_id == request_id:
                session.busy_request_id = ""
                session.busy_started_at = 0.0
            future = pending.future
            result = {
                "ok": True,
                "instrument_id": session.instrument_id,
                "route_fingerprint": session.route_fingerprint,
                "timeframe": session.timeframe,
                "scope": pending.scope,
                "client_id": session.client_id,
                "captured_at": datetime.now(tz=UTC).isoformat(),
                "width": width,
                "height": height,
                "content": image,
            }
        if future is not None and result is not None and not future.done():
            future.set_result(result)

    def session_count(self, instrument_id: str | None = None) -> int:
        with self._lock:
            return sum(
                1
                for session in self._sessions.values()
                if (
                    not session.closed
                    and (instrument_id is None or session.instrument_id == instrument_id)
                )
            )


BROWSER_CAPTURE_BROKER = BrowserCaptureBroker()


def request_browser_capture(
    instrument_id: str,
    scope: str,
) -> dict[str, Any]:
    from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument

    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="BROWSER_CAPTURE_INSTRUMENT_ID",
    )
    instrument = lookup_runtime_instrument(exact_instrument_id)
    route = route_instrument(instrument)
    if route.instrument_id != exact_instrument_id:
        raise ValueError("BROWSER_CAPTURE_ROUTE_MISMATCH")
    return BROWSER_CAPTURE_BROKER.request(
        exact_instrument_id,
        scope,
        expected_route_fingerprint=route.fingerprint,
    )


async def _browser_capture_sender(
    websocket: WebSocket,
    session: _BrowserCaptureSession,
) -> None:
    while True:
        message = await session.outbox.get()
        if message is None:
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                pass
            return
        await send_stream_json(websocket, message, "browser_capture")


async def _browser_capture_receiver(
    websocket: WebSocket,
    session: _BrowserCaptureSession,
    broker: BrowserCaptureBroker,
) -> None:
    while True:
        frame = await websocket.receive()
        frame_type = str(frame.get("type") or "")
        if frame_type == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(frame.get("code") or 1000))
        binary = frame.get("bytes")
        if isinstance(binary, bytes):
            await run_physical_thread_call(
                broker.resolve_binary,
                session.session_id,
                binary,
            )
            continue
        text = frame.get("text")
        if (
            not isinstance(text, str)
            or len(text.encode("utf-8")) > BROWSER_CAPTURE_MAX_TEXT_FRAME_BYTES
        ):
            raise ValueError("BROWSER_CAPTURE_TEXT_FRAME_INVALID")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("BROWSER_CAPTURE_MESSAGE_INVALID")
        message_type = payload.get("type")
        if message_type == "capture_state":
            broker.update_state(
                session.session_id,
                visible=payload.get("visible") is True,
                focused=payload.get("focused") is True,
                timeframe=payload.get("timeframe"),
            )
        elif message_type == "capture_error":
            broker.reject(
                session.session_id,
                payload.get("request_id"),
                error_code=payload.get("error_code"),
            )
        else:
            raise ValueError("BROWSER_CAPTURE_MESSAGE_INVALID")


async def _settle_browser_capture_tasks(
    tasks: tuple[asyncio.Task[None], ...],
) -> None:
    cancellation: asyncio.CancelledError | None = None
    for task in tasks:
        if not task.done():
            task.cancel()
        outcome = await settle_physical_task(
            task,
            deferred_cancellation=cancellation,
        )
        cancellation = cancellation or outcome.cancellation
    if cancellation is not None:
        raise cancellation


async def _run_browser_capture_session(
    websocket: WebSocket,
    session: _BrowserCaptureSession,
    broker: BrowserCaptureBroker,
) -> None:
    sender = asyncio.create_task(_browser_capture_sender(websocket, session))
    receiver = asyncio.create_task(_browser_capture_receiver(websocket, session, broker))
    tasks = (sender, receiver)
    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        await _settle_browser_capture_tasks(tasks)
        raise
    for task in pending:
        task.cancel()
    await _settle_browser_capture_tasks(tasks)
    for task in done:
        task.result()


async def run_browser_capture_socket(
    websocket: WebSocket,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    timeframe: str,
    client_id: str,
    lookup_runtime_instrument: Any,
    broker: BrowserCaptureBroker = BROWSER_CAPTURE_BROKER,
) -> None:
    await websocket.accept()
    session: _BrowserCaptureSession | None = None
    try:
        if not _browser_capture_same_origin(websocket):
            raise ValueError("BROWSER_CAPTURE_ORIGIN_INVALID")
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="BROWSER_CAPTURE_INSTRUMENT_ID",
        )
        exact_route_fingerprint = require_exact_identity_text(
            expected_route_fingerprint,
            field="BROWSER_CAPTURE_ROUTE_FINGERPRINT",
        )
        exact_client_id = _require_capture_client_id(client_id)
        exact_timeframe = _require_capture_timeframe(timeframe)
        instrument = await run_physical_thread_call(
            lookup_runtime_instrument,
            exact_instrument_id,
        )
        route = route_instrument(instrument)
        if (
            route.instrument_id != exact_instrument_id
            or route.fingerprint != exact_route_fingerprint
        ):
            raise ValueError("BROWSER_CAPTURE_ROUTE_MISMATCH")
        session = broker.register(
            client_id=exact_client_id,
            instrument_id=exact_instrument_id,
            route_fingerprint=exact_route_fingerprint,
            timeframe=exact_timeframe,
            loop=asyncio.get_running_loop(),
        )
        await send_stream_json(
            websocket,
            {
                "type": "capture_ready",
                "instrument_id": exact_instrument_id,
                "route_fingerprint": exact_route_fingerprint,
                "timeframe": exact_timeframe,
            },
            "browser_capture",
        )
        await _run_browser_capture_session(websocket, session, broker)
    except WebSocketDisconnect:
        return
    except RuntimeError, ValueError, json.JSONDecodeError:
        close = getattr(websocket, "close", None)
        if callable(close):
            try:
                await close(code=1008)
            except RuntimeError:
                pass
    finally:
        if session is not None:
            broker.unregister(session.session_id)
