from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.runtime import chart_events
from aef_terminal.ui.services.recent_confirmed_bars import (
    RecentConfirmedBarContext,
    clear_recent_confirmed_bar_cache,
    recent_confirmed_bar_cache_stats,
    recent_confirmed_bar_context,
    recent_confirmed_bar_slots,
)
from tests.provider_payloads import ibkr_future_payload


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_recent_confirmed_bar_cache()


def _bars(count: int) -> list[Bar]:
    started_at = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)
    return [
        Bar(
            symbol="ES",
            ts=started_at + timedelta(minutes=index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
            timeframe="1m",
            source="ibkr",
            closed=True,
        )
        for index in range(count)
    ]


def test_shared_recent_bar_context_reuses_larger_generation_fenced_tail() -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)

    class Store:
        reads = 0

        def read_recent_bars_batch(self, requests):
            Store.reads += 1
            assert requests[0][:3] == ("1m", "ibkr", 320)
            return {(route.instrument_id, "1m"): _bars(320)}

    larger = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=320,
    )
    smaller = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=256,
    )

    assert Store.reads == 1
    assert len(larger.bars) == 320
    assert smaller.bars == larger.bars[-256:]
    assert recent_confirmed_bar_cache_stats() == {
        "entries": 1,
        "bars": 320,
        "loads": 1,
        "slot_loads": 0,
        "max_entries": 128,
        "max_bars_per_entry": 1024,
    }


def test_shared_recent_bar_context_reuses_confirmed_slot_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.ui.services import recent_confirmed_bars

    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    slot_reads = 0

    class Store:
        def read_recent_bars_batch(self, _requests):
            return {(route.instrument_id, "1m"): _bars(10)}

    context = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=10,
    )

    def slots(*_args, **_kwargs):
        nonlocal slot_reads
        slot_reads += 1
        return list(range(10))

    monkeypatch.setattr(recent_confirmed_bars, "provider_chart_axis_slots", slots)

    first = recent_confirmed_bar_slots(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        context=context,
    )
    smaller_context = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=5,
    )
    second = recent_confirmed_bar_slots(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        context=smaller_context,
    )

    assert first == list(range(10))
    assert second == list(range(5, 10))
    assert slot_reads == 1
    assert recent_confirmed_bar_cache_stats()["slot_loads"] == 1


def test_shared_recent_bar_slots_reject_stale_source_context() -> None:
    instrument = ibkr_future_payload("CL")
    route = route_instrument(instrument)

    class Store:
        def read_recent_bars_batch(self, _requests):
            return {(route.instrument_id, "1m"): _bars(10)}

    context = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=10,
    )
    chart_events.publish_chart_bars_updated(
        route.provider,
        "1m",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        reason="test_commit",
    )

    with pytest.raises(chart_events.ChartBarsGenerationChanged):
        recent_confirmed_bar_slots(
            store=Store(),
            instrument=instrument,
            timeframe="1m",
            context=context,
        )


def test_shared_recent_bar_context_reloads_after_canonical_generation_advance() -> None:
    instrument = ibkr_future_payload("NQ")
    route = route_instrument(instrument)

    class Store:
        reads = 0

        def read_recent_bars_batch(self, _requests):
            Store.reads += 1
            return {(route.instrument_id, "1m"): _bars(Store.reads)}

    first = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=10,
    )
    chart_events.publish_chart_bars_updated(
        route.provider,
        "1m",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        reason="test_commit",
    )
    second = recent_confirmed_bar_context(
        store=Store(),
        instrument=instrument,
        timeframe="1m",
        limit=10,
    )

    assert Store.reads == 2
    assert len(first.bars) == 1
    assert len(second.bars) == 2
    assert second.generation == first.generation + 1


def test_shared_recent_bar_context_coalesces_same_scope_loads() -> None:
    instrument = ibkr_future_payload("RTY")
    route = route_instrument(instrument)
    started = threading.Event()
    release = threading.Event()

    class Store:
        reads = 0

        def read_recent_bars_batch(self, _requests):
            Store.reads += 1
            started.set()
            assert release.wait(timeout=1.0)
            return {(route.instrument_id, "1m"): _bars(10)}

    def load():
        return recent_confirmed_bar_context(
            store=Store(),
            instrument=instrument,
            timeframe="1m",
            limit=10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(load)
        assert started.wait(timeout=1.0)
        second = executor.submit(load)
        release.set()
        assert first.result(timeout=1.0).bars == second.result(timeout=1.0).bars

    assert Store.reads == 1


def test_shared_recent_bar_context_never_caches_a_read_overlapping_writer() -> None:
    instrument = ibkr_future_payload("YM")
    route = route_instrument(instrument)
    generation = chart_events.chart_bars_updated_generation(
        "1m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    guards: list[chart_events.ChartBarsWriteGuard] = []

    class Store:
        def read_recent_bars_batch(self, _requests):
            guards.append(
                chart_events.begin_chart_bars_write(
                    generation,
                    "1m",
                    route.fingerprint,
                    instrument_id=route.instrument_id,
                )
            )
            return {(route.instrument_id, "1m"): _bars(10)}

    try:
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as active:
            recent_confirmed_bar_context(
                store=Store(),
                instrument=instrument,
                timeframe="1m",
                limit=10,
            )
        assert active.value.write_in_progress is True
    finally:
        for guard in guards:
            chart_events.end_chart_bars_write(guard)

    assert recent_confirmed_bar_cache_stats()["entries"] == 0


def test_recent_confirmed_context_rejects_lookalikes_and_invalid_slot_order() -> None:
    with pytest.raises(TypeError, match="ROWS_INVALID"):
        RecentConfirmedBarContext(
            generation=0,
            requested_limit=1,
            bars=(object(),),  # type: ignore[arg-type]
        )

    bars = tuple(_bars(2))
    with pytest.raises(ValueError, match="SLOTS_INVALID"):
        RecentConfirmedBarContext(
            generation=0,
            requested_limit=2,
            bars=bars,
            confirmed_slots=(4, 4),
        )
