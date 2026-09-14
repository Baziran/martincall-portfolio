from __future__ import annotations

from typing import Any

from aef_terminal.ui.routers.error_payloads import build_action_error_response
from aef_terminal.ui.tick_aggregates import (
    parse_iso_ts,
    parse_tick_context_levels,
    tick_context_payload,
    tick_level_delta_summaries,
    tick_profile_aggregates,
    tick_profile_imbalance_groups,
    tick_profile_value_area,
    tick_recent_delta_summaries,
)
from aef_terminal.ui.tick_queries import (
    normalized_tick_range,
    tick_context_error,
    tick_context_response,
    tick_history_error,
    tick_history_response,
    tick_profile_history_response,
)


def tick_live_post_response(
    live: dict[str, Any],
    *,
    error_code: str | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    if (error_code is None) != (retryable is None) or (
        error_code is not None
        and (not isinstance(error_code, str) or not error_code or not isinstance(retryable, bool))
    ):
        raise ValueError("TICK_LIVE_ERROR_CONTRACT_INVALID")
    status = str(live.get("status") or "").lower()
    if error_code is None and status not in {"error", "sleeping"}:
        return {"ok": True, "live": live}
    last_error = str(live.get("last_error") or live.get("message") or "").strip()
    if error_code is None and status == "sleeping":
        payload = build_action_error_response(
            code="SERVER_SLEEPING",
            category="tick",
            retryable=True,
            error=last_error or "Server is sleeping. Tick live feed is paused.",
        )
    else:
        payload = build_action_error_response(
            code=error_code or "TICK_LIVE_FAILED",
            category="tick",
            retryable=True if retryable is None else retryable,
            error=last_error or "tick live failed",
        )
    return {**payload, "live": live}


__all__ = [
    "normalized_tick_range",
    "parse_iso_ts",
    "parse_tick_context_levels",
    "tick_context_error",
    "tick_context_payload",
    "tick_context_response",
    "tick_history_error",
    "tick_history_response",
    "tick_level_delta_summaries",
    "tick_live_post_response",
    "tick_profile_aggregates",
    "tick_profile_history_response",
    "tick_profile_imbalance_groups",
    "tick_profile_value_area",
    "tick_recent_delta_summaries",
]
