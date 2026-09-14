from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar


def ibkr_keep_requested_cache_window(
    store: Any | None,
    interval: str,
    range_: str,
    cached: Sequence[Bar],
    warning_parts: list[str],
) -> list[Bar]:
    """Keep the requested provider window; never substitute out-of-window context."""

    _ = store, range_
    min_context_bars = 10 if interval == "60m" else 35
    if not cached or len(cached) >= min_context_bars:
        return list(cached)
    warning_parts.append(
        f"IBKR returned only {len(cached)} bars in the requested window; "
        "not extending chart with out-of-window recent cache."
    )
    return list(cached)
