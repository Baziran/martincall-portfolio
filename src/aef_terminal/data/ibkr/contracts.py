from __future__ import annotations
import json
from datetime import UTC, date, datetime
from typing import Any
from aef_terminal.domain import Bar, BarProvenance, BarState
from aef_terminal.data.ibkr.cache import _BoundedCache
from aef_terminal.data.ibkr.runtime import _IBKR_RUNTIME
from aef_terminal.data.ibkr.event_loop import _ensure_event_loop
from aef_terminal.data.instrument_identity import (
    InstrumentIdentityError,
    current_futures_contract,
    current_futures_contract_id,
    futures_root,
    identity_payload,
    instrument_is_futures_root,
    provider_symbol,
    provider_contract_id,
    parse_exact_positive_decimal_provider_id as _exact_ibkr_con_id,
    require_exact_identity_text,
    require_provider_identity,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.runtime.timeframes import parse_aware_utc_ts


def _require_ibkr_instrument(instrument: dict[str, Any] | None) -> dict[str, Any]:
    return require_provider_identity(instrument, provider="ibkr")


def _future_spec_from_instrument(instrument: dict[str, Any] | None) -> tuple[str, str, str] | None:
    resolved_instrument = _require_ibkr_instrument(instrument)
    if instrument_is_futures_root(resolved_instrument):
        identity = identity_payload(resolved_instrument)
        root = futures_root(resolved_instrument)
        exchange = str(identity.get("exchange") or "")
        currency = str(identity.get("currency") or "")
        if root and exchange:
            return (root, exchange, currency)
    return None


def _instrument_contract_cache_token(instrument: dict[str, Any] | None) -> str:
    resolved_instrument = _require_ibkr_instrument(instrument)
    request_scope = _instrument_contract_request_scope(resolved_instrument)
    return json.dumps(
        [
            qualified_instrument_id(resolved_instrument),
            route_fingerprint(resolved_instrument),
            *request_scope,
        ],
        separators=(",", ":"),
    )


def _instrument_contract_request_scope(
    instrument: dict[str, Any] | None,
) -> tuple[str, str, str, str]:
    resolved_instrument = _require_ibkr_instrument(instrument)
    identity = identity_payload(resolved_instrument)
    sec_type = (
        "CONTFUT"
        if instrument_is_futures_root(resolved_instrument)
        else require_exact_identity_text(
            identity.get("sec_type"),
            field="ibkr_sec_type",
        )
    )
    configured_exchange = require_exact_identity_text(
        identity.get("exchange"),
        field="ibkr_exchange",
    )
    primary_exchange = require_exact_identity_text(
        identity.get("primary_exchange", ""),
        field="ibkr_primary_exchange",
        allow_empty=True,
    )
    currency = require_exact_identity_text(
        identity.get("currency"),
        field="ibkr_currency",
    )
    request_exchange = (
        primary_exchange if sec_type == "IND" and primary_exchange else configured_exchange
    )
    return sec_type, request_exchange, primary_exchange, currency


def _validate_qualified_contract_request_scope(
    contract: object,
    *,
    instrument: dict[str, Any],
) -> None:
    resolved_instrument = _require_ibkr_instrument(instrument)
    expected_scope = _instrument_contract_request_scope(resolved_instrument)
    actual_scope = tuple(
        value if isinstance(value, str) else ""
        for value in (
            getattr(contract, "secType", ""),
            getattr(contract, "exchange", ""),
            getattr(contract, "primaryExchange", ""),
            getattr(contract, "currency", ""),
        )
    )
    field_names = ("sec_type", "exchange", "primary_exchange", "currency")
    for field_name, expected, actual in zip(
        field_names,
        expected_scope,
        actual_scope,
        strict=True,
    ):
        if actual != expected:
            raise RuntimeError(
                "IBKR_QUALIFIED_CONTRACT_SCOPE_MISMATCH "
                f"instrument_id={qualified_instrument_id(resolved_instrument)} "
                f"field={field_name} expected={expected!r} actual={actual!r}"
            )
    if instrument_is_futures_root(resolved_instrument):
        return
    expected_con_id = _exact_ibkr_con_id(provider_contract_id(resolved_instrument))
    actual_con_id = _exact_ibkr_con_id(getattr(contract, "conId", None))
    if actual_con_id != expected_con_id:
        raise RuntimeError(
            "IBKR_QUALIFIED_CONTRACT_SCOPE_MISMATCH "
            f"instrument_id={qualified_instrument_id(resolved_instrument)} "
            f"field=provider_contract_id expected={expected_con_id!r} "
            f"actual={actual_con_id!r}"
        )


def _contract_from_instrument_identity(instrument: dict[str, Any] | None):
    resolved_instrument = _require_ibkr_instrument(instrument)
    instrument_id = qualified_instrument_id(resolved_instrument)
    identity = identity_payload(resolved_instrument)
    if instrument_is_futures_root(resolved_instrument):
        return None
    con_id = provider_contract_id(resolved_instrument)
    con_id_value = _exact_ibkr_con_id(con_id)
    sec_type = str(identity.get("sec_type") or "")
    if con_id_value <= 0 or not sec_type:
        raise InstrumentIdentityError(
            f"IBKR_CONTRACT_IDENTITY_INCOMPLETE instrument_id={instrument_id}"
        )
    _ensure_event_loop()
    from ib_async import Contract

    contract = Contract()
    contract.conId = con_id_value
    contract.secType = sec_type
    contract.symbol = str(identity.get("symbol") or "")
    exchange_val = str(identity.get("exchange") or "")
    primary_exchange = str(identity.get("primary_exchange") or "")
    currency = str(identity.get("currency") or "")
    if not contract.symbol or not exchange_val or not currency:
        raise InstrumentIdentityError(
            f"IBKR_CONTRACT_METADATA_REQUIRED instrument_id={instrument_id}"
        )
    if sec_type == "IND" and primary_exchange:
        contract.exchange = primary_exchange
    else:
        contract.exchange = exchange_val
    if primary_exchange:
        contract.primaryExchange = primary_exchange
    contract.currency = currency
    return contract


async def _resolve_qualified_contract_async(
    ib,
    *,
    instrument: dict[str, Any],
) -> object:
    resolved_instrument = _require_ibkr_instrument(instrument)
    contract = _contract_for_instrument(resolved_instrument)
    qualified = await ib.qualifyContractsAsync(contract)
    if not qualified:
        raise RuntimeError(
            f"IBKR could not qualify async contract for instrument_id={qualified_instrument_id(resolved_instrument)}"
        )
    result = qualified[0]
    _validate_qualified_contract_request_scope(
        result,
        instrument=resolved_instrument,
    )
    return result


async def _qualified_contract_async(
    ib,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    *,
    instrument: dict[str, Any],
):
    runtime = _IBKR_RUNTIME
    resolved_instrument = _require_ibkr_instrument(instrument)
    identity_token = _instrument_contract_cache_token(resolved_instrument)
    cache_key = (host, int(port), int(client_id), bool(readonly), identity_token)
    cached = runtime.async_history_contract_cache.get(cache_key)
    if cached is not None:
        _validate_qualified_contract_request_scope(
            cached,
            instrument=resolved_instrument,
        )
        return cached
    result = await _resolve_qualified_contract_async(
        ib,
        instrument=resolved_instrument,
    )
    runtime.async_history_contract_cache[cache_key] = result
    return result


async def _chart_contract_for_instrument_async(
    ib,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    *,
    instrument: dict[str, Any],
):
    runtime = _IBKR_RUNTIME
    resolved_instrument = _require_ibkr_instrument(instrument)
    identity_token = _instrument_contract_cache_token(resolved_instrument)
    cache_key = (host, int(port), int(client_id), bool(readonly), identity_token)
    if _drop_stale_cached_future_contract(
        runtime.async_chart_contract_cache,
        cache_key,
        instrument=resolved_instrument,
    ):
        _invalidate_chart_streams_for_route(
            qualified_instrument_id(resolved_instrument),
            route_fingerprint(resolved_instrument),
            session_prefix=(host, int(port), int(client_id), bool(readonly)),
        )
    cached = runtime.async_chart_contract_cache.get(cache_key)
    if cached is not None:
        _validate_qualified_contract_request_scope(
            cached,
            instrument=resolved_instrument,
        )
        return cached
    result = await _resolve_qualified_contract_async(
        ib,
        instrument=resolved_instrument,
    )
    runtime.async_chart_contract_cache[cache_key] = result
    return result


def _quote_contract_for_instrument(ib, instrument: dict[str, Any]):
    """Return a contract that is valid for realtime market-data subscriptions.

    IBKR accepts continuous futures for historical bars, but realtime
    ``reqMktData`` requires a concrete front-month futures contract. Stocks,
    indexes, and crypto can keep the cheaper direct contract path.
    """
    resolved_instrument = _require_ibkr_instrument(instrument)
    if instrument_is_futures_root(resolved_instrument):
        return _current_future_contract(
            ib,
            instrument=resolved_instrument,
        )
    return _contract_for_instrument(resolved_instrument)


def _contract_for_instrument(instrument: dict[str, Any]):
    _ensure_event_loop()
    from ib_async import ContFuture

    resolved_instrument = _require_ibkr_instrument(instrument)
    contract = _contract_from_instrument_identity(resolved_instrument)
    if contract is not None:
        return contract
    future_spec = _future_spec_from_instrument(resolved_instrument)
    if future_spec:
        root, exchange, currency = future_spec
        contract = ContFuture(root, exchange, currency=currency)
        identity = (
            resolved_instrument.get("contract_identity")
            if isinstance(resolved_instrument.get("contract_identity"), dict)
            else {}
        )
        trading_class = str(identity.get("trading_class") or "")
        if trading_class:
            contract.tradingClass = trading_class
        return contract
    raise RuntimeError(
        f"IBKR_PROVIDER_QUALIFIED_INSTRUMENT_REQUIRED instrument_id={qualified_instrument_id(resolved_instrument)}"
    )


def _qualified_continuous_future_contract(ib, instrument: dict[str, Any]):
    resolved_instrument = _require_ibkr_instrument(instrument)
    contract = _contract_for_instrument(resolved_instrument)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(
            f"IBKR could not qualify continuous futures contract for instrument_id={qualified_instrument_id(resolved_instrument)}"
        )
    return qualified[0]


def _exact_future_contract_query(
    payload: dict[str, Any],
    instrument: dict[str, Any],
):
    _ensure_event_loop()
    from ib_async import Contract

    resolved_instrument = _require_ibkr_instrument(instrument)
    instrument_id = qualified_instrument_id(resolved_instrument)
    future_spec = _future_spec_from_instrument(resolved_instrument)
    if not future_spec:
        raise RuntimeError(
            f"instrument_id={instrument_id} is not configured as an IBKR futures contract"
        )
    root, exchange, currency = future_spec
    local_symbol = str(payload.get("local_symbol") or "")
    if not local_symbol:
        raise RuntimeError(
            f"FUTURES_CONTINUOUS_LOCAL_SYMBOL_REQUIRED provider=ibkr instrument_id={instrument_id}"
        )
    query = Contract(
        secType="FUT",
        exchange=str(payload.get("exchange") or exchange),
        currency=str(payload.get("currency") or currency),
        localSymbol=local_symbol,
    )
    return query, root, local_symbol


def _validate_exact_future_contract(result: object, *, symbol: str, root: str, local_symbol: str):
    con_id = int(getattr(result, "conId", 0) or 0)
    if str(getattr(result, "secType", "") or "") != "FUT" or con_id <= 0:
        raise RuntimeError(
            f"FUTURES_CONCRETE_CONTRACT_MISMATCH provider=ibkr instrument_key={symbol} local_symbol={local_symbol}"
        )
    if str(getattr(result, "symbol", "") or "") != root:
        raise RuntimeError(
            f"FUTURES_CONCRETE_CONTRACT_ROOT_MISMATCH provider=ibkr instrument_key={symbol} con_id={con_id}"
        )
    if str(getattr(result, "localSymbol", "") or "") != local_symbol:
        raise RuntimeError(
            f"FUTURES_CONCRETE_LOCAL_SYMBOL_MISMATCH provider=ibkr instrument_key={symbol} local_symbol={local_symbol}"
        )
    return result


def _continuous_future_contract_payload(contract: object) -> dict[str, Any]:
    if str(getattr(contract, "secType", "") or "").upper() != "CONTFUT":
        raise RuntimeError("IBKR_CONTINUOUS_FUTURES_CONTRACT_REQUIRED")
    local_symbol = str(getattr(contract, "localSymbol", "") or "")
    if not local_symbol:
        raise RuntimeError("IBKR_CONTINUOUS_FUTURES_LOCAL_SYMBOL_REQUIRED")
    return {
        "continuous_con_id": int(getattr(contract, "conId", 0) or 0),
        "local_symbol": local_symbol,
        "expiry": str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        "trading_class": str(getattr(contract, "tradingClass", "") or ""),
        "exchange": str(getattr(contract, "exchange", "") or ""),
        "currency": str(getattr(contract, "currency", "") or ""),
    }


def _qualify_exact_future_contract(ib, payload: dict[str, Any], instrument: dict[str, Any]):
    resolved_instrument = _require_ibkr_instrument(instrument)
    instrument_id = qualified_instrument_id(resolved_instrument)
    query, root, local_symbol = _exact_future_contract_query(payload, resolved_instrument)
    qualified = ib.qualifyContracts(query)
    if not qualified:
        raise RuntimeError(
            f"FUTURES_CONCRETE_CONTRACT_QUALIFICATION_FAILED provider=ibkr instrument_id={instrument_id} local_symbol={local_symbol}"
        )
    return _validate_exact_future_contract(
        qualified[0],
        symbol=provider_symbol(resolved_instrument, "ibkr"),
        root=root,
        local_symbol=local_symbol,
    )


def _discover_current_future_contract(ib, instrument: dict[str, Any]):
    """Resolve the provider's current FUT for the server-owned lifecycle."""
    resolved_instrument = _require_ibkr_instrument(instrument)
    continuous = _qualified_continuous_future_contract(ib, resolved_instrument)
    payload = _continuous_future_contract_payload(continuous)
    return _qualify_exact_future_contract(ib, payload, resolved_instrument)


def _current_future_contract(ib, *, instrument: dict[str, Any]):
    """Build the exact live FUT from the lifecycle-owned persisted identity."""
    del ib
    resolved_instrument = _require_ibkr_instrument(instrument)
    instrument_id = qualified_instrument_id(resolved_instrument)
    future_spec = _future_spec_from_instrument(resolved_instrument)
    if not future_spec:
        raise RuntimeError(
            f"instrument_id={instrument_id} is not configured as an IBKR futures root"
        )
    root, exchange, currency = future_spec
    current = current_futures_contract(resolved_instrument)
    local_symbol = require_exact_identity_text(
        current.get("local_symbol", ""),
        field="current_contract.local_symbol",
    )
    con_id = _exact_ibkr_con_id(current_futures_contract_id(resolved_instrument))
    raw_numeric_con_id = current.get("con_id")
    numeric_con_id = _exact_ibkr_con_id(raw_numeric_con_id)
    if raw_numeric_con_id is not None and numeric_con_id != con_id:
        raise InstrumentIdentityError("FUTURES_CURRENT_CONTRACT_PROVIDER_ID_MISMATCH provider=ibkr")
    if not local_symbol or con_id <= 0:
        raise InstrumentIdentityError(
            f"FUTURES_CURRENT_CONTRACT_REQUIRED provider=ibkr instrument_key={root}"
        )
    _ensure_event_loop()
    from ib_async import Contract

    result = Contract(
        secType="FUT",
        conId=con_id,
        symbol=root,
        exchange=str(current.get("exchange") or exchange),
        currency=str(current.get("currency") or currency),
        localSymbol=local_symbol,
        lastTradeDateOrContractMonth=str(
            current.get("expiry") or current.get("contract_month") or ""
        ),
        tradingClass=str(
            current.get("trading_class")
            or identity_payload(resolved_instrument).get("trading_class")
            or ""
        ),
    )
    return _validate_exact_future_contract(
        result,
        symbol=provider_symbol(resolved_instrument, "ibkr"),
        root=root,
        local_symbol=local_symbol,
    )


async def _current_future_contract_async(ib, *, instrument: dict[str, Any]):
    return _current_future_contract(ib, instrument=instrument)


def _parse_ibkr_expiry(value: str) -> date | None:
    if not value:
        return None
    raw = str(value)
    if len(raw) >= 8 and raw[:8].isdigit():
        return datetime.strptime(raw[:8], "%Y%m%d").date()
    return None


FUTURES_ROLLOVER_WARN_DAYS = 3


def _contract_month_token(contract: object) -> str:
    raw = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
    if len(raw) >= 6 and raw[:6].isdigit():
        return raw[:6]
    return ""


def futures_contract_month_is_stale(contract: object, as_of: date | None = None) -> bool:
    if str(getattr(contract, "secType", "") or "").upper() != "FUT":
        return False
    expiry = _parse_ibkr_expiry(str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""))
    if expiry is None:
        return False
    return expiry < (as_of or datetime.now(tz=UTC).date())


def futures_contract_is_unqualified(contract: object) -> bool:
    if contract is None:
        return False
    if str(getattr(contract, "secType", "") or "").upper() != "FUT":
        return False
    local_symbol = str(getattr(contract, "localSymbol", "") or "")
    month_token = _contract_month_token(contract)
    return not local_symbol or len(month_token) < 6


def _format_future_contract_label(contract: object) -> str:
    local_symbol = str(getattr(contract, "localSymbol", "") or "")
    if local_symbol:
        return local_symbol
    month_token = _contract_month_token(contract)
    root = str(getattr(contract, "symbol", "") or "")
    if root and month_token:
        return f"{root}{month_token[4:6]}"
    return month_token or root


def futures_rollover_warning_for_contract(
    contract: object, *, as_of: date | None = None, warn_days: int = FUTURES_ROLLOVER_WARN_DAYS
) -> dict[str, object] | None:
    if contract is None or str(getattr(contract, "secType", "") or "").upper() != "FUT":
        return None
    as_of_date = as_of or datetime.now(tz=UTC).date()
    month_token = _contract_month_token(contract)
    if len(month_token) < 6:
        return None
    expiry = _parse_ibkr_expiry(str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""))
    if expiry is None:
        return None
    days_left = (expiry - as_of_date).days
    if days_left > int(warn_days):
        return None
    local_symbol = str(getattr(contract, "localSymbol", "") or "") if contract is not None else ""
    label = local_symbol or month_token
    if days_left < 0:
        message = f"Futures contract {label} expired on {expiry.isoformat()}; switch to the current front month."
        status = "expired"
    else:
        message = f"Futures contract {label} expires in {days_left} day(s) on {expiry.isoformat()}; rollover to the next front month is due."
        status = "rollover_due"
    return {
        "status": status,
        "days_left": days_left,
        "expiry_date": expiry.isoformat(),
        "contract_month": month_token,
        "message": message,
    }


def _invalidate_chart_streams_for_route(
    instrument_id: str,
    route_fingerprint: str,
    *,
    session_prefix: tuple[str, int, int, bool] | None = None,
) -> None:
    runtime = _IBKR_RUNTIME
    identity = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    route_key = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    for key in list(runtime.async_chart_streams):
        route = runtime.async_chart_stream_routes.get(key)
        if route is None or route[:2] != (identity, route_key):
            continue
        if session_prefix is not None and key[:4] != session_prefix:
            continue
        runtime.discard_chart_stream(key, cancel=True)


def _mark_trading_hours_stale_on_future_rollover(instrument: dict[str, Any]) -> None:
    from aef_terminal.data.provider_sessions import mark_provider_trading_hours_stale

    mark_provider_trading_hours_stale(
        instrument=instrument,
        reason="futures_contract_rollover",
    )


def _drop_stale_cached_future_contract(
    cache: _BoundedCache,
    cache_key: tuple,
    *,
    instrument: dict[str, Any],
    as_of: date | None = None,
) -> bool:
    cached = cache.get(cache_key)
    if cached is None:
        return False
    if not futures_contract_is_unqualified(cached) and not futures_contract_month_is_stale(
        cached, as_of
    ):
        return False
    cache.pop(cache_key, None)
    _mark_trading_hours_stale_on_future_rollover(instrument)
    return True


def _bar_size_for_interval(interval: str) -> str:
    mapping = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins", "60m": "1 hour"}
    return mapping.get(interval, "5 mins")


def _duration_for_range(range_: str) -> str:
    mapping = {
        "1d": "1 D",
        "2d": "2 D",
        "3d": "3 D",
        "4d": "4 D",
        "5d": "5 D",
        "7d": "1 W",
        "14d": "2 W",
        "31d": "31 D",
        "1mo": "1 M",
        "2mo": "2 M",
        "3mo": "3 M",
        "6mo": "6 M",
        "1y": "1 Y",
    }
    normalized = str(range_ or "").strip().lower()
    configured = mapping.get(normalized)
    if configured is not None:
        return configured
    if normalized.endswith("y") and normalized[:-1].isdigit():
        years = int(normalized[:-1])
        if years > 0:
            return f"{years} Y"
    return "1 D"


def _duration_for_chart_stream(interval: str) -> str:
    mapping = {"1m": "1 D", "5m": "1 D", "15m": "2 D", "60m": "1 W"}
    return mapping.get(str(interval), "1 D")


def _what_to_show(instrument: dict[str, Any]) -> str:
    qualified = require_provider_identity(instrument, provider="ibkr")
    identity = identity_payload(qualified)
    if str(identity.get("sec_type") or "").strip().upper() == "CASH":
        return "MIDPOINT"
    return "TRADES"


def _history_use_rth(instrument: dict[str, Any]) -> bool:
    qualified = require_provider_identity(instrument, provider="ibkr")
    identity = identity_payload(qualified)
    asset_class = (
        str(qualified.get("asset_class") or identity.get("asset_class") or "").strip().lower()
    )
    sec_type = str(identity.get("sec_type") or "").strip().upper()
    return asset_class == "index" or sec_type == "IND"


def _ibkr_bar_provenance(
    *,
    request_type: str,
    what_to_show: str,
    instrument: dict[str, Any],
    contract: object | None = None,
) -> BarProvenance:
    qualified = require_provider_identity(instrument, provider="ibkr")
    contract_type = str(getattr(contract, "secType", "") or "").strip().upper()
    if not contract_type:
        if instrument_is_futures_root(qualified):
            raise InstrumentIdentityError("IBKR_BAR_PROVENANCE_QUALIFIED_CONTRACT_REQUIRED")
        contract_type = str(identity_payload(qualified).get("sec_type") or "").strip().upper()
    if not contract_type:
        raise InstrumentIdentityError("IBKR_BAR_PROVENANCE_CONTRACT_TYPE_REQUIRED")
    if instrument_is_futures_root(qualified) and contract_type != "CONTFUT":
        raise InstrumentIdentityError("IBKR_FUTURES_BAR_PROVENANCE_REQUIRES_CONTFUT")
    contract_id = int(getattr(contract, "conId", 0) or 0)
    if contract_id <= 0:
        if instrument_is_futures_root(qualified):
            raise InstrumentIdentityError("IBKR_BAR_PROVENANCE_QUALIFIED_CONTRACT_REQUIRED")
        else:
            raw_contract_id = provider_contract_id(qualified)
        contract_id = _exact_ibkr_con_id(raw_contract_id)
    if contract_id <= 0:
        raise InstrumentIdentityError("IBKR_BAR_PROVENANCE_CONTRACT_REQUIRED")
    request_key = str(request_type or "").strip().lower()
    data_key = str(what_to_show or "").strip().upper()
    if request_key not in {"historical", "keep_up_to_date"} or not data_key:
        raise ValueError("IBKR_BAR_PROVENANCE_REQUEST_REQUIRED")
    return BarProvenance(
        provider="ibkr",
        instrument_id=qualified_instrument_id(qualified),
        route_fingerprint=route_fingerprint(qualified),
        request_type=request_key,
        provider_contract_id=str(contract_id),
        provider_contract_type=contract_type,
        data_type=data_key,
    )


def _bar_from_ibkr_item(
    item,
    symbol: str,
    interval: str,
    *,
    provenance: BarProvenance,
    state: BarState = BarState.CONFIRMED,
) -> Bar:
    ts = _normalize_ts(item.date)
    request_type = provenance.request_type.value
    return Bar(
        symbol=symbol,
        ts=ts,
        open=float(item.open),
        high=float(item.high),
        low=float(item.low),
        close=float(item.close),
        volume=float(getattr(item, "volume", 0) or 0),
        timeframe=interval,
        source=(
            f"{provenance.provider}:{request_type}:{provenance.data_type}:"
            f"contract={provenance.provider_contract_id}:"
            f"route={provenance.route_fingerprint}"
        ),
        closed=state is BarState.CONFIRMED,
        state=state,
        provenance=provenance,
    )


def _normalize_ts(value: datetime | str) -> datetime:
    parsed = parse_aware_utc_ts(value)
    if parsed is None:
        raise ValueError("IBKR_BAR_TIMESTAMP_AWARE_UTC_REQUIRED")
    return parsed
