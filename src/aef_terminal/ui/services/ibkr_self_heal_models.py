from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aef_terminal.runtime.clock import utc_now_iso as now_iso

IBKR_SELF_HEAL_POLL_SECONDS = 10.0
IBKR_SELF_HEAL_MAX_RECONNECTS = 3
IBKR_SELF_HEAL_WINDOW_SECONDS = 300.0
IBKR_SELF_HEAL_MIN_RECONNECT_SECONDS = 45.0
IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS = 60.0
IBKR_SELF_HEAL_RECONNECT_TIMEOUT_SECONDS = 18.0
IBKR_SELF_HEAL_LANE_STUCK_SECONDS = 45.0
IBKR_SELF_HEAL_PENDING_STUCK_SECONDS = 60.0
IBKR_SELF_HEAL_NO_QUOTE_SECONDS = 45.0
IBKR_SELF_HEAL_API_ERROR_THRESHOLD = 10


@dataclass(frozen=True)
class IbkrSelfHealIssue:
    code: str
    reason: str
    chart_instrument_id: str = ""
    interval: str = "5m"
    range_: str = "3d"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class IbkrSelfHealState:
    attempt_times: list[float] = field(default_factory=list)
    last_checked_at: str | None = None
    last_issue: dict[str, Any] | None = None
    last_attempt_at: str | None = None
    last_result_at: str | None = None
    last_result_ok: bool | None = None
    last_error: str = ""
    blocked_since: str | None = None
    blocked_reason: str = ""

    def _clear_reconnect_budget(self) -> None:
        self.attempt_times = []
        self.blocked_since = None
        self.blocked_reason = ""

    def _clear_blocked(self) -> None:
        self.blocked_since = None
        self.blocked_reason = ""

    def _prune(self, now: float, window_seconds: float, max_reconnects: int) -> None:
        cutoff = now - max(float(window_seconds), 1.0)
        self.attempt_times = [ts for ts in self.attempt_times if ts >= cutoff]
        if len(self.attempt_times) < max_reconnects:
            self._clear_blocked()

    def reconnect_allowed(
        self,
        now: float,
        *,
        max_reconnects: int = IBKR_SELF_HEAL_MAX_RECONNECTS,
        window_seconds: float = IBKR_SELF_HEAL_WINDOW_SECONDS,
        min_reconnect_seconds: float = IBKR_SELF_HEAL_MIN_RECONNECT_SECONDS,
        limit_retry_seconds: float = IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS,
    ) -> tuple[bool, str]:
        self._prune(now, window_seconds, max_reconnects)
        if self.attempt_times and now - self.attempt_times[-1] < min_reconnect_seconds:
            return False, "reconnect_backoff"
        if len(self.attempt_times) >= max_reconnects:
            retry_after = max(float(limit_retry_seconds), float(min_reconnect_seconds))
            if now - self.attempt_times[-1] < retry_after:
                self._clear_blocked()
                return False, "reconnect_cooldown"
            keep_attempts = max(max_reconnects - 1, 0)
            self.attempt_times = self.attempt_times[-keep_attempts:] if keep_attempts else []
            self._clear_blocked()
        return True, ""

    def record_check(self, issue: IbkrSelfHealIssue | None) -> None:
        self.last_checked_at = now_iso()
        self.last_issue = issue_payload(issue) if issue is not None else None
        if issue is None:
            self._clear_reconnect_budget()

    def record_attempt(self, now: float) -> None:
        self.attempt_times.append(now)
        self.last_attempt_at = now_iso()
        self.last_result_ok = None
        self.last_error = ""

    def record_result(self, ok: bool, error: str = "") -> None:
        self.last_result_at = now_iso()
        self.last_result_ok = bool(ok)
        self.last_error = str(error or "")

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "blocked": bool(self.blocked_since),
            "blocked_since": self.blocked_since,
            "blocked_reason": self.blocked_reason,
            "attempts_in_window": len(self.attempt_times),
            "max_attempts_in_window": IBKR_SELF_HEAL_MAX_RECONNECTS,
            "window_seconds": IBKR_SELF_HEAL_WINDOW_SECONDS,
            "limit_retry_seconds": IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS,
            "last_checked_at": self.last_checked_at,
            "last_issue": dict(self.last_issue or {}),
            "last_attempt_at": self.last_attempt_at,
            "last_result_at": self.last_result_at,
            "last_result_ok": self.last_result_ok,
            "last_error": self.last_error,
        }


def issue_payload(issue: IbkrSelfHealIssue | None) -> dict[str, Any]:
    if issue is None:
        return {}
    return {
        "code": issue.code,
        "reason": issue.reason,
        "chart_instrument_id": issue.chart_instrument_id,
        "interval": issue.interval,
        "range": issue.range_,
        "details": dict(issue.details or {}),
    }
