from __future__ import annotations

import math
import json
import threading
import weakref
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.gex.constants import (
    _GEX_DIVIDEND_YIELD_BROKER_MAX,
    _GEX_DIVIDEND_YIELD_CACHE_DAYS,
    _GEX_DIVIDEND_YIELD_MIN_YEARS,
)
from aef_terminal.data.gex.contracts import require_gex_valuation_rates
from aef_terminal.data.gex.utils import finite_number_or_none, _median_positive, parse_gex_timestamp
from aef_terminal.runtime.metrics import increment_metric
from aef_terminal.runtime.telemetry import exception_message
from aef_terminal.settings_contract import GEX_DIVIDEND_YIELD_SETTING_PREFIX


_GEX_DIVIDEND_CACHE_LOCKS_GUARD = threading.Lock()
_GEX_DIVIDEND_CACHE_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.RLock] = (
    weakref.WeakValueDictionary()
)
GEX_DIVIDEND_ESTIMATE_UNAVAILABLE = "GEX_DIVIDEND_ESTIMATE_UNAVAILABLE"
GEX_DIVIDEND_STORAGE_UNAVAILABLE = "GEX_DIVIDEND_STORAGE_UNAVAILABLE"


class _GexDividendRequest(Protocol):
    instrument_id: str
    route_fingerprint: str
    futures_options: bool
    dividend_yield: float


def _gex_dividend_cache_lock(
    instrument_id: str,
    route_fingerprint: str,
) -> threading.RLock:
    route_key = (
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
    )
    with _GEX_DIVIDEND_CACHE_LOCKS_GUARD:
        lock = _GEX_DIVIDEND_CACHE_LOCKS.get(route_key)
        if lock is None:
            lock = threading.RLock()
            _GEX_DIVIDEND_CACHE_LOCKS[route_key] = lock
        return lock


def _configured_dividend_yield(
    instrument_id: str,
    route_fingerprint: str,
    *,
    futures_options: bool,
    risk_free_rate: float,
    default_dividend_yield: float,
    configured_override: object | None = None,
    store: Any | None = None,
) -> float:
    if configured_override is not None:
        try:
            configured_yield = float(configured_override)
        except (TypeError, ValueError) as exc:
            raise ValueError("GEX dividend yield environment value must be numeric") from exc
        return require_gex_valuation_rates(
            {
                "risk_free_rate": risk_free_rate,
                "dividend_yield": configured_yield,
            }
        )["dividend_yield"]
    if futures_options:
        return 0.0
    cached = _cached_dividend_yield(
        instrument_id,
        route_fingerprint,
        store=store,
    )
    if cached is not None:
        return cached
    return require_gex_valuation_rates(
        {
            "risk_free_rate": risk_free_rate,
            "dividend_yield": default_dividend_yield,
        }
    )["dividend_yield"]


def _dividend_yield_setting_key(
    instrument_id: str,
    route_fingerprint: str,
) -> str:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    route_key = require_exact_identity_text(route_fingerprint, field="route_fingerprint")
    return GEX_DIVIDEND_YIELD_SETTING_PREFIX + json.dumps(
        [identity, route_key], ensure_ascii=False, separators=(",", ":")
    )


def _cached_dividend_payload(
    instrument_id: str,
    route_fingerprint: str,
    *,
    max_age_days: int = 45,
    store: Any | None = None,
) -> dict[str, Any] | None:
    if type(max_age_days) is not int or max_age_days <= 0:
        raise ValueError("GEX dividend cache age must be a positive integer")
    if store is None:
        return None
    try:
        raw = store.read_setting(
            "server",
            _dividend_yield_setting_key(instrument_id, route_fingerprint),
        )
    except Exception as exc:
        increment_metric(
            "gex_dividend_cache_io_total",
            operation="read",
            status="failed",
            error=exc.__class__.__name__,
        )
        return {
            "cached": False,
            "cache_state": "error",
            "source": "storage",
            "error_code": "GEX_DIVIDEND_CACHE_READ_FAILED",
            "reason": exception_message(exc),
        }
    if not isinstance(raw, dict):
        return None
    value = finite_number_or_none(raw.get("value"))
    updated_at = parse_gex_timestamp(raw.get("updated_at"))
    if value is None or not _valid_broker_dividend_yield(value) or updated_at is None:
        return None
    if datetime.now(tz=UTC) - updated_at > timedelta(days=max_age_days):
        return None
    return {**raw, "value": value, "updated_at": updated_at.isoformat()}


def _cached_dividend_yield(
    instrument_id: str,
    route_fingerprint: str,
    *,
    max_age_days: int = 45,
    store: Any | None = None,
) -> float | None:
    payload = _cached_dividend_payload(
        instrument_id,
        route_fingerprint,
        max_age_days=max_age_days,
        store=store,
    )
    return finite_number_or_none(payload.get("value")) if payload else None


def _valid_broker_dividend_yield(value: float | None) -> bool:
    number = finite_number_or_none(value)
    return number is not None and 0.0001 <= number <= _GEX_DIVIDEND_YIELD_BROKER_MAX


def _dividend_yield_for_gex_request(
    request: _GexDividendRequest,
    *,
    configured_override: bool = False,
    store: Any | None = None,
) -> dict[str, Any]:
    if request.futures_options:
        return {
            "cached": True,
            "source": "futures_options",
            "value": 0.0,
            "reason": "futures options",
        }
    if configured_override:
        return {
            "cached": False,
            "source": "env",
            "value": request.dividend_yield,
            "reason": "env override",
        }
    cached = _cached_dividend_payload(
        request.instrument_id,
        request.route_fingerprint,
        max_age_days=_GEX_DIVIDEND_YIELD_CACHE_DAYS,
        store=store,
    )
    if cached is not None and cached.get("cache_state") == "error":
        return {
            **cached,
            "cached": False,
            "source": "config",
            "value": request.dividend_yield,
        }
    if cached is not None:
        return {
            "cached": True,
            "cache_state": "fresh",
            "source": str(cached.get("source") or "broker_pv_dividend"),
            **cached,
            "reason": "fresh",
        }
    return {
        "cached": False,
        "cache_state": "missing",
        "source": "config",
        "value": request.dividend_yield,
        "reason": "no fresh broker cache",
    }


def _dividend_yield_from_pv_dividend(
    *,
    spot: float,
    years: float,
    pv_dividend: float | None,
) -> float | None:
    spot_value = finite_number_or_none(spot)
    years_value = finite_number_or_none(years)
    pv_value = finite_number_or_none(pv_dividend)
    if spot_value is None or years_value is None or pv_value is None:
        return None
    if spot_value <= 0 or years_value < _GEX_DIVIDEND_YIELD_MIN_YEARS or pv_value <= 0:
        return None
    ratio = pv_value / spot_value
    if not 0.0 < ratio < 0.25:
        return None
    annualized = -math.log(1.0 - ratio) / years_value
    if _valid_broker_dividend_yield(annualized):
        return annualized
    return None


def _maybe_update_dividend_yield_cache(
    provider_symbol: str,
    contract_rows: Sequence[dict[str, Any]],
    *,
    instrument_id: str,
    route_fingerprint: str,
    now: datetime | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    estimates = [
        value
        for row in contract_rows
        if (value := finite_number_or_none(row.get("broker_dividend_yield"))) is not None
        and _valid_broker_dividend_yield(value)
    ]
    estimate = _median_positive(estimates)
    if estimate is None:
        return {
            "cached": False,
            "code": GEX_DIVIDEND_ESTIMATE_UNAVAILABLE,
            "source": "broker_pv_dividend",
            "reason": "no reliable pvDividend estimate",
        }
    now_utc = (now or datetime.now(tz=UTC)).astimezone(UTC)
    key = _dividend_yield_setting_key(instrument_id, route_fingerprint)
    if store is None:
        return {
            "cached": False,
            "code": GEX_DIVIDEND_STORAGE_UNAVAILABLE,
            "source": "broker_pv_dividend",
            "value": estimate,
            "reason": "storage unavailable",
        }
    with _gex_dividend_cache_lock(instrument_id, route_fingerprint):
        try:
            raw = store.read_setting("server", key)
            if isinstance(raw, dict):
                updated_at = parse_gex_timestamp(raw.get("updated_at"))
                cached_value = finite_number_or_none(raw.get("value"))
                if (
                    updated_at is not None
                    and now_utc - updated_at < timedelta(days=_GEX_DIVIDEND_YIELD_CACHE_DAYS)
                    and _valid_broker_dividend_yield(cached_value)
                ):
                    assert cached_value is not None
                    return {
                        "cached": True,
                        "code": "GEX_DIVIDEND_CACHE_FRESH",
                        "source": "broker_pv_dividend",
                        "value": cached_value,
                        "updated_at": updated_at.isoformat(),
                        "reason": "fresh",
                    }
            payload = {
                "provider_symbol": require_exact_identity_text(
                    provider_symbol,
                    field="provider_symbol",
                ),
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "value": round(float(estimate), 6),
                "source": "broker_pv_dividend",
                "updated_at": now_utc.isoformat(),
                "sample_count": len(estimates),
            }
            store.upsert_setting("server", key, payload)
            return {"cached": True, "code": "GEX_DIVIDEND_CACHE_UPDATED", **payload}
        except Exception as exc:
            increment_metric(
                "gex_dividend_cache_io_total",
                operation="write",
                status="failed",
                error=exc.__class__.__name__,
            )
            return {
                "cached": False,
                "code": "GEX_DIVIDEND_CACHE_WRITE_FAILED",
                "cache_state": "error",
                "source": "broker_pv_dividend",
                "value": estimate,
                "error_code": "GEX_DIVIDEND_CACHE_WRITE_FAILED",
                "reason": exception_message(exc),
            }
