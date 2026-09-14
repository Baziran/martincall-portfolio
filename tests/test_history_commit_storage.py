from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aef_terminal.data.adapters._tinvest.qualification import (
    TInvestInstrumentCandidate,
    tinvest_candidate_payload,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryRoute,
    HistoryRequestAdmissionIdentity,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar, BarProvenance, BarProviderRequest
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
    CanonicalHistoryCommitReceipt,
    HistoryCoverageReceipt,
    current_history_coverage_contract,
)
from aef_terminal.storage.repos import bars as bars_module
from aef_terminal.storage.repos import history_commits as history_commits_module
from aef_terminal.storage.repos.bars import BarsRepoMixin
from aef_terminal.storage.repos.futures import FuturesRepoMixin
from aef_terminal.storage.repos.history_commits import HistoryCommitsRepoMixin
from tests.provider_payloads import coinbase_btc_payload, ibkr_stock_payload


def _tinvest_stock_payload() -> dict:
    return tinvest_candidate_payload(
        TInvestInstrumentCandidate(
            instrument_uid="stock-uid",
            position_uid="position:stock-uid",
            figi="figi:stock-uid",
            ticker="SBER",
            class_code="TQBR",
            instrument_type="share",
            instrument_kind="INSTRUMENT_TYPE_SHARE",
            asset_class="stock",
            name="SBER",
            api_trade_available=True,
            for_qualified_investor=False,
            weekend_trading_available=False,
            lot=1,
            first_1m_candle_at=datetime(2026, 1, 1, tzinfo=UTC),
            first_1d_candle_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )


def _bar(
    *,
    provider: str,
    instrument_id: str,
    fingerprint: str,
    provider_contract_id: str,
    provider_contract_type: str,
    ts: datetime,
) -> Bar:
    return Bar(
        symbol="SPY",
        ts=ts,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        timeframe="5m",
        source=f"{provider}:history",
        closed=True,
        provenance=BarProvenance(
            provider=provider,
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            request_type=BarProviderRequest.HISTORICAL,
            provider_contract_id=provider_contract_id,
            provider_contract_type=provider_contract_type,
            data_type="TRADES",
        ),
    )


def _coverage(
    *,
    provider: str,
    instrument_id: str,
    fingerprint: str,
    provider_contract_id: str,
    provider_contract_type: str,
    starts_at: datetime,
    ends_at: datetime,
    response_count: int,
    request_mode: str | None = None,
    provider_source: str = "IBKR_HISTORICAL",
) -> HistoryCoverageReceipt:
    exact_request_mode = request_mode or json.dumps(
        {
            "currency": "USD",
            "exchange": "SMART",
            "mode": "req_historical_data",
            "primary_exchange": "",
            "range_normalization": "filter_valid_provider_overrun_v1",
            "use_rth": False,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return HistoryCoverageReceipt(
        contract=current_history_coverage_contract(
            provider=provider,
            instrument_id=instrument_id,
            route_fingerprint=fingerprint,
            timeframe="5m",
            request_mode=exact_request_mode,
            request_type="historical",
            provider_source=provider_source,
            provider_contract_id=provider_contract_id,
            provider_contract_type=provider_contract_type,
            data_type="TRADES",
        ),
        covered_from=starts_at,
        covered_to=ends_at,
        response_count=response_count,
        completed_at=ends_at + timedelta(seconds=1),
    )


@dataclass
class _Transaction:
    committed: bool = False
    rolled_back: bool = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params) -> None:
        self.executions.append((" ".join(str(statement).split()), tuple(params)))


class _SnapshotCursor:
    def __init__(
        self,
        *,
        deleted_counts: list[int],
        readbacks: list[list[tuple[object, ...]]],
    ) -> None:
        self.deleted_counts = list(deleted_counts)
        self.readbacks = list(readbacks)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = -1
        self._rows: list[tuple[object, ...]] = []

    def execute(self, query, params) -> None:
        sql = " ".join(str(query).split())
        self.calls.append((sql, tuple(params)))
        if sql.startswith("DELETE"):
            self.rowcount = self.deleted_counts.pop(0)
            self._rows = []
            return
        if sql.startswith("SELECT"):
            self.rowcount = -1
            self._rows = self.readbacks.pop(0)
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return list(self._rows)


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _Cursor()
        self.transaction_value = _Transaction()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.cursor_value

    def transaction(self):
        return self.transaction_value


class _Repo(HistoryCommitsRepoMixin):
    def _require_canonical_writer_lease(self) -> None:
        return None

    def _acquire_canonical_writer_fence_on_cursor(self, _cur) -> None:
        return None

    def __init__(
        self,
        bar_receipt: CanonicalBarCommitReceipt,
        *,
        coverage_result: tuple[int, bool] = (0, False),
        coverage_error: Exception | None = None,
        deleted_result: int = 0,
        reconcile_error: Exception | None = None,
    ) -> None:
        self.connection = _Connection()
        self.bar_receipt = bar_receipt
        self.coverage_result = coverage_result
        self.coverage_error = coverage_error
        self.deleted_result = deleted_result
        self.reconcile_error = reconcile_error
        self.calls: list[tuple[str, object]] = []
        self.finalized = False

    def _connect(self):
        return self.connection

    def _write_bars_on_cursor(self, cur, *_args, **_kwargs):
        self.calls.append(("ordinary", cur))
        return self.bar_receipt

    def _write_futures_contract_bars_on_cursor(self, cur, *_args, **_kwargs):
        self.calls.append(("exact", cur))
        return self.bar_receipt

    def _lock_history_coverage_contract_on_cursor(self, cur, contract):
        self.calls.append(("lock", cur))
        from aef_terminal.storage.repos.history_coverage import (
            HistoryCoverageRepoMixin,
        )

        HistoryCoverageRepoMixin._lock_history_coverage_contract_on_cursor(
            cur,
            contract,
        )

    def _write_history_coverages_if_expanded(
        self,
        cur,
        _receipts,
        *,
        coverage_lock_held=False,
    ):
        assert coverage_lock_held is True
        self.calls.append(("coverage", cur))
        if self.coverage_error is not None:
            raise self.coverage_error
        return self.coverage_result

    def _reconcile_exact_history_snapshot_on_cursor(
        self,
        cur,
        _bars,
        _receipts,
        *,
        route,
        contract_key,
        revision_sequence,
    ):
        self.calls.append(
            (
                "reconcile",
                (cur, route, contract_key, revision_sequence),
            )
        )
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.deleted_result

    def _finalize_bars_commit(self, _receipt, *, provider):
        self.calls.append((f"finalize:{provider}", None))
        self.finalized = True

    def _finalize_futures_contract_bars_commit(self, _receipt):
        self.calls.append(("finalize:exact", None))
        self.finalized = True


def test_atomic_history_commit_writes_bars_then_coverage_on_one_cursor() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    chunk_end = starts_at + timedelta(hours=1)
    ends_at = starts_at + timedelta(hours=2)
    first_bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=starts_at,
    )
    second_bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=chunk_end,
    )
    first_coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=chunk_end,
        response_count=1,
    )
    second_coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=chunk_end,
        ends_at=ends_at,
        response_count=1,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=7,
            written=2,
            acknowledged=(first_bar, second_bar),
        ),
        coverage_result=(2, True),
    )

    receipt = repo.commit_history_result(
        (first_bar, second_bar),
        provider="ibkr",
        instrument=instrument,
        revision_sequence=7,
        coverage_receipts=(first_coverage, second_coverage),
    )

    assert isinstance(receipt, CanonicalHistoryCommitReceipt)
    assert receipt.coverage_inserted == 2
    assert receipt.coverage_expanded is True
    assert receipt.mutated is True
    assert [name for name, _value in repo.calls] == [
        "lock",
        "ordinary",
        "reconcile",
        "coverage",
        "finalize:ibkr",
    ]
    assert repo.calls[0][1] is repo.connection.cursor_value
    assert "pg_advisory_xact_lock" in repo.connection.cursor_value.executions[0][0]
    assert repo.calls[2][1][0] is repo.connection.cursor_value
    assert repo.calls[2][1][2] is None
    assert repo.calls[3][1] is repo.connection.cursor_value
    assert repo.connection.transaction_value.committed is True


@pytest.mark.parametrize(
    "version_field",
    (
        "receipt_contract_version",
        "request_contract_version",
        "admission_contract_version",
    ),
)
def test_history_commit_rejects_non_current_coverage_authority(version_field) -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
    )
    stale_coverage = replace(
        coverage,
        contract=replace(coverage.contract, **{version_field: 1}),
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=8,
            acknowledged=(bar,),
        )
    )

    with pytest.raises(ValueError, match="COVERAGE_CONTRACT_VERSION_MISMATCH"):
        repo.commit_history_result(
            (bar,),
            provider="ibkr",
            instrument=instrument,
            revision_sequence=8,
            coverage_receipts=(stale_coverage,),
        )

    assert repo.calls == []


@pytest.mark.parametrize(
    ("identity_field", "mismatched_value"),
    (
        ("request_mode", "different_request_mode"),
        ("provider_source", "DIFFERENT_PROVIDER_SOURCE"),
        ("provider_contract_id", "different-contract-id"),
        ("provider_contract_type", "OPT"),
    ),
)
def test_history_commit_rejects_receipt_request_identity_mismatch(
    identity_field,
    mismatched_value,
) -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
    )
    mismatched = replace(
        coverage,
        contract=replace(
            coverage.contract,
            **{identity_field: mismatched_value},
        ),
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=9,
            acknowledged=(bar,),
        )
    )

    with pytest.raises(ValueError, match="COVERAGE_REQUEST_IDENTITY_MISMATCH"):
        repo.commit_history_result(
            (bar,),
            provider="ibkr",
            instrument=instrument,
            revision_sequence=9,
            coverage_receipts=(mismatched,),
        )

    assert repo.calls == []


def test_coinbase_provider_mirror_coverage_is_accepted_for_reconciliation() -> None:
    instrument = coinbase_btc_payload()
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="PRODUCT",
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="PRODUCT",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
        request_mode="exchange_get_product_candles",
        provider_source="EXCHANGE",
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=9,
            acknowledged=(bar,),
        )
    )

    receipt = repo.commit_history_result(
        (bar,),
        provider="coinbase",
        instrument=instrument,
        revision_sequence=9,
        coverage_receipts=(coverage,),
    )

    assert receipt.bar_receipt.acknowledged == (bar,)
    assert [name for name, _value in repo.calls] == [
        "lock",
        "ordinary",
        "reconcile",
        "coverage",
        "finalize:coinbase",
    ]


def test_atomic_history_commit_rolls_back_bars_when_coverage_write_fails() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=starts_at,
    )
    chunk_end = starts_at + timedelta(hours=1)
    second_bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=chunk_end,
    )
    first_coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=chunk_end,
        response_count=1,
    )
    second_coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=chunk_end,
        ends_at=chunk_end + timedelta(hours=1),
        response_count=1,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=9,
            written=2,
            acknowledged=(bar, second_bar),
        ),
        coverage_error=RuntimeError("coverage failed"),
    )

    with pytest.raises(RuntimeError, match="coverage failed"):
        repo.commit_history_result(
            (bar, second_bar),
            provider="ibkr",
            instrument=instrument,
            revision_sequence=9,
            coverage_receipts=(first_coverage, second_coverage),
        )

    assert repo.connection.transaction_value.rolled_back is True
    assert repo.finalized is False


def test_exact_snapshot_readback_mismatch_rolls_back_before_coverage() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=10,
            acknowledged=(bar,),
        ),
        reconcile_error=RuntimeError("HISTORY_COMMIT_EXACT_SNAPSHOT_MISMATCH"),
    )

    with pytest.raises(RuntimeError, match="EXACT_SNAPSHOT_MISMATCH"):
        repo.commit_history_result(
            (bar,),
            provider="ibkr",
            instrument=instrument,
            revision_sequence=10,
            coverage_receipts=(coverage,),
        )

    assert repo.connection.transaction_value.rolled_back is True
    assert [name for name, _value in repo.calls] == [
        "lock",
        "ordinary",
        "reconcile",
    ]
    assert repo.finalized is False


def test_empty_complete_response_can_expand_coverage_without_bar_mutation() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=0,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(revision_sequence=11),
        coverage_result=(1, True),
    )

    receipt = repo.commit_history_result(
        (),
        provider="ibkr",
        instrument=instrument,
        revision_sequence=11,
        coverage_receipts=(coverage,),
    )

    assert receipt.bar_receipt.written == 0
    assert receipt.coverage_expanded is True
    assert receipt.mutated is True
    assert [name for name, _value in repo.calls] == [
        "lock",
        "ordinary",
        "reconcile",
        "coverage",
        "finalize:ibkr",
    ]


@pytest.mark.parametrize(
    "instrument_factory",
    (coinbase_btc_payload, _tinvest_stock_payload),
    ids=("coinbase", "tinvest"),
)
def test_provider_mirror_empty_snapshot_reconciles_omitted_local_rows(
    instrument_factory,
) -> None:
    instrument = instrument_factory()
    route = route_instrument(instrument)
    identity = route.adapter.history_request_identity(route.instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    ends_at = starts_at + timedelta(hours=1)
    coverage = HistoryCoverageReceipt(
        contract=current_history_coverage_contract(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe="5m",
            request_mode=identity.request_mode,
            request_type=identity.request_type.value,
            provider_source=identity.provider_source,
            provider_contract_id=identity.provider_contract_id,
            provider_contract_type=identity.provider_contract_type,
            data_type=identity.data_type,
        ),
        covered_from=starts_at,
        covered_to=ends_at,
        response_count=0,
        completed_at=ends_at + timedelta(seconds=1),
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(revision_sequence=41),
        coverage_result=(1, True),
        deleted_result=2,
    )

    receipt = repo.commit_history_result(
        (),
        provider=route.provider,
        instrument=route.instrument,
        revision_sequence=41,
        coverage_receipts=(coverage,),
    )

    assert route.adapter.capabilities.exact_history_snapshot_authority is True
    assert receipt.bar_receipt.written == 0
    assert receipt.bar_receipt.deleted == 2
    assert receipt.coverage_inserted == 1
    assert receipt.coverage_expanded is True
    assert [name for name, _value in repo.calls] == [
        "lock",
        "ordinary",
        "reconcile",
        "coverage",
        f"finalize:{route.provider}",
    ]


def test_delete_only_exact_replacement_is_a_mutation_on_coverage_replay() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=0,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(revision_sequence=12),
        coverage_result=(0, False),
        deleted_result=2,
    )

    receipt = repo.commit_history_result(
        (),
        provider="ibkr",
        instrument=instrument,
        revision_sequence=12,
        coverage_receipts=(coverage,),
    )

    assert receipt.bar_receipt.written == 0
    assert receipt.bar_receipt.deleted == 2
    assert receipt.coverage_expanded is False
    assert receipt.mutated is True


def test_ibkr_exact_future_snapshot_writes_contract_history() -> None:
    instrument = ibkr_stock_payload(
        "ESU6",
        con_id=620730920,
        asset_class="future",
        sec_type="FUT",
    )
    instrument["contract_identity"]["identity_scope"] = "contract"
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="FUT",
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="FUT",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=14,
            written=1,
            acknowledged=(bar,),
        ),
        coverage_result=(1, True),
    )

    receipt = repo.commit_history_result(
        (bar,),
        provider="ibkr",
        instrument=instrument,
        revision_sequence=14,
        coverage_receipts=(coverage,),
    )

    assert receipt.bar_receipt.written == 1
    assert receipt.coverage_expanded is True
    assert receipt.mutated is True
    assert [name for name, _value in repo.calls] == [
        "lock",
        "exact",
        "reconcile",
        "coverage",
        "finalize:exact",
    ]
    assert repo.calls[2][1][2] == "620730920"


def test_ibkr_exact_future_empty_snapshot_deletes_from_contract_history() -> None:
    instrument = ibkr_stock_payload(
        "ESU6",
        con_id=620730920,
        asset_class="future",
        sec_type="FUT",
    )
    instrument["contract_identity"]["identity_scope"] = "contract"
    route = route_instrument(instrument)
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="FUT",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=0,
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(revision_sequence=15),
        coverage_result=(0, False),
        deleted_result=2,
    )

    receipt = repo.commit_history_result(
        (),
        provider="ibkr",
        instrument=instrument,
        revision_sequence=15,
        coverage_receipts=(coverage,),
    )

    assert receipt.bar_receipt.deleted == 2
    assert receipt.coverage_expanded is False
    assert receipt.mutated is True
    assert [name for name, _value in repo.calls] == [
        "lock",
        "reconcile",
        "coverage",
        "finalize:exact",
    ]
    assert repo.calls[1][1][2] == "620730920"


@pytest.mark.parametrize(
    ("contract_key", "table_name"),
    (
        (None, "bars"),
        ("contract-uid", "futures_contract_bars"),
    ),
)
def test_exact_snapshot_reconciliation_deletes_only_absent_slots_and_rereads_range(
    contract_key,
    table_name,
) -> None:
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    route = SimpleNamespace(
        provider="ibkr",
        instrument_id="ibkr|contract|123",
        fingerprint="ibkr|contract|123",
    )
    provider_contract_type = "FUT" if contract_key else "STK"
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=contract_key or "123",
        provider_contract_type=provider_contract_type,
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id=contract_key or "123",
        provider_contract_type=provider_contract_type,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
    )
    cursor = _SnapshotCursor(
        deleted_counts=[2],
        readbacks=[
            [
                (
                    starts_at,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                )
            ]
        ],
    )

    deleted = HistoryCommitsRepoMixin._reconcile_exact_history_snapshot_on_cursor(
        cursor,
        (bar,),
        (coverage,),
        route=route,
        contract_key=contract_key,
        revision_sequence=31,
    )

    assert deleted == 2
    delete_sql, delete_params = cursor.calls[0]
    select_sql, select_params = cursor.calls[1]
    assert f"DELETE FROM {table_name}" in delete_sql
    assert f"FROM {table_name}" in select_sql
    assert "revision_sequence <= %s" in delete_sql
    assert "NOT (ts = ANY(%s::timestamptz[]))" in delete_sql
    assert delete_params[-1] == [starts_at]
    assert select_params[-2:] == (coverage.covered_from, coverage.covered_to)
    if contract_key is not None:
        assert contract_key in delete_params
        assert contract_key in select_params


def test_exact_snapshot_reconciliation_rejects_extra_persisted_slot() -> None:
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    route = SimpleNamespace(
        provider="ibkr",
        instrument_id="ibkr|contract|123",
        fingerprint="ibkr|contract|123",
    )
    bar = _bar(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id="123",
        provider_contract_type="STK",
        ts=starts_at,
    )
    coverage = _coverage(
        provider=route.provider,
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id="123",
        provider_contract_type="STK",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
    )
    row = (starts_at, bar.open, bar.high, bar.low, bar.close, bar.volume)
    cursor = _SnapshotCursor(
        deleted_counts=[0],
        readbacks=[
            [
                row,
                (starts_at + timedelta(minutes=5), *row[1:]),
            ]
        ],
    )

    with pytest.raises(RuntimeError, match="EXACT_SNAPSHOT_MISMATCH"):
        HistoryCommitsRepoMixin._reconcile_exact_history_snapshot_on_cursor(
            cursor,
            (bar,),
            (coverage,),
            route=route,
            contract_key=None,
            revision_sequence=37,
        )


def test_exact_replay_with_contained_coverage_is_no_mutation(monkeypatch) -> None:
    starts_at = datetime(2026, 8, 1, tzinfo=UTC)
    instrument = {"asset_class": "future"}
    history_route = CanonicalHistoryRoute.exact_futures_contract("contract-uid")
    adapter = SimpleNamespace(
        canonical_history_route=lambda _instrument: history_route,
        session_contract_id=lambda _instrument: "contract-uid",
        bar_data_type=lambda _instrument: "TRADES",
        capabilities=SimpleNamespace(exact_history_snapshot_authority=True),
        history_request_identity=lambda _instrument: HistoryRequestAdmissionIdentity(
            request_contract_version=HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
            admission_contract_version=HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
            request_mode="req_historical_data",
            request_type=BarProviderRequest.HISTORICAL,
            provider_source="IBKR_HISTORICAL",
            provider_contract_id="contract-uid",
            provider_contract_type="FUT",
            data_type="TRADES",
        ),
    )
    route = SimpleNamespace(
        provider="ibkr",
        instrument_id="ibkr|contract|contract-uid",
        fingerprint="ibkr|contract|contract-uid",
        instrument=instrument,
        adapter=adapter,
    )
    monkeypatch.setattr(history_commits_module, "route_instrument", lambda *_args, **_kwargs: route)
    monkeypatch.setattr(
        history_commits_module,
        "instrument_is_exact_futures_contract",
        lambda _instrument: True,
    )
    bar = _bar(
        provider="ibkr",
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id="contract-uid",
        provider_contract_type="FUT",
        ts=starts_at,
    )
    coverage = _coverage(
        provider="ibkr",
        instrument_id=route.instrument_id,
        fingerprint=route.fingerprint,
        provider_contract_id="contract-uid",
        provider_contract_type="FUT",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        response_count=1,
        request_mode="req_historical_data",
    )
    repo = _Repo(
        CanonicalBarCommitReceipt(
            revision_sequence=13,
            acknowledged=(bar,),
        ),
        coverage_result=(0, False),
    )

    receipt = repo.commit_history_result(
        (bar,),
        provider="ibkr",
        instrument=instrument,
        revision_sequence=13,
        coverage_receipts=(coverage,),
    )

    assert receipt.coverage_inserted == 0
    assert receipt.coverage_expanded is False
    assert receipt.mutated is False
    assert [name for name, _value in repo.calls] == [
        "lock",
        "exact",
        "reconcile",
        "coverage",
        "finalize:exact",
    ]
    assert repo.calls[2][1][2] == "contract-uid"


def test_contained_insert_does_not_mark_combined_receipt_mutated() -> None:
    receipt = CanonicalHistoryCommitReceipt(
        bar_receipt=CanonicalBarCommitReceipt(revision_sequence=17),
        coverage_inserted=1,
        coverage_expanded=False,
    )

    assert receipt.coverage_inserted == 1
    assert receipt.coverage_expanded is False
    assert receipt.mutated is False


def test_ordinary_post_commit_finalize_cannot_hide_committed_receipt(
    monkeypatch,
) -> None:
    class _FinalizeRepo(BarsRepoMixin):
        @staticmethod
        def _invalidate_bar_slots_cache() -> None:
            raise RuntimeError("cache failed after commit")

    def _metric_failure(*_args, **_kwargs) -> None:
        raise RuntimeError("metric failed after commit")

    monkeypatch.setattr(bars_module, "increment_metric", _metric_failure)
    receipt = CanonicalBarCommitReceipt(revision_sequence=19, deleted=1)

    _FinalizeRepo()._finalize_bars_commit(receipt, provider="ibkr")


def test_exact_future_post_commit_finalize_cannot_hide_committed_receipt() -> None:
    class _FinalizeRepo(FuturesRepoMixin):
        @staticmethod
        def _invalidate_bar_slots_cache() -> None:
            raise RuntimeError("cache failed after commit")

    receipt = CanonicalBarCommitReceipt(revision_sequence=23, deleted=1)

    _FinalizeRepo()._finalize_futures_contract_bars_commit(receipt)
