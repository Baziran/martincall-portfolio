from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.contracts import (
    gex_capture_revision_at,
    gex_comparison_scope_from_contract_universe,
    require_exact_gex_capture_lane,
    require_gex_contract_market_facts,
    require_gex_contract_observation,
    require_gex_market_data_entitlement,
    require_gex_option_universe_expiry,
)
from aef_terminal.data.gex.math import compute_gamma_flip
from aef_terminal.data.gex.payload_contract import (
    GEX_RAW_STRIKE_REDUNDANT_FIELDS,
    _GEX_RAW_META_TIMESTAMP_FIELDS,
    _OPTION_VOLUME_EVENT_FIELDS,
    _validated_option_volume_context,
    gex_level_power_class,
    require_gex_expiry_profile,
    require_gex_levels,
)
from aef_terminal.data.gex.quality import (
    _gex_contract_series_scope,
    _gex_contract_series_strike_key,
    _gex_execution_input_quality,
    _gex_frame_quality,
    _qualified_gex_contract_rows,
)
from aef_terminal.data.gex.utils import (
    _clip,
    finite_number_or_none,
    _num,
    _same_price,
    _strike_step,
    gex_kind_class,
)


def _gex_request_meta(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project provider metadata to the public acquisition-scope contract."""

    if not isinstance(meta, Mapping):
        raise TypeError("GEX request metadata must be a mapping")
    source = meta
    provider_transport_fields = {
        "client_id",
        "market_data_type",
        "wait_seconds",
        "batch_size",
        "batch_pause_seconds",
    }
    forbidden = sorted(provider_transport_fields.intersection(source))
    if forbidden:
        raise ValueError(f"GEX request metadata contains provider transport fields: {forbidden}")
    out: dict[str, Any] = {}
    for key in (
        "refresh_mode",
        "scheduler_lane",
        "provider_request_session",
        "requested_market_data_entitlement",
    ):
        value = source.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value or value != value.strip():
            raise TypeError(f"GEX request metadata {key} must be an exact non-empty string")
        out[key] = value
    for key in (
        "strike_count",
        "requested_strike_limit",
        "max_expirations",
        "max_contracts",
        "request_batch_size",
    ):
        value = source.get(key)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise TypeError(f"GEX request metadata {key} must be a non-negative integer")
        out[key] = value
    entitlement = out.get("requested_market_data_entitlement")
    if entitlement not in {"live", "frozen", "delayed", "delayed_frozen", "unknown"}:
        raise TypeError("GEX request metadata requires an exact requested entitlement")
    futures_options = source.get("futures_options")
    if futures_options is not None:
        if type(futures_options) is not bool:
            raise TypeError("GEX request metadata futures_options must be a boolean")
        out["futures_options"] = futures_options
    return out


def _option_volume_context_from_strike(row: Mapping[str, Any]) -> dict[str, Any]:
    if "option_volume_context" in row:
        raise ValueError("Strike rows cannot carry the public GEX option volume context")
    call_volume = finite_number_or_none(row.get("call_volume"))
    put_volume = finite_number_or_none(row.get("put_volume"))
    call_oi = finite_number_or_none(row.get("call_oi"))
    put_oi = finite_number_or_none(row.get("put_oi"))
    total_volume = (
        call_volume + put_volume if call_volume is not None and put_volume is not None else None
    )
    total_oi = call_oi + put_oi if call_oi is not None and put_oi is not None else None
    turnover = (
        total_volume / total_oi
        if total_volume is not None and total_oi is not None and total_oi > 0
        else None
    )
    event_source = row.get("_option_volume_event")
    return _validated_option_volume_context(
        {
            "current": {
                "call_volume": call_volume,
                "put_volume": put_volume,
                "total_volume": total_volume,
                "call_oi": call_oi,
                "put_oi": put_oi,
                "total_oi": total_oi,
                "turnover": round(turnover, 4) if turnover is not None else None,
                "rank": row.get("_current_volume_rank"),
            },
            "event": (
                {key: event_source.get(key) for key in _OPTION_VOLUME_EVENT_FIELDS}
                if isinstance(event_source, Mapping)
                else None
            ),
        }
    )


def _aggregate_by_strike(rows: Sequence[dict[str, Any]], spot: float) -> list[dict[str, Any]]:
    qualified_rows = _qualified_gex_contract_rows(rows)
    prepared_rows: list[
        tuple[
            Mapping[str, Any],
            tuple[str, str, str, float, float],
            dict[str, float | None],
        ]
    ] = []
    for row in qualified_rows:
        key = _gex_contract_series_strike_key(row)
        facts = require_gex_contract_market_facts(row)
        prepared_rows.append((row, key, facts))
    grouped: dict[float, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    call_gamma_counts: dict[float, int] = defaultdict(int)
    put_gamma_counts: dict[float, int] = defaultdict(int)
    volume_complete: dict[tuple[float, str], bool] = defaultdict(lambda: True)
    for row, key, facts in prepared_rows:
        strike = key[-1]
        gex = facts["gex"]
        gamma = facts["gamma"]
        open_interest = facts["open_interest"]
        volume = facts["volume"]
        right = row.get("right")
        assert gex is not None
        assert gamma is not None
        assert open_interest is not None
        if right == "C":
            grouped[strike]["call_gex"] += gex
            grouped[strike]["call_oi"] += open_interest
            if volume is None:
                volume_complete[(strike, right)] = False
            else:
                grouped[strike]["call_volume"] += volume
            grouped[strike]["call_gamma"] += gamma
            call_gamma_counts[strike] += 1
        elif right == "P":
            grouped[strike]["put_gex"] += gex
            grouped[strike]["put_oi"] += open_interest
            if volume is None:
                volume_complete[(strike, right)] = False
            else:
                grouped[strike]["put_volume"] += volume
            grouped[strike]["put_gamma"] += gamma
            put_gamma_counts[strike] += 1
    out = []
    for strike, values in sorted(grouped.items()):
        call_gex = values["call_gex"]
        put_gex = values["put_gex"]
        net_gex = call_gex + put_gex
        abs_gex = abs(call_gex) + abs(put_gex)
        call_oi = values["call_oi"] if call_gamma_counts[strike] > 0 else None
        put_oi = values["put_oi"] if put_gamma_counts[strike] > 0 else None
        call_volume = (
            values["call_volume"]
            if call_gamma_counts[strike] > 0 and volume_complete[(strike, "C")]
            else None
        )
        put_volume = (
            values["put_volume"]
            if put_gamma_counts[strike] > 0 and volume_complete[(strike, "P")]
            else None
        )
        out.append(
            {
                "strike": strike,
                "call_gex": call_gex,
                "call_oi": call_oi,
                "call_volume": call_volume,
                "call_gamma": values["call_gamma"] / max(call_gamma_counts[strike], 1),
                "put_gex": put_gex,
                "put_oi": put_oi,
                "put_volume": put_volume,
                "put_gamma": values["put_gamma"] / max(put_gamma_counts[strike], 1),
                "_current_volume_rank": None,
                "net_gex": net_gex,
                "abs_gex": abs_gex,
                "distance_from_spot": strike - spot,
            }
        )
    ranked_current_volume = sorted(
        (
            row
            for row in out
            if finite_number_or_none(row.get("call_oi")) is not None
            and finite_number_or_none(row.get("put_oi")) is not None
            and finite_number_or_none(row.get("call_volume")) is not None
            and finite_number_or_none(row.get("put_volume")) is not None
            and _num(row.get("call_oi")) + _num(row.get("put_oi")) > 0
            and _num(row.get("call_volume")) + _num(row.get("put_volume")) > 0
        ),
        key=lambda row: (
            -(
                (_num(row.get("call_volume")) + _num(row.get("put_volume")))
                / (_num(row.get("call_oi")) + _num(row.get("put_oi")))
            ),
            -(_num(row.get("call_volume")) + _num(row.get("put_volume"))),
            _num(row.get("strike")),
        ),
    )
    for rank, row in enumerate(ranked_current_volume, start=1):
        row["_current_volume_rank"] = rank
    return out


def _levels_from_strikes(strikes: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    if not strikes:
        return {
            "call_wall": None,
            "put_wall": None,
            "net_gex": None,
        }
    call_rows = [row for row in strikes if _num(row.get("call_gex")) > 0]
    put_rows = [row for row in strikes if _num(row.get("put_gex")) < 0]
    return {
        "call_wall": _num(max(call_rows, key=lambda row: _num(row.get("call_gex")))["strike"])
        if call_rows
        else None,
        "put_wall": _num(min(put_rows, key=lambda row: _num(row.get("put_gex")))["strike"])
        if put_rows
        else None,
        "net_gex": sum(_num(row.get("net_gex")) for row in strikes),
    }


def _select_gex_levels(
    strikes: Sequence[dict[str, Any]],
    spot: float,
    levels: dict[str, float | None],
    *,
    max_levels: int,
) -> list[dict[str, Any]]:
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX level selection requires a positive integer limit")
    if not strikes:
        return []
    exact_spot = finite_number_or_none(spot)
    if exact_spot is None or exact_spot <= 0:
        raise ValueError("GEX level selection requires a finite positive spot")
    max_abs = max((_num(row.get("abs_gex")) for row in strikes), default=0.0)
    if max_abs <= 0:
        return []
    step = _strike_step(strikes)
    ranked: list[dict[str, Any]] = []
    for row in strikes:
        strike = finite_number_or_none(row.get("strike"))
        abs_gex = finite_number_or_none(row.get("abs_gex"))
        if strike is None or abs_gex is None or abs_gex <= 0:
            continue
        raw_strength = _clip(abs_gex / max_abs, 0.0, 1.0)
        strength = round(raw_strength, 4)
        kind = _level_kind(row, levels)
        power_class = gex_level_power_class(strength)
        zone_half_width = max(
            step * (0.22 + 0.30 * raw_strength),
            max(abs(exact_spot) * 0.00025, 0.01),
        )
        spot_side = (
            "above"
            if strike > exact_spot * 1.000001
            else "below"
            if strike < exact_spot * 0.999999
            else "inside"
        )
        ranked.append(
            {
                "price": strike,
                "kind": kind,
                "kind_class": gex_kind_class(kind),
                "power_class": power_class,
                "strength": strength,
                "selection_rank": None,
                "net_gex": _num(row.get("net_gex")),
                "call_gex": _num(row.get("call_gex")),
                "put_gex": _num(row.get("put_gex")),
                "abs_gex": abs_gex,
                "abs_flow_1pt": abs_gex / (exact_spot * 0.01),
                "distance_from_spot": strike - exact_spot,
                "zone_half_width": round(zone_half_width, 4),
                "spot_side": spot_side,
                "option_volume_context": _option_volume_context_from_strike(row),
            }
        )
    ordered = sorted(
        ranked,
        key=lambda level: (
            -_num(level.get("abs_gex")),
            abs(_num(level.get("distance_from_spot"))),
            _num(level.get("price")),
        ),
    )
    selected = ordered[:max_levels]
    out: list[dict[str, Any]] = []
    for selection_rank, level in enumerate(selected, start=1):
        level["selection_rank"] = selection_rank
        out.append(level)
    return sorted(out, key=lambda row: _num(row.get("price")))


def _gex_level_is_weak(level: Mapping[str, Any]) -> bool:
    return level.get("power_class") == "WEAK"


def _gex_visibility_summary(
    *,
    strikes: Sequence[dict[str, Any]],
    output_levels: Sequence[dict[str, Any]],
    expiry_profile: Sequence[dict[str, Any]],
    max_levels: int,
) -> dict[str, Any]:
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX visibility summary requires a positive integer limit")
    raw_strikes = list(strikes or [])
    levels = list(output_levels or [])
    nonzero_strikes = [row for row in raw_strikes if _num(row.get("abs_gex")) > 0]
    weak_levels = [level for level in levels if _gex_level_is_weak(level)]
    strong_levels = [level for level in levels if not _gex_level_is_weak(level)]
    limit = max_levels
    return {
        "raw_strike_count": len(raw_strikes),
        "nonzero_strike_count": len(nonzero_strikes),
        "zero_strike_count": max(0, len(raw_strikes) - len(nonzero_strikes)),
        "candidate_level_count": len(nonzero_strikes),
        "output_level_count": len(levels),
        "expiry_profile_row_count": len(expiry_profile or []),
        "weak_level_count": len(weak_levels),
        "strong_level_count": len(strong_levels),
        "hidden_by_max_levels": max(0, len(nonzero_strikes) - len(levels)),
        "max_levels": limit,
    }


def _level_kind(row: dict[str, Any], levels: dict[str, float | None]) -> str:
    strike = _num(row.get("strike"))
    net_gex = _num(row.get("net_gex"))
    is_call_wall = _same_price(strike, levels.get("call_wall"))
    is_put_wall = _same_price(strike, levels.get("put_wall"))
    if is_call_wall and is_put_wall:
        if abs(_num(row.get("put_gex"))) > _num(row.get("call_gex")):
            return "PUT_WALL"
        return "CALL_WALL"
    if is_call_wall:
        return "CALL_WALL"
    if is_put_wall:
        return "PUT_WALL"
    if net_gex > 0:
        return "POS_GAMMA_NODE"
    if net_gex < 0:
        return "NEG_GAMMA_NODE"
    return "GEX_NODE"


def _payload_from_rows(
    provider_symbol: str,
    *,
    captured_at: datetime,
    meta: dict[str, Any],
    strikes: list[dict[str, Any]],
    diagnostics: Mapping[str, Any],
    max_levels: int,
    stale_minutes: int,
    source: str,
    capture_mode: str,
    contract_rows: Sequence[dict[str, Any]] = (),
    comparison_contract_rows: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if (
        not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        raise ValueError("GEX payload requires an explicit timezone-aware captured_at")
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX payload max_levels must be a positive integer")
    if type(stale_minutes) is not int or stale_minutes <= 0:
        raise ValueError("GEX payload stale_minutes must be a positive integer")
    captured_at = captured_at.astimezone(UTC)
    meta = dict(meta)
    if set(meta).intersection(_GEX_RAW_META_TIMESTAMP_FIELDS):
        raise ValueError("GEX raw metadata cannot duplicate captured_at")
    if meta.get("provider_symbol") != provider_symbol:
        raise ValueError("GEX raw provider symbol must exactly match the payload")
    spot = finite_number_or_none(meta.get("spot"))
    if spot is None or spot <= 0:
        raise ValueError("GEX payload requires a finite positive provider spot")
    require_exact_gex_capture_lane(
        source=source,
        capture_mode=capture_mode,
    )
    if contract_rows:
        analysis_entitlements = {
            require_gex_contract_observation(row)["market_data_entitlement"]
            for row in contract_rows
        }
        meta["market_data_entitlement"] = (
            next(iter(analysis_entitlements)) if len(analysis_entitlements) == 1 else "unknown"
        )
    requested_universe_expires_at = require_gex_option_universe_expiry(
        meta.get("option_universe_expires_at"),
        captured_at=captured_at,
        allow_unknown=True,
    )
    if comparison_contract_rows and requested_universe_expires_at is None:
        raise ValueError(
            "GEX provider-selected comparison universe requires its exact provider expiry bound"
        )
    contract_expiries: list[datetime] = []
    for row in contract_rows:
        if row.get("expiry_time_source") != "ibkr_contract_details":
            raise ValueError("GEX payload contract rows require exact provider expiry facts")
        expiry_at = require_gex_option_universe_expiry(
            row.get("expiry_at"),
            captured_at=captured_at,
            allow_unknown=False,
        )
        assert expiry_at is not None
        contract_expiries.append(expiry_at)
    option_universe_expiry_candidates = [
        expiry_at
        for expiry_at in (
            requested_universe_expires_at,
            min(contract_expiries) if contract_expiries else None,
        )
        if expiry_at is not None
    ]
    option_universe_expires_at = (
        min(option_universe_expiry_candidates) if option_universe_expiry_candidates else None
    )
    if option_universe_expires_at is None:
        raise ValueError("GEX payload requires exact provider option-universe expiry")
    meta["option_universe_expires_at"] = (
        requested_universe_expires_at or option_universe_expires_at
    ).isoformat()
    if not isinstance(diagnostics, Mapping):
        raise TypeError("GEX payload diagnostics must be a mapping")
    request_meta = _gex_request_meta(meta)
    strike_count = request_meta.get("strike_count")
    requested_strike_limit = request_meta.get("requested_strike_limit")
    futures_options = request_meta.get("futures_options")
    if type(strike_count) is not int or strike_count <= 0:
        raise ValueError("GEX payload requires the positive selected strike count")
    if (
        type(requested_strike_limit) is not int
        or requested_strike_limit <= 0
        or strike_count > requested_strike_limit
    ):
        raise ValueError(
            "GEX payload requires a positive requested strike limit covering the selected universe"
        )
    if type(futures_options) is not bool:
        raise ValueError("GEX payload requires a typed futures_options flag")
    required_count_diagnostics = (
        "diag_contracts_requested",
        "diag_strikes_requested",
        "diag_option_rows",
        "diag_strike_rows",
        "diag_gamma_rows",
        "diag_usable_gamma_rows",
        "diag_open_interest_rows",
        "diag_open_interest_unavailable_rows",
        "diag_usable_gex_rows",
        "diag_nonzero_gex_rows",
        "diag_expected_pair_count",
        "diag_received_pair_count",
        "diag_usable_pair_count",
        "diag_unpaired_series_strikes",
    )
    required_numeric_diagnostics = (
        "diag_call_oi_total",
        "diag_put_oi_total",
    )
    frame_diagnostics = dict(diagnostics)
    for key in required_count_diagnostics:
        value = frame_diagnostics.get(key)
        if type(value) is not int or value < 0:
            raise TypeError(f"GEX diagnostics {key} must be an exact non-negative integer")
    for key in required_numeric_diagnostics:
        value = frame_diagnostics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"GEX diagnostics {key} must be a non-negative number")
        finite_value = finite_number_or_none(value)
        if finite_value is None or finite_value < 0:
            raise ValueError(f"GEX diagnostics {key} must be a non-negative number")
    if type(frame_diagnostics.get("diag_collection_timeout")) is not bool:
        raise TypeError("GEX diagnostics collection timeout must be an exact boolean")
    for key in ("diag_greek_sources", "diag_generic_ticks"):
        if not isinstance(frame_diagnostics.get(key), str):
            raise TypeError(f"GEX diagnostics {key} must be a string")
    if frame_diagnostics["diag_strikes_requested"] != requested_strike_limit:
        raise ValueError("GEX diagnostics requested strike limit must match request_meta")
    if frame_diagnostics["diag_strike_rows"] != len(strikes):
        raise ValueError("GEX diagnostics strike rows must match the authoritative rows")
    levels = _levels_from_strikes(strikes)
    publishable, quality_reason, quality_meta = _gex_frame_quality(
        frame_diagnostics,
        strikes,
    )
    frame_diagnostics = {**frame_diagnostics, **quality_meta}
    market_data_entitlement = require_gex_market_data_entitlement(
        meta.get("market_data_entitlement"),
        allow_unknown=True,
    )
    execution_inputs_authoritative, execution_quality = _gex_execution_input_quality(
        captured_at=captured_at,
        meta=meta,
        contract_rows=contract_rows,
        market_data_entitlement=market_data_entitlement,
    )
    frame_diagnostics.update(execution_quality)
    spot_frame_complete = bool(frame_diagnostics.get("diag_frame_spot_eligible"))
    if publishable and not spot_frame_complete:
        publishable = False
        quality_reason = (
            "underlying spot is display-only: a frame-complete GEX snapshot "
            "requires a fresh exact bid/ask midpoint with client-receive time "
            "or last trade with provider-event time and known entitlement"
        )
        frame_diagnostics.update(
            {
                "diag_frame_publishable": False,
                "diag_frame_quality_code": "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE",
                "diag_frame_quality_reason": quality_reason,
            }
        )
    entitlement_complete = market_data_entitlement != "unknown"
    frame_diagnostics.update(
        {
            "diag_market_data_entitlement": market_data_entitlement,
            "diag_market_data_entitlement_uniform_known": entitlement_complete,
        }
    )
    if publishable and not entitlement_complete:
        publishable = False
        quality_reason = (
            "actual provider market-data entitlement is missing or mixed across "
            "the selected option universe"
        )
        frame_diagnostics.update(
            {
                "diag_frame_publishable": False,
                "diag_frame_quality_code": "MARKET_DATA_ENTITLEMENT_UNKNOWN_OR_MIXED",
                "diag_frame_quality_reason": quality_reason,
            }
        )
    if publishable:
        contracts_requested = frame_diagnostics["diag_contracts_requested"]
        expected_pair_count = frame_diagnostics["diag_expected_pair_count"]
        qualified_pair_count = meta.get("qualified_pair_count")
        if (
            type(qualified_pair_count) is not int
            or qualified_pair_count <= 0
            or contracts_requested != qualified_pair_count * 2
            or expected_pair_count != qualified_pair_count
        ):
            raise ValueError(
                "GEX publishable frame requires the exact provider-qualified pair denominator"
            )
    gamma_flip = compute_gamma_flip(
        futures_options,
        contract_rows,
        spot,
        risk_free_rate=meta.get("risk_free_rate"),
        dividend_yield=meta.get("dividend_yield"),
    )
    net_gex = finite_number_or_none(levels.get("net_gex"))
    global_gamma_regime = (
        "UNKNOWN"
        if net_gex is None or net_gex == 0
        else "POSITIVE_ESTIMATE"
        if net_gex > 0
        else "NEGATIVE_ESTIMATE"
    )
    age_minutes = (datetime.now(tz=UTC) - captured_at).total_seconds() / 60.0
    stale = age_minutes > stale_minutes
    output_levels = require_gex_levels(
        _select_gex_levels(
            strikes,
            spot,
            levels,
            max_levels=max_levels,
        ),
        max_levels=max_levels,
        spot=spot,
    )
    qualified_universe_complete = bool(
        frame_diagnostics.get("diag_frame_qualified_universe_complete")
    )
    expiry_profile = (
        _expiry_profile_from_contracts(contract_rows) if qualified_universe_complete else []
    )
    visibility_summary = _gex_visibility_summary(
        strikes=strikes,
        output_levels=output_levels,
        expiry_profile=expiry_profile,
        max_levels=max_levels,
    )
    partial = not publishable
    degraded_coverage = frame_diagnostics.get("diag_frame_quality_code") in {
        "SUFFICIENT_PAIR_COVERAGE",
        "SUFFICIENT_PAIR_COVERAGE_AFTER_TIMEOUT",
    }
    status = (
        "stale" if stale else "partial" if partial else "degraded" if degraded_coverage else "ok"
    )
    message = _gamma_regime_note(spot, levels, frame_diagnostics)
    if partial:
        message = f"GEX PARTIAL: {quality_reason}. {message}"
    else:
        if market_data_entitlement != "live":
            message = (
                f"GEX DISPLAY ONLY: {market_data_entitlement} provider market data is "
                f"not eligible for execution or alerts. {message}"
            )
        elif not execution_inputs_authoritative:
            message = (
                "GEX DISPLAY ONLY: the exact spot/model-greek observation set is "
                f"missing, stale, or non-live. {message}"
            )
        if degraded_coverage and not frame_diagnostics.get("diag_collection_timeout"):
            message = f"GEX DEGRADED COVERAGE: {quality_reason}. {message}"
    if frame_diagnostics.get("diag_collection_timeout"):
        contracts = int(_num(frame_diagnostics.get("diag_contracts_requested")))
        option_rows = int(_num(frame_diagnostics.get("diag_option_rows")))
        requested_strikes = int(_num(frame_diagnostics.get("diag_strikes_requested")))
        strike_rows = int(_num(frame_diagnostics.get("diag_strike_rows")))
        phase = str(frame_diagnostics.get("diag_collection_timeout_phase") or "option market data")
        strike_text = (
            f", strikes {strike_rows}/{requested_strikes}"
            if requested_strikes
            else f", strikes {strike_rows}"
        )
        timeout_prefix = "GEX DEGRADED COVERAGE" if publishable else "GEX PARTIAL"
        message = (
            f"{timeout_prefix}: received {option_rows}/{contracts} option rows"
            f"{strike_text}; timed out during {phase}. {message}"
        )
    if stale:
        message = f"GEX STALE: {message}"
    if partial and not qualified_universe_complete:
        output_levels = []
        expiry_profile = []
        visibility_summary = {
            **visibility_summary,
            "output_level_count": 0,
            "expiry_profile_row_count": 0,
            "weak_level_count": 0,
            "strong_level_count": 0,
            "hidden_reason": "frame_not_publishable",
        }
    payload: dict[str, Any] = {
        "ok": bool(publishable and output_levels),
        "enabled": True,
        "provider_symbol": provider_symbol,
        "source": source,
        "capture_mode": capture_mode,
        "capture_revision": gex_capture_revision_at(
            captured_at,
            capture_mode=capture_mode,
        ).isoformat(),
        "status": status,
        "message": message,
        "frame_complete": bool(publishable),
        "decision_authoritative": bool(
            publishable and not stale and execution_inputs_authoritative
        ),
        "option_universe_expires_at": (option_universe_expires_at.isoformat()),
        "market_data_entitlement": market_data_entitlement,
        "diagnostics": frame_diagnostics,
        "captured_at": captured_at.isoformat(),
        "age_minutes": round(age_minutes, 2),
        "stale_minutes": stale_minutes,
        "spot": spot,
        "global_gamma_regime": global_gamma_regime if publishable else "UNKNOWN",
        "gamma_flip": gamma_flip if publishable else None,
        "open_interest_as_of": "previous_settlement",
        "call_wall": levels.get("call_wall") if publishable else None,
        "put_wall": levels.get("put_wall") if publishable else None,
        "net_gex": net_gex if publishable else None,
        "levels": output_levels,
        "expiry_profile": expiry_profile,
        "visibility_summary": visibility_summary,
        "request_meta": request_meta,
        "raw": {
            "meta": dict(meta),
            "strikes": [
                {
                    key: value
                    for key, value in row.items()
                    if not str(key).startswith("_") and key not in GEX_RAW_STRIKE_REDUNDANT_FIELDS
                }
                for row in strikes
            ],
            "contracts": [dict(row) for row in contract_rows],
        },
    }
    if qualified_universe_complete:
        if comparison_contract_rows is None:
            raise ValueError(
                "GEX qualified frame requires the exact provider-selected "
                "comparison contract universe"
            )
        exact_comparison_rows = list(comparison_contract_rows)
        comparison_scope = gex_comparison_scope_from_contract_universe(
            exact_comparison_rows,
            source=source,
            capture_mode=capture_mode,
            futures_options=futures_options,
            risk_free_rate=meta.get("risk_free_rate"),
            dividend_yield=meta.get("dividend_yield"),
            market_data_entitlement=market_data_entitlement,
            allow_unknown_entitlement=True,
        )
        if comparison_scope["strike_count"] != strike_count:
            raise ValueError("GEX provider-selected strike count disagrees with comparison scope")
        if (
            len(comparison_scope["contract_con_ids"])
            != frame_diagnostics["diag_contracts_requested"]
        ):
            raise ValueError("GEX comparison scope must contain every requested provider contract")
        frame_diagnostics.update(
            {
                "diag_comparison_scope_source": "provider_selected_contract_universe",
                "diag_comparison_scope_contracts": len(comparison_scope["contract_con_ids"]),
                "diag_comparison_scope_strikes": comparison_scope["strike_count"],
            }
        )
        payload["diagnostics"] = frame_diagnostics
        payload["comparison_scope"] = comparison_scope
    return payload


def _expiry_profile_from_contracts(
    contract_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not contract_rows:
        return []
    _, rights_by_series_strike = _gex_contract_series_scope(contract_rows)
    if any(rights != {"C", "P"} for rights in rights_by_series_strike.values()):
        raise ValueError("GEX expiry profile requires complete exact call/put pairs")
    grouped: dict[tuple[str, float], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in contract_rows:
        expiry = row["expiry"]
        strike = finite_number_or_none(row["strike"])
        assert strike is not None
        right = row["right"]
        gex = require_gex_contract_market_facts(row)["gex"]
        if gex is None:
            raise ValueError("GEX expiry profile requires finite contract GEX")
        bucket = grouped[(expiry, strike)]
        if right == "C":
            bucket["call_gex"] += gex
        elif right == "P":
            bucket["put_gex"] += gex
    if not grouped:
        return []
    rows: list[dict[str, Any]] = []
    for (expiry, strike), values in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        call_gex = values["call_gex"]
        put_gex = values["put_gex"]
        net_gex = call_gex + put_gex
        abs_gex = abs(call_gex) + abs(put_gex)
        if abs_gex <= 0:
            continue
        rows.append(
            {
                "expiry": expiry,
                "strike": strike,
                "call_gex": call_gex,
                "put_gex": put_gex,
                "net_gex": net_gex,
                "abs_gex": abs_gex,
            }
        )
    return require_gex_expiry_profile(rows)


def _gamma_regime_note(
    spot: float,
    levels: dict[str, float | None],
    diagnostics: dict[str, Any] | None = None,
) -> str:
    net = finite_number_or_none(levels.get("net_gex"))
    if net is None or net == 0:
        detail = _gamma_unavailable_detail(diagnostics or {})
        return f"Gamma regime unavailable: {detail}"
    if net > 0:
        return "POSITIVE NET GEX ESTIMATE: modeled sign only; observed price behavior remains authoritative."
    return "NEGATIVE NET GEX ESTIMATE: modeled sign only; observed price behavior remains authoritative."


def _gamma_unavailable_detail(diagnostics: dict[str, Any]) -> str:
    contracts = int(_num(diagnostics.get("diag_contracts_requested")))
    requested_strikes = int(_num(diagnostics.get("diag_strikes_requested")))
    option_rows = int(_num(diagnostics.get("diag_option_rows")))
    strike_rows = int(_num(diagnostics.get("diag_strike_rows")))
    gamma_rows = int(_num(diagnostics.get("diag_gamma_rows")))
    usable_gamma_rows = int(_num(diagnostics.get("diag_usable_gamma_rows")))
    oi_rows = int(_num(diagnostics.get("diag_open_interest_rows")))
    usable_rows = int(_num(diagnostics.get("diag_usable_gex_rows")))
    nonzero_rows = int(_num(diagnostics.get("diag_nonzero_gex_rows")))
    call_oi = _num(diagnostics.get("diag_call_oi_total"))
    put_oi = _num(diagnostics.get("diag_put_oi_total"))
    ticks = str(diagnostics.get("diag_generic_ticks") or "").strip()
    suffix_parts = []
    if ticks:
        suffix_parts.append(f"ticks={ticks}")
    suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
    total_label = f"{option_rows}/{contracts}" if contracts else str(option_rows)
    strike_label = (
        f", strikes {strike_rows}/{requested_strikes}"
        if requested_strikes
        else f", strikes {strike_rows}"
    )
    strike_summary = strike_label.lstrip(", ")
    if contracts <= 0 and strike_rows <= 0:
        return "no option contracts were built or no cached strike rows exist."
    if option_rows <= 0 and strike_rows <= 0:
        return f"Provider returned no option rows for {contracts} requested contracts{strike_label}{suffix}."
    if option_rows > 0 and gamma_rows <= 0 and oi_rows <= 0:
        return f"Provider returned {total_label} option rows{strike_label}, but no greeks and no open interest{suffix}."
    if option_rows > 0 and gamma_rows <= 0:
        return f"Provider returned open interest on {oi_rows}/{option_rows} rows{strike_label}, but no option gamma greeks{suffix}."
    if option_rows > 0 and oi_rows <= 0:
        return f"Provider returned gamma on {usable_gamma_rows}/{option_rows} rows{strike_label}, but open interest is zero or missing on every row{suffix}."
    if option_rows > 0 and usable_rows <= 0:
        return (
            f"Provider returned gamma rows={gamma_rows} and OI rows={oi_rows}, "
            f"{strike_summary}, but no row has both non-zero gamma and open interest{suffix}."
        )
    if nonzero_rows <= 0:
        return (
            f"all calculated GEX rows are zero "
            f"({strike_summary}, call OI {call_oi:g}, put OI {put_oi:g}, usable rows {usable_rows}){suffix}."
        )
    return (
        f"net GEX summed to zero despite {nonzero_rows} non-zero rows "
        f"({strike_summary}, call OI {call_oi:g}, put OI {put_oi:g}){suffix}."
    )
