"""Pure Option Point expiry, strike, and candidate-ranking policy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.option_target_contract import option_target_dte_for_expiry
from aef_terminal.data.gex.option_target_valuation import _option_reference_price
from aef_terminal.data.gex.utils import (
    _expiration_date,
    finite_number_or_none,
    parse_gex_timestamp,
    _strike_step,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text


def _option_target_premium_cap(
    instrument_id: str,
    premium_caps: Mapping[str, Any] | None = None,
) -> float | None:
    caps: dict[str, float] = {}
    if isinstance(premium_caps, Mapping):
        for key, value in premium_caps.items():
            finite = finite_number_or_none(value)
            if isinstance(key, str) and key and finite is not None and finite > 0:
                caps[key] = finite
    return caps.get(require_exact_identity_text(instrument_id, field="instrument_id"))


def _option_target_profile(
    instrument_id: str,
    mode: str,
    premium_caps: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cap = _option_target_premium_cap(instrument_id, premium_caps)
    profiles = {
        "conservative": {
            "delta_min": 0.14,
            "delta_max": 0.32,
            "delta_target": 0.24,
            "premium_factor": 0.68,
        },
        "normal": {
            "delta_min": 0.20,
            "delta_max": 0.42,
            "delta_target": 0.32,
            "premium_factor": 0.82,
        },
        "aggressive": {
            "delta_min": 0.28,
            "delta_max": 0.50,
            "delta_target": 0.40,
            "premium_factor": 0.96,
        },
    }
    if mode not in profiles:
        raise ValueError("option target mode must be conservative, normal or aggressive")
    profile = dict(profiles[mode])
    profile["premium_cap"] = cap if cap is not None else 0.0
    profile["mode"] = mode
    return profile


def _eligible_option_target_expiries(
    contracts: list[dict[str, Any]],
    *,
    right: str,
    target_dte: str,
    valuation_now: datetime,
    scenario_valuation_at: datetime,
) -> tuple[list[str], list[str], list[str]]:
    provider_expiry_ladder = sorted({str(row.get("expiry") or "") for row in contracts})
    provider_expiry_dtes: dict[str, str] = {}
    for row in contracts:
        expiry_code = str(row.get("expiry") or "")
        expiry_dte = option_target_dte_for_expiry(
            row.get("expiry_at"),
            valuation_now=valuation_now,
        )
        if expiry_dte is not None:
            provider_expiry_dtes[expiry_code] = expiry_dte
    available_expiries = sorted(
        {str(row.get("expiry") or "") for row in contracts if row.get("right") == right}
    )
    eligible_expiries = [
        expiry_code
        for expiry_code in provider_expiry_ladder
        if provider_expiry_dtes.get(expiry_code) == target_dte
        and any(
            str(row.get("expiry") or "") == expiry_code
            and (contract_expiry_at := parse_gex_timestamp(row.get("expiry_at"))) is not None
            and contract_expiry_at.astimezone(UTC) > scenario_valuation_at.astimezone(UTC)
            for row in contracts
        )
    ]
    return available_expiries, provider_expiry_ladder, eligible_expiries


def _option_target_expiry_rows(
    contracts: list[dict[str, Any]],
    *,
    right: str,
    target_dte: str,
    target_expiry_code: str,
    scenario_valuation_at: datetime,
    target: float,
    preferred_strike: float | None,
    preferred_expiry: str,
) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in contracts
        if row.get("right") == right
        and _expiration_date(str(row.get("expiry") or "")) is not None
        and str(row.get("expiry") or "") == target_expiry_code
        and finite_number_or_none(row.get("strike")) is not None
        and (candidate_expiry_at := parse_gex_timestamp(row.get("expiry_at"))) is not None
        and candidate_expiry_at.astimezone(UTC) > scenario_valuation_at.astimezone(UTC)
    ]
    if not candidates:
        return []
    nearest_expiry = min(_expiration_date(str(row.get("expiry") or "")) for row in candidates)
    expiry_rows = [
        row
        for row in candidates
        if _expiration_date(str(row.get("expiry") or "")) == nearest_expiry
    ]
    if preferred_strike is None and not preferred_expiry and target_dte == "1dte":
        step = _strike_step(expiry_rows)
        min_distance = max(step * 2.0, 0.0)
        otm_rows = [
            row
            for row in expiry_rows
            if (strike_value := finite_number_or_none(row.get("strike"))) is not None
            and (
                (right == "C" and strike_value >= target + min_distance)
                or (right == "P" and strike_value <= target - min_distance)
            )
        ]
        if otm_rows:
            return otm_rows
    return expiry_rows


def _rank_option_target_candidates(
    expiry_rows: list[dict[str, Any]],
    *,
    profile: Mapping[str, Any],
    premium_cap: float,
    target_premium: float,
) -> tuple[
    list[tuple[float, dict[str, Any], float]],
    list[tuple[float, dict[str, Any], float, str]],
]:
    scored_rows: list[tuple[float, dict[str, Any], float]] = []
    rejected_rows: list[tuple[float, dict[str, Any], float, str]] = []
    for candidate in expiry_rows:
        reference = _option_reference_price(candidate)
        if reference is None or reference <= 0:
            continue
        effective_delta = finite_number_or_none(candidate.get("delta"))
        effective_gamma = finite_number_or_none(candidate.get("gamma"))
        if effective_delta is None:
            premium_penalty = abs(reference - target_premium) / max(premium_cap, 0.01)
            rank = -premium_penalty * 0.55
            rejected_rows.append(
                (
                    rank,
                    candidate,
                    reference,
                    "above_cap" if reference > premium_cap else "market_price_only",
                )
            )
            continue
        abs_delta = abs(effective_delta)
        in_delta_band = profile["delta_min"] <= abs_delta <= profile["delta_max"]
        delta_penalty = abs(abs_delta - profile["delta_target"])
        premium_penalty = abs(reference - target_premium) / max(premium_cap, 0.01)
        gamma_bonus = min(effective_gamma * 12.0, 0.20) if effective_gamma is not None else 0.0
        rank = (
            (0.65 if in_delta_band else 0.0)
            - delta_penalty * 0.90
            - premium_penalty * 0.55
            + gamma_bonus
        )
        if reference > premium_cap:
            rejected_rows.append((rank, candidate, reference, "above_cap"))
            continue
        scored_rows.append((rank, candidate, reference))
    return scored_rows, rejected_rows
