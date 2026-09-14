from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock
from typing import Any, Iterable, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aef_terminal.data.instrument_identity import instrument_key, require_exact_identity_text
from aef_terminal.data.provider_contract import ProviderBarBucket
from aef_terminal.data.providers import get_provider, provider_key_for_source, route_instrument
from aef_terminal.runtime.timeframes import (
    interval_minutes,
    parse_aware_utc_ts,
    require_aware_utc_datetime,
)

_TRADING_HOURS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}
_TRADING_HOURS_CACHE_EPOCH: dict[tuple[str, str], int] = {}
_TRADING_HOURS_LOCK = RLock()
_TRADING_HOURS_CACHE_TTL_SECONDS = 300.0
_TRADING_HOURS_STATE_MAX_COMPLETED_ROUTES = 2048
_TRADING_HOURS_CACHE_SEQUENCE = 0
_PENDING_TRADING_HOURS_REFRESH: dict[str, dict[tuple[str, str], int]] = {}
_TRADING_HOURS_REFRESH_EPOCH: dict[tuple[str, str, str], int] = {}
_TRADING_HOURS_REFRESH_SEQUENCE = 0
_LOGGER = logging.getLogger(__name__)
CONTINUOUS_BAR_SLOT_ANCHOR = datetime(1970, 1, 1, tzinfo=UTC)
_SESSION_COVERAGE_INTERVAL_LIMIT = 2048
_UNRESOLVED_SESSION_INTERVAL = object()


@dataclass(frozen=True)
class ProviderSessionBarSlotCoverage:
    schedule_state: Literal["continuous", "verified", "unknown"]
    expected_slots: tuple[datetime, ...]
    truncated: bool
    pending_cursor: datetime | None
    verified_until: datetime | None = None
    schedule_revision: int = 0


@dataclass(frozen=True)
class ProviderSessionFutureSlotCoverage:
    """Exact provider-session slots strictly after one canonical chart slot."""

    schedule_state: Literal["continuous", "verified", "unknown"]
    expected_slots: tuple[datetime, ...]
    complete: bool
    pending_cursor: datetime | None
    schedule_revision: int = 0


def _provider_schedule_revision(*values: Any) -> int:
    latest: datetime | None = None
    for value in values:
        timestamp = parse_aware_utc_ts(value)
        if timestamp is None:
            continue
        if latest is None or timestamp > latest:
            latest = timestamp
    if latest is None:
        return 0
    epoch = latest.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return epoch.days * 86_400_000_000 + epoch.seconds * 1_000_000 + epoch.microseconds


class ProviderBarSlotMap(dict[datetime, int]):
    """Timestamp-to-logical-slot mapping with explicit calendar authority.

    Scheduled axes are authoritative only when exact provider coverage spans the
    requested timeline. Unknown axes may still carry compressed visual coordinates,
    but consumers must not use them for signals or alert geometry.
    """

    schedule_state: Literal["continuous", "verified", "unknown"]
    authoritative: bool

    def __init__(
        self,
        values: Mapping[datetime, int] | Iterable[tuple[datetime, int]] = (),
        *,
        schedule_state: Literal["continuous", "verified", "unknown"],
    ) -> None:
        super().__init__(values)
        self.schedule_state = schedule_state
        self.authoritative = schedule_state in {"continuous", "verified"}


def _provider_key(provider: str | None) -> str:
    raw = str(provider or "").strip().lower()
    if not raw or raw == "unknown":
        return "unknown"
    key = provider_key_for_source(raw)
    return get_provider(key).key


def _prune_provider_trading_hours_state_locked(now: float) -> None:
    expired_cache_keys = [
        route_key
        for route_key, (cached_at, _row) in _TRADING_HOURS_CACHE.items()
        if now - cached_at > _TRADING_HOURS_CACHE_TTL_SECONDS
    ]
    for route_key in expired_cache_keys:
        _TRADING_HOURS_CACHE.pop(route_key, None)
    while len(_TRADING_HOURS_CACHE) > _TRADING_HOURS_STATE_MAX_COMPLETED_ROUTES:
        oldest_route_key = next(iter(_TRADING_HOURS_CACHE))
        _TRADING_HOURS_CACHE.pop(oldest_route_key, None)

    empty_providers = [
        provider for provider, pending in _PENDING_TRADING_HOURS_REFRESH.items() if not pending
    ]
    for provider in empty_providers:
        _PENDING_TRADING_HOURS_REFRESH.pop(provider, None)
    pending_route_keys = {
        route_key for pending in _PENDING_TRADING_HOURS_REFRESH.values() for route_key in pending
    }
    retained_cache_epochs = set(_TRADING_HOURS_CACHE).union(pending_route_keys)
    for route_key in tuple(_TRADING_HOURS_CACHE_EPOCH):
        if route_key not in retained_cache_epochs:
            _TRADING_HOURS_CACHE_EPOCH.pop(route_key, None)

    completed_refresh_keys = [
        refresh_key
        for refresh_key in _TRADING_HOURS_REFRESH_EPOCH
        if refresh_key[1:] not in pending_route_keys
    ]
    excess_completed = len(completed_refresh_keys) - _TRADING_HOURS_STATE_MAX_COMPLETED_ROUTES
    for refresh_key in completed_refresh_keys[: max(excess_completed, 0)]:
        _TRADING_HOURS_REFRESH_EPOCH.pop(refresh_key, None)


def _resolve_session_instrument(
    *,
    provider: str | None,
    instrument: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    hinted_provider = _provider_key(provider)
    route = route_instrument(
        instrument,
        expected_source=None if hinted_provider == "unknown" else hinted_provider,
    )
    return route.instrument, route.adapter


def provider_history_session_scope(
    adapter: Any, instrument: dict[str, Any]
) -> Literal["liquid", "trading"]:
    scope = adapter.history_session_scope(instrument)
    if scope not in {"liquid", "trading"}:
        raise RuntimeError(
            f"PROVIDER_HISTORY_SESSION_SCOPE_INVALID provider={adapter.key} scope={scope!r}"
        )
    return scope


def invalidate_provider_trading_hours_cache(*, instrument: dict[str, Any]) -> None:
    global _TRADING_HOURS_CACHE_SEQUENCE
    route = route_instrument(instrument)
    cache_key = (route.instrument_id, route.fingerprint)
    with _TRADING_HOURS_LOCK:
        _TRADING_HOURS_CACHE.pop(cache_key, None)
        _TRADING_HOURS_CACHE_SEQUENCE += 1
        _TRADING_HOURS_CACHE_EPOCH[cache_key] = _TRADING_HOURS_CACHE_EPOCH.get(cache_key, 0) + 1
        _prune_provider_trading_hours_state_locked(time.monotonic())


def mark_provider_trading_hours_stale(
    *,
    instrument: dict[str, Any],
    reason: str = "",
    store: Any | None = None,
) -> None:
    global _TRADING_HOURS_CACHE_SEQUENCE, _TRADING_HOURS_REFRESH_SEQUENCE
    route = route_instrument(instrument)
    route_key = (route.instrument_id, route.fingerprint)
    refresh_key = (route.provider, *route_key)
    cache_key = route_key
    with _TRADING_HOURS_LOCK:
        _TRADING_HOURS_REFRESH_SEQUENCE = (
            max(
                _TRADING_HOURS_REFRESH_SEQUENCE,
                max(_TRADING_HOURS_REFRESH_EPOCH.values(), default=0),
            )
            + 1
        )
        refresh_generation = _TRADING_HOURS_REFRESH_SEQUENCE
        _TRADING_HOURS_REFRESH_EPOCH.pop(refresh_key, None)
        _TRADING_HOURS_REFRESH_EPOCH[refresh_key] = refresh_generation
        pending = _PENDING_TRADING_HOURS_REFRESH.setdefault(route.provider, {})
        pending[route_key] = refresh_generation
        _TRADING_HOURS_CACHE.pop(cache_key, None)
        _TRADING_HOURS_CACHE_SEQUENCE += 1
        _TRADING_HOURS_CACHE_EPOCH[cache_key] = _TRADING_HOURS_CACHE_EPOCH.get(cache_key, 0) + 1
        _prune_provider_trading_hours_state_locked(time.monotonic())
    if store is not None:
        try:
            store.delete_trading_hours(instrument=route.instrument)
        except Exception:
            _LOGGER.exception(
                "trading_hours_stale_storage_failed provider=%s instrument_id=%s route_fingerprint=%s reason=%s",
                route.provider,
                route.instrument_id,
                route.fingerprint,
                reason,
            )


def peek_pending_provider_trading_hours_refresh(
    provider: str,
) -> tuple[tuple[str, str], ...]:
    with _TRADING_HOURS_LOCK:
        _prune_provider_trading_hours_state_locked(time.monotonic())
        return tuple(
            sorted(
                _PENDING_TRADING_HOURS_REFRESH.get(
                    _provider_key(provider),
                    {},
                )
            )
        )


def pending_provider_trading_hours_refresh_count(provider: str | None = None) -> int:
    with _TRADING_HOURS_LOCK:
        _prune_provider_trading_hours_state_locked(time.monotonic())
        if provider:
            return len(
                _PENDING_TRADING_HOURS_REFRESH.get(
                    _provider_key(provider),
                    {},
                )
            )
        return sum(len(pending) for pending in _PENDING_TRADING_HOURS_REFRESH.values())


def provider_trading_hours_refresh_generation(
    provider: str,
    instrument_id: str,
    route_fingerprint: str,
) -> int:
    provider_key = _provider_key(provider)
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    exact_route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    with _TRADING_HOURS_LOCK:
        _prune_provider_trading_hours_state_locked(time.monotonic())
        return _TRADING_HOURS_REFRESH_EPOCH.get(
            (
                provider_key,
                exact_instrument_id,
                exact_route_fingerprint,
            ),
            0,
        )


def acknowledge_provider_trading_hours_refresh(
    provider: str,
    instrument_id: str,
    route_fingerprint: str,
    *,
    expected_generation: int,
) -> bool:
    if (
        isinstance(expected_generation, bool)
        or not isinstance(expected_generation, int)
        or expected_generation < 0
    ):
        raise ValueError("expected_generation must be a non-negative integer")
    exact_route_key = (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        ),
    )
    with _TRADING_HOURS_LOCK:
        pending = _PENDING_TRADING_HOURS_REFRESH.setdefault(
            _provider_key(provider),
            {},
        )
        if pending.get(exact_route_key) != expected_generation:
            return False
        del pending[exact_route_key]
        _prune_provider_trading_hours_state_locked(time.monotonic())
        return True


def _cached_provider_trading_hours(
    store: Any | None = None,
    *,
    instrument: dict[str, Any],
) -> dict[str, Any] | None:
    if store is None:
        raise RuntimeError("trading-hours store is required; pass store=store")
    instrument, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    contract_id = adapter.session_contract_id(instrument)
    if not contract_id:
        return None
    now = time.monotonic()
    route = route_instrument(instrument, expected_source=adapter.key)
    cache_key = (route.instrument_id, route.fingerprint)
    with _TRADING_HOURS_LOCK:
        _prune_provider_trading_hours_state_locked(now)
        if cache_key in _PENDING_TRADING_HOURS_REFRESH.get(
            route.provider,
            {},
        ):
            return None
        cached = _TRADING_HOURS_CACHE.get(cache_key)
        if cached and now - cached[0] <= _TRADING_HOURS_CACHE_TTL_SECONDS:
            return cached[1]
        read_epoch = _TRADING_HOURS_CACHE_EPOCH.get(cache_key, 0)
        read_sequence = _TRADING_HOURS_CACHE_SEQUENCE
    row = store.read_trading_hours(instrument=instrument)
    with _TRADING_HOURS_LOCK:
        if (
            cache_key in _PENDING_TRADING_HOURS_REFRESH.get(route.provider, {})
            or _TRADING_HOURS_CACHE_SEQUENCE != read_sequence
            or _TRADING_HOURS_CACHE_EPOCH.get(cache_key, 0) != read_epoch
        ):
            return None
        _TRADING_HOURS_CACHE.pop(cache_key, None)
        _TRADING_HOURS_CACHE[cache_key] = (now, row)
        _prune_provider_trading_hours_state_locked(now)
    return row


def _provider_contract_id_from_trading_hours(row: dict[str, Any] | None) -> str:
    payload = (
        row.get("payload") if isinstance(row, dict) and isinstance(row.get("payload"), dict) else {}
    )
    value = payload.get("provider_contract_id")
    return value if isinstance(value, str) else ""


def provider_schedule_open_state(
    ts: datetime,
    liquid: bool | None = None,
    store: Any | None = None,
    *,
    instrument: dict[str, Any],
) -> bool | None:
    resolved, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    if adapter.continuous_session(resolved):
        return True
    hours = _cached_provider_trading_hours(store=store, instrument=resolved)
    provider_contract_id = _provider_contract_id_from_trading_hours(hours)
    if not provider_contract_id:
        return None
    liquid_scope = (
        provider_history_session_scope(adapter, resolved) == "liquid"
        if liquid is None
        else bool(liquid)
    )
    session_type = "liquid" if liquid_scope else "trading"
    state = store.read_trading_session_open_state(ts, session_type, instrument=resolved)
    if state is not None:
        return bool(state)
    return None


def provider_schedule_open_interval(
    ts: datetime,
    liquid: bool | None = None,
    store: Any | None = None,
    *,
    instrument: dict[str, Any],
) -> dict[str, Any] | None:
    resolved, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    if adapter.continuous_session(resolved) or store is None:
        return None
    hours = _cached_provider_trading_hours(store=store, instrument=resolved)
    provider_contract_id = _provider_contract_id_from_trading_hours(hours)
    if not provider_contract_id:
        return None
    timestamp = ts.astimezone(UTC)
    liquid_scope = (
        provider_history_session_scope(adapter, resolved) == "liquid"
        if liquid is None
        else bool(liquid)
    )
    session_type = "liquid" if liquid_scope else "trading"
    rows = store.read_trading_session_intervals(
        instrument=resolved,
        session_type=session_type,
        start_ts=timestamp,
        end_ts=timestamp + timedelta(microseconds=1),
    )
    for row in rows:
        if str(row.get("status") or "").strip().lower() != "open":
            continue
        try:
            opens_raw = row.get("opens_at")
            closes_raw = row.get("closes_at")
            opens_at = (
                opens_raw
                if isinstance(opens_raw, datetime)
                else datetime.fromisoformat(str(opens_raw))
            )
            closes_at = (
                closes_raw
                if isinstance(closes_raw, datetime)
                else datetime.fromisoformat(str(closes_raw))
            )
        except TypeError, ValueError:
            continue
        if opens_at.tzinfo is None or closes_at.tzinfo is None:
            continue
        opens_utc = opens_at.astimezone(UTC)
        closes_utc = closes_at.astimezone(UTC)
        if opens_utc <= timestamp < closes_utc:
            return {
                "opens_at": opens_utc,
                "closes_at": closes_utc,
                "session_type": session_type,
                "provider_contract_id": provider_contract_id,
            }
    return None


def provider_bar_bucket(
    ts: datetime,
    interval: str,
    *,
    store: Any | None = None,
    instrument: dict[str, Any],
    liquid: bool | None = None,
    session_open_interval: dict[str, Any] | None | object = _UNRESOLVED_SESSION_INTERVAL,
) -> ProviderBarBucket | None:
    """Resolve a trade to its provider-qualified forming-bar timestamp.

    The adapter owns the exact timestamp grid and close boundary.  For example,
    IBKR's RTH 60m opening bucket is 09:30-10:00 and subsequent buckets use wall
    hours, while another provider may keep wall-hour-aligned history. Missing or closed
    schedule state fails closed instead of minting a plausible-looking bucket.
    ``session_open_interval`` admits an already cached exact-route interval so a
    realtime consumer does not repeat storage I/O for every trade.
    """

    timestamp = require_aware_utc_datetime(ts, field="provider trade timestamp")
    resolved, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    if adapter.continuous_session(resolved):
        return adapter.provider_bar_bucket(resolved, timestamp, interval)
    liquid_scope = (
        provider_history_session_scope(adapter, resolved) == "liquid"
        if liquid is None
        else bool(liquid)
    )
    if session_open_interval is _UNRESOLVED_SESSION_INTERVAL:
        open_interval = provider_schedule_open_interval(
            timestamp,
            liquid_scope,
            store=store,
            instrument=resolved,
        )
    else:
        open_interval = session_open_interval
    if not isinstance(open_interval, dict):
        return None
    try:
        opens_raw = open_interval.get("opens_at")
        closes_raw = open_interval.get("closes_at")
        opens_at = (
            opens_raw if isinstance(opens_raw, datetime) else datetime.fromisoformat(str(opens_raw))
        )
        closes_at = (
            closes_raw
            if isinstance(closes_raw, datetime)
            else datetime.fromisoformat(str(closes_raw))
        )
    except TypeError, ValueError:
        return None
    if opens_at.tzinfo is None or closes_at.tzinfo is None:
        return None
    opens_utc = opens_at.astimezone(UTC)
    closes_utc = closes_at.astimezone(UTC)
    if not opens_utc <= timestamp < closes_utc:
        return None
    return adapter.provider_bar_bucket(
        resolved,
        timestamp,
        interval,
        session_open=opens_utc,
        session_close=closes_utc,
    )


def next_provider_schedule_open(
    ts: datetime,
    liquid: bool | None = None,
    store: Any | None = None,
    *,
    instrument: dict[str, Any],
) -> datetime | None:
    resolved, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    if adapter.continuous_session(resolved):
        return None
    hours = _cached_provider_trading_hours(store=store, instrument=resolved)
    provider_contract_id = _provider_contract_id_from_trading_hours(hours)
    if not provider_contract_id:
        return None
    liquid_scope = (
        provider_history_session_scope(adapter, resolved) == "liquid"
        if liquid is None
        else bool(liquid)
    )
    session_type = "liquid" if liquid_scope else "trading"
    next_open = store.read_next_trading_session_open(ts, session_type, instrument=resolved)
    if next_open is not None:
        return next_open
    return None


def continuous_provider_bar_slot(
    ts: datetime,
    interval: str,
) -> int:
    """Return the stable minute-coordinate for a 24/7 provider bucket."""
    timestamp = require_aware_utc_datetime(ts, field="continuous provider bar timestamp")
    delta = timestamp - CONTINUOUS_BAR_SLOT_ANCHOR
    elapsed_microseconds = (
        delta.days * 24 * 60 * 60 + delta.seconds
    ) * 1_000_000 + delta.microseconds
    step_minutes = max(interval_minutes(interval), 1)
    step_microseconds = step_minutes * 60 * 1_000_000
    if elapsed_microseconds < 0:
        raise ValueError("provider bar slot timestamp precedes the canonical anchor")
    if elapsed_microseconds % step_microseconds != 0:
        raise ValueError(
            f"provider bar slot timestamp is not aligned to {interval}: {timestamp.isoformat()}"
        )
    return int((elapsed_microseconds // step_microseconds) * step_minutes)


def provider_session_bar_slot_coverage_between(
    starts_at: datetime,
    ends_at: datetime,
    interval: str,
    *,
    session_scope: str | None = None,
    store: Any | None = None,
    instrument: dict[str, Any],
    limit: int = 512,
) -> ProviderSessionBarSlotCoverage:
    """Return bounded expected closed slots plus typed provider-schedule coverage.

    Exact provider snapshot horizons prove their half-open timeline. An empty list of
    expected open slots is authoritative only inside that exact typed coverage.
    """

    slot_limit = max(int(limit), 1)
    step_seconds = max(interval_minutes(interval), 1) * 60
    step = timedelta(seconds=step_seconds)
    start_utc = starts_at.astimezone(UTC)
    end_utc = ends_at.astimezone(UTC)
    first_slot = datetime.fromtimestamp(
        ceil(start_utc.timestamp() / step_seconds) * step_seconds,
        tz=UTC,
    )
    resolved, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    if adapter.continuous_session(resolved):
        total_slots = max(
            int((end_utc - first_slot).total_seconds() // step_seconds),
            0,
        )
        returned_slots = min(total_slots, slot_limit)
        expected_slots = tuple(first_slot + index * step for index in range(returned_slots))
        truncated = total_slots > returned_slots
        return ProviderSessionBarSlotCoverage(
            schedule_state="continuous",
            expected_slots=expected_slots,
            truncated=truncated,
            pending_cursor=(first_slot + returned_slots * step) if truncated else None,
            verified_until=end_utc,
            schedule_revision=0,
        )
    if store is None:
        return ProviderSessionBarSlotCoverage("unknown", (), False, None)
    hours = _cached_provider_trading_hours(store=store, instrument=resolved)
    hours_revision = _provider_schedule_revision(
        (hours or {}).get("fetched_at"),
    )
    provider_contract_id = _provider_contract_id_from_trading_hours(hours)
    timezone_name = str((hours or {}).get("timezone") or "").strip()
    if not provider_contract_id or not timezone_name:
        return ProviderSessionBarSlotCoverage(
            "unknown",
            (),
            False,
            None,
            schedule_revision=hours_revision,
        )
    try:
        session_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError, ValueError:
        return ProviderSessionBarSlotCoverage(
            "unknown",
            (),
            False,
            None,
            schedule_revision=hours_revision,
        )
    if start_utc >= end_utc:
        return ProviderSessionBarSlotCoverage(
            "verified",
            (),
            False,
            None,
            verified_until=end_utc,
            schedule_revision=hours_revision,
        )
    scope = session_scope or provider_history_session_scope(adapter, resolved)
    if scope not in {"liquid", "trading"}:
        raise ValueError(f"PROVIDER_HISTORY_SESSION_SCOPE_INVALID scope={scope!r}")
    first_date = start_utc.astimezone(session_timezone).date()
    last_date = (end_utc - timedelta(microseconds=1)).astimezone(session_timezone).date()
    query_start = datetime.combine(
        first_date, datetime.min.time(), tzinfo=session_timezone
    ).astimezone(UTC)
    query_end = datetime.combine(
        last_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=session_timezone,
    ).astimezone(UTC)
    raw_rows = store.read_trading_session_intervals(
        instrument=resolved,
        session_type=scope,
        start_ts=query_start,
        end_ts=query_end,
        limit=_SESSION_COVERAGE_INTERVAL_LIMIT + 1,
    )
    rows_truncated = len(raw_rows) > _SESSION_COVERAGE_INTERVAL_LIMIT
    rows = raw_rows[:_SESSION_COVERAGE_INTERVAL_LIMIT]
    schedule_revision = max(
        hours_revision,
        _provider_schedule_revision(*(row.get("source_fetched_at") for row in rows)),
    )
    open_intervals: list[tuple[datetime, datetime]] = []
    authority_intervals: list[tuple[datetime, datetime]] = []
    coverage_contract_valid = bool(rows) and not rows_truncated
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status not in {"open", "closed"}:
            coverage_contract_valid = False
            continue
        opens_at = parse_aware_utc_ts(row.get("opens_at"))
        closes_at = parse_aware_utc_ts(row.get("closes_at"))
        if opens_at is None or closes_at is None or closes_at <= opens_at:
            coverage_contract_valid = False
            continue
        opens_utc = opens_at
        closes_utc = closes_at
        source_start = parse_aware_utc_ts(row.get("source_coverage_start"))
        source_end = parse_aware_utc_ts(row.get("source_coverage_end"))
        if (
            source_start is None
            or source_end is None
            or source_end <= source_start
            or opens_utc < source_start
            or closes_utc > source_end
        ):
            coverage_contract_valid = False
            continue
        authority_intervals.append((source_start, source_end))
        if status == "open":
            open_intervals.append((opens_utc, closes_utc))
    verified_until: datetime | None = None
    if coverage_contract_valid:
        covered_until = start_utc
        for authority_start, authority_end in sorted(authority_intervals):
            if authority_end <= covered_until:
                continue
            if authority_start > covered_until:
                break
            covered_until = authority_end
            if covered_until >= end_utc:
                break
        exact_coverage = covered_until >= end_utc
        if covered_until > start_utc:
            verified_until = min(covered_until, end_utc)
        schedule_state: Literal["verified", "unknown"] = "verified" if exact_coverage else "unknown"
    else:
        schedule_state = "unknown"
    collected: set[datetime] = set()
    overflow_slot: datetime | None = None
    for opens_at, closes_at in sorted(open_intervals):
        buckets = adapter.provider_session_bar_buckets(
            resolved,
            interval,
            session_open=opens_at,
            session_close=closes_at,
        )
        for bucket in buckets:
            if not isinstance(bucket, ProviderBarBucket):
                break
            candidate = bucket.starts_at.astimezone(UTC)
            bucket_close = bucket.closes_at.astimezone(UTC)
            if bucket_close <= candidate:
                break
            if candidate >= start_utc and bucket_close <= end_utc:
                collected.add(candidate)
                if len(collected) > slot_limit:
                    break
        if len(collected) > slot_limit:
            break
    ordered_slots = tuple(sorted(collected))
    expected_slots = ordered_slots[:slot_limit]
    expected_truncated = len(ordered_slots) > slot_limit
    if expected_truncated:
        overflow_slot = ordered_slots[slot_limit]
    pending_cursor = overflow_slot
    if rows_truncated and pending_cursor is None and rows:
        try:
            last_close = datetime.fromisoformat(str(rows[-1].get("closes_at") or ""))
            if last_close.tzinfo is not None:
                pending_cursor = min(last_close.astimezone(UTC), end_utc)
        except TypeError, ValueError:
            pending_cursor = None
    return ProviderSessionBarSlotCoverage(
        schedule_state=schedule_state,
        expected_slots=expected_slots,
        truncated=bool(expected_truncated or rows_truncated),
        pending_cursor=pending_cursor,
        verified_until=verified_until,
        schedule_revision=schedule_revision,
    )


def provider_session_future_bar_slot_coverage(
    after: datetime,
    interval: str,
    *,
    count: int,
    through: datetime | None = None,
    store: Any | None = None,
    instrument: dict[str, Any],
) -> ProviderSessionFutureSlotCoverage:
    """Return a bounded exact future-axis prefix from provider session facts.

    One bounded interval read classifies the verified provider-schedule prefix;
    an unknown later schedule never invalidates that already verified prefix.
    """

    if type(count) is not int or count <= 0 or count > _SESSION_COVERAGE_INTERVAL_LIMIT:
        raise ValueError(
            "future provider bar-slot count must be between 1 and "
            f"{_SESSION_COVERAGE_INTERVAL_LIMIT}"
        )
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("future provider bar-slot anchor must be timezone-aware")
    if through is not None and (
        through.tzinfo is None or through.utcoffset() is None or through <= after
    ):
        raise ValueError("future provider bar-slot horizon must be aware and after anchor")
    after_utc = after.astimezone(UTC)
    horizon_end = through.astimezone(UTC) if through is not None else after_utc + timedelta(days=7)
    window_start = after_utc + timedelta(microseconds=1)
    coverage = provider_session_bar_slot_coverage_between(
        window_start,
        horizon_end,
        interval,
        store=store,
        instrument=instrument,
        limit=count + 1,
    )
    verified_until = coverage.verified_until
    has_verified_prefix = verified_until is not None and verified_until > window_start
    expected = tuple(
        sorted(
            {
                slot
                for slot in coverage.expected_slots
                if slot > after_utc and verified_until is not None and slot < verified_until
            }
        )[:count]
    )
    complete = len(expected) >= count
    schedule_state: Literal["continuous", "verified", "unknown"]
    if coverage.schedule_state == "continuous":
        schedule_state = "continuous"
    elif has_verified_prefix:
        schedule_state = "verified"
    else:
        schedule_state = "unknown"
    return ProviderSessionFutureSlotCoverage(
        schedule_state=schedule_state,
        expected_slots=expected,
        complete=complete,
        pending_cursor=(
            None if complete else coverage.pending_cursor or verified_until or window_start
        ),
        schedule_revision=coverage.schedule_revision,
    )


def expected_market_closed_notice(
    latest_ts: datetime,
    now_utc: datetime,
    interval: str,
    *,
    store: Any | None = None,
    instrument: dict[str, Any],
) -> dict[str, Any] | None:
    resolved_instrument, adapter = _resolve_session_instrument(
        provider=None,
        instrument=instrument,
    )
    continuous_session = adapter.continuous_session(resolved_instrument)
    ticker = instrument_key(resolved_instrument)
    if not continuous_session:
        if store is None:
            schedule_open = None
        else:
            schedule_open = provider_schedule_open_state(
                now_utc,
                store=store,
                instrument=resolved_instrument,
            )
        if schedule_open is None:
            return {
                "warning": (
                    "SESSION UNKNOWN: "
                    f"{ticker} has no provider trading-session metadata for execution gating; "
                    f"latest {interval} bar started at {latest_ts.astimezone(UTC).isoformat()}."
                ),
                "reopen_ts": None,
                "session": "session_unknown",
            }
    else:
        schedule_open = True
    if schedule_open is not False:
        return None
    reopen_utc = (
        next_provider_schedule_open(
            now_utc,
            store=store,
            instrument=resolved_instrument,
        )
        if store is not None
        else None
    )
    reopen_text = f", expected reopen around {reopen_utc.isoformat()}" if reopen_utc else ""
    return {
        "warning": (
            "MARKET CLOSED: "
            f"{ticker} is outside trading hours; "
            f"latest {interval} bar started at {latest_ts.astimezone(UTC).isoformat()}"
            f"{reopen_text}."
        ),
        "reopen_ts": reopen_utc,
        "session": "market_closed",
    }
