from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import exp, fsum, log, sqrt
from statistics import fmean
from typing import TypeVar

from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance
from aef_terminal.runtime.bar_quality import is_provisional_bar
from aef_terminal.runtime.math_utils import clamp, safe_div
from aef_terminal.runtime.timeframes import interval_minutes as _canonical_interval_minutes
from aef_terminal.runtime.timeframes import interval_bucket as interval_bucket


T = TypeVar("T")


def sigmoid(value: float) -> float:
    value = clamp(value, -12.0, 12.0)
    return 1.0 / (1.0 + exp(-value))


interval_minutes = _canonical_interval_minutes


def bar_is_closed(ts: datetime, interval: str, now: datetime | None = None) -> bool:
    current = now or datetime.now(tz=UTC)
    bucket = interval_bucket(ts, interval)
    return current >= bucket + timedelta(minutes=interval_minutes(interval))


def split_confirmed_bars(bars: Sequence[Bar]) -> tuple[list[Bar], Bar | None]:
    confirmed = [bar for bar in bars if not is_provisional_bar(bar)]
    live_bar = bars[-1] if bars and is_provisional_bar(bars[-1]) else None
    return confirmed, live_bar


def bar_index_at_or_before(
    bars: Sequence[Bar],
    ts: datetime,
) -> int | None:
    found: int | None = None
    for index, bar in enumerate(bars):
        if bar.ts <= ts:
            found = index
        else:
            break
    return found


def source(values: Sequence[Bar], name: str) -> list[float]:
    if name == "hl2":
        return [(bar.high + bar.low) / 2.0 for bar in values]
    if name == "hlc3":
        return [(bar.high + bar.low + bar.close) / 3.0 for bar in values]
    if name == "ohlc4":
        return [(bar.open + bar.high + bar.low + bar.close) / 4.0 for bar in values]
    return [float(getattr(bar, name)) for bar in values]


@dataclass(frozen=True)
class BarAnatomy:
    rng: float
    body: float
    body_signed: float
    body_share: float
    upper: float
    lower: float
    upper_share: float
    lower_share: float
    close_pos: float
    direction: int


def bar_anatomy(bar: Bar) -> BarAnatomy:
    rng = max(bar.high - bar.low, 0.000001)
    body_signed = bar.close - bar.open
    body = abs(body_signed)
    upper = max(bar.high - max(bar.open, bar.close), 0.0)
    lower = max(min(bar.open, bar.close) - bar.low, 0.0)
    return BarAnatomy(
        rng=rng,
        body=body,
        body_signed=body_signed,
        body_share=body / rng,
        upper=upper,
        lower=lower,
        upper_share=upper / rng,
        lower_share=lower / rng,
        close_pos=clamp((bar.close - bar.low) / rng, 0.0, 1.0),
        direction=1 if body_signed > 0 else -1 if body_signed < 0 else 0,
    )


def bar_anatomy_series(bars: Sequence[Bar]) -> list[BarAnatomy]:
    return [bar_anatomy(bar) for bar in bars]


def value_at(
    values: Sequence[float], index: int, offset: int = 0, fallback: float | None = None
) -> float:
    target = index - offset
    if 0 <= target < len(values):
        return float(values[target])
    if fallback is not None:
        return fallback
    if values:
        return float(values[0])
    return 0.0


def mean_at(values: Sequence[float], index: int, length: int) -> float:
    start = max(0, index - max(length, 1) + 1)
    window = values[start : index + 1]
    return fmean(window) if window else 0.0


def stdev_at(values: Sequence[float], index: int, length: int) -> float:
    start = max(0, index - max(length, 1) + 1)
    window = values[start : index + 1]
    if len(window) <= 1:
        return 0.0
    # Center before summing to retain small spreads on large price baselines.
    # This is population variance, without pstdev's per-window rational algebra
    # or the cancellation of E[x*x] - E[x]**2.
    origin = float(window[0])
    centered = [float(value) - origin for value in window]
    mean = fsum(centered) / len(centered)
    return sqrt(fsum((value - mean) ** 2 for value in centered) / len(centered))


def rolling_mean_series(values: Sequence[float], length: int) -> list[float]:
    window_len = max(int(length), 1)
    out: list[float] = []
    window: deque[float] = deque()
    total = 0.0
    for raw in values:
        value = float(raw)
        window.append(value)
        total += value
        if len(window) > window_len:
            total -= window.popleft()
        out.append(total / len(window) if window else 0.0)
    return out


def rolling_stdev_series(values: Sequence[float], length: int) -> list[float]:
    window_len = max(int(length), 1)
    out: list[float] = []
    window: deque[float] = deque()
    total = 0.0
    total_sq = 0.0
    for raw in values:
        value = float(raw)
        window.append(value)
        total += value
        total_sq += value * value
        if len(window) > window_len:
            old = window.popleft()
            total -= old
            total_sq -= old * old
        count = len(window)
        if count <= 1:
            out.append(0.0)
            continue
        mean = total / count
        variance = max(total_sq / count - mean * mean, 0.0)
        out.append(variance**0.5)
    return out


def _rolling_extreme_series(
    values: Sequence[float], length: int, *, exclude_current: bool, high: bool
) -> list[float | None]:
    window_len = max(int(length), 1)
    out: list[float | None] = []
    candidates: deque[int] = deque()

    def push(index: int) -> None:
        value = float(values[index])
        while candidates:
            candidate_value = float(values[candidates[-1]])
            if (high and candidate_value >= value) or (not high and candidate_value <= value):
                break
            candidates.pop()
        candidates.append(index)

    for index in range(len(values)):
        if exclude_current:
            while candidates and candidates[0] < index - window_len:
                candidates.popleft()
            out.append(float(values[candidates[0]]) if candidates else None)
            push(index)
        else:
            push(index)
            while candidates and candidates[0] < index - window_len + 1:
                candidates.popleft()
            out.append(float(values[candidates[0]]) if candidates else None)
    return out


def rolling_high_series(
    values: Sequence[float], length: int, exclude_current: bool = True
) -> list[float | None]:
    return _rolling_extreme_series(values, length, exclude_current=exclude_current, high=True)


def rolling_low_series(
    values: Sequence[float], length: int, exclude_current: bool = True
) -> list[float | None]:
    return _rolling_extreme_series(values, length, exclude_current=exclude_current, high=False)


def percent_rank(values: Sequence[float], index: int, length: int) -> float:
    start = max(0, index - max(length, 1) + 1)
    window = values[start : index + 1]
    if not window:
        return 50.0
    current = values[index]
    return clamp(sum(1 for value in window if value <= current) / len(window) * 100.0, 0.0, 100.0)


def nz(value: float | None, fallback: float = 0.0) -> float:
    return fallback if value is None else float(value)


def change_series(values: Sequence[float], length: int = 1) -> list[float]:
    out: list[float] = []
    for index, value in enumerate(values):
        previous = value_at(values, index, length, fallback=float(value))
        out.append(float(value) - previous)
    return out


def roc_series(values: Sequence[float], length: int = 1) -> list[float]:
    out: list[float] = []
    for index, value in enumerate(values):
        previous = value_at(values, index, length, fallback=float(value))
        diff = float(value) - previous
        out.append(safe_div(diff, previous) * 100.0)
    return out


def barssince(flags: Sequence[bool]) -> list[int | None]:
    out: list[int | None] = []
    last_true: int | None = None
    for index, flag in enumerate(flags):
        if flag:
            last_true = index
            out.append(0)
        elif last_true is None:
            out.append(None)
        else:
            out.append(index - last_true)
    return out


def valuewhen(flags: Sequence[bool], values: Sequence[T], occurrence: int = 0) -> list[T | None]:
    out: list[T | None] = []
    seen: list[T] = []
    for flag, value in zip(flags, values, strict=False):
        if flag:
            seen.insert(0, value)
        out.append(seen[occurrence] if occurrence < len(seen) else None)
    return out


def crossover(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    out = [False] * min(len(a), len(b))
    for index in range(1, len(out)):
        out[index] = float(a[index - 1]) <= float(b[index - 1]) and float(a[index]) > float(
            b[index]
        )
    return out


def crossunder(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    out = [False] * min(len(a), len(b))
    for index in range(1, len(out)):
        out[index] = float(a[index - 1]) >= float(b[index - 1]) and float(a[index]) < float(
            b[index]
        )
    return out


def cross(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    over = crossover(a, b)
    under = crossunder(a, b)
    return [bool(o or u) for o, u in zip(over, under, strict=False)]


def sma_series(values: Sequence[float], length: int) -> list[float]:
    return rolling_mean_series(values, length)


def ema_series(values: Sequence[float], length: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (max(length, 1) + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(float(value) * alpha + out[-1] * (1.0 - alpha))
    return out


def rma_series(values: Sequence[float], length: int) -> list[float]:
    if not values:
        return []
    alpha = 1.0 / max(length, 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(out[-1] * (1.0 - alpha) + float(value) * alpha)
    return out


def rsi_series(values: Sequence[float], length: int) -> list[float]:
    if not values:
        return []
    window = max(int(length), 1)
    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for previous, current in zip(values, values[1:], strict=False):
        delta = float(current) - float(previous)
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    average_gains = rma_series(gains, window)
    average_losses = rma_series(losses, window)
    out: list[float] = []
    for gain, loss in zip(average_gains, average_losses, strict=False):
        if loss <= 1e-12:
            out.append(100.0 if gain > 0 else 50.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + gain / loss))
    return out


def relative_volume_series(bars: Sequence[Bar], length: int, fallback: float = 1.0) -> list[float]:
    volumes = [bar.volume for bar in bars]
    baselines = rolling_mean_series(volumes, length)
    return [
        safe_div(volume, baseline, fallback) if baseline > 0 else fallback
        for volume, baseline in zip(volumes, baselines, strict=False)
    ]


def true_range_series(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1] if index else bar
        out.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous.close),
                abs(bar.low - previous.close),
                0.000001,
            )
        )
    return out


def atr_sma_series(bars: Sequence[Bar], length: int) -> list[float]:
    return [max(value, 0.000001) for value in sma_series(true_range_series(bars), length)]


def atr_rma_series(bars: Sequence[Bar], length: int) -> list[float]:
    return [max(value, 0.000001) for value in rma_series(true_range_series(bars), length)]


def dmi_series(
    bars: Sequence[Bar], length: int = 14, smooth: int | None = None
) -> tuple[list[float], list[float], list[float]]:
    """Pine-like DMI/ADX using Wilder smoothing.

    Returns plus DI, minus DI and ADX arrays. This is intentionally small and
    dependency-free so indicator ports can share one implementation.
    """

    smooth_len = smooth or length
    plus_dm = [0.0]
    minus_dm = [0.0]
    tr_values = true_range_series(bars)
    for index in range(1, len(bars)):
        up = bars[index].high - bars[index - 1].high
        down = bars[index - 1].low - bars[index].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr_rma = rma_series(tr_values, length)
    plus_rma = rma_series(plus_dm, length)
    minus_rma = rma_series(minus_dm, length)
    plus_di: list[float] = []
    minus_di: list[float] = []
    dx: list[float] = []
    for tr, plus, minus in zip(tr_rma, plus_rma, minus_rma, strict=False):
        pdi = safe_div(plus, tr) * 100.0 if tr else 0.0
        mdi = safe_div(minus, tr) * 100.0 if tr else 0.0
        plus_di.append(pdi)
        minus_di.append(mdi)
        dx.append(safe_div(abs(pdi - mdi), pdi + mdi) * 100.0 if pdi + mdi else 0.0)
    adx = rma_series(dx, smooth_len)
    return plus_di, minus_di, adx


def rolling_high(
    values: Sequence[float], index: int, length: int, exclude_current: bool = True
) -> float | None:
    end = index if exclude_current else index + 1
    start = max(0, end - max(length, 1))
    if end <= start:
        return None
    return max(values[start:end])


def rolling_low(
    values: Sequence[float], index: int, length: int, exclude_current: bool = True
) -> float | None:
    end = index if exclude_current else index + 1
    start = max(0, end - max(length, 1))
    if end <= start:
        return None
    return min(values[start:end])


def pivot_high(values: Sequence[float], index: int, left: int, right: int) -> bool:
    if index - left < 0 or index + right >= len(values):
        return False
    current = values[index]
    return all(current >= values[i] for i in range(index - left, index + right + 1) if i != index)


def pivot_low(values: Sequence[float], index: int, left: int, right: int) -> bool:
    if index - left < 0 or index + right >= len(values):
        return False
    current = values[index]
    return all(current <= values[i] for i in range(index - left, index + right + 1) if i != index)


@dataclass(frozen=True)
class PivotPoint:
    index: int
    ts: datetime
    price: float
    type: int
    label: str | None = None


def pivot_points(
    bars: Sequence[Bar],
    left: int = 3,
    right: int | None = None,
    max_points: int | None = None,
) -> list[PivotPoint]:
    right_len = right if right is not None else left
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    points: list[PivotPoint] = []
    for index, bar in enumerate(bars):
        if pivot_high(highs, index, left, right_len):
            points.append(PivotPoint(index=index, ts=bar.ts, price=bar.high, type=1))
        if pivot_low(lows, index, left, right_len):
            points.append(PivotPoint(index=index, ts=bar.ts, price=bar.low, type=-1))
    points.sort(key=lambda item: item.index)
    if max_points is not None and len(points) > max_points:
        return points[-max_points:]
    return points


def line_price(x1: int, y1: float, x2: int, y2: float, x: int) -> float:
    span = max(float(x2 - x1), 1.0)
    return float(y1) + (float(y2) - float(y1)) * ((float(x) - float(x1)) / span)


def vwap_series(
    bars: Sequence[Bar],
    reset: Callable[[Bar], str],
) -> list[float]:
    out: list[float] = []
    current_key: str | None = None
    pv_sum = 0.0
    volume_sum = 0.0
    for bar in bars:
        key = reset(bar)
        if key != current_key:
            current_key = key
            pv_sum = 0.0
            volume_sum = 0.0
        volume = bar.volume
        if volume > 0:
            typical = (bar.high + bar.low + bar.close) / 3.0
            pv_sum += typical * volume
            volume_sum += volume
        out.append(safe_div(pv_sum, volume_sum, bar.close) if volume_sum > 0 else bar.close)
    return out


def _collapse_expected_child_count(source_interval: str, target_interval: str) -> int:
    source_minutes = max(interval_minutes(source_interval), 1)
    target_minutes = max(interval_minutes(target_interval), 1)
    if source_minutes >= target_minutes:
        return 1
    return max(target_minutes // source_minutes, 1)


def collapse_bars(
    bars: Sequence[Bar], interval: str, symbol: str | None = None, source_name: str | None = None
) -> list[Bar]:
    grouped: dict[datetime, Bar] = {}
    child_slots: dict[datetime, set[datetime]] = {}
    child_closed: dict[datetime, bool] = {}
    target_minutes = max(interval_minutes(interval), 1)
    for bar in sorted(bars, key=lambda item: item.ts):
        source_minutes = max(interval_minutes(bar.timeframe), 1)
        same_interval = source_minutes == target_minutes
        # Confirmed same-timeframe bars already carry their authoritative bucket
        # phase. Re-bucketing a 60m bar from 09:30 to 09:00 would corrupt that
        # interval even though no resampling is required.
        bucket = bar.ts if same_interval else interval_bucket(bar.ts, interval)
        existing = grouped.get(bucket)
        child_slot = bar.ts if same_interval else interval_bucket(bar.ts, bar.timeframe)
        slots = child_slots.setdefault(bucket, set())
        slots.add(child_slot)
        child_closed[bucket] = child_closed.get(bucket, True) and bool(bar.closed)
        expected_children = _collapse_expected_child_count(bar.timeframe, interval)
        bucket_complete = len(slots) >= expected_children
        bucket_closed = (
            child_closed[bucket]
            if same_interval
            else child_closed[bucket] and bucket_complete and bar_is_closed(bucket, interval)
        )
        output_provenance = bar.provenance
        if not same_interval and bar.provenance is not None:
            output_provenance = (
                None
                if (
                    source_minutes > target_minutes
                    or bar.provenance.request_type is BarProviderRequest.CANONICAL_STORAGE
                )
                else BarProvenance(
                    provider=bar.provenance.provider,
                    instrument_id=bar.provenance.instrument_id,
                    route_fingerprint=bar.provenance.route_fingerprint,
                    request_type=BarProviderRequest.DETERMINISTIC_AGGREGATION,
                    provider_contract_id=bar.provenance.provider_contract_id,
                    provider_contract_type=(bar.provenance.provider_contract_type),
                    data_type=bar.provenance.data_type,
                    source_timeframe=bar.timeframe,
                )
            )
        if existing is None:
            grouped[bucket] = Bar(
                symbol=symbol or bar.symbol,
                ts=bucket,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=max(bar.volume, 0.0),
                timeframe=interval,
                source=source_name or bar.source,
                closed=bucket_closed,
                provenance=output_provenance,
            )
        else:
            grouped[bucket] = Bar(
                symbol=symbol or existing.symbol,
                ts=bucket,
                open=existing.open,
                high=max(existing.high, bar.high),
                low=min(existing.low, bar.low),
                close=bar.close,
                volume=max(existing.volume, 0.0) + max(bar.volume, 0.0),
                timeframe=interval,
                source=source_name or existing.source,
                closed=bucket_closed,
                provenance=(
                    existing.provenance if existing.provenance == output_provenance else None
                ),
            )
    return [grouped[key] for key in sorted(grouped)]


@dataclass(frozen=True)
class PineContext:
    """Small Pine-style data context with explicit confirmed/live separation."""

    bars: tuple[Bar, ...]
    confirmed_bars: tuple[Bar, ...]
    live_bar: Bar | None

    @classmethod
    def from_bars(cls, bars: Sequence[Bar]) -> "PineContext":
        normalized = tuple(bars)
        confirmed, live_bar = split_confirmed_bars(normalized)
        return cls(tuple(normalized), tuple(confirmed), live_bar)

    @property
    def latest(self) -> Bar:
        return self.bars[-1]

    @property
    def analysis_bar(self) -> Bar:
        return self.confirmed_bars[-1]

    @property
    def has_live_bar(self) -> bool:
        return self.live_bar is not None

    def security_bars(self, interval: str, *, confirmed_only: bool = True) -> list[Bar]:
        source_bars: Sequence[Bar] = self.confirmed_bars if confirmed_only else self.bars
        return collapse_bars(
            source_bars, interval, symbol=self.latest.symbol, source_name=f"security:{interval}"
        )

    def map_by_timestamp(self, values: Sequence[T], target_bars: Sequence[Bar]) -> list[T | None]:
        by_ts = {bar.ts: value for bar, value in zip(self.bars, values, strict=False)}
        return [by_ts.get(bar.ts) for bar in target_bars]


def log_volume_series(bars: Sequence[Bar]) -> list[float]:
    return [log(max(bar.volume, 1.0)) for bar in bars]
