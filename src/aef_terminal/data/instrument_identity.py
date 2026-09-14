from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from typing import Any


FUTURES_ASSET_CLASSES = frozenset({"future"})
INSTRUMENT_IDENTITY_SCOPE_CONTRACT = "contract"
INSTRUMENT_IDENTITY_SCOPE_ROOT = "root"


class InstrumentIdentityError(ValueError):
    """Raised when a provider route cannot be selected from persisted identity."""


class ProviderPriceIncrementError(InstrumentIdentityError):
    """Raised when an attached exact provider price increment is malformed."""


def parse_exact_positive_decimal_provider_id(value: Any) -> int:
    """Parse a typed numeric provider ID without accepting normalized spellings."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if not isinstance(value, str):
        return 0
    text = value
    if not text or not text.isascii() or not text.isdecimal():
        return 0
    parsed = int(text)
    return parsed if parsed > 0 and str(parsed) == text else 0


def require_exact_positive_number(value: object, *, field: str) -> float:
    """Validate provider numeric metadata without accepting coercible values."""

    error = f"{field} must be an exact finite positive number"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InstrumentIdentityError(error)
    try:
        number = float(value)
    except OverflowError as exc:
        raise InstrumentIdentityError(error) from exc
    if not isfinite(number) or number <= 0:
        raise InstrumentIdentityError(error)
    return number


def require_exact_instrument_id_sequence(
    values: object,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Validate an explicit instrument-ID collection without parsing or coercion."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("instrument_ids must be a typed sequence")
    instrument_ids = tuple(values)
    if any(not isinstance(value, str) or not value for value in instrument_ids):
        raise InstrumentIdentityError("instrument_ids must contain exact non-empty strings")
    if len(set(instrument_ids)) != len(instrument_ids):
        raise InstrumentIdentityError("instrument_ids cannot contain duplicates")
    if not allow_empty and not instrument_ids:
        raise InstrumentIdentityError("instrument_ids are required")
    return instrument_ids


def require_exact_identity_text(
    value: object,
    *,
    field: str,
    allow_empty: bool = False,
) -> str:
    """Validate one opaque identity token without parsing or coercion."""

    if not isinstance(value, str):
        raise InstrumentIdentityError(f"{field} must be an exact string")
    if not value and not allow_empty:
        raise InstrumentIdentityError(f"{field} is required")
    return value


def identity_payload(instrument: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(instrument, dict):
        return {}
    identity = instrument.get("contract_identity")
    return identity if isinstance(identity, dict) else {}


def instrument_provider(instrument: dict[str, Any] | None) -> str:
    value = (instrument or {}).get("provider")
    return value if isinstance(value, str) else ""


def instrument_asset_class(instrument: dict[str, Any] | None) -> str:
    value = (instrument or {}).get("asset_class")
    return value if isinstance(value, str) else ""


def asset_class_is_futures(asset_class: str | None) -> bool:
    return isinstance(asset_class, str) and asset_class in FUTURES_ASSET_CLASSES


def instrument_is_futures(instrument: dict[str, Any] | None) -> bool:
    return asset_class_is_futures(instrument_asset_class(instrument))


def instrument_identity_scope(instrument: dict[str, Any] | None) -> str:
    identity = identity_payload(instrument)
    raw_scope = identity.get("identity_scope")
    if isinstance(raw_scope, str) and raw_scope:
        return raw_scope
    if instrument and not instrument_is_futures(instrument):
        return INSTRUMENT_IDENTITY_SCOPE_CONTRACT
    return ""


def instrument_is_futures_root(instrument: dict[str, Any] | None) -> bool:
    return instrument_is_futures(instrument) and (
        instrument_identity_scope(instrument) == INSTRUMENT_IDENTITY_SCOPE_ROOT
    )


def instrument_is_exact_futures_contract(instrument: dict[str, Any] | None) -> bool:
    return instrument_is_futures(instrument) and (
        instrument_identity_scope(instrument) == INSTRUMENT_IDENTITY_SCOPE_CONTRACT
    )


def instrument_key(instrument: dict[str, Any] | None) -> str:
    value = (instrument or {}).get("instrument_key")
    return value if isinstance(value, str) else ""


def futures_root(instrument: dict[str, Any] | None) -> str:
    if not instrument_is_futures_root(instrument):
        return ""
    identity = identity_payload(instrument)
    root_value = identity.get("root")
    root = root_value if isinstance(root_value, str) else ""
    return root


def provider_contract_id(instrument: dict[str, Any] | None) -> str:
    value = (instrument or {}).get("provider_contract_id")
    return value if isinstance(value, str) else ""


def current_futures_contract(instrument: dict[str, Any] | None) -> dict[str, Any]:
    identity = identity_payload(instrument)
    current = identity.get("current_contract")
    return current if isinstance(current, dict) else {}


def current_futures_contract_id(instrument: dict[str, Any] | None) -> str:
    current = current_futures_contract(instrument)
    value = current.get("provider_contract_id")
    return value if isinstance(value, str) else ""


def provider_price_increment(instrument: dict[str, Any] | None) -> float | None:
    """Project the selected route's exact provider-owned price increment."""

    identity = identity_payload(instrument)
    if instrument_is_futures_root(instrument):
        owner = current_futures_contract(instrument)
        field = "current_contract.min_tick"
    else:
        owner = identity
        field = "contract_identity.min_tick"
    raw_value = owner.get("min_tick")
    if raw_value is None:
        return None
    try:
        return require_exact_positive_number(raw_value, field=field)
    except InstrumentIdentityError as exc:
        raise ProviderPriceIncrementError(str(exc)) from exc


def _require_matching_field(
    identity: dict[str, Any],
    field: str,
    expected: str,
    *,
    instrument_key_value: str,
) -> None:
    raw_actual = identity.get(field)
    actual = raw_actual if isinstance(raw_actual, str) else ""
    if not actual:
        raise InstrumentIdentityError(
            f"CONTRACT_IDENTITY_FIELD_REQUIRED field={field} instrument_key={instrument_key_value}"
        )
    if actual != expected:
        raise InstrumentIdentityError(
            f"CONTRACT_IDENTITY_MISMATCH field={field} expected={expected} actual={actual} "
            f"instrument_key={instrument_key_value}"
        )


def require_provider_identity(
    instrument: dict[str, Any] | None,
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    if not isinstance(instrument, dict):
        raise InstrumentIdentityError(
            f"INSTRUMENT_IDENTITY_REQUIRED provider={provider or 'unknown'}"
        )
    resolved_provider = instrument_provider(instrument)
    key = instrument_key(instrument)
    asset_class = instrument_asset_class(instrument)
    raw_instrument_id = instrument.get("instrument_id")
    explicit_instrument_id = raw_instrument_id if isinstance(raw_instrument_id, str) else ""
    if not resolved_provider or not key or not asset_class or not explicit_instrument_id:
        raise InstrumentIdentityError(
            f"INSTRUMENT_IDENTITY_INCOMPLETE provider={resolved_provider or provider or 'unknown'}"
        )
    if ":" in resolved_provider:
        raise InstrumentIdentityError(
            f"INSTRUMENT_PROVIDER_NOT_CANONICAL actual={resolved_provider} instrument_key={key}"
        )
    if (
        not resolved_provider.isascii()
        or not resolved_provider[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in resolved_provider
        )
    ):
        raise InstrumentIdentityError(
            f"INSTRUMENT_PROVIDER_NOT_CANONICAL actual={resolved_provider} instrument_key={key}"
        )
    if (
        not asset_class.isascii()
        or not asset_class[0].isalnum()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in asset_class
        )
    ):
        raise InstrumentIdentityError(
            f"INSTRUMENT_ASSET_CLASS_NOT_CANONICAL actual={asset_class} instrument_key={key}"
        )
    identity = identity_payload(instrument)
    if not identity:
        raise InstrumentIdentityError(
            f"CONTRACT_IDENTITY_REQUIRED provider={resolved_provider} instrument_key={key}"
        )
    _require_matching_field(identity, "provider", resolved_provider, instrument_key_value=key)
    _require_matching_field(identity, "asset_class", asset_class, instrument_key_value=key)
    raw_route_symbol = instrument.get("provider_symbol")
    route_symbol = raw_route_symbol if isinstance(raw_route_symbol, str) else ""
    if not route_symbol:
        raise InstrumentIdentityError(
            f"PROVIDER_SYMBOL_REQUIRED provider={resolved_provider} instrument_key={key}"
        )
    expected_provider = ""
    if provider is not None:
        expected_provider = require_exact_identity_text(
            provider,
            field="expected_provider",
        )
        if (
            ":" in expected_provider
            or not expected_provider.isascii()
            or not expected_provider[0].isalnum()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in expected_provider
            )
        ):
            raise InstrumentIdentityError(
                f"EXPECTED_PROVIDER_NOT_CANONICAL expected={expected_provider} instrument_key={key}"
            )
    if expected_provider and resolved_provider != expected_provider:
        raise InstrumentIdentityError(
            f"INSTRUMENT_PROVIDER_MISMATCH expected={expected_provider} actual={resolved_provider} instrument_key={key}"
        )
    scope = instrument_identity_scope(instrument)
    if scope not in {
        INSTRUMENT_IDENTITY_SCOPE_CONTRACT,
        INSTRUMENT_IDENTITY_SCOPE_ROOT,
    }:
        raise InstrumentIdentityError(
            f"INSTRUMENT_IDENTITY_SCOPE_REQUIRED provider={resolved_provider} instrument_key={key}"
        )
    if scope == INSTRUMENT_IDENTITY_SCOPE_ROOT:
        if not instrument_is_futures(instrument):
            raise InstrumentIdentityError(
                f"INSTRUMENT_ROOT_SCOPE_REQUIRES_FUTURES instrument_key={key}"
            )
        if provider_contract_id(instrument):
            raise InstrumentIdentityError(
                f"FUTURES_ROOT_PROVIDER_CONTRACT_ID_FORBIDDEN instrument_key={key}"
            )
        root = futures_root(instrument)
        if not root:
            raise InstrumentIdentityError(f"FUTURES_ROOT_IDENTITY_REQUIRED instrument_key={key}")
        raw_exchange = identity.get("exchange")
        raw_currency = identity.get("currency")
        if (
            not isinstance(raw_exchange, str)
            or not raw_exchange
            or not isinstance(raw_currency, str)
            or not raw_currency
        ):
            raise InstrumentIdentityError(
                f"FUTURES_PROVIDER_METADATA_REQUIRED provider={resolved_provider} instrument_key={key}"
            )
        current = current_futures_contract(instrument)
        if current and not current_futures_contract_id(instrument):
            raise InstrumentIdentityError(
                f"FUTURES_CURRENT_PROVIDER_CONTRACT_ID_REQUIRED provider={resolved_provider} instrument_key={key}"
            )
    else:
        if instrument_is_futures(instrument):
            raw_contract_root = identity.get("root")
            if isinstance(raw_contract_root, str) and raw_contract_root:
                raise InstrumentIdentityError(
                    f"FUTURES_CONTRACT_ROOT_FORBIDDEN instrument_key={key}"
                )
            if current_futures_contract(instrument):
                raise InstrumentIdentityError(
                    f"FUTURES_CONTRACT_CURRENT_CONTRACT_FORBIDDEN instrument_key={key}"
                )
            continuous_series = instrument.get("continuous_series")
            if isinstance(continuous_series, dict) and continuous_series:
                raise InstrumentIdentityError(
                    f"FUTURES_CONTRACT_CONTINUOUS_SERIES_FORBIDDEN instrument_key={key}"
                )
        contract_id = provider_contract_id(instrument)
        if not contract_id:
            raise InstrumentIdentityError(
                f"PROVIDER_CONTRACT_ID_REQUIRED provider={resolved_provider} instrument_key={key}"
            )
        raw_nested_contract_id = identity.get("provider_contract_id")
        nested_contract_id = (
            raw_nested_contract_id if isinstance(raw_nested_contract_id, str) else ""
        )
        if nested_contract_id != contract_id:
            raise InstrumentIdentityError(
                f"PROVIDER_CONTRACT_ID_MISMATCH provider={resolved_provider} instrument_key={key}"
            )
        raw_product_id = identity.get("product_id")
        product_id = raw_product_id if isinstance(raw_product_id, str) else ""
        if product_id and product_id != contract_id:
            raise InstrumentIdentityError(
                f"PROVIDER_PRODUCT_ID_MISMATCH provider={resolved_provider} instrument_key={key}"
            )
    if scope == INSTRUMENT_IDENTITY_SCOPE_ROOT:
        expected_instrument_id = "|".join(
            (
                resolved_provider,
                "future_root",
                futures_root(instrument),
                raw_exchange,
                raw_currency,
                identity.get("trading_class")
                if isinstance(identity.get("trading_class"), str)
                else "",
            )
        )
    else:
        expected_instrument_id = "|".join(
            (resolved_provider, "contract", provider_contract_id(instrument))
        )
    if explicit_instrument_id != expected_instrument_id:
        raise InstrumentIdentityError(
            f"INSTRUMENT_ID_MISMATCH expected={expected_instrument_id} actual={explicit_instrument_id}"
        )
    return instrument


def provider_symbol(instrument: dict[str, Any], provider: str) -> str:
    qualified = require_provider_identity(instrument, provider=provider)
    route_value = qualified.get("provider_symbol")
    route = route_value if isinstance(route_value, str) else ""
    if not route:
        raise InstrumentIdentityError(
            f"PROVIDER_SYMBOL_REQUIRED provider={instrument_provider(qualified)} instrument_key={instrument_key(qualified)}"
        )
    return route


def qualified_instrument_id(instrument: dict[str, Any] | None) -> str:
    qualified = require_provider_identity(instrument)
    instrument_id = qualified.get("instrument_id")
    return instrument_id if isinstance(instrument_id, str) else ""


def route_fingerprint(instrument: dict[str, Any] | None) -> str:
    binding = qualified_instrument_id(instrument)
    if instrument_is_futures_root(instrument):
        return f"{binding}|current:{current_futures_contract_id(instrument)}"
    return binding
