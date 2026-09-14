from __future__ import annotations

import asyncio
import inspect
import json
import math
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.config import AppConfig
from aef_terminal.data.gex import activity as gex_activity
from aef_terminal.data.gex import config as gex_config
from aef_terminal.data.gex import constants as gex_constants
from aef_terminal.data.provider_contract import (
    InstrumentRoute,
    OptionUniverseUnavailableError,
    ProviderCapabilities,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.gex import context as gex_context_module
from aef_terminal.data.gex import dividend as gex_dividend
from aef_terminal.data.gex import math as gex_math
from aef_terminal.data.gex import option_targets as gex_option_targets
from aef_terminal.data.gex import payload as gex_payload
from aef_terminal.data.gex import payload_contract as gex_payload_contract
from aef_terminal.data.gex import quality as gex_quality
from aef_terminal.data.gex import scheduler as gex_scheduler
from aef_terminal.data.gex import session as gex_session
from aef_terminal.data.gex import snapshot_validation as gex_snapshot_validation
import aef_terminal.data.ibkr.gex as ibkr_gex
import aef_terminal.data.ibkr.option_acquisition as ibkr_option_acquisition
from aef_terminal.data.ibkr import gex_market as ibkr_gex_market
from aef_terminal.data.gex import history as gex_history
from aef_terminal.data.gex import live as gex_live
from aef_terminal.data.gex import live_analysis as gex_live_analysis
from aef_terminal.data.gex.constants import GEX_REQUEST_SNAPSHOT_SOURCE
from aef_terminal.data.gex.contracts import (
    GexCaptureModeConflictError,
    GexUnderlyingQuoteUnavailableError,
    gex_underlying_quote_observation_from_cache,
    gex_capture_revision_at,
    gex_comparison_scope_from_contract_universe,
    gex_comparison_scope_from_payload,
    require_gex_contract_market_facts,
    require_gex_option_activity,
    require_gex_underlying_quote_observation,
    select_gex_option_reference,
)
from aef_terminal.data.gex.live import _live_request as _live_request_owner
from aef_terminal.data.gex.quality import (
    require_gex_acquisition_spot_observation,
)
from aef_terminal.data.gex.utils import gex_kind_class
from aef_terminal.data.ibkr.market_data import request_ibkr_market_data_ticker
from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
from aef_terminal.data.ibkr.runtime import _IBKR_RUNTIME
from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.domain import OptionRight
from aef_terminal.engine.analyze.context import option_flow_context_from_gex_history
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore
from aef_terminal.ui.services import gex_scheduler as gex_scheduler_service
from aef_terminal.ui.services.gex_scheduler import run_gex_scheduler_loop
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload
from tests.ibkr_option_test_support import ibkr_option as _ibkr_option

_ES = ibkr_future_payload("ES")
_SPY = ibkr_stock_payload("SPY", con_id=756733)
_QQQ = ibkr_stock_payload("QQQ", con_id=320227571)
_SPX = ibkr_stock_payload("SPX", con_id=416904, asset_class="index", sec_type="IND")
_ACTIVE_OPTION_EXPIRY = "20991231"
_ACTIVE_OPTION_EXPIRY_AT = datetime(2099, 12, 31, 21, 0, tzinfo=UTC)
_ACTIVE_OPTION_PRIOR_EXPIRY = "20991230"
_ACTIVE_OPTION_PRIOR_EXPIRY_AT = datetime(2099, 12, 30, 21, 0, tzinfo=UTC)
_INSTRUMENTS_BY_ID = {
    qualified_instrument_id(instrument): instrument for instrument in (_ES, _SPY, _QQQ, _SPX)
}


def _ibkr_gex_request_config(*, instrument, store=None):
    return ibkr_gex_provider_runtime.request_config(
        instrument=instrument,
        store=store,
        mode="manual",
    )


def _ibkr_provider_request(request):
    return ibkr_gex_provider_runtime._ibkr_request(request)


def _gex_test_request(
    instrument,
    *,
    strike_count=None,
    max_contracts=None,
    timeout=None,
):
    request = gex.gex_request_config(instrument=instrument)
    changes = {}
    if strike_count is not None:
        changes["strike_count"] = strike_count
    if max_contracts is not None:
        changes["max_contracts"] = max_contracts
    if timeout is not None:
        changes["provider_metadata"] = replace(
            request.provider_metadata,
            timeout_seconds=timeout,
        )
        changes["provider_request"] = replace(
            request.provider_request,
            timeout=timeout,
        )
    return replace(request, **changes)


def _live_request(*, instrument, store=None):
    return _live_request_owner(
        instrument=instrument,
        store=store,
        provider_runtime=ibkr_gex_provider_runtime,
    )


def _ibkr_auto_gex_request_config(*, instrument, store=None):
    return ibkr_gex_provider_runtime.request_config(
        instrument=instrument,
        store=store,
        mode="auto",
    )


def _ibkr_gex_runtime_status():
    return gex_scheduler.gex_runtime_status(
        provider_runtime=ibkr_gex_provider_runtime,
    )


def _ibkr_scheduler_request(job, *, instrument, store=None):
    return gex_scheduler.gex_request_for_scheduler_job(
        job,
        instrument=instrument,
        base=ibkr_gex_provider_runtime.request_config(
            instrument=instrument,
            store=store,
            mode="auto",
        ),
    )


def _with_ibkr_runtime(callback):
    def call(*args, **kwargs):
        kwargs.setdefault("provider_runtime", ibkr_gex_provider_runtime)
        return callback(*args, **kwargs)

    return call


def _collect_through_gex_lane(
    *,
    instrument,
    underlying_quote,
    request=None,
    store=None,
):
    route = route_instrument(instrument)
    exact_request = request or ibkr_gex_provider_runtime.request_config(
        instrument=route.instrument,
        store=store,
        mode="manual",
    )

    async def run():
        payload = await ibkr_gex_provider_runtime.run_sync(
            "gex",
            f"{route.provider_symbol} options",
            lambda: gex_context_module._collect_live_gex_context_owned(
                route.provider_symbol,
                request=exact_request,
                dividend_cache={},
                underlying_quote=underlying_quote,
                instrument=route.instrument,
                provider_runtime=ibkr_gex_provider_runtime,
            ),
            timeout=exact_request.provider_metadata.timeout_seconds + 3.0,
        )
        completion = gex_context_module._finalize_gex_request_payload(
            route.provider_symbol,
            payload,
            instrument=route.instrument,
            store=store,
        )
        return gex_context_module._project_gex_request_history(
            completion,
            history_hours=gex_constants.GEX_CHART_HISTORY_HOURS,
            max_levels=gex_constants.GEX_CONTEXT_MAX_LEVELS,
            store=store,
        )

    return asyncio.run(run())


# Explicit test composition over concrete owners; the production package has no
# re-export facade or import-order side effects.
gex = SimpleNamespace(
    GEX_ANALYSIS_PAIR_MAX_SNAPSHOTS=gex_constants.GEX_ANALYSIS_PAIR_MAX_SNAPSHOTS,
    GEX_CHART_HISTORY_HOURS=gex_constants.GEX_CHART_HISTORY_HOURS,
    GEX_CONTEXT_MAX_LEVELS=gex_constants.GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES=gex_constants.GEX_CONTEXT_STALE_MINUTES,
    GEX_GENERIC_TICKS=gex_constants.GEX_GENERIC_TICKS,
    GEX_LIVE_FRAME_SECONDS=gex_constants.GEX_LIVE_FRAME_SECONDS,
    GexRequestConfig=gex_config.GexRequestConfig,
    NY_TZ=gex_constants.NY_TZ,
    _GEX_RUNTIME=gex_session._GEX_RUNTIME,
    _aggregate_by_strike=gex_payload._aggregate_by_strike,
    _black_scholes_gamma_delta=gex_math._black_scholes_gamma_delta,
    _black_scholes_price=gex_math._black_scholes_price,
    _cached_dividend_yield=gex_dividend._cached_dividend_yield,
    _cached_dividend_payload=gex_dividend._cached_dividend_payload,
    _collect_live_gex_context_owned=_with_ibkr_runtime(
        gex_context_module._collect_live_gex_context_owned
    ),
    _dividend_yield_for_gex_request=gex_dividend._dividend_yield_for_gex_request,
    _dividend_yield_from_pv_dividend=gex_dividend._dividend_yield_from_pv_dividend,
    _dividend_yield_setting_key=gex_dividend._dividend_yield_setting_key,
    _expiry_profile_from_contracts=gex_payload._expiry_profile_from_contracts,
    _gex_activity_delta=gex_activity._gex_activity_delta,
    _gex_diagnostics=gex_quality._gex_diagnostics,
    _gex_frame_quality=gex_quality._gex_frame_quality,
    _gex_request_config=_ibkr_auto_gex_request_config,
    _gex_route_offset_minutes=gex_scheduler._gex_route_offset_minutes,
    _level_kind=gex_payload._level_kind,
    _levels_from_strikes=gex_payload._levels_from_strikes,
    _maybe_update_dividend_yield_cache=gex_dividend._maybe_update_dividend_yield_cache,
    _normal_cdf=gex_math._normal_cdf,
    _option_model_gamma_delta=gex_math._option_model_gamma_delta,
    _option_model_price=gex_math._option_model_price,
    _option_volume_context_from_strike=gex_payload._option_volume_context_from_strike,
    _payload_from_rows=gex_payload._payload_from_rows,
    _qualified_gex_contract_rows=gex_quality._qualified_gex_contract_rows,
    _select_gex_levels=gex_payload._select_gex_levels,
    _time_to_expiry_years=gex_math._time_to_expiry_years,
    _valid_broker_dividend_yield=gex_dividend._valid_broker_dividend_yield,
    async_gex_context=_with_ibkr_runtime(gex_context_module.async_gex_context),
    cached_gex_context=gex_context_module.cached_gex_context,
    compute_gamma_flip=gex_math.compute_gamma_flip,
    get_gex_history_series=gex_history.get_gex_history_series,
    gex_analysis_day_start=gex_history.gex_analysis_day_start,
    gex_context=_with_ibkr_runtime(gex_context_module.gex_context),
    gex_request_config=_ibkr_gex_request_config,
    gex_request_for_scheduler_job=_ibkr_scheduler_request,
    gex_runtime_status=_ibkr_gex_runtime_status,
    gex_scheduler_jobs=gex_scheduler.gex_scheduler_jobs,
    persist_gex_snapshot=gex_history.persist_gex_snapshot,
    option_target_price=gex_option_targets.option_target_price,
    require_gex_expiry_profile=gex_payload_contract.require_gex_expiry_profile,
    require_gex_levels=gex_payload_contract.require_gex_levels,
    require_gex_snapshot_payload=gex_snapshot_validation.require_gex_snapshot_payload,
)


@pytest.fixture(autouse=True)
def _isolate_gex_runtime_routes(monkeypatch):
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    gex_context_module._GEX_REQUEST_TASKS.clear()
    ibkr_option_acquisition._IBKR_OPTION_CHAIN_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_SERIES_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_EXPIRY_FACT_CACHE.clear()
    _IBKR_RUNTIME.gex_request_universe_cache.clear()
    gex_context_module._GEX_REQUEST_TASKS.clear()
    gex_live._LIVE_GEX_SESSIONS.clear()
    gex_live._publish_live_gex_status_owned()
    yield
    ibkr_option_acquisition._IBKR_OPTION_CHAIN_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_SERIES_CACHE.clear()
    ibkr_option_acquisition._IBKR_OPTION_EXPIRY_FACT_CACHE.clear()
    _IBKR_RUNTIME.gex_request_universe_cache.clear()
    gex_live._LIVE_GEX_SESSIONS.clear()
    gex_live._publish_live_gex_status_owned()


def _instrument_id(instrument: dict[str, Any]) -> str:
    return qualified_instrument_id(instrument)


def _route_key(instrument: dict[str, Any]) -> str:
    return route_fingerprint(instrument)


def _identity_key(instrument: dict[str, Any]) -> tuple[str, str]:
    return (_instrument_id(instrument), _route_key(instrument))


def test_gex_capture_mode_owner_rejects_parallel_request_and_live() -> None:
    instrument_id, route_key = _identity_key(_SPY)

    gex._GEX_RUNTIME.acquire_capture_mode(
        instrument_id,
        route_key,
        "live",
        provider_symbol="SPY",
    )

    with pytest.raises(GexCaptureModeConflictError) as raised:
        gex._GEX_RUNTIME.acquire_capture_mode(
            instrument_id,
            route_key,
            "request",
            provider_symbol="SPY",
        )

    assert raised.value.instrument_id == instrument_id
    assert raised.value.route_fingerprint == route_key
    assert raised.value.active == "live"
    assert raised.value.requested == "request"


def test_async_gex_context_coalesces_same_route_request_demands(monkeypatch) -> None:
    calls = 0

    async def run() -> None:
        nonlocal calls
        started = asyncio.Event()
        release = asyncio.Event()

        async def owned(**_kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"ok": True, "status": "ok"}

        monkeypatch.setattr(gex_context_module, "_async_gex_context_owned", owned)
        first = asyncio.create_task(
            gex_context_module.async_gex_context(
                enabled=True,
                refresh=True,
                instrument=_SPY,
                provider_runtime=ibkr_gex_provider_runtime,
            )
        )
        await started.wait()
        second = asyncio.create_task(
            gex_context_module.async_gex_context(
                enabled=True,
                refresh=True,
                instrument=_SPY,
                provider_runtime=ibkr_gex_provider_runtime,
            )
        )
        await asyncio.sleep(0)

        assert calls == 1
        release.set()
        assert await first == {"ok": True, "status": "ok"}
        assert await second == {"ok": True, "status": "ok"}
        await asyncio.sleep(0)

    asyncio.run(run())

    assert calls == 1
    assert gex_context_module._GEX_REQUEST_TASKS == {}
    runtime = gex._GEX_RUNTIME.routes_snapshot()
    assert next(iter(runtime.values()))["active_capture_mode"] == ""


def test_gex_request_settles_before_cancellation_is_exposed(monkeypatch) -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()

        async def owned(**_kwargs: Any) -> dict[str, Any]:
            started.set()
            await release.wait()
            completed.set()
            return {"ok": True, "status": "ok"}

        monkeypatch.setattr(gex_context_module, "_async_gex_context_owned", owned)
        request_task = asyncio.create_task(
            gex_context_module.async_gex_context(
                enabled=True,
                refresh=True,
                instrument=_SPY,
                provider_runtime=ibkr_gex_provider_runtime,
            )
        )
        await started.wait()
        request_task.cancel()
        await asyncio.sleep(0)

        assert not request_task.done()
        assert gex_context_module._GEX_REQUEST_TASKS

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        await asyncio.sleep(0)

        assert completed.is_set()
        assert gex_context_module._GEX_REQUEST_TASKS == {}
        runtime = gex._GEX_RUNTIME.routes_snapshot()
        assert next(iter(runtime.values()))["active_capture_mode"] == ""

    asyncio.run(run())


@pytest.mark.parametrize("manual_first", [False, True])
def test_scheduler_capture_and_joined_manual_history_have_one_owner(
    monkeypatch, manual_first: bool
) -> None:
    capture_calls = 0
    history_calls = 0
    history_release = threading.Event()
    payload = {
        "ok": True,
        "status": "ok",
        "provider_symbol": "SPY",
        "instrument_id": _instrument_id(_SPY),
        "route_fingerprint": _route_key(_SPY),
        "levels": [],
    }

    async def run():
        nonlocal capture_calls, history_calls
        capture_started = asyncio.Event()
        capture_release = asyncio.Event()
        history_started = asyncio.Event()
        loop = asyncio.get_running_loop()
        capture_store = object()

        async def capture(**kwargs):
            nonlocal capture_calls
            assert kwargs["store"] is capture_store
            capture_calls += 1
            capture_started.set()
            await capture_release.wait()
            return gex_context_module._GexRequestCompletion(
                payload,
                datetime.now(tz=UTC) + timedelta(hours=1),
            )

        def history(*_args, **kwargs):
            nonlocal history_calls
            assert kwargs["store"] is capture_store
            history_calls += 1
            loop.call_soon_threadsafe(history_started.set)
            assert history_release.wait(5)
            return [{"captured_at": "history"}]

        monkeypatch.setattr(gex_context_module, "_async_gex_context_owned", capture)
        monkeypatch.setattr(gex_context_module, "get_gex_history_series", history)

        def request(*, chart_history, store):
            return asyncio.create_task(
                gex_context_module.async_gex_context(
                    enabled=True,
                    refresh=True,
                    instrument=_SPY,
                    provider_runtime=ibkr_gex_provider_runtime,
                    project_capture_history=chart_history,
                    store=store,
                )
            )

        first = request(chart_history=manual_first, store=capture_store)
        await capture_started.wait()
        second = request(chart_history=not manual_first, store=object())
        manual, scheduler = (first, second) if manual_first else (second, first)
        joined_manual = request(chart_history=True, store=object())
        await asyncio.sleep(0)
        capture_release.set()
        await asyncio.wait_for(history_started.wait(), timeout=2)
        scheduler_result = await scheduler
        assert scheduler_result == payload
        assert "history" not in scheduler_result
        assert not manual.done()
        manual.cancel()
        await asyncio.sleep(0)
        assert not manual.done()
        history_release.set()
        with pytest.raises(asyncio.CancelledError):
            await manual
        result = await joined_manual
        assert result == {**payload, "history": [{"captured_at": "history"}]}

    try:
        asyncio.run(run())
    finally:
        history_release.set()
    assert capture_calls == history_calls == 1
    assert "history" not in payload
    assert gex_context_module._GEX_REQUEST_TASKS == {}


def test_scheduler_only_capture_never_projects_history(monkeypatch) -> None:
    async def capture(**_kwargs):
        return gex_context_module._GexRequestCompletion(
            {"ok": True, "status": "ok"}, datetime.now(tz=UTC) + timedelta(hours=1)
        )

    def unexpected_history(*_args, **_kwargs):
        raise AssertionError("scheduler has no history consumer")

    monkeypatch.setattr(gex_context_module, "_async_gex_context_owned", capture)
    monkeypatch.setattr(gex_context_module, "get_gex_history_series", unexpected_history)
    assert asyncio.run(
        gex_context_module.async_gex_context(
            enabled=True,
            refresh=True,
            instrument=_SPY,
            provider_runtime=ibkr_gex_provider_runtime,
            project_capture_history=False,
        )
    ) == {"ok": True, "status": "ok"}


def test_request_history_errors_and_expiry_after_projection_remain_visible(monkeypatch) -> None:
    expires = datetime.now(tz=UTC) + timedelta(minutes=1)
    completion = gex_context_module._GexRequestCompletion(
        {
            "provider_symbol": "SPY",
            "instrument_id": _instrument_id(_SPY),
            "route_fingerprint": _route_key(_SPY),
        },
        expires,
    )

    def invalid_history(*_args, **_kwargs):
        raise ValueError("GEX_HISTORY_CONTRACT_INVALID: invalid row")

    monkeypatch.setattr(gex_context_module, "get_gex_history_series", invalid_history)
    with pytest.raises(ValueError, match="GEX_HISTORY_CONTRACT_INVALID"):
        gex_context_module._project_gex_request_history(
            completion, history_hours=48, max_levels=5, store=None
        )

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expires + timedelta(seconds=1)

    def history(*_args, **_kwargs):
        monkeypatch.setattr(gex_context_module, "datetime", ExpiredClock)
        return []

    monkeypatch.setattr(gex_context_module, "get_gex_history_series", history)
    with pytest.raises(OptionUniverseUnavailableError) as rejected:
        gex_context_module._project_gex_request_history(
            completion, history_hours=48, max_levels=5, store=None
        )
    assert rejected.value.reason == "OPTION_UNIVERSE_ROLLOVER"
    assert "history" not in completion.payload


def test_scheduler_delivery_rechecks_expiry_after_executor_wait(monkeypatch) -> None:
    expires = datetime.now(tz=UTC) + timedelta(minutes=1)

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expires + timedelta(seconds=1)

    async def capture(**_kwargs):
        return gex_context_module._GexRequestCompletion({"ok": True}, expires)

    async def deliver_after_expiry(function, *args, **kwargs):
        result = function(*args, **kwargs)
        monkeypatch.setattr(gex_context_module, "datetime", ExpiredClock)
        return result

    monkeypatch.setattr(gex_context_module, "_async_gex_context_owned", capture)
    monkeypatch.setattr(gex_context_module, "run_physical_thread_call", deliver_after_expiry)
    with pytest.raises(OptionUniverseUnavailableError) as rejected:
        asyncio.run(
            gex_context_module.async_gex_context(
                enabled=True,
                refresh=True,
                instrument=_SPY,
                provider_runtime=ibkr_gex_provider_runtime,
                project_capture_history=False,
            )
        )
    assert rejected.value.reason == "OPTION_UNIVERSE_ROLLOVER"


def test_gex_cached_request_read_settles_worker_before_cancellation(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def cached(**_kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(1.0)
        finished.set()
        return {"ok": True, "status": "cached"}

    monkeypatch.setattr(gex_context_module, "cached_gex_context", cached)

    async def run() -> None:
        task = asyncio.create_task(
            gex_context_module._async_gex_context_owned(
                enabled=True,
                refresh=False,
                instrument=_SPY,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()

    assert finished.is_set()


def test_gex_capture_revision_is_exact_per_capture_lane() -> None:
    captured_at = datetime(2026, 7, 10, 14, 4, 48, tzinfo=UTC)

    assert (
        gex_capture_revision_at(
            captured_at,
            capture_mode="request",
        )
        == captured_at
    )
    assert gex_capture_revision_at(
        captured_at,
        capture_mode="live",
    ) == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="aware timestamp"):
        gex_capture_revision_at(
            captured_at.replace(tzinfo=None),
            capture_mode="live",
        )


def test_gex_underlying_quote_preserves_the_producer_selected_price_provenance() -> None:
    received_at = datetime(2026, 7, 10, 14, 4, 47, tzinfo=UTC).isoformat()
    provider_ts = datetime(2026, 7, 10, 14, 4, 46, tzinfo=UTC).isoformat()

    observation = require_gex_underlying_quote_observation(
        {
            "price": 101.0,
            "price_source": "last",
            "bid": 100.0,
            "ask": 100.5,
            "last": 101.0,
            "received_at": received_at,
            "provider_ts": provider_ts,
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        }
    )

    assert observation["price"] == 101.0
    assert observation["price_source"] == "last"
    assert observation["provider_ts"] == provider_ts
    assert observation["time_basis"] == "provider_event"


def test_gex_underlying_quote_cache_projection_selects_fresher_midpoint() -> None:
    last_at = datetime(2026, 7, 10, 14, 4, 0, tzinfo=UTC).isoformat()
    bid_ask_at = datetime(2026, 7, 10, 14, 4, 25, tzinfo=UTC).isoformat()

    observation = gex_underlying_quote_observation_from_cache(
        {
            "price": 101.0,
            "price_source": "last",
            "bid": 100.0,
            "ask": 100.5,
            "last": 101.0,
            "received_at": bid_ask_at,
            "bid_ask_received_at": bid_ask_at,
            "provider_ts": last_at,
            "last_provider_ts": last_at,
            "time_basis": "provider_event",
            "market_data_type": 1,
            "market_data_entitlement": "live",
        }
    )

    assert observation["price"] == 100.25
    assert observation["price_source"] == "bid_ask_mid"
    assert observation["received_at"] == bid_ask_at
    assert observation["provider_ts"] is None
    assert observation["time_basis"] == "client_receive"


def _canonical_gex_level(
    price: float,
    *,
    spot: float | None = None,
    kind: str = "CALL_WALL",
    strength: float = 1.0,
    selection_rank: int = 1,
    call_gex: float = 1_000_000.0,
    put_gex: float = -200_000.0,
    option_volume_context: dict[str, Any] | None = None,
    motion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spot = float(price if spot is None else spot)
    power_class = (
        "WEAK"
        if strength < 0.10
        else "EXTREME"
        if strength >= 0.85
        else "STRONG"
        if strength >= 0.55
        else "MEDIUM"
    )
    level = {
        "price": float(price),
        "kind": kind,
        "kind_class": gex_kind_class(kind),
        "strength": float(strength),
        "power_class": power_class,
        "selection_rank": selection_rank,
        "net_gex": float(call_gex + put_gex),
        "call_gex": float(call_gex),
        "put_gex": float(put_gex),
        "abs_gex": float(abs(call_gex) + abs(put_gex)),
        "abs_flow_1pt": float((abs(call_gex) + abs(put_gex)) / (spot * 0.01)),
        "distance_from_spot": float(price - spot),
        "zone_half_width": 0.25,
        "spot_side": "above" if price > spot else "below" if price < spot else "inside",
        "option_volume_context": (
            option_volume_context
            if option_volume_context is not None
            else gex._option_volume_context_from_strike({})
        ),
    }
    if motion is not None:
        level["motion"] = motion
    return level


def _status_route_key(instrument: dict[str, Any]) -> str:
    return json.dumps(list(_identity_key(instrument)), separators=(",", ":"))


def _live_gex_session(
    *,
    provider_symbol: str,
    instrument_id: str,
    route_fingerprint: str,
    key: tuple[Any, ...],
    request: gex.GexRequestConfig,
    spot: float,
    ib: Any | None = None,
    underlying: Any | None = None,
    contracts: list[Any] | None = None,
    tickers: list[tuple[Any, ...]] | None = None,
    chain_meta: dict[str, Any] | None = None,
    underlying_audit: dict[str, Any] | None = None,
    **state: Any,
) -> gex_live._LiveGexSession:
    del underlying
    provider_contracts = tuple(contracts or ())
    provider_tickers: list[tuple[Any, Any, ibkr_gex_market.GexTickerObservationTracker]] = []
    for subscription in tickers or ():
        if len(subscription) == 3:
            contract, ticker, tracker = subscription
        elif len(subscription) == 2:
            contract, ticker = subscription
            tracker = ibkr_gex_market.GexTickerObservationTracker(
                right=contract.right,
            )
        else:
            raise AssertionError(
                "GEX test subscriptions must contain contract, ticker, and optional tracker"
            )
        provider_tickers.append((contract, ticker, tracker))
    provider_ib = ib or SimpleNamespace(isConnected=lambda: True, disconnect=lambda: None)
    subscribed_strikes = tuple(sorted({contract.strike for contract in provider_contracts}))
    provider_chain_meta = dict(chain_meta or {})
    if provider_contracts and len(provider_contracts) % 2 == 0:
        provider_chain_meta.setdefault(
            "qualified_pair_count",
            len(provider_contracts) // 2,
        )
        provider_chain_meta.setdefault(
            "qualified_strike_count",
            len(subscribed_strikes),
        )
    provider_subscription = ibkr_gex.IbkrLiveGexSubscription(
        subscribed_strikes=subscribed_strikes,
        contracts_requested=len(provider_contracts),
        chain_meta=dict(provider_chain_meta),
        underlying_audit=dict(underlying_audit or {}),
        _ib=provider_ib,
        _contracts=provider_contracts,
        _subscriptions=provider_tickers,
    )
    return gex_live._LiveGexSession(
        provider_symbol=provider_symbol,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        key=key,
        request=request,
        provider_runtime=ibkr_gex_provider_runtime,
        provider_subscription=provider_subscription,
        subscribed_strikes=subscribed_strikes,
        contracts_requested=len(provider_contracts),
        chain_meta=dict(provider_chain_meta),
        underlying_audit=dict(underlying_audit or {}),
        spot=spot,
        **state,
    )


def _live_gex_session_token(
    session: gex_live._LiveGexSession,
) -> gex_live._LiveGexSessionToken:
    return gex_live._LiveGexSessionToken(
        identity_key=(session.instrument_id, session.route_fingerprint),
        generation=session.generation,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FakeGexStore:
    def __init__(self, snapshots: list[dict[str, Any]]):
        self.snapshots = sorted(snapshots, key=lambda row: row["captured_at"])

    def initialize(self) -> None:
        return None

    def upsert_gex_snapshot(
        self,
        instrument_id: str,
        route_fingerprint: str,
        provider_symbol: str,
        captured_at: datetime,
        payload: dict[str, Any],
        *,
        source: str,
        live_bucket_at: datetime | None = None,
    ) -> bool:
        captured_iso = captured_at.astimezone(UTC).isoformat()
        if live_bucket_at is not None:
            bucket_start = live_bucket_at.astimezone(UTC)
            bucket_end = bucket_start + timedelta(minutes=5)
            current_session_started_at = datetime.fromisoformat(
                payload["live"]["started_at"]
            ).astimezone(UTC)
            bucket_rows = [
                row
                for row in self.snapshots
                if row["instrument_id"] == instrument_id
                and row["route_fingerprint"] == route_fingerprint
                and row.get("source") == source
                and bucket_start
                <= datetime.fromisoformat(row["captured_at"]).astimezone(UTC)
                < bucket_end
            ]
            existing_session_starts = [
                datetime.fromisoformat(row["payload"]["live"]["started_at"]).astimezone(UTC)
                for row in bucket_rows
            ]
            newest_existing_session = max(existing_session_starts, default=None)
            if (
                newest_existing_session is not None
                and newest_existing_session > current_session_started_at
            ):
                return False
            if newest_existing_session == current_session_started_at:
                return True
            self.snapshots = [row for row in self.snapshots if row not in bucket_rows]
        self.snapshots = [
            row
            for row in self.snapshots
            if not (
                row["instrument_id"] == instrument_id
                and row["route_fingerprint"] == route_fingerprint
                and row["captured_at"] == captured_iso
                and row.get("source") == source
            )
        ]
        self.snapshots.append(
            {
                "provider_symbol": provider_symbol,
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "captured_at": captured_iso,
                "source": source,
                "payload": dict(payload),
            }
        )
        self.snapshots.sort(key=lambda row: row["captured_at"])
        return True

    def lookup_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        return _INSTRUMENTS_BY_ID.get(instrument_id)

    def read_trading_session_intervals(self, *_args, **_kwargs) -> list[dict[str, Any]]:
        dates = {
            datetime.fromisoformat(row["captured_at"]).astimezone(UTC).date()
            for row in self.snapshots
        }
        return [
            {
                "opens_at": datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC).isoformat(),
                "closes_at": datetime(day.year, day.month, day.day, 20, 0, tzinfo=UTC).isoformat(),
                "status": "open",
            }
            for day in sorted(dates)
        ]

    def read_latest_gex_snapshot(
        self, instrument_id: str, route_fingerprint: str, *, source: str | None = None
    ) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.snapshots
            if row["instrument_id"] == instrument_id
            and row["route_fingerprint"] == route_fingerprint
        ]
        if source:
            rows = [row for row in rows if row.get("source") == source]
        return rows[-1] if rows else None

    def read_gex_snapshots(
        self,
        instrument_id: str,
        route_fingerprint: str,
        start,
        end,
        limit: int = 512,
        *,
        source: str | None = None,
        sources: Sequence[str] | None = None,
        include_raw: bool = True,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.snapshots
            if row["instrument_id"] == instrument_id
            and row["route_fingerprint"] == route_fingerprint
            and datetime.fromisoformat(row["captured_at"]) >= start
            and datetime.fromisoformat(row["captured_at"]) <= end
        ]
        if source:
            rows = [row for row in rows if row.get("source") == source]
        elif sources is not None:
            exact_sources = tuple(sources)
            rows = [row for row in rows if row.get("source") in exact_sources]
        if not include_raw:
            compact_rows = []
            for row in rows:
                canonical_payload = deepcopy(row["payload"])
                if "option_universe_expires_at" not in canonical_payload and isinstance(
                    canonical_payload.get("raw"), dict
                ):
                    canonical_payload = gex.require_gex_snapshot_payload(
                        canonical_payload,
                        projection="persisted",
                    )
                compact_rows.append(
                    {
                        **row,
                        "payload": {
                            key: value
                            for key, value in canonical_payload.items()
                            if key
                            not in {
                                "raw",
                                "expiry_profile",
                                "diagnostics",
                            }
                        },
                    }
                )
            rows = compact_rows
        return rows[-limit:]


def _install_fake_gex_store(monkeypatch, snapshots: list[dict[str, Any]]) -> FakeGexStore:
    store = FakeGexStore(snapshots)
    monkeypatch.setattr("aef_terminal.storage.postgres.default_postgres_store", lambda: store)
    for name in (
        "cached_gex_context",
        "get_gex_history_series",
        "option_target_price",
        "_cached_dividend_payload",
        "_cached_dividend_yield",
        "_maybe_update_dividend_yield_cache",
    ):
        original = getattr(gex, name)

        def with_store(*args, _original=original, **kwargs):
            kwargs.setdefault("store", store)
            return _original(*args, **kwargs)

        monkeypatch.setattr(gex, name, with_store)
    return store


def _gex_snapshot(
    instrument: dict[str, Any],
    captured_at: datetime,
    payload: dict[str, Any],
    *,
    source: str,
    expiry: str | None = None,
) -> dict[str, Any]:
    provider_symbol = instrument["provider_symbol"]
    instrument_id = qualified_instrument_id(instrument)
    route_key = route_fingerprint(instrument)
    body = dict(payload)
    raw = body.get("raw") if isinstance(body.get("raw"), dict) else {}
    raw_meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    raw_contracts = raw.get("contracts") if isinstance(raw.get("contracts"), list) else []
    already_canonical = bool(
        body.get("frame_complete") is True
        and isinstance(body.get("comparison_scope"), dict)
        and {
            "strike_ladder",
            "contract_con_ids",
        }.issubset(body["comparison_scope"])
        and body.get("expiry_profile")
        and raw_meta
        and raw_contracts
    )
    if not already_canonical:
        capture_mode = body.get("capture_mode")
        if capture_mode not in {"request", "live"}:
            raise ValueError("Canonical GEX test snapshot requires an exact capture mode")
        if capture_mode == "live":
            body.setdefault(
                "live",
                {
                    "publishable": True,
                    "session_id": captured_at.astimezone(UTC).isoformat(),
                    "started_at": captured_at.astimezone(UTC).isoformat(),
                },
            )
        supplied_levels = body.get("levels")
        if not isinstance(supplied_levels, list) or not supplied_levels:
            raise ValueError("Canonical GEX test snapshot requires level facts")
        request_meta = (
            dict(body["request_meta"]) if isinstance(body.get("request_meta"), dict) else {}
        )
        strike_count = request_meta.get("strike_count")
        if type(strike_count) is not int or strike_count < len(supplied_levels):
            raise ValueError("Canonical GEX test snapshot requires its exact strike count")
        spot = float(body.get("spot") or 0.0)
        if spot <= 0:
            raise ValueError("Canonical GEX test snapshot requires a positive spot")
        exact_expiry = expiry
        if exact_expiry is None and raw_contracts:
            exact_expiries = sorted(
                {
                    str(row.get("expiry") or "")
                    for row in raw_contracts
                    if isinstance(row, dict) and row.get("expiry")
                }
            )
            exact_expiry = exact_expiries[0] if len(exact_expiries) == 1 else None
        if (
            not isinstance(exact_expiry, str)
            or len(exact_expiry) != 8
            or not exact_expiry.isdigit()
        ):
            raise ValueError("Canonical GEX test snapshot requires one exact expiry")
        raw_strikes = raw.get("strikes") if isinstance(raw.get("strikes"), list) else []
        source_strikes = {
            float(row["strike"]): dict(row)
            for row in raw_strikes
            if isinstance(row, dict)
            and isinstance(row.get("strike"), (int, float))
            and not isinstance(row.get("strike"), bool)
        }
        strike_seeds: list[dict[str, Any]] = []
        used_strikes: set[float] = set()
        for level in supplied_levels:
            if not isinstance(level, dict):
                raise ValueError("Canonical GEX test levels must be mappings")
            price = float(level["price"])
            current = (
                level.get("option_volume_context", {}).get("current", {})
                if isinstance(level.get("option_volume_context"), dict)
                else {}
            )
            seed = {
                **source_strikes.get(price, {}),
                "strike": price,
                "call_gex": float(level.get("call_gex") or 0.0),
                "put_gex": float(level.get("put_gex") or 0.0),
            }
            if isinstance(current, dict):
                for field in ("call_volume", "put_volume", "call_oi", "put_oi"):
                    if current.get(field) is not None and field not in seed:
                        seed[field] = current[field]
            strike_seeds.append(seed)
            used_strikes.add(price)
        offset = 1
        while len(strike_seeds) < strike_count:
            candidate = spot + float(offset)
            offset += 1
            if candidate in used_strikes:
                continue
            strike_seeds.append(
                {
                    "strike": candidate,
                    "call_gex": 0.0,
                    "put_gex": 0.0,
                    "call_volume": 0.0,
                    "put_volume": 0.0,
                }
            )
            used_strikes.add(candidate)
        futures_options = instrument.get("asset_class") == "future"
        exchange = "CME" if futures_options else "SMART"
        multiplier = 50.0 if futures_options else 100.0
        contracts = _activity_contract_rows(
            strike_seeds,
            expiry=exact_expiry,
            trading_class=provider_symbol,
            exchange=exchange,
            multiplier=multiplier,
            spot=spot,
        )
        contracts = _with_gex_test_contract_observations(
            contracts,
            entitlement=body.get("market_data_entitlement", "live"),
            observed_at=captured_at,
        )
        strikes = gex._aggregate_by_strike(contracts, spot)
        entitlement = body.get("market_data_entitlement", "live")
        meta = {
            "provider_symbol": provider_symbol,
            "requested_market_data_entitlement": request_meta.get(
                "requested_market_data_entitlement",
                "live",
            ),
            "market_data_entitlement": entitlement,
            "spot": spot,
            "spot_observation": _gex_test_spot_observation(
                spot,
                entitlement=entitlement,
                observed_at=captured_at,
            ),
            "refresh_mode": capture_mode,
            "strike_count": strike_count,
            "requested_strike_limit": request_meta.get(
                "requested_strike_limit",
                strike_count,
            ),
            "futures_options": futures_options,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": len(contracts) // 2,
        }
        for field in ("scheduler_lane", "max_expirations", "max_contracts"):
            if field in request_meta:
                meta[field] = request_meta[field]
        meta["option_universe_expires_at"] = min(str(row["expiry_at"]) for row in contracts)
        built = gex._payload_from_rows(
            provider_symbol,
            captured_at=captured_at,
            meta=meta,
            strikes=strikes,
            diagnostics=gex._gex_diagnostics(
                contracts,
                strikes,
                contracts_requested=len(contracts),
                strikes_requested=strike_count,
                generic_ticks="101",
                analysis_contract_rows=contracts,
                authoritative_contract_rows=(contracts if entitlement == "live" else ()),
            ),
            max_levels=strike_count,
            stale_minutes=999999,
            source=source,
            capture_mode=capture_mode,
            contract_rows=contracts,
            comparison_contract_rows=contracts,
        )
        supplied_by_price = {
            float(level["price"]): level for level in supplied_levels if isinstance(level, dict)
        }
        for level in built["levels"]:
            supplied = supplied_by_price.get(float(level["price"]))
            if not isinstance(supplied, dict):
                continue
            supplied_context = supplied.get("option_volume_context")
            if isinstance(supplied_context, dict):
                level["option_volume_context"]["event"] = supplied_context.get("event")
            if isinstance(supplied.get("motion"), dict):
                level["motion"] = deepcopy(supplied["motion"])
        for field in ("option_activity", "option_activity_status", "live"):
            if field in body:
                built[field] = deepcopy(body[field])
        body = built
    body.setdefault("provider_symbol", provider_symbol)
    body.setdefault("captured_at", captured_at.isoformat())
    body.setdefault("expiry_profile", [])
    body["source"] = source
    body.setdefault(
        "capture_revision",
        gex_capture_revision_at(
            captured_at,
            capture_mode=body.get("capture_mode"),
        ).isoformat(),
    )
    body["instrument_id"] = instrument_id
    body["route_fingerprint"] = route_key
    body["comparison_scope"] = gex_comparison_scope_from_payload(body)
    body.setdefault("option_activity", None)
    body.setdefault("option_activity_status", "NEED_PREVIOUS")
    return {
        "provider_symbol": provider_symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": route_key,
        "captured_at": captured_at.isoformat(),
        "source": source,
        "payload": body,
    }


def _gex_test_spot_observation(
    spot: float,
    *,
    entitlement: str = "live",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    market_data_type = {
        "live": 1,
        "frozen": 2,
        "delayed": 3,
        "delayed_frozen": 4,
    }.get(entitlement)
    if observed_at is not None and entitlement == "live":
        return {
            "price": float(spot),
            "price_source": "bid_ask_mid",
            "bid": float(spot) - 0.5,
            "ask": float(spot) + 0.5,
            "last": None,
            "received_at": observed_at.astimezone(UTC).isoformat(),
            "provider_ts": None,
            "time_basis": "client_receive",
            "market_data_type": market_data_type,
            "market_data_entitlement": entitlement,
        }
    return {
        "price": float(spot),
        "price_source": "ibkr_market_price",
        "bid": None,
        "ask": None,
        "last": None,
        "received_at": None,
        "provider_ts": None,
        "time_basis": None,
        "market_data_type": market_data_type,
        "market_data_entitlement": entitlement,
    }


def _with_gex_test_contract_observations(
    rows: list[dict[str, Any]],
    *,
    entitlement: str = "live",
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    market_data_type = {
        "live": 1,
        "frozen": 2,
        "delayed": 3,
        "delayed_frozen": 4,
    }.get(entitlement)
    timestamp = observed_at.astimezone(UTC).isoformat() if observed_at else None
    return [
        {
            **row,
            "expiry_at": row.get(
                "expiry_at",
                _ACTIVE_OPTION_EXPIRY_AT.isoformat(),
            ),
            "expiry_time_source": row.get(
                "expiry_time_source",
                "ibkr_contract_details",
            ),
            "greek_source": row.get("greek_source", "modelGreeks"),
            "market_data_type": market_data_type,
            "market_data_entitlement": entitlement,
            "model_greeks_received_at": timestamp,
            "open_interest_received_at": timestamp,
            "observation_time_basis": "client_receive",
            "open_interest_as_of": "previous_settlement",
        }
        for row in rows
    ]


def _activity_contract_rows(
    strikes: list[dict[str, Any]],
    *,
    expiry: str = "20260721",
    trading_class: str = "ES",
    exchange: str = "CME",
    multiplier: float = 50.0,
    spot: float | None = None,
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for row in strikes:
        strike = float(row["strike"])
        strike_id = int(round(strike * 100))
        call_gex = float(row.get("call_gex", 1.0))
        put_gex = float(row.get("put_gex", -1.0))
        call_oi = float(row.get("call_oi", 0.0 if call_gex == 0 else 1.0))
        put_oi = float(row.get("put_oi", 0.0 if put_gex == 0 else 1.0))
        if call_oi <= 0 and call_gex != 0:
            call_oi = 1.0
        if put_oi <= 0 and put_gex != 0:
            put_oi = 1.0
        call_gamma = (
            abs(call_gex) / (call_oi * multiplier * spot * spot * 0.01)
            if spot is not None and call_gex != 0 and call_oi > 0
            else 0.000000000001
            if call_gex == 0 and call_oi == 0
            else 0.0
            if call_gex == 0
            else 0.01
        )
        put_gamma = (
            abs(put_gex) / (put_oi * multiplier * spot * spot * 0.01)
            if spot is not None and put_gex != 0 and put_oi > 0
            else 0.000000000001
            if put_gex == 0 and put_oi == 0
            else 0.0
            if put_gex == 0
            else 0.01
        )
        contracts.extend(
            [
                {
                    "con_id": strike_id * 2 + 1,
                    "expiry": expiry,
                    "trading_class": trading_class,
                    "exchange": exchange,
                    "multiplier": multiplier,
                    "strike": strike,
                    "right": "C",
                    "gamma": call_gamma,
                    "open_interest": call_oi,
                    "volume": float(row.get("call_volume", 0.0)),
                    "reference_option_price": None,
                    "reference_option_price_source": "unavailable",
                    "gex": call_gex,
                    "abs_gex": abs(call_gex),
                },
                {
                    "con_id": strike_id * 2 + 2,
                    "expiry": expiry,
                    "trading_class": trading_class,
                    "exchange": exchange,
                    "multiplier": multiplier,
                    "strike": strike,
                    "right": "P",
                    "gamma": put_gamma,
                    "open_interest": put_oi,
                    "volume": float(row.get("put_volume", 0.0)),
                    "reference_option_price": None,
                    "reference_option_price_source": "unavailable",
                    "gex": put_gex,
                    "abs_gex": abs(put_gex),
                },
            ]
        )
    return _with_gex_test_contract_observations(contracts)


def test_gex_disabled_does_not_touch_cache_or_live(monkeypatch) -> None:
    monkeypatch.setattr(
        gex_context_module,
        "cached_gex_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache read")),
    )
    result = gex.gex_context(enabled=False, refresh=True, instrument=_ES)

    assert result["status"] == "disabled"
    assert result["levels"] == []


def test_gex_refresh_error_uses_exception_class_when_message_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        gex_context_module, "provider_schedule_open_state", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    monkeypatch.setattr(
        gex_context_module,
        "cached_gex_context",
        lambda *args, **kwargs: {
            "ok": False,
            "enabled": True,
            "provider_symbol": "ES",
            "status": "missing",
            "message": "",
            "levels": [],
            "history": [],
        },
    )

    result = asyncio.run(gex.async_gex_context(enabled=True, refresh=True, instrument=_ES))

    assert result["status"] == "refresh_error"
    assert "TimeoutError" in result["message"]


def test_gex_request_expiry_boundary_is_typed_unavailable_and_display_only(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gex_context_module,
        "provider_schedule_open_state",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ibkr_gex.IbkrOptionUniverseUnavailableError(
                "the exact 0DTE series has expired",
                reason="NO_ACTIVE_0DTE",
                diagnostics={"expired_series_excluded": 1},
            )
        ),
    )
    monkeypatch.setattr(
        gex_context_module,
        "cached_gex_context",
        lambda *args, **kwargs: {
            "ok": True,
            "status": "ok",
            "frame_complete": True,
            "decision_authoritative": True,
            "levels": [{"price": 7575.0}],
            "history": [],
        },
    )

    result = asyncio.run(
        gex.async_gex_context(
            enabled=True,
            refresh=True,
            instrument=_ES,
        )
    )

    assert result["ok"] is True
    assert result["status"] == "unavailable"
    assert result["degraded"] is True
    assert result["decision_authoritative"] is False
    assert result["levels"] == [{"price": 7575.0}]
    assert result["error"]["code"] == "GEX_OPTION_UNIVERSE_UNAVAILABLE"
    assert result["latest_attempt"]["diagnostics"]["option_universe_reason"] == "NO_ACTIVE_0DTE"


def test_gex_request_stale_spot_is_typed_unavailable_without_option_retry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gex_context_module,
        "provider_schedule_open_state",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GexUnderlyingQuoteUnavailableError(
                "the underlying observation is stale",
                diagnostics={"underlying_spot_age_seconds": 45.0},
            )
        ),
    )
    monkeypatch.setattr(
        gex_context_module,
        "cached_gex_context",
        lambda *args, **kwargs: {
            "ok": True,
            "status": "ok",
            "frame_complete": True,
            "decision_authoritative": True,
            "levels": [{"price": 7575.0}],
            "history": [],
        },
    )

    result = asyncio.run(
        gex.async_gex_context(
            enabled=True,
            refresh=True,
            instrument=_ES,
        )
    )

    assert result["ok"] is True
    assert result["status"] == "unavailable"
    assert result["degraded"] is True
    assert result["decision_authoritative"] is False
    assert result["error"]["code"] == "GEX_UNDERLYING_QUOTE_UNAVAILABLE"
    assert result["error"]["reason"] == "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
    assert result["latest_attempt"]["diagnostics"]["underlying_spot_age_seconds"] == 45.0


def test_gex_payload_explains_missing_open_interest_gamma() -> None:
    payload = gex._payload_from_rows(
        "ES",
        captured_at=datetime(2026, 5, 27, 9, 0, tzinfo=UTC),
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 5000.0,
            "spot_observation": _gex_test_spot_observation(5000.0),
            "strike_count": 1,
            "requested_strike_limit": 1,
            "futures_options": True,
            "selected_expiries": ["20260601", "20260602"],
            "option_universe_expires_at": (datetime(2026, 6, 1, 20, 0, tzinfo=UTC).isoformat()),
        },
        strikes=[
            {
                "strike": 5000.0,
                "call_gex": 0.0,
                "put_gex": 0.0,
                "net_gex": 0.0,
                "abs_gex": 0.0,
                "call_oi": 0.0,
                "put_oi": 0.0,
            }
        ],
        diagnostics={
            "diag_contracts_requested": 2,
            "diag_strikes_requested": 1,
            "diag_option_rows": 2,
            "diag_strike_rows": 1,
            "diag_gamma_rows": 2,
            "diag_usable_gamma_rows": 2,
            "diag_open_interest_rows": 0,
            "diag_open_interest_unavailable_rows": 2,
            "diag_usable_gex_rows": 0,
            "diag_nonzero_gex_rows": 0,
            "diag_expected_pair_count": 1,
            "diag_received_pair_count": 1,
            "diag_usable_pair_count": 0,
            "diag_unpaired_series_strikes": 0,
            "diag_call_oi_total": 0.0,
            "diag_put_oi_total": 0.0,
            "diag_greek_sources": "modelGreeks",
            "diag_generic_ticks": "101",
            "diag_collection_timeout": False,
        },
        max_levels=4,
        stale_minutes=999999,
        source="gex:ibkr",
        capture_mode="request",
        comparison_contract_rows=None,
    )

    assert payload["status"] == "partial"
    assert "open interest is zero or missing" in payload["message"]
    assert "strikes 1/1" in payload["message"]
    assert payload["diagnostics"]["diag_gamma_rows"] == 2
    assert payload["diagnostics"]["diag_open_interest_rows"] == 0
    assert payload["diagnostics"]["diag_strikes_requested"] == 1


def test_gex_payload_levels_include_typed_kind_class() -> None:
    captured_at = datetime(2026, 5, 27, 9, 0, tzinfo=UTC)
    requested_strikes = [
        {"strike": 95.0, "call_gex": 20.0, "put_gex": -260.0, "net_gex": -240.0, "abs_gex": 280.0},
        {"strike": 100.0, "call_gex": 180.0, "put_gex": -160.0, "net_gex": 20.0, "abs_gex": 340.0},
        {"strike": 105.0, "call_gex": 300.0, "put_gex": -40.0, "net_gex": 260.0, "abs_gex": 340.0},
        {"strike": 110.0, "call_gex": 0.0, "put_gex": 0.0, "net_gex": 0.0, "abs_gex": 0.0},
    ]
    contract_rows = _activity_contract_rows(
        requested_strikes,
        expiry="20260601",
        spot=100.0,
    )
    strike_rows = gex._aggregate_by_strike(contract_rows, 100.0)
    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 100.0,
            "spot_observation": _gex_test_spot_observation(
                100.0,
                observed_at=captured_at,
            ),
            "strike_count": 4,
            "requested_strike_limit": 4,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": len(contract_rows) // 2,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in contract_rows),
        },
        strikes=strike_rows,
        diagnostics=gex._gex_diagnostics(
            contract_rows,
            strike_rows,
            contracts_requested=len(contract_rows),
            strikes_requested=4,
            generic_ticks="101",
        ),
        max_levels=4,
        stale_minutes=999999,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=contract_rows,
        comparison_contract_rows=contract_rows,
    )

    classes = {level["kind"]: level["kind_class"] for level in payload["levels"]}
    assert classes["CALL_WALL"] == "call"
    assert classes["PUT_WALL"] == "put"
    assert {level["spot_side"] for level in payload["levels"]} <= {"above", "below", "inside"}
    assert all(
        "role_text" not in level and "action_text" not in level and "kind_role" not in level
        for level in payload["levels"]
    )
    assert [level["price"] for level in payload["levels"]] == [95.0, 100.0, 105.0]
    assert [level["selection_rank"] for level in payload["levels"]] == [3, 1, 2]
    assert all(
        set(level)
        == {
            "price",
            "kind",
            "kind_class",
            "strength",
            "power_class",
            "selection_rank",
            "net_gex",
            "call_gex",
            "put_gex",
            "abs_gex",
            "abs_flow_1pt",
            "distance_from_spot",
            "zone_half_width",
            "spot_side",
            "option_volume_context",
        }
        for level in payload["levels"]
    )
    levels_by_strike = {row["price"]: row for row in payload["levels"]}
    assert levels_by_strike[100.0]["call_gex"] == 180.0
    assert levels_by_strike[100.0]["put_gex"] == -160.0
    assert payload["visibility_summary"]["nonzero_strike_count"] == 3
    assert payload["visibility_summary"]["zero_strike_count"] == 1
    assert payload["provider_symbol"] == "ES"
    assert all(
        field not in payload
        for field in ("asset", "symbol", "recommendations", "frame_source", "profile", "nearest")
    )
    motion = {
        "call": {
            "type": "strengthening",
            "side": "call",
            "from": 10.0,
            "to": 20.0,
            "delta": 10.0,
            "delta_pct": 1.0,
            "window_seconds": 30.0,
        }
    }
    level_with_motion = {
        **payload["levels"][1],
        "selection_rank": 1,
        "motion": motion,
    }
    assert (
        gex.require_gex_levels(
            [level_with_motion],
            max_levels=1,
            spot=payload["spot"],
        )[0]["motion"]
        == motion
    )


def test_gex_level_power_class_uses_published_rounded_strength() -> None:
    abs_gex_by_strike = {
        90.0: 999.6,
        95.0: 5499.6,
        105.0: 8499.6,
        110.0: 10000.0,
    }
    strikes = [
        {
            "strike": strike,
            "call_gex": abs_gex,
            "put_gex": 0.0,
            "net_gex": abs_gex,
            "abs_gex": abs_gex,
        }
        for strike, abs_gex in abs_gex_by_strike.items()
    ]

    levels = gex._select_gex_levels(
        strikes,
        100.0,
        {
            "call_wall": 110.0,
            "put_wall": None,
            "net_gex": sum(abs_gex_by_strike.values()),
        },
        max_levels=4,
    )

    by_price = {level["price"]: level for level in levels}
    assert (by_price[90.0]["strength"], by_price[90.0]["power_class"]) == (
        0.1,
        "MEDIUM",
    )
    assert (by_price[95.0]["strength"], by_price[95.0]["power_class"]) == (
        0.55,
        "STRONG",
    )
    assert (by_price[105.0]["strength"], by_price[105.0]["power_class"]) == (
        0.85,
        "EXTREME",
    )
    assert by_price[90.0]["zone_half_width"] == 1.2499
    assert gex.require_gex_levels(levels, max_levels=4, spot=100.0) == levels


def test_gamma_flip_reprices_paired_chain_to_zero_crossing() -> None:
    rows = [
        {
            "expiry": "20260717",
            "trading_class": "SPY",
            "exchange": "SMART",
            "strike": strike,
            "right": right,
            "time_to_expiry_years": 0.05,
            "iv": 0.20,
            "gamma": 0.01,
            "open_interest": oi,
            "volume": None,
            "gex": 1.0 if right == "C" else -1.0,
            "abs_gex": 1.0,
            "multiplier": 100,
        }
        for strike, right, oi in (
            (95, "C", 10),
            (95, "P", 100),
            (105, "C", 100),
            (105, "P", 10),
        )
    ]

    flip = gex.compute_gamma_flip(
        False,
        rows,
        100,
        risk_free_rate=0.04,
        dividend_yield=0.0,
    )

    assert flip is not None
    assert 99.0 < flip < 101.0
    assert (
        gex.compute_gamma_flip(
            False,
            rows[:2],
            100,
            risk_free_rate=0.04,
            dividend_yield=0.0,
        )
        is None
    )
    assert (
        gex.compute_gamma_flip(
            False,
            rows[:-1],
            100,
            risk_free_rate=0.04,
            dividend_yield=0.0,
        )
        is None
    )
    mixed_series = [{**row, "exchange": "CBOE"} if row["right"] == "P" else row for row in rows]
    assert (
        gex.compute_gamma_flip(
            False,
            mixed_series,
            100,
            risk_free_rate=0.04,
            dividend_yield=0.0,
        )
        is None
    )

    zero_oi_pair = [
        {
            **row,
            "open_interest": 0,
            "gex": 0.0,
            "abs_gex": 0.0,
        }
        if row["strike"] == 95 and row["right"] == "C"
        else row
        for row in rows
    ]
    zero_oi_flip = gex.compute_gamma_flip(
        False,
        zero_oi_pair,
        100,
        risk_free_rate=0.04,
        dividend_yield=0.0,
    )
    assert zero_oi_flip is not None

    invalid_model_row = [
        {**row, "iv": None} if row["strike"] == 95 and row["right"] == "C" else row for row in rows
    ]
    assert (
        gex.compute_gamma_flip(
            False,
            invalid_model_row,
            100,
            risk_free_rate=0.04,
            dividend_yield=0.0,
        )
        is None
    )
    assert (
        gex.compute_gamma_flip(
            False,
            [*rows, dict(rows[0])],
            100,
            risk_free_rate=0.04,
            dividend_yield=0.0,
        )
        is None
    )
    normalized_model_row = [
        {**row, "time_to_expiry_years": "0.05"}
        if row["strike"] == 95 and row["right"] == "C"
        else row
        for row in rows
    ]
    assert (
        gex.compute_gamma_flip(
            False,
            normalized_model_row,
            100,
            risk_free_rate=0.04,
            dividend_yield=0.0,
        )
        is None
    )


def test_gex_runtime_status_distinguishes_broker_greeks_from_local_gex_math() -> None:
    status = gex.gex_runtime_status()

    assert status["input_greeks_source"] == "broker_option_stream_plus_local_iv"
    assert status["greek_source_priority"][0] == "modelGreeks"
    assert "anchor_iv" not in status["greek_source_priority"]
    assert "gamma_flip" in status["computed_fields"]
    assert "strike_net_gex" in status["computed_fields"]
    assert status["generic_ticks"] == "101"
    assert isinstance(status["routes"], dict)
    assert "last_diagnostics" not in status
    assert "scheduler_enabled" not in status
    assert "scheduler_symbols" not in status


def test_gex_runtime_status_reads_canonical_mutable_state(monkeypatch) -> None:
    captured_at = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    instrument_id = _instrument_id(_ES)
    route_key = _route_key(_ES)
    status_key = _status_route_key(_ES)
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    gex._GEX_RUNTIME.record_error(
        instrument_id,
        route_key,
        "probe",
        provider_symbol="ES",
        source="manual",
    )
    gex._GEX_RUNTIME.record_sync(
        instrument_id,
        route_key,
        captured_at,
        provider_symbol="ES",
    )
    for _ in range(17):
        gex._GEX_RUNTIME.record_diagnostics(instrument_id, route_key, {})

    status = gex.gex_runtime_status()

    assert status["last_sync_at"] == captured_at.isoformat()
    assert status["routes"][status_key]["errors"]["manual"]["message"] == "probe"
    assert status["routes"][status_key]["request_count"] == 17
    assert status["request_count_total"] == 17
    assert "last_error" not in status


def test_gex_runtime_errors_do_not_cross_asset_boundaries(monkeypatch) -> None:
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    es_sync = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    es_id = _instrument_id(_ES)
    qqq_id = _instrument_id(_QQQ)
    es_route = _route_key(_ES)
    qqq_route = _route_key(_QQQ)
    es_status_key = _status_route_key(_ES)
    qqq_status_key = _status_route_key(_QQQ)
    gex._GEX_RUNTIME.mark_request_started(
        es_id,
        es_route,
        "probe",
        provider_symbol="ES",
    )
    gex._GEX_RUNTIME.mark_request_finished(es_id, es_route)
    gex._GEX_RUNTIME.record_sync(es_id, es_route, es_sync, provider_symbol="ES")
    gex._GEX_RUNTIME.record_diagnostics(es_id, es_route, {"diag_option_rows": 36})
    gex._GEX_RUNTIME.record_error(
        qqq_id,
        qqq_route,
        "provider timeout",
        provider_symbol="QQQ",
        source="auto",
    )

    status = gex.gex_runtime_status()

    assert status["routes"][es_status_key]["errors"] == {}
    assert status["routes"][es_status_key]["last_sync_at"] == es_sync.isoformat()
    assert status["routes"][es_status_key]["last_diagnostics"]["diag_option_rows"] == 36
    assert status["routes"][qqq_status_key]["errors"]["auto"]["message"] == "provider timeout"


def test_gex_runtime_errors_are_isolated_by_route_and_source(monkeypatch) -> None:
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    es_id = _instrument_id(_ES)
    qqq_id = _instrument_id(_QQQ)
    es_route = _route_key(_ES)
    qqq_route = _route_key(_QQQ)
    es_status_key = _status_route_key(_ES)
    qqq_status_key = _status_route_key(_QQQ)
    gex._GEX_RUNTIME.record_error(
        es_id,
        es_route,
        "manual failed",
        provider_symbol="ES",
        source="manual",
    )
    gex._GEX_RUNTIME.record_error(
        es_id,
        es_route,
        "live failed",
        provider_symbol="ES",
        source="live",
    )
    gex._GEX_RUNTIME.record_error(
        qqq_id,
        qqq_route,
        "auto failed",
        provider_symbol="QQQ",
        source="auto",
    )

    gex._GEX_RUNTIME.clear_error(es_id, es_route, source="live")
    status = gex.gex_runtime_status()
    assert set(status["routes"][es_status_key]["errors"]) == {"manual"}
    assert set(status["routes"][qqq_status_key]["errors"]) == {"auto"}

    gex._GEX_RUNTIME.clear_request_errors(es_id, es_route)
    status = gex.gex_runtime_status()
    assert status["routes"][es_status_key]["errors"] == {}
    assert set(status["routes"][qqq_status_key]["errors"]) == {"auto"}


def test_gex_runtime_identity_pair_isolates_equal_route_fingerprints(monkeypatch) -> None:
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    shared_fingerprint = "same-route-generation"
    first_id = _instrument_id(_SPY)
    second_id = _instrument_id(_QQQ)

    gex._GEX_RUNTIME.record_error(
        first_id,
        shared_fingerprint,
        "first failed",
        source="manual",
    )

    assert (
        gex._GEX_RUNTIME.error_for_route(
            first_id,
            shared_fingerprint,
        )["message"]
        == "first failed"
    )
    assert (
        gex._GEX_RUNTIME.error_for_route(
            second_id,
            shared_fingerprint,
        )["message"]
        == ""
    )
    assert set(gex._GEX_RUNTIME.routes) == {(first_id, shared_fingerprint)}


def test_gex_failure_backoff_is_route_scoped_and_clears_after_recovery() -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    qqq_id = _instrument_id(_QQQ)
    es_id = _instrument_id(_ES)
    qqq_route = _route_key(_QQQ)
    es_route = _route_key(_ES)

    first = gex._GEX_RUNTIME.record_failure(
        qqq_id,
        qqq_route,
        "qualification timeout",
        provider_symbol="QQQ",
        source="auto",
        lane="request",
        at=now,
    )
    second = gex._GEX_RUNTIME.record_failure(
        qqq_id,
        qqq_route,
        "qualification timeout",
        source="auto",
        lane="request",
        at=now + timedelta(seconds=31),
    )

    assert first["retry_in_seconds"] == 30.0
    assert second["retry_in_seconds"] == 60.0
    assert (
        gex._GEX_RUNTIME.failure_backoff(
            qqq_id,
            qqq_route,
            lane="request",
            now=now + timedelta(seconds=32),
        )["active"]
        is True
    )
    assert (
        gex._GEX_RUNTIME.failure_backoff(
            qqq_id,
            qqq_route,
            lane="request",
            now=now + timedelta(seconds=32),
        )["message"]
        == "qualification timeout"
    )
    assert (
        gex._GEX_RUNTIME.failure_backoff(
            qqq_id,
            qqq_route,
            lane="live",
            now=now + timedelta(seconds=32),
        )["active"]
        is False
    )
    assert (
        gex._GEX_RUNTIME.failure_backoff(
            es_id,
            es_route,
            lane="request",
            now=now + timedelta(seconds=32),
        )["active"]
        is False
    )

    gex._GEX_RUNTIME.clear_failure(
        qqq_id,
        qqq_route,
        lane="request",
    )

    assert (
        gex._GEX_RUNTIME.failure_backoff(
            qqq_id,
            qqq_route,
            lane="request",
            now=now + timedelta(seconds=32),
        )["active"]
        is False
    )


def test_gex_live_backoff_skips_options_lane(monkeypatch) -> None:
    gex._GEX_RUNTIME.record_failure(
        _instrument_id(_QQQ),
        _route_key(_QQQ),
        "qualification timeout",
        provider_symbol="QQQ",
        source="live",
        lane="live",
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("route backoff must skip the options lane")

    monkeypatch.setattr(ibkr_gex_provider_runtime, "run_coroutine", fail_if_called)

    payload = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_QQQ,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=_gex_test_spot_observation(
                717.0,
                observed_at=datetime.now(tz=UTC),
            ),
        )
    )

    assert payload["status"] == "degraded"
    assert payload["error"]["code"] == "GEX_LIVE_BACKOFF"
    assert payload["backoff"]["active"] is True


def test_gex_live_expiry_boundary_uses_typed_unavailable_backoff(
    monkeypatch,
) -> None:
    lane_calls = 0

    stop_calls = 0

    async def unavailable(_lane, label, _coroutine_factory, **_kwargs):
        nonlocal lane_calls, stop_calls
        if "stop" in label:
            stop_calls += 1
            return {"ok": True, "stopped": 0}
        lane_calls += 1
        raise ibkr_gex.IbkrOptionUniverseUnavailableError(
            "the exact option universe crossed expiry",
            reason="OPTION_UNIVERSE_ROLLOVER",
            diagnostics={"expired_contracts_excluded": 2},
        )

    monkeypatch.setattr(
        gex_context_module,
        "gex_provider_session_state",
        lambda **_kwargs: "open",
    )
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "run_coroutine",
        unavailable,
    )

    quote = _gex_test_spot_observation(
        717.0,
        observed_at=datetime.now(tz=UTC),
    )
    first = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_QQQ,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=quote,
        )
    )
    second = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_QQQ,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=quote,
        )
    )

    assert lane_calls == 1
    assert first["status"] == "unavailable"
    assert first["error"]["code"] == "GEX_OPTION_UNIVERSE_UNAVAILABLE"
    assert first["backoff"]["kind"] == "option_universe_unavailable"
    assert first["backoff"]["reason"] == "OPTION_UNIVERSE_ROLLOVER"
    assert first["backoff"]["lane"] == "live"
    assert second["status"] == "unavailable"
    assert second["error"]["code"] == "GEX_OPTION_UNIVERSE_UNAVAILABLE"
    assert second["backoff"]["kind"] == "option_universe_unavailable"
    assert second["backoff"]["reason"] == "OPTION_UNIVERSE_ROLLOVER"
    assert stop_calls == 1


def test_gex_live_app_path_reads_existing_inputs_only_on_options_lane(monkeypatch) -> None:
    request = _live_request(instrument=_ES)
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "owned-inputs"),
        request=request,
        spot=7575.0,
        dividend_cache={"cached": True, "value": 0.01},
    )
    gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] = session
    lane_calls: list[tuple[str, str]] = []
    owned_inputs: list[tuple[gex.GexRequestConfig, dict[str, Any]]] = []

    async def run_coroutine(lane, label, coroutine_factory, **_kwargs):
        lane_calls.append((lane, label))
        return await coroutine_factory()

    async def live_frame(
        _provider_symbol,
        *,
        request: gex.GexRequestConfig,
        dividend_cache: dict[str, Any],
        **_kwargs,
    ):
        owned_inputs.append((request, dividend_cache))
        return gex_live._LiveGexOwnedFrame(
            payload={"ok": True, "provider_symbol": "ES"},
            session_token=_live_gex_session_token(session),
        )

    async def finalize(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        gex_context_module,
        "gex_provider_session_state",
        lambda **_kwargs: "open",
    )
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "run_coroutine",
        run_coroutine,
    )
    monkeypatch.setattr(gex_live, "_live_gex_context_owned", live_frame)
    monkeypatch.setattr(gex_live, "_finalize_live_gex_frame", finalize)
    monkeypatch.setattr(
        gex_live,
        "_live_request",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("app path must not read an owned session directly")
        ),
    )

    payload = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_ES,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=_gex_test_spot_observation(
                7575.0,
                observed_at=datetime.now(tz=UTC),
            ),
        )
    )

    assert payload == {"ok": True, "provider_symbol": "ES"}
    assert [lane for lane, _label in lane_calls] == ["options", "options"]
    assert lane_calls[0][1] == "ES live gex inputs"
    assert owned_inputs == [(request, {"cached": True, "value": 0.01})]


def test_gex_live_finalization_settles_before_cancellation(monkeypatch) -> None:
    request = _live_request(instrument=_ES)
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "finalization-cancel"),
        request=request,
        spot=7575.0,
        dividend_cache={"cached": True, "value": 0.01},
    )
    gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] = session

    async def run_coroutine(_lane, _label, coroutine_factory, **_kwargs):
        return await coroutine_factory()

    async def live_frame(*_args, **_kwargs):
        return gex_live._LiveGexOwnedFrame(
            payload={"ok": True, "provider_symbol": "ES"},
            session_token=_live_gex_session_token(session),
        )

    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def finalize(*_args, **_kwargs):
        started.set()
        await release.wait()
        completed.set()

    monkeypatch.setattr(
        gex_context_module,
        "gex_provider_session_state",
        lambda **_kwargs: "open",
    )
    monkeypatch.setattr(ibkr_gex_provider_runtime, "run_coroutine", run_coroutine)
    monkeypatch.setattr(gex_live, "_live_gex_context_owned", live_frame)
    monkeypatch.setattr(gex_live, "_finalize_live_gex_frame", finalize)

    async def run() -> None:
        task = asyncio.create_task(
            gex_live.async_live_gex_context(
                enabled=True,
                instrument=_ES,
                provider_runtime=ibkr_gex_provider_runtime,
                underlying_quote=_gex_test_spot_observation(
                    7575.0,
                    observed_at=datetime.now(tz=UTC),
                ),
            )
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()

    asyncio.run(run())


def test_gex_live_persistence_settles_worker_before_cancellation(monkeypatch) -> None:
    captured_at = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    commit_called = False

    plan = SimpleNamespace(
        provider_symbol="SPY",
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        captured_at=captured_at,
        bucket_at=captured_at,
        refresh_dividend_cache=False,
        persist_snapshot=True,
    )

    async def prepare(*_args: Any, **_kwargs: Any) -> Any:
        return plan

    async def commit(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal commit_called
        commit_called = True
        return None

    async def run_coroutine(_lane, _label, coroutine_factory, **_kwargs):
        return await coroutine_factory()

    def persist(*_args: Any, **_kwargs: Any) -> bool:
        started.set()
        assert release.wait(1.0)
        finished.set()
        return True

    monkeypatch.setattr(gex_live, "_prepare_live_gex_finalization_owned", prepare)
    monkeypatch.setattr(gex_live, "_commit_live_gex_finalization_owned", commit)
    monkeypatch.setattr(gex_live, "persist_gex_snapshot", persist)
    provider_runtime = SimpleNamespace(run_coroutine=run_coroutine)
    payload = {
        "live": {},
        "raw": {"meta": {}, "strikes": [], "contracts": []},
    }

    async def run() -> None:
        task = asyncio.create_task(
            gex_live._finalize_live_gex_frame(
                SimpleNamespace(identity_key=(_instrument_id(_SPY), _route_key(_SPY))),
                payload,
                store=object(),
                provider_runtime=provider_runtime,
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()

    assert finished.is_set()
    assert commit_called is False


def test_gex_runtime_status_treats_live_sessions_as_connected(monkeypatch) -> None:
    runtime_status = ibkr_gex_provider_runtime.runtime_status()
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "runtime_status",
        lambda: replace(runtime_status, request_connected=False),
    )
    monkeypatch.setattr(
        gex_live,
        "live_gex_status",
        lambda: {
            "active": True,
            "subscriptions": 32,
            "sessions": [
                {
                    "provider_symbol": "ES",
                    "instrument_id": _instrument_id(_ES),
                    "route_fingerprint": _route_key(_ES),
                    "active": True,
                    "last_frame_at": "2026-07-15T08:05:00+00:00",
                    "last_diagnostics": {"diag_option_rows": 36},
                }
            ],
        },
    )

    status = gex.gex_runtime_status()

    assert status["connected"] is True
    assert status["request_connected"] is False
    assert status["live_connected"] is True
    assert status["subscriptions"] == 32
    assert status["last_sync_at"] == "2026-07-15T08:05:00+00:00"
    assert status["routes"][_status_route_key(_ES)]["last_sync_at"] == "2026-07-15T08:05:00+00:00"
    assert status["routes"][_status_route_key(_ES)]["last_diagnostics"]["diag_option_rows"] == 36


def test_gex_runtime_status_exposes_live_subscription_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        gex_live,
        "live_gex_status",
        lambda: {
            "active": True,
            "subscriptions": 72,
            "subscription_budget": 72,
            "subscription_over_budget": 0,
            "sessions": [],
        },
    )

    status = gex.gex_runtime_status()

    assert status["subscriptions"] == 72
    assert status["subscription_budget"] == 72
    assert status["subscription_over_budget"] == 0


def test_gex_config_exposes_streaming_only_contract(monkeypatch) -> None:
    monkeypatch.delenv("AEF_IBKR_GEX_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("AEF_IBKR_GEX_TIMEOUT_SECONDS", raising=False)

    assert AppConfig().ibkr_gex_wait_seconds == 2.0
    assert AppConfig().ibkr_gex_timeout_seconds == 65.0
    instrument = _ES
    request = gex._gex_request_config(instrument=instrument)
    assert request.provider_metadata.observation_wait_seconds == 2.0
    assert request.provider_metadata.timeout_seconds == 65.0
    assert gex.GEX_GENERIC_TICKS == "101"
    assert "generic_ticks" not in gex.GexRequestConfig.__dataclass_fields__
    assert "snapshot" not in gex.GexRequestConfig.__dataclass_fields__
    assert "quiet_oi_stream" not in gex.GexRequestConfig.__dataclass_fields__
    assert "symbol" not in gex.GexRequestConfig.__dataclass_fields__


def test_gex_request_timeout_reports_active_phase(monkeypatch) -> None:
    class FakeIb:
        def reqMarketDataType(self, _data_type: int) -> None:
            raise TimeoutError()

        def isConnected(self) -> bool:
            return True

    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: FakeIb(),
    )
    instrument = _ES
    request = gex.gex_request_config(instrument=instrument)

    with pytest.raises(TimeoutError, match="market data type"):
        gex._collect_live_gex_context_owned(
            "ES",
            request=request,
            dividend_cache={},
            underlying_quote=_gex_test_spot_observation(
                7500.0,
                observed_at=datetime.now(tz=UTC),
            ),
            instrument=instrument,
        )

    runtime_error = gex._GEX_RUNTIME.error_for_route(
        _instrument_id(_ES),
        _route_key(_ES),
        sources=("manual",),
    )
    assert runtime_error["message"] == "GEX refresh timed out during market data type"


def test_gex_request_reset_retains_owner_when_close_is_unconfirmed() -> None:
    class FakeIb:
        wrapper = SimpleNamespace(clientId=3517)

        def isConnected(self) -> bool:
            return True

        def sleep(self, _seconds: float) -> None:
            return None

        def disconnect(self) -> None:
            raise RuntimeError("disconnect failed")

    session = FakeIb()
    key = ("127.0.0.1", 4002, 3517, True)
    ibkr_gex._GEX_REQUEST_SESSION = session
    ibkr_gex._GEX_REQUEST_SESSION_KEY = key
    ibkr_gex._GEX_REQUEST_SESSION_TRANSITIONING = False
    try:
        with pytest.raises(RuntimeError, match="ownership retained"):
            ibkr_gex._reset_gex_session()

        assert ibkr_gex._GEX_REQUEST_SESSION is session
        assert ibkr_gex._GEX_REQUEST_SESSION_KEY == key
        assert ibkr_gex._GEX_REQUEST_SESSION_TRANSITIONING is False
    finally:
        ibkr_gex._GEX_REQUEST_SESSION = None
        ibkr_gex._GEX_REQUEST_SESSION_KEY = None
        ibkr_gex._GEX_REQUEST_SESSION_TRANSITIONING = False
        _IBKR_RUNTIME.release_quarantined_session(session)


def test_ibkr_gex_public_acquisition_strips_timeout_sentinel_and_owns_cleanup(monkeypatch) -> None:
    events: list[str] = []

    class FakeIb:
        def reqMarketDataType(self, _data_type: int) -> None:
            events.append("configure")

    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: events.append("connect") or FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: (
            events.append("qualify")
            or SimpleNamespace(
                conId=101,
                secType="FUT",
                symbol="ES",
                localSymbol="ESU6",
                currency="USD",
            )
        ),
    )
    contracts = [
        _ibkr_option(
            con_id=1,
            strike=7500.0,
            right="C",
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
        )
    ]
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: events.append("build") or (contracts, {"qualified_contracts": 1}),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_collect_option_rows_by_expiration",
        lambda *_args, **_kwargs: (
            events.append("collect")
            or [
                {
                    "strike": 7500.0,
                    "right": "C",
                    "gamma": 0.01,
                    "_ibkr_market_data_type": 1,
                },
                {"_gex_collection_timeout": True, "_gex_timeout_phase": "option market data"},
            ]
        ),
    )
    monkeypatch.setattr(ibkr_gex, "_reset_gex_session", lambda: events.append("reset"))

    result = ibkr_gex.fetch_gex_option_rows(
        instrument=_ES,
        request=_ibkr_provider_request(gex.gex_request_config(instrument=_ES)),
        spot=7500.0,
        dividend_yield=0.0,
    )

    assert events == ["connect", "configure", "qualify", "build", "collect", "reset"]
    assert result.contract_rows == ({"strike": 7500.0, "right": "C", "gamma": 0.01},)
    assert result.contracts_requested == 1
    assert result.chain_meta == {"qualified_contracts": 1}
    assert result.underlying_audit["underlying_local_symbol"] == "ESU6"
    assert result.collection_timed_out is True
    assert result.collection_timeout_phase == "option market data"
    assert not hasattr(result, "ib")


def test_ibkr_gex_request_reuses_exact_process_universe_until_edge(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeIb:
        def reqMarketDataType(self, _data_type: int) -> None:
            events.append("configure")

    request = replace(
        gex.gex_request_config(instrument=_ES),
        strike_count=9,
    )
    strikes = [
        7550.0,
        7555.0,
        7560.0,
        7565.0,
        7570.0,
        7575.0,
        7580.0,
        7585.0,
        7590.0,
    ]
    contracts = [
        _ibkr_option(
            con_id=index * 2 + (1 if right == "C" else 2),
            strike=strike,
            right=right,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
        )
        for index, strike in enumerate(strikes)
        for right in ("C", "P")
    ]
    underlying = SimpleNamespace(
        conId=101,
        secType="FUT",
        symbol="ES",
        localSymbol="ESU6",
        lastTradeDateOrContractMonth="20260918",
        currency="USD",
        exchange="CME",
    )
    underlying_audit = ibkr_gex._underlying_audit(underlying)
    state = ibkr_gex._IbkrRetainedGexRequestUniverse(
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME",
        request_contract=(
            request.strike_count,
            request.max_expirations,
            request.max_contracts,
            request.expiry_mode,
            request.futures_options,
        ),
        underlying_audit=tuple(underlying_audit.items()),
        contracts=tuple(contracts),
    )
    _IBKR_RUNTIME.gex_request_universe_cache[
        (qualified_instrument_id(_ES), route_fingerprint(_ES))
    ] = state
    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: events.append("connect") or FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: events.append("qualify") or underlying,
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("retained universe must not be requalified")
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_collect_option_rows_by_expiration",
        lambda *_args, **_kwargs: events.append("collect") or [],
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_reset_gex_session",
        lambda: events.append("reset"),
    )

    result = ibkr_gex.fetch_gex_option_rows(
        instrument=_ES,
        request=_ibkr_provider_request(request),
        spot=7570.0,
        dividend_yield=0.0,
    )

    assert events == ["connect", "configure", "qualify", "collect", "reset"]
    assert result.selected_contract_universe == (
        ibkr_gex._gex_selected_contract_universe(contracts)
    )
    assert result.chain_meta["qualification_source"] == ("retained_provider_selected_universe")
    assert result.chain_meta["contract_detail_requests"] == 0

    retained, _meta = ibkr_gex._retained_gex_request_universe(
        state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME",
        underlying_audit=underlying_audit,
        request=_ibkr_provider_request(request),
        spot=7580.0,
    )
    assert retained == []

    retained, _meta = ibkr_gex._retained_gex_request_universe(
        state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME",
        underlying_audit=underlying_audit,
        request=replace(
            request,
            max_expirations=request.max_expirations + 1,
        ),
        spot=7570.0,
    )
    assert retained == []

    retained, _meta = ibkr_gex._retained_gex_request_universe(
        state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=f"{route_fingerprint(_ES)}:other",
        provider_symbol="ES",
        underlying_exchange="CME",
        underlying_audit=underlying_audit,
        request=_ibkr_provider_request(request),
        spot=7570.0,
    )
    assert retained == []

    retained, _meta = ibkr_gex._retained_gex_request_universe(
        state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME",
        underlying_audit={
            **underlying_audit,
            "underlying_con_id": 202,
        },
        request=_ibkr_provider_request(request),
        spot=7570.0,
    )
    assert retained == []

    expired_state = replace(
        state,
        contracts=tuple(
            replace(
                contract,
                expiry="20200101",
                expiry_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
            for contract in contracts
        ),
    )
    retained, _meta = ibkr_gex._retained_gex_request_universe(
        expired_state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME",
        underlying_audit=underlying_audit,
        request=request,
        spot=7570.0,
    )
    assert retained == []

    rolled_request = replace(request, expiry_mode="1dte")
    rolled_expiry = datetime.now(ibkr_gex.NY_TZ).strftime("%Y%m%d")
    rolled_state = replace(
        state,
        request_contract=(
            rolled_request.strike_count,
            rolled_request.max_expirations,
            rolled_request.max_contracts,
            rolled_request.expiry_mode,
            rolled_request.futures_options,
        ),
        contracts=tuple(
            replace(
                contract,
                expiry=rolled_expiry,
                expiry_at=datetime.now(tz=UTC) + timedelta(days=1),
            )
            for contract in contracts
        ),
    )
    retained, _meta = ibkr_gex._retained_gex_request_universe(
        rolled_state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME",
        underlying_audit=underlying_audit,
        request=rolled_request,
        spot=7570.0,
    )
    assert retained == []

    retained, _meta = ibkr_gex._retained_gex_request_universe(
        state,
        instrument_id=qualified_instrument_id(_ES),
        route_fingerprint=route_fingerprint(_ES),
        provider_symbol="ES",
        underlying_exchange="CME2",
        underlying_audit=underlying_audit,
        request=request,
        spot=7570.0,
    )
    assert retained == []


def test_gex_request_universe_requalifies_once_after_process_cache_reset(
    monkeypatch,
) -> None:
    build_calls = 0

    class FakeIb:
        def reqMarketDataType(self, _data_type: int) -> None:
            return None

    request = replace(
        gex.gex_request_config(instrument=_ES),
        strike_count=9,
    )
    strikes = [
        7550.0,
        7555.0,
        7560.0,
        7565.0,
        7570.0,
        7575.0,
        7580.0,
        7585.0,
        7590.0,
    ]
    contracts = [
        _ibkr_option(
            con_id=index * 2 + (1 if right == "C" else 2),
            strike=strike,
            right=right,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
        )
        for index, strike in enumerate(strikes)
        for right in ("C", "P")
    ]
    underlying = SimpleNamespace(
        conId=101,
        secType="FUT",
        symbol="ES",
        localSymbol="ESU6",
        lastTradeDateOrContractMonth="20260918",
        currency="USD",
        exchange="CME",
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: underlying,
    )

    def build(*_args, **_kwargs):
        nonlocal build_calls
        build_calls += 1
        return contracts, {
            "qualification_source": "provider_contract_details",
            "contract_detail_requests": 1,
        }

    monkeypatch.setattr(ibkr_gex, "_build_option_contracts", build)
    monkeypatch.setattr(
        ibkr_gex,
        "_collect_option_rows_by_expiration",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(ibkr_gex, "_reset_gex_session", lambda: None)

    first = ibkr_gex.fetch_gex_option_rows(
        instrument=_ES,
        request=_ibkr_provider_request(request),
        spot=7570.0,
        dividend_yield=0.0,
    )
    second = ibkr_gex.fetch_gex_option_rows(
        instrument=_ES,
        request=_ibkr_provider_request(request),
        spot=7570.0,
        dividend_yield=0.0,
    )
    assert build_calls == 1
    assert first.selected_contract_universe == second.selected_contract_universe
    assert second.chain_meta["qualification_source"] == ("retained_provider_selected_universe")

    _IBKR_RUNTIME.gex_request_universe_cache.clear()
    ibkr_gex.fetch_gex_option_rows(
        instrument=_ES,
        request=_ibkr_provider_request(request),
        spot=7570.0,
        dividend_yield=0.0,
    )
    assert build_calls == 2
    assert '"provider_selected_contract_universe"' not in inspect.getsource(
        gex_context_module._collect_live_gex_context_owned
    )


def test_ibkr_gex_request_revalidates_expiry_after_transport_cleanup(
    monkeypatch,
) -> None:
    before_expiry = datetime(2026, 7, 28, 19, 59, 59, tzinfo=UTC)
    exact_expiry = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)

    class BoundaryDateTime(datetime):
        current = before_expiry

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    contract = _ibkr_option(
        con_id=1,
        strike=600.0,
        right="C",
        expiry="20260728",
        expiry_at=exact_expiry,
    )
    monkeypatch.setattr(ibkr_gex, "datetime", BoundaryDateTime)
    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda *_args, **_kwargs: SimpleNamespace(
            reqMarketDataType=lambda _value: None,
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda *_args, **_kwargs: SimpleNamespace(
            conId=756733,
            symbol="SPY",
            secType="STK",
            localSymbol="SPY",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (
            [contract],
            {"qualified_contracts": 1},
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_collect_option_rows_by_expiration",
        lambda *_args, **_kwargs: [
            {
                "strike": 600.0,
                "right": "C",
                "gamma": 0.01,
                "_ibkr_market_data_type": 1,
            }
        ],
    )

    def cross_boundary() -> None:
        BoundaryDateTime.current = exact_expiry

    monkeypatch.setattr(ibkr_gex, "_reset_gex_session", cross_boundary)

    with pytest.raises(ibkr_gex.IbkrOptionUniverseUnavailableError) as caught:
        ibkr_gex.fetch_gex_option_rows(
            instrument=_SPY,
            request=_ibkr_provider_request(gex.gex_request_config(instrument=_SPY)),
            spot=600.0,
            dividend_yield=0.0,
        )

    assert caught.value.reason == "OPTION_UNIVERSE_ROLLOVER"


def test_gex_connection_consumes_the_single_provider_request_deadline(
    monkeypatch,
) -> None:
    clock = {"now": 100.0}
    observed: dict[str, Any] = {"connect_timeout": None}
    events: list[str] = []

    def stop_at_connect(
        _request: gex.GexRequestConfig,
        *,
        connection_timeout: float,
    ) -> None:
        observed["connect_timeout"] = connection_timeout
        raise RuntimeError("stop after budget observation")

    monkeypatch.setattr(ibkr_gex, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(ibkr_gex, "_time_monotonic", lambda: clock["now"])
    monkeypatch.setattr(ibkr_gex, "_connected_gex_ib", stop_at_connect)
    monkeypatch.setattr(
        ibkr_gex,
        "_reset_gex_session",
        lambda: events.append("reset"),
    )
    request = _gex_test_request(
        _ES,
        timeout=5.0,
    )

    with pytest.raises(RuntimeError, match="budget observation"):
        ibkr_gex.fetch_gex_option_rows(
            instrument=_ES,
            request=_ibkr_provider_request(request),
            spot=7500.0,
            dividend_yield=0.0,
        )

    assert observed["connect_timeout"] == pytest.approx(5.0)
    assert events == ["reset"]


def test_gex_request_config_uses_code_owned_canonical_window() -> None:
    request = gex.gex_request_config(instrument=_ES)

    assert request.strike_count == 17
    assert request.max_expirations == 2
    assert request.max_contracts == 68
    assert gex.GEX_CONTEXT_MAX_LEVELS == 21
    assert gex.GEX_CHART_HISTORY_HOURS == 48
    assert gex.GEX_LIVE_FRAME_SECONDS == 5.0
    assert gex.GEX_ANALYSIS_PAIR_MAX_SNAPSHOTS == 2
    assert gex.GEX_CONTEXT_STALE_MINUTES == 60

    assert gex.gex_request_config(instrument=_ES).max_contracts == 68


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (
            datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 15, 4, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 1, 15, 14, 0, tzinfo=UTC),
            datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
        ),
    ),
)
def test_gex_analysis_day_starts_at_new_york_midnight(
    value: datetime,
    expected: datetime,
) -> None:
    assert gex.gex_analysis_day_start(value) == expected


@pytest.mark.parametrize(
    ("requested_market_data_type", "actual_types", "expected"),
    (
        (3, [1, 1], "live"),
        (3, [3, 3], "delayed"),
        (3, [0, 0], "unknown"),
        (3, [1, 3], "unknown"),
    ),
)
def test_gex_actual_entitlement_comes_from_uniform_tws_callbacks_not_request(
    requested_market_data_type: int,
    actual_types: list[int],
    expected: str,
) -> None:
    assert requested_market_data_type == 3
    assert ibkr_gex._ibkr_market_data_entitlement(actual_types) == expected


def test_ibkr_market_subscription_clears_optimistic_ib_async_entitlement() -> None:
    ticker = SimpleNamespace(marketDataType=1)
    observed: dict[str, Any] = {}

    class FakeIb:
        def reqMktData(self, contract, generic_ticks, snapshot, regulatory_snapshot):
            observed.update(
                {
                    "contract": contract,
                    "generic_ticks": generic_ticks,
                    "snapshot": snapshot,
                    "regulatory_snapshot": regulatory_snapshot,
                }
            )
            return ticker

    contract = _ibkr_option(con_id=1, strike=7500.0, right="C")

    assert (
        request_ibkr_market_data_ticker(
            FakeIb(),
            contract.raw,
            "101",
            False,
            False,
        )
        is ticker
    )
    assert ticker.marketDataType == 0
    assert observed["generic_ticks"] == "101"
    assert observed["snapshot"] is False
    assert observed["regulatory_snapshot"] is False


def test_gex_request_config_preserves_exact_expiry_mode_and_rejects_coercion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AEF_IBKR_GEX_EXPIRY_MODE", "0dte")
    request = gex.gex_request_config(instrument=_ES)

    assert request.expiry_mode == "0dte"
    with pytest.raises(ValueError, match="port"):
        replace(request.provider_request, port="4002")


def test_ibkr_gex_chain_request_rejects_normalized_underlying_sec_type() -> None:
    underlying = SimpleNamespace(
        conId=123,
        symbol="ES",
        secType="fut",
        exchange="CME",
    )

    with pytest.raises(RuntimeError, match="invalid exact conId, symbol, or secType"):
        ibkr_gex._request_option_chains(SimpleNamespace(), underlying)


def test_gex_manual_window_keeps_ten_strikes_on_each_side_of_atm() -> None:
    chain = SimpleNamespace(strikes=list(range(90, 111)))

    strikes = ibkr_option_acquisition._selected_strikes(chain, 100.0, strike_count=21)

    assert strikes == [float(value) for value in range(90, 111)]
    assert len([strike for strike in strikes if strike < 100.0]) == 10
    assert len([strike for strike in strikes if strike > 100.0]) == 10


def test_gex_strike_selection_uses_exact_distance_on_ragged_ladders() -> None:
    chain = SimpleNamespace(strikes=[1, 99, 100, 101, 102])

    assert ibkr_option_acquisition._selected_strikes(
        chain,
        100.0,
        strike_count=4,
    ) == [99.0, 100.0, 101.0, 102.0]
    assert ibkr_option_acquisition._selected_strikes(
        SimpleNamespace(strikes=[99, 100, 101]),
        100.0,
        strike_count=2,
    ) == [99.0, 100.0]


def test_gex_live_config_stays_within_persistent_subscription_budget() -> None:
    instrument = _ES
    request = _live_request(instrument=instrument)
    snapshot_request = gex.gex_request_config(instrument=instrument)

    assert request.strike_count == 17
    assert request.max_expirations == 2
    assert request.max_contracts == 68
    assert request.refresh_mode == "live"
    assert request.scheduler_lane == "live"
    for field_name in ("max_expirations", "expiry_mode", "risk_free_rate", "dividend_yield"):
        assert getattr(request, field_name) == getattr(snapshot_request, field_name)
    for field_name in ("host", "port", "readonly", "market_data_type"):
        assert getattr(request.provider_request, field_name) == getattr(
            snapshot_request.provider_request,
            field_name,
        )


def test_gex_level_kind_uses_dominant_side_when_strike_is_both_walls() -> None:
    levels = {"call_wall": 7575.0, "put_wall": 7575.0}

    put_dominant = gex._level_kind(
        {
            "strike": 7575.0,
            "call_gex": 271_000_000,
            "put_gex": -428_000_000,
            "net_gex": -157_000_000,
        },
        levels,
    )
    call_dominant = gex._level_kind(
        {
            "strike": 7575.0,
            "call_gex": 430_000_000,
            "put_gex": -270_000_000,
            "net_gex": 160_000_000,
        },
        levels,
    )

    assert put_dominant == "PUT_WALL"
    assert call_dominant == "CALL_WALL"


def test_gex_level_kind_keeps_zero_net_node_neutral() -> None:
    row = {
        "strike": 7550.0,
        "call_gex": 250_000_000.0,
        "put_gex": -250_000_000.0,
        "net_gex": 0.0,
        "abs_gex": 500_000_000.0,
    }

    kind = gex._level_kind(row, {"call_wall": None, "put_wall": None})
    selected = gex._select_gex_levels(
        [row],
        7550.0,
        {"call_wall": None, "put_wall": None},
        max_levels=1,
    )

    assert kind == "GEX_NODE"
    assert selected[0]["kind"] == "GEX_NODE"
    assert selected[0]["kind_class"] == "neutral_node"


def test_gex_live_session_rebases_at_exact_expiry_or_strike_edge() -> None:
    request = _live_request(instrument=ibkr_future_payload("ES"))
    expiry_at = datetime(2026, 7, 29, 20, 0, tzinfo=UTC)
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES",),
        request=request,
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[
            _ibkr_option(
                con_id=index,
                expiry="20260729",
                expiry_at=expiry_at,
                strike=strike,
                right="C",
            )
            for index, strike in enumerate(
                [7550, 7555, 7560, 7565, 7570, 7575, 7580, 7585, 7590], start=1
            )
        ],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7572.0,
    )

    before_expiry = expiry_at - timedelta(seconds=1)
    assert (
        gex_live._live_session_rebase_reason(
            session,
            7572.0,
            now=before_expiry,
        )
        == ""
    )
    assert "near live strike edge" in gex_live._live_session_rebase_reason(
        session,
        7580.0,
        now=before_expiry,
    )
    assert "near live strike edge" in gex_live._live_session_rebase_reason(
        session,
        7560.0,
        now=before_expiry,
    )
    assert "exact option expiry reached" in gex_live._live_session_rebase_reason(
        session,
        7572.0,
        now=expiry_at,
    )


def test_gex_live_subscription_cancellation_closes_provider_transport(monkeypatch) -> None:
    class FakeIb:
        def __init__(self) -> None:
            self.connected = True

        def isConnected(self) -> bool:
            return self.connected

        def disconnect(self) -> None:
            self.connected = False

        def reqMarketDataType(self, _market_data_type: int) -> None:
            return None

    ib = FakeIb()

    async def connected(_request):
        return ib

    async def cancelled_underlying(_ib, *, instrument):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ibkr_gex, "_connected_live_gex_ib_async", connected)
    monkeypatch.setattr(ibkr_gex, "_qualified_live_underlying", cancelled_underlying)

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await ibkr_gex.open_live_gex_subscription(
                provider_symbol="SPY",
                instrument=_SPY,
                request=_ibkr_provider_request(_live_request(instrument=_SPY)),
                spot=743.0,
            )

    asyncio.run(run())

    assert ib.connected is False


def test_gex_live_stop_keeps_other_asset_session_active(monkeypatch) -> None:
    cancelled: list[str] = []

    class FakeIb:
        def isConnected(self):
            return True

    monkeypatch.setattr(
        ibkr_gex,
        "_cancel_ibkr_market_data",
        lambda _ib, _ticker, contract: (
            cancelled.append(str(getattr(contract, "localSymbol", ""))) or True
        ),
    )
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES",),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=FakeIb(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[
            (
                _ibkr_option(con_id=1, strike=7570, right="C", local_symbol="ES C1"),
                SimpleNamespace(),
            ),
            (
                _ibkr_option(con_id=2, strike=7570, right="P", local_symbol="ES P1"),
                SimpleNamespace(),
            ),
        ],
        chain_meta={},
        underlying_audit={},
        spot=7570.0,
    )
    gex_live._LIVE_GEX_SESSIONS = {_identity_key(_ES): session}

    async def run() -> None:
        result = await gex_live._stop_live_gex_context_owned(_identity_key(_SPY))
        assert result["ok"] is True
        assert result["active"] is False
        assert result["cancelled"] == 0

    try:
        asyncio.run(run())
        assert cancelled == []
        assert gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] is session
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()


def test_gex_live_status_snapshot_is_immutable_and_stop_uses_provider_symbols() -> None:
    class FakeIb:
        connected = False

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "status"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=FakeIb(),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7570.0,
    )
    gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] = session
    gex_live._publish_live_gex_status_owned()

    first_status = gex_live.live_gex_status()
    first_status["sessions"][0]["provider_symbol"] = "MUTATED"
    assert gex_live.live_gex_status()["sessions"][0]["provider_symbol"] == "ES"

    result = asyncio.run(gex_live._stop_live_gex_context_owned(_identity_key(_ES)))

    assert result["provider_symbols"] == ["ES"]
    assert "assets" not in result
    assert gex_live.live_gex_status()["sessions"] == []


def test_gex_live_raw_frame_snapshot_is_isolated_and_cleared() -> None:
    identity_key = _identity_key(_ES)
    payload = {
        "ok": True,
        "frame_complete": True,
        "instrument_id": identity_key[0],
        "route_fingerprint": identity_key[1],
        "raw": {"contracts": [{"con_id": 101, "volume": 12.0}]},
    }
    gex_live._publish_live_gex_frame_snapshot(identity_key, payload)

    first = gex_live.live_gex_frame_snapshot(*identity_key)
    assert first is not None
    first["raw"]["contracts"][0]["volume"] = 999.0
    second = gex_live.live_gex_frame_snapshot(*identity_key)

    assert second is not None
    assert second["raw"]["contracts"][0]["volume"] == 12.0
    gex_live._clear_live_gex_frame_snapshot(identity_key)
    assert gex_live.live_gex_frame_snapshot(*identity_key) is None


def test_gex_live_session_rebuilds_when_subscriptions_diverge_from_active_contracts(
    monkeypatch,
) -> None:
    cancelled: list[str] = []
    request = _live_request(instrument=ibkr_future_payload("ES"))
    key = ("ES", "route")

    class FakeIb:
        def isConnected(self):
            return True

        def reqMarketDataType(self, _data_type):
            return None

        def reqMktData(self, contract, *_args):
            return SimpleNamespace(localSymbol=getattr(contract, "localSymbol", ""))

    old_session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=key,
        request=request,
        ib=FakeIb(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[_ibkr_option(con_id=1, strike=7570, right="C", local_symbol="ES C1")],
        tickers=[
            (
                _ibkr_option(con_id=1, strike=7570, right="C", local_symbol="ES C1"),
                SimpleNamespace(),
            ),
            (
                _ibkr_option(con_id=2, strike=7570, right="P", local_symbol="ES P1"),
                SimpleNamespace(),
            ),
        ],
        chain_meta={},
        underlying_audit={},
        spot=7570.0,
    )
    old_session.last_publishable_payload = {
        "ok": True,
        "status": "ok",
        "capture_mode": "live",
    }
    gex_live._LIVE_GEX_SESSIONS = {_identity_key(_ES): old_session}

    async def fake_connected(*_args, **_kwargs):
        return FakeIb()

    async def fake_underlying(*_args, **_kwargs):
        return SimpleNamespace(
            conId=101,
            symbol="ES",
            secType="FUT",
            exchange="CME",
            currency="USD",
            localSymbol="ESU6",
            lastTradeDateOrContractMonth="20260918",
        )

    async def fake_contracts(*_args, **_kwargs):
        return [
            _ibkr_option(
                con_id=3,
                strike=7575,
                right="C",
                local_symbol="ES C2",
                expiry=_ACTIVE_OPTION_EXPIRY,
                expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            )
        ], {}

    monkeypatch.setattr(
        gex_live,
        "_cancel_live_gex_session_owned",
        lambda session: (
            cancelled.append(session.provider_symbol)
            or ibkr_gex.live_gex_subscription_health(
                session.provider_subscription
            ).subscription_count
        ),
    )
    monkeypatch.setattr(ibkr_gex, "_connected_live_gex_ib_async", fake_connected)
    monkeypatch.setattr(ibkr_gex, "_qualified_live_underlying", fake_underlying)
    monkeypatch.setattr(ibkr_gex, "_build_live_option_contracts", fake_contracts)

    async def run() -> None:
        rebuilt = await gex_live._ensure_live_gex_session_owned(
            "ES",
            request=request,
            key=key,
            instrument=ibkr_future_payload("ES"),
            underlying_spot=7570.0,
            provider_runtime=ibkr_gex_provider_runtime,
        )
        assert rebuilt is not old_session
        health = ibkr_gex.live_gex_subscription_health(rebuilt.provider_subscription)
        assert health.contract_count == 1
        assert health.subscription_count == 1
        assert rebuilt.generation != old_session.generation
        assert rebuilt.recovery_count == 1
        assert rebuilt.recovery_reason == "subscription_count_mismatch"
        assert rebuilt.chain_meta["live_recovery_reason"] == "subscription_count_mismatch"
        assert rebuilt.last_publishable_payload == old_session.last_publishable_payload

    try:
        asyncio.run(run())
        assert cancelled == ["ES"]
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()


def test_gex_live_bootstrap_cancels_open_tickers_when_subscription_fails(monkeypatch) -> None:
    cancelled: list[str] = []

    class FakeIb:
        def __init__(self):
            self.requests = 0
            self.connected = True

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

        def reqMarketDataType(self, _data_type):
            return None

        def reqMktData(self, contract, *_args):
            self.requests += 1
            if getattr(contract, "localSymbol", "") == "ES P1":
                raise RuntimeError("subscription failed")
            return SimpleNamespace(localSymbol=getattr(contract, "localSymbol", ""))

    fake_ib = FakeIb()

    async def fake_connected(*_args, **_kwargs):
        return fake_ib

    async def fake_underlying(_ib, **_kwargs):
        return SimpleNamespace(localSymbol="ESU6")

    async def fake_contracts(*_args, **_kwargs):
        return (
            [
                _ibkr_option(con_id=1, strike=7570, right="C", local_symbol="ES C1"),
                _ibkr_option(con_id=2, strike=7570, right="P", local_symbol="ES P1"),
            ],
            {},
        )

    monkeypatch.setattr(ibkr_gex, "_connected_live_gex_ib_async", fake_connected)
    monkeypatch.setattr(ibkr_gex, "_qualified_live_underlying", fake_underlying)
    monkeypatch.setattr(ibkr_gex, "_build_live_option_contracts", fake_contracts)
    monkeypatch.setattr(
        ibkr_gex,
        "_cancel_ibkr_market_data",
        lambda _ib, _ticker, contract: (
            cancelled.append(str(getattr(contract, "localSymbol", ""))) or True
        ),
    )
    gex_live._LIVE_GEX_SESSIONS.clear()

    async def run() -> None:
        request = _live_request(instrument=ibkr_future_payload("ES"))
        try:
            await gex_live._ensure_live_gex_session_owned(
                "ES",
                request=request,
                key=("ES", "test"),
                instrument=ibkr_future_payload("ES"),
                underlying_spot=7570.0,
                provider_runtime=ibkr_gex_provider_runtime,
            )
        except RuntimeError as exc:
            assert "subscription failed" in str(exc)
        else:
            raise AssertionError("bootstrap failure should be raised")

    try:
        asyncio.run(run())
        assert cancelled == ["ES C1"]
        assert fake_ib.connected is False
        assert _identity_key(_ES) not in gex_live._LIVE_GEX_SESSIONS
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()


def test_ibkr_live_gex_public_subscription_exposes_only_typed_sample_and_health(
    monkeypatch,
) -> None:
    contract = _ibkr_option(
        con_id=1,
        expiry=_ACTIVE_OPTION_EXPIRY,
        expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
        strike=7575.0,
        right="C",
    )
    observed_at = datetime.now(tz=UTC)
    ticker = SimpleNamespace(
        marketDataType=1,
        modelGreeks=SimpleNamespace(gamma=0.01),
        ticks=[SimpleNamespace(tickType=27, time=observed_at)],
        time=observed_at,
    )
    ib = SimpleNamespace()
    closed: list[int] = []
    observations: list[dict[str, Any]] = []
    subscription = ibkr_gex.IbkrLiveGexSubscription(
        subscribed_strikes=(7575.0,),
        contracts_requested=1,
        chain_meta={"qualified_contracts": 1},
        underlying_audit={"underlying_con_id": 101},
        _ib=ib,
        _contracts=(contract,),
        _subscriptions=[
            (
                contract,
                ticker,
                ibkr_gex_market.GexTickerObservationTracker(
                    right=OptionRight.CALL,
                ),
            )
        ],
    )
    monkeypatch.setattr(ibkr_gex, "_gex_transport_connected", lambda value: value is ib)
    monkeypatch.setattr(
        ibkr_gex,
        "_option_row_from_ticker",
        lambda *_args, **kwargs: (
            observations.append(dict(kwargs["observation"]))
            or {"strike": 7575.0, "right": "C", "gamma": 0.01}
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_close_live_gex_transport",
        lambda raw_ib, rows: closed.append(len(rows)) or (1 if raw_ib is ib else 0),
    )

    health = ibkr_gex.live_gex_subscription_health(subscription)
    sample = ibkr_gex.sample_live_gex_subscription(
        subscription,
        spot=7575.0,
        risk_free_rate=0.05,
        dividend_yield=0.0,
    )

    assert health == ibkr_gex.IbkrLiveGexHealth(
        connected=True,
        contract_count=1,
        subscription_count=1,
        active_contract_count=1,
        universe_expires_at=_ACTIVE_OPTION_EXPIRY_AT,
    )
    assert sample.contract_rows == ({"strike": 7575.0, "right": "C", "gamma": 0.01},)
    assert observations == [
        {
            "market_data_type": 1,
            "market_data_entitlement": "live",
            "model_greeks_received_at": observed_at.isoformat(),
            "open_interest_received_at": observed_at.isoformat(),
            "observation_time_basis": "client_receive",
            "open_interest_as_of": "previous_settlement",
        }
    ]
    assert not hasattr(sample, "ib")
    assert not hasattr(sample, "tickers")
    assert ibkr_gex.close_live_gex_subscription(subscription) == 1
    assert ibkr_gex.close_live_gex_subscription(subscription) == 0
    assert closed == [1]


def test_gex_frame_quality_rejects_partial_bootstrap_rows() -> None:
    publishable, reason, meta = gex._gex_frame_quality(
        {
            "diag_contracts_requested": 32,
            "diag_strikes_requested": 8,
            "diag_option_rows": 32,
            "diag_strike_rows": 3,
            "diag_gamma_rows": 3,
            "diag_usable_gamma_rows": 3,
            "diag_open_interest_rows": 5,
            "diag_open_interest_unavailable_rows": 27,
            "diag_usable_gex_rows": 3,
            "diag_nonzero_gex_rows": 3,
            "diag_expected_pair_count": 16,
            "diag_received_pair_count": 0,
            "diag_usable_pair_count": 0,
            "diag_unpaired_series_strikes": 32,
            "diag_collection_timeout": False,
        },
        [
            {"strike": 7575.0, "abs_gex": 719_000_000},
            {"strike": 7580.0, "abs_gex": 510_000_000},
            {"strike": 7585.0, "abs_gex": 0},
        ],
    )

    assert publishable is False
    assert "usable rows 3/32" in reason
    assert meta["diag_frame_publishable"] is False
    assert meta["diag_frame_quality_code"] == "INCOMPLETE_CONTRACT_FRAME"
    assert meta["diag_frame_qualified_universe_complete"] is False
    assert meta["diag_frame_min_usable_rows"] == 24
    assert meta["diag_frame_min_nonzero_strikes"] == 1


def test_gex_frame_quality_accepts_exact_seventeen_pair_universe() -> None:
    publishable, reason, meta = gex._gex_frame_quality(
        {
            "diag_contracts_requested": 34,
            "diag_strikes_requested": 17,
            "diag_option_rows": 34,
            "diag_strike_rows": 17,
            "diag_gamma_rows": 34,
            "diag_usable_gamma_rows": 34,
            "diag_open_interest_rows": 34,
            "diag_open_interest_unavailable_rows": 0,
            "diag_usable_gex_rows": 34,
            "diag_nonzero_gex_rows": 34,
            "diag_expected_pair_count": 17,
            "diag_received_pair_count": 17,
            "diag_usable_pair_count": 17,
            "diag_unpaired_series_strikes": 0,
            "diag_collection_timeout": False,
        },
        [{"strike": 7560.0 + index * 5.0, "abs_gex": 100} for index in range(17)],
    )

    assert publishable is True
    assert reason == "complete exact GEX frame"
    assert meta["diag_frame_publishable"] is True
    assert meta["diag_frame_quality_code"] == "COMPLETE"
    assert meta["diag_frame_requested_strike_limit"] == 17
    assert meta["diag_frame_qualified_strikes"] == 17
    assert meta["diag_frame_missing_requested_strikes"] == 0
    assert meta["diag_frame_selected_strike_scope_complete"] is True
    assert meta["diag_frame_qualified_universe_complete"] is True
    assert meta["diag_frame_exact_pair_universe"] is True
    assert meta["diag_frame_pair_coverage_ratio"] == 1.0
    assert meta["diag_frame_pair_coverage_threshold"] == 0.75


@pytest.mark.parametrize(
    ("usable_pairs", "timed_out", "publishable", "quality_code"),
    (
        (12, False, True, "SUFFICIENT_PAIR_COVERAGE"),
        (12, True, True, "SUFFICIENT_PAIR_COVERAGE_AFTER_TIMEOUT"),
        (11, False, False, "INCOMPLETE_CONTRACT_FRAME"),
        (11, True, False, "COLLECTION_TIMEOUT"),
    ),
)
def test_gex_frame_quality_requires_seventy_five_percent_exact_pairs(
    usable_pairs: int,
    timed_out: bool,
    publishable: bool,
    quality_code: str,
) -> None:
    usable_rows = usable_pairs * 2
    accepted, _reason, meta = gex._gex_frame_quality(
        {
            "diag_contracts_requested": 32,
            "diag_strikes_requested": 16,
            "diag_option_rows": usable_rows,
            "diag_strike_rows": usable_pairs,
            "diag_gamma_rows": usable_rows,
            "diag_usable_gamma_rows": usable_rows,
            "diag_open_interest_rows": usable_rows,
            "diag_open_interest_unavailable_rows": 32 - usable_rows,
            "diag_usable_gex_rows": usable_rows,
            "diag_nonzero_gex_rows": usable_rows,
            "diag_expected_pair_count": 16,
            "diag_received_pair_count": usable_pairs,
            "diag_usable_pair_count": usable_pairs,
            "diag_unpaired_series_strikes": 0,
            "diag_collection_timeout": timed_out,
        },
        [{"strike": 7500.0 + index * 5.0, "abs_gex": 100.0} for index in range(usable_pairs)],
    )

    assert accepted is publishable
    assert meta["diag_frame_quality_code"] == quality_code
    assert meta["diag_frame_min_usable_pairs"] == 12
    assert meta["diag_frame_min_usable_rows"] == 24
    assert meta["diag_frame_pair_coverage_ratio"] == usable_pairs / 16


def test_gex_frame_quality_rounds_pair_threshold_up() -> None:
    accepted, _reason, meta = gex._gex_frame_quality(
        {
            "diag_contracts_requested": 4,
            "diag_strikes_requested": 2,
            "diag_option_rows": 2,
            "diag_strike_rows": 1,
            "diag_gamma_rows": 2,
            "diag_usable_gamma_rows": 2,
            "diag_open_interest_rows": 2,
            "diag_open_interest_unavailable_rows": 2,
            "diag_usable_gex_rows": 2,
            "diag_nonzero_gex_rows": 2,
            "diag_expected_pair_count": 2,
            "diag_received_pair_count": 1,
            "diag_usable_pair_count": 1,
            "diag_unpaired_series_strikes": 0,
            "diag_collection_timeout": False,
        },
        [{"strike": 7500.0, "abs_gex": 100.0}],
    )

    assert accepted is False
    assert meta["diag_frame_min_usable_pairs"] == 2


def test_gex_payload_analyzes_only_complete_pairs_at_seventy_five_percent() -> None:
    captured_at = datetime.now(tz=UTC)
    earlier_universe_expiry_at = captured_at + timedelta(hours=1)
    later_analysis_expiry_at = captured_at + timedelta(days=1)
    raw_contracts = _with_gex_test_contract_observations(
        _activity_contract_rows(
            [
                {
                    "strike": 7500.0 + index * 5.0,
                    "call_gex": 1_000_000.0 + index,
                    "put_gex": -800_000.0 - index,
                }
                for index in range(4)
            ],
            spot=7510.0,
        ),
        observed_at=captured_at,
    )
    raw_contracts = [
        {
            **row,
            "expiry": (
                earlier_universe_expiry_at.strftime("%Y%m%d")
                if index >= 6
                else later_analysis_expiry_at.strftime("%Y%m%d")
            ),
            "trading_class": "ES-EARLY" if index >= 6 else "ES-LATER",
            "expiry_at": (
                earlier_universe_expiry_at if index >= 6 else later_analysis_expiry_at
            ).isoformat(),
            "expiry_time_source": "ibkr_contract_details",
        }
        for index, row in enumerate(raw_contracts)
    ]
    raw_contracts[-1] = {
        **raw_contracts[-1],
        "gamma": None,
        "gex": None,
        "abs_gex": None,
    }
    analysis_contracts = gex._qualified_gex_contract_rows(raw_contracts)
    strikes = gex._aggregate_by_strike(analysis_contracts, 7510.0)
    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 7510.0,
            "spot_observation": _gex_test_spot_observation(
                7510.0,
                observed_at=captured_at,
            ),
            "strike_count": 4,
            "requested_strike_limit": 4,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": 4,
            "option_universe_expires_at": (earlier_universe_expiry_at.isoformat()),
        },
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            raw_contracts,
            strikes,
            contracts_requested=8,
            strikes_requested=4,
            generic_ticks="101",
            analysis_contract_rows=analysis_contracts,
            authoritative_contract_rows=analysis_contracts,
        ),
        max_levels=4,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=analysis_contracts,
        comparison_contract_rows=raw_contracts,
    )

    assert len(analysis_contracts) == 6
    assert {row["strike"] for row in analysis_contracts} == {
        7500.0,
        7505.0,
        7510.0,
    }
    assert payload["frame_complete"] is True
    assert payload["decision_authoritative"] is True
    assert payload["diagnostics"]["diag_frame_quality_code"] == ("SUFFICIENT_PAIR_COVERAGE")
    assert payload["diagnostics"]["diag_frame_pair_coverage_ratio"] == 0.75
    assert len(payload["raw"]["contracts"]) == 6
    assert payload["comparison_scope"]["strike_count"] == 4
    assert payload["comparison_scope"]["contract_con_ids"] == sorted(
        row["con_id"] for row in raw_contracts
    )
    assert payload["diagnostics"]["diag_comparison_scope_source"] == (
        "provider_selected_contract_universe"
    )
    assert payload["diagnostics"]["diag_comparison_scope_contracts"] == 8
    assert payload["option_universe_expires_at"] == (earlier_universe_expiry_at.isoformat())
    assert gex_comparison_scope_from_payload(payload) == payload["comparison_scope"]
    payload["option_activity"] = None
    payload["option_activity_status"] = "NEED_PREVIOUS"
    assert (
        gex.require_gex_snapshot_payload(
            deepcopy(payload),
            projection="persisted",
        )["comparison_scope"]
        == payload["comparison_scope"]
    )
    durable_without_transient_provider_bound = deepcopy(payload)
    durable_without_transient_provider_bound.pop(
        "option_universe_expires_at",
    )
    durable_without_transient_provider_bound["raw"]["meta"].pop(
        "option_universe_expires_at",
    )
    durable_projection = gex.require_gex_snapshot_payload(
        durable_without_transient_provider_bound,
        projection="persisted",
    )
    assert durable_projection["option_universe_expires_at"] is None
    assert durable_projection["decision_authoritative"] is False
    persistable = deepcopy(payload)
    persistable["instrument_id"] = _instrument_id(_ES)
    persistable["route_fingerprint"] = _route_key(_ES)
    store = FakeGexStore([])
    assert gex.persist_gex_snapshot(
        "ES",
        persistable["raw"]["meta"],
        persistable["raw"]["strikes"],
        persistable,
        analysis_contracts,
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        store=store,
        captured_at=captured_at,
        capture_mode="request",
    )
    persisted = store.snapshots[0]["payload"]
    assert persisted["comparison_scope"] == payload["comparison_scope"]
    assert persisted["comparison_scope"]["contract_con_ids"] == sorted(
        row["con_id"] for row in raw_contracts
    )
    assert sorted(row["con_id"] for row in persisted["raw"]["contracts"]) == sorted(
        row["con_id"] for row in analysis_contracts
    )


@pytest.mark.parametrize(
    "defect",
    ("local_iv", "mixed_entitlement", "missing_oi_observation"),
)
def test_gex_payload_uses_authoritative_pair_subset_at_seventy_five_percent(
    defect: str,
) -> None:
    captured_at = datetime.now(tz=UTC)
    raw_contracts = _with_gex_test_contract_observations(
        _activity_contract_rows(
            [
                {
                    "strike": 7500.0 + index * 5.0,
                    "call_gex": 1_000_000.0 + index,
                    "put_gex": -800_000.0 - index,
                }
                for index in range(4)
            ],
            spot=7510.0,
        ),
        observed_at=captured_at,
    )
    for index in (-2, -1):
        row = dict(raw_contracts[index])
        if defect == "local_iv":
            row["greek_source"] = "local_iv"
        elif defect == "mixed_entitlement":
            row["market_data_type"] = 3
            row["market_data_entitlement"] = "delayed"
        else:
            row["open_interest_received_at"] = None
        raw_contracts[index] = row

    numeric_contracts = gex._qualified_gex_contract_rows(raw_contracts)
    authoritative_contracts = gex._qualified_gex_contract_rows(
        raw_contracts,
        authority_at=captured_at,
    )
    analysis_contracts = gex._qualified_gex_contract_rows(
        raw_contracts,
        authority_at=captured_at,
        expected_pair_count=4,
    )
    strikes = gex._aggregate_by_strike(analysis_contracts, 7510.0)
    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 7510.0,
            "spot_observation": _gex_test_spot_observation(
                7510.0,
                observed_at=captured_at,
            ),
            "strike_count": 4,
            "requested_strike_limit": 4,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": 4,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in raw_contracts),
        },
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            raw_contracts,
            strikes,
            contracts_requested=8,
            strikes_requested=4,
            generic_ticks="101",
            analysis_contract_rows=analysis_contracts,
            authoritative_contract_rows=authoritative_contracts,
        ),
        max_levels=4,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=analysis_contracts,
        comparison_contract_rows=raw_contracts,
    )

    assert len(numeric_contracts) == 8
    assert len(authoritative_contracts) == 6
    assert analysis_contracts == authoritative_contracts
    assert payload["ok"] is True
    assert payload["frame_complete"] is True
    assert payload["decision_authoritative"] is True
    assert payload["status"] == "degraded"
    assert payload["message"].startswith("GEX DEGRADED COVERAGE:")
    assert len(payload["raw"]["contracts"]) == 6
    assert payload["diagnostics"]["diag_option_rows"] == 8
    assert payload["diagnostics"]["diag_received_pair_count"] == 4
    assert payload["diagnostics"]["diag_numeric_usable_pair_count"] == 4
    assert payload["diagnostics"]["diag_authoritative_pair_count"] == 3
    assert payload["diagnostics"]["diag_usable_pair_count"] == 3


def test_gex_payload_keeps_numeric_display_rows_below_authority_threshold() -> None:
    captured_at = datetime.now(tz=UTC)
    raw_contracts = _with_gex_test_contract_observations(
        _activity_contract_rows(
            [
                {
                    "strike": 7500.0 + index * 5.0,
                    "call_gex": 1_000_000.0 + index,
                    "put_gex": -800_000.0 - index,
                }
                for index in range(4)
            ],
            spot=7510.0,
        ),
        observed_at=captured_at,
    )
    for index in range(4):
        raw_contracts[index] = {
            **raw_contracts[index],
            "greek_source": "local_iv",
        }
    authoritative_contracts = gex._qualified_gex_contract_rows(
        raw_contracts,
        authority_at=captured_at,
    )
    analysis_contracts = gex._qualified_gex_contract_rows(
        raw_contracts,
        authority_at=captured_at,
        expected_pair_count=4,
    )
    strikes = gex._aggregate_by_strike(analysis_contracts, 7510.0)
    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 7510.0,
            "spot_observation": _gex_test_spot_observation(
                7510.0,
                observed_at=captured_at,
            ),
            "strike_count": 4,
            "requested_strike_limit": 4,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": 4,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in raw_contracts),
        },
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            raw_contracts,
            strikes,
            contracts_requested=8,
            strikes_requested=4,
            generic_ticks="101",
            analysis_contract_rows=analysis_contracts,
            authoritative_contract_rows=authoritative_contracts,
        ),
        max_levels=4,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=analysis_contracts,
        comparison_contract_rows=raw_contracts,
    )

    assert len(authoritative_contracts) == 4
    assert len(analysis_contracts) == 8
    assert payload["ok"] is True
    assert payload["frame_complete"] is True
    assert payload["decision_authoritative"] is False
    assert payload["message"].startswith("GEX DISPLAY ONLY:")
    assert len(payload["levels"]) == 4
    assert payload["diagnostics"]["diag_numeric_usable_pair_count"] == 4
    assert payload["diagnostics"]["diag_authoritative_pair_count"] == 2
    assert payload["diagnostics"]["diag_usable_pair_count"] == 4


def test_gex_frame_quality_accepts_complete_available_subset_below_requested_limit() -> None:
    publishable, reason, meta = gex._gex_frame_quality(
        {
            "diag_contracts_requested": 10,
            "diag_strikes_requested": 17,
            "diag_option_rows": 10,
            "diag_strike_rows": 5,
            "diag_gamma_rows": 10,
            "diag_usable_gamma_rows": 10,
            "diag_open_interest_rows": 10,
            "diag_open_interest_unavailable_rows": 0,
            "diag_usable_gex_rows": 10,
            "diag_nonzero_gex_rows": 10,
            "diag_expected_pair_count": 5,
            "diag_received_pair_count": 5,
            "diag_usable_pair_count": 5,
            "diag_unpaired_series_strikes": 0,
            "diag_collection_timeout": False,
        },
        [{"strike": 4990.0 + index * 5.0, "abs_gex": 100} for index in range(5)],
    )

    assert publishable is True
    assert reason.startswith(
        "complete provider-selected universe: 5 available strikes within requested limit 17;"
    )
    assert meta["diag_frame_publishable"] is True
    assert meta["diag_frame_quality_code"] == "COMPLETE"
    assert meta["diag_frame_requested_strike_limit"] == 17
    assert meta["diag_frame_qualified_strikes"] == 5
    assert meta["diag_frame_missing_requested_strikes"] == 12
    assert meta["diag_frame_selected_strike_scope_complete"] is True
    assert meta["diag_frame_qualified_universe_complete"] is True
    assert meta["diag_frame_exact_pair_universe"] is True


def test_gex_payload_requires_complete_paired_requested_strike_scope() -> None:
    spot = 5000.0
    captured_at = datetime.now(tz=UTC)
    contract_rows = [
        {
            "expiry": expiry,
            "trading_class": "ES",
            "exchange": "CME",
            "multiplier": 50.0,
            "strike": 4950.0 + strike_index * 5.0,
            "right": "C",
            "gamma": 0.01,
            "greek_source": "modelGreeks",
            "open_interest": 100.0,
            "volume": 10.0,
            "gex": 12_500_000.0,
            "abs_gex": 12_500_000.0,
            "reference_option_price": None,
            "reference_option_price_source": "unavailable",
            "expiry_at": datetime.strptime(expiry, "%Y%m%d")
            .replace(hour=16, tzinfo=gex.NY_TZ)
            .astimezone(UTC)
            .isoformat(),
            "expiry_time_source": "ibkr_contract_details",
        }
        for expiry in ("20991229", "20991230", "20991231")
        for strike_index in range(21)
    ]
    contract_rows = _with_gex_test_contract_observations(
        contract_rows,
        observed_at=captured_at,
    )
    strikes = gex._aggregate_by_strike(contract_rows, spot)
    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": spot,
            "spot_observation": _gex_test_spot_observation(
                spot,
                observed_at=captured_at,
            ),
            "strike_count": 21,
            "requested_strike_limit": 21,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
        },
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            contract_rows,
            strikes,
            contracts_requested=63,
            strikes_requested=21,
            generic_ticks="101",
        ),
        max_levels=21,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=contract_rows,
        comparison_contract_rows=None,
    )

    assert payload["decision_authoritative"] is False
    assert payload["levels"] == []
    assert payload["diagnostics"]["diag_frame_qualified_universe_complete"] is False
    assert payload["diagnostics"]["diag_received_pair_count"] == 0
    assert payload["diagnostics"]["diag_unpaired_series_strikes"] == 63

    paired_rows = [
        {
            **row,
            "right": right,
            "gamma": 0.01 if right == "C" else 0.006,
            "gex": 12_500_000.0 if right == "C" else -7_500_000.0,
            "abs_gex": 12_500_000.0 if right == "C" else 7_500_000.0,
        }
        for row in contract_rows[:21]
        for right in ("C", "P")
    ]
    for con_id, row in enumerate(paired_rows, start=1):
        row["con_id"] = con_id
    paired_strikes = gex._aggregate_by_strike(paired_rows, spot)
    paired_payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": spot,
            "spot_observation": _gex_test_spot_observation(
                spot,
                observed_at=captured_at,
            ),
            "strike_count": 21,
            "requested_strike_limit": 21,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": 21,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in paired_rows),
        },
        strikes=paired_strikes,
        diagnostics=gex._gex_diagnostics(
            paired_rows,
            paired_strikes,
            contracts_requested=42,
            strikes_requested=21,
            generic_ticks="101",
        ),
        max_levels=21,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=paired_rows,
        comparison_contract_rows=paired_rows,
    )

    assert paired_payload["decision_authoritative"] is True
    assert len(paired_payload["levels"]) == 21
    assert paired_payload["diagnostics"]["diag_frame_exact_pair_universe"] is True

    subscription_scoped_oi_rows = [
        {
            **row,
            "open_interest_received_at": (captured_at - timedelta(hours=6)).isoformat(),
        }
        for row in paired_rows
    ]
    session_oi_payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta=deepcopy(paired_payload["raw"]["meta"]),
        strikes=paired_strikes,
        diagnostics=gex._gex_diagnostics(
            subscription_scoped_oi_rows,
            paired_strikes,
            contracts_requested=42,
            strikes_requested=21,
            generic_ticks="101",
        ),
        max_levels=21,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=subscription_scoped_oi_rows,
        comparison_contract_rows=paired_rows,
    )
    assert session_oi_payload["decision_authoritative"] is True
    assert (
        session_oi_payload["diagnostics"]["diag_execution_open_interest_validity"]
        == "provider_subscription_previous_settlement"
    )

    display_only_spot_meta = deepcopy(paired_payload["raw"]["meta"])
    display_only_spot_meta["spot_observation"] = _gex_test_spot_observation(spot)
    display_only_spot_payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta=display_only_spot_meta,
        strikes=paired_strikes,
        diagnostics=gex._gex_diagnostics(
            paired_rows,
            paired_strikes,
            contracts_requested=42,
            strikes_requested=21,
            generic_ticks="101",
        ),
        max_levels=21,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=paired_rows,
        comparison_contract_rows=paired_rows,
    )
    assert display_only_spot_payload["frame_complete"] is False
    assert display_only_spot_payload["decision_authoritative"] is False
    assert len(display_only_spot_payload["levels"]) == 21
    assert (
        display_only_spot_payload["diagnostics"]["diag_frame_quality_code"]
        == "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
    )

    unknown_entitlement_rows = _with_gex_test_contract_observations(
        paired_rows,
        entitlement="unknown",
    )
    unknown_entitlement_payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "unknown",
            "spot": spot,
            "spot_observation": _gex_test_spot_observation(
                spot,
                entitlement="unknown",
            ),
            "strike_count": 21,
            "requested_strike_limit": 21,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "option_universe_expires_at": min(
                str(row["expiry_at"]) for row in unknown_entitlement_rows
            ),
        },
        strikes=paired_strikes,
        diagnostics=gex._gex_diagnostics(
            paired_rows,
            paired_strikes,
            contracts_requested=42,
            strikes_requested=21,
            generic_ticks="101",
        ),
        max_levels=21,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=unknown_entitlement_rows,
        comparison_contract_rows=unknown_entitlement_rows,
    )
    assert unknown_entitlement_payload["frame_complete"] is False
    assert unknown_entitlement_payload["decision_authoritative"] is False
    assert len(unknown_entitlement_payload["levels"]) == 21
    assert unknown_entitlement_payload["comparison_scope"]["market_data_entitlement"] == "unknown"

    reduced_paired_rows = paired_rows[:10]
    reduced_paired_strikes = gex._aggregate_by_strike(reduced_paired_rows, spot)
    reduced_paired_payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": spot,
            "spot_observation": _gex_test_spot_observation(
                spot,
                observed_at=captured_at,
            ),
            "strike_count": 5,
            "requested_strike_limit": 21,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": 5,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in reduced_paired_rows),
        },
        strikes=reduced_paired_strikes,
        diagnostics=gex._gex_diagnostics(
            reduced_paired_rows,
            reduced_paired_strikes,
            contracts_requested=10,
            strikes_requested=21,
            generic_ticks="101",
            analysis_contract_rows=reduced_paired_rows,
            authoritative_contract_rows=reduced_paired_rows,
        ),
        max_levels=21,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=reduced_paired_rows,
        comparison_contract_rows=reduced_paired_rows,
    )

    assert len(reduced_paired_strikes) == 5
    assert reduced_paired_payload["decision_authoritative"] is True
    assert reduced_paired_payload["frame_complete"] is True
    assert reduced_paired_payload["status"] == "ok"
    assert len(reduced_paired_payload["levels"]) == 5
    assert len(reduced_paired_payload["expiry_profile"]) == 5
    assert reduced_paired_payload["diagnostics"]["diag_frame_quality_code"] == "COMPLETE"
    assert reduced_paired_payload["diagnostics"]["diag_frame_qualified_strikes"] == 5
    assert reduced_paired_payload["comparison_scope"]["strike_count"] == 5
    reduced_paired_payload["instrument_id"] = _instrument_id(_ES)
    reduced_paired_payload["route_fingerprint"] = _route_key(_ES)
    store = FakeGexStore([])
    assert gex.persist_gex_snapshot(
        "ES",
        reduced_paired_payload["raw"]["meta"],
        reduced_paired_payload["raw"]["strikes"],
        reduced_paired_payload,
        reduced_paired_payload["raw"]["contracts"],
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        store=store,
        captured_at=captured_at,
        capture_mode="request",
    )


def test_gex_live_trend_attaches_backend_motion_to_canonical_levels() -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "trend"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
    )
    first_ts = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    first_payload = {
        "levels": [{"price": 7575.0}],
        "expiry_profile": [],
    }

    first_meta = gex_live._attach_live_trend(
        session,
        first_payload,
        [
            {
                "strike": 7575.0,
                "call_gex": 100_000_000.0,
                "put_gex": -90_000_000.0,
                "net_gex": 10_000_000.0,
                "abs_gex": 190_000_000.0,
            }
        ],
        {"call_wall": 7575.0, "put_wall": 7570.0},
        first_ts,
    )

    assert first_meta["status"] == "warming"
    assert "motion" not in first_payload["levels"][0]

    next_payload = {
        "levels": [{"price": 7575.0}],
        "expiry_profile": [],
    }
    next_meta = gex_live._attach_live_trend(
        session,
        next_payload,
        [
            {
                "strike": 7575.0,
                "call_gex": 130_000_000.0,
                "put_gex": -70_000_000.0,
                "net_gex": 60_000_000.0,
                "abs_gex": 200_000_000.0,
            }
        ],
        {"call_wall": 7575.0, "put_wall": 7570.0},
        first_ts + timedelta(seconds=25),
    )

    assert next_meta["status"] == "ready"
    assert next_meta["event_count"] == 3
    assert next_payload["levels"][0]["motion"]["call"]["type"] == "strengthening"
    assert next_payload["levels"][0]["motion"]["put"]["type"] == "weakening"
    assert next_payload["levels"][0]["motion"]["net"]["type"] == "strengthening"


def test_gex_live_trend_marks_wall_roll_on_target_strike() -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "roll"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
    )
    first_ts = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    gex_live._attach_live_trend(
        session,
        {"levels": [{"price": 7575.0}], "expiry_profile": []},
        [
            {
                "strike": 7575.0,
                "call_gex": 100_000_000.0,
                "put_gex": -90_000_000.0,
                "net_gex": 10_000_000.0,
                "abs_gex": 190_000_000.0,
            },
            {
                "strike": 7580.0,
                "call_gex": 95_000_000.0,
                "put_gex": -70_000_000.0,
                "net_gex": 25_000_000.0,
                "abs_gex": 165_000_000.0,
            },
        ],
        {"call_wall": 7575.0, "put_wall": 7570.0},
        first_ts,
    )
    payload = {"levels": [{"price": 7580.0}], "expiry_profile": []}

    meta = gex_live._attach_live_trend(
        session,
        payload,
        [
            {
                "strike": 7575.0,
                "call_gex": 100_000_000.0,
                "put_gex": -90_000_000.0,
                "net_gex": 10_000_000.0,
                "abs_gex": 190_000_000.0,
            },
            {
                "strike": 7580.0,
                "call_gex": 96_000_000.0,
                "put_gex": -70_000_000.0,
                "net_gex": 26_000_000.0,
                "abs_gex": 166_000_000.0,
            },
        ],
        {"call_wall": 7580.0, "put_wall": 7570.0},
        first_ts + timedelta(seconds=25),
    )

    assert meta["status"] == "ready"
    assert payload["levels"][0]["motion"]["call"]["type"] == "roll_up"
    assert payload["levels"][0]["motion"]["call"]["from_price"] == 7575.0


def test_gex_live_option_volume_events_accumulate_contract_deltas_by_strike() -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "option-volume"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
    )
    first_ts = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    series = {
        "expiry": "20260710",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
    }
    first_rows = [
        {
            **series,
            "con_id": 101,
            "strike": 7575.0,
            "right": "C",
            "volume": 100.0,
            "open_interest": 1000.0,
            "gamma": 0.01,
            "iv": 0.20,
            "gex": 100_000_000.0,
            "abs_gex": 100_000_000.0,
            "bid": 10.0,
            "ask": 10.5,
        },
        {
            **series,
            "con_id": 102,
            "strike": 7575.0,
            "right": "P",
            "volume": 50.0,
            "open_interest": 900.0,
            "gamma": 0.01,
            "iv": 0.22,
            "gex": -90_000_000.0,
            "abs_gex": 90_000_000.0,
            "bid": 9.0,
            "ask": 9.5,
        },
    ]
    first_strikes = gex._aggregate_by_strike(first_rows, 7575.0)

    first_meta = gex_live._attach_live_option_volume_events(
        session,
        first_rows,
        first_strikes,
        first_ts,
    )

    assert first_meta["status"] == "warming"
    assert gex._option_volume_context_from_strike(first_strikes[0])["event"] is None

    next_rows = [
        {**first_rows[0], "volume": 112.0, "gamma": 0.011, "iv": 0.21, "bid": 10.25, "ask": 10.75},
        {**first_rows[1], "volume": 56.0, "gamma": 0.009, "iv": 0.215, "bid": 9.1, "ask": 9.4},
    ]
    next_strikes = gex._aggregate_by_strike(next_rows, 7575.0)

    next_meta = gex_live._attach_live_option_volume_events(
        session,
        next_rows,
        next_strikes,
        first_ts + timedelta(seconds=16),
    )

    assert next_meta["status"] == "ready"
    assert next_meta["event_window_seconds"] == 15.0
    assert next_meta["history_window_seconds"] == 120.0
    assert next_meta["window_seconds"] == 16.0
    assert next_meta["event_count"] == 1
    context = gex._option_volume_context_from_strike(next_strikes[0])
    current = context["current"]
    flow = context["event"]
    assert current["call_volume"] == 112.0
    assert current["put_volume"] == 56.0
    assert current["total_volume"] == 168.0
    assert current["total_oi"] == 1900.0
    assert current["turnover"] == 0.0884
    assert current["rank"] == 1
    assert "state" not in current
    assert "material" not in current
    assert flow is not None
    assert flow["call_volume_delta"] == 12.0
    assert flow["put_volume_delta"] == 6.0
    assert flow["total_volume_delta"] == 18.0
    assert flow["bias"] == 0.3333
    assert flow["call_participation"] == 0.6667
    assert flow["turnover"] == 0.0095
    assert flow["interaction"] == "price_near_level"
    assert flow["cross_direction"] == ""
    assert flow["state"] == "two_sided"
    assert "total_volume_delta" not in next_strikes[0]
    assert "live_flow" not in next_strikes[0]

    reset_rows = [
        {**next_rows[0], "volume": 120.0},
        {**next_rows[1], "volume": 55.0},
    ]
    reset_strikes = gex._aggregate_by_strike(reset_rows, 7575.0)
    reset_meta = gex_live._attach_live_option_volume_events(
        session,
        reset_rows,
        reset_strikes,
        first_ts + timedelta(seconds=24),
    )

    assert reset_meta["status"] == "invalid"
    assert reset_meta["reason"] == "CONTRACT_VOLUME_RESET"
    assert reset_meta["event_count"] == 0
    assert gex._option_volume_context_from_strike(reset_strikes[0])["event"] is None


def test_gex_live_option_volume_event_rejects_unavailable_contract_volume() -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "missing-option-volume"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
    )
    series = {
        "expiry": "20260710",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
    }
    rows = [
        {
            **series,
            "con_id": 101,
            "strike": 7575.0,
            "right": "C",
            "volume": None,
            "open_interest": 1000.0,
            "gamma": 0.01,
            "gex": 100_000_000.0,
            "abs_gex": 100_000_000.0,
        },
        {
            **series,
            "con_id": 102,
            "strike": 7575.0,
            "right": "P",
            "volume": 40.0,
            "open_interest": 900.0,
            "gamma": 0.01,
            "gex": -90_000_000.0,
            "abs_gex": 90_000_000.0,
        },
    ]
    strikes = gex._aggregate_by_strike(rows, 7575.0)

    meta = gex_live._attach_live_option_volume_events(
        session,
        rows,
        strikes,
        datetime(2026, 7, 10, 7, 0, tzinfo=UTC),
    )

    assert meta["status"] == "invalid"
    assert meta["reason"] == "VOLUME_UNAVAILABLE"
    assert meta["invalid_frame"] == "current"
    assert meta["invalid_contract_key"] == "con:101"
    assert meta["event_count"] == 0
    strike = strikes[0]
    assert strike["call_volume"] is None
    assert strike["put_volume"] == 40.0
    assert strike["call_oi"] == 1000.0
    assert strike["put_oi"] == 900.0
    context = gex._option_volume_context_from_strike(strike)
    assert context["current"] == {
        "call_volume": None,
        "put_volume": 40.0,
        "total_volume": None,
        "call_oi": 1000.0,
        "put_oi": 900.0,
        "total_oi": 1900.0,
        "turnover": None,
        "rank": None,
    }
    assert context["event"] is None


def test_gex_expiry_profile_is_compact_and_arithmetically_canonical() -> None:
    series = {
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
    }
    profile = gex._expiry_profile_from_contracts(
        [
            {
                **series,
                "expiry": "20260710",
                "strike": 7575.0,
                "right": "C",
                "gamma": 0.01,
                "open_interest": 1.0,
                "volume": None,
                "gex": 50_000_000.0,
                "abs_gex": 50_000_000.0,
            },
            {
                **series,
                "expiry": "20260710",
                "strike": 7575.0,
                "right": "P",
                "gamma": 0.01,
                "open_interest": 1.0,
                "volume": None,
                "gex": -90_000_000.0,
                "abs_gex": 90_000_000.0,
            },
        ],
    )

    assert profile == [
        {
            "expiry": "20260710",
            "strike": 7575.0,
            "call_gex": 50_000_000.0,
            "put_gex": -90_000_000.0,
            "net_gex": -40_000_000.0,
            "abs_gex": 140_000_000.0,
        }
    ]


def test_gex_expiry_profile_rejects_strength_without_call_put_exposure() -> None:
    with pytest.raises(ValueError, match="internally inconsistent"):
        gex.require_gex_expiry_profile(
            [
                {
                    "expiry": "20260710",
                    "strike": 7575.0,
                    "call_gex": 0.0,
                    "put_gex": 0.0,
                    "net_gex": 0.0,
                    "abs_gex": 1.0,
                }
            ]
        )


def test_gex_public_validators_reject_numeric_string_coercion() -> None:
    level = _canonical_gex_level(100.0, spot=100.0)
    with pytest.raises(ValueError, match="numeric facts must be finite"):
        gex.require_gex_levels(
            [{**level, "net_gex": str(level["net_gex"])}],
            max_levels=1,
            spot=100.0,
        )

    with pytest.raises(ValueError, match="facts must be finite numbers"):
        gex.require_gex_expiry_profile(
            [
                {
                    "expiry": "20260710",
                    "strike": "100.0",
                    "call_gex": 10.0,
                    "put_gex": -4.0,
                    "net_gex": 6.0,
                    "abs_gex": 14.0,
                }
            ]
        )

    motion = {
        "call": {
            "type": "strengthening",
            "side": "call",
            "from": 10.0,
            "to": 20.0,
            "delta": 10.0,
            "delta_pct": 1.0,
            "window_seconds": "30.0",
        }
    }
    with pytest.raises(ValueError, match="positive finite window"):
        gex.require_gex_levels(
            [{**level, "motion": motion}],
            max_levels=1,
            spot=100.0,
        )


def test_gex_live_option_volume_comparison_requires_exact_universe_and_no_resets() -> None:
    ts = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    baseline_rows = [
        {
            "con_id": 101,
            "expiry": "20260710",
            "strike": 7575.0,
            "right": "C",
            "gamma": None,
            "open_interest": None,
            "volume": 100.0,
            "gex": None,
            "abs_gex": None,
        },
        {
            "con_id": 102,
            "expiry": "20260710",
            "strike": 7575.0,
            "right": "P",
            "gamma": None,
            "open_interest": None,
            "volume": 50.0,
            "gex": None,
            "abs_gex": None,
        },
    ]
    baseline = gex_live_analysis._live_option_volume_frame(baseline_rows, ts)
    changed_universe = gex_live_analysis._live_option_volume_frame(
        [{**baseline_rows[0], "volume": 110.0}],
        ts + timedelta(seconds=16),
    )
    reset = gex_live_analysis._live_option_volume_frame(
        [
            {**baseline_rows[0], "volume": 110.0},
            {**baseline_rows[1], "volume": 49.0},
        ],
        ts + timedelta(seconds=16),
    )

    universe_result = gex_live_analysis._compare_live_option_volume_frames(
        baseline,
        changed_universe,
    )
    reset_result = gex_live_analysis._compare_live_option_volume_frames(
        baseline,
        reset,
    )

    assert universe_result["status"] == "invalid"
    assert universe_result["reason"] == "CONTRACT_UNIVERSE_CHANGED"
    assert universe_result["contract_deltas"] == {}
    assert reset_result["status"] == "invalid"
    assert reset_result["reason"] == "CONTRACT_VOLUME_RESET"
    assert reset_result["invalid_contract_key"] == "con:102"
    assert reset_result["contract_deltas"] == {}


def test_gex_live_option_volume_acceleration_compares_volume_rates_by_strike() -> None:
    frames = [
        {
            "ts": 0.0,
            "status": "ready",
            "contracts": {
                "con:101": {
                    "strike": 100.0,
                    "right": "C",
                    "expiry": "20260710",
                    "volume": 100.0,
                },
                "con:102": {
                    "strike": 105.0,
                    "right": "P",
                    "expiry": "20260710",
                    "volume": 50.0,
                },
            },
        },
        {
            "ts": 5.0,
            "status": "ready",
            "contracts": {
                "con:101": {
                    "strike": 100.0,
                    "right": "C",
                    "expiry": "20260710",
                    "volume": 110.0,
                },
                "con:102": {
                    "strike": 105.0,
                    "right": "P",
                    "expiry": "20260710",
                    "volume": 55.0,
                },
            },
        },
        {
            "ts": 10.0,
            "status": "ready",
            "contracts": {
                "con:101": {
                    "strike": 100.0,
                    "right": "C",
                    "expiry": "20260710",
                    "volume": 130.0,
                },
                "con:102": {
                    "strike": 105.0,
                    "right": "P",
                    "expiry": "20260710",
                    "volume": 55.0,
                },
            },
        },
    ]

    assert gex_live_analysis._live_option_volume_interval_rates_by_strike(
        frames,
        current_ts=20.0,
    ) == {
        100.0: [2.0, 4.0],
        105.0: [1.0],
    }


def test_gex_live_option_volume_rate_comparisons_are_linear_in_frame_count(
    monkeypatch,
) -> None:
    strikes = tuple(100.0 + float(index) for index in range(17))
    frames = [
        {
            "ts": float(frame_index),
            "status": "ready",
            "contracts": {
                f"con:{strike_index + 1}": {
                    "strike": strike,
                    "right": "C",
                    "expiry": "20260710",
                    "volume": float(frame_index * (strike_index + 1)),
                }
                for strike_index, strike in enumerate(strikes)
            },
        }
        for frame_index in range(60)
    ]
    original_compare = gex_live_analysis._compare_live_option_volume_frames
    comparison_count = 0

    def counted_compare(baseline, current):
        nonlocal comparison_count
        comparison_count += 1
        return original_compare(baseline, current)

    monkeypatch.setattr(
        gex_live_analysis,
        "_compare_live_option_volume_frames",
        counted_compare,
    )

    rates_by_strike = gex_live_analysis._live_option_volume_interval_rates_by_strike(
        frames,
        current_ts=60.0,
    )

    assert comparison_count == len(frames) - 1
    assert set(rates_by_strike) == set(strikes)
    assert all(len(rates) == len(frames) - 1 for rates in rates_by_strike.values())


def test_gex_live_option_volume_ignores_immaterial_single_contract_noise() -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "flow-noise"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
    )
    first_ts = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    series = {
        "expiry": "20260710",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
        "strike": 7580.0,
    }
    first_rows = [
        {
            **series,
            "con_id": 101,
            "right": "C",
            "volume": 100.0,
            "open_interest": 2000.0,
            "gamma": 0.01,
            "iv": 0.20,
            "gex": 100_000_000.0,
            "abs_gex": 100_000_000.0,
        },
        {
            **series,
            "con_id": 102,
            "right": "P",
            "volume": 0.0,
            "open_interest": 2000.0,
            "gamma": 0.01,
            "iv": 0.20,
            "gex": -50_000_000.0,
            "abs_gex": 50_000_000.0,
        },
    ]
    first_strikes = gex._aggregate_by_strike(first_rows, 7575.0)
    gex_live._attach_live_option_volume_events(session, first_rows, first_strikes, first_ts)

    next_rows = [{**row, "volume": 101.0 if row["right"] == "C" else 0.0} for row in first_rows]
    next_strikes = gex._aggregate_by_strike(next_rows, 7575.0)
    gex_live._attach_live_option_volume_events(
        session,
        next_rows,
        next_strikes,
        first_ts + timedelta(seconds=16),
    )

    flow = gex._option_volume_context_from_strike(next_strikes[0])["event"]
    assert flow is not None
    assert flow["total_volume_delta"] == 1.0
    assert flow["material"] is False
    assert flow["interaction"] == ""
    assert flow["cross_direction"] == ""
    assert flow["state"] == "quiet"


def test_gex_live_cross_direction_is_typed_for_both_directions() -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "flow-cross"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7574.0,
    )
    first_ts = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    series = {
        "expiry": "20260710",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
        "strike": 7575.0,
    }
    first_rows = [
        {
            **series,
            "con_id": 101,
            "right": "C",
            "volume": 100.0,
            "open_interest": 1000.0,
            "gamma": 0.01,
            "iv": 0.20,
            "gex": 100_000_000.0,
            "abs_gex": 100_000_000.0,
        },
        {
            **series,
            "con_id": 102,
            "right": "P",
            "volume": 0.0,
            "open_interest": 1000.0,
            "gamma": 0.01,
            "iv": 0.20,
            "gex": -50_000_000.0,
            "abs_gex": 50_000_000.0,
        },
    ]
    first_strikes = gex._aggregate_by_strike(first_rows, 7574.0)
    gex_live._attach_live_option_volume_events(session, first_rows, first_strikes, first_ts)

    session.spot = 7576.0
    second_rows = [{**row, "volume": 140.0 if row["right"] == "C" else 0.0} for row in first_rows]
    second_strikes = gex._aggregate_by_strike(second_rows, 7576.0)
    gex_live._attach_live_option_volume_events(
        session,
        second_rows,
        second_strikes,
        first_ts + timedelta(seconds=16),
    )
    second_event = gex._option_volume_context_from_strike(second_strikes[0])["event"]
    assert second_event is not None
    assert second_event["interaction"] == "price_crossed_with_activity"
    assert second_event["cross_direction"] == "up"

    session.spot = 7574.0
    third_rows = [{**row, "volume": 180.0 if row["right"] == "C" else 0.0} for row in first_rows]
    third_strikes = gex._aggregate_by_strike(third_rows, 7574.0)
    gex_live._attach_live_option_volume_events(
        session,
        third_rows,
        third_strikes,
        first_ts + timedelta(seconds=32),
    )
    third_event = gex._option_volume_context_from_strike(third_strikes[0])["event"]
    assert third_event is not None
    assert third_event["interaction"] == "price_crossed_with_activity"
    assert third_event["cross_direction"] == "down"


def test_gex_current_volume_context_is_shared_by_request_and_live_payloads() -> None:
    series = {
        "expiry": "20260710",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
    }
    rows = [
        {
            **series,
            "con_id": con_id,
            "strike": strike,
            "right": right,
            "volume": volume,
            "open_interest": 50.0,
            "gex": exposure,
            "abs_gex": abs(exposure),
            "gamma": abs(exposure) / (50.0 * 50.0 * 105.0 * 105.0 * 0.01),
        }
        for con_id, strike, right, volume, exposure in (
            (101, 100.0, "C", 80.0, 500.0),
            (102, 100.0, "P", 70.0, -300.0),
            (103, 105.0, "C", 60.0, 400.0),
            (104, 105.0, "P", 40.0, -200.0),
            (105, 110.0, "C", 5.0, 300.0),
            (106, 110.0, "P", 5.0, -100.0),
        )
    ]
    captured_at = datetime.now(tz=UTC)
    rows = _with_gex_test_contract_observations(
        rows,
        observed_at=captured_at,
    )

    strikes = gex._aggregate_by_strike(rows, 105.0)
    by_strike = {row["strike"]: row for row in strikes}

    context_100 = gex._option_volume_context_from_strike(by_strike[100.0])
    context_105 = gex._option_volume_context_from_strike(by_strike[105.0])
    context_110 = gex._option_volume_context_from_strike(by_strike[110.0])
    assert context_100["current"] == {
        "call_volume": 80.0,
        "put_volume": 70.0,
        "total_volume": 150.0,
        "call_oi": 50.0,
        "put_oi": 50.0,
        "total_oi": 100.0,
        "turnover": 1.5,
        "rank": 1,
    }
    assert context_100["event"] is None
    assert context_105["current"]["rank"] == 2
    assert context_110["current"]["rank"] == 3
    assert context_110["event"] is None
    assert "material" not in context_110["current"]

    levels = gex._levels_from_strikes(strikes)
    selected_levels = gex._select_gex_levels(strikes, 105.0, levels, max_levels=3)
    selected = next(row for row in selected_levels if row["price"] == 100.0)

    assert set(selected["option_volume_context"]) == {"current", "event"}
    assert selected["option_volume_context"]["current"] == context_100["current"]
    assert selected["option_volume_context"]["event"] is None

    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 105.0,
            "spot_observation": _gex_test_spot_observation(
                105.0,
                observed_at=captured_at,
            ),
            "strike_count": 3,
            "requested_strike_limit": 3,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": len(rows) // 2,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in rows),
        },
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            rows,
            strikes,
            contracts_requested=len(rows),
            strikes_requested=3,
            generic_ticks="101",
        ),
        max_levels=3,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=rows,
        comparison_contract_rows=rows,
    )
    assert all(
        not any(str(key).startswith("_") for key in row) for row in payload["raw"]["strikes"]
    )
    payload_level = next(row for row in payload["levels"] if row["price"] == 100.0)
    assert payload_level["option_volume_context"] == selected["option_volume_context"]


def test_gex_request_finalization_persists_pre_expiry_capture_but_rejects_delivery(
    monkeypatch,
) -> None:
    expiry_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    captured_at = expiry_at - timedelta(seconds=1)
    persisted: list[datetime] = []
    payload = {
        "frame_complete": True,
        "captured_at": captured_at.isoformat(),
        "levels": [{"price": 600.0}],
        "raw": {
            "meta": {
                "option_universe_expires_at": expiry_at.isoformat(),
            },
            "strikes": [{"strike": 600.0}],
            "contracts": [{"con_id": 1}],
        },
    }
    monkeypatch.setattr(
        gex_context_module,
        "_maybe_update_dividend_yield_cache",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        gex_context_module,
        "persist_gex_snapshot",
        lambda *_args, **kwargs: persisted.append(kwargs["captured_at"]) or True,
    )
    monkeypatch.setattr(
        gex_context_module,
        "get_gex_history_series",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(OptionUniverseUnavailableError) as caught:
        gex_context_module._finalize_gex_request_payload(
            "SPY",
            payload,
            instrument=_SPY,
            store="store",
        )

    assert caught.value.reason == "OPTION_UNIVERSE_ROLLOVER"
    assert persisted == [captured_at]


def test_cached_gex_snapshot_becomes_display_only_after_exact_expiry(
    monkeypatch,
) -> None:
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=2)
    expiry_at = datetime.now(tz=UTC) - timedelta(minutes=1)
    row = _gex_snapshot(
        _SPY,
        captured_at,
        {
            "capture_mode": "request",
            "spot": 600.0,
            "request_meta": {
                "refresh_mode": "request",
                "strike_count": 1,
                "futures_options": False,
            },
            "levels": [_canonical_gex_level(600.0)],
        },
        source="gex:ibkr",
        expiry=_ACTIVE_OPTION_EXPIRY,
    )
    row["payload"]["option_universe_expires_at"] = expiry_at.isoformat()
    monkeypatch.setattr(
        gex_history,
        "read_latest_gex_payload",
        lambda *_args, **_kwargs: (row, row["payload"]),
    )
    monkeypatch.setattr(
        gex_history,
        "_cached_gex_history_projection",
        lambda *_args, **_kwargs: (
            [],
            {"status": "ok", "stale": False, "message": ""},
        ),
    )

    result = gex_history.latest_gex_snapshot_payload(
        "SPY",
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        max_levels=1,
        stale_minutes=60,
        history_hours=24,
        store=object(),
    )

    assert result is not None
    assert result["status"] == "stale"
    assert result["decision_authoritative"] is False
    assert "display-only" in result["message"]


def test_gex_live_finalization_is_data_owned_bucketed_and_learns_dividend(monkeypatch) -> None:
    captured_at = datetime(2026, 7, 10, 14, 3, 12, tzinfo=UTC)
    writes: list[dict[str, Any]] = []
    learned: list[dict[str, Any]] = []
    session = _live_gex_session(
        provider_symbol="SPY",
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        key=("SPY", "persist"),
        request=_live_request(instrument=_SPY),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="SPY"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=675.0,
        last_payload={"captured_at": captured_at.isoformat()},
    )
    gex_live._LIVE_GEX_SESSIONS[_identity_key(_SPY)] = session
    monkeypatch.setattr(
        gex_live,
        "persist_gex_snapshot",
        lambda *args, **kwargs: writes.append({"args": args, "kwargs": kwargs}) or True,
    )
    monkeypatch.setattr(
        gex_live,
        "_maybe_update_dividend_yield_cache",
        lambda provider_symbol, rows, **kwargs: (
            learned.append({"provider_symbol": provider_symbol, "rows": rows, "kwargs": kwargs})
            or {
                "cached": True,
                "source": "broker_pv_dividend",
                "value": 0.015,
                "updated_at": captured_at.isoformat(),
            }
        ),
    )
    payload = {
        "source": "gex:ibkr-live",
        "captured_at": captured_at.isoformat(),
        "levels": [{"price": 675.0}],
        "request_meta": {
            "requested_market_data_entitlement": (
                session.request.provider_metadata.requested_entitlement
            ),
        },
        "live": {"publishable": True},
        "raw": {
            "meta": {
                "requested_market_data_entitlement": (
                    session.request.provider_metadata.requested_entitlement
                ),
            },
            "strikes": [{"strike": 675.0}],
            "contracts": [{"con_id": 101, "broker_dividend_yield": 0.015}],
        },
    }
    try:
        asyncio.run(
            gex_live._finalize_live_gex_frame(
                _live_gex_session_token(session),
                payload,
                store="store",
                provider_runtime=ibkr_gex_provider_runtime,
            )
        )
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert len(learned) == 1
    assert learned[0]["provider_symbol"] == "SPY"
    assert learned[0]["rows"] == [{"con_id": 101, "broker_dividend_yield": 0.015}]
    assert learned[0]["kwargs"]["now"] == captured_at
    assert session.dividend_cache_bucket == "2026-07-10T14:00:00+00:00"
    assert session.dividend_cache["value"] == 0.015
    assert session.effective_dividend_yield == 0.015
    assert session.last_payload is None
    assert payload["learned_dividend_yield_cache"]["value"] == 0.015
    assert len(writes) == 1
    assert writes[0]["kwargs"]["capture_mode"] == "live"
    assert "timestamp" not in writes[0]["args"][1]
    assert writes[0]["kwargs"]["captured_at"] == captured_at
    assert writes[0]["kwargs"]["live_bucket_at"] == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    assert payload["live"]["persisted_bucket"] == "2026-07-10T14:00:00+00:00"
    assert payload["live"]["dividend_cache_ok"] is True
    assert payload["live"]["dividend_cache_error"] == ""
    assert payload["live"]["persistence_ok"] is True
    assert payload["live"]["persistence_error"] == ""
    assert payload["request_meta"] == {
        "requested_market_data_entitlement": (
            session.request.provider_metadata.requested_entitlement
        ),
    }
    assert "raw" not in payload


def test_gex_live_finalization_commit_rejects_replaced_session_generation() -> None:
    identity_key = _identity_key(_ES)
    observed_at = datetime(2026, 7, 10, 14, 3, 12, tzinfo=UTC)
    original = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "original"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        spot=7575.0,
    )
    replacement = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "replacement"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        spot=7575.0,
    )
    payload = {
        "captured_at": observed_at.isoformat(),
        "levels": [{"price": 7575.0}],
        "live": {"publishable": True},
    }
    gex_live._LIVE_GEX_SESSIONS[identity_key] = original
    try:
        plan = asyncio.run(
            gex_live._prepare_live_gex_finalization_owned(
                _live_gex_session_token(original),
                payload,
            )
        )
        assert plan is not None
        assert original.persisting_bucket == "2026-07-10T14:00:00+00:00"

        gex_live._LIVE_GEX_SESSIONS[identity_key] = replacement
        committed = asyncio.run(
            gex_live._commit_live_gex_finalization_owned(
                plan,
                learned_dividend_cache={},
                dividend_cache_error="",
                retry_dividend_bucket=False,
                dividend_env_override=False,
                persistence_ok=True,
                persistence_error="",
            )
        )
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert committed is None
    assert replacement.persisted_bucket == ""
    assert replacement.persisting_bucket == ""
    assert replacement.persistence_error == ""


def test_gex_live_delivery_rejects_universe_expired_after_capture(
    monkeypatch,
) -> None:
    identity_key = _identity_key(_ES)
    expiry_at = datetime.now(tz=UTC) - timedelta(seconds=1)
    captured_at = expiry_at - timedelta(seconds=1)
    contract = _ibkr_option(
        con_id=1,
        strike=7575.0,
        right="C",
        expiry=expiry_at.strftime("%Y%m%d"),
        expiry_at=expiry_at,
    )
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "delivery-boundary"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        spot=7575.0,
        contracts=[contract],
    )
    payload = {
        "captured_at": captured_at.isoformat(),
        "levels": [{"price": 7575.0}],
        "live": {"publishable": True},
    }
    cancelled: list[str] = []
    monkeypatch.setattr(
        gex_live,
        "_cancel_live_gex_session_owned",
        lambda current: cancelled.append(current.provider_symbol) or 1,
    )
    gex_live._LIVE_GEX_SESSIONS[identity_key] = session
    try:
        plan = asyncio.run(
            gex_live._prepare_live_gex_finalization_owned(
                _live_gex_session_token(session),
                payload,
            )
        )
        assert plan is not None
        with pytest.raises(OptionUniverseUnavailableError) as caught:
            asyncio.run(
                gex_live._commit_live_gex_finalization_owned(
                    plan,
                    learned_dividend_cache={},
                    dividend_cache_error="",
                    retry_dividend_bucket=False,
                    dividend_env_override=False,
                    persistence_ok=True,
                    persistence_error="",
                )
            )
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert caught.value.reason == "OPTION_UNIVERSE_ROLLOVER"
    assert cancelled == ["ES"]


def test_gex_live_reconnect_mid_bucket_keeps_one_exact_observation() -> None:
    store = FakeGexStore([])
    first_observed_at = datetime(2026, 7, 10, 14, 1, 12, tzinfo=UTC)
    reconnect_observed_at = datetime(2026, 7, 10, 14, 4, 48, tzinfo=UTC)
    finalized_payloads: list[dict[str, Any]] = []

    def payload(observed_at: datetime) -> dict[str, Any]:
        return _gex_snapshot(
            _ES,
            observed_at,
            {
                "capture_mode": "live",
                "spot": 7575.0,
                "levels": [_canonical_gex_level(7575.0)],
                "request_meta": {
                    "refresh_mode": "live",
                    "strike_count": 21,
                    "futures_options": True,
                },
                "live": {"publishable": True},
            },
            source="gex:ibkr-live",
            expiry="20260710",
        )["payload"]

    try:
        for observed_at in (first_observed_at, reconnect_observed_at):
            session = _live_gex_session(
                provider_symbol="ES",
                instrument_id=_instrument_id(_ES),
                route_fingerprint=_route_key(_ES),
                key=("ES", observed_at.isoformat()),
                request=_live_request(instrument=ibkr_future_payload("ES")),
                ib=SimpleNamespace(),
                contracts=[],
                tickers=[],
                chain_meta={},
                underlying_audit={},
                spot=7575.0,
                started_at=observed_at,
            )
            gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] = session
            frame = payload(observed_at)
            frame["live"].update(
                {
                    "session_id": session.started_at.isoformat(),
                    "started_at": session.started_at.isoformat(),
                }
            )
            asyncio.run(
                gex_live._finalize_live_gex_frame(
                    _live_gex_session_token(session),
                    frame,
                    store=store,
                    provider_runtime=ibkr_gex_provider_runtime,
                )
            )
            finalized_payloads.append(frame)
            gex_live._LIVE_GEX_SESSIONS.clear()
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert len(store.snapshots) == 1
    persisted = store.snapshots[0]
    assert persisted["source"] == "gex:ibkr-live"
    assert persisted["captured_at"] == reconnect_observed_at.isoformat()
    assert persisted["payload"]["captured_at"] == reconnect_observed_at.isoformat()
    assert persisted["payload"]["capture_revision"] == "2026-07-10T14:00:00+00:00"
    assert "timestamp" not in persisted["payload"]["raw"]["meta"]
    assert all(
        frame["live"]["persisted_bucket"] == "2026-07-10T14:00:00+00:00"
        for frame in finalized_payloads
    )


def test_gex_live_activity_uses_exact_elapsed_across_reconnect() -> None:
    store = FakeGexStore([])
    observations = (
        (datetime(2026, 7, 10, 14, 4, 48, tzinfo=UTC), 100.0, 80.0),
        (datetime(2026, 7, 10, 14, 5, 12, tzinfo=UTC), 110.0, 85.0),
    )

    try:
        for observed_at, call_volume, put_volume in observations:
            strike_rows = [
                {
                    "strike": 7575.0,
                    "call_volume": call_volume,
                    "put_volume": put_volume,
                }
            ]
            session = _live_gex_session(
                provider_symbol="ES",
                instrument_id=_instrument_id(_ES),
                route_fingerprint=_route_key(_ES),
                key=("ES", observed_at.isoformat()),
                request=_live_request(instrument=ibkr_future_payload("ES")),
                ib=SimpleNamespace(),
                contracts=[],
                tickers=[],
                chain_meta={},
                underlying_audit={},
                spot=7575.0,
                started_at=observed_at,
            )
            gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] = session
            frame = _gex_snapshot(
                _ES,
                observed_at,
                {
                    "capture_mode": "live",
                    "spot": 7575.0,
                    "levels": [_canonical_gex_level(7575.0)],
                    "request_meta": {
                        "refresh_mode": "live",
                        "strike_count": 21,
                        "futures_options": True,
                    },
                    "live": {
                        "publishable": True,
                        "session_id": observed_at.isoformat(),
                        "started_at": observed_at.isoformat(),
                    },
                    "raw": {"strikes": strike_rows},
                },
                source="gex:ibkr-live",
                expiry="20260710",
            )["payload"]
            asyncio.run(
                gex_live._finalize_live_gex_frame(
                    _live_gex_session_token(session),
                    frame,
                    store=store,
                    provider_runtime=ibkr_gex_provider_runtime,
                )
            )
            gex_live._LIVE_GEX_SESSIONS.clear()
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert [row["captured_at"] for row in store.snapshots] == [
        observed_at.isoformat() for observed_at, _call, _put in observations
    ]
    activity = store.snapshots[-1]["payload"]["option_activity"]
    assert activity["elapsed_seconds"] == 24.0
    assert activity["call"]["rate_per_minute"] == 25.0
    assert activity["put"]["rate_per_minute"] == 12.5


def test_gex_live_persistence_failure_does_not_drop_realtime_frame(monkeypatch) -> None:
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "persist-error"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
    )
    gex_live._LIVE_GEX_SESSIONS[_identity_key(_ES)] = session
    monkeypatch.setattr(
        gex_live,
        "persist_gex_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    payload = {
        "captured_at": "2026-07-10T14:03:12+00:00",
        "levels": [{"price": 7575.0}],
        "live": {"publishable": True},
        "raw": {
            "meta": {
                "requested_market_data_entitlement": (
                    session.request.provider_metadata.requested_entitlement
                ),
            },
            "strikes": [],
            "contracts": [],
        },
    }
    try:
        asyncio.run(
            gex_live._finalize_live_gex_frame(
                _live_gex_session_token(session),
                payload,
                store="store",
                provider_runtime=ibkr_gex_provider_runtime,
            )
        )
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert payload["levels"] == [{"price": 7575.0}]
    assert payload["live"]["persistence_ok"] is False
    assert payload["live"]["persistence_error"] == "database unavailable"
    assert "raw" not in payload


def test_gex_live_dividend_learning_failure_does_not_drop_realtime_frame(monkeypatch) -> None:
    session = _live_gex_session(
        provider_symbol="SPY",
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        key=("SPY", "dividend-error"),
        request=_live_request(instrument=_SPY),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="SPY"),
        contracts=[],
        tickers=[],
        chain_meta={},
        underlying_audit={},
        spot=675.0,
    )
    gex_live._LIVE_GEX_SESSIONS[_identity_key(_SPY)] = session
    monkeypatch.setattr(
        gex_live,
        "_maybe_update_dividend_yield_cache",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )
    monkeypatch.setattr(gex_live, "persist_gex_snapshot", lambda *_args, **_kwargs: True)
    payload = {
        "captured_at": "2026-07-10T14:03:12+00:00",
        "levels": [{"price": 675.0}],
        "live": {"publishable": True},
        "raw": {
            "meta": {
                "requested_market_data_entitlement": (
                    session.request.provider_metadata.requested_entitlement
                ),
            },
            "strikes": [{"strike": 675.0}],
            "contracts": [{"con_id": 101, "broker_dividend_yield": 0.015}],
        },
    }
    try:
        asyncio.run(
            gex_live._finalize_live_gex_frame(
                _live_gex_session_token(session),
                payload,
                store="store",
                provider_runtime=ibkr_gex_provider_runtime,
            )
        )
    finally:
        gex_live._LIVE_GEX_SESSIONS.clear()

    assert payload["levels"] == [{"price": 675.0}]
    assert payload["live"]["dividend_cache_ok"] is False
    assert payload["live"]["dividend_cache_error"] == "settings unavailable"
    assert payload["live"]["persistence_ok"] is True


def test_gex_live_incomplete_frame_holds_previous_publishable_payload(monkeypatch) -> None:
    previous_captured_at = datetime(2026, 7, 10, 13, 59, tzinfo=UTC)
    previous_payload = _gex_snapshot(
        _ES,
        previous_captured_at,
        {
            "capture_mode": "live",
            "spot": 7575.0,
            "spot_observation": _gex_test_spot_observation(7575.0),
            "levels": [_canonical_gex_level(7575.0)],
            "request_meta": {
                "refresh_mode": "live",
                "strike_count": 21,
                "futures_options": True,
            },
        },
        source="gex:ibkr-live",
        expiry="20260710",
    )["payload"]
    active_expiry_at = datetime.now(tz=UTC) + timedelta(days=1)
    active_expiry = active_expiry_at.strftime("%Y%m%d")
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "strict"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        ib=SimpleNamespace(),
        underlying=SimpleNamespace(localSymbol="ESU6"),
        contracts=[
            _ibkr_option(
                con_id=101,
                expiry=active_expiry,
                expiry_at=active_expiry_at,
                strike=7575,
                right="C",
            ),
            _ibkr_option(
                con_id=102,
                expiry=active_expiry,
                expiry_at=active_expiry_at,
                strike=7575,
                right="P",
            ),
        ],
        tickers=[
            (
                _ibkr_option(
                    con_id=101,
                    expiry=active_expiry,
                    expiry_at=active_expiry_at,
                    strike=7575,
                    right="C",
                ),
                SimpleNamespace(right="C"),
            ),
            (
                _ibkr_option(
                    con_id=102,
                    expiry=active_expiry,
                    expiry_at=active_expiry_at,
                    strike=7575,
                    right="P",
                ),
                SimpleNamespace(right="P"),
            ),
        ],
        chain_meta={},
        underlying_audit={},
        spot=7575.0,
        last_publishable_payload=previous_payload,
    )

    observed_at = datetime.now(tz=UTC)
    rows = tuple(
        _with_gex_test_contract_observations(
            [
                {
                    "con_id": 101,
                    "expiry": active_expiry,
                    "trading_class": "ES",
                    "exchange": "CME",
                    "multiplier": 50.0,
                    "strike": 7575.0,
                    "right": right,
                    "gamma": 0.0,
                    "open_interest": 0.0,
                    "volume": 0.0,
                    "gex": 0.0,
                    "abs_gex": 0.0,
                    "greek_source": "",
                }
                for right in ("C",)
            ]
        )
    )
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "sample_live",
        lambda *_args, **_kwargs: ibkr_gex.IbkrLiveGexSample(
            contract_rows=rows,
            market_data_entitlement="live",
        ),
    )

    payload = gex_live._live_gex_frame_from_session(
        session,
        max_levels=8,
        stale_minutes=60,
        underlying_quote=_gex_test_spot_observation(
            7575.0,
            observed_at=observed_at,
        ),
    )

    assert payload["ok"] is False
    assert payload["status"] == "warming"
    assert payload["provider_symbol"] == "ES"
    assert payload["capture_mode"] == "live"
    assert payload["levels"] == previous_payload["levels"]
    assert "profile" not in payload
    assert payload["preserved_context"] is True
    assert payload["instrument_id"] == _instrument_id(_ES)
    assert payload["route_fingerprint"] == _route_key(_ES)
    assert payload["decision_authoritative"] is False
    assert payload["live"]["holding_last_publishable"] is True
    assert "decision_authoritative" not in payload["live"]
    assert set(payload["latest_attempt"]) == {
        "source",
        "capture_mode",
        "captured_at",
        "status",
        "market_data_entitlement",
        "message",
        "diagnostics",
    }
    assert payload["latest_attempt"]["source"] == "gex:ibkr-live"
    assert payload["latest_attempt"]["capture_mode"] == "live"
    assert payload["latest_attempt"]["status"] == "warming"


def test_gex_live_exact_paired_available_subset_is_publishable(monkeypatch) -> None:
    active_expiry_at = datetime.now(tz=UTC) + timedelta(days=1)
    active_expiry = active_expiry_at.strftime("%Y%m%d")
    contracts = [
        _ibkr_option(
            con_id=200 + strike_index * 2 + (1 if right == "P" else 0),
            strike=7565.0 + strike_index * 5.0,
            right=right,
            expiry=active_expiry,
            expiry_at=active_expiry_at,
        )
        for strike_index in range(5)
        for right in ("C", "P")
    ]
    session = _live_gex_session(
        provider_symbol="ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        key=("ES", "partial"),
        request=_live_request(instrument=ibkr_future_payload("ES")),
        contracts=contracts,
        spot=7575.0,
        last_publishable_payload={
            "ok": True,
            "status": "ok",
            "provider_symbol": "ES",
            "source": "gex:ibkr-live",
            "capture_mode": "live",
            "captured_at": "2026-07-10T13:59:00+00:00",
            "decision_authoritative": True,
            "levels": [_canonical_gex_level(7600.0, spot=7575.0)],
        },
    )
    observed_at = datetime.now(tz=UTC)
    rows = tuple(
        _with_gex_test_contract_observations(
            [
                {
                    "con_id": 200 + strike_index * 2 + (1 if right == "P" else 0),
                    "expiry": active_expiry,
                    "trading_class": "ES",
                    "exchange": "CME",
                    "multiplier": 50.0,
                    "strike": 7565.0 + strike_index * 5.0,
                    "right": right,
                    "gamma": 0.01,
                    "open_interest": 100.0,
                    "volume": 10.0,
                    "gex": (28_690_312.5 if right == "C" else -28_690_312.5),
                    "abs_gex": 28_690_312.5,
                    "greek_source": "modelGreeks",
                }
                for strike_index in range(5)
                for right in ("C", "P")
            ],
            observed_at=observed_at,
        )
    )
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "sample_live",
        lambda *_args, **_kwargs: ibkr_gex.IbkrLiveGexSample(
            contract_rows=rows,
            market_data_entitlement="live",
        ),
    )

    payload = gex_live._live_gex_frame_from_session(
        session,
        max_levels=21,
        stale_minutes=60,
        underlying_quote=_gex_test_spot_observation(
            7575.0,
            observed_at=observed_at,
        ),
    )

    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["decision_authoritative"] is True
    assert len(payload["levels"]) == 5
    assert payload["levels"][0]["price"] != 7600.0
    assert payload["diagnostics"]["diag_frame_quality_code"] == "COMPLETE"
    assert payload["diagnostics"]["diag_frame_exact_pair_universe"] is True
    assert payload["live"]["warming"] is False
    assert payload["live"]["holding_last_publishable"] is False
    assert "preserved_context" not in payload
    assert "latest_attempt" not in payload


def test_gex_config_defaults_to_spx_dividend_yield(monkeypatch) -> None:
    monkeypatch.delenv("AEF_IBKR_GEX_DIVIDEND_YIELD", raising=False)

    assert AppConfig().ibkr_gex_dividend_yield == 0.0135


def test_gex_dividend_yield_from_broker_pv_dividend_is_validated() -> None:
    valid = gex._dividend_yield_from_pv_dividend(spot=500.0, years=0.5, pv_dividend=3.4)

    assert valid is not None
    assert 0.013 < valid < 0.014
    assert gex._dividend_yield_from_pv_dividend(spot=500.0, years=1 / 365, pv_dividend=0.02) is None
    assert gex._dividend_yield_from_pv_dividend(spot=500.0, years=0.5, pv_dividend=80.0) is None
    assert gex._valid_broker_dividend_yield(0.20) is False


def test_gex_dividend_yield_cache_uses_valid_weekly_broker_estimate(monkeypatch) -> None:
    from aef_terminal.storage import postgres

    saved: dict[str, dict] = {}

    class FakeStore:
        def initialize(self):
            return None

        def read_setting(self, scope, key):
            assert scope == "server"
            return saved.get(key)

        def upsert_setting(self, scope, key, value):
            assert scope == "server"
            saved[key] = value

    store = FakeStore()
    monkeypatch.setattr(postgres, "default_postgres_store", lambda: store)
    now = datetime.now(tz=UTC)

    result = gex._maybe_update_dividend_yield_cache(
        "SPY",
        [
            {"broker_dividend_yield": 0.014},
            {"broker_dividend_yield": 0.016},
            {"broker_dividend_yield": 0.20},
        ],
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        now=now,
        store=store,
    )

    assert result["cached"] is True
    assert result["code"] == "GEX_DIVIDEND_CACHE_UPDATED"
    assert result["value"] == 0.015
    assert (
        saved[gex._dividend_yield_setting_key(_instrument_id(_SPY), _route_key(_SPY))][
            "sample_count"
        ]
        == 2
    )
    assert (
        gex._cached_dividend_yield(
            _instrument_id(_SPY),
            _route_key(_SPY),
            store=store,
        )
        == 0.015
    )


def test_gex_dividend_yield_cache_reports_typed_unavailable_estimate() -> None:
    result = gex._maybe_update_dividend_yield_cache(
        "SPY",
        [],
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        store=None,
    )

    assert result["cached"] is False
    assert result["code"] == "GEX_DIVIDEND_ESTIMATE_UNAVAILABLE"


def test_gex_dividend_cache_read_failure_keeps_typed_degraded_provenance() -> None:
    class FailedStore:
        @staticmethod
        def initialize() -> None:
            return None

        @staticmethod
        def read_setting(_scope: str, _key: str) -> dict[str, Any]:
            raise RuntimeError("settings storage offline")

    request = SimpleNamespace(
        futures_options=False,
        instrument_id=_instrument_id(_SPY),
        route_fingerprint=_route_key(_SPY),
        dividend_yield=0.0135,
    )

    result = gex._dividend_yield_for_gex_request(request, store=FailedStore())

    assert result == {
        "cached": False,
        "cache_state": "error",
        "source": "config",
        "error_code": "GEX_DIVIDEND_CACHE_READ_FAILED",
        "reason": "settings storage offline",
        "value": 0.0135,
    }


def test_gex_dividend_cache_lock_is_stable_per_route_and_independent_between_routes() -> None:
    spy_route = _route_key(_SPY)
    qqq_route = _route_key(_QQQ)
    spy_lock = gex_dividend._gex_dividend_cache_lock(_instrument_id(_SPY), spy_route)

    assert spy_lock is gex_dividend._gex_dividend_cache_lock(_instrument_id(_SPY), spy_route)
    assert spy_lock is not gex_dividend._gex_dividend_cache_lock(_instrument_id(_QQQ), qqq_route)


def test_gex_dividend_cache_locks_release_when_route_has_no_consumer() -> None:
    import gc

    gex_dividend._GEX_DIVIDEND_CACHE_LOCKS.clear()
    route = route_fingerprint(_SPY)
    lock = gex_dividend._gex_dividend_cache_lock(_instrument_id(_SPY), route)

    assert len(gex_dividend._GEX_DIVIDEND_CACHE_LOCKS) == 1
    del lock
    gc.collect()

    assert len(gex_dividend._GEX_DIVIDEND_CACHE_LOCKS) == 0


def test_ibkr_option_contract_requires_exact_provider_multiplier() -> None:
    assert _ibkr_option(con_id=1, strike=500.0, right="C", multiplier="100").multiplier == 100.0
    for invalid in (
        "bad",
        "",
        " 100",
        "+100",
        "1e2",
        ".5",
        "100.",
        "１００",
        "١٠٠",
        None,
        True,
    ):
        with pytest.raises(ValueError, match="exact provider multiplier"):
            _ibkr_option(con_id=1, strike=500.0, right="C", multiplier=invalid)


def test_ibkr_option_series_query_normalizes_exact_decimal_multiplier() -> None:
    query = SimpleNamespace(
        lastTradeDateOrContractMonth="20260729",
        tradingClass="E4B",
        exchange="CME",
        multiplier="50",
    )

    assert ibkr_option_acquisition._option_series_key_from_query(query) == (
        "20260729",
        "E4B",
        "CME",
        50.0,
    )
    for invalid in (
        " 50",
        "+50",
        "5e1",
        ".50",
        "50.",
        "５０",
        "٥٠",
    ):
        with pytest.raises(ValueError, match="exact provider multiplier"):
            ibkr_option_acquisition._option_series_key_from_query(
                SimpleNamespace(**{**vars(query), "multiplier": invalid})
            )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"con_id": 0}, "provider conId"),
        ({"right": "X"}, "right must be C or P"),
        ({"right": "c"}, "right must be C or P"),
        ({"sec_type": "opt"}, "unsupported secType"),
        ({"expiry": "20260719-extra"}, "exact YYYYMMDD expiry"),
        ({"strike": 0.0}, "positive finite strike"),
        ({"strike": True}, "positive finite strike"),
    ],
)
def test_ibkr_option_contract_rejects_incomplete_qualified_identity(overrides, message) -> None:
    payload = {"con_id": 1, "strike": 500.0, "right": "C", **overrides}
    with pytest.raises(ValueError, match=message):
        _ibkr_option(**payload)


@pytest.mark.parametrize(
    "con_id",
    [True, 1.0, " 1", "+1", "01", "１"],
)
def test_ibkr_option_contract_rejects_noncanonical_provider_con_id(con_id) -> None:
    with pytest.raises(ValueError, match="exact provider conId"):
        _ibkr_option(con_id=con_id, strike=500.0, right="C")


@pytest.mark.parametrize(
    "con_id",
    [True, 123.0, " 123", "+123", "0123", "１２３"],
)
def test_gex_option_chain_cache_rejects_noncanonical_provider_con_id(con_id) -> None:
    assert ibkr_option_acquisition._option_chain_cache_key(con_id) == 0


def test_gex_greeks_prefer_broker_model_then_stream_fallbacks() -> None:
    class BadGreek:
        gamma = None

    class LastGreek:
        gamma = 0.04

    ticker = SimpleNamespace(
        modelGreeks=BadGreek(), lastGreeks=LastGreek(), bidGreeks=None, askGreeks=None
    )

    greeks, source = ibkr_gex_market._greeks_from_ticker(ticker)

    assert greeks is ticker.lastGreeks
    assert source == "lastGreeks"


def test_gex_greeks_ignore_nan_model_values() -> None:
    class BadModelGreek:
        gamma = float("nan")

    class BidGreek:
        gamma = 0.03

    ticker = SimpleNamespace(
        modelGreeks=BadModelGreek(), lastGreeks=None, bidGreeks=BidGreek(), askGreeks=None
    )

    greeks, source = ibkr_gex_market._greeks_from_ticker(ticker)

    assert greeks is ticker.bidGreeks
    assert source == "bidGreeks"


@pytest.mark.parametrize("invalid_gamma", [-1.0, 0.0])
def test_gex_greeks_ignore_nonpositive_broker_gamma(invalid_gamma: float) -> None:
    invalid = SimpleNamespace(gamma=invalid_gamma)
    valid = SimpleNamespace(gamma=0.02)
    ticker = SimpleNamespace(
        modelGreeks=invalid,
        lastGreeks=valid,
        bidGreeks=None,
        askGreeks=None,
    )

    greeks, source = ibkr_gex_market._greeks_from_ticker(ticker)

    assert greeks is valid
    assert source == "lastGreeks"


def test_gex_option_row_rejects_negative_open_interest_and_gamma_sentinels() -> None:
    expiry_at = datetime.now(tz=UTC) + timedelta(days=1)
    ticker = SimpleNamespace(
        modelGreeks=SimpleNamespace(gamma=-1.0, delta=2.0, impliedVol=None),
        lastGreeks=None,
        bidGreeks=None,
        askGreeks=None,
        callOpenInterest=-1.0,
        openInterest=-1.0,
        volume=0.0,
        bid=None,
        ask=None,
        last=None,
        close=None,
    )

    row = ibkr_gex_market._option_row_from_ticker(
        _ibkr_option(
            con_id=1,
            strike=5000.0,
            right="C",
            expiry=expiry_at.strftime("%Y%m%d"),
            expiry_at=expiry_at,
        ),
        ticker,
        5000.0,
        risk_free_rate=0.05,
        dividend_yield=0.0,
    )

    assert row["gamma"] is None
    assert row["delta"] is None
    assert row["open_interest"] is None
    assert row["volume"] == 0.0
    assert row["gex"] is None
    assert row["abs_gex"] is None
    assert not {
        "open_interest_live",
        "open_interest_source",
        "volume_available",
        "volume_source",
    }.intersection(row)


def test_gex_incomplete_open_interest_strike_is_never_aggregated_or_authoritative() -> None:
    series = {
        "expiry": "20260710",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
        "strike": 7575.0,
        "gamma": 0.01,
        "volume": 10.0,
    }
    contract_rows = [
        {
            **series,
            "con_id": 101,
            "right": "C",
            "open_interest": None,
            "gex": None,
            "abs_gex": None,
        },
        {
            **series,
            "con_id": 102,
            "right": "P",
            "open_interest": 900.0,
            "gex": -258_212_812.5,
            "abs_gex": 258_212_812.5,
        },
    ]
    contract_rows = _with_gex_test_contract_observations(contract_rows)

    assert require_gex_contract_market_facts(contract_rows[0])["open_interest"] is None
    strikes = gex._aggregate_by_strike(contract_rows, 7575.0)
    diagnostics = gex._gex_diagnostics(
        contract_rows,
        strikes,
        contracts_requested=2,
        strikes_requested=1,
        generic_ticks="101",
    )
    payload = gex._payload_from_rows(
        "ES",
        captured_at=datetime.now(tz=UTC),
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 7575.0,
            "spot_observation": _gex_test_spot_observation(7575.0),
            "strike_count": 1,
            "requested_strike_limit": 1,
            "futures_options": True,
        },
        strikes=strikes,
        diagnostics=diagnostics,
        max_levels=1,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=contract_rows,
        comparison_contract_rows=None,
    )

    assert strikes == []
    assert diagnostics["diag_open_interest_unavailable_rows"] == 1
    assert diagnostics["diag_usable_pair_count"] == 0
    assert payload["decision_authoritative"] is False
    assert payload["levels"] == []


def test_gex_snapshot_validator_rejects_cross_contract_drift_and_legacy_shapes() -> None:
    captured_at = datetime.now(tz=UTC)
    spot = 100.0
    contracts = [
        {
            "con_id": con_id,
            "expiry": _ACTIVE_OPTION_EXPIRY,
            "trading_class": "SPY",
            "exchange": "SMART",
            "multiplier": 100.0,
            "strike": 100.0,
            "right": right,
            "gamma": gamma,
            "open_interest": 10.0,
            "volume": volume,
            "reference_option_price": None,
            "reference_option_price_source": "unavailable",
            "gex": gex_value,
            "abs_gex": abs(gex_value),
            "expiry_at": _ACTIVE_OPTION_EXPIRY_AT.isoformat(),
            "expiry_time_source": "ibkr_contract_details",
        }
        for con_id, right, gamma, volume, gex_value in (
            (1, "C", 0.01, 12.0, 1_000.0),
            (2, "P", 0.005, 7.0, -500.0),
        )
    ]
    contracts = _with_gex_test_contract_observations(
        contracts,
        observed_at=captured_at,
    )
    with pytest.raises(ValueError, match="forbidden duplicate market-state"):
        require_gex_contract_market_facts(
            {
                **contracts[0],
                "risk_free_rate": 0.05,
                "dividend_yield": 0.0,
            }
        )
    strikes = gex._aggregate_by_strike(contracts, spot)
    payload = gex._payload_from_rows(
        "SPY",
        captured_at=captured_at,
        meta={
            "provider_symbol": "SPY",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": spot,
            "spot_observation": _gex_test_spot_observation(
                spot,
                observed_at=captured_at,
            ),
            "refresh_mode": "request",
            "strike_count": 1,
            "requested_strike_limit": 1,
            "futures_options": False,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": 1,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in contracts),
        },
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            contracts,
            strikes,
            contracts_requested=2,
            strikes_requested=1,
            generic_ticks="101",
            analysis_contract_rows=contracts,
            authoritative_contract_rows=contracts,
        ),
        max_levels=1,
        stale_minutes=60,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=contracts,
        comparison_contract_rows=contracts,
    )
    payload["option_activity"] = None
    payload["option_activity_status"] = "NEED_PREVIOUS"
    canonical = gex.require_gex_snapshot_payload(
        payload,
        projection="persisted",
    )
    assert canonical["decision_authoritative"] is True
    assert canonical["net_gex"] == 500.0

    missing_exact_expiry = deepcopy(payload)
    missing_exact_expiry["raw"]["contracts"][0]["expiry_at"] = None
    with pytest.raises(
        ValueError,
        match="complete exact raw contract expiry facts",
    ):
        gex.require_gex_snapshot_payload(
            missing_exact_expiry,
            projection="persisted",
        )

    conflicting_series_expiry = deepcopy(payload)
    conflicting_series_expiry["raw"]["contracts"][1]["expiry_at"] = (
        datetime.fromisoformat(conflicting_series_expiry["raw"]["contracts"][0]["expiry_at"])
        + timedelta(minutes=1)
    ).isoformat()
    with pytest.raises(
        ValueError,
        match="conflicting provider expiry timestamps",
    ):
        gex.require_gex_snapshot_payload(
            conflicting_series_expiry,
            projection="persisted",
        )

    undercovered = deepcopy(payload)
    undercovered["raw"]["meta"]["qualified_pair_count"] = 2
    undercovered_diagnostics = {
        **undercovered["diagnostics"],
        "diag_contracts_requested": 4,
        "diag_expected_pair_count": 2,
    }
    _publishable, _reason, undercovered_quality = gex._gex_frame_quality(
        undercovered_diagnostics,
        undercovered["raw"]["strikes"],
    )
    undercovered["diagnostics"] = {
        **undercovered_diagnostics,
        **undercovered_quality,
    }
    with pytest.raises(ValueError, match="canonical pair coverage"):
        gex.require_gex_snapshot_payload(
            undercovered,
            projection="persisted",
        )

    delayed_payload = deepcopy(payload)
    delayed_payload["market_data_entitlement"] = "delayed"
    delayed_payload["decision_authoritative"] = False
    delayed_payload["raw"]["meta"]["market_data_entitlement"] = "delayed"
    delayed_payload["raw"]["meta"]["spot_observation"] = _gex_test_spot_observation(
        spot, entitlement="delayed"
    )
    delayed_payload["raw"]["contracts"] = _with_gex_test_contract_observations(
        delayed_payload["raw"]["contracts"],
        entitlement="delayed",
    )
    delayed_payload["comparison_scope"]["market_data_entitlement"] = "delayed"
    assert (
        gex.require_gex_snapshot_payload(
            delayed_payload,
            projection="persisted",
        )["decision_authoritative"]
        is False
    )
    with pytest.raises(ValueError, match="decision authority"):
        gex.require_gex_snapshot_payload(
            {**delayed_payload, "decision_authoritative": True},
            projection="persisted",
        )

    inconsistent = {**payload, "net_gex": 501.0}
    with pytest.raises(ValueError, match="net_gex disagrees with levels"):
        gex.require_gex_snapshot_payload(inconsistent, projection="persisted")

    canonical_history_projection = {
        key: value for key, value in payload.items() if key not in {"raw", "expiry_profile"}
    }
    history_projection = dict(canonical_history_projection)
    history_projection["profile"] = []
    with pytest.raises(ValueError, match="deleted legacy level shapes"):
        gex.require_gex_snapshot_payload(
            history_projection,
            projection="history",
        )
    missing_levels = {
        key: value for key, value in history_projection.items() if key not in {"levels", "profile"}
    }
    with pytest.raises(ValueError, match="missing canonical fields"):
        gex.require_gex_snapshot_payload(
            missing_levels,
            projection="history",
        )

    for missing_activity_field in ("option_activity", "option_activity_status"):
        missing_activity_pair = dict(canonical_history_projection)
        missing_activity_pair.pop(missing_activity_field)
        with pytest.raises(ValueError, match="missing canonical fields"):
            gex.require_gex_snapshot_payload(
                missing_activity_pair,
                projection="history",
            )

    with pytest.raises(ValueError, match="option activity status is invalid"):
        gex.require_gex_snapshot_payload(
            {
                **canonical_history_projection,
                "option_activity_status": "NOT_CAPTURED",
            },
            projection="history",
        )
    with pytest.raises(ValueError, match="must be null for its status"):
        gex.require_gex_snapshot_payload(
            {
                **canonical_history_projection,
                "option_activity": {"status": "READY"},
            },
            projection="history",
        )


def test_cached_gex_context_reads_latest_postgres_snapshot(monkeypatch) -> None:
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=30)
    contract_expiries = tuple(
        (captured_at + timedelta(days=offset)).strftime("%Y%m%d") for offset in (3, 1, 2)
    )
    meta = {
        "provider_symbol": "SPY",
        "requested_market_data_entitlement": "live",
        "market_data_entitlement": "live",
        "spot": 501,
        "spot_observation": _gex_test_spot_observation(
            501.0,
            observed_at=captured_at,
        ),
        "call_wall": 505,
        "put_wall": 495,
        "net_gex": 1200000,
        "refresh_mode": "request",
        "scheduler_lane": "manual",
        "strike_count": 3,
        "requested_strike_limit": 3,
        "max_expirations": 2,
        "max_contracts": 84,
        "futures_options": False,
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "tradingview_regime": "NEGATIVE|legacy-display-text-must-not-drive-domain-state",
    }
    requested_strikes = [
        {
            "strike": 495,
            "call_gex": 0,
            "put_gex": -400000,
            "net_gex": -400000,
            "abs_gex": 400000,
            "abs_flow_1pt": 80000,
            "distance_from_spot": -6,
        },
        {
            "strike": 500,
            "call_gex": 350000,
            "put_gex": -200000,
            "net_gex": 150000,
            "abs_gex": 550000,
            "abs_flow_1pt": 110000,
            "distance_from_spot": -1,
        },
        {
            "strike": 505,
            "call_gex": 900000,
            "put_gex": -100000,
            "net_gex": 800000,
            "abs_gex": 1000000,
            "abs_flow_1pt": 200000,
            "distance_from_spot": 4,
        },
    ]
    contracts = []
    for strike_row, expiry in zip(
        requested_strikes,
        contract_expiries,
        strict=True,
    ):
        contracts.extend(
            _activity_contract_rows(
                [strike_row],
                expiry=expiry,
                trading_class="SPY",
                exchange="SMART",
                multiplier=100.0,
                spot=501.0,
            )
        )
    contracts = _with_gex_test_contract_observations(
        contracts,
        observed_at=captured_at,
    )
    strikes = gex._aggregate_by_strike(contracts, 501.0)
    meta["qualified_pair_count"] = len(contracts) // 2
    meta["option_universe_expires_at"] = min(str(row["expiry_at"]) for row in contracts)
    payload = gex._payload_from_rows(
        "SPY",
        captured_at=captured_at,
        meta=meta,
        strikes=strikes,
        diagnostics=gex._gex_diagnostics(
            contracts,
            strikes,
            contracts_requested=len(contracts),
            strikes_requested=3,
            generic_ticks="101",
            analysis_contract_rows=contracts,
            authoritative_contract_rows=contracts,
        ),
        max_levels=4,
        stale_minutes=999999,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=contracts,
        comparison_contract_rows=contracts,
    )
    _install_fake_gex_store(
        monkeypatch, [_gex_snapshot(_SPY, captured_at, payload, source="gex:ibkr")]
    )

    result = gex.cached_gex_context(
        instrument=_SPY,
        max_levels=4,
        stale_minutes=999999,
    )

    assert result["ok"] is True
    assert result["provider_symbol"] == "SPY"
    assert "asset" not in result
    assert "symbol" not in result
    assert result["global_gamma_regime"] == "POSITIVE_ESTIMATE"
    assert "regime" not in result
    assert "regime_sign" not in result
    assert "market_regime" not in result
    assert "max_abs_gex" not in result
    assert "max_abs_gex_level" not in result
    assert "max_flow_level" not in result
    assert "net_flow_1pt" not in result
    assert "max_abs_flow_1pt" not in result
    levels_by_strike = {level["price"]: level for level in result["levels"]}
    assert levels_by_strike[505]["kind"] == "CALL_WALL"
    assert [level["price"] for level in result["levels"]] == [495.0, 500.0, 505.0]
    assert result["visibility_summary"]["raw_strike_count"] == 3
    assert result["visibility_summary"]["nonzero_strike_count"] == 3
    assert result["visibility_summary"]["candidate_level_count"] == 3
    assert result["visibility_summary"]["output_level_count"] == 3
    assert "profile_row_count" not in result["visibility_summary"]
    assert result["expiry_profile"]
    assert result["expiry_profile"][0]["expiry"] == contract_expiries[1]
    assert result["expiry_profile"][0]["strike"] == 500
    assert result["expiry_profile"][0]["net_gex"] == 150000
    assert result["request_meta"] == {
        "refresh_mode": "request",
        "scheduler_lane": "manual",
        "requested_market_data_entitlement": "live",
        "strike_count": 3,
        "requested_strike_limit": 3,
        "max_expirations": 2,
        "max_contracts": 84,
        "futures_options": False,
    }
    assert "raw" not in result
    assert "profile" not in result
    assert "nearest" not in result

    stale_result = gex.cached_gex_context(
        instrument=_SPY,
        max_levels=4,
        stale_minutes=1,
    )
    assert stale_result["status"] == "stale"
    assert stale_result["decision_authoritative"] is False


@pytest.mark.parametrize(
    "corruption",
    ("payload_contract", "row_provider_symbol"),
)
def test_cached_gex_context_rejects_noncanonical_row_as_refreshable_miss(
    monkeypatch,
    corruption: str,
) -> None:
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=30)
    snapshot = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "request_meta": {
                "strike_count": 2,
                "requested_strike_limit": 2,
                "futures_options": True,
            },
            "spot": 7500.0,
            "levels": [
                _canonical_gex_level(7495.0, spot=7500.0),
                _canonical_gex_level(
                    7505.0,
                    spot=7500.0,
                    kind="PUT_WALL",
                    selection_rank=2,
                ),
            ],
        },
        source="gex:ibkr",
        expiry="20260731",
    )
    canonical_snapshot = deepcopy(snapshot)
    if corruption == "payload_contract":
        snapshot["payload"]["raw"]["contracts"][0]["gamma"] = None
    else:
        snapshot["provider_symbol"] = None
    store = _install_fake_gex_store(monkeypatch, [snapshot])
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})

    with pytest.raises(
        ValueError,
        match="GEX_HISTORY_CONTRACT_INVALID",
    ):
        gex_history.read_latest_gex_payload(
            _instrument_id(_ES),
            _route_key(_ES),
            store=store,
        )

    result = gex.cached_gex_context(
        instrument=_ES,
        max_levels=4,
        stale_minutes=999999,
        store=store,
    )

    assert result["ok"] is False
    assert result["status"] == "missing"
    assert result["levels"] == []
    assert result["history"] == []
    assert result["latest_attempt"]["status"] == "invalid_cache"
    assert result["latest_attempt"]["source"] == "gex:ibkr"
    assert "GEX_HISTORY_CONTRACT_INVALID" in result["latest_attempt"]["message"]
    assert "requesting a fresh broker snapshot" in result["message"]
    assert gex._GEX_RUNTIME.error_for_route(
        _instrument_id(_ES),
        _route_key(_ES),
        sources=("cache",),
    )["message"].startswith("GEX_HISTORY_CONTRACT_INVALID:")

    store.snapshots = [canonical_snapshot]
    recovered = gex.cached_gex_context(
        instrument=_ES,
        max_levels=4,
        stale_minutes=999999,
        store=store,
    )

    assert recovered["ok"] is True
    assert (
        gex._GEX_RUNTIME.error_for_route(
            _instrument_id(_ES),
            _route_key(_ES),
            sources=("cache",),
        )["message"]
        == ""
    )


def test_cached_gex_context_keeps_current_levels_when_only_history_is_invalid(
    monkeypatch,
) -> None:
    latest_at = datetime.now(tz=UTC) - timedelta(minutes=5)
    older = _gex_snapshot(
        _ES,
        latest_at - timedelta(minutes=5),
        {
            "capture_mode": "request",
            "request_meta": {
                "strike_count": 2,
                "requested_strike_limit": 2,
                "futures_options": True,
            },
            "spot": 7500.0,
            "levels": [
                _canonical_gex_level(7495.0, spot=7500.0),
                _canonical_gex_level(
                    7505.0,
                    spot=7500.0,
                    kind="PUT_WALL",
                    selection_rank=2,
                ),
            ],
        },
        source="gex:ibkr",
        expiry="20260731",
    )
    older["payload"].pop("global_gamma_regime")
    latest = _gex_snapshot(
        _ES,
        latest_at,
        {
            "capture_mode": "request",
            "request_meta": {
                "strike_count": 2,
                "requested_strike_limit": 2,
                "futures_options": True,
            },
            "spot": 7500.0,
            "levels": [
                _canonical_gex_level(7495.0, spot=7500.0),
                _canonical_gex_level(
                    7505.0,
                    spot=7500.0,
                    kind="PUT_WALL",
                    selection_rank=2,
                ),
            ],
        },
        source="gex:ibkr",
        expiry="20260731",
    )
    store = _install_fake_gex_store(monkeypatch, [older, latest])
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})

    result = gex.cached_gex_context(
        instrument=_ES,
        max_levels=4,
        stale_minutes=999999,
        store=store,
    )

    assert result["ok"] is True
    assert result["status"] == "degraded"
    assert len(result["levels"]) == 2
    assert result["history"] == []
    assert result["history_status"]["status"] == "error"
    assert result["history_status"]["stale"] is False
    assert "GEX_HISTORY_CONTRACT_INVALID" in result["history_status"]["message"]
    assert "current canonical levels remain available" in result["message"]
    assert result["decision_authoritative"] is True


def test_cached_gex_context_types_invalid_live_history_without_request_snapshot(
    monkeypatch,
) -> None:
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=5)
    live = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "live",
            "request_meta": {
                "strike_count": 2,
                "requested_strike_limit": 2,
                "futures_options": True,
            },
            "spot": 7500.0,
            "levels": [
                _canonical_gex_level(7495.0, spot=7500.0),
                _canonical_gex_level(
                    7505.0,
                    spot=7500.0,
                    kind="PUT_WALL",
                    selection_rank=2,
                ),
            ],
        },
        source="gex:ibkr-live",
        expiry="20260731",
    )
    live["payload"].pop("global_gamma_regime")
    store = _install_fake_gex_store(monkeypatch, [live])
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})

    result = gex.cached_gex_context(
        instrument=_ES,
        max_levels=4,
        stale_minutes=999999,
        store=store,
    )

    assert result["ok"] is False
    assert result["status"] == "missing"
    assert result["levels"] == []
    assert result["history"] == []
    assert result["history_status"]["status"] == "error"
    assert result["history_status"]["stale"] is False
    assert "GEX_HISTORY_CONTRACT_INVALID" in result["history_status"]["message"]


def test_gex_payload_merges_partial_level_meta_with_computed_strike_metrics() -> None:
    captured_at = datetime(2026, 7, 10, 13, 0, tzinfo=UTC)
    requested_strikes = [
        {
            "strike": 4995,
            "call_gex": 50,
            "put_gex": -800,
            "net_gex": -750,
            "abs_gex": 850,
            "abs_flow_1pt": 85,
        },
        {
            "strike": 5005,
            "call_gex": 1200,
            "put_gex": -100,
            "net_gex": 1100,
            "abs_gex": 1300,
            "abs_flow_1pt": 130,
        },
    ]
    contract_rows = _activity_contract_rows(
        requested_strikes,
        expiry="20260717",
        spot=5000.0,
    )
    contract_rows = _with_gex_test_contract_observations(
        contract_rows,
        observed_at=captured_at,
    )
    strike_rows = gex._aggregate_by_strike(contract_rows, 5000.0)
    payload = gex._payload_from_rows(
        "ES",
        captured_at=captured_at,
        meta={
            "provider_symbol": "ES",
            "requested_market_data_entitlement": "live",
            "market_data_entitlement": "live",
            "spot": 5000,
            "spot_observation": _gex_test_spot_observation(
                5000.0,
                observed_at=captured_at,
            ),
            "strike_count": 2,
            "requested_strike_limit": 2,
            "futures_options": True,
            "risk_free_rate": 0.05,
            "dividend_yield": 0.0,
            "qualified_pair_count": len(contract_rows) // 2,
            "option_universe_expires_at": min(str(row["expiry_at"]) for row in contract_rows),
        },
        strikes=strike_rows,
        diagnostics=gex._gex_diagnostics(
            contract_rows,
            strike_rows,
            contracts_requested=len(contract_rows),
            strikes_requested=2,
            generic_ticks="101",
        ),
        max_levels=4,
        stale_minutes=999999,
        source="gex:ibkr",
        capture_mode="request",
        contract_rows=contract_rows,
        comparison_contract_rows=contract_rows,
    )

    assert payload["call_wall"] == 5005
    assert payload["put_wall"] == 4995
    assert "max_abs_gex" not in payload
    assert "max_abs_gex_level" not in payload
    assert payload["net_gex"] == 350


def test_cached_gex_context_keeps_snapshot_time_symbol_scoped(monkeypatch) -> None:
    captured_at = datetime(2026, 6, 3, 13, 0, tzinfo=UTC)
    other_asset_sync = datetime(2026, 6, 4, 9, 0, tzinfo=UTC)
    payload = {
        "ok": True,
        "provider_symbol": "SPY",
        "captured_at": captured_at.isoformat(),
        "capture_mode": "request",
        "spot": 500.0,
        "global_gamma_regime": "POSITIVE_ESTIMATE",
        "request_meta": {"strike_count": 21, "futures_options": False},
        "levels": [_canonical_gex_level(500.0)],
        "raw": {
            "meta": {"strike_count": 21, "refresh_mode": "request"},
            "strikes": [{"strike": 500, "net_gex": 1000, "abs_gex": 1000}],
        },
    }
    _install_fake_gex_store(
        monkeypatch,
        [
            _gex_snapshot(
                _SPY,
                captured_at,
                payload,
                source="gex:ibkr",
                expiry="20260605",
            )
        ],
    )
    es_id = _instrument_id(_ES)
    es_route = _route_key(_ES)
    gex._GEX_RUNTIME.mark_request_started(
        es_id,
        es_route,
        "probe",
        provider_symbol="ES",
    )
    gex._GEX_RUNTIME.mark_request_finished(es_id, es_route)
    gex._GEX_RUNTIME.record_sync(
        es_id,
        es_route,
        other_asset_sync,
        provider_symbol="ES",
    )

    result = gex.cached_gex_context(
        instrument=_SPY,
        max_levels=4,
        stale_minutes=999999,
    )

    assert result["provider_symbol"] == "SPY"
    assert result["captured_at"] == captured_at.isoformat()
    assert "snapshot_sync_at" not in result
    assert result.get("last_sync_at") != other_asset_sync.isoformat()
    assert result["request_meta"]["strike_count"] == 21
    assert "raw" not in result


def test_cached_gex_context_keeps_latest_refresh_error_visible(monkeypatch) -> None:
    captured_at = datetime(2026, 6, 3, 13, 0, tzinfo=UTC)
    payload = {
        "ok": True,
        "provider_symbol": "ES",
        "captured_at": captured_at.isoformat(),
        "capture_mode": "request",
        "spot": 7400.0,
        "global_gamma_regime": "POSITIVE_ESTIMATE",
        "request_meta": {"strike_count": 21, "futures_options": True},
        "levels": [_canonical_gex_level(7400.0)],
    }
    _install_fake_gex_store(
        monkeypatch,
        [
            _gex_snapshot(
                _ES,
                captured_at,
                payload,
                source="gex:ibkr",
                expiry="20260605",
            )
        ],
    )
    monkeypatch.setattr(gex._GEX_RUNTIME, "routes", {})
    gex._GEX_RUNTIME.record_error(
        _instrument_id(_ES),
        _route_key(_ES),
        "Connection terminated",
        provider_symbol="ES",
        source="manual",
        at=captured_at + timedelta(minutes=5),
    )

    result = gex.cached_gex_context(
        instrument=_ES,
        max_levels=4,
        stale_minutes=999999,
    )

    assert result["ok"] is True
    assert result["status"] == "refresh_error"
    assert result["last_error"] == "Connection terminated"
    assert result["last_error_source"] == "manual"
    assert "Latest GEX refresh failed" in result["message"]
    assert captured_at.isoformat() in result["message"]


def test_cached_gex_context_falls_back_to_latest_postgres_snapshot(monkeypatch, tmp_path) -> None:
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=30)
    snapshot = _gex_snapshot(
        _QQQ,
        captured_at,
        {
            "capture_mode": "request",
            "request_meta": {
                "strike_count": 21,
                "futures_options": False,
            },
            "spot": 756.0,
            "levels": [
                _canonical_gex_level(
                    760.0,
                    spot=756.0,
                    strength=1.0,
                    selection_rank=1,
                ),
                _canonical_gex_level(
                    755.0,
                    spot=756.0,
                    kind="PUT_WALL",
                    strength=1.0,
                    selection_rank=2,
                ),
            ],
        },
        source="gex:ibkr",
        expiry=(captured_at + timedelta(days=1)).strftime("%Y%m%d"),
    )

    store = FakeGexStore([snapshot])
    monkeypatch.setattr("aef_terminal.storage.postgres.default_postgres_store", lambda: store)

    result = gex.cached_gex_context(
        instrument=_QQQ,
        max_levels=1,
        stale_minutes=999999,
        store=store,
    )

    assert result["ok"] is True
    assert result["provider_symbol"] == "QQQ"
    assert result["source"] == "gex:ibkr"
    assert result["status"] == "ok"
    assert len(result["levels"]) == 1
    assert result["history"]


def test_cached_gex_context_reads_only_latest_request_snapshot(monkeypatch) -> None:
    request_at = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    live_at = request_at + timedelta(minutes=5)
    rows = [
        _gex_snapshot(
            _ES,
            request_at,
            {
                "ok": True,
                "provider_symbol": "ES",
                "captured_at": request_at.isoformat(),
                "capture_mode": "request",
                "global_gamma_regime": "POSITIVE_ESTIMATE",
                "request_meta": {"strike_count": 21, "futures_options": True},
                "spot": 7570.0,
                "levels": [_canonical_gex_level(7570.0)],
            },
            source="gex:ibkr",
            expiry="20260710",
        ),
        _gex_snapshot(
            _ES,
            live_at,
            {
                "ok": True,
                "provider_symbol": "ES",
                "captured_at": live_at.isoformat(),
                "capture_mode": "live",
                "global_gamma_regime": "NEGATIVE_ESTIMATE",
                "request_meta": {"strike_count": 9, "futures_options": True},
                "spot": 7580.0,
                "levels": [
                    _canonical_gex_level(
                        7580.0,
                        kind="PUT_WALL",
                        strength=1.0,
                    )
                ],
            },
            source="gex:ibkr-live",
            expiry="20260710",
        ),
    ]
    store = _install_fake_gex_store(monkeypatch, rows)

    result = gex.cached_gex_context(
        instrument=_ES,
        max_levels=4,
        stale_minutes=999999,
        store=store,
    )

    assert result["source"] == "gex:ibkr"
    assert result["capture_mode"] == "request"
    assert result["captured_at"] == request_at.isoformat()
    assert result["capture_revision"] == request_at.isoformat()
    assert result["spot"] == 7570.0


def test_gex_history_series_reads_recent_postgres_snapshots(monkeypatch) -> None:
    snapshots = []
    for captured_at, spot, strike, strength, capture_mode in [
        (datetime(2026, 5, 26, 12, 0, tzinfo=UTC), 7538.0, 7550.0, 1.0, "request"),
        (datetime(2026, 5, 26, 13, 0, tzinfo=UTC), 7541.0, 7560.0, 1.0, "live"),
    ]:
        payload = {
            "ok": True,
            "provider_symbol": "ES",
            "captured_at": captured_at.isoformat(),
            "capture_mode": capture_mode,
            "global_gamma_regime": "POSITIVE_ESTIMATE",
            "request_meta": {
                "strike_count": 21 if capture_mode == "request" else 9,
                "futures_options": True,
            },
            "spot": spot,
            "levels": [
                _canonical_gex_level(
                    strike,
                    spot=spot,
                    strength=strength,
                    option_volume_context={
                        "current": {
                            "call_volume": 100.0,
                            "put_volume": 80.0,
                            "total_volume": 180.0,
                            "call_oi": 200.0,
                            "put_oi": 220.0,
                            "total_oi": 420.0,
                            "turnover": 0.4286,
                            "rank": 1,
                        },
                        "event": (
                            {
                                "call_volume_delta": 7.0,
                                "put_volume_delta": 3.0,
                                "total_volume_delta": 10.0,
                                "bias": 0.4,
                                "call_participation": 0.7,
                                "put_participation": 0.3,
                                "turnover": 0.0238,
                                "acceleration": 2.0,
                                "flow_per_point": 20.0,
                                "spot_move_points": 0.5,
                                "material": True,
                                "interaction": "price_near_level",
                                "cross_direction": "",
                                "state": "accelerating",
                                "window_seconds": 15.0,
                                "source": "broker_volume_delta",
                            }
                            if capture_mode == "live"
                            else None
                        ),
                    },
                    motion=(
                        {
                            "call": {
                                "type": "strengthening",
                                "side": "call",
                                "from": 10.0,
                                "to": 20.0,
                                "delta": 10.0,
                                "delta_pct": 1.0,
                                "window_seconds": 30.0,
                            }
                        }
                        if capture_mode == "live"
                        else None
                    ),
                )
            ],
        }
        snapshots.append(
            _gex_snapshot(
                _ES,
                captured_at,
                payload,
                source=("gex:ibkr" if capture_mode == "request" else "gex:ibkr-live"),
                expiry="20260529",
            )
        )
    _install_fake_gex_store(monkeypatch, snapshots)

    result = gex.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=4,
        hours=48,
        now=datetime(2026, 5, 26, 14, 0, tzinfo=UTC),
    )

    assert [row["spot"] for row in result] == [7538.0, 7541.0]
    assert [row["gamma_flip"] for row in result] == [None, None]
    assert all(level["kind"] != "GAMMA_FLIP" for row in result for level in row["levels"])
    assert result[0]["levels"][0]["price"] == 7550.0
    assert result[1]["levels"][0]["strength"] == 1.0
    assert result[0]["levels"][0]["option_volume_context"]["event"] is None
    assert result[1]["levels"][0]["option_volume_context"]["event"]["total_volume_delta"] == 10.0
    assert set(result[1]["levels"][0]["option_volume_context"]) == {"current", "event"}
    assert result[1]["levels"][0]["motion"]["call"]["type"] == "strengthening"
    assert result[0]["valid_until"] == "2026-05-26T13:00:00+00:00"
    assert result[1]["valid_until"] == "2026-05-26T14:00:00+00:00"
    assert [row["duration_minutes"] for row in result] == [60.0, 60.0]
    assert [row["capture_mode"] for row in result] == ["request", "live"]
    assert [row["capture_revision"] for row in result] == [
        "2026-05-26T12:00:00+00:00",
        "2026-05-26T13:00:00+00:00",
    ]

    compact_rows = FakeGexStore(snapshots).read_gex_snapshots(
        _instrument_id(_ES),
        _route_key(_ES),
        datetime(2026, 5, 25, tzinfo=UTC),
        datetime(2026, 5, 26, 14, 0, tzinfo=UTC),
        sources=("gex:ibkr", "gex:ibkr-live"),
        include_raw=False,
    )
    unchanged = deepcopy(compact_rows)
    level_admissions: list[int] = []
    require_levels = gex_payload_contract.require_gex_levels

    def admit_levels(levels, *, max_levels, spot):
        level_admissions.append(len(levels))
        return require_levels(levels, max_levels=max_levels, spot=spot)

    monkeypatch.setattr(gex_snapshot_validation, "require_gex_levels", admit_levels)
    monkeypatch.setattr(gex_history, "require_gex_levels", admit_levels)
    replay = gex_history.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=4,
        hours=48,
        now=datetime(2026, 5, 26, 14, 0, tzinfo=UTC),
        preloaded_rows=compact_rows,
    )
    assert replay == result
    assert compact_rows == unchanged
    assert level_admissions == [len(row["payload"]["levels"]) for row in compact_rows]
    replay[0]["levels"][0]["option_volume_context"]["current"]["call_volume"] = -1
    assert compact_rows == unchanged

    malformed = deepcopy(compact_rows[0]["payload"])
    malformed["levels"].append(
        {**malformed["levels"][0], "price": 8000.0, "selection_rank": 2, "abs_gex": -1.0}
    )
    with pytest.raises(ValueError, match="internally inconsistent"):
        gex_snapshot_validation.require_gex_snapshot_payload(
            malformed,
            projection="history",
            max_levels=1,
        )


@pytest.mark.parametrize(
    ("hours", "expected_limit"),
    (
        (168, 4_036),
        (24 * 30, 17_284),
    ),
)
def test_gex_history_combined_read_budget_covers_each_source_lane(
    hours: int,
    expected_limit: int,
) -> None:
    observed: dict[str, Any] = {}

    class Store:
        def initialize(self) -> None:
            return None

        def read_gex_snapshots(self, *_args, **kwargs) -> list[dict[str, Any]]:
            observed.update(kwargs)
            return []

    end = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    start = end - timedelta(hours=hours)

    rows = gex_history._read_snapshot_history_from_db(
        "ES",
        start,
        end,
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=21,
        store=Store(),
    )

    assert rows == []
    assert observed["sources"] == ("gex:ibkr", "gex:ibkr-live")
    assert observed["limit"] == expected_limit


def test_gex_history_storage_shape_and_preloaded_identity_fail_closed() -> None:
    start = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)

    class MalformedStore:
        def initialize(self) -> None:
            return None

        def read_gex_snapshots(self, *_args, **_kwargs):
            return ()

    with pytest.raises(
        ValueError,
        match="GEX_HISTORY_CONTRACT_INVALID: storage rows must be a typed list",
    ):
        gex_history.read_gex_snapshot_rows(
            start,
            end,
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            limit=10,
            store=MalformedStore(),
        )

    mismatched_row = _gex_snapshot(
        _ES,
        start,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "request_meta": {
                "strike_count": 21,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260724",
    )
    mismatched_row["instrument_id"] = "ibkr:conid:999999"
    with pytest.raises(
        ValueError,
        match="GEX_HISTORY_CONTRACT_INVALID: preloaded row identity disagrees",
    ):
        gex_history._read_snapshot_history_from_db(
            "ES",
            start,
            end,
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            max_levels=21,
            preloaded_rows=[mismatched_row],
        )

    with pytest.raises(ValueError, match="bounds must be timezone-aware"):
        gex_history.read_gex_snapshot_rows(
            start.replace(tzinfo=None),
            end,
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            limit=10,
            store=MalformedStore(),
        )


def test_gex_history_intervals_advance_only_within_the_comparison_lane(
    monkeypatch,
) -> None:
    start = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    snapshots: list[dict[str, Any]] = []
    for minute, mode, strike_count, source in (
        (0, "request", 21, "gex:ibkr"),
        (5, "live", 9, "gex:ibkr-live"),
        (10, "request", 21, "gex:ibkr"),
        (15, "live", 9, "gex:ibkr-live"),
    ):
        captured_at = start + timedelta(minutes=minute)
        snapshots.append(
            _gex_snapshot(
                _ES,
                captured_at,
                {
                    "provider_symbol": "ES",
                    "capture_mode": mode,
                    "spot": 5000.0,
                    "global_gamma_regime": "POSITIVE_ESTIMATE",
                    "request_meta": {
                        "refresh_mode": mode,
                        "strike_count": strike_count,
                        "futures_options": True,
                    },
                    "levels": [_canonical_gex_level(5000.0)],
                },
                source=source,
                expiry="20260724",
            )
        )
    store = _install_fake_gex_store(monkeypatch, snapshots)

    history = gex.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=21,
        hours=1,
        now=start + timedelta(minutes=20),
        store=store,
    )

    assert [row["capture_mode"] for row in history] == [
        "request",
        "live",
        "request",
        "live",
    ]
    assert [row["duration_minutes"] for row in history] == [10.0, 10.0, 10.0, 5.0]
    assert history[0]["valid_until"] == history[2]["captured_at"]
    assert history[1]["valid_until"] == history[3]["captured_at"]


def test_request_and_live_snapshots_coexist_at_the_same_timestamp() -> None:
    captured_at = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    store = FakeGexStore([])
    for mode, strike_count in (("request", 21), ("live", 9)):
        source = "gex:ibkr-live" if mode == "live" else "gex:ibkr"
        snapshot_payload = _gex_snapshot(
            _ES,
            captured_at,
            {
                "capture_mode": mode,
                "spot": 5000.0,
                "request_meta": {
                    "refresh_mode": mode,
                    "strike_count": strike_count,
                    "futures_options": True,
                },
                "levels": [_canonical_gex_level(5000.0)],
                **(
                    {
                        "live": {
                            "publishable": True,
                            "session_id": captured_at.isoformat(),
                            "started_at": captured_at.isoformat(),
                        }
                    }
                    if mode == "live"
                    else {}
                ),
            },
            source=source,
            expiry=_ACTIVE_OPTION_EXPIRY,
        )["payload"]
        assert gex.persist_gex_snapshot(
            "ES",
            snapshot_payload["raw"]["meta"],
            snapshot_payload["raw"]["strikes"],
            snapshot_payload,
            snapshot_payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=store,
            captured_at=captured_at,
            capture_mode=mode,
        )

    assert [row["source"] for row in store.snapshots] == [
        "gex:ibkr",
        "gex:ibkr-live",
    ]


def test_gex_history_activity_is_scope_stable_time_normalized_and_resets_after_gaps(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)
    base = now - timedelta(minutes=50)
    samples = (
        (0, "20991230", 100.0, 50.0),
        (5, "20991230", 110.0, 55.0),
        (10, "20991231", 140.0, 70.0),
        (15, "20991231", 150.0, 75.0),
        (35, "20991231", 170.0, 85.0),
        (40, "20991231", 180.0, 90.0),
    )
    store = _install_fake_gex_store(monkeypatch, [])
    for minute_offset, expiry, call_volume, put_volume in samples:
        captured_at = base + timedelta(minutes=minute_offset)
        request_meta = {
            "refresh_mode": "request",
            "strike_count": 21,
            "futures_options": True,
        }
        strike_rows = [
            {
                "strike": 5000.0,
                "call_volume": call_volume,
                "put_volume": put_volume,
            }
        ]
        payload = _gex_snapshot(
            _ES,
            captured_at,
            {
                "capture_mode": "request",
                "spot": 5000.0,
                "request_meta": request_meta,
                "levels": [
                    _canonical_gex_level(
                        5000.0,
                        call_gex=1_200_000.0,
                        put_gex=-200_000.0,
                    )
                ],
                "raw": {"strikes": strike_rows},
            },
            source="gex:ibkr",
            expiry=expiry,
        )["payload"]
        assert gex.persist_gex_snapshot(
            "ES",
            payload["raw"]["meta"],
            payload["raw"]["strikes"],
            payload,
            payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=store,
            captured_at=captured_at,
            capture_mode="request",
        )

    history = gex.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=21,
        hours=1,
        now=now,
        store=store,
    )
    assert len(history) == 6
    assert history[0]["option_activity"] is None
    assert history[0]["option_activity_status"] == "NEED_PREVIOUS"
    first_activity = history[1]["option_activity"]
    assert first_activity == {
        "status": "BASELINING",
        "scope": "selected_chain",
        "elapsed_seconds": 300.0,
        "contract_universe": {
            "selected_strikes": 21,
            "selected_contracts": 42,
            "matched_contracts": 42,
        },
        "call": {
            "volume_delta": 10.0,
            "rate_per_minute": 2.0,
            "rvol": None,
            "baseline_samples": 0,
        },
        "put": {
            "volume_delta": 5.0,
            "rate_per_minute": 1.0,
            "rvol": None,
            "baseline_samples": 0,
        },
        "total": {
            "volume_delta": 15.0,
            "rate_per_minute": 3.0,
            "rvol": None,
            "baseline_samples": 0,
        },
    }
    assert history[1]["comparison_scope"] == {
        "capture_mode": "request",
        "strike_count": 21,
        "strike_ladder": [5000.0 + index for index in range(21)],
        "contract_con_ids": sorted(
            con_id
            for strike in (5000.0 + index for index in range(21))
            for con_id in (
                int(round(strike * 100)) * 2 + 1,
                int(round(strike * 100)) * 2 + 2,
            )
        ),
        "expiries": ["20991230"],
        "futures_options": True,
        "series": [
            {
                "expiry": "20991230",
                "trading_class": "ES",
                "exchange": "CME",
                "multiplier": 50.0,
            }
        ],
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "market_data_entitlement": "live",
    }
    assert history[1]["instrument_id"] == _instrument_id(_ES)
    assert history[1]["route_fingerprint"] == _route_key(_ES)
    assert history[1]["global_gamma_regime"] == "POSITIVE_ESTIMATE"
    assert history[1]["option_activity_status"] == "BASELINING"
    assert history[2]["option_activity"] is None
    assert history[2]["option_activity_status"] == "SCOPE_CHANGED"
    assert history[3]["option_activity"]["total"]["baseline_samples"] == 0
    assert history[4]["option_activity"] is None
    assert history[4]["option_activity_status"] == "GAP"
    assert history[5]["option_activity"]["total"]["baseline_samples"] == 0

    option_flow = option_flow_context_from_gex_history(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        history=history,
        analysis_as_of_utc=now,
    )
    assert option_flow is None


def test_gex_history_activity_uses_one_canonical_fractional_elapsed_value() -> None:
    previous_at = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    current_at = previous_at + timedelta(
        minutes=5,
        microseconds=123_456,
    )
    request_meta = {
        "refresh_mode": "request",
        "strike_count": 21,
        "futures_options": True,
    }

    def payload_at(
        captured_at: datetime,
        *,
        call_volume: float,
        put_volume: float,
    ) -> dict[str, Any]:
        return _gex_snapshot(
            _ES,
            captured_at,
            {
                "capture_mode": "request",
                "spot": 5000.0,
                "request_meta": request_meta,
                "levels": [_canonical_gex_level(5000.0)],
                "raw": {
                    "strikes": [
                        {
                            "strike": 5000.0,
                            "call_volume": call_volume,
                            "put_volume": put_volume,
                        }
                    ]
                },
            },
            source="gex:ibkr",
            expiry="20260721",
        )["payload"]

    previous = {
        "instrument_id": _instrument_id(_ES),
        "route_fingerprint": _route_key(_ES),
        "provider_symbol": "ES",
        "captured_at": previous_at.isoformat(),
        "source": "gex:ibkr",
        "payload": payload_at(
            previous_at,
            call_volume=100.0,
            put_volume=50.0,
        ),
    }
    activity, status = gex_history._gex_option_activity_for_persistence(
        payload_at(
            current_at,
            call_volume=100_100.0,
            put_volume=50_050.0,
        ),
        current_at,
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        store=FakeGexStore([previous]),
        snapshot_source="gex:ibkr",
        predecessor_before=current_at,
    )

    assert status == "BASELINING"
    assert activity is not None
    assert activity["elapsed_seconds"] == 300.123456
    assert require_gex_option_activity(activity) == activity


@pytest.mark.parametrize(
    ("include_prior", "expected_status", "expected_snapshot_count"),
    (
        (True, "BASELINING", 2),
        (False, "NEED_PREVIOUS", 1),
    ),
)
def test_live_gex_activity_excludes_current_bucket_generation_loser(
    monkeypatch,
    include_prior: bool,
    expected_status: str,
    expected_snapshot_count: int,
) -> None:
    current_bucket = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)
    prior_bucket = current_bucket - timedelta(minutes=5)
    frames = []
    if include_prior:
        frames.append(
            (
                current_bucket - timedelta(minutes=3),
                current_bucket - timedelta(minutes=4),
                prior_bucket,
                100.0,
                50.0,
            )
        )
    frames.extend(
        (
            (
                current_bucket + timedelta(minutes=1),
                current_bucket + timedelta(seconds=30),
                current_bucket,
                110.0,
                55.0,
            ),
            (
                current_bucket + timedelta(minutes=3),
                current_bucket + timedelta(minutes=2),
                current_bucket,
                130.0,
                65.0,
            ),
        )
    )
    store = _install_fake_gex_store(monkeypatch, [])

    for captured_at, started_at, bucket_at, call_volume, put_volume in frames:
        request_meta = {
            "refresh_mode": "live",
            "strike_count": 21,
            "futures_options": True,
        }
        strike_rows = [
            {
                "strike": 5000.0,
                "call_volume": call_volume,
                "put_volume": put_volume,
            }
        ]
        payload = _gex_snapshot(
            _ES,
            captured_at,
            {
                "capture_mode": "live",
                "spot": 5000.0,
                "request_meta": request_meta,
                "levels": [
                    _canonical_gex_level(
                        5000.0,
                        call_gex=1_200_000.0,
                        put_gex=-200_000.0,
                    )
                ],
                "raw": {"strikes": strike_rows},
                "live": {
                    "publishable": True,
                    "session_id": started_at.isoformat(),
                    "started_at": started_at.isoformat(),
                },
            },
            source="gex:ibkr-live",
            expiry=_ACTIVE_OPTION_EXPIRY,
        )["payload"]
        assert gex.persist_gex_snapshot(
            "ES",
            payload["raw"]["meta"],
            payload["raw"]["strikes"],
            payload,
            payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=store,
            captured_at=captured_at,
            capture_mode="live",
            live_bucket_at=bucket_at,
        )

    assert len(store.snapshots) == expected_snapshot_count
    winner = store.snapshots[-1]["payload"]
    assert winner["captured_at"] == (current_bucket + timedelta(minutes=3)).isoformat()
    assert winner["option_activity_status"] == expected_status
    if include_prior:
        assert winner["option_activity"]["elapsed_seconds"] == 360.0
        assert winner["option_activity"]["call"]["volume_delta"] == 30.0
        assert winner["option_activity"]["put"]["volume_delta"] == 15.0
    else:
        assert winner["option_activity"] is None


def test_option_flow_requires_fresh_ready_activity_baseline(monkeypatch) -> None:
    latest_at = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    now = latest_at + timedelta(minutes=1)
    base = latest_at - timedelta(minutes=20)
    store = _install_fake_gex_store(monkeypatch, [])
    for index in range(5):
        captured_at = base + timedelta(minutes=index * 5)
        request_meta = {
            "refresh_mode": "request",
            "strike_count": 21,
            "futures_options": True,
        }
        call_volume = 100.0 + index * 10.0
        put_volume = 50.0 + index * 5.0
        strike_rows = [
            {
                "strike": 5000.0,
                "call_volume": call_volume,
                "put_volume": put_volume,
            }
        ]
        payload = _gex_snapshot(
            _ES,
            captured_at,
            {
                "capture_mode": "request",
                "spot": 5000.0,
                "request_meta": request_meta,
                "levels": [
                    _canonical_gex_level(
                        5000.0,
                        call_gex=1_200_000.0,
                        put_gex=-200_000.0,
                    )
                ],
                "raw": {"strikes": strike_rows},
            },
            source="gex:ibkr",
            expiry=_ACTIVE_OPTION_EXPIRY,
        )["payload"]
        assert gex.persist_gex_snapshot(
            "ES",
            payload["raw"]["meta"],
            payload["raw"]["strikes"],
            payload,
            payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=store,
            captured_at=captured_at,
            capture_mode="request",
        )

    history = gex.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=21,
        hours=1,
        now=now,
        store=store,
    )
    long_history = gex.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=21,
        hours=168,
        now=now,
        store=store,
    )
    option_flow = option_flow_context_from_gex_history(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        history=history,
        analysis_as_of_utc=now,
    )

    assert [row["option_activity_status"] for row in history] == [
        "NEED_PREVIOUS",
        "BASELINING",
        "BASELINING",
        "BASELINING",
        "READY",
    ]
    assert history[-1]["option_activity"]["total"] == {
        "volume_delta": 15.0,
        "rate_per_minute": 3.0,
        "rvol": 1.0,
        "baseline_samples": 3,
    }
    assert long_history[-1]["option_activity"] == history[-1]["option_activity"]
    assert option_flow == {
        "call_volume_delta": 10.0,
        "put_volume_delta": 5.0,
        "call_rate_per_minute": 2.0,
        "put_rate_per_minute": 1.0,
        "option_rvol": 1.0,
        "baseline_samples": 3,
        "elapsed_seconds": 300.0,
        "spot": 5000.0,
        "source": "gex:selected-chain-volume-rate",
        "captured_at": latest_at.isoformat(),
        "provider_symbol": "ES",
        "capture_mode": "request",
        "comparison_scope": history[-1]["comparison_scope"],
        "quality": "READY",
        "decision_eligible": True,
    }


def test_option_flow_uses_latest_request_lane_instead_of_newer_live_lane(monkeypatch) -> None:
    now = datetime.now(tz=UTC).replace(second=0, microsecond=0)

    def payload(
        captured_at: datetime, *, mode: str, strike_count: int, call_rate: float
    ) -> dict[str, Any]:
        return {
            "captured_at": captured_at.isoformat(),
            "capture_mode": mode,
            "spot": 5000.0,
            "global_gamma_regime": "POSITIVE_ESTIMATE",
            "request_meta": {
                "refresh_mode": mode,
                "strike_count": strike_count,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
            "option_activity_status": "READY",
            "option_activity": {
                "status": "READY",
                "scope": "selected_chain",
                "elapsed_seconds": 300.0,
                "contract_universe": {
                    "selected_strikes": strike_count,
                    "selected_contracts": strike_count * 2,
                    "matched_contracts": strike_count * 2,
                },
                "call": {
                    "volume_delta": call_rate * 5,
                    "rate_per_minute": call_rate,
                    "rvol": 1.0,
                    "baseline_samples": 3,
                },
                "put": {
                    "volume_delta": 5.0,
                    "rate_per_minute": 1.0,
                    "rvol": 1.0,
                    "baseline_samples": 3,
                },
                "total": {
                    "volume_delta": call_rate * 5 + 5.0,
                    "rate_per_minute": call_rate + 1.0,
                    "rvol": 1.0,
                    "baseline_samples": 3,
                },
            },
        }

    request_at = now - timedelta(minutes=5)
    rows = [
        _gex_snapshot(
            _ES,
            request_at,
            payload(request_at, mode="request", strike_count=21, call_rate=2.0),
            source="gex:ibkr",
            expiry="20260721",
        ),
        _gex_snapshot(
            _ES,
            now,
            payload(now, mode="live", strike_count=9, call_rate=99.0),
            source="gex:ibkr-live",
            expiry="20260721",
        ),
    ]
    store = _install_fake_gex_store(monkeypatch, rows)
    request_history = gex.get_gex_history_series(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        max_levels=21,
        hours=1,
        now=now,
        store=store,
        sources=(GEX_REQUEST_SNAPSHOT_SOURCE,),
    )

    option_flow = option_flow_context_from_gex_history(
        "ES",
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        history=request_history,
        analysis_as_of_utc=now,
    )

    assert option_flow is not None
    assert option_flow["capture_mode"] == "request"
    assert option_flow["captured_at"] == request_at.isoformat()
    assert option_flow["call_rate_per_minute"] == 2.0
    assert option_flow["comparison_scope"]["strike_count"] == 21


def test_option_flow_rejects_fresh_frame_after_exact_option_expiry() -> None:
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=1)
    payload = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "request_meta": {
                "refresh_mode": "request",
                "strike_count": 1,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
            "option_activity_status": "READY",
            "option_activity": {
                "status": "READY",
                "scope": "selected_chain",
                "elapsed_seconds": 300.0,
                "contract_universe": {
                    "selected_strikes": 1,
                    "selected_contracts": 2,
                    "matched_contracts": 2,
                },
                "call": {
                    "volume_delta": 10.0,
                    "rate_per_minute": 2.0,
                    "rvol": 1.0,
                    "baseline_samples": 3,
                },
                "put": {
                    "volume_delta": 5.0,
                    "rate_per_minute": 1.0,
                    "rvol": 1.0,
                    "baseline_samples": 3,
                },
                "total": {
                    "volume_delta": 15.0,
                    "rate_per_minute": 3.0,
                    "rvol": 1.0,
                    "baseline_samples": 3,
                },
            },
        },
        source="gex:ibkr",
        expiry=_ACTIVE_OPTION_EXPIRY,
    )["payload"]
    payload["option_universe_expires_at"] = (
        datetime.now(tz=UTC) - timedelta(seconds=1)
    ).isoformat()
    assert (
        option_flow_context_from_gex_history(
            "ES",
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            history=[payload],
            analysis_as_of_utc=captured_at + timedelta(minutes=1),
        )
        is None
    )


def test_gex_history_interval_ends_at_exact_option_universe_expiry() -> None:
    captured_at = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
    expires_at = captured_at + timedelta(minutes=2)
    payload = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "request_meta": {
                "refresh_mode": "request",
                "strike_count": 1,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry=_ACTIVE_OPTION_EXPIRY,
    )["payload"]
    payload["option_universe_expires_at"] = expires_at.isoformat()

    interval = gex_history.attach_gex_history_intervals(
        [payload],
        captured_at + timedelta(minutes=10),
    )[0]

    assert interval["valid_until"] == expires_at.isoformat()
    assert interval["valid_until_unix_ms"] == int(expires_at.timestamp() * 1000)


def test_gex_history_rejects_noncanonical_old_level_rows(monkeypatch) -> None:
    captured_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    snapshot = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 7500.0,
            "levels": [_canonical_gex_level(7500.0)],
        },
        source="gex:ibkr",
        expiry="20260529",
    )
    snapshot["payload"]["levels"] = [
        {
            "price": 7500.0,
            "strike": 7500.0,
            "kind": "CALL_WALL",
            "strength": 1.0,
            "net_gex": 1_000_000.0,
        }
    ]
    _install_fake_gex_store(monkeypatch, [snapshot])

    with pytest.raises(ValueError, match="GEX_HISTORY_CONTRACT_INVALID"):
        gex.get_gex_history_series(
            "ES",
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            max_levels=4,
            hours=48,
            now=captured_at + timedelta(hours=1),
        )


def test_gex_history_rejects_missing_current_option_volume_context(monkeypatch) -> None:
    captured_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    snapshot = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 7500.0,
            "levels": [_canonical_gex_level(7500.0)],
        },
        source="gex:ibkr",
        expiry="20260529",
    )
    del snapshot["payload"]["levels"][0]["option_volume_context"]
    _install_fake_gex_store(monkeypatch, [snapshot])

    with pytest.raises(ValueError, match="GEX_HISTORY_CONTRACT_INVALID"):
        gex.get_gex_history_series(
            "ES",
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            max_levels=4,
            hours=48,
            now=captured_at + timedelta(hours=1),
        )


@pytest.mark.parametrize(
    "missing_field",
    ("option_activity", "option_activity_status"),
)
def test_gex_history_rejects_missing_option_activity_pair(
    missing_field: str,
) -> None:
    captured_at = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    snapshot = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 7500.0,
            "levels": [_canonical_gex_level(7500.0)],
        },
        source="gex:ibkr",
        expiry="20260529",
    )
    del snapshot["payload"][missing_field]

    with pytest.raises(
        ValueError,
        match="GEX_HISTORY_CONTRACT_INVALID: GEX snapshot payload is missing canonical fields",
    ):
        gex_history._read_snapshot_history_from_db(
            "ES",
            captured_at - timedelta(hours=1),
            captured_at + timedelta(hours=1),
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            max_levels=4,
            preloaded_rows=[snapshot],
        )


def test_gex_activity_warmup_resets_at_new_york_midnight() -> None:
    previous_at = datetime(2026, 7, 15, 3, 55, tzinfo=UTC)
    current_at = datetime(2026, 7, 15, 4, 5, tzinfo=UTC)
    previous = _gex_snapshot(
        _ES,
        previous_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 5000.0,
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260717",
    )
    current_payload = _gex_snapshot(
        _ES,
        current_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 5000.0,
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260717",
    )["payload"]

    class RecordingStore(FakeGexStore):
        read_start: datetime | None = None

        def read_gex_snapshots(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            self.read_start = args[2]
            return super().read_gex_snapshots(*args, **kwargs)

    store = RecordingStore([previous])

    activity, status = gex_history._gex_option_activity_for_persistence(
        current_payload,
        current_at,
        instrument_id=_instrument_id(_ES),
        route_fingerprint=_route_key(_ES),
        store=store,
        snapshot_source="gex:ibkr",
        predecessor_before=current_at,
    )

    assert store.read_start == datetime(2026, 7, 15, 4, 0, tzinfo=UTC)
    assert activity is None
    assert status == "NEED_PREVIOUS"


def test_gex_activity_warmup_rejects_missing_persisted_activity_pair() -> None:
    previous_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    current_at = previous_at + timedelta(minutes=5)
    previous = _gex_snapshot(
        _ES,
        previous_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 5000.0,
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260717",
    )
    del previous["payload"]["option_activity_status"]
    current_payload = _gex_snapshot(
        _ES,
        current_at,
        {
            "capture_mode": "request",
            "request_meta": {"strike_count": 21, "futures_options": True},
            "spot": 5000.0,
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260717",
    )["payload"]

    with pytest.raises(
        ValueError,
        match="GEX_HISTORY_CONTRACT_INVALID: GEX snapshot payload is missing canonical fields",
    ):
        gex_history._gex_option_activity_for_persistence(
            current_payload,
            current_at,
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=FakeGexStore([previous]),
            snapshot_source="gex:ibkr",
            predecessor_before=current_at,
        )


def test_gex_activity_warmup_rejects_row_payload_provenance_drift() -> None:
    previous_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    current_at = previous_at + timedelta(minutes=5)
    previous_strikes = [{"strike": 5000.0, "call_volume": 100.0, "put_volume": 50.0}]
    previous = _gex_snapshot(
        _ES,
        previous_at,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "global_gamma_regime": "POSITIVE_ESTIMATE",
            "request_meta": {
                "strike_count": 21,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
            "raw": {
                "strikes": previous_strikes,
                "contracts": _activity_contract_rows(
                    previous_strikes,
                    expiry="20260717",
                ),
            },
        },
        source="gex:ibkr",
        expiry="20260717",
    )
    previous["payload"]["captured_at"] = (previous_at + timedelta(seconds=1)).isoformat()
    current_strikes = [{"strike": 5000.0, "call_volume": 110.0, "put_volume": 55.0}]
    current_payload = _gex_snapshot(
        _ES,
        current_at,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "request_meta": {"strike_count": 21, "futures_options": True},
            "levels": [_canonical_gex_level(5000.0)],
            "raw": {"strikes": current_strikes},
        },
        source="gex:ibkr",
        expiry="20260717",
    )["payload"]

    with pytest.raises(
        ValueError,
        match="GEX_HISTORY_CONTRACT_INVALID: warmup row and payload provenance disagree",
    ):
        gex.persist_gex_snapshot(
            "ES",
            current_payload["raw"]["meta"],
            current_payload["raw"]["strikes"],
            current_payload,
            current_payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=FakeGexStore([previous]),
            captured_at=current_at,
            capture_mode="request",
        )


def test_live_gex_refresh_creates_event_loop_in_worker_thread(monkeypatch, tmp_path) -> None:
    class FakeIb:
        def reqMarketDataType(self, _data_type):
            asyncio.get_event_loop()

        def isConnected(self):
            return True

    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: SimpleNamespace(
            conId=416904,
            symbol="SPX",
            secType="IND",
            localSymbol="SPX",
            lastTradeDateOrContractMonth="",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (
            [
                _ibkr_option(
                    con_id=1,
                    symbol="SPX",
                    sec_type="OPT",
                    exchange="CBOE",
                    multiplier="100",
                    strike=5000.0,
                    right="C",
                    expiry=_ACTIVE_OPTION_EXPIRY,
                    expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
                ),
                _ibkr_option(
                    con_id=2,
                    symbol="SPX",
                    sec_type="OPT",
                    exchange="CBOE",
                    multiplier="100",
                    strike=5000.0,
                    right="P",
                    expiry=_ACTIVE_OPTION_EXPIRY,
                    expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
                ),
            ],
            {
                "qualified_contracts": 2,
                "qualified_strike_count": 1,
                "qualified_pair_count": 1,
            },
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_collect_option_rows",
        lambda *_args, **_kwargs: [
            {**row, "_ibkr_market_data_type": 1}
            for row in _with_gex_test_contract_observations(
                [
                    {
                        "con_id": 1,
                        "expiry": _ACTIVE_OPTION_EXPIRY,
                        "trading_class": "SPX",
                        "exchange": "CBOE",
                        "multiplier": 100.0,
                        "strike": 5000.0,
                        "right": "C",
                        "gamma": 0.01,
                        "gex": 2_500_000.0,
                        "abs_gex": 2_500_000.0,
                        "open_interest": 10,
                        "volume": 1,
                        "greek_source": "modelGreeks",
                    },
                    {
                        "con_id": 2,
                        "expiry": _ACTIVE_OPTION_EXPIRY,
                        "trading_class": "SPX",
                        "exchange": "CBOE",
                        "multiplier": 100.0,
                        "strike": 5000.0,
                        "right": "P",
                        "gamma": 0.01,
                        "gex": -1_500_000.0,
                        "abs_gex": 1_500_000.0,
                        "open_interest": 6,
                        "volume": 1,
                        "greek_source": "modelGreeks",
                    },
                ],
                observed_at=datetime.now(tz=UTC),
            )
        ],
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            _collect_through_gex_lane,
            underlying_quote=_gex_test_spot_observation(
                5000.0,
                observed_at=datetime.now(tz=UTC),
            ),
            request=_gex_test_request(
                _SPX,
                strike_count=1,
                max_contracts=4,
            ),
            instrument=_SPX,
        ).result(timeout=5)

    assert result["ok"] is True
    assert result["source"] == "gex:ibkr"


def test_gex_refresh_without_canonical_quote_spot_does_not_open_broker_session(monkeypatch) -> None:
    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker session must not open")
        ),
    )

    with pytest.raises(
        GexUnderlyingQuoteUnavailableError,
        match="canonical underlying quote cache has no exact price observation",
    ):
        require_gex_acquisition_spot_observation(
            None,
            observed_at=datetime.now(tz=UTC),
        )


def test_gex_refresh_with_stale_quote_does_not_open_broker_session(monkeypatch) -> None:
    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: (_ for _ in ()).throw(
            AssertionError("broker session must not open")
        ),
    )
    stale_at = datetime.now(tz=UTC) - timedelta(seconds=36)

    with pytest.raises(
        GexUnderlyingQuoteUnavailableError,
        match="fresh exact underlying",
    ) as exc_info:
        require_gex_acquisition_spot_observation(
            _gex_test_spot_observation(
                5000.0,
                observed_at=stale_at,
            ),
            observed_at=datetime.now(tz=UTC),
        )

    assert exc_info.value.reason == "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
    assert exc_info.value.diagnostics["underlying_spot_age_seconds"] >= 36.0


def test_gex_spot_preflight_accepts_observation_inside_35_second_bound() -> None:
    observed_at = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)

    observation = require_gex_acquisition_spot_observation(
        _gex_test_spot_observation(
            5000.0,
            observed_at=observed_at - timedelta(seconds=34.9),
        ),
        observed_at=observed_at,
    )

    assert observation["price"] == 5000.0


def test_async_gex_refresh_runs_through_market_data_manager(monkeypatch) -> None:
    calls: list[str] = []

    def fake_collect(provider_symbol: str, **kwargs):
        calls.append(provider_symbol)
        return {
            "ok": True,
            "provider_symbol": provider_symbol,
            "source": "gex:ibkr",
            "levels": [{"price": 5000.0}],
            "history": [],
            "raw": {
                "meta": {
                    "option_universe_expires_at": (_ACTIVE_OPTION_EXPIRY_AT.isoformat()),
                },
                "strikes": [],
                "contracts": [],
            },
        }

    monkeypatch.setattr(gex_context_module, "_collect_live_gex_context_owned", fake_collect)
    monkeypatch.setattr(
        gex_context_module, "provider_schedule_open_state", lambda *_args, **_kwargs: True
    )

    result = asyncio.run(
        gex.async_gex_context(
            enabled=True,
            refresh=True,
            instrument=_SPY,
        )
    )

    assert result["ok"] is True
    assert result["provider_symbol"] == "SPY"
    assert calls == ["SPY"]


def test_gex_refresh_skips_options_outside_provider_trading_session(monkeypatch) -> None:
    session_requests: list[bool] = []

    def session_closed(_now, *, liquid, **_kwargs):
        session_requests.append(liquid)
        return False

    monkeypatch.setattr(gex_context_module, "provider_schedule_open_state", session_closed)
    monkeypatch.setattr(
        gex_context_module,
        "cached_gex_context",
        lambda *, instrument, **_kwargs: {
            "ok": True,
            "provider_symbol": instrument["provider_symbol"],
            "levels": [{"price": 5000.0}],
            "history": [],
        },
    )

    def blocked_collect(*_args, **_kwargs):
        raise AssertionError("outside-session SPX refresh must not open option subscriptions")

    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        blocked_collect,
    )

    result = asyncio.run(gex.async_gex_context(enabled=True, refresh=True, instrument=_SPX))

    assert result["ok"] is True
    assert result["status"] == "refresh_blocked"
    assert result["session_state"] == "closed"
    assert result["error"]["code"] == "GEX_REFRESH_SESSION_CLOSED"
    assert "without opening option subscriptions" in result["message"]
    assert session_requests == [False]


def test_gex_refresh_preserves_unknown_provider_session(monkeypatch) -> None:
    monkeypatch.setattr(
        gex_context_module,
        "provider_schedule_open_state",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        gex_context_module,
        "cached_gex_context",
        lambda *, instrument, **_kwargs: {
            "ok": False,
            "provider_symbol": instrument["provider_symbol"],
            "levels": [],
            "history": [],
        },
    )
    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unknown session must not open option subscriptions")
        ),
    )

    result = asyncio.run(gex.async_gex_context(enabled=True, refresh=True, instrument=_ES))

    assert result["ok"] is False
    assert result["status"] == "refresh_blocked"
    assert result["session_state"] == "unknown"
    assert result["error"]["code"] == "GEX_REFRESH_SESSION_UNKNOWN"
    assert "trading-session metadata is unknown" in result["message"]


@pytest.mark.parametrize(
    ("session_state", "expected_status", "expected_code", "message_fragment"),
    [
        (
            "closed",
            "session_closed",
            "GEX_LIVE_SESSION_CLOSED",
            "trading session is closed",
        ),
        (
            "unknown",
            "session_unknown",
            "GEX_LIVE_SESSION_UNKNOWN",
            "trading-session metadata is unknown",
        ),
    ],
)
def test_gex_live_session_gate_preserves_typed_state(
    monkeypatch,
    session_state: str,
    expected_status: str,
    expected_code: str,
    message_fragment: str,
) -> None:
    lane_labels: list[str] = []

    async def run_coroutine(_lane, label, coroutine_factory, **_kwargs):
        lane_labels.append(label)
        return await coroutine_factory()

    monkeypatch.setattr(
        gex_context_module,
        "gex_provider_session_state",
        lambda **_kwargs: session_state,
    )
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "run_coroutine",
        run_coroutine,
    )

    result = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_ES,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=None,
        )
    )

    assert result["status"] == expected_status
    assert result["session_state"] == session_state
    assert result["error"]["code"] == expected_code
    assert result["error"]["session_state"] == session_state
    assert message_fragment in result["message"]
    assert lane_labels == []


def test_gex_live_session_gate_stops_an_existing_live_owner(monkeypatch) -> None:
    lane_labels: list[str] = []

    async def run_coroutine(_lane, label, coroutine_factory, **_kwargs):
        lane_labels.append(label)
        return await coroutine_factory()

    monkeypatch.setattr(
        gex_context_module,
        "gex_provider_session_state",
        lambda **_kwargs: "unknown",
    )
    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "run_coroutine",
        run_coroutine,
    )
    gex._GEX_RUNTIME.acquire_capture_mode(
        _instrument_id(_ES),
        _route_key(_ES),
        "live",
        provider_symbol="ES",
    )

    result = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_ES,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=None,
        )
    )

    assert result["status"] == "session_unknown"
    assert len(lane_labels) == 1
    assert "live gex stop" in lane_labels[0]
    assert gex._GEX_RUNTIME.active_capture_mode(_instrument_id(_ES), _route_key(_ES)) is None


def test_gex_live_missing_startup_quote_is_typed_unavailable_without_backoff(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gex_context_module,
        "gex_provider_session_state",
        lambda **_kwargs: "open",
    )
    lane_calls: list[str] = []

    async def fail_if_called(_lane, label, _coroutine_factory, **_kwargs):
        lane_calls.append(label)
        raise AssertionError("missing underlying quote must not enter the options lane")

    monkeypatch.setattr(
        ibkr_gex_provider_runtime,
        "run_coroutine",
        fail_if_called,
    )

    result = asyncio.run(
        gex_live.async_live_gex_context(
            enabled=True,
            instrument=_ES,
            provider_runtime=ibkr_gex_provider_runtime,
            underlying_quote=None,
        )
    )

    assert result["status"] == "unavailable"
    assert result["error"]["code"] == "GEX_UNDERLYING_QUOTE_UNAVAILABLE"
    assert result["error"]["reason"] == "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
    assert lane_calls == []
    assert (
        gex._GEX_RUNTIME.failure_backoff(
            _instrument_id(_ES),
            _route_key(_ES),
            lane="live",
        )["active"]
        is False
    )


@pytest.mark.parametrize("instrument", [_ES, _SPX, _SPY, _QQQ])
def test_gex_refresh_uses_provider_trading_session_for_every_asset(
    monkeypatch, instrument: dict[str, Any]
) -> None:
    provider_symbol = instrument["provider_symbol"]
    session_requests: list[bool] = []
    calls: list[str] = []

    def session_open(_now, *, liquid, instrument, **_kwargs):
        session_requests.append(liquid)
        assert instrument["provider_symbol"] == provider_symbol
        return not liquid

    monkeypatch.setattr(gex_context_module, "provider_schedule_open_state", session_open)
    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        lambda _provider_symbol, *, instrument, **_kwargs: (
            calls.append(instrument["provider_symbol"])
            or {
                "ok": True,
                "provider_symbol": instrument["provider_symbol"],
                "levels": [],
                "history": [],
            }
        ),
    )

    monkeypatch.setattr(
        gex_context_module,
        "_finalize_gex_request_payload",
        lambda _provider_symbol, payload, *, instrument, **_kwargs: (
            gex_context_module._GexRequestCompletion(
                {
                    **payload,
                    "instrument_id": _instrument_id(instrument),
                    "route_fingerprint": _route_key(instrument),
                },
                datetime.now(tz=UTC) + timedelta(hours=1),
            )
        ),
    )
    monkeypatch.setattr(gex_context_module, "get_gex_history_series", lambda *_args, **_kwargs: [])

    result = asyncio.run(
        gex.async_gex_context(
            enabled=True,
            refresh=True,
            instrument=instrument,
        )
    )

    assert result["ok"] is True
    assert session_requests == [False]
    assert calls == [provider_symbol]


def test_concurrent_gex_requests_share_the_provider_manager_lane(
    monkeypatch,
) -> None:
    first_collecting = threading.Event()
    release_first = threading.Event()
    state_lock = threading.Lock()
    active_collections = 0
    max_active_collections = 0
    collection_count = 0
    connection_count = 0
    reset_count = 0

    class FakeIb:
        def reqMarketDataType(self, _data_type: int) -> None:
            return None

    def connected(_request, **_kwargs):
        nonlocal connection_count
        with state_lock:
            connection_count += 1
        return FakeIb()

    def collect_rows(*_args, **_kwargs):
        nonlocal active_collections, collection_count, max_active_collections
        with state_lock:
            collection_count += 1
            current_collection = collection_count
            active_collections += 1
            max_active_collections = max(
                max_active_collections,
                active_collections,
            )
        try:
            if current_collection == 1:
                first_collecting.set()
                assert release_first.wait(timeout=2.0)
            return [
                {
                    "strike": 7500.0,
                    "right": "C",
                    "gamma": 0.01,
                    "_ibkr_market_data_type": 1,
                }
            ]
        finally:
            with state_lock:
                active_collections -= 1

    def reset() -> None:
        nonlocal reset_count
        with state_lock:
            assert active_collections == 0
            reset_count += 1

    request = _gex_test_request(
        _ES,
        strike_count=1,
        max_contracts=4,
        timeout=5.0,
    )

    def collect_context(provider_symbol: str, **kwargs):
        acquisition = ibkr_gex.fetch_gex_option_rows(
            instrument=kwargs["instrument"],
            request=_ibkr_provider_request(kwargs["request"]),
            spot=7500.0,
            dividend_yield=0.0,
        )
        return {
            "ok": True,
            "provider_symbol": provider_symbol,
            "source": "gex:ibkr",
            "levels": [{"price": 7500.0}],
            "contracts_requested": acquisition.contracts_requested,
        }

    monkeypatch.setattr(ibkr_gex, "_connected_gex_ib", connected)
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: SimpleNamespace(
            conId=101,
            secType="FUT",
            symbol="ES",
            localSymbol="ESU6",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (
            [
                _ibkr_option(
                    con_id=1,
                    strike=7500.0,
                    right="C",
                    expiry=_ACTIVE_OPTION_EXPIRY,
                    expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
                )
            ],
            {"qualified_contracts": 1},
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_collect_option_rows_by_expiration",
        collect_rows,
    )
    monkeypatch.setattr(ibkr_gex, "_reset_gex_session", reset)
    monkeypatch.setattr(
        gex_context_module,
        "_collect_live_gex_context_owned",
        collect_context,
    )
    monkeypatch.setattr(
        gex_context_module,
        "_finalize_gex_request_payload",
        lambda _provider_symbol, payload, *, instrument, **_kwargs: (
            gex_context_module._GexRequestCompletion(
                {
                    **payload,
                    "instrument_id": _instrument_id(instrument),
                    "route_fingerprint": _route_key(instrument),
                },
                datetime.now(tz=UTC) + timedelta(hours=1),
            )
        ),
    )
    monkeypatch.setattr(gex_context_module, "get_gex_history_series", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        gex_context_module,
        "_dividend_yield_for_gex_request",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        gex_context_module,
        "provider_schedule_open_state",
        lambda *_args, **_kwargs: True,
    )

    async def run_manager_request() -> dict[str, Any]:
        task = asyncio.create_task(
            gex.async_gex_context(
                enabled=True,
                refresh=True,
                request=request,
                underlying_quote=_gex_test_spot_observation(
                    7500.0,
                    observed_at=datetime.now(tz=UTC),
                ),
                instrument=_ES,
            )
        )
        await asyncio.sleep(0.05)
        assert task.done() is False
        with state_lock:
            assert connection_count == 1
            assert max_active_collections == 1
        release_first.set()
        return await asyncio.wait_for(task, timeout=2.0)

    with ThreadPoolExecutor(max_workers=1) as executor:
        sync_future = executor.submit(
            _collect_through_gex_lane,
            request=request,
            underlying_quote=_gex_test_spot_observation(
                7500.0,
                observed_at=datetime.now(tz=UTC),
            ),
            instrument=_ES,
        )
        assert first_collecting.wait(timeout=1.0)
        async_result = asyncio.run(run_manager_request())
        sync_result = sync_future.result(timeout=2.0)

    assert sync_result["ok"] is True
    assert async_result["ok"] is True
    assert connection_count == 2
    assert reset_count == 2
    assert max_active_collections == 1


def test_es_gex_option_chain_uses_cme_futures_option_exchange() -> None:
    calls: list[tuple[str, str, str, int]] = []
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="ES",
        multiplier="50",
        expirations=["20260601"],
        strikes=[5000.0],
    )

    class FakeIb:
        def reqSecDefOptParams(self, symbol, exchange, sec_type, con_id):
            calls.append((symbol, exchange, sec_type, con_id))
            return [chain] if exchange == "CME" else []

    result = ibkr_gex._request_option_chains(
        FakeIb(),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId=123),
    )

    assert result == [chain]
    assert calls[0] == ("ES", "CME", "FUT", 123)


def test_gex_option_chain_reuses_secdef_cache_for_same_underlying() -> None:
    calls: list[tuple[str, str, str, int]] = []
    chain = SimpleNamespace(
        exchange="CME",
        tradingClass="ES",
        multiplier="50",
        expirations=["20260601"],
        strikes=[5000.0],
    )
    underlying = SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId=987654)

    class FakeIb:
        def reqSecDefOptParams(self, symbol, exchange, sec_type, con_id):
            calls.append((symbol, exchange, sec_type, con_id))
            return [chain]

    first = ibkr_gex._request_option_chains(FakeIb(), underlying)
    second = ibkr_gex._request_option_chains(FakeIb(), underlying)

    assert first == [chain]
    assert second == [chain]
    assert calls == [("ES", "CME", "FUT", 987654)]


@pytest.mark.parametrize(
    "underlying",
    [
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId=0),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId=True),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId=987654.0),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId=" 987654"),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId="+987654"),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId="0987654"),
        SimpleNamespace(symbol="ES", exchange="CME", secType="FUT", conId="９８７６５４"),
        SimpleNamespace(symbol="ES", exchange="", secType="FUT", conId=987654),
    ],
)
def test_gex_option_chain_request_fails_closed_without_exact_underlying_metadata(
    underlying,
) -> None:
    class FakeIb:
        def reqSecDefOptParams(self, *_args):
            raise AssertionError("incomplete underlying metadata must not reach IBKR")

    cache_key = ibkr_option_acquisition._option_chain_cache_key(underlying.conId)
    if cache_key > 0:
        ibkr_option_acquisition._store_option_chains(
            cache_key,
            [SimpleNamespace(exchange="CME", tradingClass="ES")],
        )
    with pytest.raises(RuntimeError, match="invalid|missing"):
        ibkr_gex._request_option_chains(FakeIb(), underlying)


def test_gex_expiration_selection_uses_hybrid_0dte_and_1dte() -> None:
    chain = SimpleNamespace(expirations=["20260601", "20260602", "20260603"])

    result = ibkr_option_acquisition._selected_expirations(
        chain,
        2,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
    )

    assert result == ["20260601", "20260602"]


def test_gex_chain_selection_uses_expiry_policy_without_series_scoring() -> None:
    monthly_chain = SimpleNamespace(
        exchange="CME", tradingClass="ES", multiplier="50", expirations=["20260618"], strikes=[7600]
    )
    daily_chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E1A",
        multiplier="50",
        expirations=["20260601", "20260602"],
        strikes=[7585],
    )

    result = ibkr_option_acquisition._selected_chain_expirations(
        [monthly_chain, daily_chain],
        required_exchange="CME",
        max_expirations=2,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
        mode="hybrid",
    )

    assert result == [(daily_chain, ["20260601", "20260602"])]


def test_gex_chain_expiration_selection_preserves_split_es_0dte_and_1dte() -> None:
    zero_dte_chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E1A",
        multiplier="50",
        expirations=["20260601"],
        strikes=[7585],
    )
    one_dte_chain = SimpleNamespace(
        exchange="CME",
        tradingClass="E2A",
        multiplier="50",
        expirations=["20260602"],
        strikes=[7590],
    )
    monthly_chain = SimpleNamespace(
        exchange="CME",
        tradingClass="ES",
        multiplier="50",
        expirations=["20260618"],
        strikes=[7600],
    )

    result = ibkr_option_acquisition._selected_chain_expirations(
        [monthly_chain, one_dte_chain, zero_dte_chain],
        required_exchange="CME",
        max_expirations=2,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
        mode="hybrid",
    )

    assert [(chain.tradingClass, expirations) for chain, expirations in result] == [
        ("E1A", ["20260601"]),
        ("E2A", ["20260602"]),
    ]


def test_gex_chain_expiration_selection_preserves_distinct_same_date_series() -> None:
    weekly = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPXW",
        multiplier="100",
        expirations=["20260717"],
        strikes=[6200],
    )
    monthly = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPX",
        multiplier="100",
        expirations=["20260717"],
        strikes=[6200],
    )

    result = ibkr_option_acquisition._selected_chain_expirations(
        [weekly, monthly],
        required_exchange="SMART",
        max_expirations=1,
        now=datetime(2026, 7, 17, 10, 0, tzinfo=gex.NY_TZ),
    )

    assert [(chain.tradingClass, expirations) for chain, expirations in result] == [
        ("SPX", ["20260717"]),
        ("SPXW", ["20260717"]),
    ]


def test_gex_chain_expiration_selection_uses_qualified_exchange_route() -> None:
    smart = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260720"],
        strikes=[742.0, 743.0, 744.0],
    )
    cboe = SimpleNamespace(
        exchange="CBOE",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260720"],
        strikes=[742.0, 743.0, 744.0],
    )

    result = ibkr_option_acquisition._selected_chain_expirations(
        [cboe, smart],
        required_exchange="SMART",
        max_expirations=1,
        now=datetime(2026, 7, 20, 10, 0, tzinfo=gex.NY_TZ),
    )

    assert result == [(smart, ["20260720"])]

    with pytest.raises(RuntimeError, match="no option chain"):
        ibkr_option_acquisition._selected_chain_expirations(
            [cboe, smart],
            required_exchange="ISE",
            max_expirations=1,
            now=datetime(2026, 7, 20, 10, 0, tzinfo=gex.NY_TZ),
        )


def test_gex_chain_expiration_selection_rejects_conflicting_same_series_descriptors() -> None:
    first = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260720"],
        strikes=[742.0, 743.0],
    )
    conflicting = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260720"],
        strikes=[742.0, 744.0],
    )

    with pytest.raises(ValueError, match="conflicting descriptors"):
        ibkr_option_acquisition._selected_chain_expirations(
            [first, conflicting],
            required_exchange="SMART",
            max_expirations=1,
            now=datetime(2026, 7, 20, 10, 0, tzinfo=gex.NY_TZ),
        )


def test_gex_chain_expiration_selection_collapses_identical_descriptor() -> None:
    first = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260720"],
        strikes=[742.0, 743.0],
    )
    duplicate = SimpleNamespace(
        exchange="SMART",
        tradingClass="SPY",
        multiplier="100",
        expirations=["20260720"],
        strikes=[742.0, 743.0],
    )

    assert ibkr_option_acquisition._selected_chain_expirations(
        [first, duplicate],
        required_exchange="SMART",
        max_expirations=1,
        now=datetime(2026, 7, 20, 10, 0, tzinfo=gex.NY_TZ),
    ) == [(first, ["20260720"])]


def test_gex_expiry_timestamp_comes_from_provider_contract_details() -> None:
    detail = SimpleNamespace(
        contract=SimpleNamespace(lastTradeDateOrContractMonth="20260717"),
        lastTradeTime="15:00:00",
        timeZoneId="America/Chicago",
    )

    assert (
        ibkr_option_acquisition._option_expiry_at_from_details(detail, "20260717")
        == "2026-07-17T20:00:00+00:00"
    )


def test_gex_expiry_timestamp_falls_back_to_provider_liquid_session() -> None:
    detail = SimpleNamespace(
        contract=SimpleNamespace(lastTradeDateOrContractMonth="20260717"),
        realExpirationDate="20260717",
        lastTradeTime="",
        timeZoneId="America/New_York",
        liquidHours=("20260716:0930-20260716:1600;20260717:0930-20260717:1600"),
        tradingHours="20260717:0400-20260717:2000",
    )

    assert (
        ibkr_option_acquisition._option_expiry_at_from_details(detail, "20260717")
        == "2026-07-17T20:00:00+00:00"
    )


def test_gex_expiry_timestamp_rejects_provider_expiration_mismatch() -> None:
    detail = SimpleNamespace(
        realExpirationDate="20260718",
        lastTradeTime="16:00",
        timeZoneId="America/New_York",
        liquidHours="20260717:0930-20260717:1600",
        tradingHours="20260717:0400-20260717:2000",
    )

    assert ibkr_option_acquisition._option_expiry_at_from_details(detail, "20260717") == ""


def test_gex_option_series_expiry_rejects_one_mismatched_provider_detail() -> None:
    valid = SimpleNamespace(
        realExpirationDate="20260717",
        lastTradeTime="16:00",
        timeZoneId="America/New_York",
    )
    conflicting = SimpleNamespace(
        realExpirationDate="20260718",
        lastTradeTime="16:00",
        timeZoneId="America/New_York",
    )

    with pytest.raises(RuntimeError, match="realExpirationDate"):
        ibkr_option_acquisition._option_series_expiry_at_from_details(
            [valid, conflicting],
            "20260717",
        )


def test_gex_expiration_selection_preserves_provider_weekend_code() -> None:
    chain = SimpleNamespace(expirations=["20260605", "20260606", "20260608", "20260609"])

    result = ibkr_option_acquisition._selected_expirations(
        chain,
        2,
        now=datetime(2026, 6, 5, 10, 0, tzinfo=gex.NY_TZ),
    )

    assert result == ["20260605", "20260606"]


def test_gex_expiration_selection_rejects_non_exact_provider_codes() -> None:
    chain = SimpleNamespace(expirations=["202606", "June 6", ""])

    assert (
        ibkr_option_acquisition._selected_expirations(
            chain,
            2,
            now=datetime(2026, 6, 5, 10, 0, tzinfo=gex.NY_TZ),
        )
        == []
    )


def test_gex_expiration_selection_trusts_provider_expiry_ladder() -> None:
    chain = SimpleNamespace(expirations=["20260618", "20260619", "20260622", "20260623"])

    result = ibkr_option_acquisition._selected_expirations(
        chain,
        2,
        now=datetime(2026, 6, 18, 10, 0, tzinfo=gex.NY_TZ),
    )

    assert result == ["20260618", "20260619"]


def test_gex_expiration_selection_can_be_forced_to_1dte_only() -> None:
    chain = SimpleNamespace(expirations=["20260601", "20260602", "20260603"])

    result = ibkr_option_acquisition._selected_expirations(
        chain,
        2,
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
        mode="1dte",
    )

    assert result == ["20260602", "20260603"]


def test_gex_expiration_selection_keeps_provider_0dte_without_asset_branch() -> None:
    chain = SimpleNamespace(expirations=["20260611", "20260612", "20260615"])

    result = ibkr_option_acquisition._selected_expirations(
        chain,
        2,
        now=datetime(2026, 6, 11, 16, 28, tzinfo=gex.NY_TZ),
        mode="hybrid",
    )

    assert result == ["20260611", "20260612"]


def test_gex_scheduler_enables_equity_etfs_during_provider_extended_session() -> None:
    instruments = [_ES, _SPY, _QQQ]
    jobs = gex.gex_scheduler_jobs(
        instruments,
        now=datetime(2026, 6, 1, 8, 0, tzinfo=gex.NY_TZ),
        session_open=lambda instrument, _ts: instrument in instruments,
    )

    by_symbol = {job.provider_symbol: job for job in jobs}
    assert by_symbol["ES"].enabled is True
    assert by_symbol["SPY"].enabled is True
    assert by_symbol["QQQ"].enabled is True
    for job in jobs:
        assert job.offset_minutes == gex._gex_route_offset_minutes(
            job.instrument_id,
            job.route_fingerprint,
            job.interval_minutes,
        )


def test_gex_scheduler_uses_single_auto_timer_config() -> None:
    jobs = gex.gex_scheduler_jobs(
        [_ES],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
        session_open=lambda _instrument, _ts: True,
    )
    job = jobs[0]

    assert len(jobs) == 1
    assert job.lane == "auto"
    assert job.interval_minutes == 5
    assert job.strike_count == 17
    assert job.max_contracts == 68
    assert job.refresh_mode == "auto"


def test_gex_scheduler_request_uses_canonical_contract_capacity() -> None:
    instrument = _ES
    jobs = gex.gex_scheduler_jobs(
        [instrument],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
        session_open=lambda _instrument, _ts: True,
    )
    request = gex.gex_request_for_scheduler_job(jobs[0], instrument=instrument)

    assert jobs[0].strike_count == 17
    assert jobs[0].max_contracts == 68
    assert request.strike_count == 17
    assert request.max_contracts == 68


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("strike_count", 15),
        ("max_contracts", 60),
        ("max_expirations", 1),
        ("lane", "manual"),
        ("refresh_mode", "manual"),
    ),
)
def test_gex_scheduler_request_rejects_stale_job_scope(
    field: str,
    value: object,
) -> None:
    instrument = _ES
    job = gex.gex_scheduler_jobs(
        [instrument],
        now=datetime(2026, 6, 1, 10, 0, tzinfo=gex.NY_TZ),
        session_open=lambda _instrument, _ts: True,
    )[0]

    with pytest.raises(ValueError, match="configuration is stale"):
        gex.gex_request_for_scheduler_job(
            replace(job, **{field: value}),
            instrument=instrument,
        )


def test_gex_scheduler_settings_error_blocks_all_runtime_work() -> None:
    scheduler_state: dict[tuple[str, str], gex_scheduler_service.GexSchedulerRouteState] = {}

    async def run_until_settings_are_read() -> None:
        loop = asyncio.get_running_loop()
        settings_read = asyncio.Event()

        def runtime_settings() -> dict[str, Any]:
            loop.call_soon_threadsafe(settings_read.set)
            return {
                "enabled": True,
                "instrument_ids": [_instrument_id(_ES)],
                "settings_error": "server settings unavailable",
            }

        def forbidden(*_args, **_kwargs):
            raise AssertionError("scheduler work must remain disabled")

        task = asyncio.create_task(
            run_gex_scheduler_loop(
                interval_seconds=0.001,
                runtime_settings=runtime_settings,
                server_sleeping=lambda: False,
                scheduler_jobs=forbidden,
                instrument_for_job=forbidden,
                store_factory=forbidden,
                scheduler_state=scheduler_state,
                logger_warning=lambda *_args: None,
            )
        )
        await asyncio.wait_for(settings_read.wait(), timeout=2.0)
        await asyncio.sleep(0)

        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_until_settings_are_read())

    assert scheduler_state == {}


def test_gex_scheduler_skips_selected_live_routes_and_retries_request_once(
    monkeypatch,
) -> None:
    monkeypatch.setattr(gex_scheduler_service, "_GEX_SCHEDULER_RETRY_SECONDS", 0.0)
    due = datetime.now(tz=UTC) - timedelta(minutes=1)
    instruments = (_ES, _SPY, _QQQ)
    jobs = [
        SimpleNamespace(
            provider_symbol=instrument["provider_symbol"],
            instrument_id=_instrument_id(instrument),
            route_fingerprint=_route_key(instrument),
            enabled=True,
            next_due_at=due,
            interval_minutes=5,
        )
        for instrument in instruments
    ]
    live_status = {
        "enabled": True,
        "active": True,
        "sessions": [
            {
                "provider_symbol": "ES",
                "instrument_id": _instrument_id(_ES),
                "route_fingerprint": _route_key(_ES),
                "active": True,
            },
            {
                "provider_symbol": "SPY",
                "instrument_id": _instrument_id(_SPY),
                "route_fingerprint": _route_key(_SPY),
                "active": False,
            },
        ],
    }
    loads: list[str] = []
    scheduler_state: dict[tuple[str, str], gex_scheduler_service.GexSchedulerRouteState] = {}
    loaded = asyncio.Event()

    async def load(provider_symbol: str, **_kwargs):
        loads.append(provider_symbol)
        if len(loads) == 1:
            jobs[-1].next_due_at = datetime.now(tz=UTC) + timedelta(minutes=5)
            return {"ok": False, "status": "refresh_error", "message": "provider timeout"}
        loaded.set()
        return {"ok": True, "status": "ok"}

    class FakeGexAdapter:
        key = "ibkr"
        capabilities = ProviderCapabilities(gex=True)

        def gex_request_for_job(self, _instrument: dict[str, Any], _job: Any, **_kwargs: Any):
            return SimpleNamespace()

        async def async_load_gex(self, instrument: dict[str, Any], **kwargs: Any):
            return await load(instrument["provider_symbol"], **kwargs)

    adapter = FakeGexAdapter()

    def provider_router(instrument: dict[str, Any]) -> InstrumentRoute:
        return InstrumentRoute(
            instrument=instrument,
            adapter=adapter,
            provider="ibkr",
            provider_symbol=instrument["provider_symbol"],
            instrument_id=qualified_instrument_id(instrument),
            instrument_key=str(instrument.get("instrument_key") or instrument["key"]),
            fingerprint=route_fingerprint(instrument),
            price_increment=None,
        )

    async def run_until_non_live_request_completes() -> None:
        task = asyncio.create_task(
            run_gex_scheduler_loop(
                interval_seconds=0.001,
                runtime_settings=lambda: {
                    "enabled": True,
                    "instrument_ids": [
                        _instrument_id(_ES),
                        _instrument_id(_SPY),
                        _instrument_id(_QQQ),
                    ],
                },
                server_sleeping=lambda: False,
                live_gex_status=lambda: live_status,
                scheduler_jobs=lambda *_args, **_kwargs: jobs,
                instrument_for_job=lambda instrument_id: _INSTRUMENTS_BY_ID[instrument_id],
                store_factory=lambda: object(),
                scheduler_state=scheduler_state,
                logger_warning=lambda *_args: None,
                provider_router=provider_router,
            )
        )
        await asyncio.wait_for(loaded.wait(), timeout=4.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_until_non_live_request_completes())

    assert loads == ["QQQ", "QQQ"]
    assert _identity_key(_ES) not in scheduler_state
    assert _identity_key(_SPY) not in scheduler_state
    assert scheduler_state[_identity_key(_QQQ)].due_at == due
    assert scheduler_state[_identity_key(_QQQ)].attempts == 2
    assert scheduler_state[_identity_key(_QQQ)].completed is True


def test_gex_scheduler_acknowledges_completed_attempt_before_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    state = gex_scheduler_service.GexSchedulerRouteState(
        due_at=datetime.now(tz=UTC) - timedelta(minutes=1)
    )
    job = SimpleNamespace(provider_symbol="QQQ")

    class Adapter:
        capabilities = ProviderCapabilities(gex=True)

        def gex_request_for_job(self, *_args, **_kwargs):
            return SimpleNamespace()

        async def async_load_gex(self, *_args, **kwargs):
            assert kwargs["project_capture_history"] is False
            started.set()
            await release.wait()
            return {"ok": True, "status": "ok"}

    route = InstrumentRoute(
        instrument=_QQQ,
        adapter=Adapter(),
        provider="ibkr",
        provider_symbol="QQQ",
        instrument_id=_instrument_id(_QQQ),
        instrument_key="QQQ",
        fingerprint=_route_key(_QQQ),
        price_increment=None,
    )

    async def run() -> None:
        task = asyncio.create_task(
            gex_scheduler_service._run_gex_scheduler_attempt(
                route=route,
                job=job,
                store=object(),
                state=state,
                logger_warning=lambda *_args: None,
            )
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)

        assert not task.done()
        assert state.completed is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert state.attempts == 1
    assert state.completed is True
    assert state.retry_at is None


def test_gex_scheduler_does_not_repeat_partial_frame_with_stale_spot() -> None:
    due = datetime.now(tz=UTC) - timedelta(minutes=1)
    job = SimpleNamespace(
        provider_symbol="QQQ",
        instrument_id=_instrument_id(_QQQ),
        route_fingerprint=_route_key(_QQQ),
        enabled=True,
        next_due_at=due,
        interval_minutes=5,
    )
    loads: list[str] = []
    scheduler_state: dict[tuple[str, str], gex_scheduler_service.GexSchedulerRouteState] = {}

    class FakeGexAdapter:
        key = "ibkr"
        capabilities = ProviderCapabilities(gex=True)

        def gex_request_for_job(
            self,
            _instrument: dict[str, Any],
            _job: Any,
            **_kwargs: Any,
        ) -> SimpleNamespace:
            return SimpleNamespace()

        async def async_load_gex(
            self,
            instrument: dict[str, Any],
            **_kwargs: Any,
        ) -> dict[str, Any]:
            loads.append(instrument["provider_symbol"])
            return {
                "ok": False,
                "status": "partial",
                "diagnostics": {
                    "diag_frame_quality_code": ("UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"),
                },
            }

    adapter = FakeGexAdapter()

    def provider_router(instrument: dict[str, Any]) -> InstrumentRoute:
        return InstrumentRoute(
            instrument=instrument,
            adapter=adapter,
            provider="ibkr",
            provider_symbol=instrument["provider_symbol"],
            instrument_id=qualified_instrument_id(instrument),
            instrument_key=str(instrument.get("instrument_key") or instrument["key"]),
            fingerprint=route_fingerprint(instrument),
            price_increment=None,
        )

    async def run_one_slot() -> None:
        task = asyncio.create_task(
            run_gex_scheduler_loop(
                interval_seconds=0.001,
                runtime_settings=lambda: {
                    "enabled": True,
                    "instrument_ids": [_instrument_id(_QQQ)],
                },
                server_sleeping=lambda: False,
                live_gex_status=lambda: {"sessions": []},
                scheduler_jobs=lambda *_args, **_kwargs: [job],
                instrument_for_job=lambda _instrument_id: _QQQ,
                store_factory=lambda: object(),
                scheduler_state=scheduler_state,
                logger_warning=lambda *_args: None,
                provider_router=provider_router,
            )
        )
        state_key = _identity_key(_QQQ)
        try:
            async with asyncio.timeout(4.0):
                while not (state_key in scheduler_state and scheduler_state[state_key].completed):
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run_one_slot())

    state = scheduler_state[_identity_key(_QQQ)]
    assert loads == ["QQQ"]
    assert state.attempts == 1
    assert state.completed is True
    assert state.retry_at is None


def test_gex_activity_delta_uses_nearby_raw_strikes() -> None:
    previous = {
        "spot": 5000,
        "request_meta": {"strike_count": 2},
        "raw": {
            "strikes": [
                {"strike": 4995, "call_volume": 10, "put_volume": 5, "call_oi": 20, "put_oi": 30},
                {"strike": 5000, "call_volume": 12, "put_volume": 7, "call_oi": 25, "put_oi": 35},
            ],
        },
    }
    previous["raw"]["contracts"] = _activity_contract_rows(previous["raw"]["strikes"])
    current = {
        "spot": 5000,
        "request_meta": {"strike_count": 2},
        "raw": {
            "strikes": [
                {"strike": 4995, "call_volume": 15, "put_volume": 6, "call_oi": 22, "put_oi": 30},
                {"strike": 5000, "call_volume": 32, "put_volume": 9, "call_oi": 25, "put_oi": 38},
            ],
        },
    }
    current["raw"]["contracts"] = _activity_contract_rows(current["raw"]["strikes"])

    result = gex._gex_activity_delta(previous, current)

    assert result == {
        "status": "ok",
        "selected_strikes": 2,
        "selected_contracts": 4,
        "matched_contracts": 4,
        "call_volume_delta": 25.0,
        "put_volume_delta": 3.0,
    }


def test_gex_snapshot_writer_propagates_database_failures() -> None:
    class FailingStore:
        def initialize(self) -> None:
            return None

        def read_gex_snapshots(self, *_args, **_kwargs) -> list[dict]:
            return []

        def upsert_gex_snapshot(self, *_args, **_kwargs) -> None:
            raise RuntimeError("database unavailable")

    captured_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    payload = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "request_meta": {
                "refresh_mode": "request",
                "strike_count": 21,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260717",
    )["payload"]

    with pytest.raises(RuntimeError, match="database unavailable"):
        gex.persist_gex_snapshot(
            "ES",
            payload["raw"]["meta"],
            payload["raw"]["strikes"],
            payload,
            payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=FailingStore(),
            captured_at=captured_at,
            capture_mode="request",
        )


def test_gex_activity_warmup_propagates_storage_read_failures() -> None:
    class FailingWarmupStore:
        def initialize(self) -> None:
            return None

        def read_gex_snapshots(self, *_args, **_kwargs) -> list[dict[str, Any]]:
            raise RuntimeError("warmup database unavailable")

        def upsert_gex_snapshot(self, *_args, **_kwargs) -> None:
            raise AssertionError("failed warmup must not reach snapshot commit")

    captured_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    payload = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "request",
            "spot": 5000.0,
            "request_meta": {
                "refresh_mode": "request",
                "strike_count": 21,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
        },
        source="gex:ibkr",
        expiry="20260717",
    )["payload"]

    with pytest.raises(RuntimeError, match="warmup database unavailable"):
        gex.persist_gex_snapshot(
            "ES",
            payload["raw"]["meta"],
            payload["raw"]["strikes"],
            payload,
            payload["raw"]["contracts"],
            instrument_id=_instrument_id(_ES),
            route_fingerprint=_route_key(_ES),
            store=FailingWarmupStore(),
            captured_at=captured_at,
            capture_mode="request",
        )


def test_live_gex_bucket_replacement_rejects_invalid_persisted_session() -> None:
    bucket_at = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    captured_at = bucket_at + timedelta(minutes=3)
    payload = _gex_snapshot(
        _ES,
        captured_at,
        {
            "capture_mode": "live",
            "spot": 5000.0,
            "request_meta": {
                "refresh_mode": "live",
                "strike_count": 21,
                "futures_options": True,
            },
            "levels": [_canonical_gex_level(5000.0)],
            "live": {
                "publishable": True,
                "session_id": captured_at.isoformat(),
                "started_at": captured_at.isoformat(),
            },
        },
        source="gex:ibkr-live",
        expiry="20260717",
    )["payload"]
    invalid_existing = deepcopy(payload)
    invalid_existing["live"]["started_at"] = "not-a-timestamp"
    statements: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement: str, _params: object) -> None:
            statements.append(statement)

        def fetchall(self) -> list[tuple[datetime, dict[str, Any]]]:
            return [(captured_at, invalid_existing)]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def transaction(self):
            return self

        def cursor(self) -> Cursor:
            return Cursor()

    repo = PostgresStore(
        None,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )
    repo._psycopg = lambda: None  # type: ignore[attr-defined,method-assign]
    repo._connect = lambda: Connection()  # type: ignore[attr-defined,method-assign]

    with pytest.raises(
        ValueError,
        match=(
            "GEX_LIVE_BUCKET_CONTRACT_INVALID: Persisted live GEX snapshot "
            "requires one canonical session timestamp"
        ),
    ):
        repo.upsert_gex_snapshot(
            _instrument_id(_ES),
            _route_key(_ES),
            "ES",
            captured_at,
            payload,
            source="gex:ibkr-live",
            live_bucket_at=bucket_at,
        )

    assert all("DELETE FROM gex_snapshots" not in statement for statement in statements)
    assert all("INSERT INTO gex_snapshots" not in statement for statement in statements)


def test_gex_snapshot_persistence_lock_is_stable_per_exact_route_identity() -> None:
    es_instrument_id = _instrument_id(_ES)
    spy_instrument_id = _instrument_id(_SPY)
    es_route = _route_key(_ES)
    spy_route = _route_key(_SPY)
    es_lock = gex_history._gex_persist_lock(es_instrument_id, es_route)

    assert es_lock is gex_history._gex_persist_lock(es_instrument_id, es_route)
    assert es_lock is not gex_history._gex_persist_lock(spy_instrument_id, spy_route)


def test_gex_activity_delta_uses_payload_strike_count() -> None:
    previous_strikes = [
        {
            "strike": 4990 + index * 5,
            "call_volume": 10,
            "put_volume": 5,
            "call_oi": 20,
            "put_oi": 30,
        }
        for index in range(6)
    ]
    current_strikes = [
        {
            "strike": row["strike"],
            "call_volume": row["call_volume"] + 1,
            "put_volume": row["put_volume"] + 2,
            "call_oi": row["call_oi"] + 3,
            "put_oi": row["put_oi"] + 4,
        }
        for row in previous_strikes
    ]
    previous = {
        "spot": 5000,
        "request_meta": {"strike_count": 6},
        "raw": {
            "strikes": previous_strikes,
            "contracts": _activity_contract_rows(previous_strikes),
        },
    }
    current = {
        "spot": 5000,
        "request_meta": {"strike_count": 6},
        "raw": {
            "strikes": current_strikes,
            "contracts": _activity_contract_rows(current_strikes),
        },
    }

    result = gex._gex_activity_delta(previous, current)

    assert result == {
        "status": "ok",
        "selected_strikes": 6,
        "selected_contracts": 12,
        "matched_contracts": 12,
        "call_volume_delta": 6.0,
        "put_volume_delta": 12.0,
    }


def test_gex_activity_delta_requires_equal_typed_request_strike_counts() -> None:
    strikes = [{"strike": 5000.0, "call_volume": 10.0, "put_volume": 5.0}]
    contracts = _activity_contract_rows(strikes)
    previous = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 1},
        "raw": {"strikes": strikes, "contracts": contracts},
    }
    wrong_count = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 2},
        "raw": {"strikes": strikes, "contracts": contracts},
    }
    coerced_count = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 1.0},
        "raw": {"strikes": strikes, "contracts": contracts},
    }
    legacy_raw_count = {
        "spot": 5000.0,
        "raw": {
            "meta": {"strike_count": 1},
            "strikes": strikes,
            "contracts": contracts,
        },
    }

    assert gex._gex_activity_delta(previous, wrong_count)["status"] == "missing_input"
    assert gex._gex_activity_delta(previous, coerced_count)["status"] == "missing_input"
    assert gex._gex_activity_delta(previous, legacy_raw_count)["status"] == "missing_input"


def test_gex_activity_delta_rejects_counter_resets_and_missing_overlap() -> None:
    previous = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 2},
        "raw": {
            "strikes": [
                {"strike": 4995.0, "call_volume": 10.0, "put_volume": 5.0},
                {"strike": 5000.0, "call_volume": 20.0, "put_volume": 8.0},
            ],
        },
    }
    reset = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 2},
        "raw": {
            "strikes": [
                {"strike": 4995.0, "call_volume": 1.0, "put_volume": 1.0},
                {"strike": 5000.0, "call_volume": 2.0, "put_volume": 1.0},
            ],
        },
    }
    shifted = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 2},
        "raw": {
            "strikes": [
                {"strike": 5000.0, "call_volume": 21.0, "put_volume": 9.0},
                {"strike": 5005.0, "call_volume": 3.0, "put_volume": 2.0},
            ],
        },
    }
    previous["raw"]["contracts"] = _activity_contract_rows(previous["raw"]["strikes"])
    reset["raw"]["contracts"] = _activity_contract_rows(reset["raw"]["strikes"])
    shifted["raw"]["contracts"] = _activity_contract_rows(shifted["raw"]["strikes"])

    reset_result = gex._gex_activity_delta(previous, reset)
    overlap_result = gex._gex_activity_delta(previous, shifted)

    assert reset_result == {
        "status": "counter_reset",
        "selected_strikes": 2,
        "selected_contracts": 4,
        "matched_contracts": 4,
        "call_volume_delta": None,
        "put_volume_delta": None,
    }
    assert overlap_result == {
        "status": "missing_overlap",
        "selected_strikes": 2,
        "selected_contracts": 4,
        "matched_contracts": 2,
        "call_volume_delta": None,
        "put_volume_delta": None,
    }


def test_gex_activity_delta_rejects_one_contract_reset_hidden_by_aggregate_growth() -> None:
    strike_row = {"strike": 5000.0, "call_volume": 200.0, "put_volume": 50.0}
    first_series = {
        "expiry": "20260721",
        "trading_class": "ES",
        "exchange": "CME",
        "multiplier": 50.0,
        "strike": 5000.0,
        "gamma": None,
        "open_interest": None,
        "gex": None,
        "abs_gex": None,
    }
    second_series = {
        **first_series,
        "expiry": "20260728",
    }
    previous = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 1},
        "raw": {
            "strikes": [strike_row],
            "contracts": [
                {**first_series, "con_id": 1, "right": "C", "volume": 100.0},
                {**second_series, "con_id": 2, "right": "C", "volume": 100.0},
                {**first_series, "con_id": 3, "right": "P", "volume": 50.0},
                {**second_series, "con_id": 4, "right": "P", "volume": 0.0},
            ],
        },
    }
    current = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 1},
        "raw": {
            "strikes": [{"strike": 5000.0, "call_volume": 210.0, "put_volume": 55.0}],
            "contracts": [
                {**first_series, "con_id": 1, "right": "C", "volume": 90.0},
                {**second_series, "con_id": 2, "right": "C", "volume": 120.0},
                {**first_series, "con_id": 3, "right": "P", "volume": 55.0},
                {**second_series, "con_id": 4, "right": "P", "volume": 0.0},
            ],
        },
    }

    result = gex._gex_activity_delta(previous, current)

    assert result["status"] == "counter_reset"
    assert result["call_volume_delta"] is None
    assert result["put_volume_delta"] is None


def test_gex_activity_delta_requires_call_put_pair_on_the_exact_series_key() -> None:
    strikes = [{"strike": 5000.0, "call_volume": 10.0, "put_volume": 5.0}]
    contracts = _activity_contract_rows(strikes)
    contracts[1] = {**contracts[1], "trading_class": "EW"}
    previous = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 1},
        "raw": {"strikes": strikes, "contracts": contracts},
    }
    current = {
        "spot": 5000.0,
        "request_meta": {"strike_count": 1},
        "raw": {"strikes": strikes, "contracts": contracts},
    }

    result = gex._gex_activity_delta(previous, current)

    assert result["status"] == "missing_input"
    assert result["call_volume_delta"] is None
    assert result["put_volume_delta"] is None


def test_gex_comparison_scope_requires_producer_scope_and_rejects_coercion() -> None:
    series = [
        {
            "expiry": expiry,
            "trading_class": "ES",
            "exchange": "CME",
            "multiplier": 50.0,
        }
        for expiry in ("20260601", "20260602")
    ]
    contracts = [
        {
            **identity,
            "strike": 5000.0,
            "right": right,
            "con_id": index,
        }
        for index, (identity, right) in enumerate(
            (
                (series[0], "C"),
                (series[0], "P"),
                (series[1], "C"),
                (series[1], "P"),
            ),
            start=1,
        )
    ]
    valid = {
        "source": "gex:ibkr",
        "capture_mode": "request",
        "raw": {
            "meta": {
                "strike_count": 1,
                "futures_options": True,
                "risk_free_rate": 0.05,
                "dividend_yield": 0.0,
                "market_data_entitlement": "live",
            },
            "contracts": contracts,
        },
    }

    comparison_scope = gex_comparison_scope_from_contract_universe(
        contracts,
        source="gex:ibkr",
        capture_mode="request",
        futures_options=True,
        risk_free_rate=0.05,
        dividend_yield=0.0,
        market_data_entitlement="live",
    )
    assert comparison_scope == {
        "capture_mode": "request",
        "strike_count": 1,
        "strike_ladder": [5000.0],
        "contract_con_ids": [1, 2, 3, 4],
        "expiries": ["20260601", "20260602"],
        "futures_options": True,
        "series": series,
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "market_data_entitlement": "live",
    }
    persisted = {
        **valid,
        "comparison_scope": comparison_scope,
    }
    assert gex_comparison_scope_from_payload(persisted) == persisted["comparison_scope"]
    with pytest.raises(ValueError):
        gex_comparison_scope_from_payload(valid)
    with pytest.raises(ValueError):
        gex_comparison_scope_from_payload(
            {
                **persisted,
                "comparison_scope": {
                    **persisted["comparison_scope"],
                    "strike_count": 15,
                },
            },
        )
    with pytest.raises(ValueError):
        gex_comparison_scope_from_payload(
            {
                **persisted,
                "comparison_scope": {
                    **persisted["comparison_scope"],
                    "contract_con_ids": [1, 2, 3],
                },
            },
        )
    invalid_payloads = [
        {**persisted, "capture_mode": "live"},
        {
            **persisted,
            "raw": {
                **persisted["raw"],
                "meta": {**persisted["raw"]["meta"], "strike_count": 1.0},
            },
        },
        {
            **persisted,
            "raw": {
                **persisted["raw"],
                "meta": {**persisted["raw"]["meta"], "futures_options": 1},
            },
        },
        {
            **persisted,
            "raw": {**persisted["raw"], "contracts": []},
        },
        {
            **persisted,
            "raw": {
                **persisted["raw"],
                "contracts": [{**contracts[0], "expiry": "20261340"}],
            },
        },
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            gex_comparison_scope_from_payload(payload)


def test_gex_option_activity_contract_is_exact_and_requires_full_pairs() -> None:
    activity = {
        "status": "READY",
        "scope": "selected_chain",
        "elapsed_seconds": 300.0,
        "contract_universe": {
            "selected_strikes": 1,
            "selected_contracts": 2,
            "matched_contracts": 2,
        },
        "call": {
            "volume_delta": 10.0,
            "rate_per_minute": 2.0,
            "rvol": 1.0,
            "baseline_samples": 3,
        },
        "put": {
            "volume_delta": 5.0,
            "rate_per_minute": 1.0,
            "rvol": 1.0,
            "baseline_samples": 3,
        },
        "total": {
            "volume_delta": 15.0,
            "rate_per_minute": 3.0,
            "rvol": 1.0,
            "baseline_samples": 3,
        },
    }

    assert require_gex_option_activity(activity, expected_status="READY") == activity
    with pytest.raises(ValueError, match="exact typed fields"):
        require_gex_option_activity({**activity, "legacy_rate": 3.0})
    with pytest.raises(ValueError, match="complete selected contract universe"):
        require_gex_option_activity(
            {
                **activity,
                "contract_universe": {
                    **activity["contract_universe"],
                    "matched_contracts": 1,
                },
            }
        )
    with pytest.raises(ValueError, match="total.volume_delta"):
        require_gex_option_activity(
            {
                **activity,
                "total": {
                    **activity["total"],
                    "volume_delta": 14.0,
                    "rate_per_minute": 2.8,
                },
            }
        )
    with pytest.raises(ValueError, match="call.rate_per_minute is inconsistent"):
        require_gex_option_activity(
            {
                **activity,
                "call": {**activity["call"], "rate_per_minute": 99.0},
                "total": {**activity["total"], "rate_per_minute": 100.0},
            }
        )
    with pytest.raises(ValueError, match="call.rvol readiness is inconsistent"):
        require_gex_option_activity(
            {
                **activity,
                "call": {**activity["call"], "baseline_samples": 0},
            }
        )


def test_gex_time_to_expiry_is_minute_level_to_ny_close() -> None:
    result = gex._time_to_expiry_years(
        "20260601",
        now=datetime(2026, 6, 1, 15, 30, tzinfo=gex.NY_TZ),
    )

    assert round(result * 365 * 24 * 60) == 30


def test_gex_exact_expiry_does_not_clamp_an_expired_contract_to_one_minute() -> None:
    expiry_at = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)

    assert (
        gex._time_to_expiry_years(
            "20260601",
            now=expiry_at,
            expiry_at=expiry_at,
            require_exact=True,
        )
        is None
    )


def test_gex_black_scholes_fallback_uses_continuous_dividend_yield() -> None:
    no_yield = gex._black_scholes_gamma_delta(
        spot=500,
        strike=500,
        years=1 / 365,
        iv=0.20,
        right=OptionRight.CALL,
        risk_free_rate=0.052,
        dividend_yield=0.0,
    )
    with_yield = gex._black_scholes_gamma_delta(
        spot=500,
        strike=500,
        years=1 / 365,
        iv=0.20,
        right=OptionRight.CALL,
        risk_free_rate=0.052,
        dividend_yield=0.03,
    )

    assert no_yield is not None
    assert with_yield is not None
    assert with_yield["delta"] < no_yield["delta"]
    assert with_yield["gamma"] > 0


def test_gex_option_math_rejects_untyped_right() -> None:
    with pytest.raises(TypeError, match="typed OptionRight"):
        gex._black_scholes_gamma_delta(
            spot=500,
            strike=500,
            years=1 / 365,
            iv=0.20,
            right="C",
            risk_free_rate=0.052,
            dividend_yield=0.0,
        )


def test_futures_option_model_uses_black_76_not_equity_dividend_model() -> None:
    futures_price = 7395.0
    strike = 7395.0
    years = gex._time_to_expiry_years(
        "20260612",
        now=datetime(2026, 6, 12, 13, 0, tzinfo=gex.NY_TZ),
    )
    iv = 0.25
    rate = 0.052
    sqrt_t = math.sqrt(years)
    d1 = (math.log(futures_price / strike) + 0.5 * iv * iv * years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    expected_put = math.exp(-rate * years) * (
        strike * gex._normal_cdf(-d2) - futures_price * gex._normal_cdf(-d1)
    )

    es_put = gex._option_model_price(
        True,
        spot=futures_price,
        strike=strike,
        years=years,
        iv=iv,
        right=OptionRight.PUT,
        risk_free_rate=rate,
        dividend_yield=0.20,
    )
    es_greeks = gex._option_model_gamma_delta(
        True,
        spot=futures_price,
        strike=strike,
        years=years,
        iv=iv,
        right=OptionRight.PUT,
        risk_free_rate=rate,
        dividend_yield=0.20,
    )
    equity_put = gex._black_scholes_price(
        spot=futures_price,
        strike=strike,
        years=years,
        iv=iv,
        right=OptionRight.PUT,
        risk_free_rate=rate,
        dividend_yield=0.0,
    )

    assert es_put is not None
    assert es_greeks is not None
    assert round(es_put, 8) == round(expected_put, 8)
    assert es_put == gex._option_model_price(
        True,
        spot=futures_price,
        strike=strike,
        years=years,
        iv=iv,
        right=OptionRight.PUT,
        risk_free_rate=rate,
        dividend_yield=0.0,
    )
    assert round(es_greeks["delta"], 8) == round(
        math.exp(-rate * years) * (gex._normal_cdf(d1) - 1.0), 8
    )
    assert equity_put is not None
    assert es_put > equity_put


def test_gex_global_strike_ladder_preserves_ragged_exact_series() -> None:
    contracts = []
    con_id = 1
    for expiry, strike_range in (
        ("20260601", range(735, 746)),
        ("20260602", range(745, 756)),
    ):
        for strike in strike_range:
            contracts.append(
                _ibkr_option(con_id=con_id, expiry=expiry, strike=float(strike), right="C")
            )
            con_id += 1
            contracts.append(
                _ibkr_option(con_id=con_id, expiry=expiry, strike=float(strike), right="P")
            )
            con_id += 1

    selected = ibkr_option_acquisition._select_global_option_series_contracts(
        [
            [item for item in contracts if item.expiry == "20260601"],
            [item for item in contracts if item.expiry == "20260602"],
        ],
        745.0,
        9,
        36,
    )
    expiries = [item.expiry for item in selected]
    selected_strikes = {
        expiry: {item.strike for item in selected if item.expiry == expiry}
        for expiry in ("20260601", "20260602")
    }

    assert len(selected) == 20
    assert expiries.count("20260601") == 10
    assert expiries.count("20260602") == 10
    assert len(selected_strikes["20260601"] | selected_strikes["20260602"]) == 9
    assert selected_strikes["20260601"] != selected_strikes["20260602"]
    assert selected_strikes["20260601"] & selected_strikes["20260602"] == {745.0}
    assert any(item.expiry == "20260602" and item.right == "P" for item in selected)


def test_gex_underlying_audit_records_front_future_contract_fields() -> None:
    contract = SimpleNamespace(
        symbol="ES",
        secType="FUT",
        conId=12345,
        localSymbol="ESM6",
        lastTradeDateOrContractMonth="20260619",
        currency="USD",
    )

    result = ibkr_gex._underlying_audit(contract)

    assert result == {
        "underlying_symbol": "ES",
        "underlying_sec_type": "FUT",
        "underlying_con_id": 12345,
        "underlying_local_symbol": "ESM6",
        "underlying_expiry": "20260619",
        "underlying_currency": "USD",
    }


def test_gex_option_rows_are_requested_in_small_cancelled_batches() -> None:
    requests: list[float] = []
    cancellations: list[float] = []
    sleeps: list[float] = []
    tickers: list[Any] = []

    class FakeGreek:
        gamma = 0.01
        delta = 0.5
        impliedVol = 0.2

    class FakeTicker:
        modelGreeks = FakeGreek()
        callOpenInterest = 10
        putOpenInterest = 12
        callVolume = 1
        putVolume = 2
        bid = 1.0
        ask = 1.1
        last = 1.05
        tickerId = None
        ticks = [SimpleNamespace(tickType=27)]

    class FakeIb:
        def __init__(self):
            self.wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, contract, *_args):
            requests.append(contract.strike)
            ticker = FakeTicker()
            ticker.tickerId = len(tickers) + 1
            tickers.append(ticker)
            self.wrapper.tickerIdMap[ticker.tickerId] = ticker
            return ticker

        def cancelMktData(self, contract):
            cancellations.append(contract.strike)
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            sleeps.append(value)
            for ticker in tickers:
                ticker.marketDataType = 1

    contracts = [
        _ibkr_option(
            con_id=index + 1,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=float(index + 1),
            right="C",
        )
        for index in range(5)
    ]

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        contracts,
        5000.0,
        0.5,
        batch_size=2,
        batch_pause_seconds=0.25,
    )

    assert len(rows) == 5
    assert requests == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert cancellations == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sleeps == [
        0.1,
        0.15,
        0.25,
        0.1,
        0.15,
        0.25,
        0.1,
        0.15,
    ]


def test_gex_option_rows_wait_for_oi_tick_and_preserve_confirmed_zero() -> None:
    sleeps: list[float] = []

    class FakeGreek:
        gamma = 0.01
        delta = 0.5
        impliedVol = 0.2

    class FakeTicker:
        modelGreeks = FakeGreek()
        callOpenInterest = None
        tickerId = None
        ticks: list[Any] = []

    ticker = FakeTicker()

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, *_args):
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            sleeps.append(value)
            ticker.marketDataType = 1
            if sum(sleeps) >= 0.2:
                ticker.callOpenInterest = 0
                ticker.ticks = [SimpleNamespace(tickType=27)]

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        [
            _ibkr_option(
                con_id=1,
                expiry=_ACTIVE_OPTION_EXPIRY,
                expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
                strike=5000.0,
                right="C",
            )
        ],
        5000.0,
        2.0,
    )

    assert sleeps == [0.1, 0.1, 0.15]
    assert rows[0]["open_interest"] == 0
    assert rows[0]["gex"] == 0
    assert rows[0]["abs_gex"] == 0
    assert rows[0]["open_interest_received_at"] is not None


def test_gex_option_rows_wait_for_usable_greeks_after_oi_arrives() -> None:
    sleeps: list[float] = []

    class FakeTicker:
        modelGreeks = None
        callOpenInterest = 12
        tickerId = None
        ticks = [SimpleNamespace(tickType=27)]

    ticker = FakeTicker()

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, *_args):
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            sleeps.append(value)
            ticker.marketDataType = 1
            if sum(sleeps) >= 0.3:
                ticker.modelGreeks = SimpleNamespace(gamma=0.01, delta=0.5, impliedVol=0.2)

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        [
            _ibkr_option(
                con_id=1,
                expiry=_ACTIVE_OPTION_EXPIRY,
                expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
                strike=5000.0,
                right="C",
            )
        ],
        5000.0,
        1.0,
    )

    assert sleeps[:3] == [0.1, 0.1, 0.1]
    assert rows[0]["open_interest"] == 12
    assert rows[0]["gamma"] == 0.01
    assert rows[0]["greek_source"] == "modelGreeks"


def test_live_gex_waits_for_broker_model_greeks_when_local_iv_is_already_usable() -> None:
    sleeps: list[float] = []
    expiry_at = datetime.now(tz=UTC) + timedelta(days=1)

    class FakeTicker:
        modelGreeks = None
        impliedVolatility = 0.22
        bid = 24.0
        ask = 26.0
        last = None
        close = None
        callOpenInterest = 12
        tickerId = None
        ticks = [SimpleNamespace(tickType=27)]

    ticker = FakeTicker()

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, *_args):
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            sleeps.append(value)
            ticker.marketDataType = 1
            if sum(sleeps) >= 0.3:
                ticker.modelGreeks = SimpleNamespace(
                    gamma=0.01,
                    delta=0.5,
                    impliedVol=0.2,
                )

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        [
            _ibkr_option(
                con_id=1,
                expiry=expiry_at.strftime("%Y%m%d"),
                expiry_at=expiry_at,
                strike=5000.0,
                right="C",
            )
        ],
        5000.0,
        1.0,
    )

    assert sleeps[:3] == [0.1, 0.1, 0.1]
    assert rows[0]["greek_source"] == "modelGreeks"
    assert rows[0]["model_greeks_received_at"] is not None


def test_gex_reference_option_price_supplies_local_iv_when_broker_greeks_are_missing() -> None:
    expiry_at = datetime.now(tz=UTC) + timedelta(days=1)
    expiry_code = expiry_at.strftime("%Y%m%d")

    ticker = SimpleNamespace(
        modelGreeks=None,
        bid=24.0,
        ask=26.0,
        last=None,
        close=None,
        callOpenInterest=12,
        callVolume=1,
    )
    contract = _ibkr_option(
        con_id=1,
        expiry=expiry_code,
        expiry_at=expiry_at,
        strike=5000.0,
        right="C",
    )

    row = ibkr_gex_market._option_row_from_ticker(
        contract,
        ticker,
        5000.0,
        risk_free_rate=0.052,
        dividend_yield=0.0,
    )

    assert row["iv_source"] == "reference_option_price_iv"
    assert row["reference_option_price"] == 25.0
    assert row["reference_option_price_source"] == "bid_ask_mid"
    assert row["greek_source"] == "local_iv"
    assert row["gamma"] > 0
    assert row["gex"] > 0


def test_gex_option_reference_provenance_exposes_market_price_close_fallback() -> None:
    assert select_gex_option_reference(
        bid=None,
        ask=None,
        market_price=2.4,
        last=2.5,
        close=2.4,
    ) == {
        "reference_option_price": 2.5,
        "reference_option_price_source": "last",
    }
    assert select_gex_option_reference(
        bid=None,
        ask=None,
        market_price=2.4,
        last=None,
        close=2.4,
    ) == {
        "reference_option_price": 2.4,
        "reference_option_price_source": "previous_close",
    }
    assert select_gex_option_reference(
        bid=None,
        ask=None,
        market_price=2.45,
        last=None,
        close=2.4,
    ) == {
        "reference_option_price": 2.45,
        "reference_option_price_source": "ibkr_market_price",
    }


def test_gex_broker_option_volume_preserves_zero_and_unavailable_state() -> None:
    assert (
        ibkr_gex_market._volume_from_ticker(
            SimpleNamespace(callVolume=9.0, volume=0.0),
        )
        == 0.0
    )
    assert (
        ibkr_gex_market._volume_from_ticker(
            SimpleNamespace(callVolume=99.0, volume=3.0),
        )
        == 3.0
    )
    assert ibkr_gex_market._volume_from_ticker(SimpleNamespace(callVolume=99.0)) is None

    row = ibkr_gex_market._option_row_from_ticker(
        _ibkr_option(con_id=1, expiry="20260710", strike=5000.0, right="C"),
        SimpleNamespace(),
        5000.0,
        risk_free_rate=0.052,
        dividend_yield=0.0,
    )

    assert row["volume"] is None
    assert row["open_interest"] is None
    assert row["gex"] is None
    assert not {
        "open_interest_live",
        "open_interest_source",
        "volume_available",
        "volume_source",
    }.intersection(row)


def test_gex_contract_selection_requires_exact_future_expiry_time() -> None:
    now = datetime(2026, 7, 15, 20, 30, tzinfo=UTC)
    expired = [
        _ibkr_option(
            con_id=index,
            expiry="20260715",
            expiry_at="2026-07-15T20:00:00+00:00",
            strike=5000,
            right=right,
            trading_class="E3D",
        )
        for index, right in ((1, "C"), (2, "P"))
    ]
    active = [
        _ibkr_option(
            con_id=index,
            expiry="20260716",
            expiry_at="2026-07-16T20:00:00+00:00",
            strike=5000,
            right=right,
            trading_class="E3D",
        )
        for index, right in ((3, "C"), (4, "P"))
    ]
    unknown = [
        _ibkr_option(
            con_id=index,
            expiry="20260717",
            strike=5000,
            right=right,
            trading_class="E3D",
        )
        for index, right in ((5, "C"), (6, "P"))
    ]

    with pytest.raises(
        ibkr_gex.IbkrOptionUniverseUnavailableError,
        match="missing exact option expiry metadata",
    ) as caught:
        ibkr_option_acquisition._filter_active_option_contracts(
            [*expired, *active, *unknown],
            now=now,
        )
    assert caught.value.reason == "EXPIRY_TIME_UNKNOWN"

    contracts, meta = ibkr_option_acquisition._filter_active_option_contracts(
        [*expired, *active],
        now=now,
    )

    assert contracts == active
    assert meta == {
        "requested_expiries": ["20260715", "20260716"],
        "expired_contracts_excluded": 2,
        "expired_series_excluded": 1,
        "expiry_time_unknown_contracts_excluded": 0,
        "expiry_time_unknown_series": 0,
        "selected_expiries": ["20260716"],
        "expirations": "20260716",
        "chain_labels": "CME/E3D/50",
        "qualified_strike_count": 1,
        "qualified_pair_count": 1,
    }


def test_gex_option_rows_return_partial_rows_on_deadline_after_batch(monkeypatch) -> None:
    now = 0.0

    def fake_monotonic():
        return now

    class FakeGreek:
        gamma = 0.01
        delta = 0.5
        impliedVol = 0.2

    class FakeTicker:
        modelGreeks = FakeGreek()
        callOpenInterest = None
        putOpenInterest = None
        tickerId = None

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})
        tickers: list[Any] = []

        def reqMktData(self, contract, *_args):
            ticker = FakeTicker()
            self.tickers.append(ticker)
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            nonlocal now
            now += float(value)
            for ticker in self.tickers:
                ticker.marketDataType = 1

    monkeypatch.setattr(ibkr_gex, "_time_monotonic", fake_monotonic)
    contracts = [
        _ibkr_option(
            con_id=index + 1,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=float(index + 1),
            right="C",
        )
        for index in range(4)
    ]

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        contracts,
        5000.0,
        0.5,
        batch_size=2,
        batch_pause_seconds=0.25,
        deadline=0.6,
    )

    assert ibkr_gex._gex_rows_timed_out(rows) is True
    assert len(ibkr_gex._gex_option_data_rows(rows)) == 2


def test_gex_option_rows_collects_each_expiration_separately() -> None:
    requests: list[tuple[str, float]] = []
    sleeps: list[float] = []
    tickers: list[Any] = []

    class FakeGreek:
        gamma = 0.01
        delta = 0.5
        impliedVol = 0.2

    class FakeTicker:
        callOpenInterest = 10
        putOpenInterest = 12
        tickerId = None

        def __init__(self, right: str):
            self.modelGreeks = SimpleNamespace(
                gamma=FakeGreek.gamma,
                delta=FakeGreek.delta if right == "C" else -FakeGreek.delta,
                impliedVol=FakeGreek.impliedVol,
            )
            self.ticks = [SimpleNamespace(tickType=27 if right == "C" else 28)]

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, contract, *_args):
            requests.append((contract.lastTradeDateOrContractMonth, contract.strike))
            ticker = FakeTicker(contract.right)
            tickers.append(ticker)
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            sleeps.append(value)
            for ticker in tickers:
                ticker.marketDataType = 1

    contracts = [
        _ibkr_option(
            con_id=1,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5005.0,
            right="C",
        ),
        _ibkr_option(
            con_id=2,
            expiry=_ACTIVE_OPTION_PRIOR_EXPIRY,
            expiry_at=_ACTIVE_OPTION_PRIOR_EXPIRY_AT,
            strike=5000.0,
            right="C",
        ),
        _ibkr_option(
            con_id=3,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5010.0,
            right="P",
        ),
        _ibkr_option(
            con_id=4,
            expiry=_ACTIVE_OPTION_PRIOR_EXPIRY,
            expiry_at=_ACTIVE_OPTION_PRIOR_EXPIRY_AT,
            strike=4995.0,
            right="P",
        ),
    ]

    rows = ibkr_gex._collect_option_rows_by_expiration(
        FakeIb(),
        contracts,
        5000.0,
        0.5,
        batch_size=8,
        batch_pause_seconds=0.25,
    )

    assert len(rows) == 4
    assert requests == [
        (_ACTIVE_OPTION_PRIOR_EXPIRY, 4995.0),
        (_ACTIVE_OPTION_PRIOR_EXPIRY, 5000.0),
        (_ACTIVE_OPTION_EXPIRY, 5005.0),
        (_ACTIVE_OPTION_EXPIRY, 5010.0),
    ]
    assert sleeps == [
        0.1,
        0.15,
        0.25,
        0.1,
        0.15,
        0.25,
        0.1,
        0.15,
        0.25,
        0.1,
        0.15,
    ]


def test_gex_expiration_contracts_split_by_strike_halves() -> None:
    contracts = [
        _ibkr_option(con_id=1, expiry="20260612", strike=5000.0, right="C"),
        _ibkr_option(con_id=2, expiry="20260612", strike=5000.0, right="P"),
        _ibkr_option(con_id=3, expiry="20260612", strike=5005.0, right="C"),
        _ibkr_option(con_id=4, expiry="20260612", strike=5005.0, right="P"),
        _ibkr_option(con_id=5, expiry="20260612", strike=5010.0, right="C"),
        _ibkr_option(con_id=6, expiry="20260612", strike=5010.0, right="P"),
        _ibkr_option(con_id=7, expiry="20260612", strike=5015.0, right="C"),
        _ibkr_option(con_id=8, expiry="20260612", strike=5015.0, right="P"),
    ]

    segments = ibkr_gex._split_expiration_contracts_by_strike_halves(contracts)

    assert [[contract.strike for contract in segment] for segment in segments] == [
        [5000.0, 5000.0, 5005.0, 5005.0],
        [5010.0, 5010.0, 5015.0, 5015.0],
    ]


def test_gex_streaming_option_batch_plan_uses_exact_pacing_contract() -> None:
    size, pause = ibkr_gex._gex_option_batch_plan(
        contract_count=24,
        requested_batch_size=8,
        requested_pause_seconds=0.25,
    )

    assert size == 8
    assert pause == 0.25
    assert ibkr_gex._gex_option_batch_plan(3, 8, 0.25) == (3, 0.25)
    with pytest.raises(ValueError, match="batch size"):
        ibkr_gex._gex_option_batch_plan(24, 12, 0.25)
    with pytest.raises(ValueError, match="batch pause"):
        ibkr_gex._gex_option_batch_plan(24, 8, 0.05)


def test_gex_option_rows_use_streaming_generic_ticks_and_cancel() -> None:
    requests: list[tuple[float, str, bool, bool]] = []
    cancellations: list[float] = []
    sleeps: list[float] = []
    tickers: list[Any] = []

    class FakeGreek:
        gamma = 0.02
        delta = 0.4
        impliedVol = 0.25

    class FakeTicker:
        callOpenInterest = 7
        putOpenInterest = 7
        volume = 3
        bid = 1.0
        ask = 1.2
        last = 1.1
        tickerId = None

        def __init__(self, right: str):
            self.modelGreeks = SimpleNamespace(
                gamma=FakeGreek.gamma,
                delta=FakeGreek.delta if right == "C" else -FakeGreek.delta,
                impliedVol=FakeGreek.impliedVol,
            )
            self.ticks = [SimpleNamespace(tickType=27 if right == "C" else 28)]

    class FakeIb:
        def __init__(self):
            self.wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, contract, generic_ticks, snapshot, regulatory_snapshot):
            requests.append((contract.strike, generic_ticks, snapshot, regulatory_snapshot))
            ticker = FakeTicker(contract.right)
            ticker.tickerId = len(tickers) + 1
            tickers.append(ticker)
            self.wrapper.tickerIdMap[ticker.tickerId] = ticker
            return ticker

        def cancelMktData(self, contract):
            cancellations.append(contract.strike)
            return True

        def isConnected(self):
            return True

        def sleep(self, value):
            sleeps.append(value)
            for ticker in tickers:
                ticker.marketDataType = 1

    contracts = [
        _ibkr_option(
            con_id=1,
            symbol="SPY",
            sec_type="OPT",
            exchange="SMART",
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=500.0,
            right="C",
            multiplier="100",
        ),
        _ibkr_option(
            con_id=2,
            symbol="SPY",
            sec_type="OPT",
            exchange="SMART",
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=500.0,
            right="P",
            multiplier="100",
        ),
    ]

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        contracts,
        500.0,
        0.5,
        batch_size=2,
        batch_pause_seconds=0.25,
    )

    assert requests == [
        (500.0, "101", False, False),
        (500.0, "101", False, False),
    ]
    assert cancellations == [500.0, 500.0]
    assert sleeps == [0.1, 0.15]
    assert [row["open_interest"] for row in rows] == [7, 7]
    assert [row["volume"] for row in rows] == [3, 3]
    assert rows[0]["gex"] > 0
    assert rows[1]["gex"] < 0


def test_gex_option_open_interest_ignores_futures_only_generic_field() -> None:
    ticker = SimpleNamespace(
        callOpenInterest=None,
        putOpenInterest=None,
        openInterest=700,
    )

    assert ibkr_gex_market._open_interest_from_ticker(ticker, OptionRight.CALL) is None
    assert ibkr_gex_market._open_interest_from_ticker(ticker, OptionRight.PUT) is None


def test_gex_ticker_observation_does_not_refresh_replayed_oi_or_greeks() -> None:
    first_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=10)
    third_at = second_at + timedelta(seconds=10)
    first_oi_tick = SimpleNamespace(
        tickType=27,
        time=first_at,
        price=120.0,
        size=0.0,
    )
    first_model = SimpleNamespace(gamma=0.01, delta=0.5)
    ticker = SimpleNamespace(
        time=first_at,
        modelGreeks=first_model,
        ticks=[first_oi_tick],
        marketDataType=1,
    )
    tracker = ibkr_gex_market.GexTickerObservationTracker(right=OptionRight.CALL)

    tracker.observe(ticker)
    first_evidence = tracker.evidence(ticker)
    assert first_evidence["model_greeks_received_at"] == first_at.isoformat()
    assert first_evidence["open_interest_received_at"] == first_at.isoformat()

    ticker.time = second_at
    ticker.ticks = [
        first_oi_tick,
        SimpleNamespace(
            tickType=4,
            time=second_at,
            price=1.5,
            size=1.0,
        ),
    ]
    tracker.observe(ticker)
    replay_evidence = tracker.evidence(ticker)
    assert replay_evidence["model_greeks_received_at"] == first_at.isoformat()
    assert replay_evidence["open_interest_received_at"] == first_at.isoformat()

    ticker.modelGreeks = SimpleNamespace(gamma=0.011, delta=0.51)
    tracker.observe(ticker)
    changed_model_evidence = tracker.evidence(ticker)
    assert changed_model_evidence["model_greeks_received_at"] == second_at.isoformat()
    assert changed_model_evidence["open_interest_received_at"] == first_at.isoformat()

    second_oi_tick = SimpleNamespace(
        tickType=27,
        time=third_at,
        price=125.0,
        size=0.0,
    )
    ticker.time = third_at
    ticker.ticks = [first_oi_tick, second_oi_tick]
    tracker.observe(ticker)
    changed_oi_evidence = tracker.evidence(ticker)
    assert changed_oi_evidence["model_greeks_received_at"] == second_at.isoformat()
    assert changed_oi_evidence["open_interest_received_at"] == third_at.isoformat()
    assert tracker.observed_tick_packet == tuple(ticker.ticks)


def test_gex_ticker_observation_cursor_is_bounded_to_the_latest_packet() -> None:
    first_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    ticker = SimpleNamespace(
        time=first_at,
        modelGreeks=None,
        ticks=[
            SimpleNamespace(
                tickType=27,
                time=first_at + timedelta(milliseconds=index),
                price=float(index),
                size=0.0,
            )
            for index in range(64)
        ],
        marketDataType=1,
    )
    tracker = ibkr_gex_market.GexTickerObservationTracker(right=OptionRight.CALL)

    tracker.observe(ticker)
    last_first_packet_at = ticker.ticks[-1].time
    assert tracker.take_provider_callback_activity() == (1, 64)
    assert tracker.take_provider_callback_activity() == (0, 0)
    assert tracker.evidence(ticker)["open_interest_received_at"] == last_first_packet_at.isoformat()
    assert len(tracker.observed_tick_packet) == 64

    replayed_tick = ticker.ticks[-1]
    second_packet_at = first_at + timedelta(seconds=1)
    new_oi_tick = SimpleNamespace(
        tickType=27,
        time=second_packet_at,
        price=65.0,
        size=0.0,
    )
    ticker.time = second_packet_at
    ticker.ticks = [
        replayed_tick,
        SimpleNamespace(
            tickType=4,
            time=second_packet_at,
            price=1.0,
            size=1.0,
        ),
        new_oi_tick,
    ]

    tracker.observe(ticker, provider_callback=False)

    assert tracker.evidence(ticker)["open_interest_received_at"] == second_packet_at.isoformat()
    assert tracker.observed_tick_packet == tuple(ticker.ticks)
    assert len(tracker.observed_tick_packet) == 3
    assert tracker.take_provider_callback_activity() == (0, 0)


def test_gex_option_stream_uses_code_owned_generic_tick_101(monkeypatch) -> None:

    class FakeIb:
        def reqMarketDataType(self, _data_type):
            return None

        def isConnected(self):
            return True

    contracts = [
        _ibkr_option(
            con_id=1,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5000.0,
            right="C",
        ),
        _ibkr_option(
            con_id=2,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5000.0,
            right="P",
        ),
    ]

    def fake_rows(_ib, _contracts, _spot, _wait_seconds, **_kwargs):
        return _with_gex_test_contract_observations(
            [
                {
                    "con_id": 1,
                    "expiry": _ACTIVE_OPTION_EXPIRY,
                    "trading_class": "ES",
                    "exchange": "CME",
                    "multiplier": 50.0,
                    "strike": 5000.0,
                    "right": "C",
                    "gamma": 0.01,
                    "open_interest": 12.0,
                    "volume": None,
                    "reference_option_price": None,
                    "reference_option_price_source": "unavailable",
                    "gex": 1_500_000.0,
                    "abs_gex": 1_500_000.0,
                    "greek_source": "modelGreeks",
                    "_ibkr_market_data_type": 1,
                },
                {
                    "con_id": 2,
                    "expiry": _ACTIVE_OPTION_EXPIRY,
                    "trading_class": "ES",
                    "exchange": "CME",
                    "multiplier": 50.0,
                    "strike": 5000.0,
                    "right": "P",
                    "gamma": 0.01,
                    "open_interest": 10.0,
                    "volume": None,
                    "reference_option_price": None,
                    "reference_option_price_source": "unavailable",
                    "gex": -1_250_000.0,
                    "abs_gex": 1_250_000.0,
                    "greek_source": "modelGreeks",
                    "_ibkr_market_data_type": 1,
                },
            ],
            observed_at=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: SimpleNamespace(
            conId=1,
            secType="FUT",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (
            contracts,
            {
                "qualified_contracts": 2,
                "qualified_strike_count": 1,
                "qualified_pair_count": 1,
            },
        ),
    )
    monkeypatch.setattr(ibkr_gex, "_collect_option_rows_by_expiration", fake_rows)

    result = _collect_through_gex_lane(
        underlying_quote=_gex_test_spot_observation(
            5000.0,
            observed_at=datetime.now(tz=UTC),
        ),
        request=replace(
            gex.gex_request_config(instrument=_ES),
            strike_count=1,
            max_contracts=4,
        ),
        instrument=ibkr_future_payload("ES"),
    )

    assert result["status"] == "ok"
    assert result["instrument_id"] == _instrument_id(_ES)
    assert result["route_fingerprint"] == _route_key(_ES)
    assert gex.GEX_GENERIC_TICKS == "101"


def test_gex_option_rows_compute_local_gamma_from_ticker_iv() -> None:
    requests: list[str] = []
    tickers: list[Any] = []
    expiry_at = datetime.now(tz=UTC) + timedelta(days=1)
    expiry_code = expiry_at.strftime("%Y%m%d")

    class FakeTicker:
        modelGreeks = None
        impliedVolatility = 0.22
        callOpenInterest = 12
        putOpenInterest = 0
        tickerId = None

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def reqMktData(self, contract, generic_ticks, snapshot, regulatory_snapshot):
            requests.append(generic_ticks)
            ticker = FakeTicker()
            tickers.append(ticker)
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, _value):
            for ticker in tickers:
                ticker.marketDataType = 1

    contracts = [
        _ibkr_option(
            con_id=1,
            expiry=expiry_code,
            expiry_at=expiry_at,
            strike=5000.0,
            right="C",
        ),
    ]

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        contracts,
        5000.0,
        0.5,
        batch_size=8,
        batch_pause_seconds=0.25,
    )

    assert requests == ["101"]
    assert rows[0]["greek_source"] == "local_iv"
    assert rows[0]["iv_source"] == "ticker_iv"
    assert rows[0]["gamma"] > 0
    assert rows[0]["gex"] > 0


def test_gex_option_collection_never_uses_historical_data_or_snapshots() -> None:
    requests: list[tuple[str, bool]] = []
    tickers: list[Any] = []

    class FakeTicker:
        modelGreeks = SimpleNamespace(gamma=0.01, delta=0.5, impliedVol=0.2)
        callOpenInterest = 10
        putOpenInterest = 12
        tickerId = None

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def reqHistoricalData(self, *_args, **_kwargs):
            raise AssertionError("GEX option OI must never use historical requests")

        def reqMktData(self, contract, generic_ticks, snapshot, _regulatory_snapshot):
            requests.append((generic_ticks, snapshot))
            ticker = FakeTicker()
            tickers.append(ticker)
            return ticker

        def cancelMktData(self, _contract):
            return True

        def isConnected(self):
            return True

        def sleep(self, _seconds):
            for ticker in tickers:
                ticker.marketDataType = 1

    contracts = [
        _ibkr_option(
            con_id=101,
            symbol="SPY",
            sec_type="OPT",
            exchange="SMART",
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=600.0,
            right="C",
            multiplier="100",
        ),
        _ibkr_option(
            con_id=102,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=6000.0,
            right="P",
        ),
    ]

    rows = ibkr_gex._collect_option_rows(
        FakeIb(),
        contracts,
        600.0,
        0.5,
        batch_size=2,
    )

    assert len(rows) == 2
    assert requests == [("101", False), ("101", False)]


def test_live_gex_refresh_uses_streaming_only_request_contract(monkeypatch, tmp_path) -> None:
    calls: list[bool] = []

    class FakeIb:
        def reqMarketDataType(self, _data_type):
            return None

        def isConnected(self):
            return True

    contracts = [
        _ibkr_option(
            con_id=1,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5000.0,
            right="C",
        ),
        _ibkr_option(
            con_id=2,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5000.0,
            right="P",
        ),
    ]

    def fake_rows(_ib, _contracts, spot, _wait_seconds, **kwargs):
        calls.append("snapshot" in kwargs)
        return _with_gex_test_contract_observations(
            [
                {
                    "con_id": 1,
                    "expiry": _ACTIVE_OPTION_EXPIRY,
                    "trading_class": "ES",
                    "exchange": "CME",
                    "multiplier": 50.0,
                    "strike": 5000.0,
                    "right": "C",
                    "gamma": 0.01,
                    "open_interest": 10.0,
                    "volume": None,
                    "reference_option_price": None,
                    "reference_option_price_source": "unavailable",
                    "gex": 1_250_000.0,
                    "abs_gex": 1_250_000.0,
                    "greek_source": "modelGreeks",
                    "_ibkr_market_data_type": 1,
                },
                {
                    "con_id": 2,
                    "expiry": _ACTIVE_OPTION_EXPIRY,
                    "trading_class": "ES",
                    "exchange": "CME",
                    "multiplier": 50.0,
                    "strike": 5000.0,
                    "right": "P",
                    "gamma": 0.01,
                    "open_interest": 8.0,
                    "volume": None,
                    "reference_option_price": None,
                    "reference_option_price_source": "unavailable",
                    "gex": -1_000_000.0,
                    "abs_gex": 1_000_000.0,
                    "greek_source": "modelGreeks",
                    "_ibkr_market_data_type": 1,
                },
            ],
            observed_at=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: SimpleNamespace(
            conId=1,
            secType="FUT",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (
            contracts,
            {
                "qualified_contracts": 2,
                "qualified_strike_count": 1,
                "qualified_pair_count": 1,
            },
        ),
    )
    monkeypatch.setattr(ibkr_gex, "_collect_option_rows", fake_rows)
    result = _collect_through_gex_lane(
        underlying_quote=_gex_test_spot_observation(
            5000.0,
            observed_at=datetime.now(tz=UTC),
        ),
        request=replace(
            gex.gex_request_config(instrument=_ES),
            strike_count=1,
            max_contracts=4,
        ),
        instrument=ibkr_future_payload("ES"),
    )

    assert calls == [False]
    assert result["status"] == "ok"
    assert result["diagnostics"]["diag_option_rows"] == 2


def test_live_gex_refresh_does_not_retry_or_publish_empty_option_rows(
    monkeypatch, tmp_path
) -> None:
    calls: list[bool] = []
    sleeps: list[float] = []
    saved: list[str] = []

    class FakeIb:
        def reqMarketDataType(self, _data_type):
            return None

        def sleep(self, seconds):
            sleeps.append(seconds)

        def isConnected(self):
            return True

    contracts = [
        _ibkr_option(
            con_id=1,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5000.0,
            right="C",
        ),
        _ibkr_option(
            con_id=2,
            expiry=_ACTIVE_OPTION_EXPIRY,
            expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
            strike=5000.0,
            right="P",
        ),
    ]

    def fake_rows(_ib, _contracts, spot, _wait_seconds, **kwargs):
        calls.append("snapshot" in kwargs)
        return []

    monkeypatch.setattr(
        ibkr_gex,
        "_connected_gex_ib",
        lambda _request, **_kwargs: FakeIb(),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_qualified_underlying",
        lambda _ib, **_kwargs: SimpleNamespace(
            conId=1,
            secType="FUT",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        ibkr_gex,
        "_build_option_contracts",
        lambda *_args, **_kwargs: (
            contracts,
            {
                "qualified_contracts": 2,
                "qualified_strike_count": 1,
                "qualified_pair_count": 1,
            },
        ),
    )
    monkeypatch.setattr(ibkr_gex, "_collect_option_rows", fake_rows)
    monkeypatch.setattr(
        gex_context_module,
        "persist_gex_snapshot",
        lambda provider_symbol, *_args, **_kwargs: saved.append(provider_symbol),
    )

    result = _collect_through_gex_lane(
        underlying_quote=_gex_test_spot_observation(
            5000.0,
            observed_at=datetime.now(tz=UTC),
        ),
        request=replace(
            gex.gex_request_config(instrument=_ES),
            strike_count=1,
            max_contracts=4,
        ),
        instrument=ibkr_future_payload("ES"),
    )

    assert calls == [False]
    assert sleeps == []
    assert saved == []
    assert result["status"] == "partial"
    assert result["decision_authoritative"] is False
    assert result["levels"] == []
    assert result["global_gamma_regime"] == "UNKNOWN"
    assert "profile" not in result
    assert "nearest" not in result
    assert "raw" not in result
    assert result["diagnostics"]["diag_strikes_requested"] == 1
    assert "strikes 0/1" in result["message"]


def test_gex_option_cancel_treats_missing_provider_request_as_confirmed_absent() -> None:
    attempts: list[float] = []

    class FakeTicker:
        tickerId = 42

    class FakeIb:
        wrapper = SimpleNamespace(tickerIdMap={})

        def cancelMktData(self, contract):
            attempts.append(contract.strike)
            return False

    cancelled = ibkr_gex._cancel_ibkr_market_data(
        FakeIb(),
        FakeTicker(),
        SimpleNamespace(strike=7550.0),
    )

    assert cancelled is True
    assert attempts == []


def test_gex_option_collection_resets_session_when_cancel_is_ambiguous(
    monkeypatch,
) -> None:
    resets: list[bool] = []

    class FakeTicker:
        modelGreeks = SimpleNamespace(gamma=0.01, delta=0.5, impliedVol=0.2)
        callOpenInterest = 10
        marketDataType = 1
        tickerId = 42
        ticks = [SimpleNamespace(tickType=27)]

    class FakeIb:
        def __init__(self):
            self.ticker = FakeTicker()
            self.wrapper = SimpleNamespace(tickerIdMap={42: self.ticker})

        def reqMktData(self, _contract, *_args):
            return self.ticker

        def cancelMktData(self, _contract):
            return False

        def isConnected(self):
            return True

        def sleep(self, _seconds):
            return None

    monkeypatch.setattr(
        ibkr_gex,
        "_reset_gex_session",
        lambda: resets.append(True),
    )

    with pytest.raises(
        RuntimeError,
        match="cancellation was not confirmed",
    ):
        ibkr_gex._collect_option_rows(
            FakeIb(),
            [
                _ibkr_option(
                    con_id=1,
                    expiry=_ACTIVE_OPTION_EXPIRY,
                    expiry_at=_ACTIVE_OPTION_EXPIRY_AT,
                    strike=5000.0,
                    right="C",
                )
            ],
            5000.0,
            0.5,
        )

    assert resets == [True]
