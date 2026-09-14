from __future__ import annotations

import logging
import threading
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr.contracts import (
    _discover_current_future_contract,
    _parse_ibkr_expiry,
)
from aef_terminal.data.ibkr.event_loop import _ensure_event_loop
from aef_terminal.data.ibkr.session import _disconnect_owned_ibkr_session
from aef_terminal.data.instrument_identity import (
    futures_root,
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
)


_SUPPORTED_SEARCH_SEC_TYPES = {"STK", "IND", "FUT"}
# The outer lifecycle claim remains held through disconnect; the existing
# request critical section is nested inside it.
_LOOKUP_LOCK = threading.RLock()
_LOGGER = logging.getLogger(__name__)


def _contract_value(contract: object, field: str) -> str:
    return str(getattr(contract, field, "") or "")


def _description_value(description: object, field: str) -> str:
    return str(getattr(description, field, "") or "").strip()


def _asset_class(sec_type: str) -> str:
    if sec_type == "STK":
        return "stock"
    if sec_type == "IND":
        return "index"
    if sec_type == "CASH":
        return "forex"
    if sec_type == "FUT":
        return "future"
    return sec_type.lower()


def _connect_lookup_session(
    ib_factory: Any, host: str, port: int, client_id: int, timeout: float
) -> Any:
    from aef_terminal.data.ibkr.session import connect_without_account_sync

    ib = ib_factory()
    ib.RequestTimeout = timeout
    connect_without_account_sync(ib, host, port, int(client_id), timeout)
    return ib


def ibkr_search_candidate(description: object) -> dict[str, Any] | None:
    contract = getattr(description, "contract", None)
    if contract is None:
        return None
    symbol = _contract_value(contract, "symbol")
    sec_type = _contract_value(contract, "secType")
    if not symbol or sec_type not in _SUPPORTED_SEARCH_SEC_TYPES:
        return None
    if sec_type == "FUT":
        return ibkr_future_root_candidate_from_contract(description)
    currency = _contract_value(contract, "currency")
    primary_exchange = _contract_value(contract, "primaryExchange")
    exchange = _contract_value(contract, "exchange") or primary_exchange
    con_id = getattr(contract, "conId", None)
    con_id_value = int(con_id) if isinstance(con_id, int) and con_id > 0 else None
    if con_id_value is None or not exchange or not currency:
        return None
    name = (
        _description_value(description, "longName")
        or _description_value(description, "description")
        or symbol
    )
    asset_class = _asset_class(sec_type)
    min_tick_raw = getattr(description, "minTick", None)
    min_tick = (
        float(min_tick_raw) if isinstance(min_tick_raw, (int, float)) and min_tick_raw > 0 else None
    )
    return {
        "instrument_id": f"ibkr|contract|{con_id_value}",
        "key": symbol,
        "instrument_key": symbol,
        "display": symbol,
        "provider": "ibkr",
        "provider_symbol": symbol,
        "provider_contract_id": str(con_id_value or ""),
        "con_id": con_id_value,
        "asset_class": asset_class,
        "name": name,
        "contract_identity": {
            "asset_class": asset_class,
            "provider": "ibkr",
            "provider_contract_id": str(con_id_value),
            "symbol": symbol,
            "sec_type": sec_type,
            "exchange": exchange,
            "primary_exchange": primary_exchange,
            "currency": currency,
            "con_id": con_id_value,
            "min_tick": min_tick,
        },
    }


def _future_contract_payload(contract: object, details: object | None = None) -> dict[str, Any]:
    local_symbol = _contract_value(contract, "localSymbol")
    expiry = _contract_value(contract, "lastTradeDateOrContractMonth")
    con_id = getattr(contract, "conId", None)
    con_id_value = int(con_id) if isinstance(con_id, int) and con_id > 0 else None
    contract_key = local_symbol or (f"conid:{con_id_value}" if con_id_value else expiry)
    parsed_last_trade = _parse_ibkr_expiry(expiry)
    min_tick_raw = getattr(details, "minTick", None)
    min_tick = (
        float(min_tick_raw) if isinstance(min_tick_raw, (int, float)) and min_tick_raw > 0 else None
    )
    return {
        "contract_key": contract_key,
        "provider_contract_id": str(con_id_value or ""),
        "local_symbol": local_symbol,
        "con_id": con_id_value,
        "expiry": expiry,
        "contract_month": expiry[:6] if len(expiry) >= 6 else expiry,
        "last_trade_date": parsed_last_trade.isoformat() if parsed_last_trade is not None else None,
        "trading_class": _contract_value(contract, "tradingClass"),
        "min_tick": min_tick,
    }


def ibkr_future_root_candidate_from_contract(description: object) -> dict[str, Any] | None:
    contract = getattr(description, "contract", None)
    if contract is None:
        return None
    root = _contract_value(contract, "symbol")
    sec_type = _contract_value(contract, "secType")
    if not root or sec_type != "FUT":
        return None
    exchange = _contract_value(contract, "exchange")
    currency = _contract_value(contract, "currency")
    if not exchange or not currency:
        return None
    name = (
        _description_value(description, "longName")
        or _description_value(description, "description")
        or f"{root} futures root"
    )
    current_contract = _future_contract_payload(contract)
    if not current_contract["provider_contract_id"]:
        return None
    return _future_root_candidate(
        root,
        exchange,
        currency,
        name=name,
        trading_class=_contract_value(contract, "tradingClass"),
        current_contract=current_contract,
    )


def _future_root_candidate(
    root: str,
    exchange: str,
    currency: str,
    *,
    name: str = "",
    trading_class: str = "",
    current_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root_key = str(root or "")
    exchange_key = str(exchange or "")
    currency_key = str(currency or "")
    if not root_key or not exchange_key or not currency_key:
        raise ValueError("root, exchange, and currency are required for IBKR futures root")
    trading_class_key = str(trading_class or "")
    return {
        "instrument_id": f"ibkr|future_root|{root_key}|{exchange_key}|{currency_key}|{trading_class_key}",
        "key": root_key,
        "instrument_key": root_key,
        "display": root_key,
        "provider": "ibkr",
        "provider_symbol": root_key,
        "provider_contract_id": "",
        "con_id": None,
        "asset_class": "future",
        "name": name or f"{root_key} futures root",
        "contract_identity": {
            "asset_class": "future",
            "provider": "ibkr",
            "root": root_key,
            "exchange": exchange_key,
            "currency": currency_key,
            "trading_class": trading_class_key,
            "identity_scope": "root",
            "history_contract_mode": "continuous_future",
            "live_contract_mode": "provider_current_contract",
            "local_symbol": None,
            "con_id": None,
            "expiry": None,
            "current_contract": dict(current_contract or {}),
        },
        "continuous_series": {
            "instrument_key": root_key,
            "provider": "ibkr",
            "provider_symbol": root_key,
            "series_type": "provider_bound_continuous",
            "roll_source": "provider",
        },
    }


def search_ibkr_instruments(
    query: str,
    *,
    limit: int = 20,
    timeout: float = 2.5,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
) -> list[dict[str, Any]]:
    value = str(query or "").strip()
    if not value:
        return []
    _ensure_event_loop()
    config = AppConfig()
    resolved_host = host or config.ibkr_host
    resolved_port = int(port or config.ibkr_port)
    resolved_client_id = int(client_id or config.ibkr_lookup_client_id)
    wait_seconds = max(0.2, min(float(timeout), 8.0))
    from ib_async import IB

    ib = None
    _LOOKUP_LOCK.acquire()
    try:
        with _LOOKUP_LOCK:
            ib = _connect_lookup_session(
                IB, resolved_host, resolved_port, resolved_client_id, wait_seconds
            )
            descriptions = ib.reqMatchingSymbols(value)
            seen_identities: set[tuple[Any, ...]] = set()
            results: list[dict[str, Any]] = []
            for description in descriptions or []:
                description_candidates = [ibkr_search_candidate(description)]
                contract = getattr(description, "contract", None)
                derivative_types = {
                    str(item or "")
                    for item in (getattr(description, "derivativeSecTypes", None) or [])
                }
                if contract is not None and "FUT" in derivative_types:
                    root = _contract_value(contract, "symbol")
                    exchange = _contract_value(contract, "exchange") or _contract_value(
                        contract, "primaryExchange"
                    )
                    currency = _contract_value(contract, "currency")
                    if root and exchange and currency:
                        try:
                            probe = _future_root_candidate(root, exchange, currency)
                            current = _discover_current_future_contract(ib, probe)
                            details = next(iter(ib.reqContractDetails(current) or []), None)
                            description_candidates.append(
                                _future_root_candidate(
                                    root,
                                    exchange,
                                    currency,
                                    name=f"{root} futures root",
                                    trading_class=_contract_value(current, "tradingClass"),
                                    current_contract=_future_contract_payload(current, details),
                                )
                            )
                        except Exception:
                            _LOGGER.debug(
                                "IBKR futures-root search enrichment failed for %s on %s",
                                root,
                                exchange,
                                exc_info=True,
                            )
                for candidate in description_candidates:
                    if not candidate:
                        continue
                    if futures_root(candidate):
                        identity = candidate.get("contract_identity") or {}
                        root = futures_root(candidate)
                        root_identity = (
                            "root",
                            root,
                            str(identity.get("exchange") or ""),
                            str(identity.get("currency") or ""),
                            str(identity.get("trading_class") or ""),
                        )
                        if not root or root_identity in seen_identities:
                            continue
                        seen_identities.add(root_identity)
                    else:
                        con_id = int(candidate.get("con_id") or 0)
                        if con_id <= 0 or ("contract", con_id) in seen_identities:
                            continue
                        seen_identities.add(("contract", con_id))
                    results.append(candidate)
                    if len(results) >= max(int(limit), 1):
                        break
                if len(results) >= max(int(limit), 1):
                    break
            return results
    finally:
        try:
            if ib is not None:
                _disconnect_owned_ibkr_session(
                    ib,
                    session="instrument_search",
                    session_key=(
                        resolved_host,
                        resolved_port,
                        resolved_client_id,
                    ),
                )
        except Exception:
            _LOGGER.warning(
                "IBKR instrument-search session disconnect failed",
                exc_info=True,
            )
        finally:
            _LOOKUP_LOCK.release()


def bind_ibkr_instrument(
    provider_contract_id: str, *, timeout: float = 2.5
) -> dict[str, Any] | None:
    exact_contract_id = require_exact_identity_text(
        provider_contract_id,
        field="provider_contract_id",
    )
    con_id = parse_exact_positive_decimal_provider_id(exact_contract_id)
    if con_id <= 0:
        return None
    _ensure_event_loop()
    config = AppConfig()
    from ib_async import Contract, IB

    ib = None
    _LOOKUP_LOCK.acquire()
    try:
        wait_seconds = max(0.2, min(float(timeout), 8.0))
        with _LOOKUP_LOCK:
            ib = _connect_lookup_session(
                IB,
                config.ibkr_host,
                int(config.ibkr_port),
                int(config.ibkr_lookup_client_id),
                wait_seconds,
            )
            details = ib.reqContractDetails(Contract(conId=con_id))
            for detail in details or []:
                candidate = ibkr_search_candidate(detail)
                if candidate and int(candidate.get("con_id") or 0) == con_id:
                    return candidate
            return None
    finally:
        try:
            if ib is not None:
                _disconnect_owned_ibkr_session(
                    ib,
                    session="contract_bind",
                    session_key=(
                        config.ibkr_host,
                        config.ibkr_port,
                        config.ibkr_lookup_client_id,
                    ),
                )
        except Exception:
            _LOGGER.warning(
                "IBKR contract-bind session disconnect failed",
                exc_info=True,
            )
        finally:
            _LOOKUP_LOCK.release()


def bind_ibkr_future_root(
    root: str,
    *,
    binding: dict[str, Any] | None = None,
    timeout: float = 2.5,
) -> dict[str, Any] | None:
    root_key = str(root or "")
    if not root_key:
        return None
    _ensure_event_loop()
    config = AppConfig()
    from ib_async import IB

    ib = None
    _LOOKUP_LOCK.acquire()
    try:
        wait_seconds = max(0.2, min(float(timeout), 8.0))
        with _LOOKUP_LOCK:
            ib = _connect_lookup_session(
                IB,
                config.ibkr_host,
                int(config.ibkr_port),
                int(config.ibkr_lookup_client_id),
                wait_seconds,
            )
            binding_identity = (
                binding.get("contract_identity")
                if isinstance(binding, dict) and isinstance(binding.get("contract_identity"), dict)
                else {}
            )
            binding_exchange = str(binding_identity.get("exchange") or "")
            binding_currency = str(binding_identity.get("currency") or "")
            binding_trading_class = str(binding_identity.get("trading_class") or "")
            if binding_exchange and binding_currency:
                probe = _future_root_candidate(
                    root_key,
                    binding_exchange,
                    binding_currency,
                    trading_class=binding_trading_class,
                )
                contract = _discover_current_future_contract(ib, probe)
                details = next(iter(ib.reqContractDetails(contract) or []), None)
                return _future_root_candidate(
                    root_key,
                    binding_exchange,
                    binding_currency,
                    name=str((binding or {}).get("name") or ""),
                    trading_class=binding_trading_class,
                    current_contract=_future_contract_payload(contract, details),
                )
            descriptions = ib.reqMatchingSymbols(root_key)
            candidates = [ibkr_search_candidate(description) for description in descriptions or []]
            roots = [
                candidate
                for candidate in candidates
                if isinstance(candidate, dict)
                and str((candidate.get("contract_identity") or {}).get("root") or "") == root_key
            ]
            if not roots:
                return None
            if len(roots) != 1:
                raise ValueError(f"IBKR_FUTURES_ROOT_BINDING_AMBIGUOUS root={root_key}")
            candidate = roots[0]
            identity = (
                candidate.get("contract_identity")
                if isinstance(candidate.get("contract_identity"), dict)
                else {}
            )
            exchange = str(identity.get("exchange") or "")
            currency = str(identity.get("currency") or "")
            probe = _future_root_candidate(
                root_key,
                exchange,
                currency,
                trading_class=str(identity.get("trading_class") or ""),
            )
            contract = _discover_current_future_contract(ib, probe)
            details = next(iter(ib.reqContractDetails(contract) or []), None)
            current_contract = _future_contract_payload(contract, details)
            candidate = _future_root_candidate(
                root_key,
                exchange,
                currency,
                name=str(candidate.get("name") or ""),
                trading_class=str(identity.get("trading_class") or ""),
                current_contract=current_contract,
            )
            return candidate
    finally:
        try:
            if ib is not None:
                _disconnect_owned_ibkr_session(
                    ib,
                    session="future_root_bind",
                    session_key=(
                        config.ibkr_host,
                        config.ibkr_port,
                        config.ibkr_lookup_client_id,
                    ),
                )
        except Exception:
            _LOGGER.warning(
                "IBKR futures-root bind session disconnect failed",
                exc_info=True,
            )
        finally:
            _LOOKUP_LOCK.release()
