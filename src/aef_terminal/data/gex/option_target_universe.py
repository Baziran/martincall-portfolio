"""Admission and theoretical characterization of Option Point contract rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aef_terminal.data.gex.math import (
    _option_model_gamma_delta,
    _option_model_price,
    _time_to_expiry_years,
)
from aef_terminal.data.gex.utils import (
    _expiration_date,
    finite_number_or_none,
    _num,
    parse_gex_timestamp,
)
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
)
from aef_terminal.domain import OptionRight


def _require_option_target_contracts(
    contracts: list[dict[str, Any]],
    *,
    futures_options: bool,
) -> None:
    """Require exact, unique provider facts for an admitted contract universe."""
    exact_con_ids: set[int] = set()
    expected_sec_type = "FOP" if futures_options else "OPT"
    for contract_row in contracts:
        contract_con_id = parse_exact_positive_decimal_provider_id(contract_row.get("con_id"))
        if contract_con_id <= 0 or contract_con_id in exact_con_ids:
            raise ValueError("option target contract universe has invalid provider conId identity")
        if contract_row.get("right") not in {"C", "P"}:
            raise ValueError("option target contract right must be exactly C or P")
        contract_strike = finite_number_or_none(contract_row.get("strike"))
        if contract_strike is None or contract_strike <= 0:
            raise ValueError("option target contract strike must be finite and positive")
        if _expiration_date(str(contract_row.get("expiry") or "")) is None:
            raise ValueError("option target contract expiry must be an exact provider date")
        if parse_gex_timestamp(contract_row.get("expiry_at")) is None:
            raise ValueError("option target contract requires exact expiry time metadata")
        require_exact_identity_text(
            contract_row.get("exchange"),
            field="OPTION_CONTRACT_EXCHANGE",
        )
        require_exact_identity_text(
            contract_row.get("trading_class"),
            field="OPTION_CONTRACT_TRADING_CLASS",
        )
        contract_multiplier = finite_number_or_none(contract_row.get("multiplier"))
        if contract_multiplier is None or contract_multiplier <= 0:
            raise ValueError("option target contract multiplier must be positive")
        if contract_row.get("sec_type") != expected_sec_type:
            raise ValueError("option target contract type disagrees with instrument route")
        exact_con_ids.add(contract_con_id)


def _characterize_option_target_contracts(
    contracts: list[dict[str, Any]],
    *,
    futures_options: bool,
    valuation_spot: float,
    valuation_now: datetime,
    theoretical_iv: float,
    valuation_rates: tuple[float, float],
) -> None:
    """Attach deterministic theoretical reference facts without provider I/O."""
    for contract_row in contracts:
        contract_expiry = str(contract_row.get("expiry") or "")
        current_years = _time_to_expiry_years(
            contract_expiry,
            valuation_now,
            expiry_at=contract_row.get("expiry_at"),
            require_exact=True,
        )
        if current_years is None:
            raise ValueError("option target contract exact expiry time is unavailable")
        model_contract_right = OptionRight(contract_row.get("right"))
        contract_strike = _num(contract_row.get("strike"))
        reference = _option_model_price(
            futures_options,
            spot=valuation_spot,
            strike=contract_strike,
            years=current_years,
            iv=theoretical_iv,
            right=model_contract_right,
            risk_free_rate=valuation_rates[0],
            dividend_yield=valuation_rates[1],
        )
        greeks = _option_model_gamma_delta(
            futures_options,
            spot=valuation_spot,
            strike=contract_strike,
            years=current_years,
            iv=theoretical_iv,
            right=model_contract_right,
            risk_free_rate=valuation_rates[0],
            dividend_yield=valuation_rates[1],
        )
        contract_row.update(
            {
                "iv": theoretical_iv,
                "iv_source": "theoretical_default",
                "delta": None if greeks is None else greeks["delta"],
                "gamma": None if greeks is None else greeks["gamma"],
                "reference_option_price": reference,
                "reference_option_price_source": (
                    "theoretical_model"
                    if reference is not None and reference > 0
                    else "unavailable"
                ),
                "open_interest": None,
                "volume": None,
                "gex": None,
                "abs_gex": None,
                "bid": None,
                "ask": None,
                "last": None,
            }
        )
