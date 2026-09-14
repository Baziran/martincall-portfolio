from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from fastapi import HTTPException

from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.data.providers import async_load_provider_bars, route_instrument
from aef_terminal.domain import StrategyMode
from aef_terminal.engine.analysis_db import (
    EMA233_5M_WARMUP_BARS,
    EMA233_5M_WARMUP_RANGE,
)
from aef_terminal.engine.serialization import serialize_bar
from aef_terminal.engine.snapshot.builder import async_build_market_snapshot_from_db
from aef_terminal.engine.snapshot.chart_only import chart_only_market_snapshot
from aef_terminal.engine.snapshot.constants import SignalRange
from aef_terminal.engine.snapshot.empty import empty_market_snapshot
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    chart_bars_updated_generation,
    require_chart_bars_generation,
    wait_for_chart_bars_stable,
)
from aef_terminal.runtime.metrics import observe_metric
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.telemetry import log_structured_error
from aef_terminal.runtime.timeframes import (
    ChartHistoryRangeError,
    HistoryRangeWindow,
    chart_history_range_window,
)
from aef_terminal.ui.paper.constants import PAPER_MIN_RR
from aef_terminal.ui.route_selection import RouteSelectionMismatch
from aef_terminal.ui.routers.error_payloads import build_error_payload
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.services.chart_history import (
    coalesced_load_confirmed_chart_bars,
    load_confirmed_chart_history_page,
)
from aef_terminal.ui.services.market_analysis_compaction import (
    analysis_bar_window,
    compact_runtime_payload_for_response,
)
from aef_terminal.ui.services.market_analysis_job import hydrate_manual_channel_analysis_params

MARKET_ANALYSIS_CACHE_SCHEMA_VERSION = "canonical-analysis-window-v2"
MARKET_CHART_WRITE_SETTLE_SECONDS = 0.5
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketRequestParams:
    instrument_id: str = ""
    expected_route_fingerprint: str = ""
    interval: str = "5m"
    range_: str = "5d"
    signal_range: SignalRange = "2d"
    chart_only: bool = False
    queue_analysis: bool = False
    analysis_client_id: str = ""
    analysis_lease_sequence: int = 0
    global_atr_len: int = 14
    global_rvol_len: int = 30
    global_ema_pullback: int = 20
    global_ema_fast: int = 21
    global_ema_slow: int = 55
    global_ema_magnet: int = 233
    global_score_pre: float = 42.0
    global_score_watch: float = 58.0
    global_score_arm: float = 70.0
    global_score_go: float = 78.0
    global_rvol_low: float = 0.85
    global_rvol_elevated: float = 1.10
    global_rvol_high: float = 1.20
    global_rvol_climax: float = 1.60
    vsa_render_hours: int = 6
    strategy_mode: StrategyMode = StrategyMode.BALANCED
    signal_min_rr: float = PAPER_MIN_RR
    show_visuals: bool = True
    analysis_version: str = ""
    debug: bool = False
    gex_context_active: bool = False
    gex_capture_mode: GexCaptureMode = "request"
    indicator_query_params: Any = None


async def resolve_market_route_or_http_error(
    instrument_id: str,
    expected_route_fingerprint: str,
    *,
    interval: str,
    range_: str,
) -> tuple[dict[str, Any], InstrumentRoute]:
    try:
        requested_instrument = await run_physical_thread_call(
            lookup_runtime_instrument,
            instrument_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=build_error_payload(
                code="MARKET_WATCHLIST_UNAVAILABLE",
                category="market",
                retryable=True,
                error=exc,
                instrument_id=instrument_id,
                interval=interval,
                range=range_,
            ),
        ) from exc
    try:
        requested_route = route_instrument(requested_instrument)
        expected_route = require_exact_identity_text(
            expected_route_fingerprint,
            field="route_fingerprint",
        )
        if requested_route.fingerprint != expected_route:
            raise RouteSelectionMismatch(
                ((requested_route.instrument_id, expected_route),),
                ((requested_route.instrument_id, requested_route.fingerprint),),
            )
        return requested_instrument, requested_route
    except RouteSelectionMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail=build_error_payload(
                code=exc.code,
                category="market",
                retryable=True,
                error=exc,
                instrument_id=instrument_id,
                interval=interval,
                range=range_,
                expected_routes=exc.expected,
                actual_routes=exc.actual,
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=build_error_payload(
                code="MARKET_SOURCE_UNSUPPORTED",
                category="market",
                retryable=False,
                error=exc,
                instrument_id=instrument_id,
                interval=interval,
                range=range_,
            ),
        ) from exc


def market_analysis_version(
    snapshot: dict[str, Any], *, client_version: str = ""
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise TypeError("market snapshot must be a mapping")
    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        raise TypeError("market snapshot meta must be a mapping")
    if "bars" not in snapshot:
        raise ValueError("market snapshot bars are required")
    if not isinstance(client_version, str):
        raise TypeError("market analysis client version must be a string")
    window = analysis_bar_window(snapshot["bars"])
    version: dict[str, Any] = {
        "schema": MARKET_ANALYSIS_CACHE_SCHEMA_VERSION,
        "first_ts": window.first_ts,
        "latest_ts": window.latest_ts or meta.get("analysis_ts"),
        "latest_close": window.latest_close,
        "latest_volume": window.latest_volume,
        "latest_closed": True if window.count else None,
        "chart_bar_count": window.count,
        "window_hash": window.window_hash,
    }
    if client_version:
        version["client"] = client_version
    return {key: value for key, value in version.items() if value not in ("", None)}


def market_server_sleep_snapshot(
    deps: Any,
    requested_instrument: dict[str, Any],
    interval: str,
    *,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
) -> dict[str, Any]:
    message = "Server is sleeping. Market history/live refresh is paused."
    snapshot = empty_market_snapshot(
        requested_instrument,
        interval,
        message,
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
    )
    snapshot["meta"]["source"] = "server:sleep"
    snapshot["meta"]["server_sleep"] = deps.server_sleep_status()
    snapshot["meta"]["error"] = build_error_payload(
        code="SERVER_SLEEPING",
        category="market",
        retryable=True,
        error=message,
    )["error"]
    return snapshot


async def chart_only_market_response(
    deps: Any,
    params: MarketRequestParams,
    indicator_params: dict[str, Any],
    instrument: dict[str, Any],
    *,
    window: HistoryRangeWindow,
) -> dict[str, Any]:
    started = perf_counter()
    route = route_instrument(instrument)
    provider = route.provider
    chart_symbol = route.provider_symbol
    generation_error: ChartBarsGenerationChanged | None = None
    for attempt in range(2):
        load_started = perf_counter()
        try:
            store = deps.store_factory()
            history = await coalesced_load_confirmed_chart_bars(
                provider,
                instrument,
                params.interval,
                params.range_,
                timeout=2.0,
                store_factory=deps.store_factory,
                refresh_provider=False,
                window=window,
            )
            bars = history.bars
            require_chart_bars_generation(
                history.canonical_generation,
                params.interval,
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            guide_warmup_bars = []
            guide_warmup_failed = False
            if params.interval == "5m" and len(bars) < EMA233_5M_WARMUP_BARS:
                try:
                    guide_warmup_bars, _warning = await async_load_provider_bars(
                        route,
                        params.interval,
                        EMA233_5M_WARMUP_RANGE,
                        2.0,
                        store=store,
                    )
                except Exception as exc:
                    guide_warmup_failed = True
                    log_structured_error(
                        _LOGGER,
                        provider=provider,
                        symbol=chart_symbol,
                        interval=params.interval,
                        range_=EMA233_5M_WARMUP_RANGE,
                        op="read_chart_guide_warmup",
                        error=exc,
                        event="chart_guide_warmup_failed",
                        instrument_id=route.instrument_id,
                    )
            provider_warning = history.warning
            observe_metric(
                "market_chart_response_seconds",
                perf_counter() - load_started,
                phase="confirmed_bars",
                provider=provider,
                interval=params.interval,
            )
            assembly_started = perf_counter()
            snapshot = await run_physical_thread_call(
                chart_only_market_snapshot,
                instrument,
                chart_symbol,
                params.interval,
                params.range_,
                bars,
                provider_warning,
                history_coverage=history.coverage,
                canonical_generation=history.canonical_generation,
                signal_range=params.signal_range,
                data_provider=provider,
                store=store,
                indicator_params=indicator_params,
                guide_warmup_bars=guide_warmup_bars,
                guide_warmup_failed=guide_warmup_failed,
            )
            require_chart_bars_generation(
                history.canonical_generation,
                params.interval,
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            observe_metric(
                "market_chart_response_seconds",
                perf_counter() - assembly_started,
                phase="chart_snapshot",
                provider=provider,
                interval=params.interval,
            )
            break
        except ChartBarsGenerationChanged as exc:
            generation_error = exc
            if attempt == 0:
                if exc.write_in_progress:
                    try:
                        await wait_for_chart_bars_stable(
                            params.interval,
                            route.fingerprint,
                            MARKET_CHART_WRITE_SETTLE_SECONDS,
                            instrument_id=route.instrument_id,
                        )
                    except ChartBarsGenerationChanged:
                        pass
                continue
            raise HTTPException(
                status_code=503,
                detail=build_error_payload(
                    code="MARKET_CHART_GENERATION_CHANGED",
                    category="market",
                    retryable=True,
                    error=exc,
                    symbol=instrument["display"],
                    interval=params.interval,
                    range=params.range_,
                ),
            ) from exc
        except ChartHistoryRangeError as exc:
            raise HTTPException(
                status_code=422,
                detail=build_error_payload(
                    code=exc.code,
                    category="chart_history",
                    retryable=False,
                    error=exc,
                    interval=params.interval,
                    range=params.range_,
                ),
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=build_error_payload(
                    code="MARKET_CHART_LOAD_FAILED",
                    category="market",
                    retryable=True,
                    error=exc,
                    symbol=instrument["display"],
                    interval=params.interval,
                    range=params.range_,
                ),
            ) from exc
    else:
        raise AssertionError(f"unreachable chart generation retry state: {generation_error}")
    market_version = market_analysis_version(
        snapshot, client_version=str(params.analysis_version or "").strip()
    )
    analysis_payload = (
        deps.market_analysis_payload(
            source=provider,
            instrument_id=route.instrument_id,
            interval=params.interval,
            range_=params.range_,
            signal_range=params.signal_range,
            show_visuals=params.show_visuals,
            indicator_params=indicator_params,
            gex_context_active=params.gex_context_active,
            gex_capture_mode=params.gex_capture_mode,
            include_telemetry=params.debug,
            market_version=market_version,
            parent_canonical_generation=history.canonical_generation,
            instrument=instrument,
        )
        if market_version
        else deps.market_analysis_payload(
            source=provider,
            instrument_id=route.instrument_id,
            interval=params.interval,
            range_=params.range_,
            signal_range=params.signal_range,
            show_visuals=params.show_visuals,
            indicator_params=indicator_params,
            gex_context_active=params.gex_context_active,
            gex_capture_mode=params.gex_capture_mode,
            include_telemetry=params.debug,
            parent_canonical_generation=history.canonical_generation,
            instrument=instrument,
        )
    )
    analysis_key = deps.market_analysis_key(analysis_payload)
    snapshot.setdefault("meta", {})
    snapshot["meta"]["analysis_key"] = analysis_key
    if params.queue_analysis:
        analysis_client_id = str(params.analysis_client_id or "")
        if (
            not analysis_client_id
            or analysis_client_id != analysis_client_id.strip()
            or len(analysis_client_id) > 128
            or isinstance(params.analysis_lease_sequence, bool)
            or not isinstance(params.analysis_lease_sequence, int)
            or params.analysis_lease_sequence <= 0
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "analysis_client_id and a positive analysis_lease_sequence "
                    "are required when queue_analysis is true"
                ),
            )
        snapshot["meta"]["analysis_status"] = await deps.register_market_analysis_wanted(
            analysis_key,
            analysis_payload,
            analysis_client_id,
            params.analysis_lease_sequence,
        )
    observe_metric(
        "market_chart_response_seconds",
        perf_counter() - started,
        phase="total",
        provider=provider,
        interval=params.interval,
    )
    return compact_runtime_payload_for_response(
        snapshot,
        vsa_render_hours=params.vsa_render_hours,
    )


async def market_history_page_response(
    deps: Any,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    interval: str,
    before_ts: datetime,
    limit: int,
    expected_canonical_generation: int,
) -> dict[str, Any]:
    requested_instrument, requested_route = await resolve_market_route_or_http_error(
        instrument_id,
        expected_route_fingerprint,
        interval=interval,
        range_="history-page",
    )
    started = perf_counter()
    try:
        page = await load_confirmed_chart_history_page(
            requested_instrument,
            interval,
            before_ts=before_ts,
            limit=limit,
            expected_canonical_generation=expected_canonical_generation,
            store_factory=deps.store_factory,
        )
    except ChartBarsGenerationChanged as exc:
        raise HTTPException(
            status_code=409,
            detail=build_error_payload(
                code="MARKET_CHART_GENERATION_CHANGED",
                category="chart_history",
                retryable=True,
                error=exc,
                instrument_id=instrument_id,
                interval=interval,
            ),
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail=build_error_payload(
                code="MARKET_HISTORY_PAGE_TIMEOUT",
                category="chart_history",
                retryable=True,
                error=exc,
                instrument_id=instrument_id,
                interval=interval,
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=build_error_payload(
                code="MARKET_HISTORY_PAGE_FAILED",
                category="chart_history",
                retryable=True,
                error=exc,
                instrument_id=instrument_id,
                interval=interval,
            ),
        ) from exc
    observe_metric(
        "market_history_page_seconds",
        perf_counter() - started,
        provider=requested_route.provider,
        interval=interval,
        status="ok",
    )
    return {
        "bars": [serialize_bar(bar) for bar in page.bars],
        "instrument_id": requested_route.instrument_id,
        "route_fingerprint": requested_route.fingerprint,
        "timeframe": interval,
        "before_ts": before_ts.astimezone(UTC).isoformat(),
        "next_before_ts": (
            page.next_before_ts.astimezone(UTC).isoformat()
            if page.next_before_ts is not None
            else None
        ),
        "has_more": page.has_more,
        "canonical_generation": page.canonical_generation,
        "limit": limit,
    }


async def market_response(deps: Any, params: MarketRequestParams) -> dict[str, Any]:
    analysis_as_of_utc = datetime.now(UTC)
    try:
        requested_window = chart_history_range_window(
            params.range_,
            params.interval,
        )
    except ChartHistoryRangeError as exc:
        raise HTTPException(
            status_code=422,
            detail=build_error_payload(
                code=exc.code,
                category="chart_history",
                retryable=False,
                error=exc,
                instrument_id=params.instrument_id,
                interval=params.interval,
                range=params.range_,
            ),
        ) from exc
    requested_instrument, requested_route = await resolve_market_route_or_http_error(
        params.instrument_id,
        params.expected_route_fingerprint,
        interval=params.interval,
        range_=params.range_,
    )
    requested_provider_adapter = requested_route.adapter
    requested_provider = requested_route.provider
    deps.note_active_chart(requested_route.instrument_id, params.interval, params.range_)
    if requested_provider_adapter.capabilities.runtime_settings:
        await deps.apply_ibkr_runtime_settings_async()
    if deps.server_sleeping():
        return market_server_sleep_snapshot(
            deps,
            requested_instrument,
            params.interval,
            gex_context_active=params.gex_context_active,
            gex_capture_mode=params.gex_capture_mode,
        )
    generation_error: ChartBarsGenerationChanged | None = None
    for attempt in range(2):
        expected_generation = chart_bars_updated_generation(
            params.interval,
            requested_route.fingerprint,
            instrument_id=requested_route.instrument_id,
        )
        try:
            require_chart_bars_generation(
                expected_generation,
                params.interval,
                requested_route.fingerprint,
                instrument_id=requested_route.instrument_id,
            )
            store = deps.store_factory()
            indicator_params = deps.market_indicator_params(
                global_atr_len=params.global_atr_len,
                global_rvol_len=params.global_rvol_len,
                global_ema_pullback=params.global_ema_pullback,
                global_ema_fast=params.global_ema_fast,
                global_ema_slow=params.global_ema_slow,
                global_ema_magnet=params.global_ema_magnet,
                global_score_pre=params.global_score_pre,
                global_score_watch=params.global_score_watch,
                global_score_arm=params.global_score_arm,
                global_score_go=params.global_score_go,
                global_rvol_low=params.global_rvol_low,
                global_rvol_elevated=params.global_rvol_elevated,
                global_rvol_high=params.global_rvol_high,
                global_rvol_climax=params.global_rvol_climax,
                strategy_mode=params.strategy_mode,
                signal_min_rr=params.signal_min_rr,
                manual_channel_payload=None,
                manual_channel_canonical_generation=None,
                indicator_query_params=params.indicator_query_params,
            )
            if not params.chart_only:
                (
                    indicator_params,
                    _manual_channel_generation,
                ) = await hydrate_manual_channel_analysis_params(
                    indicator_params,
                    store=store,
                    instrument=requested_route.instrument,
                    interval=params.interval,
                    normalize_drawing_anchors=(deps.normalize_drawing_anchors),
                    expected_generation=expected_generation,
                )
            if params.chart_only:
                snapshot = await chart_only_market_response(
                    deps,
                    params,
                    indicator_params,
                    requested_instrument,
                    window=requested_window,
                )
            else:
                snapshot = await async_build_market_snapshot_from_db(
                    source=requested_provider,
                    interval=params.interval,
                    range_=params.range_,
                    signal_range_=params.signal_range,
                    show_visuals=params.show_visuals,
                    indicator_params=indicator_params,
                    gex_context_active=params.gex_context_active,
                    gex_capture_mode=params.gex_capture_mode,
                    include_telemetry=params.debug,
                    store=store,
                    instrument=requested_instrument,
                    window=requested_window,
                    analysis_as_of_utc=analysis_as_of_utc,
                )
            require_chart_bars_generation(
                expected_generation,
                params.interval,
                requested_route.fingerprint,
                instrument_id=requested_route.instrument_id,
            )
            return (
                snapshot
                if params.chart_only
                else compact_runtime_payload_for_response(
                    snapshot,
                    vsa_render_hours=params.vsa_render_hours,
                )
            )
        except ChartBarsGenerationChanged as exc:
            generation_error = exc
            if attempt == 0:
                if exc.write_in_progress:
                    try:
                        await wait_for_chart_bars_stable(
                            params.interval,
                            requested_route.fingerprint,
                            MARKET_CHART_WRITE_SETTLE_SECONDS,
                            instrument_id=requested_route.instrument_id,
                        )
                    except ChartBarsGenerationChanged:
                        pass
                continue
            raise HTTPException(
                status_code=503,
                detail=build_error_payload(
                    code="MARKET_CHART_GENERATION_CHANGED",
                    category="market",
                    retryable=True,
                    error=exc,
                    instrument_id=params.instrument_id,
                    interval=params.interval,
                    range=params.range_,
                ),
            ) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=build_error_payload(
                    code="MARKET_SNAPSHOT_FAILED",
                    category="market",
                    retryable=True,
                    error=exc,
                    instrument_id=params.instrument_id,
                    interval=params.interval,
                    range=params.range_,
                ),
            ) from exc
    raise AssertionError(f"unreachable market generation retry state: {generation_error}")
