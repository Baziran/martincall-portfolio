from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final, Literal

from aef_terminal.domain import bar_revision_signature
from aef_terminal.engine.serialization import is_gap_placeholder_payload
from aef_terminal.runtime.clock import utc_now_iso as chart_stream_ts


ChartStreamConsumerRole = Literal["primary", "secondary_candles"]
CHART_STREAM_CONSUMER_ROLE_PRIMARY: Final[ChartStreamConsumerRole] = "primary"
CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES: Final[ChartStreamConsumerRole] = "secondary_candles"
CHART_STREAM_SECONDARY_TAIL_MIN: Final[int] = 64
CHART_STREAM_SECONDARY_TAIL_MAX: Final[int] = 500


def require_chart_stream_consumer_role(value: object) -> ChartStreamConsumerRole:
    if value == CHART_STREAM_CONSUMER_ROLE_PRIMARY:
        return CHART_STREAM_CONSUMER_ROLE_PRIMARY
    if value == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES:
        return CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
    raise ValueError(f"CHART_STREAM_CONSUMER_ROLE_UNSUPPORTED role={value}")


def _require_nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _require_nonempty_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be exact non-empty text")
    return value


def _require_utc_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a UTC timestamp")
    return parsed


def require_chart_stream_bar_payload(
    value: object,
    *,
    interval: str,
) -> dict[str, Any]:
    """Require one real typed bar payload before chart-frame publication."""

    if not isinstance(value, dict):
        raise TypeError("chart stream bars must contain only mappings")
    if is_gap_placeholder_payload(value):
        raise ValueError("chart stream frames cannot contain gap placeholders")
    bar_revision_signature(value)
    _require_utc_timestamp(value.get("ts"), field="chart bar timestamp")
    _require_nonempty_text(value.get("symbol"), field="chart bar symbol")
    _require_nonempty_text(value.get("source"), field="chart bar source")
    if value.get("timeframe") != interval:
        raise ValueError("chart stream bar timeframe must match the frame interval")
    if value.get("state") is None:
        raise ValueError("chart stream bar state is required")
    for field in ("authoritative", "commit_pending", "bar_slot_authoritative"):
        if field not in value or not isinstance(value[field], bool):
            raise TypeError(f"chart stream bar {field} must be boolean")
    if value["authoritative"] is True and (
        value["closed"] is not True
        or value["state"] != "confirmed"
        or value["commit_pending"] is not False
    ):
        raise ValueError("authoritative chart stream bars must be committed and confirmed")
    if value["commit_pending"] is True and value["authoritative"] is not False:
        raise ValueError("commit-pending chart stream bars must be provisional")
    if "canonical_revision" in value:
        _require_nonnegative_int(
            value["canonical_revision"],
            field="chart stream bar canonical_revision",
        )
    if value.get("bar_slot") is not None and (
        isinstance(value["bar_slot"], bool) or not isinstance(value["bar_slot"], int)
    ):
        raise TypeError("chart stream bar bar_slot must be an integer or null")
    if value.get("bar_slot_authoritative") is True and value.get("bar_slot") is None:
        raise ValueError("authoritative chart stream bar_slot is required")
    if "bar_slot_schedule_state" in value:
        _require_nonempty_text(
            value["bar_slot_schedule_state"],
            field="chart stream bar_slot_schedule_state",
        )
    return value


def require_chart_future_axis_payload(
    value: object,
    *,
    interval: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("chart stream future_axis must be a mapping")
    if value.get("kind") != "provider_session_future_axis":
        raise ValueError("chart stream future_axis kind is invalid")
    timeframe = _require_nonempty_text(
        value.get("timeframe"),
        field="chart stream future_axis timeframe",
    )
    if interval is not None and timeframe != interval:
        raise ValueError("chart stream future_axis timeframe does not match its frame")
    _require_nonnegative_int(
        value.get("schedule_revision"),
        field="chart stream future_axis schedule_revision",
    )
    schedule_state = value.get("schedule_state")
    if schedule_state not in {"unknown", "continuous", "verified"}:
        raise ValueError("chart stream future_axis schedule_state is invalid")
    requested_slots = value.get("requested_slots")
    if (
        isinstance(requested_slots, bool)
        or not isinstance(requested_slots, int)
        or requested_slots <= 0
    ):
        raise ValueError("chart stream future_axis requested_slots is invalid")
    if not isinstance(value.get("complete"), bool):
        raise TypeError("chart stream future_axis complete must be boolean")
    anchor = value.get("anchor_ts")
    parsed_anchor = (
        _require_utc_timestamp(anchor, field="chart stream future_axis anchor_ts")
        if anchor is not None
        else None
    )
    slots = value.get("slots")
    if not isinstance(slots, list) or len(slots) > requested_slots:
        raise ValueError("chart stream future_axis slots are invalid")
    previous_ts: datetime | None = None
    for index, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict):
            raise TypeError("chart stream future_axis slots must contain mappings")
        timestamp = _require_utc_timestamp(
            slot.get("ts"),
            field="chart stream future_axis slot timestamp",
        )
        offset = slot.get("bar_offset")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset != index
            or (previous_ts is not None and timestamp <= previous_ts)
            or (parsed_anchor is not None and timestamp <= parsed_anchor)
        ):
            raise ValueError("chart stream future_axis slots must be strictly ordered")
        previous_ts = timestamp
    if slots and parsed_anchor is None:
        raise ValueError("chart stream future_axis slots require an anchor")
    if schedule_state == "unknown" and (value["complete"] is True or slots):
        raise ValueError("unknown chart stream future_axis cannot be complete")
    return value


def require_secondary_tail_membership(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_INVALID")
    limit = value.get("limit")
    timestamps = value.get("timestamps")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not CHART_STREAM_SECONDARY_TAIL_MIN <= limit <= CHART_STREAM_SECONDARY_TAIL_MAX
    ):
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_LIMIT_INVALID")
    if not isinstance(timestamps, list):
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_TIMESTAMPS_INVALID")
    if any(not isinstance(timestamp, str) or not timestamp for timestamp in timestamps):
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_TIMESTAMPS_INVALID")
    ordered_timestamps = list(timestamps)
    try:
        parsed_timestamps = [
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            for timestamp in ordered_timestamps
        ]
    except ValueError as exc:
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_TIMESTAMPS_INVALID") from exc
    if (
        any(
            timestamp.tzinfo is None
            or timestamp.utcoffset() is None
            or timestamp.utcoffset() != timedelta(0)
            for timestamp in parsed_timestamps
        )
        or len(ordered_timestamps) > limit
        or len(set(ordered_timestamps)) != len(ordered_timestamps)
        or len(set(parsed_timestamps)) != len(parsed_timestamps)
        or parsed_timestamps != sorted(parsed_timestamps)
    ):
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_TIMESTAMPS_INVALID")
    return {"limit": limit, "timestamps": ordered_timestamps}


def chart_heartbeat_payload(
    *,
    source: str,
    instrument_id: str,
    route_fingerprint: str,
    symbol: str,
    provider_symbol: str,
    interval: str,
    range_: str,
    consumer_role: ChartStreamConsumerRole = CHART_STREAM_CONSUMER_ROLE_PRIMARY,
) -> dict[str, Any]:
    return {
        "type": "heartbeat",
        "source": source,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "symbol": symbol,
        "provider_symbol": provider_symbol,
        "interval": interval,
        "range": range_,
        "consumer_role": require_chart_stream_consumer_role(consumer_role),
        "ts": chart_stream_ts(),
    }


def chart_status_payload(
    *,
    source: str,
    message: str,
    retry_in_seconds: float,
    instrument_id: str | None = None,
    route_fingerprint: str | None = None,
    symbol: str | None = None,
    provider_symbol: str | None = None,
    interval: str | None = None,
    range_: str | None = None,
    ok: bool | None = None,
    consumer_role: ChartStreamConsumerRole = CHART_STREAM_CONSUMER_ROLE_PRIMARY,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "chart_status",
        "source": source,
        "message": message,
        "retry_in_seconds": retry_in_seconds,
        "consumer_role": require_chart_stream_consumer_role(consumer_role),
        "ts": chart_stream_ts(),
    }
    if symbol is not None:
        payload["symbol"] = symbol
    if instrument_id is not None:
        payload["instrument_id"] = instrument_id
    if route_fingerprint is not None:
        payload["route_fingerprint"] = route_fingerprint
    if provider_symbol is not None:
        payload["provider_symbol"] = provider_symbol
    if interval is not None:
        payload["interval"] = interval
    if range_ is not None:
        payload["range"] = range_
    if ok is not None:
        payload["ok"] = ok
    return payload


def chart_bars_payload(
    *,
    source: str,
    instrument_id: str,
    route_fingerprint: str,
    symbol: str,
    provider_symbol: str,
    interval: str,
    range_: str,
    bars: list[dict[str, Any]],
    warning: str = "",
    gap_repair: dict[str, Any] | None = None,
    recovery: bool = False,
    recovery_complete: bool = False,
    recovery_scope: str = "",
    stream_seq: int = 0,
    stream_generation: int = 0,
    canonical_revision: int = 0,
    event_reason: str = "",
    chart_data_quality: dict[str, Any] | None = None,
    history_coverage: dict[str, Any] | None = None,
    future_axis: dict[str, Any] | None = None,
    expected_live_slot: str | None = None,
    expected_live_close: str | None = None,
    consumer_role: ChartStreamConsumerRole = CHART_STREAM_CONSUMER_ROLE_PRIMARY,
    secondary_tail_membership: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = _require_nonempty_text(source, field="chart stream source")
    instrument_id = _require_nonempty_text(
        instrument_id,
        field="chart stream instrument_id",
    )
    route_fingerprint = _require_nonempty_text(
        route_fingerprint,
        field="chart stream route_fingerprint",
    )
    symbol = _require_nonempty_text(symbol, field="chart stream symbol")
    provider_symbol = _require_nonempty_text(
        provider_symbol,
        field="chart stream provider_symbol",
    )
    interval = _require_nonempty_text(interval, field="chart stream interval")
    range_ = _require_nonempty_text(range_, field="chart stream range")
    if not isinstance(bars, list):
        raise TypeError("chart stream bars must be a list")
    if not isinstance(warning, str):
        raise TypeError("chart stream warning must be a string")
    if gap_repair is not None and not isinstance(gap_repair, dict):
        raise TypeError("chart stream gap_repair must be a mapping or null")
    for field, value in (
        ("recovery", recovery),
        ("recovery_complete", recovery_complete),
    ):
        if not isinstance(value, bool):
            raise TypeError(f"chart stream {field} must be boolean")
    stream_seq = _require_nonnegative_int(stream_seq, field="chart stream stream_seq")
    stream_generation = _require_nonnegative_int(
        stream_generation,
        field="chart stream stream_generation",
    )
    canonical_revision = _require_nonnegative_int(
        canonical_revision,
        field="chart stream canonical_revision",
    )
    if not isinstance(event_reason, str):
        raise TypeError("chart stream event_reason must be a string")
    if not isinstance(recovery_scope, str):
        raise TypeError("chart recovery scope must be a string")
    for field, value in (
        ("chart_data_quality", chart_data_quality),
        ("history_coverage", history_coverage),
    ):
        if value is not None and not isinstance(value, dict):
            raise TypeError(f"chart stream {field} must be a mapping or null")
    resolved_future_axis = (
        require_chart_future_axis_payload(future_axis, interval=interval)
        if future_axis is not None
        else None
    )
    typed_bars = [require_chart_stream_bar_payload(bar, interval=interval) for bar in bars]
    for bar in typed_bars:
        for field, expected in (
            ("instrument_id", instrument_id),
            ("route_fingerprint", route_fingerprint),
            ("provider_symbol", provider_symbol),
        ):
            if field in bar and bar[field] != expected:
                raise ValueError(f"chart stream bar {field} does not match its frame")
    recovery_frame = recovery or recovery_complete
    scope = recovery_scope.strip().lower()
    if recovery_scope != scope:
        raise ValueError("chart recovery scope must be a canonical code")
    if recovery_frame and scope not in {"initial", "gap_repair", "checkpoint"}:
        raise ValueError("chart recovery frames require an explicit recovery_scope")
    if not recovery_frame and scope:
        raise ValueError("live chart frames cannot declare recovery_scope")
    resolved_consumer_role = require_chart_stream_consumer_role(consumer_role)
    if resolved_consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES and recovery_frame:
        if secondary_tail_membership is None:
            raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_REQUIRED")
        resolved_membership = require_secondary_tail_membership(secondary_tail_membership)
    elif secondary_tail_membership is not None:
        raise ValueError("CHART_STREAM_SECONDARY_MEMBERSHIP_SCOPE_INVALID")
    else:
        resolved_membership = None
    phase = "recovery_complete" if recovery_complete else "recovery" if recovery_frame else "live"
    payload = {
        "type": "chart_bars",
        "source": source,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "symbol": symbol,
        "provider_symbol": provider_symbol,
        "interval": interval,
        "range": range_,
        "consumer_role": resolved_consumer_role,
        "bars": [
            {
                **bar,
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "provider_symbol": provider_symbol,
            }
            for bar in typed_bars
        ],
        "phase": phase,
        "recovery": recovery_frame,
        "recovery_complete": recovery_complete,
        "recovery_scope": scope,
        "recovery_tail": len(typed_bars) if recovery_frame else 0,
        "warning": warning,
        "gap_repair": gap_repair,
        "stream_seq": stream_seq,
        "stream_generation": stream_generation,
        "canonical_revision": canonical_revision,
        "event_reason": event_reason,
        "expected_live_slot": expected_live_slot,
        "expected_live_close": expected_live_close,
        "ts": chart_stream_ts(),
    }
    if chart_data_quality is not None:
        payload["chart_data_quality"] = dict(chart_data_quality)
        payload["chart_quality_revision"] = canonical_revision
    if history_coverage is not None:
        payload["history_coverage"] = dict(history_coverage)
    if resolved_future_axis is not None:
        payload["future_axis"] = {
            **resolved_future_axis,
            "slots": [dict(slot) for slot in resolved_future_axis["slots"]],
        }
    if resolved_membership is not None:
        payload["secondary_tail_membership"] = resolved_membership
    return payload
