from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.indicators.contracts import (
    normalize_indicator_result,
    validate_indicator_result_contract,
)
from aef_terminal.indicators.registry import indicator_empty_result
from aef_terminal.runtime.stable_hash import stable_hash

from aef_terminal.engine.analyze.constants import INDICATOR_STATUS_META


def empty_indicator_result(name: str) -> dict[str, Any]:
    return dict(indicator_empty_result(name))


def indicator_status(
    name: str,
    result: dict[str, Any] | None,
    *,
    input_bars: Sequence[Bar],
    analysis_bar: Bar | None,
    mode: str,
    started_at: datetime,
    params_hash: str | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    meta = INDICATOR_STATUS_META.get(name, {})
    calculated_at = datetime.now(tz=UTC)
    event_items = result.get("events", []) if isinstance(result, dict) else []
    preview_event_items = result.get("preview_events", []) if isinstance(result, dict) else []
    preview_item = result.get("preview") if isinstance(result, dict) else None
    series_items = result.get("series", []) if isinstance(result, dict) else []
    event_count = len(event_items) if isinstance(event_items, list) else 0
    preview_event_count = len(preview_event_items) if isinstance(preview_event_items, list) else 0
    preview_active = isinstance(preview_item, dict)
    series_count = len(series_items) if isinstance(series_items, list) else 0
    last_event_ts = None
    if isinstance(event_items, list) and event_items:
        last_event_ts = event_items[-1].get("ts")
    latest = result.get("latest") if isinstance(result, dict) else None
    latest_state = None
    latest_blocked = False
    if isinstance(latest, dict):
        latest_state = latest.get("state") or latest.get("action") or latest.get("code")
    analysis_ts = analysis_bar.ts.isoformat() if analysis_bar else None
    recent_events = 0
    recent_blocked = 0
    availability = result.get("availability") if isinstance(result, dict) else None
    availability_state = (
        str(availability.get("state") or "") if isinstance(availability, dict) else ""
    )
    availability_reason_code = (
        str(availability.get("reason_code") or "") if isinstance(availability, dict) else ""
    )
    if analysis_ts and isinstance(event_items, list):
        recent_events = sum(1 for event in event_items if event.get("ts") == analysis_ts)
        recent_blocked = sum(
            1
            for event in event_items
            if event.get("ts") == analysis_ts
            and (event.get("action") == "BLOCK" or event.get("blocked") is True)
        )
    if analysis_ts and isinstance(latest, dict) and latest.get("ts") == analysis_ts:
        latest_signal = latest.get("signal")
        latest_blocked = bool(
            latest.get("action") == "BLOCK"
            or latest.get("blocked") is True
            or (
                isinstance(latest_signal, dict)
                and (latest_signal.get("action") == "BLOCK" or latest_signal.get("blocked") is True)
            )
        )
        if latest_blocked and recent_blocked == 0:
            recent_blocked = 1
    if error is not None:
        state_code = "error"
        health = "error"
        reason_code = "indicator_error"
        trigger_event = {"code": "indicator_error"}
    elif availability_state == "blocked":
        state_code = "blocked_context"
        health = "blocked"
        reason_code = availability_reason_code or "required_context_unavailable"
        trigger_event = {"code": "indicator_context_blocked", "reason_code": reason_code}
    elif availability_state == "degraded":
        state_code = "degraded_context"
        health = "degraded"
        reason_code = availability_reason_code or "optional_context_unavailable"
        trigger_event = {"code": "indicator_context_degraded", "reason_code": reason_code}
    elif not input_bars:
        state_code = "stale"
        health = "stale"
        reason_code = "no_input_bars"
        trigger_event = {"code": "indicator_input_missing", "reason_code": reason_code}
    elif recent_blocked > 0:
        state_code = "blocked_signal"
        health = "blocked"
        reason_code = "raw_signal_blocked"
        trigger_event = {"code": "indicator_signal_blocked", "count": recent_blocked}
    elif preview_event_count > 0:
        state_code = "live_preview_signal"
        health = "signal"
        reason_code = "live_preview_signal"
        trigger_event = {"code": "indicator_live_preview", "count": preview_event_count}
    elif recent_events > 0:
        state_code = "signal"
        health = "signal"
        reason_code = "analysis_bar_signal"
        trigger_event = {"code": "indicator_signal", "count": recent_events}
    else:
        state_code = "no_signal"
        health = "ok"
        reason_code = "no_current_signal"
        trigger_event = {"code": "indicator_no_signal"}
    return {
        "id": name,
        "label": meta.get("label", name),
        "calculates": meta.get("calculates", "indicator calculation"),
        "state_code": state_code,
        "health": health,
        "reason_code": reason_code,
        "trigger_event": trigger_event,
        "preview_active": preview_active or preview_event_count > 0,
        "has_signal": recent_events > 0 or preview_event_count > 0 or recent_blocked > 0,
        "blocked_signal": recent_blocked > 0,
        "mode": mode,
        "bar_count": len(input_bars),
        "analysis_ts": analysis_ts,
        "input_first_ts": input_bars[0].ts.isoformat() if input_bars else None,
        "input_last_ts": input_bars[-1].ts.isoformat() if input_bars else None,
        "calculated_at": calculated_at.isoformat(),
        "elapsed_ms": round((calculated_at - started_at).total_seconds() * 1000.0, 3),
        "event_count": event_count,
        "preview_event_count": preview_event_count,
        "series_count": series_count,
        "recent_event_count": recent_events,
        "recent_blocked_count": recent_blocked,
        "last_event_ts": last_event_ts,
        "latest_state": latest_state,
        "params_hash": params_hash,
        "last_error": str(error) if error else None,
    }


def attach_indicator_status(
    name: str,
    result: dict[str, Any],
    *,
    input_bars: Sequence[Bar],
    analysis_bar: Bar | None,
    mode: str,
    started_at: datetime | None = None,
    params_hash: str | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    result = normalize_indicator_result(result, name=name)
    result["status"] = indicator_status(
        name,
        result,
        input_bars=input_bars,
        analysis_bar=analysis_bar,
        mode=mode,
        started_at=started_at or datetime.now(tz=UTC),
        params_hash=params_hash,
        error=error,
    )
    registry_contracts = _registered_runtime_contracts(name)
    validate_indicator_result_contract(
        result,
        name=name,
        renderer_contract=registry_contracts.get("renderer"),
        runtime_payload_contract=registry_contracts.get("runtime_payload"),
    )
    return result


def _registered_runtime_contracts(name: str) -> dict[str, dict[str, Any] | None]:
    try:
        from aef_terminal.indicators.module_contract import normalize_runtime_payload_contract
        from aef_terminal.indicators.registry import INDICATOR_REGISTRY
    except Exception:
        return {"renderer": None, "runtime_payload": None}
    spec = INDICATOR_REGISTRY.get(name)
    if spec is None:
        return {"renderer": None, "runtime_payload": None}
    return {
        "renderer": {
            "ref": spec.renderer_ref,
            "primitives": list(spec.renderer_primitives),
            "placements": list(spec.renderer_placements),
        },
        "runtime_payload": {
            key: list(value)
            for key, value in normalize_runtime_payload_contract(
                spec.runtime_payload_contract,
                spec.runtime_payload_schema_refs,
            ).items()
        },
    }


def _params_hash(params: Any) -> str:
    return stable_hash(params, length=10)
