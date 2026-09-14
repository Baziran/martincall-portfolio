from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncContextManager, Protocol

from aef_terminal.data.adapters._tinvest.qualification import open_tinvest_services
from aef_terminal.data.adapters._tinvest.values import tinvest_quotation_value
from aef_terminal.data.instrument_identity import (
    identity_payload,
    provider_contract_id,
    provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.runtime.clock import utc_now as _utc_now


_PROVIDER = "tinvest"
_SOURCE = "tinvest:last-price-exchange"
_DEFAULT_TIMEOUT_SECONDS = 3.0


class TInvestQuoteError(RuntimeError):
    """Raised when an exchange-last-price response cannot prove an exact quote batch."""


class TInvestMarketDataService(Protocol):
    async def get_last_prices(self, request: object) -> object: ...


class TInvestQuoteServices(Protocol):
    market_data: TInvestMarketDataService


@dataclass(frozen=True, slots=True)
class _QuoteRoute:
    instrument_uid: str
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str


class TInvestQuotePollingRuntime:
    """Event-loop-owned long-lived T-Invest quote transport."""

    def __init__(
        self,
        token: object,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self._timeout = _timeout_seconds(timeout)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._services_context: AsyncContextManager[TInvestQuoteServices] | None = None
        self._services: TInvestQuoteServices | None = None
        self._fetch_active = False

    def _require_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("TINVEST_QUOTE_RUNTIME_LOOP_MISMATCH")

    async def _open_services(self) -> TInvestQuoteServices:
        if self._services is not None:
            return self._services
        context = open_tinvest_services(self._token)
        services = await context.__aenter__()
        self._services_context = context
        self._services = services
        return services

    async def fetch_quotes(
        self,
        instruments: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], str]:
        self._require_loop()
        _require_instrument_list(instruments)
        if not instruments:
            return {}, ""
        if self._fetch_active:
            raise RuntimeError("TINVEST_QUOTE_RUNTIME_SINGLE_FLIGHT_REQUIRED")
        self._fetch_active = True
        try:
            services = await self._open_services()
            return await fetch_tinvest_live_quotes(
                services,
                instruments,
                timeout=self._timeout,
            )
        except BaseException as exc:
            try:
                await self._close_services()
            except Exception as close_exc:
                exc.add_note(f"T-Invest quote transport close failed: {close_exc}")
            raise
        finally:
            self._fetch_active = False

    async def _close_services(self) -> None:
        context = self._services_context
        self._services = None
        self._services_context = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def aclose(self) -> None:
        self._require_loop()
        if self._fetch_active:
            raise RuntimeError("TINVEST_QUOTE_RUNTIME_CLOSE_DURING_FETCH")
        await self._close_services()


async def fetch_tinvest_live_quotes(
    services: TInvestQuoteServices,
    instruments: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    receipt_clock: Callable[[], datetime] | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Fetch and strictly admit exchange last prices keyed by exact route fingerprint."""

    routes = _quote_routes(instruments)
    if not routes:
        return {}, ""
    timeout_seconds = _timeout_seconds(timeout)
    market_data = getattr(services, "market_data", None)
    get_last_prices = getattr(market_data, "get_last_prices", None)
    if not callable(get_last_prices):
        raise TInvestQuoteError("TINVEST_QUOTE_SERVICE_REQUIRED")

    request_type, last_price_type = _sdk_last_price_contracts()
    exchange_price_type = getattr(last_price_type, "LAST_PRICE_EXCHANGE", None)
    if exchange_price_type is None:
        raise TInvestQuoteError("TINVEST_QUOTE_SDK_CONTRACT_INVALID")
    request = request_type(
        instrument_id=list(routes),
        last_price_type=exchange_price_type,
    )
    response = await asyncio.wait_for(
        get_last_prices(request),
        timeout=timeout_seconds,
    )
    received_at = _aware_utc(
        (receipt_clock or _utc_now)(),
        code="TINVEST_QUOTE_RECEIVED_AT_INVALID",
    )
    raw_prices = getattr(response, "last_prices", None)
    if not isinstance(raw_prices, Sequence) or isinstance(raw_prices, (str, bytes)):
        raise TInvestQuoteError("TINVEST_QUOTE_RESPONSE_INVALID")

    admitted: dict[str, dict[str, Any]] = {}
    seen_uids: set[str] = set()
    for raw_price in raw_prices:
        raw_uid = getattr(raw_price, "instrument_uid", None)
        try:
            instrument_uid = require_exact_identity_text(raw_uid, field="instrument_uid")
        except (TypeError, ValueError) as exc:
            raise TInvestQuoteError("TINVEST_QUOTE_UID_INVALID") from exc
        route = routes.get(instrument_uid)
        if route is None:
            raise TInvestQuoteError(f"TINVEST_QUOTE_UID_UNEXPECTED instrument_uid={instrument_uid}")
        if instrument_uid in seen_uids:
            raise TInvestQuoteError(f"TINVEST_QUOTE_UID_DUPLICATE instrument_uid={instrument_uid}")
        seen_uids.add(instrument_uid)

        response_price_type = getattr(raw_price, "last_price_type", None)
        if (
            type(response_price_type) is not type(exchange_price_type)
            or response_price_type != exchange_price_type
        ):
            raise TInvestQuoteError(
                f"TINVEST_QUOTE_PRICE_TYPE_INVALID instrument_uid={instrument_uid}"
            )
        try:
            price = tinvest_quotation_value(getattr(raw_price, "price", None))
        except ValueError as exc:
            raise TInvestQuoteError("TINVEST_QUOTE_QUOTATION_INVALID") from exc
        provider_time = _aware_utc(
            getattr(raw_price, "time", None),
            code="TINVEST_QUOTE_TIMESTAMP_INVALID",
        )
        provider_ts = provider_time.isoformat()
        admitted[route.route_fingerprint] = {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.route_fingerprint,
            "provider_symbol": route.provider_symbol,
            "provider_contract_id": instrument_uid,
            "instrument_uid": instrument_uid,
            "provider": _PROVIDER,
            "price": price,
            "price_source": "last",
            "last": price,
            "bid": None,
            "ask": None,
            "ts": provider_ts,
            "provider_ts": provider_ts,
            "last_provider_ts": provider_ts,
            "received_at": received_at.isoformat(),
            "bid_ask_received_at": None,
            "time_basis": "provider_event",
            "source": _SOURCE,
            "entitlement": "broker",
            "market_data_entitlement": "live",
            "is_delayed": False,
            "contract": instrument_uid,
            "message": "",
        }

    missing_uids = tuple(uid for uid in routes if uid not in seen_uids)
    warning = (
        "T-Invest exchange last price is unavailable for exact instrument UID(s): "
        + ", ".join(missing_uids)
        if missing_uids
        else ""
    )
    return admitted, warning


def _quote_routes(instruments: list[dict[str, Any]]) -> dict[str, _QuoteRoute]:
    _require_instrument_list(instruments)
    routes: dict[str, _QuoteRoute] = {}
    for instrument in instruments:
        qualified = require_provider_identity(instrument, provider=_PROVIDER)
        instrument_uid = require_exact_identity_text(
            provider_contract_id(qualified),
            field="provider_contract_id",
        )
        exact_provider_symbol = provider_symbol(qualified, _PROVIDER)
        nested_uid = identity_payload(qualified).get("instrument_uid")
        if exact_provider_symbol != instrument_uid or nested_uid != instrument_uid:
            raise TInvestQuoteError(
                f"TINVEST_QUOTE_ROUTE_UID_MISMATCH instrument_uid={instrument_uid}"
            )
        route = _QuoteRoute(
            instrument_uid=instrument_uid,
            instrument_id=qualified_instrument_id(qualified),
            route_fingerprint=route_fingerprint(qualified),
            provider_symbol=exact_provider_symbol,
        )
        prior = routes.get(instrument_uid)
        if prior is not None and prior != route:
            raise TInvestQuoteError(
                f"TINVEST_QUOTE_UID_ROUTE_CONFLICT instrument_uid={instrument_uid}"
            )
        routes[instrument_uid] = route
    return routes


def _require_instrument_list(instruments: object) -> None:
    if not isinstance(instruments, list):
        raise TypeError("TINVEST_QUOTE_INSTRUMENT_LIST_REQUIRED")


def _timeout_seconds(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("TINVEST_QUOTE_TIMEOUT_INVALID")
    timeout_seconds = float(timeout)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("TINVEST_QUOTE_TIMEOUT_INVALID")
    return timeout_seconds


def _aware_utc(value: object, *, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TInvestQuoteError(code)
    return value.astimezone(UTC)


def _sdk_last_price_contracts() -> tuple[Any, Any]:
    from t_tech.invest.grpc import GetLastPricesRequest, LastPriceType

    return GetLastPricesRequest, LastPriceType


__all__ = [
    "TInvestQuoteError",
    "TInvestQuotePollingRuntime",
    "fetch_tinvest_live_quotes",
]
