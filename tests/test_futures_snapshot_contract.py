from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aef_terminal.domain import Bar
from aef_terminal.data.instrument_identity import futures_root, qualified_instrument_id
from aef_terminal.engine.snapshot.chart_only import chart_only_market_snapshot
from aef_terminal.engine.snapshot import db_context
from aef_terminal.engine.snapshot import assembly, chart_only
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.data.provider_sessions import (
    ProviderSessionFutureSlotCoverage,
    invalidate_provider_trading_hours_cache,
    provider_session_future_bar_slot_coverage,
)
from tests.provider_payloads import ibkr_future_payload


def _bars() -> list[Bar]:
    return [
        Bar(
            "ES",
            datetime(2026, 6, 12, 14, 30, tzinfo=UTC),
            5400,
            5410,
            5390,
            5405,
            1200,
            "5m",
            "ibkr",
            True,
        ),
        Bar(
            "ES",
            datetime(2026, 6, 12, 14, 35, tzinfo=UTC),
            5405,
            5420,
            5400,
            5415,
            1500,
            "5m",
            "ibkr",
            True,
        ),
    ]


def _future_instrument() -> dict[str, Any]:
    instrument = ibkr_future_payload("ES")
    instrument["display"] = "E-mini S&P 500"
    return instrument


def _roll_event() -> dict[str, Any]:
    return {
        "roll_ts": datetime(2026, 6, 12, 14, 30, tzinfo=UTC),
        "from_contract_key": "ESM6",
        "to_contract_key": "ESU6",
        "roll_policy": "provider_managed",
    }


class _Store:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def read_futures_roll_events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(dict(kwargs))
        return [_roll_event()]

    def read_bar_slots(self, **_kwargs: Any) -> dict[datetime, int]:
        return {bar.ts: index * 5 for index, bar in enumerate(_bars())}

    def get_full_snapshot_context(self, **kwargs: Any) -> dict[str, Any]:
        bars = list(kwargs["bars"])
        slots = [index * 5 for index, _bar in enumerate(bars)]
        return {
            "bars": bars,
            "bar_slots": ProviderBarSlotSequence(slots, schedule_state="verified"),
            "mtf_context": {},
            "mtf_context_slots": {},
            "mtf_context_quality": {},
        }

    def initialize(self) -> None:
        return None

    def read_trading_hours(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def test_futures_root_key_ignores_display_without_contract_identity() -> None:
    assert futures_root(_future_instrument()) == "ES"
    assert futures_root({"display": "E-mini S&P 500"}) == ""


def test_chart_only_roll_overlays_lookup_by_root_identity_not_display_alias() -> None:
    store = _Store()

    snapshot = chart_only_market_snapshot(
        _future_instrument(),
        "ES",
        "5m",
        "1d",
        _bars(),
        history_coverage={
            "state": "complete",
            "schedule_state": "verified",
        },
        canonical_generation=0,
        data_provider="ibkr",
        store=store,
    )

    assert store.calls[0]["instrument_id"] == qualified_instrument_id(_future_instrument())
    assert snapshot["meta"]["symbol"] == "E-mini S&P 500"
    assert snapshot["meta"]["provider"] == "ibkr"
    assert snapshot["meta"]["provider_contract_id"] == "11004968"
    assert "analysis_ts" not in snapshot["meta"]
    assert snapshot["overlays"]


def test_full_and_chart_only_snapshots_share_one_roll_event_reader() -> None:
    assembly_source = Path(assembly.__file__).read_text(encoding="utf-8")
    chart_only_source = Path(chart_only.__file__).read_text(encoding="utf-8")

    for source in (assembly_source, chart_only_source):
        assert "snapshot_futures_roll_events(" in source
        assert 'getattr(store, "read_futures_roll_events"' not in source


def test_provider_chart_axis_distinguishes_absent_and_incomplete_storage() -> None:
    timestamp = _bars()[0].ts
    absent = db_context.provider_chart_axis_points(
        None,
        _future_instrument(),
        "5m",
        (timestamp,),
    )

    assert absent.schedule_state == "unknown"
    with pytest.raises(AttributeError, match="read_bar_slots"):
        db_context.provider_chart_axis_points(
            object(),
            _future_instrument(),
            "5m",
            (timestamp,),
        )


def test_chart_only_vsa_projection_is_bounded_to_latest_twelve_hours() -> None:
    start = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)
    bars = [
        Bar(
            "ES",
            start + timedelta(minutes=index),
            5400 + index * 0.25,
            5401 + index * 0.25,
            5399 + index * 0.25,
            5400.5 + index * 0.25,
            1000 + index,
            "1m",
            "ibkr",
            True,
        )
        for index in range(900)
    ]

    snapshot = chart_only_market_snapshot(
        _future_instrument(),
        "ES",
        "1m",
        "1d",
        bars,
        history_coverage={"state": "complete"},
        canonical_generation=0,
        data_provider="ibkr",
        store=_Store(),
    )

    series = snapshot["vsa_volume"]["series"]
    assert len(series) == 720
    assert series[0]["ts"] == bars[-720].ts.isoformat()
    assert series[-1]["ts"] == bars[-1].ts.isoformat()
    assert snapshot["vsa_volume"]["status"]["render_window_seconds"] == 43_200
    assert snapshot["vsa_volume"]["status"]["bar_count"] == 900


def test_future_chart_axis_is_typed_time_only_transport(monkeypatch) -> None:
    anchor = datetime(2026, 6, 12, 14, 35, tzinfo=UTC)
    future = (
        anchor.replace(minute=40),
        anchor.replace(minute=45),
    )
    schedule_revision = 1_749_726_000_000_000
    observed: list[tuple[datetime, int, datetime]] = []

    def future_slots(after, *_args, **kwargs):
        observed.append((after, kwargs["count"], kwargs["through"]))
        return ProviderSessionFutureSlotCoverage(
            "verified",
            future,
            False,
            future[-1],
            schedule_revision,
        )

    monkeypatch.setattr(
        db_context,
        "provider_session_future_bar_slot_coverage",
        future_slots,
    )

    serialized_bars = [
        {
            "ts": anchor.isoformat(),
            "closed": True,
            "state": "confirmed",
        },
        {
            "ts": anchor.replace(minute=40).isoformat(),
            "closed": False,
            "state": "forming",
        },
    ]
    payload = db_context.chart_future_axis_payload(
        _Store(),
        "5m",
        serialized_bars,
        provider_key="ibkr",
        instrument=_future_instrument(),
    )
    identical = db_context.chart_future_axis_payload(
        _Store(),
        "5m",
        serialized_bars,
        provider_key="ibkr",
        instrument=_future_instrument(),
    )

    assert payload["kind"] == "provider_session_future_axis"
    assert isinstance(payload["schedule_revision"], int)
    assert payload["schedule_revision"] == schedule_revision
    assert identical["schedule_revision"] == schedule_revision
    assert payload["anchor_ts"] == anchor.isoformat()
    assert payload["schedule_state"] == "verified"
    assert payload["complete"] is False
    assert payload["slots"] == [
        {"ts": future[0].isoformat(), "bar_offset": 1},
        {"ts": future[1].isoformat(), "bar_offset": 2},
    ]
    assert observed == [
        (anchor, 720, anchor + timedelta(days=7)),
        (anchor, 720, anchor + timedelta(days=7)),
    ]
    assert "anchor_bar_slot" not in payload
    assert not {"open", "high", "low", "close", "volume"} & payload["slots"][0].keys()


def test_future_schedule_axis_uses_one_bounded_read_and_stable_provider_revision() -> None:
    instrument = _future_instrument()
    anchor = datetime(2026, 6, 12, 14, 35, tzinfo=UTC)
    horizon = anchor + timedelta(days=7)
    source_fetched_at = datetime(2026, 6, 12, 12, tzinfo=UTC)

    class _ScheduleStore:
        def __init__(self) -> None:
            self.hours_reads = 0
            self.interval_reads: list[dict[str, Any]] = []

        def initialize(self) -> None:
            return None

        def read_trading_hours(self, **_kwargs) -> dict[str, Any]:
            self.hours_reads += 1
            return {
                "timezone": "UTC",
                "payload": {"provider_contract_id": "11004968"},
                "fetched_at": source_fetched_at.isoformat(),
            }

        def read_trading_session_intervals(self, **kwargs) -> list[dict[str, Any]]:
            self.interval_reads.append(dict(kwargs))
            return [
                {
                    "status": "closed",
                    "opens_at": kwargs["start_ts"].isoformat(),
                    "closes_at": kwargs["end_ts"].isoformat(),
                    "source_coverage_start": kwargs["start_ts"].isoformat(),
                    "source_coverage_end": kwargs["end_ts"].isoformat(),
                    "source_fetched_at": source_fetched_at.isoformat(),
                }
            ]

    store = _ScheduleStore()
    invalidate_provider_trading_hours_cache(instrument=instrument)
    first = provider_session_future_bar_slot_coverage(
        anchor,
        "5m",
        count=720,
        through=horizon,
        store=store,
        instrument=instrument,
    )
    assert len(store.interval_reads) == 1
    second = provider_session_future_bar_slot_coverage(
        anchor,
        "5m",
        count=720,
        through=horizon,
        store=store,
        instrument=instrument,
    )

    assert len(store.interval_reads) == 2
    assert store.hours_reads == 1
    assert first.schedule_state == second.schedule_state == "verified"
    assert first.expected_slots == second.expected_slots == ()
    assert first.schedule_revision == second.schedule_revision > 0
    invalidate_provider_trading_hours_cache(instrument=instrument)
