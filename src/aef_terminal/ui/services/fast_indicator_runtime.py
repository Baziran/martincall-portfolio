from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.analyze.indicator_runtime import run_pipeline_indicator
from aef_terminal.engine.indicator_adapters import indicator_execution_specs
from aef_terminal.engine.indicator_pipeline import execute_indicator_pipeline
from aef_terminal.indicators.defaults import indicator_defaults_from_params
from aef_terminal.indicators.modules.option_reversal import (
    OPTION_REVERSAL_BAR_TIMEFRAME,
    OPTION_REVERSAL_HISTORY_BARS,
)
from aef_terminal.indicators.runtime import IndicatorRunContext
from aef_terminal.indicators.runtime import IndicatorRuntimeParams
from aef_terminal.indicators.runtime_params import build_indicator_runtime_params
from aef_terminal.indicators.settings_resolution import (
    indicator_calc_enabled_from_settings,
)
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    chart_bars_updated_generation,
    require_chart_bars_generation,
)
from aef_terminal.runtime.stable_hash import stable_hash
from aef_terminal.ui.market_indicator_params import server_alert_indicator_params
from aef_terminal.ui.services.recent_confirmed_bars import (
    RecentConfirmedBarContext,
    recent_confirmed_bar_cache_stats,
    recent_confirmed_bar_context,
)


FAST_INDICATOR_MAX_SCOPES = 128
FAST_INDICATOR_MAX_PROJECTIONS = 512

FastScopeKey = tuple[str, str, str]
FastTargetKey = tuple[str, str, str]


@dataclass(frozen=True)
class FastIndicatorRuntimeDeps:
    store_factory: Callable[[], Any]
    client_settings_snapshot: Callable[[], dict[str, Any]]
    lookup_runtime_instrument: Callable[[str], dict[str, Any]]


@dataclass
class _ScopeState:
    revision: int = 0
    context_signature: str = ""
    bar_canonical_generation: int | None = None
    updated_at: str = ""
    projections: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class _CalculatedScope:
    context_signature: str
    bar_canonical_generation: int | None
    enabled: bool
    updated_at: str
    projections: dict[str, dict[str, Any]]


_DEPS: FastIndicatorRuntimeDeps | None = None
_STATE_LOCK = threading.RLock()
_EVENT_LOCK: asyncio.Lock | None = None
_SCOPES: dict[FastScopeKey, _ScopeState] = {}
_LAST_ERROR = ""
_COMMITTED_BATCHES = 0
_CALCULATED_TARGETS = 0
_TOTAL_CALCULATION_MS = 0.0
_LAST_BATCH_MS = 0.0
_MAX_BATCH_MS = 0.0
_PUBLICATION_GENERATION = 0


def configure_fast_indicator_runtime(deps: FastIndicatorRuntimeDeps) -> None:
    global _DEPS, _EVENT_LOCK, _LAST_ERROR, _COMMITTED_BATCHES, _CALCULATED_TARGETS
    global _TOTAL_CALCULATION_MS, _LAST_BATCH_MS, _MAX_BATCH_MS
    global _PUBLICATION_GENERATION
    if not isinstance(deps, FastIndicatorRuntimeDeps):
        raise TypeError("fast indicator runtime dependencies are invalid")
    with _STATE_LOCK:
        _DEPS = deps
        _EVENT_LOCK = None
        _SCOPES.clear()
        _LAST_ERROR = ""
        _COMMITTED_BATCHES = 0
        _CALCULATED_TARGETS = 0
        _TOTAL_CALCULATION_MS = 0.0
        _LAST_BATCH_MS = 0.0
        _MAX_BATCH_MS = 0.0
        _PUBLICATION_GENERATION = 0


def _deps() -> FastIndicatorRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("fast indicator runtime dependencies are not configured")
    return _DEPS


def _event_lock() -> asyncio.Lock:
    global _EVENT_LOCK
    if _EVENT_LOCK is None:
        _EVENT_LOCK = asyncio.Lock()
    return _EVENT_LOCK


def _target_scope(payload: Mapping[str, Any]) -> FastScopeKey:
    return (
        require_exact_identity_text(
            payload.get("instrument_id"),
            field="fast_indicator.instrument_id",
        ),
        require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="fast_indicator.route_fingerprint",
        ),
        require_exact_identity_text(
            payload.get("timeframe"),
            field="fast_indicator.timeframe",
        ),
    )


def _target_id(payload: Mapping[str, Any]) -> str:
    return require_exact_identity_text(
        payload.get("id"),
        field="fast_indicator.option_target_id",
    )


def _load_bar_context(
    *,
    store: Any,
    scope: FastScopeKey,
    instrument: dict[str, Any],
) -> RecentConfirmedBarContext:
    instrument_id, route_fingerprint, _target_timeframe = scope
    route = route_instrument(instrument)
    if (route.instrument_id, route.fingerprint) != (
        instrument_id,
        route_fingerprint,
    ):
        raise ValueError("fast indicator route does not match persisted Option Point")
    return recent_confirmed_bar_context(
        store=store,
        instrument=instrument,
        timeframe=OPTION_REVERSAL_BAR_TIMEFRAME,
        limit=OPTION_REVERSAL_HISTORY_BARS,
    )


def _fast_result(
    *,
    payload: dict[str, Any],
    scope: FastScopeKey,
    instrument: dict[str, Any],
    indicator_params: dict[str, Any],
    runtime_params: IndicatorRuntimeParams,
    bar_context: RecentConfirmedBarContext,
    analysis_as_of_utc: datetime,
) -> dict[str, Any]:
    bars = bar_context.bars
    latest = bars[-1] if bars else None
    route = route_instrument(instrument)
    context = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=latest,
        analysis_latest=latest,
        runtime_params=runtime_params,
        indicator_params=indicator_params,
        instrument_profile=resolve_instrument_profile(route.instrument),
        analysis_as_of_utc=analysis_as_of_utc,
        features={},
        instrument_id=scope[0],
        route_fingerprint=scope[1],
        provider=route.provider,
        option_targets=(payload,),
    )
    specs = indicator_execution_specs(
        context,
        stage="post_decision",
        lane="fast",
    )
    pipeline = execute_indicator_pipeline(
        specs,
        runner=run_pipeline_indicator,
        promoter=lambda _indicator_id, _indicator: [],
    )
    if pipeline.candidates:
        raise RuntimeError("fast indicator lane cannot promote candidates")
    result = dict(pipeline.indicators.get("option_reversal") or {})
    result.pop("research_observation", None)
    result["events"] = []
    result["signals"] = []
    result["execution"] = {
        "lane": "fast",
        "authority": "advisory_only",
        "trigger": "option_target_sample",
    }
    return result


def _calculate_committed_samples(
    committed: Mapping[FastTargetKey, dict[str, Any]],
    *,
    store: Any | None,
    analysis_as_of_utc: datetime,
) -> None:
    global _COMMITTED_BATCHES, _CALCULATED_TARGETS, _LAST_ERROR
    global _TOTAL_CALCULATION_MS, _LAST_BATCH_MS, _MAX_BATCH_MS
    global _PUBLICATION_GENERATION
    started = perf_counter()
    deps = _deps()
    runtime_store = store or deps.store_factory()
    if runtime_store is None:
        raise RuntimeError("fast indicator storage is not configured")
    settings = deps.client_settings_snapshot()
    grouped: dict[FastScopeKey, list[dict[str, Any]]] = {}
    for payload in committed.values():
        if not isinstance(payload, dict):
            continue
        grouped.setdefault(_target_scope(payload), []).append(payload)

    calculated_scopes: dict[FastScopeKey, _CalculatedScope] = {}
    for scope, targets in grouped.items():
        instrument = deps.lookup_runtime_instrument(scope[0])
        route = route_instrument(instrument)
        if route.fingerprint != scope[1]:
            raise ValueError("fast indicator route generation changed")
        enabled = indicator_calc_enabled_from_settings(
            settings,
            instrument_id=scope[0],
            indicator_id="option_reversal",
        )
        indicator_params = server_alert_indicator_params(settings)
        indicator_params["option_reversal"] = {"enabled": enabled}
        defaults = indicator_defaults_from_params(indicator_params.get("global_defaults"))
        runtime_params = build_indicator_runtime_params(
            indicator_params,
            defaults,
        )
        settings_signature = stable_hash(
            {
                "enabled": enabled,
                "defaults": defaults,
                "params": runtime_params.for_indicator("option_reversal"),
            }
        )
        bar_context = (
            _load_bar_context(
                store=runtime_store,
                scope=scope,
                instrument=instrument,
            )
            if enabled
            else None
        )
        context_signature = stable_hash(
            {
                "settings": settings_signature,
                "bars_generation": (bar_context.generation if bar_context is not None else None),
            }
        )
        calculated: dict[str, dict[str, Any]] = {}
        if enabled and bar_context is not None:
            for payload in targets:
                exact_target_id = _target_id(payload)
                result = _fast_result(
                    payload=payload,
                    scope=scope,
                    instrument=instrument,
                    indicator_params=indicator_params,
                    runtime_params=runtime_params,
                    bar_context=bar_context,
                    analysis_as_of_utc=analysis_as_of_utc,
                )
                calculated[exact_target_id] = {
                    "input_signature": stable_hash(payload),
                    "indicator": result,
                }

        calculated_scopes[scope] = _CalculatedScope(
            context_signature=context_signature,
            bar_canonical_generation=(
                bar_context.generation
                if bar_context is not None
                else chart_bars_updated_generation(
                    OPTION_REVERSAL_BAR_TIMEFRAME,
                    scope[1],
                    instrument_id=scope[0],
                )
            ),
            enabled=enabled,
            updated_at=analysis_as_of_utc.isoformat(),
            projections=calculated,
        )

    for scope, calculated_scope in calculated_scopes.items():
        generation = calculated_scope.bar_canonical_generation
        assert generation is not None
        require_chart_bars_generation(
            generation,
            OPTION_REVERSAL_BAR_TIMEFRAME,
            scope[1],
            instrument_id=scope[0],
        )

    with _STATE_LOCK:
        for scope, calculated_scope in calculated_scopes.items():
            state = _SCOPES.setdefault(scope, _ScopeState())
            if state.context_signature != calculated_scope.context_signature:
                state.projections.clear()
            state.context_signature = calculated_scope.context_signature
            state.bar_canonical_generation = calculated_scope.bar_canonical_generation
            if calculated_scope.enabled:
                state.projections.update(calculated_scope.projections)
            else:
                state.projections.clear()
            state.revision += 1
            state.updated_at = calculated_scope.updated_at
        elapsed_ms = (perf_counter() - started) * 1000.0
        _COMMITTED_BATCHES += 1
        _CALCULATED_TARGETS += sum(
            len(calculated_scope.projections) for calculated_scope in calculated_scopes.values()
        )
        _TOTAL_CALCULATION_MS += elapsed_ms
        _LAST_BATCH_MS = elapsed_ms
        _MAX_BATCH_MS = max(_MAX_BATCH_MS, elapsed_ms)
        _PUBLICATION_GENERATION += 1
        _LAST_ERROR = ""
        _evict_bounded_state_locked()


def _evict_bounded_state_locked() -> None:
    if len(_SCOPES) > FAST_INDICATOR_MAX_SCOPES:
        for scope, _state in sorted(
            _SCOPES.items(),
            key=lambda item: item[1].updated_at,
        )[: len(_SCOPES) - FAST_INDICATOR_MAX_SCOPES]:
            _SCOPES.pop(scope, None)
    projection_count = sum(len(state.projections) for state in _SCOPES.values())
    if projection_count <= FAST_INDICATOR_MAX_PROJECTIONS:
        return
    for _scope, state in sorted(
        _SCOPES.items(),
        key=lambda item: item[1].updated_at,
    ):
        while state.projections and projection_count > FAST_INDICATOR_MAX_PROJECTIONS:
            state.projections.pop(next(iter(state.projections)))
            projection_count -= 1


async def option_target_samples_committed(
    committed: Mapping[FastTargetKey, dict[str, Any]],
    *,
    store: Any | None = None,
) -> None:
    """Advance Fast lane only after a material Option Point sample commit."""

    global _LAST_ERROR
    if not committed:
        return
    async with _event_lock():
        analysis_as_of_utc = datetime.now(tz=UTC)
        try:
            await run_physical_thread_call(
                _calculate_committed_samples,
                dict(committed),
                store=store,
                analysis_as_of_utc=analysis_as_of_utc,
            )
        except Exception as exc:
            with _STATE_LOCK:
                _LAST_ERROR = f"{type(exc).__name__}: {exc}"
            raise


def _projection_rank(item: tuple[str, dict[str, Any]]) -> tuple[float, str]:
    target_id, projection = item
    indicator = projection.get("indicator")
    latest = indicator.get("latest") if isinstance(indicator, dict) else None
    metrics = latest.get("metrics") if isinstance(latest, dict) else None
    near_atr = metrics.get("near_atr") if isinstance(metrics, dict) else None
    distance = (
        float(near_atr)
        if not isinstance(near_atr, bool)
        and isinstance(near_atr, (int, float))
        and math.isfinite(float(near_atr))
        else float("inf")
    )
    return distance, target_id


def fast_indicator_snapshots(
    option_targets: Sequence[dict[str, Any]],
    interval: str,
) -> list[dict[str, Any]] | None:
    """Return exact projections, or ``None`` while the target stream is behind."""

    targets_by_scope: dict[FastScopeKey, dict[str, dict[str, Any]]] = {}
    for payload in option_targets:
        if not isinstance(payload, dict):
            continue
        scope = _target_scope(payload)
        if scope[2] != interval:
            raise ValueError("fast indicator target timeframe mismatch")
        targets_by_scope.setdefault(scope, {})[_target_id(payload)] = payload

    snapshots: list[dict[str, Any]] = []
    with _STATE_LOCK:
        for scope, current_targets in sorted(targets_by_scope.items()):
            state = _SCOPES.get(scope)
            if state is None:
                continue
            bar_generation = state.bar_canonical_generation
            if bar_generation is None:
                return None
            try:
                require_chart_bars_generation(
                    bar_generation,
                    OPTION_REVERSAL_BAR_TIMEFRAME,
                    scope[1],
                    instrument_id=scope[0],
                )
            except ChartBarsGenerationChanged:
                return None
            if any(
                target_id in current_targets
                and projection.get("input_signature") != stable_hash(current_targets[target_id])
                for target_id, projection in state.projections.items()
            ):
                return None
            matching = {
                target_id: deepcopy(projection)
                for target_id, projection in state.projections.items()
                if target_id in current_targets
                and projection.get("input_signature") == stable_hash(current_targets[target_id])
            }
            ranked = sorted(matching.items(), key=_projection_rank)
            aggregate = deepcopy(ranked[0][1].get("indicator")) if ranked else None
            snapshots.append(
                {
                    "instrument_id": scope[0],
                    "route_fingerprint": scope[1],
                    "timeframe": scope[2],
                    "bar_timeframe": OPTION_REVERSAL_BAR_TIMEFRAME,
                    "bar_canonical_generation": bar_generation,
                    "revision": state.revision,
                    "updated_at": state.updated_at,
                    "authority": "advisory_only",
                    "indicator_id": "option_reversal",
                    "indicator": aggregate,
                    "by_target_id": {
                        target_id: projection.get("indicator")
                        for target_id, projection in matching.items()
                    },
                }
            )
    return snapshots


def fast_indicator_runtime_status() -> dict[str, Any]:
    bar_cache = recent_confirmed_bar_cache_stats()
    with _STATE_LOCK:
        return {
            "configured": _DEPS is not None,
            "scopes": len(_SCOPES),
            "shared_bar_contexts": bar_cache["entries"],
            "projections": sum(len(state.projections) for state in _SCOPES.values()),
            "committed_batches": _COMMITTED_BATCHES,
            "calculated_targets": _CALCULATED_TARGETS,
            "publication_generation": _PUBLICATION_GENERATION,
            "shared_bar_context_loads": bar_cache["loads"],
            "last_batch_ms": round(_LAST_BATCH_MS, 3),
            "max_batch_ms": round(_MAX_BATCH_MS, 3),
            "average_target_ms": round(
                _TOTAL_CALCULATION_MS / max(_CALCULATED_TARGETS, 1),
                3,
            ),
            "last_error": _LAST_ERROR,
        }
