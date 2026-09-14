from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.contracts import GexCaptureMode, gex_capture_lane
from aef_terminal.data.instrument_identity import (
    provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    route_fingerprint,
)
from aef_terminal.indicators.registry import (
    enabled_confirmed_bar_context_requests,
)
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    chart_bars_updated_generation,
    require_chart_bars_generation,
)
from aef_terminal.runtime.metrics import increment_metric
from aef_terminal.runtime.telemetry import log_structured_error
from aef_terminal.settings_contract import (
    instrument_paper_auto_trading_setting_key,
)
from aef_terminal.ui.routers.error_payloads import build_error_payload
from aef_terminal.ui.services.market_analysis_job import run_market_analysis_job
from aef_terminal.ui.services.market_analysis_process import (
    MarketAnalysisRequestController,
    bind_market_analysis_request_controller,
    market_analysis_process_worker_count,
)
from aef_terminal.ui.services.market_analysis_research_worker import (
    MarketAnalysisResearchWorker,
)
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
    MARKET_ANALYSIS_EFFECTS_STANDARD,
    build_market_analysis_error_item,
    build_market_analysis_snapshot_response,
    decode_market_analysis_cache_snapshot,
    market_analysis_cache_item_size,
    market_analysis_effects,
    market_analysis_key,
    market_analysis_refresh_seconds,
    market_analysis_requires_periodic_refresh,
    market_analysis_request_identity,
    register_market_analysis_wanted,
    revoke_market_analysis_wanted_locked,
    trim_market_analysis_cache,
)
from aef_terminal.ui.services.market_analysis_worker import (
    run_market_analysis_worker_loop,
)

MARKET_ANALYSIS_CACHE_TTL_SECONDS = 45.0
MARKET_ANALYSIS_CACHE_MAX_ENTRIES = 8
MARKET_ANALYSIS_CACHE_MAX_BYTES = 32 * 1024 * 1024
MARKET_ANALYSIS_REFRESH_SECONDS = 10.0
MARKET_ANALYSIS_VERSIONED_REFRESH_SECONDS = 60.0
MARKET_ANALYSIS_WANTED_TTL_SECONDS = 45.0
MARKET_ANALYSIS_GENERATION_SETTLE_SECONDS = 0.5
MARKET_ANALYSIS_CLIENT_HIGHWATER_TTL_SECONDS = 300.0

_LOGGER = logging.getLogger("aef_terminal.ui.market_analysis_runtime")


class _MarketAnalysisStateLock:
    """Serialize loop mutations and bounded synchronous read projections."""

    def __init__(self) -> None:
        self._async_lock = asyncio.Lock()
        self._thread_lock = threading.RLock()

    async def __aenter__(self) -> _MarketAnalysisStateLock:
        await self._async_lock.acquire()
        self._thread_lock.acquire()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: Any,
    ) -> None:
        self._thread_lock.release()
        self._async_lock.release()

    def __enter__(self) -> _MarketAnalysisStateLock:
        self._thread_lock.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: Any,
    ) -> None:
        self._thread_lock.release()


@dataclass(frozen=True)
class MarketAnalysisRuntimeDeps:
    client_settings_snapshot: Callable[[], dict[str, Any]]
    store_factory: Callable[[], Any] | None = None
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]] | None = None
    reconcile_confirmed_bar_demands: (
        Callable[
            [tuple[dict[str, Any], ...]],
            Awaitable[None],
        ]
        | None
    ) = None
    shutdown_confirmed_bar_demands: Callable[[], Awaitable[None]] | None = None


class MarketAnalysisRuntimeService:
    """Canonical process owner for market-analysis demand, jobs, and cache."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = MARKET_ANALYSIS_CACHE_TTL_SECONDS,
        cache_max_entries: int = MARKET_ANALYSIS_CACHE_MAX_ENTRIES,
        cache_max_bytes: int = MARKET_ANALYSIS_CACHE_MAX_BYTES,
        refresh_seconds: float = MARKET_ANALYSIS_REFRESH_SECONDS,
        versioned_refresh_seconds: float = (MARKET_ANALYSIS_VERSIONED_REFRESH_SECONDS),
        wanted_ttl_seconds: float = MARKET_ANALYSIS_WANTED_TTL_SECONDS,
        generation_settle_seconds: float = (MARKET_ANALYSIS_GENERATION_SETTLE_SECONDS),
    ) -> None:
        self.cache_max_entries = max(int(cache_max_entries), 1)
        self.cache_max_bytes = max(int(cache_max_bytes), 1)
        self.refresh_seconds = max(float(refresh_seconds), 0.1)
        self.versioned_refresh_seconds = max(
            float(versioned_refresh_seconds),
            self.refresh_seconds,
        )
        self.cache_ttl_seconds = max(
            float(cache_ttl_seconds),
            self.versioned_refresh_seconds,
        )
        self.wanted_ttl_seconds = max(float(wanted_ttl_seconds), 0.1)
        self.generation_settle_seconds = max(
            float(generation_settle_seconds),
            0.0,
        )
        self._cache: dict[str, dict[str, Any]] = {}
        self._research_worker = MarketAnalysisResearchWorker()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_identities: dict[str, tuple[Any, ...]] = {}
        self._request_controllers: dict[
            str,
            tuple[asyncio.Task[Any], MarketAnalysisRequestController],
        ] = {}
        self._wanted: dict[str, dict[str, Any]] = {}
        self._client_leases_by_key: dict[
            str,
            dict[str, tuple[int, float]],
        ] = {}
        self._analysis_key_by_client: dict[str, str] = {}
        self._client_lease_highwater: dict[str, tuple[int, float]] = {}
        self._confirmed_context_generations: dict[
            tuple[str, str, str],
            int,
        ] = {}
        self._worker_wake: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._lock = _MarketAnalysisStateLock()
        self._deps: MarketAnalysisRuntimeDeps | None = None

    def configure(self, deps: MarketAnalysisRuntimeDeps) -> None:
        self._deps = deps

    def _require_deps(self) -> MarketAnalysisRuntimeDeps:
        if self._deps is None:
            raise RuntimeError("market analysis runtime dependencies are not configured")
        return self._deps

    def analysis_key(self, payload: dict[str, Any]) -> str:
        return market_analysis_key(payload)

    def analysis_request_identity(
        self,
        payload: dict[str, Any],
    ) -> tuple[Any, ...]:
        return market_analysis_request_identity(payload)

    def analysis_refresh_seconds(self, payload: dict[str, Any]) -> float:
        return market_analysis_refresh_seconds(
            payload,
            default_seconds=self.refresh_seconds,
        )

    def analysis_requires_periodic_refresh(
        self,
        payload: dict[str, Any],
    ) -> bool:
        return market_analysis_requires_periodic_refresh(payload)

    def build_analysis_payload(
        self,
        *,
        source: str,
        instrument_id: str,
        interval: str,
        range_: str,
        signal_range: str,
        show_visuals: bool,
        indicator_params: dict[str, Any],
        gex_context_active: bool = False,
        gex_capture_mode: GexCaptureMode = "request",
        include_telemetry: bool = False,
        market_version: dict[str, Any] | None = None,
        parent_canonical_generation: int | None = None,
        instrument: dict[str, Any] | None = None,
        analysis_effects: str = MARKET_ANALYSIS_EFFECTS_STANDARD,
    ) -> dict[str, Any]:
        if type(gex_context_active) is not bool:
            raise TypeError("GEX context active state must be a boolean")
        _, exact_gex_capture_mode = gex_capture_lane(gex_capture_mode)
        exact_analysis_effects = market_analysis_effects({"analysis_effects": analysis_effects})
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        if not isinstance(instrument, dict):
            raise ValueError("market analysis requires a qualified instrument")
        if qualified_instrument_id(instrument) != exact_instrument_id:
            raise ValueError("market analysis instrument_id does not match qualified instrument")
        fingerprint = route_fingerprint(instrument)
        captured_parent_generation = (
            chart_bars_updated_generation(
                interval,
                fingerprint,
                instrument_id=exact_instrument_id,
            )
            if parent_canonical_generation is None
            else parent_canonical_generation
        )
        if (
            isinstance(captured_parent_generation, bool)
            or not isinstance(captured_parent_generation, int)
            or captured_parent_generation < 0
        ):
            raise ValueError("parent_canonical_generation must be a non-negative integer")
        payload = {
            "source": source,
            "instrument_id": exact_instrument_id,
            "route_fingerprint": fingerprint,
            "interval": interval,
            "range": range_,
            "signal_range": signal_range,
            "show_visuals": show_visuals,
            "indicator_params": indicator_params,
            "gex_context_active": gex_context_active,
            "gex_capture_mode": exact_gex_capture_mode,
            "include_telemetry": include_telemetry,
            "parent_canonical_generation": captured_parent_generation,
            "instrument": dict(instrument),
        }
        if market_version:
            payload["market_version"] = market_version
        if exact_analysis_effects != MARKET_ANALYSIS_EFFECTS_STANDARD:
            payload["analysis_effects"] = exact_analysis_effects
        return payload

    def _trim_cache_locked(self, now: float) -> None:
        trim_market_analysis_cache(
            self._cache,
            ttl_seconds=self.cache_ttl_seconds,
            max_size=self.cache_max_entries,
            max_bytes=self.cache_max_bytes,
            now=now,
            protected_keys=frozenset(self._wanted),
        )

    def auto_paper_trading_enabled(self, instrument_id: str) -> bool:
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        try:
            settings = self._require_deps().client_settings_snapshot()
        except Exception:
            _LOGGER.exception(
                "auto paper trading disabled; client settings read failed for instrument_id=%s",
                exact_instrument_id,
            )
            return False
        if not isinstance(settings, dict):
            _LOGGER.error(
                "auto paper trading disabled; client settings payload is "
                "invalid for instrument_id=%s",
                exact_instrument_id,
            )
            return False
        return (
            settings.get(instrument_paper_auto_trading_setting_key(exact_instrument_id)) == "true"
        )

    def _payload_with_confirmed_context_generations_locked(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        prepared = dict(payload)
        instrument_id = require_exact_identity_text(
            payload.get("instrument_id"),
            field="instrument_id",
        )
        fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="route_fingerprint",
        )
        parent_timeframe = require_exact_identity_text(
            payload.get("interval"),
            field="timeframe",
        )
        parent_generation = payload.get("parent_canonical_generation")
        if parent_generation is None:
            parent_generation = chart_bars_updated_generation(
                parent_timeframe,
                fingerprint,
                instrument_id=instrument_id,
            )
        if (
            isinstance(parent_generation, bool)
            or not isinstance(parent_generation, int)
            or parent_generation < 0
        ):
            raise ValueError("parent_canonical_generation must be a non-negative integer")
        prepared["parent_canonical_generation"] = parent_generation
        requests = enabled_confirmed_bar_context_requests(
            payload.get("indicator_params")
            if isinstance(payload.get("indicator_params"), dict)
            else {}
        )
        if not requests:
            prepared.pop("confirmed_bar_context_generations", None)
            return prepared
        generations: dict[str, int] = {}
        for timeframe in sorted(requests):
            context_key = (instrument_id, fingerprint, timeframe)
            generation = max(
                self._confirmed_context_generations.get(context_key, 0),
                chart_bars_updated_generation(
                    timeframe,
                    fingerprint,
                    instrument_id=instrument_id,
                ),
            )
            self._confirmed_context_generations[context_key] = generation
            generations[timeframe] = generation
        prepared["confirmed_bar_context_generations"] = generations
        return prepared

    def _captured_context_is_current_locked(
        self,
        payload: dict[str, Any],
    ) -> bool:
        instrument_id = require_exact_identity_text(
            payload.get("instrument_id"),
            field="instrument_id",
        )
        fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="route_fingerprint",
        )
        parent_timeframe = str(payload.get("interval") or "").strip()
        parent_generation = payload.get("parent_canonical_generation")
        if (
            not parent_timeframe
            or isinstance(parent_generation, bool)
            or not isinstance(parent_generation, int)
            or parent_generation < 0
        ):
            return False
        try:
            require_chart_bars_generation(
                parent_generation,
                parent_timeframe,
                fingerprint,
                instrument_id=instrument_id,
            )
        except Exception:
            return False
        captured = payload.get("confirmed_bar_context_generations")
        if not isinstance(captured, dict):
            return True
        for timeframe, generation in captured.items():
            if isinstance(generation, bool) or not isinstance(
                generation,
                int,
            ):
                return False
            exact_timeframe = str(timeframe)
            try:
                current_generation = max(
                    self._confirmed_context_generations.get(
                        (
                            instrument_id,
                            fingerprint,
                            exact_timeframe,
                        ),
                        0,
                    ),
                    chart_bars_updated_generation(
                        exact_timeframe,
                        fingerprint,
                        instrument_id=instrument_id,
                    ),
                )
            except Exception:
                return False
            if generation != current_generation:
                return False
            try:
                require_chart_bars_generation(
                    generation,
                    exact_timeframe,
                    fingerprint,
                    instrument_id=instrument_id,
                )
            except Exception:
                return False
        return True

    def _captured_generation_values_match_locked(
        self,
        payload: dict[str, Any],
    ) -> tuple[bool, bool]:
        """Return exact parent and lower-context value matches, ignoring active writes."""

        instrument_id = require_exact_identity_text(
            payload.get("instrument_id"),
            field="instrument_id",
        )
        fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="route_fingerprint",
        )
        parent_timeframe = str(payload.get("interval") or "").strip()
        parent_generation = payload.get("parent_canonical_generation")
        parent_matches = bool(
            parent_timeframe
            and not isinstance(parent_generation, bool)
            and isinstance(parent_generation, int)
            and parent_generation >= 0
            and parent_generation
            == chart_bars_updated_generation(
                parent_timeframe,
                fingerprint,
                instrument_id=instrument_id,
            )
        )
        captured = payload.get("confirmed_bar_context_generations")
        if not isinstance(captured, dict):
            return parent_matches, True
        context_matches = True
        for timeframe, generation in captured.items():
            exact_timeframe = str(timeframe)
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                return parent_matches, False
            current_generation = max(
                self._confirmed_context_generations.get(
                    (instrument_id, fingerprint, exact_timeframe),
                    0,
                ),
                chart_bars_updated_generation(
                    exact_timeframe,
                    fingerprint,
                    instrument_id=instrument_id,
                ),
            )
            if generation != current_generation:
                context_matches = False
                break
        return parent_matches, context_matches

    def _captured_context_is_current(
        self,
        payload: dict[str, Any],
    ) -> bool:
        with self._lock:
            return self._captured_context_is_current_locked(payload)

    @staticmethod
    def _cache_matches_payload_context(
        cached: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        expected_parent_generation = payload.get("parent_canonical_generation")
        if (
            isinstance(expected_parent_generation, bool)
            or not isinstance(expected_parent_generation, int)
            or cached.get("parent_canonical_generation") != expected_parent_generation
        ):
            return False
        expected = payload.get("confirmed_bar_context_generations")
        if isinstance(expected, dict):
            actual = cached.get("confirmed_bar_context_generations")
            if not isinstance(actual, dict) or actual != expected:
                return False
        expected_market_version = payload.get("market_version")
        if not isinstance(expected_market_version, dict):
            return True
        actual_market_version = cached.get("market_version")
        if not isinstance(actual_market_version, dict):
            return False
        return {key: value for key, value in actual_market_version.items() if key != "client"} == {
            key: value for key, value in expected_market_version.items() if key != "client"
        }

    def _wake_worker(self) -> None:
        if self._worker_wake.empty():
            self._worker_wake.put_nowait(None)

    def _request_analysis_task_cancellation_locked(
        self,
        key: str,
        task: asyncio.Task[Any],
    ) -> MarketAnalysisRequestController | None:
        controller_entry = self._request_controllers.get(key)
        controller = (
            controller_entry[1]
            if controller_entry is not None and controller_entry[0] is task
            else None
        )
        controller_claimed = bool(controller is not None and controller.request_cancellation())
        task.cancel()
        task.add_done_callback(lambda _task: self._wake_worker())
        return controller if controller_claimed else None

    def _cancel_analysis_task_locked(
        self,
        key: str,
        task: asyncio.Task[Any],
    ) -> None:
        self._request_analysis_task_cancellation_locked(key, task)

    async def confirmed_bar_context_advanced(
        self,
        instrument_id: str,
        route_fingerprint_value: str,
        timeframe: str,
        generation: int,
    ) -> int:
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        exact_fingerprint = require_exact_identity_text(
            route_fingerprint_value,
            field="route_fingerprint",
        )
        if not isinstance(timeframe, str) or not timeframe or timeframe != timeframe.strip():
            raise ValueError("timeframe must be exact non-empty text")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("generation must be a positive integer")
        context_key = (
            exact_instrument_id,
            exact_fingerprint,
            timeframe,
        )
        affected = 0
        request_controllers: list[MarketAnalysisRequestController] = []
        async with self._lock:
            if generation <= self._confirmed_context_generations.get(
                context_key,
                0,
            ):
                return 0
            self._confirmed_context_generations[context_key] = generation
            for key, item in self._wanted.items():
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                if (
                    payload.get("instrument_id") != exact_instrument_id
                    or payload.get("route_fingerprint") != exact_fingerprint
                ):
                    continue
                parent_advanced = (
                    market_analysis_effects(payload) == MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE
                    and payload.get("interval") == timeframe
                )
                requests = enabled_confirmed_bar_context_requests(
                    payload.get("indicator_params")
                    if isinstance(payload.get("indicator_params"), dict)
                    else {}
                )
                if not parent_advanced and timeframe not in requests:
                    continue
                advanced_payload = dict(payload)
                if parent_advanced:
                    advanced_payload["parent_canonical_generation"] = generation
                item["payload"] = self._payload_with_confirmed_context_generations_locked(
                    advanced_payload
                )
                item["not_before"] = time.monotonic() + self.generation_settle_seconds
                self._cache.pop(key, None)
                task = self._tasks.get(key)
                if task is not None and not task.done():
                    controller = self._request_analysis_task_cancellation_locked(
                        key,
                        task,
                    )
                    if controller is not None:
                        request_controllers.append(controller)
                affected += 1
        if request_controllers:
            await asyncio.gather(
                *(controller.wait_finished() for controller in request_controllers)
            )
        if affected:
            self._wake_worker()
        return affected

    async def run_analysis_job(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> None:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("market analysis job requires a task owner")
        request_controller = MarketAnalysisRequestController()
        async with self._lock:
            registered_task = self._tasks.get(key)
            if registered_task is None or registered_task is current_task:
                self._request_controllers[key] = (
                    current_task,
                    request_controller,
                )
        private_tasks = {key: current_task}
        private_identities = {key: self.analysis_request_identity(payload)}
        private_cache: dict[str, dict[str, Any]] = {}
        item: dict[str, Any] | None = None
        generation_race = False

        def on_analysis_error(job_key: str, exc: BaseException) -> None:
            log_structured_error(
                _LOGGER,
                provider="market_analysis",
                symbol=provider_symbol(
                    payload["instrument"],
                    str(payload["source"]),
                ),
                instrument_id=payload["instrument_id"],
                interval=str(payload.get("interval") or ""),
                range_=str(payload.get("range") or ""),
                op="market_analysis_job",
                error=exc,
                analysis_key=job_key,
            )

        def auto_paper_trading_admitted(instrument_id: str) -> bool:
            return self._captured_context_is_current(payload) and self.auto_paper_trading_enabled(
                instrument_id
            )

        def submit_research_capture_admitted(
            scope: str,
            capture_bytes: bytes,
        ) -> None:
            with self._lock:
                wanted_item = self._wanted.get(key)
                wanted_payload = (
                    wanted_item.get("payload") if isinstance(wanted_item, dict) else None
                )
                admitted = (
                    isinstance(wanted_payload, dict)
                    and self._captured_context_is_current_locked(payload)
                    and self._cache_matches_payload_context(
                        payload,
                        wanted_payload,
                    )
                )
            if admitted:
                self._research_worker.submit(scope, capture_bytes)

        try:
            runtime_deps = self._deps
            with bind_market_analysis_request_controller(request_controller):
                await run_market_analysis_job(
                    key,
                    payload,
                    lock=self._lock,
                    tasks=private_tasks,
                    identities=private_identities,
                    wanted=self._wanted,
                    cache=private_cache,
                    trim_cache=lambda _now: None,
                    on_error=on_analysis_error,
                    auto_paper_trading_enabled=(auto_paper_trading_admitted),
                    submit_research_capture=(submit_research_capture_admitted),
                    store_factory=(
                        runtime_deps.store_factory if runtime_deps is not None else None
                    ),
                    normalize_drawing_anchors=(
                        runtime_deps.normalize_drawing_anchors if runtime_deps is not None else None
                    ),
                )
            item = private_cache.get(key)
        except asyncio.CancelledError:
            raise
        except ChartBarsGenerationChanged:
            generation_race = True
            item = None
        except Exception as exc:
            try:
                on_analysis_error(key, exc)
            except Exception:
                _LOGGER.exception(
                    "market analysis fallback telemetry failed for key %s",
                    key,
                )
            item = build_market_analysis_error_item(
                exc,
                instrument_id=payload["instrument_id"],
                route_fingerprint=payload["route_fingerprint"],
            )
        finally:
            async with self._lock:
                registered_task = self._tasks.get(key)
                owns_registration = registered_task is None or registered_task is current_task
                if owns_registration:
                    self._tasks.pop(key, None)
                    self._task_identities.pop(key, None)
                controller_entry = self._request_controllers.get(key)
                if controller_entry is not None and controller_entry[0] is current_task:
                    self._request_controllers.pop(key, None)
                generation_current = self._captured_context_is_current_locked(payload)
                (
                    parent_generation_matches,
                    context_generations_match,
                ) = self._captured_generation_values_match_locked(payload)
                wanted_item = self._wanted.get(key)
                wanted_payload = (
                    wanted_item.get("payload") if isinstance(wanted_item, dict) else None
                )
                request_current = isinstance(
                    wanted_payload, dict
                ) and self._cache_matches_payload_context(
                    payload,
                    wanted_payload,
                )
                if (
                    owns_registration
                    and generation_current
                    and request_current
                    and item is not None
                    and key in self._wanted
                ):
                    item["confirmed_bar_context_generations"] = dict(
                        payload.get("confirmed_bar_context_generations")
                        if isinstance(
                            payload.get("confirmed_bar_context_generations"),
                            dict,
                        )
                        else {}
                    )
                    self._cache[key] = item
                    self._trim_cache_locked(time.monotonic())
                elif owns_registration and not generation_current:
                    self._cache.pop(key, None)
                    if request_current and key in self._wanted:
                        if not parent_generation_matches:
                            # Parent bars define market_version.  A new chart
                            # registration must supersede this stale payload.
                            self._wanted.pop(key, None)
                        else:
                            # Lower contexts are analysis-only.  Refresh their
                            # captured generations, or retry after a transient
                            # active-write window without changing market_version.
                            wanted_item = self._wanted.get(key)
                            if isinstance(wanted_item, dict):
                                if not context_generations_match:
                                    wanted_item["payload"] = (
                                        self._payload_with_confirmed_context_generations_locked(
                                            payload
                                        )
                                    )
                                wanted_item["not_before"] = (
                                    time.monotonic() + self.generation_settle_seconds
                                )
                elif (
                    owns_registration
                    and generation_race
                    and request_current
                    and key in self._wanted
                ):
                    self._cache.pop(key, None)
                    self._wanted[key]["not_before"] = (
                        time.monotonic() + self.generation_settle_seconds
                    )
            self._wake_worker()

    async def register_wanted(
        self,
        key: str,
        payload: dict[str, Any],
        client_id: str,
        lease_sequence: int,
    ) -> str:
        exact_client_id = require_exact_identity_text(
            client_id,
            field="analysis_client_id",
        )
        if (
            isinstance(lease_sequence, bool)
            or not isinstance(lease_sequence, int)
            or lease_sequence <= 0
        ):
            raise ValueError("analysis_lease_sequence must be a positive integer")
        status = await register_market_analysis_wanted(
            key,
            payload,
            lock=self._lock,
            wanted=self._wanted,
            cache=self._cache,
            tasks=self._tasks,
            identities=self._task_identities,
            request_identity=self.analysis_request_identity,
            cache_ttl_seconds=self.cache_ttl_seconds,
            cache_max_size=self.cache_max_entries,
            cache_max_bytes=self.cache_max_bytes,
            prepare_payload=(self._payload_with_confirmed_context_generations_locked),
            cache_admitted=self._cache_matches_payload_context,
            cancel_task=self._cancel_analysis_task_locked,
            on_payload_superseded=lambda _key: increment_metric(
                "market_analysis_payload_superseded_total",
            ),
            registration_admitted_locked=lambda registered_key, now: (
                self._client_registration_admitted_locked(
                    exact_client_id,
                    registered_key,
                    lease_sequence,
                    now,
                )
            ),
            on_registered_locked=lambda registered_key, now: self._attach_client_lease_locked(
                exact_client_id,
                registered_key,
                lease_sequence,
                now,
            ),
        )
        await self._reconcile_confirmed_bar_demands(active=True)
        return status

    def _drop_orphaned_client_leases_locked(self) -> None:
        active_keys = set(self._wanted)
        for key in tuple(self._client_leases_by_key):
            if key in active_keys:
                continue
            clients = self._client_leases_by_key.pop(key, {})
            for client_id in clients:
                if self._analysis_key_by_client.get(client_id) == key:
                    self._analysis_key_by_client.pop(client_id, None)

    def _prune_client_highwaters_locked(self, now: float) -> None:
        expired = [
            client_id
            for client_id, (_sequence, updated_at) in (self._client_lease_highwater.items())
            if client_id not in self._analysis_key_by_client
            and now - updated_at > MARKET_ANALYSIS_CLIENT_HIGHWATER_TTL_SECONDS
        ]
        for client_id in expired:
            self._client_lease_highwater.pop(client_id, None)

    def _client_registration_admitted_locked(
        self,
        client_id: str,
        key: str,
        lease_sequence: int,
        now: float,
    ) -> bool:
        self._prune_client_highwaters_locked(now)
        highwater = self._client_lease_highwater.get(client_id)
        if highwater is None or lease_sequence > highwater[0]:
            return True
        return lease_sequence == highwater[0] and self._analysis_key_by_client.get(client_id) == key

    def _detach_client_lease_locked(
        self,
        client_id: str,
        *,
        revoke_last: bool,
        expected_sequence: int | None = None,
    ) -> bool:
        key = self._analysis_key_by_client.get(client_id)
        if key is None:
            return False
        clients = self._client_leases_by_key.get(key)
        lease = clients.get(client_id) if clients is not None else None
        if expected_sequence is not None and (lease is None or lease[0] != expected_sequence):
            return False
        self._analysis_key_by_client.pop(client_id, None)
        if clients is None:
            return True
        clients.pop(client_id, None)
        if not clients:
            self._client_leases_by_key.pop(key, None)
            if revoke_last:
                revoke_market_analysis_wanted_locked(
                    key,
                    wanted=self._wanted,
                    tasks=self._tasks,
                    identities=self._task_identities,
                    cancel_task=self._cancel_analysis_task_locked,
                )
        return True

    def _attach_client_lease_locked(
        self,
        client_id: str,
        key: str,
        lease_sequence: int,
        now: float,
    ) -> None:
        self._drop_orphaned_client_leases_locked()
        previous_key = self._analysis_key_by_client.get(client_id)
        if previous_key is not None and previous_key != key:
            self._detach_client_lease_locked(
                client_id,
                revoke_last=True,
            )
        self._analysis_key_by_client[client_id] = key
        self._client_leases_by_key.setdefault(key, {})[client_id] = (
            lease_sequence,
            now,
        )
        self._client_lease_highwater[client_id] = (lease_sequence, now)

    def _prune_client_leases_locked(self, now: float) -> None:
        self._drop_orphaned_client_leases_locked()
        self._prune_client_highwaters_locked(now)
        expired_clients = [
            client_id
            for client_id, key in self._analysis_key_by_client.items()
            if now - float((self._client_leases_by_key.get(key, {}).get(client_id) or (0, 0.0))[1])
            > self.wanted_ttl_seconds
        ]
        for client_id in expired_clients:
            self._detach_client_lease_locked(
                client_id,
                revoke_last=True,
            )

    async def renew_client_lease(
        self,
        client_id: str,
        lease_sequence: int,
        key: str,
        instrument_id: str,
        route_fingerprint_value: str,
    ) -> bool:
        exact_client_id = require_exact_identity_text(
            client_id,
            field="analysis_client_id",
        )
        exact_key = require_exact_identity_text(key, field="analysis_key")
        if (
            isinstance(lease_sequence, bool)
            or not isinstance(lease_sequence, int)
            or lease_sequence <= 0
        ):
            raise ValueError("analysis_lease_sequence must be a positive integer")
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        exact_fingerprint = require_exact_identity_text(
            route_fingerprint_value,
            field="route_fingerprint",
        )
        now = time.monotonic()
        changed = False
        async with self._lock:
            before = len(self._wanted)
            self._prune_client_leases_locked(now)
            changed = len(self._wanted) != before
            item = self._wanted.get(exact_key)
            payload = item.get("payload") if isinstance(item, dict) else None
            lease = self._client_leases_by_key.get(exact_key, {}).get(exact_client_id)
            if (
                self._analysis_key_by_client.get(exact_client_id) != exact_key
                or lease is None
                or lease[0] != lease_sequence
                or not isinstance(payload, dict)
                or payload.get("instrument_id") != exact_instrument_id
                or payload.get("route_fingerprint") != exact_fingerprint
            ):
                renewed = False
            else:
                self._client_leases_by_key[exact_key][exact_client_id] = (
                    lease_sequence,
                    now,
                )
                self._client_lease_highwater[exact_client_id] = (
                    lease_sequence,
                    now,
                )
                item["wanted_at"] = now
                renewed = True
        if changed:
            await self._reconcile_confirmed_bar_demands(active=True)
            self._wake_worker()
        return renewed

    async def refresh_client_analysis(
        self,
        client_id: str,
        lease_sequence: int,
        key: str,
        instrument_id: str,
        route_fingerprint_value: str,
        client_version: str,
    ) -> bool:
        """Refresh an existing exact-scope demand without rebuilding chart state."""

        exact_client_id = require_exact_identity_text(
            client_id,
            field="analysis_client_id",
        )
        exact_key = require_exact_identity_text(key, field="analysis_key")
        if (
            isinstance(lease_sequence, bool)
            or not isinstance(lease_sequence, int)
            or lease_sequence <= 0
        ):
            raise ValueError("analysis_lease_sequence must be a positive integer")
        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        exact_fingerprint = require_exact_identity_text(
            route_fingerprint_value,
            field="route_fingerprint",
        )
        exact_client_version = str(client_version or "").strip()
        if len(exact_client_version) > 512:
            raise ValueError("analysis client version is too long")

        now = time.monotonic()
        async with self._lock:
            self._prune_client_leases_locked(now)
            item = self._wanted.get(exact_key)
            payload = item.get("payload") if isinstance(item, dict) else None
            lease = self._client_leases_by_key.get(exact_key, {}).get(exact_client_id)
            if (
                self._analysis_key_by_client.get(exact_client_id) != exact_key
                or lease is None
                or lease[0] != lease_sequence
                or not isinstance(payload, dict)
                or payload.get("instrument_id") != exact_instrument_id
                or payload.get("route_fingerprint") != exact_fingerprint
            ):
                refreshed = False
            else:
                if exact_client_version:
                    market_version = (
                        dict(payload.get("market_version"))
                        if isinstance(payload.get("market_version"), dict)
                        else {}
                    )
                    market_version["client"] = exact_client_version
                    item["payload"] = {
                        **payload,
                        "market_version": market_version,
                    }
                item["wanted_at"] = now
                item["updated_at"] = datetime.now(tz=UTC).isoformat()
                self._client_leases_by_key[exact_key][exact_client_id] = (
                    lease_sequence,
                    now,
                )
                self._client_lease_highwater[exact_client_id] = (
                    lease_sequence,
                    now,
                )
                cached = self._cache.get(exact_key)
                cache_age = now - float((cached or {}).get("updated_monotonic") or 0.0)
                refresh_after = max(
                    self.versioned_refresh_seconds,
                    self.analysis_refresh_seconds(item["payload"]),
                )
                if (
                    cached is not None
                    and cached.get("status") == "ready"
                    and cache_age >= refresh_after
                ):
                    self._cache.pop(exact_key, None)
                refreshed = True
        if refreshed:
            self._wake_worker()
        return refreshed

    async def invalidate_provider_session_route(
        self,
        instrument_id: str,
        route_fingerprint_value: str,
    ) -> int:
        """Revoke exact-route analysis leases after provider session facts change.

        The analysis key includes the provider-qualified instrument payload.  A
        newly materialized provider schedule therefore requires clients to
        register a fresh key instead of rerunning an old payload under the old
        key.  Lease renewal is the existing browser recovery boundary for that
        transition.
        """

        exact_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        exact_fingerprint = require_exact_identity_text(
            route_fingerprint_value,
            field="route_fingerprint",
        )
        invalidated = 0
        async with self._lock:
            keys = [
                key
                for key, item in self._wanted.items()
                if isinstance(item.get("payload"), dict)
                and item["payload"].get("instrument_id") == exact_instrument_id
                and item["payload"].get("route_fingerprint") == exact_fingerprint
            ]
            for key in keys:
                for client_id in tuple(self._client_leases_by_key.get(key, {})):
                    if self._analysis_key_by_client.get(client_id) == key:
                        self._analysis_key_by_client.pop(client_id, None)
                self._client_leases_by_key.pop(key, None)
                revoke_market_analysis_wanted_locked(
                    key,
                    wanted=self._wanted,
                    tasks=self._tasks,
                    identities=self._task_identities,
                    cancel_task=self._cancel_analysis_task_locked,
                )
                self._cache.pop(key, None)
                invalidated += 1
        if invalidated:
            await self._reconcile_confirmed_bar_demands(active=True)
        return invalidated

    async def release_client_lease(
        self,
        client_id: str,
        lease_sequence: int,
    ) -> bool:
        exact_client_id = require_exact_identity_text(
            client_id,
            field="analysis_client_id",
        )
        if (
            isinstance(lease_sequence, bool)
            or not isinstance(lease_sequence, int)
            or lease_sequence <= 0
        ):
            raise ValueError("analysis_lease_sequence must be a positive integer")
        async with self._lock:
            current_key = self._analysis_key_by_client.get(exact_client_id)
            current_lease = self._client_leases_by_key.get(
                current_key or "",
                {},
            ).get(exact_client_id)
            highwater = self._client_lease_highwater.get(exact_client_id)
            current_sequence = current_lease[0] if current_lease else 0
            highest_sequence = highwater[0] if highwater else 0
            if max(current_sequence, highest_sequence) > lease_sequence:
                released = False
            else:
                if current_lease is not None:
                    self._detach_client_lease_locked(
                        exact_client_id,
                        revoke_last=True,
                    )
                self._client_lease_highwater[exact_client_id] = (
                    lease_sequence,
                    time.monotonic(),
                )
                released = True
        if released:
            await self._reconcile_confirmed_bar_demands(active=True)
            self._wake_worker()
        return released

    def _confirmed_bar_demand_payloads_locked(
        self,
    ) -> tuple[dict[str, Any], ...]:
        rows = [
            (
                item.get("payload"),
                float(item.get("wanted_at") or 0.0),
            )
            for item in self._wanted.values()
            if isinstance(item, dict) and isinstance(item.get("payload"), dict)
        ]
        newest_route_by_instrument: dict[str, tuple[float, str]] = {}
        newest_request_by_route_timeframe: dict[
            tuple[str, str, str],
            float,
        ] = {}
        for payload, wanted_at in rows:
            assert isinstance(payload, dict)
            instrument_id = payload.get("instrument_id")
            fingerprint = payload.get("route_fingerprint")
            interval = payload.get("interval")
            if (
                not isinstance(instrument_id, str)
                or not isinstance(fingerprint, str)
                or not isinstance(interval, str)
            ):
                continue
            current = newest_route_by_instrument.get(instrument_id)
            if current is None or wanted_at >= current[0]:
                newest_route_by_instrument[instrument_id] = (
                    wanted_at,
                    fingerprint,
                )
            route_timeframe = (
                instrument_id,
                fingerprint,
                interval,
            )
            newest_request_by_route_timeframe[route_timeframe] = max(
                wanted_at,
                newest_request_by_route_timeframe.get(
                    route_timeframe,
                    -1.0,
                ),
            )
        selected: list[dict[str, Any]] = []
        for payload, wanted_at in rows:
            if not isinstance(payload, dict):
                continue
            instrument_id = payload.get("instrument_id")
            fingerprint = payload.get("route_fingerprint")
            interval = payload.get("interval")
            if (
                not isinstance(instrument_id, str)
                or not isinstance(fingerprint, str)
                or not isinstance(interval, str)
            ):
                continue
            if (
                newest_route_by_instrument.get(
                    instrument_id,
                    (-1.0, ""),
                )[1]
                != fingerprint
            ):
                continue
            if wanted_at != newest_request_by_route_timeframe.get(
                (instrument_id, fingerprint, interval),
                -1.0,
            ):
                continue
            demand_payload = dict(payload)
            demand_payload.pop(
                "confirmed_bar_context_generations",
                None,
            )
            selected.append(demand_payload)
        return tuple(selected)

    async def _reconcile_confirmed_bar_demands(
        self,
        active: bool,
    ) -> None:
        deps = self._deps
        if deps is None:
            return
        reconcile = deps.reconcile_confirmed_bar_demands
        if reconcile is None:
            return
        if active:
            async with self._lock:
                payloads = self._confirmed_bar_demand_payloads_locked()
        else:
            payloads = ()
        await reconcile(payloads)

    async def worker_loop(
        self,
        *,
        server_sleeping: Callable[[], bool],
    ) -> None:
        await run_market_analysis_worker_loop(
            tick_seconds=0.25,
            wanted_ttl_seconds=self.wanted_ttl_seconds,
            max_concurrent=market_analysis_process_worker_count(),
            server_sleeping=server_sleeping,
            lock=self._lock,
            wanted=self._wanted,
            tasks=self._tasks,
            identities=self._task_identities,
            cache=self._cache,
            request_identity=self.analysis_request_identity,
            refresh_seconds=self.analysis_refresh_seconds,
            spawn_job=lambda key, payload: asyncio.create_task(self.run_analysis_job(key, payload)),
            versioned_refresh_seconds=self.versioned_refresh_seconds,
            periodic_refresh_required=(self.analysis_requires_periodic_refresh),
            reconcile_wanted=self._reconcile_confirmed_bar_demands,
            wake_queue=self._worker_wake,
            cancel_task=self._cancel_analysis_task_locked,
            prune_wanted_locked=self._prune_client_leases_locked,
        )

    async def snapshot(
        self,
        key: str = "",
        wait_seconds: float = 0.0,
        *,
        instrument_id: str = "",
        expected_route_fingerprint: str = "",
        vsa_render_hours: int = 6,
    ) -> Any:
        requested_instrument_id = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
        )
        requested_route_fingerprint = require_exact_identity_text(
            expected_route_fingerprint,
            field="route_fingerprint",
        )
        if not key:
            return build_market_analysis_snapshot_response(
                "",
                cached=None,
                task_running=False,
                instrument_id=requested_instrument_id,
                route_fingerprint=requested_route_fingerprint,
                vsa_render_hours=vsa_render_hours,
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(
            max(float(wait_seconds or 0.0), 0.0),
            30.0,
        )
        while True:
            async with self._lock:
                cached = self._cache.get(key)
                task = self._tasks.get(key)
                status_instrument_id = requested_instrument_id
                status_route_fingerprint = requested_route_fingerprint
                canonical_scope_known = False
                wanted_item = self._wanted.get(key)
                wanted_payload = (
                    wanted_item.get("payload") if isinstance(wanted_item, dict) else None
                )
                generation_scope = (
                    wanted_payload
                    if isinstance(wanted_payload, dict)
                    and "parent_canonical_generation" in wanted_payload
                    else cached
                    if isinstance(cached, dict)
                    and "parent_canonical_generation" in cached
                    and "interval" in cached
                    else None
                )
                if isinstance(
                    generation_scope, dict
                ) and not self._captured_context_is_current_locked(generation_scope):
                    self._cache.pop(key, None)
                    cached = None
                if isinstance(wanted_payload, dict):
                    wanted_identity = self.analysis_request_identity(wanted_payload)
                    status_instrument_id = str(wanted_identity[1])
                    status_route_fingerprint = str(wanted_identity[2])
                    canonical_scope_known = True
                elif key in self._task_identities:
                    task_identity = self._task_identities[key]
                    status_instrument_id = require_exact_identity_text(
                        task_identity[1],
                        field="instrument_id",
                    )
                    status_route_fingerprint = require_exact_identity_text(
                        task_identity[2],
                        field="route_fingerprint",
                    )
                    canonical_scope_known = True
                elif isinstance(cached, dict):
                    cached_instrument_id = cached.get("instrument_id")
                    cached_route_fingerprint = cached.get("route_fingerprint")
                    if cached_instrument_id and cached_route_fingerprint:
                        status_instrument_id = require_exact_identity_text(
                            cached_instrument_id,
                            field="instrument_id",
                        )
                        status_route_fingerprint = require_exact_identity_text(
                            cached_route_fingerprint,
                            field="route_fingerprint",
                        )
                        canonical_scope_known = True
                if canonical_scope_known:
                    if (
                        status_instrument_id != requested_instrument_id
                        or status_route_fingerprint != requested_route_fingerprint
                    ):
                        response = build_error_payload(
                            code="MARKET_ANALYSIS_SCOPE_MISMATCH",
                            category="analysis",
                            retryable=False,
                            error=("Analysis key does not match the requested instrument route."),
                            include_message_field=True,
                        )
                        response.update(
                            {
                                "status": "error",
                                "analysis_key": key,
                                "instrument_id": status_instrument_id,
                                "route_fingerprint": (status_route_fingerprint),
                            }
                        )
                        return response
                elif cached is not None or task is not None:
                    cached = None
                    task = None
                task_running = bool(task and not task.done())
                task_queued = key in self._wanted and cached is None
                response = build_market_analysis_snapshot_response(
                    key,
                    cached=cached,
                    task_running=task_running,
                    task_queued=task_queued,
                    instrument_id=status_instrument_id,
                    route_fingerprint=status_route_fingerprint,
                    vsa_render_hours=vsa_render_hours,
                )
            remaining = deadline - loop.time()
            if remaining <= 0 or (not task_running and not task_queued):
                return response
            if task_running and task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=remaining,
                    )
                except TimeoutError:
                    return response
                except asyncio.CancelledError:
                    current_waiter = asyncio.current_task()
                    if current_waiter is not None and current_waiter.cancelling():
                        return response
                    continue
                except Exception as exc:
                    async with self._lock:
                        registered_task = self._tasks.get(key)
                        if registered_task is not task:
                            continue
                        item = build_market_analysis_error_item(
                            exc,
                            instrument_id=status_instrument_id,
                            route_fingerprint=status_route_fingerprint,
                        )
                        self._tasks.pop(key, None)
                        self._task_identities.pop(key, None)
                        if key in self._wanted:
                            self._cache[key] = item
                            self._trim_cache_locked(time.monotonic())
                    log_structured_error(
                        _LOGGER,
                        provider="market_analysis",
                        symbol=status_instrument_id,
                        instrument_id=status_instrument_id,
                        interval="",
                        range_="",
                        op="market_analysis_snapshot_wait",
                        error=exc,
                        analysis_key=key,
                    )
                    return build_market_analysis_snapshot_response(
                        key,
                        cached=item,
                        task_running=False,
                        task_queued=False,
                        instrument_id=status_instrument_id,
                        route_fingerprint=status_route_fingerprint,
                        vsa_render_hours=vsa_render_hours,
                    )
            else:
                await asyncio.sleep(min(0.05, remaining))

    def diagnostics(self, *, timing_limit: int = 10) -> dict[str, Any]:
        with self._lock:
            cache_items = tuple((key, dict(item)) for key, item in self._cache.items())
            task_entries = len(self._tasks)
            running_tasks = sum(1 for task in self._tasks.values() if task and not task.done())
            wanted_entries = len(self._wanted)
            identity_entries = len(self._task_identities)
            client_leases = len(self._analysis_key_by_client)
            wanted_scopes = tuple(
                {
                    "key": key,
                    "instrument_id": payload.get("instrument_id"),
                    "route_fingerprint": payload.get("route_fingerprint"),
                    "source": payload.get("source"),
                    "interval": payload.get("interval"),
                    "gex_context_active": (payload.get("gex_context_active") is True),
                    "analysis_effects": market_analysis_effects(payload),
                    "periodic_refresh": (self.analysis_requires_periodic_refresh(payload)),
                    "refresh_seconds": self.analysis_refresh_seconds(payload),
                }
                for key, item in self._wanted.items()
                for payload in [item.get("payload")]
                if isinstance(payload, dict)
            )
        latest_timings: dict[str, Any] = {"top": []}
        for key, item in sorted(
            cache_items,
            key=lambda pair: float(pair[1].get("updated_monotonic") or 0.0),
            reverse=True,
        ):
            if item.get("status") != "ready":
                continue
            snapshot = decode_market_analysis_cache_snapshot(item)
            meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
            timings = meta.get("indicator_timings_ms")
            if not isinstance(timings, dict) or not timings:
                continue
            sorted_timings = sorted(
                (
                    (str(name), float(value))
                    for name, value in timings.items()
                    if isinstance(value, (int, float))
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
            latest_timings = {
                "key": key,
                "updated_at": item.get("updated_at"),
                "symbol": meta.get("symbol"),
                "timeframe": meta.get("timeframe"),
                "top": [
                    {
                        "indicator": name,
                        "ms": round(value, 3),
                    }
                    for name, value in sorted_timings[: max(int(timing_limit), 1)]
                ],
            }
            break
        return {
            "cache_entries": len(cache_items),
            "cache_bytes": sum(market_analysis_cache_item_size(item) for _key, item in cache_items),
            "wanted_entries": wanted_entries,
            "client_leases": client_leases,
            "wanted_scopes": wanted_scopes,
            "task_entries": task_entries,
            "running_tasks": running_tasks,
            "identity_entries": identity_entries,
            "cache_max_entries": self.cache_max_entries,
            "cache_max_bytes": self.cache_max_bytes,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "versioned_refresh_seconds": self.versioned_refresh_seconds,
            "latest_indicator_timings": latest_timings,
            "research_worker": self._research_worker.diagnostics(),
        }

    async def shutdown(self) -> None:
        request_controllers: list[MarketAnalysisRequestController] = []
        async with self._lock:
            tasks = tuple(
                task for task in self._tasks.values() if task is not None and not task.done()
            )
            self._wanted.clear()
            self._client_leases_by_key.clear()
            self._analysis_key_by_client.clear()
            self._client_lease_highwater.clear()
            for task in tasks:
                task_key = next(
                    (
                        key
                        for key, registered_task in self._tasks.items()
                        if registered_task is task
                    ),
                    None,
                )
                if task_key is None:
                    task.cancel()
                    continue
                controller = self._request_analysis_task_cancellation_locked(
                    task_key,
                    task,
                )
                if controller is not None:
                    request_controllers.append(controller)
        if request_controllers:
            await asyncio.gather(
                *(controller.wait_finished() for controller in request_controllers)
            )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._research_worker.shutdown()
        async with self._lock:
            self._tasks.clear()
            self._task_identities.clear()
            self._request_controllers.clear()
            self._cache.clear()
            self._confirmed_context_generations.clear()
            while not self._worker_wake.empty():
                self._worker_wake.get_nowait()
        shutdown_demands = (
            self._deps.shutdown_confirmed_bar_demands if self._deps is not None else None
        )
        if shutdown_demands is not None:
            await shutdown_demands()
