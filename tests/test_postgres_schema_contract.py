from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aef_terminal.data import providers as provider_registry
from aef_terminal.paper_contract import PaperContractIdentity
from aef_terminal.storage.postgres import PostgresStore
from aef_terminal.storage.repos import schema as schema_repo


def test_non_schema_owner_never_runs_schema_ddl() -> None:
    store = PostgresStore(None, schema_owner=False)

    store.initialize()

    assert store._initialized is True


def test_schema_initialization_bootstraps_only_canonical_v18() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    versioned_verifier = inspect.getsource(schema_repo._verify_versioned_storage_runtime_contract)
    verifier = inspect.getsource(schema_repo._verify_storage_runtime_contract)

    assert schema_repo._STORAGE_SCHEMA_VERSION == 18
    assert "pg_advisory_xact_lock" in source
    assert "SELECT to_regclass('storage_schema_versions')" in source
    assert "stored_versions != (_STORAGE_SCHEMA_VERSION,)" in versioned_verifier
    assert "STORAGE_SCHEMA_VERSION_REQUIRED" in versioned_verifier
    assert "STORAGE_SCHEMA_UNVERSIONED_NONEMPTY" in source
    assert source.index("STORAGE_SCHEMA_UNVERSIONED_NONEMPTY") < source.index(
        "CREATE EXTENSION IF NOT EXISTS timescaledb"
    )
    assert source.count("INSERT INTO storage_schema_versions (version)") == 1
    assert "ON CONFLICT (version)" not in source
    assert "operator-controlled offline storage transition" in versioned_verifier
    assert "operator-controlled storage recovery required" in source
    assert "_verify_versioned_storage_runtime_contract(cur)" in source
    assert "_verify_storage_runtime_contract(cur)" in versioned_verifier
    assert "_verify_history_coverage_storage(cur)" in verifier
    assert "_verify_drawing_payload_storage(cur)" in verifier
    assert "TIMESCALEDB_EXTENSION_REQUIRED" in verifier
    assert "PG_STAT_STATEMENTS_EXTENSION_REQUIRED" in verifier
    assert "TIMESCALEDB_HYPERTABLES_REQUIRED" in verifier
    assert "TIMESCALEDB_TICK_ROWSTORE_REQUIRED" in verifier
    assert "TIMESCALEDB_TICK_COLUMNSTORE_POLICY_FORBIDDEN" in verifier
    assert "TIMESCALEDB_COMPRESSED_TICK_CHUNKS_FORBIDDEN" in verifier
    assert "CANONICAL_BAR_REVISION_COLUMNS_REQUIRED" in verifier
    assert "CANONICAL_PROVIDER_STORAGE_REQUIRED" in verifier
    assert "SETTINGS_SCOPE_CHECK_REQUIRED" in verifier
    assert "SETTINGS_MUTATION_ORDER_CHECK_REQUIRED" in verifier
    assert "SETTINGS_MUTATION_ORDER_COLUMNS_REQUIRED" in verifier
    assert "SETTINGS_MUTATION_ORDER_STORAGE_REQUIRED" in verifier
    assert "SETTINGS_STATE_COLUMNS_REQUIRED" in verifier
    assert "SETTINGS_STATE_PRIMARY_KEY_REQUIRED" in verifier
    assert "SETTINGS_STATE_CONTRACT_REQUIRED" in verifier
    assert "SETTINGS_STATE_ROW_REQUIRED" in verifier
    assert "CREATE TABLE settings_state" in source
    assert "CREATE TABLE discord_signal_messages" in source
    assert "DISCORD_SIGNAL_STORAGE_COLUMNS_REQUIRED" in inspect.getsource(
        schema_repo._verify_discord_signal_storage
    )
    assert "_verify_paper_order_protection_storage(cur)" in verifier
    assert "PAPER_ORDER_STORAGE_PROTECTION_CONTRACT_REQUIRED" in inspect.getsource(
        schema_repo._verify_paper_order_protection_storage
    )
    assert "STORAGE_SERVER_SETTINGS_CONTRACT_REQUIRED" in verifier
    assert "current_instrument_ids" in verifier
    assert "identity not in current_instrument_ids" in verifier
    assert "_migrate_" not in source
    assert "DROP TABLE" not in source
    assert "DROP INDEX" not in source
    assert "migrate_data" not in source
    assert "add_compression_policy('ticks'" not in source
    assert (
        "timescaledb.compress_segmentby = 'provider,instrument_id,route_fingerprint'" not in source
    )
    assert not any(name.startswith("_migrate_") for name in vars(schema_repo))
    application_ddl = source.replace("CREATE EXTENSION IF NOT EXISTS timescaledb", "").replace(
        "CREATE EXTENSION IF NOT EXISTS pg_stat_statements", ""
    )
    assert "IF NOT EXISTS" not in application_ddl
    assert "if_not_exists" not in application_ddl
    assert "ON CONFLICT" not in application_ddl
    for forbidden_index in schema_repo._FORBIDDEN_STORAGE_INDEXES:
        assert forbidden_index not in source


def test_runtime_contract_rejects_legacy_durable_drawing_payloads() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "drawing-1",
            "instrument-1",
            "route-1",
            "ES",
            "5m",
            {
                "id": "drawing-1",
                "instrument_id": "instrument-1",
                "route_fingerprint": "route-1",
                "symbol": "ES",
                "provider": "ibkr",
                "provider_contract_id": "contract-1",
                "type": "line",
                "points": [
                    {
                        "ts": "2026-08-01T12:00:00+00:00",
                        "price": 100.0,
                        "barSlot": 10,
                    },
                    {"ts": "2026-08-01T12:05:00+00:00", "price": 101.0},
                ],
            },
            "contract",
            "stock",
            None,
        )
    ]

    with pytest.raises(
        RuntimeError,
        match="DRAWING_PAYLOAD_STORAGE_CONTRACT_REQUIRED.*DRAWING_FIELD_FORBIDDEN: barSlot",
    ):
        schema_repo._verify_drawing_payload_storage(cursor)

    query = " ".join(cursor.execute.call_args.args[0].split())
    assert "FROM drawings AS drawing" in query
    assert "LEFT JOIN instruments AS instrument" in query
    assert "LEFT JOIN LATERAL" in query
    assert query.endswith("ORDER BY drawing.id")


def test_runtime_contract_rejects_stale_futures_drawing_routes() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "drawing-1",
            "instrument-1",
            "route-old",
            "GC",
            "5m",
            {
                "id": "drawing-1",
                "instrument_id": "instrument-1",
                "route_fingerprint": "route-old",
                "symbol": "GC",
                "provider": "ibkr",
                "provider_contract_id": "contract-old",
                "type": "line",
                "points": [
                    {"ts": "2026-08-01T12:00:00+00:00", "price": 100.0},
                    {"ts": "2026-08-01T12:05:00+00:00", "price": 101.0},
                ],
            },
            "root",
            "future",
            "contract-current",
        )
    ]

    with pytest.raises(
        RuntimeError,
        match="DRAWING_PAYLOAD_STORAGE_CONTRACT_REQUIRED.*DRAWING_FUTURES_ROUTE_STALE",
    ):
        schema_repo._verify_drawing_payload_storage(cursor)


def test_operator_docs_name_the_canonical_storage_version() -> None:
    version = schema_repo._STORAGE_SCHEMA_VERSION
    agents = Path("AGENTS.md").read_text(encoding="utf-8")
    architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert f"canonical schema v{version}" in agents
    assert f"canonical application schema v{version}" in architecture
    assert f"canonical storage schema v{version}" in readme
    assert f"non-v{version} MartinCall database" in readme


def test_v18_bootstrap_owns_exact_history_coverage_contract() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    verifier = inspect.getsource(schema_repo._verify_history_coverage_storage)
    table = source.split("CREATE TABLE history_coverage", 1)[1].split(
        "CREATE TABLE trading_hours",
        1,
    )[0]

    for column in (
        "receipt_contract_version integer NOT NULL",
        "request_contract_version integer NOT NULL",
        "admission_contract_version integer NOT NULL",
        "request_mode text NOT NULL",
        "request_type text NOT NULL",
        "provider_source text NOT NULL",
        "provider_contract_id text NOT NULL",
        "provider_contract_type text NOT NULL",
        "data_type text NOT NULL",
        "provider_limit bigint",
        "response_count bigint NOT NULL",
        "completed_at timestamptz NOT NULL",
    ):
        assert column in table
    assert "terminal_state" not in table
    assert "pages_exhausted" not in table
    assert "history_coverage_lookup_idx" in table
    primary_key = table.split("PRIMARY KEY (", 1)[1].split(")", 1)[0]
    assert "completed_at" not in primary_key
    assert "response_count" not in primary_key
    assert "provider_limit" not in primary_key
    assert "HISTORY_COVERAGE_STORAGE_COLUMNS_REQUIRED" in verifier
    assert "HISTORY_COVERAGE_STORAGE_CONTRACT_REQUIRED" in verifier


def test_v18_futures_contract_bars_keep_route_and_revision_authority() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    table = source.split("CREATE TABLE futures_contract_bars", 1)[1].split(
        "CREATE TABLE futures_continuous_bars",
        1,
    )[0]

    assert "route_fingerprint text NOT NULL" in table
    assert "revision_sequence bigint NOT NULL DEFAULT 0" in table
    assert "futures_contract_bars_route_identity_chk" in table
    assert "provider, instrument_id, route_fingerprint," in table
    assert "futures_contract_bars_route_tf_ts_idx" in table
    assert "futures_contract_bars_instrument_tf_ts_idx" not in table


def test_settings_scope_schema_contract_has_one_canonical_allowlist() -> None:
    from aef_terminal import settings_contract

    assert schema_repo._SETTINGS_SCOPE_VALUES_SQL == ", ".join(
        f"'{scope}'" for scope in settings_contract.CURRENT_SETTINGS_SCOPE_ORDER
    )
    assert schema_repo._SETTINGS_SCOPE_CHECK_DEFINITION == (
        "CHECK ((scope = ANY (ARRAY["
        + ", ".join(f"'{scope}'::text" for scope in settings_contract.CURRENT_SETTINGS_SCOPE_ORDER)
        + "])))"
    )
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    assert 'scope IN ("""' in source
    assert "_SETTINGS_SCOPE_VALUES_SQL" in source


def test_discord_v15_schema_distinguishes_active_facts_from_fact_free_tombstones() -> None:
    bootstrap = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    verifier = inspect.getsource(schema_repo._verify_discord_signal_storage)

    assert "author_id text," in bootstrap
    assert "author_name text," in bootstrap
    assert "published_at timestamptz," in bootstrap
    assert "content text," in bootstrap
    assert "discord_signal_messages_state_chk" in bootstrap
    assert "deleted_at = source_event_at" in bootstrap
    assert "num_nonnulls(" in bootstrap
    assert "_DISCORD_SIGNAL_STATE_CHECK_DEFINITION" in verifier
    assert '("author_id", "text", False, "")' in verifier
    assert '("content", "text", False, "")' in verifier
    assert "AND contype <> 'p'" in verifier
    assert "conname = ANY" not in verifier


def test_storage_provider_schema_contract_uses_only_active_manifests() -> None:
    active_manifest_values = tuple(
        sorted(
            {
                provider_registry.normalize_provider_key(provider)
                for entry in provider_registry.provider_catalog()
                for provider in entry["db_providers"]
            }
        )
    )
    storage_values = active_manifest_values

    assert provider_registry.storage_provider_keys() == active_manifest_values
    assert "moex" not in active_manifest_values
    assert schema_repo._STORAGE_PROVIDER_KEYS == storage_values
    assert schema_repo._STORAGE_PROVIDER_VALUES_SQL == ", ".join(
        f"'{provider}'" for provider in storage_values
    )
    assert schema_repo._CANONICAL_PROVIDER_CHECK_DEFINITION == (
        "CHECK ((provider = ANY (ARRAY["
        + ", ".join(f"'{provider}'::text" for provider in storage_values)
        + "])))"
    )

    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    for table in schema_repo._PROVIDER_STORAGE_TABLES:
        assert f"CONSTRAINT {table}_provider_chk" in source
    assert source.count("_STORAGE_PROVIDER_VALUES_SQL") == (
        len(schema_repo._PROVIDER_STORAGE_TABLES) + 3
    )


def test_storage_provider_manifest_rejects_sql_unsafe_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        provider_registry,
        "_PROVIDERS",
        {
            "unsafe": SimpleNamespace(
                db_providers=("unsafe'value",),
            )
        },
    )

    with pytest.raises(
        ValueError,
        match="STORAGE_PROVIDER_MANIFEST_INVALID",
    ):
        provider_registry.storage_provider_keys()


@pytest.mark.parametrize(
    "db_providers",
    [(), (" IBKR ",), ("IBKR",), ("ibkr", "ibkr")],
)
def test_storage_provider_manifest_rejects_empty_or_noncanonical_values(
    monkeypatch: pytest.MonkeyPatch,
    db_providers: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        provider_registry,
        "_PROVIDERS",
        {"invalid": SimpleNamespace(db_providers=db_providers)},
    )

    with pytest.raises(
        ValueError,
        match="STORAGE_PROVIDER_MANIFEST_(?:REQUIRED|INVALID)",
    ):
        provider_registry.storage_provider_keys()


def test_exact_current_schema_path_is_read_only_verification() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    versioned_path = source.split(
        "if version_table_row is not None and version_table_row[0] is not None:",
        1,
    )[1].split("                    else:", 1)[0]

    assert "_verify_versioned_storage_runtime_contract(cur)" in versioned_path
    for write_token in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE ", "ALTER ", "DROP "):
        assert write_token not in versioned_path


def test_read_only_storage_preflight_reuses_the_exact_runtime_verifier() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.verify_storage_runtime_contract)
    versioned_verifier = inspect.getsource(schema_repo._verify_versioned_storage_runtime_contract)

    assert "SET TRANSACTION READ ONLY" in source
    assert "pg_advisory_xact_lock_shared" in source
    assert "_verify_versioned_storage_runtime_contract(cur)" in source
    assert "SELECT version FROM storage_schema_versions ORDER BY version" in versioned_verifier
    for write_token in ("INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE ", "ALTER ", "DROP "):
        assert write_token not in source
        assert write_token not in versioned_verifier


def test_runtime_verifier_rejects_noncanonical_provider_without_rewriting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = MagicMock()
    expected_constraints = {
        f"{table}_provider_chk": table for table in schema_repo._PROVIDER_STORAGE_TABLES
    }
    cursor.fetchall.return_value = [
        (
            constraint_name,
            table,
            True,
            (
                "CHECK ((provider = 'ibkr:chunk-backfill'::text))"
                if table == "bars"
                else schema_repo._CANONICAL_PROVIDER_CHECK_DEFINITION
            ),
        )
        for constraint_name, table in expected_constraints.items()
    ]
    monkeypatch.setattr(schema_repo, "_verify_route_identity_storage", lambda _cursor: None)
    monkeypatch.setattr(schema_repo, "_verify_discord_signal_storage", lambda _cursor: None)
    monkeypatch.setattr(
        schema_repo,
        "_verify_paper_order_protection_storage",
        lambda _cursor: None,
    )
    monkeypatch.setattr(
        schema_repo,
        "_verify_history_coverage_storage",
        lambda _cursor: None,
    )
    monkeypatch.setattr(
        schema_repo,
        "_verify_paper_contract_storage",
        lambda _cursor: None,
    )
    monkeypatch.setattr(
        schema_repo,
        "_verify_drawing_payload_storage",
        lambda _cursor: None,
    )

    with pytest.raises(RuntimeError, match="CANONICAL_PROVIDER_STORAGE_REQUIRED"):
        schema_repo._verify_storage_runtime_contract(cursor)

    assert cursor.execute.call_count == 1
    assert cursor.execute.call_args.args[1] == (list(expected_constraints),)
    executed = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
    assert "UPDATE" not in executed
    assert "DELETE" not in executed
    assert "INSERT" not in executed


def test_paper_order_startup_verifier_rejects_noncanonical_protection_read_only() -> None:
    protection = {"protection_basis": "absolute_structure"}
    paper_contract = PaperContractIdentity(
        scope_kind="instrument",
        provider="ibkr",
        provider_contract_id="paper",
    ).to_payload()
    command = {
        "provider": "ibkr",
        "provider_contract_id": "paper",
        "paper_contract": paper_contract,
        "use_stop_loss": True,
        "use_target": True,
        "stop_loss": 95.0,
        "target": 110.0,
        "payload": protection,
    }
    payload = {
        "instrument_id": "ibkr|contract|paper",
        "route_fingerprint": "ibkr|route|paper",
        "provider": "ibkr",
        "provider_contract_id": "paper",
        "paper_contract": paper_contract,
        "payload": protection,
        "create_command": command,
        "execution_command": command,
    }
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    row = (
        "paper-order-1",
        "ES",
        "1m",
        "long",
        "market",
        "pending",
        1.0,
        100.0,
        95.0,
        110.0,
        True,
        True,
        now,
        now,
        None,
        None,
        "entry",
        None,
        None,
        None,
        False,
        payload,
    )
    canonical_cursor = MagicMock()
    canonical_cursor.fetchall.return_value = [row]
    schema_repo._verify_paper_order_protection_storage(canonical_cursor)

    invalid_payload = {
        **payload,
        "payload": {"absolute_level": True},
    }
    invalid_cursor = MagicMock()
    invalid_cursor.fetchall.return_value = [(*row[:-1], invalid_payload)]
    with pytest.raises(
        RuntimeError,
        match="PAPER_ORDER_STORAGE_PROTECTION_CONTRACT_REQUIRED",
    ):
        schema_repo._verify_paper_order_protection_storage(invalid_cursor)

    executed = "\n".join(
        str(call.args[0])
        for cursor in (canonical_cursor, invalid_cursor)
        for call in cursor.execute.call_args_list
    )
    assert "SELECT" in executed
    assert all(token not in executed for token in ("UPDATE", "DELETE", "INSERT", "ALTER"))


def test_timescaledb_api_schema_comes_from_extension_metadata() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = ("timescaledb_api",)

    assert schema_repo._timescaledb_api_schema(cursor) == "timescaledb_api"
    assert "extension_row.extnamespace" in cursor.execute.call_args.args[0]


@pytest.mark.parametrize("schema_row", [None, (None,), ("",)])
def test_timescaledb_api_schema_fails_closed_when_unresolved(schema_row: object) -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = schema_row

    with pytest.raises(RuntimeError, match="TIMESCALEDB_API_SCHEMA_REQUIRED"):
        schema_repo._timescaledb_api_schema(cursor)
