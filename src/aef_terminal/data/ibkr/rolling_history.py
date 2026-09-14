from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from aef_terminal.data.ibkr.history import ibkr_history_runtime_contracts
from aef_terminal.data.instrument_identity import (
    instrument_is_futures_root,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.domain import Bar, BarProviderRequest, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason
from aef_terminal.runtime.timeframes import interval_seconds, timestamp_is_bucket_aligned


IBKR_ROLLING_HISTORY_REQUEST_CONTRACT_VERSION = 2
IBKR_ROLLING_HISTORY_ADMISSION_CONTRACT_VERSION = 1
IBKR_ROLLING_HISTORY_PROVIDER_SOURCE = "IBKR_ROLLING_END_NOW"
IBKR_ROLLING_HISTORY_MAX_AGE = timedelta(days=6 * 366)

_PROVIDER = "ibkr"
_DATA_TYPE = "TRADES"
_CONTRACT_TYPE = "CONTFUT"
_SUPPORTED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "60m"})
_LOGGER = logging.getLogger(__name__)


class IbkrRollingHistoryTerminal(str, Enum):
    TARGET_CONTAINED = "target_contained"
    TARGET_NOT_CONTAINED = "target_not_contained"
    FINALITY_UNKNOWN = "finality_unknown"
    EMPTY = "empty"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class IbkrRollingHistoryRequest:
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    timeframe: str
    starts_at: datetime
    ends_at: datetime
    requested_at: datetime

    def __post_init__(self) -> None:
        for field in ("instrument_id", "route_fingerprint", "provider_symbol"):
            object.__setattr__(
                self,
                field,
                require_exact_identity_text(getattr(self, field), field=field),
            )
        if self.timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError("IBKR_ROLLING_HISTORY_TIMEFRAME_UNSUPPORTED")
        starts_at = _aware_utc(self.starts_at, field="starts_at")
        ends_at = _aware_utc(self.ends_at, field="ends_at")
        requested_at = _aware_utc(self.requested_at, field="requested_at")
        step_seconds = interval_seconds(self.timeframe)
        if (
            not timestamp_is_bucket_aligned(starts_at, step_seconds)
            or not timestamp_is_bucket_aligned(ends_at, step_seconds)
            or ends_at <= starts_at
            or ends_at > requested_at
            or requested_at - starts_at > IBKR_ROLLING_HISTORY_MAX_AGE
        ):
            raise ValueError("IBKR_ROLLING_HISTORY_RANGE_INVALID")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)
        object.__setattr__(self, "requested_at", requested_at)


@dataclass(frozen=True, slots=True)
class IbkrRollingHistoryResult:
    request: IbkrRollingHistoryRequest
    terminal: IbkrRollingHistoryTerminal
    authoritative_bars: tuple[Bar, ...]
    resolved_provider_contract_id: str | None
    response_starts_at: datetime | None = None
    response_ends_at: datetime | None = None
    error_code: str | None = None


def ibkr_rolling_history_request_max_span(timeframe: str) -> timedelta:
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError("IBKR_ROLLING_HISTORY_TIMEFRAME_UNSUPPORTED")
    return IBKR_ROLLING_HISTORY_MAX_AGE


def ibkr_rolling_duration(request: IbkrRollingHistoryRequest) -> str:
    """Enclose the target despite IBKR's partial current-day duration unit.

    Seconds are elapsed time, but an end-now N D response includes the current
    partial trading day. Rounding elapsed seconds to N days can therefore omit
    the target's first session. Reserve that partial day before choosing the
    provider unit; returned bounds, not this sizing allowance, decide admission.
    """
    step_seconds = interval_seconds(request.timeframe)
    seconds = max(
        int(math.ceil((request.requested_at - request.starts_at).total_seconds())) + step_seconds,
        step_seconds,
    )
    if seconds <= 86_400:
        return f"{seconds} S"
    days = int(math.ceil(seconds / 86_400)) + 1
    if days <= 365:
        return f"{days} D"
    years = int(math.ceil(days / 365))
    return f"{years} Y"


async def fetch_ibkr_rolling_history(
    request: IbkrRollingHistoryRequest,
    *,
    timeout: float,
    instrument: dict[str, Any],
) -> IbkrRollingHistoryResult:
    """Fetch one end-now CONTFUT response and admit only target-range bars."""

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("IBKR_ROLLING_HISTORY_TIMEOUT_INVALID")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("IBKR_ROLLING_HISTORY_TIMEOUT_INVALID")
    qualified = require_provider_identity(instrument, provider=_PROVIDER)
    if not instrument_is_futures_root(qualified):
        raise ValueError("IBKR_ROLLING_HISTORY_FUTURE_ROOT_REQUIRED")
    if (
        qualified_instrument_id(qualified),
        route_fingerprint(qualified),
    ) != (
        request.instrument_id,
        request.route_fingerprint,
    ):
        raise ValueError("IBKR_ROLLING_HISTORY_ROUTE_MISMATCH")

    feed_type, manager = ibkr_history_runtime_contracts()
    duration = ibkr_rolling_duration(request)
    feed = feed_type(
        interval=request.timeframe,
        range_="rolling",
        timeout=timeout_seconds,
        end_datetime="",
        duration=duration,
        instrument=qualified,
    )
    bars = await manager.run_coroutine(
        "history",
        f"{request.provider_symbol} {request.timeframe} rolling-end-now",
        feed._bars_async_owned,
        timeout=timeout_seconds,
    )
    result = _validate_response(
        request,
        bars,
        feed.history_response_contract_identity,
    )
    _LOGGER.info(
        "ibkr_rolling_history instrument_id=%s route_fingerprint=%s timeframe=%s "
        "requested_at=%s target_from=%s target_to=%s end_datetime='' duration=%s "
        "contract_id=%s response_from=%s response_to=%s response_count=%s "
        "admitted_count=%s terminal=%s code=%s",
        request.instrument_id,
        request.route_fingerprint,
        request.timeframe,
        request.requested_at.isoformat(),
        request.starts_at.isoformat(),
        request.ends_at.isoformat(),
        duration,
        result.resolved_provider_contract_id,
        result.response_starts_at,
        result.response_ends_at,
        len(bars) if isinstance(bars, list) else None,
        len(result.authoritative_bars),
        result.terminal.value,
        result.error_code,
    )
    return result


def _validate_response(
    request: IbkrRollingHistoryRequest,
    raw_bars: object,
    response_contract_identity: object,
) -> IbkrRollingHistoryResult:
    if not isinstance(response_contract_identity, tuple) or len(response_contract_identity) != 2:
        return _malformed(request, "IBKR_ROLLING_HISTORY_CONTRACT_IDENTITY_MISSING")
    raw_contract_id, raw_contract_type = response_contract_identity
    if (
        not isinstance(raw_contract_id, str)
        or not raw_contract_id
        or raw_contract_type != _CONTRACT_TYPE
    ):
        return _malformed(request, "IBKR_ROLLING_HISTORY_CONTRACT_IDENTITY_INVALID")
    if not isinstance(raw_bars, list) or any(not isinstance(bar, Bar) for bar in raw_bars):
        return _malformed(request, "IBKR_ROLLING_HISTORY_RESPONSE_INVALID")

    bars = sorted(raw_bars, key=lambda item: item.ts)
    if not bars:
        return IbkrRollingHistoryResult(
            request=request,
            terminal=IbkrRollingHistoryTerminal.EMPTY,
            authoritative_bars=(),
            resolved_provider_contract_id=raw_contract_id,
            error_code="IBKR_ROLLING_HISTORY_RESPONSE_EMPTY",
        )

    step_seconds = interval_seconds(request.timeframe)
    step = timedelta(seconds=step_seconds)
    seen: set[datetime] = set()
    authoritative: list[Bar] = []
    finality_unknown = False
    for bar in bars:
        ts = bar.ts.astimezone(UTC)
        provenance = bar.provenance
        if (
            not timestamp_is_bucket_aligned(ts, step_seconds)
            or ts in seen
            or bar.timeframe != request.timeframe
            or provenance is None
            or provenance.provider != _PROVIDER
            or provenance.instrument_id != request.instrument_id
            or provenance.route_fingerprint != request.route_fingerprint
            or provenance.request_type is not BarProviderRequest.HISTORICAL
            or provenance.provider_contract_id != raw_contract_id
            or provenance.provider_contract_type != _CONTRACT_TYPE
            or provenance.data_type != _DATA_TYPE
        ):
            return _malformed(request, "IBKR_ROLLING_HISTORY_BAR_IDENTITY_INVALID")
        seen.add(ts)
        if not request.starts_at <= ts < request.ends_at:
            continue
        if BarState(bar.state) is not BarState.CONFIRMED or not bar.closed:
            finality_unknown = True
            continue
        reject_reason = authoritative_bar_reject_reason(
            _PROVIDER,
            bar,
            instrument_id=request.instrument_id,
            route_fingerprint=request.route_fingerprint,
            data_type=_DATA_TYPE,
        )
        if reject_reason is not None:
            return _malformed(
                request,
                f"IBKR_ROLLING_HISTORY_BAR_REJECTED_{reject_reason}",
            )
        authoritative.append(bar)

    response_starts_at = bars[0].ts.astimezone(UTC)
    response_ends_at = bars[-1].ts.astimezone(UTC) + step
    if finality_unknown:
        return IbkrRollingHistoryResult(
            request=request,
            terminal=IbkrRollingHistoryTerminal.FINALITY_UNKNOWN,
            authoritative_bars=tuple(authoritative),
            resolved_provider_contract_id=raw_contract_id,
            response_starts_at=response_starts_at,
            response_ends_at=response_ends_at,
            error_code="IBKR_ROLLING_HISTORY_FINALITY_UNKNOWN",
        )
    if response_starts_at <= request.starts_at and response_ends_at >= request.ends_at:
        return IbkrRollingHistoryResult(
            request=request,
            terminal=IbkrRollingHistoryTerminal.TARGET_CONTAINED,
            authoritative_bars=tuple(authoritative),
            resolved_provider_contract_id=raw_contract_id,
            response_starts_at=response_starts_at,
            response_ends_at=response_ends_at,
        )
    return IbkrRollingHistoryResult(
        request=request,
        terminal=IbkrRollingHistoryTerminal.TARGET_NOT_CONTAINED,
        authoritative_bars=tuple(authoritative),
        resolved_provider_contract_id=raw_contract_id,
        response_starts_at=response_starts_at,
        response_ends_at=response_ends_at,
        error_code="IBKR_ROLLING_HISTORY_TARGET_NOT_CONTAINED",
    )


def _malformed(request: IbkrRollingHistoryRequest, code: str) -> IbkrRollingHistoryResult:
    return IbkrRollingHistoryResult(
        request=request,
        terminal=IbkrRollingHistoryTerminal.MALFORMED,
        authoritative_bars=(),
        resolved_provider_contract_id=None,
        error_code=code,
    )


def _aware_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"IBKR_ROLLING_HISTORY_{field.upper()}_INVALID")
    return value.astimezone(UTC)
