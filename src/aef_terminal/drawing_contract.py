from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping

from aef_terminal.runtime.timeframes import parse_aware_utc_ts


DRAWING_RUNTIME_GEOMETRY_FIELDS = frozenset(
    {
        "anchorResolution",
        "anchorProjection",
        "channelCompromised",
        "channelIntegrity",
        "geometryIntegrity",
    }
)

DRAWING_RETIRED_ALERT_FIELDS = frozenset(
    {
        "alertOnCross",
        "alertDeletedAt",
        "alertArmed",
        "alertFired",
        "alertCooldownUntil",
        "alertRearmedAt",
        "lastFiredAt",
        "lastFiredLevel",
        "lastTelegramStatus",
        "lastTelegramOk",
        "lastTelegramDetail",
        "telegramDeliveryStatus",
        "telegramPendingPayload",
        "telegramPendingSince",
        "telegramNextRetryAt",
        "telegramRetryCount",
        "telegramClaimedAt",
        "telegramLastAttemptAt",
        "telegramLastError",
    }
)
DRAWING_FORBIDDEN_DURABLE_FIELDS = frozenset(
    {
        "anchorCoverage",
        "offset",
        "offset_point",
        "opacity",
        "deletable",
        "delete_icon",
        "label_handle",
        "interactive",
    }
)
DRAWING_RETIRED_POINT_FIELDS = frozenset(
    {
        "bar_slot",
        "barSlot",
        "future",
        "slotAuthoritative",
        "optionTarget",
        "optionTargetAnchor",
        "snapped",
        "snapKind",
    }
)
DRAWING_POINT_COUNTS = {
    "line": 2,
    "zone": 2,
    "fib": 2,
    "channel": 2,
    "ellipse": 3,
    "text": 1,
}


@dataclass(frozen=True, slots=True)
class DrawingPointAnchor:
    kind: Literal["confirmed", "relative"]
    anchor_ts: datetime
    bar_offset: int


@dataclass(frozen=True, slots=True)
class DrawingAnchorProjection:
    instrument_id: str
    route_fingerprint: str
    timeframe: str
    canonical_generation: int
    origin_ts: datetime
    confirmed_through_ts: datetime
    confirmed_count: int
    anchors: tuple[tuple[datetime, int], ...]

    @property
    def anchor_indices(self) -> dict[datetime, int]:
        return dict(self.anchors)


def _drawing_point_error(drawing_id: str, field: str) -> ValueError:
    prefix = f"{drawing_id} " if drawing_id else ""
    return ValueError(f"DRAWING_FIELD_INVALID: {prefix}{field}")


def drawing_point_price(point: Mapping[str, Any], *, drawing_id: str = "") -> float:
    value = point.get("price")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _drawing_point_error(drawing_id, "price")
    return float(value)


def parse_drawing_point_anchor(
    point: Mapping[str, Any],
    *,
    drawing_id: str = "",
) -> DrawingPointAnchor:
    """Parse the durable drawing-point anchor union without resolving storage."""

    if not isinstance(point, Mapping):
        raise _drawing_point_error(drawing_id, "point")
    has_ts = "ts" in point
    has_anchor_ts = "anchorTs" in point
    has_bar_offset = "barOffset" in point
    if has_ts == (has_anchor_ts or has_bar_offset):
        raise _drawing_point_error(drawing_id, "anchor")
    if has_ts:
        raw_ts = point.get("ts")
        ts = parse_aware_utc_ts(raw_ts) if isinstance(raw_ts, str) else None
        if ts is None:
            raise _drawing_point_error(drawing_id, "ts")
        return DrawingPointAnchor("confirmed", ts, 0)
    if not has_anchor_ts or not has_bar_offset:
        raise _drawing_point_error(drawing_id, "anchor")
    raw_anchor_ts = point.get("anchorTs")
    anchor_ts = parse_aware_utc_ts(raw_anchor_ts) if isinstance(raw_anchor_ts, str) else None
    bar_offset = point.get("barOffset")
    if anchor_ts is None:
        raise _drawing_point_error(drawing_id, "anchorTs")
    if isinstance(bar_offset, bool) or not isinstance(bar_offset, int) or bar_offset <= 0:
        raise _drawing_point_error(drawing_id, "barOffset")
    return DrawingPointAnchor("relative", anchor_ts, bar_offset)


def canonical_drawing_point(
    point: Mapping[str, Any],
    *,
    drawing_id: str = "",
) -> dict[str, Any]:
    """Return only the durable anchor union and price fields."""

    anchor = parse_drawing_point_anchor(point, drawing_id=drawing_id)
    price = drawing_point_price(point, drawing_id=drawing_id)
    if anchor.kind == "confirmed":
        return {"ts": anchor.anchor_ts.isoformat(), "price": price}
    return {
        "anchorTs": anchor.anchor_ts.isoformat(),
        "barOffset": anchor.bar_offset,
        "price": price,
    }


def drawing_payload_points(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return every durable point, including a channel's offset point."""

    points = item.get("points")
    result: list[Mapping[str, Any]] = list(points) if isinstance(points, list) else []
    if item.get("type") == "channel":
        offset_point = item.get("offsetPoint")
        if isinstance(offset_point, Mapping):
            result.append(offset_point)
    return result


def validate_durable_drawing_payload(item: Mapping[str, Any]) -> None:
    """Validate one source-agnostic durable drawing object without storage reads."""

    if not isinstance(item, Mapping):
        raise ValueError("DRAWING_ROW_INVALID: drawing must be an object")
    forbidden = sorted(
        (
            DRAWING_FORBIDDEN_DURABLE_FIELDS
            | DRAWING_RUNTIME_GEOMETRY_FIELDS
            | DRAWING_RETIRED_ALERT_FIELDS
        ).intersection(item)
    )
    if forbidden:
        raise ValueError(f"DRAWING_FIELD_FORBIDDEN: {forbidden[0]}")
    drawing_id = item.get("id")
    if not isinstance(drawing_id, str) or not drawing_id:
        raise ValueError("DRAWING_FIELD_INVALID: id")
    kind = item.get("type")
    if kind not in {*DRAWING_POINT_COUNTS, "path"}:
        raise ValueError(f"DRAWING_FIELD_INVALID: {drawing_id} type")
    if "lineVariant" in item:
        if item["lineVariant"] != "ruler":
            raise ValueError(f"DRAWING_FIELD_INVALID: {drawing_id} lineVariant")
        if kind != "line":
            raise ValueError(f"DRAWING_FIELD_FORBIDDEN: {drawing_id} lineVariant")
        if item.get("extendRight") is not False:
            raise ValueError(f"DRAWING_FIELD_INVALID: {drawing_id} extendRight")
    raw_points = item.get("points")
    expected_count = DRAWING_POINT_COUNTS.get(kind)
    if (
        not isinstance(raw_points, list)
        or (expected_count is not None and len(raw_points) != expected_count)
        or (kind == "path" and len(raw_points) < 2)
        or any(not isinstance(point, Mapping) for point in raw_points)
    ):
        raise ValueError(f"DRAWING_FIELD_INVALID: {drawing_id} points")
    if kind == "channel" and not isinstance(item.get("offsetPoint"), Mapping):
        raise ValueError(f"DRAWING_FIELD_INVALID: {drawing_id} offsetPoint")
    if kind != "channel" and "offsetPoint" in item:
        raise ValueError(f"DRAWING_FIELD_FORBIDDEN: {drawing_id} offsetPoint")
    for point in drawing_payload_points(item):
        point_forbidden = sorted(DRAWING_RETIRED_POINT_FIELDS.intersection(point))
        if point_forbidden:
            raise ValueError(f"DRAWING_FIELD_FORBIDDEN: {point_forbidden[0]}")
        canonical = canonical_drawing_point(point, drawing_id=drawing_id)
        extra_point_fields = sorted(set(point) - set(canonical))
        if extra_point_fields:
            raise ValueError(f"DRAWING_FIELD_FORBIDDEN: {extra_point_fields[0]}")


def parse_drawing_anchor_projection(
    value: Any,
) -> DrawingAnchorProjection:
    """Parse the transient, generation-bound confirmed-bar projection."""

    if not isinstance(value, Mapping):
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: object")
    instrument_id = value.get("instrumentId")
    route_fingerprint = value.get("routeFingerprint")
    timeframe = value.get("timeframe")
    canonical_generation = value.get("canonicalGeneration")
    confirmed_count = value.get("confirmedCount")
    if not isinstance(instrument_id, str) or not instrument_id:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: instrumentId")
    if not isinstance(route_fingerprint, str) or not route_fingerprint:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: routeFingerprint")
    if not isinstance(timeframe, str) or not timeframe:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: timeframe")
    if (
        isinstance(canonical_generation, bool)
        or not isinstance(canonical_generation, int)
        or canonical_generation < 0
    ):
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: canonicalGeneration")
    if (
        isinstance(confirmed_count, bool)
        or not isinstance(confirmed_count, int)
        or confirmed_count <= 0
    ):
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: confirmedCount")
    origin_ts = parse_aware_utc_ts(value.get("originTs"))
    confirmed_through_ts = parse_aware_utc_ts(value.get("confirmedThroughTs"))
    if origin_ts is None:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: originTs")
    if confirmed_through_ts is None or confirmed_through_ts < origin_ts:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: confirmedThroughTs")
    raw_anchors = value.get("anchors")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: anchors")
    anchors: list[tuple[datetime, int]] = []
    seen_timestamps: set[datetime] = set()
    seen_indices: set[int] = set()
    for raw_anchor in raw_anchors:
        if not isinstance(raw_anchor, Mapping):
            raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: anchor")
        timestamp = parse_aware_utc_ts(raw_anchor.get("ts"))
        logical_index = raw_anchor.get("logicalIndex")
        if timestamp is None or timestamp in seen_timestamps:
            raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: anchor.ts")
        if (
            isinstance(logical_index, bool)
            or not isinstance(logical_index, int)
            or logical_index < 0
            or logical_index >= confirmed_count
            or logical_index in seen_indices
        ):
            raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: anchor.logicalIndex")
        seen_timestamps.add(timestamp)
        seen_indices.add(logical_index)
        anchors.append((timestamp, logical_index))
    if (
        anchors != sorted(anchors)
        or anchors[0] != (origin_ts, 0)
        or anchors[-1][0] > confirmed_through_ts
        or any(
            current_index <= previous_index
            for (_previous_ts, previous_index), (_current_ts, current_index) in zip(
                anchors,
                anchors[1:],
                strict=False,
            )
        )
    ):
        raise ValueError("DRAWING_ANCHOR_PROJECTION_INVALID: anchor order")
    return DrawingAnchorProjection(
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
        canonical_generation=canonical_generation,
        origin_ts=origin_ts,
        confirmed_through_ts=confirmed_through_ts,
        confirmed_count=confirmed_count,
        anchors=tuple(anchors),
    )
