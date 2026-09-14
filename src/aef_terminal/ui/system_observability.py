from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aef_terminal.engine.common import parse_aware_utc_ts


def ibkr_lane_observability(ibkr: dict[str, Any]) -> dict[str, Any]:
    manager = ibkr.get("market_data_manager") if isinstance(ibkr, dict) else {}
    lanes = manager.get("lanes") if isinstance(manager, dict) else {}
    lane_items = lanes if isinstance(lanes, dict) else {}
    worst_pending = 0.0
    worst_running = 0.0
    pending_lanes: list[str] = []
    running_lanes: list[str] = []
    error_lanes: list[str] = []
    optional_pending_lanes: list[str] = []
    optional_running_lanes: list[str] = []
    optional_error_lanes: list[str] = []
    optional_lane_names = {"gex", "options"}
    optional_worst_pending = 0.0
    optional_worst_running = 0.0
    for lane, raw in lane_items.items():
        item = raw if isinstance(raw, dict) else {}
        lane_name = str(lane)
        optional = lane_name in optional_lane_names
        pending = int(item.get("pending") or 0)
        running = bool(item.get("running"))
        pending_seconds = float(item.get("oldest_pending_seconds") or 0.0)
        running_seconds = float(item.get("running_seconds") or 0.0)
        if pending > 0:
            if optional:
                optional_pending_lanes.append(lane_name)
                optional_worst_pending = max(optional_worst_pending, pending_seconds)
            else:
                pending_lanes.append(lane_name)
                worst_pending = max(worst_pending, pending_seconds)
        if running:
            if optional:
                optional_running_lanes.append(lane_name)
                optional_worst_running = max(optional_worst_running, running_seconds)
            else:
                running_lanes.append(lane_name)
                worst_running = max(worst_running, running_seconds)
        if item.get("last_error") or item.get("error"):
            if optional:
                optional_error_lanes.append(lane_name)
            else:
                error_lanes.append(lane_name)
    severity = "ok"
    reason = "ok"
    if error_lanes:
        severity = "warn"
        reason = "lane_errors"
    if worst_pending >= 30.0:
        severity = "warn"
        reason = "lane_queue_latency"
    if worst_pending >= 120.0:
        severity = "error"
        reason = "lane_queue_stalled"
    return {
        "severity": severity,
        "reason": reason,
        "pending_lanes": pending_lanes,
        "running_lanes": running_lanes,
        "error_lanes": error_lanes,
        "max_pending_seconds": round(worst_pending, 3),
        "max_running_seconds": round(worst_running, 3),
        "optional_pending_lanes": optional_pending_lanes,
        "optional_running_lanes": optional_running_lanes,
        "optional_error_lanes": optional_error_lanes,
        "optional_max_pending_seconds": round(optional_worst_pending, 3),
        "optional_max_running_seconds": round(optional_worst_running, 3),
    }


def tick_live_observability(tick_live: dict[str, Any]) -> dict[str, Any]:
    buffer = tick_live.get("buffer") if isinstance(tick_live, dict) else {}
    buffer = buffer if isinstance(buffer, dict) else {}
    health = (
        tick_live.get("health")
        if isinstance(tick_live.get("health"), dict)
        else buffer.get("health")
    )
    health = health if isinstance(health, dict) else {}
    dropped = int(buffer.get("dropped") or 0)
    buffered = int(buffer.get("buffered") or 0)
    severity = str(health.get("severity") or "ok").lower()
    reason = str(health.get("reason") or "ok")
    if dropped > 0 and severity not in {"error", "bad"}:
        severity = "error"
        reason = "dropped_ticks"
    return {
        "severity": severity,
        "reason": reason,
        "message": str(health.get("message") or ""),
        "dropped": dropped,
        "buffered": buffered,
        "buffer_ratio": float(health.get("buffer_ratio") or 0.0),
        "db_consecutive_errors": int(buffer.get("db_consecutive_errors") or 0),
        "purge_consecutive_errors": int(buffer.get("purge_consecutive_errors") or 0),
    }


def runtime_observability_summary(
    *,
    ibkr: dict[str, Any],
    tick_live: dict[str, Any],
    gex: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ibkr_lanes = ibkr_lane_observability(ibkr)
    tick = tick_live_observability(tick_live)
    severities = {str(ibkr_lanes.get("severity") or "ok"), str(tick.get("severity") or "ok")}
    if "error" in severities or "bad" in severities:
        severity = "error"
    elif "warn" in severities:
        severity = "warn"
    else:
        severity = "ok"
    gex_reliability = gex.get("reliability_by_identity") if isinstance(gex, dict) else {}
    gex_reliability = gex_reliability if isinstance(gex_reliability, dict) else {}
    degraded_routes = sorted(
        identity_key
        for identity_key, item in gex_reliability.items()
        if isinstance(identity_key, str)
        and isinstance(item, dict)
        and str(item.get("severity") or "") == "degraded"
    )
    optional_lane_issues = set(ibkr_lanes.get("optional_error_lanes", []))
    if float(ibkr_lanes.get("optional_max_pending_seconds") or 0.0) >= 30.0:
        optional_lane_issues.update(ibkr_lanes.get("optional_pending_lanes", []))
    optional_lane_issues = sorted(optional_lane_issues)
    return {
        "severity": severity,
        "ibkr_lanes": ibkr_lanes,
        "tick_live": tick,
        "optional_data": {
            "severity": "degraded" if degraded_routes or optional_lane_issues else "ok",
            "gex_degraded_routes": degraded_routes,
            "lane_issues": optional_lane_issues,
        },
    }


def gex_reliability_metrics(gex_status: dict[str, Any]) -> dict[str, Any]:
    diagnostics = gex_status.get("last_diagnostics") if isinstance(gex_status, dict) else {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    open_interest_rows = int(diagnostics.get("diag_open_interest_rows") or 0)
    unavailable_rows = int(diagnostics.get("diag_open_interest_unavailable_rows") or 0)
    option_rows = int(diagnostics.get("diag_option_rows") or open_interest_rows or 0)

    def _ratio(rows: int, total: int) -> float | None:
        if total <= 0:
            return None
        return round(max(0.0, min(float(rows) / float(total), 1.0)), 4)

    last_sync_raw = str(gex_status.get("last_sync_at") or "")
    last_sync = parse_aware_utc_ts(last_sync_raw) if last_sync_raw else None
    if last_sync is None:
        freshness = {"status": "missing", "age_seconds": None}
    else:
        age_seconds = max((datetime.now(tz=UTC) - last_sync).total_seconds(), 0.0)
        freshness = {
            "status": "fresh" if age_seconds <= 180.0 else "stale",
            "age_seconds": round(age_seconds, 1),
            "captured_at": last_sync.isoformat(),
        }
    errors = gex_status.get("errors") if isinstance(gex_status.get("errors"), dict) else {}
    backoff = (
        gex_status.get("failure_backoff")
        if isinstance(gex_status.get("failure_backoff"), dict)
        else {}
    )
    degraded = freshness["status"] != "fresh" and (bool(errors) or bool(backoff.get("active")))
    return {
        "optional": True,
        "severity": "degraded"
        if degraded
        else "ok"
        if freshness["status"] == "fresh"
        else "unavailable",
        "reason": (
            "asset_backoff"
            if backoff.get("active")
            else "asset_error"
            if errors
            else str(freshness["status"])
        ),
        "open_interest_rows": open_interest_rows,
        "open_interest_available_share": _ratio(open_interest_rows, option_rows),
        "open_interest_unavailable_rows": unavailable_rows,
        "open_interest_unavailable_share": _ratio(unavailable_rows, option_rows),
        "freshness": freshness,
        "error_sources": sorted(str(source) for source in errors),
        "backoff": dict(backoff),
    }
