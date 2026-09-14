from __future__ import annotations

import asyncio
import json
import threading

from aef_terminal.ui.services import market_analysis_research_worker


def test_runtime_capture_uses_first_observation_journal_lane(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class ChannelJournal:
        def record_prospective_snapshot(self, snapshot: dict) -> None:
            calls.append(("channel", snapshot))

    class OptionJournal:
        def record_prospective_snapshot(self, snapshot: dict) -> None:
            calls.append(("option", snapshot))

    monkeypatch.setattr(
        market_analysis_research_worker,
        "ChannelInteractionJournal",
        ChannelJournal,
    )
    monkeypatch.setattr(
        market_analysis_research_worker,
        "OptionReversalJournal",
        OptionJournal,
    )
    snapshot = {"meta": {"instrument_id": "exact"}}

    market_analysis_research_worker._write_research_capture(json.dumps(snapshot).encode("utf-8"))

    assert calls == [("channel", snapshot), ("option", snapshot)]


def test_research_worker_is_nonblocking_latest_wins(monkeypatch) -> None:
    writes: list[bytes] = []
    first_started = threading.Event()
    release_first = threading.Event()

    def write(value: bytes) -> None:
        writes.append(value)
        if len(writes) == 1:
            first_started.set()
            assert release_first.wait(1.0)

    monkeypatch.setattr(market_analysis_research_worker, "_write_research_capture", write)

    async def scenario() -> dict:
        worker = market_analysis_research_worker.MarketAnalysisResearchWorker(
            min_interval_seconds=0.0,
        )
        worker.submit("route", b"first")
        while not first_started.is_set():
            await asyncio.sleep(0)
        worker.submit("route", b"second")
        worker.submit("route", b"latest")
        release_first.set()
        for _ in range(100):
            if worker.diagnostics()["completed"] == 2:
                break
            await asyncio.sleep(0)
        diagnostics = worker.diagnostics()
        await worker.shutdown()
        return diagnostics

    diagnostics = asyncio.run(scenario())

    assert writes == [b"first", b"latest"]
    assert diagnostics["completed"] == 2
    assert diagnostics["superseded"] == 1
