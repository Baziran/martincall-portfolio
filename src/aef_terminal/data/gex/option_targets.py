"""Option Point exact-contract selection and final payload composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import NY_TZ
from aef_terminal.data.gex.contracts import require_gex_valuation_rates
from aef_terminal.data.gex.math import (
    _option_model_gamma_delta,
    _option_model_price,
    _time_to_expiry_years,
)
from aef_terminal.data.gex.option_target_contract import (
    require_option_target_dte,
    OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS,
    option_target_finite_number,
    option_target_positive_number,
    require_option_target_underlying_quote,
)
from aef_terminal.data.gex.option_target_selection import (
    _eligible_option_target_expiries,
    _option_target_expiry_rows,
    _option_target_premium_cap,
    _option_target_profile,
    _rank_option_target_candidates,
)
from aef_terminal.data.gex.option_target_universe import (
    _characterize_option_target_contracts,
    _require_option_target_contracts,
)
from aef_terminal.data.gex.option_target_valuation import (
    _apply_option_live_quote,
    _mark_option_target_fair_stale,
    _market_option_reference,
    _option_fair_spot_for_target,
    _option_reference_price,
    _option_valuation_spot,
    _refresh_fair_from_market_quote,
)
from aef_terminal.data.gex.utils import (
    _expiration_date,
    finite_number_or_none,
    _num,
    parse_gex_timestamp,
)
from aef_terminal.data.instrument_identity import (
    instrument_provider,
    instrument_is_futures,
    parse_exact_positive_decimal_provider_id,
    provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint as instrument_route_fingerprint,
)
from aef_terminal.domain import OptionRight


_OPTION_TARGET_LEGACY_FIELDS = {
    "asset",
    "symbol",
    "side",
    "dte",
    "conId",
    "lastTradeDateOrContractMonth",
    "theoretical_price",
    "current_option_price",
    "quote_ts",
    "market_sample",
}


def _require_final_option_target_contract(
    payload: dict[str, Any],
    *,
    futures_options: bool,
) -> dict[str, Any]:
    enriched = dict(payload)
    if enriched.get("mode") not in {"conservative", "normal", "aggressive"}:
        raise ValueError("option target payload has an invalid mode")
    try:
        enriched["target_dte"] = require_option_target_dte(enriched.get("target_dte"))
    except ValueError as exc:
        raise ValueError("option target payload has an invalid target_dte") from exc
    if enriched.get("right") not in {"C", "P"}:
        raise ValueError("option target payload has an invalid option right")

    con_id = parse_exact_positive_decimal_provider_id(enriched.get("con_id"))
    strike = finite_number_or_none(enriched.get("strike"))
    expiry = enriched.get("expiry")
    expiry_at = parse_gex_timestamp(enriched.get("expiry_at"))
    exchange = enriched.get("exchange")
    trading_class = enriched.get("trading_class")
    multiplier = finite_number_or_none(enriched.get("multiplier"))
    currency = enriched.get("currency")
    expected_sec_type = "FOP" if futures_options else "OPT"
    exact_text_fields = (exchange, trading_class, currency)
    if (
        con_id <= 0
        or strike is None
        or strike <= 0
        or not isinstance(expiry, str)
        or len(expiry) != 8
        or not expiry.isdigit()
        or expiry_at is None
        or multiplier is None
        or multiplier <= 0
        or enriched.get("sec_type") != expected_sec_type
        or any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in exact_text_fields
        )
    ):
        raise ValueError("option target payload is missing exact provider contract facts")
    if _OPTION_TARGET_LEGACY_FIELDS.intersection(enriched):
        raise ValueError("option target payload contains a legacy alias")

    require_exact_identity_text(
        enriched.get("provider_symbol"),
        field="OPTION_TARGET_PROVIDER_SYMBOL",
    )
    enriched.update(
        {
            "con_id": con_id,
            "expiry_at": expiry_at.isoformat(),
            "multiplier": multiplier,
            "futures_options": futures_options,
        }
    )
    return enriched


def _apply_option_target_underlying_quote(
    payload: dict[str, Any],
    *,
    underlying_price: float | None,
    underlying_quote: Mapping[str, Any] | None,
    valuation_now: datetime,
    futures_options: bool,
) -> None:
    live_underlying = (
        option_target_finite_number(underlying_price)
        if futures_options
        else option_target_positive_number(underlying_price)
    )
    if underlying_price is not None and live_underlying is None:
        requirement = "finite signed" if futures_options else "finite positive"
        raise ValueError(f"underlying_price must be a {requirement} number or null")
    if underlying_quote is None:
        return
    underlying_facts = require_option_target_underlying_quote(
        underlying_quote,
        observed_at=valuation_now,
        futures_options=futures_options,
    )
    observed_underlying = underlying_facts["price"]
    if observed_underlying is not None:
        payload["underlying_quote_price"] = observed_underlying
        payload["underlying_quote_price_source"] = underlying_facts["price_source"]
    payload["underlying_quote_entitlement"] = underlying_facts["market_data_entitlement"]
    payload["underlying_quote_status"] = underlying_facts["status"]
    if underlying_facts["quote_ts"] is not None:
        payload["underlying_quote_ts"] = underlying_facts["quote_ts"]
        payload["underlying_quote_time_basis"] = underlying_facts["time_basis"]
    if (
        live_underlying is not None
        and underlying_facts["execution_price"] is not None
        and abs(live_underlying - underlying_facts["execution_price"]) <= 0.000001
    ):
        payload["live_underlying_price"] = live_underlying


def _expire_stale_option_target_quote(
    payload: dict[str, Any],
    *,
    valuation_now: datetime,
) -> None:
    if payload.get("live_quote_status") != "ok":
        return
    quote_at = parse_gex_timestamp(payload.get("live_quote_ts"))
    quote_age = (
        (valuation_now.astimezone(UTC) - quote_at.astimezone(UTC)).total_seconds()
        if quote_at is not None
        else None
    )
    if (
        quote_age is not None
        and quote_age >= -1.0
        and quote_age <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
    ):
        return
    payload["live_quote_status"] = "stale"
    payload["live_quote_message"] = "Live option quote timestamp is missing or stale."
    payload["reference_option_price"] = None
    payload["reference_option_price_source"] = "unavailable"
    for key in ("live_bid", "live_ask", "live_mid", "live_last"):
        payload[key] = None


def _mark_unusable_option_target_fair_price(payload: dict[str, Any]) -> dict[str, Any]:
    market = _market_option_reference(payload)
    fair = finite_number_or_none(payload.get("fair_price"))
    if market is not None and market > 0 and fair is not None and fair <= 0:
        return _mark_option_target_fair_stale(
            payload,
            "Fair price stale: live option market is positive, but local model returned a non-positive fair value.",
        )
    if payload.get("requires_live_fair") and fair is None:
        message = (
            "Fair price stale: live quote is present, but local IV/fair calculation is unavailable."
            if payload.get("live_quote_status") == "ok"
            else "Fair price stale: no live option quote is available for this fixed contract."
        )
        return _mark_option_target_fair_stale(payload, message)
    return payload


def _finalize_option_target_payload(
    payload: dict[str, Any],
    *,
    futures_options: bool,
    valuation_now: datetime,
    underlying_price: float | None = None,
    underlying_quote: Mapping[str, Any] | None = None,
    option_quote_provider: Callable[[dict[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    if not payload.get("ok"):
        return payload
    enriched = _require_final_option_target_contract(
        payload,
        futures_options=futures_options,
    )
    enriched["subscription_state"] = (
        "subscribed" if option_quote_provider is not None else "not_subscribed"
    )
    _apply_option_target_underlying_quote(
        enriched,
        underlying_price=underlying_price,
        underlying_quote=underlying_quote,
        valuation_now=valuation_now,
        futures_options=futures_options,
    )
    quoted = _apply_option_live_quote(enriched, option_quote_provider)
    _expire_stale_option_target_quote(quoted, valuation_now=valuation_now)
    refreshed = _refresh_fair_from_market_quote(quoted, valuation_now=valuation_now)
    return _mark_unusable_option_target_fair_price(refreshed)


@dataclass(frozen=True, slots=True)
class _OptionTargetPayloadContext:
    identity_payload: dict[str, Any]
    profile: dict[str, Any]
    target_dte: str
    target_expiry_code: str
    right: str
    target: float
    target_time_payload: dict[str, Any]
    futures_options: bool
    target_model_spot: float
    scenario_valuation_at: datetime
    theoretical_iv: float
    valuation_rates: tuple[float, float]
    theoretical_liquidity: dict[str, Any]
    spot: float
    qualified_at: datetime
    valuation_now: datetime
    underlying_price: float | None
    underlying_quote: Mapping[str, Any] | None
    option_quote_provider: Callable[[dict[str, Any]], Mapping[str, Any] | None] | None

    @property
    def model_right(self) -> OptionRight:
        return OptionRight(self.right)


def _option_target_contract_transport_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sec_type": str(row.get("sec_type") or ""),
        "expiry_at": str(row.get("expiry_at") or ""),
        "local_symbol": str(row.get("local_symbol") or ""),
        "con_id": parse_exact_positive_decimal_provider_id(row.get("con_id")),
        "exchange": str(row.get("exchange") or ""),
        "trading_class": str(row.get("trading_class") or ""),
        "multiplier": _num(row.get("multiplier")),
        "currency": str(row.get("currency") or ""),
    }


def _option_target_greek_fields(
    context: _OptionTargetPayloadContext,
    row: Mapping[str, Any],
    iv: float | None,
) -> dict[str, float | None]:
    strike_value = finite_number_or_none(row.get("strike"))
    expiry_value = row.get("expiry")
    right_value = row.get("right")
    if (
        strike_value is None
        or iv is None
        or iv <= 0
        or not isinstance(expiry_value, str)
        or right_value not in {"C", "P"}
    ):
        return {"target_delta": None, "target_gamma": None}
    greeks = _option_model_gamma_delta(
        context.futures_options,
        spot=context.target_model_spot,
        strike=strike_value,
        years=_time_to_expiry_years(
            expiry_value,
            context.scenario_valuation_at,
            expiry_at=row.get("expiry_at"),
            require_exact=True,
        ),
        iv=iv,
        right=OptionRight(right_value),
        risk_free_rate=context.valuation_rates[0],
        dividend_yield=context.valuation_rates[1],
    )
    return {
        "target_delta": (round(greeks["delta"], 4) if greeks is not None else None),
        "target_gamma": (greeks["gamma"] if greeks is not None else None),
    }


def _fixed_option_target_payload(
    context: _OptionTargetPayloadContext,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    reference = _option_reference_price(candidate)
    contract_expiry = str(candidate.get("expiry") or "")
    years = _time_to_expiry_years(
        contract_expiry,
        context.scenario_valuation_at,
        expiry_at=candidate.get("expiry_at"),
        require_exact=True,
    )
    if years is None:
        return None
    effective_delta = finite_number_or_none(candidate.get("delta"))
    effective_gamma = finite_number_or_none(candidate.get("gamma"))
    risk_free_rate, dividend_yield = context.valuation_rates
    theoretical_price = _option_model_price(
        context.futures_options,
        spot=context.target_model_spot,
        strike=_num(candidate.get("strike")),
        years=years,
        iv=context.theoretical_iv,
        right=context.model_right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    if theoretical_price is None:
        return None
    fair_price = round(theoretical_price, 4)
    max_limit = max(0.01, fair_price * 1.03)
    premium_cap = context.profile["premium_cap"]
    over_cap = max(0.0, reference - premium_cap)
    return {
        "ok": True,
        "fixed_contract": True,
        "fixed_contract_quote_source": (
            "qualified_contract_live_subscription"
            if context.option_quote_provider is not None
            else "qualified_contract_theoretical"
        ),
        **context.identity_payload,
        "mode": context.profile["mode"],
        "target_dte": context.target_dte,
        "requested_expiry": context.target_expiry_code,
        "right": context.right,
        "expiry": contract_expiry,
        "strike": _num(candidate.get("strike")),
        "delta": None if effective_delta is None else round(effective_delta, 4),
        "gamma": effective_gamma,
        **_option_target_greek_fields(context, candidate, context.theoretical_iv),
        "iv": context.theoretical_iv,
        "risk_free_rate": risk_free_rate,
        "dividend_yield": dividend_yield,
        "estimated_greeks": True,
        "iv_source": str(candidate.get("iv_source") or "theoretical_default"),
        "abs_gex": finite_number_or_none(candidate.get("abs_gex")),
        "target_underlying_price": context.target,
        **context.target_time_payload,
        "time_to_expiry_years": years,
        "fair_price": fair_price,
        "bid_safe_price": round(max(0.01, min(fair_price, fair_price * 0.94)), 4),
        "max_limit_price": round(max_limit, 4),
        "reference_option_price": round(reference, 4),
        "reference_option_price_source": candidate["reference_option_price_source"],
        "premium_cap": premium_cap,
        "over_cap_by": round(over_cap, 4),
        "delta_band": [context.profile["delta_min"], context.profile["delta_max"]],
        "liquidity": dict(context.theoretical_liquidity),
        **_option_target_contract_transport_fields(candidate),
        "local_symbol": str(candidate.get("local_symbol") or ""),
        "con_id": candidate.get("con_id"),
        "exchange": candidate.get("exchange"),
        "spot": context.spot,
        "captured_at": context.qualified_at.isoformat(),
        "label": f"{context.right}{_num(candidate.get('strike')):g} fair {fair_price:.2f} limit≤{max_limit:.2f}",
    }


def _fixed_option_target_live_quote_payload(
    context: _OptionTargetPayloadContext,
    *,
    preferred_strike: float | None,
    preferred_expiry: str,
    preferred_expiry_at: str,
    preferred_local_symbol: str,
    preferred_con_id: int,
    preferred_exchange: str,
    preferred_trading_class: str,
    preferred_multiplier: float | None,
    preferred_currency: str,
) -> dict[str, Any]:
    return _finalize_option_target_payload(
        {
            "ok": True,
            "fixed_contract": True,
            "fixed_contract_quote_source": "qualified_contract_live_subscription",
            "requires_live_fair": True,
            "fair_price_status": "pending",
            "fair_price_message": "Fair price pending: the selected contract is awaiting its live option quote.",
            **context.identity_payload,
            "mode": context.profile["mode"],
            "target_dte": context.target_dte,
            "requested_expiry": context.target_expiry_code,
            "right": context.right,
            "expiry": preferred_expiry,
            "strike": preferred_strike,
            "delta": None,
            "gamma": None,
            "target_delta": None,
            "target_gamma": None,
            "iv": None,
            "risk_free_rate": context.valuation_rates[0],
            "dividend_yield": context.valuation_rates[1],
            "estimated_greeks": True,
            "abs_gex": None,
            "target_underlying_price": context.target,
            **context.target_time_payload,
            "time_to_expiry_years": (
                _time_to_expiry_years(
                    preferred_expiry,
                    context.scenario_valuation_at,
                    expiry_at=preferred_expiry_at,
                    require_exact=True,
                )
                if preferred_expiry and preferred_expiry_at
                else None
            ),
            "fair_price": None,
            "bid_safe_price": None,
            "max_limit_price": None,
            "reference_option_price": None,
            "reference_option_price_source": "unavailable",
            "premium_cap": context.profile["premium_cap"],
            "delta_band": [context.profile["delta_min"], context.profile["delta_max"]],
            "liquidity": {
                "has_live_market": False,
                "spread": None,
                "spread_pct": None,
            },
            "expiry_at": preferred_expiry_at,
            "local_symbol": preferred_local_symbol,
            "con_id": preferred_con_id or None,
            "exchange": preferred_exchange,
            "trading_class": preferred_trading_class,
            "multiplier": preferred_multiplier,
            "currency": preferred_currency,
            "spot": context.spot,
            "captured_at": context.qualified_at.isoformat(),
            "label": (
                f"{context.right}{preferred_strike:g} mkt live"
                if preferred_strike is not None
                else f"{context.right} mkt live"
            ),
        },
        futures_options=context.futures_options,
        valuation_now=context.valuation_now,
        underlying_price=context.underlying_price,
        underlying_quote=context.underlying_quote,
        option_quote_provider=context.option_quote_provider,
    )


def _option_target_candidate_payload(
    context: _OptionTargetPayloadContext,
    item: tuple[float, dict[str, Any], float, str | None],
) -> dict[str, Any] | None:
    rank, candidate, reference, degraded_reason = item
    candidate_expiry = str(candidate.get("expiry") or "")
    candidate_years = _time_to_expiry_years(
        candidate_expiry,
        context.scenario_valuation_at,
        expiry_at=candidate.get("expiry_at"),
        require_exact=True,
    )
    effective_delta = finite_number_or_none(candidate.get("delta"))
    effective_gamma = finite_number_or_none(candidate.get("gamma"))
    if candidate_years is None or effective_delta is None:
        return None
    candidate_theo = _option_model_price(
        context.futures_options,
        spot=context.target_model_spot,
        strike=_num(candidate.get("strike")),
        years=candidate_years,
        iv=context.theoretical_iv,
        right=context.model_right,
        risk_free_rate=context.valuation_rates[0],
        dividend_yield=context.valuation_rates[1],
    )
    if candidate_theo is None:
        return None
    candidate_fair = round(candidate_theo, 4)
    candidate_limit = max(0.01, candidate_fair * 1.03)
    degraded_fields = (
        {"degraded": True, "degraded_reason": degraded_reason}
        if degraded_reason is not None
        else {}
    )
    premium_cap = context.profile["premium_cap"]
    return {
        "ok": True,
        **degraded_fields,
        **context.identity_payload,
        "mode": context.profile["mode"],
        "target_dte": context.target_dte,
        "requested_expiry": context.target_expiry_code,
        "right": candidate["right"],
        "expiry": candidate_expiry,
        "strike": _num(candidate.get("strike")),
        "delta": round(effective_delta, 4),
        "gamma": effective_gamma,
        **_option_target_greek_fields(context, candidate, context.theoretical_iv),
        "iv": context.theoretical_iv,
        "estimated_greeks": True,
        "iv_source": str(candidate.get("iv_source") or "theoretical_default"),
        "abs_gex": finite_number_or_none(candidate.get("abs_gex")),
        "target_underlying_price": context.target,
        **context.target_time_payload,
        "time_to_expiry_years": candidate_years,
        "fair_price": candidate_fair,
        "bid_safe_price": round(max(0.01, min(candidate_fair, candidate_fair * 0.94)), 4),
        "max_limit_price": round(candidate_limit, 4),
        "reference_option_price": round(reference, 4),
        "reference_option_price_source": candidate["reference_option_price_source"],
        "premium_cap": premium_cap,
        "over_cap_by": round(max(0.0, reference - premium_cap), 4),
        "delta_band": [context.profile["delta_min"], context.profile["delta_max"]],
        "liquidity": dict(context.theoretical_liquidity),
        "spread_pct": None,
        **_option_target_contract_transport_fields(candidate),
        "local_symbol": str(candidate.get("local_symbol") or ""),
        "con_id": candidate.get("con_id"),
        "exchange": candidate.get("exchange"),
        "spot": context.spot,
        "score": round(rank, 4),
        "label": (
            f"{context.right}{_num(candidate.get('strike')):g} "
            f"fair {candidate_fair:.2f} limit≤{candidate_limit:.2f}"
        ),
    }


@dataclass(frozen=True, slots=True)
class _OptionTargetRequest:
    instrument_id: str
    route_fingerprint: str
    provider: str
    provider_symbol: str
    identity_payload: dict[str, Any]
    right: str
    mode: str
    dte: str
    valuation_now: datetime
    scenario_valuation_at: datetime
    target_time_payload: dict[str, Any]
    target: float
    futures_options: bool


@dataclass(frozen=True, slots=True)
class _OptionTargetUniverse:
    contracts: list[dict[str, Any]]
    meta: dict[str, Any]
    source: str
    qualified_at: datetime
    futures_options: bool
    theoretical_iv: float
    valuation_rates: tuple[float, float]


@dataclass(frozen=True, slots=True)
class _PreferredOptionContract:
    strike: float | None
    expiry: str
    expiry_at: str
    con_id: int
    local_symbol: str
    exchange: str
    trading_class: str
    multiplier: float | None
    currency: str

    @property
    def requested(self) -> bool:
        return self.con_id > 0


def _option_target_error(
    identity_payload: Mapping[str, Any],
    *,
    message: str,
    status: str | None = None,
    code: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ok": False,
        **({"status": status} if status is not None else {}),
        **({"code": code} if code is not None else {}),
        "message": message,
        **dict(identity_payload),
        **extra,
    }


def _prepare_option_target_request(
    *,
    instrument: dict[str, Any],
    target_price: float,
    target_ts: datetime | None,
    right: str,
    mode: str,
    dte: str,
    now: datetime | None,
    premium_caps: Mapping[str, Any] | None,
) -> tuple[_OptionTargetRequest | None, dict[str, Any] | None]:
    instrument = require_provider_identity(instrument)
    instrument_id = qualified_instrument_id(instrument)
    route_fingerprint = instrument_route_fingerprint(instrument)
    exact_provider = instrument_provider(instrument)
    exact_provider_symbol = provider_symbol(instrument, exact_provider)
    identity_payload = {
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider": exact_provider,
        "provider_symbol": exact_provider_symbol,
    }
    if _option_target_premium_cap(instrument_id, premium_caps) is None:
        return None, _option_target_error(
            identity_payload,
            status="configuration_required",
            code="OPTION_PREMIUM_CAP_REQUIRED",
            message="Option premium cap configuration is required.",
            configuration={"field": "premium_cap", "scope": "instrument_id"},
        )
    if right not in {"auto", "C", "P"}:
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_RIGHT_INVALID",
            message="right must be exactly auto, C or P.",
        )
    if mode not in {"conservative", "normal", "aggressive"}:
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_MODE_INVALID",
            message="mode must be conservative, normal or aggressive.",
        )
    try:
        exact_dte = require_option_target_dte(dte)
    except ValueError:
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_DTE_INVALID",
            message="dte must be exactly 0dte or 1dte.",
        )
    if target_ts is not None and (
        not isinstance(target_ts, datetime)
        or target_ts.tzinfo is None
        or target_ts.utcoffset() is None
    ):
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_TIMESTAMP_INVALID",
            message="target_ts must be a timezone-aware datetime.",
        )
    target = finite_number_or_none(target_price)
    futures_options = instrument_is_futures(instrument)
    if target is None or (not futures_options and target <= 0):
        return None, _option_target_error(
            identity_payload,
            message="target_price must be finite and positive for non-futures instruments.",
        )
    valuation_now = (now or datetime.now(tz=UTC)).astimezone(NY_TZ)
    target_dt = (target_ts or valuation_now).astimezone(NY_TZ)
    scenario_valuation_at = max(target_dt, valuation_now)
    return (
        _OptionTargetRequest(
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            provider=exact_provider,
            provider_symbol=exact_provider_symbol,
            identity_payload=identity_payload,
            right=right,
            mode=mode,
            dte=exact_dte,
            valuation_now=valuation_now,
            scenario_valuation_at=scenario_valuation_at,
            target_time_payload={
                "target_ts": target_dt.isoformat(),
                "valuation_ts": scenario_valuation_at.isoformat(),
                "theta_horizon_seconds": max(
                    round((scenario_valuation_at - valuation_now).total_seconds(), 3),
                    0.0,
                ),
            },
            target=target,
            futures_options=futures_options,
        ),
        None,
    )


def _require_option_target_universe_meta(
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], bool, float, tuple[float, float]]:
    meta = dict(value)
    futures_options = meta.get("futures_options")
    if not isinstance(futures_options, bool):
        raise ValueError("option target futures_options metadata must be boolean")
    if meta.get("market_data_requested") is not False:
        raise ValueError("option contract preview must not request option market data")
    if meta.get("pricing_basis") != "theoretical_chain":
        raise ValueError("option contract preview requires theoretical-chain pricing")
    theoretical_iv = finite_number_or_none(meta.get("theoretical_iv"))
    if theoretical_iv is None or theoretical_iv <= 0:
        raise ValueError("option contract universe requires a positive theoretical IV")
    typed_rates = require_gex_valuation_rates(meta)
    return (
        meta,
        futures_options,
        theoretical_iv,
        (typed_rates["risk_free_rate"], typed_rates["dividend_yield"]),
    )


def _require_option_target_universe(
    value: Mapping[str, Any],
    *,
    request: _OptionTargetRequest,
) -> _OptionTargetUniverse:
    raw_contracts = value.get("contracts")
    meta_value = value.get("meta")
    if not isinstance(raw_contracts, list) or any(
        not isinstance(row, Mapping) for row in raw_contracts
    ):
        raise ValueError("option contract universe must contain exact mappings")
    if not isinstance(meta_value, Mapping):
        raise ValueError("option contract universe metadata is required")
    if value.get("instrument_id") != request.instrument_id:
        raise ValueError("option contract universe instrument identity mismatch")
    if value.get("route_fingerprint") != request.route_fingerprint:
        raise ValueError("option contract universe route generation mismatch")
    universe_source = value.get("source")
    if (
        not isinstance(universe_source, str)
        or not universe_source
        or universe_source != universe_source.strip()
    ):
        raise ValueError("option contract universe source is required")
    qualified_at = parse_gex_timestamp(value.get("qualified_at"))
    if qualified_at is None:
        raise ValueError("option contract universe qualification timestamp is required")
    meta, futures_options, theoretical_iv, valuation_rates = _require_option_target_universe_meta(
        meta_value
    )
    return _OptionTargetUniverse(
        contracts=[dict(row) for row in raw_contracts],
        meta=meta,
        source=universe_source,
        qualified_at=qualified_at,
        futures_options=futures_options,
        theoretical_iv=theoretical_iv,
        valuation_rates=valuation_rates,
    )


def _is_valid_optional_exact_text(value: Any) -> bool:
    return value is None or (isinstance(value, str) and bool(value) and value == value.strip())


def _is_valid_optional_positive_number(value: Any) -> bool:
    return value is None or (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and finite_number_or_none(value) is not None
        and float(value) > 0
    )


def _prepare_preferred_option_contract(
    *,
    strike: float | None,
    expiry: str | None,
    expiry_at: str | None,
    con_id: int | str | None,
    local_symbol: str | None,
    exchange: str | None,
    trading_class: str | None,
    multiplier: float | None,
    currency: str | None,
    identity_payload: Mapping[str, Any],
) -> tuple[_PreferredOptionContract | None, dict[str, Any] | None]:
    if not _is_valid_optional_positive_number(strike):
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_STRIKE_INVALID",
            message="strike must be a positive finite number.",
        )
    if expiry is not None and _expiration_date(expiry) is None:
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_EXPIRY_INVALID",
            message="expiry must be an exact YYYYMMDD provider date.",
        )
    if expiry_at is not None and parse_gex_timestamp(expiry_at) is None:
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_EXPIRY_TIMESTAMP_INVALID",
            message="expiry_at must be a timezone-aware provider timestamp.",
        )
    if local_symbol is not None and not isinstance(local_symbol, str):
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_LOCAL_SYMBOL_INVALID",
            message="local_symbol must be display text.",
        )
    for field_name, value in (
        ("exchange", exchange),
        ("trading_class", trading_class),
        ("currency", currency),
    ):
        if not _is_valid_optional_exact_text(value):
            return None, _option_target_error(
                identity_payload,
                status="invalid_request",
                code=f"OPTION_TARGET_{field_name.upper()}_INVALID",
                message=f"{field_name} must be exact provider transport metadata.",
            )
    if not _is_valid_optional_positive_number(multiplier):
        return None, _option_target_error(
            identity_payload,
            status="invalid_request",
            code="OPTION_TARGET_MULTIPLIER_INVALID",
            message="multiplier must be finite positive provider metadata.",
        )
    exact_con_id = parse_exact_positive_decimal_provider_id(con_id) if con_id is not None else 0
    if con_id is not None and exact_con_id <= 0:
        return None, _option_target_error(
            identity_payload,
            message="Fixed option contract requires an exact canonical provider conId.",
        )
    fixed_facts_supplied = any(
        value not in (None, "")
        for value in (
            strike,
            expiry,
            expiry_at,
            local_symbol,
            exchange,
            trading_class,
            multiplier,
            currency,
        )
    )
    if fixed_facts_supplied and exact_con_id <= 0:
        return None, _option_target_error(
            identity_payload,
            message="Fixed option contract metadata requires the exact provider conId.",
        )
    return (
        _PreferredOptionContract(
            strike=finite_number_or_none(strike),
            expiry=expiry or "",
            expiry_at=expiry_at or "",
            con_id=exact_con_id,
            local_symbol=local_symbol or "",
            exchange=exchange or "",
            trading_class=trading_class or "",
            multiplier=finite_number_or_none(multiplier),
            currency=currency or "",
        ),
        None,
    )


def _fixed_option_contract_mismatch(
    row: Mapping[str, Any],
    *,
    preferred: _PreferredOptionContract,
    right: str,
) -> bool:
    if row.get("right") != right:
        return True
    exact_text_facts = (
        (preferred.expiry, row.get("expiry")),
        (preferred.expiry_at, row.get("expiry_at")),
        (preferred.exchange, row.get("exchange")),
        (preferred.trading_class, row.get("trading_class")),
        (preferred.currency, row.get("currency")),
    )
    if any(expected and actual != expected for expected, actual in exact_text_facts):
        return True
    if (
        preferred.multiplier is not None
        and finite_number_or_none(row.get("multiplier")) != preferred.multiplier
    ):
        return True
    fixed_strike = finite_number_or_none(row.get("strike"))
    return bool(
        preferred.strike is not None
        and (fixed_strike is None or abs(fixed_strike - preferred.strike) > 0.000001)
    )


def _resolve_fixed_option_target(
    *,
    contracts: list[dict[str, Any]],
    preferred: _PreferredOptionContract,
    context: _OptionTargetPayloadContext,
) -> dict[str, Any]:
    fixed_row = next(
        (
            row
            for row in contracts
            if parse_exact_positive_decimal_provider_id(row.get("con_id")) == preferred.con_id
        ),
        None,
    )
    if fixed_row is not None:
        if _fixed_option_contract_mismatch(
            fixed_row,
            preferred=preferred,
            right=context.right,
        ):
            return _option_target_error(
                context.identity_payload,
                status="contract_mismatch",
                code="OPTION_TARGET_CONTRACT_FACT_MISMATCH",
                message="Fixed option contract facts disagree with its provider conId.",
            )
        payload = _fixed_option_target_payload(context, fixed_row)
        if payload is not None:
            return _finalize_option_target_payload(
                payload,
                futures_options=context.futures_options,
                valuation_now=context.valuation_now,
                underlying_price=context.underlying_price,
                underlying_quote=context.underlying_quote,
                option_quote_provider=context.option_quote_provider,
            )
    if context.option_quote_provider is not None:
        if preferred.strike is None or not preferred.expiry or not preferred.exchange:
            return _option_target_error(
                context.identity_payload,
                status="invalid_request",
                code="OPTION_TARGET_CONTRACT_FACTS_REQUIRED",
                message=(
                    "A fixed option missing from cache requires conId, exchange, "
                    "expiry, right and strike."
                ),
            )
        return _fixed_option_target_live_quote_payload(
            context,
            preferred_strike=preferred.strike,
            preferred_expiry=preferred.expiry,
            preferred_expiry_at=preferred.expiry_at,
            preferred_local_symbol=preferred.local_symbol,
            preferred_con_id=preferred.con_id,
            preferred_exchange=preferred.exchange,
            preferred_trading_class=preferred.trading_class,
            preferred_multiplier=preferred.multiplier,
            preferred_currency=preferred.currency,
        )
    return _option_target_error(
        context.identity_payload,
        message="Fixed option contract is not present in the qualified option universe.",
        fixed_contract=True,
        con_id=preferred.con_id or None,
        local_symbol=preferred.local_symbol,
        exchange=preferred.exchange,
        expiry=preferred.expiry,
        expiry_at=preferred.expiry_at,
        trading_class=preferred.trading_class,
        multiplier=preferred.multiplier,
        currency=preferred.currency,
        right=context.right,
        strike=preferred.strike,
    )


def _unpriced_option_target_result(
    context: _OptionTargetPayloadContext,
    rejected_rows: list[tuple[float, dict[str, Any], float, str | None]],
    *,
    choice_limit: int,
) -> dict[str, Any]:
    rejected_rows.sort(key=lambda item: item[0], reverse=True)
    alternatives = []
    for rejected_item in rejected_rows:
        payload = _option_target_candidate_payload(context, rejected_item)
        if payload is not None:
            alternatives.append(payload)
        if len(alternatives) >= choice_limit:
            break
    alternatives.sort(key=lambda payload: _num(payload.get("strike")))
    dte_label = context.target_dte.upper()
    if alternatives:
        return _option_target_error(
            context.identity_payload,
            message=(
                f"No {dte_label} {context.right} contract satisfied the "
                "theoretical pricing and risk filters."
            ),
            right=context.right,
            target_dte=context.target_dte,
            requested_expiry=context.target_expiry_code,
            premium_cap=context.profile["premium_cap"],
            rejected_liquidity=0,
            alternatives=alternatives,
        )
    return _option_target_error(
        context.identity_payload,
        message=(
            f"No {dte_label} {context.right} contracts could be priced from the "
            "qualified option chain."
        ),
        right=context.right,
        target_dte=context.target_dte,
        premium_cap=context.profile["premium_cap"],
        rejected_liquidity=0,
        alternatives=alternatives,
    )


def _compact_option_target_candidates(
    context: _OptionTargetPayloadContext,
    scored_rows: list[tuple[float, dict[str, Any], float]],
    rejected_rows: list[tuple[float, dict[str, Any], float, str | None]],
    *,
    selected_strike: float,
    choice_limit: int,
) -> list[dict[str, Any]]:
    choice_rows = [(rank, candidate, reference, None) for rank, candidate, reference in scored_rows]
    choice_rows.extend(rejected_rows)
    choice_rows.sort(
        key=lambda item: (
            abs(_num(item[1].get("strike")) - selected_strike),
            _num(item[1].get("strike")),
        )
    )
    compact_candidates = []
    for choice_row in choice_rows:
        candidate_payload = _option_target_candidate_payload(context, choice_row)
        if candidate_payload is not None:
            compact_candidates.append(candidate_payload)
        if len(compact_candidates) >= choice_limit:
            break
    compact_candidates.sort(key=lambda payload: _num(payload.get("strike")))
    return compact_candidates


def _price_selected_option_target(
    context: _OptionTargetPayloadContext,
    row: dict[str, Any],
    reference_price: float,
    alternatives: list[dict[str, Any]],
) -> dict[str, Any]:
    expiry = str(row.get("expiry") or "")
    years = _time_to_expiry_years(
        expiry,
        context.scenario_valuation_at,
        expiry_at=row.get("expiry_at"),
        require_exact=True,
    )
    if years is None:
        return _option_target_error(
            context.identity_payload,
            status="data_unavailable",
            code="OPTION_TARGET_EXPIRY_TIME_UNAVAILABLE",
            message="Exact provider option expiry time is unavailable.",
        )
    iv = _num(row.get("iv"))
    risk_free_rate, dividend_yield = context.valuation_rates
    theoretical_price = _option_model_price(
        context.futures_options,
        spot=context.target_model_spot,
        strike=_num(row.get("strike")),
        years=years,
        iv=iv,
        right=context.model_right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    if theoretical_price is None:
        return _option_target_error(
            context.identity_payload,
            message="Could not price selected option contract.",
        )
    fair_price = round(theoretical_price, 4)
    bid_safe = max(0.01, min(fair_price, fair_price * 0.94))
    max_limit = max(0.01, fair_price * 1.03)
    scenario_prices = {
        label: _option_model_price(
            context.futures_options,
            spot=context.target_model_spot,
            strike=_num(row.get("strike")),
            years=years,
            iv=scenario_iv,
            right=context.model_right,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
        for label, scenario_iv in (
            ("iv_down_10", max(iv * 0.90, 0.0001)),
            ("iv_up_10", iv * 1.10),
        )
    }
    return _finalize_option_target_payload(
        {
            "ok": True,
            **context.identity_payload,
            "mode": context.profile["mode"],
            "target_dte": context.target_dte,
            "requested_expiry": context.target_expiry_code,
            "right": context.right,
            "expiry": expiry,
            "strike": _num(row.get("strike")),
            "delta": round(_num(row.get("delta")), 4),
            "gamma": finite_number_or_none(row.get("gamma")),
            **_option_target_greek_fields(context, row, iv),
            "iv": iv,
            "iv_source": str(row.get("iv_source") or "theoretical_default"),
            "risk_free_rate": risk_free_rate,
            "dividend_yield": dividend_yield,
            "estimated_greeks": True,
            "abs_gex": finite_number_or_none(row.get("abs_gex")),
            "target_underlying_price": context.target,
            **context.target_time_payload,
            "time_to_expiry_years": years,
            "fair_price": fair_price,
            "bid_safe_price": round(bid_safe, 4),
            "max_limit_price": round(max_limit, 4),
            "iv_scenarios": {
                "iv_down_10": (
                    None
                    if scenario_prices["iv_down_10"] is None
                    else round(scenario_prices["iv_down_10"], 4)
                ),
                "base": fair_price,
                "iv_up_10": (
                    None
                    if scenario_prices["iv_up_10"] is None
                    else round(scenario_prices["iv_up_10"], 4)
                ),
            },
            "reference_option_price": round(reference_price, 4),
            "reference_option_price_source": row["reference_option_price_source"],
            "premium_cap": context.profile["premium_cap"],
            "delta_band": [context.profile["delta_min"], context.profile["delta_max"]],
            "liquidity": dict(context.theoretical_liquidity),
            "alternatives": alternatives,
            **_option_target_contract_transport_fields(row),
            "local_symbol": str(row.get("local_symbol") or ""),
            "con_id": row.get("con_id"),
            "exchange": row.get("exchange"),
            "spot": context.spot,
            "captured_at": context.qualified_at.isoformat(),
            "label": (
                f"{context.right}{_num(row.get('strike')):g} "
                f"fair {fair_price:.2f} limit≤{max_limit:.2f}"
            ),
        },
        futures_options=context.futures_options,
        valuation_now=context.valuation_now,
        underlying_price=context.underlying_price,
        underlying_quote=context.underlying_quote,
        option_quote_provider=context.option_quote_provider,
    )


def _price_ranked_option_targets(
    context: _OptionTargetPayloadContext,
    expiry_rows: list[dict[str, Any]],
    *,
    target_premium: float,
    choice_limit: int,
) -> dict[str, Any]:
    scored_rows, rejected_rows = _rank_option_target_candidates(
        expiry_rows,
        profile=context.profile,
        premium_cap=context.profile["premium_cap"],
        target_premium=target_premium,
    )
    if not scored_rows:
        return _unpriced_option_target_result(
            context,
            rejected_rows,
            choice_limit=choice_limit,
        )
    scored_rows.sort(key=lambda item: item[0], reverse=True)
    _, row, reference_price = scored_rows[0]
    alternatives = _compact_option_target_candidates(
        context,
        scored_rows,
        rejected_rows,
        selected_strike=_num(row.get("strike")),
        choice_limit=choice_limit,
    )
    return _price_selected_option_target(
        context,
        row,
        reference_price,
        alternatives,
    )


def _load_option_target_universe(
    request: _OptionTargetRequest,
    *,
    underlying_price: float | None,
    option_contract_provider: Callable[..., Mapping[str, Any]] | None,
    con_id: int | str | None,
    sec_type: str | None,
    strike: float | None,
    expiry: str | None,
    expiry_at: str | None,
    right: str,
    exchange: str | None,
    local_symbol: str | None,
    trading_class: str | None,
    multiplier: float | None,
    currency: str | None,
    store: Any | None,
) -> tuple[
    _OptionTargetUniverse | None,
    float | None,
    dict[str, Any],
    dict[str, Any] | None,
]:
    identity_payload = request.identity_payload
    if option_contract_provider is None:
        return (
            None,
            None,
            identity_payload,
            _option_target_error(
                identity_payload,
                status="data_unavailable",
                code="OPTION_CONTRACT_UNIVERSE_UNAVAILABLE",
                message="A provider-qualified option contract universe is required.",
            ),
        )
    universe_spot = _option_valuation_spot(
        meta_spot=None,
        underlying_price=underlying_price,
        target=request.target,
    )
    if universe_spot is None or (not request.futures_options and universe_spot <= 0):
        raise ValueError(
            "option contract qualification requires a finite underlying price "
            "that is positive for non-futures instruments"
        )
    universe_value = option_contract_provider(
        spot=universe_spot,
        dte=request.dte,
        fixed_contract=(
            {
                "con_id": con_id,
                "sec_type": sec_type,
                "strike": strike,
                "expiry": expiry,
                "expiry_at": expiry_at,
                "right": right,
                "exchange": exchange,
                "local_symbol": local_symbol,
                "trading_class": trading_class,
                "multiplier": multiplier,
                "currency": currency,
            }
            if con_id is not None
            else None
        ),
        store=store,
    )
    if not isinstance(universe_value, Mapping):
        raise ValueError("option contract provider returned an invalid universe")
    universe = _require_option_target_universe(universe_value, request=request)
    if universe.futures_options is not request.futures_options:
        raise ValueError("option contract universe model type disagrees with instrument route")
    identity_payload = {
        **identity_payload,
        "option_universe_source": universe.source,
        "option_universe_qualified_at": universe.qualified_at.isoformat(),
        "pricing_basis": "theoretical_chain",
        "subscription_state": "not_subscribed",
    }
    if not universe.contracts:
        return (
            None,
            None,
            identity_payload,
            _option_target_error(
                identity_payload,
                message=(f"No provider-qualified option contracts for {request.provider_symbol}."),
            ),
        )
    _require_option_target_contracts(
        universe.contracts,
        futures_options=universe.futures_options,
    )
    return universe, universe_spot, identity_payload, None


def option_target_price(
    *,
    instrument: dict[str, Any],
    target_price: float,
    target_ts: datetime | None = None,
    right: str = "auto",
    mode: str = "normal",
    dte: str = "1dte",
    strike: float | None = None,
    expiry: str | None = None,
    expiry_at: str | None = None,
    con_id: int | str | None = None,
    sec_type: str | None = None,
    local_symbol: str | None = None,
    exchange: str | None = None,
    trading_class: str | None = None,
    multiplier: float | None = None,
    currency: str | None = None,
    underlying_price: float | None = None,
    underlying_quote: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    premium_caps: Mapping[str, Any] | None = None,
    option_contract_provider: Callable[..., Mapping[str, Any]] | None = None,
    option_quote_provider: Callable[[dict[str, Any]], Mapping[str, Any] | None] | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    choice_limit = 7
    request, request_error = _prepare_option_target_request(
        instrument=instrument,
        target_price=target_price,
        target_ts=target_ts,
        right=right,
        mode=mode,
        dte=dte,
        now=now,
        premium_caps=premium_caps,
    )
    if request_error is not None:
        return request_error
    assert request is not None
    universe, universe_spot, identity_payload, universe_error = _load_option_target_universe(
        request,
        underlying_price=underlying_price,
        option_contract_provider=option_contract_provider,
        con_id=con_id,
        sec_type=sec_type,
        strike=strike,
        expiry=expiry,
        expiry_at=expiry_at,
        right=right,
        exchange=exchange,
        local_symbol=local_symbol,
        trading_class=trading_class,
        multiplier=multiplier,
        currency=currency,
        store=store,
    )
    if universe_error is not None:
        return universe_error
    assert universe is not None and universe_spot is not None
    contracts = universe.contracts
    spot = finite_number_or_none(universe.meta.get("spot")) or universe_spot
    valuation_spot = _option_valuation_spot(
        meta_spot=spot,
        underlying_price=underlying_price,
        target=request.target,
    )
    exact_right = request.right
    if exact_right == "auto":
        # Option points price a planned counter-trend limit at a channel boundary:
        # below spot means CALL-for-bounce, above spot means PUT-for-rejection.
        exact_right = "C" if valuation_spot is None or request.target <= valuation_spot else "P"
    if valuation_spot is None or (not universe.futures_options and valuation_spot <= 0):
        raise ValueError(
            "option target pricing requires a finite underlying value "
            "that is positive for non-futures instruments"
        )
    _characterize_option_target_contracts(
        contracts,
        futures_options=universe.futures_options,
        valuation_spot=valuation_spot,
        valuation_now=request.valuation_now,
        theoretical_iv=universe.theoretical_iv,
        valuation_rates=universe.valuation_rates,
    )
    preferred, preferred_error = _prepare_preferred_option_contract(
        strike=strike,
        expiry=expiry,
        expiry_at=expiry_at,
        con_id=con_id,
        local_symbol=local_symbol,
        exchange=exchange,
        trading_class=trading_class,
        multiplier=multiplier,
        currency=currency,
        identity_payload=identity_payload,
    )
    if preferred_error is not None:
        return preferred_error
    assert preferred is not None
    target_dte = request.dte
    dte_label = target_dte.upper()
    (
        available_expiries,
        provider_expiry_ladder,
        eligible_expiries,
    ) = _eligible_option_target_expiries(
        contracts,
        right=exact_right,
        target_dte=target_dte,
        valuation_now=request.valuation_now,
        scenario_valuation_at=request.scenario_valuation_at,
    )
    if preferred.expiry:
        target_expiry_code = preferred.expiry
    else:
        if not eligible_expiries:
            return _option_target_error(
                identity_payload,
                message=f"Cached snapshot has no provider-qualified {dte_label} expiry.",
                target_dte=target_dte,
                target_expiry="",
                available_expiries=available_expiries,
                provider_expiries=provider_expiry_ladder,
            )
        target_expiry_code = eligible_expiries[0]
    expiry_rows = _option_target_expiry_rows(
        contracts,
        right=exact_right,
        target_dte=target_dte,
        target_expiry_code=target_expiry_code,
        scenario_valuation_at=request.scenario_valuation_at,
        target=request.target,
        preferred_strike=preferred.strike,
        preferred_expiry=preferred.expiry,
    )
    if not expiry_rows:
        return _option_target_error(
            identity_payload,
            message=(
                f"No provider-qualified {dte_label} {exact_right} contracts "
                f"for {target_expiry_code}."
            ),
            target_dte=target_dte,
            target_expiry=target_expiry_code,
            available_expiries=available_expiries,
        )
    profile = _option_target_profile(request.instrument_id, request.mode, premium_caps)
    premium_cap = profile["premium_cap"]
    target_premium = premium_cap * (0.72 if target_dte == "0dte" else profile["premium_factor"])
    target_model_spot = _option_fair_spot_for_target(request.target, valuation_spot)
    theoretical_liquidity = {
        "bid": None,
        "ask": None,
        "spread": None,
        "spread_pct": None,
        "volume": None,
        "open_interest": None,
        "has_live_market": False,
    }
    payload_context = _OptionTargetPayloadContext(
        identity_payload=identity_payload,
        profile=profile,
        target_dte=target_dte,
        target_expiry_code=target_expiry_code,
        right=exact_right,
        target=request.target,
        target_time_payload=request.target_time_payload,
        futures_options=universe.futures_options,
        target_model_spot=target_model_spot,
        scenario_valuation_at=request.scenario_valuation_at,
        theoretical_iv=universe.theoretical_iv,
        valuation_rates=universe.valuation_rates,
        theoretical_liquidity=theoretical_liquidity,
        spot=spot,
        qualified_at=universe.qualified_at,
        valuation_now=request.valuation_now,
        underlying_price=underlying_price,
        underlying_quote=underlying_quote,
        option_quote_provider=option_quote_provider,
    )

    if preferred.requested:
        return _resolve_fixed_option_target(
            contracts=contracts,
            preferred=preferred,
            context=payload_context,
        )

    return _price_ranked_option_targets(
        payload_context,
        expiry_rows,
        target_premium=target_premium,
        choice_limit=choice_limit,
    )
