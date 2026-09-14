from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.data.instrument_identity import (
    identity_payload,
    instrument_asset_class,
    futures_root as instrument_futures_root,
    instrument_identity_scope,
    instrument_is_futures_root,
    instrument_key,
    instrument_provider,
    provider_contract_id,
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
    require_exact_instrument_id_sequence,
    require_exact_positive_number,
    require_provider_identity,
)
from aef_terminal.data.provider_contract import FuturesRouteTransition
from aef_terminal.settings_contract import (
    CLIENT_SETTINGS_SCOPE,
    GEX_SCHEDULER_SETTING_KEY,
    OPTION_TARGET_CAPS_SETTING_KEY,
    SERVER_SETTINGS_SCOPE,
    WATCHLIST_PRESENTATION_SCOPE,
    invalid_client_setting_instrument_keys,
    client_setting_keys_for_instrument,
    invalid_client_setting_value_keys,
    invalid_persisted_client_setting_keys,
    invalid_persisted_server_setting_keys,
    invalid_server_setting_value_keys,
    invalid_workspace_instrument_selection_keys,
    require_watchlist_display_mode,
    server_setting_mutations_without_instrument,
    watchlist_presentation_payload,
)
from aef_terminal.storage.repos.settings import advance_settings_revision, read_settings_revision
from aef_terminal.watchlist_contract import (
    WATCHLIST_SETTINGS_LOCK_KEY,
    WatchlistOrderError,
    WatchlistRetirementSettingsSnapshot,
)


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeError(f"{field} must be a JSON object")
    return dict(value)


def _instrument_row_payload(row: Sequence[Any]) -> dict[str, Any]:
    (
        instrument_id,
        instrument_key,
        provider,
        provider_symbol,
        provider_contract_id,
        con_id,
        display_name,
        asset_class,
        identity_scope,
        session,
        watchlist,
        watchlist_order,
        watchlist_version,
        metadata,
        current_contract,
        provider_session,
        presentation,
    ) = row
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    key = require_exact_identity_text(
        instrument_key,
        field="instrument_key",
    )
    provider_key = require_exact_identity_text(
        provider,
        field="provider",
    )
    symbol = require_exact_identity_text(
        provider_symbol,
        field="provider_symbol",
    )
    contract_id = require_exact_identity_text(
        provider_contract_id,
        field="provider_contract_id",
        allow_empty=True,
    )
    con_id_value = parse_exact_positive_decimal_provider_id(con_id) or None
    if watchlist_version is None:
        raise RuntimeError("WATCHLIST_STATE_REQUIRED")
    asset_class_value = require_exact_identity_text(
        asset_class,
        field="asset_class",
    )
    identity_scope_value = require_exact_identity_text(
        identity_scope,
        field="identity_scope",
    )
    display_name_value = require_exact_identity_text(
        display_name,
        field="display",
    )
    metadata_value = _json_object(metadata, field="metadata")
    raw_stored_identity = metadata_value.get("contract_identity")
    if raw_stored_identity is not None and not isinstance(raw_stored_identity, dict):
        raise RuntimeError("STORED_CONTRACT_IDENTITY_OBJECT_REQUIRED")
    stored_identity = dict(raw_stored_identity or {})
    forbidden_stored_identity_fields = {
        "current_contract",
        "provider",
        "provider_contract_id",
        "con_id",
        "asset_class",
        "identity_scope",
    }.intersection(stored_identity)
    if forbidden_stored_identity_fields:
        raise RuntimeError(
            "STORED_CONTRACT_IDENTITY_CANONICAL_FIELD_FORBIDDEN fields="
            + ",".join(sorted(forbidden_stored_identity_fields))
        )
    typed_identity = {
        key: value
        for key, value in {
            "provider": provider_key,
            "provider_contract_id": contract_id,
            "con_id": con_id_value,
            "asset_class": asset_class_value,
            "identity_scope": identity_scope_value,
        }.items()
        if value not in (None, "")
    }
    stored_identity.update(typed_identity)
    payload: dict[str, Any] = {
        "instrument_id": exact_instrument_id,
        "key": key,
        "instrument_key": key,
        "display": display_name_value,
        "name": display_name_value,
        "provider": provider_key,
        "provider_symbol": symbol,
        "provider_contract_id": contract_id,
        "asset_class": asset_class_value,
        "contract_identity": stored_identity,
        "continuous_series": {},
        "session": _json_object(session, field="session"),
        "quote_only": False,
        "watchlist": bool(watchlist),
        "watchlist_order": int(watchlist_order or 0),
        "watchlist_version": int(watchlist_version),
        "presentation": watchlist_presentation_payload(
            presentation,
            allow_default_revision=presentation is None,
        ),
    }
    if metadata_value:
        public_metadata = {
            field: value
            for field, value in metadata_value.items()
            if field not in {"contract_identity", "continuous_series", "profile", "quote_only"}
        }
        if public_metadata:
            payload["metadata"] = public_metadata
        if "continuous_series" in metadata_value:
            if not isinstance(metadata_value["continuous_series"], dict):
                raise RuntimeError("STORED_CONTINUOUS_SERIES_OBJECT_REQUIRED")
            payload["continuous_series"] = dict(metadata_value["continuous_series"])
        if "profile" in metadata_value:
            payload["profile"] = require_exact_identity_text(
                metadata_value["profile"],
                field="profile",
            )
        if "quote_only" in metadata_value:
            if not isinstance(metadata_value["quote_only"], bool):
                raise RuntimeError("STORED_QUOTE_ONLY_BOOLEAN_REQUIRED")
            payload["quote_only"] = metadata_value["quote_only"]
    if instrument_is_futures_root(payload):
        current_contract_value = _json_object(
            current_contract,
            field="current_contract",
        )
        if current_contract_value:
            raw_min_tick = current_contract_value.get("min_tick")
            if raw_min_tick is not None:
                current_contract_value["min_tick"] = require_exact_positive_number(
                    raw_min_tick,
                    field="current_contract.min_tick",
                )
            payload["contract_identity"]["current_contract"] = current_contract_value
    provider_session_value = _json_object(
        provider_session,
        field="provider_session",
    )
    if provider_session_value:
        payload["session"].update(provider_session_value)
    return require_provider_identity(payload)


def _instrument_values(item: dict[str, Any]) -> tuple[Any, ...]:
    qualified = require_provider_identity(item)
    key = instrument_key(qualified)
    provider = instrument_provider(qualified)
    provider_symbol = require_exact_identity_text(
        qualified.get("provider_symbol"),
        field="provider_symbol",
    )
    identity = identity_payload(qualified)
    raw_con_id = identity.get("con_id")
    con_id = parse_exact_positive_decimal_provider_id(raw_con_id) or None
    if raw_con_id not in (None, "") and con_id is None:
        raise ValueError("numeric provider contract identity must be canonical and exact")
    contract_id = provider_contract_id(qualified)
    asset_class = instrument_asset_class(qualified)
    identity_scope = instrument_identity_scope(qualified)
    if instrument_is_futures_root(qualified):
        if contract_id or con_id is not None:
            raise ValueError("FUTURES_CONCRETE_CONTRACTS_BELONG_IN_FUTURES_CONTRACTS")
        contract_id = ""
    elif provider == "ibkr" and con_id is not None and contract_id != str(con_id):
        raise ValueError("IBKR provider_contract_id and con_id must be the same exact identity")
    display_name = require_exact_identity_text(
        item.get("display"),
        field="display",
    )
    raw_metadata = item.get("metadata")
    if raw_metadata is not None and not isinstance(raw_metadata, dict):
        raise ValueError("metadata must be an object")
    metadata = dict(raw_metadata or {})
    reserved_metadata_fields = {
        "contract_identity",
        "continuous_series",
        "profile",
        "quote_only",
    }.intersection(metadata)
    if reserved_metadata_fields:
        raise ValueError(
            "instrument metadata cannot contain canonical fields: "
            + ",".join(sorted(reserved_metadata_fields))
        )
    stored_identity = dict(identity)
    stored_identity.pop("current_contract", None)
    for field in (
        "provider",
        "provider_contract_id",
        "con_id",
        "asset_class",
        "identity_scope",
    ):
        stored_identity.pop(field, None)
    if stored_identity:
        metadata["contract_identity"] = stored_identity
    continuous_series = item.get("continuous_series")
    if continuous_series is not None:
        if not isinstance(continuous_series, dict):
            raise ValueError("continuous_series must be an object")
        if continuous_series:
            metadata["continuous_series"] = dict(continuous_series)
    profile = item.get("profile")
    if profile is not None:
        metadata["profile"] = require_exact_identity_text(profile, field="profile")
    quote_only = item.get("quote_only")
    if quote_only is not None:
        if not isinstance(quote_only, bool):
            raise ValueError("quote_only must be a boolean")
        metadata["quote_only"] = quote_only
    session = item.get("session")
    if session is not None and not isinstance(session, dict):
        raise ValueError("session must be an object")
    return (
        qualified["instrument_id"],
        key,
        provider,
        provider_symbol,
        contract_id,
        con_id,
        display_name,
        asset_class,
        identity_scope,
        dict(session or {}),
        metadata,
    )


class InstrumentsRepoMixin:
    """PostgreSQL repository mixin for terminal instrument identity."""

    _INSTRUMENT_SELECT = f"""
        SELECT i.instrument_id, i.instrument_key, i.provider, i.provider_symbol, i.provider_contract_id, i.con_id,
               i.display_name, i.asset_class, i.identity_scope, i.session, i.watchlist, i.watchlist_order,
               (SELECT version FROM watchlist_state WHERE id = true) AS watchlist_version,
               i.metadata,
               current_contract.payload AS current_contract,
               CASE
                   WHEN session_intervals.payload IS NULL AND liquid_session_intervals.payload IS NULL THEN NULL
                   ELSE jsonb_build_object(
                       'instrument_id', i.instrument_id,
                       'route_fingerprint', route.route_fingerprint,
                       'provider', i.provider,
                       'calendar', i.provider || '_trading_hours',
                       'timezone', latest_schedule.timezone,
                       'provider_contract_id', route.provider_contract_id,
                       'trading_intervals', COALESCE(session_intervals.payload, '[]'::jsonb),
                       'liquid_intervals', COALESCE(liquid_session_intervals.payload, '[]'::jsonb),
                       'source_fetched_at', latest_schedule.fetched_at
                   )
               END AS provider_session,
               presentation_setting.value AS presentation
        FROM instruments AS i
        LEFT JOIN settings AS presentation_setting
          ON presentation_setting.scope = '{WATCHLIST_PRESENTATION_SCOPE}'
         AND presentation_setting.key = i.instrument_id
        LEFT JOIN LATERAL (
            SELECT jsonb_strip_nulls(jsonb_build_object(
                'provider', fc.provider,
                'instrument_id', fc.instrument_id,
                'contract_key', fc.contract_key,
                'root', fc.root,
                'exchange', NULLIF(fc.exchange, ''),
                'currency', NULLIF(fc.currency, ''),
                'local_symbol', fc.local_symbol,
                'con_id', fc.con_id,
                'provider_contract_id', NULLIF(fc.metadata->>'provider_contract_id', ''),
                'expiry', NULLIF(fc.expiry, ''),
                'contract_month', NULLIF(fc.contract_month, ''),
                'first_notice_date', fc.first_notice_date,
                'last_trade_date', fc.last_trade_date,
                'source', 'provider_current_contract',
                'resolved_at', fc.current_resolved_at,
                'secid', NULLIF(fc.metadata->>'secid', ''),
                'shortname', NULLIF(fc.metadata->>'shortname', ''),
                'name', NULLIF(fc.metadata->>'name', ''),
                'trading_class', NULLIF(fc.metadata->>'trading_class', ''),
                'min_tick', fc.metadata->'min_tick'
            )) AS payload
            FROM futures_contracts AS fc
            WHERE fc.provider = i.provider
              AND fc.instrument_id = i.instrument_id
              AND fc.is_current = true
            ORDER BY fc.current_resolved_at DESC NULLS LAST
            LIMIT 1
        ) AS current_contract ON i.asset_class = 'future' AND i.identity_scope = 'root'
        LEFT JOIN LATERAL (
            SELECT
                CASE
                    WHEN i.asset_class = 'future' AND i.identity_scope = 'root'
                    THEN NULLIF(current_contract.payload->>'provider_contract_id', '')
                    ELSE NULLIF(i.provider_contract_id, '')
                END AS provider_contract_id,
                CASE
                    WHEN i.asset_class = 'future' AND i.identity_scope = 'root' THEN
                        CASE
                            WHEN NULLIF(current_contract.payload->>'provider_contract_id', '') IS NOT NULL
                            THEN i.instrument_id || '|current:' || (current_contract.payload->>'provider_contract_id')
                            ELSE NULL
                        END
                    ELSE i.instrument_id
                END AS route_fingerprint
        ) AS route ON true
        LEFT JOIN LATERAL (
            SELECT snapshot.timezone, snapshot.fetched_at
            FROM trading_schedule_snapshots AS snapshot
            WHERE snapshot.instrument_id = i.instrument_id
              AND snapshot.route_fingerprint = route.route_fingerprint
            ORDER BY snapshot.fetched_at DESC
            LIMIT 1
        ) AS latest_schedule ON route.provider_contract_id IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'session_date', recent.session_date,
                    'opens_at', recent.opens_at,
                    'closes_at', recent.closes_at,
                    'status', recent.status
                )
                ORDER BY recent.opens_at, recent.closes_at
            ) AS payload
            FROM (
                SELECT session_interval.session_date, session_interval.opens_at, session_interval.closes_at, session_interval.status
                FROM trading_session_intervals AS session_interval
                WHERE session_interval.instrument_id = i.instrument_id
                  AND session_interval.route_fingerprint = route.route_fingerprint
                  AND session_interval.session_type = 'trading'
                ORDER BY session_interval.opens_at DESC, session_interval.closes_at DESC
                LIMIT 512
            ) AS recent
        ) AS session_intervals ON latest_schedule.fetched_at IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'session_date', recent.session_date,
                    'opens_at', recent.opens_at,
                    'closes_at', recent.closes_at,
                    'status', recent.status
                )
                ORDER BY recent.opens_at, recent.closes_at
            ) AS payload
            FROM (
                SELECT session_interval.session_date, session_interval.opens_at, session_interval.closes_at, session_interval.status
                FROM trading_session_intervals AS session_interval
                WHERE session_interval.instrument_id = i.instrument_id
                  AND session_interval.route_fingerprint = route.route_fingerprint
                  AND session_interval.session_type = 'liquid'
                ORDER BY session_interval.opens_at DESC, session_interval.closes_at DESC
                LIMIT 512
            ) AS recent
        ) AS liquid_session_intervals ON latest_schedule.fetched_at IS NOT NULL
    """

    def select_watchlist_instrument(
        self,
        item: dict[str, Any],
        *,
        current_futures_contract: Mapping[str, Any] | None = None,
        route_transition: FuturesRouteTransition | None = None,
    ) -> int:
        """Atomically persist one qualified instrument, lifecycle, and membership."""

        values = _instrument_values(item)
        identity = values[0]
        is_futures_root = instrument_is_futures_root(item)
        if is_futures_root and current_futures_contract is None:
            raise ValueError("WATCHLIST_FUTURES_CURRENT_CONTRACT_REQUIRED")
        if not is_futures_root and current_futures_contract is not None:
            raise ValueError("WATCHLIST_NON_FUTURES_CONTRACT_FORBIDDEN")
        if not is_futures_root and route_transition is not None:
            raise ValueError("WATCHLIST_NON_FUTURES_ROUTE_TRANSITION_FORBIDDEN")
        if current_futures_contract is not None:
            contract_identity = require_exact_identity_text(
                current_futures_contract.get("instrument_id"),
                field="current_futures_contract.instrument_id",
            )
            if contract_identity != identity:
                raise ValueError("WATCHLIST_FUTURES_CONTRACT_IDENTITY_MISMATCH")
            contract_provider = require_exact_identity_text(
                current_futures_contract.get("provider"),
                field="current_futures_contract.provider",
            )
            if contract_provider != values[2]:
                raise ValueError("WATCHLIST_FUTURES_CONTRACT_PROVIDER_MISMATCH")
            contract_root = require_exact_identity_text(
                current_futures_contract.get("root"),
                field="current_futures_contract.root",
            )
            if contract_root != instrument_futures_root(item):
                raise ValueError("WATCHLIST_FUTURES_CONTRACT_ROOT_MISMATCH")
            if current_futures_contract.get("is_current") is not True:
                raise ValueError("WATCHLIST_FUTURES_CONTRACT_MUST_BE_CURRENT")

        self._psycopg()
        from psycopg.types.json import Jsonb

        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                cur.execute("SELECT version FROM watchlist_state WHERE id = true FOR UPDATE")
                if cur.fetchone() is None:
                    raise RuntimeError("WATCHLIST_STATE_REQUIRED")
                cur.execute(
                    """
                    INSERT INTO instruments (
                        instrument_id, instrument_key, provider, provider_symbol,
                        provider_contract_id, con_id, display_name, asset_class,
                        identity_scope, session, metadata, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (instrument_id) DO UPDATE SET
                        instrument_key = EXCLUDED.instrument_key,
                        provider_symbol = EXCLUDED.provider_symbol,
                        display_name = EXCLUDED.display_name,
                        asset_class = EXCLUDED.asset_class,
                        identity_scope = EXCLUDED.identity_scope,
                        session = EXCLUDED.session,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    WHERE instruments.provider = EXCLUDED.provider
                      AND instruments.provider_contract_id = EXCLUDED.provider_contract_id
                      AND instruments.con_id IS NOT DISTINCT FROM EXCLUDED.con_id
                      AND instruments.asset_class = EXCLUDED.asset_class
                      AND instruments.identity_scope = EXCLUDED.identity_scope
                    RETURNING watchlist
                    """,
                    (
                        values[0],
                        values[1],
                        values[2],
                        values[3],
                        values[4],
                        values[5],
                        values[6],
                        values[7],
                        values[8],
                        Jsonb(values[9]),
                        Jsonb(values[10]),
                    ),
                )
                instrument_row = cur.fetchone()
                if instrument_row is None:
                    raise ValueError("INSTRUMENT_IDENTITY_CONFLICT")
                if current_futures_contract is not None:
                    self._write_futures_contract_on_cursor(
                        cur,
                        current_futures_contract,
                        route_transition=route_transition,
                    )
                cur.execute(
                    "SELECT COALESCE(MAX(watchlist_order), -1) + 1 "
                    "FROM instruments WHERE watchlist = true"
                )
                next_order = int((cur.fetchone() or [0])[0] or 0)
                cur.execute(
                    """
                    UPDATE instruments
                    SET watchlist = true,
                        watchlist_order = CASE WHEN watchlist THEN watchlist_order ELSE %s END,
                        updated_at = now()
                    WHERE instrument_id = %s
                    RETURNING instrument_id
                    """,
                    (next_order, identity),
                )
                selected_row = cur.fetchone()
                if selected_row is None:
                    raise RuntimeError("WATCHLIST_INSTRUMENT_SELECTION_FAILED")
                cur.execute(
                    """
                    UPDATE watchlist_state
                    SET version = version + 1, updated_at = now()
                    WHERE id = true
                    RETURNING version
                    """
                )
                version_row = cur.fetchone()
                if version_row is None:
                    raise RuntimeError("WATCHLIST_STATE_REQUIRED")
                watchlist_version = int(version_row[0])
        return watchlist_version

    def set_watchlist_presentation(
        self,
        instrument_id: str,
        display_mode: str,
    ) -> dict[str, Any]:
        """Atomically update one current watchlist item's global presentation."""

        identity = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        mode = require_watchlist_display_mode(display_mode)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (WATCHLIST_SETTINGS_LOCK_KEY,),
                )
                settings_cache = getattr(self, "_settings_cache", None)
                if isinstance(settings_cache, dict):
                    settings_cache.clear()
                cur.execute(
                    """
                    SELECT value
                    FROM settings
                    WHERE scope = %s AND key = %s
                    FOR UPDATE
                    """,
                    (WATCHLIST_PRESENTATION_SCOPE, identity),
                )
                existing = cur.fetchone()
                if existing is not None:
                    current_presentation = watchlist_presentation_payload(
                        existing[0],
                        allow_default_revision=False,
                    )
                    if current_presentation["display_mode"] == mode:
                        return current_presentation
                cur.execute(
                    """
                    INSERT INTO settings AS current_setting (scope, key, value, updated_at)
                    SELECT %s,
                           instrument.instrument_id,
                           jsonb_build_object('display_mode', %s::text, 'revision', 1),
                           now()
                    FROM instruments AS instrument
                    WHERE instrument.instrument_id = %s
                      AND instrument.watchlist = true
                    ON CONFLICT (scope, key) DO UPDATE SET
                        value = jsonb_build_object(
                            'display_mode', EXCLUDED.value ->> 'display_mode',
                            'revision',
                            (current_setting.value ->> 'revision')::bigint + 1
                        ),
                        updated_at = now()
                    RETURNING value
                    """,
                    (WATCHLIST_PRESENTATION_SCOPE, mode, identity),
                )
                row = cur.fetchone()
                if row is not None:
                    advance_settings_revision(cur)
        settings_cache = getattr(self, "_settings_cache", None)
        if isinstance(settings_cache, dict):
            settings_cache.clear()
        if row is None:
            raise KeyError(identity)
        return watchlist_presentation_payload(
            row[0],
            allow_default_revision=False,
        )

    def remove_instrument_from_watchlist(
        self,
        instrument_id: str,
    ) -> WatchlistRetirementSettingsSnapshot:
        identity = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (WATCHLIST_SETTINGS_LOCK_KEY,)
                    )
                    settings_cache = getattr(self, "_settings_cache", None)
                    if isinstance(settings_cache, dict):
                        settings_cache.clear()
                    cur.execute("SELECT version FROM watchlist_state WHERE id = true FOR UPDATE")
                    if cur.fetchone() is None:
                        raise RuntimeError("WATCHLIST_STATE_REQUIRED")
                    cur.execute(
                        """
                        SELECT scope, key, value
                        FROM settings
                        WHERE scope = ANY(%s)
                        ORDER BY scope, key
                        FOR UPDATE
                        """,
                        (
                            [
                                CLIENT_SETTINGS_SCOPE,
                                SERVER_SETTINGS_SCOPE,
                                WATCHLIST_PRESENTATION_SCOPE,
                            ],
                        ),
                    )
                    settings_rows = cur.fetchall()
                    client_settings = {
                        row[1]: row[2] for row in settings_rows if row[0] == CLIENT_SETTINGS_SCOPE
                    }
                    server_settings = {
                        row[1]: row[2] for row in settings_rows if row[0] == SERVER_SETTINGS_SCOPE
                    }
                    presentation_settings = {
                        row[1]: row[2]
                        for row in settings_rows
                        if row[0] == WATCHLIST_PRESENTATION_SCOPE
                    }
                    cur.execute(
                        """
                        SELECT instrument_id
                        FROM instruments
                        WHERE watchlist = true
                        ORDER BY instrument_id
                        """
                    )
                    current_instrument_ids = {row[0] for row in cur.fetchall()}

                    def instrument_exists(candidate: str) -> bool:
                        return candidate in current_instrument_ids

                    invalid_client_keys = invalid_persisted_client_setting_keys(client_settings)
                    invalid_client_values = invalid_client_setting_value_keys(client_settings)
                    invalid_client_selections = invalid_workspace_instrument_selection_keys(
                        client_settings,
                        instrument_exists=instrument_exists,
                    )
                    invalid_client_instruments = invalid_client_setting_instrument_keys(
                        client_settings,
                        instrument_exists=instrument_exists,
                    )
                    if (
                        invalid_client_keys
                        or invalid_client_values
                        or invalid_client_selections
                        or invalid_client_instruments
                    ):
                        raise RuntimeError(
                            "STORAGE_CLIENT_SETTINGS_CONTRACT_REQUIRED "
                            f"keys={invalid_client_keys!r} "
                            f"values={invalid_client_values!r} "
                            f"selections={invalid_client_selections!r} "
                            f"instruments={invalid_client_instruments!r}"
                        )
                    invalid_server_keys = invalid_persisted_server_setting_keys(server_settings)
                    invalid_server_values = invalid_server_setting_value_keys(
                        server_settings,
                        instrument_exists=instrument_exists,
                    )
                    if invalid_server_keys or invalid_server_values:
                        raise RuntimeError(
                            "STORAGE_SERVER_SETTINGS_CONTRACT_REQUIRED "
                            f"keys={invalid_server_keys!r} "
                            f"values={invalid_server_values!r}"
                        )
                    invalid_presentations: list[str] = []
                    for setting_key, setting_value in presentation_settings.items():
                        try:
                            presentation_identity = require_exact_identity_text(
                                setting_key,
                                field="instrument_id",
                            )
                            watchlist_presentation_payload(
                                setting_value,
                                allow_default_revision=False,
                            )
                        except ValueError:
                            invalid_presentations.append(str(setting_key))
                            continue
                        if not instrument_exists(presentation_identity):
                            invalid_presentations.append(presentation_identity)
                    if invalid_presentations:
                        raise RuntimeError(
                            "WATCHLIST_PRESENTATION_STORAGE_CONTRACT_REQUIRED "
                            f"keys={sorted(invalid_presentations)!r}"
                        )
                    cur.execute(
                        """
                        UPDATE instruments
                        SET watchlist = false,
                            watchlist_order = NULL,
                            updated_at = now()
                        WHERE instrument_id = %s AND watchlist = true
                        RETURNING instrument_id
                        """,
                        (identity,),
                    )
                    removed = cur.fetchone() is not None
                    if removed:
                        client_keys = client_setting_keys_for_instrument(
                            client_settings,
                            identity,
                        )
                        server_updates, server_deletes = (
                            server_setting_mutations_without_instrument(
                                server_settings,
                                identity,
                            )
                        )
                        settings_changed = bool(
                            identity in presentation_settings
                            or client_keys
                            or server_updates
                            or server_deletes
                        )
                        cur.execute(
                            """
                            DELETE FROM settings
                            WHERE scope = %s AND key = %s
                            """,
                            (WATCHLIST_PRESENTATION_SCOPE, identity),
                        )
                        if client_keys:
                            cur.execute(
                                """
                                DELETE FROM settings
                                WHERE scope = %s AND key = ANY(%s)
                                """,
                                (CLIENT_SETTINGS_SCOPE, client_keys),
                            )
                        if server_updates:
                            self._psycopg()
                            from psycopg.types.json import Jsonb

                            for setting_key, setting_value in server_updates.items():
                                cur.execute(
                                    """
                                    UPDATE settings
                                    SET value = %s, updated_at = now()
                                    WHERE scope = %s AND key = %s
                                    """,
                                    (
                                        Jsonb(setting_value),
                                        SERVER_SETTINGS_SCOPE,
                                        setting_key,
                                    ),
                                )
                        if server_deletes:
                            cur.execute(
                                """
                                DELETE FROM settings
                                WHERE scope = %s AND key = ANY(%s)
                                """,
                                (SERVER_SETTINGS_SCOPE, server_deletes),
                            )
                        if settings_changed:
                            advance_settings_revision(cur)
                        cur.execute(
                            """
                            UPDATE watchlist_state
                            SET version = version + 1, updated_at = now()
                            WHERE id = true
                            """
                        )
                    cur.execute(
                        """
                        SELECT scope,
                               key,
                               value,
                               mutation_changed_at_ms,
                               mutation_writer_id::text,
                               mutation_sequence
                        FROM settings
                        WHERE scope = %s
                           OR (scope = %s AND key = ANY(%s))
                        ORDER BY scope, key
                        """,
                        (
                            CLIENT_SETTINGS_SCOPE,
                            SERVER_SETTINGS_SCOPE,
                            [GEX_SCHEDULER_SETTING_KEY, OPTION_TARGET_CAPS_SETTING_KEY],
                        ),
                    )
                    projection_rows = cur.fetchall()
                    remaining_instrument_ids = set(current_instrument_ids)
                    if removed:
                        remaining_instrument_ids.discard(identity)
                    projected_client_settings = {
                        str(row[1]): row[2]
                        for row in projection_rows
                        if row[0] == CLIENT_SETTINGS_SCOPE
                    }
                    projected_client_orders = {
                        str(row[1]): {
                            "changed_at_ms": int(row[3]),
                            "writer_id": str(row[4]) if row[4] is not None else None,
                            "sequence": int(row[5]),
                        }
                        for row in projection_rows
                        if row[0] == CLIENT_SETTINGS_SCOPE
                    }
                    projected_server_settings = {
                        str(row[1]): row[2]
                        for row in projection_rows
                        if row[0] == SERVER_SETTINGS_SCOPE
                    }
                    self._validate_client_settings_contract(
                        projected_client_settings,
                        instrument_exists=lambda candidate: candidate in remaining_instrument_ids,
                    )
                    self._validate_server_settings_contract(
                        projected_server_settings,
                        instrument_exists=lambda candidate: candidate in remaining_instrument_ids,
                    )
                    settings_revision = read_settings_revision(cur)
                    retirement_snapshot = WatchlistRetirementSettingsSnapshot(
                        settings_revision=settings_revision,
                        client_settings=projected_client_settings,
                        client_mutation_orders=projected_client_orders,
                        gex_scheduler_setting=projected_server_settings.get(
                            GEX_SCHEDULER_SETTING_KEY
                        ),
                        gex_scheduler_present=(
                            GEX_SCHEDULER_SETTING_KEY in projected_server_settings
                        ),
                        option_target_caps_setting=projected_server_settings.get(
                            OPTION_TARGET_CAPS_SETTING_KEY
                        ),
                        option_target_caps_present=(
                            OPTION_TARGET_CAPS_SETTING_KEY in projected_server_settings
                        ),
                    )
        settings_cache = getattr(self, "_settings_cache", None)
        if isinstance(settings_cache, dict):
            settings_cache.clear()
        return retirement_snapshot

    def reorder_watchlist(
        self,
        ordered_keys: Sequence[str],
        expected_version: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        keys = list(require_exact_instrument_id_sequence(ordered_keys, allow_empty=True))
        seen = set(keys)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))", (WATCHLIST_SETTINGS_LOCK_KEY,)
                    )
                    cur.execute("SELECT version FROM watchlist_state WHERE id = true FOR UPDATE")
                    version_row = cur.fetchone()
                    if version_row is None:
                        raise RuntimeError("WATCHLIST_STATE_REQUIRED")
                    current_version = int(version_row[0])
                    if expected_version is None:
                        raise WatchlistOrderError("WATCHLIST_ORDER_VERSION_REQUIRED")
                    if int(expected_version) != current_version:
                        raise WatchlistOrderError(
                            "WATCHLIST_ORDER_STALE",
                            expected_version=int(expected_version),
                            current_version=current_version,
                        )
                    cur.execute(
                        """
                        SELECT instrument_id
                        FROM instruments
                        WHERE watchlist = true
                        ORDER BY watchlist_order ASC, instrument_key ASC
                        FOR UPDATE
                        """
                    )
                    rows = cur.fetchall()
                    current_keys = [
                        require_exact_identity_text(
                            row[0],
                            field="instrument_id",
                        )
                        for row in rows
                    ]
                    unknown = [key for key in keys if key not in current_keys]
                    if unknown:
                        raise ValueError(f"unknown watchlist instruments: {', '.join(unknown)}")
                    ordered = list(keys)
                    ordered.extend(key for key in current_keys if key not in seen)
                    cur.execute(
                        """
                        UPDATE instruments
                        SET watchlist_order = -1000000 - watchlist_order
                        WHERE watchlist = true
                        """
                    )
                    cur.execute(
                        """
                        UPDATE instruments AS instrument
                        SET watchlist_order = (ordered.position - 1)::integer,
                            updated_at = now()
                        FROM unnest(%s::text[]) WITH ORDINALITY AS ordered(instrument_id, position)
                        WHERE instrument.instrument_id = ordered.instrument_id
                          AND instrument.watchlist = true
                        """,
                        (ordered,),
                    )
                    cur.execute(
                        "UPDATE watchlist_state SET version = version + 1, updated_at = now() WHERE id = true"
                    )
        return self.read_watchlist_snapshot()

    def read_watchlist_snapshot(self) -> tuple[list[dict[str, Any]], int]:
        """Read watchlist rows and their concurrency version from one SQL snapshot."""

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH watchlist_snapshot AS (
                        SELECT version AS watchlist_version
                        FROM watchlist_state
                        WHERE id = true
                    )
                    SELECT snapshot.watchlist_version, selected.*
                    FROM watchlist_snapshot AS snapshot
                    LEFT JOIN LATERAL (
                        {self._INSTRUMENT_SELECT}
                        WHERE i.watchlist = true
                        ORDER BY i.watchlist_order ASC NULLS LAST,
                                 i.instrument_key ASC
                    ) AS selected ON true
                    """
                )
                rows = cur.fetchall()
        if not rows:
            raise RuntimeError("WATCHLIST_STATE_REQUIRED")
        version = int(rows[0][0])
        instruments = [
            _instrument_row_payload(row[1:]) for row in rows if len(row) > 1 and row[1] is not None
        ]
        return instruments, version

    def lookup_instrument(
        self, instrument_id: str, *, watchlist_only: bool = False
    ) -> dict[str, Any] | None:
        identity = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        where = "WHERE i.instrument_id = %s"
        params: tuple[Any, ...] = (identity,)
        if watchlist_only:
            where += " AND i.watchlist = true"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"{self._INSTRUMENT_SELECT} {where}", params)
                row = cur.fetchone()
        return _instrument_row_payload(row) if row else None
