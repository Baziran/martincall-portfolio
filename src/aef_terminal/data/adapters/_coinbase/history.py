from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason
from aef_terminal.runtime.timeframes import interval_seconds, timestamp_is_bucket_aligned


_PROVIDER = "coinbase"
_PROVIDER_SOURCE = "EXCHANGE"
_PROVIDER_CONTRACT_TYPE = "PRODUCT"
_DATA_TYPE = "TRADES"
_PROVIDER_RESPONSE_LIMIT = 300
_MAX_TARGET_BUCKETS = _PROVIDER_RESPONSE_LIMIT - 1


class CoinbaseHistoryTerminal(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class CoinbaseHistoryRequest:
    product_id: str
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    timeframe: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        for field in (
            "product_id",
            "instrument_id",
            "route_fingerprint",
            "provider_symbol",
        ):
            object.__setattr__(
                self,
                field,
                require_exact_identity_text(getattr(self, field), field=field),
            )
        expected_id = f"{_PROVIDER}|contract|{self.product_id}"
        if self.instrument_id != expected_id:
            raise ValueError("COINBASE_HISTORY_INSTRUMENT_ID_MISMATCH")
        if self.route_fingerprint != self.instrument_id:
            raise ValueError("COINBASE_HISTORY_ROUTE_FINGERPRINT_MISMATCH")
        if self.provider_symbol != self.product_id:
            raise ValueError("COINBASE_HISTORY_PROVIDER_SYMBOL_MISMATCH")
        step_seconds = interval_seconds(self.timeframe)
        starts_at = _aware_utc(self.starts_at, field="starts_at")
        ends_at = _aware_utc(self.ends_at, field="ends_at")
        if (
            not timestamp_is_bucket_aligned(starts_at, step_seconds)
            or not timestamp_is_bucket_aligned(ends_at, step_seconds)
            or ends_at <= starts_at
        ):
            raise ValueError("COINBASE_HISTORY_RANGE_INVALID")
        if ends_at - starts_at > coinbase_history_request_max_span(self.timeframe):
            raise ValueError("COINBASE_HISTORY_RANGE_TOO_LARGE")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)


@dataclass(frozen=True, slots=True)
class CoinbaseHistoryResult:
    request: CoinbaseHistoryRequest
    terminal: CoinbaseHistoryTerminal
    authoritative_bars: tuple[Bar, ...]
    provisional_bars: tuple[Bar, ...]
    response_count: int | None
    provider_source: str = _PROVIDER_SOURCE
    provider_limit: int = _PROVIDER_RESPONSE_LIMIT
    error_code: str | None = None


class _MalformedCoinbaseHistory(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def coinbase_history_request_max_span(timeframe: str) -> timedelta:
    return timedelta(seconds=_MAX_TARGET_BUCKETS * interval_seconds(timeframe))


def fetch_coinbase_history(
    request: CoinbaseHistoryRequest,
    *,
    timeout: float,
) -> CoinbaseHistoryResult:
    """Fetch one provider-bounded target and mirror its terminal response."""

    return _fetch_coinbase_range(
        request,
        timeout=timeout,
        confirmed_before=request.ends_at,
    )


def fetch_coinbase_chart_tail(
    request: CoinbaseHistoryRequest,
    *,
    timeout: float,
    confirmed_before: datetime,
) -> CoinbaseHistoryResult:
    """Fetch one live tail while keeping the active provider bucket provisional."""

    finality_cutoff = _aware_utc(confirmed_before, field="confirmed_before")
    if (
        not request.starts_at <= finality_cutoff < request.ends_at
        or not timestamp_is_bucket_aligned(
            finality_cutoff,
            interval_seconds(request.timeframe),
        )
    ):
        raise ValueError("COINBASE_CHART_TAIL_FINALITY_CUTOFF_INVALID")
    return _fetch_coinbase_range(
        request,
        timeout=timeout,
        confirmed_before=finality_cutoff,
    )


def _fetch_coinbase_range(
    request: CoinbaseHistoryRequest,
    *,
    timeout: float,
    confirmed_before: datetime,
) -> CoinbaseHistoryResult:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("COINBASE_HISTORY_TIMEOUT_INVALID")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("COINBASE_HISTORY_TIMEOUT_INVALID")
    deadline = time.monotonic() + timeout_seconds

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("COINBASE_HISTORY_TIMEOUT")
        return remaining

    provenance = BarProvenance(
        provider=_PROVIDER,
        instrument_id=request.instrument_id,
        route_fingerprint=request.route_fingerprint,
        request_type=BarProviderRequest.HISTORICAL,
        provider_contract_id=request.product_id,
        provider_contract_type=_PROVIDER_CONTRACT_TYPE,
        data_type=_DATA_TYPE,
    )
    try:
        target_payload = _request_candles(
            request.product_id,
            starts_at=request.starts_at,
            ends_at=request.ends_at,
            timeframe=request.timeframe,
            timeout=remaining_timeout(),
        )
        authoritative_bars, provisional_bars = _parse_candles(
            target_payload,
            request=request,
            starts_at=request.starts_at,
            ends_at=request.ends_at,
            provenance=provenance,
            confirmed_before=confirmed_before,
        )
    except _MalformedCoinbaseHistory as exc:
        return CoinbaseHistoryResult(
            request=request,
            terminal=CoinbaseHistoryTerminal.MALFORMED,
            authoritative_bars=(),
            provisional_bars=(),
            response_count=None,
            error_code=exc.code,
        )
    response_count = len(authoritative_bars) + len(provisional_bars)
    return CoinbaseHistoryResult(
        request=request,
        terminal=(
            CoinbaseHistoryTerminal.INCOMPLETE
            if provisional_bars
            else CoinbaseHistoryTerminal.COMPLETE
        ),
        authoritative_bars=tuple(authoritative_bars),
        provisional_bars=tuple(provisional_bars),
        response_count=response_count,
    )


def _request_candles(
    product_id: str,
    *,
    starts_at: datetime,
    ends_at: datetime,
    timeframe: str,
    timeout: float,
) -> object:
    if ends_at <= starts_at:
        raise _MalformedCoinbaseHistory("COINBASE_HISTORY_PROVIDER_RANGE_INVALID")
    params = urlencode(
        {
            "start": _provider_timestamp(starts_at),
            "end": _provider_timestamp(ends_at - timedelta(microseconds=1)),
            "granularity": str(interval_seconds(timeframe)),
        }
    )
    request = Request(
        f"https://api.exchange.coinbase.com/products/{quote(product_id)}/candles?{params}",
        headers={
            "User-Agent": "AEF-Python-Terminal/0.1",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_candles(
    payload: object,
    *,
    request: CoinbaseHistoryRequest,
    starts_at: datetime,
    ends_at: datetime,
    provenance: BarProvenance,
    confirmed_before: datetime,
) -> tuple[list[Bar], list[Bar]]:
    if not isinstance(payload, list):
        raise _MalformedCoinbaseHistory("COINBASE_HISTORY_RESPONSE_INVALID")
    response_count = len(payload)
    step_seconds = interval_seconds(request.timeframe)
    possible_slots = int((ends_at - starts_at).total_seconds()) // step_seconds
    if response_count >= _PROVIDER_RESPONSE_LIMIT or response_count > possible_slots:
        raise _MalformedCoinbaseHistory("COINBASE_HISTORY_RESPONSE_CARDINALITY_INVALID")
    authoritative: list[Bar] = []
    provisional: list[Bar] = []
    seen: set[datetime] = set()
    for raw in payload:
        if not isinstance(raw, list) or len(raw) < 6:
            raise _MalformedCoinbaseHistory("COINBASE_HISTORY_CANDLE_INVALID")
        try:
            raw_timestamp = float(raw[0])
            if not math.isfinite(raw_timestamp) or not raw_timestamp.is_integer():
                raise ValueError
            ts = datetime.fromtimestamp(int(raw_timestamp), tz=UTC)
            low = float(raw[1])
            high = float(raw[2])
            open_ = float(raw[3])
            close = float(raw[4])
            volume = float(raw[5])
        except (OSError, OverflowError, TypeError, ValueError) as exc:
            raise _MalformedCoinbaseHistory("COINBASE_HISTORY_CANDLE_NUMERIC_INVALID") from exc
        if (
            not starts_at <= ts < ends_at
            or not timestamp_is_bucket_aligned(ts, step_seconds)
            or ts in seen
        ):
            raise _MalformedCoinbaseHistory("COINBASE_HISTORY_CANDLE_IDENTITY_INVALID")
        values = (open_, high, low, close, volume)
        if (
            any(not math.isfinite(value) for value in values)
            or min(open_, high, low, close) <= 0
            or high < max(open_, close, low)
            or low > min(open_, close, high)
            or volume < 0
        ):
            raise _MalformedCoinbaseHistory("COINBASE_HISTORY_OHLCV_INVALID")
        confirmed = ts < confirmed_before
        bar = Bar(
            symbol=request.provider_symbol,
            ts=ts,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            timeframe=request.timeframe,
            source=f"coinbase:{request.product_id}",
            closed=confirmed,
            state=BarState.CONFIRMED if confirmed else BarState.FORMING,
            provenance=provenance,
        )
        if confirmed:
            reject_reason = authoritative_bar_reject_reason(
                _PROVIDER,
                bar,
                instrument_id=request.instrument_id,
                route_fingerprint=request.route_fingerprint,
                data_type=_DATA_TYPE,
            )
            if reject_reason is not None:
                raise _MalformedCoinbaseHistory(f"COINBASE_HISTORY_CANDLE_REJECTED_{reject_reason}")
            authoritative.append(bar)
        else:
            provisional.append(bar)
        seen.add(ts)
    return (
        sorted(authoritative, key=lambda item: item.ts),
        sorted(provisional, key=lambda item: item.ts),
    )


def _provider_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"COINBASE_HISTORY_{field.upper()}_INVALID")
    return value.astimezone(UTC)
