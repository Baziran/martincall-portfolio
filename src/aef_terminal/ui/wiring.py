from __future__ import annotations


from fastapi import FastAPI

from aef_terminal.data.ibkr.quotes import runtime_status as ibkr_runtime_status
from aef_terminal.data.ibkr.session import disconnect_all_sessions as ibkr_disconnect_all_sessions
from aef_terminal.settings_contract import IBKR_PORT_SETTING_KEY
from aef_terminal.ui.runtime.constants import (
    CHART_STREAM_POLL_SECONDS,
    QUOTE_STREAM_CLEANUP_GRACE_SECONDS,
    QUOTE_STREAM_SECONDS,
    WS_HEARTBEAT_SECONDS,
)
from aef_terminal.ui.routers.assets import AssetRouterDeps, create_asset_router
from aef_terminal.ui.routers.browser_capture import (
    BrowserCaptureRouterDeps,
    create_browser_capture_router,
)
from aef_terminal.ui.routers.economic_calendar import (
    EconomicCalendarRouterDeps,
    create_economic_calendar_router,
)
from aef_terminal.ui.routers.gex import (
    GEX_MANUAL_REFRESH_OWNER,
    GexRouterDeps,
    create_gex_router,
)
from aef_terminal.ui.routers.ibkr import IbkrRouterDeps, create_ibkr_router
from aef_terminal.ui.routers.market import MarketRouterDeps, create_market_router
from aef_terminal.ui.routers.reference import ReferenceRouterDeps, create_reference_router
from aef_terminal.ui.routers.storage import StorageRouterDeps, create_storage_router
from aef_terminal.ui.routers.streams import StreamRouterDeps, create_stream_router
from aef_terminal.ui.routers.system import SystemRouterDeps, create_system_router
from aef_terminal.ui.routers.telegram import TelegramRouterDeps, create_telegram_router
from aef_terminal.ui.routers.trading import TradingRouterDeps, create_trading_router
from aef_terminal.ui.wiring_contracts import AppRouterWiring
from aef_terminal.runtime.clock import utc_now_iso as _now_iso


def wire_app_routers(app: FastAPI, wiring: AppRouterWiring) -> None:
    app.include_router(
        create_asset_router(
            AssetRouterDeps(
                html=wiring.assets.martincall_html_async,
                js=wiring.assets.martincall_js_delivery_async,
                css=wiring.assets.martincall_css_delivery_async,
                quote_worker=wiring.assets.martincall_quote_worker_delivery_async,
                js_gzip=wiring.assets.martincall_js_gzip_async,
                css_gzip=wiring.assets.martincall_css_gzip_async,
                quote_worker_gzip=wiring.assets.martincall_quote_worker_gzip_async,
            )
        )
    )
    app.include_router(
        create_system_router(
            SystemRouterDeps(
                health_status=wiring.system.health_status,
                readiness_status=wiring.system.readiness_status,
                system_status=wiring.system.system_status,
                memory_status=wiring.system.memory_status,
                set_server_sleeping=wiring.system.set_server_sleeping,
                disconnect_ibkr_sessions=ibkr_disconnect_all_sessions,
                ibkr_status=ibkr_runtime_status,
                apply_ibkr_runtime_settings=wiring.system.apply_ibkr_runtime_settings,
                backend_restart_record=wiring.system.backend_restart_record,
                flush_log_handlers=wiring.system.flush_log_handlers,
                now_iso=_now_iso,
            )
        )
    )
    app.include_router(
        create_economic_calendar_router(
            EconomicCalendarRouterDeps(snapshot=wiring.economic_calendar.snapshot)
        )
    )
    app.include_router(
        create_telegram_router(
            TelegramRouterDeps(
                set_telegram_interactive_enabled=(wiring.telegram.set_telegram_interactive_enabled),
                paper_telegram_feed_status=wiring.telegram.paper_telegram_feed_status,
                telegram_interactive_status=wiring.telegram.telegram_interactive_status,
                store_factory=wiring.telegram.store_factory,
                now_iso=_now_iso,
            )
        )
    )
    app.include_router(
        create_ibkr_router(
            IbkrRouterDeps(
                apply_ibkr_runtime_settings=wiring.ibkr.apply_ibkr_runtime_settings,
                apply_ibkr_runtime_settings_async=wiring.ibkr.apply_ibkr_runtime_settings_async,
                combined_quote_instruments=wiring.ibkr.combined_quote_instruments,
                disconnect_ibkr_sessions=ibkr_disconnect_all_sessions,
                ibkr_gateway_login_control_status=(wiring.ibkr.ibkr_gateway_login_control_status),
                request_ibkr_gateway_login=wiring.ibkr.request_ibkr_gateway_login,
                server_sleeping=wiring.ibkr.server_sleeping,
                server_sleep_status=wiring.ibkr.server_sleep_status,
                ibkr_port_setting_key=IBKR_PORT_SETTING_KEY,
                now_iso=_now_iso,
            )
        )
    )
    app.include_router(
        create_reference_router(
            ReferenceRouterDeps(
                reconcile_client_settings=wiring.reference.reconcile_client_settings,
                reconcile_gex_scheduler_settings=(
                    wiring.reference.reconcile_gex_scheduler_settings
                ),
                reconcile_option_target_caps_settings=(
                    wiring.reference.reconcile_option_target_caps_settings
                ),
                data_provider_catalog=wiring.reference.data_provider_catalog,
                search_provider_instruments=wiring.reference.search_provider_instruments,
                bind_provider_instrument=wiring.reference.bind_provider_instrument,
                bind_provider_future_root=wiring.reference.bind_provider_future_root,
                store_factory=wiring.reference.store_factory,
                refresh_quote_routes=wiring.reference.refresh_quote_routes,
            )
        )
    )
    app.include_router(
        create_market_router(
            MarketRouterDeps(
                store_factory=wiring.market.store_factory,
                normalize_drawing_anchors=wiring.market.normalize_drawing_anchors,
                apply_ibkr_runtime_settings_async=wiring.market.apply_ibkr_runtime_settings_async,
                server_sleeping=wiring.market.server_sleeping,
                server_sleep_status=wiring.market.server_sleep_status,
                note_active_chart=wiring.market.note_active_chart,
                market_indicator_params=wiring.market.market_indicator_params,
                market_analysis_payload=wiring.market.market_analysis_payload,
                market_analysis_key=wiring.market.market_analysis_key,
                register_market_analysis_wanted=wiring.market.register_market_analysis_wanted,
                renew_market_analysis_lease=wiring.market.renew_market_analysis_lease,
                refresh_market_analysis=wiring.market.refresh_market_analysis,
                release_market_analysis_lease=wiring.market.release_market_analysis_lease,
                market_analysis_snapshot=wiring.market.market_analysis_snapshot,
                screener_snapshot=wiring.market.screener_snapshot,
                screener_trends_snapshot=wiring.market.screener_trends_snapshot,
            )
        )
    )
    app.include_router(
        create_trading_router(
            TradingRouterDeps(
                store_factory=wiring.trading.store_factory,
                paper_config=wiring.trading.paper_config,
                queue_paper_trade_telegram=wiring.trading.queue_paper_trade_telegram,
                paper_trade_enriched=wiring.trading.paper_trade_enriched,
                paper_trade_stats=wiring.trading.paper_trade_stats,
                paper_replay_summary=wiring.trading.paper_replay_summary,
                paper_signal_source_key=wiring.trading.paper_signal_source_key,
                paper_setup_source_label=wiring.trading.paper_setup_source_label,
                paper_setup_key=wiring.trading.paper_setup_key,
                paper_trade_session=wiring.trading.paper_trade_session,
                paper_trade_edge_action=wiring.trading.paper_trade_edge_action,
                paper_order_price_snapshot=wiring.trading.paper_order_price_snapshot,
            )
        )
    )
    app.include_router(
        create_storage_router(
            StorageRouterDeps(
                store_factory=wiring.storage.store_factory,
                apply_ibkr_runtime_settings=wiring.storage.apply_ibkr_runtime_settings,
                publish_client_settings_mutations=(
                    wiring.storage.publish_client_settings_mutations
                ),
                normalize_drawing_anchors=wiring.storage.normalize_drawing_anchors,
                require_unique_price_alerts=wiring.storage.require_unique_price_alerts,
            )
        )
    )
    app.include_router(
        create_gex_router(
            GexRouterDeps(
                apply_ibkr_runtime_settings=wiring.gex.apply_ibkr_runtime_settings,
                apply_ibkr_runtime_settings_async=wiring.gex.apply_ibkr_runtime_settings_async,
                exception_message=wiring.gex.exception_message,
                gex_live_status=wiring.gex.gex_live_status,
                gex_scheduler_runtime_settings=wiring.gex.gex_scheduler_runtime_settings,
                save_gex_scheduler_settings=wiring.gex.save_gex_scheduler_settings,
                instrument_lookup=wiring.gex.instrument_lookup,
                option_target_caps_settings=wiring.gex.option_target_caps_settings,
                save_option_target_caps_settings=wiring.gex.save_option_target_caps_settings,
                parse_iso_ts=wiring.gex.parse_iso_ts,
                quote_cache_for_instruments=wiring.gex.quote_cache_for_instruments,
                server_sleeping=wiring.gex.server_sleeping,
                store_factory=wiring.gex.store_factory,
                now_iso=_now_iso,
                manual_refresh_owner=GEX_MANUAL_REFRESH_OWNER,
            )
        )
    )
    app.include_router(
        create_stream_router(
            StreamRouterDeps(
                logger=wiring.streams.logger,
                store_factory=wiring.streams.store_factory,
                apply_provider_runtime_settings_async=wiring.streams.apply_ibkr_runtime_settings_async,
                server_sleeping=wiring.streams.server_sleeping,
                quote_route_snapshot=wiring.streams.quote_route_snapshot,
                set_quote_stream_wanted=wiring.streams.set_quote_stream_wanted,
                quote_stream_started=wiring.streams.quote_stream_started,
                quote_stream_finished=wiring.streams.quote_stream_finished,
                screener_bases=wiring.streams.screener_bases,
                screener_rows=wiring.streams.screener_rows,
                quote_cache_for_instruments=wiring.streams.quote_cache_for_instruments,
                record_quote_execution_snapshots=wiring.streams.record_quote_execution_snapshots,
                fast_indicator_snapshots=wiring.streams.fast_indicator_snapshots,
                quote_stream_seconds=QUOTE_STREAM_SECONDS,
                quote_stream_cleanup_grace_seconds=QUOTE_STREAM_CLEANUP_GRACE_SECONDS,
                websocket_heartbeat_seconds=WS_HEARTBEAT_SECONDS,
                chart_stream_key=wiring.streams.chart_stream_key,
                chart_stream_started=wiring.streams.chart_stream_started,
                chart_stream_finished=wiring.streams.chart_stream_finished,
                stream_bar_payload=wiring.streams.stream_bar_payload,
                stream_bar_signature=wiring.streams.stream_bar_signature,
                parse_iso_ts=wiring.streams.parse_iso_ts,
                parse_stream_ts=wiring.streams.parse_stream_ts,
                chart_recovery_snapshot=wiring.streams.chart_recovery_snapshot,
                record_chart_execution_snapshot=wiring.streams.record_chart_execution_snapshot,
                chart_stream_poll_seconds=CHART_STREAM_POLL_SECONDS,
                lookup_runtime_instrument=wiring.streams.instrument_lookup,
            )
        )
    )
    app.include_router(
        create_browser_capture_router(
            BrowserCaptureRouterDeps(
                lookup_runtime_instrument=wiring.streams.instrument_lookup,
            )
        )
    )
