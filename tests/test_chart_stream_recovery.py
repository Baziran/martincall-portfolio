from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.data.provider_contract import ProviderBarBucket
from aef_terminal.domain import Bar
from aef_terminal.data.provider_sessions import continuous_provider_bar_slot
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.ui import stream_payload
from aef_terminal.ui.runtime.constants import (
    CHART_STREAM_MAX_RECOVERY_TAIL,
    CHART_STREAM_RECOVERY_TAIL,
)
from aef_terminal.ui.stream_payload import (
    StreamPayloadDeps,
    chart_recovery_snapshot,
    chart_recovery_tail,
    stream_bar_payload,
)
from tests.provider_payloads import coinbase_btc_payload, ibkr_future_payload, ibkr_stock_payload


def make_bar(index: int, ts: datetime) -> Bar:
    price = 5000.0 + index
    return Bar(
        symbol="ES",
        ts=ts,
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.25,
        volume=1000 + index,
        timeframe="5m",
        source="test",
        closed=True,
    )


def _configure_recovery(
    monkeypatch,
    *,
    quality: dict,
    slots: list[int],
    live_bucket: datetime | None = None,
) -> None:
    monkeypatch.setattr(stream_payload, "_DEPS", StreamPayloadDeps(store_factory=lambda: None))
    monkeypatch.setattr(
        stream_payload,
        "snapshot_db_context",
        lambda _store, _symbol, _interval, _range, bars, **_kwargs: (
            list(bars),
            ProviderBarSlotSequence(slots, schedule_state="verified"),
            {},
            {},
            {},
        ),
    )
    monkeypatch.setattr(stream_payload, "data_quality_report", lambda *_args, **_kwargs: quality)
    monkeypatch.setattr(
        stream_payload,
        "provider_bar_bucket",
        lambda *_args, **_kwargs: (
            ProviderBarBucket(live_bucket, live_bucket + timedelta(minutes=5))
            if live_bucket is not None
            else None
        ),
    )


def test_continuous_live_stream_bar_carries_canonical_slot_before_commit() -> None:
    ts = datetime(2026, 5, 8, 7, 45, tzinfo=UTC)
    bar = Bar(
        "BTC-USD",
        ts,
        100,
        101,
        99,
        100.5,
        1,
        "5m",
        "coinbase:BTC-USD",
        closed=False,
    )

    payload = stream_bar_payload(bar, instrument=coinbase_btc_payload())

    assert payload["bar_slot"] == continuous_provider_bar_slot(ts, "5m")
    assert payload["expected_close"] == (ts + timedelta(minutes=5)).isoformat()


def test_native_ibkr_stream_bar_carries_opening_partial_close_atomically() -> None:
    opens_at = datetime(2026, 7, 16, 13, 30, tzinfo=UTC)
    closes_at = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)
    instrument = ibkr_stock_payload(
        "SPX",
        con_id=416904,
        asset_class="index",
        sec_type="IND",
    )
    session = {
        "session_date": opens_at.date().isoformat(),
        "opens_at": opens_at.isoformat(),
        "closes_at": closes_at.isoformat(),
        "status": "open",
    }
    instrument["session"] = {
        "calendar": "ibkr_trading_hours",
        "timezone": "America/New_York",
        "trading_intervals": [session],
        "liquid_intervals": [session],
    }
    bar = Bar(
        "SPX",
        opens_at,
        6800,
        6801,
        6799,
        6800.5,
        100,
        "60m",
        "ibkr:keep_up_to_date",
        closed=False,
    )

    payload = stream_bar_payload(bar, instrument=instrument)

    assert payload["ts"] == opens_at.isoformat()
    assert payload["expected_close"] == datetime(2026, 7, 16, 14, 0, tzinfo=UTC).isoformat()
    assert "bar_slot" not in payload
    assert payload["bar_slot_authoritative"] is False
    assert "bar_slot_visual_only" not in payload
    assert payload["bar_slot_schedule_state"] == "unknown"


def test_stream_and_recovery_bar_boundaries_reject_lookalikes_and_provisional_history() -> None:
    with pytest.raises(TypeError, match="typed Bar"):
        stream_bar_payload(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Bar list"):
        chart_recovery_snapshot(  # type: ignore[arg-type]
            (),
            None,
            "5m",
            ibkr_stock_payload("ES"),
        )
    provisional = Bar(
        "ES",
        datetime(2026, 5, 25, 14, 0, tzinfo=UTC),
        100.0,
        101.0,
        99.0,
        100.0,
        1.0,
        "5m",
        "ibkr",
        closed=False,
    )
    with pytest.raises(ValueError, match="only confirmed"):
        chart_recovery_snapshot([provisional], None, "5m", ibkr_stock_payload("ES"))


def test_chart_recovery_tail_expands_for_long_feed_gap() -> None:
    last_sent = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)
    latest = last_sent + timedelta(hours=3)

    tail = chart_recovery_tail(last_sent, latest, "1m")

    assert tail == 184
    assert CHART_STREAM_RECOVERY_TAIL < tail < CHART_STREAM_MAX_RECOVERY_TAIL


def test_chart_recovery_rejects_naive_client_timestamps(monkeypatch) -> None:
    ts = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)
    bars = [make_bar(0, ts)]
    _configure_recovery(monkeypatch, quality={"gaps": []}, slots=[0])

    assert stream_payload.parse_iso_ts("2026-05-25T14:00:00") is None
    with pytest.raises(ValueError, match="CHART_RECOVERY_LAST_SENT_TS_NOT_AWARE"):
        chart_recovery_snapshot(bars, datetime(2026, 5, 25, 14, 0), "5m", ibkr_stock_payload("ES"))


def test_chart_recovery_snapshot_starts_at_last_sent_bar_and_returns_quality(monkeypatch) -> None:
    start = datetime(2026, 5, 25, 14, 0, tzinfo=UTC)
    bars = [make_bar(index, start + timedelta(minutes=5 * index)) for index in range(5)]
    quality = {"gaps": [], "market_closed": False}
    _configure_recovery(monkeypatch, quality=quality, slots=[0, 5, 10, 15, 20])

    snapshot = chart_recovery_snapshot(
        bars,
        start + timedelta(minutes=10),
        "5m",
        ibkr_stock_payload("ES"),
    )

    assert [item["ts"] for item in snapshot.payloads] == [
        "2026-05-25T14:10:00+00:00",
        "2026-05-25T14:15:00+00:00",
        "2026-05-25T14:20:00+00:00",
    ]
    assert snapshot.quality == quality
    assert snapshot.expected_live_slot is None


def test_chart_recovery_snapshot_keeps_required_slot_outside_normal_tail(monkeypatch) -> None:
    start = datetime(2026, 5, 20, 0, 0, tzinfo=UTC)
    count = CHART_STREAM_MAX_RECOVERY_TAIL + 5
    bars = [make_bar(index, start + timedelta(minutes=5 * index)) for index in range(count)]
    _configure_recovery(
        monkeypatch,
        quality={"gaps": [], "market_closed": False},
        slots=list(range(count)),
    )

    snapshot = chart_recovery_snapshot(
        bars,
        None,
        "5m",
        ibkr_stock_payload("ES"),
        (start.isoformat(),),
    )

    assert snapshot.payloads[0]["ts"] == start.isoformat()
    assert snapshot.payloads[-1]["ts"] == bars[-1].ts.isoformat()
    assert len(snapshot.payloads) == CHART_STREAM_MAX_RECOVERY_TAIL + 1


def test_chart_recovery_snapshot_preserves_provider_bars_across_gaps(monkeypatch) -> None:
    bars = [
        Bar(
            "QQQ",
            datetime(2026, 6, 1, 16, 10, tzinfo=UTC),
            740.0,
            740.8,
            739.8,
            740.63,
            1000,
            "5m",
            "ibkr",
        ),
        Bar(
            "QQQ",
            datetime(2026, 6, 1, 16, 25, tzinfo=UTC),
            740.63,
            741.04,
            740.63,
            741.04,
            0,
            "5m",
            "ibkr",
        ),
    ]
    _configure_recovery(monkeypatch, quality={"gaps": []}, slots=[100, 115])

    snapshot = chart_recovery_snapshot(bars, None, "5m", ibkr_stock_payload("QQQ"))

    assert [item["ts"] for item in snapshot.payloads] == [
        "2026-06-01T16:10:00+00:00",
        "2026-06-01T16:25:00+00:00",
    ]
    assert all(not item.get("missing", False) for item in snapshot.payloads)


def test_chart_recovery_snapshot_never_materializes_missing_history_slots(monkeypatch) -> None:
    latest = datetime(2026, 6, 1, 16, 20, tzinfo=UTC)
    pending_slot = datetime(2026, 6, 1, 16, 25, tzinfo=UTC)
    bars = [Bar("ES", latest, 7580.0, 7581.0, 7579.0, 7580.5, 1000, "5m", "ibkr")]
    quality = {
        "gaps": [
            {
                "from": latest,
                "to": pending_slot,
                "missing_bars": 1,
                "missing_slots": [pending_slot],
                "tail_gap": True,
            }
        ]
    }
    _configure_recovery(
        monkeypatch,
        quality=quality,
        slots=[656195],
        live_bucket=pending_slot,
    )

    snapshot = chart_recovery_snapshot(
        bars,
        latest,
        "5m",
        ibkr_future_payload("ES"),
    )

    assert [item["ts"] for item in snapshot.payloads] == [
        "2026-06-01T16:20:00+00:00",
    ]
    assert all(not item.get("missing", False) for item in snapshot.payloads)
    assert snapshot.expected_live_slot == pending_slot.isoformat()
    assert snapshot.expected_live_close == (pending_slot + timedelta(minutes=5)).isoformat()
    assert snapshot.quality == quality


def test_chart_recovery_snapshot_hides_expected_slot_when_market_is_closed(monkeypatch) -> None:
    latest = datetime(2026, 6, 1, 16, 20, tzinfo=UTC)
    pending_slot = latest + timedelta(minutes=5)
    bars = [make_bar(0, latest)]
    quality = {
        "market_closed": True,
        "gaps": [
            {
                "from": latest,
                "to": pending_slot,
                "missing_slots": [pending_slot],
                "tail_gap": True,
            }
        ],
    }
    _configure_recovery(monkeypatch, quality=quality, slots=[10])

    snapshot = chart_recovery_snapshot(bars, None, "5m", ibkr_stock_payload("ES"))

    assert [item["ts"] for item in snapshot.payloads] == [latest.isoformat()]
    assert all(not item.get("missing", False) for item in snapshot.payloads)
    assert snapshot.expected_live_slot is None
    assert snapshot.expected_live_close is None


def test_chart_recovery_expected_live_slot_uses_current_provider_bucket_not_stale_gap_tail(
    monkeypatch,
) -> None:
    latest = datetime(2026, 6, 1, 16, 20, tzinfo=UTC)
    stale_missing_slot = latest + timedelta(minutes=5)
    current_bucket = latest + timedelta(hours=2)
    bars = [make_bar(0, latest)]
    quality = {
        "market_closed": False,
        "session_unknown": False,
        "gaps": [
            {
                "from": latest,
                "to": stale_missing_slot,
                "missing_slots": [stale_missing_slot],
                "tail_gap": True,
            }
        ],
    }
    _configure_recovery(
        monkeypatch,
        quality=quality,
        slots=[10],
        live_bucket=current_bucket,
    )

    snapshot = chart_recovery_snapshot(bars, None, "5m", ibkr_stock_payload("ES"))

    assert snapshot.payloads[-1]["ts"] == latest.isoformat()
    assert snapshot.expected_live_slot == current_bucket.isoformat()
    assert snapshot.expected_live_close == (current_bucket + timedelta(minutes=5)).isoformat()
