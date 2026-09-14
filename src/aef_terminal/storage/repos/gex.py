from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from aef_terminal.data.gex.constants import (
    GEX_CHART_HISTORY_SOURCES,
    GEX_LIVE_SNAPSHOT_SOURCE,
    GEX_SCHEDULER_INTERVAL_MINUTES,
)
from aef_terminal.data.gex.contracts import require_exact_gex_capture_lane
from aef_terminal.data.gex.option_target_contract import (
    OPTION_TARGET_IMMUTABLE_CONTRACT_KEYS,
    OPTION_TARGET_MARKET_SAMPLE_FIELDS,
    OPTION_TARGET_ROUTE_LIMIT,
    OptionTargetCreateResult,
    option_target_market_sample,
    require_option_target_intent,
)
from aef_terminal.data.gex.snapshot_validation import (
    require_gex_snapshot_payload,
)
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
)
from aef_terminal.data.providers import provider_key_for_source
from aef_terminal.domain import domain_wire_value
from aef_terminal.runtime.math_utils import exact_finite_number_or_none, float_or_none
from aef_terminal.runtime.option_target_changes import option_target_mutation
from aef_terminal.storage.db_utils import ensure_utc
from aef_terminal.storage.repos.schema import _timescaledb_sql


class GexRepoMixin:
    """PostgreSQL repository mixin."""

    _OPTION_TARGET_ITEM_KEYS = {
        "id",
        "symbol",
        "timeframe",
        "point",
        "created_at_ms",
        "instrument_id",
        "route_fingerprint",
        "provider",
        "provider_contract_id",
    }

    @classmethod
    def _option_target_intent_payload(
        cls,
        option_payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing = option_payload.get("intent")
        if not isinstance(existing, dict):
            raise ValueError("option target payload.intent is required")
        return require_option_target_intent(existing)

    @classmethod
    def _option_target_persistent_payload(
        cls, item: dict[str, Any], display_key: str, timeframe: str, option_id: str
    ) -> dict[str, Any]:
        payload = {key: item[key] for key in cls._OPTION_TARGET_ITEM_KEYS if key in item}
        raw_point = item.get("point")
        if not isinstance(raw_point, dict):
            raise ValueError("option target point is required")
        point_price = float_or_none(raw_point.get("price"))
        if point_price is None or point_price <= 0 or isinstance(raw_point.get("price"), bool):
            raise ValueError("option target point.price must be positive and finite")
        point_ts = raw_point.get("ts")
        if not isinstance(point_ts, str) or parse_gex_timestamp(point_ts) is None:
            raise ValueError("option target point.ts must be timezone-aware")
        payload["point"] = {
            **raw_point,
            "price": point_price,
            "ts": point_ts,
        }
        option_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        intent = cls._option_target_intent_payload(option_payload)
        payload["payload"] = {"intent": intent, "market_sample": {}}
        payload["id"] = option_id
        payload["symbol"] = display_key
        payload["timeframe"] = timeframe
        return payload

    @classmethod
    def _option_target_intent_update_payload(
        cls,
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        existing_option = (
            existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
        )
        incoming_option = (
            incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}
        )
        for key in (
            "id",
            "instrument_id",
            "route_fingerprint",
            "provider",
            "provider_contract_id",
        ):
            if existing.get(key) != incoming.get(key):
                raise ValueError(f"option target immutable {key} mismatch")
        incoming_intent = incoming_option.get("intent")
        existing_intent = existing_option.get("intent")
        existing_market_sample = existing_option.get("market_sample")
        if (
            not isinstance(incoming_intent, dict)
            or not isinstance(existing_intent, dict)
            or not isinstance(existing_market_sample, dict)
        ):
            raise ValueError("option target canonical intent/market_sample contract is required")
        for key in OPTION_TARGET_IMMUTABLE_CONTRACT_KEYS:
            if existing_intent.get(key) != incoming_intent.get(key):
                raise ValueError(f"option target immutable exact contract {key} mismatch")
        merged_option = {
            "intent": dict(incoming_intent),
            "market_sample": dict(existing_market_sample),
        }
        return {
            **existing,
            **incoming,
            "payload": merged_option,
        }

    @classmethod
    def _option_target_market_sample_update_payload(
        cls,
        existing: dict[str, Any],
        option_payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing_option = (
            existing.get("payload") if isinstance(existing.get("payload"), dict) else {}
        )
        intent = existing_option.get("intent")
        if not isinstance(intent, dict):
            raise ValueError("option target canonical intent is required")
        unexpected = set(option_payload) - set(OPTION_TARGET_MARKET_SAMPLE_FIELDS)
        if unexpected:
            raise ValueError("option target market sample has unsupported fields")
        market_sample = option_target_market_sample(
            option_payload,
            sec_type=str(intent.get("sec_type") or ""),
        )
        merged_option = {"intent": dict(intent), "market_sample": market_sample}
        return {
            **existing,
            "payload": merged_option,
        }

    def read_option_targets(
        self,
        timeframe: str | None = None,
        *,
        route_pairs: Sequence[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        if route_pairs is not None and (
            not isinstance(route_pairs, Sequence) or isinstance(route_pairs, (str, bytes))
        ):
            raise TypeError("read_option_targets route_pairs must be an exact sequence")
        conditions: list[str] = []
        params: list[Any] = []
        exact_route_pairs: list[tuple[str, str]] = []
        seen_route_pairs: set[tuple[str, str]] = set()
        for pair in route_pairs or ():
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError(
                    "read_option_targets route_pairs must contain exact identity pairs"
                )
            exact_pair = (
                require_exact_identity_text(
                    pair[0],
                    field="OPTION_TARGET_INSTRUMENT_ID",
                ),
                require_exact_identity_text(
                    pair[1],
                    field="OPTION_TARGET_ROUTE_FINGERPRINT",
                ),
            )
            if exact_pair not in seen_route_pairs:
                seen_route_pairs.add(exact_pair)
                exact_route_pairs.append(exact_pair)
        if route_pairs is not None:
            if not exact_route_pairs:
                return []
            conditions.append(
                """
                EXISTS (
                    SELECT 1
                    FROM unnest(%s::text[], %s::text[])
                        AS route_filter(instrument_id, route_fingerprint)
                    WHERE route_filter.instrument_id = option_target.instrument_id
                      AND route_filter.route_fingerprint = option_target.route_fingerprint
                )
                """
            )
            params.extend(
                (
                    [pair[0] for pair in exact_route_pairs],
                    [pair[1] for pair in exact_route_pairs],
                )
            )
        if timeframe is not None:
            conditions.append("timeframe = %s")
            params.append(
                require_exact_identity_text(
                    timeframe,
                    field="OPTION_TARGET_TIMEFRAME",
                )
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, symbol, timeframe, payload, created_at, updated_at
                    FROM option_targets AS option_target
                    {where}
                    ORDER BY updated_at ASC,
                             instrument_id ASC,
                             route_fingerprint ASC,
                             id ASC
                    """,
                    params,
                )
                rows = cur.fetchall()
        targets: list[dict[str, Any]] = []
        for row in rows:
            option_id = require_exact_identity_text(
                row[0],
                field="OPTION_TARGET_ID",
            )
            display_key = require_exact_identity_text(
                row[1],
                field="OPTION_TARGET_DISPLAY_KEY",
            )
            exact_timeframe = require_exact_identity_text(
                row[2],
                field="OPTION_TARGET_TIMEFRAME",
            )
            payload = row[3]
            if not isinstance(payload, dict):
                raise ValueError("persisted option target payload must be an object")
            if (
                payload.get("id") != option_id
                or payload.get("symbol") != display_key
                or payload.get("timeframe") != exact_timeframe
            ):
                raise ValueError("persisted option target row metadata disagrees with its payload")
            require_exact_identity_text(
                payload.get("instrument_id"),
                field="OPTION_TARGET_INSTRUMENT_ID",
            )
            require_exact_identity_text(
                payload.get("route_fingerprint"),
                field="OPTION_TARGET_ROUTE_FINGERPRINT",
            )
            targets.append(
                {
                    "id": option_id,
                    "symbol": display_key,
                    "timeframe": exact_timeframe,
                    "payload": payload,
                    "created_at": (
                        row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4])
                    ),
                    "updated_at": (
                        row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5])
                    ),
                }
            )
        return targets

    @classmethod
    def _canonical_option_target_write(
        cls,
        instrument_id: str,
        display_key: str,
        timeframe: str,
        item: dict[str, Any],
        *,
        route_fingerprint: str,
        provider: str,
        provider_contract_id: str,
        generate_id: bool,
    ) -> tuple[str, str, str, str, str, dict[str, Any]]:
        raw_option_id = item.get("id")
        if raw_option_id in (None, ""):
            if not generate_id:
                raise ValueError("OPTION_TARGET_ID_REQUIRED")
            option_id = f"opt-{uuid4().hex}"
            item = {**item, "id": option_id}
        else:
            option_id = require_exact_identity_text(
                raw_option_id,
                field="OPTION_TARGET_ID",
            )
            if option_id != option_id.strip():
                raise ValueError("option target id must be exact")
        exact_display_key = require_exact_identity_text(
            display_key,
            field="OPTION_TARGET_DISPLAY_KEY",
        )
        exact_timeframe = require_exact_identity_text(
            timeframe,
            field="OPTION_TARGET_TIMEFRAME",
        )
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="OPTION_TARGET_INSTRUMENT_ID",
        )
        exact_route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="OPTION_TARGET_ROUTE_FINGERPRINT",
        )
        exact_provider_contract_id = require_exact_identity_text(
            provider_contract_id,
            field="OPTION_TARGET_PROVIDER_CONTRACT_ID",
        )
        exact_provider = require_exact_identity_text(
            provider,
            field="OPTION_TARGET_PROVIDER",
        )
        incoming = cls._option_target_persistent_payload(
            {
                **item,
                "instrument_id": exact_instrument_id,
                "route_fingerprint": exact_route_fingerprint,
                "provider": exact_provider,
                "provider_contract_id": exact_provider_contract_id,
            },
            exact_display_key,
            exact_timeframe,
            option_id,
        )
        return (
            option_id,
            exact_display_key,
            exact_timeframe,
            exact_instrument_id,
            exact_route_fingerprint,
            incoming,
        )

    def create_option_target_intent(
        self,
        instrument_id: str,
        display_key: str,
        timeframe: str,
        item: dict[str, Any],
        *,
        route_fingerprint: str,
        provider: str,
        provider_contract_id: str,
    ) -> OptionTargetCreateResult:
        self._psycopg()
        from psycopg.types.json import Jsonb

        (
            option_id,
            exact_display_key,
            exact_timeframe,
            instrument_id,
            route_fingerprint,
            incoming,
        ) = self._canonical_option_target_write(
            instrument_id,
            display_key,
            timeframe,
            item,
            route_fingerprint=route_fingerprint,
            provider=provider,
            provider_contract_id=provider_contract_id,
            generate_id=True,
        )
        incoming_intent = incoming["payload"]["intent"]
        exact_expiry_at = parse_gex_timestamp(incoming_intent.get("expiry_at"))
        if exact_expiry_at is None or exact_expiry_at.astimezone(UTC) <= datetime.now(tz=UTC):
            raise ValueError("option target exact provider contract has already expired")
        self._require_canonical_writer_lease()
        with option_target_mutation() as mutation, self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (
                        f"option-target-limit:{instrument_id}",
                        route_fingerprint,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO option_targets (id, symbol, timeframe, payload, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, now(), now())
                    ON CONFLICT (instrument_id, route_fingerprint, id) DO NOTHING
                    RETURNING payload
                    """,
                    (option_id, exact_display_key, exact_timeframe, Jsonb(incoming)),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    mutation.changed = True
                    item = inserted[0] if isinstance(inserted[0], dict) else incoming
                    cur.execute(
                        """
                        SELECT id, payload
                        FROM option_targets
                        WHERE instrument_id = %s
                          AND route_fingerprint = %s
                        ORDER BY created_at DESC, id DESC
                        OFFSET %s
                        FOR UPDATE
                        """,
                        (instrument_id, route_fingerprint, OPTION_TARGET_ROUTE_LIMIT),
                    )
                    overflow = tuple(
                        (str(row[0]), dict(row[1]))
                        for row in cur.fetchall()
                        if isinstance(row[1], dict)
                    )
                    if overflow:
                        cur.execute(
                            """
                            DELETE FROM option_targets
                            WHERE instrument_id = %s
                              AND route_fingerprint = %s
                              AND id = ANY(%s::text[])
                            """,
                            (
                                instrument_id,
                                route_fingerprint,
                                [row[0] for row in overflow],
                            ),
                        )
                    return OptionTargetCreateResult(
                        item=item,
                        evicted=tuple(row[1] for row in overflow),
                    )
                cur.execute(
                    """
                    SELECT payload
                    FROM option_targets
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    FOR UPDATE
                    """,
                    (option_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
                existing = row[0] if row and isinstance(row[0], dict) else None
                if existing is None:
                    raise ValueError("OPTION_TARGET_CREATE_CONFLICT")
                replay = self._option_target_intent_update_payload(existing, incoming)
                if replay != existing:
                    raise ValueError("OPTION_TARGET_CREATE_CONFLICT")
                return OptionTargetCreateResult(item=existing)

    def update_option_target_intent(
        self,
        instrument_id: str,
        display_key: str,
        timeframe: str,
        item: dict[str, Any],
        *,
        route_fingerprint: str,
        provider: str,
        provider_contract_id: str,
    ) -> dict[str, Any]:
        self._psycopg()
        from psycopg.types.json import Jsonb

        (
            option_id,
            exact_display_key,
            exact_timeframe,
            instrument_id,
            route_fingerprint,
            incoming,
        ) = self._canonical_option_target_write(
            instrument_id,
            display_key,
            timeframe,
            item,
            route_fingerprint=route_fingerprint,
            provider=provider,
            provider_contract_id=provider_contract_id,
            generate_id=False,
        )
        self._require_canonical_writer_lease()
        with option_target_mutation() as mutation, self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    SELECT payload
                    FROM option_targets
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    FOR UPDATE
                    """,
                    (option_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
                existing = row[0] if row and isinstance(row[0], dict) else None
                if existing is None:
                    raise ValueError("OPTION_TARGET_NOT_FOUND")
                payload = self._option_target_intent_update_payload(existing, incoming)
                cur.execute(
                    """
                    UPDATE option_targets
                    SET symbol = %s,
                        timeframe = %s,
                        payload = %s,
                        updated_at = now()
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND (
                          symbol IS DISTINCT FROM %s
                          OR timeframe IS DISTINCT FROM %s
                          OR payload IS DISTINCT FROM %s
                      )
                    """,
                    (
                        exact_display_key,
                        exact_timeframe,
                        Jsonb(payload),
                        option_id,
                        instrument_id,
                        route_fingerprint,
                        exact_display_key,
                        exact_timeframe,
                        Jsonb(payload),
                    ),
                )
                mutation.changed = bool(cur.rowcount)
        return payload

    def update_option_target_market_samples(
        self,
        updates: Sequence[tuple[str, str, str, dict[str, Any]]],
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        if not updates:
            return {}
        self._psycopg()
        from psycopg.types.json import Jsonb

        incoming_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        for instrument_id, route_fingerprint, option_id, option_payload in updates:
            exact_key = (
                require_exact_identity_text(
                    instrument_id,
                    field="OPTION_TARGET_INSTRUMENT_ID",
                ),
                require_exact_identity_text(
                    route_fingerprint,
                    field="OPTION_TARGET_ROUTE_FINGERPRINT",
                ),
                require_exact_identity_text(
                    option_id,
                    field="OPTION_TARGET_ID",
                ),
            )
            if exact_key in incoming_by_key:
                raise ValueError("duplicate option target market-sample identity")
            incoming_by_key[exact_key] = dict(option_payload)
        self._require_canonical_writer_lease()
        with option_target_mutation() as mutation, self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    SELECT instrument_id, route_fingerprint, id, payload
                    FROM option_targets
                    WHERE EXISTS (
                        SELECT 1
                        FROM unnest(%s::text[], %s::text[], %s::text[])
                            AS requested(instrument_id, route_fingerprint, id)
                        WHERE requested.instrument_id = option_targets.instrument_id
                          AND requested.route_fingerprint = option_targets.route_fingerprint
                          AND requested.id = option_targets.id
                    )
                    FOR UPDATE
                    """,
                    (
                        [key[0] for key in incoming_by_key],
                        [key[1] for key in incoming_by_key],
                        [key[2] for key in incoming_by_key],
                    ),
                )
                existing_by_key = {
                    (
                        require_exact_identity_text(
                            row[0],
                            field="OPTION_TARGET_INSTRUMENT_ID",
                        ),
                        require_exact_identity_text(
                            row[1],
                            field="OPTION_TARGET_ROUTE_FINGERPRINT",
                        ),
                        require_exact_identity_text(
                            row[2],
                            field="OPTION_TARGET_ID",
                        ),
                    ): row[3]
                    for row in cur.fetchall()
                    if isinstance(row[3], dict)
                }
                payloads = {
                    key: self._option_target_market_sample_update_payload(
                        existing_by_key[key],
                        option_payload,
                    )
                    for key, option_payload in incoming_by_key.items()
                    if key in existing_by_key
                }
                if payloads:
                    input_payload = [
                        {
                            "input_ordinal": input_ordinal,
                            "instrument_id": key[0],
                            "route_fingerprint": key[1],
                            "id": key[2],
                            "payload": payload,
                        }
                        for input_ordinal, (key, payload) in enumerate(payloads.items())
                    ]
                    cur.execute(
                        """
                        WITH input AS MATERIALIZED (
                            SELECT *
                            FROM jsonb_to_recordset(%s::jsonb) AS row(
                                input_ordinal integer,
                                instrument_id text,
                                route_fingerprint text,
                                id text,
                                payload jsonb
                            )
                        )
                        UPDATE option_targets AS target
                        SET payload = input.payload,
                            updated_at = now()
                        FROM input
                        WHERE target.id = input.id
                          AND target.instrument_id = input.instrument_id
                          AND target.route_fingerprint = input.route_fingerprint
                          AND target.payload IS DISTINCT FROM input.payload
                        RETURNING target.instrument_id,
                                  target.route_fingerprint,
                                  target.id,
                                  target.payload
                        """,
                        (Jsonb(input_payload),),
                    )
                    committed_payloads = {
                        (
                            require_exact_identity_text(
                                row[0],
                                field="OPTION_TARGET_INSTRUMENT_ID",
                            ),
                            require_exact_identity_text(
                                row[1],
                                field="OPTION_TARGET_ROUTE_FINGERPRINT",
                            ),
                            require_exact_identity_text(
                                row[2],
                                field="OPTION_TARGET_ID",
                            ),
                        ): dict(row[3])
                        for row in cur.fetchall()
                        if isinstance(row[3], dict)
                    }
                else:
                    committed_payloads = {}
                mutation.changed = bool(committed_payloads)
        return committed_payloads

    def delete_option_target(
        self,
        instrument_id: str,
        route_fingerprint: str,
        option_id: str,
    ) -> bool:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="OPTION_TARGET_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="OPTION_TARGET_ROUTE_FINGERPRINT",
        )
        option_id = require_exact_identity_text(
            option_id,
            field="OPTION_TARGET_ID",
        )
        self._require_canonical_writer_lease()
        with option_target_mutation() as mutation, self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    DELETE FROM option_targets
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    """,
                    (option_id, instrument_id, route_fingerprint),
                )
                mutation.changed = bool(cur.rowcount)
                return mutation.changed

    def upsert_gex_snapshot(
        self,
        instrument_id: str,
        route_fingerprint: str,
        provider_symbol: str,
        captured_at: datetime,
        payload: dict[str, Any],
        *,
        source: str,
        live_bucket_at: datetime | None = None,
    ) -> bool:
        self._psycopg()
        from psycopg.types.json import Jsonb

        instrument_id = require_exact_identity_text(
            instrument_id,
            field="GEX_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="GEX_ROUTE_FINGERPRINT",
        )
        payload_instrument_id = require_exact_identity_text(
            payload.get("instrument_id"),
            field="GEX_PAYLOAD_INSTRUMENT_ID",
        )
        payload_route_fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="GEX_PAYLOAD_ROUTE_FINGERPRINT",
        )
        if payload_instrument_id != instrument_id or payload_route_fingerprint != route_fingerprint:
            raise ValueError("GEX snapshot payload identity does not match its storage key")
        source = require_exact_identity_text(source, field="GEX_SNAPSHOT_SOURCE")
        source, _capture_mode = require_exact_gex_capture_lane(
            source=source,
            capture_mode=payload.get("capture_mode"),
        )
        provider_symbol = require_exact_identity_text(
            provider_symbol,
            field="GEX_PROVIDER_SYMBOL",
        )
        if (
            not isinstance(captured_at, datetime)
            or captured_at.tzinfo is None
            or captured_at.utcoffset() is None
        ):
            raise ValueError("GEX snapshot capture timestamp must be timezone-aware")
        require_exact_identity_text(
            payload.get("provider_symbol"),
            field="GEX_PAYLOAD_PROVIDER_SYMBOL",
        )
        payload_captured_at = parse_gex_timestamp(payload.get("captured_at"))
        if payload_captured_at is None or payload_captured_at.astimezone(
            UTC
        ) != captured_at.astimezone(UTC):
            raise ValueError("GEX snapshot payload facts do not match its storage row")
        payload = require_gex_snapshot_payload(payload, projection="persisted")
        storage_payload = dict(payload)
        storage_payload.pop("option_universe_expires_at", None)
        storage_raw = dict(storage_payload["raw"])
        storage_meta = dict(storage_raw["meta"])
        storage_meta.pop("option_universe_expires_at", None)
        storage_meta.pop("option_expiry_facts", None)
        storage_raw["meta"] = storage_meta
        storage_payload["raw"] = storage_raw
        storage_payload.pop("option_universe_expires_at", None)
        live_session_started_at: datetime | None = None
        live_bucket_end: datetime | None = None
        if live_bucket_at is not None:
            if (
                source != GEX_LIVE_SNAPSHOT_SOURCE
                or live_bucket_at.tzinfo is None
                or live_bucket_at.utcoffset() is None
            ):
                raise ValueError(
                    "A live GEX bucket commit requires exact live source and UTC bounds"
                )
            live_bucket_at = ensure_utc(live_bucket_at)
            live_bucket_end = live_bucket_at + timedelta(minutes=GEX_SCHEDULER_INTERVAL_MINUTES)
            if not live_bucket_at <= ensure_utc(captured_at) < live_bucket_end:
                raise ValueError("GEX snapshot capture timestamp is outside its live bucket")
            live_session_started_at = parse_gex_timestamp(payload["live"]["started_at"])
            assert live_session_started_at is not None
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    if live_bucket_at is not None:
                        assert live_bucket_end is not None
                        assert live_session_started_at is not None
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                            (
                                f"gex-live:{instrument_id}:{route_fingerprint}",
                                live_bucket_at.isoformat(),
                            ),
                        )
                        cur.execute(
                            """
                            SELECT captured_at, payload
                            FROM gex_snapshots
                            WHERE instrument_id = %s
                              AND route_fingerprint = %s
                              AND source = %s
                              AND captured_at >= %s
                              AND captured_at < %s
                            FOR UPDATE
                            """,
                            (
                                instrument_id,
                                route_fingerprint,
                                source,
                                live_bucket_at,
                                live_bucket_end,
                            ),
                        )
                        existing_session_starts: list[datetime] = []
                        for row in cur.fetchall():
                            if (
                                not row
                                or len(row) != 2
                                or not isinstance(row[0], datetime)
                                or row[0].tzinfo is None
                                or row[0].utcoffset() is None
                                or not isinstance(row[1], dict)
                            ):
                                raise ValueError(
                                    "GEX_LIVE_BUCKET_CONTRACT_INVALID: persisted row shape"
                                )
                            try:
                                existing_payload = require_gex_snapshot_payload(
                                    row[1],
                                    projection="persisted",
                                )
                            except (TypeError, ValueError) as exc:
                                raise ValueError(
                                    f"GEX_LIVE_BUCKET_CONTRACT_INVALID: {exc}"
                                ) from exc
                            existing_captured_at = ensure_utc(row[0])
                            if (
                                existing_payload.get("instrument_id") != instrument_id
                                or existing_payload.get("route_fingerprint") != route_fingerprint
                                or existing_payload.get("source") != source
                                or parse_gex_timestamp(existing_payload.get("captured_at"))
                                != existing_captured_at
                            ):
                                raise ValueError(
                                    "GEX_LIVE_BUCKET_CONTRACT_INVALID: row and payload provenance disagree"
                                )
                            existing_session_started_at = parse_gex_timestamp(
                                existing_payload["live"]["started_at"]
                            )
                            assert existing_session_started_at is not None
                            existing_session_starts.append(existing_session_started_at)
                        newest_existing_session = max(
                            existing_session_starts,
                            default=None,
                        )
                        if (
                            newest_existing_session is not None
                            and newest_existing_session > live_session_started_at
                        ):
                            return False
                        if newest_existing_session == live_session_started_at:
                            return True
                        cur.execute(
                            """
                            DELETE FROM gex_snapshots
                            WHERE instrument_id = %s
                              AND route_fingerprint = %s
                              AND source = %s
                              AND captured_at >= %s
                              AND captured_at < %s
                            """,
                            (
                                instrument_id,
                                route_fingerprint,
                                source,
                                live_bucket_at,
                                live_bucket_end,
                            ),
                        )
                    cur.execute(
                        """
                        INSERT INTO gex_snapshots (
                            instrument_id, route_fingerprint, provider_symbol, captured_at, source, payload, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (instrument_id, route_fingerprint, captured_at, source) DO UPDATE SET
                            provider_symbol = EXCLUDED.provider_symbol,
                            payload = EXCLUDED.payload,
                            created_at = now()
                        """,
                        (
                            instrument_id,
                            route_fingerprint,
                            provider_symbol,
                            ensure_utc(captured_at),
                            source,
                            Jsonb(storage_payload),
                        ),
                    )
        return True

    def read_gex_snapshots(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        limit: int = 512,
        *,
        source: str | None = None,
        sources: Sequence[str] | None = None,
        include_raw: bool = True,
    ) -> list[dict[str, Any]]:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="GEX_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="GEX_ROUTE_FINGERPRINT",
        )
        if type(limit) is not int or limit <= 0:
            raise ValueError("GEX snapshot read limit must be a positive integer")
        if type(include_raw) is not bool:
            raise TypeError("GEX snapshot include_raw must be boolean")
        source_filters = sum((source is not None, sources is not None))
        if source_filters != 1:
            raise ValueError("GEX snapshot reads accept exactly one source filter contract")
        if source is not None and source not in GEX_CHART_HISTORY_SOURCES:
            raise ValueError(f"Unsupported GEX snapshot source: {source}")
        if (
            not isinstance(start, datetime)
            or start.tzinfo is None
            or start.utcoffset() is None
            or not isinstance(end, datetime)
            or end.tzinfo is None
            or end.utcoffset() is None
        ):
            raise ValueError("GEX snapshot read bounds must be timezone-aware")
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if end_utc < start_utc:
            raise ValueError("GEX snapshot read bounds are inverted")
        exact_sources: tuple[str, ...] | None = None
        if sources is not None:
            exact_sources = tuple(sources)
            if not exact_sources:
                return []
            if any(item not in GEX_CHART_HISTORY_SOURCES for item in exact_sources):
                raise ValueError("Unsupported GEX snapshot source set")
            if len(set(exact_sources)) != len(exact_sources):
                raise ValueError("GEX snapshot sources must be unique")
        conditions = [
            "instrument_id = %s",
            "route_fingerprint = %s",
            "captured_at >= %s",
            "captured_at <= %s",
        ]
        params: list[Any] = [instrument_id, route_fingerprint, start_utc, end_utc]
        if source is not None:
            conditions.append("source = %s")
            params.append(source)
        elif exact_sources is not None:
            conditions.append("source = ANY(%s)")
            params.append(list(exact_sources))
        params.append(limit)
        payload_projection = (
            "payload"
            if include_raw
            else """
                jsonb_build_object(
                    'captured_at', payload -> 'captured_at',
                    'capture_revision', payload -> 'capture_revision',
                    'source', payload -> 'source',
                    'capture_mode', payload -> 'capture_mode',
                    'instrument_id', payload -> 'instrument_id',
                    'route_fingerprint', payload -> 'route_fingerprint',
                    'provider_symbol', payload -> 'provider_symbol',
                    'comparison_scope', payload -> 'comparison_scope',
                    'frame_complete', payload -> 'frame_complete',
                    'decision_authoritative', payload -> 'decision_authoritative',
                    'option_universe_expires_at', (
                        CASE
                            WHEN jsonb_typeof(payload #> '{raw,contracts}') = 'array'
                              AND jsonb_array_length(payload #> '{raw,contracts}') > 0
                              AND jsonb_typeof(
                                  payload #> '{comparison_scope,series}'
                              ) = 'array'
                              AND jsonb_array_length(
                                  payload #> '{comparison_scope,series}'
                              ) > 0
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM jsonb_array_elements(
                                      payload #> '{raw,contracts}'
                                  ) AS expiry_contract
                                  WHERE expiry_contract ->> 'expiry_time_source'
                                      IS DISTINCT FROM 'ibkr_contract_details'
                                     OR expiry_contract ->> 'expiry_at' IS NULL
                              )
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM jsonb_array_elements(
                                      payload #> '{raw,contracts}'
                                  ) AS series_expiry_contract
                                  GROUP BY
                                      series_expiry_contract ->> 'expiry',
                                      series_expiry_contract
                                          ->> 'trading_class',
                                      series_expiry_contract ->> 'exchange',
                                      series_expiry_contract -> 'multiplier'
                                  HAVING COUNT(
                                      DISTINCT series_expiry_contract
                                          ->> 'expiry_at'
                                  ) <> 1
                              )
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM jsonb_array_elements(
                                      payload #> '{comparison_scope,series}'
                                  ) AS scope_series
                                  WHERE NOT EXISTS (
                                      SELECT 1
                                      FROM jsonb_array_elements(
                                          payload #> '{raw,contracts}'
                                      ) AS scoped_expiry_contract
                                      WHERE scoped_expiry_contract ->> 'expiry'
                                          = scope_series ->> 'expiry'
                                        AND scoped_expiry_contract
                                                ->> 'trading_class'
                                          = scope_series ->> 'trading_class'
                                        AND scoped_expiry_contract ->> 'exchange'
                                          = scope_series ->> 'exchange'
                                        AND scoped_expiry_contract -> 'multiplier'
                                          = scope_series -> 'multiplier'
                                  )
                              )
                            THEN (
                                SELECT MIN(expiry_contract ->> 'expiry_at')
                                FROM jsonb_array_elements(
                                    payload #> '{raw,contracts}'
                                ) AS expiry_contract
                            )
                            ELSE NULL
                        END
                    ),
                    'market_data_entitlement', payload -> 'market_data_entitlement',
                    'open_interest_as_of', payload -> 'open_interest_as_of',
                    'spot', payload -> 'spot',
                    'gamma_flip', payload -> 'gamma_flip',
                    'levels', payload -> 'levels',
                    'call_wall', payload -> 'call_wall',
                    'put_wall', payload -> 'put_wall',
                    'net_gex', payload -> 'net_gex',
                    'global_gamma_regime', payload -> 'global_gamma_regime',
                    'option_activity', payload -> 'option_activity',
                    'option_activity_status', payload -> 'option_activity_status'
                )
            """.strip()
        )
        selection = f"""
            FROM gex_snapshots
            WHERE {" AND ".join(conditions)}
            ORDER BY captured_at DESC, source DESC
            LIMIT %s
        """
        if include_raw:
            query = f"""
                SELECT captured_at, source, payload, instrument_id, route_fingerprint, provider_symbol
                {selection}
            """
        else:
            # Each compact field otherwise detoasts the same large raw payload
            # again. Materialize one JSONB value per already bounded row. OFFSET
            # prevents the planner from inlining the lateral expression back
            # into every field/expiry check; no admission checks are skipped.
            query = f"""
                SELECT captured_at, source, {payload_projection}, instrument_id, route_fingerprint, provider_symbol
                FROM (
                    SELECT captured_at, source, payload AS stored_payload,
                           instrument_id, route_fingerprint, provider_symbol
                    {selection}
                ) AS selected
                CROSS JOIN LATERAL (
                    SELECT jsonb_build_array(stored_payload) -> 0 AS payload OFFSET 0
                ) AS expanded
                ORDER BY captured_at DESC, source DESC
            """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        rows.reverse()
        return [
            {
                "captured_at": ensure_utc(row[0]).isoformat(),
                "source": row[1],
                "payload": row[2],
                "instrument_id": require_exact_identity_text(
                    row[3],
                    field="GEX_INSTRUMENT_ID",
                ),
                "route_fingerprint": require_exact_identity_text(
                    row[4],
                    field="GEX_ROUTE_FINGERPRINT",
                ),
                "provider_symbol": require_exact_identity_text(
                    row[5],
                    field="GEX_PROVIDER_SYMBOL",
                ),
            }
            for row in rows
        ]

    def read_latest_gex_snapshot(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        source: str,
    ) -> dict[str, Any] | None:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="GEX_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="GEX_ROUTE_FINGERPRINT",
        )
        conditions = ["instrument_id = %s", "route_fingerprint = %s"]
        params: list[Any] = [instrument_id, route_fingerprint]
        if source not in GEX_CHART_HISTORY_SOURCES:
            raise ValueError(f"Unsupported GEX snapshot source: {source}")
        conditions.append("source = %s")
        params.append(source)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT captured_at, source, payload, instrument_id, route_fingerprint, provider_symbol
                    FROM gex_snapshots
                    WHERE {" AND ".join(conditions)}
                    ORDER BY captured_at DESC, created_at DESC, source ASC
                    LIMIT 1
                    """,
                    params,
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {
            "captured_at": ensure_utc(row[0]).isoformat(),
            "source": row[1],
            "payload": row[2],
            "instrument_id": require_exact_identity_text(
                row[3],
                field="GEX_INSTRUMENT_ID",
            ),
            "route_fingerprint": require_exact_identity_text(
                row[4],
                field="GEX_ROUTE_FINGERPRINT",
            ),
            "provider_symbol": require_exact_identity_text(
                row[5],
                field="GEX_PROVIDER_SYMBOL",
            ),
        }

    def read_gex_option_expiry_facts(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        instrument_id = require_exact_identity_text(
            instrument_id,
            field="GEX_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="GEX_ROUTE_FINGERPRINT",
        )
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("GEX option expiry fact limit must be between 1 and 512")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH recent_gex_snapshots AS (
                        SELECT captured_at, payload
                        FROM gex_snapshots
                        WHERE instrument_id = %s
                          AND route_fingerprint = %s
                          AND source = ANY(%s)
                        ORDER BY captured_at DESC, created_at DESC
                        LIMIT %s
                    )
                    SELECT
                        expiry_contract ->> 'expiry',
                        expiry_contract ->> 'trading_class',
                        expiry_contract ->> 'exchange',
                        expiry_contract ->> 'multiplier',
                        array_agg(
                            DISTINCT expiry_contract ->> 'expiry_at'
                        ),
                        count(
                            DISTINCT expiry_contract ->> 'con_id'
                        ),
                        max(captured_at) AS last_confirmed_at
                    FROM recent_gex_snapshots
                    CROSS JOIN LATERAL jsonb_array_elements(
                        payload #> '{raw,contracts}'
                    ) AS expiry_contract
                    WHERE expiry_contract ->> 'expiry_time_source'
                          = 'ibkr_contract_details'
                      AND expiry_contract ->> 'expiry_at' IS NOT NULL
                    GROUP BY 1, 2, 3, 4
                    ORDER BY last_confirmed_at DESC, 1, 2, 3, 4
                    LIMIT %s
                    """,
                    (
                        instrument_id,
                        route_fingerprint,
                        list(GEX_CHART_HISTORY_SOURCES),
                        limit,
                        limit,
                    ),
                )
                rows = cur.fetchall()
        return [
            {
                "expiry": row[0],
                "trading_class": row[1],
                "exchange": row[2],
                "multiplier": row[3],
                "expiry_at_values": list(row[4] or ()),
                "contract_count": int(row[5] or 0),
            }
            for row in rows
        ]

    def upsert_quote_snapshots(
        self,
        snapshots: Sequence[tuple[str, str, str, datetime, dict[str, Any], str]],
    ) -> None:
        if not snapshots:
            return
        self._psycopg()
        from psycopg.types.json import Jsonb

        input_payload: list[dict[str, Any]] = []
        for instrument_id, route_fingerprint, symbol, ts, payload, provider in snapshots:
            ticker = str(symbol or "")
            if not ticker:
                continue
            if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
                raise ValueError("QUOTE_SNAPSHOT_TIMESTAMP_MUST_BE_AWARE")
            captured_at = ts.astimezone(UTC)
            stable_id = require_exact_identity_text(
                instrument_id, field="QUOTE_SNAPSHOT_INSTRUMENT_ID"
            )
            fingerprint = require_exact_identity_text(
                route_fingerprint, field="QUOTE_SNAPSHOT_ROUTE_FINGERPRINT"
            )
            row = domain_wire_value(
                dict(payload or {}),
                field_name="quote_snapshot.payload",
            )
            numbers: dict[str, float | None] = {}
            for field in ("price", "bid", "ask", "last", "close"):
                value = row.get(field)
                numbers[field] = None
                if value is None:
                    continue
                parsed = exact_finite_number_or_none(value)
                if parsed is None:
                    raise ValueError(f"QUOTE_SNAPSHOT_{field.upper()}_MUST_BE_FINITE")
                numbers[field] = parsed
            input_payload.append(
                {
                    "input_ordinal": len(input_payload),
                    "provider": provider_key_for_source(provider),
                    "instrument_id": stable_id,
                    "route_fingerprint": fingerprint,
                    "symbol": ticker,
                    "ts": captured_at.isoformat(),
                    "price": numbers["price"],
                    "bid": numbers["bid"],
                    "ask": numbers["ask"],
                    "last": numbers["last"],
                    "close": numbers["close"],
                    "payload": row,
                }
            )
        if not input_payload:
            return

        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    WITH raw_input AS MATERIALIZED (
                        SELECT *
                        FROM jsonb_to_recordset(%s::jsonb) AS row(
                            input_ordinal integer,
                            provider text,
                            instrument_id text,
                            route_fingerprint text,
                            symbol text,
                            ts timestamptz,
                            price double precision,
                            bid double precision,
                            ask double precision,
                            last double precision,
                            close double precision,
                            payload jsonb
                        )
                    ),
                    input AS MATERIALIZED (
                        SELECT DISTINCT ON (
                            provider, instrument_id, route_fingerprint, ts
                        )
                            input_ordinal, provider, instrument_id,
                            route_fingerprint, symbol, ts,
                            price, bid, ask, last, close, payload
                        FROM raw_input
                        ORDER BY
                            provider, instrument_id, route_fingerprint, ts,
                            input_ordinal DESC
                    )
                    INSERT INTO quote_snapshots (
                        provider, instrument_id, route_fingerprint, symbol,
                        ts, price, bid, ask, last, close, payload, created_at
                    )
                    SELECT
                        provider, instrument_id, route_fingerprint, symbol,
                        ts, price, bid, ask, last, close, payload, now()
                    FROM input
                    ORDER BY input_ordinal
                    ON CONFLICT (provider, instrument_id, route_fingerprint, ts) DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        price = EXCLUDED.price,
                        bid = EXCLUDED.bid,
                        ask = EXCLUDED.ask,
                        last = EXCLUDED.last,
                        close = EXCLUDED.close,
                        payload = EXCLUDED.payload,
                        created_at = now()
                    WHERE
                        quote_snapshots.price IS DISTINCT FROM EXCLUDED.price
                        OR quote_snapshots.bid IS DISTINCT FROM EXCLUDED.bid
                        OR quote_snapshots.ask IS DISTINCT FROM EXCLUDED.ask
                        OR quote_snapshots.last IS DISTINCT FROM EXCLUDED.last
                        OR quote_snapshots.close IS DISTINCT FROM EXCLUDED.close
                        OR quote_snapshots.payload IS DISTINCT FROM EXCLUDED.payload
                    """,
                    (Jsonb(input_payload),),
                )

    def prune_market_snapshot_history(
        self,
        *,
        quote_before: datetime,
        gex_before: datetime,
        future_after: datetime,
        batch_limit: int = 10_000,
    ) -> dict[str, int]:
        """Bound durable quote/GEX history without touching realtime delivery."""

        for field_name, value in (
            ("quote_before", quote_before),
            ("gex_before", gex_before),
            ("future_after", future_after),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"MARKET_SNAPSHOT_RETENTION_{field_name.upper()}_UTC_REQUIRED")
        quote_cutoff = ensure_utc(quote_before)
        gex_cutoff = ensure_utc(gex_before)
        future_cutoff = ensure_utc(future_after)
        if not quote_cutoff < future_cutoff or not gex_cutoff < future_cutoff:
            raise ValueError("MARKET_SNAPSHOT_RETENTION_RANGE_INVALID")
        if type(batch_limit) is not int or not 1 <= batch_limit <= 100_000:
            raise ValueError("MARKET_SNAPSHOT_RETENTION_BATCH_LIMIT_INVALID")
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("market-snapshot-retention",),
                )
                cur.execute(
                    _timescaledb_sql(
                        cur,
                        """
                        SELECT {timescaledb}.drop_chunks(
                            'quote_snapshots'::regclass,
                            older_than => %s::timestamptz
                        )
                        """,
                    ),
                    (quote_cutoff,),
                )
                dropped_quote_chunks = len(cur.fetchall())
                cur.execute(
                    """
                    DELETE FROM quote_snapshots
                    WHERE ctid IN (
                        SELECT ctid
                        FROM quote_snapshots
                        WHERE ts < %s OR ts > %s
                        ORDER BY ts ASC
                        LIMIT %s
                    )
                    """,
                    (quote_cutoff, future_cutoff, batch_limit),
                )
                deleted_quotes = int(cur.rowcount or 0)
                cur.execute(
                    """
                    DELETE FROM gex_snapshots
                    WHERE ctid IN (
                        SELECT ctid
                        FROM gex_snapshots
                        WHERE captured_at < %s OR captured_at > %s
                        ORDER BY captured_at ASC
                        LIMIT %s
                    )
                    """,
                    (gex_cutoff, future_cutoff, batch_limit),
                )
                deleted_gex = int(cur.rowcount or 0)
                cur.execute(
                    """
                    SELECT
                        EXISTS (
                            SELECT 1 FROM quote_snapshots
                            WHERE ts < %s OR ts > %s
                            LIMIT 1
                        ),
                        EXISTS (
                            SELECT 1 FROM gex_snapshots
                            WHERE captured_at < %s OR captured_at > %s
                            LIMIT 1
                        )
                    """,
                    (quote_cutoff, future_cutoff, gex_cutoff, future_cutoff),
                )
                remaining = cur.fetchone()
        return {
            "dropped_quote_chunks": dropped_quote_chunks,
            "deleted_quote_rows": deleted_quotes,
            "deleted_gex_rows": deleted_gex,
            "has_more": int(bool(remaining and (remaining[0] or remaining[1]))),
        }

    def read_quote_snapshots(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        provider: str,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="QUOTE_SNAPSHOT_ROUTE_FINGERPRINT",
        )
        stable_id = require_exact_identity_text(
            instrument_id,
            field="QUOTE_SNAPSHOT_INSTRUMENT_ID",
        )
        if type(limit) is not int or limit <= 0:
            raise ValueError("QUOTE_SNAPSHOT_LIMIT_MUST_BE_POSITIVE_INTEGER")
        if not isinstance(start, datetime) or start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("QUOTE_SNAPSHOT_START_MUST_BE_AWARE")
        if not isinstance(end, datetime) or end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("QUOTE_SNAPSHOT_END_MUST_BE_AWARE")
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if end_utc < start_utc:
            raise ValueError("QUOTE_SNAPSHOT_RANGE_INVALID")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ts, price, bid, ask, last, close, payload
                    FROM quote_snapshots
                    WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                      AND ts >= %s AND ts <= %s
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (
                        provider_key_for_source(provider),
                        stable_id,
                        fingerprint,
                        start_utc,
                        end_utc,
                        limit,
                    ),
                )
                rows = cur.fetchall()
        snapshots = [
            {
                "ts": ensure_utc(row[0]).isoformat(),
                "price": row[1],
                "bid": row[2],
                "ask": row[3],
                "last": row[4],
                "close": row[5],
                "payload": row[6] if isinstance(row[6], dict) else {},
            }
            for row in rows
        ]
        return list(reversed(snapshots))

    def read_quote_snapshot_buckets_batch(
        self,
        routes: Sequence[tuple[str, str, str]],
        start: datetime,
        end: datetime,
        *,
        bucket_minutes: int,
        max_points: int,
    ) -> dict[tuple[str, str], list[dict[str, float | str]]]:
        """Read the latest persisted quote in each bucket for exact routes."""

        if not isinstance(start, datetime) or start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("QUOTE_SNAPSHOT_START_MUST_BE_AWARE")
        if not isinstance(end, datetime) or end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("QUOTE_SNAPSHOT_END_MUST_BE_AWARE")
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        if end_utc < start_utc:
            raise ValueError("QUOTE_SNAPSHOT_RANGE_INVALID")
        if type(bucket_minutes) is not int or bucket_minutes <= 0:
            raise ValueError("QUOTE_SNAPSHOT_BUCKET_MINUTES_MUST_BE_POSITIVE_INTEGER")
        if type(max_points) is not int or max_points <= 0:
            raise ValueError("QUOTE_SNAPSHOT_MAX_POINTS_MUST_BE_POSITIVE_INTEGER")
        bucket_size = bucket_minutes
        point_limit = max_points

        prepared: list[tuple[str, str, str]] = []
        provider_by_identity: dict[tuple[str, str], str] = {}
        grouped: dict[tuple[str, str], list[dict[str, float | str]]] = {}
        for provider, instrument_id, route_fingerprint in routes:
            stable_id = require_exact_identity_text(
                instrument_id,
                field="QUOTE_SNAPSHOT_INSTRUMENT_ID",
            )
            fingerprint = require_exact_identity_text(
                route_fingerprint,
                field="QUOTE_SNAPSHOT_ROUTE_FINGERPRINT",
            )
            provider_key = provider_key_for_source(provider)
            identity = (stable_id, fingerprint)
            previous_provider = provider_by_identity.get(identity)
            if previous_provider is not None and previous_provider != provider_key:
                raise ValueError("QUOTE_SNAPSHOT_ROUTE_PROVIDER_CONFLICT")
            grouped.setdefault(identity, [])
            if previous_provider is None:
                provider_by_identity[identity] = provider_key
                prepared.append((provider_key, stable_id, fingerprint))
        if not prepared:
            return grouped

        values_sql = ", ".join("(%s::text, %s::text, %s::text)" for _route in prepared)
        params: list[Any] = [value for route in prepared for value in route]
        params.extend(
            (
                timedelta(minutes=bucket_size),
                start_utc,
                end_utc,
                point_limit,
            )
        )
        sql = f"""
            WITH requested(provider, instrument_id, route_fingerprint) AS (
                VALUES {values_sql}
            ), sampled AS (
                SELECT
                    quote_snapshots.provider,
                    quote_snapshots.instrument_id,
                    quote_snapshots.route_fingerprint,
                    date_bin(
                        %s::interval,
                        quote_snapshots.ts,
                        TIMESTAMPTZ '1970-01-01 00:00:00+00'
                    ) AS bucket_ts,
                    quote_snapshots.ts AS sample_ts,
                    quote_snapshots.price
                FROM quote_snapshots
                JOIN requested
                  ON requested.provider = quote_snapshots.provider
                 AND requested.instrument_id = quote_snapshots.instrument_id
                 AND requested.route_fingerprint = quote_snapshots.route_fingerprint
                WHERE quote_snapshots.ts >= %s
                  AND quote_snapshots.ts <= %s
                  AND quote_snapshots.price IS NOT NULL
            ), bucket_latest AS (
                SELECT DISTINCT ON (
                    provider,
                    instrument_id,
                    route_fingerprint,
                    bucket_ts
                )
                    provider,
                    instrument_id,
                    route_fingerprint,
                    bucket_ts,
                    price
                FROM sampled
                ORDER BY
                    provider,
                    instrument_id,
                    route_fingerprint,
                    bucket_ts,
                    sample_ts DESC
            ), ranked AS (
                SELECT
                    provider,
                    instrument_id,
                    route_fingerprint,
                    bucket_ts,
                    price,
                    ROW_NUMBER() OVER (
                        PARTITION BY provider, instrument_id, route_fingerprint
                        ORDER BY bucket_ts DESC
                    ) AS point_rank
                FROM bucket_latest
            )
            SELECT instrument_id, route_fingerprint, bucket_ts, price
            FROM ranked
            WHERE point_rank <= %s
            ORDER BY instrument_id, route_fingerprint, bucket_ts
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        for row in rows:
            identity = (
                require_exact_identity_text(row[0], field="QUOTE_SNAPSHOT_INSTRUMENT_ID"),
                require_exact_identity_text(row[1], field="QUOTE_SNAPSHOT_ROUTE_FINGERPRINT"),
            )
            if identity not in grouped:
                raise RuntimeError("QUOTE_SNAPSHOT_BATCH_RETURNED_UNREQUESTED_ROUTE")
            price = float_or_none(row[3])
            if price is None:
                continue
            grouped[identity].append(
                {
                    "ts": ensure_utc(row[2]).isoformat(),
                    "price": price,
                }
            )
        return grouped
