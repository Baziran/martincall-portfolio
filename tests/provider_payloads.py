from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING

from aef_terminal.domain import Bar
from aef_terminal.features.provider_session import ProviderSessionReset, provider_vwap_session

if TYPE_CHECKING:
    from aef_terminal.ui.runtime.quote_stream import QuoteRouteSnapshot


def quote_route_snapshot(instruments: list[dict]) -> QuoteRouteSnapshot:
    """Build the canonical immutable read model without a database."""

    from aef_terminal.ui.runtime.quote_stream import QuoteStreamRuntime

    return QuoteStreamRuntime().refresh_routes_from_store(
        SimpleNamespace(read_watchlist_snapshot=lambda: (instruments, 1))
    )


def _resolved_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def instrument_with_bar_sessions(instrument: dict, bars: Sequence[Bar]) -> dict:
    if str((instrument.get("session") or {}).get("calendar") or "") == "continuous_24_7":
        return instrument
    intervals = []
    for day in sorted({bar.ts.astimezone(UTC).date() for bar in bars}):
        opens_at = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        intervals.append(
            {
                "session_date": day.isoformat(),
                "opens_at": opens_at.isoformat(),
                "closes_at": (opens_at + timedelta(days=1)).isoformat(),
                "status": "open",
            }
        )
    return {
        **instrument,
        "session": {
            "provider": str(instrument.get("provider") or "test"),
            "calendar": "test_provider_schedule",
            "timezone": "UTC",
            "trading_intervals": intervals,
            "liquid_intervals": intervals,
        },
    }


def explicit_vwap_session_for_bars(bars: Sequence[Bar]) -> ProviderSessionReset:
    return provider_vwap_session(
        instrument_with_bar_sessions({"provider": "test"}, bars),
        bars,
    )


def ibkr_stock_payload(
    symbol: str,
    *,
    name: str | None = None,
    con_id: int = 12345,
    asset_class: str = "stock",
    sec_type: str = "STK",
) -> dict:
    key = str(symbol or "")
    instrument_id = f"ibkr|contract|{con_id}"
    return {
        "instrument_id": instrument_id,
        "key": key,
        "instrument_key": key,
        "display": key,
        "name": name or key,
        "provider": "ibkr",
        "provider_symbol": key,
        "provider_contract_id": str(con_id),
        "asset_class": asset_class,
        "contract_identity": {
            "asset_class": asset_class,
            "provider": "ibkr",
            "provider_contract_id": str(con_id),
            "symbol": key,
            "sec_type": sec_type,
            "exchange": "SMART",
            "currency": "USD",
            "con_id": con_id,
            "min_tick": 0.01,
        },
    }


def ibkr_future_payload(
    root: str,
    *,
    exchange: str = "CME",
    con_id: int = 11004968,
    local_symbol: str | None = None,
) -> dict:
    key = str(root or "")
    local = local_symbol or f"{key}U6"
    instrument_id = f"ibkr|future_root|{key}|{exchange}|USD|"
    return {
        "instrument_id": instrument_id,
        "key": key,
        "instrument_key": key,
        "display": key,
        "name": f"{key} futures root",
        "provider": "ibkr",
        "provider_symbol": key,
        "asset_class": "future",
        "contract_identity": {
            "asset_class": "future",
            "provider": "ibkr",
            "root": key,
            "exchange": exchange,
            "currency": "USD",
            "identity_scope": "root",
            "history_contract_mode": "continuous_future",
            "live_contract_mode": "provider_current_contract",
            "current_contract": {
                "contract_key": local,
                "provider_contract_id": str(con_id),
                "con_id": con_id,
                "local_symbol": local,
                "expiry": "20260918",
                "contract_month": "202609",
                "resolved_at": _resolved_now(),
                "min_tick": {"ES": 0.25, "GC": 0.1, "CL": 0.01}.get(key, 0.01),
            },
        },
        "continuous_series": {
            "instrument_key": key,
            "provider": "ibkr",
            "provider_symbol": key,
            "series_type": "provider_bound_continuous",
            "roll_source": "provider",
        },
    }


def coinbase_btc_payload() -> dict:
    return {
        "instrument_id": "coinbase|contract|BTC-USD",
        "key": "BTC",
        "instrument_key": "BTC",
        "display": "BTC",
        "name": "Bitcoin USD",
        "provider": "coinbase",
        "provider_symbol": "BTC-USD",
        "provider_contract_id": "BTC-USD",
        "asset_class": "crypto",
        "session": {
            "provider": "coinbase",
            "calendar": "continuous_24_7",
            "family": "crypto",
            "timezone": "UTC",
        },
        "contract_identity": {
            "asset_class": "crypto",
            "provider": "coinbase",
            "provider_contract_id": "BTC-USD",
            "product_id": "BTC-USD",
        },
    }
