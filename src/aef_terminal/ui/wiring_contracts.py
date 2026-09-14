from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aef_terminal.ui.runtime.quote_stream import QuoteRouteSnapshot


@dataclass(frozen=True)
class AssetWiring:
    martincall_html_async: Callable[..., Any]
    martincall_js_delivery_async: Callable[..., Any]
    martincall_css_delivery_async: Callable[..., Any]
    martincall_quote_worker_delivery_async: Callable[..., Any]
    martincall_js_gzip_async: Callable[..., Any]
    martincall_css_gzip_async: Callable[..., Any]
    martincall_quote_worker_gzip_async: Callable[..., Any]


@dataclass(frozen=True)
class SystemWiring:
    health_status: Callable[[], dict[str, Any]]
    readiness_status: Callable[[], dict[str, Any]]
    system_status: Callable[[], dict[str, Any]]
    memory_status: Callable[..., dict[str, Any]]
    set_server_sleeping: Callable[..., Any]
    apply_ibkr_runtime_settings: Callable[..., Any]
    backend_restart_record: Callable[..., Any]
    flush_log_handlers: Callable[[], None]


@dataclass(frozen=True)
class EconomicCalendarWiring:
    snapshot: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class TelegramWiring:
    set_telegram_interactive_enabled: Callable[..., Any]
    paper_telegram_feed_status: Callable[[], dict[str, Any]]
    telegram_interactive_status: Callable[[], dict[str, Any]]
    store_factory: Callable[[], Any]


@dataclass(frozen=True)
class IbkrWiring:
    apply_ibkr_runtime_settings: Callable[..., Any]
    apply_ibkr_runtime_settings_async: Callable[..., Any]
    combined_quote_instruments: Callable[..., Any]
    ibkr_gateway_login_control_status: Callable[[], dict[str, Any]]
    request_ibkr_gateway_login: Callable[[], dict[str, Any]]
    server_sleeping: Callable[[], bool]
    server_sleep_status: Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class ReferenceWiring:
    reconcile_client_settings: Callable[[dict[str, Any], dict[str, dict[str, Any]], int], None]
    reconcile_gex_scheduler_settings: Callable[[Any, bool, int], dict[str, Any]]
    reconcile_option_target_caps_settings: Callable[[Any, bool, int], dict[str, float]]
    data_provider_catalog: Callable[..., Any]
    search_provider_instruments: Callable[..., Any]
    bind_provider_instrument: Callable[..., Any]
    bind_provider_future_root: Callable[[str, str, dict[str, Any] | None], dict[str, Any] | None]
    store_factory: Callable[[], Any]
    refresh_quote_routes: Callable[..., Any]


@dataclass(frozen=True)
class MarketWiring:
    store_factory: Callable[[], Any]
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]]
    apply_ibkr_runtime_settings_async: Callable[..., Any]
    server_sleeping: Callable[[], bool]
    server_sleep_status: Callable[[], dict[str, Any]]
    note_active_chart: Callable[[str, str, str], None]
    market_indicator_params: Callable[..., dict[str, Any]]
    market_analysis_payload: Callable[..., Any]
    market_analysis_key: Callable[..., str]
    register_market_analysis_wanted: Callable[..., None]
    renew_market_analysis_lease: Callable[..., Any]
    refresh_market_analysis: Callable[..., Any]
    release_market_analysis_lease: Callable[..., Any]
    market_analysis_snapshot: Callable[..., Any]
    screener_snapshot: Callable[..., Any]
    screener_trends_snapshot: Callable[..., Any]


@dataclass(frozen=True)
class TradingWiring:
    store_factory: Callable[[], Any]
    paper_config: Callable[[], dict[str, Any]]
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None]
    paper_trade_enriched: Callable[[dict[str, Any]], dict[str, Any]]
    paper_trade_stats: Callable[[list[dict[str, Any]]], dict[str, Any]]
    paper_replay_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    paper_signal_source_key: Callable[[dict[str, Any]], str]
    paper_setup_source_label: Callable[[dict[str, Any]], str]
    paper_setup_key: Callable[[dict[str, Any]], str]
    paper_trade_session: Callable[[dict[str, Any]], str]
    paper_trade_edge_action: Callable[[dict[str, Any]], str]
    paper_order_price_snapshot: Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class StorageWiring:
    store_factory: Callable[[], Any]
    apply_ibkr_runtime_settings: Callable[..., Any]
    publish_client_settings_mutations: Callable[[dict[str, dict[str, Any]], int], None]
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]]
    require_unique_price_alerts: Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True)
class GexWiring:
    apply_ibkr_runtime_settings: Callable[..., Any]
    apply_ibkr_runtime_settings_async: Callable[..., Any]
    exception_message: Callable[[BaseException], str]
    gex_scheduler_runtime_settings: Callable[[], dict[str, Any]]
    save_gex_scheduler_settings: Callable[[dict[str, Any]], dict[str, Any]]
    gex_live_status: Callable[[], dict[str, Any]]
    instrument_lookup: Callable[[str], dict[str, Any]]
    option_target_caps_settings: Callable[[], dict[str, float]]
    save_option_target_caps_settings: Callable[[dict[str, float]], dict[str, float]]
    parse_iso_ts: Callable[[str | None], datetime | None]
    quote_cache_for_instruments: Callable[..., Any]
    server_sleeping: Callable[[], bool]
    store_factory: Callable[[], Any]


@dataclass(frozen=True)
class StreamWiring:
    logger: Any
    store_factory: Callable[[], Any]
    apply_ibkr_runtime_settings_async: Callable[..., Any]
    server_sleeping: Callable[[], bool]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    set_quote_stream_wanted: Callable[..., None]
    quote_stream_started: Callable[..., None]
    quote_stream_finished: Callable[..., None]
    screener_bases: Callable[..., dict[str, dict[str, Any]]]
    screener_rows: Callable[..., list[dict[str, Any]]]
    quote_cache_for_instruments: Callable[..., Any]
    record_quote_execution_snapshots: Callable[[list[dict[str, Any]]], None]
    fast_indicator_snapshots: Callable[
        [list[dict[str, Any]], str],
        list[dict[str, Any]] | None,
    ]
    chart_stream_key: Callable[[str, str, str, str], tuple[str, str, str, str]]
    chart_stream_started: Callable[..., None]
    chart_stream_finished: Callable[..., None]
    stream_bar_payload: Callable[..., dict[str, Any]]
    stream_bar_signature: Callable[[dict[str, Any]], tuple[Any, ...]]
    parse_stream_ts: Callable[[dict[str, Any] | None], datetime | None]
    chart_recovery_snapshot: Callable[..., Any]
    record_chart_execution_snapshot: Callable[..., None]
    instrument_lookup: Callable[[str], dict[str, Any]]
    parse_iso_ts: Callable[[str | None], datetime | None]


@dataclass(frozen=True)
class AppRouterWiring:
    assets: AssetWiring
    system: SystemWiring
    economic_calendar: EconomicCalendarWiring
    telegram: TelegramWiring
    ibkr: IbkrWiring
    reference: ReferenceWiring
    market: MarketWiring
    trading: TradingWiring
    storage: StorageWiring
    gex: GexWiring
    streams: StreamWiring
