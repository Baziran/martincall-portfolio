"""IBKR implementation of the typed common GEX provider boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from hashlib import sha1
import json
from typing import Any, TypeVar

from aef_terminal.config import AppConfig
from aef_terminal.data.gex.config import (
    GexProviderRequestMetadata,
    GexRequestConfig,
)
from aef_terminal.data.gex.constants import (
    GEX_REQUEST_MAX_CONTRACTS,
    GEX_REQUEST_STRIKE_COUNT,
)
from aef_terminal.data.gex.dividend import _configured_dividend_yield
from aef_terminal.data.gex.provider_runtime import (
    GexAcquisition,
    GexProviderRuntimeStatus,
    LiveGexHealth,
    LiveGexSample,
    LiveGexSubscription,
)
import aef_terminal.data.ibkr.gex as ibkr_gex
from aef_terminal.data.ibkr.gex_request import (
    IbkrGexRequest,
    IbkrGexTransportConfig,
)
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.data.ibkr.market_data import ibkr_market_data_entitlement
from aef_terminal.data.ibkr.option_contracts import (
    read_persisted_ibkr_option_expiry_facts,
    require_ibkr_option_expiry_facts,
)
from aef_terminal.data.instrument_identity import (
    instrument_is_futures,
    qualified_instrument_id,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import require_option_expiry_mode


T = TypeVar("T")


class IbkrGexProviderRuntime:
    provider_key = "ibkr"

    def request_config(
        self,
        *,
        instrument: dict[str, Any],
        store: Any | None = None,
        mode: str,
    ) -> GexRequestConfig:
        if mode not in {"auto", "manual", "live"}:
            raise ValueError("GEX_REQUEST_MODE_INVALID")
        qualified = require_provider_identity(instrument, provider="ibkr")
        config = AppConfig()
        instrument_id = qualified_instrument_id(qualified)
        route_key = route_fingerprint(qualified)
        strike_count = GEX_REQUEST_STRIKE_COUNT
        client_id = config.ibkr_gex_client_id
        if mode == "live":
            client_id = int(config.ibkr_gex_live_client_id) + int(
                sha1(route_key.encode("utf-8")).hexdigest()[:7],
                16,
            )
        transport = IbkrGexTransportConfig(
            host=config.ibkr_gex_host,
            port=config.ibkr_gex_port,
            client_id=client_id,
            readonly=config.ibkr_readonly,
            market_data_type=config.ibkr_gex_market_data_type,
            wait_seconds=config.ibkr_gex_wait_seconds,
            timeout=config.ibkr_gex_timeout_seconds,
            batch_size=config.ibkr_gex_batch_size,
            batch_pause_seconds=config.ibkr_gex_batch_pause_seconds,
        )
        generation = sha1(
            json.dumps(
                {
                    "host": transport.host,
                    "port": transport.port,
                    "client_id": transport.client_id,
                    "readonly": transport.readonly,
                    "market_data_type": transport.market_data_type,
                    "wait_seconds": transport.wait_seconds,
                    "timeout": transport.timeout,
                    "batch_size": transport.batch_size,
                    "batch_pause_seconds": transport.batch_pause_seconds,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        expiry_facts = require_ibkr_option_expiry_facts(
            read_persisted_ibkr_option_expiry_facts(
                instrument_id=instrument_id,
                route_fingerprint=route_key,
                store=store,
            )
        )
        return GexRequestConfig(
            instrument_id=instrument_id,
            route_fingerprint=route_key,
            futures_options=instrument_is_futures(qualified),
            provider_metadata=GexProviderRequestMetadata(
                provider_key=self.provider_key,
                generation=generation,
                request_session=str(client_id),
                requested_entitlement=ibkr_market_data_entitlement(transport.market_data_type),
                timeout_seconds=transport.timeout,
                observation_wait_seconds=transport.wait_seconds,
                batch_size=transport.batch_size,
                batch_pause_seconds=transport.batch_pause_seconds,
            ),
            provider_request=transport,
            strike_count=strike_count,
            max_contracts=(strike_count * 2 * 2 if mode == "live" else GEX_REQUEST_MAX_CONTRACTS),
            expiry_mode=require_option_expiry_mode(config.ibkr_gex_expiry_mode),
            risk_free_rate=config.ibkr_gex_risk_free_rate,
            dividend_yield=_configured_dividend_yield(
                instrument_id,
                route_key,
                futures_options=instrument_is_futures(qualified),
                risk_free_rate=config.ibkr_gex_risk_free_rate,
                default_dividend_yield=config.ibkr_gex_dividend_yield,
                configured_override=(
                    str(config.ibkr_gex_dividend_yield)
                    if config.ibkr_gex_dividend_yield_override_active
                    else None
                ),
                store=store,
            ),
            option_expiry_facts=expiry_facts,
            refresh_mode=mode,
            scheduler_lane=mode,
        )

    async def run_coroutine(
        self,
        lane: str,
        label: str,
        factory: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
    ) -> T:
        return await ibkr_market_data_manager.run_coroutine(
            lane,
            label,
            factory,
            timeout=timeout,
        )

    async def run_sync(
        self,
        lane: str,
        label: str,
        callback: Callable[[], T],
        *,
        timeout: float | None = None,
    ) -> T:
        return await ibkr_market_data_manager.run_sync(
            lane,
            label,
            callback,
            timeout=timeout,
        )

    @staticmethod
    def _ibkr_request(request: GexRequestConfig) -> IbkrGexRequest:
        if request.provider_metadata.provider_key != "ibkr" or not isinstance(
            request.provider_request,
            IbkrGexTransportConfig,
        ):
            raise TypeError("IBKR_GEX_PROVIDER_REQUEST_REQUIRED")
        return IbkrGexRequest(policy=request, transport=request.provider_request)

    def acquire(
        self,
        *,
        instrument: dict[str, Any],
        request: GexRequestConfig,
        spot: float,
        dividend_yield: float,
    ) -> GexAcquisition:
        return ibkr_gex.fetch_gex_option_rows(
            instrument=instrument,
            request=self._ibkr_request(request),
            spot=spot,
            dividend_yield=dividend_yield,
        )

    async def open_live(
        self,
        *,
        provider_symbol: str,
        instrument: dict[str, Any],
        request: GexRequestConfig,
        spot: float,
    ) -> LiveGexSubscription:
        return await ibkr_gex.open_live_gex_subscription(
            provider_symbol=provider_symbol,
            instrument=instrument,
            request=self._ibkr_request(request),
            spot=spot,
        )

    def close_live(self, subscription: LiveGexSubscription) -> int:
        return ibkr_gex.close_live_gex_subscription(subscription)

    def sample_live(
        self,
        subscription: LiveGexSubscription,
        *,
        spot: float,
        risk_free_rate: float,
        dividend_yield: float,
    ) -> LiveGexSample:
        return ibkr_gex.sample_live_gex_subscription(
            subscription,
            spot=spot,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )

    def live_health(
        self,
        subscription: LiveGexSubscription,
        *,
        now: datetime | None = None,
    ) -> LiveGexHealth:
        return ibkr_gex.live_gex_subscription_health(subscription, now=now)

    def selected_strikes_require_rebase(
        self,
        strikes: Sequence[float],
        spot: float,
    ) -> bool:
        return ibkr_gex.gex_selected_strike_universe_requires_rebase(strikes, spot)

    def runtime_status(self) -> GexProviderRuntimeStatus:
        config = AppConfig()
        manager_lane = ibkr_market_data_manager.status().get("lanes", {}).get("gex", {})
        request_transport = ibkr_gex.gex_request_transport_status()
        return GexProviderRuntimeStatus(
            provider_key=self.provider_key,
            request_connected=request_transport.connected,
            endpoint=(
                f"{request_transport.host or config.ibkr_gex_host}:"
                f"{request_transport.port or config.ibkr_gex_port}"
            ),
            request_session=str(request_transport.client_id or config.ibkr_gex_client_id),
            requested_entitlement=ibkr_market_data_entitlement(config.ibkr_gex_market_data_type),
            batch_size=config.ibkr_gex_batch_size,
            expiry_mode=config.ibkr_gex_expiry_mode,
            risk_free_rate=config.ibkr_gex_risk_free_rate,
            dividend_yield=config.ibkr_gex_dividend_yield,
            queue_pending=int(manager_lane.get("pending") or 0),
            queue_running=bool(manager_lane.get("running")),
            queue_running_label=str(manager_lane.get("running_label") or ""),
        )

    def dividend_yield_override_active(self) -> bool:
        return AppConfig().ibkr_gex_dividend_yield_override_active


ibkr_gex_provider_runtime = IbkrGexProviderRuntime()
