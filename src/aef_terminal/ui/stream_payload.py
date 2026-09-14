from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.engine.common import interval_minutes, parse_aware_utc_ts
from aef_terminal.domain import Bar, bar_revision_signature
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.snapshot.db_context import (
    snapshot_db_context,
    chart_future_axis_payload,
)
from aef_terminal.engine.data_quality import data_quality_report
from aef_terminal.engine.serialization import (
    is_gap_placeholder_payload,
    serialize_bar,
    serialize_bars,
)
from aef_terminal.data.provider_sessions import (
    continuous_provider_bar_slot,
    provider_bar_bucket,
    provider_history_session_scope,
)
from aef_terminal.features.provider_session import provider_session_intervals, provider_vwap_session
from aef_terminal.ui.runtime.constants import (
    CHART_STREAM_MAX_RECOVERY_TAIL,
    CHART_STREAM_RECOVERY_TAIL,
)
from aef_terminal.runtime.bar_series import require_ordered_bar_list


@dataclass(frozen=True)
class StreamPayloadDeps:
    store_factory: Callable[[], Any]


@dataclass(frozen=True)
class ChartRecoverySnapshot:
    payloads: tuple[dict[str, Any], ...]
    quality: dict[str, Any]
    expected_live_slot: str | None
    expected_live_close: str | None
    future_axis: dict[str, Any] | None = None


_DEPS: StreamPayloadDeps | None = None


def configure_stream_payload_deps(deps: StreamPayloadDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> StreamPayloadDeps:
    if _DEPS is None:
        raise RuntimeError("stream payload dependencies are not configured")
    return _DEPS


def stream_bar_payload(bar: Bar, *, instrument: dict[str, Any] | None = None) -> dict:
    if not isinstance(bar, Bar):
        raise TypeError("stream bar payload requires a typed Bar")
    payload = serialize_bar(bar)
    if instrument is not None:
        payload["expected_close"] = None
        route = route_instrument(instrument)
        if route.adapter.continuous_session(route.instrument):
            payload["bar_slot"] = continuous_provider_bar_slot(
                bar.ts,
                bar.timeframe,
            )
            payload["bar_slot_authoritative"] = True
            payload["bar_slot_schedule_state"] = "continuous"
            bucket = route.adapter.provider_bar_bucket(
                route.instrument,
                bar.ts,
                bar.timeframe,
            )
        else:
            payload["bar_slot_authoritative"] = False
            payload["bar_slot_schedule_state"] = "unknown"
            session_scope = provider_history_session_scope(route.adapter, route.instrument)
            session_key = "liquid_intervals" if session_scope == "liquid" else "trading_intervals"
            session_interval = next(
                (
                    candidate
                    for candidate in provider_session_intervals(route.instrument, session_key)
                    if candidate.contains(bar.ts)
                ),
                None,
            )
            bucket = (
                route.adapter.provider_bar_bucket(
                    route.instrument,
                    bar.ts,
                    bar.timeframe,
                    session_open=session_interval.opens_at,
                    session_close=session_interval.closes_at,
                )
                if session_interval is not None
                else None
            )
        if bucket is not None:
            payload["expected_close"] = bucket.closes_at.isoformat()
    return payload


def stream_bar_signature(payload: dict) -> tuple:
    return (
        *bar_revision_signature(payload),
        str(payload.get("preview_kind") or ""),
        bool(is_gap_placeholder_payload(payload)),
        str(payload.get("availability_state") or ""),
        payload.get("fill_forward") is True,
        payload.get("authoritative") is not False,
        payload.get("bar_slot"),
        payload.get("bar_slot_authoritative") is True,
        str(payload.get("bar_slot_schedule_state") or ""),
        str(payload.get("expected_close") or ""),
    )


def parse_iso_ts(value: str | None) -> datetime | None:
    return parse_aware_utc_ts(value)


def parse_stream_ts(payload: dict | None) -> datetime | None:
    if not payload:
        return None
    return parse_iso_ts(payload.get("ts"))


def chart_recovery_tail(
    last_sent_ts: datetime | None, latest_ts: datetime | None, interval: str
) -> int:
    if last_sent_ts is None or latest_ts is None:
        return CHART_STREAM_RECOVERY_TAIL
    step_seconds = max(interval_minutes(interval) * 60, 60)
    gap_seconds = max((latest_ts - last_sent_ts).total_seconds(), 0.0)
    missing_bars = int(gap_seconds // step_seconds)
    return min(max(missing_bars + 4, CHART_STREAM_RECOVERY_TAIL), CHART_STREAM_MAX_RECOVERY_TAIL)


def chart_recovery_snapshot(
    bars: list[Bar],
    last_sent_ts: datetime | None,
    interval: str,
    instrument: dict[str, Any],
    required_timestamps: Collection[str | datetime] = (),
) -> ChartRecoverySnapshot:
    if not isinstance(interval, str) or not interval or interval != interval.strip():
        raise ValueError("CHART_RECOVERY_INTERVAL_INVALID")
    bar_list = require_ordered_bar_list(
        bars,
        timeframe=interval,
        field="chart recovery bars",
        require_confirmed=True,
    )
    inferred_interval = interval
    route = route_instrument(instrument)
    store = _deps().store_factory()
    snapshot_now = datetime.now(tz=UTC)
    if bar_list:
        (
            bar_list,
            bar_slots,
            _mtf_context,
            _mtf_context_slots,
            _mtf_quality,
        ) = snapshot_db_context(
            store,
            route.provider_symbol,
            inferred_interval,
            "",
            bar_list,
            provider_key=route.provider,
            instrument=route.instrument,
            include_mtf=False,
        )
    else:
        bar_slots = None
    quality = data_quality_report(
        bar_list,
        inferred_interval,
        now_utc=snapshot_now,
        store=store,
        instrument=route.instrument,
    )
    if bar_list:
        payloads = serialize_bars(
            bar_list,
            bar_slots,
            session_reset=provider_vwap_session(route.instrument, bar_list),
        )
    else:
        payloads = []
    required_slots = {
        parsed for raw in required_timestamps if (parsed := parse_aware_utc_ts(raw)) is not None
    }
    normalized_last_sent = parse_aware_utc_ts(last_sent_ts)
    if last_sent_ts is not None and normalized_last_sent is None:
        raise ValueError("CHART_RECOVERY_LAST_SENT_TS_NOT_AWARE")
    if normalized_last_sent is None:
        recovered = payloads[-CHART_STREAM_MAX_RECOVERY_TAIL:]
    else:
        latest_ts = parse_stream_ts(payloads[-1]) if payloads else None
        tail = chart_recovery_tail(normalized_last_sent, latest_ts, inferred_interval)
        recovered = [
            payload
            for payload in payloads
            if (parsed := parse_stream_ts(payload)) is not None and parsed >= normalized_last_sent
        ]
        recovered = (recovered or payloads[-tail:])[-CHART_STREAM_MAX_RECOVERY_TAIL:]
    if required_slots:
        recovered_by_timestamp = {
            parsed: payload
            for payload in recovered
            if (parsed := parse_stream_ts(payload)) is not None
        }
        for payload in payloads:
            parsed = parse_stream_ts(payload)
            if parsed in required_slots:
                recovered_by_timestamp[parsed] = payload
        recovered = [
            recovered_by_timestamp[timestamp] for timestamp in sorted(recovered_by_timestamp)
        ]
    current_bucket = provider_bar_bucket(
        snapshot_now,
        inferred_interval,
        store=store,
        instrument=route.instrument,
    )
    expected_live_slot = (
        current_bucket.starts_at.isoformat() if current_bucket is not None else None
    )
    expected_live_close = (
        current_bucket.closes_at.isoformat() if current_bucket is not None else None
    )
    return ChartRecoverySnapshot(
        payloads=tuple(recovered),
        quality=dict(quality),
        expected_live_slot=expected_live_slot,
        expected_live_close=expected_live_close,
        future_axis=chart_future_axis_payload(
            store,
            inferred_interval,
            payloads,
            provider_key=route.provider,
            instrument=route.instrument,
        ),
    )
