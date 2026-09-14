from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.contracts import (
    GexCaptureMode,
    GexComparisonLaneKey,
    gex_capture_lane,
    gex_comparison_lane_key,
    require_exact_gex_capture_lane,
)
from aef_terminal.data.gex.snapshot_validation import (
    require_gex_snapshot_payload,
)
from aef_terminal.data.gex.utils import finite_number_or_none
from aef_terminal.domain import Bar, Direction, DomainFact
from aef_terminal.runtime import pine


_MAX_SNAPSHOT_AGE_SECONDS = 60 * 60
_MAX_PAIR_GAP_SECONDS = 15 * 60
_GEX_DYNAMICS_ATR_LENGTH = 14
_ComparisonLane = tuple[str, str, GexComparisonLaneKey]


@dataclass(frozen=True)
class GexDynamicsParams:
    motion_tolerance_atr: float = 0.18
    proximity_atr: float = 0.55
    activity_rvol_threshold: float = 1.8
    capture_mode: GexCaptureMode = "request"

    def __post_init__(self) -> None:
        for field_name in (
            "motion_tolerance_atr",
            "proximity_atr",
            "activity_rvol_threshold",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"GexDynamicsParams.{field_name} must be numeric")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"GexDynamicsParams.{field_name} must be finite and non-negative")
        gex_capture_lane(self.capture_mode)


@dataclass(frozen=True)
class _SnapshotRecord:
    captured_at: datetime | None
    value: dict[str, Any] | None
    issue: str = ""
    comparison_lane: _ComparisonLane | None = None
    capture_mode: GexCaptureMode | None = None


def gex_dynamics(
    bars: Sequence[Bar],
    gex_history: Sequence[dict[str, Any]] | None = None,
    params: GexDynamicsParams | None = None,
) -> dict[str, Any]:
    """Compare exact GEX snapshots and emit non-trading observation context."""

    params = params or GexDynamicsParams()
    empty = {
        "version": "2.2-python",
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
    }
    if not bars:
        return empty

    records: list[_SnapshotRecord] = []
    for snapshot in gex_history or ():
        record = _validate_snapshot(snapshot)
        if record.capture_mode is None or record.capture_mode == params.capture_mode:
            records.append(record)
    invalid_without_time = next(
        (record.issue for record in records if record.captured_at is None),
        "",
    )
    timeline = sorted(
        (record for record in records if record.captured_at is not None),
        key=lambda record: record.captured_at,
    )
    atr_values = pine.atr_rma_series(bars, _GEX_DYNAMICS_ATR_LENGTH)
    series: list[dict[str, Any]] = []
    history_ptr = 0
    current_record: _SnapshotRecord | None = None
    previous_record: _SnapshotRecord | None = None
    current_motion_tolerance: float | None = None

    for index, bar in enumerate(bars):
        bar_time = bar.ts
        previous_ptr = history_ptr
        while (
            history_ptr < len(timeline)
            and timeline[history_ptr].captured_at is not None
            and timeline[history_ptr].captured_at <= bar_time
        ):
            history_ptr += 1
        snapshot_effective_now = history_ptr > previous_ptr
        if snapshot_effective_now:
            current_record = timeline[history_ptr - 1]
            previous_record = _immediate_previous_record(timeline, history_ptr - 1)
            snapshot_atr = finite_number_or_none(atr_values[index])
            current_motion_tolerance = (
                snapshot_atr * float(params.motion_tolerance_atr)
                if snapshot_atr is not None and snapshot_atr > 0
                else None
            )
        atr = finite_number_or_none(atr_values[index])
        series.append(
            _comparison_row(
                timestamp=bar_time,
                close=float(bar.close),
                atr=atr,
                current_record=current_record,
                previous_record=previous_record,
                invalid_without_time=invalid_without_time,
                motion_tolerance=current_motion_tolerance,
                snapshot_effective_now=snapshot_effective_now,
                params=params,
            )
        )

    latest = series[-1]
    newest_record = timeline[-1] if timeline else None
    if (
        newest_record is not None
        and newest_record.captured_at is not None
        and newest_record.captured_at > bars[-1].ts
    ):
        latest_atr = finite_number_or_none(atr_values[-1])
        latest_motion_tolerance = (
            latest_atr * float(params.motion_tolerance_atr)
            if latest_atr is not None and latest_atr > 0
            else None
        )
        newest_value = newest_record.value
        current_spot = (
            finite_number_or_none(newest_value.get("spot")) if newest_value is not None else None
        )
        latest = _comparison_row(
            timestamp=newest_record.captured_at,
            close=current_spot if current_spot is not None else float(bars[-1].close),
            atr=latest_atr,
            current_record=newest_record,
            previous_record=_immediate_previous_record(timeline, len(timeline) - 1),
            invalid_without_time=invalid_without_time,
            motion_tolerance=latest_motion_tolerance,
            snapshot_effective_now=True,
            params=params,
        )

    return {
        "version": "2.2-python",
        "series": series,
        "events": [],
        "latest": latest,
        "overlays": [],
    }


def _comparison_row(
    *,
    timestamp: datetime,
    close: float,
    atr: float | None,
    current_record: _SnapshotRecord | None,
    previous_record: _SnapshotRecord | None,
    invalid_without_time: str,
    motion_tolerance: float | None,
    snapshot_effective_now: bool,
    params: GexDynamicsParams,
) -> dict[str, Any]:
    quality, issue, age_minutes, gap_minutes, coverage = _comparison_quality(
        current_record,
        previous_record,
        bar_time=timestamp,
        invalid_without_time=invalid_without_time,
    )
    current = current_record.value if current_record is not None else None
    previous = previous_record.value if previous_record is not None else None
    if quality == "READY" and motion_tolerance is None:
        quality = "INVALID"
        issue = "invalid_snapshot_atr"

    tolerance = motion_tolerance if motion_tolerance is not None else 0.0
    comparable = quality == "READY"
    current_call_wall = current.get("call_wall") if current else None
    previous_call_wall = previous.get("call_wall") if previous else None
    current_put_wall = current.get("put_wall") if current else None
    previous_put_wall = previous.get("put_wall") if previous else None
    current_gamma_flip = current.get("gamma_flip") if current else None
    previous_gamma_flip = previous.get("gamma_flip") if previous else None
    call_motion = _motion(
        current_call_wall,
        previous_call_wall,
        tolerance=tolerance,
        comparable=comparable,
    )
    put_motion = _motion(
        current_put_wall,
        previous_put_wall,
        tolerance=tolerance,
        comparable=comparable,
    )
    flip_motion = _motion(
        current_gamma_flip,
        previous_gamma_flip,
        tolerance=tolerance,
        comparable=comparable,
    )
    current_wall_span = (
        abs(current_call_wall - current_put_wall)
        if current_call_wall is not None and current_put_wall is not None
        else None
    )
    previous_wall_span = (
        abs(previous_call_wall - previous_put_wall)
        if previous_call_wall is not None and previous_put_wall is not None
        else None
    )
    wall_span_motion = _motion(
        current_wall_span,
        previous_wall_span,
        tolerance=tolerance,
        comparable=comparable,
    )
    structure_state = _structure_state(
        comparison_quality=quality,
        call_motion=call_motion,
        put_motion=put_motion,
        gamma_flip_motion=flip_motion,
        wall_span_motion=wall_span_motion,
    )
    activity_state = _activity_state(
        current.get("option_activity") if comparable and current else None,
        threshold=float(params.activity_rvol_threshold),
    )
    proximities = (
        _proximities(
            current,
            close=close,
            atr=atr,
            proximity_atr=float(params.proximity_atr),
            activity_state=activity_state,
        )
        if comparable and current is not None and atr is not None
        else []
    )
    significant_motion = any(
        motion["significant"] for motion in (call_motion, put_motion, flip_motion)
    )
    has_comparable_structure = structure_state != "UNKNOWN"
    migration_event = snapshot_effective_now and significant_motion
    if not comparable or not has_comparable_structure:
        state = "WAIT"
    elif migration_event:
        state = "MIGRATION"
    elif proximities:
        state = "NEAR"
    else:
        state = "NO_KEY_LEVEL_MIGRATION"
    detail_code = _detail_code(state, quality)
    captured_at = (
        current_record.captured_at.isoformat()
        if current_record is not None and current_record.captured_at is not None
        else None
    )
    previous_captured_at = (
        previous_record.captured_at.isoformat()
        if previous_record is not None and previous_record.captured_at is not None
        else None
    )
    return {
        "ts": timestamp.isoformat(),
        "state": state,
        "watch_state": "WATCH" if comparable and has_comparable_structure else "WAIT",
        "detail_code": detail_code,
        "context_only": True,
        "action": "WAIT",
        "direction": Direction.FLAT.value,
        "score": 0.0,
        "comparison_quality": quality,
        "comparison_issue": issue,
        "comparison_age_minutes": age_minutes,
        "comparison_gap_minutes": gap_minutes,
        "level_price_coverage": coverage,
        "snapshot_effective_now": snapshot_effective_now,
        "captured_at": captured_at,
        "previous_captured_at": previous_captured_at,
        "instrument_id": current.get("instrument_id") if current else None,
        "route_fingerprint": current.get("route_fingerprint") if current else None,
        "provider_symbol": current.get("provider_symbol") if current else None,
        "source": current.get("source") if current else None,
        "capture_mode": current.get("capture_mode") if current else None,
        "capture_revision": current.get("capture_revision") if current else None,
        "comparison_scope": current.get("comparison_scope") if current else None,
        "global_gamma_regime": current.get("global_gamma_regime") if current else "UNKNOWN",
        "gamma_flip": current.get("gamma_flip") if current else None,
        "call_wall": current.get("call_wall") if current else None,
        "put_wall": current.get("put_wall") if current else None,
        "call_wall_motion": call_motion,
        "put_wall_motion": put_motion,
        "gamma_flip_motion": flip_motion,
        "wall_span_motion": wall_span_motion,
        "structure_state": structure_state,
        "activity_state": activity_state,
        "proximities": proximities,
        "scenario": "gex_dynamics",
        "trigger_event": DomainFact(detail_code).as_dict(),
        "evidence": {
            "context": [
                DomainFact("context_only").as_dict(),
                DomainFact("not_market_phase").as_dict(),
            ],
        },
        "quality": DomainFact(quality.lower()).as_dict(),
        "metrics": {
            "comparison_quality": quality,
            "level_price_coverage": coverage,
            "proximity_count": len(proximities),
            "significant_motion": significant_motion,
            "migration_event": migration_event,
            "structure_state": structure_state,
        },
    }


def _validate_snapshot(snapshot: dict[str, Any]) -> _SnapshotRecord:
    if not isinstance(snapshot, Mapping):
        return _SnapshotRecord(None, None, "invalid_snapshot")
    captured_at = _captured_at(snapshot.get("captured_at"))
    if captured_at is None:
        return _SnapshotRecord(None, None, "invalid_captured_at")

    try:
        source, capture_mode = require_exact_gex_capture_lane(
            source=snapshot.get("source"),
            capture_mode=snapshot.get("capture_mode"),
        )
    except ValueError:
        return _SnapshotRecord(captured_at, None, "invalid_capture_mode")
    for identity_field in (
        "instrument_id",
        "route_fingerprint",
        "provider_symbol",
    ):
        value = snapshot.get(identity_field)
        if not isinstance(value, str) or not value or value != value.strip():
            return _SnapshotRecord(
                captured_at,
                None,
                f"invalid_{identity_field}",
                capture_mode=capture_mode,
            )

    try:
        canonical_snapshot = require_gex_snapshot_payload(
            snapshot,
            projection="history",
        )
        scope = canonical_snapshot["comparison_scope"]
        scope_lane = gex_comparison_lane_key(scope, capture_mode=capture_mode)
    except TypeError, ValueError:
        return _SnapshotRecord(
            captured_at,
            None,
            "invalid_snapshot_payload",
            capture_mode=capture_mode,
        )
    comparison_lane: _ComparisonLane = (
        snapshot["instrument_id"],
        snapshot["route_fingerprint"],
        scope_lane,
    )
    canonical_levels = canonical_snapshot["levels"]
    level_prices = frozenset(float(level["price"]) for level in canonical_levels)

    option_activity = canonical_snapshot["option_activity"]

    return _SnapshotRecord(
        captured_at,
        {
            "captured_at": captured_at,
            "instrument_id": snapshot["instrument_id"],
            "route_fingerprint": snapshot["route_fingerprint"],
            "provider_symbol": snapshot["provider_symbol"],
            "source": source,
            "capture_mode": capture_mode,
            "capture_revision": canonical_snapshot["capture_revision"],
            "comparison_scope": scope,
            "level_prices": level_prices,
            "spot": canonical_snapshot["spot"],
            "global_gamma_regime": canonical_snapshot["global_gamma_regime"],
            "call_wall": canonical_snapshot["call_wall"],
            "put_wall": canonical_snapshot["put_wall"],
            "gamma_flip": canonical_snapshot["gamma_flip"],
            "option_activity": option_activity,
        },
        comparison_lane=comparison_lane,
        capture_mode=capture_mode,
    )


def _immediate_previous_record(
    timeline: Sequence[_SnapshotRecord],
    current_index: int,
) -> _SnapshotRecord | None:
    if current_index <= 0:
        return None
    return timeline[current_index - 1]


def _captured_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _comparison_quality(
    current_record: _SnapshotRecord | None,
    previous_record: _SnapshotRecord | None,
    *,
    bar_time: datetime,
    invalid_without_time: str,
) -> tuple[str, str, float | None, float | None, float | None]:
    if invalid_without_time:
        return "INVALID", invalid_without_time, None, None, None
    if current_record is None:
        return "NEED_HISTORY", "need_current_snapshot", None, None, None
    if current_record.value is None:
        return "INVALID", current_record.issue, None, None, None
    age_seconds = (bar_time - current_record.captured_at).total_seconds()
    age_minutes = round(age_seconds / 60.0, 3)
    if age_seconds < 0:
        return "INVALID", "snapshot_from_future", age_minutes, None, None
    if age_seconds > _MAX_SNAPSHOT_AGE_SECONDS:
        return "STALE", "snapshot_stale", age_minutes, None, None
    if previous_record is None:
        return "NEED_HISTORY", "need_previous_snapshot", age_minutes, None, None
    if previous_record.value is None:
        return "INVALID", previous_record.issue, age_minutes, None, None
    gap_seconds = (current_record.captured_at - previous_record.captured_at).total_seconds()
    gap_minutes = round(gap_seconds / 60.0, 3)
    if gap_seconds <= 0:
        return "INVALID", "non_increasing_capture_time", age_minutes, gap_minutes, None
    if current_record.comparison_lane != previous_record.comparison_lane:
        return (
            "SCOPE_CHANGED",
            "comparison_scope_changed",
            age_minutes,
            gap_minutes,
            None,
        )
    if gap_seconds > _MAX_PAIR_GAP_SECONDS:
        return "GAP", "snapshot_pair_gap", age_minutes, gap_minutes, None

    current = current_record.value
    previous = previous_record.value
    coverage = _level_price_coverage(current["level_prices"], previous["level_prices"])
    return "READY", "", age_minutes, gap_minutes, coverage


def _level_price_coverage(
    current: frozenset[float],
    previous: frozenset[float],
) -> float:
    denominator = max(len(current), len(previous))
    return round(len(current.intersection(previous)) / denominator, 4) if denominator else 0.0


def _motion(
    current: float | None,
    previous: float | None,
    *,
    tolerance: float,
    comparable: bool,
) -> dict[str, Any]:
    raw_delta = current - previous if current is not None and previous is not None else None
    delta = raw_delta if comparable else None
    if delta is None:
        state = "UNKNOWN"
        significant = False
    elif abs(delta) <= tolerance:
        state = "UNCHANGED"
        significant = False
    else:
        state = "UP" if delta > 0 else "DOWN"
        significant = True
    return {
        "state": state,
        "previous": previous,
        "current": current,
        "delta": delta,
        "tolerance": tolerance,
        "significant": significant,
    }


def _structure_state(
    *,
    comparison_quality: str,
    call_motion: Mapping[str, Any],
    put_motion: Mapping[str, Any],
    gamma_flip_motion: Mapping[str, Any],
    wall_span_motion: Mapping[str, Any],
) -> str:
    if comparison_quality != "READY":
        return "UNKNOWN"
    call_state = str(call_motion.get("state") or "UNKNOWN")
    put_state = str(put_motion.get("state") or "UNKNOWN")
    flip_state = str(gamma_flip_motion.get("state") or "UNKNOWN")
    states = tuple(
        state
        for state in (call_state, put_state, flip_state)
        if state in {"UP", "DOWN", "UNCHANGED"}
    )
    if not states:
        return "UNKNOWN"
    if all(state == "UNCHANGED" for state in states):
        return "LEVELS_HELD"

    directional_states = [state for state in states if state in {"UP", "DOWN"}]
    if len(directional_states) >= 2 and len(set(directional_states)) == 1:
        return "SHIFT_HIGHER" if directional_states[0] == "UP" else "SHIFT_LOWER"

    if call_state in {"UP", "DOWN"} and put_state in {"UP", "DOWN"}:
        span_state = str(wall_span_motion.get("state") or "UNKNOWN")
        if span_state == "UP":
            return "WALLS_WIDENING"
        if span_state == "DOWN":
            return "WALLS_NARROWING"
        return "MIXED_MIGRATION"

    significant_fields = [
        name
        for name, state in (
            ("CALL", call_state),
            ("PUT", put_state),
            ("GAMMA_FLIP", flip_state),
        )
        if state in {"UP", "DOWN"}
    ]
    if significant_fields == ["CALL"]:
        return "CALL_WALL_MOVING"
    if significant_fields == ["PUT"]:
        return "PUT_WALL_MOVING"
    if significant_fields == ["GAMMA_FLIP"]:
        return "GAMMA_FLIP_MOVING"
    return "MIXED_MIGRATION"


def _activity_state(
    activity: dict[str, Any] | None,
    *,
    threshold: float,
) -> dict[str, Any]:
    if activity is None:
        unknown = {
            "state": "UNKNOWN",
            "volume_delta": None,
            "rate_per_minute": None,
            "rvol": None,
            "baseline_samples": 0,
        }
        return {
            "scope": "selected_chain",
            "elapsed_seconds": None,
            "contract_universe": None,
            "call": dict(unknown),
            "put": dict(unknown),
        }
    sides: dict[str, dict[str, Any]] = {}
    for side in ("call", "put"):
        source = activity[side]
        if source["rvol"] is None:
            state = "BASELINING"
        elif source["volume_delta"] <= 0 or source["rate_per_minute"] <= 0:
            state = "QUIET"
        elif source["rvol"] >= threshold:
            state = "ACTIVE"
        else:
            state = "THIN"
        sides[side] = {"state": state, **source}
    return {
        "scope": activity["scope"],
        "elapsed_seconds": activity["elapsed_seconds"],
        "contract_universe": dict(activity["contract_universe"]),
        **sides,
    }


def _proximities(
    current: Mapping[str, Any],
    *,
    close: float,
    atr: float,
    proximity_atr: float,
    activity_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    threshold = atr * proximity_atr
    out: list[dict[str, Any]] = []
    for side, field_name in (("CALL", "call_wall"), ("PUT", "put_wall")):
        price = current.get(field_name)
        if price is None:
            continue
        distance = abs(close - price)
        if distance <= threshold:
            side_activity = activity_state[side.lower()]
            out.append(
                {
                    "side": side,
                    "price": price,
                    "distance": distance,
                    "distance_atr": distance / atr,
                    "activity_state": side_activity["state"],
                }
            )
    return out


def _detail_code(state: str, quality: str) -> str:
    if quality != "READY":
        return quality.lower()
    if state == "WAIT":
        return "insufficient_structure"
    if state == "MIGRATION":
        return "migration"
    if state == "NEAR":
        return "price_near_concentration"
    return "no_key_level_migration"
