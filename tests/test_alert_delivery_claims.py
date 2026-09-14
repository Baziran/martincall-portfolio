from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui import server_alert_runtime
from aef_terminal.ui.runtime.quote_stream import QuoteRouteEntry, QuoteRouteSnapshot
from aef_terminal.ui.services import server_alert_delivery
from aef_terminal.ui.services.server_alert_delivery import (
    persist_price_alert_delivery,
    process_pending_price_telegram_deliveries,
    process_server_alert_delivery_tick,
)
from tests.provider_payloads import ibkr_stock_payload, quote_route_snapshot


def _price_pending_payload(instrument_id: str, route_fingerprint: str) -> dict:
    return {
        "alert_id": "price-1",
        "alert_type": "price",
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "symbol": "ES",
        "timeframe": "5m",
        "kind": "Price alert",
        "label": "",
        "level_source": {},
        "direction": "cross",
        "price": 100.0,
        "current_price": 101.0,
        "source": "MartinCall backend",
        "ts": "2026-07-22T10:00:00+00:00",
        "trigger_event_at": 1_000,
        "message": "fired",
    }


class _CasStore:
    def __init__(self, payload: dict) -> None:
        self.payload = dict(payload)

    def update_price_alert_payload_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        patch: dict,
        **guards,
    ) -> dict | None:
        if (
            alert_id != self.payload.get("id")
            or instrument_id != self.payload.get("instrument_id")
            or route_fingerprint != self.payload.get("route_fingerprint")
        ):
            return None
        if guards.get("check_generation") and int(self.payload.get("rearmedAt") or 0) != int(
            guards.get("expected_rearmed_at") or 0
        ):
            return None
        expected_pending = guards.get("expected_pending_payload")
        if (
            expected_pending is not None
            and self.payload.get("telegramPendingPayload") != expected_pending
        ):
            return None
        if any(
            self.payload.get(field) != value
            for field, value in guards.get("expected_fields", {}).items()
        ):
            return None
        self.payload.update(patch)
        return dict(self.payload)


def test_empty_delivery_outbox_returns_before_route_snapshot_materialization() -> None:
    class Store:
        def read_watchlist_snapshot(self):
            raise AssertionError("empty outbox must not materialize watchlist")

    store = Store()
    deps = SimpleNamespace(
        store_factory=lambda: store,
        quote_route_snapshot=lambda: (_ for _ in ()).throw(
            AssertionError("empty outbox must not materialize route snapshot")
        ),
        server_pending_price_alerts_snapshot=lambda _store: [],
    )

    asyncio.run(process_server_alert_delivery_tick(deps))


@pytest.mark.parametrize("send_ok", [True, False])
def test_runtime_delivery_tick_uses_current_dependencies_for_nonempty_outbox(
    monkeypatch, send_ok: bool
) -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    snapshot = quote_route_snapshot([instrument])
    pending = _price_pending_payload(route.instrument_id, route.fingerprint)
    store = _CasStore(
        {
            "id": "price-1",
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "rearmedAt": 100,
            **TELEGRAM_CANCELLED_DELIVERY_STATE,
            "telegramDeliveryStatus": "pending",
            "telegramPendingPayload": pending,
            "telegramPendingSince": 900,
            "telegramNextRetryAt": 1_000,
        }
    )
    sent: list[dict] = []
    owner_thread = threading.get_ident()
    snapshot_reads = 0

    def snapshot_reader():
        nonlocal snapshot_reads
        snapshot_reads += 1
        return snapshot

    def forbidden(*_args, **_kwargs):
        raise AssertionError("delivery must not materialize watchlist or evaluate market data")

    def sender(payload):
        assert threading.get_ident() != owner_thread
        assert store.payload["telegramDeliveryStatus"] == "delivering"
        assert store.payload["telegramClaimedAt"] == 1_000
        sent.append(payload)
        return {"ok": send_ok, "status": "sent" if send_ok else "send_failed"}

    monkeypatch.setattr(QuoteRouteSnapshot, "select_instruments", forbidden)
    monkeypatch.setattr(QuoteRouteEntry, "wire_instrument", forbidden)
    monkeypatch.setattr(server_alert_delivery, "time", SimpleNamespace(time=lambda: 1.0))
    monkeypatch.setattr(
        server_alert_runtime,
        "_DEPS",
        server_alert_runtime.ServerAlertRuntimeDeps(
            store_factory=lambda: store,
            quote_route_snapshot=snapshot_reader,
            set_server_alert_wanted=forbidden,
            quote_cache_for_instruments=forbidden,
            queue_paper_trade_telegram=forbidden,
            send_server_telegram_alert=sender,
            logger=logging.getLogger(__name__),
            server_alert_indicator_params=forbidden,
        ),
    )
    monkeypatch.setattr(
        server_alert_runtime.price_alert_runtime,
        "pending_delivery_snapshot",
        lambda: [dict(store.payload)] if store.payload["telegramPendingPayload"] else [],
    )

    async def run() -> None:
        await server_alert_runtime.server_alert_delivery_tick()
        await server_alert_runtime.server_alert_delivery_tick()

    asyncio.run(run())

    assert sent == [pending]
    assert snapshot_reads == (1 if send_ok else 2)
    assert store.payload["lastTelegramOk"] is send_ok
    assert store.payload["telegramClaimedAt"] == 0
    if send_ok:
        assert store.payload["telegramDeliveryStatus"] == "sent"
        assert store.payload["telegramPendingPayload"] is None
    else:
        assert store.payload["telegramDeliveryStatus"] == "pending"
        assert store.payload["telegramPendingPayload"] == pending
        assert store.payload["telegramPendingSince"] == 900
        assert store.payload["telegramNextRetryAt"] > 1_000
        assert store.payload["telegramRetryCount"] == 1


@pytest.mark.parametrize("route_state", ["missing", "changed"])
def test_delivery_does_not_claim_or_send_for_missing_or_changed_exact_route(route_state) -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    pending = _price_pending_payload(route.instrument_id, route.fingerprint)
    alert = {
        "id": "price-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "rearmedAt": 100,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
        "telegramDeliveryStatus": "pending",
        "telegramPendingPayload": pending,
        "telegramPendingSince": 900,
        "telegramNextRetryAt": 1_000,
    }
    if route_state == "changed":
        alert["route_fingerprint"] = pending["route_fingerprint"] = "previous-route"
    snapshot = quote_route_snapshot([instrument] if route_state == "changed" else [])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("delivery must not claim or send for another route")

    result = asyncio.run(
        process_pending_price_telegram_deliveries(
            SimpleNamespace(send_server_telegram_alert=forbidden),
            SimpleNamespace(update_price_alert_payload_exact=forbidden),
            [alert],
            snapshot,
            1_000,
        )
    )

    assert result == 0
    assert alert["telegramDeliveryStatus"] == "pending"


def test_slow_delivery_and_option_quote_lanes_do_not_block_alert_evaluation(monkeypatch) -> None:
    evaluation_finished = asyncio.Event()
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    option_quote_started = asyncio.Event()
    release_option_quote = asyncio.Event()

    async def evaluation_tick() -> None:
        evaluation_finished.set()

    async def delivery_tick() -> None:
        delivery_started.set()
        await release_delivery.wait()

    async def one_iteration_loop(**kwargs) -> None:
        await kwargs["run_iteration"]()

    async def option_quote_loop(**_kwargs) -> None:
        option_quote_started.set()
        await release_option_quote.wait()

    monkeypatch.setattr(
        server_alert_runtime,
        "server_alert_monitor_tick",
        evaluation_tick,
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "server_alert_delivery_loop",
        lambda **_kwargs: delivery_tick(),
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "server_price_alert_quote_loop",
        lambda **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "_deps",
        lambda: SimpleNamespace(store_factory=lambda: None),
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "run_server_alert_monitor_loop",
        one_iteration_loop,
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "run_paper_option_quote_refresh_runtime",
        option_quote_loop,
    )

    async def run() -> None:
        task = asyncio.create_task(
            server_alert_runtime.server_alert_monitor_loop(
                server_sleeping=lambda: False,
            )
        )
        await delivery_started.wait()
        await option_quote_started.wait()
        await asyncio.wait_for(evaluation_finished.wait(), timeout=0.1)
        assert task.done() is False
        release_delivery.set()
        release_option_quote.set()
        await task

    asyncio.run(run())


def test_price_alert_is_claimed_before_send_and_stale_claim_cannot_send_twice() -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    original = {
        "id": "price-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "enabled": True,
        "armed": True,
        "fired": True,
        "cooldownUntil": 0,
        "rearmedAt": 100,
        "lastFiredAt": 50,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    store = _CasStore(original)
    sent: list[dict] = []

    def sender(payload: dict) -> dict:
        assert store.payload["telegramDeliveryStatus"] == "delivering"
        assert store.payload["telegramClaimedAt"] == 1_000
        assert store.payload["telegramPendingPayload"] == payload
        sent.append(payload)
        return {"ok": True, "status": "sent"}

    deps = SimpleNamespace(send_server_telegram_alert=sender)
    pending = _price_pending_payload(route.instrument_id, route.fingerprint)
    patch = {"armed": True, "fired": True, "lastFiredAt": 1_000}

    async def run() -> None:
        assert await persist_price_alert_delivery(store, original, pending, patch, now_ms=1_000)
        assert not await persist_price_alert_delivery(store, original, pending, patch, now_ms=1_000)
        assert sent == []
        assert (
            await process_pending_price_telegram_deliveries(
                deps,
                store,
                [dict(store.payload)],
                quote_route_snapshot([instrument]),
                1_000,
            )
            == 1
        )

    asyncio.run(run())

    assert sent == [pending]
    assert store.payload["telegramPendingPayload"] is None
    assert store.payload["lastTelegramStatus"] == "sent"


def test_cancelled_delivery_owner_waits_for_send_and_finalizer() -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    original = {
        "id": "price-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 100,
        "lastFiredAt": 0,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    store = _CasStore(original)
    pending = _price_pending_payload(route.instrument_id, route.fingerprint)
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def sender(_payload: dict) -> dict:
        started.set()
        assert release.wait(timeout=1.0)
        completed.set()
        return {"ok": True, "status": "sent"}

    deps = SimpleNamespace(send_server_telegram_alert=sender)

    async def run() -> None:
        assert await persist_price_alert_delivery(
            store,
            original,
            pending,
            {"armed": False, "fired": True, "lastFiredAt": 1_000},
            now_ms=1_000,
        )
        task = asyncio.create_task(
            process_pending_price_telegram_deliveries(
                deps,
                store,
                [dict(store.payload)],
                quote_route_snapshot([instrument]),
                1_000,
            )
        )
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert completed.is_set()
    assert store.payload["telegramPendingPayload"] is None
    assert store.payload["lastTelegramStatus"] == "sent"


def test_late_price_delivery_finalizer_cannot_overwrite_renewed_retry_lease() -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    original = {
        "id": "price-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 100,
        "lastFiredAt": 0,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    store = _CasStore(original)

    def sender(_payload: dict) -> dict:
        store.payload["telegramNextRetryAt"] += 1
        return {"ok": True, "status": "sent"}

    deps = SimpleNamespace(send_server_telegram_alert=sender)
    pending = _price_pending_payload(route.instrument_id, route.fingerprint)

    async def run() -> None:
        assert await persist_price_alert_delivery(
            store,
            original,
            pending,
            {"armed": False, "fired": True, "lastFiredAt": 1_000},
            now_ms=1_000,
        )
        assert (
            await process_pending_price_telegram_deliveries(
                deps,
                store,
                [dict(store.payload)],
                quote_route_snapshot([instrument]),
                1_000,
            )
            == 1
        )

    asyncio.run(run())

    assert store.payload["telegramPendingPayload"] == pending
    assert store.payload["telegramDeliveryStatus"] == "delivering"
    assert store.payload["telegramClaimedAt"] == 1_000


def test_delivery_batch_bounds_failed_claim_attempts() -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    original = {
        "id": "price-1",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 100,
        "lastFiredAt": 0,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    persisted_store = _CasStore(original)
    pending = _price_pending_payload(route.instrument_id, route.fingerprint)

    class RejectingStore:
        def __init__(self) -> None:
            self.claims = 0

        def update_price_alert_payload_exact(self, *_args, **_kwargs):
            self.claims += 1
            return None

    rejecting_store = RejectingStore()

    def sender(_payload: dict) -> dict:
        raise AssertionError("a rejected claim must not send")

    deps = SimpleNamespace(send_server_telegram_alert=sender)

    async def run() -> None:
        assert await persist_price_alert_delivery(
            persisted_store,
            original,
            pending,
            {"armed": False, "fired": True, "lastFiredAt": 1_000},
            now_ms=1_000,
        )
        due = dict(persisted_store.payload)
        delivered = await process_pending_price_telegram_deliveries(
            deps,
            rejecting_store,
            [due, {**due, "id": "price-2"}],
            quote_route_snapshot([instrument]),
            1_000,
            limit=1,
        )
        assert delivered == 0

    asyncio.run(run())

    assert rejecting_store.claims == 1
