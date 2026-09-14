from __future__ import annotations
import asyncio
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.data.ibkr.market_data import request_ibkr_market_data_ticker
from aef_terminal.data.ibkr.option_contracts import (
    IbkrOptionContract,
    IbkrOptionContractRequest,
    IbkrOptionMarketDataSource,
    IbkrOptionSeriesExpiryFact,
    IbkrOptionUniverseUnavailableError,
    partition_ibkr_option_contracts_by_expiry,
    read_persisted_ibkr_option_expiry_facts,
    require_ibkr_option_expiry_facts,
)
from aef_terminal.data.instrument_identity import (
    instrument_is_futures,
    parse_exact_positive_decimal_provider_id,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import require_option_expiry_mode
from aef_terminal.data.gex.option_target_contract import require_option_target_dte
from aef_terminal.data.ibkr.event_loop import _ensure_event_loop
from aef_terminal.data.ibkr.quotes import (
    _quote_from_ticker,
    _quote_has_value,
    _recent_quote_error_message,
)
from aef_terminal.data.ibkr.runtime import _IBKR_RUNTIME
from aef_terminal.data.ibkr.session import (
    _connected_option_quote_ib_async,
    _ibkr_session_ready,
    _reset_ibkr_async_option_quote_session,
)
from aef_terminal.data.ibkr.utils import _safe_cancel_mkt_data


OPTION_BOARD_STRIKE_COUNT = 17
OPTION_BOARD_MAX_CONTRACTS = 68
OPTION_BOARD_INITIAL_WAIT_SECONDS = 0.35
OPTION_BOARD_CURRENT_SPOT_MAX_AGE_SECONDS = 10.0
OPTION_POINT_QUOTE_CONSUMER_ID = "option-point"
OptionSubscriptionCacheKey = tuple[str, int, int, bool, str]
OptionBoardSessionKey = tuple[str, str, str]


@dataclass
class IbkrOptionBoardSubscription:
    key: OptionBoardSessionKey
    consumer_id: str
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    expiry_mode: str
    spot: float
    contracts: tuple[IbkrOptionContract, ...]
    cache_keys: tuple[OptionSubscriptionCacheKey, ...]
    chain_meta: dict[str, Any]
    opened_subscription_count: int
    generation: int = 1
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


def _require_option_model_input(value: object, *, field: str, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}_INVALID")
    try:
        exact = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field}_INVALID") from exc
    if not math.isfinite(exact) or not 0 <= exact <= upper:
        raise ValueError(f"{field}_INVALID")
    return exact


def option_contract_universe(
    instrument: dict[str, Any],
    *,
    spot: float,
    dte: str,
    fixed_contract: dict[str, Any] | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Qualify an exact option ladder without requesting option market data."""

    qualified = require_provider_identity(instrument, provider="ibkr")
    if isinstance(spot, bool) or not isinstance(spot, (int, float)):
        raise ValueError("OPTION_CHAIN_SPOT_INVALID")
    exact_spot = float(spot)
    if not math.isfinite(exact_spot) or (not instrument_is_futures(qualified) and exact_spot <= 0):
        raise ValueError("OPTION_CHAIN_SPOT_INVALID")
    try:
        dte = require_option_target_dte(dte)
    except ValueError as exc:
        raise ValueError("OPTION_CHAIN_DTE_INVALID") from exc
    config = AppConfig()
    theoretical_iv = _require_option_model_input(
        config.option_target_theoretical_iv,
        field="OPTION_TARGET_THEORETICAL_IV",
        upper=5.0,
    )
    if theoretical_iv <= 0:
        raise ValueError("OPTION_TARGET_THEORETICAL_IV_INVALID")
    risk_free_rate = _require_option_model_input(
        config.option_target_risk_free_rate,
        field="OPTION_TARGET_RISK_FREE_RATE",
        upper=1.0,
    )
    dividend_yield = _require_option_model_input(
        config.option_target_dividend_yield,
        field="OPTION_TARGET_DIVIDEND_YIELD",
        upper=1.0,
    )
    if fixed_contract is not None:
        if not isinstance(fixed_contract, dict):
            raise TypeError("OPTION_FIXED_CONTRACT_REQUIRED")
        con_id = parse_exact_positive_decimal_provider_id(fixed_contract.get("con_id"))
        sec_type = fixed_contract.get("sec_type")
        strike = fixed_contract.get("strike")
        expiry = fixed_contract.get("expiry")
        expiry_at = fixed_contract.get("expiry_at")
        right = fixed_contract.get("right")
        exchange = require_exact_identity_text(
            fixed_contract.get("exchange"),
            field="OPTION_PROVIDER_EXCHANGE",
        )
        trading_class = require_exact_identity_text(
            fixed_contract.get("trading_class"),
            field="OPTION_PROVIDER_TRADING_CLASS",
        )
        currency = require_exact_identity_text(
            fixed_contract.get("currency"),
            field="OPTION_PROVIDER_CURRENCY",
        )
        multiplier = fixed_contract.get("multiplier")
        if (
            con_id <= 0
            or isinstance(strike, bool)
            or not isinstance(strike, (int, float))
            or not math.isfinite(float(strike))
            or float(strike) <= 0
            or not isinstance(expiry, str)
            or len(expiry) != 8
            or not expiry.isdigit()
            or right not in {"C", "P"}
            or isinstance(multiplier, bool)
            or not isinstance(multiplier, (int, float))
            or not math.isfinite(float(multiplier))
            or float(multiplier) <= 0
        ):
            raise ValueError("OPTION_FIXED_CONTRACT_FACTS_INVALID")
        if (
            not isinstance(expiry_at, str)
            or not expiry_at
            or datetime.fromisoformat(expiry_at.replace("Z", "+00:00")).tzinfo is None
        ):
            raise ValueError("OPTION_FIXED_CONTRACT_EXPIRY_TIME_INVALID")
        exact_expiry_at = datetime.fromisoformat(expiry_at.replace("Z", "+00:00")).astimezone(UTC)
        qualification_now = datetime.now(tz=UTC)
        if exact_expiry_at <= qualification_now:
            raise IbkrOptionUniverseUnavailableError(
                "The selected exact provider option contract has expired",
                reason=("NO_ACTIVE_0DTE" if dte == "0dte" else "NO_ACTIVE_EXPIRY"),
                diagnostics={
                    "requested_expiries": [expiry],
                    "expired_contracts_excluded": 1,
                    "expired_series_excluded": 1,
                },
            )
        futures_options = instrument_is_futures(qualified)
        expected_sec_type = "FOP" if futures_options else "OPT"
        if sec_type != expected_sec_type:
            raise ValueError("OPTION_FIXED_CONTRACT_SEC_TYPE_ROUTE_MISMATCH")
        qualified_at = qualification_now.isoformat()
        return {
            "source": "selected_provider_qualified_option_contract",
            "qualified_at": qualified_at,
            "instrument_id": qualified_instrument_id(qualified),
            "route_fingerprint": route_fingerprint(qualified),
            "contracts": [
                {
                    "con_id": con_id,
                    "sec_type": sec_type,
                    "expiry": expiry,
                    "expiry_at": expiry_at,
                    "strike": float(strike),
                    "right": right,
                    "multiplier": float(multiplier),
                    "trading_class": trading_class,
                    "exchange": exchange,
                    "currency": currency,
                    "local_symbol": str(fixed_contract.get("local_symbol") or ""),
                }
            ],
            "meta": {
                "spot": exact_spot,
                "futures_options": futures_options,
                "theoretical_iv": theoretical_iv,
                "risk_free_rate": risk_free_rate,
                "dividend_yield": 0.0 if futures_options else dividend_yield,
                "market_data_requested": False,
                "pricing_basis": "theoretical_chain",
                "qualified_contracts": 1,
            },
        }
    option_expiry_facts = read_persisted_ibkr_option_expiry_facts(
        instrument_id=qualified_instrument_id(qualified),
        route_fingerprint=route_fingerprint(qualified),
        store=store,
    )
    request_timeout = 15.0
    return ibkr_market_data_manager.run_coroutine_blocking(
        "options",
        f"option chain {qualified_instrument_id(qualified)} {dte}",
        lambda: _qualified_option_contract_universe_async(
            qualified,
            spot=exact_spot,
            dte=dte,
            host=config.ibkr_host,
            port=config.ibkr_port,
            client_id=config.ibkr_option_client_id,
            readonly=config.ibkr_readonly,
            timeout=request_timeout,
            theoretical_iv=theoretical_iv,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            option_expiry_facts=option_expiry_facts,
        ),
        timeout=request_timeout + 3.0,
    )


async def _qualified_option_contract_universe_async(
    instrument: dict[str, Any],
    *,
    spot: float,
    dte: str,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    timeout: float,
    theoretical_iv: float,
    risk_free_rate: float,
    dividend_yield: float,
    option_expiry_facts: tuple[IbkrOptionSeriesExpiryFact, ...],
) -> dict[str, Any]:
    import aef_terminal.data.ibkr.option_acquisition as option_acquisition

    ib = await _connected_option_quote_ib_async(
        host,
        port,
        client_id,
        readonly,
        timeout,
    )
    underlying = await option_acquisition._qualified_live_underlying(
        ib,
        instrument=instrument,
    )
    request = IbkrOptionContractRequest(
        route_fingerprint=route_fingerprint(instrument),
        futures_options=instrument_is_futures(instrument),
        strike_count=17,
        max_contracts=68,
        max_expirations=1,
        expiry_mode=dte,
        option_expiry_facts=option_expiry_facts,
    )
    contracts, chain_meta = await option_acquisition._build_live_option_contracts(
        ib,
        require_exact_identity_text(
            getattr(underlying, "symbol", None),
            field="IBKR_UNDERLYING_SYMBOL",
        ),
        underlying,
        spot,
        request,
    )
    qualified_at = datetime.now(tz=UTC).isoformat()
    futures_options = instrument_is_futures(instrument)
    return {
        "source": "ibkr_qualified_option_chain",
        "qualified_at": qualified_at,
        "instrument_id": qualified_instrument_id(instrument),
        "route_fingerprint": route_fingerprint(instrument),
        "contracts": [
            {
                "con_id": contract.con_id,
                "sec_type": contract.sec_type,
                "expiry": contract.expiry,
                "expiry_at": (
                    contract.expiry_at.astimezone(UTC).isoformat()
                    if contract.expiry_at is not None
                    else None
                ),
                "strike": contract.strike,
                "right": contract.right.value,
                "multiplier": contract.multiplier,
                "trading_class": contract.trading_class,
                "exchange": contract.exchange,
                "currency": contract.currency,
                "local_symbol": contract.local_symbol,
            }
            for contract in contracts
        ],
        "meta": {
            **dict(chain_meta),
            "spot": spot,
            "futures_options": futures_options,
            "theoretical_iv": theoretical_iv,
            "risk_free_rate": risk_free_rate,
            "dividend_yield": 0.0 if futures_options else dividend_yield,
            "market_data_requested": False,
            "pricing_basis": "theoretical_chain",
        },
    }


def _require_option_connection_values(
    host: object,
    port: object,
    client_id: object,
    readonly: object,
) -> tuple[str, int, int, bool]:
    exact_host = require_exact_identity_text(host, field="OPTION_PROVIDER_HOST")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("OPTION_PROVIDER_PORT_INVALID")
    if type(client_id) is not int or client_id < 0:
        raise ValueError("OPTION_PROVIDER_CLIENT_ID_INVALID")
    if type(readonly) is not bool:
        raise ValueError("OPTION_PROVIDER_READONLY_INVALID")
    return exact_host, port, client_id, readonly


def _require_option_quote_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("OPTION_QUOTE_TIMEOUT_INVALID")
    try:
        exact_timeout = float(timeout)
    except OverflowError as exc:
        raise ValueError("OPTION_QUOTE_TIMEOUT_INVALID") from exc
    if not math.isfinite(exact_timeout) or exact_timeout <= 0:
        raise ValueError("OPTION_QUOTE_TIMEOUT_INVALID")
    return exact_timeout


def _option_quote_contract(payload: dict[str, Any]):
    if not isinstance(payload, dict):
        raise TypeError("OPTION_PROVIDER_CONTRACT_REQUIRED")
    _ensure_event_loop()
    from ib_async import Contract

    con_id = parse_exact_positive_decimal_provider_id(payload.get("con_id"))
    if con_id <= 0:
        raise ValueError("OPTION_PROVIDER_CONTRACT_ID_REQUIRED")
    exchange = require_exact_identity_text(
        payload.get("exchange"),
        field="OPTION_PROVIDER_EXCHANGE",
    )
    sec_type = payload.get("sec_type")
    if sec_type not in {"OPT", "FOP"}:
        raise ValueError("OPTION_PROVIDER_SEC_TYPE_REQUIRED")
    return Contract(conId=con_id, exchange=exchange, secType=sec_type)


def _option_quote_contract_key(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise TypeError("OPTION_PROVIDER_CONTRACT_REQUIRED")
    con_id = parse_exact_positive_decimal_provider_id(payload.get("con_id"))
    if con_id <= 0:
        raise ValueError("OPTION_PROVIDER_CONTRACT_ID_REQUIRED")
    exchange = require_exact_identity_text(
        payload.get("exchange"),
        field="OPTION_PROVIDER_EXCHANGE",
    )
    sec_type = payload.get("sec_type")
    if sec_type not in {"OPT", "FOP"}:
        raise ValueError("OPTION_PROVIDER_SEC_TYPE_REQUIRED")
    return json.dumps([con_id, exchange, sec_type], separators=(",", ":"))


def _option_subscription_cache_key(
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    contract_key: str,
) -> tuple[str, int, int, bool, str]:
    exact_host, exact_port, exact_client_id, exact_readonly = _require_option_connection_values(
        host, port, client_id, readonly
    )
    exact_contract_key = require_exact_identity_text(
        contract_key,
        field="OPTION_CONTRACT_KEY",
    )
    return (
        exact_host,
        exact_port,
        exact_client_id,
        exact_readonly,
        exact_contract_key,
    )


def _require_option_board_spot(value: object, *, futures_options: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("OPTION_BOARD_SPOT_INVALID")
    spot = float(value)
    if not math.isfinite(spot) or (not futures_options and spot <= 0):
        raise ValueError("OPTION_BOARD_SPOT_INVALID")
    return spot


def _option_board_spot_reference_state(
    quote: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    if quote.get("market_data_entitlement") != "live":
        return "reference"
    source = quote.get("price_source")
    observed_text = (
        quote.get("bid_ask_received_at")
        if source == "bid_ask_mid"
        else quote.get("last_provider_ts")
        if source == "last"
        else None
    )
    try:
        observed_at = datetime.fromisoformat(str(observed_text).replace("Z", "+00:00"))
    except ValueError:
        return "reference"
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return "reference"
    current = (now or datetime.now(tz=UTC)).astimezone(UTC)
    age_seconds = (current - observed_at.astimezone(UTC)).total_seconds()
    return (
        "current"
        if -1.0 <= age_seconds <= OPTION_BOARD_CURRENT_SPOT_MAX_AGE_SECONDS
        else "reference"
    )


async def _option_board_provider_spot_reference(
    ib: Any,
    underlying: Any,
    *,
    futures_options: bool,
    timeout: float,
) -> tuple[float, str, str, str]:
    """Load one bounded exact-underlying reference without retaining a subscription."""

    ticker = request_ibkr_market_data_ticker(
        ib,
        underlying,
        "",
        True,
        False,
    )
    reference: tuple[float, str, str, str] | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.5, min(float(timeout), 3.0))
    try:
        while loop.time() < deadline:
            quote = _quote_from_ticker(ticker)
            try:
                spot = _require_option_board_spot(
                    quote.get("price"),
                    futures_options=futures_options,
                )
            except ValueError:
                await asyncio.sleep(0.1)
                continue
            reference = (
                spot,
                str(quote.get("price_source") or "unavailable"),
                str(quote.get("market_data_entitlement") or "unknown"),
                _option_board_spot_reference_state(quote),
            )
            break
    finally:
        if not _safe_cancel_mkt_data(ib, ticker, underlying):
            _reset_ibkr_async_option_quote_session()
            raise RuntimeError(
                "IBKR Option Board underlying reference subscription could not be released"
            )
    if reference is None:
        raise RuntimeError(
            "IBKR returned no exact underlying current or previous-close reference for Option Board"
        )
    return reference


def _require_option_quote_consumer_id(value: object) -> str:
    return require_exact_identity_text(value, field="OPTION_QUOTE_CONSUMER_ID")


def _option_board_session_key(
    instrument: dict[str, Any],
    consumer_id: str,
) -> OptionBoardSessionKey:
    return (
        qualified_instrument_id(instrument),
        route_fingerprint(instrument),
        _require_option_quote_consumer_id(consumer_id),
    )


def _option_contract_quote_payload(contract: IbkrOptionContract) -> dict[str, Any]:
    return {
        "con_id": contract.con_id,
        "sec_type": contract.sec_type,
        "expiry": contract.expiry,
        "expiry_at": (
            contract.expiry_at.astimezone(UTC).isoformat()
            if contract.expiry_at is not None
            else None
        ),
        "strike": contract.strike,
        "right": contract.right.value,
        "multiplier": contract.multiplier,
        "trading_class": contract.trading_class,
        "exchange": contract.exchange,
        "currency": contract.currency,
        "local_symbol": contract.local_symbol,
        "market_rule_id": contract.market_rule_id,
        "minimum_tick": contract.minimum_tick,
        "price_increments": [increment.to_payload() for increment in contract.price_increments],
    }


def _register_option_ticker_consumer(
    cache_key: OptionSubscriptionCacheKey,
    consumer_id: str,
) -> None:
    exact_consumer = _require_option_quote_consumer_id(consumer_id)
    _IBKR_RUNTIME.async_option_ticker_consumers.setdefault(
        cache_key,
        set(),
    ).add(exact_consumer)


def _release_option_ticker_consumer_key(
    cache_key: OptionSubscriptionCacheKey,
    consumer_id: str,
) -> bool:
    exact_consumer = _require_option_quote_consumer_id(consumer_id)
    consumers = _IBKR_RUNTIME.async_option_ticker_consumers.get(cache_key)
    if consumers is None:
        return _cancel_option_quote_key(cache_key)
    if exact_consumer not in consumers:
        return False
    consumers.remove(exact_consumer)
    if consumers:
        return False
    _IBKR_RUNTIME.async_option_ticker_consumers.pop(cache_key, None)
    return _cancel_option_quote_key(cache_key)


def live_option_quote(
    payload: dict[str, Any],
    timeout: float = 1.4,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    consumer_id: str = OPTION_POINT_QUOTE_CONSUMER_ID,
) -> dict[str, float | str | None]:
    """Return bid/ask/last for one option contract using the option quote session."""
    if not isinstance(payload, dict):
        raise TypeError("OPTION_PROVIDER_CONTRACT_REQUIRED")
    config = AppConfig()
    resolved_host, resolved_port, resolved_client_id, resolved_readonly = (
        _require_option_connection_values(
            config.ibkr_host if host is None else host,
            config.ibkr_port if port is None else port,
            config.ibkr_option_client_id if client_id is None else client_id,
            config.ibkr_readonly if readonly is None else readonly,
        )
    )
    resolved_timeout = _require_option_quote_timeout(timeout)
    exact_consumer = _require_option_quote_consumer_id(consumer_id)
    contract_key = _option_quote_contract_key(payload)
    return ibkr_market_data_manager.run_coroutine_blocking(
        "options",
        f"option quote {contract_key[:32]}",
        lambda: _live_option_quote_async_owned(
            payload,
            contract_key,
            resolved_timeout,
            resolved_host,
            resolved_port,
            resolved_client_id,
            resolved_readonly,
            exact_consumer,
        ),
        timeout=resolved_timeout + 3.0,
    )


def cancel_option_quote(
    payload: dict[str, Any],
    *,
    timeout: float = 1.0,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    consumer_id: str = OPTION_POINT_QUOTE_CONSUMER_ID,
) -> dict[str, Any]:
    """Cancel a cached live option quote subscription without opening a socket."""
    if not isinstance(payload, dict):
        raise TypeError("OPTION_PROVIDER_CONTRACT_REQUIRED")
    config = AppConfig()
    resolved_host, resolved_port, resolved_client_id, resolved_readonly = (
        _require_option_connection_values(
            config.ibkr_host if host is None else host,
            config.ibkr_port if port is None else port,
            config.ibkr_option_client_id if client_id is None else client_id,
            config.ibkr_readonly if readonly is None else readonly,
        )
    )
    resolved_timeout = _require_option_quote_timeout(timeout)
    exact_consumer = _require_option_quote_consumer_id(consumer_id)
    contract_key = _option_quote_contract_key(payload)
    cache_key = _option_subscription_cache_key(
        resolved_host,
        resolved_port,
        resolved_client_id,
        resolved_readonly,
        contract_key,
    )
    async_cancelled = ibkr_market_data_manager.run_coroutine_blocking(
        "options",
        f"cancel option quote {contract_key[:32]}",
        lambda: _cancel_option_quote_async_owned(
            cache_key,
            exact_consumer,
        ),
        timeout=resolved_timeout,
        apply_pacing=False,
    )
    return {"ok": True, "cancelled": async_cancelled, "contract_key": contract_key}


def reconcile_option_point_quote_consumers(
    active_consumer_ids: set[str],
    *,
    timeout: float = 1.0,
) -> dict[str, Any]:
    if not isinstance(active_consumer_ids, set):
        raise TypeError("OPTION_POINT_CONSUMER_SET_REQUIRED")
    exact_consumers = {_require_option_quote_consumer_id(value) for value in active_consumer_ids}
    resolved_timeout = _require_option_quote_timeout(timeout)
    return ibkr_market_data_manager.run_coroutine_blocking(
        "options",
        "reconcile option point quote consumers",
        lambda: _reconcile_option_point_quote_consumers_owned(exact_consumers),
        timeout=resolved_timeout,
        apply_pacing=False,
    )


async def _reconcile_option_point_quote_consumers_owned(
    active_consumer_ids: set[str],
) -> dict[str, Any]:
    released = 0
    cancelled = 0
    runtime = _IBKR_RUNTIME
    for cache_key, consumers in tuple(runtime.async_option_ticker_consumers.items()):
        stale = tuple(
            consumer_id
            for consumer_id in tuple(consumers)
            if consumer_id.startswith("option-point:") and consumer_id not in active_consumer_ids
        )
        for consumer_id in stale:
            released += 1
            if _release_option_ticker_consumer_key(cache_key, consumer_id):
                cancelled += 1
    if cancelled:
        await asyncio.sleep(0.05)
    return {
        "ok": True,
        "active": len(active_consumer_ids),
        "released": released,
        "cancelled": cancelled,
    }


async def _cancel_option_quote_async_owned(
    cache_key: OptionSubscriptionCacheKey,
    consumer_id: str = OPTION_POINT_QUOTE_CONSUMER_ID,
) -> bool:
    cancelled = _release_option_ticker_consumer_key(cache_key, consumer_id)
    if cancelled:
        await asyncio.sleep(0.05)
    return cancelled


def _cancel_option_quote_key(cache_key: OptionSubscriptionCacheKey) -> bool:
    runtime = _IBKR_RUNTIME
    ib = runtime.async_option_quote_session
    cache = runtime.async_option_ticker_cache
    contract_cache = runtime.async_option_quote_contract_cache
    session_key = runtime.async_option_quote_session_key
    existed = cache_key in cache or cache_key in contract_cache
    if not existed:
        runtime.async_option_ticker_consumers.pop(cache_key, None)
        return False
    ticker = cache.get(cache_key)
    contract = getattr(ticker, "contract", None) if ticker is not None else None
    cached_contract = contract_cache.get(cache_key)
    contract = contract or cached_contract
    if not _ibkr_session_ready(ib):
        _reset_ibkr_async_option_quote_session()
        return True
    if (
        ticker is None
        or contract is None
        or session_key != cache_key[:4]
        or not _safe_cancel_mkt_data(ib, ticker, contract)
    ):
        _reset_ibkr_async_option_quote_session()
        raise RuntimeError(
            "IBKR option quote cancellation state is ambiguous; option quote session was reset"
        )
    cache.pop(cache_key, None)
    contract_cache.pop(cache_key, None)
    runtime.async_option_ticker_consumers.pop(cache_key, None)
    runtime.discard_ticker_bbo_observation(ticker)
    return True


async def _live_option_quote_async_owned(
    payload: dict[str, Any],
    contract_key: str,
    timeout: float,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    consumer_id: str = OPTION_POINT_QUOTE_CONSUMER_ID,
) -> dict[str, float | str | None]:
    runtime = _IBKR_RUNTIME
    cache_key = _option_subscription_cache_key(
        host,
        port,
        client_id,
        readonly,
        contract_key,
    )
    quote_error_index = len(runtime.quote_errors)
    try:
        ib = await _connected_option_quote_ib_async(host, port, client_id, readonly, timeout)
        cached_ticker = runtime.async_option_ticker_cache.get(cache_key)
        if cached_ticker is not None:
            _register_option_ticker_consumer(cache_key, consumer_id)
            await asyncio.sleep(0.03)
            quote = _quote_from_ticker(cached_ticker)
            if _quote_has_value(quote):
                return quote
        contract = runtime.async_option_quote_contract_cache.get(cache_key)
        if contract is None:
            contract = _option_quote_contract(payload)
            runtime.async_option_quote_contract_cache[cache_key] = contract
        ticker = runtime.async_option_ticker_cache.get(cache_key)
        if ticker is None:
            ticker = request_ibkr_market_data_ticker(
                ib,
                contract,
                "",
                False,
                False,
            )
            runtime.async_option_ticker_cache[cache_key] = ticker
        _register_option_ticker_consumer(cache_key, consumer_id)
        await asyncio.sleep(0)
        quote = _quote_from_ticker(ticker)
        if _quote_has_value(quote):
            return quote
        poll_attempts = math.ceil(timeout / 0.2)
        for _ in range(poll_attempts):
            await asyncio.sleep(0.2)
            quote = _quote_from_ticker(ticker)
            if _quote_has_value(quote):
                return quote
        quote = _quote_from_ticker(ticker)
        message = _recent_quote_error_message(quote_error_index)
        if message:
            quote["message"] = message
        return quote
    except Exception:
        _reset_ibkr_async_option_quote_session()
        raise


async def async_option_board_snapshot(
    instrument: dict[str, Any],
    *,
    consumer_id: str,
    spot: float | None = None,
    spot_price_source: str = "",
    spot_market_data_entitlement: str = "unknown",
    spot_reference_state: str = "reference",
    expiry_mode: str = "hybrid",
    option_expiry_facts: tuple[IbkrOptionSeriesExpiryFact, ...] = (),
) -> dict[str, Any]:
    qualified = require_provider_identity(instrument, provider="ibkr")
    exact_consumer = _require_option_quote_consumer_id(consumer_id)
    exact_spot = (
        _require_option_board_spot(
            spot,
            futures_options=instrument_is_futures(qualified),
        )
        if spot is not None
        else None
    )
    exact_expiry_mode = require_option_expiry_mode(expiry_mode)
    exact_expiry_facts = require_ibkr_option_expiry_facts(option_expiry_facts)
    config = AppConfig()
    timeout = 15.0
    return await ibkr_market_data_manager.run_coroutine(
        "options",
        f"option board {qualified_instrument_id(qualified)}",
        lambda: _option_board_snapshot_owned(
            qualified,
            consumer_id=exact_consumer,
            spot=exact_spot,
            spot_price_source=spot_price_source,
            spot_market_data_entitlement=spot_market_data_entitlement,
            spot_reference_state=spot_reference_state,
            expiry_mode=exact_expiry_mode,
            host=config.ibkr_host,
            port=config.ibkr_port,
            client_id=config.ibkr_option_client_id,
            readonly=config.ibkr_readonly,
            timeout=timeout,
            option_expiry_facts=exact_expiry_facts,
        ),
        timeout=timeout + 3.0,
        apply_pacing=False,
    )


async def async_stop_option_board(
    instrument: dict[str, Any],
    *,
    consumer_id: str,
) -> dict[str, Any]:
    qualified = require_provider_identity(instrument, provider="ibkr")
    exact_consumer = _require_option_quote_consumer_id(consumer_id)
    key = _option_board_session_key(qualified, exact_consumer)
    return await ibkr_market_data_manager.run_coroutine(
        "options",
        f"stop option board {qualified_instrument_id(qualified)}",
        lambda: _stop_option_board_owned(key),
        timeout=3.0,
        apply_pacing=False,
    )


def _option_board_requires_rebase(
    subscription: IbkrOptionBoardSubscription,
    spot: float,
    *,
    now: datetime | None = None,
) -> bool:
    _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
        subscription.contracts,
        now=now,
    )
    if unknown:
        raise IbkrOptionUniverseUnavailableError(
            "Option Board subscription is missing exact expiry metadata",
            reason="EXPIRY_TIME_UNKNOWN",
            diagnostics={
                "expiry_time_unknown_contracts_excluded": len(unknown),
            },
        )
    if expired:
        return True
    strikes = sorted({contract.strike for contract in subscription.contracts})
    if len(strikes) < 5:
        return False
    return spot <= strikes[1] or spot >= strikes[-2]


async def _ensure_option_board_subscription_owned(
    instrument: dict[str, Any],
    *,
    consumer_id: str,
    spot: float,
    expiry_mode: str,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    timeout: float,
    option_expiry_facts: tuple[IbkrOptionSeriesExpiryFact, ...] = (),
) -> IbkrOptionBoardSubscription:
    import aef_terminal.data.ibkr.option_acquisition as option_acquisition

    runtime = _IBKR_RUNTIME
    key = _option_board_session_key(instrument, consumer_id)
    current = runtime.async_option_board_sessions.get(key)
    current_healthy = bool(
        isinstance(current, IbkrOptionBoardSubscription)
        and _ibkr_session_ready(runtime.async_option_quote_session)
        and runtime.async_option_quote_session_key == (host, port, client_id, readonly)
        and all(
            cache_key in runtime.async_option_ticker_cache
            and cache_key in runtime.async_option_quote_contract_cache
            and consumer_id in runtime.async_option_ticker_consumers.get(cache_key, set())
            for cache_key in current.cache_keys
        )
    )
    if (
        isinstance(current, IbkrOptionBoardSubscription)
        and current_healthy
        and current.expiry_mode == expiry_mode
        and not _option_board_requires_rebase(current, spot)
    ):
        current.spot = spot
        return current
    generation = current.generation + 1 if isinstance(current, IbkrOptionBoardSubscription) else 1
    if isinstance(current, IbkrOptionBoardSubscription):
        if current_healthy:
            await _stop_option_board_owned(key)
        else:
            _reset_ibkr_async_option_quote_session()
    ib = await _connected_option_quote_ib_async(
        host,
        port,
        client_id,
        readonly,
        timeout,
    )
    underlying = await option_acquisition._qualified_live_underlying(
        ib,
        instrument=instrument,
    )
    request = IbkrOptionContractRequest(
        route_fingerprint=route_fingerprint(instrument),
        futures_options=instrument_is_futures(instrument),
        strike_count=OPTION_BOARD_STRIKE_COUNT,
        max_contracts=OPTION_BOARD_MAX_CONTRACTS,
        max_expirations=1,
        expiry_mode=expiry_mode,
        option_expiry_facts=option_expiry_facts,
    )
    contracts, chain_meta = await option_acquisition._build_live_option_contracts(
        ib,
        require_exact_identity_text(
            getattr(underlying, "symbol", None),
            field="IBKR_UNDERLYING_SYMBOL",
        ),
        underlying,
        spot,
        request,
    )
    if not contracts:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR returned no exact option contracts for Option Board"
        )
    contracts, price_rule_meta = await option_acquisition._attach_option_price_increments_async(
        ib,
        contracts,
    )
    chain_meta = {**dict(chain_meta), **price_rule_meta}
    cache_keys: list[OptionSubscriptionCacheKey] = []
    opened_subscription_count = 0
    try:
        for index, contract in enumerate(contracts, start=1):
            contract_payload = _option_contract_quote_payload(contract)
            contract_key = _option_quote_contract_key(contract_payload)
            cache_key = _option_subscription_cache_key(
                host,
                port,
                client_id,
                readonly,
                contract_key,
            )
            ticker = runtime.async_option_ticker_cache.get(cache_key)
            if ticker is None:
                ticker = request_ibkr_market_data_ticker(
                    ib,
                    contract.raw,
                    "",
                    False,
                    False,
                )
                runtime.async_option_ticker_cache[cache_key] = ticker
                opened_subscription_count += 1
            runtime.async_option_quote_contract_cache[cache_key] = contract.raw
            _register_option_ticker_consumer(cache_key, consumer_id)
            cache_keys.append(cache_key)
            if index % 12 == 0:
                await asyncio.sleep(0)
        await asyncio.sleep(OPTION_BOARD_INITIAL_WAIT_SECONDS)
        _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(contracts)
        if expired or unknown:
            raise IbkrOptionUniverseUnavailableError(
                "Option Board universe crossed its exact expiry during subscription bootstrap",
                reason="OPTION_UNIVERSE_ROLLOVER",
                diagnostics={
                    "expired_contracts_excluded": len(expired),
                    "expiry_time_unknown_contracts_excluded": len(unknown),
                },
            )
    except Exception, asyncio.CancelledError:
        for cache_key in cache_keys:
            _release_option_ticker_consumer_key(cache_key, consumer_id)
        raise
    subscription = IbkrOptionBoardSubscription(
        key=key,
        consumer_id=consumer_id,
        instrument_id=qualified_instrument_id(instrument),
        route_fingerprint=route_fingerprint(instrument),
        provider_symbol=require_exact_identity_text(
            getattr(underlying, "symbol", None),
            field="IBKR_UNDERLYING_SYMBOL",
        ),
        expiry_mode=expiry_mode,
        spot=spot,
        contracts=tuple(contracts),
        cache_keys=tuple(cache_keys),
        chain_meta=dict(chain_meta),
        opened_subscription_count=opened_subscription_count,
        generation=generation,
    )
    runtime.async_option_board_sessions[key] = subscription
    return subscription


def _option_board_quote_row(
    contract: IbkrOptionContract,
    ticker: Any,
) -> dict[str, Any]:
    quote = _quote_from_ticker(ticker)
    bid = quote.get("bid")
    ask = quote.get("ask")
    midpoint = (
        (float(bid) + float(ask)) / 2.0
        if isinstance(bid, (int, float))
        and not isinstance(bid, bool)
        and isinstance(ask, (int, float))
        and not isinstance(ask, bool)
        and math.isfinite(float(bid))
        and math.isfinite(float(ask))
        and float(ask) >= float(bid)
        else None
    )
    return {
        **_option_contract_quote_payload(contract),
        "price": quote.get("price"),
        "price_source": quote.get("price_source"),
        "bid": bid,
        "ask": ask,
        "mid": midpoint,
        "last": quote.get("last"),
        "close": quote.get("close"),
        "bid_size": quote.get("bid_size"),
        "ask_size": quote.get("ask_size"),
        "last_size": quote.get("last_size"),
        "ts": quote.get("ts"),
        "provider_ts": quote.get("provider_ts"),
        "last_provider_ts": quote.get("last_provider_ts"),
        "received_at": quote.get("received_at"),
        "packet_received_at": quote.get("packet_received_at"),
        "bid_received_at": quote.get("bid_received_at"),
        "ask_received_at": quote.get("ask_received_at"),
        "bid_ask_received_at": quote.get("bid_ask_received_at"),
        "time_basis": quote.get("time_basis"),
        "market_data_type": quote.get("market_data_type"),
        "market_data_entitlement": quote.get("market_data_entitlement"),
        "is_delayed": quote.get("is_delayed"),
    }


def _option_board_series(
    contracts: tuple[IbkrOptionContract, ...],
    quote_rows: list[dict[str, Any]],
    *,
    valuation_now: datetime | None = None,
) -> list[dict[str, Any]]:
    from aef_terminal.data.gex.option_target_contract import (
        option_target_dte_for_expiry,
    )

    grouped: dict[
        tuple[str, str, str, float, str],
        dict[float, dict[str, dict[str, Any]]],
    ] = defaultdict(lambda: defaultdict(dict))
    expiry_at_by_series: dict[tuple[str, str, str, float, str], str] = {}
    for contract, quote in zip(contracts, quote_rows, strict=True):
        series_key = (
            contract.expiry,
            contract.trading_class,
            contract.exchange,
            contract.multiplier,
            contract.currency,
        )
        grouped[series_key][contract.strike][contract.right.value] = quote
        expiry_at_by_series[series_key] = (
            contract.expiry_at.astimezone(UTC).isoformat() if contract.expiry_at is not None else ""
        )
    series_rows: list[dict[str, Any]] = []
    for series_key in sorted(grouped):
        expiry, trading_class, exchange, multiplier, currency = series_key
        expiry_at = expiry_at_by_series[series_key]
        target_dte = option_target_dte_for_expiry(
            expiry_at,
            valuation_now=valuation_now,
        )
        try:
            target_dte = require_option_target_dte(target_dte)
        except ValueError:
            raise IbkrOptionUniverseUnavailableError(
                "Option Board exact series has no active Option Point DTE",
                reason="NO_ACTIVE_EXPIRY",
            )
        strikes = grouped[series_key]
        rows = []
        for strike in sorted(strikes):
            pair = strikes[strike]
            if set(pair) != {"C", "P"}:
                raise RuntimeError("Option Board exact contract universe lost a Call/Put pair")
            rows.append(
                {
                    "strike": strike,
                    "call": pair["C"],
                    "put": pair["P"],
                }
            )
        series_rows.append(
            {
                "expiry": expiry,
                "expiry_at": expiry_at,
                "target_dte": target_dte,
                "trading_class": trading_class,
                "exchange": exchange,
                "multiplier": multiplier,
                "currency": currency,
                "rows": rows,
            }
        )
    return series_rows


def _option_board_payload(
    *,
    instrument_id: str,
    route_key: str,
    provider_symbol: str,
    consumer_id: str,
    generation: object,
    spot: float,
    spot_reference: dict[str, Any] | None = None,
    expiry_mode: str,
    contracts: tuple[IbkrOptionContract, ...],
    quote_rows: list[dict[str, Any]],
    chain_meta: dict[str, Any],
    physical_subscription_source: str,
    opened_subscription_count: int,
) -> dict[str, Any]:
    captured_at = datetime.now(tz=UTC)
    active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
        contracts,
        now=captured_at,
    )
    if expired or unknown or len(active) != len(contracts):
        raise IbkrOptionUniverseUnavailableError(
            "Option Board universe crossed its exact expiry before delivery",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={
                "expired_contracts_excluded": len(expired),
                "expiry_time_unknown_contracts_excluded": len(unknown),
            },
        )
    if len(contracts) != len(quote_rows):
        raise RuntimeError("Option Board contract and quote universes are inconsistent")
    available = sum(
        any(quote.get(field) is not None for field in ("bid", "ask", "last"))
        for quote in quote_rows
    )
    entitlements = {
        entitlement
        for quote in quote_rows
        if isinstance(
            entitlement := quote.get("market_data_entitlement"),
            str,
        )
        and entitlement
    }
    contract_count = len(contracts)
    status = "ready" if available == contract_count else "partial" if available else "waiting"
    return {
        "ok": True,
        "status": status,
        "source": "provider_option_board",
        "physical_subscription_source": physical_subscription_source,
        "instrument_id": instrument_id,
        "route_fingerprint": route_key,
        "provider_symbol": provider_symbol,
        "consumer_id": consumer_id,
        "generation": generation,
        "captured_at": captured_at.isoformat(),
        "spot": spot,
        "spot_reference": dict(spot_reference or {}),
        "expiry_mode": expiry_mode,
        "strike_limit": OPTION_BOARD_STRIKE_COUNT,
        "contract_budget": OPTION_BOARD_MAX_CONTRACTS,
        "contract_count": contract_count,
        "subscription_count": contract_count,
        "opened_subscription_count": opened_subscription_count,
        "reused_subscription_count": (contract_count - opened_subscription_count),
        "priced_contract_count": available,
        "market_data_entitlement": (
            next(iter(entitlements))
            if len(entitlements) == 1
            else "mixed"
            if entitlements
            else "unknown"
        ),
        "series": _option_board_series(
            contracts,
            quote_rows,
            valuation_now=captured_at,
        ),
        "chain_meta": {
            key: value
            for key, value in chain_meta.items()
            if key
            in {
                "expirations",
                "chain_labels",
                "qualified_strike_count",
                "qualified_pair_count",
                "expiry_time_source",
                "price_rule_status",
                "price_rule_contract_count",
                "price_rule_unavailable_contract_count",
                "price_rule_ids",
                "price_rule_failed_ids",
            }
        },
    }


async def _option_board_snapshot_owned(
    instrument: dict[str, Any],
    *,
    consumer_id: str,
    spot: float | None,
    expiry_mode: str,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    timeout: float,
    option_expiry_facts: tuple[IbkrOptionSeriesExpiryFact, ...] = (),
    spot_price_source: str = "",
    spot_market_data_entitlement: str = "unknown",
    spot_reference_state: str = "reference",
) -> dict[str, Any]:
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    exact_spot = spot
    exact_spot_source = str(spot_price_source or "")
    exact_spot_entitlement = str(spot_market_data_entitlement or "unknown")
    exact_spot_state = "current" if spot_reference_state == "current" else "reference"
    spot_acquisition = "canonical_quote_cache" if exact_spot is not None else "provider_snapshot"
    if exact_spot is None:
        import aef_terminal.data.ibkr.option_acquisition as option_acquisition

        ib = await _connected_option_quote_ib_async(
            host,
            port,
            client_id,
            readonly,
            timeout,
        )
        underlying = await option_acquisition._qualified_live_underlying(
            ib,
            instrument=instrument,
        )
        (
            exact_spot,
            exact_spot_source,
            exact_spot_entitlement,
            exact_spot_state,
        ) = await _option_board_provider_spot_reference(
            ib,
            underlying,
            futures_options=instrument_is_futures(instrument),
            timeout=timeout,
        )
    spot_reference = {
        "acquisition": spot_acquisition,
        "price_source": exact_spot_source or "provided",
        "market_data_entitlement": exact_spot_entitlement,
        "state": exact_spot_state,
    }
    reusable_source = _IBKR_RUNTIME.option_market_data_source(
        instrument_id,
        route_key,
        expiry_mode,
    )
    if reusable_source is not None and not isinstance(
        reusable_source,
        IbkrOptionMarketDataSource,
    ):
        raise RuntimeError("Option Board found an invalid provider ticker source")
    if isinstance(reusable_source, IbkrOptionMarketDataSource) and _ibkr_session_ready(
        reusable_source.transport
    ):
        _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
            reusable_source.contracts
        )
        if expired or unknown:
            reusable_source = None
    if isinstance(reusable_source, IbkrOptionMarketDataSource) and _ibkr_session_ready(
        reusable_source.transport
    ):
        own_key = _option_board_session_key(instrument, consumer_id)
        if own_key in _IBKR_RUNTIME.async_option_board_sessions:
            await _stop_option_board_owned(own_key)
        shared_quote_rows = [
            _option_board_quote_row(contract, ticker)
            for contract, ticker in zip(
                reusable_source.contracts,
                reusable_source.tickers,
                strict=True,
            )
        ]
        return _option_board_payload(
            instrument_id=instrument_id,
            route_key=route_key,
            provider_symbol=reusable_source.provider_symbol,
            consumer_id=consumer_id,
            generation=reusable_source.generation,
            spot=exact_spot,
            spot_reference=spot_reference,
            expiry_mode=expiry_mode,
            contracts=reusable_source.contracts,
            quote_rows=shared_quote_rows,
            chain_meta=dict(reusable_source.chain_meta),
            physical_subscription_source=reusable_source.source_id,
            opened_subscription_count=0,
        )

    subscription = await _ensure_option_board_subscription_owned(
        instrument,
        consumer_id=consumer_id,
        spot=exact_spot,
        expiry_mode=expiry_mode,
        host=host,
        port=port,
        client_id=client_id,
        readonly=readonly,
        timeout=timeout,
        option_expiry_facts=option_expiry_facts,
    )
    quote_rows: list[dict[str, Any]] = []
    for contract, cache_key in zip(
        subscription.contracts,
        subscription.cache_keys,
        strict=True,
    ):
        ticker = _IBKR_RUNTIME.async_option_ticker_cache.get(cache_key)
        if ticker is None:
            raise RuntimeError("Option Board provider ticker cache lost an exact contract")
        quote = _option_board_quote_row(contract, ticker)
        quote_rows.append(quote)
    return _option_board_payload(
        instrument_id=subscription.instrument_id,
        route_key=subscription.route_fingerprint,
        provider_symbol=subscription.provider_symbol,
        consumer_id=subscription.consumer_id,
        generation=subscription.generation,
        spot=exact_spot,
        spot_reference=spot_reference,
        expiry_mode=subscription.expiry_mode,
        contracts=subscription.contracts,
        quote_rows=quote_rows,
        chain_meta=subscription.chain_meta,
        physical_subscription_source="option-quote-pool",
        opened_subscription_count=subscription.opened_subscription_count,
    )


async def _stop_option_board_owned(
    key: OptionBoardSessionKey,
) -> dict[str, Any]:
    current = _IBKR_RUNTIME.async_option_board_sessions.pop(key, None)
    if not isinstance(current, IbkrOptionBoardSubscription):
        return {"ok": True, "cancelled": 0, "released": 0}
    cancelled = 0
    for cache_key in current.cache_keys:
        if _release_option_ticker_consumer_key(cache_key, current.consumer_id):
            cancelled += 1
    if cancelled:
        await asyncio.sleep(0.05)
    return {
        "ok": True,
        "cancelled": cancelled,
        "released": len(current.cache_keys),
        "instrument_id": current.instrument_id,
        "route_fingerprint": current.route_fingerprint,
    }
