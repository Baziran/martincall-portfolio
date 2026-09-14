from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from aef_terminal.data.gex.utils import finite_number_or_none, gex_kind_class

_OPTION_VOLUME_CURRENT_FIELDS = (
    "call_volume",
    "put_volume",
    "total_volume",
    "call_oi",
    "put_oi",
    "total_oi",
    "turnover",
    "rank",
)
_OPTION_VOLUME_EVENT_FIELDS = (
    "call_volume_delta",
    "put_volume_delta",
    "total_volume_delta",
    "bias",
    "call_participation",
    "put_participation",
    "turnover",
    "acceleration",
    "flow_per_point",
    "spot_move_points",
    "material",
    "interaction",
    "cross_direction",
    "state",
    "window_seconds",
    "source",
)

_GEX_LEVEL_FIELDS = frozenset(
    {
        "price",
        "kind",
        "kind_class",
        "strength",
        "power_class",
        "selection_rank",
        "net_gex",
        "call_gex",
        "put_gex",
        "abs_gex",
        "abs_flow_1pt",
        "distance_from_spot",
        "zone_half_width",
        "spot_side",
        "option_volume_context",
    }
)
_GEX_LEVEL_MOTION_VALUE_FIELDS = frozenset(
    {"type", "side", "from", "to", "delta", "delta_pct", "window_seconds"}
)
_GEX_LEVEL_MOTION_ROLL_FIELDS = frozenset(
    {"type", "side", "from_price", "to_price", "window_seconds"}
)
_GEX_EXPIRY_PROFILE_FIELDS = frozenset(
    {"expiry", "strike", "call_gex", "put_gex", "net_gex", "abs_gex"}
)
GEX_RAW_STRIKE_REDUNDANT_FIELDS = frozenset(
    {
        "call_flow_1pt",
        "put_flow_1pt",
        "net_flow_1pt",
        "abs_flow_1pt",
        "abs_flow_0_5pt",
        "abs_flow_2pt",
    }
)
_GEX_RAW_META_TIMESTAMP_FIELDS = frozenset(
    {"timestamp", "timestamp_ny", "timestamp_unix_ms", "captured_at"}
)


def gex_level_power_class(strength: float) -> str:
    """Classify the exact published four-decimal GEX strength."""

    canonical_strength = finite_number_or_none(strength)
    if canonical_strength is None or not 0 <= canonical_strength <= 1:
        raise ValueError("GEX level strength must be finite and between zero and one")
    return (
        "WEAK"
        if canonical_strength < 0.10
        else "EXTREME"
        if canonical_strength >= 0.85
        else "STRONG"
        if canonical_strength >= 0.55
        else "MEDIUM"
    )


def require_gex_expiry_profile(
    value: object,
) -> list[dict[str, Any]]:
    """Validate the one compact per-expiry GEX breakdown contract."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("GEX expiry profile must be a typed sequence")
    out: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, float]] = set()
    previous_key: tuple[str, float] | None = None
    for row in value:
        if not isinstance(row, Mapping) or set(row) != _GEX_EXPIRY_PROFILE_FIELDS:
            raise ValueError("GEX expiry profile row requires its exact fields")
        expiry = row.get("expiry")
        if not isinstance(expiry, str) or len(expiry) != 8 or not expiry.isdigit():
            raise ValueError("GEX expiry profile requires an exact YYYYMMDD expiry")
        try:
            datetime.strptime(expiry, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("GEX expiry profile requires an exact YYYYMMDD expiry") from exc
        numeric = {
            field: finite_number_or_none(row.get(field))
            for field in ("strike", "call_gex", "put_gex", "net_gex", "abs_gex")
        }
        if any(item is None for item in numeric.values()):
            raise ValueError("GEX expiry profile facts must be finite numbers")
        strike = numeric["strike"]
        call_gex = numeric["call_gex"]
        put_gex = numeric["put_gex"]
        net_gex = numeric["net_gex"]
        abs_gex = numeric["abs_gex"]
        assert None not in (strike, call_gex, put_gex, net_gex, abs_gex)
        if (
            strike <= 0
            or call_gex < 0
            or put_gex > 0
            or abs_gex <= 0
            or not math.isclose(
                net_gex,
                call_gex + put_gex,
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
            or not math.isclose(
                abs_gex,
                abs(call_gex) + abs(put_gex),
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        ):
            raise ValueError("GEX expiry profile facts are internally inconsistent")
        key = (expiry, strike)
        if key in seen_keys or (previous_key is not None and key <= previous_key):
            raise ValueError("GEX expiry profile rows must be unique and ordered")
        seen_keys.add(key)
        previous_key = key
        out.append(dict(row))
    return out


def gex_payload_strikes(payload: object) -> list[dict[str, Any]]:
    """Read authoritative aggregate strike rows from a canonical GEX payload."""

    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("raw")
    strikes = raw.get("strikes") if isinstance(raw, Mapping) else None
    if not isinstance(strikes, list) or any(not isinstance(row, Mapping) for row in strikes):
        return []
    return [dict(row) for row in strikes]


def gex_payload_strike_count(payload: object) -> int | None:
    """Read the exact acquisition strike count without coercion."""

    if not isinstance(payload, Mapping):
        return None
    request_meta = payload.get("request_meta")
    if not isinstance(request_meta, Mapping):
        return None
    value = request_meta.get("strike_count")
    return value if type(value) is int and value > 0 else None


def _validated_option_volume_context(value: object) -> dict[str, Any]:
    """Validate and copy the canonical per-level broker volume context."""

    if not isinstance(value, Mapping) or set(value) != {"current", "event"}:
        raise ValueError("GEX option volume context requires exact current/event fields")
    current = value.get("current")
    if not isinstance(current, Mapping) or set(current) != set(_OPTION_VOLUME_CURRENT_FIELDS):
        raise ValueError("GEX current option volume context has an invalid shape")
    for key in (
        "call_volume",
        "put_volume",
        "total_volume",
        "call_oi",
        "put_oi",
        "total_oi",
        "turnover",
    ):
        item = current.get(key)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"GEX current option volume {key} must be numeric or null")
        number = finite_number_or_none(item)
        if number is None or number < 0:
            raise ValueError(f"GEX current option volume {key} must be finite and non-negative")
    rank = current.get("rank")
    if rank is not None and (type(rank) is not int or rank <= 0):
        raise ValueError("GEX current option volume rank must be a positive integer or null")
    call_volume = finite_number_or_none(current.get("call_volume"))
    put_volume = finite_number_or_none(current.get("put_volume"))
    total_volume = finite_number_or_none(current.get("total_volume"))
    call_oi = finite_number_or_none(current.get("call_oi"))
    put_oi = finite_number_or_none(current.get("put_oi"))
    total_oi = finite_number_or_none(current.get("total_oi"))
    turnover = finite_number_or_none(current.get("turnover"))
    volume_sides_complete = call_volume is not None and put_volume is not None
    oi_sides_complete = call_oi is not None and put_oi is not None
    if (total_volume is not None) != volume_sides_complete:
        raise ValueError("GEX current option volume total requires both sides")
    if (total_oi is not None) != oi_sides_complete:
        raise ValueError("GEX current option OI total requires both sides")
    if total_volume is not None and not math.isclose(
        total_volume,
        call_volume + put_volume,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("GEX current option volume total disagrees with its sides")
    if total_oi is not None and not math.isclose(
        total_oi,
        call_oi + put_oi,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise ValueError("GEX current option OI total disagrees with its sides")
    if total_volume is not None and total_oi is not None and total_oi > 0:
        if turnover is None or not math.isclose(
            turnover,
            total_volume / total_oi,
            rel_tol=0.0,
            abs_tol=5e-5,
        ):
            raise ValueError("GEX current option turnover disagrees with volume and OI")
    elif turnover is not None:
        raise ValueError("GEX current option turnover requires volume and positive OI")

    event = value.get("event")
    if event is not None:
        if not isinstance(event, Mapping) or set(event) != set(_OPTION_VOLUME_EVENT_FIELDS):
            raise ValueError("GEX option volume event has an invalid shape")
        for key in (
            "call_volume_delta",
            "put_volume_delta",
            "total_volume_delta",
            "turnover",
            "acceleration",
            "flow_per_point",
            "window_seconds",
        ):
            item = event.get(key)
            if item is None:
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"GEX option volume event {key} must be numeric or null")
            number = finite_number_or_none(item)
            if number is None or number < 0:
                raise ValueError(f"GEX option volume event {key} must be finite and non-negative")
        for key, lower, upper in (
            ("bias", -1.0, 1.0),
            ("call_participation", 0.0, 1.0),
            ("put_participation", 0.0, 1.0),
        ):
            item = event.get(key)
            if item is None:
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError(f"GEX option volume event {key} must be numeric or null")
            number = finite_number_or_none(item)
            if number is None or not lower <= number <= upper:
                raise ValueError(f"GEX option volume event {key} is out of range")
        spot_move = event.get("spot_move_points")
        if spot_move is not None and (
            isinstance(spot_move, bool)
            or not isinstance(spot_move, (int, float))
            or finite_number_or_none(spot_move) is None
        ):
            raise ValueError("GEX option volume event spot movement must be finite or null")
        if type(event.get("material")) is not bool:
            raise ValueError("GEX option volume event material must be boolean")
        if event.get("interaction") not in {
            "",
            "activity_away_from_level",
            "price_approaching_with_activity",
            "price_crossed_with_activity",
            "price_near_level",
            "price_stationary_near_activity",
            "spot_moved_away_with_activity",
            "two_sided_activity_near_level",
        }:
            raise ValueError("GEX option volume event interaction is invalid")
        if event.get("cross_direction") not in {"", "up", "down"}:
            raise ValueError("GEX option volume event cross direction is invalid")
        if event.get("state") not in {
            "accelerating",
            "call_participation",
            "high_turnover",
            "put_participation",
            "quiet",
            "two_sided",
        }:
            raise ValueError("GEX option volume event state is invalid")
        if event.get("source") != "broker_volume_delta":
            raise ValueError("GEX option volume event source is invalid")
        call_delta = finite_number_or_none(event.get("call_volume_delta"))
        put_delta = finite_number_or_none(event.get("put_volume_delta"))
        total_delta = finite_number_or_none(event.get("total_volume_delta"))
        if (
            call_delta is None
            or put_delta is None
            or total_delta is None
            or not math.isclose(total_delta, call_delta + put_delta, rel_tol=0.0, abs_tol=1e-6)
        ):
            raise ValueError("GEX option volume event total disagrees with its sides")
        bias = finite_number_or_none(event.get("bias"))
        call_participation = finite_number_or_none(event.get("call_participation"))
        put_participation = finite_number_or_none(event.get("put_participation"))
        if total_delta > 0:
            expected_bias = (call_delta - put_delta) / total_delta
            expected_call_participation = call_delta / total_delta
            expected_put_participation = put_delta / total_delta
            if (
                bias is None
                or call_participation is None
                or put_participation is None
                or not math.isclose(bias, expected_bias, rel_tol=0.0, abs_tol=5e-5)
                or not math.isclose(
                    call_participation,
                    expected_call_participation,
                    rel_tol=0.0,
                    abs_tol=5e-5,
                )
                or not math.isclose(
                    put_participation,
                    expected_put_participation,
                    rel_tol=0.0,
                    abs_tol=5e-5,
                )
            ):
                raise ValueError("GEX option volume event participation disagrees with its sides")
        elif any(value is not None for value in (bias, call_participation, put_participation)):
            raise ValueError("Zero GEX option volume delta requires null participation")
    return {
        "current": dict(current),
        "event": dict(event) if isinstance(event, Mapping) else None,
    }


def _validated_gex_level_motion(value: object) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(value, Mapping)
        or not value
        or any(side not in {"call", "put", "net"} for side in value)
    ):
        raise ValueError("GEX level motion requires exact call/put/net event keys")
    out: dict[str, dict[str, Any]] = {}
    for side, raw_event in value.items():
        if not isinstance(raw_event, Mapping) or raw_event.get("side") != side:
            raise ValueError("GEX level motion side is invalid")
        motion_type = raw_event.get("type")
        if motion_type in {"roll_up", "roll_down"}:
            if side not in {"call", "put"} or set(raw_event) != _GEX_LEVEL_MOTION_ROLL_FIELDS:
                raise ValueError("GEX wall-roll motion has an invalid shape")
            previous = finite_number_or_none(raw_event.get("from_price"))
            current = finite_number_or_none(raw_event.get("to_price"))
            if previous is None or current is None or previous == current:
                raise ValueError("GEX wall-roll motion requires distinct finite prices")
            if (motion_type == "roll_up") != (current > previous):
                raise ValueError("GEX wall-roll direction disagrees with its prices")
        else:
            if (
                motion_type
                not in {
                    "strengthening",
                    "weakening",
                    "sign_flip_positive",
                    "sign_flip_negative",
                }
                or set(raw_event) != _GEX_LEVEL_MOTION_VALUE_FIELDS
            ):
                raise ValueError("GEX level-value motion has an invalid shape")
            previous = finite_number_or_none(raw_event.get("from"))
            current = finite_number_or_none(raw_event.get("to"))
            delta = finite_number_or_none(raw_event.get("delta"))
            delta_pct = raw_event.get("delta_pct")
            if (
                previous is None
                or current is None
                or delta is None
                or not math.isclose(delta, current - previous, rel_tol=0.0, abs_tol=1e-3)
                or (
                    delta_pct is not None
                    and (
                        isinstance(delta_pct, bool)
                        or not isinstance(delta_pct, (int, float))
                        or finite_number_or_none(delta_pct) is None
                    )
                )
            ):
                raise ValueError("GEX level-value motion facts are inconsistent")
            if motion_type == "sign_flip_positive" and not previous < 0 < current:
                raise ValueError("Positive GEX sign-flip motion is inconsistent")
            if motion_type == "sign_flip_negative" and not previous > 0 > current:
                raise ValueError("Negative GEX sign-flip motion is inconsistent")
        window_seconds = finite_number_or_none(raw_event.get("window_seconds"))
        if window_seconds is None or window_seconds <= 0:
            raise ValueError("GEX level motion requires a positive finite window")
        out[side] = dict(raw_event)
    return out


def require_gex_levels(
    levels: Sequence[Mapping[str, Any]],
    *,
    max_levels: int,
    spot: float,
) -> list[dict[str, Any]]:
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX level projection requires a positive integer limit")
    exact_spot = finite_number_or_none(spot)
    if exact_spot is None or exact_spot <= 0:
        raise ValueError("GEX level projection requires a finite positive spot")
    ranked: list[dict[str, Any]] = []
    seen_prices: set[float] = set()
    seen_ranks: set[int] = set()
    for level in levels:
        if not isinstance(level, Mapping):
            raise TypeError("GEX levels must be mappings")
        extra_fields = set(level) - _GEX_LEVEL_FIELDS
        if extra_fields not in (set(), {"motion"}):
            raise ValueError("GEX level contains fields outside the canonical contract")
        price = finite_number_or_none(level.get("price"))
        rank = level.get("selection_rank")
        if price is None or price <= 0 or type(rank) is not int or rank <= 0:
            raise ValueError("GEX levels require canonical price and selection_rank")
        if price in seen_prices or rank in seen_ranks:
            raise ValueError("GEX level prices and selection ranks must be unique")
        seen_prices.add(price)
        seen_ranks.add(rank)
        kind = level.get("kind")
        kind_class = level.get("kind_class")
        strength = finite_number_or_none(level.get("strength"))
        power_class = level.get("power_class")
        if (
            not isinstance(kind, str)
            or kind
            not in {
                "CALL_WALL",
                "PUT_WALL",
                "POS_GAMMA_NODE",
                "NEG_GAMMA_NODE",
                "GEX_NODE",
            }
            or kind_class != gex_kind_class(kind)
            or strength is None
            or not 0 <= strength <= 1
        ):
            raise ValueError("GEX level kind and strength fields are invalid")
        expected_power_class = gex_level_power_class(strength)
        if power_class != expected_power_class:
            raise ValueError("GEX level power_class does not match strength")
        numeric = {
            key: finite_number_or_none(level.get(key))
            for key in (
                "net_gex",
                "call_gex",
                "put_gex",
                "abs_gex",
                "abs_flow_1pt",
                "distance_from_spot",
                "zone_half_width",
            )
        }
        if any(value is None for value in numeric.values()):
            raise ValueError("GEX level numeric facts must be finite")
        if (
            numeric["abs_gex"] <= 0
            or numeric["call_gex"] < 0
            or numeric["put_gex"] > 0
            or numeric["abs_flow_1pt"] <= 0
            or numeric["zone_half_width"] <= 0
            or not math.isclose(
                numeric["net_gex"],
                numeric["call_gex"] + numeric["put_gex"],
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
            or not math.isclose(
                numeric["abs_gex"],
                abs(numeric["call_gex"]) + abs(numeric["put_gex"]),
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
        ):
            raise ValueError("GEX level facts are internally inconsistent")
        expected_distance = price - exact_spot
        expected_abs_flow = numeric["abs_gex"] / (exact_spot * 0.01)
        expected_spot_side = (
            "above"
            if price > exact_spot * 1.000001
            else "below"
            if price < exact_spot * 0.999999
            else "inside"
        )
        if (
            not math.isclose(
                numeric["distance_from_spot"],
                expected_distance,
                rel_tol=1e-12,
                abs_tol=1e-8,
            )
            or not math.isclose(
                numeric["abs_flow_1pt"],
                expected_abs_flow,
                rel_tol=1e-9,
                abs_tol=1e-6,
            )
            or level.get("spot_side") != expected_spot_side
        ):
            raise ValueError("GEX level spot facts are internally inconsistent")
        canonical_level = dict(level)
        canonical_level["option_volume_context"] = _validated_option_volume_context(
            level.get("option_volume_context")
        )
        if "motion" in level:
            canonical_level["motion"] = _validated_gex_level_motion(level["motion"])
        ranked.append(canonical_level)
    if sorted(seen_ranks) != list(range(1, len(ranked) + 1)):
        raise ValueError("GEX level selection ranks must be contiguous")
    if ranked:
        peak_abs_gex = max(float(level["abs_gex"]) for level in ranked)
        for level in ranked:
            expected_strength = round(float(level["abs_gex"]) / peak_abs_gex, 4)
            if not math.isclose(
                float(level["strength"]),
                expected_strength,
                rel_tol=0.0,
                abs_tol=5e-5,
            ):
                raise ValueError("GEX level strength must match gross GEX relative to the peak")
    return _select_validated_gex_levels(ranked, max_levels=max_levels)


def _select_validated_gex_levels(
    levels: Sequence[dict[str, Any]], *, max_levels: int
) -> list[dict[str, Any]]:
    """Select within the admission pipeline after every level has been validated."""
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX level projection requires a positive integer limit")
    selected = sorted(
        levels,
        key=lambda level: (level["selection_rank"], level["price"]),
    )[:max_levels]
    return sorted(selected, key=lambda level: level["price"])
