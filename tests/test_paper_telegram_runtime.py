from __future__ import annotations

import asyncio
import threading
import time
from queue import Queue
from typing import Any

from aef_terminal.settings_contract import PAPER_TELEGRAM_SETTING_KEY
from aef_terminal.ui import paper_telegram_runtime


def _reset_runtime(monkeypatch) -> None:
    monkeypatch.setattr(paper_telegram_runtime, "PAPER_TELEGRAM_QUEUE", Queue(maxsize=256))
    monkeypatch.setattr(paper_telegram_runtime, "PAPER_TELEGRAM_THREAD", None)
    monkeypatch.setattr(paper_telegram_runtime, "PAPER_TELEGRAM_STOP", threading.Event())
    monkeypatch.setattr(paper_telegram_runtime, "PAPER_TELEGRAM_LOCK", threading.RLock())
    monkeypatch.setattr(paper_telegram_runtime, "PAPER_TELEGRAM_ACCEPTING", True)
    monkeypatch.setattr(
        paper_telegram_runtime,
        "_DEPS",
        paper_telegram_runtime.PaperTelegramRuntimeDeps(
            client_settings_snapshot=lambda: {PAPER_TELEGRAM_SETTING_KEY: "true"}
        ),
    )


def test_paper_telegram_stop_settles_active_send_and_discards_queued_work(monkeypatch) -> None:
    _reset_runtime(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, Any] = {}

    def send(_message: str, *, silent: bool) -> dict[str, Any]:
        assert silent is True
        entered.set()
        assert release.wait(timeout=2.0)
        return {"ok": True, "status": "sent"}

    monkeypatch.setattr(paper_telegram_runtime, "send_telegram_message", send)
    paper_telegram_runtime.ensure_paper_telegram_worker()
    paper_telegram_runtime.queue_paper_trade_telegram("open", {"id": "active"})
    assert entered.wait(timeout=1.0)
    paper_telegram_runtime.queue_paper_trade_telegram("close", {"id": "queued"})

    def stop() -> None:
        outcome.update(paper_telegram_runtime.stop_paper_telegram_worker(timeout=1.0))

    stopper = threading.Thread(target=stop)
    stopper.start()
    time.sleep(0.05)
    assert stopper.is_alive()
    release.set()
    stopper.join(timeout=2.0)

    assert stopper.is_alive() is False
    assert outcome == {
        "drained": True,
        "thread_alive": False,
        "discarded": 1,
        "queue_size": 0,
    }
    assert paper_telegram_runtime.PAPER_TELEGRAM_THREAD is None
    assert paper_telegram_runtime.PAPER_TELEGRAM_ACCEPTING is False


def test_paper_telegram_stop_retains_undrained_thread_owner(monkeypatch) -> None:
    _reset_runtime(monkeypatch)

    class StuckThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float] = []

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float) -> None:
            self.join_timeouts.append(timeout)

    thread = StuckThread()
    paper_telegram_runtime.PAPER_TELEGRAM_THREAD = thread  # type: ignore[assignment]

    outcome = paper_telegram_runtime.stop_paper_telegram_worker(timeout=0.1)

    assert outcome["drained"] is False
    assert outcome["thread_alive"] is True
    assert paper_telegram_runtime.PAPER_TELEGRAM_THREAD is thread
    assert thread.join_timeouts == [0.1]


def test_paper_telegram_async_start_runs_outside_event_loop(monkeypatch) -> None:
    event_loop_thread = threading.get_ident()
    start_threads: list[int] = []
    monkeypatch.setattr(
        paper_telegram_runtime,
        "ensure_paper_telegram_worker",
        lambda: start_threads.append(threading.get_ident()),
    )

    asyncio.run(paper_telegram_runtime.ensure_paper_telegram_worker_async())

    assert start_threads
    assert start_threads[0] != event_loop_thread
