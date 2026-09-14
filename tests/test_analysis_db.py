from __future__ import annotations

import inspect
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.domain import Bar
from aef_terminal.data.adapters.ibkr_cache import ibkr_keep_requested_cache_window
from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.data.provider_contract import CanonicalHistoryRoute
from aef_terminal.data.provider_history import read_provider_history as read_provider_history_owner
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.analysis_db import (
    provider_backed_bar_slots,
    read_provider_mtf_context,
)
from aef_terminal.data.provider_sessions import (
    ProviderBarSlotMap,
    continuous_provider_bar_slot,
)
from aef_terminal.data import provider_sessions as sessions_module
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.timeframes import HistoryRangeWindow
from aef_terminal.indicators.modules.channel_master import manual_channel_context
from aef_terminal.storage.repos import bars as bars_repo_module
from aef_terminal.storage.repos.bars import BarsRepoMixin
from aef_terminal.storage.repos.futures import FuturesRepoMixin
from aef_terminal.storage.repos.connection import PostgresConnectionMixin
from aef_terminal.data.history_coverage import HistoryCoverageReceipt
from tests.provider_payloads import coinbase_btc_payload, ibkr_future_payload, ibkr_stock_payload


def read_provider_history(store, instrument, interval, range_, **kwargs):
    adapter = kwargs.pop("adapter", None) or route_instrument(instrument).adapter
    return read_provider_history_owner(
        store,
        instrument,
        interval,
        range_,
        adapter=adapter,
        **kwargs,
    )


class _FakeStore:
    def __init__(self, grouped: dict[tuple[str, str], list[Bar]] | None = None) -> None:
        self.grouped = grouped or {}
        self.calls: list[tuple[str, str, str]] = []
        self.multi_calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def read_bars_multi(
        self,
        *,
        timeframes: list[str],
        providers: list[str],
        start: datetime,
        end: datetime,
        instrument,
    ) -> dict[tuple[str, str], list[Bar]]:
        _ = start, end, instrument
        self.multi_calls.append((tuple(timeframes), tuple(providers)))
        return {
            key: list(bars)
            for key, bars in self.grouped.items()
            if key[0] in providers and key[1] in timeframes
        }

    def read_bars(self, *, timeframe: str, provider: str, start: datetime, instrument) -> list[Bar]:
        self.calls.append((qualified_instrument_id(instrument), timeframe, provider))
        return list(self.grouped.get((provider, timeframe), []))


def _bar(ts: str, close: float = 100.0, timeframe: str = "5m") -> Bar:
    return Bar(
        ts=datetime.fromisoformat(ts).replace(tzinfo=UTC),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1.0,
        symbol="ES",
        timeframe=timeframe,
    )


def test_full_snapshot_context_exposes_empty_cache_status() -> None:
    source = inspect.getsource(BarsRepoMixin.get_full_snapshot_context)

    assert not hasattr(BarsRepoMixin, "cache_state")
    assert '"cache_status": {}' in source


def test_multi_bar_reader_applies_per_timeframe_limits_inside_sql() -> None:
    executed: list[tuple[str, list[Any]]] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str, params: list[Any]) -> None:
            executed.append((sql, list(params)))

        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return _Cursor()

    repo = BarsRepoMixin()
    repo._connect = lambda: _Connection()
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)

    result = repo.read_bars_multi(
        timeframes=("5m", "1m"),
        providers=("ibkr",),
        instrument=ibkr_stock_payload("SPY"),
        timeframe_read_windows={
            "5m": (start, end, None),
            "1m": (start, end, 64),
        },
    )

    assert result == {}
    sql, params = executed[0]
    assert "WITH requested" in sql
    assert "CROSS JOIN LATERAL" in sql
    assert "ORDER BY bars.ts DESC" in sql
    assert "LIMIT requested.row_limit" in sql
    assert "ROW_NUMBER() OVER" not in sql
    assert params[4] is None
    assert params[9] == 64


def test_futures_multi_bar_reader_applies_per_timeframe_limits_inside_sql() -> None:
    executed: list[tuple[str, list[Any]]] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str, params: list[Any]) -> None:
            executed.append((sql, list(params)))

        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return _Cursor()

    repo = FuturesRepoMixin()
    repo._connect = lambda: _Connection()
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)

    result = repo.read_futures_canonical_bars_multi(
        provider="ibkr",
        instrument_id="ibkr:future:es",
        route_fingerprint="test-route-fingerprint",
        symbol="ES",
        timeframes=("5m", "1m"),
        series_type="provider_native",
        roll_policy="provider_managed",
        timeframe_read_windows={
            "5m": (start, end, None),
            "1m": (start, end, 256),
        },
    )

    assert result == {"5m": [], "1m": []}
    sql, params = executed[0]
    assert "WITH requested" in sql
    assert "CROSS JOIN LATERAL" in sql
    assert "ORDER BY bars.ts DESC" in sql
    assert "LIMIT requested.row_limit" in sql
    assert "ROW_NUMBER() OVER" not in sql
    assert params[3] is None
    assert params[7] == 256


def test_exact_futures_multi_reader_scopes_contract_route_and_half_open_windows() -> None:
    executed: list[tuple[str, list[Any]]] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str, params: list[Any]) -> None:
            executed.append((sql, list(params)))

        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return _Cursor()

    repo = FuturesRepoMixin()
    repo._connect = lambda: _Connection()
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)

    result = repo.read_futures_contract_bars_multi(
        provider="ibkr",
        instrument_id="ibkr|contract|contract-uid",
        route_fingerprint="ibkr|contract|contract-uid",
        symbol="SBERF6",
        contract_key="contract-uid",
        timeframes=("5m", "1m"),
        timeframe_read_windows={
            "5m": (start, end, None),
            "1m": (start, end, 256),
        },
    )

    assert result == {"5m": [], "1m": []}
    sql, params = executed[0]
    assert "FROM futures_contract_bars AS bars" in sql
    assert "bars.route_fingerprint = %s" in sql
    assert "bars.contract_key = %s" in sql
    assert "bars.ts < requested.end_exclusive" in sql
    assert "ORDER BY bars.ts DESC" in sql
    assert "LIMIT requested.row_limit" in sql
    assert params[-4:] == [
        "ibkr",
        "ibkr|contract|contract-uid",
        "ibkr|contract|contract-uid",
        "contract-uid",
    ]


def test_recent_batch_dispatches_exact_futures_to_contract_storage(monkeypatch) -> None:
    history_route = CanonicalHistoryRoute.exact_futures_contract("contract-uid")
    instrument = {"display": "SBERF6"}
    instrument_id = "tinvest|contract|contract-uid"
    monkeypatch.setattr(
        bars_repo_module,
        "_storage_route",
        lambda _providers, candidate: (("tinvest", history_route), candidate, instrument_id),
    )
    monkeypatch.setattr(bars_repo_module, "route_fingerprint", lambda _instrument: instrument_id)
    monkeypatch.setattr(bars_repo_module, "instrument_key", lambda _instrument: "SBERF6")
    calls: list[dict[str, object]] = []

    class _Repo(BarsRepoMixin):
        def read_futures_contract_bars_multi(self, **kwargs):
            calls.append(kwargs)
            return {"5m": [_bar("2026-01-01T14:00:00+00:00")]}

        def read_futures_canonical_bars_multi(self, **_kwargs):
            raise AssertionError("exact contract must not read continuous futures storage")

    grouped = _Repo().read_recent_bars_batch([("5m", "tinvest", 10, instrument)])

    assert list(grouped) == [(instrument_id, "5m")]
    assert calls[0]["contract_key"] == "contract-uid"
    assert calls[0]["latest"] is True
    assert "series_type" not in calls[0]
    assert "roll_policy" not in calls[0]


@pytest.mark.parametrize(
    ("latest", "expected_order"),
    ((True, "DESC"), (False, "ASC")),
)
def test_futures_limited_reader_uses_per_timeframe_index_limit(
    latest: bool,
    expected_order: str,
) -> None:
    executed: list[tuple[str, list[Any]]] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str, params: list[Any]) -> None:
            executed.append((sql, list(params)))

        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return _Cursor()

    repo = FuturesRepoMixin()
    repo._connect = lambda: _Connection()
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)

    result = repo.read_futures_canonical_bars_multi(
        provider="ibkr",
        instrument_id="ibkr:future:es",
        route_fingerprint="test-route-fingerprint",
        symbol="ES",
        timeframes=("5m", "1m"),
        series_type="provider_native",
        roll_policy="provider_managed",
        start=start,
        end=end,
        limit=64,
        latest=latest,
    )

    assert result == {"5m": [], "1m": []}
    sql, params = executed[0]
    assert "WITH requested(timeframe)" in sql
    assert "CROSS JOIN LATERAL" in sql
    assert f"ORDER BY bars.ts {expected_order}" in sql
    assert "LIMIT %s" in sql
    assert "ROW_NUMBER() OVER" not in sql
    assert params == [
        "5m",
        "1m",
        "ibkr",
        "ibkr:future:es",
        "provider_native",
        "provider_managed",
        start,
        end,
        64,
    ]


def test_full_snapshot_context_uses_canonical_futures_context_without_legacy_reads() -> None:
    chart_bars = [
        Bar("CL", datetime(2026, 1, 1, 14, 0, tzinfo=UTC), 100, 101, 99, 100, 1, "5m"),
        Bar("CL", datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 101, 102, 100, 101, 1, "5m"),
    ]
    mtf_bar = Bar("CL", datetime(2026, 1, 1, 14, 1, tzinfo=UTC), 100, 101, 99, 100, 1, "1m")
    calls: list[tuple[str, str, tuple[str, ...]]] = []
    read_windows: list[dict[str, tuple[datetime | None, datetime | None, int | None]]] = []

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_futures_canonical_bars_multi(
            self, *, provider: str, instrument_id: str, timeframes, **_kwargs
        ):
            requested = tuple(timeframes)
            calls.append((provider, instrument_id, requested))
            read_windows.append(dict(_kwargs["timeframe_read_windows"]))
            return {
                timeframe: {"5m": chart_bars, "1m": [mtf_bar]}[timeframe] for timeframe in requested
            }

        def read_bars_multi(self, **_kwargs):
            raise AssertionError("futures snapshot context must not read legacy bars")

        def _bar_slot_map_for_canonical_timestamps(self, **kwargs):
            timestamps = tuple(kwargs["timestamps"])
            step = int(kwargs["step"])
            return ProviderBarSlotMap(
                {timestamp: index * step for index, timestamp in enumerate(timestamps)},
                schedule_state="verified",
            )

    payload = _Store().get_full_snapshot_context(
        interval="5m",
        bars=chart_bars,
        providers=("ibkr",),
        mtf_timeframes=("1m",),
        mtf_history_bars={"1m": 256},
        history_start=datetime(2026, 1, 1, tzinfo=UTC),
        instrument=ibkr_future_payload("CL", exchange="NYMEX"),
    )

    assert payload["mtf_context"] == {"1m": [mtf_bar]}
    assert isinstance(
        payload["mtf_context_slots"]["1m"],
        ProviderBarSlotSequence,
    )
    assert payload["mtf_context_slots"]["1m"].authoritative is True
    assert payload["mtf_context_slots"]["1m"] == [0]
    assert payload["bar_slots"] == [0, 5]
    assert read_windows[0]["1m"][2] == 256
    assert calls == [
        ("ibkr", qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX")), ("5m", "1m"))
    ]


def test_full_snapshot_context_skips_all_mtf_readers_for_one_minute_futures() -> None:
    chart_bars = [
        Bar("ES", datetime(2026, 1, 1, 14, 0, tzinfo=UTC), 100, 101, 99, 100, 1, "1m"),
        Bar("ES", datetime(2026, 1, 1, 14, 1, tzinfo=UTC), 101, 102, 100, 101, 1, "1m"),
    ]

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_futures_canonical_bars_multi(self, *, timeframes, **_kwargs):
            assert tuple(timeframes) == ("1m",)
            return {"1m": chart_bars}

        def read_bars_multi(self, **_kwargs):
            raise AssertionError("futures snapshot must never read legacy bars")

        def _bar_slot_map_for_canonical_timestamps(self, **_kwargs):
            return ProviderBarSlotMap(
                {bar.ts: index for index, bar in enumerate(chart_bars)},
                schedule_state="verified",
            )

    payload = _Store().get_full_snapshot_context(
        interval="1m",
        bars=chart_bars,
        providers=("ibkr",),
        mtf_timeframes=(),
        history_start=datetime(2026, 1, 1, tzinfo=UTC),
        instrument=ibkr_future_payload("ES", exchange="CME"),
    )

    assert payload["mtf_context"] == {}
    assert payload["bar_slots"] == [0, 1]


def test_full_snapshot_context_rebuilds_virtual_three_minute_bars_from_one_minute_storage() -> None:
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    source_bars = [
        Bar(
            "BTC-USD",
            start + timedelta(minutes=offset),
            100.0 + offset,
            101.0 + offset,
            99.0 + offset,
            100.5 + offset,
            1.0,
            "1m",
            "coinbase",
        )
        for offset in range(5)
    ]
    stale_confirmed = Bar(
        "BTC-USD",
        start,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        "3m",
        "derived:1m",
    )
    live_tail = Bar(
        "BTC-USD",
        start + timedelta(minutes=3),
        103.0,
        108.0,
        102.0,
        107.0,
        4.0,
        "3m",
        "coinbase:chart-stream",
        closed=False,
    )
    read_timeframes: list[tuple[str, ...]] = []

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_bars_multi(self, **kwargs):
            read_timeframes.append(tuple(kwargs["timeframes"]))
            return {("coinbase", "1m"): source_bars}

        def _bar_slot_map_for_canonical_timestamps(self, **kwargs):
            timeframe = str(kwargs["timeframe"])
            return ProviderBarSlotMap(
                {
                    timestamp: continuous_provider_bar_slot(timestamp, timeframe)
                    for timestamp in kwargs["timestamps"]
                },
                schedule_state="continuous",
            )

    payload = _Store().get_full_snapshot_context(
        interval="3m",
        bars=[stale_confirmed, live_tail],
        providers=("coinbase",),
        mtf_timeframes=("1m",),
        mtf_history_bars={"1m": 64},
        history_start=start,
        instrument=coinbase_btc_payload(),
        analysis_as_of_utc=start + timedelta(minutes=4, seconds=30),
    )

    confirmed, projected_live = payload["bars"]
    assert read_timeframes == [("1m",)]
    assert confirmed.timeframe == "3m"
    assert confirmed.ts == start
    assert confirmed.open == 100.0
    assert confirmed.high == 103.0
    assert confirmed.low == 99.0
    assert confirmed.close == 102.5
    assert confirmed.volume == 3.0
    assert confirmed.closed is True
    assert projected_live is live_tail
    assert payload["bar_slots"] == [
        continuous_provider_bar_slot(start, "3m"),
        continuous_provider_bar_slot(start + timedelta(minutes=3), "3m"),
    ]


def test_full_snapshot_context_replaces_stale_confirmed_bars_but_keeps_live_tail() -> None:
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    stale_confirmed = [
        Bar("ES", start + timedelta(minutes=offset), 100, 101, 99, 100, 1, "5m")
        for offset in (0, 5, 10)
    ]
    live_tail = Bar(
        "ES",
        start + timedelta(minutes=15),
        101,
        102,
        100,
        101,
        1,
        "5m",
        closed=False,
    )
    current_canonical = [stale_confirmed[0], stale_confirmed[2]]
    slot_inputs: list[tuple[datetime, ...]] = []

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 3.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_futures_canonical_bars_multi(self, *, timeframes, **_kwargs):
            assert tuple(timeframes) == ("5m",)
            return {"5m": current_canonical}

        def _bar_slot_map_for_canonical_timestamps(self, **kwargs):
            timestamps = tuple(kwargs["timestamps"])
            slot_inputs.append(timestamps)
            return ProviderBarSlotMap(
                {
                    current_canonical[0].ts: 0,
                    current_canonical[1].ts: 10,
                },
                schedule_state="verified",
            )

    payload = _Store().get_full_snapshot_context(
        interval="5m",
        bars=[*stale_confirmed, live_tail],
        providers=("ibkr",),
        mtf_timeframes=(),
        instrument=ibkr_future_payload("ES"),
    )

    assert payload["bars"] == [*current_canonical, live_tail]
    assert payload["bar_slots"] == [0, 10, 15]
    assert slot_inputs == [tuple(bar.ts for bar in current_canonical)]


def test_bar_slot_cache_does_not_publish_reader_from_invalidated_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    results: list[ProviderBarSlotMap] = []
    timestamp = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    def blocked_slot_map(_store, timestamps, **_kwargs):
        started.set()
        assert release.wait(timeout=1.0)
        return ProviderBarSlotMap(
            {timestamps[0]: 0},
            schedule_state="verified",
        )

    monkeypatch.setattr(
        bars_repo_module,
        "_provider_schedule_bar_slot_map",
        blocked_slot_map,
    )

    class _Store(BarsRepoMixin, PostgresConnectionMixin):
        pass

    store = _Store(None, schema_owner=False)
    with store._bar_slots_cache_lock:
        read_generation = store._bar_slots_cache_generation

    def read_old_generation() -> None:
        try:
            results.append(
                store._bar_slot_map_for_canonical_timestamps(
                    timeframe="5m",
                    provider="ibkr",
                    start=timestamp,
                    end=timestamp,
                    step=5,
                    instrument=ibkr_future_payload("ES"),
                    timestamps=(timestamp,),
                    cache_generation=read_generation,
                )
            )
        except BaseException as exc:
            failures.append(exc)

    reader = threading.Thread(target=read_old_generation)
    reader.start()
    assert started.wait(timeout=1.0)
    store._invalidate_bar_slots_cache()
    release.set()
    reader.join(timeout=1.0)

    assert not reader.is_alive()
    assert failures == []
    assert results == [ProviderBarSlotMap({timestamp: 0}, schedule_state="verified")]
    assert store._bar_slots_cache_generation == read_generation + 1
    assert store._bar_slots_cache == {}


def test_recent_bars_batch_coalesces_canonical_futures_timeframes() -> None:
    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    one_minute = [
        Bar("CL", datetime(2026, 1, 1, 14, minute, tzinfo=UTC), 100, 101, 99, 100, 1, "1m")
        for minute in range(3)
    ]
    five_minute = [
        Bar("CL", datetime(2026, 1, 1, 14, minute, tzinfo=UTC), 100, 101, 99, 100, 1, "5m")
        for minute in (0, 5)
    ]
    calls: list[tuple[str, ...]] = []

    class _Store(BarsRepoMixin):
        def read_futures_canonical_bars_multi(self, *, timeframes, **_kwargs):
            requested = tuple(timeframes)
            calls.append(requested)
            return {
                timeframe: {"1m": one_minute, "5m": five_minute}[timeframe]
                for timeframe in requested
            }

    result = _Store().read_recent_bars_batch(
        (
            ("1m", "ibkr", 2, instrument),
            ("5m", "ibkr", 1, instrument),
        )
    )

    instrument_id = qualified_instrument_id(instrument)
    assert calls == [("1m", "5m")]
    assert result[(instrument_id, "1m")] == one_minute[-2:]
    assert result[(instrument_id, "5m")] == five_minute[-1:]


def test_recent_bars_batch_preserves_complete_empty_futures_timeframes() -> None:
    instrument = ibkr_future_payload("CL", exchange="NYMEX")

    class _Store(BarsRepoMixin):
        def read_futures_canonical_bars_multi(self, *, timeframes, **_kwargs):
            return {timeframe: [] for timeframe in timeframes}

    result = _Store().read_recent_bars_batch(
        (
            ("1m", "ibkr", 2, instrument),
            ("5m", "ibkr", 1, instrument),
        )
    )

    instrument_id = qualified_instrument_id(instrument)
    assert result == {
        (instrument_id, "1m"): [],
        (instrument_id, "5m"): [],
    }


def test_recent_bars_batch_rejects_incomplete_futures_timeframe_membership() -> None:
    instrument = ibkr_future_payload("CL", exchange="NYMEX")

    class _Store(BarsRepoMixin):
        def read_futures_canonical_bars_multi(self, **_kwargs):
            return {"1m": []}

    with pytest.raises(RuntimeError, match="incomplete timeframes"):
        _Store().read_recent_bars_batch(
            (
                ("1m", "ibkr", 2, instrument),
                ("5m", "ibkr", 1, instrument),
            )
        )


@pytest.mark.parametrize("limit", [True, 0, "2"])
def test_recent_bar_storage_rejects_noncanonical_limits(limit: object) -> None:
    store = BarsRepoMixin()
    instrument = ibkr_stock_payload("SPY")

    with pytest.raises(ValueError, match="RECENT_BAR_LIMIT_INVALID"):
        store.read_recent_bars(  # type: ignore[arg-type]
            "5m",
            "ibkr",
            limit=limit,
            instrument=instrument,
        )
    with pytest.raises(ValueError, match="RECENT_BAR_BATCH_LIMIT_INVALID"):
        store.read_recent_bars_batch(  # type: ignore[list-item]
            [("5m", "ibkr", limit, instrument)]
        )


def test_recent_bars_batch_applies_exact_route_limits_inside_sql() -> None:
    source = inspect.getsource(BarsRepoMixin.read_recent_bars_batch)

    assert "WITH requested" in source
    assert "CROSS JOIN LATERAL" in source
    assert "ORDER BY bars.ts DESC" in source
    assert "LIMIT requested.bar_limit" in source
    assert "ROW_NUMBER() OVER" not in source


def test_full_snapshot_context_reads_current_provider_mtf_rows() -> None:
    chart_bar = _bar("2026-01-01T14:00:00+00:00")
    provider_bar = _bar("2026-01-01T13:59:00+00:00", close=101.0, timeframe="1m")
    read_windows: list[dict[str, tuple[datetime | None, datetime | None, int | None]]] = []

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_bars_multi(self, **_kwargs):
            read_windows.append(dict(_kwargs["timeframe_read_windows"]))
            return {
                ("ibkr", "5m"): [chart_bar],
                ("ibkr", "1m"): [provider_bar],
            }

        def _bar_slot_map_for_canonical_timestamps(self, **kwargs):
            timestamps = tuple(kwargs["timestamps"])
            step = int(kwargs["step"])
            return ProviderBarSlotMap(
                {timestamp: index * step for index, timestamp in enumerate(timestamps)},
                schedule_state="verified",
            )

    payload = _Store().get_full_snapshot_context(
        interval="5m",
        bars=[chart_bar],
        providers=("ibkr",),
        mtf_timeframes=("1m",),
        mtf_history_bars={"1m": 64},
        history_start=datetime(2026, 1, 1, tzinfo=UTC),
        instrument=ibkr_stock_payload("SPY"),
    )

    assert payload["mtf_context"]["1m"] == [provider_bar]
    assert payload["mtf_context_slots"]["1m"] == [0]
    assert payload["mtf_context_slots"]["1m"].schedule_state == "verified"
    assert read_windows[0]["1m"][2] == 64


def test_full_snapshot_context_admits_only_child_bars_closed_by_as_of() -> None:
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    chart_bar = Bar(
        "BTC",
        start,
        100.0,
        101.0,
        99.0,
        100.0,
        1.0,
        "5m",
        "coinbase",
    )
    micro_bars = [
        Bar(
            "BTC",
            start + timedelta(minutes=offset),
            100.0,
            101.0,
            99.0,
            100.0,
            1.0,
            "1m",
            "coinbase",
        )
        for offset in range(3)
    ]
    read_windows: list[dict[str, tuple[datetime | None, datetime | None, int | None]]] = []

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_bars_multi(self, **_kwargs):
            read_windows.append(dict(_kwargs["timeframe_read_windows"]))
            return {
                ("coinbase", "5m"): [chart_bar],
                ("coinbase", "1m"): micro_bars,
            }

        def _bar_slot_map_for_canonical_timestamps(self, **kwargs):
            timeframe = str(kwargs["timeframe"])
            return ProviderBarSlotMap(
                {
                    timestamp: continuous_provider_bar_slot(
                        timestamp,
                        timeframe,
                    )
                    for timestamp in kwargs["timestamps"]
                },
                schedule_state="continuous",
            )

    payload = _Store().get_full_snapshot_context(
        interval="5m",
        bars=[chart_bar],
        providers=("coinbase",),
        mtf_timeframes=("1m",),
        history_start=start,
        instrument=coinbase_btc_payload(),
        include_mtf_quality=True,
        analysis_as_of_utc=start + timedelta(minutes=2, seconds=30),
    )

    assert payload["mtf_context"]["1m"] == micro_bars[:2]
    assert payload["mtf_context_slots"]["1m"] == [
        continuous_provider_bar_slot(bar.ts, "1m") for bar in micro_bars[:2]
    ]
    assert read_windows[0]["1m"][1] == start + timedelta(minutes=2)
    quality = payload["mtf_context_quality"]["1m"]
    assert quality["coverage_end_exclusive"] == (start + timedelta(minutes=2)).isoformat()
    assert quality["snapshot_as_of_ts"] == (start + timedelta(minutes=2, seconds=30)).isoformat()
    assert quality["provider_complete"] is False
    assert quality["observed_bar_count"] == 2


@pytest.mark.parametrize(
    ("range_covered", "expected_complete"),
    (
        (True, True),
        (False, False),
    ),
)
def test_full_snapshot_context_builds_exact_provider_mtf_range_quality(
    range_covered: bool,
    expected_complete: bool,
) -> None:
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    chart_bar = Bar(
        "SPY",
        start,
        100.0,
        101.0,
        99.0,
        100.0,
        1.0,
        "5m",
        "ibkr",
    )
    micro_bars = [
        Bar(
            "SPY",
            start + timedelta(minutes=offset),
            100.0,
            101.0,
            99.0,
            100.0,
            1.0,
            "1m",
            "ibkr",
        )
        for offset in (0, 1, 3, 4)
    ]
    read_ends: list[datetime] = []
    coverage_reads: list[tuple[datetime, datetime]] = []

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_bars_multi(self, *, end: datetime, **_kwargs):
            read_ends.append(end)
            return {
                ("ibkr", "5m"): [chart_bar],
                ("ibkr", "1m"): micro_bars,
            }

        def read_history_coverage(
            self,
            contract,
            *,
            starts_at: datetime,
            ends_at: datetime,
        ):
            coverage_reads.append((starts_at, ends_at))
            covered_to = ends_at if range_covered else starts_at + timedelta(minutes=2)
            return (
                HistoryCoverageReceipt(
                    contract=contract,
                    covered_from=starts_at,
                    covered_to=covered_to,
                    response_count=len(micro_bars),
                    completed_at=ends_at,
                ),
            )

        def _bar_slot_map_for_canonical_timestamps(self, **kwargs):
            timeframe = str(kwargs["timeframe"])
            return ProviderBarSlotMap(
                {
                    timestamp: continuous_provider_bar_slot(
                        timestamp,
                        timeframe,
                    )
                    for timestamp in kwargs["timestamps"]
                },
                schedule_state="continuous",
            )

    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    payload = _Store().get_full_snapshot_context(
        interval="5m",
        bars=[chart_bar],
        providers=("ibkr",),
        mtf_timeframes=("1m",),
        history_start=start,
        instrument=instrument,
        include_mtf_quality=True,
    )

    quality = payload["mtf_context_quality"]["1m"]
    assert read_ends == [start + timedelta(minutes=4)]
    assert coverage_reads == [(start, start + timedelta(minutes=5))]
    assert quality["contract"] == "provider-mtf-range-quality-v2"
    assert quality["provider"] == route.provider
    assert quality["instrument_id"] == route.instrument_id
    assert quality["route_fingerprint"] == route.fingerprint
    assert quality["timeframe"] == "1m"
    assert quality["provider_complete"] is expected_complete
    assert quality["slot_axis_authoritative"] is True
    assert quality["slot_count"] == len(micro_bars)
    assert quality["snapshot_as_of_ts"] == (start + timedelta(minutes=5)).isoformat()
    assert quality["eligible_ranges"] == [
        {
            "from": start.isoformat(),
            "to": (start + timedelta(minutes=5 if range_covered else 2)).isoformat(),
        }
    ]
    assert quality["eligible_range_count"] == 1
    assert quality["observed_bar_count"] == len(micro_bars)


def test_future_root_mtf_quality_exposes_unsupported_coverage_without_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    start = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    parent_bar = _bar(start.isoformat(), timeframe="5m")
    micro_bar = _bar(start.isoformat(), timeframe="1m")

    class _Store:
        @staticmethod
        def read_history_coverage(*_args, **_kwargs):
            raise AssertionError("unsupported provider identity must skip coverage storage")

    caplog.set_level("ERROR", logger=bars_repo_module.__name__)
    quality = bars_repo_module._provider_mtf_context_quality(
        _Store(),
        [parent_bar],
        {"1m": [micro_bar]},
        instrument=ibkr_future_payload("ES"),
        timeframes=("1m",),
    )["1m"]

    assert quality["provider_complete"] is False
    assert quality["observed_bar_count"] == 1
    assert quality["coverage_state"] == "unsupported"
    assert quality["coverage_error_code"] == (
        "PROVIDER_RESPONSE_HISTORY_EXACT_COVERAGE_UNSUPPORTED"
    )
    assert caplog.records == []


def test_provider_history_does_not_hardcode_primary_provider_name() -> None:
    source = Path("src/aef_terminal/data/provider_history.py").read_text(encoding="utf-8")

    assert '"ibkr"' not in source
    assert "read_recent_ibkr_cache" not in source
    assert "expand_sparse_ibkr_cache" not in source
    assert "route_instrument(" not in source
    assert "from aef_terminal.data.providers" not in source
    assert "adapter.db_providers" in source
    assert "or (route.provider,)" not in source


def test_read_provider_history_returns_collapsed_bars_from_multi_read() -> None:
    store = _FakeStore(
        {
            ("ibkr", "5m"): [_bar("2026-01-01T14:00:00+00:00"), _bar("2026-01-01T14:05:00+00:00")],
        }
    )
    bars = read_provider_history(
        store,
        ibkr_stock_payload("SPY"),
        "5m",
        "1d",
    )
    assert len(bars) == 2
    assert bars[0].close == 100.0


def test_read_provider_history_translates_half_open_window_to_inclusive_storage_end() -> None:
    starts_at = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=5)
    observed: dict[str, datetime] = {}

    class Store:
        @staticmethod
        def read_bars_multi(*, timeframes, providers, start, end, instrument):
            _ = timeframes, providers, instrument
            observed["start"] = start
            observed["end"] = end
            return {}

    bars = read_provider_history(
        Store(),
        ibkr_stock_payload("SPY"),
        "5m",
        "1d",
        window=HistoryRangeWindow("1d", starts_at, ends_at),
    )

    assert bars == []
    assert observed == {
        "start": starts_at,
        "end": ends_at - timedelta(microseconds=1),
    }


def test_read_provider_history_reads_current_provider_rows() -> None:
    provider_bar = _bar("2026-01-01T14:00:00+00:00", close=101.0)
    store = _FakeStore({("ibkr", "5m"): [provider_bar]})

    bars = read_provider_history(
        store,
        ibkr_stock_payload("SPY"),
        "5m",
        "1d",
    )

    assert bars == [provider_bar]
    assert store.multi_calls == [(("5m",), ("ibkr",))]


def test_read_provider_history_uses_canonical_futures_series_without_legacy_fallback() -> None:
    canonical = [_bar("2026-01-01T14:00:00+00:00"), _bar("2026-01-01T14:05:00+00:00")]
    calls: list[tuple[str, str, str]] = []

    class _Store:
        def read_futures_canonical_bars(
            self, *, provider: str, instrument_id: str, timeframe: str, start: datetime, **_route
        ):
            calls.append((provider, instrument_id, timeframe))
            return canonical if timeframe == "5m" else []

        def read_bars_multi(self, **_kwargs):
            raise AssertionError("futures canonical runtime read must not use legacy bars")

        def read_bars(self, **_kwargs):
            raise AssertionError("futures canonical runtime read must not use legacy bars")

    bars = read_provider_history(
        _Store(),
        ibkr_future_payload("CL", exchange="NYMEX"),
        "5m",
        "1d",
    )

    assert len(bars) == 2
    cl_id = qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX"))
    assert calls == [("ibkr", cl_id, "5m")]


def test_read_provider_history_dispatches_exact_futures_contract_storage(monkeypatch) -> None:
    expected = [_bar("2026-01-01T14:00:00+00:00")]
    instrument = ibkr_future_payload("ES")
    history_route = CanonicalHistoryRoute.exact_futures_contract("contract-uid")
    routed = SimpleNamespace(
        provider="ibkr",
        instrument_id="ibkr|contract|contract-uid",
        fingerprint="ibkr|contract|contract-uid",
        instrument_key="SBERF6",
        provider_symbol="SBERF6",
        instrument=instrument,
        adapter=SimpleNamespace(
            key="ibkr",
            db_providers=("ibkr",),
            canonical_history_route=lambda _instrument: history_route,
        ),
    )
    calls: list[dict[str, object]] = []

    class _Store:
        def read_futures_contract_bars(self, **kwargs):
            calls.append(kwargs)
            return expected

        def read_futures_canonical_bars(self, **_kwargs):
            raise AssertionError("exact contract must not read continuous futures storage")

        def read_bars_multi(self, **_kwargs):
            raise AssertionError("exact contract must not read generic bars")

    bars = read_provider_history(
        _Store(),
        instrument,
        "5m",
        "1d",
        adapter=routed.adapter,
    )

    assert bars == expected
    assert calls[0]["contract_key"] == "contract-uid"
    assert "series_type" not in calls[0]
    assert "roll_policy" not in calls[0]


def test_read_provider_history_does_not_mix_legacy_rows_when_futures_canonical_is_empty() -> None:
    class _Store:
        def read_futures_canonical_bars(self, **_kwargs):
            return []

        def read_bars_multi(self, **_kwargs):
            raise AssertionError("empty futures canonical history must not fallback to legacy bars")

        def read_bars(self, **_kwargs):
            raise AssertionError("empty futures canonical history must not fallback to legacy bars")

    bars = read_provider_history(_Store(), ibkr_future_payload("CL", exchange="NYMEX"), "5m", "1d")

    assert bars == []


def test_read_provider_history_requires_canonical_reader_for_ibkr_futures() -> None:
    class _Store:
        def read_bars_multi(self, **_kwargs):
            raise AssertionError("IBKR futures must fail instead of reading legacy bars")

        def read_bars(self, **_kwargs):
            raise AssertionError("IBKR futures must fail instead of reading legacy bars")

    with pytest.raises(AttributeError, match="read_futures_canonical_bars"):
        read_provider_history(_Store(), ibkr_future_payload("CL", exchange="NYMEX"), "5m", "1d")


def test_read_provider_history_never_substitutes_finer_bars_for_authoritative_target_timeframe() -> (
    None
):
    store = _FakeStore(
        {
            ("ibkr", "15m"): [
                _bar("2026-06-17T05:45:00+00:00", close=100.0, timeframe="15m"),
                _bar("2026-06-17T06:00:00+00:00", close=101.0, timeframe="15m"),
            ],
            ("ibkr", "5m"): [
                _bar("2026-06-29T19:00:00+00:00", close=200.0, timeframe="5m"),
                _bar("2026-06-29T19:05:00+00:00", close=201.0, timeframe="5m"),
                _bar("2026-06-29T19:10:00+00:00", close=202.0, timeframe="5m"),
            ],
        }
    )

    bars = read_provider_history(
        store,
        ibkr_stock_payload("SPY"),
        "15m",
        "2mo",
    )

    assert [bar.ts for bar in bars] == [
        datetime(2026, 6, 17, 5, 45, tzinfo=UTC),
        datetime(2026, 6, 17, 6, 0, tzinfo=UTC),
    ]
    assert all(bar.timeframe == "15m" for bar in bars)


def test_read_provider_history_keeps_ibkr_opening_partial_without_wrong_phase_finer_bucket() -> (
    None
):
    store = _FakeStore(
        {
            ("ibkr", "60m"): [
                _bar("2026-07-16T13:30:00+00:00", close=100.0, timeframe="60m"),
            ],
            ("ibkr", "15m"): [
                _bar("2026-07-16T13:30:00+00:00", close=200.0, timeframe="15m"),
                _bar("2026-07-16T13:45:00+00:00", close=202.0, timeframe="15m"),
            ],
        }
    )

    bars = read_provider_history(
        store,
        ibkr_stock_payload("SPY"),
        "60m",
        "2mo",
    )

    assert len(bars) == 1
    assert bars[0].ts == datetime(2026, 7, 16, 13, 30, tzinfo=UTC)
    assert bars[0].close == 100.0
    assert all(bar.ts != datetime(2026, 7, 16, 13, 0, tzinfo=UTC) for bar in bars)


def test_read_provider_history_returns_empty_without_store() -> None:
    assert read_provider_history(None, ibkr_future_payload("ES"), "5m", "1d") == []


def test_explicit_provider_history_delegates_to_provider_storage() -> None:
    store = _FakeStore({("ibkr", "5m"): [_bar("2026-01-01T14:00:00+00:00")]})
    bars = read_provider_history(
        store,
        ibkr_stock_payload("SPY"),
        "5m",
        "1d",
    )
    assert len(bars) == 1


def test_read_provider_mtf_context_loads_finer_timeframes() -> None:
    store = _FakeStore(
        {
            ("ibkr", "5m"): [_bar("2026-01-01T14:00:00+00:00")],
            ("ibkr", "1m"): [_bar("2026-01-01T14:01:00+00:00")],
        }
    )
    context = read_provider_mtf_context(
        store,
        ibkr_stock_payload("SPY"),
        "15m",
        "1d",
    )
    assert set(context.keys()) == {"5m", "1m"}
    assert len(context["5m"]) == 1


def test_read_provider_mtf_context_requires_canonical_reader_for_futures() -> None:
    class _Store:
        def read_bars_multi(self, **_kwargs):
            raise AssertionError("futures MTF context must fail instead of reading legacy bars")

        def read_bars(self, **_kwargs):
            raise AssertionError("futures MTF context must fail instead of reading legacy bars")

    with pytest.raises(AttributeError, match="read_futures_canonical_bars"):
        read_provider_mtf_context(
            _Store(),
            ibkr_future_payload("CL", exchange="NYMEX"),
            "15m",
            "1d",
        )


def test_read_provider_mtf_context_returns_empty_without_store() -> None:
    assert read_provider_mtf_context(None, ibkr_future_payload("ES"), "15m", "1d") == {}


def test_provider_backed_bar_slots_returns_none_without_store() -> None:
    bars = [
        _bar("2026-01-01T14:00:00+00:00"),
        _bar("2026-01-01T14:05:00+00:00"),
        _bar("2026-01-01T14:10:00+00:00"),
    ]
    assert provider_backed_bar_slots(None, bars, instrument=ibkr_future_payload("ES")) is None


def test_provider_backed_bar_slots_use_store_mapping_when_available() -> None:
    bars = [
        _bar("2026-01-01T14:00:00+00:00"),
        _bar("2026-01-01T14:05:00+00:00"),
        _bar("2026-01-01T14:10:00+00:00"),
    ]

    class _Store:
        def read_bar_slots(self, **kwargs):
            assert kwargs["provider"] == "ibkr"
            assert "symbol" not in kwargs
            assert kwargs["timeframe"] == "5m"
            assert kwargs["step"] == 5
            return ProviderBarSlotMap(
                {
                    bars[0].ts: 1000,
                    bars[1].ts: 1005,
                    bars[2].ts: 1010,
                },
                schedule_state="verified",
            )

    with pytest.raises(
        RuntimeError,
        match="FULL_SNAPSHOT_CONTEXT_REQUIRED_FOR_PROVIDER_BAR_SLOTS",
    ):
        provider_backed_bar_slots(_Store(), bars, instrument=ibkr_future_payload("ES"))


def test_read_bar_slots_uses_canonical_futures_without_legacy_reads() -> None:
    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0
            self.calls: list[tuple[str, str, str, datetime, datetime]] = []
            self.bars = [
                Bar("CL", datetime(2026, 1, 1, 13, 55, tzinfo=UTC), 100, 101, 99, 100, 1, "5m"),
                Bar("CL", datetime(2026, 1, 1, 14, 0, tzinfo=UTC), 101, 102, 100, 101, 1, "5m"),
                Bar("CL", datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 102, 103, 101, 102, 1, "5m"),
            ]

        def read_futures_canonical_bars(
            self,
            *,
            provider: str,
            instrument_id: str,
            timeframe: str,
            start: datetime,
            end: datetime,
            **_route,
        ) -> list[Bar]:
            self.calls.append((provider, instrument_id, timeframe, start, end))
            return [bar for bar in self.bars if start <= bar.ts <= end]

        @staticmethod
        def read_trading_session_axis_anchor(**_kwargs):
            return None

        def _connect(self):
            raise AssertionError("futures bar slots must not read legacy bars")

    store = _Store()
    slots = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
        end=datetime(2026, 1, 1, 14, 5, tzinfo=UTC),
        step=5,
        instrument=ibkr_future_payload("CL", exchange="NYMEX"),
    )

    assert slots == {
        datetime(2026, 1, 1, 14, 0, tzinfo=UTC): 0,
        datetime(2026, 1, 1, 14, 5, tzinfo=UTC): 5,
    }
    assert slots.authoritative is False
    assert store.calls == [
        (
            "ibkr",
            qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX")),
            "5m",
            datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
            datetime(2026, 1, 1, 14, 5, tzinfo=UTC),
        )
    ]


def test_continuous_bar_slots_are_absolute_and_include_provider_gap_positions() -> None:
    first = datetime(2026, 5, 8, 1, 15, tzinfo=UTC)
    second = datetime(2026, 5, 8, 7, 45, tzinfo=UTC)

    class _Cursor:
        def __init__(self, store: "_Store") -> None:
            self.store = store
            self.rows: list[tuple[datetime]] = []

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            assert "count(*)" not in sql
            self.store.queries += 1
            start, end = params[3], params[4]
            self.rows = [(ts,) for ts in self.store.timestamps if start <= ts <= end]

        def fetchall(self) -> list[tuple[datetime]]:
            return list(self.rows)

    class _Connection:
        def __init__(self, store: "_Store") -> None:
            self.store = store

        def __enter__(self) -> "_Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor(self.store)

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0
            self.timestamps = [first, second]
            self.queries = 0

        def _connect(self) -> _Connection:
            return _Connection(self)

    instrument = coinbase_btc_payload()
    store = _Store()
    slots = store.read_bar_slots(
        timeframe="5m",
        provider="coinbase",
        start=first,
        end=second,
        step=5,
        instrument=instrument,
    )
    second_only = store.read_bar_slots(
        timeframe="5m",
        provider="coinbase",
        start=second,
        end=second,
        step=5,
        instrument=instrument,
    )

    assert slots[first] == continuous_provider_bar_slot(first, "5m")
    assert slots[second] == continuous_provider_bar_slot(second, "5m")
    assert slots[second] - slots[first] == 390
    assert second_only[second] == slots[second]
    assert store.queries == 2


def test_continuous_bar_slot_rejects_unaligned_timestamp() -> None:
    with pytest.raises(ValueError, match="not aligned"):
        continuous_provider_bar_slot(
            datetime(2026, 5, 8, 1, 16, tzinfo=UTC),
            "5m",
        )


def test_continuous_bar_slot_supports_provider_history_before_2022() -> None:
    first = datetime(2021, 7, 1, 0, 0, tzinfo=UTC)
    second = first + timedelta(minutes=5)

    assert continuous_provider_bar_slot(first, "5m") >= 0
    assert (
        continuous_provider_bar_slot(second, "5m") - continuous_provider_bar_slot(first, "5m") == 5
    )


def test_forming_continuous_bar_uses_absolute_slot_before_storage_commit() -> None:
    first = datetime(2026, 5, 8, 1, 15, tzinfo=UTC)
    second = first + timedelta(hours=6, minutes=30)
    bars = [
        Bar("BTC-USD", first, 100, 101, 99, 100, 1, "5m", "coinbase:BTC-USD", closed=True),
        Bar("BTC-USD", second, 101, 102, 100, 101, 1, "5m", "coinbase:BTC-USD", closed=False),
    ]
    first_slot = continuous_provider_bar_slot(first, "5m")

    assert provider_backed_bar_slots(
        None,
        bars,
        instrument=coinbase_btc_payload(),
    ) == [first_slot, first_slot + 390]


def test_scheduled_bar_slots_are_visual_only_when_provider_axis_anchor_is_unknown() -> None:
    class _Cursor:
        def __init__(self, store: "_Store") -> None:
            self.store = store
            self.rows: list[tuple[Any, ...]] = []

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            self.store.window_queries += 1
            start = params[3]
            end = params[4]
            self.rows = [(ts,) for ts in self.store.timestamps if start <= ts <= end]

        def fetchone(self) -> tuple[Any, ...] | None:
            return self.rows[0] if self.rows else None

        def fetchall(self) -> list[tuple[Any, ...]]:
            return list(self.rows)

    class _Connection:
        def __init__(self, store: "_Store") -> None:
            self.store = store

        def __enter__(self) -> "_Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor(self.store)

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0
            self.window_queries = 0
            self.timestamps = [
                datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
                datetime(2026, 1, 1, 14, 5, tzinfo=UTC),
                datetime(2026, 1, 1, 14, 10, tzinfo=UTC),
                datetime(2026, 1, 1, 14, 15, tzinfo=UTC),
            ]

        def _connect(self) -> _Connection:
            return _Connection(self)

        @staticmethod
        def read_trading_session_axis_anchor(**_kwargs):
            return None

    store = _Store()
    first = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=store.timestamps[0],
        end=store.timestamps[2],
        step=5,
        instrument=ibkr_stock_payload("SPY"),
    )
    second = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=store.timestamps[0],
        end=store.timestamps[3],
        step=5,
        instrument=ibkr_stock_payload("SPY"),
    )

    assert list(first.values()) == [0, 5, 10]
    assert list(second.values()) == [0, 5, 10, 15]
    assert first.schedule_state == "unknown"
    assert first.authoritative is False
    assert store.window_queries == 2


def test_scheduled_bar_slots_do_not_define_channel_confirmed_distance() -> None:
    sessions_module._TRADING_HOURS_CACHE.clear()
    first = datetime(2026, 7, 16, 13, 30, tzinfo=UTC)
    second = first + timedelta(minutes=10)
    instrument = ibkr_stock_payload("SPY")

    class _Cursor:
        def __init__(self, store: "_Store") -> None:
            self.store = store
            self.rows: list[tuple[Any, ...]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            self.store.window_queries += 1
            start, end = params[3], params[4]
            self.rows = [(ts,) for ts in (first, second) if start <= ts <= end]

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return list(self.rows)

    class _Connection:
        def __init__(self, store: "_Store") -> None:
            self.store = store

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return _Cursor(self.store)

    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0
            self.window_queries = 0

        def _connect(self):
            return _Connection(self)

        @staticmethod
        def initialize():
            return None

        @staticmethod
        def read_trading_hours(*, instrument: dict):
            return {
                "timezone": "America/New_York",
                "payload": {"provider_contract_id": "12345"},
            }

        @staticmethod
        def read_trading_session_axis_anchor(**_kwargs):
            return first

        @staticmethod
        def read_trading_session_intervals(**_kwargs):
            return [
                {
                    "session_date": "2026-07-16",
                    "opens_at": first,
                    "closes_at": first + timedelta(minutes=15),
                    "status": "open",
                    "source_coverage_start": first,
                    "source_coverage_end": first + timedelta(minutes=15),
                }
            ]

    store = _Store()
    slots = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=first,
        end=second,
        step=5,
        instrument=instrument,
    )

    assert slots == {first: 0, second: 10}
    placeholder = first + timedelta(minutes=5)
    exact_required_slots = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=first,
        end=second,
        step=5,
        instrument=instrument,
        required_timestamps=(placeholder, first + timedelta(minutes=3)),
    )
    assert exact_required_slots == {
        first: 0,
        placeholder: 5,
        second: 10,
    }
    assert exact_required_slots.authoritative is True
    queries_before_exact_only = store.window_queries
    exact_only_slots = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=placeholder,
        end=placeholder,
        step=5,
        instrument=instrument,
        required_timestamps=(placeholder,),
        required_timestamps_only=True,
    )
    assert exact_only_slots == {placeholder: 5}
    assert exact_only_slots.authoritative is True
    assert store.window_queries == queries_before_exact_only
    expanded_from_right = store.read_bar_slots(
        timeframe="5m",
        provider="ibkr",
        start=second,
        end=second,
        step=5,
        instrument=instrument,
    )
    assert expanded_from_right[second] == slots[second]
    bars = [
        Bar("SPY", first, 100, 101, 99, 100, 100, "5m", "ibkr", closed=True),
        Bar("SPY", second, 120, 121, 119, 120, 100, "5m", "ibkr", closed=True),
    ]
    route = route_instrument(instrument)
    channel = {
        "id": "confirmed-distance",
        "type": "channel",
        "points": [
            {"ts": first.isoformat(), "price": 100.0},
            {"ts": second.isoformat(), "price": 110.0},
        ],
        "offsetPoint": {"ts": first.isoformat(), "price": 120.0},
        "anchorResolution": {"status": "resolved", "reasons": []},
        "anchorProjection": {
            "instrumentId": route.instrument_id,
            "routeFingerprint": route.fingerprint,
            "timeframe": "5m",
            "canonicalGeneration": 0,
            "originTs": first.isoformat(),
            "confirmedThroughTs": second.isoformat(),
            "confirmedCount": 2,
            "anchors": [
                {"ts": first.isoformat(), "logicalIndex": 0},
                {"ts": second.isoformat(), "logicalIndex": 1},
            ],
        },
        "showGuides": True,
    }
    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        canonical_generation=0,
    )

    assert slots == {first: 0, second: 10}
    assert context["channels"][0]["slope_per_confirmed_bar"] == 10.0
    assert context["nearest"]["role"] == "midline"
    assert context["nearest"]["price"] == 120.0
    sessions_module._TRADING_HOURS_CACHE.clear()


def test_read_recent_bars_uses_canonical_futures_without_legacy_reads() -> None:
    calls: list[tuple[str, str, str, int, bool]] = []
    canonical = [
        Bar("CL", datetime(2026, 1, 1, 14, 0, tzinfo=UTC), 100, 101, 99, 100, 1, "5m"),
        Bar("CL", datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 101, 102, 100, 101, 1, "5m"),
    ]

    class _Store(BarsRepoMixin):
        def read_futures_canonical_bars(
            self,
            *,
            provider: str,
            instrument_id: str,
            timeframe: str,
            limit: int,
            latest: bool,
            **_route,
        ) -> list[Bar]:
            calls.append((provider, instrument_id, timeframe, limit, latest))
            return canonical

        def _connect(self):
            raise AssertionError("futures recent bars must not read legacy bars")

    bars = _Store().read_recent_bars(
        "5m", "ibkr:db-cache", limit=2, instrument=ibkr_future_payload("CL", exchange="NYMEX")
    )

    assert bars == canonical
    assert calls == [
        (
            "ibkr",
            qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX")),
            "5m",
            2,
            True,
        )
    ]


def test_read_bar_boundary_uses_canonical_futures_without_legacy_reads() -> None:
    latest_bar = Bar("CL", datetime(2026, 1, 1, 14, 5, tzinfo=UTC), 101, 102, 100, 101, 1, "5m")
    earliest_bar = Bar("CL", datetime(2026, 1, 1, 14, 0, tzinfo=UTC), 100, 101, 99, 100, 1, "5m")
    calls: list[tuple[str, str, str, int, bool]] = []

    class _Store(BarsRepoMixin):
        def read_futures_canonical_bars(
            self,
            *,
            provider: str,
            instrument_id: str,
            timeframe: str,
            limit: int,
            latest: bool,
            **_route,
        ) -> list[Bar]:
            calls.append((provider, instrument_id, timeframe, limit, latest))
            return [latest_bar if latest else earliest_bar]

        def _connect(self):
            raise AssertionError("futures boundary must not read legacy bars")

    store = _Store()

    assert (
        store.read_bar_boundary(
            "5m", "ibkr", direction="latest", instrument=ibkr_future_payload("CL", exchange="NYMEX")
        )
        == latest_bar.ts
    )
    assert (
        store.read_bar_boundary(
            "5m",
            "ibkr",
            direction="earliest",
            instrument=ibkr_future_payload("CL", exchange="NYMEX"),
        )
        == earliest_bar.ts
    )
    assert calls == [
        (
            "ibkr",
            qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX")),
            "5m",
            1,
            True,
        ),
        (
            "ibkr",
            qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX")),
            "5m",
            1,
            False,
        ),
    ]


def test_storage_futures_reads_reject_an_incomplete_store_contract() -> None:
    class _Store(BarsRepoMixin):
        def __init__(self) -> None:
            self._bar_slots_cache = {}
            self._bar_slots_cache_ttl = 0.0
            self._bar_slots_cache_lock = threading.Lock()
            self._bar_slots_cache_generation = 0

        def read_bars_multi(self, **_kwargs):
            raise AssertionError("IBKR futures must fail instead of reading legacy bars")

        def _connect(self):
            raise AssertionError("IBKR futures must fail instead of opening legacy SQL")

    store = _Store()
    chart_bars = [Bar("CL", datetime(2026, 1, 1, 14, 0, tzinfo=UTC), 100, 101, 99, 100, 1, "5m")]

    with pytest.raises(AttributeError, match="read_futures_canonical_bars_multi"):
        store.get_full_snapshot_context(
            interval="5m",
            bars=chart_bars,
            providers=("ibkr",),
            mtf_timeframes=("1m",),
            instrument=ibkr_future_payload("CL", exchange="NYMEX"),
        )
    with pytest.raises(AttributeError, match="read_futures_canonical_bars"):
        store.read_recent_bars(
            "5m", "ibkr", limit=2, instrument=ibkr_future_payload("CL", exchange="NYMEX")
        )
    with pytest.raises(AttributeError, match="read_futures_canonical_bars"):
        store.read_bar_boundary(
            "5m", "ibkr", direction="latest", instrument=ibkr_future_payload("CL", exchange="NYMEX")
        )
    with pytest.raises(AttributeError, match="read_futures_canonical_bars"):
        store.read_bar_slots(
            "5m",
            "ibkr",
            chart_bars[0].ts,
            chart_bars[0].ts,
            step=5,
            instrument=ibkr_future_payload("CL", exchange="NYMEX"),
        )


def test_ibkr_keep_requested_cache_window_keeps_sufficient_bars() -> None:
    cached = [
        _bar(f"2026-01-01T{14 + minute // 60:02d}:{minute % 60:02d}:00+00:00")
        for minute in range(0, 180, 5)
    ]
    warnings: list[str] = []
    result = ibkr_keep_requested_cache_window(None, "5m", "1d", cached, warnings)
    assert result == cached
    assert warnings == []


def test_ibkr_keep_requested_cache_window_keeps_requested_window_when_sparse() -> None:
    cached = [_bar("2026-01-01T14:00:00+00:00"), _bar("2026-01-01T14:05:00+00:00")]

    warnings: list[str] = []
    result = ibkr_keep_requested_cache_window(None, "5m", "1d", cached, warnings)
    assert result == cached
    assert len(warnings) == 1
    assert "IBKR returned only 2 bars" in warnings[0]
    assert "out-of-window recent cache" in warnings[0]


def test_read_provider_history_propagates_multi_read_failure() -> None:
    class _Store:
        def read_bars_multi(self, **_kwargs):
            raise RuntimeError("multi read failed")

        def read_bars(self, **_kwargs):
            return []

    with pytest.raises(RuntimeError, match="multi read failed"):
        read_provider_history(
            _Store(),
            ibkr_stock_payload("SPY"),
            "5m",
            "1d",
        )
