from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from aef_terminal.data.instrument_identity import (
    instrument_provider,
    provider_symbol as exact_provider_symbol,
    qualified_instrument_id,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import OptionUniverseUnavailableError
from aef_terminal.runtime.async_tasks import (
    await_cancellation_deferred_task,
    run_physical_thread_call,
)
from aef_terminal.runtime.telemetry import log_structured_error
from aef_terminal.data.gex.config import GexRequestConfig
from aef_terminal.data.gex.constants import (
    GEX_CHART_HISTORY_HOURS,
    GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES,
    GEX_GENERIC_TICKS,
    GEX_REQUEST_SNAPSHOT_SOURCE,
    LOGGER,
)
from aef_terminal.data.gex.contracts import (
    GexUnderlyingQuoteUnavailableError,
    require_gex_valuation_rates,
)
from aef_terminal.data.gex.dividend import (
    _dividend_yield_for_gex_request,
    _maybe_update_dividend_yield_cache,
)
from aef_terminal.data.gex.history import (
    _cached_gex_history_projection,
    latest_gex_snapshot_payload,
    get_gex_history_series,
    persist_gex_snapshot,
)
from aef_terminal.data.gex.payload import (
    _aggregate_by_strike,
    _payload_from_rows,
)
from aef_terminal.data.gex.provider_runtime import GexProviderRuntime
from aef_terminal.data.gex.quality import (
    _gex_diagnostics,
    _qualified_gex_contract_rows,
    require_gex_acquisition_spot_observation,
)
from aef_terminal.data.gex.session import _GEX_RUNTIME
from aef_terminal.data.gex.utils import finite_number_or_none, parse_gex_timestamp
from aef_terminal.data.provider_sessions import provider_schedule_open_state
from aef_terminal.runtime.metrics import increment_metric, observe_metric
from aef_terminal.runtime.telemetry import exception_message


@dataclass(frozen=True)
class _GexRequestCompletion:
    payload: dict[str, Any]
    option_universe_expires_at: datetime


@dataclass
class _GexRequestWork:
    """One acquisition owner and projections for its joined callers, not a cache."""

    capture: asyncio.Task[_GexRequestCompletion | dict[str, Any]]
    store: Any | None
    history: dict[tuple[int, int], asyncio.Task[dict[str, Any]]] = field(default_factory=dict)


_GEX_REQUEST_TASKS: dict[tuple[str, str], _GexRequestWork] = {}
GexProviderSessionState = Literal["open", "closed", "unknown"]


async def async_gex_context(
    *,
    enabled: bool,
    refresh: bool = False,
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    stale_minutes: int = GEX_CONTEXT_STALE_MINUTES,
    history_hours: int = GEX_CHART_HISTORY_HOURS,
    project_capture_history: bool = True,
    request: GexRequestConfig | None = None,
    store: Any | None = None,
    underlying_quote: dict[str, Any] | None = None,
    instrument: dict[str, Any],
    provider_runtime: GexProviderRuntime | None = None,
) -> dict[str, Any]:
    if type(project_capture_history) is not bool:
        raise TypeError("GEX project_capture_history must be boolean")
    if not enabled or not refresh:
        return await _async_gex_context_owned(
            enabled=enabled,
            refresh=refresh,
            max_levels=max_levels,
            stale_minutes=stale_minutes,
            history_hours=history_hours,
            request=request,
            store=store,
            underlying_quote=underlying_quote,
            instrument=instrument,
            provider_runtime=provider_runtime,
        )

    qualified = require_provider_identity(instrument)
    instrument_id = qualified_instrument_id(qualified)
    route_key = route_fingerprint(qualified)
    provider_key = instrument_provider(qualified)
    if provider_runtime is None:
        raise RuntimeError("GEX_PROVIDER_RUNTIME_REQUIRED")
    if provider_runtime.provider_key != provider_key:
        raise ValueError("GEX_PROVIDER_RUNTIME_MISMATCH")
    if request is not None and (
        request.instrument_id != instrument_id or request.route_fingerprint != route_key
    ):
        raise ValueError("GEX request route no longer matches the qualified instrument")
    task_key = (instrument_id, route_key)
    existing = _GEX_REQUEST_TASKS.get(task_key)
    if existing is not None and not existing.capture.done():
        return await _gex_request_result(
            existing,
            project_capture_history=project_capture_history,
            history_hours=history_hours,
            max_levels=max_levels,
        )

    provider_symbol = exact_provider_symbol(qualified, provider_key)
    _GEX_RUNTIME.acquire_capture_mode(
        instrument_id,
        route_key,
        "request",
        provider_symbol=provider_symbol,
    )

    async def run_owned_request() -> _GexRequestCompletion | dict[str, Any]:
        try:
            return await _async_gex_context_owned(
                enabled=True,
                refresh=True,
                max_levels=max_levels,
                stale_minutes=stale_minutes,
                history_hours=history_hours,
                request=request,
                store=store,
                underlying_quote=underlying_quote,
                instrument=qualified,
                provider_runtime=provider_runtime,
            )
        finally:
            _GEX_RUNTIME.release_capture_mode(instrument_id, route_key, "request")

    try:
        task = asyncio.create_task(
            run_owned_request(),
            name=f"gex-request:{instrument_id}:{route_key}",
        )
    except BaseException:
        _GEX_RUNTIME.release_capture_mode(instrument_id, route_key, "request")
        raise
    work = _GexRequestWork(task, store)
    _GEX_REQUEST_TASKS[task_key] = work

    def forget(_completed: asyncio.Task[_GexRequestCompletion | dict[str, Any]]) -> None:
        if _GEX_REQUEST_TASKS.get(task_key) is work:
            _GEX_REQUEST_TASKS.pop(task_key, None)

    task.add_done_callback(forget)
    return await _gex_request_result(
        work,
        project_capture_history=project_capture_history,
        history_hours=history_hours,
        max_levels=max_levels,
    )


async def _gex_request_result(
    work: _GexRequestWork,
    *,
    project_capture_history: bool,
    history_hours: int,
    max_levels: int,
) -> dict[str, Any]:
    completion = await await_cancellation_deferred_task(
        work.capture,
        task_cancelled_error="GEX_REQUEST_TASK_CANCELLED",
    )
    if not isinstance(completion, _GexRequestCompletion):
        # Cached/unavailable outcomes retain their existing advisory contract.
        return completion
    if not project_capture_history:
        payload = await run_physical_thread_call(deepcopy, completion.payload)
        _require_gex_request_delivery_expiry(completion.option_universe_expires_at)
        return payload
    projection_key = (history_hours, max_levels)
    projection = work.history.get(projection_key)
    if projection is None:
        projection = asyncio.create_task(
            run_physical_thread_call(
                _project_gex_request_history,
                completion,
                history_hours=history_hours,
                max_levels=max_levels,
                store=work.store,
            ),
            name="gex-request-history",
        )
        work.history[projection_key] = projection
    return await await_cancellation_deferred_task(
        projection,
        task_cancelled_error="GEX_REQUEST_HISTORY_TASK_CANCELLED",
    )


def gex_context(
    *,
    enabled: bool,
    refresh: bool = False,
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    stale_minutes: int = GEX_CONTEXT_STALE_MINUTES,
    history_hours: int = GEX_CHART_HISTORY_HOURS,
    request: GexRequestConfig | None = None,
    store: Any | None = None,
    underlying_quote: dict[str, Any] | None = None,
    instrument: dict[str, Any],
    provider_runtime: GexProviderRuntime | None = None,
) -> dict[str, Any]:
    instrument = require_provider_identity(instrument)
    provider_key = instrument_provider(instrument)
    if provider_runtime is not None and provider_runtime.provider_key != provider_key:
        raise ValueError("GEX_PROVIDER_RUNTIME_MISMATCH")
    provider_symbol = exact_provider_symbol(instrument, provider_key)
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    if not enabled:
        return {
            "ok": False,
            "enabled": False,
            "provider_symbol": provider_symbol,
            "instrument_id": instrument_id,
            "route_fingerprint": route_key,
            "source": GEX_REQUEST_SNAPSHOT_SOURCE,
            "capture_mode": "request",
            "status": "disabled",
            "message": "GEX disabled; no option-chain requests are running.",
            "levels": [],
            "history": [],
        }
    if refresh:
        raise RuntimeError("GEX_ASYNC_REFRESH_REQUIRED")

    return cached_gex_context(
        instrument=instrument,
        max_levels=max_levels,
        stale_minutes=stale_minutes,
        history_hours=history_hours,
        store=store,
    )


async def _async_gex_context_owned(
    *,
    enabled: bool,
    refresh: bool = False,
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    stale_minutes: int = GEX_CONTEXT_STALE_MINUTES,
    history_hours: int = GEX_CHART_HISTORY_HOURS,
    request: GexRequestConfig | None = None,
    store: Any | None = None,
    underlying_quote: dict[str, Any] | None = None,
    instrument: dict[str, Any],
    provider_runtime: GexProviderRuntime | None = None,
) -> _GexRequestCompletion | dict[str, Any]:
    instrument = require_provider_identity(instrument)
    provider_key = instrument_provider(instrument)
    if provider_runtime is not None and provider_runtime.provider_key != provider_key:
        raise ValueError("GEX_PROVIDER_RUNTIME_MISMATCH")
    provider_symbol = exact_provider_symbol(instrument, provider_key)
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    if not enabled:
        return {
            "ok": False,
            "enabled": False,
            "provider_symbol": provider_symbol,
            "instrument_id": instrument_id,
            "route_fingerprint": route_key,
            "source": GEX_REQUEST_SNAPSHOT_SOURCE,
            "capture_mode": "request",
            "status": "disabled",
            "message": "GEX disabled; no option-chain requests are running.",
            "levels": [],
            "history": [],
        }
    if refresh:
        if provider_runtime is None:
            raise RuntimeError("GEX_PROVIDER_RUNTIME_REQUIRED")
        if request is None:
            request = await run_physical_thread_call(
                provider_runtime.request_config,
                instrument=instrument,
                store=store,
                mode="auto",
            )
        dividend_cache = await run_physical_thread_call(
            _dividend_yield_for_gex_request,
            request,
            configured_override=provider_runtime.dividend_yield_override_active(),
            store=store,
        )
        backoff = _GEX_RUNTIME.failure_backoff(
            instrument_id,
            route_key,
            lane="request",
        )
        if request.refresh_mode != "manual" and backoff.get("active"):
            if backoff.get("kind") == "option_universe_unavailable":
                unavailable = await run_physical_thread_call(
                    _cached_gex_option_universe_unavailable,
                    OptionUniverseUnavailableError(
                        str(
                            backoff.get("message")
                            or "the exact provider option universe is temporarily unavailable"
                        ),
                        reason=str(backoff.get("reason") or "OPTION_UNIVERSE_UNAVAILABLE"),
                        diagnostics=(
                            backoff.get("diagnostics")
                            if isinstance(backoff.get("diagnostics"), dict)
                            else {}
                        ),
                    ),
                    instrument=instrument,
                    max_levels=max_levels,
                    stale_minutes=stale_minutes,
                    history_hours=history_hours,
                    store=store,
                )
                unavailable["backoff"] = dict(backoff)
                return unavailable
            cached = await run_physical_thread_call(
                cached_gex_context,
                instrument=instrument,
                max_levels=max_levels,
                stale_minutes=stale_minutes,
                history_hours=history_hours,
                store=store,
            )
            cached["ok"] = bool(cached.get("levels"))
            cached["status"] = "refresh_error"
            cached["degraded"] = True
            cached["decision_authoritative"] = False
            cached["backoff"] = dict(backoff)
            cached["message"] = (
                f"GEX refresh is in route backoff after {backoff.get('source') or 'provider'} failure; "
                f"retry in {float(backoff.get('retry_in_seconds') or 0.0):.1f}s. "
                "Showing latest cached snapshot."
            )
            return cached
        provider_session_state = await run_physical_thread_call(
            gex_provider_session_state,
            store=store,
            instrument=instrument,
        )
        if provider_session_state != "open":
            return await run_physical_thread_call(
                _cached_gex_refresh_blocked,
                instrument=instrument,
                provider_session_state=provider_session_state,
                max_levels=max_levels,
                stale_minutes=stale_minutes,
                history_hours=history_hours,
                store=store,
            )
        try:
            payload = await provider_runtime.run_sync(
                "gex",
                f"{provider_symbol} options",
                lambda: _collect_live_gex_context_owned(
                    provider_symbol,
                    max_levels=max_levels,
                    stale_minutes=stale_minutes,
                    request=request,
                    dividend_cache=dividend_cache,
                    underlying_quote=underlying_quote,
                    instrument=instrument,
                    provider_runtime=provider_runtime,
                ),
                timeout=request.provider_metadata.timeout_seconds + 3.0,
            )
            return await run_physical_thread_call(
                _finalize_gex_request_payload,
                provider_symbol,
                payload,
                instrument=instrument,
                store=store,
            )
        except GexUnderlyingQuoteUnavailableError as exc:
            increment_metric(
                "gex_underlying_quote_preflight_skipped_total",
                route_fingerprint=route_key,
                source=str(request.refresh_mode or "auto"),
            )
            return await run_physical_thread_call(
                _cached_gex_underlying_quote_unavailable,
                exc,
                instrument=instrument,
                max_levels=max_levels,
                stale_minutes=stale_minutes,
                history_hours=history_hours,
                store=store,
            )
        except OptionUniverseUnavailableError as exc:
            unavailable_reason = exc.reason
            unavailable_diagnostics = dict(exc.diagnostics)
            if request.refresh_mode == "manual":
                failure_backoff = {}
            else:
                failure_backoff = _GEX_RUNTIME.record_failure(
                    instrument_id,
                    route_key,
                    exception_message(exc),
                    provider_symbol=provider_symbol,
                    source=request.refresh_mode,
                    lane="request",
                    kind="option_universe_unavailable",
                    reason=unavailable_reason,
                    diagnostics=unavailable_diagnostics,
                    base_backoff_seconds=(
                        1.0 if unavailable_reason == "OPTION_UNIVERSE_ROLLOVER" else 30.0
                    ),
                    max_backoff_seconds=(
                        5.0 if unavailable_reason == "OPTION_UNIVERSE_ROLLOVER" else 300.0
                    ),
                )
            unavailable = await run_physical_thread_call(
                _cached_gex_option_universe_unavailable,
                exc,
                instrument=instrument,
                max_levels=max_levels,
                stale_minutes=stale_minutes,
                history_hours=history_hours,
                store=store,
            )
            if failure_backoff:
                unavailable["backoff"] = dict(failure_backoff)
            return unavailable
        except Exception as exc:
            reason = exception_message(exc)
            failure_source = request.refresh_mode
            if failure_source == "manual":
                _GEX_RUNTIME.record_error(
                    instrument_id,
                    route_key,
                    reason,
                    provider_symbol=provider_symbol,
                    source=failure_source,
                )
                failure_backoff: dict[str, Any] = {}
            else:
                failure_backoff = _GEX_RUNTIME.record_failure(
                    instrument_id,
                    route_key,
                    reason,
                    provider_symbol=provider_symbol,
                    source=failure_source,
                    lane="request",
                )
                increment_metric(
                    "gex_route_backoff_total",
                    route_fingerprint=route_key,
                    source=failure_source,
                )
            cached = await run_physical_thread_call(
                cached_gex_context,
                instrument=instrument,
                max_levels=max_levels,
                stale_minutes=stale_minutes,
                history_hours=history_hours,
                store=store,
            )
            cached["ok"] = bool(cached.get("levels"))
            cached["status"] = "refresh_error"
            cached["degraded"] = True
            cached["decision_authoritative"] = False
            if failure_backoff:
                cached["backoff"] = dict(failure_backoff)
            cached["message"] = f"GEX refresh failed: {reason}. Showing latest cached snapshot."
            return cached

    return await run_physical_thread_call(
        cached_gex_context,
        instrument=instrument,
        max_levels=max_levels,
        stale_minutes=stale_minutes,
        history_hours=history_hours,
        store=store,
    )


def cached_gex_context(
    *,
    instrument: dict[str, Any],
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    stale_minutes: int = GEX_CONTEXT_STALE_MINUTES,
    history_hours: int = GEX_CHART_HISTORY_HOURS,
    store: Any | None = None,
) -> dict[str, Any]:
    instrument = require_provider_identity(instrument)
    provider_symbol = exact_provider_symbol(instrument, instrument_provider(instrument))
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    cache_rejection = ""
    try:
        db_payload = latest_gex_snapshot_payload(
            provider_symbol,
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            max_levels=max_levels,
            stale_minutes=stale_minutes,
            history_hours=history_hours,
            store=store,
        )
    except ValueError as exc:
        cache_rejection = str(exc)
        if not cache_rejection.startswith("GEX_HISTORY_CONTRACT_INVALID:"):
            raise
        previous = _GEX_RUNTIME.error_for_route(
            instrument_id,
            route_key,
            sources=("cache",),
        )
        if previous.get("message") != cache_rejection:
            LOGGER.warning(
                "Rejected noncanonical cached GEX snapshot for %s (%s): %s",
                instrument_id,
                route_key,
                cache_rejection,
            )
            increment_metric(
                "gex_cache_snapshot_rejected_total",
                route_fingerprint=route_key,
            )
            _GEX_RUNTIME.record_error(
                instrument_id,
                route_key,
                cache_rejection,
                provider_symbol=provider_symbol,
                source="cache",
            )
        db_payload = None
    else:
        _GEX_RUNTIME.clear_error(
            instrument_id,
            route_key,
            source="cache",
        )
    if db_payload is not None:
        db_payload["ok"] = bool(db_payload.get("levels"))
        return db_payload
    missing_message = f"No cached GEX snapshot for {provider_symbol}."
    latest_attempt = None
    if cache_rejection:
        missing_message = (
            "Stored GEX cache does not satisfy the current canonical contract; "
            "requesting a fresh broker snapshot."
        )
        latest_attempt = {
            "source": GEX_REQUEST_SNAPSHOT_SOURCE,
            "capture_mode": "request",
            "captured_at": None,
            "status": "invalid_cache",
            "message": cache_rejection,
            "diagnostics": None,
        }
    history: list[dict[str, Any]] = []
    history_status: dict[str, Any] | None = None
    if not cache_rejection:
        history, history_status = _cached_gex_history_projection(
            provider_symbol,
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            history_hours=history_hours,
            max_levels=max_levels,
            store=store,
        )
    return {
        "ok": False,
        "enabled": True,
        "provider_symbol": provider_symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": route_key,
        "source": GEX_REQUEST_SNAPSHOT_SOURCE,
        "capture_mode": "request",
        "status": "missing",
        "message": missing_message,
        "levels": [],
        "history": history,
        **({"history_status": history_status} if history_status is not None else {}),
        **({"latest_attempt": latest_attempt} if latest_attempt is not None else {}),
    }


def gex_provider_session_state(
    now: datetime | None = None,
    *,
    store: Any | None,
    instrument: dict[str, Any],
) -> GexProviderSessionState:
    state = provider_schedule_open_state(
        now or datetime.now(tz=UTC),
        liquid=False,
        store=store,
        instrument=instrument,
    )
    if state is True:
        return "open"
    if state is False:
        return "closed"
    return "unknown"


def _cached_gex_option_universe_unavailable(
    error: OptionUniverseUnavailableError,
    *,
    instrument: dict[str, Any],
    max_levels: int,
    stale_minutes: int,
    history_hours: int,
    store: Any | None = None,
) -> dict[str, Any]:
    cached = cached_gex_context(
        instrument=instrument,
        max_levels=max_levels,
        stale_minutes=stale_minutes,
        history_hours=history_hours,
        store=store,
    )
    message = exception_message(error)
    reason = str(
        getattr(error, "reason", "OPTION_UNIVERSE_UNAVAILABLE") or "OPTION_UNIVERSE_UNAVAILABLE"
    )
    raw_diagnostics = getattr(error, "diagnostics", {})
    diagnostics = dict(raw_diagnostics) if isinstance(raw_diagnostics, dict) else {}
    diagnostics["option_universe_reason"] = reason
    has_levels = bool(cached.get("levels"))
    cached["ok"] = has_levels
    cached["status"] = "unavailable"
    cached["degraded"] = has_levels
    cached["frame_complete"] = bool(cached.get("frame_complete")) if has_levels else False
    cached["decision_authoritative"] = False
    cached["message"] = f"GEX option universe is temporarily unavailable: {message}. " + (
        "Showing the latest cached snapshot as display-only."
        if has_levels
        else "No provider-qualified GEX frame is available."
    )
    cached["error"] = {
        "code": "GEX_OPTION_UNIVERSE_UNAVAILABLE",
        "category": "gex",
        "retryable": True,
        "reason": reason,
        "diagnostics": dict(diagnostics),
        "message": message,
    }
    cached["latest_attempt"] = {
        "source": GEX_REQUEST_SNAPSHOT_SOURCE,
        "capture_mode": "request",
        "captured_at": None,
        "status": "unavailable",
        "message": message,
        "diagnostics": diagnostics,
    }
    return cached


def _cached_gex_underlying_quote_unavailable(
    error: GexUnderlyingQuoteUnavailableError,
    *,
    instrument: dict[str, Any],
    max_levels: int,
    stale_minutes: int,
    history_hours: int,
    store: Any | None = None,
) -> dict[str, Any]:
    cached = cached_gex_context(
        instrument=instrument,
        max_levels=max_levels,
        stale_minutes=stale_minutes,
        history_hours=history_hours,
        store=store,
    )
    message = exception_message(error)
    reason = str(
        getattr(error, "reason", "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE")
        or "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
    )
    raw_diagnostics = getattr(error, "diagnostics", {})
    diagnostics = dict(raw_diagnostics) if isinstance(raw_diagnostics, dict) else {}
    diagnostics["underlying_quote_reason"] = reason
    has_levels = bool(cached.get("levels"))
    cached["ok"] = has_levels
    cached["status"] = "unavailable"
    cached["degraded"] = has_levels
    cached["decision_authoritative"] = False
    cached["message"] = f"GEX option sampling skipped: {message}. " + (
        "Showing the latest cached snapshot as display-only."
        if has_levels
        else "No frame was created and no option subscriptions were opened."
    )
    cached["error"] = {
        "code": "GEX_UNDERLYING_QUOTE_UNAVAILABLE",
        "category": "gex",
        "retryable": True,
        "reason": reason,
        "diagnostics": dict(diagnostics),
        "message": message,
    }
    cached["latest_attempt"] = {
        "source": GEX_REQUEST_SNAPSHOT_SOURCE,
        "capture_mode": "request",
        "captured_at": None,
        "status": "unavailable",
        "message": message,
        "diagnostics": diagnostics,
    }
    return cached


def _cached_gex_refresh_blocked(
    *,
    instrument: dict[str, Any],
    provider_session_state: GexProviderSessionState,
    max_levels: int,
    stale_minutes: int,
    history_hours: int,
    store: Any | None = None,
) -> dict[str, Any]:
    instrument = require_provider_identity(instrument)
    provider_symbol = exact_provider_symbol(instrument, instrument_provider(instrument))
    cached = cached_gex_context(
        instrument=instrument,
        max_levels=max_levels,
        stale_minutes=stale_minutes,
        history_hours=history_hours,
        store=store,
    )
    cached["ok"] = bool(cached.get("levels"))
    cached["status"] = "refresh_blocked"
    cached["decision_authoritative"] = False
    cached["session_state"] = provider_session_state
    if provider_session_state == "closed":
        reason = "provider trading session is closed"
        error_code = "GEX_REFRESH_SESSION_CLOSED"
    elif provider_session_state == "unknown":
        reason = "provider trading-session metadata is unknown"
        error_code = "GEX_REFRESH_SESSION_UNKNOWN"
    else:
        raise ValueError("GEX_REFRESH_BLOCK_REQUIRES_CLOSED_OR_UNKNOWN_SESSION")
    message = (
        f"GEX refresh skipped for {provider_symbol}: {reason}; "
        "showing latest cached snapshot without opening option subscriptions."
    )
    cached["message"] = message
    cached["error"] = {
        "code": error_code,
        "category": "gex",
        "retryable": True,
        "session_state": provider_session_state,
        "message": message,
    }
    return cached


def _finalize_gex_request_payload(
    provider_symbol: str,
    payload: dict[str, Any],
    *,
    instrument: dict[str, Any],
    store: Any | None,
) -> _GexRequestCompletion:
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    payload["instrument_id"] = instrument_id
    payload["route_fingerprint"] = route_key
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    strikes = raw.get("strikes") if isinstance(raw.get("strikes"), list) else []
    contract_rows = raw.get("contracts") if isinstance(raw.get("contracts"), list) else []
    learned_dividend_cache = _maybe_update_dividend_yield_cache(
        provider_symbol,
        contract_rows,
        instrument_id=qualified_instrument_id(instrument),
        route_fingerprint=route_key,
        store=store,
    )
    if learned_dividend_cache.get("cached"):
        payload["learned_dividend_yield_cache"] = learned_dividend_cache
    if payload.get("frame_complete"):
        captured_at = parse_gex_timestamp(payload.get("captured_at"))
        if captured_at is None:
            raise ValueError("Authoritative GEX payload requires captured_at")
        payload["last_sync_at"] = captured_at.isoformat()
        persistence_started = time.monotonic()
        persisted = persist_gex_snapshot(
            provider_symbol,
            meta,
            strikes,
            payload,
            contract_rows,
            instrument_id=qualified_instrument_id(instrument),
            route_fingerprint=route_fingerprint(instrument),
            store=store,
            captured_at=captured_at,
            capture_mode="request",
        )
        observe_metric(
            "gex_persistence_seconds",
            max(time.monotonic() - persistence_started, 0.0),
            route_fingerprint=route_key,
        )
        if store is not None and not persisted:
            raise RuntimeError("GEX snapshot persistence did not complete.")
        _GEX_RUNTIME.record_sync(
            instrument_id,
            route_key,
            captured_at,
            provider_symbol=provider_symbol,
        )
    else:
        last_sync_at = _GEX_RUNTIME.sync_at(instrument_id, route_key)
        payload["last_sync_at"] = last_sync_at.isoformat() if last_sync_at else ""
    universe_expires_at = parse_gex_timestamp(meta.get("option_universe_expires_at"))
    if universe_expires_at is None:
        raise ValueError("GEX request payload is missing exact option universe expiry")
    _require_gex_request_delivery_expiry(universe_expires_at)
    if payload.get("frame_complete"):
        _GEX_RUNTIME.clear_request_errors(instrument_id, route_key)
        _GEX_RUNTIME.clear_failure(
            instrument_id,
            route_key,
            lane="request",
        )
    payload.pop("raw", None)
    return _GexRequestCompletion(payload, universe_expires_at)


def _require_gex_request_delivery_expiry(universe_expires_at: datetime) -> None:
    if universe_expires_at.astimezone(UTC) <= datetime.now(tz=UTC):
        raise OptionUniverseUnavailableError(
            "the exact provider option universe expired before request delivery",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={
                "option_universe_expires_at": universe_expires_at.astimezone(UTC).isoformat(),
            },
        )


def _project_gex_request_history(
    completion: _GexRequestCompletion,
    *,
    history_hours: int,
    max_levels: int,
    store: Any | None,
) -> dict[str, Any]:
    payload = deepcopy(completion.payload)
    history_started = time.monotonic()
    payload["history"] = get_gex_history_series(
        payload["provider_symbol"],
        instrument_id=payload["instrument_id"],
        route_fingerprint=payload["route_fingerprint"],
        hours=history_hours,
        max_levels=max_levels,
        store=store,
    )
    observe_metric(
        "gex_history_projection_seconds",
        max(time.monotonic() - history_started, 0.0),
        route_fingerprint=payload["route_fingerprint"],
    )
    _require_gex_request_delivery_expiry(completion.option_universe_expires_at)
    return payload


def _collect_live_gex_context_owned(
    provider_symbol: str,
    *,
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    stale_minutes: int = GEX_CONTEXT_STALE_MINUTES,
    request: GexRequestConfig,
    dividend_cache: dict[str, Any],
    underlying_quote: dict[str, Any] | None = None,
    instrument: dict[str, Any],
    provider_runtime: GexProviderRuntime,
) -> dict[str, Any]:
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    if request.instrument_id != instrument_id or request.route_fingerprint != route_key:
        raise ValueError("GEX request route no longer matches the qualified instrument")
    _GEX_RUNTIME.mark_request_started(
        instrument_id,
        route_key,
        f"{provider_symbol} options",
        provider_symbol=provider_symbol,
    )
    started = time.monotonic()
    try:
        spot_observation = require_gex_acquisition_spot_observation(
            underlying_quote,
            observed_at=datetime.now(tz=UTC),
        )
        spot = spot_observation["price"]
        cached_dividend_yield = (
            finite_number_or_none(dividend_cache.get("value"))
            if dividend_cache.get("cached")
            else None
        )
        effective_dividend_yield = require_gex_valuation_rates(
            {
                "risk_free_rate": request.risk_free_rate,
                "dividend_yield": (
                    cached_dividend_yield
                    if cached_dividend_yield is not None
                    else request.dividend_yield
                ),
            }
        )["dividend_yield"]
        acquisition = provider_runtime.acquire(
            instrument=instrument,
            request=request,
            spot=spot,
            dividend_yield=effective_dividend_yield,
        )
        calculation_started = time.monotonic()
        contract_rows = list(acquisition.contract_rows)
        selected_strike_count = acquisition.chain_meta.get("qualified_strike_count")
        if type(selected_strike_count) is not int or selected_strike_count <= 0:
            raise RuntimeError(
                "Provider GEX acquisition is missing the exact selected strike count"
            )
        captured_at = acquisition.captured_at
        authoritative_contract_rows = _qualified_gex_contract_rows(
            contract_rows,
            authority_at=captured_at,
        )
        analysis_contract_rows = _qualified_gex_contract_rows(
            contract_rows,
            authority_at=captured_at,
            expected_pair_count=acquisition.contracts_requested // 2,
        )
        strikes = _aggregate_by_strike(analysis_contract_rows, spot)
        diagnostics = _gex_diagnostics(
            contract_rows,
            strikes,
            contracts_requested=acquisition.contracts_requested,
            strikes_requested=request.strike_count,
            generic_ticks=GEX_GENERIC_TICKS,
            analysis_contract_rows=analysis_contract_rows,
            authoritative_contract_rows=authoritative_contract_rows,
        )
        if acquisition.collection_timed_out:
            diagnostics = {
                **diagnostics,
                "diag_collection_timeout": True,
                "diag_collection_timeout_phase": acquisition.collection_timeout_phase,
            }
        meta = {
            "provider_symbol": provider_symbol,
            "spot": spot,
            "spot_observation": spot_observation,
            **acquisition.underlying_audit,
            "provider_request_session": request.provider_metadata.request_session,
            "requested_market_data_entitlement": (request.provider_metadata.requested_entitlement),
            "market_data_entitlement": acquisition.market_data_entitlement,
            "generic_ticks": GEX_GENERIC_TICKS,
            "observation_wait_seconds": (request.provider_metadata.observation_wait_seconds),
            "strike_count": selected_strike_count,
            "provider_selected_strike_count": selected_strike_count,
            "requested_strike_limit": request.strike_count,
            "max_expirations": request.max_expirations,
            "max_contracts": request.max_contracts,
            "request_batch_size": request.provider_metadata.batch_size,
            "request_batch_pause_seconds": (request.provider_metadata.batch_pause_seconds),
            "refresh_mode": request.refresh_mode,
            "scheduler_lane": request.scheduler_lane,
            "futures_options": request.futures_options,
            "risk_free_rate": request.risk_free_rate,
            "dividend_yield": effective_dividend_yield,
            "dividend_yield_source": "broker_cache" if dividend_cache.get("cached") else "config",
            "dividend_yield_cache": dividend_cache,
            "time_to_expiry_model": "provider_contract_expiry_at",
            "option_universe_expires_at": (acquisition.universe_expires_at.isoformat()),
            **acquisition.chain_meta,
        }
        payload = _payload_from_rows(
            provider_symbol,
            captured_at=captured_at,
            meta=meta,
            strikes=strikes,
            diagnostics=diagnostics,
            max_levels=max_levels,
            stale_minutes=stale_minutes,
            source=GEX_REQUEST_SNAPSHOT_SOURCE,
            capture_mode="request",
            contract_rows=analysis_contract_rows,
            comparison_contract_rows=acquisition.selected_contract_universe,
        )
        _GEX_RUNTIME.record_diagnostics(
            instrument_id,
            route_key,
            payload["diagnostics"],
        )
        last_sync_at = _GEX_RUNTIME.sync_at(instrument_id, route_key)
        payload["last_sync_at"] = last_sync_at.isoformat() if last_sync_at else ""
        payload["request_seconds"] = round(time.monotonic() - started, 3)
        observe_metric(
            "gex_calculation_seconds",
            max(time.monotonic() - calculation_started, 0.0),
            route_fingerprint=route_key,
        )
        return payload
    except OptionUniverseUnavailableError:
        raise
    except Exception as exc:
        error = exc
        log_structured_error(
            LOGGER,
            event="gex_request_failed",
            provider=provider_runtime.provider_key,
            symbol=provider_symbol,
            interval="options",
            range_="snapshot",
            op="collect_live_gex_context",
            error=error,
        )
        _GEX_RUNTIME.record_error(
            instrument_id,
            route_key,
            exception_message(error),
            provider_symbol=provider_symbol,
            source=request.refresh_mode,
        )
        raise
    finally:
        _GEX_RUNTIME.mark_request_finished(instrument_id, route_key)
