from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.instrument_identity import (
    instrument_key,
    instrument_is_futures,
    qualified_instrument_id,
    require_exact_identity_text,
    route_fingerprint,
)
from aef_terminal.data.providers import (
    provider_data_policy_for_source,
    provider_key_for_source,
    route_instrument,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryRoute,
    CanonicalHistoryStorageKind,
    HistoryContractResolution,
    HistoryRequestUnsupportedError,
    canonical_bar_commit_receipt,
)
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance
from aef_terminal.data.provider_sessions import (
    ProviderBarSlotMap,
    continuous_provider_bar_slot,
    provider_history_session_scope,
    provider_session_bar_slot_coverage_between,
)
from aef_terminal.runtime.bar_quality import (
    authoritative_bar_reject_reason,
    canonical_storage_bar_provenance,
    is_provisional_bar,
)
from aef_terminal.runtime.bar_series import require_ordered_bar_list
from aef_terminal.runtime.derived_timeframes import derive_chart_bars
from aef_terminal.runtime.metrics import increment_metric, observe_metric
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.timeframes import (
    canonical_bar_source_interval,
    interval_bucket,
    interval_minutes,
    require_persisted_bar_timeframe,
)
from aef_terminal.runtime.timeframes import (
    MAX_CHART_HISTORY_CALENDAR_SLOTS,
)
from aef_terminal.storage.db_utils import ensure_utc
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection
from aef_terminal.storage.bar_projection import (
    read_confirmed_bar_timestamps,
    read_confirmed_bar_logical_projection,
)
from aef_terminal.data.history_coverage import (
    HistoryCoverageReceipt,
    current_history_coverage_contract,
    history_coverage_union,
    history_range_is_covered,
)

_LOGGER = logging.getLogger(__name__)
_SCHEDULED_BAR_SLOT_WINDOW_LIMIT = 60_000
_STORAGE_BAR_IDENTITY_REJECT_REASONS = frozenset(
    {
        "provider_provenance_missing",
        "provider_provenance_mismatch",
        "instrument_id_mismatch",
        "route_fingerprint_mismatch",
        "provider_request_type_mismatch",
        "provider_contract_id_mismatch",
        "provider_contract_type_mismatch",
        "provider_data_type_mismatch",
        "provider_aggregation_timeframe_mismatch",
    }
)
_STORAGE_PROVIDER_REQUEST_TYPES = frozenset(
    {
        BarProviderRequest.HISTORICAL,
        BarProviderRequest.KEEP_UP_TO_DATE,
        BarProviderRequest.DETERMINISTIC_AGGREGATION,
    }
)


def _storage_bar_reject_reason(
    provider: str,
    bar: Bar,
    *,
    instrument_id: str | None = None,
    expected_route_fingerprint: str | None = None,
    expected_provider_contract_id: str | None = None,
    expected_provider_contract_type: str | None = None,
    expected_data_type: str | None = None,
    strict_identity: bool = False,
) -> str | None:
    provider_key = provider_key_for_source(provider)
    if not provider_data_policy_for_source(provider).authoritative_ohlcv:
        return "provider_not_authoritative"
    reason = authoritative_bar_reject_reason(
        provider_key,
        bar,
        instrument_id=instrument_id,
        route_fingerprint=expected_route_fingerprint,
        allowed_request_types=_STORAGE_PROVIDER_REQUEST_TYPES,
        provider_contract_id=expected_provider_contract_id,
        provider_contract_type=expected_provider_contract_type,
        data_type=expected_data_type,
    )
    if strict_identity and reason in _STORAGE_BAR_IDENTITY_REJECT_REASONS:
        raise ValueError(
            "CANONICAL_BAR_IDENTITY_REJECTED "
            f"provider={provider_key} "
            f"instrument_id={instrument_id or ''} "
            f"route_fingerprint={expected_route_fingerprint or ''} "
            f"reason={reason}"
        )
    return reason


def _storage_bar_allowed(
    provider: str,
    bar: Bar,
    *,
    instrument_id: str | None = None,
    expected_route_fingerprint: str | None = None,
    expected_provider_contract_id: str | None = None,
    expected_provider_contract_type: str | None = None,
    expected_data_type: str | None = None,
) -> bool:
    reason = _storage_bar_reject_reason(
        provider,
        bar,
        instrument_id=instrument_id,
        expected_route_fingerprint=expected_route_fingerprint,
        expected_provider_contract_id=expected_provider_contract_id,
        expected_provider_contract_type=expected_provider_contract_type,
        expected_data_type=expected_data_type,
        strict_identity=True,
    )
    return reason is None


def _storage_route(
    providers: Sequence[str],
    instrument: dict[str, Any],
) -> tuple[Any | None, dict[str, Any], str]:
    if not providers:
        route = route_instrument(instrument)
        return None, route.instrument, route.instrument_id
    provider_keys = {
        provider_key_for_source(str(provider))
        for provider in providers
        if str(provider or "").strip()
    }
    if len(provider_keys) != 1:
        raise ValueError(
            f"STORAGE_PROVIDER_ROUTE_AMBIGUOUS providers={','.join(sorted(provider_keys))}"
        )
    provider = next(iter(provider_keys))
    route = route_instrument(instrument, expected_source=provider)
    history_route = route.adapter.canonical_history_route(route.instrument)
    storage_route = (route.provider, history_route) if history_route is not None else None
    return storage_route, route.instrument, route.instrument_id


def _futures_history_reader(
    store: Any,
    history_route: CanonicalHistoryRoute,
    *,
    multi: bool,
) -> Any:
    if history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS:
        return (
            store.read_futures_canonical_bars_multi if multi else store.read_futures_canonical_bars
        )
    return store.read_futures_contract_bars_multi if multi else store.read_futures_contract_bars


def _futures_history_route_kwargs(
    history_route: CanonicalHistoryRoute,
) -> dict[str, str]:
    if history_route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS:
        return {
            "series_type": history_route.series_type,
            "roll_policy": history_route.roll_policy,
        }
    return {"contract_key": history_route.contract_key}


def _raise_if_futures_instrument(instrument: dict[str, Any]) -> None:
    if instrument_is_futures(instrument):
        raise ValueError(
            "Generic bars storage cannot read/write futures; use futures contract/canonical storage for "
            + qualified_instrument_id(instrument)
        )


def _ordinary_bars_storage_route(
    provider: str,
    instrument: dict[str, Any],
    *,
    symbol: str,
) -> tuple[str, dict[str, Any], str]:
    provider_key = provider_key_for_source(provider)
    _futures_provider, resolved_instrument, instrument_id = _storage_route(
        (provider_key,),
        instrument,
    )
    if instrument_is_futures(resolved_instrument):
        raise ValueError(
            "Generic bars storage cannot read/write futures; use futures contract/canonical storage for "
            + symbol
        )
    return provider_key, resolved_instrument, instrument_id


def _cache_bar_slot_result(
    cache: dict[tuple[Any, ...], tuple[float, ProviderBarSlotMap]],
    key: tuple[Any, ...],
    now: float,
    result: ProviderBarSlotMap,
    *,
    max_items: int = 128,
    evict_count: int = 32,
) -> None:
    cache[key] = (
        now,
        ProviderBarSlotMap(result, schedule_state=result.schedule_state),
    )
    if len(cache) > max_items:
        oldest = sorted(cache.items(), key=lambda item: item[1][0])[:evict_count]
        for stale_key, _value in oldest:
            cache.pop(stale_key, None)


def _provider_schedule_bar_slot_map(
    store: Any,
    timestamps: Sequence[datetime],
    *,
    timeframe: str,
    step: int,
    instrument: dict[str, Any],
    required_timestamps: Sequence[datetime] = (),
) -> ProviderBarSlotMap:
    """Map bars onto one route-qualified compressed provider-session axis.

    The persistent axis starts at the earliest retained provider session row for
    the exact route.  Expected-but-unconfirmed slots occupy coordinates.  If
    the complete anchor-to-tail schedule cannot be proven, the returned mapping
    is explicitly visual-only and uses a range-local compressed axis for
    canonical bars only.  Exact requested timestamps join the authoritative map
    only when provider-session coverage proves that each one is an expected
    slot; they are never projected from neighbouring bars.
    """

    canonical = tuple(sorted({ensure_utc(ts) for ts in timestamps}))
    required = tuple(sorted({ensure_utc(ts) for ts in required_timestamps}))
    exact_timestamps = tuple(sorted({*canonical, *required}))
    if not exact_timestamps:
        return ProviderBarSlotMap(schedule_state="unknown")
    step_value = max(int(step), 1)
    visual_slots = ProviderBarSlotMap(
        {ts: int(index * step_value) for index, ts in enumerate(canonical)},
        schedule_state="unknown",
    )
    route = route_instrument(instrument)
    session_scope = provider_history_session_scope(route.adapter, route.instrument)
    anchor_reader = store.read_trading_session_axis_anchor
    try:
        axis_anchor = anchor_reader(
            instrument=route.instrument,
            session_type=session_scope,
        )
        if not isinstance(axis_anchor, datetime):
            return visual_slots
        axis_anchor = ensure_utc(axis_anchor)
        if exact_timestamps[0] < axis_anchor:
            return visual_slots
        coverage = provider_session_bar_slot_coverage_between(
            axis_anchor,
            exact_timestamps[-1] + timedelta(minutes=step_value),
            timeframe,
            store=store,
            instrument=route.instrument,
            limit=_SCHEDULED_BAR_SLOT_WINDOW_LIMIT,
        )
    except Exception:
        _LOGGER.exception(
            "provider bar-slot schedule lookup failed instrument_id=%s timeframe=%s",
            instrument.get("instrument_id"),
            timeframe,
        )
        return visual_slots
    if coverage.schedule_state != "verified" or coverage.truncated:
        return visual_slots
    display_axis = coverage.expected_slots
    expected = set(display_axis)
    if any(ts not in expected for ts in canonical):
        _LOGGER.warning(
            "canonical bar timestamp is outside verified provider slot grid instrument_id=%s timeframe=%s",
            route.instrument_id,
            timeframe,
        )
        return visual_slots
    exact_required = tuple(ts for ts in required if ts in expected)
    slot_by_ts = {ts: int(index * step_value) for index, ts in enumerate(display_axis)}
    return ProviderBarSlotMap(
        {ts: slot_by_ts[ts] for ts in sorted({*canonical, *exact_required})},
        schedule_state="verified",
    )


def _provider_mtf_context_quality(
    store: Any,
    parent_bars: Sequence[Bar],
    mtf_context: dict[str, list[Bar]],
    *,
    instrument: dict[str, Any],
    timeframes: Sequence[str],
    context_ends: Mapping[str, datetime] | None = None,
    history_bars: Mapping[str, int] | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Build exact route-qualified history coverage for lower-TF context."""

    parent_confirmed = [bar for bar in parent_bars if not is_provisional_bar(bar)]
    if not parent_confirmed:
        return {}
    route = route_instrument(instrument)
    parent_minutes = max(
        interval_minutes(parent_confirmed[-1].timeframe),
        1,
    )
    quality: dict[str, dict[str, Any]] = {}
    for timeframe in timeframes:
        context_minutes = max(interval_minutes(timeframe), 1)
        if context_minutes >= parent_minutes or parent_minutes % context_minutes != 0:
            continue
        parent_limit = max(
            (MAX_CHART_HISTORY_CALENDAR_SLOTS * context_minutes // parent_minutes),
            1,
        )
        scoped_parent = parent_confirmed[-parent_limit:]
        context_history_limit = max(
            int((history_bars or {}).get(timeframe) or 0),
            0,
        )
        context_tail = mtf_context.get(timeframe, ())
        if context_history_limit:
            context_tail = context_tail[-context_history_limit:]
        window_start = max(
            ensure_utc(scoped_parent[0].ts),
            (ensure_utc(context_tail[0].ts) if context_tail else ensure_utc(scoped_parent[0].ts)),
        )
        coverage_end = ensure_utc(
            (context_ends or {}).get(timeframe)
            or (ensure_utc(scoped_parent[-1].ts) + timedelta(minutes=parent_minutes))
        )
        snapshot_as_of = ensure_utc(analysis_as_of_utc or coverage_end)
        observed_bar_count = sum(
            1
            for bar in mtf_context.get(timeframe, ())
            if (not is_provisional_bar(bar) and window_start <= ensure_utc(bar.ts) < coverage_end)
        )
        base = {
            "contract": "provider-mtf-range-quality-v2",
            "provider": route.provider,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "timeframe": timeframe,
            "provider_complete": False,
            "window_start_ts": window_start.isoformat(),
            "coverage_end_exclusive": coverage_end.isoformat(),
            "snapshot_as_of_ts": snapshot_as_of.isoformat(),
            "as_of_ts": snapshot_as_of.isoformat(),
            "eligible_ranges": [],
            "eligible_range_count": 0,
            "observed_bar_count": observed_bar_count,
        }
        if coverage_end <= window_start or coverage_end > snapshot_as_of:
            quality[timeframe] = base
            continue
        if not route.adapter.capabilities.gap_repair:
            quality[timeframe] = base
            continue
        try:
            identity = route.adapter.history_request_identity(route.instrument)
            if identity.contract_resolution is not HistoryContractResolution.EXACT:
                quality[timeframe] = {
                    **base,
                    "coverage_state": "unsupported",
                    "coverage_error_code": ("PROVIDER_RESPONSE_HISTORY_EXACT_COVERAGE_UNSUPPORTED"),
                }
                continue
            coverage_contract = current_history_coverage_contract(
                provider=route.provider,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                timeframe=timeframe,
                request_mode=identity.request_mode,
                request_type=identity.request_type.value,
                provider_source=identity.provider_source,
                provider_contract_id=identity.provider_contract_id,
                provider_contract_type=identity.provider_contract_type,
                data_type=identity.data_type,
            )
            receipts = tuple(
                store.read_history_coverage(
                    coverage_contract,
                    starts_at=window_start,
                    ends_at=coverage_end,
                )
            )
            if any(
                not isinstance(receipt, HistoryCoverageReceipt)
                or receipt.contract != coverage_contract
                for receipt in receipts
            ):
                raise TypeError("HISTORY_COVERAGE_RECEIPT_CONTRACT_MISMATCH")
        except HistoryRequestUnsupportedError as exc:
            quality[timeframe] = {
                **base,
                "coverage_state": "unsupported",
                "coverage_error_code": exc.code,
            }
            continue
        except Exception:
            _LOGGER.exception(
                "provider MTF history coverage unavailable "
                "provider=%s instrument_id=%s timeframe=%s",
                route.provider,
                route.instrument_id,
                timeframe,
            )
            quality[timeframe] = base
            continue
        eligible_ranges = tuple(
            (
                max(covered_from, window_start),
                min(covered_to, coverage_end),
            )
            for covered_from, covered_to in history_coverage_union(receipts)
            if covered_to > window_start and covered_from < coverage_end
        )
        quality[timeframe] = {
            **base,
            "provider_complete": history_range_is_covered(
                receipts,
                starts_at=window_start,
                ends_at=coverage_end,
            ),
            "eligible_ranges": [
                {
                    "from": covered_from.isoformat(),
                    "to": covered_to.isoformat(),
                }
                for covered_from, covered_to in eligible_ranges
            ],
            "eligible_range_count": len(eligible_ranges),
        }
    return quality


class BarsRepoMixin:
    """PostgreSQL repository mixin."""

    def _write_bars_on_cursor(
        self,
        cur: Any,
        bars: Sequence[Bar],
        provider: str,
        *,
        instrument: dict[str, Any],
        revision_sequence: int,
    ) -> CanonicalBarCommitReceipt:
        if (
            isinstance(revision_sequence, bool)
            or not isinstance(revision_sequence, int)
            or revision_sequence <= 0
        ):
            raise ValueError("revision_sequence must be positive")
        commit_sequence = revision_sequence
        if not bars:
            return CanonicalBarCommitReceipt(revision_sequence=commit_sequence)
        for bar in bars:
            require_persisted_bar_timeframe(bar.timeframe)
        symbols = {str(bar.symbol or "") for bar in bars}
        if len(symbols) != 1:
            raise ValueError("write_bars requires one provider-qualified instrument per call")
        symbol = next(iter(symbols))
        provider_key, resolved_instrument, instrument_id = _ordinary_bars_storage_route(
            provider,
            instrument,
            symbol=symbol,
        )
        started_at = time.perf_counter()
        exact_route_fingerprint = route_fingerprint(resolved_instrument)
        route = route_instrument(
            resolved_instrument,
            expected_source=provider_key,
        )
        expected_provider_contract_id = route.adapter.session_contract_id(route.instrument)
        expected_data_type = route.adapter.bar_data_type(route.instrument)
        increment_metric("storage_bars_attempted_total", len(bars), provider=provider_key)
        allowed_bars: list[Bar] = []
        rejected_reasons: Counter[str] = Counter()
        for bar in bars:
            reason = _storage_bar_reject_reason(
                provider_key,
                bar,
                instrument_id=instrument_id,
                expected_route_fingerprint=exact_route_fingerprint,
                expected_provider_contract_id=expected_provider_contract_id,
                expected_data_type=expected_data_type,
                strict_identity=True,
            )
            if reason is None:
                allowed_bars.append(bar)
            else:
                rejected_reasons[reason] += 1
        if rejected_reasons:
            for reason, count in rejected_reasons.items():
                increment_metric(
                    "storage_bars_rejected_total", count, provider=provider_key, reason=reason
                )
            _LOGGER.debug(
                "skipped non-authoritative bars",
                extra={
                    "provider": provider_key,
                    "rejected": sum(rejected_reasons.values()),
                    "reasons": dict(rejected_reasons),
                },
            )
        if not allowed_bars:
            observe_metric(
                "storage_bars_write_seconds",
                time.perf_counter() - started_at,
                provider=provider_key,
            )
            return CanonicalBarCommitReceipt(
                revision_sequence=commit_sequence,
                rejected=tuple(bars),
            )
        slots = {
            (require_persisted_bar_timeframe(bar.timeframe), ensure_utc(bar.ts))
            for bar in allowed_bars
        }
        if len(slots) != len(allowed_bars):
            raise ValueError("write_bars requires unique timeframe/timestamp slots")

        try:
            ordered_bars = sorted(
                allowed_bars,
                key=lambda bar: (
                    require_persisted_bar_timeframe(bar.timeframe),
                    ensure_utc(bar.ts),
                ),
            )
            input_payload = [
                {
                    "input_ordinal": ordinal,
                    "instrument_id": instrument_id,
                    "route_fingerprint": exact_route_fingerprint,
                    "provider": provider_key,
                    "symbol": bar.symbol,
                    "timeframe": require_persisted_bar_timeframe(bar.timeframe),
                    "ts": ensure_utc(bar.ts).isoformat(),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                    "source": bar.source,
                    "closed": bool(bar.closed),
                    "revision_sequence": commit_sequence,
                }
                for ordinal, bar in enumerate(ordered_bars)
            ]
            input_json = json.dumps(
                input_payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            cur.execute(
                """
                WITH input AS MATERIALIZED (
                    SELECT *
                    FROM jsonb_to_recordset(%s::jsonb) AS row(
                        input_ordinal integer,
                        instrument_id text,
                        route_fingerprint text,
                        provider text,
                        symbol text,
                        timeframe text,
                        ts timestamptz,
                        open double precision,
                        high double precision,
                        low double precision,
                        close double precision,
                        volume double precision,
                        source text,
                        closed boolean,
                        revision_sequence bigint
                    )
                )
                INSERT INTO bars (
                    instrument_id, route_fingerprint, provider, symbol, timeframe, ts,
                    open, high, low, close, volume, source, closed, revision_sequence
                )
                SELECT
                    instrument_id, route_fingerprint, provider, symbol, timeframe, ts,
                    open, high, low, close, volume, source, closed, revision_sequence
                FROM input
                ORDER BY input_ordinal
                ON CONFLICT (instrument_id, route_fingerprint, timeframe, ts) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    symbol = EXCLUDED.symbol,
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    source = EXCLUDED.source,
                    closed = EXCLUDED.closed,
                    revision_sequence = EXCLUDED.revision_sequence,
                    updated_at = now()
                WHERE
                    EXCLUDED.revision_sequence > bars.revision_sequence
                    AND NOT (bars.closed = true AND EXCLUDED.closed = false)
                    AND (
                        bars.open IS DISTINCT FROM EXCLUDED.open
                        OR bars.high IS DISTINCT FROM EXCLUDED.high
                        OR bars.low IS DISTINCT FROM EXCLUDED.low
                        OR bars.close IS DISTINCT FROM EXCLUDED.close
                        OR bars.volume IS DISTINCT FROM EXCLUDED.volume
                        OR bars.source IS DISTINCT FROM EXCLUDED.source
                        OR bars.closed IS DISTINCT FROM EXCLUDED.closed
                    )
                RETURNING timeframe, ts
                """,
                (input_json,),
            )
            written_rows = len(cur.fetchall())
            cur.execute(
                """
                WITH input AS MATERIALIZED (
                    SELECT *
                    FROM jsonb_to_recordset(%s::jsonb) AS row(
                        input_ordinal integer,
                        instrument_id text,
                        route_fingerprint text,
                        provider text,
                        symbol text,
                        timeframe text,
                        ts timestamptz,
                        open double precision,
                        high double precision,
                        low double precision,
                        close double precision,
                        volume double precision,
                        source text,
                        closed boolean,
                        revision_sequence bigint
                    )
                )
                SELECT
                    persisted.symbol, persisted.ts,
                    persisted.open, persisted.high, persisted.low, persisted.close,
                    persisted.volume, persisted.timeframe, persisted.source,
                    persisted.closed, persisted.revision_sequence
                FROM input
                JOIN bars AS persisted
                  ON persisted.instrument_id = input.instrument_id
                 AND persisted.route_fingerprint = input.route_fingerprint
                 AND persisted.timeframe = input.timeframe
                 AND persisted.ts = input.ts
                ORDER BY input.input_ordinal
                """,
                (input_json,),
            )
            persisted_rows = cur.fetchall()
            provenance = canonical_storage_bar_provenance(
                provider_key, instrument_id, exact_route_fingerprint
            )
            persisted = [
                (
                    Bar(
                        symbol=row[0],
                        ts=ensure_utc(row[1]),
                        open=float(row[2]),
                        high=float(row[3]),
                        low=float(row[4]),
                        close=float(row[5]),
                        volume=float(row[6]),
                        timeframe=row[7],
                        source=row[8],
                        closed=bool(row[9]),
                        provenance=provenance,
                    ),
                    int(row[10]),
                )
                for row in persisted_rows
            ]
            return canonical_bar_commit_receipt(
                allowed_bars,
                persisted,
                revision_sequence=commit_sequence,
                written=written_rows,
                rejected=[bar for bar in bars if bar not in allowed_bars],
                timeframe_normalizer=require_persisted_bar_timeframe,
            )
        except Exception:
            increment_metric("storage_bars_errors_total", provider=provider_key)
            raise
        finally:
            observe_metric(
                "storage_bars_write_seconds",
                time.perf_counter() - started_at,
                provider=provider_key,
            )

    def _finalize_bars_commit(
        self,
        receipt: CanonicalBarCommitReceipt,
        *,
        provider: str,
    ) -> None:
        provider_key = provider_key_for_source(provider)
        if receipt.written > 0 or receipt.deleted > 0:
            try:
                self._invalidate_bar_slots_cache()
            except Exception:
                _LOGGER.exception(
                    "storage_bar_cache_invalidation_failed provider=%s",
                    provider_key,
                )
        try:
            increment_metric(
                "storage_bars_written_total",
                receipt.written,
                provider=provider_key,
            )
            increment_metric(
                "storage_bars_deleted_total",
                receipt.deleted,
                provider=provider_key,
            )
        except Exception:
            _LOGGER.exception(
                "storage_bar_commit_metric_failed provider=%s",
                provider_key,
            )

    def write_bars(
        self,
        bars: Sequence[Bar],
        provider: str,
        *,
        instrument: dict[str, Any],
        revision_sequence: int | None = None,
    ) -> CanonicalBarCommitReceipt:
        for bar in bars:
            require_persisted_bar_timeframe(bar.timeframe)
        if not bars:
            if revision_sequence is not None and (
                isinstance(revision_sequence, bool)
                or not isinstance(revision_sequence, int)
                or revision_sequence <= 0
            ):
                raise ValueError("revision_sequence must be positive")
            empty_sequence = (
                revision_sequence
                if revision_sequence is not None
                else self.reserve_bar_revision_sequence()
            )
            return CanonicalBarCommitReceipt(revision_sequence=empty_sequence)
        symbols = {str(bar.symbol or "") for bar in bars}
        if len(symbols) != 1:
            raise ValueError("write_bars requires one provider-qualified instrument per call")
        provider_key, resolved_instrument, instrument_id = _ordinary_bars_storage_route(
            provider,
            instrument,
            symbol=next(iter(symbols)),
        )
        exact_route_fingerprint = route_fingerprint(resolved_instrument)
        route = route_instrument(
            resolved_instrument,
            expected_source=provider_key,
        )
        expected_provider_contract_id = route.adapter.session_contract_id(route.instrument)
        expected_data_type = route.adapter.bar_data_type(route.instrument)
        # Reject route/provenance drift before allocating a connection. The
        # cursor owner repeats this validation at the transactional boundary.
        for bar in bars:
            _storage_bar_reject_reason(
                provider_key,
                bar,
                instrument_id=instrument_id,
                expected_route_fingerprint=exact_route_fingerprint,
                expected_provider_contract_id=expected_provider_contract_id,
                expected_data_type=expected_data_type,
                strict_identity=True,
            )
        with self._canonical_write_cursor() as cur:
            if revision_sequence is None:
                cur.execute("SELECT nextval('canonical_bar_revision_sequence')")
                sequence_row = cur.fetchone()
                commit_sequence = int(sequence_row[0]) if sequence_row else 0
            else:
                commit_sequence = revision_sequence
            receipt = self._write_bars_on_cursor(
                cur,
                bars,
                provider,
                instrument=instrument,
                revision_sequence=commit_sequence,
            )
        self._finalize_bars_commit(receipt, provider=provider)
        return receipt

    def read_bars(
        self,
        timeframe: str,
        provider: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        *,
        instrument: dict[str, Any],
        latest: bool = False,
    ) -> list[Bar]:
        _futures_provider, resolved_instrument, instrument_id = _storage_route(
            (provider,), instrument
        )
        _raise_if_futures_instrument(resolved_instrument)
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        conditions = ["instrument_id = %s", "route_fingerprint = %s", "timeframe = %s"]
        params: list[Any] = [instrument_id, route_fingerprint(resolved_instrument), timeframe_key]
        if start is not None:
            conditions.append("ts >= %s")
            params.append(ensure_utc(start))
        if end is not None:
            conditions.append("ts <= %s")
            params.append(ensure_utc(end))
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT %s"
            params.append(max(int(limit), 1))
        order_direction = "DESC" if latest else "ASC"
        sql = (
            "SELECT symbol, ts, open, high, low, close, volume, timeframe, source, closed "
            f"FROM bars WHERE {' AND '.join(conditions)} ORDER BY ts {order_direction}{limit_clause}"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        if latest:
            rows = list(reversed(rows))
        provenance = canonical_storage_bar_provenance(
            provider_key_for_source(provider), instrument_id, route_fingerprint(resolved_instrument)
        )
        return [
            Bar(
                symbol=row[0],
                ts=ensure_utc(row[1]),
                open=float(row[2]),
                high=float(row[3]),
                low=float(row[4]),
                close=float(row[5]),
                volume=float(row[6]),
                timeframe=row[7],
                source=row[8],
                closed=bool(row[9]),
                provenance=provenance,
            )
            for row in rows
        ]

    def read_bar_logical_projection(
        self,
        timeframe: str,
        provider: str,
        anchor_timestamps: Sequence[datetime],
        *,
        instrument: dict[str, Any],
        derived_timeframe: str | None = None,
    ) -> ConfirmedBarLogicalProjection:
        _futures_provider, resolved_instrument, instrument_id = _storage_route(
            (provider,),
            instrument,
        )
        _raise_if_futures_instrument(resolved_instrument)
        return read_confirmed_bar_logical_projection(
            self,
            table="bars",
            route_conditions=(
                "instrument_id = %s",
                "route_fingerprint = %s",
                "provider = %s",
            ),
            route_params=(
                instrument_id,
                route_fingerprint(resolved_instrument),
                provider_key_for_source(provider),
            ),
            timeframe=require_persisted_bar_timeframe(timeframe),
            anchor_timestamps=anchor_timestamps,
            derived_timeframe=derived_timeframe,
        )

    def read_confirmed_bar_timestamps(
        self,
        timeframe: str,
        provider: str,
        anchor_timestamps: Sequence[datetime],
        *,
        instrument: dict[str, Any],
        derived_timeframe: str | None = None,
    ) -> frozenset[datetime]:
        _futures_provider, resolved_instrument, instrument_id = _storage_route(
            (provider,),
            instrument,
        )
        _raise_if_futures_instrument(resolved_instrument)
        return read_confirmed_bar_timestamps(
            self,
            table="bars",
            route_conditions=(
                "instrument_id = %s",
                "route_fingerprint = %s",
                "provider = %s",
            ),
            route_params=(
                instrument_id,
                route_fingerprint(resolved_instrument),
                provider_key_for_source(provider),
            ),
            timeframe=require_persisted_bar_timeframe(timeframe),
            anchor_timestamps=anchor_timestamps,
            derived_timeframe=derived_timeframe,
        )

    def read_bars_multi(
        self,
        timeframes: Sequence[str],
        providers: Sequence[str],
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        instrument: dict[str, Any],
        timeframe_read_windows: Mapping[
            str,
            tuple[datetime | None, datetime | None, int | None],
        ]
        | None = None,
    ) -> dict[tuple[str, str], list[Bar]]:
        _futures_provider, resolved_instrument, instrument_id = _storage_route(
            providers, instrument
        )
        _raise_if_futures_instrument(resolved_instrument)
        timeframe_list = list(
            dict.fromkeys(require_persisted_bar_timeframe(item) for item in timeframes)
        )
        provider_list = list(dict.fromkeys(str(item) for item in providers if str(item)))
        if not timeframe_list or not provider_list:
            return {}
        provider_placeholders = ", ".join(["%s"] * len(provider_list))
        normalized_read_windows = {
            require_persisted_bar_timeframe(timeframe): (
                ensure_utc(window_start) if window_start is not None else None,
                ensure_utc(window_end) if window_end is not None else None,
                (max(int(row_limit), 1) if row_limit is not None else None),
            )
            for timeframe, (
                window_start,
                window_end,
                row_limit,
            ) in (timeframe_read_windows or {}).items()
        }
        if normalized_read_windows:
            missing_windows = set(timeframe_list) - set(normalized_read_windows)
            if missing_windows:
                raise ValueError("timeframe_read_windows must cover every requested timeframe")
            requested_rows = [
                (
                    timeframe,
                    provider,
                    *normalized_read_windows[timeframe],
                )
                for timeframe in timeframe_list
                for provider in provider_list
            ]
            values_sql = ", ".join(
                "(%s::text, %s::text, %s::timestamptz, %s::timestamptz, %s::integer)"
                for _row in requested_rows
            )
            params: list[Any] = [value for row in requested_rows for value in row]
            params.extend(
                (
                    instrument_id,
                    route_fingerprint(resolved_instrument),
                )
            )
            sql = f"""
                WITH requested(
                    timeframe,
                    provider,
                    start_inclusive,
                    end_exclusive,
                    row_limit
                ) AS (
                    VALUES {values_sql}
                )
                SELECT
                    selected.provider,
                    selected.timeframe,
                    selected.symbol,
                    selected.ts,
                    selected.open,
                    selected.high,
                    selected.low,
                    selected.close,
                    selected.volume,
                    selected.source,
                    selected.closed
                FROM requested
                CROSS JOIN LATERAL (
                    SELECT
                        bars.provider,
                        bars.timeframe,
                        bars.symbol,
                        bars.ts,
                        bars.open,
                        bars.high,
                        bars.low,
                        bars.close,
                        bars.volume,
                        bars.source,
                        bars.closed
                    FROM bars
                    WHERE bars.instrument_id = %s
                      AND bars.route_fingerprint = %s
                      AND bars.timeframe = requested.timeframe
                      AND bars.provider = requested.provider
                      AND (
                          requested.start_inclusive IS NULL
                          OR bars.ts >= requested.start_inclusive
                      )
                      AND (
                          requested.end_exclusive IS NULL
                          OR bars.ts < requested.end_exclusive
                      )
                    ORDER BY bars.ts DESC
                    LIMIT requested.row_limit
                ) AS selected
                ORDER BY selected.timeframe ASC, selected.provider ASC, selected.ts ASC
            """
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
        else:
            timeframe_placeholders = ", ".join(["%s"] * len(timeframe_list))
            conditions = [
                "instrument_id = %s",
                "route_fingerprint = %s",
                f"timeframe IN ({timeframe_placeholders})",
                f"provider IN ({provider_placeholders})",
            ]
            params = [
                instrument_id,
                route_fingerprint(resolved_instrument),
                *timeframe_list,
                *provider_list,
            ]
            if start is not None:
                conditions.append("ts >= %s")
                params.append(ensure_utc(start))
            if end is not None:
                conditions.append("ts <= %s")
                params.append(ensure_utc(end))
            sql = (
                "SELECT provider, timeframe, symbol, ts, open, high, low, close, volume, source, closed "
                f"FROM bars WHERE {' AND '.join(conditions)} "
                "ORDER BY timeframe ASC, provider ASC, ts ASC"
            )
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
        grouped: dict[tuple[str, str], list[Bar]] = {}
        provenance_by_provider: dict[str, BarProvenance] = {}
        fingerprint = route_fingerprint(resolved_instrument)
        for row in rows:
            key = (str(row[0]), str(row[1]))
            if key[0] not in provenance_by_provider:
                provenance_by_provider[key[0]] = canonical_storage_bar_provenance(
                    provider_key_for_source(row[0]), instrument_id, fingerprint
                )
            grouped.setdefault(key, []).append(
                Bar(
                    symbol=row[2],
                    ts=ensure_utc(row[3]),
                    open=float(row[4]),
                    high=float(row[5]),
                    low=float(row[6]),
                    close=float(row[7]),
                    volume=float(row[8]),
                    timeframe=row[1],
                    source=row[9],
                    closed=bool(row[10]),
                    provenance=provenance_by_provider[key[0]],
                )
            )
        return grouped

    def get_full_snapshot_context(
        self,
        *,
        interval: str,
        bars: Sequence[Bar],
        providers: Sequence[str],
        mtf_timeframes: Sequence[str],
        mtf_context_ends: Mapping[str, datetime] | None = None,
        mtf_history_bars: Mapping[str, int] | None = None,
        history_start: datetime | None = None,
        instrument: dict[str, Any],
        include_mtf_quality: bool = False,
        analysis_as_of_utc: datetime | None = None,
    ) -> dict[str, Any]:
        if not bars:
            return {
                "bars": [],
                "bar_slots": None,
                "mtf_context": {},
                "mtf_context_slots": {},
                "mtf_context_quality": {},
                "cache_status": {},
            }
        interval_key = str(interval or "").strip().lower()
        source_interval_key = require_persisted_bar_timeframe(
            canonical_bar_source_interval(interval_key)
        )
        mtf_timeframe_keys = list(
            dict.fromkeys(require_persisted_bar_timeframe(item) for item in mtf_timeframes)
        )
        requested_timeframes = list(dict.fromkeys([source_interval_key, *mtf_timeframe_keys]))
        step = max(interval_minutes(interval_key), 1)
        futures_route, resolved_instrument, instrument_id = _storage_route(providers, instrument)
        requested_start = ensure_utc(bars[0].ts)
        requested_end = ensure_utc(bars[-1].ts)
        query_start = (
            min(requested_start, ensure_utc(history_start))
            if history_start is not None
            else requested_start
        )
        with self._bar_slots_cache_lock:
            cache_generation = self._bar_slots_cache_generation
        grouped_context: dict[str, list[Bar]] = {
            timeframe: [] for timeframe in requested_timeframes
        }
        context_query_end = requested_end + timedelta(
            minutes=max(
                step
                - min(
                    (interval_minutes(timeframe) for timeframe in mtf_timeframe_keys),
                    default=step,
                ),
                0,
            )
        )
        normalized_context_ends = {
            require_persisted_bar_timeframe(timeframe): ensure_utc(end)
            for timeframe, end in (mtf_context_ends or {}).items()
            if isinstance(end, datetime)
        }
        if analysis_as_of_utc is not None:
            if (
                not isinstance(analysis_as_of_utc, datetime)
                or analysis_as_of_utc.tzinfo is None
                or analysis_as_of_utc.utcoffset() is None
            ):
                raise ValueError("analysis_as_of_utc must be timezone-aware")
            resolved_analysis_as_of = ensure_utc(analysis_as_of_utc)
            for timeframe in mtf_timeframe_keys:
                existing_end = normalized_context_ends.get(timeframe)
                confirmed_context_end = interval_bucket(
                    resolved_analysis_as_of,
                    timeframe,
                )
                normalized_context_ends[timeframe] = (
                    min(existing_end, confirmed_context_end)
                    if existing_end is not None
                    else confirmed_context_end
                )
        else:
            resolved_analysis_as_of = None
        normalized_history_bars = {
            require_persisted_bar_timeframe(timeframe): max(int(count), 1)
            for timeframe, count in (mtf_history_bars or {}).items()
            if not isinstance(count, bool) and isinstance(count, int) and count > 0
        }
        if normalized_context_ends:
            context_query_end = max(
                context_query_end,
                max(normalized_context_ends.values()) - timedelta(microseconds=1),
            )
        timeframe_read_windows: dict[
            str,
            tuple[datetime | None, datetime | None, int | None],
        ] = {
            source_interval_key: (
                requested_start,
                requested_end + timedelta(minutes=step),
                None,
            )
        }
        for timeframe in mtf_timeframe_keys:
            context_end = normalized_context_ends.get(timeframe)
            requested_context_end = (
                context_end
                if context_end is not None
                else context_query_end + timedelta(microseconds=1)
            )
            history_limit = normalized_history_bars.get(timeframe)
            if timeframe != source_interval_key:
                timeframe_read_windows[timeframe] = (
                    None if history_limit is not None else query_start,
                    requested_context_end,
                    history_limit,
                )
        if futures_route:
            futures_provider, history_route = futures_route
            futures_reader = _futures_history_reader(
                self,
                history_route,
                multi=True,
            )
            grouped_context = futures_reader(
                provider=futures_provider,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint(resolved_instrument),
                symbol=instrument_key(resolved_instrument),
                timeframes=requested_timeframes,
                start=query_start,
                end=context_query_end,
                timeframe_read_windows=timeframe_read_windows,
                **_futures_history_route_kwargs(history_route),
            )
        else:
            grouped = self.read_bars_multi(
                timeframes=requested_timeframes,
                providers=providers,
                start=query_start,
                end=context_query_end,
                instrument=resolved_instrument,
                timeframe_read_windows=timeframe_read_windows,
            )
            for timeframe in requested_timeframes:
                selected: dict[datetime, Bar] = {}
                for provider in providers:
                    for bar in grouped.get((str(provider), timeframe), []):
                        selected.setdefault(bar.ts.astimezone(UTC), bar)
                grouped_context[timeframe] = [selected[ts] for ts in sorted(selected)]
        source_bars = [
            bar
            for bar in grouped_context.get(source_interval_key, [])
            if (requested_start <= ensure_utc(bar.ts) < requested_end + timedelta(minutes=step))
        ]
        canonical_bars = [
            bar
            for bar in derive_chart_bars(
                source_bars,
                interval_key,
                as_of=resolved_analysis_as_of,
            )
            if (
                requested_start <= ensure_utc(bar.ts) <= requested_end
                and not is_provisional_bar(bar)
            )
        ]
        snapshot_by_ts = {ensure_utc(bar.ts): bar for bar in bars if is_provisional_bar(bar)}
        snapshot_by_ts.update((ensure_utc(bar.ts), bar) for bar in canonical_bars)
        snapshot_bars = [snapshot_by_ts[ts] for ts in sorted(snapshot_by_ts)]
        mtf_context: dict[str, list[Bar]] = {}
        for timeframe in mtf_timeframe_keys:
            context_bars = grouped_context.get(timeframe, [])
            context_end = normalized_context_ends.get(timeframe)
            if context_end is not None:
                context_bars = [bar for bar in context_bars if ensure_utc(bar.ts) < context_end]
            if resolved_analysis_as_of is not None:
                context_step = max(interval_minutes(timeframe), 1)
                context_bars = [
                    bar
                    for bar in context_bars
                    if (
                        ensure_utc(bar.ts) + timedelta(minutes=context_step)
                        <= resolved_analysis_as_of
                    )
                ]
            history_limit = normalized_history_bars.get(timeframe)
            if history_limit is not None:
                context_bars = context_bars[-history_limit:]
            if context_bars:
                mtf_context[timeframe] = sorted(
                    context_bars,
                    key=lambda item: item.ts,
                )
        if futures_route:
            slot_provider = futures_route[0]
        elif providers:
            slot_provider = str(providers[0])
        else:
            raise ValueError(f"STORAGE_PROVIDER_REQUIRED instrument_id={instrument_id}")
        mtf_context_slots: dict[str, ProviderBarSlotSequence] = {}
        mtf_slot_schedule_states: dict[str, str] = {}
        for timeframe, context_bars in mtf_context.items():
            context_step = max(interval_minutes(timeframe), 1)
            context_slot_map = self._bar_slot_map_for_canonical_timestamps(
                timeframe=timeframe,
                provider=slot_provider,
                start=ensure_utc(context_bars[0].ts),
                end=ensure_utc(context_bars[-1].ts),
                step=context_step,
                instrument=resolved_instrument,
                timestamps=[bar.ts for bar in context_bars],
                cache_generation=cache_generation,
            )
            context_slot_state = str(getattr(context_slot_map, "schedule_state", "unknown"))
            mtf_slot_schedule_states[timeframe] = context_slot_state
            if getattr(context_slot_map, "authoritative", False) is True and all(
                ensure_utc(bar.ts) in context_slot_map for bar in context_bars
            ):
                mtf_context_slots[timeframe] = ProviderBarSlotSequence(
                    (context_slot_map[ensure_utc(bar.ts)] for bar in context_bars),
                    schedule_state=context_slot_state,
                )
        mtf_context_quality = (
            _provider_mtf_context_quality(
                self,
                snapshot_bars,
                mtf_context,
                instrument=resolved_instrument,
                timeframes=mtf_timeframe_keys,
                context_ends=normalized_context_ends,
                history_bars=normalized_history_bars,
                analysis_as_of_utc=resolved_analysis_as_of,
            )
            if include_mtf_quality
            else {}
        )
        if include_mtf_quality:
            for timeframe in mtf_timeframe_keys:
                context_bars = mtf_context.get(timeframe, ())
                context_slots = mtf_context_slots.get(timeframe)
                slot_axis_authoritative = bool(
                    context_slots is not None
                    and context_slots.authoritative
                    and len(context_slots) == len(context_bars)
                )
                quality_item = mtf_context_quality.setdefault(timeframe, {})
                quality_item.update(
                    {
                        "slot_axis_authoritative": slot_axis_authoritative,
                        "slot_schedule_state": mtf_slot_schedule_states.get(
                            timeframe,
                            "unknown",
                        ),
                        "slot_count": (len(context_slots) if context_slots is not None else 0),
                    }
                )
        if not snapshot_bars:
            return {
                "bars": [],
                "bar_slots": None,
                "slots": None,
                "bar_slot_schedule_state": "unknown",
                "mtf_context": mtf_context,
                "mtf_context_slots": mtf_context_slots,
                "mtf_context_quality": mtf_context_quality,
                "cache_status": {},
            }
        slot_map = self._bar_slot_map_for_canonical_timestamps(
            timeframe=interval_key,
            provider=slot_provider,
            start=requested_start,
            end=requested_end,
            step=step,
            instrument=resolved_instrument,
            timestamps=[bar.ts for bar in canonical_bars],
            cache_generation=cache_generation,
        )
        slot_state = getattr(
            slot_map,
            "schedule_state",
            "continuous"
            if route_instrument(resolved_instrument).adapter.continuous_session(resolved_instrument)
            else "unknown",
        )
        slot_authoritative = slot_state in {"continuous", "verified"}
        if not slot_authoritative:
            return {
                "bars": snapshot_bars,
                "bar_slots": None,
                "slots": None,
                "bar_slot_schedule_state": slot_state,
                "mtf_context": mtf_context,
                "mtf_context_slots": mtf_context_slots,
                "mtf_context_quality": mtf_context_quality,
                "cache_status": {},
            }
        bar_slots: list[int] = []
        last_slot: int | None = None
        continuous_session = route_instrument(resolved_instrument).adapter.continuous_session(
            resolved_instrument
        )
        for index, bar in enumerate(snapshot_bars):
            slot = slot_map.get(bar.ts.astimezone(UTC))
            if slot is None:
                if bar.closed and slot_authoritative:
                    raise RuntimeError(
                        f"CONFIRMED_BAR_SLOT_MISSING provider={slot_provider} "
                        f"instrument_id={instrument_id} ts={bar.ts.isoformat()}"
                    )
                if continuous_session:
                    slot = continuous_provider_bar_slot(bar.ts, bar.timeframe)
                else:
                    slot = (last_slot + step) if last_slot is not None else index * step
            if last_slot is not None and slot <= last_slot:
                raise RuntimeError(
                    f"BAR_SLOT_ORDER_INVALID provider={slot_provider} "
                    f"instrument_id={instrument_id} ts={bar.ts.isoformat()}"
                )
            bar_slots.append(int(slot))
            last_slot = int(slot)
        typed_bar_slots = ProviderBarSlotSequence(
            bar_slots,
            schedule_state=slot_state,
        )
        return {
            "bars": snapshot_bars,
            "bar_slots": typed_bar_slots,
            "slots": typed_bar_slots,
            "bar_slot_schedule_state": slot_state,
            "mtf_context": mtf_context,
            "mtf_context_slots": mtf_context_slots,
            "mtf_context_quality": mtf_context_quality,
            "cache_status": {},
        }

    def read_recent_bars(
        self,
        timeframe: str,
        provider: str,
        limit: int = 2,
        *,
        instrument: dict[str, Any],
    ) -> list[Bar]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("RECENT_BAR_LIMIT_INVALID")
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        futures_route, resolved_instrument, instrument_id = _storage_route((provider,), instrument)
        if futures_route:
            futures_provider, history_route = futures_route
            futures_reader = _futures_history_reader(
                self,
                history_route,
                multi=False,
            )
            return require_ordered_bar_list(
                futures_reader(
                    provider=futures_provider,
                    instrument_id=instrument_id,
                    route_fingerprint=route_fingerprint(resolved_instrument),
                    symbol=instrument_key(resolved_instrument),
                    timeframe=timeframe_key,
                    limit=limit,
                    latest=True,
                    **_futures_history_route_kwargs(history_route),
                ),
                timeframe=timeframe_key,
                field="futures recent-bar storage",
                require_confirmed=True,
            )
        return require_ordered_bar_list(
            self.read_bars(
                timeframe=timeframe_key,
                provider=provider,
                limit=limit,
                instrument=resolved_instrument,
                latest=True,
            ),
            timeframe=timeframe_key,
            field="recent-bar storage",
            require_confirmed=True,
        )

    def read_recent_bars_batch(
        self,
        requests: Sequence[tuple[str, str, int, dict[str, Any]]],
    ) -> dict[tuple[str, str], list[Bar]]:
        resolved: dict[tuple[str, str], tuple[str, str, int, dict[str, Any], Any | None]] = {}
        for timeframe, provider, limit, instrument in requests:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("RECENT_BAR_BATCH_LIMIT_INVALID")
            timeframe_key = require_persisted_bar_timeframe(timeframe)
            futures_route, resolved_instrument, instrument_id = _storage_route(
                (provider,), instrument
            )
            key = (instrument_id, timeframe_key)
            previous = resolved.get(key)
            requested_limit = limit
            if previous is None or requested_limit > previous[2]:
                resolved[key] = (
                    provider,
                    route_fingerprint(resolved_instrument),
                    requested_limit,
                    resolved_instrument,
                    futures_route,
                )
        grouped: dict[tuple[str, str], list[Bar]] = {key: [] for key in resolved}
        normal_rows: list[tuple[str, str, str, int]] = []
        futures_batches: dict[
            tuple[str, str, str, CanonicalHistoryRoute],
            dict[str, Any],
        ] = {}
        for (instrument_id, timeframe), (
            provider,
            fingerprint,
            limit,
            instrument,
            futures_route,
        ) in resolved.items():
            if futures_route is not None:
                futures_provider, history_route = futures_route
                batch = futures_batches.setdefault(
                    (
                        futures_provider,
                        instrument_id,
                        fingerprint,
                        history_route,
                    ),
                    {
                        "symbol": instrument_key(instrument),
                        "limits": {},
                    },
                )
                batch["limits"][timeframe] = max(limit, int(batch["limits"].get(timeframe, 0)))
                continue
            normal_rows.append((instrument_id, fingerprint, timeframe, limit))
        for (
            futures_provider,
            instrument_id,
            fingerprint,
            history_route,
        ), batch in futures_batches.items():
            limits = batch["limits"]
            futures_batch_reader = _futures_history_reader(
                self,
                history_route,
                multi=True,
            )
            recent = futures_batch_reader(
                provider=futures_provider,
                instrument_id=instrument_id,
                route_fingerprint=fingerprint,
                symbol=batch["symbol"],
                timeframes=tuple(limits),
                limit=max(limits.values()),
                latest=True,
                **_futures_history_route_kwargs(history_route),
            )
            if not isinstance(recent, dict) or set(recent) != set(limits):
                raise RuntimeError("futures recent-bar storage returned incomplete timeframes")
            for timeframe, requested_limit in limits.items():
                bars = require_ordered_bar_list(
                    recent[timeframe],
                    timeframe=timeframe,
                    field="futures recent-bar storage",
                    require_confirmed=True,
                )
                grouped[(instrument_id, timeframe)] = bars[-requested_limit:]
        if not normal_rows:
            for (_instrument_id, timeframe), bars in grouped.items():
                require_ordered_bar_list(
                    bars,
                    timeframe=timeframe,
                    field="recent-bar batch storage",
                    require_confirmed=True,
                )
            return grouped
        values_sql = ", ".join("(%s::text, %s::text, %s::text, %s::integer)" for _ in normal_rows)
        params: list[Any] = [value for row in normal_rows for value in row]
        sql = f"""
            WITH requested(instrument_id, route_fingerprint, timeframe, bar_limit) AS (
                VALUES {values_sql}
            )
            SELECT
                bars.instrument_id, bars.route_fingerprint, bars.symbol, bars.ts,
                bars.open, bars.high, bars.low, bars.close, bars.volume,
                bars.timeframe, bars.source, bars.closed
            FROM requested
            CROSS JOIN LATERAL (
                SELECT
                    bars.instrument_id,
                    bars.route_fingerprint,
                    bars.symbol,
                    bars.ts,
                    bars.open,
                    bars.high,
                    bars.low,
                    bars.close,
                    bars.volume,
                    bars.timeframe,
                    bars.source,
                    bars.closed
                FROM bars
                WHERE bars.instrument_id = requested.instrument_id
                  AND bars.route_fingerprint = requested.route_fingerprint
                  AND bars.timeframe = requested.timeframe
                ORDER BY bars.ts DESC
                LIMIT requested.bar_limit
            ) AS bars
            ORDER BY bars.instrument_id ASC, bars.route_fingerprint ASC,
                     bars.timeframe ASC, bars.ts ASC
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        provenance_by_scope: dict[tuple[str, str, str], BarProvenance] = {}
        for row in rows:
            key = (require_exact_identity_text(row[0], field="instrument_id"), str(row[9]))
            resolved_provider = resolved[key][0]
            scope = (
                resolved_provider,
                key[0],
                require_exact_identity_text(row[1], field="route_fingerprint"),
            )
            if scope not in provenance_by_scope:
                provenance_by_scope[scope] = canonical_storage_bar_provenance(
                    provider_key_for_source(resolved_provider), scope[1], scope[2]
                )
            grouped.setdefault(key, []).append(
                Bar(
                    symbol=row[2],
                    ts=ensure_utc(row[3]),
                    open=float(row[4]),
                    high=float(row[5]),
                    low=float(row[6]),
                    close=float(row[7]),
                    volume=float(row[8]),
                    timeframe=row[9],
                    source=row[10],
                    closed=row[11],
                    provenance=provenance_by_scope[scope],
                )
            )
        for (_instrument_id, timeframe), bars in grouped.items():
            require_ordered_bar_list(
                bars,
                timeframe=timeframe,
                field="recent-bar batch storage",
                require_confirmed=True,
            )
        return grouped

    def read_bar_boundary(
        self,
        timeframe: str,
        provider: str,
        *,
        direction: str = "latest",
        instrument: dict[str, Any],
    ) -> datetime | None:
        normalized = str(direction or "latest").lower()
        order = "DESC" if normalized in {"latest", "forward", "max"} else "ASC"
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        futures_route, resolved_instrument, instrument_id = _storage_route((provider,), instrument)
        if futures_route:
            futures_provider, history_route = futures_route
            futures_reader = _futures_history_reader(
                self,
                history_route,
                multi=False,
            )
            bars = futures_reader(
                provider=futures_provider,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint(resolved_instrument),
                symbol=instrument_key(resolved_instrument),
                timeframe=timeframe_key,
                limit=1,
                latest=(order == "DESC"),
                **_futures_history_route_kwargs(history_route),
            )
            return ensure_utc(bars[0].ts) if bars else None
        sql = (
            "SELECT ts FROM bars "
            "WHERE instrument_id = %s AND route_fingerprint = %s AND timeframe = %s "
            f"ORDER BY ts {order} LIMIT 1"
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql, (instrument_id, route_fingerprint(resolved_instrument), timeframe_key)
                )
                row = cur.fetchone()
        return ensure_utc(row[0]) if row and row[0] is not None else None

    def read_bar_slots(
        self,
        timeframe: str,
        provider: str,
        start: datetime,
        end: datetime,
        step: int,
        *,
        instrument: dict[str, Any],
        required_timestamps: Sequence[datetime] = (),
        required_timestamps_only: bool = False,
    ) -> ProviderBarSlotMap:
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        futures_route, resolved_instrument, instrument_id = _storage_route((provider,), instrument)
        identity_token = route_fingerprint(resolved_instrument)
        range_start = start_utc
        step_value = max(int(step), 1)
        expected_step = max(interval_minutes(timeframe_key), 1)
        if step_value != expected_step:
            raise ValueError(
                f"provider bar slot step mismatch for {timeframe_key}: expected {expected_step}, got {step_value}"
            )
        if not isinstance(required_timestamps_only, bool):
            raise ValueError("required_timestamps_only must be a boolean")
        exact_required_timestamps = tuple(
            sorted({ensure_utc(timestamp) for timestamp in required_timestamps})
        )
        if required_timestamps_only and not exact_required_timestamps:
            raise ValueError("required_timestamps_only requires exact bar-slot timestamps")
        if any(
            timestamp < start_utc or timestamp > end_utc for timestamp in exact_required_timestamps
        ):
            raise ValueError("required bar-slot timestamps must be inside the requested range")
        with self._bar_slots_cache_lock:
            cache_generation = self._bar_slots_cache_generation
        if required_timestamps_only:
            timestamps: list[datetime] = []
        elif futures_route:
            futures_provider, history_route = futures_route
            futures_reader = _futures_history_reader(
                self,
                history_route,
                multi=False,
            )
            bars = futures_reader(
                provider=futures_provider,
                instrument_id=instrument_id,
                route_fingerprint=identity_token,
                symbol=instrument_key(resolved_instrument),
                timeframe=timeframe_key,
                start=range_start,
                end=end_utc,
                **_futures_history_route_kwargs(history_route),
            )
            timestamps = sorted(
                {ensure_utc(bar.ts) for bar in bars if range_start <= ensure_utc(bar.ts) <= end_utc}
            )
        else:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT ts
                        FROM bars
                        WHERE instrument_id = %s
                          AND route_fingerprint = %s
                          AND timeframe = %s
                          AND ts >= %s
                          AND ts <= %s
                        ORDER BY ts ASC
                        """,
                        (
                            instrument_id,
                            identity_token,
                            timeframe_key,
                            range_start,
                            end_utc,
                        ),
                    )
                    rows = cur.fetchall()
            timestamps = [ensure_utc(row[0]) for row in rows]
        return self._bar_slot_map_for_canonical_timestamps(
            timeframe=timeframe_key,
            provider=provider,
            start=start_utc,
            end=end_utc,
            step=step_value,
            instrument=resolved_instrument,
            timestamps=timestamps,
            required_timestamps=exact_required_timestamps,
            cache_generation=cache_generation,
        )

    def _bar_slot_map_for_canonical_timestamps(
        self,
        *,
        timeframe: str,
        provider: str,
        start: datetime,
        end: datetime,
        step: int,
        instrument: dict[str, Any],
        timestamps: Sequence[datetime],
        required_timestamps: Sequence[datetime] = (),
        cache_generation: int,
    ) -> ProviderBarSlotMap:
        timeframe_key = require_persisted_bar_timeframe(timeframe)
        start_utc = ensure_utc(start)
        end_utc = ensure_utc(end)
        futures_route, resolved_instrument, instrument_id = _storage_route(
            (provider,),
            instrument,
        )
        identity_token = route_fingerprint(resolved_instrument)
        canonical_timestamps = tuple(sorted({ensure_utc(ts) for ts in timestamps}))
        exact_required_timestamps = tuple(sorted({ensure_utc(ts) for ts in required_timestamps}))
        generation_digest = hashlib.sha256(
            b"\0".join(
                timestamp.isoformat(timespec="microseconds").encode("ascii")
                for timestamp in canonical_timestamps
            )
        ).digest()
        required_generation_digest = hashlib.sha256(
            b"\0".join(
                timestamp.isoformat(timespec="microseconds").encode("ascii")
                for timestamp in exact_required_timestamps
            )
        ).digest()
        cache_key = (
            instrument_id,
            identity_token,
            timeframe_key,
            start_utc,
            end_utc,
            max(int(step), 1),
            generation_digest,
            required_generation_digest,
        )
        now = time.time()
        with self._bar_slots_cache_lock:
            if cache_generation == self._bar_slots_cache_generation:
                cached = self._bar_slots_cache.get(cache_key)
                if cached and (now - cached[0]) <= self._bar_slots_cache_ttl:
                    return ProviderBarSlotMap(
                        cached[1],
                        schedule_state=cached[1].schedule_state,
                    )
        route = route_instrument(resolved_instrument)
        continuous_session = not futures_route and route.adapter.continuous_session(
            route.instrument
        )
        if continuous_session:
            result = ProviderBarSlotMap(
                {
                    timestamp: continuous_provider_bar_slot(
                        timestamp,
                        timeframe_key,
                    )
                    for timestamp in sorted({*canonical_timestamps, *exact_required_timestamps})
                },
                schedule_state="continuous",
            )
        else:
            result = _provider_schedule_bar_slot_map(
                self,
                canonical_timestamps,
                timeframe=timeframe_key,
                step=max(int(step), 1),
                instrument=resolved_instrument,
                required_timestamps=exact_required_timestamps,
            )
        with self._bar_slots_cache_lock:
            if cache_generation == self._bar_slots_cache_generation:
                _cache_bar_slot_result(
                    self._bar_slots_cache,
                    cache_key,
                    now,
                    result,
                )
        return result
