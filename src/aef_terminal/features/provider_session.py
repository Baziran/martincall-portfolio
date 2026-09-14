from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.runtime.timeframes import interval_minutes


@dataclass(frozen=True)
class ProviderSessionInterval:
    opens_at: datetime
    closes_at: datetime
    session_date: date | None = None

    @property
    def session_key(self) -> str | None:
        return self.session_date.isoformat() if self.session_date is not None else None

    @property
    def key(self) -> str:
        if self.session_key is None:
            raise ValueError("Provider session date is unavailable")
        return self.session_key

    def contains(self, ts: datetime) -> bool:
        return self.opens_at <= ts.astimezone(UTC) < self.closes_at


@dataclass(frozen=True)
class ProviderSessionReset:
    available: bool
    source: str
    reason_code: str
    calendar: str
    intervals: tuple[ProviderSessionInterval, ...] = ()

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source,
            "reason_code": self.reason_code,
            "calendar": self.calendar,
            "intervals": [
                {
                    "opens_at": interval.opens_at.isoformat(),
                    "closes_at": interval.closes_at.isoformat(),
                    "session_date": (
                        interval.session_date.isoformat()
                        if interval.session_date is not None
                        else None
                    ),
                    "session_key": interval.session_key,
                }
                for interval in self.intervals
            ],
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source,
            "reason_code": self.reason_code,
            "calendar": self.calendar,
        }

    def key_for_bar(self, bar: Bar) -> str:
        if not self.available:
            raise ValueError(f"VWAP session reset is unavailable: {self.reason_code}")
        if self.calendar == "continuous_24_7":
            return f"UTC:{bar.ts.astimezone(UTC).date().isoformat()}"
        matching = [interval for interval in self.intervals if interval.contains(bar.ts)]
        for interval in matching:
            if interval.session_key is not None:
                return interval.session_key
        if matching:
            raise ValueError(f"Provider session date is unavailable for bar {bar.ts.isoformat()}")
        raise ValueError(f"Bar {bar.ts.isoformat()} is outside the provider trading schedule")

    def interval_for_timestamp(self, ts: datetime) -> ProviderSessionInterval | None:
        """Return the exact provider interval containing ``ts`` without guessing."""

        if not self.available or self.calendar == "continuous_24_7":
            return None
        timestamp = ts.astimezone(UTC)
        matches = [interval for interval in self.intervals if interval.contains(timestamp)]
        if not matches:
            return None
        session_keys = {interval.session_key for interval in matches}
        if None in session_keys or len(session_keys) != 1:
            raise ValueError("provider_session_intervals_ambiguous")
        return min(matches, key=lambda interval: interval.opens_at)


def provider_session_active_minute(
    session: ProviderSessionReset,
    ts: datetime,
) -> int:
    """Return the exact active-minute coordinate within one provider session.

    Closed gaps between intervals carrying the same provider session date do not
    advance the coordinate. Continuous providers use their typed UTC-day reset.
    """

    if not session.available:
        raise ValueError(f"Provider session is unavailable: {session.reason_code}")
    timestamp = ts.astimezone(UTC)
    if session.calendar == "continuous_24_7":
        opens_at = datetime.combine(timestamp.date(), datetime.min.time(), tzinfo=UTC)
        elapsed = timestamp - opens_at
    else:
        interval = session.interval_for_timestamp(timestamp)
        if interval is None or interval.session_key is None:
            raise ValueError(
                f"Bar {timestamp.isoformat()} is outside the provider trading schedule"
            )
        session_intervals = sorted(
            (
                candidate
                for candidate in session.intervals
                if candidate.session_key == interval.session_key
            ),
            key=lambda candidate: candidate.opens_at,
        )
        merged: list[ProviderSessionInterval] = []
        for candidate in session_intervals:
            previous = merged[-1] if merged else None
            if previous is not None and candidate.opens_at <= previous.closes_at:
                merged[-1] = ProviderSessionInterval(
                    opens_at=previous.opens_at,
                    closes_at=max(previous.closes_at, candidate.closes_at),
                    session_date=previous.session_date,
                )
            else:
                merged.append(candidate)
        elapsed = timedelta(0)
        matched = False
        for candidate in merged:
            if candidate.contains(timestamp):
                elapsed += timestamp - candidate.opens_at
                matched = True
                break
            if candidate.closes_at <= timestamp:
                elapsed += candidate.closes_at - candidate.opens_at
        if not matched:
            raise ValueError(
                f"Bar {timestamp.isoformat()} is outside the provider trading schedule"
            )
    elapsed_seconds = elapsed.total_seconds()
    if elapsed_seconds < 0 or elapsed_seconds % 60 != 0:
        raise ValueError("provider_session_bar_not_minute_aligned")
    return int(elapsed_seconds // 60)


def provider_session_closing_window(
    session: ProviderSessionReset | None,
    ts: datetime,
    *,
    minutes: int,
) -> bool:
    """Return a provider-backed closing-window fact, never a calendar heuristic."""

    if session is None or not session.available or minutes <= 0:
        return False
    try:
        interval = session.interval_for_timestamp(ts)
    except ValueError:
        return False
    if interval is None:
        return False
    timestamp = ts.astimezone(UTC)
    return interval.closes_at - timedelta(minutes=minutes) <= timestamp < interval.closes_at


def provider_session_bounds_for_bar(
    session: ProviderSessionReset | None,
    bar: Bar,
) -> tuple[datetime, datetime] | None:
    """Resolve exact current session bounds, including typed continuous UTC days."""

    if session is None or not session.available:
        return None
    if session.calendar == "continuous_24_7":
        opens_at = datetime.combine(
            bar.ts.astimezone(UTC).date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        return opens_at, opens_at + timedelta(days=1)
    try:
        interval = session.interval_for_timestamp(bar.ts)
    except ValueError:
        return None
    if interval is None:
        return None
    return interval.opens_at, interval.closes_at


def provider_session_intervals(
    instrument: Mapping[str, Any] | None,
    key: str = "trading_intervals",
) -> tuple[ProviderSessionInterval, ...]:
    session = instrument.get("session") if isinstance(instrument, Mapping) else None
    rows = session.get(key) if isinstance(session, Mapping) else None
    return provider_session_intervals_from_rows(
        rows if isinstance(rows, list) else (),
    )


def provider_session_intervals_from_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[ProviderSessionInterval, ...]:
    """Parse exact provider-owned open session rows into typed intervals."""

    intervals: list[ProviderSessionInterval] = []
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("status") or "").strip().lower() != "open":
            continue
        try:
            opens_at = datetime.fromisoformat(str(row.get("opens_at") or "").replace("Z", "+00:00"))
            closes_at = datetime.fromisoformat(
                str(row.get("closes_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if opens_at.tzinfo is None or closes_at.tzinfo is None or closes_at <= opens_at:
            continue
        session_date: date | None = None
        raw_session_date = row.get("session_date")
        if isinstance(raw_session_date, str):
            try:
                parsed_session_date = date.fromisoformat(raw_session_date)
            except ValueError:
                parsed_session_date = None
            if (
                parsed_session_date is not None
                and parsed_session_date.isoformat() == raw_session_date
            ):
                session_date = parsed_session_date
        intervals.append(
            ProviderSessionInterval(
                opens_at=opens_at.astimezone(UTC),
                closes_at=closes_at.astimezone(UTC),
                session_date=session_date,
            )
        )
    return tuple(sorted(intervals, key=lambda interval: interval.opens_at))


def provider_session_bar_indexes(
    intervals: Sequence[ProviderSessionInterval],
    bars: Sequence[Bar],
) -> dict[str, tuple[int, ...]]:
    """Group bars by exact provider session date without inferring calendar days."""

    ordered_intervals = tuple(sorted(intervals, key=lambda interval: interval.opens_at))
    if any(interval.session_key is None for interval in ordered_intervals):
        raise ValueError("provider_session_date_missing")
    for position, left in enumerate(ordered_intervals):
        for right in ordered_intervals[position + 1 :]:
            if right.opens_at >= left.closes_at:
                break
            if right.session_key != left.session_key:
                raise ValueError("provider_session_intervals_ambiguous")

    merged_intervals: list[ProviderSessionInterval] = []
    for interval in ordered_intervals:
        previous = merged_intervals[-1] if merged_intervals else None
        if (
            previous is not None
            and previous.session_key == interval.session_key
            and interval.opens_at <= previous.closes_at
        ):
            merged_intervals[-1] = ProviderSessionInterval(
                opens_at=previous.opens_at,
                closes_at=max(previous.closes_at, interval.closes_at),
                session_date=previous.session_date,
            )
        else:
            merged_intervals.append(interval)

    indexed_bars = sorted(
        ((bar.ts.astimezone(UTC), index) for index, bar in enumerate(bars)),
        key=lambda item: item[0],
    )
    bar_times = [item[0] for item in indexed_bars]
    grouped: dict[str, set[int]] = {}
    for interval in merged_intervals:
        session_key = interval.key
        start = bisect_left(bar_times, interval.opens_at)
        end = bisect_left(bar_times, interval.closes_at, lo=start)
        grouped.setdefault(session_key, set()).update(
            indexed_bars[position][1] for position in range(start, end)
        )
    return {
        session_key: tuple(sorted(indexes, key=lambda index: bars[index].ts))
        for session_key, indexes in grouped.items()
        if indexes
    }


def previous_provider_session_levels(
    bars: Sequence[Bar],
    session: ProviderSessionReset,
) -> tuple[list[float | None], list[float | None]]:
    """Return prior observed provider-session highs/lows for each bar."""

    if not session.available:
        raise ValueError(f"Provider session levels are unavailable: {session.reason_code}")
    previous_highs: list[float | None] = []
    previous_lows: list[float | None] = []
    active_key: str | None = None
    active_high: float | None = None
    active_low: float | None = None
    previous_high: float | None = None
    previous_low: float | None = None
    for bar in bars:
        session_key = session.key_for_bar(bar)
        if active_key is None:
            active_key = session_key
        elif session_key != active_key:
            previous_high = active_high
            previous_low = active_low
            active_key = session_key
            active_high = None
            active_low = None
        previous_highs.append(previous_high)
        previous_lows.append(previous_low)
        active_high = float(bar.high) if active_high is None else max(active_high, float(bar.high))
        active_low = float(bar.low) if active_low is None else min(active_low, float(bar.low))
    return previous_highs, previous_lows


@dataclass(frozen=True)
class ProviderSessionLevelContext:
    available: bool
    source: str
    reason_code: str
    session_key: str | None = None
    opening_range_high: float | None = None
    opening_range_low: float | None = None
    opening_range_complete: bool = False
    initial_balance_high: float | None = None
    initial_balance_low: float | None = None
    initial_balance_complete: bool = False
    previous_session_high: float | None = None
    previous_session_low: float | None = None
    current_session_high: float | None = None
    current_session_low: float | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source": self.source,
            "reason_code": self.reason_code,
            "session_key": self.session_key,
            "or_high": self.opening_range_high,
            "or_low": self.opening_range_low,
            "or_complete": self.opening_range_complete,
            "ib_high": self.initial_balance_high,
            "ib_low": self.initial_balance_low,
            "ib_complete": self.initial_balance_complete,
            "previous_session_high": self.previous_session_high,
            "previous_session_low": self.previous_session_low,
            "current_session_high": self.current_session_high,
            "current_session_low": self.current_session_low,
        }


def provider_session_level_context(
    bars: Sequence[Bar],
    session: ProviderSessionReset | None,
) -> ProviderSessionLevelContext:
    """Build observed session levels from the canonical provider reset."""

    if session is None or not session.available:
        return ProviderSessionLevelContext(
            available=False,
            source=(session.source if session is not None else "provider_session"),
            reason_code=(
                session.reason_code if session is not None else "provider_session_missing"
            ),
        )
    if not bars:
        return ProviderSessionLevelContext(
            available=False,
            source=session.source,
            reason_code="provider_session_bars_missing",
        )
    grouped: dict[str, list[Bar]] = {}
    ordered_keys: list[str] = []
    try:
        for bar in bars:
            key = session.key_for_bar(bar)
            if key not in grouped:
                grouped[key] = []
                ordered_keys.append(key)
            grouped[key].append(bar)
        current_key = session.key_for_bar(bars[-1])
    except ValueError as exc:
        return ProviderSessionLevelContext(
            available=False,
            source=session.source,
            reason_code=str(exc),
        )
    current_bars = grouped.get(current_key, [])
    if not current_bars:
        return ProviderSessionLevelContext(
            available=False,
            source=session.source,
            reason_code="provider_current_session_bars_missing",
        )
    if session.calendar == "continuous_24_7":
        current_open = datetime.combine(
            bars[-1].ts.astimezone(UTC).date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
    else:
        current_interval = next(
            (
                interval
                for interval in session.intervals
                if interval.key == current_key and interval.contains(bars[-1].ts)
            ),
            None,
        )
        if current_interval is None:
            return ProviderSessionLevelContext(
                available=False,
                source=session.source,
                reason_code="provider_current_session_interval_missing",
            )
        current_open = current_interval.opens_at

    def completed_window(minutes: int) -> tuple[list[Bar], bool]:
        cutoff = current_open + timedelta(minutes=minutes)
        rows = [
            bar
            for bar in current_bars
            if current_open <= bar.ts.astimezone(UTC)
            and bar.ts.astimezone(UTC) + timedelta(minutes=max(interval_minutes(bar.timeframe), 1))
            <= cutoff
        ]
        last_close = max(
            (
                bar.ts.astimezone(UTC) + timedelta(minutes=max(interval_minutes(bar.timeframe), 1))
                for bar in current_bars
            ),
            default=current_open,
        )
        return rows, last_close >= cutoff

    opening_rows, opening_complete = completed_window(30)
    balance_rows, balance_complete = completed_window(60)
    previous_bars: list[Bar] = []
    current_position = ordered_keys.index(current_key)
    if current_position > 0:
        previous_bars = grouped[ordered_keys[current_position - 1]]
    return ProviderSessionLevelContext(
        available=True,
        source=session.source,
        reason_code="",
        session_key=current_key,
        opening_range_high=(
            max(bar.high for bar in opening_rows) if opening_complete and opening_rows else None
        ),
        opening_range_low=(
            min(bar.low for bar in opening_rows) if opening_complete and opening_rows else None
        ),
        opening_range_complete=opening_complete,
        initial_balance_high=(
            max(bar.high for bar in balance_rows) if balance_complete and balance_rows else None
        ),
        initial_balance_low=(
            min(bar.low for bar in balance_rows) if balance_complete and balance_rows else None
        ),
        initial_balance_complete=balance_complete,
        previous_session_high=(max(bar.high for bar in previous_bars) if previous_bars else None),
        previous_session_low=(min(bar.low for bar in previous_bars) if previous_bars else None),
        current_session_high=max(bar.high for bar in current_bars),
        current_session_low=min(bar.low for bar in current_bars),
    )


def provider_vwap_session(
    instrument: Mapping[str, Any] | None,
    bars: Sequence[Bar],
) -> ProviderSessionReset:
    session = instrument.get("session") if isinstance(instrument, Mapping) else None
    if not isinstance(session, Mapping):
        return ProviderSessionReset(
            available=False,
            source="provider_session",
            reason_code="provider_session_missing",
            calendar="unknown",
        )
    calendar = str(session.get("calendar") or "unknown").strip().lower()
    if calendar == "continuous_24_7":
        return ProviderSessionReset(
            available=True,
            source="provider_continuous_session",
            reason_code="",
            calendar=calendar,
        )
    intervals = provider_session_intervals(instrument)
    if not intervals:
        return ProviderSessionReset(
            available=False,
            source="provider_session",
            reason_code="provider_trading_intervals_missing",
            calendar=calendar or "unknown",
        )
    try:
        grouped_bar_indexes = provider_session_bar_indexes(intervals, bars)
    except ValueError as exc:
        return ProviderSessionReset(
            available=False,
            source="provider_session",
            reason_code=str(exc),
            calendar=calendar or "unknown",
            intervals=intervals,
        )
    covered_indexes = {index for indexes in grouped_bar_indexes.values() for index in indexes}
    if len(covered_indexes) != len(bars):
        return ProviderSessionReset(
            available=False,
            source="provider_session",
            reason_code="bar_outside_provider_trading_intervals",
            calendar=calendar or "unknown",
            intervals=intervals,
        )
    return ProviderSessionReset(
        available=True,
        source="provider_trading_intervals",
        reason_code="",
        calendar=calendar or "unknown",
        intervals=intervals,
    )
