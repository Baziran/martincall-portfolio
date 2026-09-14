"""Canonical Option Point sample, freshness, and quote-admission contracts."""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import NY_TZ
from aef_terminal.data.gex.contracts import (
    require_gex_market_data_entitlement,
    require_gex_option_reference,
)
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
)
from aef_terminal.data.market_data import (
    market_data_observation_time,
    market_data_price_source_is_current,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none

OPTION_TARGET_COMPRESSION_FIELDS = (
    "fair_price",
    "display_fair_price",
    "reference_option_price",
    "max_limit_price",
    "bid_safe_price",
    "live_bid",
    "live_ask",
    "live_mid",
    "live_last",
    "live_underlying_price",
    "underlying_quote_price",
    "previous_live_bid",
    "previous_live_ask",
    "previous_live_mid",
    "previous_reference_option_price",
    "previous_underlying_price",
)
OPTION_TARGET_CURRENT_QUOTE_PRICE_SOURCES = frozenset({"bid_ask_mid"})
OPTION_TARGET_UNDERLYING_REFERENCE_SOURCES = frozenset(
    {"bid_ask_mid", "ibkr_market_price", "last", "previous_close", "bid", "ask"}
)
OPTION_TARGET_SIGNED_UNDERLYING_FIELDS = frozenset(
    {"live_underlying_price", "underlying_quote_price", "previous_underlying_price"}
)
OPTION_TARGET_DTE_VALUES = frozenset({"0dte", "1dte"})
OPTION_TARGET_SEC_TYPES = frozenset({"OPT", "FOP"})
OPTION_TARGET_INTENT_KEYS = frozenset(
    {
        "provider_symbol",
        "mode",
        "right",
        "target_dte",
        "sec_type",
        "con_id",
        "local_symbol",
        "exchange",
        "expiry",
        "expiry_at",
        "strike",
        "trading_class",
        "multiplier",
        "currency",
        "target_delta",
        "estimated_greeks",
    }
)
OPTION_TARGET_IMMUTABLE_CONTRACT_KEYS = frozenset(
    {
        "provider_symbol",
        "right",
        "target_dte",
        "sec_type",
        "con_id",
        "local_symbol",
        "exchange",
        "expiry",
        "expiry_at",
        "strike",
        "trading_class",
        "multiplier",
        "currency",
    }
)
OPTION_TARGET_MARKET_SAMPLE_FIELDS = (
    *OPTION_TARGET_COMPRESSION_FIELDS,
    "previous_live_quote_ts",
    "previous_live_quote_time_basis",
    "previous_underlying_quote_ts",
    "previous_underlying_quote_time_basis",
    "live_quote_ts",
    "live_quote_time_basis",
    "live_quote_status",
    "live_quote_source",
    "live_quote_message",
    "fair_price_status",
    "fair_price_message",
    "live_quote_entitlement",
    "reference_option_price_source",
    "underlying_quote_price_source",
    "underlying_quote_entitlement",
    "underlying_quote_status",
    "underlying_quote_ts",
    "underlying_quote_time_basis",
    "valuation_ts",
    "theta_horizon_seconds",
    "time_to_expiry_years",
    "compression_sample_at",
)


def option_target_dte_for_expiry(
    expiry_at: object,
    *,
    valuation_now: datetime | None = None,
) -> str | None:
    """Classify an exact provider expiry for the Option Point intent contract."""

    exact_expiry_at = parse_gex_timestamp(expiry_at)
    current = valuation_now or datetime.now(tz=UTC)
    if (
        exact_expiry_at is None
        or not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        return None
    if exact_expiry_at.astimezone(UTC) <= current.astimezone(UTC):
        return None
    valuation_date = current.astimezone(NY_TZ).date()
    expiry_date = exact_expiry_at.astimezone(NY_TZ).date()
    if expiry_date == valuation_date:
        return "0dte"
    if expiry_date > valuation_date:
        return "1dte"
    return None


OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS = 30.0
OPTION_TARGET_ROUTE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class OptionTargetCreateResult:
    item: dict[str, Any]
    evicted: tuple[dict[str, Any], ...] = ()


def require_option_target_dte(value: object) -> str:
    """Return one canonical Option Point expiry class."""

    if not isinstance(value, str) or value not in OPTION_TARGET_DTE_VALUES:
        raise ValueError("option target target_dte must be exactly 0dte or 1dte")
    return value


def require_option_target_intent(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one exact provider-qualified Option Point intent."""

    unexpected = set(payload) - OPTION_TARGET_INTENT_KEYS
    if unexpected:
        raise ValueError("option target intent contains unsupported or duplicate fields")
    intent = {
        key: value
        for key, value in payload.items()
        if key in OPTION_TARGET_INTENT_KEYS and value is not None
    }
    intent["provider_symbol"] = require_exact_identity_text(
        intent.get("provider_symbol"),
        field="OPTION_TARGET_PROVIDER_SYMBOL",
    )
    if intent["provider_symbol"] != intent["provider_symbol"].strip():
        raise ValueError("option target provider_symbol must be exact")
    if intent.get("mode") not in {"conservative", "normal", "aggressive"}:
        raise ValueError("option target mode must be conservative, normal or aggressive")
    if intent.get("right") not in {"C", "P"}:
        raise ValueError("option target right must be exactly C or P")
    intent["target_dte"] = require_option_target_dte(intent.get("target_dte"))
    if intent.get("sec_type") not in OPTION_TARGET_SEC_TYPES:
        raise ValueError("option target sec_type must be exactly OPT or FOP")
    con_id = parse_exact_positive_decimal_provider_id(intent.get("con_id"))
    if con_id <= 0:
        raise ValueError("option target requires an exact provider conId")
    intent["con_id"] = con_id
    intent["exchange"] = require_exact_identity_text(
        intent.get("exchange"),
        field="OPTION_TARGET_PROVIDER_EXCHANGE",
    )
    if intent["exchange"] != intent["exchange"].strip():
        raise ValueError("option target exchange must be exact")
    expiry = intent.get("expiry")
    if not isinstance(expiry, str) or len(expiry) != 8 or not expiry.isdigit():
        raise ValueError("option target expiry must be exact YYYYMMDD")
    try:
        datetime.strptime(expiry, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("option target expiry must be a valid provider date") from exc
    parsed_expiry_at = parse_gex_timestamp(intent.get("expiry_at"))
    if parsed_expiry_at is None:
        raise ValueError("option target expiry_at must be an exact provider timestamp")
    intent["expiry_at"] = parsed_expiry_at.astimezone(UTC).isoformat()
    strike = option_target_positive_number(intent.get("strike"))
    if strike is None:
        raise ValueError("option target strike must be positive and finite")
    intent["strike"] = strike
    intent["trading_class"] = require_exact_identity_text(
        intent.get("trading_class"),
        field="OPTION_TARGET_PROVIDER_TRADING_CLASS",
    )
    intent["currency"] = require_exact_identity_text(
        intent.get("currency"),
        field="OPTION_TARGET_PROVIDER_CURRENCY",
    )
    multiplier = option_target_positive_number(intent.get("multiplier"))
    if multiplier is None:
        raise ValueError("option target multiplier must be positive and finite")
    intent["multiplier"] = multiplier
    local_symbol = intent.get("local_symbol")
    if local_symbol is not None and not isinstance(local_symbol, str):
        raise ValueError("option target local_symbol must be display text")
    target_delta = intent.get("target_delta")
    if target_delta is not None:
        if (
            isinstance(target_delta, bool)
            or not isinstance(target_delta, (int, float))
            or not math.isfinite(float(target_delta))
        ):
            raise ValueError("option target target_delta must be finite")
        intent["target_delta"] = float(target_delta)
    estimated_greeks = intent.get("estimated_greeks")
    if estimated_greeks is not None and type(estimated_greeks) is not bool:
        raise ValueError("option target estimated_greeks must be boolean")
    return intent


def option_target_positive_number(value: object) -> float | None:
    """Return one exact finite positive Option Target number without coercion."""

    number = exact_finite_number_or_none(value)
    return number if number is not None and number > 0 else None


def option_target_finite_number(value: object) -> float | None:
    """Return one exact finite signed market number without coercion."""

    return exact_finite_number_or_none(value)


def option_target_market_sample(
    payload: Mapping[str, Any],
    *,
    analysis_as_of_utc: datetime | None = None,
    sec_type: str = "OPT",
) -> dict[str, Any]:
    current = analysis_as_of_utc or datetime.now(tz=UTC)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("analysis_as_of_utc must be timezone-aware")
    current = current.astimezone(UTC)
    if sec_type not in OPTION_TARGET_SEC_TYPES:
        raise ValueError("option target market sample requires exact OPT or FOP scope")
    if "market_sample" in payload:
        raise ValueError("option target market sample input must be a flat producer result")
    sample = {
        key: payload[key]
        for key in OPTION_TARGET_MARKET_SAMPLE_FIELDS
        if key in payload and payload[key] is not None
    }
    for key in OPTION_TARGET_COMPRESSION_FIELDS:
        if key not in sample:
            continue
        raw_value = sample[key]
        signed_underlying = key in OPTION_TARGET_SIGNED_UNDERLYING_FIELDS and sec_type == "FOP"
        value = (
            option_target_finite_number(raw_value)
            if signed_underlying
            else option_target_positive_number(raw_value)
        )
        if value is None:
            requirement = "finite" if signed_underlying else "finite and positive"
            raise ValueError(f"option target market sample {key} must be {requirement}")
        sample[key] = value
    for key in ("compression_sample_at",):
        if key in sample and (
            not isinstance(sample[key], str) or parse_gex_timestamp(sample[key]) is None
        ):
            raise ValueError(f"option target market sample {key} must be timezone-aware")
    if "valuation_ts" in sample and (
        not isinstance(sample["valuation_ts"], str)
        or parse_gex_timestamp(sample["valuation_ts"]) is None
    ):
        raise ValueError("option target market sample valuation_ts must be timezone-aware")
    for key in ("theta_horizon_seconds", "time_to_expiry_years"):
        if key not in sample:
            continue
        value = sample[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError(f"option target market sample {key} must be finite and non-negative")
        sample[key] = float(value)
    timestamp_pairs = (
        ("previous_live_quote_ts", "previous_live_quote_time_basis"),
        (
            "previous_underlying_quote_ts",
            "previous_underlying_quote_time_basis",
        ),
        ("live_quote_ts", "live_quote_time_basis"),
        ("underlying_quote_ts", "underlying_quote_time_basis"),
    )
    for timestamp_field, basis_field in timestamp_pairs:
        timestamp_present = timestamp_field in sample
        basis_present = basis_field in sample
        if timestamp_present != basis_present:
            raise ValueError(
                f"option target market sample {timestamp_field} requires {basis_field}"
            )
        if timestamp_present and (
            not isinstance(sample[timestamp_field], str)
            or parse_gex_timestamp(sample[timestamp_field]) is None
            or not isinstance(sample[basis_field], str)
            or sample[basis_field] not in {"provider_event", "client_receive"}
        ):
            raise ValueError(
                f"option target market sample {timestamp_field} has invalid time basis"
            )
    for key in (
        "live_quote_source",
        "live_quote_message",
        "fair_price_message",
        "reference_option_price_source",
        "underlying_quote_price_source",
        "underlying_quote_status",
        "live_quote_time_basis",
        "underlying_quote_time_basis",
        "previous_live_quote_time_basis",
        "previous_underlying_quote_time_basis",
    ):
        if key in sample and not isinstance(sample[key], str):
            raise ValueError(f"option target market sample {key} must be text")
    status = sample.get("live_quote_status")
    if status is not None and status not in {
        "ok",
        "empty",
        "error",
        "stale",
        "frozen",
        "delayed",
        "delayed_frozen",
        "unknown",
        "display_only",
    }:
        raise ValueError("option target market sample has an invalid quote status")
    fair_status = sample.get("fair_price_status")
    if fair_status is not None and fair_status not in {
        "ok",
        "pending",
        "stale",
        "unavailable",
    }:
        raise ValueError("option target market sample has an invalid fair price status")
    fair_fields = {"fair_price", "max_limit_price", "bid_safe_price"}
    if fair_fields.intersection(sample) and fair_status != "ok":
        raise ValueError("option target fair values require fair_price_status=ok")
    if fair_status == "ok" and "fair_price" not in sample:
        raise ValueError("option target fair_price_status=ok requires fair_price")
    if fair_status in {"pending", "stale", "unavailable"} and fair_fields.intersection(sample):
        raise ValueError("non-authoritative fair price status cannot carry fair values")
    if "display_fair_price" in sample and fair_status != "stale":
        raise ValueError("option target display fair price requires fair_price_status=stale")
    live_quote_entitlement = None
    if "live_quote_entitlement" in sample:
        live_quote_entitlement = require_gex_market_data_entitlement(
            sample["live_quote_entitlement"],
            allow_unknown=True,
        )
    underlying_quote_entitlement = None
    if "underlying_quote_entitlement" in sample:
        underlying_quote_entitlement = require_gex_market_data_entitlement(
            sample["underlying_quote_entitlement"],
            allow_unknown=True,
        )
    underlying_quote_status = sample.get("underlying_quote_status")
    if underlying_quote_status is not None and underlying_quote_status not in {
        "live",
        "frozen",
        "delayed",
        "delayed_frozen",
        "unknown",
        "stale",
        "unavailable",
    }:
        raise ValueError("option target market sample has an invalid underlying quote status")
    if (
        "underlying_quote_price" in sample
        and underlying_quote_entitlement
        in {
            "frozen",
            "delayed",
            "delayed_frozen",
            "unknown",
        }
        and underlying_quote_status != underlying_quote_entitlement
    ):
        raise ValueError("visual underlying quote status must match its exact entitlement")
    if "underlying_quote_price" in sample:
        if sample.get("underlying_quote_price_source") not in (
            OPTION_TARGET_UNDERLYING_REFERENCE_SOURCES
        ):
            raise ValueError("underlying quote reference source is not an exact supported value")
    else:
        sample.pop("underlying_quote_price_source", None)
    if "reference_option_price" in sample:
        require_gex_option_reference(sample)
    else:
        sample.pop("reference_option_price_source", None)
    if status == "ok":
        quote_at = parse_gex_timestamp(sample.get("live_quote_ts"))
        reference_price = sample.get("reference_option_price")
        reference_source = sample.get("reference_option_price_source")
        raw_bid = sample.get("live_bid")
        raw_ask = sample.get("live_ask")
        midpoint = (
            (raw_bid + raw_ask) / 2.0
            if raw_bid is not None and raw_ask is not None and raw_ask >= raw_bid
            else None
        )
        quote_age = (current - quote_at).total_seconds() if quote_at is not None else None
        if (
            sample.get("reference_option_price") is None
            or reference_source not in OPTION_TARGET_CURRENT_QUOTE_PRICE_SOURCES
            or not market_data_price_source_is_current(
                reference_source,
                sample.get("live_quote_time_basis"),
            )
            or midpoint is None
            or not math.isclose(
                reference_price,
                midpoint,
                rel_tol=0.0,
                abs_tol=0.000001,
            )
            or live_quote_entitlement != "live"
            or quote_age is None
            or quote_age < -1.0
            or quote_age > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        ):
            raise ValueError("live option quote facts are missing or stale")
    if fair_status == "ok" and (status != "ok" or "live_underlying_price" not in sample):
        raise ValueError(
            "authoritative fair price requires an authoritative live option/underlying quote pair"
        )
    if status in {"frozen", "delayed", "delayed_frozen", "unknown"} and (
        live_quote_entitlement != status
    ):
        raise ValueError("visual option quote status must match its exact entitlement")
    if "live_underlying_price" in sample:
        underlying_at = parse_gex_timestamp(sample.get("underlying_quote_ts"))
        option_at = parse_gex_timestamp(sample.get("live_quote_ts"))
        underlying_age = (
            (current - underlying_at).total_seconds() if underlying_at is not None else None
        )
        if (
            underlying_quote_entitlement != "live"
            or sample.get("underlying_quote_status") != "live"
            or option_at is None
            or underlying_age is None
            or underlying_age < -1.0
            or underlying_age > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
            or abs((option_at - underlying_at).total_seconds())
            > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        ):
            raise ValueError("live underlying quote facts are missing or stale")
        if not market_data_price_source_is_current(
            sample.get("underlying_quote_price_source"),
            sample.get("underlying_quote_time_basis"),
        ):
            raise ValueError("live underlying quote price source is not current")
        if (
            sample.get("underlying_quote_price") is None
            or abs(sample["live_underlying_price"] - sample["underlying_quote_price"]) > 0.000001
        ):
            raise ValueError("live underlying price must match its quote reference")
    previous_pair_fields = {
        "previous_reference_option_price",
        "previous_underlying_price",
        "previous_live_quote_ts",
        "previous_live_quote_time_basis",
        "previous_underlying_quote_ts",
        "previous_underlying_quote_time_basis",
    }
    present_previous_pair_fields = previous_pair_fields.intersection(sample)
    if present_previous_pair_fields and present_previous_pair_fields != previous_pair_fields:
        raise ValueError("previous option/underlying sample must be atomic")
    if present_previous_pair_fields:
        previous_option = sample["previous_reference_option_price"]
        if previous_option <= 0:
            raise ValueError("previous option price must be positive")
        previous_option_at = parse_gex_timestamp(sample["previous_live_quote_ts"])
        previous_underlying_at = parse_gex_timestamp(sample["previous_underlying_quote_ts"])
        current_option_at = parse_gex_timestamp(sample.get("live_quote_ts"))
        current_underlying_at = parse_gex_timestamp(sample.get("underlying_quote_ts"))
        if (
            previous_option_at is None
            or previous_underlying_at is None
            or current_option_at is None
            or current_underlying_at is None
            or abs((previous_option_at - previous_underlying_at).total_seconds())
            > OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
            or not 0.0
            <= (current_option_at - previous_option_at).total_seconds()
            <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
            or not 0.0
            <= (current_underlying_at - previous_underlying_at).total_seconds()
            <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        ):
            raise ValueError("previous option/underlying sample timestamps are incoherent")
    return sample


def require_option_target_underlying_quote(
    quote: Mapping[str, Any],
    *,
    observed_at: datetime,
    futures_options: bool,
) -> dict[str, Any]:
    """Validate one exact underlying quote and derive execution eligibility."""

    if not isinstance(quote, Mapping):
        raise ValueError("underlying quote must be a typed quote envelope")
    if type(futures_options) is not bool:
        raise TypeError("underlying quote requires a typed OPT/FOP model flag")
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("underlying quote validation time must be timezone-aware")
    raw_price = quote.get("price")
    price_source = quote.get("price_source")
    price = (
        option_target_finite_number(raw_price)
        if futures_options
        else option_target_positive_number(raw_price)
    )
    if raw_price is None:
        if price_source != "unavailable":
            raise ValueError("unavailable underlying quote requires unavailable provenance")
    elif price is None:
        requirement = "finite signed" if futures_options else "finite positive"
        raise ValueError(f"underlying quote price must be a {requirement} number or null")
    elif price_source not in OPTION_TARGET_UNDERLYING_REFERENCE_SOURCES:
        raise ValueError("underlying quote price source is not an exact supported value")
    raw_market_data_type = quote.get("market_data_type")
    reported_entitlement = require_gex_market_data_entitlement(
        quote.get("market_data_entitlement"),
        allow_unknown=True,
    )
    status = quote.get("status")
    if status not in {
        "live",
        "frozen",
        "delayed",
        "delayed_frozen",
        "unknown",
        "stale",
        "unavailable",
    }:
        raise ValueError("underlying quote status is not exact")
    is_stale = quote.get("is_stale")
    is_delayed = quote.get("is_delayed")
    if type(is_stale) is not bool or type(is_delayed) is not bool:
        raise ValueError("underlying quote freshness flags must be typed booleans")
    if is_delayed is not (reported_entitlement in {"delayed", "delayed_frozen"}):
        raise ValueError("underlying quote delayed flag disagrees with entitlement")
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
        raise ValueError("underlying quote ts disagrees with its declared time basis")
    source_time_is_current = (
        quote_at is not None
        and isinstance(raw_observation_ts, str)
        and market_data_price_source_is_current(price_source, time_basis)
    )
    if price is None or (reported_entitlement == "live" and not source_time_is_current):
        expected_status = "unavailable"
    elif reported_entitlement in {
        "frozen",
        "delayed",
        "delayed_frozen",
        "unknown",
    }:
        expected_status = reported_entitlement
    else:
        expected_status = "stale" if is_stale else "live"
    if status != expected_status:
        raise ValueError("underlying quote status disagrees with its typed facts")
    quote_age = (
        (observed_at.astimezone(UTC) - quote_at).total_seconds() if quote_at is not None else None
    )
    number_reader = (
        option_target_finite_number if futures_options else option_target_positive_number
    )
    raw_last = number_reader(quote.get("last"))
    raw_bid = number_reader(quote.get("bid"))
    raw_ask = number_reader(quote.get("ask"))
    raw_mid = (
        (raw_bid + raw_ask) / 2.0
        if raw_bid is not None and raw_ask is not None and raw_ask >= raw_bid
        else None
    )
    if price_source == "last" and (
        raw_last is None
        or price is None
        or not math.isclose(price, raw_last, rel_tol=0.0, abs_tol=0.000001)
    ):
        raise ValueError("underlying quote last provenance disagrees with raw last")
    if price_source == "bid_ask_mid" and (
        raw_mid is None
        or price is None
        or not math.isclose(price, raw_mid, rel_tol=0.0, abs_tol=0.000001)
    ):
        raise ValueError("underlying quote midpoint provenance disagrees with raw bid/ask")
    execution_price = (
        price
        if price is not None
        and source_time_is_current
        and reported_entitlement == "live"
        and status == "live"
        and is_stale is False
        and is_delayed is False
        and quote_age is not None
        and -1.0 <= quote_age <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        else None
    )
    bid_ask_at = parse_gex_timestamp(quote.get("bid_ask_received_at"))
    bid_ask_age = (
        (observed_at.astimezone(UTC) - bid_ask_at).total_seconds()
        if bid_ask_at is not None
        else None
    )
    use_live_midpoint = bool(
        execution_price is None
        and raw_mid is not None
        and reported_entitlement == "live"
        and quote.get("bid_ask_status") == "live"
        and is_delayed is False
        and bid_ask_age is not None
        and -1.0 <= bid_ask_age <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
    )
    if use_live_midpoint:
        price = raw_mid
        price_source = "bid_ask_mid"
        status = "live"
        is_stale = False
        quote_at = bid_ask_at
        time_basis = "client_receive"
        execution_price = raw_mid
    return {
        "price": price,
        "price_source": price_source,
        "market_data_type": raw_market_data_type,
        "market_data_entitlement": reported_entitlement,
        "status": status,
        "provider_ts": quote.get("provider_ts") if time_basis == "provider_event" else None,
        "received_at": (
            bid_ask_at.isoformat()
            if use_live_midpoint and bid_ask_at is not None
            else quote.get("received_at")
        ),
        "quote_ts": (
            bid_ask_at.isoformat()
            if use_live_midpoint and bid_ask_at is not None
            else raw_observation_ts
            if quote_at is not None
            else None
        ),
        "quote_at": quote_at,
        "time_basis": time_basis,
        "is_stale": is_stale,
        "is_delayed": is_delayed,
        "execution_price": execution_price,
    }
