from __future__ import annotations

from datetime import datetime
from typing import Any

from aef_terminal.runtime.metrics import increment_metric
from aef_terminal.ui.quote_helpers import (
    execution_quote_geometry,
    quote_snapshot_ts,
)


def server_alert_price_snapshot(
    quote: dict[str, Any] | None,
) -> dict[str, Any] | None:
    current, low, high = execution_quote_geometry(quote)
    if current is None or low is None or high is None:
        increment_metric("server_alert_price_snapshots_total", status="unavailable")
        return None
    quote_ts = quote_snapshot_ts(quote)
    if not isinstance(quote_ts, datetime):
        increment_metric("server_alert_price_snapshots_total", status="timestamp_unknown")
        return None
    increment_metric("server_alert_price_snapshots_total", status="live")
    return {
        "price": current,
        "high": high,
        "low": low,
        "ts": quote_ts,
        "source": "quote",
    }
