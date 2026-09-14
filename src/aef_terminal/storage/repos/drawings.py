from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.alerts.definition_identity import (
    price_alert_definition_identity,
    require_unique_price_alert_definitions,
)
from aef_terminal.alerts.runtime_registry import (
    price_alert_runtime,
    retain_price_alert,
)
from aef_terminal.alerts.runtime_contract import validate_price_alert
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.drawing_contract import validate_durable_drawing_payload
from aef_terminal.storage.repos.route_authority import (
    lock_provider_instrument_lifecycle_on_cursor,
)


def _verify_drawing_route_current_on_cursor(
    cur: Any,
    *,
    provider: str,
    instrument_id: str,
    provider_contract_id: str,
) -> None:
    provider, instrument_id = lock_provider_instrument_lifecycle_on_cursor(
        cur,
        provider=provider,
        instrument_id=instrument_id,
    )
    provider_contract_id = require_exact_identity_text(
        provider_contract_id,
        field="provider_contract_id",
    )
    cur.execute(
        """
        SELECT identity_scope, asset_class
        FROM instruments
        WHERE provider = %s AND instrument_id = %s
        """,
        (provider, instrument_id),
    )
    instrument_row = cur.fetchone()
    if instrument_row is None:
        raise RuntimeError("DRAWING_INSTRUMENT_NOT_FOUND")
    identity_scope = require_exact_identity_text(
        instrument_row[0],
        field="identity_scope",
    )
    asset_class = require_exact_identity_text(
        instrument_row[1],
        field="asset_class",
    )
    if identity_scope != "root" or asset_class != "future":
        return
    cur.execute(
        """
        SELECT metadata ->> 'provider_contract_id'
        FROM futures_contracts
        WHERE provider = %s AND instrument_id = %s AND is_current = true
        FOR UPDATE
        """,
        (provider, instrument_id),
    )
    current_row = cur.fetchone()
    current_provider_contract_id = (
        require_exact_identity_text(
            current_row[0],
            field="current_provider_contract_id",
        )
        if current_row is not None
        else ""
    )
    if current_provider_contract_id != provider_contract_id:
        raise RuntimeError(
            "DRAWING_FUTURES_ROUTE_STALE "
            f"expected_provider_contract_id={current_provider_contract_id!r} "
            f"actual_provider_contract_id={provider_contract_id!r}"
        )


def _rebind_futures_root_drawings_on_cursor(
    cur: Any,
    *,
    provider: str,
    instrument_id: str,
    previous_route_fingerprint: str,
    next_route_fingerprint: str,
    previous_provider_contract_id: str,
    next_provider_contract_id: str,
    lifecycle_lock_held: bool = False,
) -> int:
    """Move one futures root's drawings with its provider-owned route rollover."""

    if lifecycle_lock_held is not True:
        raise RuntimeError("FUTURES_DRAWING_REBIND_LIFECYCLE_LOCK_REQUIRED")
    provider = require_exact_identity_text(provider, field="provider")
    instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    previous_route_fingerprint = require_exact_identity_text(
        previous_route_fingerprint,
        field="previous_route_fingerprint",
    )
    next_route_fingerprint = require_exact_identity_text(
        next_route_fingerprint,
        field="next_route_fingerprint",
    )
    previous_provider_contract_id = require_exact_identity_text(
        previous_provider_contract_id,
        field="previous_provider_contract_id",
    )
    next_provider_contract_id = require_exact_identity_text(
        next_provider_contract_id,
        field="next_provider_contract_id",
    )
    if previous_route_fingerprint == next_route_fingerprint:
        raise ValueError("FUTURES_DRAWING_REBIND_ROUTE_MUST_CHANGE")
    if previous_provider_contract_id == next_provider_contract_id:
        raise ValueError("FUTURES_DRAWING_REBIND_CONTRACT_MUST_CHANGE")

    cur.execute(
        """
        SELECT id, route_fingerprint, timeframe, updated_at, payload
        FROM drawings
        WHERE instrument_id = %s
          AND (
              route_fingerprint = ANY(%s::text[])
              OR payload ->> 'provider_contract_id' = ANY(%s::text[])
          )
        ORDER BY id
        FOR UPDATE
        """,
        (
            instrument_id,
            [previous_route_fingerprint, next_route_fingerprint],
            [previous_provider_contract_id, next_provider_contract_id],
        ),
    )
    source_rows: dict[str, tuple[str, Any]] = {}
    for drawing_id, route_fingerprint, timeframe, updated_at, payload in cur.fetchall():
        drawing_id = require_exact_identity_text(drawing_id, field="DRAWING_ID")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="DRAWING_ROUTE_FINGERPRINT",
        )
        timeframe = require_exact_identity_text(
            timeframe,
            field="DRAWING_TIMEFRAME",
        )
        if not isinstance(payload, dict):
            raise ValueError("DRAWING_ROW_INVALID: stored payload must be an object")
        if payload.get("id") != drawing_id:
            raise ValueError("DRAWING_SCOPE_MISMATCH: id")
        if payload.get("instrument_id") != instrument_id:
            raise ValueError("DRAWING_SCOPE_MISMATCH: instrument_id")
        payload_route_fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="DRAWING_ROUTE_FINGERPRINT",
        )
        payload_provider = require_exact_identity_text(
            payload.get("provider"),
            field="DRAWING_PROVIDER",
        )
        payload_provider_contract_id = require_exact_identity_text(
            payload.get("provider_contract_id"),
            field="DRAWING_PROVIDER_CONTRACT_ID",
        )
        if payload_route_fingerprint != route_fingerprint:
            raise ValueError("DRAWING_SCOPE_MISMATCH: route_fingerprint")
        if (
            route_fingerprint == next_route_fingerprint
            or payload_provider_contract_id == next_provider_contract_id
        ):
            raise RuntimeError("FUTURES_DRAWING_REBIND_TARGET_NOT_EMPTY")
        if (
            route_fingerprint != previous_route_fingerprint
            or payload_provider != provider
            or payload_provider_contract_id != previous_provider_contract_id
        ):
            raise RuntimeError("FUTURES_DRAWING_REBIND_SOURCE_SCOPE_MISMATCH")
        if drawing_id in source_rows:
            raise RuntimeError("FUTURES_DRAWING_REBIND_DUPLICATE_ID")
        source_rows[drawing_id] = (timeframe, updated_at)

    if not source_rows:
        return 0
    cur.execute(
        """
        UPDATE drawings
        SET payload = jsonb_set(
            jsonb_set(
                payload,
                '{route_fingerprint}',
                to_jsonb(%s::text),
                false
            ),
            '{provider_contract_id}',
            to_jsonb(%s::text),
            false
        )
        WHERE instrument_id = %s AND route_fingerprint = %s
        """,
        (
            next_route_fingerprint,
            next_provider_contract_id,
            instrument_id,
            previous_route_fingerprint,
        ),
    )
    moved = int(cur.rowcount or 0)
    if moved != len(source_rows):
        raise RuntimeError(
            "FUTURES_DRAWING_REBIND_WRITE_COUNT_MISMATCH "
            f"expected={len(source_rows)} actual={moved}"
        )
    cur.execute(
        """
        SELECT id, timeframe, updated_at, payload
        FROM drawings
        WHERE instrument_id = %s AND route_fingerprint = %s
        ORDER BY id
        """,
        (instrument_id, next_route_fingerprint),
    )
    readback_rows = cur.fetchall()
    if len(readback_rows) != len(source_rows):
        raise RuntimeError("FUTURES_DRAWING_REBIND_READBACK_COUNT_MISMATCH")
    for drawing_id, timeframe, updated_at, payload in readback_rows:
        drawing_id = require_exact_identity_text(drawing_id, field="DRAWING_ID")
        expected = source_rows.get(drawing_id)
        if expected != (timeframe, updated_at) or not isinstance(payload, dict):
            raise RuntimeError("FUTURES_DRAWING_REBIND_READBACK_MISMATCH")
        if (
            payload.get("id") != drawing_id
            or payload.get("instrument_id") != instrument_id
            or payload.get("route_fingerprint") != next_route_fingerprint
            or payload.get("provider") != provider
            or payload.get("provider_contract_id") != next_provider_contract_id
        ):
            raise RuntimeError("FUTURES_DRAWING_REBIND_READBACK_MISMATCH")
    return moved


class DrawingsRepoMixin:
    """PostgreSQL repository mixin."""

    @staticmethod
    def _materialize_price_alert_rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for alert_id, instrument_id, route_fingerprint, symbol, timeframe, payload in rows:
            if not isinstance(payload, dict):
                raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
            expected_fields = {
                "id": alert_id,
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "symbol": symbol,
                "timeframe": timeframe,
            }
            for field, expected in expected_fields.items():
                if payload.get(field) != require_exact_identity_text(
                    expected, field=f"PRICE_ALERT_{field.upper()}"
                ):
                    raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {field}")
            for field in (
                "provider",
                "provider_contract_id",
            ):
                require_exact_identity_text(
                    payload.get(field), field=f"PRICE_ALERT_{field.upper()}"
                )
            result.append(payload)
        return result

    @staticmethod
    def _lock_alert_scope(
        cur: Any,
        *,
        object_id: str,
        instrument_id: str,
        route_fingerprint: str,
    ) -> tuple[str, str, str] | None:
        object_id = require_exact_identity_text(object_id, field="object_id")
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        namespace = "price-alerts"
        base_query = """
            SELECT instrument_id, route_fingerprint, timeframe
            FROM price_alerts
            WHERE id = %s
        """
        cur.execute(
            base_query + " AND instrument_id = %s AND route_fingerprint = %s",
            (object_id, instrument_id, route_fingerprint),
        )
        row = cur.fetchone()
        if row is None:
            return None
        exact_instrument_id = require_exact_identity_text(row[0], field="instrument_id")
        exact_route_fingerprint = require_exact_identity_text(row[1], field="route_fingerprint")
        timeframe = require_exact_identity_text(row[2], field="timeframe")
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (
                f"{namespace}:{exact_instrument_id}:{exact_route_fingerprint}",
                timeframe,
            ),
        )
        return exact_instrument_id, exact_route_fingerprint, timeframe

    @staticmethod
    def _cancel_pending_alert_delivery(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "telegramDeliveryStatus": "cancelled",
            "telegramPendingPayload": None,
            "telegramPendingSince": 0,
            "telegramNextRetryAt": 0,
            "telegramRetryCount": 0,
            "telegramClaimedAt": 0,
            "telegramLastError": "",
        }

    @staticmethod
    def _next_alert_generation(
        payload: dict[str, Any],
        requested: int,
    ) -> int:
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
            raise ValueError("ALERT_GENERATION_INVALID: requested")
        current = payload.get("rearmedAt")
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise ValueError("ALERT_GENERATION_INVALID: rearmedAt")
        return max(requested, current + 1)

    def read_drawings(
        self,
        instrument_id: str,
        timeframe: str,
        *,
        route_fingerprint: str,
    ) -> list[dict[str, Any]]:
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, symbol, timeframe, payload
                    FROM drawings
                    WHERE instrument_id = %s AND route_fingerprint = %s AND timeframe = %s
                    ORDER BY updated_at ASC, id ASC
                    """,
                    (instrument_id, route_fingerprint, timeframe),
                )
                rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        for drawing_id, symbol, stored_timeframe, payload in rows:
            if not isinstance(payload, dict):
                raise ValueError("DRAWING_ROW_INVALID: stored payload must be an object")
            if payload.get("id") != require_exact_identity_text(drawing_id, field="DRAWING_ID"):
                raise ValueError("DRAWING_SCOPE_MISMATCH: id")
            if payload.get("instrument_id") != instrument_id:
                raise ValueError("DRAWING_SCOPE_MISMATCH: instrument_id")
            if payload.get("route_fingerprint") != route_fingerprint:
                raise ValueError("DRAWING_SCOPE_MISMATCH: route_fingerprint")
            if payload.get("symbol") != require_exact_identity_text(symbol, field="DRAWING_SYMBOL"):
                raise ValueError("DRAWING_SCOPE_MISMATCH: symbol")
            if (
                require_exact_identity_text(stored_timeframe, field="DRAWING_TIMEFRAME")
                != timeframe
            ):
                raise ValueError("DRAWING_SCOPE_MISMATCH: timeframe")
            require_exact_identity_text(payload.get("provider"), field="DRAWING_PROVIDER")
            require_exact_identity_text(
                payload.get("provider_contract_id"),
                field="DRAWING_PROVIDER_CONTRACT_ID",
            )
            result.append(payload)
        return result

    def replace_drawings(
        self,
        instrument_id: str,
        presentation_symbol: str,
        timeframe: str,
        drawings: Sequence[dict[str, Any]],
        *,
        route_fingerprint: str,
        provider: str,
        provider_contract_id: str,
    ) -> None:
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        presentation_symbol = require_exact_identity_text(
            presentation_symbol,
            field="presentation_symbol",
        )
        timeframe = require_exact_identity_text(timeframe, field="timeframe")
        provider = require_exact_identity_text(provider, field="provider")
        provider_contract_id = require_exact_identity_text(
            provider_contract_id, field="provider_contract_id"
        )
        if any(not isinstance(item, dict) for item in drawings):
            raise ValueError("DRAWING_ROW_INVALID: drawings must contain only objects")
        persisted_drawings = [dict(item) for item in drawings]
        drawing_ids: set[str] = set()
        for item in persisted_drawings:
            drawing_id = require_exact_identity_text(item.get("id"), field="DRAWING_ID")
            if drawing_id in drawing_ids:
                raise ValueError(f"DRAWING_ID_DUPLICATE: {drawing_id}")
            drawing_ids.add(drawing_id)
            item_presentation_symbol = require_exact_identity_text(
                item.get("symbol", presentation_symbol),
                field="DRAWING_SYMBOL",
            )
            for field, expected in (
                ("instrument_id", instrument_id),
                ("route_fingerprint", route_fingerprint),
                ("provider", provider),
                ("provider_contract_id", provider_contract_id),
            ):
                if field in item and item[field] != expected:
                    raise ValueError(f"DRAWING_SCOPE_MISMATCH: {field}")
            item.update(
                {
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "symbol": item_presentation_symbol,
                    "provider": provider,
                    "provider_contract_id": provider_contract_id,
                }
            )
            validate_durable_drawing_payload(item)
        self._require_canonical_writer_lease()
        self._psycopg()
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    _verify_drawing_route_current_on_cursor(
                        cur,
                        provider=provider,
                        instrument_id=instrument_id,
                        provider_contract_id=provider_contract_id,
                    )
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                        (
                            f"drawings:{instrument_id}:{route_fingerprint}",
                            str(timeframe),
                        ),
                    )
                    cur.execute(
                        """
                        SELECT id, symbol, payload
                        FROM drawings
                        WHERE instrument_id = %s
                          AND route_fingerprint = %s
                          AND timeframe = %s
                        ORDER BY updated_at ASC, id ASC
                        FOR UPDATE
                        """,
                        (instrument_id, route_fingerprint, timeframe),
                    )
                    locked_ids: set[str] = set()
                    for stored_id, stored_symbol, stored_payload in cur.fetchall():
                        if not isinstance(stored_payload, dict):
                            raise ValueError(
                                "DRAWING_ROW_INVALID: stored payload must be an object"
                            )
                        drawing_id = require_exact_identity_text(stored_id, field="DRAWING_ID")
                        if drawing_id in locked_ids:
                            raise ValueError(f"DRAWING_ID_DUPLICATE: {drawing_id}")
                        locked_ids.add(drawing_id)
                        if stored_payload.get("id") != drawing_id:
                            raise ValueError("DRAWING_SCOPE_MISMATCH: id")
                        exact_stored_symbol = require_exact_identity_text(
                            stored_symbol,
                            field="DRAWING_SYMBOL",
                        )
                        if stored_payload.get("symbol") != exact_stored_symbol:
                            raise ValueError("DRAWING_SCOPE_MISMATCH: symbol")
                        for field, expected in (
                            ("instrument_id", instrument_id),
                            ("route_fingerprint", route_fingerprint),
                            ("provider", provider),
                            ("provider_contract_id", provider_contract_id),
                        ):
                            if stored_payload.get(field) != expected:
                                raise ValueError(f"DRAWING_SCOPE_MISMATCH: {field}")
                    cur.execute(
                        "DELETE FROM drawings WHERE instrument_id = %s AND route_fingerprint = %s AND timeframe = %s",
                        (instrument_id, route_fingerprint, timeframe),
                    )
                    input_payload = [
                        {
                            "input_ordinal": input_ordinal,
                            "id": item["id"],
                            "symbol": item["symbol"],
                            "timeframe": timeframe,
                            "payload": {
                                **dict(item),
                                "instrument_id": instrument_id,
                                "route_fingerprint": route_fingerprint,
                                "provider": provider,
                                "provider_contract_id": provider_contract_id,
                            },
                        }
                        for input_ordinal, item in enumerate(persisted_drawings)
                    ]
                    if input_payload:
                        cur.execute(
                            """
                            WITH input AS MATERIALIZED (
                                SELECT *
                                FROM jsonb_to_recordset(%s::jsonb) AS row(
                                    input_ordinal integer,
                                    id text,
                                    symbol text,
                                    timeframe text,
                                    payload jsonb
                                )
                            )
                            INSERT INTO drawings (id, symbol, timeframe, payload, updated_at)
                            SELECT id, symbol, timeframe, payload, now()
                            FROM input
                            ORDER BY input_ordinal
                            """,
                            (Jsonb(input_payload),),
                        )

    def read_price_alerts(
        self,
        instrument_id: str | None = None,
        timeframe: str | None = None,
        *,
        route_fingerprint: str | None = None,
        route_pairs: Sequence[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_timeframe = str(timeframe or "").strip()
        if not normalized_timeframe:
            raise ValueError("read_price_alerts requires timeframe")
        exact_route_pairs: list[tuple[str, str]] = []
        seen_route_pairs: set[tuple[str, str]] = set()
        if route_pairs is not None:
            if instrument_id is not None or route_fingerprint is not None:
                raise ValueError("read_price_alerts accepts either one exact route or route_pairs")
            for pair in route_pairs:
                if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                    raise ValueError(
                        "read_price_alerts route_pairs must contain exact identity pairs"
                    )
                exact_pair = (
                    require_exact_identity_text(pair[0], field="PRICE_ALERT_INSTRUMENT_ID"),
                    require_exact_identity_text(pair[1], field="PRICE_ALERT_ROUTE_FINGERPRINT"),
                )
                if exact_pair not in seen_route_pairs:
                    seen_route_pairs.add(exact_pair)
                    exact_route_pairs.append(exact_pair)
            if not exact_route_pairs:
                return []
        else:
            exact_route_pairs.append(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT price_alert.id, price_alert.symbol, price_alert.timeframe,
                           price_alert.payload
                    FROM price_alerts AS price_alert
                    WHERE EXISTS (
                        SELECT 1
                        FROM unnest(%s::text[], %s::text[])
                            AS route_filter(instrument_id, route_fingerprint)
                        WHERE route_filter.instrument_id = price_alert.instrument_id
                          AND route_filter.route_fingerprint = price_alert.route_fingerprint
                    )
                      AND price_alert.timeframe = %s
                      AND price_alert.payload -> 'enabled' = 'true'::jsonb
                      AND (
                          price_alert.payload -> 'armed' = 'true'::jsonb
                          OR price_alert.payload -> 'fired' = 'true'::jsonb
                      )
                    ORDER BY price_alert.updated_at ASC, price_alert.id ASC
                    """,
                    (
                        [pair[0] for pair in exact_route_pairs],
                        [pair[1] for pair in exact_route_pairs],
                        normalized_timeframe,
                    ),
                )
                rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        exact_route_pairs_set = set(exact_route_pairs)
        for alert_id, symbol, stored_timeframe, payload in rows:
            if not isinstance(payload, dict):
                raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
            if payload.get("id") != require_exact_identity_text(alert_id, field="PRICE_ALERT_ID"):
                raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: id")
            exact_pair = (
                require_exact_identity_text(
                    payload.get("instrument_id"), field="PRICE_ALERT_INSTRUMENT_ID"
                ),
                require_exact_identity_text(
                    payload.get("route_fingerprint"),
                    field="PRICE_ALERT_ROUTE_FINGERPRINT",
                ),
            )
            if exact_pair not in exact_route_pairs_set:
                raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: route")
            if payload.get("timeframe") != normalized_timeframe:
                raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: timeframe")
            if (
                require_exact_identity_text(stored_timeframe, field="PRICE_ALERT_TIMEFRAME")
                != normalized_timeframe
            ):
                raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: timeframe")
            if payload.get("symbol") != require_exact_identity_text(
                symbol, field="PRICE_ALERT_SYMBOL"
            ):
                raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: symbol")
            require_exact_identity_text(payload.get("provider"), field="PRICE_ALERT_PROVIDER")
            require_exact_identity_text(
                payload.get("provider_contract_id"),
                field="PRICE_ALERT_PROVIDER_CONTRACT_ID",
            )
            result.append(payload)
        return result

    def read_all_price_alerts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, instrument_id, route_fingerprint, symbol, timeframe, payload
                    FROM price_alerts
                    WHERE instrument_id IS NOT NULL
                      AND payload -> 'enabled' = 'true'::jsonb
                      AND (
                          payload -> 'armed' = 'true'::jsonb
                          OR payload -> 'fired' = 'true'::jsonb
                      )
                    ORDER BY instrument_id ASC, timeframe ASC, updated_at ASC, id ASC
                    """
                )
                rows = cur.fetchall()
        return self._materialize_price_alert_rows(rows)

    def read_price_alert_runtime_snapshot(self) -> list[dict[str, Any]]:
        """Validate and return the exact current-schema runtime snapshot."""

        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, instrument_id, route_fingerprint, symbol, timeframe, payload
                    FROM price_alerts
                    ORDER BY instrument_id ASC, timeframe ASC, updated_at ASC, id ASC
                    """
                )
                rows = cur.fetchall()
        materialized = self._materialize_price_alert_rows(rows)
        for alert in materialized:
            validate_price_alert(alert)
            if not retain_price_alert(alert):
                raise ValueError("PRICE_ALERT_RUNTIME_INERT_ROW")
        return materialized

    def update_price_alert_payload_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        patch: dict[str, Any],
        *,
        expected_rearmed_at: Any = None,
        check_generation: bool = False,
        require_active: bool = False,
        expected_pending_payload: dict[str, Any] | None = None,
        expected_fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        self._psycopg()
        from psycopg.types.json import Jsonb

        if not isinstance(patch, dict) or any(
            not isinstance(field, str) or not field for field in patch
        ):
            raise ValueError("PRICE_ALERT_PATCH_INVALID")
        if not isinstance(check_generation, bool) or not isinstance(require_active, bool):
            raise ValueError("PRICE_ALERT_CAS_INVALID")
        if expected_pending_payload is not None and not isinstance(expected_pending_payload, dict):
            raise ValueError("PRICE_ALERT_CAS_INVALID: expected_pending_payload")
        if expected_fields is not None and (
            not isinstance(expected_fields, Mapping)
            or any(not isinstance(field, str) or not field for field in expected_fields)
        ):
            raise ValueError("PRICE_ALERT_CAS_INVALID: expected_fields")
        alert_id = require_exact_identity_text(alert_id, field="alert_id")
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if (
                    self._lock_alert_scope(
                        cur,
                        object_id=alert_id,
                        instrument_id=instrument_id,
                        route_fingerprint=route_fingerprint,
                    )
                    is None
                ):
                    return None
                cur.execute(
                    """
                    SELECT payload
                    FROM price_alerts
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    FOR UPDATE
                    """,
                    (alert_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                if not isinstance(row[0], dict):
                    raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
                current = dict(row[0])
                validate_price_alert(current)
                if check_generation:
                    current_generation = current.get("rearmedAt")
                    expected_generation = expected_rearmed_at
                    if (
                        isinstance(current_generation, bool)
                        or not isinstance(current_generation, int)
                        or current_generation < 0
                    ):
                        raise ValueError("PRICE_ALERT_FIELD_INVALID: rearmedAt")
                    if (
                        isinstance(expected_generation, bool)
                        or not isinstance(expected_generation, int)
                        or expected_generation < 0
                    ):
                        raise ValueError("PRICE_ALERT_CAS_INVALID: expected_rearmed_at")
                    if current_generation != expected_generation:
                        return None
                if require_active:
                    if not isinstance(current.get("enabled"), bool) or not isinstance(
                        current.get("armed"), bool
                    ):
                        raise ValueError("PRICE_ALERT_FIELD_INVALID: active state")
                    if current["enabled"] is False or current["armed"] is False:
                        return None
                if (
                    expected_pending_payload is not None
                    and current.get("telegramPendingPayload") != expected_pending_payload
                ):
                    return None
                if any(
                    current.get(field) != value
                    for field, value in dict(expected_fields or {}).items()
                ):
                    return None
                updated = {**current, **dict(patch)}
                validate_price_alert(updated)
                if updated == current:
                    return current
                if retain_price_alert(updated):
                    cur.execute(
                        """
                        UPDATE price_alerts
                        SET payload = %s,
                            updated_at = now()
                        WHERE id = %s
                          AND instrument_id = %s
                          AND route_fingerprint = %s
                        RETURNING payload
                        """,
                        (Jsonb(updated), alert_id, instrument_id, route_fingerprint),
                    )
                    saved = cur.fetchone()
                else:
                    cur.execute(
                        """
                        DELETE FROM price_alerts
                        WHERE id = %s
                          AND instrument_id = %s
                          AND route_fingerprint = %s
                        """,
                        (alert_id, instrument_id, route_fingerprint),
                    )
                    saved = (updated,)
        result = saved[0] if saved and isinstance(saved[0], dict) else None
        if result is not None:
            price_alert_runtime.publish_committed(result)
        return result

    def read_price_alert_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
    ) -> dict[str, Any] | None:
        alert_id = require_exact_identity_text(alert_id, field="alert_id")
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload
                    FROM price_alerts
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    """,
                    (alert_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
        if row is None:
            return None
        if not isinstance(row[0], dict):
            raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
        payload = row[0]
        if payload.get("id") != alert_id:
            raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: id")
        if payload.get("instrument_id") != instrument_id:
            raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: instrument_id")
        if payload.get("route_fingerprint") != route_fingerprint:
            raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: route_fingerprint")
        return payload

    def create_or_rearm_price_alert_exact(
        self,
        alert: dict[str, Any],
        *,
        rearmed_at: int,
    ) -> dict[str, Any]:
        self._psycopg()
        from psycopg.types.json import Jsonb

        alert_id = require_exact_identity_text(alert.get("id"), field="alert_id")
        instrument_id = require_exact_identity_text(
            alert.get("instrument_id"), field="instrument_id"
        )
        route_fingerprint = require_exact_identity_text(
            alert.get("route_fingerprint"), field="route_fingerprint"
        )
        provider_contract_id = require_exact_identity_text(
            alert.get("provider_contract_id"), field="provider_contract_id"
        )
        symbol = require_exact_identity_text(alert.get("symbol"), field="symbol")
        timeframe = require_exact_identity_text(alert.get("timeframe"), field="timeframe")
        provider = require_exact_identity_text(alert.get("provider"), field="provider")
        candidate = dict(alert)
        validate_price_alert(candidate)
        definition_identity = price_alert_definition_identity(candidate)
        if not retain_price_alert(candidate):
            raise ValueError("PRICE_ALERT_RUNTIME_INERT_ROW")
        self._require_canonical_writer_lease()
        result: dict[str, Any] | None = None
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (
                        f"price-alerts:{instrument_id}:{route_fingerprint}",
                        timeframe,
                    ),
                )
                cur.execute(
                    """
                    SELECT id, symbol, timeframe, payload
                    FROM price_alerts
                    WHERE instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                    ORDER BY updated_at ASC, id ASC
                    FOR UPDATE
                    """,
                    (instrument_id, route_fingerprint, timeframe),
                )
                stored_alerts: list[tuple[str, dict[str, Any]]] = []
                for existing_id, stored_symbol, stored_timeframe, payload in cur.fetchall():
                    if not isinstance(payload, dict):
                        raise ValueError(
                            "PRICE_ALERT_ROW_INVALID: stored payload must be an object"
                        )
                    validate_price_alert(payload)
                    exact_existing_id = require_exact_identity_text(
                        existing_id, field="PRICE_ALERT_ID"
                    )
                    exact_stored_symbol = require_exact_identity_text(
                        stored_symbol,
                        field="PRICE_ALERT_SYMBOL",
                    )
                    exact_stored_timeframe = require_exact_identity_text(
                        stored_timeframe,
                        field="PRICE_ALERT_TIMEFRAME",
                    )
                    if exact_stored_timeframe != timeframe:
                        raise ValueError("PRICE_ALERT_SCOPE_MISMATCH: timeframe")
                    expected_fields = (
                        ("id", exact_existing_id),
                        ("instrument_id", instrument_id),
                        ("route_fingerprint", route_fingerprint),
                        ("symbol", exact_stored_symbol),
                        ("timeframe", exact_stored_timeframe),
                        ("provider", provider),
                        ("provider_contract_id", provider_contract_id),
                    )
                    for field, expected in expected_fields:
                        if payload.get(field) != expected:
                            raise ValueError(f"PRICE_ALERT_SCOPE_MISMATCH: {field}")
                    stored_alerts.append((exact_existing_id, payload))
                require_unique_price_alert_definitions(
                    payload for _existing_id, payload in stored_alerts
                )
                for exact_existing_id, payload in stored_alerts:
                    matches = price_alert_definition_identity(payload) == definition_identity
                    if not matches:
                        continue
                    generation = self._next_alert_generation(payload, rearmed_at)
                    updated = self._cancel_pending_alert_delivery(
                        {
                            **payload,
                            **{
                                field: candidate[field]
                                for field in (
                                    "price",
                                    "kind",
                                    "label",
                                    "direction",
                                    "toleranceAtr",
                                    "tolerancePoints",
                                    "rearmMinutes",
                                    "level_source",
                                )
                                if field in candidate
                            },
                            "enabled": True,
                            "armed": True,
                            "fired": False,
                            "cooldownUntil": 0,
                            "rearmedAt": generation,
                        }
                    )
                    validate_price_alert(updated)
                    cur.execute(
                        """
                        UPDATE price_alerts
                        SET payload = %s,
                            updated_at = now()
                        WHERE id = %s
                          AND instrument_id = %s
                          AND route_fingerprint = %s
                        RETURNING payload
                        """,
                        (
                            Jsonb(updated),
                            exact_existing_id,
                            instrument_id,
                            route_fingerprint,
                        ),
                    )
                    saved = cur.fetchone()
                    if saved is None or not isinstance(saved[0], dict):
                        raise ValueError("semantic price alert changed concurrently")
                    result = {"created": False, "payload": saved[0]}
                    break
                if result is None:
                    cur.execute(
                        """
                        INSERT INTO price_alerts (id, symbol, timeframe, payload, updated_at)
                        VALUES (%s, %s, %s, %s, now())
                        RETURNING payload
                        """,
                        (alert_id, symbol, timeframe, Jsonb(candidate)),
                    )
                    inserted = cur.fetchone()
                    if inserted is None or not isinstance(inserted[0], dict):
                        raise ValueError("price alert insert failed")
                    result = {"created": True, "payload": inserted[0]}
        if result is None or not isinstance(result.get("payload"), dict):
            raise ValueError("price alert mutation produced no payload")
        price_alert_runtime.publish_committed(result["payload"])
        return result

    def rearm_price_alert_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        *,
        rearmed_at: int,
        definition_patch: dict[str, Any] | None = None,
        expected_rearmed_at: int | None = None,
    ) -> dict[str, Any] | None:
        self._psycopg()
        from psycopg.types.json import Jsonb

        if definition_patch is not None and (
            not isinstance(definition_patch, dict)
            or any(not isinstance(field, str) or not field for field in definition_patch)
        ):
            raise ValueError("PRICE_ALERT_DEFINITION_PATCH_INVALID")
        if expected_rearmed_at is not None and (
            isinstance(expected_rearmed_at, bool)
            or not isinstance(expected_rearmed_at, int)
            or expected_rearmed_at < 0
        ):
            raise ValueError("PRICE_ALERT_CAS_INVALID: expected_rearmed_at")
        alert_id = require_exact_identity_text(alert_id, field="alert_id")
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                locked_scope = self._lock_alert_scope(
                    cur,
                    object_id=alert_id,
                    instrument_id=instrument_id,
                    route_fingerprint=route_fingerprint,
                )
                if locked_scope is None:
                    return None
                timeframe = locked_scope[2]
                cur.execute(
                    """
                    SELECT payload
                    FROM price_alerts
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    FOR UPDATE
                    """,
                    (alert_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                if not isinstance(row[0], dict):
                    raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
                current = dict(row[0])
                validate_price_alert(current)
                if (
                    expected_rearmed_at is not None
                    and current.get("rearmedAt") != expected_rearmed_at
                ):
                    return None
                generation = self._next_alert_generation(current, rearmed_at)
                armed = current.get("armed")
                fired = current.get("fired")
                cooldown_until = current.get("cooldownUntil")
                if not isinstance(armed, bool) or not isinstance(fired, bool):
                    raise ValueError("PRICE_ALERT_FIELD_INVALID: armed/fired")
                if (
                    isinstance(cooldown_until, bool)
                    or not isinstance(cooldown_until, int)
                    or cooldown_until < 0
                ):
                    raise ValueError("PRICE_ALERT_FIELD_INVALID: cooldownUntil")
                pending_payload = current.get("telegramPendingPayload")
                if pending_payload is not None and not isinstance(pending_payload, dict):
                    raise ValueError("PRICE_ALERT_FIELD_INVALID: telegramPendingPayload")
                if (
                    armed is True
                    and fired is False
                    and cooldown_until == 0
                    and not definition_patch
                    and pending_payload is None
                ):
                    return current
                updated = self._cancel_pending_alert_delivery(
                    {
                        **current,
                        **dict(definition_patch or {}),
                        "enabled": True,
                        "armed": True,
                        "fired": False,
                        "cooldownUntil": 0,
                        "rearmedAt": generation,
                    }
                )
                validate_price_alert(updated)
                cur.execute(
                    """
                    SELECT id, instrument_id, route_fingerprint, symbol, timeframe, payload
                    FROM price_alerts
                    WHERE instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                    ORDER BY updated_at ASC, id ASC
                    FOR UPDATE
                    """,
                    (instrument_id, route_fingerprint, timeframe),
                )
                scope_alerts = self._materialize_price_alert_rows(cur.fetchall())
                require_unique_price_alert_definitions(
                    updated if alert["id"] == alert_id else alert for alert in scope_alerts
                )
                cur.execute(
                    """
                    UPDATE price_alerts
                    SET payload = %s,
                        updated_at = now()
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    RETURNING payload
                    """,
                    (Jsonb(updated), alert_id, instrument_id, route_fingerprint),
                )
                saved = cur.fetchone()
        result = saved[0] if saved and isinstance(saved[0], dict) else None
        if result is not None:
            price_alert_runtime.publish_committed(result)
        return result

    def set_price_alert_enabled_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        *,
        enabled: bool,
        rearmed_at: int,
        expected_rearmed_at: int | None = None,
    ) -> dict[str, Any] | None:
        self._psycopg()
        from psycopg.types.json import Jsonb

        if not isinstance(enabled, bool):
            raise ValueError("PRICE_ALERT_FIELD_INVALID: enabled")
        if expected_rearmed_at is not None and (
            isinstance(expected_rearmed_at, bool)
            or not isinstance(expected_rearmed_at, int)
            or expected_rearmed_at < 0
        ):
            raise ValueError("PRICE_ALERT_CAS_INVALID: expected_rearmed_at")
        alert_id = require_exact_identity_text(alert_id, field="alert_id")
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if (
                    self._lock_alert_scope(
                        cur,
                        object_id=alert_id,
                        instrument_id=instrument_id,
                        route_fingerprint=route_fingerprint,
                    )
                    is None
                ):
                    return None
                cur.execute(
                    """
                    SELECT payload
                    FROM price_alerts
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    FOR UPDATE
                    """,
                    (alert_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                if not isinstance(row[0], dict):
                    raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
                current = dict(row[0])
                validate_price_alert(current)
                if (
                    expected_rearmed_at is not None
                    and current.get("rearmedAt") != expected_rearmed_at
                ):
                    return None
                generation = self._next_alert_generation(current, rearmed_at)
                current_enabled = current.get("enabled")
                armed = current.get("armed")
                fired = current.get("fired")
                cooldown_until = current.get("cooldownUntil")
                if (
                    not isinstance(current_enabled, bool)
                    or not isinstance(armed, bool)
                    or not isinstance(fired, bool)
                ):
                    raise ValueError("PRICE_ALERT_FIELD_INVALID: enabled/armed/fired")
                if (
                    isinstance(cooldown_until, bool)
                    or not isinstance(cooldown_until, int)
                    or cooldown_until < 0
                ):
                    raise ValueError("PRICE_ALERT_FIELD_INVALID: cooldownUntil")
                pending_payload = current.get("telegramPendingPayload")
                if pending_payload is not None and not isinstance(pending_payload, dict):
                    raise ValueError("PRICE_ALERT_FIELD_INVALID: telegramPendingPayload")
                if enabled:
                    if (
                        current_enabled is True
                        and armed is True
                        and fired is False
                        and cooldown_until == 0
                        and pending_payload is None
                    ):
                        return current
                    updated = self._cancel_pending_alert_delivery(
                        {
                            **current,
                            "enabled": True,
                            "armed": True,
                            "fired": False,
                            "cooldownUntil": 0,
                            "rearmedAt": generation,
                        }
                    )
                else:
                    updated = self._cancel_pending_alert_delivery(
                        {
                            **current,
                            "enabled": False,
                            "armed": False,
                            "fired": False,
                            "cooldownUntil": 0,
                            "rearmedAt": generation,
                        }
                    )
                validate_price_alert(updated)
                if enabled:
                    cur.execute(
                        """
                        UPDATE price_alerts
                        SET payload = %s,
                            updated_at = now()
                        WHERE id = %s
                          AND instrument_id = %s
                          AND route_fingerprint = %s
                        RETURNING payload
                        """,
                        (Jsonb(updated), alert_id, instrument_id, route_fingerprint),
                    )
                    saved = cur.fetchone()
                    result = saved[0] if saved and isinstance(saved[0], dict) else None
                else:
                    cur.execute(
                        """
                        DELETE FROM price_alerts
                        WHERE id = %s
                          AND instrument_id = %s
                          AND route_fingerprint = %s
                        """,
                        (alert_id, instrument_id, route_fingerprint),
                    )
                    result = updated
        if result is not None:
            if enabled:
                price_alert_runtime.publish_committed(result)
            else:
                price_alert_runtime.remove_committed(
                    alert_id,
                    instrument_id,
                    route_fingerprint,
                )
        return result

    def delete_price_alert_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        *,
        deleted_at: int,
        expected_rearmed_at: int | None = None,
    ) -> dict[str, Any] | None:
        if expected_rearmed_at is not None and (
            isinstance(expected_rearmed_at, bool)
            or not isinstance(expected_rearmed_at, int)
            or expected_rearmed_at < 0
        ):
            raise ValueError("PRICE_ALERT_CAS_INVALID: expected_rearmed_at")
        alert_id = require_exact_identity_text(alert_id, field="alert_id")
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if (
                    self._lock_alert_scope(
                        cur,
                        object_id=alert_id,
                        instrument_id=instrument_id,
                        route_fingerprint=route_fingerprint,
                    )
                    is None
                ):
                    return None
                cur.execute(
                    """
                    SELECT payload
                    FROM price_alerts
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    FOR UPDATE
                    """,
                    (alert_id, instrument_id, route_fingerprint),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                if not isinstance(row[0], dict):
                    raise ValueError("PRICE_ALERT_ROW_INVALID: stored payload must be an object")
                current = dict(row[0])
                validate_price_alert(current)
                if (
                    expected_rearmed_at is not None
                    and current.get("rearmedAt") != expected_rearmed_at
                ):
                    return None
                self._next_alert_generation(current, deleted_at)
                cur.execute(
                    """
                    DELETE FROM price_alerts
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                    """,
                    (alert_id, instrument_id, route_fingerprint),
                )
        price_alert_runtime.remove_committed(
            alert_id,
            instrument_id,
            route_fingerprint,
        )
        return current

    def delete_price_alert_scope_exact(
        self,
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str,
    ) -> int:
        instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
        route_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
        timeframe = require_exact_identity_text(timeframe, field="timeframe")
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (
                        f"price-alerts:{instrument_id}:{route_fingerprint}",
                        timeframe,
                    ),
                )
                cur.execute(
                    """
                    SELECT id, instrument_id, route_fingerprint, symbol, timeframe, payload
                    FROM price_alerts
                    WHERE instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                    ORDER BY updated_at ASC, id ASC
                    FOR UPDATE
                    """,
                    (instrument_id, route_fingerprint, timeframe),
                )
                rows = cur.fetchall()
                alerts = self._materialize_price_alert_rows(rows)
                for alert in alerts:
                    validate_price_alert(alert)
                cur.execute(
                    """
                    DELETE FROM price_alerts
                    WHERE instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                    """,
                    (instrument_id, route_fingerprint, timeframe),
                )
                deleted = int(cur.rowcount)
        price_alert_runtime.remove_scope_committed(
            instrument_id,
            route_fingerprint,
            timeframe,
        )
        return deleted
