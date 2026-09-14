from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data.adapters._tinvest import schedule
from aef_terminal.data.adapters._tinvest.schedule import (
    TInvestScheduleRequest,
    fetch_tinvest_schedule,
)


def _provider_interval(
    interval_type: str,
    starts_at: datetime,
    ends_at: datetime,
) -> SimpleNamespace:
    return SimpleNamespace(
        type=interval_type,
        interval=SimpleNamespace(start_ts=starts_at, end_ts=ends_at),
    )


def _trading_day(
    day: int,
    *,
    is_trading_day: bool,
    intervals: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        date=datetime(2026, 8, day, tzinfo=UTC),
        is_trading_day=is_trading_day,
        intervals=list(intervals or ()),
    )


def _regular_sessions(day: int) -> list[SimpleNamespace]:
    def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
        return datetime(2026, 8, day, hour, minute, second, tzinfo=UTC)

    return [
        _provider_interval("opening_auction", at(3, 50), at(3, 59, 59)),
        _provider_interval("regular_trading_session_morning", at(4), at(6, 59, 59)),
        _provider_interval("regular_trading_session", at(4), at(6, 59, 59)),
        _provider_interval("regular_trading_session_main", at(7), at(15, 59, 59)),
        _provider_interval("regular_trading_session", at(7), at(15, 59, 59)),
        _provider_interval("regular_trading_session_evening", at(16), at(20, 50)),
        _provider_interval("regular_trading_session", at(16), at(20, 50)),
        _provider_interval("clearing", at(20, 50, 1), at(21, 30)),
    ]


class _Instruments:
    def __init__(self, response: object, *, uid: str = "Exact-Uid") -> None:
        self.response = response
        self.uid = uid
        self.calls: list[object] = []

    async def get_instrument_by(self, request: object) -> object:
        self.calls.append(request)
        return SimpleNamespace(
            instrument=SimpleNamespace(
                uid=self.uid,
                exchange="forts_futures_weekend",
            )
        )

    async def trading_schedules(self, request: object) -> object:
        self.calls.append(request)
        return self.response


def _response(days: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        exchanges=[
            SimpleNamespace(
                exchange="FORTS_FUTURES_WEEKEND",
                days=days,
            )
        ]
    )


def _fetch(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
    *,
    starts_at: datetime,
    ends_at: datetime,
) -> tuple[dict[str, Any], _Instruments]:
    monkeypatch.setattr(schedule, "tinvest_uid_request", lambda uid: ("uid", uid))
    monkeypatch.setattr(
        schedule,
        "_trading_schedules_request",
        lambda exchange, *, starts_at, ends_at: (
            "schedule",
            exchange,
            starts_at,
            ends_at,
        ),
    )
    instruments = _Instruments(response)
    payload = asyncio.run(
        fetch_tinvest_schedule(
            SimpleNamespace(instruments=instruments),
            TInvestScheduleRequest("Exact-Uid", starts_at, ends_at),
            timeout=2.0,
        )
    )
    return payload, instruments


def test_exact_uid_schedule_materializes_merged_open_and_closed_complements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts_at = datetime(2026, 8, 13, 2, tzinfo=UTC)
    ends_at = datetime(2026, 8, 17, tzinfo=UTC)
    days = [
        _trading_day(13, is_trading_day=True, intervals=_regular_sessions(13)),
        _trading_day(14, is_trading_day=True, intervals=_regular_sessions(14)),
        _trading_day(15, is_trading_day=False),
        _trading_day(16, is_trading_day=False),
        # T-Invest currently returns the `to` date inclusively. It carries no
        # authority outside the exact requested half-open range.
        _trading_day(17, is_trading_day=True, intervals=_regular_sessions(17)),
    ]

    payload, instruments = _fetch(
        monkeypatch,
        _response(days),
        starts_at=starts_at,
        ends_at=ends_at,
    )

    assert instruments.calls == [
        ("uid", "Exact-Uid"),
        ("schedule", "forts_futures_weekend", starts_at, ends_at),
    ]
    assert payload["provider"] == "tinvest"
    assert payload["provider_contract_id"] == "Exact-Uid"
    assert payload["time_zone"] == "UTC"
    assert payload["source"] == "tinvest:TradingSchedules"
    assert payload["schedule_format"] == "provider_declared_intervals"
    assert payload["schedule_coverage_start"] == starts_at.isoformat()
    assert payload["schedule_coverage_end"] == ends_at.isoformat()
    assert payload["requested_exchange"] == "forts_futures_weekend"
    assert payload["returned_exchange"] == "FORTS_FUTURES_WEEKEND"

    intervals = payload["trading_intervals"]
    assert len(intervals) == 8
    first_day = [item for item in intervals if item["session_date"] == "2026-08-13"]
    assert [(item["status"], item["opens_at"], item["closes_at"]) for item in first_day] == [
        (
            "closed",
            "2026-08-13T02:00:00+00:00",
            "2026-08-13T04:00:00+00:00",
        ),
        (
            "open",
            "2026-08-13T04:00:00+00:00",
            "2026-08-13T20:50:00+00:00",
        ),
        (
            "closed",
            "2026-08-13T20:50:00+00:00",
            "2026-08-14T00:00:00+00:00",
        ),
    ]
    closed_days = [
        item for item in intervals if item["session_date"] in {"2026-08-15", "2026-08-16"}
    ]
    assert len(closed_days) == 2
    assert all(item["status"] == "closed" for item in closed_days)
    assert all(item["metadata"]["provider_closed_complement"] is True for item in closed_days)
    assert not any(item["session_date"] == "2026-08-17" for item in intervals)

    cursor = starts_at
    for item in intervals:
        assert datetime.fromisoformat(item["opens_at"]) == cursor
        cursor = datetime.fromisoformat(item["closes_at"])
    assert cursor == ends_at


def test_schedule_clips_one_regular_interval_to_the_exact_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts_at = datetime(2026, 8, 13, 5, tzinfo=UTC)
    ends_at = datetime(2026, 8, 13, 6, tzinfo=UTC)

    payload, _instruments = _fetch(
        monkeypatch,
        _response([_trading_day(13, is_trading_day=True, intervals=_regular_sessions(13))]),
        starts_at=starts_at,
        ends_at=ends_at,
    )

    assert payload["trading_intervals"] == [
        {
            "session_date": "2026-08-13",
            "session_type": "trading",
            "opens_at": starts_at.isoformat(),
            "closes_at": ends_at.isoformat(),
            "status": "open",
            "metadata": {
                "provider_source": "TradingSchedules",
                "provider_requested_exchange": "forts_futures_weekend",
                "provider_returned_exchange": "FORTS_FUTURES_WEEKEND",
                "provider_is_trading_day": True,
                "provider_interval_type": "regular_trading_session",
            },
        }
    ]


@pytest.mark.parametrize(
    ("days", "error"),
    [
        (
            [
                _trading_day(13, is_trading_day=True, intervals=_regular_sessions(13)),
                _trading_day(13, is_trading_day=True, intervals=_regular_sessions(13)),
            ],
            "TINVEST_SCHEDULE_DUPLICATE_DAY",
        ),
        (
            [
                _trading_day(13, is_trading_day=True, intervals=_regular_sessions(13)),
                _trading_day(15, is_trading_day=False),
            ],
            "TINVEST_SCHEDULE_DAY_COVERAGE_GAP",
        ),
        (
            [
                _trading_day(14, is_trading_day=True, intervals=_regular_sessions(14)),
                _trading_day(13, is_trading_day=True, intervals=_regular_sessions(13)),
            ],
            "TINVEST_SCHEDULE_DAY_ORDER_INVALID",
        ),
    ],
)
def test_schedule_rejects_non_exact_day_coverage(
    monkeypatch: pytest.MonkeyPatch,
    days: list[SimpleNamespace],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        _fetch(
            monkeypatch,
            _response(days),
            starts_at=datetime(2026, 8, 13, tzinfo=UTC),
            ends_at=datetime(2026, 8, 15, tzinfo=UTC),
        )


def test_trading_day_requires_the_exact_generic_regular_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specific_only = [
        _provider_interval(
            "regular_trading_session_main",
            datetime(2026, 8, 13, 7, tzinfo=UTC),
            datetime(2026, 8, 13, 16, tzinfo=UTC),
        )
    ]

    with pytest.raises(ValueError, match="TINVEST_SCHEDULE_REGULAR_INTERVALS_REQUIRED"):
        _fetch(
            monkeypatch,
            _response([_trading_day(13, is_trading_day=True, intervals=specific_only)]),
            starts_at=datetime(2026, 8, 13, tzinfo=UTC),
            ends_at=datetime(2026, 8, 14, tzinfo=UTC),
        )


def test_schedule_rejects_duplicate_provider_regular_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_interval = _provider_interval(
        "regular_trading_session",
        datetime(2026, 8, 13, 4, tzinfo=UTC),
        datetime(2026, 8, 13, 20, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="TINVEST_SCHEDULE_INTERVAL_DUPLICATE"):
        _fetch(
            monkeypatch,
            _response(
                [
                    _trading_day(
                        13,
                        is_trading_day=True,
                        intervals=[exact_interval, exact_interval],
                    )
                ]
            ),
            starts_at=datetime(2026, 8, 13, tzinfo=UTC),
            ends_at=datetime(2026, 8, 14, tzinfo=UTC),
        )


def test_schedule_rejects_overlapping_provider_regular_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intervals = [
        _provider_interval(
            "regular_trading_session",
            datetime(2026, 8, 13, 4, tzinfo=UTC),
            datetime(2026, 8, 13, 12, tzinfo=UTC),
        ),
        _provider_interval(
            "regular_trading_session",
            datetime(2026, 8, 13, 11, tzinfo=UTC),
            datetime(2026, 8, 13, 20, tzinfo=UTC),
        ),
    ]

    with pytest.raises(ValueError, match="TINVEST_SCHEDULE_INTERVAL_OVERLAP"):
        _fetch(
            monkeypatch,
            _response([_trading_day(13, is_trading_day=True, intervals=intervals)]),
            starts_at=datetime(2026, 8, 13, tzinfo=UTC),
            ends_at=datetime(2026, 8, 14, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("starts_at", "ends_at", "error"),
    [
        (
            datetime(2026, 8, 13),
            datetime(2026, 8, 14, tzinfo=UTC),
            "TINVEST_SCHEDULE_STARTS_AT_UTC_REQUIRED",
        ),
        (
            datetime(2026, 8, 14, tzinfo=UTC),
            datetime(2026, 8, 13, tzinfo=UTC),
            "TINVEST_SCHEDULE_RANGE_INVALID",
        ),
        (
            datetime(2026, 8, 13, tzinfo=UTC),
            datetime(2026, 8, 20, tzinfo=UTC) + timedelta(microseconds=1),
            "TINVEST_SCHEDULE_RANGE_TOO_LARGE",
        ),
    ],
)
def test_schedule_request_requires_one_bounded_aware_utc_window(
    starts_at: datetime,
    ends_at: datetime,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        TInvestScheduleRequest("Exact-Uid", starts_at, ends_at)


def test_schedule_rejects_uid_mismatch_before_requesting_exchange_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedule, "tinvest_uid_request", lambda uid: ("uid", uid))
    instruments = _Instruments(_response([]), uid="Different-Uid")

    with pytest.raises(ValueError, match="TINVEST_SCHEDULE_INSTRUMENT_UID_MISMATCH"):
        asyncio.run(
            fetch_tinvest_schedule(
                SimpleNamespace(instruments=instruments),
                TInvestScheduleRequest(
                    "Exact-Uid",
                    datetime(2026, 8, 13, tzinfo=UTC),
                    datetime(2026, 8, 14, tzinfo=UTC),
                ),
                timeout=2.0,
            )
        )
    assert instruments.calls == [("uid", "Exact-Uid")]
