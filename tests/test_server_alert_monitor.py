from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aef_terminal.ui.services.server_alert_monitor import (
    AlertCrossingBaseline,
    _cached_server_alert_price_snapshot,
    _prune_crossing_baselines,
    _qualified_paper_orders,
    process_server_alert_quote_event,
)
from aef_terminal.alerts.runtime_registry import AlertQuoteEvent


def test_alert_price_snapshot_is_reused_once_per_exact_scope() -> None:
    calls: list[dict] = []

    def snapshot(quote):
        calls.append(quote)
        return {"price": 100.0}

    deps = SimpleNamespace(server_alert_price_snapshot=snapshot)
    cache = {}
    quote = {"price": 100.0}
    scope_key = ("instrument-a", "route-a", "5m")

    async def run() -> None:
        first = await _cached_server_alert_price_snapshot(
            deps,
            quote,
            cache,
            scope_key=scope_key,
        )
        second = await _cached_server_alert_price_snapshot(
            deps,
            quote,
            cache,
            scope_key=scope_key,
        )
        assert first is second

    asyncio.run(run())

    assert calls == [quote]


def test_pending_paper_order_qualification_rejects_invalid_runtime_identity() -> None:
    with pytest.raises(ValueError, match="PAPER_ORDER_ID"):
        _qualified_paper_orders(
            [
                {
                    "id": "",
                    "instrument_id": "ibkr|contract|123",
                    "route_fingerprint": "route-1",
                }
            ],
            {},
        )


def test_quote_event_without_armed_route_does_not_select_instruments_or_open_storage() -> None:
    def unexpected():
        raise AssertionError("an unobserved route has no evaluation work")

    deps = SimpleNamespace(
        store_factory=unexpected,
        quote_route_snapshot=unexpected,
        server_active_price_alerts_for_route=lambda *_args: [],
    )
    asyncio.run(
        process_server_alert_quote_event(deps, AlertQuoteEvent(1, "instrument-a", "route-a", {}))
    )


def test_crossing_baseline_pruning_removes_inactive_and_superseded_generations() -> None:
    current_scope = ("alert-current", "instrument-a", "route-a")
    removed_scope = ("alert-removed", "instrument-b", "route-b")
    baselines = {
        current_scope: AlertCrossingBaseline(timeframe="5m", rearmed_at=1, price=99.0),
        removed_scope: AlertCrossingBaseline(timeframe="5m", rearmed_at=1, price=50.0),
    }
    deps = SimpleNamespace(crossing_baselines=baselines)
    active = {
        "id": "alert-current",
        "instrument_id": "instrument-a",
        "route_fingerprint": "route-a",
        "timeframe": "5m",
        "kind": "price",
        "rearmedAt": 2,
    }

    _prune_crossing_baselines(deps, [active])

    assert baselines == {}
