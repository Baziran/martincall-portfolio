from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
    route_fingerprint,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.ui.services.ibkr_self_heal_detector import detect_ibkr_self_heal_issue
from aef_terminal.ui.services.ibkr_self_heal_models import (
    IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS,
    IBKR_SELF_HEAL_MAX_RECONNECTS,
    IBKR_SELF_HEAL_POLL_SECONDS,
    IBKR_SELF_HEAL_RECONNECT_TIMEOUT_SECONDS,
    IbkrSelfHealIssue,
    IbkrSelfHealState,
    issue_payload,
)


_RECOVERY_TAIL_ACCEPTED_ADMISSION_STATUSES = frozenset({"scheduled", "running", "queued"})
_RECOVERY_TAIL_SUCCESSFUL_TERMINAL_STATUSES = frozenset({"committed", "no_change"})


def _recovery_tail_failure(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return "malformed_result"
    history_coverage = payload.get("history_coverage")
    repair = history_coverage.get("repair") if isinstance(history_coverage, Mapping) else None
    if not isinstance(repair, Mapping):
        return "repair_outcome_missing"
    phase = str(repair.get("phase") or "").strip().lower()
    status = str(repair.get("status") or "").strip().lower()
    if phase == "idle" and status == "not_needed":
        return ""
    if phase == "admission" and status in _RECOVERY_TAIL_ACCEPTED_ADMISSION_STATUSES:
        return ""
    if phase == "terminal" and status in _RECOVERY_TAIL_SUCCESSFUL_TERMINAL_STATUSES:
        return ""
    return status or phase or "repair_outcome_malformed"


def _history_connection_generation(payload: object) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    generation = payload.get("history_connection_generation")
    if type(generation) is not int or generation < 0:
        return None
    return generation


async def run_ibkr_self_heal_loop(
    *,
    poll_seconds: float = IBKR_SELF_HEAL_POLL_SECONDS,
    server_sleeping: Callable[[], bool],
    active_chart_streams: Callable[[], list[tuple[str, str, str, int]]],
    quote_instruments: Callable[[], list[dict[str, Any]]],
    instrument_lookup: Callable[[str], dict[str, Any]],
    ibkr_status: Callable[[], dict[str, Any]],
    watchlist_gap_quality: Callable[[str, str], dict[str, Any]],
    request_recovery_tail: Callable[[str, str], dict[str, Any]] | None = None,
    force_reconnect: Callable[..., Awaitable[dict[str, Any]]],
    state: IbkrSelfHealState,
    cleanup_quarantine: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    history_session_recovered: Callable[[], None] | None = None,
    logger_warning: Callable[[str, object], None] | None = None,
    logger_debug: Callable[[str, object], None] | None = None,
) -> None:
    observed_history_generation: int | None = None
    pending_history_recovery = False

    def observe_history_generation(payload: object) -> None:
        nonlocal observed_history_generation, pending_history_recovery
        generation = _history_connection_generation(payload)
        if generation is None:
            return
        previous = observed_history_generation
        observed_history_generation = generation
        if previous is not None and generation > previous:
            pending_history_recovery = True

    def publish_history_recovery() -> None:
        nonlocal pending_history_recovery
        if not pending_history_recovery or history_session_recovered is None:
            return
        try:
            history_session_recovered()
        except Exception as exc:
            if logger_warning is not None:
                logger_warning("IBKR history recovery callback failed: %s", exc)
            return
        pending_history_recovery = False

    while True:
        try:
            if not server_sleeping():
                status = ibkr_status()
                observe_history_generation(status)
                streams: list[tuple[str, str, str, int]] = []
                for stream in active_chart_streams():
                    try:
                        instrument_id = require_exact_identity_text(
                            stream[0],
                            field="instrument_id",
                        )
                        instrument = instrument_lookup(instrument_id)
                        if route_instrument(instrument).provider == "ibkr":
                            streams.append(stream)
                    except Exception as exc:
                        if logger_debug is not None:
                            logger_debug("IBKR self-heal chart demand rejected: %s", exc)
                quote_items: list[dict[str, Any]] = []
                for instrument in quote_instruments():
                    try:
                        if route_instrument(instrument).provider == "ibkr":
                            quote_items.append(instrument)
                    except Exception as exc:
                        if logger_debug is not None:
                            logger_debug("IBKR self-heal quote demand rejected: %s", exc)
                quote_routes = [route_fingerprint(instrument) for instrument in quote_items]
                qualities: dict[tuple[str, str], dict[str, Any]] = {}
                for instrument_id, interval, _range, _count in streams[:8]:
                    try:
                        exact_instrument_id = require_exact_identity_text(
                            instrument_id,
                            field="instrument_id",
                        )
                        exact_interval = str(interval)
                        qualities[
                            (exact_instrument_id, exact_interval)
                        ] = await run_physical_thread_call(
                            watchlist_gap_quality,
                            exact_instrument_id,
                            exact_interval,
                        )
                    except Exception as exc:
                        if logger_debug is not None:
                            logger_debug("IBKR self-heal quality check failed: %s", exc)
                issue = detect_ibkr_self_heal_issue(status, streams, qualities, quote_routes)
                state.record_check(issue)
                if issue is not None:
                    if issue.code == "session_quarantined":
                        if cleanup_quarantine is None:
                            state.record_result(
                                False,
                                "IBKR quarantine cleanup is not configured",
                            )
                        else:
                            try:
                                result = await asyncio.wait_for(
                                    cleanup_quarantine(timeout=2.5),
                                    timeout=IBKR_SELF_HEAL_RECONNECT_TIMEOUT_SECONDS,
                                )
                                ok = bool(result.get("ok"))
                                error = str(result.get("error") or "") if ok else str(result)
                                state.record_result(ok, error)
                                if logger_warning is not None:
                                    logger_warning(
                                        "IBKR self-heal quarantine cleanup: %s",
                                        issue.reason,
                                    )
                            except Exception as exc:
                                state.record_result(
                                    False,
                                    str(exc) or exc.__class__.__name__,
                                )
                    else:
                        now = time.monotonic()
                        allowed, skip_reason = state.reconnect_allowed(now)
                        if allowed:
                            state.record_attempt(now)
                            try:
                                result = await asyncio.wait_for(
                                    force_reconnect(
                                        quote_instruments=quote_items,
                                        timeout=2.5,
                                    ),
                                    timeout=IBKR_SELF_HEAL_RECONNECT_TIMEOUT_SECONDS,
                                )
                                observe_history_generation(result.get("status"))
                                ok = bool(result.get("ok"))
                                error = str(result.get("error") or "") if ok else str(result)
                                if ok:
                                    recovery_intervals = sorted(
                                        {str(stream[1]) for stream in streams if str(stream[1])}
                                    )
                                    recovery_failures: list[str] = []
                                    if recovery_intervals and request_recovery_tail is None:
                                        recovery_failures.append("recovery_tail_not_configured")
                                    elif recovery_intervals and request_recovery_tail is not None:
                                        for instrument in quote_items:
                                            try:
                                                recovery_route = route_instrument(
                                                    instrument,
                                                    expected_source="ibkr",
                                                )
                                            except Exception as exc:
                                                recovery_failures.append(
                                                    "route_resolution_failed: "
                                                    + (str(exc) or exc.__class__.__name__)
                                                )
                                                continue
                                            for recovery_interval in recovery_intervals:
                                                try:
                                                    recovery = await run_physical_thread_call(
                                                        request_recovery_tail,
                                                        recovery_route.instrument_id,
                                                        recovery_interval,
                                                    )
                                                    failure = _recovery_tail_failure(recovery)
                                                    if failure:
                                                        recovery_failures.append(
                                                            f"{recovery_route.instrument_id} "
                                                            f"{recovery_interval}: {failure}"
                                                        )
                                                except Exception as exc:
                                                    recovery_failures.append(
                                                        f"{recovery_route.instrument_id} "
                                                        f"{recovery_interval}: "
                                                        f"{str(exc) or exc.__class__.__name__}"
                                                    )
                                                    if logger_debug is not None:
                                                        logger_debug(
                                                            "IBKR recovery tail request failed: %s",
                                                            exc,
                                                        )
                                    if recovery_failures:
                                        ok = False
                                        error = "IBKR recovery tail failed: " + "; ".join(
                                            recovery_failures
                                        )
                                state.record_result(ok, error)
                                if logger_warning is not None:
                                    logger_warning("IBKR self-heal reconnect: %s", issue.reason)
                            except Exception as exc:
                                state.record_result(False, str(exc) or exc.__class__.__name__)
                        elif skip_reason == "reconnect_limit":
                            state.record_result(False, state.blocked_reason)
            publish_history_recovery()
            await asyncio.sleep(max(float(poll_seconds), 1.0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.record_result(False, str(exc) or exc.__class__.__name__)
            if logger_warning is not None:
                logger_warning("IBKR self-heal loop error: %s", exc)
            publish_history_recovery()
            await asyncio.sleep(max(float(poll_seconds), 1.0))


__all__ = [
    "IBKR_SELF_HEAL_POLL_SECONDS",
    "IBKR_SELF_HEAL_LIMIT_RETRY_SECONDS",
    "IBKR_SELF_HEAL_MAX_RECONNECTS",
    "IBKR_SELF_HEAL_RECONNECT_TIMEOUT_SECONDS",
    "IbkrSelfHealIssue",
    "IbkrSelfHealState",
    "detect_ibkr_self_heal_issue",
    "issue_payload",
    "run_ibkr_self_heal_loop",
]
