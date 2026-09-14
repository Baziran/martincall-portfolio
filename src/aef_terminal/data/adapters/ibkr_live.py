from __future__ import annotations

from typing import Any

from aef_terminal.data.adapters.ibkr_cache import ibkr_keep_requested_cache_window
from aef_terminal.data.provider_contract import ProviderAdapter
from aef_terminal.domain import Bar
from aef_terminal.runtime.timeframes import HistoryRangeWindow

IBKR_REQUESTED_WINDOW_EMPTY_WARNING = "No IBKR bars are available in the requested calendar window."


def _load_ibkr_cache_context(
    store: Any | None,
    interval: str,
    range_: str,
    instrument: dict[str, Any],
    *,
    adapter: ProviderAdapter,
    window: HistoryRangeWindow | None = None,
) -> tuple[list[Bar], list[str]]:
    from aef_terminal.data.provider_history import read_provider_history

    warning_parts: list[str] = []
    cached = read_provider_history(
        store,
        instrument,
        interval,
        range_,
        adapter=adapter,
        window=window,
    )
    cached = ibkr_keep_requested_cache_window(
        store,
        interval,
        range_,
        cached,
        warning_parts,
    )
    if not cached:
        warning_parts.append(IBKR_REQUESTED_WINDOW_EMPTY_WARNING)
    return cached, warning_parts
