from __future__ import annotations

from typing import Any

from aef_terminal.data.providers import (
    normalize_provider_key,
    provider_capabilities,
    provider_catalog,
    route_instrument,
)
from aef_terminal.data.instrument_identity import instrument_key
from aef_terminal.ui.quote_helpers import quote_is_execution_eligible
from aef_terminal.ui.screener_change import (
    quote_close_base_for_route,
    watchlist_change_base,
)


def _quote_bool(quote: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in quote:
        return default
    value = quote[key]
    if type(value) is not bool:
        raise ValueError(f"SCREENER_QUOTE_BOOLEAN_INVALID field={key}")
    return value


def _quote_nullable_text(quote: dict[str, Any], key: str) -> str | None:
    value = quote.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"SCREENER_QUOTE_TEXT_INVALID field={key}")
    return value


def _quote_rollover_warning(quote: dict[str, Any]) -> dict[str, Any] | None:
    value = quote.get("contract_rollover_warning")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("SCREENER_QUOTE_ROLLOVER_WARNING_INVALID")
    return value


def _provider_label(provider: str) -> str:
    provider_key = str(provider or "").strip().lower()
    for item in provider_catalog():
        if str(item.get("key") or "").strip().lower() == provider_key:
            return str(item.get("name") or provider_key.upper()).strip()
    return provider_key.upper() if provider_key else "Provider"


def _provider_supports_polling(provider: str) -> bool:
    try:
        return bool(provider_capabilities(provider).live_quote_polling)
    except ValueError:
        return False


def _provider_supports_live_quotes(provider: str) -> bool:
    try:
        capabilities = provider_capabilities(provider)
    except ValueError:
        return False
    return bool(capabilities.live_quote_stream or capabilities.live_quote_polling)


def _quote_source(
    *,
    quote_provider: str,
    quote: dict[str, Any],
    live_price: Any,
) -> str:
    explicit = str(quote.get("source") or "").strip()
    if explicit:
        return explicit
    provider = normalize_provider_key(quote_provider)
    if live_price is not None:
        mode = "quote-live" if _provider_supports_live_quotes(provider) else "quote"
        return f"{provider}:{mode}"
    return f"{provider}:quote-unavailable"


def _row_has_live_quote(
    *,
    quote_provider: str,
    live_price: Any,
    quote: dict[str, Any],
) -> bool:
    if live_price is None:
        return False
    return _provider_supports_live_quotes(quote_provider) and quote_is_execution_eligible(quote)


def screener_rows(
    instruments: list[dict],
    bases: dict[str, dict],
    live_map: dict[str, dict[str, float | str | None]],
    live_warning: str = "",
    interval: str = "5m",
) -> list[dict]:
    rows = []
    for instrument in instruments:
        route = route_instrument(instrument)
        key = instrument_key(instrument)
        instrument_id = route.instrument_id
        provider_symbol = route.provider_symbol
        base_row = bases.get(instrument_id, {})
        latest = base_row.get("latest")
        previous = base_row.get("previous")
        previous_session_close = base_row.get("previous_session_close")
        quote = live_map.get(route.fingerprint, {})
        live_price = quote.get("price") if isinstance(quote, dict) else None
        quote_provider = route.provider
        quote_close_base = (
            quote_close_base_for_route(route, quote)
            if isinstance(quote, dict) and live_price is not None
            else None
        )
        price = live_price
        base = watchlist_change_base(
            quote_close_base=quote_close_base,
            previous_session_close=previous_session_close,
            previous=previous,
            latest=latest,
        )
        change = (price - base) if price is not None and base is not None else None
        change_pct = (change / base * 100.0) if change is not None and base else None
        warning = ""
        if isinstance(quote, dict):
            warning = str(quote.get("message") or "")
        if not warning and live_warning:
            warning = live_warning
        provider_label = _provider_label(quote_provider)
        provider_polling = _provider_supports_polling(quote_provider)
        if not warning and latest is None:
            warning = f"No cached {provider_label} bars for {provider_symbol} {interval}"
        if not warning and live_price is None:
            mode = "polling quote" if provider_polling else "live quote"
            warning = f"{provider_label} {mode} is not available for {provider_symbol}"
        quote_source = _quote_source(
            quote_provider=quote_provider,
            quote=quote if isinstance(quote, dict) else {},
            live_price=live_price,
        )
        live_quote = _row_has_live_quote(
            quote_provider=quote_provider,
            live_price=live_price,
            quote=quote if isinstance(quote, dict) else {},
        )
        rows.append(
            {
                "key": key,
                "instrument_id": instrument_id,
                "display": instrument["display"],
                "provider_symbol": provider_symbol,
                "name": instrument["name"],
                "price": price,
                "bid": quote.get("bid") if isinstance(quote, dict) else None,
                "ask": quote.get("ask") if isinstance(quote, dict) else None,
                "last": quote.get("last") if isinstance(quote, dict) else None,
                "quote_close": quote.get("close") if isinstance(quote, dict) else None,
                "quote_ts": quote.get("ts") if isinstance(quote, dict) else None,
                "price_source": quote.get("price_source") if isinstance(quote, dict) else None,
                "quote_time_basis": quote.get("time_basis") if isinstance(quote, dict) else None,
                "quote_provider_ts": quote.get("provider_ts") if isinstance(quote, dict) else None,
                "quote_received_at": quote.get("received_at") if isinstance(quote, dict) else None,
                "quote_age_seconds": quote.get("age_seconds") if isinstance(quote, dict) else None,
                "quote_status": quote.get("status") if isinstance(quote, dict) else "unavailable",
                "quote_entitlement": quote.get("market_data_entitlement")
                if isinstance(quote, dict)
                else "unknown",
                "quote_is_delayed": _quote_bool(quote, "is_delayed", default=False)
                if isinstance(quote, dict)
                else False,
                "quote_is_stale": _quote_bool(quote, "is_stale", default=True)
                if isinstance(quote, dict)
                else True,
                "last_provider_ts": quote.get("last_provider_ts")
                if isinstance(quote, dict)
                else None,
                "last_age_seconds": quote.get("last_age_seconds")
                if isinstance(quote, dict)
                else None,
                "last_status": quote.get("last_status")
                if isinstance(quote, dict)
                else "unavailable",
                "bid_ask_received_at": quote.get("bid_ask_received_at")
                if isinstance(quote, dict)
                else None,
                "bid_ask_age_seconds": quote.get("bid_ask_age_seconds")
                if isinstance(quote, dict)
                else None,
                "bid_ask_status": quote.get("bid_ask_status")
                if isinstance(quote, dict)
                else "unavailable",
                "change": change,
                "change_pct": change_pct,
                "previous_session_close": previous_session_close,
                "change_base": base,
                "source": quote_source,
                "live_quote": live_quote,
                "warning": warning,
                "quote_provider": quote_provider,
                "provider": route.provider,
                "route_fingerprint": route.fingerprint,
                "quote_only": bool(instrument.get("quote_only")),
                "session": instrument.get("session"),
                "contract": _quote_nullable_text(quote, "contract")
                if isinstance(quote, dict)
                else None,
                "local_symbol": _quote_nullable_text(quote, "local_symbol")
                if isinstance(quote, dict)
                else None,
                "contract_month": _quote_nullable_text(quote, "contract_month")
                if isinstance(quote, dict)
                else None,
                "contract_rollover_due": _quote_bool(quote, "contract_rollover_due", default=False)
                if isinstance(quote, dict)
                else False,
                "contract_rollover_warning": _quote_rollover_warning(quote)
                if isinstance(quote, dict)
                else None,
                "contract_rollover_new": _quote_bool(quote, "contract_rollover_new", default=False)
                if isinstance(quote, dict)
                else False,
                "contract_rollover_new_message": _quote_nullable_text(
                    quote, "contract_rollover_new_message"
                )
                if isinstance(quote, dict)
                else None,
            }
        )
    return rows
