from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aef_terminal.runtime.clock import utc_now_iso as _now_iso

CLOSED_SESSION_PRODUCT_INTERVALS = ("1m", "5m", "15m", "60m")
DEFAULT_CLOSED_SESSION_MAINTENANCE_TIMEZONE = "Asia/Jerusalem"
DEFAULT_CLOSED_SESSION_MAINTENANCE_START_LOCAL = "00:05"
DEFAULT_CLOSED_SESSION_MAINTENANCE_END_LOCAL = "01:00"


class ProviderRequestCompletionOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"


def _strict_local_minute(value: str, *, field_name: str) -> int:
    text = str(value)
    parts = text.split(":")
    if (
        len(parts) != 2
        or len(parts[0]) != 2
        or len(parts[1]) != 2
        or not parts[0].isdigit()
        or not parts[1].isdigit()
    ):
        raise ValueError(f"{field_name} must use strict HH:MM format")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour not in range(24) or minute not in range(60):
        raise ValueError(f"{field_name} must be a valid local time")
    return (hour * 60) + minute


@dataclass(frozen=True)
class ClosedSessionMaintenanceSettings:
    enabled: bool = True
    intervals: tuple[str, ...] = CLOSED_SESSION_PRODUCT_INTERVALS
    history_timezone: str = DEFAULT_CLOSED_SESSION_MAINTENANCE_TIMEZONE
    history_start_local: str = DEFAULT_CLOSED_SESSION_MAINTENANCE_START_LOCAL
    history_end_local: str = DEFAULT_CLOSED_SESSION_MAINTENANCE_END_LOCAL
    active_poll_seconds: float = 30.0
    idle_poll_seconds: float = 30.0 * 60.0
    repair_timeout_seconds: float = 30.0
    max_provider_admissions_per_cycle: int = 1
    admission_window_seconds: float = 12.0 * 60.0 * 60.0
    max_provider_admissions_per_window: int = 32
    failure_threshold: int = 3
    breaker_backoff_seconds: float = 6.0 * 60.0 * 60.0

    def __post_init__(self) -> None:
        timezone_name = str(self.history_timezone)
        if not timezone_name or timezone_name != timezone_name.strip():
            raise ValueError("history_timezone must be an exact IANA timezone name")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"history_timezone must be a valid IANA timezone: {timezone_name}"
            ) from exc
        start_minute = _strict_local_minute(
            self.history_start_local,
            field_name="history_start_local",
        )
        end_minute = _strict_local_minute(
            self.history_end_local,
            field_name="history_end_local",
        )
        if start_minute >= end_minute:
            raise ValueError(
                "history maintenance window must end after it starts on the same local day"
            )

    def history_admission_open(self, observed_at: datetime) -> bool:
        local = self._local_observation(observed_at)
        wall_minute = (local.hour * 60) + local.minute
        return self._history_start_minute <= wall_minute < self._history_end_minute

    def seconds_until_history_boundary(self, observed_at: datetime) -> float:
        observed_utc = self._utc_observation(observed_at)
        local = observed_utc.astimezone(self._history_zone)
        start_at, end_at = self._history_window_for_date(local.date())
        if local < start_at:
            boundary = start_at
        elif local < end_at:
            boundary = end_at
        else:
            boundary, _ = self._history_window_for_date(local.date() + timedelta(days=1))
        return max(
            (boundary.astimezone(UTC) - observed_utc).total_seconds(),
            0.0,
        )

    def next_history_start_at(self, observed_at: datetime) -> datetime:
        local = self._local_observation(observed_at)
        start_at, _ = self._history_window_for_date(local.date())
        if local >= start_at:
            start_at, _ = self._history_window_for_date(local.date() + timedelta(days=1))
        return start_at

    @property
    def _history_zone(self) -> ZoneInfo:
        return ZoneInfo(self.history_timezone)

    @property
    def _history_start_minute(self) -> int:
        return _strict_local_minute(
            self.history_start_local,
            field_name="history_start_local",
        )

    @property
    def _history_end_minute(self) -> int:
        return _strict_local_minute(
            self.history_end_local,
            field_name="history_end_local",
        )

    @staticmethod
    def _utc_observation(observed_at: datetime) -> datetime:
        if observed_at.tzinfo is None:
            raise ValueError("history maintenance clock must be timezone-aware")
        return observed_at.astimezone(UTC)

    def _local_observation(self, observed_at: datetime) -> datetime:
        return self._utc_observation(observed_at).astimezone(self._history_zone)

    def _history_window_for_date(
        self,
        local_date: date,
    ) -> tuple[datetime, datetime]:
        zone = self._history_zone
        start_minute = self._history_start_minute
        end_minute = self._history_end_minute
        start_at = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            start_minute // 60,
            start_minute % 60,
            tzinfo=zone,
        )
        end_at = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            end_minute // 60,
            end_minute % 60,
            tzinfo=zone,
        )
        return start_at, end_at

    def as_dict(self) -> dict[str, Any]:
        observed_at = datetime.now(tz=UTC)
        return {
            "enabled": bool(self.enabled),
            "intervals": list(self.intervals),
            "history_timezone": self.history_timezone,
            "history_start_local": self.history_start_local,
            "history_end_local": self.history_end_local,
            "history_admission_open": bool(
                self.enabled and self.history_admission_open(observed_at)
            ),
            "next_history_start_at": self.next_history_start_at(observed_at).isoformat(),
            "active_poll_seconds": max(float(self.active_poll_seconds), 1.0),
            "idle_poll_seconds": max(float(self.idle_poll_seconds), 1.0),
            "repair_timeout_seconds": max(float(self.repair_timeout_seconds), 0.1),
            "max_provider_admissions_per_cycle": max(
                int(self.max_provider_admissions_per_cycle),
                1,
            ),
            "admission_window_seconds": max(
                float(self.admission_window_seconds),
                1.0,
            ),
            "max_provider_admissions_per_window": max(
                int(self.max_provider_admissions_per_window),
                1,
            ),
            "failure_threshold": max(int(self.failure_threshold), 1),
            "breaker_backoff_seconds": max(
                float(self.breaker_backoff_seconds),
                1.0,
            ),
        }


@dataclass(frozen=True)
class ClosedSessionMaintenanceCycle:
    status: str
    checked_at: str
    duration_seconds: float = 0.0
    watchlist_routes: int = 0
    schedule_ready_routes: int = 0
    open_routes: int = 0
    closed_routes: int = 0
    continuous_routes: int = 0
    unknown_routes: int = 0
    quality_checks: int = 0
    repair_candidates: int = 0
    scheduled_repairs: int = 0
    running_repairs: int = 0
    throttled_repairs: int = 0
    deferred_repairs: int = 0
    safety_deferred_repairs: int = 0
    budget_deferred_repairs: int = 0
    circuit_deferred_repairs: int = 0
    cached_verified_schedules: int = 0
    schedule_fetch_attempts: int = 0
    schedule_fetch_successes: int = 0
    schedule_fetch_failures: int = 0
    schedule_fetch_deferred: int = 0
    schedule_safety_deferred: int = 0
    schedule_cycle_deferred: int = 0
    schedule_retry_deferred: int = 0
    skipped_full_history: int = 0
    fail_closed_checks: int = 0
    failures: int = 0
    work_remaining: bool = False
    schedule_errors: list[dict[str, str]] = field(default_factory=list)
    provider_admissions: dict[str, int] = field(default_factory=dict)
    provider_block_reasons: dict[str, str] = field(default_factory=dict)
    provider_schedule_admissions: dict[str, int] = field(default_factory=dict)
    provider_schedule_block_reasons: dict[str, str] = field(default_factory=dict)
    provider_safety: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "duration_seconds": round(max(float(self.duration_seconds), 0.0), 6),
            "watchlist_routes": int(self.watchlist_routes),
            "schedule_ready_routes": int(self.schedule_ready_routes),
            "open_routes": int(self.open_routes),
            "closed_routes": int(self.closed_routes),
            "continuous_routes": int(self.continuous_routes),
            "unknown_routes": int(self.unknown_routes),
            "quality_checks": int(self.quality_checks),
            "repair_candidates": int(self.repair_candidates),
            "scheduled_repairs": int(self.scheduled_repairs),
            "running_repairs": int(self.running_repairs),
            "throttled_repairs": int(self.throttled_repairs),
            "deferred_repairs": int(self.deferred_repairs),
            "safety_deferred_repairs": int(self.safety_deferred_repairs),
            "budget_deferred_repairs": int(self.budget_deferred_repairs),
            "circuit_deferred_repairs": int(self.circuit_deferred_repairs),
            "cached_verified_schedules": int(self.cached_verified_schedules),
            "schedule_fetch_attempts": int(self.schedule_fetch_attempts),
            "schedule_fetch_successes": int(self.schedule_fetch_successes),
            "schedule_fetch_failures": int(self.schedule_fetch_failures),
            "schedule_fetch_deferred": int(self.schedule_fetch_deferred),
            "schedule_safety_deferred": int(self.schedule_safety_deferred),
            "schedule_cycle_deferred": int(self.schedule_cycle_deferred),
            "schedule_retry_deferred": int(self.schedule_retry_deferred),
            "skipped_full_history": int(self.skipped_full_history),
            "fail_closed_checks": int(self.fail_closed_checks),
            "failures": int(self.failures),
            "work_remaining": bool(self.work_remaining),
            "schedule_errors": [dict(error) for error in self.schedule_errors],
            "provider_admissions": dict(self.provider_admissions),
            "provider_block_reasons": dict(self.provider_block_reasons),
            "provider_schedule_admissions": dict(self.provider_schedule_admissions),
            "provider_schedule_block_reasons": dict(self.provider_schedule_block_reasons),
            "provider_safety": {
                provider: dict(safety) for provider, safety in self.provider_safety.items()
            },
        }


@dataclass
class ClosedSessionMaintenanceState:
    settings: ClosedSessionMaintenanceSettings = field(
        default_factory=ClosedSessionMaintenanceSettings
    )
    running: bool = False
    started_at: str | None = None
    last_completed_at: str | None = None
    last_error: str = ""
    last_cycle: ClosedSessionMaintenanceCycle | None = None
    provider_round_robin_cursor: dict[str, tuple[str, str, str]] = field(
        default_factory=dict,
        repr=False,
    )
    provider_schedule_round_robin_cursor: dict[str, tuple[str, str]] = field(
        default_factory=dict,
        repr=False,
    )
    provider_request_timestamps: dict[str, list[float]] = field(
        default_factory=dict,
        repr=False,
    )
    provider_failure_streaks: dict[str, int] = field(
        default_factory=dict,
        repr=False,
    )
    provider_breaker_until: dict[str, float] = field(
        default_factory=dict,
        repr=False,
    )
    provider_schedule_retry_not_before: dict[str, float] = field(
        default_factory=dict,
        repr=False,
    )

    def cycle_start_index(
        self,
        provider: str,
        candidates: tuple[tuple[str, str, str], ...],
    ) -> int:
        if not candidates:
            return 0
        cursor = self.provider_round_robin_cursor.get(provider)
        if cursor not in candidates:
            return 0
        return (candidates.index(cursor) + 1) % len(candidates)

    def advance_provider_cursor(
        self,
        provider: str,
        candidate: tuple[str, str, str],
    ) -> None:
        self.provider_round_robin_cursor[provider] = candidate

    def schedule_cycle_start_index(
        self,
        provider: str,
        candidates: tuple[tuple[str, str], ...],
    ) -> int:
        if not candidates:
            return 0
        cursor = self.provider_schedule_round_robin_cursor.get(provider)
        if cursor not in candidates:
            return 0
        return (candidates.index(cursor) + 1) % len(candidates)

    def advance_schedule_cursor(
        self,
        provider: str,
        candidate: tuple[str, str],
    ) -> None:
        self.provider_schedule_round_robin_cursor[provider] = candidate

    def provider_admission_gate(
        self,
        provider: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        observed_at = monotonic() if now is None else float(now)
        settings = self.settings
        window_seconds = max(float(settings.admission_window_seconds), 1.0)
        cutoff = observed_at - window_seconds
        recent = [
            timestamp
            for timestamp in self.provider_request_timestamps.get(provider, ())
            if timestamp > cutoff
        ]
        if recent:
            self.provider_request_timestamps[provider] = recent
        else:
            self.provider_request_timestamps.pop(provider, None)

        breaker_until = float(self.provider_breaker_until.get(provider, 0.0))
        if breaker_until and breaker_until <= observed_at:
            self.provider_breaker_until.pop(provider, None)
            self.provider_failure_streaks[provider] = 0
            breaker_until = 0.0
        breaker_remaining = max(breaker_until - observed_at, 0.0)
        admission_limit = max(int(settings.max_provider_admissions_per_window), 1)
        admitted = len(recent)
        if breaker_remaining > 0:
            blocker_reason = "provider_circuit_open"
        elif admitted >= admission_limit:
            blocker_reason = "provider_window_budget_exhausted"
        else:
            blocker_reason = ""
        window_reset_seconds = max(recent[0] + window_seconds - observed_at, 0.0) if recent else 0.0
        return {
            "admitted_requests": admitted,
            "remaining_requests": max(admission_limit - admitted, 0),
            "admission_limit": admission_limit,
            "window_seconds": window_seconds,
            "window_reset_seconds": window_reset_seconds,
            "failure_streak": max(int(self.provider_failure_streaks.get(provider, 0)), 0),
            "failure_threshold": max(int(settings.failure_threshold), 1),
            "breaker_open": breaker_remaining > 0,
            "breaker_remaining_seconds": breaker_remaining,
            "blocker_reason": blocker_reason,
            "admit": not blocker_reason,
        }

    def record_provider_request(
        self,
        provider: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        observed_at = monotonic() if now is None else float(now)
        self.provider_admission_gate(provider, now=observed_at)
        self.provider_request_timestamps.setdefault(provider, []).append(observed_at)
        return self.provider_admission_gate(provider, now=observed_at)

    def record_provider_request_completion(
        self,
        provider: str,
        outcome: ProviderRequestCompletionOutcome,
        *,
        now: float | None = None,
    ) -> ProviderRequestCompletionOutcome:
        observed_at = monotonic() if now is None else float(now)
        if not isinstance(outcome, ProviderRequestCompletionOutcome):
            raise TypeError("PROVIDER_REQUEST_COMPLETION_OUTCOME_REQUIRED")
        if outcome is ProviderRequestCompletionOutcome.FAILURE:
            streak = max(int(self.provider_failure_streaks.get(provider, 0)), 0) + 1
            self.provider_failure_streaks[provider] = streak
            threshold = max(int(self.settings.failure_threshold), 1)
            if streak >= threshold:
                self.provider_breaker_until[provider] = observed_at + max(
                    float(self.settings.breaker_backoff_seconds),
                    1.0,
                )
            return outcome
        if outcome is ProviderRequestCompletionOutcome.SUCCESS:
            self.provider_failure_streaks[provider] = 0
            self.provider_breaker_until.pop(provider, None)
        return outcome

    def provider_schedule_retry_remaining(
        self,
        provider: str,
        *,
        now: float | None = None,
    ) -> float:
        observed_at = monotonic() if now is None else float(now)
        retry_not_before = float(self.provider_schedule_retry_not_before.get(provider, 0.0))
        if retry_not_before <= observed_at:
            self.provider_schedule_retry_not_before.pop(provider, None)
            return 0.0
        return retry_not_before - observed_at

    def record_provider_schedule_retry(
        self,
        provider: str,
        retry_after_seconds: float,
        *,
        now: float | None = None,
    ) -> float:
        observed_at = monotonic() if now is None else float(now)
        retry_not_before = observed_at + max(float(retry_after_seconds), 1.0)
        self.provider_schedule_retry_not_before[provider] = max(
            float(self.provider_schedule_retry_not_before.get(provider, 0.0)),
            retry_not_before,
        )
        return self.provider_schedule_retry_remaining(provider, now=observed_at)

    def clear_provider_schedule_retry(self, provider: str) -> None:
        self.provider_schedule_retry_not_before.pop(provider, None)

    def record_provider_connection_recovered(self, provider: str) -> None:
        """Start a fresh failure epoch after provider transport recovery."""

        self.provider_failure_streaks[provider] = 0
        self.provider_breaker_until.pop(provider, None)
        self.clear_provider_schedule_retry(provider)

    def provider_safety_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        observed_at = monotonic() if now is None else float(now)
        providers = sorted(
            set(self.provider_request_timestamps)
            | set(self.provider_failure_streaks)
            | set(self.provider_breaker_until)
            | set(self.provider_round_robin_cursor)
            | set(self.provider_schedule_round_robin_cursor)
            | set(self.provider_schedule_retry_not_before)
        )
        snapshot: dict[str, dict[str, Any]] = {}
        for provider in providers:
            safety = self.provider_admission_gate(provider, now=observed_at)
            safety["schedule_retry_remaining_seconds"] = self.provider_schedule_retry_remaining(
                provider, now=observed_at
            )
            snapshot[provider] = safety
        return snapshot

    def record_started(self) -> None:
        self.running = True
        self.started_at = _now_iso()
        self.last_error = ""

    def record_completed(self, cycle: ClosedSessionMaintenanceCycle) -> None:
        self.running = False
        self.last_completed_at = _now_iso()
        self.last_error = ""
        self.last_cycle = cycle

    def record_error(self, error: BaseException | str) -> None:
        self.running = False
        self.last_completed_at = _now_iso()
        self.last_error = str(error or "").strip()

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.settings.as_dict(),
            "running": bool(self.running),
            "started_at": self.started_at,
            "last_completed_at": self.last_completed_at,
            "last_error": self.last_error,
            "last_cycle": self.last_cycle.as_dict() if self.last_cycle is not None else {},
            "provider_safety": self.provider_safety_snapshot(),
        }
