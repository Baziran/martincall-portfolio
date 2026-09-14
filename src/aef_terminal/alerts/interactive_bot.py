from __future__ import annotations

import html
import json
import logging
import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

from aef_terminal.alerts.snapshot_rendering import (
    gex_market_data_status_text,
    render_gex_interval_png,
    render_gex_strike_png,
    telegram_capture_header_time,
)
from aef_terminal.alerts.telegram import TELEGRAM_API_BASE, telegram_callback_token
from aef_terminal.config import AppConfig
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.runtime.math_utils import float_or_none

logger = logging.getLogger(__name__)
_TELEGRAM_MAX_INFLIGHT_COMMANDS = 128

SystemStatusProvider = Callable[[], Mapping[str, Any]]
QuoteProvider = Callable[[str], Mapping[str, Any]]
GexProvider = Callable[[str], Mapping[str, Any]]
BrowserCaptureProvider = Callable[[str, str], Mapping[str, Any] | None]
WatchlistInstrumentsProvider = Callable[[], list[dict[str, Any]]]
AlertCommandProvider = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def _valid_alert_callback_component(value: Any, *, max_length: int = 20) -> bool:
    token = str(value or "")
    return bool(token) and len(token) <= max_length and token.isascii() and token.isalnum()


class TelegramInteractiveBot:
    """Telegram long-polling command bot for status, market data and diagnostics."""

    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        system_status_provider: SystemStatusProvider | None = None,
        quote_provider: QuoteProvider | None = None,
        gex_provider: GexProvider | None = None,
        browser_capture_provider: BrowserCaptureProvider | None = None,
        watchlist_instruments_provider: WatchlistInstrumentsProvider | None = None,
        alert_command_provider: AlertCommandProvider | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = config or AppConfig()
        self.system_status_provider = system_status_provider or (lambda: {})
        self.quote_provider = quote_provider or (lambda _instrument_id: {})
        self.gex_provider = gex_provider or (lambda _instrument_id: {})
        self.browser_capture_provider = browser_capture_provider or (
            lambda _instrument_id, _scope: None
        )
        self.watchlist_instruments_provider = watchlist_instruments_provider or (lambda: [])
        self.alert_command_provider = alert_command_provider or (
            lambda _action, _payload: {"ok": False, "status": "unavailable"}
        )
        self.opener = opener or urllib.request.urlopen
        self.last_update_id = 0
        self.api_latency_ms = 0.0
        self.base_url = f"{TELEGRAM_API_BASE}/bot{self.cfg.telegram_bot_token or ''}"
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegram-command")
        self._alert_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="telegram-alert-command",
        )
        self._futures: set[Future[None]] = set()
        self._futures_lock = threading.Lock()
        self._heavy_lock = threading.Lock()
        self._pending_alert_inputs: dict[str, dict[str, Any]] = {}
        self._pending_alert_lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id)

    def enabled(self) -> bool:
        return bool(self.cfg.telegram_interactive_enabled and self.configured())

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.cfg.telegram_interactive_enabled),
            "configured": self.configured(),
            "running": bool(self._thread and self._thread.is_alive()),
            "last_update_id": int(self.last_update_id),
            "api_latency_ms": round(float(self.api_latency_ms), 1),
            "inflight_commands": self._inflight_count(),
        }

    def start_background(self, *, force: bool = False) -> bool:
        if not (self.enabled() or (force and self.configured())):
            logger.info("Telegram interactive bot is disabled or not configured")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="telegram-interactive-bot", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout: float | None = None) -> dict[str, Any]:
        self._stop_event.set()
        thread = self._thread
        join_timeout = max(
            float(
                timeout
                if timeout is not None
                else self.cfg.telegram_interactive_poll_timeout_seconds + 3.0
            ),
            0.1,
        )
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        thread_alive = bool(thread is not None and thread.is_alive())
        if thread_alive:
            return {
                "drained": False,
                "thread_alive": True,
                "inflight_commands": self._inflight_count(),
            }
        self._thread = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._alert_executor.shutdown(wait=False, cancel_futures=True)
        with self._futures_lock:
            futures = tuple(self._futures)
        if futures:
            wait(futures, timeout=join_timeout)
        inflight_commands = self._inflight_count()
        if inflight_commands:
            return {
                "drained": False,
                "thread_alive": False,
                "inflight_commands": inflight_commands,
            }
        self._executor.shutdown(wait=True)
        self._alert_executor.shutdown(wait=True)
        return {
            "drained": True,
            "thread_alive": False,
            "inflight_commands": 0,
        }

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                if _is_timeout_error(exc):
                    logger.debug("Telegram interactive poll timed out")
                    self._stop_event.wait(0.2)
                    continue
                logger.warning("Telegram interactive poll failed: %s", exc)
                self._stop_event.wait(3.0)

    def poll_once(self) -> None:
        if not self.configured():
            return
        timeout = max(float(self.cfg.telegram_interactive_poll_timeout_seconds), 1.0)
        url = f"{self.base_url}/getUpdates?offset={self.last_update_id + 1}&timeout={int(timeout)}"
        started = time.monotonic()
        with self.opener(url, timeout=timeout + 2.0) as response:
            self.api_latency_ms = (time.monotonic() - started) * 1000.0
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        if not payload.get("ok"):
            return
        for update in payload.get("result") or []:
            if self._stop_event.is_set():
                return
            self.last_update_id = int(update.get("update_id") or self.last_update_id)
            self.submit_update(update)

    def submit_update(self, update: Mapping[str, Any]) -> Future[None]:
        if self._stop_event.is_set():
            stopped: Future[None] = Future()
            stopped.set_result(None)
            return stopped
        self._reap_futures()
        if self._inflight_count() >= _TELEGRAM_MAX_INFLIGHT_COMMANDS:
            command = _update_command(update) or "unknown"
            logger.warning("Telegram command dropped: backlog is full (%s)", command)
            dropped: Future[None] = Future()
            dropped.set_result(None)
            return dropped
        command = _update_command(update)
        ordered_alert_command = (
            command in {"/alerts", "/alert", "/gexalert", "al", "cb_alerts"}
            or (command.startswith("al:") and not command.startswith("al:a:c:"))
            or bool(command and not command.startswith(("/", "cb_")))
        )
        executor = self._alert_executor if ordered_alert_command else self._executor
        future = executor.submit(self._handle_update_logged, update)
        with self._futures_lock:
            self._futures.add(future)
        return future

    def _handle_update_logged(self, update: Mapping[str, Any]) -> None:
        started = time.monotonic()
        command = _update_command(update)
        try:
            self.handle_update(update)
        except Exception as exc:
            logger.warning(
                "Telegram interactive command failed (%s): %s", command or "unknown", exc
            )
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if elapsed_ms >= 1000.0:
                logger.info(
                    "Telegram interactive command finished: %s in %.0f ms",
                    command or "unknown",
                    elapsed_ms,
                )

    def _reap_futures(self) -> None:
        with self._futures_lock:
            done = {future for future in self._futures if future.done()}
            self._futures.difference_update(done)

    def _inflight_count(self) -> int:
        self._reap_futures()
        with self._futures_lock:
            return len(self._futures)

    def handle_update(self, update: Mapping[str, Any]) -> None:
        callback_query = update.get("callback_query")
        message = update.get("message")
        if callback_query and isinstance(callback_query, Mapping):
            message = callback_query.get("message")
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            return
        chat_id = str(chat.get("id") or "")
        if not self._authorized(chat_id):
            logger.warning("Unauthorized Telegram chat_id: %s", chat_id)
            return
        if callback_query and isinstance(callback_query, Mapping):
            command = str(callback_query.get("data") or "")
            callback_id = str(callback_query.get("id") or "")
            if callback_id:
                self._answer_callback(callback_id)
            if command.startswith("al:"):
                self.handle_alert_callback(
                    chat_id,
                    command,
                    message_id=message.get("message_id"),
                )
                return
            self._dispatch(chat_id, command, [])
            return
        text = str(message.get("text") or "").strip()
        if not text:
            return
        if not text.startswith("/") and self._consume_alert_input(chat_id, text):
            return
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        self._dispatch(chat_id, command, parts[1:])

    def _authorized(self, chat_id: str) -> bool:
        return bool(chat_id) and chat_id == str(self.cfg.telegram_chat_id)

    def _dispatch(self, chat_id: str, command: str, args: list[str]) -> None:
        if command in {"/start", "/help", "cb_menu"}:
            self.send_dashboard(chat_id)
        elif command in {"/status", "cb_status"}:
            self.send_status(chat_id)
        elif command in {"/gex", "cb_gex"}:
            self.send_gex(chat_id)
        elif command in {"/alerts", "cb_alerts", "al"}:
            self.send_alerts(chat_id)
        elif command == "/alert":
            self.create_price_alert_command(chat_id, args)
        elif command == "/gexalert":
            self.create_gex_alert_command(chat_id, args)
        elif command.startswith("al:"):
            self.handle_alert_callback(chat_id, command)
        elif command.startswith("cb_gex_map_"):
            self.send_gex_map(chat_id, command.removeprefix("cb_gex_map_"), mode="interval")
        elif command.startswith("cb_gex_strike_"):
            self.send_gex_map(chat_id, command.removeprefix("cb_gex_strike_"), mode="strike")
        elif command in {"/gexmap", "/gex1"}:
            self.send_gex_map(chat_id, args[0] if args else "", mode="interval")
        elif command in {"/gexstrike", "/gex2"}:
            self.send_gex_map(chat_id, args[0] if args else "", mode="strike")
        elif command in {"/gexmaps"}:
            self.send_gex_maps(chat_id, args[0] if args else "")
        elif command == "cb_chart":
            self.send_chart_menu(chat_id)
        elif command.startswith("cb_chart_p_"):
            instrument = self._watchlist_instrument_for_token(command.removeprefix("cb_chart_p_"))
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /chart.")
                return
            self.send_chart_snapshot(
                chat_id,
                instrument["instrument_id"],
                capture_scope="price",
            )
        elif command.startswith("cb_chart_t_"):
            instrument = self._watchlist_instrument_for_token(command.removeprefix("cb_chart_t_"))
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /chart.")
                return
            self.send_chart_snapshot(
                chat_id,
                instrument["instrument_id"],
                capture_scope="terminal",
            )
        elif command.startswith("cb_chart_"):
            self.send_chart_snapshot(chat_id, command.removeprefix("cb_chart_"))
        elif command in {"/chart", "/screen", "/screenshot"}:
            if args:
                requested_scope = (
                    args[1].lower()
                    if len(args) > 1
                    else "terminal"
                    if command in {"/screen", "/screenshot"}
                    else "price"
                )
                if requested_scope not in {"price", "terminal"}:
                    self.send_message(
                        chat_id,
                        "Chart scope must be <code>price</code> or <code>terminal</code>.",
                    )
                    return
                self.send_chart_snapshot(
                    chat_id,
                    args[0],
                    capture_scope=requested_scope,
                )
            else:
                self.send_chart_menu(chat_id)
        elif command == "/quote":
            self.send_quote(chat_id, args[0] if args else "")
        else:
            self.send_message(chat_id, "Unknown command. Use /help.")

    def send_message(
        self, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._telegram_post("sendMessage", payload)

    def _answer_callback(self, callback_query_id: str) -> None:
        self._telegram_post("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def _telegram_post(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "status": "not_configured"}
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(
                request, timeout=max(float(self.cfg.telegram_timeout_seconds), 0.5)
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                return {
                    "ok": bool(data.get("ok")),
                    "status": "sent" if data.get("ok") else "telegram_error",
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            return {
                "ok": False,
                "status": "telegram_http_error",
                "code": exc.code,
                "detail": detail,
            }
        except Exception as exc:
            logger.warning("Telegram interactive send failed: %s", exc)
            return {"ok": False, "status": "send_failed", "detail": str(exc)[:500]}

    def _telegram_post_multipart(
        self,
        method: str,
        fields: Mapping[str, Any],
        *,
        file_field: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        if not self.configured():
            return {"ok": False, "status": "not_configured"}
        boundary = f"martincall-{int(time.time() * 1000)}"
        parts: list[bytes] = []
        for key, value in fields.items():
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{file_field}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode("utf-8"),
            ]
        )
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self.opener(
                request, timeout=max(float(self.cfg.telegram_timeout_seconds), 0.5)
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                return {
                    "ok": bool(data.get("ok")),
                    "status": "sent" if data.get("ok") else "telegram_error",
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            return {
                "ok": False,
                "status": "telegram_http_error",
                "code": exc.code,
                "detail": detail,
            }
        except Exception as exc:
            logger.warning("Telegram interactive multipart send failed: %s", exc)
            return {"ok": False, "status": "send_failed", "detail": str(exc)[:500]}

    def send_document(
        self,
        chat_id: str,
        *,
        filename: str,
        content: bytes,
        caption: str,
        content_type: str = "image/svg+xml",
    ) -> dict[str, Any]:
        return self._telegram_post_multipart(
            "sendDocument",
            {
                "chat_id": chat_id,
                "caption": caption[:1000],
                "parse_mode": "HTML",
            },
            file_field="document",
            filename=filename,
            content=content,
            content_type=content_type,
        )

    def send_photo(
        self,
        chat_id: str,
        *,
        filename: str,
        content: bytes,
        caption: str,
    ) -> dict[str, Any]:
        return self._telegram_post_multipart(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "caption": caption[:1000],
                "parse_mode": "HTML",
            },
            file_field="photo",
            filename=filename,
            content=content,
            content_type="image/png",
        )

    def send_dashboard(self, chat_id: str) -> None:
        keyboard = {
            "inline_keyboard": [
                [{"text": "Alerts", "callback_data": "al"}],
                [{"text": "Charts", "callback_data": "cb_chart"}],
                [{"text": "GEX", "callback_data": "cb_gex"}],
                [{"text": "Status", "callback_data": "cb_status"}],
            ]
        }
        text = (
            "<b>MartinCall Interactive</b>\n"
            "/alerts - alert manager\n"
            "/alert &lt;instrument_id&gt; &lt;price&gt; [cross|up|down] [timeframe]\n"
            "/gexalert &lt;instrument_id&gt; &lt;CALL_WALL|PUT_WALL|GAMMA_FLIP&gt; [cross|up|down] [timeframe]\n"
            "/chart - chart menu\n"
            "/chart &lt;instrument_id&gt; [price|terminal] - browser screenshot\n"
            "/screen &lt;instrument_id&gt; - full terminal screenshot\n"
            "/gex - scheduler and cache\n"
            "/gexmap &lt;instrument_id&gt; - GEX interval map\n"
            "/gexstrike &lt;instrument_id&gt; - GEX strike heatmap\n"
            "/gexmaps &lt;instrument_id&gt; - both GEX maps\n"
            "/status - system, server, database and IBKR\n"
            "/quote &lt;instrument_id&gt; - cached quote"
        )
        self.send_message(chat_id, text, reply_markup=keyboard)

    def _alert_command(
        self, action: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            result = self.alert_command_provider(str(action), dict(payload or {}))
        except Exception as exc:
            logger.warning("Telegram alert command failed (%s): %s", action, exc)
            return {"ok": False, "status": "error", "message": str(exc)}
        return (
            dict(result)
            if isinstance(result, Mapping)
            else {
                "ok": False,
                "status": "invalid_response",
                "message": "Alert manager returned an invalid response.",
            }
        )

    def _watchlist_instrument_for_token(self, token: str) -> dict[str, str] | None:
        matches = [
            item
            for item in self._watchlist_chart_instruments()
            if telegram_callback_token(item["instrument_id"], namespace="instrument") == token
        ]
        return matches[0] if len(matches) == 1 else None

    def send_alerts(self, chat_id: str, *, status: str = "all") -> None:
        result = self._alert_command("list", {"status": "" if status == "all" else status})
        if not result.get("ok"):
            self.send_message(
                chat_id,
                f"Alert manager unavailable: {_escape(result.get('message') or result.get('status') or 'unknown error')}",
            )
            return
        alerts = [item for item in result.get("alerts", []) if isinstance(item, Mapping)]
        counts = result.get("counts") if isinstance(result.get("counts"), Mapping) else {}
        if not counts:
            counts = {
                "active": sum(_telegram_alert_status(item) == "active" for item in alerts),
                "fired": sum(_telegram_alert_status(item) == "fired" for item in alerts),
                "disabled": sum(_telegram_alert_status(item) == "disabled" for item in alerts),
            }
        lines = [
            "<b>🔔 ALERTS</b>",
            "",
            f"🟢 Active: <b>{_fmt_int(counts.get('active'))}</b>",
            f"🟠 Fired: <b>{_fmt_int(counts.get('fired'))}</b>",
            f"⚪ Disabled: <b>{_fmt_int(counts.get('disabled'))}</b>",
            f"⛔ Blocked: <b>{_fmt_int(counts.get('blocked'))}</b>",
        ]
        keyboard_rows: list[list[dict[str, str]]] = [
            [{"text": "➕ Create", "callback_data": "al:new"}],
            [
                {"text": "🟢 Active", "callback_data": "al:l:active"},
                {"text": "🟠 Fired", "callback_data": "al:l:fired"},
                {"text": "⚪ Disabled", "callback_data": "al:l:disabled"},
            ],
            [{"text": "⛔ Blocked", "callback_data": "al:l:blocked"}],
        ]
        if status != "all":
            lines.extend(["", f"<b>{_escape(status.upper())}</b>"])
        for alert in alerts[:12]:
            alert_status = _telegram_alert_status(alert)
            alert_id = str(alert.get("alert_id") or "")
            callback_token = str(alert.get("callback_token") or "")
            if not alert_id or not callback_token:
                continue
            symbol = str(alert.get("symbol") or "?")
            timeframe = str(alert.get("timeframe") or "")
            direction = _telegram_alert_direction(alert.get("direction"))
            price = _fmt_price(alert.get("price"))
            level_source = (
                alert.get("level_source") if isinstance(alert.get("level_source"), Mapping) else {}
            )
            source = str(
                alert.get("source_label")
                or alert.get("gex_level")
                or level_source.get("label")
                or level_source.get("level_kind")
                or ""
            ).strip()
            lines.append(
                f"{_telegram_alert_status_emoji(alert_status)} <b>{_escape(symbol)}</b> "
                f"[{_escape(timeframe)}] {_escape(direction)} {_escape(price)}"
                f"{f' · {_escape(source)}' if source else ''}"
            )
            if alert_status == "blocked":
                keyboard_rows.append(
                    [{"text": f"🗑 {symbol}", "callback_data": f"al:a:x:p:{callback_token}"}]
                )
                continue
            if alert_status == "fired":
                action = "r"
                action_text = f"🔄 {symbol} rearm"
            elif alert_status == "disabled":
                action = "e"
                action_text = f"▶️ {symbol} enable"
            else:
                action = "d"
                action_text = f"⏸ {symbol} disable"
            action_buttons = [
                {"text": action_text, "callback_data": f"al:a:{action}:p:{callback_token}"},
                {"text": "📊", "callback_data": f"al:a:c:p:{callback_token}"},
            ]
            action_buttons.append({"text": "🗑", "callback_data": f"al:a:x:p:{callback_token}"})
            keyboard_rows.append(action_buttons)
        if not alerts:
            lines.extend(["", "No alerts in this section."])
        self.send_message(
            chat_id, "\n".join(lines), reply_markup={"inline_keyboard": keyboard_rows}
        )

    def send_alert_create_menu(self, chat_id: str) -> None:
        instruments = self._watchlist_chart_instruments()
        buttons = [
            {
                "text": item["display"],
                "callback_data": "al:i:"
                + telegram_callback_token(item["instrument_id"], namespace="instrument"),
            }
            for item in instruments
        ]
        self.send_message(
            chat_id,
            "<b>➕ NEW ALERT</b>\nChoose a watchlist instrument.",
            reply_markup={"inline_keyboard": _inline_keyboard_rows(buttons, per_row=3)},
        )

    def create_price_alert_command(self, chat_id: str, args: list[str]) -> None:
        if len(args) < 2:
            self.send_message(
                chat_id,
                "Usage: <code>/alert &lt;instrument_id&gt; &lt;price&gt; [cross|up|down] [timeframe]</code>",
            )
            return
        try:
            instrument_id = require_exact_identity_text(
                args[0], field="TELEGRAM_ALERT_INSTRUMENT_ID"
            )
            price = float_or_none(args[1])
            if price is None:
                raise ValueError("price must be finite")
            direction = _normalize_alert_direction(args[2] if len(args) > 2 else "cross")
            timeframe = str(args[3] if len(args) > 3 else "5m")
        except (TypeError, ValueError) as exc:
            self.send_message(chat_id, f"Invalid alert: {_escape(exc)}")
            return
        self._send_alert_command_result(
            chat_id,
            self._alert_command(
                "create_price",
                {
                    "instrument_id": instrument_id,
                    "price": price,
                    "direction": direction,
                    "timeframe": timeframe,
                },
            ),
            success_label="Price alert armed",
        )

    def create_gex_alert_command(self, chat_id: str, args: list[str]) -> None:
        if len(args) < 2:
            self.send_message(
                chat_id,
                "Usage: <code>/gexalert &lt;instrument_id&gt; &lt;CALL_WALL|PUT_WALL|GAMMA_FLIP&gt; [cross|up|down] [timeframe]</code>",
            )
            return
        try:
            instrument_id = require_exact_identity_text(
                args[0], field="TELEGRAM_ALERT_INSTRUMENT_ID"
            )
            selector = str(args[1]).strip().upper()
            direction = _normalize_alert_direction(args[2] if len(args) > 2 else "cross")
            timeframe = str(args[3] if len(args) > 3 else "5m")
        except ValueError as exc:
            self.send_message(chat_id, f"Invalid GEX alert: {_escape(exc)}")
            return
        snapshot = self._alert_command("gex_levels", {"instrument_id": instrument_id})
        snapshot_version = str(snapshot.get("snapshot_version") or "")
        if not snapshot.get("ok") or not _valid_alert_callback_component(snapshot_version):
            self._send_alert_command_result(
                chat_id,
                snapshot,
                success_label="GEX snapshot loaded",
            )
            return
        self._send_alert_command_result(
            chat_id,
            self._alert_command(
                "create_gex",
                {
                    "instrument_id": instrument_id,
                    "selector": selector,
                    "direction": direction,
                    "timeframe": timeframe,
                    "snapshot_version": snapshot_version,
                },
            ),
            success_label="GEX snapshot alert armed",
        )

    def _consume_alert_input(self, chat_id: str, text: str) -> bool:
        with self._pending_alert_lock:
            pending = dict(self._pending_alert_inputs.get(chat_id) or {})
        if pending.get("kind") != "price":
            return False
        parts = text.split()
        try:
            price = float_or_none(parts[0])
            if price is None:
                raise ValueError("price must be finite")
            direction = _normalize_alert_direction(parts[1] if len(parts) > 1 else "cross")
            timeframe = str(parts[2] if len(parts) > 2 else "5m")
        except (IndexError, TypeError, ValueError) as exc:
            self.send_message(
                chat_id,
                f"Invalid value: {_escape(exc)}\nSend <code>price [cross|up|down] [timeframe]</code>.",
            )
            return True
        with self._pending_alert_lock:
            self._pending_alert_inputs.pop(chat_id, None)
        self._send_alert_command_result(
            chat_id,
            self._alert_command(
                "create_price",
                {
                    "instrument_id": pending.get("instrument_id"),
                    "price": price,
                    "direction": direction,
                    "timeframe": timeframe,
                },
            ),
            success_label="Price alert armed",
        )
        return True

    def handle_alert_callback(self, chat_id: str, command: str, *, message_id: Any = None) -> None:
        parts = command.split(":")
        if command == "al:new":
            self.send_alert_create_menu(chat_id)
            return
        if len(parts) == 3 and parts[:2] == ["al", "l"]:
            self.send_alerts(chat_id, status=parts[2])
            return
        if len(parts) == 3 and parts[:2] == ["al", "i"]:
            instrument = self._watchlist_instrument_for_token(parts[2])
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /alerts.")
                return
            self.send_message(
                chat_id,
                f"<b>{_escape(instrument['display'])}</b> alert source",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Enter price", "callback_data": f"al:p:{parts[2]}"}],
                        [{"text": "GEX levels", "callback_data": f"al:g:{parts[2]}"}],
                    ]
                },
            )
            return
        if len(parts) == 3 and parts[:2] == ["al", "p"]:
            instrument = self._watchlist_instrument_for_token(parts[2])
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /alerts.")
                return
            with self._pending_alert_lock:
                self._pending_alert_inputs[chat_id] = {
                    "kind": "price",
                    "instrument_id": instrument["instrument_id"],
                }
            self.send_message(
                chat_id,
                f"Send the level for <b>{_escape(instrument['display'])}</b> as "
                "<code>price [cross|up|down] [timeframe]</code>.",
            )
            return
        if len(parts) == 3 and parts[:2] == ["al", "g"]:
            instrument = self._watchlist_instrument_for_token(parts[2])
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /alerts.")
                return
            result = self._alert_command(
                "gex_levels", {"instrument_id": instrument["instrument_id"]}
            )
            levels = [item for item in result.get("levels", []) if isinstance(item, Mapping)]
            snapshot_version = str(result.get("snapshot_version") or "")
            if (
                not result.get("ok")
                or not levels
                or not _valid_alert_callback_component(snapshot_version)
            ):
                self.send_message(
                    chat_id,
                    f"No fresh publishable GEX snapshot: {_escape(result.get('message') or result.get('status') or 'unavailable')}",
                )
                return
            rows = []
            for level in levels[:10]:
                selector = str(level.get("selector") or level.get("key") or "").strip()
                if not selector or ":" in selector:
                    continue
                label = str(level.get("label") or selector)
                rows.append(
                    [
                        {
                            "text": f"{label} {_fmt_price(level.get('price'))}",
                            "callback_data": f"al:gl:{parts[2]}:{snapshot_version}:{selector}",
                        }
                    ]
                )
            self.send_message(
                chat_id,
                f"<b>{_escape(instrument['display'])} GEX levels</b>\n"
                f"Snapshot: {_escape(result.get('captured_at') or '-')}\n"
                f"Market data: {_escape(gex_market_data_status_text(result))}",
                reply_markup={"inline_keyboard": rows},
            )
            return
        if len(parts) == 5 and parts[:2] == ["al", "gl"]:
            instrument = self._watchlist_instrument_for_token(parts[2])
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /alerts.")
                return
            snapshot_version, selector = parts[3], parts[4]
            if not _valid_alert_callback_component(snapshot_version):
                self.send_message(chat_id, "GEX snapshot selection expired; reopen /alerts.")
                return
            self.send_message(
                chat_id,
                f"Condition for <b>{_escape(selector)}</b>",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Cross",
                                "callback_data": f"al:gc:{parts[2]}:{snapshot_version}:{selector}:cross",
                            },
                            {
                                "text": "Cross ↑",
                                "callback_data": f"al:gc:{parts[2]}:{snapshot_version}:{selector}:above",
                            },
                            {
                                "text": "Cross ↓",
                                "callback_data": f"al:gc:{parts[2]}:{snapshot_version}:{selector}:below",
                            },
                        ]
                    ]
                },
            )
            return
        if len(parts) == 6 and parts[:2] == ["al", "gc"]:
            instrument = self._watchlist_instrument_for_token(parts[2])
            if instrument is None:
                self.send_message(chat_id, "Instrument route changed; reopen /alerts.")
                return
            result = self._alert_command(
                "create_gex",
                {
                    "instrument_id": instrument["instrument_id"],
                    "snapshot_version": parts[3],
                    "selector": parts[4],
                    "direction": _normalize_alert_direction(parts[5]),
                    "timeframe": "5m",
                },
            )
            self._send_alert_command_result(
                chat_id, result, success_label="GEX snapshot alert armed"
            )
            return
        if len(parts) == 5 and parts[:2] == ["al", "a"]:
            action_code, type_code, callback_token = parts[2], parts[3], parts[4]
            if type_code != "p":
                self.send_message(chat_id, "Unknown alert type.")
                return
            action = {
                "r": "rearm",
                "e": "enable",
                "d": "disable",
                "x": "delete",
                "c": "resolve",
            }.get(action_code)
            if action is None:
                self.send_message(chat_id, "Unknown alert action.")
                return
            result = self._alert_command(
                action,
                {"callback_token": callback_token, "alert_type": "price"},
            )
            if action_code == "c" and result.get("ok") and result.get("instrument_id"):
                self.send_chart_snapshot(chat_id, result["instrument_id"])
                return
            self._send_alert_command_result(chat_id, result, success_label=action.capitalize())
            if result.get("ok") and message_id is not None:
                self._telegram_post(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "reply_markup": {
                            "inline_keyboard": [
                                [
                                    {"text": f"✅ {action.capitalize()}", "callback_data": "al"},
                                ]
                            ]
                        },
                    },
                )
            return
        self.send_message(chat_id, "Alert action expired; reopen /alerts.")

    def _send_alert_command_result(
        self,
        chat_id: str,
        result: Mapping[str, Any],
        *,
        success_label: str,
    ) -> None:
        if not result.get("ok"):
            self.send_message(
                chat_id,
                f"❌ {_escape(result.get('message') or result.get('status') or 'Alert command failed')}",
            )
            return
        alert = result.get("alert") if isinstance(result.get("alert"), Mapping) else result
        symbol = str(alert.get("symbol") or "")
        timeframe = str(alert.get("timeframe") or "")
        price = _fmt_price(alert.get("price"))
        detail = f"\n{_escape(symbol)} [{_escape(timeframe)}] · {_escape(price)}" if symbol else ""
        self.send_message(
            chat_id,
            f"✅ <b>{_escape(success_label)}</b>{detail}",
            reply_markup={"inline_keyboard": [[{"text": "🔔 Alerts", "callback_data": "al"}]]},
        )

    def send_gex_maps(self, chat_id: str, instrument_id: str) -> None:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="TELEGRAM_INSTRUMENT_ID",
        )
        self.send_gex_map(chat_id, instrument_id, mode="interval")
        self.send_gex_map(chat_id, instrument_id, mode="strike")

    def send_gex_map(self, chat_id: str, instrument_id: str, *, mode: str) -> None:
        with self._heavy_lock:
            instrument_id = require_exact_identity_text(
                instrument_id,
                field="TELEGRAM_INSTRUMENT_ID",
            )
            payload = dict(self.gex_provider(instrument_id))
            if payload and payload.get("instrument_id") != instrument_id:
                raise ValueError("Telegram GEX payload instrument identity mismatch")
            display = str(payload.get("display") or payload.get("provider_symbol") or "unknown")
            if not payload or not (payload.get("levels") or payload.get("history")):
                self.send_message(chat_id, f"No cached GEX data for <b>{_escape(display)}</b>.")
                return
            image = (
                render_gex_strike_png(payload)
                if mode == "strike"
                else render_gex_interval_png(payload)
            )
            label = "strike heatmap" if mode == "strike" else "interval map"
            filename = (
                f"gex-{str(payload.get('provider_symbol') or 'instrument').lower()}-{mode}.png"
            )
            caption = (
                f"<b>{_escape(display)} GEX {label}</b>\n"
                f"{_escape(payload.get('status') or 'cache')} · "
                f"{_escape(payload.get('captured_at') or '-')}\n"
                f"Market data: {_escape(gex_market_data_status_text(payload))}"
            )
            self.send_photo(
                chat_id,
                filename=filename,
                content=image,
                caption=caption,
            )

    def send_chart_menu(self, chat_id: str) -> None:
        instruments = self._watchlist_chart_instruments()
        watchlist_label = _escape(", ".join(item["display"] for item in instruments))
        rows: list[list[dict[str, str]]] = []
        for item in instruments:
            token = telegram_callback_token(
                item["instrument_id"],
                namespace="instrument",
            )
            if not token:
                continue
            rows.append(
                [
                    {
                        "text": f"📈 {item['display']}",
                        "callback_data": f"cb_chart_p_{token}",
                    },
                    {
                        "text": f"🖥 {item['display']}",
                        "callback_data": f"cb_chart_t_{token}",
                    },
                ]
            )
        keyboard = {"inline_keyboard": rows}
        text = (
            "<b>Charts</b>\n"
            f"Watchlist: {watchlist_label}\n"
            "📈 Price: price frame with every visible object\n"
            "🖥 Terminal: current terminal window with watchlist and volume\n"
            "Requires an open terminal tab on the exact instrument."
        )
        self.send_message(chat_id, text, reply_markup=keyboard)

    def _watchlist_chart_instruments(self) -> list[dict[str, str]]:
        try:
            instruments = self.watchlist_instruments_provider()
        except Exception:
            instruments = []
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in instruments:
            if not isinstance(item, dict):
                continue
            raw_instrument_id = item.get("instrument_id")
            instrument_id = raw_instrument_id if isinstance(raw_instrument_id, str) else ""
            display = str(item.get("display") or "").strip()
            if not instrument_id or not display or instrument_id in seen:
                continue
            seen.add(instrument_id)
            deduped.append({"instrument_id": instrument_id, "display": display})
        return deduped

    def send_chart_snapshot(
        self,
        chat_id: str,
        instrument_id: str,
        *,
        capture_scope: str = "price",
    ) -> None:
        with self._heavy_lock:
            instrument_id = require_exact_identity_text(
                instrument_id,
                field="TELEGRAM_INSTRUMENT_ID",
            )
            scope = str(capture_scope or "").strip().lower()
            if scope not in {"price", "terminal"}:
                raise ValueError("Telegram chart capture scope is invalid")
            browser_capture: dict[str, Any] = {}
            failure_code = "BROWSER_CAPTURE_UNAVAILABLE"
            try:
                result = self.browser_capture_provider(instrument_id, scope)
                if isinstance(result, Mapping):
                    browser_capture = dict(result)
            except Exception as exc:
                logger.info(
                    "Telegram browser capture unavailable (%s, %s): %s",
                    instrument_id,
                    scope,
                    exc,
                )
            if browser_capture and browser_capture.get("ok") is not True:
                failure_code = str(browser_capture.get("error_code") or "BROWSER_CAPTURE_FAILED")
                logger.info(
                    "Telegram browser capture failed (instrument_id=%s scope=%s error_code=%s)",
                    instrument_id,
                    scope,
                    failure_code,
                )
                browser_capture = {}
            content = browser_capture.get("content")
            if browser_capture:
                invalid_capture = (
                    browser_capture.get("instrument_id") != instrument_id
                    or browser_capture.get("scope") != scope
                    or not isinstance(content, bytes)
                    or len(content) > 9_500_000
                    or not content.startswith(b"\x89PNG\r\n\x1a\n")
                )
                if invalid_capture:
                    failure_code = "BROWSER_CAPTURE_INVALID"
                    logger.warning(
                        "Telegram browser capture rejected (instrument_id=%s scope=%s)",
                        instrument_id,
                        scope,
                    )
                    browser_capture = {}
                    content = None
            if isinstance(content, bytes):
                display = next(
                    (
                        item["display"]
                        for item in self._watchlist_chart_instruments()
                        if item["instrument_id"] == instrument_id
                    ),
                    "Chart",
                )
                scope_label = "PRICE" if scope == "price" else "TERMINAL"
                caption = (
                    f"<b>{_escape(display)} {scope_label.lower()}</b>\n"
                    f"{_escape(browser_capture.get('timeframe') or 'chart')} · "
                    f"{_escape(telegram_capture_header_time(browser_capture.get('captured_at')))}\n"
                    f"Browser tab · {scope_label}"
                )
                self.send_photo(
                    chat_id,
                    filename=f"chart-{scope}.png",
                    content=content,
                    caption=caption,
                )
                return

            self.send_message(
                chat_id,
                f"Chart capture failed: <code>{_escape(failure_code)}</code>. "
                "Open the exact instrument in a terminal tab and retry.",
            )

    def send_status(self, chat_id: str) -> None:
        status = dict(self.system_status_provider())
        app = _dict(status.get("app"))
        sleep = _dict(status.get("sleep"))
        storage = _dict(status.get("storage"))
        ibkr = _dict(status.get("ibkr"))
        resources = host_resources()
        lines = [
            "<b>MartinCall Status</b>",
            f"System: <b>{_escape(status.get('status', 'unknown'))}</b> | Sleep: {'ON' if sleep.get('sleeping') else 'OFF'}",
            f"Checked: {_escape(status.get('checked_at') or datetime.now(tz=UTC).isoformat())}",
            "",
            "<b>Server</b>",
            f"PID: {_escape(app.get('pid', os.getpid()))}",
            f"App uptime: {_fmt_seconds(app.get('uptime_seconds'))}",
            f"RSS: {_fmt_bytes(app.get('rss_bytes'))}",
            f"Memory available: {_fmt_bytes(app.get('mem_available_bytes'))} / {_fmt_bytes(app.get('mem_total_bytes'))}",
            f"Load avg: {_escape(resources.get('load_avg') or 'n/a')}",
            f"Disk free: {_fmt_bytes(resources.get('disk_free_bytes'))} / {_fmt_bytes(resources.get('disk_total_bytes'))}",
            f"Telegram API latency: {self.api_latency_ms:.1f} ms",
            "",
            "<b>Database</b>",
            f"State: {_ok_text(storage)}",
            f"Bars: {_fmt_int(storage.get('bars'))}",
            f"Latest bar: {_escape(storage.get('latest_bar_ts') or '-')}",
            f"Size: {_fmt_bytes(storage.get('database_bytes'))}",
        ]
        providers = storage.get("providers") if isinstance(storage.get("providers"), list) else []
        if providers:
            lines.append("")
            lines.append("<b>Providers</b>")
            for item in providers[:6]:
                row = _dict(item)
                lines.append(
                    f"{_escape(row.get('provider'))}: {_fmt_int(row.get('bars'))} "
                    f"to {_short_ts(row.get('to'))}"
                )
        coverage = storage.get("coverage") if isinstance(storage.get("coverage"), list) else []
        if coverage:
            lines.append("")
            lines.append("<b>Coverage</b>")
            for item in coverage[:8]:
                row = _dict(item)
                lines.append(
                    f"{_escape(row.get('symbol'))} {_escape(row.get('timeframe'))} "
                    f"{_escape(row.get('provider'))}: {_fmt_int(row.get('bars'))} "
                    f"to {_short_ts(row.get('to'))}"
                )
        lines.extend(
            [
                "",
                "<b>IBKR</b>",
                f"State: {_ok_text(ibkr)}",
                f"History error: {_escape(ibkr.get('last_history_error') or 'ok')}",
                f"Chart error: {_escape(ibkr.get('last_chart_error') or 'ok')}",
                f"Quote error: {_escape(ibkr.get('last_quote_error') or 'ok')}",
                f"History duration: {_fmt_seconds(ibkr.get('last_hist_duration_seconds'))}",
                f"Quote subscriptions: {_fmt_int(ibkr.get('quote_subscriptions'))}",
                f"Quote values: {_fmt_int(ibkr.get('quote_values'))}",
                f"Chart streams: {_fmt_int(ibkr.get('chart_streams'))}",
            ]
        )
        self.send_message(chat_id, "\n".join(lines))

    def send_gex(self, chat_id: str) -> None:
        status = dict(self.system_status_provider())
        gex = _dict(status.get("gex"))
        scheduler = _dict(gex.get("scheduler"))
        lines = [
            "<b>GEX</b>",
            f"State: {_ok_text(gex)}",
            f"Scheduler: {'ON' if scheduler.get('enabled') else 'OFF'} "
            f"{_escape(', '.join(scheduler.get('instrument_ids') or []))}",
            f"Last error: {_escape(gex.get('last_error') or 'ok')}",
            f"Requests: {_fmt_int(gex.get('request_count'))}",
            f"Snapshots: {_fmt_int(gex.get('snapshot_count'))}",
        ]
        instruments = self._watchlist_chart_instruments()
        keyboard = {
            "inline_keyboard": _inline_keyboard_rows(
                [
                    {
                        "text": f"{item['display']} MAP",
                        "callback_data": f"cb_gex_map_{item['instrument_id']}",
                    }
                    for item in instruments
                ]
                + [
                    {
                        "text": f"{item['display']} STRIKE",
                        "callback_data": f"cb_gex_strike_{item['instrument_id']}",
                    }
                    for item in instruments
                ],
                per_row=3,
            )
        }
        self.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)

    def send_quote(self, chat_id: str, instrument_id: str) -> None:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="TELEGRAM_INSTRUMENT_ID",
        )
        quote = dict(self.quote_provider(instrument_id))
        if quote and quote.get("instrument_id") != instrument_id:
            raise ValueError("Telegram quote payload instrument identity mismatch")
        display = str(quote.get("display") or quote.get("symbol") or "unknown")
        price = float_or_none(quote.get("price"))
        if price is None:
            price = float_or_none(quote.get("last"))
        if price is None:
            price = float_or_none(quote.get("market_price"))
        ts = quote.get("ts") or quote.get("time")
        message = quote.get("message") or quote.get("status") or ""
        time_str = _escape(ts or "-")

        line1 = f"💎 <b>{html.escape(display)}</b> | <code>{time_str}</code>"
        line2 = f"<b>Price: {_fmt_price(price)}</b> | Bid: <b>{_fmt_price(quote.get('bid'))}</b> | Ask: <b>{_fmt_price(quote.get('ask'))}</b>"

        lines = [line1, line2]
        if message:
            lines.append(f"💬 Note: {_escape(message)}")
        self.send_message(chat_id, "\n".join(lines))


def host_resources() -> dict[str, Any]:
    try:
        load_avg = ", ".join(f"{value:.2f}" for value in os.getloadavg())
    except Exception:
        load_avg = ""
    try:
        disk = shutil.disk_usage("/")
        disk_total = int(disk.total)
        disk_free = int(disk.free)
    except Exception:
        disk_total = 0
        disk_free = 0
    return {
        "load_avg": load_avg,
        "disk_total_bytes": disk_total,
        "disk_free_bytes": disk_free,
    }


def _inline_keyboard_rows(
    buttons: list[dict[str, str]], *, per_row: int = 3
) -> list[list[dict[str, str]]]:
    width = max(int(per_row), 1)
    return [buttons[index : index + width] for index in range(0, len(buttons), width)]


def _normalize_alert_direction(value: Any) -> str:
    normalized = str(value or "cross").strip().lower().replace("-", "_")
    mapping = {
        "cross": "cross",
        "up": "above",
        "above": "above",
        "cross_up": "above",
        "down": "below",
        "below": "below",
        "cross_down": "below",
    }
    if normalized not in mapping:
        raise ValueError("direction must be cross, up, or down")
    return mapping[normalized]


def _telegram_alert_status(alert: Mapping[str, Any]) -> str:
    explicit = str(alert.get("status") or "").strip().lower()
    if explicit in {"active", "fired", "disabled", "blocked"}:
        return explicit
    if alert.get("enabled") is False or alert.get("armed") is False and not alert.get("fired"):
        return "disabled"
    if bool(alert.get("fired")):
        return "fired"
    return "active"


def _telegram_alert_status_emoji(status: str) -> str:
    return {"active": "🟢", "fired": "🟠", "disabled": "⚪", "blocked": "⛔"}.get(status, "⚪")


def _telegram_alert_direction(value: Any) -> str:
    return {"above": "cross ↑", "below": "cross ↓", "cross": "cross"}.get(
        str(value or "cross").lower(),
        str(value or "cross"),
    )


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _ok_text(value: Mapping[str, Any]) -> str:
    return "OK" if bool(value.get("ok")) else "DEGRADED"


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def _fmt_price(value: Any) -> str:
    number = float_or_none(value)
    if number is None:
        return "-"
    return f"{number:,.2f}"


def _fmt_bytes(value: Any) -> str:
    try:
        number = float(value or 0)
    except Exception:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while abs(number) >= 1024 and index < len(units) - 1:
        number /= 1024.0
        index += 1
    return f"{number:.1f} {units[index]}"


def _fmt_seconds(value: Any) -> str:
    try:
        seconds = max(float(value or 0), 0.0)
    except Exception:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.1f}d"


def _short_ts(value: Any) -> str:
    text = str(value or "-")
    return html.escape(text.replace("T", " ")[:16])


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(exc).lower()


def _update_command(update: Mapping[str, Any]) -> str:
    callback_query = update.get("callback_query")
    if isinstance(callback_query, Mapping):
        return str(callback_query.get("data") or "")
    message = update.get("message")
    if isinstance(message, Mapping):
        text = str(message.get("text") or "").strip()
        if text:
            return text.split()[0].split("@", 1)[0].lower()
    return ""
