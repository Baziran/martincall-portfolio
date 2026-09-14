from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from logging import Logger
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from aef_terminal.alerts.runtime_registry import price_alert_runtime
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.provider_contract import InstrumentRoute, ProviderBarBucket
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_sessions import provider_bar_bucket
from aef_terminal.runtime.chart_events import chart_bars_updated_generation
from aef_terminal.runtime.async_tasks import (
    await_cancellation_deferred_task,
    settle_physical_task,
)
from aef_terminal.runtime.metrics import increment_metric
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.option_target_changes import option_targets_revision
from aef_terminal.runtime.timeframes import interval_bucket, interval_seconds
from aef_terminal.ui.price_alert_services import price_alert_runtime_state
from aef_terminal.ui.quote_stream_contract import project_quote_stream_row
from aef_terminal.ui.quote_helpers import quote_snapshot_ts
from aef_terminal.ui.route_selection import (
    RequestedInstrumentRoute,
    RouteSelectionMismatch,
    parse_route_selection,
    resolve_route_selection,
)
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision, QuoteRouteSnapshot
from aef_terminal.ui.services.websocket_send import send_stream_json


@dataclass(frozen=True)
class QuoteStreamWsDeps:
    logger: Logger
    store_factory: Callable[[], Any]
    apply_provider_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    set_quote_stream_wanted: Callable[[int, Any], None]
    quote_stream_started: Callable[[], int]
    quote_stream_finished: Callable[[], int]
    screener_bases: Callable[[list[dict[str, Any]], str], dict[str, dict]]
    screener_rows: Callable[..., list[dict[str, Any]]]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]
    record_quote_execution_snapshots: Callable[[list[dict[str, Any]]], None]
    fast_indicator_snapshots: Callable[
        [list[dict[str, Any]], str],
        list[dict[str, Any]] | None,
    ]
    quote_stream_seconds: float
    quote_stream_cleanup_grace_seconds: float
    websocket_heartbeat_seconds: float
    stream_status_payload: Callable[..., dict[str, Any]]


@dataclass
class _QuoteSnapshotProducer:
    key: tuple[Any, ...]
    deps: QuoteStreamWsDeps
    instruments: list[dict[str, Any]]
    requested_routes: tuple[RequestedInstrumentRoute, ...]
    quote_wanted_keys: list[str]
    interval: str
    consumers: int = 0
    rows_revision: int = 0
    targets_revision: int | None = None
    fast_indicators_revision: int | None = None
    alert_runtime_revision: int | None = None
    live_bars_revision: int = 0
    rows: list[dict[str, Any]] | None = None
    rows_cache_revision: QuoteCacheRevision | None = None
    live_bars: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    live_bar_revisions: dict[tuple[str, str], int] = field(default_factory=dict)
    live_trade_sequences: dict[str, int] = field(default_factory=dict)
    live_trade_overflow_sequence: int = 0
    option_targets: list[dict[str, Any]] = field(default_factory=list)
    fast_indicators: list[dict[str, Any]] = field(default_factory=list)
    price_alert_runtime: list[dict[str, Any]] = field(default_factory=list)
    bases_error: str = ""
    targets_error: str = ""
    fast_indicators_error: str = ""
    alert_runtime_error: str = ""
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    error: str = ""
    requires_resubscribe: bool = False
    task: asyncio.Task[None] | None = None
    route_snapshot: QuoteRouteSnapshot | None = None
    routes: tuple[InstrumentRoute, ...] = field(init=False)
    trade_routes: dict[tuple[str, str], InstrumentRoute] = field(init=False)

    def __post_init__(self) -> None:
        self._set_instruments(self.instruments)

    def _set_instruments(self, instruments: list[dict[str, Any]]) -> None:
        routes = tuple(map(route_instrument, instruments))
        self.instruments = instruments
        self.routes = routes
        self.trade_routes = {
            (route.instrument_id, route.fingerprint): route
            for route in routes
            if route.adapter.capabilities.quote_trade_events
        }

    def refresh_instruments(self) -> bool:
        snapshot = self.deps.quote_route_snapshot()
        if snapshot is self.route_snapshot:
            return False
        if self.route_snapshot is not None and all(
            snapshot.entries.get(request.instrument_id)
            is self.route_snapshot.entries.get(request.instrument_id)
            for request in self.requested_routes
        ):
            self.route_snapshot = snapshot
            return False
        instruments = resolve_route_selection(snapshot.select_instruments, self.requested_routes)
        self._set_instruments(instruments)
        self.route_snapshot = snapshot
        return True


_QUOTE_SNAPSHOT_PRODUCERS: dict[tuple[Any, ...], _QuoteSnapshotProducer] = {}
_QUOTE_SNAPSHOT_RELEASE_TASKS: set[asyncio.Task[None]] = set()
_LIVE_TRADE_POLL_SECONDS = 0.05
_LIVE_TRADE_PREVIEW_TTL_SECONDS = 10.0
_QUOTE_HEARTBEAT_MAX_SECONDS = 3.0
_SCREENER_BASE_GENERATION_POLL_SECONDS = 0.25
_SCREENER_BASE_RETRY_SECONDS = 5.0
_SCREENER_BASE_SAFETY_REFRESH_SECONDS = 300.0
_FAST_INDICATOR_SNAPSHOT_POLL_SECONDS = 0.1


def _screener_base_generations(
    routes: Sequence[InstrumentRoute],
    interval: str,
) -> dict[tuple[str, str], int]:
    generations: dict[tuple[str, str], int] = {}
    for route in routes:
        identity = (route.instrument_id, route.fingerprint)
        generations[identity] = chart_bars_updated_generation(
            interval,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
    return generations


def _quote_trade_bar_axis(
    producer: _QuoteSnapshotProducer,
    route: Any,
    trade_ts: datetime,
) -> tuple[ProviderBarBucket, dict[str, Any]] | None:
    """Resolve a provisional trade bucket without storage or schedule I/O."""

    if route.adapter.continuous_session(route.instrument):
        bucket = provider_bar_bucket(
            trade_ts,
            producer.interval,
            instrument=route.instrument,
        )
        if bucket is None:
            return None
        return bucket, {}
    starts_at = interval_bucket(trade_ts, producer.interval)
    return (
        ProviderBarBucket(
            starts_at=starts_at,
            closes_at=starts_at + timedelta(seconds=interval_seconds(producer.interval)),
        ),
        {},
    )


def _read_quote_trade_events(
    producer: _QuoteSnapshotProducer,
    route_identities: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, tuple[int, bool]]]:
    routes_by_provider: dict[str, tuple[Any, list[tuple[str, str]]]] = {}
    for identity in route_identities:
        route = producer.trade_routes.get(identity)
        if route is None:
            continue
        adapter, identities = routes_by_provider.setdefault(
            route.provider,
            (route.adapter, []),
        )
        identities.append(identity)
    events: list[dict[str, Any]] = []
    read_states: dict[str, tuple[int, bool]] = {}
    for provider, (adapter, identities) in routes_by_provider.items():
        current_sequence = producer.live_trade_sequences.get(provider, -1)
        sequence, provider_events, provider_overflowed = adapter.read_quote_trade_events(
            current_sequence,
            tuple(identities),
        )
        if type(sequence) is not int or sequence < current_sequence:
            raise ValueError(f"{provider.upper()}_QUOTE_TRADE_SEQUENCE_INVALID")
        read_states[provider] = (sequence, bool(provider_overflowed))
        events.extend(
            {
                **event,
                "_quote_trade_provider": provider,
            }
            for event in provider_events
            if isinstance(event, dict)
        )
    return events, read_states


@dataclass(frozen=True, slots=True)
class _AdmittedQuoteTrade:
    route: Any
    identity: tuple[str, str]
    sequence: int
    price: float
    trade_ts: datetime
    gateway_ts: datetime
    bucket: ProviderBarBucket
    slot_metadata: dict[str, Any]


def _parsed_aware_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _admit_quote_trade_event(
    producer: _QuoteSnapshotProducer,
    event: dict[str, Any],
    routes: dict[tuple[str, str], Any],
    retryable_providers: set[str] | None,
) -> _AdmittedQuoteTrade | None:
    event_provider = event.get("_quote_trade_provider")
    if (
        retryable_providers is not None
        and isinstance(event_provider, str)
        and event_provider in retryable_providers
    ):
        return None
    identity = (event.get("instrument_id"), event.get("route_fingerprint"))
    if not all(isinstance(value, str) for value in identity):
        return None
    route = routes.get(identity)
    if route is None or (isinstance(event_provider, str) and event_provider != route.provider):
        return None
    sequence = event.get("sequence")
    price = float_or_none(event.get("price"))
    trade_ts = _parsed_aware_utc(event.get("ts"))
    if type(sequence) is not int or sequence < 0 or price is None or trade_ts is None:
        return None
    raw_gateway_ts = event.get("gateway_ts")
    gateway_ts = quote_snapshot_ts(
        {"received_at": raw_gateway_ts, "time_basis": "client_receive"}
        if raw_gateway_ts is not None
        else {}
    )
    if gateway_ts is None:
        return None
    bar_axis = _quote_trade_bar_axis(producer, route, trade_ts)
    if bar_axis is None:
        if retryable_providers is not None:
            retryable_providers.add(route.provider)
        return None
    bucket, slot_metadata = bar_axis
    return _AdmittedQuoteTrade(
        route=route,
        identity=identity,
        sequence=sequence,
        price=price,
        trade_ts=trade_ts,
        gateway_ts=gateway_ts,
        bucket=bucket,
        slot_metadata=slot_metadata,
    )


def _project_quote_trade_bar(
    producer: _QuoteSnapshotProducer,
    trade: _AdmittedQuoteTrade,
) -> dict[str, Any] | None:
    existing = producer.live_bars.get(trade.identity)
    existing_sequence = existing.get("event_sequence") if isinstance(existing, dict) else None
    if type(existing_sequence) is int and trade.sequence <= existing_sequence:
        return None
    existing_bucket = _parsed_aware_utc(existing.get("ts")) if isinstance(existing, dict) else None
    if existing_bucket is not None and existing_bucket > trade.bucket.starts_at:
        return None
    if existing_bucket == trade.bucket.starts_at and isinstance(existing, dict):
        existing_preview_ts = _parsed_aware_utc(existing.get("preview_ts"))
        if existing_preview_ts is not None and trade.trade_ts < existing_preview_ts:
            return None
        existing_high = float_or_none(existing.get("high"))
        existing_low = float_or_none(existing.get("low"))
        return {
            **existing,
            "high": max(existing_high if existing_high is not None else trade.price, trade.price),
            "low": min(existing_low if existing_low is not None else trade.price, trade.price),
            "close": trade.price,
            "preview_ts": trade.trade_ts.isoformat(),
            "gateway_ts": trade.gateway_ts.isoformat(),
            "event_sequence": trade.sequence,
        }
    route = trade.route
    return {
        "symbol": route.instrument_key,
        "ts": trade.bucket.starts_at.isoformat(),
        "open": trade.price,
        "high": trade.price,
        "low": trade.price,
        "close": trade.price,
        "volume": 0.0,
        "timeframe": producer.interval,
        "source": f"{route.provider}:trade-preview",
        "closed": False,
        "state": "forming",
        "provider": route.provider,
        "instrument_id": route.instrument_id,
        "provider_symbol": route.provider_symbol,
        "route_fingerprint": route.fingerprint,
        "preview_kind": "broker_trade",
        "preview_ts": trade.trade_ts.isoformat(),
        "gateway_ts": trade.gateway_ts.isoformat(),
        "event_sequence": trade.sequence,
        "authoritative": False,
        "commit_pending": False,
        "expected_close": trade.bucket.closes_at.isoformat(),
        **trade.slot_metadata,
    }


async def _update_quote_live_bars(
    producer: _QuoteSnapshotProducer,
    events: list[dict[str, Any]],
    *,
    retryable_providers: set[str] | None = None,
) -> bool:
    changed = False
    instrument_routes = producer.trade_routes
    for event in events:
        trade = _admit_quote_trade_event(
            producer,
            event,
            instrument_routes,
            retryable_providers,
        )
        if trade is None:
            continue
        next_bar = _project_quote_trade_bar(producer, trade)
        if next_bar is None:
            continue
        existing = producer.live_bars.get(trade.identity)
        if next_bar != existing:
            producer.live_bars[trade.identity] = next_bar
            producer.live_bar_revisions[trade.identity] = producer.live_bars_revision + 1
            changed = True
    return changed


def _producer_route_pairs(producer: _QuoteSnapshotProducer) -> list[tuple[str, str]]:
    return [(route.instrument_id, route.fingerprint) for route in producer.routes]


def _validated_option_targets(
    producer: _QuoteSnapshotProducer,
    stored_targets: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_routes = set(_producer_route_pairs(producer))
    targets: list[dict[str, Any]] = []
    for row in stored_targets:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        target_route = (
            require_exact_identity_text(
                payload.get("instrument_id"), field="option_target.instrument_id"
            ),
            require_exact_identity_text(
                payload.get("route_fingerprint"), field="option_target.route_fingerprint"
            ),
        )
        if (
            target_route not in expected_routes
            or str(payload.get("timeframe") or "") != producer.interval
        ):
            raise ValueError("option target must match the exact quote subscription")
        targets.append(payload)
    return targets


def _validated_alert_runtime(
    producer: _QuoteSnapshotProducer,
    stored_alerts: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_routes = set(_producer_route_pairs(producer))
    runtime_rows: list[dict[str, Any]] = []
    for payload in stored_alerts:
        if not isinstance(payload, dict):
            continue
        runtime_state = price_alert_runtime_state(payload)
        alert_route = (
            require_exact_identity_text(
                runtime_state.get("instrument_id"), field="price_alert.instrument_id"
            ),
            require_exact_identity_text(
                runtime_state.get("route_fingerprint"), field="price_alert.route_fingerprint"
            ),
        )
        if (
            alert_route not in expected_routes
            or str(runtime_state.get("timeframe") or "") != producer.interval
        ):
            raise ValueError("price alert must match the exact quote subscription")
        runtime_rows.append(runtime_state)
    runtime_rows.sort(
        key=lambda item: (
            item["instrument_id"],
            item["route_fingerprint"],
            item["timeframe"],
            item["id"],
        )
    )
    return runtime_rows


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fresh_trade_events(
    events: Sequence[dict[str, Any]], freshness_cutoff: datetime
) -> list[dict[str, Any]]:
    fresh: list[dict[str, Any]] = []
    for event in events:
        raw_gateway_ts = event.get("gateway_ts")
        gateway_ts = quote_snapshot_ts(
            {"received_at": raw_gateway_ts, "time_basis": "client_receive"},
        )
        if gateway_ts is not None and gateway_ts >= freshness_cutoff:
            fresh.append(event)
    return fresh


def _expire_live_bars(
    producer: _QuoteSnapshotProducer,
    freshness_cutoff: datetime,
) -> bool:
    expired: list[tuple[str, str]] = []
    for identity, bar in producer.live_bars.items():
        raw_gateway_ts = bar.get("gateway_ts")
        gateway_ts = quote_snapshot_ts(
            {"received_at": raw_gateway_ts, "time_basis": "client_receive"}
            if raw_gateway_ts is not None
            else {}
        )
        if gateway_ts is None or gateway_ts < freshness_cutoff:
            expired.append(identity)
    for identity in expired:
        producer.live_bars.pop(identity, None)
        producer.live_bar_revisions.pop(identity, None)
    return bool(expired)


@dataclass
class _QuoteProducerRuntime:
    producer: _QuoteSnapshotProducer
    bases: dict[str, dict] = field(default_factory=dict)
    bases_task: asyncio.Task[dict[str, dict]] | None = None
    bases_task_generations: dict[tuple[str, str], int] = field(default_factory=dict)
    bases_task_identities: frozenset[tuple[str, str]] = frozenset()
    bases_task_full_refresh: bool = True
    bases_task_reason: str = "initial"
    loaded_base_generations: dict[tuple[str, str], int] = field(default_factory=dict)
    last_base_safety_refresh: float = 0.0
    next_base_generation_check_at: float = 0.0
    next_base_retry_at: float = 0.0
    rows_signature: str = ""
    option_targets_signature: str = ""
    fast_indicators_signature: str = ""
    alert_runtime_signature: str | None = None
    last_alert_registry_revision: int | None = None
    next_option_targets_refresh_at: float = 0.0
    next_fast_indicators_refresh_at: float = 0.0
    targets_task: asyncio.Task[list[dict[str, Any]]] | None = None
    targets_task_revision: int | None = None
    targets_task_scope: tuple[tuple[str, str], ...] = ()
    applied_targets_revision: int | None = None
    applied_targets_scope: tuple[tuple[str, str], ...] = ()
    rows_task: asyncio.Task[list[dict[str, Any]]] | None = None
    rows_task_cache_revision: QuoteCacheRevision | None = None
    applied_cache_revision: QuoteCacheRevision | None = None
    rows_refresh_required: bool = True
    live_route_identities: list[tuple[str, str]] = field(default_factory=list)

    @property
    def deps(self) -> QuoteStreamWsDeps:
        return self.producer.deps

    def start_initial_bases(self) -> None:
        generations = _screener_base_generations(
            self.producer.routes,
            self.producer.interval,
        )
        self.bases_task_generations = dict(generations)
        self.bases_task_identities = frozenset(generations)
        self._start_bases_task(self.producer.instruments, reason="initial", full_refresh=True)

    def _start_bases_task(
        self,
        instruments: list[dict[str, Any]],
        *,
        reason: str,
        full_refresh: bool,
    ) -> None:
        self.bases_task_reason = reason
        self.bases_task_full_refresh = full_refresh
        self.bases_task = asyncio.create_task(
            asyncio.to_thread(
                self.deps.screener_bases,
                instruments,
                self.producer.interval,
            ),
            name=f"quote-screener-bases:{self.producer.interval}",
        )
        increment_metric("quote_screener_bases_refresh_total", status="scheduled", reason=reason)
        increment_metric(
            "quote_screener_bases_routes_total",
            len(self.bases_task_identities),
            reason=reason,
        )

    def complete_bases(self, now: float) -> None:
        if self.bases_task is None or not self.bases_task.done():
            return
        try:
            refreshed = self.bases_task.result()
            if self.bases_task_full_refresh:
                self.bases = refreshed
                self.last_base_safety_refresh = now
            else:
                self.bases.update(refreshed)
            self.loaded_base_generations.update(self.bases_task_generations)
            self.producer.bases_error = ""
            self.next_base_retry_at = 0.0
            increment_metric(
                "quote_screener_bases_refresh_total",
                status="completed",
                reason=self.bases_task_reason,
            )
        except Exception as exc:
            self.producer.bases_error = str(exc) or exc.__class__.__name__
            self.next_base_retry_at = now + _SCREENER_BASE_RETRY_SECONDS
            increment_metric(
                "quote_screener_bases_refresh_total",
                status="error",
                reason=self.bases_task_reason,
            )
        self.bases_task = None
        self.rows_refresh_required = True

    def complete_targets(self) -> None:
        if self.targets_task is None or not self.targets_task.done():
            return
        task = self.targets_task
        self.targets_task = None
        try:
            rows = task.result()
            if (
                self.targets_task_revision != option_targets_revision()
                or self.targets_task_scope != tuple(_producer_route_pairs(self.producer))
            ):
                self.next_option_targets_refresh_at = 0.0
                return
            targets = _validated_option_targets(self.producer, rows)
            signature = _stable_json(targets)
            if signature != self.option_targets_signature:
                self.option_targets_signature = signature
                self.producer.option_targets = targets
                self.producer.targets_revision = (self.producer.targets_revision or 0) + 1
            self.producer.targets_error = ""
            self.applied_targets_revision = self.targets_task_revision
            self.applied_targets_scope = self.targets_task_scope
        except Exception as exc:
            self.producer.targets_error = str(exc) or exc.__class__.__name__
            self.deps.logger.warning("option target stream snapshot failed: %s", exc)

    def refresh_fast_indicators(self, now: float) -> None:
        if self.producer.targets_revision is None or now < self.next_fast_indicators_refresh_at:
            return
        self.next_fast_indicators_refresh_at = now + _FAST_INDICATOR_SNAPSHOT_POLL_SECONDS
        try:
            snapshot = self.deps.fast_indicator_snapshots(
                self.producer.option_targets,
                self.producer.interval,
            )
            published = snapshot if snapshot is not None else []
            signature = _stable_json(published)
            if signature != self.fast_indicators_signature:
                self.fast_indicators_signature = signature
                self.producer.fast_indicators = published
                self.producer.fast_indicators_revision = (
                    self.producer.fast_indicators_revision or 0
                ) + 1
            self.producer.fast_indicators_error = ""
        except Exception as exc:
            signature = _stable_json([])
            if signature != self.fast_indicators_signature:
                self.fast_indicators_signature = signature
                self.producer.fast_indicators = []
                self.producer.fast_indicators_revision = (
                    self.producer.fast_indicators_revision or 0
                ) + 1
            self.producer.fast_indicators_error = str(exc) or exc.__class__.__name__
            self.deps.logger.warning("fast indicator stream snapshot failed: %s", exc)

    def refresh_alerts(self) -> None:
        stored_alerts, revision = price_alert_runtime.scope_snapshot(
            route_pairs=_producer_route_pairs(self.producer),
            timeframe=self.producer.interval,
            after_revision=self.last_alert_registry_revision,
        )
        if stored_alerts is None:
            return
        try:
            runtime_rows = _validated_alert_runtime(self.producer, stored_alerts)
            signature = _stable_json(runtime_rows)
            if signature != self.alert_runtime_signature:
                self.alert_runtime_signature = signature
                self.producer.price_alert_runtime = runtime_rows
                self.producer.alert_runtime_revision = (
                    self.producer.alert_runtime_revision or 0
                ) + 1
            self.producer.alert_runtime_error = ""
            self.last_alert_registry_revision = revision
        except Exception as exc:
            self.producer.alert_runtime_error = str(exc) or exc.__class__.__name__
            self.deps.logger.warning("price alert stream snapshot failed: %s", exc)

    def complete_rows(self) -> None:
        if self.rows_task is None or not self.rows_task.done():
            return
        task = self.rows_task
        self.rows_task = None
        rows = task.result()
        self.deps.record_quote_execution_snapshots(rows)
        signature = _stable_json([project_quote_stream_row(row) for row in rows])
        if signature != self.rows_signature:
            self.rows_signature = signature
            self.producer.rows = rows
            self.producer.rows_revision += 1
        if self.rows_task_cache_revision is None:
            raise RuntimeError("QUOTE_CACHE_REVISION_REQUIRED")
        self.producer.rows_cache_revision = self.rows_task_cache_revision
        self.producer.error = ""
        self.applied_cache_revision = self.rows_task_cache_revision
        self.rows_task_cache_revision = None

    async def refresh_live_trade_preview(self) -> None:
        try:
            events, read_states = _read_quote_trade_events(
                self.producer,
                self.live_route_identities,
            )
            freshness_cutoff = datetime.now(tz=UTC) - timedelta(
                seconds=_LIVE_TRADE_PREVIEW_TTL_SECONDS
            )
            expired = _expire_live_bars(self.producer, freshness_cutoff)
            if expired:
                self.producer.live_bars_revision += 1
            retryable_providers: set[str] = set()
            updated = await _update_quote_live_bars(
                self.producer,
                _fresh_trade_events(events, freshness_cutoff),
                retryable_providers=retryable_providers,
            )
            overflowed = self._acknowledge_live_trade_reads(
                read_states,
                retryable_providers,
            )
            if overflowed or updated:
                self.producer.live_bars_revision += 1
            if overflowed:
                self.producer.live_trade_overflow_sequence = self.producer.live_bars_revision
                increment_metric("quote_trade_event_overflow_total")
        except Exception as exc:
            self.deps.logger.debug("live quote trade preview failed: %s", exc)

    def _acknowledge_live_trade_reads(
        self,
        read_states: dict[str, tuple[int, bool]],
        retryable_providers: set[str],
    ) -> bool:
        overflowed = False
        # The adapter sequence is a read ceiling, not an acknowledgement: keep it
        # behind the first provider event awaiting server-owned axis data.
        for provider, (read_sequence, provider_overflowed) in read_states.items():
            if provider in retryable_providers:
                continue
            self.producer.live_trade_sequences[provider] = read_sequence
            overflowed = overflowed or provider_overflowed
        return overflowed

    def schedule_bases(self, now: float) -> None:
        if (
            self.bases_task is not None
            or now < self.next_base_generation_check_at
            or now < self.next_base_retry_at
        ):
            return
        self.next_base_generation_check_at = now + _SCREENER_BASE_GENERATION_POLL_SECONDS
        current = _screener_base_generations(
            self.producer.routes,
            self.producer.interval,
        )
        full_refresh = (
            set(current) != set(self.loaded_base_generations)
            or now - self.last_base_safety_refresh >= _SCREENER_BASE_SAFETY_REFRESH_SECONDS
        )
        changed = {
            identity
            for identity, generation in current.items()
            if self.loaded_base_generations.get(identity) != generation
        }
        identities = set(current) if full_refresh else changed
        if not identities:
            return
        instruments = [
            instrument
            for instrument, route in zip(
                self.producer.instruments, self.producer.routes, strict=True
            )
            if (route.instrument_id, route.fingerprint) in identities
        ]
        self.bases_task_generations = {identity: current[identity] for identity in identities}
        self.bases_task_identities = frozenset(identities)
        reason = "safety" if full_refresh else "canonical_generation"
        self._start_bases_task(instruments, reason=reason, full_refresh=full_refresh)

    def schedule_targets(self, now: float) -> None:
        if now < self.next_option_targets_refresh_at or self.targets_task is not None:
            return
        self.next_option_targets_refresh_at = now + 1.0
        revision = option_targets_revision()
        scope = tuple(_producer_route_pairs(self.producer))
        if revision == self.applied_targets_revision and scope == self.applied_targets_scope:
            return
        self.targets_task_revision = revision
        self.targets_task_scope = scope

        def read_targets() -> list[dict[str, Any]]:
            store = self.deps.store_factory()
            if store is None:
                raise RuntimeError("option target storage is not configured")
            return store.read_option_targets(timeframe=self.producer.interval, route_pairs=scope)

        self.targets_task = asyncio.create_task(
            asyncio.to_thread(read_targets),
            name=f"quote-option-targets:{self.producer.interval}",
        )

    def schedule_rows(self) -> None:
        initial_bases_ready = self.producer.rows is not None or self.bases_task is None
        if not initial_bases_ready or self.rows_task is not None:
            return
        live_map, live_warning, cache_revision = self.deps.quote_cache_for_instruments(
            self.producer.instruments,
            after_revision=(
                None
                if self.producer.rows is None or self.rows_refresh_required
                else self.applied_cache_revision
            ),
        )
        if live_map is None:
            return
        self.rows_task = asyncio.create_task(
            asyncio.to_thread(
                self.deps.screener_rows,
                self.producer.instruments,
                self.bases,
                live_map,
                live_warning,
                self.producer.interval,
            ),
            name=f"quote-screener-rows:{self.producer.interval}",
        )
        self.rows_task_cache_revision = cache_revision
        self.rows_refresh_required = False

    async def step(self) -> None:
        if self.producer.refresh_instruments():
            self.rows_refresh_required = True
            self.last_alert_registry_revision = None
        now = time.monotonic()
        self.complete_bases(now)
        self.complete_targets()
        self.refresh_fast_indicators(now)
        self.refresh_alerts()
        self.complete_rows()
        await self.refresh_live_trade_preview()
        self.schedule_bases(now)
        self.schedule_targets(now)
        self.schedule_rows()

    async def cancel(self) -> None:
        tasks = tuple(
            task
            for task in (
                self.bases_task,
                self.targets_task,
                self.rows_task,
            )
            if task is not None
        )
        cancellation: asyncio.CancelledError | None = None
        for task in tasks:
            outcome = await settle_physical_task(
                task,
                deferred_cancellation=cancellation,
            )
            cancellation = cancellation or outcome.cancellation
            if outcome.error is not None:
                self.deps.logger.debug(
                    "quote producer physical task settled with error: %s",
                    outcome.error,
                )
        self.bases_task = None
        self.targets_task = None
        self.rows_task = None
        if cancellation is not None:
            raise cancellation


def _quote_producer_runtime(producer: _QuoteSnapshotProducer) -> _QuoteProducerRuntime:
    runtime = _QuoteProducerRuntime(producer=producer)
    runtime.live_route_identities = [identity for identity in producer.trade_routes]
    runtime.start_initial_bases()
    return runtime


async def _run_quote_snapshot_producer(producer: _QuoteSnapshotProducer) -> None:
    try:
        producer.refresh_instruments()
    except asyncio.CancelledError:
        producer.ready.set()
        raise
    except RouteSelectionMismatch as exc:
        producer.error = str(exc)
        producer.requires_resubscribe = True
        producer.ready.set()
        return
    except Exception as exc:
        producer.error = str(exc) or exc.__class__.__name__
        producer.ready.set()
        return

    runtime = _quote_producer_runtime(producer)
    try:
        while True:
            try:
                await runtime.step()
            except RouteSelectionMismatch as exc:
                producer.error = str(exc)
                producer.requires_resubscribe = True
                await runtime.cancel()
                producer.ready.set()
                return
            except Exception as exc:
                producer.error = str(exc)
            producer.ready.set()
            await asyncio.sleep(_LIVE_TRADE_POLL_SECONDS)
    except asyncio.CancelledError:
        await runtime.cancel()
        raise


def _acquire_quote_snapshot_producer(
    *,
    key: tuple[Any, ...],
    deps: QuoteStreamWsDeps,
    instruments: list[dict[str, Any]],
    requested_routes: tuple[RequestedInstrumentRoute, ...],
    quote_wanted_keys: list[str],
    interval: str,
) -> _QuoteSnapshotProducer:
    producer = _QUOTE_SNAPSHOT_PRODUCERS.get(key)
    if producer is not None and producer.task is not None and producer.task.done():
        _QUOTE_SNAPSHOT_PRODUCERS.pop(key, None)
        deps.set_quote_stream_wanted(id(producer), [])
        producer = None
    if producer is None:
        producer = _QuoteSnapshotProducer(
            key=key,
            deps=deps,
            instruments=instruments,
            requested_routes=requested_routes,
            quote_wanted_keys=quote_wanted_keys,
            interval=interval,
        )
        deps.set_quote_stream_wanted(id(producer), quote_wanted_keys)
        producer.task = asyncio.create_task(
            _run_quote_snapshot_producer(producer),
            name=f"quote-snapshot-producer:{interval}",
        )
        _QUOTE_SNAPSHOT_PRODUCERS[key] = producer
    producer.consumers += 1
    return producer


async def _release_quote_snapshot_producer(producer: _QuoteSnapshotProducer) -> None:
    producer.consumers = max(0, producer.consumers - 1)
    if producer.consumers or _QUOTE_SNAPSHOT_PRODUCERS.get(producer.key) is not producer:
        return
    grace_seconds = max(float(producer.deps.quote_stream_cleanup_grace_seconds), 0.0)
    if grace_seconds and not producer.deps.server_sleeping():
        await asyncio.sleep(grace_seconds)
    if producer.consumers or _QUOTE_SNAPSHOT_PRODUCERS.get(producer.key) is not producer:
        return
    _QUOTE_SNAPSHOT_PRODUCERS.pop(producer.key, None)
    if producer.task is not None:
        producer.task.cancel()
        await asyncio.gather(producer.task, return_exceptions=True)
    producer.deps.set_quote_stream_wanted(id(producer), [])


def _schedule_quote_snapshot_release(
    producer: _QuoteSnapshotProducer,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _release_quote_snapshot_producer(producer),
        name=f"quote-snapshot-release:{producer.interval}",
    )
    _QUOTE_SNAPSHOT_RELEASE_TASKS.add(task)
    task.add_done_callback(_QUOTE_SNAPSHOT_RELEASE_TASKS.discard)
    return task


async def _send_quote_sleep_status(websocket: WebSocket) -> None:
    await send_stream_json(
        websocket,
        {
            "type": "quote_status",
            "source": "server:sleep",
            "ts": datetime.now(tz=UTC).isoformat(),
            "code": "SERVER_SLEEPING",
            "retryable": True,
            "message": "Server sleeping: quote stream paused.",
        },
        "quotes",
    )


def _quote_producer_warnings(producer: _QuoteSnapshotProducer) -> dict[str, str]:
    return {
        "quote_rows": producer.error,
        "screener_bases": producer.bases_error,
        "option_targets": producer.targets_error,
        "fast_indicators": producer.fast_indicators_error,
        "price_alert_runtime": producer.alert_runtime_error,
    }


def _exact_quote_rows(
    rows: list[dict[str, Any]],
    expected_identities: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    projected = [project_quote_stream_row(row) for row in rows]
    by_identity = {
        (
            require_exact_identity_text(row.get("instrument_id"), field="instrument_id"),
            require_exact_identity_text(row.get("route_fingerprint"), field="route_fingerprint"),
        ): row
        for row in projected
    }
    if len(by_identity) != len(projected) or by_identity.keys() != expected_identities:
        raise ValueError("quote rows must contain each subscribed exact route exactly once")
    return projected, by_identity


@dataclass(slots=True)
class _QuoteStreamConsumer:
    websocket: WebSocket
    deps: QuoteStreamWsDeps
    producer: _QuoteSnapshotProducer
    subscription: dict[str, Any]
    expected_row_identities: set[tuple[str, str]]
    last_rows_revision: int = -1
    last_rows_cache_revision: QuoteCacheRevision | None = None
    last_targets_revision: int = 0
    last_fast_indicators_revision: int = 0
    last_alert_runtime_revision: int = 0
    last_live_bars_revision: int = -1
    last_loss_sequence: int = 0
    last_error: str = ""
    last_rows_by_identity: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    last_aux_signature: str = ""
    quote_sequence: int = 0
    snapshot_sent: bool = False
    last_quote_state_sent_at: float = 0.0

    async def send_live_bars(self) -> None:
        revision = self.producer.live_bars_revision
        if revision == self.last_live_bars_revision:
            return
        if revision <= 0:
            self.last_live_bars_revision = revision
            return
        loss_sequence = (
            self.producer.live_trade_overflow_sequence
            if self.producer.live_trade_overflow_sequence > self.last_loss_sequence
            else 0
        )
        bars = (
            list(self.producer.live_bars.values())
            if loss_sequence
            else [
                bar
                for identity, bar in self.producer.live_bars.items()
                if self.producer.live_bar_revisions.get(identity, 0) > self.last_live_bars_revision
            ]
        )
        if bars or loss_sequence:
            await send_stream_json(
                self.websocket,
                {
                    "type": "live_candle_delta",
                    "source": "quote:trade-preview",
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "interval": self.producer.interval,
                    "sequence": revision,
                    "loss_after_sequence": loss_sequence or None,
                    "bars": bars,
                },
                "quotes",
            )
        self.last_live_bars_revision = revision
        self.last_loss_sequence = max(self.last_loss_sequence, loss_sequence)

    async def send_rows(self, now: float) -> None:
        revision = self.producer.rows_revision
        rows = None if self.producer.rows is None else list(self.producer.rows)
        cache_revision = self.producer.rows_cache_revision
        rows_changed = revision != self.last_rows_revision
        cache_changed = cache_revision != self.last_rows_cache_revision
        if rows is None or not (rows_changed or cache_changed):
            return
        if cache_revision is None:
            raise RuntimeError("QUOTE_CACHE_REVISION_REQUIRED")
        projected, by_identity = _exact_quote_rows(rows, self.expected_row_identities)
        warnings = _quote_producer_warnings(self.producer)
        aux_signature = _stable_json(warnings)
        if not self.snapshot_sent:
            await self._send_initial_rows(
                projected,
                warnings,
                revision,
                cache_revision,
            )
            self.snapshot_sent = True
            self.last_aux_signature = aux_signature
            self.last_quote_state_sent_at = now
        else:
            dirty_rows = [
                row
                for identity, row in by_identity.items()
                if self.last_rows_by_identity.get(identity) != row
            ]
            if dirty_rows or cache_changed:
                await self._send_row_delta(dirty_rows, revision, cache_revision)
                self.last_quote_state_sent_at = now
        self.last_rows_by_identity = by_identity
        self.last_rows_revision = revision
        self.last_rows_cache_revision = cache_revision

    async def _send_initial_rows(
        self,
        rows: list[dict[str, Any]],
        warnings: dict[str, str],
        revision: int,
        cache_revision: QuoteCacheRevision,
    ) -> None:
        self.quote_sequence += 1
        message = {
            "type": "quote_snapshot",
            "source": "quote:stream",
            "ts": datetime.now(tz=UTC).isoformat(),
            "sequence": self.quote_sequence,
            "revision": revision,
            "cache_epoch": cache_revision.epoch,
            "cache_generation": cache_revision.generation,
            "subscription": self.subscription,
            "rows": rows,
        }
        if any(warnings.values()):
            message["degraded"] = True
            message["warnings"] = warnings
        await send_stream_json(self.websocket, message, "quotes")

    async def _send_row_delta(
        self,
        rows: list[dict[str, Any]],
        revision: int,
        cache_revision: QuoteCacheRevision,
    ) -> None:
        self.quote_sequence += 1
        await send_stream_json(
            self.websocket,
            {
                "type": "quote_delta",
                "source": "quote:stream",
                "ts": datetime.now(tz=UTC).isoformat(),
                "interval": self.producer.interval,
                "sequence": self.quote_sequence,
                "revision": revision,
                "cache_epoch": cache_revision.epoch,
                "cache_generation": cache_revision.generation,
                "rows": rows,
            },
            "quotes",
        )

    async def send_producer_error(self) -> None:
        error = self.producer.error
        requires_resubscribe = self.producer.requires_resubscribe
        if error and error != self.last_error and (not self.snapshot_sent or requires_resubscribe):
            await send_stream_json(
                self.websocket,
                self.deps.stream_status_payload(
                    status_type="quote_status",
                    source="quote:stream",
                    code=(
                        "QUOTE_STREAM_ROUTE_CHANGED"
                        if requires_resubscribe
                        else "QUOTE_STREAM_ERROR"
                    ),
                    category="routing" if requires_resubscribe else "stream",
                    retryable=True,
                    error=RuntimeError(error),
                    retry_in_seconds=self.deps.quote_stream_seconds,
                    requires_resubscribe=requires_resubscribe,
                ),
                "quotes",
            )
        self.last_error = error

    async def send_option_targets(self, now: float) -> None:
        revision = self.producer.targets_revision
        changed = revision is not None and revision != self.last_targets_revision
        if not self.snapshot_sent or not changed or self.producer.targets_error:
            return
        self.quote_sequence += 1
        await send_stream_json(
            self.websocket,
            {
                "type": "option_targets_snapshot",
                "source": "quote:stream",
                "ts": datetime.now(tz=UTC).isoformat(),
                "interval": self.producer.interval,
                "sequence": self.quote_sequence,
                "revision": revision,
                "option_targets": list(self.producer.option_targets),
            },
            "quotes",
        )
        self.last_targets_revision = revision
        self.last_quote_state_sent_at = now

    async def send_fast_indicators(self, now: float) -> None:
        revision = self.producer.fast_indicators_revision
        changed = revision is not None and revision != self.last_fast_indicators_revision
        if not self.snapshot_sent or not changed:
            return
        self.quote_sequence += 1
        await send_stream_json(
            self.websocket,
            {
                "type": "fast_indicators_snapshot",
                "source": "quote:fast-indicators",
                "ts": datetime.now(tz=UTC).isoformat(),
                "interval": self.producer.interval,
                "sequence": self.quote_sequence,
                "revision": revision,
                "scopes": list(self.producer.fast_indicators),
            },
            "quotes",
        )
        self.last_fast_indicators_revision = revision
        self.last_quote_state_sent_at = now

    async def send_aux_status(self, now: float) -> None:
        if not self.snapshot_sent:
            return
        warnings = _quote_producer_warnings(self.producer)
        signature = _stable_json(warnings)
        if self.last_aux_signature and signature != self.last_aux_signature:
            self.quote_sequence += 1
            await send_stream_json(
                self.websocket,
                {
                    "type": "quote_aux_status",
                    "source": "quote:stream",
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "interval": self.producer.interval,
                    "sequence": self.quote_sequence,
                    "degraded": any(warnings.values()),
                    "warnings": warnings,
                },
                "quotes",
            )
            self.last_quote_state_sent_at = now
        self.last_aux_signature = signature

    async def send_alert_runtime(self, now: float) -> None:
        revision = self.producer.alert_runtime_revision
        changed = revision is not None and revision != self.last_alert_runtime_revision
        if not self.snapshot_sent or not changed or self.producer.alert_runtime_error:
            return
        self.quote_sequence += 1
        await send_stream_json(
            self.websocket,
            {
                "type": "quote_heartbeat",
                "source": "quote:stream",
                "ts": datetime.now(tz=UTC).isoformat(),
                "interval": self.producer.interval,
                "sequence": self.quote_sequence,
                "revision": self.last_rows_revision,
                "alert_runtime_revision": revision,
                "price_alert_runtime": list(self.producer.price_alert_runtime),
            },
            "quotes",
        )
        self.last_alert_runtime_revision = revision
        self.last_quote_state_sent_at = now

    async def send_heartbeat(self) -> None:
        heartbeat_seconds = min(
            max(self.deps.websocket_heartbeat_seconds, _LIVE_TRADE_POLL_SECONDS),
            _QUOTE_HEARTBEAT_MAX_SECONDS,
        )
        if (
            not self.snapshot_sent
            or time.monotonic() - self.last_quote_state_sent_at < heartbeat_seconds
        ):
            return
        self.quote_sequence += 1
        await send_stream_json(
            self.websocket,
            {
                "type": "quote_heartbeat",
                "source": "quote:stream",
                "ts": datetime.now(tz=UTC).isoformat(),
                "interval": self.producer.interval,
                "sequence": self.quote_sequence,
                "revision": self.last_rows_revision,
            },
            "quotes",
        )
        self.last_quote_state_sent_at = time.monotonic()

    async def step(self) -> bool:
        if self.deps.server_sleeping():
            await _send_quote_sleep_status(self.websocket)
            return False
        now = time.monotonic()
        await self.send_live_bars()
        await self.send_rows(now)
        await self.send_producer_error()
        if self.producer.task is not None and self.producer.task.done():
            return False
        await self.send_option_targets(now)
        await self.send_fast_indicators(now)
        await self.send_aux_status(now)
        await self.send_alert_runtime(now)
        await self.send_heartbeat()
        await asyncio.sleep(_LIVE_TRADE_POLL_SECONDS)
        return True

    async def run(self) -> None:
        while await self.step():
            pass


async def _quote_stream_consumer(
    websocket: WebSocket,
    requested_routes: tuple[RequestedInstrumentRoute, ...],
    interval: str,
    deps: QuoteStreamWsDeps,
) -> _QuoteStreamConsumer | None:
    try:
        instruments = resolve_route_selection(
            deps.quote_route_snapshot().select_instruments,
            requested_routes,
        )
    except RouteSelectionMismatch as exc:
        await send_stream_json(
            websocket,
            deps.stream_status_payload(
                status_type="quote_status",
                source="quote:route",
                code="QUOTE_STREAM_ROUTE_CHANGED",
                category="routing",
                retryable=True,
                error=exc,
                retry_in_seconds=deps.quote_stream_seconds,
                requires_resubscribe=True,
                expected_routes=[
                    {"instrument_id": instrument_id, "route_fingerprint": fingerprint}
                    for instrument_id, fingerprint in exc.expected
                ],
                actual_routes=[
                    {"instrument_id": instrument_id, "route_fingerprint": fingerprint}
                    for instrument_id, fingerprint in exc.actual
                ],
            ),
            "quotes",
        )
        return None
    live_routes = [route_instrument(instrument) for instrument in instruments]
    if any(route.adapter.capabilities.runtime_settings for route in live_routes):
        await deps.apply_provider_runtime_settings_async()
    quote_wanted_keys = [
        route.instrument_id
        for instrument, route in zip(instruments, live_routes, strict=True)
        if not bool(instrument.get("quote_only"))
    ]
    producer = _acquire_quote_snapshot_producer(
        key=(str(interval), tuple(route.identity for route in requested_routes)),
        deps=deps,
        instruments=instruments,
        requested_routes=requested_routes,
        quote_wanted_keys=quote_wanted_keys,
        interval=interval,
    )
    try:
        await producer.ready.wait()
        subscription = {
            "interval": producer.interval,
            "routes": [
                {
                    "instrument_key": route.instrument_key,
                    "instrument_id": route.instrument_id,
                    "provider": route.provider,
                    "provider_symbol": route.provider_symbol,
                    "route_fingerprint": route.fingerprint,
                }
                for route in producer.routes
            ],
        }
        expected_identities = {
            (route["instrument_id"], route["route_fingerprint"]) for route in subscription["routes"]
        }
        return _QuoteStreamConsumer(
            websocket=websocket,
            deps=deps,
            producer=producer,
            subscription=subscription,
            expected_row_identities=expected_identities,
        )
    except BaseException:
        release_task = _schedule_quote_snapshot_release(producer)
        await await_cancellation_deferred_task(
            release_task,
            task_cancelled_error="QUOTE_SNAPSHOT_RELEASE_TASK_CANCELLED",
        )
        raise


async def run_quote_stream(
    websocket: WebSocket,
    routes: str,
    interval: str,
    deps: QuoteStreamWsDeps,
) -> None:
    await websocket.accept()
    try:
        requested_routes = parse_route_selection(routes)
    except ValueError as exc:
        await send_stream_json(
            websocket,
            deps.stream_status_payload(
                status_type="quote_status",
                source="quote:route",
                code="QUOTE_STREAM_ROUTES_INVALID",
                category="request",
                retryable=False,
                error=exc,
                retry_in_seconds=deps.quote_stream_seconds,
                requires_resubscribe=True,
            ),
            "quotes",
        )
        return
    if deps.server_sleeping():
        await _send_quote_sleep_status(websocket)
        return
    deps.quote_stream_started()
    producer: _QuoteSnapshotProducer | None = None
    try:
        consumer = await _quote_stream_consumer(
            websocket,
            requested_routes,
            interval,
            deps,
        )
        if consumer is None:
            return
        producer = consumer.producer
        await consumer.run()
    except WebSocketDisconnect:
        return
    except Exception as exc:
        deps.logger.warning("quote stream failed: %s", exc)
        try:
            await send_stream_json(
                websocket,
                deps.stream_status_payload(
                    status_type="quote_status",
                    source="quote:stream",
                    code="QUOTE_STREAM_ERROR",
                    category="stream",
                    retryable=True,
                    error=exc,
                    retry_in_seconds=deps.quote_stream_seconds,
                ),
                "quotes",
            )
        except Exception:
            return
    finally:
        deps.quote_stream_finished()
        if producer is not None:
            release_task = _schedule_quote_snapshot_release(producer)
            await await_cancellation_deferred_task(
                release_task,
                task_cancelled_error="QUOTE_SNAPSHOT_RELEASE_TASK_CANCELLED",
            )
