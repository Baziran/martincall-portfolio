from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from aef_terminal.data.provider_contract import (
    HistoryRepairIntent,
    HistoryRepairPriority,
    HistoryRequestAdmissionIdentity,
)
from aef_terminal.domain import Bar
from aef_terminal.runtime.timeframes import (
    interval_seconds,
    require_aware_utc_datetime,
)


@dataclass(frozen=True, slots=True)
class ConfirmedHistoryGap:
    """One concrete absent interval between requested confirmed-bar boundaries."""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        starts_at = require_aware_utc_datetime(self.starts_at, field="starts_at")
        ends_at = require_aware_utc_datetime(self.ends_at, field="ends_at")
        if ends_at <= starts_at:
            raise ValueError("confirmed history gap must be non-empty")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)


def confirmed_history_gap_ranges(
    bars: Sequence[Bar],
    timeframe: str,
    *,
    requested_from: datetime,
    requested_to: datetime,
    include_prefix: bool = True,
    include_tail: bool = True,
) -> tuple[ConfirmedHistoryGap, ...]:
    """Return timestamp discontinuity envelopes in a bounded confirmed series.

    This low-level function deliberately does not infer trading sessions and therefore
    does not by itself establish that an internal discontinuity is a market-data gap.
    The caller must qualify internal envelopes with a continuous-session contract or
    verified provider schedule coverage. Prefix/tail envelopes are opt-in demands.
    """

    starts_at = require_aware_utc_datetime(requested_from, field="requested_from")
    ends_at = require_aware_utc_datetime(requested_to, field="requested_to")
    if ends_at <= starts_at:
        raise ValueError("requested history range must be non-empty")
    step_seconds = interval_seconds(timeframe)
    if (
        starts_at.microsecond
        or ends_at.microsecond
        or int(starts_at.timestamp()) % step_seconds
        or int(ends_at.timestamp()) % step_seconds
    ):
        raise ValueError("requested history range must be timeframe-aligned")
    step = timedelta(seconds=step_seconds)
    timestamps: set[datetime] = set()
    for bar in bars:
        if not isinstance(bar, Bar):
            raise TypeError("confirmed history gap input must contain Bar values")
        timestamp = require_aware_utc_datetime(bar.ts, field="bar.ts")
        if (
            timestamp.microsecond
            or int(timestamp.timestamp()) % step_seconds
            or not starts_at <= timestamp < ends_at
        ):
            continue
        if bar.closed is False:
            continue
        timestamps.add(timestamp)

    ordered = sorted(timestamps)
    if not ordered:
        return (ConfirmedHistoryGap(starts_at, ends_at),) if include_prefix or include_tail else ()
    gaps: list[ConfirmedHistoryGap] = []
    cursor = ordered[0] + step
    if include_prefix and ordered[0] > starts_at:
        gaps.append(ConfirmedHistoryGap(starts_at, ordered[0]))
    for timestamp in ordered[1:]:
        if timestamp > cursor:
            gaps.append(ConfirmedHistoryGap(cursor, timestamp))
        cursor = max(cursor, timestamp + step)
    if include_tail and cursor < ends_at:
        gaps.append(ConfirmedHistoryGap(cursor, ends_at))
    return tuple(gaps)


def select_history_repair_intent(
    *,
    provider: str,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
    requested_from: datetime,
    requested_to: datetime,
    request_identity: HistoryRequestAdmissionIdentity,
    canonical_generation: int,
    priority: HistoryRepairPriority,
    cooldown_seconds: float,
    max_request_span: timedelta,
) -> HistoryRepairIntent | None:
    """Select the newest provider-bounded chunk of one concrete repair envelope."""

    starts_at = require_aware_utc_datetime(requested_from, field="requested_from")
    ends_at = require_aware_utc_datetime(requested_to, field="requested_to")
    if ends_at <= starts_at:
        raise ValueError("requested history range must be non-empty")
    if (
        not isinstance(max_request_span, timedelta)
        or max_request_span <= timedelta(0)
        or max_request_span.microseconds
    ):
        raise ValueError("max_request_span must be a canonical positive timedelta")
    step_seconds = interval_seconds(timeframe)
    if (
        starts_at.microsecond
        or ends_at.microsecond
        or int(starts_at.timestamp()) % step_seconds
        or int(ends_at.timestamp()) % step_seconds
    ):
        raise ValueError("requested history range must be timeframe-aligned")
    if ends_at > datetime.now(tz=UTC):
        raise ValueError("requested history range must be fully elapsed")
    max_slots = int(max_request_span.total_seconds()) // step_seconds
    if max_slots < 1:
        raise ValueError("max_request_span must contain one complete timeframe")
    bounded_from = max(
        starts_at,
        ends_at - timedelta(seconds=max_slots * step_seconds),
    )
    return HistoryRepairIntent(
        provider=provider,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
        starts_at=bounded_from,
        ends_at=ends_at,
        request_identity=request_identity,
        canonical_generation=canonical_generation,
        priority=priority,
        cooldown_seconds=cooldown_seconds,
        target_starts_at=starts_at,
        target_ends_at=ends_at,
    )
