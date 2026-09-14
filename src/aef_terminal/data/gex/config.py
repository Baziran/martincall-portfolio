from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite

from aef_terminal.data.gex.constants import (
    GEX_REQUEST_MAX_CONTRACTS,
    GEX_REQUEST_STRIKE_COUNT,
)
from aef_terminal.data.gex.contracts import (
    require_gex_valuation_rates,
)
from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
)
from aef_terminal.data.market_data import MarketDataEntitlement
from aef_terminal.data.provider_contract import require_option_expiry_mode


@dataclass(frozen=True)
class GexProviderRequestMetadata:
    provider_key: str
    generation: str
    request_session: str
    requested_entitlement: MarketDataEntitlement
    timeout_seconds: float
    observation_wait_seconds: float
    batch_size: int
    batch_pause_seconds: float

    def __post_init__(self) -> None:
        require_exact_identity_text(self.provider_key, field="GEX_PROVIDER_KEY")
        require_exact_identity_text(self.generation, field="GEX_PROVIDER_GENERATION")
        require_exact_identity_text(self.request_session, field="GEX_PROVIDER_REQUEST_SESSION")
        if self.requested_entitlement not in {
            "live",
            "frozen",
            "delayed",
            "delayed_frozen",
            "unknown",
        }:
            raise ValueError("GEX requested entitlement is invalid")
        for field_name, lower, upper in (
            ("timeout_seconds", 5.0, 90.0),
            ("observation_wait_seconds", 0.5, 8.0),
            ("batch_pause_seconds", 0.25, 3.0),
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not lower <= float(value) <= upper
            ):
                raise ValueError(f"GEX provider metadata {field_name} is invalid")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 8:
            raise ValueError("GEX provider metadata batch_size is invalid")


@dataclass(frozen=True)
class GexRequestConfig:
    instrument_id: str
    route_fingerprint: str
    futures_options: bool
    provider_metadata: GexProviderRequestMetadata
    provider_request: object = field(repr=False, compare=False)
    strike_count: int = GEX_REQUEST_STRIKE_COUNT
    max_expirations: int = 2
    max_contracts: int = GEX_REQUEST_MAX_CONTRACTS
    expiry_mode: str = "hybrid"
    risk_free_rate: float = 0.052
    dividend_yield: float = 0.0
    refresh_mode: str = "auto"
    scheduler_lane: str = "auto"
    option_expiry_facts: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        require_exact_identity_text(self.instrument_id, field="GEX_INSTRUMENT_ID")
        require_exact_identity_text(
            self.route_fingerprint,
            field="GEX_ROUTE_FINGERPRINT",
        )
        if type(self.futures_options) is not bool:
            raise TypeError("GEX futures_options must be a boolean")
        if not isinstance(self.provider_metadata, GexProviderRequestMetadata):
            raise TypeError("GEX provider_metadata must be typed")
        if self.provider_request is None:
            raise TypeError("GEX provider_request is required")
        for field_name, lower, upper in (
            ("strike_count", 1, None),
            ("max_expirations", 1, None),
            ("max_contracts", 2, None),
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < lower or (upper is not None and value > upper):
                raise ValueError(f"GEX {field_name} is outside its exact integer contract")
        if self.max_contracts % 2 != 0:
            raise ValueError("GEX max_contracts must preserve complete call/put pairs")
        if self.max_contracts < self.strike_count * self.max_expirations * 2:
            raise ValueError(
                "GEX max_contracts must cover the worst-case global strike union "
                "across selected expiries"
            )
        require_gex_valuation_rates(
            {
                "risk_free_rate": self.risk_free_rate,
                "dividend_yield": self.dividend_yield,
            }
        )
        require_option_expiry_mode(self.expiry_mode)
        if self.refresh_mode not in {"auto", "manual", "live"}:
            raise ValueError("GEX refresh_mode must be exact")
        if self.scheduler_lane not in {"auto", "manual", "live"}:
            raise ValueError("GEX scheduler_lane must be exact")
        if not isinstance(self.option_expiry_facts, tuple):
            raise TypeError("GEX option_expiry_facts must be a typed tuple")


@dataclass(frozen=True)
class GexSchedulerJob:
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    interval_minutes: int
    offset_minutes: int
    strike_count: int
    max_contracts: int
    max_expirations: int
    enabled: bool
    reason: str
    next_due_at: datetime
    lane: str = "auto"
    refresh_mode: str = "auto"
