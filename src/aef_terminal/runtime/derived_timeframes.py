from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aef_terminal.domain import (
    Bar,
    BarProvenance,
    BarProviderRequest,
    BarState,
)
from aef_terminal.runtime.timeframes import (
    canonical_bar_source_interval,
    interval_bucket,
    interval_seconds,
    is_derived_chart_interval,
)


def _derived_provenance(source_bars: Sequence[Bar], source_interval: str) -> BarProvenance | None:
    provenances = [bar.provenance for bar in source_bars]
    if not provenances or any(item is None for item in provenances):
        return None
    first = provenances[0]
    assert first is not None
    identity = (
        first.provider,
        first.instrument_id,
        first.route_fingerprint,
        first.provider_contract_id,
        first.provider_contract_type,
        first.data_type,
    )
    if any(
        item is None
        or (
            item.provider,
            item.instrument_id,
            item.route_fingerprint,
            item.provider_contract_id,
            item.provider_contract_type,
            item.data_type,
        )
        != identity
        for item in provenances
    ):
        return None
    return BarProvenance(
        provider=first.provider,
        instrument_id=first.instrument_id,
        route_fingerprint=first.route_fingerprint,
        request_type=BarProviderRequest.DETERMINISTIC_AGGREGATION,
        provider_contract_id=(
            first.provider_contract_id if first.provider_contract_id else "CANONICAL_STORAGE"
        ),
        provider_contract_type=first.provider_contract_type,
        data_type=first.data_type,
        source_timeframe=source_interval,
    )


def derive_chart_bars(
    bars: Sequence[Bar],
    interval: str,
    *,
    as_of: datetime | None = None,
) -> list[Bar]:
    """Project provider/persisted source bars onto a virtual chart timeframe.

    No missing source timestamp is invented.  A non-empty elapsed target bucket
    is confirmed once every returned child is confirmed; the current bucket is
    a forming preview.  A later real source-bar commit simply revises the same
    derived bucket through the source generation.
    """

    if not is_derived_chart_interval(interval):
        return list(bars)
    source_interval = canonical_bar_source_interval(interval)
    latest_by_timestamp: dict[datetime, Bar] = {}
    for bar in bars:
        latest_by_timestamp[bar.ts.astimezone(UTC)] = bar
    ordered = [latest_by_timestamp[key] for key in sorted(latest_by_timestamp)]
    if any(bar.timeframe != source_interval for bar in ordered):
        raise ValueError(
            f"DERIVED_CHART_SOURCE_TIMEFRAME_MISMATCH target={interval} source={source_interval}"
        )
    current = (as_of or datetime.now(tz=UTC)).astimezone(UTC)
    grouped: dict[datetime, list[Bar]] = {}
    for bar in ordered:
        grouped.setdefault(interval_bucket(bar.ts, interval), []).append(bar)

    projected: list[Bar] = []
    target_seconds = interval_seconds(interval)
    source_seconds = interval_seconds(source_interval)
    if target_seconds % source_seconds:
        raise ValueError(
            f"DERIVED_CHART_INTERVAL_RATIO_INVALID target={interval} source={source_interval}"
        )
    expected_child_count = target_seconds // source_seconds
    target_span = timedelta(seconds=target_seconds)
    source_span = timedelta(seconds=source_seconds)
    for bucket, children in grouped.items():
        elapsed = current >= bucket + target_span
        complete = len(children) == expected_child_count and all(
            child.ts.astimezone(UTC) == bucket + source_span * index
            for index, child in enumerate(children)
        )
        confirmed = (
            elapsed
            and complete
            and all(
                child.closed and BarState(child.state) is BarState.CONFIRMED for child in children
            )
        )
        if elapsed and not confirmed:
            continue
        projected.append(
            Bar(
                symbol=children[-1].symbol,
                ts=bucket,
                open=children[0].open,
                high=max(child.high for child in children),
                low=min(child.low for child in children),
                close=children[-1].close,
                volume=sum(max(child.volume, 0.0) for child in children),
                timeframe=interval,
                source=f"derived:{source_interval}",
                closed=confirmed,
                state=(BarState.CONFIRMED if confirmed else BarState.FORMING),
                provenance=_derived_provenance(children, source_interval),
            )
        )
    return projected
