from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from threading import Event
from typing import Any

import pytest
from fastapi import HTTPException

from aef_terminal.ui.routers.system import SystemRouterDeps, create_system_router
from aef_terminal.ui.system_observability import runtime_observability_summary
from aef_terminal.ui.system_status import (
    SystemStatusDeps,
    SystemStatusService,
    health_status_snapshot,
    readiness_status_snapshot,
)


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _system_deps(**overrides: Any) -> SystemRouterDeps:
    defaults = {
        "health_status": lambda: {"status": "ok"},
        "readiness_status": lambda: {
            "status": "ok",
            "storage": {"configured": True, "ok": True},
        },
        "system_status": lambda **_kwargs: {"status": "ok", "sleep": {"sleeping": False}},
        "memory_status": lambda **_kwargs: {"ok": True, "caches": {}},
        "set_server_sleeping": lambda sleeping, _reason: {"sleeping": sleeping},
        "disconnect_ibkr_sessions": lambda **_kwargs: {"history": {"ok": True}},
        "ibkr_status": lambda: {"connected": True},
        "apply_ibkr_runtime_settings": lambda: {},
        "backend_restart_record": lambda _request, _payload: {
            "requested_at": "2026-01-01T00:00:00+00:00"
        },
        "flush_log_handlers": lambda: None,
        "now_iso": lambda: "2026-01-01T00:00:00+00:00",
    }
    defaults.update(overrides)
    return SystemRouterDeps(**defaults)


def test_system_health_endpoint() -> None:
    endpoint = _route_endpoint(create_system_router(_system_deps()), "/api/health", "GET")
    payload = endpoint()
    assert payload["status"] == "ok"


def test_system_health_is_liveness_only() -> None:
    assert health_status_snapshot() == {"status": "ok"}


def test_system_readiness_returns_503_when_storage_is_not_ready() -> None:
    endpoint = _route_endpoint(
        create_system_router(
            _system_deps(
                readiness_status=lambda: {
                    "status": "not_ready",
                    "storage": {"configured": True, "ok": False},
                }
            )
        ),
        "/api/ready",
        "GET",
    )

    response = endpoint()

    assert response.status_code == 503
    assert json.loads(response.body)["storage"]["ok"] is False


def test_readiness_snapshot_fails_closed_on_storage_error() -> None:
    def unavailable_store():
        raise RuntimeError("database unavailable")

    payload = readiness_status_snapshot(store_factory=unavailable_store)

    assert payload["status"] == "not_ready"
    assert payload["storage"]["ok"] is False
    assert payload["storage"]["message"] == "database unavailable"


def test_readiness_snapshot_rejects_truthy_non_boolean_status() -> None:
    class InvalidStatusStore:
        @staticmethod
        def readiness() -> dict[str, Any]:
            return {"configured": True, "ok": "false"}

    payload = readiness_status_snapshot(store_factory=InvalidStatusStore)

    assert payload["status"] == "not_ready"


def test_readiness_snapshot_uses_lightweight_storage_contract() -> None:
    class Store:
        @staticmethod
        def readiness() -> dict[str, Any]:
            return {
                "configured": True,
                "ok": True,
                "canonical_writer": {"ok": True, "state": "held"},
                "message": "PostgreSQL ready",
            }

        @staticmethod
        def status() -> dict[str, Any]:
            raise AssertionError("readiness must not run storage diagnostics")

    payload = readiness_status_snapshot(store_factory=Store)

    assert payload["status"] == "ok"
    assert payload["storage"]["canonical_writer"]["state"] == "held"
    assert "bars" not in payload["storage"]


def test_system_wake_endpoint() -> None:
    endpoint = _route_endpoint(create_system_router(_system_deps()), "/api/system/wake", "POST")
    payload = endpoint({"reason": "manual wake"})
    assert payload["ok"] is True
    assert payload["sleep"]["sleeping"] is False


def test_system_sleep_reports_disconnect_error() -> None:
    def disconnect_fail(**_kwargs):
        raise RuntimeError("gateway timeout")

    endpoint = _route_endpoint(
        create_system_router(_system_deps(disconnect_ibkr_sessions=disconnect_fail)),
        "/api/system/sleep",
        "POST",
    )
    payload = endpoint({"reason": "manual sleep"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "SYSTEM_SLEEP_DISCONNECT_FAILED"
    assert payload["error"]["retryable"] is True
    assert payload["sleep"]["sleeping"] is True


def test_system_status_raises_structured_http_error() -> None:
    async def run() -> None:
        def boom() -> dict[str, Any]:
            raise RuntimeError("status unavailable")

        endpoint = _route_endpoint(
            create_system_router(_system_deps(system_status=boom)),
            "/api/system",
            "GET",
        )
        with pytest.raises(HTTPException) as raised:
            await endpoint()
        detail = raised.value.detail
        assert detail["ok"] is False
        assert detail["error"]["code"] == "SYSTEM_STATUS_ERROR"
        assert detail["error"]["retryable"] is True

    asyncio.run(run())


def test_system_status_exposes_one_nested_gex_scheduler_contract() -> None:
    scheduler = {"enabled": True, "symbols": "ES,SPY,QQQ", "source": "server"}
    maintenance = {
        "enabled": True,
        "intervals": ["1m", "5m", "15m", "60m"],
        "last_cycle": {"closed_routes": 2, "scheduled_repairs": 1},
    }

    class Store:
        @staticmethod
        def canonical_writer_lease_status(*, verify: bool) -> dict[str, Any]:
            assert verify is True
            return {"ok": True, "state": "held"}

        @staticmethod
        def diagnostics() -> dict[str, Any]:
            return {"configured": True, "ok": True}

    service = SystemStatusService(
        SystemStatusDeps(
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            build={"version": "test", "git_sha": "abc1234", "built_at": ""},
            apply_runtime_settings=lambda: {},
            store_factory=Store,
            ibkr_status=lambda: {"ok": True},
            ibkr_gateway_login_control_status=lambda: {"configured": True},
            ibkr_self_heal_status=lambda: {"blocked": False},
            tick_live_status=lambda: {},
            chart_stream_status=lambda: {},
            server_sleep_status=lambda: {"sleeping": False},
            host_sleep_status=lambda: {},
            gex_status=lambda: {"ok": True},
            gex_scheduler_settings=lambda: scheduler,
            quote_snapshot_stats=lambda: {},
            option_target_caps_settings=lambda: {},
            telegram_config_status=lambda: {},
            telegram_interactive_status=lambda: {},
            paper_telegram_feed_status=lambda: {},
            backend_restart_status=lambda: {},
            closed_session_maintenance_status=lambda: maintenance,
            quote_stream_status=lambda: {"clients": 1},
            research_capture_status=lambda: {"enabled": True, "active_leases": [{}]},
        )
    )

    snapshot = service.snapshot()
    gex = snapshot["gex"]

    assert gex["scheduler"] == scheduler
    assert snapshot["closed_session_maintenance"] == maintenance
    assert snapshot["quote_stream"] == {"clients": 1}
    assert snapshot["research_capture"] == {"enabled": True, "active_leases": [{}]}
    assert snapshot["ibkr"]["gateway_login_control"] == {"configured": True}
    assert "scheduler_enabled" not in gex
    assert "scheduler_symbols" not in gex


def test_system_memory_endpoint() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_system_router(_system_deps()), "/api/system/memory", "GET"
        )
        payload = await endpoint(include_objects=True)
        assert payload["ok"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    ("path", "dependency", "arguments"),
    (
        ("/api/system", "system_status", {"details": True}),
        ("/api/system/memory", "memory_status", {"include_objects": True}),
    ),
)
def test_system_diagnostics_drain_cancelled_physical_calls(
    path: str,
    dependency: str,
    arguments: dict[str, bool],
) -> None:
    started = Event()
    release = Event()

    def blocking_status(**_kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=2.0)
        return {"ok": True}

    endpoint = _route_endpoint(
        create_system_router(_system_deps(**{dependency: blocking_status})),
        path,
        "GET",
    )

    async def run() -> None:
        task = asyncio.create_task(endpoint(**arguments))
        while not started.is_set():
            await asyncio.sleep(0)

        task.cancel()
        await asyncio.sleep(0)

        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_system_restart_terminates_after_response_background_task(monkeypatch) -> None:
    events: list[Any] = []

    def flush_logs() -> None:
        events.append("flush")

    def fake_kill(pid: int, sig: int) -> None:
        events.append(("kill", pid, sig))

    monkeypatch.setattr("aef_terminal.ui.routers.system.os.getpid", lambda: 12345)
    monkeypatch.setattr("aef_terminal.ui.routers.system.os.kill", fake_kill)
    endpoint = _route_endpoint(
        create_system_router(_system_deps(flush_log_handlers=flush_logs)),
        "/api/system/restart",
        "POST",
    )

    response = endpoint(None, {"reason": "test"})
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert events == []
    assert response.background is not None
    asyncio.run(response.background())
    assert events == ["flush", ("kill", 12345, 15)]


def test_runtime_observability_summary_surfaces_tick_and_lane_degradation() -> None:
    summary = runtime_observability_summary(
        ibkr={
            "market_data_manager": {
                "lanes": {
                    "history": {
                        "pending": 1,
                        "oldest_pending_seconds": 125.0,
                        "running": False,
                        "running_seconds": None,
                    }
                }
            }
        },
        tick_live={
            "buffer": {
                "dropped": 2,
                "buffered": 10,
                "db_consecutive_errors": 30,
                "purge_consecutive_errors": 2,
                "health": {"severity": "warn", "reason": "db_errors", "buffer_ratio": 0.5},
            }
        },
    )

    assert summary["severity"] == "error"
    assert summary["ibkr_lanes"]["reason"] == "lane_queue_stalled"
    assert summary["ibkr_lanes"]["pending_lanes"] == ["history"]
    assert summary["tick_live"]["severity"] == "error"
    assert summary["tick_live"]["reason"] == "dropped_ticks"
    assert summary["tick_live"]["purge_consecutive_errors"] == 2


def test_runtime_observability_keeps_optional_gex_failure_out_of_core_warning() -> None:
    summary = runtime_observability_summary(
        ibkr={
            "market_data_manager": {
                "lanes": {
                    "chart": {
                        "pending": 0,
                        "running": False,
                    },
                    "gex": {
                        "pending": 0,
                        "running": True,
                        "running_seconds": 56.0,
                        "last_error": "qualification timeout",
                    },
                    "options": {
                        "pending": 1,
                        "oldest_pending_seconds": 41.0,
                        "running": False,
                    },
                }
            }
        },
        tick_live={"buffer": {"dropped": 0, "buffered": 0, "health": {"severity": "ok"}}},
        gex={
            "reliability_by_identity": {
                '["ibkr|future_root|ES|CME|USD|ES","ibkr:ES:route"]': {"severity": "ok"},
                '["ibkr|contract|756733","ibkr:SPY:route"]': {"severity": "ok"},
                '["ibkr|contract|320227571","ibkr:QQQ:route"]': {"severity": "degraded"},
            }
        },
    )

    assert summary["severity"] == "ok"
    assert summary["ibkr_lanes"]["severity"] == "ok"
    assert summary["ibkr_lanes"]["error_lanes"] == []
    assert summary["ibkr_lanes"]["optional_error_lanes"] == ["gex"]
    assert summary["optional_data"] == {
        "severity": "degraded",
        "gex_degraded_routes": ['["ibkr|contract|320227571","ibkr:QQQ:route"]'],
        "lane_issues": ["gex", "options"],
    }
