from __future__ import annotations

import re
from math import isfinite

from aef_terminal.data.ibkr.runtime import _IBKR_CANCEL_IGNORED_ERROR_CODES, _LOGGER


def _safe_cancel_mkt_data(ib, ticker, contract) -> bool:
    """Return whether cancellation was dispatched or absence was confirmed."""

    ticker_id = _active_market_data_ticker_id(ib, ticker)
    if ticker_id is None:
        _LOGGER.debug(
            "IBKR market-data cancellation already absent for %s",
            _contract_label(contract),
        )
        return True
    try:
        cancelled = ib.cancelMktData(contract)
    except Exception as exc:
        code = _ib_api_error_code(exc)
        if code in _IBKR_CANCEL_IGNORED_ERROR_CODES:
            _LOGGER.debug(
                "Ignored IBKR cancelMktData error %s for tickerId %s (%s): %s",
                code,
                ticker_id,
                _contract_label(contract),
                exc,
            )
            return True
        _LOGGER.warning(
            "IBKR cancelMktData failed for tickerId %s (%s): %s",
            ticker_id,
            _contract_label(contract),
            exc,
            exc_info=True,
        )
        return False
    if cancelled is True:
        return True
    _LOGGER.warning(
        "IBKR cancelMktData did not confirm dispatch for tickerId %s (%s)",
        ticker_id if ticker_id is not None else "unknown",
        _contract_label(contract),
    )
    return False


def _active_market_data_ticker_id(ib, ticker) -> int | None:
    wrapper = getattr(ib, "wrapper", None)
    if wrapper is None or ticker is None:
        return None
    ticker_id = _ticker_id_from_wrapper(wrapper, ticker)
    ticker_id_map = getattr(wrapper, "tickerIdMap", None)
    if ticker_id_map is not None:
        if ticker_id is not None and _mapping_contains_key(ticker_id_map, ticker_id):
            return ticker_id
        mapped_id = _mapping_key_for_value(ticker_id_map, ticker)
        return mapped_id
    if ticker_id is not None:
        return ticker_id
    req_id_map = getattr(wrapper, "reqId2Ticker", None)
    return _mapping_key_for_value(req_id_map, ticker)


def _ticker_id_from_wrapper(wrapper, ticker) -> int | None:
    for attr in ("tickerId", "reqId", "requestId"):
        value = getattr(ticker, attr, None)
        parsed = _int_or_none(value)
        if parsed is not None:
            return parsed
    ticker2reqid = getattr(wrapper, "ticker2ReqId", None)
    if ticker2reqid is not None:
        try:
            mkt_data_map = ticker2reqid.get("mktData")
        except Exception:
            mkt_data_map = None
        mapped = _mapping_value_for_key(mkt_data_map, ticker)
        if mapped is not None:
            return mapped
    req_id_map = getattr(wrapper, "reqId2Ticker", None)
    return _mapping_key_for_value(req_id_map, ticker)


def _mapping_contains_key(mapping, key: int) -> bool:
    if mapping is None:
        return False
    try:
        return key in mapping
    except Exception:
        return False


def _mapping_value_for_key(mapping, key) -> int | None:
    if mapping is None:
        return None
    try:
        return _int_or_none(mapping.get(key))
    except Exception:
        return None


def _mapping_key_for_value(mapping, value) -> int | None:
    if mapping is None:
        return None
    try:
        items = mapping.items()
    except Exception:
        return None
    for key, mapped in items:
        if mapped is value:
            return _int_or_none(key)
    return None


def _ib_api_error_code(exc: Exception) -> int | None:
    for attr in ("code", "errorCode", "error_code"):
        parsed = _int_or_none(getattr(exc, attr, None))
        if parsed is not None:
            return parsed
    for arg in getattr(exc, "args", ()):
        parsed = _int_or_none(arg)
        if parsed is not None:
            return parsed
    match = re.search("\\b(\\d{3,5})\\b", str(exc))
    return int(match.group(1)) if match else None


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _contract_label(contract) -> str:
    return str(
        getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None) or contract
    )


def _clean_float(value) -> float | None:
    try:
        result = float(value)
    except Exception:
        return None
    return result if isfinite(result) else None


def _clean_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    return _clean_float(value)
