from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from aef_terminal.data.gex.history import get_gex_analysis_pair
from aef_terminal.data.gex.constants import GEX_CONTEXT_MAX_LEVELS
from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.tick_buffer import tick_ingest_quality
from aef_terminal.domain import Bar
from aef_terminal.runtime.metrics import increment_metric
from aef_terminal.runtime.telemetry import exception_message, log_structured_error


_LOGGER = logging.getLogger(__name__)


def load_gex_history_input(
    provider_symbol: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    capture_mode: GexCaptureMode,
    now: datetime,
    store: Any | None,
    preloaded_rows: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return get_gex_analysis_pair(
        provider_symbol,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        max_levels=GEX_CONTEXT_MAX_LEVELS,
        capture_mode=capture_mode,
        now=now,
        store=store,
        preloaded_rows=preloaded_rows,
    )


def load_option_targets_input(
    store: Any | None,
    *,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    if store is None:
        return []
    rows = store.read_option_targets(
        timeframe=timeframe,
        route_pairs=[(instrument_id, route_fingerprint)],
    )
    if not isinstance(rows, list):
        raise TypeError("Option target storage must return a typed list")
    targets: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            raise TypeError("Option target storage rows must contain a canonical payload")
        targets.append(row["payload"])
    return targets


def load_tick_flow_input(
    store: Any | None,
    instrument: dict[str, Any],
    bars: Sequence[Bar],
    *,
    hours: int = 24,
) -> dict[str, Any] | None:
    if store is None or not bars:
        return None
    step = (
        max(bars[-1].ts - bars[-2].ts, timedelta(minutes=1))
        if len(bars) >= 2
        else timedelta(minutes=5)
    )
    end = bars[-1].ts + step
    start = max(bars[0].ts, end - timedelta(hours=max(int(hours), 1)))
    route = None
    try:
        route = route_instrument(instrument, expected_source="ibkr")
        rows = store.read_tick_delta(
            route.instrument_id,
            route.fingerprint,
            start,
            end,
            bucket="1 minute",
        )
        if not rows:
            return None
        return {
            "source": "ibkr_tick",
            "provider": route.provider,
            "symbol": route.instrument_key,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "delta": rows,
            "classification_method": "inferred_quote_or_tick_test",
            "classification_confidence": "advisory",
            "decision_eligible": False,
            "availability_state": "available",
            "quality": tick_ingest_quality(
                route.provider,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                start=start,
                end=end,
            ),
        }
    except Exception as exc:
        provider = str(getattr(route, "provider", "") or instrument.get("provider") or "")
        instrument_id = route.instrument_id if route is not None else ""
        route_fingerprint = route.fingerprint if route is not None else ""
        increment_metric(
            "snapshot_context_load_total",
            context="tick_flow",
            status="failed",
            error=exc.__class__.__name__,
        )
        log_structured_error(
            _LOGGER,
            provider=provider,
            symbol="",
            interval="1m",
            range_=f"{start.isoformat()}..{end.isoformat()}",
            op="load_tick_flow_input",
            error=exc,
            event="snapshot_context_unavailable",
        )
        return {
            "source": "ibkr_tick",
            "provider": provider,
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "delta": [],
            "decision_eligible": False,
            "availability_state": "error",
            "error_code": "TICK_FLOW_INPUT_UNAVAILABLE",
            "reason": exception_message(exc),
        }
