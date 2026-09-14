from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    IndicatorDefaults,
    label_importance_floor,
    score_action,
)
from aef_terminal.indicators.module_contract import (
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime import overlays as overlay_primitives
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.signal_state import SignalState, lifecycle_from_signals

from .continuation import (
    CONTINUATION_MAX_BARS,
    CONTINUATION_MIN_FORMATION_BARS,
    CONTINUATION_MIN_STABLE_PREFIXES,
    continuation_pattern as _continuation_pattern,
)


_COMPACT_ROW_FIELDS = (
    "direction",
    "entry",
    "event",
    "exhaustion_risk",
    "fib1",
    "fib2",
    "important",
    "label",
    "move_atr",
    "origin",
    "price",
    "range_atr",
    "rr",
    "rvol",
    "score",
    "state",
    "stop",
    "target",
    "ts",
    "warning",
)

_OVERLAY_FIELDS = (
    "fib_kind",
    "fib_level",
    "fib_rank",
)

IMPULSE_OVERLAY_HISTORY_LIMIT = 120
IMPULSE_CONTINUATION_OVERLAY_LIMIT = 2
IMPULSE_OVERLAY_COMPACT_LIMIT = IMPULSE_OVERLAY_HISTORY_LIMIT + IMPULSE_CONTINUATION_OVERLAY_LIMIT

_ROW_FIELDS = (
    "alert_lifecycle",
    "body_share",
    "candle_extreme",
    "candle_origin",
    "close_pos",
    "continuation_pattern",
    "direction",
    "entry",
    "event",
    "exhaustion_risk",
    "extreme",
    "fib1",
    "fib2",
    "fib_kind",
    "fib_level",
    "fib_levels",
    "fib_rank",
    "fib_lookback_used",
    "impulse_end_ts",
    "impulse_scale",
    "impulse_scale_label",
    "impulse_start_ts",
    "important",
    "label",
    "leg_bars",
    "leg_duration_minutes",
    "leg_end_ts",
    "leg_merged",
    "leg_range_atr",
    "leg_start_ts",
    "lifecycle",
    "move_atr",
    "origin",
    "pb_levels",
    "pine_fib_levels",
    "price",
    "range_atr",
    "rr",
    "rvol",
    "score",
    "signal",
    "state",
    "stop",
    "target",
    "ts",
    "warning",
    "warnings",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "impulse_fib",
        "Impulse Fib",
        "directional impulse detection, pullback fib zone and continuation plan",
        {"version": "1.1-python", "series": [], "events": [], "latest": None, "overlays": []},
        "impulseFib",
        "aef_terminal.indicators.modules.impulse_fib:impulse_fib",
        pipeline_order=50,
        signal_source="impulse_fib",
        signal_name_resolver_ref="aef_terminal.indicators.domain_facts:resolve_signal_name",
        candidate_score_floor_ref="structure_floor",
        score_family="trend",
        empirical_power=1.05,
        usefulness=1.00,
        state_key="impulseFib",
        calc_key="impulseFibCalcEnabled",
        visible_key="impulseFibVisible",
        chart_control_id="impulse-fib-toggle",
        process_control_id="impulse-fib-process",
        api_enabled_key="impulse_enabled",
        manager_order=60,
        runtime_order=50,
        default_calc=False,
        overlay_layer="zones",
        overlay_filter_ref="impulse_fib",
        renderer_primitives=("box", "line", "label", "marker"),
        renderer_placements=("price",),
        runtime_payload_contract={
            "top_level": ("continuation_pattern",),
            "latest": _ROW_FIELDS,
            "series": _ROW_FIELDS,
            "events": _ROW_FIELDS,
            "overlays": _OVERLAY_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"limit": 24, "fields": _COMPACT_ROW_FIELDS},
            "events": {"limit": 80, "fields": _COMPACT_ROW_FIELDS},
            "overlays": {"limit": IMPULSE_OVERLAY_COMPACT_LIMIT, "fields": "compact_contract"},
            "warnings": {"limit": 12},
        },
        controls=(
            _control(
                "zones",
                "Zones",
                "toggle",
                True,
                "impulseFibZones",
                element_id="impulse-fib-zones",
                compact=True,
            ),
            _control(
                "pbZones",
                "PB",
                "toggle",
                True,
                "impulseFibPbZones",
                element_id="impulse-fib-pb-zones",
                compact=True,
            ),
            _control(
                "patterns",
                "Patterns",
                "toggle",
                True,
                "impulseFibPatterns",
                element_id="impulse-fib-patterns",
                compact=True,
            ),
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "impulseFibLabels",
                element_id="impulse-fib-labels",
                compact=True,
            ),
            _control(
                "style",
                "Style",
                "select",
                "both",
                "impulseFibStyle",
                element_id="impulse-fib-style",
                options=("both", "zones", "lines", "minimal"),
            ),
            _control(
                "minScore",
                "Min",
                "number",
                72,
                "impulseFibMinScore",
                element_id="impulse-fib-min-score",
                api_key="impulse_min_score",
                param_key="min_score",
                minimum=55,
                maximum=95,
                step=1,
                action="load_apply",
            ),
            _control(
                "fibLookback",
                "Lookback",
                "number",
                18,
                "impulseFibLookback",
                element_id="impulse-fib-lookback",
                api_key="impulse_fib_lookback",
                param_key="fib_lookback",
                minimum=8,
                maximum=48,
                step=1,
                action="load_apply",
            ),
            _control(
                "projectionBars",
                "Projection",
                "number",
                4,
                "impulseFibProjectionBars",
                element_id="impulse-fib-projection-bars",
                api_key="impulse_fib_projection_bars",
                param_key="projection_bars",
                minimum=3,
                maximum=80,
                step=1,
                action="load_apply",
            ),
            _control(
                "fibBars",
                "Bars",
                "number",
                8,
                "impulseFibBars",
                element_id="impulse-fib-bars",
                api_key="impulse_fib_bars",
                param_key="fib_bars",
                minimum=5,
                maximum=80,
                step=1,
                action="load_apply",
            ),
            _control(
                "zoneOpacity",
                "Opacity",
                "number",
                10,
                "impulseFibZoneOpacity",
                element_id="impulse-fib-zone-opacity",
                minimum=0,
                maximum=30,
                step=1,
            ),
            _control(
                "markWidth",
                "Width",
                "number",
                1.25,
                "impulseFibMarkWidth",
                element_id="impulse-fib-mark-width",
                minimum=0.5,
                maximum=4,
                step=0.25,
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.impulse_fib:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.impulse_fib:build_params",
    ui_js_assets=("client.js",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = impulse_fib
    params = ctx.params("impulse_fib")
    return IndicatorExecutionSpec(
        id="impulse_fib",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=lambda: calculate(
            ctx.confirmed_bars,
            profile=ctx.instrument_profile,
            params=params,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
    )


@dataclass(frozen=True)
class ImpulseFibParams:
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    volume_len: int = DEFAULT_INDICATOR_SETTINGS.rvol_len
    breakout_len: int = 12
    range_atr_mult: float = 1.15
    min_body_share: float = 0.58
    close_pos_min: float = 0.68
    min_rvol: float = 1.20
    min_score: float = 72.0
    fib_lookback: int = 18
    adaptive_fib_lookback: bool = True
    max_fib_lookback: int = 42
    fib_levels: tuple[float, ...] = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
    zone_levels: tuple[float, ...] = (0.382, 0.618)
    pb_levels: tuple[float, ...] = (0.618, 0.786)
    fib_bars: int = 8
    projection_bars: int = 4
    merge_gap_bars: int = 2
    max_events: int = 80


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> ImpulseFibParams:
    return ImpulseFibParams(
        atr_len=defaults.atr_len,
        volume_len=defaults.rvol_len,
        min_score=float(raw.get("min_score", label_importance_floor(defaults.score))),
        fib_lookback=int(raw.get("fib_lookback", ImpulseFibParams.fib_lookback)),
        fib_bars=int(raw.get("fib_bars", ImpulseFibParams.fib_bars)),
        projection_bars=int(raw.get("projection_bars", ImpulseFibParams.projection_bars)),
        adaptive_fib_lookback=bool(
            raw.get(
                "adaptive_fib_lookback",
                ImpulseFibParams.adaptive_fib_lookback,
            )
        ),
        max_fib_lookback=int(
            raw.get(
                "max_fib_lookback",
                ImpulseFibParams.max_fib_lookback,
            )
        ),
    )


_rvol_series = pine.relative_volume_series


def _profile_thresholds(
    profile: InstrumentProfile, params: ImpulseFibParams
) -> tuple[float, float, float, float]:
    if profile.family == "equity_etf":
        return 1.20, 0.56, 0.66, 1.45
    if profile.family == "crypto":
        return 1.35, 0.54, 0.64, 1.05
    if profile.family in {"index_future", "tech_index"}:
        return 1.10, 0.58, 0.68, 1.25
    return params.range_atr_mult, params.min_body_share, params.close_pos_min, params.min_rvol


def _impulse_states(
    bars: Sequence[Bar],
    profile: InstrumentProfile,
    params: ImpulseFibParams,
    atr_values: list[float] | None = None,
) -> list[dict[str, Any]]:
    if atr_values is None:
        atr_values = pine.atr_rma_series(bars, params.atr_len)
    rvol_values = _rvol_series(bars, params.volume_len)
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    breakout_highs = pine.rolling_high_series(highs, params.breakout_len)
    breakout_lows = pine.rolling_low_series(lows, params.breakout_len)
    range_mult, body_min, close_min, volume_mult = _profile_thresholds(profile, params)
    states: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        anatomy = pine.bar_anatomy(bar)
        atr = max(atr_values[index], 0.000001)
        rvol = rvol_values[index]
        directional_close_pos = (
            anatomy.close_pos if bar.close >= bar.open else (bar.high - bar.close) / anatomy.rng
        )
        break_high = breakout_highs[index]
        break_low = breakout_lows[index]
        range_atr = anatomy.rng / atr
        range_ok = range_atr >= range_mult
        vol_ok = rvol >= volume_mult
        bull_break = break_high is not None and bar.close > break_high
        bear_break = break_low is not None and bar.close < break_low
        candle_impulse = (
            vol_ok
            and range_atr >= range_mult * 0.70
            and anatomy.body_share >= body_min * 0.85
            and directional_close_pos >= close_min * 0.92
        )
        bull = (
            bar.close > bar.open
            and candle_impulse
            and (bull_break or range_ok or rvol >= volume_mult * 1.18)
        )
        bear = (
            bar.close < bar.open
            and candle_impulse
            and (bear_break or range_ok or rvol >= volume_mult * 1.18)
        )
        exhaustion_risk = (
            anatomy.body_share > 0.90 and rvol > 2.50 and range_atr > range_mult * 1.15
        )
        raw_strength = pine.clamp(
            25.0 * range_atr
            + 35.0 * anatomy.body_share
            + 25.0 * directional_close_pos
            + 15.0 * (rvol / max(volume_mult, 0.000001)),
            0.0,
            100.0,
        )
        strength = pine.clamp(raw_strength - (16.0 if exhaustion_risk else 0.0), 0.0, 100.0)
        states.append(
            {
                "bull": bull,
                "bear": bear,
                "direction": "long" if bull else "short" if bear else "flat",
                "score": strength,
                "move_atr": (bar.close - bars[index - 1].close) / atr if index else 0.0,
                "range_atr": range_atr,
                "body_share": anatomy.body_share,
                "close_pos": directional_close_pos,
                "rvol": rvol,
                "atr": atr,
                "candle_impulse": candle_impulse,
                "breakout": bull_break if bull else bear_break if bear else False,
                "exhaustion_risk": exhaustion_risk,
                "raw_score": raw_strength,
            }
        )
    return states


def _best_range_indices(
    bars: Sequence[Bar], index: int, lookback: int
) -> tuple[int, int, float, float]:
    start = max(0, index - max(lookback, 2) + 1)
    window = list(enumerate(bars[start : index + 1], start=start))
    hi_index, hi_bar = max(window, key=lambda item: item[1].high)
    lo_index, lo_bar = min(window, key=lambda item: item[1].low)
    return hi_index, lo_index, hi_bar.high, lo_bar.low


def _range_indices_between(
    bars: Sequence[Bar], start: int, end: int
) -> tuple[int, int, float, float]:
    window = list(enumerate(bars[max(0, start) : end + 1], start=max(0, start)))
    hi_index, hi_bar = max(window, key=lambda item: item[1].high)
    lo_index, lo_bar = min(window, key=lambda item: item[1].low)
    return hi_index, lo_index, hi_bar.high, lo_bar.low


def _adaptive_impulse_lookback(
    bars: Sequence[Bar],
    states: Sequence[dict[str, Any]],
    index: int,
    direction: str,
    params: ImpulseFibParams,
) -> int:
    base = max(int(params.fib_lookback), 2)
    if not params.adaptive_fib_lookback:
        return base
    max_window = max(base, int(params.max_fib_lookback))
    start = index
    opposite = "short" if direction == "long" else "long"
    while start > 0 and index - start + 1 < max_window:
        prev_bar = bars[start - 1]
        current_bar = bars[start]
        prev_state = str(states[start - 1].get("direction") or "flat")
        if prev_state == opposite:
            break
        if direction == "long":
            sharp_counter = (
                prev_bar.close > current_bar.close
                and prev_bar.close - current_bar.close
                > max(float(states[index]["atr"]) * 0.55, 0.0)
            )
        else:
            sharp_counter = (
                current_bar.close > prev_bar.close
                and current_bar.close - prev_bar.close
                > max(float(states[index]["atr"]) * 0.55, 0.0)
            )
        if sharp_counter:
            break
        start -= 1
    return max(base, index - start + 1)


def _dedupe_levels(levels: Sequence[float]) -> list[float]:
    out: list[float] = []
    for level in levels:
        value = round(float(level), 4)
        if value < 0.0 or value > 1.0:
            continue
        if all(abs(value - existing) > 0.0001 for existing in out):
            out.append(value)
    return sorted(out)


def _leg_pullback_levels(levels: Sequence[float], leg_bars: int) -> list[float]:
    base = list(levels)
    if leg_bars > 3:
        base = [0.236, 0.382, 0.5, *base]
    return _dedupe_levels(base)


def _leg_duration_minutes(bars: Sequence[Bar], start_index: int, end_index: int) -> float:
    if end_index <= start_index:
        return 0.0
    duration = bars[end_index].ts - bars[start_index].ts
    return max(duration.total_seconds() / 60.0, 0.0)


def _leg_scale(*, leg_bars: int, duration_minutes: float, range_atr: float) -> str:
    if duration_minutes >= 45.0 or leg_bars >= 9:
        return "strong"
    if duration_minutes >= 15.0 or leg_bars >= 3 or (leg_bars >= 2 and range_atr >= 3.0):
        return "medium"
    return "weak"


def _leg_scale_label(scale: str) -> str:
    return {"strong": "IMP-1H", "medium": "IMP-15", "weak": "IMP-5"}.get(scale, "IMP")


def _fib_price(direction: str, high_value: float, low_value: float, level: float) -> float:
    range_value = high_value - low_value
    # Classic retracement convention: draw the fib from impulse origin to impulse
    # extreme, with 0 at the extreme and 1 at the origin.
    # Bullish impulse: 1=low/origin, 0=high/extreme.
    # Bearish impulse: 1=high/origin, 0=low/extreme.
    return (
        high_value - range_value * level if direction == "long" else low_value + range_value * level
    )


def _pine_candle_retrace_price(
    direction: str, high_value: float, low_value: float, retrace: float
) -> float:
    return _fib_price(direction, high_value, low_value, retrace)


def _pullback_price(direction: str, high_value: float, low_value: float, retrace: float) -> float:
    range_value = high_value - low_value
    return (
        high_value - range_value * retrace
        if direction == "long"
        else low_value + range_value * retrace
    )


def _rr(entry: float, stop: float | None, target: float | None) -> float | None:
    if stop is None or target is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    return abs(target - entry) / risk


def _impulse_warning(
    *,
    bar: Bar,
    state: dict[str, Any],
    direction: str,
    confirmed: bool,
    min_score: float,
) -> dict[str, Any] | None:
    if direction not in {"long", "short"}:
        return None
    score = float(state["score"])
    event_type = "confirmed_impulse" if confirmed else "developing_impulse"
    return {
        "id": f"impulse_fib:{event_type}:{bar.timeframe}:{bar.ts.isoformat()}",
        "source": "impulse_fib",
        "event_type": event_type,
        "code": "IMP_CONF" if confirmed else "IMP_DEV",
        "direction": direction,
        "score": _round(score, 2),
        "min_score": _round(min_score, 2),
        "severity": "warning" if confirmed else "info",
        "should_alert": bool(confirmed),
        "telegram_optional": True,
        **indicator_fact_payload(
            scenario=event_type,
            trigger_event={
                "code": "confirmed_impulse_not_an_immediate_entry"
                if confirmed
                else "developing_impulse_not_an_entry",
            },
            quality={"code": "score_threshold"},
            metrics={
                "breakout": bool(state.get("breakout")),
                "score": _metric_number(score, digits=0),
                "min_score": _metric_number(min_score, digits=0),
                "rvol": _metric_number(state.get("rvol")),
                "range_atr": _metric_number(state.get("range_atr"), suffix=" ATR"),
                "body_share": _metric_number(state.get("body_share")),
                "close_location": _metric_number(state.get("close_pos")),
            },
        ),
    }


def _impulse_manifest(
    *,
    direction: str,
    state: dict[str, Any],
    params: ImpulseFibParams,
    entry: float,
    stop: float,
    target: float,
    rr_value: float | None,
) -> dict[str, Any]:
    supporting = [
        {"code": "range_impulse_confirmed"},
        {"code": "rvol_confirmed"},
        {"code": "close_near_impulse_extreme"},
    ]
    if state.get("breakout"):
        supporting.append({"code": "structural_breakout"})
    opposing: list[dict[str, Any]] = []
    if state.get("exhaustion_risk"):
        opposing.append({"code": "exhaustion_risk"})
    if rr_value is None:
        opposing.append({"code": "rr_unavailable"})
    elif rr_value < 1.50:
        opposing.append({"code": "rr_below_minimum", "minimum": 1.50, "actual": _round(rr_value)})

    return {
        "reason_code": "impulse_continuation",
        "trigger_event": {"code": "controlled_pullback_or_breakout_retest"},
        "supporting": supporting,
        "opposing": opposing,
        "risk": {
            "code": "stop_invalidation",
            "price": _round(stop),
            "low_rr": bool(rr_value is not None and rr_value < 1.50),
        },
        "trade_plan": {
            "source": "impulse_fib",
            "direction": direction,
            "trigger": _round(entry),
            "entry": _round(entry),
            "stop": _round(stop),
            "target": _round(target),
            "invalidation": _round(stop),
            "complete": True,
            "coherent": (stop < entry < target) if direction == "long" else (target < entry < stop),
            "actionable": bool(rr_value is not None and rr_value >= 1.50),
        },
        "metrics": {
            "range_atr": _metric_number(state.get("range_atr"), suffix=" ATR"),
            "range_trigger_atr": _metric_number(params.range_atr_mult, suffix=" ATR"),
            "rvol": _metric_number(state.get("rvol")),
            "close_location_pct": _metric_number(
                float(state["close_pos"]) * 100.0, digits=0, suffix="%"
            ),
            "breakout_bars": _metric_number(params.breakout_len, digits=0),
            "rr": _metric_number(rr_value) if rr_value is not None else None,
            "stop": _metric_number(stop),
        },
    }


def _impulse_leg_overlays(
    spec: Mapping[str, Any],
    params: ImpulseFibParams,
) -> list[dict[str, Any]]:
    bar = spec["bar"]
    impulse_start_bar = spec["impulse_start_bar"]
    render_direction = str(spec["direction"])
    end_iso = str(spec["end_iso"])
    fib_prices = list(spec["fib_prices"])
    pb_prices = list(spec["pb_prices"])
    event = spec["event"]
    event_signal = spec["event_signal"]
    manifest = spec["manifest"]
    render_score = float(spec["render_score"])
    leg_bars = int(spec["leg_bars"])
    leg_duration_minutes = float(spec["leg_duration_minutes"])
    leg_range_atr = float(spec["leg_range_atr"])
    impulse_scale = str(spec["impulse_scale"])
    impulse_scale_label = str(spec["impulse_scale_label"])
    event_fact_fields = copy_indicator_facts(event)
    pb_fact_fields = indicator_fact_payload(
        scenario="impulse_pullback",
        trigger_event=manifest["trigger_event"],
        supporting=manifest["supporting"],
        opposing=manifest["opposing"],
        risk=manifest["risk"],
        quality={"code": "score"},
        trade_plan=manifest["trade_plan"],
        metrics={
            **manifest["metrics"],
            "impulse_scale": impulse_scale,
            "impulse_scale_label": impulse_scale_label,
            "score": _metric_number(render_score, digits=0),
            "leg_bars": _metric_number(leg_bars, digits=0),
            "leg_duration_minutes": _metric_number(
                leg_duration_minutes,
                digits=0,
                suffix="m",
            ),
            "leg_range_atr": _metric_number(
                leg_range_atr,
                suffix=" ATR",
            ),
        },
    )
    out: list[dict[str, Any]] = [
        {
            "type": "box",
            "id": f"impulse-zone-{bar.ts.isoformat()}",
            "ts": bar.ts.isoformat(),
            "start_ts": impulse_start_bar.ts.isoformat(),
            "end_ts": end_iso,
            "top": _round(spec["fib_top"]),
            "bottom": _round(spec["fib_bottom"]),
            "source": "impulse_fib",
            "role": "impulse_zone",
            "direction": render_direction,
            **event_fact_fields,
        },
        {
            "type": "box",
            "id": f"impulse-pb-zone-{bar.ts.isoformat()}",
            "ts": bar.ts.isoformat(),
            "start_ts": bar.ts.isoformat(),
            **overlay_primitives.projected_end(
                bar,
                params.projection_bars,
            ),
            "top": _round(max(price for _, price in pb_prices)),
            "bottom": _round(min(price for _, price in pb_prices)),
            **pb_fact_fields,
            "source": "impulse_fib",
            "role": "pullback_zone",
            "direction": render_direction,
            "badge_facts": [
                {
                    "code": "impulse_pullback",
                    "direction": render_direction,
                }
            ],
        },
    ]
    for level, fib_price in fib_prices:
        label_text = f"{level:.3f}".rstrip("0").rstrip(".")
        line_overlay = overlay_primitives.line(
            start=impulse_start_bar,
            end_ts=end_iso,
            price=fib_price,
            label_text=label_text,
            tone=("negative" if render_direction == "short" else "positive"),
            width=(1.4 if level in {0.0, 0.5, 0.618, 0.786, 1.0} else 1.0),
            style=("solid" if level in {0.0, 1.0} else "dashed" if level == 0.5 else "dotted"),
            label_side=("above" if render_direction == "short" else "below"),
            label_position="line_right",
            label_font_size=7,
            label_gap_px=1,
            direction=render_direction,
            fact_fields=event_fact_fields,
        )
        line_overlay["id"] = f"impulse-fib-{level:.3f}-{bar.ts.isoformat()}"
        line_overlay["fib_kind"] = "impulse"
        line_overlay["fib_level"] = float(level)
        out.append(line_overlay)
    for pb_index, (level, pb_price) in enumerate(pb_prices):
        label_text = f"{level:.3f}".rstrip("0").rstrip(".")
        line_overlay = overlay_primitives.line(
            start=bar,
            bars_forward=params.fib_bars,
            price=pb_price,
            label_text=label_text,
            tone=("negative" if render_direction == "short" else "positive"),
            width=1.0,
            style="dotted" if pb_index == 0 else "dashed",
            role="pullback_zone",
            label_side=("above" if render_direction == "short" else "below"),
            label_position="line_right",
            label_font_size=7,
            label_gap_px=1,
            direction=render_direction,
            fact_fields=pb_fact_fields,
        )
        line_overlay["id"] = f"impulse-pb-{level:.3f}-{bar.ts.isoformat()}"
        line_overlay["fib_kind"] = "pullback"
        line_overlay["fib_level"] = float(level)
        line_overlay["fib_rank"] = pb_index
        out.append(line_overlay)
    signal_label = overlay_primitives.signal_label(
        bar=bar,
        price=_round(bar.low if render_direction == "long" else bar.high),
        lines=[],
        signal=event_signal,
        side="below" if render_direction == "long" else "above",
        role="impulse_fib",
    )
    signal_label["anchor_price_mode"] = "overlay"
    signal_label.update(event_fact_fields)
    out.append(signal_label)
    return out


def _impulse_event_fact_fields(spec: Mapping[str, Any]) -> dict[str, Any]:
    manifest = spec["manifest"]
    return indicator_fact_payload(
        scenario=("low_rr" if bool(spec["low_rr"]) else "continuation_pullback"),
        trigger_event=manifest["trigger_event"],
        supporting=manifest["supporting"],
        opposing=manifest["opposing"],
        risk=manifest["risk"],
        quality={"code": "score"},
        trade_plan=manifest["trade_plan"],
        metrics={
            **manifest["metrics"],
            "impulse_scale": spec["impulse_scale"],
            "impulse_scale_label": spec["impulse_scale_label"],
            "score": _metric_number(spec["render_score"], digits=0),
            "leg_bars": _metric_number(spec["leg_bars"], digits=0),
            "leg_duration_minutes": _metric_number(
                spec["leg_duration_minutes"],
                digits=0,
                suffix="m",
            ),
            "leg_range_atr": _metric_number(
                spec["leg_range_atr"],
                suffix=" ATR",
            ),
            "fib_zone_1": _metric_number(spec["fib_level_1"], digits=3),
            "fib_zone_2": _metric_number(spec["fib_level_2"], digits=3),
        },
    )


def impulse_fib(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    params: ImpulseFibParams | None = None,
) -> dict[str, Any]:
    params = params or ImpulseFibParams()
    if not bars:
        return {"version": "1.1-python", "series": [], "events": [], "latest": None, "overlays": []}

    atr_values = pine.atr_rma_series(bars, params.atr_len)
    states = _impulse_states(bars, profile, params, atr_values)
    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    overlays: list[dict[str, Any]] = []
    overlay_specs: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    signal_states: list[SignalState] = []
    latest_event: dict[str, Any] | None = None
    active_leg: dict[str, Any] | None = None

    for index, state in enumerate(states):
        bar = bars[index]
        direction = str(state["direction"])
        direction_enum = (
            Direction.LONG
            if state["bull"]
            else Direction.SHORT
            if state["bear"]
            else Direction.FLAT
        )
        state_score = float(state["score"])
        developing_direction = direction
        if developing_direction == "flat" and state.get("candle_impulse"):
            developing_direction = "long" if bar.close >= bar.open else "short"
        confirmed_impulse = (
            direction in {"long", "short"}
            and state_score >= params.min_score
            and bool(state.get("breakout"))
        )
        warning = (
            _impulse_warning(
                bar=bar,
                state=state,
                direction=developing_direction,
                confirmed=confirmed_impulse,
                min_score=params.min_score,
            )
            if state.get("candle_impulse") or confirmed_impulse
            else None
        )
        signal = SignalState(
            source="impulse_fib",
            action=ActionPhase.WAIT,
            raw_action="ARM"
            if direction_enum != Direction.FLAT and state_score >= params.min_score
            else "WAIT",
            direction=direction_enum,
            score=state_score,
            confirmed=bar.closed,
            source_tf=bar.timeframe,
            trigger=None,
            reason="directional_impulse"
            if direction_enum != Direction.FLAT
            else "no_directional_impulse",
            code="IMPULSE_FIB",
        )
        signal_payload = signal.as_dict()
        signal_payload["reason_code"] = signal_payload.pop("reason")
        item = {
            "ts": bar.ts.isoformat(),
            "direction": direction,
            "score": _round(state_score, 2),
            "move_atr": _round(float(state["move_atr"]), 4),
            "range_atr": _round(float(state["range_atr"]), 4),
            "body_share": _round(float(state["body_share"]), 4),
            "close_pos": _round(float(state["close_pos"]), 4),
            "rvol": _round(float(state["rvol"]), 4),
            "exhaustion_risk": bool(state.get("exhaustion_risk")),
            "important": direction != "flat" and state_score >= params.min_score,
            "warning": warning,
            "signal": signal_payload,
        }
        if warning:
            warnings.append({**warning, "ts": bar.ts.isoformat()})
        series.append(item)
        signal_states.append(signal)
        scored_impulse = direction in {"long", "short"} and state_score >= params.min_score
        merge_gap = max(int(params.merge_gap_bars), 0) + 1
        active_direction = str(active_leg.get("direction") or "") if active_leg else ""
        active_last_index = int(active_leg.get("last_index", -9999)) if active_leg else -9999
        active_extreme = float(active_leg["extreme_value"]) if active_leg else 0.0
        gap_ok = bool(active_leg and index - active_last_index <= merge_gap)
        extends_active_leg = bool(
            active_leg
            and gap_ok
            and (
                (active_direction == "long" and bar.high > active_extreme)
                or (active_direction == "short" and bar.low < active_extreme)
            )
        )
        if not scored_impulse and not extends_active_leg:
            continue

        render_direction = direction if scored_impulse else active_direction
        if render_direction not in {"long", "short"}:
            continue
        render_score = max(
            state_score,
            float(active_leg["score"]) if active_leg and extends_active_leg else 0.0,
        )
        render_direction_enum = Direction.LONG if render_direction == "long" else Direction.SHORT
        fib_lookback = _adaptive_impulse_lookback(bars, states, index, render_direction, params)
        merge_leg = (
            active_leg
            if active_leg
            and active_leg.get("direction") == render_direction
            and (scored_impulse or extends_active_leg)
            and gap_ok
            else None
        )
        if merge_leg:
            hi_index, lo_index, high_value, low_value = _range_indices_between(
                bars,
                int(merge_leg["range_start_index"]),
                index,
            )
            fib_lookback = max(fib_lookback, index - int(merge_leg["range_start_index"]) + 1)
        else:
            hi_index, lo_index, high_value, low_value = _best_range_indices(
                bars, index, fib_lookback
            )
        range_value = high_value - low_value
        order_ok = lo_index <= hi_index if render_direction == "long" else hi_index <= lo_index
        if not order_ok or range_value <= max(atr_values[index] * 0.60, 0.000001):
            continue
        if merge_leg:
            event_index = int(merge_leg["event_index"])
            events.pop(event_index)
            overlay_specs.pop(event_index)
        leg_first_index = int(merge_leg.get("first_index", index)) if merge_leg else index
        leg_bars = index - leg_first_index + 1
        leg_duration_minutes = _leg_duration_minutes(bars, leg_first_index, index)
        leg_range_atr = range_value / max(atr_values[index], 0.000001)
        impulse_scale = _leg_scale(
            leg_bars=leg_bars, duration_minutes=leg_duration_minutes, range_atr=leg_range_atr
        )
        impulse_scale_label = _leg_scale_label(impulse_scale)

        fib_levels = _dedupe_levels(params.fib_levels)
        zone_levels = _dedupe_levels(params.zone_levels)
        if not zone_levels:
            zone_levels = [0.382, 0.618]
        fib_prices = [
            (level, _fib_price(render_direction, high_value, low_value, level))
            for level in fib_levels
        ]
        pine_retrace_levels = (0.5, 0.618, 0.786)
        pine_fib_prices = [
            (level, _pine_candle_retrace_price(render_direction, bar.high, bar.low, level))
            for level in pine_retrace_levels
        ]
        pb_levels = _leg_pullback_levels(params.pb_levels, leg_bars)
        if not pb_levels:
            pb_levels = [0.618, 0.786]
        pb_prices = [
            (level, _pullback_price(render_direction, high_value, low_value, level))
            for level in pb_levels
        ]
        zone_prices = [
            _fib_price(render_direction, high_value, low_value, level) for level in zone_levels
        ]
        fib_level_1 = zone_prices[0]
        fib_level_2 = zone_prices[-1]
        fib_top = max(fib_level_1, fib_level_2)
        fib_bottom = min(fib_level_1, fib_level_2)
        impulse_start_index = lo_index if render_direction == "long" else hi_index
        impulse_end_index = hi_index if render_direction == "long" else lo_index
        impulse_start_ts = bars[impulse_start_index].ts
        impulse_end_ts = bars[impulse_end_index].ts
        stop_value = (
            (low_value - atr_values[index] * 0.18)
            if render_direction == "long"
            else (high_value + atr_values[index] * 0.18)
        )
        target_value = (
            (high_value + range_value * 0.236)
            if render_direction == "long"
            else (low_value - range_value * 0.236)
        )
        rr_value = _rr(bar.close, stop_value, target_value)
        low_rr = rr_value is not None and rr_value < 1.50
        manifest = _impulse_manifest(
            direction=render_direction,
            state=state,
            params=params,
            entry=bar.close,
            stop=stop_value,
            target=target_value,
            rr_value=rr_value,
        )
        tier_action = score_action(render_score, DEFAULT_INDICATOR_SETTINGS.score)
        action = (
            "WATCH" if low_rr else "WAIT" if tier_action in {"WAIT", "CANDIDATE"} else tier_action
        )
        event_signal = SignalState(
            source="impulse_fib",
            action=ActionPhase(action),
            raw_action="WATCH_LOW_RR" if low_rr else action,
            direction=render_direction_enum,
            score=render_score,
            confirmed=bar.closed,
            source_tf=bar.timeframe,
            trigger=bar.close,
            stop=stop_value,
            target=target_value,
            invalidation=stop_value,
            reason=manifest["reason_code"],
            blocked_reason="rr_below_minimum" if low_rr else "",
            code="IMPULSE_LOW_RR" if low_rr else "IMPULSE_FIB",
        )
        signal_states[-1] = event_signal
        event_signal_payload = event_signal.as_dict()
        event_signal_payload["reason_code"] = event_signal_payload.pop("reason")
        item["signal"] = event_signal_payload
        label = (
            ("BRK-L" if state.get("breakout") else "FIB-L")
            if render_direction == "long"
            else ("BRK-S" if state.get("breakout") else "FIB-S")
        )
        event = {
            **item,
            "direction": render_direction,
            "score": _round(render_score, 2),
            "important": True,
            "event": label,
            "label": label,
            "price": _round(bar.low if render_direction == "long" else bar.high),
            "fib1": _round(fib_level_1),
            "fib2": _round(fib_level_2),
            "origin": _round(low_value if render_direction == "long" else high_value),
            "extreme": _round(high_value if render_direction == "long" else low_value),
            "candle_origin": _round(bar.low if render_direction == "long" else bar.high),
            "candle_extreme": _round(bar.high if render_direction == "long" else bar.low),
            "impulse_start_ts": impulse_start_ts.isoformat(),
            "impulse_end_ts": impulse_end_ts.isoformat(),
            "fib_lookback_used": fib_lookback,
            "leg_start_ts": bars[min(hi_index, lo_index)].ts.isoformat(),
            "leg_end_ts": bar.ts.isoformat(),
            "leg_merged": bool(merge_leg),
            "leg_bars": leg_bars,
            "leg_duration_minutes": _round(leg_duration_minutes, 2),
            "leg_range_atr": _round(leg_range_atr, 2),
            "impulse_scale": impulse_scale,
            "impulse_scale_label": impulse_scale_label,
            "fib_levels": [
                {"level": _round(level, 3), "price": _round(price)} for level, price in fib_prices
            ],
            "pine_fib_levels": [
                {"level": _round(level, 3), "price": _round(price)}
                for level, price in pine_fib_prices
            ],
            "pb_levels": [
                {"level": _round(level, 3), "price": _round(price)} for level, price in pb_prices
            ],
            "target": _round(target_value),
            "stop": _round(stop_value),
            "entry": _round(bar.close),
            "rr": _round(rr_value),
            "exhaustion_risk": bool(state.get("exhaustion_risk")),
        }
        events.append(event)
        latest_event = event
        active_leg = {
            "direction": render_direction,
            "first_index": leg_first_index,
            "last_index": index,
            "range_start_index": min(hi_index, lo_index),
            "event_index": len(events) - 1,
            "score": render_score,
            "extreme_value": high_value if render_direction == "long" else low_value,
        }
        overlay_specs.append(
            {
                "bar": bar,
                "impulse_start_bar": bars[impulse_start_index],
                "direction": render_direction,
                "end_iso": impulse_end_ts.isoformat(),
                "fib_top": fib_top,
                "fib_bottom": fib_bottom,
                "fib_prices": tuple(fib_prices),
                "pb_prices": tuple(pb_prices),
                "event": event,
                "event_signal": event_signal,
                "manifest": manifest,
                "render_score": render_score,
                "leg_bars": leg_bars,
                "leg_duration_minutes": leg_duration_minutes,
                "leg_range_atr": leg_range_atr,
                "impulse_scale": impulse_scale,
                "impulse_scale_label": impulse_scale_label,
                "low_rr": low_rr,
                "fib_level_1": fib_level_1,
                "fib_level_2": fib_level_2,
            }
        )

    if len(events) > params.max_events:
        events = events[-params.max_events :]
        overlay_specs = overlay_specs[-params.max_events :]
    for spec in overlay_specs:
        spec["event"].update(_impulse_event_fact_fields(spec))
    overlays = [
        overlay for spec in overlay_specs for overlay in _impulse_leg_overlays(spec, params)
    ]
    for overlay in overlays:
        overlay.setdefault("retention", "history")
    max_overlay_items = min(
        params.max_events * (len(params.fib_levels) + len(params.pb_levels) + 4),
        IMPULSE_OVERLAY_HISTORY_LIMIT,
    )
    if len(overlays) > max_overlay_items:
        overlays = overlays[-max_overlay_items:]

    lifecycle = lifecycle_from_signals(
        source="impulse_fib",
        bars=bars,
        signals=signal_states,
        atr_values=atr_values,
        max_age_bars=params.projection_bars,
        trail_atr=1.0,
    )
    latest_state = {**series[-1]}
    latest_state["lifecycle"] = lifecycle.as_dict() if lifecycle else None
    latest_warning = (
        warnings[-1] if warnings and warnings[-1].get("ts") == bars[-1].ts.isoformat() else None
    )
    latest_state["warnings"] = [latest_warning] if latest_warning else []
    latest_state["alert_lifecycle"] = latest_warning
    continuation_pattern = _continuation_pattern(
        bars=bars,
        latest_event=latest_event,
        atr_values=atr_values,
        projection_bars=params.projection_bars,
    )
    latest_state["continuation_pattern"] = continuation_pattern
    if continuation_pattern.get("active"):
        pattern_tip = indicator_fact_payload(
            scenario=str(continuation_pattern.get("type") or "continuation_flag"),
            trigger_event={"code": "break_or_invalidation"},
            quality={"code": "pattern_quality"},
            fact_groups=[
                {
                    "kind": "continuation_evidence",
                    "items": list(continuation_pattern.get("evidence", []))[:4],
                }
            ],
            metrics={
                "break_level": _metric_number(continuation_pattern.get("break_level")),
                "invalidation": _metric_number(continuation_pattern.get("invalidation")),
                "quality": _metric_number(continuation_pattern.get("quality"), digits=0),
                "target_1": _metric_number(continuation_pattern.get("target_1")),
                "target_2": _metric_number(continuation_pattern.get("target_2")),
            },
        )
        direction = str(continuation_pattern.get("direction") or "")
        projection_bars = int(continuation_pattern.get("boundary_projection_bars") or 0)
        common_fields = {
            "type": "line",
            "retention": "active",
            "source": "impulse_fib",
            "role": "continuation_pattern",
            "ts": bars[-1].ts.isoformat(),
            "start_ts": str(continuation_pattern.get("start_ts") or bars[-1].ts.isoformat()),
            "direction": direction,
            "pattern": str(continuation_pattern.get("structure_kind") or "flag"),
            "structure_phase": str(continuation_pattern.get("phase") or "watch"),
            "control_key": "patterns",
            "interactive": True,
            "badge_facts": [
                {"code": str(continuation_pattern.get("type") or "continuation_flag")},
                {"code": (f"structure_{continuation_pattern.get('structure_kind') or 'flag'}")},
                {"code": f"phase_{continuation_pattern.get('phase') or 'watch'}"},
            ],
            **pattern_tip,
        }
        if projection_bars > 0:
            common_fields.update(overlay_primitives.projected_end(bars[-1], projection_bars))
        else:
            common_fields["end_ts"] = bars[-1].ts.isoformat()
        for boundary_name in ("upper", "lower"):
            boundary = continuation_pattern.get(f"{boundary_name}_boundary")
            if not isinstance(boundary, Mapping):
                continue
            is_break = (direction == "long") == (boundary_name == "upper")
            overlays.append(
                {
                    **common_fields,
                    "id": (f"impulse-continuation-{boundary_name}-{bars[-1].ts.isoformat()}"),
                    "geometry_segment": f"{boundary_name}_boundary",
                    "level_kind": "break" if is_break else "invalidation",
                    "y1": boundary.get("start_price"),
                    "y2": boundary.get("end_price"),
                    "label": "BREAK" if is_break else "FAIL",
                    "label_side": "above" if boundary_name == "upper" else "below",
                    "label_position": "line_right",
                    "label_font_size": 8,
                    "label_gap_px": 2,
                    "tone": (
                        "positive"
                        if is_break and direction == "long"
                        else "negative"
                        if is_break
                        else "negative"
                        if direction == "long"
                        else "positive"
                    ),
                    "style": "solid" if is_break else "dashed",
                    "width": 1.4 if is_break else 1.0,
                    "opacity": 0.92 if is_break else 0.78,
                }
            )
    latest = {
        **latest_state,
        "state": (
            latest_state.get("label")
            or (latest_state.get("lifecycle") or {}).get("state")
            or latest_state.get("direction")
            or "WAIT"
        ),
    }
    return {
        "version": "1.1-python",
        "series": series,
        "events": events,
        "last_event": latest_event,
        "continuation_pattern": continuation_pattern,
        "latest": latest,
        "overlays": overlays,
        "warnings": warnings[-24:],
        "params": {
            "min_score": params.min_score,
            "breakout_len": params.breakout_len,
            "fib_lookback": params.fib_lookback,
            "adaptive_fib_lookback": params.adaptive_fib_lookback,
            "max_fib_lookback": params.max_fib_lookback,
            "fib_bars": params.fib_bars,
            "projection_bars": params.projection_bars,
            "continuation_min_formation_bars": CONTINUATION_MIN_FORMATION_BARS,
            "continuation_stable_prefixes": CONTINUATION_MIN_STABLE_PREFIXES,
            "continuation_max_bars": CONTINUATION_MAX_BARS,
            "exhaustion_body_share": 0.90,
            "exhaustion_rvol": 2.50,
            "fib_levels": list(params.fib_levels),
            "zone_levels": list(params.zone_levels),
            "pb_levels": list(params.pb_levels),
        },
    }
