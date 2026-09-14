"""Provider boundary consumed by the provider-independent GEX domain."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar

from aef_terminal.data.gex.config import GexRequestConfig
from aef_terminal.data.market_data import MarketDataEntitlement


T = TypeVar("T")


class GexAcquisition(Protocol):
    contract_rows: Sequence[Mapping[str, Any]]
    selected_contract_universe: Sequence[Mapping[str, Any]]
    contracts_requested: int
    chain_meta: Mapping[str, Any]
    underlying_audit: Mapping[str, Any]
    market_data_entitlement: MarketDataEntitlement
    captured_at: datetime
    universe_expires_at: datetime
    collection_timed_out: bool
    collection_timeout_phase: str


class LiveGexSubscription(Protocol):
    subscribed_strikes: Sequence[float]
    contracts_requested: int
    chain_meta: Mapping[str, Any]
    underlying_audit: Mapping[str, Any]

    @property
    def selected_contract_universe(self) -> Sequence[Mapping[str, Any]]: ...


class LiveGexSample(Protocol):
    contract_rows: Sequence[Mapping[str, Any]]
    market_data_entitlement: MarketDataEntitlement


class LiveGexHealth(Protocol):
    connected: bool
    contract_count: int
    subscription_count: int
    active_contract_count: int
    expired_contract_count: int
    unknown_expiry_contract_count: int
    earliest_expired_at: datetime | None
    universe_expires_at: datetime | None


@dataclass(frozen=True)
class GexProviderRuntimeStatus:
    provider_key: str
    request_connected: bool
    endpoint: str
    request_session: str
    requested_entitlement: MarketDataEntitlement
    batch_size: int
    expiry_mode: str
    risk_free_rate: float
    dividend_yield: float
    queue_pending: int
    queue_running: bool
    queue_running_label: str


class GexProviderRuntime(Protocol):
    provider_key: str

    def request_config(
        self,
        *,
        instrument: dict[str, Any],
        store: Any | None = None,
        mode: str,
    ) -> GexRequestConfig: ...

    async def run_coroutine(
        self,
        lane: str,
        label: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
    ) -> T: ...

    async def run_sync(
        self,
        lane: str,
        label: str,
        callback: Callable[[], T],
        *,
        timeout: float | None = None,
    ) -> T: ...

    def runtime_status(self) -> GexProviderRuntimeStatus: ...

    def acquire(
        self,
        *,
        instrument: dict[str, Any],
        request: GexRequestConfig,
        spot: float,
        dividend_yield: float,
    ) -> GexAcquisition: ...

    async def open_live(
        self,
        *,
        provider_symbol: str,
        instrument: dict[str, Any],
        request: GexRequestConfig,
        spot: float,
    ) -> LiveGexSubscription: ...

    def close_live(self, subscription: LiveGexSubscription) -> int: ...

    def sample_live(
        self,
        subscription: LiveGexSubscription,
        *,
        spot: float,
        risk_free_rate: float,
        dividend_yield: float,
    ) -> LiveGexSample: ...

    def live_health(
        self,
        subscription: LiveGexSubscription,
        *,
        now: datetime | None = None,
    ) -> LiveGexHealth: ...

    def selected_strikes_require_rebase(
        self,
        strikes: Sequence[float],
        spot: float,
    ) -> bool: ...

    def dividend_yield_override_active(self) -> bool: ...
