from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.adapters._tinvest.values import tinvest_quotation_value
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason
from aef_terminal.runtime.timeframes import interval_seconds, timestamp_is_bucket_aligned


_PROVIDER = "tinvest"
_PROVIDER_SOURCE = "EXCHANGE"
_BAR_SOURCE = "tinvest:exchange"
_PROVIDER_CONTRACT_TYPE = "INSTRUMENT_UID"
_DATA_TYPE = "TRADES"
_MAX_CHUNK_SPAN = timedelta(days=1)
_PROVIDER_RESPONSE_LIMIT = 2_400
_SDK_INTERVAL_NAME = {
    "1m": "CANDLE_INTERVAL_1_MIN",
    "5m": "CANDLE_INTERVAL_5_MIN",
    "15m": "CANDLE_INTERVAL_15_MIN",
    "60m": "CANDLE_INTERVAL_HOUR",
}


class TInvestHistoryTerminal(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


class TInvestMarketDataService(Protocol):
    async def get_candles(self, request: object) -> object: ...


class TInvestHistoryServices(Protocol):
    market_data: TInvestMarketDataService


@dataclass(frozen=True, slots=True)
class TInvestHistoryRequest:
    instrument_uid: str
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    timeframe: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "instrument_uid",
            "instrument_id",
            "route_fingerprint",
            "provider_symbol",
        ):
            object.__setattr__(
                self,
                field_name,
                require_exact_identity_text(
                    getattr(self, field_name),
                    field=field_name,
                ),
            )
        if self.timeframe not in _SDK_INTERVAL_NAME:
            raise ValueError("TINVEST_HISTORY_TIMEFRAME_UNSUPPORTED")
        expected_instrument_id = f"{_PROVIDER}|contract|{self.instrument_uid}"
        if self.instrument_id != expected_instrument_id:
            raise ValueError("TINVEST_HISTORY_INSTRUMENT_ID_MISMATCH")
        if self.route_fingerprint != self.instrument_id:
            raise ValueError("TINVEST_HISTORY_ROUTE_FINGERPRINT_MISMATCH")
        if self.provider_symbol != self.instrument_uid:
            raise ValueError("TINVEST_HISTORY_PROVIDER_SYMBOL_MISMATCH")
        starts_at = _aware_utc(self.starts_at, field="starts_at")
        ends_at = _aware_utc(self.ends_at, field="ends_at")
        step_seconds = interval_seconds(self.timeframe)
        if not timestamp_is_bucket_aligned(starts_at, step_seconds):
            raise ValueError("TINVEST_HISTORY_START_UNALIGNED")
        if not timestamp_is_bucket_aligned(ends_at, step_seconds):
            raise ValueError("TINVEST_HISTORY_END_UNALIGNED")
        if ends_at <= starts_at:
            raise ValueError("TINVEST_HISTORY_RANGE_INVALID")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)


@dataclass(frozen=True, slots=True)
class TInvestHistoryChunkRequest:
    starts_at: datetime
    ends_at: datetime
    source: str = _PROVIDER_SOURCE
    request_limit: None = None
    provider_limit: int = _PROVIDER_RESPONSE_LIMIT

    @property
    def range_seconds(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds())


@dataclass(frozen=True, slots=True)
class TInvestHistoryChunkResult:
    request: TInvestHistoryChunkRequest
    terminal: TInvestHistoryTerminal
    response_count: int | None
    authoritative_count: int
    provisional_count: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TInvestHistoryResult:
    request: TInvestHistoryRequest
    terminal: TInvestHistoryTerminal
    authoritative_bars: tuple[Bar, ...]
    provisional_bars: tuple[Bar, ...]
    chunks: tuple[TInvestHistoryChunkResult, ...]
    source: str = _PROVIDER_SOURCE
    request_limit: None = None
    provider_limit_per_chunk: int = _PROVIDER_RESPONSE_LIMIT
    error_code: str | None = None

    @property
    def response_count(self) -> int | None:
        counts = [chunk.response_count for chunk in self.chunks]
        if any(count is None for count in counts):
            return None
        return sum(int(count) for count in counts)

    @property
    def authoritative_count(self) -> int:
        return len(self.authoritative_bars)

    @property
    def provisional_count(self) -> int:
        return len(self.provisional_bars)


class _MalformedHistoryResponse(ValueError):
    def __init__(self, code: str, *, response_count: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.response_count = response_count


def plan_tinvest_history_chunks(
    request: TInvestHistoryRequest,
) -> tuple[TInvestHistoryChunkRequest, ...]:
    """Partition one exact range into provider-safe, non-overlapping chunks."""

    cursor = request.starts_at
    chunks: list[TInvestHistoryChunkRequest] = []
    while request.ends_at - cursor > _MAX_CHUNK_SPAN:
        chunk_end = cursor + _MAX_CHUNK_SPAN
        chunks.append(TInvestHistoryChunkRequest(cursor, chunk_end))
        cursor = chunk_end
    chunks.append(TInvestHistoryChunkRequest(cursor, request.ends_at))
    return tuple(chunks)


def tinvest_history_request_max_span(timeframe: str) -> timedelta:
    """Return the provider-owned bounded repair span for one canonical timeframe."""

    if timeframe not in _SDK_INTERVAL_NAME:
        raise ValueError("TINVEST_HISTORY_TIMEFRAME_UNSUPPORTED")
    return _MAX_CHUNK_SPAN


async def fetch_tinvest_history(
    services: TInvestHistoryServices,
    request: TInvestHistoryRequest,
    *,
    timeout: float,
) -> TInvestHistoryResult:
    """Fetch and validate one exact range without storage or coverage side effects."""

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("TINVEST_HISTORY_TIMEOUT_INVALID")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("TINVEST_HISTORY_TIMEOUT_INVALID")
    market_data = getattr(services, "market_data", None)
    if market_data is None or not callable(getattr(market_data, "get_candles", None)):
        raise TypeError("TINVEST_HISTORY_SERVICE_REQUIRED")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    provenance = BarProvenance(
        provider=_PROVIDER,
        instrument_id=request.instrument_id,
        route_fingerprint=request.route_fingerprint,
        request_type=BarProviderRequest.HISTORICAL,
        provider_contract_id=request.instrument_uid,
        provider_contract_type=_PROVIDER_CONTRACT_TYPE,
        data_type=_DATA_TYPE,
    )
    authoritative: list[Bar] = []
    provisional: list[Bar] = []
    chunk_results: list[TInvestHistoryChunkResult] = []
    seen_timestamps: set[datetime] = set()

    for chunk in plan_tinvest_history_chunks(request):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("TINVEST_HISTORY_TIMEOUT")
        sdk_request = _build_sdk_request(request, chunk)
        response = await asyncio.wait_for(
            market_data.get_candles(sdk_request),
            timeout=remaining,
        )
        try:
            chunk_authoritative, chunk_provisional, response_count = _parse_chunk(
                response,
                request=request,
                chunk=chunk,
                provenance=provenance,
                seen_timestamps=seen_timestamps,
            )
        except _MalformedHistoryResponse as exc:
            chunk_results.append(
                TInvestHistoryChunkResult(
                    request=chunk,
                    terminal=TInvestHistoryTerminal.MALFORMED,
                    response_count=exc.response_count,
                    authoritative_count=0,
                    provisional_count=0,
                    error_code=exc.code,
                )
            )
            return TInvestHistoryResult(
                request=request,
                terminal=TInvestHistoryTerminal.MALFORMED,
                authoritative_bars=(),
                provisional_bars=(),
                chunks=tuple(chunk_results),
                error_code=exc.code,
            )
        authoritative.extend(chunk_authoritative)
        provisional.extend(chunk_provisional)
        chunk_results.append(
            TInvestHistoryChunkResult(
                request=chunk,
                terminal=(
                    TInvestHistoryTerminal.INCOMPLETE
                    if chunk_provisional
                    else TInvestHistoryTerminal.COMPLETE
                ),
                response_count=response_count,
                authoritative_count=len(chunk_authoritative),
                provisional_count=len(chunk_provisional),
            )
        )

    terminal = TInvestHistoryTerminal.INCOMPLETE if provisional else TInvestHistoryTerminal.COMPLETE
    return TInvestHistoryResult(
        request=request,
        terminal=terminal,
        authoritative_bars=tuple(sorted(authoritative, key=lambda bar: bar.ts)),
        provisional_bars=tuple(sorted(provisional, key=lambda bar: bar.ts)),
        chunks=tuple(chunk_results),
    )


def _parse_chunk(
    response: object,
    *,
    request: TInvestHistoryRequest,
    chunk: TInvestHistoryChunkRequest,
    provenance: BarProvenance,
    seen_timestamps: set[datetime],
) -> tuple[list[Bar], list[Bar], int]:
    raw_candles = getattr(response, "candles", None)
    if not isinstance(raw_candles, Sequence) or isinstance(raw_candles, (str, bytes)):
        raise _MalformedHistoryResponse("TINVEST_HISTORY_RESPONSE_INVALID")
    response_count = len(raw_candles)
    step_seconds = interval_seconds(request.timeframe)
    possible_slots = chunk.range_seconds // step_seconds
    if response_count > chunk.provider_limit or response_count > possible_slots:
        raise _MalformedHistoryResponse(
            "TINVEST_HISTORY_RESPONSE_CARDINALITY_INVALID",
            response_count=response_count,
        )

    authoritative: list[Bar] = []
    provisional: list[Bar] = []
    local_timestamps: set[datetime] = set()
    _request_type, _interval_type, source_type = _sdk_history_contracts()
    exchange_source = getattr(source_type, "CANDLE_SOURCE_EXCHANGE")
    try:
        for raw in raw_candles:
            timestamp = _aware_utc(getattr(raw, "time", None), field="candle_time")
            if timestamp in local_timestamps or timestamp in seen_timestamps:
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_DUPLICATE_TIMESTAMP",
                    response_count=response_count,
                )
            if not chunk.starts_at <= timestamp < chunk.ends_at:
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_TIMESTAMP_OUT_OF_RANGE",
                    response_count=response_count,
                )
            if not timestamp_is_bucket_aligned(timestamp, step_seconds):
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_TIMESTAMP_UNALIGNED",
                    response_count=response_count,
                )
            raw_source = getattr(raw, "candle_source", None)
            if type(raw_source) is not type(exchange_source):
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_SOURCE_INVALID",
                    response_count=response_count,
                )
            if raw_source != exchange_source:
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_SOURCE_NOT_EXCHANGE",
                    response_count=response_count,
                )
            is_complete = getattr(raw, "is_complete", None)
            if type(is_complete) is not bool:
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_FINALITY_INVALID",
                    response_count=response_count,
                )
            volume = getattr(raw, "volume", None)
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                raise _MalformedHistoryResponse(
                    "TINVEST_HISTORY_VOLUME_INVALID",
                    response_count=response_count,
                )
            bar = Bar(
                symbol=request.provider_symbol,
                ts=timestamp,
                open=tinvest_quotation_value(getattr(raw, "open", None)),
                high=tinvest_quotation_value(getattr(raw, "high", None)),
                low=tinvest_quotation_value(getattr(raw, "low", None)),
                close=tinvest_quotation_value(getattr(raw, "close", None)),
                volume=float(volume),
                timeframe=request.timeframe,
                source=_BAR_SOURCE,
                closed=is_complete,
                state=BarState.CONFIRMED if is_complete else BarState.FORMING,
                provenance=provenance,
            )
            if is_complete:
                reject_reason = authoritative_bar_reject_reason(
                    _PROVIDER,
                    bar,
                    instrument_id=request.instrument_id,
                    route_fingerprint=request.route_fingerprint,
                    allowed_request_types=frozenset({BarProviderRequest.HISTORICAL}),
                    provider_contract_id=request.instrument_uid,
                    provider_contract_type=_PROVIDER_CONTRACT_TYPE,
                    data_type=_DATA_TYPE,
                )
                if reject_reason is not None:
                    raise _MalformedHistoryResponse(
                        f"TINVEST_HISTORY_BAR_REJECTED:{reject_reason}",
                        response_count=response_count,
                    )
                authoritative.append(bar)
            else:
                provisional.append(bar)
            local_timestamps.add(timestamp)
    except _MalformedHistoryResponse:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise _MalformedHistoryResponse(
            "TINVEST_HISTORY_CANDLE_INVALID",
            response_count=response_count,
        ) from exc
    seen_timestamps.update(local_timestamps)
    return authoritative, provisional, response_count


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"TINVEST_HISTORY_{field.upper()}_INVALID")
    return value.astimezone(UTC)


def _sdk_history_contracts() -> tuple[Any, Any, Any]:
    from t_tech.invest.grpc import CandleInterval, CandleSource, GetCandlesRequest

    return GetCandlesRequest, CandleInterval, CandleSource


def _build_sdk_request(
    request: TInvestHistoryRequest,
    chunk: TInvestHistoryChunkRequest,
) -> object:
    request_type, interval_type, source_type = _sdk_history_contracts()
    return request_type(
        instrument_id=request.instrument_uid,
        from_=chunk.starts_at,
        to=chunk.ends_at,
        interval=getattr(interval_type, _SDK_INTERVAL_NAME[request.timeframe]),
        candle_source_type=getattr(source_type, "CANDLE_SOURCE_EXCHANGE"),
    )


__all__ = [
    "TInvestHistoryChunkRequest",
    "TInvestHistoryChunkResult",
    "TInvestHistoryRequest",
    "TInvestHistoryResult",
    "TInvestHistoryTerminal",
    "fetch_tinvest_history",
    "plan_tinvest_history_chunks",
    "tinvest_history_request_max_span",
]
