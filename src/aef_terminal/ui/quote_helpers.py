from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.market_data import (
    market_data_observation_time,
    market_data_price_source_is_current,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none


def quote_price(row: dict[str, Any] | None) -> float | None:
    if not isinstance(row, dict):
        return None
    return exact_finite_number_or_none(row.get("price"))


def _current_quote_geometry(
    row: dict[str, Any],
) -> tuple[float | None, float | None, float | None]:
    _observed_at, time_basis = market_data_observation_time(row)
    price_source = row.get("price_source")
    if not market_data_price_source_is_current(price_source, time_basis):
        return None, None, None
    selected_price = exact_finite_number_or_none(row.get("price"))
    if selected_price is None:
        return None, None, None
    if price_source == "last":
        last = exact_finite_number_or_none(row.get("last"))
        if last is None or abs(selected_price - last) > 0.000001:
            return None, None, None
        return selected_price, selected_price, selected_price
    if price_source != "bid_ask_mid":
        return None, None, None
    bid = exact_finite_number_or_none(row.get("bid"))
    ask = exact_finite_number_or_none(row.get("ask"))
    if bid is None or ask is None or ask < bid:
        return None, None, None
    midpoint = (bid + ask) / 2.0
    if abs(selected_price - midpoint) > 0.000001:
        return None, None, None
    return selected_price, bid, ask


def current_quote_price(row: dict[str, Any]) -> float | None:
    """Return one exact provider-selected current trade/midpoint fact."""

    return _current_quote_geometry(row)[0]


def execution_quote_geometry(
    row: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None]:
    if (
        not isinstance(row, dict)
        or row.get("status") != "live"
        or row.get("is_stale") is not False
        or row.get("is_delayed") is not False
    ):
        return None, None, None
    return _current_quote_geometry(row)


def quote_snapshot_ts(row: dict[str, Any] | None) -> datetime | None:
    if not isinstance(row, dict):
        return None
    observed_at, _basis = market_data_observation_time(row)
    return observed_at


def quote_envelope(
    row: dict[str, Any],
    *,
    received_at: datetime | None = None,
    stale_after_seconds: float = 5.0,
) -> dict[str, Any]:
    now = received_at or datetime.now(tz=UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("QUOTE_RECEIVED_AT_MUST_BE_AWARE")
    now = now.astimezone(UTC)
    freshness_limit = max(float(stale_after_seconds), 0.1)
    raw_bid = exact_finite_number_or_none(row.get("bid"))
    raw_ask = exact_finite_number_or_none(row.get("ask"))
    raw_last = exact_finite_number_or_none(row.get("last"))
    midpoint = (
        (raw_bid + raw_ask) / 2.0
        if raw_bid is not None and raw_ask is not None and raw_ask >= raw_bid
        else None
    )
    raw_last_provider_ts = row.get("last_provider_ts")
    if raw_last_provider_ts is None and row.get("price_source") == "last":
        raw_last_provider_ts = row.get("provider_ts")
    last_observed_at, _last_basis = market_data_observation_time(
        {
            "time_basis": "provider_event",
            "provider_ts": raw_last_provider_ts,
            "received_at": None,
        }
    )
    raw_bid_ask_received_at = row.get("bid_ask_received_at")
    if raw_bid_ask_received_at is None and midpoint is not None:
        raw_bid_ask_received_at = row.get("received_at")
    bid_ask_observed_at, _bid_ask_basis = market_data_observation_time(
        {
            "time_basis": "client_receive",
            "provider_ts": None,
            "received_at": raw_bid_ask_received_at,
        }
    )
    raw_last_age_seconds = (
        (now - last_observed_at).total_seconds() if last_observed_at is not None else None
    )
    raw_bid_ask_age_seconds = (
        (now - bid_ask_observed_at).total_seconds() if bid_ask_observed_at is not None else None
    )
    last_is_fresh = bool(
        raw_last is not None
        and raw_last_age_seconds is not None
        and -freshness_limit <= raw_last_age_seconds <= freshness_limit
    )
    bid_ask_is_fresh = bool(
        midpoint is not None
        and raw_bid_ask_age_seconds is not None
        and -freshness_limit <= raw_bid_ask_age_seconds <= freshness_limit
    )
    selected_row = dict(row)
    observed_at, time_basis = market_data_observation_time(selected_row)
    provider_ts = observed_at if time_basis == "provider_event" else None
    reported_received_at, _received_basis = market_data_observation_time(
        {
            "time_basis": "client_receive",
            "received_at": selected_row.get("received_at"),
            "provider_ts": None,
        }
    )
    client_received_at = reported_received_at
    raw_age_seconds = (now - observed_at).total_seconds() if observed_at is not None else None
    age_seconds = max(raw_age_seconds, 0.0) if raw_age_seconds is not None else None
    market_data_entitlement = selected_row.get("market_data_entitlement")
    if market_data_entitlement not in {
        "live",
        "frozen",
        "delayed",
        "delayed_frozen",
        "unknown",
    }:
        market_data_entitlement = "unknown"
    expected_delayed = market_data_entitlement in {"delayed", "delayed_frozen"}
    raw_is_delayed = selected_row.get("is_delayed")
    is_delayed = raw_is_delayed if type(raw_is_delayed) is bool else expected_delayed
    delayed_fact_is_exact = type(raw_is_delayed) is bool and raw_is_delayed is expected_delayed
    is_stale = (
        observed_at is None
        or raw_age_seconds is None
        or raw_age_seconds < -freshness_limit
        or raw_age_seconds > freshness_limit
    )
    has_price = quote_price(selected_row) is not None
    status = "unavailable"
    if has_price:
        if market_data_entitlement in {
            "frozen",
            "delayed",
            "delayed_frozen",
            "unknown",
        }:
            status = market_data_entitlement
        elif current_quote_price(selected_row) is None or not delayed_fact_is_exact:
            status = "unavailable"
        else:
            status = "stale" if is_stale else "live"
    if raw_last is None:
        last_status = "unavailable"
    elif market_data_entitlement != "live":
        last_status = market_data_entitlement
    elif raw_last_age_seconds is None:
        last_status = "unavailable"
    else:
        last_status = "live" if last_is_fresh else "stale"
    if midpoint is None:
        bid_ask_status = "unavailable"
    elif market_data_entitlement != "live":
        bid_ask_status = market_data_entitlement
    elif raw_bid_ask_age_seconds is None:
        bid_ask_status = "unavailable"
    else:
        bid_ask_status = "live" if bid_ask_is_fresh else "stale"
    return {
        **selected_row,
        "provider_ts": provider_ts.isoformat() if provider_ts is not None else None,
        "received_at": (client_received_at.isoformat() if client_received_at is not None else None),
        "time_basis": time_basis,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "last_provider_ts": (
            last_observed_at.isoformat() if last_observed_at is not None else None
        ),
        "last_age_seconds": (
            round(max(raw_last_age_seconds, 0.0), 3) if raw_last_age_seconds is not None else None
        ),
        "last_status": last_status,
        "bid_ask_received_at": (
            bid_ask_observed_at.isoformat() if bid_ask_observed_at is not None else None
        ),
        "bid_ask_age_seconds": (
            round(max(raw_bid_ask_age_seconds, 0.0), 3)
            if raw_bid_ask_age_seconds is not None
            else None
        ),
        "bid_ask_status": bid_ask_status,
        "entitlement": str(selected_row.get("entitlement") or "unknown"),
        "is_delayed": is_delayed,
        "is_stale": is_stale,
        "status": status,
    }


def quote_is_execution_eligible(row: dict[str, Any] | None) -> bool:
    return execution_quote_geometry(row)[0] is not None


def quote_envelope_next_transition(
    row: dict[str, Any],
    *,
    received_at: datetime,
    stale_after_seconds: float,
) -> float:
    """Next wall-clock boundary of the envelope's categorical freshness facts.

    Continuous ages do not create revisions. The inclusive fresh upper bound
    changes only on the first representable datetime after the deadline.
    """

    limit = timedelta(seconds=max(float(stale_after_seconds), 0.1))
    observations = (
        market_data_observation_time(row)[0],
        market_data_observation_time(
            {"time_basis": "provider_event", "provider_ts": row.get("last_provider_ts")}
        )[0],
        market_data_observation_time(
            {"time_basis": "client_receive", "received_at": row.get("bid_ask_received_at")}
        )[0],
    )
    return min(
        (
            boundary.timestamp()
            for observed_at in observations
            if observed_at is not None
            for boundary in (observed_at - limit, observed_at + limit + timedelta(microseconds=1))
            if boundary > received_at
        ),
        default=float("inf"),
    )
