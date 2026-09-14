from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aef_terminal.domain import Bar, Direction
from aef_terminal.indicators.modules import trade_setup_engine as tse


class _NoSlice(list[float]):
    def __getitem__(self, key):
        if isinstance(key, slice):
            raise AssertionError("growing history slices are forbidden")
        return super().__getitem__(key)


def _bars(count: int) -> list[Bar]:
    started_at = datetime(2026, 8, 3, tzinfo=UTC)
    return [
        Bar(
            symbol="ES",
            ts=started_at + timedelta(minutes=index),
            open=100.0 + index * 0.1,
            high=100.5 + index * 0.1,
            low=99.5 + index * 0.1,
            close=100.1 + index * 0.1,
            volume=1000.0 + index,
            timeframe="1m",
            source="ibkr",
            closed=True,
        )
        for index in range(count)
    ]


def test_bounded_scores_do_not_slice_growing_history() -> None:
    values = _NoSlice([1.0, 1.1, 0.9, 1.2, 1.4, 1.6, 1.3, 1.7])

    assert tse._volume_build_score(values, 7) > 0
    assert tse._chop_score(values, 7, 6) >= 0


def test_engine_reuses_one_close_series_for_every_bar(monkeypatch) -> None:
    bars = _bars(48)
    close_series_ids: list[int] = []
    close_series_lengths: list[int] = []

    def best_candidate(_bars, _index, *, closes, **_kwargs):
        close_series_ids.append(id(closes))
        close_series_lengths.append(len(closes))
        return None

    monkeypatch.setattr(tse, "_best_candidate", best_candidate)

    tse.trade_setup_engine(bars)

    assert len(close_series_ids) == len(bars)
    assert len(set(close_series_ids)) == 1
    assert set(close_series_lengths) == {len(bars)}


def test_rolling_grind_scores_match_causal_window_calculation() -> None:
    bars = _bars(48)

    for direction in (Direction.LONG, Direction.SHORT):
        rolling = tse._grind_score_series(bars, direction, 12)

        assert rolling == [
            tse._grind_score(bars, index, direction, 12) for index in range(len(bars))
        ]
