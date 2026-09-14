from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from aef_terminal.data.instrument_identity import (
    provider_contract_id,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.math_utils import float_or_none

_COINBASE_QUOTE_TIMEOUT_SECONDS = 1.5
_COINBASE_QUOTE_MAX_CONCURRENCY = 4
_COINBASE_CONFIGURED_PRODUCTS: dict[str, dict[str, str]] = {
    "BTC-USD": {
        "key": "BTC",
        "name": "Bitcoin USD",
        "base_currency": "BTC",
        "quote_currency": "USD",
    },
    "ETH-USD": {
        "key": "ETH",
        "name": "Ethereum USD",
        "base_currency": "ETH",
        "quote_currency": "USD",
    },
}


def coinbase_granularity(interval: str) -> int:
    granularities = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "60m": 3600,
    }
    if interval not in granularities:
        raise ValueError(f"COINBASE_UNSUPPORTED_INTERVAL interval={interval}")
    return granularities[interval]


def configured_coinbase_product_id(provider_contract_id: str) -> str | None:
    product_id = require_exact_identity_text(
        provider_contract_id,
        field="provider_contract_id",
    )
    if product_id in _COINBASE_CONFIGURED_PRODUCTS:
        return product_id
    return None


def search_coinbase_products(query: str, limit: int = 20) -> list[dict[str, Any]]:
    product_id = str(query or "")
    spec = _COINBASE_CONFIGURED_PRODUCTS.get(product_id)
    if spec is None:
        return []
    return [_coinbase_product_payload(product_id, spec)][: max(int(limit), 1)]


def bind_coinbase_product(product_id: str) -> dict[str, Any] | None:
    product = require_exact_identity_text(
        product_id,
        field="provider_contract_id",
    )
    spec = _COINBASE_CONFIGURED_PRODUCTS.get(product)
    if spec is None:
        return None
    return _coinbase_product_payload(product, spec)


def _coinbase_product_payload(product_id: str, spec: dict[str, str]) -> dict[str, Any]:
    key = spec["key"]
    return {
        "instrument_id": f"coinbase|contract|{product_id}",
        "key": key,
        "instrument_key": key,
        "display": key,
        "name": spec["name"],
        "provider": "coinbase",
        "provider_symbol": product_id,
        "provider_contract_id": product_id,
        "con_id": None,
        "asset_class": "crypto",
        "session": {
            "provider": "coinbase",
            "calendar": "continuous_24_7",
            "family": "crypto",
            "timezone": "UTC",
        },
        "contract_identity": {
            "asset_class": "crypto",
            "provider": "coinbase",
            "provider_contract_id": product_id,
            "product_id": product_id,
            "base_currency": spec["base_currency"],
            "quote_currency": spec["quote_currency"],
        },
    }


class CoinbaseQuotePollingRuntime:
    """Event-loop-owned Coinbase HTTP polling boundary."""

    def __init__(self, *, timeout: float = _COINBASE_QUOTE_TIMEOUT_SECONDS) -> None:
        self._timeout = _positive_timeout(timeout)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fetch_active = False

    def _require_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("COINBASE_QUOTE_RUNTIME_LOOP_MISMATCH")

    async def fetch_quotes(
        self,
        instruments: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], str]:
        self._require_loop()
        if self._fetch_active:
            raise RuntimeError("COINBASE_QUOTE_RUNTIME_SINGLE_FLIGHT_REQUIRED")
        routes = _coinbase_quote_routes(instruments)
        if not routes:
            return {}, ""
        self._fetch_active = True
        try:
            return await _fetch_coinbase_live_quotes(routes, timeout=self._timeout)
        finally:
            self._fetch_active = False

    async def aclose(self) -> None:
        self._require_loop()
        if self._fetch_active:
            raise RuntimeError("COINBASE_QUOTE_RUNTIME_CLOSE_DURING_FETCH")


def _coinbase_quote_routes(
    instruments: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    if not isinstance(instruments, list):
        raise TypeError("COINBASE_QUOTE_INSTRUMENT_LIST_REQUIRED")
    routes = {
        (qualified_instrument_id(qualified), route_fingerprint(qualified)): provider_contract_id(
            qualified
        )
        for item in instruments
        for qualified in [require_provider_identity(item, provider="coinbase")]
    }
    invalid = [
        product for product in routes.values() if configured_coinbase_product_id(product) is None
    ]
    if invalid:
        raise ValueError(
            f"COINBASE_PROVIDER_CONTRACT_ID_UNKNOWN contract_id={invalid[0] or 'empty'}"
        )
    return routes


async def _fetch_coinbase_live_quotes(
    routes: dict[tuple[str, str], str],
    *,
    timeout: float,
) -> tuple[dict[str, dict[str, Any]], str]:
    admitted: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    ordered_routes = sorted(routes.items())
    for offset in range(0, len(ordered_routes), _COINBASE_QUOTE_MAX_CONCURRENCY):
        batch = ordered_routes[offset : offset + _COINBASE_QUOTE_MAX_CONCURRENCY]
        outcomes = await asyncio.gather(
            *(
                run_physical_thread_call(
                    _fetch_coinbase_ticker,
                    product,
                    timeout=timeout,
                )
                for _route_key, product in batch
            ),
            return_exceptions=True,
        )
        for (route_key, product), outcome in zip(batch, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                errors.append(f"Coinbase quote unavailable for {product}: {outcome}")
                continue
            admitted[route_key[1]] = outcome
    return admitted, "; ".join(errors)


def _fetch_coinbase_ticker(product: str, *, timeout: float) -> dict[str, Any]:
    request = Request(
        f"https://api.exchange.coinbase.com/products/{quote(product)}/ticker",
        headers={
            "User-Agent": "AEF-Python-Terminal/0.1",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Coinbase returned invalid ticker payload")
    price = float_or_none(payload.get("price"))
    if price is None or price <= 0:
        raise RuntimeError(f"Coinbase ticker has no valid price for {product}")
    ts = _coinbase_quote_ts(payload.get("time"))
    provider_ts = ts.isoformat()
    received_at = datetime.now(tz=UTC).isoformat()
    bid = float_or_none(payload.get("bid"))
    ask = float_or_none(payload.get("ask"))
    bid_ask_received_at = (
        received_at if bid is not None and ask is not None and ask >= bid else None
    )
    return {
        "price": price,
        "price_source": "last",
        "last": price,
        "bid": bid,
        "ask": ask,
        "ts": provider_ts,
        "provider_ts": provider_ts,
        "last_provider_ts": provider_ts,
        "received_at": received_at,
        "bid_ask_received_at": bid_ask_received_at,
        "time_basis": "provider_event",
        "source": "coinbase:quote-live",
        "entitlement": "public",
        "market_data_entitlement": "live",
        "is_delayed": False,
        "provider": "coinbase",
        "product": product,
        "contract": product,
        "message": "",
    }


def _coinbase_quote_ts(raw: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    except TypeError, ValueError:
        raise RuntimeError("Coinbase ticker has no valid exchange timestamp") from None


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("COINBASE_QUOTE_TIMEOUT_INVALID")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("COINBASE_QUOTE_TIMEOUT_INVALID")
    return timeout
