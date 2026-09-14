from __future__ import annotations

import logging
from functools import partial
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aef_terminal.alerts.telegram import telegram_config_status
from aef_terminal.config import AppConfig
from aef_terminal.data import providers as provider_registry
from aef_terminal.data.gex.scheduler import gex_runtime_status
from aef_terminal.data.ibkr.quotes import runtime_status as ibkr_runtime_status
from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
from aef_terminal.data.ibkr.session import (
    cleanup_quarantined_sessions_async,
    force_reconnect_async as ibkr_force_reconnect_async,
)
from aef_terminal.runtime.chart_commits import chart_commits
from aef_terminal.indicators.settings_resolution import indicator_calc_enabled_from_settings
from aef_terminal.ui import (
    backend_restart,
    background_loops_runtime,
    client_settings,
    ibkr_runtime,
    market_analysis_runtime,
    market_indicator_params,
    option_target_runtime,
    paper_runtime,
    paper_telegram_runtime,
    screener_services,
    server_alert_runtime,
    stream_payload,
    system_status_runtime,
    telegram_runtime,
)
from aef_terminal.ui.ibkr_gateway_login_control import ibkr_gateway_login_control_status
from aef_terminal.ui.bootstrap import BootstrapDeps, configure_bootstrap_deps
from aef_terminal.ui.drawing_services import normalize_drawing_anchors
from aef_terminal.ui.indicator_service_host import (
    IndicatorServiceContext,
    configure_indicator_services,
    start_indicator_service_tasks,
    stop_indicator_services,
)
from aef_terminal.ui.paper.edge_filters import paper_load_edge_filters
from aef_terminal.ui.paper.execution import (
    paper_order_execution_transition,
    record_chart_execution_snapshot,
)
from aef_terminal.ui.paper.sync import PaperSyncDeps, configure_paper_sync_deps
from aef_terminal.ui.runtime import (
    CHART_STREAM_POLL_SECONDS,
    WS_HEARTBEAT_SECONDS,
    chart_stream,
    host_sleep_monitor_loop,
    host_sleep_status,
    quote_stream,
    server_sleep_status,
    tick_live_status,
    configure_tick_live,
    stop_tick_live,
    tick_live_restore_loop,
)
from aef_terminal.ui.runtime.host_sleep import HostSleepDeps, configure_host_sleep_deps
from aef_terminal.ui.runtime.server_sleep import (
    ServerSleepDeps,
    configure_server_sleep_deps,
    server_sleeping,
    set_server_sleeping,
)
from aef_terminal.ui.runtime.confirmed_bar_demand import confirmed_bar_demand
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.services.fast_indicator_runtime import (
    FastIndicatorRuntimeDeps,
    configure_fast_indicator_runtime,
    fast_indicator_runtime_status,
)
from aef_terminal.ui.services.headless_research_capture import (
    HeadlessResearchCaptureDeps,
    HeadlessResearchCaptureSettings,
    configure_headless_research_capture,
    headless_research_capture_loop,
    headless_research_capture_status,
)
from aef_terminal.ui.services.gex_scheduler import GexSchedulerRouteState
from aef_terminal.ui.routers.gex import GEX_MANUAL_REFRESH_OWNER
from aef_terminal.ui.services.chart_history import (
    shutdown_chart_history_runtime,
    start_chart_history_runtime,
)
from aef_terminal.ui.services.market_analysis_process import (
    start_market_analysis_process_runtime,
)
from aef_terminal.ui.services.chart_stream_coordinator import (
    ChartStreamCoordinatorDeps,
)
from aef_terminal.ui.services.quote_orchestrator import QuoteOrchestratorState
from aef_terminal.ui.services.quote_snapshots import quote_snapshot_write_stats
from aef_terminal.ui.system_status import SystemStatusDeps


ibkr_gex_runtime_status = partial(
    gex_runtime_status,
    provider_runtime=ibkr_gex_provider_runtime,
)

_QUOTE_ORCHESTRATOR_STATE = QuoteOrchestratorState()
_GEX_SCHEDULER_STATE: dict[tuple[str, str], GexSchedulerRouteState] = {}


def start_request_task_owners() -> None:
    provider_registry.start_provider_bar_load_runtime()
    start_chart_history_runtime()
    screener_services.start_screener_trend_runtime()
    GEX_MANUAL_REFRESH_OWNER.start_lifecycle()


async def stop_request_task_owners() -> None:
    try:
        await GEX_MANUAL_REFRESH_OWNER.shutdown()
    finally:
        try:
            await screener_services.shutdown_screener_trend_runtime()
        finally:
            try:
                await shutdown_chart_history_runtime()
            finally:
                await provider_registry.shutdown_provider_bar_load_runtime()


def set_server_sleeping_runtime(
    sleeping: bool, reason: str = "", *, persist: bool = True
) -> dict[str, Any]:
    return set_server_sleeping(
        sleeping,
        reason,
        persist=persist,
        clear_quote_wanted=quote_stream.clear_all_wanted,
    )


def flush_log_handlers(logger: logging.Logger) -> None:
    for active_logger in (logger, logging.getLogger()):
        for handler in list(getattr(active_logger, "handlers", []) or []):
            try:
                handler.flush()
            except Exception:
                pass


def configure_runtime_state(*, store_factory: Callable[[], Any]) -> None:
    client_settings.configure_client_settings_deps(
        client_settings.ClientSettingsDeps(store_factory=store_factory)
    )
    configure_server_sleep_deps(
        ServerSleepDeps(
            store_factory=store_factory,
            publish_client_settings_patch=client_settings.publish_client_settings_patch,
        )
    )
    configure_host_sleep_deps(HostSleepDeps(store_factory=store_factory))
    telegram_runtime.configure_telegram_runtime_deps(
        telegram_runtime.TelegramRuntimeDeps(
            store_factory=store_factory,
            selected_instruments=lambda: quote_stream.select_instruments(None),
            quote_cache_for_instruments=quote_stream.cache_for_instruments,
        )
    )
    paper_telegram_runtime.configure_paper_telegram_runtime_deps(
        paper_telegram_runtime.PaperTelegramRuntimeDeps(
            client_settings_snapshot=client_settings.client_settings_snapshot,
        )
    )
    configure_paper_sync_deps(
        PaperSyncDeps(
            store_factory=store_factory,
            paper_config=paper_runtime.paper_config,
            paper_order_execution_transition=paper_order_execution_transition,
            paper_order_price_snapshot=paper_runtime.paper_order_price_snapshot,
        )
    )


def configure_system_status(
    *,
    started_at: datetime,
    build: dict[str, str],
    store_factory: Callable[[], Any],
) -> None:
    system_status_runtime.configure_system_status_runtime(
        SystemStatusDeps(
            started_at=started_at,
            build=dict(build),
            apply_runtime_settings=ibkr_runtime.apply_ibkr_runtime_settings,
            store_factory=store_factory,
            ibkr_status=ibkr_runtime_status,
            ibkr_gateway_login_control_status=ibkr_gateway_login_control_status,
            ibkr_self_heal_status=background_loops_runtime.ibkr_self_heal_status,
            tick_live_status=tick_live_status,
            chart_stream_status=chart_stream.status,
            closed_session_maintenance_status=(
                background_loops_runtime.closed_session_maintenance_status
            ),
            server_sleep_status=server_sleep_status,
            host_sleep_status=host_sleep_status,
            gex_status=ibkr_gex_runtime_status,
            gex_scheduler_settings=background_loops_runtime.gex_scheduler_runtime_settings,
            quote_snapshot_stats=quote_snapshot_write_stats,
            option_target_caps_settings=option_target_runtime.option_target_caps_settings,
            telegram_config_status=telegram_config_status,
            telegram_interactive_status=telegram_runtime.telegram_interactive_status,
            paper_telegram_feed_status=paper_telegram_runtime.paper_telegram_feed_status,
            backend_restart_status=backend_restart.backend_restart_status,
            quote_stream_status=quote_stream.cache_stats,
            fast_indicator_status=fast_indicator_runtime_status,
            research_capture_status=headless_research_capture_status,
        )
    )


def configure_backend_restart(
    *,
    store_factory: Callable[[], Any],
    flush_log_handlers_cb: Callable[[], None],
) -> None:
    backend_restart.configure_backend_restart_deps(
        backend_restart.BackendRestartDeps(
            store_factory=store_factory,
            ibkr_status=ibkr_runtime_status,
            gex_status=ibkr_gex_runtime_status,
            tick_live_status=tick_live_status,
            quote_client_count=quote_stream.client_count,
            chart_connection_summary=chart_stream.connection_summary,
            app_runtime_status=system_status_runtime.app_runtime_status,
            flush_log_handlers=flush_log_handlers_cb,
        )
    )


def configure_runtime_callbacks(
    *, logger: logging.Logger, store_factory: Callable[[], Any]
) -> None:
    configure_tick_live(
        server_sleeping=server_sleeping,
        client_settings_snapshot=client_settings.client_settings_snapshot,
        publish_client_settings_patch=client_settings.publish_client_settings_patch,
        restore_allowed=_tick_flow_calc_enabled_for_restore,
        store_factory=store_factory,
        logger=logger,
    )
    configure_fast_indicator_runtime(
        FastIndicatorRuntimeDeps(
            store_factory=store_factory,
            client_settings_snapshot=client_settings.client_settings_snapshot,
            lookup_runtime_instrument=lookup_runtime_instrument,
        )
    )
    confirmed_bar_demand.configure(
        ChartStreamCoordinatorDeps(
            logger=logger,
            store_factory=store_factory,
            apply_provider_runtime_settings_async=(ibkr_runtime.apply_ibkr_runtime_settings_async),
            server_sleeping=server_sleeping,
            websocket_heartbeat_seconds=WS_HEARTBEAT_SECONDS,
            stream_bar_payload=stream_payload.stream_bar_payload,
            stream_bar_signature=stream_payload.stream_bar_signature,
            parse_iso_ts=stream_payload.parse_iso_ts,
            parse_stream_ts=stream_payload.parse_stream_ts,
            chart_recovery_snapshot=stream_payload.chart_recovery_snapshot,
            record_chart_execution_snapshot=record_chart_execution_snapshot,
            chart_stream_poll_seconds=CHART_STREAM_POLL_SECONDS,
            lookup_runtime_instrument=lookup_runtime_instrument,
        ),
        on_generation=(market_analysis_runtime.confirmed_bar_context_advanced),
    )
    option_target_runtime.configure_option_target_runtime_deps(
        option_target_runtime.OptionTargetRuntimeDeps(store_factory=store_factory)
    )
    ibkr_runtime.configure_ibkr_runtime_deps(
        ibkr_runtime.IbkrRuntimeDeps(
            load_client_settings_snapshot=client_settings.read_client_settings_snapshot,
            publish_client_settings_snapshot=client_settings.publish_client_settings_snapshot,
            clear_quote_wanted=quote_stream.clear_all_wanted,
        )
    )
    server_alert_runtime.configure_server_alert_runtime_deps(
        server_alert_runtime.ServerAlertRuntimeDeps(
            store_factory=store_factory,
            quote_route_snapshot=quote_stream.route_snapshot,
            set_server_alert_wanted=quote_stream.set_server_alert_wanted,
            quote_cache_for_instruments=quote_stream.cache_for_instruments,
            queue_paper_trade_telegram=paper_telegram_runtime.queue_paper_trade_telegram,
            send_server_telegram_alert=telegram_runtime.send_server_telegram_alert,
            logger=logger,
            server_alert_indicator_params=lambda: (
                market_indicator_params.server_alert_indicator_params(
                    client_settings.client_settings_snapshot()
                )
            ),
        )
    )
    paper_runtime.configure_paper_runtime_deps(
        paper_runtime.PaperRuntimeDeps(
            client_settings_snapshot=client_settings.client_settings_snapshot,
            edge_filter_loader=paper_load_edge_filters,
        )
    )
    market_analysis_runtime.configure_market_analysis_runtime_deps(
        market_analysis_runtime.MarketAnalysisRuntimeDeps(
            client_settings_snapshot=client_settings.client_settings_snapshot,
            store_factory=store_factory,
            normalize_drawing_anchors=normalize_drawing_anchors,
            reconcile_confirmed_bar_demands=confirmed_bar_demand.reconcile,
            shutdown_confirmed_bar_demands=confirmed_bar_demand.shutdown,
        )
    )
    configure_headless_research_capture(
        HeadlessResearchCaptureSettings.from_config(AppConfig()),
        HeadlessResearchCaptureDeps(
            lookup_runtime_instrument=lookup_runtime_instrument,
            client_settings_snapshot=client_settings.client_settings_snapshot,
            build_analysis_payload=market_analysis_runtime.build_analysis_payload,
            analysis_key=market_analysis_runtime.analysis_key,
            register_wanted=market_analysis_runtime.register_wanted,
            renew_client_lease=market_analysis_runtime.renew_client_lease,
            release_client_lease=market_analysis_runtime.release_client_lease,
            server_sleeping=server_sleeping,
        ),
    )
    quote_stream.configure_server_sleeping(server_sleeping=server_sleeping)
    screener_services.configure_screener_runtime_deps(
        screener_services.ScreenerRuntimeDeps(
            store_factory=store_factory,
            apply_ibkr_runtime_settings_async=ibkr_runtime.apply_ibkr_runtime_settings_async,
            selected_instruments=quote_stream.select_instruments,
            server_sleeping=server_sleeping,
            set_transient_quote_wanted=quote_stream.set_transient_wanted,
            quote_cache_for_instruments=quote_stream.cache_for_instruments,
        )
    )
    stream_payload.configure_stream_payload_deps(
        stream_payload.StreamPayloadDeps(
            store_factory=store_factory,
        )
    )


def _tick_flow_calc_enabled_for_restore(
    settings: dict[str, object],
    instrument_id: str,
    _instrument: dict[str, object],
) -> bool:
    return indicator_calc_enabled_from_settings(
        settings,
        instrument_id=instrument_id,
        indicator_id="tick_flow",
    )


def configure_indicator_module_services(
    *,
    logger: logging.Logger,
    store_factory: Callable[[], Any],
) -> None:
    configure_indicator_services(
        IndicatorServiceContext(
            logger=logger,
            store_factory=store_factory,
            server_sleeping=server_sleeping,
            client_settings_snapshot=client_settings.client_settings_snapshot,
        )
    )


def configure_bootstrap(*, logger: logging.Logger) -> None:
    configure_bootstrap_deps(
        BootstrapDeps(
            start_market_analysis_process_runtime=start_market_analysis_process_runtime,
            start_chart_commit_runtime=chart_commits.start,
            start_request_task_owners=start_request_task_owners,
            stop_request_task_owners=stop_request_task_owners,
            stop_chart_commit_runtime=chart_commits.shutdown,
            server_alert_monitor_loop=lambda: server_alert_runtime.server_alert_monitor_loop(
                server_sleeping=server_sleeping,
                logger=logger,
            ),
            trading_hours_loop=background_loops_runtime.trading_hours_refresh_loop,
            gex_scheduler_loop=background_loops_runtime.gex_scheduler_loop,
            option_target_reprice_loop=background_loops_runtime.option_target_reprice_loop,
            host_sleep_monitor_loop=lambda: host_sleep_monitor_loop(
                server_sleeping=server_sleeping
            ),
            market_analysis_worker_loop=lambda: market_analysis_runtime.worker_loop(
                server_sleeping=server_sleeping
            ),
            research_capture_loop=headless_research_capture_loop,
            stop_market_analysis_runtime=market_analysis_runtime.shutdown,
            quote_orchestrator_loop=background_loops_runtime.quote_orchestrator_loop,
            snapshot_retention_loop=background_loops_runtime.snapshot_retention_loop,
            ibkr_self_heal_loop=background_loops_runtime.ibkr_self_heal_loop,
            tick_live_restore_loop=tick_live_restore_loop,
            stop_tick_live=stop_tick_live,
            ensure_paper_telegram_worker=(
                paper_telegram_runtime.ensure_paper_telegram_worker_async
            ),
            ensure_telegram_interactive_bot=lambda: (
                telegram_runtime.ensure_telegram_interactive_bot_async(
                    system_status_provider=system_status_runtime.system_status_snapshot,
                )
            ),
            start_indicator_services=start_indicator_service_tasks,
            stop_indicator_services=stop_indicator_services,
            stop_telegram_interactive_bot=(telegram_runtime.stop_telegram_interactive_bot_async),
            stop_paper_telegram_worker=(paper_telegram_runtime.stop_paper_telegram_worker_async),
        )
    )


def configure_background_loops(
    *,
    logger: logging.Logger,
    store_factory: Callable[[], Any],
) -> None:
    background_loops_runtime.configure_background_loop_deps(
        background_loops_runtime.BackgroundLoopDeps(
            store_factory=store_factory,
            quote_orchestrator_state=_QUOTE_ORCHESTRATOR_STATE,
            logger=logger,
            server_sleeping=server_sleeping,
            combined_quote_instruments=quote_stream.combined_quote_instruments,
            quote_route_snapshot=quote_stream.route_snapshot,
            store_quote_cache=quote_stream.store_cache,
            refresh_quote_routes=quote_stream.refresh_routes_from_store,
            invalidate_market_analysis_route=(
                market_analysis_runtime.invalidate_provider_session_route
            ),
            selected_instruments=lambda: quote_stream.select_instruments(None),
            instrument_lookup=lookup_runtime_instrument,
            active_chart_streams=chart_stream.active_streams,
            watchlist_gap_quality=screener_services.watchlist_gap_quality,
            request_watchlist_recovery_tail=(screener_services.request_watchlist_recovery_tail),
            set_transient_quote_wanted=quote_stream.set_transient_wanted,
            quote_cache_for_instruments=quote_stream.cache_for_instruments,
            ibkr_status=ibkr_runtime_status,
            force_ibkr_reconnect=ibkr_force_reconnect_async,
            cleanup_ibkr_quarantine=cleanup_quarantined_sessions_async,
            gex_scheduler_state=_GEX_SCHEDULER_STATE,
            option_target_caps_settings=option_target_runtime.option_target_caps_settings,
            parse_iso_ts=stream_payload.parse_iso_ts,
        )
    )
