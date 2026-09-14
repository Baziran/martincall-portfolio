from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from aef_terminal.config import AppConfig

from aef_terminal.data.ibkr.cache import _BoundedCache

_DEFAULT_CONFIG = AppConfig()
_CONTRACT_CACHE_MAXSIZE = max(16, min(int(_DEFAULT_CONFIG.ibkr_contract_cache_size), 4096))
IbkrConnectionSession = Literal["history", "chart", "quote", "option_quote"]
_IBKR_CONNECTION_SESSIONS: tuple[IbkrConnectionSession, ...] = (
    "history",
    "chart",
    "quote",
    "option_quote",
)
IbkrConnectionKey = tuple[str, int, int]
IbkrGatewaySessionKey = tuple[str, int, int, bool]
_QUOTE_RECEIPT_PROJECTION_FIELDS = (
    "price",
    "price_source",
    "bid",
    "ask",
    "last",
    "close",
    "provider_ts",
    "last_provider_ts",
    "time_basis",
)


def ibkr_gateway_session_key(
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
) -> IbkrGatewaySessionKey:
    """Build the canonical identity of one physical IBKR gateway session."""

    return (host, int(port), int(client_id), bool(readonly))


@dataclass(frozen=True, slots=True)
class IbkrQuarantinedSession:
    ib: Any
    session: str
    session_key: object
    connection_key: IbkrConnectionKey | None
    owner_loop: asyncio.AbstractEventLoop | None
    owner_thread_id: int | None


class _IbkrRuntimeState:
    """Own mutable IBKR adapter state behind one object.

    Module-level exported references intentionally point at this object so stateful
    code has one mutable surface.
    """

    def __init__(self, contract_cache_size: int) -> None:
        self.async_history_session: Any = None
        self.async_history_session_key: IbkrGatewaySessionKey | None = None
        self.async_history_contract_cache = _BoundedCache(contract_cache_size)

        self.async_chart_session: Any = None
        self.async_chart_session_key: IbkrGatewaySessionKey | None = None
        self.async_chart_streams: dict[tuple[str, int, int, bool, str, str, str], object] = {}
        self.async_chart_stream_consumers: dict[
            tuple[str, int, int, bool, str, str, str],
            set[str],
        ] = {}
        self.async_chart_stream_callbacks: dict[
            tuple[str, int, int, bool, str, str, str],
            Callable[..., None],
        ] = {}
        self.async_chart_stream_routes: dict[
            tuple[str, int, int, bool, str, str, str],
            tuple[str, str, str],
        ] = {}
        self.async_chart_contract_cache = _BoundedCache(contract_cache_size)
        self.planned_chart_cancel_req_ids: dict[int, float] = {}
        self.planned_chart_cancel_lock = threading.Lock()

        self.async_quote_session: Any = None
        self.async_quote_session_key: IbkrGatewaySessionKey | None = None
        self.async_ticker_cache: dict[tuple[str, int, int, bool, str], object] = {}
        self.async_quote_ticker_routes: dict[int, dict[str, str]] = {}
        self.async_quote_contract_cache = _BoundedCache(contract_cache_size)
        self.async_option_quote_keys: set[tuple[str, int, int, bool, str]] = set()
        self.quote_snapshot_lock = threading.Lock()
        self.quote_snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self.quote_snapshot_routes: dict[tuple[str, str], str] = {}
        self.quote_snapshot_sequence = 0
        self.quote_snapshot_updates = 0
        self.quote_snapshot_evictions = 0
        self.quote_snapshot_updated_at: datetime | None = None
        self.live_quote_trade_lock = threading.Lock()
        self.live_quote_trade_sequence = 0
        self.live_quote_trade_events: deque[dict[str, Any]] = deque(maxlen=8192)
        self.live_quote_trade_signatures: dict[tuple[str, str], tuple[str, float]] = {}
        self.ticker_bbo_observation_lock = threading.Lock()
        self.ticker_bbo_observations: dict[
            int,
            tuple[datetime | None, datetime | None],
        ] = {}
        self.async_option_quote_session: Any = None
        self.async_option_quote_session_key: tuple[str, int, int, bool] | None = None
        self.async_option_ticker_cache: dict[tuple[str, int, int, bool, str], object] = {}
        self.async_option_ticker_consumers: dict[
            tuple[str, int, int, bool, str],
            set[str],
        ] = {}
        self.async_option_quote_contract_cache = _BoundedCache(contract_cache_size)
        self.async_option_board_sessions: dict[
            tuple[str, str, str],
            object,
        ] = {}
        self.async_option_market_data_sources: dict[
            tuple[str, str, str],
            object,
        ] = {}
        self.gex_request_universe_cache = _BoundedCache(contract_cache_size)

        self.last_chart_sync_at: datetime | None = None
        self.last_chart_error = ""
        self.last_history_error = ""
        self.api_errors: list[tuple[datetime, str, int | None, str, str]] = []
        self.quote_errors: list[tuple[datetime, int | None, str, str, int]] = []
        self.last_quote_error: tuple[datetime, int | None, str, str, int] | None = None
        self.last_quote_wanted_keys: set[tuple[str, int, int, bool, str]] = set()
        self.last_quote_sync_at: datetime | None = None
        self.last_hist_duration_seconds: float | None = None
        self.history_request_started_at: datetime | None = None
        self.history_last_activity_at: datetime | None = None
        self.history_request_label = ""

        self.connect_backoff: dict[tuple[str, int, int], tuple[float, str]] = {}
        self.connect_backoff_lock = threading.Lock()
        self.connection_state_lock = threading.Lock()
        self.connection_in_progress: dict[IbkrConnectionSession, bool] = {
            session: False for session in _IBKR_CONNECTION_SESSIONS
        }
        self.history_connection_generation = 0
        self.quarantined_session_lock = threading.Lock()
        self.quarantined_sessions: dict[
            int,
            IbkrQuarantinedSession,
        ] = {}

    def record_ticker_bbo_observation(
        self,
        ticker: object,
        *,
        side: Literal["bid", "ask"],
        observed_at: datetime | None,
    ) -> None:
        if side not in {"bid", "ask"}:
            raise ValueError("IBKR_BBO_SIDE_INVALID")
        if observed_at is not None:
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("IBKR_BBO_OBSERVATION_TIME_MUST_BE_AWARE")
            observed_at = observed_at.astimezone(UTC)
        ticker_key = id(ticker)
        with self.ticker_bbo_observation_lock:
            bid_at, ask_at = self.ticker_bbo_observations.get(
                ticker_key,
                (None, None),
            )
            if side == "bid":
                bid_at = observed_at
            else:
                ask_at = observed_at
            if bid_at is None and ask_at is None:
                self.ticker_bbo_observations.pop(ticker_key, None)
            else:
                self.ticker_bbo_observations[ticker_key] = (bid_at, ask_at)

    def ticker_bbo_observation(
        self,
        ticker: object,
    ) -> tuple[datetime | None, datetime | None]:
        with self.ticker_bbo_observation_lock:
            return self.ticker_bbo_observations.get(id(ticker), (None, None))

    def discard_ticker_bbo_observation(self, ticker: object) -> None:
        with self.ticker_bbo_observation_lock:
            self.ticker_bbo_observations.pop(id(ticker), None)

    def clear_ticker_bbo_observations(self) -> None:
        with self.ticker_bbo_observation_lock:
            self.ticker_bbo_observations.clear()

    def publish_quote_snapshots(
        self,
        updates: dict[tuple[str, str], dict[str, Any]],
        *,
        active_routes: dict[tuple[str, str], str] | None = None,
    ) -> int:
        with self.quote_snapshot_lock:
            changed = False
            if active_routes is not None:
                next_routes = dict(active_routes)
                stale_snapshot_identities = set(self.quote_snapshots).difference(next_routes)
                removed_route_identities = set(self.quote_snapshot_routes).difference(next_routes)
                for identity in stale_snapshot_identities:
                    self.quote_snapshots.pop(identity, None)
                evicted_identities = stale_snapshot_identities.union(removed_route_identities)
                if evicted_identities:
                    self.quote_snapshot_evictions += len(evicted_identities)
                    changed = True
                if self.quote_snapshot_routes != next_routes:
                    self.quote_snapshot_routes.clear()
                    self.quote_snapshot_routes.update(next_routes)
                    changed = True
            normalized_updates: dict[
                tuple[str, str],
                dict[str, Any],
            ] = {}
            for identity, quote in updates.items():
                next_quote = deepcopy(quote)
                previous = self.quote_snapshots.get(identity)
                if previous is not None:
                    bbo_clock_advanced = bool(
                        next_quote.get("price_source") == "bid_ask_mid"
                        and next_quote.get("bid_ask_received_at") is not None
                        and next_quote.get("bid_ask_received_at")
                        != previous.get("bid_ask_received_at")
                    )
                    if (
                        previous.get("received_at") is not None
                        and not bbo_clock_advanced
                        and all(
                            previous.get(field) == next_quote.get(field)
                            for field in _QUOTE_RECEIPT_PROJECTION_FIELDS
                        )
                    ):
                        next_quote["received_at"] = previous["received_at"]
                    if (
                        previous.get("bid_ask_received_at") is not None
                        and next_quote.get("bid_ask_received_at") is None
                    ):
                        next_quote["bid_ask_received_at"] = previous["bid_ask_received_at"]
                normalized_updates[identity] = next_quote
            changed_updates = {
                identity: quote
                for identity, quote in normalized_updates.items()
                if self.quote_snapshots.get(identity) != quote
            }
            for identity, quote in normalized_updates.items():
                provider_symbol = quote.get("provider_symbol")
                if (
                    isinstance(provider_symbol, str)
                    and provider_symbol
                    and self.quote_snapshot_routes.get(identity) != provider_symbol
                ):
                    self.quote_snapshot_routes[identity] = provider_symbol
                    changed = True
            if changed_updates:
                self.quote_snapshots.update(changed_updates)
                self.quote_snapshot_updates += len(changed_updates)
                changed = True
            if not changed:
                return self.quote_snapshot_sequence
            self.quote_snapshot_sequence += 1
            if changed_updates:
                # This is quote-data freshness, not route-metadata freshness.
                # Subscription reconciliation must never make stale prices
                # appear newly updated in runtime health.
                self.quote_snapshot_updated_at = datetime.now(tz=UTC)
            return self.quote_snapshot_sequence

    def read_quote_snapshots(
        self,
        route_identities: Sequence[tuple[str, str]],
        *,
        after_sequence: int | None = None,
    ) -> tuple[int, dict[tuple[str, str], dict[str, Any]] | None]:
        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0):
            raise ValueError("QUOTE_SNAPSHOT_SEQUENCE_INVALID")
        with self.quote_snapshot_lock:
            if after_sequence == self.quote_snapshot_sequence:
                return self.quote_snapshot_sequence, None
            return (
                self.quote_snapshot_sequence,
                {
                    identity: deepcopy(self.quote_snapshots[identity])
                    for identity in route_identities
                    if identity in self.quote_snapshots
                },
            )

    def discard_quote_snapshot(self, identity: tuple[str, str]) -> None:
        with self.quote_snapshot_lock:
            removed_quote = self.quote_snapshots.pop(identity, None)
            removed_route = self.quote_snapshot_routes.pop(identity, None)
            if removed_quote is None and removed_route is None:
                return
            self.quote_snapshot_evictions += 1
            self.quote_snapshot_sequence += 1

    def clear_quote_snapshots(self) -> None:
        with self.quote_snapshot_lock:
            removed = len(set(self.quote_snapshots).union(self.quote_snapshot_routes))
            self.quote_snapshots.clear()
            self.quote_snapshot_routes.clear()
            if removed:
                self.quote_snapshot_evictions += removed
                self.quote_snapshot_sequence += 1

    def register_option_market_data_source(self, source: object) -> None:
        key = getattr(source, "key", None)
        if (
            not isinstance(key, tuple)
            or len(key) != 3
            or any(not isinstance(value, str) or not value for value in key)
        ):
            raise ValueError("OPTION_MARKET_DATA_SOURCE_KEY_INVALID")
        current = self.async_option_market_data_sources.get(key)
        if current is not None and current is not source:
            raise RuntimeError(
                "OPTION_MARKET_DATA_SOURCE_ALREADY_REGISTERED "
                f"instrument_id={key[0]} route_fingerprint={key[1]} "
                f"source_id={key[2]}"
            )
        self.async_option_market_data_sources[key] = source

    def unregister_option_market_data_source(self, source: object) -> bool:
        key = getattr(source, "key", None)
        if self.async_option_market_data_sources.get(key) is not source:
            return False
        self.async_option_market_data_sources.pop(key, None)
        return True

    def option_market_data_source(
        self,
        instrument_id: str,
        route_key: str,
        expiry_mode: str,
    ) -> object | None:
        matches = [
            source
            for (
                source_instrument_id,
                source_route,
                _source_id,
            ), source in self.async_option_market_data_sources.items()
            if source_instrument_id == instrument_id
            and source_route == route_key
            and getattr(source, "expiry_mode", None) == expiry_mode
        ]
        if len(matches) > 1:
            raise RuntimeError(
                "OPTION_MARKET_DATA_SOURCE_AMBIGUOUS "
                f"instrument_id={instrument_id} route_fingerprint={route_key}"
            )
        return matches[0] if matches else None

    def discard_chart_stream(self, cache_key: tuple, *, cancel: bool) -> bool:
        """Detach and optionally cancel one manager-owned broker bar stream."""

        raw_bars = self.async_chart_streams.get(cache_key)
        callback = self.async_chart_stream_callbacks.get(cache_key)
        route = self.async_chart_stream_routes.get(cache_key)
        if cancel and raw_bars is not None:
            from aef_terminal.data.ibkr.session import (
                _ibkr_session_ready,
                _reset_ibkr_async_chart_session,
            )

            ib = self.async_chart_session
            if (
                ib is None
                or self.async_chart_session_key != cache_key[:4]
                or not _ibkr_session_ready(ib)
            ):
                _LOGGER.warning(
                    "IBKR chart stream cancellation required session reset for %s",
                    cache_key[4:],
                )
                _reset_ibkr_async_chart_session()
                return True
            try:
                self.mark_planned_chart_cancel(raw_bars)
                ib.cancelHistoricalData(raw_bars)
            except Exception:
                _LOGGER.warning(
                    "IBKR chart stream cancellation failed for %s; resetting session",
                    cache_key[4:],
                    exc_info=True,
                )
                _reset_ibkr_async_chart_session()
                return True

        self.async_chart_streams.pop(cache_key, None)
        self.async_chart_stream_callbacks.pop(cache_key, None)
        self.async_chart_stream_routes.pop(cache_key, None)
        self.async_chart_stream_consumers.pop(cache_key, None)
        if raw_bars is not None and callback is not None:
            try:
                raw_bars.updateEvent -= callback
            except Exception:
                _LOGGER.debug(
                    "IBKR chart stream callback detach failed for %s",
                    cache_key[4:],
                    exc_info=True,
                )
        if route is not None:
            from aef_terminal.runtime.chart_events import clear_live_chart_bars

            instrument_id, route_fingerprint, interval = route
            clear_live_chart_bars(
                interval,
                route_fingerprint,
                instrument_id=instrument_id,
            )
        return raw_bars is not None

    def reset_connection_backoff(self) -> None:
        with self.connect_backoff_lock:
            self.connect_backoff.clear()

    def set_connection_in_progress(
        self,
        session: IbkrConnectionSession,
        active: bool,
    ) -> None:
        with self.connection_state_lock:
            if session not in self.connection_in_progress:
                raise ValueError(f"IBKR_CONNECTION_SESSION_INVALID session={session}")
            self.connection_in_progress[session] = bool(active)

    def connection_in_progress_snapshot(self) -> dict[str, bool]:
        with self.connection_state_lock:
            return dict(self.connection_in_progress)

    def advance_history_connection_generation(self) -> int:
        with self.connection_state_lock:
            self.history_connection_generation += 1
            return self.history_connection_generation

    def history_connection_generation_snapshot(self) -> int:
        with self.connection_state_lock:
            return self.history_connection_generation

    def retain_quarantined_session(
        self,
        ib: Any,
        *,
        session: str,
        session_key: object,
        connection_key: IbkrConnectionKey | None,
        owner_loop: asyncio.AbstractEventLoop | None,
        owner_thread_id: int | None,
    ) -> None:
        with self.quarantined_session_lock:
            self.quarantined_sessions[id(ib)] = IbkrQuarantinedSession(
                ib=ib,
                session=session,
                session_key=session_key,
                connection_key=connection_key,
                owner_loop=owner_loop,
                owner_thread_id=owner_thread_id,
            )

    def release_quarantined_session(self, ib: Any) -> None:
        with self.quarantined_session_lock:
            self.quarantined_sessions.pop(id(ib), None)

    def is_session_quarantined(self, ib: Any) -> bool:
        with self.quarantined_session_lock:
            return id(ib) in self.quarantined_sessions

    def quarantined_sessions_snapshot(
        self,
    ) -> tuple[IbkrQuarantinedSession, ...]:
        with self.quarantined_session_lock:
            return tuple(self.quarantined_sessions.values())

    def quarantined_sessions_for_connection(
        self,
        connection_key: IbkrConnectionKey,
    ) -> tuple[IbkrQuarantinedSession, ...]:
        with self.quarantined_session_lock:
            return tuple(
                entry
                for entry in self.quarantined_sessions.values()
                if entry.connection_key == connection_key
            )

    def quarantined_session_count(self) -> int:
        with self.quarantined_session_lock:
            return len(self.quarantined_sessions)

    def mark_planned_chart_cancel(self, bars: Any, *, ttl_seconds: float = 10.0) -> None:
        try:
            req_id = int(getattr(bars, "reqId"))
        except AttributeError, TypeError, ValueError:
            return
        expires_at = time.monotonic() + max(float(ttl_seconds), 1.0)
        with self.planned_chart_cancel_lock:
            now = time.monotonic()
            self.planned_chart_cancel_req_ids = {
                key: expiry
                for key, expiry in self.planned_chart_cancel_req_ids.items()
                if expiry >= now
            }
            self.planned_chart_cancel_req_ids[req_id] = expires_at

    def consume_planned_chart_cancel(self, req_id: Any) -> bool:
        try:
            key = int(req_id)
        except TypeError, ValueError:
            return False
        with self.planned_chart_cancel_lock:
            now = time.monotonic()
            expires_at = self.planned_chart_cancel_req_ids.pop(key, None)
            self.planned_chart_cancel_req_ids = {
                item_key: expiry
                for item_key, expiry in self.planned_chart_cancel_req_ids.items()
                if expiry >= now
            }
        return expires_at is not None and expires_at >= now

    def clear_planned_chart_cancels(self) -> None:
        with self.planned_chart_cancel_lock:
            self.planned_chart_cancel_req_ids.clear()


_IBKR_RUNTIME = _IbkrRuntimeState(_CONTRACT_CACHE_MAXSIZE)
_IBKR_INFO_ERROR_CODES = {2104, 2106, 2107, 2108, 2119, 2158}
_IBKR_CANCEL_IGNORED_ERROR_CODES = {300}
_IBKR_CONNECT_BACKOFF_SECONDS = 4.0
_IBKR_CLIENT_ID_IN_USE_BACKOFF_SECONDS = 60.0
_LOGGER = logging.getLogger(__name__)


def _mark_history_activity() -> None:
    _IBKR_RUNTIME.history_last_activity_at = datetime.now(tz=UTC)


class _AsyncIbkrActivityWatchdog:
    """Async companion for IBKR history requests owned by the manager loop."""

    def __init__(
        self, *, label: str, idle_timeout: float, on_timeout_disconnect: Callable[[], None]
    ) -> None:
        self.label = label
        self.idle_timeout = max(float(idle_timeout), 0.1)
        self._on_timeout_disconnect = on_timeout_disconnect
        self._started_at = time.monotonic()
        self._last_activity_at = self._started_at
        self._timed_out = False
        self._task: asyncio.Task | None = None

    @property
    def timed_out(self) -> bool:
        return self._timed_out

    def beat(self, *_args) -> None:
        self._last_activity_at = time.monotonic()
        _mark_history_activity()

    def attach_ib_update_event(self, ib) -> Callable[[], None]:
        try:
            ib.updateEvent += self.beat
        except Exception as exc:
            _LOGGER.warning(
                "IBKR async history watchdog update hook registration failed for %s",
                self.label,
                exc_info=True,
            )
            raise RuntimeError("IBKR_HISTORY_WATCHDOG_UPDATE_HOOK_REQUIRED") from exc

        def detach() -> None:
            try:
                ib.updateEvent -= self.beat
            except Exception:
                _LOGGER.debug(
                    "IBKR async history watchdog update hook detach failed for %s",
                    self.label,
                    exc_info=True,
                )

        return detach

    def start(self) -> None:
        self.beat()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="ibkr-history-watchdog")

    async def cancel(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        interval = min(max(self.idle_timeout / 4.0, 0.1), 1.0)
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            silent_for = now - self._last_activity_at
            elapsed = now - self._started_at
            if silent_for < self.idle_timeout or elapsed < max(self.idle_timeout * 0.5, 0.1):
                continue
            self._timed_out = True
            _LOGGER.warning(
                "IBKR async history request idle-timeout after %.1fs of silence: %s",
                silent_for,
                self.label,
            )
            try:
                self._on_timeout_disconnect()
            except Exception:
                _LOGGER.warning(
                    "IBKR async history watchdog disconnect failed after timeout for %s",
                    self.label,
                    exc_info=True,
                )
            return
