from __future__ import annotations

from typing import Final

from aef_terminal.data.market_data import (
    KnownMarketDataEntitlement,
    MarketDataEntitlement,
)

_IBKR_MARKET_DATA_ENTITLEMENT_BY_TYPE: Final[dict[int, KnownMarketDataEntitlement]] = {
    1: "live",
    2: "frozen",
    3: "delayed",
    4: "delayed_frozen",
}


def request_ibkr_market_data_ticker(
    ib: object,
    contract: object,
    generic_ticks: str,
    snapshot: bool,
    regulatory_snapshot: bool,
) -> object:
    """Subscribe with entitlement unknown until IBKR sends its actual type."""

    ticker = ib.reqMktData(  # type: ignore[attr-defined]
        contract,
        generic_ticks,
        snapshot,
        regulatory_snapshot,
    )
    ticker.marketDataType = 0
    return ticker


def ibkr_market_data_entitlement(
    actual_market_data_type: object,
) -> MarketDataEntitlement:
    """Map one actual TWS marketDataType callback value without coercion."""

    if type(actual_market_data_type) is not int:
        return "unknown"
    return _IBKR_MARKET_DATA_ENTITLEMENT_BY_TYPE.get(
        actual_market_data_type,
        "unknown",
    )
