"""Causal adaptive Gaussian kernel regression and residual-band state.

Algorithm reference: "Uptrick: ML Kernel Regression" by Uptrick, published
under CC BY-SA 4.0. This personal-use adaptation preserves the source equations
and visual modes against MartinCall's confirmed/provisional typed contracts;
alert delivery remains server-owned.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from math import exp, isfinite
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.runtime import overlays, pine
from aef_terminal.runtime.timeframes import interval_minutes

from .contracts import (
    ADAPTIVE_KERNEL_ADAPTATION_STRENGTH,
    ADAPTIVE_KERNEL_ENTRY_PLAN_TIMEFRAMES,
    ADAPTIVE_KERNEL_ENTRY_PLAN_WAIT_MINUTES,
    ADAPTIVE_KERNEL_REGRESSION_SOURCE,
    ADAPTIVE_KERNEL_REGRESSION_VERSION,
    AdaptiveKernelRegressionParams,
)


_PRICE_DIGITS = 8
_ENTRY_PLAN_ACTIVE_STATES = frozenset(
    {"armed", "waiting_pullback", "waiting_reclaim", "ready_next_open"}
)


def _rounded(value: float) -> float:
    return round(float(value), _PRICE_DIGITS)


def _empty_entry_plan(
    params: AdaptiveKernelRegressionParams,
    *,
    state: str,
    reason_code: str,
    source_tf: str = "",
) -> dict[str, Any]:
    return {
        "enabled": bool(params.entry_plan_enabled),
        "advisory_only": True,
        "state": state,
        "direction": "flat",
        "active": False,
        "entry_ready": False,
        "triggered": False,
        "source_tf": source_tf,
        "entry_method": "",
        "trigger_line": "",
        "trigger_level": None,
        "confirmation": "",
        "entry_timing": "next_bar_open",
        "signal_ts": None,
        "signal_available_at": None,
        "signal_price": None,
        "touch_ts": None,
        "reclaim_ts": None,
        "entry_ts": None,
        "entry_price": None,
        "expires_at": None,
        "wait_minutes": ADAPTIVE_KERNEL_ENTRY_PLAN_WAIT_MINUTES,
        "wait_bars": 0,
        "bars_since_signal": 0,
        "reason_code": reason_code,
        "invalidation_code": "opposite_kernel_regime",
    }


def _entry_plan_touch(bar: Bar, level: float, direction: str) -> bool:
    if direction == "long":
        return float(bar.open) <= level or float(bar.low) <= level <= float(bar.high)
    return float(bar.open) >= level or float(bar.low) <= level <= float(bar.high)


def _entry_plan(
    bars: Sequence[Bar],
    series: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    ema20: Sequence[float],
    params: AdaptiveKernelRegressionParams,
) -> dict[str, Any]:
    source_tf = str(bars[-1].timeframe or "").strip().lower() if bars else ""
    if not params.entry_plan_enabled:
        return _empty_entry_plan(
            params,
            state="disabled",
            reason_code="entry_plan_disabled",
            source_tf=source_tf,
        )
    if not bars or not series:
        return _empty_entry_plan(
            params,
            state="unavailable",
            reason_code="entry_plan_input_unavailable",
            source_tf=source_tf,
        )
    if source_tf not in ADAPTIVE_KERNEL_ENTRY_PLAN_TIMEFRAMES:
        return _empty_entry_plan(
            params,
            state="unsupported_timeframe",
            reason_code="entry_plan_supported_on_3m_5m",
            source_tf=source_tf,
        )
    eligible_events = [
        event
        for event in events
        if bool(event.get("confirmed")) and event.get("previous_state") != "neutral"
    ]
    if not eligible_events:
        return _empty_entry_plan(
            params,
            state="observing",
            reason_code="await_confirmed_regime_transition",
            source_tf=source_tf,
        )

    event = eligible_events[-1]
    signal_index = int(event["index"])
    latest_index = len(bars) - 1
    direction = "long" if event["direction"] == Direction.LONG.value else "short"
    expected_state = "bullish" if direction == "long" else "bearish"
    timeframe_minutes = interval_minutes(source_tf)
    wait_bars = max(ADAPTIVE_KERNEL_ENTRY_PLAN_WAIT_MINUTES // timeframe_minutes, 1)
    signal_available_at = bars[signal_index].ts + timedelta(minutes=timeframe_minutes)
    expires_at = signal_available_at + timedelta(minutes=ADAPTIVE_KERNEL_ENTRY_PLAN_WAIT_MINUTES)
    entry_method = "upper_band_reclaim" if direction == "long" else "ema20_reclaim"
    trigger_line = "upper" if direction == "long" else "ema20"
    confirmation = "close_above_trigger" if direction == "long" else "close_below_trigger"
    trigger_level = float(event["upper"]) if direction == "long" else float(ema20[signal_index])
    plan = _empty_entry_plan(
        params,
        state="armed",
        reason_code="await_entry_pullback",
        source_tf=source_tf,
    )
    plan.update(
        {
            "direction": direction,
            "active": True,
            "entry_method": entry_method,
            "trigger_line": trigger_line,
            "trigger_level": _rounded(trigger_level),
            "confirmation": confirmation,
            "signal_ts": str(event["ts"]),
            "signal_available_at": signal_available_at.isoformat(),
            "signal_price": _rounded(float(event["price"])),
            "expires_at": expires_at.isoformat(),
            "wait_bars": wait_bars,
            "bars_since_signal": max(latest_index - signal_index, 0),
        }
    )
    if latest_index <= signal_index:
        return plan

    series_by_index = {int(row["index"]): row for row in series}
    first_touch_index: int | None = None
    reclaim_index: int | None = None
    reclaim_level: float | None = None
    scan_end = min(latest_index, signal_index + wait_bars)
    for index in range(signal_index + 1, scan_end + 1):
        previous_row = series_by_index.get(index - 1)
        if previous_row is None or previous_row.get("state") != expected_state:
            plan.update(
                {
                    "state": "invalidated",
                    "active": False,
                    "reason_code": "opposite_kernel_regime",
                }
            )
            return plan
        level = float(previous_row["upper"]) if direction == "long" else float(ema20[index - 1])
        trigger_level = level
        if not _entry_plan_touch(bars[index], level, direction):
            continue
        if first_touch_index is None:
            first_touch_index = index
        current_row = series_by_index.get(index)
        reclaimed = (
            float(bars[index].close) > level
            if direction == "long"
            else float(bars[index].close) < level
        )
        if reclaimed and current_row is not None and current_row.get("state") == expected_state:
            reclaim_index = index
            reclaim_level = level
            break

    plan["trigger_level"] = _rounded(reclaim_level if reclaim_level is not None else trigger_level)
    if first_touch_index is not None:
        plan["touch_ts"] = bars[first_touch_index].ts.isoformat()
    if reclaim_index is not None:
        entry_index = reclaim_index + 1
        plan.update(
            {
                "reclaim_ts": bars[reclaim_index].ts.isoformat(),
                "reason_code": "entry_reclaim_confirmed",
            }
        )
        if entry_index >= len(bars):
            plan.update(
                {
                    "state": "ready_next_open",
                    "entry_ready": True,
                }
            )
            return plan
        plan.update(
            {
                "state": "triggered",
                "active": False,
                "triggered": True,
                "entry_ts": bars[entry_index].ts.isoformat(),
                "entry_price": _rounded(float(bars[entry_index].open)),
            }
        )
        return plan
    if latest_index > signal_index + wait_bars:
        plan.update(
            {
                "state": "expired",
                "active": False,
                "reason_code": "entry_plan_expired",
            }
        )
    elif first_touch_index is not None:
        plan.update(
            {
                "state": "waiting_reclaim",
                "reason_code": "await_close_reclaim",
            }
        )
    else:
        plan.update(
            {
                "state": "waiting_pullback",
                "reason_code": "await_entry_pullback",
            }
        )
    return plan


def _pine_seeded_rma(values: Sequence[float], length: int) -> list[float]:
    """Pine ta.rma-compatible values with an SMA seed at length - 1."""

    if not values:
        return []
    window = max(int(length), 1)
    if window == 1:
        return [float(value) for value in values]
    out = [0.0] * len(values)
    if len(values) < window:
        return out
    seed_index = window - 1
    previous = sum(float(value) for value in values[:window]) / window
    out[seed_index] = previous
    alpha = 1.0 / window
    for index in range(seed_index + 1, len(values)):
        previous = previous * (1.0 - alpha) + float(values[index]) * alpha
        out[index] = previous
    return out


def _pine_atr_values(bars: Sequence[Bar], length: int) -> list[float]:
    return _pine_seeded_rma(pine.true_range_series(bars), length)


def _effective_bandwidths(
    bars: Sequence[Bar],
    params: AdaptiveKernelRegressionParams,
) -> list[float]:
    if not bars:
        return []
    if not params.adaptive_bandwidth:
        return [params.base_bandwidth] * len(bars)
    atr_values = _pine_atr_values(bars, params.atr_len)
    seed_index = params.atr_len - 1
    factors = [0.0] * len(bars)
    close = float(bars[seed_index].close)
    factor = atr_values[seed_index] / max(close, 1e-12)
    factors[seed_index] = factor
    alpha = 2.0 / (params.atr_len + 1.0)
    for index in range(seed_index + 1, len(bars)):
        close = float(bars[index].close)
        normalized_atr = atr_values[index] / max(close, 1e-12)
        factor = normalized_atr * alpha + factor * (1.0 - alpha)
        factors[index] = factor
    return [
        params.base_bandwidth * (1.0 + factor * ADAPTIVE_KERNEL_ADAPTATION_STRENGTH)
        for factor in factors
    ]


def _gaussian_kernel_values(
    closes: Sequence[float],
    bandwidths: Sequence[float],
    *,
    lookback: int,
    start_index: int,
) -> list[float]:
    values: list[float] = []
    for index in range(start_index, len(closes)):
        bandwidth = max(float(bandwidths[index]), 1e-12)
        denominator = 2.0 * bandwidth * bandwidth
        weighted_sum = 0.0
        weight_sum = 0.0
        for lag in range(lookback):
            weight = exp(-(lag * lag) / denominator)
            weighted_sum += weight * float(closes[index - lag])
            weight_sum += weight
        value = weighted_sum / max(weight_sum, 1e-12)
        if not isfinite(value):
            raise ValueError("adaptive kernel regression produced a non-finite value")
        values.append(value)
    return values


def _transition_label(
    event: dict[str, Any],
    params: AdaptiveKernelRegressionParams,
) -> dict[str, Any]:
    bullish = event["state"] == "bullish"
    if params.label_anchor == "high_low":
        base_price = event["bar_low"] if bullish else event["bar_high"]
    elif params.label_anchor == "bands":
        base_price = event["lower"] if bullish else event["upper"]
    else:
        base_price = event["center"]
    offset = float(event["label_atr"]) * params.label_offset_mult
    return {
        "type": "label",
        "ts": event["ts"],
        "price": _rounded(base_price - offset if bullish else base_price + offset),
        "lines": ["BUY" if bullish else "SELL"],
        "side": "below" if bullish else "above",
        "anchor": "below" if bullish else "above",
        "anchor_price_mode": "overlay",
        "tone": "positive" if bullish else "negative",
        "opacity": 0.94,
        "interactive": True,
        "no_tick": True,
        "role": "kernel_regime_transition",
        "direction": event["direction"],
        "event_code": event["event_code"],
        "code": event["code"],
        "layer": "signals",
    }


def _dashboard(
    latest: dict[str, Any],
    params: AdaptiveKernelRegressionParams,
    plan: dict[str, Any],
) -> dict[str, Any]:
    bullish = latest["state"] == "bullish"
    tone = "positive" if bullish else "negative"
    table = overlays.table(
        table_id="adaptive-kernel-regression",
        model_ref="adaptive_kernel_regression",
        model={
            "state": latest["state"],
            "center": latest["center"],
            "upper": latest["upper"],
            "lower": latest["lower"],
            "residual_sigma": latest["residual_sigma"],
            "effective_bandwidth": latest["effective_bandwidth"],
            "adaptive_bandwidth": params.adaptive_bandwidth,
            "plan": plan,
        },
        tone=tone,
        role="kernel_regime_dashboard",
    )
    table["control_key"] = "dashboard"
    table["layer"] = "tables"
    return table


def _overlays(
    bars: Sequence[Bar],
    series: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    params: AdaptiveKernelRegressionParams,
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    if not series:
        return []
    visible_series = list(series[-params.render_bars :])
    latest = visible_series[-1]
    payload: list[dict[str, Any]] = [
        {
            "type": "custom",
            "source": ADAPTIVE_KERNEL_REGRESSION_SOURCE,
            "renderer_ref": "adaptive_kernel_regression_series",
            "payload": {
                "kind": "indicator_state_series_v1",
                "render_bars": params.render_bars,
            },
            "render_key": (
                f"{latest['ts']}:{latest['center']}:{latest['upper']}:"
                f"{latest['lower']}:{len(series)}:{params.render_bars}"
            ),
            "role": "kernel_regression_series",
            "layer": "levels",
        }
    ]
    first_visible_index = int(visible_series[0]["index"])
    if not params.entry_plan_enabled:
        payload.extend(
            _transition_label(event, params)
            for event in events
            if (int(event["index"]) >= first_visible_index and event["previous_state"] != "neutral")
        )
    if (
        plan.get("state") in _ENTRY_PLAN_ACTIVE_STATES
        and plan.get("trigger_level") is not None
        and bars
    ):
        remaining_bars = max(
            int(plan.get("wait_bars") or 1) - int(plan.get("bars_since_signal") or 0),
            1,
        )
        plan_line = overlays.line(
            start=bars[-1],
            price=float(plan["trigger_level"]),
            label_text=None,
            bars_forward=remaining_bars,
            width=1.4,
            style="dashed",
            role="kernel_entry_plan_trigger",
            direction=str(plan.get("direction") or "flat"),
        )
        plan_line.update(
            {
                "source": ADAPTIVE_KERNEL_REGRESSION_SOURCE,
                "source_tf": str(plan.get("source_tf") or ""),
                "state_code": str(plan.get("state") or ""),
                "action": "ENTER_NEXT_OPEN" if plan.get("entry_ready") is True else "WATCH",
                "action_reason_code": str(plan.get("reason_code") or ""),
                "level_kind": str(plan.get("trigger_line") or "entry_trigger"),
                "layer": "signals",
            }
        )
        payload.append(plan_line)
    payload.append(_dashboard(latest, params, plan))
    return payload


def adaptive_kernel_regression(
    bars: Sequence[Bar],
    *,
    params: AdaptiveKernelRegressionParams | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    resolved = params or AdaptiveKernelRegressionParams()
    settings = {
        "calculation_mode": resolved.calculation_mode,
        "lookback_window": resolved.lookback_window,
        "base_bandwidth": resolved.base_bandwidth,
        "adaptive_bandwidth": resolved.adaptive_bandwidth,
        "adaptation_strength": ADAPTIVE_KERNEL_ADAPTATION_STRENGTH,
        "atr_len": resolved.atr_len,
        "output_smoothing": resolved.output_smoothing,
        "band_multiplier": resolved.band_multiplier,
        "band_lookback": resolved.band_lookback,
        "band_smoothing": resolved.band_smoothing,
        "entry_plan_enabled": resolved.entry_plan_enabled,
        "entry_plan_wait_minutes": ADAPTIVE_KERNEL_ENTRY_PLAN_WAIT_MINUTES,
        "label_anchor": resolved.label_anchor,
        "label_offset_mult": resolved.label_offset_mult,
        "required_bars": resolved.required_bars,
        "render_bars": resolved.render_bars,
    }
    if len(bars) < resolved.required_bars:
        plan = _empty_entry_plan(
            resolved,
            state="unavailable" if resolved.entry_plan_enabled else "disabled",
            reason_code=("kernel_warmup" if resolved.entry_plan_enabled else "entry_plan_disabled"),
            source_tf=str(bars[-1].timeframe or "").strip().lower() if bars else "",
        )
        return {
            "version": ADAPTIVE_KERNEL_REGRESSION_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "plan": plan,
            "overlays": [],
            "signals": [],
            "settings": settings,
            "availability": {
                "state": "blocked",
                "reason_code": "kernel_warmup",
                "required_bars": resolved.required_bars,
                "available_bars": len(bars),
            },
        }

    closes = [float(bar.close) for bar in bars]
    bandwidths = _effective_bandwidths(bars, resolved)
    kernel_start = max(
        resolved.lookback_window - 1,
        resolved.atr_len - 1 if resolved.adaptive_bandwidth else 0,
    )
    kernel_raw = _gaussian_kernel_values(
        closes,
        bandwidths,
        lookback=resolved.lookback_window,
        start_index=kernel_start,
    )
    centers = pine.ema_series(kernel_raw, resolved.output_smoothing)
    residuals = [closes[kernel_start + offset] - center for offset, center in enumerate(centers)]
    sigma_raw = pine.rolling_stdev_series(residuals, resolved.band_lookback)
    band_offset = resolved.band_lookback - 1
    sigma_values = pine.ema_series(
        sigma_raw[band_offset:],
        resolved.band_smoothing,
    )
    label_atr_values = _pine_atr_values(bars, 14)

    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    state = "neutral"
    regime_age = 0
    series_start = kernel_start + band_offset
    for offset, sigma in enumerate(sigma_values):
        index = series_start + offset
        center = centers[band_offset + offset]
        residual = residuals[band_offset + offset]
        upper = center + resolved.band_multiplier * sigma
        lower = center - resolved.band_multiplier * sigma
        previous_state = state
        close = closes[index]
        comparison_tolerance = max(abs(close) * 1e-12, 1e-12)
        if close > upper + comparison_tolerance:
            state = "bullish"
        elif close < lower - comparison_tolerance:
            state = "bearish"
        if state == previous_state:
            regime_age = regime_age + 1 if state != "neutral" else 0
        else:
            regime_age = 1
        deviation_z = residual / sigma if sigma > 1e-12 else 0.0
        row = {
            "ts": bars[index].ts.isoformat(),
            "index": index,
            "center": _rounded(center),
            "upper": _rounded(upper),
            "lower": _rounded(lower),
            "residual": _rounded(residual),
            "residual_sigma": _rounded(sigma),
            "deviation_z": round(deviation_z, 6),
            "effective_bandwidth": round(bandwidths[index], 6),
            "regime_code": state,
            "regime_age_bars": regime_age,
            "state": state,
        }
        series.append(row)
        if state != previous_state and state != "neutral":
            bullish = state == "bullish"
            events.append(
                {
                    "ts": row["ts"],
                    "index": index,
                    "bar_high": _rounded(bars[index].high),
                    "bar_low": _rounded(bars[index].low),
                    "label_atr": _rounded(label_atr_values[index]),
                    "event_code": ("kernel_regime_bullish" if bullish else "kernel_regime_bearish"),
                    "code": "KERNEL_REGIME_BULLISH" if bullish else "KERNEL_REGIME_BEARISH",
                    "direction": Direction.LONG.value if bullish else Direction.SHORT.value,
                    "previous_state": previous_state,
                    "state": state,
                    "price": _rounded(close),
                    "center": row["center"],
                    "upper": row["upper"],
                    "lower": row["lower"],
                    "deviation_z": row["deviation_z"],
                    "effective_bandwidth": row["effective_bandwidth"],
                    "confirmed": bool(bars[index].closed),
                    "source": ADAPTIVE_KERNEL_REGRESSION_SOURCE,
                }
            )

    events = events[-resolved.max_events :]
    latest = series[-1] if series else None
    ema20 = pine.ema_series(closes, 20)
    plan = (
        _entry_plan(bars, series, events, ema20, resolved)
        if not preview_only
        else _empty_entry_plan(
            resolved,
            state="preview_ignored" if resolved.entry_plan_enabled else "disabled",
            reason_code=(
                "entry_plan_confirmed_bars_only"
                if resolved.entry_plan_enabled
                else "entry_plan_disabled"
            ),
            source_tf=str(bars[-1].timeframe or "").strip().lower() if bars else "",
        )
    )
    if preview_only:
        preview_ts = latest["ts"] if latest else ""
        return {
            "version": ADAPTIVE_KERNEL_REGRESSION_VERSION,
            "series": [],
            "events": [event for event in events if event["ts"] == preview_ts],
            "latest": latest,
            "plan": plan,
            "overlays": [],
            "signals": [],
            "settings": settings,
            "availability": {
                "state": "ready",
                "reason_code": "kernel_preview_ready",
                "required_bars": resolved.required_bars,
                "available_bars": len(bars),
            },
        }
    return {
        "version": ADAPTIVE_KERNEL_REGRESSION_VERSION,
        "series": series,
        "events": events,
        "latest": latest,
        "plan": plan,
        "overlays": _overlays(bars, series, events, resolved, plan),
        "signals": [],
        "settings": settings,
        "availability": {
            "state": "ready",
            "reason_code": "kernel_ready",
            "required_bars": resolved.required_bars,
            "available_bars": len(bars),
        },
    }
