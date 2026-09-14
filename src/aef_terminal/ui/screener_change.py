from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aef_terminal.data.instrument_identity import instrument_provider
from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.domain import Bar
from aef_terminal.runtime.math_utils import float_or_none


def previous_session_close_from_bars(
    instrument: dict[str, Any],
    bars: Sequence[Bar],
    store: Any | None,
) -> float | None:
    if any(not isinstance(bar, Bar) for bar in bars):
        raise TypeError("screener history must contain only Bar rows")
    if any(not bar.closed for bar in bars):
        raise ValueError("screener history must contain only confirmed bars")
    if len(bars) < 2 or store is None:
        return None
    latest = bars[-1]
    provider = instrument_provider(instrument)
    if not provider:
        return None
    intervals = store.read_trading_session_intervals(
        instrument=instrument,
        session_type="trading",
        limit=64,
    )
    current_open = None
    for row in reversed(intervals):
        opens_at = datetime.fromisoformat(str(row.get("opens_at") or ""))
        closes_at = datetime.fromisoformat(str(row.get("closes_at") or ""))
        if opens_at <= latest.ts < closes_at:
            current_open = opens_at
            break
    if current_open is None:
        return None
    previous = [bar for bar in bars if bar.ts < current_open]
    return float_or_none(previous[-1].close) if previous else None


def quote_close_base_for_route(route: InstrumentRoute, quote: dict[str, Any]) -> float | None:
    return route.adapter.quote_close_base(route.instrument, quote)


def watchlist_change_base(
    *,
    quote_close_base: float | None,
    previous_session_close: float | None,
    previous: Bar | None,
    latest: Bar | None,
) -> float | None:
    for value in (
        quote_close_base,
        previous_session_close,
        previous.close if previous is not None else None,
        latest.close if latest is not None else None,
    ):
        parsed = float_or_none(value)
        if parsed is not None:
            return parsed
    return None
