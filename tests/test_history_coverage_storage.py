from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
    HistoryCoverageReceipt,
    current_history_coverage_contract,
    history_coverage_union,
    history_range_is_covered,
    history_uncovered_ranges,
)
from aef_terminal.storage.repos.history_coverage import HistoryCoverageRepoMixin


def _contract(*, request_mode: str = "req_historical_data"):
    return current_history_coverage_contract(
        provider="ibkr",
        instrument_id="ibkr|contract|123",
        route_fingerprint="ibkr|contract|123",
        timeframe="5m",
        request_mode=request_mode,
        request_type="historical",
        provider_source="TRADES",
        provider_contract_id="123",
        provider_contract_type="STK",
        data_type="TRADES",
    )


def _receipt(
    starts_at: datetime,
    ends_at: datetime,
    *,
    contract=None,
    response_count: int = 0,
) -> HistoryCoverageReceipt:
    return HistoryCoverageReceipt(
        contract=contract or _contract(),
        covered_from=starts_at,
        covered_to=ends_at,
        response_count=response_count,
        completed_at=ends_at + timedelta(seconds=1),
    )


def test_current_history_coverage_contract_versions_every_authority_layer() -> None:
    contract = _contract()

    assert HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION == 2
    assert HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION == 2
    assert HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION == 2
    assert contract.receipt_contract_version == HISTORY_COVERAGE_RECEIPT_CONTRACT_VERSION
    assert contract.request_contract_version == HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION
    assert contract.admission_contract_version == HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_type", "keep_up_to_date"),
        ("data_type", "MIDPOINT"),
        ("timeframe", "1h"),
    ],
)
def test_history_coverage_rejects_non_authoritative_contract_fields(
    field: str,
    value: str,
) -> None:
    kwargs = {
        "provider": "ibkr",
        "instrument_id": "ibkr|contract|123",
        "route_fingerprint": "ibkr|contract|123",
        "timeframe": "5m",
        "request_mode": "req_historical_data",
        "request_type": "historical",
        "provider_source": "TRADES",
        "provider_contract_id": "123",
        "provider_contract_type": "STK",
        "data_type": "TRADES",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        current_history_coverage_contract(**kwargs)


def test_history_coverage_requires_fully_elapsed_exact_range() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)

    with pytest.raises(ValueError, match="fully elapsed"):
        HistoryCoverageReceipt(
            contract=_contract(),
            covered_from=start,
            covered_to=end,
            response_count=0,
            completed_at=end - timedelta(seconds=1),
        )


def test_history_coverage_rejects_provider_limit_saturation() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="must not be saturated"):
        HistoryCoverageReceipt(
            contract=_contract(),
            covered_from=start,
            covered_to=start + timedelta(hours=1),
            response_count=300,
            provider_limit=300,
            completed_at=start + timedelta(hours=1, seconds=1),
        )


def test_v18_storage_check_rejects_saturated_history_coverage() -> None:
    from aef_terminal.storage.repos import schema as schema_repo

    assert "response_count < provider_limit" in (
        schema_repo._HISTORY_COVERAGE_COUNT_CHECK_DEFINITION
    )


def test_history_coverage_union_merges_ranges_without_slot_inference() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipts = (
        _receipt(start + timedelta(hours=1), start + timedelta(hours=2)),
        _receipt(start, start + timedelta(hours=1)),
        _receipt(start + timedelta(hours=3), start + timedelta(hours=4)),
    )

    assert history_coverage_union(receipts) == (
        (start, start + timedelta(hours=2)),
        (start + timedelta(hours=3), start + timedelta(hours=4)),
    )
    assert history_range_is_covered(
        receipts,
        starts_at=start,
        ends_at=start + timedelta(hours=2),
    )
    assert not history_range_is_covered(
        receipts,
        starts_at=start,
        ends_at=start + timedelta(hours=4),
    )


def test_history_coverage_union_rejects_mixed_authority_contracts() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="one exact contract"):
        history_coverage_union(
            (
                _receipt(start, start + timedelta(hours=1)),
                _receipt(
                    start + timedelta(hours=1),
                    start + timedelta(hours=2),
                    contract=_contract(request_mode="req_historical_data_adjusted"),
                ),
            )
        )


def test_history_uncovered_ranges_subtracts_clipped_half_open_union() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipts = (
        _receipt(start - timedelta(hours=1), start + timedelta(hours=1)),
        _receipt(start + timedelta(hours=2), start + timedelta(hours=3)),
        _receipt(start + timedelta(hours=3), start + timedelta(hours=4)),
        _receipt(start + timedelta(hours=6), start + timedelta(hours=7)),
    )

    assert history_uncovered_ranges(
        receipts,
        starts_at=start,
        ends_at=start + timedelta(hours=6),
    ) == (
        (start + timedelta(hours=1), start + timedelta(hours=2)),
        (start + timedelta(hours=4), start + timedelta(hours=6)),
    )


def test_history_uncovered_ranges_returns_whole_range_without_receipts() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)

    assert history_uncovered_ranges((), starts_at=start, ends_at=end) == ((start, end),)


def test_history_coverage_batch_write_uses_one_caller_owned_cursor() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipts = (
        _receipt(start, start + timedelta(days=1), response_count=100),
        _receipt(
            start + timedelta(days=1),
            start + timedelta(days=2),
            response_count=90,
        ),
    )

    class Cursor:
        def __init__(self) -> None:
            self.executions: list[tuple[str, tuple]] = []

        def execute(self, statement, params) -> None:
            self.executions.append((str(statement), tuple(params)))

        def fetchone(self):
            return (1,)

    cursor = Cursor()

    assert HistoryCoverageRepoMixin._write_history_coverages(cursor, receipts) == 2
    assert len(cursor.executions) == 2
    assert all("ON CONFLICT DO NOTHING" in statement for statement, _ in cursor.executions)
    assert cursor.executions[0][1][5] == start + timedelta(days=1)
    assert cursor.executions[1][1][4] == start + timedelta(days=1)


def test_authoritative_history_coverage_has_no_public_write_path() -> None:
    assert not hasattr(HistoryCoverageRepoMixin, "write_history_coverages")
    assert not hasattr(HistoryCoverageRepoMixin, "write_history_coverage")


def test_history_coverage_batch_write_is_idempotent_inside_caller_transaction() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipt = _receipt(start, start + timedelta(days=1), response_count=100)

    class Cursor:
        def __init__(self) -> None:
            self.responses = iter(((1,), None))
            self.executions = 0

        def execute(self, _statement, _params) -> None:
            self.executions += 1

        def fetchone(self):
            return next(self.responses)

    cursor = Cursor()

    assert (
        HistoryCoverageRepoMixin._write_history_coverages(
            cursor,
            (receipt, receipt),
        )
        == 1
    )
    assert cursor.executions == 2


def test_contained_receipt_is_not_inserted_or_reported_as_union_expansion() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipt = _receipt(start, start + timedelta(hours=2), response_count=10)

    class Cursor:
        def __init__(self) -> None:
            self.query = ""
            self.executions: list[str] = []

        def execute(self, statement, _params) -> None:
            self.query = str(statement)
            self.executions.append(self.query)

        def fetchall(self):
            assert "FROM history_coverage" in self.query
            return [
                (start, start + timedelta(hours=1), 5, None, start + timedelta(hours=1)),
                (
                    start + timedelta(hours=1),
                    start + timedelta(hours=2),
                    5,
                    None,
                    start + timedelta(hours=2),
                ),
            ]

    cursor = Cursor()

    assert HistoryCoverageRepoMixin._write_history_coverages_if_expanded(
        cursor,
        (receipt,),
    ) == (0, False)
    assert len(cursor.executions) == 2
    assert "pg_advisory_xact_lock" in cursor.executions[0]
    assert all("INSERT INTO history_coverage" not in query for query in cursor.executions)


def test_receipt_that_extends_union_is_inserted_and_reported_as_expansion() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipt = _receipt(start, start + timedelta(hours=2), response_count=10)

    class Cursor:
        def __init__(self) -> None:
            self.query = ""
            self.executions: list[str] = []

        def execute(self, statement, _params) -> None:
            self.query = str(statement)
            self.executions.append(self.query)

        def fetchall(self):
            assert "FROM history_coverage" in self.query
            return [
                (start, start + timedelta(hours=1), 5, None, start + timedelta(hours=1)),
            ]

        def fetchone(self):
            assert "INSERT INTO history_coverage" in self.query
            return (1,)

    cursor = Cursor()

    assert HistoryCoverageRepoMixin._write_history_coverages_if_expanded(
        cursor,
        (receipt,),
    ) == (1, True)
    assert len(cursor.executions) == 3
    assert "INSERT INTO history_coverage" in cursor.executions[-1]


def test_multiple_chunk_receipts_share_one_lock_and_insert_atomically() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipts = (
        _receipt(start, start + timedelta(hours=1), response_count=5),
        _receipt(
            start + timedelta(hours=1),
            start + timedelta(hours=2),
            response_count=6,
        ),
    )

    class Cursor:
        def __init__(self) -> None:
            self.query = ""
            self.executions: list[str] = []

        def execute(self, statement, _params) -> None:
            self.query = str(statement)
            self.executions.append(self.query)

        def fetchall(self):
            assert "FROM history_coverage" in self.query
            return []

        def fetchone(self):
            assert "INSERT INTO history_coverage" in self.query
            return (1,)

    cursor = Cursor()

    assert HistoryCoverageRepoMixin._write_history_coverages_if_expanded(
        cursor,
        receipts,
    ) == (2, True)
    assert sum("pg_advisory_xact_lock" in query for query in cursor.executions) == 1
    assert sum("INSERT INTO history_coverage" in query for query in cursor.executions) == 2


def test_prelocked_coverage_expansion_does_not_reacquire_advisory_lock() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    receipt = _receipt(start, start + timedelta(hours=1), response_count=0)

    class Cursor:
        def __init__(self) -> None:
            self.query = ""
            self.executions: list[str] = []

        def execute(self, statement, _params) -> None:
            self.query = str(statement)
            self.executions.append(self.query)

        def fetchall(self):
            assert "FROM history_coverage" in self.query
            return []

        def fetchone(self):
            assert "INSERT INTO history_coverage" in self.query
            return (1,)

    cursor = Cursor()

    assert HistoryCoverageRepoMixin._write_history_coverages_if_expanded(
        cursor,
        (receipt,),
        coverage_lock_held=True,
    ) == (1, True)
    assert all("pg_advisory_xact_lock" not in query for query in cursor.executions)


def test_all_ranges_for_one_contract_share_one_advisory_lock_identity() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    first = _receipt(start, start + timedelta(hours=1), response_count=0)
    second = _receipt(
        start + timedelta(hours=1),
        start + timedelta(hours=2),
        response_count=1,
    )

    class Cursor:
        def __init__(self) -> None:
            self.execution: tuple[str, tuple] | None = None

        def execute(self, statement, params) -> None:
            self.execution = (str(statement), tuple(params))

    first_cursor = Cursor()
    second_cursor = Cursor()
    HistoryCoverageRepoMixin._lock_history_coverage_contract_on_cursor(
        first_cursor,
        first.contract,
    )
    HistoryCoverageRepoMixin._lock_history_coverage_contract_on_cursor(
        second_cursor,
        second.contract,
    )

    assert first_cursor.execution is not None
    assert second_cursor.execution is not None
    assert "pg_advisory_xact_lock" in first_cursor.execution[0]
    assert first_cursor.execution[1] == second_cursor.execution[1]
