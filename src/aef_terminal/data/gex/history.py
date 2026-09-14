from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakValueDictionary

from aef_terminal.data.gex.constants import (
    GEX_ANALYSIS_PAIR_MAX_SNAPSHOTS,
    GEX_CHART_HISTORY_SOURCES,
    GEX_CHART_HISTORY_HOURS,
    GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES,
    GEX_LIVE_SNAPSHOT_SOURCE,
    GEX_OPTION_ACTIVITY_BASELINE_MAX_SAMPLES,
    GEX_OPTION_ACTIVITY_MIN_BASELINE_SAMPLES,
    GEX_OPTION_ACTIVITY_WARMUP_HOURS,
    GEX_REQUEST_SNAPSHOT_SOURCE,
    GEX_SCHEDULER_INTERVAL_MINUTES,
    LOGGER,
    NY_TZ,
    _OPTION_TARGET_GEX_SNAPSHOT_WINDOW_MINUTES,
)
from aef_terminal.data.gex.contracts import (
    GexCaptureMode,
    GexComparisonLaneKey,
    GexComparisonScope,
    gex_capture_lane,
    gex_capture_revision_at,
    gex_comparison_lane_key,
    gex_comparison_scope_from_payload,
    require_exact_gex_capture_lane,
    require_gex_option_activity,
    require_gex_option_universe_expiry,
)
from aef_terminal.data.gex.payload_contract import (
    GEX_RAW_STRIKE_REDUNDANT_FIELDS,
    require_gex_levels,
)
from aef_terminal.data.gex.session import _GEX_RUNTIME
from aef_terminal.data.gex.snapshot_validation import (
    require_gex_snapshot_payload,
)
from aef_terminal.data.gex.utils import finite_number_or_none, _median_positive, parse_gex_timestamp
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.runtime.metrics import increment_metric

_GEX_PERSIST_LOCKS_GUARD = threading.Lock()
_GEX_PERSIST_LOCKS: WeakValueDictionary[tuple[str, str], threading.RLock] = WeakValueDictionary()


def _gex_persist_lock(instrument_id: str, route_fingerprint: str) -> threading.RLock:
    identity_key = (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
    )
    with _GEX_PERSIST_LOCKS_GUARD:
        lock = _GEX_PERSIST_LOCKS.get(identity_key)
        if lock is None:
            lock = threading.RLock()
            _GEX_PERSIST_LOCKS[identity_key] = lock
        return lock


def get_gex_history_series(
    provider_symbol: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    hours: int = GEX_CHART_HISTORY_HOURS,
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    now: datetime | None = None,
    store: Any | None = None,
    preloaded_rows: Sequence[dict[str, Any]] | None = None,
    sources: Sequence[str] = GEX_CHART_HISTORY_SOURCES,
) -> list[dict[str, Any]]:
    """Return compact historical GEX level snapshots without touching IBKR."""
    provider_symbol = require_exact_identity_text(
        provider_symbol,
        field="GEX_PROVIDER_SYMBOL",
    )
    if type(hours) is not int or not 1 <= hours <= 24 * 30:
        raise ValueError("GEX history hours must be an integer from 1 through 720")
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX history max_levels must be a positive integer")
    lookback_hours = hours
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("GEX history now must be timezone-aware")
    now_utc = current.astimezone(UTC)
    min_ts = now_utc - timedelta(hours=lookback_hours)
    db_series = _read_snapshot_history_from_db(
        provider_symbol,
        min_ts,
        now_utc,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        max_levels=max_levels,
        store=store,
        preloaded_rows=preloaded_rows,
        sources=sources,
    )
    return attach_gex_history_intervals(db_series, now_utc)


def gex_analysis_day_start(value: datetime) -> datetime:
    """Return the canonical New York calendar-day boundary for live analysis."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("GEX analysis day requires a timezone-aware timestamp")
    local = value.astimezone(NY_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def get_gex_analysis_pair(
    provider_symbol: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    capture_mode: GexCaptureMode,
    max_levels: int = GEX_CONTEXT_MAX_LEVELS,
    now: datetime | None = None,
    store: Any | None = None,
    preloaded_rows: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Read only the current NY-day pair required by ``gex_dynamics.latest``."""

    provider_symbol = require_exact_identity_text(
        provider_symbol,
        field="GEX_PROVIDER_SYMBOL",
    )
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX analysis max_levels must be a positive integer")
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("GEX analysis now must be timezone-aware")
    now_utc = current.astimezone(UTC)
    day_start = gex_analysis_day_start(now_utc)
    snapshot_source, exact_capture_mode = gex_capture_lane(capture_mode)
    if preloaded_rows is None:
        rows: Sequence[dict[str, Any]] = read_gex_snapshot_rows(
            day_start,
            now_utc,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            limit=GEX_ANALYSIS_PAIR_MAX_SNAPSHOTS,
            store=store,
            source=snapshot_source,
        )
    else:
        bounded_rows: list[tuple[datetime, dict[str, Any]]] = []
        for row in preloaded_rows:
            if not isinstance(row, dict):
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row must be an object")
            if row.get("source") != snapshot_source:
                continue
            captured_at = parse_gex_timestamp(row.get("captured_at"))
            if captured_at is None:
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row timestamp is invalid")
            if day_start <= captured_at <= now_utc:
                bounded_rows.append((captured_at, row))
        bounded_rows.sort(key=lambda item: item[0])
        rows = [row for _captured_at, row in bounded_rows[-GEX_ANALYSIS_PAIR_MAX_SNAPSHOTS:]]
    history = _read_snapshot_history_from_db(
        provider_symbol,
        day_start,
        now_utc,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        max_levels=max_levels,
        store=store,
        preloaded_rows=rows,
        sources=(snapshot_source,),
    )
    if any(row.get("capture_mode") != exact_capture_mode for row in history):
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: analysis pair capture lane disagrees")
    return history


def _cached_gex_history_projection(
    provider_symbol: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    history_hours: int,
    max_levels: int,
    store: Any | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read cached history while isolating old rows from the display lane."""

    try:
        history = get_gex_history_series(
            provider_symbol,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            hours=history_hours,
            max_levels=max_levels,
            store=store,
        )
    except ValueError as exc:
        history_error = str(exc)
        if not history_error.startswith("GEX_HISTORY_CONTRACT_INVALID:"):
            raise
        previous = _GEX_RUNTIME.error_for_route(
            instrument_id,
            route_fingerprint,
            sources=("cache_history",),
        )
        if previous.get("message") != history_error:
            LOGGER.warning(
                "Rejected noncanonical cached GEX history for %s (%s): %s",
                instrument_id,
                route_fingerprint,
                history_error,
            )
            increment_metric(
                "gex_cache_history_rejected_total",
                route_fingerprint=route_fingerprint,
            )
            _GEX_RUNTIME.record_error(
                instrument_id,
                route_fingerprint,
                history_error,
                provider_symbol=provider_symbol,
                source="cache_history",
            )
        return [], {
            "status": "error",
            "stale": False,
            "message": history_error,
        }
    _GEX_RUNTIME.clear_error(
        instrument_id,
        route_fingerprint,
        source="cache_history",
    )
    return history, {
        "status": "ok",
        "stale": False,
        "message": "",
    }


def attach_gex_history_intervals(
    series: list[dict[str, Any]], now_utc: datetime
) -> list[dict[str, Any]]:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("GEX history interval end must be timezone-aware")
    now_utc = now_utc.astimezone(UTC)
    out: list[dict[str, Any]] = []
    next_by_stream: dict[
        str,
        tuple[datetime, GexComparisonLaneKey],
    ] = {}
    prepared: list[
        tuple[
            int,
            dict[str, Any],
            datetime,
            str,
            GexComparisonLaneKey,
        ]
    ] = []
    previous_start: datetime | None = None
    for index, item in enumerate(series):
        start = parse_gex_timestamp(item.get("captured_at"))
        if start is None:
            raise ValueError("GEX history intervals require canonical captured_at")
        if previous_start is not None and start < previous_start:
            raise ValueError("GEX history intervals require chronological input")
        previous_start = start
        scope = item.get("comparison_scope")
        source = item.get("source")
        if source not in GEX_CHART_HISTORY_SOURCES:
            raise ValueError("GEX history intervals require an exact source stream")
        prepared.append((index, item, start, source, gex_comparison_lane_key(scope)))
    rows_by_index: dict[int, dict[str, Any]] = {}
    for index, item, start, source, lane in reversed(prepared):
        next_item = next_by_stream.get(source)
        ttl_end = start + timedelta(minutes=GEX_CONTEXT_STALE_MINUTES)
        option_universe_expires_at = require_gex_option_universe_expiry(
            item.get("option_universe_expires_at"),
            captured_at=start,
            allow_unknown=True,
        )
        end = min(
            next_item[0] if next_item is not None else now_utc,
            ttl_end,
            option_universe_expires_at or now_utc,
            now_utc,
        )
        if end <= start:
            raise ValueError(
                "GEX history intervals require strictly increasing timestamps "
                "within each source stream and an explicit tail end after the last row"
            )
        row = dict(item)
        row["valid_from"] = start.isoformat()
        row["valid_until"] = end.isoformat()
        row["valid_from_unix_ms"] = int(start.timestamp() * 1000)
        row["valid_until_unix_ms"] = int(end.timestamp() * 1000)
        row["duration_minutes"] = round((end - start).total_seconds() / 60.0, 2)
        row["scope_discontinuity"] = bool(next_item is not None and next_item[1] != lane)
        row["sequence"] = index
        rows_by_index[index] = row
        next_by_stream[source] = (start, lane)
    out.extend(rows_by_index[index] for index in range(len(prepared)))
    return out


def persist_gex_snapshot(
    provider_symbol: str,
    meta: dict[str, Any],
    strikes: Sequence[dict[str, Any]],
    payload: dict[str, Any],
    contract_rows: Sequence[dict[str, Any]] = (),
    *,
    instrument_id: str,
    route_fingerprint: str,
    store: Any | None = None,
    captured_at: datetime,
    capture_mode: str,
    live_bucket_at: datetime | None = None,
) -> bool:
    provider_symbol = require_exact_identity_text(
        provider_symbol,
        field="GEX_PROVIDER_SYMBOL",
    )
    instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
    route_fingerprint = require_exact_identity_text(route_fingerprint, field="route_fingerprint")
    if store is None:
        return False
    if (
        not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        raise ValueError("GEX persistence requires a timezone-aware capture timestamp")
    with _gex_persist_lock(instrument_id, route_fingerprint):
        db_payload = dict(payload)
        db_payload.pop("history", None)
        snapshot_source, mode = require_exact_gex_capture_lane(
            source=db_payload.get("source"),
            capture_mode=capture_mode,
        )
        if db_payload.get("capture_mode") != mode:
            raise ValueError("GEX payload capture mode must match the persistence lane")
        if (
            db_payload.get("instrument_id") != instrument_id
            or db_payload.get("route_fingerprint") != route_fingerprint
        ):
            raise ValueError("GEX payload identity must match its persistence key")
        producer_comparison_scope = gex_comparison_scope_from_payload(db_payload)
        captured_utc = captured_at.astimezone(UTC)
        if parse_gex_timestamp(db_payload.get("captured_at")) != captured_utc:
            raise ValueError("GEX payload captured_at must match the persistence observation")
        bucket_start: datetime | None = None
        if live_bucket_at is not None:
            if mode != "live":
                raise ValueError("A live GEX persistence bucket requires capture_mode=live")
            if live_bucket_at.tzinfo is None or live_bucket_at.utcoffset() is None:
                raise ValueError("A live GEX persistence bucket must be timezone-aware")
            bucket_start = live_bucket_at.astimezone(UTC)
            bucket_end = bucket_start + timedelta(minutes=GEX_SCHEDULER_INTERVAL_MINUTES)
            if not bucket_start <= captured_utc < bucket_end:
                raise ValueError(
                    "The exact live GEX observation timestamp is outside its persistence bucket"
                )
        db_payload["captured_at"] = captured_utc.isoformat()
        db_payload["capture_revision"] = gex_capture_revision_at(
            captured_utc,
            capture_mode=mode,
        ).isoformat()
        db_payload.pop("option_universe_expires_at", None)
        durable_meta = dict(meta)
        durable_meta.pop("option_universe_expires_at", None)
        durable_meta.pop("option_expiry_facts", None)
        db_payload["raw"] = {
            "meta": durable_meta,
            "strikes": [
                {
                    key: value
                    for key, value in row.items()
                    if not str(key).startswith("_") and key not in GEX_RAW_STRIKE_REDUNDANT_FIELDS
                }
                for row in strikes
            ],
            "contracts": [dict(row) for row in contract_rows],
        }
        db_payload["instrument_id"] = instrument_id
        db_payload["route_fingerprint"] = route_fingerprint
        db_payload["comparison_scope"] = producer_comparison_scope
        option_activity, option_activity_status = _gex_option_activity_for_persistence(
            db_payload,
            captured_utc,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            store=store,
            snapshot_source=snapshot_source,
            predecessor_before=bucket_start or captured_utc,
        )
        db_payload["option_activity"] = option_activity
        db_payload["option_activity_status"] = option_activity_status
        db_payload = require_gex_snapshot_payload(
            db_payload,
            projection="persisted",
        )
        db_payload.pop("option_universe_expires_at", None)
        committed = store.upsert_gex_snapshot(
            instrument_id,
            route_fingerprint,
            provider_symbol,
            captured_utc,
            db_payload,
            source=snapshot_source,
            live_bucket_at=bucket_start,
        )
    return bool(committed)


def read_gex_snapshot_rows(
    start: datetime,
    end: datetime,
    *,
    instrument_id: str,
    route_fingerprint: str,
    limit: int,
    store: Any | None,
    source: str | None = GEX_REQUEST_SNAPSHOT_SOURCE,
    sources: Sequence[str] | None = None,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("GEX snapshot row limit must be a positive integer")
    if any(
        not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
        for value in (start, end)
    ):
        raise ValueError("GEX snapshot row bounds must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if end < start:
        raise ValueError("GEX snapshot row end must not precede start")
    if store is None:
        return []
    if (source is None) == (sources is None):
        raise ValueError("GEX snapshot rows require one exact source lane contract")
    requested_sources = (source,) if source is not None else tuple(sources or ())
    if not requested_sources or any(
        item not in GEX_CHART_HISTORY_SOURCES for item in requested_sources
    ):
        raise ValueError("Unsupported GEX snapshot source lane")
    rows = store.read_gex_snapshots(
        instrument_id,
        route_fingerprint,
        start,
        end,
        limit=limit,
        source=source,
        sources=sources,
        include_raw=include_raw,
    )
    if not isinstance(rows, list):
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: storage rows must be a typed list")
    return list(rows)


def read_latest_gex_payload(
    instrument_id: str,
    route_fingerprint: str,
    target_ts: datetime | None = None,
    store: Any | None = None,
    *,
    source: str = GEX_REQUEST_SNAPSHOT_SOURCE,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if store is None:
        return None
    expected_mode = (
        "request"
        if source == GEX_REQUEST_SNAPSHOT_SOURCE
        else "live"
        if source == GEX_LIVE_SNAPSHOT_SOURCE
        else None
    )
    try:
        exact_source, expected_capture_mode = require_exact_gex_capture_lane(
            source=source,
            capture_mode=expected_mode,
        )
    except ValueError as exc:
        raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
    row = None
    if isinstance(target_ts, datetime) and (
        target_ts.tzinfo is None or target_ts.utcoffset() is None
    ):
        raise ValueError("GEX target timestamp must be timezone-aware")
    target_utc = target_ts.astimezone(UTC) if isinstance(target_ts, datetime) else None
    if target_utc is not None:
        window = timedelta(minutes=_OPTION_TARGET_GEX_SNAPSHOT_WINDOW_MINUTES)
        rows = read_gex_snapshot_rows(
            target_utc - window,
            target_utc,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            limit=512,
            store=store,
            source=exact_source,
            include_raw=True,
        )
        timestamped_rows: list[tuple[datetime, dict[str, Any]]] = []
        for candidate in rows:
            if not isinstance(candidate, dict):
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row must be an object")
            candidate_ts = parse_gex_timestamp(candidate.get("captured_at"))
            if candidate_ts is None:
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: invalid database timestamp")
            if target_utc - window <= candidate_ts <= target_utc:
                timestamped_rows.append((candidate_ts, candidate))
        row = max(timestamped_rows, key=lambda item: item[0], default=None)
        row = row[1] if row is not None else None
        if row is None:
            return None
    else:
        row = store.read_latest_gex_snapshot(
            instrument_id,
            route_fingerprint,
            source=exact_source,
        )
    if row is None:
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: payload must be an object")
    try:
        comparison_scope = gex_comparison_scope_from_payload(payload)
    except ValueError as exc:
        raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
    row_captured_at = parse_gex_timestamp(row.get("captured_at"))
    payload_captured_at = parse_gex_timestamp(payload.get("captured_at"))
    try:
        row_provider_symbol = require_exact_identity_text(
            row.get("provider_symbol"),
            field="GEX_PROVIDER_SYMBOL",
        )
    except ValueError as exc:
        raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
    if (
        row.get("source") != exact_source
        or comparison_scope["capture_mode"] != expected_capture_mode
        or payload.get("source") != exact_source
        or payload.get("capture_mode") != expected_capture_mode
        or payload.get("provider_symbol") != row_provider_symbol
        or payload.get("instrument_id") != instrument_id
        or payload.get("route_fingerprint") != route_fingerprint
        or row.get("instrument_id") != instrument_id
        or row.get("route_fingerprint") != route_fingerprint
        or row_captured_at is None
        or payload_captured_at != row_captured_at
    ):
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: database row and payload disagree")
    try:
        canonical_payload = require_gex_snapshot_payload(
            payload,
            projection="persisted",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
    return row, canonical_payload


def latest_gex_snapshot_payload(
    provider_symbol: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    max_levels: int,
    stale_minutes: int,
    history_hours: int,
    store: Any | None = None,
) -> dict[str, Any] | None:
    provider_symbol = require_exact_identity_text(
        provider_symbol,
        field="GEX_PROVIDER_SYMBOL",
    )
    if type(max_levels) is not int or max_levels <= 0:
        raise ValueError("GEX cached max_levels must be a positive integer")
    if type(stale_minutes) is not int or stale_minutes <= 0:
        raise ValueError("GEX cached stale_minutes must be a positive integer")
    if type(history_hours) is not int or not 1 <= history_hours <= 24 * 30:
        raise ValueError("GEX cached history_hours must be an integer from 1 through 720")
    latest = read_latest_gex_payload(instrument_id, route_fingerprint, store=store)
    if latest is None:
        return None
    row, payload = latest
    out = dict(payload)
    captured_at = parse_gex_timestamp(row.get("captured_at"))
    if captured_at is not None:
        out["captured_at"] = captured_at.isoformat()
        age_minutes = (datetime.now(tz=UTC) - captured_at).total_seconds() / 60.0
        stale = age_minutes > float(stale_minutes)
        out["age_minutes"] = round(max(age_minutes, 0.0), 1)
        out["stale_minutes"] = stale_minutes
        diagnostics = out.get("diagnostics")
        quality_code = (
            diagnostics.get("diag_frame_quality_code") if isinstance(diagnostics, Mapping) else None
        )
        degraded_coverage = quality_code in {
            "SUFFICIENT_PAIR_COVERAGE",
            "SUFFICIENT_PAIR_COVERAGE_AFTER_TIMEOUT",
        }
        out["status"] = "stale" if stale else "degraded" if degraded_coverage else "ok"
        if stale:
            out["decision_authoritative"] = False
            out["message"] = f"Cached GEX snapshot is {age_minutes:.0f} minutes old."
        option_universe_expires_at = require_gex_option_universe_expiry(
            out.get("option_universe_expires_at"),
            captured_at=captured_at,
            allow_unknown=True,
        )
        if option_universe_expires_at is None or option_universe_expires_at <= datetime.now(tz=UTC):
            out["status"] = "stale"
            out["decision_authoritative"] = False
            out["message"] = (
                "Cached GEX option universe has no exact active expiry; "
                f"showing snapshot from {captured_at.isoformat()} as display-only."
                if option_universe_expires_at is None
                else "Cached GEX option universe expired at "
                f"{option_universe_expires_at.isoformat()}; showing snapshot "
                f"from {captured_at.isoformat()} as display-only."
            )
        runtime_error = _GEX_RUNTIME.error_for_route(
            instrument_id,
            route_fingerprint,
            sources=("manual", "auto"),
        )
        error_at = runtime_error.get("at")
        error_message = str(runtime_error.get("message") or "")
        if error_message and isinstance(error_at, datetime) and error_at > captured_at:
            out["status"] = "refresh_error"
            out["decision_authoritative"] = False
            out["last_error"] = error_message
            out["last_error_at"] = error_at.isoformat()
            out["last_error_source"] = str(runtime_error.get("source") or "")
            out["message"] = (
                f"Latest GEX refresh failed: {error_message}. "
                f"Showing cached snapshot from {captured_at.isoformat()} ({age_minutes:.0f} minutes old)."
            )
    out["source"] = row["source"]
    if captured_at is not None:
        out["capture_revision"] = gex_capture_revision_at(
            captured_at,
            capture_mode=out.get("capture_mode"),
        ).isoformat()
    out["levels"] = require_gex_levels(
        out["levels"],
        max_levels=max_levels,
        spot=out["spot"],
    )
    visibility = (
        out.get("visibility_summary") if isinstance(out.get("visibility_summary"), dict) else {}
    )
    visible_levels = list(out["levels"])
    out["visibility_summary"] = {
        **visibility,
        "output_level_count": len(visible_levels),
        "weak_level_count": sum(
            1 for level in visible_levels if level.get("power_class") == "WEAK"
        ),
        "strong_level_count": sum(
            1 for level in visible_levels if level.get("power_class") != "WEAK"
        ),
        "max_levels": max_levels,
    }
    out.pop("raw", None)
    out["ok"] = bool(out.get("levels"))
    out["history"], out["history_status"] = _cached_gex_history_projection(
        provider_symbol,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        history_hours=history_hours,
        max_levels=max_levels,
        store=store,
    )
    if out["history_status"]["status"] == "error":
        if out.get("status") == "ok":
            out["status"] = "degraded"
        current_message = str(out.get("message") or "").strip()
        history_message = (
            "Cached GEX history was rejected; current canonical levels remain available."
        )
        out["message"] = (
            f"{current_message} {history_message}".strip() if current_message else history_message
        )
    return out


def _gex_side_activity(
    *,
    volume_delta: float,
    elapsed_seconds: float,
    prior_rates: Sequence[float],
) -> dict[str, Any]:
    exact_volume_delta = finite_number_or_none(volume_delta)
    exact_elapsed_seconds = finite_number_or_none(elapsed_seconds)
    if exact_volume_delta is None or exact_volume_delta < 0:
        raise ValueError("GEX activity volume delta must be finite and non-negative")
    if exact_elapsed_seconds is None or exact_elapsed_seconds <= 0:
        raise ValueError("GEX activity elapsed seconds must be finite and positive")
    rate = exact_volume_delta / (exact_elapsed_seconds / 60.0)
    positive_rates = [
        value for value in prior_rates[-GEX_OPTION_ACTIVITY_BASELINE_MAX_SAMPLES:] if value > 0
    ]
    baseline = (
        _median_positive(positive_rates)
        if len(positive_rates) >= GEX_OPTION_ACTIVITY_MIN_BASELINE_SAMPLES
        else None
    )
    rvol = rate / baseline if baseline and baseline > 0 else None
    return {
        "volume_delta": round(exact_volume_delta, 4),
        "rate_per_minute": round(rate, 4),
        "rvol": round(rvol, 4) if rvol is not None else None,
        "baseline_samples": len(positive_rates),
    }


def _gex_option_activity_for_persistence(
    current_payload: dict[str, Any],
    captured_at: datetime,
    *,
    instrument_id: str,
    route_fingerprint: str,
    store: Any,
    snapshot_source: str,
    predecessor_before: datetime,
) -> tuple[dict[str, Any] | None, str]:
    """Build one canonical activity fact before the snapshot is committed."""

    if (
        predecessor_before.tzinfo is None
        or predecessor_before.utcoffset() is None
        or predecessor_before > captured_at
    ):
        raise ValueError("GEX activity predecessor boundary is invalid")
    start = max(
        captured_at - timedelta(hours=GEX_OPTION_ACTIVITY_WARMUP_HOURS),
        gex_analysis_day_start(captured_at),
    )
    row_limit = GEX_OPTION_ACTIVITY_WARMUP_HOURS * 60 // GEX_SCHEDULER_INTERVAL_MINUTES * 2 + 4
    rows = store.read_gex_snapshots(
        instrument_id,
        route_fingerprint,
        start,
        predecessor_before,
        limit=row_limit,
        source=snapshot_source,
        include_raw=True,
    )
    if not isinstance(rows, list):
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: warmup rows must be a typed list")

    current_scope = gex_comparison_scope_from_payload(current_payload)
    require_exact_identity_text(
        current_payload.get("provider_symbol"),
        field="GEX_PAYLOAD_PROVIDER_SYMBOL",
    )
    history_rows: list[tuple[datetime, dict[str, Any], GexComparisonScope]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row, Mapping) else None
        if not isinstance(payload, dict):
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: warmup payload must be an object")
        row_captured_at = parse_gex_timestamp(row.get("captured_at"))
        payload_captured_at = parse_gex_timestamp(payload.get("captured_at"))
        require_exact_identity_text(
            row.get("provider_symbol"),
            field="GEX_PROVIDER_SYMBOL",
        )
        require_exact_identity_text(
            payload.get("provider_symbol"),
            field="GEX_PAYLOAD_PROVIDER_SYMBOL",
        )
        if (
            row.get("instrument_id") != instrument_id
            or payload.get("instrument_id") != instrument_id
            or row.get("route_fingerprint") != route_fingerprint
            or payload.get("route_fingerprint") != route_fingerprint
            or row.get("source") != snapshot_source
            or payload.get("source") != snapshot_source
            or row_captured_at is None
            or payload_captured_at != row_captured_at
        ):
            raise ValueError(
                "GEX_HISTORY_CONTRACT_INVALID: warmup row and payload provenance disagree"
            )
        if row_captured_at >= predecessor_before:
            continue
        try:
            canonical_payload = require_gex_snapshot_payload(
                payload,
                projection="persisted",
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
        history_rows.append(
            (
                row_captured_at,
                canonical_payload,
                canonical_payload["comparison_scope"],
            )
        )
    history_rows.sort(key=lambda item: item[0])
    if not history_rows:
        return None, "NEED_PREVIOUS"
    if any(
        current[0] <= previous[0]
        for previous, current in zip(history_rows, history_rows[1:], strict=False)
    ):
        raise ValueError(
            "GEX_HISTORY_CONTRACT_INVALID: warmup timestamps must be strictly increasing"
        )

    previous_captured_at, previous_payload, previous_scope = history_rows[-1]
    if previous_scope != current_scope:
        return None, "SCOPE_CHANGED"
    elapsed_seconds = round(
        (captured_at - previous_captured_at).total_seconds(),
        6,
    )
    if elapsed_seconds <= 0:
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: predecessor time must precede capture")
    if elapsed_seconds > GEX_SCHEDULER_INTERVAL_MINUTES * 3 * 60:
        return None, "GAP"

    from aef_terminal.data.gex.activity import _gex_activity_delta

    activity_delta = _gex_activity_delta(previous_payload, current_payload)
    if activity_delta["status"] != "ok":
        return None, {
            "missing_input": "MISSING_INPUT",
            "missing_overlap": "MISSING_OVERLAP",
            "counter_reset": "COUNTER_RESET",
        }[activity_delta["status"]]

    prior_rates: dict[str, list[float]] = {"call": [], "put": [], "total": []}
    next_captured_at = captured_at
    for row_captured_at, payload, candidate_scope in reversed(history_rows):
        row_gap_seconds = (next_captured_at - row_captured_at).total_seconds()
        if (
            candidate_scope != current_scope
            or row_gap_seconds <= 0
            or row_gap_seconds > GEX_SCHEDULER_INTERVAL_MINUTES * 3 * 60
        ):
            break
        status = payload["option_activity_status"]
        if status not in {"BASELINING", "READY"}:
            break
        canonical_activity = payload["option_activity"]
        assert canonical_activity is not None
        for side in ("call", "put", "total"):
            prior_rates[side].append(canonical_activity[side]["rate_per_minute"])
        next_captured_at = row_captured_at
        if len(prior_rates["total"]) >= GEX_OPTION_ACTIVITY_BASELINE_MAX_SAMPLES:
            break

    call_volume_delta = activity_delta["call_volume_delta"]
    put_volume_delta = activity_delta["put_volume_delta"]
    assert call_volume_delta is not None and put_volume_delta is not None
    call_activity = _gex_side_activity(
        volume_delta=call_volume_delta,
        elapsed_seconds=elapsed_seconds,
        prior_rates=prior_rates["call"],
    )
    put_activity = _gex_side_activity(
        volume_delta=put_volume_delta,
        elapsed_seconds=elapsed_seconds,
        prior_rates=prior_rates["put"],
    )
    total_activity = _gex_side_activity(
        volume_delta=call_volume_delta + put_volume_delta,
        elapsed_seconds=elapsed_seconds,
        prior_rates=prior_rates["total"],
    )
    status = "READY" if total_activity["rvol"] is not None else "BASELINING"
    activity = require_gex_option_activity(
        {
            "status": status,
            "scope": "selected_chain",
            "elapsed_seconds": elapsed_seconds,
            "contract_universe": {
                "selected_strikes": activity_delta["selected_strikes"],
                "selected_contracts": activity_delta["selected_contracts"],
                "matched_contracts": activity_delta["matched_contracts"],
            },
            "call": call_activity,
            "put": put_activity,
            "total": total_activity,
        },
        expected_status=status,
    )
    return activity, status


def _read_snapshot_history_from_db(
    provider_symbol: str,
    start: datetime,
    end: datetime,
    *,
    instrument_id: str,
    route_fingerprint: str,
    max_levels: int,
    store: Any | None = None,
    preloaded_rows: Sequence[dict[str, Any]] | None = None,
    sources: Sequence[str] = GEX_CHART_HISTORY_SOURCES,
) -> list[dict[str, Any]]:
    provider_symbol = require_exact_identity_text(
        provider_symbol,
        field="GEX_PROVIDER_SYMBOL",
    )
    if any(
        not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
        for value in (start, end)
    ):
        raise ValueError("GEX history bounds must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if end < start:
        raise ValueError("GEX history end must not precede start")
    exact_sources = tuple(sources)
    if not exact_sources:
        return []
    if any(source not in GEX_CHART_HISTORY_SOURCES for source in exact_sources):
        raise ValueError("Unsupported GEX snapshot history source")
    if len(set(exact_sources)) != len(exact_sources):
        raise ValueError("GEX snapshot history sources must be unique")
    if preloaded_rows is not None:
        rows = []
        for row in preloaded_rows:
            if not isinstance(row, dict):
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row must be an object")
            if (
                row.get("instrument_id") != instrument_id
                or row.get("route_fingerprint") != route_fingerprint
                or row.get("provider_symbol") != provider_symbol
            ):
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: preloaded row identity disagrees")
            if row.get("source") not in exact_sources:
                continue
            ts = parse_gex_timestamp(row.get("captured_at"))
            if ts is None:
                raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row timestamp is invalid")
            if start <= ts <= end:
                rows.append(row)
    elif store is None:
        return []
    else:
        expected_five_minute_rows = (
            math.ceil(max((end - start).total_seconds(), 0.0) / 300.0) + 2
        ) * len(exact_sources)
        rows = read_gex_snapshot_rows(
            start,
            end,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            limit=max(24 * 30, expected_five_minute_rows),
            store=store,
            source=None,
            sources=exact_sources,
        )
    source_order = {source: index for index, source in enumerate(GEX_CHART_HISTORY_SOURCES)}
    timestamped_rows: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row must be an object")
        row_timestamp = parse_gex_timestamp(row.get("captured_at"))
        if row_timestamp is None:
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row timestamp is invalid")
        timestamped_rows.append((row_timestamp, row))
    timestamped_rows.sort(
        key=lambda item: (
            item[0],
            source_order.get(item[1].get("source"), len(source_order)),
        )
    )
    out: list[dict[str, Any]] = []
    for row_captured_at, row in timestamped_rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: payload must be an object")
        payload_captured_at = parse_gex_timestamp(payload.get("captured_at"))
        if payload_captured_at != row_captured_at:
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: capture timestamps disagree")
        captured_at = row_captured_at
        snapshot_source = row.get("source")
        try:
            _, expected_capture_mode = require_exact_gex_capture_lane(
                source=snapshot_source,
                capture_mode=payload.get("capture_mode"),
            )
        except ValueError as exc:
            raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
        row_provider_symbol = require_exact_identity_text(
            row.get("provider_symbol"), field="GEX_PROVIDER_SYMBOL"
        )
        payload_provider_symbol = require_exact_identity_text(
            payload.get("provider_symbol"), field="GEX_PAYLOAD_PROVIDER_SYMBOL"
        )
        if (
            row_provider_symbol != provider_symbol
            or payload_provider_symbol != provider_symbol
            or payload.get("source") != snapshot_source
            or payload.get("capture_mode") != expected_capture_mode
            or payload.get("instrument_id") != instrument_id
            or payload.get("route_fingerprint") != route_fingerprint
            or row.get("instrument_id") != instrument_id
            or row.get("route_fingerprint") != route_fingerprint
        ):
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: row and payload provenance disagree")
        try:
            canonical_payload = require_gex_snapshot_payload(
                payload,
                projection="history",
                max_levels=max_levels,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"GEX_HISTORY_CONTRACT_INVALID: {exc}") from exc
        payload = canonical_payload
        levels: list[dict[str, Any]] = []
        for level in payload["levels"]:
            compact_level = {
                key: level.get(key)
                for key in (
                    "price",
                    "kind",
                    "kind_class",
                    "power_class",
                    "strength",
                    "selection_rank",
                    "net_gex",
                    "call_gex",
                    "put_gex",
                    "abs_gex",
                    "abs_flow_1pt",
                    "distance_from_spot",
                    "zone_half_width",
                    "spot_side",
                )
                if key in level
            }
            # Admission above created detached canonical nested projections.
            # Reuse them inside this batch instead of validating/copying again.
            compact_level["option_volume_context"] = level["option_volume_context"]
            if isinstance(level.get("motion"), Mapping):
                compact_level["motion"] = level["motion"]
            levels.append(compact_level)
        gamma_flip = payload["gamma_flip"]
        spot = float(payload["spot"])
        if not levels and gamma_flip is None:
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: snapshot has no price facts")
        comparison_scope = payload["comparison_scope"]
        if comparison_scope["capture_mode"] != expected_capture_mode:
            raise ValueError("GEX_HISTORY_CONTRACT_INVALID: comparison lane mismatch")
        option_activity = payload["option_activity"]
        option_activity_status = payload["option_activity_status"]
        out.append(
            {
                "captured_at": captured_at.isoformat(),
                "timestamp_unix_ms": int(captured_at.timestamp() * 1000),
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "provider_symbol": provider_symbol,
                "spot": spot,
                "gamma_flip": gamma_flip,
                "net_gex": payload["net_gex"],
                "global_gamma_regime": payload["global_gamma_regime"],
                "call_wall": payload["call_wall"],
                "put_wall": payload["put_wall"],
                "source": snapshot_source,
                "capture_mode": expected_capture_mode,
                "frame_complete": payload["frame_complete"],
                "decision_authoritative": payload["decision_authoritative"],
                "option_universe_expires_at": payload["option_universe_expires_at"],
                "market_data_entitlement": payload["market_data_entitlement"],
                "open_interest_as_of": payload["open_interest_as_of"],
                "capture_revision": gex_capture_revision_at(
                    captured_at,
                    capture_mode=expected_capture_mode,
                ).isoformat(),
                "comparison_scope": comparison_scope,
                "levels": levels,
                "option_activity": option_activity,
                "option_activity_status": option_activity_status,
            }
        )
    return out
