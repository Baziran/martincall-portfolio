from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.indicators.domain_facts import indicator_fact_payload, metric_number
from aef_terminal.indicators.service_contract import (
    IndicatorServiceContext,
    IndicatorServiceContribution,
    IndicatorServiceTask,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.stable_hash import stable_hash
from aef_terminal.settings_contract import instrument_ids_for_indicator_setting
from aef_terminal.ui.routers.error_payloads import build_error_payload

from . import DISCORD_SIGNALS_VERSION
from .contracts import DiscordSignalEvent, DiscordSignalProfile
from .rpc_bridge import DiscordSignalFeed, DiscordSignalFeedConfig
from .lifecycle import project_discord_signal_lifecycle
from .parser import (
    discord_source_session_date,
    parse_discord_feed,
)


_SERVICE_CONTEXT: IndicatorServiceContext | None = None
_DISCORD_FEED: DiscordSignalFeed | None = None


class DiscordSignalServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _discord_http_error(
    status_code: int,
    *,
    code: str,
    retryable: bool,
    message: str,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=build_error_payload(
            code=code,
            category="discord_signals",
            retryable=retryable,
            error=message,
        ),
    )


def configure_service(
    context: IndicatorServiceContext,
) -> IndicatorServiceContribution:
    from aef_terminal.config import AppConfig

    feed_config = DiscordSignalFeedConfig.from_app_config(AppConfig())

    def upsert_messages(
        rows: Sequence[Mapping[str, Any]],
        source_event: str,
        observed_at: datetime,
    ) -> Mapping[str, Mapping[str, Any]]:
        store = context.store_factory()
        if store is None:
            raise RuntimeError("DISCORD_SIGNAL_STORAGE_REQUIRED")
        return store.upsert_discord_signal_messages(
            rows,
            source_event=source_event,
            observed_at=observed_at,
        )

    def delete_message(
        message_id: str,
        channel_id: str,
        observed_at: datetime,
    ) -> Mapping[str, Any]:
        store = context.store_factory()
        if store is None:
            raise RuntimeError("DISCORD_SIGNAL_STORAGE_REQUIRED")
        return store.mark_discord_signal_message_deleted(
            message_id=message_id,
            channel_id=channel_id,
            observed_at=observed_at,
        )

    feed = DiscordSignalFeed(
        feed_config,
        upsert_messages=upsert_messages,
        delete_message=delete_message,
    )
    global _SERVICE_CONTEXT, _DISCORD_FEED
    _SERVICE_CONTEXT = context
    _DISCORD_FEED = feed

    async def service_loop() -> None:
        if feed_config.enabled and feed_config.configured:
            store = await run_physical_thread_call(context.store_factory)
            if store is None:
                raise RuntimeError("DISCORD_SIGNAL_STORAGE_REQUIRED")
            rows = await run_physical_thread_call(
                store.read_discord_signal_messages,
                channel_ids=feed_config.channel_ids,
                author_id=feed_config.author_id,
                published_since=(datetime.now(UTC) - timedelta(hours=feed_config.retention_hours)),
                limit=feed_config.history_limit,
            )
            feed.restore(rows)
        while True:
            settings = context.client_settings_snapshot()
            target_ids = _discord_signal_target_ids(settings)
            _refresh_feed_gate(target_ids)
            await asyncio.sleep(2.0)

    async def shutdown() -> None:
        feed.set_targets(())

    return IndicatorServiceContribution(
        tasks=(
            IndicatorServiceTask(
                name="indicator:discord-signals:settings-gate",
                factory=service_loop,
                restart_delay_seconds=2.0,
                restart_window_seconds=60.0,
                max_restarts_per_window=3,
            ),
        ),
        shutdown=shutdown,
    )


def discord_signal_payload(
    *,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
    rows: Sequence[Mapping[str, Any]],
    profile: DiscordSignalProfile,
    as_of_session_date: date,
    mode: str,
    feed_status: Mapping[str, Any],
) -> dict[str, Any]:
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    exact_route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe is required")
    events = parse_discord_feed(rows, profile)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=as_of_session_date,
    )
    links = projection["event_links"]
    position_rows = [
        {
            **position,
            "position_side": profile.default_position_side,
            "underlying_bias": (
                "long"
                if profile.default_position_side == "long_premium"
                and position.get("option_right") == "call"
                else "short"
                if profile.default_position_side == "long_premium"
                and position.get("option_right") == "put"
                else "flat"
            ),
        }
        for position in projection["positions"]
    ]
    event_rows = [
        {
            **event.as_dict(),
            "instrument_id": exact_instrument_id,
            "route_fingerprint": exact_route_fingerprint,
            "position_side": profile.default_position_side,
            "underlying_bias": (
                "long"
                if profile.default_position_side == "long_premium" and event.option_right == "call"
                else "short"
                if profile.default_position_side == "long_premium" and event.option_right == "put"
                else "flat"
            ),
            **links.get(
                event.event_id,
                {"lifecycle_status": "advisory_only", "position_id": ""},
            ),
            "facts": _discord_signal_event_facts(event, profile),
        }
        for event in events
    ]
    stats = {
        "message_count": len({str(row.get("message_id", row.get("id"))) for row in rows}),
        **projection["stats"],
    }
    revision = stable_hash(
        {
            "instrument_id": exact_instrument_id,
            "route_fingerprint": exact_route_fingerprint,
            "timeframe": timeframe,
            "mode": mode,
            "as_of_session_date": projection["as_of_session_date"],
            "events": event_rows,
            "positions": position_rows,
            "stats": stats,
        },
        length=20,
    )
    return {
        "ok": True,
        "version": DISCORD_SIGNALS_VERSION,
        "mode": mode,
        "instrument_id": exact_instrument_id,
        "route_fingerprint": exact_route_fingerprint,
        "timeframe": timeframe,
        "as_of_session_date": projection["as_of_session_date"],
        "revision": revision,
        "feed_status": dict(feed_status),
        "events": event_rows,
        "positions": position_rows,
        "stats": stats,
    }


def discord_signal_live_payload(
    *,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
) -> dict[str, Any]:
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    feed = _DISCORD_FEED
    if feed is None:
        raise DiscordSignalServiceError(
            "DISCORD_SIGNAL_SERVICE_NOT_INITIALIZED",
            "Discord signal service is not initialized",
        )
    if not feed.config.enabled or not feed.config.configured:
        raise DiscordSignalServiceError(
            "DISCORD_SIGNAL_RPC_NOT_CONFIGURED",
            "Discord signal RPC is not configured",
        )
    gate = _refresh_feed_gate()
    target_ids = tuple(gate["live_target_instrument_ids"])
    snapshot = feed.snapshot()
    status = {
        **snapshot["status"],
        "gate": gate,
        "selected_target_enabled": exact_instrument_id in target_ids,
        "selected_target_live": exact_instrument_id in target_ids,
    }
    rows = snapshot["rows"] if exact_instrument_id in target_ids else ()
    profile = feed.config.profile()
    return discord_signal_payload(
        instrument_id=exact_instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
        rows=rows,
        profile=profile,
        as_of_session_date=discord_source_session_date(datetime.now(UTC), profile),
        mode="live_discord_rpc",
        feed_status=status,
    )


def _discord_signal_target_ids(
    settings: Mapping[str, Any],
) -> tuple[str, ...]:
    return instrument_ids_for_indicator_setting(
        settings,
        "discordSignalsCalcEnabled",
    )


def _refresh_feed_gate(
    live_target_instrument_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    feed = _DISCORD_FEED
    context = _SERVICE_CONTEXT
    if feed is None or context is None:
        return {
            "active": False,
            "forced_by_calc": False,
            "activation_source": "calc_enabled_settings",
            "live_target_instrument_ids": [],
            "target_instrument_ids": [],
            "server_sleeping": False,
        }
    if live_target_instrument_ids is None:
        target_ids = tuple(feed.snapshot()["status"]["live_target_instrument_ids"])
    else:
        target_ids = live_target_instrument_ids
    sleeping = bool(context.server_sleeping())
    effective_targets = () if sleeping else target_ids
    feed.set_targets(
        effective_targets,
        live_instrument_ids=target_ids,
    )
    return {
        "active": bool(effective_targets),
        "forced_by_calc": bool(target_ids),
        "activation_source": "calc_enabled_settings",
        "live_target_instrument_ids": list(target_ids),
        "target_instrument_ids": list(effective_targets),
        "server_sleeping": sleeping,
    }


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/indicators/discord-signals/feed")
    def live_feed(
        instrument_id: str = Query(...),
        route_fingerprint: str = Query(...),
        timeframe: str = Query(...),
    ) -> dict[str, Any]:
        try:
            return discord_signal_live_payload(
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                timeframe=timeframe,
            )
        except DiscordSignalServiceError as exc:
            raise _discord_http_error(
                503,
                code=exc.code,
                retryable=exc.code == "DISCORD_SIGNAL_SERVICE_NOT_INITIALIZED",
                message=str(exc),
            ) from exc

    @router.get("/api/indicators/discord-signals/status")
    def feed_status() -> dict[str, Any]:
        feed = _DISCORD_FEED
        if feed is None:
            return {
                "ok": False,
                "state": "service_not_initialized",
                "configured": False,
            }
        gate = _refresh_feed_gate()
        target_ids = tuple(gate["live_target_instrument_ids"])
        status = feed.snapshot()["status"]
        return {
            "ok": True,
            **status,
            "gate": gate,
            "group_instrument_ids": list(target_ids),
        }

    @router.get("/api/indicators/discord-signals/companion-gate")
    def companion_gate(
        bridge_secret: str = Header(
            default="",
            alias="X-MartinCall-Discord-RPC",
        ),
    ) -> dict[str, Any]:
        feed = _DISCORD_FEED
        if feed is None:
            raise _discord_http_error(
                503,
                code="DISCORD_SIGNAL_SERVICE_NOT_INITIALIZED",
                retryable=True,
                message="Discord signal service is not initialized",
            )
        if not feed.config.enabled or not feed.config.configured:
            raise _discord_http_error(
                503,
                code="DISCORD_SIGNAL_RPC_NOT_CONFIGURED",
                retryable=False,
                message="Discord signal RPC is not configured",
            )
        if not feed.config.accepts_bridge_secret(bridge_secret):
            raise _discord_http_error(
                401,
                code="DISCORD_SIGNAL_RPC_UNAUTHORIZED",
                retryable=False,
                message="Discord signal RPC authorization failed",
            )
        return {"ok": True, "gate": _refresh_feed_gate()}

    @router.post("/api/indicators/discord-signals/rpc-ingest")
    def rpc_ingest(
        payload: dict[str, Any] = Body(...),
        bridge_secret: str = Header(
            default="",
            alias="X-MartinCall-Discord-RPC",
        ),
    ) -> dict[str, Any]:
        feed = _DISCORD_FEED
        if feed is None:
            raise _discord_http_error(
                503,
                code="DISCORD_SIGNAL_SERVICE_NOT_INITIALIZED",
                retryable=True,
                message="Discord signal service is not initialized",
            )
        if not feed.config.enabled or not feed.config.configured:
            raise _discord_http_error(
                503,
                code="DISCORD_SIGNAL_RPC_NOT_CONFIGURED",
                retryable=False,
                message="Discord signal RPC is not configured",
            )
        if not feed.config.accepts_bridge_secret(bridge_secret):
            raise _discord_http_error(
                401,
                code="DISCORD_SIGNAL_RPC_UNAUTHORIZED",
                retryable=False,
                message="Discord signal RPC authorization failed",
            )
        try:
            result = feed.ingest_bridge_envelope(payload)
        except (TypeError, ValueError) as exc:
            raise _discord_http_error(
                422,
                code="DISCORD_SIGNAL_RPC_PAYLOAD_INVALID",
                retryable=False,
                message=str(exc),
            ) from exc
        return {"ok": True, **result}

    return router


def _discord_signal_event_facts(
    event: DiscordSignalEvent,
    profile: DiscordSignalProfile,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "reported_premium": (
            metric_number(event.reported_premium, digits=2)
            if event.reported_premium is not None
            else None
        ),
        "strike": (metric_number(event.strike, digits=2) if event.strike is not None else None),
        "option_right": event.option_right,
        "position_side": profile.default_position_side,
        "underlying_bias": (
            "long"
            if profile.default_position_side == "long_premium" and event.option_right == "call"
            else "short"
            if profile.default_position_side == "long_premium" and event.option_right == "put"
            else "flat"
        ),
        "expiry": event.expiry,
        "reported_dte": event.reported_dte,
        "quantity": event.quantity,
        "sold_quantity": event.sold_quantity,
        "remaining_quantity": event.remaining_quantity,
        "remaining_fraction": (
            metric_number(event.remaining_fraction, digits=2, suffix="x")
            if event.remaining_fraction is not None
            else None
        ),
        "stop_mode": event.stop_mode,
        "stop_value": (
            metric_number(event.stop_value, digits=2) if event.stop_value is not None else None
        ),
        "stop_basis": event.stop_basis,
        "directional_condition": event.directional_condition,
        "directional_level": (
            metric_number(event.directional_level, digits=2)
            if event.directional_level is not None
            else None
        ),
        "directional_reference": event.directional_reference,
        "directional_confirmation": event.directional_confirmation,
        "exit_reason": event.exit_reason,
        "reported_return_pct": (
            metric_number(event.reported_return_pct, digits=2, suffix="%")
            if event.reported_return_pct is not None
            else None
        ),
        "outcome_hint": event.outcome_hint,
        "context_cue": event.context_cue.value if event.context_cue is not None else None,
    }
    return indicator_fact_payload(
        scenario="external_option_signal",
        setup="discord_advisory_call",
        trigger_event={
            "code": f"external_option_{event.action.value}",
            "action": event.action.value,
            "source": profile.source_id,
        },
        context=(
            {
                "code": "discord_source_message",
                "content": event.raw_content,
                "author_id": profile.author_id,
                "channel_id": event.channel_id,
                "published_at": event.published_at.isoformat(),
            },
        ),
        quality={
            "code": event.parse_status.value,
            "advisory": True,
            "provider_qualified": False,
            "reason_code": event.reason_code,
        },
        metrics=metrics,
    )
