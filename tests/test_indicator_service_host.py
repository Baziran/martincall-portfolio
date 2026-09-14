from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.ui import bootstrap, indicator_service_host
from aef_terminal.ui.indicator_service_host import (
    IndicatorServiceContext,
    IndicatorServiceContribution,
    IndicatorServiceTask,
)


class _TaskRegistry:
    def __init__(self) -> None:
        self.ensured: list[tuple[str, Any, dict[str, Any]]] = []
        self.cancelled: list[list[str]] = []

    def ensure(
        self,
        name: str,
        factory,
        **policy: Any,
    ) -> None:
        self.ensured.append((name, factory, policy))

    async def cancel_all(
        self,
        names: list[str] | None = None,
    ) -> None:
        self.cancelled.append(list(names or []))


def _context() -> IndicatorServiceContext:
    return IndicatorServiceContext(
        logger=logging.getLogger("indicator-service-test"),
        store_factory=lambda: None,
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
    )


@pytest.fixture(autouse=True)
def _reset_indicator_service_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indicator_service_host,
        "_CONTRIBUTIONS",
        {},
    )
    monkeypatch.setattr(
        indicator_service_host,
        "_ACTIVE_TASK_NAMES",
        (),
    )


def test_indicator_service_host_starts_and_stops_discovered_contributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_calls: list[str] = []

    async def loop() -> None:
        return None

    async def shutdown() -> None:
        shutdown_calls.append("shutdown")

    contribution = IndicatorServiceContribution(
        tasks=(
            IndicatorServiceTask(
                name="indicator:test:restore",
                factory=loop,
                restart_delay_seconds=2.0,
                restart_window_seconds=30.0,
                max_restarts_per_window=4,
            ),
        ),
        shutdown=shutdown,
    )
    module = SimpleNamespace(id="test", service_ref="test:service")
    monkeypatch.setattr(
        indicator_service_host,
        "indicator_modules",
        lambda: (module,),
    )
    monkeypatch.setattr(
        indicator_service_host,
        "resolve_ref",
        lambda _ref: lambda _context: contribution,
    )

    indicator_service_host.configure_indicator_services(_context())
    registry = _TaskRegistry()
    indicator_service_host.start_indicator_service_tasks(registry)

    assert registry.ensured == [
        (
            "indicator:test:restore",
            loop,
            {
                "restart_delay_seconds": 2.0,
                "restart_window_seconds": 30.0,
                "max_restarts_per_window": 4,
            },
        )
    ]

    asyncio.run(indicator_service_host.stop_indicator_services(registry))

    assert registry.cancelled == [["indicator:test:restore"]]
    assert shutdown_calls == ["shutdown"]


def test_indicator_service_host_is_empty_when_package_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indicator_service_host,
        "indicator_modules",
        lambda: (),
    )

    indicator_service_host.configure_indicator_services(_context())
    registry = _TaskRegistry()
    indicator_service_host.start_indicator_service_tasks(registry)
    asyncio.run(indicator_service_host.stop_indicator_services(registry))

    assert registry.ensured == []
    assert registry.cancelled == [[]]


def test_indicator_service_shutdown_failure_isolated_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    shutdown_calls: list[str] = []

    async def failing_shutdown() -> None:
        shutdown_calls.append("failing")
        raise RuntimeError("broken package shutdown")

    async def healthy_shutdown() -> None:
        shutdown_calls.append("healthy")

    modules = (
        SimpleNamespace(id="failing", service_ref="test:failing"),
        SimpleNamespace(id="healthy", service_ref="test:healthy"),
    )
    contributions = {
        "test:failing": IndicatorServiceContribution(
            shutdown=failing_shutdown,
        ),
        "test:healthy": IndicatorServiceContribution(
            shutdown=healthy_shutdown,
        ),
    }
    monkeypatch.setattr(
        indicator_service_host,
        "indicator_modules",
        lambda: modules,
    )
    monkeypatch.setattr(
        indicator_service_host,
        "resolve_ref",
        lambda ref: lambda _context: contributions[ref],
    )

    indicator_service_host.configure_indicator_services(_context())
    registry = _TaskRegistry()
    with caplog.at_level(
        logging.WARNING,
        logger="indicator-service-test",
    ):
        asyncio.run(indicator_service_host.stop_indicator_services(registry))

    assert shutdown_calls == ["healthy", "failing"]
    assert "Indicator service shutdown failed: failing" in caplog.text


def test_terminal_workers_stop_even_if_indicator_service_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def cancel_all(_names: list[str]) -> None:
        calls.append("background")

    async def failing_indicator_stop(_registry: Any) -> None:
        calls.append("indicator")
        raise RuntimeError("indicator cleanup failed")

    async def stop_market_analysis() -> None:
        calls.append("market")

    async def stop_tick_live() -> dict[str, object]:
        calls.append("tick")
        return {"enabled": False, "running": False, "status": "off"}

    async def stop_chart_commits() -> object:
        calls.append("chart_commits")
        return SimpleNamespace(status="drained")

    async def stop_interactive() -> dict[str, object]:
        calls.append("telegram")
        return {"drained": True}

    async def stop_paper() -> dict[str, object]:
        calls.append("paper")
        return {"drained": True}

    monkeypatch.setattr(
        bootstrap.BACKGROUND_TASKS,
        "cancel_all",
        cancel_all,
    )
    monkeypatch.setattr(
        bootstrap,
        "_DEPS",
        SimpleNamespace(
            stop_request_task_owners=lambda: asyncio.sleep(0),
            stop_tick_live=stop_tick_live,
            stop_market_analysis_runtime=stop_market_analysis,
            stop_indicator_services=failing_indicator_stop,
            stop_chart_commit_runtime=stop_chart_commits,
            stop_telegram_interactive_bot=stop_interactive,
            stop_paper_telegram_worker=stop_paper,
        ),
    )

    with pytest.raises(RuntimeError, match="indicator cleanup failed"):
        asyncio.run(bootstrap.stop_server_alert_monitor())

    assert calls == [
        "background",
        "tick",
        "market",
        "indicator",
        "chart_commits",
        "telegram",
        "paper",
    ]


def test_chart_commit_shutdown_pending_state_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    async def cancel_all(_names: list[str]) -> None:
        calls.append("background")

    async def stop_market_analysis() -> None:
        calls.append("market")

    async def stop_tick_live() -> dict[str, object]:
        calls.append("tick")
        return {"enabled": False, "running": False, "status": "off"}

    async def stop_indicators(_registry: Any) -> None:
        calls.append("indicator")

    async def stop_chart_commits() -> object:
        calls.append("chart_commits")
        return SimpleNamespace(
            status="pending_rearm",
            pending_bars=2,
            pending_lanes=1,
            task_errors=(),
        )

    async def stop_interactive() -> dict[str, object]:
        calls.append("telegram")
        return {"drained": True}

    async def stop_paper() -> dict[str, object]:
        calls.append("paper")
        return {"drained": True}

    monkeypatch.setattr(
        bootstrap.BACKGROUND_TASKS,
        "cancel_all",
        cancel_all,
    )
    monkeypatch.setattr(
        bootstrap,
        "_DEPS",
        SimpleNamespace(
            stop_request_task_owners=lambda: asyncio.sleep(0),
            stop_tick_live=stop_tick_live,
            stop_market_analysis_runtime=stop_market_analysis,
            stop_indicator_services=stop_indicators,
            stop_chart_commit_runtime=stop_chart_commits,
            stop_telegram_interactive_bot=stop_interactive,
            stop_paper_telegram_worker=stop_paper,
        ),
    )

    with caplog.at_level(
        logging.CRITICAL,
        logger="aef_terminal.ui.bootstrap",
    ):
        with pytest.raises(
            RuntimeError,
            match="CHART_COMMIT_SHUTDOWN_UNDRAINED",
        ):
            asyncio.run(bootstrap.stop_server_alert_monitor())

    assert calls == [
        "background",
        "tick",
        "market",
        "indicator",
        "chart_commits",
        "telegram",
        "paper",
    ]
    assert "status=pending_rearm pending_bars=2" in caplog.text


def test_tick_live_undrained_shutdown_fails_closed_and_other_owners_still_stop(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    async def cancel_all(_names: list[str]) -> None:
        calls.append("background")

    async def stop_tick_live() -> dict[str, object]:
        calls.append("tick")
        return {"enabled": True, "running": True, "status": "live"}

    async def stop_market_analysis() -> None:
        calls.append("market")

    async def stop_indicators(_registry: Any) -> None:
        calls.append("indicator")

    async def stop_chart_commits() -> object:
        calls.append("chart_commits")
        return SimpleNamespace(status="drained")

    async def stop_interactive() -> dict[str, object]:
        calls.append("telegram")
        return {"drained": True}

    async def stop_paper() -> dict[str, object]:
        calls.append("paper")
        return {"drained": True}

    monkeypatch.setattr(bootstrap.BACKGROUND_TASKS, "cancel_all", cancel_all)
    monkeypatch.setattr(
        bootstrap,
        "_DEPS",
        SimpleNamespace(
            stop_request_task_owners=lambda: asyncio.sleep(0),
            stop_tick_live=stop_tick_live,
            stop_market_analysis_runtime=stop_market_analysis,
            stop_indicator_services=stop_indicators,
            stop_chart_commit_runtime=stop_chart_commits,
            stop_telegram_interactive_bot=stop_interactive,
            stop_paper_telegram_worker=stop_paper,
        ),
    )

    with caplog.at_level(logging.CRITICAL, logger="aef_terminal.ui.bootstrap"):
        with pytest.raises(RuntimeError, match="TICK_LIVE_SHUTDOWN_UNDRAINED"):
            asyncio.run(bootstrap.stop_server_alert_monitor())

    assert calls == [
        "background",
        "tick",
        "market",
        "indicator",
        "chart_commits",
        "telegram",
        "paper",
    ]
    assert "Tick live shutdown did not drain" in caplog.text


def test_undrained_interactive_bot_fails_closed_after_all_thread_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def cancel_all(_names: list[str]) -> None:
        calls.append("background")

    async def stop_tick_live() -> dict[str, object]:
        calls.append("tick")
        return {"enabled": False, "running": False}

    async def stop_market_analysis() -> None:
        calls.append("market")

    async def stop_indicators(_registry: Any) -> None:
        calls.append("indicator")

    async def stop_chart_commits() -> object:
        calls.append("chart_commits")
        return SimpleNamespace(status="drained")

    async def stop_interactive() -> dict[str, object]:
        calls.append("telegram")
        return {"drained": False, "thread_alive": True}

    async def stop_paper() -> dict[str, object]:
        calls.append("paper")
        return {"drained": True}

    monkeypatch.setattr(bootstrap.BACKGROUND_TASKS, "cancel_all", cancel_all)
    monkeypatch.setattr(
        bootstrap,
        "_DEPS",
        SimpleNamespace(
            stop_request_task_owners=lambda: asyncio.sleep(0),
            stop_tick_live=stop_tick_live,
            stop_market_analysis_runtime=stop_market_analysis,
            stop_indicator_services=stop_indicators,
            stop_chart_commit_runtime=stop_chart_commits,
            stop_telegram_interactive_bot=stop_interactive,
            stop_paper_telegram_worker=stop_paper,
        ),
    )

    with pytest.raises(RuntimeError, match="TELEGRAM_INTERACTIVE_SHUTDOWN_UNDRAINED"):
        asyncio.run(bootstrap.stop_server_alert_monitor())

    assert calls == [
        "background",
        "tick",
        "market",
        "indicator",
        "chart_commits",
        "telegram",
        "paper",
    ]


def test_indicator_service_host_rejects_duplicate_task_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def loop() -> None:
        return None

    modules = (
        SimpleNamespace(id="first", service_ref="test:first"),
        SimpleNamespace(id="second", service_ref="test:second"),
    )
    contribution = IndicatorServiceContribution(
        tasks=(
            IndicatorServiceTask(
                name="indicator:duplicate",
                factory=loop,
            ),
        ),
    )
    monkeypatch.setattr(
        indicator_service_host,
        "indicator_modules",
        lambda: modules,
    )
    monkeypatch.setattr(
        indicator_service_host,
        "resolve_ref",
        lambda _ref: lambda _context: contribution,
    )

    with pytest.raises(
        ValueError,
        match="claimed by first and second",
    ):
        indicator_service_host.configure_indicator_services(_context())


@pytest.mark.parametrize(
    ("task", "error_type", "error_match"),
    (
        (
            IndicatorServiceTask(
                name=" indicator:invalid",
                factory=lambda: asyncio.sleep(0),
            ),
            ValueError,
            "task name is invalid",
        ),
        (
            IndicatorServiceTask(
                name="indicator:invalid",
                factory=None,  # type: ignore[arg-type]
            ),
            TypeError,
            "factory must be callable",
        ),
    ),
)
def test_indicator_service_host_rejects_invalid_tasks(
    monkeypatch: pytest.MonkeyPatch,
    task: IndicatorServiceTask,
    error_type: type[Exception],
    error_match: str,
) -> None:
    module = SimpleNamespace(id="invalid", service_ref="test:invalid")
    monkeypatch.setattr(
        indicator_service_host,
        "indicator_modules",
        lambda: (module,),
    )
    monkeypatch.setattr(
        indicator_service_host,
        "resolve_ref",
        lambda _ref: (
            lambda _context: IndicatorServiceContribution(
                tasks=(task,),
            )
        ),
    )

    with pytest.raises(error_type, match=error_match):
        indicator_service_host.configure_indicator_services(_context())


def test_tick_flow_package_removal_preserves_core_tick_routes_without_package_lifecycle(
    tmp_path: Path,
) -> None:
    copied_src = tmp_path / "src"
    copied_package = copied_src / "aef_terminal"
    shutil.copytree(Path("src/aef_terminal"), copied_package)
    shutil.rmtree(copied_package / "indicators" / "modules" / "tick_flow")
    script = """
from aef_terminal.indicators.registry import INDICATOR_REGISTRY
from aef_terminal.ui import app as ui_app
from aef_terminal.ui import indicator_service_host

assert "tick_flow" not in INDICATOR_REGISTRY
assert "tick_flow" not in indicator_service_host._CONTRIBUTIONS
assert any(
    str(getattr(child, "path", "")).startswith("/api/ticks")
    for route in ui_app.app.routes
    for child in getattr(getattr(route, "original_router", None), "routes", ())
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(copied_src),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
