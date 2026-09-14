import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from fastapi import APIRouter, Body

from dataclasses import replace
from aef_terminal.data.gex.constants import (
    GEX_CHART_HISTORY_HOURS,
    GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES,
    GEX_REQUEST_SNAPSHOT_SOURCE,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.provider_contract import (
    InstrumentRoute,
    OptionUniverseUnavailableError,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.snapshot.db_context import require_provider_chart_axis_point
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.ui.gex_actions import (
    gex_validation_error,
    save_gex_scheduler_settings,
    save_option_settings,
)
from aef_terminal.ui.gex_projection import project_gex_context_for_chart
from aef_terminal.ui.routers.error_payloads import build_action_error_response
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision
from aef_terminal.ui.services.option_target_reprice import (
    option_target_underlying_quote,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GexRouterDeps:
    apply_ibkr_runtime_settings: Callable[[], dict[str, Any]]
    apply_ibkr_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    exception_message: Callable[[BaseException], str]
    gex_live_status: Callable[[], dict[str, Any]]
    gex_scheduler_runtime_settings: Callable[[], dict[str, Any]]
    save_gex_scheduler_settings: Callable[[dict[str, Any]], dict[str, Any]]
    instrument_lookup: Callable[[str], dict[str, Any]]
    option_target_caps_settings: Callable[[], dict[str, float]]
    save_option_target_caps_settings: Callable[[dict[str, float]], dict[str, float]]
    parse_iso_ts: Callable[[str | None], datetime | None]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]
    server_sleeping: Callable[[], bool]
    store_factory: Callable[[], Any]
    now_iso: Callable[[], str]
    manual_refresh_owner: GexManualRefreshOwner
    provider_router: Callable[[dict[str, Any]], InstrumentRoute] = route_instrument


def _instrument_feature_route(
    deps: GexRouterDeps,
    instrument_id: str,
    *,
    enabled: bool,
    feature: str,
    expected_route_fingerprint: str = "",
) -> tuple[str | None, InstrumentRoute | None, dict[str, Any] | None]:
    if feature not in {"gex", "options"}:
        raise ValueError("provider feature must be exactly gex or options")
    feature_label = "GEX" if feature == "gex" else "Option"
    code_prefix = "GEX" if feature == "gex" else "OPTION"
    provider_symbol: str | None = None
    try:
        exact_instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route = deps.provider_router(deps.instrument_lookup(exact_instrument_id))
        expected_route = require_exact_identity_text(
            expected_route_fingerprint,
            field="route_fingerprint",
            allow_empty=True,
        )
        if expected_route and route.fingerprint != expected_route:
            raise ValueError(
                f"{code_prefix}_ROUTE_CHANGED expected={expected_route} actual={route.fingerprint}"
            )
        if not bool(getattr(route.adapter.capabilities, feature)):
            raise ValueError(
                f"{code_prefix}_PROVIDER_UNSUPPORTED "
                f"provider={route.provider} instrument_key={route.instrument_key}"
            )
        provider_symbol = route.provider_symbol
        return provider_symbol, route, None
    except Exception as exc:
        return (
            provider_symbol,
            None,
            {
                "checked_at": deps.now_iso(),
                "enabled": enabled,
                "provider_symbol": provider_symbol,
                "instrument_id": instrument_id,
                "route_fingerprint": expected_route_fingerprint,
                "route_qualified": False,
                "status": "error",
                "levels": [],
                **gex_validation_error(
                    f"{code_prefix}_INSTRUMENT_IDENTITY_ERROR",
                    f"{feature_label} instrument identity is unavailable: {exc}",
                    category="options" if feature == "options" else "gex",
                    retryable=False,
                ),
            },
        )


def _gex_refresh_outcome(payload: Any, baseline: dict[str, Any] | None = None) -> str:
    if not isinstance(payload, dict):
        return "error"
    status = payload.get("status")
    if not isinstance(status, str) or not status or status != status.strip():
        return "error"
    if status == "refresh_blocked":
        return "blocked"
    if status == "unavailable":
        return "unavailable"
    if status in {"error", "refresh_error", "failed"}:
        return "error"
    if (
        payload.get("ok") is not True
        or payload.get("frame_complete") is not True
        or status not in {"ok", "degraded"}
    ):
        return "rejected"
    result_captured_at = payload.get("captured_at")
    baseline_captured_at = (baseline or {}).get("captured_at")
    if (
        not isinstance(result_captured_at, str)
        or not result_captured_at
        or result_captured_at != result_captured_at.strip()
        or result_captured_at == baseline_captured_at
    ):
        return "rejected"
    if payload.get("decision_authoritative") is True:
        return "published"
    if payload.get("market_data_entitlement") in {
        "frozen",
        "delayed",
        "delayed_frozen",
    }:
        return "published_display_only"
    return "rejected"


def _gex_snapshot_shape(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    request_meta = (
        payload.get("request_meta") if isinstance(payload.get("request_meta"), dict) else {}
    )
    visibility = (
        payload.get("visibility_summary", {})
        if isinstance(payload.get("visibility_summary"), dict)
        else {}
    )
    selected_strike_count = request_meta.get("strike_count")
    requested_strike_limit = request_meta.get("requested_strike_limit")
    raw_strike_count = visibility.get("raw_strike_count")
    nonzero_strike_count = visibility.get("nonzero_strike_count")
    output_level_count = visibility.get("output_level_count")
    return {
        "captured_at": payload.get("captured_at")
        if isinstance(payload.get("captured_at"), str)
        else None,
        "source": payload.get("source") if isinstance(payload.get("source"), str) else None,
        "status": payload.get("status") if isinstance(payload.get("status"), str) else None,
        "frame_complete": payload.get("frame_complete")
        if type(payload.get("frame_complete")) is bool
        else None,
        "decision_authoritative": payload.get("decision_authoritative")
        if type(payload.get("decision_authoritative")) is bool
        else None,
        "market_data_entitlement": payload.get("market_data_entitlement")
        if isinstance(payload.get("market_data_entitlement"), str)
        else None,
        "refresh_mode": request_meta.get("refresh_mode")
        if isinstance(request_meta.get("refresh_mode"), str)
        else None,
        "selected_strike_count": selected_strike_count
        if type(selected_strike_count) is int and selected_strike_count > 0
        else None,
        "requested_strike_limit": requested_strike_limit
        if type(requested_strike_limit) is int and requested_strike_limit > 0
        else None,
        "raw_strike_count": raw_strike_count
        if type(raw_strike_count) is int and raw_strike_count >= 0
        else None,
        "nonzero_strike_count": nonzero_strike_count
        if type(nonzero_strike_count) is int and nonzero_strike_count >= 0
        else None,
        "output_level_count": output_level_count
        if type(output_level_count) is int and output_level_count >= 0
        else None,
    }


def _gex_refresh_request_payload(
    *,
    status: str,
    requested: dict[str, Any],
    cached: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "status": status,
        "requested": dict(requested),
    }
    if cached is not None:
        out["showing_cached"] = True
        out["cached"] = _gex_snapshot_shape(cached)
    if result is not None:
        out["result"] = _gex_snapshot_shape(result)
    return out


class GexManualRefreshOwner:
    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], asyncio.Task[dict[str, Any]]] = {}
        self._results: dict[tuple[str, str], dict[str, Any]] = {}
        self._accepting = True

    def start_lifecycle(self) -> None:
        if self._tasks:
            raise RuntimeError("GEX_MANUAL_REFRESH_START_WITH_ACTIVE_TASKS")
        self._results.clear()
        self._accepting = True

    def state(self, refresh_key: tuple[str, str]) -> tuple[bool, dict[str, Any]]:
        task = self._tasks.get(refresh_key)
        return (
            task is not None and not task.done(),
            self._results.get(refresh_key, {}),
        )

    def start_refresh(
        self,
        refresh_key: tuple[str, str],
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        initial_result: dict[str, Any],
        now_iso: Callable[[], str],
    ) -> bool:
        if not self._accepting:
            raise RuntimeError("GEX_MANUAL_REFRESH_OWNER_STOPPING")
        running = self._tasks.get(refresh_key)
        if running is not None and not running.done():
            return False
        task = asyncio.create_task(
            operation(),
            name=f"gex-manual-refresh:{refresh_key[0]}:{refresh_key[1]}",
        )
        self._tasks[refresh_key] = task
        self._results[refresh_key] = initial_result
        task.add_done_callback(
            lambda completed, key=refresh_key: self._finish_refresh(
                key,
                completed,
                now_iso=now_iso,
            )
        )
        return True

    def _finish_refresh(
        self,
        refresh_key: tuple[str, str],
        task: asyncio.Task[dict[str, Any]],
        *,
        now_iso: Callable[[], str],
    ) -> None:
        if self._tasks.get(refresh_key) is task:
            self._tasks.pop(refresh_key, None)
        if task.cancelled():
            self._results[refresh_key] = {
                "refresh_status": "cancelled",
                "refresh_error": "GEX refresh was cancelled.",
                "refresh_finished_at": now_iso(),
            }
            return
        try:
            payload = task.result()
        except Exception as error:
            LOGGER.warning("GEX manual refresh task failed: %s", error)
            self._results[refresh_key] = {
                "refresh_status": "error",
                "refresh_error": str(error),
                "refresh_finished_at": now_iso(),
            }
            return
        previous = self._results.get(refresh_key, {})
        baseline = (
            previous.get("refresh_baseline")
            if isinstance(previous.get("refresh_baseline"), dict)
            else {}
        )
        outcome = _gex_refresh_outcome(payload, baseline)
        refresh_message = (
            ""
            if outcome in {"published", "published_display_only"}
            else str(payload.get("message") or "GEX refresh returned no new publishable snapshot.")
        )
        provisional_payload = (
            payload
            if outcome == "rejected"
            and payload.get("status") == "partial"
            and payload.get("decision_authoritative") is False
            and isinstance(payload.get("levels"), list)
            and bool(payload["levels"])
            else None
        )
        self._results[refresh_key] = {
            "refresh_status": outcome,
            "refresh_error": refresh_message,
            "refresh_finished_at": now_iso(),
            "refresh_payload": provisional_payload,
            "refresh_request": _gex_refresh_request_payload(
                status=outcome,
                requested=dict(previous.get("refresh_request", {}).get("requested") or {}),
                result=payload,
            ),
        }

    async def shutdown(self) -> None:
        self._accepting = False
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._results.clear()


GEX_MANUAL_REFRESH_OWNER = GexManualRefreshOwner()


def _api_option_target_price(
    deps: GexRouterDeps,
    instrument_id: str,
    expected_route_fingerprint: str = "",
    target_price: float = 0.0,
    target_ts: str | None = None,
    timeframe: str = "",
    right: str = "auto",
    mode: str = "normal",
    dte: str = "1dte",
    strike: float | None = None,
    expiry: str | None = None,
    expiry_at: str | None = None,
    con_id: str | None = None,
    local_symbol: str | None = None,
    exchange: str | None = None,
    trading_class: str | None = None,
    multiplier: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    deps.apply_ibkr_runtime_settings()
    if deps.server_sleeping():
        return gex_validation_error(
            "OPTION_TARGET_SERVER_SLEEPING",
            "Server is sleeping. Option target pricing is paused.",
            category="options",
            retryable=True,
            target_dte=dte,
        )
    try:
        _asset, route, route_error = _instrument_feature_route(
            deps,
            instrument_id,
            enabled=True,
            feature="options",
            expected_route_fingerprint=expected_route_fingerprint,
        )
        if route_error is not None or route is None:
            return {
                **(
                    route_error
                    or gex_validation_error(
                        "OPTION_TARGET_INSTRUMENT_IDENTITY_ERROR",
                        "Option target instrument identity is unavailable.",
                        category="options",
                    )
                ),
                "target_dte": dte,
            }
        underlying_price, underlying_quote = option_target_underlying_quote(
            route.instrument,
            route.fingerprint,
            deps.quote_cache_for_instruments,
        )
        parsed_target_ts = deps.parse_iso_ts(target_ts)
        store = deps.store_factory()
        validated_target_slot: int | None = None
        if target_ts is not None:
            if parsed_target_ts is None or not timeframe:
                raise ValueError("option target chart point requires timeframe and timestamp")
            validated_target_slot = require_provider_chart_axis_point(
                store,
                route.instrument,
                timeframe,
                parsed_target_ts,
            )
        payload = route.adapter.option_target_price(
            route.instrument,
            target_price=target_price,
            target_ts=parsed_target_ts,
            right=right,
            mode=mode,
            dte=dte,
            strike=strike,
            expiry=expiry,
            expiry_at=expiry_at,
            con_id=con_id,
            local_symbol=local_symbol,
            exchange=exchange,
            trading_class=trading_class,
            multiplier=multiplier,
            currency=currency,
            underlying_price=underlying_price,
            underlying_quote=underlying_quote,
            premium_caps=deps.option_target_caps_settings(),
            store=store,
        )
        response_scope = {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "timeframe": timeframe or None,
            "target_bar_slot": validated_target_slot,
            "target_dte": dte,
        }
        raw_alternatives = payload.get("alternatives")
        scoped_alternatives = (
            [
                {
                    **alternative,
                    **response_scope,
                }
                for alternative in raw_alternatives
                if isinstance(alternative, dict)
            ]
            if isinstance(raw_alternatives, list)
            else None
        )
        return {
            **payload,
            **response_scope,
            **({"alternatives": scoped_alternatives} if scoped_alternatives is not None else {}),
        }
    except OptionUniverseUnavailableError as exc:
        return build_action_error_response(
            code="OPTION_CONTRACT_UNIVERSE_UNAVAILABLE",
            category="options",
            retryable=True,
            error=(f"Option target pricing is unavailable: {deps.exception_message(exc)}"),
            instrument_id=instrument_id,
            status="unavailable",
            target_dte=dte,
        )
    except Exception as exc:
        return build_action_error_response(
            code="OPTION_TARGET_PRICE_ERROR",
            category="options",
            retryable=True,
            error=f"Option target pricing failed: {deps.exception_message(exc)}",
            instrument_id=instrument_id,
            target_dte=dte,
        )


def create_gex_router(deps: GexRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/gex/scheduler")
    def api_gex_scheduler_status() -> dict[str, Any]:
        return {
            "checked_at": deps.now_iso(),
            **deps.gex_scheduler_runtime_settings(),
        }

    @router.put("/api/gex/scheduler")
    def api_gex_scheduler_update(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return save_gex_scheduler_settings(deps, payload)

    @router.get("/api/options/settings")
    def api_option_settings() -> dict[str, Any]:
        return {"ok": True, "premium_caps": deps.option_target_caps_settings()}

    @router.put("/api/options/settings")
    def api_option_settings_update(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return save_option_settings(deps, payload)

    @router.get("/api/gex")
    async def api_gex(
        instrument_id: str,
        expected_route_fingerprint: str = "",
        enabled: bool = False,
        refresh: bool = False,
    ) -> dict[str, Any]:
        await deps.apply_ibkr_runtime_settings_async()
        if deps.server_sleeping():
            return {
                "checked_at": deps.now_iso(),
                "enabled": enabled,
                "instrument_id": instrument_id,
                "route_fingerprint": expected_route_fingerprint,
                "status": "sleeping",
                "levels": [],
                **gex_validation_error(
                    "GEX_SERVER_SLEEPING",
                    "Server is sleeping. GEX refresh is paused.",
                    retryable=True,
                ),
            }
        request = None
        provider_symbol, route, route_error = await run_physical_thread_call(
            _instrument_feature_route,
            deps,
            instrument_id,
            enabled=True,
            feature="gex",
            expected_route_fingerprint=expected_route_fingerprint,
        )
        if route_error is not None:
            return route_error
        assert route is not None
        refresh_key = (route.instrument_id, route.fingerprint)
        if refresh:
            live_status = deps.gex_live_status()
            live_route = next(
                (
                    session
                    for session in live_status.get("sessions", [])
                    if isinstance(session, dict)
                    and session.get("instrument_id") == route.instrument_id
                    and session.get("route_fingerprint") == route.fingerprint
                ),
                None,
            )
            if live_route is not None:
                return {
                    "checked_at": deps.now_iso(),
                    "enabled": enabled,
                    "instrument_id": route.instrument_id,
                    "route_fingerprint": route.fingerprint,
                    "provider_symbol": provider_symbol,
                    "status": "unavailable",
                    "levels": [],
                    "active_capture_mode": "live",
                    "requested_capture_mode": "request",
                    **gex_validation_error(
                        "GEX_CAPTURE_MODE_CONFLICT",
                        "Stop live GEX capture before requesting a snapshot for this route.",
                        retryable=False,
                    ),
                }
        refresh_running, refresh_result = deps.manual_refresh_owner.state(refresh_key)
        store = await run_physical_thread_call(deps.store_factory)
        if refresh and provider_symbol is not None:
            base_request = await run_physical_thread_call(
                route.adapter.gex_request_config,
                route.instrument,
                store=store,
            )
            requested_refresh = {
                "provider_symbol": provider_symbol,
                "requested_strike_limit": base_request.strike_count,
                "max_levels": GEX_CONTEXT_MAX_LEVELS,
                "stale_minutes": GEX_CONTEXT_STALE_MINUTES,
                "history_hours": GEX_CHART_HISTORY_HOURS,
            }
            request = replace(
                base_request,
                refresh_mode="manual",
                scheduler_lane="manual",
            )
            cached = await route.adapter.async_load_gex(
                route.instrument,
                enabled=enabled,
                refresh=False,
                max_levels=GEX_CONTEXT_MAX_LEVELS,
                stale_minutes=GEX_CONTEXT_STALE_MINUTES,
                history_hours=GEX_CHART_HISTORY_HOURS,
                request=request,
                store=store,
            )
            if not refresh_running:

                async def _run_manual_gex_refresh() -> dict[str, Any]:
                    return await route.adapter.async_load_gex(
                        route.instrument,
                        enabled=enabled,
                        refresh=True,
                        max_levels=GEX_CONTEXT_MAX_LEVELS,
                        stale_minutes=GEX_CONTEXT_STALE_MINUTES,
                        history_hours=GEX_CHART_HISTORY_HOURS,
                        request=request,
                        store=store,
                    )

                refresh_running = deps.manual_refresh_owner.start_refresh(
                    refresh_key,
                    _run_manual_gex_refresh,
                    initial_result={
                        "refresh_status": "running",
                        "refresh_error": "",
                        "refresh_finished_at": "",
                        "refresh_baseline": _gex_snapshot_shape(cached),
                        "refresh_request": _gex_refresh_request_payload(
                            status="running",
                            requested=requested_refresh,
                            cached=cached,
                        ),
                    },
                    now_iso=deps.now_iso,
                )
            cached_chart = await run_physical_thread_call(
                project_gex_context_for_chart,
                cached,
            )
            return {
                "checked_at": deps.now_iso(),
                **cached_chart,
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "refresh_queued": True,
                "refresh_running": refresh_running,
                "refresh_status": "running" if refresh_running else "queued",
                "refresh_error": str(refresh_result.get("refresh_error") or ""),
                "refresh_finished_at": str(refresh_result.get("refresh_finished_at") or ""),
                "refresh_request": _gex_refresh_request_payload(
                    status="running" if refresh_running else "queued",
                    requested=requested_refresh,
                    cached=cached,
                ),
                "message": f"GEX refresh queued in broker lane; showing latest cached snapshot. {cached.get('message') or ''}".strip(),
            }
        payload = await route.adapter.async_load_gex(
            route.instrument,
            enabled=enabled,
            refresh=refresh,
            max_levels=GEX_CONTEXT_MAX_LEVELS,
            stale_minutes=GEX_CONTEXT_STALE_MINUTES,
            history_hours=GEX_CHART_HISTORY_HOURS,
            request=request,
            store=store,
        )
        provisional_payload = refresh_result.get("refresh_payload")
        provisional_captured_at = deps.parse_iso_ts(
            provisional_payload.get("captured_at")
            if isinstance(provisional_payload, dict)
            else None
        )
        persisted_captured_at = deps.parse_iso_ts(
            payload.get("captured_at") if isinstance(payload, dict) else None
        )
        override_now = deps.parse_iso_ts(deps.now_iso())
        provisional_is_fresh = bool(
            provisional_captured_at is not None
            and override_now is not None
            and 0.0
            <= (override_now - provisional_captured_at).total_seconds()
            <= GEX_CONTEXT_STALE_MINUTES * 60
        )
        if (
            not refresh_running
            and isinstance(provisional_payload, dict)
            and not provisional_is_fresh
        ):
            refresh_result.pop("refresh_payload", None)
        if (
            not refresh_running
            and isinstance(provisional_payload, dict)
            and provisional_captured_at is not None
            and provisional_is_fresh
            and (persisted_captured_at is None or provisional_captured_at > persisted_captured_at)
            and provisional_payload.get("instrument_id") == route.instrument_id
            and provisional_payload.get("route_fingerprint") == route.fingerprint
            and provisional_payload.get("source") == GEX_REQUEST_SNAPSHOT_SOURCE
            and provisional_payload.get("capture_mode") == "request"
            and provisional_payload.get("status") == "partial"
            and provisional_payload.get("decision_authoritative") is False
            and isinstance(provisional_payload.get("levels"), list)
            and bool(provisional_payload["levels"])
        ):
            payload = provisional_payload
        chart_payload = await run_physical_thread_call(project_gex_context_for_chart, payload)
        return {
            "checked_at": deps.now_iso(),
            **chart_payload,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "refresh_running": refresh_running,
            "refresh_status": "running"
            if refresh_running
            else str(refresh_result.get("refresh_status") or "idle"),
            "refresh_error": str(refresh_result.get("refresh_error") or ""),
            "refresh_finished_at": str(refresh_result.get("refresh_finished_at") or ""),
            "refresh_request": refresh_result.get("refresh_request")
            if isinstance(refresh_result.get("refresh_request"), dict)
            else None,
        }

    router.add_api_route(
        "/api/options/target-price",
        partial(_api_option_target_price, deps),
        methods=["GET"],
    )

    return router
