from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from aef_terminal.config import AppConfig
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.runtime.constants import load_runtime_constants
from aef_terminal.ui.services import quote_snapshots
from aef_terminal.ui.services.quote_snapshots import (
    persist_quote_snapshots_async,
    quote_snapshot_write_stats,
    reset_quote_snapshot_state,
)
from tests.provider_payloads import ibkr_stock_payload


def _current_last_quote(price: float, **fields) -> dict:
    observed_at = datetime.now(tz=UTC).isoformat()
    return {
        "price": price,
        "price_source": "last",
        "last": price,
        "provider_ts": observed_at,
        "time_basis": "provider_event",
        **fields,
    }


def test_quote_snapshot_interval_is_configurable_and_clamped_for_runtime() -> None:
    assert AppConfig(quote_snapshot_seconds=7.5).quote_snapshot_seconds == 7.5
    assert (
        load_runtime_constants(AppConfig(quote_snapshot_seconds=0.2))["QUOTE_SNAPSHOT_SECONDS"]
        == 1.0
    )


def test_quote_snapshot_persistence_uses_exact_route_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_quote_snapshot_state()
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    writes = []
    operation_timeouts: list[dict[str, int]] = []

    @contextmanager
    def capture_operation_timeouts(**kwargs):
        operation_timeouts.append(dict(kwargs))
        yield

    monkeypatch.setattr(
        quote_snapshots,
        "postgres_operation_timeouts",
        capture_operation_timeouts,
    )

    class Store:
        def upsert_quote_snapshots(self, snapshots):
            writes.extend(snapshots)

    asyncio.run(
        persist_quote_snapshots_async(
            {
                route.fingerprint: _current_last_quote(
                    750.25,
                    bid=750.2,
                    ask=750.3,
                )
            },
            instruments=[instrument],
            store_factory=Store,
        )
    )

    assert writes == [
        (
            route.instrument_id,
            route.fingerprint,
            route.provider_symbol,
            writes[0][3],
            {
                "price": 750.25,
                "price_source": "last",
                "last": 750.25,
                "bid": 750.2,
                "ask": 750.3,
                "provider_ts": writes[0][4]["provider_ts"],
                "time_basis": "provider_event",
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
            },
            "ibkr",
        )
    ]
    assert operation_timeouts == [
        {
            "statement_timeout_ms": 2_000,
            "lock_timeout_ms": 500,
        }
    ]
    assert quote_snapshot_write_stats()["persisted"] == 1


def test_quote_snapshot_persistence_failure_is_not_swallowed_or_marked_persisted() -> None:
    reset_quote_snapshot_state()
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)

    class FailingStore:
        def upsert_quote_snapshots(self, _snapshots):
            raise RuntimeError("snapshot database unavailable")

    with pytest.raises(RuntimeError, match="snapshot database unavailable"):
        asyncio.run(
            persist_quote_snapshots_async(
                {route.fingerprint: _current_last_quote(750.25)},
                instruments=[instrument],
                store_factory=FailingStore,
            )
        )

    assert quote_snapshot_write_stats()["persisted"] == 0


def test_quote_snapshot_persistence_settles_worker_before_cancellation() -> None:
    reset_quote_snapshot_state()
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Store:
        def upsert_quote_snapshots(self, _snapshots):
            started.set()
            assert release.wait(1.0)
            finished.set()

    async def run() -> None:
        task = asyncio.create_task(
            persist_quote_snapshots_async(
                {route.fingerprint: _current_last_quote(750.25)},
                instruments=[instrument],
                store_factory=Store,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()

    assert finished.is_set()


def test_quote_snapshot_persistence_state_evicts_routes_outside_current_generation() -> None:
    reset_quote_snapshot_state()
    spy = ibkr_stock_payload("SPY")
    qqq = ibkr_stock_payload("QQQ", con_id=320227571)
    spy_route = route_instrument(spy)
    qqq_route = route_instrument(qqq)

    class Store:
        def upsert_quote_snapshots(self, _snapshots):
            return None

    asyncio.run(
        persist_quote_snapshots_async(
            {spy_route.fingerprint: _current_last_quote(750.25)},
            instruments=[spy],
            store_factory=Store,
        )
    )
    asyncio.run(
        persist_quote_snapshots_async(
            {qqq_route.fingerprint: _current_last_quote(640.0)},
            instruments=[qqq],
            store_factory=Store,
        )
    )

    stats = quote_snapshot_write_stats()
    assert stats["evicted"] == 1
    assert stats["state_entries"] == 1

    asyncio.run(
        persist_quote_snapshots_async(
            {},
            instruments=[],
            store_factory=Store,
        )
    )

    stats = quote_snapshot_write_stats()
    assert stats["evicted"] == 2
    assert stats["state_entries"] == 0
