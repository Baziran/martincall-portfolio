from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from aef_terminal.data.adapters._tinvest.qualification import tinvest_uid_request
from aef_terminal.data.instrument_identity import require_exact_identity_text


_PROVIDER = "tinvest"
_PROVIDER_SOURCE = "TradingSchedules"
_SCHEDULE_SOURCE = "tinvest:TradingSchedules"
_REGULAR_SESSION_TYPE = "regular_trading_session"
_MAX_SCHEDULE_SPAN = timedelta(days=7)
_PROVIDER_INTERVAL_SEAM = timedelta(seconds=1)


class TInvestScheduleInstrumentsService(Protocol):
    async def get_instrument_by(self, request: object) -> object: ...

    async def trading_schedules(self, request: object) -> object: ...


class TInvestScheduleServices(Protocol):
    instruments: TInvestScheduleInstrumentsService


@dataclass(frozen=True, slots=True)
class TInvestScheduleRequest:
    instrument_uid: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_uid",
            require_exact_identity_text(self.instrument_uid, field="instrument_uid"),
        )
        starts_at = _aware_utc(self.starts_at, field="starts_at")
        ends_at = _aware_utc(self.ends_at, field="ends_at")
        if ends_at <= starts_at:
            raise ValueError("TINVEST_SCHEDULE_RANGE_INVALID")
        if ends_at - starts_at > _MAX_SCHEDULE_SPAN:
            raise ValueError("TINVEST_SCHEDULE_RANGE_TOO_LARGE")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)


@dataclass(frozen=True, slots=True)
class _ProviderTradingDay:
    session_date: date
    is_trading_day: bool
    regular_intervals: tuple[tuple[datetime, datetime], ...]


async def fetch_tinvest_schedule(
    services: TInvestScheduleServices,
    request: TInvestScheduleRequest,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Fetch one exact UID's bounded provider-declared schedule."""

    if not isinstance(request, TInvestScheduleRequest):
        raise TypeError("TINVEST_SCHEDULE_REQUEST_REQUIRED")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("TINVEST_SCHEDULE_TIMEOUT_INVALID")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("TINVEST_SCHEDULE_TIMEOUT_INVALID")
    instruments = getattr(services, "instruments", None)
    if (
        instruments is None
        or not callable(getattr(instruments, "get_instrument_by", None))
        or not callable(getattr(instruments, "trading_schedules", None))
    ):
        raise TypeError("TINVEST_SCHEDULE_SERVICE_REQUIRED")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    instrument_response = await asyncio.wait_for(
        instruments.get_instrument_by(tinvest_uid_request(request.instrument_uid)),
        timeout=_remaining_timeout(loop, deadline),
    )
    instrument = getattr(instrument_response, "instrument", None)
    if instrument is None:
        raise ValueError("TINVEST_SCHEDULE_INSTRUMENT_RESPONSE_INVALID")
    returned_uid = require_exact_identity_text(
        getattr(instrument, "uid", None),
        field="instrument_uid",
    )
    if returned_uid != request.instrument_uid:
        raise ValueError("TINVEST_SCHEDULE_INSTRUMENT_UID_MISMATCH")
    requested_exchange = require_exact_identity_text(
        getattr(instrument, "exchange", None),
        field="exchange",
    )

    schedule_response = await asyncio.wait_for(
        instruments.trading_schedules(
            _trading_schedules_request(
                requested_exchange,
                starts_at=request.starts_at,
                ends_at=request.ends_at,
            )
        ),
        timeout=_remaining_timeout(loop, deadline),
    )
    returned_exchange, days = _parse_schedule_response(schedule_response, request=request)
    intervals = _materialize_exact_coverage(
        days,
        request=request,
        requested_exchange=requested_exchange,
        returned_exchange=returned_exchange,
    )
    return {
        "provider": _PROVIDER,
        "provider_contract_id": request.instrument_uid,
        "time_zone": "UTC",
        "trading_hours": "",
        "liquid_hours": "",
        "source": _SCHEDULE_SOURCE,
        "schedule_format": "provider_declared_intervals",
        "schedule_coverage_start": request.starts_at.isoformat(),
        "schedule_coverage_end": request.ends_at.isoformat(),
        "requested_start": request.starts_at.isoformat(),
        "requested_end": request.ends_at.isoformat(),
        "requested_exchange": requested_exchange,
        "returned_exchange": returned_exchange,
        "trading_intervals": intervals,
    }


def _parse_schedule_response(
    response: object,
    *,
    request: TInvestScheduleRequest,
) -> tuple[str, tuple[_ProviderTradingDay, ...]]:
    raw_schedules = getattr(response, "exchanges", None)
    if not _typed_sequence(raw_schedules) or len(raw_schedules) != 1:
        raise ValueError("TINVEST_SCHEDULE_EXCHANGE_RESPONSE_AMBIGUOUS")
    raw_schedule = raw_schedules[0]
    returned_exchange = require_exact_identity_text(
        getattr(raw_schedule, "exchange", None),
        field="returned_exchange",
    )
    raw_days = getattr(raw_schedule, "days", None)
    if not _typed_sequence(raw_days) or not raw_days:
        raise ValueError("TINVEST_SCHEDULE_DAYS_REQUIRED")
    days = tuple(_parse_trading_day(raw_day) for raw_day in raw_days)
    _validate_response_dates(days, request=request)
    return returned_exchange, days


def _parse_trading_day(raw_day: object) -> _ProviderTradingDay:
    day_timestamp = _provider_utc_datetime(getattr(raw_day, "date", None), field="day_date")
    if day_timestamp.timetz() != time(0, tzinfo=UTC):
        raise ValueError("TINVEST_SCHEDULE_DAY_DATE_INVALID")
    is_trading_day = getattr(raw_day, "is_trading_day", None)
    if type(is_trading_day) is not bool:
        raise ValueError("TINVEST_SCHEDULE_TRADING_DAY_FLAG_INVALID")
    raw_intervals = getattr(raw_day, "intervals", None)
    if not _typed_sequence(raw_intervals):
        raise ValueError("TINVEST_SCHEDULE_INTERVALS_INVALID")

    exact_regular: set[tuple[datetime, datetime]] = set()
    day_start = day_timestamp
    day_end = day_start + timedelta(days=1)
    for raw_interval in raw_intervals:
        interval_type = getattr(raw_interval, "type", None)
        if not isinstance(interval_type, str):
            raise ValueError("TINVEST_SCHEDULE_INTERVAL_TYPE_INVALID")
        if interval_type != _REGULAR_SESSION_TYPE:
            continue
        raw_bounds = getattr(raw_interval, "interval", None)
        if raw_bounds is None:
            raise ValueError("TINVEST_SCHEDULE_INTERVAL_BOUNDS_REQUIRED")
        starts_at = _provider_utc_datetime(
            getattr(raw_bounds, "start_ts", None),
            field="interval_start",
        )
        ends_at = _provider_utc_datetime(
            getattr(raw_bounds, "end_ts", None),
            field="interval_end",
        )
        if ends_at <= starts_at or starts_at < day_start or ends_at > day_end:
            raise ValueError("TINVEST_SCHEDULE_INTERVAL_RANGE_INVALID")
        exact_interval = (starts_at, ends_at)
        if exact_interval in exact_regular:
            raise ValueError("TINVEST_SCHEDULE_INTERVAL_DUPLICATE")
        exact_regular.add(exact_interval)

    if is_trading_day and not exact_regular:
        raise ValueError("TINVEST_SCHEDULE_REGULAR_INTERVALS_REQUIRED")
    if not is_trading_day and exact_regular:
        raise ValueError("TINVEST_SCHEDULE_CLOSED_DAY_HAS_REGULAR_INTERVAL")
    return _ProviderTradingDay(
        session_date=day_timestamp.date(),
        is_trading_day=is_trading_day,
        regular_intervals=_merge_regular_intervals(tuple(sorted(exact_regular))),
    )


def _validate_response_dates(
    days: tuple[_ProviderTradingDay, ...],
    *,
    request: TInvestScheduleRequest,
) -> None:
    returned_dates = tuple(day.session_date for day in days)
    if len(set(returned_dates)) != len(returned_dates):
        raise ValueError("TINVEST_SCHEDULE_DUPLICATE_DAY")
    if returned_dates != tuple(sorted(returned_dates)):
        raise ValueError("TINVEST_SCHEDULE_DAY_ORDER_INVALID")
    for previous, current in zip(returned_dates, returned_dates[1:], strict=False):
        if current != previous + timedelta(days=1):
            raise ValueError("TINVEST_SCHEDULE_DAY_COVERAGE_GAP")

    exact_dates = _exact_coverage_dates(request.starts_at, request.ends_at)
    accepted_date_sets = {exact_dates}
    if request.ends_at.timetz() == time(0, tzinfo=UTC):
        accepted_date_sets.add((*exact_dates, request.ends_at.date()))
    if returned_dates not in accepted_date_sets:
        raise ValueError("TINVEST_SCHEDULE_DAY_COVERAGE_MISMATCH")


def _merge_regular_intervals(
    intervals: tuple[tuple[datetime, datetime], ...],
) -> tuple[tuple[datetime, datetime], ...]:
    merged: list[tuple[datetime, datetime]] = []
    for starts_at, ends_at in intervals:
        if merged and starts_at < merged[-1][1]:
            raise ValueError("TINVEST_SCHEDULE_INTERVAL_OVERLAP")
        if not merged or starts_at > merged[-1][1] + _PROVIDER_INTERVAL_SEAM:
            merged.append((starts_at, ends_at))
            continue
        prior_start, prior_end = merged[-1]
        merged[-1] = (prior_start, max(prior_end, ends_at))
    return tuple(merged)


def _materialize_exact_coverage(
    days: tuple[_ProviderTradingDay, ...],
    *,
    request: TInvestScheduleRequest,
    requested_exchange: str,
    returned_exchange: str,
) -> list[dict[str, Any]]:
    by_date = {day.session_date: day for day in days}
    materialized: list[dict[str, Any]] = []
    for session_date in _exact_coverage_dates(request.starts_at, request.ends_at):
        day = by_date[session_date]
        day_start = datetime.combine(session_date, time.min, tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        coverage_start = max(request.starts_at, day_start)
        coverage_end = min(request.ends_at, day_end)
        clipped_open = tuple(
            (max(starts_at, coverage_start), min(ends_at, coverage_end))
            for starts_at, ends_at in day.regular_intervals
            if starts_at < coverage_end and ends_at > coverage_start
        )
        cursor = coverage_start
        for starts_at, ends_at in clipped_open:
            if starts_at > cursor:
                materialized.append(
                    _schedule_interval(
                        session_date,
                        cursor,
                        starts_at,
                        status="closed",
                        is_trading_day=day.is_trading_day,
                        requested_exchange=requested_exchange,
                        returned_exchange=returned_exchange,
                    )
                )
            materialized.append(
                _schedule_interval(
                    session_date,
                    starts_at,
                    ends_at,
                    status="open",
                    is_trading_day=day.is_trading_day,
                    requested_exchange=requested_exchange,
                    returned_exchange=returned_exchange,
                )
            )
            cursor = max(cursor, ends_at)
        if cursor < coverage_end:
            materialized.append(
                _schedule_interval(
                    session_date,
                    cursor,
                    coverage_end,
                    status="closed",
                    is_trading_day=day.is_trading_day,
                    requested_exchange=requested_exchange,
                    returned_exchange=returned_exchange,
                )
            )
    if not materialized:
        raise ValueError("TINVEST_SCHEDULE_EXACT_COVERAGE_EMPTY")
    return materialized


def _schedule_interval(
    session_date: date,
    starts_at: datetime,
    ends_at: datetime,
    *,
    status: str,
    is_trading_day: bool,
    requested_exchange: str,
    returned_exchange: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider_source": _PROVIDER_SOURCE,
        "provider_requested_exchange": requested_exchange,
        "provider_returned_exchange": returned_exchange,
        "provider_is_trading_day": is_trading_day,
    }
    if status == "open":
        metadata["provider_interval_type"] = _REGULAR_SESSION_TYPE
    else:
        metadata["provider_closed_complement"] = True
    return {
        "session_date": session_date.isoformat(),
        "session_type": "trading",
        "opens_at": starts_at.isoformat(),
        "closes_at": ends_at.isoformat(),
        "status": status,
        "metadata": metadata,
    }


def _exact_coverage_dates(starts_at: datetime, ends_at: datetime) -> tuple[date, ...]:
    last_date = (ends_at - timedelta(microseconds=1)).date()
    cursor = starts_at.date()
    dates: list[date] = []
    while cursor <= last_date:
        dates.append(cursor)
        cursor += timedelta(days=1)
    return tuple(dates)


def _remaining_timeout(loop: asyncio.AbstractEventLoop, deadline: float) -> float:
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError("TINVEST_SCHEDULE_TIMEOUT")
    return remaining


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"TINVEST_SCHEDULE_{field.upper()}_UTC_REQUIRED")
    return value.astimezone(UTC)


def _provider_utc_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"TINVEST_SCHEDULE_{field.upper()}_UTC_REQUIRED")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"TINVEST_SCHEDULE_{field.upper()}_UTC_REQUIRED")
    return value.astimezone(UTC)


def _typed_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _trading_schedules_request(
    exchange: str,
    *,
    starts_at: datetime,
    ends_at: datetime,
) -> object:
    from t_tech.invest.grpc import TradingSchedulesRequest

    return TradingSchedulesRequest(
        exchange=exchange,
        from_=starts_at,
        to=ends_at,
    )
