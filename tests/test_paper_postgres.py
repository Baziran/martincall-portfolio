from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aef_terminal.paper_contract import PAPER_EXECUTION_CONFIG_KEY, PaperContractIdentity
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore


_INSTRUMENT_ID = "ibkr|contract|123"
_ROUTE_FINGERPRINT = "route-paper-postgres"
_PAPER_CONTRACT = PaperContractIdentity(
    scope_kind="instrument",
    provider="ibkr",
    provider_contract_id="123",
)


def _postgres_schema_dsn(schema: str) -> str:
    dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL paper storage tests")
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options=-csearch_path%3D{schema}"


def _filled_order(
    order_id: str,
    *,
    timestamp: datetime,
    side: str,
    qty: float,
    entry: float,
    fill_price: float,
    role: str = "entry",
    position_id: str | None = None,
    reduce_only: bool = False,
    stop_loss: float | None = None,
    target: float | None = None,
) -> dict[str, object]:
    return {
        "id": order_id,
        "symbol": "CL",
        "instrument_id": _INSTRUMENT_ID,
        "route_fingerprint": _ROUTE_FINGERPRINT,
        "provider": "ibkr",
        "provider_contract_id": "123",
        "paper_contract": _PAPER_CONTRACT.to_payload(),
        "timeframe": "5m",
        "side": side,
        "order_type": "market",
        "status": "filled",
        "qty": qty,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "use_stop_loss": stop_loss is not None,
        "use_target": target is not None,
        "fill_price": fill_price,
        "filled_at": timestamp.isoformat(),
        "created_at": timestamp.isoformat(),
        "updated_at": timestamp.isoformat(),
        "role": role,
        "position_id": position_id,
        "reduce_only": reduce_only,
        "payload": {
            "protection_basis": "absolute_structure",
            PAPER_EXECUTION_CONFIG_KEY: {"min_rr": 1.25, "edge_gate": True},
        },
    }


def _manual_close_order(
    order_id: str,
    *,
    timestamp: datetime,
    position_id: str,
    fill_price: float,
) -> dict[str, object]:
    order = _filled_order(
        order_id,
        timestamp=timestamp,
        side="short",
        qty=1.0,
        entry=fill_price,
        fill_price=fill_price,
        role="close",
        position_id=position_id,
        reduce_only=True,
    )
    return order


def test_postgres_paper_fill_replay_partial_reduce_and_close() -> None:
    import psycopg

    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL paper storage tests")
    schema = f"test_paper_fill_{uuid4().hex}"
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = PostgresStore(
        _postgres_schema_dsn(schema),
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )
    opened_at = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    entry = _filled_order(
        "entry-1",
        timestamp=opened_at,
        side="long",
        qty=2.0,
        entry=100.0,
        fill_price=100.0,
        stop_loss=95.0,
        target=110.0,
    )
    try:
        store.initialize()

        opened = store.apply_filled_paper_order(entry)
        position_id = str(opened["position"]["id"])
        assert opened["outcome"] == "filled"
        assert opened["opened_new_position"] is True
        assert opened["position"]["qty"] == 2.0
        assert {order["role"] for order in opened["created_orders"]} == {"stop", "take"}

        replay = store.apply_filled_paper_order(entry)
        assert replay["outcome"] == "no_op"
        assert replay["fill"]["id"] == opened["fill"]["id"]

        reduced = store.apply_filled_paper_order(
            _filled_order(
                "take-partial-1",
                timestamp=opened_at + timedelta(minutes=1),
                side="short",
                qty=1.0,
                entry=105.0,
                fill_price=105.0,
                role="take",
                position_id=position_id,
                reduce_only=True,
            )
        )
        assert reduced["outcome"] == "filled"
        assert reduced["position"]["status"] == "open"
        assert reduced["position"]["qty"] == 1.0
        assert reduced["position"]["realized_pnl"] == 5.0

        closed = store.apply_filled_paper_order(
            _manual_close_order(
                "close-1",
                timestamp=opened_at + timedelta(minutes=2),
                position_id=position_id,
                fill_price=107.0,
            )
        )
        assert closed["outcome"] == "filled"
        assert closed["position"]["status"] == "closed"
        assert closed["position"]["qty"] == 0.0
        assert closed["position"]["realized_pnl"] == 12.0
        assert {order["role"] for order in closed["cancelled_orders"]} == {"stop", "take"}

        fills = store.read_paper_fills(
            instrument_id=_INSTRUMENT_ID,
            route_fingerprint=_ROUTE_FINGERPRINT,
            paper_contract=_PAPER_CONTRACT,
        )
        assert [fill["order_id"] for fill in fills] == [
            "close-1",
            "take-partial-1",
            "entry-1",
        ]
    finally:
        store.close()
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
