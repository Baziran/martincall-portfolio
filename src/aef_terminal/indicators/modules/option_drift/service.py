from __future__ import annotations

import threading
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from aef_terminal.data.gex.contracts import (
    gex_capture_lane,
    gex_comparison_scope_from_payload,
    require_gex_contract_market_facts,
    require_gex_market_data_entitlement,
    require_gex_option_reference,
)
from aef_terminal.data.gex.history import gex_analysis_day_start, read_gex_snapshot_rows
from aef_terminal.data.gex.live import live_gex_frame_snapshot
from aef_terminal.data.gex.utils import finite_number_or_none, parse_gex_timestamp
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.indicators.service_contract import (
    IndicatorServiceContext,
    IndicatorServiceContribution,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.stable_hash import stable_hash
from aef_terminal.ui.routers.error_payloads import build_error_payload

from . import OPTION_DRIFT_VERSION
from .calculation import calculate_option_drift


_SERVICE_CONTEXT: IndicatorServiceContext | None = None
_OPTION_DRIFT_ROW_LIMIT = 256
_OPTION_DRIFT_LIVE_INTERVAL_SECONDS = 60
_OPTION_DRIFT_LIVE_MAX_SAMPLES = 256
_LIVE_MINUTE_LANES_LOCK = threading.Lock()
_LIVE_MINUTE_LANES: dict[tuple[str, str], dict[str, Any]] = {}
_SERVICE_GENERATION = 0


class OptionDriftServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def configure_service(
    context: IndicatorServiceContext,
) -> IndicatorServiceContribution:
    global _SERVICE_CONTEXT
    _SERVICE_CONTEXT = context
    _clear_live_minute_lanes()
    return IndicatorServiceContribution(shutdown=_shutdown_service)


def _clear_live_minute_lanes() -> None:
    global _SERVICE_GENERATION
    with _LIVE_MINUTE_LANES_LOCK:
        _LIVE_MINUTE_LANES.clear()
        _SERVICE_GENERATION += 1


async def _shutdown_service() -> None:
    global _SERVICE_CONTEXT
    _SERVICE_CONTEXT = None
    await run_physical_thread_call(_clear_live_minute_lanes)


def _http_error(status_code: int, *, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=build_error_payload(
            code=code,
            category="option_drift",
            retryable=status_code >= 500,
            error=message,
        ),
    )


def _validated_payloads(
    rows: object,
    *,
    instrument_id: str,
    route_fingerprint: str,
    source: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError("OPTION_DRIFT_HISTORY_INVALID: storage rows must be a typed list")
    payloads: list[dict[str, Any]] = []
    previous_at: datetime | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("OPTION_DRIFT_HISTORY_INVALID: row must be an object")
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("OPTION_DRIFT_HISTORY_INVALID: payload must be an object")
        canonical = _require_option_drift_snapshot_payload(payload)
        row_at = parse_gex_timestamp(row.get("captured_at"))
        payload_at = parse_gex_timestamp(canonical.get("captured_at"))
        if (
            row.get("instrument_id") != instrument_id
            or canonical.get("instrument_id") != instrument_id
            or row.get("route_fingerprint") != route_fingerprint
            or canonical.get("route_fingerprint") != route_fingerprint
            or row.get("source") != source
            or canonical.get("source") != source
            or row_at is None
            or payload_at != row_at
            or (previous_at is not None and row_at <= previous_at)
        ):
            raise ValueError("OPTION_DRIFT_HISTORY_INVALID: row provenance disagrees")
        previous_at = row_at
        payloads.append(canonical)
    return payloads


def _require_option_drift_snapshot_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate only the canonical persisted facts consumed by this advisory projection."""

    canonical = dict(payload)
    scope = gex_comparison_scope_from_payload(canonical)
    entitlement = require_gex_market_data_entitlement(canonical.get("market_data_entitlement"))
    if scope["market_data_entitlement"] != entitlement:
        raise ValueError("Option Drift snapshot entitlement disagrees with comparison scope")
    spot = finite_number_or_none(canonical.get("spot"))
    if spot is None or spot <= 0:
        raise ValueError("Option Drift snapshot requires a finite positive spot")
    raw = canonical.get("raw")
    contracts = raw.get("contracts") if isinstance(raw, Mapping) else None
    if (
        not isinstance(contracts, list)
        or not contracts
        or any(not isinstance(row, Mapping) for row in contracts)
    ):
        raise ValueError("Option Drift snapshot requires canonical raw option contracts")
    for contract in contracts:
        require_gex_contract_market_facts(contract, spot=spot)
        require_gex_option_reference(contract)
    canonical["comparison_scope"] = scope
    return canonical


def _minute_bucket_at(captured_at: datetime) -> datetime:
    bucket_seconds = (
        int(captured_at.astimezone(UTC).timestamp()) // _OPTION_DRIFT_LIVE_INTERVAL_SECONDS
    ) * _OPTION_DRIFT_LIVE_INTERVAL_SECONDS
    return datetime.fromtimestamp(bucket_seconds, tz=UTC)


def _record_live_minute_frame(
    payload: Mapping[str, Any],
    *,
    instrument_id: str,
    route_fingerprint: str,
    generation: int,
) -> list[dict[str, Any]]:
    canonical = _require_option_drift_snapshot_payload(payload)
    source, capture_mode = gex_capture_lane("live")
    captured_at = parse_gex_timestamp(canonical.get("captured_at"))
    if (
        canonical.get("instrument_id") != instrument_id
        or canonical.get("route_fingerprint") != route_fingerprint
        or canonical.get("source") != source
        or canonical.get("capture_mode") != capture_mode
        or captured_at is None
    ):
        raise ValueError("OPTION_DRIFT_LIVE_FRAME_INVALID: live frame provenance disagrees")
    bucket_at = _minute_bucket_at(captured_at)
    identity_key = (instrument_id, route_fingerprint)
    with _LIVE_MINUTE_LANES_LOCK:
        if generation != _SERVICE_GENERATION:
            raise OptionDriftServiceError(
                "OPTION_DRIFT_SERVICE_CHANGED",
                "Option Drift service changed during projection",
            )
        lane = _LIVE_MINUTE_LANES.setdefault(
            identity_key,
            {"bucket_at": None, "forming": None, "closed": []},
        )
        current_bucket = lane["bucket_at"]
        forming = lane["forming"]
        if current_bucket is None:
            lane["bucket_at"] = bucket_at
            lane["forming"] = deepcopy(canonical)
        elif bucket_at == current_bucket:
            forming_at = (
                parse_gex_timestamp(forming.get("captured_at"))
                if isinstance(forming, Mapping)
                else None
            )
            if forming_at is None or captured_at > forming_at:
                lane["forming"] = deepcopy(canonical)
        elif bucket_at > current_bucket:
            if isinstance(forming, Mapping):
                lane["closed"].append(deepcopy(dict(forming)))
                lane["closed"] = lane["closed"][-_OPTION_DRIFT_LIVE_MAX_SAMPLES:]
            lane["bucket_at"] = bucket_at
            lane["forming"] = deepcopy(canonical)
        return deepcopy(lane["closed"])


def _merge_live_minute_payloads(
    persisted: list[dict[str, Any]],
    minute_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ordered_minutes = sorted(
        minute_payloads,
        key=lambda payload: (
            parse_gex_timestamp(payload.get("captured_at")) or datetime.min.replace(tzinfo=UTC)
        ),
    )
    if not ordered_minutes:
        return persisted
    first_minute_at = parse_gex_timestamp(ordered_minutes[0].get("captured_at"))
    if first_minute_at is None:
        raise ValueError("OPTION_DRIFT_LIVE_FRAME_INVALID: minute frame time is missing")
    persisted_prefix = [
        payload
        for payload in persisted
        if (
            (captured_at := parse_gex_timestamp(payload.get("captured_at"))) is not None
            and captured_at < first_minute_at
        )
    ]
    return [*persisted_prefix, *ordered_minutes]


def _build_option_drift_payload(
    *,
    instrument_id: str,
    route_fingerprint: str,
    capture_mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    with _LIVE_MINUTE_LANES_LOCK:
        context = _SERVICE_CONTEXT
        generation = _SERVICE_GENERATION
    if context is None:
        raise OptionDriftServiceError(
            "OPTION_DRIFT_SERVICE_NOT_INITIALIZED",
            "Option Drift service is not initialized",
        )
    exact_instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
    exact_route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    source, exact_capture_mode = gex_capture_lane(capture_mode)
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Option Drift now must be timezone-aware")
    now_utc = current.astimezone(UTC)
    day_start = gex_analysis_day_start(now_utc)

    def read_payloads() -> list[dict[str, Any]]:
        store = context.store_factory()
        rows = read_gex_snapshot_rows(
            day_start,
            now_utc,
            instrument_id=exact_instrument_id,
            route_fingerprint=exact_route_fingerprint,
            limit=_OPTION_DRIFT_ROW_LIMIT,
            store=store,
            source=source,
            include_raw=True,
        )
        return _validated_payloads(
            rows,
            instrument_id=exact_instrument_id,
            route_fingerprint=exact_route_fingerprint,
            source=source,
        )

    try:
        payloads = read_payloads()
    except (TypeError, ValueError) as exc:
        raise OptionDriftServiceError(
            "OPTION_DRIFT_HISTORY_INVALID",
            str(exc),
        ) from exc
    except RuntimeError as exc:
        raise OptionDriftServiceError(
            "OPTION_DRIFT_HISTORY_UNAVAILABLE",
            str(exc),
        ) from exc
    live_minute_payloads: list[dict[str, Any]] = []
    live_minute_active = False
    if exact_capture_mode == "live":
        live_frame = live_gex_frame_snapshot(
            exact_instrument_id,
            exact_route_fingerprint,
        )
        if live_frame is not None:
            live_minute_active = True
            try:
                live_minute_payloads = _record_live_minute_frame(
                    live_frame,
                    instrument_id=exact_instrument_id,
                    route_fingerprint=exact_route_fingerprint,
                    generation=generation,
                )
            except (TypeError, ValueError) as exc:
                raise OptionDriftServiceError(
                    "OPTION_DRIFT_LIVE_FRAME_INVALID",
                    str(exc),
                ) from exc
            live_minute_payloads = [
                payload
                for payload in live_minute_payloads
                if (
                    (captured_at := parse_gex_timestamp(payload.get("captured_at"))) is not None
                    and day_start <= captured_at <= now_utc
                )
            ]
            payloads = _merge_live_minute_payloads(payloads, live_minute_payloads)
    projection = calculate_option_drift(payloads, now=now_utc)
    projection["quality"].update(
        {
            "live_minute_active": live_minute_active,
            "live_minute_samples": len(live_minute_payloads),
            "target_interval_seconds": (
                _OPTION_DRIFT_LIVE_INTERVAL_SECONDS if exact_capture_mode == "live" else None
            ),
        }
    )
    response = {
        "ok": True,
        "version": OPTION_DRIFT_VERSION,
        "instrument_id": exact_instrument_id,
        "route_fingerprint": exact_route_fingerprint,
        "capture_mode": exact_capture_mode,
        "source": source,
        **projection,
    }
    response["revision"] = stable_hash(response, length=20)
    with _LIVE_MINUTE_LANES_LOCK:
        if generation != _SERVICE_GENERATION or context is not _SERVICE_CONTEXT:
            raise OptionDriftServiceError(
                "OPTION_DRIFT_SERVICE_CHANGED",
                "Option Drift service changed during projection",
            )
    return response


async def option_drift_payload(
    *,
    instrument_id: str,
    route_fingerprint: str,
    capture_mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    # Storage, admission, live-frame copies and projection settle together;
    # cancellation must not leave a worker mutating the minute lane unowned.
    return await run_physical_thread_call(
        _build_option_drift_payload,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        capture_mode=capture_mode,
        now=now,
    )


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/indicators/option-drift/state")
    async def state(
        instrument_id: str = Query(...),
        route_fingerprint: str = Query(...),
        capture_mode: str = Query("live"),
    ) -> dict[str, Any]:
        try:
            return await option_drift_payload(
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                capture_mode=capture_mode,
            )
        except OptionDriftServiceError as exc:
            raise _http_error(503, code=exc.code, message=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise _http_error(
                422,
                code="OPTION_DRIFT_REQUEST_INVALID",
                message=str(exc),
            ) from exc

    return router
