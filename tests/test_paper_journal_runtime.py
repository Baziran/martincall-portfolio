from __future__ import annotations

import asyncio
import threading

import pytest

from aef_terminal.paper_contract import PaperContractIdentity
from aef_terminal.paper_journal_runtime import PaperJournalRuntime


PAPER_CONTRACT = PaperContractIdentity(
    scope_kind="instrument",
    provider="ibkr",
    provider_contract_id="paper-runtime",
).to_payload()


def _order(entity_id: str, *, status: str) -> dict:
    return {
        "id": entity_id,
        "instrument_id": "ibkr|contract|paper-runtime",
        "route_fingerprint": "ibkr|route|paper-runtime",
        "provider": "ibkr",
        "provider_contract_id": "paper-runtime",
        "paper_contract": PAPER_CONTRACT,
        "status": status,
    }


def _position(entity_id: str, *, status: str) -> dict:
    payload = _order(entity_id, status=status)
    return {
        "id": entity_id,
        "instrument_id": payload["instrument_id"],
        "route_fingerprint": payload["route_fingerprint"],
        "paper_contract": PAPER_CONTRACT,
        "status": status,
        "payload": payload,
    }


def test_active_snapshot_reads_once_then_only_after_committed_mutation() -> None:
    registry = PaperJournalRuntime()

    class Store:
        order_reads = 0
        position_reads = 0

        @classmethod
        def read_paper_orders(cls, **kwargs):
            assert kwargs == {"status": "pending", "complete_pending": True}
            cls.order_reads += 1
            return [_order("order-1", status="pending")]

        @classmethod
        def read_paper_positions(cls, **kwargs):
            assert kwargs == {"status": "open", "complete_open": True}
            cls.position_reads += 1
            return [_position("position-1", status="open")]

    async def scenario() -> None:
        first = await registry.active_snapshot(Store)
        second = await registry.active_snapshot(Store)
        assert first == second
        assert Store.order_reads == 1
        assert Store.position_reads == 1

        registry.mark_committed()
        await registry.active_snapshot(Store)
        assert Store.order_reads == 2
        assert Store.position_reads == 2

    asyncio.run(scenario())


def test_active_snapshot_rejects_non_active_rows() -> None:
    registry = PaperJournalRuntime()

    class Store:
        @staticmethod
        def read_paper_orders(**_kwargs):
            return [_order("order-1", status="filled")]

        @staticmethod
        def read_paper_positions(**_kwargs):
            return []

    with pytest.raises(ValueError, match="PAPER_ORDER_RUNTIME_STATUS_INVALID"):
        asyncio.run(registry.active_snapshot(Store))


def test_active_snapshot_coalesces_concurrent_refreshes() -> None:
    registry = PaperJournalRuntime()
    read_started = threading.Event()
    release = threading.Event()

    class Store:
        order_reads = 0
        position_reads = 0

        @classmethod
        def read_paper_orders(cls, **_kwargs):
            cls.order_reads += 1
            read_started.set()
            assert release.wait(timeout=2)
            return [_order("order-1", status="pending")]

        @classmethod
        def read_paper_positions(cls, **_kwargs):
            cls.position_reads += 1
            return [_position("position-1", status="open")]

    async def scenario() -> None:
        first = asyncio.create_task(registry.active_snapshot(Store))
        second = asyncio.create_task(registry.active_snapshot(Store))
        for _ in range(100):
            if read_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert read_started.is_set()
        await asyncio.sleep(0.01)
        assert Store.order_reads == 1

        release.set()
        first_snapshot, second_snapshot = await asyncio.gather(first, second)
        assert first_snapshot == second_snapshot
        assert Store.order_reads == 1
        assert Store.position_reads == 1

    asyncio.run(scenario())


def test_active_snapshot_reloads_if_commit_occurs_during_refresh() -> None:
    registry = PaperJournalRuntime()
    first_order_read = threading.Event()
    release = threading.Event()

    class Store:
        order_reads = 0
        position_reads = 0
        current = "old"

        @classmethod
        def read_paper_orders(cls, **_kwargs):
            cls.order_reads += 1
            entity = cls.current
            if cls.order_reads == 1:
                first_order_read.set()
                assert release.wait(timeout=2)
            return [_order(f"order-{entity}", status="pending")]

        @classmethod
        def read_paper_positions(cls, **_kwargs):
            cls.position_reads += 1
            return [_position(f"position-{cls.current}", status="open")]

    async def scenario() -> None:
        task = asyncio.create_task(registry.active_snapshot(Store))
        for _ in range(100):
            if first_order_read.is_set():
                break
            await asyncio.sleep(0.01)
        assert first_order_read.is_set()

        Store.current = "new"
        registry.mark_committed()
        release.set()
        orders, positions = await task

        assert [row["id"] for row in orders] == ["order-new"]
        assert [row["id"] for row in positions] == ["position-new"]
        assert Store.order_reads == 2
        assert Store.position_reads == 2

    asyncio.run(scenario())


def test_active_snapshot_cancellation_waits_for_physical_refresh() -> None:
    registry = PaperJournalRuntime()
    read_started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Store:
        @staticmethod
        def read_paper_orders(**_kwargs):
            read_started.set()
            assert release.wait(timeout=2)
            finished.set()
            return [_order("order-1", status="pending")]

        @staticmethod
        def read_paper_positions(**_kwargs):
            return [_position("position-1", status="open")]

    async def scenario() -> None:
        task = asyncio.create_task(registry.active_snapshot(Store))
        for _ in range(100):
            if read_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert read_started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() is True

    asyncio.run(scenario())
