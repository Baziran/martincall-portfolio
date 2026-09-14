from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aef_terminal.data.adapters.ibkr_live import _load_ibkr_cache_context
from aef_terminal.data.ibkr.history import (
    write_continuous_history,
)
from aef_terminal.data.instrument_identity import (
    instrument_is_futures_root,
    provider_symbol,
)
from aef_terminal.data.provider_contract import CanonicalBarCommitReceipt, ProviderAdapter
from aef_terminal.domain import Bar
from aef_terminal.runtime import pine
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.timeframes import HistoryRangeWindow

_LOGGER = logging.getLogger(__name__)


AsyncLiveBarStorageRoute = Callable[
    [Any | None, list[Bar], str, str, dict[str, Any], int],
    Awaitable[tuple[CanonicalBarCommitReceipt, str]],
]


async def _write_ibkr_futures_continuous_bars_async(
    store: Any | None,
    live_bars: list[Bar],
    interval: str,
    range_: str,
    instrument: dict[str, Any],
    revision_sequence: int,
) -> tuple[CanonicalBarCommitReceipt, str]:
    if store is None or not live_bars:
        return CanonicalBarCommitReceipt(revision_sequence=revision_sequence), ""
    symbol = provider_symbol(instrument, "ibkr")
    try:
        receipt = await run_physical_thread_call(
            write_continuous_history,
            store,
            live_bars,
            instrument=instrument,
            revision_sequence=revision_sequence,
        )
        return receipt, ""
    except Exception as exc:
        _LOGGER.exception(
            "ibkr_provider_continuous_write_failed loader=stream symbol=%s interval=%s range=%s rows=%s",
            symbol,
            interval,
            range_,
            len(live_bars),
        )
        return (
            CanonicalBarCommitReceipt(revision_sequence=revision_sequence),
            f"IBKR continuous futures write failed: {exc}",
        )


async def _write_ibkr_provider_live_bars_async(
    store: Any | None,
    live_bars: list[Bar],
    interval: str,
    range_: str,
    instrument: dict[str, Any],
    revision_sequence: int,
) -> tuple[CanonicalBarCommitReceipt, str]:
    if store is None or not live_bars:
        return CanonicalBarCommitReceipt(revision_sequence=revision_sequence), ""
    symbol = provider_symbol(instrument, "ibkr")
    try:
        receipt = await run_physical_thread_call(
            store.write_bars,
            live_bars,
            "ibkr",
            instrument=instrument,
            revision_sequence=revision_sequence,
        )
        return receipt, ""
    except Exception:
        _LOGGER.exception(
            "ibkr_live_bar_write_failed loader=stream symbol=%s interval=%s range=%s rows=%s",
            symbol,
            interval,
            range_,
            len(live_bars),
        )
        return (
            CanonicalBarCommitReceipt(revision_sequence=revision_sequence),
            "IBKR live bar write failed",
        )


def _select_ibkr_live_bar_storage_route_async(
    instrument: dict[str, Any],
) -> AsyncLiveBarStorageRoute:
    if instrument_is_futures_root(instrument):
        return _write_ibkr_futures_continuous_bars_async
    return _write_ibkr_provider_live_bars_async


def load_ibkr_bars(
    instrument: dict[str, str],
    interval: str,
    range_: str,
    timeout: float,
    live_refresh: bool = False,
    store: Any | None = None,
    *,
    adapter: ProviderAdapter,
    window: HistoryRangeWindow | None = None,
) -> tuple[list[Bar], str]:
    """Read exchange-confirmed IBKR bars from canonical storage.

    Broker history is intentionally not part of this adapter contract. Realtime
    chart streams persist closed bars, while the background gap engine owns any
    historical repair. A user-facing read must never create a broker request.
    """
    del timeout, live_refresh
    symbol = provider_symbol(instrument, "ibkr")
    cached, warning_parts = _load_ibkr_cache_context(
        store,
        interval,
        range_,
        instrument,
        adapter=adapter,
        window=window,
    )
    merged = pine.collapse_bars(
        cached,
        interval,
        symbol=symbol,
        source_name="ibkr:db-cache" if cached else "ibkr:no-data",
    )
    return merged, "; ".join(warning_parts)


async def async_load_ibkr_bars(
    instrument: dict[str, str],
    interval: str,
    range_: str,
    timeout: float,
    live_refresh: bool = False,
    store: Any | None = None,
    *,
    adapter: ProviderAdapter,
    window: HistoryRangeWindow | None = None,
) -> tuple[list[Bar], str]:
    return await run_physical_thread_call(
        load_ibkr_bars,
        instrument,
        interval,
        range_,
        timeout,
        live_refresh,
        store,
        adapter=adapter,
        window=window,
    )
