from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from aef_terminal.alerts.interactive_bot import TelegramInteractiveBot
from aef_terminal.alerts.telegram import (
    format_alert_message,
    send_telegram_message,
)
from aef_terminal.config import AppConfig
from aef_terminal.data.gex.history import latest_gex_snapshot_payload
from aef_terminal.data.gex.constants import GEX_CHART_HISTORY_HOURS, GEX_CONTEXT_MAX_LEVELS
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.settings_contract import TELEGRAM_INTERACTIVE_SETTING_KEY
from aef_terminal.ui.alert_commands import alert_callback_token, execute_alert_command
from aef_terminal.ui.gex_projection import project_gex_context_for_chart
from aef_terminal.ui.quote_helpers import quote_price
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.runtime_services import (
    telegram_interactive_runtime_settings as build_telegram_interactive_runtime_settings,
)
from aef_terminal.ui.services.browser_capture import request_browser_capture

_LOGGER = logging.getLogger(__name__)

TELEGRAM_ALERT_DEDUP: dict[str, float] = {}
TELEGRAM_ALERT_DEDUP_SECONDS = 15 * 60
TELEGRAM_ALERT_DEDUP_LOCK = threading.Lock()

TELEGRAM_INTERACTIVE_BOT: TelegramInteractiveBot | None = None
_TELEGRAM_INTERACTIVE_LOCK = threading.RLock()
_TELEGRAM_INTERACTIVE_STOP_LOCK = threading.Lock()
_TELEGRAM_INTERACTIVE_SETTINGS: dict[str, Any] | None = None
_TELEGRAM_INTERACTIVE_STOPPING = False


@dataclass(frozen=True)
class TelegramRuntimeDeps:
    store_factory: Callable[[], Any]
    selected_instruments: Callable[[], list[dict[str, Any]]]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]


_DEPS: TelegramRuntimeDeps | None = None


def configure_telegram_runtime_deps(deps: TelegramRuntimeDeps) -> None:
    global _DEPS, _TELEGRAM_INTERACTIVE_SETTINGS
    _DEPS = deps
    with _TELEGRAM_INTERACTIVE_LOCK:
        _TELEGRAM_INTERACTIVE_SETTINGS = None


def _deps() -> TelegramRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("telegram runtime dependencies are not configured")
    return _DEPS


def _postgres_store():
    return _deps().store_factory()


def telegram_alert_dedup_key(payload: dict[str, Any]) -> str:
    instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="TELEGRAM_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="TELEGRAM_ALERT_ROUTE_FINGERPRINT",
    )
    timeframe = str(payload.get("timeframe") or "")
    kind = str(
        payload.get("kind") or payload.get("label") or payload.get("title") or "Alert"
    ).lower()
    direction = str(payload.get("direction") or "cross").lower()
    alert_id = str(payload.get("alert_id") or "").strip()
    trigger_event_at = str(payload.get("trigger_event_at") or "").strip()
    try:
        price = float(payload.get("price"))
        price_key = f"{price:.2f}"
    except TypeError, ValueError:
        price_key = ""
    if alert_id:
        return json.dumps(
            [
                "telegram-alert",
                instrument_id,
                route_fingerprint,
                alert_id,
                timeframe,
                kind,
                direction,
                price_key,
                trigger_event_at,
            ],
            separators=(",", ":"),
        )
    return json.dumps(
        [
            "telegram-alert",
            instrument_id,
            route_fingerprint,
            timeframe,
            kind,
            direction,
            price_key,
            trigger_event_at,
        ],
        separators=(",", ":"),
    )


def telegram_alert_recently_sent(payload: dict[str, Any], now: float | None = None) -> bool:
    key = telegram_alert_dedup_key(payload)
    if not key.strip("|"):
        return False
    current = float(now if now is not None else time.time())
    cutoff = current - TELEGRAM_ALERT_DEDUP_SECONDS
    with TELEGRAM_ALERT_DEDUP_LOCK:
        for old_key, ts in list(TELEGRAM_ALERT_DEDUP.items()):
            if ts < cutoff:
                TELEGRAM_ALERT_DEDUP.pop(old_key, None)
        previous = TELEGRAM_ALERT_DEDUP.get(key)
        return previous is not None and current - previous < TELEGRAM_ALERT_DEDUP_SECONDS


def mark_telegram_alert_sent(payload: dict[str, Any], now: float | None = None) -> None:
    key = telegram_alert_dedup_key(payload)
    if not key.strip("|"):
        return
    current = float(now if now is not None else time.time())
    with TELEGRAM_ALERT_DEDUP_LOCK:
        TELEGRAM_ALERT_DEDUP[key] = current


def send_server_telegram_alert(payload: dict[str, Any]) -> dict[str, Any]:
    dedupe_payload = {**payload, "alert_id": ""}
    if telegram_alert_recently_sent(dedupe_payload):
        return {
            "ok": True,
            "status": "duplicate",
            "message": "Duplicate server Telegram alert suppressed.",
        }
    # Price alerts are armed intentionally; deliver when credentials are configured
    # even if AEF_TELEGRAM_ENABLED=false (test endpoint uses the same bypass).
    result = send_telegram_message(
        format_alert_message(payload),
        force=True,
        reply_markup=telegram_alert_action_markup(payload),
    )
    if bool(result.get("ok")):
        mark_telegram_alert_sent(dedupe_payload)
    return result


def telegram_alert_action_markup(payload: dict[str, Any]) -> dict[str, Any] | None:
    alert_id = str(payload.get("alert_id") or "").strip()
    instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="TELEGRAM_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="TELEGRAM_ALERT_ROUTE_FINGERPRINT",
    )
    if not alert_id:
        return None
    callback_token = alert_callback_token(
        alert_id,
        instrument_id,
        route_fingerprint,
    )
    if not callback_token:
        return None
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 Activate again", "callback_data": f"al:a:r:p:{callback_token}"},
                {"text": "📊 Chart", "callback_data": f"al:a:c:p:{callback_token}"},
            ],
            [{"text": "⏸ Disable", "callback_data": f"al:a:d:p:{callback_token}"}],
        ]
    }


def telegram_interactive_quote(instrument_id: str) -> dict[str, Any]:
    requested = require_exact_identity_text(
        instrument_id,
        field="TELEGRAM_INSTRUMENT_ID",
    )
    try:
        instrument = lookup_runtime_instrument(requested)
        route = route_instrument(instrument)
        provider = route.provider
        provider_symbol = route.provider_symbol
        display_symbol = str(instrument.get("display") or "").strip()
    except Exception as exc:
        return {
            "ok": False,
            "symbol": "",
            "instrument_id": requested,
            "provider_symbol": "",
            "source": "instrument:missing",
            "message": str(exc),
        }
    quote_map, warning, _cache_revision = _deps().quote_cache_for_instruments([instrument])
    quote = dict(quote_map.get(route.fingerprint) or {})
    source = f"{provider or 'unknown'}:quote-cache"
    ok = quote_price(quote) is not None
    if not ok and not quote.get("message"):
        quote["message"] = warning or "Quote has not arrived from the quote orchestrator yet."
    return {
        "ok": ok,
        "symbol": display_symbol,
        "instrument_id": route.instrument_id,
        "provider_symbol": provider_symbol,
        "route_fingerprint": route.fingerprint,
        **quote,
        "source": source,
    }


def telegram_interactive_gex(instrument_id: str) -> dict[str, Any]:
    requested = require_exact_identity_text(
        instrument_id,
        field="TELEGRAM_INSTRUMENT_ID",
    )
    try:
        route = route_instrument(lookup_runtime_instrument(requested))
        if not route.adapter.capabilities.gex:
            raise ValueError(
                f"GEX_PROVIDER_UNSUPPORTED provider={route.provider} instrument_key={route.instrument_key}"
            )
    except Exception as exc:
        return {
            "ok": False,
            "symbol": requested,
            "instrument_id": requested,
            "error": f"GEX instrument identity is unavailable: {exc}",
        }
    payload = (
        latest_gex_snapshot_payload(
            str(route.instrument.get("display") or route.instrument_key),
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            max_levels=GEX_CONTEXT_MAX_LEVELS,
            stale_minutes=24 * 60,
            history_hours=GEX_CHART_HISTORY_HOURS,
            store=_postgres_store(),
        )
        or {}
    )
    payload = project_gex_context_for_chart(payload)
    return {
        **payload,
        "symbol": str(route.instrument.get("display") or route.instrument_key),
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
    }


def telegram_alert_command(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    return execute_alert_command(
        action,
        payload,
        store_factory=_deps().store_factory,
        instrument_lookup=lookup_runtime_instrument,
    )


def _read_telegram_interactive_setting() -> object:
    store = _postgres_store()
    if store is None:
        raise RuntimeError("SERVER_SETTINGS_STORAGE_REQUIRED")
    return store.read_setting("server", TELEGRAM_INTERACTIVE_SETTING_KEY)


def telegram_interactive_runtime_settings() -> dict[str, Any]:
    global _TELEGRAM_INTERACTIVE_SETTINGS
    with _TELEGRAM_INTERACTIVE_LOCK:
        if _TELEGRAM_INTERACTIVE_SETTINGS is not None:
            return deepcopy(_TELEGRAM_INTERACTIVE_SETTINGS)
        cfg = AppConfig()
        try:
            raw = _read_telegram_interactive_setting()
        except Exception as exc:
            return build_telegram_interactive_runtime_settings(
                env_enabled=bool(cfg.telegram_interactive_enabled),
                configured=bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
                settings_error=exc,
            )
        settings = build_telegram_interactive_runtime_settings(
            env_enabled=bool(cfg.telegram_interactive_enabled),
            configured=bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
            raw_setting=raw,
        )
        _TELEGRAM_INTERACTIVE_SETTINGS = deepcopy(settings)
        return settings


def _save_telegram_interactive_runtime_setting(enabled: bool) -> dict[str, Any]:
    global _TELEGRAM_INTERACTIVE_SETTINGS
    store = _postgres_store()
    if store is None:
        raise RuntimeError("SERVER_SETTINGS_STORAGE_REQUIRED")
    exact_setting = {"enabled": bool(enabled)}
    store.upsert_setting("server", TELEGRAM_INTERACTIVE_SETTING_KEY, exact_setting)
    cfg = AppConfig()
    settings = build_telegram_interactive_runtime_settings(
        env_enabled=bool(cfg.telegram_interactive_enabled),
        configured=bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
        raw_setting=exact_setting,
    )
    _TELEGRAM_INTERACTIVE_SETTINGS = deepcopy(settings)
    return settings


def telegram_watchlist_instruments() -> list[dict[str, str]]:
    instruments = _deps().selected_instruments()
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for instrument in instruments:
        try:
            instrument_id = require_exact_identity_text(
                instrument.get("instrument_id"),
                field="TELEGRAM_INSTRUMENT_ID",
            )
        except ValueError:
            continue
        display = str(instrument.get("display") or instrument.get("key") or "").strip()
        if not instrument_id or not display or instrument_id in seen:
            continue
        seen.add(instrument_id)
        items.append({"instrument_id": instrument_id, "display": display})
    return items


def telegram_interactive_status() -> dict[str, Any]:
    with _TELEGRAM_INTERACTIVE_LOCK:
        settings = telegram_interactive_runtime_settings()
        if TELEGRAM_INTERACTIVE_BOT is None:
            return {
                **settings,
                "running": False,
                "stopping": _TELEGRAM_INTERACTIVE_STOPPING,
            }
        bot_status = TELEGRAM_INTERACTIVE_BOT.status()
        return {
            **bot_status,
            **settings,
            "running": bool(bot_status.get("running")),
            "stopping": _TELEGRAM_INTERACTIVE_STOPPING,
        }


def ensure_telegram_interactive_bot(
    *,
    system_status_provider: Any,
) -> dict[str, Any]:
    return _apply_telegram_interactive_bot(
        system_status_provider=system_status_provider,
        enabled=None,
        persist=False,
    )


async def ensure_telegram_interactive_bot_async(
    *,
    system_status_provider: Any,
) -> dict[str, Any]:
    return await run_physical_thread_call(
        ensure_telegram_interactive_bot,
        system_status_provider=system_status_provider,
    )


def set_telegram_interactive_enabled(
    *,
    system_status_provider: Any,
    enabled: bool,
) -> dict[str, Any]:
    return _apply_telegram_interactive_bot(
        system_status_provider=system_status_provider,
        enabled=bool(enabled),
        persist=True,
    )


def _apply_telegram_interactive_bot(
    *,
    system_status_provider: Any,
    enabled: bool | None,
    persist: bool,
) -> dict[str, Any]:
    global TELEGRAM_INTERACTIVE_BOT
    with _TELEGRAM_INTERACTIVE_LOCK:
        settings = (
            _save_telegram_interactive_runtime_setting(bool(enabled))
            if persist
            else telegram_interactive_runtime_settings()
        )
    if not settings["enabled"]:
        outcome = stop_telegram_interactive_bot()
        if outcome.get("drained") is not True:
            raise RuntimeError("TELEGRAM_INTERACTIVE_SHUTDOWN_UNDRAINED")
        return telegram_interactive_status()
    with _TELEGRAM_INTERACTIVE_LOCK:
        if _TELEGRAM_INTERACTIVE_STOPPING:
            raise RuntimeError("TELEGRAM_INTERACTIVE_STOPPING")
        cfg = AppConfig()
        if TELEGRAM_INTERACTIVE_BOT is None:
            TELEGRAM_INTERACTIVE_BOT = TelegramInteractiveBot(
                cfg,
                system_status_provider=system_status_provider,
                quote_provider=telegram_interactive_quote,
                gex_provider=telegram_interactive_gex,
                browser_capture_provider=request_browser_capture,
                watchlist_instruments_provider=telegram_watchlist_instruments,
                alert_command_provider=telegram_alert_command,
            )
        TELEGRAM_INTERACTIVE_BOT.start_background(force=True)
    return telegram_interactive_status()


def stop_telegram_interactive_bot() -> dict[str, Any]:
    global TELEGRAM_INTERACTIVE_BOT, _TELEGRAM_INTERACTIVE_STOPPING
    with _TELEGRAM_INTERACTIVE_STOP_LOCK:
        with _TELEGRAM_INTERACTIVE_LOCK:
            bot = TELEGRAM_INTERACTIVE_BOT
            if bot is None:
                _TELEGRAM_INTERACTIVE_STOPPING = False
                return {
                    "drained": True,
                    "thread_alive": False,
                    "inflight_commands": 0,
                }
            _TELEGRAM_INTERACTIVE_STOPPING = True
        outcome = bot.stop()
        with _TELEGRAM_INTERACTIVE_LOCK:
            if outcome.get("drained") is True and TELEGRAM_INTERACTIVE_BOT is bot:
                TELEGRAM_INTERACTIVE_BOT = None
                _TELEGRAM_INTERACTIVE_STOPPING = False
        return outcome


async def stop_telegram_interactive_bot_async() -> dict[str, Any]:
    return await run_physical_thread_call(stop_telegram_interactive_bot)
