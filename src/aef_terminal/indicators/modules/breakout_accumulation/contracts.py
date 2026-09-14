from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults


BREAKOUT_ACCUMULATION_VERSION = "16-python-confirmed-1m-preview"
BREAKOUT_OVERLAY_HISTORY_LIMIT = 80
BREAKOUT_OVERLAY_COMPACT_LIMIT = 96

LONG_EVENT_KEYS = frozenset(
    {"trap_call", "cont_call", "go_up", "probe_up", "rot_call", "rb_call", "arm_up", "wide_call"}
)
SHORT_EVENT_KEYS = frozenset(
    {"trap_put", "cont_put", "go_down", "probe_down", "rot_put", "rb_put", "arm_down", "wide_put"}
)
LOW_EDGE_EVENT_CODES = frozenset({"trap_call", "rotation_call", "rebound_call", "far_stop_call"})
HIGH_EDGE_EVENT_CODES = frozenset({"trap_put", "rotation_put", "rebound_put", "far_stop_put"})
MID_EVENT_CODES = frozenset({"coil", "wide_range", "forming_range"})


@dataclass(frozen=True)
class BreakoutAccumulationParams:
    trade_mode: str = "Early"
    coil_len: int = 12
    barcode_len: int = 36
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    vol_len: int = DEFAULT_INDICATOR_SETTINGS.rvol_len
    ema_fast_len: int = 9
    ema_slow_len: int = DEFAULT_INDICATOR_SETTINGS.ema.fast
    confirm_atr: float = 0.03
    pre_break_atr: float = 0.55
    late_move_atr: float = 0.65
    min_go_rvol: float = 0.85
    accept_bars: int = 2
    no_follow_bars: int = 3
    range_judge_bars: int = 4
    operator_max_bars: int = 10
    accept_retest_atr: float = 0.12
    rebound_touch_atr: float = 0.12
    rebound_wick_body: float = 1.25
    touch_memory_bars: int = 20
    extreme_stretch_atr: float = 0.65
    max_rev_stop_atr: float = 0.40
    rev_stop_buffer_atr: float = 0.06
    join_stop_atr: float = 0.50
    missed_atr: float = 0.35
    target_range_mult: float = 1.0
    target_min_atr: float = 0.75
    pivot_lookback_days: int = 3
    pivot_left_bars: int = 3
    pivot_right_bars: int = 3
    pivot_near_atr: float = 0.18


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> BreakoutAccumulationParams:
    return BreakoutAccumulationParams(
        coil_len=int(raw.get("coil_len", BreakoutAccumulationParams.coil_len)),
        barcode_len=int(raw.get("barcode_len", BreakoutAccumulationParams.barcode_len)),
        atr_len=defaults.atr_len,
        vol_len=defaults.rvol_len,
        ema_slow_len=defaults.ema.slow,
        operator_max_bars=int(
            raw.get(
                "operator_max_bars",
                BreakoutAccumulationParams.operator_max_bars,
            )
        ),
    )


def price_on(value: float | None) -> bool:
    return value is not None and value > 0
