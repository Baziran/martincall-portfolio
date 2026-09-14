"""Atomic process-owned IBKR runtime connection settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from threading import Lock


def _environment_int(name: str, default: int, *, lower: int, upper: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}_INVALID") from exc
    if not lower <= value <= upper:
        raise ValueError(f"{name}_INVALID")
    return value


@dataclass(frozen=True)
class IbkrRuntimeSettingsSnapshot:
    port: int
    gex_port: int
    market_data_type: int
    gex_market_data_type: int
    generation: int
    source: str

    def payload(self) -> dict[str, int | str]:
        return {
            "ibkr_port": self.port,
            "ibkr_gex_port": self.gex_port,
            "ibkr_market_data_type": self.market_data_type,
            "ibkr_gex_market_data_type": self.gex_market_data_type,
            "generation": self.generation,
            "source": self.source,
        }


_LOCK = Lock()
_SNAPSHOT = IbkrRuntimeSettingsSnapshot(
    port=_environment_int("AEF_IBKR_PORT", 7497, lower=1, upper=65_535),
    gex_port=_environment_int(
        "AEF_IBKR_GEX_PORT",
        _environment_int("AEF_IBKR_PORT", 7497, lower=1, upper=65_535),
        lower=1,
        upper=65_535,
    ),
    market_data_type=_environment_int(
        "AEF_IBKR_MARKET_DATA_TYPE",
        1,
        lower=1,
        upper=4,
    ),
    gex_market_data_type=_environment_int(
        "AEF_IBKR_GEX_MARKET_DATA_TYPE",
        3,
        lower=1,
        upper=4,
    ),
    generation=0,
    source="environment",
)


def ibkr_runtime_settings_snapshot() -> IbkrRuntimeSettingsSnapshot:
    with _LOCK:
        return _SNAPSHOT


def publish_ibkr_runtime_port(port: int) -> IbkrRuntimeSettingsSnapshot:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("IBKR_RUNTIME_PORT_INVALID")
    global _SNAPSHOT
    with _LOCK:
        if _SNAPSHOT.port == port and _SNAPSHOT.source == "client-settings":
            return _SNAPSHOT
        _SNAPSHOT = replace(
            _SNAPSHOT,
            port=port,
            gex_port=port,
            market_data_type=1,
            gex_market_data_type=1,
            generation=_SNAPSHOT.generation + 1,
            source="client-settings",
        )
        return _SNAPSHOT
