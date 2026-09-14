from __future__ import annotations

from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.providers import storage_provider_keys
from aef_terminal.settings_contract import (
    CURRENT_SETTINGS_SCOPE_ORDER,
    CURRENT_SETTINGS_SCOPES,
)


_STORAGE_SCHEMA_VERSION = 18
_DISCORD_SIGNAL_IDENTITY_CHECK_DEFINITION = (
    "CHECK (((message_id ~ '^[0-9]+$'::text) AND "
    "(channel_id ~ '^[0-9]+$'::text) AND "
    "((author_id IS NULL) OR (author_id ~ '^[0-9]+$'::text)) AND "
    "((reply_to_message_id IS NULL) OR "
    "(reply_to_message_id ~ '^[0-9]+$'::text))))"
)
_DISCORD_SIGNAL_CONTENT_CHECK_DEFINITION = "CHECK (((content IS NULL) OR (content <> ''::text)))"
_DISCORD_SIGNAL_SOURCE_EVENT_CHECK_DEFINITION = (
    "CHECK ((source_event = ANY (ARRAY['snapshot'::text, "
    "'message_create'::text, 'message_update'::text, "
    "'message_delete'::text])))"
)
_DISCORD_SIGNAL_STATE_CHECK_DEFINITION = (
    "CHECK ((((source_event = 'message_delete'::text) AND "
    "(deleted_at IS NOT NULL) AND (deleted_at = source_event_at) AND "
    "(num_nonnulls(author_id, author_name, published_at, content, "
    "reply_to_message_id) = 0)) OR ((source_event <> 'message_delete'::text) "
    "AND (deleted_at IS NULL) AND "
    "(num_nonnulls(author_id, author_name, published_at, content) = 4))))"
)
_GEX_SNAPSHOT_SOURCE_CHECK_DEFINITION = (
    "CHECK ((source = ANY (ARRAY['gex:ibkr'::text, 'gex:ibkr-live'::text])))"
)
_INSTRUMENT_PROVIDER_CONTRACT_CHECK_DEFINITION = (
    "CHECK ((((identity_scope = 'root'::text) AND (asset_class = 'future'::text) AND "
    "(provider_contract_id = ''::text) AND (con_id IS NULL)) OR "
    "((identity_scope = 'contract'::text) AND (provider_contract_id <> ''::text))))"
)
_HISTORY_COVERAGE_RANGE_CHECK_DEFINITION = (
    "CHECK (((covered_from < covered_to) AND (covered_to <= completed_at)))"
)
_HISTORY_COVERAGE_VERSION_CHECK_DEFINITION = (
    "CHECK (((receipt_contract_version > 0) AND (request_contract_version > 0) AND "
    "(admission_contract_version > 0)))"
)
_HISTORY_COVERAGE_AUTHORITY_CHECK_DEFINITION = (
    "CHECK (((request_mode <> ''::text) AND (request_type = 'historical'::text) AND "
    "(provider_source <> ''::text) AND (provider_contract_id <> ''::text) AND "
    "(provider_contract_type <> ''::text) AND (data_type = 'TRADES'::text)))"
)
_HISTORY_COVERAGE_COUNT_CHECK_DEFINITION = (
    "CHECK (((response_count >= 0) AND ((provider_limit IS NULL) OR "
    "((provider_limit > 0) AND (response_count < provider_limit)))))"
)
_PAPER_CONTRACT_SCOPE_CHECK_DEFINITION = (
    "CHECK (((contract_scope_key <> ''::text) AND (contract_provider <> ''::text) AND "
    "(contract_provider_contract_id <> ''::text) AND (((contract_scope_kind = "
    "'instrument'::text) AND (contract_sec_type IS NULL) AND "
    "(contract_con_id IS NULL) AND (contract_exchange IS NULL)) OR "
    "((contract_scope_kind = 'option'::text) AND (contract_sec_type = ANY "
    "(ARRAY['OPT'::text, 'FOP'::text])) AND (contract_con_id > 0) AND "
    "(contract_exchange <> ''::text)))))"
)
_STORAGE_PROVIDER_KEYS = storage_provider_keys()
_STORAGE_PROVIDER_VALUES_SQL = ", ".join(f"'{provider}'" for provider in _STORAGE_PROVIDER_KEYS)
_CANONICAL_PROVIDER_CHECK_DEFINITION = (
    "CHECK ((provider = ANY (ARRAY["
    + ", ".join(f"'{provider}'::text" for provider in _STORAGE_PROVIDER_KEYS)
    + "])))"
)
_PAPER_CONTRACT_PROVIDER_CHECK_DEFINITION = (
    "CHECK ((contract_provider = ANY (ARRAY["
    + ", ".join(f"'{provider}'::text" for provider in _STORAGE_PROVIDER_KEYS)
    + "])))"
)
_SETTINGS_SCOPE_VALUES_SQL = ", ".join(f"'{scope}'" for scope in CURRENT_SETTINGS_SCOPE_ORDER)
_SETTINGS_SCOPE_CHECK_DEFINITION = (
    "CHECK ((scope = ANY (ARRAY["
    + ", ".join(f"'{scope}'::text" for scope in CURRENT_SETTINGS_SCOPE_ORDER)
    + "])))"
)
_SETTINGS_MUTATION_ORDER_CHECK_DEFINITION = (
    "CHECK ((((mutation_writer_id IS NULL) AND (mutation_changed_at_ms = 0) AND "
    "(mutation_sequence = 0)) OR ((mutation_writer_id IS NOT NULL) AND "
    "(mutation_changed_at_ms > 0) AND "
    "(mutation_changed_at_ms <= '9007199254740991'::bigint) AND "
    "(mutation_sequence > 0) AND "
    "(mutation_sequence <= '9007199254740991'::bigint))))"
)
_SETTINGS_STATE_SINGLETON_CHECK_DEFINITION = "CHECK (id)"
_SETTINGS_STATE_REVISION_CHECK_DEFINITION = (
    "CHECK (((revision >= 0) AND (revision <= '9007199254740991'::bigint)))"
)
_PROVIDER_STORAGE_TABLES = (
    "bars",
    "ticks",
    "tick_delta_1m_rollup",
    "tick_volume_profile_5m_rollup",
    "futures_contracts",
    "futures_roll_events",
    "futures_contract_bars",
    "futures_continuous_bars",
    "trading_hours",
    "trading_schedule_snapshots",
    "trading_session_intervals",
    "instruments",
    "quote_snapshots",
    "history_coverage",
)
_REQUIRED_HYPERTABLES = frozenset(
    {
        "bars",
        "futures_contract_bars",
        "futures_continuous_bars",
        "quote_snapshots",
        "ticks",
    }
)


def _timescaledb_api_schema(cur: Any) -> str:
    """Return the namespace that owns the database-scoped TimescaleDB API."""

    cur.execute(
        """
        SELECT namespace_row.nspname
        FROM pg_extension AS extension_row
        JOIN pg_namespace AS namespace_row
          ON namespace_row.oid = extension_row.extnamespace
        WHERE extension_row.extname = 'timescaledb'
        """
    )
    schema_row = cur.fetchone()
    schema_name = str(schema_row[0] or "").strip() if schema_row else ""
    if not schema_name:
        raise RuntimeError("TIMESCALEDB_API_SCHEMA_REQUIRED")
    return schema_name


def _timescaledb_sql(cur: Any, statement: str) -> Any:
    """Qualify TimescaleDB API calls without widening the storage search path."""

    from psycopg import sql

    return sql.SQL(statement).format(timescaledb=sql.Identifier(_timescaledb_api_schema(cur)))


_ROUTE_IDENTITY_PRIMARY_KEYS = {
    "bars": ("instrument_id", "route_fingerprint", "timeframe", "ts"),
    "futures_contract_bars": (
        "provider",
        "instrument_id",
        "route_fingerprint",
        "contract_key",
        "timeframe",
        "ts",
    ),
    "history_coverage": (
        "provider",
        "instrument_id",
        "route_fingerprint",
        "timeframe",
        "covered_from",
        "covered_to",
        "receipt_contract_version",
        "request_contract_version",
        "admission_contract_version",
        "request_mode",
        "request_type",
        "provider_source",
        "provider_contract_id",
        "provider_contract_type",
        "data_type",
    ),
    "drawings": ("id",),
    "trading_hours": ("instrument_id", "route_fingerprint"),
    "trading_schedule_snapshots": ("instrument_id", "route_fingerprint", "fetched_at"),
    "gex_snapshots": ("instrument_id", "route_fingerprint", "captured_at", "source"),
    "option_targets": ("instrument_id", "route_fingerprint", "id"),
    "price_alerts": ("id",),
    "quote_snapshots": ("provider", "instrument_id", "route_fingerprint", "ts"),
    "tick_delta_1m_rollup": ("provider", "instrument_id", "route_fingerprint", "bucket"),
    "tick_volume_profile_5m_rollup": (
        "provider",
        "instrument_id",
        "route_fingerprint",
        "bucket",
        "price",
    ),
}


_DURABLE_ROUTE_IDENTITY_TABLES = (
    "bars",
    "futures_contract_bars",
    "history_coverage",
    "trading_hours",
    "trading_schedule_snapshots",
    "trading_session_intervals",
    "gex_snapshots",
    "quote_snapshots",
    "ticks",
    "tick_delta_1m_rollup",
    "tick_volume_profile_5m_rollup",
)


_GENERATED_ROUTE_IDENTITY_TABLES = (
    "drawings",
    "option_targets",
    "price_alerts",
    "paper_orders",
    "paper_positions",
    "paper_fills",
    "paper_trade_events",
)


_ROUTE_IDENTITY_TABLES = (
    *_DURABLE_ROUTE_IDENTITY_TABLES,
    *_GENERATED_ROUTE_IDENTITY_TABLES,
)


_ROUTE_IDENTITY_CHECK_DEFINITION = "CHECKinstrument_id<>''::textANDroute_fingerprint<>''::text"


_ROUTE_IDENTITY_UNIQUE_INDEXES = frozenset(
    {
        "futures_contracts_current_uidx",
        "instruments_provider_contract_uidx",
        "instruments_watchlist_order_uidx",
        "paper_fills_order_unique_idx",
        "trading_session_intervals_uidx",
    }
)


_ROUTE_IDENTITY_INDEX_OPTIONS = {
    "futures_contracts_current_uidx": ((0, 0), "is_current=true"),
    "instruments_provider_contract_uidx": (
        (0, 0),
        "provider<>''::textandprovider_contract_id<>''::text",
    ),
    "instruments_watchlist_order_uidx": ((0,), "watchlist=true"),
    "bars_hygiene_recent_idx": ((3, 0, 0, 0, 0), "closed=false"),
    "futures_contract_bars_route_tf_ts_idx": ((0, 0, 0, 0, 3), ""),
    "history_coverage_lookup_idx": ((0, 0, 0, 0, 0, 0), ""),
    "trading_schedule_snapshots_lookup_idx": ((0, 0, 3), ""),
    "trading_session_intervals_uidx": ((0, 0, 0, 0, 0), ""),
    "trading_session_intervals_lookup_idx": ((0, 0, 0, 0, 0), ""),
    "gex_snapshots_route_captured_idx": ((0, 0, 3), ""),
    "drawings_identity_timeframe_updated_idx": ((0, 0, 0, 0, 0), ""),
    "option_targets_identity_timeframe_updated_idx": ((0, 0, 0, 0, 0), ""),
    "option_targets_updated_idx": ((0, 0, 0, 0), ""),
    "price_alerts_identity_timeframe_updated_idx": ((0, 0, 0, 0, 0), ""),
    "quote_snapshots_route_ts_idx": ((0, 0, 3), ""),
    "paper_orders_identity_status_idx": ((0, 0, 0, 0, 3), ""),
    "paper_orders_position_pending_idx": ((0, 0, 0, 3), "status='pending'::text"),
    "paper_positions_identity_status_idx": ((0, 0, 0, 0, 3), ""),
    "paper_fills_position_time_idx": ((0, 0, 3), ""),
    "paper_fills_identity_time_idx": ((0, 0, 0, 3), ""),
    "paper_fills_order_unique_idx": ((0, 0), ""),
    "ticks_route_ts_ingest_idx": ((0, 0, 0, 3, 3), ""),
    "ticks_route_price_ts_idx": ((0, 0, 0, 0, 3), ""),
}

_FORBIDDEN_STORAGE_INDEXES = (
    "futures_contract_bars_instrument_tf_ts_idx",
    "tick_delta_1m_rollup_route_bucket_idx",
    "tick_volume_profile_5m_rollup_route_bucket_price_idx",
    "ticks_route_ts_idx",
)


_ROUTE_IDENTITY_INDEXES = {
    "futures_contracts_current_uidx": (
        "futures_contracts",
        ("provider", "instrument_id"),
    ),
    "instruments_provider_contract_uidx": (
        "instruments",
        ("provider", "provider_contract_id"),
    ),
    "instruments_watchlist_order_uidx": (
        "instruments",
        ("watchlist_order",),
    ),
    "bars_hygiene_recent_idx": (
        "bars",
        ("updated_at", "instrument_id", "route_fingerprint", "timeframe", "source"),
    ),
    "futures_contract_bars_route_tf_ts_idx": (
        "futures_contract_bars",
        ("provider", "instrument_id", "route_fingerprint", "timeframe", "ts"),
    ),
    "history_coverage_lookup_idx": (
        "history_coverage",
        (
            "provider",
            "instrument_id",
            "route_fingerprint",
            "timeframe",
            "covered_from",
            "covered_to",
        ),
    ),
    "trading_schedule_snapshots_lookup_idx": (
        "trading_schedule_snapshots",
        ("instrument_id", "route_fingerprint", "fetched_at"),
    ),
    "trading_session_intervals_uidx": (
        "trading_session_intervals",
        ("instrument_id", "route_fingerprint", "session_type", "opens_at", "closes_at"),
    ),
    "trading_session_intervals_lookup_idx": (
        "trading_session_intervals",
        ("instrument_id", "route_fingerprint", "session_type", "opens_at", "closes_at"),
    ),
    "gex_snapshots_route_captured_idx": (
        "gex_snapshots",
        ("instrument_id", "route_fingerprint", "captured_at"),
    ),
    "drawings_identity_timeframe_updated_idx": (
        "drawings",
        ("instrument_id", "route_fingerprint", "timeframe", "updated_at", "id"),
    ),
    "option_targets_identity_timeframe_updated_idx": (
        "option_targets",
        ("instrument_id", "route_fingerprint", "timeframe", "updated_at", "id"),
    ),
    "option_targets_updated_idx": (
        "option_targets",
        ("updated_at", "instrument_id", "route_fingerprint", "id"),
    ),
    "price_alerts_identity_timeframe_updated_idx": (
        "price_alerts",
        ("instrument_id", "route_fingerprint", "timeframe", "updated_at", "id"),
    ),
    "quote_snapshots_route_ts_idx": (
        "quote_snapshots",
        ("instrument_id", "route_fingerprint", "ts"),
    ),
    "paper_orders_identity_status_idx": (
        "paper_orders",
        (
            "instrument_id",
            "route_fingerprint",
            "contract_scope_key",
            "status",
            "updated_at",
        ),
    ),
    "paper_orders_position_pending_idx": (
        "paper_orders",
        ("contract_scope_key", "position_id", "role", "updated_at"),
    ),
    "paper_positions_identity_status_idx": (
        "paper_positions",
        (
            "instrument_id",
            "route_fingerprint",
            "contract_scope_key",
            "status",
            "updated_at",
        ),
    ),
    "paper_fills_position_time_idx": (
        "paper_fills",
        ("contract_scope_key", "position_id", "filled_at"),
    ),
    "paper_fills_identity_time_idx": (
        "paper_fills",
        ("instrument_id", "route_fingerprint", "contract_scope_key", "filled_at"),
    ),
    "paper_fills_order_unique_idx": (
        "paper_fills",
        ("contract_scope_key", "order_id"),
    ),
    "ticks_route_ts_ingest_idx": (
        "ticks",
        ("provider", "instrument_id", "route_fingerprint", "ts", "ingest_seq"),
    ),
    "ticks_route_price_ts_idx": (
        "ticks",
        ("provider", "instrument_id", "route_fingerprint", "price", "ts"),
    ),
}


def _verify_route_identity_storage(cur: Any) -> None:
    """Require the exact provider-qualified route identity schema without repairing it."""

    current_primary_keys: dict[str, tuple[str, tuple[str, ...]] | None] = {}
    for table in _ROUTE_IDENTITY_PRIMARY_KEYS:
        cur.execute(
            """
            SELECT constraint_row.conname,
                   array_agg(attribute_row.attname ORDER BY key_column.ordinality)
            FROM pg_constraint AS constraint_row
            JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
              AS key_column(attnum, ordinality) ON true
            JOIN pg_attribute AS attribute_row
              ON attribute_row.attrelid = constraint_row.conrelid
             AND attribute_row.attnum = key_column.attnum
            WHERE constraint_row.conrelid = to_regclass(%s)
              AND constraint_row.contype = 'p'
            GROUP BY constraint_row.conname
            """,
            (table,),
        )
        row = cur.fetchone()
        current_primary_keys[table] = (
            (str(row[0]), tuple(str(value) for value in row[1])) if row else None
        )

    current_indexes: dict[
        str,
        tuple[
            bool,
            bool,
            bool,
            str,
            str,
            tuple[str, ...],
            tuple[int, ...],
            str,
        ]
        | None,
    ] = {}
    for index_name in _ROUTE_IDENTITY_INDEXES:
        cur.execute(
            """
            SELECT index_row.indisunique,
                   index_row.indisvalid,
                   index_row.indisready,
                   access_method.amname,
                   table_relation.relname,
                   array_agg(attribute_row.attname ORDER BY key_column.ordinality),
                   index_row.indoption::smallint[],
                   pg_get_expr(index_row.indpred, index_row.indrelid)
            FROM pg_index AS index_row
            JOIN pg_class AS index_relation ON index_relation.oid = index_row.indexrelid
            JOIN pg_namespace AS index_namespace
              ON index_namespace.oid = index_relation.relnamespace
            JOIN pg_class AS table_relation ON table_relation.oid = index_row.indrelid
            JOIN pg_am AS access_method ON access_method.oid = index_relation.relam
            JOIN LATERAL unnest(index_row.indkey) WITH ORDINALITY
              AS key_column(attnum, ordinality) ON true
            JOIN pg_attribute AS attribute_row
              ON attribute_row.attrelid = index_row.indrelid
             AND attribute_row.attnum = key_column.attnum
            WHERE index_relation.oid = to_regclass(%s)
              AND index_namespace.nspname = current_schema()
            GROUP BY index_relation.oid,
                     index_row.indisunique,
                     index_row.indisvalid,
                     index_row.indisready,
                     access_method.amname,
                     table_relation.relname,
                     index_row.indoption,
                     index_row.indpred,
                     index_row.indrelid
            """,
            (index_name,),
        )
        row = cur.fetchone()
        current_indexes[index_name] = (
            (
                bool(row[0]),
                bool(row[1]),
                bool(row[2]),
                str(row[3]),
                str(row[4]),
                tuple(str(value) for value in row[5]),
                tuple(int(value) for value in row[6]),
                "".join(
                    character.lower()
                    for character in str(row[7] or "")
                    if not character.isspace() and character not in "()"
                ),
            )
            if row
            else None
        )

    cur.execute(
        """
        SELECT index_relation.relname
        FROM pg_class AS index_relation
        JOIN pg_namespace AS index_namespace
          ON index_namespace.oid = index_relation.relnamespace
        WHERE index_relation.relkind = 'i'
          AND index_relation.relname = ANY(%s)
          AND index_namespace.nspname = current_schema()
        ORDER BY index_relation.relname
        """,
        (list(_FORBIDDEN_STORAGE_INDEXES),),
    )
    forbidden_indexes = tuple(str(row[0]) for row in cur.fetchall())

    current_identity_not_null: dict[str, bool] = {}
    for table in _ROUTE_IDENTITY_TABLES:
        cur.execute(
            """
            SELECT count(*) = 2 AND bool_and(attribute_row.attnotnull)
            FROM pg_attribute AS attribute_row
            WHERE attribute_row.attrelid = to_regclass(%s)
              AND attribute_row.attname = ANY(%s)
              AND NOT attribute_row.attisdropped
            """,
            (table, ["instrument_id", "route_fingerprint"]),
        )
        row = cur.fetchone()
        current_identity_not_null[table] = bool(row and row[0])

    expected_check_constraints = {
        f"{table}_route_identity_chk": table for table in _ROUTE_IDENTITY_TABLES
    }
    cur.execute(
        """
        SELECT constraint_row.conname,
               table_row.relname,
               constraint_row.convalidated,
               pg_get_constraintdef(constraint_row.oid)
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace_row ON namespace_row.oid = table_row.relnamespace
        WHERE constraint_row.conname = ANY(%s)
          AND namespace_row.nspname = current_schema()
        """,
        (list(expected_check_constraints),),
    )
    current_check_constraints = {
        str(row[0]): (
            str(row[1]),
            bool(row[2]),
            "".join(
                character
                for character in str(row[3])
                if not character.isspace() and character not in "()"
            ),
        )
        for row in cur.fetchall()
    }
    present_check_constraints = {
        constraint_name
        for constraint_name, (table, validated, definition) in current_check_constraints.items()
        if table == expected_check_constraints[constraint_name]
        and definition == _ROUTE_IDENTITY_CHECK_DEFINITION
        and validated
    }
    invalid_primary_keys = sorted(
        table
        for table, columns in _ROUTE_IDENTITY_PRIMARY_KEYS.items()
        if current_primary_keys[table] is None
        or current_primary_keys[table][0] != f"{table}_pkey"
        or current_primary_keys[table][1] != columns
    )
    invalid_indexes = sorted(
        index_name
        for index_name, definition in _ROUTE_IDENTITY_INDEXES.items()
        if current_indexes[index_name]
        != (
            index_name in _ROUTE_IDENTITY_UNIQUE_INDEXES,
            True,
            True,
            "btree",
            definition[0],
            definition[1],
            _ROUTE_IDENTITY_INDEX_OPTIONS[index_name][0],
            _ROUTE_IDENTITY_INDEX_OPTIONS[index_name][1],
        )
    )
    invalid_not_null = sorted(
        table for table, is_not_null in current_identity_not_null.items() if not is_not_null
    )
    missing_checks = sorted(set(expected_check_constraints) - present_check_constraints)
    if (
        invalid_primary_keys
        or invalid_indexes
        or invalid_not_null
        or missing_checks
        or forbidden_indexes
    ):
        raise RuntimeError(
            "ROUTE_IDENTITY_SCHEMA_CONTRACT_REQUIRED "
            f"primary_keys={invalid_primary_keys!r} "
            f"indexes={invalid_indexes!r} "
            f"forbidden_indexes={list(forbidden_indexes)!r} "
            f"not_null={invalid_not_null!r} "
            f"missing_checks={missing_checks!r}"
        )


def _verify_discord_signal_storage(cur: Any) -> None:
    """Require the exact durable Discord raw-message journal without repairing it."""

    cur.execute(
        """
        SELECT attribute.attname,
               format_type(attribute.atttypid, attribute.atttypmod),
               attribute.attnotnull,
               COALESCE(pg_get_expr(default_value.adbin, default_value.adrelid), '')
        FROM pg_attribute AS attribute
        LEFT JOIN pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        WHERE attribute.attrelid = to_regclass('discord_signal_messages')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY attribute.attnum
        """
    )
    columns = tuple(cur.fetchall())
    expected_columns = (
        ("message_id", "text", True, ""),
        ("channel_id", "text", True, ""),
        ("author_id", "text", False, ""),
        ("author_name", "text", False, ""),
        ("published_at", "timestamp with time zone", False, ""),
        ("content", "text", False, ""),
        ("reply_to_message_id", "text", False, ""),
        ("source_event", "text", True, ""),
        ("source_event_at", "timestamp with time zone", True, ""),
        ("created_at", "timestamp with time zone", True, "now()"),
        ("updated_at", "timestamp with time zone", True, "now()"),
        ("deleted_at", "timestamp with time zone", False, ""),
    )
    if columns != expected_columns:
        raise RuntimeError(f"DISCORD_SIGNAL_STORAGE_COLUMNS_REQUIRED columns={columns!r}")

    cur.execute(
        """
        SELECT constraint_row.conname,
               array_agg(attribute_row.attname ORDER BY key_column.ordinality)
        FROM pg_constraint AS constraint_row
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
          AS key_column(attnum, ordinality) ON true
        JOIN pg_attribute AS attribute_row
          ON attribute_row.attrelid = constraint_row.conrelid
         AND attribute_row.attnum = key_column.attnum
        WHERE constraint_row.conrelid = to_regclass('discord_signal_messages')
          AND constraint_row.contype = 'p'
        GROUP BY constraint_row.conname
        """
    )
    primary_key = cur.fetchone()
    if primary_key != ("discord_signal_messages_pkey", ["message_id"]):
        raise RuntimeError(
            f"DISCORD_SIGNAL_STORAGE_PRIMARY_KEY_REQUIRED constraint={primary_key!r}"
        )

    cur.execute(
        """
        SELECT conname, contype, convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('discord_signal_messages')
          AND contype <> 'p'
        ORDER BY conname
        """
    )
    constraints = tuple(cur.fetchall())
    expected_constraints = (
        (
            "discord_signal_messages_content_chk",
            "c",
            True,
            _DISCORD_SIGNAL_CONTENT_CHECK_DEFINITION,
        ),
        (
            "discord_signal_messages_identity_chk",
            "c",
            True,
            _DISCORD_SIGNAL_IDENTITY_CHECK_DEFINITION,
        ),
        (
            "discord_signal_messages_source_event_chk",
            "c",
            True,
            _DISCORD_SIGNAL_SOURCE_EVENT_CHECK_DEFINITION,
        ),
        (
            "discord_signal_messages_state_chk",
            "c",
            True,
            _DISCORD_SIGNAL_STATE_CHECK_DEFINITION,
        ),
    )
    if constraints != expected_constraints:
        raise RuntimeError(
            f"DISCORD_SIGNAL_STORAGE_CONSTRAINTS_REQUIRED constraints={constraints!r}"
        )

    cur.execute(
        """
        SELECT index_row.indisunique,
               index_row.indisvalid,
               index_row.indisready,
               access_method.amname,
               table_relation.relname,
               array_agg(attribute_row.attname ORDER BY key_column.ordinality),
               index_row.indoption::smallint[],
               pg_get_expr(index_row.indpred, index_row.indrelid)
        FROM pg_index AS index_row
        JOIN pg_class AS index_relation ON index_relation.oid = index_row.indexrelid
        JOIN pg_class AS table_relation ON table_relation.oid = index_row.indrelid
        JOIN pg_am AS access_method ON access_method.oid = index_relation.relam
        JOIN LATERAL unnest(index_row.indkey) WITH ORDINALITY
          AS key_column(attnum, ordinality) ON true
        JOIN pg_attribute AS attribute_row
          ON attribute_row.attrelid = index_row.indrelid
         AND attribute_row.attnum = key_column.attnum
        WHERE index_relation.oid = to_regclass('discord_signal_messages_active_idx')
        GROUP BY index_relation.oid,
                 index_row.indisunique,
                 index_row.indisvalid,
                 index_row.indisready,
                 access_method.amname,
                 table_relation.relname,
                 index_row.indoption,
                 index_row.indpred,
                 index_row.indrelid
        """
    )
    index_row = cur.fetchone()
    expected_index = (
        False,
        True,
        True,
        "btree",
        "discord_signal_messages",
        ["channel_id", "author_id", "published_at", "message_id"],
        [0, 0, 3, 3],
        "(deleted_at IS NULL)",
    )
    if index_row != expected_index:
        raise RuntimeError(f"DISCORD_SIGNAL_STORAGE_INDEX_REQUIRED index={index_row!r}")


def _verify_paper_order_protection_storage(cur: Any) -> None:
    from aef_terminal.storage.db_utils import _paper_order_from_row

    cur.execute(
        """
        SELECT id, symbol, timeframe, side, order_type, status, qty, entry,
               stop_loss, target, use_stop_loss, use_target, created_at,
               updated_at, filled_at, fill_price, role, position_id,
               parent_order_id, oco_group_id, reduce_only, payload
        FROM paper_orders
        ORDER BY id
        """
    )
    for row in cur.fetchall():
        try:
            _paper_order_from_row(row)
        except (TypeError, ValueError) as exc:
            order_id = row[0] if row else None
            raise RuntimeError(
                f"PAPER_ORDER_STORAGE_PROTECTION_CONTRACT_REQUIRED order_id={order_id!r}"
            ) from exc


def _verify_paper_contract_storage(cur: Any) -> None:
    from aef_terminal.storage.db_utils import (
        _paper_fill_from_row,
        _paper_position_from_row,
    )

    generated_columns = (
        (
            "contract_scope_key",
            "text",
            True,
            "s",
            "((payload -> 'paper_contract'::text) ->> 'scope_key'::text)",
        ),
        (
            "contract_scope_kind",
            "text",
            True,
            "s",
            "((payload -> 'paper_contract'::text) ->> 'scope_kind'::text)",
        ),
        (
            "contract_provider",
            "text",
            True,
            "s",
            "((payload -> 'paper_contract'::text) ->> 'provider'::text)",
        ),
        (
            "contract_sec_type",
            "text",
            False,
            "s",
            "((payload -> 'paper_contract'::text) ->> 'sec_type'::text)",
        ),
        (
            "contract_provider_contract_id",
            "text",
            True,
            "s",
            "((payload -> 'paper_contract'::text) ->> 'provider_contract_id'::text)",
        ),
        (
            "contract_con_id",
            "bigint",
            False,
            "s",
            "(((payload -> 'paper_contract'::text) ->> 'con_id'::text))::bigint",
        ),
        (
            "contract_exchange",
            "text",
            False,
            "s",
            "((payload -> 'paper_contract'::text) ->> 'exchange'::text)",
        ),
    )
    for table_name in ("paper_orders", "paper_positions", "paper_fills"):
        cur.execute(
            """
            SELECT attribute.attname,
                   format_type(attribute.atttypid, attribute.atttypmod),
                   attribute.attnotnull,
                   attribute.attgenerated,
                   COALESCE(pg_get_expr(generated_value.adbin, generated_value.adrelid), '')
            FROM pg_attribute AS attribute
            LEFT JOIN pg_attrdef AS generated_value
              ON generated_value.adrelid = attribute.attrelid
             AND generated_value.adnum = attribute.attnum
            WHERE attribute.attrelid = to_regclass(%s)
              AND attribute.attname = ANY(%s)
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            ORDER BY attribute.attnum
            """,
            (table_name, [column[0] for column in generated_columns]),
        )
        columns = tuple(cur.fetchall())
        if columns != generated_columns:
            raise RuntimeError(
                f"PAPER_CONTRACT_STORAGE_COLUMNS_REQUIRED table={table_name!r} columns={columns!r}"
            )
        cur.execute(
            """
            SELECT convalidated, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = to_regclass(%s)
              AND conname = %s
            """,
            (table_name, f"{table_name}_contract_scope_chk"),
        )
        constraint = cur.fetchone()
        if constraint != (True, _PAPER_CONTRACT_SCOPE_CHECK_DEFINITION):
            raise RuntimeError(
                "PAPER_CONTRACT_STORAGE_CHECK_REQUIRED "
                f"table={table_name!r} constraint={constraint!r}"
            )
        cur.execute(
            """
            SELECT convalidated, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = to_regclass(%s)
              AND conname = %s
            """,
            (table_name, f"{table_name}_contract_provider_chk"),
        )
        provider_constraint = cur.fetchone()
        if provider_constraint != (
            True,
            _PAPER_CONTRACT_PROVIDER_CHECK_DEFINITION,
        ):
            raise RuntimeError(
                "PAPER_CONTRACT_PROVIDER_STORAGE_CHECK_REQUIRED "
                f"table={table_name!r} constraint={provider_constraint!r}"
            )

    cur.execute(
        """
        SELECT id, symbol, timeframe, status, qty, avg_entry, opened_at,
               updated_at, closed_at, realized_pnl, payload
        FROM paper_positions
        ORDER BY id
        """
    )
    for row in cur.fetchall():
        try:
            _paper_position_from_row(row)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "PAPER_POSITION_CONTRACT_STORAGE_REQUIRED "
                f"position_id={(row[0] if row else None)!r}"
            ) from exc
    cur.execute(
        """
        SELECT id, order_id, position_id, symbol, timeframe, side, qty, price,
               role, reduce_only, pnl_points, filled_at, payload
        FROM paper_fills
        ORDER BY id
        """
    )
    for row in cur.fetchall():
        try:
            _paper_fill_from_row(row)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"PAPER_FILL_CONTRACT_STORAGE_REQUIRED fill_id={(row[0] if row else None)!r}"
            ) from exc


def _verify_history_coverage_storage(cur: Any) -> None:
    """Require the exact typed complete-range receipt relation."""

    cur.execute(
        """
        SELECT attribute.attname,
               format_type(attribute.atttypid, attribute.atttypmod),
               attribute.attnotnull,
               COALESCE(pg_get_expr(default_value.adbin, default_value.adrelid), '')
        FROM pg_attribute AS attribute
        LEFT JOIN pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        WHERE attribute.attrelid = to_regclass('history_coverage')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY attribute.attnum
        """
    )
    columns = tuple(cur.fetchall())
    expected_columns = (
        ("provider", "text", True, ""),
        ("instrument_id", "text", True, ""),
        ("route_fingerprint", "text", True, ""),
        ("timeframe", "text", True, ""),
        ("covered_from", "timestamp with time zone", True, ""),
        ("covered_to", "timestamp with time zone", True, ""),
        ("receipt_contract_version", "integer", True, ""),
        ("request_contract_version", "integer", True, ""),
        ("admission_contract_version", "integer", True, ""),
        ("request_mode", "text", True, ""),
        ("request_type", "text", True, ""),
        ("provider_source", "text", True, ""),
        ("provider_contract_id", "text", True, ""),
        ("provider_contract_type", "text", True, ""),
        ("data_type", "text", True, ""),
        ("provider_limit", "bigint", False, ""),
        ("response_count", "bigint", True, ""),
        ("completed_at", "timestamp with time zone", True, ""),
        ("created_at", "timestamp with time zone", True, "now()"),
    )
    if columns != expected_columns:
        raise RuntimeError(f"HISTORY_COVERAGE_STORAGE_COLUMNS_REQUIRED columns={columns!r}")

    cur.execute(
        """
        SELECT conname, convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('history_coverage')
          AND conname = ANY(%s)
        ORDER BY conname
        """,
        (
            [
                "history_coverage_authority_chk",
                "history_coverage_count_chk",
                "history_coverage_range_chk",
                "history_coverage_versions_chk",
            ],
        ),
    )
    constraints = tuple(cur.fetchall())
    expected_constraints = (
        (
            "history_coverage_authority_chk",
            True,
            _HISTORY_COVERAGE_AUTHORITY_CHECK_DEFINITION,
        ),
        (
            "history_coverage_count_chk",
            True,
            _HISTORY_COVERAGE_COUNT_CHECK_DEFINITION,
        ),
        (
            "history_coverage_range_chk",
            True,
            _HISTORY_COVERAGE_RANGE_CHECK_DEFINITION,
        ),
        (
            "history_coverage_versions_chk",
            True,
            _HISTORY_COVERAGE_VERSION_CHECK_DEFINITION,
        ),
    )
    if constraints != expected_constraints:
        raise RuntimeError(
            f"HISTORY_COVERAGE_STORAGE_CONTRACT_REQUIRED constraints={constraints!r}"
        )


def _verify_drawing_payload_storage(cur: Any) -> None:
    from aef_terminal.data.instrument_identity import require_exact_identity_text
    from aef_terminal.drawing_contract import validate_durable_drawing_payload

    cur.execute(
        """
        SELECT drawing.id, drawing.instrument_id, drawing.route_fingerprint,
               drawing.symbol, drawing.timeframe, drawing.payload,
               instrument.identity_scope, instrument.asset_class,
               current_contract.provider_contract_id
        FROM drawings AS drawing
        LEFT JOIN instruments AS instrument
          ON instrument.instrument_id = drawing.instrument_id
         AND instrument.provider = drawing.payload ->> 'provider'
        LEFT JOIN LATERAL (
            SELECT NULLIF(contract.metadata ->> 'provider_contract_id', '')
                       AS provider_contract_id
            FROM futures_contracts AS contract
            WHERE contract.provider = instrument.provider
              AND contract.instrument_id = instrument.instrument_id
              AND contract.is_current = true
            ORDER BY contract.current_resolved_at DESC NULLS LAST
            LIMIT 1
        ) AS current_contract
          ON instrument.identity_scope = 'root'
         AND instrument.asset_class = 'future'
        ORDER BY drawing.id
        """
    )
    violations: list[str] = []
    for (
        drawing_id,
        instrument_id,
        route_fingerprint,
        symbol,
        timeframe,
        payload,
        identity_scope,
        asset_class,
        current_provider_contract_id,
    ) in cur.fetchall():
        exact_id = drawing_id if isinstance(drawing_id, str) else "<missing>"
        try:
            exact_id = require_exact_identity_text(drawing_id, field="DRAWING_ID")
            exact_instrument_id = require_exact_identity_text(
                instrument_id,
                field="DRAWING_INSTRUMENT_ID",
            )
            exact_route_fingerprint = require_exact_identity_text(
                route_fingerprint,
                field="DRAWING_ROUTE_FINGERPRINT",
            )
            exact_symbol = require_exact_identity_text(
                symbol,
                field="DRAWING_SYMBOL",
            )
            require_exact_identity_text(timeframe, field="DRAWING_TIMEFRAME")
            if not isinstance(payload, dict):
                raise ValueError("DRAWING_ROW_INVALID: stored payload must be an object")
            for field, expected in (
                ("id", exact_id),
                ("instrument_id", exact_instrument_id),
                ("route_fingerprint", exact_route_fingerprint),
                ("symbol", exact_symbol),
            ):
                if payload.get(field) != expected:
                    raise ValueError(f"DRAWING_SCOPE_MISMATCH: {field}")
            for field in ("provider", "provider_contract_id"):
                value = payload.get(field)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"DRAWING_SCOPE_MISMATCH: {field}")
            if not isinstance(identity_scope, str) or not isinstance(asset_class, str):
                raise ValueError("DRAWING_INSTRUMENT_NOT_FOUND")
            if identity_scope == "root" and asset_class == "future":
                if (
                    not isinstance(current_provider_contract_id, str)
                    or not current_provider_contract_id
                    or payload.get("provider_contract_id") != current_provider_contract_id
                ):
                    raise ValueError("DRAWING_FUTURES_ROUTE_STALE")
            validate_durable_drawing_payload(payload)
        except (TypeError, ValueError) as exc:
            violations.append(f"{exact_id or '<missing>'}:{exc}")
    if violations:
        raise RuntimeError(
            "DRAWING_PAYLOAD_STORAGE_CONTRACT_REQUIRED violations=" + repr(violations[:12])
        )


def _verify_storage_runtime_contract(cur: Any) -> None:
    from aef_terminal.data.instrument_identity import require_exact_identity_text
    from aef_terminal.settings_contract import (
        CLIENT_SETTINGS_SCOPE,
        SERVER_SETTINGS_SCOPE,
        WATCHLIST_PRESENTATION_SCOPE,
        invalid_client_setting_instrument_keys,
        invalid_client_setting_value_keys,
        invalid_persisted_client_setting_keys,
        invalid_persisted_server_setting_keys,
        invalid_server_setting_value_keys,
        invalid_workspace_instrument_selection_keys,
        watchlist_presentation_payload,
    )
    from aef_terminal.watchlist_contract import WATCHLIST_SETTINGS_LOCK_KEY

    _verify_route_identity_storage(cur)
    _verify_discord_signal_storage(cur)
    _verify_paper_order_protection_storage(cur)
    _verify_paper_contract_storage(cur)
    _verify_history_coverage_storage(cur)
    expected_provider_constraints = {
        f"{table}_provider_chk": table for table in _PROVIDER_STORAGE_TABLES
    }
    cur.execute(
        """
        SELECT constraint_row.conname,
               table_row.relname,
               constraint_row.convalidated,
               pg_get_constraintdef(constraint_row.oid)
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS table_row ON table_row.oid = constraint_row.conrelid
        JOIN pg_namespace AS namespace_row ON namespace_row.oid = table_row.relnamespace
        WHERE constraint_row.conname = ANY(%s)
          AND namespace_row.nspname = current_schema()
        """,
        (list(expected_provider_constraints),),
    )
    valid_provider_constraints = {
        str(row[0])
        for row in cur.fetchall()
        if str(row[1]) == expected_provider_constraints[str(row[0])]
        and bool(row[2])
        and str(row[3]) == _CANONICAL_PROVIDER_CHECK_DEFINITION
    }
    invalid_provider_constraints = sorted(
        set(expected_provider_constraints) - valid_provider_constraints
    )
    if invalid_provider_constraints:
        raise RuntimeError(
            f"CANONICAL_PROVIDER_STORAGE_REQUIRED constraints={invalid_provider_constraints!r}"
        )
    cur.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
        (WATCHLIST_SETTINGS_LOCK_KEY,),
    )
    cur.execute(
        """
        SELECT convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('settings')
          AND conname = 'settings_scope_chk'
        """
    )
    settings_scope_check = cur.fetchone()
    if settings_scope_check != (True, _SETTINGS_SCOPE_CHECK_DEFINITION):
        raise RuntimeError(f"SETTINGS_SCOPE_CHECK_REQUIRED constraint={settings_scope_check!r}")
    cur.execute(
        """
        SELECT convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('settings')
          AND conname = 'settings_mutation_order_chk'
        """
    )
    mutation_order_check = cur.fetchone()
    if mutation_order_check != (True, _SETTINGS_MUTATION_ORDER_CHECK_DEFINITION):
        raise RuntimeError(
            f"SETTINGS_MUTATION_ORDER_CHECK_REQUIRED constraint={mutation_order_check!r}"
        )
    cur.execute(
        """
        SELECT attribute.attname,
               format_type(attribute.atttypid, attribute.atttypmod),
               attribute.attnotnull,
               COALESCE(pg_get_expr(default_value.adbin, default_value.adrelid), '')
        FROM pg_attribute AS attribute
        LEFT JOIN pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        WHERE attribute.attrelid = to_regclass('settings')
          AND attribute.attname = ANY(%s)
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY attribute.attname
        """,
        (["mutation_changed_at_ms", "mutation_sequence", "mutation_writer_id"],),
    )
    mutation_order_columns = tuple(cur.fetchall())
    if mutation_order_columns != (
        ("mutation_changed_at_ms", "bigint", True, "0"),
        ("mutation_sequence", "bigint", True, "0"),
        ("mutation_writer_id", "uuid", False, ""),
    ):
        raise RuntimeError(
            f"SETTINGS_MUTATION_ORDER_COLUMNS_REQUIRED columns={mutation_order_columns!r}"
        )
    cur.execute(
        """
        SELECT attribute.attname,
               format_type(attribute.atttypid, attribute.atttypmod),
               attribute.attnotnull,
               COALESCE(pg_get_expr(default_value.adbin, default_value.adrelid), '')
        FROM pg_attribute AS attribute
        LEFT JOIN pg_attrdef AS default_value
          ON default_value.adrelid = attribute.attrelid
         AND default_value.adnum = attribute.attnum
        WHERE attribute.attrelid = to_regclass('settings_state')
          AND attribute.attname = ANY(%s)
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        ORDER BY attribute.attname
        """,
        (["id", "revision", "updated_at"],),
    )
    settings_state_columns = tuple(cur.fetchall())
    if settings_state_columns != (
        ("id", "boolean", True, "true"),
        ("revision", "bigint", True, "0"),
        ("updated_at", "timestamp with time zone", True, "now()"),
    ):
        raise RuntimeError(f"SETTINGS_STATE_COLUMNS_REQUIRED columns={settings_state_columns!r}")
    cur.execute(
        """
        SELECT constraint_row.conname,
               array_agg(attribute_row.attname ORDER BY key_column.ordinality)
        FROM pg_constraint AS constraint_row
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
          AS key_column(attnum, ordinality) ON true
        JOIN pg_attribute AS attribute_row
          ON attribute_row.attrelid = constraint_row.conrelid
         AND attribute_row.attnum = key_column.attnum
        WHERE constraint_row.conrelid = to_regclass('settings_state')
          AND constraint_row.contype = 'p'
        GROUP BY constraint_row.conname
        """
    )
    settings_state_primary_key = cur.fetchone()
    if settings_state_primary_key != ("settings_state_pkey", ["id"]):
        raise RuntimeError(
            f"SETTINGS_STATE_PRIMARY_KEY_REQUIRED constraint={settings_state_primary_key!r}"
        )
    cur.execute(
        """
        SELECT conname, convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('settings_state')
          AND conname = ANY(%s)
        ORDER BY conname
        """,
        (["settings_state_revision_chk", "settings_state_singleton_chk"],),
    )
    settings_state_constraints = tuple(cur.fetchall())
    if settings_state_constraints != (
        (
            "settings_state_revision_chk",
            True,
            _SETTINGS_STATE_REVISION_CHECK_DEFINITION,
        ),
        (
            "settings_state_singleton_chk",
            True,
            _SETTINGS_STATE_SINGLETON_CHECK_DEFINITION,
        ),
    ):
        raise RuntimeError(
            f"SETTINGS_STATE_CONTRACT_REQUIRED constraints={settings_state_constraints!r}"
        )
    cur.execute("SELECT id, revision FROM settings_state ORDER BY id")
    settings_state_rows = tuple(cur.fetchall())
    if (
        len(settings_state_rows) != 1
        or settings_state_rows[0][0] is not True
        or not 0 <= int(settings_state_rows[0][1]) <= 9_007_199_254_740_991
    ):
        raise RuntimeError(f"SETTINGS_STATE_ROW_REQUIRED rows={settings_state_rows!r}")
    cur.execute(
        """
        SELECT scope,
               key,
               mutation_changed_at_ms,
               mutation_writer_id::text,
               mutation_sequence
        FROM settings
        ORDER BY scope, key
        """
    )
    mutation_order_rows = cur.fetchall()
    from uuid import UUID

    invalid_mutation_orders: list[str] = []
    for scope, key, changed_at_ms, writer_id, sequence in mutation_order_rows:
        if writer_id is None and changed_at_ms == 0 and sequence == 0:
            continue
        try:
            parsed_writer_id = UUID(writer_id)
        except AttributeError, TypeError, ValueError:
            invalid_mutation_orders.append(f"{scope}:{key}")
            continue
        if (
            scope != CLIENT_SETTINGS_SCOPE
            or parsed_writer_id.version != 4
            or str(parsed_writer_id) != writer_id
            or not isinstance(changed_at_ms, int)
            or isinstance(changed_at_ms, bool)
            or not 0 < changed_at_ms <= 9_007_199_254_740_991
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 < sequence <= 9_007_199_254_740_991
        ):
            invalid_mutation_orders.append(f"{scope}:{key}")
    if invalid_mutation_orders:
        raise RuntimeError(
            f"SETTINGS_MUTATION_ORDER_STORAGE_REQUIRED rows={sorted(invalid_mutation_orders)!r}"
        )
    cur.execute("SELECT scope, key, value FROM settings ORDER BY scope, key")
    settings_rows = cur.fetchall()
    invalid_scopes = sorted(
        {str(row[0]) for row in settings_rows if row[0] not in CURRENT_SETTINGS_SCOPES}
    )
    if invalid_scopes:
        raise RuntimeError(f"STORAGE_SETTINGS_SCOPE_REQUIRED scopes={invalid_scopes!r}")
    cur.execute(
        """
        SELECT instrument_id
        FROM instruments
        WHERE watchlist = true
        ORDER BY instrument_id
        """
    )
    current_instrument_ids = {row[0] for row in cur.fetchall()}

    def current_instrument_exists(instrument_id: str) -> bool:
        return instrument_id in current_instrument_ids

    client_settings = {row[1]: row[2] for row in settings_rows if row[0] == CLIENT_SETTINGS_SCOPE}
    invalid_setting_keys = invalid_persisted_client_setting_keys(client_settings)
    invalid_client_values = invalid_client_setting_value_keys(
        client_settings,
        instrument_exists=current_instrument_exists,
    )
    invalid_selection_keys = invalid_workspace_instrument_selection_keys(
        client_settings,
        instrument_exists=current_instrument_exists,
    )
    invalid_client_instrument_keys = invalid_client_setting_instrument_keys(
        client_settings,
        instrument_exists=current_instrument_exists,
    )
    if (
        invalid_setting_keys
        or invalid_client_values
        or invalid_selection_keys
        or invalid_client_instrument_keys
    ):
        raise RuntimeError(
            "STORAGE_CLIENT_SETTINGS_CONTRACT_REQUIRED "
            f"keys={invalid_setting_keys!r} "
            f"values={invalid_client_values!r} "
            f"selection_values={invalid_selection_keys!r} "
            f"instruments={invalid_client_instrument_keys!r}"
        )
    server_settings = {row[1]: row[2] for row in settings_rows if row[0] == SERVER_SETTINGS_SCOPE}
    invalid_server_keys = invalid_persisted_server_setting_keys(server_settings)
    invalid_server_values = invalid_server_setting_value_keys(
        server_settings,
        instrument_exists=current_instrument_exists,
    )
    if invalid_server_keys or invalid_server_values:
        raise RuntimeError(
            "STORAGE_SERVER_SETTINGS_CONTRACT_REQUIRED "
            f"keys={invalid_server_keys!r} "
            f"values={invalid_server_values!r}"
        )
    invalid_presentations: list[str] = []
    for scope, key, value in settings_rows:
        if scope != WATCHLIST_PRESENTATION_SCOPE:
            continue
        try:
            identity = require_exact_identity_text(
                key,
                field="instrument_id",
            )
            watchlist_presentation_payload(
                value,
                allow_default_revision=False,
            )
        except ValueError:
            invalid_presentations.append(str(key))
            continue
        if identity not in current_instrument_ids:
            invalid_presentations.append(identity)
    if invalid_presentations:
        raise RuntimeError(
            "WATCHLIST_PRESENTATION_STORAGE_CONTRACT_REQUIRED "
            f"keys={sorted(invalid_presentations)!r}"
        )
    cur.execute(
        """
        SELECT convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('instruments')
          AND conname = 'instruments_provider_contract_required_chk'
        """
    )
    instrument_contract_check = cur.fetchone()
    if instrument_contract_check != (
        True,
        _INSTRUMENT_PROVIDER_CONTRACT_CHECK_DEFINITION,
    ):
        raise RuntimeError(
            f"INSTRUMENT_PROVIDER_CONTRACT_CHECK_REQUIRED constraint={instrument_contract_check!r}"
        )
    cur.execute(
        """
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'instruments'
          AND column_name = 'identity_scope'
        """
    )
    identity_scope_column = cur.fetchone()
    if identity_scope_column != ("text", "NO", None):
        raise RuntimeError(
            f"INSTRUMENT_IDENTITY_SCOPE_COLUMN_REQUIRED column={identity_scope_column!r}"
        )
    cur.execute(
        """
        SELECT column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'gex_snapshots'
          AND column_name = ANY(%s)
        ORDER BY column_name
        """,
        (["asset", "provider_symbol"],),
    )
    gex_metadata_columns = [(str(row[0]), str(row[1])) for row in cur.fetchall()]
    if gex_metadata_columns != [("provider_symbol", "NO")]:
        raise RuntimeError(
            f"GEX_SNAPSHOT_PROVIDER_SYMBOL_COLUMN_REQUIRED columns={gex_metadata_columns!r}"
        )
    cur.execute(
        """
        SELECT is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'gex_snapshots'
          AND column_name = 'source'
        """
    )
    gex_source_column = cur.fetchone()
    if gex_source_column != ("NO", None):
        raise RuntimeError(f"GEX_SNAPSHOT_SOURCE_COLUMN_REQUIRED column={gex_source_column!r}")
    cur.execute(
        """
        SELECT convalidated, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = to_regclass('gex_snapshots')
          AND conname = 'gex_snapshots_source_chk'
        """
    )
    gex_source_check = cur.fetchone()
    if gex_source_check != (True, _GEX_SNAPSHOT_SOURCE_CHECK_DEFINITION):
        raise RuntimeError(f"GEX_SNAPSHOT_SOURCE_CHECK_REQUIRED constraint={gex_source_check!r}")
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    extension_row = cur.fetchone()
    if extension_row is None or not str(extension_row[0] or "").strip():
        raise RuntimeError("TIMESCALEDB_EXTENSION_REQUIRED")
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'pg_stat_statements'")
    statistics_extension_row = cur.fetchone()
    if statistics_extension_row is None or not str(statistics_extension_row[0] or "").strip():
        raise RuntimeError("PG_STAT_STATEMENTS_EXTENSION_REQUIRED")

    cur.execute("SELECT to_regclass('canonical_bar_revision_sequence')")
    revision_sequence_row = cur.fetchone()
    if revision_sequence_row is None or revision_sequence_row[0] is None:
        raise RuntimeError("CANONICAL_BAR_REVISION_SEQUENCE_REQUIRED")
    cur.execute(
        """
        SELECT table_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name IN ('bars', 'futures_contract_bars', 'futures_continuous_bars')
          AND column_name = 'revision_sequence'
        """
    )
    revision_columns = {str(row[0]): (str(row[1]), str(row[2] or "")) for row in cur.fetchall()}
    if set(revision_columns) != {
        "bars",
        "futures_contract_bars",
        "futures_continuous_bars",
    } or any(nullable != "NO" or not default for nullable, default in revision_columns.values()):
        raise RuntimeError("CANONICAL_BAR_REVISION_COLUMNS_REQUIRED")

    cur.execute("SELECT current_schema()")
    schema_row = cur.fetchone()
    schema_name = str(schema_row[0] or "") if schema_row else ""
    if not schema_name:
        raise RuntimeError("STORAGE_SCHEMA_UNRESOLVED")
    cur.execute(
        """
        SELECT hypertable_name
        FROM timescaledb_information.hypertables
        WHERE hypertable_schema = %s
        """,
        (schema_name,),
    )
    hypertables = {str(row[0]) for row in cur.fetchall()}
    missing_hypertables = sorted(_REQUIRED_HYPERTABLES - hypertables)
    if missing_hypertables:
        raise RuntimeError(
            "TIMESCALEDB_HYPERTABLES_REQUIRED missing=" + ",".join(missing_hypertables)
        )

    cur.execute(
        """
        SELECT compression_enabled
        FROM timescaledb_information.hypertables
        WHERE hypertable_schema = %s
          AND hypertable_name = 'ticks'
        """,
        (schema_name,),
    )
    tick_compression_row = cur.fetchone()
    if tick_compression_row != (False,):
        raise RuntimeError(
            f"TIMESCALEDB_TICK_ROWSTORE_REQUIRED compression_enabled={tick_compression_row!r}"
        )
    cur.execute(
        """
        SELECT count(*)
        FROM timescaledb_information.jobs
        WHERE hypertable_schema = %s
          AND hypertable_name = 'ticks'
          AND proc_name IN ('policy_compression', 'policy_columnstore')
        """,
        (schema_name,),
    )
    tick_policy_row = cur.fetchone()
    if tick_policy_row != (0,):
        raise RuntimeError(
            f"TIMESCALEDB_TICK_COLUMNSTORE_POLICY_FORBIDDEN policy_count={tick_policy_row!r}"
        )
    cur.execute(
        """
        SELECT count(*)
        FROM timescaledb_information.chunks
        WHERE hypertable_schema = %s
          AND hypertable_name = 'ticks'
          AND is_compressed = true
        """,
        (schema_name,),
    )
    compressed_tick_chunks_row = cur.fetchone()
    if compressed_tick_chunks_row != (0,):
        raise RuntimeError(
            "TIMESCALEDB_COMPRESSED_TICK_CHUNKS_FORBIDDEN "
            f"chunk_count={compressed_tick_chunks_row!r}"
        )

    cur.execute("SELECT version FROM watchlist_state WHERE id = true")
    watchlist_state_row = cur.fetchone()
    if watchlist_state_row is None or int(watchlist_state_row[0] or 0) < 0:
        raise RuntimeError("WATCHLIST_STATE_REQUIRED")
    _verify_drawing_payload_storage(cur)


def _verify_versioned_storage_runtime_contract(cur: Any) -> None:
    cur.execute("SELECT version FROM storage_schema_versions ORDER BY version")
    stored_versions = tuple(int(row[0]) for row in cur.fetchall())
    if stored_versions != (_STORAGE_SCHEMA_VERSION,):
        raise RuntimeError(
            "STORAGE_SCHEMA_VERSION_REQUIRED "
            f"database={stored_versions!r} "
            f"runtime={(_STORAGE_SCHEMA_VERSION,)!r}; "
            "operator-controlled offline storage transition or recovery required"
        )
    _verify_storage_runtime_contract(cur)


class SchemaRepoMixin:
    """PostgreSQL repository mixin."""

    def verify_storage_runtime_contract(self) -> None:
        """Verify an existing database using the current code without mutation."""

        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                    ("aef-terminal-storage-schema",),
                )
                cur.execute("SELECT to_regclass('storage_schema_versions')")
                version_table_row = cur.fetchone()
                if version_table_row is None or version_table_row[0] is None:
                    raise RuntimeError(
                        "STORAGE_SCHEMA_VERSION_REQUIRED "
                        f"database=() runtime={(_STORAGE_SCHEMA_VERSION,)!r}; "
                        "operator-controlled storage bootstrap or recovery required"
                    )
                _verify_versioned_storage_runtime_contract(cur)

    def initialize(self) -> None:
        # Verify/bootstrap once per process and mark success only after commit.
        if getattr(self, "_initialized", False):
            return
        if not getattr(self, "_schema_owner", True):
            self._initialized = True
            return
        with self._init_lock:
            if getattr(self, "_initialized", False):
                return
            self._require_canonical_writer_lease()
            with self._connect() as conn:
                with conn.transaction(), conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ("aef-terminal-storage-schema",),
                    )
                    cur.execute("SELECT to_regclass('storage_schema_versions')")
                    version_table_row = cur.fetchone()
                    if version_table_row is not None and version_table_row[0] is not None:
                        _verify_versioned_storage_runtime_contract(cur)
                    else:
                        cur.execute(
                            """
                            SELECT relation_row.relname, relation_row.relkind
                            FROM pg_class AS relation_row
                            JOIN pg_namespace AS namespace_row
                              ON namespace_row.oid = relation_row.relnamespace
                            WHERE namespace_row.nspname = current_schema()
                              AND relation_row.relkind IN ('r', 'p', 'm', 'v', 'S', 'f')
                            ORDER BY relation_row.relname
                            """
                        )
                        existing_relations = tuple(
                            (str(row[0]), str(row[1])) for row in cur.fetchall()
                        )
                        if existing_relations:
                            raise RuntimeError(
                                "STORAGE_SCHEMA_UNVERSIONED_NONEMPTY "
                                f"relations={existing_relations!r}; "
                                "operator-controlled storage recovery required"
                            )
                        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
                        from psycopg import sql

                        timescaledb_schema = _timescaledb_api_schema(cur)
                        cur.execute("CREATE SEQUENCE canonical_bar_revision_sequence AS bigint")
                        cur.execute(
                            """
                            CREATE TABLE storage_schema_versions (
                                version integer PRIMARY KEY,
                                applied_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE bars (
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                provider text NOT NULL,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                ts timestamptz NOT NULL,
                                open double precision NOT NULL,
                                high double precision NOT NULL,
                                low double precision NOT NULL,
                                close double precision NOT NULL,
                                volume double precision NOT NULL DEFAULT 0,
                                source text NOT NULL DEFAULT 'unknown',
                                closed boolean NOT NULL DEFAULT true,
                                revision_sequence bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT bars_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT bars_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (instrument_id, route_fingerprint, timeframe, ts)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX bars_hygiene_recent_idx
                            ON bars (updated_at DESC, instrument_id, route_fingerprint, timeframe, source)
                            WHERE closed = false
                            """
                        )
                        cur.execute(
                            sql.SQL("SELECT {}.create_hypertable('bars', 'ts')").format(
                                sql.Identifier(timescaledb_schema)
                            )
                        )
                        cur.execute(
                            """
                            CREATE TABLE futures_contracts (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                contract_key text NOT NULL,
                                root text NOT NULL,
                                exchange text NOT NULL DEFAULT '',
                                currency text NOT NULL DEFAULT '',
                                local_symbol text NOT NULL DEFAULT '',
                                con_id bigint,
                                expiry text NOT NULL DEFAULT '',
                                contract_month text NOT NULL DEFAULT '',
                                first_notice_date date,
                                last_trade_date date,
                                is_current boolean NOT NULL DEFAULT false,
                                current_resolved_at timestamptz,
                                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT futures_contracts_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                PRIMARY KEY (provider, instrument_id, contract_key)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX futures_contracts_root_expiry_idx
                            ON futures_contracts (provider, root, expiry, last_trade_date)
                            """
                        )
                        cur.execute(
                            """
                            CREATE UNIQUE INDEX futures_contracts_current_uidx
                            ON futures_contracts (provider, instrument_id)
                            WHERE is_current = true
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE futures_roll_events (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                roll_ts timestamptz NOT NULL,
                                from_contract_key text NOT NULL DEFAULT '',
                                to_contract_key text NOT NULL,
                                roll_policy text NOT NULL,
                                reason text NOT NULL DEFAULT '',
                                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT futures_roll_events_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                PRIMARY KEY (provider, instrument_id, roll_ts, roll_policy)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX futures_roll_events_instrument_ts_idx
                            ON futures_roll_events (provider, instrument_id, roll_ts DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE futures_contract_bars (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                contract_key text NOT NULL,
                                timeframe text NOT NULL,
                                ts timestamptz NOT NULL,
                                open double precision NOT NULL,
                                high double precision NOT NULL,
                                low double precision NOT NULL,
                                close double precision NOT NULL,
                                volume double precision NOT NULL DEFAULT 0,
                                source text NOT NULL DEFAULT 'unknown',
                                closed boolean NOT NULL DEFAULT true,
                                revision_sequence bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT futures_contract_bars_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT futures_contract_bars_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (
                                    provider, instrument_id, route_fingerprint,
                                    contract_key, timeframe, ts
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX futures_contract_bars_route_tf_ts_idx
                            ON futures_contract_bars (
                                provider, instrument_id, route_fingerprint, timeframe, ts DESC
                            )
                            """
                        )
                        cur.execute(
                            sql.SQL(
                                "SELECT {}.create_hypertable('futures_contract_bars', 'ts')"
                            ).format(sql.Identifier(timescaledb_schema))
                        )
                        cur.execute(
                            """
                            CREATE TABLE futures_continuous_bars (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                series_type text NOT NULL,
                                roll_policy text NOT NULL,
                                timeframe text NOT NULL,
                                ts timestamptz NOT NULL,
                                open double precision NOT NULL,
                                high double precision NOT NULL,
                                low double precision NOT NULL,
                                close double precision NOT NULL,
                                volume double precision NOT NULL DEFAULT 0,
                                source text NOT NULL DEFAULT 'unknown',
                                closed boolean NOT NULL DEFAULT true,
                                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                                revision_sequence bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT futures_continuous_bars_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                PRIMARY KEY (provider, instrument_id, series_type, roll_policy, timeframe, ts)
                            )
                            """
                        )
                        cur.execute(
                            sql.SQL(
                                "SELECT {}.create_hypertable('futures_continuous_bars', 'ts')"
                            ).format(sql.Identifier(timescaledb_schema))
                        )
                        cur.execute(
                            """
                            CREATE TABLE history_coverage (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                timeframe text NOT NULL,
                                covered_from timestamptz NOT NULL,
                                covered_to timestamptz NOT NULL,
                                receipt_contract_version integer NOT NULL,
                                request_contract_version integer NOT NULL,
                                admission_contract_version integer NOT NULL,
                                request_mode text NOT NULL,
                                request_type text NOT NULL,
                                provider_source text NOT NULL,
                                provider_contract_id text NOT NULL,
                                provider_contract_type text NOT NULL,
                                data_type text NOT NULL,
                                provider_limit bigint,
                                response_count bigint NOT NULL,
                                completed_at timestamptz NOT NULL,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT history_coverage_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT history_coverage_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                CONSTRAINT history_coverage_range_chk CHECK (
                                    covered_from < covered_to AND covered_to <= completed_at
                                ),
                                CONSTRAINT history_coverage_versions_chk CHECK (
                                    receipt_contract_version > 0
                                    AND request_contract_version > 0
                                    AND admission_contract_version > 0
                                ),
                                CONSTRAINT history_coverage_authority_chk CHECK (
                                    request_mode <> ''
                                    AND request_type = 'historical'
                                    AND provider_source <> ''
                                    AND provider_contract_id <> ''
                                    AND provider_contract_type <> ''
                                    AND data_type = 'TRADES'
                                ),
                                CONSTRAINT history_coverage_count_chk CHECK (
                                    response_count >= 0
                                    AND (
                                        provider_limit IS NULL
                                        OR (
                                            provider_limit > 0
                                            AND response_count < provider_limit
                                        )
                                    )
                                ),
                                PRIMARY KEY (
                                    provider, instrument_id, route_fingerprint, timeframe,
                                    covered_from, covered_to,
                                    receipt_contract_version, request_contract_version,
                                    admission_contract_version, request_mode, request_type,
                                    provider_source, provider_contract_id,
                                    provider_contract_type, data_type
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX history_coverage_lookup_idx
                            ON history_coverage (
                                provider, instrument_id, route_fingerprint, timeframe,
                                covered_from, covered_to
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE trading_hours (
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                provider text NOT NULL,
                                symbol text NOT NULL,
                                timezone text NOT NULL DEFAULT '',
                                trading_hours text NOT NULL DEFAULT '',
                                liquid_hours text NOT NULL DEFAULT '',
                                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                fetched_at timestamptz NOT NULL DEFAULT now(),
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT trading_hours_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT trading_hours_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (instrument_id, route_fingerprint)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE trading_schedule_snapshots (
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                provider text NOT NULL,
                                symbol text NOT NULL,
                                provider_contract_id text NOT NULL DEFAULT '',
                                timezone text NOT NULL DEFAULT '',
                                trading_hours text NOT NULL DEFAULT '',
                                liquid_hours text NOT NULL DEFAULT '',
                                raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                fetched_at timestamptz NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT trading_schedule_snapshots_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT trading_schedule_snapshots_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (instrument_id, route_fingerprint, fetched_at)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX trading_schedule_snapshots_lookup_idx
                            ON trading_schedule_snapshots (instrument_id, route_fingerprint, fetched_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE trading_session_intervals (
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                provider text NOT NULL,
                                symbol text NOT NULL,
                                provider_contract_id text NOT NULL DEFAULT '',
                                session_date date NOT NULL,
                                session_type text NOT NULL,
                                opens_at timestamptz NOT NULL,
                                closes_at timestamptz NOT NULL,
                                status text NOT NULL DEFAULT 'open',
                                source_fetched_at timestamptz NOT NULL,
                                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT trading_session_intervals_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT trading_session_intervals_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE UNIQUE INDEX trading_session_intervals_uidx
                            ON trading_session_intervals (
                                instrument_id, route_fingerprint, session_type, opens_at, closes_at
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX trading_session_intervals_lookup_idx
                            ON trading_session_intervals (
                                instrument_id, route_fingerprint, session_type, opens_at, closes_at
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE settings (
                                scope text NOT NULL,
                                key text NOT NULL,
                                value jsonb NOT NULL,
                                mutation_changed_at_ms bigint NOT NULL DEFAULT 0,
                                mutation_writer_id uuid,
                                mutation_sequence bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT settings_scope_chk CHECK (
                                    scope IN ("""
                            + _SETTINGS_SCOPE_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT settings_mutation_order_chk CHECK (
                                    (
                                        mutation_writer_id IS NULL
                                        AND mutation_changed_at_ms = 0
                                        AND mutation_sequence = 0
                                    )
                                    OR (
                                        mutation_writer_id IS NOT NULL
                                        AND mutation_changed_at_ms > 0
                                        AND mutation_changed_at_ms <= 9007199254740991
                                        AND mutation_sequence > 0
                                        AND mutation_sequence <= 9007199254740991
                                    )
                                ),
                                PRIMARY KEY (scope, key)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE settings_state (
                                id boolean PRIMARY KEY DEFAULT true,
                                revision bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT settings_state_singleton_chk CHECK (id),
                                CONSTRAINT settings_state_revision_chk CHECK (
                                    revision >= 0 AND revision <= 9007199254740991
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO settings_state (id, revision)
                            VALUES (true, 0)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE discord_signal_messages (
                                message_id text PRIMARY KEY,
                                channel_id text NOT NULL,
                                author_id text,
                                author_name text,
                                published_at timestamptz,
                                content text,
                                reply_to_message_id text,
                                source_event text NOT NULL,
                                source_event_at timestamptz NOT NULL,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                deleted_at timestamptz,
                                CONSTRAINT discord_signal_messages_identity_chk CHECK (
                                    message_id ~ '^[0-9]+$'
                                    AND channel_id ~ '^[0-9]+$'
                                    AND (
                                        author_id IS NULL
                                        OR author_id ~ '^[0-9]+$'
                                    )
                                    AND (
                                        reply_to_message_id IS NULL
                                        OR reply_to_message_id ~ '^[0-9]+$'
                                    )
                                ),
                                CONSTRAINT discord_signal_messages_content_chk CHECK (
                                    content IS NULL OR content <> ''
                                ),
                                CONSTRAINT discord_signal_messages_source_event_chk CHECK (
                                    source_event IN (
                                        'snapshot',
                                        'message_create',
                                        'message_update',
                                        'message_delete'
                                    )
                                ),
                                CONSTRAINT discord_signal_messages_state_chk CHECK (
                                    (
                                        source_event = 'message_delete'
                                        AND deleted_at IS NOT NULL
                                        AND deleted_at = source_event_at
                                        AND num_nonnulls(
                                            author_id,
                                            author_name,
                                            published_at,
                                            content,
                                            reply_to_message_id
                                        ) = 0
                                    )
                                    OR (
                                        source_event <> 'message_delete'
                                        AND deleted_at IS NULL
                                        AND num_nonnulls(
                                            author_id,
                                            author_name,
                                            published_at,
                                            content
                                        ) = 4
                                    )
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX discord_signal_messages_active_idx
                            ON discord_signal_messages (
                                channel_id,
                                author_id,
                                published_at DESC,
                                message_id DESC
                            )
                            WHERE deleted_at IS NULL
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE instruments (
                                instrument_id text PRIMARY KEY,
                                instrument_key text NOT NULL,
                                provider text NOT NULL DEFAULT '',
                                provider_symbol text NOT NULL DEFAULT '',
                                provider_contract_id text NOT NULL DEFAULT '',
                                con_id bigint,
                                display_name text NOT NULL DEFAULT '',
                                asset_class text NOT NULL DEFAULT '',
                                identity_scope text NOT NULL,
                                session jsonb NOT NULL DEFAULT '{}'::jsonb,
                                watchlist boolean NOT NULL DEFAULT false,
                                watchlist_order integer,
                                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT instruments_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT instruments_provider_contract_required_chk CHECK (
                                    (
                                        identity_scope = 'root'
                                        AND asset_class = 'future'
                                        AND provider_contract_id = ''
                                        AND con_id IS NULL
                                    )
                                    OR (
                                        identity_scope = 'contract'
                                        AND provider_contract_id <> ''
                                    )
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX instruments_watchlist_order_idx
                            ON instruments (watchlist, watchlist_order ASC NULLS LAST, instrument_id)
                            """
                        )
                        cur.execute(
                            """
                            CREATE UNIQUE INDEX instruments_provider_contract_uidx
                            ON instruments (provider, provider_contract_id)
                            WHERE provider <> '' AND provider_contract_id <> ''
                            """
                        )
                        cur.execute(
                            """
                            CREATE UNIQUE INDEX instruments_watchlist_order_uidx
                            ON instruments (watchlist_order)
                            WHERE watchlist = true
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE watchlist_state (
                                id boolean PRIMARY KEY DEFAULT true CHECK (id),
                                version bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now()
                            )
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO watchlist_state (id, version, updated_at)
                            VALUES (true, 0, now())
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE drawings (
                                id text PRIMARY KEY,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                payload jsonb NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT drawings_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX drawings_identity_timeframe_updated_idx
                            ON drawings (instrument_id, route_fingerprint, timeframe, updated_at ASC, id ASC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE option_targets (
                                id text NOT NULL,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                payload jsonb NOT NULL,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT option_targets_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (instrument_id, route_fingerprint, id)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX option_targets_identity_timeframe_updated_idx
                            ON option_targets (instrument_id, route_fingerprint, timeframe, updated_at ASC, id ASC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX option_targets_updated_idx
                            ON option_targets (
                                updated_at ASC,
                                instrument_id ASC,
                                route_fingerprint ASC,
                                id ASC
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE price_alerts (
                                id text PRIMARY KEY,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                payload jsonb NOT NULL,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT price_alerts_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX price_alerts_identity_timeframe_updated_idx
                            ON price_alerts (instrument_id, route_fingerprint, timeframe, updated_at ASC, id ASC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE gex_snapshots (
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                provider_symbol text NOT NULL,
                                captured_at timestamptz NOT NULL,
                                source text NOT NULL,
                                payload jsonb NOT NULL,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT gex_snapshots_source_chk
                                    CHECK (source IN ('gex:ibkr', 'gex:ibkr-live')),
                                CONSTRAINT gex_snapshots_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (instrument_id, route_fingerprint, captured_at, source)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX gex_snapshots_route_captured_idx
                            ON gex_snapshots (instrument_id, route_fingerprint, captured_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE quote_snapshots (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                symbol text NOT NULL,
                                ts timestamptz NOT NULL,
                                price double precision,
                                bid double precision,
                                ask double precision,
                                last double precision,
                                close double precision,
                                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT quote_snapshots_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT quote_snapshots_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (provider, instrument_id, route_fingerprint, ts)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX quote_snapshots_route_ts_idx
                            ON quote_snapshots (instrument_id, route_fingerprint, ts DESC)
                            """
                        )
                        cur.execute(
                            sql.SQL("SELECT {}.create_hypertable('quote_snapshots', 'ts')").format(
                                sql.Identifier(timescaledb_schema)
                            )
                        )
                        cur.execute(
                            """
                            CREATE TABLE paper_orders (
                                id text PRIMARY KEY,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                contract_scope_key text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'scope_key') STORED NOT NULL,
                                contract_scope_kind text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'scope_kind') STORED NOT NULL,
                                contract_provider text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'provider') STORED NOT NULL,
                                contract_sec_type text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'sec_type') STORED,
                                contract_provider_contract_id text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'provider_contract_id') STORED NOT NULL,
                                contract_con_id bigint GENERATED ALWAYS AS ((payload -> 'paper_contract' ->> 'con_id')::bigint) STORED,
                                contract_exchange text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'exchange') STORED,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                side text NOT NULL CHECK (side IN ('long', 'short')),
                                order_type text NOT NULL,
                                status text NOT NULL DEFAULT 'pending',
                                qty double precision NOT NULL DEFAULT 1,
                                entry double precision NOT NULL,
                                stop_loss double precision,
                                target double precision,
                                use_stop_loss boolean NOT NULL DEFAULT true,
                                use_target boolean NOT NULL DEFAULT true,
                                created_at timestamptz NOT NULL,
                                updated_at timestamptz NOT NULL,
                                filled_at timestamptz,
                                fill_price double precision,
                                role text NOT NULL DEFAULT 'entry',
                                position_id text,
                                parent_order_id text,
                                oco_group_id text,
                                reduce_only boolean NOT NULL DEFAULT false,
                                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                CONSTRAINT paper_orders_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                CONSTRAINT paper_orders_contract_scope_chk CHECK (
                                    contract_scope_key <> ''
                                    AND contract_provider <> ''
                                    AND contract_provider_contract_id <> ''
                                    AND (
                                        (
                                            contract_scope_kind = 'instrument'
                                            AND contract_sec_type IS NULL
                                            AND contract_con_id IS NULL
                                            AND contract_exchange IS NULL
                                        )
                                        OR (
                                            contract_scope_kind = 'option'
                                            AND contract_sec_type IN ('OPT', 'FOP')
                                            AND contract_con_id > 0
                                            AND contract_exchange <> ''
                                        )
                                    )
                                ),
                                CONSTRAINT paper_orders_contract_provider_chk CHECK (
                                    contract_provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_orders_identity_status_idx
                            ON paper_orders (
                                instrument_id, route_fingerprint, contract_scope_key,
                                status, updated_at DESC
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_orders_status_updated_idx
                            ON paper_orders (status, updated_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_orders_activity_idx
                            ON paper_orders ((COALESCE(filled_at, updated_at, created_at)) DESC, updated_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_orders_position_pending_idx
                            ON paper_orders (contract_scope_key, position_id, role, updated_at DESC)
                            WHERE status = 'pending'
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE paper_positions (
                                id text PRIMARY KEY,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                contract_scope_key text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'scope_key') STORED NOT NULL,
                                contract_scope_kind text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'scope_kind') STORED NOT NULL,
                                contract_provider text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'provider') STORED NOT NULL,
                                contract_sec_type text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'sec_type') STORED,
                                contract_provider_contract_id text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'provider_contract_id') STORED NOT NULL,
                                contract_con_id bigint GENERATED ALWAYS AS ((payload -> 'paper_contract' ->> 'con_id')::bigint) STORED,
                                contract_exchange text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'exchange') STORED,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                status text NOT NULL DEFAULT 'open',
                                qty double precision NOT NULL DEFAULT 0,
                                avg_entry double precision,
                                opened_at timestamptz NOT NULL,
                                updated_at timestamptz NOT NULL,
                                closed_at timestamptz,
                                realized_pnl double precision NOT NULL DEFAULT 0,
                                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                CONSTRAINT paper_positions_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                CONSTRAINT paper_positions_contract_scope_chk CHECK (
                                    contract_scope_key <> ''
                                    AND contract_provider <> ''
                                    AND contract_provider_contract_id <> ''
                                    AND (
                                        (
                                            contract_scope_kind = 'instrument'
                                            AND contract_sec_type IS NULL
                                            AND contract_con_id IS NULL
                                            AND contract_exchange IS NULL
                                        )
                                        OR (
                                            contract_scope_kind = 'option'
                                            AND contract_sec_type IN ('OPT', 'FOP')
                                            AND contract_con_id > 0
                                            AND contract_exchange <> ''
                                        )
                                    )
                                ),
                                CONSTRAINT paper_positions_contract_provider_chk CHECK (
                                    contract_provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_positions_identity_status_idx
                            ON paper_positions (
                                instrument_id, route_fingerprint, contract_scope_key,
                                status, updated_at DESC
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE paper_fills (
                                id bigserial PRIMARY KEY,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                contract_scope_key text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'scope_key') STORED NOT NULL,
                                contract_scope_kind text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'scope_kind') STORED NOT NULL,
                                contract_provider text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'provider') STORED NOT NULL,
                                contract_sec_type text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'sec_type') STORED,
                                contract_provider_contract_id text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'provider_contract_id') STORED NOT NULL,
                                contract_con_id bigint GENERATED ALWAYS AS ((payload -> 'paper_contract' ->> 'con_id')::bigint) STORED,
                                contract_exchange text GENERATED ALWAYS AS (payload -> 'paper_contract' ->> 'exchange') STORED,
                                order_id text NOT NULL,
                                position_id text,
                                symbol text NOT NULL,
                                timeframe text NOT NULL,
                                side text NOT NULL CHECK (side IN ('long', 'short')),
                                qty double precision NOT NULL,
                                price double precision NOT NULL,
                                role text NOT NULL DEFAULT 'entry',
                                reduce_only boolean NOT NULL DEFAULT false,
                                pnl_points double precision,
                                filled_at timestamptz NOT NULL,
                                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                CONSTRAINT paper_fills_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                CONSTRAINT paper_fills_contract_scope_chk CHECK (
                                    contract_scope_key <> ''
                                    AND contract_provider <> ''
                                    AND contract_provider_contract_id <> ''
                                    AND (
                                        (
                                            contract_scope_kind = 'instrument'
                                            AND contract_sec_type IS NULL
                                            AND contract_con_id IS NULL
                                            AND contract_exchange IS NULL
                                        )
                                        OR (
                                            contract_scope_kind = 'option'
                                            AND contract_sec_type IN ('OPT', 'FOP')
                                            AND contract_con_id > 0
                                            AND contract_exchange <> ''
                                        )
                                    )
                                ),
                                CONSTRAINT paper_fills_contract_provider_chk CHECK (
                                    contract_provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_fills_position_time_idx
                            ON paper_fills (contract_scope_key, position_id, filled_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE UNIQUE INDEX paper_fills_order_unique_idx
                            ON paper_fills (contract_scope_key, order_id)
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_fills_identity_time_idx
                            ON paper_fills (
                                instrument_id, route_fingerprint, contract_scope_key,
                                filled_at DESC
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_fills_filled_at_idx
                            ON paper_fills (filled_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE paper_trade_events (
                                id bigserial PRIMARY KEY,
                                instrument_id text GENERATED ALWAYS AS (payload ->> 'instrument_id') STORED NOT NULL,
                                route_fingerprint text GENERATED ALWAYS AS (payload ->> 'route_fingerprint') STORED NOT NULL,
                                event_type text NOT NULL,
                                order_id text,
                                trade_id text,
                                symbol text,
                                timeframe text,
                                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                                created_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT paper_trade_events_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_trade_events_created_idx
                            ON paper_trade_events (created_at DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX paper_trade_events_identity_created_idx
                            ON paper_trade_events (
                                instrument_id, route_fingerprint, created_at DESC
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE ticks (
                                ingest_seq bigint GENERATED BY DEFAULT AS IDENTITY,
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                ts timestamptz NOT NULL,
                                price double precision NOT NULL,
                                volume integer NOT NULL,
                                delta_sign smallint NOT NULL CHECK (delta_sign IN (-1, 0, 1)),
                                tick_type text NOT NULL DEFAULT 'last',
                                bid double precision,
                                ask double precision,
                                exchange text NOT NULL DEFAULT '',
                                conditions text[] NOT NULL DEFAULT ARRAY[]::text[],
                                received_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT ticks_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT ticks_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                )
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX ticks_route_ts_ingest_idx
                            ON ticks (provider, instrument_id, route_fingerprint, ts DESC, ingest_seq DESC)
                            """
                        )
                        cur.execute(
                            """
                            CREATE INDEX ticks_route_price_ts_idx
                            ON ticks (provider, instrument_id, route_fingerprint, price, ts DESC)
                            """
                        )
                        cur.execute(
                            sql.SQL(
                                "SELECT {}.create_hypertable('ticks', 'ts', chunk_time_interval => make_interval(mins => %s))"
                            ).format(sql.Identifier(timescaledb_schema)),
                            (max(AppConfig().tick_chunk_minutes, 1),),
                        )
                        cur.execute(
                            """
                            CREATE TABLE tick_delta_1m_rollup (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                bucket timestamptz NOT NULL,
                                total_volume bigint NOT NULL DEFAULT 0,
                                net_delta bigint NOT NULL DEFAULT 0,
                                trade_count bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT tick_delta_1m_rollup_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT tick_delta_1m_rollup_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (provider, instrument_id, route_fingerprint, bucket)
                            )
                            """
                        )
                        cur.execute(
                            """
                            CREATE TABLE tick_volume_profile_5m_rollup (
                                provider text NOT NULL,
                                instrument_id text NOT NULL,
                                route_fingerprint text NOT NULL,
                                bucket timestamptz NOT NULL,
                                price double precision NOT NULL,
                                total_volume bigint NOT NULL DEFAULT 0,
                                buy_volume bigint NOT NULL DEFAULT 0,
                                sell_volume bigint NOT NULL DEFAULT 0,
                                net_delta bigint NOT NULL DEFAULT 0,
                                trade_count bigint NOT NULL DEFAULT 0,
                                updated_at timestamptz NOT NULL DEFAULT now(),
                                CONSTRAINT tick_volume_profile_5m_rollup_provider_chk CHECK (
                                    provider IN ("""
                            + _STORAGE_PROVIDER_VALUES_SQL
                            + """)
                                ),
                                CONSTRAINT tick_volume_profile_5m_rollup_route_identity_chk CHECK (
                                    instrument_id <> '' AND route_fingerprint <> ''
                                ),
                                PRIMARY KEY (provider, instrument_id, route_fingerprint, bucket, price)
                            )
                            """
                        )
                        _verify_storage_runtime_contract(cur)
                        cur.execute(
                            """
                            INSERT INTO storage_schema_versions (version)
                            VALUES (%s)
                            """,
                            (_STORAGE_SCHEMA_VERSION,),
                        )
            # Mark initialized only after verification/bootstrap commits successfully.
            self._initialized = True
