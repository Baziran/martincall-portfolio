from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from aef_terminal.data.ibkr.history import ibkr_history_runtime_contracts
from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.domain import Bar, BarProviderRequest, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason
from aef_terminal.runtime.timeframes import interval_seconds, timestamp_is_bucket_aligned


_PROVIDER = "ibkr"
_PROVIDER_SOURCE = "IBKR_HISTORICAL"
_DATA_TYPE = "TRADES"
_MAX_SPAN = timedelta(days=1)
_SUPPORTED_TIMEFRAMES = frozenset({"1m", "5m", "15m", "60m"})


class IbkrHistoryTerminal(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class IbkrHistoryRequest:
    provider_contract_id: str
    provider_contract_type: str
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    timeframe: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        for field in (
            "provider_contract_id",
            "provider_contract_type",
            "instrument_id",
            "route_fingerprint",
            "provider_symbol",
        ):
            object.__setattr__(
                self,
                field,
                require_exact_identity_text(getattr(self, field), field=field),
            )
        if self.timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError("IBKR_HISTORY_TIMEFRAME_UNSUPPORTED")
        starts_at = _aware_utc(self.starts_at, field="starts_at")
        ends_at = _aware_utc(self.ends_at, field="ends_at")
        step_seconds = interval_seconds(self.timeframe)
        if (
            not timestamp_is_bucket_aligned(starts_at, step_seconds)
            or not timestamp_is_bucket_aligned(ends_at, step_seconds)
            or ends_at <= starts_at
            or ends_at - starts_at > _MAX_SPAN
        ):
            raise ValueError("IBKR_HISTORY_RANGE_INVALID")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)


@dataclass(frozen=True, slots=True)
class IbkrHistoryResult:
    request: IbkrHistoryRequest
    terminal: IbkrHistoryTerminal
    authoritative_bars: tuple[Bar, ...]
    response_count: int | None
    provider_source: str = _PROVIDER_SOURCE
    error_code: str | None = None


def ibkr_history_request_max_span(timeframe: str) -> timedelta:
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError("IBKR_HISTORY_TIMEFRAME_UNSUPPORTED")
    return _MAX_SPAN


async def fetch_ibkr_exact_history(
    request: IbkrHistoryRequest,
    *,
    timeout: float,
    instrument: dict[str, Any],
) -> IbkrHistoryResult:
    """Await one IBKR historicalDataEnd-backed exact TRADES response."""

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("IBKR_HISTORY_TIMEOUT_INVALID")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("IBKR_HISTORY_TIMEOUT_INVALID")
    qualified = require_provider_identity(instrument, provider=_PROVIDER)
    if (
        qualified_instrument_id(qualified),
        route_fingerprint(qualified),
    ) != (
        request.instrument_id,
        request.route_fingerprint,
    ):
        raise ValueError("IBKR_HISTORY_ROUTE_MISMATCH")
    feed_type, manager = ibkr_history_runtime_contracts()
    span_seconds = int((request.ends_at - request.starts_at).total_seconds())
    feed = feed_type(
        interval=request.timeframe,
        range_="1d",
        timeout=timeout_seconds,
        end_datetime=request.ends_at,
        duration=f"{span_seconds} S",
        instrument=qualified,
    )
    bars = await manager.run_coroutine(
        "history",
        f"{request.provider_symbol} {request.timeframe} exact",
        feed._bars_async_owned,
        timeout=timeout_seconds,
    )
    return _validate_response(request, bars)


def _validate_response(
    request: IbkrHistoryRequest,
    raw_bars: object,
) -> IbkrHistoryResult:
    if not isinstance(raw_bars, list) or any(not isinstance(bar, Bar) for bar in raw_bars):
        return _malformed(request, "IBKR_HISTORY_RESPONSE_INVALID")
    bars = sorted(raw_bars, key=lambda item: item.ts)
    seen: set[datetime] = set()
    authoritative: list[Bar] = []
    finality_unknown = False
    step_seconds = interval_seconds(request.timeframe)
    for bar in bars:
        ts = bar.ts.astimezone(UTC)
        provenance = bar.provenance
        if not timestamp_is_bucket_aligned(ts, step_seconds):
            return _malformed(request, "IBKR_HISTORY_BAR_TIMESTAMP_INVALID")
        if ts in seen:
            return _malformed(request, "IBKR_HISTORY_BAR_DUPLICATE")
        if (
            bar.timeframe != request.timeframe
            or provenance is None
            or provenance.provider != _PROVIDER
            or provenance.instrument_id != request.instrument_id
            or provenance.route_fingerprint != request.route_fingerprint
            or provenance.request_type is not BarProviderRequest.HISTORICAL
            or provenance.provider_contract_id != request.provider_contract_id
            or provenance.provider_contract_type != request.provider_contract_type
            or provenance.data_type != _DATA_TYPE
        ):
            return _malformed(request, "IBKR_HISTORY_BAR_IDENTITY_INVALID")
        seen.add(ts)
        # IBKR defines the response as the available bars closest to the
        # requested end time. Around closed sessions it can therefore include
        # valid bars before the exact wall-clock duration. They are provider
        # evidence, but not members of this half-open canonical snapshot.
        if not request.starts_at <= ts < request.ends_at:
            continue
        state = BarState(bar.state)
        if state is not BarState.CONFIRMED or not bar.closed:
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
                f"IBKR_HISTORY_BAR_REJECTED_{reject_reason}",
            )
        authoritative.append(bar)
    if finality_unknown:
        return IbkrHistoryResult(
            request=request,
            terminal=IbkrHistoryTerminal.INCOMPLETE,
            authoritative_bars=tuple(authoritative),
            response_count=len(bars),
            error_code="IBKR_HISTORY_FINALITY_UNKNOWN",
        )
    return IbkrHistoryResult(
        request=request,
        terminal=IbkrHistoryTerminal.COMPLETE,
        authoritative_bars=tuple(authoritative),
        response_count=len(authoritative),
    )


def _malformed(request: IbkrHistoryRequest, code: str) -> IbkrHistoryResult:
    return IbkrHistoryResult(
        request=request,
        terminal=IbkrHistoryTerminal.MALFORMED,
        authoritative_bars=(),
        response_count=None,
        error_code=code,
    )


def _aware_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"IBKR_HISTORY_{field.upper()}_INVALID")
    return value.astimezone(UTC)
