from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Protocol

from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr.contracts import _quote_contract_for_instrument
from aef_terminal.data.ibkr.runtime import _IBKR_INFO_ERROR_CODES
from aef_terminal.data.ibkr.session import (
    _disconnect_owned_ibkr_session,
    connect_without_account_sync_async,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.instrument_identity import (
    ProviderPriceIncrementError,
    provider_symbol,
    qualified_instrument_id,
    require_provider_identity,
)
from aef_terminal.data.tick_buffer import TickBulkAggregator, TickBulkStore

_LOGGER = logging.getLogger(__name__)

TickIdentity = tuple[str, str]


class IbkrTickFeedConfigurationError(ValueError):
    """Typed terminal rejection for an invalid provider-qualified tick route."""

    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        error_code = str(code or "").strip()
        if not error_code:
            raise ValueError("IBKR_TICK_FEED_ERROR_CODE_REQUIRED")
        super().__init__(message)
        self.code = error_code


class IbkrTickAdmissionError(ValueError):
    """Typed rejection for one malformed provider tick packet."""

    retryable = False

    def __init__(self, message: str, *, code: str) -> None:
        error_code = str(code or "").strip()
        if not error_code:
            raise ValueError("IBKR_TICK_ADMISSION_ERROR_CODE_REQUIRED")
        super().__init__(message)
        self.code = error_code


class IbkrTickStore(TickBulkStore, Protocol):
    pass


class IbkrTickFeed:
    """Tick-by-tick IBKR collector for true delta and volume profile inputs."""

    def __init__(
        self,
        instruments: Sequence[dict[str, Any]],
        store: IbkrTickStore,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        readonly: bool | None = None,
        use_bidask: bool = False,
        flush_interval: float = 1.0,
        max_batch: int = 500,
        max_flush_batches: int = 20,
        max_buffer: int = 100_000,
        retention_hours: float | None = None,
        purge_interval: float = 3600.0,
    ) -> None:
        config = AppConfig()
        self.instruments: dict[TickIdentity, dict[str, Any]] = {}
        self.instrument_keys: dict[TickIdentity, str] = {}
        self.provider_symbol_by_route: dict[TickIdentity, str] = {}
        self.min_ticks: dict[TickIdentity, float] = {}
        for instrument in instruments:
            qualified = require_provider_identity(instrument, provider="ibkr")
            symbol = provider_symbol(qualified, "ibkr")
            try:
                route = route_instrument(qualified, expected_source="ibkr")
            except ProviderPriceIncrementError as exc:
                raise IbkrTickFeedConfigurationError(
                    f"IBKR_MIN_TICK_REQUIRED provider_symbol={symbol}",
                    code="IBKR_MIN_TICK_REQUIRED",
                ) from exc
            min_tick = route.price_increment
            if min_tick is None:
                raise IbkrTickFeedConfigurationError(
                    f"IBKR_MIN_TICK_REQUIRED provider_symbol={symbol}",
                    code="IBKR_MIN_TICK_REQUIRED",
                )
            identity = (qualified_instrument_id(qualified), route.fingerprint)
            self.instruments[identity] = qualified
            self.instrument_keys[identity] = route.instrument_key
            self.provider_symbol_by_route[identity] = symbol
            self.min_ticks[identity] = min_tick
        self.route_identities = list(self.instruments)
        self.route_fingerprints = [
            route_fingerprint for _, route_fingerprint in self.route_identities
        ]
        self.display_keys = list(self.instrument_keys.values())
        if not self.route_fingerprints:
            raise ValueError("at least one tick symbol is required")
        self.store = store
        self.host = host or config.ibkr_host
        self.port = int(port or config.ibkr_port)
        self.client_id = int(client_id or config.ibkr_tick_client_id)
        self.readonly = config.ibkr_readonly if readonly is None else bool(readonly)
        self.use_bidask = bool(use_bidask)
        self.aggregator = TickBulkAggregator(
            store,
            provider="ibkr",
            flush_interval=flush_interval,
            max_batch=max_batch,
            max_flush_batches=max_flush_batches,
            max_buffer=max_buffer,
            retention_hours=retention_hours,
            purge_interval=purge_interval,
        )
        self.ib: Any | None = None
        self._running = False
        self._stop_requested = False
        self._ticker_routes: dict[int, TickIdentity] = {}
        self._ticker_subscription_sequences: dict[int, int] = {}
        self._next_ticker_subscription_sequence = 0
        self._tick_subscriptions: list[tuple[Any, str, TickIdentity]] = []
        self._active_tick_routes: set[TickIdentity] = set()
        self._latest_bidask: dict[
            TickIdentity,
            tuple[datetime, float | None, float | None],
        ] = {}
        self._last_trade_price: dict[TickIdentity, float] = {}
        self._last_delta_sign: dict[TickIdentity, int] = {}
        self._rejected_ticks = 0
        self._last_admission_error = ""
        self._last_error = ""
        self._started_at: datetime | None = None

    def request_stop(self) -> None:
        self._stop_requested = True
        self._running = False

    async def run_forever(self) -> None:
        from ib_async import IB

        if self._stop_requested:
            return
        try:
            self.aggregator.start()
            self.ib = IB()
            self.ib.errorEvent += self._on_error
            await connect_without_account_sync_async(
                self.ib,
                self.host,
                self.port,
                self.client_id,
                timeout=8.0,
            )
            self.ib.pendingTickersEvent += self._on_pending_tickers
            if self._stop_requested:
                return
            self._running = True
            self._started_at = datetime.now(tz=UTC)
            self._start_tick_subscriptions(reason="start")
            while self._running:
                await asyncio.sleep(1.0)
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop_requested = True
        self._running = False
        ib = self.ib
        if ib is not None:
            self._stop_tick_subscriptions(reason="shutdown")
        failures: list[Exception] = []
        if ib is not None:
            try:
                _disconnect_owned_ibkr_session(
                    ib,
                    session="tick_feed",
                    session_key=(
                        self.host,
                        self.port,
                        self.client_id,
                        self.readonly,
                    ),
                )
            except Exception as exc:
                failures.append(exc)
                _LOGGER.warning(
                    "IBKR tick-feed session disconnect failed for %s:%s clientId %s",
                    self.host,
                    self.port,
                    self.client_id,
                    exc_info=True,
                )
        try:
            await self.aggregator.stop()
        except Exception as exc:
            failures.append(exc)
        if failures:
            raise ExceptionGroup("IBKR tick-feed shutdown failed", failures)

    def _on_pending_tickers(self, tickers: set[Any]) -> None:
        pending: list[tuple[tuple[Any, ...], TickIdentity, Any]] = []
        for ticker in tickers:
            ticker_id = id(ticker)
            identity = self._ticker_routes.get(ticker_id)
            if not identity:
                continue
            raw_ticks = list(getattr(ticker, "tickByTicks", None) or [])
            try:
                ticker.tickByTicks.clear()
            except Exception:
                _LOGGER.debug(
                    "IBKR tick packet buffer clear failed",
                    exc_info=True,
                )
            subscription_sequence = self._ticker_subscription_sequences.get(ticker_id, -1)
            for buffer_sequence, raw_tick in enumerate(raw_ticks):
                pending.append(
                    (
                        _pending_tick_order_key(
                            identity,
                            raw_tick,
                            subscription_sequence=subscription_sequence,
                            buffer_sequence=buffer_sequence,
                        ),
                        identity,
                        raw_tick,
                    )
                )

        unexpected: list[Exception] = []
        for _order_key, identity, raw_tick in sorted(pending, key=lambda item: item[0]):
            try:
                self._handle_raw_tick(identity, raw_tick)
            except IbkrTickAdmissionError as exc:
                self._rejected_ticks += 1
                self._last_admission_error = exc.code
                _LOGGER.warning(
                    "IBKR tick packet rejected: code=%s identity=%s",
                    exc.code,
                    identity,
                )
            except Exception as exc:
                unexpected.append(exc)
                _LOGGER.exception("Unexpected IBKR tick packet failure for %s", identity)
        if unexpected:
            raise ExceptionGroup("Unexpected IBKR tick packet failures", unexpected)

    def _start_tick_subscriptions(
        self,
        reason: str,
        route_identities: Sequence[TickIdentity] | None = None,
    ) -> None:
        if self.ib is None:
            return
        requested = [
            identity
            for identity in (route_identities or self.route_identities)
            if identity in self.instruments and identity not in self._active_tick_routes
        ]
        if not requested:
            return
        for identity in requested:
            try:
                contract = _quote_contract_for_instrument(
                    self.ib,
                    self.instruments[identity],
                )
                all_last_ticker = self.ib.reqTickByTickData(
                    contract,
                    "AllLast",
                    numberOfTicks=0,
                    ignoreSize=False,
                )
                self._register_ticker_route(all_last_ticker, identity)
                self._tick_subscriptions.append((contract, "AllLast", identity))
            except Exception as exc:
                self._last_error = f"IBKR tick subscribe failed for {identity}: {exc}"
                _LOGGER.warning(self._last_error)
                continue
            if self.use_bidask:
                try:
                    bidask_ticker = self.ib.reqTickByTickData(
                        contract,
                        "BidAsk",
                        numberOfTicks=0,
                        ignoreSize=True,
                    )
                    self._register_ticker_route(bidask_ticker, identity)
                    self._tick_subscriptions.append((contract, "BidAsk", identity))
                except Exception as exc:
                    self._last_error = f"IBKR tick bidask subscribe failed for {identity}: {exc}"
                    _LOGGER.warning(self._last_error)
            self._active_tick_routes.add(identity)
            _LOGGER.info(
                "IBKR tick stream subscribed: %s on %s:%s clientId=%s reason=%s",
                f"{self.instrument_keys[identity]} ({self.provider_symbol_by_route[identity]}; {identity})",
                self.host,
                self.port,
                self.client_id,
                reason,
            )

    def _stop_tick_subscriptions(self, reason: str) -> None:
        if self.ib is None:
            return
        for contract, tick_type, _symbol in list(self._tick_subscriptions):
            try:
                self.ib.cancelTickByTickData(contract, tick_type)
            except Exception as exc:
                _LOGGER.debug("IBKR cancelTickByTickData failed for %s: %s", tick_type, exc)
        if self._tick_subscriptions:
            _LOGGER.info("IBKR tick stream stopped reason=%s", reason)
        self._tick_subscriptions.clear()
        self._ticker_routes.clear()
        self._ticker_subscription_sequences.clear()
        self._active_tick_routes.clear()

    def _register_ticker_route(self, ticker: Any, identity: TickIdentity) -> None:
        ticker_id = id(ticker)
        self._ticker_routes[ticker_id] = identity
        self._ticker_subscription_sequences[ticker_id] = self._next_ticker_subscription_sequence
        self._next_ticker_subscription_sequence += 1

    def _handle_raw_tick(self, identity: TickIdentity, raw_tick: Any) -> None:
        class_name = raw_tick.__class__.__name__
        if class_name == "TickByTickBidAsk":
            ts = _aware_provider_tick_timestamp(getattr(raw_tick, "time", None))
            bid = _optional_provider_tick_price(getattr(raw_tick, "bidPrice", None), field="BID")
            ask = _optional_provider_tick_price(getattr(raw_tick, "askPrice", None), field="ASK")
            current = self._latest_bidask.get(identity)
            if current is None or ts >= current[0]:
                self._latest_bidask[identity] = (ts, bid, ask)
            return
        if class_name != "TickByTickAllLast":
            return
        price = clean_price(getattr(raw_tick, "price", None))
        if price is None:
            raise IbkrTickAdmissionError(
                "IBKR trade price must be finite",
                code="IBKR_TICK_PRICE_NOT_FINITE",
            )
        size = _exact_positive_tick_size(getattr(raw_tick, "size", None))
        ts = _aware_provider_tick_timestamp(getattr(raw_tick, "time", None))
        latest_bidask = self._latest_bidask.get(identity)
        if latest_bidask is not None and latest_bidask[0] <= ts:
            _, bid, ask = latest_bidask
        else:
            bid, ask = None, None
        delta_sign, classification_method = self._classify_delta(identity, price, bid, ask)
        attrib = getattr(raw_tick, "tickAttribLast", None)
        conditions = list(parse_conditions(getattr(raw_tick, "specialConditions", "")))
        if bool(getattr(attrib, "pastLimit", False)):
            conditions.append("pastLimit")
        if bool(getattr(attrib, "unreported", False)):
            conditions.append("unreported")
        self.aggregator.add_tick(
            ts=ts,
            route_fingerprint=identity[1],
            instrument_id=identity[0],
            price=price,
            volume=size,
            delta_sign=delta_sign,
            tick_type=f"all_last:{classification_method}",
            bid=bid,
            ask=ask,
            exchange=str(getattr(raw_tick, "exchange", "") or ""),
            conditions=tuple(conditions),
        )

    def _classify_delta(
        self,
        identity: TickIdentity,
        price: float,
        bid: float | None,
        ask: float | None,
    ) -> tuple[int, str]:
        min_tick = self.min_ticks[identity]
        eps = min_tick * 0.10
        if ask is not None and price >= ask - eps:
            sign = 1
            method = "quote_test"
        elif bid is not None and price <= bid + eps:
            sign = -1
            method = "quote_test"
        else:
            previous = self._last_trade_price.get(identity)
            if previous is None:
                sign = self._last_delta_sign.get(identity, 0)
                method = "unknown"
            elif price > previous:
                sign = 1
                method = "tick_test"
            elif price < previous:
                sign = -1
                method = "tick_test"
            else:
                sign = self._last_delta_sign.get(identity, 0)
                method = "tick_test" if sign else "unknown"
        self._last_trade_price[identity] = price
        self._last_delta_sign[identity] = sign
        return sign, method

    def _on_error(self, req_id: int, code: int, message: str, contract: Any = None) -> None:
        error_message = f"IBKR tick error {code}: {message}"
        if int(code) in _IBKR_INFO_ERROR_CODES:
            _LOGGER.debug(error_message)
            return
        self._last_error = error_message
        _LOGGER.warning(error_message)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "display_keys": self.display_keys,
            "route_fingerprints": self.route_fingerprints,
            "route_identities": [
                {"instrument_id": instrument_id, "route_fingerprint": route_fingerprint}
                for instrument_id, route_fingerprint in self.route_identities
            ],
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "use_bidask": self.use_bidask,
            "started_at": self._started_at.isoformat() if self._started_at else "",
            "tick_subscriptions": len(self._tick_subscriptions),
            "rejected_ticks": self._rejected_ticks,
            "last_admission_error": self._last_admission_error,
            "last_error": self._last_error,
            "buffer": self.aggregator.status(),
        }


def clean_price(value: Any) -> float | None:
    try:
        price = float(value)
    except TypeError, ValueError:
        return None
    if not isfinite(price):
        return None
    return price


def _aware_provider_tick_timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IbkrTickAdmissionError(
            "IBKR tick timestamp must be timezone-aware",
            code="IBKR_TICK_TIMESTAMP_NOT_AWARE",
        )
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(UTC)
    except Exception as exc:
        raise IbkrTickAdmissionError(
            "IBKR tick timestamp must be a valid timezone-aware datetime",
            code="IBKR_TICK_TIMESTAMP_INVALID",
        ) from exc
    if offset is None:
        raise IbkrTickAdmissionError(
            "IBKR tick timestamp must be timezone-aware",
            code="IBKR_TICK_TIMESTAMP_NOT_AWARE",
        )
    return normalized


def _optional_provider_tick_price(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    price = clean_price(value)
    if price is None:
        raise IbkrTickAdmissionError(
            f"IBKR tick {field.lower()} must be finite when present",
            code=f"IBKR_TICK_{field}_NOT_FINITE",
        )
    return price


def _exact_positive_tick_size(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise IbkrTickAdmissionError(
            "IBKR trade size must be an exact positive integer",
            code="IBKR_TICK_VOLUME_NOT_EXACT_INT",
        )
    try:
        exact = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IbkrTickAdmissionError(
            "IBKR trade size must be an exact positive integer",
            code="IBKR_TICK_VOLUME_NOT_EXACT_INT",
        ) from exc
    if not exact.is_finite() or exact != exact.to_integral_value():
        raise IbkrTickAdmissionError(
            "IBKR trade size must be an exact positive integer",
            code="IBKR_TICK_VOLUME_NOT_EXACT_INT",
        )
    size = int(exact)
    if size <= 0:
        raise IbkrTickAdmissionError(
            "IBKR trade size must be positive",
            code="IBKR_TICK_VOLUME_NOT_POSITIVE",
        )
    return size


def _pending_tick_order_key(
    identity: TickIdentity,
    raw_tick: Any,
    *,
    subscription_sequence: int,
    buffer_sequence: int,
) -> tuple[Any, ...]:
    raw_ts = getattr(raw_tick, "time", None)
    try:
        timestamp_valid = (
            isinstance(raw_ts, datetime)
            and raw_ts.tzinfo is not None
            and raw_ts.utcoffset() is not None
        )
        ordered_ts = raw_ts.astimezone(UTC) if timestamp_valid else datetime.max.replace(tzinfo=UTC)
    except Exception:
        timestamp_valid = False
        ordered_ts = datetime.max.replace(tzinfo=UTC)
    class_name = raw_tick.__class__.__name__
    kind_rank = {
        "TickByTickBidAsk": 0,
        "TickByTickAllLast": 1,
    }.get(class_name, 2)
    return (
        0 if timestamp_valid else 1,
        ordered_ts,
        identity,
        kind_rank,
        subscription_sequence,
        buffer_sequence,
    )


def parse_conditions(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.replace(",", ";").split(";") if part.strip())
