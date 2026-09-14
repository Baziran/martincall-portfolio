from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import (
    GEX_OPTION_ACTIVITY_MIN_BASELINE_SAMPLES,
    GEX_SCHEDULER_INTERVAL_MINUTES,
)
from aef_terminal.data.gex.contracts import (
    require_exact_gex_capture_lane,
    require_gex_comparison_scope,
    require_gex_option_activity,
    require_gex_option_universe_expiry,
)
from aef_terminal.domain import Bar, Direction, SignalCandidate
from aef_terminal.engine.common import (
    bar_index_at_or_before,
    parse_aware_utc_ts,
)


def _exact_option_flow_latest(
    history: Sequence[dict[str, Any]],
    *,
    provider_symbol: str,
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any] | None:
    if not history:
        return None
    if any(
        not isinstance(row, dict)
        or row.get("provider_symbol") != provider_symbol
        or row.get("instrument_id") != instrument_id
        or row.get("route_fingerprint") != route_fingerprint
        for row in history
    ):
        raise ValueError("Option-flow history identity must match the exact analysis route")
    return history[-1]


def _option_flow_lane(
    latest: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    try:
        _, capture_mode = require_exact_gex_capture_lane(
            source=latest.get("source"),
            capture_mode=latest.get("capture_mode"),
        )
        comparison_scope = require_gex_comparison_scope(
            latest.get("comparison_scope"),
            capture_mode=capture_mode,
        )
    except ValueError:
        return None
    if (
        capture_mode != "request"
        or comparison_scope["market_data_entitlement"] != "live"
        or latest.get("decision_authoritative") is not True
        or str(latest.get("option_activity_status") or "") != "READY"
    ):
        return None
    return capture_mode, comparison_scope


def _option_flow_captured_at(
    latest: dict[str, Any],
    *,
    analysis_as_of_utc: datetime,
) -> datetime | None:
    captured_at = parse_aware_utc_ts(latest.get("captured_at"))
    if captured_at is None:
        return None
    if analysis_as_of_utc.tzinfo is None or analysis_as_of_utc.utcoffset() is None:
        raise ValueError("analysis_as_of_utc must be timezone-aware")
    now_utc = analysis_as_of_utc.astimezone(UTC)
    try:
        expires_at = require_gex_option_universe_expiry(
            latest.get("option_universe_expires_at"),
            captured_at=captured_at,
            allow_unknown=True,
        )
    except ValueError:
        return None
    age_seconds = (now_utc - captured_at).total_seconds()
    if (
        expires_at is None
        or expires_at <= now_utc
        or age_seconds < 0
        or age_seconds > GEX_SCHEDULER_INTERVAL_MINUTES * 3 * 60
    ):
        return None
    return captured_at


def _option_flow_activity(
    latest: dict[str, Any],
    comparison_scope: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        activity = require_gex_option_activity(
            latest.get("option_activity"),
            expected_status="READY",
        )
    except ValueError:
        return None
    contract_universe = activity["contract_universe"]
    total = activity["total"]
    if (
        contract_universe["selected_strikes"] != comparison_scope["strike_count"]
        or contract_universe["selected_contracts"] != len(comparison_scope["contract_con_ids"])
        or activity["elapsed_seconds"] > GEX_SCHEDULER_INTERVAL_MINUTES * 3 * 60
        or total["rvol"] is None
        or total["baseline_samples"] < GEX_OPTION_ACTIVITY_MIN_BASELINE_SAMPLES
    ):
        return None
    return activity


def option_flow_context_from_gex_history(
    provider_symbol: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    history: Sequence[dict[str, Any]],
    analysis_as_of_utc: datetime,
) -> dict[str, Any] | None:
    latest = _exact_option_flow_latest(
        history,
        provider_symbol=provider_symbol,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )
    if latest is None:
        return None
    lane = _option_flow_lane(latest)
    if (
        lane is None
        or _option_flow_captured_at(
            latest,
            analysis_as_of_utc=analysis_as_of_utc,
        )
        is None
    ):
        return None
    capture_mode, comparison_scope = lane
    activity = _option_flow_activity(latest, comparison_scope)
    if activity is None:
        return None
    call = activity["call"]
    put = activity["put"]
    total = activity["total"]
    elapsed_seconds = activity["elapsed_seconds"]
    option_rvol = total["rvol"]
    baseline_samples = total["baseline_samples"]
    return {
        "call_volume_delta": call["volume_delta"],
        "put_volume_delta": put["volume_delta"],
        "call_rate_per_minute": call["rate_per_minute"],
        "put_rate_per_minute": put["rate_per_minute"],
        "option_rvol": option_rvol,
        "baseline_samples": baseline_samples,
        "elapsed_seconds": elapsed_seconds,
        "spot": latest.get("spot"),
        "source": "gex:selected-chain-volume-rate",
        "captured_at": latest.get("captured_at"),
        "provider_symbol": provider_symbol,
        "capture_mode": capture_mode,
        "comparison_scope": dict(comparison_scope),
        "quality": "READY",
        "decision_eligible": True,
    }


def _recent_vsa_fuel_direction(
    vsa_context: dict[str, Any],
    bars: Sequence[Bar],
    *,
    cooldown_bars: int = 3,
) -> tuple[Direction | None, str]:
    if not bars:
        return None, ""
    latest_index = len(bars) - 1
    for event in reversed(vsa_context.get("events") or []):
        if not isinstance(event, dict):
            continue
        code = str(event.get("code") or "").upper()
        if code not in {"FUEL_UP", "FUEL_DN"} and not bool(event.get("fuel")):
            continue
        event_ts = parse_aware_utc_ts(event.get("ts"))
        if event_ts is None:
            continue
        event_index = bar_index_at_or_before(bars, event_ts)
        if event_index is None:
            continue
        age = latest_index - event_index
        if age < 0 or age > max(int(cooldown_bars), 0):
            continue
        event_direction = str(event.get("direction") or "").lower()
        if event_direction not in {Direction.LONG.value, Direction.SHORT.value}:
            continue
        direction = Direction.LONG if event_direction == Direction.LONG.value else Direction.SHORT
        label = code or ("CLIMAX_UP" if direction == Direction.LONG else "CLIMAX_DN")
        return direction, f"VSA {label} {age} bar(s) ago"
    return None, ""


def apply_vsa_fuel_sfp_cooldown(
    candidates: Sequence[SignalCandidate],
    bars: Sequence[Bar],
    *,
    vsa_context: dict[str, Any] | None = None,
    cooldown_bars: int = 3,
) -> tuple[list[SignalCandidate], dict[str, Any] | None]:
    vsa_payload = vsa_context if isinstance(vsa_context, dict) else {}
    vsa_direction, reason = _recent_vsa_fuel_direction(
        vsa_payload,
        bars,
        cooldown_bars=cooldown_bars,
    )
    if vsa_direction is None:
        return list(candidates), None
    blocked_direction = Direction.SHORT if vsa_direction == Direction.LONG else Direction.LONG
    filtered: list[SignalCandidate] = []
    blocked: list[dict[str, Any]] = []
    for candidate in candidates:
        if (
            candidate.source == "sfp"
            and candidate.role == "liquidity_sweep"
            and candidate.direction == blocked_direction
        ):
            blocked.append(
                {
                    "name": candidate.name,
                    "direction": candidate.direction.value,
                    "score": round(float(candidate.score), 2),
                    "level": candidate.level,
                    "reason": candidate.reason,
                }
            )
            continue
        filtered.append(candidate)
    if not blocked:
        return filtered, None
    return filtered, {
        "active": True,
        "bars": max(int(cooldown_bars), 0),
        "vsa_fuel_direction": vsa_direction.value,
        "blocked_direction": blocked_direction.value,
        "reason": reason,
        "blocked": blocked,
    }
