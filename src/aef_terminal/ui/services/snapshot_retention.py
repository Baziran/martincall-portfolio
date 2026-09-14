from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.gex.constants import GEX_CHART_HISTORY_HOURS
from aef_terminal.data.market_data import (
    QUOTE_SNAPSHOT_FUTURE_SKEW_SECONDS,
    QUOTE_SNAPSHOT_RETENTION_HOURS,
)
from aef_terminal.runtime.async_tasks import settle_physical_task
from aef_terminal.runtime.metrics import increment_metric, set_metric
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts


SNAPSHOT_RETENTION_INTERVAL_SECONDS = 3600.0
SNAPSHOT_RETENTION_RETRY_SECONDS = 30.0
_SNAPSHOT_RETENTION_STATEMENT_TIMEOUT_MS = 60_000
_SNAPSHOT_RETENTION_LOCK_TIMEOUT_MS = 2_000


async def run_snapshot_retention_loop(
    *,
    store_factory: Callable[[], Any],
    logger_warning: Callable[..., Any],
    interval_seconds: float = SNAPSHOT_RETENTION_INTERVAL_SECONDS,
    retry_seconds: float = SNAPSHOT_RETENTION_RETRY_SECONDS,
) -> None:
    """Keep durable market snapshot history inside its code-owned horizons."""

    interval = max(float(interval_seconds), 30.0)
    retry = max(float(retry_seconds), 1.0)
    while True:
        store = store_factory()
        if store is None:
            await asyncio.sleep(retry)
            continue
        now_utc = datetime.now(tz=UTC)
        with postgres_operation_timeouts(
            statement_timeout_ms=_SNAPSHOT_RETENTION_STATEMENT_TIMEOUT_MS,
            lock_timeout_ms=_SNAPSHOT_RETENTION_LOCK_TIMEOUT_MS,
        ):
            retention_task = asyncio.create_task(
                asyncio.to_thread(
                    store.prune_market_snapshot_history,
                    quote_before=now_utc - timedelta(hours=QUOTE_SNAPSHOT_RETENTION_HOURS),
                    gex_before=now_utc - timedelta(hours=GEX_CHART_HISTORY_HOURS),
                    future_after=now_utc + timedelta(seconds=QUOTE_SNAPSHOT_FUTURE_SKEW_SECONDS),
                )
            )
        outcome = await settle_physical_task(retention_task)
        if outcome.error is not None or outcome.task_cancelled:
            error = outcome.error or RuntimeError(
                "market snapshot retention completion task was cancelled"
            )
            increment_metric("market_snapshot_retention_errors_total")
            logger_warning("Market snapshot retention failed: %s", error)
            if outcome.cancellation is not None:
                raise outcome.cancellation from error
            await asyncio.sleep(retry)
            continue
        result = outcome.result
        if not isinstance(result, dict):
            increment_metric("market_snapshot_retention_errors_total")
            logger_warning("Market snapshot retention returned an invalid acknowledgement")
            if outcome.cancellation is not None:
                raise outcome.cancellation
            await asyncio.sleep(retry)
            continue
        for field in (
            "dropped_quote_chunks",
            "deleted_quote_rows",
            "deleted_gex_rows",
        ):
            count = max(int(result.get(field) or 0), 0)
            if count:
                increment_metric(
                    "market_snapshot_retention_mutations_total",
                    count,
                    mutation=field,
                )
        has_more = bool(result.get("has_more"))
        set_metric("market_snapshot_retention_backlog", int(has_more))
        if outcome.cancellation is not None:
            raise outcome.cancellation
        await asyncio.sleep(retry if has_more else interval)
