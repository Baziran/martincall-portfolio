from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from aef_terminal.data.instrument_identity import futures_root, qualified_instrument_id
from aef_terminal.data.providers import default_data_source, route_instrument
from aef_terminal.domain import Bar
from aef_terminal.engine.analysis_db import (
    finer_timeframes,
    provider_backed_bar_slots,
    read_provider_mtf_context,
)
from aef_terminal.engine.analyze.constants import _LOGGER
from aef_terminal.engine.common import interval_minutes
from aef_terminal.data.provider_sessions import (
    ProviderBarSlotMap,
    continuous_provider_bar_slot,
    provider_session_future_bar_slot_coverage,
)
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.timeframes import history_range_window, range_start_utc
from aef_terminal.runtime.telemetry import log_structured_error

CHART_FUTURE_AXIS_HORIZON_MINUTES = 7 * 24 * 60
CHART_FUTURE_AXIS_MAX_SLOTS = 720


def snapshot_futures_roll_events(
    store: Any | None,
    *,
    instrument: dict[str, Any],
    bars: Sequence[Bar],
    provider_key: str,
) -> list[dict[str, Any]]:
    """Read the exact durable roll events for one rendered futures window."""

    bar_rows = tuple(bars)
    if store is None or not bar_rows or not futures_root(instrument):
        return []
    rows = store.read_futures_roll_events(
        provider=provider_key,
        instrument_id=qualified_instrument_id(instrument),
        start=min(bar.ts for bar in bar_rows),
        end=max(bar.ts for bar in bar_rows),
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TypeError("futures roll event storage must return a list of mappings")
    return rows


def chart_future_axis_payload(
    store: Any | None,
    interval: str,
    serialized_bars: Sequence[dict[str, Any]],
    *,
    provider_key: str,
    instrument: dict[str, Any],
) -> dict[str, Any]:
    """Serialize exact future chart coordinates without creating OHLCV bars."""

    step = max(interval_minutes(interval), 1)
    requested_slots = min(
        CHART_FUTURE_AXIS_MAX_SLOTS,
        max(ceil(CHART_FUTURE_AXIS_HORIZON_MINUTES / step), 1),
    )
    empty = {
        "kind": "provider_session_future_axis",
        "timeframe": interval,
        "schedule_revision": 0,
        "schedule_state": "unknown",
        "anchor_ts": None,
        "requested_slots": requested_slots,
        "complete": False,
        "slots": [],
    }
    if not serialized_bars:
        return empty
    route = route_instrument(instrument, expected_source=provider_key)
    anchor = next(
        (
            bar
            for bar in reversed(serialized_bars)
            if bar.get("closed") is True
            and str(bar.get("state") or "") == "confirmed"
            and bar.get("authoritative") is not False
            and not bar.get("missing")
            and not bar.get("data_gap")
            and str(bar.get("preview_kind") or "") != "gap_placeholder"
        ),
        None,
    )
    if anchor is None:
        return empty
    anchor_ts_raw = anchor.get("ts")
    try:
        anchor_ts = datetime.fromisoformat(str(anchor_ts_raw).replace("Z", "+00:00"))
    except TypeError, ValueError:
        return empty
    if anchor_ts.tzinfo is None or anchor_ts.utcoffset() is None:
        return empty
    anchor_ts = anchor_ts.astimezone(UTC)
    try:
        coverage = provider_session_future_bar_slot_coverage(
            anchor_ts,
            interval,
            count=requested_slots,
            through=anchor_ts + timedelta(minutes=CHART_FUTURE_AXIS_HORIZON_MINUTES),
            store=store,
            instrument=route.instrument,
        )
    except Exception as exc:
        log_structured_error(
            _LOGGER,
            provider=route.provider,
            symbol=route.provider_symbol,
            interval=interval,
            range_="future_axis",
            op="chart_future_axis",
            error=exc,
            level="debug",
            event="chart_future_axis_unavailable",
            instrument_id=route.instrument_id,
        )
        return {
            **empty,
            "anchor_ts": anchor_ts.isoformat(),
        }
    covered = {
        **empty,
        "schedule_revision": coverage.schedule_revision,
        "anchor_ts": anchor_ts.isoformat(),
    }
    if coverage.schedule_state not in {"continuous", "verified"}:
        return {
            **covered,
        }
    slots = [
        {
            "ts": timestamp.isoformat(),
            "bar_offset": index + 1,
        }
        for index, timestamp in enumerate(coverage.expected_slots)
    ]
    return {
        **covered,
        "schedule_state": coverage.schedule_state,
        "complete": coverage.complete,
        "slots": slots,
    }


def provider_chart_axis_points(
    store: Any | None,
    instrument: dict[str, Any],
    interval: str,
    timestamps: Sequence[datetime],
) -> ProviderBarSlotMap:
    """Resolve exact chart points against the canonical provider-session axis."""

    raw_timestamps = tuple(timestamps)
    if any(
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
        for timestamp in raw_timestamps
    ):
        raise ValueError("chart axis point timestamps must be timezone-aware")
    normalized = tuple(sorted({timestamp.astimezone(UTC) for timestamp in raw_timestamps}))
    if not normalized:
        return ProviderBarSlotMap(schedule_state="unknown")
    route = route_instrument(instrument)
    step = max(interval_minutes(interval), 1)
    if route.adapter.continuous_session(route.instrument):
        return ProviderBarSlotMap(
            {
                timestamp: continuous_provider_bar_slot(timestamp, interval)
                for timestamp in normalized
            },
            schedule_state="continuous",
        )
    if store is None:
        return ProviderBarSlotMap(schedule_state="unknown")
    slot_map = store.read_bar_slots(
        interval,
        route.provider,
        normalized[0],
        normalized[-1],
        step,
        instrument=route.instrument,
        required_timestamps=normalized,
        required_timestamps_only=True,
    )
    if not isinstance(slot_map, dict) or getattr(slot_map, "authoritative", False) is not True:
        return ProviderBarSlotMap(schedule_state="unknown")
    return ProviderBarSlotMap(
        {timestamp: int(slot_map[timestamp]) for timestamp in normalized if timestamp in slot_map},
        schedule_state="verified",
    )


def provider_chart_axis_slots(
    store: Any | None,
    instrument: dict[str, Any],
    interval: str,
    bars: Sequence[Bar],
) -> list[int] | None:
    """Return the exact authoritative provider slot for every supplied bar."""

    bar_rows = tuple(bars)
    slot_map = provider_chart_axis_points(
        store,
        instrument,
        interval,
        [bar.ts for bar in bar_rows],
    )
    if getattr(slot_map, "authoritative", False) is not True or len(slot_map) != len(bar_rows):
        return None
    return [slot_map[bar.ts.astimezone(UTC)] for bar in bar_rows]


def require_provider_chart_axis_point(
    store: Any | None,
    instrument: dict[str, Any],
    interval: str,
    timestamp: datetime,
) -> int:
    """Resolve one chart point on the canonical provider-session axis."""

    slot_map = provider_chart_axis_points(
        store,
        instrument,
        interval,
        (timestamp,),
    )
    actual_slot = slot_map.get(timestamp.astimezone(UTC))
    if getattr(slot_map, "authoritative", False) is not True or actual_slot is None:
        raise ValueError("target timestamp is outside the verified provider chart axis")
    return int(actual_slot)


def snapshot_db_context(
    store: Any | None,
    symbol: str,
    interval: str,
    range_: str,
    bars: Sequence[Bar],
    provider_key: str = default_data_source(),
    instrument: dict[str, Any] | None = None,
    *,
    include_mtf: bool = True,
    include_mtf_quality: bool = False,
    mtf_timeframes: Sequence[str] | None = None,
    mtf_history_bars: Mapping[str, int] | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> tuple[
    list[Bar],
    ProviderBarSlotSequence | None,
    dict[str, list[Bar]],
    dict[str, ProviderBarSlotSequence],
    dict[str, dict[str, Any]],
]:
    route = route_instrument(instrument, expected_source=provider_key)
    if analysis_as_of_utc is not None and (
        not isinstance(analysis_as_of_utc, datetime)
        or analysis_as_of_utc.tzinfo is None
        or analysis_as_of_utc.utcoffset() is None
    ):
        raise ValueError("analysis_as_of_utc must be timezone-aware")
    resolved_analysis_as_of = (
        analysis_as_of_utc.astimezone(UTC) if analysis_as_of_utc is not None else None
    )
    db_providers = route.adapter.db_providers
    resolved_mtf_timeframes = (
        tuple(dict.fromkeys(str(item) for item in mtf_timeframes if str(item)))
        if mtf_timeframes is not None
        else tuple(finer_timeframes(interval))
    )
    mtf_context_ends = {
        timeframe: history_range_window(
            range_,
            timeframe,
            now=resolved_analysis_as_of,
        ).ends_at
        for timeframe in resolved_mtf_timeframes
    }
    resolved_mtf_history_bars = {
        str(timeframe): max(int(count), 1)
        for timeframe, count in (mtf_history_bars or {}).items()
        if str(timeframe) and not isinstance(count, bool) and isinstance(count, int) and count > 0
    }
    if store is None or not bars:
        return (
            list(bars),
            provider_backed_bar_slots(store, bars, instrument=route.instrument),
            (
                {
                    timeframe: (
                        context_bars[
                            -resolved_mtf_history_bars.get(
                                timeframe,
                                len(context_bars),
                            ) :
                        ]
                    )
                    for timeframe, context_bars in read_provider_mtf_context(
                        store,
                        route.instrument,
                        interval,
                        range_,
                        timeframes=resolved_mtf_timeframes,
                    ).items()
                }
                if include_mtf
                else {}
            ),
            {},
            {},
        )
    try:
        payload = store.get_full_snapshot_context(
            interval=interval,
            bars=bars,
            providers=db_providers,
            mtf_timeframes=(resolved_mtf_timeframes if include_mtf else ()),
            mtf_context_ends=(mtf_context_ends if include_mtf else {}),
            mtf_history_bars=(resolved_mtf_history_bars if include_mtf else {}),
            history_start=(
                range_start_utc(range_, now=resolved_analysis_as_of) if include_mtf else None
            ),
            instrument=route.instrument,
            include_mtf_quality=include_mtf_quality,
            analysis_as_of_utc=resolved_analysis_as_of,
        )
        if not isinstance(payload, dict):
            raise TypeError("get_full_snapshot_context must return a mapping")
        snapshot_bars = payload["bars"]
        bar_slots = payload["bar_slots"]
        mtf_context = payload["mtf_context"]
        mtf_context_slots = payload["mtf_context_slots"]
        mtf_context_quality = payload["mtf_context_quality"]
        if not isinstance(snapshot_bars, list) or any(
            not isinstance(bar, Bar) for bar in snapshot_bars
        ):
            raise TypeError("get_full_snapshot_context bars must be a Bar list")
        if bar_slots is not None:
            if not isinstance(bar_slots, ProviderBarSlotSequence):
                raise TypeError(
                    "get_full_snapshot_context bar_slots must be ProviderBarSlotSequence or None"
                )
            if bar_slots.authoritative is not True:
                raise TypeError("get_full_snapshot_context bar_slots must be authoritative")
            if len(bar_slots) != len(snapshot_bars):
                raise TypeError("get_full_snapshot_context bar_slots must align with bars")
        if not isinstance(mtf_context, dict):
            raise TypeError("get_full_snapshot_context mtf_context must be a mapping")
        if not isinstance(mtf_context_slots, dict):
            raise TypeError("get_full_snapshot_context mtf_context_slots must be a mapping")
        if not isinstance(mtf_context_quality, dict):
            raise TypeError("get_full_snapshot_context mtf_context_quality must be a mapping")
        resolved_mtf_context: dict[str, list[Bar]] = {}
        for timeframe, context_bars in mtf_context.items():
            if not isinstance(context_bars, list) or any(
                not isinstance(bar, Bar) for bar in context_bars
            ):
                raise TypeError("get_full_snapshot_context mtf_context values must be Bar lists")
            resolved_mtf_context[str(timeframe)] = list(context_bars)
        resolved_mtf_context_slots: dict[str, ProviderBarSlotSequence] = {}
        for timeframe, context_slots in mtf_context_slots.items():
            timeframe_key = str(timeframe)
            if not isinstance(context_slots, ProviderBarSlotSequence):
                raise TypeError(
                    "get_full_snapshot_context mtf_context_slots values must be "
                    "ProviderBarSlotSequence"
                )
            if context_slots.authoritative is not True:
                raise TypeError(
                    "get_full_snapshot_context mtf_context_slots values must be authoritative"
                )
            context_bars = resolved_mtf_context.get(timeframe_key)
            if context_bars is None or len(context_slots) != len(context_bars):
                raise TypeError(
                    "get_full_snapshot_context mtf_context_slots must align with mtf_context"
                )
            resolved_mtf_context_slots[timeframe_key] = context_slots
        if any(
            not isinstance(timeframe, str) or not isinstance(item, dict)
            for timeframe, item in mtf_context_quality.items()
        ):
            raise TypeError(
                "get_full_snapshot_context mtf_context_quality must map strings to mappings"
            )
        resolved_mtf_quality = {
            timeframe: dict(item) for timeframe, item in mtf_context_quality.items()
        }
        if include_mtf_quality:
            for timeframe, context_bars in resolved_mtf_context.items():
                context_slots = resolved_mtf_context_slots.get(timeframe)
                slot_axis_authoritative = bool(
                    context_slots is not None
                    and context_slots.authoritative
                    and len(context_slots) == len(context_bars)
                )
                quality_item = resolved_mtf_quality.setdefault(timeframe, {})
                quality_item.update(
                    {
                        "slot_axis_authoritative": slot_axis_authoritative,
                        "slot_schedule_state": (
                            context_slots.schedule_state
                            if context_slots is not None
                            else str(quality_item.get("slot_schedule_state") or "unknown")
                        ),
                        "slot_count": (len(context_slots) if context_slots is not None else 0),
                    }
                )
        return (
            list(snapshot_bars),
            bar_slots,
            resolved_mtf_context,
            resolved_mtf_context_slots,
            resolved_mtf_quality,
        )
    except Exception as exc:
        log_structured_error(
            _LOGGER,
            provider="db",
            symbol=symbol,
            interval=interval,
            range_=range_,
            op="snapshot_db_context",
            error=exc,
            level="debug",
            event="snapshot_db_context_contract_error",
        )
        raise
