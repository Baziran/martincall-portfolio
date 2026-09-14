"""Option Point live-quote admission and fair-value refresh."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import LOGGER, NY_TZ
from aef_terminal.data.gex.contracts import (
    require_gex_market_data_entitlement,
    require_gex_option_reference,
)
from aef_terminal.data.gex.math import (
    _implied_option_vol,
    _option_model_gamma_delta,
    _option_model_price,
    _time_to_expiry_years,
)
from aef_terminal.data.gex.option_target_contract import (
    OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS,
    option_target_finite_number,
    option_target_positive_number,
)
from aef_terminal.data.gex.utils import finite_number_or_none, parse_gex_timestamp
from aef_terminal.data.market_data import (
    market_data_observation_time,
    market_data_price_source_is_current,
)
from aef_terminal.domain import OptionRight
from aef_terminal.runtime.telemetry import log_structured_error


def _option_reference_price(row: Mapping[str, Any]) -> float | None:
    if row.get("reference_option_price_source") == "theoretical_model":
        reference = finite_number_or_none(row.get("reference_option_price"))
        if reference is None or reference <= 0:
            raise ValueError("Theoretical option reference must be finite and positive")
        return reference
    return require_gex_option_reference(row)["reference_option_price"]


def _apply_option_live_quote(
    payload: dict[str, Any],
    provider: Callable[[dict[str, Any]], Mapping[str, Any] | None] | None,
) -> dict[str, Any]:
    if provider is None or not payload.get("ok"):
        return payload
    try:
        futures_options = payload.get("futures_options")
        if type(futures_options) is not bool:
            raise ValueError("Option quote requires an exact OPT/FOP model type")
        quote = provider(
            {
                "con_id": payload.get("con_id"),
                "exchange": payload.get("exchange"),
                "sec_type": "FOP" if futures_options else "OPT",
            }
        )
    except Exception as exc:
        log_structured_error(
            LOGGER,
            level="debug",
            event="gex_request_failed",
            provider=str(payload.get("provider") or "unknown"),
            symbol=str(payload.get("provider_symbol") or ""),
            interval="options",
            range_="snapshot",
            op="option_live_quote",
            error=exc,
        )
        out = dict(payload)
        out["live_quote_status"] = "error"
        from aef_terminal.runtime.telemetry import exception_message

        out["live_quote_message"] = exception_message(exc)
        out["live_quote_entitlement"] = "unknown"
        out["reference_option_price"] = None
        out["reference_option_price_source"] = "unavailable"
        for key in (
            "live_bid",
            "live_ask",
            "live_mid",
            "live_last",
            "live_quote_ts",
            "live_quote_time_basis",
        ):
            out[key] = None
        return out
    if not isinstance(quote, Mapping):
        out = dict(payload)
        out["live_quote_status"] = "error"
        out["live_quote_message"] = "Option quote provider returned an invalid payload."
        out["live_quote_entitlement"] = "unknown"
        out["reference_option_price"] = None
        out["reference_option_price_source"] = "unavailable"
        for key in (
            "live_bid",
            "live_ask",
            "live_mid",
            "live_last",
            "live_quote_ts",
            "live_quote_time_basis",
        ):
            out[key] = None
        return out
    try:
        if any(
            legacy in quote
            for legacy in ("reference_option_price", "reference_option_price_source")
        ):
            raise ValueError("Option quote producer returned duplicate GEX reference fields")
        option_reference = require_gex_option_reference(
            {
                "reference_option_price": quote.get("price"),
                "reference_option_price_source": quote.get("price_source"),
            }
        )
        reported_entitlement = require_gex_market_data_entitlement(
            quote.get("market_data_entitlement"),
            allow_unknown=True,
        )
        quote_at, time_basis = market_data_observation_time(quote)
        raw_observation_ts = (
            quote.get("provider_ts")
            if time_basis == "provider_event"
            else quote.get("received_at")
            if time_basis == "client_receive"
            else None
        )
        raw_ts = quote.get("ts")
        if raw_ts is not None and raw_ts != raw_observation_ts:
            raise ValueError("Option quote ts disagrees with its declared time basis")
        bid = option_target_positive_number(quote.get("bid"))
        ask = option_target_positive_number(quote.get("ask"))
        last = option_target_positive_number(quote.get("last"))
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None and ask >= bid else None
        reference = option_reference["reference_option_price"]
        source = option_reference["reference_option_price_source"]
        if source == "last" and (
            last is None
            or reference is None
            or not math.isclose(reference, last, rel_tol=0.0, abs_tol=0.000001)
        ):
            raise ValueError("Option quote last provenance disagrees with raw last")
        if source == "bid_ask_mid" and (
            mid is None
            or reference is None
            or not math.isclose(reference, mid, rel_tol=0.0, abs_tol=0.000001)
        ):
            raise ValueError("Option quote midpoint provenance disagrees with raw bid/ask")
    except ValueError as exc:
        out = dict(payload)
        out["live_quote_status"] = "error"
        out["live_quote_message"] = str(exc)
        out["live_quote_entitlement"] = "unknown"
        out["reference_option_price"] = None
        out["reference_option_price_source"] = "unavailable"
        for key in (
            "live_bid",
            "live_ask",
            "live_mid",
            "live_last",
            "live_quote_ts",
            "live_quote_time_basis",
        ):
            out[key] = None
        return out
    if reference is None:
        out = dict(payload)
        out["live_quote_status"] = "empty"
        out["live_quote_entitlement"] = reported_entitlement
        out["reference_option_price"] = None
        out["reference_option_price_source"] = "unavailable"
        out["live_bid"] = bid
        out["live_ask"] = ask
        out["live_mid"] = round(mid, 4) if mid is not None else None
        out["live_last"] = last
        out["live_quote_ts"] = raw_observation_ts if quote_at is not None else None
        out["live_quote_time_basis"] = time_basis
        if quote.get("message"):
            out["live_quote_message"] = str(quote.get("message") or "")
        return out
    out = dict(payload)
    source_is_current = (
        source == "bid_ask_mid"
        and quote_at is not None
        and isinstance(raw_observation_ts, str)
        and market_data_price_source_is_current(source, time_basis)
    )
    out["live_quote_status"] = (
        "ok"
        if reported_entitlement == "live" and source_is_current
        else "display_only"
        if reported_entitlement == "live"
        else reported_entitlement
    )
    out["live_quote_source"] = "ibkr_option_quote"
    out["live_quote_entitlement"] = reported_entitlement
    out["reference_option_price"] = round(reference, 4)
    out["reference_option_price_source"] = option_reference["reference_option_price_source"]
    out["live_bid"] = bid
    out["live_ask"] = ask
    out["live_mid"] = round(mid, 4) if mid is not None else None
    out["live_last"] = last
    out["live_quote_ts"] = raw_observation_ts
    out["live_quote_time_basis"] = time_basis
    if quote.get("message"):
        out["live_quote_message"] = str(quote.get("message") or "")
    return out


def _option_valuation_spot(
    *,
    meta_spot: float | None,
    underlying_price: float | None,
    target: float | None,
) -> float | None:
    live = finite_number_or_none(underlying_price)
    if live is not None:
        return live
    cached = finite_number_or_none(meta_spot)
    if cached is not None:
        return cached
    return finite_number_or_none(target)


def _option_fair_spot_for_target(target: float, underlying: float | None) -> float:
    if underlying is None or underlying <= 0:
        return target
    tolerance = max(0.05, abs(underlying) * 0.0003)
    if abs(target - underlying) <= tolerance:
        return underlying
    return target


def _market_option_reference(payload: Mapping[str, Any]) -> float | None:
    try:
        return require_gex_option_reference(payload)["reference_option_price"]
    except ValueError:
        return None


def _mark_option_target_fair_stale(payload: dict[str, Any], message: str) -> dict[str, Any]:
    out = dict(payload)
    current_fair = option_target_positive_number(out.get("fair_price"))
    if current_fair is not None:
        out["display_fair_price"] = current_fair
    out["fair_price_status"] = "stale"
    out["fair_price_message"] = message
    out["fair_price"] = None
    out["max_limit_price"] = None
    out["bid_safe_price"] = None
    return out


def _refresh_fair_from_market_quote(
    payload: dict[str, Any], *, valuation_now: datetime | None = None
) -> dict[str, Any]:
    if not payload.get("ok"):
        return payload
    if payload.get("market_price_only"):
        return _mark_option_target_fair_stale(
            payload,
            "Fair price unavailable: the selected contract has market price only.",
        )
    if "live_quote_status" not in payload:
        # Contract-selection previews can carry a model estimate. Once a fixed
        # contract quote is requested, only a complete current quote pair may
        # replace that estimate.
        return payload
    observed_at = valuation_now or datetime.now(tz=UTC)
    quote_at = parse_gex_timestamp(payload.get("live_quote_ts"))
    underlying_at = parse_gex_timestamp(payload.get("underlying_quote_ts"))
    quote_age = (
        (observed_at.astimezone(UTC) - quote_at).total_seconds()
        if isinstance(observed_at, datetime)
        and observed_at.tzinfo is not None
        and observed_at.utcoffset() is not None
        and quote_at is not None
        else None
    )
    underlying_age = (
        (observed_at.astimezone(UTC) - underlying_at).total_seconds()
        if isinstance(observed_at, datetime)
        and observed_at.tzinfo is not None
        and observed_at.utcoffset() is not None
        and underlying_at is not None
        else None
    )
    underlying_for_iv = option_target_finite_number(payload.get("live_underlying_price"))
    if (
        payload.get("live_quote_status") != "ok"
        or payload.get("live_quote_entitlement") != "live"
        or payload.get("reference_option_price_source") != "bid_ask_mid"
        or not market_data_price_source_is_current(
            payload.get("reference_option_price_source"),
            payload.get("live_quote_time_basis"),
        )
        or quote_age is None
        or quote_age < -1.0
        or quote_age > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        or underlying_for_iv is None
        or payload.get("underlying_quote_entitlement") != "live"
        or payload.get("underlying_quote_status") != "live"
        or not market_data_price_source_is_current(
            payload.get("underlying_quote_price_source"),
            payload.get("underlying_quote_time_basis"),
        )
        or underlying_age is None
        or underlying_age < -1.0
        or underlying_age > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        or underlying_at is None
        or quote_at is None
        or abs((quote_at - underlying_at).total_seconds())
        > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
    ):
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: a current authoritative option/underlying quote pair is unavailable.",
        )
    market = _market_option_reference(payload)
    if market is None or market <= 0:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: the authoritative option reference is unavailable.",
        )
    futures_options = payload.get("futures_options")
    if type(futures_options) is not bool:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: the option model type is unavailable.",
        )
    strike = finite_number_or_none(payload.get("strike"))
    target = finite_number_or_none(payload.get("target_underlying_price"))
    if strike is None or target is None:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: exact strike or target facts are unavailable.",
        )
    expiry = str(payload.get("expiry") or "")
    observed_at = (valuation_now or datetime.now(tz=UTC)).astimezone(NY_TZ)
    target_at = parse_gex_timestamp(payload.get("target_ts"))
    scenario_at = (
        max(observed_at, target_at.astimezone(NY_TZ)) if target_at is not None else observed_at
    )
    expiry_at = payload.get("expiry_at")
    observed_years = _time_to_expiry_years(
        expiry,
        observed_at,
        expiry_at=expiry_at,
        require_exact=True,
    )
    years = _time_to_expiry_years(
        expiry,
        scenario_at,
        expiry_at=expiry_at,
        require_exact=True,
    )
    if observed_years is None or years is None:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: exact option expiry time is unavailable.",
        )
    right = OptionRight(payload.get("right"))
    seed_iv = finite_number_or_none(payload.get("iv")) or 0.20
    risk_free_rate = finite_number_or_none(payload.get("risk_free_rate"))
    dividend_yield = finite_number_or_none(payload.get("dividend_yield"))
    if risk_free_rate is None or dividend_yield is None:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: canonical valuation rates are unavailable.",
        )
    implied = _implied_option_vol(
        futures_options,
        spot=underlying_for_iv,
        strike=strike,
        years=observed_years,
        iv=seed_iv,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        market_price=market,
    )
    if implied is None:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: market-implied volatility could not be solved.",
        )
    fair_spot = _option_fair_spot_for_target(target, underlying_for_iv)
    theo_at_fair_spot = _option_model_price(
        futures_options,
        spot=fair_spot,
        strike=strike,
        years=years,
        iv=implied,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    if theo_at_fair_spot is None:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: the local valuation model returned no value.",
        )
    fair_price = round(theo_at_fair_spot, 4)
    out = dict(payload)
    out["iv"] = round(implied, 6)
    out["iv_source"] = "market_implied"
    out["fair_price"] = fair_price
    out["valuation_ts"] = scenario_at.isoformat()
    out["theta_horizon_seconds"] = max(
        round((scenario_at - observed_at).total_seconds(), 3),
        0.0,
    )
    out["time_to_expiry_years"] = years
    out["max_limit_price"] = round(max(0.01, fair_price * 1.03), 4)
    out["bid_safe_price"] = round(max(0.01, min(fair_price, fair_price * 0.94)), 4)
    out["fair_price_status"] = "ok"
    out.pop("fair_price_message", None)
    greeks = _option_model_gamma_delta(
        futures_options,
        spot=underlying_for_iv,
        strike=strike,
        years=years,
        iv=implied,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    if greeks:
        out["delta"] = round(greeks["delta"], 4)
        out["gamma"] = greeks["gamma"]
        out["estimated_greeks"] = False
    target_greeks = _option_model_gamma_delta(
        futures_options,
        spot=fair_spot,
        strike=strike,
        years=years,
        iv=implied,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    if target_greeks:
        out["target_delta"] = round(target_greeks["delta"], 4)
        out["target_gamma"] = target_greeks["gamma"]
    return out
