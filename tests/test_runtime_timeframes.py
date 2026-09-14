from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.runtime.timeframes import (
    MAX_CHART_HISTORY_CALENDAR_SLOTS,
    ChartHistoryRangeError,
    HistoryRangeWindow,
    chart_history_range_window,
    canonical_bar_source_interval,
    history_lookback_window,
    interval_seconds,
    require_persisted_bar_timeframe,
)


@pytest.mark.parametrize(
    ("interval", "range_key", "expected_days"),
    (
        ("1m", "31d", 32),
        ("3m", "3mo", 96),
        ("5m", "6mo", 192),
        ("15m", "1y", 370),
        ("60m", "5y", 1_850),
    ),
)
def test_chart_history_range_caps_preserve_full_half_open_window(
    interval: str,
    range_key: str,
    expected_days: int,
) -> None:
    now = datetime(2026, 7, 19, 12, 34, 56, tzinfo=UTC)

    window = chart_history_range_window(range_key, interval, now=now)

    assert window.starts_at is not None
    assert window.ends_at - window.starts_at == timedelta(days=expected_days)
    slots = int((window.ends_at - window.starts_at).total_seconds() // interval_seconds(interval))
    assert slots <= MAX_CHART_HISTORY_CALENDAR_SLOTS


@pytest.mark.parametrize(
    ("interval", "range_key", "code"),
    (
        ("1m", "2mo", "CHART_HISTORY_RANGE_TOO_LARGE"),
        ("5m", "5y", "CHART_HISTORY_RANGE_TOO_LARGE"),
        ("1m", "all", "CHART_HISTORY_RANGE_UNBOUNDED"),
        ("30m", "31d", "CHART_HISTORY_INTERVAL_UNSUPPORTED"),
        ("5m", "legacy", "CHART_HISTORY_RANGE_INVALID"),
    ),
)
def test_chart_history_range_rejects_unsafe_geometry(
    interval: str,
    range_key: str,
    code: str,
) -> None:
    with pytest.raises(ChartHistoryRangeError) as raised:
        chart_history_range_window(range_key, interval)

    assert raised.value.code == code


def test_chart_history_range_reuses_and_validates_frozen_window_identity() -> None:
    supplied = HistoryRangeWindow(
        "1d",
        datetime(2026, 7, 17, tzinfo=UTC),
        datetime(2026, 7, 19, tzinfo=UTC),
    )

    assert chart_history_range_window("1d", "5m", window=supplied) is supplied

    oversized = HistoryRangeWindow(
        "1d",
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 7, 19, tzinfo=UTC),
    )
    with pytest.raises(ChartHistoryRangeError) as raised:
        chart_history_range_window("1d", "1m", window=oversized)

    assert raised.value.code == "CHART_HISTORY_RANGE_TOO_LARGE"


@pytest.mark.parametrize(
    ("interval", "expected_end"),
    (
        ("1m", datetime(2026, 7, 19, 12, 34, tzinfo=UTC)),
        ("3m", datetime(2026, 7, 19, 12, 33, tzinfo=UTC)),
        ("5m", datetime(2026, 7, 19, 12, 30, tzinfo=UTC)),
        ("15m", datetime(2026, 7, 19, 12, 30, tzinfo=UTC)),
        ("60m", datetime(2026, 7, 19, 12, 0, tzinfo=UTC)),
    ),
)
def test_history_lookback_window_is_exact_and_bucket_aligned(
    interval: str,
    expected_end: datetime,
) -> None:
    now = datetime(2026, 7, 19, 12, 34, 56, tzinfo=UTC)

    window = history_lookback_window(31, interval, now=now)

    assert window.range_key == "31d"
    assert window.ends_at == expected_end
    assert window.starts_at == expected_end - timedelta(days=31)
    assert window.ends_at - window.starts_at == timedelta(days=31)


@pytest.mark.parametrize("days", (0, -1, True))
def test_history_lookback_window_requires_positive_integer_days(days: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        history_lookback_window(days, "5m")


def test_three_minute_chart_interval_has_one_minute_canonical_source() -> None:
    assert canonical_bar_source_interval("3m") == "1m"
    assert canonical_bar_source_interval("5m") == "5m"


def test_virtual_three_minute_interval_is_forbidden_at_storage_boundary() -> None:
    assert require_persisted_bar_timeframe("1m") == "1m"
    with pytest.raises(
        ValueError,
        match="CANONICAL_BAR_STORAGE_TIMEFRAME_UNSUPPORTED timeframe=3m",
    ):
        require_persisted_bar_timeframe("3m")


@pytest.mark.parametrize("legacy_alias", ("1h", "60"))
def test_legacy_hour_aliases_are_forbidden_at_storage_boundary(legacy_alias: str) -> None:
    with pytest.raises(
        ValueError,
        match=rf"CANONICAL_BAR_STORAGE_TIMEFRAME_UNSUPPORTED timeframe={legacy_alias}",
    ):
        require_persisted_bar_timeframe(legacy_alias)
