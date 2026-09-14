from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.data.providers import read_recent_provider_bars_batch, route_instrument
from aef_terminal.domain import Bar, BarState
from aef_terminal.engine.snapshot.db_context import provider_chart_axis_slots
from aef_terminal.runtime.chart_events import (
    chart_bars_updated_generation,
    require_chart_bars_generation,
)


RECENT_CONFIRMED_BAR_MAX_SCOPES = 128
RECENT_CONFIRMED_BAR_MAX_LIMIT = 1024
_LOAD_LOCK_COUNT = 32

RecentBarScope = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class RecentConfirmedBarContext:
    generation: int
    requested_limit: int
    bars: tuple[Bar, ...]
    confirmed_slots: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("RECENT_CONFIRMED_BAR_GENERATION_INVALID")
        if (
            isinstance(self.requested_limit, bool)
            or not isinstance(self.requested_limit, int)
            or not 0 < self.requested_limit <= RECENT_CONFIRMED_BAR_MAX_LIMIT
        ):
            raise ValueError("RECENT_CONFIRMED_BAR_LIMIT_INVALID")
        if not isinstance(self.bars, tuple) or any(not isinstance(bar, Bar) for bar in self.bars):
            raise TypeError("RECENT_CONFIRMED_BAR_ROWS_INVALID")
        if len(self.bars) > self.requested_limit:
            raise ValueError("RECENT_CONFIRMED_BAR_LIMIT_EXCEEDED")
        previous_ts = None
        timeframe = self.bars[0].timeframe if self.bars else None
        for bar in self.bars:
            if bar.timeframe != timeframe:
                raise ValueError("RECENT_CONFIRMED_BAR_TIMEFRAME_MISMATCH")
            if previous_ts is not None and bar.ts <= previous_ts:
                raise ValueError("RECENT_CONFIRMED_BAR_ORDER_INVALID")
            if bar.closed is not True or BarState(bar.state) is not BarState.CONFIRMED:
                raise ValueError("RECENT_CONFIRMED_BAR_STATE_INVALID")
            previous_ts = bar.ts
        if self.confirmed_slots is None:
            return
        if not isinstance(self.confirmed_slots, tuple) or any(
            isinstance(slot, bool) or not isinstance(slot, int) for slot in self.confirmed_slots
        ):
            raise TypeError("RECENT_CONFIRMED_BAR_SLOTS_INVALID")
        if len(self.confirmed_slots) != len(self.bars) or any(
            right <= left
            for left, right in zip(self.confirmed_slots, self.confirmed_slots[1:], strict=False)
        ):
            raise ValueError("RECENT_CONFIRMED_BAR_SLOTS_INVALID")


_CACHE_LOCK = threading.RLock()
_LOAD_LOCKS = tuple(threading.Lock() for _ in range(_LOAD_LOCK_COUNT))
_CACHE: OrderedDict[RecentBarScope, RecentConfirmedBarContext] = OrderedDict()
_LOADS = 0
_SLOT_LOADS = 0


def _scope(route: InstrumentRoute, timeframe: str) -> RecentBarScope:
    return (
        route.provider,
        route.instrument_id,
        route.fingerprint,
        timeframe,
    )


def _current_context(
    scope: RecentBarScope,
    *,
    generation: int,
    requested_limit: int,
) -> RecentConfirmedBarContext | None:
    with _CACHE_LOCK:
        cached = _CACHE.get(scope)
        if (
            cached is None
            or cached.generation != generation
            or cached.requested_limit < requested_limit
        ):
            return None
        _CACHE.move_to_end(scope)
        return cached


def recent_confirmed_bar_context(
    *,
    store: Any,
    instrument: dict[str, Any],
    timeframe: str,
    limit: int,
) -> RecentConfirmedBarContext:
    """Load one generation-fenced confirmed tail through the canonical route."""

    exact_timeframe = str(timeframe or "").strip()
    if not exact_timeframe:
        raise ValueError("RECENT_CONFIRMED_BAR_TIMEFRAME_REQUIRED")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 0 < limit <= RECENT_CONFIRMED_BAR_MAX_LIMIT
    ):
        raise ValueError("RECENT_CONFIRMED_BAR_LIMIT_INVALID")
    route = route_instrument(instrument)
    scope = _scope(route, exact_timeframe)
    generation = chart_bars_updated_generation(
        exact_timeframe,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    require_chart_bars_generation(
        generation,
        exact_timeframe,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    cached = _current_context(
        scope,
        generation=generation,
        requested_limit=limit,
    )
    if cached is not None:
        require_chart_bars_generation(
            generation,
            exact_timeframe,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        return RecentConfirmedBarContext(
            generation=cached.generation,
            requested_limit=cached.requested_limit,
            bars=cached.bars[-limit:],
        )

    with _LOAD_LOCKS[hash(scope) % _LOAD_LOCK_COUNT]:
        generation = chart_bars_updated_generation(
            exact_timeframe,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        require_chart_bars_generation(
            generation,
            exact_timeframe,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        cached = _current_context(
            scope,
            generation=generation,
            requested_limit=limit,
        )
        if cached is not None:
            require_chart_bars_generation(
                generation,
                exact_timeframe,
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            return RecentConfirmedBarContext(
                generation=cached.generation,
                requested_limit=cached.requested_limit,
                bars=cached.bars[-limit:],
            )

        recent_by_scope = read_recent_provider_bars_batch(
            [(route, exact_timeframe, limit)],
            store=store,
        )
        bars = tuple(recent_by_scope[(route.instrument_id, exact_timeframe)])
        require_chart_bars_generation(
            generation,
            exact_timeframe,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        loaded = RecentConfirmedBarContext(
            generation=generation,
            requested_limit=limit,
            bars=bars,
        )
        global _LOADS
        with _CACHE_LOCK:
            _CACHE[scope] = loaded
            _LOADS += 1
            _CACHE.move_to_end(scope)
            while len(_CACHE) > RECENT_CONFIRMED_BAR_MAX_SCOPES:
                _CACHE.popitem(last=False)
        return loaded


def recent_confirmed_bar_cache_stats() -> dict[str, int]:
    with _CACHE_LOCK:
        return {
            "entries": len(_CACHE),
            "bars": sum(len(context.bars) for context in _CACHE.values()),
            "loads": _LOADS,
            "slot_loads": _SLOT_LOADS,
            "max_entries": RECENT_CONFIRMED_BAR_MAX_SCOPES,
            "max_bars_per_entry": RECENT_CONFIRMED_BAR_MAX_LIMIT,
        }


def recent_confirmed_bar_slots(
    *,
    store: Any,
    instrument: dict[str, Any],
    timeframe: str,
    context: RecentConfirmedBarContext,
) -> list[int] | None:
    """Resolve and retain exact provider slots for one cached confirmed suffix."""

    if not isinstance(context, RecentConfirmedBarContext):
        raise TypeError("RECENT_CONFIRMED_BAR_CONTEXT_REQUIRED")
    bars = context.bars
    if not bars:
        return []
    route = route_instrument(instrument)
    exact_timeframe = str(timeframe or "").strip()
    if any(bar.timeframe != exact_timeframe for bar in bars):
        raise ValueError("RECENT_CONFIRMED_BAR_TIMEFRAME_MISMATCH")
    scope = _scope(route, exact_timeframe)
    generation = context.generation
    require_chart_bars_generation(
        generation,
        exact_timeframe,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    with _LOAD_LOCKS[hash(scope) % _LOAD_LOCK_COUNT]:
        cached = _current_context(
            scope,
            generation=generation,
            requested_limit=context.requested_limit,
        )
        if cached is None or cached.bars[-len(bars) :] != bars:
            return None
        if cached.confirmed_slots is not None:
            return list(cached.confirmed_slots[-len(bars) :])
        slots = provider_chart_axis_slots(
            store,
            instrument,
            exact_timeframe,
            cached.bars,
        )
        require_chart_bars_generation(
            generation,
            exact_timeframe,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        if slots is None:
            return None
        if any(isinstance(slot, bool) or not isinstance(slot, int) for slot in slots):
            raise TypeError("RECENT_CONFIRMED_BAR_SLOTS_INVALID")
        exact_slots = tuple(slots)
        retained = RecentConfirmedBarContext(
            generation=cached.generation,
            requested_limit=cached.requested_limit,
            bars=cached.bars,
            confirmed_slots=exact_slots,
        )
        global _SLOT_LOADS
        with _CACHE_LOCK:
            current = _CACHE.get(scope)
            if current is cached:
                _CACHE[scope] = retained
                _CACHE.move_to_end(scope)
                _SLOT_LOADS += 1
            elif current is not None and current.confirmed_slots is not None:
                retained = current
            else:
                return None
        return list(retained.confirmed_slots[-len(bars) :])


def clear_recent_confirmed_bar_cache() -> None:
    global _LOADS, _SLOT_LOADS
    with _CACHE_LOCK:
        _CACHE.clear()
        _LOADS = 0
        _SLOT_LOADS = 0
