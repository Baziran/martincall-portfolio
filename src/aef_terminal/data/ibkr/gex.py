from __future__ import annotations

import asyncio
import math
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aef_terminal.data.gex.constants import (
    GEX_ANALYSIS_MIN_PAIR_COVERAGE,
    GEX_EXECUTION_MODEL_INPUT_MAX_AGE_SECONDS,
    GEX_GENERIC_TICKS,
    LOGGER,
    NY_TZ,
)
from aef_terminal.data.gex.utils import finite_number_or_none, parse_gex_timestamp
from aef_terminal.data.ibkr.contracts import (
    _contract_for_instrument,
    _current_future_contract,
)
from aef_terminal.data.ibkr.event_loop import _ensure_event_loop
from aef_terminal.data.ibkr.gex_market import (
    GexTickerObservationTracker,
    _open_interest_from_ticker,
    _option_row_from_ticker,
    attach_gex_ticker_observation,
    detach_gex_ticker_observation,
)
from aef_terminal.data.ibkr.market_data import (
    ibkr_market_data_entitlement,
    request_ibkr_market_data_ticker,
)
from aef_terminal.data.ibkr.gex_request import IbkrGexRequest
from aef_terminal.data.ibkr.option_contracts import (
    IbkrOptionContract,
    IbkrOptionContractRequest,
    IbkrOptionMarketDataSource,
    IbkrOptionUniverseUnavailableError,
    partition_ibkr_option_contracts_by_expiry,
)
from aef_terminal.data.ibkr.option_acquisition import (
    _IbkrOptionExpiryFact,
    _assemble_exact_option_contract_candidates,
    _build_live_option_contracts,
    _cached_option_chains,
    _cached_option_series_details,
    _contracts_from_option_series_details,
    _filter_active_option_contracts,
    _option_chain_cache_key,
    _option_expiry_fact_for_series,
    _option_expiry_fact_payloads,
    _option_series_cache_key,
    _option_series_expiry_at_from_details,
    _option_series_key_from_query,
    _option_series_query,
    _qualified_live_underlying,
    _request_option_expiry_facts_by_series,
    _retain_option_expiry_fact,
    _selected_chain_expirations,
    _selected_expirations,
    _store_option_chains,
    _store_option_expiry_fact,
    _store_option_series_details,
    _time_monotonic,
)
from aef_terminal.data.ibkr.runtime import _IBKR_RUNTIME
from aef_terminal.data.ibkr.session import (
    _disconnect_owned_ibkr_session,
    _ibkr_session_ready,
    connect_without_account_sync,
    connect_without_account_sync_async,
)
from aef_terminal.data.ibkr.utils import _safe_cancel_mkt_data as _cancel_ibkr_market_data
from aef_terminal.data.instrument_identity import (
    instrument_is_futures,
    parse_exact_positive_decimal_provider_id,
    provider_symbol as exact_provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.market_data import MarketDataEntitlement
from aef_terminal.runtime.metrics import increment_metric, observe_metric

_DATETIME_TYPE = datetime
_GEX_REQUEST_SESSION_LOCK = threading.RLock()
_GEX_REQUEST_SESSION: Any | None = None
_GEX_REQUEST_SESSION_KEY: tuple[str, int, int, bool] | None = None
_GEX_REQUEST_SESSION_TRANSITIONING = False


@dataclass(frozen=True)
class IbkrGexAcquisition:
    contract_rows: tuple[dict[str, Any], ...]
    selected_contract_universe: tuple[dict[str, Any], ...]
    contracts_requested: int
    chain_meta: Mapping[str, Any]
    underlying_audit: Mapping[str, Any]
    market_data_entitlement: MarketDataEntitlement
    captured_at: datetime
    universe_expires_at: datetime
    collection_timed_out: bool = False
    collection_timeout_phase: str = ""


@dataclass(frozen=True)
class IbkrGexRequestTransportStatus:
    connected: bool
    host: str = ""
    port: int = 0
    client_id: int = 0


@dataclass(frozen=True)
class _IbkrRetainedGexRequestUniverse:
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    underlying_exchange: str
    request_contract: tuple[int, int, int, str, bool]
    underlying_audit: tuple[tuple[str, object], ...]
    contracts: tuple[IbkrOptionContract, ...]


@dataclass
class IbkrLiveGexSubscription:
    subscribed_strikes: tuple[float, ...]
    contracts_requested: int
    chain_meta: Mapping[str, Any]
    underlying_audit: Mapping[str, Any]
    _ib: Any = field(repr=False)
    _contracts: tuple[IbkrOptionContract, ...] = field(repr=False)
    _subscriptions: list[tuple[IbkrOptionContract, Any, GexTickerObservationTracker]] = field(
        repr=False
    )
    _option_source: IbkrOptionMarketDataSource | None = field(
        default=None,
        repr=False,
    )
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def selected_contract_universe(self) -> tuple[dict[str, Any], ...]:
        return _gex_selected_contract_universe(self._contracts)


@dataclass(frozen=True)
class IbkrLiveGexSample:
    contract_rows: tuple[dict[str, Any], ...]
    market_data_entitlement: MarketDataEntitlement


@dataclass(frozen=True)
class IbkrLiveGexHealth:
    connected: bool
    contract_count: int
    subscription_count: int
    active_contract_count: int = 0
    expired_contract_count: int = 0
    unknown_expiry_contract_count: int = 0
    earliest_expired_at: datetime | None = None
    universe_expires_at: datetime | None = None


def _gex_selected_contract_universe(
    contracts: Sequence[IbkrOptionContract],
) -> tuple[dict[str, Any], ...]:
    """Project exact provider-qualified identities without market-data facts."""

    return tuple(
        {
            "con_id": contract.con_id,
            "expiry": contract.expiry,
            "trading_class": contract.trading_class,
            "exchange": contract.exchange,
            "multiplier": contract.multiplier,
            "strike": contract.strike,
            "right": contract.right.value,
        }
        for contract in contracts
    )


def gex_selected_strike_universe_requires_rebase(
    strikes: Sequence[object],
    spot: object,
) -> bool:
    """Apply the canonical GEX strike-edge hysteresis to one selected universe."""

    current_spot = finite_number_or_none(spot)
    if current_spot is None or current_spot <= 0:
        raise ValueError("GEX universe rebase requires a positive canonical spot")
    canonical_strikes: list[float] = []
    for value in strikes:
        strike = finite_number_or_none(value)
        if strike is None or strike <= 0:
            raise ValueError("GEX universe rebase requires finite positive strikes")
        canonical_strikes.append(strike)
    if canonical_strikes != sorted(set(canonical_strikes)):
        raise ValueError("GEX universe rebase requires a sorted unique strike ladder")
    if len(canonical_strikes) < 5:
        return False
    return current_spot <= canonical_strikes[2] or current_spot >= canonical_strikes[-3]


def _ibkr_market_data_entitlement(
    actual_market_data_types: Iterable[object],
) -> MarketDataEntitlement:
    """Aggregate exact actual TWS callback values at the provider boundary."""

    values = list(actual_market_data_types)
    entitlements = {ibkr_market_data_entitlement(value) for value in values}
    if len(entitlements) != 1 or "unknown" in entitlements:
        return "unknown"
    return next(iter(entitlements))


def _qualified_underlying(ib, *, instrument: dict[str, Any]):
    qualified_instrument = require_provider_identity(instrument, provider="ibkr")
    provider_symbol = exact_provider_symbol(qualified_instrument, "ibkr")
    if instrument_is_futures(qualified_instrument):
        return _current_future_contract(ib, instrument=qualified_instrument)
    contract = _contract_for_instrument(qualified_instrument)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"Could not qualify {provider_symbol} underlying for GEX.")
    return qualified[0]


def _underlying_audit(contract: Any) -> dict[str, Any]:
    return {
        "underlying_symbol": str(getattr(contract, "symbol", "") or ""),
        "underlying_sec_type": str(getattr(contract, "secType", "") or ""),
        "underlying_con_id": getattr(contract, "conId", None),
        "underlying_local_symbol": str(getattr(contract, "localSymbol", "") or ""),
        "underlying_expiry": str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        "underlying_currency": require_exact_identity_text(
            getattr(contract, "currency", None),
            field="IBKR_UNDERLYING_CURRENCY",
        ),
    }


def _raise_if_gex_deadline(deadline: float | None, phase: str) -> None:
    if deadline is not None and _time_monotonic() >= deadline:
        raise TimeoutError(f"GEX refresh timed out during {phase}")


def _build_option_contracts(
    ib,
    provider_symbol: str,
    underlying,
    spot: float,
    request: IbkrGexRequest,
    deadline: float | None = None,
):
    currency = require_exact_identity_text(
        getattr(underlying, "currency", None),
        field="IBKR_UNDERLYING_CURRENCY",
    )
    _raise_if_gex_deadline(deadline, "option chain")
    chains = _request_option_chains(ib, underlying, deadline=deadline)
    request_now = datetime.now(NY_TZ)
    chain_expirations = _selected_chain_expirations(
        chains,
        required_exchange=require_exact_identity_text(
            getattr(underlying, "exchange", None),
            field="IBKR_UNDERLYING_EXCHANGE",
        ),
        max_expirations=request.max_expirations,
        now=request_now,
        mode=request.expiry_mode,
    )
    _raise_if_gex_deadline(deadline, "contract build")
    try:
        contracts, contract_meta = _build_option_contract_candidates(
            ib,
            provider_symbol,
            currency,
            spot,
            request,
            chain_expirations,
            deadline=deadline,
            now=request_now,
        )
    except IbkrOptionUniverseUnavailableError as exc:
        if not (
            request.expiry_mode == "hybrid"
            and exc.reason == "NO_ACTIVE_EXPIRY"
            and int(exc.diagnostics.get("expired_series_excluded") or 0) > 0
        ):
            raise
        expanded_chain_expirations = _selected_chain_expirations(
            chains,
            required_exchange=require_exact_identity_text(
                getattr(underlying, "exchange", None),
                field="IBKR_UNDERLYING_EXCHANGE",
            ),
            max_expirations=request.max_expirations + 1,
            now=request_now,
            mode=request.expiry_mode,
        )
        if {
            expiration
            for _chain, expirations in expanded_chain_expirations
            for expiration in expirations
        } == {
            expiration for _chain, expirations in chain_expirations for expiration in expirations
        }:
            raise
        contracts, contract_meta = _build_option_contract_candidates(
            ib,
            provider_symbol,
            currency,
            spot,
            request,
            expanded_chain_expirations,
            deadline=deadline,
            now=request_now,
        )
        chain_expirations = expanded_chain_expirations
    if (
        request.expiry_mode == "hybrid"
        and int(contract_meta.get("expired_series_excluded") or 0) > 0
        and len(contract_meta.get("selected_expiries") or ()) < request.max_expirations
    ):
        expanded_chain_expirations = _selected_chain_expirations(
            chains,
            required_exchange=require_exact_identity_text(
                getattr(underlying, "exchange", None),
                field="IBKR_UNDERLYING_EXCHANGE",
            ),
            max_expirations=request.max_expirations + 1,
            now=request_now,
            mode=request.expiry_mode,
        )
        if {
            expiration
            for _chain, expirations in expanded_chain_expirations
            for expiration in expirations
        } != {
            expiration for _chain, expirations in chain_expirations for expiration in expirations
        }:
            contracts, contract_meta = _build_option_contract_candidates(
                ib,
                provider_symbol,
                currency,
                spot,
                request,
                expanded_chain_expirations,
                deadline=deadline,
                now=request_now,
            )
    active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
        contracts,
        now=datetime.now(tz=UTC),
    )
    if unknown or expired or len(active) != len(contracts):
        raise IbkrOptionUniverseUnavailableError(
            "IBKR option universe crossed its exact expiry during qualification; retry required",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={
                "expired_contracts_excluded": len(expired),
                "expiry_time_unknown_contracts_excluded": len(unknown),
            },
        )
    _raise_if_gex_deadline(deadline, "exact option series")
    return list(contracts), contract_meta


def _attach_option_expiry_metadata(
    ib: Any,
    contracts: Sequence[IbkrOptionContract],
    *,
    known_expiry_at_by_series: Mapping[
        tuple[str, str, str, float],
        datetime,
    ]
    | None = None,
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    by_series: dict[tuple[str, str, str, float], list[IbkrOptionContract]] = defaultdict(list)
    for contract in contracts:
        by_series[contract.series_key].append(contract)
    known_expiry_at_by_series = dict(known_expiry_at_by_series or {})
    expiry_by_series: dict[tuple[str, str, str, float], datetime] = {}
    unknown_series: list[tuple[str, str, str, float]] = []
    for series_key, bucket in by_series.items():
        exact_values = {
            expiry_at.astimezone(UTC)
            for expiry_at in (
                *(contract.expiry_at for contract in bucket if contract.expiry_at is not None),
                known_expiry_at_by_series.get(series_key),
            )
            if isinstance(expiry_at, _DATETIME_TYPE)
            and expiry_at.tzinfo is not None
            and expiry_at.utcoffset() is not None
        }
        if len(exact_values) > 1:
            raise RuntimeError("IBKR option series has conflicting exact expiry timestamps")
        expiry_at = next(iter(exact_values), None)
        if expiry_at is not None:
            expiry_by_series[series_key] = expiry_at
            continue
        try:
            details = ib.reqContractDetails(bucket[0].raw) or []
        except Exception as exc:
            raise RuntimeError(
                f"IBKR option expiry qualification failed for conId={bucket[0].con_id}"
            ) from exc
        expiry_at = _option_series_expiry_at_from_details(
            details,
            bucket[0].expiry,
        )
        if expiry_at is None:
            unknown_series.append(series_key)
            continue
        expiry_by_series[series_key] = expiry_at
    if unknown_series:
        unknown_contracts = sum(len(by_series[series_key]) for series_key in unknown_series)
        raise IbkrOptionUniverseUnavailableError(
            "IBKR option contract universe is missing exact option expiry metadata",
            reason="EXPIRY_TIME_UNKNOWN",
            diagnostics={
                "expiry_time_source": "ibkr_contract_details",
                "expiry_time_exact_series": len(expiry_by_series),
                "expiry_time_series": len(by_series),
                "expiry_time_unknown_series": len(unknown_series),
                "expiry_time_unknown_contracts_excluded": unknown_contracts,
            },
        )
    updated = [
        replace(contract, expiry_at=expiry_by_series.get(contract.series_key, contract.expiry_at))
        for contract in contracts
    ]
    return updated, {
        "expiry_time_source": "ibkr_contract_details",
        "expiry_time_exact_series": len(expiry_by_series),
        "expiry_time_series": len(by_series),
        "expiry_time_unknown_series": 0,
    }


def _build_option_contract_candidates(
    ib: Any,
    provider_symbol: str,
    currency: str,
    spot: float,
    request: IbkrGexRequest | IbkrOptionContractRequest,
    chain_expirations: Sequence[tuple[Any, Sequence[str]]],
    *,
    deadline: float | None = None,
    now: datetime | None = None,
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    active_at = now or datetime.now(tz=UTC)
    partition_ibkr_option_contracts_by_expiry((), now=active_at)
    active_at_utc = active_at.astimezone(UTC)
    contract_series: list[list[IbkrOptionContract]] = []
    cache_key_by_series: dict[
        tuple[str, str, str, float],
        tuple[str, ...],
    ] = {}
    pending_series_details: dict[tuple[str, ...], Sequence[Any]] = {}
    known_expiry_at_by_series: dict[
        tuple[str, str, str, float],
        datetime,
    ] = {}
    requested_series_count = 0
    requested_expiry_values: set[str] = set()
    confirmed_expired_series_count = 0
    confirmed_expired_contract_count = 0
    request_expiry_facts = _request_option_expiry_facts_by_series(request)
    observed_expiry_facts = dict(request_expiry_facts)
    for chain, expirations in chain_expirations:
        for expiration in expirations:
            requested_series_count += 1
            requested_expiry_values.add(str(expiration))
            _raise_if_gex_deadline(deadline, "exact option series")
            query = _option_series_query(
                provider_symbol,
                currency,
                chain,
                str(expiration),
                request.futures_options,
            )
            cache_key = _option_series_cache_key(request.route_fingerprint, query)
            query_series_key = _option_series_key_from_query(query)
            cached_expiry_fact = _option_expiry_fact_for_series(
                cache_key,
                query_series_key,
                request_expiry_facts,
            )
            if cached_expiry_fact is not None:
                _retain_option_expiry_fact(
                    observed_expiry_facts,
                    query_series_key,
                    cached_expiry_fact,
                )
            if cached_expiry_fact is not None and cached_expiry_fact.expiry_at <= active_at_utc:
                confirmed_expired_series_count += 1
                confirmed_expired_contract_count += cached_expiry_fact.contract_count
                continue
            details = _cached_option_series_details(cache_key)
            if details is None:
                details = ib.reqContractDetails(query) or []
                pending_series_details[cache_key] = details
            parsed_series = _contracts_from_option_series_details(
                details,
                currency=currency,
                chain=chain,
                futures_options=request.futures_options,
                expiration=str(expiration),
            )
            series_key = parsed_series[0].series_key
            if series_key != query_series_key:
                raise RuntimeError("IBKR exact option qualification changed the requested series")
            if series_key in cache_key_by_series:
                raise RuntimeError("IBKR exact option qualification returned a duplicate series")
            cache_key_by_series[series_key] = cache_key
            if cached_expiry_fact is not None:
                known_expiry_at_by_series[series_key] = cached_expiry_fact.expiry_at
            exact_expiry_values = {
                contract.expiry_at.astimezone(UTC)
                for contract in parsed_series
                if contract.expiry_at is not None
            }
            if len(exact_expiry_values) > 1:
                raise RuntimeError("IBKR option series has conflicting exact expiry timestamps")
            parsed_expiry_at = next(iter(exact_expiry_values), None)
            if parsed_expiry_at is not None:
                _store_option_expiry_fact(
                    cache_key,
                    parsed_expiry_at,
                    contract_count=len(parsed_series),
                )
                known_expiry_at_by_series[series_key] = parsed_expiry_at
            contract_series.append(parsed_series)
    candidates = [contract for series in contract_series for contract in series]
    candidates, expiry_meta = _attach_option_expiry_metadata(
        ib,
        candidates,
        known_expiry_at_by_series=known_expiry_at_by_series,
    )
    expiry_meta["expiry_time_exact_series"] = (
        int(expiry_meta.get("expiry_time_exact_series") or 0) + confirmed_expired_series_count
    )
    expiry_meta["expiry_time_series"] = (
        int(expiry_meta.get("expiry_time_series") or 0) + confirmed_expired_series_count
    )
    expiry_by_series = {
        contract.series_key: contract.expiry_at
        for contract in candidates
        if contract.expiry_at is not None
    }
    for series_key, expiry_at in expiry_by_series.items():
        cache_key = cache_key_by_series[series_key]
        _store_option_expiry_fact(
            cache_key,
            expiry_at,
            contract_count=sum(contract.series_key == series_key for contract in candidates),
        )
        _retain_option_expiry_fact(
            observed_expiry_facts,
            series_key,
            _IbkrOptionExpiryFact(
                expiry_at=expiry_at.astimezone(UTC),
                contract_count=sum(contract.series_key == series_key for contract in candidates),
            ),
        )
        pending_details = pending_series_details.get(cache_key)
        if pending_details is not None:
            _store_option_series_details(cache_key, pending_details)
    active, active_meta = _filter_active_option_contracts(
        candidates,
        now=now,
        expiry_mode=request.expiry_mode,
        requested_expiries=tuple(requested_expiry_values),
        confirmed_expired_series_count=confirmed_expired_series_count,
        confirmed_expired_contract_count=confirmed_expired_contract_count,
    )
    contracts, contract_meta = _assemble_exact_option_contract_candidates(
        spot,
        request,
        active,
        contract_detail_requests=requested_series_count,
    )
    return contracts, {
        **expiry_meta,
        **active_meta,
        **contract_meta,
        "option_expiry_facts": _option_expiry_fact_payloads(observed_expiry_facts),
    }


def _request_option_chains(ib, underlying: Any, deadline: float | None = None) -> list[Any]:
    con_id = parse_exact_positive_decimal_provider_id(getattr(underlying, "conId", None))
    underlying_symbol = getattr(underlying, "symbol", None)
    underlying_sec_type = getattr(underlying, "secType", None)
    if (
        con_id <= 0
        or not isinstance(underlying_symbol, str)
        or not underlying_symbol
        or underlying_symbol != underlying_symbol.strip()
        or underlying_sec_type not in {"FUT", "STK", "IND"}
    ):
        raise RuntimeError("IBKR qualified underlying has invalid exact conId, symbol, or secType")
    if underlying_sec_type == "FUT":
        exchange = getattr(underlying, "exchange", None)
        if not isinstance(exchange, str) or not exchange or exchange != exchange.strip():
            raise RuntimeError("IBKR qualified futures underlying is missing exchange")
    else:
        exchange = ""
    cache_key = _option_chain_cache_key(con_id)
    cached = _cached_option_chains(cache_key)
    if cached is not None:
        return cached
    _raise_if_gex_deadline(deadline, "option chain")
    chains = list(
        ib.reqSecDefOptParams(underlying_symbol, exchange, underlying_sec_type, con_id) or []
    )
    if chains:
        _store_option_chains(cache_key, chains)
    return chains


def _collect_option_rows(
    ib,
    contracts: Sequence[IbkrOptionContract],
    spot: float,
    wait_seconds: float,
    *,
    batch_size: int = 8,
    batch_pause_seconds: float = 0.25,
    deadline: float | None = None,
    risk_free_rate: float = 0.052,
    dividend_yield: float = 0.0,
) -> list[dict[str, Any]]:
    rows = []
    if not contracts:
        return rows
    _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(contracts)
    if expired or unknown:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR option market-data collection requires an active exact option universe",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={
                "expired_contracts_excluded": len(expired),
                "expiry_time_unknown_contracts_excluded": len(unknown),
            },
        )
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, (int, float))
        or not math.isfinite(float(wait_seconds))
        or not 0.5 <= float(wait_seconds) <= 8.0
    ):
        raise ValueError("GEX option wait must be between 0.5 and 8 seconds")
    size, pause = _gex_option_batch_plan(
        len(contracts),
        batch_size,
        batch_pause_seconds,
    )
    wait_limit = float(wait_seconds)
    for start in range(0, len(contracts), size):
        if deadline is not None and _time_monotonic() >= deadline:
            if rows:
                rows.append(
                    {"_gex_collection_timeout": True, "_gex_timeout_phase": "option market data"}
                )
                return rows
            _raise_if_gex_deadline(deadline, "option market data")
        batch = list(contracts[start : start + size])
        tickers: list[tuple[IbkrOptionContract, Any, GexTickerObservationTracker]] = []
        timed_out_after_wait = False
        cancellation_ambiguous = False
        try:
            for contract in batch:
                try:
                    ticker = request_ibkr_market_data_ticker(
                        ib,
                        contract.raw,
                        GEX_GENERIC_TICKS,
                        False,
                        False,
                    )
                    tickers.append(
                        (
                            contract,
                            ticker,
                            attach_gex_ticker_observation(ticker, contract.right),
                        )
                    )
                except Exception as exc:
                    LOGGER.debug(
                        "IBKR GEX reqMktData failed for conId=%s: %s", contract.con_id, exc
                    )
            if tickers:
                wait_started = _time_monotonic()
                while True:
                    # Observe the subscription state as well as its event
                    # stream. This closes the race where IBKR populates
                    # modelGreeks between reqMktData and tracker attachment.
                    for _contract, ticker, tracker in tickers:
                        tracker.observe(ticker, provider_callback=False)
                    oi_complete = all(
                        _open_interest_from_ticker(ticker, contract.right) is not None
                        for contract, ticker, _tracker in tickers
                    )
                    usable_rows = [
                        _option_row_from_ticker(
                            contract,
                            ticker,
                            spot,
                            risk_free_rate=risk_free_rate,
                            dividend_yield=dividend_yield,
                            observation=tracker.evidence(ticker),
                        )
                        for contract, ticker, tracker in tickers
                    ]
                    display_greeks_complete = all(
                        (finite_number_or_none(row.get("gamma")) or 0.0) > 0
                        and finite_number_or_none(row.get("delta")) is not None
                        for row in usable_rows
                    )
                    broker_model_greeks_complete = all(
                        row.get("greek_source") == "modelGreeks"
                        and row.get("model_greeks_received_at") is not None
                        and (finite_number_or_none(row.get("gamma")) or 0.0) > 0
                        and finite_number_or_none(row.get("delta")) is not None
                        for row in usable_rows
                    )
                    entitlement_complete = all(
                        type(getattr(ticker, "marketDataType", None)) is int
                        and getattr(ticker, "marketDataType") in {1, 2, 3, 4}
                        for _contract, ticker, _tracker in tickers
                    )
                    live_entitlement = bool(
                        entitlement_complete
                        and all(
                            getattr(ticker, "marketDataType") == 1
                            for _contract, ticker, _tracker in tickers
                        )
                    )
                    greeks_complete = (
                        broker_model_greeks_complete
                        if live_entitlement
                        else display_greeks_complete
                    )
                    expected_pair_keys = {
                        (*contract.series_key, contract.strike)
                        for contract, _ticker, _tracker in tickers
                    }
                    complete_pair_rights: dict[
                        tuple[str, str, str, float, float],
                        set[str],
                    ] = defaultdict(set)
                    complete_observation_rows = 0
                    observation_now = datetime.now(tz=UTC)
                    for (
                        contract,
                        ticker,
                        _tracker,
                    ), row in zip(tickers, usable_rows, strict=True):
                        actual_market_data_type = getattr(
                            ticker,
                            "marketDataType",
                            None,
                        )
                        model_received_at = parse_gex_timestamp(row.get("model_greeks_received_at"))
                        open_interest_received_at = parse_gex_timestamp(
                            row.get("open_interest_received_at")
                        )
                        model_age_seconds = (
                            (observation_now - model_received_at).total_seconds()
                            if model_received_at is not None
                            else None
                        )
                        open_interest_age_seconds = (
                            (observation_now - open_interest_received_at).total_seconds()
                            if open_interest_received_at is not None
                            else None
                        )
                        exact_greeks = bool(
                            (finite_number_or_none(row.get("gamma")) or 0.0) > 0
                            and finite_number_or_none(row.get("delta")) is not None
                            and (
                                actual_market_data_type != 1
                                or (
                                    row.get("greek_source") == "modelGreeks"
                                    and model_age_seconds is not None
                                    and 0.0
                                    <= model_age_seconds
                                    <= GEX_EXECUTION_MODEL_INPUT_MAX_AGE_SECONDS
                                )
                            )
                        )
                        complete_observation = bool(
                            type(actual_market_data_type) is int
                            and actual_market_data_type in {1, 2, 3, 4}
                            and finite_number_or_none(row.get("open_interest")) is not None
                            and open_interest_age_seconds is not None
                            and open_interest_age_seconds >= 0.0
                            and exact_greeks
                        )
                        if complete_observation:
                            complete_observation_rows += 1
                            complete_pair_rights[(*contract.series_key, contract.strike)].add(
                                contract.right.value
                            )
                    complete_pair_count = sum(
                        1 for rights in complete_pair_rights.values() if rights == {"C", "P"}
                    )
                    minimum_pair_count = max(
                        1,
                        math.ceil(len(expected_pair_keys) * GEX_ANALYSIS_MIN_PAIR_COVERAGE),
                    )
                    sufficient_data = complete_pair_count >= minimum_pair_count

                    elapsed_wait = max(_time_monotonic() - wait_started, 0.0)
                    if (
                        oi_complete
                        and greeks_complete
                        and entitlement_complete
                        and complete_observation_rows == len(usable_rows)
                    ):
                        break
                    if sufficient_data and elapsed_wait > 2.0:
                        break
                    remaining_wait = max(wait_limit - elapsed_wait, 0.0)
                    remaining_deadline = (
                        max(deadline - _time_monotonic(), 0.0)
                        if deadline is not None
                        else remaining_wait
                    )
                    sleep_for = min(0.1, remaining_wait, remaining_deadline)
                    if sleep_for <= 0:
                        break
                    ib.sleep(sleep_for)
                timed_out_after_wait = deadline is not None and _time_monotonic() >= deadline
            _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(batch)
            if expired or unknown:
                raise IbkrOptionUniverseUnavailableError(
                    "IBKR option universe crossed its exact expiry during market-data collection",
                    reason="OPTION_UNIVERSE_ROLLOVER",
                    diagnostics={
                        "expired_contracts_excluded": len(expired),
                        "expiry_time_unknown_contracts_excluded": len(unknown),
                    },
                )
            for contract, ticker, tracker in tickers:
                row = _option_row_from_ticker(
                    contract,
                    ticker,
                    spot,
                    risk_free_rate=risk_free_rate,
                    dividend_yield=dividend_yield,
                    observation=tracker.evidence(ticker),
                )
                row["_ibkr_market_data_type"] = getattr(
                    ticker,
                    "marketDataType",
                    0,
                )
                rows.append(row)
        finally:
            for contract, ticker, tracker in tickers:
                detach_gex_ticker_observation(ticker, tracker)
                if not _cancel_ibkr_market_data(ib, ticker, contract.raw):
                    cancellation_ambiguous = True
            if tickers:
                _flush_gex_market_data_cancels(ib)
            if cancellation_ambiguous:
                _reset_gex_session()
        if cancellation_ambiguous:
            raise RuntimeError(
                "IBKR GEX market-data cancellation was not confirmed; the request session was reset"
            )
        if timed_out_after_wait:
            if rows:
                rows.append(
                    {"_gex_collection_timeout": True, "_gex_timeout_phase": "option market data"}
                )
                return rows
            _raise_if_gex_deadline(deadline, "option market data")
        if pause and start + size < len(contracts):
            if deadline is None:
                ib.sleep(pause)
            else:
                remaining = max(deadline - _time_monotonic(), 0.0)
                ib.sleep(min(pause, remaining))
            if deadline is not None and _time_monotonic() >= deadline:
                if rows:
                    rows.append(
                        {
                            "_gex_collection_timeout": True,
                            "_gex_timeout_phase": "option market data pause",
                        }
                    )
                    return rows
                _raise_if_gex_deadline(deadline, "option market data pause")
    return rows


def _collect_option_rows_by_expiration(
    ib,
    contracts: Sequence[IbkrOptionContract],
    spot: float,
    wait_seconds: float,
    *,
    batch_size: int = 8,
    batch_pause_seconds: float = 0.25,
    deadline: float | None = None,
    risk_free_rate: float = 0.052,
    dividend_yield: float = 0.0,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_expiry: dict[str, list[IbkrOptionContract]] = {}
    for contract in contracts:
        by_expiry.setdefault(contract.expiry, []).append(contract)
    expirations = sorted(by_expiry)
    for index, expiry in enumerate(expirations):
        if deadline is not None and _time_monotonic() >= deadline:
            if rows:
                rows.append(
                    {
                        "_gex_collection_timeout": True,
                        "_gex_timeout_phase": f"option market data expiry {expiry}",
                    }
                )
                return rows
            _raise_if_gex_deadline(deadline, f"option market data expiry {expiry}")
        segments = _split_expiration_contracts_by_strike_halves(by_expiry[expiry])
        for segment_index, segment in enumerate(segments):
            rows.extend(
                _collect_option_rows(
                    ib,
                    segment,
                    spot,
                    wait_seconds,
                    batch_size=batch_size,
                    batch_pause_seconds=batch_pause_seconds,
                    deadline=deadline,
                    risk_free_rate=risk_free_rate,
                    dividend_yield=dividend_yield,
                )
            )
            if _gex_rows_timed_out(rows):
                return rows
            if segment_index + 1 >= len(segments):
                continue
            pause = float(batch_pause_seconds)
            if deadline is None:
                ib.sleep(pause)
            else:
                remaining = max(deadline - _time_monotonic(), 0.0)
                ib.sleep(min(pause, remaining))
            if deadline is not None and _time_monotonic() >= deadline:
                if rows:
                    rows.append(
                        {
                            "_gex_collection_timeout": True,
                            "_gex_timeout_phase": f"option market data segment pause {expiry}",
                        }
                    )
                    return rows
                _raise_if_gex_deadline(deadline, f"option market data segment pause {expiry}")
        if index + 1 < len(expirations):
            pause = float(batch_pause_seconds)
            if deadline is None:
                ib.sleep(pause)
            else:
                remaining = max(deadline - _time_monotonic(), 0.0)
                ib.sleep(min(pause, remaining))
            if deadline is not None and _time_monotonic() >= deadline:
                if rows:
                    rows.append(
                        {
                            "_gex_collection_timeout": True,
                            "_gex_timeout_phase": f"option market data expiry pause {expiry}",
                        }
                    )
                    return rows
                _raise_if_gex_deadline(deadline, f"option market data expiry pause {expiry}")
    return rows


def _gex_rows_timed_out(rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(bool(row.get("_gex_collection_timeout")) for row in rows if isinstance(row, Mapping))


def _gex_rows_timeout_phase(rows: Sequence[Mapping[str, Any]]) -> str:
    for row in rows:
        if isinstance(row, Mapping) and row.get("_gex_collection_timeout"):
            return str(row.get("_gex_timeout_phase") or "option market data")
    return ""


def _gex_option_data_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in row.items() if key != "_ibkr_market_data_type"}
        for row in rows
        if isinstance(row, Mapping) and not row.get("_gex_collection_timeout")
    ]


def _split_expiration_contracts_by_strike_halves(
    contracts: Sequence[IbkrOptionContract],
) -> list[list[IbkrOptionContract]]:
    grouped: dict[float, list[IbkrOptionContract]] = defaultdict(list)
    for contract in contracts:
        grouped[contract.strike].append(contract)
    strikes = sorted(grouped)
    if len(strikes) <= 1:
        return [list(contracts)] if contracts else []
    midpoint = (len(strikes) + 1) // 2
    segments: list[list[IbkrOptionContract]] = []
    for strike_group in (strikes[:midpoint], strikes[midpoint:]):
        segment: list[IbkrOptionContract] = []
        for strike in strike_group:
            segment.extend(grouped[strike])
        if segment:
            segments.append(segment)
    return segments


def _gex_option_batch_plan(
    contract_count: int,
    requested_batch_size: int,
    requested_pause_seconds: float,
) -> tuple[int, float]:
    if type(contract_count) is not int or contract_count <= 0:
        raise ValueError("GEX option batch plan requires a positive contract count")
    if type(requested_batch_size) is not int or not 1 <= requested_batch_size <= 8:
        raise ValueError("GEX option batch size must be an exact integer from 1 to 8")
    if (
        isinstance(requested_pause_seconds, bool)
        or not isinstance(requested_pause_seconds, (int, float))
        or not math.isfinite(float(requested_pause_seconds))
        or not 0.25 <= float(requested_pause_seconds) <= 3.0
    ):
        raise ValueError("GEX option batch pause must be between 0.25 and 3 seconds")
    return min(requested_batch_size, contract_count), float(requested_pause_seconds)


def _flush_gex_market_data_cancels(ib: Any, delay_seconds: float = 0.15) -> None:
    if not _ibkr_session_ready(ib):
        return
    if (
        isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, (int, float))
        or not math.isfinite(float(delay_seconds))
        or not 0.05 <= float(delay_seconds) <= 0.35
    ):
        raise ValueError("GEX cancel flush delay must be between 0.05 and 0.35 seconds")
    try:
        ib.sleep(float(delay_seconds))
    except Exception as exc:
        LOGGER.debug("Ignored IBKR GEX cancel flush error: %s", exc)


def _gex_transport_connected(ib: Any | None) -> bool:
    return _ibkr_session_ready(ib)


def _configure_gex_market_data(ib: Any, market_data_type: int) -> None:
    ib.reqMarketDataType(market_data_type)


def _connected_gex_ib(
    request: IbkrGexRequest,
    *,
    connection_timeout: float | None = None,
) -> Any:
    _ensure_event_loop()
    from ib_async import IB

    resolved_connection_timeout = float(
        request.timeout if connection_timeout is None else connection_timeout
    )
    if not math.isfinite(resolved_connection_timeout) or resolved_connection_timeout <= 0:
        raise TimeoutError("GEX refresh timed out before broker connect")
    global _GEX_REQUEST_SESSION, _GEX_REQUEST_SESSION_TRANSITIONING
    global _GEX_REQUEST_SESSION_KEY
    key = (request.host, request.port, request.client_id, request.readonly)
    with _GEX_REQUEST_SESSION_LOCK:
        if _GEX_REQUEST_SESSION_TRANSITIONING:
            raise RuntimeError("IBKR GEX request session transition is in progress")
        if _GEX_REQUEST_SESSION_KEY == key and _gex_transport_connected(_GEX_REQUEST_SESSION):
            return _GEX_REQUEST_SESSION
        previous_session = _GEX_REQUEST_SESSION
        previous_key = _GEX_REQUEST_SESSION_KEY
        _GEX_REQUEST_SESSION_TRANSITIONING = True
    try:
        if previous_session is not None:
            _flush_gex_market_data_cancels(previous_session)
            _disconnect_owned_ibkr_session(
                previous_session,
                session="gex_request",
                session_key=previous_key,
            )
        with _GEX_REQUEST_SESSION_LOCK:
            if _GEX_REQUEST_SESSION is previous_session:
                _GEX_REQUEST_SESSION = None
                _GEX_REQUEST_SESSION_KEY = None
        ib = IB()
        connect_without_account_sync(
            ib,
            request.host,
            request.port,
            request.client_id,
            resolved_connection_timeout,
        )
        try:
            ib.RequestTimeout = resolved_connection_timeout
        except Exception:
            LOGGER.warning(
                "IBKR GEX request timeout configuration failed for %s:%s clientId %s",
                request.host,
                request.port,
                request.client_id,
                exc_info=True,
            )
    except Exception:
        with _GEX_REQUEST_SESSION_LOCK:
            _GEX_REQUEST_SESSION_TRANSITIONING = False
        raise
    with _GEX_REQUEST_SESSION_LOCK:
        _GEX_REQUEST_SESSION = ib
        _GEX_REQUEST_SESSION_KEY = key
        _GEX_REQUEST_SESSION_TRANSITIONING = False
    return ib


def _reset_gex_session() -> None:
    global _GEX_REQUEST_SESSION, _GEX_REQUEST_SESSION_TRANSITIONING
    global _GEX_REQUEST_SESSION_KEY
    with _GEX_REQUEST_SESSION_LOCK:
        if _GEX_REQUEST_SESSION_TRANSITIONING:
            return
        session = _GEX_REQUEST_SESSION
        session_key = _GEX_REQUEST_SESSION_KEY
        if session is None:
            _GEX_REQUEST_SESSION_KEY = None
            return
        _GEX_REQUEST_SESSION_TRANSITIONING = True
    try:
        _flush_gex_market_data_cancels(session)
        _disconnect_owned_ibkr_session(
            session,
            session="gex_request",
            session_key=session_key,
        )
    except Exception:
        with _GEX_REQUEST_SESSION_LOCK:
            _GEX_REQUEST_SESSION_TRANSITIONING = False
        raise
    with _GEX_REQUEST_SESSION_LOCK:
        if _GEX_REQUEST_SESSION is session:
            _GEX_REQUEST_SESSION = None
            _GEX_REQUEST_SESSION_KEY = None
        _GEX_REQUEST_SESSION_TRANSITIONING = False


def gex_request_transport_status() -> IbkrGexRequestTransportStatus:
    with _GEX_REQUEST_SESSION_LOCK:
        key = _GEX_REQUEST_SESSION_KEY
        return IbkrGexRequestTransportStatus(
            connected=(
                not _GEX_REQUEST_SESSION_TRANSITIONING
                and _gex_transport_connected(_GEX_REQUEST_SESSION)
            ),
            host=key[0] if key else "",
            port=key[1] if key else 0,
            client_id=key[2] if key else 0,
        )


def _retained_gex_request_universe(
    state: object,
    *,
    instrument_id: str,
    route_fingerprint: str,
    provider_symbol: str,
    underlying_exchange: str,
    underlying_audit: Mapping[str, object],
    request: IbkrGexRequest,
    spot: float,
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    """Reuse one exact process-owned request universe while its contract holds."""

    if not isinstance(state, _IbkrRetainedGexRequestUniverse):
        return [], {}
    request_contract = (
        request.strike_count,
        request.max_expirations,
        request.max_contracts,
        request.expiry_mode,
        request.futures_options,
    )
    if (
        state.instrument_id != instrument_id
        or state.route_fingerprint != route_fingerprint
        or state.provider_symbol != provider_symbol
        or not isinstance(underlying_exchange, str)
        or not underlying_exchange
        or underlying_exchange != underlying_exchange.strip()
        or state.underlying_exchange != underlying_exchange
        or state.request_contract != request_contract
        or dict(state.underlying_audit) != dict(underlying_audit)
    ):
        return [], {}
    contracts = list(state.contracts)
    expected_sec_type = "FOP" if request.futures_options else "OPT"
    expected_currency = underlying_audit.get("underlying_currency")
    if not contracts or any(
        contract.symbol != provider_symbol
        or contract.sec_type != expected_sec_type
        or contract.currency != expected_currency
        for contract in contracts
    ):
        return [], {}
    try:
        active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
            contracts,
        )
        if expired or unknown or len(active) != len(contracts):
            return [], {}
        retained_expirations = sorted({contract.expiry for contract in contracts})
        if (
            _selected_expirations(
                SimpleNamespace(expirations=retained_expirations),
                request.max_expirations,
                mode=request.expiry_mode,
            )
            != retained_expirations
        ):
            return [], {}
        strikes = sorted({contract.strike for contract in contracts})
        if gex_selected_strike_universe_requires_rebase(strikes, spot):
            return [], {}
        selected, chain_meta = _assemble_exact_option_contract_candidates(
            spot,
            request,
            contracts,
            contract_detail_requests=1,
        )
        if {contract.con_id for contract in selected} != {
            contract.con_id for contract in contracts
        }:
            raise ValueError("retained provider-selected universe exceeds current request contract")
    except IbkrOptionUniverseUnavailableError, TypeError, ValueError:
        return [], {}
    chain_meta.update(
        {
            "qualification_source": "retained_provider_selected_universe",
            "contract_detail_requests": 0,
        }
    )
    return selected, chain_meta


def fetch_gex_option_rows(
    *,
    instrument: dict[str, Any],
    request: IbkrGexRequest,
    spot: float,
    dividend_yield: float,
) -> IbkrGexAcquisition:
    _ensure_event_loop()
    request_started = _time_monotonic()
    qualified_instrument = require_provider_identity(instrument, provider="ibkr")
    route_key = route_fingerprint(qualified_instrument)
    if request.route_fingerprint != route_key:
        raise ValueError("GEX request route no longer matches the qualified instrument")
    provider_symbol = exact_provider_symbol(qualified_instrument, "ibkr")
    resolved_spot = finite_number_or_none(spot)
    if resolved_spot is None or resolved_spot <= 0:
        raise ValueError("IBKR GEX acquisition requires a positive canonical spot")
    deadline = _time_monotonic() + float(request.timeout)
    session_reset = False
    try:
        phase = "connect"
        try:
            _raise_if_gex_deadline(deadline, "owner gate")
            ib = _connected_gex_ib(
                request,
                connection_timeout=max(deadline - _time_monotonic(), 0.0),
            )
            _raise_if_gex_deadline(deadline, phase)
            phase = "market data type"
            _configure_gex_market_data(ib, request.market_data_type)
            phase = "underlying qualification"
            qualification_started = _time_monotonic()
            underlying = _qualified_underlying(ib, instrument=qualified_instrument)
            underlying_audit = _underlying_audit(underlying)
            raw_underlying_exchange = getattr(underlying, "exchange", None)
            underlying_exchange = (
                raw_underlying_exchange
                if isinstance(raw_underlying_exchange, str)
                and raw_underlying_exchange
                and raw_underlying_exchange == raw_underlying_exchange.strip()
                else ""
            )
            _raise_if_gex_deadline(deadline, "underlying")
            universe_cache_key = (
                qualified_instrument_id(qualified_instrument),
                route_key,
            )
            contracts, chain_meta = _retained_gex_request_universe(
                _IBKR_RUNTIME.gex_request_universe_cache.get(universe_cache_key),
                instrument_id=universe_cache_key[0],
                route_fingerprint=route_key,
                provider_symbol=provider_symbol,
                underlying_exchange=underlying_exchange,
                underlying_audit=underlying_audit,
                request=request,
                spot=resolved_spot,
            )
            if not contracts:
                phase = "option contract qualification"
                contracts, chain_meta = _build_option_contracts(
                    ib,
                    provider_symbol,
                    underlying,
                    resolved_spot,
                    request,
                    deadline=deadline,
                )
            observe_metric(
                "gex_acquisition_phase_seconds",
                max(_time_monotonic() - qualification_started, 0.0),
                route_fingerprint=route_key,
                phase="qualification",
            )
            increment_metric(
                "gex_option_contracts_requested_total",
                len(contracts),
                route_fingerprint=route_key,
            )
            phase = "option market data"
            option_market_data_started = _time_monotonic()
            collected_rows = _collect_option_rows_by_expiration(
                ib,
                contracts,
                resolved_spot,
                request.wait_seconds,
                batch_size=request.batch_size,
                batch_pause_seconds=request.batch_pause_seconds,
                deadline=deadline,
                risk_free_rate=request.risk_free_rate,
                dividend_yield=dividend_yield,
            )
            observe_metric(
                "gex_acquisition_phase_seconds",
                max(_time_monotonic() - option_market_data_started, 0.0),
                route_fingerprint=route_key,
                phase="option_market_data",
            )
            captured_at = datetime.now(tz=UTC)
            _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
                contracts,
                now=captured_at,
            )
            if expired or unknown:
                raise IbkrOptionUniverseUnavailableError(
                    "IBKR option universe crossed its exact expiry before request capture",
                    reason="OPTION_UNIVERSE_ROLLOVER",
                    diagnostics={
                        "expired_contracts_excluded": len(expired),
                        "expiry_time_unknown_contracts_excluded": len(unknown),
                    },
                )
            market_data_entitlement = _ibkr_market_data_entitlement(
                row.get("_ibkr_market_data_type")
                for row in collected_rows
                if isinstance(row, Mapping) and not row.get("_gex_collection_timeout")
            )
            contract_rows = tuple(_gex_option_data_rows(collected_rows))
            collection_timed_out = _gex_rows_timed_out(collected_rows)
            collection_timeout_phase = _gex_rows_timeout_phase(collected_rows)
            if collection_timed_out and not contract_rows:
                raise TimeoutError(
                    f"GEX refresh timed out during {collection_timeout_phase or phase}"
                )
            acquisition = IbkrGexAcquisition(
                contract_rows=contract_rows,
                selected_contract_universe=_gex_selected_contract_universe(contracts),
                contracts_requested=len(contracts),
                chain_meta=dict(chain_meta),
                underlying_audit=dict(underlying_audit),
                market_data_entitlement=market_data_entitlement,
                captured_at=captured_at,
                universe_expires_at=min(
                    contract.expiry_at.astimezone(UTC)
                    for contract in contracts
                    if contract.expiry_at is not None
                ),
                collection_timed_out=collection_timed_out,
                collection_timeout_phase=collection_timeout_phase,
            )
        except TimeoutError as exc:
            if str(exc).strip():
                raise
            raise TimeoutError(f"GEX refresh timed out during {phase}") from exc
        finally:
            _reset_gex_session()
            session_reset = True
        final_captured_at = datetime.now(tz=UTC)
        _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
            contracts,
            now=final_captured_at,
        )
        if expired or unknown:
            raise IbkrOptionUniverseUnavailableError(
                "IBKR option universe crossed its exact expiry before request delivery",
                reason="OPTION_UNIVERSE_ROLLOVER",
                diagnostics={
                    "expired_contracts_excluded": len(expired),
                    "expiry_time_unknown_contracts_excluded": len(unknown),
                },
            )
        _IBKR_RUNTIME.gex_request_universe_cache[universe_cache_key] = (
            _IbkrRetainedGexRequestUniverse(
                instrument_id=universe_cache_key[0],
                route_fingerprint=route_key,
                provider_symbol=provider_symbol,
                underlying_exchange=underlying_exchange,
                request_contract=(
                    request.strike_count,
                    request.max_expirations,
                    request.max_contracts,
                    request.expiry_mode,
                    request.futures_options,
                ),
                underlying_audit=tuple(underlying_audit.items()),
                contracts=tuple(contracts),
            )
        )
        observe_metric(
            "gex_acquisition_seconds",
            max(_time_monotonic() - request_started, 0.0),
            route_fingerprint=route_key,
        )
        return acquisition
    finally:
        if not session_reset:
            _reset_gex_session()


async def _connected_live_gex_ib_async(request: IbkrGexRequest) -> Any:
    """Open the broker socket exclusively owned by continuous GEX."""

    from ib_async import IB

    ib = IB()
    return await connect_without_account_sync_async(
        ib,
        request.host,
        request.port,
        request.client_id,
        float(request.timeout),
    )


def _disconnect_live_gex_ib(ib: Any | None) -> None:
    _disconnect_owned_ibkr_session(
        ib,
        session="gex_live",
        session_key="dedicated",
    )


async def open_live_gex_subscription(
    *,
    provider_symbol: str,
    instrument: dict[str, Any],
    request: IbkrGexRequest,
    spot: float,
) -> IbkrLiveGexSubscription:
    ib = await _connected_live_gex_ib_async(request)
    underlying = None
    tickers: list[tuple[IbkrOptionContract, Any, GexTickerObservationTracker]] = []
    try:
        _configure_gex_market_data(ib, request.market_data_type)
        underlying = await _qualified_live_underlying(ib, instrument=instrument)
        contracts, chain_meta = await _build_live_option_contracts(
            ib,
            provider_symbol,
            underlying,
            spot,
            request,
        )
        for contract in contracts:
            ticker = request_ibkr_market_data_ticker(
                ib,
                contract.raw,
                GEX_GENERIC_TICKS,
                False,
                False,
            )
            tickers.append(
                (
                    contract,
                    ticker,
                    attach_gex_ticker_observation(ticker, contract.right),
                )
            )
            if len(tickers) % 12 == 0:
                await asyncio.sleep(0)
        await asyncio.sleep(float(request.wait_seconds))
        _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(contracts)
        if expired or unknown:
            raise IbkrOptionUniverseUnavailableError(
                "IBKR live option universe crossed its exact expiry during subscription bootstrap",
                reason="OPTION_UNIVERSE_ROLLOVER",
                diagnostics={
                    "expired_contracts_excluded": len(expired),
                    "expiry_time_unknown_contracts_excluded": len(unknown),
                },
            )
        option_source = IbkrOptionMarketDataSource(
            source_id="live-gex",
            instrument_id=qualified_instrument_id(instrument),
            route_fingerprint=route_fingerprint(instrument),
            provider_symbol=provider_symbol,
            expiry_mode=request.expiry_mode,
            contracts=tuple(contracts),
            tickers=tuple(ticker for _contract, ticker, _tracker in tickers),
            chain_meta=dict(chain_meta),
            transport=ib,
            generation=f"live-gex:{id(ib)}",
        )
        subscription = IbkrLiveGexSubscription(
            subscribed_strikes=tuple(sorted({contract.strike for contract in contracts})),
            contracts_requested=len(contracts),
            chain_meta=dict(chain_meta),
            underlying_audit=_underlying_audit(underlying),
            _ib=ib,
            _contracts=tuple(contracts),
            _subscriptions=tickers,
            _option_source=option_source,
        )
        _IBKR_RUNTIME.register_option_market_data_source(option_source)
        return subscription
    except asyncio.CancelledError:
        _close_live_gex_transport(ib, tickers)
        raise
    except Exception:
        _close_live_gex_transport(ib, tickers)
        raise


def sample_live_gex_subscription(
    subscription: IbkrLiveGexSubscription,
    *,
    spot: float,
    risk_free_rate: float,
    dividend_yield: float,
) -> IbkrLiveGexSample:
    provider_callback_count = 0
    provider_tick_count = 0
    with subscription._lock:
        if subscription._closed:
            raise RuntimeError("IBKR live GEX subscription is closed")
        _active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
            subscription._contracts
        )
        if expired or unknown:
            raise IbkrOptionUniverseUnavailableError(
                "IBKR live option subscription crossed its exact expiry",
                reason="OPTION_UNIVERSE_ROLLOVER",
                diagnostics={
                    "expired_contracts_excluded": len(expired),
                    "expiry_time_unknown_contracts_excluded": len(unknown),
                },
            )
        sampled_rows: list[dict[str, Any]] = []
        for contract, ticker, tracker in subscription._subscriptions:
            # reqMktData can populate cached ticker facts before ib_insync
            # attaches updateEvent listeners. Close that observation race at
            # the canonical sample boundary before reading typed evidence.
            tracker.observe(ticker, provider_callback=False)
            callback_count, tick_count = tracker.take_provider_callback_activity()
            provider_callback_count += callback_count
            provider_tick_count += tick_count
            sampled_rows.append(
                _option_row_from_ticker(
                    contract,
                    ticker,
                    spot,
                    risk_free_rate=risk_free_rate,
                    dividend_yield=dividend_yield,
                    observation=tracker.evidence(ticker),
                )
            )
        rows = tuple(sampled_rows)
        market_data_entitlement = _ibkr_market_data_entitlement(
            getattr(ticker, "marketDataType", 0)
            for _contract, ticker, _tracker in subscription._subscriptions
        )
        route_key = (
            subscription._option_source.route_fingerprint
            if subscription._option_source is not None
            else ""
        )
    if route_key:
        observe_metric(
            "gex_live_provider_activity_per_frame",
            provider_callback_count,
            route_fingerprint=route_key,
            activity="ticker_callbacks",
        )
        observe_metric(
            "gex_live_provider_activity_per_frame",
            provider_tick_count,
            route_fingerprint=route_key,
            activity="tick_rows",
        )
    return IbkrLiveGexSample(
        contract_rows=rows,
        market_data_entitlement=market_data_entitlement,
    )


def live_gex_subscription_health(
    subscription: IbkrLiveGexSubscription,
    *,
    now: datetime | None = None,
) -> IbkrLiveGexHealth:
    with subscription._lock:
        active, expired, unknown = partition_ibkr_option_contracts_by_expiry(
            subscription._contracts,
            now=now,
        )
        earliest_expired_at = min(
            (
                contract.expiry_at.astimezone(UTC)
                for contract in expired
                if contract.expiry_at is not None
            ),
            default=None,
        )
        universe_expires_at = min(
            (
                contract.expiry_at.astimezone(UTC)
                for contract in subscription._contracts
                if contract.expiry_at is not None
            ),
            default=None,
        )
        return IbkrLiveGexHealth(
            connected=not subscription._closed and _gex_transport_connected(subscription._ib),
            contract_count=len(subscription._contracts),
            subscription_count=len(subscription._subscriptions),
            active_contract_count=len(active),
            expired_contract_count=len(expired),
            unknown_expiry_contract_count=len(unknown),
            earliest_expired_at=earliest_expired_at,
            universe_expires_at=universe_expires_at,
        )


def close_live_gex_subscription(subscription: IbkrLiveGexSubscription) -> int:
    with subscription._lock:
        if subscription._closed:
            return 0
        if subscription._option_source is not None:
            _IBKR_RUNTIME.unregister_option_market_data_source(subscription._option_source)
        cancelled = _close_live_gex_transport(
            subscription._ib,
            list(subscription._subscriptions),
        )
        subscription._subscriptions.clear()
        subscription._closed = True
        return cancelled


def _close_live_gex_transport(
    ib: Any,
    tickers: Sequence[tuple[IbkrOptionContract, Any, GexTickerObservationTracker]],
) -> int:
    cancelled = 0
    for contract, ticker, tracker in list(tickers):
        detach_gex_ticker_observation(ticker, tracker)
        try:
            if _cancel_ibkr_market_data(ib, ticker, contract.raw):
                cancelled += 1
            else:
                LOGGER.warning(
                    "IBKR live GEX cancellation was not confirmed for %s",
                    contract.raw,
                )
        except Exception:
            LOGGER.warning(
                "IBKR live GEX cancellation failed for %s",
                contract.raw,
                exc_info=True,
            )
    _disconnect_live_gex_ib(ib)
    return cancelled
