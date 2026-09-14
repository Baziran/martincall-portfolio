from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha1
import json
from typing import Any

from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    require_exact_identity_text,
    route_fingerprint,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.gex.config import (
    GexRequestConfig,
    GexSchedulerJob,
)
from aef_terminal.data.gex.constants import (
    GEX_GENERIC_TICKS,
    GEX_REQUEST_MAX_CONTRACTS,
    GEX_SCHEDULER_INTERVAL_MINUTES,
    NY_TZ,
)
from aef_terminal.data.gex.session import _GEX_RUNTIME
from aef_terminal.data.gex.provider_runtime import GexProviderRuntime


def gex_runtime_status(*, provider_runtime: GexProviderRuntime) -> dict[str, Any]:
    from aef_terminal.data.gex.live import live_gex_status

    provider_status = provider_runtime.runtime_status()
    request_connected = provider_status.request_connected
    live_status = live_gex_status()
    route_runtime = _GEX_RUNTIME.routes_snapshot()
    live_sessions = [
        dict(row) for row in live_status.get("sessions", []) if isinstance(row, Mapping)
    ]
    for session in live_sessions:
        raw_instrument_id = session.get("instrument_id")
        raw_route_key = session.get("route_fingerprint")
        instrument_id = raw_instrument_id if isinstance(raw_instrument_id, str) else ""
        route_key = raw_route_key if isinstance(raw_route_key, str) else ""
        raw_provider_symbol = session.get("provider_symbol")
        try:
            provider_symbol = require_exact_identity_text(
                raw_provider_symbol,
                field="provider_symbol",
            )
        except ValueError:
            continue
        if not instrument_id or not route_key:
            continue
        runtime_key = json.dumps([instrument_id, route_key], separators=(",", ":"))
        state = route_runtime.setdefault(
            runtime_key,
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_key,
                "provider_symbol": provider_symbol,
                "active_capture_mode": "live",
                "errors": {},
                "last_sync_at": "",
                "last_request": "",
                "last_diagnostics": {},
                "request_count": 0,
                "failure_backoff": {
                    "active": False,
                    "retry_at": "",
                    "retry_in_seconds": 0.0,
                    "consecutive_failures": 0,
                    "source": "",
                    "kind": "",
                    "reason": "",
                    "diagnostics": {},
                },
                "failure_backoffs": {},
            },
        )
        live_sync_at = str(session.get("last_frame_at") or "")
        if live_sync_at > str(state.get("last_sync_at") or ""):
            state["last_sync_at"] = live_sync_at
            live_diagnostics = session.get("last_diagnostics")
            if isinstance(live_diagnostics, Mapping):
                state["last_diagnostics"] = dict(live_diagnostics)
    live_sync_times = [
        str(row.get("last_frame_at") or "")
        for row in live_sessions
        if str(row.get("last_frame_at") or "")
    ]
    request_sync_times = [
        str(state.get("last_sync_at") or "")
        for state in route_runtime.values()
        if str(state.get("last_sync_at") or "")
    ]
    last_sync_at = max([*request_sync_times, *live_sync_times], default="")
    return {
        "connected": request_connected or bool(live_status.get("active")),
        "request_connected": request_connected,
        "live_connected": bool(live_status.get("active")),
        "sessions": live_sessions,
        "subscriptions": int(live_status.get("subscriptions") or 0),
        "subscription_budget": int(live_status.get("subscription_budget") or 0),
        "subscription_over_budget": int(live_status.get("subscription_over_budget") or 0),
        "provider": provider_status.provider_key,
        "endpoint": provider_status.endpoint,
        "request_session": provider_status.request_session,
        "requested_market_data_entitlement": provider_status.requested_entitlement,
        "generic_ticks": GEX_GENERIC_TICKS,
        "max_contracts": GEX_REQUEST_MAX_CONTRACTS,
        "batch_size": provider_status.batch_size,
        "expiry_mode": provider_status.expiry_mode,
        "input_greeks_source": "broker_option_stream_plus_local_iv",
        "greek_source_priority": [
            "modelGreeks",
            "lastGreeks",
            "bidGreeks",
            "askGreeks",
            "local_iv",
        ],
        "computed_fields": [
            "contract_gex",
            "strike_net_gex",
            "gamma_flip",
            "call_wall",
            "put_wall",
            "flow_per_point",
        ],
        "math_defaults": {
            "risk_free_rate": provider_status.risk_free_rate,
            "dividend_yield": provider_status.dividend_yield,
            "time_to_expiry": "provider contract expiry_at; Zero Gamma unavailable when unknown",
        },
        "last_sync_at": last_sync_at,
        "routes": route_runtime,
        "request_count_total": sum(
            int(state.get("request_count") or 0) for state in route_runtime.values()
        ),
        "lock_busy": provider_status.queue_running,
        "queue_pending": provider_status.queue_pending,
        "queue_running": provider_status.queue_running,
        "queue_running_label": provider_status.queue_running_label,
    }


def gex_scheduler_jobs(
    instruments: Sequence[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    session_open: Callable[[Mapping[str, Any], datetime], bool | None] | None = None,
) -> list[GexSchedulerJob]:
    current = (now or datetime.now(NY_TZ)).astimezone(NY_TZ)
    jobs: list[GexSchedulerJob] = []
    for instrument in instruments or ():
        route = route_instrument(dict(instrument))
        if not route.adapter.capabilities.gex:
            continue
        exact_provider_symbol = route.provider_symbol
        try:
            session_state = (
                session_open(route.instrument, current) if session_open is not None else None
            )
        except Exception:
            session_state = None
        session_ok = session_state is True
        interval = _gex_lane_interval()
        request = route.adapter.gex_request_config(
            route.instrument,
            mode="auto",
        )
        offset = _gex_route_offset_minutes(
            route.instrument_id,
            route.fingerprint,
            interval,
        )
        next_due = _next_scheduler_due(current, interval, offset)
        jobs.append(
            GexSchedulerJob(
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                provider_symbol=exact_provider_symbol,
                lane="auto",
                interval_minutes=interval,
                offset_minutes=offset,
                strike_count=request.strike_count,
                max_contracts=request.max_contracts,
                max_expirations=request.max_expirations,
                enabled=session_ok,
                reason="session_open"
                if session_ok
                else "session_unknown"
                if session_state is None
                else "outside_exchange_hours",
                next_due_at=next_due,
                refresh_mode="auto",
            )
        )
    return jobs


def gex_request_for_scheduler_job(
    job: GexSchedulerJob,
    *,
    instrument: dict[str, Any],
    base: GexRequestConfig,
) -> GexRequestConfig:
    route_key = route_fingerprint(instrument)
    if (
        qualified_instrument_id(instrument) != job.instrument_id
        or route_key != job.route_fingerprint
    ):
        raise ValueError("GEX scheduler job route no longer matches the qualified instrument")
    if (
        job.strike_count != base.strike_count
        or job.max_contracts != base.max_contracts
        or job.max_expirations != base.max_expirations
        or job.lane != "auto"
        or job.refresh_mode != "auto"
    ):
        raise ValueError(
            "GEX scheduler job configuration is stale relative to the canonical request"
        )
    return replace(base, refresh_mode="auto", scheduler_lane="auto")


def _gex_lane_interval() -> int:
    return GEX_SCHEDULER_INTERVAL_MINUTES


def _gex_route_offset_minutes(
    instrument_id: str,
    route_fingerprint: str,
    interval_minutes: int,
) -> int:
    if type(interval_minutes) is not int or interval_minutes <= 0:
        raise ValueError("GEX route interval must be a positive integer")
    interval = interval_minutes
    instrument_key = require_exact_identity_text(instrument_id, field="instrument_id")
    route_key = require_exact_identity_text(route_fingerprint, field="route_fingerprint")
    identity_key = json.dumps([instrument_key, route_key], separators=(",", ":"))
    return int(sha1(identity_key.encode("utf-8")).hexdigest()[:8], 16) % interval


def _next_scheduler_due(now: datetime, interval_minutes: int, offset_minutes: int) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("GEX scheduler requires a timezone-aware timestamp")
    if type(interval_minutes) is not int or interval_minutes <= 0:
        raise ValueError("GEX scheduler interval must be a positive integer")
    if type(offset_minutes) is not int or not 0 <= offset_minutes < interval_minutes:
        raise ValueError("GEX scheduler offset must belong to its exact interval")
    current = now.astimezone(NY_TZ).replace(second=0, microsecond=0)
    midnight = current.replace(hour=0, minute=0)
    elapsed = int((current - midnight).total_seconds() // 60)
    interval = interval_minutes
    offset = offset_minutes
    remainder = (elapsed - offset) % interval
    due_in = 0 if remainder == 0 else interval - remainder
    return current + timedelta(minutes=due_in)
