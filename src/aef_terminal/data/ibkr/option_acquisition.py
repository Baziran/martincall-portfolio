from __future__ import annotations

import math
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from aef_terminal.data.ibkr.gex_request import IbkrGexRequest
from aef_terminal.data.provider_contract import require_option_expiry_mode
from aef_terminal.data.gex.contracts import require_gex_option_series_identity
from aef_terminal.data.gex.constants import NY_TZ
from aef_terminal.data.gex.utils import _expiration_date, finite_number_or_none
from aef_terminal.data.ibkr.contracts import (
    _contract_for_instrument,
    _current_future_contract_async,
)
from aef_terminal.data.ibkr.event_loop import _ensure_event_loop
from aef_terminal.data.ibkr.option_contracts import (
    IbkrOptionContract,
    IbkrOptionContractRequest,
    IbkrOptionUniverseUnavailableError,
    IbkrPriceIncrement,
    partition_ibkr_option_contracts_by_expiry,
    parse_ibkr_option_contract,
    require_ibkr_price_increments,
    require_ibkr_option_multiplier,
)
from aef_terminal.data.instrument_identity import (
    instrument_is_futures,
    parse_exact_positive_decimal_provider_id,
    provider_symbol as exact_provider_symbol,
    require_exact_identity_text,
    require_provider_identity,
)


_IBKR_OPTION_CHAIN_CACHE_TTL_SECONDS = 30 * 60
_IBKR_OPTION_CHAIN_CACHE_MAX = 32
_IBKR_OPTION_CHAIN_CACHE_LOCK = threading.Lock()
_IBKR_OPTION_CHAIN_CACHE: dict[int, tuple[float, list[Any]]] = {}
_IBKR_OPTION_SERIES_CACHE_TTL_SECONDS = 30 * 60
_IBKR_OPTION_SERIES_CACHE_MAX = 128
_IBKR_OPTION_SERIES_CACHE: dict[tuple[str, ...], tuple[float, list[Any]]] = {}
_IBKR_OPTION_EXPIRY_FACT_CACHE_MAX = 512
_DATETIME_TYPE = datetime


@dataclass(frozen=True)
class _IbkrOptionExpiryFact:
    expiry_at: datetime
    contract_count: int


_IBKR_OPTION_EXPIRY_FACT_CACHE: dict[
    tuple[str, ...],
    _IbkrOptionExpiryFact,
] = {}


def _option_market_rule_id_from_details(
    details: Any,
    *,
    exchange: str,
) -> int | None:
    """Resolve the rule aligned with the contract's exact exchange."""

    valid_exchanges = getattr(details, "validExchanges", None)
    market_rule_ids = getattr(details, "marketRuleIds", None)
    if not isinstance(valid_exchanges, str) or not isinstance(
        market_rule_ids,
        str,
    ):
        return None
    exchanges = valid_exchanges.split(",")
    rule_values = market_rule_ids.split(",")
    if len(exchanges) != len(rule_values):
        raise RuntimeError("IBKR option details returned misaligned exchange market rules")
    matches = {
        value
        for candidate, value in zip(
            exchanges,
            rule_values,
            strict=True,
        )
        if candidate == exchange
    }
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("IBKR option details returned conflicting exchange market rules")
    raw_rule_id = next(iter(matches))
    if not raw_rule_id.isdigit() or int(raw_rule_id) <= 0:
        raise RuntimeError("IBKR option details returned an invalid exchange market rule")
    return int(raw_rule_id)


def _option_minimum_tick_from_details(details: Any) -> float | None:
    raw = getattr(details, "minTick", None)
    if raw is None:
        return None
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
        or float(raw) <= 0
    ):
        raise RuntimeError("IBKR option details returned an invalid minTick")
    return float(raw)


def _price_increments_from_market_rule(
    values: object,
) -> tuple[IbkrPriceIncrement, ...]:
    if not isinstance(values, Sequence) or isinstance(
        values,
        (str, bytes, bytearray),
    ):
        raise ValueError("IBKR market rule response is invalid")
    increments = tuple(
        IbkrPriceIncrement(
            low_edge=getattr(value, "lowEdge", None),
            increment=getattr(value, "increment", None),
        )
        for value in values
    )
    return require_ibkr_price_increments(increments)


async def _attach_option_price_increments_async(
    ib: Any,
    contracts: Sequence[IbkrOptionContract],
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    """Attach exact provider price bands without inventing a fallback."""

    rule_ids = sorted(
        {contract.market_rule_id for contract in contracts if contract.market_rule_id is not None}
    )
    requester = getattr(ib, "reqMarketRuleAsync", None)
    schedules: dict[int, tuple[IbkrPriceIncrement, ...]] = {}
    failed_rule_ids: list[int] = []
    if callable(requester):
        for rule_id in rule_ids:
            try:
                schedules[rule_id] = _price_increments_from_market_rule(await requester(rule_id))
            except Exception:
                failed_rule_ids.append(rule_id)
    else:
        failed_rule_ids.extend(rule_ids)
    attached = [
        replace(
            contract,
            price_increments=schedules.get(contract.market_rule_id, ()),
        )
        for contract in contracts
    ]
    available = sum(bool(contract.price_increments) for contract in attached)
    total = len(attached)
    return attached, {
        "price_rule_status": (
            "complete"
            if total and available == total
            else "partial"
            if available
            else "unavailable"
        ),
        "price_rule_contract_count": available,
        "price_rule_unavailable_contract_count": total - available,
        "price_rule_ids": rule_ids,
        "price_rule_failed_ids": failed_rule_ids,
    }


def _assemble_exact_option_contract_candidates(
    spot: float,
    request: IbkrGexRequest | IbkrOptionContractRequest,
    candidates: Sequence[IbkrOptionContract],
    *,
    contract_detail_requests: int,
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    if type(contract_detail_requests) is not int or contract_detail_requests <= 0:
        raise ValueError("IBKR exact option qualification requires a positive request count")
    if not candidates:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR returned no active exact option series.",
            reason=("NO_ACTIVE_0DTE" if request.expiry_mode == "0dte" else "NO_ACTIVE_EXPIRY"),
        )
    selected_expirations = sorted({contract.expiry for contract in candidates})[
        : request.max_expirations
    ]
    selected_expiration_set = set(selected_expirations)
    selected_candidates = [
        contract for contract in candidates if contract.expiry in selected_expiration_set
    ]
    contract_series_by_key: dict[
        tuple[str, str, str, float],
        list[IbkrOptionContract],
    ] = defaultdict(list)
    for contract in selected_candidates:
        contract_series_by_key[contract.series_key].append(contract)
    contracts = _select_global_option_series_contracts(
        [contract_series_by_key[series_key] for series_key in sorted(contract_series_by_key)],
        spot,
        request.strike_count,
        request.max_contracts,
    )
    if not contracts:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR returned no paired contracts in the active exact option series",
            reason="NO_ACTIVE_EXPIRY",
        )
    seen_contracts: set[int] = set()
    for contract in contracts:
        if contract.con_id in seen_contracts:
            raise ValueError("IBKR exact option universe contains a duplicate provider conId")
        seen_contracts.add(contract.con_id)
    selected_expirations = sorted({contract.expiry for contract in contracts})
    chain_labels = {
        "/".join(
            item
            for item in (
                contract.exchange,
                contract.trading_class,
                f"{contract.multiplier:g}",
            )
            if item
        )
        for contract in contracts
    }
    qualified_strike_count = len({contract.strike for contract in contracts})
    return contracts, {
        "chain_exchange": ",".join(sorted({contract.exchange for contract in contracts})),
        "trading_class": ",".join(sorted({contract.trading_class for contract in contracts})),
        "chain_count": len(
            {
                (
                    contract.exchange,
                    contract.trading_class,
                    contract.multiplier,
                )
                for contract in contracts
            }
        ),
        "chain_labels": ",".join(sorted(label for label in chain_labels if label)),
        "expiry_mode": request.expiry_mode,
        "trade_expiration": _trade_expiration(selected_expirations),
        "selected_expiries": selected_expirations,
        "expirations": ",".join(selected_expirations),
        "qualified_strike_count": qualified_strike_count,
        "qualified_pair_count": len(contracts) // 2,
        "candidate_contracts": len(contracts),
        "qualified_contracts": len(contracts),
        "qualified_series_count": len({contract.series_key for contract in contracts}),
        "global_strike_ladder": sorted({contract.strike for contract in contracts}),
        "qualification_source": "ibkr_contract_details_exact_series",
        "contract_detail_requests": contract_detail_requests,
    }


async def _attach_option_expiry_metadata_async(
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
            details = await ib.reqContractDetailsAsync(bucket[0].raw)
        except Exception as exc:
            raise RuntimeError(
                f"IBKR option expiry qualification failed for conId={bucket[0].con_id}"
            ) from exc
        expiry_at = _option_series_expiry_at_from_details(
            details or [],
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


async def _build_live_option_contracts(
    ib: Any,
    provider_symbol: str,
    underlying: Any,
    spot: float,
    request: IbkrGexRequest | IbkrOptionContractRequest,
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    currency = require_exact_identity_text(
        getattr(underlying, "currency", None),
        field="IBKR_UNDERLYING_CURRENCY",
    )
    chains = await _request_live_option_chains(ib, underlying)
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
    try:
        contracts, contract_meta = await _build_option_contract_candidates_async(
            ib,
            provider_symbol,
            currency,
            spot,
            request,
            chain_expirations,
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
        contracts, contract_meta = await _build_option_contract_candidates_async(
            ib,
            provider_symbol,
            currency,
            spot,
            request,
            expanded_chain_expirations,
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
            contracts, contract_meta = await _build_option_contract_candidates_async(
                ib,
                provider_symbol,
                currency,
                spot,
                request,
                expanded_chain_expirations,
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
    return contracts, contract_meta


async def _build_option_contract_candidates_async(
    ib: Any,
    provider_symbol: str,
    currency: str,
    spot: float,
    request: IbkrGexRequest | IbkrOptionContractRequest,
    chain_expirations: Sequence[tuple[Any, Sequence[str]]],
    *,
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
                details = await ib.reqContractDetailsAsync(query)
                pending_series_details[cache_key] = details or []
            parsed_series = _contracts_from_option_series_details(
                details or [],
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
    candidates, expiry_meta = await _attach_option_expiry_metadata_async(
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


def _cached_option_chains(key: int) -> list[Any] | None:
    if not key:
        return None
    now = _time_monotonic()
    with _IBKR_OPTION_CHAIN_CACHE_LOCK:
        cached = _IBKR_OPTION_CHAIN_CACHE.get(key)
        if cached is None:
            return None
        expires_at, chains = cached
        if expires_at <= now:
            _IBKR_OPTION_CHAIN_CACHE.pop(key, None)
            return None
        return list(chains)


def _cached_option_expiry_fact(
    key: tuple[str, ...],
) -> _IbkrOptionExpiryFact | None:
    with _IBKR_OPTION_CHAIN_CACHE_LOCK:
        return _IBKR_OPTION_EXPIRY_FACT_CACHE.get(key)


def _cached_option_series_details(key: tuple[str, ...]) -> list[Any] | None:
    now = _time_monotonic()
    with _IBKR_OPTION_CHAIN_CACHE_LOCK:
        cached = _IBKR_OPTION_SERIES_CACHE.get(key)
        if cached is None:
            return None
        expires_at, details = cached
        if expires_at <= now:
            _IBKR_OPTION_SERIES_CACHE.pop(key, None)
            return None
        return list(details)


def _contracts_from_option_series_details(
    details: Sequence[Any],
    *,
    currency: str,
    chain: Any,
    futures_options: bool,
    expiration: str,
) -> list[IbkrOptionContract]:
    expected_expiry = expiration
    if (
        not isinstance(expected_expiry, str)
        or len(expected_expiry) != 8
        or not expected_expiry.isdigit()
    ):
        raise ValueError("IBKR option series requires an exact YYYYMMDD expiry")
    expected_sec_type = "FOP" if futures_options else "OPT"
    expected_exchange = getattr(chain, "exchange", None)
    expected_trading_class = getattr(chain, "tradingClass", None)
    expected_multiplier_value = getattr(chain, "multiplier", None)
    if (
        not isinstance(expected_exchange, str)
        or not expected_exchange
        or expected_exchange != expected_exchange.strip()
        or not isinstance(expected_trading_class, str)
        or not expected_trading_class
        or expected_trading_class != expected_trading_class.strip()
    ):
        raise ValueError("IBKR option series requires exact chain identity metadata")
    try:
        expected_multiplier = require_ibkr_option_multiplier(expected_multiplier_value)
    except ValueError as exc:
        raise ValueError("IBKR option series requires an exact multiplier") from exc
    expected_strikes: set[float] = set()
    for value in getattr(chain, "strikes", ()) or ():
        try:
            strike = float(value)
        except TypeError, ValueError:
            continue
        if math.isfinite(strike) and strike > 0:
            expected_strikes.add(strike)
    if not expected_strikes:
        raise ValueError("IBKR option series requires exact provider strikes")
    if not details:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR option details returned no contracts for the exact requested series"
        )
    contracts: list[IbkrOptionContract] = []
    exact_expiry_values: set[datetime] = set()
    for detail in details or []:
        contract = getattr(detail, "contract", None)
        if contract is None:
            raise RuntimeError("IBKR option details omitted the qualified contract")
        try:
            parsed = parse_ibkr_option_contract(contract)
        except ValueError as exc:
            raise RuntimeError("IBKR option details returned invalid contract identity") from exc
        parsed = replace(
            parsed,
            market_rule_id=_option_market_rule_id_from_details(
                detail,
                exchange=parsed.exchange,
            ),
            minimum_tick=_option_minimum_tick_from_details(detail),
        )
        if (
            parsed.sec_type != expected_sec_type
            or parsed.expiry != expected_expiry
            or parsed.currency != currency
            or parsed.exchange != expected_exchange
            or parsed.trading_class != expected_trading_class
            or parsed.multiplier != expected_multiplier
        ):
            raise RuntimeError(
                "IBKR option details disagreed with the exact requested option series"
            )
        expiry_at = _option_expiry_at_from_details(detail, parsed.expiry)
        if expiry_at:
            exact_expiry_values.add(
                datetime.fromisoformat(expiry_at.replace("Z", "+00:00")).astimezone(UTC)
            )
        contracts.append(parsed)
    if len(exact_expiry_values) > 1:
        raise RuntimeError(
            "IBKR option details returned conflicting exact expiry timestamps for one option series"
        )
    exact_expiry_at = next(iter(exact_expiry_values), None)
    if exact_expiry_at is not None:
        contracts = [replace(contract, expiry_at=exact_expiry_at) for contract in contracts]
    rights_by_strike: dict[float, set[str]] = defaultdict(set)
    for contract in contracts:
        if contract.right.value in rights_by_strike[contract.strike]:
            raise RuntimeError("IBKR option details returned a duplicate strike/right contract")
        rights_by_strike[contract.strike].add(contract.right.value)
    if any(rights != {"C", "P"} for rights in rights_by_strike.values()):
        raise RuntimeError("IBKR option details returned an incomplete Call/Put pair")
    return contracts


def _filter_active_option_contracts(
    contracts: Sequence[IbkrOptionContract],
    *,
    now: datetime | None = None,
    expiry_mode: str = "hybrid",
    allow_empty: bool = False,
    requested_expiries: Sequence[str] = (),
    confirmed_expired_series_count: int = 0,
    confirmed_expired_contract_count: int = 0,
) -> tuple[list[IbkrOptionContract], dict[str, Any]]:
    exact_mode = require_option_expiry_mode(expiry_mode)
    if type(allow_empty) is not bool:
        raise TypeError("IBKR option active filter allow_empty must be boolean")
    if (
        type(confirmed_expired_series_count) is not int
        or confirmed_expired_series_count < 0
        or type(confirmed_expired_contract_count) is not int
        or confirmed_expired_contract_count < 0
    ):
        raise ValueError("IBKR confirmed expired option counts must not be negative")
    active_tuple, expired_tuple, unknown_tuple = partition_ibkr_option_contracts_by_expiry(
        contracts,
        now=now,
    )
    active = list(active_tuple)
    expired = len(expired_tuple) + confirmed_expired_contract_count
    unknown = len(unknown_tuple)
    requested_expiry_values = sorted(
        {
            *(contract.expiry for contract in contracts),
            *(str(expiry) for expiry in requested_expiries),
        }
    )
    expired_series = {contract.series_key for contract in expired_tuple}
    unknown_series = {contract.series_key for contract in unknown_tuple}
    expired_series_count = len(expired_series) + confirmed_expired_series_count
    if unknown:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR option contract universe is missing exact option expiry metadata",
            reason="EXPIRY_TIME_UNKNOWN",
            diagnostics={
                "requested_expiries": requested_expiry_values,
                "expired_contracts_excluded": expired,
                "expired_series_excluded": expired_series_count,
                "expiry_time_unknown_contracts_excluded": unknown,
                "expiry_time_unknown_series": len(unknown_series),
            },
        )
    active_expiries = sorted({contract.expiry for contract in active})
    active_chain_labels = sorted(
        {
            "/".join(
                filter(
                    None,
                    (
                        contract.exchange,
                        contract.trading_class,
                        f"{contract.multiplier:g}",
                    ),
                )
            )
            for contract in active
        }
    )
    rights_by_series_strike: dict[tuple[str, str, str, float, float], set[str]] = defaultdict(set)
    strikes_by_expiry: dict[str, set[float]] = defaultdict(set)
    for contract in active:
        rights_by_series_strike[(*contract.series_key, contract.strike)].add(contract.right.value)
        strikes_by_expiry[contract.expiry].add(contract.strike)
    if any(rights != {"C", "P"} for rights in rights_by_series_strike.values()):
        raise RuntimeError("IBKR active GEX contract universe is not call/put paired")
    if not active and not allow_empty:
        reason = "NO_ACTIVE_0DTE" if exact_mode == "0dte" else "NO_ACTIVE_EXPIRY"
        label = "exact 0DTE option series" if exact_mode == "0dte" else "exact option series"
        raise IbkrOptionUniverseUnavailableError(
            f"IBKR returned no active {label}",
            reason=reason,
            diagnostics={
                "requested_expiries": requested_expiry_values,
                "expired_contracts_excluded": expired,
                "expired_series_excluded": expired_series_count,
                "expiry_time_unknown_contracts_excluded": 0,
                "expiry_time_unknown_series": 0,
            },
        )
    return active, {
        "requested_expiries": requested_expiry_values,
        "expired_contracts_excluded": expired,
        "expired_series_excluded": expired_series_count,
        "expiry_time_unknown_contracts_excluded": unknown,
        "expiry_time_unknown_series": len(unknown_series),
        "selected_expiries": active_expiries,
        "expirations": ",".join(active_expiries),
        "chain_labels": ",".join(label for label in active_chain_labels if label),
        "qualified_strike_count": len(
            {strike for strikes in strikes_by_expiry.values() for strike in strikes}
        ),
        "qualified_pair_count": len(rights_by_series_strike),
    }


def _option_chain_cache_key(con_id: Any) -> int:
    return parse_exact_positive_decimal_provider_id(con_id)


def _option_expiry_at_from_details(detail: Any, expiry: str) -> str:
    raw_time = str(getattr(detail, "lastTradeTime", "") or "").strip()
    timezone_name = str(getattr(detail, "timeZoneId", "") or "").strip()
    if len(expiry) != 8 or not expiry.isdigit() or not timezone_name:
        return ""
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        return ""
    expiry_date = datetime.strptime(expiry, "%Y%m%d").date()
    real_expiration_date = str(getattr(detail, "realExpirationDate", "") or "").strip()
    if real_expiration_date and real_expiration_date != expiry:
        return ""
    for time_token in raw_time.replace(",", " ").split():
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(time_token, pattern).time()
            except ValueError:
                continue
            return (
                datetime.combine(
                    expiry_date,
                    parsed_time,
                    tzinfo=timezone,
                )
                .astimezone(UTC)
                .isoformat()
            )

    for session_field in ("liquidHours", "tradingHours"):
        raw_sessions = str(getattr(detail, session_field, "") or "").strip()
        exact_ends: list[datetime] = []
        for raw_session in raw_sessions.split(";"):
            if not raw_session or "CLOSED" in raw_session or "-" not in raw_session:
                continue
            _start, raw_end = raw_session.split("-", 1)
            try:
                session_end = datetime.strptime(
                    raw_end,
                    "%Y%m%d:%H%M",
                ).replace(tzinfo=timezone)
            except ValueError:
                continue
            if session_end.date() == expiry_date:
                exact_ends.append(session_end)
        if exact_ends:
            return max(exact_ends).astimezone(UTC).isoformat()
    return ""


def _option_expiry_fact_for_series(
    cache_key: tuple[str, ...],
    series_key: tuple[str, str, str, float],
    request_facts: Mapping[
        tuple[str, str, str, float],
        _IbkrOptionExpiryFact,
    ],
) -> _IbkrOptionExpiryFact | None:
    cached = _cached_option_expiry_fact(cache_key)
    persisted = request_facts.get(series_key)
    if cached is not None and persisted is not None and cached.expiry_at != persisted.expiry_at:
        raise RuntimeError(
            "IBKR option expiry cache conflicts with the durable exact provider series fact"
        )
    if persisted is not None:
        _store_option_expiry_fact(
            cache_key,
            persisted.expiry_at,
            contract_count=persisted.contract_count,
        )
        return _cached_option_expiry_fact(cache_key)
    return cached


def _option_expiry_fact_payloads(
    facts_by_series: Mapping[
        tuple[str, str, str, float],
        _IbkrOptionExpiryFact,
    ],
) -> list[dict[str, Any]]:
    newest = sorted(
        facts_by_series.items(),
        key=lambda item: item[1].expiry_at,
        reverse=True,
    )[:_IBKR_OPTION_EXPIRY_FACT_CACHE_MAX]
    return [
        {
            "expiry": series_key[0],
            "trading_class": series_key[1],
            "exchange": series_key[2],
            "multiplier": series_key[3],
            "expiry_at": fact.expiry_at.astimezone(UTC).isoformat(),
            "contract_count": fact.contract_count,
            "expiry_time_source": "ibkr_contract_details",
        }
        for series_key, fact in sorted(
            newest,
            key=lambda item: item[0],
        )
    ]


def _option_series_cache_key(route_key: str, query: Any) -> tuple[str, ...]:
    return (
        route_key,
        str(getattr(query, "symbol", "") or ""),
        str(getattr(query, "secType", "") or ""),
        str(getattr(query, "lastTradeDateOrContractMonth", "") or ""),
        str(getattr(query, "exchange", "") or ""),
        str(getattr(query, "currency", "") or ""),
        str(getattr(query, "multiplier", "") or ""),
        str(getattr(query, "tradingClass", "") or ""),
    )


def _option_series_expiry_at_from_details(
    details: Sequence[Any],
    expiry: str,
) -> datetime | None:
    conflicting_real_expirations = {
        raw_real_expiration
        for detail in details
        if (raw_real_expiration := str(getattr(detail, "realExpirationDate", "") or "").strip())
        and raw_real_expiration != expiry
    }
    if conflicting_real_expirations:
        raise RuntimeError(
            "IBKR option details returned a conflicting realExpirationDate "
            "for one exact option series"
        )
    exact_values = {
        datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
        for detail in details
        if (raw := _option_expiry_at_from_details(detail, expiry))
    }
    if len(exact_values) > 1:
        raise RuntimeError(
            "IBKR option details returned conflicting exact expiry timestamps for one option series"
        )
    return next(iter(exact_values), None)


def _option_series_key_from_query(
    query: Any,
) -> tuple[str, str, str, float]:
    multiplier = require_ibkr_option_multiplier(getattr(query, "multiplier", None))
    identity = require_gex_option_series_identity(
        {
            "expiry": getattr(
                query,
                "lastTradeDateOrContractMonth",
                None,
            ),
            "trading_class": getattr(query, "tradingClass", None),
            "exchange": getattr(query, "exchange", None),
            "multiplier": multiplier,
        }
    )
    return (
        identity["expiry"],
        identity["trading_class"],
        identity["exchange"],
        identity["multiplier"],
    )


def _option_series_query(
    provider_symbol: str,
    currency: str,
    chain: Any,
    expiration: str,
    futures_options: bool,
) -> Any:
    _ensure_event_loop()
    from ib_async import Contract

    exchange = getattr(chain, "exchange", None)
    multiplier = getattr(chain, "multiplier", None)
    trading_class = getattr(chain, "tradingClass", None)
    exact_currency = require_exact_identity_text(
        currency,
        field="IBKR_UNDERLYING_CURRENCY",
    )
    if (
        not isinstance(exchange, str)
        or not exchange
        or exchange != exchange.strip()
        or not isinstance(multiplier, str)
        or not multiplier
        or multiplier != multiplier.strip()
        or not isinstance(trading_class, str)
        or not trading_class
        or trading_class != trading_class.strip()
    ):
        raise RuntimeError(
            "IBKR option chain is missing exact exchange, trading class, or multiplier metadata"
        )
    return Contract(
        symbol=provider_symbol,
        secType="FOP" if futures_options else "OPT",
        lastTradeDateOrContractMonth=str(expiration),
        exchange=exchange,
        currency=exact_currency,
        multiplier=multiplier,
        tradingClass=trading_class,
    )


async def _qualified_live_underlying(
    ib: Any,
    *,
    instrument: dict[str, Any],
) -> Any:
    qualified_instrument = require_provider_identity(instrument, provider="ibkr")
    provider_symbol = exact_provider_symbol(qualified_instrument, "ibkr")
    if instrument_is_futures(qualified_instrument):
        return await _current_future_contract_async(ib, instrument=qualified_instrument)
    contract = _contract_for_instrument(qualified_instrument)
    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise RuntimeError(f"Could not qualify {provider_symbol} underlying for GEX live mode.")
    return qualified[0]


async def _request_live_option_chains(ib: Any, underlying: Any) -> list[Any]:
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
        raise RuntimeError(
            "IBKR qualified live underlying has invalid exact conId, symbol, or secType"
        )
    if underlying_sec_type == "FUT":
        exchange = getattr(underlying, "exchange", None)
        if not isinstance(exchange, str) or not exchange or exchange != exchange.strip():
            raise RuntimeError("IBKR qualified live futures underlying is missing exchange")
    else:
        exchange = ""
    cache_key = _option_chain_cache_key(con_id)
    cached = _cached_option_chains(cache_key)
    if cached is not None:
        return cached
    chains = list(
        await ib.reqSecDefOptParamsAsync(
            underlying_symbol,
            exchange,
            underlying_sec_type,
            con_id,
        )
        or []
    )
    if not chains:
        raise IbkrOptionUniverseUnavailableError(
            f"IBKR returned no live option chains for underlying conId={con_id}."
        )
    _store_option_chains(cache_key, chains)
    return chains


def _request_option_expiry_facts_by_series(
    request: IbkrGexRequest | IbkrOptionContractRequest,
) -> dict[tuple[str, str, str, float], _IbkrOptionExpiryFact]:
    facts_by_series: dict[
        tuple[str, str, str, float],
        _IbkrOptionExpiryFact,
    ] = {}
    for fact in getattr(request, "option_expiry_facts", ()):
        series_key = (
            fact.expiry,
            fact.trading_class,
            fact.exchange,
            fact.multiplier,
        )
        incoming = _IbkrOptionExpiryFact(
            expiry_at=fact.expiry_at.astimezone(UTC),
            contract_count=fact.contract_count,
        )
        existing = facts_by_series.get(series_key)
        if existing is not None and existing.expiry_at != incoming.expiry_at:
            raise RuntimeError(
                "Persisted IBKR option expiry facts conflict for one exact provider series"
            )
        if existing is None or incoming.contract_count > existing.contract_count:
            facts_by_series[series_key] = incoming
    return facts_by_series


def _retain_option_expiry_fact(
    facts_by_series: dict[
        tuple[str, str, str, float],
        _IbkrOptionExpiryFact,
    ],
    series_key: tuple[str, str, str, float],
    fact: _IbkrOptionExpiryFact,
) -> None:
    existing = facts_by_series.get(series_key)
    if existing is not None and existing.expiry_at != fact.expiry_at:
        raise RuntimeError("IBKR option expiry facts conflict for one exact provider series")
    if existing is None or fact.contract_count > existing.contract_count:
        facts_by_series[series_key] = fact


def _select_global_option_series_contracts(
    contract_series: Sequence[Sequence[IbkrOptionContract]],
    spot: float,
    strike_count: int,
    max_contracts: int,
) -> list[IbkrOptionContract]:
    """Select the nearest global strike union and every available exact C/P pair."""

    if type(strike_count) is not int or strike_count <= 0:
        raise ValueError("GEX global strike count must be a positive integer")
    if type(max_contracts) is not int or max_contracts <= 0:
        raise ValueError("GEX global contract budget must be a positive integer")
    if not contract_series:
        return []
    series_by_strike: list[dict[float, dict[str, IbkrOptionContract]]] = []
    global_paired_strikes: set[float] = set()
    seen_series: set[tuple[str, str, str, float]] = set()
    for contracts in contract_series:
        by_strike: dict[float, dict[str, IbkrOptionContract]] = defaultdict(dict)
        series_keys = {contract.series_key for contract in contracts}
        if len(series_keys) > 1:
            raise ValueError("IBKR exact option selection input mixes distinct option series")
        if series_keys:
            series_key = next(iter(series_keys))
            if series_key in seen_series:
                raise ValueError("IBKR exact option selection contains a duplicate series")
            seen_series.add(series_key)
        for contract in contracts:
            right = contract.right.value
            if right in by_strike[contract.strike]:
                raise ValueError(
                    "IBKR exact option series contains a duplicate strike/right contract"
                )
            by_strike[contract.strike][right] = contract
        paired_strikes = {
            strike for strike, rights in by_strike.items() if set(rights) == {"C", "P"}
        }
        global_paired_strikes.update(paired_strikes)
        series_by_strike.append(by_strike)

    global_depth = min(strike_count, len(global_paired_strikes))
    if global_depth <= 0:
        return []
    selected_strikes = _selected_strikes(
        SimpleNamespace(strikes=sorted(global_paired_strikes)),
        spot,
        strike_count=global_depth,
    )
    selected = [
        by_strike[strike][right]
        for by_strike in series_by_strike
        for strike in selected_strikes
        if strike in by_strike and set(by_strike[strike]) == {"C", "P"}
        for right in ("C", "P")
    ]
    if len(selected) > max_contracts:
        raise ValueError(
            "GEX max_contracts cannot carry every available exact-series pair "
            "for the selected global strikes"
        )
    return sorted(
        selected,
        key=lambda item: (
            *item.series_key,
            item.strike,
            item.right.value,
        ),
    )


def _selected_chain_expirations(
    chains: Iterable[Any],
    *,
    required_exchange: str,
    max_expirations: int,
    now: datetime | None = None,
    mode: str = "hybrid",
) -> list[tuple[Any, list[str]]]:
    exact_required_exchange = require_exact_identity_text(
        required_exchange,
        field="IBKR_UNDERLYING_EXCHANGE",
    )
    provider_chains = list(chains or [])
    if not provider_chains:
        raise IbkrOptionUniverseUnavailableError("IBKR returned no option chains.")
    chain_list = [
        chain
        for chain in provider_chains
        if getattr(chain, "exchange", None) == exact_required_exchange
    ]
    if not chain_list:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR returned no option chain on the qualified underlying exchange"
        )

    merged_expirations = sorted(
        {
            str(item)
            for chain in chain_list
            for item in (getattr(chain, "expirations", []) or [])
            if _expiration_date(str(item)) is not None
        },
        key=lambda raw: _expiration_date(raw) or datetime.max.date(),
    )
    if not merged_expirations:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR option chains contain no exact expiration dates."
        )

    selected = _selected_expirations(
        SimpleNamespace(expirations=merged_expirations),
        max_expirations,
        now=now,
        mode=mode,
    )
    selected_set = set(selected)
    exact_chains: dict[tuple[str, str, float], Any] = {}
    descriptors: dict[
        tuple[str, str, float],
        tuple[frozenset[str], frozenset[float]],
    ] = {}
    for chain in chain_list:
        exchange = require_exact_identity_text(
            getattr(chain, "exchange", None),
            field="IBKR_OPTION_CHAIN_EXCHANGE",
        )
        trading_class = require_exact_identity_text(
            getattr(chain, "tradingClass", None),
            field="IBKR_OPTION_CHAIN_TRADING_CLASS",
        )
        raw_multiplier = getattr(chain, "multiplier", None)
        try:
            multiplier = require_ibkr_option_multiplier(raw_multiplier)
        except ValueError as exc:
            raise ValueError("IBKR option chain requires an exact multiplier") from exc

        expirations = frozenset(str(item) for item in (getattr(chain, "expirations", ()) or ()))
        raw_strikes = list(getattr(chain, "strikes", ()) or ())
        strikes: set[float] = set()
        for raw_strike in raw_strikes:
            if (
                isinstance(raw_strike, bool)
                or not isinstance(raw_strike, (int, float))
                or not math.isfinite(float(raw_strike))
                or float(raw_strike) <= 0
            ):
                raise ValueError("IBKR option chain contains an invalid exact strike")
            strikes.add(float(raw_strike))
        if not expirations or not strikes:
            raise ValueError("IBKR option chain descriptor is incomplete")

        key = (exchange, trading_class, multiplier)
        descriptor = (expirations, frozenset(strikes))
        existing = descriptors.get(key)
        if existing is not None:
            if existing != descriptor:
                raise ValueError(
                    "IBKR returned conflicting descriptors for one exact option series"
                )
            continue
        descriptors[key] = descriptor
        exact_chains[key] = chain

    groups = [
        (
            exact_chains[key],
            sorted(descriptors[key][0].intersection(selected_set)),
        )
        for key in sorted(exact_chains)
        if descriptors[key][0].intersection(selected_set)
    ]
    if not groups:
        raise IbkrOptionUniverseUnavailableError(
            "IBKR returned no exact option-chain series for selected expirations."
        )
    return groups


def _selected_expirations(
    chain: Any,
    max_expirations: int,
    now: datetime | None = None,
    *,
    mode: str = "hybrid",
) -> list[str]:
    if type(max_expirations) is not int or max_expirations <= 0:
        raise ValueError("GEX expiration count must be a positive integer")
    current_clock = now or datetime.now(NY_TZ)
    if current_clock.tzinfo is None or current_clock.utcoffset() is None:
        raise ValueError("GEX expiration selection clock must be timezone-aware")
    current = current_clock.astimezone(NY_TZ).date()
    parsed = [
        (item, expiry)
        for item in (getattr(chain, "expirations", []) or [])
        if isinstance(item, str) and (expiry := _expiration_date(item)) is not None
    ]
    exact_mode = require_option_expiry_mode(mode)
    ordered = [
        (raw, expiry)
        for raw, expiry in sorted(parsed, key=lambda item: item[1])
        if expiry >= current
    ]
    if exact_mode == "0dte":
        selected = [raw for raw, expiry in ordered if expiry == current]
    elif exact_mode == "1dte":
        selected = [raw for raw, expiry in ordered if expiry > current]
    else:
        selected = [raw for raw, _expiry in ordered]
    return selected[:max_expirations]


def _selected_strikes(
    chain: Any,
    spot: float,
    *,
    strike_count: int,
) -> list[float]:
    all_strikes = sorted(
        float(item)
        for item in (getattr(chain, "strikes", []) or [])
        if finite_number_or_none(item) is not None
    )
    if type(strike_count) is not int or strike_count <= 0:
        raise ValueError("GEX strike selection requires a positive exact count")
    if not all_strikes:
        return []
    nearest = sorted(
        all_strikes,
        key=lambda strike: (abs(strike - spot), strike),
    )[:strike_count]
    return sorted(nearest)


def _store_option_chains(key: int, chains: Sequence[Any]) -> None:
    if not key or not chains:
        return
    expires_at = _time_monotonic() + _IBKR_OPTION_CHAIN_CACHE_TTL_SECONDS
    with _IBKR_OPTION_CHAIN_CACHE_LOCK:
        _IBKR_OPTION_CHAIN_CACHE[key] = (expires_at, list(chains))
        while len(_IBKR_OPTION_CHAIN_CACHE) > _IBKR_OPTION_CHAIN_CACHE_MAX:
            oldest_key = min(
                _IBKR_OPTION_CHAIN_CACHE, key=lambda item: _IBKR_OPTION_CHAIN_CACHE[item][0]
            )
            _IBKR_OPTION_CHAIN_CACHE.pop(oldest_key, None)


def _store_option_expiry_fact(
    key: tuple[str, ...],
    expiry_at: datetime,
    *,
    contract_count: int = 0,
) -> None:
    if (
        not isinstance(expiry_at, _DATETIME_TYPE)
        or expiry_at.tzinfo is None
        or expiry_at.utcoffset() is None
    ):
        raise ValueError("IBKR option expiry fact must be a timezone-aware provider timestamp")
    exact_expiry_at = expiry_at.astimezone(UTC)
    if type(contract_count) is not int or contract_count < 0:
        raise ValueError("IBKR option expiry fact contract count is invalid")
    with _IBKR_OPTION_CHAIN_CACHE_LOCK:
        existing = _IBKR_OPTION_EXPIRY_FACT_CACHE.get(key)
        if existing is not None and existing.expiry_at.astimezone(UTC) != exact_expiry_at:
            raise RuntimeError("IBKR option expiry fact changed for one exact provider series")
        if existing is not None:
            if contract_count > existing.contract_count:
                _IBKR_OPTION_EXPIRY_FACT_CACHE[key] = _IbkrOptionExpiryFact(
                    expiry_at=existing.expiry_at,
                    contract_count=contract_count,
                )
            return
        _IBKR_OPTION_EXPIRY_FACT_CACHE[key] = _IbkrOptionExpiryFact(
            expiry_at=exact_expiry_at,
            contract_count=contract_count,
        )
        while len(_IBKR_OPTION_EXPIRY_FACT_CACHE) > _IBKR_OPTION_EXPIRY_FACT_CACHE_MAX:
            oldest_key = next(iter(_IBKR_OPTION_EXPIRY_FACT_CACHE))
            _IBKR_OPTION_EXPIRY_FACT_CACHE.pop(oldest_key, None)


def _store_option_series_details(key: tuple[str, ...], details: Sequence[Any]) -> None:
    if not details:
        return
    expires_at = _time_monotonic() + _IBKR_OPTION_SERIES_CACHE_TTL_SECONDS
    with _IBKR_OPTION_CHAIN_CACHE_LOCK:
        _IBKR_OPTION_SERIES_CACHE[key] = (expires_at, list(details))
        while len(_IBKR_OPTION_SERIES_CACHE) > _IBKR_OPTION_SERIES_CACHE_MAX:
            oldest_key = min(
                _IBKR_OPTION_SERIES_CACHE,
                key=lambda item: _IBKR_OPTION_SERIES_CACHE[item][0],
            )
            _IBKR_OPTION_SERIES_CACHE.pop(oldest_key, None)


def _time_monotonic() -> float:
    return time.monotonic()


def _trade_expiration(expirations: Sequence[str]) -> str:
    ordered = sorted({item for item in expirations if _expiration_date(item) is not None})
    return ordered[1] if len(ordered) > 1 else ordered[0] if ordered else ""
