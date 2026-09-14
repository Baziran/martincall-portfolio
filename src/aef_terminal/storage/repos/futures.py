from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import batched
from typing import Any

from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
    require_exact_positive_number,
)
from aef_terminal.data.providers import provider_key_for_source
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    FuturesRouteTransition,
    canonical_bar_commit_receipt,
)
from aef_terminal.domain import Bar
from aef_terminal.runtime.bar_quality import canonical_storage_bar_provenance
from aef_terminal.runtime.timeframes import require_persisted_bar_timeframe
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection
from aef_terminal.storage.bar_projection import (
    read_confirmed_bar_timestamps,
    read_confirmed_bar_logical_projection,
)
from aef_terminal.storage.db_utils import ensure_utc
from aef_terminal.storage.repos.bars import _storage_bar_allowed
from aef_terminal.storage.repos.drawings import (
    _rebind_futures_root_drawings_on_cursor,
)
from aef_terminal.storage.repos.route_authority import (
    lock_provider_instrument_lifecycle_on_cursor,
)


_FUTURES_CANONICAL_WRITE_BATCH_ROWS = 2_048
_LOGGER = logging.getLogger(__name__)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _require_canonical_route(series_type: str, roll_policy: str) -> tuple[str, str]:
    series = require_exact_identity_text(series_type, field="series_type")
    policy = require_exact_identity_text(roll_policy, field="roll_policy")
    return series, policy


def _canonical_futures_bar_admission_metadata(
    metadata: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str, str, str]:
    payload = dict(metadata or {})
    provider_contract_id = require_exact_identity_text(
        payload.get("provider_contract_id"),
        field="metadata.provider_contract_id",
    )
    provider_contract_type = require_exact_identity_text(
        payload.get("provider_contract_type"),
        field="metadata.provider_contract_type",
    )
    data_type = payload.get("data_type")
    if not isinstance(data_type, str) or not data_type or data_type != data_type.strip():
        raise ValueError("metadata.data_type must be an exact non-empty string")
    return payload, provider_contract_id, provider_contract_type, data_type


def _contract_key(contract: Mapping[str, Any]) -> str:
    return require_exact_identity_text(
        contract.get("contract_key"),
        field="contract_key",
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = parse_exact_positive_decimal_provider_id(value)
    if parsed <= 0:
        raise ValueError("numeric provider contract identity must be canonical and exact")
    return parsed


def _validated_futures_route_transition(
    transition: FuturesRouteTransition,
    *,
    provider: str,
    instrument_id: str,
    next_provider_contract_id: str,
) -> FuturesRouteTransition:
    if not isinstance(transition, FuturesRouteTransition):
        raise TypeError("route_transition must be FuturesRouteTransition")
    exact = FuturesRouteTransition(
        provider=require_exact_identity_text(
            transition.provider,
            field="route_transition.provider",
        ),
        instrument_id=require_exact_identity_text(
            transition.instrument_id,
            field="route_transition.instrument_id",
        ),
        previous_route_fingerprint=require_exact_identity_text(
            transition.previous_route_fingerprint,
            field="route_transition.previous_route_fingerprint",
        ),
        next_route_fingerprint=require_exact_identity_text(
            transition.next_route_fingerprint,
            field="route_transition.next_route_fingerprint",
        ),
        previous_session_contract_id=require_exact_identity_text(
            transition.previous_session_contract_id,
            field="route_transition.previous_session_contract_id",
        ),
        next_session_contract_id=require_exact_identity_text(
            transition.next_session_contract_id,
            field="route_transition.next_session_contract_id",
        ),
    )
    if exact.provider != provider or exact.instrument_id != instrument_id:
        raise ValueError("FUTURES_ROUTE_TRANSITION_SCOPE_MISMATCH")
    if exact.next_session_contract_id != next_provider_contract_id:
        raise ValueError("FUTURES_ROUTE_TRANSITION_NEXT_CONTRACT_MISMATCH")
    return exact


def _futures_contract_row_payload(row: Sequence[Any], *, source: str) -> dict[str, Any]:
    metadata = row[13] or {}
    provider_contract_id = require_exact_identity_text(
        metadata.get("provider_contract_id"),
        field="provider_contract_id",
    )
    payload = {
        "provider": require_exact_identity_text(row[0], field="provider"),
        "instrument_id": require_exact_identity_text(row[1], field="instrument_id"),
        "contract_key": require_exact_identity_text(row[2], field="contract_key"),
        "root": require_exact_identity_text(row[3], field="futures_root"),
        "exchange": row[4],
        "currency": row[5],
        "local_symbol": require_exact_identity_text(row[6], field="local_symbol"),
        "con_id": row[7],
        "provider_contract_id": provider_contract_id,
        "expiry": row[8],
        "contract_month": require_exact_identity_text(row[9], field="contract_month"),
        "first_notice_date": row[10].isoformat() if row[10] is not None else None,
        "last_trade_date": row[11].isoformat() if row[11] is not None else None,
        "source": source,
        "resolved_at": ensure_utc(row[12]).isoformat() if row[12] is not None else None,
    }
    for field in ("secid", "trading_class"):
        value = metadata.get(field)
        if value is not None and value != "":
            payload[field] = require_exact_identity_text(value, field=field)
    for field in ("shortname", "name"):
        value = _clean_text(metadata.get(field))
        if value:
            payload[field] = value
    raw_min_tick = metadata.get("min_tick")
    if raw_min_tick is not None:
        payload["min_tick"] = require_exact_positive_number(
            raw_min_tick,
            field="metadata.min_tick",
        )
    return payload


def _write_futures_canonical_rows(
    cur: Any,
    rows: Sequence[tuple[Any, ...]],
) -> tuple[int, list[tuple[Any, ...]]]:
    written_rows = 0
    persisted_rows: list[tuple[Any, ...]] = []
    for batch_index, row_batch in enumerate(batched(rows, _FUTURES_CANONICAL_WRITE_BATCH_ROWS)):
        ordinal_offset = batch_index * _FUTURES_CANONICAL_WRITE_BATCH_ROWS
        input_payload = [
            {
                "input_ordinal": ordinal_offset + ordinal,
                "provider": row[0],
                "instrument_id": row[1],
                "series_type": row[2],
                "roll_policy": row[3],
                "timeframe": row[4],
                "ts": ensure_utc(row[5]).isoformat(),
                "open": row[6],
                "high": row[7],
                "low": row[8],
                "close": row[9],
                "volume": row[10],
                "source": row[11],
                "closed": row[12],
                "metadata": row[13],
                "revision_sequence": row[14],
            }
            for ordinal, row in enumerate(row_batch)
        ]
        input_json = json.dumps(
            input_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        cur.execute(
            """
            WITH input AS MATERIALIZED (
                SELECT *
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    input_ordinal integer,
                    provider text,
                    instrument_id text,
                    series_type text,
                    roll_policy text,
                    timeframe text,
                    ts timestamptz,
                    open double precision,
                    high double precision,
                    low double precision,
                    close double precision,
                    volume double precision,
                    source text,
                    closed boolean,
                    metadata jsonb,
                    revision_sequence bigint
                )
            )
            INSERT INTO futures_continuous_bars (
                provider, instrument_id, series_type, roll_policy, timeframe, ts,
                open, high, low, close, volume, source, closed, metadata, revision_sequence
            )
            SELECT
                provider, instrument_id, series_type, roll_policy, timeframe, ts,
                open, high, low, close, volume, source, closed, metadata, revision_sequence
            FROM input
            ORDER BY input_ordinal
            ON CONFLICT (provider, instrument_id, series_type, roll_policy, timeframe, ts)
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                source = EXCLUDED.source,
                closed = EXCLUDED.closed,
                metadata = EXCLUDED.metadata,
                revision_sequence = EXCLUDED.revision_sequence,
                updated_at = now()
            WHERE
                EXCLUDED.revision_sequence > futures_continuous_bars.revision_sequence
                AND NOT (futures_continuous_bars.closed = true AND EXCLUDED.closed = false)
                AND (
                    futures_continuous_bars.open IS DISTINCT FROM EXCLUDED.open
                    OR futures_continuous_bars.high IS DISTINCT FROM EXCLUDED.high
                    OR futures_continuous_bars.low IS DISTINCT FROM EXCLUDED.low
                    OR futures_continuous_bars.close IS DISTINCT FROM EXCLUDED.close
                    OR futures_continuous_bars.volume IS DISTINCT FROM EXCLUDED.volume
                    OR futures_continuous_bars.source IS DISTINCT FROM EXCLUDED.source
                    OR futures_continuous_bars.closed IS DISTINCT FROM EXCLUDED.closed
                    OR futures_continuous_bars.metadata IS DISTINCT FROM EXCLUDED.metadata
                )
            RETURNING timeframe, ts
            """,
            (input_json,),
        )
        written_rows += len(cur.fetchall())
        cur.execute(
            """
            WITH input AS MATERIALIZED (
                SELECT *
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    input_ordinal integer,
                    provider text,
                    instrument_id text,
                    series_type text,
                    roll_policy text,
                    timeframe text,
                    ts timestamptz,
                    open double precision,
                    high double precision,
                    low double precision,
                    close double precision,
                    volume double precision,
                    source text,
                    closed boolean,
                    metadata jsonb,
                    revision_sequence bigint
                )
            )
            SELECT
                persisted.ts, persisted.open, persisted.high, persisted.low,
                persisted.close, persisted.volume, persisted.timeframe,
                persisted.source, persisted.closed, persisted.revision_sequence
            FROM input
            JOIN futures_continuous_bars AS persisted
              ON persisted.provider = input.provider
             AND persisted.instrument_id = input.instrument_id
             AND persisted.series_type = input.series_type
             AND persisted.roll_policy = input.roll_policy
             AND persisted.timeframe = input.timeframe
             AND persisted.ts = input.ts
            ORDER BY input.input_ordinal
            """,
            (input_json,),
        )
        persisted_rows.extend(cur.fetchall())
    return written_rows, persisted_rows


def _write_futures_contract_rows(
    cur: Any,
    rows: Sequence[tuple[Any, ...]],
) -> tuple[int, list[tuple[Any, ...]]]:
    written_rows = 0
    persisted_rows: list[tuple[Any, ...]] = []
    for batch_index, row_batch in enumerate(batched(rows, _FUTURES_CANONICAL_WRITE_BATCH_ROWS)):
        ordinal_offset = batch_index * _FUTURES_CANONICAL_WRITE_BATCH_ROWS
        input_payload = [
            {
                "input_ordinal": ordinal_offset + ordinal,
                "provider": row[0],
                "instrument_id": row[1],
                "route_fingerprint": row[2],
                "contract_key": row[3],
                "timeframe": row[4],
                "ts": ensure_utc(row[5]).isoformat(),
                "open": row[6],
                "high": row[7],
                "low": row[8],
                "close": row[9],
                "volume": row[10],
                "source": row[11],
                "closed": row[12],
                "revision_sequence": row[13],
            }
            for ordinal, row in enumerate(row_batch)
        ]
        input_json = json.dumps(
            input_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        cur.execute(
            """
            WITH input AS MATERIALIZED (
                SELECT *
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    input_ordinal integer,
                    provider text,
                    instrument_id text,
                    route_fingerprint text,
                    contract_key text,
                    timeframe text,
                    ts timestamptz,
                    open double precision,
                    high double precision,
                    low double precision,
                    close double precision,
                    volume double precision,
                    source text,
                    closed boolean,
                    revision_sequence bigint
                )
            )
            INSERT INTO futures_contract_bars (
                provider, instrument_id, route_fingerprint, contract_key,
                timeframe, ts, open, high, low, close, volume, source,
                closed, revision_sequence
            )
            SELECT
                provider, instrument_id, route_fingerprint, contract_key,
                timeframe, ts, open, high, low, close, volume, source,
                closed, revision_sequence
            FROM input
            ORDER BY input_ordinal
            ON CONFLICT (
                provider, instrument_id, route_fingerprint, contract_key,
                timeframe, ts
            )
            DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                source = EXCLUDED.source,
                closed = EXCLUDED.closed,
                revision_sequence = EXCLUDED.revision_sequence,
                updated_at = now()
            WHERE
                EXCLUDED.revision_sequence > futures_contract_bars.revision_sequence
                AND NOT (futures_contract_bars.closed = true AND EXCLUDED.closed = false)
                AND (
                    futures_contract_bars.open IS DISTINCT FROM EXCLUDED.open
                    OR futures_contract_bars.high IS DISTINCT FROM EXCLUDED.high
                    OR futures_contract_bars.low IS DISTINCT FROM EXCLUDED.low
                    OR futures_contract_bars.close IS DISTINCT FROM EXCLUDED.close
                    OR futures_contract_bars.volume IS DISTINCT FROM EXCLUDED.volume
                    OR futures_contract_bars.source IS DISTINCT FROM EXCLUDED.source
                    OR futures_contract_bars.closed IS DISTINCT FROM EXCLUDED.closed
                )
            RETURNING timeframe, ts
            """,
            (input_json,),
        )
        written_rows += len(cur.fetchall())
        cur.execute(
            """
            WITH input AS MATERIALIZED (
                SELECT *
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    input_ordinal integer,
                    provider text,
                    instrument_id text,
                    route_fingerprint text,
                    contract_key text,
                    timeframe text,
                    ts timestamptz,
                    open double precision,
                    high double precision,
                    low double precision,
                    close double precision,
                    volume double precision,
                    source text,
                    closed boolean,
                    revision_sequence bigint
                )
            )
            SELECT
                persisted.ts, persisted.open, persisted.high, persisted.low,
                persisted.close, persisted.volume, persisted.timeframe,
                persisted.source, persisted.closed, persisted.revision_sequence
            FROM input
            JOIN futures_contract_bars AS persisted
              ON persisted.provider = input.provider
             AND persisted.instrument_id = input.instrument_id
             AND persisted.route_fingerprint = input.route_fingerprint
             AND persisted.contract_key = input.contract_key
             AND persisted.timeframe = input.timeframe
             AND persisted.ts = input.ts
            ORDER BY input.input_ordinal
            """,
            (input_json,),
        )
        persisted_rows.extend(cur.fetchall())
    return written_rows, persisted_rows


class FuturesRepoMixin:
    """PostgreSQL repository mixin for explicit futures contract storage."""

    def _write_futures_contract_on_cursor(
        self,
        cur: Any,
        contract: Mapping[str, Any],
        *,
        route_transition: FuturesRouteTransition | None = None,
    ) -> str:
        self._psycopg()
        from psycopg.types.json import Jsonb

        provider = require_exact_identity_text(
            contract.get("provider"),
            field="provider",
        )
        if provider_key_for_source(provider) != provider:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider}")
        instrument_id = require_exact_identity_text(
            contract.get("instrument_id"),
            field="instrument_id",
        )
        key = _contract_key(contract)
        provider_contract_id = require_exact_identity_text(
            contract.get("provider_contract_id"),
            field="provider_contract_id",
        )
        root = require_exact_identity_text(contract.get("root"), field="futures_root")
        local_symbol = require_exact_identity_text(
            contract.get("local_symbol"),
            field="local_symbol",
        )
        contract_month = require_exact_identity_text(
            contract.get("contract_month"),
            field="contract_month",
        )
        is_current = contract.get("is_current")
        if not isinstance(is_current, bool):
            raise ValueError("is_current must be a boolean")
        if route_transition is not None and not is_current:
            raise ValueError("route_transition requires is_current=true")
        exchange = require_exact_identity_text(
            contract.get("exchange", ""),
            field="exchange",
            allow_empty=True,
        )
        currency = require_exact_identity_text(
            contract.get("currency", ""),
            field="currency",
            allow_empty=True,
        )
        expiry = require_exact_identity_text(
            contract.get("expiry", ""),
            field="expiry",
            allow_empty=True,
        )
        if not provider or not instrument_id or not key or not root:
            raise ValueError("provider, instrument_id, provider contract id/root are required")
        metadata = dict(contract.get("metadata") or {})
        existing_provider_contract_id = metadata.get("provider_contract_id")
        if (
            existing_provider_contract_id is not None
            and require_exact_identity_text(
                existing_provider_contract_id,
                field="metadata.provider_contract_id",
            )
            != provider_contract_id
        ):
            raise ValueError("metadata.provider_contract_id must match provider_contract_id")
        metadata["provider_contract_id"] = provider_contract_id
        exact_route_transition = (
            _validated_futures_route_transition(
                route_transition,
                provider=provider,
                instrument_id=instrument_id,
                next_provider_contract_id=provider_contract_id,
            )
            if route_transition is not None
            else None
        )
        row = (
            provider,
            instrument_id,
            key,
            root,
            exchange,
            currency,
            local_symbol,
            _optional_int(contract.get("con_id")),
            expiry,
            contract_month,
            contract.get("first_notice_date"),
            contract.get("last_trade_date"),
            is_current,
            is_current,
            Jsonb(metadata),
        )
        lock_provider_instrument_lifecycle_on_cursor(
            cur,
            provider=provider,
            instrument_id=instrument_id,
        )
        if is_current:
            cur.execute(
                """
                SELECT metadata ->> 'provider_contract_id'
                FROM futures_contracts
                WHERE provider = %s
                  AND instrument_id = %s
                  AND is_current = true
                FOR UPDATE
                """,
                (provider, instrument_id),
            )
            current_row = cur.fetchone()
            previous_provider_contract_id = (
                require_exact_identity_text(
                    current_row[0],
                    field="current_provider_contract_id",
                )
                if current_row is not None
                else None
            )
            if (
                previous_provider_contract_id is not None
                and exact_route_transition is not None
                and exact_route_transition.previous_session_contract_id
                != previous_provider_contract_id
            ):
                raise RuntimeError("FUTURES_ROUTE_TRANSITION_PREVIOUS_CONTRACT_MISMATCH")
            if (
                previous_provider_contract_id is not None
                and previous_provider_contract_id != provider_contract_id
            ):
                if exact_route_transition is None:
                    raise RuntimeError("FUTURES_CURRENT_ROUTE_REBIND_REQUIRED")
                if (
                    exact_route_transition.previous_route_fingerprint
                    == exact_route_transition.next_route_fingerprint
                ):
                    raise RuntimeError("FUTURES_ROUTE_TRANSITION_FINGERPRINT_UNCHANGED")
                _rebind_futures_root_drawings_on_cursor(
                    cur,
                    provider=provider,
                    instrument_id=instrument_id,
                    previous_route_fingerprint=(exact_route_transition.previous_route_fingerprint),
                    next_route_fingerprint=exact_route_transition.next_route_fingerprint,
                    previous_provider_contract_id=previous_provider_contract_id,
                    next_provider_contract_id=provider_contract_id,
                    lifecycle_lock_held=True,
                )
            elif (
                previous_provider_contract_id == provider_contract_id
                and exact_route_transition is not None
                and exact_route_transition.previous_route_fingerprint
                != exact_route_transition.next_route_fingerprint
            ):
                raise RuntimeError("FUTURES_ROUTE_TRANSITION_SAME_CONTRACT_ROUTE_MISMATCH")
            cur.execute(
                """
                UPDATE futures_contracts
                SET is_current = false, current_resolved_at = NULL, updated_at = now()
                WHERE provider = %s AND instrument_id = %s AND is_current = true
                """,
                (provider, instrument_id),
            )
        cur.execute(
            """
            INSERT INTO futures_contracts (
                provider, instrument_id, contract_key, root, exchange, currency,
                local_symbol, con_id, expiry, contract_month, first_notice_date,
                last_trade_date, is_current, current_resolved_at, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s THEN now() ELSE NULL END, %s)
            ON CONFLICT (provider, instrument_id, contract_key) DO UPDATE SET
                root = EXCLUDED.root,
                exchange = EXCLUDED.exchange,
                currency = EXCLUDED.currency,
                local_symbol = EXCLUDED.local_symbol,
                con_id = EXCLUDED.con_id,
                expiry = EXCLUDED.expiry,
                contract_month = EXCLUDED.contract_month,
                first_notice_date = EXCLUDED.first_notice_date,
                last_trade_date = EXCLUDED.last_trade_date,
                is_current = futures_contracts.is_current OR EXCLUDED.is_current,
                current_resolved_at = CASE
                    WHEN EXCLUDED.is_current THEN EXCLUDED.current_resolved_at
                    ELSE futures_contracts.current_resolved_at
                END,
                metadata = futures_contracts.metadata || EXCLUDED.metadata,
                updated_at = now()
            """,
            row,
        )
        return key

    def write_futures_contract(
        self,
        contract: Mapping[str, Any],
        *,
        route_transition: FuturesRouteTransition | None = None,
    ) -> str:
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                return self._write_futures_contract_on_cursor(
                    cur,
                    contract,
                    route_transition=route_transition,
                )

    def write_futures_roll_event(self, event: Mapping[str, Any]) -> None:
        self._require_canonical_writer_lease()
        self._psycopg()
        from psycopg.types.json import Jsonb

        provider = require_exact_identity_text(
            event.get("provider"),
            field="provider",
        )
        if provider_key_for_source(provider) != provider:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider}")
        instrument_id = require_exact_identity_text(
            event.get("instrument_id"),
            field="instrument_id",
        )
        roll_ts = event.get("roll_ts")
        to_contract_key = require_exact_identity_text(
            event.get("to_contract_key"),
            field="to_contract_key",
        )
        if not provider or not instrument_id or roll_ts is None or not to_contract_key:
            raise ValueError("provider, instrument_id, roll_ts, to_contract_key are required")
        roll_policy = _clean_text(event.get("roll_policy"))
        if not roll_policy:
            raise ValueError("roll_policy is required provider routing metadata")
        raw_from_contract_key = event.get("from_contract_key")
        from_contract_key = (
            ""
            if raw_from_contract_key is None
            else require_exact_identity_text(
                raw_from_contract_key,
                field="from_contract_key",
                allow_empty=True,
            )
        )
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    INSERT INTO futures_roll_events (
                        provider, instrument_id, roll_ts, from_contract_key, to_contract_key,
                        roll_policy, reason, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (provider, instrument_id, roll_ts, roll_policy) DO UPDATE SET
                        from_contract_key = EXCLUDED.from_contract_key,
                        to_contract_key = EXCLUDED.to_contract_key,
                        reason = EXCLUDED.reason,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    (
                        provider,
                        instrument_id,
                        ensure_utc(roll_ts),
                        from_contract_key,
                        to_contract_key,
                        roll_policy,
                        _clean_text(event.get("reason")),
                        Jsonb(dict(event.get("metadata") or {})),
                    ),
                )

    def _write_futures_contract_bars_on_cursor(
        self,
        cur: Any,
        bars: Sequence[Bar],
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        contract_key: str,
        provider_contract_id: str,
        provider_contract_type: str,
        data_type: str,
        revision_sequence: int,
    ) -> CanonicalBarCommitReceipt:
        if (
            isinstance(revision_sequence, bool)
            or not isinstance(revision_sequence, int)
            or revision_sequence <= 0
        ):
            raise ValueError("revision_sequence must be positive")
        commit_sequence = revision_sequence
        for bar in bars:
            require_persisted_bar_timeframe(bar.timeframe)
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        contract = require_exact_identity_text(contract_key, field="contract_key")
        exact_provider_contract_id = require_exact_identity_text(
            provider_contract_id,
            field="provider_contract_id",
        )
        exact_provider_contract_type = require_exact_identity_text(
            provider_contract_type,
            field="provider_contract_type",
        )
        if not isinstance(data_type, str) or not data_type or data_type != data_type.strip():
            raise ValueError("data_type must be an exact non-empty string")
        if not provider_key or not stable_id or not fingerprint or not contract:
            raise ValueError(
                "provider, instrument_id, route_fingerprint, contract_key are required"
            )
        allowed_bars: list[Bar] = []
        rejected_bars: list[Bar] = []
        for bar in bars:
            if _storage_bar_allowed(
                provider_key,
                bar,
                instrument_id=stable_id,
                expected_route_fingerprint=fingerprint,
                expected_provider_contract_id=exact_provider_contract_id,
                expected_provider_contract_type=exact_provider_contract_type,
                expected_data_type=data_type,
            ):
                allowed_bars.append(bar)
            else:
                rejected_bars.append(bar)
        if not allowed_bars:
            return CanonicalBarCommitReceipt(
                revision_sequence=commit_sequence,
                rejected=tuple(bars),
            )
        slots = {
            (require_persisted_bar_timeframe(bar.timeframe), ensure_utc(bar.ts))
            for bar in allowed_bars
        }
        if len(slots) != len(allowed_bars):
            raise ValueError(
                "write_futures_contract_bars requires unique timeframe/timestamp slots"
            )
        ordered_bars = sorted(
            allowed_bars,
            key=lambda bar: (
                require_persisted_bar_timeframe(bar.timeframe),
                ensure_utc(bar.ts),
            ),
        )
        rows = [
            (
                provider_key,
                stable_id,
                fingerprint,
                contract,
                require_persisted_bar_timeframe(bar.timeframe),
                ensure_utc(bar.ts),
                float(bar.open),
                float(bar.high),
                float(bar.low),
                float(bar.close),
                float(bar.volume),
                _clean_text(bar.source) or provider_key,
                bool(bar.closed),
                commit_sequence,
            )
            for bar in ordered_bars
        ]
        written_rows, persisted_rows = _write_futures_contract_rows(
            cur,
            rows,
        )
        provenance = canonical_storage_bar_provenance(provider_key, stable_id, fingerprint)
        persisted = [
            (
                Bar(
                    symbol=ordered_bars[0].symbol,
                    ts=ensure_utc(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    timeframe=row[6],
                    source=row[7],
                    closed=bool(row[8]),
                    provenance=provenance,
                ),
                int(row[9]),
            )
            for row in persisted_rows
        ]
        return canonical_bar_commit_receipt(
            allowed_bars,
            persisted,
            revision_sequence=commit_sequence,
            written=written_rows,
            rejected=rejected_bars,
            timeframe_normalizer=require_persisted_bar_timeframe,
        )

    def _finalize_futures_contract_bars_commit(
        self,
        receipt: CanonicalBarCommitReceipt,
    ) -> None:
        if receipt.written > 0 or receipt.deleted > 0:
            try:
                self._invalidate_bar_slots_cache()
            except Exception:
                _LOGGER.exception("storage_futures_bar_cache_invalidation_failed")

    def write_futures_contract_bars(
        self,
        bars: Sequence[Bar],
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        contract_key: str,
        provider_contract_id: str,
        provider_contract_type: str,
        data_type: str,
        revision_sequence: int | None = None,
    ) -> CanonicalBarCommitReceipt:
        self._require_canonical_writer_lease()
        for bar in bars:
            require_persisted_bar_timeframe(bar.timeframe)
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if revision_sequence is None:
                    cur.execute("SELECT nextval('canonical_bar_revision_sequence')")
                    sequence_row = cur.fetchone()
                    commit_sequence = int(sequence_row[0]) if sequence_row else 0
                else:
                    commit_sequence = revision_sequence
                receipt = self._write_futures_contract_bars_on_cursor(
                    cur,
                    bars,
                    provider=provider,
                    instrument_id=instrument_id,
                    route_fingerprint=route_fingerprint,
                    contract_key=contract_key,
                    provider_contract_id=provider_contract_id,
                    provider_contract_type=provider_contract_type,
                    data_type=data_type,
                    revision_sequence=commit_sequence,
                )
        self._finalize_futures_contract_bars_commit(receipt)
        return receipt

    def read_futures_contract_bars_multi(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        symbol: str,
        contract_key: str,
        timeframes: Sequence[str],
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        latest: bool = False,
        timeframe_read_windows: Mapping[
            str,
            tuple[datetime | None, datetime | None, int | None],
        ]
        | None = None,
    ) -> dict[str, list[Bar]]:
        display_symbol = _clean_text(symbol)
        if not display_symbol:
            raise ValueError("symbol is required for Bar display metadata")
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        contract = require_exact_identity_text(contract_key, field="contract_key")
        timeframe_keys = tuple(
            dict.fromkeys(require_persisted_bar_timeframe(item) for item in timeframes)
        )
        if (
            not provider_key
            or not stable_id
            or not fingerprint
            or not contract
            or not timeframe_keys
        ):
            raise ValueError(
                "provider, instrument_id, route_fingerprint, contract_key, and at least one timeframe are required"
            )
        normalized_read_windows = {
            require_persisted_bar_timeframe(timeframe): (
                ensure_utc(window_start) if window_start is not None else None,
                ensure_utc(window_end) if window_end is not None else None,
                max(int(row_limit), 1) if row_limit is not None else None,
            )
            for timeframe, (
                window_start,
                window_end,
                row_limit,
            ) in (timeframe_read_windows or {}).items()
        }
        if normalized_read_windows:
            missing_windows = set(timeframe_keys) - set(normalized_read_windows)
            if missing_windows:
                raise ValueError("timeframe_read_windows must cover every requested timeframe")
            values_sql = ", ".join(
                "(%s::text, %s::timestamptz, %s::timestamptz, %s::integer)"
                for _timeframe in timeframe_keys
            )
            params: list[Any] = []
            for timeframe in timeframe_keys:
                window_start, window_end, row_limit = normalized_read_windows[timeframe]
                params.extend((timeframe, window_start, window_end, row_limit))
            params.extend((provider_key, stable_id, fingerprint, contract))
            sql = f"""
                WITH requested(timeframe, start_inclusive, end_exclusive, row_limit) AS (
                    VALUES {values_sql}
                )
                SELECT selected.ts, selected.open, selected.high, selected.low,
                       selected.close, selected.volume, selected.timeframe,
                       selected.source, selected.closed
                FROM requested
                CROSS JOIN LATERAL (
                    SELECT bars.ts, bars.open, bars.high, bars.low, bars.close,
                           bars.volume, bars.timeframe, bars.source, bars.closed
                    FROM futures_contract_bars AS bars
                    WHERE bars.provider = %s
                      AND bars.instrument_id = %s
                      AND bars.route_fingerprint = %s
                      AND bars.contract_key = %s
                      AND bars.timeframe = requested.timeframe
                      AND (requested.start_inclusive IS NULL OR bars.ts >= requested.start_inclusive)
                      AND (requested.end_exclusive IS NULL OR bars.ts < requested.end_exclusive)
                    ORDER BY bars.ts DESC
                    LIMIT requested.row_limit
                ) AS selected
                ORDER BY selected.timeframe ASC, selected.ts ASC
            """
        elif limit is not None:
            values_sql = ", ".join("(%s::text)" for _timeframe in timeframe_keys)
            conditions = [
                "bars.provider = %s",
                "bars.instrument_id = %s",
                "bars.route_fingerprint = %s",
                "bars.contract_key = %s",
                "bars.timeframe = requested.timeframe",
            ]
            params = [*timeframe_keys, provider_key, stable_id, fingerprint, contract]
            if start is not None:
                conditions.append("bars.ts >= %s")
                params.append(ensure_utc(start))
            if end is not None:
                conditions.append("bars.ts <= %s")
                params.append(ensure_utc(end))
            params.append(max(int(limit), 1))
            ranked_order = "DESC" if latest else "ASC"
            sql = f"""
                WITH requested(timeframe) AS (VALUES {values_sql})
                SELECT selected.ts, selected.open, selected.high, selected.low,
                       selected.close, selected.volume, selected.timeframe,
                       selected.source, selected.closed
                FROM requested
                CROSS JOIN LATERAL (
                    SELECT bars.ts, bars.open, bars.high, bars.low, bars.close,
                           bars.volume, bars.timeframe, bars.source, bars.closed
                    FROM futures_contract_bars AS bars
                    WHERE {" AND ".join(conditions)}
                    ORDER BY bars.ts {ranked_order}
                    LIMIT %s
                ) AS selected
                ORDER BY selected.timeframe ASC, selected.ts ASC
            """
        else:
            conditions = [
                "provider = %s",
                "instrument_id = %s",
                "route_fingerprint = %s",
                "contract_key = %s",
                f"timeframe IN ({', '.join('%s' for _timeframe in timeframe_keys)})",
            ]
            params = [provider_key, stable_id, fingerprint, contract, *timeframe_keys]
            if start is not None:
                conditions.append("ts >= %s")
                params.append(ensure_utc(start))
            if end is not None:
                conditions.append("ts <= %s")
                params.append(ensure_utc(end))
            sql = (
                "SELECT ts, open, high, low, close, volume, timeframe, source, closed "
                f"FROM futures_contract_bars WHERE {' AND '.join(conditions)} "
                "ORDER BY timeframe ASC, ts ASC"
            )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        grouped: dict[str, list[Bar]] = {timeframe: [] for timeframe in timeframe_keys}
        provenance = canonical_storage_bar_provenance(provider_key, stable_id, fingerprint)
        for row in rows:
            grouped[_clean_text(row[6])].append(
                Bar(
                    symbol=display_symbol,
                    ts=ensure_utc(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    timeframe=row[6],
                    source=row[7],
                    closed=bool(row[8]),
                    provenance=provenance,
                )
            )
        return grouped

    def read_futures_contract_bars(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        symbol: str,
        contract_key: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        latest: bool = False,
    ) -> list[Bar]:
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        return self.read_futures_contract_bars_multi(
            provider=provider,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            symbol=symbol,
            contract_key=contract_key,
            timeframes=(timeframe_key,),
            start=start,
            end=end,
            limit=limit,
            latest=latest,
        )[timeframe_key]

    def read_latest_futures_contract_bar_boundaries(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str,
        limit: int = 8,
    ) -> list[tuple[str, datetime]]:
        """Return one latest timestamp per exact physical contract."""

        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ValueError("limit must be an integer from 1 to 64")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (contract_key) contract_key, ts
                    FROM futures_contract_bars
                    WHERE provider = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                    ORDER BY contract_key, ts DESC
                    LIMIT %s
                    """,
                    (
                        provider_key,
                        stable_id,
                        fingerprint,
                        timeframe_key,
                        limit,
                    ),
                )
                return [(str(row[0]), ensure_utc(row[1])) for row in cur.fetchall()]

    def read_futures_contract_logical_projection(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        contract_key: str,
        timeframe: str,
        anchor_timestamps: Sequence[datetime],
        derived_timeframe: str | None = None,
    ) -> ConfirmedBarLogicalProjection:
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        contract = require_exact_identity_text(contract_key, field="contract_key")
        return read_confirmed_bar_logical_projection(
            self,
            table="futures_contract_bars",
            route_conditions=(
                "provider = %s",
                "instrument_id = %s",
                "route_fingerprint = %s",
                "contract_key = %s",
            ),
            route_params=(provider_key, stable_id, fingerprint, contract),
            timeframe=require_persisted_bar_timeframe(timeframe),
            anchor_timestamps=anchor_timestamps,
            derived_timeframe=derived_timeframe,
        )

    def read_futures_contract_confirmed_timestamps(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        contract_key: str,
        timeframe: str,
        anchor_timestamps: Sequence[datetime],
        derived_timeframe: str | None = None,
    ) -> frozenset[datetime]:
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        contract = require_exact_identity_text(contract_key, field="contract_key")
        return read_confirmed_bar_timestamps(
            self,
            table="futures_contract_bars",
            route_conditions=(
                "provider = %s",
                "instrument_id = %s",
                "route_fingerprint = %s",
                "contract_key = %s",
            ),
            route_params=(provider_key, stable_id, fingerprint, contract),
            timeframe=require_persisted_bar_timeframe(timeframe),
            anchor_timestamps=anchor_timestamps,
            derived_timeframe=derived_timeframe,
        )

    def read_futures_roll_events(
        self,
        *,
        provider: str,
        instrument_id: str,
        roll_policy: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["provider = %s", "instrument_id = %s"]
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        if not provider_key or not stable_id:
            raise ValueError("provider and instrument_id are required")
        params: list[Any] = [provider_key, stable_id]
        policy = _clean_text(roll_policy)
        if policy:
            conditions.append("roll_policy = %s")
            params.append(policy)
        if start is not None:
            conditions.append("roll_ts >= %s")
            params.append(ensure_utc(start))
        if end is not None:
            conditions.append("roll_ts <= %s")
            params.append(ensure_utc(end))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT roll_ts, from_contract_key, to_contract_key, roll_policy, reason, metadata
                    FROM futures_roll_events
                    WHERE """
                    + " AND ".join(conditions)
                    + """
                    ORDER BY roll_ts ASC
                    """,
                    params,
                )
                rows = cur.fetchall()
        return [
            {
                "roll_ts": ensure_utc(row[0]),
                "from_contract_key": row[1],
                "to_contract_key": row[2],
                "roll_policy": row[3],
                "reason": row[4],
                "metadata": row[5] or {},
            }
            for row in rows
        ]

    def write_futures_canonical_bars(
        self,
        bars: Sequence[Bar],
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        series_type: str = "",
        roll_policy: str = "",
        metadata: Mapping[str, Any] | None = None,
        revision_sequence: int | None = None,
    ) -> CanonicalBarCommitReceipt:
        self._require_canonical_writer_lease()
        self._psycopg()

        for bar in bars:
            require_persisted_bar_timeframe(bar.timeframe)

        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        series, policy = _require_canonical_route(series_type, roll_policy)
        if not provider_key or not stable_id or not fingerprint:
            raise ValueError("provider, instrument_id, and route_fingerprint are required")
        (
            admission_metadata,
            expected_provider_contract_id,
            expected_provider_contract_type,
            expected_data_type,
        ) = _canonical_futures_bar_admission_metadata(metadata)
        allowed_bars: list[Bar] = []
        rejected_bars: list[Bar] = []
        for bar in bars:
            if _storage_bar_allowed(
                provider_key,
                bar,
                instrument_id=stable_id,
                expected_route_fingerprint=fingerprint,
                expected_provider_contract_id=expected_provider_contract_id,
                expected_provider_contract_type=expected_provider_contract_type,
                expected_data_type=expected_data_type,
            ):
                allowed_bars.append(bar)
            else:
                rejected_bars.append(bar)
        if not allowed_bars:
            rejected_sequence = (
                int(revision_sequence)
                if revision_sequence is not None
                else self.reserve_bar_revision_sequence()
            )
            return CanonicalBarCommitReceipt(
                revision_sequence=rejected_sequence,
                rejected=tuple(bars),
            )
        slots = {
            (require_persisted_bar_timeframe(bar.timeframe), ensure_utc(bar.ts))
            for bar in allowed_bars
        }
        if len(slots) != len(allowed_bars):
            raise ValueError(
                "write_futures_canonical_bars requires unique timeframe/timestamp slots"
            )
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if revision_sequence is None:
                    cur.execute("SELECT nextval('canonical_bar_revision_sequence')")
                    sequence_row = cur.fetchone()
                    commit_sequence = int(sequence_row[0]) if sequence_row else 0
                else:
                    commit_sequence = int(revision_sequence)
                if commit_sequence <= 0:
                    raise ValueError("revision_sequence must be positive")
                ordered_bars = sorted(
                    allowed_bars,
                    key=lambda bar: (
                        require_persisted_bar_timeframe(bar.timeframe),
                        ensure_utc(bar.ts),
                    ),
                )
                rows = [
                    (
                        provider_key,
                        stable_id,
                        series,
                        policy,
                        require_persisted_bar_timeframe(bar.timeframe),
                        ensure_utc(bar.ts),
                        float(bar.open),
                        float(bar.high),
                        float(bar.low),
                        float(bar.close),
                        float(bar.volume),
                        _clean_text(bar.source) or provider_key,
                        bool(bar.closed),
                        admission_metadata,
                        commit_sequence,
                    )
                    for bar in ordered_bars
                ]
                written_rows, persisted_rows = _write_futures_canonical_rows(
                    cur,
                    rows,
                )
                provenance = canonical_storage_bar_provenance(provider_key, stable_id, fingerprint)
                persisted = [
                    (
                        Bar(
                            symbol=allowed_bars[0].symbol,
                            ts=ensure_utc(row[0]),
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            timeframe=row[6],
                            source=row[7],
                            closed=bool(row[8]),
                            provenance=provenance,
                        ),
                        int(row[9]),
                    )
                    for row in persisted_rows
                ]
                receipt = canonical_bar_commit_receipt(
                    allowed_bars,
                    persisted,
                    revision_sequence=commit_sequence,
                    written=written_rows,
                    rejected=rejected_bars,
                    timeframe_normalizer=require_persisted_bar_timeframe,
                )
        if written_rows > 0:
            self._invalidate_bar_slots_cache()
        return receipt

    def read_futures_canonical_bars_multi(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        symbol: str,
        timeframes: Sequence[str],
        series_type: str = "",
        roll_policy: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        latest: bool = False,
        timeframe_read_windows: Mapping[
            str,
            tuple[datetime | None, datetime | None, int | None],
        ]
        | None = None,
    ) -> dict[str, list[Bar]]:
        display_symbol = _clean_text(symbol)
        if not display_symbol:
            raise ValueError("symbol is required for Bar display metadata")
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        series, policy = _require_canonical_route(series_type, roll_policy)
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        )
        timeframe_keys = tuple(
            dict.fromkeys(require_persisted_bar_timeframe(item) for item in timeframes)
        )
        if not provider_key or not stable_id or not fingerprint or not timeframe_keys:
            raise ValueError(
                "provider, instrument_id, route_fingerprint, and at least one timeframe are required"
            )
        normalized_read_windows = {
            require_persisted_bar_timeframe(timeframe): (
                ensure_utc(window_start) if window_start is not None else None,
                ensure_utc(window_end) if window_end is not None else None,
                (max(int(row_limit), 1) if row_limit is not None else None),
            )
            for timeframe, (
                window_start,
                window_end,
                row_limit,
            ) in (timeframe_read_windows or {}).items()
        }
        if normalized_read_windows:
            missing_windows = set(timeframe_keys) - set(normalized_read_windows)
            if missing_windows:
                raise ValueError("timeframe_read_windows must cover every requested timeframe")
            values_sql = ", ".join(
                "(%s::text, %s::timestamptz, %s::timestamptz, %s::integer)"
                for _timeframe in timeframe_keys
            )
            params: list[Any] = []
            for timeframe in timeframe_keys:
                window_start, window_end, row_limit = normalized_read_windows[timeframe]
                params.extend((timeframe, window_start, window_end, row_limit))
            params.extend((provider_key, stable_id, series, policy))
            sql = f"""
                WITH requested(
                    timeframe,
                    start_inclusive,
                    end_exclusive,
                    row_limit
                ) AS (
                    VALUES {values_sql}
                )
                SELECT
                    selected.ts,
                    selected.open,
                    selected.high,
                    selected.low,
                    selected.close,
                    selected.volume,
                    selected.timeframe,
                    selected.source,
                    selected.closed
                FROM requested
                CROSS JOIN LATERAL (
                    SELECT
                        bars.ts,
                        bars.open,
                        bars.high,
                        bars.low,
                        bars.close,
                        bars.volume,
                        bars.timeframe,
                        bars.source,
                        bars.closed
                    FROM futures_continuous_bars
                    AS bars
                    WHERE bars.provider = %s
                      AND bars.instrument_id = %s
                      AND bars.series_type = %s
                      AND bars.roll_policy = %s
                      AND bars.timeframe = requested.timeframe
                      AND (
                          requested.start_inclusive IS NULL
                          OR bars.ts >= requested.start_inclusive
                      )
                      AND (
                          requested.end_exclusive IS NULL
                          OR bars.ts < requested.end_exclusive
                      )
                    ORDER BY bars.ts DESC
                    LIMIT requested.row_limit
                ) AS selected
                ORDER BY selected.timeframe ASC, selected.ts ASC
            """
        else:
            ranked_order = "DESC" if latest else "ASC"
            if limit is not None:
                values_sql = ", ".join("(%s::text)" for _ in timeframe_keys)
                lateral_conditions = [
                    "bars.provider = %s",
                    "bars.instrument_id = %s",
                    "bars.series_type = %s",
                    "bars.roll_policy = %s",
                    "bars.timeframe = requested.timeframe",
                ]
                params = [
                    *timeframe_keys,
                    provider_key,
                    stable_id,
                    series,
                    policy,
                ]
                if start is not None:
                    lateral_conditions.append("bars.ts >= %s")
                    params.append(ensure_utc(start))
                if end is not None:
                    lateral_conditions.append("bars.ts <= %s")
                    params.append(ensure_utc(end))
                params.append(max(int(limit), 1))
                sql = f"""
                    WITH requested(timeframe) AS (
                        VALUES {values_sql}
                    )
                    SELECT
                        selected.ts,
                        selected.open,
                        selected.high,
                        selected.low,
                        selected.close,
                        selected.volume,
                        selected.timeframe,
                        selected.source,
                        selected.closed
                    FROM requested
                    CROSS JOIN LATERAL (
                        SELECT
                            bars.ts,
                            bars.open,
                            bars.high,
                            bars.low,
                            bars.close,
                            bars.volume,
                            bars.timeframe,
                            bars.source,
                            bars.closed
                        FROM futures_continuous_bars AS bars
                        WHERE {" AND ".join(lateral_conditions)}
                        ORDER BY bars.ts {ranked_order}
                        LIMIT %s
                    ) AS selected
                    ORDER BY selected.timeframe ASC, selected.ts ASC
                """
            else:
                conditions = [
                    "provider = %s",
                    "instrument_id = %s",
                    "series_type = %s",
                    "roll_policy = %s",
                    f"timeframe IN ({', '.join('%s' for _ in timeframe_keys)})",
                ]
                params = [
                    provider_key,
                    stable_id,
                    series,
                    policy,
                    *timeframe_keys,
                ]
                if start is not None:
                    conditions.append("ts >= %s")
                    params.append(ensure_utc(start))
                if end is not None:
                    conditions.append("ts <= %s")
                    params.append(ensure_utc(end))
                sql = (
                    "SELECT ts, open, high, low, close, volume, timeframe, source, closed "
                    f"FROM futures_continuous_bars WHERE {' AND '.join(conditions)} "
                    "ORDER BY timeframe ASC, ts ASC"
                )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        grouped: dict[str, list[Bar]] = {timeframe: [] for timeframe in timeframe_keys}
        provenance = canonical_storage_bar_provenance(provider_key, stable_id, fingerprint)
        for row in rows:
            grouped[_clean_text(row[6])].append(
                Bar(
                    symbol=display_symbol,
                    ts=ensure_utc(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    timeframe=row[6],
                    source=row[7],
                    closed=bool(row[8]),
                    provenance=provenance,
                )
            )
        return grouped

    def read_futures_canonical_bars(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        symbol: str,
        timeframe: str,
        series_type: str = "",
        roll_policy: str = "",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        latest: bool = False,
    ) -> list[Bar]:
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        return self.read_futures_canonical_bars_multi(
            provider=provider,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            symbol=symbol,
            timeframes=(timeframe_key,),
            series_type=series_type,
            roll_policy=roll_policy,
            start=start,
            end=end,
            limit=limit,
            latest=latest,
        )[timeframe_key]

    def read_futures_canonical_logical_projection(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        series_type: str,
        roll_policy: str,
        timeframe: str,
        anchor_timestamps: Sequence[datetime],
        derived_timeframe: str | None = None,
    ) -> ConfirmedBarLogicalProjection:
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        require_exact_identity_text(route_fingerprint, field="route_fingerprint")
        series, policy = _require_canonical_route(series_type, roll_policy)
        return read_confirmed_bar_logical_projection(
            self,
            table="futures_continuous_bars",
            route_conditions=(
                "provider = %s",
                "instrument_id = %s",
                "series_type = %s",
                "roll_policy = %s",
            ),
            route_params=(provider_key, stable_id, series, policy),
            timeframe=require_persisted_bar_timeframe(timeframe),
            anchor_timestamps=anchor_timestamps,
            derived_timeframe=derived_timeframe,
        )

    def read_futures_canonical_confirmed_timestamps(
        self,
        *,
        provider: str,
        instrument_id: str,
        route_fingerprint: str,
        series_type: str,
        roll_policy: str,
        timeframe: str,
        anchor_timestamps: Sequence[datetime],
        derived_timeframe: str | None = None,
    ) -> frozenset[datetime]:
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        require_exact_identity_text(route_fingerprint, field="route_fingerprint")
        series, policy = _require_canonical_route(series_type, roll_policy)
        return read_confirmed_bar_timestamps(
            self,
            table="futures_continuous_bars",
            route_conditions=(
                "provider = %s",
                "instrument_id = %s",
                "series_type = %s",
                "roll_policy = %s",
            ),
            route_params=(provider_key, stable_id, series, policy),
            timeframe=require_persisted_bar_timeframe(timeframe),
            anchor_timestamps=anchor_timestamps,
            derived_timeframe=derived_timeframe,
        )

    def read_current_futures_contract(
        self,
        *,
        provider: str,
        instrument_id: str,
    ) -> dict[str, Any] | None:
        provider_key = require_exact_identity_text(provider, field="provider")
        if provider_key_for_source(provider_key) != provider_key:
            raise ValueError(f"FUTURES_PROVIDER_NOT_CANONICAL provider={provider_key}")
        stable_id = require_exact_identity_text(instrument_id, field="instrument_id")
        if not provider_key or not stable_id:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        provider, instrument_id, contract_key, root, exchange, currency,
                        local_symbol, con_id, expiry, contract_month, first_notice_date,
                        last_trade_date, current_resolved_at, metadata
                    FROM futures_contracts
                    WHERE provider = %s
                      AND instrument_id = %s
                      AND is_current = true
                    ORDER BY current_resolved_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (provider_key, stable_id),
                )
                row = cur.fetchone()
        if not row:
            return None
        return _futures_contract_row_payload(row, source="provider_current_contract")
