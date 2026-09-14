from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any


OPTION_DRIFT_MAX_INTERVAL_SECONDS = 15 * 60
OPTION_DRIFT_STALE_SECONDS = 15 * 60
OPTION_DRIFT_MIN_INTERVALS = 3
OPTION_DRIFT_FULL_CONFIDENCE_INTERVALS = 6
OPTION_DRIFT_MIN_VOLUME_COVERAGE = 0.75
OPTION_DRIFT_MAX_CORRECTED_CONTRACT_SHARE = 0.05
OPTION_DRIFT_MAX_CORRECTED_VOLUME_SHARE = 0.005
OPTION_DRIFT_MIN_CORRECTED_VOLUME_ALLOWANCE = 2.0

_TRUSTED_OPTION_PRICE_SOURCES = frozenset(
    {
        "bid_ask_mid",
        "ibkr_market_price",
        "last",
    }
)
_ENTITLEMENT_CONFIDENCE = {
    "live": 1.0,
    "frozen": 0.80,
    "delayed": 0.65,
    "delayed_frozen": 0.55,
}


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _contract_key(row: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        row.get("con_id"),
        row.get("expiry"),
        row.get("trading_class"),
        row.get("exchange"),
        row.get("multiplier"),
        row.get("right"),
    )


def _contracts(payload: Mapping[str, Any]) -> dict[tuple[object, ...], Mapping[str, Any]]:
    raw = payload.get("raw")
    rows = raw.get("contracts") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return {}
    keyed = {_contract_key(row): row for row in rows}
    return keyed if len(keyed) == len(rows) else {}


def _scope_key(payload: Mapping[str, Any]) -> tuple[object, ...] | None:
    scope = payload.get("comparison_scope")
    if not isinstance(scope, Mapping):
        return None
    try:
        return (
            scope.get("capture_mode"),
            scope.get("strike_count"),
            tuple(scope.get("strike_ladder") or ()),
            tuple(scope.get("contract_con_ids") or ()),
            tuple(scope.get("expiries") or ()),
            scope.get("futures_options"),
            tuple(
                (
                    item.get("expiry"),
                    item.get("trading_class"),
                    item.get("exchange"),
                    item.get("multiplier"),
                )
                for item in scope.get("series") or ()
                if isinstance(item, Mapping)
            ),
            scope.get("risk_free_rate"),
            scope.get("dividend_yield"),
            scope.get("market_data_entitlement"),
        )
    except TypeError:
        return None


def _positive_quote_spread(row: Mapping[str, Any]) -> float | None:
    bid = _finite(row.get("bid"))
    ask = _finite(row.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    return ask - bid


def _interval_projection(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    previous_contracts: Mapping[tuple[object, ...], Mapping[str, Any]],
    current_contracts: Mapping[tuple[object, ...], Mapping[str, Any]],
) -> dict[str, float] | None:
    if not previous_contracts or set(previous_contracts) != set(current_contracts):
        return None
    previous_spot = _finite(previous.get("spot"))
    current_spot = _finite(current.get("spot"))
    if previous_spot is None or current_spot is None or previous_spot <= 0 or current_spot <= 0:
        return None
    spot_change = current_spot - previous_spot
    totals = {
        "call_drift": 0.0,
        "put_drift": 0.0,
        "call_gross": 0.0,
        "put_gross": 0.0,
        "total_volume": 0.0,
        "covered_volume": 0.0,
        "previous_volume": 0.0,
        "corrected_volume": 0.0,
        "corrected_contracts": 0.0,
    }
    for key, current_row in current_contracts.items():
        previous_row = previous_contracts[key]
        previous_volume = _finite(previous_row.get("volume"))
        current_volume = _finite(current_row.get("volume"))
        if previous_volume is None or current_volume is None:
            return None
        totals["previous_volume"] += previous_volume
        volume_delta = current_volume - previous_volume
        if volume_delta < 0:
            totals["corrected_volume"] += abs(volume_delta)
            totals["corrected_contracts"] += 1.0
            continue
        if volume_delta == 0:
            continue
        totals["total_volume"] += volume_delta
        previous_source = previous_row.get("reference_option_price_source")
        current_source = current_row.get("reference_option_price_source")
        previous_price = _finite(previous_row.get("reference_option_price"))
        current_price = _finite(current_row.get("reference_option_price"))
        previous_delta = _finite(previous_row.get("delta"))
        current_delta = _finite(current_row.get("delta"))
        multiplier = _finite(current_row.get("multiplier"))
        right = current_row.get("right")
        if (
            previous_source != current_source
            or current_source not in _TRUSTED_OPTION_PRICE_SOURCES
            or previous_price is None
            or current_price is None
            or previous_price <= 0
            or current_price <= 0
            or previous_delta is None
            or current_delta is None
            or multiplier is None
            or multiplier <= 0
            or right not in {"C", "P"}
        ):
            continue
        if right == "C" and not (0.0 <= previous_delta <= 1.0 and 0.0 <= current_delta <= 1.0):
            continue
        if right == "P" and not (-1.0 <= previous_delta <= 0.0 and -1.0 <= current_delta <= 0.0):
            continue
        average_price = (previous_price + current_price) / 2.0
        average_delta = (previous_delta + current_delta) / 2.0
        residual = (current_price - previous_price) - average_delta * spot_change
        spreads = tuple(
            spread
            for spread in (
                _positive_quote_spread(previous_row),
                _positive_quote_spread(current_row),
            )
            if spread is not None
        )
        noise_scale = max((*spreads, average_price * 0.02, 0.01))
        conviction = max(-1.0, min(1.0, residual / noise_scale))
        gross = volume_delta * average_price * multiplier
        side = "call" if right == "C" else "put"
        totals[f"{side}_gross"] += gross
        totals[f"{side}_drift"] += gross * conviction
        totals["covered_volume"] += volume_delta
    if totals["total_volume"] <= 0:
        coverage = 1.0
    else:
        coverage = totals["covered_volume"] / totals["total_volume"]
    if coverage < OPTION_DRIFT_MIN_VOLUME_COVERAGE:
        return None
    corrected_contract_limit = max(
        1,
        math.floor(len(current_contracts) * OPTION_DRIFT_MAX_CORRECTED_CONTRACT_SHARE),
    )
    corrected_volume_limit = max(
        OPTION_DRIFT_MIN_CORRECTED_VOLUME_ALLOWANCE,
        totals["previous_volume"] * OPTION_DRIFT_MAX_CORRECTED_VOLUME_SHARE,
    )
    if (
        totals["corrected_contracts"] > corrected_contract_limit
        or totals["corrected_volume"] > corrected_volume_limit
    ):
        return None
    counter_volume_coverage = (
        max(0.0, 1.0 - totals["corrected_volume"] / totals["previous_volume"])
        if totals["previous_volume"] > 0
        else 1.0
    )
    totals["coverage"] = coverage
    totals["counter_volume_coverage"] = counter_volume_coverage
    totals["spot"] = current_spot
    return totals


def _price_efficiency(spots: Sequence[float]) -> float:
    if len(spots) < 2:
        return 0.0
    travelled = sum(abs(current - previous) for previous, current in zip(spots, spots[1:]))
    if travelled <= 0:
        return 0.0
    return max(0.0, min(1.0, abs(spots[-1] - spots[0]) / travelled))


def calculate_option_drift(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Project a display-only flow heuristic from canonical persisted GEX snapshots."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Option Drift now must be timezone-aware")
    now_utc = now.astimezone(UTC)
    prepared = sorted(
        (
            (captured_at, snapshot)
            for snapshot in snapshots
            if (captured_at := _timestamp(snapshot.get("captured_at"))) is not None
        ),
        key=lambda item: item[0],
    )
    segment: list[tuple[datetime, Mapping[str, Any], dict[str, float]]] = []
    rejected_intervals = 0
    scopes = [_scope_key(snapshot) for _, snapshot in prepared]
    last_indexed: tuple[int, dict[tuple[object, ...], Mapping[str, Any]]] | None = None
    for index, ((previous_at, previous), (current_at, current)) in enumerate(
        zip(prepared, prepared[1:])
    ):
        continuous = bool(
            0 < (current_at - previous_at).total_seconds() <= OPTION_DRIFT_MAX_INTERVAL_SECONDS
            and scopes[index] is not None
            and scopes[index] == scopes[index + 1]
        )
        projection = None
        if continuous:
            previous_contracts = (
                last_indexed[1]
                if last_indexed is not None and last_indexed[0] == index
                else _contracts(previous)
            )
            current_contracts = _contracts(current)
            # Retain only the adjacent index, not a second day-history cache.
            last_indexed = (index + 1, current_contracts)
            projection = _interval_projection(
                previous,
                current,
                previous_contracts=previous_contracts,
                current_contracts=current_contracts,
            )
        if projection is None:
            rejected_intervals += 1
            segment = []
            continue
        segment.append((current_at, current, projection))

    call_drift = 0.0
    put_drift = 0.0
    call_gross = 0.0
    put_gross = 0.0
    coverage_values: list[float] = []
    counter_coverage_values: list[float] = []
    corrected_volume = 0.0
    corrected_contract_observations = 0
    series: list[dict[str, Any]] = []
    balances: list[float] = []
    spots: list[float] = []
    if segment:
        first_current = segment[0][1]
        first_contract_at = _timestamp(first_current.get("captured_at"))
        previous_candidates = [
            item
            for item in prepared
            if first_contract_at is not None and item[0] < first_contract_at
        ]
        if previous_candidates:
            initial_spot = _finite(previous_candidates[-1][1].get("spot"))
            if initial_spot is not None:
                spots.append(initial_spot)
    for captured_at, _snapshot, projection in segment:
        call_drift += projection["call_drift"]
        put_drift += projection["put_drift"]
        call_gross += projection["call_gross"]
        put_gross += projection["put_gross"]
        coverage_values.append(projection["coverage"])
        counter_coverage_values.append(projection["counter_volume_coverage"])
        corrected_volume += projection["corrected_volume"]
        corrected_contract_observations += int(projection["corrected_contracts"])
        spots.append(projection["spot"])
        balance = call_drift - put_drift
        balances.append(balance)
        series.append(
            {
                "ts": captured_at.isoformat(),
                "spot": round(projection["spot"], 8),
                "call_drift": round(call_drift, 2),
                "put_drift": round(put_drift, 2),
                "balance": round(balance, 2),
            }
        )

    gross_premium = call_gross + put_gross
    balance = call_drift - put_drift
    balance_ratio = balance / gross_premium if gross_premium > 0 else 0.0
    price_efficiency = _price_efficiency(spots)
    two_sided_ratio = 2.0 * min(call_gross, put_gross) / gross_premium if gross_premium > 0 else 0.0
    signs: list[int] = []
    for index, value in enumerate(balances):
        cumulative_gross = sum(
            item[2]["call_gross"] + item[2]["put_gross"] for item in segment[: index + 1]
        )
        deadband = cumulative_gross * 0.05
        sign = 1 if value > deadband else -1 if value < -deadband else 0
        if sign:
            signs.append(sign)
    crossings = sum(previous != current for previous, current in zip(signs, signs[1:]))
    recent_cross = bool(len(signs) >= 2 and signs[-1] != signs[-2])
    proximity = 1.0 - min(1.0, abs(balance_ratio))
    cross_factor = min(1.0, crossings / 2.0)
    chop_score = 100.0 * (
        0.35 * proximity
        + 0.25 * cross_factor
        + 0.25 * (1.0 - price_efficiency)
        + 0.15 * two_sided_ratio
    )
    latest_at = prepared[-1][0] if prepared else None
    latest_age_seconds = (
        max(0.0, (now_utc - latest_at).total_seconds()) if latest_at is not None else None
    )
    latest_payload = prepared[-1][1] if prepared else {}
    entitlement = str(latest_payload.get("market_data_entitlement") or "unknown")
    average_coverage = sum(coverage_values) / len(coverage_values) if coverage_values else 0.0
    average_counter_coverage = (
        sum(counter_coverage_values) / len(counter_coverage_values)
        if counter_coverage_values
        else 0.0
    )
    confidence = (
        min(1.0, len(segment) / OPTION_DRIFT_FULL_CONFIDENCE_INTERVALS)
        * average_coverage
        * average_counter_coverage
        * _ENTITLEMENT_CONFIDENCE.get(entitlement, 0.0)
    )
    if latest_age_seconds is not None and latest_age_seconds > OPTION_DRIFT_STALE_SECONDS:
        state_code = "STALE"
    elif len(segment) < OPTION_DRIFT_MIN_INTERVALS or gross_premium <= 0 or confidence < 0.30:
        state_code = "BASELINING"
    elif recent_cross or chop_score >= 70.0:
        state_code = "CONFLICT"
    elif abs(balance_ratio) <= 0.12:
        state_code = "BALANCE"
    elif chop_score >= 55.0:
        state_code = "TRANSITION"
    elif balance_ratio >= 0.20:
        state_code = "CALL_CONTROL"
    elif balance_ratio <= -0.20:
        state_code = "PUT_CONTROL"
    else:
        state_code = "TRANSITION"
    return {
        "as_of": latest_at.isoformat() if latest_at is not None else None,
        "state_code": state_code,
        "advisory_only": True,
        "decision_eligible": False,
        "metrics": {
            "call_drift": round(call_drift, 2),
            "put_drift": round(put_drift, 2),
            "balance": round(balance, 2),
            "call_gross_premium": round(call_gross, 2),
            "put_gross_premium": round(put_gross, 2),
            "gross_premium": round(gross_premium, 2),
            "balance_ratio": round(balance_ratio, 4),
            "chop_score": round(chop_score, 1),
            "confidence": round(confidence, 4),
            "price_efficiency": round(price_efficiency, 4),
            "two_sided_ratio": round(two_sided_ratio, 4),
            "covered_volume_ratio": round(average_coverage, 4),
            "counter_volume_coverage": round(average_counter_coverage, 4),
            "crossings": crossings,
            "recent_cross": recent_cross,
        },
        "quality": {
            "snapshots": len(prepared),
            "accepted_intervals": len(segment),
            "rejected_intervals": rejected_intervals,
            "corrected_volume": round(corrected_volume, 2),
            "corrected_contract_observations": corrected_contract_observations,
            "latest_age_seconds": (
                round(latest_age_seconds, 1) if latest_age_seconds is not None else None
            ),
            "market_data_entitlement": entitlement,
            "heuristic_basis": "volume_delta_x_delta_adjusted_option_price_residual",
        },
        "series": series,
    }
