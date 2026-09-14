from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.market_data import quote_snapshot_timestamp_is_admissible
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts
from aef_terminal.ui.quote_helpers import current_quote_price, quote_price, quote_snapshot_ts
from aef_terminal.ui.runtime.constants import QUOTE_SNAPSHOT_SECONDS

_LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY: dict[
    tuple[str, str],
    tuple[tuple[Any, ...], float],
] = {}
_QUOTE_SNAPSHOT_KEEPALIVE_SECONDS = max(QUOTE_SNAPSHOT_SECONDS * 12.0, 60.0)
_QUOTE_SNAPSHOT_PERSIST_STATEMENT_TIMEOUT_MS = 2_000
_QUOTE_SNAPSHOT_PERSIST_LOCK_TIMEOUT_MS = 500
_QUOTE_SNAPSHOT_STATS: dict[str, int] = {
    "persisted": 0,
    "changed": 0,
    "first": 0,
    "keepalive": 0,
    "skipped_unchanged": 0,
    "evicted": 0,
    "rejected_timestamp": 0,
    "state_entries": 0,
}


def quote_snapshot_write_stats() -> dict[str, int]:
    return dict(_QUOTE_SNAPSHOT_STATS)


def reset_quote_snapshot_state() -> None:
    _LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY.clear()
    _QUOTE_SNAPSHOT_STATS.clear()
    _QUOTE_SNAPSHOT_STATS.update(
        {
            "persisted": 0,
            "changed": 0,
            "first": 0,
            "keepalive": 0,
            "skipped_unchanged": 0,
            "evicted": 0,
            "rejected_timestamp": 0,
            "state_entries": 0,
        }
    )


def _rounded_quote_value(value: Any) -> float | None:
    number = exact_finite_number_or_none(value)
    return round(number, 6) if number is not None else None


def _quote_snapshot_signature(quote: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _rounded_quote_value(quote_price(quote)),
        _rounded_quote_value(quote.get("bid")),
        _rounded_quote_value(quote.get("ask")),
        _rounded_quote_value(quote.get("last")),
        _rounded_quote_value(quote.get("close")),
    )


def _quote_snapshot_due(
    instrument_id: str,
    route_fingerprint: str,
    quote: dict[str, Any],
    now: float,
) -> tuple[bool, tuple[Any, ...], str]:
    signature = _quote_snapshot_signature(quote)
    identity_key = (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
    )
    previous = _LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY.get(identity_key)
    if previous is None:
        return True, signature, "first"
    previous_signature, previous_at = previous
    if now - previous_at < QUOTE_SNAPSHOT_SECONDS:
        return False, signature, "unchanged"
    if signature != previous_signature:
        return True, signature, "changed"
    if now - previous_at >= _QUOTE_SNAPSHOT_KEEPALIVE_SECONDS:
        return True, signature, "keepalive"
    return False, signature, "unchanged"


async def persist_quote_snapshots_async(
    live_map: dict[str, dict[str, Any]] | None,
    *,
    instruments: Sequence[dict[str, Any]],
    store_factory: Callable[[], Any],
) -> None:
    if not isinstance(live_map, dict):
        return
    now = time.monotonic()
    captures: list[tuple[Any, datetime, dict[str, Any], tuple[Any, ...]]] = []
    routes = [route_instrument(instrument) for instrument in instruments]
    active_route_identities = {(route.instrument_id, route.fingerprint) for route in routes}
    stale_route_identities = set(_LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY).difference(
        active_route_identities
    )
    for identity in stale_route_identities:
        _LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY.pop(identity, None)
    if stale_route_identities:
        _QUOTE_SNAPSHOT_STATS["evicted"] += len(stale_route_identities)
    _QUOTE_SNAPSHOT_STATS["state_entries"] = len(_LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY)
    for route in routes:
        quote = live_map.get(route.fingerprint)
        if not isinstance(quote, dict) or current_quote_price(quote) is None:
            continue
        due, signature, reason = _quote_snapshot_due(
            route.instrument_id,
            route.fingerprint,
            quote,
            now,
        )
        if due:
            capture_boundary = datetime.now(tz=UTC)
            captured_at = quote_snapshot_ts(quote)
            if captured_at is None or not quote_snapshot_timestamp_is_admissible(
                captured_at,
                captured_at=capture_boundary,
            ):
                _QUOTE_SNAPSHOT_STATS["rejected_timestamp"] = (
                    _QUOTE_SNAPSHOT_STATS.get("rejected_timestamp", 0) + 1
                )
                continue
            _QUOTE_SNAPSHOT_STATS[reason] = _QUOTE_SNAPSHOT_STATS.get(reason, 0) + 1
            captures.append(
                (
                    route,
                    captured_at,
                    {
                        **quote,
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                    },
                    signature,
                )
            )
        else:
            _QUOTE_SNAPSHOT_STATS["skipped_unchanged"] = (
                _QUOTE_SNAPSHOT_STATS.get("skipped_unchanged", 0) + 1
            )
    if not captures:
        return

    def persist() -> bool:
        store = store_factory()
        if store is None:
            return False
        with postgres_operation_timeouts(
            statement_timeout_ms=_QUOTE_SNAPSHOT_PERSIST_STATEMENT_TIMEOUT_MS,
            lock_timeout_ms=_QUOTE_SNAPSHOT_PERSIST_LOCK_TIMEOUT_MS,
        ):
            store.upsert_quote_snapshots(
                [
                    (
                        route.instrument_id,
                        route.fingerprint,
                        route.provider_symbol,
                        captured_at,
                        quote,
                        route.provider,
                    )
                    for route, captured_at, quote, _signature in captures
                ]
            )
        return True

    persisted = await run_physical_thread_call(persist)
    if persisted:
        _QUOTE_SNAPSHOT_STATS["persisted"] = _QUOTE_SNAPSHOT_STATS.get("persisted", 0) + len(
            captures
        )
        for route, _captured_at, _quote, signature in captures:
            _LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY[(route.instrument_id, route.fingerprint)] = (
                signature,
                now,
            )
        _QUOTE_SNAPSHOT_STATS["state_entries"] = len(_LAST_QUOTE_SNAPSHOT_BY_ROUTE_IDENTITY)
