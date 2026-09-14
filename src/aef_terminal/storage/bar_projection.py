from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Sequence

from aef_terminal.storage.db_utils import ensure_utc
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection
from aef_terminal.runtime.timeframes import interval_bucket, interval_seconds


_CONFIRMED_BAR_TABLES = frozenset({"bars", "futures_contract_bars", "futures_continuous_bars"})
MAX_CONFIRMED_BAR_PROJECTION_ANCHORS = 512


def _derived_bucket_contract(
    source_timeframe: str,
    derived_timeframe: str,
) -> tuple[int, int, int, int]:
    source_seconds = interval_seconds(source_timeframe)
    target_seconds = interval_seconds(derived_timeframe)
    if target_seconds <= source_seconds or target_seconds % source_seconds:
        raise ValueError(
            "DERIVED_BAR_PROJECTION_INTERVAL_RATIO_INVALID "
            f"target={derived_timeframe} source={source_timeframe}"
        )
    child_count = target_seconds // source_seconds
    return target_seconds, source_seconds, child_count, (child_count - 1) * source_seconds


def _normalized_projection_anchors(
    anchor_timestamps: Sequence[datetime],
) -> tuple[datetime, ...]:
    anchors = tuple(sorted({ensure_utc(item) for item in anchor_timestamps}))
    if not anchors:
        raise ValueError("confirmed bar projection requires at least one anchor")
    if len(anchors) > MAX_CONFIRMED_BAR_PROJECTION_ANCHORS:
        raise ValueError(
            "confirmed bar projection anchor limit exceeded: "
            f"{len(anchors)} > {MAX_CONFIRMED_BAR_PROJECTION_ANCHORS}"
        )
    return anchors


def read_confirmed_bar_timestamps(
    store: Any,
    *,
    table: str,
    route_conditions: Sequence[str],
    route_params: Sequence[Any],
    timeframe: str,
    anchor_timestamps: Sequence[datetime],
    derived_timeframe: str | None = None,
) -> frozenset[datetime]:
    """Read an exact bounded set of confirmed anchors in one connection."""

    if table not in _CONFIRMED_BAR_TABLES:
        raise ValueError("confirmed bar projection table is invalid")
    anchors = _normalized_projection_anchors(anchor_timestamps)
    if not route_conditions or len(route_conditions) != len(route_params):
        raise ValueError("confirmed bar projection route is invalid")
    conditions = [*route_conditions, "timeframe = %s", "closed = true"]
    params: list[Any] = [*route_params, timeframe]
    if derived_timeframe is None:
        conditions.append("ts = ANY(%s::timestamptz[])")
        params.append(list(anchors))
        sql = f"SELECT ts FROM {table} WHERE {' AND '.join(conditions)} ORDER BY ts"
    else:
        (
            target_seconds,
            source_seconds,
            child_count,
            last_child_offset_seconds,
        ) = _derived_bucket_contract(timeframe, derived_timeframe)
        confirmed_before = interval_bucket(datetime.now(tz=UTC), derived_timeframe)
        conditions.extend(("ts >= %s", "ts < %s"))
        params.extend(
            (anchors[0], min(anchors[-1] + timedelta(seconds=target_seconds), confirmed_before))
        )
        bucket_sql = "to_timestamp(floor(extract(epoch FROM ts) / %s) * %s)"
        sql = (
            f"WITH source AS (SELECT {bucket_sql} AS bucket_ts, ts FROM {table} "
            f"WHERE {' AND '.join(conditions)}), "
            "derived AS (SELECT bucket_ts AS ts FROM source GROUP BY bucket_ts "
            "HAVING COUNT(*) = %s AND MIN(ts) = bucket_ts "
            "AND MAX(ts) = bucket_ts + (%s * interval '1 second') "
            "AND BOOL_AND(ts = to_timestamp(floor(extract(epoch FROM ts) / %s) * %s))) "
            "SELECT ts FROM derived WHERE ts = ANY(%s::timestamptz[]) ORDER BY ts"
        )
        params = [
            target_seconds,
            target_seconds,
            *params,
            child_count,
            last_child_offset_seconds,
            source_seconds,
            source_seconds,
            list(anchors),
        ]
    with store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    confirmed = tuple(ensure_utc(row[0]) for row in rows)
    if len(set(confirmed)) != len(confirmed) or not set(confirmed).issubset(anchors):
        raise ValueError("DRAWING_ANCHOR_STORAGE_AMBIGUOUS")
    return frozenset(confirmed)


def read_confirmed_bar_logical_projection(
    store: Any,
    *,
    table: str,
    route_conditions: Sequence[str],
    route_params: Sequence[Any],
    timeframe: str,
    anchor_timestamps: Sequence[datetime],
    derived_timeframe: str | None = None,
) -> ConfirmedBarLogicalProjection:
    """Count confirmed rows once and project exact anchors onto that ordering."""

    if table not in _CONFIRMED_BAR_TABLES:
        raise ValueError("confirmed bar projection table is invalid")
    anchors = _normalized_projection_anchors(anchor_timestamps)
    if not route_conditions or len(route_conditions) != len(route_params):
        raise ValueError("confirmed bar projection route is invalid")
    aggregate_columns = [
        "MIN(ts)",
        "MAX(ts)",
        "COUNT(*)",
        *("COUNT(*) FILTER (WHERE ts <= %s)" for _anchor in anchors),
        *("COUNT(*) FILTER (WHERE ts = %s)" for _anchor in anchors),
    ]
    conditions = [*route_conditions, "timeframe = %s", "closed = true", "ts >= %s"]
    if derived_timeframe is None:
        params = [*anchors, *anchors, *route_params, timeframe, anchors[0]]
        sql = f"SELECT {', '.join(aggregate_columns)} FROM {table} WHERE {' AND '.join(conditions)}"
    else:
        (
            target_seconds,
            source_seconds,
            child_count,
            last_child_offset_seconds,
        ) = _derived_bucket_contract(timeframe, derived_timeframe)
        confirmed_before = interval_bucket(datetime.now(tz=UTC), derived_timeframe)
        conditions.append("ts < %s")
        bucket_sql = "to_timestamp(floor(extract(epoch FROM ts) / %s) * %s)"
        sql = (
            f"WITH source AS (SELECT {bucket_sql} AS bucket_ts, ts FROM {table} "
            f"WHERE {' AND '.join(conditions)}), "
            "derived AS (SELECT bucket_ts AS ts FROM source GROUP BY bucket_ts "
            "HAVING COUNT(*) = %s AND MIN(ts) = bucket_ts "
            "AND MAX(ts) = bucket_ts + (%s * interval '1 second') "
            "AND BOOL_AND(ts = to_timestamp(floor(extract(epoch FROM ts) / %s) * %s))) "
            f"SELECT {', '.join(aggregate_columns)} FROM derived"
        )
        params = [
            target_seconds,
            target_seconds,
            *route_params,
            timeframe,
            anchors[0],
            confirmed_before,
            child_count,
            last_child_offset_seconds,
            source_seconds,
            source_seconds,
            *anchors,
            *anchors,
        ]
    with store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
    if row is None or row[0] is None or row[1] is None:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_EMPTY")
    origin_ts = ensure_utc(row[0])
    confirmed_through_ts = ensure_utc(row[1])
    confirmed_count = int(row[2])
    counts = tuple(int(value) for value in row[3 : 3 + len(anchors)])
    exact_counts = tuple(int(value) for value in row[3 + len(anchors) :])
    if (
        confirmed_count <= 0
        or len(counts) != len(anchors)
        or len(exact_counts) != len(anchors)
        or any(value != 1 for value in exact_counts)
    ):
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID")
    anchor_indices = {
        anchor: count - 1 for anchor, count in zip(anchors, counts, strict=True) if count > 0
    }
    if set(anchor_indices) != set(anchors):
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INCOMPLETE")
    return ConfirmedBarLogicalProjection(
        origin_ts=origin_ts,
        confirmed_through_ts=confirmed_through_ts,
        confirmed_count=confirmed_count,
        anchor_indices=MappingProxyType(anchor_indices),
    )
