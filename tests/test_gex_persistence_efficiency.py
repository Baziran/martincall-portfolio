from __future__ import annotations

import gc
import os
import weakref
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aef_terminal.data.gex import history as gex_history
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore
from aef_terminal.storage.repos import gex as gex_repo


@pytest.fixture
def postgres_gex_reader(monkeypatch):
    """Exercise the real projection SQL against an isolated temporary relation."""
    import psycopg

    dsn = os.environ.get("AEF_DATABASE_URL")
    if not dsn:
        pytest.skip("AEF_DATABASE_URL is required for GEX reader SQL tests")
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            """
            CREATE TEMP TABLE gex_snapshots (
                captured_at timestamptz NOT NULL, source text NOT NULL,
                payload jsonb NOT NULL, instrument_id text NOT NULL,
                route_fingerprint text NOT NULL, provider_symbol text NOT NULL,
                PRIMARY KEY (instrument_id, route_fingerprint, captured_at, source)
            )
            """
        )

        @contextmanager
        def connect():
            yield connection

        reader = PostgresStore(
            None,
            schema_owner=False,
            canonical_writer_capability=CanonicalWriterCapability.READ_ONLY,
        )
        monkeypatch.setattr(reader, "_connect", connect)
        yield reader, connection


def _projection_payload() -> dict[str, Any]:
    series = {
        "expiry": "20260828",
        "trading_class": "SPY",
        "exchange": "SMART",
        "multiplier": 100,
    }
    return {
        "captured_at": "2026-08-28T14:00:00+00:00",
        "capture_revision": "2026-08-28T14:00:00+00:00",
        "source": "gex:ibkr",
        "capture_mode": "request",
        "instrument_id": "ibkr|contract|1",
        "route_fingerprint": "ibkr|contract|1",
        "provider_symbol": "SPY",
        "comparison_scope": {"series": [series]},
        "frame_complete": True,
        "decision_authoritative": True,
        "market_data_entitlement": "live",
        "open_interest_as_of": "previous_settlement",
        "spot": 650.0,
        "gamma_flip": None,
        "levels": [{"price": 650.0, "net_gex": 125.0}],
        "call_wall": 650.0,
        "put_wall": 645.0,
        "net_gex": 125.0,
        "global_gamma_regime": "positive",
        "option_activity": {"call_volume": 42.0},
        "option_activity_status": "ready",
        "raw": {
            "contracts": [
                {
                    **series,
                    "expiry_time_source": "ibkr_contract_details",
                    "expiry_at": "2026-08-28T20:00:00+00:00",
                }
            ],
            "provider_evidence": "unprojected" * 4_000,
        },
    }


def _insert_projection_payload(connection, payload: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    connection.execute(
        "INSERT INTO gex_snapshots VALUES (%s, %s, %s, %s, %s, %s)",
        (
            payload["captured_at"],
            payload["source"],
            Jsonb(payload),
            payload["instrument_id"],
            payload["route_fingerprint"],
            payload["provider_symbol"],
        ),
    )


def test_compact_gex_sql_preserves_projection_scope_limit_and_order(postgres_gex_reader) -> None:
    reader, connection = postgres_gex_reader
    payload = _projection_payload()
    earlier = {**payload, "captured_at": "2026-08-28T13:55:00+00:00"}
    live = {**payload, "source": "gex:ibkr-live", "capture_mode": "live"}
    other_route = {**payload, "route_fingerprint": "ibkr|contract|2"}
    other_instrument = {**payload, "instrument_id": "ibkr|contract|2"}
    for row in (earlier, payload, live, other_route, other_instrument):
        _insert_projection_payload(connection, row)
    start = datetime(2026, 8, 28, 13, 55, tzinfo=UTC)
    end = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    rows = reader.read_gex_snapshots(
        payload["instrument_id"],
        payload["route_fingerprint"],
        start,
        end,
        limit=2,
        sources=("gex:ibkr", "gex:ibkr-live"),
        include_raw=False,
    )
    expected = [
        {
            **{key: value for key, value in row.items() if key != "raw"},
            "option_universe_expires_at": "2026-08-28T20:00:00+00:00",
        }
        for row in (payload, live)
    ]
    assert [row["payload"] for row in rows] == expected
    assert [row["source"] for row in rows] == ["gex:ibkr", "gex:ibkr-live"]
    raw = reader.read_gex_snapshots(
        payload["instrument_id"],
        payload["route_fingerprint"],
        start,
        end,
        limit=1,
        source="gex:ibkr",
        include_raw=True,
    )
    assert raw[0]["payload"] == payload
    assert (
        reader.read_gex_snapshots(
            payload["instrument_id"],
            payload["route_fingerprint"],
            end + timedelta(seconds=1),
            end + timedelta(seconds=2),
            source="gex:ibkr",
            include_raw=False,
        )
        == []
    )


@pytest.mark.parametrize(
    "invalid_fact",
    [
        "missing_raw",
        "null_raw",
        "empty_contracts",
        "invalid_contract",
        "missing_expiry",
        "null_expiry",
        "wrong_source",
        "conflict",
        "missing_series",
        "empty_series",
    ],
)
def test_compact_gex_sql_never_recovers_unproven_expiry(
    postgres_gex_reader, invalid_fact: str
) -> None:
    reader, connection = postgres_gex_reader
    payload = _projection_payload()
    contract = payload["raw"]["contracts"][0]
    if invalid_fact == "missing_raw":
        del payload["raw"]
    elif invalid_fact == "null_raw":
        payload["raw"] = None
    elif invalid_fact == "empty_contracts":
        payload["raw"]["contracts"] = []
    elif invalid_fact == "invalid_contract":
        payload["raw"]["contracts"] = ["not a contract"]
    elif invalid_fact == "missing_expiry":
        del contract["expiry_at"]
    elif invalid_fact == "null_expiry":
        contract["expiry_at"] = None
    elif invalid_fact == "wrong_source":
        contract["expiry_time_source"] = "inferred"
    elif invalid_fact == "conflict":
        payload["raw"]["contracts"].append({**contract, "expiry_at": "2026-08-28T21:00:00+00:00"})
    elif invalid_fact == "missing_series":
        extra_series = deepcopy(payload["comparison_scope"]["series"][0])
        extra_series["trading_class"] = "OTHER"
        payload["comparison_scope"]["series"].append(extra_series)
    elif invalid_fact == "empty_series":
        payload["comparison_scope"]["series"] = []
    _insert_projection_payload(connection, payload)
    captured_at = datetime.fromisoformat(payload["captured_at"])
    rows = reader.read_gex_snapshots(
        payload["instrument_id"],
        payload["route_fingerprint"],
        captured_at,
        captured_at,
        source="gex:ibkr",
        include_raw=False,
    )
    assert rows[0]["payload"]["option_universe_expires_at"] is None


def test_completed_route_persistence_locks_are_released() -> None:
    instrument_id = "ibkr|future_root|LOCK-RETIREMENT-TEST"
    route_fingerprint = "ibkr|future|contract|999999991"
    key = (instrument_id, route_fingerprint)
    lock = gex_history._gex_persist_lock(instrument_id, route_fingerprint)
    reference = weakref.ref(lock)

    assert gex_history._GEX_PERSIST_LOCKS.get(key) is lock

    del lock
    gc.collect()

    assert reference() is None
    assert key not in gex_history._GEX_PERSIST_LOCKS


def test_storage_validates_new_snapshot_once_before_transient_cleanup(
    monkeypatch,
) -> None:
    captured_at = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
    instrument_id = "ibkr|future_root|ES"
    route_fingerprint = "ibkr|future|contract|700001"
    option_expiry = "2026-08-03T20:00:00+00:00"
    input_payload = {
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider_symbol": "ESU6",
        "captured_at": captured_at.isoformat(),
        "capture_mode": "request",
    }
    canonical_payload = {
        **input_payload,
        "source": "gex:ibkr",
        "option_universe_expires_at": option_expiry,
        "raw": {
            "meta": {
                "provider_symbol": "ESU6",
                "option_universe_expires_at": option_expiry,
                "option_expiry_facts": {"source": "provider"},
            },
            "strikes": [{"strike": 5000.0}],
            "contracts": [{"con_id": 800001}],
        },
    }
    validation_calls: list[tuple[object, str]] = []

    def validate(value: object, *, projection: str) -> dict[str, Any]:
        validation_calls.append((value, projection))
        return {
            **canonical_payload,
            "raw": {
                "meta": dict(canonical_payload["raw"]["meta"]),
                "strikes": list(canonical_payload["raw"]["strikes"]),
                "contracts": list(canonical_payload["raw"]["contracts"]),
            },
        }

    inserted_payloads: list[dict[str, Any]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement: str, params: tuple[Any, ...]) -> None:
            if "INSERT INTO gex_snapshots" in statement:
                inserted_payloads.append(dict(params[5].obj))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def transaction(self):
            return self

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(gex_repo, "require_gex_snapshot_payload", validate)
    repo = PostgresStore(
        None,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )
    repo._psycopg = lambda: None  # type: ignore[attr-defined,method-assign]
    repo._connect = lambda: Connection()  # type: ignore[attr-defined,method-assign]

    assert repo.upsert_gex_snapshot(
        instrument_id,
        route_fingerprint,
        "ESU6",
        captured_at,
        input_payload,
        source="gex:ibkr",
    )

    assert validation_calls == [(input_payload, "persisted")]
    assert len(inserted_payloads) == 1
    inserted = inserted_payloads[0]
    assert "option_universe_expires_at" not in inserted
    assert "option_universe_expires_at" not in inserted["raw"]["meta"]
    assert "option_expiry_facts" not in inserted["raw"]["meta"]


def test_market_snapshot_retention_drops_expired_chunks_and_bounds_rows() -> None:
    statements: list[str] = []

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement: object, _params: object = None) -> None:
            source = str(statement)
            statements.append(source)
            self.rowcount = (
                7
                if "DELETE FROM quote_snapshots" in source
                else 3
                if "DELETE FROM gex_snapshots" in source
                else 0
            )

        def fetchall(self) -> list[tuple[str]]:
            return [("_timescaledb_internal._hyper_4_12_chunk",)]

        def fetchone(self) -> tuple[Any, ...]:
            if "pg_extension" in statements[-1]:
                return ("public",)
            return (False, False)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def transaction(self):
            return self

        def cursor(self) -> Cursor:
            return Cursor()

    repo = PostgresStore(
        None,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )
    repo._psycopg = lambda: None  # type: ignore[attr-defined,method-assign]
    repo._connect = lambda: Connection()  # type: ignore[attr-defined,method-assign]
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

    result = repo.prune_market_snapshot_history(
        quote_before=now - timedelta(hours=48),
        gex_before=now - timedelta(hours=48),
        future_after=now + timedelta(seconds=5),
    )

    assert result == {
        "dropped_quote_chunks": 1,
        "deleted_quote_rows": 7,
        "deleted_gex_rows": 3,
        "has_more": 0,
    }
    source = "\n".join(statements)
    assert "drop_chunks" in source
    assert "DELETE FROM quote_snapshots" in source
    assert "DELETE FROM gex_snapshots" in source
