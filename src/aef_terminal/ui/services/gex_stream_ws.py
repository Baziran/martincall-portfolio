from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from aef_terminal.data.gex.constants import (
    GEX_CHART_HISTORY_SOURCES,
    GEX_CHART_HISTORY_HOURS,
    GEX_LIVE_FRAME_SECONDS,
    GEX_CONTEXT_MAX_LEVELS,
)
from aef_terminal.data.gex.contracts import require_exact_gex_capture_lane
from aef_terminal.data.gex.history import (
    get_gex_history_series,
    read_gex_snapshot_rows,
)
from aef_terminal.data.gex.payload_contract import require_gex_levels
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.async_tasks import (
    run_cancellation_deferred,
    run_physical_thread_call,
    settle_physical_task,
)
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts
from aef_terminal.ui.gex_projection import (
    project_gex_context_for_chart,
    project_gex_history_for_chart,
)
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.services.websocket_send import send_stream_json


GEX_STREAM_FRAME_SECONDS = GEX_LIVE_FRAME_SECONDS
GEX_STREAM_PUBLICATION_SECONDS = 5.0
GEX_STREAM_HISTORY_REFRESH_SECONDS = 30.0
GEX_STREAM_ROUTE_CHECK_SECONDS = 30.0
_GEX_HISTORY_STATEMENT_TIMEOUT_MS = 8_000
_GEX_HISTORY_LOCK_TIMEOUT_MS = 1_000
_GEX_STREAM_PRODUCERS: dict[tuple[str, str], "_GexStreamProducer"] = {}


@dataclass(frozen=True)
class GexStreamWsDeps:
    apply_provider_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
    store_factory: Callable[[], Any]
    websocket_heartbeat_seconds: float


@dataclass(frozen=True)
class _GexHistoryReadModel:
    snapshot_rows: list[dict[str, Any]]
    history: list[dict[str, Any]]
    wire_history: list[dict[str, Any]]


@dataclass
class _GexStreamProducer:
    key: tuple[str, str]
    instrument_id: str
    provider_symbol: str
    route: Any
    store: Any
    consumers: int = 0
    revision: int = 0
    frame_seq: int = 0
    frame: dict[str, Any] | None = None
    material_key: str = ""
    authority_key: str = ""
    authority_revision: int = 0
    error: str = ""
    transport_status: dict[str, Any] | None = None
    transport_status_key: tuple[str, str, str, str] | None = None
    transport_status_revision: int = 0
    transport_retry_at: float = 0.0
    closing: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)
    wire_history: list[dict[str, Any]] = field(default_factory=list)
    history_revision: int = 0
    history_ready: bool = False
    history_error: str = ""
    history_task: asyncio.Task[_GexHistoryReadModel] | None = field(
        default=None,
        repr=False,
    )
    history_snapshot_rows: list[dict[str, Any]] | None = field(
        default=None,
        repr=False,
    )
    history_refreshed_at: float = 0.0
    route_checked_at: float = field(default_factory=time.monotonic)
    last_history_bucket: str = ""
    history_delta: dict[str, Any] | None = field(default=None, repr=False)
    deps: GexStreamWsDeps | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def _gex_history_row_key(row: Mapping[str, Any]) -> str:
    captured_at = row.get("captured_at")
    source = row.get("source")
    capture_mode = row.get("capture_mode")
    if not isinstance(captured_at, str) or not captured_at or captured_at != captured_at.strip():
        raise ValueError("GEX history row key requires exact captured_at")
    try:
        exact_source, exact_capture_mode = require_exact_gex_capture_lane(
            source=source,
            capture_mode=capture_mode,
        )
    except ValueError:
        raise ValueError("GEX history row key requires an exact capture lane") from None
    return json.dumps(
        [
            exact_source,
            exact_capture_mode,
            captured_at,
        ],
        separators=(",", ":"),
    )


def _gex_history_delta(
    previous: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    *,
    base_revision: int,
    revision: int,
) -> dict[str, Any]:
    previous_by_key = {_gex_history_row_key(row): row for row in previous}
    current_by_key = {_gex_history_row_key(row): row for row in current}
    return {
        "base_history_revision": base_revision,
        "history_revision": revision,
        "upserts": [
            dict(row) for key, row in current_by_key.items() if previous_by_key.get(key) != row
        ],
        "removed_history_keys": [key for key in previous_by_key if key not in current_by_key],
    }


def _gex_history_read_model_changed(
    previous: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
) -> bool:
    return list(previous) != list(current)


def _gex_history_status(producer: _GexStreamProducer) -> dict[str, Any]:
    loading = producer.history_task is not None and not producer.history_ready
    refreshing = producer.history_task is not None and producer.history_ready
    return {
        "status": "error" if producer.history_error else "loading" if loading else "ok",
        "stale": bool(producer.wire_history and (producer.history_error or refreshing)),
        "message": producer.history_error,
    }


def _gex_snapshot_row_key(row: Mapping[str, Any]) -> tuple[str, datetime]:
    source = row.get("source")
    if source not in GEX_CHART_HISTORY_SOURCES:
        raise ValueError("GEX history snapshot row requires an exact source lane")
    captured_at = parse_gex_timestamp(row.get("captured_at"))
    if captured_at is None:
        raise ValueError("GEX history snapshot row requires an exact captured_at")
    return source, captured_at


def _merge_gex_snapshot_rows(
    previous: Sequence[dict[str, Any]],
    incoming: Sequence[dict[str, Any]],
    *,
    min_captured_at: datetime,
) -> list[dict[str, Any]]:
    if min_captured_at.tzinfo is None or min_captured_at.utcoffset() is None:
        raise ValueError("GEX history minimum timestamp must be timezone-aware")
    minimum = min_captured_at.astimezone(UTC)
    merged: dict[tuple[str, datetime], dict[str, Any]] = {}
    for row in (*previous, *incoming):
        if not isinstance(row, dict):
            raise ValueError("GEX history snapshot row must be an object")
        key = _gex_snapshot_row_key(row)
        if key[1] >= minimum:
            merged[key] = row
    source_order = {source: index for index, source in enumerate(GEX_CHART_HISTORY_SOURCES)}
    return [
        merged[key]
        for key in sorted(
            merged,
            key=lambda item: (item[1], source_order[item[0]]),
        )
    ]


def _read_gex_stream_snapshot_rows(
    producer: _GexStreamProducer,
    *,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    window_start = now_utc - timedelta(hours=GEX_CHART_HISTORY_HOURS)
    previous = producer.history_snapshot_rows
    if previous is None:
        read_start = window_start
    else:
        timestamps = [_gex_snapshot_row_key(row)[1] for row in previous]
        read_start = max(
            window_start,
            (max(timestamps) - timedelta(minutes=5))
            if timestamps
            else now_utc - timedelta(minutes=5),
        )
    expected_rows = (math.ceil(max((now_utc - read_start).total_seconds(), 0.0) / 300.0) + 2) * len(
        GEX_CHART_HISTORY_SOURCES
    )
    incoming = read_gex_snapshot_rows(
        read_start,
        now_utc,
        instrument_id=producer.instrument_id,
        route_fingerprint=producer.route.fingerprint,
        limit=max(expected_rows, len(GEX_CHART_HISTORY_SOURCES)),
        store=producer.store,
        source=None,
        sources=GEX_CHART_HISTORY_SOURCES,
    )
    return _merge_gex_snapshot_rows(
        previous or (),
        incoming,
        min_captured_at=window_start,
    )


async def _load_gex_history_read_model(
    producer: _GexStreamProducer,
) -> _GexHistoryReadModel:
    now_utc = datetime.now(tz=UTC)
    read_mode = "bootstrap" if producer.history_snapshot_rows is None else "incremental"
    read_started_at = time.perf_counter()
    with postgres_operation_timeouts(
        statement_timeout_ms=_GEX_HISTORY_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=_GEX_HISTORY_LOCK_TIMEOUT_MS,
    ):
        history_task = asyncio.create_task(
            asyncio.to_thread(
                _read_gex_stream_snapshot_rows,
                producer,
                now_utc=now_utc,
            ),
            name=f"gex-history-read:{producer.route.fingerprint}",
        )
    history_outcome = await settle_physical_task(history_task)
    history_error = history_outcome.error
    if history_outcome.task_cancelled:
        history_error = RuntimeError("GEX_HISTORY_READ_TASK_CANCELLED")
    if history_error is not None:
        if history_outcome.cancellation is not None:
            raise history_outcome.cancellation from history_error
        raise history_error
    if history_outcome.cancellation is not None:
        raise history_outcome.cancellation
    snapshot_rows = history_outcome.result
    observe_metric(
        "gex_history_storage_read_seconds",
        time.perf_counter() - read_started_at,
        mode=read_mode,
    )
    set_metric("gex_history_cached_snapshot_rows", len(snapshot_rows))
    if producer.history_snapshot_rows == snapshot_rows:
        increment_metric("gex_history_refresh_total", status="unchanged")
        return _GexHistoryReadModel(
            snapshot_rows=snapshot_rows,
            history=producer.history,
            wire_history=producer.wire_history,
        )
    build_started_at = time.perf_counter()
    projection_task = asyncio.create_task(
        asyncio.to_thread(
            get_gex_history_series,
            producer.provider_symbol,
            instrument_id=producer.instrument_id,
            hours=GEX_CHART_HISTORY_HOURS,
            max_levels=GEX_CONTEXT_MAX_LEVELS,
            now=now_utc,
            preloaded_rows=snapshot_rows,
            sources=GEX_CHART_HISTORY_SOURCES,
            route_fingerprint=producer.route.fingerprint,
        ),
        name=f"gex-history-build:{producer.route.fingerprint}",
    )
    projection_outcome = await settle_physical_task(projection_task)
    projection_error = projection_outcome.error
    if projection_outcome.task_cancelled:
        projection_error = RuntimeError("GEX_HISTORY_BUILD_TASK_CANCELLED")
    if projection_error is not None:
        if projection_outcome.cancellation is not None:
            raise projection_outcome.cancellation from projection_error
        raise projection_error
    if projection_outcome.cancellation is not None:
        raise projection_outcome.cancellation
    history = projection_outcome.result
    observe_metric(
        "gex_history_build_seconds",
        time.perf_counter() - build_started_at,
    )
    projection_started_at = time.perf_counter()
    wire_projection_task = asyncio.create_task(
        asyncio.to_thread(project_gex_history_for_chart, history),
        name=f"gex-history-project:{producer.route.fingerprint}",
    )
    projection_outcome = await settle_physical_task(wire_projection_task)
    projection_error = projection_outcome.error
    if projection_outcome.task_cancelled:
        projection_error = RuntimeError("GEX_HISTORY_PROJECTION_TASK_CANCELLED")
    if projection_error is not None:
        if projection_outcome.cancellation is not None:
            raise projection_outcome.cancellation from projection_error
        raise projection_error
    if projection_outcome.cancellation is not None:
        raise projection_outcome.cancellation
    wire_history = projection_outcome.result
    observe_metric(
        "gex_history_projection_seconds",
        time.perf_counter() - projection_started_at,
    )
    increment_metric("gex_history_refresh_total", status="changed")
    return _GexHistoryReadModel(
        snapshot_rows=snapshot_rows,
        history=history,
        wire_history=wire_history,
    )


def _schedule_gex_history_refresh(producer: _GexStreamProducer) -> None:
    producer.history_refreshed_at = time.monotonic()
    producer.history_task = asyncio.create_task(
        _load_gex_history_read_model(producer),
        name=f"gex-history:{producer.route.fingerprint}",
    )


def _consume_gex_history_refresh(producer: _GexStreamProducer) -> None:
    task = producer.history_task
    if task is None or not task.done():
        return
    previous_wire = producer.wire_history
    previous_ready = producer.history_ready
    previous_error = producer.history_error
    base_revision = producer.history_revision
    try:
        read_model = task.result()
        producer.history_snapshot_rows = read_model.snapshot_rows
        producer.history = read_model.history
        producer.history_ready = True
        producer.history_error = ""
        if (
            not previous_ready
            or previous_error
            or _gex_history_read_model_changed(previous_wire, read_model.wire_history)
        ):
            producer.history_revision += 1
            producer.wire_history = read_model.wire_history
            producer.history_delta = (
                _gex_history_delta(
                    previous_wire,
                    read_model.wire_history,
                    base_revision=base_revision,
                    revision=producer.history_revision,
                )
                if previous_ready
                else None
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if not previous_ready or message != previous_error:
            producer.history_revision += 1
            producer.history_delta = (
                _gex_history_delta(
                    previous_wire,
                    previous_wire,
                    base_revision=base_revision,
                    revision=producer.history_revision,
                )
                if previous_ready
                else None
            )
        producer.history_error = message
    finally:
        producer.history_task = None


def _prepare_gex_live_frame(
    frame: dict[str, Any],
    *,
    max_levels: int,
) -> dict[str, Any]:
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX stream max_levels must be a positive integer")
    live = frame.get("live")
    if not isinstance(live, dict):
        raise ValueError("GEX stream frame requires typed live metadata")
    frame_seq = live.get("frame_seq")
    if type(frame_seq) is not int or frame_seq <= 0:
        raise ValueError("GEX stream frame requires a positive integer frame_seq")
    all_levels = frame.get("levels")
    if not isinstance(all_levels, list):
        raise ValueError("GEX stream frame requires a levels list")
    frame["levels"] = require_gex_levels(
        all_levels,
        max_levels=max_levels,
        spot=frame.get("spot"),
    )
    if isinstance(frame.get("visibility_summary"), dict):
        frame["visibility_summary"] = {
            **frame["visibility_summary"],
            "output_level_count": len(frame["levels"]),
            "hidden_by_max_levels": max(0, len(all_levels) - len(frame["levels"])),
            "max_levels": max_levels,
        }
    return frame


def _gex_material_frame_key(frame: Mapping[str, Any]) -> str:
    """Return exact browser content without acquisition-only clock fields."""

    material = dict(frame)
    material.pop("captured_at", None)
    material.pop("request_seconds", None)
    live = material.get("live")
    if isinstance(live, Mapping):
        material["live"] = {
            key: value for key, value in live.items() if key not in {"frame_seq", "last_frame_at"}
        }
    return json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _gex_authority_frame_key(frame: Mapping[str, Any]) -> str:
    live = frame.get("live")
    live_meta = live if isinstance(live, Mapping) else {}
    return json.dumps(
        {
            "ok": frame.get("ok"),
            "status": frame.get("status"),
            "degraded": frame.get("degraded"),
            "frame_complete": frame.get("frame_complete"),
            "decision_authoritative": frame.get("decision_authoritative"),
            "publishable": live_meta.get("publishable"),
            "warming": live_meta.get("warming"),
            "quality_reason": live_meta.get("quality_reason"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def _run_gex_stream_producer(
    producer: _GexStreamProducer,
    deps: GexStreamWsDeps,
) -> None:
    try:
        while True:
            if deps.server_sleeping():
                await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
                continue
            persisted_bucket = ""
            if isinstance(producer.frame, dict):
                live_meta = (
                    producer.frame.get("live")
                    if isinstance(producer.frame.get("live"), dict)
                    else {}
                )
                persisted_bucket = str(live_meta.get("persisted_bucket") or "")
            if (
                GEX_CHART_HISTORY_HOURS > 0
                and producer.history_task is None
                and (producer.frame is not None or not producer.history_ready)
                and (
                    producer.history_refreshed_at <= 0
                    or (persisted_bucket and persisted_bucket != producer.last_history_bucket)
                    or time.monotonic() - producer.history_refreshed_at
                    >= GEX_STREAM_HISTORY_REFRESH_SECONDS
                )
            ):
                _schedule_gex_history_refresh(producer)
            _consume_gex_history_refresh(producer)
            if producer.history_ready and persisted_bucket:
                producer.last_history_bucket = persisted_bucket
            try:
                now = time.monotonic()
                if now - producer.route_checked_at >= GEX_STREAM_ROUTE_CHECK_SECONDS:
                    producer.route_checked_at = now
                    current_instrument = await run_physical_thread_call(
                        lookup_runtime_instrument,
                        producer.instrument_id,
                    )
                    current_route = route_instrument(current_instrument)
                    if (
                        current_route.instrument_id != producer.key[0]
                        or current_route.fingerprint != producer.key[1]
                    ):
                        message = (
                            "GEX_ROUTE_CHANGED: provider contract changed; reconnecting live GEX."
                        )
                        producer.error = message
                        producer.transport_status = {
                            "ok": False,
                            "status": "error",
                            "message": message,
                            "error": {
                                "code": "GEX_ROUTE_CHANGED",
                                "category": "gex",
                                "retryable": True,
                                "message": message,
                            },
                        }
                        producer.transport_status_key = (
                            "error",
                            "GEX_ROUTE_CHANGED",
                            "",
                            message,
                        )
                        producer.transport_status_revision += 1
                        producer.revision += 1
                        return
                if producer.transport_retry_at > now:
                    await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
                    continue
                phase_started = time.monotonic()
                frame = await producer.route.adapter.async_load_live_gex(
                    producer.route.instrument,
                    enabled=True,
                    store=producer.store,
                )
                observe_metric(
                    "gex_live_stream_phase_seconds",
                    max(time.monotonic() - phase_started, 0.0),
                    route_fingerprint=producer.key[1],
                    phase="load_context",
                )
                if not frame.get("ok"):
                    error_msg = str(frame.get("message") or "GEX live stream encountered an error.")
                    status = str(frame.get("status") or "error")
                    error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
                    error_code = str(error.get("code") or "GEX_LIVE_ERROR")
                    backoff = frame.get("backoff") if isinstance(frame.get("backoff"), dict) else {}
                    failure_reason = str(error.get("reason") or backoff.get("reason") or "")
                    failure_diagnostics = (
                        frame.get("diagnostics")
                        if isinstance(frame.get("diagnostics"), dict)
                        else error.get("diagnostics")
                        if isinstance(error.get("diagnostics"), dict)
                        else backoff.get("diagnostics")
                        if isinstance(backoff.get("diagnostics"), dict)
                        else {}
                    )
                    retry_at = str(backoff.get("retry_at") or "")
                    status_key = (
                        status,
                        error_code,
                        retry_at,
                        "" if retry_at else error_msg,
                    )
                    if status_key != producer.transport_status_key:
                        producer.error = error_msg
                        producer.transport_status = {
                            "ok": False,
                            "status": status,
                            "message": error_msg,
                            **({"error": dict(error)} if error else {}),
                            **({"backoff": dict(backoff)} if backoff else {}),
                            **({"reason": failure_reason} if failure_reason else {}),
                            **(
                                {"diagnostics": dict(failure_diagnostics)}
                                if failure_diagnostics
                                else {}
                            ),
                            **(
                                {"live": dict(frame["live"])}
                                if isinstance(frame.get("live"), dict)
                                else {}
                            ),
                        }
                        producer.transport_status_key = status_key
                        producer.transport_status_revision += 1
                        producer.revision += 1
                    retry_in_seconds = backoff.get("retry_in_seconds")
                    if backoff.get("active") is True and type(retry_in_seconds) in {int, float}:
                        producer.transport_retry_at = time.monotonic() + max(
                            float(retry_in_seconds), 0.0
                        )
                    elif error_code in {
                        "GEX_LIVE_SESSION_CLOSED",
                        "GEX_LIVE_SESSION_UNKNOWN",
                    }:
                        producer.transport_retry_at = (
                            time.monotonic() + GEX_STREAM_ROUTE_CHECK_SECONDS
                        )
                    else:
                        producer.transport_retry_at = 0.0
                    await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
                    continue

                phase_started = time.monotonic()
                frame = project_gex_context_for_chart(
                    _prepare_gex_live_frame(dict(frame), max_levels=GEX_CONTEXT_MAX_LEVELS)
                )
                observe_metric(
                    "gex_live_stream_phase_seconds",
                    max(time.monotonic() - phase_started, 0.0),
                    route_fingerprint=producer.key[1],
                    phase="chart_projection",
                )
                live = frame["live"]
                frame_seq = live["frame_seq"]
                phase_started = time.monotonic()
                material_key = _gex_material_frame_key(frame)
                authority_key = _gex_authority_frame_key(frame)
                observe_metric(
                    "gex_live_stream_phase_seconds",
                    max(time.monotonic() - phase_started, 0.0),
                    route_fingerprint=producer.key[1],
                    phase="material_key",
                )
                if (
                    producer.frame is None
                    or material_key != producer.material_key
                    or producer.transport_status is not None
                ):
                    producer.frame = frame
                    producer.frame_seq = frame_seq
                    producer.material_key = material_key
                    if authority_key != producer.authority_key:
                        producer.authority_key = authority_key
                        producer.authority_revision += 1
                    producer.error = ""
                    producer.transport_status = None
                    producer.transport_status_key = None
                    producer.transport_retry_at = 0.0
                    producer.revision += 1
                elif frame_seq != producer.frame_seq:
                    producer.frame = frame
                    producer.frame_seq = frame_seq
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc)
                status_key = ("error", exc.__class__.__name__, "", message)
                if status_key != producer.transport_status_key:
                    producer.error = message
                    producer.transport_status = {
                        "ok": False,
                        "status": "error",
                        "message": message,
                        "error": {
                            "code": exc.__class__.__name__,
                            "category": "gex",
                            "retryable": True,
                            "message": message,
                        },
                    }
                    producer.transport_status_key = status_key
                    producer.transport_status_revision += 1
                    producer.revision += 1
            await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
    finally:
        if producer.history_task is not None:
            producer.history_task.cancel()
            await asyncio.gather(producer.history_task, return_exceptions=True)
            producer.history_task = None


def _acquire_gex_stream_producer(
    *,
    instrument_id: str,
    provider_symbol: str,
    route: Any,
    store: Any,
    deps: GexStreamWsDeps,
) -> _GexStreamProducer:
    key = (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(route.fingerprint, field="route_fingerprint"),
    )
    producer = _GEX_STREAM_PRODUCERS.get(key)
    if producer is None:
        producer = _GexStreamProducer(
            key=key,
            instrument_id=instrument_id,
            provider_symbol=provider_symbol,
            route=route,
            store=store,
            deps=deps,
            history_ready=GEX_CHART_HISTORY_HOURS <= 0,
        )
        producer.task = asyncio.create_task(
            _run_gex_stream_producer(producer, deps),
            name=f"gex-stream-producer:{provider_symbol}",
        )
        _GEX_STREAM_PRODUCERS[key] = producer
    producer.consumers += 1
    return producer


async def _release_gex_stream_producer(producer: _GexStreamProducer) -> None:
    await run_cancellation_deferred(
        _release_gex_stream_producer_owned(producer),
        task_cancelled_error="GEX_STREAM_RELEASE_TASK_CANCELLED",
    )


async def _release_gex_stream_producer_owned(producer: _GexStreamProducer) -> None:
    if producer.consumers <= 0:
        raise RuntimeError("GEX stream producer consumer count underflow")
    producer.consumers -= 1
    if producer.consumers or _GEX_STREAM_PRODUCERS.get(producer.key) is not producer:
        return
    producer.closing = True
    if producer.task is not None:
        producer.task.cancel()
        await asyncio.gather(producer.task, return_exceptions=True)
        producer.task = None
    try:
        if producer.consumers == 0:
            await producer.route.adapter.async_stop_live_gex(producer.route.instrument)
    finally:
        if producer.consumers:
            if producer.deps is None:
                raise RuntimeError(
                    "GEX stream producer dependencies are unavailable during reconnect"
                )
            producer.task = asyncio.create_task(
                _run_gex_stream_producer(producer, producer.deps),
                name=f"gex-stream-producer:{producer.provider_symbol}",
            )
        elif _GEX_STREAM_PRODUCERS.get(producer.key) is producer:
            _GEX_STREAM_PRODUCERS.pop(producer.key, None)
        producer.closing = False


async def _send_gex_history_update(
    websocket: WebSocket,
    producer: _GexStreamProducer,
    *,
    last_history_revision: int,
) -> int:
    history_revision = producer.history_revision
    if history_revision == last_history_revision:
        return last_history_revision
    common = {
        "provider_symbol": producer.provider_symbol,
        "instrument_id": producer.instrument_id,
        "route_fingerprint": producer.route.fingerprint,
        "history_status": _gex_history_status(producer),
    }
    delta = producer.history_delta
    wire_history = producer.wire_history
    if (
        last_history_revision >= 0
        and isinstance(delta, dict)
        and type(delta.get("base_history_revision")) is int
        and delta["base_history_revision"] == last_history_revision
        and type(delta.get("history_revision")) is int
        and delta["history_revision"] == history_revision
    ):
        message = {"type": "gex_history_delta", **common, **delta}
    else:
        message = {
            "type": "gex_history_snapshot",
            **common,
            "history_revision": history_revision,
            "history": wire_history,
        }
    await send_stream_json(websocket, message, "gex")
    return history_revision


async def run_gex_stream(
    websocket: WebSocket,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    deps: GexStreamWsDeps,
) -> None:
    await websocket.accept()
    instrument = await run_physical_thread_call(
        lookup_runtime_instrument,
        instrument_id,
    )
    route = route_instrument(instrument)
    expected_route = require_exact_identity_text(
        expected_route_fingerprint, field="route_fingerprint"
    )
    if route.fingerprint != expected_route:
        raise ValueError(
            f"GEX_STREAM_ROUTE_CHANGED expected={expected_route_fingerprint} actual={route.fingerprint}"
        )
    provider_symbol = route.provider_symbol
    if not route.adapter.capabilities.gex:
        await send_stream_json(
            websocket,
            {
                "type": "gex_status",
                "ok": False,
                "status": "unsupported",
                "provider_symbol": provider_symbol,
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "message": f"GEX is not supported by provider {route.provider}.",
            },
            "gex",
        )
        return
    await deps.apply_provider_runtime_settings_async()
    store = await run_physical_thread_call(deps.store_factory)
    producer = _acquire_gex_stream_producer(
        instrument_id=route.instrument_id,
        provider_symbol=provider_symbol,
        route=route,
        store=store,
        deps=deps,
    )
    first_frame = True
    last_revision = -1
    last_status_revision = -1
    last_authority_revision = -1
    last_history_revision = -1
    idle_seconds = 0.0
    last_frame_sent_at = 0.0
    transport_status_active = False
    try:
        while True:
            if deps.server_sleeping():
                await send_stream_json(
                    websocket,
                    {
                        "type": "gex_status",
                        "ok": False,
                        "status": "sleeping",
                        "provider_symbol": provider_symbol,
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                        "message": "Server sleeping: GEX stream paused.",
                    },
                    "gex",
                )
                return
            if producer.closing:
                await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
                continue
            if (
                producer.transport_status is not None
                and producer.transport_status_revision != last_status_revision
            ):
                await send_stream_json(
                    websocket,
                    {
                        "type": "gex_status",
                        **producer.transport_status,
                        "provider_symbol": provider_symbol,
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                    },
                    "gex",
                )
                last_status_revision = producer.transport_status_revision
                idle_seconds = 0.0
                transport_status_active = True
            history_changed = producer.history_revision != last_history_revision
            if producer.frame is None or producer.transport_status is not None:
                if history_changed:
                    last_history_revision = await _send_gex_history_update(
                        websocket,
                        producer,
                        last_history_revision=last_history_revision,
                    )
                if producer.task is not None and producer.task.done():
                    return
                idle_seconds += GEX_STREAM_FRAME_SECONDS
                if idle_seconds >= deps.websocket_heartbeat_seconds:
                    await send_stream_json(
                        websocket,
                        {
                            "type": "gex_heartbeat",
                            "ok": True,
                            "provider_symbol": provider_symbol,
                            "instrument_id": route.instrument_id,
                            "route_fingerprint": route.fingerprint,
                            "frame_available": producer.frame is not None,
                            "status_revision": producer.transport_status_revision,
                        },
                        "gex",
                    )
                    idle_seconds = 0.0
                await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
                continue
            if producer.task is not None and producer.task.done():
                return
            recovered_from_transport_status = transport_status_active
            transport_status_active = False
            if producer.revision == last_revision and not history_changed:
                idle_seconds += GEX_STREAM_FRAME_SECONDS
                if idle_seconds >= deps.websocket_heartbeat_seconds:
                    live_meta = (
                        producer.frame.get("live")
                        if isinstance(producer.frame.get("live"), dict)
                        else {}
                    )
                    await send_stream_json(
                        websocket,
                        {
                            "type": "gex_heartbeat",
                            "ok": True,
                            "provider_symbol": provider_symbol,
                            "instrument_id": route.instrument_id,
                            "route_fingerprint": route.fingerprint,
                            "frame_seq": live_meta["frame_seq"],
                        },
                        "gex",
                    )
                    idle_seconds = 0.0
                await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
                continue
            frame = dict(producer.frame)
            frame["history_status"] = _gex_history_status(producer)
            if producer.history_error:
                frame["degraded"] = True
            live_meta = frame.get("live")
            if not isinstance(live_meta, dict):
                raise ValueError("GEX stream frame lost typed live metadata")
            seq = live_meta.get("frame_seq")
            if type(seq) is not int or seq <= 0:
                raise ValueError("GEX stream frame lost its positive frame_seq")
            sent_initial_frame = first_frame
            if sent_initial_frame:
                frame["history_revision"] = max(last_history_revision, 0)
                await send_stream_json(
                    websocket,
                    {
                        "type": "gex_frame",
                        **frame,
                        "provider_symbol": provider_symbol,
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                    },
                    "gex",
                )
                first_frame = False
                last_revision = producer.revision
                last_authority_revision = producer.authority_revision
                last_frame_sent_at = time.monotonic()
                idle_seconds = 0.0
            if history_changed:
                last_history_revision = await _send_gex_history_update(
                    websocket,
                    producer,
                    last_history_revision=last_history_revision,
                )
            frame["history_revision"] = max(last_history_revision, 0)
            publication_due = (
                time.monotonic() - last_frame_sent_at >= GEX_STREAM_PUBLICATION_SECONDS
            )
            if not sent_initial_frame and (
                (producer.revision != last_revision and publication_due)
                or producer.authority_revision != last_authority_revision
                or recovered_from_transport_status
            ):
                await send_stream_json(
                    websocket,
                    {
                        "type": "gex_frame",
                        **frame,
                        "provider_symbol": provider_symbol,
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                    },
                    "gex",
                )
                last_revision = producer.revision
                last_authority_revision = producer.authority_revision
                last_frame_sent_at = time.monotonic()
                idle_seconds = 0.0
            elif not sent_initial_frame:
                idle_seconds += GEX_STREAM_FRAME_SECONDS
                if idle_seconds >= deps.websocket_heartbeat_seconds:
                    await send_stream_json(
                        websocket,
                        {
                            "type": "gex_heartbeat",
                            "ok": True,
                            "provider_symbol": provider_symbol,
                            "instrument_id": route.instrument_id,
                            "route_fingerprint": route.fingerprint,
                            "frame_seq": seq,
                        },
                        "gex",
                    )
                    idle_seconds = 0.0
            await asyncio.sleep(GEX_STREAM_FRAME_SECONDS)
    except WebSocketDisconnect:
        return
    finally:
        await _release_gex_stream_producer(producer)
