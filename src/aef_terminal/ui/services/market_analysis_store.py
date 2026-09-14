from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

from starlette.responses import Response

from aef_terminal.data.gex.constants import GEX_SCHEDULER_INTERVAL_MINUTES
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.indicators.registry import INDICATOR_REGISTRY, indicator_enabled
from aef_terminal.ui.routers.error_payloads import build_error_payload
from aef_terminal.ui.services.market_analysis_compaction import (
    compact_runtime_payload_for_response,
)


class MarketAnalysisStateLock(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None: ...


_PERIODIC_SHARED_CONTEXT_REFS = frozenset(
    {
        "option_flow",
        "tick_flow",
    }
)

MARKET_ANALYSIS_EFFECTS_STANDARD = "standard"
MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE = "research_capture"
MARKET_ANALYSIS_RESEARCH_REFRESH_SECONDS = 60.0
_MARKET_ANALYSIS_EFFECTS = frozenset(
    {
        MARKET_ANALYSIS_EFFECTS_STANDARD,
        MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
    }
)


def market_analysis_effects(payload: dict[str, Any]) -> str:
    raw = payload.get("analysis_effects", MARKET_ANALYSIS_EFFECTS_STANDARD)
    if not isinstance(raw, str) or raw not in _MARKET_ANALYSIS_EFFECTS:
        raise ValueError("analysis_effects must be a registered exact value")
    return raw


def _market_analysis_cache_snapshot_bytes(item: dict[str, Any]) -> bytes:
    if not isinstance(item, dict):
        raise TypeError("market analysis cache item must be a mapping")
    if item.get("status") != "ready":
        raise ValueError("market analysis cache item must be ready before reading its snapshot")
    raw = item.get("snapshot_json")
    if not isinstance(raw, bytes):
        raise TypeError("ready market analysis cache item must contain snapshot bytes")
    size_bytes = item.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes != len(raw):
        raise ValueError("market analysis cache byte size does not match its snapshot")
    return raw


def decode_market_analysis_cache_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    """Decode the immutable bytes payload owned by one ready cache record."""

    raw = _market_analysis_cache_snapshot_bytes(item)
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("market analysis cache snapshot is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise TypeError("market analysis cache snapshot must decode to a mapping")
    return parsed


def market_analysis_cache_item_size(item: dict[str, Any]) -> int:
    if not isinstance(item, dict):
        raise TypeError("market analysis cache item must be a mapping")
    if item.get("status") == "error":
        return 0
    return len(_market_analysis_cache_snapshot_bytes(item))


def build_market_analysis_snapshot_response(
    key: str,
    *,
    cached: dict[str, Any] | None,
    task_running: bool,
    task_queued: bool = False,
    instrument_id: str = "",
    route_fingerprint: str = "",
    vsa_render_hours: int = 6,
) -> Any:
    status_scope = (
        {
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
        }
        if instrument_id and route_fingerprint
        else {}
    )
    if not key:
        payload = build_error_payload(
            code="MARKET_ANALYSIS_KEY_MISSING",
            category="analysis",
            retryable=False,
            error="Missing analysis key.",
            include_message_field=True,
        )
        payload["status"] = "missing"
        payload.update(status_scope)
        return payload
    if cached and cached.get("status") == "ready":
        snapshot = decode_market_analysis_cache_snapshot(cached)
        raw = _market_analysis_cache_snapshot_bytes(cached)
        if isinstance(snapshot.get("vsa_volume"), dict):
            projected = compact_runtime_payload_for_response(
                deepcopy(snapshot),
                vsa_render_hours=vsa_render_hours,
            )
            raw = json.dumps(projected, separators=(",", ":")).encode("utf-8")
        return Response(content=raw, media_type="application/json")
    if cached and cached.get("status") == "error":
        message = str(cached.get("message") or "analysis failed")
        error_code = str(cached.get("error_code") or "MARKET_ANALYSIS_FAILED")
        payload = build_error_payload(
            code=error_code,
            category="analysis",
            retryable=cached.get("retryable") is not False,
            error=message,
            include_message_field=True,
        )
        payload["status"] = "error"
        payload["analysis_key"] = key
        payload.update(status_scope)
        return payload
    if task_running:
        payload = build_error_payload(
            code="MARKET_ANALYSIS_RUNNING",
            category="analysis",
            retryable=True,
            error="Analysis is still running.",
            include_message_field=True,
        )
        payload["status"] = "running"
        payload["analysis_key"] = key
        payload.update(status_scope)
        return payload
    if task_queued:
        payload = build_error_payload(
            code="MARKET_ANALYSIS_QUEUED",
            category="analysis",
            retryable=True,
            error="Analysis is queued.",
            include_message_field=True,
        )
        payload["status"] = "queued"
        payload["analysis_key"] = key
        payload.update(status_scope)
        return payload
    payload = build_error_payload(
        code="MARKET_ANALYSIS_NOT_FOUND",
        category="analysis",
        retryable=False,
        error="Analysis snapshot not found.",
        include_message_field=True,
    )
    payload["status"] = "missing"
    payload["analysis_key"] = key
    payload.update(status_scope)
    return payload


def build_market_analysis_error_item(
    error: BaseException | str,
    *,
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any]:
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    exact_route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    message = str(error).strip()
    if not message and isinstance(error, BaseException):
        message = error.__class__.__name__
    raw_error_code = getattr(error, "error_code", "")
    error_code = (
        raw_error_code
        if isinstance(raw_error_code, str) and raw_error_code
        else "MARKET_ANALYSIS_FAILED"
    )
    return {
        "status": "error",
        "message": message or "analysis failed",
        "error_code": error_code,
        "retryable": getattr(error, "retryable", True) is not False,
        "instrument_id": exact_instrument_id,
        "route_fingerprint": exact_route_fingerprint,
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "updated_monotonic": time.monotonic(),
    }


def market_analysis_key(payload: dict[str, Any]) -> str:
    stable_payload = dict(payload)
    stable_payload.pop("market_version", None)
    stable_payload.pop("parent_canonical_generation", None)
    raw = json.dumps(stable_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def market_analysis_request_identity(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(payload.get("source") or ""),
        require_exact_identity_text(payload.get("instrument_id"), field="instrument_id"),
        require_exact_identity_text(payload.get("route_fingerprint"), field="route_fingerprint"),
        str(payload.get("interval") or ""),
        str(payload.get("range") or ""),
        str(payload.get("signal_range") or ""),
        bool(payload.get("show_visuals")),
        payload.get("gex_context_active") is True,
        str(payload.get("gex_capture_mode") or "request"),
        market_analysis_effects(payload),
    )


def market_analysis_refresh_seconds(payload: dict[str, Any], *, default_seconds: float) -> float:
    if market_analysis_effects(payload) == MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE:
        return MARKET_ANALYSIS_RESEARCH_REFRESH_SECONDS
    indicator_params = (
        payload.get("indicator_params") if isinstance(payload.get("indicator_params"), dict) else {}
    )
    declared_intervals: list[float] = [
        float(spec.analysis_refresh_interval_seconds)
        for indicator_id, spec in INDICATOR_REGISTRY.items()
        if spec.analysis_refresh_interval_seconds is not None
        and indicator_enabled(indicator_params, indicator_id)
    ]
    if payload.get("gex_context_active") is True:
        declared_intervals.append(float(GEX_SCHEDULER_INTERVAL_MINUTES * 60))
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        if not indicator_enabled(indicator_params, indicator_id):
            continue
        if "option_flow" in spec.shared_context_refs:
            declared_intervals.append(float(GEX_SCHEDULER_INTERVAL_MINUTES * 60))
        if _PERIODIC_SHARED_CONTEXT_REFS.intersection(spec.shared_context_refs).difference(
            {"option_flow"}
        ):
            declared_intervals.append(float(default_seconds))
    return min(declared_intervals) if declared_intervals else float(default_seconds)


def market_analysis_requires_periodic_refresh(
    payload: dict[str, Any],
) -> bool:
    if market_analysis_effects(payload) == MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE:
        return True
    if payload.get("gex_context_active") is True:
        return True
    indicator_params = (
        payload.get("indicator_params") if isinstance(payload.get("indicator_params"), dict) else {}
    )
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        if not indicator_enabled(indicator_params, indicator_id):
            continue
        if spec.analysis_refresh_interval_seconds is not None:
            return True
        if _PERIODIC_SHARED_CONTEXT_REFS.intersection(spec.shared_context_refs):
            return True
    return False


def trim_market_analysis_cache(
    cache: dict[str, dict[str, Any]],
    *,
    ttl_seconds: float,
    max_size: int,
    max_bytes: int | None = None,
    now: float,
    protected_keys: frozenset[str] = frozenset(),
) -> None:
    expired = [
        key
        for key, item in cache.items()
        if key not in protected_keys
        if now - float(item.get("updated_monotonic") or 0.0) > ttl_seconds
    ]
    for key in expired:
        cache.pop(key, None)
    if len(cache) > max_size:
        ordered = sorted(
            cache.items(),
            key=lambda item: float(item[1].get("updated_monotonic") or 0.0),
        )
        for key, _ in ordered[: max(len(cache) - max_size, 0)]:
            cache.pop(key, None)
    if max_bytes is None or max_bytes <= 0:
        return
    total_bytes = sum(market_analysis_cache_item_size(item) for item in cache.values())
    if total_bytes <= max_bytes:
        return
    ordered = sorted(
        cache.items(),
        key=lambda item: float(item[1].get("updated_monotonic") or 0.0),
    )
    for key, item in ordered:
        if total_bytes <= max_bytes:
            break
        total_bytes -= market_analysis_cache_item_size(item)
        cache.pop(key, None)


def revoke_market_analysis_wanted_locked(
    key: str,
    *,
    wanted: dict[str, dict[str, Any]],
    tasks: dict[str, asyncio.Task[Any]],
    identities: dict[str, tuple[Any, ...]] | None = None,
    cancel_task: Callable[[str, asyncio.Task[Any]], None] | None = None,
) -> None:
    """Revoke demand and cancel without releasing active task ownership."""

    wanted.pop(key, None)
    task = tasks.get(key)
    if task is not None and not task.done():
        if cancel_task is None:
            task.cancel()
        else:
            cancel_task(key, task)
        return
    tasks.pop(key, None)
    if identities is not None:
        identities.pop(key, None)


async def register_market_analysis_wanted(
    key: str,
    payload: dict[str, Any],
    *,
    lock: MarketAnalysisStateLock,
    wanted: dict[str, dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    tasks: dict[str, asyncio.Task[Any]],
    request_identity: Callable[[dict[str, Any]], tuple[Any, ...]],
    cache_ttl_seconds: float,
    cache_max_size: int,
    cache_max_bytes: int | None = None,
    identities: dict[str, tuple[Any, ...]] | None = None,
    prepare_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    cache_admitted: Callable[
        [dict[str, Any], dict[str, Any]],
        bool,
    ]
    | None = None,
    cancel_task: Callable[[str, asyncio.Task[Any]], None] | None = None,
    on_payload_superseded: Callable[[str], None] | None = None,
    registration_admitted_locked: Callable[[str, float], bool] | None = None,
    on_registered_locked: Callable[[str, float], None] | None = None,
) -> str:
    now = time.monotonic()
    async with lock:
        if registration_admitted_locked is not None and not registration_admitted_locked(key, now):
            return "superseded"
        trim_market_analysis_cache(
            cache,
            ttl_seconds=cache_ttl_seconds,
            max_size=cache_max_size,
            max_bytes=cache_max_bytes,
            now=now,
            protected_keys=frozenset((*wanted, key)),
        )
        canonical_payload = prepare_payload(payload) if prepare_payload is not None else payload
        identity = request_identity(canonical_payload)
        current_item = wanted.get(key)
        current_payload = current_item.get("payload") if isinstance(current_item, dict) else None
        payload_superseded = (
            isinstance(current_payload, dict)
            and cache_admitted is not None
            and not cache_admitted(current_payload, canonical_payload)
        )
        if payload_superseded:
            revoke_market_analysis_wanted_locked(
                key,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cancel_task=cancel_task,
            )
            if on_payload_superseded is not None:
                on_payload_superseded(key)
        stale_keys = [
            existing_key
            for existing_key, item in wanted.items()
            if existing_key != key and request_identity(item.get("payload") or {}) == identity
        ]
        for existing_key in stale_keys:
            revoke_market_analysis_wanted_locked(
                existing_key,
                wanted=wanted,
                tasks=tasks,
                identities=identities,
                cancel_task=cancel_task,
            )
            cache.pop(existing_key, None)
        wanted[key] = {
            "payload": canonical_payload,
            "wanted_at": now,
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }
        if on_registered_locked is not None:
            on_registered_locked(key, now)
        cached = cache.get(key)
        if (
            cached is not None
            and cache_admitted is not None
            and not cache_admitted(cached, canonical_payload)
        ):
            cache.pop(key, None)
            cached = None
        if cached and cached.get("status") == "ready":
            return "ready"
        task = tasks.get(key)
        if task and not task.done():
            return "running"
        return "queued"
