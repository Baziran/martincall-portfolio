from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import Bar, bar_ingress_revision_signature
from aef_terminal.runtime.timeframes import canonical_bar_source_interval


ChartRefreshKey = tuple[str, str, str]


@dataclass(frozen=True)
class ChartBarsUpdatedEvent:
    provider: str
    instrument_id: str
    interval: str
    generation: int
    route_fingerprint: str
    reason: str
    bars: tuple[Bar, ...]
    gap: dict[str, Any]
    repair: dict[str, Any]


@dataclass(frozen=True)
class LiveChartBarsEvent:
    provider: str
    instrument_id: str
    interval: str
    generation: int
    route_fingerprint: str
    bars: tuple[Bar, ...]
    observed_at: datetime
    reason: str = "provider_update"


@dataclass(frozen=True, slots=True)
class ChartBarsWriteGuard:
    """Process-owned guard for one canonical storage publication."""

    instrument_id: str
    route_fingerprint: str
    interval: str
    expected_generation: int
    token: int


@dataclass(frozen=True, slots=True)
class ChartBarsGenerationScope:
    instrument_id: str
    route_fingerprint: str
    interval: str
    generation: int


@dataclass(frozen=True, slots=True)
class ChartBarsPublicationGuard:
    scopes: tuple[tuple[ChartRefreshKey, int], ...]
    token: int


_lock = threading.Lock()
_generations: dict[ChartRefreshKey, int] = {}
_events: dict[ChartRefreshKey, list[ChartBarsUpdatedEvent]] = {}
_writes_in_progress: dict[ChartRefreshKey, int] = {}
_publications_in_progress: dict[ChartRefreshKey, set[int]] = {}
_next_write_token = 0
_next_publication_token = 0
_waiters: list[tuple[ChartRefreshKey, asyncio.AbstractEventLoop, asyncio.Event]] = []
_stable_waiters: list[tuple[ChartRefreshKey, asyncio.AbstractEventLoop, asyncio.Event]] = []
_MAX_EVENTS_PER_KEY = 32
_MAX_REFRESH_KEYS = 1024
_MAX_GENERATION_KEYS = 4096
_live_generations: dict[ChartRefreshKey, int] = {}
_live_events: dict[ChartRefreshKey, LiveChartBarsEvent] = {}
_live_signatures: dict[ChartRefreshKey, tuple[tuple[Any, ...], ...]] = {}
_live_waiters: list[tuple[ChartRefreshKey, asyncio.AbstractEventLoop, asyncio.Event]] = []
_MAX_LIVE_KEYS = 512


class ChartBarsGenerationChanged(RuntimeError):
    def __init__(
        self,
        *,
        expected: int,
        observed: int,
        write_in_progress: bool = False,
        publication_in_progress: bool = False,
    ) -> None:
        self.expected = int(expected)
        self.observed = int(observed)
        self.write_in_progress = bool(write_in_progress)
        self.publication_in_progress = bool(publication_in_progress)
        if self.write_in_progress:
            super().__init__(f"CHART_BARS_WRITE_IN_PROGRESS generation={self.observed}")
            return
        if self.publication_in_progress:
            super().__init__(f"CHART_BARS_PUBLICATION_IN_PROGRESS generation={self.observed}")
            return
        super().__init__(
            f"CHART_BARS_GENERATION_CHANGED expected={self.expected} observed={self.observed}"
        )


def _trim_live_events_locked(protected_key: ChartRefreshKey) -> None:
    active_waiter_keys = {waiter_key for waiter_key, _loop, _event in _live_waiters}
    while len(_live_events) > _MAX_LIVE_KEYS:
        stale_key = next(
            (
                candidate
                for candidate in _live_events
                if candidate != protected_key and candidate not in active_waiter_keys
            ),
            None,
        )
        if stale_key is None:
            break
        _live_events.pop(stale_key, None)
        _live_signatures.pop(stale_key, None)
    if len(_live_generations) > _MAX_GENERATION_KEYS:
        stale_generation_key = next(
            (
                candidate
                for candidate in _live_generations
                if candidate != protected_key
                and candidate not in _live_events
                and candidate not in active_waiter_keys
            ),
            None,
        )
        if stale_generation_key is not None:
            _live_generations.pop(stale_generation_key, None)


def _key(
    instrument_id: str,
    interval: str,
    route_fingerprint: str,
) -> ChartRefreshKey:
    return (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
        canonical_bar_source_interval(interval),
    )


def chart_bars_updated_generation(
    interval: str,
    route_fingerprint: str,
    *,
    instrument_id: str,
) -> int:
    key = _key(instrument_id, interval, route_fingerprint)
    with _lock:
        return _generations.get(key, 0)


def require_chart_bars_generation(
    expected: int,
    interval: str,
    route_fingerprint: str,
    *,
    instrument_id: str,
) -> int:
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise ValueError("expected canonical generation must be a non-negative integer")
    key = _key(instrument_id, interval, route_fingerprint)
    with _lock:
        observed = _generations.get(key, 0)
        write_in_progress = key in _writes_in_progress
    if write_in_progress or observed != expected:
        raise ChartBarsGenerationChanged(
            expected=expected,
            observed=observed,
            write_in_progress=write_in_progress,
        )
    return observed


def begin_chart_bars_write(
    expected_generation: int,
    interval: str,
    route_fingerprint: str,
    *,
    instrument_id: str,
) -> ChartBarsWriteGuard:
    """Mark the DB-commit to generation-publication interval as unstable."""

    global _next_write_token
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
    ):
        raise ValueError("expected canonical generation must be a non-negative integer")
    key = _key(instrument_id, interval, route_fingerprint)
    if not all(key):
        raise ValueError("canonical chart write identity is required")
    with _lock:
        observed = _generations.get(key, 0)
        if key in _writes_in_progress:
            raise ChartBarsGenerationChanged(
                expected=expected_generation,
                observed=observed,
                write_in_progress=True,
            )
        if _publications_in_progress.get(key):
            raise ChartBarsGenerationChanged(
                expected=expected_generation,
                observed=observed,
                publication_in_progress=True,
            )
        if observed != expected_generation:
            raise ChartBarsGenerationChanged(
                expected=expected_generation,
                observed=observed,
            )
        _next_write_token += 1
        token = _next_write_token
        _writes_in_progress[key] = token
    return ChartBarsWriteGuard(
        instrument_id=key[0],
        route_fingerprint=key[1],
        interval=key[2],
        expected_generation=expected_generation,
        token=token,
    )


def begin_chart_bars_publication(
    scopes: Sequence[ChartBarsGenerationScope],
) -> ChartBarsPublicationGuard:
    """Fence one synchronous authority effect to exact canonical inputs."""

    global _next_publication_token
    normalized: dict[ChartRefreshKey, int] = {}
    for scope in scopes:
        if not isinstance(scope, ChartBarsGenerationScope):
            raise TypeError("canonical chart generation scope is required")
        generation = scope.generation
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("canonical chart generation must be a non-negative integer")
        key = _key(
            scope.instrument_id,
            scope.interval,
            scope.route_fingerprint,
        )
        previous = normalized.get(key)
        if previous is not None and previous != generation:
            raise ValueError("canonical chart publication scope generations conflict")
        normalized[key] = generation
    if not normalized:
        raise ValueError("canonical chart publication requires at least one scope")
    ordered = tuple(sorted(normalized.items()))
    with _lock:
        for key, expected in ordered:
            observed = _generations.get(key, 0)
            write_in_progress = key in _writes_in_progress
            if write_in_progress or observed != expected:
                raise ChartBarsGenerationChanged(
                    expected=expected,
                    observed=observed,
                    write_in_progress=write_in_progress,
                )
        _next_publication_token += 1
        token = _next_publication_token
        for key, _expected in ordered:
            _publications_in_progress.setdefault(key, set()).add(token)
    return ChartBarsPublicationGuard(scopes=ordered, token=token)


def end_chart_bars_publication(guard: ChartBarsPublicationGuard) -> None:
    if not isinstance(guard, ChartBarsPublicationGuard):
        raise TypeError("canonical chart publication guard is required")
    with _lock:
        if any(
            guard.token not in _publications_in_progress.get(key, set())
            for key, _expected in guard.scopes
        ):
            raise RuntimeError("CHART_BARS_PUBLICATION_GUARD_OWNERSHIP_LOST")
        for key, _expected in guard.scopes:
            tokens = _publications_in_progress[key]
            tokens.remove(guard.token)
            if not tokens:
                _publications_in_progress.pop(key, None)


def end_chart_bars_write(guard: ChartBarsWriteGuard) -> None:
    """Release a proven-stable guard returned by ``begin``.

    Callers may use this only before storage starts, after a validated no-op,
    or after the matching generation was published.  An uncertain storage
    outcome must go through ``invalidate_chart_bars_write`` instead.
    """

    if not isinstance(guard, ChartBarsWriteGuard):
        raise TypeError("canonical chart write guard is required")
    key = _key(
        guard.instrument_id,
        guard.interval,
        guard.route_fingerprint,
    )
    with _lock:
        if _writes_in_progress.get(key) != guard.token:
            raise RuntimeError("CHART_BARS_WRITE_GUARD_OWNERSHIP_LOST")
        _writes_in_progress.pop(key, None)
        stable_waiters = [
            (loop, waiter_event)
            for waiter_key, loop, waiter_event in _stable_waiters
            if waiter_key == key
        ]
    _wake_chart_bars_waiters(stable_waiters)


def _record_chart_bars_updated_locked(
    key: ChartRefreshKey,
    *,
    provider: str,
    generation: int,
    reason: str,
    bars: Sequence[Bar],
    gap: dict[str, Any] | None,
    repair: dict[str, Any] | None,
) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]:
    event = ChartBarsUpdatedEvent(
        provider=str(provider or "").strip().lower(),
        instrument_id=key[0],
        interval=key[2],
        generation=generation,
        route_fingerprint=key[1],
        reason=str(reason or "canonical_commit"),
        bars=tuple(bars),
        gap=dict(gap or {}),
        repair=dict(repair or {}),
    )
    queue = _events.setdefault(key, [])
    queue.append(event)
    if len(queue) > _MAX_EVENTS_PER_KEY:
        del queue[: len(queue) - _MAX_EVENTS_PER_KEY]
    active_waiter_keys = {waiter_key for waiter_key, _loop, _event in (*_waiters, *_stable_waiters)}
    if len(_events) > _MAX_REFRESH_KEYS:
        stale_key = next(
            (
                candidate
                for candidate in _events
                if candidate != key and candidate not in active_waiter_keys
            ),
            None,
        )
        if stale_key is not None:
            _events.pop(stale_key, None)
    if len(_generations) > _MAX_GENERATION_KEYS:
        stale_generation_key = next(
            (
                candidate
                for candidate in _generations
                if candidate != key
                and candidate not in _events
                and candidate not in active_waiter_keys
                and candidate not in _writes_in_progress
                and candidate not in _publications_in_progress
            ),
            None,
        )
        if stale_generation_key is not None:
            _generations.pop(stale_generation_key, None)
    return [
        (loop, waiter_event) for waiter_key, loop, waiter_event in _waiters if waiter_key == key
    ]


def _wake_chart_bars_waiters(
    waiters: Sequence[tuple[asyncio.AbstractEventLoop, asyncio.Event]],
) -> None:
    for loop, waiter_event in waiters:
        try:
            loop.call_soon_threadsafe(waiter_event.set)
        except RuntimeError:
            continue


def invalidate_chart_bars_write(
    guard: ChartBarsWriteGuard,
    provider: str,
    *,
    reason: str,
    repair: dict[str, Any] | None = None,
) -> int:
    """Atomically invalidate canonical reads and release an uncertain write.

    Once storage work has started, an exception or invalid receipt cannot prove
    that the database stayed unchanged.  This recovery owner advances the
    generation before releasing the scope.  If recovery itself fails, the
    guard remains active and readers continue to fail closed.
    """

    if not isinstance(guard, ChartBarsWriteGuard):
        raise TypeError("canonical chart write guard is required")
    key = _key(
        guard.instrument_id,
        guard.interval,
        guard.route_fingerprint,
    )
    with _lock:
        if _writes_in_progress.get(key) != guard.token:
            raise RuntimeError("CHART_BARS_WRITE_GUARD_OWNERSHIP_LOST")
        observed = _generations.get(key, 0)
        if observed < guard.expected_generation:
            raise RuntimeError("CHART_BARS_WRITE_GENERATION_REGRESSED")
        waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        if observed == guard.expected_generation:
            generation = observed + 1
            _generations[key] = generation
            waiters = _record_chart_bars_updated_locked(
                key,
                provider=provider,
                generation=generation,
                reason=reason,
                bars=(),
                gap=None,
                repair=repair,
            )
        else:
            generation = observed
        _writes_in_progress.pop(key, None)
        stable_waiters = [
            (loop, waiter_event)
            for waiter_key, loop, waiter_event in _stable_waiters
            if waiter_key == key
        ]
    _wake_chart_bars_waiters(waiters)
    _wake_chart_bars_waiters(stable_waiters)
    return generation


def publish_chart_bars_updated(
    provider: str,
    interval: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    reason: str = "canonical_commit",
    bars: Sequence[Bar] = (),
    gap: dict[str, Any] | None = None,
    repair: dict[str, Any] | None = None,
    expected_generation: int | None = None,
) -> int:
    key = _key(instrument_id, interval, route_fingerprint)
    if not all(key):
        return 0
    if expected_generation is not None and (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
    ):
        raise ValueError("expected_generation must be a non-negative integer")
    with _lock:
        if expected_generation is not None and _generations.get(key, 0) != expected_generation:
            return 0
        generation = _generations.get(key, 0) + 1
        _generations[key] = generation
        waiters = _record_chart_bars_updated_locked(
            key,
            provider=provider,
            generation=generation,
            reason=reason,
            bars=bars,
            gap=gap,
            repair=repair,
        )
    _wake_chart_bars_waiters(waiters)
    return generation


def chart_bars_updated_events_since(
    interval: str,
    route_fingerprint: str,
    seen_generation: int,
    *,
    instrument_id: str,
) -> tuple[int, list[ChartBarsUpdatedEvent]]:
    key = _key(instrument_id, interval, route_fingerprint)
    with _lock:
        generation = _generations.get(key, 0)
        events = [
            event for event in _events.get(key, []) if int(event.generation) > int(seen_generation)
        ]
    return generation, events


async def wait_for_chart_bars_updated(
    interval: str,
    route_fingerprint: str,
    seen_generation: int,
    timeout: float,
    *,
    instrument_id: str,
) -> tuple[int, list[ChartBarsUpdatedEvent]]:
    key = _key(instrument_id, interval, route_fingerprint)
    if not all(key) or timeout <= 0:
        return chart_bars_updated_events_since(
            interval,
            route_fingerprint,
            seen_generation,
            instrument_id=instrument_id,
        )
    loop = asyncio.get_running_loop()
    waiter_event = asyncio.Event()
    waiter = (key, loop, waiter_event)
    with _lock:
        current = _generations.get(key, 0)
        if current != seen_generation:
            events = [
                event
                for event in _events.get(key, [])
                if int(event.generation) > int(seen_generation)
            ]
            return current, events
        _waiters.append(waiter)
    try:
        try:
            await asyncio.wait_for(waiter_event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return chart_bars_updated_events_since(
            interval,
            route_fingerprint,
            seen_generation,
            instrument_id=instrument_id,
        )
    finally:
        with _lock:
            try:
                _waiters.remove(waiter)
            except ValueError:
                pass


async def wait_for_chart_bars_stable(
    interval: str,
    route_fingerprint: str,
    timeout: float,
    *,
    instrument_id: str,
) -> int:
    """Wait for one exact canonical chart scope to have no active writer.

    A generation publication does not make the scope readable: the matching
    writer retains its guard through publication and releases it only after the
    canonical commit has fully settled.  A replacement writer may acquire the
    scope before a released waiter resumes, so every wake is rechecked against
    one fixed deadline.
    """

    key = _key(instrument_id, interval, route_fingerprint)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout), 0.0)
    while True:
        waiter_event = asyncio.Event()
        waiter = (key, loop, waiter_event)
        with _lock:
            observed = _generations.get(key, 0)
            if key not in _writes_in_progress:
                return observed
            remaining = deadline - loop.time()
            if remaining > 0:
                _stable_waiters.append(waiter)
        if remaining <= 0:
            raise ChartBarsGenerationChanged(
                expected=observed,
                observed=observed,
                write_in_progress=True,
            )
        try:
            try:
                await asyncio.wait_for(waiter_event.wait(), timeout=remaining)
            except TimeoutError:
                with _lock:
                    observed = _generations.get(key, 0)
                    write_in_progress = key in _writes_in_progress
                if not write_in_progress:
                    return observed
                raise ChartBarsGenerationChanged(
                    expected=observed,
                    observed=observed,
                    write_in_progress=True,
                ) from None
        finally:
            with _lock:
                try:
                    _stable_waiters.remove(waiter)
                except ValueError:
                    pass


def publish_live_chart_bars(
    provider: str,
    interval: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    bars: Sequence[Bar],
    reason: str = "provider_update",
) -> int:
    """Publish an immutable provider snapshot without exposing broker objects."""

    key = _key(instrument_id, interval, route_fingerprint)
    if not all(key):
        raise ValueError("LIVE_CHART_EVENT_IDENTITY_REQUIRED")
    normalized = tuple(bars)
    signature = tuple(bar_ingress_revision_signature(bar) for bar in normalized)
    with _lock:
        if _live_signatures.get(key) == signature:
            return _live_generations.get(key, 0)
        generation = _live_generations.get(key, 0) + 1
        _live_generations[key] = generation
        _live_signatures[key] = signature
        _live_events[key] = LiveChartBarsEvent(
            provider=str(provider or "").strip().lower(),
            instrument_id=key[0],
            interval=key[2],
            generation=generation,
            route_fingerprint=key[1],
            bars=normalized,
            observed_at=datetime.now(tz=UTC),
            reason=str(reason or "provider_update"),
        )
        _trim_live_events_locked(key)
        waiters = [(loop, event) for waiter_key, loop, event in _live_waiters if waiter_key == key]
    for loop, waiter_event in waiters:
        try:
            loop.call_soon_threadsafe(waiter_event.set)
        except RuntimeError:
            continue
    return generation


def live_chart_bars_snapshot(
    interval: str,
    route_fingerprint: str,
    *,
    instrument_id: str,
) -> LiveChartBarsEvent | None:
    key = _key(instrument_id, interval, route_fingerprint)
    with _lock:
        return _live_events.get(key)


async def wait_for_live_chart_bars(
    interval: str,
    route_fingerprint: str,
    seen_generation: int,
    timeout: float,
    *,
    instrument_id: str,
) -> tuple[int, LiveChartBarsEvent | None]:
    key = _key(instrument_id, interval, route_fingerprint)
    with _lock:
        current = _live_generations.get(key, 0)
        if current != int(seen_generation) or timeout <= 0:
            return current, _live_events.get(key)
        loop = asyncio.get_running_loop()
        waiter_event = asyncio.Event()
        waiter = (key, loop, waiter_event)
        _live_waiters.append(waiter)
    try:
        try:
            await asyncio.wait_for(waiter_event.wait(), timeout=max(float(timeout), 0.0))
        except TimeoutError:
            pass
        with _lock:
            return _live_generations.get(key, 0), _live_events.get(key)
    finally:
        with _lock:
            try:
                _live_waiters.remove(waiter)
            except ValueError:
                pass


def clear_live_chart_bars(
    interval: str,
    route_fingerprint: str,
    *,
    instrument_id: str,
) -> None:
    key = _key(instrument_id, interval, route_fingerprint)
    with _lock:
        generation = _live_generations.get(key, 0) + 1
        _live_generations[key] = generation
        _live_signatures.pop(key, None)
        _live_events[key] = LiveChartBarsEvent(
            provider="",
            instrument_id=key[0],
            interval=key[2],
            generation=generation,
            route_fingerprint=key[1],
            bars=(),
            observed_at=datetime.now(tz=UTC),
            reason="stream_closed",
        )
        _trim_live_events_locked(key)
        waiters = [(loop, event) for waiter_key, loop, event in _live_waiters if waiter_key == key]
    for loop, waiter_event in waiters:
        try:
            loop.call_soon_threadsafe(waiter_event.set)
        except RuntimeError:
            continue
