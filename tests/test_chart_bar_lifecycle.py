from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aef_terminal.data.ibkr import bars as ibkr_bars
from aef_terminal.data.ibkr.contracts import _ibkr_bar_provenance
from aef_terminal.domain import BarState
from tests.provider_payloads import ibkr_stock_payload


def _raw_bar(ts: datetime, close: float) -> SimpleNamespace:
    return SimpleNamespace(
        date=ts,
        open=close - 0.25,
        high=close + 0.5,
        low=close - 0.5,
        close=close,
        volume=100,
    )


def _instrument_with_session(opens_at: datetime, closes_at: datetime) -> dict:
    instrument = ibkr_stock_payload("SPY")
    interval = {
        "session_date": opens_at.date().isoformat(),
        "opens_at": opens_at.isoformat(),
        "closes_at": closes_at.isoformat(),
        "status": "open",
    }
    instrument["session"] = {
        "calendar": "ibkr_trading_hours",
        "timezone": "America/New_York",
        "trading_intervals": [interval],
        "liquid_intervals": [interval],
    }
    return instrument


def _provenance(instrument: dict, request_type: str):
    return _ibkr_bar_provenance(
        request_type=request_type,
        what_to_show="TRADES",
        instrument=instrument,
    )


def test_ibkr_provider_succession_confirms_only_bars_with_a_later_provider_bucket() -> None:
    base = datetime(2099, 7, 18, 12, 0, tzinfo=UTC)
    instrument = _instrument_with_session(base - timedelta(hours=1), base + timedelta(hours=1))

    result = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(base, 100.0), _raw_bar(base + timedelta(minutes=5), 101.0)],
        "SPY",
        "5m",
        provenance=_provenance(instrument, "keep_up_to_date"),
        instrument=instrument,
    )

    assert result[0].state is BarState.CONFIRMED
    assert result[0].closed is True
    assert result[0].provenance == _provenance(
        instrument,
        "keep_up_to_date",
    )
    assert result[1].state is BarState.FORMING
    assert result[1].closed is False


def test_ibkr_midpoint_succession_remains_display_only_provisional() -> None:
    base = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    instrument = ibkr_stock_payload(
        "EUR",
        con_id=12087792,
        asset_class="forex",
        sec_type="CASH",
    )
    provenance = _ibkr_bar_provenance(
        request_type="keep_up_to_date",
        what_to_show="MIDPOINT",
        instrument=instrument,
    )

    result = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(base, 1.15), _raw_bar(base + timedelta(minutes=5), 1.16)],
        "EUR",
        "5m",
        provenance=provenance,
        instrument=instrument,
    )

    assert [bar.state for bar in result] == [
        BarState.AWAITING_PROVIDER_CONFIRMATION,
        BarState.AWAITING_PROVIDER_CONFIRMATION,
    ]
    assert all(bar.closed is False for bar in result)
    assert all(bar.provenance is provenance for bar in result)


def test_ibkr_elapsed_latest_bucket_awaits_provider_confirmation() -> None:
    base = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    instrument = _instrument_with_session(base - timedelta(hours=1), base + timedelta(hours=1))

    result = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(base, 100.0)],
        "SPY",
        "5m",
        provenance=_provenance(instrument, "keep_up_to_date"),
        instrument=instrument,
    )

    assert result[0].state is BarState.AWAITING_PROVIDER_CONFIRMATION
    assert result[0].closed is False


def test_ibkr_historical_response_confirms_elapsed_latest_bucket() -> None:
    base = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    instrument = _instrument_with_session(base - timedelta(hours=1), base + timedelta(hours=1))

    result = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(base, 100.0)],
        "SPY",
        "5m",
        provenance=_provenance(instrument, "historical"),
        instrument=instrument,
        confirm_elapsed_latest=True,
    )

    assert result[0].state is BarState.CONFIRMED
    assert result[0].closed is True


def test_ibkr_closing_partial_hour_uses_exact_provider_session_close() -> None:
    opens_at = datetime(2026, 7, 18, 20, 0, tzinfo=UTC)
    closes_at = datetime(2026, 7, 18, 20, 15, tzinfo=UTC)
    instrument = _instrument_with_session(opens_at, closes_at)

    live = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(opens_at, 100.0)],
        "VIX",
        "60m",
        provenance=_provenance(instrument, "keep_up_to_date"),
        instrument=instrument,
    )
    historical = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(opens_at, 100.0)],
        "VIX",
        "60m",
        provenance=_provenance(instrument, "historical"),
        instrument=instrument,
        confirm_elapsed_latest=True,
    )

    assert live[0].state is BarState.AWAITING_PROVIDER_CONFIRMATION
    assert historical[0].state is BarState.CONFIRMED


def test_ibkr_complete_explicit_past_page_confirms_latest_without_retained_schedule() -> None:
    bar_ts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    instrument = ibkr_stock_payload("SPY")

    result = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(bar_ts, 100.0)],
        "SPY",
        "60m",
        provenance=_provenance(instrument, "historical"),
        instrument=instrument,
        confirm_elapsed_latest=True,
        provider_response_complete=True,
        confirmed_through=bar_ts + timedelta(days=1),
    )

    assert result[0].state is BarState.CONFIRMED


def test_ibkr_complete_page_does_not_confirm_bar_before_its_bucket_close() -> None:
    bar_ts = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)
    instrument = _instrument_with_session(
        datetime(2026, 7, 20, 13, 30, tzinfo=UTC),
        datetime(2026, 7, 20, 20, 0, tzinfo=UTC),
    )

    partial = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(bar_ts, 100.0)],
        "SPY",
        "60m",
        provenance=_provenance(instrument, "historical"),
        instrument=instrument,
        confirm_elapsed_latest=True,
        provider_response_complete=True,
        confirmed_through=bar_ts + timedelta(minutes=30),
    )
    complete = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(bar_ts, 100.0)],
        "SPY",
        "60m",
        provenance=_provenance(instrument, "historical"),
        instrument=instrument,
        confirm_elapsed_latest=True,
        provider_response_complete=True,
        confirmed_through=bar_ts + timedelta(hours=1),
    )

    assert partial[0].state is BarState.AWAITING_PROVIDER_CONFIRMATION
    assert complete[0].state is BarState.CONFIRMED


def test_ibkr_current_tail_without_schedule_awaits_provider_confirmation() -> None:
    bar_ts = datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    instrument = ibkr_stock_payload("SPY")

    result = ibkr_bars._bars_from_ibkr_sequence(
        [_raw_bar(bar_ts, 100.0)],
        "SPY",
        "60m",
        provenance=_provenance(instrument, "historical"),
        instrument=instrument,
        confirm_elapsed_latest=True,
        provider_response_complete=True,
    )

    assert result[0].state is BarState.AWAITING_PROVIDER_CONFIRMATION
