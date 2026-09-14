from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from aef_terminal.domain import BarState, bar_revision_signature
from aef_terminal.engine.serialization import is_gap_placeholder_payload
from aef_terminal.engine.vsa_context import (
    VSA_VOLUME_COMPACT_CONTRACT,
    VSA_VOLUME_COMPACT_POLICY,
    VSA_VOLUME_RENDER_DEFAULT_HOURS,
    VSA_VOLUME_RENDER_MAX_HOURS,
    bound_vsa_volume_render_context,
)
from aef_terminal.indicators.registry import indicator_manifest

_MAX_RUNTIME_GAP_SLOTS = 64


@dataclass(frozen=True, slots=True)
class AnalysisBarWindow:
    count: int
    first_ts: str | None
    latest_ts: str | None
    latest_close: float | None
    latest_volume: float | None
    window_hash: str


def analysis_bar_window(value: object) -> AnalysisBarWindow:
    """Validate and identify one exact confirmed serialized analysis window."""

    if not isinstance(value, list):
        raise TypeError("analysis bars must be a list")
    rows: list[list[Any]] = []
    first_ts: str | None = None
    latest_ts: str | None = None
    latest_close: float | None = None
    latest_volume: float | None = None
    previous_timestamp = None
    timeframe: str | None = None
    for bar in value:
        if not isinstance(bar, dict):
            raise TypeError("analysis bars must contain only mappings")
        if is_gap_placeholder_payload(bar):
            raise ValueError("analysis bars cannot contain display placeholders")
        (
            timestamp,
            open_price,
            high,
            low,
            close,
            volume,
            state,
            source,
        ) = bar_revision_signature(bar)
        if state != BarState.CONFIRMED.value:
            raise ValueError("analysis bars must contain only confirmed rows")
        raw_timeframe = bar.get("timeframe")
        if (
            not isinstance(raw_timeframe, str)
            or not raw_timeframe
            or raw_timeframe != raw_timeframe.strip()
        ):
            raise ValueError("analysis bar timeframe is invalid")
        if timeframe is None:
            timeframe = raw_timeframe
        elif raw_timeframe != timeframe:
            raise ValueError("analysis bars must use one timeframe")
        symbol = bar.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
            raise ValueError("analysis bar symbol is invalid")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("analysis bars must be strictly timestamp-ordered")
        timestamp_text = timestamp.isoformat()
        first_ts = first_ts or timestamp_text
        latest_ts = timestamp_text
        latest_close = close
        latest_volume = volume
        rows.append(
            [
                timestamp_text,
                open_price,
                high,
                low,
                close,
                volume,
                state,
                source,
                raw_timeframe,
                symbol,
            ]
        )
        previous_timestamp = timestamp
    raw = json.dumps(rows, separators=(",", ":"))
    return AnalysisBarWindow(
        count=len(rows),
        first_ts=first_ts,
        latest_ts=latest_ts,
        latest_close=latest_close,
        latest_volume=latest_volume,
        window_hash=(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] if rows else ""),
    )


@lru_cache(maxsize=1)
def _runtime_payload_contracts() -> dict[str, dict[str, tuple[str, ...]]]:
    contracts: dict[str, dict[str, tuple[str, ...]]] = {}
    for indicator_id, spec in indicator_manifest().items():
        raw_contract = spec.get("runtime_payload_contract")
        if not isinstance(raw_contract, dict):
            continue
        contracts[indicator_id] = {
            section: tuple(str(field) for field in fields)
            for section, fields in raw_contract.items()
            if isinstance(fields, list)
        }
    return contracts


def _runtime_payload_fields(indicator_id: str, section: str) -> tuple[str, ...] | None:
    fields = _runtime_payload_contracts().get(indicator_id, {}).get(section, ())
    return fields or None


@lru_cache(maxsize=1)
def _runtime_payload_compact_contracts() -> dict[str, dict[str, tuple[str, ...]]]:
    contracts: dict[str, dict[str, tuple[str, ...]]] = {}
    for indicator_id, spec in indicator_manifest().items():
        raw_contract = spec.get("runtime_payload_compact_contract")
        if not isinstance(raw_contract, dict):
            continue
        contracts[indicator_id] = {
            section: tuple(str(field) for field in fields)
            for section, fields in raw_contract.items()
            if isinstance(fields, list)
        }
    return contracts


def _runtime_payload_compact_fields(indicator_id: str, section: str) -> tuple[str, ...] | None:
    fields = _runtime_payload_compact_contracts().get(indicator_id, {}).get(section, ())
    return fields or None


@lru_cache(maxsize=1)
def _runtime_payload_compact_policies() -> dict[str, dict[str, dict[str, Any]]]:
    policies: dict[str, dict[str, dict[str, Any]]] = {}
    for indicator_id, spec in indicator_manifest().items():
        raw_policy = spec.get("runtime_payload_compact")
        if not isinstance(raw_policy, dict):
            continue
        sections: dict[str, dict[str, Any]] = {}
        for section, policy in raw_policy.items():
            if isinstance(policy, dict):
                sections[str(section)] = dict(policy)
        if sections:
            policies[indicator_id] = sections
    return policies


def _compact_policy_fields(
    indicator_id: str, section: str, policy: dict[str, Any]
) -> tuple[str, ...] | None:
    fields = policy.get("fields")
    if fields == "runtime_contract":
        contract_fields = _runtime_payload_fields(indicator_id, section)
        if contract_fields is None:
            raise ValueError(f"Missing runtime payload contract for {indicator_id}.{section}")
        return contract_fields
    if fields == "compact_contract":
        contract_fields = _runtime_payload_compact_fields(indicator_id, section)
        if contract_fields is None:
            raise ValueError(
                f"Missing compact runtime payload contract for {indicator_id}.{section}"
            )
        return contract_fields
    if isinstance(fields, (list, tuple)):
        return tuple(str(field) for field in fields)
    if isinstance(fields, str):
        raise ValueError(
            f"Unknown runtime payload compact fields ref for {indicator_id}.{section}: {fields}"
        )
    return None


def _compact_object_fields(item: Any, fields: tuple[str, ...] | None) -> Any:
    if not isinstance(item, dict):
        return item
    if not fields:
        return item
    return {key: item[key] for key in fields if key in item}


def _compact_array_tail(value: Any, limit: int) -> Any:
    if not isinstance(value, list):
        return value
    max_items = max(int(limit or 0), 0)
    if max_items and len(value) > max_items:
        return value[-max_items:]
    return value


def _compact_overlay_tail(value: Any, limit: int) -> Any:
    if not isinstance(value, list):
        return value
    max_items = max(int(limit or 0), 0)
    if not max_items or len(value) <= max_items:
        return value
    active_indices = {
        index
        for index, item in enumerate(value)
        if isinstance(item, dict) and item.get("retention") == "active"
    }
    if not active_indices:
        return value[-max_items:]
    history_slots = max(max_items - len(active_indices), 0)
    history_indices = [index for index in range(len(value)) if index not in active_indices]
    selected = active_indices | set(history_indices[-history_slots:] if history_slots else ())
    return [item for index, item in enumerate(value) if index in selected]


def _compact_indicator_array(
    indicator: dict[str, Any], key: str, limit: int, fields: tuple[str, ...] | None = None
) -> None:
    rows = (
        _compact_overlay_tail(indicator.get(key), limit)
        if key == "overlays"
        else _compact_array_tail(indicator.get(key), limit)
    )
    if not isinstance(rows, list):
        return
    indicator[key] = [_compact_object_fields(item, fields) for item in rows] if fields else rows


def _compact_gap_quality(meta: dict[str, Any]) -> None:
    for key in ("data_quality", "chart_data_quality"):
        quality = meta.get(key)
        if not isinstance(quality, dict):
            continue
        gaps = quality.get("gaps")
        if not isinstance(gaps, list):
            continue
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            slots = gap.get("missing_slots")
            if isinstance(slots, list) and len(slots) > _MAX_RUNTIME_GAP_SLOTS:
                gap["missing_slots_count"] = len(slots)
                gap["missing_slots"] = slots[:_MAX_RUNTIME_GAP_SLOTS]


def _record_overlay_compaction(
    snapshot: dict[str, Any],
    *,
    indicator_id: str,
    before: int,
    after: int,
) -> None:
    dropped = max(before - after, 0)
    if not dropped:
        return
    meta = snapshot.setdefault("meta", {})
    diagnostics = meta.setdefault("indicator_overlay_compaction", {})
    existing = diagnostics.get(indicator_id)
    previous = existing if isinstance(existing, dict) else {}
    diagnostics[indicator_id] = {
        "dropped": int(previous.get("dropped") or 0) + dropped,
        "retained": after,
    }


def _compact_analysis_indicators_for_cache(snapshot: dict[str, Any]) -> None:
    indicators = snapshot.get("indicators")
    if not isinstance(indicators, dict):
        return

    for indicator_id, section_policies in _runtime_payload_compact_policies().items():
        indicator = indicators.get(indicator_id)
        if not isinstance(indicator, dict):
            continue
        for section, policy in section_policies.items():
            if policy.get("drop") is True:
                indicator.pop(section, None)
                continue
            limit = int(policy.get("limit") or 0)
            if limit <= 0:
                continue
            before = (
                len(indicator.get(section) or []) if isinstance(indicator.get(section), list) else 0
            )
            _compact_indicator_array(
                indicator,
                section,
                limit,
                _compact_policy_fields(indicator_id, section, policy),
            )
            if section == "overlays":
                after = (
                    len(indicator.get(section) or [])
                    if isinstance(indicator.get(section), list)
                    else 0
                )
                _record_overlay_compaction(
                    snapshot,
                    indicator_id=indicator_id,
                    before=before,
                    after=after,
                )


def _compact_vsa_volume_context(
    snapshot: dict[str, Any],
    *,
    render_hours: int,
) -> None:
    context = snapshot.get("vsa_volume")
    if not isinstance(context, dict):
        return
    overlays_before_window = (
        len(context.get("overlays") or []) if isinstance(context.get("overlays"), list) else 0
    )
    context = bound_vsa_volume_render_context(context, render_hours=render_hours)
    snapshot["vsa_volume"] = context
    overlays_after_window = (
        len(context.get("overlays") or []) if isinstance(context.get("overlays"), list) else 0
    )
    _record_overlay_compaction(
        snapshot,
        indicator_id="vsa_volume",
        before=overlays_before_window,
        after=overlays_after_window,
    )
    for section, policy in VSA_VOLUME_COMPACT_POLICY.items():
        before = len(context.get(section) or []) if isinstance(context.get(section), list) else 0
        _compact_indicator_array(
            context,
            section,
            int(policy["limit"]),
            VSA_VOLUME_COMPACT_CONTRACT.get(section),
        )
        if section == "overlays":
            after = len(context.get(section) or []) if isinstance(context.get(section), list) else 0
            _record_overlay_compaction(
                snapshot,
                indicator_id="vsa_volume",
                before=before,
                after=after,
            )


def compact_runtime_payload_for_response(
    snapshot: dict[str, Any],
    *,
    vsa_render_hours: int = VSA_VOLUME_RENDER_DEFAULT_HOURS,
) -> dict[str, Any]:
    meta = snapshot.get("meta")
    if isinstance(meta, dict):
        _compact_gap_quality(meta)
    _compact_analysis_indicators_for_cache(snapshot)
    _compact_vsa_volume_context(snapshot, render_hours=vsa_render_hours)
    return snapshot


def compact_analysis_snapshot_for_cache(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise TypeError("analysis snapshot must be a mapping")
    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        raise TypeError("analysis snapshot meta must be a mapping")
    if "bars" not in snapshot:
        raise ValueError("analysis snapshot bars are required before compaction")
    snapshot.pop("chart_guides", None)
    bars = snapshot.pop("bars")
    window = analysis_bar_window(bars)
    meta["analysis_window_hash"] = window.window_hash
    meta["analysis_bar_count"] = window.count
    meta["analysis_first_ts"] = window.first_ts
    meta["analysis_latest_ts"] = window.latest_ts
    if window.latest_ts is not None:
        existing_analysis_ts = meta.get("analysis_ts")
        if existing_analysis_ts not in {None, window.latest_ts}:
            raise ValueError("analysis snapshot timestamp disagrees with its confirmed window")
        meta["analysis_ts"] = window.latest_ts
    _compact_gap_quality(meta)
    _compact_analysis_indicators_for_cache(snapshot)
    _compact_vsa_volume_context(snapshot, render_hours=VSA_VOLUME_RENDER_MAX_HOURS)
    meta["analysis_only"] = True
    meta["analysis_bars_included"] = False
    return snapshot
