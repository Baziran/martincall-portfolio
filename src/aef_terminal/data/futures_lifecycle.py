from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.instrument_identity import (
    current_futures_contract,
    futures_root,
    instrument_is_futures_root,
    instrument_provider,
    qualified_instrument_id,
    require_exact_identity_text,
    require_exact_positive_number,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_contract import FuturesRouteTransition


CURRENT_CONTRACT_MAX_AGE = timedelta(minutes=30)


def current_contract_is_stale(
    instrument: dict[str, Any],
    *,
    now: datetime | None = None,
    max_age: timedelta = CURRENT_CONTRACT_MAX_AGE,
) -> bool:
    if not instrument_is_futures_root(instrument):
        return False
    current = current_futures_contract(instrument)
    if instrument_provider(instrument) == "ibkr":
        try:
            require_exact_positive_number(
                current.get("min_tick"),
                field="current_contract.min_tick",
            )
        except ValueError:
            return True
    resolved_at = str(current.get("resolved_at") or "").strip()
    if not resolved_at:
        return True
    try:
        resolved = datetime.fromisoformat(resolved_at.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return True
    return (now or datetime.now(tz=UTC)) - resolved > max_age


def current_futures_contract_record(
    instrument: dict[str, Any],
) -> dict[str, Any]:
    if not instrument_is_futures_root(instrument):
        raise ValueError("FUTURES_ROOT_IDENTITY_REQUIRED")
    current = current_futures_contract(instrument)
    root = futures_root(instrument)
    provider = instrument_provider(instrument)
    identity = (
        instrument.get("contract_identity")
        if isinstance(instrument.get("contract_identity"), dict)
        else {}
    )
    contract_key = require_exact_identity_text(
        current.get("contract_key", ""),
        field="current_contract.contract_key",
    )
    current_provider_contract_id = require_exact_identity_text(
        current.get("provider_contract_id", ""),
        field="current_contract.provider_contract_id",
    )
    local_symbol = require_exact_identity_text(
        current.get("local_symbol", ""),
        field="current_contract.local_symbol",
    )
    contract_month = require_exact_identity_text(
        current.get("contract_month", ""),
        field="current_contract.contract_month",
    )
    raw_min_tick = current.get("min_tick")
    min_tick = (
        None
        if raw_min_tick is None
        else require_exact_positive_number(
            raw_min_tick,
            field="current_contract.min_tick",
        )
    )
    if not provider or not root or not contract_key or not current_provider_contract_id:
        raise ValueError(
            f"FUTURES_CURRENT_CONTRACT_INCOMPLETE provider={provider or 'unknown'} instrument_key={root or 'unknown'}"
        )
    metadata = {
        "provider_contract_id": current_provider_contract_id,
        "secid": current.get("secid") or "",
        "shortname": current.get("shortname") or "",
        "name": current.get("name") or "",
        "trading_class": current.get("trading_class") or identity.get("trading_class") or "",
        "source": "provider_lifecycle",
    }
    if min_tick is not None:
        metadata["min_tick"] = min_tick
    return {
        "provider": provider,
        "instrument_id": qualified_instrument_id(instrument),
        "provider_contract_id": current_provider_contract_id,
        "contract_key": contract_key,
        "root": root,
        "exchange": str(current.get("exchange") or identity.get("exchange") or ""),
        "currency": str(current.get("currency") or identity.get("currency") or ""),
        "local_symbol": local_symbol,
        "con_id": current.get("con_id"),
        "expiry": str(current.get("expiry") or ""),
        "contract_month": contract_month,
        "first_notice_date": current.get("first_notice_date"),
        "last_trade_date": current.get("last_trade_date"),
        "is_current": True,
        "metadata": metadata,
    }


def persist_current_futures_contract(
    store: Any,
    instrument: dict[str, Any],
    *,
    route_transition: FuturesRouteTransition | None = None,
) -> None:
    contract = current_futures_contract_record(instrument)
    if route_transition is None:
        store.write_futures_contract(contract)
    else:
        store.write_futures_contract(contract, route_transition=route_transition)


def futures_route_transition(
    previous_instrument: dict[str, Any],
    next_instrument: dict[str, Any],
) -> FuturesRouteTransition:
    previous_identity = qualified_instrument_id(previous_instrument)
    if qualified_instrument_id(next_instrument) != previous_identity:
        raise ValueError("FUTURES_ROUTE_TRANSITION_IDENTITY_MISMATCH")
    previous_route = route_instrument(previous_instrument)
    next_route = route_instrument(next_instrument, expected_source=previous_route.provider)
    return FuturesRouteTransition(
        provider=next_route.provider,
        instrument_id=next_route.instrument_id,
        previous_route_fingerprint=previous_route.fingerprint,
        next_route_fingerprint=next_route.fingerprint,
        previous_session_contract_id=previous_route.adapter.session_contract_id(
            previous_route.instrument
        ),
        next_session_contract_id=next_route.adapter.session_contract_id(next_route.instrument),
    )


def resolve_and_persist_current_contract(
    store: Any,
    instrument: dict[str, Any],
) -> dict[str, Any]:
    provider = instrument_provider(instrument)
    root = futures_root(instrument)
    if not provider or not root:
        raise ValueError("FUTURES_ROOT_IDENTITY_REQUIRED")
    previous_route = route_instrument(instrument)
    bound = previous_route.adapter.resolve_current_contract(previous_route.instrument)
    if not isinstance(bound, dict):
        raise RuntimeError(
            f"FUTURES_CURRENT_CONTRACT_UNRESOLVED provider={provider} instrument_key={root}"
        )
    if qualified_instrument_id(bound) != qualified_instrument_id(instrument):
        raise RuntimeError(
            f"FUTURES_ROOT_BINDING_MISMATCH provider={provider} instrument_key={root}"
        )
    transition = futures_route_transition(instrument, bound)
    persist_current_futures_contract(
        store,
        bound,
        route_transition=transition,
    )
    return bound
