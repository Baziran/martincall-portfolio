from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import Bar, bar_revision_signature
from aef_terminal.runtime.stable_hash import stable_hash
from aef_terminal.features.context import FeatureContext, build_feature_context
from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.signals.primitives import SignalPrimitiveContext, build_signal_primitive_context


@dataclass(frozen=True)
class CachedAnalysisContext:
    feature_context: FeatureContext
    live_feature_context: FeatureContext
    structure_feature_context: FeatureContext
    primitives: SignalPrimitiveContext
    key: str


_CONTEXT_CACHE: OrderedDict[str, CachedAnalysisContext] = OrderedDict()
_CONTEXT_CACHE_TIMES: dict[str, float] = {}
_CONTEXT_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE_PENDING: dict[str, Future[CachedAnalysisContext]] = {}
_CONTEXT_CACHE_MAX = 8
_CONTEXT_CACHE_TTL_SECONDS = 90.0


def _settings_hash(value: Any) -> str:
    return stable_hash(value, length=12)


def _trim_context_cache_locked(now: float) -> None:
    expired = [
        key
        for key, updated_at in _CONTEXT_CACHE_TIMES.items()
        if now - float(updated_at or 0.0) > _CONTEXT_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _CONTEXT_CACHE.pop(key, None)
        _CONTEXT_CACHE_TIMES.pop(key, None)
    while len(_CONTEXT_CACHE) > _CONTEXT_CACHE_MAX:
        key, _ = _CONTEXT_CACHE.popitem(last=False)
        _CONTEXT_CACHE_TIMES.pop(key, None)


def analysis_context_cache_key(
    confirmed_bars: list[Bar],
    *,
    instrument_id: str,
    route_fingerprint: str,
    defaults: IndicatorDefaults,
    instrument_profile: InstrumentProfile,
    live_signal_bars: list[Bar] | None = None,
    structure_bars: list[Bar] | None = None,
    feature_extra: dict[str, Any] | None = None,
    structure_offset: int = 0,
) -> str:
    latest = confirmed_bars[-1] if confirmed_bars else None
    payload = {
        "instrument_id": require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        ),
        "route_fingerprint": require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
        ),
        "timeframe": latest.timeframe if latest else "",
        "confirmed_window": tuple(bar_revision_signature(bar) for bar in confirmed_bars),
        "live_signal_window": tuple(
            bar_revision_signature(bar) for bar in (live_signal_bars or ())
        ),
        "structure_window": tuple(bar_revision_signature(bar) for bar in (structure_bars or ())),
        "defaults": defaults,
        "instrument_profile": instrument_profile.key,
        "feature_extra": feature_extra or {},
        "structure_offset": structure_offset,
    }
    return _settings_hash(payload)


def get_cached_analysis_context(
    *,
    instrument_id: str,
    route_fingerprint: str,
    confirmed_bars: list[Bar],
    live_signal_bars: list[Bar],
    features_extra: dict[str, Any] | None,
    defaults: IndicatorDefaults,
    instrument_profile: InstrumentProfile,
    structure_bars: list[Bar],
    structure_offset: int,
) -> CachedAnalysisContext:
    """Reuse or coalesce a calculation on analysis workers, never on the live loop."""
    now = time.monotonic()
    key = analysis_context_cache_key(
        confirmed_bars,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        defaults=defaults,
        instrument_profile=instrument_profile,
        live_signal_bars=live_signal_bars,
        structure_bars=structure_bars,
        feature_extra=features_extra,
        structure_offset=structure_offset,
    )
    with _CONTEXT_CACHE_LOCK:
        _trim_context_cache_locked(now)
        cached = _CONTEXT_CACHE.get(key)
        if cached is not None:
            _CONTEXT_CACHE.move_to_end(key)
            _CONTEXT_CACHE_TIMES[key] = now
            return cached
        pending = _CONTEXT_CACHE_PENDING.get(key)
        owns_calculation = pending is None
        if pending is None:
            pending = Future()
            _CONTEXT_CACHE_PENDING[key] = pending

    if not owns_calculation:
        return pending.result()

    try:
        feature_context = build_feature_context(
            confirmed_bars,
            instrument_profile=instrument_profile,
            defaults=defaults,
            extra=features_extra,
        )
        live_feature_context = (
            feature_context
            if live_signal_bars == confirmed_bars
            else build_feature_context(
                live_signal_bars,
                instrument_profile=instrument_profile,
                defaults=defaults,
                extra=features_extra,
            )
        )
        primitives = build_signal_primitive_context(
            live_signal_bars=live_signal_bars,
            live_feature_context=live_feature_context,
        )
        structure_feature_context = (
            feature_context
            if structure_bars == confirmed_bars
            else build_feature_context(
                list(structure_bars),
                instrument_profile=instrument_profile,
                defaults=defaults,
            )
        )
        result = CachedAnalysisContext(
            feature_context=feature_context,
            live_feature_context=live_feature_context,
            structure_feature_context=structure_feature_context,
            primitives=primitives,
            key=key,
        )
    except BaseException as exc:
        with _CONTEXT_CACHE_LOCK:
            if _CONTEXT_CACHE_PENDING.get(key) is pending:
                del _CONTEXT_CACHE_PENDING[key]
        pending.set_exception(exc)
        raise

    with _CONTEXT_CACHE_LOCK:
        # clear() fences an in-flight build without abandoning its existing callers.
        if _CONTEXT_CACHE_PENDING.get(key) is pending:
            del _CONTEXT_CACHE_PENDING[key]
            now = time.monotonic()
            _trim_context_cache_locked(now)
            _CONTEXT_CACHE[key] = result
            _CONTEXT_CACHE_TIMES[key] = now
            _CONTEXT_CACHE.move_to_end(key)
            _trim_context_cache_locked(now)
    pending.set_result(result)
    return result


def clear_analysis_context_cache() -> None:
    with _CONTEXT_CACHE_LOCK:
        _CONTEXT_CACHE.clear()
        _CONTEXT_CACHE_TIMES.clear()
        _CONTEXT_CACHE_PENDING.clear()


def analysis_context_cache_stats() -> dict[str, Any]:
    now = time.monotonic()
    with _CONTEXT_CACHE_LOCK:
        _trim_context_cache_locked(now)
        ages = [now - float(updated_at or 0.0) for updated_at in _CONTEXT_CACHE_TIMES.values()]
        return {
            "entries": len(_CONTEXT_CACHE),
            "max_entries": _CONTEXT_CACHE_MAX,
            "ttl_seconds": _CONTEXT_CACHE_TTL_SECONDS,
            "oldest_age_seconds": round(max(ages), 3) if ages else 0.0,
        }
