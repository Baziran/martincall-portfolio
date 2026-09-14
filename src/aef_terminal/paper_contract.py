from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import math
from typing import Any, Literal, TypedDict, cast

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.runtime.math_utils import float_or_none


PAPER_PROTECTION_BASIS_KEY = "protection_basis"
PAPER_PROTECTION_INTENT_KEY = "paper_protection_intent"
PAPER_EXECUTION_CONFIG_KEY = "paper_execution_config"
PAPER_CONTRACT_KEY = "paper_contract"
PAPER_CONTRACT_VERSION = 1

PaperContractScopeKind = Literal["instrument", "option"]


class PaperJournalError(ValueError):
    """Typed paper-journal rejection used across storage and execution owners."""

    def __init__(self, code: str, message: str | None = None) -> None:
        exact_code = str(code or "").strip()
        if not exact_code:
            raise ValueError("PAPER_JOURNAL_ERROR_CODE_REQUIRED")
        self.code = exact_code
        super().__init__(message or exact_code)


def _paper_contract_scope_key(parts: tuple[object, ...]) -> str:
    encoded = tuple(str(part) for part in parts)
    return "paper-contract-v1|" + "|".join(f"{len(value)}:{value}" for value in encoded)


def _exact_utc_iso(value: object, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field}_INVALID") from exc
    else:
        raise ValueError(f"{field}_REQUIRED")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}_UTC_REQUIRED")
    return parsed.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class PaperContractIdentity:
    """Physical paper-execution identity, independent of display/watchlist identity."""

    scope_kind: PaperContractScopeKind
    provider: str
    provider_contract_id: str
    sec_type: str | None = None
    con_id: int | None = None
    exchange: str | None = None
    expiry: str | None = None
    expiry_at: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    trading_class: str | None = None
    multiplier: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        provider = require_exact_identity_text(
            self.provider,
            field="PAPER_CONTRACT_PROVIDER",
        )
        if provider != provider.lower():
            raise ValueError("PAPER_CONTRACT_PROVIDER_NOT_CANONICAL")
        provider_contract_id = require_exact_identity_text(
            self.provider_contract_id,
            field="PAPER_CONTRACT_PROVIDER_CONTRACT_ID",
        )
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "provider_contract_id", provider_contract_id)
        if self.scope_kind == "instrument":
            if any(
                value is not None
                for value in (
                    self.sec_type,
                    self.con_id,
                    self.exchange,
                    self.expiry,
                    self.expiry_at,
                    self.strike,
                    self.right,
                    self.trading_class,
                    self.multiplier,
                    self.currency,
                )
            ):
                raise ValueError("PAPER_INSTRUMENT_CONTRACT_OPTION_FACTS_FORBIDDEN")
            return
        if self.scope_kind != "option":
            raise ValueError("PAPER_CONTRACT_SCOPE_KIND_INVALID")
        if self.sec_type not in {"OPT", "FOP"}:
            raise ValueError("PAPER_OPTION_SEC_TYPE_INVALID")
        if isinstance(self.con_id, bool) or not isinstance(self.con_id, int) or self.con_id <= 0:
            raise ValueError("PAPER_OPTION_CON_ID_INVALID")
        if self.provider == "ibkr" and provider_contract_id != str(self.con_id):
            raise ValueError("PAPER_OPTION_PROVIDER_CONTRACT_ID_MISMATCH")
        for field_name in ("exchange", "expiry", "trading_class", "currency"):
            object.__setattr__(
                self,
                field_name,
                require_exact_identity_text(
                    getattr(self, field_name),
                    field=f"PAPER_OPTION_{field_name.upper()}",
                ),
            )
        if len(cast(str, self.expiry)) != 8 or not cast(str, self.expiry).isdigit():
            raise ValueError("PAPER_OPTION_EXPIRY_INVALID")
        try:
            datetime.strptime(cast(str, self.expiry), "%Y%m%d")
        except ValueError as exc:
            raise ValueError("PAPER_OPTION_EXPIRY_INVALID") from exc
        object.__setattr__(
            self,
            "expiry_at",
            _exact_utc_iso(self.expiry_at, field="PAPER_OPTION_EXPIRY_AT"),
        )
        if self.right not in {"C", "P"}:
            raise ValueError("PAPER_OPTION_RIGHT_INVALID")
        for field_name in ("strike", "multiplier"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"PAPER_OPTION_{field_name.upper()}_INVALID")
            object.__setattr__(self, field_name, float(value))

    @property
    def scope_key(self) -> str:
        if self.scope_kind == "instrument":
            parts: tuple[object, ...] = (
                self.scope_kind,
                self.provider,
                self.provider_contract_id,
            )
        else:
            parts = (
                self.scope_kind,
                self.provider,
                self.sec_type,
                self.provider_contract_id,
                self.con_id,
                self.exchange,
            )
        return _paper_contract_scope_key(parts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": PAPER_CONTRACT_VERSION,
            "scope_kind": self.scope_kind,
            "scope_key": self.scope_key,
            "provider": self.provider,
            "provider_contract_id": self.provider_contract_id,
            "sec_type": self.sec_type,
            "con_id": self.con_id,
            "exchange": self.exchange,
            "expiry": self.expiry,
            "expiry_at": self.expiry_at,
            "strike": self.strike,
            "right": self.right,
            "trading_class": self.trading_class,
            "multiplier": self.multiplier,
            "currency": self.currency,
        }


_PAPER_OPTION_FIELDS = frozenset(
    {
        "sec_type",
        "con_id",
        "exchange",
        "expiry",
        "expiry_at",
        "strike",
        "right",
        "trading_class",
        "multiplier",
        "currency",
    }
)


def paper_instrument_contract(
    provider: object,
    provider_contract_id: object,
) -> PaperContractIdentity:
    """Build the explicit physical scope for one ordinary instrument."""

    return PaperContractIdentity(
        scope_kind="instrument",
        provider=require_exact_identity_text(
            provider,
            field="PAPER_CONTRACT_PROVIDER",
        ),
        provider_contract_id=require_exact_identity_text(
            provider_contract_id,
            field="PAPER_CONTRACT_PROVIDER_CONTRACT_ID",
        ),
    )


def require_paper_contract_identity(payload: Mapping[str, Any]) -> PaperContractIdentity:
    raw_contract = payload.get(PAPER_CONTRACT_KEY)
    provider = require_exact_identity_text(
        payload.get("provider"),
        field="PAPER_CONTRACT_PROVIDER",
    )
    provider_contract_id = require_exact_identity_text(
        payload.get("provider_contract_id"),
        field="PAPER_CONTRACT_PROVIDER_CONTRACT_ID",
    )
    if raw_contract is None:
        raise ValueError("PAPER_CONTRACT_PAYLOAD_REQUIRED")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("PAPER_CONTRACT_PAYLOAD_INVALID")
    expected_keys = {
        "version",
        "scope_kind",
        "scope_key",
        "provider",
        "provider_contract_id",
        *_PAPER_OPTION_FIELDS,
    }
    if set(raw_contract) != expected_keys:
        raise ValueError("PAPER_CONTRACT_PAYLOAD_SHAPE_INVALID")
    if raw_contract.get("version") != PAPER_CONTRACT_VERSION:
        raise ValueError("PAPER_CONTRACT_VERSION_INVALID")
    contract = PaperContractIdentity(
        scope_kind=cast(PaperContractScopeKind, raw_contract.get("scope_kind")),
        provider=cast(str, raw_contract.get("provider")),
        provider_contract_id=cast(str, raw_contract.get("provider_contract_id")),
        sec_type=cast(str | None, raw_contract.get("sec_type")),
        con_id=cast(int | None, raw_contract.get("con_id")),
        exchange=cast(str | None, raw_contract.get("exchange")),
        expiry=cast(str | None, raw_contract.get("expiry")),
        expiry_at=cast(str | None, raw_contract.get("expiry_at")),
        strike=cast(float | None, raw_contract.get("strike")),
        right=cast(Literal["C", "P"] | None, raw_contract.get("right")),
        trading_class=cast(str | None, raw_contract.get("trading_class")),
        multiplier=cast(float | None, raw_contract.get("multiplier")),
        currency=cast(str | None, raw_contract.get("currency")),
    )
    if (
        contract.provider != provider
        or contract.provider_contract_id != provider_contract_id
        or raw_contract.get("scope_key") != contract.scope_key
        or dict(raw_contract) != contract.to_payload()
    ):
        raise ValueError("PAPER_CONTRACT_PAYLOAD_CONTRADICTORY")
    return contract


def require_paper_position_contract_identity(
    position: Mapping[str, Any],
) -> PaperContractIdentity:
    """Validate the physical contract carried by one canonical position projection."""

    raw_payload = position.get("payload")
    if not isinstance(raw_payload, Mapping):
        raise ValueError("PAPER_POSITION_PAYLOAD_REQUIRED")
    contract = require_paper_contract_identity(raw_payload)
    if position.get(PAPER_CONTRACT_KEY) != contract.to_payload():
        raise ValueError("PAPER_POSITION_CONTRACT_COPIES_CONTRADICT")
    return contract


PaperProtectionBasis = Literal["absolute_structure", "fill_distance"]


class PaperProtectionIntent(TypedDict):
    basis: Literal["fill_distance"]
    stop_points: float | None
    target_points: float | None


def paper_manual_protective_levels(
    payload: Mapping[str, Any],
) -> dict[str, float | None] | None:
    use_stop = payload.get("use_stop_loss", payload.get("stop_points") is not None)
    use_target = payload.get("use_target", payload.get("target_points") is not None)
    if not isinstance(use_stop, bool):
        raise ValueError("use_stop_loss must be a boolean")
    if not isinstance(use_target, bool):
        raise ValueError("use_target must be a boolean")
    if payload.get("stop_loss") is not None or payload.get("target") is not None:
        return None
    entry = float_or_none(payload.get("entry"))
    if entry is None:
        return None
    side = str(payload.get("side") or "").lower()
    if side not in {"long", "short"}:
        return None
    stop_points_raw = payload.get("stop_points")
    target_points_raw = payload.get("target_points")
    stop_points = float_or_none(stop_points_raw) if stop_points_raw is not None else None
    target_points = float_or_none(target_points_raw) if target_points_raw is not None else None
    if not use_stop and not use_target:
        return None
    stop = None
    target = None
    if use_stop and stop_points is not None and stop_points > 0:
        stop = entry - stop_points if side == "long" else entry + stop_points
    if use_target and target_points is not None and target_points > 0:
        target = entry + target_points if side == "long" else entry - target_points
    return {"stop": stop, "target": target}


def canonical_manual_paper_protection(
    order: Mapping[str, Any],
    nested_payload: Mapping[str, Any] | None,
) -> tuple[dict[str, float | None] | None, dict[str, Any]]:
    metadata = dict(nested_payload or {})
    if "absolute_level" in metadata:
        raise ValueError("PAPER_PROTECTION_LEGACY_ABSOLUTE_LEVEL_FORBIDDEN")
    has_basis = PAPER_PROTECTION_BASIS_KEY in metadata
    has_intent = PAPER_PROTECTION_INTENT_KEY in metadata
    has_point_fields = "stop_points" in order or "target_points" in order
    if has_point_fields and (has_basis or has_intent):
        raise ValueError("PAPER_PROTECTION_INPUT_CONTRADICTORY")

    levels: dict[str, float | None] | None = None
    if has_point_fields:
        if order.get("stop_loss") is not None or order.get("target") is not None:
            raise ValueError("PAPER_PROTECTION_POINT_AND_ABSOLUTE_LEVELS_CONTRADICT")
        levels = paper_manual_protective_levels(order)
        use_stop = order.get("use_stop_loss", order.get("stop_points") is not None)
        use_target = order.get("use_target", order.get("target_points") is not None)
        stop_points = float_or_none(order.get("stop_points"))
        target_points = float_or_none(order.get("target_points"))
        if not use_stop and not use_target:
            metadata[PAPER_PROTECTION_BASIS_KEY] = "absolute_structure"
            metadata.pop(PAPER_PROTECTION_INTENT_KEY, None)
            return None, metadata
        if (
            (use_stop and (stop_points is None or stop_points <= 0))
            or (use_target and (target_points is None or target_points <= 0))
            or levels is None
        ):
            raise ValueError("PAPER_FILL_DISTANCE_INTENT_INVALID")
        metadata[PAPER_PROTECTION_BASIS_KEY] = "fill_distance"
        metadata[PAPER_PROTECTION_INTENT_KEY] = {
            "basis": "fill_distance",
            "stop_points": stop_points if use_stop else None,
            "target_points": target_points if use_target else None,
        }
        return levels, metadata

    if not has_basis:
        if has_intent:
            raise ValueError("PAPER_PROTECTION_BASIS_REQUIRED")
        metadata[PAPER_PROTECTION_BASIS_KEY] = "absolute_structure"
    return levels, metadata


def require_paper_order_protection(order: Mapping[str, Any]) -> dict[str, Any]:
    payload = order.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("PAPER_ORDER_PROTECTION_PAYLOAD_REQUIRED")
    canonical_payload = dict(payload)
    if "absolute_level" in canonical_payload:
        raise ValueError("PAPER_PROTECTION_LEGACY_ABSOLUTE_LEVEL_FORBIDDEN")
    basis = canonical_payload.get(PAPER_PROTECTION_BASIS_KEY)
    if basis not in {"absolute_structure", "fill_distance"}:
        raise ValueError("PAPER_PROTECTION_BASIS_INVALID")

    use_stop_loss = order.get("use_stop_loss")
    use_target = order.get("use_target")
    if not isinstance(use_stop_loss, bool) or not isinstance(use_target, bool):
        raise ValueError("PAPER_PROTECTION_FLAGS_INVALID")
    stop_loss = float_or_none(order.get("stop_loss"))
    target = float_or_none(order.get("target"))
    if (
        (use_stop_loss and stop_loss is None)
        or (not use_stop_loss and order.get("stop_loss") is not None)
        or (use_target and target is None)
        or (not use_target and order.get("target") is not None)
    ):
        raise ValueError("PAPER_PROTECTION_LEVELS_INVALID")

    intent = canonical_payload.get(PAPER_PROTECTION_INTENT_KEY)
    if basis == "absolute_structure":
        if intent is not None:
            raise ValueError("PAPER_ABSOLUTE_PROTECTION_INTENT_FORBIDDEN")
        return canonical_payload

    if not isinstance(intent, Mapping) or set(intent) != {
        "basis",
        "stop_points",
        "target_points",
    }:
        raise ValueError("PAPER_FILL_DISTANCE_INTENT_INVALID")
    if intent.get("basis") != "fill_distance":
        raise ValueError("PAPER_FILL_DISTANCE_INTENT_INVALID")
    stop_points = float_or_none(intent.get("stop_points"))
    target_points = float_or_none(intent.get("target_points"))
    if (
        (not use_stop_loss and not use_target)
        or (use_stop_loss and (stop_points is None or stop_points <= 0))
        or (not use_stop_loss and intent.get("stop_points") is not None)
        or (use_target and (target_points is None or target_points <= 0))
        or (not use_target and intent.get("target_points") is not None)
    ):
        raise ValueError("PAPER_FILL_DISTANCE_INTENT_INVALID")
    canonical_payload[PAPER_PROTECTION_INTENT_KEY] = PaperProtectionIntent(
        basis="fill_distance",
        stop_points=stop_points if use_stop_loss else None,
        target_points=target_points if use_target else None,
    )
    canonical_payload[PAPER_PROTECTION_BASIS_KEY] = cast(
        PaperProtectionBasis,
        basis,
    )
    return canonical_payload
