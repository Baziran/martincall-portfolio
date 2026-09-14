"""Deterministic continuation-pattern geometry for Impulse Fib."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional


CONTINUATION_MIN_FORMATION_BARS = 5
CONTINUATION_MIN_STABLE_PREFIXES = 2
CONTINUATION_MAX_BARS = 12
CONTINUATION_PARALLEL_GAP_ATR = 0.04
CONTINUATION_SLOPE_EPSILON_ATR = 0.008
CONTINUATION_CONVERGENCE_MIN_ATR = 0.02
CONTINUATION_CONTRACTION_MIN = 0.12
CONTINUATION_CONTRACTION_MAX = 0.72
CONTINUATION_TOUCH_TOLERANCE_ATR = 0.20
CONTINUATION_MAX_MEAN_GAP_ATR = 0.55
CONTINUATION_INVALIDATION_PAD_ATR = 0.12


def _round(value: float | None, digits: int = 4) -> float | None:
    return round_optional(value, digits)


@dataclass(frozen=True, slots=True)
class _BoundaryFit:
    slope: float
    intercept: float
    start: float
    current: float
    mean_gap_atr: float
    touches: int


@dataclass(frozen=True, slots=True)
class _GeometryModel:
    accepted: bool
    classification_code: str
    structure_kind: str
    reason_code: str
    upper: _BoundaryFit
    lower: _BoundaryFit
    start_width: float
    current_width: float
    convergence_per_bar: float
    contraction: float
    parallel_gap_atr: float
    fit_score: float


def _pivot_indices(values: Sequence[float], *, upper: bool) -> tuple[int, ...]:
    if len(values) < 2:
        return tuple(range(len(values)))
    selected = {0, len(values) - 1}
    for index in range(1, len(values) - 1):
        previous = float(values[index - 1])
        current = float(values[index])
        following = float(values[index + 1])
        if upper:
            if (
                current >= previous
                and current >= following
                and (current > previous or current > following)
            ):
                selected.add(index)
        elif (
            current <= previous
            and current <= following
            and (current < previous or current < following)
        ):
            selected.add(index)
    return tuple(sorted(selected))


def _theil_sen_slope(values: Sequence[float], indices: Sequence[int]) -> float:
    slopes = [
        (float(values[right]) - float(values[left])) / (right - left)
        for left_position, left in enumerate(indices)
        for right in indices[left_position + 1 :]
        if right > left
    ]
    return float(median(slopes)) if slopes else 0.0


def _fit_boundary(
    values: Sequence[float],
    *,
    atr: float,
    upper: bool,
) -> _BoundaryFit:
    indices = _pivot_indices(values, upper=upper)
    slope = _theil_sen_slope(values, indices)
    intercepts = [float(value) - slope * index for index, value in enumerate(values)]
    intercept = max(intercepts) if upper else min(intercepts)
    fitted = [intercept + slope * index for index in range(len(values))]
    gaps = [
        max(line - float(value), 0.0) if upper else max(float(value) - line, 0.0)
        for line, value in zip(fitted, values, strict=True)
    ]
    touch_tolerance = atr * CONTINUATION_TOUCH_TOLERANCE_ATR
    return _BoundaryFit(
        slope=slope,
        intercept=intercept,
        start=fitted[0],
        current=fitted[-1],
        mean_gap_atr=(sum(gaps) / len(gaps)) / atr,
        touches=sum(gap <= touch_tolerance for gap in gaps),
    )


def _classification(
    *,
    direction: str,
    upper_slope_atr: float,
    lower_slope_atr: float,
    parallel_gap_atr: float,
    contraction: float,
    convergence_atr: float,
) -> tuple[str, str, str]:
    epsilon = CONTINUATION_SLOPE_EPSILON_ATR
    converging = (
        convergence_atr >= CONTINUATION_CONVERGENCE_MIN_ATR
        and CONTINUATION_CONTRACTION_MIN <= contraction <= CONTINUATION_CONTRACTION_MAX
    )
    if converging and upper_slope_atr < -epsilon < lower_slope_atr:
        return (
            "bull_pennant" if direction == "long" else "bear_pennant",
            "pennant",
            "continuation_pennant_confirmed",
        )
    if converging and upper_slope_atr < -epsilon and lower_slope_atr < -epsilon:
        if direction == "long":
            return "bull_falling_wedge", "wedge", "continuation_falling_wedge_confirmed"
        return "", "irregular", "continuation_wrong_direction_wedge"
    if converging and upper_slope_atr > epsilon and lower_slope_atr > epsilon:
        if direction == "short":
            return "bear_rising_wedge", "wedge", "continuation_rising_wedge_confirmed"
        return "", "irregular", "continuation_wrong_direction_wedge"

    parallel = parallel_gap_atr <= CONTINUATION_PARALLEL_GAP_ATR
    if not parallel:
        return "", "irregular", "continuation_boundaries_not_parallel_or_converging"
    if direction == "long" and upper_slope_atr < -epsilon and lower_slope_atr < -epsilon:
        return "bull_flag", "flag", "continuation_countertrend_flag_confirmed"
    if direction == "short" and upper_slope_atr > epsilon and lower_slope_atr > epsilon:
        return "bear_flag", "flag", "continuation_countertrend_flag_confirmed"
    if direction == "long" and upper_slope_atr > epsilon and lower_slope_atr > epsilon:
        return "bull_runaway_channel", "runaway_channel", "continuation_runaway_channel_confirmed"
    if direction == "short" and upper_slope_atr < -epsilon and lower_slope_atr < -epsilon:
        return "bear_runaway_channel", "runaway_channel", "continuation_runaway_channel_confirmed"
    return "", "irregular", "continuation_directional_slope_missing"


def _fit_geometry(
    window: Sequence[Bar],
    *,
    atr: float,
    direction: str,
) -> _GeometryModel:
    safe_atr = max(float(atr), 0.000001)
    highs = [float(bar.high) for bar in window]
    lows = [float(bar.low) for bar in window]
    upper = _fit_boundary(highs, atr=safe_atr, upper=True)
    lower = _fit_boundary(lows, atr=safe_atr, upper=False)
    start_width = upper.start - lower.start
    current_width = upper.current - lower.current
    convergence_per_bar = lower.slope - upper.slope
    contraction = (
        1.0 - current_width / start_width if start_width > 0.0 and current_width > 0.0 else 0.0
    )
    parallel_gap_atr = abs(upper.slope - lower.slope) / safe_atr
    fit_score = pine.clamp(
        1.0 - max(upper.mean_gap_atr, lower.mean_gap_atr) / CONTINUATION_MAX_MEAN_GAP_ATR,
        0.0,
        1.0,
    )
    if len(window) < CONTINUATION_MIN_FORMATION_BARS:
        return _GeometryModel(
            False,
            "",
            "forming",
            "continuation_formation_window_short",
            upper,
            lower,
            start_width,
            current_width,
            convergence_per_bar,
            contraction,
            parallel_gap_atr,
            fit_score,
        )
    if start_width <= safe_atr * 0.12 or current_width <= safe_atr * 0.12:
        reason_code = "continuation_boundaries_crossed_or_too_narrow"
        classification_code = ""
        structure_kind = "irregular"
    elif (
        upper.touches < 2
        or lower.touches < 2
        or upper.mean_gap_atr > CONTINUATION_MAX_MEAN_GAP_ATR
        or lower.mean_gap_atr > CONTINUATION_MAX_MEAN_GAP_ATR
    ):
        reason_code = "continuation_boundary_fit_insufficient"
        classification_code = ""
        structure_kind = "irregular"
    else:
        classification_code, structure_kind, reason_code = _classification(
            direction=direction,
            upper_slope_atr=upper.slope / safe_atr,
            lower_slope_atr=lower.slope / safe_atr,
            parallel_gap_atr=parallel_gap_atr,
            contraction=contraction,
            convergence_atr=convergence_per_bar / safe_atr,
        )
    return _GeometryModel(
        bool(classification_code),
        classification_code,
        structure_kind,
        reason_code,
        upper,
        lower,
        start_width,
        current_width,
        convergence_per_bar,
        contraction,
        parallel_gap_atr,
        fit_score,
    )


def _geometry_payload(
    model: _GeometryModel,
    *,
    atr: float,
    projection_bars: int,
    forward_bars: int = 0,
) -> dict[str, Any]:
    safe_atr = max(float(atr), 0.000001)
    upper_current = model.upper.current + model.upper.slope * forward_bars
    lower_current = model.lower.current + model.lower.slope * forward_bars
    current_width = upper_current - lower_current
    requested_projection = max(3, min(int(projection_bars), 12))
    rendered_projection = requested_projection
    if model.convergence_per_bar > 0.0:
        minimum_width = max(safe_atr * 0.12, current_width * 0.20)
        safe_projection = int(max((current_width - minimum_width) / model.convergence_per_bar, 0.0))
        rendered_projection = min(requested_projection, safe_projection)
    upper_projected = upper_current + model.upper.slope * rendered_projection
    lower_projected = lower_current + model.lower.slope * rendered_projection
    return {
        "geometry_accepted": model.accepted,
        "classification_code": model.classification_code,
        "structure_kind": model.structure_kind,
        "geometry_reason_code": model.reason_code,
        "upper_boundary": {
            "start_price": _round(model.upper.start),
            "current_price": _round(upper_current),
            "end_price": _round(upper_projected),
            "slope_price_per_bar": _round(model.upper.slope, 8),
            "slope_atr_per_bar": _round(model.upper.slope / safe_atr),
            "touches": model.upper.touches,
            "mean_gap_atr": _round(model.upper.mean_gap_atr),
        },
        "lower_boundary": {
            "start_price": _round(model.lower.start),
            "current_price": _round(lower_current),
            "end_price": _round(lower_projected),
            "slope_price_per_bar": _round(model.lower.slope, 8),
            "slope_atr_per_bar": _round(model.lower.slope / safe_atr),
            "touches": model.lower.touches,
            "mean_gap_atr": _round(model.lower.mean_gap_atr),
        },
        "boundary_projection_bars": rendered_projection,
        "boundary_start_width_atr": _round(model.start_width / safe_atr),
        "boundary_current_width_atr": _round(current_width / safe_atr),
        "boundary_convergence": _round(model.contraction),
        "boundary_convergence_atr_per_bar": _round(model.convergence_per_bar / safe_atr),
        "boundary_parallel_gap_atr": _round(model.parallel_gap_atr),
        "boundary_fit_score": _round(model.fit_score),
    }


def continuation_geometry(
    window: Sequence[Bar],
    *,
    latest_atr: float,
    projection_bars: int,
    direction: str,
) -> dict[str, Any]:
    """Return one typed geometry classification for tests and diagnostics."""

    model = _fit_geometry(window, atr=latest_atr, direction=direction)
    return _geometry_payload(
        model,
        atr=latest_atr,
        projection_bars=projection_bars,
    )


def _bar_index_for_ts(bars: Sequence[Bar], ts: str | None) -> int | None:
    if not ts:
        return None
    for index, bar in enumerate(bars):
        if bar.ts.isoformat() == ts:
            return index
    return None


def _inactive_pattern(
    *,
    state: str,
    reason_code: str,
    direction: str = "flat",
    pattern_bars: int = 0,
) -> dict[str, Any]:
    return {
        "active": False,
        "state": state,
        "reason_code": reason_code,
        "direction": direction,
        "phase": "failed" if state == "failed" else "forming",
        "pattern_bars": pattern_bars,
    }


def _stable_classification(
    window: Sequence[Bar],
    *,
    atr: float,
    direction: str,
) -> tuple[str, int] | None:
    previous_code = ""
    previous_accepted = False
    stable_run = 0
    for size in range(CONTINUATION_MIN_FORMATION_BARS, len(window) + 1):
        model = _fit_geometry(window[:size], atr=atr, direction=direction)
        if model.accepted and previous_accepted and model.classification_code == previous_code:
            stable_run += 1
        else:
            stable_run = 1 if model.accepted else 0
        if stable_run >= CONTINUATION_MIN_STABLE_PREFIXES:
            return model.classification_code, size
        previous_code = model.classification_code
        previous_accepted = model.accepted
    return None


def continuation_pattern(
    *,
    bars: Sequence[Bar],
    latest_event: dict[str, Any] | None,
    atr_values: Sequence[float],
    projection_bars: int,
) -> dict[str, Any]:
    if not latest_event:
        return _inactive_pattern(
            state="unavailable",
            reason_code="continuation_impulse_missing",
        )
    direction = str(latest_event.get("direction") or "flat")
    if direction not in {"long", "short"}:
        return _inactive_pattern(
            state="unavailable",
            reason_code="continuation_direction_missing",
        )
    event_index = _bar_index_for_ts(bars, str(latest_event.get("ts") or ""))
    if event_index is None:
        event_index = _bar_index_for_ts(bars, str(latest_event.get("impulse_end_ts") or ""))
    if event_index is None:
        return _inactive_pattern(
            state="unavailable",
            reason_code="continuation_impulse_anchor_missing",
            direction=direction,
        )
    pattern_bars = len(bars) - event_index - 1
    minimum_pattern_bars = CONTINUATION_MIN_FORMATION_BARS + CONTINUATION_MIN_STABLE_PREFIXES
    if pattern_bars < minimum_pattern_bars:
        return _inactive_pattern(
            state="forming",
            reason_code="continuation_wait_stable_geometry",
            direction=direction,
            pattern_bars=pattern_bars,
        )
    if pattern_bars > CONTINUATION_MAX_BARS:
        return _inactive_pattern(
            state="expired",
            reason_code="continuation_window_expired",
            direction=direction,
            pattern_bars=pattern_bars,
        )

    origin = float(latest_event.get("origin") or 0.0)
    extreme = float(latest_event.get("extreme") or 0.0)
    pole = abs(extreme - origin)
    reference_atr = (
        float(atr_values[event_index])
        if len(atr_values) == len(bars) and event_index < len(atr_values)
        else float(atr_values[-1] if atr_values else 0.0)
    )
    reference_atr = max(reference_atr, 0.000001)
    if pole < reference_atr * 1.2:
        return _inactive_pattern(
            state="rejected",
            reason_code="continuation_pole_too_small",
            direction=direction,
            pattern_bars=pattern_bars,
        )

    window = list(bars[event_index + 1 :])
    formation = window[:-1]
    stable = _stable_classification(formation, atr=reference_atr, direction=direction)
    if stable is None:
        latest_geometry = _fit_geometry(
            formation,
            atr=reference_atr,
            direction=direction,
        )
        return _inactive_pattern(
            state="forming" if latest_geometry.accepted else "rejected",
            reason_code=(
                "continuation_classification_not_stable"
                if latest_geometry.accepted
                else latest_geometry.reason_code
            ),
            direction=direction,
            pattern_bars=pattern_bars,
        )
    locked_code, locked_at_bars = stable
    geometry_model = _fit_geometry(formation, atr=reference_atr, direction=direction)
    if not geometry_model.accepted or geometry_model.classification_code != locked_code:
        return _inactive_pattern(
            state="rejected",
            reason_code="continuation_locked_structure_degraded",
            direction=direction,
            pattern_bars=pattern_bars,
        )

    pattern_high = max(float(bar.high) for bar in formation)
    pattern_low = min(float(bar.low) for bar in formation)
    pattern_range = pattern_high - pattern_low
    if pattern_range > pole * 0.62 or pattern_range > reference_atr * 4.5:
        return _inactive_pattern(
            state="rejected",
            reason_code="continuation_range_too_wide",
            direction=direction,
            pattern_bars=pattern_bars,
        )
    if direction == "short":
        retrace = max(0.0, (pattern_high - extreme) / max(pole, 0.000001))
    else:
        retrace = max(0.0, (extreme - pattern_low) / max(pole, 0.000001))
    if not 0.18 <= retrace <= 0.86:
        return _inactive_pattern(
            state="rejected",
            reason_code="continuation_retrace_outside_bounds",
            direction=direction,
            pattern_bars=pattern_bars,
        )

    geometry = _geometry_payload(
        geometry_model,
        atr=reference_atr,
        projection_bars=projection_bars,
        forward_bars=1,
    )
    latest = window[-1]
    upper_current = float(geometry["upper_boundary"]["current_price"])
    lower_current = float(geometry["lower_boundary"]["current_price"])
    if direction == "long":
        break_level = upper_current
        invalidation = lower_current - reference_atr * CONTINUATION_INVALIDATION_PAD_ATR
        failed = float(latest.close) < invalidation
        triggered = float(latest.close) > break_level and float(latest.high) >= break_level
        armed = float(latest.high) >= break_level - reference_atr * 0.18
        target_1 = break_level + pole * 0.382
        target_2 = break_level + pole * 0.618
    else:
        break_level = lower_current
        invalidation = upper_current + reference_atr * CONTINUATION_INVALIDATION_PAD_ATR
        failed = float(latest.close) > invalidation
        triggered = float(latest.close) < break_level and float(latest.low) <= break_level
        armed = float(latest.low) <= break_level + reference_atr * 0.18
        target_1 = break_level - pole * 0.382
        target_2 = break_level - pole * 0.618
    phase = "failed" if failed else "triggered" if triggered else "arm" if armed else "watch"

    pole_atr = pole / reference_atr
    compression = pine.clamp(1.0 - pattern_range / max(pole * 0.62, 0.000001), 0.0, 1.0)
    retrace_quality = pine.clamp(1.0 - abs(retrace - 0.45) / 0.41, 0.0, 1.0)
    touch_quality = min(
        (geometry_model.upper.touches + geometry_model.lower.touches) / 6.0,
        1.0,
    )
    quality = pine.clamp(
        44.0
        + min(18.0, max(pole_atr - 1.2, 0.0) * 5.0)
        + compression * 10.0
        + retrace_quality * 8.0
        + geometry_model.fit_score * 12.0
        + touch_quality * 8.0,
        0.0,
        100.0,
    )
    payload = {
        "active": not failed,
        "state": "failed" if failed else "confirmed",
        "reason_code": (
            "confirmed_close_beyond_invalidation" if failed else geometry_model.reason_code
        ),
        "type": locked_code,
        "structure_kind": geometry_model.structure_kind,
        "direction": direction,
        "phase": phase,
        "quality": _round(quality, 1),
        "start_ts": formation[0].ts.isoformat(),
        "end_ts": latest.ts.isoformat(),
        "pattern_high": _round(pattern_high),
        "pattern_low": _round(pattern_low),
        "break_level": _round(break_level),
        "invalidation": _round(invalidation),
        "target_1": _round(target_1),
        "target_2": _round(target_2),
        "pole_origin": _round(origin),
        "pole_extreme": _round(extreme),
        "pole_atr": _round(pole_atr, 2),
        "retrace": _round(retrace),
        "pattern_bars": pattern_bars,
        "pattern_range_atr": _round(pattern_range / reference_atr, 2),
        "classification_locked_at_bars": locked_at_bars,
        **geometry,
        "directive": (
            "stand_down"
            if failed
            else "continue_on_break"
            if phase in {"arm", "triggered"}
            else "watch_break_or_reclaim"
        ),
        "evidence": [
            {
                "code": "continuation_window",
                "pattern_bars": pattern_bars,
                "pole_atr": _round(pole_atr, 2),
            },
            {
                "code": "stable_structure_classification",
                "classification_code": locked_code,
                "locked_at_bars": locked_at_bars,
            },
            {
                "code": "controlled_retrace",
                "retrace": _round(retrace),
                "pattern_range_atr": _round(pattern_range / reference_atr, 2),
            },
            {
                "code": "boundary_geometry",
                "break_level": _round(break_level),
                "invalidation": _round(invalidation),
            },
        ],
    }
    return payload


__all__ = [
    "CONTINUATION_MAX_BARS",
    "CONTINUATION_MIN_FORMATION_BARS",
    "CONTINUATION_MIN_STABLE_PREFIXES",
    "continuation_geometry",
    "continuation_pattern",
]
