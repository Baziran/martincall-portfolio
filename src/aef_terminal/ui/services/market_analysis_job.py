from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.indicators.registry import indicator_enabled
from aef_terminal.runtime.async_tasks import settle_physical_task, run_physical_thread_call
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    ChartBarsGenerationScope,
    begin_chart_bars_publication,
    chart_bars_updated_generation,
    end_chart_bars_publication,
    require_chart_bars_generation,
)
from aef_terminal.ui.services.market_analysis_process import (
    MarketAnalysisProcessResult,
    run_market_analysis_process,
)
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
    MarketAnalysisStateLock,
    build_market_analysis_error_item,
    market_analysis_effects,
)

_LOGGER = logging.getLogger(__name__)


def _analysis_bar_generation_scopes(
    payload: dict[str, Any],
    *,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
) -> tuple[ChartBarsGenerationScope, ...]:
    parent_generation = payload.get("parent_canonical_generation")
    if (
        isinstance(parent_generation, bool)
        or not isinstance(parent_generation, int)
        or parent_generation < 0
    ):
        raise ValueError("parent_canonical_generation must be a non-negative integer")
    scopes = [
        ChartBarsGenerationScope(
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            interval=timeframe,
            generation=parent_generation,
        )
    ]
    context_generations = payload.get(
        "confirmed_bar_context_generations",
        {},
    )
    if not isinstance(context_generations, dict):
        raise ValueError("confirmed_bar_context_generations must be an object")
    for context_timeframe, generation in sorted(context_generations.items()):
        if (
            not isinstance(context_timeframe, str)
            or not context_timeframe
            or context_timeframe != context_timeframe.strip()
        ):
            raise ValueError("confirmed bar context timeframe must be exact non-empty text")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("confirmed bar context generation must be a non-negative integer")
        scopes.append(
            ChartBarsGenerationScope(
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                interval=context_timeframe,
                generation=generation,
            )
        )
    return tuple(scopes)


def _require_analysis_bar_generations(
    payload: dict[str, Any],
    *,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
) -> int:
    """Fence every canonical bar scope read by one analysis-worker request."""

    scopes = _analysis_bar_generation_scopes(
        payload,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
    )
    for scope in scopes:
        require_chart_bars_generation(
            scope.generation,
            scope.interval,
            scope.route_fingerprint,
            instrument_id=scope.instrument_id,
        )
    return scopes[0].generation


async def hydrate_manual_channel_analysis_params(
    indicator_params: dict[str, Any],
    *,
    store: Any,
    instrument: dict[str, Any],
    interval: str,
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]],
    expected_generation: int | None = None,
) -> tuple[dict[str, Any], int | None]:
    """Attach canonical manual channels only at an analysis execution boundary."""

    prepared = dict(indicator_params)
    prepared.pop("manual_channels", None)
    prepared.pop("manual_channel_canonical_generation", None)
    if not indicator_enabled(prepared, "channel_master"):
        return prepared, None
    if not callable(normalize_drawing_anchors):
        raise RuntimeError("manual channel normalizer is required")
    route = route_instrument(instrument)
    generation = (
        chart_bars_updated_generation(
            interval,
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        if expected_generation is None
        else expected_generation
    )
    require_chart_bars_generation(
        generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    drawing_rows = await run_physical_thread_call(
        store.read_drawings,
        route.instrument_id,
        interval,
        route_fingerprint=route.fingerprint,
    )
    normalized = await run_physical_thread_call(
        normalize_drawing_anchors,
        store,
        route.instrument_id,
        interval,
        drawing_rows,
        instrument=route.instrument,
        provider=route.provider,
        route_fingerprint=route.fingerprint,
    )
    require_chart_bars_generation(
        generation,
        interval,
        route.fingerprint,
        instrument_id=route.instrument_id,
    )
    prepared["manual_channels"] = [
        item for item in normalized if isinstance(item, dict) and item.get("type") == "channel"
    ]
    prepared["manual_channel_canonical_generation"] = generation
    return prepared, generation


def _sync_paper_from_analysis_snapshot_bytes(
    key: str,
    snapshot_bytes: bytes,
    *,
    expected_instrument_id: object,
    expected_route_fingerprint: object,
    expected_timeframe: object,
    expected_provider: object,
    expected_provider_contract_id: object,
    generation_scopes: tuple[ChartBarsGenerationScope, ...],
    auto_paper_trading_enabled: Callable[[str], bool],
) -> dict[str, Any] | None:
    try:
        snapshot = json.loads(snapshot_bytes)
    except TypeError, ValueError:
        _LOGGER.exception("paper sync skipped invalid analysis snapshot for key %s", key)
        return
    if not isinstance(snapshot, dict):
        return
    trade_setup = (
        snapshot.get("trade_setup") if isinstance(snapshot.get("trade_setup"), dict) else {}
    )
    if str(trade_setup.get("action") or "").upper() != "GO":
        return
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    try:
        snapshot_identity = {
            "instrument_id": require_exact_identity_text(
                meta.get("instrument_id"),
                field="instrument_id",
            ),
            "route_fingerprint": require_exact_identity_text(
                meta.get("route_fingerprint"),
                field="route_fingerprint",
            ),
            "timeframe": require_exact_identity_text(
                meta.get("timeframe"),
                field="timeframe",
            ),
            "provider": require_exact_identity_text(
                meta.get("provider"),
                field="provider",
            ),
            "provider_contract_id": require_exact_identity_text(
                meta.get("provider_contract_id"),
                field="provider_contract_id",
            ),
        }
        expected_identity = {
            "instrument_id": require_exact_identity_text(
                expected_instrument_id,
                field="expected_instrument_id",
            ),
            "route_fingerprint": require_exact_identity_text(
                expected_route_fingerprint,
                field="expected_route_fingerprint",
            ),
            "timeframe": require_exact_identity_text(
                expected_timeframe,
                field="expected_timeframe",
            ),
            "provider": require_exact_identity_text(
                expected_provider,
                field="expected_provider",
            ),
            "provider_contract_id": require_exact_identity_text(
                expected_provider_contract_id,
                field="expected_provider_contract_id",
            ),
        }
    except ValueError as exc:
        raise ValueError("PAPER_ANALYSIS_EXECUTION_IDENTITY_INVALID") from exc
    for field, expected_value in expected_identity.items():
        snapshot_value = snapshot_identity[field]
        if snapshot_value != expected_value:
            raise ValueError(
                "PAPER_ANALYSIS_EXECUTION_IDENTITY_MISMATCH "
                f"field={field} expected={expected_value} snapshot={snapshot_value}"
            )
    if not generation_scopes:
        raise ValueError("PAPER_ANALYSIS_GENERATION_SCOPES_REQUIRED")
    expected_parent_generation = generation_scopes[0].generation
    expected_context_generations = {
        scope.interval: scope.generation for scope in generation_scopes[1:]
    }
    actual_parent_generation = meta.get("analysis_parent_canonical_revision")
    actual_context_generations = meta.get("analysis_confirmed_bar_context_revisions")
    if (
        isinstance(actual_parent_generation, bool)
        or not isinstance(actual_parent_generation, int)
        or actual_parent_generation != expected_parent_generation
        or not isinstance(actual_context_generations, dict)
        or actual_context_generations != expected_context_generations
    ):
        raise ValueError("PAPER_ANALYSIS_CANONICAL_GENERATIONS_MISMATCH")
    snapshot_instrument_id = snapshot_identity["instrument_id"]
    publication_guard = begin_chart_bars_publication(generation_scopes)
    try:
        try:
            paper_enabled = auto_paper_trading_enabled(snapshot_instrument_id)
        except Exception:
            _LOGGER.exception(
                "paper sync skipped; auto paper trading setting check failed "
                "for analysis key %s instrument_id=%s",
                key,
                snapshot_instrument_id,
            )
            return
        if not paper_enabled:
            return
        try:
            from aef_terminal.ui.paper.sync import paper_sync_from_analysis_snapshot

            result = paper_sync_from_analysis_snapshot(snapshot)
        except Exception:
            _LOGGER.exception("paper sync failed for analysis key %s", key)
            return None
        if not isinstance(result, dict):
            return None
        outcomes = result.get("outcomes")
        if isinstance(outcomes, list):
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                status = str(outcome.get("status") or "")
                if status not in {"rejected", "skipped"}:
                    continue
                _LOGGER.warning(
                    "automatic paper execution %s key=%s instrument_id=%s "
                    "code=%s reason=%s execution=%s",
                    status,
                    key,
                    snapshot_instrument_id,
                    str(outcome.get("code") or ""),
                    str(outcome.get("reason") or ""),
                    outcome.get("execution") if isinstance(outcome.get("execution"), dict) else {},
                )
        return result
    finally:
        end_chart_bars_publication(publication_guard)


async def run_market_analysis_job(
    key: str,
    payload: dict[str, Any],
    *,
    lock: MarketAnalysisStateLock,
    tasks: dict[str, asyncio.Task[Any]],
    identities: dict[str, tuple[Any, ...]],
    wanted: dict[str, dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    trim_cache: Callable[[float], None],
    on_error: Callable[[str, BaseException], None],
    auto_paper_trading_enabled: Callable[[str], bool],
    submit_research_capture: Callable[[str, bytes], None] | None = None,
    build_snapshot_bytes: Callable[..., Any] | None = None,
    store_factory: Callable[[], Any] | None = None,
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]] | None = None,
) -> None:
    instrument_id = ""
    route_fingerprint = ""
    exact_timeframe = ""
    expected_timeframe: object = payload.get("interval")
    expected_provider: object = payload.get("source")
    instrument = payload.get("instrument")
    expected_provider_contract_id: object = (
        instrument.get("session_contract_id") if isinstance(instrument, dict) else None
    )
    item: dict[str, Any] | None = None
    try:
        analysis_effects = market_analysis_effects(payload)
        instrument_id = require_exact_identity_text(
            payload.get("instrument_id"),
            field="instrument_id",
        )
        route_fingerprint = require_exact_identity_text(
            payload.get("route_fingerprint"),
            field="route_fingerprint",
        )
        exact_timeframe = require_exact_identity_text(
            expected_timeframe,
            field="timeframe",
        )
        async with lock:
            if key not in wanted:
                return
        parent_generation = _require_analysis_bar_generations(
            payload,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=exact_timeframe,
        )
        raw_indicator_params = (
            payload.get("indicator_params")
            if isinstance(payload.get("indicator_params"), dict)
            else {}
        )
        channel_enabled = indicator_enabled(
            raw_indicator_params,
            "channel_master",
        )
        analysis_payload = dict(payload)
        if channel_enabled:
            if store_factory is None or normalize_drawing_anchors is None:
                raise RuntimeError("manual channel analysis dependencies are required")
            if not isinstance(instrument, dict):
                raise ValueError("manual channel analysis requires a qualified instrument")
            route = route_instrument(instrument)
            if (
                route.instrument_id != instrument_id
                or route.fingerprint != route_fingerprint
                or route.provider != expected_provider
                or route.instrument.get("instrument_id") != instrument.get("instrument_id")
            ):
                raise ValueError("manual channel analysis route identity mismatch")
            (
                hydrated_params,
                manual_channel_generation,
            ) = await hydrate_manual_channel_analysis_params(
                raw_indicator_params,
                store=store_factory(),
                instrument=route.instrument,
                interval=exact_timeframe,
                normalize_drawing_anchors=normalize_drawing_anchors,
                expected_generation=parent_generation,
            )
            if manual_channel_generation != parent_generation:
                raise RuntimeError(
                    "manual channel generation must match the parent analysis generation"
                )
            analysis_payload["indicator_params"] = hydrated_params
        else:
            sanitized_params = dict(raw_indicator_params)
            sanitized_params.pop("manual_channels", None)
            sanitized_params.pop(
                "manual_channel_canonical_generation",
                None,
            )
            analysis_payload["indicator_params"] = sanitized_params

        _require_analysis_bar_generations(
            payload,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=exact_timeframe,
        )
        if build_snapshot_bytes is None:
            analysis_task = asyncio.create_task(
                run_market_analysis_process(key, analysis_payload),
                name=f"market-analysis-process:{key}",
            )
            analysis_outcome = await settle_physical_task(analysis_task)
            analysis_error = analysis_outcome.error
            if analysis_outcome.task_cancelled:
                if analysis_outcome.cancellation is not None:
                    raise analysis_outcome.cancellation
                raise asyncio.CancelledError()
            if analysis_error is not None:
                if analysis_outcome.cancellation is not None:
                    raise analysis_outcome.cancellation from analysis_error
                raise analysis_error
            if analysis_outcome.cancellation is not None:
                raise analysis_outcome.cancellation
            process_result = analysis_outcome.result
            if not isinstance(process_result, MarketAnalysisProcessResult):
                raise TypeError("market analysis process must return a typed result")
            snapshot_bytes = process_result.snapshot_bytes
            research_capture_bytes = process_result.research_capture_bytes
        else:
            maybe_bytes = build_snapshot_bytes(key, analysis_payload)
            if asyncio.iscoroutine(maybe_bytes):
                maybe_bytes = await maybe_bytes
            snapshot_bytes = maybe_bytes
            research_capture_bytes = None
        _require_analysis_bar_generations(
            payload,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=exact_timeframe,
        )
        if not isinstance(snapshot_bytes, bytes):
            raise TypeError("market analysis snapshot builder must return bytes")
        async with lock:
            registered_task = tasks.get(key)
            owns_registration = registered_task is None or registered_task is asyncio.current_task()
            paper_sync_admitted = owns_registration and key in wanted
        if paper_sync_admitted:
            generation_scopes = _analysis_bar_generation_scopes(
                payload,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                timeframe=exact_timeframe,
            )
            if submit_research_capture is not None and research_capture_bytes is not None:
                research_scope = "|".join(
                    (
                        instrument_id,
                        route_fingerprint,
                        str(expected_timeframe or ""),
                    )
                )
                submit_research_capture(
                    research_scope,
                    research_capture_bytes,
                )

            def generation_guarded_paper_admission(
                exact_instrument_id: str,
            ) -> bool:
                _require_analysis_bar_generations(
                    payload,
                    instrument_id=instrument_id,
                    route_fingerprint=route_fingerprint,
                    timeframe=exact_timeframe,
                )
                return auto_paper_trading_enabled(exact_instrument_id)

            if analysis_effects != MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE:
                paper_sync_task = asyncio.create_task(
                    asyncio.to_thread(
                        _sync_paper_from_analysis_snapshot_bytes,
                        key,
                        snapshot_bytes,
                        expected_instrument_id=instrument_id,
                        expected_route_fingerprint=route_fingerprint,
                        expected_timeframe=expected_timeframe,
                        expected_provider=expected_provider,
                        expected_provider_contract_id=expected_provider_contract_id,
                        generation_scopes=generation_scopes,
                        auto_paper_trading_enabled=(generation_guarded_paper_admission),
                    )
                )
                paper_sync_outcome = await settle_physical_task(paper_sync_task)
                if paper_sync_outcome.cancellation is not None:
                    raise paper_sync_outcome.cancellation
                if paper_sync_outcome.task_cancelled:
                    raise asyncio.CancelledError()
                if paper_sync_outcome.error is not None:
                    raise paper_sync_outcome.error
        _require_analysis_bar_generations(
            payload,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=exact_timeframe,
        )
        item = {
            "status": "ready",
            "snapshot_json": snapshot_bytes,
            "size_bytes": len(snapshot_bytes),
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "interval": exact_timeframe,
            "parent_canonical_generation": parent_generation,
            "updated_at": datetime.now(tz=UTC).isoformat(),
            "updated_monotonic": time.monotonic(),
            **(
                {"market_version": dict(payload["market_version"])}
                if isinstance(payload.get("market_version"), dict)
                else {}
            ),
        }
    except asyncio.CancelledError:
        raise
    except ChartBarsGenerationChanged:
        raise
    except Exception as exc:
        if instrument_id and route_fingerprint:
            try:
                on_error(key, exc)
            except Exception:
                _LOGGER.exception(
                    "market analysis error telemetry failed for key %s",
                    key,
                )
            item = build_market_analysis_error_item(
                exc,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
            )
        else:
            raise
    finally:
        async with lock:
            registered_task = tasks.get(key)
            owns_registration = registered_task is None or registered_task is asyncio.current_task()
            if owns_registration:
                tasks.pop(key, None)
                identities.pop(key, None)
            if owns_registration and item is not None and key in wanted:
                try:
                    _require_analysis_bar_generations(
                        payload,
                        instrument_id=instrument_id,
                        route_fingerprint=route_fingerprint,
                        timeframe=exact_timeframe,
                    )
                except ChartBarsGenerationChanged:
                    item = None
                if item is not None:
                    cache[key] = item
                    trim_cache(time.monotonic())
