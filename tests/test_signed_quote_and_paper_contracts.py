from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data.ibkr.quotes import _quote_from_ticker
from aef_terminal.data.ibkr.utils import _clean_price
from aef_terminal.data.market_data import quote_snapshot_timestamp_is_admissible
from aef_terminal.data.providers import route_instrument
from aef_terminal.paper_contract import paper_instrument_contract
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore
from aef_terminal.ui.paper import positions as paper_positions
from aef_terminal.ui.paper.execution import (
    clear_execution_snapshots,
    finite_price,
    normalize_execution_snapshot,
    quote_execution_price_snapshot,
    record_quote_execution_snapshots,
)
from aef_terminal.ui.paper.orders import (
    mark_order_filled,
    normalize_paper_order,
    paper_manual_protective_levels,
)
from aef_terminal.ui.quote_helpers import (
    quote_envelope,
    quote_is_execution_eligible,
    quote_price,
    quote_snapshot_ts,
)
from aef_terminal.ui.services.quote_snapshots import (
    _quote_snapshot_signature,
    persist_quote_snapshots_async,
    quote_snapshot_write_stats,
    reset_quote_snapshot_state,
)
from tests.provider_payloads import ibkr_future_payload


def _paper_order(**overrides: Any) -> dict[str, Any]:
    return {
        "symbol": "CL",
        "instrument_id": "ibkr|contract|cl-signed",
        "route_fingerprint": "ibkr|route|cl-signed",
        "provider": "ibkr",
        "provider_contract_id": "cl-signed",
        "timeframe": "5m",
        "side": "long",
        "order_type": "limit",
        "qty": 1.0,
        "entry": 0.0,
        "stop_loss": -1.0,
        "target": 1.0,
        **overrides,
    }


def test_generic_quote_selection_and_snapshot_signature_preserve_zero_and_negative() -> None:
    assert quote_price({"price": 0.0, "last": 12.0}) == 0.0
    assert quote_price({"price": -37.63}) == -37.63
    assert quote_price({"price": "-37.63"}) is None
    assert quote_price({"price": float("nan")}) is None
    assert _quote_snapshot_signature({"price": -37.63, "last": 0.0})[3] == 0.0


def test_ibkr_quote_cleaner_is_finite_without_a_positive_price_guess() -> None:
    assert _clean_price(0.0) == 0.0
    assert _clean_price(-37.63) == -37.63
    assert _clean_price(float("nan")) is None
    assert _clean_price(float("inf")) is None
    assert _clean_price(True) is None

    ticker = SimpleNamespace(
        bid=-37.64,
        ask=-37.62,
        last=-37.63,
        close=0.0,
        bidSize=1,
        askSize=1,
        lastSize=1,
        time=datetime(2026, 7, 21, 12, 0),
        contract=SimpleNamespace(
            secType="FUT",
            symbol="CL",
            localSymbol="CLQ6",
            lastTradeDateOrContractMonth="202608",
        ),
        marketPrice=lambda: -37.63,
    )
    quote = _quote_from_ticker(ticker, "CL")
    assert quote["price"] == pytest.approx(-37.63)
    assert quote["price_source"] == "bid_ask_mid"
    assert quote["close"] == 0.0
    assert quote["ts"] is None
    assert quote["provider_ts"] is None


def test_quote_timestamps_never_guess_utc_or_freshness() -> None:
    received_at = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    assert quote_snapshot_ts({}) is None
    assert quote_snapshot_ts({"provider_ts": "2026-07-21T12:00:00"}) is None
    assert quote_snapshot_ts({"provider_ts": datetime(2026, 7, 21, 12, 0)}) is None

    envelope = quote_envelope(
        {"price": -37.63, "provider_ts": "2026-07-21T12:00:00"},
        received_at=received_at,
    )
    assert envelope["provider_ts"] is None
    assert envelope["received_at"] is None
    assert envelope["status"] == "unknown"
    assert envelope["is_stale"] is True
    with pytest.raises(ValueError, match="QUOTE_RECEIVED_AT_MUST_BE_AWARE"):
        quote_envelope({"price": -37.63}, received_at=datetime(2026, 7, 21, 12, 0))


def test_quote_snapshot_timestamp_admission_matches_the_durable_history_horizon() -> None:
    captured_at = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    assert quote_snapshot_timestamp_is_admissible(
        captured_at - timedelta(hours=48),
        captured_at=captured_at,
    )
    assert not quote_snapshot_timestamp_is_admissible(
        captured_at - timedelta(hours=48, microseconds=1),
        captured_at=captured_at,
    )
    assert not quote_snapshot_timestamp_is_admissible(
        captured_at + timedelta(seconds=5, microseconds=1),
        captured_at=captured_at,
    )


def test_quote_envelope_keeps_client_receive_time_distinct_from_provider_time() -> None:
    received_at = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    row = {
        "price": 500.0,
        "price_source": "bid_ask_mid",
        "bid": 499.5,
        "ask": 500.5,
        "received_at": received_at.isoformat(),
        "provider_ts": None,
        "time_basis": "client_receive",
        "is_delayed": False,
        "market_data_entitlement": "live",
    }

    envelope = quote_envelope(row, received_at=received_at)

    assert quote_snapshot_ts(row) == received_at
    assert envelope["provider_ts"] is None
    assert envelope["received_at"] == received_at.isoformat()
    assert envelope["time_basis"] == "client_receive"
    assert envelope["status"] == "live"
    unstamped_last = quote_envelope(
        {
            **row,
            "price_source": "last",
            "last": 500.0,
        },
        received_at=received_at,
    )
    assert unstamped_last["price_source"] == "last"
    assert unstamped_last["status"] == "unavailable"
    assert unstamped_last["last_status"] == "unavailable"
    assert unstamped_last["bid_ask_status"] == "live"


def test_quote_envelope_does_not_replace_selected_stale_last_with_midpoint() -> None:
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    row = {
        "price": 101.0,
        "price_source": "last",
        "last": 101.0,
        "bid": 99.0,
        "ask": 100.0,
        "provider_ts": (now - timedelta(seconds=20)).isoformat(),
        "last_provider_ts": (now - timedelta(seconds=20)).isoformat(),
        "received_at": (now - timedelta(seconds=1)).isoformat(),
        "bid_ask_received_at": (now - timedelta(seconds=1)).isoformat(),
        "time_basis": "provider_event",
        "ts": (now - timedelta(seconds=20)).isoformat(),
        "is_delayed": False,
        "market_data_entitlement": "live",
    }

    envelope = quote_envelope(row, received_at=now, stale_after_seconds=5.0)

    assert envelope["price"] == pytest.approx(101.0)
    assert envelope["price_source"] == "last"
    assert envelope["last"] == 101.0
    assert envelope["provider_ts"] == (now - timedelta(seconds=20)).isoformat()
    assert envelope["received_at"] == (now - timedelta(seconds=1)).isoformat()
    assert envelope["time_basis"] == "provider_event"
    assert envelope["ts"] == (now - timedelta(seconds=20)).isoformat()
    assert envelope["status"] == "stale"
    assert envelope["is_stale"] is True
    assert envelope["last_status"] == "stale"
    assert envelope["bid_ask_status"] == "live"
    assert quote_is_execution_eligible(envelope) is False

    fully_stale = quote_envelope(
        row,
        received_at=now + timedelta(seconds=10),
        stale_after_seconds=5.0,
    )
    assert fully_stale["price"] == 101.0
    assert fully_stale["price_source"] == "last"
    assert fully_stale["status"] == "stale"
    assert fully_stale["last_status"] == "stale"
    assert fully_stale["bid_ask_status"] == "stale"
    assert quote_is_execution_eligible(fully_stale) is False


def test_quote_execution_rejects_untyped_and_display_only_prices() -> None:
    observed_at = "2026-07-21T12:00:00+00:00"
    base = {
        "status": "live",
        "is_stale": False,
        "is_delayed": False,
        "price": 500.0,
        "last": 500.0,
        "bid": 499.5,
        "ask": 500.5,
    }

    assert (
        quote_is_execution_eligible(
            {
                **base,
                "price_source": "last",
                "provider_ts": observed_at,
                "time_basis": "provider_event",
            }
        )
        is True
    )
    assert (
        quote_is_execution_eligible(
            {
                **base,
                "price_source": "bid_ask_mid",
                "received_at": observed_at,
                "time_basis": "client_receive",
            }
        )
        is True
    )
    assert (
        quote_is_execution_eligible(
            {
                **base,
                "price_source": "last",
                "received_at": observed_at,
                "time_basis": "client_receive",
            }
        )
        is False
    )
    assert quote_is_execution_eligible(base) is False
    assert quote_is_execution_eligible({**base, "price_source": "ibkr_market_price"}) is False
    assert quote_is_execution_eligible({**base, "price_source": "previous_close"}) is False
    assert (
        quote_is_execution_eligible(
            {
                **base,
                "price": "500.0",
                "price_source": "last",
                "provider_ts": observed_at,
                "time_basis": "provider_event",
            }
        )
        is False
    )


def test_quote_snapshot_service_passes_signed_futures_price_to_exact_route_storage() -> None:
    reset_quote_snapshot_state()
    instrument = ibkr_future_payload("CL")
    route = route_instrument(instrument)
    observed_at = datetime.now(tz=UTC).isoformat()
    writes: list[tuple[Any, ...]] = []

    class Store:
        def upsert_quote_snapshots(self, snapshots: list[tuple[Any, ...]]) -> None:
            writes.extend(snapshots)

    asyncio.run(
        persist_quote_snapshots_async(
            {
                route.fingerprint: {
                    "price": -37.63,
                    "price_source": "bid_ask_mid",
                    "bid": -37.64,
                    "ask": -37.62,
                    "received_at": observed_at,
                    "time_basis": "client_receive",
                }
            },
            instruments=[instrument],
            store_factory=Store,
        )
    )

    assert len(writes) == 1
    assert writes[0][0] == route.instrument_id
    assert writes[0][1] == route.fingerprint
    assert writes[0][4]["price"] == -37.63
    assert writes[0][5] == "ibkr"

    reset_quote_snapshot_state()
    asyncio.run(
        persist_quote_snapshots_async(
            {
                route.fingerprint: {
                    "price": -37.64,
                    "price_source": "last",
                    "last": -37.64,
                    "provider_ts": "1970-01-01T00:00:00+00:00",
                    "time_basis": "provider_event",
                }
            },
            instruments=[instrument],
            store_factory=Store,
        )
    )
    assert len(writes) == 1
    assert quote_snapshot_write_stats()["rejected_timestamp"] == 1

    reset_quote_snapshot_state()
    asyncio.run(
        persist_quote_snapshots_async(
            {
                route.fingerprint: {
                    "price": -37.64,
                    "provider_ts": "2026-07-21T12:00:00",
                }
            },
            instruments=[instrument],
            store_factory=Store,
        )
    )
    assert len(writes) == 1


class _Cursor:
    def __init__(self) -> None:
        self.rows: list[tuple[Any, ...]] = []
        self.result_rows: list[tuple[Any, ...]] = []
        self.query = ""
        self.params: list[Any] = []
        self.execute_calls = 0
        self.executemany_calls = 0

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def executemany(self, _query: str, rows: list[tuple[Any, ...]]) -> None:
        self.executemany_calls += 1
        self.rows.extend(rows)

    def execute(self, query: str, params: list[Any]) -> None:
        self.execute_calls += 1
        self.query = query
        self.params = list(params)
        if "INSERT INTO quote_snapshots" not in query or "jsonb_to_recordset" not in query:
            return
        payload = getattr(params[0], "obj", params[0])
        latest_by_key = {
            (
                row["provider"],
                row["instrument_id"],
                row["route_fingerprint"],
                row["ts"],
            ): row
            for row in payload
        }
        self.rows.extend(
            (
                row["provider"],
                row["instrument_id"],
                row["route_fingerprint"],
                row["symbol"],
                datetime.fromisoformat(row["ts"]),
                row["price"],
                row["bid"],
                row["ask"],
                row["last"],
                row["close"],
                row["payload"],
            )
            for row in sorted(
                latest_by_key.values(),
                key=lambda item: item["input_ordinal"],
            )
        )

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.result_rows)


class _Context:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, *_args: Any) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def transaction(self) -> _Context:
        return _Context()

    def cursor(self) -> _Cursor:
        return self._cursor


class _QuoteSnapshotRepo(PostgresStore):
    def __init__(self) -> None:
        super().__init__(
            None,
            canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
        )
        self.cursor = _Cursor()
        self.connection = _Connection(self.cursor)

    @staticmethod
    def _psycopg() -> None:
        return None

    def _connect(self) -> _Context:
        return _Context(self.connection)


def test_quote_snapshot_repo_requires_aware_time_and_finite_payload_numbers() -> None:
    repo = _QuoteSnapshotRepo()
    base = (
        "ibkr|contract|cl-signed",
        "ibkr|route|cl-signed",
        "CL",
    )

    with pytest.raises(ValueError, match="QUOTE_SNAPSHOT_TIMESTAMP_MUST_BE_AWARE"):
        repo.upsert_quote_snapshots([(*base, datetime(2026, 7, 21, 12, 0), {"price": 0.0}, "ibkr")])
    with pytest.raises(ValueError, match="non-finite"):
        repo.upsert_quote_snapshots(
            [
                (
                    *base,
                    datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
                    {"price": float("nan")},
                    "ibkr",
                )
            ]
        )
    with pytest.raises(ValueError, match="QUOTE_SNAPSHOT_PRICE_MUST_BE_FINITE"):
        repo.upsert_quote_snapshots(
            [
                (
                    *base,
                    datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
                    {"price": "not-a-number"},
                    "ibkr",
                )
            ]
        )
    with pytest.raises(ValueError, match="QUOTE_SNAPSHOT_PRICE_MUST_BE_FINITE"):
        repo.upsert_quote_snapshots(
            [
                (
                    *base,
                    datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
                    {"price": "-37.63"},
                    "ibkr",
                )
            ]
        )

    source_ts = datetime(
        2026,
        7,
        21,
        15,
        0,
        tzinfo=timezone(timedelta(hours=3)),
    )
    repo.upsert_quote_snapshots(
        [
            (
                *base,
                source_ts,
                {
                    "price": 0.0,
                    "bid": -0.25,
                    "ask": 0.25,
                    "last": None,
                },
                "ibkr",
            )
        ]
    )
    stored = repo.cursor.rows[-1]
    assert repo.cursor.execute_calls == 1
    assert repo.cursor.executemany_calls == 0
    assert stored[4] == datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    assert stored[5:10] == (0.0, -0.25, 0.25, None, None)


def test_quote_snapshot_set_batch_uses_one_statement_and_last_duplicate_wins() -> None:
    repo = _QuoteSnapshotRepo()
    base = (
        "ibkr|contract|cl-batch",
        "ibkr|route|cl-batch",
        "CL",
        datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    )

    repo.upsert_quote_snapshots(
        [
            (*base, {"price": -1.0}, "ibkr"),
            (*base, {"price": -2.0}, "ibkr"),
            (
                base[0],
                base[1],
                base[2],
                datetime(2026, 7, 21, 12, 0, 1, tzinfo=UTC),
                {"price": -3.0},
                "ibkr",
            ),
        ]
    )

    assert repo.cursor.execute_calls == 1
    assert repo.cursor.executemany_calls == 0
    assert len(repo.cursor.rows) == 2
    assert [row[5] for row in repo.cursor.rows] == [-2.0, -3.0]


def test_quote_snapshot_bucket_batch_is_exact_route_and_keeps_latest_bucket_prices() -> None:
    repo = _QuoteSnapshotRepo()
    instrument_id = "ibkr|contract|cl-signed"
    route_fingerprint = "ibkr|route|cl-signed"
    start = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    end = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    repo.cursor.result_rows = [
        (instrument_id, route_fingerprint, datetime(2026, 7, 21, 11, 55, tzinfo=UTC), -0.25),
        (instrument_id, route_fingerprint, datetime(2026, 7, 21, 12, 0, tzinfo=UTC), 0.0),
    ]

    points = repo.read_quote_snapshot_buckets_batch(
        [("ibkr", instrument_id, route_fingerprint)],
        start,
        end,
        bucket_minutes=5,
        max_points=36,
    )

    assert points[(instrument_id, route_fingerprint)] == [
        {"ts": "2026-07-21T11:55:00+00:00", "price": -0.25},
        {"ts": "2026-07-21T12:00:00+00:00", "price": 0.0},
    ]
    assert "date_bin" in repo.cursor.query
    assert "requested.provider = quote_snapshots.provider" in repo.cursor.query
    assert "requested.instrument_id = quote_snapshots.instrument_id" in repo.cursor.query
    assert "requested.route_fingerprint = quote_snapshots.route_fingerprint" in repo.cursor.query
    assert repo.cursor.params == [
        "ibkr",
        instrument_id,
        route_fingerprint,
        timedelta(minutes=5),
        start,
        end,
        36,
    ]


def test_paper_execution_snapshots_preserve_zero_and_negative_prices() -> None:
    clear_execution_snapshots()
    route_fingerprint = "ibkr|route|cl-signed"
    record_quote_execution_snapshots(
        [
            {
                "instrument_id": "ibkr|contract|cl-signed",
                "route_fingerprint": route_fingerprint,
                "provider": "ibkr",
                "live_quote": True,
                "price": 0.0,
                "bid": -0.25,
                "ask": 0.25,
                "price_source": "bid_ask_mid",
                "bid_ask_status": "live",
                "bid_ask_received_at": "2026-07-21T12:00:00+00:00",
                "last_status": "unavailable",
                "quote_time_basis": "client_receive",
                "quote_received_at": "2026-07-21T12:00:00+00:00",
                "quote_ts": "2026-07-21T12:00:00+00:00",
            }
        ]
    )
    snapshot = quote_execution_price_snapshot(
        "ibkr|contract|cl-signed",
        route_fingerprint,
    )
    assert snapshot is not None
    assert snapshot["price"] == 0.0
    assert snapshot["low"] == 0.0
    assert snapshot["high"] == 0.0
    assert snapshot["reference_kind"] == "bid_ask_mid"
    assert normalize_execution_snapshot({"price": -37.63, "low": -38.0, "high": 0.0}) == {
        "price": -37.63,
        "low": -38.0,
        "high": 0.0,
        "range_kind": "price",
        "ts": None,
        "source": "screen",
        "bar_slot": None,
    }
    assert finite_price(0.0) == 0.0
    assert finite_price(-37.63) == -37.63
    assert finite_price(float("nan")) is None
    assert finite_price(True) is None
    record_quote_execution_snapshots(
        [
            {
                "instrument_id": "ibkr|contract|naive-time",
                "route_fingerprint": "ibkr|route|naive-time",
                "provider": "ibkr",
                "live_quote": True,
                "price": -1.0,
                "price_source": "last",
                "quote_time_basis": "provider_event",
                "quote_provider_ts": "2026-07-21T12:00:00",
                "quote_ts": "2026-07-21T12:00:00",
            }
        ]
    )
    assert (
        quote_execution_price_snapshot(
            "ibkr|contract|naive-time",
            "ibkr|route|naive-time",
        )
        is None
    )
    clear_execution_snapshots()


def test_paper_order_contract_allows_signed_levels_but_requires_positive_qty() -> None:
    long_order = normalize_paper_order(_paper_order())
    assert long_order["entry"] == 0.0
    assert long_order["stop_loss"] == -1.0
    assert long_order["target"] == 1.0
    assert long_order["payload"]["protection_basis"] == "absolute_structure"

    short_order = normalize_paper_order(
        _paper_order(side="short", entry=-5.0, stop_loss=-4.0, target=-6.0)
    )
    assert short_order["entry"] == -5.0
    assert short_order["stop_loss"] == -4.0
    assert short_order["target"] == -6.0

    assert paper_manual_protective_levels(
        {
            "entry": 0.0,
            "side": "long",
            "stop_points": 1.0,
            "target_points": 2.0,
        }
    ) == {"stop": -1.0, "target": 2.0}
    assert mark_order_filled(long_order, fill_price=0.0)["fill_price"] == 0.0

    with pytest.raises(ValueError, match="qty must be a positive finite number"):
        normalize_paper_order(_paper_order(qty=0.0))
    with pytest.raises(ValueError, match="qty must be a positive finite number"):
        normalize_paper_order(_paper_order(qty=-1.0))
    with pytest.raises(ValueError, match="must be finite"):
        normalize_paper_order(_paper_order(target=float("inf")))
    with pytest.raises(ValueError, match="must be finite"):
        normalize_paper_order(_paper_order(stop_loss=None, use_stop_loss=True))
    with pytest.raises(ValueError, match="fill_price must be a finite number"):
        mark_order_filled(long_order, fill_price=float("nan"))
    for field in ("entry", "stop_loss", "target"):
        with pytest.raises(ValueError, match="must be finite numbers"):
            normalize_paper_order(_paper_order(**{field: True}))
        with pytest.raises(ValueError, match="must be finite numbers"):
            normalize_paper_order(_paper_order(**{field: "1.0"}))
    with pytest.raises(ValueError, match="qty must be a positive finite number"):
        normalize_paper_order(_paper_order(qty=True))
    with pytest.raises(ValueError, match="qty must be a positive finite number"):
        normalize_paper_order(_paper_order(qty="1.0"))
    with pytest.raises(ValueError, match="fill_price must be a finite number"):
        mark_order_filled(long_order, fill_price=True)
    with pytest.raises(ValueError, match="PAPER_ORDER_ID"):
        normalize_paper_order(_paper_order(id=123))
    for field in ("use_stop_loss", "use_target"):
        with pytest.raises(ValueError, match=f"{field} must be a boolean"):
            normalize_paper_order(_paper_order(**{field: "false"}))
        with pytest.raises(ValueError, match=f"{field} must be a boolean"):
            paper_manual_protective_levels(
                {
                    "entry": 0.0,
                    "side": "long",
                    "stop_points": 1.0,
                    "target_points": 2.0,
                    field: "false",
                }
            )
    for field in ("use_stop_loss", "use_target"):
        for invalid_value in ("true", 1):
            with pytest.raises(ValueError, match=rf"{field} must be a boolean"):
                normalize_paper_order(_paper_order(**{field: invalid_value}))
            with pytest.raises(ValueError, match=rf"{field} must be a boolean"):
                paper_manual_protective_levels(
                    {
                        "entry": 0.0,
                        "side": "long",
                        "stop_points": 1.0,
                        "target_points": 2.0,
                        field: invalid_value,
                    }
                )


def test_paper_position_mark_and_signal_qty_do_not_apply_positive_price_or_qty_clamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        paper_positions,
        "paper_trade_mark_price",
        lambda *_args: -1.5,
    )
    marked = paper_positions.paper_open_trade_mark_pnl(
        {
            "status": "open",
            "entry": 0.0,
            "qty": 2.0,
            "side": "long",
            "instrument_id": "ibkr|contract|cl-signed",
            "route_fingerprint": "ibkr|route|cl-signed",
            "provider": "ibkr",
            "provider_contract_id": "cl-signed",
            "paper_contract": paper_instrument_contract("ibkr", "cl-signed").to_payload(),
            "timeframe": "5m",
        }
    )
    assert marked["mark_price"] == -1.5
    assert marked["unrealized_points"] == -3.0
    assert marked["unrealized_pnl"] == -3.0
    assert marked["pnl_unit"] == "points"

    monkeypatch.setattr(
        paper_positions,
        "paper_trade_from_signal",
        lambda *_args, **_kwargs: {
            "id": "signed-signal",
            "source": "trade_setup",
            "side": "long",
            "entry": 0.0,
            "stop": -1.0,
            "target": 2.0,
        },
    )
    kwargs = {
        "instrument_id": "ibkr|contract|cl-signed",
        "route_fingerprint": "ibkr|route|cl-signed",
        "provider": "ibkr",
        "provider_contract_id": "cl-signed",
        "config": {"min_rr": 1.25, "edge_gate": True},
    }
    assert (
        paper_positions.paper_order_payload_from_signal(
            "CL",
            "5m",
            {"source": "trade_setup"},
            qty=0.01,
            **kwargs,
        )
        is None
    )
    assert (
        paper_positions.paper_order_payload_from_signal(
            "CL",
            "5m",
            {"source": "trade_setup", "protection_basis": "absolute_structure"},
            qty=0.0,
            **kwargs,
        )
        is None
    )
    assert (
        paper_positions.paper_order_payload_from_signal(
            "CL",
            "5m",
            {"source": "trade_setup", "protection_basis": "absolute_structure"},
            qty=-1.0,
            **kwargs,
        )
        is None
    )
    assert (
        paper_positions.paper_order_payload_from_signal(
            "CL",
            "5m",
            {"source": "trade_setup", "protection_basis": "absolute_structure"},
            qty=True,
            **kwargs,
        )
        is None
    )
    payload = paper_positions.paper_order_payload_from_signal(
        "CL",
        "5m",
        {"source": "trade_setup", "protection_basis": "absolute_structure"},
        qty=0.01,
        **kwargs,
    )
    assert payload is not None
    assert payload["qty"] == 0.01
    rolled_payload = paper_positions.paper_order_payload_from_signal(
        "CL",
        "5m",
        {"source": "trade_setup", "protection_basis": "absolute_structure"},
        qty=0.01,
        **{
            **kwargs,
            "route_fingerprint": "ibkr|route|cl-signed-next",
        },
    )
    assert rolled_payload is not None
    assert rolled_payload["id"] != payload["id"]
