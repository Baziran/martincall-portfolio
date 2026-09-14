from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

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
    require_current_settings_scope,
    watchlist_presentation_payload,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.watchlist_contract import WATCHLIST_SETTINGS_LOCK_KEY


_MAX_SAFE_SETTINGS_REVISION = 9_007_199_254_740_991


def read_settings_revision(cur: Any) -> int:
    cur.execute("SELECT revision FROM settings_state WHERE id = true")
    row = cur.fetchone()
    revision = int(row[0]) if row is not None else -1
    if not 0 <= revision <= _MAX_SAFE_SETTINGS_REVISION:
        raise RuntimeError("SETTINGS_REVISION_STORAGE_INVALID")
    return revision


def advance_settings_revision(cur: Any) -> int:
    cur.execute(
        """
        UPDATE settings_state
        SET revision = revision + 1,
            updated_at = now()
        WHERE id = true
          AND revision < %s
        RETURNING revision
        """,
        (_MAX_SAFE_SETTINGS_REVISION,),
    )
    row = cur.fetchone()
    revision = int(row[0]) if row is not None else -1
    if not 0 < revision <= _MAX_SAFE_SETTINGS_REVISION:
        raise RuntimeError("SETTINGS_REVISION_EXHAUSTED")
    return revision


class SettingsRepoMixin:
    """PostgreSQL repository mixin."""

    def _validate_client_settings_contract(
        self,
        values: dict[str, Any],
        *,
        instrument_exists: Callable[[str], bool] | None,
    ) -> None:
        invalid_keys = invalid_persisted_client_setting_keys(values)
        if invalid_keys:
            raise ValueError(f"STORAGE_CLIENT_SETTINGS_FORBIDDEN keys={invalid_keys!r}")
        invalid_selection_keys = invalid_workspace_instrument_selection_keys(
            values,
            instrument_exists=instrument_exists,
        )
        if invalid_selection_keys:
            raise ValueError(
                f"STORAGE_CLIENT_SETTINGS_INSTRUMENT_INVALID keys={invalid_selection_keys!r}"
            )
        invalid_instrument_keys = invalid_client_setting_instrument_keys(
            values,
            instrument_exists=instrument_exists,
        )
        if invalid_instrument_keys:
            raise ValueError(
                f"STORAGE_CLIENT_SETTINGS_INSTRUMENT_INVALID keys={invalid_instrument_keys!r}"
            )
        invalid_values = invalid_client_setting_value_keys(
            values,
            instrument_exists=instrument_exists,
        )
        if invalid_values:
            raise ValueError(f"STORAGE_CLIENT_SETTINGS_INVALID keys={invalid_values!r}")

    def _validate_server_settings_contract(
        self,
        values: dict[str, Any],
        *,
        instrument_exists: Callable[[str], bool] | None,
    ) -> None:
        invalid_keys = invalid_persisted_server_setting_keys(values)
        if invalid_keys:
            raise ValueError(f"STORAGE_SERVER_SETTINGS_FORBIDDEN keys={invalid_keys!r}")
        invalid_values = invalid_server_setting_value_keys(
            values,
            instrument_exists=instrument_exists,
        )
        if invalid_values:
            raise ValueError(f"STORAGE_SERVER_SETTINGS_INVALID keys={invalid_values!r}")

    def _validate_watchlist_presentation_contract(
        self,
        values: dict[str, Any],
        *,
        instrument_exists: Callable[[str], bool],
    ) -> None:
        invalid_keys: list[str] = []
        for key, value in values.items():
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
                invalid_keys.append(key if isinstance(key, str) else repr(key))
                continue
            if not instrument_exists(identity):
                invalid_keys.append(identity)
        if invalid_keys:
            raise ValueError(
                "WATCHLIST_PRESENTATION_STORAGE_CONTRACT_REQUIRED "
                f"keys={sorted(set(invalid_keys))!r}"
            )

    def _validate_settings_contract(
        self,
        scope: str,
        values: dict[str, Any],
        *,
        instrument_exists: Callable[[str], bool] | None,
    ) -> None:
        if scope == CLIENT_SETTINGS_SCOPE:
            self._validate_client_settings_contract(
                values,
                instrument_exists=instrument_exists,
            )
        elif scope == SERVER_SETTINGS_SCOPE:
            self._validate_server_settings_contract(
                values,
                instrument_exists=instrument_exists,
            )
        else:
            self._validate_watchlist_presentation_contract(
                values,
                instrument_exists=(instrument_exists or (lambda _identity: False)),
            )

    @staticmethod
    def _cursor_current_instrument_ids(cur: Any) -> set[str]:
        cur.execute(
            """
            SELECT instrument_id
            FROM instruments
            WHERE watchlist = true
            ORDER BY instrument_id
            """
        )
        return {row[0] for row in cur.fetchall()}

    def upsert_setting(
        self,
        scope: str,
        key: str,
        value: dict[str, Any] | list[Any] | str,
    ) -> int:
        exact_scope = require_current_settings_scope(scope)
        if exact_scope == WATCHLIST_PRESENTATION_SCOPE:
            raise ValueError("WATCHLIST_PRESENTATION_OWNER_REQUIRED")
        setting_values = {key: value}
        self._validate_settings_contract(
            exact_scope,
            setting_values,
            instrument_exists=None,
        )
        self._psycopg()
        from psycopg.types.json import Jsonb

        jsonb = Jsonb(value)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                self._settings_cache.clear()
                current_instrument_ids = self._cursor_current_instrument_ids(cur)
                self._validate_settings_contract(
                    exact_scope,
                    setting_values,
                    instrument_exists=lambda instrument_id: instrument_id in current_instrument_ids,
                )
                cur.execute(
                    """
                    INSERT INTO settings AS current_setting (scope, key, value, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (scope, key) DO UPDATE SET
                        value = EXCLUDED.value,
                        mutation_changed_at_ms = 0,
                        mutation_writer_id = NULL,
                        mutation_sequence = 0,
                        updated_at = now()
                    WHERE current_setting.value IS DISTINCT FROM EXCLUDED.value
                       OR current_setting.mutation_changed_at_ms <> 0
                       OR current_setting.mutation_writer_id IS NOT NULL
                       OR current_setting.mutation_sequence <> 0
                    RETURNING 1
                    """,
                    (exact_scope, key, jsonb),
                )
                settings_revision = (
                    advance_settings_revision(cur)
                    if cur.fetchone() is not None
                    else read_settings_revision(cur)
                )
        self._settings_cache.clear()
        return settings_revision

    def upsert_settings(
        self,
        scope: str,
        values: dict[str, Any],
        *,
        mutation_writer_id: str = "",
        mutation_orders: dict[str, tuple[int, int]] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        exact_scope = require_current_settings_scope(scope)
        if exact_scope == WATCHLIST_PRESENTATION_SCOPE:
            raise ValueError("WATCHLIST_PRESENTATION_OWNER_REQUIRED")
        if not values:
            raise ValueError("SETTINGS_PAYLOAD_EMPTY")
        exact_mutation_orders = dict(mutation_orders or {})
        if (mutation_writer_id == "") != (not exact_mutation_orders):
            raise ValueError("SETTINGS_MUTATION_ORDER_INVALID")
        if mutation_writer_id and exact_scope != CLIENT_SETTINGS_SCOPE:
            raise ValueError("SETTINGS_MUTATION_WRITER_SCOPE_INVALID")
        if mutation_writer_id:
            try:
                parsed_writer_id = UUID(mutation_writer_id)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("SETTINGS_MUTATION_WRITER_INVALID") from exc
            if parsed_writer_id.version != 4 or str(parsed_writer_id) != mutation_writer_id:
                raise ValueError("SETTINGS_MUTATION_WRITER_INVALID")
            if set(exact_mutation_orders) != set(values):
                raise ValueError("SETTINGS_MUTATION_ORDER_INVALID")
            for order in exact_mutation_orders.values():
                if not isinstance(order, tuple) or len(order) != 2:
                    raise ValueError("SETTINGS_MUTATION_ORDER_INVALID")
                changed_at_ms, sequence = order
                if (
                    not isinstance(changed_at_ms, int)
                    or isinstance(changed_at_ms, bool)
                    or not 0 < changed_at_ms <= 9_007_199_254_740_991
                    or not isinstance(sequence, int)
                    or isinstance(sequence, bool)
                    or not 0 < sequence <= 9_007_199_254_740_991
                ):
                    raise ValueError("SETTINGS_MUTATION_ORDER_INVALID")
        exact_values = dict(values)
        self._validate_settings_contract(
            exact_scope,
            exact_values,
            instrument_exists=None,
        )
        self._psycopg()
        from psycopg.types.json import Jsonb

        rows = [(exact_scope, key, Jsonb(value)) for key, value in exact_values.items()]
        applied_keys: set[str] = set()
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                self._settings_cache.clear()
                current_instrument_ids = self._cursor_current_instrument_ids(cur)
                self._validate_settings_contract(
                    exact_scope,
                    exact_values,
                    instrument_exists=lambda instrument_id: instrument_id in current_instrument_ids,
                )
                for row_scope, key, value in rows:
                    changed_at_ms, sequence = exact_mutation_orders.get(key, (0, 0))
                    cur.execute(
                        """
                        INSERT INTO settings AS current_setting (
                            scope,
                            key,
                            value,
                            mutation_changed_at_ms,
                            mutation_writer_id,
                            mutation_sequence,
                            updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (scope, key) DO UPDATE SET
                            value = EXCLUDED.value,
                            mutation_changed_at_ms = EXCLUDED.mutation_changed_at_ms,
                            mutation_writer_id = EXCLUDED.mutation_writer_id,
                            mutation_sequence = EXCLUDED.mutation_sequence,
                            updated_at = now()
                        WHERE (
                                  EXCLUDED.mutation_writer_id IS NULL
                              AND (
                                     current_setting.value IS DISTINCT FROM EXCLUDED.value
                                  OR current_setting.mutation_changed_at_ms <> 0
                                  OR current_setting.mutation_writer_id IS NOT NULL
                                  OR current_setting.mutation_sequence <> 0
                              )
                        )
                           OR (
                                  EXCLUDED.mutation_writer_id IS NOT NULL
                              AND (
                                     current_setting.mutation_writer_id IS NULL
                                  OR (
                                      current_setting.mutation_changed_at_ms,
                                      current_setting.mutation_writer_id,
                                      current_setting.mutation_sequence
                                  ) < (
                                      EXCLUDED.mutation_changed_at_ms,
                                      EXCLUDED.mutation_writer_id,
                                      EXCLUDED.mutation_sequence
                                  )
                              )
                        )
                        RETURNING key
                        """,
                        (
                            row_scope,
                            key,
                            value,
                            changed_at_ms,
                            mutation_writer_id or None,
                            sequence,
                        ),
                    )
                    applied = cur.fetchone()
                    if applied is not None:
                        applied_keys.add(str(applied[0]))
                cur.execute(
                    """
                    SELECT key,
                           value,
                           mutation_changed_at_ms,
                           mutation_writer_id::text,
                           mutation_sequence
                    FROM settings
                    WHERE scope = %s
                      AND key = ANY(%s)
                    ORDER BY key
                    """,
                    (exact_scope, list(exact_values)),
                )
                authoritative_rows = cur.fetchall()
                if {str(row[0]) for row in authoritative_rows} != set(exact_values):
                    raise RuntimeError("SETTINGS_MUTATION_ACK_STORAGE_INVALID")
                self._validate_settings_contract(
                    exact_scope,
                    {str(row[0]): row[1] for row in authoritative_rows},
                    instrument_exists=lambda instrument_id: instrument_id in current_instrument_ids,
                )
                mutation_results: dict[str, dict[str, Any]] = {}
                for key, value, changed_at_ms, writer_id, sequence in authoritative_rows:
                    result_key = str(key)
                    authoritative_order = (
                        int(changed_at_ms),
                        str(writer_id) if writer_id is not None else None,
                        int(sequence),
                    )
                    submitted_changed_at_ms, submitted_sequence = exact_mutation_orders.get(
                        result_key,
                        (0, 0),
                    )
                    submitted_order = (
                        submitted_changed_at_ms,
                        mutation_writer_id or None,
                        submitted_sequence,
                    )
                    if result_key in applied_keys:
                        if (
                            authoritative_order != submitted_order
                            or value != exact_values[result_key]
                        ):
                            raise RuntimeError("SETTINGS_MUTATION_APPLY_MISMATCH")
                        outcome = "applied"
                    elif authoritative_order == submitted_order:
                        if value != exact_values[result_key]:
                            raise RuntimeError("SETTINGS_MUTATION_REPLAY_MISMATCH")
                        outcome = "replayed"
                    elif (
                        mutation_writer_id
                        and authoritative_order[0] > 0
                        and authoritative_order > submitted_order
                    ):
                        outcome = "rejected"
                    else:
                        raise RuntimeError("SETTINGS_MUTATION_ACK_STORAGE_INVALID")
                    mutation_results[result_key] = {
                        "outcome": outcome,
                        "value": value,
                        "changed_at_ms": authoritative_order[0],
                        "writer_id": authoritative_order[1],
                        "sequence": authoritative_order[2],
                    }
                settings_revision = (
                    advance_settings_revision(cur) if applied_keys else read_settings_revision(cur)
                )
        self._settings_cache.clear()
        return mutation_results, settings_revision

    def read_settings_snapshot(
        self,
        scope: str,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], int, int]:
        exact_scope = require_current_settings_scope(scope)
        # lightweight in-memory cache to reduce repeated UI calls
        now = time.time()
        cached = self._settings_cache.get(exact_scope)
        if cached is not None:
            ts, value, mutation_orders, mutation_changed_at_ms, settings_revision = cached
            if now - ts <= float(self._settings_cache_ttl):
                return (
                    dict(value),
                    {key: dict(order) for key, order in mutation_orders.items()},
                    mutation_changed_at_ms,
                    settings_revision,
                )
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                cur.execute(
                    """
                    SELECT key,
                           value,
                           mutation_changed_at_ms,
                           mutation_writer_id::text,
                           mutation_sequence
                    FROM settings
                    WHERE scope = %s
                    ORDER BY key
                    """,
                    (exact_scope,),
                )
                rows = cur.fetchall()
                current_instrument_ids = self._cursor_current_instrument_ids(cur)
                result = {row[0]: row[1] for row in rows}
                mutation_changed_at_ms = max((int(row[2]) for row in rows), default=0)
                mutation_orders = {
                    str(row[0]): {
                        "changed_at_ms": int(row[2]),
                        "writer_id": str(row[3]) if row[3] is not None else None,
                        "sequence": int(row[4]),
                    }
                    for row in rows
                }
                settings_revision = read_settings_revision(cur)
                self._validate_settings_contract(
                    exact_scope,
                    result,
                    instrument_exists=lambda instrument_id: instrument_id in current_instrument_ids,
                )
                self._settings_cache[exact_scope] = (
                    now,
                    dict(result),
                    {key: dict(order) for key, order in mutation_orders.items()},
                    mutation_changed_at_ms,
                    settings_revision,
                )
        return result, mutation_orders, mutation_changed_at_ms, settings_revision

    def read_settings(self, scope: str) -> dict[str, Any]:
        values, _mutation_orders, _mutation_changed_at_ms, _settings_revision = (
            self.read_settings_snapshot(scope)
        )
        return values

    def read_setting(self, scope: str, key: str, default: Any = None) -> Any:
        exact_scope = require_current_settings_scope(scope)
        if not isinstance(key, str) or not key:
            raise ValueError("STORAGE_SETTINGS_KEY_INVALID")
        if exact_scope == CLIENT_SETTINGS_SCOPE:
            raise ValueError("CLIENT_SETTINGS_BULK_READ_REQUIRED")
        if exact_scope == WATCHLIST_PRESENTATION_SCOPE:
            raise ValueError("WATCHLIST_PRESENTATION_OWNER_REQUIRED")
        invalid_keys = invalid_persisted_server_setting_keys((key,))
        if invalid_keys:
            raise ValueError(f"STORAGE_SERVER_SETTINGS_FORBIDDEN keys={invalid_keys!r}")
        now = time.time()
        cached = self._settings_cache.get(exact_scope)
        if cached is not None:
            ts, value, _mutation_orders, _mutation_changed_at_ms, _settings_revision = cached
            if now - ts <= float(self._settings_cache_ttl):
                return value.get(key, default)
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                cur.execute(
                    "SELECT value FROM settings WHERE scope = %s AND key = %s LIMIT 1",
                    (exact_scope, key),
                )
                row = cur.fetchone()
                current_instrument_ids = (
                    self._cursor_current_instrument_ids(cur) if row is not None else set()
                )
        if row is None:
            return default
        value = row[0]
        self._validate_server_settings_contract(
            {key: value},
            instrument_exists=lambda instrument_id: instrument_id in current_instrument_ids,
        )
        return value

    def read_setting_snapshot(
        self,
        scope: str,
        key: str,
    ) -> tuple[Any, bool, int]:
        """Read one exact server setting and its same-transaction revision."""

        exact_scope = require_current_settings_scope(scope)
        if not isinstance(key, str) or not key:
            raise ValueError("STORAGE_SETTINGS_KEY_INVALID")
        if exact_scope == CLIENT_SETTINGS_SCOPE:
            raise ValueError("CLIENT_SETTINGS_BULK_READ_REQUIRED")
        if exact_scope == WATCHLIST_PRESENTATION_SCOPE:
            raise ValueError("WATCHLIST_PRESENTATION_OWNER_REQUIRED")
        invalid_keys = invalid_persisted_server_setting_keys((key,))
        if invalid_keys:
            raise ValueError(f"STORAGE_SERVER_SETTINGS_FORBIDDEN keys={invalid_keys!r}")
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                cur.execute(
                    "SELECT value FROM settings WHERE scope = %s AND key = %s LIMIT 1",
                    (exact_scope, key),
                )
                row = cur.fetchone()
                current_instrument_ids = (
                    self._cursor_current_instrument_ids(cur) if row is not None else set()
                )
                settings_revision = read_settings_revision(cur)
        if row is None:
            return None, False, settings_revision
        value = row[0]
        self._validate_server_settings_contract(
            {key: value},
            instrument_exists=lambda instrument_id: instrument_id in current_instrument_ids,
        )
        return value, True, settings_revision
