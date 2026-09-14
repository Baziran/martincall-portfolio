from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


CHART_HISTORY_INTERVALS = ("1m", "3m", "5m", "15m", "60m")
PERSISTED_BAR_INTERVALS = ("1m", "5m", "15m", "60m")
DERIVED_CHART_INTERVAL_SOURCES = {"3m": "1m"}
MAX_CHART_HISTORY_CALENDAR_SLOTS = 60_000
_CHART_HISTORY_RANGE_PATTERN = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>mo|d|y)$")


@dataclass(frozen=True)
class HistoryRangeWindow:
    """One immutable half-open provider-bar window shared by one request."""

    range_key: str
    starts_at: datetime | None
    ends_at: datetime


class ChartHistoryRangeError(ValueError):
    """Typed, non-retryable rejection for an unsafe interactive history request."""

    def __init__(
        self,
        code: str,
        *,
        interval: str,
        range_key: str,
        calendar_slots: int | None = None,
    ) -> None:
        self.code = str(code)
        self.interval = str(interval)
        self.range_key = str(range_key)
        self.calendar_slots = calendar_slots
        detail = (
            f" calendar_slots={calendar_slots} max_slots={MAX_CHART_HISTORY_CALENDAR_SLOTS}"
            if calendar_slots is not None
            else ""
        )
        super().__init__(f"{self.code} interval={self.interval} range={self.range_key}{detail}")


def interval_minutes(interval: str) -> int:
    normalized = str(interval or "").lower().strip()
    try:
        if normalized.endswith("m"):
            return max(int(normalized[:-1] or 1), 1)
        if normalized == "60m":
            return 60
    except ValueError:
        return 5
    return 5


def interval_seconds(interval: str) -> int:
    return interval_minutes(interval) * 60


def timestamp_is_bucket_aligned(value: datetime, step_seconds: int) -> bool:
    """Return whether a timestamp lies exactly on one epoch bucket boundary."""

    return value.microsecond == 0 and int(value.timestamp()) % step_seconds == 0


def canonical_bar_source_interval(interval: str) -> str:
    """Return the persisted/provider interval that owns one chart interval."""

    normalized = str(interval or "").strip().lower()
    if normalized not in CHART_HISTORY_INTERVALS:
        raise ValueError(f"CHART_INTERVAL_UNSUPPORTED interval={normalized}")
    return DERIVED_CHART_INTERVAL_SOURCES.get(normalized, normalized)


def is_derived_chart_interval(interval: str) -> bool:
    normalized = str(interval or "").strip().lower()
    return normalized in DERIVED_CHART_INTERVAL_SOURCES


def require_persisted_bar_timeframe(timeframe: object) -> str:
    """Reject virtual/chart-only intervals at every canonical write boundary."""

    normalized = str(timeframe or "").strip().lower()
    if normalized not in PERSISTED_BAR_INTERVALS:
        raise ValueError(f"CANONICAL_BAR_STORAGE_TIMEFRAME_UNSUPPORTED timeframe={normalized}")
    return normalized


def parse_aware_utc_ts(value: Any) -> datetime | None:
    """Parse an explicitly timezone-qualified timestamp without inventing UTC."""

    if not value:
        return None
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def require_aware_utc_datetime(value: object, *, field: str) -> datetime:
    """Return one aware UTC datetime or reject an untyped/naive boundary value."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def adapt_bars(base: int, timeframe: str, source_minutes: int = 5, *, min_bars: int = 1) -> int:
    minutes = max(interval_minutes(timeframe), 1)
    if minutes <= 1:
        return max(min_bars, int(base) * max(source_minutes, 1))
    if minutes >= 60 and source_minutes == 5:
        return max(min_bars, round(int(base) / 2))
    return max(min_bars, round(int(base) * max(source_minutes, 1) / minutes))


def expiry_after_bars(ts: datetime, timeframe: str, bars_count: int) -> datetime:
    return ts + timedelta(minutes=max(1, int(bars_count)) * interval_minutes(timeframe))


def interval_bucket(ts: datetime, interval: str) -> datetime:
    seconds = interval_seconds(interval)
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def interval_is_closed(ts: datetime, interval: str, now: datetime | None = None) -> bool:
    current = now or datetime.now(tz=UTC)
    bucket = interval_bucket(ts.astimezone(UTC), interval)
    return current >= bucket + timedelta(seconds=interval_seconds(interval))


def range_start_utc(range_key: str, now: datetime | None = None) -> datetime | None:
    current = now or datetime.now(tz=UTC)
    normalized = range_key.strip().lower()
    if normalized == "all":
        return None
    if normalized.endswith("mo"):
        return current - timedelta(days=max(int(normalized[:-2]), 1) * 32)
    if normalized.endswith("d"):
        return current - timedelta(days=max(int(normalized[:-1]), 1) + 1)
    if normalized.endswith("y"):
        return current - timedelta(days=max(int(normalized[:-1]), 1) * 370)
    return None


def history_range_window(
    range_key: str,
    interval: str,
    *,
    now: datetime | None = None,
) -> HistoryRangeWindow:
    """Freeze one bucket-aligned ``[starts_at, ends_at)`` window for one request."""

    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    ends_at = interval_bucket(current, interval)
    raw_start = range_start_utc(range_key, now=current)
    starts_at = interval_bucket(raw_start, interval) if raw_start is not None else None
    return HistoryRangeWindow(
        range_key=str(range_key),
        starts_at=starts_at,
        ends_at=ends_at,
    )


def history_lookback_window(
    days: int,
    interval: str,
    *,
    now: datetime | None = None,
) -> HistoryRangeWindow:
    """Freeze one exact bucket-aligned ``[end - days, end)`` lookback."""

    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("history lookback days must be a positive integer")
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    ends_at = interval_bucket(current, interval)
    starts_at = ends_at - timedelta(days=days)
    if interval_bucket(starts_at, interval) != starts_at:
        raise ValueError("history lookback duration must align to the interval grid")
    return HistoryRangeWindow(
        range_key=f"{days}d",
        starts_at=starts_at,
        ends_at=ends_at,
    )


def chart_history_range_window(
    range_key: str,
    interval: str,
    *,
    now: datetime | None = None,
    window: HistoryRangeWindow | None = None,
) -> HistoryRangeWindow:
    """Validate and freeze one bounded interactive chart-history window.

    The limit is deliberately geometric: it bounds database work before any
    provider calendar/session filtering.  A caller-supplied window is validated
    and returned unchanged so every stage of one load shares the same half-open
    ``[starts_at, ends_at)`` boundary.
    """

    normalized_interval = str(interval or "").strip().lower()
    normalized_range = str(range_key or "").strip().lower()
    if normalized_interval not in CHART_HISTORY_INTERVALS:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_INTERVAL_UNSUPPORTED",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    if normalized_range == "all":
        raise ChartHistoryRangeError(
            "CHART_HISTORY_RANGE_UNBOUNDED",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    match = _CHART_HISTORY_RANGE_PATTERN.fullmatch(normalized_range)
    if match is None:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_RANGE_INVALID",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    count = int(match.group("count"))
    unit = match.group("unit")
    calendar_days = count + 1 if unit == "d" else count * 32 if unit == "mo" else count * 370
    calendar_slots = calendar_days * 86_400 // interval_seconds(normalized_interval)
    if calendar_slots > MAX_CHART_HISTORY_CALENDAR_SLOTS:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_RANGE_TOO_LARGE",
            interval=normalized_interval,
            range_key=normalized_range,
            calendar_slots=calendar_slots,
        )

    requested_window = window or history_range_window(
        normalized_range,
        normalized_interval,
        now=now,
    )
    if str(requested_window.range_key).strip().lower() != normalized_range:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_WINDOW_MISMATCH",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    if requested_window.starts_at is None:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_RANGE_UNBOUNDED",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    starts_at = requested_window.starts_at
    ends_at = requested_window.ends_at
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_WINDOW_INVALID",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    starts_at = starts_at.astimezone(UTC)
    ends_at = ends_at.astimezone(UTC)
    actual_seconds = (ends_at - starts_at).total_seconds()
    actual_slots = int(actual_seconds // interval_seconds(normalized_interval))
    if (
        actual_seconds <= 0
        or actual_seconds % interval_seconds(normalized_interval) != 0
        or interval_bucket(starts_at, normalized_interval) != starts_at
        or interval_bucket(ends_at, normalized_interval) != ends_at
    ):
        raise ChartHistoryRangeError(
            "CHART_HISTORY_WINDOW_INVALID",
            interval=normalized_interval,
            range_key=normalized_range,
        )
    if actual_slots > MAX_CHART_HISTORY_CALENDAR_SLOTS:
        raise ChartHistoryRangeError(
            "CHART_HISTORY_RANGE_TOO_LARGE",
            interval=normalized_interval,
            range_key=normalized_range,
            calendar_slots=actual_slots,
        )
    return requested_window
