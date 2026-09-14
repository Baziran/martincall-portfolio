from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.runtime.chart_commits import ChartCommitShutdownOutcome
from aef_terminal.ui.services.background_tasks import (
    BackgroundTaskRegistry,
)

BACKGROUND_TASKS = BackgroundTaskRegistry()
_LOGGER = logging.getLogger("aef_terminal.ui.bootstrap")
BACKGROUND_LOOP_NAMES = (
    "server_alert",
    "trading_hours",
    "gex_scheduler",
    "option_target_reprice",
    "host_sleep_monitor",
    "market_analysis_worker",
    "research_capture",
    "quote_orchestrator",
    "snapshot_retention",
    "ibkr_self_heal",
    "tick_live_restore",
)


@dataclass(frozen=True)
class BootstrapDeps:
    start_market_analysis_process_runtime: Callable[[], Awaitable[None]]
    start_chart_commit_runtime: Callable[[], None]
    start_request_task_owners: Callable[[], None]
    stop_request_task_owners: Callable[[], Awaitable[None]]
    stop_chart_commit_runtime: Callable[
        [],
        Awaitable[ChartCommitShutdownOutcome],
    ]
    server_alert_monitor_loop: Callable[[], Awaitable[None]]
    trading_hours_loop: Callable[[], Awaitable[None]]
    gex_scheduler_loop: Callable[[], Awaitable[None]]
    option_target_reprice_loop: Callable[[], Awaitable[None]]
    host_sleep_monitor_loop: Callable[[], Awaitable[None]]
    market_analysis_worker_loop: Callable[[], Awaitable[None]]
    research_capture_loop: Callable[[], Awaitable[None]]
    stop_market_analysis_runtime: Callable[[], Awaitable[None]]
    quote_orchestrator_loop: Callable[[], Awaitable[None]]
    snapshot_retention_loop: Callable[[], Awaitable[None]]
    ibkr_self_heal_loop: Callable[[], Awaitable[None]]
    tick_live_restore_loop: Callable[[], Awaitable[None]]
    stop_tick_live: Callable[[], Awaitable[dict]]
    ensure_paper_telegram_worker: Callable[[], Awaitable[None]]
    ensure_telegram_interactive_bot: Callable[[], Awaitable[dict[str, Any]]]
    start_indicator_services: Callable[[BackgroundTaskRegistry], None]
    stop_indicator_services: Callable[
        [BackgroundTaskRegistry],
        Awaitable[None],
    ]
    stop_telegram_interactive_bot: Callable[[], Awaitable[dict[str, Any]]]
    stop_paper_telegram_worker: Callable[[], Awaitable[dict[str, Any]]]


_DEPS: BootstrapDeps | None = None


def configure_bootstrap_deps(deps: BootstrapDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> BootstrapDeps:
    if _DEPS is None:
        raise RuntimeError("bootstrap dependencies are not configured")
    return _DEPS


async def start_server_alert_monitor() -> None:
    deps = _deps()
    deps.start_request_task_owners()
    await deps.start_market_analysis_process_runtime()
    deps.start_chart_commit_runtime()
    BACKGROUND_TASKS.ensure(
        "server_alert",
        deps.server_alert_monitor_loop,
        restart_delay_seconds=1.0,
        restart_window_seconds=60.0,
        max_restarts_per_window=5,
    )
    BACKGROUND_TASKS.ensure(
        "trading_hours",
        deps.trading_hours_loop,
        restart_delay_seconds=1.0,
        restart_window_seconds=60.0,
        max_restarts_per_window=5,
    )
    BACKGROUND_TASKS.ensure(
        "gex_scheduler",
        deps.gex_scheduler_loop,
        restart_delay_seconds=2.0,
        restart_window_seconds=120.0,
        max_restarts_per_window=4,
    )
    BACKGROUND_TASKS.ensure(
        "option_target_reprice",
        deps.option_target_reprice_loop,
        restart_delay_seconds=2.0,
        restart_window_seconds=120.0,
        max_restarts_per_window=4,
    )
    BACKGROUND_TASKS.ensure(
        "host_sleep_monitor",
        deps.host_sleep_monitor_loop,
        restart_delay_seconds=1.0,
        restart_window_seconds=60.0,
        max_restarts_per_window=5,
    )
    BACKGROUND_TASKS.ensure(
        "market_analysis_worker",
        deps.market_analysis_worker_loop,
        restart_delay_seconds=1.0,
        restart_window_seconds=60.0,
        max_restarts_per_window=5,
    )
    BACKGROUND_TASKS.ensure(
        "research_capture",
        deps.research_capture_loop,
        restart_delay_seconds=2.0,
        restart_window_seconds=120.0,
        max_restarts_per_window=4,
    )
    BACKGROUND_TASKS.ensure(
        "quote_orchestrator",
        deps.quote_orchestrator_loop,
        restart_delay_seconds=1.0,
        restart_window_seconds=60.0,
        max_restarts_per_window=5,
    )
    BACKGROUND_TASKS.ensure(
        "snapshot_retention",
        deps.snapshot_retention_loop,
        restart_delay_seconds=5.0,
        restart_window_seconds=300.0,
        max_restarts_per_window=3,
    )
    BACKGROUND_TASKS.ensure(
        "ibkr_self_heal",
        deps.ibkr_self_heal_loop,
        restart_delay_seconds=2.0,
        restart_window_seconds=300.0,
        max_restarts_per_window=3,
    )
    BACKGROUND_TASKS.ensure(
        "tick_live_restore",
        deps.tick_live_restore_loop,
        restart_delay_seconds=1.0,
        restart_window_seconds=60.0,
        max_restarts_per_window=5,
    )
    deps.start_indicator_services(BACKGROUND_TASKS)
    await deps.ensure_paper_telegram_worker()
    await deps.ensure_telegram_interactive_bot()


async def stop_server_alert_monitor() -> None:
    await BACKGROUND_TASKS.cancel_all(list(BACKGROUND_LOOP_NAMES))
    deps = _deps()
    try:
        await deps.stop_request_task_owners()
    finally:
        try:
            tick_outcome = await deps.stop_tick_live()
            if (
                not isinstance(tick_outcome, dict)
                or tick_outcome.get("enabled") is True
                or tick_outcome.get("running") is True
            ):
                _LOGGER.critical(
                    "Tick live shutdown did not drain: outcome=%r",
                    tick_outcome,
                )
                raise RuntimeError("TICK_LIVE_SHUTDOWN_UNDRAINED")
        finally:
            try:
                await deps.stop_market_analysis_runtime()
            finally:
                try:
                    await deps.stop_indicator_services(BACKGROUND_TASKS)
                finally:
                    try:
                        outcome = await deps.stop_chart_commit_runtime()
                        if outcome.status != "drained":
                            _LOGGER.critical(
                                "Chart commit shutdown did not drain: "
                                "status=%s pending_bars=%s pending_lanes=%s "
                                "task_errors=%s",
                                outcome.status,
                                outcome.pending_bars,
                                outcome.pending_lanes,
                                outcome.task_errors,
                            )
                            raise RuntimeError(
                                "CHART_COMMIT_SHUTDOWN_UNDRAINED "
                                f"status={outcome.status} "
                                f"pending_bars={outcome.pending_bars} "
                                f"pending_lanes={outcome.pending_lanes}"
                            )
                    finally:
                        try:
                            interactive_outcome = await deps.stop_telegram_interactive_bot()
                            if (
                                not isinstance(interactive_outcome, dict)
                                or interactive_outcome.get("drained") is not True
                            ):
                                _LOGGER.critical(
                                    "Telegram interactive shutdown did not drain: outcome=%r",
                                    interactive_outcome,
                                )
                                raise RuntimeError("TELEGRAM_INTERACTIVE_SHUTDOWN_UNDRAINED")
                        finally:
                            paper_outcome = await deps.stop_paper_telegram_worker()
                            if (
                                not isinstance(paper_outcome, dict)
                                or paper_outcome.get("drained") is not True
                            ):
                                _LOGGER.critical(
                                    "Paper Telegram shutdown did not drain: outcome=%r",
                                    paper_outcome,
                                )
                                raise RuntimeError("PAPER_TELEGRAM_SHUTDOWN_UNDRAINED")
