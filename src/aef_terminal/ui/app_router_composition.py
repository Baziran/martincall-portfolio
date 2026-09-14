from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from aef_terminal.data.gex.live import live_gex_status
from aef_terminal.data.providers import (
    bind_provider_future_root,
    bind_provider_instrument,
    provider_catalog,
    search_provider_instruments,
)
from aef_terminal.indicators.registry import indicator_modules
from aef_terminal.indicators.refs import resolve_ref
from aef_terminal.ui import (
    backend_restart,
    background_loops_runtime,
    client_settings,
    economic_calendar_runtime,
    ibkr_gateway_login_control,
    ibkr_runtime,
    market_analysis_runtime,
    market_indicator_params,
    option_target_runtime,
    paper_telegram_runtime,
    paper_runtime,
    screener_services,
    stream_payload,
    system_status_runtime,
    telegram_runtime,
)
from aef_terminal.ui.price_alert_services import require_unique_price_alerts
from aef_terminal.ui.asset_services import (
    martincall_css_delivery_async,
    martincall_css_gzip_async,
    martincall_css_gzip_sync,
    martincall_html_async,
    martincall_js_delivery_async,
    martincall_js_gzip_async,
    martincall_js_gzip_sync,
    martincall_quote_worker_delivery_async,
    martincall_quote_worker_gzip_async,
    martincall_quote_worker_gzip_sync,
)
from aef_terminal.ui.drawing_services import normalize_drawing_anchors
from aef_terminal.ui.paper.execution import (
    record_chart_execution_snapshot,
    record_quote_execution_snapshots,
)
from aef_terminal.ui.paper.metadata import (
    paper_setup_key,
    paper_setup_source_label,
    paper_signal_source_key,
    paper_trade_edge_action,
    paper_trade_enriched,
    paper_trade_session,
)
from aef_terminal.ui.paper.metrics import paper_replay_summary, paper_trade_stats
from aef_terminal.ui.runtime import (
    chart_stream,
    quote_stream,
    server_sleep_status,
    server_sleeping,
    set_tick_live_enabled,
    tick_live_status,
)
from aef_terminal.ui.routers.ticks import TickRouterDeps, create_tick_router
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.runtime.telemetry import exception_message
from aef_terminal.ui.services.memory_diagnostics import memory_diagnostics_snapshot
from aef_terminal.ui.services.fast_indicator_runtime import (
    fast_indicator_snapshots,
)
from aef_terminal.ui.system_status import (
    health_status_snapshot,
    readiness_status_snapshot,
)
from aef_terminal.ui.wiring import wire_app_routers
from aef_terminal.ui.wiring_contracts import (
    AppRouterWiring,
    AssetWiring,
    EconomicCalendarWiring,
    GexWiring,
    IbkrWiring,
    MarketWiring,
    ReferenceWiring,
    StorageWiring,
    StreamWiring,
    SystemWiring,
    TelegramWiring,
    TradingWiring,
)


def wire_default_app_routers(
    app: FastAPI,
    *,
    logger: logging.Logger,
    store_factory: Callable[[], Any],
    set_server_sleeping: Callable[[bool, str], dict[str, Any]],
    flush_log_handlers: Callable[[], None],
) -> None:
    martincall_js_gzip_sync()
    martincall_css_gzip_sync()
    martincall_quote_worker_gzip_sync()
    wire_app_routers(
        app,
        AppRouterWiring(
            assets=AssetWiring(
                martincall_html_async=martincall_html_async,
                martincall_js_delivery_async=martincall_js_delivery_async,
                martincall_css_delivery_async=martincall_css_delivery_async,
                martincall_quote_worker_delivery_async=martincall_quote_worker_delivery_async,
                martincall_js_gzip_async=martincall_js_gzip_async,
                martincall_css_gzip_async=martincall_css_gzip_async,
                martincall_quote_worker_gzip_async=martincall_quote_worker_gzip_async,
            ),
            system=SystemWiring(
                health_status=health_status_snapshot,
                readiness_status=lambda: readiness_status_snapshot(store_factory=store_factory),
                system_status=system_status_runtime.system_status_snapshot,
                memory_status=memory_diagnostics_snapshot,
                set_server_sleeping=set_server_sleeping,
                apply_ibkr_runtime_settings=ibkr_runtime.apply_ibkr_runtime_settings,
                backend_restart_record=backend_restart.record_backend_restart_request,
                flush_log_handlers=flush_log_handlers,
            ),
            economic_calendar=EconomicCalendarWiring(
                snapshot=economic_calendar_runtime.economic_calendar_snapshot,
            ),
            telegram=TelegramWiring(
                set_telegram_interactive_enabled=lambda enabled: (
                    telegram_runtime.set_telegram_interactive_enabled(
                        system_status_provider=system_status_runtime.system_status_snapshot,
                        enabled=enabled,
                    )
                ),
                paper_telegram_feed_status=paper_telegram_runtime.paper_telegram_feed_status,
                telegram_interactive_status=telegram_runtime.telegram_interactive_status,
                store_factory=store_factory,
            ),
            ibkr=IbkrWiring(
                apply_ibkr_runtime_settings=ibkr_runtime.apply_ibkr_runtime_settings,
                apply_ibkr_runtime_settings_async=ibkr_runtime.apply_ibkr_runtime_settings_async,
                combined_quote_instruments=quote_stream.combined_quote_instruments,
                ibkr_gateway_login_control_status=(
                    ibkr_gateway_login_control.ibkr_gateway_login_control_status
                ),
                request_ibkr_gateway_login=(ibkr_gateway_login_control.request_ibkr_gateway_login),
                server_sleeping=server_sleeping,
                server_sleep_status=server_sleep_status,
            ),
            reference=ReferenceWiring(
                reconcile_client_settings=client_settings.reconcile_client_settings_snapshot,
                reconcile_gex_scheduler_settings=(
                    background_loops_runtime.reconcile_gex_scheduler_runtime_settings
                ),
                reconcile_option_target_caps_settings=(
                    option_target_runtime.reconcile_option_target_caps_settings
                ),
                data_provider_catalog=provider_catalog,
                search_provider_instruments=search_provider_instruments,
                bind_provider_instrument=bind_provider_instrument,
                bind_provider_future_root=bind_provider_future_root,
                store_factory=store_factory,
                refresh_quote_routes=quote_stream.refresh_routes_from_store,
            ),
            market=MarketWiring(
                store_factory=store_factory,
                normalize_drawing_anchors=normalize_drawing_anchors,
                apply_ibkr_runtime_settings_async=ibkr_runtime.apply_ibkr_runtime_settings_async,
                server_sleeping=server_sleeping,
                server_sleep_status=server_sleep_status,
                note_active_chart=chart_stream.note_active_chart,
                market_indicator_params=market_indicator_params.market_indicator_params,
                market_analysis_payload=market_analysis_runtime.build_analysis_payload,
                market_analysis_key=market_analysis_runtime.analysis_key,
                register_market_analysis_wanted=market_analysis_runtime.register_wanted,
                renew_market_analysis_lease=market_analysis_runtime.renew_client_lease,
                refresh_market_analysis=market_analysis_runtime.refresh_client_analysis,
                release_market_analysis_lease=market_analysis_runtime.release_client_lease,
                market_analysis_snapshot=market_analysis_runtime.snapshot,
                screener_snapshot=screener_services.screener_snapshot,
                screener_trends_snapshot=screener_services.screener_trends_snapshot,
            ),
            trading=TradingWiring(
                store_factory=store_factory,
                paper_config=paper_runtime.paper_config,
                queue_paper_trade_telegram=paper_telegram_runtime.queue_paper_trade_telegram,
                paper_trade_enriched=paper_trade_enriched,
                paper_trade_stats=paper_trade_stats,
                paper_replay_summary=paper_replay_summary,
                paper_signal_source_key=paper_signal_source_key,
                paper_setup_source_label=paper_setup_source_label,
                paper_setup_key=paper_setup_key,
                paper_trade_session=paper_trade_session,
                paper_trade_edge_action=paper_trade_edge_action,
                paper_order_price_snapshot=paper_runtime.paper_order_price_snapshot,
            ),
            storage=StorageWiring(
                store_factory=store_factory,
                apply_ibkr_runtime_settings=ibkr_runtime.apply_ibkr_runtime_settings,
                publish_client_settings_mutations=(
                    client_settings.publish_client_settings_mutations
                ),
                normalize_drawing_anchors=normalize_drawing_anchors,
                require_unique_price_alerts=require_unique_price_alerts,
            ),
            gex=GexWiring(
                apply_ibkr_runtime_settings=ibkr_runtime.apply_ibkr_runtime_settings,
                apply_ibkr_runtime_settings_async=ibkr_runtime.apply_ibkr_runtime_settings_async,
                exception_message=exception_message,
                gex_scheduler_runtime_settings=background_loops_runtime.gex_scheduler_runtime_settings,
                save_gex_scheduler_settings=(
                    background_loops_runtime.save_gex_scheduler_runtime_settings
                ),
                gex_live_status=live_gex_status,
                instrument_lookup=lookup_runtime_instrument,
                option_target_caps_settings=option_target_runtime.option_target_caps_settings,
                save_option_target_caps_settings=(
                    option_target_runtime.save_option_target_caps_settings
                ),
                parse_iso_ts=stream_payload.parse_iso_ts,
                quote_cache_for_instruments=quote_stream.cache_for_instruments,
                server_sleeping=server_sleeping,
                store_factory=store_factory,
            ),
            streams=StreamWiring(
                logger=logger,
                store_factory=store_factory,
                apply_ibkr_runtime_settings_async=ibkr_runtime.apply_ibkr_runtime_settings_async,
                server_sleeping=server_sleeping,
                quote_route_snapshot=quote_stream.route_snapshot,
                set_quote_stream_wanted=quote_stream.set_stream_wanted,
                quote_stream_started=quote_stream.stream_started,
                quote_stream_finished=quote_stream.stream_finished,
                screener_bases=screener_services.screener_bases,
                screener_rows=screener_services.screener_rows,
                quote_cache_for_instruments=quote_stream.cache_for_instruments,
                record_quote_execution_snapshots=record_quote_execution_snapshots,
                fast_indicator_snapshots=fast_indicator_snapshots,
                chart_stream_key=chart_stream.stream_key,
                chart_stream_started=chart_stream.stream_started,
                chart_stream_finished=chart_stream.stream_finished,
                stream_bar_payload=stream_payload.stream_bar_payload,
                stream_bar_signature=stream_payload.stream_bar_signature,
                parse_stream_ts=stream_payload.parse_stream_ts,
                chart_recovery_snapshot=stream_payload.chart_recovery_snapshot,
                record_chart_execution_snapshot=record_chart_execution_snapshot,
                instrument_lookup=lookup_runtime_instrument,
                parse_iso_ts=stream_payload.parse_iso_ts,
            ),
        ),
    )
    app.include_router(
        create_tick_router(
            TickRouterDeps(
                store_factory=store_factory,
                live_status=tick_live_status,
                set_live_enabled=set_tick_live_enabled,
            )
        )
    )
    for module in indicator_modules():
        if module.router_ref:
            app.include_router(resolve_ref(module.router_ref)())
