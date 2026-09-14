from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from aef_terminal.data.gex.contracts import require_gex_contract_market_facts
from aef_terminal.data.gex.payload_contract import (
    gex_payload_strike_count,
    gex_payload_strikes,
)
from aef_terminal.data.gex.utils import finite_number_or_none
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
)


class GexActivityDelta(TypedDict):
    status: Literal[
        "ok",
        "missing_input",
        "missing_overlap",
        "counter_reset",
    ]
    selected_strikes: int
    selected_contracts: int
    matched_contracts: int
    call_volume_delta: float | None
    put_volume_delta: float | None


def _selected_contract_universe(
    payload: Mapping[str, Any] | None,
    selected_strikes: frozenset[float],
) -> (
    dict[
        int,
        tuple[tuple[str, str, str, float, float], str, float],
    ]
    | None
):
    raw = payload.get("raw") if isinstance(payload, Mapping) else None
    contract_rows = raw.get("contracts") if isinstance(raw, Mapping) else None
    if not isinstance(contract_rows, list) or not selected_strikes:
        return None

    universe: dict[
        int,
        tuple[tuple[str, str, str, float, float], str, float],
    ] = {}
    rights_by_series: dict[tuple[str, str, str, float, float], set[str]] = {}
    for row in contract_rows:
        if not isinstance(row, Mapping):
            return None
        strike_value = row.get("strike")
        if (
            isinstance(strike_value, bool)
            or not isinstance(strike_value, (int, float))
            or (strike := finite_number_or_none(strike_value)) is None
        ):
            return None
        strike = float(strike)
        if strike not in selected_strikes:
            continue
        con_id = parse_exact_positive_decimal_provider_id(row.get("con_id"))
        expiry = row.get("expiry")
        trading_class = row.get("trading_class")
        exchange = row.get("exchange")
        multiplier_value = row.get("multiplier")
        right = row.get("right")
        try:
            volume = require_gex_contract_market_facts(row)["volume"]
        except ValueError:
            return None
        if (
            con_id <= 0
            or not isinstance(expiry, str)
            or not expiry
            or expiry != expiry.strip()
            or not isinstance(trading_class, str)
            or not trading_class
            or trading_class != trading_class.strip()
            or not isinstance(exchange, str)
            or not exchange
            or exchange != exchange.strip()
            or isinstance(multiplier_value, bool)
            or not isinstance(multiplier_value, (int, float))
            or (multiplier := finite_number_or_none(multiplier_value)) is None
            or multiplier <= 0
            or right not in {"C", "P"}
            or volume is None
            or con_id in universe
        ):
            return None
        series_key = (
            expiry,
            trading_class,
            exchange,
            float(multiplier),
            strike,
        )
        series_rights = rights_by_series.setdefault(series_key, set())
        if right in series_rights:
            return None
        series_rights.add(right)
        universe[con_id] = (series_key, right, float(volume))
    if any(rights != {"C", "P"} for rights in rights_by_series.values()):
        return None
    return universe


def _gex_activity_delta(
    previous_payload: dict[str, Any] | None,
    current_payload: dict[str, Any],
) -> GexActivityDelta:
    """Measure gross option-volume activity over one exact selected contract set.

    Volume is intentionally not interpreted as dealer positioning, opening or
    closing flow. OI is excluded because the broker value is a settlement
    snapshot rather than an intraday position update.
    """

    current_strikes = gex_payload_strikes(current_payload)
    if not current_strikes or not isinstance(previous_payload, dict):
        return {
            "status": "missing_input",
            "selected_strikes": 0,
            "selected_contracts": 0,
            "matched_contracts": 0,
            "call_volume_delta": None,
            "put_volume_delta": None,
        }
    spot_value = current_payload.get("spot")
    spot = (
        finite_number_or_none(spot_value)
        if not isinstance(spot_value, bool) and isinstance(spot_value, (int, float))
        else None
    )
    current_strike_count = gex_payload_strike_count(current_payload)
    previous_strike_count = gex_payload_strike_count(previous_payload)
    if (
        spot is None
        or current_strike_count is None
        or previous_strike_count is None
        or current_strike_count != previous_strike_count
    ):
        return {
            "status": "missing_input",
            "selected_strikes": 0,
            "selected_contracts": 0,
            "matched_contracts": 0,
            "call_volume_delta": None,
            "put_volume_delta": None,
        }
    selected_strike_values: list[float] = []
    for row in current_strikes:
        raw_strike = row.get("strike")
        strike = (
            finite_number_or_none(raw_strike)
            if not isinstance(raw_strike, bool) and isinstance(raw_strike, (int, float))
            else None
        )
        if strike is None or strike <= 0:
            return {
                "status": "missing_input",
                "selected_strikes": 0,
                "selected_contracts": 0,
                "matched_contracts": 0,
                "call_volume_delta": None,
                "put_volume_delta": None,
            }
        selected_strike_values.append(float(strike))
    selected_strikes = frozenset(selected_strike_values)
    if (
        len(selected_strike_values) != current_strike_count
        or len(selected_strikes) != current_strike_count
    ):
        return {
            "status": "missing_input",
            "selected_strikes": 0,
            "selected_contracts": 0,
            "matched_contracts": 0,
            "call_volume_delta": None,
            "put_volume_delta": None,
        }
    current_contracts = _selected_contract_universe(current_payload, selected_strikes)
    previous_contracts = _selected_contract_universe(previous_payload, selected_strikes)
    selected_contract_count = len(current_contracts or {})
    matched_contract_count = len(
        set(current_contracts or {}).intersection(previous_contracts or {})
    )
    if current_contracts is None or previous_contracts is None:
        return {
            "status": "missing_input",
            "selected_strikes": len(selected_strikes),
            "selected_contracts": selected_contract_count,
            "matched_contracts": matched_contract_count,
            "call_volume_delta": None,
            "put_volume_delta": None,
        }
    current_covered_strikes = {contract[0][4] for contract in current_contracts.values()}
    previous_covered_strikes = {contract[0][4] for contract in previous_contracts.values()}
    if (
        not current_contracts
        or current_covered_strikes != set(selected_strikes)
        or previous_covered_strikes != set(selected_strikes)
        or set(current_contracts) != set(previous_contracts)
    ):
        return {
            "status": "missing_overlap",
            "selected_strikes": len(selected_strikes),
            "selected_contracts": selected_contract_count,
            "matched_contracts": matched_contract_count,
            "call_volume_delta": None,
            "put_volume_delta": None,
        }

    call_volume_delta = 0.0
    put_volume_delta = 0.0
    for con_id, current_contract in current_contracts.items():
        previous_contract = previous_contracts[con_id]
        if current_contract[:2] != previous_contract[:2]:
            return {
                "status": "missing_overlap",
                "selected_strikes": len(selected_strikes),
                "selected_contracts": selected_contract_count,
                "matched_contracts": matched_contract_count,
                "call_volume_delta": None,
                "put_volume_delta": None,
            }
        volume_delta = current_contract[2] - previous_contract[2]
        if volume_delta < -1e-9:
            return {
                "status": "counter_reset",
                "selected_strikes": len(selected_strikes),
                "selected_contracts": selected_contract_count,
                "matched_contracts": matched_contract_count,
                "call_volume_delta": None,
                "put_volume_delta": None,
            }
        if current_contract[1] == "C":
            call_volume_delta += volume_delta
        else:
            put_volume_delta += volume_delta
    return {
        "status": "ok",
        "selected_strikes": len(selected_strikes),
        "selected_contracts": selected_contract_count,
        "matched_contracts": matched_contract_count,
        "call_volume_delta": call_volume_delta,
        "put_volume_delta": put_volume_delta,
    }
