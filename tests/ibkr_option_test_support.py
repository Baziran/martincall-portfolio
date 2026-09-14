from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

from aef_terminal.data.ibkr.option_contracts import parse_ibkr_option_contract


def ibkr_option(
    *,
    con_id: Any,
    strike: float,
    right: str,
    expiry: str = "20260719",
    symbol: str = "ES",
    sec_type: str = "FOP",
    exchange: str = "CME",
    trading_class: str | None = None,
    multiplier: str = "50",
    currency: str = "USD",
    local_symbol: str = "",
    expiry_at: str | datetime | None = None,
):
    raw = SimpleNamespace(
        conId=con_id,
        secType=sec_type,
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right=right,
        exchange=exchange,
        tradingClass=trading_class or symbol,
        multiplier=multiplier,
        currency=currency,
        localSymbol=local_symbol,
    )
    return parse_ibkr_option_contract(raw, expiry_at=expiry_at)
