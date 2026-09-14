from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance
from aef_terminal.runtime import chart_events as events


def _bar(ts: datetime, *, close: float = 6300.5, closed: bool = True) -> Bar:
    return Bar(
        "ES",
        ts,
        6300,
        max(6301, close),
        6299,
        close,
        10,
        "5m",
        "ibkr:chart",
        closed=closed,
    )


def test_canonical_read_guard_stays_closed_through_generation_publication() -> None:
    instrument_id = "ibkr|contract|canonical-write-window"
    route_fingerprint = "ibkr|ES|canonical-write-window"
    seen = events.chart_bars_updated_generation(
        "5m",
        route_fingerprint,
        instrument_id=instrument_id,
    )
    guard = events.begin_chart_bars_write(
        seen,
        "5m",
        route_fingerprint,
        instrument_id=instrument_id,
    )
    try:
        with pytest.raises(events.ChartBarsGenerationChanged) as active_before:
            events.require_chart_bars_generation(
                seen,
                "5m",
                route_fingerprint,
                instrument_id=instrument_id,
            )
        assert active_before.value.write_in_progress is True

        published = events.publish_chart_bars_updated(
            "ibkr",
            "5m",
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            expected_generation=seen,
        )
        with pytest.raises(events.ChartBarsGenerationChanged) as active_after:
            events.require_chart_bars_generation(
                published,
                "5m",
                route_fingerprint,
                instrument_id=instrument_id,
            )
        assert active_after.value.write_in_progress is True
    finally:
        events.end_chart_bars_write(guard)

    assert (
        events.require_chart_bars_generation(
            published,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        == published
    )


def test_authority_publication_guard_fences_every_captured_canonical_scope() -> None:
    instrument_id = "ibkr|contract|authority-publication"
    route_fingerprint = "ibkr|ES|authority-publication"
    generations = {
        timeframe: events.chart_bars_updated_generation(
            timeframe,
            route_fingerprint,
            instrument_id=instrument_id,
        )
        for timeframe in ("5m", "1m")
    }
    guard = events.begin_chart_bars_publication(
        tuple(
            events.ChartBarsGenerationScope(
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                interval=timeframe,
                generation=generation,
            )
            for timeframe, generation in generations.items()
        )
    )
    try:
        for timeframe, generation in generations.items():
            with pytest.raises(events.ChartBarsGenerationChanged) as active:
                events.begin_chart_bars_write(
                    generation,
                    timeframe,
                    route_fingerprint,
                    instrument_id=instrument_id,
                )
            assert active.value.publication_in_progress is True
    finally:
        events.end_chart_bars_publication(guard)

    for timeframe, generation in generations.items():
        write_guard = events.begin_chart_bars_write(
            generation,
            timeframe,
            route_fingerprint,
            instrument_id=instrument_id,
        )
        events.end_chart_bars_write(write_guard)


def test_uncertain_canonical_write_invalidates_before_guard_release() -> None:
    instrument_id = "ibkr|contract|canonical-write-recovery"
    route_fingerprint = "ibkr|ES|canonical-write-recovery"
    seen = events.chart_bars_updated_generation(
        "5m",
        route_fingerprint,
        instrument_id=instrument_id,
    )
    guard = events.begin_chart_bars_write(
        seen,
        "5m",
        route_fingerprint,
        instrument_id=instrument_id,
    )

    recovered = events.invalidate_chart_bars_write(
        guard,
        "ibkr",
        reason="canonical_commit_outcome_uncertain",
        repair={"commit_outcome": "uncertain"},
    )

    assert recovered == seen + 1
    assert (
        events.require_chart_bars_generation(
            recovered,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        == recovered
    )
    generation, observed = events.chart_bars_updated_events_since(
        "5m",
        route_fingerprint,
        seen,
        instrument_id=instrument_id,
    )
    assert generation == recovered
    assert observed[-1].reason == "canonical_commit_outcome_uncertain"
    assert observed[-1].repair == {"commit_outcome": "uncertain"}
    assert observed[-1].bars == ()


def test_canonical_stable_waiter_wakes_only_after_published_write_ends() -> None:
    async def run() -> None:
        instrument_id = "ibkr|contract|stable-published-write"
        route_fingerprint = "ibkr|ES|stable-published-write"
        seen = events.chart_bars_updated_generation(
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        guard = events.begin_chart_bars_write(
            seen,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        waiter = asyncio.create_task(
            events.wait_for_chart_bars_stable(
                "5m",
                route_fingerprint,
                0.2,
                instrument_id=instrument_id,
            )
        )
        await asyncio.sleep(0)

        published = events.publish_chart_bars_updated(
            "ibkr",
            "5m",
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            expected_generation=seen,
        )
        await asyncio.sleep(0)
        assert waiter.done() is False

        events.end_chart_bars_write(guard)
        assert await asyncio.wait_for(waiter, timeout=0.1) == published

    asyncio.run(run())


def test_canonical_stable_waiter_rechecks_replacement_writer_until_same_deadline() -> None:
    async def run() -> None:
        instrument_id = "ibkr|contract|stable-replacement-write"
        route_fingerprint = "ibkr|ES|stable-replacement-write"
        seen = events.chart_bars_updated_generation(
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        first_guard = events.begin_chart_bars_write(
            seen,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        waiter = asyncio.create_task(
            events.wait_for_chart_bars_stable(
                "5m",
                route_fingerprint,
                0.2,
                instrument_id=instrument_id,
            )
        )
        await asyncio.sleep(0)

        events.end_chart_bars_write(first_guard)
        replacement_guard = events.begin_chart_bars_write(
            seen,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        await asyncio.sleep(0)
        assert waiter.done() is False

        events.end_chart_bars_write(replacement_guard)
        assert await asyncio.wait_for(waiter, timeout=0.1) == seen

    asyncio.run(run())


def test_canonical_stable_waiter_wakes_after_uncertain_write_invalidation() -> None:
    async def run() -> None:
        instrument_id = "ibkr|contract|stable-invalidated-write"
        route_fingerprint = "ibkr|ES|stable-invalidated-write"
        seen = events.chart_bars_updated_generation(
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        guard = events.begin_chart_bars_write(
            seen,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        waiter = asyncio.create_task(
            events.wait_for_chart_bars_stable(
                "5m",
                route_fingerprint,
                0.2,
                instrument_id=instrument_id,
            )
        )
        await asyncio.sleep(0)

        invalidated = events.invalidate_chart_bars_write(
            guard,
            "ibkr",
            reason="canonical_commit_outcome_uncertain",
        )

        assert await asyncio.wait_for(waiter, timeout=0.1) == invalidated

    asyncio.run(run())


def test_canonical_stable_waiter_times_out_with_active_write_observable() -> None:
    async def run() -> None:
        instrument_id = "ibkr|contract|stable-write-timeout"
        route_fingerprint = "ibkr|ES|stable-write-timeout"
        seen = events.chart_bars_updated_generation(
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        guard = events.begin_chart_bars_write(
            seen,
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        try:
            with pytest.raises(events.ChartBarsGenerationChanged) as raised:
                await events.wait_for_chart_bars_stable(
                    "5m",
                    route_fingerprint,
                    0.01,
                    instrument_id=instrument_id,
                )
            assert raised.value.write_in_progress is True
            assert raised.value.observed == seen
        finally:
            events.end_chart_bars_write(guard)

    asyncio.run(run())


def test_three_minute_generation_is_owned_by_one_minute_source() -> None:
    instrument_id = "coinbase|spot|BTC-USD|derived-three-minute"
    route_fingerprint = instrument_id
    seen = events.chart_bars_updated_generation(
        "3m", route_fingerprint, instrument_id=instrument_id
    )

    generation = events.publish_chart_bars_updated(
        "coinbase",
        "1m",
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )

    assert generation == seen + 1
    assert (
        events.chart_bars_updated_generation("3m", route_fingerprint, instrument_id=instrument_id)
        == generation
    )


def test_chart_bars_updated_event_wakes_identity_scoped_waiter_with_payload() -> None:
    async def run() -> None:
        provider = "ibkr"
        instrument_id = "ibkr|contract|canonical-waiter"
        interval = "5m"
        route_fingerprint = "ibkr|ES|canonical-waiter"
        seen = events.chart_bars_updated_generation(
            interval,
            route_fingerprint,
            instrument_id=instrument_id,
        )
        waiter = asyncio.create_task(
            events.wait_for_chart_bars_updated(
                interval,
                route_fingerprint,
                seen,
                1.0,
                instrument_id=instrument_id,
            )
        )
        await asyncio.sleep(0)

        generation = events.publish_chart_bars_updated(
            provider,
            interval,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            reason="canonical_commit",
            bars=[_bar(datetime(2026, 7, 6, 5, 10, tzinfo=UTC))],
            gap={"from": "2026-07-06T05:05:00+00:00", "to": "2026-07-06T05:15:00+00:00"},
            repair={"status": "ok", "written_bars": 1},
        )
        observed_generation, observed_events = await waiter

        assert observed_generation == generation
        assert observed_generation > seen
        assert len(observed_events) == 1
        assert observed_events[0].provider == "ibkr"
        assert observed_events[0].instrument_id == instrument_id
        assert observed_events[0].interval == "5m"
        assert observed_events[0].route_fingerprint == route_fingerprint
        assert observed_events[0].reason == "canonical_commit"
        assert observed_events[0].bars[0].close == 6300.5
        assert observed_events[0].gap["from"] == "2026-07-06T05:05:00+00:00"
        assert observed_events[0].repair["written_bars"] == 1

    asyncio.run(run())


def test_canonical_refresh_generations_are_isolated_by_instrument_identity() -> None:
    route_fingerprint = "shared-fingerprint-for-isolation-test"
    first_id = "ibkr|contract|refresh-isolation-a"
    second_id = "ibkr|contract|refresh-isolation-b"

    first_seen = events.chart_bars_updated_generation(
        "5m", route_fingerprint, instrument_id=first_id
    )
    second_seen = events.chart_bars_updated_generation(
        "5m", route_fingerprint, instrument_id=second_id
    )
    generation = events.publish_chart_bars_updated(
        "ibkr",
        "5m",
        instrument_id=first_id,
        route_fingerprint=route_fingerprint,
    )

    assert generation == first_seen + 1
    assert (
        events.chart_bars_updated_generation("5m", route_fingerprint, instrument_id=second_id)
        == second_seen
    )


def test_live_chart_event_publishes_immutable_latest_snapshot_and_deduplicates() -> None:
    instrument_id = "ibkr|contract|live-snapshot"
    route_fingerprint = "ibkr|ES|live-snapshot"
    base = datetime(2026, 7, 6, 5, 10, tzinfo=UTC)
    mutable_input = [_bar(base, closed=False)]

    first_generation = events.publish_live_chart_bars(
        "ibkr",
        "5m",
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        bars=mutable_input,
    )
    mutable_input.append(_bar(base + timedelta(minutes=5), closed=False))
    duplicate_generation = events.publish_live_chart_bars(
        "ibkr",
        "5m",
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        bars=mutable_input[:1],
    )
    snapshot = events.live_chart_bars_snapshot(
        "5m",
        route_fingerprint,
        instrument_id=instrument_id,
    )

    assert duplicate_generation == first_generation
    assert snapshot is not None
    assert snapshot.generation == first_generation
    assert snapshot.instrument_id == instrument_id
    assert snapshot.route_fingerprint == route_fingerprint
    assert snapshot.reason == "provider_update"
    assert snapshot.bars == (_bar(base, closed=False),)


def test_live_chart_event_does_not_deduplicate_provenance_correction() -> None:
    instrument_id = "ibkr|contract|live-provenance-correction"
    route_fingerprint = "ibkr|ES|live-provenance-correction"
    base = _bar(
        datetime(2026, 7, 6, 5, 10, tzinfo=UTC),
        closed=False,
    )
    provenance_fields = {
        "provider": "ibkr",
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider_contract_id": "756733",
        "provider_contract_type": "STK",
        "data_type": "TRADES",
    }
    historical = replace(
        base,
        provenance=BarProvenance(
            request_type=BarProviderRequest.HISTORICAL,
            **provenance_fields,
        ),
    )
    keep_up_to_date = replace(
        base,
        provenance=BarProvenance(
            request_type=BarProviderRequest.KEEP_UP_TO_DATE,
            **provenance_fields,
        ),
    )

    first_generation = events.publish_live_chart_bars(
        "ibkr",
        "5m",
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        bars=[historical],
    )
    corrected_generation = events.publish_live_chart_bars(
        "ibkr",
        "5m",
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        bars=[keep_up_to_date],
    )

    assert corrected_generation == first_generation + 1
    snapshot = events.live_chart_bars_snapshot(
        "5m",
        route_fingerprint,
        instrument_id=instrument_id,
    )
    assert snapshot is not None
    assert snapshot.bars == (keep_up_to_date,)


def test_live_chart_waiter_receives_revision_and_stream_closed_event() -> None:
    async def run() -> None:
        instrument_id = "ibkr|contract|live-waiter"
        route_fingerprint = "ibkr|ES|live-waiter"
        base = datetime(2026, 7, 6, 5, 10, tzinfo=UTC)
        seen = events.publish_live_chart_bars(
            "ibkr",
            "5m",
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            bars=[_bar(base, closed=False)],
        )
        update_waiter = asyncio.create_task(
            events.wait_for_live_chart_bars(
                "5m",
                route_fingerprint,
                seen,
                1.0,
                instrument_id=instrument_id,
            )
        )
        await asyncio.sleep(0)

        revised_generation = events.publish_live_chart_bars(
            "ibkr",
            "5m",
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            bars=[_bar(base, close=6300.75, closed=False)],
        )
        observed_generation, observed = await update_waiter

        assert observed_generation == revised_generation
        assert observed is not None
        assert observed.bars[0].close == 6300.75

        close_waiter = asyncio.create_task(
            events.wait_for_live_chart_bars(
                "5m",
                route_fingerprint,
                observed_generation,
                1.0,
                instrument_id=instrument_id,
            )
        )
        await asyncio.sleep(0)
        events.clear_live_chart_bars(
            "5m",
            route_fingerprint,
            instrument_id=instrument_id,
        )
        closed_generation, closed = await close_waiter

        assert closed_generation > observed_generation
        assert closed is not None
        assert closed.reason == "stream_closed"
        assert closed.bars == ()

    asyncio.run(run())
