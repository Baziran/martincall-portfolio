from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from aef_terminal.domain import Bar
from aef_terminal.data.providers import route_instrument
from aef_terminal.indicators.modules.ai_third_opinion.router import (
    create_router as create_ai_third_opinion_router,
)
from aef_terminal.runtime import chart_events
from aef_terminal.runtime.timeframes import interval_minutes
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from tests.provider_payloads import ibkr_future_payload
from aef_terminal.ui import market_actions
from aef_terminal.ui.routers.market import MarketRouterDeps, create_market_router
from aef_terminal.ui.route_selection import RouteSelectionError, RouteSelectionMismatch
from aef_terminal.ui.services.chart_history import (
    ConfirmedChartHistoryLoad,
    ConfirmedChartHistoryPage,
)
from aef_terminal.ui.services import market_analysis_job
from aef_terminal.ui.services.market_analysis_store import build_market_analysis_snapshot_response
from tests.provider_payloads import coinbase_btc_payload, ibkr_stock_payload


def test_market_route_cancellation_settles_physical_identity_lookup(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_lookup(_instrument_id: str) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return ibkr_future_payload("ES")

    monkeypatch.setattr(market_actions, "lookup_runtime_instrument", blocking_lookup)

    async def scenario() -> None:
        task = asyncio.create_task(
            market_actions.resolve_market_route_or_http_error(
                "instrument-v1",
                "route-v1",
                interval="5m",
                range_="1d",
            )
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert finished.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() is True

    asyncio.run(scenario())


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _request(**query_params: object) -> SimpleNamespace:
    return SimpleNamespace(query_params=query_params)


def _analysis_version_bar(
    ts: str,
    *,
    open_price: float = 100.0,
    high: float = 102.0,
    low: float = 99.0,
    close: float = 101.0,
    volume: float = 25.0,
) -> dict[str, Any]:
    return {
        "symbol": "ES",
        "ts": ts,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "timeframe": "5m",
        "source": "ibkr",
        "closed": True,
        "state": "confirmed",
    }


def _chart_history_load(
    bars: Any,
    warning: str = "",
    *,
    state: str = "complete",
    canonical_generation: int = 0,
) -> ConfirmedChartHistoryLoad:
    return ConfirmedChartHistoryLoad(
        tuple(bars),
        warning,
        {
            "requested_range": "1d",
            "requested_start": "2026-01-01T00:00:00+00:00",
            "requested_end": "2026-01-02T00:00:00+00:00",
            "state": state,
            "schedule_state": "verified",
            "verified_from": "2026-01-01T00:00:00+00:00" if state == "complete" else None,
            "pending_from": None if state == "complete" else "2026-01-01T00:00:00+00:00",
            "pending_to": None if state == "complete" else "2026-01-02T00:00:00+00:00",
            "pending_cursor": None,
            "truncated": state != "complete",
            "has_older": state != "complete",
            "repair": {
                "attempted": state != "complete",
                "status": "ok" if state == "complete" else "partial",
                "code": None,
                "terminal_no_data": False,
                "remaining_missing_slots": 0 if state == "complete" else 1,
            },
        },
        canonical_generation,
    )


class _MarketStore:
    def __init__(self, drawings: list[dict[str, Any]] | None = None) -> None:
        self.drawings = list(drawings or [])

    @staticmethod
    def initialize() -> None:
        return None

    @staticmethod
    def read_trading_hours(*, instrument: dict[str, Any]) -> None:
        assert instrument["instrument_id"]
        return None

    def read_drawings(
        self,
        _instrument_id: str,
        _interval: str,
        *,
        route_fingerprint: str,
    ) -> list[dict[str, Any]]:
        assert route_fingerprint
        return list(self.drawings)

    def read_bar_slots(self, **kwargs: Any) -> dict[datetime, int]:
        return {kwargs["start"]: 0}

    def get_full_snapshot_context(self, **kwargs: Any) -> dict[str, Any]:
        bars = list(kwargs["bars"])
        step = interval_minutes(str(kwargs["interval"]))
        return {
            "bars": bars,
            "bar_slots": ProviderBarSlotSequence(
                [index * step for index, _bar in enumerate(bars)],
                schedule_state="verified",
            ),
            "mtf_context": {},
            "mtf_context_slots": {},
            "mtf_context_quality": {},
        }

    @staticmethod
    def read_futures_roll_events(**_kwargs: Any) -> list[dict[str, Any]]:
        return []


def test_market_http_range_parameter_maps_to_internal_range_contract() -> None:
    router = create_market_router(_market_deps())
    route = next(route for route in router.routes if getattr(route, "path", "") == "/api/market")
    range_param = next(param for param in route.dependant.query_params if param.name == "range_")

    assert range_param.alias == "range"


@pytest.mark.parametrize("invalid_signal_range", ("", "5d", "14d", "off", " 2d "))
def test_market_http_rejects_invalid_signal_range(invalid_signal_range: str) -> None:
    app = FastAPI()
    app.include_router(create_market_router(_market_deps(server_sleeping=lambda: True)))
    route = route_instrument(coinbase_btc_payload())

    with TestClient(app) as client:
        response = client.get(
            "/api/market",
            params={
                "instrument_id": route.instrument_id,
                "expected_route_fingerprint": route.fingerprint,
                "signal_range": invalid_signal_range,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "signal_range"]


def test_market_history_page_returns_bounded_generation_fenced_payload(monkeypatch) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("SPY")
        route = route_instrument(instrument)
        boundary = datetime(2026, 1, 2, 14, 0, tzinfo=UTC)
        bar = Bar(
            "SPY",
            datetime(2026, 1, 2, 13, 55, tzinfo=UTC),
            100,
            101,
            99,
            100,
            10,
            "5m",
        )
        captured: dict[str, Any] = {}

        async def load_page(requested_instrument, interval, **kwargs):
            captured.update(
                instrument=requested_instrument,
                interval=interval,
                **kwargs,
            )
            return ConfirmedChartHistoryPage(
                bars=(bar,),
                next_before_ts=bar.ts,
                has_more=True,
                canonical_generation=7,
            )

        monkeypatch.setattr(
            market_actions,
            "lookup_runtime_instrument",
            lambda instrument_id: instrument if instrument_id == route.instrument_id else None,
        )
        monkeypatch.setattr(
            market_actions,
            "load_confirmed_chart_history_page",
            load_page,
        )
        endpoint = _route_endpoint(
            create_market_router(_market_deps()),
            "/api/market/history-page",
            "GET",
        )

        payload = await endpoint(
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            before_ts=boundary,
            limit=600,
            expected_canonical_generation=7,
        )

        assert payload["instrument_id"] == route.instrument_id
        assert payload["route_fingerprint"] == route.fingerprint
        assert payload["canonical_generation"] == 7
        assert payload["has_more"] is True
        assert payload["next_before_ts"] == bar.ts.isoformat()
        assert [item["ts"] for item in payload["bars"]] == [bar.ts.isoformat()]
        assert captured["before_ts"] == boundary
        assert captured["limit"] == 600
        assert captured["expected_canonical_generation"] == 7

    asyncio.run(run())


def test_market_http_accepts_vsa_render_hours_query_string() -> None:
    captured_analysis: dict[str, Any] = {}

    async def analysis_snapshot(_key: str, **kwargs: Any) -> dict[str, Any]:
        captured_analysis.update(kwargs)
        return {"ok": False, "status": "missing"}

    app = FastAPI()
    app.include_router(
        create_market_router(
            _market_deps(
                server_sleeping=lambda: True,
                market_analysis_snapshot=analysis_snapshot,
            )
        )
    )
    route = route_instrument(coinbase_btc_payload())
    common_params = {
        "instrument_id": route.instrument_id,
        "expected_route_fingerprint": route.fingerprint,
        "vsa_render_hours": "12",
    }

    with TestClient(app) as client:
        market_response = client.get("/api/market", params=common_params)
        analysis_response = client.get(
            "/api/market/analysis",
            params={"key": "analysis-key", **common_params},
        )
        invalid_response = client.get(
            "/api/market",
            params={**common_params, "vsa_render_hours": "8"},
        )

    assert market_response.status_code == 200
    assert market_response.json()["meta"]["source"] == "server:sleep"
    assert analysis_response.status_code == 200
    assert captured_analysis["vsa_render_hours"] == 12
    assert invalid_response.status_code == 422
    assert invalid_response.json()["detail"][0]["loc"] == ["query", "vsa_render_hours"]


@pytest.mark.parametrize("chart_only", (False, True))
def test_market_rejects_unsafe_history_before_any_store_access(
    chart_only: bool,
) -> None:
    async def run() -> None:
        store_calls = 0

        def store_factory() -> _MarketStore:
            nonlocal store_calls
            store_calls += 1
            return _MarketStore()

        endpoint = _route_endpoint(
            create_market_router(_market_deps(store_factory=store_factory)),
            "/api/market",
            "GET",
        )
        route = route_instrument(ibkr_future_payload("ES"))

        with pytest.raises(HTTPException) as raised:
            await endpoint(
                _request(),
                instrument_id=route.instrument_id,
                expected_route_fingerprint=route.fingerprint,
                interval="1m",
                range_="2mo",
                chart_only=chart_only,
            )

        assert raised.value.status_code == 422
        assert raised.value.detail["error"]["code"] == "CHART_HISTORY_RANGE_TOO_LARGE"
        assert raised.value.detail["error"]["retryable"] is False
        assert store_calls == 0

    asyncio.run(run())


def test_screener_router_accepts_only_typed_routes_query() -> None:
    captured: dict[str, Any] = {}
    expected = {
        "cache_epoch": "00000000-0000-4000-8000-000000000001",
        "cache_generation": 7,
        "rows": [],
    }

    async def screener_snapshot(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return expected

    router = create_market_router(_market_deps(screener_snapshot=screener_snapshot))
    route = next(route for route in router.routes if getattr(route, "path", "") == "/api/screener")
    endpoint = route.endpoint
    routes = '[{"instrument_id":"id,a|b","route_fingerprint":"route,a|b"}]'

    assert asyncio.run(endpoint(routes=routes, interval="5m")) == expected
    assert captured == {"routes": routes, "interval": "5m"}
    assert [param.name for param in route.dependant.query_params] == ["routes", "interval"]


def test_screener_router_reports_invalid_typed_routes_as_422() -> None:
    async def reject_routes(**_kwargs: Any) -> dict[str, Any]:
        raise RouteSelectionError("ROUTES_JSON_INVALID")

    endpoint = _route_endpoint(
        create_market_router(_market_deps(screener_snapshot=reject_routes)),
        "/api/screener",
        "GET",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint(routes="legacy,parallel", interval="5m"))

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "ok": False,
        "error": {
            "code": "ROUTES_JSON_INVALID",
            "category": "market",
            "retryable": False,
            "message": "ROUTES_JSON_INVALID",
        },
    }


@pytest.mark.parametrize("path", ["/api/screener", "/api/screener/trends"])
def test_screener_routes_report_stale_route_generation_as_retryable_conflict(path: str) -> None:
    async def reject_routes(**_kwargs: Any) -> dict[str, Any]:
        raise RouteSelectionMismatch(
            (("instrument-1", "route-old"),),
            (("instrument-1", "route-current"),),
        )

    overrides = (
        {"screener_snapshot": reject_routes}
        if path == "/api/screener"
        else {"screener_trends_snapshot": reject_routes}
    )
    endpoint = _route_endpoint(create_market_router(_market_deps(**overrides)), path, "GET")

    with pytest.raises(HTTPException) as exc_info:
        if path == "/api/screener":
            asyncio.run(endpoint(routes="[]", interval="5m"))
        else:
            asyncio.run(endpoint(routes="[]"))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == {
        "code": "ROUTE_SELECTION_MISMATCH",
        "category": "market",
        "retryable": True,
        "message": (
            'ROUTE_SELECTION_MISMATCH expected=[["instrument-1", "route-old"]] '
            'actual=[["instrument-1", "route-current"]]'
        ),
    }
    assert exc_info.value.detail["expected_routes"] == (("instrument-1", "route-old"),)
    assert exc_info.value.detail["actual_routes"] == (("instrument-1", "route-current"),)


def test_screener_trends_router_has_exact_routes_only_object_contract() -> None:
    captured: dict[str, Any] = {}

    async def screener_trends_snapshot(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"window_minutes": 180, "bucket_minutes": 5, "rows": []}

    router = create_market_router(_market_deps(screener_trends_snapshot=screener_trends_snapshot))
    route = next(
        route for route in router.routes if getattr(route, "path", "") == "/api/screener/trends"
    )
    routes = '[{"instrument_id":"id,a|b","route_fingerprint":"route,a|b"}]'

    assert asyncio.run(route.endpoint(routes=routes)) == {
        "window_minutes": 180,
        "bucket_minutes": 5,
        "rows": [],
    }
    assert captured == {"routes": routes}
    assert [param.name for param in route.dependant.query_params] == ["routes"]


def test_screener_trends_router_reports_invalid_exact_routes_as_422() -> None:
    async def reject_routes(**_kwargs: Any) -> dict[str, Any]:
        raise RouteSelectionError("ROUTES_JSON_INVALID")

    endpoint = _route_endpoint(
        create_market_router(_market_deps(screener_trends_snapshot=reject_routes)),
        "/api/screener/trends",
        "GET",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint(routes="legacy,parallel"))

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "ok": False,
        "error": {
            "code": "ROUTES_JSON_INVALID",
            "category": "market",
            "retryable": False,
            "message": "ROUTES_JSON_INVALID",
        },
    }


@pytest.fixture(autouse=True)
def _persisted_runtime_instruments(monkeypatch) -> None:
    instruments = [
        coinbase_btc_payload(),
        ibkr_stock_payload("SPY", con_id=756733),
        ibkr_stock_payload("QQQ", con_id=320227571),
        ibkr_stock_payload("PLTR", con_id=444857942),
        ibkr_stock_payload("NVDA", con_id=4815747),
        ibkr_future_payload("ES"),
    ]
    by_instrument_id = {
        route_instrument(instrument).instrument_id: instrument for instrument in instruments
    }

    def lookup(instrument_id):
        if instrument_id not in by_instrument_id:
            raise ValueError(f"missing instrument_id={instrument_id}")
        return by_instrument_id[instrument_id]

    monkeypatch.setattr(market_actions, "lookup_runtime_instrument", lookup)


def _market_deps(**overrides: Any) -> MarketRouterDeps:
    async def apply_settings() -> dict[str, Any]:
        return {}

    async def register_wanted(
        _key: str,
        _payload: dict[str, Any],
        _client_id: str,
        _lease_sequence: int,
    ) -> str:
        return "queued"

    async def renew_lease(
        _client_id: str,
        _lease_sequence: int,
        _key: str,
        _instrument_id: str,
        _route_fingerprint: str,
    ) -> bool:
        return True

    async def refresh_analysis(
        _client_id: str,
        _lease_sequence: int,
        _key: str,
        _instrument_id: str,
        _route_fingerprint: str,
        _client_version: str,
    ) -> bool:
        return True

    async def release_lease(
        _client_id: str,
        _lease_sequence: int,
    ) -> bool:
        return True

    async def analysis_snapshot(_key: str, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "status": "missing"}

    async def screener_snapshot(**_kwargs: Any) -> dict[str, Any]:
        return {
            "cache_epoch": "00000000-0000-4000-8000-000000000001",
            "cache_generation": 0,
            "rows": [],
        }

    async def screener_trends_snapshot(**_kwargs: Any) -> dict[str, Any]:
        return {"window_minutes": 180, "bucket_minutes": 5, "rows": []}

    defaults = {
        "store_factory": _MarketStore,
        "normalize_drawing_anchors": (
            lambda _store, _instrument_id, _interval, drawings, **_kwargs: drawings
        ),
        "apply_ibkr_runtime_settings_async": apply_settings,
        "server_sleeping": lambda: False,
        "server_sleep_status": lambda: {"sleeping": False},
        "note_active_chart": lambda *_args: None,
        "market_indicator_params": lambda **_kwargs: {},
        "market_analysis_payload": lambda **_kwargs: {"symbol": "ES"},
        "market_analysis_key": lambda payload: "analysis-key",
        "register_market_analysis_wanted": register_wanted,
        "renew_market_analysis_lease": renew_lease,
        "refresh_market_analysis": refresh_analysis,
        "release_market_analysis_lease": release_lease,
        "market_analysis_snapshot": analysis_snapshot,
        "screener_snapshot": screener_snapshot,
        "screener_trends_snapshot": screener_trends_snapshot,
    }
    defaults.update(overrides)
    return MarketRouterDeps(**defaults)


def test_market_endpoint_sleep_error_is_provider_neutral() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_market_router(_market_deps(server_sleeping=lambda: True)), "/api/market", "GET"
        )
        route = route_instrument(coinbase_btc_payload())
        response = Response()
        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            response=response,
            interval="5m",
            range_="1d",
            chart_only=True,
        )
        assert snapshot["meta"]["source"] == "server:sleep"
        assert snapshot["meta"]["error"]["code"] == "SERVER_SLEEPING"
        assert "IBKR" not in snapshot["meta"]["error"]["message"]
        assert (
            snapshot["meta"]["error"]["message"]
            == "Server is sleeping. Market history/live refresh is paused."
        )
        assert response.headers["server-timing"].startswith("market;dur=")

    asyncio.run(run())


def test_market_endpoint_reports_stale_route_generation_as_retryable_conflict() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(create_market_router(_market_deps()), "/api/market", "GET")
        route = route_instrument(ibkr_future_payload("ES"))

        with pytest.raises(HTTPException) as exc_info:
            await endpoint(
                _request(),
                instrument_id=route.instrument_id,
                expected_route_fingerprint="stale-route-fingerprint",
                interval="5m",
                range_="1d",
                chart_only=True,
            )

        assert exc_info.value.status_code == 409
        detail = exc_info.value.detail
        assert detail["error"]["code"] == "ROUTE_SELECTION_MISMATCH"
        assert detail["error"]["retryable"] is True
        assert detail["expected_routes"] == ((route.instrument_id, "stale-route-fingerprint"),)
        assert detail["actual_routes"] == ((route.instrument_id, route.fingerprint),)

    asyncio.run(run())


def test_market_analysis_reads_manual_channels_from_canonical_drawing_store(monkeypatch) -> None:
    async def run() -> None:
        captured: dict[str, Any] = {}
        channel = {
            "id": "chan-1",
            "type": "channel",
            "points": [
                {"ts": "2026-07-01T00:00:00Z", "price": 100.0},
                {"ts": "2026-07-01T00:05:00Z", "price": 101.0},
            ],
            "offsetPoint": {"ts": "2026-07-01T00:00:00Z", "price": 110.0},
        }
        normalized_channel = {
            **channel,
            "anchorResolution": {"status": "resolved", "reasons": []},
        }

        async def fake_snapshot(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "bars": [], "meta": {"source": "test"}}

        def indicator_params(**kwargs: Any) -> dict[str, Any]:
            captured["indicator_params_input"] = kwargs
            return {
                "channel_master": {"enabled": True},
                "manual_channels": kwargs["manual_channel_payload"],
                "manual_channel_canonical_generation": kwargs[
                    "manual_channel_canonical_generation"
                ],
            }

        def normalize(
            _store: Any,
            instrument_id: str,
            interval: str,
            drawings: list[dict[str, Any]],
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            captured["normalization"] = {
                "instrument_id": instrument_id,
                "interval": interval,
                "drawings": drawings,
                **kwargs,
            }
            return [normalized_channel]

        monkeypatch.setattr(market_actions, "async_build_market_snapshot_from_db", fake_snapshot)
        router = create_market_router(
            _market_deps(
                store_factory=lambda: _MarketStore([channel]),
                market_indicator_params=indicator_params,
                normalize_drawing_anchors=normalize,
            )
        )
        market_endpoint = _route_endpoint(router, "/api/market", "GET")
        route = route_instrument(ibkr_future_payload("ES"))

        await market_endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=False,
        )

        assert captured["normalization"]["drawings"] == [channel]
        assert captured["normalization"]["instrument_id"] == route.instrument_id
        assert captured["normalization"]["interval"] == "5m"
        assert captured["normalization"]["route_fingerprint"] == route.fingerprint
        assert captured["indicator_params"]["manual_channels"] == [normalized_channel]
        assert captured["indicator_params"]["manual_channel_canonical_generation"] == 0

    asyncio.run(run())


def test_market_analysis_retries_drawing_and_snapshot_as_one_generation_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        generation = 4
        normalize_generations: list[int] = []
        snapshot_generations: list[int] = []

        def indicator_params(**kwargs: Any) -> dict[str, Any]:
            return {
                "channel_master": {"enabled": True},
                "manual_channels": kwargs["manual_channel_payload"],
                "manual_channel_canonical_generation": kwargs[
                    "manual_channel_canonical_generation"
                ],
            }

        def normalize(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            normalize_generations.append(generation)
            return [{"id": "channel-a", "type": "channel"}]

        async def fake_snapshot(**kwargs: Any) -> dict[str, Any]:
            nonlocal generation
            snapshot_generations.append(
                kwargs["indicator_params"]["manual_channel_canonical_generation"]
            )
            if len(snapshot_generations) == 1:
                generation = 5
            return {"ok": True, "bars": [], "meta": {"source": "test"}}

        def require_generation(expected: int, *_args: Any, **_kwargs: Any) -> int:
            if expected != generation:
                raise market_actions.ChartBarsGenerationChanged(
                    expected=expected,
                    observed=generation,
                )
            return generation

        monkeypatch.setattr(
            market_actions,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: generation,
        )
        monkeypatch.setattr(
            market_actions,
            "require_chart_bars_generation",
            require_generation,
        )
        monkeypatch.setattr(
            market_analysis_job,
            "require_chart_bars_generation",
            require_generation,
        )
        monkeypatch.setattr(
            market_actions,
            "async_build_market_snapshot_from_db",
            fake_snapshot,
        )
        route = route_instrument(ibkr_future_payload("ES"))
        endpoint = _route_endpoint(
            create_market_router(
                _market_deps(
                    store_factory=lambda: _MarketStore([{"id": "channel-a"}]),
                    market_indicator_params=indicator_params,
                    normalize_drawing_anchors=normalize,
                )
            ),
            "/api/market",
            "GET",
        )

        await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=False,
        )

        assert normalize_generations == [4, 5]
        assert snapshot_generations == [4, 5]

    asyncio.run(run())


def test_market_full_snapshot_fails_closed_while_canonical_write_is_active() -> None:
    async def run() -> None:
        store_calls = 0

        def store_factory() -> _MarketStore:
            nonlocal store_calls
            store_calls += 1
            return _MarketStore()

        route = route_instrument(ibkr_future_payload("ES"))
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        guard = chart_events.begin_chart_bars_write(
            seen,
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        endpoint = _route_endpoint(
            create_market_router(_market_deps(store_factory=store_factory)),
            "/api/market",
            "GET",
        )
        try:
            with pytest.raises(HTTPException) as raised:
                await endpoint(
                    _request(),
                    instrument_id=route.instrument_id,
                    expected_route_fingerprint=route.fingerprint,
                    interval="5m",
                    range_="1d",
                    chart_only=False,
                )
            assert raised.value.status_code == 503
            assert raised.value.detail["error"]["code"] == "MARKET_CHART_GENERATION_CHANGED"
            assert store_calls == 0
        finally:
            chart_events.end_chart_bars_write(guard)

    asyncio.run(run())


def test_market_chart_registration_waits_for_canonical_write_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        store_calls = 0
        registered: list[dict[str, Any]] = []

        def store_factory() -> _MarketStore:
            nonlocal store_calls
            store_calls += 1
            return _MarketStore()

        async def register_wanted(
            _key: str,
            payload: dict[str, Any],
            _client_id: str,
            _lease_sequence: int,
        ) -> str:
            registered.append(dict(payload))
            return "queued"

        async def release_guard(guard: chart_events.ChartBarsWriteGuard) -> None:
            await asyncio.sleep(0.01)
            chart_events.end_chart_bars_write(guard)

        route = route_instrument(ibkr_future_payload("ES"))
        seen = chart_events.chart_bars_updated_generation(
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )
        guard = chart_events.begin_chart_bars_write(
            seen,
            "5m",
            route.fingerprint,
            instrument_id=route.instrument_id,
        )

        async def load_history(*_args: Any, **_kwargs: Any) -> ConfirmedChartHistoryLoad:
            return _chart_history_load(
                [
                    Bar(
                        "ES",
                        datetime(2026, 8, 13, 8, 45, tzinfo=UTC),
                        7780.0,
                        7782.0,
                        7779.0,
                        7781.0,
                        1000.0,
                        "5m",
                        "ibkr",
                        True,
                    )
                ],
                canonical_generation=seen,
            )

        monkeypatch.setattr(
            market_actions,
            "coalesced_load_confirmed_chart_bars",
            load_history,
        )
        endpoint = _route_endpoint(
            create_market_router(
                _market_deps(
                    store_factory=store_factory,
                    register_market_analysis_wanted=register_wanted,
                )
            ),
            "/api/market",
            "GET",
        )
        release_task = asyncio.create_task(release_guard(guard))
        try:
            snapshot = await endpoint(
                _request(),
                instrument_id=route.instrument_id,
                expected_route_fingerprint=route.fingerprint,
                interval="5m",
                range_="1d",
                chart_only=True,
                queue_analysis=True,
                analysis_client_id="test-client",
                analysis_lease_sequence=1,
            )
        finally:
            await release_task

        assert snapshot["meta"]["chart_canonical_revision"] == seen
        assert snapshot["meta"]["analysis_status"] == "queued"
        assert store_calls > 0
        assert len(registered) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("chart_only", "channel_enabled"),
    ((True, True), (False, False)),
)
def test_market_skips_manual_drawing_work_without_channel_master_analysis(
    monkeypatch: pytest.MonkeyPatch,
    chart_only: bool,
    channel_enabled: bool,
) -> None:
    async def run() -> None:
        async def fake_chart_snapshot(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "bars": [], "meta": {"source": "test"}}

        async def fake_full_snapshot(**_kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "bars": [], "meta": {"source": "test"}}

        def indicator_params(**kwargs: Any) -> dict[str, Any]:
            return {
                "channel_master": {"enabled": channel_enabled},
                "manual_channels": kwargs["manual_channel_payload"],
            }

        def reject_normalization(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("drawing normalization must be skipped")

        monkeypatch.setattr(
            market_actions,
            "chart_only_market_response",
            fake_chart_snapshot,
        )
        monkeypatch.setattr(
            market_actions,
            "async_build_market_snapshot_from_db",
            fake_full_snapshot,
        )
        route = route_instrument(ibkr_future_payload("ES"))
        endpoint = _route_endpoint(
            create_market_router(
                _market_deps(
                    market_indicator_params=indicator_params,
                    normalize_drawing_anchors=reject_normalization,
                )
            ),
            "/api/market",
            "GET",
        )

        await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=chart_only,
        )

    asyncio.run(run())


def test_market_response_uses_runtime_watchlist_instrument(monkeypatch) -> None:
    async def run() -> None:
        instrument = ibkr_stock_payload("RSU", name="Roblox", con_id=4242)
        captured: dict[str, Any] = {}
        active_charts: list[tuple[str, str, str]] = []

        async def fake_snapshot(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "bars": [], "meta": {"source": "ibkr:db-cache"}}

        route = route_instrument(instrument)
        monkeypatch.setattr(
            market_actions,
            "lookup_runtime_instrument",
            lambda instrument_id: (
                dict(instrument)
                if instrument_id == route.instrument_id
                else (_ for _ in ()).throw(ValueError(instrument_id))
            ),
        )
        monkeypatch.setattr(market_actions, "async_build_market_snapshot_from_db", fake_snapshot)
        endpoint = _route_endpoint(
            create_market_router(
                _market_deps(note_active_chart=lambda *args: active_charts.append(args))
            ),
            "/api/market",
            "GET",
        )

        await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=False,
        )

        assert captured["source"] == "ibkr"
        assert "symbol" not in captured
        assert captured["instrument"] == instrument
        assert captured["analysis_as_of_utc"].tzinfo is UTC
        assert active_charts == [(route.instrument_id, "5m", "1d")]

    asyncio.run(run())


def test_market_response_compacts_runtime_payload_before_return(monkeypatch) -> None:
    async def run() -> None:
        async def fake_snapshot(**_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "bars": [],
                "meta": {"source": "test"},
                "vsa_volume": {
                    "contract": "vsa-volume-context-v1",
                    "series": [
                        {"ts": f"t-{index}", "score": index, "unknown_heavy": "x" * 100}
                        for index in range(900)
                    ],
                    "events": [],
                    "overlays": [],
                },
                "indicators": {
                    "trade_setup_engine": {
                        "series": [{"ts": "t", "reason": "heavy"}],
                        "events": [{"ts": "t", "score": 1, "reason": "ok"}],
                        "overlays": [],
                    },
                },
            }

        monkeypatch.setattr(market_actions, "async_build_market_snapshot_from_db", fake_snapshot)
        endpoint = _route_endpoint(create_market_router(_market_deps()), "/api/market", "GET")
        route = route_instrument(ibkr_future_payload("ES"))

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=False,
        )

        vsa_series = snapshot["vsa_volume"]["series"]
        assert len(vsa_series) == 720
        assert vsa_series[0]["ts"] == "t-180"
        assert vsa_series[-1]["ts"] == "t-899"
        assert vsa_series[-1]["score"] == 899
        assert "unknown_heavy" not in vsa_series[-1]
        assert "vsa_volume" not in snapshot["indicators"]
        assert "series" not in snapshot["indicators"]["trade_setup_engine"]

    asyncio.run(run())


def test_market_chart_only_raises_structured_http_error() -> None:
    async def run() -> None:
        async def boom(*_args, **_kwargs):
            raise RuntimeError("db unavailable")

        deps = _market_deps()
        endpoint = _route_endpoint(create_market_router(deps), "/api/market", "GET")

        original = market_actions.coalesced_load_confirmed_chart_bars
        market_actions.coalesced_load_confirmed_chart_bars = boom
        route = route_instrument(ibkr_future_payload("ES"))
        try:
            with pytest.raises(HTTPException) as raised:
                await endpoint(
                    _request(),
                    instrument_id=route.instrument_id,
                    expected_route_fingerprint=route.fingerprint,
                    interval="5m",
                    range_="1d",
                    chart_only=True,
                )
        finally:
            market_actions.coalesced_load_confirmed_chart_bars = original

        detail = raised.value.detail
        assert detail["ok"] is False
        assert detail["error"]["code"] == "MARKET_CHART_LOAD_FAILED"
        assert detail["error"]["retryable"] is True

    asyncio.run(run())


def test_market_coinbase_chart_only_does_not_apply_ibkr_runtime_settings(monkeypatch) -> None:
    async def run() -> None:
        kwargs_seen: dict[str, Any] = {}

        class Store:
            def initialize(self):
                return None

            def read_trading_hours(self, *, instrument):
                return None

            def read_bar_slots(self, **_kwargs):
                return {datetime(2026, 6, 1, 9, 0, tzinfo=UTC): 1000}

            def get_full_snapshot_context(self, **kwargs):
                bars = list(kwargs["bars"])
                step = interval_minutes(str(kwargs["interval"]))
                return {
                    "bars": bars,
                    "bar_slots": ProviderBarSlotSequence(
                        [1000 + index * step for index, _bar in enumerate(bars)],
                        schedule_state="continuous",
                    ),
                    "mtf_context": {},
                    "mtf_context_slots": {},
                    "mtf_context_quality": {},
                }

        store = Store()

        async def fail_apply_settings() -> dict[str, Any]:
            raise AssertionError("IBKR runtime settings must not run for Coinbase")

        async def fake_provider_bars(provider, instrument, interval, range_, timeout, **kwargs):
            kwargs_seen.update(
                provider=provider,
                instrument=instrument,
                interval=interval,
                range_=range_,
                timeout=timeout,
                **kwargs,
            )
            return _chart_history_load(
                [
                    Bar(
                        symbol="BTC",
                        ts=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
                        open=100.0,
                        high=101.0,
                        low=99.0,
                        close=100.5,
                        volume=10.0,
                        timeframe="5m",
                        source="coinbase:db-cache",
                        closed=True,
                    )
                ]
            )

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(
            create_market_router(
                _market_deps(
                    apply_ibkr_runtime_settings_async=fail_apply_settings,
                    store_factory=lambda: store,
                )
            ),
            "/api/market",
            "GET",
        )
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=True,
        )
        assert snapshot["meta"]["source"] == "coinbase:db-cache"
        assert snapshot["meta"].get("gap_repair") is None
        assert kwargs_seen["provider"] == "coinbase"
        assert kwargs_seen["store_factory"]() is store

    asyncio.run(run())


def test_market_coinbase_chart_only_empty_snapshot_uses_provider_warning(monkeypatch) -> None:
    async def run() -> None:
        async def fake_provider_bars(*_args: Any, **_kwargs: Any):
            return _chart_history_load([])

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(create_market_router(_market_deps()), "/api/market", "GET")
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=True,
        )

        assert snapshot["meta"]["warning"] == "No cached COINBASE bars available."
        assert "IBKR" not in snapshot["meta"]["warning"]

    asyncio.run(run())


def test_market_http_chart_path_does_not_own_gap_repair_admission(monkeypatch) -> None:
    async def run() -> None:
        bar = Bar(
            symbol="BTC",
            ts=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            timeframe="5m",
            source="coinbase:db-cache",
            closed=True,
        )

        async def fake_provider_bars(*_args, **_kwargs):
            return _chart_history_load([bar])

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(create_market_router(_market_deps()), "/api/market", "GET")
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=True,
        )

        assert snapshot["bars"][0]["ts"] == bar.ts.isoformat()
        assert snapshot["meta"]["source"] == "coinbase:db-cache"
        assert (
            "schedule_chart_cached_gap_repair"
            not in open(
                "src/aef_terminal/ui/market_actions.py",
                encoding="utf-8",
            ).read()
        )

    asyncio.run(run())


def test_market_chart_only_retries_generation_race_and_publishes_exact_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        generation = 17
        checks = 0
        loads = 0
        bar = Bar(
            symbol="BTC",
            ts=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=10.0,
            timeframe="5m",
            source="coinbase:db-cache",
            closed=True,
        )

        async def fake_provider_bars(*_args: Any, **_kwargs: Any):
            nonlocal loads
            loads += 1
            return _chart_history_load(
                [bar],
                canonical_generation=generation,
            )

        def require_generation(expected: int, *_args: Any, **_kwargs: Any) -> int:
            nonlocal checks, generation
            checks += 1
            if checks == 2:
                generation = 18
            if expected != generation:
                raise market_actions.ChartBarsGenerationChanged(
                    expected=expected,
                    observed=generation,
                )
            return generation

        monkeypatch.setattr(
            market_actions,
            "coalesced_load_confirmed_chart_bars",
            fake_provider_bars,
        )
        monkeypatch.setattr(
            market_actions,
            "require_chart_bars_generation",
            require_generation,
        )
        monkeypatch.setattr(
            market_actions,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: generation,
        )
        monkeypatch.setattr(
            market_actions,
            "chart_only_market_snapshot",
            lambda *_args, canonical_generation, **_kwargs: {
                "bars": [],
                "meta": {"chart_canonical_revision": canonical_generation},
            },
        )
        endpoint = _route_endpoint(
            create_market_router(_market_deps()),
            "/api/market",
            "GET",
        )
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=True,
        )

        # The chart-only builder performs its own retry, then the outer market
        # batch retries once more so every response component shares generation 18.
        assert loads == 3
        assert checks == 9
        assert snapshot["meta"]["chart_canonical_revision"] == 18

    asyncio.run(run())


def test_market_chart_only_history_path_stays_db_first(monkeypatch) -> None:
    async def run() -> None:
        calls: list[dict[str, Any]] = []

        async def fake_provider_bars(*_args: Any, **kwargs: Any):
            calls.append(kwargs)
            return _chart_history_load(
                [
                    Bar(
                        symbol="BTC",
                        ts=datetime(2026, 7, 5, 4, 35, tzinfo=UTC),
                        open=62000.0,
                        high=62100.0,
                        low=61950.0,
                        close=62075.0,
                        volume=12.0,
                        timeframe="5m",
                        source="coinbase:BTC-USD",
                        closed=True,
                    )
                ]
            )

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(create_market_router(_market_deps()), "/api/market", "GET")
        route = route_instrument(coinbase_btc_payload())

        await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=True,
        )
        assert len(calls) == 1
        assert calls[0]["refresh_provider"] is False

    asyncio.run(run())


def test_market_chart_only_analysis_payload_includes_market_version(monkeypatch) -> None:
    async def run() -> None:
        registered: dict[str, Any] = {}

        async def fake_provider_bars(*_args: Any, **_kwargs: Any):
            return _chart_history_load(
                [
                    Bar(
                        symbol="MIX",
                        ts=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
                        open=100.0,
                        high=102.0,
                        low=99.0,
                        close=101.0,
                        volume=25.0,
                        timeframe="5m",
                        source="coinbase:db-cache",
                        closed=True,
                    )
                ]
            )

        def build_payload(**kwargs: Any) -> dict[str, Any]:
            return dict(kwargs)

        def analysis_key(payload: dict[str, Any]) -> str:
            return str((payload.get("market_version") or {}).get("latest_ts") or "base")

        async def register_wanted(
            key: str,
            payload: dict[str, Any],
            client_id: str,
            lease_sequence: int,
        ) -> str:
            registered["key"] = key
            registered["payload"] = payload
            registered["client_id"] = client_id
            registered["lease_sequence"] = lease_sequence
            return "queued"

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(
            create_market_router(
                _market_deps(
                    market_analysis_payload=build_payload,
                    market_analysis_key=analysis_key,
                    register_market_analysis_wanted=register_wanted,
                )
            ),
            "/api/market",
            "GET",
        )
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="1d",
            chart_only=True,
            queue_analysis=True,
            analysis_client_id="client-1",
            analysis_lease_sequence=1,
            analysis_version="client-live",
        )

        assert snapshot["meta"]["analysis_key"] == "2026-06-30T10:00:00+00:00"
        assert registered["key"] == "2026-06-30T10:00:00+00:00"
        assert registered["client_id"] == "client-1"
        assert registered["lease_sequence"] == 1
        assert registered["payload"]["parent_canonical_generation"] == 0
        market_version = registered["payload"]["market_version"]
        assert market_version == {
            "schema": "canonical-analysis-window-v2",
            "first_ts": "2026-06-30T10:00:00+00:00",
            "latest_ts": "2026-06-30T10:00:00+00:00",
            "latest_close": 101.0,
            "latest_volume": 25.0,
            "latest_closed": True,
            "chart_bar_count": 1,
            "window_hash": market_version["window_hash"],
            "client": "client-live",
        }
        assert len(market_version["window_hash"]) == 32

    asyncio.run(run())


def test_market_analysis_lease_endpoints_delegate_exact_scope() -> None:
    async def run() -> None:
        renewed: list[tuple[str, int, str, str, str]] = []
        refreshed: list[tuple[str, int, str, str, str, str]] = []
        released: list[tuple[str, int]] = []

        async def renew(
            client_id: str,
            lease_sequence: int,
            key: str,
            instrument_id: str,
            fingerprint: str,
        ) -> bool:
            renewed.append((client_id, lease_sequence, key, instrument_id, fingerprint))
            return True

        async def refresh(
            client_id: str,
            lease_sequence: int,
            key: str,
            instrument_id: str,
            fingerprint: str,
            client_version: str,
        ) -> bool:
            refreshed.append(
                (
                    client_id,
                    lease_sequence,
                    key,
                    instrument_id,
                    fingerprint,
                    client_version,
                )
            )
            return True

        async def release(client_id: str, lease_sequence: int) -> bool:
            released.append((client_id, lease_sequence))
            return True

        router = create_market_router(
            _market_deps(
                renew_market_analysis_lease=renew,
                refresh_market_analysis=refresh,
                release_market_analysis_lease=release,
            )
        )
        renew_endpoint = _route_endpoint(
            router,
            "/api/market/analysis/lease",
            "POST",
        )
        release_endpoint = _route_endpoint(
            router,
            "/api/market/analysis/release",
            "POST",
        )
        refresh_endpoint = _route_endpoint(
            router,
            "/api/market/analysis/refresh",
            "POST",
        )

        assert await renew_endpoint("client-1", 7, "key-1", "instrument-1", "route-1") == {
            "renewed": True
        }
        assert await refresh_endpoint(
            "client-1",
            7,
            "key-1",
            "instrument-1",
            "route-1",
            "quote-42",
        ) == {"refreshed": True}
        assert await release_endpoint("client-1", 7) == {"released": True}
        assert renewed == [("client-1", 7, "key-1", "instrument-1", "route-1")]
        assert refreshed == [
            (
                "client-1",
                7,
                "key-1",
                "instrument-1",
                "route-1",
                "quote-42",
            )
        ]
        assert released == [("client-1", 7)]

    asyncio.run(run())


def test_market_analysis_version_rejects_noncanonical_bar_rows() -> None:
    confirmed = _analysis_version_bar("2026-06-30T10:00:00+00:00")

    with pytest.raises(ValueError, match="only confirmed"):
        market_actions.market_analysis_version(
            {
                "meta": {},
                "bars": [
                    confirmed,
                    {
                        **confirmed,
                        "ts": "2026-06-30T10:05:00+00:00",
                        "close": 103.0,
                        "high": 103.0,
                        "volume": 2.0,
                        "closed": False,
                        "state": "forming",
                    },
                ],
            }
        )
    with pytest.raises(ValueError, match="display placeholders"):
        market_actions.market_analysis_version(
            {
                "meta": {},
                "bars": [
                    confirmed,
                    {
                        **confirmed,
                        "ts": "2026-06-30T10:05:00+00:00",
                        "missing": True,
                        "preview_kind": "gap_placeholder",
                    },
                ],
            }
        )


def test_partial_history_still_queues_analysis(monkeypatch) -> None:
    async def run() -> None:
        registered: list[str] = []
        bar = Bar(
            symbol="MIX",
            ts=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=25.0,
            timeframe="5m",
            source="coinbase:db-cache",
            closed=True,
        )

        async def fake_provider_bars(*_args: Any, **_kwargs: Any) -> ConfirmedChartHistoryLoad:
            return _chart_history_load([bar], state="partial")

        async def register_wanted(
            key: str,
            _payload: dict[str, Any],
            _client_id: str,
            _lease_sequence: int,
        ) -> str:
            registered.append(key)
            return "queued"

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(
            create_market_router(_market_deps(register_market_analysis_wanted=register_wanted)),
            "/api/market",
            "GET",
        )
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="14d",
            chart_only=True,
            queue_analysis=True,
            analysis_client_id="client-1",
            analysis_lease_sequence=1,
        )

        assert len(registered) == 1
        assert snapshot["meta"]["analysis_status"] == "queued"
        assert snapshot["meta"]["history_coverage"]["state"] == "partial"

    asyncio.run(run())


@pytest.mark.parametrize("coverage_state", ["partial", "unknown"])
def test_incomplete_history_still_queues_analysis(
    monkeypatch,
    coverage_state: str,
) -> None:
    async def run() -> None:
        registered: list[str] = []
        bar = Bar(
            symbol="MIX",
            ts=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=25.0,
            timeframe="5m",
            source="coinbase:db-cache",
            closed=True,
        )

        async def fake_provider_bars(*_args: Any, **_kwargs: Any) -> ConfirmedChartHistoryLoad:
            return _chart_history_load([bar], state=coverage_state)

        async def register_wanted(
            key: str,
            _payload: dict[str, Any],
            _client_id: str,
            _lease_sequence: int,
        ) -> str:
            registered.append(key)
            return "queued"

        monkeypatch.setattr(
            market_actions, "coalesced_load_confirmed_chart_bars", fake_provider_bars
        )
        endpoint = _route_endpoint(
            create_market_router(_market_deps(register_market_analysis_wanted=register_wanted)),
            "/api/market",
            "GET",
        )
        route = route_instrument(coinbase_btc_payload())

        snapshot = await endpoint(
            _request(),
            instrument_id=route.instrument_id,
            expected_route_fingerprint=route.fingerprint,
            interval="5m",
            range_="14d",
            chart_only=True,
            queue_analysis=True,
            analysis_client_id="client-1",
            analysis_lease_sequence=1,
        )

        assert len(registered) == 1
        assert snapshot["meta"]["analysis_status"] == "queued"
        assert snapshot["meta"]["history_coverage"]["state"] == coverage_state

    asyncio.run(run())


def test_market_analysis_version_changes_for_middle_bar_correction() -> None:
    snapshot = {
        "meta": {"chart_bar_count": 3},
        "bars": [
            _analysis_version_bar(
                "2026-06-30T10:00:00+00:00",
                high=101.0,
                close=100.5,
                volume=10.0,
            ),
            _analysis_version_bar(
                "2026-06-30T10:05:00+00:00",
                open_price=100.5,
                low=100.0,
                close=101.5,
                volume=20.0,
            ),
            _analysis_version_bar(
                "2026-06-30T10:10:00+00:00",
                open_price=101.5,
                high=103.0,
                low=101.0,
                close=102.5,
                volume=30.0,
            ),
        ],
    }
    corrected = {
        **snapshot,
        "bars": [
            snapshot["bars"][0],
            {**snapshot["bars"][1], "close": 101.75},
            snapshot["bars"][2],
        ],
    }

    base_version = market_actions.market_analysis_version(snapshot)
    corrected_version = market_actions.market_analysis_version(corrected)

    assert corrected_version["latest_ts"] == base_version["latest_ts"]
    assert corrected_version["chart_bar_count"] == base_version["chart_bar_count"]
    assert corrected_version["window_hash"] != base_version["window_hash"]


def test_market_analysis_endpoint_missing_key() -> None:
    async def analysis_snapshot(key: str, **kwargs: Any) -> dict[str, Any]:
        return build_market_analysis_snapshot_response(
            key,
            cached=None,
            task_running=False,
            instrument_id=kwargs["instrument_id"],
            route_fingerprint=kwargs["expected_route_fingerprint"],
        )

    async def run() -> None:
        endpoint = _route_endpoint(
            create_market_router(_market_deps(market_analysis_snapshot=analysis_snapshot)),
            "/api/market/analysis",
            "GET",
        )
        payload = await endpoint(
            key="",
            instrument_id="coinbase|contract|BTC-USD",
            expected_route_fingerprint="coinbase|contract|BTC-USD",
        )
        assert payload["ok"] is False
        assert payload["status"] == "missing"
        assert payload["error"]["code"] == "MARKET_ANALYSIS_KEY_MISSING"
        assert payload["instrument_id"] == "coinbase|contract|BTC-USD"
        assert payload["route_fingerprint"] == "coinbase|contract|BTC-USD"

    asyncio.run(run())


def test_market_analysis_endpoint_running() -> None:
    captured: dict[str, Any] = {}

    async def analysis_snapshot(key: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return build_market_analysis_snapshot_response(
            key,
            cached=None,
            task_running=True,
            instrument_id=kwargs["instrument_id"],
            route_fingerprint=kwargs["expected_route_fingerprint"],
        )

    async def run() -> None:
        endpoint = _route_endpoint(
            create_market_router(_market_deps(market_analysis_snapshot=analysis_snapshot)),
            "/api/market/analysis",
            "GET",
        )
        payload = await endpoint(
            key="abc123",
            instrument_id="coinbase|contract|BTC-USD",
            expected_route_fingerprint="coinbase|contract|BTC-USD",
            wait_seconds=0.0,
        )
        assert payload["ok"] is False
        assert payload["status"] == "running"
        assert payload["error"]["code"] == "MARKET_ANALYSIS_RUNNING"
        assert payload["analysis_key"] == "abc123"
        assert payload["instrument_id"] == "coinbase|contract|BTC-USD"
        assert payload["route_fingerprint"] == "coinbase|contract|BTC-USD"
        assert captured == {
            "wait_seconds": 0.0,
            "instrument_id": "coinbase|contract|BTC-USD",
            "expected_route_fingerprint": "coinbase|contract|BTC-USD",
            "vsa_render_hours": 6,
        }

    asyncio.run(run())


def test_market_analysis_endpoint_ready_snapshot() -> None:
    snapshot = {"ok": True, "symbol": "ES", "bars": []}
    snapshot_bytes = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")

    async def analysis_snapshot(_key: str, **_kwargs: Any) -> Any:
        return build_market_analysis_snapshot_response(
            "abc123",
            cached={
                "status": "ready",
                "snapshot_json": snapshot_bytes,
                "size_bytes": len(snapshot_bytes),
            },
            task_running=False,
        )

    async def run() -> None:
        endpoint = _route_endpoint(
            create_market_router(_market_deps(market_analysis_snapshot=analysis_snapshot)),
            "/api/market/analysis",
            "GET",
        )
        payload = await endpoint(
            key="abc123",
            instrument_id="coinbase|contract|BTC-USD",
            expected_route_fingerprint="coinbase|contract|BTC-USD",
        )
        assert json.loads(payload.body) == snapshot

    asyncio.run(run())


def test_ai_third_opinion_discussion_endpoint(monkeypatch) -> None:
    calls: list[tuple[dict[str, Any], str, Any]] = []

    def fake_discuss(
        snapshot: dict[str, Any], question: str, history: Any = None
    ) -> dict[str, Any]:
        calls.append((snapshot, question, history))
        return {"ok": True, "answer": "context answer", "usage": {"total_tokens": 12}}

    import aef_terminal.indicators.modules.ai_third_opinion.router as ai_router_module

    monkeypatch.setattr(ai_router_module, "discuss_ai_third_opinion", fake_discuss)

    async def run() -> None:
        endpoint = _route_endpoint(
            create_ai_third_opinion_router(), "/api/ai-third-opinion/discuss", "POST"
        )
        payload = await endpoint(
            {
                "snapshot": {"meta": {"symbol": "ES"}},
                "question": "why?",
                "history": [{"role": "user", "content": "prev"}],
            }
        )
        assert payload["ok"] is True
        assert payload["answer"] == "context answer"
        assert calls == [
            ({"meta": {"symbol": "ES"}}, "why?", [{"role": "user", "content": "prev"}])
        ]

    asyncio.run(run())


def test_ai_third_opinion_discussion_endpoint_rejects_missing_snapshot() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_ai_third_opinion_router(), "/api/ai-third-opinion/discuss", "POST"
        )
        payload = await endpoint({"question": "why?"})
        assert payload["ok"] is False
        assert payload["status"] == "invalid_payload"

    asyncio.run(run())


def test_ai_third_opinion_provider_test_endpoint(monkeypatch) -> None:
    calls: list[tuple[Any, Any]] = []

    def fake_provider_test(provider: Any = None, model: Any = None) -> dict[str, Any]:
        calls.append((provider, model))
        return {"ok": True, "provider": provider, "model": model, "status": "ready"}

    import aef_terminal.indicators.modules.ai_third_opinion.router as ai_router_module

    monkeypatch.setattr(ai_router_module, "test_ai_third_opinion_provider", fake_provider_test)

    async def run() -> None:
        endpoint = _route_endpoint(
            create_ai_third_opinion_router(), "/api/ai-third-opinion/provider-test", "POST"
        )
        payload = await endpoint({"provider": "gemini", "model": "gemini-3.5-flash"})
        assert payload == {
            "ok": True,
            "provider": "gemini",
            "model": "gemini-3.5-flash",
            "status": "ready",
        }
        assert calls == [("gemini", "gemini-3.5-flash")]

    asyncio.run(run())


def test_ai_third_opinion_gate_endpoint(monkeypatch) -> None:
    calls: list[tuple[Any, Any]] = []

    def fake_gate(enabled: Any, provider: Any = None) -> dict[str, Any]:
        calls.append((enabled, provider))
        return {"ok": True, "provider": "codex", "gate": "open"}

    import aef_terminal.indicators.modules.ai_third_opinion.router as ai_router_module

    monkeypatch.setattr(ai_router_module, "set_ai_third_opinion_gate", fake_gate)

    async def run() -> None:
        endpoint = _route_endpoint(
            create_ai_third_opinion_router(),
            "/api/ai-third-opinion/gate",
            "POST",
        )
        payload = await endpoint({"enabled": True, "provider": "codex"})
        assert payload == {"ok": True, "provider": "codex", "gate": "open"}
        assert calls == [(True, "codex")]

    asyncio.run(run())
