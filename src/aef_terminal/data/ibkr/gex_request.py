"""IBKR-owned transport contract for one prepared GEX request."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from aef_terminal.data.gex.config import GexRequestConfig


@dataclass(frozen=True)
class IbkrGexTransportConfig:
    host: str
    port: int
    client_id: int
    readonly: bool
    market_data_type: int
    wait_seconds: float
    timeout: float
    batch_size: int
    batch_pause_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host or self.host != self.host.strip():
            raise ValueError("IBKR GEX host must be an exact non-empty string")
        for field_name, lower, upper in (
            ("port", 1, 65_535),
            ("client_id", 0, 2_147_483_647),
            ("market_data_type", 1, 4),
            ("batch_size", 1, 8),
        ):
            value = getattr(self, field_name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"IBKR GEX {field_name} is outside its exact contract")
        if type(self.readonly) is not bool:
            raise TypeError("IBKR GEX readonly must be a boolean")
        for field_name, lower, upper in (
            ("wait_seconds", 0.5, 8.0),
            ("timeout", 5.0, 90.0),
            ("batch_pause_seconds", 0.25, 3.0),
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not lower <= float(value) <= upper
            ):
                raise ValueError(f"IBKR GEX {field_name} is outside its finite contract")


@dataclass(frozen=True)
class IbkrGexRequest:
    policy: GexRequestConfig
    transport: IbkrGexTransportConfig

    @property
    def instrument_id(self) -> str:
        return self.policy.instrument_id

    @property
    def route_fingerprint(self) -> str:
        return self.policy.route_fingerprint

    @property
    def futures_options(self) -> bool:
        return self.policy.futures_options

    @property
    def strike_count(self) -> int:
        return self.policy.strike_count

    @property
    def max_expirations(self) -> int:
        return self.policy.max_expirations

    @property
    def max_contracts(self) -> int:
        return self.policy.max_contracts

    @property
    def expiry_mode(self) -> str:
        return self.policy.expiry_mode

    @property
    def risk_free_rate(self) -> float:
        return self.policy.risk_free_rate

    @property
    def dividend_yield(self) -> float:
        return self.policy.dividend_yield

    @property
    def option_expiry_facts(self) -> tuple[object, ...]:
        return self.policy.option_expiry_facts

    @property
    def refresh_mode(self) -> str:
        return self.policy.refresh_mode

    @property
    def scheduler_lane(self) -> str:
        return self.policy.scheduler_lane

    @property
    def host(self) -> str:
        return self.transport.host

    @property
    def port(self) -> int:
        return self.transport.port

    @property
    def client_id(self) -> int:
        return self.transport.client_id

    @property
    def readonly(self) -> bool:
        return self.transport.readonly

    @property
    def market_data_type(self) -> int:
        return self.transport.market_data_type

    @property
    def wait_seconds(self) -> float:
        return self.transport.wait_seconds

    @property
    def timeout(self) -> float:
        return self.transport.timeout

    @property
    def batch_size(self) -> int:
        return self.transport.batch_size

    @property
    def batch_pause_seconds(self) -> float:
        return self.transport.batch_pause_seconds
