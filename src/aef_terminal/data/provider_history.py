from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.provider_contract import (
    CanonicalHistoryRoute,
    CanonicalHistoryStorageKind,
    InstrumentRoute,
    ProviderAdapter,
)
from aef_terminal.data.instrument_identity import (
    instrument_key,
    qualified_instrument_id,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.domain import Bar, BarState
from aef_terminal.runtime.derived_timeframes import derive_chart_bars
from aef_terminal.runtime.bar_series import (
    dedupe_bars_by_timestamp,
    merge_history_candidates_by_authority,
    require_ordered_bar_list,
)
from aef_terminal.runtime.timeframes import (
    HistoryRangeWindow,
    canonical_bar_source_interval,
    history_range_window,
    interval_seconds,
    is_derived_chart_interval,
)
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection


@dataclass(frozen=True, slots=True)
class ProviderHistoryPage:
    bars: tuple[Bar, ...]
    next_before_ts: datetime | None
    has_more: bool


def read_provider_history_page(
    route: InstrumentRoute,
    timeframe: str,
    *,
    before_ts: datetime,
    limit: int,
    store: Any,
) -> ProviderHistoryPage:
    """Read one bounded canonical DB page immediately before ``before_ts``."""

    if before_ts.tzinfo is None or before_ts.utcoffset() is None:
        raise ValueError("before_ts must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if store is None:
        raise RuntimeError("canonical history store is required")

    boundary = before_ts.astimezone(UTC)
    source_timeframe = canonical_bar_source_interval(timeframe)
    target_seconds = interval_seconds(timeframe)
    source_seconds = interval_seconds(source_timeframe)
    if target_seconds % source_seconds:
        raise ValueError(
            f"HISTORY_PAGE_INTERVAL_RATIO_INVALID target={timeframe} source={source_timeframe}"
        )
    ratio = target_seconds // source_seconds
    source_limit = (limit + 1) * ratio * (2 if is_derived_chart_interval(timeframe) else 1)
    history_route = route.adapter.canonical_history_route(route.instrument)
    if history_route is not None:
        reader, route_kwargs = _canonical_futures_reader(store, history_route)
        source_bars = list(
            reader(
                provider=route.provider,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                symbol=route.instrument_key,
                timeframe=source_timeframe,
                end=boundary - timedelta(microseconds=1),
                limit=source_limit,
                latest=True,
                **route_kwargs,
            )
        )
    else:
        providers = tuple(str(item) for item in route.adapter.db_providers)
        grouped = store.read_bars_multi(
            timeframes=[source_timeframe],
            providers=list(providers),
            instrument=route.instrument,
            timeframe_read_windows={
                source_timeframe: (None, boundary, source_limit),
            },
        )
        source_bars = merge_history_candidates_by_authority(
            [
                (priority, grouped.get((provider, source_timeframe), []))
                for priority, provider in enumerate(providers)
            ]
        )

    projected = derive_chart_bars(
        dedupe_bars_by_timestamp(source_bars),
        timeframe,
        as_of=boundary,
    )
    confirmed = [
        bar
        for bar in projected
        if bar.ts.astimezone(UTC) < boundary
        and bar.closed
        and BarState(bar.state) is BarState.CONFIRMED
    ]
    candidates = sorted(
        dedupe_bars_by_timestamp(confirmed),
        key=lambda item: item.ts,
    )
    has_more = len(candidates) > limit or (
        is_derived_chart_interval(timeframe) and len(source_bars) >= source_limit
    )
    page_bars = tuple(candidates[-limit:])
    next_before_ts = page_bars[0].ts if page_bars else None
    if has_more and next_before_ts is None and source_bars:
        next_before_ts = min(bar.ts for bar in source_bars)
    return ProviderHistoryPage(
        bars=page_bars,
        next_before_ts=(next_before_ts if has_more else None),
        has_more=has_more,
    )


def _canonical_futures_reader(
    store: Any,
    history_route: CanonicalHistoryRoute,
) -> tuple[Any, dict[str, str]]:
    if history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS:
        reader = store.read_futures_canonical_bars
        route_kwargs = {
            "series_type": history_route.series_type,
            "roll_policy": history_route.roll_policy,
        }
    else:
        reader = store.read_futures_contract_bars
        route_kwargs = {"contract_key": history_route.contract_key}
    return reader, route_kwargs


def read_provider_history(
    store: Any | None,
    instrument: dict[str, Any],
    interval: str,
    range_: str,
    *,
    adapter: ProviderAdapter,
    window: HistoryRangeWindow | None = None,
) -> list[Bar]:
    if store is None:
        return []
    resolved_instrument = require_provider_identity(instrument, provider=adapter.key)
    instrument_id = qualified_instrument_id(resolved_instrument)
    fingerprint = route_fingerprint(resolved_instrument)
    display_key = instrument_key(resolved_instrument)
    source_interval = canonical_bar_source_interval(interval)
    providers = adapter.db_providers
    requested_window = window or history_range_window(range_, interval)
    start = requested_window.starts_at
    # Storage readers use an inclusive upper bound, while the canonical history
    # window is half-open. Keep the current/forming provider bucket out of the
    # confirmed snapshot without changing the shared coverage boundary.
    end = requested_window.ends_at - timedelta(microseconds=1)
    # The requested provider timeframe is authoritative. Finer bars remain
    # separate MTF context and must not be resampled onto a potentially
    # different provider grid (for example an opening partial 60m bar versus
    # epoch-aligned 15m buckets).
    grouped: dict[tuple[str, str], list[Bar]] = {}
    provider_candidates = tuple(str(item) for item in providers)
    if not provider_candidates:
        return []
    canonical_route = adapter.canonical_history_route(resolved_instrument)
    if canonical_route is not None:
        futures_reader, route_kwargs = _canonical_futures_reader(store, canonical_route)
        for timeframe in (source_interval,):
            bars = futures_reader(
                provider=adapter.key,
                instrument_id=instrument_id,
                route_fingerprint=fingerprint,
                symbol=display_key,
                timeframe=timeframe,
                start=start,
                end=end,
                **route_kwargs,
            )
            if bars:
                grouped[(adapter.key, timeframe)] = list(bars)
        exact_bars = list(grouped.get((adapter.key, source_interval), []))
    else:
        grouped = store.read_bars_multi(
            timeframes=[source_interval],
            providers=list(providers),
            start=start,
            end=end,
            instrument=resolved_instrument,
        )
        exact_bars = merge_history_candidates_by_authority(
            [
                (priority, grouped.get((str(provider), source_interval), []))
                for priority, provider in enumerate(providers)
            ]
        )
    return derive_chart_bars(
        dedupe_bars_by_timestamp(exact_bars),
        interval,
        as_of=requested_window.ends_at,
    )


def read_recent_provider_bars(
    route: InstrumentRoute,
    timeframe: str,
    *,
    store: Any,
    limit: int,
) -> list[Bar]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("PROVIDER_HISTORY_LIMIT_INVALID")
    source_timeframe = canonical_bar_source_interval(timeframe)
    source_limit = limit * 3 + 2 if is_derived_chart_interval(timeframe) else limit
    history_route = route.adapter.canonical_history_route(route.instrument)
    if history_route:
        futures_reader, route_kwargs = _canonical_futures_reader(store, history_route)
        source_bars = require_ordered_bar_list(
            futures_reader(
                provider=route.provider,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                symbol=route.instrument_key,
                timeframe=source_timeframe,
                limit=source_limit,
                latest=True,
                **route_kwargs,
            ),
            timeframe=source_timeframe,
            field="recent provider history",
            require_confirmed=True,
        )
    else:
        source_bars = require_ordered_bar_list(
            store.read_recent_bars(
                timeframe=source_timeframe,
                provider=route.provider,
                limit=source_limit,
                instrument=route.instrument,
            ),
            timeframe=source_timeframe,
            field="recent provider history",
            require_confirmed=True,
        )
    if not is_derived_chart_interval(timeframe):
        return source_bars
    projected = [bar for bar in derive_chart_bars(source_bars, timeframe) if bar.closed][-limit:]
    return require_ordered_bar_list(
        projected,
        timeframe=timeframe,
        field="derived recent provider history",
        require_confirmed=True,
    )


def read_recent_provider_bars_batch(
    requests: Sequence[tuple[InstrumentRoute, str, int]],
    *,
    store: Any,
) -> dict[tuple[str, str], list[Bar]]:
    """Read bounded recent tails for exact routes in one storage admission."""

    resolved: dict[tuple[str, str], tuple[InstrumentRoute, int]] = {}
    for route, timeframe, limit in requests:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("PROVIDER_HISTORY_BATCH_LIMIT_INVALID")
        key = (route.instrument_id, timeframe)
        requested_limit = limit
        previous = resolved.get(key)
        if previous is not None and previous[0].fingerprint != route.fingerprint:
            raise ValueError(
                "PROVIDER_HISTORY_BATCH_ROUTE_MISMATCH "
                f"instrument_id={route.instrument_id} timeframe={timeframe}"
            )
        if previous is None or requested_limit > previous[1]:
            resolved[key] = (route, requested_limit)

    storage_requests = []
    for (_instrument_id, timeframe), (route, limit) in resolved.items():
        source_timeframe = canonical_bar_source_interval(timeframe)
        source_limit = limit * 3 + 2 if is_derived_chart_interval(timeframe) else limit
        storage_requests.append((source_timeframe, route.provider, source_limit, route.instrument))
    recent_by_source = store.read_recent_bars_batch(storage_requests)
    if not isinstance(recent_by_source, dict):
        raise TypeError("canonical recent-bar storage must return a mapping")
    expected_source_keys = {
        (instrument_id, canonical_bar_source_interval(timeframe))
        for instrument_id, timeframe in resolved
    }
    if set(recent_by_source) != expected_source_keys:
        raise RuntimeError("canonical recent-bar storage returned incomplete scope membership")

    result: dict[tuple[str, str], list[Bar]] = {}
    for (instrument_id, timeframe), (route, limit) in resolved.items():
        source_timeframe = canonical_bar_source_interval(timeframe)
        source_bars = require_ordered_bar_list(
            recent_by_source[(instrument_id, source_timeframe)],
            timeframe=source_timeframe,
            field="canonical recent-bar storage",
            require_confirmed=True,
        )
        if is_derived_chart_interval(timeframe):
            projected = [bar for bar in derive_chart_bars(source_bars, timeframe) if bar.closed][
                -limit:
            ]
            result[(instrument_id, timeframe)] = require_ordered_bar_list(
                projected,
                timeframe=timeframe,
                field="derived canonical recent-bar storage",
                require_confirmed=True,
            )
        else:
            result[(instrument_id, timeframe)] = source_bars[-limit:]
    return result


def read_provider_bar_logical_projection(
    route: InstrumentRoute,
    timeframe: str,
    *,
    store: Any,
    anchor_timestamps: list[datetime],
) -> ConfirmedBarLogicalProjection:
    """Project exact confirmed timestamps without materializing intervening bars."""

    source_timeframe = canonical_bar_source_interval(timeframe)
    derived_timeframe = timeframe if is_derived_chart_interval(timeframe) else None
    derived_kwargs = (
        {"derived_timeframe": derived_timeframe} if derived_timeframe is not None else {}
    )
    history_route = route.adapter.canonical_history_route(route.instrument)
    if history_route:
        if history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS:
            reader = store.read_futures_canonical_logical_projection
            route_kwargs = {
                "series_type": history_route.series_type,
                "roll_policy": history_route.roll_policy,
            }
        else:
            reader = store.read_futures_contract_logical_projection
            route_kwargs = {"contract_key": history_route.contract_key}
        projection = reader(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe=source_timeframe,
            anchor_timestamps=anchor_timestamps,
            **derived_kwargs,
            **route_kwargs,
        )
    else:
        projection = store.read_bar_logical_projection(
            timeframe=source_timeframe,
            provider=route.provider,
            anchor_timestamps=anchor_timestamps,
            instrument=route.instrument,
            **derived_kwargs,
        )
    if not isinstance(projection, ConfirmedBarLogicalProjection):
        raise RuntimeError("canonical logical projection reader returned invalid data")
    return projection


def read_provider_confirmed_bar_timestamps(
    route: InstrumentRoute,
    timeframe: str,
    *,
    store: Any,
    anchor_timestamps: list[datetime],
) -> frozenset[datetime]:
    """Read a bounded exact set of confirmed anchors through the canonical route."""

    source_timeframe = canonical_bar_source_interval(timeframe)
    derived_timeframe = timeframe if is_derived_chart_interval(timeframe) else None
    derived_kwargs = (
        {"derived_timeframe": derived_timeframe} if derived_timeframe is not None else {}
    )
    history_route = route.adapter.canonical_history_route(route.instrument)
    if history_route:
        if history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS:
            reader = store.read_futures_canonical_confirmed_timestamps
            route_kwargs = {
                "series_type": history_route.series_type,
                "roll_policy": history_route.roll_policy,
            }
        else:
            reader = store.read_futures_contract_confirmed_timestamps
            route_kwargs = {"contract_key": history_route.contract_key}
        confirmed = reader(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe=source_timeframe,
            anchor_timestamps=anchor_timestamps,
            **derived_kwargs,
            **route_kwargs,
        )
    else:
        confirmed = store.read_confirmed_bar_timestamps(
            timeframe=source_timeframe,
            provider=route.provider,
            anchor_timestamps=anchor_timestamps,
            instrument=route.instrument,
            **derived_kwargs,
        )
    if not isinstance(confirmed, frozenset) or any(
        not isinstance(timestamp, datetime) for timestamp in confirmed
    ):
        raise RuntimeError("canonical confirmed-anchor reader returned invalid data")
    return confirmed
