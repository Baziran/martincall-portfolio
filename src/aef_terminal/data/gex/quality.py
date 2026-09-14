from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import (
    GEX_ANALYSIS_MIN_PAIR_COVERAGE,
    GEX_EXECUTION_MODEL_INPUT_MAX_AGE_SECONDS,
    GEX_EXECUTION_SPOT_MAX_AGE_SECONDS,
)
from aef_terminal.data.gex.contracts import (
    GexUnderlyingQuoteObservation,
    GexUnderlyingQuoteUnavailableError,
    gex_underlying_quote_observation_from_cache,
    require_gex_contract_market_facts,
    require_gex_contract_observation,
    require_gex_option_series_identity,
    require_gex_underlying_quote_observation,
)
from aef_terminal.data.gex.utils import finite_number_or_none, _num, parse_gex_timestamp


def require_gex_acquisition_spot_observation(
    value: object,
    *,
    observed_at: datetime,
) -> GexUnderlyingQuoteObservation:
    """Fail before option acquisition when the cached spot cannot complete a frame."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("GEX spot preflight time must be timezone-aware")
    try:
        observation = gex_underlying_quote_observation_from_cache(value)
    except (TypeError, ValueError) as exc:
        raise GexUnderlyingQuoteUnavailableError(
            "the canonical underlying quote cache has no exact price observation",
            diagnostics={"quote_contract_error": str(exc)},
        ) from exc
    evidence_at = (
        parse_gex_timestamp(observation["received_at"])
        if observation["price_source"] == "bid_ask_mid"
        else parse_gex_timestamp(observation["provider_ts"])
        if observation["price_source"] == "last"
        else None
    )
    age_seconds = (
        (observed_at.astimezone(UTC) - evidence_at).total_seconds()
        if evidence_at is not None
        else None
    )
    eligible = bool(
        observation["market_data_entitlement"] != "unknown"
        and observation["price_source"] in {"bid_ask_mid", "last"}
        and (
            observation["time_basis"] == "client_receive"
            if observation["price_source"] == "bid_ask_mid"
            else observation["time_basis"] == "provider_event"
        )
        and age_seconds is not None
        and 0.0 <= age_seconds <= GEX_EXECUTION_SPOT_MAX_AGE_SECONDS
    )
    if eligible:
        return observation
    diagnostics = {
        "underlying_spot_price_source": observation["price_source"],
        "underlying_spot_time_basis": observation["time_basis"],
        "underlying_spot_market_data_entitlement": observation["market_data_entitlement"],
        "underlying_spot_age_seconds": (round(age_seconds, 3) if age_seconds is not None else None),
        "underlying_spot_max_age_seconds": GEX_EXECUTION_SPOT_MAX_AGE_SECONDS,
    }
    raise GexUnderlyingQuoteUnavailableError(
        "a fresh exact underlying bid/ask midpoint or last trade is not available",
        diagnostics=diagnostics,
    )


def _gex_contract_series_strike_key(
    row: Mapping[str, Any],
) -> tuple[str, str, str, float, float]:
    identity = require_gex_option_series_identity(row)
    strike_value = row.get("strike")
    strike = (
        finite_number_or_none(strike_value)
        if not isinstance(strike_value, bool) and isinstance(strike_value, (int, float))
        else None
    )
    if strike is None or strike <= 0:
        raise ValueError("GEX broker contract row has invalid exact series identity")
    return (
        identity["expiry"],
        identity["trading_class"],
        identity["exchange"],
        identity["multiplier"],
        strike,
    )


def _gex_contract_series_scope(
    contract_rows: Sequence[Mapping[str, Any]],
) -> tuple[
    list[str],
    dict[tuple[str, str, str, float, float], set[str]],
]:
    expiries: set[str] = set()
    rights_by_series_strike: dict[tuple[str, str, str, float, float], set[str]] = defaultdict(set)
    for row in contract_rows:
        if not isinstance(row, Mapping):
            raise TypeError("GEX broker contract rows must be mappings")
        key = _gex_contract_series_strike_key(row)
        right = row.get("right")
        if right not in {"C", "P"}:
            raise ValueError("GEX broker contract row has invalid option right")
        if right in rights_by_series_strike[key]:
            raise ValueError("GEX broker contract universe contains a duplicate option right")
        expiries.add(key[0])
        rights_by_series_strike[key].add(right)
    return sorted(expiries), rights_by_series_strike


def _qualified_gex_contract_rows(
    contract_rows: Sequence[Mapping[str, Any]],
    *,
    authority_at: datetime | None = None,
    expected_pair_count: int | None = None,
) -> list[dict[str, Any]]:
    """Select the canonical exact C/P universe for display or decision analysis.

    Without ``authority_at`` this returns every numerically complete pair. With
    it, only fresh live provider-observed pairs are returned. Supplying
    ``expected_pair_count`` enables the 75% analysis contract: use the
    authoritative subset when it reaches the threshold, otherwise retain the
    numerical subset for display-only analysis.
    """

    rows = [dict(row) for row in contract_rows]
    if not rows:
        return []
    if authority_at is not None and (
        not isinstance(authority_at, datetime)
        or authority_at.tzinfo is None
        or authority_at.utcoffset() is None
    ):
        raise ValueError("GEX pair authority clock must be timezone-aware")
    if expected_pair_count is not None and (
        type(expected_pair_count) is not int or expected_pair_count < 0
    ):
        raise ValueError("GEX expected pair count must be a non-negative integer")
    if expected_pair_count is not None and authority_at is None:
        raise ValueError("GEX pair coverage selection requires an authority clock")
    authority_utc = authority_at.astimezone(UTC) if authority_at is not None else None
    _, rights_by_series_strike = _gex_contract_series_scope(rows)
    rows_by_series_strike: dict[
        tuple[str, str, str, float, float],
        list[tuple[dict[str, Any], dict[str, float | None], bool]],
    ] = defaultdict(list)
    for row in rows:
        key = _gex_contract_series_strike_key(row)
        facts = require_gex_contract_market_facts(row)
        authority_eligible = True
        if authority_utc is not None:
            observation = require_gex_contract_observation(row)
            greek_source = row.get("greek_source")
            if not isinstance(greek_source, str):
                raise ValueError("GEX contract greek_source must be an exact string")
            model_received_at = parse_gex_timestamp(observation["model_greeks_received_at"])
            oi_received_at = parse_gex_timestamp(observation["open_interest_received_at"])
            model_age_seconds = (
                (authority_utc - model_received_at).total_seconds()
                if model_received_at is not None
                else None
            )
            oi_age_seconds = (
                (authority_utc - oi_received_at).total_seconds()
                if oi_received_at is not None
                else None
            )
            authority_eligible = bool(
                observation["market_data_entitlement"] == "live"
                and greek_source == "modelGreeks"
                and model_age_seconds is not None
                and 0.0 <= model_age_seconds <= GEX_EXECUTION_MODEL_INPUT_MAX_AGE_SECONDS
                and oi_age_seconds is not None
                and oi_age_seconds >= 0.0
            )
        rows_by_series_strike[key].append((row, facts, authority_eligible))
    numerically_qualified_keys = {
        key
        for key, entries in rows_by_series_strike.items()
        if rights_by_series_strike.get(key) == {"C", "P"}
        and len(entries) == 2
        and all(
            facts["gamma"] is not None
            and facts["gamma"] > 0
            and facts["open_interest"] is not None
            and facts["gex"] is not None
            and facts["abs_gex"] is not None
            for _row, facts, _authority_eligible in entries
        )
    }
    authoritative_keys = {
        key
        for key in numerically_qualified_keys
        if all(
            authority_eligible for _row, _facts, authority_eligible in rows_by_series_strike[key]
        )
    }
    if authority_utc is None:
        qualified_keys = numerically_qualified_keys
    elif expected_pair_count is None:
        qualified_keys = authoritative_keys
    else:
        minimum_pair_count = (
            max(
                1,
                math.ceil(expected_pair_count * GEX_ANALYSIS_MIN_PAIR_COVERAGE),
            )
            if expected_pair_count > 0
            else 0
        )
        qualified_keys = (
            authoritative_keys
            if len(authoritative_keys) >= minimum_pair_count
            else numerically_qualified_keys
        )
    return [row for row in rows if _gex_contract_series_strike_key(row) in qualified_keys]


def _gex_diagnostics(
    contract_rows: Sequence[dict[str, Any]],
    strikes: Sequence[dict[str, Any]],
    *,
    contracts_requested: int = 0,
    strikes_requested: int = 0,
    generic_ticks: str = "",
    analysis_contract_rows: Sequence[Mapping[str, Any]] | None = None,
    authoritative_contract_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if type(contracts_requested) is not int or contracts_requested < 0:
        raise ValueError("GEX requested contract count must be a non-negative integer")
    if type(strikes_requested) is not int or strikes_requested < 0:
        raise ValueError("GEX requested strike count must be a non-negative integer")
    option_rows = list(contract_rows or [])
    strike_rows = list(strikes or [])
    if option_rows:
        _, rights_by_series_strike = _gex_contract_series_scope(option_rows)
        facts_by_row = [require_gex_contract_market_facts(row) for row in option_rows]
        gamma_rows = [
            row
            for row, facts in zip(option_rows, facts_by_row, strict=True)
            if facts["gamma"] is not None
        ]
        oi_rows = [
            row
            for row, facts in zip(option_rows, facts_by_row, strict=True)
            if facts["open_interest"] is not None
        ]
        oi_unavailable_rows = [
            row
            for row, facts in zip(option_rows, facts_by_row, strict=True)
            if facts["open_interest"] is None
        ]
        numerically_usable_rows = _qualified_gex_contract_rows(option_rows)
        usable_rows = (
            [dict(row) for row in analysis_contract_rows]
            if analysis_contract_rows is not None
            else numerically_usable_rows
        )
        if _qualified_gex_contract_rows(usable_rows) != usable_rows:
            raise ValueError("GEX diagnostics analysis rows must be complete exact C/P pairs")
        authoritative_rows = (
            [dict(row) for row in authoritative_contract_rows]
            if authoritative_contract_rows is not None
            else []
        )
        if (
            authoritative_rows
            and _qualified_gex_contract_rows(authoritative_rows) != authoritative_rows
        ):
            raise ValueError("GEX diagnostics authoritative rows must be complete exact C/P pairs")
        expected_pairs = contracts_requested // 2 if contracts_requested > 0 else 0
        received_pairs = sum(
            1 for rights in rights_by_series_strike.values() if rights == {"C", "P"}
        )
        usable_pairs = len(usable_rows) // 2
        unpaired_series_strikes = sum(
            1 for rights in rights_by_series_strike.values() if rights != {"C", "P"}
        )
        nonzero_gex_rows = [
            row
            for row, facts in zip(option_rows, facts_by_row, strict=True)
            if facts["gex"] is not None and abs(facts["gex"]) > 0
        ]
        call_oi_total = sum(
            float(facts["open_interest"])
            for row, facts in zip(option_rows, facts_by_row, strict=True)
            if row.get("right") == "C" and facts["open_interest"] is not None
        )
        put_oi_total = sum(
            float(facts["open_interest"])
            for row, facts in zip(option_rows, facts_by_row, strict=True)
            if row.get("right") == "P" and facts["open_interest"] is not None
        )
        greek_sources = sorted(
            {
                str(row.get("greek_source") or "")
                for row in option_rows
                if str(row.get("greek_source") or "")
            }
        )
        return {
            "diag_contracts_requested": contracts_requested,
            "diag_strikes_requested": strikes_requested,
            "diag_option_rows": len(option_rows),
            "diag_strike_rows": len(strike_rows),
            "diag_gamma_rows": len(gamma_rows),
            "diag_usable_gamma_rows": len(usable_rows),
            "diag_open_interest_rows": len(oi_rows),
            "diag_open_interest_unavailable_rows": len(oi_unavailable_rows),
            "diag_usable_gex_rows": len(usable_rows),
            "diag_numeric_usable_gex_rows": len(numerically_usable_rows),
            "diag_authoritative_gex_rows": len(authoritative_rows),
            "diag_nonzero_gex_rows": len(nonzero_gex_rows),
            "diag_expected_pair_count": expected_pairs,
            "diag_received_pair_count": received_pairs,
            "diag_usable_pair_count": usable_pairs,
            "diag_numeric_usable_pair_count": (len(numerically_usable_rows) // 2),
            "diag_authoritative_pair_count": len(authoritative_rows) // 2,
            "diag_unpaired_series_strikes": unpaired_series_strikes,
            "diag_call_oi_total": round(call_oi_total, 4),
            "diag_put_oi_total": round(put_oi_total, 4),
            "diag_greek_sources": ",".join(greek_sources),
            "diag_generic_ticks": str(generic_ticks),
            "diag_collection_timeout": False,
        }
    call_oi_total = sum(_num(row.get("call_oi")) for row in strike_rows)
    put_oi_total = sum(_num(row.get("put_oi")) for row in strike_rows)
    nonzero_gex_rows = [row for row in strike_rows if abs(_num(row.get("abs_gex"))) > 0]
    gamma_rows = [
        row
        for row in strike_rows
        if abs(_num(row.get("call_gamma"))) > 0 or abs(_num(row.get("put_gamma"))) > 0
    ]
    return {
        "diag_contracts_requested": contracts_requested,
        "diag_strikes_requested": strikes_requested,
        "diag_option_rows": 0,
        "diag_strike_rows": len(strike_rows),
        "diag_gamma_rows": len(gamma_rows),
        "diag_usable_gamma_rows": len(gamma_rows),
        "diag_open_interest_rows": sum(
            1 for row in strike_rows if _num(row.get("call_oi")) > 0 or _num(row.get("put_oi")) > 0
        ),
        "diag_open_interest_unavailable_rows": 0,
        "diag_usable_gex_rows": len(nonzero_gex_rows),
        "diag_numeric_usable_gex_rows": len(nonzero_gex_rows),
        "diag_authoritative_gex_rows": 0,
        "diag_nonzero_gex_rows": len(nonzero_gex_rows),
        "diag_expected_pair_count": 0,
        "diag_received_pair_count": 0,
        "diag_usable_pair_count": 0,
        "diag_numeric_usable_pair_count": 0,
        "diag_authoritative_pair_count": 0,
        "diag_unpaired_series_strikes": 0,
        "diag_call_oi_total": round(call_oi_total, 4),
        "diag_put_oi_total": round(put_oi_total, 4),
        "diag_greek_sources": "",
        "diag_generic_ticks": str(generic_ticks),
        "diag_collection_timeout": False,
    }


def _gex_frame_quality(
    diagnostics: Mapping[str, Any],
    strikes: Sequence[Mapping[str, Any]],
) -> tuple[bool, str, dict[str, Any]]:
    count_fields = (
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
    exact_counts: dict[str, int] = {}
    for field in count_fields:
        value = diagnostics.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"GEX diagnostics {field} must be an exact non-negative integer")
        exact_counts[field] = value
    optional_count_fields = (
        "diag_numeric_usable_gex_rows",
        "diag_authoritative_gex_rows",
        "diag_numeric_usable_pair_count",
        "diag_authoritative_pair_count",
    )
    optional_counts: dict[str, int] = {}
    for field in optional_count_fields:
        value = diagnostics.get(field)
        if value is None:
            continue
        if type(value) is not int or value < 0:
            raise ValueError(f"GEX diagnostics {field} must be an exact non-negative integer")
        optional_counts[field] = value
    collection_timed_out = diagnostics.get("diag_collection_timeout")
    if type(collection_timed_out) is not bool:
        raise ValueError("GEX diagnostics collection timeout must be an exact boolean")
    contracts_requested = exact_counts["diag_contracts_requested"]
    strikes_requested = exact_counts["diag_strikes_requested"]
    gamma_rows = exact_counts["diag_usable_gamma_rows"]
    oi_rows = exact_counts["diag_open_interest_rows"]
    usable_rows = exact_counts["diag_usable_gex_rows"]
    nonzero_rows = exact_counts["diag_nonzero_gex_rows"]
    option_rows = exact_counts["diag_option_rows"]
    expected_pairs = exact_counts["diag_expected_pair_count"]
    received_pairs = exact_counts["diag_received_pair_count"]
    usable_pairs = exact_counts["diag_usable_pair_count"]
    unpaired_series_strikes = exact_counts["diag_unpaired_series_strikes"]
    qualified_strikes = len(strikes)
    nonzero_strikes = sum(1 for row in strikes if abs(float(row["abs_gex"])) > 0.0)

    coverage_known = contracts_requested > 0
    requested_strike_scope_known = strikes_requested > 0
    pair_scope_known = (
        coverage_known
        and contracts_requested % 2 == 0
        and expected_pairs == contracts_requested // 2
    )
    minimum_pair_count = (
        max(
            1,
            math.ceil(expected_pairs * GEX_ANALYSIS_MIN_PAIR_COVERAGE),
        )
        if expected_pairs
        else 0
    )
    pair_counts_consistent = (
        usable_pairs <= received_pairs <= expected_pairs and usable_rows == usable_pairs * 2
    )
    if optional_counts:
        pair_counts_consistent = bool(
            pair_counts_consistent
            and set(optional_counts) == set(optional_count_fields)
            and optional_counts["diag_authoritative_pair_count"]
            <= optional_counts["diag_numeric_usable_pair_count"]
            <= received_pairs
            and optional_counts["diag_authoritative_gex_rows"]
            == optional_counts["diag_authoritative_pair_count"] * 2
            and optional_counts["diag_numeric_usable_gex_rows"]
            == optional_counts["diag_numeric_usable_pair_count"] * 2
            and usable_pairs
            == (
                optional_counts["diag_authoritative_pair_count"]
                if optional_counts["diag_authoritative_pair_count"] >= minimum_pair_count
                else optional_counts["diag_numeric_usable_pair_count"]
            )
        )
    selected_strike_scope_complete = (
        requested_strike_scope_known and 0 < qualified_strikes <= strikes_requested
    )
    missing_requested_strikes = max(strikes_requested - qualified_strikes, 0)
    excess_requested_strikes = max(qualified_strikes - strikes_requested, 0)
    exact_pair_universe = (
        pair_scope_known
        and pair_counts_consistent
        and option_rows == contracts_requested
        and received_pairs == expected_pairs
        and usable_pairs == expected_pairs
        and unpaired_series_strikes == 0
    )
    min_pairs = minimum_pair_count
    min_rows = min_pairs * 2
    min_strikes = 1 if strikes else 0
    pair_coverage_ratio = usable_pairs / expected_pairs if expected_pairs > 0 else 0.0
    qualified_universe_complete = (
        bool(strikes)
        and nonzero_strikes >= min_strikes
        and pair_scope_known
        and pair_counts_consistent
        and expected_pairs > 0
        and usable_pairs >= min_pairs
        and gamma_rows >= min_rows
        and oi_rows >= min_rows
        and usable_rows >= min_rows
        and nonzero_rows > 0
    )
    publishable = qualified_universe_complete and selected_strike_scope_complete
    reason = "complete exact GEX frame"
    quality_code = "COMPLETE"
    contract_quality_detail = (
        f"usable rows {usable_rows}/{contracts_requested}, gamma rows {gamma_rows}, "
        f"OI rows {oi_rows}, nonzero strikes {nonzero_strikes}/{qualified_strikes}; "
        f"paired series-strikes {usable_pairs}/{expected_pairs} "
        f"(received {received_pairs}, unpaired {unpaired_series_strikes}); "
        f"required at least {min_pairs} complete pairs "
        f"({GEX_ANALYSIS_MIN_PAIR_COVERAGE:.0%} coverage), {min_rows} complete rows, "
        f"and {min_strikes} nonzero strikes"
    )
    if collection_timed_out and not qualified_universe_complete:
        quality_code = "COLLECTION_TIMEOUT"
        reason = (
            f"provider collection timed out before frame confirmation; {contract_quality_detail}"
        )
    elif not requested_strike_scope_known:
        quality_code = "REQUESTED_STRIKE_SCOPE_UNKNOWN"
        reason = (
            f"requested strike scope is unavailable; qualified strikes {qualified_strikes}; "
            f"a typed positive requested strike count is required; {contract_quality_detail}"
        )
    elif not qualified_universe_complete:
        quality_code = "INCOMPLETE_CONTRACT_FRAME"
        reason = contract_quality_detail
    elif collection_timed_out or not exact_pair_universe:
        quality_code = (
            "SUFFICIENT_PAIR_COVERAGE_AFTER_TIMEOUT"
            if collection_timed_out
            else "SUFFICIENT_PAIR_COVERAGE"
        )
        reason = (
            f"provider-confirmed degraded GEX universe accepted at "
            f"{pair_coverage_ratio:.1%} pair coverage; {contract_quality_detail}"
        )
    elif excess_requested_strikes > 0:
        quality_code = "REQUESTED_STRIKE_OVERFLOW"
        reason = (
            f"selected strike universe {qualified_strikes} exceeds requested limit "
            f"{strikes_requested} by {excess_requested_strikes}; {contract_quality_detail}"
        )
    elif missing_requested_strikes > 0:
        reason = (
            f"complete provider-selected universe: {qualified_strikes} available strikes "
            f"within requested limit {strikes_requested}; {contract_quality_detail}"
        )
    return (
        publishable,
        reason,
        {
            "diag_frame_publishable": publishable,
            "diag_frame_quality_code": quality_code,
            "diag_frame_quality_reason": reason,
            "diag_frame_min_usable_rows": min_rows,
            "diag_frame_min_usable_pairs": min_pairs,
            "diag_frame_pair_coverage_ratio": round(pair_coverage_ratio, 6),
            "diag_frame_pair_coverage_threshold": GEX_ANALYSIS_MIN_PAIR_COVERAGE,
            "diag_frame_pair_scope_known": pair_scope_known,
            "diag_frame_pair_counts_consistent": pair_counts_consistent,
            "diag_frame_min_nonzero_strikes": min_strikes,
            "diag_frame_requested_strike_limit": strikes_requested,
            "diag_frame_qualified_strikes": qualified_strikes,
            "diag_frame_missing_requested_strikes": missing_requested_strikes,
            "diag_frame_excess_requested_strikes": excess_requested_strikes,
            "diag_frame_selected_strike_scope_complete": selected_strike_scope_complete,
            "diag_frame_qualified_universe_complete": qualified_universe_complete,
            "diag_frame_nonzero_strikes": nonzero_strikes,
            "diag_frame_exact_pair_universe": exact_pair_universe,
        },
    )


def _gex_execution_input_quality(
    *,
    captured_at: datetime,
    meta: Mapping[str, Any],
    contract_rows: Sequence[Mapping[str, Any]],
    market_data_entitlement: str,
) -> tuple[bool, dict[str, Any]]:
    """Prove field-specific authority without conflating it with display completeness."""

    spot_observation = require_gex_underlying_quote_observation(meta.get("spot_observation"))
    spot = finite_number_or_none(meta.get("spot"))
    if spot is None or not math.isclose(
        spot,
        spot_observation["price"],
        rel_tol=1e-12,
        abs_tol=1e-8,
    ):
        raise ValueError("GEX spot observation must exactly match the calculation spot")

    spot_evidence_at = (
        parse_gex_timestamp(spot_observation["received_at"])
        if spot_observation["price_source"] == "bid_ask_mid"
        else parse_gex_timestamp(spot_observation["provider_ts"])
        if spot_observation["price_source"] == "last"
        else None
    )
    spot_age_seconds = (
        (captured_at - spot_evidence_at).total_seconds() if spot_evidence_at is not None else None
    )
    spot_frame_eligible = bool(
        spot_observation["market_data_entitlement"] != "unknown"
        and spot_observation["market_data_entitlement"] == market_data_entitlement
        and spot_observation["price_source"] in {"bid_ask_mid", "last"}
        and (
            spot_observation["time_basis"] == "client_receive"
            if spot_observation["price_source"] == "bid_ask_mid"
            else spot_observation["time_basis"] == "provider_event"
        )
        and spot_age_seconds is not None
        and 0.0 <= spot_age_seconds <= GEX_EXECUTION_SPOT_MAX_AGE_SECONDS
    )
    spot_eligible = bool(
        spot_frame_eligible and spot_observation["market_data_entitlement"] == "live"
    )

    for row in contract_rows:
        require_gex_contract_observation(row)
        require_gex_contract_market_facts(row, spot=spot)
        greek_source = row.get("greek_source")
        if not isinstance(greek_source, str):
            raise ValueError("GEX contract greek_source must be an exact string")

    required_contract_rows = len(contract_rows)
    eligible_contract_rows = len(
        _qualified_gex_contract_rows(
            contract_rows,
            authority_at=captured_at,
        )
    )
    authoritative = bool(
        market_data_entitlement == "live"
        and spot_eligible
        and required_contract_rows > 0
        and eligible_contract_rows == required_contract_rows
    )
    quality_code = "EXECUTION_INPUTS_ELIGIBLE" if authoritative else "DISPLAY_ONLY_INPUTS"
    return authoritative, {
        "diag_execution_input_authoritative": authoritative,
        "diag_execution_input_quality_code": quality_code,
        "diag_execution_required_gex_rows": required_contract_rows,
        "diag_execution_eligible_gex_rows": eligible_contract_rows,
        "diag_frame_spot_eligible": spot_frame_eligible,
        "diag_underlying_spot_price_source": spot_observation["price_source"],
        "diag_underlying_spot_time_basis": spot_observation["time_basis"],
        "diag_underlying_spot_market_data_entitlement": spot_observation["market_data_entitlement"],
        "diag_execution_spot_eligible": spot_eligible,
        "diag_execution_spot_age_seconds": (
            round(spot_age_seconds, 3) if spot_age_seconds is not None else None
        ),
        "diag_execution_model_max_age_seconds": (GEX_EXECUTION_MODEL_INPUT_MAX_AGE_SECONDS),
        "diag_execution_open_interest_validity": ("provider_subscription_previous_settlement"),
        "diag_execution_spot_max_age_seconds": (GEX_EXECUTION_SPOT_MAX_AGE_SECONDS),
    }
