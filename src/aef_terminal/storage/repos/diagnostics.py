from __future__ import annotations

from typing import Any

from aef_terminal.storage.db_utils import ensure_utc, format_pg_setting


class DiagnosticsRepoMixin:
    """PostgreSQL repository mixin."""

    def _canonical_writer_readiness(self) -> dict[str, Any]:
        status = self.canonical_writer_lease_status(verify=True)
        if not isinstance(status, dict) or status.get("ok") is not True:
            return {
                "configured": True,
                "ok": False,
                "canonical_writer": status if isinstance(status, dict) else {},
                "message": (
                    str(status.get("message") or "CANONICAL_WRITER_LEASE_REQUIRED")
                    if isinstance(status, dict)
                    else "CANONICAL_WRITER_LEASE_STATUS_INVALID"
                ),
            }
        return status

    def _estimated_bars_count(self, cur) -> int:
        cur.execute(
            """
            WITH chunk_estimates AS (
                SELECT GREATEST(c.reltuples, 0)::bigint AS rows
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class p ON p.oid = i.inhparent
                JOIN pg_namespace n ON n.oid = p.relnamespace
                WHERE p.relname = 'bars'
                  AND n.nspname = ANY (current_schemas(false))
            ),
            parent_estimate AS (
                SELECT GREATEST(c.reltuples, 0)::bigint AS rows
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = 'bars'
                  AND n.nspname = ANY (current_schemas(false))
                LIMIT 1
            )
            SELECT GREATEST(
                COALESCE((SELECT SUM(rows) FROM chunk_estimates), 0),
                COALESCE((SELECT rows FROM parent_estimate), 0)
            )::bigint
            """
        )
        return int((cur.fetchone() or [0])[0] or 0)

    def _data_hygiene_report(self, cur) -> dict[str, Any]:
        cur.execute(
            """
            SELECT instrument_id, route_fingerprint, provider, symbol, timeframe, source, closed,
                   COUNT(*)::bigint, MAX(updated_at)
            FROM bars
            WHERE updated_at >= now() - interval '14 days'
              AND closed = false
            GROUP BY instrument_id, route_fingerprint, provider, symbol, timeframe, source, closed
            ORDER BY COUNT(*) DESC, MAX(updated_at) DESC
            LIMIT 24
            """
        )
        provisional_rows = [
            {
                "instrument_id": row[0],
                "route_fingerprint": row[1],
                "provider": row[2],
                "symbol": row[3],
                "timeframe": row[4],
                "source": row[5],
                "closed": bool(row[6]),
                "rows": int(row[7] or 0),
                "latest_updated_at": ensure_utc(row[8]).isoformat() if row[8] else None,
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT relname, n_live_tup::bigint, n_dead_tup::bigint,
                   last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
            FROM pg_stat_user_tables
            WHERE relname IN (
                'bars',
                'ticks',
                'tick_delta_1m_rollup',
                'tick_volume_profile_5m_rollup',
                'futures_contract_bars',
                'futures_continuous_bars'
            )
            ORDER BY n_dead_tup DESC
            LIMIT 16
            """
        )
        table_pressure = [
            {
                "table": row[0],
                "live_rows_est": int(row[1] or 0),
                "dead_rows_est": int(row[2] or 0),
                "dead_ratio": (float(row[2] or 0) / max(float((row[1] or 0) + (row[2] or 0)), 1.0)),
                "last_vacuum": ensure_utc(row[3]).isoformat() if row[3] else None,
                "last_autovacuum": ensure_utc(row[4]).isoformat() if row[4] else None,
                "last_analyze": ensure_utc(row[5]).isoformat() if row[5] else None,
                "last_autoanalyze": ensure_utc(row[6]).isoformat() if row[6] else None,
            }
            for row in cur.fetchall()
        ]
        max_dead_ratio = max((item["dead_ratio"] for item in table_pressure), default=0.0)
        severity = "warn" if provisional_rows or max_dead_ratio >= 0.2 else "ok"
        return {
            "severity": severity,
            "window": "14d provisional rows and current table pressure",
            "provisional_rows": provisional_rows,
            "table_pressure": table_pressure,
            "summary": {
                "provisional_groups": len(provisional_rows),
                "max_dead_ratio": max_dead_ratio,
            },
        }

    def _futures_continuous_report(self, cur) -> list[dict[str, Any]]:
        cur.execute(
            """
            SELECT DISTINCT ON (provider, instrument_id, series_type, roll_policy, timeframe)
                   provider, instrument_id, series_type, roll_policy, timeframe,
                   ts, source, closed, updated_at
            FROM futures_continuous_bars
            ORDER BY provider, instrument_id, series_type, roll_policy, timeframe, ts DESC
            LIMIT 48
            """
        )
        return [
            {
                "provider": row[0],
                "instrument_id": row[1],
                "series_type": row[2],
                "roll_policy": row[3],
                "timeframe": row[4],
                "last_bar_ts": ensure_utc(row[5]).isoformat() if row[5] else None,
                "source": row[6],
                "closed": bool(row[7]),
                "updated_at": ensure_utc(row[8]).isoformat() if row[8] else None,
            }
            for row in cur.fetchall()
        ]

    def readiness(self) -> dict[str, Any]:
        """Return the lightweight storage and canonical-writer readiness contract."""

        if not self.configured:
            return {"configured": False, "ok": False, "message": "AEF_DATABASE_URL is not set"}
        try:
            writer_status = self._canonical_writer_readiness()
            if writer_status.get("ok") is not True:
                return writer_status
            return {
                "configured": True,
                "ok": True,
                "canonical_writer": writer_status,
                "message": "PostgreSQL ready",
            }
        except Exception as exc:
            return {"configured": True, "ok": False, "message": str(exc)}

    def status(self) -> dict[str, Any]:
        readiness = self.readiness()
        if readiness.get("ok") is not True:
            return readiness
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    count = self._estimated_bars_count(cur)
            return {
                "configured": True,
                "ok": True,
                "bars": count,
                "bars_estimated": True,
                "canonical_writer": readiness["canonical_writer"],
                "message": "PostgreSQL ready",
            }
        except Exception as exc:
            return {"configured": True, "ok": False, "message": str(exc)}

    def diagnostics(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "ok": False, "message": "AEF_DATABASE_URL is not set"}
        try:
            writer_status = self._canonical_writer_readiness()
            if writer_status.get("ok") is not True:
                return writer_status
            with self._connect() as conn:
                with conn.cursor() as cur:
                    bars_count = self._estimated_bars_count(cur)
                    cur.execute(
                        """
                        SELECT GREATEST(
                                   (SELECT MAX(ts) FROM bars WHERE closed = true),
                                   (SELECT MAX(ts) FROM futures_continuous_bars WHERE closed = true)
                               ),
                               pg_database_size(current_database())
                        """
                    )
                    latest_ts, database_bytes = cur.fetchone()
                    cur.execute(
                        """
                        SELECT name, setting, unit
                        FROM pg_settings
                        WHERE name IN (
                            'shared_buffers',
                            'work_mem',
                            'maintenance_work_mem',
                            'effective_cache_size',
                            'max_parallel_workers_per_gather',
                            'max_parallel_workers',
                            'max_worker_processes',
                            'wal_buffers',
                            'max_wal_size',
                            'min_wal_size',
                            'checkpoint_timeout',
                            'checkpoint_completion_target',
                            'effective_io_concurrency',
                            'random_page_cost',
                            'default_statistics_target',
                            'timescaledb.max_background_workers'
                        )
                        ORDER BY name
                        """
                    )
                    settings = {row[0]: format_pg_setting(row[1], row[2]) for row in cur.fetchall()}
                    data_hygiene = self._data_hygiene_report(cur)
                    futures_continuous = self._futures_continuous_report(cur)
                    cur.execute(
                        """
                        SELECT provider, instrument_id, route_fingerprint, COUNT(*)::bigint, MIN(ts), MAX(ts),
                               SUM(volume)::bigint, SUM(volume * delta_sign)::bigint
                        FROM ticks
                        GROUP BY provider, instrument_id, route_fingerprint
                        ORDER BY COUNT(*) DESC
                        LIMIT 32
                        """
                    )
                    ticks = [
                        {
                            "provider": row[0],
                            "instrument_id": row[1],
                            "route_fingerprint": row[2],
                            "ticks": int(row[3] or 0),
                            "from": ensure_utc(row[4]).isoformat() if row[4] else None,
                            "to": ensure_utc(row[5]).isoformat() if row[5] else None,
                            "volume": int(row[6] or 0),
                            "net_delta": int(row[7] or 0),
                        }
                        for row in cur.fetchall()
                    ]
            return {
                "configured": True,
                "ok": True,
                "bars": int(bars_count or 0),
                "bars_estimated": True,
                "canonical_writer": writer_status,
                "diagnostics_source": "catalog_estimates_and_canonical_bars",
                "latest_bar_ts": ensure_utc(latest_ts).isoformat() if latest_ts else None,
                "database_bytes": int(database_bytes or 0),
                "settings": settings,
                "data_hygiene": data_hygiene,
                "futures_continuous": futures_continuous,
                "ticks": ticks,
                "message": "PostgreSQL ready",
            }
        except Exception as exc:
            return {"configured": True, "ok": False, "message": str(exc)}
