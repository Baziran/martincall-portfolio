"""IBKR option-market ticker parsing for GEX acquisition."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from collections.abc import Mapping
from typing import Any

from aef_terminal.data.gex.contracts import (
    GexOptionReference,
    require_gex_contract_market_facts,
    require_gex_option_reference,
    require_gex_valuation_rates,
    select_gex_option_reference,
)
from aef_terminal.data.gex.dividend import _dividend_yield_from_pv_dividend
from aef_terminal.data.gex.math import (
    _implied_option_vol,
    _option_model_gamma_delta,
    _time_to_expiry_years,
)
from aef_terminal.data.gex.utils import finite_number_or_none
from aef_terminal.data.ibkr.option_contracts import IbkrOptionContract
from aef_terminal.data.ibkr.market_data import ibkr_market_data_entitlement
from aef_terminal.domain import OptionRight


@dataclass
class GexTickerObservationTracker:
    """Field-specific client-receive evidence for one option subscription."""

    right: OptionRight
    model_greeks_observation: Any | None = field(default=None, repr=False)
    observed_tick_packet: tuple[Any, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    model_greeks_received_at: datetime | None = None
    open_interest_received_at: datetime | None = None
    provider_callback_count: int = 0
    provider_tick_count: int = 0

    def observe(self, ticker: Any, *, provider_callback: bool = True) -> None:
        raw_received_at = getattr(ticker, "time", None)
        received_at = (
            raw_received_at.astimezone(UTC)
            if isinstance(raw_received_at, datetime)
            and raw_received_at.tzinfo is not None
            and raw_received_at.utcoffset() is not None
            else datetime.now(tz=UTC)
        )
        model_greeks = getattr(ticker, "modelGreeks", None)
        if model_greeks is not None and model_greeks is not self.model_greeks_observation:
            self.model_greeks_observation = model_greeks
            self.model_greeks_received_at = received_at
        expected_oi_tick = 27 if self.right is OptionRight.CALL else 28
        tick_packet = tuple(getattr(ticker, "ticks", ()) or ())
        if provider_callback:
            self.provider_callback_count += 1
            self.provider_tick_count += len(tick_packet)
        prior_tick_ids = {id(tick) for tick in self.observed_tick_packet}
        self.observed_tick_packet = tick_packet
        for tick in tick_packet:
            if id(tick) in prior_tick_ids:
                continue
            if getattr(tick, "tickType", None) != expected_oi_tick:
                continue
            tick_time = getattr(tick, "time", None)
            self.open_interest_received_at = (
                tick_time.astimezone(UTC)
                if isinstance(tick_time, datetime)
                and tick_time.tzinfo is not None
                and tick_time.utcoffset() is not None
                else received_at
            )

    def take_provider_callback_activity(self) -> tuple[int, int]:
        activity = (self.provider_callback_count, self.provider_tick_count)
        self.provider_callback_count = 0
        self.provider_tick_count = 0
        return activity

    def evidence(self, ticker: Any) -> dict[str, Any]:
        raw_market_data_type = getattr(ticker, "marketDataType", None)
        market_data_type = (
            raw_market_data_type
            if type(raw_market_data_type) is int and raw_market_data_type in {1, 2, 3, 4}
            else None
        )
        return {
            "market_data_type": market_data_type,
            "market_data_entitlement": ibkr_market_data_entitlement(market_data_type),
            "model_greeks_received_at": (
                self.model_greeks_received_at.isoformat()
                if self.model_greeks_received_at is not None
                else None
            ),
            "open_interest_received_at": (
                self.open_interest_received_at.isoformat()
                if self.open_interest_received_at is not None
                else None
            ),
            "observation_time_basis": "client_receive",
            "open_interest_as_of": "previous_settlement",
        }


def attach_gex_ticker_observation(
    ticker: Any,
    right: OptionRight,
) -> GexTickerObservationTracker:
    tracker = GexTickerObservationTracker(right=right)
    update_event = getattr(ticker, "updateEvent", None)
    if update_event is not None:
        update_event += tracker.observe
    return tracker


def detach_gex_ticker_observation(
    ticker: Any,
    tracker: GexTickerObservationTracker,
) -> None:
    update_event = getattr(ticker, "updateEvent", None)
    if update_event is None:
        return
    try:
        update_event -= tracker.observe
    except TypeError, ValueError:
        return


def _option_row_from_ticker(
    contract: IbkrOptionContract,
    ticker: Any,
    spot: float,
    *,
    risk_free_rate: float,
    dividend_yield: float,
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    valuation_rates = require_gex_valuation_rates(
        {
            "risk_free_rate": risk_free_rate,
            "dividend_yield": dividend_yield,
        }
    )
    risk_free_rate = valuation_rates["risk_free_rate"]
    dividend_yield = valuation_rates["dividend_yield"]
    greeks, greek_source = _greeks_from_ticker(ticker)
    gamma = finite_number_or_none(getattr(greeks, "gamma", None)) if greeks is not None else None
    raw_delta = (
        finite_number_or_none(getattr(greeks, "delta", None)) if greeks is not None else None
    )
    iv = _iv_from_ticker(ticker, greeks)
    iv_source = "ticker_iv" if iv is not None else ""
    right = contract.right
    delta = (
        raw_delta
        if raw_delta is not None
        and (
            (right is OptionRight.CALL and 0.0 <= raw_delta <= 1.0)
            or (right is OptionRight.PUT and -1.0 <= raw_delta <= 0.0)
        )
        else None
    )
    strike = contract.strike
    expiry = contract.expiry
    expiry_at = contract.expiry_at
    t_years = _time_to_expiry_years(expiry, expiry_at=expiry_at, require_exact=True)
    option_reference = _ticker_reference_price(ticker)
    reference_option_price = option_reference["reference_option_price"]
    futures_options = contract.futures_option
    if iv is None and reference_option_price is not None and t_years is not None:
        iv = _implied_option_vol(
            futures_options,
            spot=spot,
            strike=strike,
            years=t_years,
            iv=0.0,
            right=right,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            market_price=reference_option_price,
        )
        if iv is not None:
            iv_source = "reference_option_price_iv"
    pv_dividend = _pv_dividend_from_ticker(ticker, greeks)
    broker_dividend_yield = _dividend_yield_from_pv_dividend(
        spot=spot,
        years=t_years or 0.0,
        pv_dividend=pv_dividend,
    )
    if (gamma is None or gamma <= 0 or delta is None) and iv is not None and t_years is not None:
        local = _option_model_gamma_delta(
            futures_options,
            spot=spot,
            strike=strike,
            years=t_years,
            iv=iv,
            right=right,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
        if local is not None:
            gamma = local["gamma"] if gamma is None or gamma <= 0 else gamma
            delta = local["delta"] if delta is None else delta
            greek_source = greek_source or "local_iv"
    open_interest = _open_interest_from_ticker(ticker, right)
    volume = _volume_from_ticker(ticker)

    sign = 1.0 if right is OptionRight.CALL else -1.0
    gex = (
        sign * gamma * open_interest * contract.multiplier * spot * spot * 0.01
        if gamma is not None and open_interest is not None
        else None
    )
    exact_observation = dict(observation or {})
    raw_market_data_type = exact_observation.get("market_data_type")
    market_data_type = (
        raw_market_data_type
        if type(raw_market_data_type) is int and raw_market_data_type in {1, 2, 3, 4}
        else None
    )
    market_data_entitlement = exact_observation.get("market_data_entitlement")
    if market_data_entitlement != ibkr_market_data_entitlement(market_data_type):
        market_data_entitlement = ibkr_market_data_entitlement(market_data_type)
    row = {
        "expiry": expiry,
        "trading_class": contract.trading_class,
        "exchange": contract.exchange,
        "strike": strike,
        "right": right.value,
        "multiplier": contract.multiplier,
        "gamma": gamma,
        "delta": delta,
        "iv": iv,
        "time_to_expiry_years": t_years,
        "expiry_at": expiry_at.isoformat() if expiry_at is not None else None,
        "expiry_time_source": "ibkr_contract_details" if expiry_at else "unknown",
        "pv_dividend": pv_dividend,
        "broker_dividend_yield": broker_dividend_yield,
        **option_reference,
        "open_interest": open_interest,
        "volume": volume,
        "gex": gex,
        "abs_gex": abs(gex) if gex is not None else None,
        "greek_source": greek_source,
        "iv_source": iv_source,
        "market_data_type": market_data_type,
        "market_data_entitlement": market_data_entitlement,
        "model_greeks_received_at": exact_observation.get("model_greeks_received_at"),
        "open_interest_received_at": exact_observation.get("open_interest_received_at"),
        "observation_time_basis": "client_receive",
        "open_interest_as_of": "previous_settlement",
        "bid": finite_number_or_none(getattr(ticker, "bid", None)),
        "ask": finite_number_or_none(getattr(ticker, "ask", None)),
        "last": finite_number_or_none(getattr(ticker, "last", None)),
        "con_id": contract.con_id,
        "local_symbol": contract.local_symbol,
    }
    require_gex_contract_market_facts(row, spot=spot)
    require_gex_option_reference(row)
    return row


def _ticker_reference_price(ticker: Any) -> GexOptionReference:
    bid = finite_number_or_none(getattr(ticker, "bid", None))
    ask = finite_number_or_none(getattr(ticker, "ask", None))
    market_price = getattr(ticker, "marketPrice", None)
    try:
        selected_market_price = market_price() if callable(market_price) else None
    except Exception:
        selected_market_price = None
    return select_gex_option_reference(
        bid=bid,
        ask=ask,
        market_price=finite_number_or_none(selected_market_price),
        last=finite_number_or_none(getattr(ticker, "last", None)),
        close=finite_number_or_none(getattr(ticker, "close", None)),
    )


def _greeks_from_ticker(ticker: Any) -> tuple[Any | None, str]:
    for name in ("modelGreeks", "lastGreeks", "bidGreeks", "askGreeks"):
        greeks = getattr(ticker, name, None)
        gamma = (
            finite_number_or_none(getattr(greeks, "gamma", None)) if greeks is not None else None
        )
        if gamma is not None and gamma > 0:
            return greeks, name
    return None, ""


def _iv_from_ticker(ticker: Any, preferred_greeks: Any | None = None) -> float | None:
    candidates = [preferred_greeks] if preferred_greeks is not None else []
    candidates.extend(
        getattr(ticker, name, None)
        for name in ("modelGreeks", "lastGreeks", "bidGreeks", "askGreeks")
    )
    candidates.append(ticker)
    for item in candidates:
        if item is None:
            continue
        for name in ("impliedVol", "impliedVolatility", "iv"):
            value = finite_number_or_none(getattr(item, name, None))
            if value is not None and 0.0001 < value < 10.0:
                return value
    return None


def _pv_dividend_from_ticker(ticker: Any, preferred_greeks: Any | None = None) -> float | None:
    candidates = [preferred_greeks] if preferred_greeks is not None else []
    candidates.extend(
        getattr(ticker, name, None)
        for name in ("modelGreeks", "lastGreeks", "bidGreeks", "askGreeks")
    )
    candidates.append(ticker)
    for item in candidates:
        if item is None:
            continue
        for name in ("pvDividend", "pv_dividend"):
            value = finite_number_or_none(getattr(item, name, None))
            if value is not None and value >= 0:
                return value
    return None


def _open_interest_from_ticker(ticker: Any, right: OptionRight) -> float | None:
    name = "callOpenInterest" if right is OptionRight.CALL else "putOpenInterest"
    direct = finite_number_or_none(getattr(ticker, name, None))
    return direct if direct is not None and direct >= 0 else None


def _volume_from_ticker(ticker: Any) -> float | None:
    """Return native daily volume for the exact option contract (IBKR tick 8)."""

    value = finite_number_or_none(getattr(ticker, "volume", None))
    return value if value is not None and value >= 0 else None
