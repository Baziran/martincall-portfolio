from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.storage.db_utils import ensure_utc, utc_now
from aef_terminal.storage.session_intervals import (
    normalize_trading_session_intervals,
    trading_session_interval_replacement_horizons,
)


class TradingHoursRepoMixin:
    """Provider-owned trading schedule and normalized session persistence."""

    def delete_trading_hours(
        self,
        *,
        instrument: dict[str, Any],
    ) -> bool:
        route = route_instrument(instrument)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{route.fingerprint}:trading-hours",),
                )
                cur.execute(
                    "DELETE FROM trading_session_intervals WHERE instrument_id = %s AND route_fingerprint = %s",
                    (route.instrument_id, route.fingerprint),
                )
                cur.execute(
                    "DELETE FROM trading_schedule_snapshots WHERE instrument_id = %s AND route_fingerprint = %s",
                    (route.instrument_id, route.fingerprint),
                )
                cur.execute(
                    "DELETE FROM trading_hours WHERE instrument_id = %s AND route_fingerprint = %s",
                    (route.instrument_id, route.fingerprint),
                )
                deleted = bool(cur.rowcount)
        self._invalidate_bar_slots_cache()
        return deleted

    def read_trading_hours(
        self,
        *,
        instrument: dict[str, Any],
    ) -> dict[str, Any] | None:
        route = route_instrument(instrument)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT instrument_id, route_fingerprint, provider, symbol, timezone,
                           trading_hours, liquid_hours, payload, fetched_at, updated_at
                    FROM trading_hours
                    WHERE instrument_id = %s AND route_fingerprint = %s
                    """,
                    (route.instrument_id, route.fingerprint),
                )
                row = cur.fetchone()
        if not row:
            return None
        fetched_at = ensure_utc(row[8])
        updated_at = ensure_utc(row[9])
        return {
            "instrument_id": require_exact_identity_text(row[0], field="instrument_id"),
            "route_fingerprint": require_exact_identity_text(
                row[1],
                field="route_fingerprint",
            ),
            "provider": str(row[2] or ""),
            "symbol": str(row[3] or ""),
            "timezone": str(row[4] or ""),
            "trading_hours": str(row[5] or ""),
            "liquid_hours": str(row[6] or ""),
            "payload": row[7] or {},
            "fetched_at": fetched_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "age_seconds": max((utc_now() - fetched_at).total_seconds(), 0.0),
        }

    def read_trading_session_intervals(
        self,
        *,
        instrument: dict[str, Any],
        session_type: str = "trading",
        start_ts: datetime | None = None,
        end_ts: datetime | None = None,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        route = route_instrument(instrument)
        session_key = str(session_type or "").strip().lower()
        if not session_key:
            return []
        if (start_ts is None) != (end_ts is None):
            raise ValueError("start_ts and end_ts must be provided together")
        range_start = ensure_utc(start_ts) if start_ts is not None else None
        range_end = ensure_utc(end_ts) if end_ts is not None else None
        if range_start is not None and range_end is not None and range_start >= range_end:
            return []
        row_limit = max(1, min(int(limit), 4096))
        with self._connect() as conn:
            with conn.cursor() as cur:
                if range_start is not None and range_end is not None:
                    cur.execute(
                        """
                        WITH scoped AS MATERIALIZED (
                            SELECT session.*,
                                   min(session.opens_at) OVER (
                                       PARTITION BY session.source_fetched_at
                                   ) AS materialized_coverage_start,
                                   max(session.closes_at) OVER (
                                       PARTITION BY session.source_fetched_at
                                   ) AS materialized_coverage_end
                            FROM trading_session_intervals AS session
                            WHERE session.instrument_id = %s
                              AND session.route_fingerprint = %s
                              AND session.session_type = %s
                        )
                        SELECT scoped.instrument_id, scoped.route_fingerprint,
                               scoped.session_date, scoped.opens_at, scoped.closes_at,
                               scoped.status, scoped.source_fetched_at, scoped.metadata,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN scoped.materialized_coverage_start
                               END AS source_coverage_start,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN scoped.materialized_coverage_end
                               END AS source_coverage_end,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN snapshot.raw_payload ->> 'schedule_coverage_start'
                               END AS snapshot_coverage_start,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN snapshot.raw_payload ->> 'schedule_coverage_end'
                               END AS snapshot_coverage_end,
                               hours.timezone
                        FROM scoped
                        LEFT JOIN trading_schedule_snapshots AS snapshot
                          ON snapshot.instrument_id = scoped.instrument_id
                         AND snapshot.route_fingerprint = scoped.route_fingerprint
                         AND snapshot.fetched_at = scoped.source_fetched_at
                        LEFT JOIN trading_hours AS hours
                          ON hours.instrument_id = scoped.instrument_id
                         AND hours.route_fingerprint = scoped.route_fingerprint
                        WHERE scoped.opens_at < %s
                          AND scoped.closes_at > %s
                        ORDER BY scoped.opens_at ASC, scoped.closes_at ASC
                        LIMIT %s
                        """,
                        (
                            route.instrument_id,
                            route.fingerprint,
                            session_key,
                            range_end,
                            range_start,
                            row_limit,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        WITH scoped AS MATERIALIZED (
                            SELECT session.*,
                                   min(session.opens_at) OVER (
                                       PARTITION BY session.source_fetched_at
                                   ) AS materialized_coverage_start,
                                   max(session.closes_at) OVER (
                                       PARTITION BY session.source_fetched_at
                                   ) AS materialized_coverage_end
                            FROM trading_session_intervals AS session
                            WHERE session.instrument_id = %s
                              AND session.route_fingerprint = %s
                              AND session.session_type = %s
                        )
                        SELECT scoped.instrument_id, scoped.route_fingerprint,
                               scoped.session_date, scoped.opens_at, scoped.closes_at,
                               scoped.status, scoped.source_fetched_at, scoped.metadata,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN scoped.materialized_coverage_start
                               END AS source_coverage_start,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN scoped.materialized_coverage_end
                               END AS source_coverage_end,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN snapshot.raw_payload ->> 'schedule_coverage_start'
                               END AS snapshot_coverage_start,
                               CASE
                                   WHEN snapshot.raw_payload ->> 'schedule_format'
                                          = 'provider_declared_intervals'
                                    AND COALESCE(
                                        snapshot.raw_payload -> 'trading_intervals',
                                        '[]'::jsonb
                                    ) @> jsonb_build_array(
                                        jsonb_build_object(
                                            'session_type',
                                            scoped.session_type
                                        )
                                    )
                                   THEN snapshot.raw_payload ->> 'schedule_coverage_end'
                               END AS snapshot_coverage_end,
                               hours.timezone
                        FROM scoped
                        LEFT JOIN trading_schedule_snapshots AS snapshot
                          ON snapshot.instrument_id = scoped.instrument_id
                         AND snapshot.route_fingerprint = scoped.route_fingerprint
                         AND snapshot.fetched_at = scoped.source_fetched_at
                        LEFT JOIN trading_hours AS hours
                          ON hours.instrument_id = scoped.instrument_id
                         AND hours.route_fingerprint = scoped.route_fingerprint
                        ORDER BY scoped.opens_at DESC, scoped.closes_at DESC
                        LIMIT %s
                        """,
                        (route.instrument_id, route.fingerprint, session_key, row_limit),
                    )
                rows = cur.fetchall()
        ordered_rows = rows if range_start is not None else reversed(rows)
        result: list[dict[str, Any]] = []
        for row in ordered_rows:
            interval = {
                "instrument_id": require_exact_identity_text(row[0], field="instrument_id"),
                "route_fingerprint": require_exact_identity_text(
                    row[1],
                    field="route_fingerprint",
                ),
                "session_date": row[2].isoformat(),
                "opens_at": ensure_utc(row[3]).isoformat(),
                "closes_at": ensure_utc(row[4]).isoformat(),
                "status": str(row[5] or "unknown"),
                "source_fetched_at": ensure_utc(row[6]).isoformat(),
                "metadata": dict(row[7] or {}),
                "timezone": str(row[12] or ""),
            }
            if row[8] is not None and row[9] is not None:
                source_coverage_start = ensure_utc(row[8])
                source_coverage_end = ensure_utc(row[9])
                try:
                    snapshot_coverage_start = datetime.fromisoformat(
                        str(row[10] or "").replace("Z", "+00:00")
                    )
                    snapshot_coverage_end = datetime.fromisoformat(
                        str(row[11] or "").replace("Z", "+00:00")
                    )
                except TypeError, ValueError:
                    snapshot_coverage_start = None
                    snapshot_coverage_end = None
                if (
                    snapshot_coverage_start is not None
                    and snapshot_coverage_end is not None
                    and snapshot_coverage_start.utcoffset() is not None
                    and snapshot_coverage_end.utcoffset() is not None
                    and snapshot_coverage_end > snapshot_coverage_start
                ):
                    source_coverage_start = min(
                        source_coverage_start,
                        ensure_utc(snapshot_coverage_start),
                    )
                    source_coverage_end = max(
                        source_coverage_end,
                        ensure_utc(snapshot_coverage_end),
                    )
                interval["source_coverage_start"] = source_coverage_start.isoformat()
                interval["source_coverage_end"] = source_coverage_end.isoformat()
            result.append(interval)
        return result

    def read_trading_session_axis_anchor(
        self,
        *,
        instrument: dict[str, Any],
        session_type: str = "trading",
    ) -> datetime | None:
        """Return the stable first retained provider-calendar row for a route."""

        route = route_instrument(instrument)
        session_key = str(session_type or "").strip().lower()
        if session_key not in {"liquid", "trading"}:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT min(opens_at)
                    FROM trading_session_intervals
                    WHERE instrument_id = %s
                      AND route_fingerprint = %s
                      AND session_type = %s
                    """,
                    (route.instrument_id, route.fingerprint, session_key),
                )
                row = cur.fetchone()
        return ensure_utc(row[0]) if row and row[0] is not None else None

    def read_trading_session_open_state(
        self,
        ts: datetime,
        session_type: str,
        *,
        instrument: dict[str, Any],
    ) -> bool | None:
        session_key = str(session_type or "").strip().lower()
        timestamp = ensure_utc(ts)
        if not session_key:
            return None
        rows = self.read_trading_session_intervals(
            instrument=instrument,
            session_type=session_key,
            start_ts=timestamp - timedelta(days=2),
            end_ts=timestamp + timedelta(days=2),
            limit=4096,
        )
        if not rows:
            return None
        parsed_rows: list[tuple[dict[str, Any], datetime, datetime]] = []
        for row in rows:
            try:
                opens_at = datetime.fromisoformat(
                    str(row.get("opens_at") or "").replace("Z", "+00:00")
                )
                closes_at = datetime.fromisoformat(
                    str(row.get("closes_at") or "").replace("Z", "+00:00")
                )
            except TypeError, ValueError:
                continue
            if opens_at.tzinfo is None or closes_at.tzinfo is None or closes_at <= opens_at:
                continue
            parsed_rows.append((row, ensure_utc(opens_at), ensure_utc(closes_at)))
        if any(
            str(row.get("status") or "") == "open" and opens_at <= timestamp < closes_at
            for row, opens_at, closes_at in parsed_rows
        ):
            return True
        if any(
            str(row.get("status") or "") == "closed" and opens_at <= timestamp < closes_at
            for row, opens_at, closes_at in parsed_rows
        ):
            return False
        for row in rows:
            if "source_coverage_start" not in row and "source_coverage_end" not in row:
                continue
            coverage_start = row.get("source_coverage_start")
            coverage_end = row.get("source_coverage_end")
            if coverage_start is None or coverage_end is None:
                continue
            try:
                starts_at = datetime.fromisoformat(str(coverage_start).replace("Z", "+00:00"))
                ends_at = datetime.fromisoformat(str(coverage_end).replace("Z", "+00:00"))
            except TypeError, ValueError:
                continue
            if starts_at.tzinfo is None or ends_at.tzinfo is None:
                continue
            starts_at = ensure_utc(starts_at)
            ends_at = ensure_utc(ends_at)
            if starts_at <= timestamp < ends_at:
                return False
        return None

    def read_next_trading_session_open(
        self,
        ts: datetime,
        session_type: str,
        *,
        instrument: dict[str, Any],
    ) -> datetime | None:
        route = route_instrument(instrument)
        session_key = str(session_type or "").strip().lower()
        timestamp = ensure_utc(ts)
        if not session_key:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT opens_at
                    FROM trading_session_intervals
                    WHERE instrument_id = %s
                      AND route_fingerprint = %s
                      AND session_type = %s
                      AND status = 'open'
                      AND opens_at > %s
                    ORDER BY opens_at ASC
                    LIMIT 1
                    """,
                    (route.instrument_id, route.fingerprint, session_key, timestamp),
                )
                row = cur.fetchone()
        if not row:
            return None
        return ensure_utc(row[0])

    def upsert_trading_hours(
        self,
        timezone: str,
        trading_hours: str,
        liquid_hours: str,
        payload: dict[str, Any] | None = None,
        *,
        instrument: dict[str, Any],
    ) -> None:
        self._psycopg()
        from psycopg.types.json import Jsonb

        route = route_instrument(instrument)
        provider_key = route.provider
        symbol_key = route.provider_symbol
        fetched_at = utc_now()
        payload_dict = dict(payload or {})
        provider_contract_id = require_exact_identity_text(
            route.adapter.session_contract_id(route.instrument),
            field="provider_contract_id",
        )
        payload_dict["provider_contract_id"] = provider_contract_id
        snapshot_intervals = normalize_trading_session_intervals(
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            provider=provider_key,
            symbol=symbol_key,
            provider_contract_id=provider_contract_id,
            payload=payload_dict,
            fetched_at=fetched_at,
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{route.fingerprint}:trading-hours",),
                )
                cur.execute(
                    """
                    INSERT INTO trading_hours (
                        instrument_id, route_fingerprint, provider, symbol,
                        timezone, trading_hours, liquid_hours, payload,
                        fetched_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, route_fingerprint) DO UPDATE SET
                        provider = EXCLUDED.provider,
                        symbol = EXCLUDED.symbol,
                        timezone = EXCLUDED.timezone,
                        trading_hours = EXCLUDED.trading_hours,
                        liquid_hours = EXCLUDED.liquid_hours,
                        payload = EXCLUDED.payload,
                        fetched_at = EXCLUDED.fetched_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        route.instrument_id,
                        route.fingerprint,
                        provider_key,
                        symbol_key,
                        timezone,
                        trading_hours,
                        liquid_hours,
                        Jsonb(payload_dict),
                        fetched_at,
                        fetched_at,
                    ),
                )
                cur.execute(
                    """
                    SELECT provider_contract_id, timezone, trading_hours, liquid_hours, raw_payload
                    FROM trading_schedule_snapshots
                    WHERE instrument_id = %s AND route_fingerprint = %s
                    ORDER BY fetched_at DESC
                    LIMIT 1
                    """,
                    (route.instrument_id, route.fingerprint),
                )
                latest_snapshot = cur.fetchone()
                schedule_changed = latest_snapshot is None or (
                    str(latest_snapshot[0] or "") != provider_contract_id
                    or str(latest_snapshot[1] or "") != str(timezone or "")
                    or str(latest_snapshot[2] or "") != str(trading_hours or "")
                    or str(latest_snapshot[3] or "") != str(liquid_hours or "")
                    or dict(latest_snapshot[4] or {}) != payload_dict
                )
                if schedule_changed:
                    cur.execute(
                        """
                        INSERT INTO trading_schedule_snapshots (
                            instrument_id, route_fingerprint, provider, symbol, provider_contract_id, timezone,
                            trading_hours, liquid_hours, raw_payload, fetched_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (instrument_id, route_fingerprint, fetched_at) DO NOTHING
                        """,
                        (
                            route.instrument_id,
                            route.fingerprint,
                            provider_key,
                            symbol_key,
                            provider_contract_id,
                            timezone,
                            trading_hours,
                            liquid_hours,
                            Jsonb(payload_dict),
                            fetched_at,
                            fetched_at,
                        ),
                    )
                    replacements = trading_session_interval_replacement_horizons(snapshot_intervals)
                    for replacement in replacements:
                        cur.execute(
                            """
                            DELETE FROM trading_session_intervals
                            WHERE instrument_id = %s
                              AND route_fingerprint = %s
                              AND session_type = %s
                              AND opens_at < %s
                              AND closes_at > %s
                            """,
                            (
                                replacement.instrument_id,
                                replacement.route_fingerprint,
                                replacement.session_type,
                                replacement.ends_at,
                                replacement.starts_at,
                            ),
                        )
                    interval_payload = [
                        {
                            "instrument_id": row["instrument_id"],
                            "route_fingerprint": row["route_fingerprint"],
                            "provider": row["provider"],
                            "symbol": row["symbol"],
                            "provider_contract_id": row["provider_contract_id"],
                            "session_date": row["session_date"].isoformat(),
                            "session_type": row["session_type"],
                            "opens_at": ensure_utc(row["opens_at"]).isoformat(),
                            "closes_at": ensure_utc(row["closes_at"]).isoformat(),
                            "status": row["status"],
                            "source_fetched_at": ensure_utc(row["source_fetched_at"]).isoformat(),
                            "metadata": dict(row.get("metadata") or {}),
                        }
                        for replacement in replacements
                        for row in replacement.rows
                    ]
                    if interval_payload:
                        cur.execute(
                            """
                            WITH input AS MATERIALIZED (
                                SELECT *
                                FROM jsonb_to_recordset(%s::jsonb) AS row(
                                    instrument_id text,
                                    route_fingerprint text,
                                    provider text,
                                    symbol text,
                                    provider_contract_id text,
                                    session_date date,
                                    session_type text,
                                    opens_at timestamptz,
                                    closes_at timestamptz,
                                    status text,
                                    source_fetched_at timestamptz,
                                    metadata jsonb
                                )
                            )
                            INSERT INTO trading_session_intervals (
                                instrument_id, route_fingerprint, provider, symbol, provider_contract_id,
                                session_date, session_type, opens_at, closes_at, status,
                                source_fetched_at, metadata, updated_at
                            )
                            SELECT
                                instrument_id, route_fingerprint, provider, symbol, provider_contract_id,
                                session_date, session_type, opens_at, closes_at, status,
                                source_fetched_at, metadata, now()
                            FROM input
                            ON CONFLICT (
                                instrument_id, route_fingerprint, session_type, opens_at, closes_at
                            ) DO UPDATE SET
                                provider = EXCLUDED.provider,
                                symbol = EXCLUDED.symbol,
                                provider_contract_id = EXCLUDED.provider_contract_id,
                                session_date = EXCLUDED.session_date,
                                status = EXCLUDED.status,
                                source_fetched_at = EXCLUDED.source_fetched_at,
                                metadata = EXCLUDED.metadata,
                                updated_at = now()
                            """,
                            (Jsonb(interval_payload),),
                        )
        if schedule_changed:
            self._invalidate_bar_slots_cache()
