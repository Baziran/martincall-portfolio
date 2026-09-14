from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from html import escape as html_escape
from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from typing import Any

from aef_terminal.config import AppConfig

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_DISPLAY_TZ = ZoneInfo("Asia/Jerusalem")


def telegram_callback_token(value: Any, *, namespace: str = "alert") -> str:
    """Return a compact deterministic token suitable for Telegram callback_data."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    scope = str(namespace or "alert").strip().lower() or "alert"
    digest = hashlib.sha256(f"{scope}\0{raw}".encode("utf-8")).hexdigest()[:16]
    return f"{scope[:2]}{digest}"


def telegram_config_status(config: AppConfig | None = None) -> dict[str, Any]:
    cfg = config or AppConfig()
    return {
        "enabled": bool(cfg.telegram_enabled),
        "interactive_enabled": bool(cfg.telegram_interactive_enabled),
        "configured": bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
        "chat_id": _masked_chat_id(cfg.telegram_chat_id),
        "timeout_seconds": cfg.telegram_timeout_seconds,
        "interactive_poll_timeout_seconds": cfg.telegram_interactive_poll_timeout_seconds,
    }


def format_alert_message(payload: Mapping[str, Any]) -> str:
    symbol = _clean(payload.get("symbol") or "MARTIN")
    timeframe = _clean(payload.get("timeframe") or "")
    kind = _clean(payload.get("kind") or payload.get("label") or payload.get("title") or "Alert")
    direction = _clean(payload.get("direction") or "cross")
    price = _fmt_price(payload.get("price"))
    current = _fmt_price(payload.get("current_price"))
    level_source = (
        payload.get("level_source") if isinstance(payload.get("level_source"), Mapping) else {}
    )

    # Направление для эмодзи
    emoji = (
        "📈"
        if direction.lower() in {"above", "long", "up"}
        else "📉"
        if direction.lower() in {"below", "short", "down"}
        else "🔔"
    )

    if kind.lower() == "price alert":
        trigger_line = {
            "cross": "Level crossed",
            "above": "Moved above level",
            "below": "Moved below level",
        }.get(
            direction.lower(),
            f"Price alert {direction}",
        )
    elif "touch" in kind.lower():
        trigger_line = kind
    else:
        trigger_line = f"{kind} {direction}".strip()

    # Header with a direct link to TradingView for convenience
    tv_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol, safe='')}"
    time_str = _format_local_ts(payload.get("ts"))

    # Line 1: Emoji, Symbol (link to TV), Timeframe, Time
    line1 = f"{emoji} <a href='{tv_url}'><b>{html_escape(symbol)}</b></a> <b>[{timeframe}]</b> | <code>{time_str}</code>"

    # Line 2: Level information and potential trade plan
    line2 = f"📍 {trigger_line}"
    if price:
        line2 += f": <b>{price}</b>"
    if current:
        line2 += f" -> <b>{current}</b>"

    trigger_p = _fmt_price(payload.get("trigger"))
    stop_p = _fmt_price(payload.get("stop"))
    target_p = _fmt_price(payload.get("target"))

    if trigger_p or stop_p or target_p:
        plan = " / ".join(
            part
            for part in (
                f"<b>E {trigger_p}</b>" if trigger_p else "",
                f"<b>S {stop_p}</b>" if stop_p else "",
                f"<b>T {target_p}</b>" if target_p else "",
            )
            if part
        )
        line2 += f" | {plan}"

    message = _clean(payload.get("message") or "")
    if message and message.lower() != "fired":
        line2 += f" | 💬 {message}"

    provenance = ""
    if level_source.get("type") == "gex_snapshot":
        captured = _format_local_ts(level_source.get("snapshot_captured_at"))
        source = _clean(level_source.get("snapshot_source") or "")
        provenance = f"\n🧭 GEX snapshot: <code>{captured}</code>"
        if source:
            provenance += f" | {_clean(source)}"

    return f"{line1}\n{line2}{provenance}"


def format_paper_trade_message(trade: Mapping[str, Any], event: str = "open") -> str:
    symbol = _clean(trade.get("symbol") or "MARTIN")
    timeframe = _clean(trade.get("timeframe") or "")
    side = _clean(trade.get("side") or "").upper()
    entry = _fmt_price(trade.get("entry"))
    stop = _fmt_price(trade.get("stop"))
    target = _fmt_price(trade.get("target"))

    opened_at = trade.get("opened_at")
    closed_at = trade.get("closed_at")
    exit_reason = _clean(trade.get("exit_reason") or "")
    exit_price = _fmt_price(trade.get("exit_price"))
    pnl = _fmt_signed(trade.get("pnl_points"))
    rr = _trade_rr(trade)

    is_close = str(event).lower() == "close"
    emoji = "⚪" if is_close else ("🟢" if side.lower() == "long" else "🔴")
    tv_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol, safe='')}"
    time_str = _format_local_ts(closed_at if is_close else opened_at)
    header_text = "CLOSE" if is_close else "OPEN"

    # Line 1: OPEN/CLOSE, Symbol (link), Side, Time
    line1 = f"{emoji} <b>{header_text}</b> <a href='{tv_url}'><b>{html_escape(symbol)}</b></a> <b>[{timeframe}]</b> <b>{side}</b> | <code>{time_str}</code>"

    plan = " / ".join(
        part
        for part in (
            f"E <b><code>{entry}</code></b>" if entry else "",
            f"S <b><code>{stop}</code></b>" if stop else "",
            f"T <b><code>{target}</code></b>" if target else "",
        )
        if part
    )
    line2 = f"📝 Plan: {plan}"

    details = []
    if rr:
        details.append(f"RR {rr}")
    if is_close:
        if exit_reason:
            details.append(f"Reason: {exit_reason}")
        if exit_price:
            details.append(f"X <b>{exit_price}</b>")
        if pnl:
            pnl_emoji = "💰" if (trade.get("pnl_points") or 0) > 0 else "📉"
            details.append(f"{pnl_emoji} PnL: <b>{pnl}</b>")

    if details:
        line2 += " | " + " | ".join(details)

    return f"{line1}\n{line2}"


def send_telegram_message(
    text: str,
    *,
    config: AppConfig | None = None,
    force: bool = False,
    silent: bool = False,
    reply_markup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or AppConfig()
    status = telegram_config_status(cfg)
    if not force and not cfg.telegram_enabled:
        return {**status, "ok": False, "status": "disabled"}
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return {**status, "ok": False, "status": "not_configured"}
    if not text.strip():
        return {**status, "ok": False, "status": "empty_message"}

    url = f"{TELEGRAM_API_BASE}/bot{cfg.telegram_bot_token}/sendMessage"
    body = {
        "chat_id": cfg.telegram_chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if silent:
        body["disable_notification"] = True
    if reply_markup:
        body["reply_markup"] = dict(reply_markup)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=max(float(cfg.telegram_timeout_seconds), 0.5)
        ) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return {
                **status,
                "ok": bool(data.get("ok")),
                "status": "sent" if data.get("ok") else "telegram_error",
            }
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        return {
            **status,
            "ok": False,
            "status": "telegram_http_error",
            "code": error.code,
            "detail": detail,
        }
    except Exception as error:
        return {**status, "ok": False, "status": "send_failed", "detail": str(error)[:500]}


def _masked_chat_id(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value)
    if len(raw) <= 4:
        return "*" * len(raw)
    return f"{raw[:2]}***{raw[-2:]}"


def _clean(value: Any) -> str:
    return str(value or "").replace("\r", " ").strip()[:240]


def _fmt_price(value: Any) -> str:
    try:
        number = float(value)
    except TypeError, ValueError:
        return ""
    if not number == number:
        return ""
    return f"{number:.2f}"


def _fmt_signed(value: Any) -> str:
    try:
        number = float(value)
    except TypeError, ValueError:
        return ""
    if not number == number:
        return ""
    return f"{number:+.2f}"


def _trade_rr(trade: Mapping[str, Any]) -> str:
    try:
        entry = float(trade.get("entry"))
        stop = float(trade.get("stop"))
        target = float(trade.get("target"))
    except TypeError, ValueError:
        return ""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward <= 0:
        return ""
    return f"{reward / risk:.2f}"


def _format_local_ts(value: Any) -> str:
    parsed = _parse_ts(value)
    if parsed is None:
        parsed = datetime.now(tz=UTC)
    return parsed.astimezone(TELEGRAM_DISPLAY_TZ).strftime("%H:%M %Y-%m-%d")


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
