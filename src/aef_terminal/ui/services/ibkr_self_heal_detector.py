from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.runtime.timeframes import parse_aware_utc_ts
from aef_terminal.ui.services.ibkr_self_heal_models import (
    IBKR_SELF_HEAL_API_ERROR_THRESHOLD,
    IBKR_SELF_HEAL_LANE_STUCK_SECONDS,
    IBKR_SELF_HEAL_NO_QUOTE_SECONDS,
    IBKR_SELF_HEAL_PENDING_STUCK_SECONDS,
    IBKR_SELF_HEAL_POLL_SECONDS,
    IbkrSelfHealIssue,
)


def _float_value(value: Any) -> float:
    try:
        return float(value or 0.0)
    except TypeError, ValueError:
        return 0.0


def _parse_iso_age_seconds(value: Any, now_wall: datetime) -> float | None:
    parsed = parse_aware_utc_ts(value)
    if parsed is None:
        return None
    return max((now_wall - parsed).total_seconds(), 0.0)


def _active_chart_seed(active_streams: list[tuple[str, str, str, int]]) -> tuple[str, str, str]:
    if not active_streams:
        return "", "5m", "3d"
    instrument_id, interval, range_, _count = active_streams[0]
    return (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        str(interval or "5m"),
        str(range_ or "3d"),
    )


def detect_ibkr_self_heal_issue(
    status: dict[str, Any],
    active_streams: list[tuple[str, str, str, int]],
    qualities: dict[tuple[str, str], dict[str, Any]],
    quote_route_fingerprints: list[str],
    *,
    now_wall: datetime | None = None,
) -> IbkrSelfHealIssue | None:
    status = status if isinstance(status, dict) else {}
    quote_route_fingerprints = [
        require_exact_identity_text(
            route_key,
            field="quote_route_fingerprint",
        )
        for route_key in quote_route_fingerprints
    ]
    quote_subscriptions = int(status.get("quote_subscriptions") or 0)
    chart_demand = bool(active_streams)
    quote_demand = bool(quote_route_fingerprints or quote_subscriptions > 0)
    active_demand = bool(chart_demand or quote_demand)
    quarantined_session_count = int(status.get("quarantined_session_count") or 0)
    if quarantined_session_count > 0:
        raw_scopes = status.get("quarantined_session_scopes")
        quarantine_scopes = (
            {str(scope): int(count) for scope, count in raw_scopes.items() if int(count) > 0}
            if isinstance(raw_scopes, dict)
            else {}
        )
        raw_persistent = status.get("quarantined_persistent_sessions")
        persistent_sessions = (
            sorted({str(item) for item in raw_persistent if str(item)})
            if isinstance(raw_persistent, list)
            else []
        )
        chart_instrument_id, interval, range_ = _active_chart_seed(active_streams)
        return IbkrSelfHealIssue(
            code="session_quarantined",
            reason=(
                "IBKR has "
                f"{quarantined_session_count} quarantined session"
                f"{'' if quarantined_session_count == 1 else 's'}"
            ),
            chart_instrument_id=chart_instrument_id,
            interval=interval,
            range_=range_,
            details={
                "quarantined_session_count": quarantined_session_count,
                "quarantined_session_scopes": quarantine_scopes,
                "quarantined_persistent_sessions": persistent_sessions,
            },
        )
    if not active_demand:
        return None

    chart_instrument_id, interval, range_ = _active_chart_seed(active_streams)
    manager = (
        status.get("market_data_manager")
        if isinstance(status.get("market_data_manager"), dict)
        else {}
    )
    lanes = manager.get("lanes") if isinstance(manager.get("lanes"), dict) else {}
    for lane, raw_lane in lanes.items():
        lane_status = raw_lane if isinstance(raw_lane, dict) else {}
        running_seconds = _float_value(lane_status.get("running_seconds"))
        running_timeout_seconds = _float_value(lane_status.get("running_timeout_seconds"))
        running_stuck_seconds = max(
            IBKR_SELF_HEAL_LANE_STUCK_SECONDS,
            running_timeout_seconds + IBKR_SELF_HEAL_POLL_SECONDS
            if running_timeout_seconds > 0
            else 0.0,
        )
        pending_seconds = _float_value(lane_status.get("oldest_pending_seconds"))
        pending = int(lane_status.get("pending") or 0)
        if bool(lane_status.get("running")) and running_seconds >= running_stuck_seconds:
            return IbkrSelfHealIssue(
                code="lane_running_stalled",
                reason=f"IBKR {lane} lane running for {running_seconds:.1f}s",
                chart_instrument_id=chart_instrument_id,
                interval=interval,
                range_=range_,
                details={
                    "lane": str(lane),
                    "running_seconds": round(running_seconds, 3),
                    "running_timeout_seconds": running_timeout_seconds or None,
                    "stuck_threshold_seconds": running_stuck_seconds,
                },
            )
        if pending > 0 and pending_seconds >= IBKR_SELF_HEAL_PENDING_STUCK_SECONDS:
            return IbkrSelfHealIssue(
                code="lane_queue_stalled",
                reason=f"IBKR {lane} lane pending for {pending_seconds:.1f}s",
                chart_instrument_id=chart_instrument_id,
                interval=interval,
                range_=range_,
                details={
                    "lane": str(lane),
                    "pending": pending,
                    "pending_seconds": round(pending_seconds, 3),
                },
            )
    history_busy_seconds = _float_value(status.get("history_busy_seconds"))
    if history_busy_seconds >= IBKR_SELF_HEAL_LANE_STUCK_SECONDS:
        return IbkrSelfHealIssue(
            code="history_request_stalled",
            reason=f"IBKR history request busy for {history_busy_seconds:.1f}s",
            chart_instrument_id=chart_instrument_id,
            interval=interval,
            range_=range_,
            details={
                "history_request": status.get("history_request") or "",
                "history_busy_seconds": round(history_busy_seconds, 3),
            },
        )

    recent_api_errors = int(status.get("recent_api_error_count_60s") or 0)
    if recent_api_errors >= IBKR_SELF_HEAL_API_ERROR_THRESHOLD:
        return IbkrSelfHealIssue(
            code="api_error_flood",
            reason=f"IBKR API error stream noisy: {recent_api_errors} errors/60s",
            chart_instrument_id=chart_instrument_id,
            interval=interval,
            range_=range_,
            details={"recent_api_error_count_60s": recent_api_errors},
        )

    connected = any(
        bool(status.get(key))
        for key in ("quote_connected", "chart_connected", "history_connected", "ok")
    )
    raw_connection_in_progress = status.get("connection_in_progress")
    connection_in_progress = (
        raw_connection_in_progress if isinstance(raw_connection_in_progress, dict) else {}
    )
    chart_connection_in_progress = bool(
        chart_demand
        and (
            connection_in_progress.get("chart") is True
            or connection_in_progress.get("history") is True
        )
    )
    quote_connection_in_progress = bool(
        quote_demand and connection_in_progress.get("quote") is True
    )
    if not connected:
        if chart_connection_in_progress or quote_connection_in_progress:
            return None
        return IbkrSelfHealIssue(
            code="connection_down",
            reason="IBKR has active demand but no connected session",
            chart_instrument_id=chart_instrument_id,
            interval=interval,
            range_=range_,
            details={
                "quote_route_fingerprints": quote_route_fingerprints[:12],
                "active_streams": len(active_streams),
            },
        )

    now_wall = now_wall or datetime.now(tz=UTC)
    quote_connected = bool(status.get("quote_connected"))
    quote_values = int(status.get("quote_values") or 0)
    if quote_demand and not quote_connected:
        if quote_connection_in_progress:
            return None
        quote_lane = lanes.get("quote") if isinstance(lanes.get("quote"), dict) else {}
        quote_lane_error = ""
        raw_quote_lane_error = (
            quote_lane.get("error") if isinstance(quote_lane.get("error"), dict) else {}
        )
        if raw_quote_lane_error:
            quote_lane_error = str(
                raw_quote_lane_error.get("message") or raw_quote_lane_error.get("code") or ""
            )
        quote_lane_error = quote_lane_error or str(quote_lane.get("last_error") or "")
        return IbkrSelfHealIssue(
            code="quote_session_down",
            reason="IBKR quote session is down while live quote demand is active",
            chart_instrument_id=chart_instrument_id,
            interval=interval,
            range_=range_,
            details={
                "quote_route_fingerprints": quote_route_fingerprints[:12],
                "quote_subscriptions": quote_subscriptions,
                "chart_connected": bool(status.get("chart_connected")),
                "history_connected": bool(status.get("history_connected")),
                "quote_lane_error": quote_lane_error,
                "last_quote_error": str(status.get("last_quote_error") or ""),
            },
        )
    last_quote_sync_age = _parse_iso_age_seconds(status.get("last_quote_sync_at"), now_wall)
    if (
        quote_connected
        and quote_subscriptions > 0
        and quote_values == 0
        and (last_quote_sync_age is None or last_quote_sync_age >= IBKR_SELF_HEAL_NO_QUOTE_SECONDS)
    ):
        return IbkrSelfHealIssue(
            code="connected_no_quotes",
            reason="IBKR quote session is connected but no quote values arrive",
            chart_instrument_id=chart_instrument_id,
            interval=interval,
            range_=range_,
            details={
                "quote_subscriptions": quote_subscriptions,
                "last_quote_sync_age": last_quote_sync_age,
            },
        )

    history_lane = lanes.get("history") if isinstance(lanes.get("history"), dict) else {}
    history_repair_active = (
        bool(history_lane.get("running")) or int(history_lane.get("pending") or 0) > 0
    )
    for (instrument_id, active_interval), quality in qualities.items():
        quality = quality if isinstance(quality, dict) else {}
        if bool(quality.get("market_closed")) or bool(quality.get("session_warmup")):
            continue
        stale_minutes = _float_value(quality.get("stale_minutes"))
        stale_threshold = _float_value(quality.get("stale_threshold_minutes"))
        if stale_threshold > 0 and stale_minutes > stale_threshold and not history_repair_active:
            return IbkrSelfHealIssue(
                code="active_chart_stale",
                reason=f"Active IBKR chart {instrument_id} {active_interval} is stale while sessions are connected",
                chart_instrument_id=require_exact_identity_text(
                    instrument_id,
                    field="instrument_id",
                ),
                interval=str(active_interval or interval),
                range_=range_,
                details={
                    "quality_status": quality.get("status"),
                    "warning": quality.get("warning") or "",
                },
            )
    return None
