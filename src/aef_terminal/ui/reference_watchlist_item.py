from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.data.instrument_identity import (
    instrument_asset_class,
    instrument_key,
    instrument_provider,
    provider_contract_id,
    require_exact_identity_text,
    require_provider_identity,
    require_exact_instrument_id_sequence,
    qualified_instrument_id,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.settings_contract import watchlist_presentation_payload


def instrument_identity_id(item: dict[str, Any]) -> str:
    return require_exact_identity_text(
        item.get("instrument_id"),
        field="instrument_id",
    )


def instrument_session_metadata(item: dict[str, Any]) -> dict[str, Any]:
    explicit = item.get("session")
    if isinstance(explicit, dict) and explicit.get("calendar"):
        return dict(explicit)
    provider = instrument_provider(item)
    return {
        "provider": provider,
        "calendar": "unknown",
        "family": "unknown",
        "timezone": "unknown",
    }


def materialize_watchlist_instrument(item: dict[str, Any]) -> dict[str, Any]:
    source = require_provider_identity(item)
    key = instrument_key(source)
    provider = instrument_provider(source)
    provider_symbol = require_exact_identity_text(
        source.get("provider_symbol"),
        field="provider_symbol",
    )
    display = require_exact_identity_text(
        source.get("display"),
        field="display",
    )
    quote_only = source.get("quote_only", False)
    if not isinstance(quote_only, bool):
        raise ValueError("quote_only must be a boolean")
    materialized: dict[str, Any] = {
        "key": key,
        "instrument_key": key,
        "provider": provider,
        "display": display,
        "provider_symbol": provider_symbol,
    }
    for field in ("name", "profile"):
        if source.get(field) not in (None, ""):
            materialized[field] = source[field]
    for field in ("session", "contract_identity", "continuous_series"):
        if isinstance(source.get(field), dict):
            materialized[field] = dict(source[field])
    materialized["presentation"] = watchlist_presentation_payload(source.get("presentation"))
    materialized["provider_contract_id"] = provider_contract_id(source)
    materialized["asset_class"] = instrument_asset_class(source)
    materialized["quote_only"] = quote_only
    materialized["instrument_id"] = require_exact_identity_text(
        source.get("instrument_id"),
        field="instrument_id",
    )
    materialized["profile"] = resolve_instrument_profile(materialized).key
    materialized["session"] = instrument_session_metadata(materialized)
    qualified_instrument_id(materialized)
    route = route_instrument(materialized)
    materialized["route_fingerprint"] = route.fingerprint
    materialized["session_contract_id"] = route.adapter.session_contract_id(route.instrument)
    return require_provider_identity(materialized)


def materialize_watchlist_instruments(
    instruments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in instruments:
        source = require_provider_identity(item)
        identity = instrument_identity_id(source)
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        merged.append(materialize_watchlist_instrument(source))
    return merged


def selected_instruments(
    instruments: list[dict[str, Any]],
    instrument_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    merged = materialize_watchlist_instruments(instruments)
    if instrument_ids is None:
        return merged
    wanted = require_exact_instrument_id_sequence(instrument_ids, allow_empty=True)
    instruments_by_id = {instrument_identity_id(instrument): instrument for instrument in merged}
    return [
        instruments_by_id[instrument_id]
        for instrument_id in wanted
        if instrument_id in instruments_by_id
    ]


def instrument_provider_keys(item: dict[str, Any]) -> set[str]:
    provider = instrument_provider(item)
    return {provider} if provider else set()


def queue_watchlist_session_refresh(item: dict[str, Any]) -> None:
    from aef_terminal.data.providers import route_instrument

    route = route_instrument(item)
    if not route.adapter.capabilities.trading_hours or route.adapter.continuous_session(
        route.instrument
    ):
        return
    from aef_terminal.data.provider_sessions import mark_provider_trading_hours_stale

    mark_provider_trading_hours_stale(instrument=route.instrument)
