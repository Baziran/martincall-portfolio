from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.context import FeatureContext, build_feature_context
from aef_terminal.features.market_context import provider_new_york_range_context, vwap_band_series
from aef_terminal.features.provider_session import (
    ProviderSessionInterval,
    provider_session_bar_indexes,
    provider_session_intervals,
    provider_vwap_session,
)
from aef_terminal.indicators.defaults import indicator_defaults_from_params
from aef_terminal.runtime import pine
from aef_terminal.runtime.instruments import resolve_instrument_profile


def _latest_finite(values: Sequence[Any]) -> float | None:
    for value in reversed(values):
        if isinstance(value, dict):
            value = value.get("vwap")
        try:
            number = float(value)
        except TypeError, ValueError:
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return None


def _chart_guide_generation(calculation_bars: Sequence[Bar]) -> str:
    rows = [
        [
            bar.ts.isoformat(),
            round(float(bar.open), 8),
            round(float(bar.high), 8),
            round(float(bar.low), 8),
            round(float(bar.close), 8),
            round(float(bar.volume), 8),
        ]
        for bar in calculation_bars
    ]
    raw = json.dumps(rows, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _project_values_to_display_axis(
    calculation_bars: Sequence[Bar],
    values: Sequence[Any],
    display_axis: Sequence[Mapping[str, Any]],
    *,
    missing: Any,
) -> list[Any]:
    """Project calculated facts onto the terminal-owned transport axis.

    Provisional display slots are intentionally absent from calculation bars,
    so they receive an explicit unavailable value without entering indicator
    math or changing any calculated point.
    """

    by_ts = {
        bar.ts.astimezone(UTC).isoformat(): value
        for bar, value in zip(calculation_bars, values, strict=False)
    }
    return [by_ts.get(str(item.get("ts") or ""), missing) for item in display_axis]


def _chart_vwap_values(
    calculation_bars: Sequence[Bar],
    *,
    instrument: Mapping[str, Any] | None,
) -> tuple[list[dict[str, float | None]], dict[str, Any], float | None]:
    """Calculate only the VWAP segments backed by exact provider sessions."""

    session = provider_vwap_session(instrument, calculation_bars)
    empty = [{"vwap": None, "sigma": None} for _ in calculation_bars]
    intervals = provider_session_intervals(instrument)
    grouped_bar_indexes: dict[str, tuple[int, ...]] | None = None
    if intervals:
        try:
            grouped_bar_indexes = provider_session_bar_indexes(intervals, calculation_bars)
        except ValueError:
            grouped_bar_indexes = None
    if session.available:
        reset = session.key_for_bar
        if session.calendar != "continuous_24_7":
            if grouped_bar_indexes is None:
                return empty, session.status_payload(), None
            key_by_ts = {
                calculation_bars[index].ts: session_key
                for session_key, indexes in grouped_bar_indexes.items()
                for index in indexes
            }

            def reset(bar: Bar) -> str:
                return key_by_ts[bar.ts]

        sigma, _, _, _, _ = vwap_band_series(
            list(calculation_bars),
            reset=reset,
        )
        values = pine.vwap_series(list(calculation_bars), reset=reset)
        series = [
            {
                "vwap": round(values[index], 6) if index < len(values) else None,
                "sigma": round(sigma[index], 6) if index < len(sigma) else None,
            }
            for index in range(len(calculation_bars))
        ]
        latest = series[-1]["vwap"] if series else None
        return series, session.status_payload(), latest

    if not intervals or not calculation_bars:
        return empty, session.status_payload(), None

    series = list(empty)
    if grouped_bar_indexes is None:
        return empty, session.status_payload(), None
    assigned: set[int] = set()
    for session_key, bar_indexes in grouped_bar_indexes.items():
        segment = [calculation_bars[index] for index in bar_indexes]
        sigma, _, _, _, _ = vwap_band_series(
            segment,
            reset=lambda _bar, key=session_key: key,
        )
        values = pine.vwap_series(
            segment,
            reset=lambda _bar, key=session_key: key,
        )
        for segment_index, bar_index in enumerate(bar_indexes):
            series[bar_index] = {
                "vwap": round(values[segment_index], 6),
                "sigma": round(sigma[segment_index], 6),
            }
            assigned.add(bar_index)

    covered_count = len(assigned)
    if not covered_count:
        return empty, session.status_payload(), None
    latest = series[-1]["vwap"] if series else None
    status = {
        **session.status_payload(),
        "available": latest is not None,
        "source": "provider_trading_intervals",
        "reason_code": "partial_provider_session_coverage",
        "coverage": "partial",
        "covered_bar_count": covered_count,
        "bar_count": len(calculation_bars),
    }
    return series, status, latest


def _bar_summary(bars: Sequence[Bar]) -> dict[str, Any] | None:
    if not bars:
        return None
    volume = sum(bar.volume for bar in bars)
    weighted = sum(((bar.high + bar.low + bar.close) / 3.0) * bar.volume for bar in bars)
    return {
        "open": bars[0].open,
        "high": max(bar.high for bar in bars),
        "low": min(bar.low for bar in bars),
        "close": bars[-1].close,
        "volume": volume,
        "startTs": bars[0].ts.isoformat(),
        "endTs": bars[-1].ts.isoformat(),
        "count": len(bars),
        "vwap": weighted / volume if volume > 0 else None,
    }


def _chart_context(
    bars: Sequence[Bar],
    *,
    instrument: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not bars:
        return {}
    new_york_range = provider_new_york_range_context(
        bars,
        instrument=instrument,
    )
    opening_range = (
        {
            "high": new_york_range["high"],
            "low": new_york_range["low"],
            "mid": new_york_range["mid"],
            "startTs": new_york_range["start_ts"],
            "endTs": new_york_range["end_ts"],
            "sessionEndTs": new_york_range["session_end_ts"],
            "durationMinutes": new_york_range["duration_minutes"],
            "complete": bool(new_york_range["ready"]),
            "day": new_york_range["day"],
            "session": new_york_range["session"],
            "timezone": new_york_range["timezone"],
            "source": "provider_new_york_range_context",
            "authority": "display_only",
        }
        if new_york_range.get("active")
        else None
    )
    display_context = {"openingRange": opening_range} if opening_range is not None else {}
    session = instrument.get("session") if isinstance(instrument, Mapping) else None
    calendar = (
        str(session.get("calendar") or "").strip().lower() if isinstance(session, Mapping) else ""
    )
    trading = provider_session_intervals(instrument, "trading_intervals")
    liquid = provider_session_intervals(instrument, "liquid_intervals")
    if not trading and calendar == "continuous_24_7":
        days = sorted({bar.ts.astimezone(UTC).date() for bar in bars})
        trading = tuple(
            ProviderSessionInterval(
                opens_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                closes_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                + timedelta(days=1),
                session_date=day,
            )
            for day in days
        )
        liquid = trading
    if not trading:
        return display_context

    try:
        trading_bar_indexes = provider_session_bar_indexes(trading, bars)
        liquid_bar_indexes = provider_session_bar_indexes(liquid, bars) if liquid else {}
    except ValueError:
        return display_context
    latest = bars[-1]
    latest_index = len(bars) - 1
    current_key = next(
        (
            session_key
            for session_key, indexes in trading_bar_indexes.items()
            if latest_index in indexes
        ),
        None,
    )
    if current_key is None:
        return display_context
    current_bars = [bars[index] for index in trading_bar_indexes[current_key]]
    session_order = sorted(
        trading_bar_indexes,
        key=lambda session_key: min(
            interval.opens_at for interval in trading if interval.session_key == session_key
        ),
    )
    current_session_index = session_order.index(current_key)
    previous_bars: list[Bar] = []
    previous_key = ""
    for session_key in reversed(session_order[:current_session_index]):
        candidate = [bars[index] for index in trading_bar_indexes[session_key]]
        if candidate:
            previous_bars = candidate
            previous_key = session_key
            break

    liquid_bars = [bars[index] for index in liquid_bar_indexes.get(current_key, ())]

    local_bars = list(bars[-30:])
    return {
        "symbol": str((instrument or {}).get("display") or latest.symbol),
        "timeframe": latest.timeframe,
        "latest": {
            "ts": latest.ts.isoformat(),
            "price": latest.close,
            "open": latest.open,
            "high": latest.high,
            "low": latest.low,
            "volume": latest.volume,
        },
        "day": {
            "key": current_key,
            **(_bar_summary(current_bars) or {}),
        },
        "previousDay": (
            {"key": previous_key, **(_bar_summary(previous_bars) or {})} if previous_bars else None
        ),
        "local": {
            "high": max(bar.high for bar in local_bars),
            "low": min(bar.low for bar in local_bars),
            "lookback": len(local_bars),
        },
        "sessions": {
            "trading": _bar_summary(current_bars),
            "liquid": _bar_summary(liquid_bars),
        },
        "openingRange": opening_range,
    }


def display_feature_context(
    cached_feature_context: FeatureContext,
    *,
    live_feature_context: FeatureContext,
    display_bars: Sequence[Bar],
) -> FeatureContext:
    if len(live_feature_context.bars) == len(display_bars):
        return live_feature_context
    if len(cached_feature_context.bars) == len(display_bars):
        return cached_feature_context
    return build_feature_context(
        list(display_bars),
        instrument_profile=cached_feature_context.instrument_profile,
        defaults=cached_feature_context.defaults,
    )


@dataclass(frozen=True, slots=True)
class EmaTouchGuideProjection:
    level: float
    atr: float
    latest_low: float
    latest_high: float
    ema_length: int
    atr_length: int


def ema_touch_guide_projection(
    bars: Sequence[Bar],
    *,
    instrument: Mapping[str, Any],
    global_defaults: Mapping[str, Any] | None = None,
) -> EmaTouchGuideProjection | None:
    normalized = list(pine.PineContext.from_bars(bars).bars)
    if not normalized:
        return None
    defaults = indicator_defaults_from_params(dict(global_defaults or {}))
    feature_context = build_feature_context(
        normalized,
        instrument_profile=resolve_instrument_profile(dict(instrument)),
        defaults=defaults,
    )
    if not feature_context.ema_magnet:
        return None
    latest = feature_context.bars[-1]
    return EmaTouchGuideProjection(
        level=float(feature_context.ema_magnet[-1]),
        atr=feature_context.latest_atr,
        latest_low=float(latest.low),
        latest_high=float(latest.high),
        ema_length=defaults.ema.magnet,
        atr_length=defaults.atr_len,
    )


def ema_touch_guide_state_from_projection(
    projection: EmaTouchGuideProjection,
    *,
    current_price: float,
    tolerance_atr: float = 0.08,
    tolerance_points: float = 0.0,
) -> dict[str, float | int | bool]:
    tolerance = max(
        float(tolerance_points),
        projection.atr * float(tolerance_atr),
        abs(current_price) * 0.000015,
        0.01,
    )
    low = min(projection.latest_low, current_price)
    high = max(projection.latest_high, current_price)
    return {
        "level": projection.level,
        "tolerance": tolerance,
        "touching": low <= projection.level + tolerance and high >= projection.level - tolerance,
        "ema_length": projection.ema_length,
        "atr_length": projection.atr_length,
    }


def ema_touch_guide_state(
    bars: Sequence[Bar],
    *,
    current_price: float,
    instrument: Mapping[str, Any],
    global_defaults: Mapping[str, Any] | None = None,
    tolerance_atr: float = 0.08,
    tolerance_points: float = 0.0,
) -> dict[str, float | int | bool] | None:
    projection = ema_touch_guide_projection(
        bars,
        instrument=instrument,
        global_defaults=global_defaults,
    )
    if projection is None:
        return None
    return ema_touch_guide_state_from_projection(
        projection,
        current_price=current_price,
        tolerance_atr=tolerance_atr,
        tolerance_points=tolerance_points,
    )


def build_chart_guides(
    calculation_bars: Sequence[Bar],
    *,
    symbol: str,
    feature_context: FeatureContext,
    instrument: Mapping[str, Any] | None = None,
    display_axis: Sequence[Mapping[str, Any]] | None = None,
    context_bars: Sequence[Bar] | None = None,
    include_telemetry: bool = False,
    magnet_warmup_failed: bool = False,
) -> dict[str, Any]:
    if len(feature_context.bars) != len(calculation_bars):
        raise ValueError(
            f"chart guides require feature context bar count {len(feature_context.bars)} "
            f"to match calculation bars {len(calculation_bars)}"
        )
    started = perf_counter() if include_telemetry else 0.0
    defaults = feature_context.defaults
    ema_defaults = defaults.ema
    atr_len = max(2, min(int(defaults.atr_len or 14), 100))
    pullback_len = max(5, min(int(ema_defaults.pullback or 20), 300))
    trend_len = max(10, min(int(ema_defaults.slow or 55), 400))
    magnet_len = max(50, min(int(ema_defaults.magnet or 233), 1000))
    vwap, vwap_status, latest_vwap = _chart_vwap_values(
        calculation_bars,
        instrument=instrument,
    )
    ema = {
        str(pullback_len): feature_context.ema_pullback,
        str(trend_len): feature_context.ema_slow,
        str(magnet_len): feature_context.ema_magnet,
    }
    ema_status: dict[str, Any] = {}
    if calculation_bars and calculation_bars[0].timeframe == "5m":
        calculation_bar_count = len(calculation_bars)
        target_warmup_bar_count = magnet_len * 3
        magnet_available = calculation_bar_count >= magnet_len
        warmup_complete = calculation_bar_count >= target_warmup_bar_count
        if not magnet_available:
            magnet_reason = "insufficient_calculation_bars"
        elif not warmup_complete:
            magnet_reason = "limited_warmup"
        else:
            magnet_reason = ""
        ema_status["magnet"] = {
            "available": magnet_available,
            "warmup_complete": warmup_complete,
            "warmup_status": (
                "complete" if warmup_complete else "failed" if magnet_warmup_failed else "limited"
            ),
            "reason": magnet_reason,
            "length": magnet_len,
            "required_bar_count": magnet_len,
            "target_warmup_bar_count": target_warmup_bar_count,
            "calculation_bar_count": calculation_bar_count,
        }
        if not magnet_available:
            ema[str(magnet_len)] = [None for _bar in calculation_bars]
    if display_axis is not None:
        ema = {
            length: _project_values_to_display_axis(
                calculation_bars,
                values,
                display_axis,
                missing=None,
            )
            for length, values in ema.items()
        }
        vwap = _project_values_to_display_axis(
            calculation_bars,
            vwap,
            display_axis,
            missing={"vwap": None, "sigma": None},
        )
    latest_atr = feature_context.latest_atr
    chart_context_bars = list(context_bars) if context_bars is not None else list(calculation_bars)
    guides = {
        "ema": ema,
        "vwap": vwap,
        "latest": {
            "ema_pullback": _latest_finite(ema.get(str(pullback_len), [])),
            "ema_trend": _latest_finite(ema.get(str(trend_len), [])),
            "ema_magnet": _latest_finite(ema.get(str(magnet_len), [])),
            "vwap": latest_vwap,
            "atr14": latest_atr,
            "atr": latest_atr,
            "atr_len": atr_len,
        },
        "lengths": {
            "pullback": pullback_len,
            "trend": trend_len,
            "magnet": magnet_len,
        },
        "context": _chart_context(chart_context_bars, instrument=instrument),
        "vwap_status": vwap_status,
    }
    if ema_status:
        guides["ema_status"] = ema_status
    guides["meta"] = {
        "bar_count": len(display_axis) if display_axis is not None else len(calculation_bars),
        "calculation_bar_count": len(calculation_bars),
        "generation": _chart_guide_generation(calculation_bars),
    }
    if include_telemetry:
        guides["meta"]["compute_ms"] = round((perf_counter() - started) * 1000.0, 3)
    _ = symbol
    return guides
