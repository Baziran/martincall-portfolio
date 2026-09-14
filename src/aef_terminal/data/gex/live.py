from __future__ import annotations

import asyncio
import logging
import threading
import time
from copy import deepcopy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from aef_terminal.data.gex.config import GexRequestConfig
from aef_terminal.data.gex.constants import (
    GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES,
    GEX_GENERIC_TICKS,
    GEX_LIVE_FRAME_SECONDS,
    GEX_LIVE_SNAPSHOT_SOURCE,
    GEX_SCHEDULER_INTERVAL_MINUTES,
)
from aef_terminal.data.gex.contracts import (
    GexUnderlyingQuoteUnavailableError,
    gex_underlying_quote_observation_from_cache,
    gex_capture_revision_at,
    require_gex_valuation_rates,
)
from aef_terminal.data.gex.dividend import (
    GEX_DIVIDEND_ESTIMATE_UNAVAILABLE,
    _dividend_yield_for_gex_request,
    _maybe_update_dividend_yield_cache,
)
from aef_terminal.data.provider_contract import OptionUniverseUnavailableError
from aef_terminal.data.gex.history import persist_gex_snapshot
from aef_terminal.data.gex.live_analysis import (
    _LIVE_GEX_OPTION_VOLUME_EVENT_SECONDS,
    _LIVE_GEX_OPTION_VOLUME_HISTORY_SECONDS,
    _LIVE_GEX_TREND_WINDOW_SECONDS,
    _attach_live_motion_to_rows,
    _compare_live_option_volume_frames,
    _live_net_motion,
    _live_option_volume_activity_is_material,
    _live_option_volume_baseline,
    _live_option_volume_event_state,
    _live_option_volume_frame,
    _live_option_volume_interaction,
    _live_option_volume_interval_rates_by_strike,
    _live_roll_motion,
    _live_side_motion,
    _live_strike_step_from_strikes,
    _live_trend_baseline,
    _live_trend_frame,
    _live_trend_price_key,
)
from aef_terminal.data.gex.payload import (
    _aggregate_by_strike,
    _gex_request_meta,
    _levels_from_strikes,
    _option_volume_context_from_strike,
    _payload_from_rows,
)
from aef_terminal.data.gex.quality import (
    _gex_diagnostics,
    _qualified_gex_contract_rows,
    require_gex_acquisition_spot_observation,
)
from aef_terminal.data.gex.provider_runtime import (
    GexProviderRuntime,
    LiveGexSubscription,
)
from aef_terminal.data.gex.session import _GEX_RUNTIME
from aef_terminal.runtime.async_tasks import (
    await_cancellation_deferred_task,
    run_physical_thread_call,
)
from aef_terminal.runtime.telemetry import exception_message
from aef_terminal.data.gex.utils import finite_number_or_none, _median_positive, parse_gex_timestamp
from aef_terminal.data.instrument_identity import (
    instrument_provider,
    provider_symbol as exact_provider_symbol,
    require_provider_identity,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.runtime.metrics import increment_metric, observe_metric


LIVE_GEX_FRAME_CACHE_SECONDS = GEX_LIVE_FRAME_SECONDS
LIVE_GEX_PERSIST_INTERVAL_SECONDS = GEX_SCHEDULER_INTERVAL_MINUTES * 60
LOGGER = logging.getLogger(__name__)


@dataclass
class _LiveGexSession:
    provider_symbol: str
    instrument_id: str
    route_fingerprint: str
    key: tuple[Any, ...]
    request: GexRequestConfig
    provider_runtime: GexProviderRuntime
    provider_subscription: LiveGexSubscription
    subscribed_strikes: tuple[float, ...]
    contracts_requested: int
    chain_meta: dict[str, Any]
    underlying_audit: dict[str, Any]
    spot: float
    generation: str = field(default_factory=lambda: uuid4().hex)
    recovery_count: int = 0
    recovery_reason: str = ""
    dividend_cache: dict[str, Any] = field(default_factory=dict)
    effective_dividend_yield: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    last_frame_at: datetime | None = None
    frame_seq: int = 0
    trend_frames: list[dict[str, Any]] = field(default_factory=list)
    option_volume_frames: list[dict[str, Any]] = field(default_factory=list)
    last_payload: dict[str, Any] | None = None
    last_publishable_payload: dict[str, Any] | None = None
    last_payload_built_at: float = 0.0
    dividend_cache_bucket: str = ""
    dividend_cache_error: str = ""
    persisted_bucket: str = ""
    persisting_bucket: str = ""
    persistence_error: str = ""


@dataclass(frozen=True)
class _LiveGexSessionToken:
    identity_key: tuple[str, str]
    generation: str


@dataclass(frozen=True)
class _LiveGexFinalizationPlan:
    session_token: _LiveGexSessionToken
    provider_symbol: str
    instrument_id: str
    route_fingerprint: str
    captured_at: datetime
    bucket_at: datetime
    bucket_key: str
    refresh_dividend_cache: bool
    persist_snapshot: bool
    dividend_cache_error: str
    persistence_error: str


@dataclass(frozen=True)
class _LiveGexOwnedFrame:
    payload: dict[str, Any]
    session_token: _LiveGexSessionToken


@dataclass(frozen=True)
class _LiveGexOwnedInputs:
    request: GexRequestConfig
    dividend_cache: dict[str, Any]


_LIVE_GEX_SESSIONS: dict[tuple[str, str], _LiveGexSession] = {}
_LIVE_GEX_STATUS_LOCK = threading.Lock()
_LIVE_GEX_STATUS_SNAPSHOT: dict[str, Any] = {
    "enabled": False,
    "active": False,
    "sessions": [],
    "subscriptions": 0,
    "subscription_budget": 0,
    "subscription_over_budget": 0,
}
_LIVE_GEX_FRAME_SNAPSHOT_LOCK = threading.Lock()
_LIVE_GEX_FRAME_SNAPSHOTS: dict[tuple[str, str], dict[str, Any]] = {}


def live_gex_frame_snapshot(
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any] | None:
    """Return an isolated copy of the latest complete raw live frame for one exact route."""

    identity_key = (instrument_id, route_fingerprint)
    with _LIVE_GEX_FRAME_SNAPSHOT_LOCK:
        payload = _LIVE_GEX_FRAME_SNAPSHOTS.get(identity_key)
        return deepcopy(payload) if payload is not None else None


def _publish_live_gex_frame_snapshot(
    identity_key: tuple[str, str],
    payload: Mapping[str, Any],
) -> None:
    raw = payload.get("raw")
    if (
        payload.get("ok") is not True
        or payload.get("frame_complete") is not True
        or not isinstance(raw, Mapping)
        or not isinstance(raw.get("contracts"), list)
    ):
        return
    with _LIVE_GEX_FRAME_SNAPSHOT_LOCK:
        _LIVE_GEX_FRAME_SNAPSHOTS[identity_key] = deepcopy(dict(payload))


def _clear_live_gex_frame_snapshot(identity_key: tuple[str, str] | None) -> None:
    with _LIVE_GEX_FRAME_SNAPSHOT_LOCK:
        if identity_key is None:
            _LIVE_GEX_FRAME_SNAPSHOTS.clear()
        else:
            _LIVE_GEX_FRAME_SNAPSHOTS.pop(identity_key, None)


def _live_error_payload(
    provider_symbol: str,
    message: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    code: str = "GEX_LIVE_ERROR",
    status: str = "error",
    backoff: Mapping[str, Any] | None = None,
    reason: str = "",
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": False,
        "enabled": True,
        "provider_symbol": provider_symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "source": GEX_LIVE_SNAPSHOT_SOURCE,
        "capture_mode": "live",
        "status": status,
        "degraded": status == "degraded",
        "frame_complete": False,
        "decision_authoritative": False,
        "message": message,
        "error": {
            "code": code,
            "category": "gex",
            "retryable": True,
            "message": message,
        },
        "levels": [],
        "history": [],
        "live": {"enabled": True, "active": False},
    }
    if reason:
        exact_diagnostics = dict(diagnostics or {})
        exact_diagnostics["option_universe_reason"] = reason
        payload["error"]["reason"] = reason
        payload["error"]["diagnostics"] = dict(exact_diagnostics)
        payload["diagnostics"] = exact_diagnostics
    if backoff:
        payload["backoff"] = dict(backoff)
        payload["live"]["backoff"] = dict(backoff)
    return payload


def _live_session_key(request: GexRequestConfig, instrument: dict[str, Any]) -> tuple[Any, ...]:
    return (
        qualified_instrument_id(instrument),
        route_fingerprint(instrument),
        request.provider_metadata.provider_key,
        request.provider_metadata.generation,
        request.strike_count,
        request.max_expirations,
        request.max_contracts,
        request.expiry_mode,
    )


def _live_request(
    *,
    instrument: dict[str, Any],
    provider_runtime: GexProviderRuntime,
    store: Any | None = None,
) -> GexRequestConfig:
    return provider_runtime.request_config(
        instrument=instrument,
        store=store,
        mode="live",
    )


async def async_live_gex_context(
    *,
    enabled: bool,
    store: Any | None = None,
    underlying_quote: dict[str, Any] | None = None,
    instrument: dict[str, Any],
    provider_runtime: GexProviderRuntime,
) -> dict[str, Any]:
    qualified_instrument = require_provider_identity(instrument)
    provider_key = instrument_provider(qualified_instrument)
    if provider_runtime.provider_key != provider_key:
        raise ValueError("GEX_PROVIDER_RUNTIME_MISMATCH")
    instrument_id = qualified_instrument_id(qualified_instrument)
    route_key = route_fingerprint(qualified_instrument)
    identity_key = (instrument_id, route_key)
    provider_symbol = exact_provider_symbol(qualified_instrument, provider_key)
    if not enabled:
        await async_stop_live_gex_context(
            qualified_instrument,
            provider_runtime=provider_runtime,
        )
        return {
            "ok": False,
            "enabled": False,
            "provider_symbol": provider_symbol,
            "instrument_id": instrument_id,
            "route_fingerprint": route_key,
            "source": GEX_LIVE_SNAPSHOT_SOURCE,
            "capture_mode": "live",
            "status": "disabled",
            "message": "GEX live mode disabled; option subscriptions are stopped.",
            "levels": [],
            "history": [],
            "live": {"enabled": False, "active": False},
        }
    backoff = _GEX_RUNTIME.failure_backoff(
        instrument_id,
        route_key,
        lane="live",
    )
    if backoff.get("active"):
        option_unavailable = backoff.get("kind") == "option_universe_unavailable"
        if option_unavailable:
            await async_stop_live_gex_context(
                qualified_instrument,
                provider_runtime=provider_runtime,
            )
        return _live_error_payload(
            provider_symbol,
            (
                "GEX live option universe is temporarily unavailable; "
                if option_unavailable
                else f"GEX live mode is temporarily degraded after {backoff.get('source') or 'provider'} failure; "
            )
            + (f"retry in {float(backoff.get('retry_in_seconds') or 0.0):.1f}s."),
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            code=("GEX_OPTION_UNIVERSE_UNAVAILABLE" if option_unavailable else "GEX_LIVE_BACKOFF"),
            status="unavailable" if option_unavailable else "degraded",
            backoff=backoff,
            reason=(str(backoff.get("reason") or "") if option_unavailable else ""),
            diagnostics=(
                backoff.get("diagnostics")
                if option_unavailable and isinstance(backoff.get("diagnostics"), Mapping)
                else None
            ),
        )
    try:
        from aef_terminal.data.gex.context import gex_provider_session_state

        provider_session_state = await run_physical_thread_call(
            gex_provider_session_state,
            store=store,
            instrument=qualified_instrument,
        )
        if provider_session_state != "open":
            if _live_gex_route_requires_stop(identity_key):
                await async_stop_live_gex_context(
                    qualified_instrument,
                    provider_runtime=provider_runtime,
                )
            if provider_session_state == "closed":
                message = "GEX live mode is paused while the provider trading session is closed."
                error_code = "GEX_LIVE_SESSION_CLOSED"
                status = "session_closed"
            else:
                message = (
                    "GEX live mode is blocked because provider trading-session metadata is unknown."
                )
                error_code = "GEX_LIVE_SESSION_UNKNOWN"
                status = "session_unknown"
            payload = _live_error_payload(
                provider_symbol,
                message,
                instrument_id=instrument_id,
                route_fingerprint=route_key,
                code=error_code,
                status=status,
            )
            payload["session_state"] = provider_session_state
            payload["error"]["session_state"] = provider_session_state
            return payload
        spot_observation = require_gex_acquisition_spot_observation(
            underlying_quote,
            observed_at=datetime.now(tz=UTC),
        )
        owned_inputs = await provider_runtime.run_coroutine(
            "options",
            f"{provider_symbol} live gex inputs",
            lambda: _live_gex_inputs_owned(identity_key),
            timeout=2.0,
        )
        if owned_inputs is not None:
            request = owned_inputs.request
            dividend_cache = owned_inputs.dividend_cache
        else:
            request = await run_physical_thread_call(
                _live_request,
                instrument=qualified_instrument,
                provider_runtime=provider_runtime,
                store=store,
            )
            dividend_cache = await run_physical_thread_call(
                _dividend_yield_for_gex_request,
                request,
                configured_override=(provider_runtime.dividend_yield_override_active()),
                store=store,
            )
        owned_frame = await provider_runtime.run_coroutine(
            "options",
            f"{provider_symbol} live gex frame",
            lambda: _live_gex_context_owned(
                provider_symbol,
                instrument=qualified_instrument,
                request=request,
                dividend_cache=dividend_cache,
                underlying_quote=spot_observation,
                provider_runtime=provider_runtime,
            ),
            timeout=12.0,
        )
        payload = owned_frame.payload
        finalization_task = asyncio.create_task(
            _finalize_live_gex_frame(
                owned_frame.session_token,
                payload,
                store=store,
                provider_runtime=provider_runtime,
            )
        )
        await await_cancellation_deferred_task(
            finalization_task,
            task_cancelled_error="GEX_LIVE_FINALIZATION_TASK_CANCELLED",
        )
        _GEX_RUNTIME.clear_error(instrument_id, route_key, source="live")
        _GEX_RUNTIME.clear_failure(
            instrument_id,
            route_key,
            lane="live",
        )
        return payload
    except GexUnderlyingQuoteUnavailableError as exc:
        message = exception_message(exc)
        reason = str(
            getattr(exc, "reason", "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE")
            or "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
        )
        raw_diagnostics = getattr(exc, "diagnostics", {})
        diagnostics = dict(raw_diagnostics) if isinstance(raw_diagnostics, dict) else {}
        diagnostics["underlying_quote_reason"] = reason
        increment_metric(
            "gex_underlying_quote_preflight_skipped_total",
            route_fingerprint=route_key,
            source="live",
        )
        payload = _live_error_payload(
            provider_symbol,
            f"GEX live frame is waiting for a fresh canonical underlying quote: {message}",
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            code="GEX_UNDERLYING_QUOTE_UNAVAILABLE",
            status="unavailable",
        )
        payload["error"]["reason"] = reason
        payload["error"]["diagnostics"] = dict(diagnostics)
        payload["diagnostics"] = diagnostics
        return payload
    except OptionUniverseUnavailableError as exc:
        message = exception_message(exc)
        unavailable_reason = exc.reason
        unavailable_diagnostics = dict(exc.diagnostics)
        backoff = _GEX_RUNTIME.record_failure(
            instrument_id,
            route_key,
            message,
            provider_symbol=provider_symbol,
            source="live",
            lane="live",
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
        return _live_error_payload(
            provider_symbol,
            f"GEX live option universe is unavailable: {message}",
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            code="GEX_OPTION_UNIVERSE_UNAVAILABLE",
            status="unavailable",
            backoff=backoff,
            reason=unavailable_reason,
            diagnostics=unavailable_diagnostics,
        )
    except Exception as exc:
        message = exception_message(exc)
        backoff = _GEX_RUNTIME.record_failure(
            instrument_id,
            route_key,
            message,
            provider_symbol=provider_symbol,
            source="live",
            lane="live",
        )
        increment_metric("gex_route_backoff_total", route_fingerprint=route_key, source="live")
        return _live_error_payload(
            provider_symbol,
            f"GEX live frame failed: {message}",
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            status="degraded",
            backoff=backoff,
        )


async def async_stop_live_gex_context(
    instrument: dict[str, Any] | None = None,
    *,
    provider_runtime: GexProviderRuntime,
) -> dict[str, Any]:
    qualified = require_provider_identity(instrument) if instrument is not None else None
    if qualified is not None and instrument_provider(qualified) != provider_runtime.provider_key:
        raise ValueError("GEX_PROVIDER_RUNTIME_MISMATCH")
    identity_key = (
        (qualified_instrument_id(qualified), route_fingerprint(qualified))
        if qualified is not None
        else None
    )
    return await provider_runtime.run_coroutine(
        "options",
        f"{identity_key or 'all'} live gex stop",
        lambda: _stop_live_gex_context_owned(identity_key),
        timeout=3.0,
    )


def live_gex_status() -> dict[str, Any]:
    with _LIVE_GEX_STATUS_LOCK:
        return deepcopy(_LIVE_GEX_STATUS_SNAPSHOT)


def _live_gex_route_requires_stop(identity_key: tuple[str, str]) -> bool:
    if _GEX_RUNTIME.active_capture_mode(*identity_key) == "live":
        return True
    with _LIVE_GEX_STATUS_LOCK:
        sessions = _LIVE_GEX_STATUS_SNAPSHOT.get("sessions")
        if not isinstance(sessions, list):
            return False
        return any(
            isinstance(session, dict)
            and session.get("instrument_id") == identity_key[0]
            and session.get("route_fingerprint") == identity_key[1]
            for session in sessions
        )


async def _live_gex_inputs_owned(
    identity_key: tuple[str, str],
) -> _LiveGexOwnedInputs | None:
    session = _LIVE_GEX_SESSIONS.get(identity_key)
    if session is None:
        return None
    return _LiveGexOwnedInputs(
        request=session.request,
        dividend_cache=deepcopy(session.dividend_cache),
    )


def _publish_live_gex_status_owned() -> dict[str, Any]:
    global _LIVE_GEX_STATUS_SNAPSHOT
    sessions = list(_LIVE_GEX_SESSIONS.values())
    if not sessions:
        status = {
            "enabled": False,
            "active": False,
            "sessions": [],
            "subscriptions": 0,
            "subscription_budget": 0,
            "subscription_over_budget": 0,
        }
    else:
        session_rows = []
        for session in sessions:
            try:
                health = session.provider_runtime.live_health(session.provider_subscription)
            except Exception:
                health = None
            connected = bool(health and health.connected)
            subscription_budget = session.request.max_contracts
            subscriptions = health.subscription_count if health is not None else 0
            session_rows.append(
                {
                    "provider_symbol": session.provider_symbol,
                    "instrument_id": session.instrument_id,
                    "route_fingerprint": session.route_fingerprint,
                    "active": connected,
                    "generation": session.generation,
                    "recovery_count": session.recovery_count,
                    "recovery_reason": session.recovery_reason,
                    "started_at": session.started_at.isoformat(),
                    "last_frame_at": session.last_frame_at.isoformat()
                    if session.last_frame_at
                    else "",
                    "frame_seq": session.frame_seq,
                    "subscriptions": subscriptions,
                    "subscription_budget": subscription_budget,
                    "subscription_over_budget": max(subscriptions - subscription_budget, 0),
                    "provider_request_session": (session.request.provider_metadata.request_session),
                    "dividend_cache_bucket": session.dividend_cache_bucket,
                    "dividend_cache_error": session.dividend_cache_error,
                    "persisted_bucket": session.persisted_bucket,
                    "persisting_bucket": session.persisting_bucket,
                    "persistence_error": session.persistence_error,
                    "last_diagnostics": (
                        dict(session.last_payload.get("diagnostics") or {})
                        if isinstance(session.last_payload, dict)
                        and isinstance(session.last_payload.get("diagnostics"), dict)
                        else {}
                    ),
                }
            )
        status = {
            "enabled": True,
            "active": any(row["active"] for row in session_rows),
            "sessions": session_rows,
            "subscriptions": sum(row["subscriptions"] for row in session_rows),
            "subscription_budget": sum(row["subscription_budget"] for row in session_rows),
            "subscription_over_budget": sum(
                row["subscription_over_budget"] for row in session_rows
            ),
        }
    with _LIVE_GEX_STATUS_LOCK:
        _LIVE_GEX_STATUS_SNAPSHOT = deepcopy(status)
    return status


async def _finalize_live_gex_frame(
    session_token: _LiveGexSessionToken,
    payload: dict[str, Any],
    *,
    store: Any | None,
    provider_runtime: GexProviderRuntime,
) -> None:
    live = payload.get("live") if isinstance(payload.get("live"), dict) else {}
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    raw_meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    plan = await provider_runtime.run_coroutine(
        "options",
        f"{session_token.identity_key} live gex finalize prepare",
        lambda: _prepare_live_gex_finalization_owned(session_token, payload),
    )
    if plan is not None:
        raw_contracts = raw.get("contracts") if isinstance(raw.get("contracts"), list) else []
        learned_dividend_cache: dict[str, Any] = {}
        dividend_cache_error = ""
        retry_dividend_bucket = False
        dividend_env_override = False
        if plan.refresh_dividend_cache:
            try:
                learned_dividend_cache = await run_physical_thread_call(
                    _maybe_update_dividend_yield_cache,
                    plan.provider_symbol,
                    raw_contracts,
                    instrument_id=plan.instrument_id,
                    route_fingerprint=plan.route_fingerprint,
                    now=plan.captured_at,
                    store=store,
                )
            except Exception as exc:
                dividend_cache_error = str(exc) or exc.__class__.__name__
            else:
                learned_code = str(learned_dividend_cache.get("code") or "")
                learned_reason = str(learned_dividend_cache.get("reason") or "")
                if learned_dividend_cache.get("cached"):
                    payload["learned_dividend_yield_cache"] = dict(learned_dividend_cache)
                    dividend_env_override = provider_runtime.dividend_yield_override_active()
                elif learned_code == GEX_DIVIDEND_ESTIMATE_UNAVAILABLE:
                    retry_dividend_bucket = True
                else:
                    dividend_cache_error = (
                        learned_reason or "Dividend-cache refresh did not complete."
                    )

        persistence_ok = False
        persistence_error = ""
        if plan.persist_snapshot:
            try:
                persisted = await run_physical_thread_call(
                    persist_gex_snapshot,
                    plan.provider_symbol,
                    {
                        **raw_meta,
                        "refresh_mode": "live",
                    },
                    raw.get("strikes") if isinstance(raw.get("strikes"), list) else [],
                    payload,
                    raw_contracts,
                    instrument_id=plan.instrument_id,
                    route_fingerprint=plan.route_fingerprint,
                    store=store,
                    captured_at=plan.captured_at,
                    capture_mode="live",
                    live_bucket_at=plan.bucket_at,
                )
                if not persisted:
                    raise RuntimeError("GEX live snapshot persistence did not complete.")
            except Exception as exc:
                persistence_error = str(exc) or exc.__class__.__name__
            else:
                persistence_ok = True

        committed = await provider_runtime.run_coroutine(
            "options",
            f"{plan.provider_symbol} live gex finalize commit",
            lambda: _commit_live_gex_finalization_owned(
                plan,
                learned_dividend_cache=learned_dividend_cache,
                dividend_cache_error=dividend_cache_error,
                retry_dividend_bucket=retry_dividend_bucket,
                dividend_env_override=dividend_env_override,
                persistence_ok=persistence_ok,
                persistence_error=persistence_error,
            ),
        )
        if committed is not None:
            if dividend_cache_error and dividend_cache_error != plan.dividend_cache_error:
                LOGGER.warning(
                    "GEX live dividend-cache refresh failed for %s: %s",
                    plan.provider_symbol,
                    dividend_cache_error,
                )
            if persistence_error and persistence_error != plan.persistence_error:
                LOGGER.warning(
                    "GEX live snapshot persistence failed for %s: %s",
                    plan.provider_symbol,
                    persistence_error,
                )
            if committed["persisted_bucket"]:
                live["persisted_bucket"] = committed["persisted_bucket"]
                live["persist_interval_seconds"] = LIVE_GEX_PERSIST_INTERVAL_SECONDS
            live["dividend_cache_ok"] = not bool(committed["dividend_cache_error"])
            live["dividend_cache_error"] = committed["dividend_cache_error"]
            live["persistence_pending"] = committed["persisting_bucket"] == plan.bucket_key
            live["persistence_ok"] = (
                not bool(committed["persistence_error"]) and not live["persistence_pending"]
            )
            live["persistence_error"] = committed["persistence_error"]
        else:
            live["persistence_pending"] = False
            live["persistence_ok"] = False
            live["persistence_error"] = "Live GEX session changed before finalization commit."
            payload["decision_authoritative"] = False
            live["publishable"] = False
            payload["message"] = (
                "Live GEX session changed before delivery; the captured frame is display-only."
            )
    elif payload.get("decision_authoritative"):
        payload["decision_authoritative"] = False
        live["publishable"] = False
        payload["message"] = (
            "Live GEX frame did not pass finalization admission; "
            "the captured frame is display-only."
        )
    payload.pop("raw", None)
    payload["request_meta"] = _gex_request_meta(
        payload.get("request_meta")
        if isinstance(payload.get("request_meta"), Mapping)
        else raw_meta
    )


async def _prepare_live_gex_finalization_owned(
    session_token: _LiveGexSessionToken,
    payload: Mapping[str, Any],
) -> _LiveGexFinalizationPlan | None:
    session = _LIVE_GEX_SESSIONS.get(session_token.identity_key)
    if session is None or session.generation != session_token.generation:
        return None
    if session_token.identity_key != (
        session.instrument_id,
        session.route_fingerprint,
    ):
        return None
    live = payload.get("live") if isinstance(payload.get("live"), Mapping) else {}
    captured_at = parse_gex_timestamp(payload.get("captured_at"))
    if captured_at is not None:
        health = session.provider_runtime.live_health(
            session.provider_subscription,
            now=captured_at,
        )
        if health.expired_contract_count or health.unknown_expiry_contract_count:
            return None
    if not live.get("publishable") or captured_at is None or not payload.get("levels"):
        return None
    bucket_at = gex_capture_revision_at(captured_at, capture_mode="live")
    bucket_key = bucket_at.isoformat()
    refresh_dividend_cache = (
        not session.request.futures_options and session.dividend_cache_bucket != bucket_key
    )
    if refresh_dividend_cache:
        session.dividend_cache_bucket = bucket_key
    persist_snapshot = (
        session.persisted_bucket != bucket_key and session.persisting_bucket != bucket_key
    )
    if persist_snapshot:
        session.persisting_bucket = bucket_key
    _publish_live_gex_status_owned()
    return _LiveGexFinalizationPlan(
        session_token=session_token,
        provider_symbol=session.provider_symbol,
        instrument_id=session.instrument_id,
        route_fingerprint=session.route_fingerprint,
        captured_at=captured_at,
        bucket_at=bucket_at,
        bucket_key=bucket_key,
        refresh_dividend_cache=refresh_dividend_cache,
        persist_snapshot=persist_snapshot,
        dividend_cache_error=session.dividend_cache_error,
        persistence_error=session.persistence_error,
    )


async def _commit_live_gex_finalization_owned(
    plan: _LiveGexFinalizationPlan,
    *,
    learned_dividend_cache: Mapping[str, Any],
    dividend_cache_error: str,
    retry_dividend_bucket: bool,
    dividend_env_override: bool,
    persistence_ok: bool,
    persistence_error: str,
) -> dict[str, str] | None:
    session = _LIVE_GEX_SESSIONS.get(plan.session_token.identity_key)
    if session is None or session.generation != plan.session_token.generation:
        return None
    if plan.session_token.identity_key != (
        session.instrument_id,
        session.route_fingerprint,
    ):
        return None
    health = session.provider_runtime.live_health(session.provider_subscription)
    if health.unknown_expiry_contract_count:
        await _stop_live_gex_context_owned(plan.session_token.identity_key)
        raise OptionUniverseUnavailableError(
            "Provider live option subscription is missing exact expiry metadata at delivery",
            reason="EXPIRY_TIME_UNKNOWN",
            diagnostics={
                "expiry_time_unknown_contracts_excluded": (health.unknown_expiry_contract_count),
            },
        )
    if health.expired_contract_count:
        await _stop_live_gex_context_owned(plan.session_token.identity_key)
        raise OptionUniverseUnavailableError(
            "Provider live option universe crossed its exact expiry before delivery",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={
                "expired_contracts_excluded": health.expired_contract_count,
            },
        )
    if plan.refresh_dividend_cache:
        if learned_dividend_cache.get("cached"):
            session.dividend_cache_error = ""
            learned_value = finite_number_or_none(learned_dividend_cache.get("value"))
            if learned_value is not None and not dividend_env_override:
                previous_value = session.effective_dividend_yield
                session.dividend_cache = dict(learned_dividend_cache)
                session.effective_dividend_yield = require_gex_valuation_rates(
                    {
                        "risk_free_rate": session.request.risk_free_rate,
                        "dividend_yield": learned_value,
                    }
                )["dividend_yield"]
                if session.effective_dividend_yield != previous_value:
                    session.last_payload = None
                    session.last_payload_built_at = 0.0
        elif retry_dividend_bucket:
            if session.dividend_cache_bucket == plan.bucket_key:
                session.dividend_cache_bucket = ""
            session.dividend_cache_error = ""
        else:
            session.dividend_cache_error = dividend_cache_error
    if plan.persist_snapshot and session.persisting_bucket == plan.bucket_key:
        if persistence_ok:
            session.persisted_bucket = plan.bucket_key
            session.persistence_error = ""
        else:
            session.persistence_error = persistence_error
        session.persisting_bucket = ""
    _publish_live_gex_status_owned()
    return {
        "persisted_bucket": session.persisted_bucket,
        "persisting_bucket": session.persisting_bucket,
        "persistence_error": session.persistence_error,
        "dividend_cache_error": session.dividend_cache_error,
    }


async def _live_gex_context_owned(
    provider_symbol: str,
    *,
    instrument: dict[str, Any],
    request: GexRequestConfig,
    dividend_cache: dict[str, Any],
    underlying_quote: dict[str, Any],
    provider_runtime: GexProviderRuntime,
) -> _LiveGexOwnedFrame:
    identity_key = (
        qualified_instrument_id(instrument),
        route_fingerprint(instrument),
    )
    if request.instrument_id != identity_key[0] or request.route_fingerprint != identity_key[1]:
        raise ValueError("Live GEX request route no longer matches the qualified instrument")
    key = _live_session_key(request, instrument)
    spot_observation = gex_underlying_quote_observation_from_cache(underlying_quote)
    underlying_spot = spot_observation["price"]
    session = await _ensure_live_gex_session_owned(
        provider_symbol,
        request=request,
        dividend_cache=dividend_cache,
        key=key,
        instrument=instrument,
        underlying_spot=underlying_spot,
        provider_runtime=provider_runtime,
    )
    try:
        rebase_reason = _live_session_rebase_reason(session, underlying_spot)
    except OptionUniverseUnavailableError:
        await _stop_live_gex_context_owned(identity_key)
        raise
    if rebase_reason:
        await _stop_live_gex_context_owned(identity_key)
        session = await _ensure_live_gex_session_owned(
            provider_symbol,
            request=request,
            dividend_cache=dividend_cache,
            key=key,
            instrument=instrument,
            underlying_spot=underlying_spot,
            provider_runtime=provider_runtime,
        )
        session.chain_meta["live_rebase_reason"] = rebase_reason
    now = time.monotonic()
    if (
        session.last_payload is not None
        and now - session.last_payload_built_at < LIVE_GEX_FRAME_CACHE_SECONDS
    ):
        payload = deepcopy(session.last_payload)
    else:
        try:
            payload = _live_gex_frame_from_session(
                session,
                max_levels=GEX_CONTEXT_MAX_LEVELS,
                stale_minutes=GEX_CONTEXT_STALE_MINUTES,
                underlying_quote=spot_observation,
            )
        except OptionUniverseUnavailableError:
            await _stop_live_gex_context_owned(identity_key)
            raise
        session.last_payload = deepcopy(payload)
        session.last_payload_built_at = time.monotonic()
    _publish_live_gex_status_owned()
    return _LiveGexOwnedFrame(
        payload=payload,
        session_token=_LiveGexSessionToken(
            identity_key=identity_key,
            generation=session.generation,
        ),
    )


async def _ensure_live_gex_session_owned(
    provider_symbol: str,
    *,
    request: GexRequestConfig,
    key: tuple[Any, ...],
    instrument: dict[str, Any],
    underlying_spot: float,
    provider_runtime: GexProviderRuntime,
    dividend_cache: dict[str, Any] | None = None,
) -> _LiveGexSession:
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    identity_key = (instrument_id, route_key)
    _GEX_RUNTIME.acquire_capture_mode(
        instrument_id,
        route_key,
        "live",
        provider_symbol=provider_symbol,
    )
    session = _LIVE_GEX_SESSIONS.get(identity_key)
    recovery_reason = ""
    previous_publishable_payload: dict[str, Any] | None = None
    recovery_count = 0
    if session is not None and session.key == key and session.provider_runtime is provider_runtime:
        try:
            health = provider_runtime.live_health(session.provider_subscription)
            subscriptions_match_contracts = health.subscription_count == health.contract_count
            within_budget = health.subscription_count <= session.request.max_contracts
            if health.connected and subscriptions_match_contracts and within_budget:
                return session
            recovery_reason = (
                "transport_disconnected"
                if not health.connected
                else "subscription_count_mismatch"
                if not subscriptions_match_contracts
                else "subscription_budget_exceeded"
            )
        except Exception:
            recovery_reason = "health_check_failed"
            LOGGER.warning(
                "GEX live subscription health check failed for %s (%s); "
                "replacing the owned session",
                instrument_id,
                route_key,
                exc_info=True,
            )
    elif session is not None:
        recovery_reason = "subscription_contract_changed"
    if session is not None:
        if session.key == key:
            previous_publishable_payload = deepcopy(session.last_publishable_payload)
            recovery_count = session.recovery_count + 1
        _cancel_live_gex_session_owned(session)
        _LIVE_GEX_SESSIONS.pop(identity_key, None)
    spot = finite_number_or_none(underlying_spot)
    if spot is None or spot <= 0:
        _GEX_RUNTIME.release_capture_mode(instrument_id, route_key, "live")
        raise RuntimeError("Canonical provider quote cache has no underlying spot for GEX.")
    try:
        provider_subscription = await provider_runtime.open_live(
            provider_symbol=provider_symbol,
            instrument=instrument,
            request=request,
            spot=spot,
        )
    except BaseException:
        _GEX_RUNTIME.release_capture_mode(instrument_id, route_key, "live")
        raise
    try:
        dividend_cache = dict(dividend_cache or {})
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
        replacement = _LiveGexSession(
            provider_symbol=provider_symbol,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint(instrument),
            key=key,
            request=request,
            provider_runtime=provider_runtime,
            provider_subscription=provider_subscription,
            subscribed_strikes=tuple(provider_subscription.subscribed_strikes),
            contracts_requested=provider_subscription.contracts_requested,
            chain_meta=dict(provider_subscription.chain_meta),
            underlying_audit=dict(provider_subscription.underlying_audit),
            spot=spot,
            recovery_count=recovery_count,
            recovery_reason=recovery_reason,
            dividend_cache=dict(dividend_cache),
            effective_dividend_yield=effective_dividend_yield,
        )
    except BaseException:
        provider_runtime.close_live(provider_subscription)
        _GEX_RUNTIME.release_capture_mode(instrument_id, route_key, "live")
        raise
    if previous_publishable_payload is not None:
        replacement.last_publishable_payload = previous_publishable_payload
    if recovery_reason:
        replacement.chain_meta["live_recovery_reason"] = recovery_reason
        LOGGER.info(
            "GEX live subscription recovered for %s (%s): generation=%s reason=%s",
            instrument_id,
            route_key,
            replacement.generation,
            recovery_reason,
        )
    _LIVE_GEX_SESSIONS[identity_key] = replacement
    _GEX_RUNTIME.clear_error(instrument_id, route_key, source="live")
    _publish_live_gex_status_owned()
    return _LIVE_GEX_SESSIONS[identity_key]


def _live_session_strikes(session: _LiveGexSession) -> list[float]:
    return list(session.subscribed_strikes)


def _live_session_rebase_reason(
    session: _LiveGexSession,
    spot: float | None = None,
    *,
    now: datetime | None = None,
) -> str:
    health = session.provider_runtime.live_health(
        session.provider_subscription,
        now=now,
    )
    if health.unknown_expiry_contract_count:
        raise OptionUniverseUnavailableError(
            "Provider live option subscription is missing exact expiry metadata",
            reason="EXPIRY_TIME_UNKNOWN",
            diagnostics={
                "expiry_time_unknown_contracts_excluded": (health.unknown_expiry_contract_count),
            },
        )
    if health.expired_contract_count:
        expiry_at = health.earliest_expired_at
        if expiry_at is None:
            raise RuntimeError("Provider live option health omitted the exact expired timestamp")
        return f"exact option expiry reached {expiry_at.astimezone(UTC).isoformat()}"
    current_spot = finite_number_or_none(spot)
    if current_spot is None:
        current_spot = finite_number_or_none(session.spot)
    if current_spot is None:
        return ""
    strikes = _live_session_strikes(session)
    if session.provider_runtime.selected_strikes_require_rebase(
        strikes,
        current_spot,
    ):
        return f"spot {current_spot:.4f} near live strike edge {strikes[0]:.4f}-{strikes[-1]:.4f}"
    return ""


def _live_gex_frame_from_session(
    session: _LiveGexSession,
    *,
    max_levels: int,
    stale_minutes: int,
    underlying_quote: dict[str, Any],
) -> dict[str, Any]:
    frame_started = time.monotonic()
    phase_started = frame_started
    spot_observation = gex_underlying_quote_observation_from_cache(underlying_quote)
    spot = spot_observation["price"]
    session.spot = spot
    dividend_cache = session.dividend_cache
    effective_dividend_yield = session.effective_dividend_yield
    sample = session.provider_runtime.sample_live(
        session.provider_subscription,
        spot=spot,
        risk_free_rate=session.request.risk_free_rate,
        dividend_yield=effective_dividend_yield,
    )
    observe_metric(
        "gex_live_frame_phase_seconds",
        max(time.monotonic() - phase_started, 0.0),
        route_fingerprint=session.route_fingerprint,
        phase="provider_snapshot",
    )
    phase_started = time.monotonic()
    rows = list(sample.contract_rows)
    selected_strike_count = len(session.provider_subscription.subscribed_strikes)
    if selected_strike_count <= 0:
        raise RuntimeError("Provider live GEX subscription has no selected strike universe")
    now_utc = datetime.now(tz=UTC)
    health = session.provider_runtime.live_health(
        session.provider_subscription,
        now=now_utc,
    )
    if health.expired_contract_count or health.unknown_expiry_contract_count:
        raise OptionUniverseUnavailableError(
            "Provider live option universe crossed its exact expiry during sampling",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={
                "expired_contracts_excluded": health.expired_contract_count,
                "expiry_time_unknown_contracts_excluded": (health.unknown_expiry_contract_count),
            },
        )
    if health.universe_expires_at is None:
        raise OptionUniverseUnavailableError(
            "Provider live option subscription is missing its exact universe expiry",
            reason="EXPIRY_TIME_UNKNOWN",
            diagnostics={
                "expiry_time_unknown_contracts_excluded": (health.contract_count),
            },
        )
    authoritative_contract_rows = _qualified_gex_contract_rows(
        rows,
        authority_at=now_utc,
    )
    analysis_contract_rows = _qualified_gex_contract_rows(
        rows,
        authority_at=now_utc,
        expected_pair_count=session.contracts_requested // 2,
    )
    strikes = _aggregate_by_strike(analysis_contract_rows, spot)
    option_volume_meta = _attach_live_option_volume_events(
        session,
        analysis_contract_rows,
        strikes,
        now_utc,
    )
    diagnostics = _gex_diagnostics(
        rows,
        strikes,
        contracts_requested=session.contracts_requested,
        strikes_requested=session.request.strike_count,
        generic_ticks=GEX_GENERIC_TICKS,
        analysis_contract_rows=analysis_contract_rows,
        authoritative_contract_rows=authoritative_contract_rows,
    )
    levels = _levels_from_strikes(strikes)
    observe_metric(
        "gex_live_frame_phase_seconds",
        max(time.monotonic() - phase_started, 0.0),
        route_fingerprint=session.route_fingerprint,
        phase="domain_analysis",
    )
    meta = {
        "provider_symbol": session.provider_symbol,
        "spot": spot,
        "spot_observation": spot_observation,
        **session.underlying_audit,
        "provider_request_session": session.request.provider_metadata.request_session,
        "requested_market_data_entitlement": (
            session.request.provider_metadata.requested_entitlement
        ),
        "market_data_entitlement": sample.market_data_entitlement,
        "generic_ticks": GEX_GENERIC_TICKS,
        "observation_wait_seconds": 0.0,
        "strike_count": selected_strike_count,
        "provider_selected_strike_count": selected_strike_count,
        "requested_strike_limit": session.request.strike_count,
        "max_expirations": session.request.max_expirations,
        "max_contracts": session.request.max_contracts,
        "request_batch_size": 0,
        "request_batch_pause_seconds": 0.0,
        "refresh_mode": "live",
        "scheduler_lane": "live",
        "futures_options": session.request.futures_options,
        "risk_free_rate": session.request.risk_free_rate,
        "dividend_yield": effective_dividend_yield,
        "dividend_yield_source": "broker_cache" if dividend_cache.get("cached") else "config",
        "dividend_yield_cache": dividend_cache,
        "time_to_expiry_model": "provider_contract_expiry_at",
        "option_universe_expires_at": (health.universe_expires_at.astimezone(UTC).isoformat()),
        **session.chain_meta,
    }
    phase_started = time.monotonic()
    payload = _payload_from_rows(
        session.provider_symbol,
        captured_at=now_utc,
        meta=meta,
        strikes=strikes,
        diagnostics=diagnostics,
        max_levels=max_levels,
        stale_minutes=stale_minutes,
        source=GEX_LIVE_SNAPSHOT_SOURCE,
        capture_mode="live",
        contract_rows=analysis_contract_rows,
        comparison_contract_rows=(session.provider_subscription.selected_contract_universe),
    )
    observe_metric(
        "gex_live_frame_phase_seconds",
        max(time.monotonic() - phase_started, 0.0),
        route_fingerprint=session.route_fingerprint,
        phase="payload_build",
    )
    phase_started = time.monotonic()
    diagnostics = payload["diagnostics"]
    frame_complete = bool(payload.get("frame_complete"))
    quality_reason = str(diagnostics.get("diag_frame_quality_reason") or "incomplete GEX frame")
    session.frame_seq += 1
    session.last_frame_at = now_utc
    warming = not frame_complete
    trend_meta: dict[str, Any] = {}
    if frame_complete:
        trend_meta = _attach_live_trend(session, payload, strikes, levels, now_utc)
    live_meta = {
        "enabled": True,
        "active": True,
        "session_id": session.started_at.isoformat(),
        "started_at": session.started_at.isoformat(),
        "last_frame_at": session.last_frame_at.isoformat(),
        "frame_seq": session.frame_seq,
        "subscriptions": session.provider_runtime.live_health(
            session.provider_subscription
        ).subscription_count,
        "warming": bool(warming),
        "publishable": bool(frame_complete),
        "quality_reason": quality_reason,
        "holding_last_publishable": bool(warming and session.last_publishable_payload is not None),
        "trend_window_seconds": _LIVE_GEX_TREND_WINDOW_SECONDS,
        "trend": trend_meta,
        "option_volume_history_seconds": _LIVE_GEX_OPTION_VOLUME_HISTORY_SECONDS,
        "option_volume_event_seconds": _LIVE_GEX_OPTION_VOLUME_EVENT_SECONDS,
        "option_volume_events": option_volume_meta,
    }
    if warming and session.last_publishable_payload is not None:
        current_partial = payload
        payload = deepcopy(session.last_publishable_payload)
        payload["ok"] = False
        payload["status"] = "warming"
        payload["stale"] = True
        payload["preserved_context"] = True
        payload["frame_complete"] = False
        payload["decision_authoritative"] = False
        payload["message"] = (
            "GEX LIVE WARMING: latest streamed frame is incomplete; "
            f"holding the last publishable context. {quality_reason}."
        )
        payload["latest_attempt"] = {
            "source": current_partial["source"],
            "capture_mode": current_partial["capture_mode"],
            "captured_at": current_partial["captured_at"],
            "market_data_entitlement": current_partial["market_data_entitlement"],
            "status": "warming",
            "message": str(current_partial.get("message") or quality_reason),
            "diagnostics": diagnostics,
        }
    elif warming:
        payload["ok"] = False
        payload["status"] = "warming"
        payload["frame_complete"] = False
        payload["decision_authoritative"] = False
    payload["instrument_id"] = session.instrument_id
    payload["route_fingerprint"] = session.route_fingerprint
    payload["live"] = live_meta
    payload["last_sync_at"] = payload["live"]["last_frame_at"]
    if frame_complete:
        session.last_publishable_payload = deepcopy(payload)
        _publish_live_gex_frame_snapshot(
            (session.instrument_id, session.route_fingerprint),
            payload,
        )
    observe_metric(
        "gex_live_frame_phase_seconds",
        max(time.monotonic() - phase_started, 0.0),
        route_fingerprint=session.route_fingerprint,
        phase="finalization",
    )
    observe_metric(
        "gex_live_frame_seconds",
        max(time.monotonic() - frame_started, 0.0),
        route_fingerprint=session.route_fingerprint,
    )
    return payload


def _attach_live_option_volume_events(
    session: _LiveGexSession,
    rows: Sequence[Mapping[str, Any]],
    strikes: Sequence[dict[str, Any]],
    now_utc: datetime,
) -> dict[str, Any]:
    current_spot = finite_number_or_none(session.spot)
    frame = _live_option_volume_frame(rows, now_utc, current_spot)
    session.option_volume_frames.append(frame)
    current_ts = float(frame["ts"])
    cutoff = current_ts - _LIVE_GEX_OPTION_VOLUME_HISTORY_SECONDS
    session.option_volume_frames = [
        item for item in session.option_volume_frames if float(item.get("ts") or 0.0) >= cutoff
    ]
    event_meta = {
        "frame_count": len(session.option_volume_frames),
        "event_window_seconds": _LIVE_GEX_OPTION_VOLUME_EVENT_SECONDS,
        "history_window_seconds": _LIVE_GEX_OPTION_VOLUME_HISTORY_SECONDS,
        "contract_count": len(frame["contracts"]),
        "invalid_frame": "",
        "invalid_contract_key": "",
    }
    if frame["status"] != "ready":
        return {
            **event_meta,
            "status": "invalid",
            "reason": frame["reason"],
            "invalid_frame": "current",
            "invalid_contract_key": frame["invalid_contract_key"],
            "window_seconds": 0.0,
            "event_count": 0,
        }

    baseline = _live_option_volume_baseline(session.option_volume_frames, current_ts)
    if baseline is None:
        return {
            **event_meta,
            "status": "warming",
            "window_seconds": 0.0,
            "event_count": 0,
            "reason": "WAITING_FOR_EVENT_WINDOW",
        }

    baseline_ts = finite_number_or_none(baseline.get("ts"))
    if baseline_ts is None or baseline_ts >= current_ts:
        return {
            **event_meta,
            "status": "invalid",
            "reason": "INVALID_EVENT_WINDOW",
            "invalid_frame": "comparison",
            "invalid_contract_key": "",
            "window_seconds": 0.0,
            "event_count": 0,
        }

    window_frames = [
        item
        for item in session.option_volume_frames
        if (item_ts := finite_number_or_none(item.get("ts"))) is not None
        and baseline_ts <= item_ts <= current_ts
    ]
    previous_window_frame = window_frames[0]
    for window_frame in window_frames[1:]:
        window_comparison = _compare_live_option_volume_frames(previous_window_frame, window_frame)
        if window_comparison["status"] != "ready":
            return {
                **event_meta,
                "status": "invalid",
                "reason": window_comparison["reason"],
                "invalid_frame": window_comparison["invalid_frame"],
                "invalid_contract_key": window_comparison["invalid_contract_key"],
                "window_seconds": round(current_ts - baseline_ts, 1),
                "event_count": 0,
            }
        previous_window_frame = window_frame

    comparison = _compare_live_option_volume_frames(baseline, frame)
    if comparison["status"] != "ready":
        return {
            **event_meta,
            "status": "invalid",
            "reason": comparison["reason"],
            "invalid_frame": comparison["invalid_frame"],
            "invalid_contract_key": comparison["invalid_contract_key"],
            "window_seconds": round(current_ts - baseline_ts, 1),
            "event_count": 0,
        }

    current_contracts = frame.get("contracts") if isinstance(frame.get("contracts"), dict) else {}
    contract_deltas = comparison["contract_deltas"]
    by_strike: dict[float, dict[str, Any]] = {}
    for row in strikes:
        strike = finite_number_or_none(row.get("strike"))
        if strike is None:
            continue
        current = _option_volume_context_from_strike(row)["current"]
        by_strike[float(strike)] = {
            "strike": float(strike),
            "call_volume_delta": 0.0,
            "put_volume_delta": 0.0,
            "total_oi": float(current["total_oi"]),
        }
    window_seconds = current_ts - baseline_ts
    previous_spot = finite_number_or_none(baseline.get("spot"))
    spot_move = (
        current_spot - previous_spot
        if current_spot is not None and previous_spot is not None
        else None
    )
    strike_step = _live_strike_step_from_strikes(strikes)
    strike_abs_gex = {
        float(strike): float(row["abs_gex"])
        for row in strikes
        if (strike := finite_number_or_none(row.get("strike"))) is not None
    }
    for key, current in current_contracts.items():
        if not isinstance(current, dict):
            continue
        strike = finite_number_or_none(current.get("strike"))
        if strike is None:
            continue
        right = current.get("right")
        if right not in {"C", "P"}:
            raise ValueError("Live GEX option activity requires an exact option right")
        volume_delta = float(contract_deltas[str(key)])
        bucket = by_strike.get(float(strike))
        if bucket is None:
            continue
        side = "call" if right == "C" else "put"
        bucket[f"{side}_volume_delta"] += volume_delta

    prior_rates_by_strike = _live_option_volume_interval_rates_by_strike(
        session.option_volume_frames,
        current_ts=current_ts,
    )
    material_event_count = 0
    for strike, bucket in sorted(by_strike.items()):
        call_delta = float(bucket["call_volume_delta"])
        put_delta = float(bucket["put_volume_delta"])
        total_delta = call_delta + put_delta
        total_oi = float(bucket["total_oi"])
        event_bias = (call_delta - put_delta) / total_delta if total_delta > 0 else None
        turnover_delta = total_delta / total_oi if total_oi > 0 else None
        prior_rates = prior_rates_by_strike.get(strike, ())
        median_rate = _median_positive(prior_rates)
        current_rate = total_delta / window_seconds if window_seconds > 0 else None
        acceleration = (
            current_rate / median_rate
            if current_rate is not None and median_rate is not None and median_rate > 0
            else None
        )
        flow_per_point = (
            total_delta / abs(spot_move)
            if spot_move is not None and abs(spot_move) > 1e-9
            else None
        )
        material = _live_option_volume_activity_is_material(
            total_delta,
            turnover_delta,
            acceleration,
        )
        interaction, cross_direction = _live_option_volume_interaction(
            strike=strike,
            total_delta=total_delta,
            event_bias=event_bias,
            turnover_delta=turnover_delta,
            acceleration=acceleration,
            abs_gex=strike_abs_gex.get(strike, 0.0),
            total_oi=total_oi,
            current_spot=current_spot,
            previous_spot=previous_spot,
            strike_step=strike_step,
            material=material,
        )
        event = {
            "total_volume_delta": total_delta,
            "call_volume_delta": call_delta,
            "put_volume_delta": put_delta,
            "bias": round(event_bias, 4) if event_bias is not None else None,
            "call_participation": round(call_delta / total_delta, 4) if total_delta > 0 else None,
            "put_participation": round(put_delta / total_delta, 4) if total_delta > 0 else None,
            "turnover": round(turnover_delta, 4) if turnover_delta is not None else None,
            "acceleration": round(acceleration, 4) if acceleration is not None else None,
            "flow_per_point": round(flow_per_point, 4) if flow_per_point is not None else None,
            "spot_move_points": round(spot_move, 4) if spot_move is not None else None,
            "material": material,
            "interaction": interaction,
            "cross_direction": cross_direction,
            "state": _live_option_volume_event_state(
                total_delta,
                event_bias,
                turnover_delta,
                acceleration,
                material=material,
            ),
            "window_seconds": round(window_seconds, 1),
            "source": "broker_volume_delta",
        }
        bucket["_option_volume_event"] = event
        if material:
            material_event_count += 1

    by_key = {_live_trend_price_key(row["strike"]): row for row in by_strike.values()}
    for row in strikes:
        flow = by_key.get(_live_trend_price_key(row.get("strike")))
        if not flow:
            continue
        row["_option_volume_event"] = dict(flow.get("_option_volume_event") or {})

    return {
        **event_meta,
        "status": "ready",
        "reason": comparison["reason"],
        "window_seconds": round(window_seconds, 1),
        "event_count": material_event_count,
    }


def _attach_live_trend(
    session: _LiveGexSession,
    payload: dict[str, Any],
    strikes: Sequence[Mapping[str, Any]],
    levels: Mapping[str, Any],
    now_utc: datetime,
) -> dict[str, Any]:
    frame = _live_trend_frame(strikes, levels, now_utc)
    session.trend_frames.append(frame)
    cutoff = float(frame["ts"]) - _LIVE_GEX_TREND_WINDOW_SECONDS
    session.trend_frames = [item for item in session.trend_frames if float(item["ts"]) >= cutoff]
    baseline = _live_trend_baseline(session.trend_frames, float(frame["ts"]))
    if baseline is None:
        return {
            "status": "warming",
            "frame_count": len(session.trend_frames),
            "reason": "waiting for enough live frames to measure trend",
        }

    window_seconds = max(0.0, float(frame["ts"]) - float(baseline["ts"]))
    motion_by_key: dict[str, dict[str, Any]] = {}
    baseline_rows = baseline.get("rows") if isinstance(baseline.get("rows"), dict) else {}
    current_rows = frame.get("rows") if isinstance(frame.get("rows"), dict) else {}
    for key, current_row in current_rows.items():
        previous_row = baseline_rows.get(key)
        if not isinstance(previous_row, dict):
            continue
        events: dict[str, Any] = {}
        for side in ("call", "put"):
            motion = _live_side_motion(
                side,
                float(previous_row[side]),
                float(current_row[side]),
                window_seconds,
            )
            if motion:
                events[side] = motion
        net_motion = _live_net_motion(
            float(previous_row["net"]),
            float(current_row["net"]),
            window_seconds,
        )
        if net_motion:
            events["net"] = net_motion
        if events:
            motion_by_key[key] = events

    for wall_name, side in (("call_wall", "call"), ("put_wall", "put")):
        roll = _live_roll_motion(
            side, baseline.get(wall_name), frame.get(wall_name), window_seconds
        )
        if not roll:
            continue
        key = _live_trend_price_key(frame.get(wall_name))
        if not key:
            continue
        motion_by_key.setdefault(key, {})[side] = roll

    _attach_live_motion_to_rows(payload.get("levels"), motion_by_key)
    return {
        "status": "ready",
        "frame_count": len(session.trend_frames),
        "baseline_age_seconds": round(window_seconds, 1),
        "event_count": sum(len(events) for events in motion_by_key.values()),
    }


async def _stop_live_gex_context_owned(
    identity_key: tuple[str, str] | None,
) -> dict[str, Any]:
    sessions = (
        [_LIVE_GEX_SESSIONS[identity_key]]
        if identity_key is not None and identity_key in _LIVE_GEX_SESSIONS
        else list(_LIVE_GEX_SESSIONS.values())
        if identity_key is None
        else []
    )
    if not sessions:
        _clear_live_gex_frame_snapshot(identity_key)
        if identity_key is None:
            _GEX_RUNTIME.release_all_capture_modes("live")
        else:
            _GEX_RUNTIME.release_capture_mode(*identity_key, "live")
        _publish_live_gex_status_owned()
        return {"ok": True, "enabled": False, "active": False, "cancelled": 0}
    cancelled = 0
    for session in sessions:
        cancelled += _cancel_live_gex_session_owned(session)
        session_identity_key = (session.instrument_id, session.route_fingerprint)
        _LIVE_GEX_SESSIONS.pop(session_identity_key, None)
        _clear_live_gex_frame_snapshot(session_identity_key)
        _GEX_RUNTIME.release_capture_mode(
            session.instrument_id,
            session.route_fingerprint,
            "live",
        )
    if identity_key is None:
        _clear_live_gex_frame_snapshot(None)
        _GEX_RUNTIME.release_all_capture_modes("live")
    await asyncio.sleep(0.05)
    _publish_live_gex_status_owned()
    return {
        "ok": True,
        "enabled": False,
        "active": False,
        "cancelled": cancelled,
        "provider_symbols": [session.provider_symbol for session in sessions],
    }


def _cancel_live_gex_session_owned(session: _LiveGexSession) -> int:
    return session.provider_runtime.close_live(session.provider_subscription)
