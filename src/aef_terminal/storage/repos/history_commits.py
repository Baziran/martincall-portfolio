from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import (
    instrument_is_exact_futures_contract,
    instrument_is_futures,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryStorageKind,
    HistoryRequestAdmissionIdentity,
)
from aef_terminal.data.providers import provider_key_for_source, route_instrument
from aef_terminal.domain import Bar, BarProviderRequest
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
    CanonicalHistoryCommitReceipt,
    HistoryCoverageReceipt,
)


def _typed_bars(bars: Sequence[Bar]) -> tuple[Bar, ...]:
    if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence):
        raise TypeError("bars must be a typed sequence")
    typed = tuple(bars)
    if any(not isinstance(bar, Bar) for bar in typed):
        raise TypeError("bars must contain Bar values")
    return typed


def _typed_coverage_receipts(
    receipts: Sequence[HistoryCoverageReceipt],
) -> tuple[HistoryCoverageReceipt, ...]:
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
        raise TypeError("coverage_receipts must be a typed sequence")
    typed = tuple(receipts)
    if any(not isinstance(receipt, HistoryCoverageReceipt) for receipt in typed):
        raise TypeError("coverage_receipts must contain HistoryCoverageReceipt values")
    return typed


def _validate_coverage_receipts(
    receipts: Sequence[HistoryCoverageReceipt],
    *,
    route: Any,
    bars: Sequence[Bar],
) -> None:
    if not receipts:
        return
    contract = receipts[0].contract
    if any(receipt.contract != contract for receipt in receipts):
        raise ValueError("HISTORY_COMMIT_COVERAGE_CONTRACT_MISMATCH")
    if (
        contract.receipt_contract_version != HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION
        or contract.request_contract_version != HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION
        or contract.admission_contract_version != HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION
    ):
        raise ValueError("HISTORY_COMMIT_COVERAGE_CONTRACT_VERSION_MISMATCH")
    if (
        getattr(
            route.adapter.capabilities,
            "exact_history_snapshot_authority",
            False,
        )
        is not True
    ):
        raise ValueError("HISTORY_COMMIT_SNAPSHOT_AUTHORITY_REQUIRED")
    ordered = tuple(sorted(receipts, key=lambda item: (item.covered_from, item.covered_to)))
    if any(
        previous.covered_to > current.covered_from
        for previous, current in zip(ordered, ordered[1:])
    ):
        raise ValueError("HISTORY_COMMIT_COVERAGE_CHUNKS_OVERLAP")
    expected = (
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    actual = (
        contract.provider,
        contract.instrument_id,
        contract.route_fingerprint,
    )
    if actual != expected:
        raise ValueError("HISTORY_COMMIT_COVERAGE_ROUTE_MISMATCH")
    request_identity = route.adapter.history_request_identity(route.instrument)
    if not isinstance(request_identity, HistoryRequestAdmissionIdentity):
        raise TypeError("HISTORY_COMMIT_TYPED_REQUEST_IDENTITY_REQUIRED")
    expected_request_identity = (
        request_identity.request_contract_version,
        request_identity.admission_contract_version,
        request_identity.request_mode,
        request_identity.request_type.value,
        request_identity.provider_source,
        request_identity.provider_contract_id,
        request_identity.provider_contract_type,
        request_identity.data_type,
    )
    receipt_request_identity = (
        contract.request_contract_version,
        contract.admission_contract_version,
        contract.request_mode,
        contract.request_type,
        contract.provider_source,
        contract.provider_contract_id,
        contract.provider_contract_type,
        contract.data_type,
    )
    if receipt_request_identity != expected_request_identity:
        raise ValueError("HISTORY_COMMIT_COVERAGE_REQUEST_IDENTITY_MISMATCH")
    for bar in bars:
        if bar.timeframe != contract.timeframe:
            raise ValueError("HISTORY_COMMIT_COVERAGE_TIMEFRAME_MISMATCH")
        provenance = bar.provenance
        if provenance is None:
            raise ValueError("HISTORY_COMMIT_BAR_PROVENANCE_REQUIRED")
        if provenance.request_type is not BarProviderRequest.HISTORICAL:
            raise ValueError("HISTORY_COMMIT_BAR_REQUEST_TYPE_MISMATCH")
        if provenance.provider_contract_type != contract.provider_contract_type:
            raise ValueError("HISTORY_COMMIT_BAR_CONTRACT_TYPE_MISMATCH")
    admitted_count = 0
    for receipt in ordered:
        chunk_count = sum(receipt.covered_from <= bar.ts < receipt.covered_to for bar in bars)
        if receipt.response_count != chunk_count:
            raise ValueError("HISTORY_COMMIT_COVERAGE_RESPONSE_COUNT_MISMATCH")
        admitted_count += chunk_count
    if admitted_count != len(bars):
        raise ValueError("HISTORY_COMMIT_BAR_OUTSIDE_COVERAGE_RANGES")


def _snapshot_bar_values(bar: Bar) -> tuple[datetime, float, float, float, float, float]:
    return (
        bar.ts.astimezone(UTC),
        float(bar.open),
        float(bar.high),
        float(bar.low),
        float(bar.close),
        float(bar.volume),
    )


def _persisted_snapshot_values(
    row: Sequence[Any],
) -> tuple[datetime, float, float, float, float, float]:
    return (
        row[0].astimezone(UTC),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
    )


class HistoryCommitsRepoMixin:
    """Atomic canonical history bars plus exact eligible coverage expansion."""

    @staticmethod
    def _reconcile_exact_history_snapshot_on_cursor(
        cur: Any,
        bars: Sequence[Bar],
        receipts: Sequence[HistoryCoverageReceipt],
        *,
        route: Any,
        contract_key: str | None,
        revision_sequence: int,
    ) -> int:
        """Replace each proven exact range and verify its complete OHLCV snapshot."""

        ordered_receipts = tuple(
            sorted(receipts, key=lambda item: (item.covered_from, item.covered_to))
        )
        deleted = 0
        for receipt in ordered_receipts:
            expected = tuple(
                sorted(
                    (
                        _snapshot_bar_values(bar)
                        for bar in bars
                        if receipt.covered_from <= bar.ts < receipt.covered_to
                    ),
                    key=lambda item: item[0],
                )
            )
            expected_timestamps = [item[0] for item in expected]
            if contract_key is None:
                cur.execute(
                    """
                    DELETE FROM bars
                    WHERE provider = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                      AND ts >= %s
                      AND ts < %s
                      AND revision_sequence <= %s
                      AND NOT (ts = ANY(%s::timestamptz[]))
                    """,
                    (
                        route.provider,
                        route.instrument_id,
                        route.fingerprint,
                        receipt.contract.timeframe,
                        receipt.covered_from,
                        receipt.covered_to,
                        revision_sequence,
                        expected_timestamps,
                    ),
                )
            else:
                cur.execute(
                    """
                    DELETE FROM futures_contract_bars
                    WHERE provider = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND contract_key = %s
                      AND timeframe = %s
                      AND ts >= %s
                      AND ts < %s
                      AND revision_sequence <= %s
                      AND NOT (ts = ANY(%s::timestamptz[]))
                    """,
                    (
                        route.provider,
                        route.instrument_id,
                        route.fingerprint,
                        contract_key,
                        receipt.contract.timeframe,
                        receipt.covered_from,
                        receipt.covered_to,
                        revision_sequence,
                        expected_timestamps,
                    ),
                )
            deleted += max(int(cur.rowcount), 0)

            if contract_key is None:
                cur.execute(
                    """
                    SELECT ts, open, high, low, close, volume
                    FROM bars
                    WHERE provider = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND timeframe = %s
                      AND ts >= %s
                      AND ts < %s
                    ORDER BY ts
                    """,
                    (
                        route.provider,
                        route.instrument_id,
                        route.fingerprint,
                        receipt.contract.timeframe,
                        receipt.covered_from,
                        receipt.covered_to,
                    ),
                )
            else:
                cur.execute(
                    """
                    SELECT ts, open, high, low, close, volume
                    FROM futures_contract_bars
                    WHERE provider = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND contract_key = %s
                      AND timeframe = %s
                      AND ts >= %s
                      AND ts < %s
                    ORDER BY ts
                    """,
                    (
                        route.provider,
                        route.instrument_id,
                        route.fingerprint,
                        contract_key,
                        receipt.contract.timeframe,
                        receipt.covered_from,
                        receipt.covered_to,
                    ),
                )
            persisted = tuple(_persisted_snapshot_values(row) for row in cur.fetchall())
            if persisted != expected:
                raise RuntimeError("HISTORY_COMMIT_EXACT_SNAPSHOT_MISMATCH")
        return deleted

    def commit_history_result(
        self,
        bars: Sequence[Bar],
        *,
        provider: str,
        instrument: dict[str, Any],
        revision_sequence: int,
        coverage_receipts: Sequence[HistoryCoverageReceipt] = (),
    ) -> CanonicalHistoryCommitReceipt:
        self._require_canonical_writer_lease()
        if (
            isinstance(revision_sequence, bool)
            or not isinstance(revision_sequence, int)
            or revision_sequence <= 0
        ):
            raise ValueError("revision_sequence must be positive")
        commit_sequence = revision_sequence
        typed_bars = _typed_bars(bars)
        typed_coverages = _typed_coverage_receipts(coverage_receipts)
        provider_key = provider_key_for_source(provider)
        route = route_instrument(instrument, expected_source=provider_key)
        history_route = route.adapter.canonical_history_route(route.instrument)
        exact_futures = history_route is not None
        if exact_futures:
            if (
                history_route.storage_kind is not CanonicalHistoryStorageKind.FUTURES_CONTRACT
                or not instrument_is_exact_futures_contract(route.instrument)
            ):
                raise ValueError("HISTORY_COMMIT_EXACT_FUTURES_ROUTE_REQUIRED")
            if history_route.contract_key != route.adapter.session_contract_id(route.instrument):
                raise ValueError("HISTORY_COMMIT_EXACT_CONTRACT_ROUTE_MISMATCH")
        elif instrument_is_futures(route.instrument):
            raise ValueError("HISTORY_COMMIT_FUTURES_STORAGE_ROUTE_REQUIRED")

        _validate_coverage_receipts(
            typed_coverages,
            route=route,
            bars=typed_bars,
        )

        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if typed_coverages:
                    self._lock_history_coverage_contract_on_cursor(
                        cur,
                        typed_coverages[0].contract,
                    )
                if exact_futures:
                    if typed_bars:
                        first_provenance = typed_bars[0].provenance
                        if first_provenance is None:
                            raise ValueError("HISTORY_COMMIT_BAR_PROVENANCE_REQUIRED")
                        provider_contract_type = (
                            typed_coverages[0].contract.provider_contract_type
                            if typed_coverages
                            else first_provenance.provider_contract_type
                        )
                        provider_contract_id = route.adapter.session_contract_id(route.instrument)
                        data_type = route.adapter.bar_data_type(route.instrument)
                        bar_receipt = self._write_futures_contract_bars_on_cursor(
                            cur,
                            typed_bars,
                            provider=route.provider,
                            instrument_id=route.instrument_id,
                            route_fingerprint=route.fingerprint,
                            contract_key=history_route.contract_key,
                            provider_contract_id=provider_contract_id,
                            provider_contract_type=provider_contract_type,
                            data_type=data_type,
                            revision_sequence=commit_sequence,
                        )
                    else:
                        bar_receipt = CanonicalBarCommitReceipt(revision_sequence=commit_sequence)
                else:
                    bar_receipt = self._write_bars_on_cursor(
                        cur,
                        typed_bars,
                        route.provider,
                        instrument=route.instrument,
                        revision_sequence=commit_sequence,
                    )
                if bar_receipt.rejected:
                    raise ValueError("HISTORY_COMMIT_REJECTED_BARS")
                if typed_coverages:
                    deleted = self._reconcile_exact_history_snapshot_on_cursor(
                        cur,
                        typed_bars,
                        typed_coverages,
                        route=route,
                        contract_key=(history_route.contract_key if exact_futures else None),
                        revision_sequence=commit_sequence,
                    )
                    if deleted:
                        bar_receipt = replace(
                            bar_receipt,
                            deleted=bar_receipt.deleted + deleted,
                        )
                coverage_inserted = 0
                coverage_expanded = False
                if typed_coverages:
                    (
                        coverage_inserted,
                        coverage_expanded,
                    ) = self._write_history_coverages_if_expanded(
                        cur,
                        typed_coverages,
                        coverage_lock_held=True,
                    )
                commit_receipt = CanonicalHistoryCommitReceipt(
                    bar_receipt=bar_receipt,
                    coverage_inserted=coverage_inserted,
                    coverage_expanded=coverage_expanded,
                )
        if exact_futures:
            self._finalize_futures_contract_bars_commit(bar_receipt)
        else:
            self._finalize_bars_commit(bar_receipt, provider=route.provider)
        return commit_receipt
