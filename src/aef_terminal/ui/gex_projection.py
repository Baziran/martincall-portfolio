from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.constants import GEX_CONTEXT_MAX_LEVELS
from aef_terminal.data.gex.contracts import (
    GexComparisonLaneKey,
    gex_capture_revision_at,
    gex_comparison_lane_key,
    require_exact_gex_capture_lane,
    require_gex_comparison_scope,
    require_gex_market_data_entitlement,
    require_gex_option_universe_expiry,
)
from aef_terminal.data.gex.history import attach_gex_history_intervals
from aef_terminal.data.gex.payload_contract import require_gex_levels
from aef_terminal.data.gex.utils import parse_gex_timestamp


GEX_CHART_HISTORY_DENSE_HOURS = 6
GEX_CHART_HISTORY_MEDIUM_HOURS = 24
GEX_CHART_HISTORY_MEDIUM_BUCKET_MINUTES = 15
GEX_CHART_HISTORY_OLD_BUCKET_MINUTES = 30
_GEX_HISTORY_WIRE_FIELDS = (
    "captured_at",
    "timestamp_unix_ms",
    "capture_mode",
    "source",
    "capture_revision",
    "option_universe_expires_at",
    "market_data_entitlement",
    "open_interest_as_of",
    "comparison_scope",
    "spot",
    "gamma_flip",
    "levels",
    "valid_until_unix_ms",
)
_GEX_CURRENT_CHART_FIELDS = (
    "ok",
    "provider_symbol",
    "instrument_id",
    "route_fingerprint",
    "source",
    "capture_mode",
    "capture_revision",
    "status",
    "message",
    "error",
    "backoff",
    "degraded",
    "frame_complete",
    "decision_authoritative",
    "option_universe_expires_at",
    "market_data_entitlement",
    "diagnostics",
    "captured_at",
    "spot",
    "global_gamma_regime",
    "gamma_flip",
    "open_interest_as_of",
    "levels",
    "expiry_profile",
    "visibility_summary",
    "comparison_scope",
    "history",
    "history_status",
    "live",
    "latest_attempt",
    "preserved_context",
    "display_context_source",
    "display_context_captured_at",
    "request_seconds",
)


def project_gex_history_for_chart(
    history: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return a bounded chart read-model with explicit interval/sample semantics."""

    source_rows: list[dict[str, Any]] = []
    for row in history:
        if not isinstance(row, Mapping):
            raise ValueError("GEX chart history requires mapping rows")
        timestamp_unix_ms = row.get("timestamp_unix_ms")
        if type(timestamp_unix_ms) is not int or timestamp_unix_ms <= 0:
            raise ValueError("GEX chart history requires an exact positive timestamp")
        source_rows.append(dict(row))
    if not source_rows:
        return []
    newest_ms = max(int(row["timestamp_unix_ms"]) for row in source_rows)
    valid_until_candidates = [
        row["valid_until_unix_ms"]
        for row in source_rows
        if type(row.get("valid_until_unix_ms")) is int and row["valid_until_unix_ms"] > 0
    ]
    interval_end_ms = max([newest_ms, *valid_until_candidates])
    interval_rows = attach_gex_history_intervals(
        source_rows,
        datetime.fromtimestamp(interval_end_ms / 1000, tz=UTC),
    )
    timestamped = [
        (index, int(row["timestamp_unix_ms"]), row) for index, row in enumerate(interval_rows)
    ]
    dense_ms = GEX_CHART_HISTORY_DENSE_HOURS * 60 * 60 * 1000
    medium_ms = GEX_CHART_HISTORY_MEDIUM_HOURS * 60 * 60 * 1000
    selected_indexes = {timestamped[0][0], timestamped[-1][0]}
    bucket_indexes: dict[tuple[int, int, str, GexComparisonLaneKey], int] = {}
    for index, timestamp_ms, row in timestamped:
        age_ms = max(0, newest_ms - timestamp_ms)
        if age_ms <= dense_ms:
            selected_indexes.add(index)
            continue
        bucket_minutes = (
            GEX_CHART_HISTORY_MEDIUM_BUCKET_MINUTES
            if age_ms <= medium_ms
            else GEX_CHART_HISTORY_OLD_BUCKET_MINUTES
        )
        bucket_ms = bucket_minutes * 60 * 1000
        comparison_scope = row.get("comparison_scope")
        if not isinstance(comparison_scope, Mapping):
            raise ValueError("GEX chart history requires a comparison scope")
        bucket_indexes[
            (
                bucket_minutes,
                timestamp_ms // bucket_ms,
                row["source"],
                gex_comparison_lane_key(comparison_scope),
            )
        ] = index
    selected_indexes.update(bucket_indexes.values())

    projected: list[dict[str, Any]] = []
    for index, timestamp_ms, row in timestamped:
        if index not in selected_indexes:
            continue
        age_ms = max(0, newest_ms - timestamp_ms)
        if age_ms <= dense_ms:
            projection_mode = "interval"
            projection_bucket_minutes = None
        else:
            projection_mode = "sample"
            projection_bucket_minutes = (
                GEX_CHART_HISTORY_MEDIUM_BUCKET_MINUTES
                if age_ms <= medium_ms
                else GEX_CHART_HISTORY_OLD_BUCKET_MINUTES
            )
        missing_wire_fields = [field for field in _GEX_HISTORY_WIRE_FIELDS if field not in row]
        if missing_wire_fields:
            raise ValueError(f"GEX chart history is missing wire fields: {missing_wire_fields}")
        source, capture_mode = require_exact_gex_capture_lane(
            source=row["source"],
            capture_mode=row["capture_mode"],
        )
        captured_at = parse_gex_timestamp(row["captured_at"])
        capture_revision = parse_gex_timestamp(row["capture_revision"])
        if captured_at is None or capture_revision is None:
            raise ValueError("GEX chart history requires canonical aware timestamps")
        if int(captured_at.timestamp() * 1000) != timestamp_ms:
            raise ValueError("GEX chart history timestamp disagrees with captured_at")
        if capture_revision != gex_capture_revision_at(
            captured_at,
            capture_mode=capture_mode,
        ):
            raise ValueError("GEX chart history capture revision is invalid")
        valid_until_ms = row["valid_until_unix_ms"]
        if type(valid_until_ms) is not int or valid_until_ms <= timestamp_ms:
            raise ValueError("GEX chart history requires a forward validity interval")
        option_universe_expires_at = require_gex_option_universe_expiry(
            row["option_universe_expires_at"],
            captured_at=captured_at,
            allow_unknown=True,
        )
        if option_universe_expires_at is not None and valid_until_ms > int(
            option_universe_expires_at.timestamp() * 1000
        ):
            raise ValueError("GEX chart history validity exceeds the exact option universe expiry")
        entitlement = require_gex_market_data_entitlement(row["market_data_entitlement"])
        comparison_scope = require_gex_comparison_scope(
            row["comparison_scope"],
            capture_mode=capture_mode,
        )
        if comparison_scope["market_data_entitlement"] != entitlement:
            raise ValueError("GEX chart history entitlement disagrees with its scope")
        if row["open_interest_as_of"] != "previous_settlement":
            raise ValueError("GEX chart history requires typed open-interest provenance")
        spot = row["spot"]
        if (
            isinstance(spot, bool)
            or not isinstance(spot, (int, float))
            or not math.isfinite(float(spot))
            or float(spot) <= 0
        ):
            raise ValueError("GEX chart history requires a finite positive spot")
        gamma_flip = row["gamma_flip"]
        if gamma_flip is not None and (
            isinstance(gamma_flip, bool)
            or not isinstance(gamma_flip, (int, float))
            or not math.isfinite(float(gamma_flip))
            or float(gamma_flip) <= 0
        ):
            raise ValueError("GEX chart history gamma flip must be null or positive")
        wire_row = {field: row[field] for field in _GEX_HISTORY_WIRE_FIELDS}
        wire_row["source"] = source
        wire_row["capture_mode"] = capture_mode
        wire_row["option_universe_expires_at"] = (
            option_universe_expires_at.isoformat()
            if option_universe_expires_at is not None
            else None
        )
        wire_row["market_data_entitlement"] = entitlement
        wire_row["comparison_scope"] = comparison_scope
        wire_row["spot"] = float(spot)
        wire_row["gamma_flip"] = float(gamma_flip) if gamma_flip is not None else None
        wire_row["projection_mode"] = projection_mode
        wire_row["projection_bucket_minutes"] = projection_bucket_minutes
        raw_levels = row.get("levels")
        if raw_levels is None:
            raw_levels = ()
        if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
            raise ValueError("GEX chart history levels must be a sequence")
        if any(not isinstance(level, Mapping) for level in raw_levels):
            raise ValueError("GEX chart history requires mapping levels")
        wire_row["levels"] = require_gex_levels(
            raw_levels,
            max_levels=GEX_CONTEXT_MAX_LEVELS,
            spot=float(spot),
        )
        projected.append(wire_row)

    return projected


def project_gex_context_for_chart(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build the explicit browser read-model and project its history."""

    if not isinstance(payload, Mapping):
        raise TypeError("GEX chart context must be a mapping")
    out = {field: payload[field] for field in _GEX_CURRENT_CHART_FIELDS if field in payload}
    if "option_universe_expires_at" in out or (
        type(out.get("decision_authoritative")) is bool and "captured_at" in out
    ):
        captured_at = parse_gex_timestamp(out.get("captured_at"))
        option_universe_expires_at = require_gex_option_universe_expiry(
            out.get("option_universe_expires_at"),
            captured_at=captured_at,
            allow_unknown=True,
        )
        out["option_universe_expires_at"] = (
            option_universe_expires_at.isoformat()
            if option_universe_expires_at is not None
            else None
        )
        if out.get("decision_authoritative") is True and (
            option_universe_expires_at is None or option_universe_expires_at <= datetime.now(tz=UTC)
        ):
            out["decision_authoritative"] = False
            out["degraded"] = True
            out["status"] = "stale"
            out["message"] = (
                "GEX option universe has no exact active provider expiry; "
                "showing levels as display-only."
                if option_universe_expires_at is None
                else "GEX option universe expired at "
                f"{option_universe_expires_at.isoformat()}; showing levels "
                "as display-only."
            )
    history = out.get("history")
    if history is not None:
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            raise ValueError("GEX chart context history must be a sequence")
        out["history"] = project_gex_history_for_chart(history)
    return out
