from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.routers.error_payloads import build_action_error_response
from aef_terminal.ui.tick_aggregates import (
    parse_iso_ts,
    parse_tick_context_levels,
    tick_context_payload,
)


class TickPriceIncrementUnavailableError(ValueError):
    """The selected exact provider route has no executable price increment."""


class TickPriceStepInvalidError(ValueError):
    """A requested profile step is not an exact multiple of the provider increment."""


def _tick_price_step(price_step: float | None, price_increment: float | None) -> float:
    if price_increment is None:
        raise TickPriceIncrementUnavailableError("TICK_PRICE_INCREMENT_UNAVAILABLE")
    if (
        isinstance(price_increment, bool)
        or not isinstance(price_increment, (int, float))
        or not isfinite(float(price_increment))
        or float(price_increment) <= 0
    ):
        raise TickPriceIncrementUnavailableError("TICK_PRICE_INCREMENT_INVALID")
    increment = float(price_increment)
    if price_step is None:
        return increment
    if (
        isinstance(price_step, bool)
        or not isinstance(price_step, (int, float))
        or not isfinite(float(price_step))
        or float(price_step) <= 0
    ):
        raise TickPriceStepInvalidError("TICK_PRICE_STEP_INVALID")
    step = float(price_step)
    try:
        ratio = Decimal(str(step)) / Decimal(str(increment))
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise TickPriceStepInvalidError("TICK_PRICE_STEP_INVALID") from exc
    if ratio != ratio.to_integral_value():
        raise TickPriceStepInvalidError("TICK_PRICE_STEP_NOT_ALIGNED")
    return step


def normalized_tick_range(
    start: str | None,
    end: str | None,
    *,
    default_lookback: timedelta,
    max_window: timedelta | None = None,
) -> tuple[datetime, datetime]:
    end_dt = parse_iso_ts(end) or datetime.now(tz=UTC)
    start_dt = parse_iso_ts(start) or (end_dt - default_lookback)
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    if max_window is not None and end_dt - start_dt > max_window:
        start_dt = end_dt - max_window
    return start_dt, end_dt


def tick_context_error(
    code: str, message: str, *, retryable: bool = False, **extra: Any
) -> dict[str, Any]:
    payload = build_action_error_response(
        code=code,
        category="tick",
        retryable=retryable,
        error=message,
        **extra,
    )
    payload.setdefault("delta", [])
    payload.setdefault("profile", [])
    payload.setdefault("stats", {})
    return payload


def tick_history_error(
    code: str, message: str, *, retryable: bool = False, **extra: Any
) -> dict[str, Any]:
    payload = build_action_error_response(
        code=code,
        category="tick",
        retryable=retryable,
        error=message,
        **extra,
    )
    payload.setdefault("rows", [])
    payload.setdefault("stats", {})
    return payload


def tick_context_response(
    store: Any,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    start: str | None = None,
    end: str | None = None,
    bucket: str = "1 minute",
    price_step: float | None = None,
    levels: str | None = None,
    limit: int = 160,
    live_status: Callable[[], dict[str, Any]] | None = None,
    include_telemetry: bool = False,
) -> dict[str, Any]:
    try:
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        expected_route = require_exact_identity_text(
            expected_route_fingerprint, field="route_fingerprint"
        )
    except ValueError as exc:
        return tick_context_error("TICK_INSTRUMENT_ID_REQUIRED", str(exc))
    if store is None:
        return tick_context_error("TICK_STORAGE_NOT_CONFIGURED", "storage not configured")
    start_dt, end_dt = normalized_tick_range(
        start, end, default_lookback=timedelta(hours=2), max_window=timedelta(hours=24)
    )
    try:
        instrument = store.lookup_instrument(identity)
        if not isinstance(instrument, dict):
            raise ValueError(f"TICK_INSTRUMENT_IDENTITY_REQUIRED instrument_id={identity}")
        route = route_instrument(instrument, expected_source="ibkr")
        if route.fingerprint != expected_route:
            raise ValueError(
                f"TICK_ROUTE_CHANGED expected={expected_route_fingerprint} actual={route.fingerprint}"
            )
        step = _tick_price_step(price_step, route.price_increment)
        delta_rows = store.read_tick_delta(
            route.instrument_id,
            route.fingerprint,
            start_dt,
            end_dt,
            bucket=bucket,
        )
        profile_rows = store.read_volume_profile(
            route.instrument_id,
            route.fingerprint,
            start_dt,
            end_dt,
            price_step=step,
        )
        payload = tick_context_payload(
            delta_rows,
            profile_rows,
            limit=limit,
            price_step=step,
            levels=parse_tick_context_levels(levels),
            include_telemetry=include_telemetry,
        )
        payload["stats"]["live"] = live_status() if live_status is not None else {}
        return {
            "ok": True,
            "symbol": route.instrument_key,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "bucket": bucket,
            "price_step": step,
            **payload,
        }
    except TickPriceIncrementUnavailableError as exc:
        return tick_context_error(
            "TICK_PRICE_INCREMENT_UNAVAILABLE",
            str(exc),
            retryable=True,
        )
    except TickPriceStepInvalidError as exc:
        return tick_context_error("TICK_PRICE_STEP_INVALID", str(exc))
    except Exception as exc:
        return tick_context_error("TICK_CONTEXT_LOAD_FAILED", str(exc), retryable=True)


def tick_history_response(
    store: Any,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    start: str | None = None,
    end: str | None = None,
    timeframe: str = "5m",
    limit: int = 5000,
) -> dict[str, Any]:
    try:
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        expected_route = require_exact_identity_text(
            expected_route_fingerprint, field="route_fingerprint"
        )
    except ValueError as exc:
        return tick_history_error("TICK_INSTRUMENT_ID_REQUIRED", str(exc))
    if store is None:
        return tick_history_error("TICK_STORAGE_NOT_CONFIGURED", "storage not configured")
    start_dt, end_dt = normalized_tick_range(start, end, default_lookback=timedelta(days=30))
    row_limit = max(1, min(int(limit or 5000), 50000))
    try:
        instrument = store.lookup_instrument(identity)
        if not isinstance(instrument, dict):
            raise ValueError(f"TICK_INSTRUMENT_IDENTITY_REQUIRED instrument_id={identity}")
        route = route_instrument(instrument, expected_source="ibkr")
        if route.fingerprint != expected_route:
            raise ValueError(
                f"TICK_ROUTE_CHANGED expected={expected_route_fingerprint} actual={route.fingerprint}"
            )
        rows = store.read_tick_bar_history(
            route.instrument_id,
            route.fingerprint,
            start_dt,
            end_dt,
            timeframe=timeframe,
            limit=row_limit,
        )
        total_volume = sum(int(row["total_volume"]) for row in rows)
        net_delta = sum(int(row["net_delta"]) for row in rows)
        return {
            "ok": True,
            "provider": route.provider,
            "symbol": route.instrument_key,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "timeframe": timeframe,
            "rows": rows,
            "stats": {
                "bars": len(rows),
                "total_volume": total_volume,
                "net_delta": net_delta,
                "delta_ratio": (net_delta / total_volume) if total_volume else 0.0,
            },
        }
    except Exception as exc:
        return tick_history_error("TICK_HISTORY_LOAD_FAILED", str(exc), retryable=True)


def tick_profile_history_response(
    store: Any,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    start: str | None = None,
    end: str | None = None,
    timeframe: str = "5m",
    price_step: float | None = None,
    limit: int = 50000,
) -> dict[str, Any]:
    try:
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        expected_route = require_exact_identity_text(
            expected_route_fingerprint, field="route_fingerprint"
        )
    except ValueError as exc:
        return tick_history_error("TICK_INSTRUMENT_ID_REQUIRED", str(exc))
    if store is None:
        return tick_history_error("TICK_STORAGE_NOT_CONFIGURED", "storage not configured")
    start_dt, end_dt = normalized_tick_range(start, end, default_lookback=timedelta(days=30))
    row_limit = max(1, min(int(limit or 50000), 200000))
    try:
        instrument = store.lookup_instrument(identity)
        if not isinstance(instrument, dict):
            raise ValueError(f"TICK_INSTRUMENT_IDENTITY_REQUIRED instrument_id={identity}")
        route = route_instrument(instrument, expected_source="ibkr")
        if route.fingerprint != expected_route:
            raise ValueError(
                f"TICK_ROUTE_CHANGED expected={expected_route_fingerprint} actual={route.fingerprint}"
            )
        step = _tick_price_step(price_step, route.price_increment)
        rows = store.read_tick_volume_profile_history(
            route.instrument_id,
            route.fingerprint,
            start_dt,
            end_dt,
            timeframe=timeframe,
            price_step=step,
            limit=row_limit,
        )
        total_volume = sum(int(row["total_volume"]) for row in rows)
        net_delta = sum(int(row["net_delta"]) for row in rows)
        buckets = {str(row.get("ts") or "") for row in rows}
        prices = {float(row.get("price")) for row in rows if row.get("price") is not None}
        return {
            "ok": True,
            "provider": route.provider,
            "symbol": route.instrument_key,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "timeframe": timeframe,
            "price_step": step,
            "rows": rows,
            "stats": {
                "rows": len(rows),
                "buckets": len(buckets),
                "prices": len(prices),
                "total_volume": total_volume,
                "net_delta": net_delta,
                "delta_ratio": (net_delta / total_volume) if total_volume else 0.0,
            },
        }
    except TickPriceIncrementUnavailableError as exc:
        return tick_history_error(
            "TICK_PRICE_INCREMENT_UNAVAILABLE",
            str(exc),
            retryable=True,
        )
    except TickPriceStepInvalidError as exc:
        return tick_history_error("TICK_PRICE_STEP_INVALID", str(exc))
    except Exception as exc:
        return tick_history_error("TICK_HISTORY_LOAD_FAILED", str(exc), retryable=True)
