from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Any

from aef_terminal.alerts.telegram import format_paper_trade_message, send_telegram_message
from aef_terminal.config import AppConfig
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.settings_contract import (
    PAPER_TELEGRAM_SETTING_KEY,
    invalid_client_setting_value_keys,
)

_LOGGER = logging.getLogger(__name__)

PAPER_TELEGRAM_QUEUE: Queue[tuple[str, dict[str, Any]]] = Queue(maxsize=256)
PAPER_TELEGRAM_THREAD: threading.Thread | None = None
PAPER_TELEGRAM_STOP = threading.Event()
PAPER_TELEGRAM_LOCK = threading.RLock()
PAPER_TELEGRAM_ACCEPTING = True


@dataclass(frozen=True)
class PaperTelegramRuntimeDeps:
    client_settings_snapshot: Callable[[], dict[str, Any]]


_DEPS: PaperTelegramRuntimeDeps | None = None


def configure_paper_telegram_runtime_deps(deps: PaperTelegramRuntimeDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> PaperTelegramRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("paper telegram runtime dependencies are not configured")
    return _DEPS


def _paper_telegram_worker() -> None:
    while not PAPER_TELEGRAM_STOP.is_set():
        try:
            event, trade = PAPER_TELEGRAM_QUEUE.get(timeout=0.5)
        except Empty:
            continue
        try:
            message = format_paper_trade_message(trade, event=event)
            result = send_telegram_message(message, silent=True)
            if not result.get("ok") and result.get("status") not in {"disabled", "not_configured"}:
                _LOGGER.warning(
                    "Paper Telegram %s failed: %s", event, result.get("status") or result
                )
        except Exception as exc:
            _LOGGER.warning("Paper Telegram %s failed: %s", event, exc)
        finally:
            PAPER_TELEGRAM_QUEUE.task_done()


def ensure_paper_telegram_worker() -> None:
    global PAPER_TELEGRAM_THREAD, PAPER_TELEGRAM_ACCEPTING
    with PAPER_TELEGRAM_LOCK:
        if PAPER_TELEGRAM_THREAD is not None and PAPER_TELEGRAM_THREAD.is_alive():
            if PAPER_TELEGRAM_STOP.is_set():
                raise RuntimeError("PAPER_TELEGRAM_PREVIOUS_WORKER_UNDRAINED")
            PAPER_TELEGRAM_ACCEPTING = True
            return
        PAPER_TELEGRAM_STOP.clear()
        PAPER_TELEGRAM_ACCEPTING = True
        PAPER_TELEGRAM_THREAD = threading.Thread(
            target=_paper_telegram_worker, name="paper-telegram-feed", daemon=True
        )
        PAPER_TELEGRAM_THREAD.start()


async def ensure_paper_telegram_worker_async() -> None:
    await run_physical_thread_call(ensure_paper_telegram_worker)


def stop_paper_telegram_worker(timeout: float | None = None) -> dict[str, Any]:
    global PAPER_TELEGRAM_THREAD, PAPER_TELEGRAM_ACCEPTING
    with PAPER_TELEGRAM_LOCK:
        PAPER_TELEGRAM_ACCEPTING = False
        PAPER_TELEGRAM_STOP.set()
        discarded = 0
        while True:
            try:
                PAPER_TELEGRAM_QUEUE.get_nowait()
            except Empty:
                break
            else:
                PAPER_TELEGRAM_QUEUE.task_done()
                discarded += 1
        thread = PAPER_TELEGRAM_THREAD
        join_timeout = max(
            float(timeout if timeout is not None else AppConfig().telegram_timeout_seconds + 1.0),
            0.1,
        )
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
        thread_alive = bool(thread is not None and thread.is_alive())
        if not thread_alive and PAPER_TELEGRAM_THREAD is thread:
            PAPER_TELEGRAM_THREAD = None
        return {
            "drained": not thread_alive,
            "thread_alive": thread_alive,
            "discarded": discarded,
            "queue_size": PAPER_TELEGRAM_QUEUE.qsize(),
        }


async def stop_paper_telegram_worker_async() -> dict[str, Any]:
    return await run_physical_thread_call(stop_paper_telegram_worker)


def paper_telegram_feed_enabled() -> bool:
    try:
        settings = _deps().client_settings_snapshot()
    except Exception:
        return False
    raw_value = settings.get(PAPER_TELEGRAM_SETTING_KEY)
    if raw_value is None or invalid_client_setting_value_keys(
        {PAPER_TELEGRAM_SETTING_KEY: raw_value}
    ):
        return False
    return raw_value == "true"


def paper_telegram_feed_status() -> dict[str, Any]:
    enabled = paper_telegram_feed_enabled()
    with PAPER_TELEGRAM_LOCK:
        return {
            "enabled": enabled,
            "silent": True,
            "setting_key": PAPER_TELEGRAM_SETTING_KEY,
            "queue_size": PAPER_TELEGRAM_QUEUE.qsize(),
            "thread_alive": bool(
                PAPER_TELEGRAM_THREAD is not None and PAPER_TELEGRAM_THREAD.is_alive()
            ),
            "accepting": PAPER_TELEGRAM_ACCEPTING,
        }


def queue_paper_trade_telegram(event: str, trade: dict[str, Any]) -> None:
    if not paper_telegram_feed_enabled():
        return
    with PAPER_TELEGRAM_LOCK:
        if not PAPER_TELEGRAM_ACCEPTING:
            return
        ensure_paper_telegram_worker()
        try:
            PAPER_TELEGRAM_QUEUE.put_nowait((str(event or "open"), dict(trade or {})))
        except Exception as exc:
            _LOGGER.warning("Paper Telegram queue dropped %s: %s", event, exc)
