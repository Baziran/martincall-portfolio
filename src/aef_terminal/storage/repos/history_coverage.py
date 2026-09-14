from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aef_terminal.data.providers import provider_key_for_source
from aef_terminal.data.history_coverage import (
    HistoryCoverageContract,
    HistoryCoverageReceipt,
    history_range_is_covered,
)


def _require_registered_provider(contract: HistoryCoverageContract) -> None:
    if provider_key_for_source(contract.provider) != contract.provider:
        raise ValueError(f"HISTORY_COVERAGE_PROVIDER_NOT_CANONICAL provider={contract.provider}")


def _receipt_from_row(
    contract: HistoryCoverageContract,
    row: tuple[Any, ...],
) -> HistoryCoverageReceipt:
    return HistoryCoverageReceipt(
        contract=contract,
        covered_from=row[0],
        covered_to=row[1],
        response_count=int(row[2]),
        provider_limit=int(row[3]) if row[3] is not None else None,
        completed_at=row[4],
    )


def _contract_identity_values(
    contract: HistoryCoverageContract,
) -> tuple[Any, ...]:
    return (
        contract.provider,
        contract.instrument_id,
        contract.route_fingerprint,
        contract.timeframe,
        contract.receipt_contract_version,
        contract.request_contract_version,
        contract.admission_contract_version,
        contract.request_mode,
        contract.request_type,
        contract.provider_source,
        contract.provider_contract_id,
        contract.provider_contract_type,
        contract.data_type,
    )


class HistoryCoverageRepoMixin:
    """PostgreSQL repository for admitted complete provider-range receipts."""

    @staticmethod
    def _lock_history_coverage_contract_on_cursor(
        cur: Any,
        contract: HistoryCoverageContract,
    ) -> None:
        """Serialize one exact authority contract for the caller's transaction."""

        if not isinstance(contract, HistoryCoverageContract):
            raise TypeError("contract must be a HistoryCoverageContract")
        _require_registered_provider(contract)
        lock_identity = json.dumps(
            _contract_identity_values(contract),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (lock_identity,),
        )

    @staticmethod
    def _write_history_coverages(
        cur: Any,
        receipts: Sequence[HistoryCoverageReceipt],
    ) -> int:
        """Insert one admitted sequence on a caller-owned transaction cursor."""

        if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
            raise TypeError("receipts must be a typed sequence")
        typed_receipts = tuple(receipts)
        if any(not isinstance(receipt, HistoryCoverageReceipt) for receipt in typed_receipts):
            raise TypeError("receipts must contain HistoryCoverageReceipt values")
        for receipt in typed_receipts:
            _require_registered_provider(receipt.contract)
        inserted = 0
        for receipt in typed_receipts:
            contract = receipt.contract
            cur.execute(
                """
                INSERT INTO history_coverage (
                    provider, instrument_id, route_fingerprint, timeframe,
                    covered_from, covered_to,
                    receipt_contract_version, request_contract_version,
                    admission_contract_version, request_mode, request_type,
                    provider_source, provider_contract_id, provider_contract_type,
                    data_type, provider_limit, response_count, completed_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING 1
                """,
                (
                    contract.provider,
                    contract.instrument_id,
                    contract.route_fingerprint,
                    contract.timeframe,
                    receipt.covered_from,
                    receipt.covered_to,
                    contract.receipt_contract_version,
                    contract.request_contract_version,
                    contract.admission_contract_version,
                    contract.request_mode,
                    contract.request_type,
                    contract.provider_source,
                    contract.provider_contract_id,
                    contract.provider_contract_type,
                    contract.data_type,
                    receipt.provider_limit,
                    receipt.response_count,
                    receipt.completed_at,
                ),
            )
            inserted += int(cur.fetchone() is not None)
        return inserted

    @staticmethod
    def _read_history_coverages_on_cursor(
        cur: Any,
        contract: HistoryCoverageContract,
        *,
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[HistoryCoverageReceipt, ...]:
        if not isinstance(contract, HistoryCoverageContract):
            raise TypeError("contract must be a HistoryCoverageContract")
        _require_registered_provider(contract)
        probe = HistoryCoverageReceipt(
            contract=contract,
            covered_from=starts_at,
            covered_to=ends_at,
            response_count=0,
            completed_at=ends_at,
        )
        cur.execute(
            """
            SELECT covered_from, covered_to, response_count,
                   provider_limit, completed_at
            FROM history_coverage
            WHERE provider = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND timeframe = %s
              AND receipt_contract_version = %s
              AND request_contract_version = %s
              AND admission_contract_version = %s
              AND request_mode = %s
              AND request_type = %s
              AND provider_source = %s
              AND provider_contract_id = %s
              AND provider_contract_type = %s
              AND data_type = %s
              AND covered_to > %s
              AND covered_from < %s
            ORDER BY covered_from, covered_to
            """,
            (
                *_contract_identity_values(contract),
                probe.covered_from,
                probe.covered_to,
            ),
        )
        return tuple(_receipt_from_row(contract, row) for row in cur.fetchall())

    @classmethod
    def _write_history_coverages_if_expanded(
        cls,
        cur: Any,
        receipts: Sequence[HistoryCoverageReceipt],
        *,
        coverage_lock_held: bool = False,
    ) -> tuple[int, bool]:
        """Insert exact chunk receipts only where they expand one eligible union."""

        if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
            raise TypeError("receipts must be a typed sequence")
        typed_receipts = tuple(receipts)
        if not typed_receipts:
            return 0, False
        if any(not isinstance(receipt, HistoryCoverageReceipt) for receipt in typed_receipts):
            raise TypeError("receipts must contain HistoryCoverageReceipt values")
        if not isinstance(coverage_lock_held, bool):
            raise TypeError("coverage_lock_held must be bool")
        contract = typed_receipts[0].contract
        if any(receipt.contract != contract for receipt in typed_receipts):
            raise ValueError("coverage expansion requires one exact contract")
        ordered = tuple(
            sorted(typed_receipts, key=lambda item: (item.covered_from, item.covered_to))
        )
        if any(
            previous.covered_to > current.covered_from
            for previous, current in zip(ordered, ordered[1:])
        ):
            raise ValueError("coverage chunk receipts must not overlap")
        if not coverage_lock_held:
            cls._lock_history_coverage_contract_on_cursor(cur, contract)
        existing = cls._read_history_coverages_on_cursor(
            cur,
            contract,
            starts_at=ordered[0].covered_from,
            ends_at=ordered[-1].covered_to,
        )
        eligible = list(existing)
        inserted = 0
        for receipt in ordered:
            if history_range_is_covered(
                eligible,
                starts_at=receipt.covered_from,
                ends_at=receipt.covered_to,
            ):
                continue
            if cls._write_history_coverages(cur, (receipt,)) != 1:
                raise RuntimeError("HISTORY_COVERAGE_EXPANSION_NOT_DURABLE")
            eligible.append(receipt)
            inserted += 1
        return inserted, inserted > 0

    def read_history_coverage(
        self,
        contract: HistoryCoverageContract,
        *,
        starts_at: datetime,
        ends_at: datetime,
    ) -> tuple[HistoryCoverageReceipt, ...]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                return self._read_history_coverages_on_cursor(
                    cur,
                    contract,
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
