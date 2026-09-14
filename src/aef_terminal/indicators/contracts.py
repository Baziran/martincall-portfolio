from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from aef_terminal.domain import DomainFact, domain_wire_value
from aef_terminal.indicators.domain_facts import validate_indicator_fact_fields
from aef_terminal.runtime.math_utils import is_exact_finite_number


INDICATOR_CONTRACT_VERSION = "indicator-contract-v1"
OVERLAY_CONTRACT_VERSION = "overlay-contract-v1"
ALLOWED_OVERLAY_TYPES = frozenset({"box", "line", "marker", "label", "table", "custom"})
ALLOWED_OVERLAY_LAYERS = frozenset(
    {"background", "zones", "levels", "signals", "tables", "foreground"}
)
OVERLAY_TYPE_PLACEMENTS = {
    "table": "table",
    "custom": "price",
}
REQUIRED_INDICATOR_KEYS = frozenset(
    {"series", "events", "overlays", "latest", "settings", "signals", "contract", "id", "status"}
)
REQUIRED_TRADE_PLAN_KEYS = frozenset(
    {
        "source",
        "direction",
        "entry",
        "trigger",
        "stop",
        "target",
        "complete",
        "coherent",
        "actionable",
    }
)


def is_exact_utc_indicator_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    offset = parsed.utcoffset()
    return parsed.tzinfo is not None and offset is not None and offset.total_seconds() == 0


def _validate_fact_contract(source: Mapping[str, Any], *, name: str, section: str) -> None:
    try:
        validate_indicator_fact_fields(source)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} {section} has invalid indicator facts: {exc}") from exc


def _validate_nested_fact_contract(
    source: Mapping[str, Any],
    *,
    field: str,
    name: str,
    section: str,
) -> None:
    nested = source.get(field)
    if nested is None:
        return
    if not isinstance(nested, Mapping):
        raise ValueError(f"{name} {section} {field} must be a mapping")
    _validate_fact_contract(nested, name=name, section=f"{section} {field}")
    plan = nested.get("trade_plan")
    if plan is not None:
        if not isinstance(plan, Mapping):
            raise ValueError(f"{name} {section} {field} trade_plan must be a mapping")
        _validate_trade_plan_contract(plan, name=name)


def normalize_overlay_item(item: Mapping[str, Any], *, source: str = "") -> dict[str, Any]:
    overlay = dict(item)
    if isinstance(overlay.get("signal"), Mapping):
        overlay["signal_overlay"] = True
    overlay_type = str(overlay.get("type") or "").strip().lower() or "label"
    overlay["type"] = overlay_type
    if source:
        overlay.setdefault("source", source)
    layer = str(overlay.get("layer") or "").strip().lower()
    if layer:
        overlay["layer"] = layer
    else:
        overlay.pop("layer", None)
    overlay["contract"] = OVERLAY_CONTRACT_VERSION
    return overlay


def normalize_signal_item(item: Mapping[str, Any], *, source: str = "") -> dict[str, Any]:
    signal = dict(item)
    if source:
        signal.setdefault("source", source)
    signal.setdefault("details", {})
    signal.setdefault("overlays", [])
    raw_overlays = signal.get("overlays")
    if not isinstance(raw_overlays, list):
        raise ValueError(f"{source or 'indicator'} signal overlays must be a list")
    normalized_overlays: list[dict[str, Any]] = []
    for index, overlay in enumerate(raw_overlays):
        if not isinstance(overlay, Mapping):
            raise ValueError(
                f"{source or 'indicator'} signal overlay item {index} must be a mapping"
            )
        normalized_overlays.append(normalize_overlay_item(overlay, source=source))
    signal["overlays"] = normalized_overlays
    return signal


def _validate_preview_item(
    item: Mapping[str, Any],
    *,
    name: str,
    section: str,
    require_confirmed: bool,
    require_ts: bool,
) -> None:
    if require_confirmed and item.get("confirmed") is not False:
        raise ValueError(f"{name} {section} confirmed must be false")
    if "confirmed" in item and item.get("confirmed") is not False:
        raise ValueError(f"{name} {section} confirmed must be false when present")
    if require_ts and "ts" not in item:
        raise ValueError(f"{name} {section} must contain a UTC ts")
    if "ts" in item and not is_exact_utc_indicator_timestamp(item.get("ts")):
        raise ValueError(f"{name} {section} ts must be an aware UTC timestamp")
    _validate_fact_contract(item, name=name, section=section)
    _validate_nested_fact_contract(
        item,
        field="signal",
        name=name,
        section=section,
    )


def normalize_indicator_preview_result(
    result: Any,
    *,
    name: str,
    event_ts: str = "",
) -> dict[str, Any]:
    """Validate and project one provisional calculation without hiding producer violations."""

    if not isinstance(result, Mapping):
        raise ValueError(f"{name} preview result must be a mapping, got {type(result).__name__}")
    if not isinstance(event_ts, str):
        raise TypeError(f"{name} preview event_ts must be a string")
    if event_ts and not is_exact_utc_indicator_timestamp(event_ts):
        raise ValueError(f"{name} preview event_ts must be an aware UTC timestamp")

    raw_latest = result.get("latest")
    latest: dict[str, Any] | None = None
    if raw_latest is not None:
        if not isinstance(raw_latest, Mapping):
            raise ValueError(f"{name} preview latest must be a mapping or null")
        _validate_preview_item(
            raw_latest,
            name=name,
            section="preview latest",
            require_confirmed=False,
            require_ts=False,
        )
        latest = {**raw_latest, "confirmed": False}

    raw_events = result.get("events", [])
    if not isinstance(raw_events, list):
        raise ValueError(f"{name} preview events must be a list")
    events: list[dict[str, Any]] = []
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            raise ValueError(f"{name} preview event item {index} must be a mapping")
        _validate_preview_item(
            raw_event,
            name=name,
            section=f"preview event item {index}",
            require_confirmed=False,
            require_ts=True,
        )
        event = {**raw_event, "confirmed": False}
        if not event_ts or event["ts"] == event_ts:
            events.append(event)
    return {"latest": latest, "events": events}


def normalize_indicator_result(result: Mapping[str, Any], *, name: str = "") -> dict[str, Any]:
    payload = dict(result)
    payload.setdefault("series", [])
    payload.setdefault("events", [])
    payload.setdefault("overlays", [])
    payload.setdefault("latest", None)
    payload.setdefault("settings", {})
    payload.setdefault("signals", [])
    payload.setdefault("contract", INDICATOR_CONTRACT_VERSION)
    if name:
        payload.setdefault("id", name)
    owner = name or str(payload.get("id") or "indicator")
    raw_overlays = payload.get("overlays")
    if not isinstance(raw_overlays, list):
        raise ValueError(f"{owner} field overlays must be a list")
    normalized_overlays: list[dict[str, Any]] = []
    for index, item in enumerate(raw_overlays):
        if not isinstance(item, Mapping):
            raise ValueError(f"{owner} overlay item {index} must be a mapping")
        normalized_overlays.append(normalize_overlay_item(item, source=name))
    payload["overlays"] = normalized_overlays

    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, list):
        raise ValueError(f"{owner} field signals must be a list")
    normalized_signals: list[dict[str, Any]] = []
    for index, item in enumerate(raw_signals):
        if not isinstance(item, Mapping):
            raise ValueError(f"{owner} signal item {index} must be a mapping")
        normalized_signals.append(normalize_signal_item(item, source=name))
    payload["signals"] = normalized_signals

    if "preview" in payload or "preview_events" in payload:
        preview = normalize_indicator_preview_result(
            {
                "latest": payload.get("preview"),
                "events": payload.get("preview_events", []),
            },
            name=owner,
        )
        if "preview" in payload:
            payload["preview"] = preview["latest"]
        if "preview_events" in payload:
            payload["preview_events"] = preview["events"]
    return payload


def validate_indicator_result_contract(
    result: Mapping[str, Any],
    *,
    name: str = "",
    renderer_contract: Mapping[str, Any] | None = None,
    runtime_payload_contract: Mapping[str, Any] | None = None,
) -> None:
    missing = sorted(REQUIRED_INDICATOR_KEYS.difference(result.keys()))
    if missing:
        raise ValueError(
            f"{name or result.get('id') or 'indicator'} missing contract keys: {', '.join(missing)}"
        )
    if result.get("contract") != INDICATOR_CONTRACT_VERSION:
        raise ValueError(
            f"{name or result.get('id') or 'indicator'} has invalid indicator contract"
        )
    if name and result.get("id") != name:
        raise ValueError(f"{name} has mismatched indicator id: {result.get('id')}")
    for key in ("series", "events", "overlays", "signals"):
        if not isinstance(result.get(key), list):
            raise ValueError(
                f"{name or result.get('id') or 'indicator'} field {key} must be a list"
            )
    if not isinstance(result.get("settings"), dict):
        raise ValueError(f"{name or result.get('id') or 'indicator'} field settings must be a dict")
    latest = result.get("latest")
    if latest is not None and not isinstance(latest, Mapping):
        raise ValueError(
            f"{name or result.get('id') or 'indicator'} field latest must be a mapping or null"
        )
    if isinstance(latest, Mapping):
        _validate_fact_contract(
            latest,
            name=name or str(result.get("id") or "indicator"),
            section="latest",
        )
        _validate_nested_fact_contract(
            latest,
            field="signal",
            name=name or str(result.get("id") or "indicator"),
            section="latest",
        )
    preview = result.get("preview")
    if preview is not None:
        if not isinstance(preview, Mapping):
            raise ValueError(
                f"{name or result.get('id') or 'indicator'} field preview must be a mapping or null"
            )
        _validate_preview_item(
            preview,
            name=name or str(result.get("id") or "indicator"),
            section="preview",
            require_confirmed=True,
            require_ts=False,
        )
    preview_events = result.get("preview_events")
    if preview_events is not None:
        if not isinstance(preview_events, list):
            raise ValueError(
                f"{name or result.get('id') or 'indicator'} field preview_events must be a list"
            )
        for index, item in enumerate(preview_events):
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"{name or result.get('id') or 'indicator'} "
                    f"preview event item {index} must be a mapping"
                )
            _validate_preview_item(
                item,
                name=name or str(result.get("id") or "indicator"),
                section=f"preview event item {index}",
                require_confirmed=True,
                require_ts=True,
            )
    opinion = result.get("opinion")
    if opinion is not None:
        if not isinstance(opinion, Mapping):
            raise ValueError(f"{name or result.get('id') or 'indicator'} opinion must be a mapping")
        _validate_fact_contract(
            opinion,
            name=name or str(result.get("id") or "indicator"),
            section="opinion",
        )
        _validate_nested_fact_contract(
            opinion,
            field="signal",
            name=name or str(result.get("id") or "indicator"),
            section="opinion",
        )
    status = result.get("status")
    if not isinstance(status, Mapping):
        raise ValueError(
            f"{name or result.get('id') or 'indicator'} field status must be a mapping"
        )
    for status_field in (
        "id",
        "state_code",
        "health",
        "reason_code",
        "trigger_event",
        "mode",
        "bar_count",
        "calculated_at",
    ):
        if status_field not in status:
            raise ValueError(
                f"{name or result.get('id') or 'indicator'} status missing {status_field}"
            )
    _validate_status_contract(status, name=name or str(result.get("id") or "indicator"))
    for key in ("series", "events"):
        for item in result.get(key, []):
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"{name or result.get('id') or 'indicator'} {key} item must be a mapping"
                )
            _validate_fact_contract(
                item,
                name=name or str(result.get("id") or "indicator"),
                section=f"{key} item",
            )
            _validate_nested_fact_contract(
                item,
                field="signal",
                name=name or str(result.get("id") or "indicator"),
                section=f"{key} item",
            )
    for item in result.get("overlays", []):
        _validate_overlay_contract(item, name=name or str(result.get("id") or "indicator"))
    for item in result.get("signals", []):
        if not isinstance(item, Mapping):
            raise ValueError(f"{name or result.get('id') or 'indicator'} signal must be a mapping")
        _validate_fact_contract(
            item,
            name=name or str(result.get("id") or "indicator"),
            section="signal",
        )
        details = item.get("details")
        if details is not None and not isinstance(details, Mapping):
            raise ValueError(
                f"{name or result.get('id') or 'indicator'} signal details must be a mapping"
            )
        if isinstance(details, Mapping):
            try:
                domain_wire_value(details, field_name="signal.details")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{name or result.get('id') or 'indicator'} signal details "
                    f"must contain JSON-safe typed values: {exc}"
                ) from exc
        overlays = item.get("overlays")
        if not isinstance(overlays, list):
            raise ValueError(
                f"{name or result.get('id') or 'indicator'} signal overlays must be a list"
            )
        for overlay in overlays:
            _validate_overlay_contract(overlay, name=name or str(result.get("id") or "indicator"))
        plan = item.get("trade_plan")
        if isinstance(plan, Mapping):
            _validate_trade_plan_contract(plan, name=name or str(result.get("id") or "indicator"))
    if renderer_contract is not None:
        _validate_renderer_contract(
            result, renderer_contract, name=name or str(result.get("id") or "indicator")
        )
    if runtime_payload_contract is not None:
        _validate_runtime_payload_contract(
            result,
            runtime_payload_contract,
            name=name or str(result.get("id") or "indicator"),
        )


def _validate_status_contract(status: Mapping[str, Any], *, name: str) -> None:
    if status.get("id") != name:
        raise ValueError(f"{name} status has mismatched id: {status.get('id')}")
    for legacy_field in ("state", "message"):
        if legacy_field in status:
            raise ValueError(f"{name} status field {legacy_field} is a display-text legacy field")
    for status_field in ("state_code", "health", "reason_code", "mode", "calculated_at"):
        if (
            not isinstance(status.get(status_field), str)
            or not str(status.get(status_field)).strip()
        ):
            raise ValueError(f"{name} status field {status_field} must be a non-empty string")
    trigger_event = status.get("trigger_event")
    if not isinstance(trigger_event, Mapping):
        raise ValueError(f"{name} status field trigger_event must contain a non-empty code")
    try:
        DomainFact.from_mapping(trigger_event)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} status field trigger_event is invalid: {exc}") from exc
    bar_count = status.get("bar_count")
    if not isinstance(bar_count, int) or bar_count < 0:
        raise ValueError(f"{name} status field bar_count must be a non-negative int")
    for count_field in (
        "event_count",
        "preview_event_count",
        "series_count",
        "recent_event_count",
        "recent_blocked_count",
    ):
        value = status.get(count_field)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} status field {count_field} must be a non-negative int")


def _validate_overlay_contract(item: Any, *, name: str) -> None:
    if not isinstance(item, Mapping):
        raise ValueError(f"{name} overlay must be a mapping")
    if item.get("contract") != OVERLAY_CONTRACT_VERSION:
        raise ValueError(f"{name} overlay has invalid contract")
    _validate_fact_contract(item, name=name, section="overlay")
    _validate_nested_fact_contract(
        item,
        field="signal",
        name=name,
        section="overlay",
    )
    overlay_type = item.get("type")
    if overlay_type not in ALLOWED_OVERLAY_TYPES:
        raise ValueError(f"{name} overlay has invalid type")
    layer = item.get("layer")
    if layer is not None and layer not in ALLOWED_OVERLAY_LAYERS:
        raise ValueError(f"{name} overlay has invalid layer")
    missing = _overlay_renderer_missing_fields(item)
    if missing:
        raise ValueError(
            f"{name} {overlay_type} overlay missing renderer fields: {', '.join(missing)}"
        )
    _validate_overlay_renderer_values(item, name=name)
    if overlay_type == "custom":
        try:
            domain_wire_value(item["payload"], field_name="overlay.payload")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name} custom overlay payload must contain JSON-safe typed values: {exc}"
            ) from exc
    plan = item.get("trade_plan")
    if isinstance(plan, Mapping):
        _validate_trade_plan_contract(plan, name=name)


def _iter_runtime_overlays(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    overlays: list[Mapping[str, Any]] = [
        item for item in result.get("overlays", []) if isinstance(item, Mapping)
    ]
    for signal in result.get("signals", []):
        if not isinstance(signal, Mapping):
            continue
        overlays.extend(item for item in signal.get("overlays", []) if isinstance(item, Mapping))
    return overlays


def _validate_renderer_contract(
    result: Mapping[str, Any], renderer_contract: Mapping[str, Any], *, name: str
) -> None:
    primitives = renderer_contract.get("primitives")
    placements = renderer_contract.get("placements")
    renderer_ref = str(renderer_contract.get("ref") or "").strip()
    if not isinstance(primitives, list):
        raise ValueError(f"{name} renderer contract primitives must be a list")
    if not isinstance(placements, list):
        raise ValueError(f"{name} renderer contract placements must be a list")
    declared_primitives = {str(item) for item in primitives if str(item).strip()}
    declared_placements = {str(item) for item in placements if str(item).strip()}
    for item in _iter_runtime_overlays(result):
        overlay_type = str(item.get("type") or "")
        if overlay_type not in declared_primitives:
            raise ValueError(f"{name} {overlay_type} overlay not declared by renderer primitives")
        if overlay_type == "custom" and str(item.get("renderer_ref") or "").strip() != renderer_ref:
            raise ValueError(f"{name} custom overlay renderer_ref does not match renderer contract")
        required_placement = OVERLAY_TYPE_PLACEMENTS.get(overlay_type, "price")
        if required_placement not in declared_placements:
            raise ValueError(
                f"{name} {overlay_type} overlay requires renderer placement {required_placement}"
            )


def _declared_payload_fields(runtime_payload_contract: Mapping[str, Any], section: str) -> set[str]:
    raw = runtime_payload_contract.get(section)
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(item) for item in raw if str(item).strip()}


def _validate_mapping_keys(
    item: Mapping[str, Any],
    allowed: set[str],
    *,
    name: str,
    section: str,
) -> None:
    if not allowed:
        return
    extra = sorted(str(key) for key in item.keys() if str(key) not in allowed)
    if extra:
        raise ValueError(
            f"{name} {section} has undeclared runtime payload fields: {', '.join(extra)}"
        )


def _validate_runtime_payload_contract(
    result: Mapping[str, Any],
    runtime_payload_contract: Mapping[str, Any],
    *,
    name: str,
) -> None:
    top_level_allowed = set(REQUIRED_INDICATOR_KEYS) | _declared_payload_fields(
        runtime_payload_contract, "top_level"
    )
    _validate_mapping_keys(result, top_level_allowed, name=name, section="result")

    latest = result.get("latest")
    if isinstance(latest, Mapping):
        _validate_mapping_keys(
            latest,
            _declared_payload_fields(runtime_payload_contract, "latest"),
            name=name,
            section="latest",
        )
    preview = result.get("preview")
    preview_allowed = _declared_payload_fields(runtime_payload_contract, "latest")
    if isinstance(preview, Mapping) and preview_allowed:
        _validate_mapping_keys(
            preview,
            preview_allowed | {"confirmed"},
            name=name,
            section="preview",
        )
    preview_events = result.get("preview_events")
    preview_event_allowed = _declared_payload_fields(runtime_payload_contract, "events")
    if isinstance(preview_events, list) and preview_event_allowed:
        for item in preview_events:
            if isinstance(item, Mapping):
                _validate_mapping_keys(
                    item,
                    preview_event_allowed | {"confirmed"},
                    name=name,
                    section="preview_events",
                )
    list_sections = {"series", "events", "signals"}
    list_sections.update(
        section
        for section, fields in runtime_payload_contract.items()
        if section not in {"top_level", "latest", "status"}
        and isinstance(fields, (list, tuple, set))
    )
    for section in sorted(list_sections):
        allowed = _declared_payload_fields(runtime_payload_contract, section)
        for item in result.get(section, []):
            if isinstance(item, Mapping):
                if section not in {"series", "events", "overlays", "signals"}:
                    _validate_fact_contract(item, name=name, section=f"{section} item")
                _validate_mapping_keys(item, allowed, name=name, section=section)
    status = result.get("status")
    if isinstance(status, Mapping):
        _validate_mapping_keys(
            status,
            {
                "id",
                "label",
                "calculates",
                "preview_active",
                "mode",
                "bar_count",
                "analysis_ts",
                "input_first_ts",
                "input_last_ts",
                "calculated_at",
                "elapsed_ms",
                "event_count",
                "preview_event_count",
                "series_count",
                "recent_event_count",
                "recent_blocked_count",
                "last_event_ts",
                "latest_state",
                "params_hash",
                "last_error",
            }
            | _declared_payload_fields(runtime_payload_contract, "status"),
            name=name,
            section="status",
        )


def _has_any(item: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    return any(field in item and item.get(field) is not None for field in fields)


def _has_renderable_overlay_text(item: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    for field in fields:
        value = item.get(field)
        if isinstance(value, (list, tuple)):
            if any(str(part).strip() for part in value if part is not None):
                return True
        elif value is not None and str(value).strip():
            return True
    return False


def _validate_overlay_renderer_values(item: Mapping[str, Any], *, name: str) -> None:
    overlay_type = str(item.get("type") or "")
    if "signal_overlay" in item and type(item.get("signal_overlay")) is not bool:
        raise ValueError(f"{name} {overlay_type} overlay signal_overlay must be a boolean")
    invalid_timestamps = [
        field
        for field in ("ts", "start_ts", "end_ts", "end_anchor_ts")
        if field in item and not is_exact_utc_indicator_timestamp(item.get(field))
    ]
    if invalid_timestamps:
        raise ValueError(
            f"{name} {overlay_type} overlay has invalid UTC timestamps: "
            f"{', '.join(invalid_timestamps)}"
        )

    if overlay_type == "box":
        numeric_fields = ("top", "bottom")
    elif overlay_type == "line":
        numeric_fields = tuple(field for field in ("price", "y1", "y2") if field in item)
    elif overlay_type in {"label", "marker"}:
        numeric_fields = ("price",)
    else:
        numeric_fields = ()
    invalid_numbers = [
        field for field in numeric_fields if not is_exact_finite_number(item.get(field))
    ]
    if invalid_numbers:
        raise ValueError(
            f"{name} {overlay_type} overlay has non-finite renderer values: "
            f"{', '.join(invalid_numbers)}"
        )

    forbidden_bar_slots = [
        field
        for field in (
            "bar_slot",
            "start_bar_slot",
            "end_bar_slot",
            "end_anchor_bar_slot",
            "origin_end_bar_slot",
        )
        if field in item
    ]
    if forbidden_bar_slots:
        raise ValueError(
            f"{name} {overlay_type} overlay must use confirmed timestamps and relative bar "
            f"offsets; schedule-ranked bar slots are forbidden: {', '.join(forbidden_bar_slots)}"
        )

    retention = item.get("retention")
    if retention is not None and retention not in {"active", "history"}:
        raise ValueError(f"{name} {overlay_type} overlay has invalid retention")


def _overlay_renderer_missing_fields(item: Mapping[str, Any]) -> list[str]:
    overlay_type = str(item.get("type") or "")
    missing: list[str] = []
    has_projected_end = "end_anchor_ts" in item or "end_bar_offset" in item
    projected_end = (
        isinstance(item.get("end_anchor_ts"), str)
        and bool(item.get("end_anchor_ts"))
        and isinstance(item.get("end_bar_offset"), int)
        and not isinstance(item.get("end_bar_offset"), bool)
        and int(item.get("end_bar_offset")) >= 1
    )
    if overlay_type == "box":
        if not _has_any(item, ("ts", "start_ts")):
            missing.append("start_ts")
        if not _has_any(item, ("ts", "end_ts")) and not projected_end:
            missing.append("end_ts")
        if has_projected_end and (not projected_end or _has_any(item, ("end_ts",))):
            missing.append("projected_end")
        for field in ("top", "bottom"):
            if item.get(field) is None:
                missing.append(field)
    elif overlay_type == "line":
        if not _has_any(item, ("ts", "start_ts")):
            missing.append("start_ts")
        if not _has_any(item, ("ts", "end_ts")) and not projected_end:
            missing.append("end_ts")
        if has_projected_end and (not projected_end or _has_any(item, ("end_ts",))):
            missing.append("projected_end")
        if not _has_any(item, ("price", "y1")):
            missing.append("y1")
        if not _has_any(item, ("price", "y1", "y2")):
            missing.append("y2")
    elif overlay_type in {"label", "marker"}:
        for field in ("ts", "price"):
            if item.get(field) is None:
                missing.append(field)
        if item.get("glyph_only") is True and not _has_renderable_overlay_text(
            item, ("lines", "glyph", "glyph_kind", "event", "code")
        ):
            missing.append("glyph")
        semantic_marker = overlay_type == "marker" and _has_any(item, ("pivot_number",))
        has_text = (
            _has_renderable_overlay_text(
                item,
                ("lines", "label", "glyph", "event", "code", "state", "compact_label"),
            )
            or item.get("fuel") is True
            or item.get("terminal_climax") is True
        )
        if not semantic_marker and not has_text:
            missing.append("label")
    elif overlay_type == "table":
        table = item.get("table")
        if not isinstance(table, Mapping):
            missing.append("table")
        elif (
            not isinstance(table.get("columns"), list)
            and not str(table.get("model_ref") or "").strip()
        ):
            missing.append("table.columns")
    elif overlay_type == "custom":
        renderer_ref = item.get("renderer_ref")
        if not isinstance(renderer_ref, str) or not renderer_ref.strip():
            missing.append("renderer_ref")
        if not isinstance(item.get("payload"), Mapping):
            missing.append("payload")
        render_key = item.get("render_key")
        if not isinstance(render_key, str) or not render_key.strip():
            missing.append("render_key")
    return missing


def _validate_trade_plan_contract(plan: Mapping[str, Any], *, name: str) -> None:
    missing = sorted(REQUIRED_TRADE_PLAN_KEYS.difference(plan.keys()))
    if missing:
        raise ValueError(f"{name} trade_plan missing contract keys: {', '.join(missing)}")
    direction = plan.get("direction")
    if direction not in {"long", "short", "flat"}:
        raise ValueError(f"{name} trade_plan has invalid direction")
    for plan_field in ("complete", "coherent", "actionable"):
        if not isinstance(plan.get(plan_field), bool):
            raise ValueError(f"{name} trade_plan field {plan_field} must be a bool")
