from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aef_terminal.runtime.metrics import runtime_metrics_snapshot
from aef_terminal.ui.system_observability import (
    gex_reliability_metrics,
    runtime_observability_summary,
)


@dataclass(frozen=True)
class SystemStatusDeps:
    started_at: datetime
    build: dict[str, str]
    apply_runtime_settings: Callable[[], dict[str, Any]]
    store_factory: Callable[[], Any]
    ibkr_status: Callable[[], dict[str, Any]]
    ibkr_self_heal_status: Callable[[], dict[str, Any]]
    tick_live_status: Callable[[], dict[str, Any]]
    chart_stream_status: Callable[[], dict[str, Any]]
    server_sleep_status: Callable[[], dict[str, Any]]
    host_sleep_status: Callable[[], dict[str, Any]]
    gex_status: Callable[[], dict[str, Any]]
    gex_scheduler_settings: Callable[[], dict[str, Any]]
    quote_snapshot_stats: Callable[[], dict[str, Any]]
    option_target_caps_settings: Callable[[], dict[str, float]]
    telegram_config_status: Callable[[], dict[str, Any]]
    telegram_interactive_status: Callable[[], dict[str, Any]]
    paper_telegram_feed_status: Callable[[], dict[str, Any]]
    backend_restart_status: Callable[[], dict[str, Any]]
    closed_session_maintenance_status: Callable[[], dict[str, Any]] = field(default=lambda: {})
    quote_stream_status: Callable[[], dict[str, Any]] = field(default=lambda: {})
    fast_indicator_status: Callable[[], dict[str, Any]] = field(default=lambda: {})
    research_capture_status: Callable[[], dict[str, Any]] = field(default=lambda: {})
    ibkr_gateway_login_control_status: Callable[[], dict[str, Any]] = field(
        default=lambda: {"configured": False}
    )


class SystemStatusService:
    def __init__(self, deps: SystemStatusDeps) -> None:
        self._deps = deps
        self._storage_diagnostics_at = 0.0
        self._storage_diagnostics: dict[str, Any] = {}
        self._storage_diagnostics_ttl_seconds = 45.0

    def app_runtime_status(self) -> dict[str, Any]:
        status = {
            "started_at": self._deps.started_at.isoformat(),
            "uptime_seconds": max(
                (datetime.now(tz=UTC) - self._deps.started_at).total_seconds(), 0.0
            ),
            "pid": os.getpid(),
            **self._deps.build,
        }
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            statm = Path("/proc/self/statm").read_text(encoding="utf-8").split()
            if len(statm) >= 2:
                status["rss_bytes"] = int(statm[1]) * int(page_size)
        except Exception:
            pass
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            for line in meminfo:
                if line.startswith("MemTotal:"):
                    status["mem_total_bytes"] = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    status["mem_available_bytes"] = int(line.split()[1]) * 1024
        except Exception:
            pass
        return status

    def storage_snapshot(self, store: Any) -> dict[str, Any]:
        if store is None:
            return {"configured": False, "ok": False}
        writer_status = store.canonical_writer_lease_status(verify=True)
        if not isinstance(writer_status, dict) or writer_status.get("ok") is not True:
            return {
                "configured": True,
                "ok": False,
                "canonical_writer": (writer_status if isinstance(writer_status, dict) else {}),
                "message": (
                    str(writer_status.get("message") or "CANONICAL_WRITER_LEASE_REQUIRED")
                    if isinstance(writer_status, dict)
                    else "CANONICAL_WRITER_LEASE_STATUS_INVALID"
                ),
            }
        now = time.monotonic()
        if (
            self._storage_diagnostics
            and now - self._storage_diagnostics_at < self._storage_diagnostics_ttl_seconds
        ):
            return dict(self._storage_diagnostics)
        payload = store.diagnostics()
        self._storage_diagnostics = payload
        self._storage_diagnostics_at = now
        return dict(payload)

    def snapshot(self, *, details: bool = True) -> dict[str, Any]:
        runtime_settings = self._deps.apply_runtime_settings()
        store = self._deps.store_factory()
        storage = (
            self.storage_snapshot(store)
            if details
            else (store.status() if store is not None else {"configured": False, "ok": False})
        )
        ibkr = dict(self._deps.ibkr_status())
        ibkr["gateway_login_control"] = self._deps.ibkr_gateway_login_control_status()
        chart_stream = self._deps.chart_stream_status()
        ibkr_self_heal = self._deps.ibkr_self_heal_status()
        tick_live = self._deps.tick_live_status()
        sleep = self._deps.server_sleep_status()
        gex = self._deps.gex_status()
        route_runtime = gex.get("routes") if isinstance(gex.get("routes"), dict) else {}
        gex["reliability_by_identity"] = {
            identity_key: gex_reliability_metrics(state)
            for identity_key, state in route_runtime.items()
            if isinstance(state, dict)
        }
        gex["scheduler"] = self._deps.gex_scheduler_settings()
        observability = runtime_observability_summary(ibkr=ibkr, tick_live=tick_live, gex=gex)
        data_hygiene = (
            storage.get("data_hygiene") if isinstance(storage.get("data_hygiene"), dict) else {}
        )
        storage_degraded = str(data_hygiene.get("severity") or "ok") in {"warn", "error"}
        return {
            "status": (
                "sleeping"
                if sleep["sleeping"]
                else "degraded"
                if observability["severity"] == "error"
                or ibkr_self_heal.get("blocked")
                or storage_degraded
                else "ok"
                if storage.get("ok") and ibkr.get("ok")
                else "degraded"
            ),
            "checked_at": datetime.now(tz=UTC).isoformat(),
            "app": self.app_runtime_status(),
            "sleep": sleep,
            "host_sleep": self._deps.host_sleep_status(),
            "storage": storage,
            "ibkr": ibkr,
            "chart_stream": chart_stream,
            "closed_session_maintenance": self._deps.closed_session_maintenance_status(),
            "ibkr_self_heal": ibkr_self_heal,
            "tick_live": tick_live,
            "runtime_observability": observability,
            "runtime_metrics": runtime_metrics_snapshot(),
            "gex": gex,
            "quote_stream": self._deps.quote_stream_status(),
            "quote_snapshots": self._deps.quote_snapshot_stats(),
            "research_capture": self._deps.research_capture_status(),
            "options": {
                "premium_caps": self._deps.option_target_caps_settings(),
                "fast_indicators": self._deps.fast_indicator_status(),
            },
            "telegram": {
                **self._deps.telegram_config_status(),
                "interactive": self._deps.telegram_interactive_status(),
                "paper_feed": self._deps.paper_telegram_feed_status(),
            },
            "restart": self._deps.backend_restart_status(),
            "runtime_settings": runtime_settings,
        }


def health_status_snapshot() -> dict[str, Any]:
    return {"status": "ok"}


def readiness_status_snapshot(*, store_factory: Callable[[], Any]) -> dict[str, Any]:
    try:
        store = store_factory()
        raw_storage = (
            store.readiness()
            if store is not None
            else {
                "configured": False,
                "ok": False,
                "message": "storage is not configured",
            }
        )
        storage = (
            dict(raw_storage)
            if isinstance(raw_storage, dict)
            else {
                "configured": True,
                "ok": False,
                "message": "storage status contract is invalid",
            }
        )
    except Exception as exc:
        storage = {
            "configured": True,
            "ok": False,
            "message": str(exc) or exc.__class__.__name__,
        }
    storage_ready = storage.get("configured") is True and storage.get("ok") is True
    return {
        "status": "ok" if storage_ready else "not_ready",
        "storage": storage,
    }
