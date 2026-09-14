from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.provider_contract import (
    HistoryRepairAdmission,
    HistoryRepairPriority,
    HistoryRequestUnsupportedError,
)
from aef_terminal.data.provider_sessions import provider_session_bar_slot_coverage_between
from aef_terminal.data.providers import read_recent_provider_bars
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.data_quality import data_quality_report
from aef_terminal.runtime.chart_events import chart_bars_updated_generation
from aef_terminal.runtime.timeframes import (
    history_lookback_window,
    interval_bucket,
    interval_seconds,
)
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.services.chart_stream_gap_repair import (
    history_repair_outcome_payload,
    latest_chart_history_repair_outcome,
    schedule_chart_cached_gap_repair,
    select_chart_history_repair,
)


WATCHLIST_HISTORY_MAINTENANCE_MAX_DAYS = 31
WATCHLIST_RECOVERY_TAIL_MAX_SPAN = timedelta(days=1)
_STORE_FACTORY: Callable[[], Any] | None = None


def configure_watchlist_gap_repair(*, store_factory: Callable[[], Any]) -> None:
    global _STORE_FACTORY
    _STORE_FACTORY = store_factory


def _store() -> Any | None:
    return _STORE_FACTORY() if _STORE_FACTORY is not None else None


def watchlist_gap_quality(
    instrument_id: str,
    interval: str,
) -> dict[str, Any]:
    return _watchlist_gap_quality_for_instrument(
        lookup_runtime_instrument(instrument_id),
        interval,
    )


def request_watchlist_recovery_tail(
    instrument_id: str,
    interval: str,
) -> dict[str, Any]:
    """Request one bounded tail refresh after an explicit provider reconnect."""

    return _watchlist_gap_quality_for_instrument(
        lookup_runtime_instrument(instrument_id),
        interval,
        explicit_recovery_tail=True,
    )


def _repair_payload(
    admission: HistoryRepairAdmission | None,
    status: str,
    *,
    terminal_outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if terminal_outcome is not None:
        return {
            "phase": "terminal",
            "status": str(terminal_outcome["status"]),
            "code": terminal_outcome.get("code"),
            "newly_admitted": False,
            "retry_after_seconds": 0.0,
            "requested_from": terminal_outcome["requested_from"],
            "requested_to": terminal_outcome["requested_to"],
            "target_from": terminal_outcome["target_from"],
            "target_to": terminal_outcome["target_to"],
            "remaining_from": terminal_outcome["remaining_from"],
            "remaining_to": terminal_outcome["remaining_to"],
            "priority": None,
            "terminal_outcome": dict(terminal_outcome),
        }
    if admission is None:
        return {
            "phase": "idle",
            "status": status,
            "newly_admitted": False,
            "retry_after_seconds": 0.0,
            "target_from": None,
            "target_to": None,
            "remaining_from": None,
            "remaining_to": None,
        }
    return {
        "phase": "admission",
        "status": admission.status.value,
        "newly_admitted": admission.newly_admitted,
        "retry_after_seconds": admission.retry_after_seconds,
        "requested_from": admission.intent.starts_at.isoformat(),
        "requested_to": admission.intent.ends_at.isoformat(),
        "target_from": admission.intent.target_starts_at.isoformat(),
        "target_to": admission.intent.target_ends_at.isoformat(),
        "remaining_from": admission.intent.target_starts_at.isoformat(),
        "remaining_to": admission.intent.ends_at.isoformat(),
        "priority": admission.intent.priority.name.lower(),
    }


def _current_tail_quality(
    *,
    route: Any,
    interval: str,
    window: Any,
    store: Any,
) -> tuple[dict[str, Any], Any | None]:
    recent = read_recent_provider_bars(
        route,
        interval,
        store=store,
        limit=1,
    )
    latest = max(
        (bar for bar in recent if bar.closed is not False),
        key=lambda bar: bar.ts,
        default=None,
    )
    if latest is None:
        return (
            {
                "status": "empty",
                "signals_ok": False,
                "empty_history": True,
            },
            None,
        )
    return (
        data_quality_report(
            [latest],
            interval,
            now_utc=window.ends_at,
            store=store,
            instrument=route.instrument,
        ),
        latest,
    )


def _unsupported_provider_quality(
    *,
    route: Any,
    interval: str,
    window: Any,
    store: Any,
    error_code: str = "PROVIDER_HISTORY_REPAIR_UNAVAILABLE",
) -> dict[str, Any]:
    quality, _latest = _current_tail_quality(
        route=route,
        interval=interval,
        window=window,
        store=store,
    )
    return {
        **quality,
        "history_coverage": {
            "state": "unsupported",
            "requested_from": window.starts_at.isoformat(),
            "requested_to": window.ends_at.isoformat(),
            "provider_range_complete": False,
            "bar_repair": {
                "supported": False,
                "phase": "idle",
                "status": "not_supported",
                "error_code": error_code,
                "requested_from": window.starts_at.isoformat(),
                "requested_to": window.ends_at.isoformat(),
            },
            "absence_verification": {
                "supported": False,
                "state": "unsupported",
                "error_code": error_code,
                "requested_from": window.starts_at.isoformat(),
                "requested_to": window.ends_at.isoformat(),
            },
            "repair": _repair_payload(None, "not_supported"),
        },
    }


def _provider_recovery_tail_range(
    *,
    route: Any,
    interval: str,
    window: Any,
    latest: Any | None,
    store: Any,
) -> tuple[datetime, datetime] | None:
    """Select elapsed provider-open tail slots within one bounded reconnect envelope."""

    step = timedelta(seconds=interval_seconds(interval))
    ends_at = interval_bucket(window.ends_at.astimezone(UTC), interval)
    starts_at = max(
        window.starts_at.astimezone(UTC),
        ends_at - WATCHLIST_RECOVERY_TAIL_MAX_SPAN,
        (
            latest.ts.astimezone(UTC) + step
            if latest is not None
            else ends_at - WATCHLIST_RECOVERY_TAIL_MAX_SPAN
        ),
    )
    if starts_at >= ends_at:
        return None
    slot_limit = int((ends_at - starts_at).total_seconds() // step.total_seconds()) + 1
    try:
        schedule = provider_session_bar_slot_coverage_between(
            starts_at,
            ends_at,
            interval,
            store=store,
            instrument=route.instrument,
            limit=slot_limit,
        )
    except Exception:
        schedule = None
    if schedule is None or schedule.schedule_state != "verified" or schedule.truncated:
        return starts_at, ends_at
    expected_slots = tuple(
        sorted(
            slot.astimezone(UTC)
            for slot in schedule.expected_slots
            if isinstance(slot, datetime)
            and slot.tzinfo is not None
            and slot.utcoffset() is not None
            and starts_at <= slot.astimezone(UTC) < ends_at
        )
    )
    if not expected_slots:
        return None
    return expected_slots[0], min(expected_slots[-1] + step, ends_at)


def _watchlist_gap_quality_for_instrument(
    instrument: dict[str, Any],
    interval: str,
    *,
    explicit_recovery_tail: bool = False,
) -> dict[str, Any]:
    route = route_instrument(instrument)
    now = datetime.now(tz=UTC)
    window = history_lookback_window(
        WATCHLIST_HISTORY_MAINTENANCE_MAX_DAYS,
        interval,
        now=now,
    )
    assert window.starts_at is not None
    store = _store()
    if store is None:
        return {
            "status": "storage_unavailable",
            "signals_ok": False,
            "history_coverage": {
                "state": "unknown",
                "requested_from": window.starts_at.isoformat(),
                "requested_to": window.ends_at.isoformat(),
                "provider_range_complete": False,
                "bar_repair": {
                    "supported": False,
                    "phase": "idle",
                    "status": "storage_unavailable",
                    "error_code": "HISTORY_REPAIR_STORAGE_UNAVAILABLE",
                    "requested_from": window.starts_at.isoformat(),
                    "requested_to": window.ends_at.isoformat(),
                },
                "absence_verification": {
                    "supported": False,
                    "state": "unknown",
                    "error_code": "HISTORY_COVERAGE_STORAGE_UNAVAILABLE",
                    "requested_from": window.starts_at.isoformat(),
                    "requested_to": window.ends_at.isoformat(),
                },
                "repair": _repair_payload(None, "storage_unavailable"),
            },
        }
    quality, latest = _current_tail_quality(
        route=route,
        interval=interval,
        window=window,
        store=store,
    )
    if not route.adapter.capabilities.gap_repair:
        return _unsupported_provider_quality(
            route=route,
            interval=interval,
            window=window,
            store=store,
        )

    try:
        route.adapter.history_request_identity(route.instrument)
    except HistoryRequestUnsupportedError as exc:
        return _unsupported_provider_quality(
            route=route,
            interval=interval,
            window=window,
            store=store,
            error_code=exc.code,
        )
    generation = chart_bars_updated_generation(
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    admission: HistoryRepairAdmission | None = None
    intent = None
    tail_statuses = set(str(quality.get("status") or "").split("+"))
    normal_tail_repair_needed = bool(
        (latest is None or "stale" in tail_statuses)
        and not quality.get("market_closed")
        and not quality.get("session_warmup")
    )
    recovery_range = (
        _provider_recovery_tail_range(
            route=route,
            interval=interval,
            window=window,
            latest=latest,
            store=store,
        )
        if explicit_recovery_tail
        else None
    )
    repair_needed = bool(normal_tail_repair_needed or recovery_range is not None)
    repair_status = "not_needed"
    if repair_needed:
        repair_status = "stale_snapshot"
        step = timedelta(seconds=interval_seconds(interval))
        requested_from, requested_to = recovery_range or (
            max(
                window.starts_at,
                latest.ts.astimezone(UTC) + step if latest is not None else window.starts_at,
            ),
            window.ends_at,
        )
        if requested_from < requested_to:
            intent = select_chart_history_repair(
                route=route,
                interval=interval,
                requested_from=requested_from,
                requested_to=requested_to,
                canonical_generation=generation,
                priority=HistoryRepairPriority.BACKGROUND,
                store=store,
                classify_small_recent_gap=not explicit_recovery_tail,
            )
        if (
            chart_bars_updated_generation(
                interval,
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            == generation
        ):
            admission = (
                schedule_chart_cached_gap_repair(
                    intent,
                    store_factory=lambda: store,
                    instrument=route.instrument,
                )
                if intent is not None
                else None
            )
            repair_status = "not_needed" if intent is None else "not_admitted"

    exact_snapshot_authority = bool(
        getattr(
            route.adapter.capabilities,
            "exact_history_snapshot_authority",
            False,
        )
    )
    latest_outcome = latest_chart_history_repair_outcome(intent) if intent is not None else None
    terminal_outcome = (
        history_repair_outcome_payload(latest_outcome) if latest_outcome is not None else None
    )
    repair = _repair_payload(
        admission,
        repair_status,
        terminal_outcome=terminal_outcome,
    )
    return {
        **quality,
        "history_coverage": {
            "state": "not_checked",
            "requested_from": window.starts_at.isoformat(),
            "requested_to": window.ends_at.isoformat(),
            "provider_range_complete": False,
            "pending_from": None,
            "pending_to": None,
            "bar_repair": {
                "supported": True,
                "phase": repair["phase"],
                "status": repair["status"],
                "error_code": repair.get("code"),
                "requested_from": repair.get("requested_from"),
                "requested_to": repair.get("requested_to"),
                "target_from": repair.get("target_from"),
                "target_to": repair.get("target_to"),
                "remaining_from": repair.get("remaining_from"),
                "remaining_to": repair.get("remaining_to"),
            },
            "absence_verification": {
                "supported": exact_snapshot_authority,
                "state": "not_checked" if exact_snapshot_authority else "unsupported",
                "error_code": (
                    None
                    if exact_snapshot_authority
                    else "PROVIDER_ABSENCE_VERIFICATION_UNAVAILABLE"
                ),
                "requested_from": window.starts_at.isoformat(),
                "requested_to": window.ends_at.isoformat(),
            },
            "repair": repair,
        },
    }
