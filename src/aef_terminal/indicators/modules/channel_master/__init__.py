from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.drawing_contract import (
    DrawingAnchorProjection,
    drawing_point_price,
    parse_drawing_anchor_projection,
    parse_drawing_point_anchor,
)
from aef_terminal.domain import (
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
)
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
)
from aef_terminal.runtime import pine
from aef_terminal.runtime.timeframes import expiry_after_bars
from aef_terminal.signals.trade_plan import trade_plan_quality

CHANNEL_LEVELS = (0.0, 0.25, 0.5, 0.75, 1.0)
CHANNEL_GHOST_COPIES_DEFAULT = 3
CHANNEL_GHOST_COPIES_MAX = 10

_LEVEL_FIELDS = (
    "acceptance",
    "accepted_bars",
    "bounce_score",
    "bounce_target",
    "break_score",
    "breakout_target",
    "channel_coord",
    "channel_id",
    "coord_distance",
    "decision_price",
    "direction",
    "distance",
    "distance_atr",
    "ghost",
    "ghost_copy",
    "impulse_atr",
    "inside",
    "label",
    "level",
    "near",
    "no_fade",
    "position_pct",
    "price",
    "primary_path",
    "raw_break_score",
    "reclaim",
    "role",
    "score",
    "side",
    "target",
    "thin_impulse_probe",
    "touch",
    "trend_with_break",
    "volume_ratio",
    "wick_reject",
)

_CHANNEL_FIELDS = (
    "base",
    "color",
    "definition_revision",
    "ghost_copies",
    "ghost_direction",
    "id",
    "impulse_strength",
    "inside",
    "levels",
    "lower",
    "offset",
    "position_pct",
    "slope_per_confirmed_bar",
    "upper",
)

INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "channel_master",
        "Channel Master",
        "Confirmed-bar analysis of user-drawn channel levels, break/bounce paths and reaction candidates",
        {
            "version": "3.2-decision-anchor",
            "active": False,
            "count": 0,
            "analysis_logical_index": None,
            "analysis_ts": None,
            "decision_anchor_ts": None,
            "decision_bar_offset": None,
            "decision_available_at": None,
            "price": None,
            "channels": [],
            "nearest": None,
            "interaction_model": {
                "state": "unavailable",
                "reason_code": "channel_inactive",
            },
            "features": {},
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
        },
        "channelMaster",
        "aef_terminal.indicators.modules.channel_master:channel_master_indicator",
        group="service",
        pipeline_stage="primary",
        pipeline_order=94,
        signal_source="channel_master",
        candidate_promoter="channel_master",
        paper_tradable=False,
        custom_renderer=True,
        renderer_kind="service",
        renderer_ref="channel_master_ui",
        renderer_primitives=("line", "label"),
        renderer_placements=("price", "settings"),
        service_renderer_requirements=("settings_ui", "drawing_decorator"),
        overlay_collect=False,
        input_contract="confirmed-bars+canonical-drawing-anchors",
        output_contract="channel-master-v3",
        score_family="structure",
        empirical_power=0.75,
        usefulness=0.95,
        notes=(
            "Owns deterministic manual-channel analytics on ordered confirmed-bar "
            "logical indices and candidate promotion. The browser only renders typed "
            "backend facts; the one-minute ML shadow remains fail closed until an exact "
            "cross-timeframe logical mapping is owned."
        ),
        module_type="context",
        state_key="channelMaster",
        calc_key="channelMasterCalcEnabled",
        visible_key="channelMasterCalcEnabled",
        chart_control_id="channel-master-process",
        process_control_id="channel-master-process",
        api_enabled_key="channel_master_enabled",
        manager_order=75,
        runtime_order=75,
        default_calc=False,
        default_visible=True,
        show_in_runtime=False,
        runtime_payload_contract={
            "top_level": (
                "active",
                "analysis_logical_index",
                "analysis_ts",
                "availability",
                "candidate_promotion",
                "channels",
                "count",
                "decision_anchor_ts",
                "decision_available_at",
                "decision_bar_offset",
                "features",
                "interaction_model",
                "nearest",
                "price",
                "research_observations",
                "version",
            ),
            "latest": _LEVEL_FIELDS,
            "channels": _CHANNEL_FIELDS,
        },
        runtime_payload_compact={
            "research_observations": {
                "drop": True,
            },
        },
        controls=(
            _control(
                "marks",
                "Marks",
                "toggle",
                True,
                "channelMasterMarks",
                element_id="channel-master-marks",
                action="render",
            ),
            _control(
                "tooltips",
                "Tips",
                "toggle",
                True,
                "channelMasterTooltips",
                element_id="channel-master-tooltips",
                action="render",
            ),
            _control(
                "arrows",
                "Arrows",
                "toggle",
                True,
                "channelMasterArrows",
                element_id="channel-master-arrows",
                action="render",
            ),
        ),
    ),
    adapter_ref=("aef_terminal.indicators.modules.channel_master:build_execution_spec"),
    candidate_promoter_ref=(
        "aef_terminal.indicators.modules.channel_master:signal_candidates_from_channel_master"
    ),
    ui_js_assets=("client.js",),
)


def _number(value: object, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        result = float(value)
    except TypeError, ValueError:
        return default
    return result if result == result else default


@dataclass(frozen=True, slots=True)
class _DrawingPoint:
    anchor_ts: datetime
    bar_offset: int
    price: float
    durable_identity: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ResolvedChannelGeometry:
    first_index: int
    first_price: float
    second_index: int
    second_price: float
    offset_index: int
    offset_price: float
    bar_indices: tuple[int, ...]

    @property
    def slope_per_confirmed_bar(self) -> float:
        return (self.second_price - self.first_price) / (self.second_index - self.first_index)

    def base_at(self, logical_index: int) -> float:
        return self.first_price + self.slope_per_confirmed_bar * (logical_index - self.first_index)

    @property
    def offset(self) -> float:
        return self.offset_price - self.base_at(self.offset_index)


def _drawing_point(point: Mapping[str, Any]) -> _DrawingPoint | None:
    try:
        anchor = parse_drawing_point_anchor(point)
        price = drawing_point_price(point)
    except ValueError:
        return None
    durable_identity = (
        {"ts": anchor.anchor_ts.isoformat(), "price": price}
        if anchor.kind == "confirmed"
        else {
            "anchorTs": anchor.anchor_ts.isoformat(),
            "barOffset": anchor.bar_offset,
            "price": price,
        }
    )
    return _DrawingPoint(
        anchor_ts=anchor.anchor_ts,
        bar_offset=anchor.bar_offset,
        price=price,
        durable_identity=durable_identity,
    )


def _anchor_projection(
    channel: Mapping[str, Any],
    *,
    timeframe: str,
    instrument_id: str,
    route_fingerprint: str,
) -> tuple[DrawingAnchorProjection | None, str | None]:
    resolution = channel.get("anchorResolution")
    if isinstance(resolution, Mapping) and resolution.get("status") != "resolved":
        return None, "drawing_anchor_resolution_pending"
    raw = channel.get("anchorProjection")
    if raw is None:
        return None, None
    try:
        projection = parse_drawing_anchor_projection(raw)
    except ValueError:
        return None, "drawing_anchor_projection_invalid"
    if (
        projection.timeframe != timeframe
        or (instrument_id and projection.instrument_id != instrument_id)
        or (route_fingerprint and projection.route_fingerprint != route_fingerprint)
    ):
        return None, "drawing_anchor_projection_identity_mismatch"
    if (
        projection.confirmed_through_ts in projection.anchor_indices
        and projection.anchor_indices[projection.confirmed_through_ts]
        != projection.confirmed_count - 1
    ):
        return None, "drawing_anchor_projection_invalid"
    return projection, None


def _resolve_channel_geometry(
    channel: Mapping[str, Any],
    bars: Sequence[Bar],
    *,
    instrument_id: str,
    route_fingerprint: str,
) -> tuple[_ResolvedChannelGeometry | None, str | None]:
    if not bars:
        return None, "confirmed_bar_series_unavailable"
    timestamps = tuple(bar.ts.astimezone(UTC) for bar in bars)
    if (
        len(set(timestamps)) != len(timestamps)
        or list(timestamps) != sorted(timestamps)
        or any(not bar.closed or bar.state.value != "confirmed" for bar in bars)
    ):
        return None, "confirmed_bar_series_invalid"
    points = channel.get("points")
    offset_point = channel.get("offsetPoint")
    if (
        not isinstance(points, Sequence)
        or isinstance(points, (str, bytes))
        or len(points) != 2
        or not isinstance(points[0], Mapping)
        or not isinstance(points[1], Mapping)
        or not isinstance(offset_point, Mapping)
    ):
        return None, "channel_geometry_invalid"
    durable_points = (
        _drawing_point(points[0]),
        _drawing_point(points[1]),
        _drawing_point(offset_point),
    )
    if any(point is None for point in durable_points):
        return None, "drawing_anchor_invalid"
    first, second, offset = durable_points
    assert first is not None and second is not None and offset is not None
    local_indices = {timestamp: index for index, timestamp in enumerate(timestamps)}
    projection, projection_error = _anchor_projection(
        channel,
        timeframe=bars[-1].timeframe,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )
    if projection_error is not None:
        return None, projection_error
    if projection is None:
        return None, "drawing_anchor_projection_unavailable"
    references = (
        (projection.confirmed_through_ts, projection.confirmed_count - 1),
        *projection.anchors,
    )
    projection_offsets = {
        logical_index - local_indices[timestamp]
        for timestamp, logical_index in references
        if timestamp in local_indices
    }
    if not projection_offsets:
        return None, "drawing_anchor_projection_unavailable"
    if len(projection_offsets) != 1:
        return None, "drawing_anchor_projection_invalid"
    projection_offset = next(iter(projection_offsets))
    bar_indices = tuple(projection_offset + index for index in range(len(bars)))
    projected_local = {timestamp: bar_indices[index] for timestamp, index in local_indices.items()}
    if any(
        projected_local[timestamp] != logical_index
        for timestamp, logical_index in projection.anchor_indices.items()
        if timestamp in projected_local
    ):
        return None, "drawing_anchor_projection_invalid"
    index_by_ts = {
        **projection.anchor_indices,
        **projected_local,
    }
    resolved_indices: list[int] = []
    for point in (first, second, offset):
        base_index = index_by_ts.get(point.anchor_ts)
        if base_index is None:
            return None, "drawing_anchor_unresolved"
        resolved_indices.append(base_index + point.bar_offset)
    if resolved_indices[0] == resolved_indices[1]:
        return None, "channel_primary_anchor_collision"
    geometry = _ResolvedChannelGeometry(
        first_index=resolved_indices[0],
        first_price=first.price,
        second_index=resolved_indices[1],
        second_price=second.price,
        offset_index=resolved_indices[2],
        offset_price=offset.price,
        bar_indices=bar_indices,
    )
    if not math.isfinite(geometry.offset) or abs(geometry.offset) < 1e-9:
        return None, "channel_offset_invalid"
    return geometry, None


def _level_role(level: float, offset: float) -> tuple[str, str]:
    physical = level if offset >= 0 else 1.0 - level
    if abs(physical) <= 0.001:
        return "lower_edge", "LOW"
    if abs(physical - 1.0) <= 0.001:
        return "upper_edge", "HIGH"
    if abs(physical - 0.5) <= 0.001:
        return "midline", "MID"
    if abs(physical - 0.25) <= 0.001:
        return "lower_quarter", "1/4"
    if abs(physical - 0.75) <= 0.001:
        return "upper_quarter", "3/4"
    if physical < 0:
        return "ghost_low", f"G{physical:g}"
    return "ghost_high", f"G{physical:g}"


def _channel_definition_revision(channel: Mapping[str, Any]) -> str:
    """Hash only the durable anchor union and level-generation settings."""

    raw_points = channel.get("points")
    points = (
        [_drawing_point(point) for point in raw_points]
        if isinstance(raw_points, Sequence)
        and not isinstance(raw_points, (str, bytes))
        and all(isinstance(point, Mapping) for point in raw_points)
        else []
    )
    offset_point = channel.get("offsetPoint")
    parsed_offset = _drawing_point(offset_point) if isinstance(offset_point, Mapping) else None
    if not points or any(point is None for point in points) or parsed_offset is None:
        raise ValueError("channel revision requires canonical durable anchors")
    canonical = {
        "id": str(channel.get("id") or ""),
        "type": str(channel.get("type") or ""),
        "points": [point.durable_identity for point in points if point is not None],
        "offset_point": parsed_offset.durable_identity,
        "show_guides": channel.get("showGuides") is not False,
        "ghost_copies": int(
            round(
                _number(
                    channel.get("ghostCopies", channel.get("ghost_copies")),
                    float(CHANNEL_GHOST_COPIES_DEFAULT),
                )
                or 0.0
            )
        ),
        "extend_right": channel.get("extendRight") is not False,
    }
    encoded = json.dumps(
        canonical,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _blocked_interaction(
    context: Mapping[str, Any],
    reason_code: str,
) -> dict[str, Any]:
    nearest = context.get("nearest")
    payload: dict[str, Any] = {
        "state": "blocked",
        "reason_code": reason_code,
        "phase": "pre_touch",
    }
    if isinstance(nearest, Mapping):
        payload.update(
            {
                "channel_id": str(nearest.get("channel_id") or ""),
                "level": nearest.get("level"),
                "level_price": nearest.get("price"),
                "decision_logical_index": context.get("analysis_logical_index"),
                "near": bool(nearest.get("near")),
            }
        )
    return payload


def _channel_interaction_payload(
    context: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fail closed until an exact logical-parent-to-1m mapping is owned."""

    if not context.get("active") or not isinstance(context.get("nearest"), Mapping):
        return _blocked_interaction(context, "channel_inactive"), []
    blocked = _blocked_interaction(
        context,
        "parent_logical_channel_1m_projection_unavailable",
    )
    blocked["required_context"] = "exact_parent_confirmed_index_to_confirmed_1m_mapping"
    return blocked, []


def _direction_for_level(role: str, latest: Bar, price: float) -> Direction:
    if role in {"lower_edge", "lower_quarter"}:
        return Direction.LONG
    if role in {"upper_edge", "upper_quarter"}:
        return Direction.SHORT
    if latest.close > price and latest.close >= latest.open:
        return Direction.LONG
    if latest.close < price and latest.close <= latest.open:
        return Direction.SHORT
    return Direction.FLAT


def _next_target(
    levels: Sequence[dict[str, Any]],
    nearest: dict[str, Any],
    direction: Direction,
    impulse_strength: float = 0.0,
) -> float | None:
    current_level = _number(nearest.get("level"))
    if current_level is None:
        return None
    ordered = sorted(levels, key=lambda item: float(item.get("price", 0.0)))
    current_price = _number(nearest.get("price"))
    if current_price is None:
        return None

    # Если импульс сильный, прыгаем через уровни (значительно выше/ниже)
    skip = 2 if impulse_strength > 1.15 else 1 if impulse_strength > 0.65 else 0

    if direction == Direction.LONG:
        higher = [
            item
            for item in ordered
            if float(item.get("price", current_price)) > current_price + 1e-7
        ]
        if not higher or len(higher) == 0:
            return None
        idx = min(skip, len(higher) - 1)
        return float(higher[idx]["price"])
    if direction == Direction.SHORT:
        lower = [
            item
            for item in ordered
            if float(item.get("price", current_price)) < current_price - 1e-7
        ]
        if not lower or len(lower) == 0:
            return None
        idx = max(0, len(lower) - 1 - skip)
        return float(lower[idx]["price"])
    return None


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = channel_master_indicator
    raw_canonical_generation = ctx.indicator_params.get("manual_channel_canonical_generation")
    canonical_generation = (
        raw_canonical_generation
        if not isinstance(raw_canonical_generation, bool)
        and isinstance(raw_canonical_generation, int)
        and raw_canonical_generation >= 0
        else None
    )
    return IndicatorExecutionSpec(
        id="channel_master",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=lambda: calculate(
            ctx.confirmed_bars,
            ctx.manual_channel_drawings,
            atr_value=ctx.atr_value,
            features=ctx.features,
            feature_context=ctx.feature_context,
            vwap_session=ctx.vwap_session,
            instrument_id=ctx.instrument_id,
            route_fingerprint=ctx.route_fingerprint,
            canonical_generation=canonical_generation,
        ),
        runtime_params=ctx.runtime_params,
    )


def manual_channel_context(
    bars: Sequence[Bar],
    drawings: Sequence[Mapping[str, Any]] | None,
    *,
    atr_value: float | None = None,
    impulse_strength: float | None = None,
    ema_value: float | None = None,
    vwap_value: float | None = None,
    instrument_id: str = "",
    route_fingerprint: str = "",
    canonical_generation: int | None = None,
    proximity_atr: float = 0.16,
) -> dict[str, Any]:
    """Evaluate canonical drawings on ordered confirmed-bar logical indices."""

    if not bars or not drawings:
        return {
            "active": False,
            "count": 0,
            "analysis_logical_index": None,
            "analysis_ts": None,
            "decision_anchor_ts": None,
            "decision_bar_offset": None,
            "decision_available_at": None,
            "price": None,
            "channels": [],
            "nearest": None,
            "features": {
                "manual_channel_active": False,
                "manual_channel_count": 0,
            },
        }
    latest = bars[-1]
    analysis_ts = latest.ts.isoformat()
    decision_available_at = expiry_after_bars(latest.ts, latest.timeframe, 1).isoformat()
    decision_bar_offset = 1
    atr_safe = max(float(atr_value or 0.0), max(latest.high - latest.low, 0.0), 1e-9)
    proximity_price = atr_safe * max(proximity_atr, 0.01)
    shared_impulse_strength = max(float(impulse_strength or 0.0), 0.0)
    channels: list[dict[str, Any]] = []
    nearest: dict[str, Any] | None = None
    blocked_channels: list[dict[str, str]] = []
    # Response-level analysis coordinates belong to the current confirmed input
    # series.  Each drawing projection is independently rebased to its own
    # earliest anchor, so its latest logical index is channel-local and cannot
    # be compared across drawings.
    analysis_logical_index = len(bars) - 1

    for channel in drawings:
        if not isinstance(channel, Mapping):
            continue
        if channel.get("type") != "channel":
            continue
        geometry, geometry_error = _resolve_channel_geometry(
            channel,
            bars,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
        )
        if geometry is None:
            blocked_channels.append(
                {
                    "channel_id": str(channel.get("id") or ""),
                    "reason_code": geometry_error or "channel_geometry_unavailable",
                }
            )
            continue
        try:
            definition_revision = _channel_definition_revision(channel)
        except OverflowError, ValueError:
            blocked_channels.append(
                {
                    "channel_id": str(channel.get("id") or ""),
                    "reason_code": "channel_definition_revision_invalid",
                }
            )
            continue
        latest_logical_index = geometry.bar_indices[-1]
        offset = geometry.offset
        base_price = geometry.base_at(latest_logical_index)
        slope_per_confirmed_bar = geometry.slope_per_confirmed_bar

        base_levels = CHANNEL_LEVELS if channel.get("showGuides") is not False else (0.0, 1.0)
        ghost_copies_value = _number(
            channel.get("ghostCopies", channel.get("ghost_copies")),
            float(CHANNEL_GHOST_COPIES_DEFAULT),
        )
        ghost_copies = min(
            max(int(round(ghost_copies_value or 0.0)), 0),
            CHANNEL_GHOST_COPIES_MAX,
        )
        level_rows: list[dict[str, Any]] = []
        edge_a = base_price
        edge_b = base_price + offset
        lower = min(edge_a, edge_b)
        upper = max(edge_a, edge_b)
        width = max(upper - lower, 1e-9)
        inside = lower <= latest.close <= upper
        position_pct = (latest.close - lower) / width
        ghost_direction = (
            1
            if latest.close > upper
            else -1
            if latest.close < lower
            else 1
            if latest.close >= (upper + lower) * 0.5
            else -1
        )
        ghost_level_step = ghost_direction * (1 if offset >= 0 else -1)
        level_specs: dict[float, tuple[float, int]] = {
            round(level, 9): (level, 0) for level in base_levels
        }
        for ghost_copy in range(1, ghost_copies + 1):
            for base_level in base_levels:
                effective_level = base_level + ghost_level_step * ghost_copy
                level_specs.setdefault(
                    round(effective_level, 9),
                    (effective_level, ghost_copy),
                )

        for level, ghost_copy in sorted(level_specs.values()):
            price = base_price + offset * level
            decision_price = price + slope_per_confirmed_bar * decision_bar_offset
            role, label = _level_role(level, offset)
            distance = latest.close - price
            if "ghost" in role:
                label = f"PROJ {label}"
            abs_distance = abs(distance)
            level_crossed = latest.low <= price <= latest.high
            touch = level_crossed or abs_distance <= proximity_price
            channel_coord = (latest.close - base_price) / offset
            coord_distance = channel_coord - level
            direction_to_line = -1.0 if coord_distance >= 0 else 1.0

            approach_index = len(bars) - 2 if level_crossed and len(bars) >= 3 else len(bars) - 1
            approach_previous_index = max(0, approach_index - 1)
            approach_lookback_index = max(0, approach_index - 4)
            approach_bar = bars[approach_index]
            approach_previous = bars[approach_previous_index]
            approach_lookback = bars[approach_lookback_index]
            approach_base = geometry.base_at(geometry.bar_indices[approach_index])
            approach_previous_base = geometry.base_at(geometry.bar_indices[approach_previous_index])
            approach_price = approach_base + offset * level
            approach_previous_price = approach_previous_base + offset * level
            approach_coord = (approach_bar.close - approach_base) / offset
            approach_previous_coord = (approach_previous.close - approach_previous_base) / offset
            approach_coord_distance = approach_coord - level
            approach_direction_to_line = -1.0 if approach_coord_distance >= 0 else 1.0
            approach_abs_distance = abs(approach_bar.close - approach_price)
            approach_previous_abs_distance = abs(approach_previous.close - approach_previous_price)
            approach_progress_atr = (
                approach_previous_abs_distance - approach_abs_distance
            ) / atr_safe
            impulse_atr = (approach_bar.close - approach_lookback.close) / atr_safe
            approach_impulse = min(abs(impulse_atr), 3.0)
            moving_toward = (
                1.0
                if approach_coord - approach_previous_coord > 0
                else -1.0
                if approach_coord - approach_previous_coord < 0
                else 0.0
            ) == approach_direction_to_line
            recent = bars[-24:]
            average_volume = sum(bar.volume for bar in recent) / len(recent) if recent else 0.0
            approach_recent = bars[max(0, approach_index - 23) : approach_index + 1]
            approach_average_volume = (
                sum(bar.volume for bar in approach_recent) / len(approach_recent)
                if approach_recent
                else 0.0
            )
            volume_ratio = (
                approach_bar.volume / approach_average_volume
                if approach_average_volume > 0
                else 1.0
            )
            touch_volume_ratio = latest.volume / average_volume if average_volume > 0 else 1.0
            low_coord = (latest.low - base_price) / offset
            high_coord = (latest.high - base_price) / offset
            coord_low = min(low_coord, high_coord)
            coord_high = max(low_coord, high_coord)
            crossed_up = coord_low <= level and channel_coord > level
            crossed_down = coord_high >= level and channel_coord < level
            wick_reject = (coord_distance > 0 and crossed_up) or (
                coord_distance < 0 and crossed_down
            )
            accepted_bars = 0
            for bar, logical_index in zip(
                bars[-3:],
                geometry.bar_indices[-3:],
                strict=True,
            ):
                accepted_base = geometry.base_at(logical_index)
                accepted_coord = (bar.close - accepted_base) / offset
                if (accepted_coord - level) * approach_direction_to_line > 0:
                    accepted_bars += 1
            acceptance = (
                accepted_bars >= 2
                and abs_distance / atr_safe >= 0.12
                and touch_volume_ratio >= 0.55
            )
            thin_impulse_probe = volume_ratio < 0.55 and approach_impulse >= 1.35
            breakout_target = base_price + offset * (level + direction_to_line * 0.25)
            bounce_target = base_price + offset * (level - direction_to_line * 0.25)
            break_points_up = breakout_target > latest.close
            trend_with_break = (
                break_points_up
                and (
                    ema_value is not None
                    and latest.close >= ema_value
                    or vwap_value is not None
                    and latest.close >= vwap_value
                )
            ) or (
                not break_points_up
                and (
                    ema_value is not None
                    and latest.close <= ema_value
                    or vwap_value is not None
                    and latest.close <= vwap_value
                )
            )
            approach_break_points_up = approach_direction_to_line > 0
            approach_trend_with_break = (
                approach_bar.close >= approach_lookback.close
                if approach_break_points_up
                else approach_bar.close <= approach_lookback.close
            )
            raw_break_score = 34.0
            raw_break_score += 12.0 if moving_toward else -8.0
            raw_break_score += approach_impulse * 5.0
            raw_break_score += (
                12.0
                if approach_progress_atr >= 0.35
                else 6.0
                if approach_progress_atr >= 0.15
                else -8.0
                if approach_progress_atr <= -0.10
                else 0.0
            )
            raw_break_score += (
                14.0
                if volume_ratio >= 1.35
                else 5.0
                if volume_ratio >= 1.05
                else -18.0
                if volume_ratio <= 0.55
                else -10.0
                if volume_ratio <= 0.75
                else 0.0
            )
            raw_break_score += 10.0 if approach_trend_with_break else -4.0
            raw_break_score += (
                6.0
                if approach_abs_distance / atr_safe <= 0.25
                else 3.0
                if approach_abs_distance / atr_safe <= 0.50
                else -6.0
                if approach_abs_distance / atr_safe > 1.0
                else 0.0
            )
            raw_break_score = min(max(round(raw_break_score), 5), 95)

            break_score = float(raw_break_score)
            break_score += 12.0 if acceptance else 0.0
            break_score += -16.0 if thin_impulse_probe else 0.0
            break_score += -18.0 if wick_reject else 0.0
            break_score += 8.0 if trend_with_break else -4.0
            break_score += 4.0 if touch else -6.0
            break_score += (
                6.0
                if touch_volume_ratio >= 1.35 and not wick_reject
                else -4.0
                if touch_volume_ratio <= 0.55
                else 0.0
            )
            break_score = min(max(round(break_score), 5), 95)
            reclaim = (
                role in {"lower_edge", "lower_quarter", "midline"}
                and latest.low <= price
                and latest.close > price
            ) or (
                role in {"upper_edge", "upper_quarter", "midline"}
                and latest.high >= price
                and latest.close < price
            )
            primary_path = "break" if break_score >= 50 else "bounce"
            no_fade = bool(touch and raw_break_score >= 70 and not wick_reject and not reclaim)
            direction = _direction_for_level(role, latest, price)
            score = 52.0
            if role.endswith("edge"):
                score += 8.0
            elif "ghost" in role:
                score += 3.0  # Призрачные уровни имеют чуть меньший базовый вес
            elif "quarter" in role:
                score += 5.0
            else:
                score += 4.0
            if touch:
                score += 8.0
            if reclaim:
                score += 7.0
            if inside:
                score += 3.0
            if abs_distance / atr_safe <= 0.05:
                score += 4.0
            row = {
                "channel_id": str(channel.get("id") or ""),
                "level": level,
                "ghost_copy": ghost_copy,
                "ghost": ghost_copy > 0,
                "label": label,
                "role": role,
                "price": price,
                "decision_price": decision_price,
                "distance": distance,
                "distance_atr": abs_distance / atr_safe,
                "side": "above" if distance > 0 else "below" if distance < 0 else "on",
                "touch": touch,
                "near": touch,
                "reclaim": reclaim,
                "inside": inside,
                "position_pct": position_pct,
                "direction": direction.value,
                "score": min(score, 76.0),
                "break_score": break_score,
                "bounce_score": 100 - break_score,
                "primary_path": primary_path,
                "no_fade": no_fade,
                "raw_break_score": raw_break_score,
                "impulse_atr": impulse_atr,
                "volume_ratio": volume_ratio,
                "wick_reject": wick_reject,
                "acceptance": acceptance,
                "accepted_bars": accepted_bars,
                "thin_impulse_probe": thin_impulse_probe,
                "trend_with_break": trend_with_break,
                "channel_coord": channel_coord,
                "coord_distance": coord_distance,
                "breakout_target": breakout_target,
                "bounce_target": bounce_target,
            }
            level_rows.append(row)
            if nearest is None or row["distance_atr"] < nearest["distance_atr"]:
                nearest = row

        if nearest and nearest.get("channel_id") == str(channel.get("id") or ""):
            target = _next_target(
                level_rows, nearest, Direction(nearest["direction"]), shared_impulse_strength
            )
            nearest["target"] = target

        manual_color = channel.get("color")

        channels.append(
            {
                "id": str(channel.get("id") or ""),
                "definition_revision": definition_revision,
                "levels": level_rows,
                "inside": inside,
                "position_pct": position_pct,
                "lower": lower,
                "upper": upper,
                "base": base_price,
                "offset": offset,
                "ghost_copies": ghost_copies,
                "ghost_direction": ghost_direction,
                "slope_per_confirmed_bar": slope_per_confirmed_bar,
                "color": manual_color,
                "impulse_strength": round(shared_impulse_strength, 2),
            }
        )

    active = bool(channels)
    features: dict[str, Any] = {
        "manual_channel_active": active,
        "manual_channel_count": len(channels),
    }
    if nearest:
        features.update(
            {
                "manual_channel_nearest_price": nearest["price"],
                "manual_channel_distance_atr": nearest["distance_atr"],
                "manual_channel_role": nearest["role"],
                "manual_channel_touch": bool(nearest["touch"]),
                "manual_channel_reclaim": bool(nearest["reclaim"]),
                "manual_channel_direction": nearest["direction"],
            }
        )
    return {
        "active": active,
        "count": len(channels),
        "analysis_logical_index": analysis_logical_index,
        "analysis_ts": analysis_ts,
        "decision_anchor_ts": analysis_ts,
        "decision_bar_offset": decision_bar_offset,
        "decision_available_at": decision_available_at,
        "price": latest.close,
        "channels": channels,
        "nearest": nearest,
        "features": features,
        **(
            {
                "availability": {
                    "state": "blocked",
                    "reason_code": "manual_channel_geometry_unavailable",
                    "channels": blocked_channels,
                }
            }
            if not active and blocked_channels
            else {}
        ),
    }


def channel_master_indicator(
    bars: Sequence[Bar],
    drawings: Sequence[Mapping[str, Any]] | None = None,
    *,
    atr_value: float | None = None,
    features: Mapping[str, Any] | None = None,
    feature_context: Any | None = None,
    vwap_session: ProviderSessionReset | None = None,
    instrument_id: str = "",
    route_fingerprint: str = "",
    canonical_generation: int | None = None,
) -> dict[str, Any]:
    feature_values = features if isinstance(features, Mapping) else {}
    move_atr = _number(feature_values.get("move_atr"), 0.0) or 0.0
    context_rvol = _number(feature_values.get("rvol_adaptive"))
    if context_rvol is None:
        context_rvol = _number(getattr(feature_context, "latest_rvol", None), 1.0)
    impulse_strength = max(
        abs(move_atr) * 0.75 + max(float(context_rvol or 1.0) - 1.0, 0.0) * 0.45,
        0.0,
    )
    ema_values = getattr(feature_context, "ema_magnet", ())
    ema_value = _number(ema_values[-1]) if isinstance(ema_values, Sequence) and ema_values else None
    vwap_values = (
        pine.vwap_series(bars, reset=vwap_session.key_for_bar)
        if bars and vwap_session is not None and vwap_session.available
        else []
    )
    context = manual_channel_context(
        bars,
        drawings,
        atr_value=atr_value,
        impulse_strength=impulse_strength,
        ema_value=ema_value,
        vwap_value=_number(vwap_values[-1]) if vwap_values else None,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        canonical_generation=canonical_generation,
    )
    interaction_model, research_observations = _channel_interaction_payload(context)
    return {
        **context,
        "version": "3.2-decision-anchor",
        "interaction_model": interaction_model,
        **({"research_observations": research_observations} if research_observations else {}),
        "series": [],
        "events": [],
        "latest": context.get("nearest"),
        "overlays": [],
    }


def signal_candidates_from_channel_master(
    context: Mapping[str, Any],
    *,
    score_floor: float = 0.0,
    atr_value: float | None = None,
    features: dict[str, Any] | None = None,
) -> list[SignalCandidate]:
    del atr_value, features
    nearest = context.get("nearest")
    if not isinstance(nearest, Mapping) or not nearest.get("touch") or not nearest.get("score"):
        return []

    direction_value = str(nearest.get("direction") or "flat")
    if direction_value not in {"long", "short"}:
        return []

    score = float(nearest["score"])
    if score < max(58.0, float(score_floor)):
        return []
    if nearest.get("no_fade") is True:
        return []

    level = _number(nearest.get("price"))
    if level is None:
        return []

    # Трейдерская логика: чем важнее роль уровня, тем больше бонус к качеству
    role = nearest.get("role")
    reclaim = nearest.get("reclaim")

    # Формируем человекочитаемую причину для Decision Panel
    level_label = nearest.get("label") or "EDGE"
    reason = "channel_master_reaction"

    # Добавляем в детали информацию о потенциале (RR)
    target = _number(nearest.get("target"))
    channel_id = str(nearest.get("channel_id") or "")
    channel_data = next((c for c in context.get("channels", []) if c["id"] == channel_id), {})

    direction = Direction(direction_value)
    quality = trade_plan_quality(direction, trigger=level, target=target)
    details = {
        **quality,
        "source": "channel_master",
        "level_role": role,
        "level_label": level_label,
        "distance_atr": nearest.get("distance_atr"),
        "is_reclaim": bool(reclaim),
        "is_major_edge": role in {"lower_edge", "upper_edge"},
        "impulse_strength": channel_data.get("impulse_strength", 0.0),
        "is_ghost": "ghost" in role,
        "plan_inference": "manual_channel_level_plan",
    }
    kind = (
        ScenarioKind.TRANSIT
        if float(details["impulse_strength"] or 0.0) > 0.8
        else ScenarioKind.FADE
    )

    return [
        SignalCandidate(
            name="manual_channel_reaction",
            direction=direction,
            score=score,
            level=level,
            reason=reason,
            details=details,
            kind=kind,
            source="channel_master",
            role="channel_reaction",
            trigger_event=DomainFact(
                "manual_channel_reclaim" if reclaim else "manual_channel_touch",
                {"level_role": str(role or "channel")},
            ),
            reason_code=reason,
            finality=CandidateFinality.CONFIRMED,
        )
    ]
