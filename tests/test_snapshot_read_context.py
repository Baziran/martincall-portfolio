from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aef_terminal.data.gex.constants import (
    GEX_LIVE_SNAPSHOT_SOURCE,
    GEX_REQUEST_SNAPSHOT_SOURCE,
)
from aef_terminal.data.gex.contracts import gex_capture_revision_at
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.engine.snapshot import read_context as read_context_module
from aef_terminal.engine.snapshot.read_context import SnapshotReadContext
from aef_terminal.engine.analyze.inputs import resolve_analysis_as_of_utc
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from tests.provider_payloads import ibkr_stock_payload


def _enable_archived_option_flow_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    original = read_context_module.indicator_ids_for_shared_context

    def consumers(context_ref: str) -> tuple[str, ...]:
        if context_ref == "option_flow":
            return ("absorption_trap",)
        return original(context_ref)

    monkeypatch.setattr(read_context_module, "indicator_ids_for_shared_context", consumers)


class FakeSnapshotStore:
    def __init__(self) -> None:
        self.gex_queries: list[dict[str, Any]] = []
        self.option_targets_queries: list[dict[str, Any]] = []
        self.tick_delta_queries: list[dict[str, Any]] = []
        self.snapshot_context_queries: list[dict[str, Any]] = []
        self.initialize_count = 0

    def initialize(self) -> None:
        self.initialize_count += 1

    def read_gex_snapshots(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        limit: int = 512,
        *,
        source: str | None = None,
        sources: Sequence[str] | None = None,
        include_raw: bool = True,
    ) -> list[dict[str, Any]]:
        self.gex_queries.append(
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "start": start,
                "end": end,
                "limit": limit,
                "include_raw": include_raw,
                "source": source,
                "sources": tuple(sources or ()),
            }
        )
        exact_source = source or tuple(sources or ())[0]
        capture_mode = "live" if exact_source == GEX_LIVE_SNAPSHOT_SOURCE else "request"
        now = min(datetime.now(tz=UTC), end - timedelta(microseconds=1))
        captured_at = now.isoformat()
        capture_revision = gex_capture_revision_at(
            now,
            capture_mode=capture_mode,
        ).isoformat()
        option_volume_context = {
            "current": {
                "call_volume": None,
                "put_volume": None,
                "total_volume": None,
                "call_oi": None,
                "put_oi": None,
                "total_oi": None,
                "turnover": None,
                "rank": None,
            },
            "event": None,
        }
        return [
            {
                "captured_at": captured_at,
                "source": exact_source,
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "provider_symbol": "SPY",
                "payload": {
                    "captured_at": captured_at,
                    "capture_revision": capture_revision,
                    "source": exact_source,
                    "capture_mode": capture_mode,
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "provider_symbol": "SPY",
                    "comparison_scope": {
                        "capture_mode": capture_mode,
                        "strike_count": 1,
                        "strike_ladder": [105.0],
                        "contract_con_ids": [1, 2],
                        "expiries": ["20260724"],
                        "futures_options": False,
                        "series": [
                            {
                                "expiry": "20260724",
                                "trading_class": "SPY",
                                "exchange": "SMART",
                                "multiplier": 100.0,
                            }
                        ],
                        "risk_free_rate": 0.05,
                        "dividend_yield": 0.0,
                        "market_data_entitlement": "live",
                    },
                    "frame_complete": True,
                    "decision_authoritative": True,
                    "market_data_entitlement": "live",
                    "open_interest_as_of": "previous_settlement",
                    "spot": 100.0,
                    "gamma_flip": 100.0,
                    "levels": [
                        {
                            "price": 105.0,
                            "kind": "CALL_WALL",
                            "kind_class": "call",
                            "power_class": "EXTREME",
                            "strength": 1.0,
                            "selection_rank": 1,
                            "net_gex": 100.0,
                            "call_gex": 100.0,
                            "put_gex": 0.0,
                            "abs_gex": 100.0,
                            "abs_flow_1pt": 100.0,
                            "distance_from_spot": 5.0,
                            "zone_half_width": 0.25,
                            "spot_side": "above",
                            "option_volume_context": option_volume_context,
                        }
                    ],
                    "call_wall": 105.0,
                    "put_wall": None,
                    "net_gex": 100.0,
                    "global_gamma_regime": "POSITIVE_ESTIMATE",
                    "option_activity": None,
                    "option_activity_status": "NEED_PREVIOUS",
                },
            }
        ]

    def read_option_targets(
        self,
        timeframe: str,
        *,
        route_pairs: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        instrument_id, route_fingerprint = route_pairs[0]
        self.option_targets_queries.append(
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "timeframe": timeframe,
            }
        )
        return [
            {
                "id": "opt-1",
                "symbol": "SPY",
                "timeframe": timeframe,
                "payload": {
                    "id": "opt-1",
                    "symbol": "SPY",
                    "timeframe": timeframe,
                    "point": {
                        "price": 105.0,
                        "ts": datetime.now(tz=UTC).isoformat(),
                    },
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "provider": "ibkr",
                    "provider_contract_id": "756733",
                    "payload": {"intent": {}, "market_sample": {}},
                },
            }
        ]

    def read_tick_delta(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start: datetime,
        end: datetime,
        bucket: str,
    ) -> list[dict[str, Any]]:
        self.tick_delta_queries.append(
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "start": start,
                "end": end,
            }
        )
        return [{"bucket": start.isoformat(), "net_delta": 150.0}]

    def read_bar_slots(self, **kwargs):
        return {kwargs["start"].astimezone(UTC): 0}

    def read_bars_multi(self, **_kwargs):
        return {}

    def get_full_snapshot_context(self, **kwargs: Any) -> dict[str, Any]:
        self.snapshot_context_queries.append(dict(kwargs))
        bars = list(kwargs["bars"])
        return {
            "bars": bars,
            "bar_slots": ProviderBarSlotSequence(
                list(range(len(bars))),
                schedule_state="verified",
            ),
            "mtf_context": {},
            "mtf_context_slots": {},
            "mtf_context_quality": (
                {
                    "1m": {
                        "contract": "provider-mtf-range-quality-v2",
                    }
                }
                if kwargs.get("include_mtf_quality") is True
                else {}
            ),
        }


def test_direct_analysis_clock_is_deterministic_latest_bar_end() -> None:
    latest_ts = datetime(2026, 7, 20, 13, 30, tzinfo=UTC)
    bars = [Bar("SPY", latest_ts, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")]

    first = resolve_analysis_as_of_utc(bars, None)
    second = resolve_analysis_as_of_utc(bars, None)

    assert first == latest_ts + timedelta(minutes=5)
    assert second == first
    assembly_source = Path("src/aef_terminal/engine/snapshot/assembly.py").read_text(
        encoding="utf-8"
    )
    assert "datetime.now" not in assembly_source


@pytest.mark.parametrize(
    ("capture_mode", "snapshot_source"),
    (
        ("request", GEX_REQUEST_SNAPSHOT_SOURCE),
        ("live", GEX_LIVE_SNAPSHOT_SOURCE),
    ),
)
def test_snapshot_read_context_keeps_analysis_pair_and_option_flow_in_selected_lane(
    capture_mode: str,
    snapshot_source: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_archived_option_flow_consumer(monkeypatch)
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    bars = [Bar("SPY", now - timedelta(minutes=5), 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")]

    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=bars,
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
        gex_capture_mode=capture_mode,
        indicator_params={
            "absorption_trap": {"enabled": True, "option_flow_hours": 6},
        },
    )
    ctx.prefetch_sync()

    history = ctx.get_gex_history()
    ctx.get_option_flow()

    assert len(store.gex_queries) == 1
    assert store.gex_queries[0]["source"] == snapshot_source
    assert store.gex_queries[0]["sources"] == ()
    assert store.gex_queries[0]["limit"] == 720
    assert all(query["include_raw"] is False for query in store.gex_queries)
    assert (
        store.gex_queries[0]["route_fingerprint"]
        == route_instrument(ibkr_stock_payload("SPY")).fingerprint
    )
    assert len(history) >= 1


def test_shared_gex_rows_keep_analysis_projection_to_latest_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_archived_option_flow_consumer(monkeypatch)

    class MultiRowStore(FakeSnapshotStore):
        def read_gex_snapshots(
            self,
            instrument_id: str,
            route_fingerprint: str,
            start: datetime,
            end: datetime,
            limit: int = 512,
            *,
            source: str | None = None,
            sources: Sequence[str] | None = None,
            include_raw: bool = True,
        ) -> list[dict[str, Any]]:
            template = super().read_gex_snapshots(
                instrument_id,
                route_fingerprint,
                start,
                end,
                limit,
                source=source,
                sources=sources,
                include_raw=include_raw,
            )[0]
            capture_mode = template["payload"]["capture_mode"]
            rows: list[dict[str, Any]] = []
            for minutes in (15, 10, 5):
                captured_at = end - timedelta(minutes=minutes)
                row = deepcopy(template)
                row["captured_at"] = captured_at.isoformat()
                row["payload"]["captured_at"] = captured_at.isoformat()
                row["payload"]["capture_revision"] = gex_capture_revision_at(
                    captured_at,
                    capture_mode=capture_mode,
                ).isoformat()
                rows.append(row)
            return rows

    as_of = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    store = MultiRowStore()
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[
            Bar(
                "SPY",
                as_of - timedelta(minutes=5),
                100.0,
                101.0,
                99.0,
                100.5,
                10,
                "5m",
                "ibkr",
            )
        ],
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
        indicator_params={
            "absorption_trap": {"enabled": True, "option_flow_hours": 6},
        },
        analysis_as_of_utc=as_of,
    )

    ctx.prefetch_sync()
    history = ctx.get_gex_history()

    assert len(store.gex_queries) == 1
    assert [row["captured_at"] for row in history] == [
        (as_of - timedelta(minutes=10)).isoformat(),
        (as_of - timedelta(minutes=5)).isoformat(),
    ]


@pytest.mark.parametrize(
    ("as_of", "expected_start"),
    (
        (
            datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 15, 14, 0, tzinfo=UTC),
            datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
        ),
    ),
)
def test_snapshot_read_context_bounds_analysis_pair_to_new_york_day(
    as_of: datetime,
    expected_start: datetime,
) -> None:
    store = FakeSnapshotStore()
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[
            Bar(
                "SPY",
                as_of - timedelta(minutes=5),
                100.0,
                101.0,
                99.0,
                100.5,
                10,
                "5m",
                "ibkr",
            )
        ],
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
        analysis_as_of_utc=as_of,
    )

    ctx.get_gex_history()

    assert len(store.gex_queries) == 1
    assert store.gex_queries[0]["start"] == expected_start
    assert store.gex_queries[0]["end"] == as_of
    assert store.gex_queries[0]["limit"] == 2


def test_snapshot_read_context_does_not_activate_gex_from_legacy_indicator_params() -> None:
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[
            Bar(
                "SPY",
                now - timedelta(minutes=5),
                100.0,
                101.0,
                99.0,
                100.5,
                10,
                "5m",
                "ibkr",
            )
        ],
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={
            "gex_dynamics": {"enabled": True, "capture_mode": "live"},
        },
    )

    ctx.prefetch_sync()

    assert ctx.gex_needed is False
    assert ctx.get_gex_history() == []
    assert store.gex_queries == []


def test_snapshot_read_context_async_prefetch() -> None:
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    bars = [Bar("SPY", now - timedelta(minutes=5), 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")]

    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=bars,
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
        indicator_params={
            "option_reversal": {"enabled": True},
            "absorption_trap": {"enabled": True, "option_flow_hours": 6},
            "tick_flow": {"enabled": True, "tick_flow_hours": 12},
        },
    )
    asyncio.run(ctx.async_prefetch())

    assert len(store.gex_queries) == 1
    assert len(store.option_targets_queries) == 1
    assert (
        store.option_targets_queries[0]["instrument_id"]
        == route_instrument(ibkr_stock_payload("SPY")).instrument_id
    )
    assert (
        store.option_targets_queries[0]["route_fingerprint"]
        == route_instrument(ibkr_stock_payload("SPY")).fingerprint
    )
    assert len(store.tick_delta_queries) == 1

    # Ensure repeated calls do not trigger new queries
    ctx.get_gex_history()
    ctx.get_option_targets()
    ctx.get_option_flow()
    ctx.get_tick_flow()

    assert len(store.gex_queries) == 1
    assert len(store.option_targets_queries) == 1
    assert len(store.tick_delta_queries) == 1


def test_snapshot_read_context_async_prefetch_defers_cancellation_until_reads_settle(
    monkeypatch,
) -> None:
    started = (threading.Event(), threading.Event())
    release = threading.Event()
    finished = (threading.Event(), threading.Event())
    now = datetime.now(tz=UTC)
    bars = [Bar("SPY", now, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")]
    ctx = SnapshotReadContext(
        store=FakeSnapshotStore(),
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=bars,
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
    )

    def blocking_read(index: int) -> None:
        started[index].set()
        assert release.wait(timeout=2)
        finished[index].set()

    monkeypatch.setattr(ctx, "get_db_context", lambda: blocking_read(0))
    monkeypatch.setattr(ctx, "_prefetch_gex_inputs", lambda: blocking_read(1))

    async def scenario() -> None:
        task = asyncio.create_task(ctx.async_prefetch())
        for _ in range(100):
            if all(event.is_set() for event in started):
                break
            await asyncio.sleep(0.01)
        assert all(event.is_set() for event in started)

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert not any(event.is_set() for event in finished)

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(event.is_set() for event in finished)

    asyncio.run(scenario())


def test_snapshot_read_context_async_prefetch_drains_siblings_before_raising(
    monkeypatch,
) -> None:
    slow_started = threading.Event()
    release = threading.Event()
    slow_finished = threading.Event()
    now = datetime.now(tz=UTC)
    bars = [Bar("SPY", now, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")]
    ctx = SnapshotReadContext(
        store=FakeSnapshotStore(),
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=bars,
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
    )

    def failed_read() -> None:
        raise RuntimeError("snapshot read failed")

    def slow_read() -> None:
        slow_started.set()
        assert release.wait(timeout=2)
        slow_finished.set()

    monkeypatch.setattr(ctx, "get_db_context", failed_read)
    monkeypatch.setattr(ctx, "_prefetch_gex_inputs", slow_read)

    async def scenario() -> None:
        task = asyncio.create_task(ctx.async_prefetch())
        for _ in range(100):
            if slow_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert slow_started.is_set()
        await asyncio.sleep(0.01)
        assert task.done() is False

        release.set()
        with pytest.raises(RuntimeError, match="snapshot read failed"):
            await task
        assert slow_finished.is_set()

    asyncio.run(scenario())


def test_snapshot_read_context_does_not_fetch_tick_flow_when_calc_is_disabled() -> None:
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    bars = [
        Bar(
            "SPY",
            now - timedelta(minutes=5),
            100.0,
            101.0,
            99.0,
            100.5,
            10,
            "5m",
            "ibkr",
        )
    ]
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=bars,
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={
            "absorption_trap": {
                "enabled": True,
            },
            "tick_flow": {"enabled": False, "tick_flow_hours": 12},
        },
    )

    ctx.prefetch_sync()

    assert ctx.get_tick_flow() is None
    assert store.tick_delta_queries == []


def test_snapshot_read_context_rejects_coerced_option_flow_lookbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_archived_option_flow_consumer(monkeypatch)
    now = datetime.now(tz=UTC)
    bars = [
        Bar(
            "SPY",
            now - timedelta(minutes=5),
            100.0,
            101.0,
            99.0,
            100.5,
            10,
            "5m",
            "ibkr",
        )
    ]
    for invalid_hours in (True, False, 0, -1, 721, 6.0, "6"):
        ctx = SnapshotReadContext(
            store=FakeSnapshotStore(),
            symbol="SPY",
            interval="5m",
            range_="1d",
            bars=bars,
            instrument=ibkr_stock_payload("SPY"),
            indicator_params={
                "absorption_trap": {
                    "enabled": True,
                    "option_flow_hours": invalid_hours,
                },
            },
        )
        with pytest.raises(ValueError, match="integer from 1 to 720"):
            ctx.prefetch_sync()


def test_snapshot_read_context_propagates_gex_storage_contract_errors() -> None:
    class FailingGexStore(FakeSnapshotStore):
        def read_gex_snapshots(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise ValueError("corrupt GEX row")

    now = datetime.now(tz=UTC)
    ctx = SnapshotReadContext(
        store=FailingGexStore(),
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[Bar("SPY", now, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")],
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
    )

    with pytest.raises(ValueError, match="corrupt GEX row"):
        ctx.get_gex_history()


def test_snapshot_read_context_propagates_option_target_contract_errors() -> None:
    class FailingTargetStore(FakeSnapshotStore):
        def read_option_targets(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise ValueError("corrupt option target")

    now = datetime.now(tz=UTC)
    ctx = SnapshotReadContext(
        store=FailingTargetStore(),
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[Bar("SPY", now, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")],
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={"option_reversal": {"enabled": True}},
    )

    with pytest.raises(ValueError, match="corrupt option target"):
        ctx.get_option_targets()


def test_snapshot_read_context_requests_the_exact_gex_capture_lane(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def read_history(_provider_symbol: str, **kwargs: Any) -> list[dict[str, Any]]:
        observed.update(kwargs)
        return []

    monkeypatch.setattr(
        read_context_module,
        "load_gex_history_input",
        read_history,
    )
    now = datetime.now(tz=UTC)
    store = FakeSnapshotStore()
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[Bar("SPY", now, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")],
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
        gex_capture_mode="live",
    )

    assert ctx.get_gex_history() == []
    assert observed["capture_mode"] == "live"
    assert store.gex_queries == []


def test_snapshot_read_context_keeps_large_option_flow_read_in_live_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_archived_option_flow_consumer(monkeypatch)
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[Bar("SPY", now, 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")],
        instrument=ibkr_stock_payload("SPY"),
        gex_context_active=True,
        gex_capture_mode="live",
        indicator_params={
            "absorption_trap": {"enabled": True, "option_flow_hours": 24 * 30},
        },
    )

    ctx.prefetch_sync()

    assert len(store.gex_queries) == 1
    assert store.gex_queries[0]["source"] == GEX_LIVE_SNAPSHOT_SOURCE
    assert store.gex_queries[0]["limit"] == 8_642


def test_option_reversal_does_not_load_gex_history() -> None:
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    bars = [Bar("SPY", now - timedelta(minutes=5), 100.0, 101.0, 99.0, 100.5, 10, "5m", "ibkr")]
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=bars,
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={"option_reversal": {"enabled": True}},
    )

    ctx.prefetch_sync()

    assert store.gex_queries == []
    assert len(store.option_targets_queries) == 1


@pytest.mark.parametrize(
    ("rounded_enabled", "quality_expected"),
    (
        (False, False),
        (True, True),
    ),
)
def test_snapshot_read_context_gates_provider_mtf_quality_to_rounded_calc(
    rounded_enabled: bool,
    quality_expected: bool,
) -> None:
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[
            Bar(
                "SPY",
                now - timedelta(minutes=5),
                100.0,
                101.0,
                99.0,
                100.5,
                10,
                "5m",
                "ibkr",
            )
        ],
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={
            "rounded_reversal": {"enabled": rounded_enabled},
        },
    )

    _bar_slots, _mtf_context, _mtf_context_slots, mtf_quality = ctx.get_db_context()

    assert len(store.snapshot_context_queries) == 1
    assert store.snapshot_context_queries[0]["include_mtf_quality"] is quality_expected
    assert store.snapshot_context_queries[0]["mtf_timeframes"] == (
        ("1m",) if rounded_enabled else ()
    )
    assert store.snapshot_context_queries[0]["mtf_history_bars"] == (
        {"1m": 256} if rounded_enabled else {}
    )
    assert bool(store.snapshot_context_queries[0]["mtf_context_ends"]) is quality_expected
    assert bool(mtf_quality) is quality_expected


def test_channel_master_does_not_request_unsupported_1m_context() -> None:
    store = FakeSnapshotStore()
    as_of = datetime(2026, 7, 30, 14, 2, 30, tzinfo=UTC)
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[
            Bar(
                "SPY",
                as_of - timedelta(minutes=5),
                100.0,
                101.0,
                99.0,
                100.5,
                10,
                "5m",
                "ibkr",
            )
        ],
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={"channel_master": {"enabled": True}},
        analysis_as_of_utc=as_of,
    )

    ctx.get_db_context()

    query = store.snapshot_context_queries[0]
    assert query["analysis_as_of_utc"] == as_of
    assert query["mtf_timeframes"] == ()
    assert query["mtf_history_bars"] == {}
    assert query["mtf_context_ends"] == {}


def test_snapshot_read_context_ignores_params_for_undiscovered_consumers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        read_context_module,
        "indicator_ids_for_shared_context",
        lambda _context_ref: (),
    )
    store = FakeSnapshotStore()
    now = datetime.now(tz=UTC)
    ctx = SnapshotReadContext(
        store=store,
        symbol="SPY",
        interval="5m",
        range_="1d",
        bars=[
            Bar(
                "SPY",
                now - timedelta(minutes=5),
                100.0,
                101.0,
                99.0,
                100.5,
                10,
                "5m",
                "ibkr",
            )
        ],
        instrument=ibkr_stock_payload("SPY"),
        indicator_params={
            "option_reversal": {"enabled": True},
            "absorption_trap": {"enabled": True},
            "tick_flow": {"enabled": True},
        },
    )

    ctx.prefetch_sync()

    assert ctx.get_option_targets() == []
    assert ctx.get_option_flow() is None
    assert ctx.get_tick_flow() is None
    assert store.gex_queries == []
    assert store.option_targets_queries == []
    assert store.tick_delta_queries == []
