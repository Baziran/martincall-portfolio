from __future__ import annotations

from datetime import UTC, datetime

import pytest

import aef_terminal.ui.paper.execution as execution_module
from aef_terminal.ui.paper.execution import (
    chart_execution_price_snapshot,
    clear_execution_snapshots,
    quote_execution_price_snapshot,
    record_chart_execution_snapshot,
    record_quote_execution_snapshots,
    screen_execution_price_snapshot,
)
from aef_terminal.ui.paper.positions import _open_position_for_route


@pytest.fixture(autouse=True)
def _clear_execution_cache() -> None:
    clear_execution_snapshots()
    yield
    clear_execution_snapshots()


def _chart_bar(
    instrument_id: str,
    ts: str,
    close: float,
    **overrides,
) -> dict:
    return {
        "instrument_id": instrument_id,
        "ts": ts,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "closed": False,
        "state": "forming",
        **overrides,
    }


def _canonical_chart_bar(
    instrument_id: str,
    ts: str,
    close: float,
    **overrides,
) -> dict:
    return _chart_bar(
        instrument_id,
        ts,
        close,
        closed=True,
        state="confirmed",
        authoritative=True,
        canonical_revision=1,
        **overrides,
    )


def test_chart_execution_cache_is_scoped_by_instrument_route_and_timeframe() -> None:
    route_fingerprint = "shared-route-token"
    record_chart_execution_snapshot(
        "instrument-a",
        "5m",
        [_canonical_chart_bar("instrument-a", "2026-07-19T10:00:00Z", 101.0)],
        "ibkr:canonical-commit",
        route_fingerprint,
    )
    record_chart_execution_snapshot(
        "instrument-b",
        "5m",
        [_canonical_chart_bar("instrument-b", "2026-07-19T10:00:00Z", 202.0)],
        "ibkr:canonical-commit",
        route_fingerprint,
    )
    record_chart_execution_snapshot(
        "instrument-a",
        "15m",
        [_canonical_chart_bar("instrument-a", "2026-07-19T10:00:00Z", 303.0)],
        "ibkr:canonical-commit",
        route_fingerprint,
    )

    assert chart_execution_price_snapshot("instrument-a", "5m", route_fingerprint)["price"] == 101.0
    assert chart_execution_price_snapshot("instrument-b", "5m", route_fingerprint)["price"] == 202.0
    assert (
        chart_execution_price_snapshot("instrument-a", "15m", route_fingerprint)["price"] == 303.0
    )
    assert chart_execution_price_snapshot("missing", "5m", route_fingerprint) is None


def test_execution_snapshot_ttl_uses_monotonic_time_and_rejects_clock_regression(
    monkeypatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(execution_module.time, "monotonic", lambda: clock[0])
    record_chart_execution_snapshot(
        "instrument-a",
        "5m",
        [_canonical_chart_bar("instrument-a", "2026-07-19T10:00:00Z", 101.0)],
        "ibkr:canonical-commit",
        "route-a",
    )

    clock[0] = 129.0
    assert chart_execution_price_snapshot("instrument-a", "5m", "route-a") is not None
    clock[0] = 99.0
    assert chart_execution_price_snapshot("instrument-a", "5m", "route-a") is None
    clock[0] = 131.0
    assert chart_execution_price_snapshot("instrument-a", "5m", "route-a") is None


def test_chart_execution_rejects_placeholders_non_authoritative_and_wrong_identity() -> None:
    instrument_id = "instrument-a"
    route_fingerprint = "route-a"
    rejected = [
        _chart_bar(
            instrument_id,
            "2026-07-19T10:00:00Z",
            99.0,
            source="gap-placeholder",
            preview_kind="gap_placeholder",
            missing=True,
            data_gap=True,
            authoritative=False,
        ),
        _chart_bar(
            instrument_id,
            "2026-07-19T10:00:00Z",
            98.0,
            source="trade-preview",
            authoritative=False,
        ),
        _chart_bar(
            "instrument-b",
            "2026-07-19T10:00:00Z",
            97.0,
            authoritative=True,
        ),
        _chart_bar(
            instrument_id,
            "2026-07-19T10:00:00Z",
            96.0,
            authoritative=True,
            canonical_revision=1,
        ),
    ]

    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        rejected,
        "db-checkpoint",
        route_fingerprint,
    )

    assert chart_execution_price_snapshot(instrument_id, "5m", route_fingerprint) is None

    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [_canonical_chart_bar(instrument_id, "2026-07-19T10:00:00Z", 100.0)],
        "ibkr:canonical-commit",
        route_fingerprint,
    )
    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        rejected,
        "canonical-commit",
        route_fingerprint,
    )

    assert chart_execution_price_snapshot(instrument_id, "5m", route_fingerprint)["price"] == 100.0


def test_chart_execution_updates_are_timestamp_monotonic_with_same_slot_source_priority() -> None:
    instrument_id = "instrument-a"
    route_fingerprint = "route-a"
    slot_ts = "2026-07-19T10:00:00Z"

    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [_canonical_chart_bar(instrument_id, slot_ts, 100.0)],
        "db-checkpoint",
        route_fingerprint,
    )
    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [_canonical_chart_bar(instrument_id, slot_ts, 101.0)],
        "ibkr:canonical-commit",
        route_fingerprint,
    )
    assert chart_execution_price_snapshot(instrument_id, "5m", route_fingerprint)["price"] == 101.0

    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [_canonical_chart_bar(instrument_id, slot_ts, 99.0)],
        "db-checkpoint",
        route_fingerprint,
    )
    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [_canonical_chart_bar(instrument_id, "2026-07-19T09:55:00Z", 200.0)],
        "canonical-commit",
        route_fingerprint,
    )
    snapshot = chart_execution_price_snapshot(instrument_id, "5m", route_fingerprint)
    assert snapshot["price"] == 101.0
    assert snapshot["source"] == "ibkr:canonical-commit"

    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [_canonical_chart_bar(instrument_id, "2026-07-19T10:05:00Z", 102.0)],
        "db-checkpoint",
        route_fingerprint,
    )
    snapshot = chart_execution_price_snapshot(instrument_id, "5m", route_fingerprint)
    assert snapshot["price"] == 102.0
    assert snapshot["ts"] == datetime(2026, 7, 19, 10, 5, tzinfo=UTC)


def test_quote_execution_cache_also_requires_instrument_identity() -> None:
    route_fingerprint = "shared-route-token"
    record_quote_execution_snapshots(
        [
            {
                "instrument_id": "instrument-a",
                "route_fingerprint": route_fingerprint,
                "provider": "ibkr",
                "live_quote": True,
                "price": 101.0,
                "price_source": "last",
                "quote_time_basis": "provider_event",
                "quote_provider_ts": "2026-07-19T10:00:00Z",
                "quote_ts": "2026-07-19T10:00:00Z",
            },
            {
                "instrument_id": "instrument-b",
                "route_fingerprint": route_fingerprint,
                "provider": "ibkr",
                "live_quote": True,
                "price": 202.0,
                "price_source": "last",
                "quote_time_basis": "provider_event",
                "quote_provider_ts": "2026-07-19T10:00:00Z",
                "quote_ts": "2026-07-19T10:00:00Z",
            },
        ]
    )

    assert quote_execution_price_snapshot("instrument-a", route_fingerprint)["price"] == 101.0
    assert quote_execution_price_snapshot("instrument-b", route_fingerprint)["price"] == 202.0
    assert quote_execution_price_snapshot("missing", route_fingerprint) is None


def test_screen_execution_prefers_fresh_live_quote_over_chart_close() -> None:
    instrument_id = "instrument-a"
    route_fingerprint = "route-a"
    record_chart_execution_snapshot(
        instrument_id,
        "5m",
        [
            _canonical_chart_bar(
                instrument_id,
                "2026-07-19T10:00:00Z",
                100.0,
            )
        ],
        "ibkr:canonical-commit",
        route_fingerprint,
    )
    record_quote_execution_snapshots(
        [
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "provider": "ibkr",
                "live_quote": True,
                "price": 101.5,
                "price_source": "last",
                "quote_time_basis": "provider_event",
                "quote_provider_ts": "2026-07-19T10:00:01Z",
                "quote_ts": "2026-07-19T10:00:01Z",
            }
        ]
    )

    snapshot = screen_execution_price_snapshot(
        instrument_id,
        "5m",
        route_fingerprint,
    )

    assert snapshot is not None
    assert snapshot["price"] == 101.5
    assert snapshot["source"] == "ibkr:quote-live"


def test_quote_execution_cache_uses_selected_live_reference_without_bbo_side_gate() -> None:
    instrument_id = "instrument-stale-bbo"
    route_fingerprint = "route-stale-bbo"
    record_quote_execution_snapshots(
        [
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "provider": "ibkr",
                "live_quote": True,
                "price": 100.0,
                "price_source": "last",
                "quote_time_basis": "provider_event",
                "quote_provider_ts": "2026-07-19T10:05:00Z",
                "quote_ts": "2026-07-19T10:05:00Z",
                "bid": 90.0,
                "ask": 110.0,
                "bid_ask_status": "stale",
                "bid_ask_received_at": "2026-07-19T10:00:00Z",
            }
        ]
    )

    snapshot = quote_execution_price_snapshot(instrument_id, route_fingerprint)

    assert snapshot is not None
    assert snapshot["price"] == 100.0
    assert snapshot["low"] == 100.0
    assert snapshot["high"] == 100.0
    assert snapshot["reference_kind"] == "last"
    assert "bid" not in snapshot
    assert "ask" not in snapshot


def test_open_paper_position_selection_requires_exact_instrument_route_pair() -> None:
    class Store:
        @staticmethod
        def read_paper_positions(
            *,
            limit: int,
            status: str,
            instrument_id: str,
            route_fingerprint: str,
            timeframe: str,
        ) -> list[dict]:
            assert limit == 1
            assert status == "open"
            assert instrument_id == "instrument-a"
            assert route_fingerprint == "shared-route-token"
            assert timeframe == "5m"
            return [
                {
                    "id": "exact-pair",
                    "timeframe": "5m",
                    "status": "open",
                    "qty": 1.0,
                    "payload": {
                        "instrument_id": "instrument-a",
                        "route_fingerprint": "shared-route-token",
                    },
                },
            ]

    position = _open_position_for_route(
        Store(),
        "instrument-a",
        "shared-route-token",
        "5m",
    )

    assert position is not None
    assert position["id"] == "exact-pair"
