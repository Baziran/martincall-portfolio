from __future__ import annotations

from typing import Any

from aef_terminal.data.providers import read_recent_provider_bars_batch
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.engine.common import interval_minutes
from aef_terminal.ui.screener_change import previous_session_close_from_bars


def screener_bar_limit(interval: str) -> int:
    minutes = max(interval_minutes(interval), 1)
    return max(600, min(int((5 * 24 * 60) / minutes), 7200))


def load_screener_bases(
    instruments: list[dict],
    interval: str,
    store: Any | None,
) -> dict[str, dict]:
    bases: dict[str, dict] = {}
    limit = screener_bar_limit(interval)
    routed = [(instrument, route_instrument(instrument)) for instrument in instruments]
    recent_by_route: dict[tuple[str, str], list[Bar]] = {}
    if store is not None:
        requests = [(route, interval, limit) for _instrument, route in routed]
        recent_by_route = read_recent_provider_bars_batch(requests, store=store)
    for instrument, route in routed:
        instrument_id = route.instrument_id
        recent = recent_by_route[(instrument_id, interval)] if store is not None else []
        latest = recent[-1] if recent else None
        previous = recent[-2] if len(recent) > 1 else None
        bases[instrument_id] = {
            "latest": latest,
            "previous": previous,
            "previous_session_close": previous_session_close_from_bars(instrument, recent, store),
        }
    return bases
