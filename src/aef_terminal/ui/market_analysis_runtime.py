from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.ui.services.market_analysis_runtime_service import (
    MARKET_ANALYSIS_REFRESH_SECONDS,
    MarketAnalysisRuntimeDeps,
    MarketAnalysisRuntimeService,
)
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_STANDARD,
)
from aef_terminal.ui.services.market_analysis_process import (
    market_analysis_process_runtime_diagnostics,
    shutdown_market_analysis_process_runtime,
)

REFRESH_SECONDS = MARKET_ANALYSIS_REFRESH_SECONDS
_RUNTIME = MarketAnalysisRuntimeService()


def configure_market_analysis_runtime_deps(
    deps: MarketAnalysisRuntimeDeps,
) -> None:
    _RUNTIME.configure(deps)


def analysis_key(payload: dict[str, Any]) -> str:
    return _RUNTIME.analysis_key(payload)


def analysis_request_identity(payload: dict[str, Any]) -> tuple[Any, ...]:
    return _RUNTIME.analysis_request_identity(payload)


def analysis_refresh_seconds(payload: dict[str, Any]) -> float:
    return _RUNTIME.analysis_refresh_seconds(payload)


def build_analysis_payload(
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
    return _RUNTIME.build_analysis_payload(
        source=source,
        instrument_id=instrument_id,
        interval=interval,
        range_=range_,
        signal_range=signal_range,
        show_visuals=show_visuals,
        indicator_params=indicator_params,
        gex_context_active=gex_context_active,
        gex_capture_mode=gex_capture_mode,
        include_telemetry=include_telemetry,
        market_version=market_version,
        parent_canonical_generation=parent_canonical_generation,
        instrument=instrument,
        analysis_effects=analysis_effects,
    )


def auto_paper_trading_enabled(instrument_id: str) -> bool:
    return _RUNTIME.auto_paper_trading_enabled(instrument_id)


async def run_analysis_job(
    key: str,
    payload: dict[str, Any],
) -> None:
    await _RUNTIME.run_analysis_job(key, payload)


async def register_wanted(
    key: str,
    payload: dict[str, Any],
    client_id: str,
    lease_sequence: int,
) -> str:
    return await _RUNTIME.register_wanted(
        key,
        payload,
        client_id,
        lease_sequence,
    )


async def renew_client_lease(
    client_id: str,
    lease_sequence: int,
    key: str,
    instrument_id: str,
    expected_route_fingerprint: str,
) -> bool:
    return await _RUNTIME.renew_client_lease(
        client_id,
        lease_sequence,
        key,
        instrument_id,
        expected_route_fingerprint,
    )


async def refresh_client_analysis(
    client_id: str,
    lease_sequence: int,
    key: str,
    instrument_id: str,
    expected_route_fingerprint: str,
    client_version: str,
) -> bool:
    return await _RUNTIME.refresh_client_analysis(
        client_id,
        lease_sequence,
        key,
        instrument_id,
        expected_route_fingerprint,
        client_version,
    )


async def invalidate_provider_session_route(
    instrument_id: str,
    route_fingerprint: str,
) -> int:
    return await _RUNTIME.invalidate_provider_session_route(
        instrument_id,
        route_fingerprint,
    )


async def release_client_lease(
    client_id: str,
    lease_sequence: int,
) -> bool:
    return await _RUNTIME.release_client_lease(
        client_id,
        lease_sequence,
    )


async def confirmed_bar_context_advanced(
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
    generation: int,
) -> int:
    return await _RUNTIME.confirmed_bar_context_advanced(
        instrument_id,
        route_fingerprint,
        timeframe,
        generation,
    )


async def worker_loop(*, server_sleeping: Callable[[], bool]) -> None:
    await _RUNTIME.worker_loop(server_sleeping=server_sleeping)


async def snapshot(
    key: str = "",
    wait_seconds: float = 0.0,
    *,
    instrument_id: str = "",
    expected_route_fingerprint: str = "",
    vsa_render_hours: int = 6,
) -> Any:
    return await _RUNTIME.snapshot(
        key,
        wait_seconds,
        instrument_id=instrument_id,
        expected_route_fingerprint=expected_route_fingerprint,
        vsa_render_hours=vsa_render_hours,
    )


def diagnostics() -> dict[str, Any]:
    payload = _RUNTIME.diagnostics()
    payload["process_workers"] = market_analysis_process_runtime_diagnostics()
    return payload


async def shutdown() -> None:
    try:
        await _RUNTIME.shutdown()
    finally:
        await shutdown_market_analysis_process_runtime()
