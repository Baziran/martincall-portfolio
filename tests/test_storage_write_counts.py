from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar, BarProvenance, Tick
from aef_terminal.storage.repos import futures as futures_repo_module
from aef_terminal.storage.repos.bars import BarsRepoMixin
from aef_terminal.storage.repos.connection import PostgresConnectionMixin
from aef_terminal.storage.repos.connection import CanonicalWriterCapability
from aef_terminal.storage.repos.futures import FuturesRepoMixin
from aef_terminal.storage.repos.ticks import TicksRepoMixin
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


class _Copy:
    def __init__(self, cursor: "_Cursor") -> None:
        self.cursor = cursor
        self.pending_rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        if exc_type is not None:
            self.pending_rows.clear()
            return None
        self.cursor.copied_rows.extend(self.pending_rows)
        self.cursor.rowcount = len(self.pending_rows)
        self.pending_rows.clear()
        return None

    def write_row(self, row) -> None:
        if self.cursor.copy_fail_at == len(self.pending_rows):
            raise RuntimeError("copy failed")
        self.pending_rows.append(row)


class _Cursor:
    def __init__(
        self,
        rowcount: int,
        copy_fail_at: int | None = None,
        *,
        persisted_close: float | None = None,
        persisted_revision: int | None = None,
    ) -> None:
        self.rowcount = rowcount
        self.configured_rowcount = rowcount
        self.copy_fail_at = copy_fail_at
        self.persisted_close = persisted_close
        self.persisted_revision = persisted_revision
        self.copy_query = ""
        self.copied_rows: list[tuple] = []
        self.query = ""
        self.written_rows: list[tuple] = []
        self.result_rows: list[tuple] = []
        self.has_result_rows = False
        self.execute_calls = 0
        self.executemany_calls = 0
        self.json_payload_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params=None) -> None:
        self.execute_calls += 1
        self.query = query
        if "jsonb_to_recordset" not in query or not params:
            return
        payload = getattr(params[0], "obj", params[0])
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.json_payload_sizes.append(len(payload))
        written_limit = max(int(self.configured_rowcount), 0)
        self.has_result_rows = True
        if "RETURNING timeframe, ts" in query:
            self.result_rows = [
                (row["timeframe"], datetime.fromisoformat(row["ts"]))
                for row in payload[:written_limit]
            ]
        elif "JOIN futures_contract_bars AS persisted" in query:
            self.result_rows = [
                (
                    datetime.fromisoformat(row["ts"]),
                    row["open"],
                    row["high"],
                    row["low"],
                    (self.persisted_close if self.persisted_close is not None else row["close"]),
                    row["volume"],
                    row["timeframe"],
                    row["source"],
                    row["closed"],
                    (
                        self.persisted_revision
                        if self.persisted_revision is not None
                        else row["revision_sequence"]
                    ),
                )
                for row in payload
            ]
        elif "JOIN futures_continuous_bars AS persisted" in query:
            self.result_rows = [
                (
                    datetime.fromisoformat(row["ts"]),
                    row["open"],
                    row["high"],
                    row["low"],
                    (self.persisted_close if self.persisted_close is not None else row["close"]),
                    row["volume"],
                    row["timeframe"],
                    row["source"],
                    row["closed"],
                    (
                        self.persisted_revision
                        if self.persisted_revision is not None
                        else row["revision_sequence"]
                    ),
                )
                for row in payload
            ]
        elif "JOIN bars AS persisted" in query:
            self.result_rows = [
                (
                    row["symbol"],
                    datetime.fromisoformat(row["ts"]),
                    row["open"],
                    row["high"],
                    row["low"],
                    (self.persisted_close if self.persisted_close is not None else row["close"]),
                    row["volume"],
                    row["timeframe"],
                    row["source"],
                    row["closed"],
                    (
                        self.persisted_revision
                        if self.persisted_revision is not None
                        else row["revision_sequence"]
                    ),
                )
                for row in payload
            ]
        else:
            self.result_rows = []

    def executemany(self, query: str, rows) -> None:
        self.executemany_calls += 1
        self.query = query
        self.written_rows = list(rows)

    def fetchall(self):
        if self.has_result_rows:
            return self.result_rows
        if not self.written_rows:
            return []
        row = self.written_rows[0]
        if "FROM futures_continuous_bars" in self.query:
            return [
                (
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[4],
                    row[11],
                    row[12],
                    row[14],
                )
            ]
        if "FROM bars" in self.query:
            return [
                (
                    row[3],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[4],
                    row[11],
                    row[12],
                    row[13],
                )
            ]
        return []

    def copy(self, query: str) -> _Copy:
        self.copy_query = query
        return _Copy(self)


class _Connection:
    def __init__(
        self,
        rowcount: int,
        copy_fail_at: int | None = None,
        *,
        persisted_close: float | None = None,
        persisted_revision: int | None = None,
    ) -> None:
        self._cursor = _Cursor(
            rowcount,
            copy_fail_at,
            persisted_close=persisted_close,
            persisted_revision=persisted_revision,
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor

    def transaction(self):
        return self


class _Repo(BarsRepoMixin, FuturesRepoMixin, TicksRepoMixin, PostgresConnectionMixin):
    def __init__(
        self,
        rowcount: int,
        copy_fail_at: int | None = None,
        *,
        persisted_close: float | None = None,
        persisted_revision: int | None = None,
    ) -> None:
        self._canonical_writer_capability = CanonicalWriterCapability.TEST_BYPASS
        self._connection = _Connection(
            rowcount,
            copy_fail_at,
            persisted_close=persisted_close,
            persisted_revision=persisted_revision,
        )
        self._bar_slots_cache = {}
        self._bar_slots_cache_lock = threading.Lock()
        self._bar_slots_cache_generation = 0

    def _connect(self) -> _Connection:
        return self._connection

    def _psycopg(self) -> None:
        return None


def _bar(symbol: str = "SPY") -> Bar:
    route = route_instrument(
        ibkr_future_payload("ES") if symbol == "ES" else ibkr_stock_payload(symbol)
    )
    return Bar(
        symbol=symbol,
        ts=datetime(2026, 7, 15, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=10.0,
        source="ibkr:historical:TRADES:contract=123:route=test",
        closed=True,
        provenance=BarProvenance(
            provider="ibkr",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            request_type="historical",
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
            provider_contract_type=("CONTFUT" if symbol == "ES" else "STK"),
            data_type=route.adapter.bar_data_type(route.instrument),
        ),
    )


def test_storage_writers_return_database_rowcount(monkeypatch) -> None:
    metrics: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "aef_terminal.storage.repos.bars.increment_metric",
        lambda name, value=1, **_labels: metrics.append((name, value)),
    )
    monkeypatch.setattr(
        "aef_terminal.storage.repos.bars.observe_metric", lambda *_args, **_kwargs: None
    )

    repo = _Repo(rowcount=1)
    repo._bar_slots_cache["window"] = (1.0, {})
    receipt = repo.write_bars(
        [_bar()],
        "ibkr",
        instrument=ibkr_stock_payload("SPY"),
        revision_sequence=1,
    )
    assert receipt.written == 1
    assert repo._bar_slots_cache == {}
    assert ("storage_bars_written_total", 1) in metrics
    future_route = route_instrument(ibkr_future_payload("ES"))
    concrete_bar = _bar("ES")
    assert concrete_bar.provenance is not None
    concrete_bar = replace(
        concrete_bar,
        provenance=replace(
            concrete_bar.provenance,
            provider_contract_type="FUT",
        ),
    )
    execute_calls_before = repo._connection._cursor.execute_calls
    executemany_calls_before = repo._connection._cursor.executemany_calls
    contract_receipt = repo.write_futures_contract_bars(
        [concrete_bar],
        provider="ibkr",
        instrument_id=future_route.instrument_id,
        route_fingerprint=future_route.fingerprint,
        contract_key="ESU6",
        provider_contract_id=future_route.adapter.session_contract_id(future_route.instrument),
        provider_contract_type="FUT",
        data_type=future_route.adapter.bar_data_type(future_route.instrument),
        revision_sequence=2,
    )
    assert contract_receipt.written == 1
    assert contract_receipt.acknowledged == (concrete_bar,)
    assert repo._connection._cursor.execute_calls == execute_calls_before + 2
    assert repo._connection._cursor.executemany_calls == executemany_calls_before
    repo._bar_slots_cache["futures-window"] = (1.5, {})
    receipt = repo.write_futures_canonical_bars(
        [_bar("ES")],
        provider="ibkr",
        instrument_id=future_route.instrument_id,
        route_fingerprint=future_route.fingerprint,
        series_type="continuous",
        roll_policy="volume",
        metadata={
            "provider_contract_id": future_route.adapter.session_contract_id(
                future_route.instrument
            ),
            "provider_contract_type": "CONTFUT",
            "data_type": future_route.adapter.bar_data_type(future_route.instrument),
        },
        revision_sequence=2,
    )
    assert receipt.written == 1
    assert repo._bar_slots_cache == {}
    with pytest.raises(ValueError, match="TICK_VOLUME_NEGATIVE"):
        Tick(
            instrument_id=future_route.instrument_id,
            route_fingerprint=future_route.fingerprint,
            ts=datetime(2026, 7, 15, tzinfo=UTC),
            price=6300.0,
            volume=-3,
            delta_sign=1,
        )
    with pytest.raises(ValueError, match="TICK_DELTA_SIGN_INVALID"):
        Tick(
            instrument_id=future_route.instrument_id,
            route_fingerprint=future_route.fingerprint,
            ts=datetime(2026, 7, 15, tzinfo=UTC),
            price=6300.0,
            volume=3,
            delta_sign=9,
        )
    tick = Tick(
        instrument_id=future_route.instrument_id,
        route_fingerprint=future_route.fingerprint,
        ts=datetime(2026, 7, 15, tzinfo=UTC),
        price=6300.0,
        volume=3,
        delta_sign=1,
        tick_type="trade",
        bid=6299.75,
        ask=6300.25,
        exchange="CME",
        conditions=("fast",),
    )
    repo._bar_slots_cache["window"] = (2.0, {})
    assert repo.write_ticks([tick, tick], provider="ibkr") == 2
    assert repo._bar_slots_cache == {"window": (2.0, {})}
    assert "COPY ticks" in repo._connection._cursor.copy_query
    assert "FROM STDIN" in repo._connection._cursor.copy_query
    assert (
        repo._connection._cursor.copied_rows
        == [
            (
                "ibkr",
                future_route.instrument_id,
                future_route.fingerprint,
                datetime(2026, 7, 15, tzinfo=UTC),
                6300.0,
                3,
                1,
                "trade",
                6299.75,
                6300.25,
                "CME",
                ["fast"],
            ),
        ]
        * 2
    )


def test_futures_canonical_noop_write_preserves_bar_slot_cache() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    repo = _Repo(rowcount=0)
    repo._bar_slots_cache["futures-window"] = (1.0, {})

    receipt = repo.write_futures_canonical_bars(
        [_bar("ES")],
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        series_type="continuous",
        roll_policy="volume",
        metadata={
            "provider_contract_id": route.adapter.session_contract_id(route.instrument),
            "provider_contract_type": "CONTFUT",
            "data_type": route.adapter.bar_data_type(route.instrument),
        },
        revision_sequence=2,
    )

    assert receipt.written == 0
    assert repo._bar_slots_cache == {"futures-window": (1.0, {})}


def test_futures_canonical_large_write_uses_bounded_json_batches() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    batch_rows = futures_repo_module._FUTURES_CANONICAL_WRITE_BATCH_ROWS
    total_rows = batch_rows * 2 + 1
    base = _bar("ES")
    chronological = [
        replace(base, ts=base.ts + timedelta(minutes=5 * index)) for index in range(total_rows)
    ]
    submitted = list(reversed(chronological))
    repo = _Repo(rowcount=total_rows)
    repo._bar_slots_cache["futures-window"] = (1.0, {})

    receipt = repo.write_futures_canonical_bars(
        submitted,
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        series_type="continuous",
        roll_policy="volume",
        metadata={
            "provider_contract_id": route.adapter.session_contract_id(route.instrument),
            "provider_contract_type": "CONTFUT",
            "data_type": route.adapter.bar_data_type(route.instrument),
        },
        revision_sequence=2,
    )

    assert repo._connection._cursor.json_payload_sizes == [
        batch_rows,
        batch_rows,
        batch_rows,
        batch_rows,
        1,
        1,
    ]
    assert repo._connection._cursor.execute_calls == 6
    assert repo._connection._cursor.executemany_calls == 0
    assert receipt.written == total_rows
    assert receipt.acknowledged == tuple(submitted)
    assert receipt.superseded == ()
    assert receipt.rejected == ()
    assert repo._bar_slots_cache == {}
    assert repo._bar_slots_cache_generation == 1


def test_canonical_bar_receipts_read_exact_newer_conflict_after_blocked_upsert() -> None:
    generic_bar = _bar()
    generic_repo = _Repo(
        rowcount=0,
        persisted_close=generic_bar.high,
        persisted_revision=2,
    )

    generic_receipt = generic_repo.write_bars(
        [generic_bar],
        "ibkr",
        instrument=ibkr_stock_payload("SPY"),
        revision_sequence=1,
    )

    assert generic_receipt.written == 0
    assert generic_receipt.acknowledged == ()
    assert generic_receipt.superseded == (generic_bar,)

    futures_bar = _bar("ES")
    route = route_instrument(ibkr_future_payload("ES"))
    futures_repo = _Repo(
        rowcount=0,
        persisted_close=futures_bar.high,
        persisted_revision=3,
    )
    futures_receipt = futures_repo.write_futures_canonical_bars(
        [futures_bar],
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        series_type="continuous",
        roll_policy="volume",
        metadata={
            "provider_contract_id": route.adapter.session_contract_id(route.instrument),
            "provider_contract_type": "CONTFUT",
            "data_type": route.adapter.bar_data_type(route.instrument),
        },
        revision_sequence=1,
    )

    assert futures_receipt.written == 0
    assert futures_receipt.acknowledged == ()
    assert futures_receipt.superseded == (futures_bar,)

    assert futures_bar.provenance is not None
    contract_bar = replace(
        futures_bar,
        provenance=replace(
            futures_bar.provenance,
            provider_contract_type="FUT",
        ),
    )
    exact_repo = _Repo(
        rowcount=0,
        persisted_close=contract_bar.high,
        persisted_revision=3,
    )
    exact_receipt = exact_repo.write_futures_contract_bars(
        [contract_bar],
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        contract_key="ESU6",
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="FUT",
        data_type=route.adapter.bar_data_type(route.instrument),
        revision_sequence=1,
    )

    assert exact_receipt.written == 0
    assert exact_receipt.acknowledged == ()
    assert exact_receipt.superseded == (contract_bar,)


def test_identical_later_revision_is_acknowledged_without_mutation_and_correction_writes() -> None:
    generic_bar = _bar()
    generic_replay = _Repo(rowcount=0, persisted_revision=1).write_bars(
        [generic_bar],
        "ibkr",
        instrument=ibkr_stock_payload("SPY"),
        revision_sequence=2,
    )
    assert generic_replay.written == 0
    assert generic_replay.acknowledged == (generic_bar,)
    generic_correction_bar = replace(generic_bar, close=generic_bar.close + 0.25)
    generic_correction = _Repo(rowcount=1).write_bars(
        [generic_correction_bar],
        "ibkr",
        instrument=ibkr_stock_payload("SPY"),
        revision_sequence=3,
    )
    assert generic_correction.written == 1
    assert generic_correction.acknowledged == (generic_correction_bar,)

    route = route_instrument(ibkr_future_payload("ES"))
    exact_bar = _bar("ES")
    assert exact_bar.provenance is not None
    exact_bar = replace(
        exact_bar,
        provenance=replace(exact_bar.provenance, provider_contract_type="FUT"),
    )
    exact_replay = _Repo(rowcount=0, persisted_revision=1).write_futures_contract_bars(
        [exact_bar],
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        contract_key="ESU6",
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="FUT",
        data_type=route.adapter.bar_data_type(route.instrument),
        revision_sequence=2,
    )
    assert exact_replay.written == 0
    assert exact_replay.acknowledged == (exact_bar,)

    corrected_bar = replace(exact_bar, close=exact_bar.close + 0.25)
    correction = _Repo(rowcount=1).write_futures_contract_bars(
        [corrected_bar],
        provider="ibkr",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        contract_key="ESU6",
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
        provider_contract_type="FUT",
        data_type=route.adapter.bar_data_type(route.instrument),
        revision_sequence=3,
    )
    assert correction.written == 1
    assert correction.acknowledged == (corrected_bar,)


def test_tick_copy_failure_propagates_without_invalidating_bar_slot_cache() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    tick = Tick(
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        ts=datetime(2026, 7, 15, tzinfo=UTC),
        price=6300.0,
        volume=1,
        delta_sign=1,
    )
    repo = _Repo(rowcount=0, copy_fail_at=1)
    repo._bar_slots_cache["window"] = (3.0, {})

    with pytest.raises(RuntimeError, match="copy failed"):
        repo.write_ticks([tick, tick], provider="ibkr")

    assert repo._connection._cursor.copied_rows == []
    assert repo._bar_slots_cache == {"window": (3.0, {})}
