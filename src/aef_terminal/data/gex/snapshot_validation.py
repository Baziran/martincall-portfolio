from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aef_terminal.data.gex.contracts import (
    gex_capture_revision_at,
    gex_comparison_scope_from_payload,
    require_exact_gex_capture_lane,
    require_gex_comparison_scope,
    require_gex_contract_market_facts,
    require_gex_contract_observation,
    require_gex_market_data_entitlement,
    require_gex_option_activity,
    require_gex_option_reference,
    require_gex_option_series_identity,
    require_gex_option_universe_expiry,
    require_gex_underlying_quote_observation,
    require_gex_valuation_rates,
)
from aef_terminal.data.gex.math import compute_gamma_flip
from aef_terminal.data.gex.payload import (
    _aggregate_by_strike,
    _expiry_profile_from_contracts,
    _gex_request_meta,
    _levels_from_strikes,
    _select_gex_levels,
)
from aef_terminal.data.gex.payload_contract import (
    _select_validated_gex_levels,
    GEX_RAW_STRIKE_REDUNDANT_FIELDS,
    _GEX_LEVEL_FIELDS,
    _GEX_RAW_META_TIMESTAMP_FIELDS,
    require_gex_expiry_profile,
    require_gex_levels,
)
from aef_terminal.data.gex.quality import (
    _gex_contract_series_scope,
    _gex_execution_input_quality,
    _gex_frame_quality,
    _qualified_gex_contract_rows,
)
from aef_terminal.data.gex.utils import finite_number_or_none, parse_gex_timestamp
from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
)

_GEX_SNAPSHOT_OPTION_ACTIVITY_STATUSES = frozenset(
    {
        "BASELINING",
        "READY",
        "NEED_PREVIOUS",
        "SCOPE_CHANGED",
        "GAP",
        "MISSING_INPUT",
        "MISSING_OVERLAP",
        "COUNTER_RESET",
    }
)

_GEX_SNAPSHOT_REQUIRED_FIELDS = frozenset(
    {
        "captured_at",
        "capture_revision",
        "source",
        "capture_mode",
        "comparison_scope",
        "frame_complete",
        "decision_authoritative",
        "market_data_entitlement",
        "open_interest_as_of",
        "spot",
        "gamma_flip",
        "levels",
        "call_wall",
        "put_wall",
        "net_gex",
        "global_gamma_regime",
        "option_activity",
        "option_activity_status",
    }
)


@dataclass(slots=True)
class _GexSnapshotContext:
    payload: dict[str, Any]
    projection: str
    captured_at: datetime
    capture_mode: str
    entitlement: str
    declared_decision_authoritative: bool
    decision_authoritative: bool
    option_universe_expiry_declared: bool
    option_universe_expires_at: datetime | None
    comparison_scope: dict[str, Any]
    spot: float
    gamma_flip: float | None
    levels: list[dict[str, Any]]


def _require_snapshot_shape(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("GEX snapshot payload must be a mapping")
    if {"profile", "nearest"}.intersection(value):
        raise ValueError("GEX snapshot payload contains deleted legacy level shapes")
    if not _GEX_SNAPSHOT_REQUIRED_FIELDS.issubset(value):
        raise ValueError("GEX snapshot payload is missing canonical fields")
    return dict(value)


def _require_canonical_snapshot_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("GEX snapshot timestamps must be exact strings")
    parsed = parse_gex_timestamp(value)
    if parsed is None:
        raise ValueError("GEX snapshot timestamps must be timezone-aware")
    if value != parsed.isoformat():
        raise ValueError("GEX snapshot timestamps must use canonical UTC form")
    return parsed


def _require_snapshot_capture(payload: Mapping[str, Any]) -> tuple[datetime, str]:
    captured_at = _require_canonical_snapshot_timestamp(
        payload.get("captured_at"),
        field="captured_at",
    )
    capture_revision = _require_canonical_snapshot_timestamp(
        payload.get("capture_revision"),
        field="capture_revision",
    )
    _source, capture_mode = require_exact_gex_capture_lane(
        source=payload.get("source"),
        capture_mode=payload.get("capture_mode"),
    )
    if capture_revision != gex_capture_revision_at(
        captured_at,
        capture_mode=capture_mode,
    ):
        raise ValueError("GEX snapshot capture revision disagrees with its lane")
    return captured_at, capture_mode


def _require_snapshot_authority(
    payload: dict[str, Any],
    *,
    projection: str,
    captured_at: datetime,
) -> tuple[str, bool, bool, datetime | None, bool]:
    entitlement = require_gex_market_data_entitlement(payload.get("market_data_entitlement"))
    if payload.get("frame_complete") is not True:
        raise ValueError("Persisted/history GEX snapshot must be frame-complete")
    decision_authoritative = payload.get("decision_authoritative")
    if type(decision_authoritative) is not bool:
        raise ValueError("GEX decision authority must be an exact boolean")
    declared_decision_authoritative = decision_authoritative
    option_universe_expiry_declared = "option_universe_expires_at" in payload
    option_universe_expires_at = require_gex_option_universe_expiry(
        payload.get("option_universe_expires_at"),
        captured_at=captured_at,
        allow_unknown=True,
    )
    if option_universe_expires_at is None and projection == "history":
        decision_authoritative = False
        payload["decision_authoritative"] = False
    payload["option_universe_expires_at"] = (
        option_universe_expires_at.isoformat() if option_universe_expires_at is not None else None
    )
    if decision_authoritative and entitlement != "live":
        raise ValueError("GEX decision authority requires live market-data entitlement")
    if payload.get("open_interest_as_of") != "previous_settlement":
        raise ValueError("GEX snapshot requires exact open-interest as-of semantics")
    return (
        entitlement,
        declared_decision_authoritative,
        decision_authoritative,
        option_universe_expires_at,
        option_universe_expiry_declared,
    )


def _require_snapshot_gamma_flip(payload: dict[str, Any]) -> float | None:
    raw_gamma_flip = payload.get("gamma_flip")
    gamma_flip = None if raw_gamma_flip is None else finite_number_or_none(raw_gamma_flip)
    if raw_gamma_flip is not None and (gamma_flip is None or gamma_flip <= 0):
        raise ValueError("GEX snapshot gamma_flip must be finite positive or null")
    payload["gamma_flip"] = gamma_flip
    return gamma_flip


def _require_snapshot_level_summary(
    payload: dict[str, Any],
    levels: list[dict[str, Any]],
) -> None:
    call_rows = [row for row in levels if float(row["call_gex"]) > 0]
    put_rows = [row for row in levels if float(row["put_gex"]) < 0]
    expected_net_gex = sum(float(row["net_gex"]) for row in levels)
    expected_numbers = {
        "call_wall": (
            float(max(call_rows, key=lambda row: float(row["call_gex"]))["price"])
            if call_rows
            else None
        ),
        "put_wall": (
            float(min(put_rows, key=lambda row: float(row["put_gex"]))["price"])
            if put_rows
            else None
        ),
        "net_gex": expected_net_gex,
    }
    for field, expected in expected_numbers.items():
        raw_value = payload.get(field)
        actual = None if raw_value is None else finite_number_or_none(raw_value)
        if raw_value is not None and actual is None:
            raise ValueError(f"GEX snapshot {field} must be numeric or null")
        if (actual is None) != (expected is None) or (
            actual is not None
            and expected is not None
            and not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-6)
        ):
            raise ValueError(f"GEX snapshot {field} disagrees with levels")
        payload[field] = actual
    expected_regime = (
        "UNKNOWN"
        if expected_net_gex == 0
        else "POSITIVE_ESTIMATE"
        if expected_net_gex > 0
        else "NEGATIVE_ESTIMATE"
    )
    if payload.get("global_gamma_regime") != expected_regime:
        raise ValueError("GEX snapshot gamma regime disagrees with net GEX")


def _require_snapshot_levels(
    payload: dict[str, Any],
    *,
    spot: float,
) -> list[dict[str, Any]]:
    raw_levels = payload.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError("GEX snapshot requires a non-empty typed levels list")
    levels = require_gex_levels(raw_levels, max_levels=len(raw_levels), spot=spot)
    payload["levels"] = levels
    _require_snapshot_level_summary(payload, levels)
    return levels


def _require_snapshot_option_activity(payload: dict[str, Any]) -> None:
    option_activity_status = payload.get("option_activity_status")
    if (
        not isinstance(option_activity_status, str)
        or option_activity_status not in _GEX_SNAPSHOT_OPTION_ACTIVITY_STATUSES
    ):
        raise ValueError("GEX snapshot option activity status is invalid")
    option_activity = payload.get("option_activity")
    if option_activity_status in {"BASELINING", "READY"}:
        payload["option_activity"] = require_gex_option_activity(
            option_activity,
            expected_status=option_activity_status,
        )
    elif option_activity is not None:
        raise ValueError("GEX snapshot option activity must be null for its status")


@dataclass(frozen=True, slots=True)
class _RawContractExpiryFacts:
    universe_expires_at: datetime | None
    series: set[tuple[str, str, str, float]]
    complete: bool


def _require_live_snapshot_session(
    payload: Mapping[str, Any],
    *,
    capture_mode: str,
) -> None:
    if capture_mode != "live":
        return
    live = payload.get("live")
    if not isinstance(live, Mapping):
        raise ValueError("Persisted live GEX snapshot requires typed session facts")
    live_started_value = live.get("started_at")
    live_session_id_value = live.get("session_id")
    live_started_at = parse_gex_timestamp(live_started_value)
    live_session_id = parse_gex_timestamp(live_session_id_value)
    if (
        not isinstance(live_started_value, str)
        or not isinstance(live_session_id_value, str)
        or live_started_at is None
        or live_session_id != live_started_at
        or live_started_value != live_started_at.isoformat()
        or live_session_id_value != live_started_value
    ):
        raise ValueError("Persisted live GEX snapshot requires one canonical session timestamp")


def _require_raw_snapshot_shape(
    payload: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
]:
    expiry_profile = require_gex_expiry_profile(payload.get("expiry_profile"))
    if not expiry_profile:
        raise ValueError("Persisted GEX snapshot requires an expiry profile")
    payload["expiry_profile"] = expiry_profile
    raw = payload.get("raw")
    if not isinstance(raw, Mapping) or set(raw) != {"meta", "strikes", "contracts"}:
        raise ValueError("Persisted GEX snapshot requires exact raw broker facts")
    raw_meta = raw.get("meta")
    raw_strikes = raw.get("strikes")
    raw_contracts = raw.get("contracts")
    if not isinstance(raw_meta, Mapping):
        raise ValueError("Persisted GEX raw broker facts have an invalid shape")
    if (
        not isinstance(raw_strikes, list)
        or not raw_strikes
        or any(not isinstance(row, Mapping) for row in raw_strikes)
    ):
        raise ValueError("Persisted GEX raw broker facts have an invalid shape")
    if (
        not isinstance(raw_contracts, list)
        or not raw_contracts
        or any(not isinstance(row, Mapping) for row in raw_contracts)
    ):
        raise ValueError("Persisted GEX raw broker facts have an invalid shape")
    return expiry_profile, dict(raw_meta), raw_strikes, raw_contracts


def _require_raw_snapshot_identity(
    payload: Mapping[str, Any],
    raw_meta: dict[str, Any],
    *,
    spot: float,
) -> dict[str, Any]:
    raw_spot = finite_number_or_none(raw_meta.get("spot"))
    if raw_spot is None or not math.isclose(
        raw_spot,
        spot,
        rel_tol=1e-12,
        abs_tol=1e-8,
    ):
        raise ValueError("Persisted GEX raw spot disagrees with snapshot spot")
    if set(raw_meta).intersection(_GEX_RAW_META_TIMESTAMP_FIELDS):
        raise ValueError("Persisted GEX raw metadata cannot duplicate captured_at")
    provider_symbol = payload.get("provider_symbol")
    if (
        not isinstance(provider_symbol, str)
        or not provider_symbol
        or provider_symbol != provider_symbol.strip()
        or raw_meta.get("provider_symbol") != provider_symbol
    ):
        raise ValueError("Persisted GEX raw provider symbol disagrees with snapshot")
    canonical_raw_meta = dict(raw_meta)
    canonical_raw_meta.update(require_gex_valuation_rates(raw_meta))
    return canonical_raw_meta


def _require_raw_contract_expiry_facts(
    raw_contracts: list[Mapping[str, Any]],
    *,
    captured_at: datetime,
) -> _RawContractExpiryFacts:
    expiries: list[datetime] = []
    series_set: set[tuple[str, str, str, float]] = set()
    expiry_by_series: dict[tuple[str, str, str, float], datetime] = {}
    complete = True
    for contract in raw_contracts:
        contract_series = require_gex_option_series_identity(contract)
        series_key = (
            contract_series["expiry"],
            contract_series["trading_class"],
            contract_series["exchange"],
            contract_series["multiplier"],
        )
        series_set.add(series_key)
        if contract.get("expiry_time_source") != "ibkr_contract_details":
            complete = False
            continue
        expiry_at = require_gex_option_universe_expiry(
            contract.get("expiry_at"),
            captured_at=captured_at,
            allow_unknown=True,
        )
        existing_expiry_at = expiry_by_series.get(series_key)
        if expiry_at is None:
            complete = False
        elif existing_expiry_at is not None and existing_expiry_at != expiry_at:
            raise ValueError(
                "Persisted GEX exact series has conflicting provider expiry timestamps"
            )
        else:
            expiry_by_series[series_key] = expiry_at
            expiries.append(expiry_at)
    complete = complete and len(expiries) == len(raw_contracts)
    return _RawContractExpiryFacts(
        universe_expires_at=min(expiries) if complete and expiries else None,
        series=series_set,
        complete=complete,
    )


def _reconcile_raw_option_universe_expiry(
    context: _GexSnapshotContext,
    *,
    raw_meta: Mapping[str, Any],
    raw_contracts: list[Mapping[str, Any]],
) -> None:
    meta_expiry = require_gex_option_universe_expiry(
        raw_meta.get("option_universe_expires_at"),
        captured_at=context.captured_at,
        allow_unknown=True,
    )
    contract_facts = _require_raw_contract_expiry_facts(
        raw_contracts,
        captured_at=context.captured_at,
    )
    if context.declared_decision_authoritative and not contract_facts.complete:
        raise ValueError("GEX decision authority requires complete exact raw contract expiry facts")
    comparison_series = {
        (
            series["expiry"],
            series["trading_class"],
            series["exchange"],
            series["multiplier"],
        )
        for series in context.comparison_scope["series"]
    }
    covers_comparison = bool(comparison_series) and comparison_series.issubset(
        contract_facts.series
    )
    candidates = [meta_expiry] if meta_expiry is not None else []
    if contract_facts.universe_expires_at is not None and (
        meta_expiry is not None or covers_comparison
    ):
        candidates.append(contract_facts.universe_expires_at)
    canonical_expiry = min(candidates) if candidates else None
    if (
        context.option_universe_expiry_declared
        and context.option_universe_expires_at != canonical_expiry
    ):
        raise ValueError("Persisted GEX option universe expiry disagrees with raw contracts")
    if (
        meta_expiry is not None
        and contract_facts.universe_expires_at is not None
        and meta_expiry > contract_facts.universe_expires_at
    ):
        raise ValueError("Persisted GEX raw universe expiry exceeds a contract expiry")
    if canonical_expiry is None:
        context.decision_authoritative = False
        context.payload["decision_authoritative"] = False
    context.payload["option_universe_expires_at"] = (
        canonical_expiry.isoformat() if canonical_expiry is not None else None
    )


def _require_raw_snapshot_request_meta(
    payload: Mapping[str, Any],
    raw_meta: dict[str, Any],
    *,
    spot: float,
    entitlement: str,
) -> tuple[dict[str, Any], int, int]:
    canonical_spot_observation = require_gex_underlying_quote_observation(
        raw_meta.get("spot_observation")
    )
    if (
        not isinstance(raw_meta.get("spot_observation"), Mapping)
        or dict(raw_meta["spot_observation"]) != canonical_spot_observation
        or not math.isclose(
            canonical_spot_observation["price"],
            spot,
            rel_tol=1e-12,
            abs_tol=1e-8,
        )
    ):
        raise ValueError("Persisted GEX spot observation is not canonical")
    raw_meta["spot_observation"] = canonical_spot_observation
    raw_entitlement = require_gex_market_data_entitlement(raw_meta.get("market_data_entitlement"))
    if raw_entitlement != entitlement:
        raise ValueError("Persisted GEX raw entitlement disagrees with snapshot")
    request_meta = payload.get("request_meta")
    if not isinstance(request_meta, Mapping) or dict(request_meta) != _gex_request_meta(raw_meta):
        raise ValueError("Persisted GEX request metadata disagrees with raw metadata")
    request_meta = dict(request_meta)
    strike_count = request_meta.get("strike_count")
    if type(strike_count) is not int or strike_count <= 0:
        raise ValueError("Persisted GEX snapshot requires a positive strike count")
    requested_strike_limit = request_meta.get("requested_strike_limit")
    if type(requested_strike_limit) is not int or requested_strike_limit < strike_count:
        raise ValueError(
            "Persisted GEX snapshot requires the requested strike limit covering its universe"
        )
    return request_meta, strike_count, requested_strike_limit


@dataclass(frozen=True, slots=True)
class _ValidatedRawContracts:
    rows: list[dict[str, Any]]
    con_ids: set[int]
    expiries: list[str]
    rights_by_series_strike: dict[Any, set[str]]


def _require_raw_snapshot_contracts(
    context: _GexSnapshotContext,
    raw_contracts: list[Mapping[str, Any]],
    *,
    canonical_raw_meta: dict[str, Any],
) -> _ValidatedRawContracts:
    canonical_contracts = [dict(row) for row in raw_contracts]
    con_ids: set[int] = set()
    for contract in canonical_contracts:
        con_id = parse_exact_positive_decimal_provider_id(contract.get("con_id"))
        if con_id <= 0:
            raise ValueError("Persisted GEX contract requires an exact positive con_id")
        if con_id in con_ids:
            raise ValueError("Persisted GEX contracts require unique con_id values")
        con_ids.add(con_id)
        facts = require_gex_contract_market_facts(contract, spot=context.spot)
        require_gex_option_reference(contract)
        require_gex_contract_observation(contract)
        if any(facts[field] is None for field in ("gamma", "open_interest", "gex", "abs_gex")):
            raise ValueError("Persisted GEX snapshot cannot contain incomplete exposure facts")
    execution_inputs_authoritative, _execution_quality = _gex_execution_input_quality(
        captured_at=context.captured_at,
        meta=canonical_raw_meta,
        contract_rows=canonical_contracts,
        market_data_entitlement=context.entitlement,
    )
    if context.decision_authoritative and not execution_inputs_authoritative:
        raise ValueError("GEX decision authority requires fresh live model and spot observations")
    expiries, rights_by_series_strike = _gex_contract_series_scope(canonical_contracts)
    if any(rights != {"C", "P"} for rights in rights_by_series_strike.values()):
        raise ValueError("Persisted GEX snapshot requires complete exact C/P pairs")
    if _qualified_gex_contract_rows(canonical_contracts) != canonical_contracts:
        raise ValueError("Persisted GEX snapshot requires numerically qualified exact C/P pairs")
    return _ValidatedRawContracts(
        rows=canonical_contracts,
        con_ids=con_ids,
        expiries=expiries,
        rights_by_series_strike=rights_by_series_strike,
    )


def _require_snapshot_diagnostic_counts(
    payload: Mapping[str, Any],
    raw_meta: Mapping[str, Any],
    *,
    raw_strike_count: int,
    selected_contract_count: int,
    selected_pair_count: int,
    requested_strike_limit: int,
) -> tuple[dict[str, Any], int]:
    persisted_diagnostics = payload.get("diagnostics")
    if not isinstance(persisted_diagnostics, Mapping):
        raise ValueError("Persisted GEX snapshot requires typed coverage diagnostics")
    diagnostics = dict(persisted_diagnostics)
    persisted_count_bindings = {
        "diag_strike_rows": raw_strike_count,
        "diag_usable_gamma_rows": selected_contract_count,
        "diag_usable_gex_rows": selected_contract_count,
        "diag_usable_pair_count": selected_pair_count,
    }
    for field, expected in persisted_count_bindings.items():
        if diagnostics.get(field) != expected:
            raise ValueError(f"Persisted GEX diagnostics {field} disagrees with raw facts")
    contracts_requested = diagnostics.get("diag_contracts_requested")
    expected_pair_count = diagnostics.get("diag_expected_pair_count")
    qualified_pair_count = raw_meta.get("qualified_pair_count")
    if (
        type(contracts_requested) is not int
        or contracts_requested <= 0
        or contracts_requested % 2 != 0
        or type(expected_pair_count) is not int
        or expected_pair_count != contracts_requested // 2
        or type(qualified_pair_count) is not int
        or qualified_pair_count != expected_pair_count
    ):
        raise ValueError("Persisted GEX snapshot lost the exact requested pair denominator")
    if diagnostics.get("diag_strikes_requested") != requested_strike_limit:
        raise ValueError(
            "Persisted GEX diagnostics requested strike limit disagrees with raw facts"
        )
    for field in (
        "diag_numeric_usable_gex_rows",
        "diag_authoritative_gex_rows",
        "diag_numeric_usable_pair_count",
        "diag_authoritative_pair_count",
    ):
        value = diagnostics.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(
                f"Persisted GEX diagnostics {field} must be an exact non-negative integer"
            )
    return diagnostics, contracts_requested


def _require_snapshot_coverage_diagnostics(
    context: _GexSnapshotContext,
    diagnostics: dict[str, Any],
    raw_strikes: list[Mapping[str, Any]],
    *,
    selected_pair_count: int,
    selected_contract_count: int,
) -> None:
    coverage_publishable, _coverage_reason, coverage_meta = _gex_frame_quality(
        diagnostics,
        raw_strikes,
    )
    if not coverage_publishable:
        raise ValueError("Persisted GEX snapshot does not satisfy canonical pair coverage")
    for field, expected in coverage_meta.items():
        if field != "diag_frame_quality_reason" and diagnostics.get(field) != expected:
            raise ValueError(f"Persisted GEX diagnostics {field} disagrees with canonical coverage")
    if context.decision_authoritative and (
        diagnostics["diag_authoritative_pair_count"] != selected_pair_count
        or diagnostics["diag_authoritative_gex_rows"] != selected_contract_count
    ):
        raise ValueError(
            "GEX decision authority requires the selected raw pair universe "
            "to match authoritative coverage diagnostics"
        )


def _require_snapshot_comparison_scope_diagnostics(
    context: _GexSnapshotContext,
    diagnostics: Mapping[str, Any],
    contracts: _ValidatedRawContracts,
    raw_meta: Mapping[str, Any],
    *,
    strike_count: int,
    contracts_requested: int,
) -> None:
    comparison_scope_source = diagnostics.get("diag_comparison_scope_source")
    if comparison_scope_source not in {
        "analysis_contract_rows",
        "provider_selected_contract_universe",
    }:
        raise ValueError("Persisted GEX comparison scope source is invalid")
    comparison_scope = context.comparison_scope
    if (
        comparison_scope["strike_count"] != strike_count
        or not set(contracts.expiries).issubset(comparison_scope["expiries"])
        or comparison_scope["futures_options"] != raw_meta.get("futures_options")
    ):
        raise ValueError("Persisted GEX comparison scope disagrees with raw facts")
    if (
        diagnostics.get("diag_comparison_scope_contracts")
        != len(comparison_scope["contract_con_ids"])
        or diagnostics.get("diag_comparison_scope_strikes") != comparison_scope["strike_count"]
    ):
        raise ValueError("Persisted GEX comparison scope diagnostics are incomplete")
    if comparison_scope_source == "provider_selected_contract_universe":
        if len(comparison_scope["contract_con_ids"]) != contracts_requested:
            raise ValueError("Persisted GEX provider-selected comparison scope is incomplete")
    elif set(comparison_scope["contract_con_ids"]) != contracts.con_ids or set(
        comparison_scope["expiries"]
    ) != set(contracts.expiries):
        raise ValueError(
            "Persisted analysis-row GEX comparison scope disagrees with analysis facts"
        )


def _require_rebuilt_snapshot_strikes_and_levels(
    context: _GexSnapshotContext,
    contracts: list[dict[str, Any]],
    raw_strikes: list[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
    *,
    strike_count: int,
) -> list[dict[str, Any]]:
    rebuilt_strikes = _aggregate_by_strike(contracts, context.spot)
    if (
        len(rebuilt_strikes) != diagnostics["diag_strike_rows"]
        or len(rebuilt_strikes) > strike_count
    ):
        raise ValueError("Persisted GEX analysis strike rows disagree with selected scope")
    canonical_rebuilt_strikes = [
        {
            key: item
            for key, item in row.items()
            if not str(key).startswith("_") and key not in GEX_RAW_STRIKE_REDUNDANT_FIELDS
        }
        for row in rebuilt_strikes
    ]
    if [dict(row) for row in raw_strikes] != canonical_rebuilt_strikes:
        raise ValueError("Persisted GEX aggregate strikes disagree with contracts")
    rebuilt_summary = _levels_from_strikes(rebuilt_strikes)
    rebuilt_levels = require_gex_levels(
        _select_gex_levels(
            rebuilt_strikes,
            context.spot,
            rebuilt_summary,
            max_levels=strike_count,
        ),
        max_levels=strike_count,
        spot=context.spot,
    )
    level_fields = _GEX_LEVEL_FIELDS - {"option_volume_context"}
    actual_level_facts = [{field: row[field] for field in level_fields} for row in context.levels]
    rebuilt_level_facts = [{field: row[field] for field in level_fields} for row in rebuilt_levels]
    if actual_level_facts != rebuilt_level_facts:
        raise ValueError("Persisted GEX levels disagree with raw aggregate strikes")
    for actual, rebuilt in zip(context.levels, rebuilt_levels, strict=True):
        if (
            actual["option_volume_context"]["current"]
            != rebuilt["option_volume_context"]["current"]
        ):
            raise ValueError("Persisted GEX level volume context disagrees with raw strikes")
    return canonical_rebuilt_strikes


def _require_rebuilt_snapshot_gamma_and_expiry(
    context: _GexSnapshotContext,
    contracts: list[dict[str, Any]],
    request_meta: Mapping[str, Any],
    canonical_raw_meta: Mapping[str, Any],
    expiry_profile: list[dict[str, Any]],
) -> None:
    rebuilt_gamma_flip = compute_gamma_flip(
        request_meta["futures_options"],
        contracts,
        context.spot,
        risk_free_rate=canonical_raw_meta["risk_free_rate"],
        dividend_yield=canonical_raw_meta["dividend_yield"],
    )
    gamma_flip = context.gamma_flip
    gamma_presence_mismatch = (gamma_flip is None) != (rebuilt_gamma_flip is None)
    gamma_value_mismatch = (
        gamma_flip is not None
        and rebuilt_gamma_flip is not None
        and not math.isclose(
            gamma_flip,
            rebuilt_gamma_flip,
            rel_tol=1e-12,
            abs_tol=1e-4,
        )
    )
    if gamma_presence_mismatch or gamma_value_mismatch:
        raise ValueError("Persisted GEX gamma_flip disagrees with raw contracts")
    if expiry_profile != _expiry_profile_from_contracts(contracts):
        raise ValueError("Persisted GEX expiry profile disagrees with contracts")


def require_gex_snapshot_payload(
    value: object,
    *,
    projection: str,
    max_levels: int | None = None,
) -> dict[str, Any]:
    """Validate one persisted snapshot or its explicit compact history projection."""

    if projection not in {"persisted", "history"}:
        raise ValueError("GEX snapshot projection must be persisted or history")
    if max_levels is not None and projection != "history":
        raise ValueError("Only a GEX history projection may limit admitted levels")
    payload = _require_snapshot_shape(value)
    captured_at, capture_mode = _require_snapshot_capture(payload)
    (
        entitlement,
        declared_decision_authoritative,
        decision_authoritative,
        option_universe_expires_at,
        option_universe_expiry_declared,
    ) = _require_snapshot_authority(
        payload,
        projection=projection,
        captured_at=captured_at,
    )

    comparison_scope = (
        gex_comparison_scope_from_payload(payload)
        if projection == "persisted"
        else require_gex_comparison_scope(
            payload.get("comparison_scope"),
            capture_mode=capture_mode,
        )
    )
    if comparison_scope["market_data_entitlement"] != entitlement:
        raise ValueError("GEX comparison entitlement disagrees with snapshot")
    spot = finite_number_or_none(payload.get("spot"))
    if spot is None or spot <= 0:
        raise ValueError("GEX snapshot requires a finite positive spot")
    gamma_flip = _require_snapshot_gamma_flip(payload)
    levels = _require_snapshot_levels(payload, spot=spot)
    _require_snapshot_option_activity(payload)
    context = _GexSnapshotContext(
        payload=payload,
        projection=projection,
        captured_at=captured_at,
        capture_mode=capture_mode,
        entitlement=entitlement,
        declared_decision_authoritative=declared_decision_authoritative,
        decision_authoritative=decision_authoritative,
        option_universe_expiry_declared=option_universe_expiry_declared,
        option_universe_expires_at=option_universe_expires_at,
        comparison_scope=comparison_scope,
        spot=spot,
        gamma_flip=gamma_flip,
        levels=levels,
    )

    if projection == "history":
        if "raw" in payload or "expiry_profile" in payload:
            raise ValueError("GEX history projection cannot contain full raw or expiry facts")
        if max_levels is not None:
            # Full levels and their summaries are admitted before selection;
            # malformed unselected levels must still reject the snapshot.
            payload["levels"] = _select_validated_gex_levels(levels, max_levels=max_levels)
        return payload

    _require_live_snapshot_session(payload, capture_mode=capture_mode)
    expiry_profile, raw_meta, raw_strikes, raw_contracts = _require_raw_snapshot_shape(payload)
    canonical_raw_meta = _require_raw_snapshot_identity(payload, raw_meta, spot=spot)
    _reconcile_raw_option_universe_expiry(
        context,
        raw_meta=raw_meta,
        raw_contracts=raw_contracts,
    )
    request_meta, strike_count, requested_strike_limit = _require_raw_snapshot_request_meta(
        payload,
        canonical_raw_meta,
        spot=spot,
        entitlement=entitlement,
    )
    contracts = _require_raw_snapshot_contracts(
        context,
        raw_contracts,
        canonical_raw_meta=canonical_raw_meta,
    )
    selected_pair_count = len(contracts.rights_by_series_strike)
    selected_contract_count = len(contracts.rows)
    canonical_diagnostics, contracts_requested = _require_snapshot_diagnostic_counts(
        payload,
        raw_meta,
        raw_strike_count=len(raw_strikes),
        selected_contract_count=selected_contract_count,
        selected_pair_count=selected_pair_count,
        requested_strike_limit=requested_strike_limit,
    )
    _require_snapshot_coverage_diagnostics(
        context,
        canonical_diagnostics,
        raw_strikes,
        selected_pair_count=selected_pair_count,
        selected_contract_count=selected_contract_count,
    )
    _require_snapshot_comparison_scope_diagnostics(
        context,
        canonical_diagnostics,
        contracts,
        raw_meta,
        strike_count=strike_count,
        contracts_requested=contracts_requested,
    )
    canonical_contracts = contracts.rows
    canonical_rebuilt_strikes = _require_rebuilt_snapshot_strikes_and_levels(
        context,
        canonical_contracts,
        raw_strikes,
        canonical_diagnostics,
        strike_count=strike_count,
    )
    _require_rebuilt_snapshot_gamma_and_expiry(
        context,
        canonical_contracts,
        request_meta,
        canonical_raw_meta,
        expiry_profile,
    )
    payload["raw"] = {
        "meta": canonical_raw_meta,
        "strikes": canonical_rebuilt_strikes,
        "contracts": canonical_contracts,
    }
    return payload
