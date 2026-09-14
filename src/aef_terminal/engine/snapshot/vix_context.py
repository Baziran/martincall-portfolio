from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.market_data import (
    market_data_observation_time,
    market_data_price_source_is_current,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.analyze.features import VixContextInput, VixContextSample
from aef_terminal.runtime.timeframes import parse_aware_utc_ts


VIX_CONTEXT_LOOKBACK = timedelta(hours=8)
VIX_CONTEXT_MAX_AGE = timedelta(seconds=60)
VIX_CONTEXT_MAX_SAMPLES = 240


def _finite_quote_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _qualified_vix_sample(
    row: Mapping[str, Any],
    *,
    instrument_id: str,
    route_fingerprint: str,
    provider_symbol: str,
) -> VixContextSample | None:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    if (
        payload.get("instrument_id") != instrument_id
        or payload.get("route_fingerprint") != route_fingerprint
        or payload.get("provider_symbol") != provider_symbol
        or payload.get("market_data_entitlement") != "live"
        or payload.get("is_delayed") is not False
        or payload.get("is_stale") is True
    ):
        return None
    observed_at, time_basis = market_data_observation_time(payload)
    price_source = payload.get("price_source")
    if observed_at is None or not market_data_price_source_is_current(price_source, time_basis):
        return None
    stored_at = parse_aware_utc_ts(row.get("ts"))
    if stored_at is None or stored_at != observed_at:
        return None
    price = _finite_quote_number(payload.get("price"))
    stored_price = _finite_quote_number(row.get("price"))
    if price is None or stored_price is None or abs(price - stored_price) > 0.000001:
        return None
    if price_source == "last":
        last = _finite_quote_number(payload.get("last"))
        if last is None or abs(price - last) > 0.000001:
            return None
    elif price_source == "bid_ask_mid":
        bid = _finite_quote_number(payload.get("bid"))
        ask = _finite_quote_number(payload.get("ask"))
        if bid is None or ask is None or ask < bid:
            return None
        if abs(price - (bid + ask) / 2.0) > 0.000001:
            return None
    else:
        return None
    try:
        return VixContextSample(observed_at=observed_at, price=price)
    except ValueError:
        return None


def load_vix_context_input(
    store: Any | None,
    *,
    analysis_as_of_utc: datetime,
    config: AppConfig | None = None,
) -> VixContextInput | None:
    """Read and qualify the exact configured VIX route for snapshot analysis."""

    if store is None:
        return None
    if analysis_as_of_utc.tzinfo is None or analysis_as_of_utc.utcoffset() is None:
        raise ValueError("analysis_as_of_utc must be timezone-aware")
    current = analysis_as_of_utc.astimezone(UTC)
    configured_instrument_id = (config or AppConfig()).vix_instrument_id
    if not isinstance(configured_instrument_id, str) or not configured_instrument_id:
        return None
    instrument = store.lookup_instrument(configured_instrument_id, watchlist_only=False)
    if not isinstance(instrument, dict):
        return None
    route = route_instrument(instrument)
    if route.instrument_id != configured_instrument_id:
        raise ValueError("configured VIX instrument identity disagrees with storage")
    rows = store.read_quote_snapshots(
        route.instrument_id,
        route.fingerprint,
        current - VIX_CONTEXT_LOOKBACK,
        current + timedelta(seconds=1),
        limit=VIX_CONTEXT_MAX_SAMPLES,
        provider=route.provider,
    )
    if not isinstance(rows, list):
        raise TypeError("VIX quote snapshot storage must return a list")
    samples = tuple(
        sample
        for row in rows
        if isinstance(row, Mapping)
        and (
            sample := _qualified_vix_sample(
                row,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                provider_symbol=route.provider_symbol,
            )
        )
        is not None
    )
    if not samples:
        return None
    age = current - samples[-1].observed_at
    if age < timedelta(0) or age > VIX_CONTEXT_MAX_AGE:
        return None
    return VixContextInput(
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        provider=route.provider,
        provider_symbol=route.provider_symbol,
        samples=samples,
    )
