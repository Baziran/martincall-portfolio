from datetime import UTC, datetime

import pytest

from aef_terminal.ui.services.chart_stream_payload_tracker import ChartStreamPayloadTracker


def _parse_ts(payload: dict[str, object] | None) -> datetime | None:
    if not payload:
        return None
    raw = payload.get("ts")
    return datetime.fromisoformat(str(raw)) if raw else None


def _signature(payload: dict[str, object] | None) -> tuple:
    if not payload:
        return ()
    return (payload.get("ts"), payload.get("close"), payload.get("closed"))


def test_chart_stream_payload_tracker_remembers_latest_payload_state() -> None:
    tracker = ChartStreamPayloadTracker(parse_stream_ts=_parse_ts, stream_bar_signature=_signature)

    tracker.remember(
        [
            {"ts": "2026-07-02T14:00:00+00:00", "close": 100.0, "closed": True},
            {"ts": "2026-07-02T14:05:00+00:00", "close": 101.0, "closed": False},
        ]
    )

    assert tracker.last_sent_ts == datetime(2026, 7, 2, 14, 5, tzinfo=UTC)
    assert tracker.last_signature == ("2026-07-02T14:05:00+00:00", 101.0, False)
    assert tracker.last_sent_closed is False


def test_chart_stream_payload_tracker_detects_changed_payloads() -> None:
    tracker = ChartStreamPayloadTracker(parse_stream_ts=_parse_ts, stream_bar_signature=_signature)
    original = {"ts": "2026-07-02T14:00:00+00:00", "close": 100.0, "closed": True}
    changed = {"ts": "2026-07-02T14:00:00+00:00", "close": 100.5, "closed": True}

    tracker.remember([original])

    assert tracker.payload_is_new_or_changed(original) is False
    assert tracker.payload_is_new_or_changed(changed) is True


def test_chart_stream_payload_tracker_rejects_malformed_batches_and_timestamps() -> None:
    tracker = ChartStreamPayloadTracker(parse_stream_ts=_parse_ts, stream_bar_signature=_signature)

    with pytest.raises(TypeError, match="batch must be a list"):
        tracker.remember(())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a mapping"):
        tracker.remember([object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="TIMESTAMP_INVALID"):
        tracker.payload_is_new_or_changed({"close": 100.0, "closed": True})
    with pytest.raises(ValueError, match="INVALIDATION_TIMESTAMP_INVALID"):
        tracker.invalidate_timestamps(["not-a-timestamp"])


@pytest.mark.parametrize("max_signatures", [True, 0, 15, "512"])
def test_chart_stream_payload_tracker_requires_exact_bounded_capacity(
    max_signatures: object,
) -> None:
    with pytest.raises(ValueError, match="MAX_SIGNATURES_INVALID"):
        ChartStreamPayloadTracker(
            parse_stream_ts=_parse_ts,
            stream_bar_signature=_signature,
            max_signatures=max_signatures,  # type: ignore[arg-type]
        )


def test_chart_stream_payload_tracker_replays_invalidated_slot_until_send_ack() -> None:
    invalidated_ts = "2026-07-02T13:55:00+00:00"
    payload = {"ts": invalidated_ts, "close": 99.5, "closed": True}
    tracker = ChartStreamPayloadTracker(
        parse_stream_ts=_parse_ts,
        stream_bar_signature=_signature,
        initial_ts=datetime(2026, 7, 2, 14, 5, tzinfo=UTC),
    )

    assert tracker.payload_is_new_or_changed(payload) is False
    assert tracker.invalidate_timestamps([invalidated_ts]) == 1
    assert tracker.invalidated_timestamps == (invalidated_ts,)
    assert tracker.payload_is_new_or_changed(payload) is True
    assert tracker.payload_is_new_or_changed(payload) is True

    tracker.remember([payload])

    assert tracker.invalidated_timestamps == ()
    assert tracker.payload_is_new_or_changed(payload) is False


def test_chart_stream_payload_tracker_invalidations_are_exact_timestamp_scoped() -> None:
    tracker = ChartStreamPayloadTracker(
        parse_stream_ts=_parse_ts,
        stream_bar_signature=_signature,
        initial_ts=datetime(2026, 7, 2, 14, 5, tzinfo=UTC),
    )
    tracker.invalidate_timestamps(["2026-07-02T13:55:00Z"])

    assert tracker.invalidated_timestamps == ("2026-07-02T13:55:00+00:00",)
    assert (
        tracker.payload_is_new_or_changed(
            {"ts": "2026-07-02T13:55:00+00:00", "close": 99.5, "closed": True}
        )
        is True
    )
    assert (
        tracker.payload_is_new_or_changed(
            {"ts": "2026-07-02T14:00:00+00:00", "close": 100.0, "closed": True}
        )
        is False
    )
