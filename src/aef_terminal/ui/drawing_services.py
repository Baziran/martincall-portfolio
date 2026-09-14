from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aef_terminal.data.providers import (
    read_provider_bar_logical_projection,
    read_provider_confirmed_bar_timestamps,
    route_instrument,
)
from aef_terminal.runtime.chart_events import (
    chart_bars_updated_generation,
    require_chart_bars_generation,
)
from aef_terminal.storage.bar_projection import (
    MAX_CONFIRMED_BAR_PROJECTION_ANCHORS,
)
from aef_terminal.drawing_contract import (
    DRAWING_RETIRED_ALERT_FIELDS,
    DRAWING_RUNTIME_GEOMETRY_FIELDS,
    canonical_drawing_point,
    drawing_payload_points,
    parse_drawing_point_anchor,
    validate_durable_drawing_payload,
)


def _confirmed_anchor_timestamps(
    store: Any,
    route: Any,
    timeframe: str,
    timestamps: Sequence[datetime],
) -> frozenset[datetime]:
    if not timestamps:
        return frozenset()
    return read_provider_confirmed_bar_timestamps(
        route,
        timeframe,
        store=store,
        anchor_timestamps=list(timestamps),
    )


def normalize_drawing_anchors(
    store: Any,
    instrument_id: str,
    interval: str,
    drawings: list[dict[str, Any]],
    *,
    instrument: dict[str, Any],
    provider: str,
    route_fingerprint: str,
    require_resolved: bool = False,
) -> list[dict[str, Any]]:
    """Normalize durable anchors and attach a generation-bound runtime projection."""

    if not isinstance(drawings, list):
        raise ValueError("DRAWING_ROWS_INVALID: drawings must be a list")
    if not isinstance(require_resolved, bool):
        raise ValueError("require_resolved must be a boolean")
    route = route_instrument(instrument)
    if route.instrument_id != instrument_id:
        raise ValueError("drawing instrument identity mismatch")
    if not isinstance(route_fingerprint, str) or route.fingerprint != route_fingerprint:
        raise ValueError("drawing route fingerprint mismatch")
    if not isinstance(provider, str) or provider != route.provider:
        raise ValueError("drawing provider mismatch")

    normalized: list[dict[str, Any]] = []
    drawing_ids: set[str] = set()
    anchor_timestamps: list[datetime] = []
    point_count = 0
    point_records_by_id: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    for raw_item in drawings:
        if not isinstance(raw_item, dict):
            raise ValueError("DRAWING_ROW_INVALID: drawing must be an object")
        item = {
            key: value
            for key, value in raw_item.items()
            if key not in DRAWING_RUNTIME_GEOMETRY_FIELDS
            and key not in DRAWING_RETIRED_ALERT_FIELDS
        }
        drawing_id = item.get("id")
        if not isinstance(drawing_id, str) or not drawing_id:
            raise ValueError("DRAWING_FIELD_INVALID: id")
        if drawing_id in drawing_ids:
            raise ValueError(f"DRAWING_ID_DUPLICATE: {drawing_id}")
        drawing_ids.add(drawing_id)
        validate_durable_drawing_payload(item)
        raw_points = item.get("points")
        assert isinstance(raw_points, list)
        item["points"] = [dict(point) for point in raw_points]
        if item["type"] == "channel":
            item["offsetPoint"] = dict(item["offsetPoint"])
        records: list[tuple[dict[str, Any], Any]] = []
        for raw_point in drawing_payload_points(item):
            point = dict(raw_point)
            point_count += 1
            if point_count > MAX_CONFIRMED_BAR_PROJECTION_ANCHORS:
                raise ValueError(
                    "DRAWING_ANCHOR_LIMIT_EXCEEDED: "
                    f"{point_count} > {MAX_CONFIRMED_BAR_PROJECTION_ANCHORS}"
                )
            anchor = parse_drawing_point_anchor(point, drawing_id=drawing_id)
            anchor_timestamps.append(anchor.anchor_ts)
            records.append((point, anchor))
        point_records_by_id[drawing_id] = records
        normalized.append(item)

    projection_generation = chart_bars_updated_generation(
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    require_chart_bars_generation(
        projection_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    confirmed_timestamps = _confirmed_anchor_timestamps(
        store,
        route,
        interval,
        anchor_timestamps,
    )
    require_chart_bars_generation(
        projection_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    anchor_projections: dict[str, dict[str, Any]] = {}
    if confirmed_timestamps and not require_resolved:
        projection = read_provider_bar_logical_projection(
            route,
            interval,
            store=store,
            anchor_timestamps=list(confirmed_timestamps),
        )
        require_chart_bars_generation(
            projection_generation,
            interval,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        for item in normalized:
            drawing_id = item["id"]
            drawing_anchors = tuple(
                sorted(
                    {
                        anchor.anchor_ts
                        for _point, anchor in point_records_by_id[drawing_id]
                        if anchor.anchor_ts in confirmed_timestamps
                    }
                )
            )
            if not drawing_anchors:
                continue
            drawing_origin = drawing_anchors[0]
            logical_offset = projection.anchor_indices[drawing_origin]
            anchor_projections[drawing_id] = {
                "instrumentId": route.instrument_id,
                "routeFingerprint": route.fingerprint,
                "timeframe": interval,
                "canonicalGeneration": projection_generation,
                "originTs": drawing_origin.isoformat(),
                "confirmedThroughTs": projection.confirmed_through_ts.isoformat(),
                "confirmedCount": projection.confirmed_count - logical_offset,
                "anchors": [
                    {
                        "ts": timestamp.isoformat(),
                        "logicalIndex": (projection.anchor_indices[timestamp] - logical_offset),
                    }
                    for timestamp in drawing_anchors
                ],
            }
    for item in normalized:
        drawing_id = item["id"]
        records = point_records_by_id[drawing_id]
        unresolved: list[str] = []
        normalized_points: dict[int, dict[str, Any]] = {}
        for point, anchor in records:
            normalized_points[id(point)] = canonical_drawing_point(
                point,
                drawing_id=drawing_id,
            )
            if anchor.anchor_ts not in confirmed_timestamps:
                unresolved.append(
                    "relative_anchor_not_confirmed"
                    if anchor.kind == "relative"
                    else "confirmed_anchor_not_found"
                )
        item["points"] = [
            normalized_points[id(point)] for point, _anchor in records[: len(item["points"])]
        ]
        if item["type"] == "channel":
            item["offsetPoint"] = normalized_points[id(records[-1][0])]
        unique_reasons = sorted(set(unresolved))
        if unique_reasons and require_resolved:
            raise ValueError(f"DRAWING_ANCHOR_UNRESOLVED: {drawing_id} " + ",".join(unique_reasons))
        if not require_resolved:
            item["anchorResolution"] = {
                "status": "needs_history" if unique_reasons else "resolved",
                "reasons": unique_reasons,
            }
            if not unique_reasons and drawing_id in anchor_projections:
                item["anchorProjection"] = anchor_projections[drawing_id]
    require_chart_bars_generation(
        projection_generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    return normalized
