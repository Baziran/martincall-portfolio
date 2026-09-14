from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aef_terminal.research.channel_interaction_journal import (
    ChannelInteractionJournal,
)
from aef_terminal.research.option_reversal_journal import (
    OptionReversalJournal,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.metrics import increment_metric, observe_metric
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_RESEARCH_REFRESH_SECONDS,
)


MARKET_ANALYSIS_RESEARCH_MIN_INTERVAL_SECONDS = MARKET_ANALYSIS_RESEARCH_REFRESH_SECONDS
_LOGGER = logging.getLogger("aef_terminal.ui.market_analysis_research")


def _write_research_capture(capture_bytes: bytes) -> None:
    snapshot = json.loads(capture_bytes)
    if not isinstance(snapshot, dict):
        raise TypeError("market analysis research capture must be a JSON object")
    ChannelInteractionJournal().record_prospective_snapshot(snapshot)
    OptionReversalJournal().record_prospective_snapshot(snapshot)


class MarketAnalysisResearchWorker:
    """Bounded latest-wins consumer for non-authoritative research capture."""

    def __init__(
        self,
        *,
        min_interval_seconds: float = (MARKET_ANALYSIS_RESEARCH_MIN_INTERVAL_SECONDS),
    ) -> None:
        self.min_interval_seconds = max(
            float(min_interval_seconds),
            0.0,
        )
        self._pending: dict[str, bytes] = {}
        self._last_started: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._running_scope = ""
        self._completed = 0
        self._failed = 0
        self._superseded = 0

    def submit(self, scope: str, capture_bytes: bytes) -> None:
        if self._closing:
            return
        if not isinstance(scope, str) or not scope:
            raise ValueError("research capture scope must be exact text")
        if not isinstance(capture_bytes, bytes) or not capture_bytes:
            raise ValueError("research capture must be non-empty bytes")
        if scope in self._pending:
            self._superseded += 1
            increment_metric(
                "market_analysis_research_capture_total",
                status="superseded",
            )
        self._pending[scope] = capture_bytes
        self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name="market-analysis-research-worker",
            )

    async def _run(self) -> None:
        while self._pending and not self._closing:
            now = time.monotonic()
            scope = min(
                self._pending,
                key=lambda item: self._last_started.get(item, 0.0),
            )
            last_started = self._last_started.get(scope)
            delay = (
                max(
                    self.min_interval_seconds - (now - last_started),
                    0.0,
                )
                if last_started is not None
                else 0.0
            )
            if delay > 0:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            capture_bytes = self._pending.pop(scope)
            self._last_started[scope] = time.monotonic()
            self._running_scope = scope
            started = time.perf_counter()
            status = "ok"
            try:
                await run_physical_thread_call(
                    _write_research_capture,
                    capture_bytes,
                )
                self._completed += 1
            except Exception:
                status = "error"
                self._failed += 1
                _LOGGER.exception(
                    "market analysis research capture failed for scope %s",
                    scope,
                )
            finally:
                self._running_scope = ""
                increment_metric(
                    "market_analysis_research_capture_total",
                    status=status,
                )
                observe_metric(
                    "market_analysis_research_capture_seconds",
                    time.perf_counter() - started,
                    status=status,
                )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "pending_scopes": len(self._pending),
            "running": bool(self._running_scope),
            "running_scope": self._running_scope,
            "completed": self._completed,
            "failed": self._failed,
            "superseded": self._superseded,
            "min_interval_seconds": self.min_interval_seconds,
        }

    async def shutdown(self) -> None:
        self._closing = True
        self._pending.clear()
        self._wake.set()
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
