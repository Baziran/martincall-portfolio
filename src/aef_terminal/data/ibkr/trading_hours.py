from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aef_terminal.config import AppConfig
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    qualified_instrument_id,
)
from aef_terminal.data.ibkr.contracts import (
    _history_use_rth,
    _quote_contract_for_instrument,
    _require_ibkr_instrument,
)
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.data.ibkr.session import _connected_ib_async


@dataclass(frozen=True, slots=True)
class _IBKRHistoricalSession:
    session_date: date
    opens_at: datetime
    closes_at: datetime
    provider_ref_date: str


@dataclass(frozen=True, slots=True)
class _IBKRHistoricalSchedule:
    provider_reported_start: datetime
    provider_reported_end: datetime
    coverage_start: datetime
    coverage_end: datetime
    sessions: tuple[_IBKRHistoricalSession, ...]


def _parse_ibkr_schedule_datetime(
    raw_value: object,
    *,
    parser: Callable[[str], object],
    session_timezone: ZoneInfo,
    field: str,
    contract_id: str,
) -> datetime:
    if not isinstance(raw_value, str) or not raw_value:
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_{field}_INVALID conId={contract_id}")
    try:
        parsed = parser(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_{field}_INVALID conId={contract_id}") from exc
    if not isinstance(parsed, datetime):
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_{field}_INVALID conId={contract_id}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=session_timezone)
    return parsed.astimezone(UTC)


def _parse_ibkr_session_ref_date(
    raw_value: object,
    *,
    contract_id: str,
) -> tuple[str, date]:
    if not isinstance(raw_value, str) or len(raw_value) != 8 or not raw_value.isascii():
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_SESSION_REF_DATE_INVALID conId={contract_id}")
    try:
        parsed = datetime.strptime(raw_value, "%Y%m%d").date()
    except ValueError as exc:
        raise RuntimeError(
            f"IBKR_TRADING_SCHEDULE_SESSION_REF_DATE_INVALID conId={contract_id}"
        ) from exc
    if parsed.strftime("%Y%m%d") != raw_value:
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_SESSION_REF_DATE_INVALID conId={contract_id}")
    return raw_value, parsed


def _parse_ibkr_historical_schedule(
    historical_schedule: object,
    *,
    parser: Callable[[str], object],
    session_timezone: ZoneInfo,
    requested_start: datetime,
    requested_end: datetime,
    contract_id: str,
) -> _IBKRHistoricalSchedule:
    provider_reported_start = _parse_ibkr_schedule_datetime(
        getattr(historical_schedule, "startDateTime", None),
        parser=parser,
        session_timezone=session_timezone,
        field="COVERAGE_START",
        contract_id=contract_id,
    )
    provider_reported_end = _parse_ibkr_schedule_datetime(
        getattr(historical_schedule, "endDateTime", None),
        parser=parser,
        session_timezone=session_timezone,
        field="COVERAGE_END",
        contract_id=contract_id,
    )
    if provider_reported_end <= provider_reported_start:
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_COVERAGE_RANGE_INVALID conId={contract_id}")
    if provider_reported_end <= requested_start or provider_reported_start >= requested_end:
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_COVERAGE_OUTSIDE_REQUEST conId={contract_id}")

    raw_sessions = getattr(historical_schedule, "sessions", None)
    if (
        not isinstance(raw_sessions, Sequence)
        or isinstance(raw_sessions, (str, bytes, bytearray))
        or not raw_sessions
    ):
        raise RuntimeError(f"IBKR_TRADING_SCHEDULE_SESSIONS_REQUIRED conId={contract_id}")

    parsed_sessions: list[_IBKRHistoricalSession] = []
    exact_bounds: set[tuple[datetime, datetime]] = set()
    for raw_session in raw_sessions:
        opens_at = _parse_ibkr_schedule_datetime(
            getattr(raw_session, "startDateTime", None),
            parser=parser,
            session_timezone=session_timezone,
            field="SESSION_START",
            contract_id=contract_id,
        )
        closes_at = _parse_ibkr_schedule_datetime(
            getattr(raw_session, "endDateTime", None),
            parser=parser,
            session_timezone=session_timezone,
            field="SESSION_END",
            contract_id=contract_id,
        )
        if closes_at <= opens_at:
            raise RuntimeError(f"IBKR_TRADING_SCHEDULE_SESSION_RANGE_INVALID conId={contract_id}")
        provider_ref_date, session_date = _parse_ibkr_session_ref_date(
            getattr(raw_session, "refDate", None),
            contract_id=contract_id,
        )
        bounds = (opens_at, closes_at)
        if bounds in exact_bounds:
            raise RuntimeError(f"IBKR_TRADING_SCHEDULE_SESSION_DUPLICATE conId={contract_id}")
        exact_bounds.add(bounds)
        parsed_sessions.append(
            _IBKRHistoricalSession(
                session_date=session_date,
                opens_at=opens_at,
                closes_at=closes_at,
                provider_ref_date=provider_ref_date,
            )
        )

    ordered_sessions = tuple(
        sorted(parsed_sessions, key=lambda item: (item.opens_at, item.closes_at))
    )
    for previous, current in zip(ordered_sessions, ordered_sessions[1:], strict=False):
        if current.opens_at < previous.closes_at:
            raise RuntimeError(f"IBKR_TRADING_SCHEDULE_SESSION_OVERLAP conId={contract_id}")
    coverage_start = min(provider_reported_start, ordered_sessions[0].opens_at)
    coverage_end = max(provider_reported_end, ordered_sessions[-1].closes_at)
    return _IBKRHistoricalSchedule(
        provider_reported_start=provider_reported_start,
        provider_reported_end=provider_reported_end,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        sessions=ordered_sessions,
    )


def _closed_ibkr_schedule_intervals(
    starts_at: datetime,
    ends_at: datetime,
    *,
    session_type: str,
    session_timezone: ZoneInfo,
    timezone_name: str,
    use_rth: bool,
) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    cursor = starts_at
    while cursor < ends_at:
        local_cursor = cursor.astimezone(session_timezone)
        next_local_midnight = datetime.combine(
            local_cursor.date() + timedelta(days=1),
            time.min,
            tzinfo=session_timezone,
        ).astimezone(UTC)
        segment_end = min(ends_at, next_local_midnight)
        if segment_end <= cursor:
            raise RuntimeError("IBKR_TRADING_SCHEDULE_LOCAL_DATE_RANGE_INVALID")
        intervals.append(
            {
                "session_date": local_cursor.date().isoformat(),
                "session_type": session_type,
                "opens_at": cursor.isoformat(),
                "closes_at": segment_end.isoformat(),
                "status": "closed",
                "metadata": {
                    "provider_timezone": timezone_name,
                    "provider_source": "reqHistoricalSchedule",
                    "provider_closed_complement": True,
                    "use_rth": use_rth,
                },
            }
        )
        cursor = segment_end
    return intervals


def _materialize_ibkr_schedule_intervals(
    sessions: tuple[_IBKRHistoricalSession, ...],
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    session_type: str,
    session_timezone: ZoneInfo,
    timezone_name: str,
    use_rth: bool,
) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    cursor = coverage_start
    for session in sessions:
        intervals.extend(
            _closed_ibkr_schedule_intervals(
                cursor,
                session.opens_at,
                session_type=session_type,
                session_timezone=session_timezone,
                timezone_name=timezone_name,
                use_rth=use_rth,
            )
        )
        intervals.append(
            {
                "session_date": session.session_date.isoformat(),
                "session_type": session_type,
                "opens_at": session.opens_at.isoformat(),
                "closes_at": session.closes_at.isoformat(),
                "status": "open",
                "metadata": {
                    "provider_ref_date": session.provider_ref_date,
                    "provider_timezone": timezone_name,
                    "provider_source": "reqHistoricalSchedule",
                    "use_rth": use_rth,
                },
            }
        )
        cursor = session.closes_at
    intervals.extend(
        _closed_ibkr_schedule_intervals(
            cursor,
            coverage_end,
            session_type=session_type,
            session_timezone=session_timezone,
            timezone_name=timezone_name,
            use_rth=use_rth,
        )
    )
    return intervals


async def fetch_trading_hours_async(
    instrument: dict[str, Any],
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    timeout: float = 6.0,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch IBKR contract trading-hours metadata on the owner loop."""
    resolved_instrument = _require_ibkr_instrument(instrument)
    instrument_id = qualified_instrument_id(resolved_instrument)
    config = AppConfig()
    resolved_host = host or config.ibkr_host
    resolved_port = int(port or config.ibkr_port)
    resolved_client_id = int(client_id or config.ibkr_history_client_id)
    resolved_readonly = config.ibkr_readonly if readonly is None else bool(readonly)

    if (starts_at is None) != (ends_at is None):
        raise ValueError("IBKR_TRADING_SCHEDULE_WINDOW_REQUIRES_BOTH_BOUNDS")
    requested_start = (
        starts_at.astimezone(UTC)
        if starts_at is not None and starts_at.tzinfo is not None
        else starts_at
    )
    requested_end = (
        ends_at.astimezone(UTC) if ends_at is not None and ends_at.tzinfo is not None else ends_at
    )
    if requested_start is not None and requested_start.tzinfo is None:
        raise ValueError("IBKR_TRADING_SCHEDULE_START_MUST_BE_TIMEZONE_AWARE")
    if requested_end is not None and requested_end.tzinfo is None:
        raise ValueError("IBKR_TRADING_SCHEDULE_END_MUST_BE_TIMEZONE_AWARE")
    if (
        requested_start is not None
        and requested_end is not None
        and requested_end <= requested_start
    ):
        raise ValueError("IBKR_TRADING_SCHEDULE_WINDOW_INVALID")

    async def _fetch() -> dict[str, Any]:
        ib = await _connected_ib_async(
            resolved_host, resolved_port, resolved_client_id, resolved_readonly, timeout
        )
        contract = _quote_contract_for_instrument(
            ib,
            resolved_instrument,
        )
        con_id = parse_exact_positive_decimal_provider_id(getattr(contract, "conId", 0))
        contract_id = str(con_id) if con_id > 0 else ""
        if con_id <= 0:
            raise RuntimeError(
                f"IBKR_TRADING_SCHEDULE_CONTRACT_ID_REQUIRED instrument_id={instrument_id}"
            )
        if not str(getattr(contract, "exchange", "") or ""):
            raise RuntimeError(
                f"IBKR_TRADING_SCHEDULE_EXCHANGE_REQUIRED instrument_id={instrument_id}"
            )
        from ib_async import util

        historical_schedule = None
        historical_session_type = "trading"
        if requested_start is not None and requested_end is not None:
            num_days = max((requested_end.date() - requested_start.date()).days + 2, 2)
            use_rth = _history_use_rth(resolved_instrument)
            historical_session_type = "liquid" if use_rth else "trading"
            details, historical_schedule = await asyncio.wait_for(
                asyncio.gather(
                    ib.reqContractDetailsAsync(contract),
                    ib.reqHistoricalScheduleAsync(
                        contract,
                        num_days,
                        endDateTime=requested_end,
                        useRTH=use_rth,
                    ),
                ),
                timeout=max(float(timeout), 1.0),
            )
        else:
            details = await asyncio.wait_for(
                ib.reqContractDetailsAsync(contract),
                timeout=max(float(timeout), 1.0),
            )
        if not details:
            raise RuntimeError(f"IBKR returned no contract details for conId={contract_id}")
        detail = details[0]
        detail_contract = getattr(detail, "contract", None)
        payload: dict[str, Any] = {
            "provider": "ibkr",
            "provider_contract_id": contract_id,
            "con_id": getattr(detail_contract, "conId", None),
            "local_symbol": str(getattr(detail_contract, "localSymbol", "") or ""),
            "time_zone": str(getattr(detail, "timeZoneId", "") or ""),
            "trading_hours": str(getattr(detail, "tradingHours", "") or ""),
            "liquid_hours": str(getattr(detail, "liquidHours", "") or ""),
            "source": "ibkr:contract-details",
        }
        if historical_schedule is None:
            return payload

        timezone_name = str(
            getattr(historical_schedule, "timeZone", "") or getattr(detail, "timeZoneId", "") or ""
        )
        if not timezone_name:
            raise RuntimeError(
                f"IBKR historical schedule returned no timezone for conId={contract_id}"
            )
        try:
            session_timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise RuntimeError(
                f"IBKR historical schedule returned unsupported timezone={timezone_name!r} conId={contract_id}"
            ) from exc

        assert requested_start is not None
        assert requested_end is not None
        schedule = _parse_ibkr_historical_schedule(
            historical_schedule,
            parser=util.parseIBDatetime,
            session_timezone=session_timezone,
            requested_start=requested_start,
            requested_end=requested_end,
            contract_id=contract_id,
        )
        trading_intervals = _materialize_ibkr_schedule_intervals(
            schedule.sessions,
            coverage_start=schedule.coverage_start,
            coverage_end=schedule.coverage_end,
            session_type=historical_session_type,
            session_timezone=session_timezone,
            timezone_name=timezone_name,
            use_rth=use_rth,
        )
        payload.update(
            {
                "time_zone": timezone_name,
                "source": "ibkr:reqHistoricalSchedule",
                "schedule_format": "provider_declared_intervals",
                "schedule_coverage_start": schedule.coverage_start.isoformat(),
                "schedule_coverage_end": schedule.coverage_end.isoformat(),
                "provider_reported_schedule_start": schedule.provider_reported_start.isoformat(),
                "provider_reported_schedule_end": schedule.provider_reported_end.isoformat(),
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "trading_intervals": trading_intervals,
            }
        )
        return payload

    return await ibkr_market_data_manager.run_coroutine(
        "history",
        f"trading hours instrument_id={instrument_id}",
        _fetch,
        timeout=max(float(timeout) + 2.0, 3.0),
    )
