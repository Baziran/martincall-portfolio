from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from aef_terminal.data.tick_buffer import TickBatchWriteError
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import provider_key_for_source
from aef_terminal.domain import Tick
from aef_terminal.storage.db_utils import (
    ensure_utc,
    safe_tick_bucket,
    safe_tick_history_interval,
    utc_now,
)
from aef_terminal.storage.repos.connection import CanonicalWriterLeaseLost
from aef_terminal.storage.repos.schema import _timescaledb_sql


def _drop_expired_empty_tick_chunks(cur: Any, cutoff: datetime) -> int:
    """Drop only wholly expired chunks after every raw row before cutoff is gone."""

    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM ticks
            WHERE ts < %s
            LIMIT 1
        )
        """,
        (cutoff,),
    )
    remaining = cur.fetchone()
    if remaining and remaining[0]:
        return 0
    cur.execute(
        _timescaledb_sql(
            cur,
            """
            SELECT {timescaledb}.drop_chunks(
                'ticks'::regclass,
                older_than => %s::timestamptz
            )
            """,
        ),
        (cutoff,),
    )
    return len(cur.fetchall())


class TicksRepoMixin:
    """PostgreSQL repository mixin."""

    def rollup_ticks(
        self,
        older_than: datetime | None = None,
        *,
        provider: str,
    ) -> dict[str, int]:
        cutoff = ensure_utc(older_than or utc_now())
        return self._rollup_and_purge_ticks(cutoff, provider=provider)

    def purge_raw_ticks(
        self,
        older_than: datetime | None,
        provider: str,
        rollup_first: bool = True,
    ) -> dict[str, int]:
        cutoff = ensure_utc(older_than or utc_now())
        return self._rollup_and_purge_ticks(
            cutoff,
            provider=provider,
            rollup=rollup_first,
        )

    def _rollup_and_purge_ticks(
        self,
        cutoff: datetime,
        provider: str,
        *,
        rollup: bool = True,
    ) -> dict[str, int]:
        provider_key = provider_key_for_source(provider)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"tick-purge:{provider_key}",),
                    )
                    cur.execute(
                        """
                        CREATE TEMP TABLE tick_purge_batch (
                            ingest_seq bigint NOT NULL,
                            provider text NOT NULL,
                            instrument_id text NOT NULL,
                            route_fingerprint text NOT NULL,
                            ts timestamptz NOT NULL,
                            price double precision NOT NULL,
                            volume integer NOT NULL,
                            delta_sign smallint NOT NULL
                        ) ON COMMIT DROP
                        """
                    )
                    cur.execute(
                        _timescaledb_sql(
                            cur,
                            """
                        WITH next_bucket AS (
                            SELECT
                                provider,
                                instrument_id,
                                route_fingerprint,
                                {timescaledb}.time_bucket('5 minutes', ts) AS bucket
                            FROM ticks
                            WHERE provider = %s
                              AND ts < {timescaledb}.time_bucket(
                                  '5 minutes', %s::timestamptz
                              )
                            ORDER BY ts ASC, ingest_seq ASC
                            LIMIT 1
                        )
                        INSERT INTO tick_purge_batch (
                            ingest_seq, provider, instrument_id, route_fingerprint,
                            ts, price, volume, delta_sign
                        )
                        SELECT
                            raw.ingest_seq,
                            raw.provider,
                            raw.instrument_id,
                            raw.route_fingerprint,
                            raw.ts,
                            raw.price,
                            raw.volume,
                            raw.delta_sign
                        FROM ticks AS raw
                        JOIN next_bucket AS selected
                          ON raw.provider = selected.provider
                         AND raw.instrument_id = selected.instrument_id
                         AND raw.route_fingerprint = selected.route_fingerprint
                         AND raw.ts >= selected.bucket
                         AND raw.ts < selected.bucket + INTERVAL '5 minutes'
                        ORDER BY raw.ts ASC, raw.ingest_seq ASC
                            """,
                        ),
                        (provider_key, cutoff),
                    )
                    staged = int(cur.rowcount or 0)
                    if staged <= 0:
                        return {
                            "delta_rows": 0,
                            "profile_rows": 0,
                            "deleted_ticks": 0,
                            "dropped_chunks": _drop_expired_empty_tick_chunks(cur, cutoff),
                            "has_more": 0,
                        }
                    # A session-local temp table has no planner statistics after
                    # INSERT. Without this explicit analysis PostgreSQL can choose
                    # a merge join that replays every hypertable chunk through the
                    # route indexes for the bounded delete. The exact staged batch
                    # is the delete relation, so analyze it before any plan that
                    # joins it back to the raw hypertable.
                    cur.execute("ANALYZE tick_purge_batch")
                    delta_rows = 0
                    profile_rows = 0
                    if rollup:
                        cur.execute(
                            _timescaledb_sql(
                                cur,
                                """
                            INSERT INTO tick_delta_1m_rollup (
                                provider, instrument_id, route_fingerprint, bucket,
                                total_volume, net_delta, trade_count, updated_at
                            )
                            SELECT
                                provider,
                                instrument_id,
                                route_fingerprint,
                                {timescaledb}.time_bucket('1 minute', ts) AS bucket,
                                SUM(volume)::bigint,
                                SUM(volume * delta_sign)::bigint,
                                COUNT(*)::bigint,
                                now()
                            FROM tick_purge_batch
                            GROUP BY provider, instrument_id, route_fingerprint, bucket
                            ON CONFLICT (provider, instrument_id, route_fingerprint, bucket) DO UPDATE SET
                                total_volume = tick_delta_1m_rollup.total_volume + EXCLUDED.total_volume,
                                net_delta = tick_delta_1m_rollup.net_delta + EXCLUDED.net_delta,
                                trade_count = tick_delta_1m_rollup.trade_count + EXCLUDED.trade_count,
                                updated_at = now()
                                """,
                            )
                        )
                        delta_rows = int(cur.rowcount or 0)
                        cur.execute(
                            _timescaledb_sql(
                                cur,
                                """
                            INSERT INTO tick_volume_profile_5m_rollup (
                                provider, instrument_id, route_fingerprint, bucket, price, total_volume,
                                buy_volume, sell_volume, net_delta, trade_count, updated_at
                            )
                            SELECT
                                provider,
                                instrument_id,
                                route_fingerprint,
                                {timescaledb}.time_bucket('5 minutes', ts) AS bucket,
                                price,
                                SUM(volume)::bigint,
                                SUM(CASE WHEN delta_sign = 1 THEN volume ELSE 0 END)::bigint,
                                SUM(CASE WHEN delta_sign = -1 THEN volume ELSE 0 END)::bigint,
                                SUM(volume * delta_sign)::bigint,
                                COUNT(*)::bigint,
                                now()
                            FROM tick_purge_batch
                            GROUP BY provider, instrument_id, route_fingerprint, bucket, price
                            ON CONFLICT (provider, instrument_id, route_fingerprint, bucket, price) DO UPDATE SET
                                total_volume = tick_volume_profile_5m_rollup.total_volume + EXCLUDED.total_volume,
                                buy_volume = tick_volume_profile_5m_rollup.buy_volume + EXCLUDED.buy_volume,
                                sell_volume = tick_volume_profile_5m_rollup.sell_volume + EXCLUDED.sell_volume,
                                net_delta = tick_volume_profile_5m_rollup.net_delta + EXCLUDED.net_delta,
                                trade_count = tick_volume_profile_5m_rollup.trade_count + EXCLUDED.trade_count,
                                updated_at = now()
                                """,
                            )
                        )
                        profile_rows = int(cur.rowcount or 0)
                    cur.execute(
                        """
                        DELETE FROM ticks AS raw
                        USING tick_purge_batch AS batch
                        WHERE raw.provider = batch.provider
                          AND raw.instrument_id = batch.instrument_id
                          AND raw.route_fingerprint = batch.route_fingerprint
                          AND raw.ts = batch.ts
                          AND raw.ingest_seq = batch.ingest_seq
                        """
                    )
                    deleted = int(cur.rowcount or 0)
                    if deleted != staged:
                        raise RuntimeError(
                            f"TICK_PURGE_CARDINALITY_MISMATCH staged={staged} deleted={deleted}"
                        )
                    cur.execute(
                        _timescaledb_sql(
                            cur,
                            """
                        SELECT EXISTS (
                            SELECT 1
                            FROM ticks
                            WHERE provider = %s
                              AND ts < {timescaledb}.time_bucket(
                                  '5 minutes', %s::timestamptz
                              )
                            LIMIT 1
                        )
                            """,
                        ),
                        (provider_key, cutoff),
                    )
                    remaining_row = cur.fetchone()
                    has_more = bool(remaining_row and remaining_row[0])
                    dropped_chunks = 0 if has_more else _drop_expired_empty_tick_chunks(cur, cutoff)
        return {
            "delta_rows": delta_rows,
            "profile_rows": profile_rows,
            "deleted_ticks": deleted,
            "dropped_chunks": dropped_chunks,
            "has_more": int(has_more),
        }

    def write_ticks(self, ticks: Sequence[Tick], provider: str) -> int:
        if not ticks:
            return 0
        try:
            provider_key = provider_key_for_source(provider)
            for tick in ticks:
                require_exact_identity_text(tick.instrument_id, field="instrument_id")
                require_exact_identity_text(tick.route_fingerprint, field="route_fingerprint")
            rows = [
                (
                    provider_key,
                    tick.instrument_id,
                    tick.route_fingerprint,
                    tick.ts,
                    float(tick.price),
                    tick.volume,
                    tick.delta_sign,
                    tick.tick_type,
                    float(tick.bid) if tick.bid is not None else None,
                    float(tick.ask) if tick.ask is not None else None,
                    tick.exchange,
                    list(tick.conditions),
                )
                for tick in ticks
            ]
        except Exception as exc:
            raise TickBatchWriteError(
                str(exc) or exc.__class__.__name__,
                commit_outcome="not_committed",
                retryable=False,
            ) from exc
        self._require_canonical_writer_lease()
        copy_started = False
        try:
            with self._connect() as conn:
                with conn.transaction(), conn.cursor() as cur:
                    self._acquire_canonical_writer_fence_on_cursor(cur)
                    with cur.copy(
                        """
                        COPY ticks (
                            provider, instrument_id, route_fingerprint, ts, price, volume, delta_sign,
                            tick_type, bid, ask, exchange, conditions
                        )
                        FROM STDIN
                        """,
                    ) as tick_copy:
                        copy_started = True
                        for row in rows:
                            tick_copy.write_row(row)
        except CanonicalWriterLeaseLost:
            raise
        except Exception as exc:
            sqlstate = str(getattr(exc, "sqlstate", "") or "")
            commit_outcome: Literal["not_committed", "unknown"] = (
                "not_committed" if not copy_started or bool(sqlstate) else "unknown"
            )
            retryable = bool(
                not copy_started
                or sqlstate.startswith("40")
                or sqlstate in {"55P03", "57014", "57P01", "57P02", "57P03"}
            )
            raise TickBatchWriteError(
                str(exc) or exc.__class__.__name__,
                commit_outcome=commit_outcome,
                retryable=retryable and commit_outcome == "not_committed",
            ) from exc
        return len(rows)

    def read_tick_delta(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        bucket: str = "1 minute",
        provider: str = "ibkr",
        include_rollup: bool = True,
    ) -> list[dict[str, Any]]:
        provider = provider_key_for_source(provider)
        bucket_interval = safe_tick_bucket(bucket)
        with self._connect() as conn:
            with conn.cursor() as cur:
                if include_rollup and bucket_interval == "1 minute":
                    cur.execute(
                        _timescaledb_sql(
                            cur,
                            """
                        WITH rollup AS (
                            SELECT bucket, total_volume, net_delta, trade_count
                            FROM tick_delta_1m_rollup
                            WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                              AND bucket >= %s AND bucket <= %s
                        ),
                        raw AS (
                            SELECT
                                {timescaledb}.time_bucket('1 minute', t.ts) AS bucket,
                                SUM(t.volume)::bigint AS total_volume,
                                SUM(t.volume * t.delta_sign)::bigint AS net_delta,
                                COUNT(*)::bigint AS trade_count
                            FROM ticks t
                            WHERE t.provider = %s AND t.instrument_id = %s AND t.route_fingerprint = %s
                              AND t.ts >= %s AND t.ts <= %s
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM tick_delta_1m_rollup r
                                  WHERE r.provider = t.provider
                                    AND r.instrument_id = t.instrument_id
                                    AND r.route_fingerprint = t.route_fingerprint
                                    AND r.bucket = {timescaledb}.time_bucket('1 minute', t.ts)
                              )
                            GROUP BY 1
                        ),
                        merged AS (
                            SELECT * FROM rollup
                            UNION ALL
                            SELECT * FROM raw
                        )
                        SELECT
                            bucket,
                            SUM(total_volume)::bigint AS total_volume,
                            SUM(net_delta)::bigint AS net_delta,
                            SUM(trade_count)::bigint AS trade_count
                        FROM merged
                        GROUP BY bucket
                        ORDER BY 1 ASC
                            """,
                        ),
                        (
                            provider,
                            instrument_id,
                            route_fingerprint,
                            ensure_utc(start),
                            ensure_utc(end),
                            provider,
                            instrument_id,
                            route_fingerprint,
                            ensure_utc(start),
                            ensure_utc(end),
                        ),
                    )
                    rows = cur.fetchall()
                    return [
                        {
                            "ts": ensure_utc(row[0]).isoformat(),
                            "total_volume": int(row[1]),
                            "net_delta": int(row[2]),
                            "trade_count": int(row[3]),
                        }
                        for row in rows
                    ]
                cur.execute(
                    _timescaledb_sql(
                        cur,
                        """
                    SELECT
                        {timescaledb}.time_bucket(%s::interval, ts) AS bucket,
                        SUM(volume)::bigint AS total_volume,
                        SUM(volume * delta_sign)::bigint AS net_delta,
                        COUNT(*)::bigint AS trade_count
                    FROM ticks
                    WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                      AND ts >= %s AND ts <= %s
                    GROUP BY 1
                    ORDER BY 1 ASC
                        """,
                    ),
                    (
                        bucket_interval,
                        provider,
                        instrument_id,
                        route_fingerprint,
                        ensure_utc(start),
                        ensure_utc(end),
                    ),
                )
                rows = cur.fetchall()
        return [
            {
                "ts": ensure_utc(row[0]).isoformat(),
                "total_volume": int(row[1]),
                "net_delta": int(row[2]),
                "trade_count": int(row[3]),
            }
            for row in rows
        ]

    def read_tick_bar_history(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
        provider: str = "ibkr",
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        provider = provider_key_for_source(provider)
        interval = safe_tick_history_interval(timeframe)
        row_limit = max(1, min(int(limit or 5000), 50000))
        start_dt = ensure_utc(start)
        end_dt = ensure_utc(end)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _timescaledb_sql(
                        cur,
                        """
                    WITH rollup AS (
                        SELECT bucket, total_volume, net_delta, trade_count
                        FROM tick_delta_1m_rollup
                        WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                          AND bucket >= %s AND bucket <= %s
                    ),
                    raw AS (
                        SELECT
                            {timescaledb}.time_bucket('1 minute', t.ts) AS bucket,
                            SUM(t.volume)::bigint AS total_volume,
                            SUM(t.volume * t.delta_sign)::bigint AS net_delta,
                            COUNT(*)::bigint AS trade_count
                        FROM ticks t
                        WHERE t.provider = %s AND t.instrument_id = %s AND t.route_fingerprint = %s
                          AND t.ts >= %s AND t.ts <= %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM tick_delta_1m_rollup r
                              WHERE r.provider = t.provider
                                AND r.instrument_id = t.instrument_id
                                AND r.route_fingerprint = t.route_fingerprint
                                AND r.bucket = {timescaledb}.time_bucket('1 minute', t.ts)
                          )
                        GROUP BY 1
                    ),
                    merged_1m AS (
                        SELECT * FROM rollup
                        UNION ALL
                        SELECT * FROM raw
                    ),
                    bars AS (
                        SELECT
                            {timescaledb}.time_bucket(%s::interval, bucket) AS bucket,
                            SUM(total_volume)::bigint AS total_volume,
                            SUM(net_delta)::bigint AS net_delta,
                            SUM(trade_count)::bigint AS trade_count
                        FROM merged_1m
                        GROUP BY 1
                    )
                    SELECT
                        bucket,
                        total_volume,
                        net_delta,
                        trade_count,
                        CASE
                            WHEN total_volume > 0 THEN net_delta::double precision / total_volume::double precision
                            ELSE 0.0
                        END AS delta_ratio
                    FROM bars
                    ORDER BY bucket ASC
                    LIMIT %s
                        """,
                    ),
                    (
                        provider,
                        instrument_id,
                        route_fingerprint,
                        start_dt,
                        end_dt,
                        provider,
                        instrument_id,
                        route_fingerprint,
                        start_dt,
                        end_dt,
                        interval,
                        row_limit,
                    ),
                )
                rows = cur.fetchall()
        return [
            {
                "ts": ensure_utc(row[0]).isoformat(),
                "total_volume": int(row[1]),
                "net_delta": int(row[2]),
                "trade_count": int(row[3]),
                "delta_ratio": float(row[4]),
            }
            for row in rows
        ]

    def read_tick_volume_profile_history(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
        price_step: float = 0.25,
        provider: str = "ibkr",
        limit: int = 50000,
    ) -> list[dict[str, Any]]:
        provider = provider_key_for_source(provider)
        interval = safe_tick_history_interval(timeframe)
        step = max(float(price_step), 0.000001)
        row_limit = max(1, min(int(limit or 50000), 200000))
        start_dt = ensure_utc(start)
        end_dt = ensure_utc(end)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _timescaledb_sql(
                        cur,
                        """
                    WITH rollup AS (
                        SELECT
                            {timescaledb}.time_bucket(%s::interval, bucket) AS bucket,
                            (floor((price / %s) + 0.5) * %s)::double precision AS price,
                            SUM(total_volume)::bigint AS total_volume,
                            SUM(buy_volume)::bigint AS buy_volume,
                            SUM(sell_volume)::bigint AS sell_volume,
                            SUM(net_delta)::bigint AS net_delta,
                            SUM(trade_count)::bigint AS trade_count
                        FROM tick_volume_profile_5m_rollup
                        WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                          AND bucket >= %s AND bucket <= %s
                        GROUP BY 1, 2
                    ),
                    raw AS (
                        SELECT
                            {timescaledb}.time_bucket(%s::interval, t.ts) AS bucket,
                            (floor((t.price / %s) + 0.5) * %s)::double precision AS price,
                            SUM(t.volume)::bigint AS total_volume,
                            SUM(CASE WHEN t.delta_sign = 1 THEN t.volume ELSE 0 END)::bigint AS buy_volume,
                            SUM(CASE WHEN t.delta_sign = -1 THEN t.volume ELSE 0 END)::bigint AS sell_volume,
                            SUM(t.volume * t.delta_sign)::bigint AS net_delta,
                            COUNT(*)::bigint AS trade_count
                        FROM ticks t
                        WHERE t.provider = %s AND t.instrument_id = %s AND t.route_fingerprint = %s
                          AND t.ts >= %s AND t.ts <= %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM tick_volume_profile_5m_rollup r
                              WHERE r.provider = t.provider
                                AND r.instrument_id = t.instrument_id
                                AND r.route_fingerprint = t.route_fingerprint
                                AND r.bucket = {timescaledb}.time_bucket('5 minutes', t.ts)
                                AND r.price = t.price
                          )
                        GROUP BY 1, 2
                    ),
                    merged AS (
                        SELECT * FROM rollup
                        UNION ALL
                        SELECT * FROM raw
                    )
                    SELECT
                        bucket,
                        price,
                        SUM(total_volume)::bigint AS total_volume,
                        SUM(buy_volume)::bigint AS buy_volume,
                        SUM(sell_volume)::bigint AS sell_volume,
                        SUM(net_delta)::bigint AS net_delta,
                        SUM(trade_count)::bigint AS trade_count,
                        CASE
                            WHEN SUM(total_volume) > 0 THEN SUM(net_delta)::double precision / SUM(total_volume)::double precision
                            ELSE 0.0
                        END AS delta_ratio
                    FROM merged
                    GROUP BY bucket, price
                    ORDER BY bucket ASC, price ASC
                    LIMIT %s
                        """,
                    ),
                    (
                        interval,
                        step,
                        step,
                        provider,
                        instrument_id,
                        route_fingerprint,
                        start_dt,
                        end_dt,
                        interval,
                        step,
                        step,
                        provider,
                        instrument_id,
                        route_fingerprint,
                        start_dt,
                        end_dt,
                        row_limit,
                    ),
                )
                rows = cur.fetchall()
        return [
            {
                "ts": ensure_utc(row[0]).isoformat(),
                "price": float(row[1]),
                "total_volume": int(row[2]),
                "buy_volume": int(row[3]),
                "sell_volume": int(row[4]),
                "net_delta": int(row[5]),
                "trade_count": int(row[6]),
                "delta_ratio": float(row[7]),
            }
            for row in rows
        ]

    def read_volume_profile(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        price_step: float = 0.25,
        provider: str = "ibkr",
        include_rollup: bool = True,
    ) -> list[dict[str, Any]]:
        provider = provider_key_for_source(provider)
        step = max(float(price_step), 0.000001)
        with self._connect() as conn:
            with conn.cursor() as cur:
                if include_rollup:
                    cur.execute(
                        _timescaledb_sql(
                            cur,
                            """
                        SELECT
                            price,
                            SUM(total_volume)::bigint AS total_volume,
                            SUM(buy_volume)::bigint AS buy_volume,
                            SUM(sell_volume)::bigint AS sell_volume,
                            SUM(net_delta)::bigint AS net_delta,
                            SUM(trade_count)::bigint AS trade_count
                        FROM (
                            SELECT
                                (floor((price / %s) + 0.5) * %s)::double precision AS price,
                                total_volume,
                                buy_volume,
                                sell_volume,
                                net_delta,
                                trade_count
                            FROM tick_volume_profile_5m_rollup
                            WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                              AND bucket >= %s AND bucket <= %s
                            UNION ALL
                            SELECT
                                (floor((price / %s) + 0.5) * %s)::double precision AS price,
                                SUM(t.volume)::bigint AS total_volume,
                                SUM(CASE WHEN t.delta_sign = 1 THEN t.volume ELSE 0 END)::bigint AS buy_volume,
                                SUM(CASE WHEN t.delta_sign = -1 THEN t.volume ELSE 0 END)::bigint AS sell_volume,
                                SUM(t.volume * t.delta_sign)::bigint AS net_delta,
                                COUNT(*)::bigint AS trade_count
                            FROM ticks t
                            WHERE t.provider = %s AND t.instrument_id = %s AND t.route_fingerprint = %s
                              AND t.ts >= %s AND t.ts <= %s
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM tick_volume_profile_5m_rollup r
                                  WHERE r.provider = t.provider
                                    AND r.instrument_id = t.instrument_id
                                    AND r.route_fingerprint = t.route_fingerprint
                                    AND r.bucket = {timescaledb}.time_bucket('5 minutes', t.ts)
                                    AND r.price = t.price
                              )
                            GROUP BY 1
                        ) merged
                        GROUP BY price
                        ORDER BY price ASC
                            """,
                        ),
                        (
                            step,
                            step,
                            provider,
                            instrument_id,
                            route_fingerprint,
                            ensure_utc(start),
                            ensure_utc(end),
                            step,
                            step,
                            provider,
                            instrument_id,
                            route_fingerprint,
                            ensure_utc(start),
                            ensure_utc(end),
                        ),
                    )
                    rows = cur.fetchall()
                    return [
                        {
                            "price": float(row[0]),
                            "total_volume": int(row[1]),
                            "buy_volume": int(row[2]),
                            "sell_volume": int(row[3]),
                            "net_delta": int(row[4]),
                            "trade_count": int(row[5]),
                        }
                        for row in rows
                    ]
                cur.execute(
                    """
                    SELECT
                        (floor((price / %s) + 0.5) * %s)::double precision AS price,
                        SUM(volume)::bigint AS total_volume,
                        SUM(CASE WHEN delta_sign = 1 THEN volume ELSE 0 END)::bigint AS buy_volume,
                        SUM(CASE WHEN delta_sign = -1 THEN volume ELSE 0 END)::bigint AS sell_volume,
                        SUM(volume * delta_sign)::bigint AS net_delta,
                        COUNT(*)::bigint AS trade_count
                    FROM ticks
                    WHERE provider = %s AND instrument_id = %s AND route_fingerprint = %s
                      AND ts >= %s AND ts <= %s
                    GROUP BY 1
                    ORDER BY 1 ASC
                    """,
                    (
                        step,
                        step,
                        provider,
                        instrument_id,
                        route_fingerprint,
                        ensure_utc(start),
                        ensure_utc(end),
                    ),
                )
                rows = cur.fetchall()
        return [
            {
                "price": float(row[0]),
                "total_volume": int(row[1]),
                "buy_volume": int(row[2]),
                "sell_volume": int(row[3]),
                "net_delta": int(row[4]),
                "trade_count": int(row[5]),
            }
            for row in rows
        ]
