from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from aef_terminal.domain import Bar, BarState
from aef_terminal.runtime.pine import collapse_bars, interval_bucket
from aef_terminal.runtime.timeframes import interval_minutes, parse_aware_utc_ts


class ProviderBarSlotSequence(list[int]):
    """Logical slots plus the authority of the provider calendar axis."""

    schedule_state: Literal["continuous", "verified", "unknown"]
    authoritative: bool

    def __init__(
        self,
        values: Sequence[int] = (),
        *,
        schedule_state: Literal["continuous", "verified", "unknown"],
    ) -> None:
        if schedule_state not in {"continuous", "verified", "unknown"}:
            raise ValueError("provider bar-slot schedule_state is invalid")
        normalized: list[int] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("provider bar slots must be integers")
            normalized.append(value)
        if any(right <= left for left, right in zip(normalized, normalized[1:], strict=False)):
            raise ValueError("provider bar slots must be strictly increasing")
        super().__init__(normalized)
        self.schedule_state = schedule_state
        self.authoritative = schedule_state in {"continuous", "verified"}


def mtf_context_quality(
    parent_bars: Sequence[Bar],
    context_bars: Sequence[Bar],
    timeframe: str,
    *,
    min_coverage: float = 0.82,
) -> dict[str, Any]:
    if not parent_bars or not context_bars:
        return {
            "ok": False,
            "coverage": 0.0,
            "expected_bars": 0,
            "actual_bars": len(context_bars),
            "fresh": False,
            "warning": "no security context bars",
        }
    parent_minutes = max(interval_minutes(parent_bars[-1].timeframe), 1)
    context_minutes = max(interval_minutes(timeframe), 1)
    expected_bars = max(
        int(round(len(parent_bars) * parent_minutes / context_minutes)),
        1,
    )
    first_ts = parent_bars[0].ts
    last_ts = parent_bars[-1].ts + timedelta(minutes=max(parent_minutes - context_minutes, 0))
    actual_bars = len([bar for bar in context_bars if first_ts <= bar.ts <= last_ts])
    coverage = actual_bars / expected_bars if expected_bars else 0.0
    fresh_cutoff = parent_bars[-1].ts - timedelta(minutes=context_minutes)
    fresh = context_bars[-1].ts >= fresh_cutoff
    ok = coverage >= min_coverage and fresh
    warning = ""
    if not ok:
        warning = (
            f"{timeframe} context incomplete: coverage {coverage:.0%} "
            f"({actual_bars}/{expected_bars}), fresh={fresh}"
        )
    return {
        "ok": ok,
        "coverage": round(coverage, 4),
        "expected_bars": expected_bars,
        "actual_bars": actual_bars,
        "fresh": fresh,
        "warning": warning,
    }


def confirmed_bar_context_quality(
    context_bars: Sequence[Bar],
    provider_quality: Mapping[str, Any] | None,
    timeframe: str,
    *,
    context_slots: ProviderBarSlotSequence | None = None,
    slots_required: bool = False,
) -> dict[str, Any]:
    """Describe one admitted confirmed lower-TF tail and its audit coverage."""

    quality = dict(provider_quality or {})
    provider_complete = quality.get("provider_complete") is True
    has_bars = bool(context_bars)
    slot_axis_authoritative = bool(
        context_slots is not None
        and context_slots.authoritative is True
        and len(context_slots) == len(context_bars)
    )
    slots_admissible = bool(not slots_required or slot_axis_authoritative)
    due_provisional_count = int(quality.get("due_provisional_bar_count") or 0)
    finality_admissible = due_provisional_count == 0
    input_admitted = bool(has_bars and slots_admissible and finality_admissible)
    quality.update(
        {
            "ok": input_admitted,
            "input_admitted": input_admitted,
            "actual_bars": len(context_bars),
            "fresh": input_admitted,
            "current_finality_ok": finality_admissible,
            "timeframe": timeframe,
            "slot_axis_authoritative": slot_axis_authoritative,
            "slot_count": len(context_slots) if context_slots is not None else 0,
            "slot_schedule_state": str(
                (context_slots.schedule_state if context_slots is not None else None)
                or quality.get("slot_schedule_state")
                or "unknown"
            ),
        }
    )
    if not has_bars:
        quality["warning"] = "no confirmed bar context"
    elif not slots_admissible:
        quality["warning"] = "provider bar-slot axis unavailable"
    elif not finality_admissible:
        quality["warning"] = "provider bar finality pending"
    else:
        quality["warning"] = ""
    quality["coverage_warning"] = (
        "exact history coverage incomplete" if input_admitted and not provider_complete else ""
    )
    return quality


def project_confirmed_bar_context_quality(
    context_bars: Sequence[Bar],
    provider_quality: Mapping[str, Any] | None,
    timeframe: str,
    *,
    context_slots: ProviderBarSlotSequence | None = None,
    slots_required: bool = False,
) -> dict[str, Any]:
    """Project exact provider-range audit coverage onto one confirmed-bar tail.

    A shared DB read may be widened for another consumer. Coverage diagnostics
    are recomputed for the selected tail, but admitted provider-confirmed bars
    remain usable when the durable coverage union is incomplete. Omitted
    timestamps are not reconstructed as trading slots.
    """

    quality = dict(provider_quality or {})
    bars = list(context_bars)
    if not bars:
        quality.update(
            {
                "provider_complete": False,
                "quality_projection_valid": False,
                "quality_projection_reason_code": "empty_context_tail",
            }
        )
        return confirmed_bar_context_quality(
            bars,
            quality,
            timeframe,
            context_slots=context_slots,
            slots_required=slots_required,
        )

    bar_timestamps = [bar.ts.astimezone(UTC) for bar in bars]
    window_start = bar_timestamps[0]
    upstream_window_start = parse_aware_utc_ts(quality.get("window_start_ts"))
    coverage_end = parse_aware_utc_ts(quality.get("coverage_end_exclusive"))
    snapshot_as_of = parse_aware_utc_ts(quality.get("snapshot_as_of_ts") or quality.get("as_of_ts"))

    raw_ranges = quality.get("eligible_ranges")
    ranges_valid = bool(
        isinstance(raw_ranges, Sequence) and not isinstance(raw_ranges, (str, bytes))
    )
    eligible_ranges: list[tuple[datetime, datetime]] = []
    if ranges_valid:
        for item in raw_ranges:
            if not isinstance(item, Mapping):
                ranges_valid = False
                continue
            starts_at = parse_aware_utc_ts(item.get("from"))
            ends_at = parse_aware_utc_ts(item.get("to"))
            if starts_at is None or ends_at is None or ends_at <= starts_at:
                ranges_valid = False
                continue
            eligible_ranges.append((starts_at, ends_at))
    ranges_canonical = bool(
        ranges_valid
        and eligible_ranges == sorted(eligible_ranges)
        and all(
            left[1] < right[0]
            for left, right in zip(
                eligible_ranges,
                eligible_ranges[1:],
                strict=False,
            )
        )
        and type(quality.get("eligible_range_count")) is int
        and quality.get("eligible_range_count") == len(eligible_ranges)
    )
    timestamps_valid = bool(
        len(set(bar_timestamps)) == len(bar_timestamps)
        and bar_timestamps == sorted(bar_timestamps)
        and all(bar.timeframe == timeframe for bar in bars)
        and upstream_window_start is not None
        and upstream_window_start <= window_start
        and coverage_end is not None
        and window_start < coverage_end
        and snapshot_as_of is not None
        and coverage_end <= snapshot_as_of
        and all(window_start <= timestamp < coverage_end for timestamp in bar_timestamps)
    )
    projection_valid = bool(
        quality.get("contract") == "provider-mtf-range-quality-v2"
        and quality.get("timeframe") == timeframe
        and ranges_canonical
        and timestamps_valid
    )

    scoped_ranges: list[tuple[datetime, datetime]] = []
    range_complete = False
    if projection_valid:
        scoped_ranges = [
            (max(starts_at, window_start), min(ends_at, coverage_end))
            for starts_at, ends_at in eligible_ranges
            if ends_at > window_start and starts_at < coverage_end
        ]
        cursor = window_start
        for starts_at, ends_at in scoped_ranges:
            if starts_at > cursor:
                break
            cursor = max(cursor, ends_at)
            if cursor >= coverage_end:
                range_complete = True
                break
        quality.update(
            {
                "window_start_ts": window_start.isoformat(),
                "eligible_ranges": [
                    {
                        "from": starts_at.isoformat(),
                        "to": ends_at.isoformat(),
                    }
                    for starts_at, ends_at in scoped_ranges
                ],
                "eligible_range_count": len(scoped_ranges),
                "observed_bar_count": len(bars),
            }
        )
    else:
        quality.update(
            {
                "window_start_ts": window_start.isoformat(),
                "provider_complete": False,
                "eligible_ranges": [],
                "eligible_range_count": 0,
                "observed_bar_count": len(bars),
            }
        )

    quality.update(
        {
            "provider_complete": bool(
                projection_valid and quality.get("provider_complete") is True and range_complete
            ),
            "quality_projection_valid": projection_valid,
            "quality_projection_reason_code": (
                "projected_exact_tail" if projection_valid else "provider_quality_scope_unverified"
            ),
        }
    )
    return confirmed_bar_context_quality(
        bars,
        quality,
        timeframe,
        context_slots=context_slots,
        slots_required=slots_required,
    )


def confirmed_intrabar_parent_preview(
    parent_bars: Sequence[Bar],
    context_bars: Sequence[Bar],
    provider_quality: Mapping[str, Any] | None,
    context_timeframe: str,
) -> list[Bar]:
    """Build one non-authoritative parent preview from confirmed lower-TF bars."""

    if not parent_bars or not context_bars or (provider_quality or {}).get("ok") is not True:
        return []
    parent_timeframe = parent_bars[-1].timeframe
    if interval_minutes(context_timeframe) >= interval_minutes(parent_timeframe):
        return []
    if parent_bars[-1].ts != interval_bucket(
        parent_bars[-1].ts,
        parent_timeframe,
    ):
        return []
    if any(bar.timeframe != context_timeframe or not bar.closed for bar in context_bars):
        return []
    collapsed = collapse_bars(
        context_bars,
        parent_timeframe,
        symbol=parent_bars[-1].symbol,
        source_name=f"confirmed-context:{context_timeframe}",
    )
    future_parent_bars = [bar for bar in collapsed if bar.ts > parent_bars[-1].ts]
    if len(future_parent_bars) != 1:
        return []
    preview_bar = replace(
        future_parent_bars[0],
        closed=False,
        state=BarState.FORMING,
    )
    return [*parent_bars, preview_bar]


def parent_context_cutoff(
    parent_bar: Bar,
    context_timeframe: str,
) -> datetime:
    parent_minutes = max(interval_minutes(parent_bar.timeframe), 1)
    context_minutes = max(interval_minutes(context_timeframe), 1)
    return parent_bar.ts + timedelta(minutes=max(parent_minutes - context_minutes, 0))


def trim_mtf_context_to_parent(
    parent_bars: Sequence[Bar],
    context_bars: Sequence[Bar],
    context_timeframe: str,
) -> list[Bar]:
    if not parent_bars or not context_bars:
        return []
    first_ts = parent_bars[0].ts
    cutoff_ts = parent_context_cutoff(
        parent_bars[-1],
        context_timeframe,
    )
    return [bar for bar in context_bars if first_ts <= bar.ts <= cutoff_ts]
