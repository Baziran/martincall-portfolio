from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.provider_contract import CanonicalBarCommitReceipt
from aef_terminal.domain import BarProviderRequest
from aef_terminal.runtime.timeframes import (
    PERSISTED_BAR_INTERVALS,
    require_aware_utc_datetime,
)


HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION = 2
HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION = 2
HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION = 2


def _positive_version(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _non_negative_count(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class HistoryCoverageContract:
    """Exact authority dimensions that make one range receipt eligible."""

    provider: str
    instrument_id: str
    route_fingerprint: str
    timeframe: str
    receipt_contract_version: int
    request_contract_version: int
    admission_contract_version: int
    request_mode: str
    request_type: str
    provider_source: str
    provider_contract_id: str
    provider_contract_type: str
    data_type: str

    def __post_init__(self) -> None:
        for field in (
            "provider",
            "instrument_id",
            "route_fingerprint",
            "timeframe",
            "request_mode",
            "request_type",
            "provider_source",
            "provider_contract_id",
            "provider_contract_type",
            "data_type",
        ):
            object.__setattr__(
                self,
                field,
                require_exact_identity_text(getattr(self, field), field=field),
            )
        if self.timeframe not in PERSISTED_BAR_INTERVALS:
            raise ValueError("history coverage timeframe must be canonical")
        if self.request_type != BarProviderRequest.HISTORICAL.value:
            raise ValueError("history coverage request_type must be historical")
        if self.data_type != "TRADES":
            raise ValueError("history coverage data_type must be TRADES")
        for field in (
            "receipt_contract_version",
            "request_contract_version",
            "admission_contract_version",
        ):
            object.__setattr__(
                self,
                field,
                _positive_version(getattr(self, field), field=field),
            )


@dataclass(frozen=True, slots=True)
class HistoryCoverageReceipt:
    """One admitted provider-complete exact half-open history range."""

    contract: HistoryCoverageContract
    covered_from: datetime
    covered_to: datetime
    response_count: int
    completed_at: datetime
    provider_limit: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.contract, HistoryCoverageContract):
            raise TypeError("contract must be a HistoryCoverageContract")
        covered_from = require_aware_utc_datetime(self.covered_from, field="covered_from")
        covered_to = require_aware_utc_datetime(self.covered_to, field="covered_to")
        completed_at = require_aware_utc_datetime(self.completed_at, field="completed_at")
        if covered_to <= covered_from:
            raise ValueError("history coverage range must be non-empty")
        if covered_to > completed_at:
            raise ValueError("history coverage range must be fully elapsed")
        object.__setattr__(self, "covered_from", covered_from)
        object.__setattr__(self, "covered_to", covered_to)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(
            self,
            "response_count",
            _non_negative_count(self.response_count, field="response_count"),
        )
        if self.provider_limit is not None:
            object.__setattr__(
                self,
                "provider_limit",
                _positive_version(self.provider_limit, field="provider_limit"),
            )
            if self.response_count >= self.provider_limit:
                raise ValueError("history coverage response must not be saturated")


@dataclass(frozen=True, slots=True)
class CanonicalHistoryCommitReceipt:
    """Atomic outcome for canonical bars and exact chunk coverage expansion."""

    bar_receipt: CanonicalBarCommitReceipt
    coverage_inserted: int
    coverage_expanded: bool
    mutated: bool = dataclass_field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bar_receipt, CanonicalBarCommitReceipt):
            raise TypeError("bar_receipt must be a CanonicalBarCommitReceipt")
        if (
            isinstance(self.coverage_inserted, bool)
            or not isinstance(self.coverage_inserted, int)
            or self.coverage_inserted < 0
        ):
            raise TypeError("coverage_inserted must be a non-negative integer")
        if not isinstance(self.coverage_expanded, bool):
            raise TypeError("coverage_expanded must be bool")
        if self.coverage_expanded and not self.coverage_inserted:
            raise ValueError("coverage expansion requires inserted receipts")
        object.__setattr__(
            self,
            "mutated",
            bool(self.bar_receipt.written or self.bar_receipt.deleted or self.coverage_expanded),
        )


def current_history_coverage_contract(
    *,
    provider: str,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
    request_mode: str,
    request_type: str,
    provider_source: str,
    provider_contract_id: str,
    provider_contract_type: str,
    data_type: str,
) -> HistoryCoverageContract:
    """Build the current exact receipt/request/admission contract identity."""

    return HistoryCoverageContract(
        provider=provider,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
        receipt_contract_version=HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION,
        request_contract_version=HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
        admission_contract_version=HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
        request_mode=request_mode,
        request_type=request_type,
        provider_source=provider_source,
        provider_contract_id=provider_contract_id,
        provider_contract_type=provider_contract_type,
        data_type=data_type,
    )


def history_coverage_union(
    receipts: Sequence[HistoryCoverageReceipt],
) -> tuple[tuple[datetime, datetime], ...]:
    """Merge overlapping or adjacent receipts without inventing intermediate slots."""

    if not receipts:
        return ()
    contract = receipts[0].contract
    if any(receipt.contract != contract for receipt in receipts):
        raise ValueError("history coverage union requires one exact contract")
    merged: list[tuple[datetime, datetime]] = []
    for receipt in sorted(receipts, key=lambda item: (item.covered_from, item.covered_to)):
        if not merged or receipt.covered_from > merged[-1][1]:
            merged.append((receipt.covered_from, receipt.covered_to))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], receipt.covered_to))
    return tuple(merged)


def history_range_is_covered(
    receipts: Sequence[HistoryCoverageReceipt],
    *,
    starts_at: datetime,
    ends_at: datetime,
) -> bool:
    """Return whether the eligible receipt union covers one exact half-open range."""

    start = require_aware_utc_datetime(starts_at, field="starts_at")
    end = require_aware_utc_datetime(ends_at, field="ends_at")
    if end <= start:
        raise ValueError("history coverage query range must be non-empty")
    return any(
        covered_from <= start and covered_to >= end
        for covered_from, covered_to in history_coverage_union(receipts)
    )


def history_uncovered_ranges(
    receipts: Sequence[HistoryCoverageReceipt],
    *,
    starts_at: datetime,
    ends_at: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    """Subtract the eligible receipt union from one exact half-open range."""

    start = require_aware_utc_datetime(starts_at, field="starts_at")
    end = require_aware_utc_datetime(ends_at, field="ends_at")
    if end <= start:
        raise ValueError("history coverage query range must be non-empty")
    cursor = start
    uncovered: list[tuple[datetime, datetime]] = []
    for covered_from, covered_to in history_coverage_union(receipts):
        if covered_to <= cursor:
            continue
        if covered_from >= end:
            break
        clipped_from = max(covered_from, start)
        clipped_to = min(covered_to, end)
        if clipped_from > cursor:
            uncovered.append((cursor, clipped_from))
        cursor = max(cursor, clipped_to)
        if cursor >= end:
            break
    if cursor < end:
        uncovered.append((cursor, end))
    return tuple(uncovered)
