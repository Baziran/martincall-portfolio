from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import NY_TZ
from aef_terminal.data.gex.contracts import (
    require_gex_contract_market_facts,
    require_gex_option_series_identity,
    require_gex_valuation_rates,
)
from aef_terminal.data.gex.utils import _expiration_date
from aef_terminal.domain import OptionRight


def _require_option_right(right: OptionRight) -> OptionRight:
    if not isinstance(right, OptionRight):
        raise TypeError("GEX option math requires typed OptionRight")
    return right


def _time_to_expiry_years(
    expiry: str,
    now: datetime | None = None,
    *,
    expiry_at: str | datetime | None = None,
    require_exact: bool = False,
) -> float | None:
    expiry_date = _expiration_date(expiry)
    current = (now or datetime.now(tz=UTC)).astimezone(NY_TZ)
    exact: datetime | None = None
    if isinstance(expiry_at, datetime):
        exact = expiry_at
    elif expiry_at:
        try:
            exact = datetime.fromisoformat(str(expiry_at).replace("Z", "+00:00"))
        except ValueError:
            exact = None
    if exact is not None and exact.tzinfo is not None:
        close = exact.astimezone(NY_TZ)
        minutes = (close - current).total_seconds() / 60.0
        if minutes <= 0:
            return None
        minutes = max(minutes, 1.0)
    elif require_exact:
        return None
    elif expiry_date is None:
        minutes = 1.0
    else:
        close = datetime(expiry_date.year, expiry_date.month, expiry_date.day, 16, 0, tzinfo=NY_TZ)
        minutes = max((close - current).total_seconds() / 60.0, 1.0)
    return minutes / (365.0 * 24.0 * 60.0)


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _black_scholes_gamma_delta(
    *,
    spot: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
    dividend_yield: float,
) -> dict[str, float] | None:
    right = _require_option_right(right)
    if min(float(spot), float(strike), float(years), float(iv)) <= 0:
        return None
    sqrt_t = math.sqrt(years)
    denom = iv * sqrt_t
    if denom <= 0:
        return None
    d1 = (
        math.log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * iv * iv) * years
    ) / denom
    carry_discount = math.exp(-dividend_yield * years)
    gamma = carry_discount * _normal_pdf(d1) / (spot * denom)
    call_delta = carry_discount * _normal_cdf(d1)
    delta = call_delta if right == OptionRight.CALL else call_delta - carry_discount
    return {"gamma": gamma, "delta": delta}


def _black_76_gamma_delta(
    *,
    futures_price: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
) -> dict[str, float] | None:
    right = _require_option_right(right)
    if min(float(futures_price), float(strike), float(years), float(iv)) <= 0:
        return None
    sqrt_t = math.sqrt(years)
    denom = iv * sqrt_t
    if denom <= 0:
        return None
    d1 = (math.log(futures_price / strike) + 0.5 * iv * iv * years) / denom
    discount = math.exp(-risk_free_rate * years)
    gamma = discount * _normal_pdf(d1) / (futures_price * denom)
    call_delta = discount * _normal_cdf(d1)
    delta = call_delta if right == OptionRight.CALL else call_delta - discount
    return {"gamma": gamma, "delta": delta}


def _option_model_gamma_delta(
    futures_options: bool,
    *,
    spot: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
    dividend_yield: float,
) -> dict[str, float] | None:
    if futures_options:
        return _black_76_gamma_delta(
            futures_price=spot,
            strike=strike,
            years=years,
            iv=iv,
            right=right,
            risk_free_rate=risk_free_rate,
        )
    return _black_scholes_gamma_delta(
        spot=spot,
        strike=strike,
        years=years,
        iv=iv,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )


def _black_scholes_price(
    *,
    spot: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
    dividend_yield: float,
) -> float | None:
    right = _require_option_right(right)
    if min(float(spot), float(strike), float(years), float(iv)) <= 0:
        return None
    sqrt_t = math.sqrt(years)
    denom = iv * sqrt_t
    if denom <= 0:
        return None
    d1 = (
        math.log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * iv * iv) * years
    ) / denom
    d2 = d1 - denom
    carry_discount = math.exp(-dividend_yield * years)
    rate_discount = math.exp(-risk_free_rate * years)
    if right == OptionRight.PUT:
        price = strike * rate_discount * _normal_cdf(-d2) - spot * carry_discount * _normal_cdf(-d1)
    else:
        price = spot * carry_discount * _normal_cdf(d1) - strike * rate_discount * _normal_cdf(d2)
    return max(price, 0.0)


def _black_76_price(
    *,
    futures_price: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
) -> float | None:
    right = _require_option_right(right)
    if min(float(futures_price), float(strike), float(years), float(iv)) <= 0:
        return None
    sqrt_t = math.sqrt(years)
    denom = iv * sqrt_t
    if denom <= 0:
        return None
    d1 = (math.log(futures_price / strike) + 0.5 * iv * iv * years) / denom
    d2 = d1 - denom
    discount = math.exp(-risk_free_rate * years)
    if right == OptionRight.PUT:
        price = discount * (strike * _normal_cdf(-d2) - futures_price * _normal_cdf(-d1))
    else:
        price = discount * (futures_price * _normal_cdf(d1) - strike * _normal_cdf(d2))
    return max(price, 0.0)


def _option_model_price(
    futures_options: bool,
    *,
    spot: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
    dividend_yield: float,
) -> float | None:
    if futures_options:
        return _black_76_price(
            futures_price=spot,
            strike=strike,
            years=years,
            iv=iv,
            right=right,
            risk_free_rate=risk_free_rate,
        )
    return _black_scholes_price(
        spot=spot,
        strike=strike,
        years=years,
        iv=iv,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )


def _implied_option_vol(
    futures_options: bool,
    *,
    spot: float,
    strike: float,
    years: float,
    iv: float,
    right: OptionRight,
    risk_free_rate: float,
    dividend_yield: float,
    market_price: float,
) -> float | None:
    if min(float(spot), float(strike), float(years), float(market_price)) <= 0:
        return None
    low = max(0.0005, float(iv) * 0.25) if iv and iv > 0 else 0.0005
    high = max(low * 2.0, float(iv) * 4.0) if iv and iv > 0 else 3.0
    max_high = 20.0
    while high < max_high:
        high_price = _option_model_price(
            futures_options,
            spot=spot,
            strike=strike,
            years=years,
            iv=high,
            right=right,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
        if high_price is None:
            return None
        if high_price >= market_price:
            break
        high = min(high * 2.0, max_high)
    high_price = _option_model_price(
        futures_options,
        spot=spot,
        strike=strike,
        years=years,
        iv=high,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    if high_price is None or high_price < market_price:
        return None
    for _ in range(72):
        mid = (low + high) / 2.0
        price = _option_model_price(
            futures_options,
            spot=spot,
            strike=strike,
            years=years,
            iv=mid,
            right=right,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
        if price is None:
            return None
        if abs(price - market_price) <= 0.0005:
            return mid
        if price > market_price:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def compute_gamma_flip(
    futures_options: bool,
    contract_rows: Sequence[Mapping[str, Any]],
    spot: float,
    *,
    risk_free_rate: object,
    dividend_yield: object,
) -> float | None:
    """Return the nearest modeled zero-gamma spot under sticky-strike IV.

    The chain is repriced at hypothetical underlying prices.  A cumulative sum
    across strike buckets is not a zero-gamma calculation and must never be used
    as this public value.  Every selected row must belong to one exact Call/Put
    pair and remain priceable across the model search; partial model coverage is
    unavailable rather than a gamma flip for a hidden subset.
    """

    if type(futures_options) is not bool:
        raise TypeError("GEX gamma-flip model requires typed futures_options")
    if isinstance(spot, bool) or not isinstance(spot, (int, float)):
        return None
    anchor = float(spot)
    if not math.isfinite(anchor) or anchor <= 0:
        return None
    try:
        valuation_rates = require_gex_valuation_rates(
            {
                "risk_free_rate": risk_free_rate,
                "dividend_yield": dividend_yield,
            }
        )
    except ValueError:
        return None
    rows: list[
        tuple[
            tuple[str, str, str, float, float],
            float,
            OptionRight,
            float,
            float,
            float,
            float,
        ]
    ] = []
    rights_by_series_strike: dict[
        tuple[str, str, str, float, float],
        set[OptionRight],
    ] = defaultdict(set)
    for row in contract_rows:
        try:
            market_facts = require_gex_contract_market_facts(row)
            series = require_gex_option_series_identity(row)
        except ValueError:
            return None
        raw_strike = row.get("strike")
        raw_years = row.get("time_to_expiry_years")
        raw_iv = row.get("iv")
        raw_multiplier = row.get("multiplier")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (raw_strike, raw_years, raw_iv, raw_multiplier)
        ):
            return None
        strike = float(raw_strike)
        years = float(raw_years)
        iv = float(raw_iv)
        oi = market_facts["open_interest"]
        multiplier = float(raw_multiplier)
        right_value = row.get("right")
        if right_value not in {"C", "P"}:
            return None
        right = OptionRight(right_value)
        if (
            not math.isfinite(strike)
            or strike <= 0
            or not math.isfinite(years)
            or years <= 0
            or not math.isfinite(iv)
            or iv <= 0
            or oi is None
            or oi < 0
            or not math.isfinite(multiplier)
            or multiplier <= 0
        ):
            return None
        series_strike = (
            series["expiry"],
            series["trading_class"],
            series["exchange"],
            series["multiplier"],
            strike,
        )
        if right in rights_by_series_strike[series_strike]:
            return None
        rights_by_series_strike[series_strike].add(right)
        rows.append(
            (
                series_strike,
                strike,
                right,
                years,
                iv,
                oi,
                multiplier,
            )
        )
    if any(
        rights != {OptionRight.CALL, OptionRight.PUT} for rights in rights_by_series_strike.values()
    ):
        return None
    if len(rows) < 4 or len(rights_by_series_strike) < 2:
        return None

    strikes = [row[1] for row in rows]
    lower = max(0.01, min(min(strikes), anchor * 0.85))
    upper = max(max(strikes), anchor * 1.15)
    if upper <= lower:
        return None

    def total_gex(hypothetical_spot: float) -> float | None:
        total = 0.0
        for _series_strike, strike, right, years, iv, oi, multiplier in rows:
            greeks = _option_model_gamma_delta(
                futures_options,
                spot=hypothetical_spot,
                strike=strike,
                years=years,
                iv=iv,
                right=right,
                risk_free_rate=valuation_rates["risk_free_rate"],
                dividend_yield=valuation_rates["dividend_yield"],
            )
            if greeks is None:
                return None
            gamma = greeks.get("gamma")
            if not isinstance(gamma, (int, float)) or isinstance(gamma, bool):
                return None
            gamma = float(gamma)
            if not math.isfinite(gamma) or gamma < 0:
                return None
            sign = 1.0 if right is OptionRight.CALL else -1.0
            contribution = (
                sign * gamma * oi * multiplier * hypothetical_spot * hypothetical_spot * 0.01
            )
            if not math.isfinite(contribution):
                return None
            total += contribution
        return total if math.isfinite(total) else None

    grid = [lower + (upper - lower) * index / 120.0 for index in range(121)]
    values: list[tuple[float, float]] = []
    for price in grid:
        value = total_gex(price)
        if value is None:
            return None
        values.append((price, value))
    brackets: list[tuple[float, float]] = []
    for (left_price, left_value), (right_price, right_value) in zip(
        values, values[1:], strict=False
    ):
        if left_value == 0:
            brackets.append((left_price, left_price))
        elif left_value * right_value < 0:
            brackets.append((left_price, right_price))
    if not brackets:
        return None

    roots: list[float] = []
    for left, right in brackets:
        if left == right:
            roots.append(left)
            continue
        left_value = total_gex(left)
        if left_value is None:
            return None
        for _ in range(48):
            middle = (left + right) / 2.0
            middle_value = total_gex(middle)
            if middle_value is None:
                return None
            if abs(middle_value) <= 1e-6 or right - left <= 1e-6:
                left = right = middle
                break
            if left_value * middle_value <= 0:
                right = middle
            else:
                left = middle
                left_value = middle_value
        roots.append((left + right) / 2.0)
    return round(min(roots, key=lambda value: abs(value - anchor)), 4)
