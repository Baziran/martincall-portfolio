from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from aef_terminal.settings_contract import BACKEND_RESTART_SETTING_KEY
from aef_terminal.runtime.telemetry import exception_message

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendRestartDeps:
    store_factory: Callable[[], Any]
    ibkr_status: Callable[[], dict[str, Any]]
    gex_status: Callable[[], dict[str, Any]]
    tick_live_status: Callable[[], dict[str, Any]]
    quote_client_count: Callable[[], int]
    chart_connection_summary: Callable[[], tuple[int, int]]
    app_runtime_status: Callable[[], dict[str, Any]]
    flush_log_handlers: Callable[[], None]


_DEPS: BackendRestartDeps | None = None
_BACKEND_RESTART_LOCK = threading.RLock()
_BACKEND_RESTART_UNSET = object()
_BACKEND_RESTART_LAST: object = _BACKEND_RESTART_UNSET


def configure_backend_restart_deps(deps: BackendRestartDeps) -> None:
    global _DEPS, _BACKEND_RESTART_LAST
    _DEPS = deps
    with _BACKEND_RESTART_LOCK:
        _BACKEND_RESTART_LAST = _BACKEND_RESTART_UNSET


def _deps() -> BackendRestartDeps:
    if _DEPS is None:
        raise RuntimeError("backend restart dependencies are not configured")
    return _DEPS


def _flush_log_handlers() -> None:
    _deps().flush_log_handlers()


def _app_runtime_status() -> dict[str, Any]:
    return _deps().app_runtime_status()


def _store_factory() -> Any:
    return _deps().store_factory()


def _ibkr_status() -> dict[str, Any]:
    return _deps().ibkr_status()


def _gex_status() -> dict[str, Any]:
    return _deps().gex_status()


def _quote_client_count() -> int:
    return _deps().quote_client_count()


def _chart_connection_summary() -> tuple[int, int]:
    return _deps().chart_connection_summary()


def backend_restart_status() -> dict[str, Any]:
    global _BACKEND_RESTART_LAST
    with _BACKEND_RESTART_LOCK:
        if _BACKEND_RESTART_LAST is not _BACKEND_RESTART_UNSET:
            return {
                "last": deepcopy(_BACKEND_RESTART_LAST),
                "storage": "server-settings",
            }
        store = _store_factory()
        if store is None:
            return {"last": None, "storage": "not_configured"}
        try:
            value = store.read_setting("server", BACKEND_RESTART_SETTING_KEY)
        except Exception as exc:
            return {"last": None, "storage": "error", "message": exception_message(exc)}
        _BACKEND_RESTART_LAST = deepcopy(value) if isinstance(value, dict) else None
        return {
            "last": deepcopy(_BACKEND_RESTART_LAST),
            "storage": "server-settings",
        }


def _safe_tick_live_status_for_restart() -> dict[str, Any]:
    try:
        status = _deps().tick_live_status()
    except Exception as exc:
        return {"error": exception_message(exc)}
    return {
        "running": bool(status.get("running")),
        "enabled": bool(status.get("enabled")),
        "display_keys": status.get("display_keys") or [],
        "last_error": str(status.get("last_error") or ""),
    }


def backend_restart_record(
    request: Request | None, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    client = getattr(request, "client", None)
    headers = getattr(request, "headers", {}) or {}
    chart_clients, chart_streams = _chart_connection_summary()
    try:
        ibkr = _ibkr_status()
        ibkr_summary = {
            "ok": bool(ibkr.get("ok")),
            "live_ok": bool(ibkr.get("live_ok")),
            "quote_connected": bool(ibkr.get("quote_connected")),
            "chart_connected": bool(ibkr.get("chart_connected")),
            "quote_subscriptions": int(ibkr.get("quote_subscriptions") or 0),
            "last_api_error": str(ibkr.get("last_api_error") or ""),
        }
    except Exception as exc:
        ibkr_summary = {"error": exception_message(exc)}
    try:
        gex = _gex_status()
        gex_summary = {
            "connected": bool(gex.get("connected")),
            "lock_busy": bool(gex.get("lock_busy")),
            "queue_pending": int(gex.get("queue_pending") or 0),
            "queue_running": bool(gex.get("queue_running")),
            "last_error": str(gex.get("last_error") or ""),
        }
    except Exception as exc:
        gex_summary = {"error": exception_message(exc)}
    return {
        "requested_at": datetime.now(tz=UTC).isoformat(),
        "reason": str(payload.get("reason") or "manual").strip()[:240] or "manual",
        "source": str(payload.get("source") or "api").strip()[:80] or "api",
        "caller": {
            "host": str(getattr(client, "host", "") or ""),
            "port": getattr(client, "port", None),
            "user_agent": str(headers.get("user-agent") or "")[:240],
        },
        "app": _app_runtime_status(),
        "connections": {
            "quote_clients": _quote_client_count(),
            "chart_clients": chart_clients,
            "chart_streams": chart_streams,
        },
        "tick_live": _safe_tick_live_status_for_restart(),
        "ibkr": ibkr_summary,
        "gex": gex_summary,
    }


def record_backend_restart_request(
    request: Request | None, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    global _BACKEND_RESTART_LAST
    record = backend_restart_record(request, payload)
    store = _store_factory()
    if store is None:
        record["persisted"] = False
        record["persist_error"] = "storage not configured"
    else:
        try:
            durable_record = deepcopy(record)
            with _BACKEND_RESTART_LOCK:
                store.upsert_setting("server", BACKEND_RESTART_SETTING_KEY, durable_record)
                _BACKEND_RESTART_LAST = durable_record
                record["persisted"] = True
        except Exception as exc:
            record["persisted"] = False
            record["persist_error"] = exception_message(exc)
    _LOGGER.warning(
        "BACKEND_RESTART_REQUESTED reason=%s source=%s caller=%s uptime=%.3f persisted=%s",
        record.get("reason"),
        record.get("source"),
        (record.get("caller") or {}).get("host"),
        (record.get("app") or {}).get("uptime_seconds") or 0.0,
        record.get("persisted"),
    )
    _flush_log_handlers()
    return record
