from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from aef_terminal.data.gex.contracts import require_gex_contract_market_facts
from aef_terminal.data.gex.utils import finite_number_or_none
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
)

_LIVE_GEX_TREND_WINDOW_SECONDS = 120.0
_LIVE_GEX_TREND_MIN_SECONDS = 20.0
_LIVE_GEX_TREND_MIN_REL_CHANGE = 0.05
_LIVE_GEX_NET_TREND_MIN_REL_CHANGE = 0.08
_LIVE_GEX_TREND_MIN_ABS_GEX = 1_000_000.0
_LIVE_GEX_OPTION_VOLUME_HISTORY_SECONDS = 120.0
_LIVE_GEX_OPTION_VOLUME_EVENT_SECONDS = 15.0
_LIVE_GEX_PROCESS_TURNOVER_HIGH = 0.05
_LIVE_GEX_PROCESS_TURNOVER_ACTIVE = 0.015
_LIVE_GEX_PROCESS_ACCELERATION_ACTIVE = 1.8
_LIVE_GEX_PROCESS_MIN_VOLUME_DELTA = 3.0
_LIVE_GEX_PROCESS_MIN_TURNOVER = 0.001


def _live_trend_price_key(value: Any) -> str:
    number = finite_number_or_none(value)
    if number is None:
        return ""
    return f"{number:.6f}"


def _live_contract_key(row: Mapping[str, Any]) -> str:
    con_id = parse_exact_positive_decimal_provider_id(row.get("con_id"))
    return f"con:{con_id}" if con_id > 0 else ""


def _live_option_volume_frame(
    rows: Sequence[Mapping[str, Any]],
    ts: datetime,
    spot: float | None = None,
) -> dict[str, Any]:
    contracts: dict[str, dict[str, Any]] = {}
    frame_reason = ""
    invalid_contract_key = ""
    for row in rows:
        strike_value = row.get("strike")
        strike = (
            finite_number_or_none(strike_value)
            if not isinstance(strike_value, bool) and isinstance(strike_value, (int, float))
            else None
        )
        right = row.get("right")
        expiry = row.get("expiry")
        key = _live_contract_key(row)
        if (
            not key
            or strike is None
            or strike <= 0
            or right not in {"C", "P"}
            or not isinstance(expiry, str)
            or not expiry
            or expiry != expiry.strip()
        ):
            frame_reason = "INVALID_CONTRACT_IDENTITY"
            invalid_contract_key = key
            break
        if key in contracts:
            frame_reason = "DUPLICATE_CONTRACT_KEY"
            invalid_contract_key = key
            break

        try:
            volume = require_gex_contract_market_facts(row)["volume"]
        except ValueError:
            frame_reason = "INVALID_CONTRACT_MARKET_FACTS"
            volume = None
        if not frame_reason and volume is None:
            frame_reason = "VOLUME_UNAVAILABLE"
        if frame_reason:
            invalid_contract_key = key
            break

        contracts[key] = {
            "strike": float(strike),
            "right": right,
            "expiry": expiry,
            "volume": float(volume),
        }
    if not frame_reason and not contracts:
        frame_reason = "EMPTY_CONTRACT_UNIVERSE"
    return {
        "ts": float(ts.timestamp()),
        "spot": finite_number_or_none(spot),
        "contracts": contracts,
        "status": "invalid" if frame_reason else "ready",
        "reason": frame_reason or "FRAME_READY",
        "invalid_contract_key": invalid_contract_key,
    }


def _compare_live_option_volume_frames(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    frame_contracts: dict[str, Mapping[str, Any]] = {}
    for frame_name, frame in (("baseline", baseline), ("current", current)):
        if str(frame.get("status") or "") == "invalid":
            return {
                "status": "invalid",
                "reason": str(frame.get("reason") or "INVALID_VOLUME_FRAME"),
                "invalid_frame": frame_name,
                "invalid_contract_key": str(frame.get("invalid_contract_key") or ""),
                "contract_deltas": {},
            }
        contracts = frame.get("contracts")
        if not isinstance(contracts, dict) or not contracts:
            return {
                "status": "invalid",
                "reason": "EMPTY_CONTRACT_UNIVERSE",
                "invalid_frame": frame_name,
                "invalid_contract_key": "",
                "contract_deltas": {},
            }
        for key, contract in contracts.items():
            contract_id = (
                parse_exact_positive_decimal_provider_id(key[4:])
                if isinstance(key, str) and key.startswith("con:")
                else 0
            )
            if not isinstance(contract, Mapping) or contract_id <= 0:
                return {
                    "status": "invalid",
                    "reason": "INVALID_CONTRACT_ROW",
                    "invalid_frame": frame_name,
                    "invalid_contract_key": str(key),
                    "contract_deltas": {},
                }
            raw_volume = contract.get("volume")
            volume = (
                finite_number_or_none(raw_volume)
                if not isinstance(raw_volume, bool) and isinstance(raw_volume, (int, float))
                else None
            )
            strike = contract.get("strike")
            expiry = contract.get("expiry")
            if (
                isinstance(strike, bool)
                or not isinstance(strike, (int, float))
                or finite_number_or_none(strike) is None
                or contract.get("right") not in {"C", "P"}
                or not isinstance(expiry, str)
                or not expiry
                or expiry != expiry.strip()
            ):
                return {
                    "status": "invalid",
                    "reason": "INVALID_CONTRACT_IDENTITY",
                    "invalid_frame": frame_name,
                    "invalid_contract_key": str(key),
                    "contract_deltas": {},
                }
            if volume is None or volume < 0:
                return {
                    "status": "invalid",
                    "reason": "VOLUME_UNAVAILABLE" if raw_volume is None else "INVALID_VOLUME",
                    "invalid_frame": frame_name,
                    "invalid_contract_key": str(key),
                    "contract_deltas": {},
                }
        frame_contracts[frame_name] = contracts

    baseline_contracts = frame_contracts["baseline"]
    current_contracts = frame_contracts["current"]
    if set(baseline_contracts) != set(current_contracts):
        return {
            "status": "invalid",
            "reason": "CONTRACT_UNIVERSE_CHANGED",
            "invalid_frame": "comparison",
            "invalid_contract_key": "",
            "contract_deltas": {},
        }

    contract_deltas: dict[str, float] = {}
    for key in current_contracts:
        prior = baseline_contracts[key]
        latest = current_contracts[key]
        if (
            finite_number_or_none(prior.get("strike"))
            != finite_number_or_none(latest.get("strike"))
            or prior.get("right") != latest.get("right")
            or prior.get("expiry") != latest.get("expiry")
        ):
            return {
                "status": "invalid",
                "reason": "CONTRACT_IDENTITY_CHANGED",
                "invalid_frame": "comparison",
                "invalid_contract_key": str(key),
                "contract_deltas": {},
            }
        prior_volume = float(prior["volume"])
        current_volume = float(latest["volume"])
        if current_volume < prior_volume:
            return {
                "status": "invalid",
                "reason": "CONTRACT_VOLUME_RESET",
                "invalid_frame": "comparison",
                "invalid_contract_key": str(key),
                "contract_deltas": {},
            }
        contract_deltas[str(key)] = current_volume - prior_volume
    return {
        "status": "ready",
        "reason": "CONTRACT_UNIVERSE_MATCHED",
        "invalid_frame": "",
        "invalid_contract_key": "",
        "contract_deltas": contract_deltas,
    }


def _live_option_volume_baseline(
    frames: Sequence[Mapping[str, Any]],
    current_ts: float,
) -> Mapping[str, Any] | None:
    target_ts = current_ts - _LIVE_GEX_OPTION_VOLUME_EVENT_SECONDS
    candidates = [frame for frame in frames if float(frame["ts"]) <= target_ts]
    if not candidates:
        return None
    return candidates[-1]


def _live_option_volume_interval_rates_by_strike(
    frames: Sequence[Mapping[str, Any]],
    *,
    current_ts: float,
) -> dict[float, list[float]]:
    rates_by_strike: dict[float, list[float]] = {}
    previous: Mapping[str, Any] | None = None
    for frame in frames:
        if float(frame["ts"]) >= current_ts:
            break
        if previous is None:
            previous = frame
            continue
        comparison = _compare_live_option_volume_frames(previous, frame)
        if comparison["status"] != "ready":
            previous = frame
            continue
        current_contracts = (
            frame.get("contracts") if isinstance(frame.get("contracts"), dict) else {}
        )
        contract_deltas = comparison["contract_deltas"]
        interval_totals: dict[float, float] = {}
        for key, current in current_contracts.items():
            if not isinstance(current, dict):
                continue
            strike = finite_number_or_none(current.get("strike"))
            if strike is None:
                continue
            strike = float(strike)
            interval_totals[strike] = interval_totals.get(strike, 0.0) + float(
                contract_deltas[str(key)]
            )
        duration = float(frame["ts"]) - float(previous["ts"])
        if duration > 0:
            for strike, total in interval_totals.items():
                if total > 0:
                    rates_by_strike.setdefault(strike, []).append(total / duration)
        previous = frame
    return rates_by_strike


def _live_option_volume_activity_is_material(
    total_delta: float,
    turnover_delta: float | None,
    acceleration: float | None,
) -> bool:
    if total_delta >= _LIVE_GEX_PROCESS_MIN_VOLUME_DELTA:
        return True
    if turnover_delta is not None and turnover_delta >= _LIVE_GEX_PROCESS_MIN_TURNOVER:
        return True
    return bool(
        total_delta >= 2.0
        and acceleration is not None
        and acceleration >= _LIVE_GEX_PROCESS_ACCELERATION_ACTIVE
    )


def _live_option_volume_event_state(
    total_delta: float,
    event_bias: float | None,
    turnover_delta: float | None,
    acceleration: float | None,
    *,
    material: bool,
) -> str:
    if not material:
        return "quiet"
    if turnover_delta is not None and turnover_delta >= 0.10:
        return "high_turnover"
    if acceleration is not None and acceleration >= 2.0:
        return "accelerating"
    if event_bias is not None and event_bias >= 0.45:
        return "call_participation"
    if event_bias is not None and event_bias <= -0.45:
        return "put_participation"
    return "two_sided"


def _live_strike_step_from_strikes(strikes: Sequence[Mapping[str, Any]]) -> float:
    prices = sorted(
        {
            float(price)
            for row in strikes
            if (price := finite_number_or_none(row.get("strike"))) is not None
        }
    )
    diffs = [b - a for a, b in zip(prices, prices[1:], strict=False) if b > a]
    return min(diffs) if diffs else 1.0


def _live_option_volume_interaction(
    *,
    strike: float,
    total_delta: float,
    event_bias: float | None,
    turnover_delta: float | None,
    acceleration: float | None,
    abs_gex: float,
    total_oi: float,
    current_spot: float | None,
    previous_spot: float | None,
    strike_step: float,
    material: bool,
) -> tuple[str, str]:
    has_structure = abs(abs_gex) > 0 or total_oi > 0
    if not has_structure or not material:
        return "", ""
    if current_spot is None:
        return "activity_away_from_level", ""

    distance = strike - current_spot
    near_width = max(abs(strike_step) * 0.55, abs(current_spot) * 0.0002, 0.25)
    near = abs(distance) <= near_width
    elevated_activity = (
        turnover_delta is not None and turnover_delta >= _LIVE_GEX_PROCESS_TURNOVER_ACTIVE
    ) or (acceleration is not None and acceleration >= _LIVE_GEX_PROCESS_ACCELERATION_ACTIVE)
    high_turnover = turnover_delta is not None and turnover_delta >= _LIVE_GEX_PROCESS_TURNOVER_HIGH
    if high_turnover and near and event_bias is not None and abs(event_bias) <= 0.25:
        return "two_sided_activity_near_level", ""

    if previous_spot is not None:
        crossed_up = previous_spot < strike <= current_spot
        crossed_down = previous_spot > strike >= current_spot
        crossed = crossed_up or crossed_down
        previous_distance = strike - previous_spot
        previous_near = abs(previous_distance) <= near_width
        moving_toward = abs(distance) < abs(previous_distance)
        moving_away = previous_near and abs(distance) > abs(previous_distance) + max(
            strike_step * 0.08, 0.05
        )
        if crossed and elevated_activity:
            return "price_crossed_with_activity", "up" if crossed_up else "down"
        if moving_away and elevated_activity:
            return "spot_moved_away_with_activity", ""
        if (
            near
            and elevated_activity
            and abs(current_spot - previous_spot) <= max(strike_step * 0.12, 0.05)
        ):
            return "price_stationary_near_activity", ""
        if near and moving_toward and elevated_activity:
            return "price_approaching_with_activity", ""

    if near:
        return "price_near_level", ""
    return "activity_away_from_level", ""


def _live_trend_frame(
    strikes: Sequence[Mapping[str, Any]],
    levels: Mapping[str, Any],
    ts: datetime,
) -> dict[str, Any]:
    rows: dict[str, dict[str, float]] = {}
    for row in strikes:
        strike = finite_number_or_none(row.get("strike"))
        if strike is None:
            continue
        rows[_live_trend_price_key(strike)] = {
            "strike": float(strike),
            "call": abs(float(row["call_gex"])),
            "put": abs(float(row["put_gex"])),
            "net": float(row["net_gex"]),
            "abs": abs(float(row["abs_gex"])),
        }
    return {
        "ts": float(ts.timestamp()),
        "rows": rows,
        "call_wall": finite_number_or_none(levels.get("call_wall")),
        "put_wall": finite_number_or_none(levels.get("put_wall")),
    }


def _live_trend_baseline(
    frames: Sequence[Mapping[str, Any]], current_ts: float
) -> Mapping[str, Any] | None:
    target_ts = current_ts - _LIVE_GEX_TREND_MIN_SECONDS
    candidates = [frame for frame in frames if float(frame["ts"]) <= target_ts]
    if not candidates:
        return None
    return candidates[-1]


def _live_side_motion(
    side: str, previous: float, current: float, window_seconds: float
) -> dict[str, Any] | None:
    if not (math.isfinite(previous) and math.isfinite(current)) or (previous <= 0 and current <= 0):
        return None
    scale = max(abs(previous), abs(current), _LIVE_GEX_TREND_MIN_ABS_GEX)
    delta = current - previous
    if abs(delta) < scale * _LIVE_GEX_TREND_MIN_REL_CHANGE:
        return None
    pct = (delta / abs(previous) * 100.0) if abs(previous) > 1e-9 else None
    return {
        "type": "strengthening" if delta > 0 else "weakening",
        "side": side,
        "from": round(previous, 4),
        "to": round(current, 4),
        "delta": round(delta, 4),
        "delta_pct": round(pct, 2) if pct is not None and math.isfinite(pct) else None,
        "window_seconds": round(window_seconds, 1),
    }


def _live_net_motion(
    previous: float, current: float, window_seconds: float
) -> dict[str, Any] | None:
    if not (math.isfinite(previous) and math.isfinite(current)):
        return None
    scale = max(abs(previous), abs(current), _LIVE_GEX_TREND_MIN_ABS_GEX)
    if previous < 0 < current and max(abs(previous), abs(current)) >= _LIVE_GEX_TREND_MIN_ABS_GEX:
        motion_type = "sign_flip_positive"
    elif previous > 0 > current and max(abs(previous), abs(current)) >= _LIVE_GEX_TREND_MIN_ABS_GEX:
        motion_type = "sign_flip_negative"
    else:
        delta = abs(current) - abs(previous)
        if abs(delta) < scale * _LIVE_GEX_NET_TREND_MIN_REL_CHANGE:
            return None
        motion_type = "strengthening" if delta > 0 else "weakening"
    delta = current - previous
    pct = (delta / abs(previous) * 100.0) if abs(previous) > 1e-9 else None
    return {
        "type": motion_type,
        "side": "net",
        "from": round(previous, 4),
        "to": round(current, 4),
        "delta": round(delta, 4),
        "delta_pct": round(pct, 2) if pct is not None and math.isfinite(pct) else None,
        "window_seconds": round(window_seconds, 1),
    }


def _live_roll_motion(
    side: str, previous_price: Any, current_price: Any, window_seconds: float
) -> dict[str, Any] | None:
    previous = finite_number_or_none(previous_price)
    current = finite_number_or_none(current_price)
    if previous is None or current is None or abs(current - previous) <= 1e-9:
        return None
    return {
        "type": "roll_up" if current > previous else "roll_down",
        "side": side,
        "from_price": round(previous, 4),
        "to_price": round(current, 4),
        "window_seconds": round(window_seconds, 1),
    }


def _attach_live_motion_to_rows(rows: Any, motion_by_key: Mapping[str, dict[str, Any]]) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "price" not in row:
            raise ValueError("GEX live motion rows require canonical level price")
        key = _live_trend_price_key(row["price"])
        if not key:
            raise ValueError("GEX live motion level price must be finite")
        motion = motion_by_key.get(key)
        if motion:
            row["motion"] = deepcopy(motion)
        elif "motion" in row:
            row.pop("motion", None)
