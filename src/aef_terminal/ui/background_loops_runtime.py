from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.futures_lifecycle import resolve_and_persist_current_contract
from aef_terminal.data.gex.live import live_gex_status
from aef_terminal.data.gex.scheduler import gex_scheduler_jobs
from aef_terminal.data.providers import (
    get_provider,
    live_quote_polling_providers,
)
from aef_terminal.ui.runtime.constants import (
    QUOTE_SNAPSHOT_SECONDS,
    QUOTE_STREAM_SECONDS,
)
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision, QuoteRouteSnapshot
from aef_terminal.ui.services.closed_session_maintenance_models import (
    ClosedSessionMaintenanceSettings,
    ClosedSessionMaintenanceState,
)
from aef_terminal.ui.services.gex_scheduler import (
    GexSchedulerSettingsRuntime,
    GexSchedulerRouteState,
    run_gex_scheduler_loop,
)
from aef_terminal.ui.services.ibkr_self_heal import (
    IBKR_SELF_HEAL_POLL_SECONDS,
    IbkrSelfHealState,
    run_ibkr_self_heal_loop,
)
from aef_terminal.ui.services.option_target_reprice import (
    OPTION_TARGET_REPRICE_POLL_SECONDS,
    run_option_target_reprice_loop,
)
from aef_terminal.ui.services.fast_indicator_runtime import (
    option_target_samples_committed,
)
from aef_terminal.ui.services.quote_orchestrator import (
    QuoteOrchestratorRuntimeDeps,
    QuoteProviderPoller,
    run_quote_orchestrator_runtime,
)
from aef_terminal.ui.services.quote_snapshots import (
    persist_quote_snapshots_async,
)
from aef_terminal.ui.services.snapshot_retention import (
    run_snapshot_retention_loop,
)
from aef_terminal.ui.services.trading_hours_refresh import (
    TradingHoursRefreshDeps,
    recover_provider_trading_hours_after_connection,
    run_trading_hours_refresh_loop,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackgroundLoopDeps:
    store_factory: Callable[[], Any]
    quote_orchestrator_state: Any
    logger: logging.Logger
    server_sleeping: Callable[[], bool]
    combined_quote_instruments: Callable[[], list[dict[str, Any]]]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    store_quote_cache: Callable[[dict[tuple[str, str], Any] | None, str], None]
    refresh_quote_routes: Callable[..., Any]
    invalidate_market_analysis_route: Callable[[str, str], Awaitable[int]]
    selected_instruments: Callable[[], list[dict[str, Any]]]
    instrument_lookup: Callable[[str], dict[str, Any]]
    active_chart_streams: Callable[[], list[tuple[str, str, str, int]]]
    watchlist_gap_quality: Callable[[str, str], dict[str, Any]]
    request_watchlist_recovery_tail: Callable[[str, str], dict[str, Any]]
    set_transient_quote_wanted: Callable[[str, Any, float], None]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]
    ibkr_status: Callable[[], dict[str, Any]]
    force_ibkr_reconnect: Callable[..., Any]
    cleanup_ibkr_quarantine: Callable[..., Any]
    gex_scheduler_state: dict[tuple[str, str], GexSchedulerRouteState]
    option_target_caps_settings: Callable[[], dict[str, float]]
    parse_iso_ts: Callable[[str | None], datetime | None]


_DEPS: BackgroundLoopDeps | None = None
_GEX_SCHEDULER_SETTINGS_RUNTIME = GexSchedulerSettingsRuntime()
_IBKR_SELF_HEAL_STATE = IbkrSelfHealState()
_APP_CONFIG = AppConfig()
_CLOSED_SESSION_MAINTENANCE_SETTINGS = ClosedSessionMaintenanceSettings(
    enabled=_APP_CONFIG.closed_session_maintenance_enabled,
    history_timezone=_APP_CONFIG.closed_session_maintenance_timezone,
    history_start_local=_APP_CONFIG.closed_session_maintenance_start_local,
    history_end_local=_APP_CONFIG.closed_session_maintenance_end_local,
    active_poll_seconds=_APP_CONFIG.closed_session_maintenance_active_poll_seconds,
    idle_poll_seconds=_APP_CONFIG.closed_session_maintenance_idle_poll_seconds,
    repair_timeout_seconds=_APP_CONFIG.closed_session_maintenance_repair_timeout_seconds,
    max_provider_admissions_per_cycle=(
        _APP_CONFIG.closed_session_maintenance_max_provider_admissions
    ),
    admission_window_seconds=(_APP_CONFIG.closed_session_maintenance_admission_window_seconds),
    max_provider_admissions_per_window=(
        _APP_CONFIG.closed_session_maintenance_max_provider_admissions_per_window
    ),
    failure_threshold=(_APP_CONFIG.closed_session_maintenance_failure_threshold),
    breaker_backoff_seconds=(_APP_CONFIG.closed_session_maintenance_breaker_backoff_seconds),
)
_CLOSED_SESSION_MAINTENANCE_STATE = ClosedSessionMaintenanceState(
    settings=_CLOSED_SESSION_MAINTENANCE_SETTINGS
)
_TRADING_HOURS_REFRESH_WAKEUP = asyncio.Event()


def _ibkr_history_session_recovered() -> None:
    recover_provider_trading_hours_after_connection(
        "ibkr",
        maintenance_state=_CLOSED_SESSION_MAINTENANCE_STATE,
        wakeup_event=_TRADING_HOURS_REFRESH_WAKEUP,
    )


def configure_background_loop_deps(deps: BackgroundLoopDeps) -> None:
    global _DEPS
    _DEPS = deps
    _GEX_SCHEDULER_SETTINGS_RUNTIME.configure(store_factory=deps.store_factory)


def _deps() -> BackgroundLoopDeps:
    if _DEPS is None:
        raise RuntimeError("background loop dependencies are not configured")
    return _DEPS


def gex_scheduler_runtime_settings() -> dict[str, Any]:
    return _GEX_SCHEDULER_SETTINGS_RUNTIME.settings()


def save_gex_scheduler_runtime_settings(setting: dict[str, Any]) -> dict[str, Any]:
    return _GEX_SCHEDULER_SETTINGS_RUNTIME.save(setting)


def reconcile_gex_scheduler_runtime_settings(
    raw_setting: Any,
    present: bool,
    settings_revision: int,
) -> dict[str, Any]:
    return _GEX_SCHEDULER_SETTINGS_RUNTIME.publish_committed_snapshot(
        raw_setting,
        present,
        settings_revision,
    )


def ibkr_self_heal_status() -> dict[str, Any]:
    return _IBKR_SELF_HEAL_STATE.snapshot()


def closed_session_maintenance_status() -> dict[str, Any]:
    return _CLOSED_SESSION_MAINTENANCE_STATE.snapshot()


async def persist_quote_snapshots(
    live_map: dict[str, dict[str, Any]] | None,
    instruments: Sequence[dict[str, Any]],
) -> None:
    deps = _deps()
    await persist_quote_snapshots_async(
        live_map,
        instruments=instruments,
        store_factory=deps.store_factory,
    )


async def quote_orchestrator_loop() -> None:
    deps = _deps()
    ibkr_adapter = get_provider("ibkr")
    await run_quote_orchestrator_runtime(
        QuoteOrchestratorRuntimeDeps(
            state=deps.quote_orchestrator_state,
            logger=deps.logger,
            startup_delay_seconds=1.0,
            poll_seconds=QUOTE_STREAM_SECONDS,
            snapshot_persist_seconds=QUOTE_SNAPSHOT_SECONDS,
            server_sleeping=deps.server_sleeping,
            quote_route_snapshot=deps.quote_route_snapshot,
            provider_pollers=quote_provider_pollers(),
            quote_subscription_generation=ibkr_adapter.quote_subscription_generation,
            sync_ibkr_subscriptions=ibkr_adapter.async_sync_quote_subscriptions,
            cached_quotes=ibkr_adapter.cached_quotes,
            store_quote_cache=deps.store_quote_cache,
            persist_quote_snapshots=persist_quote_snapshots,
        )
    )


async def snapshot_retention_loop() -> None:
    deps = _deps()
    await run_snapshot_retention_loop(
        store_factory=deps.store_factory,
        logger_warning=deps.logger.warning,
    )


def quote_provider_pollers() -> tuple[QuoteProviderPoller, ...]:
    pollers: list[QuoteProviderPoller] = []
    for provider in live_quote_polling_providers():
        runtime = get_provider(provider).create_quote_polling_runtime()
        pollers.append(
            QuoteProviderPoller(
                provider=provider,
                fetch_quotes=runtime.fetch_quotes,
                close=runtime.aclose,
            )
        )
    return tuple(pollers)


async def trading_hours_refresh_loop() -> None:
    deps = _deps()
    await run_trading_hours_refresh_loop(
        TradingHoursRefreshDeps(
            store_factory=deps.store_factory,
            server_sleeping=deps.server_sleeping,
            selected_instruments=deps.selected_instruments,
            active_chart_streams=deps.active_chart_streams,
            logger_warning=deps.logger.warning,
            resolve_current_futures_contract=lambda store, instrument: (
                resolve_and_persist_current_contract(
                    store,
                    instrument,
                )
            ),
            refresh_quote_routes=deps.refresh_quote_routes,
            invalidate_market_analysis_route=deps.invalidate_market_analysis_route,
            watchlist_gap_quality=deps.watchlist_gap_quality,
            maintenance_settings=_CLOSED_SESSION_MAINTENANCE_SETTINGS,
            maintenance_state=_CLOSED_SESSION_MAINTENANCE_STATE,
            wakeup_event=_TRADING_HOURS_REFRESH_WAKEUP,
        )
    )


async def ibkr_self_heal_loop() -> None:
    deps = _deps()
    await run_ibkr_self_heal_loop(
        poll_seconds=IBKR_SELF_HEAL_POLL_SECONDS,
        server_sleeping=deps.server_sleeping,
        active_chart_streams=deps.active_chart_streams,
        quote_instruments=deps.combined_quote_instruments,
        instrument_lookup=deps.instrument_lookup,
        ibkr_status=deps.ibkr_status,
        watchlist_gap_quality=deps.watchlist_gap_quality,
        request_recovery_tail=deps.request_watchlist_recovery_tail,
        force_reconnect=deps.force_ibkr_reconnect,
        cleanup_quarantine=deps.cleanup_ibkr_quarantine,
        history_session_recovered=_ibkr_history_session_recovered,
        state=_IBKR_SELF_HEAL_STATE,
        logger_warning=deps.logger.warning,
        logger_debug=deps.logger.debug,
    )


async def gex_scheduler_loop() -> None:
    deps = _deps()
    await run_gex_scheduler_loop(
        interval_seconds=30.0,
        runtime_settings=gex_scheduler_runtime_settings,
        server_sleeping=deps.server_sleeping,
        live_gex_status=live_gex_status,
        scheduler_jobs=gex_scheduler_jobs,
        instrument_for_job=deps.instrument_lookup,
        store_factory=deps.store_factory,
        scheduler_state=deps.gex_scheduler_state,
        logger_warning=deps.logger.warning,
    )


async def option_target_reprice_loop() -> None:
    deps = _deps()
    await run_option_target_reprice_loop(
        poll_seconds=OPTION_TARGET_REPRICE_POLL_SECONDS,
        server_sleeping=deps.server_sleeping,
        store_factory=deps.store_factory,
        option_target_caps_settings=deps.option_target_caps_settings,
        quote_cache_for_instruments=deps.quote_cache_for_instruments,
        parse_iso_ts=deps.parse_iso_ts,
        option_target_samples_committed=option_target_samples_committed,
        logger_debug=deps.logger.debug,
    )
