from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.runtime.math_utils import float_or_none

WATCHLIST_TREND_WINDOW_MINUTES = 180
WATCHLIST_TREND_BUCKET_MINUTES = 5
WATCHLIST_TREND_MAX_POINTS = 36
WATCHLIST_TREND_SOURCE = "quote_snapshots"


def load_watchlist_trend_snapshot(
    instruments: list[dict[str, Any]],
    store: Any | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the exact-route watchlist trend read model from persisted quotes only."""

    end = now or datetime.now(tz=UTC)
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("WATCHLIST_TREND_NOW_MUST_BE_AWARE")
    end = end.astimezone(UTC)
    start = end - timedelta(minutes=WATCHLIST_TREND_WINDOW_MINUTES)
    routes = [route_instrument(instrument) for instrument in instruments]

    def response_rows(
        status: str,
        points_by_identity: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        source_points = points_by_identity or {}
        for route in routes:
            points: list[dict[str, float | str]] = []
            for point in source_points.get((route.instrument_id, route.fingerprint), []):
                if not isinstance(point, dict):
                    continue
                ts = parse_aware_utc_ts(point.get("ts"))
                price = float_or_none(point.get("price"))
                if ts is None or price is None:
                    continue
                points.append({"ts": ts.isoformat(), "price": price})
            points.sort(key=lambda point: str(point["ts"]))
            points = points[-WATCHLIST_TREND_MAX_POINTS:]
            row_status = status
            if status == "ready" and len(points) < 2:
                row_status = "insufficient_data"
            rows.append(
                {
                    "instrument_id": route.instrument_id,
                    "route_fingerprint": route.fingerprint,
                    "status": row_status,
                    "source": WATCHLIST_TREND_SOURCE,
                    "as_of": points[-1]["ts"] if points else None,
                    "points": points,
                }
            )
        return rows

    if store is None:
        rows = response_rows("storage_unavailable")
    else:
        try:
            points_by_identity = store.read_quote_snapshot_buckets_batch(
                [(route.provider, route.instrument_id, route.fingerprint) for route in routes],
                start,
                end,
                bucket_minutes=WATCHLIST_TREND_BUCKET_MINUTES,
                max_points=WATCHLIST_TREND_MAX_POINTS,
            )
            rows = response_rows("ready", points_by_identity)
        except Exception:
            rows = response_rows("storage_error")

    return {
        "window_minutes": WATCHLIST_TREND_WINDOW_MINUTES,
        "bucket_minutes": WATCHLIST_TREND_BUCKET_MINUTES,
        "rows": rows,
    }
