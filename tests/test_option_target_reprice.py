from __future__ import annotations

import asyncio
import threading

import pytest

from aef_terminal.ui.services.option_target_reprice import (
    _commit_option_target_updates,
    _run_option_target_reprice_cycle,
)
from aef_terminal.ui.services import option_target_reprice
from aef_terminal.runtime.option_target_changes import option_target_mutation


def test_reprice_reuses_unchanged_rows_but_still_reprices_and_reconciles(monkeypatch):
    reads = []
    repriced = []
    reconciled = []
    rows = []

    class Store:
        def read_option_targets(self):
            reads.append(1)
            return list(rows)

    store = Store()
    state = option_target_reprice._OptionTargetRepriceState()
    monkeypatch.setattr(
        option_target_reprice,
        "reconcile_option_target_quote_consumers",
        lambda items: reconciled.append(list(items)),
    )

    def reprice(items, **_kwargs):
        repriced.append(list(items))
        return []

    monkeypatch.setattr(option_target_reprice, "reprice_option_target_rows", reprice)

    async def cycle():
        await _run_option_target_reprice_cycle(
            state=state,
            store_factory=lambda: store,
            option_target_caps_settings=lambda: {},
            quote_cache_for_instruments=lambda *_args, **_kwargs: (None, "", object()),
            parse_iso_ts=lambda _value: None,
            option_target_samples_committed=lambda *_args, **_kwargs: pytest.fail("no changes"),
        )

    async def run():
        for _ in range(30):
            await cycle()
        assert len(reads) == 1
        assert len(repriced) == len(reconciled) == 30
        with option_target_mutation() as mutation:
            rows.append({"id": "new"})
            mutation.changed = True
        await cycle()
        for _ in range(10):
            await cycle()
        assert len(reads) == 2
        assert repriced[-1] == reconciled[-1] == [{"id": "new"}]
        with option_target_mutation() as mutation:
            rows.clear()
            mutation.changed = True
        await cycle()
        assert len(reads) == 3
        assert repriced[-1] == reconciled[-1] == []

    asyncio.run(run())


def test_reprice_rejects_raced_read_and_retries_failed_read(monkeypatch):
    reads = 0
    reconciled = []

    class Store:
        def read_option_targets(self):
            nonlocal reads
            reads += 1
            if reads == 1:
                with option_target_mutation() as mutation:
                    mutation.changed = True
                return [{"id": "stale"}]
            if reads == 2:
                raise RuntimeError("storage unavailable")
            return [{"id": "current"}]

    store = Store()
    state = option_target_reprice._OptionTargetRepriceState()
    monkeypatch.setattr(
        option_target_reprice,
        "reconcile_option_target_quote_consumers",
        lambda rows: reconciled.append(rows),
    )
    monkeypatch.setattr(
        option_target_reprice, "reprice_option_target_rows", lambda *_args, **_kwargs: []
    )

    async def cycle():
        await _run_option_target_reprice_cycle(
            state=state,
            store_factory=lambda: store,
            option_target_caps_settings=lambda: {},
            quote_cache_for_instruments=lambda *_args, **_kwargs: (None, "", object()),
            parse_iso_ts=lambda _value: None,
            option_target_samples_committed=lambda *_args, **_kwargs: pytest.fail("no changes"),
        )

    async def run():
        await cycle()
        assert not reconciled
        assert state.rows is None
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await cycle()
        assert not reconciled
        await cycle()
        assert reconciled == [[{"id": "current"}]]

    asyncio.run(run())


def test_option_target_commit_publishes_before_cancellation_is_exposed() -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    published = asyncio.Event()
    committed = {("instrument", "route", "target"): {"sample": "committed"}}
    updates = [("instrument", "route", "target", {"sample": "next"})]

    class Store:
        def update_option_target_market_samples(self, received):
            assert received == updates
            started.set()
            assert release.wait(timeout=1.0)
            completed.set()
            return committed

    store = Store()

    async def publish(received, *, store: object) -> None:
        assert received == committed
        assert isinstance(store, Store)
        published.set()

    async def run() -> None:
        task = asyncio.create_task(
            _commit_option_target_updates(
                store,
                updates,
                publish,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert published.is_set()

    asyncio.run(run())
    assert completed.is_set()


def test_option_target_cycle_finishes_reconciliation_and_commit_before_cancellation(
    monkeypatch,
) -> None:
    reconcile_started = threading.Event()
    reconcile_release = threading.Event()
    stages: list[str] = []
    updates = [("instrument", "route", "target", {"sample": "next"})]
    committed = {("instrument", "route", "target"): {"sample": "committed"}}

    class Store:
        def read_option_targets(self):
            stages.append("read")
            return [{"id": "target"}]

        def update_option_target_market_samples(self, received):
            assert received == updates
            stages.append("commit")
            return committed

    store = Store()

    def reconcile(rows) -> None:
        assert rows == [{"id": "target"}]
        stages.append("reconcile_started")
        reconcile_started.set()
        assert reconcile_release.wait(timeout=1.0)
        stages.append("reconcile_finished")

    def reprice(rows, **_kwargs):
        assert rows == [{"id": "target"}]
        stages.append("reprice")
        return updates

    async def publish(received, *, store: object) -> None:
        assert received == committed
        assert isinstance(store, Store)
        stages.append("publish")

    monkeypatch.setattr(
        option_target_reprice,
        "reconcile_option_target_quote_consumers",
        reconcile,
    )
    monkeypatch.setattr(
        option_target_reprice,
        "reprice_option_target_rows",
        reprice,
    )

    async def run() -> None:
        task = asyncio.create_task(
            _run_option_target_reprice_cycle(
                state=option_target_reprice._OptionTargetRepriceState(),
                store_factory=lambda: store,
                option_target_caps_settings=lambda: {},
                quote_cache_for_instruments=lambda *_args, **_kwargs: (None, "", object()),
                parse_iso_ts=lambda _value: None,
                option_target_samples_committed=publish,
            )
        )
        assert await asyncio.to_thread(reconcile_started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        reconcile_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        reconcile_release.set()

    assert stages == [
        "read",
        "reconcile_started",
        "reconcile_finished",
        "reprice",
        "commit",
        "publish",
    ]
