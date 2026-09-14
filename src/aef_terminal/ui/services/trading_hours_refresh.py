from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic, perf_counter
from typing import Any

from aef_terminal.data.futures_lifecycle import current_contract_is_stale
from aef_terminal.data.instrument_identity import (
    futures_root,
    instrument_key,
    require_exact_identity_text,
)
from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_sessions import (
    ProviderSessionBarSlotCoverage,
    acknowledge_provider_trading_hours_refresh,
    invalidate_provider_trading_hours_cache,
    peek_pending_provider_trading_hours_refresh,
    pending_provider_trading_hours_refresh_count,
    provider_history_session_scope,
    provider_session_bar_slot_coverage_between,
    provider_trading_hours_refresh_generation,
)
from aef_terminal.runtime.async_tasks import (
    await_cancellation_deferred_task,
    run_physical_thread_call,
)
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric
from aef_terminal.runtime.timeframes import interval_bucket
from aef_terminal.ui.services.closed_session_maintenance_models import (
    ProviderRequestCompletionOutcome,
    ClosedSessionMaintenanceCycle,
    ClosedSessionMaintenanceSettings,
    ClosedSessionMaintenanceState,
)


_TradingHoursFetchKey = tuple[str, str, str, str, str]
_trading_hours_fetch_tasks: dict[_TradingHoursFetchKey, asyncio.Task[bool]] = {}
_trading_hours_fetch_results: dict[_TradingHoursFetchKey, tuple[float, bool]] = {}
_TRADING_HOURS_FETCH_COOLDOWN_SECONDS = 5 * 60.0
_TRADING_HOURS_FETCH_FAILURE_BACKOFF_SECONDS = 60.0
_BACKGROUND_SCHEDULE_RETRY_SECONDS = 60.0
_MAX_SCHEDULE_FETCH_ATTEMPTS_PER_PROVIDER_CYCLE = 1


class TradingHoursRefreshSuperseded(RuntimeError):
    """The exact route schedule became stale while its provider fetch was active."""


@dataclass(frozen=True)
class _ChartAxisScheduleProbe:
    schedule_state: str
    repair_from: datetime | None = None


async def _probe_chart_axis_schedule(
    store: Any,
    route: InstrumentRoute,
    *,
    required_until: datetime,
) -> _ChartAxisScheduleProbe:
    """Locate the first unverified boundary on one persistent provider axis."""

    session_scope = provider_history_session_scope(route.adapter, route.instrument)
    axis_anchor = await run_physical_thread_call(
        store.read_trading_session_axis_anchor,
        instrument=route.instrument,
        session_type=session_scope,
    )
    if not isinstance(axis_anchor, datetime):
        return _ChartAxisScheduleProbe("unknown")
    anchor_utc = axis_anchor.astimezone(UTC)
    coverage: ProviderSessionBarSlotCoverage = await run_physical_thread_call(
        provider_session_bar_slot_coverage_between,
        anchor_utc,
        required_until,
        "1m",
        session_scope=session_scope,
        store=store,
        instrument=route.instrument,
        # Slot enumeration is irrelevant here. The typed source-coverage
        # union and verified_until cursor are the schedule-repair authority.
        limit=1,
    )
    if coverage.schedule_state == "verified":
        return _ChartAxisScheduleProbe("verified")
    repair_from = coverage.verified_until or anchor_utc
    return _ChartAxisScheduleProbe(
        "unknown",
        repair_from=interval_bucket(repair_from, "1m"),
    )


def _trading_hours_fetch_key(
    route: InstrumentRoute,
    *,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> _TradingHoursFetchKey:
    if (starts_at is None) != (ends_at is None):
        raise ValueError("TRADING_SCHEDULE_WINDOW_REQUIRES_BOTH_BOUNDS")
    if starts_at is None or ends_at is None:
        window_start = ""
        window_end = ""
    else:
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise ValueError("TRADING_SCHEDULE_WINDOW_MUST_BE_TIMEZONE_AWARE")
        starts_utc = starts_at.astimezone(UTC)
        ends_utc = ends_at.astimezone(UTC)
        if ends_utc <= starts_utc:
            raise ValueError("TRADING_SCHEDULE_WINDOW_INVALID")
        window_start = starts_utc.date().isoformat()
        window_end = ends_utc.date().isoformat()
    return (
        route.provider,
        route.instrument_id,
        route.fingerprint,
        window_start,
        window_end,
    )


@dataclass(frozen=True)
class TradingHoursRefreshDeps:
    store_factory: Callable[[], Any]
    server_sleeping: Callable[[], bool]
    selected_instruments: Callable[[], list[dict[str, Any]]]
    logger_warning: Callable[..., None]
    active_chart_streams: Callable[[], list[tuple[str, str, str, int]]] | None = None
    resolve_current_futures_contract: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None
    refresh_quote_routes: Callable[..., Any] | None = None
    invalidate_market_analysis_route: Callable[[str, str], Awaitable[int]] | None = None
    watchlist_gap_quality: Callable[[str, str], dict[str, Any]] | None = None
    maintenance_settings: ClosedSessionMaintenanceSettings = field(
        default_factory=ClosedSessionMaintenanceSettings
    )
    maintenance_state: ClosedSessionMaintenanceState | None = None
    wakeup_event: asyncio.Event | None = None
    now_utc: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))


def recover_provider_trading_hours_after_connection(
    provider: str,
    *,
    maintenance_state: ClosedSessionMaintenanceState,
    wakeup_event: asyncio.Event,
) -> None:
    """Clear transport-era backoff and wake one provider schedule probe."""

    maintenance_state.record_provider_connection_recovered(provider)
    stale_keys = [key for key in _trading_hours_fetch_results if key[0] == provider]
    for key in stale_keys:
        _trading_hours_fetch_results.pop(key, None)
    wakeup_event.set()


async def refresh_trading_hours_once(
    deps: TradingHoursRefreshDeps,
) -> ClosedSessionMaintenanceCycle:
    started = perf_counter()
    checked_at = deps.now_utc().astimezone(UTC)
    if deps.maintenance_state is not None:
        deps.maintenance_state.record_started()
    stats: dict[str, Any] = {
        "watchlist_routes": 0,
        "schedule_ready_routes": 0,
        "open_routes": 0,
        "closed_routes": 0,
        "continuous_routes": 0,
        "unknown_routes": 0,
        "quality_checks": 0,
        "repair_candidates": 0,
        "scheduled_repairs": 0,
        "running_repairs": 0,
        "throttled_repairs": 0,
        "deferred_repairs": 0,
        "safety_deferred_repairs": 0,
        "budget_deferred_repairs": 0,
        "circuit_deferred_repairs": 0,
        "cached_verified_schedules": 0,
        "schedule_fetch_attempts": 0,
        "schedule_fetch_successes": 0,
        "schedule_fetch_failures": 0,
        "schedule_fetch_deferred": 0,
        "schedule_safety_deferred": 0,
        "schedule_cycle_deferred": 0,
        "schedule_retry_deferred": 0,
        "skipped_full_history": 0,
        "fail_closed_checks": 0,
        "failures": 0,
        "work_remaining": False,
        "schedule_errors": [],
        "provider_admissions": {},
        "provider_block_reasons": {},
        "provider_schedule_admissions": {},
        "provider_schedule_block_reasons": {},
        "provider_safety": {},
    }
    provider_cycle_requests: dict[str, int] = {}
    store = deps.store_factory()
    if store is None:
        stats["failures"] += 1
        return _finish_maintenance_cycle(deps, started, "storage_unavailable", stats)
    if deps.server_sleeping():
        return _finish_maintenance_cycle(deps, started, "sleeping", stats)
    try:
        instruments = await run_physical_thread_call(deps.selected_instruments)
    except Exception as exc:
        deps.logger_warning("trading hours watchlist unavailable: %s", exc)
        stats["failures"] += 1
        return _finish_maintenance_cycle(deps, started, "watchlist_error", stats)
    active_instrument_ids: set[str] = set()
    if deps.active_chart_streams is not None:
        try:
            active_streams = await run_physical_thread_call(deps.active_chart_streams)
            active_instrument_ids = {
                require_exact_identity_text(stream[0], field="active_stream_instrument_id")
                for stream in active_streams
                if isinstance(stream, tuple) and len(stream) == 4
            }
        except Exception as exc:
            deps.logger_warning("active chart schedule demand unavailable: %s", exc)
    pending_by_provider: dict[str, set[tuple[str, str]]] = {}
    seen: set[tuple[str, str, str, str]] = set()
    watchlist_provider_order: dict[str, int] = {}
    schedule_ready_routes: list[InstrumentRoute] = []
    schedule_candidates_by_provider: dict[
        str,
        list[tuple[tuple[str, str], InstrumentRoute, str, bool]],
    ] = {}
    schedule_provider_order: list[str] = []
    for instrument in instruments:
        try:
            route = route_instrument(instrument)
        except Exception as exc:
            deps.logger_warning("trading hours instrument route invalid: %s", exc)
            stats["failures"] += 1
            continue
        stats["watchlist_routes"] += 1
        provider = route.provider
        watchlist_provider_order.setdefault(provider, len(watchlist_provider_order))
        if route.adapter.continuous_session(route.instrument):
            seen_key = (
                provider,
                route.instrument_id,
                route.fingerprint,
                "continuous_session",
            )
            if seen_key not in seen:
                seen.add(seen_key)
                schedule_ready_routes.append(route)
                stats["schedule_ready_routes"] += 1
            continue
        if not route.adapter.capabilities.trading_hours:
            stats["unknown_routes"] += 1
            stats["fail_closed_checks"] += 1
            continue
        if futures_root(instrument) and current_contract_is_stale(instrument):
            resolver = deps.resolve_current_futures_contract
            if resolver is None:
                deps.logger_warning(
                    "%s current futures contract unresolved for %s: lifecycle resolver unavailable",
                    provider.upper(),
                    instrument_key(instrument) or "unknown",
                )
                stats["failures"] += 1
                stats["unknown_routes"] += 1
                stats["fail_closed_checks"] += 1
                continue
            try:
                previous_route_fingerprint = route.fingerprint
                instrument = await run_physical_thread_call(resolver, store, route.instrument)
                route = route_instrument(instrument, expected_source=provider)
                if route.fingerprint != previous_route_fingerprint:
                    if deps.refresh_quote_routes is None:
                        raise RuntimeError("QUOTE_ROUTE_REFRESH_REQUIRED")
                    await run_physical_thread_call(
                        deps.refresh_quote_routes,
                        store,
                        route_generation_changed=True,
                    )
            except Exception as exc:
                deps.logger_warning(
                    "%s current futures contract refresh failed for %s: %s",
                    provider.upper(),
                    instrument_key(instrument) or "unknown",
                    exc,
                )
                stats["failures"] += 1
                stats["unknown_routes"] += 1
                stats["fail_closed_checks"] += 1
                continue
        pending = pending_by_provider.setdefault(
            provider,
            set(peek_pending_provider_trading_hours_refresh(provider)),
        )
        display_key = instrument_key(instrument)
        route_key = route.fingerprint
        identity_key = (route.instrument_id, route_key)
        provider_contract_id = route.adapter.session_contract_id(route.instrument)
        if not provider_contract_id:
            deps.logger_warning(
                "%s trading hours refresh skipped for %s: missing provider contract id",
                provider.upper(),
                display_key or route.instrument_id,
            )
            stats["failures"] += 1
            stats["unknown_routes"] += 1
            stats["fail_closed_checks"] += 1
            continue
        seen_key = (
            provider,
            route.instrument_id,
            route_key,
            provider_contract_id,
        )
        if seen_key in seen:
            continue
        seen.add(seen_key)
        force_refresh = identity_key in pending
        if provider not in schedule_candidates_by_provider:
            schedule_provider_order.append(provider)
            schedule_candidates_by_provider[provider] = []
        schedule_candidates_by_provider[provider].append(
            (
                (route.instrument_id, route.fingerprint),
                route,
                provider_contract_id,
                force_refresh,
            )
        )

    ordered_schedule_candidates: list[tuple[tuple[str, str], InstrumentRoute, str, bool]] = []
    maintenance_state = deps.maintenance_state
    for provider in schedule_provider_order:
        provider_candidates = sorted(
            schedule_candidates_by_provider[provider],
            key=lambda item: item[0],
        )
        forced_candidates = [item for item in provider_candidates if item[3]]
        active_candidates = [
            item
            for item in provider_candidates
            if not item[3] and item[1].instrument_id in active_instrument_ids
        ]
        background_candidates = [
            item
            for item in provider_candidates
            if not item[3] and item[1].instrument_id not in active_instrument_ids
        ]
        if maintenance_state is not None and background_candidates:
            candidate_keys = tuple(item[0] for item in background_candidates)
            start = maintenance_state.schedule_cycle_start_index(
                provider,
                candidate_keys,
            )
            background_candidates = background_candidates[start:] + background_candidates[:start]
        ordered_schedule_candidates.extend(
            forced_candidates + active_candidates + background_candidates
        )

    provider_schedule_admissions: dict[str, int] = stats["provider_schedule_admissions"]
    for candidate_key, route, provider_contract_id, force_refresh in ordered_schedule_candidates:
        provider = route.provider
        safety_gate = (
            maintenance_state.provider_admission_gate(provider)
            if maintenance_state is not None
            else {"admit": True, "blocker_reason": ""}
        )
        safety_reason = str(safety_gate.get("blocker_reason") or "")
        retry_remaining = (
            maintenance_state.provider_schedule_retry_remaining(provider)
            if maintenance_state is not None
            else 0.0
        )
        max_provider_cycle_requests = max(
            int(deps.maintenance_settings.max_provider_admissions_per_cycle),
            1,
        )
        if safety_reason:
            provider_fetch_blocker = safety_reason
        elif retry_remaining > 0:
            provider_fetch_blocker = "provider_schedule_retry_not_before"
        elif provider_cycle_requests.get(provider, 0) >= max_provider_cycle_requests:
            provider_fetch_blocker = "provider_cycle_limit"
        elif (
            provider_schedule_admissions.get(provider, 0)
            >= _MAX_SCHEDULE_FETCH_ATTEMPTS_PER_PROVIDER_CYCLE
        ):
            provider_fetch_blocker = "provider_schedule_cycle_limit"
        else:
            provider_fetch_blocker = ""
        try:
            schedule_result = await _refresh_instrument_trading_hours(
                deps,
                store,
                provider_contract_id,
                route,
                force_refresh=force_refresh,
                require_chart_axis=route.instrument_id in active_instrument_ids,
                required_at=checked_at,
                provider_fetch_blocker=provider_fetch_blocker,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats["schedule_errors"].append(
                {
                    "provider": provider,
                    "instrument_id": route.instrument_id,
                    "route_fingerprint": route.fingerprint,
                    "provider_contract_id": provider_contract_id,
                    "status": "refresh_exception",
                    "message": str(exc) or exc.__class__.__name__,
                }
            )
            deps.logger_warning(
                "%s trading hours refresh failed for %s: %s",
                provider.upper(),
                route.instrument_key or route.instrument_id,
                exc,
            )
            stats["failures"] += 1
            stats["unknown_routes"] += 1
            stats["fail_closed_checks"] += 1
            continue
        fetch_started = schedule_result.get("fetch_started") is True
        provider_result = schedule_result.get("provider_result")
        schedule_state = str(schedule_result.get("schedule_state") or "unknown")
        chart_axis_required = schedule_result.get("chart_axis_required") is True
        chart_axis_state = str(schedule_result.get("chart_axis_state") or "not_required")
        result_status = str(schedule_result.get("status") or "unknown")
        deferred_reason = str(schedule_result.get("deferred_reason") or "")
        result_error = str(schedule_result.get("error") or "")
        if result_error:
            stats["schedule_errors"].append(
                {
                    "provider": provider,
                    "instrument_id": route.instrument_id,
                    "route_fingerprint": route.fingerprint,
                    "provider_contract_id": provider_contract_id,
                    "status": result_status,
                    "message": result_error,
                }
            )
            deps.logger_warning(
                "%s trading schedule %s for %s (%s): %s",
                provider.upper(),
                result_status,
                route.instrument_key or route.instrument_id,
                route.fingerprint,
                result_error,
            )
        refresh_superseded = result_status == "refresh_superseded"
        if chart_axis_required and chart_axis_state != "verified":
            stats["work_remaining"] = True
        if provider_result is True:
            pending_by_provider.get(provider, set()).discard(candidate_key)
        if fetch_started:
            stats["schedule_fetch_attempts"] += 1
            provider_schedule_admissions[provider] = (
                provider_schedule_admissions.get(provider, 0) + 1
            )
            provider_cycle_requests[provider] = provider_cycle_requests.get(provider, 0) + 1
            if maintenance_state is not None:
                maintenance_state.record_provider_request(provider)
                maintenance_state.advance_schedule_cursor(provider, candidate_key)
            if refresh_superseded:
                stats["work_remaining"] = True
                if maintenance_state is not None:
                    maintenance_state.record_provider_request_completion(
                        provider,
                        ProviderRequestCompletionOutcome.NEUTRAL,
                    )
                    maintenance_state.clear_provider_schedule_retry(provider)
            elif provider_result is True and schedule_state == "verified":
                stats["schedule_fetch_successes"] += 1
                if maintenance_state is not None:
                    maintenance_state.record_provider_request_completion(
                        provider,
                        ProviderRequestCompletionOutcome.SUCCESS,
                    )
                    maintenance_state.clear_provider_schedule_retry(provider)
            else:
                stats["schedule_fetch_failures"] += 1
                stats["failures"] += 1
                stats["work_remaining"] = True
                if maintenance_state is not None:
                    maintenance_state.record_provider_request_completion(
                        provider,
                        ProviderRequestCompletionOutcome.FAILURE,
                    )
                    maintenance_state.record_provider_schedule_retry(
                        provider,
                        _BACKGROUND_SCHEDULE_RETRY_SECONDS,
                    )
        elif refresh_superseded:
            stats["work_remaining"] = True
        elif schedule_state == "verified":
            stats["cached_verified_schedules"] += 1
        elif deferred_reason:
            stats["schedule_fetch_deferred"] += 1
            stats["provider_schedule_block_reasons"][provider] = deferred_reason
            if deferred_reason in {
                "provider_cycle_limit",
                "provider_schedule_cycle_limit",
            }:
                stats["schedule_cycle_deferred"] += 1
                stats["work_remaining"] = True
            elif deferred_reason == "provider_schedule_retry_not_before":
                stats["schedule_retry_deferred"] += 1
                stats["work_remaining"] = True
            elif deferred_reason in {
                "provider_window_budget_exhausted",
                "provider_circuit_open",
            }:
                stats["schedule_safety_deferred"] += 1
        else:
            retry_after = float(schedule_result.get("retry_after_seconds") or 0.0)
            if retry_after > 0 and maintenance_state is not None:
                maintenance_state.record_provider_schedule_retry(
                    provider,
                    retry_after,
                )
        if schedule_state != "verified":
            stats["unknown_routes"] += 1
            stats["fail_closed_checks"] += 1
            increment_metric(
                "closed_session_maintenance_admission_total",
                provider=provider,
                interval="schedule",
                status=result_status,
                reason=deferred_reason or "current_session_unverified",
            )
            continue
        schedule_ready_routes.append(route)
        stats["schedule_ready_routes"] += 1

    schedule_ready_routes.sort(
        key=lambda route: (
            watchlist_provider_order.get(route.provider, len(watchlist_provider_order)),
            route.instrument_id,
            route.fingerprint,
        )
    )
    status = "ok" if deps.maintenance_settings.enabled else "maintenance_disabled"
    if stats["failures"]:
        status = f"{status}_with_errors"
    return _finish_maintenance_cycle(deps, started, status, stats)


def _finish_maintenance_cycle(
    deps: TradingHoursRefreshDeps,
    started: float,
    status: str,
    stats: dict[str, Any],
) -> ClosedSessionMaintenanceCycle:
    duration = max(perf_counter() - started, 0.0)
    if deps.maintenance_state is not None:
        stats["provider_safety"] = deps.maintenance_state.provider_safety_snapshot()
    cycle = ClosedSessionMaintenanceCycle(
        status=status,
        checked_at=datetime.now(tz=UTC).isoformat(),
        duration_seconds=duration,
        **stats,
    )
    if deps.maintenance_state is not None:
        deps.maintenance_state.record_completed(cycle)
    increment_metric("closed_session_maintenance_cycles_total", status=status)
    observe_metric("closed_session_maintenance_cycle_seconds", duration, status=status)
    set_metric("closed_session_maintenance_closed_routes", cycle.closed_routes)
    set_metric("closed_session_maintenance_repair_candidates", cycle.repair_candidates)
    set_metric("closed_session_maintenance_scheduled_repairs", cycle.scheduled_repairs)
    return cycle


async def _refresh_instrument_trading_hours(
    deps: TradingHoursRefreshDeps,
    store: Any,
    provider_contract_id: str,
    route: InstrumentRoute,
    *,
    force_refresh: bool,
    required_at: datetime,
    provider_fetch_blocker: str,
    require_chart_axis: bool = False,
) -> dict[str, Any]:
    """Verify current provider-owned session state and conditionally refresh it."""

    provider = route.provider
    display_key = instrument_key(route.instrument) or route.instrument_id
    if not provider_contract_id:
        deps.logger_warning(
            "%s trading hours refresh skipped for %s: missing provider contract id",
            provider.upper(),
            display_key,
        )
        return {
            "status": "missing_provider_contract",
            "schedule_state": "unknown",
            "fetch_started": False,
            "provider_result": None,
        }
    cached = await run_physical_thread_call(
        store.read_trading_hours,
        instrument=route.instrument,
    )
    probe_start = interval_bucket(required_at, "1m")
    probe_end = probe_start + timedelta(minutes=1)
    cached_schedule_state = "unknown"
    chart_axis_probe = _ChartAxisScheduleProbe("unknown" if require_chart_axis else "not_required")
    cached_is_fresh = bool(cached and float(cached.get("age_seconds") or 0.0) < 24 * 60 * 60)
    if cached_is_fresh:
        try:
            cached_coverage: ProviderSessionBarSlotCoverage = await run_physical_thread_call(
                provider_session_bar_slot_coverage_between,
                probe_start,
                probe_end,
                "1m",
                store=store,
                instrument=route.instrument,
                limit=1,
            )
        except Exception:
            cached_schedule_state = "unknown"
        else:
            cached_schedule_state = cached_coverage.schedule_state
    if require_chart_axis:
        try:
            chart_axis_probe = await _probe_chart_axis_schedule(
                store,
                route,
                required_until=probe_end,
            )
        except Exception:
            chart_axis_probe = _ChartAxisScheduleProbe("unknown")
    if (
        not force_refresh
        and cached_is_fresh
        and cached_schedule_state == "verified"
        and chart_axis_probe.schedule_state in {"verified", "not_required"}
    ):
        await _publish_runtime_trading_schedule(deps, store, route)
        return {
            "status": "cached_verified",
            "schedule_state": "verified",
            "chart_axis_required": require_chart_axis,
            "chart_axis_state": chart_axis_probe.schedule_state,
            "fetch_started": False,
            "provider_result": None,
        }

    fetch_start = probe_start
    if chart_axis_probe.repair_from is not None:
        fetch_start = min(fetch_start, chart_axis_probe.repair_from)
    fetch_end = probe_start + timedelta(days=7)
    fetch_key = _trading_hours_fetch_key(
        route,
        starts_at=fetch_start,
        ends_at=fetch_end,
    )
    observed_at = monotonic()
    running = _trading_hours_fetch_tasks.get(fetch_key)
    shared_running = running is not None and not running.done()
    shared_cached = _trading_hours_fetch_results.get(fetch_key)
    shared_cached_valid = bool(shared_cached is not None and observed_at < shared_cached[0])
    fetch_started = bool(not shared_running and (force_refresh or not shared_cached_valid))
    if fetch_started and provider_fetch_blocker:
        return {
            "status": (
                "schedule_axis_fetch_deferred"
                if cached_schedule_state == "verified" and require_chart_axis
                else "schedule_fetch_deferred"
            ),
            "schedule_state": cached_schedule_state,
            "chart_axis_required": require_chart_axis,
            "chart_axis_state": chart_axis_probe.schedule_state,
            "fetch_started": False,
            "provider_result": None,
            "deferred_reason": provider_fetch_blocker,
        }
    if fetch_started and force_refresh:
        _trading_hours_fetch_results.pop(fetch_key, None)
    try:
        refreshed = await fetch_and_store_trading_hours(
            store,
            route,
            timeout=5.0,
            logger_warning=deps.logger_warning,
            starts_at=fetch_start,
            ends_at=fetch_end,
        )
    except asyncio.CancelledError:
        raise
    except TradingHoursRefreshSuperseded:
        return {
            "status": "refresh_superseded",
            "schedule_state": "unknown",
            "chart_axis_required": require_chart_axis,
            "chart_axis_state": chart_axis_probe.schedule_state,
            "fetch_started": fetch_started,
            "provider_result": None,
            "retry_after_seconds": 0.0,
        }
    except Exception as exc:
        return {
            "status": "schedule_fetch_exception" if fetch_started else "shared_fetch_exception",
            "schedule_state": "unknown",
            "chart_axis_required": require_chart_axis,
            "chart_axis_state": chart_axis_probe.schedule_state,
            "fetch_started": fetch_started,
            "provider_result": None,
            "error": str(exc) or exc.__class__.__name__,
            "retry_after_seconds": _TRADING_HOURS_FETCH_FAILURE_BACKOFF_SECONDS,
        }
    if refreshed:
        await _publish_runtime_trading_schedule(deps, store, route)
    try:
        refreshed_coverage: ProviderSessionBarSlotCoverage = await run_physical_thread_call(
            provider_session_bar_slot_coverage_between,
            probe_start,
            probe_end,
            "1m",
            store=store,
            instrument=route.instrument,
            limit=1,
        )
        schedule_state = refreshed_coverage.schedule_state
    except Exception as exc:
        schedule_state = "unknown"
        schedule_error = str(exc) or exc.__class__.__name__
    else:
        schedule_error = ""
    if require_chart_axis:
        try:
            chart_axis_probe = await _probe_chart_axis_schedule(
                store,
                route,
                required_until=probe_end,
            )
        except Exception as exc:
            chart_axis_probe = _ChartAxisScheduleProbe("unknown")
            if not schedule_error:
                schedule_error = str(exc) or exc.__class__.__name__
    if fetch_started and schedule_state != "verified":
        _trading_hours_fetch_results.pop(fetch_key, None)
    shared_deadline = _trading_hours_fetch_results.get(fetch_key, (0.0, False))[0]
    retry_after_seconds = max(float(shared_deadline) - monotonic(), 0.0)
    if fetch_started:
        if (
            refreshed
            and schedule_state == "verified"
            and chart_axis_probe.schedule_state in {"verified", "not_required"}
        ):
            status = "schedule_fetch_verified"
        elif refreshed and schedule_state == "verified":
            status = "schedule_fetch_verified_axis_unknown"
        elif not refreshed:
            status = "schedule_fetch_no_progress"
        else:
            status = "schedule_fetch_current_session_unknown"
    elif schedule_state == "verified":
        status = (
            "shared_schedule_verified"
            if chart_axis_probe.schedule_state in {"verified", "not_required"}
            else "shared_schedule_verified_axis_unknown"
        )
    else:
        status = "shared_schedule_unknown"
    return {
        "status": status,
        "schedule_state": schedule_state,
        "chart_axis_required": require_chart_axis,
        "chart_axis_state": chart_axis_probe.schedule_state,
        "fetch_started": fetch_started,
        "provider_result": bool(refreshed),
        "retry_after_seconds": retry_after_seconds,
        "error": schedule_error,
    }


async def _publish_runtime_trading_schedule(
    deps: TradingHoursRefreshDeps,
    store: Any,
    route: InstrumentRoute,
) -> None:
    """Publish committed schedule facts to route and analysis read models."""

    if deps.refresh_quote_routes is None and deps.invalidate_market_analysis_route is None:
        return
    if deps.refresh_quote_routes is None:
        raise RuntimeError("TRADING_SCHEDULE_RUNTIME_ROUTE_REFRESH_REQUIRED")
    await run_physical_thread_call(
        deps.refresh_quote_routes,
        store,
        route_generation_changed=True,
    )
    if deps.invalidate_market_analysis_route is None:
        raise RuntimeError("TRADING_SCHEDULE_ANALYSIS_INVALIDATION_REQUIRED")
    await deps.invalidate_market_analysis_route(
        route.instrument_id,
        route.fingerprint,
    )


async def fetch_and_store_trading_hours(
    store: Any,
    route: InstrumentRoute,
    *,
    timeout: float = 5.0,
    logger_warning: Callable[..., None] | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> bool:
    """Single-flight fetch and persist one exact provider-owned route schedule."""

    key = _trading_hours_fetch_key(
        route,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    now = monotonic()
    cached = _trading_hours_fetch_results.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    _trading_hours_fetch_results.pop(key, None)
    task = _trading_hours_fetch_tasks.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            _fetch_and_store_trading_hours_once(
                store,
                route,
                timeout=timeout,
                logger_warning=logger_warning,
                starts_at=starts_at,
                ends_at=ends_at,
            ),
            name=f"trading-hours-refresh:{route.provider}:{route.instrument_id}",
        )
        _trading_hours_fetch_tasks[key] = task

        def finish(completed: asyncio.Task[bool]) -> None:
            if _trading_hours_fetch_tasks.get(key) is completed:
                _trading_hours_fetch_tasks.pop(key, None)
            if completed.cancelled():
                return
            try:
                completed_result = completed.result()
            except TradingHoursRefreshSuperseded:
                _trading_hours_fetch_results.pop(key, None)
            except Exception:
                _trading_hours_fetch_results[key] = (
                    monotonic() + _TRADING_HOURS_FETCH_FAILURE_BACKOFF_SECONDS,
                    False,
                )
            else:
                _trading_hours_fetch_results[key] = (
                    monotonic() + _TRADING_HOURS_FETCH_COOLDOWN_SECONDS,
                    bool(completed_result),
                )

        task.add_done_callback(finish)
    try:
        result = await await_cancellation_deferred_task(
            task,
            task_cancelled_error="TRADING_HOURS_FETCH_TASK_CANCELLED",
        )
    except asyncio.CancelledError:
        raise
    except TradingHoursRefreshSuperseded:
        _trading_hours_fetch_results.pop(key, None)
        raise
    except Exception:
        _trading_hours_fetch_results[key] = (
            monotonic() + _TRADING_HOURS_FETCH_FAILURE_BACKOFF_SECONDS,
            False,
        )
        raise
    _trading_hours_fetch_results[key] = (
        monotonic() + _TRADING_HOURS_FETCH_COOLDOWN_SECONDS,
        bool(result),
    )
    return bool(result)


async def _fetch_and_store_trading_hours_once(
    store: Any,
    route: InstrumentRoute,
    *,
    timeout: float,
    logger_warning: Callable[..., None] | None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> bool:
    """Execute the canonical provider fetch/store operation for one route."""

    provider = route.provider
    display_key = instrument_key(route.instrument) or route.instrument_id
    provider_contract_id = route.adapter.session_contract_id(route.instrument)
    if not provider_contract_id:
        if logger_warning is not None:
            logger_warning(
                "%s trading hours refresh skipped for %s: missing provider contract id",
                provider.upper(),
                display_key,
            )
        return False
    schedule_id = require_exact_identity_text(
        provider_contract_id,
        field="provider_contract_id",
    )
    refresh_generation = provider_trading_hours_refresh_generation(
        provider,
        route.instrument_id,
        route.fingerprint,
    )
    hours = await route.adapter.fetch_trading_schedule(
        route.instrument,
        timeout=max(float(timeout), 0.1),
        starts_at=starts_at,
        ends_at=ends_at,
    )
    if (
        provider_trading_hours_refresh_generation(
            provider,
            route.instrument_id,
            route.fingerprint,
        )
        != refresh_generation
    ):
        raise TradingHoursRefreshSuperseded("TRADING_HOURS_REFRESH_GENERATION_CHANGED")
    trading_hours = str(hours.get("trading_hours") or "")
    liquid_hours = str(hours.get("liquid_hours") or "")
    has_provider_intervals = bool(hours.get("trading_intervals"))
    if not trading_hours and not liquid_hours and not has_provider_intervals:
        if logger_warning is not None:
            logger_warning(
                "%s trading hours refresh skipped for %s: empty schedule",
                provider.upper(),
                display_key,
            )
        return False
    payload = dict(hours)
    payload["provider_contract_id"] = schedule_id
    await run_physical_thread_call(
        store.upsert_trading_hours,
        str(hours.get("time_zone") or ""),
        trading_hours,
        liquid_hours,
        payload,
        instrument=route.instrument,
    )
    invalidate_provider_trading_hours_cache(instrument=route.instrument)
    acknowledge_provider_trading_hours_refresh(
        provider,
        route.instrument_id,
        route.fingerprint,
        expected_generation=refresh_generation,
    )
    if (
        provider_trading_hours_refresh_generation(
            provider,
            route.instrument_id,
            route.fingerprint,
        )
        != refresh_generation
    ):
        raise TradingHoursRefreshSuperseded("TRADING_HOURS_REFRESH_GENERATION_CHANGED")
    return True


async def run_trading_hours_refresh_loop(
    deps: TradingHoursRefreshDeps,
    *,
    startup_delay_seconds: float = 2.0,
) -> None:
    await asyncio.sleep(max(float(startup_delay_seconds), 0.0))
    while True:
        cycle: ClosedSessionMaintenanceCycle | None = None
        try:
            cycle = await refresh_trading_hours_once(deps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if deps.maintenance_state is not None:
                deps.maintenance_state.record_error(exc)
            deps.logger_warning("trading hours refresh loop failed: %s", exc)
        active = bool(
            cycle.work_remaining
            if cycle is not None
            else pending_provider_trading_hours_refresh_count() > 0
        )
        delay_seconds = (
            deps.maintenance_settings.active_poll_seconds
            if active
            else deps.maintenance_settings.idle_poll_seconds
        )
        delay_seconds = max(float(delay_seconds), 1.0)
        wakeup_event = deps.wakeup_event
        if wakeup_event is None:
            await asyncio.sleep(max(delay_seconds, 0.1))
            continue
        try:
            await asyncio.wait_for(
                wakeup_event.wait(),
                timeout=max(delay_seconds, 0.1),
            )
        except TimeoutError:
            continue
        wakeup_event.clear()
