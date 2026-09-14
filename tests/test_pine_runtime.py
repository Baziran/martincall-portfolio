from dataclasses import replace
from datetime import datetime, timedelta, timezone
from random import Random
from statistics import pstdev

import pytest

from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance, BarState
from aef_terminal.features.provider_session import provider_vwap_session
from aef_terminal.runtime.mtf import confirmed_intrabar_parent_preview
from aef_terminal.runtime.pine import (
    PineContext,
    bar_anatomy,
    collapse_bars,
    ema_series,
    pivot_high,
    pivot_low,
    relative_volume_series,
    rsi_series,
    rolling_high_series,
    rolling_low_series,
    rolling_mean_series,
    rolling_stdev_series,
    stdev_at,
    vwap_series,
)


def make_bar(index: int, close: float, volume: float = 100.0, closed: bool = True) -> Bar:
    ts = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=index)
    return Bar(
        symbol="ES",
        ts=ts,
        open=close - 0.25,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=volume,
        timeframe="1m",
        source="test",
        closed=closed,
    )


def test_pine_context_keeps_confirmed_and_live_bars_separate() -> None:
    bars = [make_bar(0, 100), make_bar(1, 101), make_bar(2, 102, closed=False)]
    ctx = PineContext(tuple(bars), tuple(bars[:2]), bars[-1])

    assert ctx.latest.close == 102
    assert ctx.analysis_bar.close == 101
    assert ctx.has_live_bar


def test_pine_context_does_not_promote_live_only_bars_to_confirmed() -> None:
    bars = [make_bar(0, 100, closed=False), make_bar(1, 101, closed=False)]
    ctx = PineContext.from_bars(bars)

    assert list(ctx.confirmed_bars) == []
    assert ctx.latest == bars[-1]
    assert ctx.live_bar == bars[-1]


def test_ema_series_matches_pine_recurrence_from_first_source_value() -> None:
    values = ema_series([10.0, 13.0, 11.0], 3)

    assert values == [10.0, 11.5, 11.25]


@pytest.mark.parametrize("length", [1, 2, 30, 180, 900])
def test_stdev_matches_population_reference_without_large_baseline_cancellation(length) -> None:
    rng = Random(4807)
    sequences = [
        [3.5] * 400,
        [rng.uniform(-20, 20) for _ in range(400)],
        [1e12 + rng.randrange(-4, 5) * 0.000244140625 for _ in range(400)],
        [rng.uniform(-1e-12, 1e-12) for _ in range(400)],
    ]
    assert stdev_at([], 0, length) == 0.0
    for values in sequences:
        for index in range(len(values)):
            window = values[max(0, index - length + 1) : index + 1]
            assert stdev_at(values, index, length) == pytest.approx(
                pstdev(window), rel=3e-15, abs=1e-27
            )


def test_rsi_series_uses_wilder_recurrence_and_canonical_flat_value() -> None:
    assert rsi_series([], 3) == []
    assert rsi_series([1.0], 3) == [50.0]

    values = rsi_series([1.0, 2.0, 1.0], 2)

    assert values[:2] == [50.0, 100.0]
    assert round(values[2], 6) == 33.333333


def test_collapse_bars_builds_higher_timeframe_ohlcv() -> None:
    bars = [make_bar(index, 100 + index, volume=10 + index) for index in range(5)]
    collapsed = collapse_bars(bars, "5m", symbol="ES", source_name="security:5m")

    assert len(collapsed) == 1
    assert collapsed[0].open == bars[0].open
    assert collapsed[0].close == bars[-1].close
    assert collapsed[0].high == max(bar.high for bar in bars)
    assert collapsed[0].low == min(bar.low for bar in bars)
    assert collapsed[0].volume == sum(bar.volume for bar in bars)


def test_collapse_bars_types_provider_resampling_as_deterministic_aggregation() -> None:
    provenance = BarProvenance(
        provider="demo",
        instrument_id="demo|contract|MIX",
        route_fingerprint="demo-route",
        request_type=BarProviderRequest.HISTORICAL,
        provider_contract_id="MXU6",
        provider_contract_type="FUT",
        data_type="TRADES",
    )
    bars = [replace(make_bar(index, 100 + index), provenance=provenance) for index in range(5)]

    collapsed = collapse_bars(
        bars,
        "5m",
        symbol="MIX",
        source_name="demo:test",
    )

    assert collapsed[0].provenance == BarProvenance(
        provider="demo",
        instrument_id="demo|contract|MIX",
        route_fingerprint="demo-route",
        request_type=BarProviderRequest.DETERMINISTIC_AGGREGATION,
        provider_contract_id="MXU6",
        provider_contract_type="FUT",
        data_type="TRADES",
        source_timeframe="1m",
    )


def test_collapse_bars_clears_mixed_provider_lineage() -> None:
    first_provenance = BarProvenance(
        provider="demo",
        instrument_id="demo|contract|MIX",
        route_fingerprint="demo-route",
        request_type=BarProviderRequest.HISTORICAL,
        provider_contract_id="MXU6",
        provider_contract_type="FUT",
        data_type="TRADES",
    )
    second_provenance = replace(
        first_provenance,
        provider_contract_id="MXZ6",
    )
    bars = [
        replace(
            make_bar(index, 100 + index),
            provenance=(first_provenance if index < 4 else second_provenance),
        )
        for index in range(5)
    ]

    collapsed = collapse_bars(bars, "5m")

    assert collapsed[0].provenance is None


def test_collapse_bars_does_not_invent_provider_lineage_from_storage_readback() -> None:
    storage_provenance = BarProvenance(
        provider="ibkr",
        instrument_id="ibkr|stock|SPY",
        route_fingerprint="ibkr-route",
        request_type=BarProviderRequest.CANONICAL_STORAGE,
        provider_contract_id="",
        provider_contract_type="CANONICAL_STORAGE",
        data_type="canonical_ohlcv",
    )
    bars = [
        replace(
            make_bar(index, 100 + index),
            provenance=storage_provenance,
        )
        for index in range(5)
    ]

    collapsed = collapse_bars(bars, "5m")

    assert collapsed[0].provenance is None


def test_collapse_bars_preserves_unconfirmed_source_state() -> None:
    bars = [make_bar(index, 100 + index, closed=index < 4) for index in range(5)]
    collapsed = collapse_bars(bars, "5m", symbol="ES", source_name="security:5m")

    assert len(collapsed) == 1
    assert collapsed[0].closed is False


def test_collapse_bars_keeps_incomplete_source_bucket_unconfirmed() -> None:
    bars = [make_bar(index, 100 + index) for index in (0, 1, 3, 4)]
    collapsed = collapse_bars(bars, "5m", symbol="ES", source_name="security:5m")

    assert len(collapsed) == 1
    assert collapsed[0].closed is False


def test_confirmed_intrabar_parent_preview_is_never_authoritative() -> None:
    parent = replace(
        make_bar(0, 100.0),
        timeframe="5m",
    )
    context = [make_bar(index, 100.0 + index) for index in (5, 6, 7, 8, 9)]

    preview = confirmed_intrabar_parent_preview(
        [parent],
        context,
        {"ok": True},
        "1m",
    )

    assert preview[:-1] == [parent]
    assert preview[-1].timeframe == "5m"
    assert preview[-1].ts == context[0].ts
    assert preview[-1].close == context[-1].close
    assert preview[-1].volume == sum(bar.volume for bar in context)
    assert preview[-1].source == "confirmed-context:1m"
    assert preview[-1].closed is False
    assert preview[-1].state is BarState.FORMING
    assert (
        confirmed_intrabar_parent_preview(
            [parent],
            context,
            {"ok": False},
            "1m",
        )
        == []
    )
    off_phase_parent = replace(
        parent,
        ts=parent.ts + timedelta(minutes=30),
        timeframe="60m",
    )
    off_phase_context = [
        replace(
            make_bar(index, 100.0 + index),
            ts=off_phase_parent.ts + timedelta(minutes=60 + index),
        )
        for index in range(3)
    ]
    assert (
        confirmed_intrabar_parent_preview(
            [off_phase_parent],
            off_phase_context,
            {"ok": True},
            "1m",
        )
        == []
    )


def test_confirmed_intrabar_parent_preview_rejects_parent_history_lag() -> None:
    parent = replace(
        make_bar(0, 100.0),
        timeframe="5m",
    )
    context = [make_bar(index, 100.0 + index) for index in range(5, 11)]

    assert (
        confirmed_intrabar_parent_preview(
            [parent],
            context,
            {"ok": True},
            "1m",
        )
        == []
    )


def test_collapse_bars_preserves_provider_phase_and_state_for_an_equal_interval(
    monkeypatch,
) -> None:
    provider_ts = datetime(2026, 7, 17, 13, 30, tzinfo=timezone.utc)
    bar = Bar(
        symbol="SPY",
        ts=provider_ts,
        open=620.0,
        high=622.0,
        low=619.0,
        close=621.0,
        volume=1000.0,
        timeframe="60m",
        source="ibkr:historical:TRADES:contract=756733",
        closed=True,
    )

    monkeypatch.setattr("aef_terminal.runtime.pine.bar_is_closed", lambda *_args, **_kwargs: False)

    collapsed = collapse_bars([bar], "60m", symbol="SPY", source_name="ibkr:db-cache")

    assert len(collapsed) == 1
    assert collapsed[0].ts == provider_ts
    assert collapsed[0].closed is True


def test_pivot_helpers_match_confirmed_left_right_logic() -> None:
    values = [4, 3, 5, 3, 1, 4, 5]

    assert pivot_high(values, 2, 2, 2)
    assert pivot_low(values, 4, 2, 2)
    assert not pivot_high(values, 5, 2, 2)


def test_vwap_resets_by_market_day() -> None:
    first = make_bar(0, 100, volume=10)
    second = make_bar(1, 110, volume=30)
    next_day = Bar(
        symbol="ES",
        ts=datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
        open=119,
        high=121,
        low=118,
        close=120,
        volume=20,
        timeframe="1m",
        source="test",
    )

    values = vwap_series(
        [first, second, next_day],
        reset=lambda bar: bar.ts.astimezone(timezone.utc).date().isoformat(),
    )

    first_typical = (first.high + first.low + first.close) / 3.0
    second_typical = (second.high + second.low + second.close) / 3.0
    assert values[1] == (first_typical * 10 + second_typical * 30) / 40
    assert values[2] == (next_day.high + next_day.low + next_day.close) / 3.0


def test_vwap_session_uses_provider_intervals_without_symbol_inference() -> None:
    bars = [make_bar(0, 100), make_bar(1, 101)]
    for provider, calendar in (("ibkr", "ibkr_trading_hours"), ("demo", "demo_marketdata")):
        session = provider_vwap_session(
            {
                "session": {
                    "provider": provider,
                    "calendar": calendar,
                    "trading_intervals": [
                        {
                            "session_date": "2026-01-01",
                            "opens_at": "2026-01-01T08:00:00+00:00",
                            "closes_at": "2026-01-01T18:00:00+00:00",
                            "status": "open",
                        }
                    ],
                }
            },
            bars,
        )

        assert session.available is True
        assert session.key_for_bar(bars[0]) == "2026-01-01"
        assert vwap_series(bars, reset=session.key_for_bar)[-1] > 0


def test_vwap_session_groups_disjoint_intervals_by_exact_provider_session_date() -> None:
    bars = [
        Bar("ES", datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc), 100, 100, 100, 100, 1, "1m"),
        Bar("ES", datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc), 200, 200, 200, 200, 1, "1m"),
        Bar("ES", datetime(2026, 1, 6, 9, 0, tzinfo=timezone.utc), 300, 300, 300, 300, 1, "1m"),
    ]
    session = provider_vwap_session(
        {
            "session": {
                "provider": "ibkr",
                "calendar": "ibkr_trading_hours",
                "trading_intervals": [
                    {
                        "session_date": "2026-01-05",
                        "opens_at": "2026-01-05T08:00:00+00:00",
                        "closes_at": "2026-01-05T10:00:00+00:00",
                        "status": "open",
                    },
                    {
                        "session_date": "2026-01-05",
                        "opens_at": "2026-01-05T12:00:00+00:00",
                        "closes_at": "2026-01-05T14:00:00+00:00",
                        "status": "open",
                    },
                    {
                        "session_date": "2026-01-06",
                        "opens_at": "2026-01-06T08:00:00+00:00",
                        "closes_at": "2026-01-06T10:00:00+00:00",
                        "status": "open",
                    },
                ],
            }
        },
        bars,
    )

    keys = [session.key_for_bar(bar) for bar in bars]
    values = vwap_series(bars, reset=session.key_for_bar)

    assert session.available is True
    assert keys == ["2026-01-05", "2026-01-05", "2026-01-06"]
    assert values == [100.0, 150.0, 300.0]


def test_vwap_session_fails_closed_without_exact_provider_session_date() -> None:
    bar = make_bar(0, 100)
    session = provider_vwap_session(
        {
            "session": {
                "provider": "ibkr",
                "calendar": "ibkr_trading_hours",
                "trading_intervals": [
                    {
                        "opens_at": "2026-01-01T08:00:00+00:00",
                        "closes_at": "2026-01-01T18:00:00+00:00",
                        "status": "open",
                    }
                ],
            }
        },
        [bar],
    )

    assert session.available is False
    assert session.reason_code == "provider_session_date_missing"


def test_vwap_session_fails_closed_on_overlapping_provider_session_dates() -> None:
    bar = make_bar(0, 100)
    session = provider_vwap_session(
        {
            "session": {
                "provider": "ibkr",
                "calendar": "ibkr_trading_hours",
                "trading_intervals": [
                    {
                        "session_date": "2026-01-01",
                        "opens_at": "2026-01-01T08:00:00+00:00",
                        "closes_at": "2026-01-01T10:00:00+00:00",
                        "status": "open",
                    },
                    {
                        "session_date": "2026-01-02",
                        "opens_at": "2026-01-01T08:30:00+00:00",
                        "closes_at": "2026-01-01T10:30:00+00:00",
                        "status": "open",
                    },
                ],
            }
        },
        [bar],
    )

    assert session.available is False
    assert session.reason_code == "provider_session_intervals_ambiguous"


def test_vwap_session_is_unavailable_when_provider_schedule_is_unknown() -> None:
    session = provider_vwap_session(
        {"session": {"provider": "demo", "calendar": "unknown", "trading_intervals": []}},
        [make_bar(0, 100)],
    )

    assert session.available is False
    assert session.reason_code == "provider_trading_intervals_missing"


def test_vwap_continuous_provider_resets_on_utc_date() -> None:
    first = make_bar(0, 100)
    next_day = Bar(
        **{
            **first.__dict__,
            "ts": datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc),
            "open": 109,
            "high": 111,
            "low": 108,
            "close": 110,
        }
    )
    session = provider_vwap_session(
        {"session": {"provider": "coinbase", "calendar": "continuous_24_7", "timezone": "UTC"}},
        [first, next_day],
    )

    assert session.key_for_bar(first) == "UTC:2026-01-01"
    assert session.key_for_bar(next_day) == "UTC:2026-01-02"


def test_bar_anatomy_and_relative_volume_are_shared_primitives() -> None:
    bars = [
        make_bar(0, 100, volume=100),
        make_bar(1, 101, volume=100),
        make_bar(2, 102, volume=400),
    ]

    anatomy = bar_anatomy(bars[-1])
    rvol = relative_volume_series(bars, 3)

    assert anatomy.direction == 1
    assert 0 < anatomy.body_share < 1
    assert anatomy.upper_share > 0
    assert rvol[-1] > 1.9


def test_rolling_series_match_point_in_time_windows() -> None:
    values = [3.0, 1.0, 5.0, 4.0, 2.0]

    assert rolling_mean_series(values, 3) == [3.0, 2.0, 3.0, 10.0 / 3.0, 11.0 / 3.0]
    assert rolling_high_series(values, 2) == [None, 3.0, 3.0, 5.0, 5.0]
    assert rolling_low_series(values, 2) == [None, 3.0, 1.0, 1.0, 4.0]
    assert rolling_high_series(values, 2, exclude_current=False) == [3.0, 3.0, 5.0, 5.0, 4.0]
    assert round(rolling_stdev_series(values, 3)[-1], 6) == round((14.0 / 9.0) ** 0.5, 6)
