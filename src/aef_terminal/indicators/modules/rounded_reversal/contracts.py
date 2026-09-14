"""Rounded Reversal immutable settings and payload contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults
from aef_terminal.indicators.module_contract import INDICATOR_FACT_RUNTIME_FIELDS
from aef_terminal.runtime.math_utils import bool_param, clamp_float, clamp_int

ROUNDED_REVERSAL_VERSION = "2.0-dual-track-adaptive"
ROUNDED_REVERSAL_SOURCE = "rounded_reversal"
ROUNDED_REVERSAL_MICRO_TIMEFRAME = "1m"
_ACTIVE_STATES = frozenset({"ARMED", "CONFIRMED", "RETEST_CONFIRMED"})
_TERMINAL_STATES = frozenset({"MISSED", "INVALIDATED", "EXPIRED"})
_FAST_ACTIVE_STATES = frozenset({"WATCH", "ARMED", "TRIGGERED", "RETEST_CONFIRMED"})
_FAST_TERMINAL_STATES = frozenset({"MISSED", "INVALIDATED", "EXPIRED"})
_ROUNDED_INDEX_FIELDS = frozenset(
    {
        "index",
        "projected_index",
        "end_index",
        "transition_index",
        "armed_index",
        "breakdown_index",
        "confirmed_index",
        "ready_index",
        "ready_parent_index",
    }
)
_ROW_FIELDS = (
    "active",
    "adaptive_thresholds",
    "anchors",
    "armed_index",
    "available_at_ts",
    "breakdown_index",
    "code",
    "confirmation",
    "confirmation_pending",
    "direction",
    "event_on_latest",
    "event_type",
    "evidence_groups",
    "gate_margins",
    "generation_id",
    "geometry",
    "id",
    "level",
    "levels",
    "linked_context",
    "metrics",
    "micro_rejection_cluster",
    "pattern_id",
    "price",
    "rejection_cluster",
    "reanchored_from_pattern_id",
    "rollover",
    "score",
    "source",
    "source_tf",
    "state",
    "state_code",
    "terminal",
    "track",
    "trigger_state",
    "transition_index",
    "transition_ts",
    "ts",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


@dataclass(frozen=True)
class RoundedReversalParams:
    track_mode: str = "both"
    sensitivity: str = "balanced"
    scan_bars: int = 192
    pivot_len: int = 2
    phase_bars: int = 3
    min_rise_bars: int = 6
    max_rise_bars: int = 36
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    min_height_atr: float = 1.50
    min_early_slope_atr_per_bar: float = 0.12
    late_slope_floor_atr_per_bar: float = -0.03
    max_late_to_early_ratio: float = 0.65
    wick_before: int = 3
    wick_after: int = 3
    min_rejections: int = 3
    min_wick_share: float = 0.30
    min_wick_atr: float = 0.10
    max_rejection_close_pos: float = 0.60
    level_tolerance_atr: float = 0.20
    level_tolerance_ticks: int = 2
    min_roll_drop_atr: float = 0.45
    max_roll_slope_atr_per_bar: float = -0.06
    break_buffer_atr: float = 0.05
    break_buffer_ticks: int = 1
    invalidation_atr: float = 0.08
    invalidation_ticks: int = 1
    max_pattern_age: int = 24
    retest_bars: int = 6
    retest_tolerance_atr: float = 0.12
    use_mtf_rejections: bool = True
    micro_min_rejections: int = 4
    micro_min_coverage: float = 0.82
    micro_min_wick_share: float = 0.28
    micro_min_wick_atr: float = 0.16
    use_linked_context: bool = True
    adaptive_strong_bar_score: float = 0.70
    adaptive_shape_floor: float = 0.45
    adaptive_roll_floor: float = 0.70
    local_neckline_fraction: float = 0.25
    fast_scan_bars: int = 48
    fast_rise_lookback: int = 20
    fast_min_rise_atr: float = 1.00
    fast_wick_before: int = 6
    fast_wick_after: int = 8
    fast_target_mass: float = 1.00
    fast_pivot_len: int = 2
    fast_max_pattern_age: int = 16
    fast_max_entry_distance_atr: float = 1.00
    fast_retest_tolerance_atr: float = 0.15


def _choice_param(
    value: Any,
    default: str,
    choices: tuple[str, ...],
) -> str:
    normalized = str(value).strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in choices else default


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults = DEFAULT_INDICATOR_SETTINGS,
) -> RoundedReversalParams:
    """Build a bounded, JSON-hashable parameter contract for later registration."""

    min_rise_bars = clamp_int(
        raw.get("min_rise_bars"),
        RoundedReversalParams.min_rise_bars,
        4,
        24,
    )
    max_rise_bars = clamp_int(
        raw.get("max_rise_bars"),
        RoundedReversalParams.max_rise_bars,
        min_rise_bars,
        96,
    )
    return RoundedReversalParams(
        track_mode=_choice_param(
            raw.get("track_mode"),
            RoundedReversalParams.track_mode,
            ("both", "fast", "slow"),
        ),
        sensitivity=_choice_param(
            raw.get("sensitivity"),
            RoundedReversalParams.sensitivity,
            ("early", "balanced", "strict"),
        ),
        scan_bars=clamp_int(
            raw.get("scan_bars"),
            RoundedReversalParams.scan_bars,
            48,
            480,
        ),
        pivot_len=clamp_int(
            raw.get("pivot_len"),
            RoundedReversalParams.pivot_len,
            2,
            8,
        ),
        phase_bars=clamp_int(
            raw.get("phase_bars"),
            RoundedReversalParams.phase_bars,
            2,
            12,
        ),
        min_rise_bars=min_rise_bars,
        max_rise_bars=max_rise_bars,
        atr_len=clamp_int(
            raw.get("atr_len"),
            defaults.atr_len,
            2,
            200,
        ),
        min_height_atr=clamp_float(
            raw.get("min_height_atr"),
            RoundedReversalParams.min_height_atr,
            0.5,
            8.0,
        ),
        min_early_slope_atr_per_bar=clamp_float(
            raw.get("min_early_slope_atr_per_bar"),
            RoundedReversalParams.min_early_slope_atr_per_bar,
            0.01,
            1.5,
        ),
        late_slope_floor_atr_per_bar=clamp_float(
            raw.get("late_slope_floor_atr_per_bar"),
            RoundedReversalParams.late_slope_floor_atr_per_bar,
            -0.5,
            0.5,
        ),
        max_late_to_early_ratio=clamp_float(
            raw.get("max_late_to_early_ratio"),
            RoundedReversalParams.max_late_to_early_ratio,
            -0.5,
            0.95,
        ),
        wick_before=clamp_int(
            raw.get("wick_before"),
            RoundedReversalParams.wick_before,
            1,
            12,
        ),
        wick_after=clamp_int(
            raw.get("wick_after"),
            RoundedReversalParams.wick_after,
            1,
            12,
        ),
        min_rejections=clamp_int(
            raw.get("min_rejections"),
            RoundedReversalParams.min_rejections,
            2,
            8,
        ),
        min_wick_share=clamp_float(
            raw.get("min_wick_share"),
            RoundedReversalParams.min_wick_share,
            0.10,
            0.75,
        ),
        min_wick_atr=clamp_float(
            raw.get("min_wick_atr"),
            RoundedReversalParams.min_wick_atr,
            0.02,
            1.0,
        ),
        max_rejection_close_pos=clamp_float(
            raw.get("max_rejection_close_pos"),
            RoundedReversalParams.max_rejection_close_pos,
            0.20,
            0.80,
        ),
        level_tolerance_atr=clamp_float(
            raw.get("level_tolerance_atr"),
            RoundedReversalParams.level_tolerance_atr,
            0.02,
            1.0,
        ),
        level_tolerance_ticks=clamp_int(
            raw.get("level_tolerance_ticks"),
            RoundedReversalParams.level_tolerance_ticks,
            1,
            12,
        ),
        min_roll_drop_atr=clamp_float(
            raw.get("min_roll_drop_atr"),
            RoundedReversalParams.min_roll_drop_atr,
            0.10,
            3.0,
        ),
        max_roll_slope_atr_per_bar=clamp_float(
            raw.get("max_roll_slope_atr_per_bar"),
            RoundedReversalParams.max_roll_slope_atr_per_bar,
            -1.0,
            -0.005,
        ),
        break_buffer_atr=clamp_float(
            raw.get("break_buffer_atr"),
            RoundedReversalParams.break_buffer_atr,
            0.0,
            0.5,
        ),
        break_buffer_ticks=clamp_int(
            raw.get("break_buffer_ticks"),
            RoundedReversalParams.break_buffer_ticks,
            1,
            8,
        ),
        invalidation_atr=clamp_float(
            raw.get("invalidation_atr"),
            RoundedReversalParams.invalidation_atr,
            0.0,
            0.75,
        ),
        invalidation_ticks=clamp_int(
            raw.get("invalidation_ticks"),
            RoundedReversalParams.invalidation_ticks,
            1,
            8,
        ),
        max_pattern_age=clamp_int(
            raw.get("max_pattern_age"),
            RoundedReversalParams.max_pattern_age,
            6,
            96,
        ),
        retest_bars=clamp_int(
            raw.get("retest_bars"),
            RoundedReversalParams.retest_bars,
            1,
            24,
        ),
        retest_tolerance_atr=clamp_float(
            raw.get("retest_tolerance_atr"),
            RoundedReversalParams.retest_tolerance_atr,
            0.02,
            0.75,
        ),
        use_mtf_rejections=bool_param(
            raw.get("use_mtf_rejections"),
            RoundedReversalParams.use_mtf_rejections,
        ),
        micro_min_rejections=clamp_int(
            raw.get("micro_min_rejections"),
            RoundedReversalParams.micro_min_rejections,
            2,
            12,
        ),
        micro_min_coverage=clamp_float(
            raw.get("micro_min_coverage"),
            RoundedReversalParams.micro_min_coverage,
            0.50,
            1.0,
        ),
        micro_min_wick_share=clamp_float(
            raw.get("micro_min_wick_share"),
            RoundedReversalParams.micro_min_wick_share,
            0.10,
            0.80,
        ),
        micro_min_wick_atr=clamp_float(
            raw.get("micro_min_wick_atr"),
            RoundedReversalParams.micro_min_wick_atr,
            0.02,
            1.5,
        ),
        use_linked_context=bool_param(
            raw.get("use_linked_context"),
            RoundedReversalParams.use_linked_context,
        ),
        adaptive_strong_bar_score=clamp_float(
            raw.get("adaptive_strong_bar_score"),
            RoundedReversalParams.adaptive_strong_bar_score,
            0.40,
            0.95,
        ),
        adaptive_shape_floor=clamp_float(
            raw.get("adaptive_shape_floor"),
            RoundedReversalParams.adaptive_shape_floor,
            0.20,
            0.90,
        ),
        adaptive_roll_floor=clamp_float(
            raw.get("adaptive_roll_floor"),
            RoundedReversalParams.adaptive_roll_floor,
            0.30,
            0.95,
        ),
        local_neckline_fraction=clamp_float(
            raw.get("local_neckline_fraction"),
            RoundedReversalParams.local_neckline_fraction,
            0.10,
            0.60,
        ),
        fast_scan_bars=clamp_int(
            raw.get("fast_scan_bars"),
            RoundedReversalParams.fast_scan_bars,
            24,
            256,
        ),
        fast_rise_lookback=clamp_int(
            raw.get("fast_rise_lookback"),
            RoundedReversalParams.fast_rise_lookback,
            8,
            60,
        ),
        fast_min_rise_atr=clamp_float(
            raw.get("fast_min_rise_atr"),
            RoundedReversalParams.fast_min_rise_atr,
            0.40,
            5.0,
        ),
        fast_wick_before=clamp_int(
            raw.get("fast_wick_before"),
            RoundedReversalParams.fast_wick_before,
            2,
            20,
        ),
        fast_wick_after=clamp_int(
            raw.get("fast_wick_after"),
            RoundedReversalParams.fast_wick_after,
            2,
            30,
        ),
        fast_target_mass=clamp_float(
            raw.get("fast_target_mass"),
            RoundedReversalParams.fast_target_mass,
            0.50,
            4.0,
        ),
        fast_pivot_len=clamp_int(
            raw.get("fast_pivot_len"),
            RoundedReversalParams.fast_pivot_len,
            2,
            4,
        ),
        fast_max_pattern_age=clamp_int(
            raw.get("fast_max_pattern_age"),
            RoundedReversalParams.fast_max_pattern_age,
            4,
            60,
        ),
        fast_max_entry_distance_atr=clamp_float(
            raw.get("fast_max_entry_distance_atr"),
            RoundedReversalParams.fast_max_entry_distance_atr,
            0.25,
            3.0,
        ),
        fast_retest_tolerance_atr=clamp_float(
            raw.get("fast_retest_tolerance_atr"),
            RoundedReversalParams.fast_retest_tolerance_atr,
            0.02,
            0.75,
        ),
    )
