from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.domain import StrategyMode
from aef_terminal.engine.snapshot.constants import SignalRange
from aef_terminal.engine.vsa_context import VsaVolumeRenderHours
from aef_terminal.indicators.settings_schema import global_settings_schema
from aef_terminal.runtime.metrics import observe_metric
from aef_terminal.ui.market_actions import (
    MarketRequestParams,
    market_history_page_response,
    market_response,
)
from aef_terminal.ui.paper.constants import PAPER_MIN_RR
from aef_terminal.ui.route_selection import RouteSelectionError, RouteSelectionMismatch
from aef_terminal.ui.routers.error_payloads import build_error_payload


@dataclass(frozen=True)
class MarketRouterDeps:
    store_factory: Callable[[], Any]
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]]
    apply_ibkr_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
    server_sleep_status: Callable[[], dict[str, Any]]
    note_active_chart: Callable[[str, str, str], None]
    market_indicator_params: Callable[..., dict[str, Any]]
    market_analysis_payload: Callable[..., dict[str, Any]]
    market_analysis_key: Callable[[dict[str, Any]], str]
    register_market_analysis_wanted: Callable[
        [str, dict[str, Any], str, int],
        Awaitable[str],
    ]
    renew_market_analysis_lease: Callable[
        [str, int, str, str, str],
        Awaitable[bool],
    ]
    refresh_market_analysis: Callable[
        [str, int, str, str, str, str],
        Awaitable[bool],
    ]
    release_market_analysis_lease: Callable[[str, int], Awaitable[bool]]
    market_analysis_snapshot: Callable[..., Awaitable[Any]]
    screener_snapshot: Callable[..., Awaitable[dict[str, Any]]]
    screener_trends_snapshot: Callable[..., Awaitable[dict[str, Any]]]


def attach_market_analysis_routes(
    router: APIRouter,
    deps: MarketRouterDeps,
) -> None:
    @router.get("/api/market/analysis")
    async def market_analysis(
        key: str = "",
        instrument_id: str = Query(..., min_length=1),
        expected_route_fingerprint: str = Query(..., min_length=1),
        wait_seconds: float = Query(0.0, ge=0.0, le=30.0),
        vsa_render_hours: VsaVolumeRenderHours = VsaVolumeRenderHours.SIX_HOURS,
    ) -> Any:
        started = perf_counter()
        response = await deps.market_analysis_snapshot(
            key,
            wait_seconds=wait_seconds,
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            vsa_render_hours=int(vsa_render_hours),
        )
        status = str(response.get("status") or "ready") if isinstance(response, dict) else "ready"
        observe_metric(
            "market_analysis_response_seconds",
            perf_counter() - started,
            status=status,
            waited=bool(wait_seconds),
        )
        return response

    @router.post("/api/market/analysis/lease")
    async def renew_market_analysis_lease(
        analysis_client_id: str = Query(..., min_length=1, max_length=128),
        analysis_lease_sequence: int = Query(..., ge=1),
        key: str = Query(..., min_length=1),
        instrument_id: str = Query(..., min_length=1),
        expected_route_fingerprint: str = Query(..., min_length=1),
    ) -> dict[str, bool]:
        return {
            "renewed": await deps.renew_market_analysis_lease(
                analysis_client_id,
                analysis_lease_sequence,
                key,
                instrument_id,
                expected_route_fingerprint,
            )
        }

    @router.post("/api/market/analysis/release")
    async def release_market_analysis_lease(
        analysis_client_id: str = Query(..., min_length=1, max_length=128),
        analysis_lease_sequence: int = Query(..., ge=1),
    ) -> dict[str, bool]:
        return {
            "released": await deps.release_market_analysis_lease(
                analysis_client_id,
                analysis_lease_sequence,
            )
        }

    @router.post("/api/market/analysis/refresh")
    async def refresh_market_analysis(
        analysis_client_id: str = Query(..., min_length=1, max_length=128),
        analysis_lease_sequence: int = Query(..., ge=1),
        key: str = Query(..., min_length=1),
        instrument_id: str = Query(..., min_length=1),
        expected_route_fingerprint: str = Query(..., min_length=1),
        analysis_version: str = Query("", max_length=512),
    ) -> dict[str, bool]:
        return {
            "refreshed": await deps.refresh_market_analysis(
                analysis_client_id,
                analysis_lease_sequence,
                key,
                instrument_id,
                expected_route_fingerprint,
                analysis_version,
            )
        }


def attach_market_history_routes(
    router: APIRouter,
    deps: MarketRouterDeps,
) -> None:
    @router.get("/api/market/history-page")
    async def market_history_page(
        instrument_id: str = Query(..., min_length=1),
        expected_route_fingerprint: str = Query(..., min_length=1),
        interval: str = Query("5m", pattern="^(1m|3m|5m|15m|60m)$"),
        before_ts: datetime = Query(...),
        limit: int = Query(600, ge=100, le=1000),
        expected_canonical_generation: int = Query(..., ge=0),
    ) -> dict[str, Any]:
        if before_ts.tzinfo is None or before_ts.utcoffset() is None:
            raise HTTPException(
                status_code=422,
                detail=build_error_payload(
                    code="MARKET_HISTORY_PAGE_TIMESTAMP_REQUIRED",
                    category="chart_history",
                    retryable=False,
                    error="before_ts must include a UTC offset",
                    instrument_id=instrument_id,
                    interval=interval,
                ),
            )
        return await market_history_page_response(
            deps,
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            interval=interval,
            before_ts=before_ts,
            limit=limit,
            expected_canonical_generation=expected_canonical_generation,
        )


def create_market_router(deps: MarketRouterDeps) -> APIRouter:
    router = APIRouter()
    attach_market_analysis_routes(router, deps)
    attach_market_history_routes(router, deps)

    @router.get("/api/indicator-settings/schema")
    async def indicator_settings_schema() -> dict[str, Any]:
        return global_settings_schema()

    @router.get("/api/market")
    async def market(
        request: Request,
        instrument_id: str,
        expected_route_fingerprint: str,
        response: Response = None,
        interval: str = "5m",
        range_: str = Query("5d", alias="range"),
        signal_range: SignalRange = "2d",
        chart_only: bool = False,
        queue_analysis: bool = False,
        analysis_client_id: str = "",
        analysis_lease_sequence: int = 0,
        global_atr_len: int = 14,
        global_rvol_len: int = 30,
        global_ema_pullback: int = 20,
        global_ema_fast: int = 21,
        global_ema_slow: int = 55,
        global_ema_magnet: int = 233,
        global_score_pre: float = 42.0,
        global_score_watch: float = 58.0,
        global_score_arm: float = 70.0,
        global_score_go: float = 78.0,
        global_rvol_low: float = 0.85,
        global_rvol_elevated: float = 1.10,
        global_rvol_high: float = 1.20,
        global_rvol_climax: float = 1.60,
        vsa_render_hours: VsaVolumeRenderHours = VsaVolumeRenderHours.SIX_HOURS,
        strategy_mode: StrategyMode = StrategyMode.BALANCED,
        signal_min_rr: float = PAPER_MIN_RR,
        show_visuals: bool = True,
        analysis_version: str = "",
        debug: bool = False,
        gex_context_active: bool = False,
        gex_capture_mode: GexCaptureMode = "request",
    ) -> dict[str, Any]:
        started = perf_counter()
        try:
            return await market_response(
                deps,
                MarketRequestParams(
                    instrument_id=instrument_id,
                    expected_route_fingerprint=expected_route_fingerprint,
                    interval=interval,
                    range_=range_,
                    signal_range=signal_range,
                    chart_only=chart_only,
                    queue_analysis=queue_analysis,
                    analysis_client_id=analysis_client_id,
                    analysis_lease_sequence=analysis_lease_sequence,
                    global_atr_len=global_atr_len,
                    global_rvol_len=global_rvol_len,
                    global_ema_pullback=global_ema_pullback,
                    global_ema_fast=global_ema_fast,
                    global_ema_slow=global_ema_slow,
                    global_ema_magnet=global_ema_magnet,
                    global_score_pre=global_score_pre,
                    global_score_watch=global_score_watch,
                    global_score_arm=global_score_arm,
                    global_score_go=global_score_go,
                    global_rvol_low=global_rvol_low,
                    global_rvol_elevated=global_rvol_elevated,
                    global_rvol_high=global_rvol_high,
                    global_rvol_climax=global_rvol_climax,
                    vsa_render_hours=int(vsa_render_hours),
                    strategy_mode=strategy_mode,
                    signal_min_rr=signal_min_rr,
                    show_visuals=show_visuals,
                    analysis_version=analysis_version,
                    debug=debug,
                    gex_context_active=gex_context_active,
                    gex_capture_mode=gex_capture_mode,
                    indicator_query_params=request.query_params,
                ),
            )
        finally:
            if response is not None:
                response.headers["Server-Timing"] = (
                    f"market;dur={(perf_counter() - started) * 1000.0:.3f}"
                )

    @router.get("/api/screener")
    async def screener(
        routes: str,
        interval: str = "5m",
    ) -> dict[str, Any]:
        try:
            return await deps.screener_snapshot(
                routes=routes,
                interval=interval,
            )
        except RouteSelectionMismatch as exc:
            raise HTTPException(
                status_code=409,
                detail=build_error_payload(
                    code=exc.code,
                    category="market",
                    retryable=True,
                    error=exc,
                    expected_routes=exc.expected,
                    actual_routes=exc.actual,
                ),
            ) from exc
        except RouteSelectionError as exc:
            raise HTTPException(
                status_code=422,
                detail=build_error_payload(
                    code=exc.code,
                    category="market",
                    retryable=False,
                    error=exc,
                ),
            ) from exc

    @router.get("/api/screener/trends")
    async def screener_trends(routes: str) -> dict[str, Any]:
        try:
            return await deps.screener_trends_snapshot(routes=routes)
        except RouteSelectionMismatch as exc:
            raise HTTPException(
                status_code=409,
                detail=build_error_payload(
                    code=exc.code,
                    category="market",
                    retryable=True,
                    error=exc,
                    expected_routes=exc.expected,
                    actual_routes=exc.actual,
                ),
            ) from exc
        except RouteSelectionError as exc:
            raise HTTPException(
                status_code=422,
                detail=build_error_payload(
                    code=exc.code,
                    category="market",
                    retryable=False,
                    error=exc,
                ),
            ) from exc

    return router
