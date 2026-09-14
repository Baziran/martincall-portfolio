from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance
from aef_terminal.engine.analyze import replay_analysis_timeline
from tests.provider_payloads import ibkr_future_payload


_INSTRUMENT = ibkr_future_payload("ES")
_PROVENANCE = BarProvenance(
    provider="ibkr",
    instrument_id=_INSTRUMENT["instrument_id"],
    route_fingerprint=route_fingerprint(_INSTRUMENT),
    request_type=BarProviderRequest.CANONICAL_STORAGE,
    provider_contract_id="",
    provider_contract_type="CANONICAL_STORAGE",
    data_type="canonical_ohlcv",
)


def _replay_bars(count: int = 48) -> list[Bar]:
    start = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    price = 100.0
    bars: list[Bar] = []
    for index in range(count):
        close = price + (0.08 if index < 24 else 0.22)
        bars.append(
            Bar(
                "ES",
                start + timedelta(minutes=index),
                price,
                max(price, close) + 0.25,
                min(price, close) - 0.25,
                close,
                1000 + index * 5,
                "1m",
                provenance=_PROVENANCE,
            )
        )
        price = close
    return bars


def _stable_params() -> dict[str, dict[str, bool]]:
    return {
        "ai_third_opinion": {"enabled": False},
        "martin_carlo": {"enabled": False},
        "gex_dynamics": {"enabled": False},
        "option_reversal": {"enabled": False},
    }


def test_replay_analysis_timeline_is_deterministic_and_golden() -> None:
    bars = _replay_bars()

    first = replay_analysis_timeline(
        bars, instrument=_INSTRUMENT, min_bars=24, step=8, indicator_params=_stable_params()
    )
    second = replay_analysis_timeline(
        bars, instrument=_INSTRUMENT, min_bars=24, step=8, indicator_params=_stable_params()
    )

    assert first == second
    assert [
        (
            row["index"],
            row["close"],
            row["decision"]["action"],
            row["decision"]["direction"],
            row["decision"]["kind"],
        )
        for row in first
    ] == [
        (23, 101.92, "BLOCK", "flat", "wait"),
        (31, 103.68, "BLOCK", "flat", "wait"),
        (39, 105.44, "BLOCK", "flat", "wait"),
        (47, 107.2, "BLOCK", "flat", "wait"),
    ]
    assert all(row["decision"]["coherent"] is True for row in first)
    assert all(row["preview_active"] is False for row in first)


def test_replay_analysis_timeline_uses_confirmed_bars_only() -> None:
    bars = _replay_bars()
    live_tail = Bar(
        "ES",
        bars[-1].ts + timedelta(minutes=1),
        bars[-1].close,
        bars[-1].close + 8,
        bars[-1].close - 0.2,
        bars[-1].close + 7,
        9000,
        "1m",
        "ibkr:db-cache+quote-live",
        closed=False,
    )

    baseline = replay_analysis_timeline(
        bars, instrument=_INSTRUMENT, min_bars=24, step=8, indicator_params=_stable_params()
    )
    with_live_tail = replay_analysis_timeline(
        [*bars, live_tail],
        instrument=_INSTRUMENT,
        min_bars=24,
        step=8,
        indicator_params=_stable_params(),
    )

    assert with_live_tail == baseline


def test_replay_analysis_timeline_observes_signal_bar_at_exact_bucket_end() -> None:
    bars = _replay_bars(count=24)
    analysis_clocks: list[datetime] = []

    def capture_clock(_window, *, analysis_as_of_utc, **_kwargs):
        analysis_clocks.append(analysis_as_of_utc)
        return {}

    replay_analysis_timeline(
        bars,
        instrument=_INSTRUMENT,
        min_bars=24,
        analyzer=capture_clock,
    )

    assert analysis_clocks == [bars[-1].ts + timedelta(minutes=1)]


def test_replay_analysis_timeline_rejects_confirmed_bar_after_provisional_tail() -> None:
    bars = _replay_bars(count=24)
    provisional = Bar(
        "ES",
        bars[-1].ts + timedelta(minutes=1),
        bars[-1].close,
        bars[-1].close + 0.5,
        bars[-1].close - 0.5,
        bars[-1].close,
        100,
        "1m",
        closed=False,
    )
    later_confirmed = Bar(
        "ES",
        provisional.ts + timedelta(minutes=1),
        bars[-1].close,
        bars[-1].close + 0.5,
        bars[-1].close - 0.5,
        bars[-1].close,
        100,
        "1m",
        provenance=_PROVENANCE,
    )

    with pytest.raises(ValueError, match="REPLAY_CONFIRMED_AFTER_PROVISIONAL"):
        replay_analysis_timeline(
            [*bars, provisional, later_confirmed],
            instrument=_INSTRUMENT,
            min_bars=24,
        )
