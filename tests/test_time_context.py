from datetime import UTC, datetime

from aef_terminal.features.time_context import utc_daypart


def test_utc_daypart_is_explicit_and_does_not_infer_invalid_time() -> None:
    assert utc_daypart(datetime(2026, 7, 20, 13, 0, tzinfo=UTC)) == "utc_13_21"
    assert utc_daypart("2026-07-20T21:00:00Z") == "utc_21_24"
    assert utc_daypart(datetime(2026, 7, 20, 13, 0)) == "unknown"
    assert utc_daypart("2026-07-20T21:00:00") == "unknown"
    assert utc_daypart("") == "unknown"
    assert utc_daypart("not-a-timestamp") == "unknown"
