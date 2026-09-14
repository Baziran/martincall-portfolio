from __future__ import annotations

import inspect
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore
from aef_terminal.storage.repos import futures as futures_repo
from aef_terminal.storage.repos import instruments as instrument_repo
from aef_terminal.storage.repos import schema as schema_repo
from aef_terminal.data.futures_lifecycle import (
    current_contract_is_stale,
    current_futures_contract_record,
    persist_current_futures_contract,
)
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import FuturesRouteTransition
from aef_terminal.domain import Bar, BarProvenance
from aef_terminal.settings_contract import (
    WATCHLIST_DISPLAY_MODES,
    WATCHLIST_PRESENTATION_SCOPE,
    require_watchlist_display_mode,
    watchlist_presentation_payload,
)
from aef_terminal.ui import reference_actions
from aef_terminal.ui.reference_services import selected_instruments
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


def _test_postgres_store(dsn: str) -> PostgresStore:
    return PostgresStore(
        dsn,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )


def _test_instruments_repo() -> PostgresStore:
    return PostgresStore(
        None,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )


def test_lookup_instrument_is_exact_instrument_id_only() -> None:
    source = inspect.getsource(instrument_repo.InstrumentsRepoMixin.lookup_instrument)

    assert "WHERE i.instrument_id = %s" in source
    assert "WHERE i.instrument_key = %s" not in source
    assert "display" not in source
    assert "data_symbol" not in source
    assert "provider_symbol" not in source
    assert "name" not in source
    assert "_clean_text" not in source


def test_instruments_schema_has_single_identity_columns_and_constraints() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    create_body = source.split("CREATE TABLE instruments", 1)[1].split(
        "CREATE TABLE drawings",
        1,
    )[0]

    assert "instrument_id text PRIMARY KEY" in create_body
    assert "instrument_key text PRIMARY KEY" not in create_body
    assert "instrument_key text NOT NULL" in create_body
    assert "provider text NOT NULL" in create_body
    assert "provider_symbol text NOT NULL" in create_body
    assert "provider_contract_id text NOT NULL" in create_body
    assert "con_id bigint" in create_body
    assert "display_name text NOT NULL" in create_body
    assert "asset_class text NOT NULL" in create_body
    assert "identity_scope text NOT NULL" in create_body
    assert "watchlist_version bigint" not in create_body
    assert "data_sources jsonb" not in create_body
    assert "provider_symbols jsonb" not in create_body
    assert "instruments_provider_symbols_gin_idx" not in create_body
    assert "instruments_provider_contract_uidx" in create_body
    assert "instruments_provider_symbol_uidx" not in create_body
    assert "instruments_provider_symbol_idx" not in create_body
    assert "instruments_instrument_key_idx" not in create_body
    assert "instruments_watchlist_order_uidx" in create_body
    assert "instruments_provider_contract_required_chk" in create_body
    assert "asset_class = 'future'" in create_body
    assert "AND provider_contract_id = ''" in create_body
    assert "AND con_id IS NULL" in create_body
    assert "identity_scope = 'root'" in create_body
    assert "identity_scope = 'contract'" in create_body
    assert "CREATE TABLE watchlist_state" in source
    for table_name in (
        "bars",
        "trading_hours",
        "trading_schedule_snapshots",
        "trading_session_intervals",
        "gex_snapshots",
        "quote_snapshots",
        "ticks",
        "tick_delta_1m_rollup",
        "tick_volume_profile_5m_rollup",
    ):
        table_body = source.split(f"CREATE TABLE {table_name}", 1)[1].split(
            ')\n                            """',
            1,
        )[0]
        assert "instrument_id text NOT NULL" in table_body
        assert "route_fingerprint text NOT NULL" in table_body
    gex_table_body = source.split("CREATE TABLE gex_snapshots", 1)[1].split(
        ')\n                            """', 1
    )[0]
    assert "provider_symbol text NOT NULL" in gex_table_body
    assert "asset text" not in gex_table_body
    assert "CHECK (source IN ('gex:ibkr', 'gex:ibkr-live'))" in gex_table_body
    assert "DROP TABLE" not in source
    verifier_source = inspect.getsource(schema_repo._verify_route_identity_storage)
    assert "for table in _ROUTE_IDENTITY_TABLES" in verifier_source
    assert "convalidated" in verifier_source
    assert "ROUTE_IDENTITY_SCHEMA_CONTRACT_REQUIRED" in verifier_source
    assert "ALTER TABLE" not in verifier_source
    assert "DROP INDEX" not in verifier_source
    assert "DELETE FROM" not in verifier_source
    assert "UPDATE " not in verifier_source
    for table_name in (
        "drawings",
        "option_targets",
        "price_alerts",
        "paper_orders",
        "paper_positions",
        "paper_fills",
        "paper_trade_events",
    ):
        assert table_name in schema_repo._GENERATED_ROUTE_IDENTITY_TABLES
        assert f"DELETE FROM {table_name} WHERE instrument_id IS NULL" not in source
    assert "DELETE FROM bars WHERE closed = false" not in source
    assert "RENAME COLUMN instrument_key" not in source
    assert "DROP COLUMN IF EXISTS instrument_key" not in source


def test_watchlist_repo_exposes_atomic_selection_not_composable_add() -> None:
    repo_source = inspect.getsource(instrument_repo.InstrumentsRepoMixin)
    selection_source = inspect.getsource(
        instrument_repo.InstrumentsRepoMixin.select_watchlist_instrument
    )
    reorder_source = inspect.getsource(instrument_repo.InstrumentsRepoMixin.reorder_watchlist)
    remove_source = inspect.getsource(
        instrument_repo.InstrumentsRepoMixin.remove_instrument_from_watchlist
    )

    assert "def select_watchlist_instrument(" in repo_source
    assert "def add_instrument_to_watchlist(" not in repo_source
    assert "def upsert_instrument(" not in repo_source
    assert "def remove_instrument_from_watchlist(" in repo_source
    assert "def reorder_watchlist(" in repo_source
    assert "expected_version" in reorder_source
    assert "WATCHLIST_ORDER_VERSION_REQUIRED" in reorder_source
    assert "-1000000 - watchlist_order" in reorder_source
    assert "WITH ORDINALITY" in reorder_source
    assert "for offset" not in reorder_source
    assert "for order" not in reorder_source
    assert "def replace_watchlist_instruments(" not in repo_source
    assert "UPDATE instruments SET watchlist = false" not in repo_source
    assert "aef:watchlist" not in repo_source
    assert "with conn.transaction(), conn.cursor() as cur:" in selection_source
    assert "_write_futures_contract_on_cursor(" in selection_source
    assert "instruments.provider = EXCLUDED.provider" in selection_source
    assert "instruments.provider_contract_id = EXCLUDED.provider_contract_id" in selection_source
    assert "instruments.con_id IS NOT DISTINCT FROM EXCLUDED.con_id" in selection_source
    assert "instruments.asset_class = EXCLUDED.asset_class" in selection_source
    assert "instruments.identity_scope = EXCLUDED.identity_scope" in selection_source
    assert selection_source.index("INSERT INTO instruments") < selection_source.index(
        "_write_futures_contract_on_cursor("
    )
    assert selection_source.index("_write_futures_contract_on_cursor(") < selection_source.index(
        "UPDATE instruments"
    )
    assert selection_source.index("UPDATE instruments") < selection_source.index(
        "UPDATE watchlist_state"
    )
    action_source = inspect.getsource(reference_actions.add_reference_instrument_payload)
    assert "select_watchlist_instrument(" in action_source
    assert "upsert_instrument(" not in action_source
    assert "add_instrument_to_watchlist(" not in action_source
    assert "persist_current_futures_contract(" not in action_source
    for method in (
        instrument_repo.InstrumentsRepoMixin.select_watchlist_instrument,
        instrument_repo.InstrumentsRepoMixin.remove_instrument_from_watchlist,
        instrument_repo.InstrumentsRepoMixin.reorder_watchlist,
    ):
        method_source = inspect.getsource(method)
        assert "_clean_text" not in method_source
        assert "SELECT version FROM watchlist_state WHERE id = true FOR UPDATE" in method_source
        assert "WATCHLIST_STATE_REQUIRED" in method_source
        assert "INSERT INTO watchlist_state" not in method_source
        assert "ON CONFLICT (id)" not in method_source
    assert "COALESCE((SELECT version FROM watchlist_state" not in repo_source
    assert "client_setting_keys_for_instrument" in remove_source
    assert "server_setting_mutations_without_instrument" in remove_source
    assert "DELETE FROM settings" in remove_source
    assert "WATCHLIST_PRESENTATION_SCOPE" in remove_source
    assert "WatchlistRetirementSettingsSnapshot(" in remove_source
    assert "projected_client_orders" in remove_source
    assert "read_settings_revision(cur)" in remove_source
    assert remove_source.index("DELETE FROM settings") < remove_source.rindex(
        "UPDATE watchlist_state"
    )
    assert remove_source.rindex("UPDATE watchlist_state") < remove_source.rindex("SELECT scope,")
    presentation_source = inspect.getsource(
        instrument_repo.InstrumentsRepoMixin.set_watchlist_presentation
    )
    for source in (presentation_source, remove_source):
        assert source.index("pg_advisory_xact_lock") < source.index("settings_cache.clear")
        assert "advance_settings_revision(cur)" in source


def test_atomic_watchlist_selection_returns_post_increment_version() -> None:
    instrument = ibkr_stock_payload("SPY")
    instrument_id = qualified_instrument_id(instrument)
    rows = iter(
        [
            (4,),
            (False,),
            (0,),
            (instrument_id,),
            (5,),
        ]
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement, _params=()):
            return None

        def fetchone(self):
            return next(rows)

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo = _test_instruments_repo()
    repo._psycopg = lambda: None
    repo._connect = lambda: Connection()

    watchlist_version = repo.select_watchlist_instrument(instrument)

    assert watchlist_version == 5


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("instrument_id", "ibkr|future_root|NQ|CME|USD|", "IDENTITY_MISMATCH"),
        ("provider", "coinbase", "PROVIDER_MISMATCH"),
        ("root", "NQ", "ROOT_MISMATCH"),
        ("is_current", False, "MUST_BE_CURRENT"),
    ],
)
def test_atomic_watchlist_selection_rejects_cross_route_current_contract(
    field: str,
    value: object,
    error: str,
) -> None:
    instrument = ibkr_future_payload("ES")
    contract = current_futures_contract_record(instrument)
    contract[field] = value

    with pytest.raises(ValueError, match=error):
        _test_instruments_repo().select_watchlist_instrument(
            instrument,
            current_futures_contract=contract,
        )


def test_atomic_watchlist_selection_requires_one_typed_futures_lifecycle() -> None:
    repo = _test_instruments_repo()
    future = ibkr_future_payload("ES")
    stock = ibkr_stock_payload("SPY")

    with pytest.raises(ValueError, match="WATCHLIST_FUTURES_CURRENT_CONTRACT_REQUIRED"):
        repo.select_watchlist_instrument(future)
    with pytest.raises(ValueError, match="WATCHLIST_NON_FUTURES_CONTRACT_FORBIDDEN"):
        repo.select_watchlist_instrument(
            stock,
            current_futures_contract=current_futures_contract_record(future),
        )
    with pytest.raises(ValueError, match="WATCHLIST_NON_FUTURES_ROUTE_TRANSITION_FORBIDDEN"):
        repo.select_watchlist_instrument(stock, route_transition=SimpleNamespace())


@pytest.mark.parametrize(
    ("method_name", "args", "kwargs"),
    [
        ("select_watchlist_instrument", (ibkr_stock_payload("SPY"),), {}),
        ("remove_instrument_from_watchlist", ("instrument-a",), {}),
        ("reorder_watchlist", ([],), {"expected_version": 0}),
    ],
)
def test_watchlist_mutations_require_existing_state_without_repair(
    method_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    executions: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params=()):
            executions.append(str(statement))

        def fetchone(self):
            return None

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo = _test_instruments_repo()
    repo._psycopg = lambda: None
    repo._connect = lambda: Connection()

    with pytest.raises(RuntimeError, match="WATCHLIST_STATE_REQUIRED"):
        getattr(repo, method_name)(*args, **kwargs)

    assert len(executions) == 2
    assert "pg_advisory_xact_lock" in executions[0]
    assert "SELECT version FROM watchlist_state WHERE id = true FOR UPDATE" in executions[1]
    assert all("INSERT INTO watchlist_state" not in statement for statement in executions)
    assert all("UPDATE instruments" not in statement for statement in executions)


def test_instrument_read_returns_one_persisted_identity_snapshot() -> None:
    select = instrument_repo.InstrumentsRepoMixin._INSTRUMENT_SELECT
    read_source = inspect.getsource(instrument_repo.InstrumentsRepoMixin.read_watchlist_snapshot)
    action_source = inspect.getsource(reference_actions.current_watchlist)
    response_source = inspect.getsource(reference_actions.current_watchlist_response)

    assert "FROM instruments AS i" in select
    assert "FROM futures_contracts AS fc" in select
    assert "FROM trading_schedule_snapshots AS snapshot" in select
    assert "FROM trading_session_intervals AS session_interval" in select
    assert "LEFT JOIN LATERAL" in select
    assert "'min_tick', fc.metadata->'min_tick'" in select
    assert "'min_tick', fc.metadata->>'min_tick'" not in select
    assert "read_current_futures_contract" not in read_source
    assert "_attach_persisted_current_contract" not in inspect.getsource(
        instrument_repo.InstrumentsRepoMixin
    )
    assert "read_trading_hours" not in action_source
    assert "read_trading_session_intervals" not in action_source
    assert response_source.count("_watchlist_store(") == 1


def test_watchlist_has_one_atomic_read_contract_without_legacy_cache() -> None:
    repo_source = inspect.getsource(instrument_repo.InstrumentsRepoMixin)

    assert "def read_watchlist_snapshot(" in repo_source
    assert "def read_watchlist_instruments(" not in repo_source
    assert "def read_watchlist_version(" not in repo_source
    assert "_instruments_cache" not in repo_source


def test_empty_watchlist_snapshot_reads_items_and_version_atomically() -> None:
    executions: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement: str):
            executions.append(statement)

        def fetchall(self):
            return [(7, *([None] * 16))]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    repo = _test_instruments_repo()
    repo._connect = lambda: Connection()

    items, version = repo.read_watchlist_snapshot()

    assert items == []
    assert version == 7
    assert len(executions) == 1
    assert "LEFT JOIN LATERAL" in executions[0]
    assert "SELECT snapshot.watchlist_version, selected.*" in executions[0]


def test_watchlist_snapshot_requires_existing_state_instead_of_defaulting_to_zero() -> None:
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement):
            return None

        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    repo = _test_instruments_repo()
    repo._connect = lambda: Connection()

    with pytest.raises(RuntimeError, match="WATCHLIST_STATE_REQUIRED"):
        repo.read_watchlist_snapshot()


def test_instrument_row_payload_merges_persisted_contract_and_session() -> None:
    payload = instrument_repo._instrument_row_payload(
        (
            "ibkr|future_root|ES|CME|USD|",
            "ES",
            "ibkr",
            "ES",
            "",
            None,
            "E-mini S&P 500",
            "future",
            "root",
            {"status": "unknown"},
            True,
            0,
            7,
            {
                "contract_identity": {
                    "root": "ES",
                    "exchange": "CME",
                    "currency": "USD",
                }
            },
            {
                "contract_key": "ESU6",
                "provider_contract_id": "649180671",
                "local_symbol": "ESU6",
                "source": "provider_current_contract",
                "min_tick": 0.25,
            },
            {
                "provider": "ibkr",
                "calendar": "ibkr_trading_hours",
                "provider_contract_id": "649180671",
                "trading_intervals": [
                    {
                        "session_date": "2026-07-13",
                        "opens_at": "2026-07-13T22:00:00+00:00",
                        "closes_at": "2026-07-14T21:00:00+00:00",
                        "status": "open",
                    }
                ],
            },
            {"display_mode": "trend", "revision": 4},
        )
    )

    assert payload["instrument_id"] == "ibkr|future_root|ES|CME|USD|"
    assert payload["contract_identity"]["current_contract"]["contract_key"] == "ESU6"
    assert payload["contract_identity"]["current_contract"]["min_tick"] == 0.25
    assert payload["session"]["provider_contract_id"] == "649180671"
    assert payload["session"]["trading_intervals"][0]["session_date"] == "2026-07-13"
    assert payload["session"]["trading_intervals"][0]["status"] == "open"
    assert payload["presentation"] == {"display_mode": "trend", "revision": 4}


def test_current_futures_contract_lifecycle_persists_provider_min_tick() -> None:
    writes: list[dict[str, Any]] = []
    store = SimpleNamespace(write_futures_contract=writes.append)

    persist_current_futures_contract(store, ibkr_future_payload("ES"))

    assert len(writes) == 1
    assert writes[0]["metadata"]["min_tick"] == 0.25


def test_ibkr_current_contract_without_provider_min_tick_is_stale() -> None:
    instrument = ibkr_future_payload("ES")
    current = instrument["contract_identity"]["current_contract"]
    resolved_at = datetime.fromisoformat(current["resolved_at"])

    assert current_contract_is_stale(instrument, now=resolved_at) is False

    current.pop("min_tick")

    assert current_contract_is_stale(instrument, now=resolved_at) is True


@pytest.mark.parametrize(
    "min_tick",
    (True, "0.25", 0, -0.25, float("nan"), float("inf")),
)
def test_current_futures_contract_lifecycle_rejects_invalid_provider_min_tick(
    min_tick: object,
) -> None:
    instrument = ibkr_future_payload("ES")
    instrument["contract_identity"]["current_contract"]["min_tick"] = min_tick
    store = SimpleNamespace(
        write_futures_contract=lambda _payload: pytest.fail(
            "invalid provider min_tick must fail before persistence"
        )
    )

    with pytest.raises(
        ValueError,
        match=r"current_contract\.min_tick must be an exact finite positive number",
    ):
        persist_current_futures_contract(store, instrument)


def test_current_futures_contract_read_projects_provider_min_tick() -> None:
    payload = futures_repo._futures_contract_row_payload(
        (
            "ibkr",
            "ibkr|future_root|ES|CME|USD|",
            "ESU6",
            "ES",
            "CME",
            "USD",
            "ESU6",
            649180671,
            "20260918",
            "202609",
            None,
            None,
            datetime(2026, 7, 30, tzinfo=timezone.utc),
            {
                "provider_contract_id": "649180671",
                "min_tick": 0.25,
            },
        ),
        source="provider_current_contract",
    )

    assert payload["min_tick"] == 0.25


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("current_contract", {"contract_key": "ESU6"}),
        ("provider", "ibkr"),
        ("provider_contract_id", "756733"),
        ("con_id", 756733),
        ("asset_class", "stock"),
    ),
)
def test_instrument_row_payload_rejects_canonical_columns_in_stored_identity(
    field: str,
    value: object,
) -> None:
    row = (
        "ibkr|contract|756733",
        "SPY",
        "ibkr",
        "SPY",
        "756733",
        756733,
        "SPY",
        "stock",
        "contract",
        {},
        True,
        0,
        1,
        {"contract_identity": {field: value}},
        None,
        None,
        None,
    )

    with pytest.raises(
        RuntimeError,
        match="STORED_CONTRACT_IDENTITY_CANONICAL_FIELD_FORBIDDEN",
    ):
        instrument_repo._instrument_row_payload(row)


def test_watchlist_presentation_joins_atomic_snapshot_and_updates_exact_membership() -> None:
    select = instrument_repo.InstrumentsRepoMixin._INSTRUMENT_SELECT
    update_source = inspect.getsource(
        instrument_repo.InstrumentsRepoMixin.set_watchlist_presentation
    )

    assert "LEFT JOIN settings AS presentation_setting" in select
    assert "presentation_setting.key = i.instrument_id" in select
    assert "presentation_setting.value AS presentation" in select
    assert "INSERT INTO settings AS current_setting" in update_source
    assert "instrument.instrument_id = %s" in update_source
    assert "instrument.watchlist = true" in update_source
    assert "ON CONFLICT (scope, key) DO UPDATE" in update_source
    assert "jsonb_build_object('display_mode', %s::text, 'revision', 1)" in update_source
    assert "RETURNING value" in update_source
    assert "FOR UPDATE" in update_source
    assert "watchlist_presentation_payload(" in update_source
    assert "allow_default_revision=False" in update_source
    assert 'current_presentation["display_mode"] == mode' in update_source
    assert "return current_presentation" in update_source
    assert "ELSE 1" not in update_source
    assert "watchlist_state" not in update_source


def test_settings_contract_owns_watchlist_presentation_shape() -> None:
    assert WATCHLIST_DISPLAY_MODES == {"classic", "trend"}
    assert require_watchlist_display_mode("trend") == "trend"
    assert watchlist_presentation_payload() == {
        "display_mode": "classic",
        "revision": 0,
    }
    assert watchlist_presentation_payload(
        {"display_mode": "trend", "revision": 3},
        allow_default_revision=False,
    ) == {"display_mode": "trend", "revision": 3}
    with pytest.raises(ValueError, match="WATCHLIST_PRESENTATION_REVISION_INVALID"):
        watchlist_presentation_payload(
            {"display_mode": "trend", "revision": 0},
            allow_default_revision=False,
        )


def test_watchlist_presentation_repo_returns_atomic_revision_ack() -> None:
    executions: list[tuple[str, tuple]] = []
    transaction_events: list[str] = []
    rows = iter(
        [
            ({"display_mode": "classic", "revision": 6},),
            ({"display_mode": "trend", "revision": 7},),
            (8,),
        ]
    )

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, params):
            executions.append((statement, params))

        def fetchone(self):
            return next(rows)

    class Transaction:
        def __enter__(self):
            transaction_events.append("enter")
            return self

        def __exit__(self, *_args):
            transaction_events.append("exit")
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo = _test_instruments_repo()
    repo._connect = lambda: Connection()

    presentation = repo.set_watchlist_presentation(
        "ibkr|contract|756733",
        "trend",
    )

    assert presentation == {"display_mode": "trend", "revision": 7}
    assert transaction_events == ["enter", "exit"]
    assert "pg_advisory_xact_lock" in executions[0][0]
    assert executions[1][1] == (
        "watchlist_presentation",
        "ibkr|contract|756733",
    )
    assert executions[2][1] == (
        "watchlist_presentation",
        "trend",
        "ibkr|contract|756733",
    )


def test_watchlist_presentation_same_mode_is_revision_neutral() -> None:
    executions: list[tuple[str, tuple]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, params):
            executions.append((statement, params))

        def fetchone(self):
            return ({"display_mode": "classic", "revision": 6},)

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo = _test_instruments_repo()
    repo._connect = lambda: Connection()

    presentation = repo.set_watchlist_presentation(
        "ibkr|contract|756733",
        "classic",
    )

    assert presentation == {"display_mode": "classic", "revision": 6}
    assert len(executions) == 2
    assert "INSERT INTO settings" not in "\n".join(statement for statement, _params in executions)


def test_watchlist_presentation_repo_rejects_malformed_existing_row_without_repair() -> None:
    executions: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params):
            executions.append(str(statement))

        def fetchone(self):
            return ({"display_mode": "trend", "revision": 0},)

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo = _test_instruments_repo()
    repo._connect = lambda: Connection()

    with pytest.raises(
        ValueError,
        match="WATCHLIST_PRESENTATION_REVISION_INVALID",
    ):
        repo.set_watchlist_presentation("ibkr|contract|756733", "classic")

    assert len(executions) == 2
    assert "pg_advisory_xact_lock" in executions[0]
    assert "FOR UPDATE" in executions[1]
    assert all("INSERT INTO settings" not in statement for statement in executions)


def test_instrument_row_payload_rejects_malformed_persisted_presentation() -> None:
    row = (
        "ibkr|contract|756733",
        "SPY",
        "ibkr",
        "SPY",
        "756733",
        756733,
        "SPY",
        "stock",
        "contract",
        {},
        True,
        0,
        1,
        {},
        None,
        None,
        {"display_mode": "trend", "revision": 0},
    )

    with pytest.raises(ValueError, match="WATCHLIST_PRESENTATION_REVISION_INVALID"):
        instrument_repo._instrument_row_payload(row)


def test_reference_add_path_has_no_static_catalog_fallback() -> None:
    module_source = inspect.getsource(reference_actions)
    add_source = inspect.getsource(reference_actions.add_reference_instrument_payload)

    assert "def instrument_from_payload(" not in module_source
    assert "instrument_catalog()" not in add_source
    assert "deps.instrument_catalog" not in add_source
    assert "deps.bind_provider_instrument" in add_source
    assert "deps.bind_provider_future_root" in add_source
    assert "provider_instrument_from_payload" not in module_source
    assert "provider_future_root_from_payload" not in module_source


def test_instrument_values_rejects_concrete_futures_identity() -> None:
    concrete = ibkr_future_payload("ES")
    concrete["provider_contract_id"] = "12345"
    with pytest.raises(ValueError, match="FUTURES_ROOT_PROVIDER_CONTRACT_ID_FORBIDDEN"):
        instrument_repo._instrument_values(concrete)


def test_instrument_values_requires_futures_root_identity() -> None:
    with pytest.raises(ValueError, match="FUTURES_ROOT_IDENTITY_REQUIRED"):
        invalid = ibkr_future_payload("ES")
        invalid["contract_identity"].pop("root")
        instrument_repo._instrument_values(invalid)
    values = instrument_repo._instrument_values(ibkr_future_payload("ES"))
    assert values[0] == qualified_instrument_id(ibkr_future_payload("ES"))
    assert values[4] == ""
    assert values[5] is None


def test_futures_identity_does_not_use_display_instrument_key_as_proof() -> None:
    renamed = ibkr_future_payload("ES")
    renamed["key"] = "DISPLAY ONLY"
    renamed["instrument_key"] = "DISPLAY ONLY"

    values = instrument_repo._instrument_values(renamed)

    assert values[0] == "ibkr|future_root|ES|CME|USD|"
    assert values[1] == "DISPLAY ONLY"
    assert values[3] == "ES"


def test_instrument_values_requires_canonical_provider_symbol_field() -> None:
    invalid = ibkr_stock_payload("PLTR")
    invalid.pop("provider_symbol")
    with pytest.raises(ValueError, match="PROVIDER_SYMBOL_REQUIRED"):
        instrument_repo._instrument_values(invalid)


def test_instrument_values_preserves_opaque_provider_identity_fields_exactly() -> None:
    instrument = {
        "instrument_id": "demo|contract| contract-id ",
        "instrument_key": "DISPLAY",
        "display": "DISPLAY",
        "provider": "demo",
        "provider_symbol": " route-symbol ",
        "provider_contract_id": " contract-id ",
        "asset_class": "stock",
        "contract_identity": {
            "provider": "demo",
            "provider_contract_id": " contract-id ",
            "asset_class": "stock",
        },
    }

    values = instrument_repo._instrument_values(instrument)

    assert values[0] == "demo|contract| contract-id "
    assert values[3] == " route-symbol "
    assert values[4] == " contract-id "


def test_numeric_provider_id_parser_accepts_only_exact_int_or_canonical_ascii_decimal() -> None:
    class StringConvertibleProviderId:
        def __str__(self) -> str:
            return "12345"

    assert parse_exact_positive_decimal_provider_id(12345) == 12345
    assert parse_exact_positive_decimal_provider_id("12345") == 12345
    assert parse_exact_positive_decimal_provider_id(True) == 0
    assert parse_exact_positive_decimal_provider_id(StringConvertibleProviderId()) == 0
    assert parse_exact_positive_decimal_provider_id(" 12345 ") == 0
    assert parse_exact_positive_decimal_provider_id("0012345") == 0
    assert parse_exact_positive_decimal_provider_id("１２３４５") == 0


def test_instrument_storage_rejects_source_qualified_provider_identity() -> None:
    instrument = ibkr_stock_payload("SPY", con_id=756733)
    instrument["provider"] = "ibkr:historical"
    instrument["instrument_id"] = "ibkr:historical|contract|756733"
    instrument["contract_identity"] = {
        **instrument["contract_identity"],
        "provider": "ibkr:historical",
    }

    with pytest.raises(ValueError, match="INSTRUMENT_PROVIDER_NOT_CANONICAL"):
        instrument_repo._instrument_values(instrument)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("instrument_id", 12345),
        ("provider_symbol", 12345),
        ("provider_contract_id", 12345),
    ),
)
def test_instrument_values_rejects_non_string_opaque_identity_fields(
    field: str,
    value: object,
) -> None:
    instrument = ibkr_stock_payload("PLTR")
    instrument[field] = value
    if field == "provider_contract_id":
        instrument["contract_identity"]["provider_contract_id"] = value

    with pytest.raises(ValueError):
        instrument_repo._instrument_values(instrument)


def test_instrument_identity_rejects_whitespace_variant_instead_of_normalizing_it() -> None:
    instrument = ibkr_stock_payload("PLTR")
    instrument["instrument_id"] = f" {instrument['instrument_id']} "

    with pytest.raises(ValueError, match="INSTRUMENT_ID_MISMATCH"):
        instrument_repo._instrument_values(instrument)


def test_watchlist_selection_matches_only_exact_instrument_ids() -> None:
    instruments = [ibkr_future_payload("ES"), ibkr_stock_payload("SPY")]
    es_id = qualified_instrument_id(instruments[0])
    spy_id = qualified_instrument_id(instruments[1])

    assert [item["key"] for item in selected_instruments(instruments, (es_id,))] == ["ES"]
    assert [item["key"] for item in selected_instruments(instruments, (spy_id,))] == ["SPY"]
    assert selected_instruments(instruments, ("ES",)) == []
    assert selected_instruments(instruments, ("SPY",)) == []
    assert selected_instruments(instruments, ("E-mini S&P 500",)) == []
    assert selected_instruments(instruments, ("ESZ6",)) == []
    with pytest.raises(TypeError, match="typed sequence"):
        selected_instruments(instruments, f"{es_id},{spy_id}")
    with pytest.raises(ValueError, match="duplicates"):
        selected_instruments(instruments, (es_id, es_id))


def _postgres_schema_dsn(schema: str) -> str:
    dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options=-csearch_path%3D{schema}"


def test_postgres_option_target_intent_and_market_sample_updates_serialize() -> None:
    import psycopg

    intent_merge_started = threading.Event()
    release_intent_merge = threading.Event()
    sample_call_started = threading.Event()
    sample_call_finished = threading.Event()

    class CoordinatedStore(PostgresStore):
        @classmethod
        def _option_target_intent_update_payload(
            cls,
            existing: dict[str, Any],
            incoming: dict[str, Any],
        ) -> dict[str, Any]:
            intent_merge_started.set()
            if not release_intent_merge.wait(timeout=5):
                raise TimeoutError("test did not release option target intent merge")
            return super()._option_target_intent_update_payload(existing, incoming)

    schema = f"test_option_target_serialization_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = CoordinatedStore(
        _postgres_schema_dsn(schema),
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )
    executor = ThreadPoolExecutor(max_workers=2)
    instrument_id = "ibkr|future_root|ES"
    route_key = "ibkr|future|ESU6"
    target_id = "opt-concurrent"
    initial_item = {
        "id": target_id,
        "point": {
            "price": 7550.0,
            "ts": "2026-06-22T05:00:00+00:00",
        },
        "payload": {
            "intent": {
                "provider_symbol": "ES",
                "mode": "normal",
                "right": "P",
                "target_dte": "0dte",
                "sec_type": "FOP",
                "con_id": 123,
                "local_symbol": "ES P7550",
                "exchange": "CME",
                "expiry": "20990622",
                "expiry_at": "2099-06-22T20:00:00+00:00",
                "strike": 7550.0,
                "trading_class": "ES",
                "multiplier": 50.0,
                "currency": "USD",
            }
        },
    }
    updated_item = {
        **initial_item,
        "payload": {
            "intent": {
                **initial_item["payload"]["intent"],
                "mode": "aggressive",
            }
        },
    }

    def write_market_sample() -> dict[tuple[str, str, str], dict[str, Any]]:
        sample_call_started.set()
        try:
            return store.update_option_target_market_samples(
                [
                    (
                        instrument_id,
                        route_key,
                        target_id,
                        {
                            "live_quote_status": "empty",
                            "fair_price_status": "pending",
                        },
                    )
                ]
            )
        finally:
            sample_call_finished.set()

    try:
        store.initialize()
        store.create_option_target_intent(
            instrument_id,
            "ES",
            "5m",
            initial_item,
            route_fingerprint=route_key,
            provider="ibkr",
            provider_contract_id="11004968",
        )
        intent_future = executor.submit(
            store.update_option_target_intent,
            instrument_id,
            "ES",
            "5m",
            updated_item,
            route_fingerprint=route_key,
            provider="ibkr",
            provider_contract_id="11004968",
        )
        assert intent_merge_started.wait(timeout=2)
        sample_future = executor.submit(write_market_sample)
        assert sample_call_started.wait(timeout=2)
        assert sample_call_finished.wait(timeout=0.25) is False

        release_intent_merge.set()
        intent_future.result(timeout=5)
        sample_future.result(timeout=5)

        rows = store.read_option_targets(
            timeframe="5m",
            route_pairs=[(instrument_id, route_key)],
        )
        assert len(rows) == 1
        option_payload = rows[0]["payload"]["payload"]
        assert option_payload["intent"]["mode"] == "aggressive"
        assert option_payload["market_sample"] == {
            "live_quote_status": "empty",
            "fair_price_status": "pending",
        }
    finally:
        release_intent_merge.set()
        executor.shutdown(wait=True, cancel_futures=True)
        store.close()
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_option_target_route_limit_serializes_concurrent_creates() -> None:
    import psycopg

    schema = f"test_option_target_limit_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    executor = ThreadPoolExecutor(max_workers=5)
    start = threading.Barrier(5)
    instrument_id = "ibkr|future_root|ES"
    route_key = "ibkr|future|ESU6"

    def create_target(index: int):
        start.wait(timeout=5)
        return store.create_option_target_intent(
            instrument_id,
            "ES",
            "5m",
            {
                "id": f"opt-concurrent-{index}",
                "point": {
                    "price": 7550.0 + index,
                    "ts": "2026-06-22T05:00:00+00:00",
                },
                "payload": {
                    "intent": {
                        "provider_symbol": "ES",
                        "mode": "normal",
                        "right": "P",
                        "target_dte": "0dte",
                        "sec_type": "FOP",
                        "con_id": 1000 + index,
                        "local_symbol": f"ES P{7550 + index}",
                        "exchange": "CME",
                        "expiry": "20990622",
                        "expiry_at": "2099-06-22T20:00:00+00:00",
                        "strike": 7550.0 + index,
                        "trading_class": "ES",
                        "multiplier": 50.0,
                        "currency": "USD",
                    }
                },
            },
            route_fingerprint=route_key,
            provider="ibkr",
            provider_contract_id="11004968",
        )

    try:
        store.initialize()
        results = [
            future.result(timeout=10)
            for future in [executor.submit(create_target, index) for index in range(5)]
        ]
        rows = store.read_option_targets(
            timeframe="5m",
            route_pairs=[(instrument_id, route_key)],
        )
        evicted_ids = {item["id"] for result in results for item in result.evicted}
        remaining_ids = {row["id"] for row in rows}

        assert len(rows) == 3
        assert len(evicted_ids) == 2
        assert evicted_ids.isdisjoint(remaining_ids)
        assert evicted_ids | remaining_ids == {f"opt-concurrent-{index}" for index in range(5)}
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        store.close()
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_timescaledb_runtime_calls_respect_isolated_storage_schema() -> None:
    import psycopg

    schema = f"test_timescaledb_runtime_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    schema_dsn = _postgres_schema_dsn(schema)
    store = _test_postgres_store(schema_dsn)
    instrument_id = "ibkr|contract|timescale-runtime"
    route_token = "opaque timescale runtime token"
    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, tzinfo=timezone.utc)
    try:
        store.initialize()
        with psycopg.connect(schema_dsn) as conn:
            conn.execute(
                """
                INSERT INTO ticks (
                    provider, instrument_id, route_fingerprint, ts,
                    price, volume, delta_sign
                )
                VALUES ('ibkr', %s, %s, %s, 100.25, 2, 1)
                """,
                (instrument_id, route_token, datetime(2026, 7, 21, tzinfo=timezone.utc)),
            )

        assert (
            store.rollup_ticks(datetime(2026, 7, 21, 0, 2, tzinfo=timezone.utc), provider="ibkr")[
                "deleted_ticks"
            ]
            == 0
        )
        assert store.read_tick_delta(
            instrument_id,
            route_token,
            start,
            end,
            bucket="5 seconds",
            include_rollup=False,
        )

        assert store.rollup_ticks(end, provider="ibkr")["deleted_ticks"] == 1
        assert store.read_tick_delta(
            instrument_id,
            route_token,
            start,
            end,
        )
        assert (
            store.read_tick_delta(
                instrument_id,
                route_token,
                start,
                end,
                bucket="5 seconds",
                include_rollup=False,
            )
            == []
        )
        assert store.read_tick_bar_history(
            instrument_id,
            route_token,
            start,
            end,
        )
        assert store.read_tick_volume_profile_history(
            instrument_id,
            route_token,
            start,
            end,
        )
        assert store.read_volume_profile(
            instrument_id,
            route_token,
            start,
            end,
        )
    finally:
        store.close()
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_route_identity_verifier_is_read_only_and_rejects_index_drift() -> None:
    assert schema_repo._ROUTE_IDENTITY_PRIMARY_KEYS["drawings"] == ("id",)
    assert schema_repo._ROUTE_IDENTITY_PRIMARY_KEYS["price_alerts"] == ("id",)

    class RouteSchemaCursor:
        def __init__(
            self,
            *,
            wrong_bars_index: bool = False,
            forbidden_index: str | None = None,
        ) -> None:
            self.executions: list[str] = []
            self.statement = ""
            self.params: tuple[object, ...] = ()
            self.wrong_bars_index = wrong_bars_index
            self.forbidden_index = forbidden_index

        def execute(self, statement: object, params: tuple[object, ...] = ()) -> None:
            self.statement = str(statement)
            self.params = params
            self.executions.append(self.statement)

        def fetchone(self):
            if "constraint_row.contype = 'p'" in self.statement:
                table = str(self.params[0])
                return (
                    f"{table}_pkey",
                    list(schema_repo._ROUTE_IDENTITY_PRIMARY_KEYS[table]),
                )
            if "FROM pg_index AS index_row" in self.statement:
                index_name = str(self.params[0])
                options, predicate = schema_repo._ROUTE_IDENTITY_INDEX_OPTIONS[index_name]
                if self.wrong_bars_index and index_name == "bars_hygiene_recent_idx":
                    options = (0, 0, 0, 0, 0)
                return (
                    index_name in schema_repo._ROUTE_IDENTITY_UNIQUE_INDEXES,
                    True,
                    True,
                    "btree",
                    schema_repo._ROUTE_IDENTITY_INDEXES[index_name][0],
                    list(schema_repo._ROUTE_IDENTITY_INDEXES[index_name][1]),
                    list(options),
                    predicate or None,
                )
            if "count(*) = 2" in self.statement:
                return (True,)
            raise AssertionError(f"unexpected fetchone statement: {self.statement}")

        def fetchall(self):
            if "index_relation.relkind = 'i'" in self.statement:
                return [(self.forbidden_index,)] if self.forbidden_index else []
            if "pg_get_constraintdef" not in self.statement:
                raise AssertionError(f"unexpected fetchall statement: {self.statement}")
            return [
                (
                    f"{table}_route_identity_chk",
                    table,
                    True,
                    "CHECK ((instrument_id <> ''::text) AND (route_fingerprint <> ''::text))",
                )
                for table in schema_repo._ROUTE_IDENTITY_TABLES
            ]

    current = RouteSchemaCursor()
    schema_repo._verify_route_identity_storage(current)
    assert current.executions
    assert all(statement.lstrip().startswith("SELECT") for statement in current.executions)

    drifted = RouteSchemaCursor(wrong_bars_index=True)
    with pytest.raises(
        RuntimeError,
        match=r"ROUTE_IDENTITY_SCHEMA_CONTRACT_REQUIRED .*bars_hygiene_recent_idx",
    ):
        schema_repo._verify_route_identity_storage(drifted)
    assert drifted.executions
    assert all(statement.lstrip().startswith("SELECT") for statement in drifted.executions)

    for forbidden_index in schema_repo._FORBIDDEN_STORAGE_INDEXES:
        legacy_index = RouteSchemaCursor(forbidden_index=forbidden_index)
        with pytest.raises(
            RuntimeError,
            match=(r"ROUTE_IDENTITY_SCHEMA_CONTRACT_REQUIRED .*" + forbidden_index),
        ):
            schema_repo._verify_route_identity_storage(legacy_index)


def test_postgres_clean_bootstrap_creates_exact_canonical_v18_once() -> None:
    import psycopg

    schema = f"test_storage_bootstrap_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    schema_dsn = _postgres_schema_dsn(schema)
    stores: list[PostgresStore] = []
    try:
        first = _test_postgres_store(schema_dsn)
        stores.append(first)
        first.initialize()
        second = _test_postgres_store(schema_dsn)
        stores.append(second)
        second.initialize()

        with psycopg.connect(schema_dsn) as conn:
            versions = conn.execute(
                "SELECT version FROM storage_schema_versions ORDER BY version"
            ).fetchall()
            canonical_relations = conn.execute(
                """
                SELECT to_regclass('bars'),
                       to_regclass('instruments'),
                       to_regclass('trading_schedule_snapshots'),
                       to_regclass('trading_session_intervals'),
                       to_regclass('discord_signal_messages')
                """
            ).fetchone()
            tick_rowstore = conn.execute(
                """
                SELECT compression_enabled
                FROM timescaledb_information.hypertables
                WHERE hypertable_schema = %s
                  AND hypertable_name = 'ticks'
                """,
                (schema,),
            ).fetchone()
            tick_columnstore_jobs = conn.execute(
                """
                SELECT count(*)
                FROM timescaledb_information.jobs
                WHERE hypertable_schema = %s
                  AND hypertable_name = 'ticks'
                  AND proc_name IN ('policy_compression', 'policy_columnstore')
                """,
                (schema,),
            ).fetchone()

        assert versions == [(18,)]
        assert canonical_relations is not None
        assert all(relation is not None for relation in canonical_relations)
        assert tick_rowstore == (False,)
        assert tick_columnstore_jobs == (0,)
    finally:
        for store in stores:
            try:
                store._pool.close()
            except Exception:
                pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_discord_signal_journal_orders_updates_and_tombstones() -> None:
    import psycopg

    schema = f"test_discord_signal_journal_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    schema_dsn = _postgres_schema_dsn(schema)
    store = _test_postgres_store(schema_dsn)
    published_at = datetime(2026, 8, 1, 13, 30, tzinfo=timezone.utc)
    row = {
        "message_id": "1533100000000000000",
        "channel_id": "800106845237805067",
        "author_id": "795769152258310174",
        "author_name": "GAA",
        "published_at": published_at,
        "content": "  7370c @ 13.3  ",
        "reply_to_message_id": None,
    }
    try:
        store.initialize()
        created = store.upsert_discord_signal_messages(
            (row,),
            source_event="message_create",
            observed_at=datetime(2026, 8, 1, 13, 30, 1, tzinfo=timezone.utc),
        )
        replayed = store.upsert_discord_signal_messages(
            (row,),
            source_event="message_create",
            observed_at=datetime(2026, 8, 1, 13, 30, 1, tzinfo=timezone.utc),
        )
        rejected_update = store.upsert_discord_signal_messages(
            ({**row, "content": "stale update"},),
            source_event="message_update",
            observed_at=datetime(2026, 8, 1, 13, 30, tzinfo=timezone.utc),
        )
        assert created[row["message_id"]]["outcome"] == "applied"
        assert replayed[row["message_id"]]["outcome"] == "replayed"
        assert rejected_update[row["message_id"]]["outcome"] == "rejected"
        assert rejected_update[row["message_id"]]["message"]["content"] == row["content"]
        equal_time_different_event = store.upsert_discord_signal_messages(
            (row,),
            source_event="snapshot",
            observed_at=datetime(2026, 8, 1, 13, 30, 1, tzinfo=timezone.utc),
        )
        assert equal_time_different_event[row["message_id"]]["outcome"] == "rejected"
        assert equal_time_different_event[row["message_id"]]["source_event"] == "message_create"
        unknown_delete = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000999",
            channel_id="800106845237805067",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, tzinfo=timezone.utc),
        )
        wrong_channel_delete = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000000",
            channel_id="800106845237805099",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, tzinfo=timezone.utc),
        )
        assert unknown_delete == {
            "message_id": "1533100000000000999",
            "outcome": "applied",
            "message": None,
            "deleted": True,
            "source_event": "message_delete",
            "source_event_at": "2026-08-01T13:30:02+00:00",
        }
        assert wrong_channel_delete["outcome"] == "ignored_unknown"
        assert wrong_channel_delete["deleted"] is False
        unknown_delete_replay = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000999",
            channel_id="800106845237805067",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, tzinfo=timezone.utc),
        )
        unknown_delete_later = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000999",
            channel_id="800106845237805067",
            observed_at=datetime(2026, 8, 1, 13, 30, 3, tzinfo=timezone.utc),
        )
        rejected_older_unknown_resurrection = store.upsert_discord_signal_messages(
            ({**row, "message_id": "1533100000000000999"},),
            source_event="message_create",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, 500_000, tzinfo=timezone.utc),
        )
        rejected_unknown_resurrection = store.upsert_discord_signal_messages(
            ({**row, "message_id": "1533100000000000999"},),
            source_event="message_create",
            observed_at=datetime(2026, 8, 1, 13, 30, 3, tzinfo=timezone.utc),
        )
        rejected_newer_snapshot_resurrection = store.upsert_discord_signal_messages(
            ({**row, "message_id": "1533100000000000999"},),
            source_event="snapshot",
            observed_at=datetime(2026, 8, 1, 13, 30, 4, tzinfo=timezone.utc),
        )
        assert unknown_delete_replay["outcome"] == "replayed"
        assert unknown_delete_later["outcome"] == "applied"
        assert rejected_older_unknown_resurrection["1533100000000000999"]["outcome"] == "rejected"
        assert rejected_unknown_resurrection["1533100000000000999"]["outcome"] == ("rejected")
        assert rejected_unknown_resurrection["1533100000000000999"]["deleted"] is True
        assert rejected_newer_snapshot_resurrection["1533100000000000999"]["outcome"] == "rejected"
        assert rejected_newer_snapshot_resurrection["1533100000000000999"]["deleted"] is True
        active = store.read_discord_signal_messages(
            channel_ids=("800106845237805067",),
            author_id="795769152258310174",
            published_since=datetime(2026, 8, 1, tzinfo=timezone.utc),
            limit=100,
        )
        assert [message["content"] for message in active] == ["  7370c @ 13.3  "]

        deleted = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000000",
            channel_id="800106845237805067",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, tzinfo=timezone.utc),
        )
        replayed_delete = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000000",
            channel_id="800106845237805067",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, tzinfo=timezone.utc),
        )
        assert deleted["outcome"] == "applied"
        assert deleted["deleted"] is True
        assert replayed_delete["outcome"] == "replayed"
        assert replayed_delete["deleted"] is True
        assert (
            store.read_discord_signal_messages(
                channel_ids=("800106845237805067",),
                author_id="795769152258310174",
                published_since=datetime(2026, 8, 1, tzinfo=timezone.utc),
                limit=100,
            )
            == []
        )

        rejected_resurrection = store.upsert_discord_signal_messages(
            ({**row, "content": "stale resurrection"},),
            source_event="message_update",
            observed_at=datetime(2026, 8, 1, 13, 30, 1, 500_000, tzinfo=timezone.utc),
        )
        assert rejected_resurrection[row["message_id"]]["outcome"] == "rejected"
        assert rejected_resurrection[row["message_id"]]["deleted"] is True

        resurrected = store.upsert_discord_signal_messages(
            ({**row, "content": "7370c @ 14.0"},),
            source_event="message_update",
            observed_at=datetime(2026, 8, 1, 13, 30, 3, tzinfo=timezone.utc),
        )
        rejected_delete = store.mark_discord_signal_message_deleted(
            message_id="1533100000000000000",
            channel_id="800106845237805067",
            observed_at=datetime(2026, 8, 1, 13, 30, 2, 500_000, tzinfo=timezone.utc),
        )
        assert resurrected[row["message_id"]]["outcome"] == "applied"
        assert rejected_delete["outcome"] == "rejected"
        assert rejected_delete["message"]["content"] == "7370c @ 14.0"
        with psycopg.connect(schema_dsn) as conn:
            stored = conn.execute(
                """
                SELECT content, source_event, deleted_at
                FROM discord_signal_messages
                WHERE message_id = '1533100000000000000'
                """
            ).fetchone()
            unknown_tombstone = conn.execute(
                """
                SELECT author_id,
                       author_name,
                       published_at,
                       content,
                       reply_to_message_id,
                       source_event,
                       source_event_at = deleted_at
                FROM discord_signal_messages
                WHERE message_id = '1533100000000000999'
                """
            ).fetchone()
        assert stored == ("7370c @ 14.0", "message_update", None)
        assert unknown_tombstone == (
            None,
            None,
            None,
            None,
            None,
            "message_delete",
            True,
        )
    finally:
        store.close()
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_settings_mutations_are_ordered_by_client_intent_not_arrival() -> None:
    import psycopg

    schema = f"test_settings_order_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    schema_dsn = _postgres_schema_dsn(schema)
    store = _test_postgres_store(schema_dsn)
    key = "aef:motionPreference"
    older_writer = str(uuid4())
    newer_writer = str(uuid4())
    older_order = (1_800_000_000_000, 7)
    newer_order = (1_800_000_000_100, 1)
    latest_order = (1_800_000_000_200, 8)
    try:
        store.initialize()

        first_results, first_revision = store.upsert_settings(
            "client",
            {key: "reduced"},
            mutation_writer_id=newer_writer,
            mutation_orders={key: newer_order},
        )
        assert first_results[key]["outcome"] == "applied"
        assert first_revision == 1
        older_results, older_revision = store.upsert_settings(
            "client",
            {key: "system"},
            mutation_writer_id=older_writer,
            mutation_orders={key: older_order},
        )
        assert older_results[key]["outcome"] == "rejected"
        assert older_revision == first_revision
        replay_results, replay_revision = store.upsert_settings(
            "client",
            {key: "reduced"},
            mutation_writer_id=newer_writer,
            mutation_orders={key: newer_order},
        )
        assert replay_results[key]["outcome"] == "replayed"
        assert replay_revision == first_revision
        with pytest.raises(RuntimeError, match="SETTINGS_MUTATION_REPLAY_MISMATCH"):
            store.upsert_settings(
                "client",
                {key: "system"},
                mutation_writer_id=newer_writer,
                mutation_orders={key: newer_order},
            )
        latest_results, latest_revision = store.upsert_settings(
            "client",
            {key: "off"},
            mutation_writer_id=older_writer,
            mutation_orders={key: latest_order},
        )
        assert latest_results[key]["outcome"] == "applied"
        assert latest_revision == first_revision + 1
        normalized_results, normalized_revision = store.upsert_settings(
            "client",
            {key: "off"},
        )
        assert normalized_results[key]["outcome"] == "applied"
        assert normalized_revision == latest_revision + 1
        anonymous_replay_results, anonymous_replay_revision = store.upsert_settings(
            "client",
            {key: "off"},
        )
        assert anonymous_replay_results[key]["outcome"] == "replayed"
        assert anonymous_replay_revision == normalized_revision
        anonymous_change_results, anonymous_change_revision = store.upsert_settings(
            "client",
            {key: "system"},
        )
        assert anonymous_change_results[key]["outcome"] == "applied"
        assert anonymous_change_revision == normalized_revision + 1

        with psycopg.connect(schema_dsn) as conn:
            row = conn.execute(
                """
                SELECT value,
                       mutation_changed_at_ms,
                       mutation_writer_id::text,
                       mutation_sequence
                FROM settings
                WHERE scope = 'client' AND key = %s
                """,
                (key,),
            ).fetchone()
        assert row == (
            "system",
            0,
            None,
            0,
        )
        with psycopg.connect(schema_dsn) as conn:
            assert conn.execute(
                "SELECT revision FROM settings_state WHERE id = true"
            ).fetchone() == (anonymous_change_revision,)
    finally:
        store.close()
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_exact_previous_schema_version_fails_without_runtime_conversion() -> None:
    import psycopg

    schema = f"test_storage_old_version_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(
            f"""
            CREATE TABLE {schema}.storage_schema_versions (
                version integer PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(f"INSERT INTO {schema}.storage_schema_versions (version) VALUES (15)")
        conn.execute(
            f"CREATE TABLE {schema}.operator_data (id integer PRIMARY KEY, payload text NOT NULL)"
        )
        conn.execute(f"INSERT INTO {schema}.operator_data (id, payload) VALUES (1, 'preserve-me')")

    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        with pytest.raises(RuntimeError, match="STORAGE_SCHEMA_VERSION_REQUIRED"):
            store.initialize()

        with psycopg.connect(_postgres_schema_dsn(schema)) as conn:
            assert conn.execute(
                "SELECT version FROM storage_schema_versions ORDER BY version"
            ).fetchall() == [(15,)]
            assert conn.execute("SELECT id, payload FROM operator_data").fetchall() == [
                (1, "preserve-me")
            ]
            assert conn.execute("SELECT to_regclass('bars')").fetchone()[0] is None
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_route_and_index_drift_fail_without_repair_or_data_loss() -> None:
    import psycopg

    schema = f"test_storage_route_drift_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    schema_dsn = _postgres_schema_dsn(schema)
    stores: list[PostgresStore] = []
    try:
        initial = _test_postgres_store(schema_dsn)
        stores.append(initial)
        initial.initialize()
        marker_ts = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
        with psycopg.connect(schema_dsn) as conn:
            conn.execute(
                """
                INSERT INTO bars (
                    instrument_id, route_fingerprint, provider, symbol, timeframe, ts,
                    open, high, low, close, volume, source, closed
                )
                VALUES (
                    'ibkr|contract|12345', 'opaque-route-token', 'ibkr', 'PLTR', '5m', %s,
                    1, 1, 1, 1, 1, 'ibkr:test', true
                )
                """,
                (marker_ts,),
            )
            conn.execute("ALTER TABLE bars DROP CONSTRAINT bars_route_identity_chk")
            conn.execute("DROP INDEX bars_hygiene_recent_idx")
            conn.execute(
                """
                CREATE INDEX bars_hygiene_recent_idx
                ON bars (updated_at, instrument_id, route_fingerprint, timeframe, source)
                WHERE closed = true
                """
            )

        drifted = _test_postgres_store(schema_dsn)
        stores.append(drifted)
        with pytest.raises(RuntimeError, match="ROUTE_IDENTITY_SCHEMA_CONTRACT_REQUIRED"):
            drifted.initialize()

        with psycopg.connect(schema_dsn) as conn:
            assert conn.execute(
                "SELECT instrument_id, route_fingerprint, ts FROM bars"
            ).fetchall() == [("ibkr|contract|12345", "opaque-route-token", marker_ts)]
            assert (
                conn.execute(
                    """
                SELECT count(*)
                FROM pg_constraint
                WHERE conrelid = to_regclass('bars')
                  AND conname = 'bars_route_identity_chk'
                """
                ).fetchone()[0]
                == 0
            )
            predicate = conn.execute(
                """
                SELECT pg_get_expr(index_row.indpred, index_row.indrelid)
                FROM pg_index AS index_row
                WHERE index_row.indexrelid = to_regclass('bars_hygiene_recent_idx')
                """
            ).fetchone()[0]
            assert "closed = true" in str(predicate)
            assert conn.execute(
                "SELECT version FROM storage_schema_versions ORDER BY version"
            ).fetchall() == [(18,)]
    finally:
        for store in stores:
            try:
                store._pool.close()
            except Exception:
                pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_identical_closed_futures_upserts_do_not_mutate() -> None:
    import psycopg

    schema = f"test_futures_idempotent_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        store.initialize()
        instrument = ibkr_future_payload("ES", con_id=111, local_symbol="ESU6")
        instrument_id = qualified_instrument_id(instrument)
        fingerprint = route_fingerprint(instrument)
        bar = Bar(
            symbol="ES",
            ts=datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc),
            open=6300.0,
            high=6302.0,
            low=6298.0,
            close=6301.0,
            volume=125.0,
            timeframe="5m",
            source="ibkr:historical:TRADES:contract=CONTFUT:route=test",
            closed=True,
            provenance=BarProvenance(
                provider="ibkr",
                instrument_id=instrument_id,
                route_fingerprint=fingerprint,
                request_type="historical",
                provider_contract_id="11004968",
                provider_contract_type="CONTFUT",
                data_type="TRADES",
            ),
        )
        stale_slots = store.read_bar_slots(
            timeframe="5m",
            provider="ibkr",
            start=bar.ts,
            end=bar.ts,
            step=5,
            instrument=instrument,
        )
        assert bar.ts not in stale_slots

        first_receipt = store.write_futures_canonical_bars(
            [bar],
            provider="ibkr",
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            series_type="provider_native",
            roll_policy="provider_managed",
            metadata={
                "provider_contract_id": "11004968",
                "provider_contract_type": "CONTFUT",
                "data_type": "TRADES",
                "series_authority": "provider",
            },
        )
        assert first_receipt.written == 1
        assert first_receipt.acknowledged == (bar,)
        refreshed_slots = store.read_bar_slots(
            timeframe="5m",
            provider="ibkr",
            start=bar.ts,
            end=bar.ts,
            step=5,
            instrument=instrument,
        )
        assert refreshed_slots[bar.ts] == 0
        second_receipt = store.write_futures_canonical_bars(
            [bar],
            provider="ibkr",
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            series_type="provider_native",
            roll_policy="provider_managed",
            metadata={
                "provider_contract_id": "11004968",
                "provider_contract_type": "CONTFUT",
                "data_type": "TRADES",
                "series_authority": "provider",
            },
        )
        assert second_receipt.written == 0
        assert second_receipt.acknowledged == (bar,)
        assert bar.provenance is not None
        contract_bar = replace(
            bar,
            provenance=replace(
                bar.provenance,
                provider_contract_type="FUT",
            ),
        )
        first_contract_receipt = store.write_futures_contract_bars(
            [contract_bar],
            provider="ibkr",
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            contract_key="ESU6",
            provider_contract_id="11004968",
            provider_contract_type="FUT",
            data_type="TRADES",
        )
        assert first_contract_receipt.written == 1
        assert first_contract_receipt.acknowledged == (contract_bar,)
        second_contract_receipt = store.write_futures_contract_bars(
            [contract_bar],
            provider="ibkr",
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            contract_key="ESU6",
            provider_contract_id="11004968",
            provider_contract_type="FUT",
            data_type="TRADES",
        )
        assert second_contract_receipt.written == 0
        assert second_contract_receipt.acknowledged == (contract_bar,)
        replay_contract_receipt = store.write_futures_contract_bars(
            [contract_bar],
            provider="ibkr",
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            contract_key="ESU6",
            provider_contract_id="11004968",
            provider_contract_type="FUT",
            data_type="TRADES",
            revision_sequence=second_contract_receipt.revision_sequence,
        )
        assert replay_contract_receipt.written == 0
        assert replay_contract_receipt.acknowledged == (contract_bar,)
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_reorder_swaps_rows_under_unique_watchlist_order() -> None:
    import psycopg

    schema = f"test_instruments_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        store.initialize()
        es_item = ibkr_future_payload("ES")
        spy_item = ibkr_stock_payload("SPY", con_id=756733)
        store.select_watchlist_instrument(
            es_item,
            current_futures_contract=current_futures_contract_record(es_item),
        )
        store.select_watchlist_instrument(spy_item)
        _rows, version = store.read_watchlist_snapshot()
        ordered, next_version = store.reorder_watchlist(
            [spy_item["instrument_id"], es_item["instrument_id"]],
            expected_version=version,
        )

        assert [row["key"] for row in ordered] == ["SPY", "ES"]
        assert next_version == version + 1
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_watchlist_presentation_binds_typed_jsonb_value() -> None:
    import psycopg

    schema = f"test_watchlist_presentation_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        store.initialize()
        spy = ibkr_stock_payload("SPY", con_id=756733)
        store.select_watchlist_instrument(spy)

        first = store.set_watchlist_presentation(spy["instrument_id"], "trend")
        second = store.set_watchlist_presentation(spy["instrument_id"], "classic")
        _values, _orders, _watermark, revision_before_replay = store.read_settings_snapshot(
            WATCHLIST_PRESENTATION_SCOPE
        )
        replayed = store.set_watchlist_presentation(spy["instrument_id"], "classic")
        _values, _orders, _watermark, revision_after_replay = store.read_settings_snapshot(
            WATCHLIST_PRESENTATION_SCOPE
        )
        rows, _version = store.read_watchlist_snapshot()

        assert first == {"display_mode": "trend", "revision": 1}
        assert second == {"display_mode": "classic", "revision": 2}
        assert replayed == second
        assert revision_after_replay == revision_before_replay
        assert rows[0]["presentation"] == second
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_unversioned_nonempty_schema_fails_without_mutation() -> None:
    import psycopg

    schema = f"test_instruments_old_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(
            f"""
            CREATE TABLE {schema}.instruments (
                instrument_key text PRIMARY KEY,
                display text NOT NULL DEFAULT '',
                provider_symbols jsonb NOT NULL DEFAULT '{{}}'::jsonb
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO {schema}.instruments (instrument_key, display, provider_symbols)
            VALUES ('SPY', 'SPY', '{{"ibkr":"756733"}}'::jsonb)
            """
        )
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        with pytest.raises(RuntimeError, match="STORAGE_SCHEMA_UNVERSIONED_NONEMPTY"):
            store.initialize()
        with psycopg.connect(_postgres_schema_dsn(schema)) as conn:
            assert conn.execute(
                "SELECT instrument_key, display, provider_symbols FROM instruments"
            ).fetchall() == [("SPY", "SPY", {"ibkr": "756733"})]
            assert (
                conn.execute("SELECT to_regclass('storage_schema_versions')").fetchone()[0] is None
            )
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_rejects_contract_identity_fields_for_futures_root() -> None:
    import psycopg

    schema = f"test_instruments_future_chk_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        store.initialize()
        with psycopg.connect(_postgres_schema_dsn(schema)) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    """
                    INSERT INTO instruments (
                        instrument_id, instrument_key, provider, provider_symbol, provider_contract_id,
                        con_id, display_name, asset_class, identity_scope
                    )
                    VALUES (
                        'ibkr|future_root|ES|CME|USD|', 'ES', 'ibkr', 'ESU6',
                        '12345', 12345, 'ESU6', 'future', 'root'
                    )
                    """
                )
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_trading_sessions_are_contract_aware_and_preserve_history() -> None:
    import psycopg

    schema = f"test_trading_sessions_contract_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL instrument storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    try:
        store.initialize()
        instrument = ibkr_future_payload(
            "ES",
            con_id=111,
            local_symbol="ESU6",
        )
        store.select_watchlist_instrument(
            instrument,
            current_futures_contract=current_futures_contract_record(instrument),
        )
        instrument_id = instrument["instrument_id"]
        store.write_futures_contract(
            {
                "provider": "ibkr",
                "instrument_id": instrument_id,
                "contract_key": "ESZ6",
                "provider_contract_id": "222",
                "root": "ES",
                "local_symbol": "ESZ6",
                "expiry": "20261218",
                "contract_month": "202612",
                "con_id": 222,
                "is_current": True,
            },
            route_transition=FuturesRouteTransition(
                provider="ibkr",
                instrument_id=instrument_id,
                previous_route_fingerprint=route_fingerprint(
                    ibkr_future_payload(
                        "ES",
                        con_id=111,
                        local_symbol="ESU6",
                    )
                ),
                next_route_fingerprint=route_fingerprint(
                    ibkr_future_payload(
                        "ES",
                        con_id=222,
                        local_symbol="ESZ6",
                    )
                ),
                previous_session_contract_id="111",
                next_session_contract_id="222",
            ),
        )
        current_contract = store.read_current_futures_contract(
            provider="ibkr", instrument_id=instrument_id
        )
        assert current_contract is not None
        assert current_contract["contract_key"] == "ESZ6"
        assert current_contract["con_id"] == 222
        assert current_contract["source"] == "provider_current_contract"
        store.write_futures_roll_event(
            {
                "provider": "ibkr",
                "instrument_id": instrument_id,
                "roll_ts": datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc),
                "from_contract_key": "ESU6",
                "to_contract_key": "ESZ6",
                "roll_policy": "provider_current_contract",
            }
        )
        first_route = {
            **ibkr_future_payload("ES", con_id=111, local_symbol="ESU6"),
            "instrument_id": instrument_id,
        }
        second_route = {
            **ibkr_future_payload("ES", con_id=222, local_symbol="ESZ6"),
            "instrument_id": instrument_id,
        }

        def schedule_payload(
            contract_id: int,
            local_symbol: str,
            session_date: str,
            opens_at: str,
            closes_at: str,
        ) -> dict[str, object]:
            return {
                "symbol": "ES",
                "con_id": contract_id,
                "local_symbol": local_symbol,
                "schedule_format": "provider_declared_intervals",
                "schedule_coverage_start": opens_at,
                "schedule_coverage_end": closes_at,
                "trading_intervals": [
                    {
                        "session_date": session_date,
                        "session_type": session_type,
                        "opens_at": opens_at,
                        "closes_at": closes_at,
                        "status": "open",
                    }
                    for session_type in ("trading", "liquid")
                ],
            }

        store.upsert_trading_hours(
            "America/Chicago",
            "20260909:1700-20260910:1600",
            "20260909:1700-20260910:1600",
            schedule_payload(
                111,
                "ESU6",
                "2026-09-09",
                "2026-09-09T22:00:00+00:00",
                "2026-09-10T21:00:00+00:00",
            ),
            instrument=first_route,
        )
        store.upsert_trading_hours(
            "America/Chicago",
            "20260910:1700-20260911:1600",
            "20260910:1700-20260911:1600",
            schedule_payload(
                222,
                "ESZ6",
                "2026-09-10",
                "2026-09-10T22:00:00+00:00",
                "2026-09-11T21:00:00+00:00",
            ),
            instrument=second_route,
        )
        store.upsert_trading_hours(
            "America/Chicago",
            "20260911:1700-20260912:1600",
            "20260911:1700-20260912:1600",
            schedule_payload(
                222,
                "ESZ6",
                "2026-09-11",
                "2026-09-11T22:00:00+00:00",
                "2026-09-12T21:00:00+00:00",
            ),
            instrument=second_route,
        )

        assert (
            store.read_trading_session_open_state(
                datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc),
                "trading",
                instrument=first_route,
            )
            is True
        )
        assert (
            store.read_trading_session_open_state(
                datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc),
                "trading",
                instrument=second_route,
            )
            is True
        )
        assert (
            store.read_trading_session_open_state(
                datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc),
                "trading",
                instrument=second_route,
            )
            is True
        )
        with psycopg.connect(_postgres_schema_dsn(schema)) as conn:
            rows = conn.execute(
                """
                SELECT route_fingerprint, count(*)
                FROM trading_session_intervals
                WHERE instrument_id = %s
                GROUP BY route_fingerprint
                ORDER BY route_fingerprint
                """,
                (instrument_id,),
            ).fetchall()
            snapshot_count = conn.execute(
                "SELECT count(*) FROM trading_schedule_snapshots WHERE instrument_id = %s",
                (instrument_id,),
            ).fetchone()[0]
            interval_dates = {
                row[0].isoformat()
                for row in conn.execute(
                    """
                    SELECT DISTINCT session_date
                    FROM trading_session_intervals
                    WHERE instrument_id = %s
                    """,
                    (instrument_id,),
                ).fetchall()
            }
        assert rows == [(route_fingerprint(first_route), 2), (route_fingerprint(second_route), 4)]
        assert snapshot_count == 3
        assert interval_dates == {"2026-09-09", "2026-09-10", "2026-09-11"}
        instrument = store.read_watchlist_snapshot()[0][0]
        assert instrument["contract_identity"]["current_contract"]["contract_key"] == "ESZ6"
        assert instrument["session"]["provider_contract_id"] == "222"
        assert len(instrument["session"]["trading_intervals"]) == 2
        assert {
            interval["session_date"] for interval in instrument["session"]["trading_intervals"]
        } == {"2026-09-10", "2026-09-11"}
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_postgres_current_futures_rollover_rebinds_month_old_drawing_exactly() -> None:
    import psycopg

    schema = f"test_futures_drawing_rollover_{uuid4().hex}"
    base_dsn = os.environ.get("AEF_DATABASE_URL", "").strip()
    if not base_dsn:
        pytest.skip("AEF_DATABASE_URL is required for PostgreSQL storage contract tests")
    with psycopg.connect(base_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    store = _test_postgres_store(_postgres_schema_dsn(schema))
    previous_instrument = ibkr_future_payload(
        "ES",
        con_id=111,
        local_symbol="ESU6",
    )
    next_instrument = ibkr_future_payload(
        "ES",
        con_id=222,
        local_symbol="ESZ6",
    )
    instrument_id = qualified_instrument_id(previous_instrument)
    previous_route_fingerprint = route_fingerprint(previous_instrument)
    next_route_fingerprint = route_fingerprint(next_instrument)
    drawing_id = f"month-old-{uuid4().hex}"
    month_old_updated_at = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    try:
        store.initialize()
        store.select_watchlist_instrument(
            previous_instrument,
            current_futures_contract=current_futures_contract_record(previous_instrument),
        )
        store.replace_drawings(
            instrument_id,
            "ES",
            "5m",
            [
                {
                    "id": drawing_id,
                    "symbol": "ES",
                    "type": "line",
                    "points": [
                        {"ts": "2026-04-01T14:30:00+00:00", "price": 5000.0},
                        {"ts": "2026-04-02T14:30:00+00:00", "price": 5012.5},
                    ],
                }
            ],
            route_fingerprint=previous_route_fingerprint,
            provider="ibkr",
            provider_contract_id="111",
        )
        with psycopg.connect(_postgres_schema_dsn(schema)) as conn:
            conn.execute(
                "UPDATE drawings SET updated_at = %s WHERE id = %s",
                (month_old_updated_at, drawing_id),
            )
            before = conn.execute(
                """
                SELECT id, instrument_id, route_fingerprint, timeframe, payload, updated_at
                FROM drawings
                WHERE id = %s
                """,
                (drawing_id,),
            ).fetchone()
        assert before is not None

        store.write_futures_contract(
            {
                "provider": "ibkr",
                "instrument_id": instrument_id,
                "contract_key": "ESZ6",
                "provider_contract_id": "222",
                "root": "ES",
                "exchange": "CME",
                "currency": "USD",
                "local_symbol": "ESZ6",
                "expiry": "20261218",
                "contract_month": "202612",
                "con_id": 222,
                "is_current": True,
            },
            route_transition=FuturesRouteTransition(
                provider="ibkr",
                instrument_id=instrument_id,
                previous_route_fingerprint=previous_route_fingerprint,
                next_route_fingerprint=next_route_fingerprint,
                previous_session_contract_id="111",
                next_session_contract_id="222",
            ),
        )

        with psycopg.connect(_postgres_schema_dsn(schema)) as conn:
            after = conn.execute(
                """
                SELECT id, instrument_id, route_fingerprint, timeframe, payload, updated_at
                FROM drawings
                WHERE id = %s
                """,
                (drawing_id,),
            ).fetchone()
            stale_scope_count = conn.execute(
                """
                SELECT count(*)
                FROM drawings
                WHERE instrument_id = %s AND route_fingerprint = %s
                """,
                (instrument_id, previous_route_fingerprint),
            ).fetchone()[0]
        assert after is not None
        assert after[:2] == (drawing_id, instrument_id)
        assert after[2] == next_route_fingerprint
        assert after[3] == "5m"
        assert after[4] == {
            **before[4],
            "route_fingerprint": next_route_fingerprint,
            "provider_contract_id": "222",
        }
        assert after[4]["provider"] == "ibkr"
        assert after[5] == month_old_updated_at
        assert stale_scope_count == 0
    finally:
        try:
            store._pool.close()
        except Exception:
            pass
        with psycopg.connect(base_dsn) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
