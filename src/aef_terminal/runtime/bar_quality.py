from __future__ import annotations

from collections.abc import Sequence

from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarProvenance,
    BarState,
)


def is_provisional_bar(bar: Bar) -> bool:
    return bar.closed is False or BarState(bar.state) is not BarState.CONFIRMED


def confirmed_storage_bars(bars: Sequence[Bar]) -> list[Bar]:
    return [bar for bar in bars if not is_provisional_bar(bar)]


def canonical_storage_bar_provenance(
    provider: str,
    instrument_id: str,
    route_fingerprint: str,
) -> BarProvenance:
    return BarProvenance(
        provider=provider,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        request_type=BarProviderRequest.CANONICAL_STORAGE,
        provider_contract_id="",
        provider_contract_type="CANONICAL_STORAGE",
        data_type="canonical_ohlcv",
    )


def authoritative_bar_reject_reason(
    provider: str,
    bar: Bar,
    *,
    instrument_id: str | None = None,
    route_fingerprint: str | None = None,
    allowed_request_types: frozenset[BarProviderRequest] | None = None,
    provider_contract_id: str | None = None,
    provider_contract_type: str | None = None,
    data_type: str | None = None,
) -> str | None:
    if is_provisional_bar(bar):
        return "provisional_bar"
    # Every persisted provider timeframe is minute-based. Provider adapters own
    # the wider interval grid (including session-opening partial bars), but a
    # sub-minute timestamp can never identify a canonical provider bar.
    if bar.ts.second != 0 or bar.ts.microsecond != 0:
        return "provider_timestamp_not_minute_aligned"
    provider_key = provider if isinstance(provider, str) else ""
    provenance = bar.provenance
    if provenance is None:
        return "provider_provenance_missing"
    if provenance.provider != provider_key:
        return "provider_provenance_mismatch"
    if instrument_id is not None and provenance.instrument_id != instrument_id:
        return "instrument_id_mismatch"
    if route_fingerprint is not None and provenance.route_fingerprint != route_fingerprint:
        return "route_fingerprint_mismatch"
    if allowed_request_types is not None and provenance.request_type not in allowed_request_types:
        return "provider_request_type_mismatch"
    if provider_contract_id is not None and provenance.provider_contract_id != provider_contract_id:
        return "provider_contract_id_mismatch"
    if (
        provider_contract_type is not None
        and provenance.provider_contract_type != provider_contract_type
    ):
        return "provider_contract_type_mismatch"
    if data_type is not None and provenance.data_type != data_type:
        return "provider_data_type_mismatch"
    if provenance.request_type is BarProviderRequest.DETERMINISTIC_AGGREGATION:
        source_timeframe = provenance.source_timeframe or ""
        target_timeframe = bar.timeframe
        if (
            not source_timeframe.endswith("m")
            or not source_timeframe[:-1].isdigit()
            or int(source_timeframe[:-1]) <= 0
            or source_timeframe != f"{int(source_timeframe[:-1])}m"
            or not target_timeframe.endswith("m")
            or not target_timeframe[:-1].isdigit()
            or int(target_timeframe[:-1]) <= 0
            or target_timeframe != f"{int(target_timeframe[:-1])}m"
        ):
            return "provider_aggregation_timeframe_mismatch"
        source_minutes = int(source_timeframe[:-1])
        target_minutes = int(target_timeframe[:-1])
        if source_minutes >= target_minutes or target_minutes % source_minutes != 0:
            return "provider_aggregation_timeframe_mismatch"
    return None
