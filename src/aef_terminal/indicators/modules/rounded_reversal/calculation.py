"""Causal Rounded Top detection, lifecycle, and typed output."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from aef_terminal.domain import Bar, BarProviderRequest, Direction
from aef_terminal.features.price_action import CandleAnatomy, candle_anatomies
from aef_terminal.features.vsa import VsaFact
from aef_terminal.features.vsa_classify import (
    VSA_ABSORPTION_CODES,
    VSA_IMPULSE_CODES,
    VSA_REVERSAL_SHORT_CODES,
)
from aef_terminal.indicators.domain_facts import indicator_fact_payload
from aef_terminal.runtime import overlays, pine
from aef_terminal.runtime.bar_quality import (
    authoritative_bar_reject_reason,
    confirmed_storage_bars,
    is_provisional_bar,
)
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.mtf import (
    mtf_context_quality,
    parent_context_cutoff,
    trim_mtf_context_to_parent,
)
from aef_terminal.runtime.pivots import alternating_pivot_points
from aef_terminal.runtime.timeframes import interval_minutes, parse_aware_utc_ts

from .adaptive import (
    adaptive_rejection_summary,
    sensitivity_value,
    soft_down,
    soft_up,
)
from .contracts import (
    ROUNDED_REVERSAL_MICRO_TIMEFRAME,
    ROUNDED_REVERSAL_SOURCE,
    ROUNDED_REVERSAL_VERSION,
    RoundedReversalParams,
    _ACTIVE_STATES,
    _TERMINAL_STATES,
)


@dataclass(frozen=True)
class _RoundedTopCandidate:
    pattern_id: str
    base_index: int
    apex_index: int
    apex_confirm_index: int
    base_price: float
    apex_price: float
    atr: float
    height_atr: float
    early_slope_atr_per_bar: float
    late_slope_atr_per_bar: float
    late_to_early_ratio: float
    shape_score: float
    height_score: float
    rise_score: float
    deceleration_score: float


def _shape_candidate(
    bars: Sequence[Bar],
    *,
    atr_values: Sequence[float],
    params: RoundedReversalParams,
) -> _RoundedTopCandidate | None:
    pivot_len = max(int(params.pivot_len), 1)
    pivots = alternating_pivot_points(bars, left=pivot_len, right=pivot_len)
    if len(pivots) < 2:
        return None

    for apex_position in range(len(pivots) - 1, 0, -1):
        apex = pivots[apex_position]
        base = pivots[apex_position - 1]
        if int(apex.get("type") or 0) != 1 or int(base.get("type") or 0) != -1:
            continue
        base_index = int(base["index"])
        apex_index = int(apex["index"])
        rise_bars = apex_index - base_index
        phase_bars = max(int(params.phase_bars), 2)
        if rise_bars < max(int(params.min_rise_bars), phase_bars * 2) or rise_bars > int(
            params.max_rise_bars
        ):
            continue
        apex_confirm_index = apex_index + pivot_len
        if apex_confirm_index >= len(bars):
            continue

        atr = max(float(atr_values[apex_index]), 1e-9)
        apex_price = float(apex["price"])
        base_price = float(base["price"])
        height_atr = (apex_price - base_price) / atr
        height_floor = max(float(params.min_height_atr) * (2.0 / 3.0), 0.10)
        if height_atr < height_floor:
            continue

        early_start = apex_index - phase_bars * 2
        early_end = apex_index - phase_bars
        early_slope = (float(bars[early_end].close) - float(bars[early_start].close)) / (
            phase_bars * atr
        )
        late_slope = (float(bars[apex_index].close) - float(bars[early_end].close)) / (
            phase_bars * atr
        )
        rise_floor = max(float(params.min_early_slope_atr_per_bar) * 0.50, 0.005)
        if early_slope < rise_floor:
            continue
        if late_slope < float(params.late_slope_floor_atr_per_bar) - 0.12:
            continue
        slope_ratio = late_slope / max(early_slope, 1e-9)
        if slope_ratio > float(params.max_late_to_early_ratio) + 0.20:
            continue

        height_score = soft_up(
            height_atr,
            float(params.min_height_atr),
            max(float(params.min_height_atr) - height_floor, 0.10),
        )
        rise_score = soft_up(
            early_slope,
            float(params.min_early_slope_atr_per_bar),
            max(float(params.min_early_slope_atr_per_bar) - rise_floor, 0.01),
        )
        deceleration_score = soft_down(
            slope_ratio,
            float(params.max_late_to_early_ratio),
            0.20,
        )
        shape_score = height_score * 0.35 + rise_score * 0.25 + deceleration_score * 0.40
        shape_floor = sensitivity_value(
            params.sensitivity,
            early=max(float(params.adaptive_shape_floor) - 0.10, 0.20),
            balanced=float(params.adaptive_shape_floor),
            strict=min(float(params.adaptive_shape_floor) + 0.12, 0.90),
        )
        if shape_score < shape_floor:
            continue

        pattern_id = (
            f"rounded-top:{bars[base_index].ts.isoformat()}:{bars[apex_index].ts.isoformat()}"
        )
        return _RoundedTopCandidate(
            pattern_id=pattern_id,
            base_index=base_index,
            apex_index=apex_index,
            apex_confirm_index=apex_confirm_index,
            base_price=base_price,
            apex_price=apex_price,
            atr=atr,
            height_atr=height_atr,
            early_slope_atr_per_bar=early_slope,
            late_slope_atr_per_bar=late_slope,
            late_to_early_ratio=slope_ratio,
            shape_score=shape_score,
            height_score=height_score,
            rise_score=rise_score,
            deceleration_score=deceleration_score,
        )
    return None


def _route_key(bars: Sequence[Bar]) -> tuple[str, str, str] | None:
    if not bars:
        return None
    first_provenance = bars[0].provenance
    if first_provenance is None:
        return None
    route_key = (
        first_provenance.provider,
        first_provenance.instrument_id,
        first_provenance.route_fingerprint,
    )
    allowed_request_types = frozenset(
        {
            BarProviderRequest.CANONICAL_STORAGE,
            BarProviderRequest.DETERMINISTIC_AGGREGATION,
        }
    )
    for bar in bars:
        if (
            authoritative_bar_reject_reason(
                route_key[0],
                bar,
                instrument_id=route_key[1],
                route_fingerprint=route_key[2],
                allowed_request_types=allowed_request_types,
                data_type="canonical_ohlcv",
            )
            is not None
        ):
            return None
    return route_key


def _causal_confirmed_prefix(bars: Sequence[Bar]) -> list[Bar]:
    """Never compress a pending slot out of the analysis time axis."""

    prefix: list[Bar] = []
    for bar in bars:
        if is_provisional_bar(bar):
            break
        prefix.append(bar)
    return prefix


def _bounded_bar_window(
    bars: Sequence[Bar],
    *,
    start: datetime,
    end: datetime,
) -> list[Bar] | None:
    """Binary-select one causal window from an ordered typed bar sequence."""

    def lower_bound(target: datetime) -> int:
        left = 0
        right = len(bars)
        while left < right:
            middle = (left + right) // 2
            item = bars[middle]
            if not isinstance(item, Bar):
                raise TypeError
            if item.ts < target:
                left = middle + 1
            else:
                right = middle
        return left

    try:
        left = lower_bound(start)
        right = lower_bound(end)
    except TypeError:
        return None
    selected = list(bars[left:right])
    if any(not isinstance(bar, Bar) for bar in selected):
        return None
    return selected


def _rejection_observations(
    bars: Sequence[Bar],
    *,
    candidate: _RoundedTopCandidate,
    params: RoundedReversalParams,
    tick_size: float,
    min_rejections: int,
    min_wick_share: float,
    min_wick_atr: float,
    source_timeframe: str,
    atr_values: Sequence[float] | None = None,
) -> dict[str, Any]:
    resolved_atr_values = (
        list(atr_values)
        if atr_values is not None and len(atr_values) == len(bars)
        else pine.atr_rma_series(bars, params.atr_len)
    )
    cluster_threshold = sensitivity_value(
        params.sensitivity,
        early=0.68,
        balanced=0.80,
        strict=0.90,
    )
    strong_threshold = sensitivity_value(
        params.sensitivity,
        early=max(float(params.adaptive_strong_bar_score) - 0.10, 0.40),
        balanced=float(params.adaptive_strong_bar_score),
        strict=min(float(params.adaptive_strong_bar_score) + 0.08, 0.95),
    )
    return adaptive_rejection_summary(
        bars,
        atr_values=resolved_atr_values,
        apex_price=candidate.apex_price,
        apex_atr=candidate.atr,
        tick_size=tick_size,
        target_wick_share=min_wick_share,
        target_wick_atr=min_wick_atr,
        target_close_pos=params.max_rejection_close_pos,
        level_tolerance_atr=params.level_tolerance_atr,
        level_tolerance_ticks=params.level_tolerance_ticks,
        target_mass=max(float(min_rejections) * 0.50, 1.0),
        max_scored_bars=max(int(min_rejections) + 1, 3),
        cluster_threshold=cluster_threshold,
        strong_bar_threshold=strong_threshold,
        min_support_bars=2,
        source_timeframe=source_timeframe,
    )


def _macro_rejection_cluster(
    bars: Sequence[Bar],
    *,
    atr_values: Sequence[float],
    candidate: _RoundedTopCandidate,
    params: RoundedReversalParams,
    tick_size: float,
) -> dict[str, Any]:
    start = max(
        candidate.base_index,
        candidate.apex_index - int(params.wick_before),
    )
    end = min(
        len(bars) - 1,
        candidate.apex_index + int(params.wick_after),
    )
    cluster = _rejection_observations(
        bars[start : end + 1],
        candidate=candidate,
        params=params,
        tick_size=tick_size,
        min_rejections=int(params.min_rejections),
        min_wick_share=float(params.min_wick_share),
        min_wick_atr=float(params.min_wick_atr),
        source_timeframe=bars[-1].timeframe,
        atr_values=atr_values[start : end + 1],
    )
    observation_ts = set(cluster["observation_ts"])
    local_indices = [
        index
        for index, bar in enumerate(bars[start : end + 1])
        if bar.ts.isoformat() in observation_ts
    ]
    ready_local_index = cluster.pop("ready_local_index", None)
    ready_parent_index = (
        start + int(ready_local_index) if isinstance(ready_local_index, int) else None
    )
    return {
        **cluster,
        "source": "macro",
        "indices": [start + index for index in local_indices],
        "ready_index": ready_parent_index,
        "ready_parent_index": ready_parent_index,
        "availability_state": ("qualified" if cluster["qualified"] else "not_qualified"),
        "reason_code": (
            "macro_rejections_qualified"
            if cluster["qualified"]
            else "macro_rejections_insufficient"
        ),
    }


def _micro_rejection_resolution(
    bars: Sequence[Bar],
    *,
    candidate: _RoundedTopCandidate,
    macro_cluster: Mapping[str, Any],
    params: RoundedReversalParams,
    tick_size: float,
    mtf_context: Mapping[str, Sequence[Bar]] | None,
    mtf_quality: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    parent_minutes = interval_minutes(bars[-1].timeframe)
    window_start_index = max(
        candidate.base_index,
        candidate.apex_index - int(params.wick_before),
    )
    window_start = bars[window_start_index].ts
    window_end = bars[candidate.apex_index].ts + timedelta(
        minutes=parent_minutes * (int(params.wick_after) + 1)
    )
    parent_end = bars[-1].ts + timedelta(minutes=parent_minutes)
    observation_end = min(parent_end, window_end)
    base_micro = {
        "availability_state": "unavailable",
        "reason_code": "micro_context_absent",
        "source_timeframe": ROUNDED_REVERSAL_MICRO_TIMEFRAME,
        "window_start_ts": window_start.isoformat(),
        "window_end_exclusive": window_end.isoformat(),
        "as_of_ts": parent_end.isoformat(),
        "window_complete": parent_end >= window_end,
        "identity_verified": False,
        "observed_bar_count": 0,
        "qualifying_count": 0,
        "ready_observation_ts": None,
        "ready_ts": None,
        "ready_parent_index": None,
        "quality": {},
    }
    base_confirmation = {
        "mode": "macro_only",
        "effective_rejection_source": "macro",
        "micro": base_micro,
    }
    if not params.use_mtf_rejections:
        base_micro["reason_code"] = "micro_context_disabled"
        return dict(macro_cluster), base_confirmation, None
    micro_minutes = interval_minutes(ROUNDED_REVERSAL_MICRO_TIMEFRAME)
    if parent_minutes <= micro_minutes or parent_minutes % micro_minutes != 0:
        base_micro.update(
            {
                "availability_state": "invalid",
                "reason_code": "micro_timeframe_not_lower",
            }
        )
        return dict(macro_cluster), base_confirmation, None
    raw_micro = (
        mtf_context.get(ROUNDED_REVERSAL_MICRO_TIMEFRAME)
        if isinstance(mtf_context, Mapping)
        else None
    )
    upstream_quality = (
        dict(mtf_quality.get(ROUNDED_REVERSAL_MICRO_TIMEFRAME) or {})
        if isinstance(mtf_quality, Mapping)
        else {}
    )
    if (
        not isinstance(raw_micro, Sequence)
        or isinstance(raw_micro, (str, bytes))
        or (not raw_micro and not upstream_quality)
    ):
        return dict(macro_cluster), base_confirmation, None
    visible_micro = _bounded_bar_window(
        raw_micro,
        start=window_start,
        end=observation_end,
    )
    if visible_micro is None:
        base_micro.update(
            {
                "availability_state": "invalid",
                "reason_code": "micro_context_untyped",
            }
        )
        return dict(macro_cluster), base_confirmation, None
    if any(
        left.ts >= right.ts
        for left, right in zip(
            visible_micro,
            visible_micro[1:],
            strict=False,
        )
    ):
        base_micro.update(
            {
                "availability_state": "invalid",
                "reason_code": "micro_context_not_strictly_ordered",
            }
        )
        return dict(macro_cluster), base_confirmation, None
    if any(bar.timeframe != ROUNDED_REVERSAL_MICRO_TIMEFRAME for bar in visible_micro):
        base_micro.update(
            {
                "availability_state": "invalid",
                "reason_code": "micro_timeframe_mismatch",
            }
        )
        return dict(macro_cluster), base_confirmation, None

    parent_window = [bar for bar in bars if window_start <= bar.ts < observation_end]
    confirmed_micro = confirmed_storage_bars(visible_micro)
    raw_due_provisional_count = sum(1 for bar in visible_micro if is_provisional_bar(bar))
    upstream_due_provisional_count = (
        upstream_quality.get("due_provisional_bar_count")
        if type(upstream_quality.get("due_provisional_bar_count")) is int
        and upstream_quality.get("due_provisional_bar_count") >= 0
        else 0
    )
    due_provisional_count = max(
        raw_due_provisional_count,
        upstream_due_provisional_count,
    )
    finality_pending = due_provisional_count > 0
    parent_route = _route_key(bars)
    micro_route = _route_key(confirmed_micro)
    identity_verified = bool(
        parent_route is not None and micro_route is not None and parent_route == micro_route
    )
    if confirmed_micro and (parent_route is None or micro_route is None):
        base_micro.update(
            {
                "availability_state": "unavailable",
                "reason_code": "micro_identity_unverified",
            }
        )
        return dict(macro_cluster), base_confirmation, None
    if parent_route is not None and micro_route is not None and parent_route != micro_route:
        base_micro.update(
            {
                "availability_state": "invalid",
                "reason_code": "micro_route_mismatch",
            }
        )
        return dict(macro_cluster), base_confirmation, None

    base_confirmation.update(
        {
            "mode": "mtf_collecting",
            "effective_rejection_source": (ROUNDED_REVERSAL_MICRO_TIMEFRAME),
        }
    )

    trimmed_micro = trim_mtf_context_to_parent(
        parent_window,
        confirmed_micro,
        ROUNDED_REVERSAL_MICRO_TIMEFRAME,
    )
    trimmed_micro = [bar for bar in trimmed_micro if window_start <= bar.ts < window_end]
    local_quality = mtf_context_quality(
        parent_window,
        trimmed_micro,
        ROUNDED_REVERSAL_MICRO_TIMEFRAME,
        min_coverage=float(params.micro_min_coverage),
    )
    quality_snapshot_as_of = parse_aware_utc_ts(
        upstream_quality.get("snapshot_as_of_ts") or upstream_quality.get("as_of_ts")
    )
    quality_coverage_end = parse_aware_utc_ts(
        upstream_quality.get("coverage_end_exclusive") or upstream_quality.get("as_of_ts")
    )
    quality_window_start = parse_aware_utc_ts(upstream_quality.get("window_start_ts"))
    quality_identity_verified = bool(
        parent_route is not None
        and upstream_quality.get("provider") == parent_route[0]
        and upstream_quality.get("instrument_id") == parent_route[1]
        and upstream_quality.get("route_fingerprint") == parent_route[2]
        and upstream_quality.get("timeframe") == ROUNDED_REVERSAL_MICRO_TIMEFRAME
        and upstream_quality.get("contract") == "provider-mtf-range-quality-v2"
    )
    quality_scope_verified = bool(
        quality_identity_verified
        and quality_snapshot_as_of == parent_end
        and quality_window_start is not None
        and quality_window_start <= window_start
        and quality_coverage_end is not None
        and quality_coverage_end >= observation_end
    )
    provider_complete = bool(
        quality_scope_verified and upstream_quality.get("provider_complete") is True
    )
    scoped_upstream_quality = upstream_quality if quality_scope_verified else {}
    if not trimmed_micro:
        base_micro.update(
            {
                "availability_state": "unavailable",
                "reason_code": "micro_confirmed_window_empty",
                "identity_verified": identity_verified,
                "quality": {
                    "local": local_quality,
                    "upstream": scoped_upstream_quality,
                    "input_admitted": False,
                    "quality_identity_verified": quality_identity_verified,
                    "quality_scope_verified": quality_scope_verified,
                    "provider_complete": provider_complete,
                    "observed_bar_count": 0,
                },
            }
        )
        return (
            dict(macro_cluster),
            {
                "mode": "macro_only",
                "effective_rejection_source": "macro",
                "micro": base_micro,
            },
            None,
        )
    input_admitted = bool(identity_verified and not finality_pending)
    micro_cluster = _rejection_observations(
        trimmed_micro,
        candidate=candidate,
        params=params,
        tick_size=tick_size,
        min_rejections=int(params.micro_min_rejections),
        min_wick_share=float(params.micro_min_wick_share),
        min_wick_atr=float(params.micro_min_wick_atr),
        source_timeframe=ROUNDED_REVERSAL_MICRO_TIMEFRAME,
    )
    ready_local_index = micro_cluster.pop("ready_local_index", None)
    ready_observation = (
        trimmed_micro[ready_local_index]
        if isinstance(ready_local_index, int) and 0 <= ready_local_index < len(trimmed_micro)
        else None
    )
    ready_parent_index = (
        pine.bar_index_at_or_before(bars, ready_observation.ts)
        if ready_observation is not None
        else None
    )
    if (
        ready_parent_index is not None
        and ready_observation is not None
        and ready_observation.ts
        > parent_context_cutoff(
            bars[ready_parent_index],
            ROUNDED_REVERSAL_MICRO_TIMEFRAME,
        )
    ):
        ready_parent_index = None
    ready_ts = (
        ready_observation.ts + timedelta(minutes=interval_minutes(ROUNDED_REVERSAL_MICRO_TIMEFRAME))
        if ready_observation is not None
        else None
    )
    planned_complete = parent_end >= window_end
    if finality_pending:
        availability_state = "collecting"
        reason_code = "micro_provider_finality_pending"
    elif micro_cluster["qualified"] and ready_parent_index is not None:
        availability_state = "qualified"
        reason_code = "micro_rejections_qualified"
    elif planned_complete:
        availability_state = "not_qualified"
        reason_code = "micro_rejections_insufficient"
    else:
        availability_state = "collecting"
        reason_code = "micro_rejection_window_open"

    observed_qualified = bool(micro_cluster.get("qualified"))
    if availability_state != "qualified":
        micro_cluster["qualified"] = False
    micro_cluster.update(
        {
            "source": "micro",
            "availability_state": availability_state,
            "reason_code": reason_code,
            "observed_qualified": observed_qualified,
            "ready_parent_index": ready_parent_index,
            "ready_ts": ready_ts.isoformat() if ready_ts is not None else None,
            "identity_verified": identity_verified,
            "window_start_ts": window_start.isoformat(),
            "window_end_exclusive": window_end.isoformat(),
            "as_of_ts": parent_end.isoformat(),
            "window_complete": planned_complete,
            "observed_bar_count": len(trimmed_micro),
            "qualifying_count": int(micro_cluster.get("count") or 0),
            "quality": {
                "local": local_quality,
                "upstream": scoped_upstream_quality,
                "input_admitted": input_admitted,
                "current_finality_ok": not finality_pending,
                "due_provisional_bar_count": due_provisional_count,
                "quality_identity_verified": quality_identity_verified,
                "quality_scope_verified": quality_scope_verified,
                "provider_complete": provider_complete,
                "eligible_range_count": (
                    upstream_quality.get("eligible_range_count")
                    if type(upstream_quality.get("eligible_range_count")) is int
                    else 0
                ),
                "observed_bar_count": len(trimmed_micro),
            },
        }
    )
    micro_public = {
        key: value for key, value in micro_cluster.items() if key != "ready_local_index"
    }
    effective = {
        **micro_public,
        "qualified": (
            bool(micro_public.get("qualified")) if availability_state == "qualified" else False
        ),
        "ready_index": (ready_parent_index if availability_state == "qualified" else None),
    }
    return (
        effective,
        {
            "mode": ("mtf_collecting" if availability_state == "collecting" else "mtf_preferred"),
            "effective_rejection_source": ROUNDED_REVERSAL_MICRO_TIMEFRAME,
            "micro": micro_public,
        },
        micro_public,
    )


def _first_rollover_index(
    bars: Sequence[Bar],
    *,
    candidate: _RoundedTopCandidate,
    params: RoundedReversalParams,
    tick_size: float,
) -> tuple[int | None, float, float, float]:
    last_drop = 0.0
    last_slope = 0.0
    last_score = 0.0
    roll_floor = sensitivity_value(
        params.sensitivity,
        early=max(float(params.adaptive_roll_floor) - 0.10, 0.30),
        balanced=float(params.adaptive_roll_floor),
        strict=min(float(params.adaptive_roll_floor) + 0.12, 0.95),
    )
    for index in range(candidate.apex_confirm_index, len(bars)):
        elapsed = max(index - candidate.apex_index, 1)
        last_drop = (
            float(bars[candidate.apex_index].close) - float(bars[index].close)
        ) / candidate.atr
        last_slope = (float(bars[index].close) - float(bars[candidate.apex_index].close)) / (
            elapsed * candidate.atr
        )
        drop_score = soft_up(
            last_drop,
            float(params.min_roll_drop_atr),
            max(0.10, float(tick_size) / max(candidate.atr, 1e-9)),
        )
        slope_score = soft_down(
            last_slope,
            float(params.max_roll_slope_atr_per_bar),
            0.04,
        )
        last_score = drop_score * 0.65 + slope_score * 0.35
        if last_score >= roll_floor:
            return index, last_drop, last_slope, last_score
    return None, last_drop, last_slope, last_score


def _stage_candidate(
    bars: Sequence[Bar],
    *,
    anatomies: Sequence[CandleAnatomy],
    candidate: _RoundedTopCandidate,
    cluster: Mapping[str, Any],
    macro_cluster: Mapping[str, Any],
    micro_cluster: Mapping[str, Any] | None,
    confirmation: Mapping[str, Any],
    params: RoundedReversalParams,
    tick_size: float,
) -> dict[str, Any]:
    current_index = len(bars) - 1
    break_buffer = max(
        tick_size * int(params.break_buffer_ticks),
        candidate.atr * float(params.break_buffer_atr),
    )
    invalidation_buffer = max(
        tick_size * int(params.invalidation_ticks),
        candidate.atr * float(params.invalidation_atr),
    )
    rise_bars = candidate.apex_index - candidate.base_index
    cap_bars = min(
        max(
            int(round(rise_bars * float(params.local_neckline_fraction))),
            int(params.phase_bars),
        ),
        8,
    )
    neckline_start = max(candidate.base_index, candidate.apex_index - cap_bars)
    neckline_rows = bars[neckline_start : candidate.apex_index]
    if neckline_rows:
        neckline_index = min(
            range(neckline_start, candidate.apex_index),
            key=lambda index: (float(bars[index].low), index),
        )
        neckline = float(bars[neckline_index].low)
    else:
        neckline_index = candidate.base_index
        neckline = candidate.base_price
    break_level = neckline - break_buffer
    invalidation = candidate.apex_price + invalidation_buffer
    theoretical_target = neckline - (candidate.apex_price - neckline)

    rollover_index, roll_drop_atr, roll_slope, roll_score = _first_rollover_index(
        bars,
        candidate=candidate,
        params=params,
        tick_size=tick_size,
    )
    cluster_ready_index = cluster.get("ready_index")
    armed_index = (
        max(int(cluster_ready_index), int(rollover_index))
        if isinstance(cluster_ready_index, int) and rollover_index is not None
        else None
    )

    breakdown_index: int | None = None
    invalidation_index: int | None = None
    for index in range(candidate.apex_confirm_index, len(bars)):
        if float(bars[index].close) > invalidation:
            invalidation_index = index
            break
        if breakdown_index is None and (
            index > 0
            and float(bars[index - 1].close) >= break_level
            and float(bars[index].close) < break_level
        ):
            breakdown_index = index

    expiry_index = candidate.apex_index + int(params.max_pattern_age)
    confirmation_micro = (
        confirmation.get("micro") if isinstance(confirmation.get("micro"), Mapping) else {}
    )
    confirmation_pending = bool(
        confirmation.get("effective_rejection_source") == ROUNDED_REVERSAL_MICRO_TIMEFRAME
        and confirmation_micro.get("availability_state") not in {"qualified", "not_qualified"}
    )
    if breakdown_index is not None and expiry_index < breakdown_index:
        state = "EXPIRED"
        transition_index = expiry_index
    elif invalidation_index is not None and (
        breakdown_index is None or invalidation_index < breakdown_index
    ):
        state = "INVALIDATED"
        transition_index = invalidation_index
    elif breakdown_index is not None and (armed_index is None or breakdown_index < armed_index):
        if confirmation_pending:
            state = "FORMING"
            transition_index = candidate.apex_confirm_index
        else:
            state = "MISSED"
            transition_index = breakdown_index
    elif breakdown_index is not None and invalidation_index is not None:
        state = "INVALIDATED"
        transition_index = invalidation_index
    elif breakdown_index is not None:
        retest_index: int | None = None
        retest_limit = min(
            len(bars),
            breakdown_index + int(params.retest_bars) + 1,
        )
        retest_floor = neckline - candidate.atr * float(params.retest_tolerance_atr)
        for index in range(breakdown_index + 1, retest_limit):
            anatomy = anatomies[index]
            if (
                float(bars[index].high) >= retest_floor
                and float(bars[index].close) < neckline
                and float(bars[index].close) < float(bars[index].open)
                and anatomy.upper_share >= 0.20
            ):
                retest_index = index
                break
        if retest_index is not None:
            state = "RETEST_CONFIRMED"
            transition_index = retest_index
        else:
            state = "CONFIRMED"
            transition_index = breakdown_index
    elif current_index >= expiry_index:
        state = "EXPIRED"
        transition_index = expiry_index
    elif armed_index is not None:
        state = "ARMED"
        transition_index = armed_index
    else:
        state = "FORMING"
        transition_index = candidate.apex_confirm_index

    neckline_break_atr = (
        (neckline - float(bars[breakdown_index].close)) / candidate.atr
        if breakdown_index is not None
        else 0.0
    )
    shape_component = candidate.shape_score * 50.0
    cluster_component = float(cluster["cluster_score"]) * 30.0
    rollover_component = roll_score * 10.0
    confirmation_component = 10.0 if state in {"CONFIRMED", "RETEST_CONFIRMED"} else 0.0
    score = pine.clamp(
        shape_component + cluster_component + rollover_component + confirmation_component,
        0.0,
        99.0,
    )

    transition_index = min(max(int(transition_index), 0), current_index)
    transition_ts = bars[transition_index].ts.isoformat()
    availability_index = min(
        max(transition_index, candidate.apex_confirm_index),
        current_index,
    )
    available_at_ts = (
        bars[availability_index].ts
        + timedelta(minutes=interval_minutes(bars[availability_index].timeframe))
    ).isoformat()
    shape_floor = sensitivity_value(
        params.sensitivity,
        early=max(float(params.adaptive_shape_floor) - 0.10, 0.20),
        balanced=float(params.adaptive_shape_floor),
        strict=min(float(params.adaptive_shape_floor) + 0.12, 0.90),
    )
    roll_floor = sensitivity_value(
        params.sensitivity,
        early=max(float(params.adaptive_roll_floor) - 0.10, 0.30),
        balanced=float(params.adaptive_roll_floor),
        strict=min(float(params.adaptive_roll_floor) + 0.12, 0.95),
    )
    return {
        "track": "slow",
        "pattern_id": candidate.pattern_id,
        "generation_id": f"slow:{candidate.pattern_id}",
        "reanchored_from_pattern_id": None,
        "state": state,
        "state_code": state.lower(),
        "trigger_state": _trigger_code(state),
        "direction": Direction.SHORT.value,
        "score": round(score, 2),
        "transition_index": transition_index,
        "transition_ts": transition_ts,
        "available_at_ts": available_at_ts,
        "event_on_latest": transition_index == current_index,
        "active": state in _ACTIVE_STATES,
        "terminal": state in _TERMINAL_STATES,
        "confirmation_pending": confirmation_pending,
        "armed_index": armed_index,
        "breakdown_index": breakdown_index,
        "anchors": {
            "base": {
                "index": candidate.base_index,
                "ts": bars[candidate.base_index].ts.isoformat(),
                "price": round(candidate.base_price, 6),
            },
            "apex": {
                "index": candidate.apex_index,
                "ts": bars[candidate.apex_index].ts.isoformat(),
                "price": round(candidate.apex_price, 6),
                "confirmed_index": candidate.apex_confirm_index,
                "confirmed_ts": bars[candidate.apex_confirm_index].ts.isoformat(),
            },
            "local_neckline": {
                "index": neckline_index,
                "ts": bars[neckline_index].ts.isoformat(),
                "price": round(neckline, 6),
            },
        },
        "geometry": {
            "rise_bars": rise_bars,
            "height_atr": round(candidate.height_atr, 4),
            "early_slope_atr_per_bar": round(
                candidate.early_slope_atr_per_bar,
                4,
            ),
            "late_slope_atr_per_bar": round(
                candidate.late_slope_atr_per_bar,
                4,
            ),
            "late_to_early_ratio": round(
                candidate.late_to_early_ratio,
                4,
            ),
            "shape_score": round(candidate.shape_score, 4),
            "height_score": round(candidate.height_score, 4),
            "rise_score": round(candidate.rise_score, 4),
            "deceleration_score": round(candidate.deceleration_score, 4),
            "cap_bars": cap_bars,
        },
        "rejection_cluster": dict(macro_cluster),
        "micro_rejection_cluster": (
            dict(micro_cluster) if isinstance(micro_cluster, Mapping) else None
        ),
        "confirmation": dict(confirmation),
        "rollover": {
            "index": rollover_index,
            "drop_atr": round(roll_drop_atr, 4),
            "slope_atr_per_bar": round(roll_slope, 4),
            "score": round(roll_score, 4),
        },
        "levels": {
            "apex": round(candidate.apex_price, 6),
            "origin_base": round(candidate.base_price, 6),
            "local_neckline": round(neckline, 6),
            "trigger": round(break_level, 6),
            "neckline": round(neckline, 6),
            "break_level": round(break_level, 6),
            "structural_invalidation": round(invalidation, 6),
            "theoretical_target": round(theoretical_target, 6),
            "rejection_zone_top": macro_cluster.get("zone_top"),
            "rejection_zone_bottom": macro_cluster.get("zone_bottom"),
            "effective_rejection_zone_top": cluster.get("zone_top"),
            "effective_rejection_zone_bottom": cluster.get("zone_bottom"),
        },
        "metrics": {
            "pattern_age_bars": current_index - candidate.apex_index,
            "height_atr": round(candidate.height_atr, 4),
            "early_slope_atr_per_bar": round(
                candidate.early_slope_atr_per_bar,
                4,
            ),
            "late_slope_atr_per_bar": round(
                candidate.late_slope_atr_per_bar,
                4,
            ),
            "late_to_early_ratio": round(
                candidate.late_to_early_ratio,
                4,
            ),
            "rejection_count": int(cluster.get("count") or 0),
            "macro_rejection_count": int(macro_cluster.get("count") or 0),
            "micro_rejection_count": int((micro_cluster or {}).get("count") or 0),
            "roll_drop_atr": round(roll_drop_atr, 4),
            "roll_slope_atr_per_bar": round(roll_slope, 4),
            "roll_score": round(roll_score, 4),
            "shape_score": round(candidate.shape_score, 4),
            "cluster_score": round(float(cluster["cluster_score"]), 4),
            "neckline_break_atr": round(neckline_break_atr, 4),
        },
        "adaptive_thresholds": {
            "sensitivity": params.sensitivity,
            "shape_score": round(shape_floor, 4),
            "roll_score": round(roll_floor, 4),
            "local_neckline_fraction": round(float(params.local_neckline_fraction), 4),
            **dict(cluster.get("adaptive_thresholds") or {}),
        },
        "gate_margins": {
            "shape_score": round(candidate.shape_score - shape_floor, 4),
            "cluster_score": round(
                float(cluster["cluster_score"])
                - float(cluster["adaptive_thresholds"]["cluster_score"]),
                4,
            ),
            "roll_score": round(roll_score - roll_floor, 4),
        },
        "evidence_groups": [
            {"group": "shape", "score": round(candidate.shape_score, 4)},
            {
                "group": "auction_rejection",
                "score": round(float(cluster["cluster_score"]), 4),
            },
            {"group": "rollover", "score": round(roll_score, 4)},
        ],
    }


def _context_row_ts(
    row: Mapping[str, Any],
    field_name: str,
    *,
    start: datetime,
    end: datetime,
) -> datetime | None:
    parsed = parse_aware_utc_ts(row.get(field_name))
    if parsed is None or not (start <= parsed < end):
        return None
    return parsed


def _linked_context_evidence(
    bars: Sequence[Bar],
    *,
    candidate: _RoundedTopCandidate,
    latest: Mapping[str, Any],
    effective_cluster: Mapping[str, Any],
    indicator_context: Mapping[str, Any] | None,
    params: RoundedReversalParams,
) -> dict[str, Any]:
    base_score = float(latest["score"])
    empty = {
        "mode": "solo",
        "links": {},
        "evidence": [],
        "evidence_groups": [],
        "advisory_score_delta": 0.0,
        "base_score": round(base_score, 2),
        "contextual_score": round(base_score, 2),
        "calibrated": False,
        "authoritative_for_lifecycle": False,
        "identity_verified": False,
    }
    if not params.use_linked_context or not isinstance(
        indicator_context,
        Mapping,
    ):
        return empty
    mapping_module_names = (
        "absorption_trap",
        "tick_flow",
        "channel_master",
        "market_spotlight",
    )
    parent_route = _route_key(bars)
    context_identity = indicator_context.get("identity")
    context_identity = context_identity if isinstance(context_identity, Mapping) else {}
    identity_verified = bool(
        parent_route is not None
        and context_identity.get("provider") == parent_route[0]
        and context_identity.get("instrument_id") == parent_route[1]
        and context_identity.get("route_fingerprint") == parent_route[2]
    )
    present_modules = [
        module_name
        for module_name in mapping_module_names
        if isinstance(indicator_context.get(module_name), Mapping)
    ]
    raw_vsa_facts = indicator_context.get("vsa_facts")
    if (
        isinstance(raw_vsa_facts, Sequence)
        and not isinstance(raw_vsa_facts, (str, bytes))
        and raw_vsa_facts
    ):
        present_modules.append("vsa_volume")
    if present_modules and not identity_verified:
        return {
            **empty,
            "links": {
                module_name: {
                    "state": "ignored",
                    "reason_code": "context_route_unverified",
                }
                for module_name in present_modules
            },
        }

    confirmation = latest.get("confirmation")
    micro = (
        confirmation.get("micro")
        if isinstance(confirmation, Mapping) and isinstance(confirmation.get("micro"), Mapping)
        else {}
    )
    window_start = parse_aware_utc_ts(micro.get("window_start_ts"))
    window_end = parse_aware_utc_ts(micro.get("window_end_exclusive"))
    parent_minutes = interval_minutes(bars[-1].timeframe)
    current_end = bars[-1].ts + timedelta(minutes=parent_minutes)
    apex_ts = bars[candidate.apex_index].ts
    if window_start is None:
        window_start = apex_ts - timedelta(minutes=parent_minutes * int(params.wick_before))
    if window_end is None:
        window_end = apex_ts + timedelta(minutes=parent_minutes * (int(params.wick_after) + 1))
    rejection_end = min(window_end, current_end)
    raw_zone_top = float_or_none(effective_cluster.get("zone_top"))
    raw_zone_bottom = float_or_none(effective_cluster.get("zone_bottom"))
    zone_top = candidate.apex_price if raw_zone_top is None else raw_zone_top
    zone_bottom = (
        (candidate.apex_price - candidate.atr * float(params.level_tolerance_atr))
        if raw_zone_bottom is None
        else raw_zone_bottom
    )
    level_tolerance = max(
        candidate.atr * float(params.level_tolerance_atr),
        1e-9,
    )

    evidence: list[dict[str, Any]] = []
    links: dict[str, dict[str, Any]] = {}

    if isinstance(raw_vsa_facts, Sequence) and not isinstance(raw_vsa_facts, (str, bytes)):
        vsa_rows = [
            fact
            for fact in raw_vsa_facts[-max(int(params.scan_bars) * 2, 128) :]
            if (isinstance(fact, VsaFact) and fact.bar.ts <= bars[-1].ts)
        ]
        vsa_route = _route_key([fact.bar for fact in vsa_rows])
        if (
            vsa_rows
            and identity_verified
            and parent_route is not None
            and vsa_route == parent_route
            and vsa_rows[-1].bar.ts == bars[-1].ts
        ):
            vsa_support: list[dict[str, Any]] = []
            vsa_opposition: list[dict[str, Any]] = []
            for fact in vsa_rows:
                ts = fact.bar.ts
                if not (window_start <= ts < current_end):
                    continue
                code = str(fact.primary_code or fact.code or "").upper()
                role = str(fact.absorption_role or "").lower()
                within_rejection = ts < rejection_end
                item = {
                    "source": "vsa_facts",
                    "evidence_ts": ts.isoformat(),
                    "group": "volume_behavior",
                    "authority": "confirmed",
                }
                if within_rejection and (
                    code in VSA_REVERSAL_SHORT_CODES
                    or (code in VSA_ABSORPTION_CODES and role == "resistance")
                ):
                    vsa_support.append(
                        {
                            **item,
                            "code": (
                                "vsa_resistance_absorption"
                                if code in VSA_ABSORPTION_CODES
                                else f"vsa_{code.lower()}"
                            ),
                            "alignment": "supporting",
                            "weight": 4.0,
                            "vsa_code": code,
                        }
                    )
                elif ts >= apex_ts and code in VSA_IMPULSE_CODES and code.endswith("_DN"):
                    vsa_support.append(
                        {
                            **item,
                            "code": "vsa_right_side_sell_effort",
                            "alignment": "supporting",
                            "weight": 3.0,
                            "vsa_code": code,
                        }
                    )
                elif ts >= apex_ts and code in VSA_IMPULSE_CODES and code.endswith("_UP"):
                    vsa_opposition.append(
                        {
                            **item,
                            "code": "vsa_bullish_continuation_flow",
                            "alignment": "opposing",
                            "weight": -4.0,
                            "vsa_code": code,
                        }
                    )
            if vsa_support:
                evidence.append(
                    max(
                        vsa_support,
                        key=lambda item: (
                            float(item.get("weight") or 0.0),
                            str(item.get("evidence_ts") or ""),
                        ),
                    )
                )
            if vsa_opposition:
                evidence.append(vsa_opposition[-1])
            links["vsa_volume"] = {
                "state": "linked",
                "reason_code": "typed_confirmed_context",
                "supporting_count": len(vsa_support),
                "opposing_count": len(vsa_opposition),
            }
        else:
            links["vsa_volume"] = {
                "state": "ignored",
                "reason_code": "vsa_facts_stale_or_route_unverified",
            }

    absorption = indicator_context.get("absorption_trap")
    absorption_support: dict[str, Any] | None = None
    if isinstance(absorption, Mapping):
        rows = absorption.get("events")
        rows = rows if isinstance(rows, list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            event_ts = _context_row_ts(
                row,
                "ts",
                start=window_start,
                end=rejection_end,
            )
            attack_ts = _context_row_ts(
                row,
                "attack_ts",
                start=window_start,
                end=rejection_end,
            )
            signal = row.get("signal")
            signal = signal if isinstance(signal, Mapping) else {}
            level = float_or_none(row.get("level"))
            if (
                event_ts is None
                or attack_ts is None
                or row.get("code") != "ABS_HIGH"
                or row.get("state") != "FAILED_HIGH"
                or row.get("direction") != Direction.SHORT.value
                or signal.get("confirmed") is not True
                or level is None
                or not (zone_bottom - level_tolerance <= level <= zone_top + level_tolerance)
            ):
                continue
            pressure_source = str(row.get("pressure_source") or "candle_proxy")
            absorption_support = {
                "source": "absorption_trap",
                "code": (
                    "absorption_high_tick_confirmed"
                    if pressure_source == "tick"
                    else "absorption_high_confirmed"
                ),
                "alignment": "supporting",
                "evidence_ts": event_ts.isoformat(),
                "group": "auction_rejection",
                "authority": ("tick" if pressure_source == "tick" else "candle_proxy"),
                "weight": 4.0 if pressure_source == "tick" else 2.5,
                "level": round(level, 6),
            }
        if absorption_support is not None:
            evidence.append(absorption_support)
            links["absorption_trap"] = {
                "state": "linked",
                "reason_code": "failed_high_aligned",
            }
        else:
            links["absorption_trap"] = {
                "state": "ignored",
                "reason_code": "no_aligned_failed_high",
            }

    tick_flow = indicator_context.get("tick_flow")
    if isinstance(tick_flow, Mapping):
        tick_instrument_id = tick_flow.get("instrument_id")
        tick_identity_verified = bool(
            parent_route is not None
            and tick_flow.get("provider") == parent_route[0]
            and tick_instrument_id == parent_route[1]
            and tick_flow.get("route_fingerprint") == parent_route[2]
        )
        if not tick_identity_verified:
            links["tick_flow"] = {
                "state": "ignored",
                "reason_code": "tick_flow_route_unverified",
            }
        elif tick_flow.get("decision_eligible") is not True:
            links["tick_flow"] = {
                "state": "advisory_only",
                "reason_code": "tick_flow_not_decision_eligible",
            }
        else:
            rows = tick_flow.get("delta")
            rows = rows if isinstance(rows, list) else []
            rows = rows[-max(int(params.scan_bars) * 5, 500) :]
            total_volume = 0.0
            net_delta = 0.0
            row_count = 0
            last_evidence_ts: datetime | None = None
            cluster_timeframe = str(effective_cluster.get("source_timeframe") or bars[-1].timeframe)
            tail_minutes = interval_minutes(cluster_timeframe)
            tail_windows: list[tuple[datetime, datetime]] = []
            for raw_ts in effective_cluster.get("observation_ts") or []:
                observation_ts = parse_aware_utc_ts(raw_ts)
                if observation_ts is not None:
                    tail_windows.append(
                        (
                            observation_ts,
                            observation_ts + timedelta(minutes=tail_minutes),
                        )
                    )
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                ts = _context_row_ts(
                    row,
                    "ts",
                    start=window_start,
                    end=rejection_end,
                )
                if ts is None:
                    continue
                if not any(tail_start <= ts < tail_end for tail_start, tail_end in tail_windows):
                    continue
                total = float_or_none(row.get("total_volume"))
                net = float_or_none(row.get("net_delta"))
                if total is None or net is None or total <= 0:
                    continue
                total_volume += total
                net_delta += net
                row_count += 1
                last_evidence_ts = ts
            delta_ratio = net_delta / total_volume if total_volume > 0 else 0.0
            if (
                row_count > 0
                and delta_ratio >= 0.15
                and effective_cluster.get("qualified") is True
                and absorption_support is None
            ):
                evidence.append(
                    {
                        "source": "tick_flow",
                        "code": "buy_aggression_no_progress_advisory",
                        "alignment": "supporting",
                        "evidence_ts": (
                            last_evidence_ts.isoformat() if last_evidence_ts is not None else None
                        ),
                        "group": "auction_rejection",
                        "authority": "advisory",
                        "weight": 2.0,
                        "delta_ratio": round(delta_ratio, 4),
                        "row_count": row_count,
                    }
                )
                reason_code = "positive_delta_rejection_advisory"
            elif absorption_support is not None:
                reason_code = "superseded_by_absorption_trap"
            else:
                reason_code = "no_aligned_positive_delta_rejection"
            links["tick_flow"] = {
                "state": "linked",
                "reason_code": reason_code,
                "row_count": row_count,
                "delta_ratio": round(delta_ratio, 4),
            }

    channel = indicator_context.get("channel_master")
    if isinstance(channel, Mapping):
        analysis_ts = parse_aware_utc_ts(channel.get("analysis_ts"))
        nearest = channel.get("nearest")
        nearest = nearest if isinstance(nearest, Mapping) else {}
        price = float_or_none(nearest.get("price"))
        distance_ok = bool(
            price is not None and abs(price - candidate.apex_price) <= candidate.atr * 0.35
        )
        if analysis_ts != bars[-1].ts or not distance_ok:
            links["channel_master"] = {
                "state": "ignored",
                "reason_code": "channel_context_stale_or_distant",
            }
        else:
            break_score = float_or_none(nearest.get("break_score"))
            opposing = bool(
                nearest.get("no_fade") is True
                or (nearest.get("primary_path") == "break" and (break_score or 0.0) >= 70.0)
            )
            supporting = bool(
                not opposing
                and nearest.get("direction") == Direction.SHORT.value
                and (
                    nearest.get("touch") is True
                    or nearest.get("reclaim") is True
                    or nearest.get("wick_reject") is True
                )
            )
            if supporting:
                evidence.append(
                    {
                        "source": "channel_master",
                        "code": "current_resistance_confluence",
                        "alignment": "supporting",
                        "evidence_ts": analysis_ts.isoformat(),
                        "group": "location",
                        "authority": "current_context",
                        "weight": 3.0,
                        "price": round(float(price), 6),
                    }
                )
                reason_code = "current_resistance_aligned"
            elif opposing:
                evidence.append(
                    {
                        "source": "channel_master",
                        "code": "channel_break_no_fade",
                        "alignment": "opposing",
                        "evidence_ts": analysis_ts.isoformat(),
                        "group": "location",
                        "authority": "current_context",
                        "weight": -4.0,
                        "price": round(float(price), 6),
                    }
                )
                reason_code = "current_channel_opposes_fade"
            else:
                reason_code = "current_channel_neutral"
            links["channel_master"] = {
                "state": "linked",
                "reason_code": reason_code,
            }

    spotlight = indicator_context.get("market_spotlight")
    if isinstance(spotlight, Mapping):
        latest_spotlight = spotlight.get("latest")
        latest_spotlight = latest_spotlight if isinstance(latest_spotlight, Mapping) else {}
        market_context = spotlight.get("market_context")
        market_context = market_context if isinstance(market_context, Mapping) else {}
        spotlight_ts = parse_aware_utc_ts(latest_spotlight.get("ts"))
        vwap = market_context.get("vwap")
        vwap = vwap if isinstance(vwap, Mapping) else {}
        if spotlight_ts == bars[-1].ts and market_context.get("closed") is True:
            continuation_risk = bool(
                vwap.get("upper_2_ride") is True
                or str(market_context.get("side_lock") or "").upper() == "LONG"
                or market_context.get("contra_short_locked") is True
            )
            if continuation_risk:
                evidence.append(
                    {
                        "source": "market_spotlight",
                        "code": "bullish_runaway_context",
                        "alignment": "opposing",
                        "evidence_ts": spotlight_ts.isoformat(),
                        "group": "market_regime",
                        "authority": "confirmed",
                        "weight": -4.0,
                    }
                )
                reason_code = "bullish_continuation_risk"
            else:
                reason_code = "current_market_context_neutral"
            links["market_spotlight"] = {
                "state": "linked",
                "reason_code": reason_code,
            }
        else:
            links["market_spotlight"] = {
                "state": "ignored",
                "reason_code": "market_context_stale_or_unconfirmed",
            }

    group_values: dict[str, float] = {}
    for item in evidence:
        group = str(item.get("group") or "other")
        weight = float(item.get("weight") or 0.0)
        group_values[group] = pine.clamp(
            group_values.get(group, 0.0) + weight,
            -4.0,
            4.0,
        )
    advisory_delta = pine.clamp(sum(group_values.values()), -8.0, 8.0)
    evidence_groups = [
        {
            "group": group,
            "weight": round(weight, 2),
        }
        for group, weight in sorted(group_values.items())
    ]
    return {
        "mode": "linked" if links else "solo",
        "links": links,
        "evidence": evidence,
        "evidence_groups": evidence_groups,
        "advisory_score_delta": round(advisory_delta, 2),
        "base_score": round(base_score, 2),
        "contextual_score": round(
            pine.clamp(base_score + advisory_delta, 0.0, 99.0),
            2,
        ),
        "calibrated": False,
        "authoritative_for_lifecycle": False,
        "identity_verified": identity_verified,
    }


def _trigger_code(state: str) -> str:
    return {
        "FORMING": "rounded_top_slow_forming",
        "ARMED": "rounded_top_slow_armed",
        "CONFIRMED": "rounded_top_slow_neckline_break",
        "RETEST_CONFIRMED": "rounded_top_slow_retest_rejection",
        "MISSED": "rounded_top_slow_break_before_armed",
        "INVALIDATED": "rounded_top_slow_invalidated",
        "EXPIRED": "rounded_top_slow_expired",
    }[state]


def _fact_fields(latest: Mapping[str, Any]) -> dict[str, Any]:
    state = str(latest.get("state") or "FORMING")
    metrics = dict(latest.get("metrics")) if isinstance(latest.get("metrics"), Mapping) else {}
    levels = latest.get("levels") if isinstance(latest.get("levels"), Mapping) else {}
    macro_cluster = (
        latest.get("rejection_cluster")
        if isinstance(latest.get("rejection_cluster"), Mapping)
        else {}
    )
    confirmation = (
        latest.get("confirmation") if isinstance(latest.get("confirmation"), Mapping) else {}
    )
    micro_cluster = (
        latest.get("micro_rejection_cluster")
        if isinstance(latest.get("micro_rejection_cluster"), Mapping)
        else {}
    )
    cluster = (
        micro_cluster
        if confirmation.get("effective_rejection_source") == ROUNDED_REVERSAL_MICRO_TIMEFRAME
        else macro_cluster
    )
    rollover = latest.get("rollover") if isinstance(latest.get("rollover"), Mapping) else {}
    linked = (
        latest.get("linked_context") if isinstance(latest.get("linked_context"), Mapping) else {}
    )
    if linked:
        metrics.update(
            {
                "contextual_score": linked.get("contextual_score"),
                "context_advisory_delta": linked.get("advisory_score_delta"),
            }
        )
    supporting: list[dict[str, Any]] = [
        {
            "code": "prior_uptrend",
            "height_atr": metrics.get("height_atr"),
        },
        {
            "code": "decelerating_ascent",
            "early_slope_atr_per_bar": metrics.get("early_slope_atr_per_bar"),
            "late_slope_atr_per_bar": metrics.get("late_slope_atr_per_bar"),
        },
    ]
    if cluster.get("qualified"):
        supporting.append(
            {
                "code": (
                    "lower_timeframe_upper_wick_cluster"
                    if confirmation.get("effective_rejection_source")
                    == ROUNDED_REVERSAL_MICRO_TIMEFRAME
                    else "upper_wick_cluster"
                ),
                "count": cluster.get("count"),
                "mean_upper_share": cluster.get("mean_upper_share"),
                "source_timeframe": cluster.get("source_timeframe"),
            }
        )
    if isinstance(latest.get("armed_index"), int):
        supporting.append(
            {
                "code": "right_side_rollover",
                "drop_atr": rollover.get("drop_atr"),
                "slope_atr_per_bar": rollover.get("slope_atr_per_bar"),
            }
        )
    if state in {"CONFIRMED", "RETEST_CONFIRMED"}:
        supporting.append(
            {
                "code": "neckline_close_break",
                "distance_atr": metrics.get("neckline_break_atr"),
            }
        )
    if state == "RETEST_CONFIRMED":
        supporting.append({"code": "neckline_retest_rejection"})

    opposing: list[dict[str, Any]] = []
    if not cluster.get("qualified"):
        opposing.append(
            {
                "code": (
                    "lower_timeframe_rejections_not_qualified"
                    if confirmation.get("effective_rejection_source")
                    == ROUNDED_REVERSAL_MICRO_TIMEFRAME
                    else "upper_wick_cluster_incomplete"
                ),
                "count": cluster.get("count"),
            }
        )
    if state == "MISSED":
        opposing.append({"code": "neckline_cross_preceded_arming"})
    elif state == "INVALIDATED":
        opposing.append({"code": "apex_close_invalidation"})
    elif state == "EXPIRED":
        opposing.append({"code": "pattern_confirmation_expired"})

    for item in linked.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        fact = {
            key: value for key, value in item.items() if key not in {"alignment", "weight", "group"}
        }
        if item.get("alignment") == "supporting":
            supporting.append(fact)
        elif item.get("alignment") == "opposing":
            opposing.append(fact)

    context_facts: list[dict[str, Any]] = [
        {"code": "confirmed_bars_only"},
        {
            "code": "frozen_neckline",
            "price": levels.get("neckline"),
        },
        {
            "code": "rejection_source",
            "source": confirmation.get(
                "effective_rejection_source",
                "macro",
            ),
            "micro_state": (
                (confirmation.get("micro") or {}).get("availability_state")
                if isinstance(confirmation.get("micro"), Mapping)
                else None
            ),
        },
    ]
    if linked:
        context_facts.append(
            {
                "code": "optional_context_mode",
                "mode": linked.get("mode"),
                "calibrated": linked.get("calibrated"),
                "authoritative_for_lifecycle": linked.get("authoritative_for_lifecycle"),
            }
        )

    return indicator_fact_payload(
        scenario="rounded_top_slow",
        setup="rounded_reversal_slow",
        trigger_event={"code": _trigger_code(state)},
        supporting=supporting,
        opposing=opposing,
        context=context_facts,
        risk={
            "code": "apex_close_invalidation",
            "price": levels.get("structural_invalidation"),
        },
        quality={
            "code": "rounded_top_quality",
            "value": latest.get("score"),
        },
        fact_groups=[
            {
                "kind": "rounded_top_structure",
                "items": supporting,
            }
        ],
        metrics=dict(metrics),
    )


def _overlay_items(
    bars: Sequence[Bar],
    latest: Mapping[str, Any],
    *,
    display: Mapping[str, bool],
) -> list[dict[str, Any]]:
    state = str(latest.get("state") or "")
    if state not in _ACTIVE_STATES and state != "FORMING":
        return []
    levels = latest.get("levels") if isinstance(latest.get("levels"), Mapping) else {}
    macro_cluster = (
        latest.get("rejection_cluster")
        if isinstance(latest.get("rejection_cluster"), Mapping)
        else {}
    )
    confirmation = (
        latest.get("confirmation") if isinstance(latest.get("confirmation"), Mapping) else {}
    )
    micro_cluster = (
        latest.get("micro_rejection_cluster")
        if isinstance(latest.get("micro_rejection_cluster"), Mapping)
        else {}
    )
    cluster = (
        micro_cluster
        if confirmation.get("effective_rejection_source") == ROUNDED_REVERSAL_MICRO_TIMEFRAME
        else macro_cluster
    )
    anchors = latest.get("anchors") if isinstance(latest.get("anchors"), Mapping) else {}
    apex = anchors.get("apex") if isinstance(anchors.get("apex"), Mapping) else {}
    start_ts = str(cluster.get("first_ts") or apex.get("ts") or "")
    end_ts = bars[-1].ts.isoformat()
    fact_fields = _fact_fields(latest)
    animate = bool(latest.get("event_on_latest")) and state in {
        "CONFIRMED",
        "RETEST_CONFIRMED",
    }
    items: list[dict[str, Any]] = []
    effective_zone_top = float_or_none(levels.get("effective_rejection_zone_top"))
    effective_zone_bottom = float_or_none(levels.get("effective_rejection_zone_bottom"))

    if display.get("zone", True):
        zone = overlays.box(
            start_ts=start_ts,
            end_ts=end_ts,
            top=float(
                levels["rejection_zone_top"] if effective_zone_top is None else effective_zone_top
            ),
            bottom=float(
                levels["rejection_zone_bottom"]
                if effective_zone_bottom is None
                else effective_zone_bottom
            ),
            tone="negative" if state in {"CONFIRMED", "RETEST_CONFIRMED"} else "warning",
            opacity=0.0,
            interactive=True,
            role="rounded_reversal_slow_zone",
            direction=Direction.SHORT,
            fact_fields=fact_fields,
        )
        zone.update(
            {
                "source": ROUNDED_REVERSAL_SOURCE,
                "style": "dashed",
                "width": 1.6 if state in {"CONFIRMED", "RETEST_CONFIRMED"} else 1.0,
                "border_opacity": 0.84 if state in {"CONFIRMED", "RETEST_CONFIRMED"} else 0.45,
                "animated_border": animate,
                "event_code": _trigger_code(state),
            }
        )
        items.append(zone)

    if display.get("neckline", True):
        neckline = overlays.line(
            start_ts=str((anchors.get("local_neckline") or {}).get("ts") or ""),
            end_ts=end_ts,
            price=float(levels["neckline"]),
            label_text=None,
            tone="negative" if state in {"CONFIRMED", "RETEST_CONFIRMED"} else "warning",
            width=1.0,
            opacity=0.72,
            interactive=True,
            style="dotted",
            role="rounded_reversal_slow_neckline",
            direction=Direction.SHORT,
            fact_fields=fact_fields,
        )
        neckline.update(
            {
                "source": ROUNDED_REVERSAL_SOURCE,
                "event_code": _trigger_code(state),
            }
        )
        items.append(neckline)
    return items


def _latest_candidate(
    bars: Sequence[Bar],
    *,
    params: RoundedReversalParams,
    tick_size: float,
    mtf_context: Mapping[str, Sequence[Bar]] | None,
    mtf_quality: Mapping[str, Mapping[str, Any]] | None,
    indicator_context: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if len(bars) < max(params.min_rise_bars + params.pivot_len * 2 + 1, 10):
        return None
    atr_values = pine.atr_rma_series(bars, params.atr_len)
    candidate = _shape_candidate(
        bars,
        atr_values=atr_values,
        params=params,
    )
    if candidate is None:
        return None
    parent_step = timedelta(minutes=interval_minutes(bars[-1].timeframe))
    pattern_axis = bars[candidate.base_index :]
    if any(
        right.ts - left.ts != parent_step
        for left, right in zip(
            pattern_axis,
            pattern_axis[1:],
            strict=False,
        )
    ):
        return None
    anatomies = candle_anatomies(bars)
    macro_cluster = _macro_rejection_cluster(
        bars,
        atr_values=atr_values,
        candidate=candidate,
        params=params,
        tick_size=tick_size,
    )
    cluster, confirmation, micro_cluster = _micro_rejection_resolution(
        bars,
        candidate=candidate,
        macro_cluster=macro_cluster,
        params=params,
        tick_size=tick_size,
        mtf_context=mtf_context,
        mtf_quality=mtf_quality,
    )
    latest = _stage_candidate(
        bars,
        anatomies=anatomies,
        candidate=candidate,
        cluster=cluster,
        macro_cluster=macro_cluster,
        micro_cluster=micro_cluster,
        confirmation=confirmation,
        params=params,
        tick_size=tick_size,
    )
    latest["linked_context"] = _linked_context_evidence(
        bars,
        candidate=candidate,
        latest=latest,
        effective_cluster=cluster,
        indicator_context=indicator_context,
        params=params,
    )
    latest.update(_fact_fields(latest))
    return latest


def rounded_reversal(
    bars: Sequence[Bar],
    *,
    tick_size: float,
    params: RoundedReversalParams | None = None,
    display: Mapping[str, bool] | None = None,
    mtf_context: Mapping[str, Sequence[Bar]] | None = None,
    mtf_quality: Mapping[str, Mapping[str, Any]] | None = None,
    indicator_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the latest causal Rounded Top lifecycle from confirmed bars."""

    if (
        isinstance(tick_size, bool)
        or not isinstance(tick_size, (int, float))
        or not isfinite(float(tick_size))
        or float(tick_size) <= 0
    ):
        raise ValueError("ROUNDED_REVERSAL_TICK_SIZE_INVALID")
    resolved_params = params or RoundedReversalParams()
    confirmed = _causal_confirmed_prefix(bars)
    if any(left.ts >= right.ts for left, right in zip(confirmed, confirmed[1:], strict=False)):
        raise ValueError("ROUNDED_REVERSAL_BARS_NOT_STRICTLY_ORDERED")
    empty = {
        "version": ROUNDED_REVERSAL_VERSION,
        "settings": {
            "track": "slow",
            "track_mode": resolved_params.track_mode,
            "sensitivity": resolved_params.sensitivity,
            "scan_bars": resolved_params.scan_bars,
            "pivot_len": resolved_params.pivot_len,
            "min_height_atr": resolved_params.min_height_atr,
            "min_rejections": resolved_params.min_rejections,
            "use_mtf_rejections": resolved_params.use_mtf_rejections,
            "micro_timeframe": ROUNDED_REVERSAL_MICRO_TIMEFRAME,
            "micro_min_rejections": (resolved_params.micro_min_rejections),
            "micro_min_coverage": resolved_params.micro_min_coverage,
            "use_linked_context": resolved_params.use_linked_context,
        },
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
        "signals": [],
    }
    if not confirmed:
        return empty

    required_bars = max(
        resolved_params.min_rise_bars + resolved_params.pivot_len * 2 + 1,
        10,
    )
    if len(confirmed) < required_bars:
        return {
            **empty,
            "availability": {
                "state": "blocked",
                "reason_code": "rounded_reversal_warmup",
                "required_bars": required_bars,
                "available_bars": len(confirmed),
            },
        }

    history_limit = max(
        int(resolved_params.scan_bars),
        int(resolved_params.max_rise_bars)
        + int(resolved_params.max_pattern_age)
        + int(resolved_params.pivot_len) * 2
        + 8,
    )
    offset = max(len(confirmed) - history_limit, 0)
    working = confirmed[offset:]
    latest = _latest_candidate(
        working,
        params=resolved_params,
        tick_size=float(tick_size),
        mtf_context=mtf_context,
        mtf_quality=mtf_quality,
        indicator_context=indicator_context,
    )
    if latest is None:
        return empty

    for anchor in (latest.get("anchors") or {}).values():
        if isinstance(anchor, dict):
            if isinstance(anchor.get("index"), int):
                anchor["index"] += offset
            if isinstance(anchor.get("confirmed_index"), int):
                anchor["confirmed_index"] += offset
    for field in (
        "transition_index",
        "armed_index",
        "breakdown_index",
    ):
        if isinstance(latest.get(field), int):
            latest[field] += offset
    rollover = latest.get("rollover")
    if isinstance(rollover, dict) and isinstance(rollover.get("index"), int):
        rollover["index"] += offset
    cluster = latest.get("rejection_cluster")
    if isinstance(cluster, dict):
        cluster["indices"] = [int(index) + offset for index in cluster.get("indices") or []]
        if isinstance(cluster.get("ready_index"), int):
            cluster["ready_index"] += offset
        if isinstance(cluster.get("ready_parent_index"), int):
            cluster["ready_parent_index"] += offset
    micro_cluster = latest.get("micro_rejection_cluster")
    if isinstance(micro_cluster, dict) and isinstance(
        micro_cluster.get("ready_parent_index"),
        int,
    ):
        micro_cluster["ready_parent_index"] += offset
    confirmation = latest.get("confirmation")
    confirmation_micro = (
        confirmation.get("micro")
        if isinstance(confirmation, dict) and isinstance(confirmation.get("micro"), dict)
        else None
    )
    if isinstance(confirmation_micro, dict) and isinstance(
        confirmation_micro.get("ready_parent_index"),
        int,
    ):
        confirmation_micro["ready_parent_index"] += offset

    event: dict[str, Any] | None = None
    if latest.get("event_on_latest"):
        transition_code = _trigger_code(str(latest["state"]))
        event = {
            **latest,
            "id": (f"{latest['pattern_id']}:{latest['state_code']}:{latest['transition_ts']}"),
            "ts": latest["transition_ts"],
            "source": ROUNDED_REVERSAL_SOURCE,
            "event_type": "rounded_reversal_slow_stage",
            "code": transition_code,
            "price": round(float(confirmed[-1].close), 6),
            "level": latest["levels"]["neckline"],
        }

    overlay_items = _overlay_items(
        working,
        latest,
        display=display or {},
    )
    return {
        **empty,
        "series": [latest],
        "events": [event] if event is not None else [],
        "latest": latest,
        "overlays": overlay_items,
    }
