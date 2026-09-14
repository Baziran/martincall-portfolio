from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from aef_terminal.alerts.definition_identity import require_unique_price_alert_definitions
from aef_terminal.alerts.runtime_contract import validate_price_alert
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import domain_frozen_value, domain_wire_value
from aef_terminal.runtime.metrics import increment_metric, set_metric


_MAX_QUOTE_EVENTS = 8192


def _freeze_quote_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_quote_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_quote_value(item) for item in value)
    return value


def _wire_quote_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _wire_quote_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_quote_value(item) for item in value]
    return value


def retain_price_alert(alert: Mapping[str, Any]) -> bool:
    """Return whether one alert belongs in durable and process-owned state."""

    return alert["enabled"] is True and (alert["armed"] is True or alert["fired"] is True)


@dataclass(frozen=True, slots=True)
class AlertQuoteEvent:
    sequence: int
    instrument_id: str
    route_fingerprint: str
    quote: Mapping[str, Any]

    def wire_quote(self) -> dict[str, Any]:
        value = _wire_quote_value(self.quote)
        if not isinstance(value, dict):
            raise TypeError("alert quote event must contain an object")
        return value


class AlertRuntimeRegistry:
    """Process-owned alert projection and bounded ordered quote-event inbox.

    PostgreSQL remains the durable authority. The registry is hydrated once at
    runtime startup and every committed alert mutation is published into it.
    Quote ingestion only appends immutable events for exact routes with armed
    consumers; it never performs storage work.
    """

    def __init__(self, *, max_quote_events: int = _MAX_QUOTE_EVENTS) -> None:
        self._lock = threading.RLock()
        self._alerts: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        self._active_route_counts: dict[tuple[str, str], int] = {}
        self._revision = 0
        self._hydrated = False
        self._hydration_generation = 0
        self._active_hydration: int | None = None
        self._hydration_mutations: list[tuple[str, Any]] = []
        self._quote_events: deque[AlertQuoteEvent] = deque(maxlen=max(int(max_quote_events), 1))
        self._quote_sequence = 0
        self._revision_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self._quote_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    @staticmethod
    def _key(alert: Mapping[str, Any]) -> tuple[str, str, str]:
        return (
            require_exact_identity_text(alert.get("id"), field="PRICE_ALERT_ID"),
            require_exact_identity_text(
                alert.get("instrument_id"), field="PRICE_ALERT_INSTRUMENT_ID"
            ),
            require_exact_identity_text(
                alert.get("route_fingerprint"),
                field="PRICE_ALERT_ROUTE_FINGERPRINT",
            ),
        )

    @staticmethod
    def _freeze(alert: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = dict(alert)
        validate_price_alert(payload)
        frozen = domain_frozen_value(payload, field_name="price_alert_runtime")
        if not isinstance(frozen, Mapping):
            raise TypeError("price alert runtime must be an object")
        return frozen

    @staticmethod
    def _wire(alert: Mapping[str, Any]) -> dict[str, Any]:
        payload = domain_wire_value(alert, field_name="price_alert_runtime")
        if not isinstance(payload, dict):
            raise TypeError("price alert runtime must be an object")
        return payload

    def _rebuild_active_routes_locked(self) -> None:
        counts: dict[tuple[str, str], int] = {}
        for alert in self._alerts.values():
            if alert["enabled"] is True and alert["armed"] is True and alert["kind"] == "price":
                route = (
                    alert["instrument_id"],
                    alert["route_fingerprint"],
                )
                counts[route] = counts.get(route, 0) + 1
        self._active_route_counts = counts

    def _changed_locked(self) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]:
        self._revision += 1
        self._rebuild_active_routes_locked()
        waiters = list(self._revision_waiters)
        self._revision_waiters.clear()
        set_metric("price_alert_runtime_revision", self._revision)
        set_metric("price_alert_runtime_rows", len(self._alerts))
        set_metric("price_alert_runtime_quote_routes", len(self._active_route_counts))
        return waiters

    @staticmethod
    def _wake(waiters: Sequence[tuple[asyncio.AbstractEventLoop, asyncio.Event]]) -> None:
        for loop, event in waiters:
            if loop.is_closed():
                continue
            loop.call_soon_threadsafe(event.set)

    def begin_hydration(self) -> int:
        with self._lock:
            if self._active_hydration is not None:
                raise RuntimeError("PRICE_ALERT_RUNTIME_HYDRATION_IN_PROGRESS")
            self._hydration_generation += 1
            self._active_hydration = self._hydration_generation
            self._hydration_mutations.clear()
            return self._hydration_generation

    def cancel_hydration(self, token: int) -> None:
        with self._lock:
            if self._active_hydration != token:
                return
            self._active_hydration = None
            self._hydration_mutations.clear()

    def hydrate(
        self,
        alerts: Sequence[Mapping[str, Any]],
        *,
        token: int | None = None,
    ) -> int:
        next_alerts: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for alert in alerts:
            frozen = self._freeze(alert)
            if not retain_price_alert(frozen):
                raise ValueError("PRICE_ALERT_RUNTIME_INERT_ROW")
            key = self._key(frozen)
            if key in next_alerts:
                raise ValueError(f"PRICE_ALERT_ID_DUPLICATE: {key[0]}")
            next_alerts[key] = frozen
        with self._lock:
            if token is not None and self._active_hydration != token:
                raise RuntimeError("PRICE_ALERT_RUNTIME_HYDRATION_TOKEN_STALE")
            if token is None and self._active_hydration is not None:
                raise RuntimeError("PRICE_ALERT_RUNTIME_HYDRATION_TOKEN_REQUIRED")
            for operation, payload in self._hydration_mutations:
                if operation == "upsert":
                    key, frozen = payload
                    next_alerts[key] = frozen
                elif operation == "remove":
                    next_alerts.pop(payload, None)
                elif operation == "remove_scope":
                    instrument_id, route_fingerprint, timeframe = payload
                    next_alerts = {
                        key: alert
                        for key, alert in next_alerts.items()
                        if not (
                            alert.get("instrument_id") == instrument_id
                            and alert.get("route_fingerprint") == route_fingerprint
                            and alert.get("timeframe") == timeframe
                        )
                    }
            require_unique_price_alert_definitions(next_alerts.values())
            changed = next_alerts != self._alerts or not self._hydrated
            self._alerts = next_alerts
            self._hydrated = True
            self._active_hydration = None
            self._hydration_mutations.clear()
            waiters = self._changed_locked() if changed else []
            revision = self._revision
        self._wake(waiters)
        increment_metric("price_alert_runtime_hydrations_total", status="completed")
        return revision

    def publish_committed(self, alert: Mapping[str, Any]) -> int:
        frozen = self._freeze(alert)
        if not retain_price_alert(frozen):
            raise ValueError("PRICE_ALERT_RUNTIME_INERT_ROW")
        key = self._key(frozen)
        with self._lock:
            projected = {**self._alerts, key: frozen}
            require_unique_price_alert_definitions(projected.values())
            if self._active_hydration is not None:
                self._hydration_mutations.append(("upsert", (key, frozen)))
            changed = self._alerts.get(key) != frozen
            if changed:
                self._alerts[key] = frozen
            waiters = self._changed_locked() if changed else []
            revision = self._revision
        self._wake(waiters)
        if changed:
            increment_metric("price_alert_runtime_publications_total", operation="upsert")
        return revision

    def remove_committed(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
    ) -> int:
        key = (
            require_exact_identity_text(alert_id, field="PRICE_ALERT_ID"),
            require_exact_identity_text(instrument_id, field="PRICE_ALERT_INSTRUMENT_ID"),
            require_exact_identity_text(
                route_fingerprint,
                field="PRICE_ALERT_ROUTE_FINGERPRINT",
            ),
        )
        with self._lock:
            if self._active_hydration is not None:
                self._hydration_mutations.append(("remove", key))
            changed = self._alerts.pop(key, None) is not None
            waiters = self._changed_locked() if changed else []
            revision = self._revision
        self._wake(waiters)
        if changed:
            increment_metric("price_alert_runtime_publications_total", operation="delete")
        return revision

    def remove_scope_committed(
        self,
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str,
    ) -> int:
        exact_instrument_id = require_exact_identity_text(
            instrument_id, field="PRICE_ALERT_INSTRUMENT_ID"
        )
        exact_fingerprint = require_exact_identity_text(
            route_fingerprint, field="PRICE_ALERT_ROUTE_FINGERPRINT"
        )
        exact_timeframe = require_exact_identity_text(timeframe, field="PRICE_ALERT_TIMEFRAME")
        with self._lock:
            if self._active_hydration is not None:
                self._hydration_mutations.append(
                    (
                        "remove_scope",
                        (exact_instrument_id, exact_fingerprint, exact_timeframe),
                    )
                )
            next_alerts = {
                key: alert
                for key, alert in self._alerts.items()
                if not (
                    alert.get("instrument_id") == exact_instrument_id
                    and alert.get("route_fingerprint") == exact_fingerprint
                    and alert.get("timeframe") == exact_timeframe
                )
            }
            changed = next_alerts != self._alerts
            if changed:
                self._alerts = next_alerts
            waiters = self._changed_locked() if changed else []
            revision = self._revision
        self._wake(waiters)
        if changed:
            increment_metric("price_alert_runtime_publications_total", operation="remove_scope")
        return revision

    def snapshot(self) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [self._wire(alert) for alert in self._alerts.values()]
            revision = self._revision
        rows.sort(
            key=lambda item: (
                item["instrument_id"],
                item["route_fingerprint"],
                item["timeframe"],
                item["id"],
            )
        )
        return rows, revision

    async def wait_for_revision(
        self,
        after_revision: int,
        *,
        timeout: float | None = None,
    ) -> None:
        if isinstance(after_revision, bool) or not isinstance(after_revision, int):
            raise TypeError("alert runtime revision must be an integer")
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._lock:
            if self._revision > after_revision:
                return
            self._revision_waiters.append(waiter)
        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=max(float(timeout), 0.001))
        except TimeoutError:
            return
        finally:
            with self._lock:
                if waiter in self._revision_waiters:
                    self._revision_waiters.remove(waiter)

    def active_snapshot(self) -> list[dict[str, Any]]:
        rows, _revision = self.snapshot()
        return [row for row in rows if row["enabled"] is True and row["armed"] is True]

    def active_route_snapshot(
        self,
        instrument_id: str,
        route_fingerprint: str,
    ) -> list[dict[str, Any]]:
        exact_identity = (
            require_exact_identity_text(instrument_id, field="PRICE_ALERT_INSTRUMENT_ID"),
            require_exact_identity_text(
                route_fingerprint,
                field="PRICE_ALERT_ROUTE_FINGERPRINT",
            ),
        )
        with self._lock:
            rows = [
                self._wire(alert)
                for alert in self._alerts.values()
                if (alert.get("instrument_id"), alert.get("route_fingerprint")) == exact_identity
                and alert["enabled"] is True
                and alert["armed"] is True
                and alert["kind"] == "price"
            ]
        rows.sort(key=lambda item: (item["timeframe"], item["id"]))
        return rows

    def pending_delivery_snapshot(self) -> list[dict[str, Any]]:
        rows, _revision = self.snapshot()
        return [row for row in rows if isinstance(row.get("telegramPendingPayload"), dict)]

    def scope_snapshot(
        self,
        *,
        route_pairs: Sequence[tuple[str, str]],
        timeframe: str,
        after_revision: int | None = None,
    ) -> tuple[list[dict[str, Any]] | None, int]:
        if after_revision is not None and (type(after_revision) is not int or after_revision < 0):
            raise ValueError("alert runtime revision must be a non-negative integer")
        exact_pairs = {
            (
                require_exact_identity_text(pair[0], field="PRICE_ALERT_INSTRUMENT_ID"),
                require_exact_identity_text(pair[1], field="PRICE_ALERT_ROUTE_FINGERPRINT"),
            )
            for pair in route_pairs
        }
        exact_timeframe = require_exact_identity_text(timeframe, field="PRICE_ALERT_TIMEFRAME")
        with self._lock:
            revision = self._revision
            if after_revision == revision:
                return None, revision
            rows = [
                self._wire(row)
                for row in self._alerts.values()
                if (row["instrument_id"], row["route_fingerprint"]) in exact_pairs
                and row["timeframe"] == exact_timeframe
            ]
        rows.sort(
            key=lambda item: (
                item["instrument_id"],
                item["route_fingerprint"],
                item["timeframe"],
                item["id"],
            )
        )
        return rows, revision

    def publish_quote_changes(
        self,
        changes: Sequence[tuple[tuple[str, str], Mapping[str, Any]]],
    ) -> int:
        waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        published = 0
        with self._lock:
            for identity, quote in changes:
                exact_identity = (
                    require_exact_identity_text(identity[0], field="PRICE_ALERT_INSTRUMENT_ID"),
                    require_exact_identity_text(identity[1], field="PRICE_ALERT_ROUTE_FINGERPRINT"),
                )
                if exact_identity not in self._active_route_counts:
                    continue
                frozen_quote = _freeze_quote_value(quote)
                if not isinstance(frozen_quote, Mapping):
                    raise TypeError("alert quote event must contain an object")
                self._quote_sequence += 1
                self._quote_events.append(
                    AlertQuoteEvent(
                        sequence=self._quote_sequence,
                        instrument_id=exact_identity[0],
                        route_fingerprint=exact_identity[1],
                        quote=frozen_quote,
                    )
                )
                published += 1
            if published:
                waiters = list(self._quote_waiters)
                self._quote_waiters.clear()
                set_metric("price_alert_quote_event_sequence", self._quote_sequence)
                set_metric("price_alert_quote_event_buffer_size", len(self._quote_events))
            sequence = self._quote_sequence
        self._wake(waiters)
        if published:
            increment_metric("price_alert_quote_events_total", published)
        return sequence

    def read_quote_events(
        self,
        after_sequence: int,
        *,
        limit: int = 256,
    ) -> tuple[list[AlertQuoteEvent], int, bool]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise TypeError("alert quote sequence must be an integer")
        with self._lock:
            ceiling = self._quote_sequence
            first = self._quote_events[0].sequence if self._quote_events else ceiling + 1
            overflowed = after_sequence < first - 1
            floor = first - 1 if overflowed else after_sequence
            events = [event for event in self._quote_events if event.sequence > floor][
                : max(int(limit), 1)
            ]
        return events, ceiling, overflowed

    async def wait_for_quote_events(
        self,
        after_sequence: int,
        *,
        timeout: float | None = None,
    ) -> None:
        events, _ceiling, overflowed = self.read_quote_events(after_sequence, limit=1)
        if events or overflowed:
            return
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._lock:
            if self._quote_sequence > after_sequence:
                return
            self._quote_waiters.append(waiter)
        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=max(float(timeout), 0.001))
        finally:
            with self._lock:
                if waiter in self._quote_waiters:
                    self._quote_waiters.remove(waiter)


price_alert_runtime = AlertRuntimeRegistry()
