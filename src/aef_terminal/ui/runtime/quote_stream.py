from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from aef_terminal.alerts.runtime_registry import price_alert_runtime
from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
    require_exact_instrument_id_sequence,
    route_fingerprint,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import domain_frozen_value, domain_wire_value
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric
from aef_terminal.ui.quote_helpers import quote_envelope, quote_envelope_next_transition
from aef_terminal.ui.reference_watchlist_item import materialize_watchlist_instruments
from aef_terminal.ui.runtime.constants import QUOTE_STREAM_SECONDS


_QUOTE_CACHE_CONTINUOUS_AGE_FIELDS = frozenset(
    {
        "age_seconds",
        "last_age_seconds",
        "bid_ask_age_seconds",
    }
)


def _quote_cache_semantic_payload(quote: Mapping[str, Any]) -> dict[str, Any]:
    """Return facts and categorical state that define one cache revision."""

    return {
        key: value for key, value in quote.items() if key not in _QUOTE_CACHE_CONTINUOUS_AGE_FIELDS
    }


@dataclass(frozen=True, slots=True)
class QuoteRouteEntry:
    instrument_id: str
    provider: str
    provider_symbol: str
    route_fingerprint: str
    instrument: Mapping[str, Any]

    def wire_instrument(self) -> dict[str, Any]:
        """Make an isolated transport projection only at a mutable boundary."""

        payload = domain_wire_value(
            self.instrument,
            field_name=f"quote_route_snapshot.{self.instrument_id}",
        )
        if not isinstance(payload, dict):
            raise TypeError("quote route snapshot transport payload must be an object")
        return payload


@dataclass(frozen=True, slots=True)
class QuoteRouteSnapshot:
    generation: int
    watchlist_version: int
    entries: Mapping[str, QuoteRouteEntry]
    route_identities: frozenset[tuple[str, str]]
    signature: tuple[tuple[str, str, str, str], ...]
    refreshed_at: float

    def select_instruments(
        self,
        instrument_ids: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        """Project exact identities from this captured immutable version."""

        if self.generation <= 0:
            raise RuntimeError("QUOTE_ROUTE_SNAPSHOT_REQUIRED")
        requested = (
            tuple(self.entries)
            if instrument_ids is None
            else require_exact_instrument_id_sequence(instrument_ids, allow_empty=True)
        )
        return [
            entry.wire_instrument()
            for instrument_id in requested
            if (entry := self.entries.get(instrument_id)) is not None
        ]


@dataclass(frozen=True, slots=True)
class QuoteCacheRevision:
    epoch: str
    generation: int


class QuoteStreamRuntime:
    def __init__(
        self,
        *,
        quote_stream_seconds: float = QUOTE_STREAM_SECONDS,
        max_cache_entries: int = 512,
    ) -> None:
        self._quote_stream_seconds = quote_stream_seconds
        self._max_cache_entries = max(int(max_cache_entries or 0), 1)
        self._clients = 0
        self._clients_lock = threading.Lock()
        self._wanted_lock = threading.Lock()
        self._stream_wanted_by_client: dict[int, set[str]] = {}
        self._server_alert_wanted_instrument_ids: set[str] = set()
        self._transient_wanted_by_key: dict[str, tuple[set[str], float]] = {}
        self._cache_lock = threading.Lock()
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._cache_route_updated_at: dict[tuple[str, str], float] = {}
        self._cache_freshness_deadlines: dict[tuple[str, str], float] = {}
        self._cache_next_freshness_at = float("inf")
        self._cache_freshness_checked_at = 0.0
        self._cache_warning = ""
        self._cache_updated_at = 0.0
        self._cache_attempted_at = 0.0
        self._cache_epoch = str(uuid4())
        self._cache_generation = 0
        self._route_refresh_lock = threading.Lock()
        self._route_snapshot = QuoteRouteSnapshot(
            generation=0,
            watchlist_version=-1,
            entries=MappingProxyType({}),
            route_identities=frozenset(),
            signature=(),
            refreshed_at=0.0,
        )
        self._server_sleeping: Callable[[], bool] | None = None

    def configure_server_sleeping(
        self,
        *,
        server_sleeping: Callable[[], bool],
    ) -> None:
        self._server_sleeping = server_sleeping

    def refresh_routes_from_store(
        self,
        store: Any,
        *,
        expected_watchlist_version: int | None = None,
        route_generation_changed: bool = False,
    ) -> QuoteRouteSnapshot:
        """Refresh the immutable exact-route read model from PostgreSQL.

        Callers run this synchronous method outside an asyncio event-loop
        thread. The refresh lock serializes the database read and publication,
        so an older route snapshot cannot publish after a newer committed route.
        """

        started = time.perf_counter()
        with self._route_refresh_lock:
            current = self._route_snapshot
            if expected_watchlist_version is not None:
                if (
                    not isinstance(expected_watchlist_version, int)
                    or isinstance(expected_watchlist_version, bool)
                    or expected_watchlist_version < 0
                ):
                    raise ValueError("quote route watchlist version must be a non-negative integer")
                if (
                    expected_watchlist_version < current.watchlist_version
                    and not route_generation_changed
                ):
                    increment_metric(
                        "quote_route_snapshot_refresh_total",
                        status="stale",
                    )
                    return current
                if (
                    expected_watchlist_version == current.watchlist_version
                    and not route_generation_changed
                ):
                    increment_metric(
                        "quote_route_snapshot_refresh_total",
                        status="unchanged_version",
                    )
                    return current
            try:
                instruments, watchlist_version = store.read_watchlist_snapshot()
                snapshot = self._replace_route_snapshot(
                    instruments,
                    watchlist_version=watchlist_version,
                )
            except Exception:
                increment_metric(
                    "quote_route_snapshot_refresh_total",
                    status="error",
                )
                raise
        observe_metric(
            "quote_route_snapshot_refresh_seconds",
            max(time.perf_counter() - started, 0.0),
        )
        return snapshot

    def _replace_route_snapshot(
        self,
        instruments: Sequence[dict[str, Any]],
        *,
        watchlist_version: int,
    ) -> QuoteRouteSnapshot:
        if not isinstance(instruments, list):
            raise TypeError("quote route snapshot instruments must be a list")
        if (
            not isinstance(watchlist_version, int)
            or isinstance(watchlist_version, bool)
            or watchlist_version < 0
        ):
            raise ValueError("quote route watchlist version must be a non-negative integer")
        if any(not isinstance(instrument, dict) for instrument in instruments):
            raise TypeError("quote route snapshot rows must be typed objects")
        current = self._route_snapshot
        if watchlist_version < current.watchlist_version:
            increment_metric("quote_route_snapshot_refresh_total", status="stale")
            return current

        materialized_instruments = materialize_watchlist_instruments(instruments)
        entries: dict[str, QuoteRouteEntry] = {}
        for materialized in materialized_instruments:
            route = route_instrument(materialized)
            if route.instrument_id in entries:
                raise ValueError(
                    f"QUOTE_ROUTE_SNAPSHOT_DUPLICATE_INSTRUMENT instrument_id={route.instrument_id}"
                )
            frozen_instrument = domain_frozen_value(
                materialized,
                field_name=f"quote_route_snapshot.{route.instrument_id}",
            )
            if not isinstance(frozen_instrument, Mapping):
                raise TypeError("quote route snapshot instrument must freeze to a mapping")
            entry = QuoteRouteEntry(
                instrument_id=route.instrument_id,
                provider=route.provider,
                provider_symbol=route.provider_symbol,
                route_fingerprint=route.fingerprint,
                instrument=frozen_instrument,
            )
            previous = current.entries.get(route.instrument_id)
            entries[route.instrument_id] = previous if previous == entry else entry
        signature = tuple(
            sorted(
                (
                    entry.instrument_id,
                    entry.provider,
                    entry.provider_symbol,
                    entry.route_fingerprint,
                )
                for entry in entries.values()
            )
        )
        payloads_unchanged = len(entries) == len(current.entries) and all(
            instrument_id in current.entries
            and entry.instrument == current.entries[instrument_id].instrument
            for instrument_id, entry in entries.items()
        )
        if (
            signature == current.signature
            and watchlist_version == current.watchlist_version
            and payloads_unchanged
        ):
            increment_metric(
                "quote_route_snapshot_refresh_total",
                status="unchanged",
            )
            return current

        snapshot = QuoteRouteSnapshot(
            generation=current.generation + 1,
            watchlist_version=watchlist_version,
            entries=MappingProxyType(entries),
            route_identities=frozenset(
                (entry.instrument_id, entry.route_fingerprint) for entry in entries.values()
            ),
            signature=signature,
            refreshed_at=time.monotonic(),
        )
        allowed_ids = frozenset(entries)
        with self._wanted_lock:
            self._stream_wanted_by_client = {
                client_key: instrument_ids.intersection(allowed_ids)
                for client_key, instrument_ids in self._stream_wanted_by_client.items()
                if instrument_ids.intersection(allowed_ids)
            }
            self._server_alert_wanted_instrument_ids.intersection_update(allowed_ids)
            self._transient_wanted_by_key = {
                key: (
                    instrument_ids.intersection(allowed_ids),
                    expires_at,
                )
                for key, (instrument_ids, expires_at) in self._transient_wanted_by_key.items()
                if instrument_ids.intersection(allowed_ids)
            }
            with self._cache_lock:
                stale_identities = set(self._cache).difference(snapshot.route_identities)
                for identity in stale_identities:
                    self._cache.pop(identity, None)
                    self._cache_route_updated_at.pop(identity, None)
                    self._cache_freshness_deadlines.pop(identity, None)
                if stale_identities:
                    self._cache_generation += 1
                    self._cache_next_freshness_at = min(
                        self._cache_freshness_deadlines.values(), default=float("inf")
                    )
                cache_generation = self._cache_generation
                # Publish only after dependent demand/cache state matches this
                # route generation. Readers that coordinate through either
                # lock can therefore never observe a mixed generation.
                self._route_snapshot = snapshot
        increment_metric("quote_route_snapshot_refresh_total", status="changed")
        if stale_identities:
            increment_metric(
                "quote_route_cache_evictions_total",
                len(stale_identities),
                reason="route_generation",
            )
            set_metric("quote_cache_generation", cache_generation)
        set_metric("quote_route_snapshot_generation", snapshot.generation)
        set_metric(
            "quote_route_snapshot_watchlist_version",
            snapshot.watchlist_version,
        )
        set_metric("quote_route_snapshot_entries", len(snapshot.entries))
        return snapshot

    def route_snapshot(self) -> QuoteRouteSnapshot:
        return self._route_snapshot

    def select_instruments(
        self,
        instrument_ids: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        """Resolve exact watchlist identities from one immutable route version."""

        return self._route_snapshot.select_instruments(instrument_ids)

    def stream_started(self) -> int:
        with self._clients_lock:
            self._clients += 1
            return self._clients

    def stream_finished(self) -> int:
        with self._clients_lock:
            self._clients = max(0, self._clients - 1)
            return self._clients

    def client_count(self) -> int:
        with self._clients_lock:
            return self._clients

    def exact_wanted_instrument_ids(self, instrument_ids) -> set[str]:
        values = [] if instrument_ids is None else instrument_ids
        return set(require_exact_instrument_id_sequence(values, allow_empty=True))

    def cache_routes_for_instruments(self, instruments) -> dict[tuple[str, str], str]:
        """Return exact cache identities mapped to their public quote-map keys."""

        wanted: dict[tuple[str, str], str] = {}
        for instrument in instruments or []:
            if not isinstance(instrument, dict):
                raise ValueError("Quote cache reads require provider-qualified instrument payloads")
            route = route_instrument(instrument)
            fingerprint = route_fingerprint(instrument)
            wanted[(route.instrument_id, fingerprint)] = fingerprint
        return wanted

    def _allowed_provider_instruments(
        self,
        snapshot: QuoteRouteSnapshot,
        instrument_ids,
        provider: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        provider_key = str(provider or "").strip().lower()
        wanted: dict[tuple[str, str], dict[str, Any]] = {}
        values = [] if instrument_ids is None else instrument_ids
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise TypeError("quote stream instrument_ids must be a typed collection")
        for instrument_id in values:
            if not isinstance(instrument_id, str) or not instrument_id:
                raise ValueError("quote stream instrument_ids must be exact non-empty strings")
            entry = snapshot.entries.get(instrument_id)
            if entry is None:
                raise ValueError(
                    f"QUOTE_ROUTE_SNAPSHOT_INSTRUMENT_REQUIRED instrument_id={instrument_id}"
                )
            if entry.provider != provider_key:
                continue
            payload = entry.wire_instrument()
            payload["provider_symbol"] = entry.provider_symbol
            wanted[(entry.instrument_id, entry.route_fingerprint)] = payload
        return wanted

    def set_stream_wanted(self, client_key: int, instrument_ids) -> None:
        wanted = self.exact_wanted_instrument_ids(instrument_ids)
        with self._wanted_lock:
            if wanted:
                self._stream_wanted_by_client[int(client_key)] = wanted
            else:
                self._stream_wanted_by_client.pop(int(client_key), None)

    def set_server_alert_wanted(self, instrument_ids) -> None:
        wanted = self.exact_wanted_instrument_ids(instrument_ids)
        with self._wanted_lock:
            self._server_alert_wanted_instrument_ids = wanted

    def set_transient_wanted(self, key: str, instrument_ids, ttl_seconds: float = 10.0) -> None:
        wanted = self.exact_wanted_instrument_ids(instrument_ids)
        expires_at = time.monotonic() + max(float(ttl_seconds or 0.0), 0.1)
        with self._wanted_lock:
            if wanted:
                self._transient_wanted_by_key[str(key or "transient")] = (wanted, expires_at)
            else:
                self._transient_wanted_by_key.pop(str(key or "transient"), None)

    def quote_wanted_allowed(self, instrument_id: str) -> bool:
        return self.provider_quote_wanted_allowed("ibkr", instrument_id)

    def provider_quote_wanted_allowed(self, provider: str, instrument_id: str) -> bool:
        provider_key = str(provider or "").strip().lower()
        if not provider_key or not isinstance(instrument_id, str) or not instrument_id:
            return False
        snapshot = self._route_snapshot
        if snapshot.generation <= 0:
            raise RuntimeError("QUOTE_ROUTE_SNAPSHOT_REQUIRED")
        entry = snapshot.entries.get(instrument_id)
        if entry is None:
            raise ValueError(
                f"QUOTE_ROUTE_SNAPSHOT_INSTRUMENT_REQUIRED instrument_id={instrument_id}"
            )
        return entry.provider == provider_key

    def clear_all_wanted(self) -> None:
        with self._wanted_lock:
            self._stream_wanted_by_client.clear()
            self._server_alert_wanted_instrument_ids.clear()
            self._transient_wanted_by_key.clear()

    def _expire_transient_wanted(self, now: float) -> None:
        expired = [
            key
            for key, (_instrument_ids, expires_at) in self._transient_wanted_by_key.items()
            if expires_at <= now
        ]
        for key in expired:
            self._transient_wanted_by_key.pop(key, None)

    def combined_quote_instruments(self, extra_instrument_ids=None) -> list[dict[str, Any]]:
        return self.combined_provider_quote_instruments("ibkr", extra_instrument_ids)

    def combined_provider_quote_instruments(
        self, provider: str, extra_instrument_ids=None
    ) -> list[dict[str, Any]]:
        provider_key = str(provider or "").strip().lower()
        server_sleeping = self._server_sleeping
        if server_sleeping is None:
            raise RuntimeError("QuoteStreamRuntime server-sleep callback is not configured")
        now = time.monotonic()
        with self._wanted_lock:
            snapshot = self._route_snapshot
            if snapshot.generation <= 0:
                raise RuntimeError("QUOTE_ROUTE_SNAPSHOT_REQUIRED")
            self._expire_transient_wanted(now)
            wanted_ids = set(snapshot.entries)
            wanted_ids.update(
                require_exact_instrument_id_sequence(
                    [] if extra_instrument_ids is None else extra_instrument_ids,
                    allow_empty=True,
                )
            )
            for instrument_ids in self._stream_wanted_by_client.values():
                wanted_ids.update(instrument_ids)
            wanted_ids.update(self._server_alert_wanted_instrument_ids)
            for instrument_ids, _expires_at in self._transient_wanted_by_key.values():
                wanted_ids.update(instrument_ids)
        if server_sleeping():
            return []
        wanted = self._allowed_provider_instruments(
            snapshot,
            wanted_ids,
            provider_key,
        )
        return [wanted[key] for key in sorted(wanted)]

    def store_cache(
        self,
        live_map: dict[tuple[str, str], dict[str, Any]] | None,
        warning: str = "",
    ) -> None:
        """Publish changed provider rows, or None for a successful unchanged read.

        Freshness deadlines belong to this cache owner, not the provider cursor.
        Empty/failed attempts retain last observations without renewing success.
        """

        wall_now = datetime.now(tz=UTC)
        wall_seconds = wall_now.timestamp()
        freshness_limit = max(self._quote_stream_seconds * 10.0, 5.0)
        normalized: dict[tuple[str, str], dict[str, Any]] = {}
        for identity, quote in (live_map or {}).items():
            if not isinstance(identity, tuple) or len(identity) != 2:
                raise ValueError(
                    "quote cache key must be an (instrument_id, route_fingerprint) pair"
                )
            exact_identity = (
                require_exact_identity_text(identity[0], field="instrument_id"),
                require_exact_identity_text(identity[1], field="route_fingerprint"),
            )
            if not isinstance(quote, dict):
                continue
            if not quote:
                continue
            normalized[exact_identity] = quote_envelope(
                dict(quote),
                received_at=wall_now,
                stale_after_seconds=freshness_limit,
            )
        now = time.monotonic()
        alert_quote_changes: list[tuple[tuple[str, str], dict[str, Any]]] = []
        with self._cache_lock:
            snapshot = self._route_snapshot
            if snapshot.generation <= 0:
                raise RuntimeError("QUOTE_ROUTE_SNAPSHOT_REQUIRED")
            stale_publications = set(normalized).difference(snapshot.route_identities)
            for identity in stale_publications:
                normalized.pop(identity, None)
            incoming_identities = set(normalized)
            clock_regressed = wall_seconds < self._cache_freshness_checked_at
            if clock_regressed or wall_seconds >= self._cache_next_freshness_at:
                for identity, current_quote in self._cache.items():
                    if identity not in normalized and (
                        clock_regressed
                        or self._cache_freshness_deadlines.get(identity, float("inf"))
                        <= wall_seconds
                    ):
                        normalized[identity] = quote_envelope(
                            current_quote,
                            received_at=wall_now,
                            stale_after_seconds=freshness_limit,
                        )
            self._cache_freshness_checked_at = wall_seconds
            self._cache_attempted_at = now
            next_warning = str(warning or "")
            cache_changed = self._cache_warning != next_warning
            if normalized:
                for identity, quote in normalized.items():
                    current_quote = self._cache.get(identity)
                    if current_quote is None or _quote_cache_semantic_payload(
                        current_quote
                    ) != _quote_cache_semantic_payload(quote):
                        cache_changed = True
                        alert_quote_changes.append((identity, quote))
                    self._cache[identity] = quote
                    if identity in incoming_identities:
                        self._cache_route_updated_at[identity] = now
                    deadline = quote_envelope_next_transition(
                        quote, received_at=wall_now, stale_after_seconds=freshness_limit
                    )
                    if deadline == float("inf"):
                        self._cache_freshness_deadlines.pop(identity, None)
                    else:
                        self._cache_freshness_deadlines[identity] = deadline
                if len(self._cache) > self._max_cache_entries:
                    overflow = len(self._cache) - self._max_cache_entries
                    oldest = sorted(
                        self._cache_route_updated_at.items(),
                        key=lambda item: item[1],
                    )[:overflow]
                    for identity, _updated_at in oldest:
                        self._cache.pop(identity, None)
                        self._cache_route_updated_at.pop(identity, None)
                        self._cache_freshness_deadlines.pop(identity, None)
                    cache_changed = True
                self._cache_next_freshness_at = min(
                    self._cache_freshness_deadlines.values(), default=float("inf")
                )
            if self._cache and (incoming_identities or live_map is None):
                self._cache_updated_at = now
            self._cache_warning = next_warning
            if cache_changed:
                self._cache_generation += 1
            cache_generation = self._cache_generation
        if stale_publications:
            increment_metric(
                "quote_route_cache_publication_drops_total",
                len(stale_publications),
                reason="route_generation",
            )
        if cache_changed:
            set_metric("quote_cache_generation", cache_generation)
        if alert_quote_changes:
            price_alert_runtime.publish_quote_changes(
                sorted(alert_quote_changes, key=lambda item: item[0])
            )

    def cache_for_instruments(
        self,
        instruments,
        *,
        after_revision: QuoteCacheRevision | None = None,
    ) -> tuple[
        dict[str, dict[str, Any]] | None,
        str,
        QuoteCacheRevision,
    ]:
        if after_revision is not None and not isinstance(
            after_revision,
            QuoteCacheRevision,
        ):
            raise TypeError("quote cache after_revision must be a QuoteCacheRevision")
        now = time.monotonic()
        if after_revision is not None:
            with self._cache_lock:
                revision = QuoteCacheRevision(
                    epoch=self._cache_epoch,
                    generation=self._cache_generation,
                )
                warning = self._cache_warning
                if after_revision == revision:
                    return None, warning, revision
        wanted = self.cache_routes_for_instruments(instruments)
        with self._cache_lock:
            warning = self._cache_warning
            revision = QuoteCacheRevision(
                epoch=self._cache_epoch,
                generation=self._cache_generation,
            )
            age = now - self._cache_updated_at if self._cache_updated_at else None
            attempted_age = now - self._cache_attempted_at if self._cache_attempted_at else None
            result = {
                fingerprint: quote_envelope(
                    dict(self._cache.get(identity) or {}),
                    stale_after_seconds=max(self._quote_stream_seconds * 10.0, 5.0),
                )
                for identity, fingerprint in wanted.items()
            }
        if age is None:
            if attempted_age is None:
                warning = warning or "Quote orchestrator has not published a snapshot yet."
            else:
                warning = warning or "Quote snapshot has no successful refresh yet."
        elif age > max(self._quote_stream_seconds * 10, 2.0):
            warning = warning or "Quote snapshot is stale."
        return result, warning, revision

    def cache_stats(self) -> dict[str, Any]:
        now = time.monotonic()
        clients = self.client_count()
        with self._cache_lock:
            snapshot = self._route_snapshot
            stats = {
                "clients": clients,
                "cache_entries": len(self._cache),
                "cache_max_entries": self._max_cache_entries,
                "cache_age_seconds": round(now - self._cache_updated_at, 3)
                if self._cache_updated_at
                else None,
                "cache_attempted_age_seconds": round(now - self._cache_attempted_at, 3)
                if self._cache_attempted_at
                else None,
                "cache_epoch": self._cache_epoch,
                "cache_generation": self._cache_generation,
                "warning": self._cache_warning,
            }
        stats.update(
            {
                "route_generation": snapshot.generation,
                "route_watchlist_version": (
                    snapshot.watchlist_version if snapshot.generation > 0 else None
                ),
                "route_entries": len(snapshot.entries),
                "route_age_seconds": (
                    round(now - snapshot.refreshed_at, 3) if snapshot.refreshed_at else None
                ),
            }
        )
        return stats


quote_stream = QuoteStreamRuntime()
