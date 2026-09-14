from __future__ import annotations

import asyncio
import inspect
import json
import threading
from datetime import UTC, datetime
from typing import Any

import pytest

from aef_terminal.config import AppConfig
from aef_terminal.data.provider_contract import InstrumentRoute, ProviderCapabilities
from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
from aef_terminal.data.instrument_identity import qualified_instrument_id, route_fingerprint
from aef_terminal.ui.gex_projection import project_gex_context_for_chart
from aef_terminal.ui.routers import gex as gex_router_module
from aef_terminal.ui.routers.gex import (
    GexManualRefreshOwner,
    GexRouterDeps,
    create_gex_router,
)
from aef_terminal.ui.runtime_services import gex_scheduler_runtime_settings
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload

_ES = ibkr_future_payload("ES")
_SPY = ibkr_stock_payload("SPY", con_id=756733)
_QQQ = ibkr_stock_payload("QQQ", con_id=320227571)
_SPX = ibkr_stock_payload("SPX", con_id=416904, asset_class="index", sec_type="IND")
_INSTRUMENTS_BY_ID = {
    qualified_instrument_id(instrument): instrument for instrument in (_ES, _SPY, _QQQ, _SPX)
}


def _instrument_id(instrument: dict[str, Any]) -> str:
    return qualified_instrument_id(instrument)


def _route_key(instrument: dict[str, Any]) -> str:
    return route_fingerprint(instrument)


def _instrument_lookup(instrument_id: str) -> dict[str, Any]:
    instrument = _INSTRUMENTS_BY_ID.get(instrument_id)
    if instrument is None:
        raise ValueError(f"unknown instrument_id {instrument_id}")
    return instrument


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _gex_deps(**overrides: Any) -> GexRouterDeps:
    async def apply_async() -> dict[str, Any]:
        return {}

    load_gex = overrides.pop(
        "load_gex_context",
        lambda *_args, **_kwargs: asyncio.sleep(0, result={"ok": True, "levels": []}),
    )
    option_quote = overrides.pop("option_quote_provider", lambda _payload: None)
    option_price = overrides.pop(
        "option_target_price",
        lambda _instrument, **_kwargs: {"ok": True},
    )

    class FakeGexAdapter:
        key = "ibkr"
        capabilities = ProviderCapabilities(gex=True, options=True)

        async def async_load_gex(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return await load_gex(instrument=instrument, **kwargs)

        def gex_request_config(self, instrument: dict[str, Any], **kwargs: Any):
            return ibkr_gex_provider_runtime.request_config(
                instrument=instrument,
                mode=str(kwargs.pop("mode", "manual")),
                **kwargs,
            )

        def live_option_quote(
            self, instrument: dict[str, Any], payload: dict[str, Any]
        ) -> dict[str, Any] | None:
            return option_quote(payload)

        def option_target_price(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return option_price(instrument, **kwargs)

        def option_contract_universe(
            self,
            instrument: dict[str, Any],
            **kwargs: Any,
        ) -> dict[str, Any]:
            return {"instrument": instrument, **kwargs}

    adapter = FakeGexAdapter()

    def save_scheduler_unavailable(_setting: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("GEX_SCHEDULER_SETTINGS_STORAGE_REQUIRED")

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

    defaults = {
        "apply_ibkr_runtime_settings": lambda: {},
        "apply_ibkr_runtime_settings_async": apply_async,
        "exception_message": lambda exc: str(exc),
        "gex_live_status": lambda: {"sessions": []},
        "gex_scheduler_runtime_settings": lambda: {"enabled": False},
        "save_gex_scheduler_settings": save_scheduler_unavailable,
        "instrument_lookup": _instrument_lookup,
        "option_target_caps_settings": lambda: {},
        "save_option_target_caps_settings": lambda caps: dict(caps),
        "parse_iso_ts": lambda _value: None,
        "quote_cache_for_instruments": lambda _instruments: ({}, "", 0),
        "server_sleeping": lambda: False,
        "store_factory": lambda: None,
        "now_iso": lambda: "2026-01-01T00:00:00+00:00",
        "manual_refresh_owner": GexManualRefreshOwner(),
        "provider_router": provider_router,
    }
    defaults.update(overrides)
    return GexRouterDeps(**defaults)


def test_gex_scheduler_storage_unavailable() -> None:
    endpoint = _route_endpoint(create_gex_router(_gex_deps()), "/api/gex/scheduler", "PUT")
    payload = endpoint({"enabled": True, "instrument_ids": [_instrument_id(_ES)]})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "GEX_STORAGE_NOT_CONFIGURED"


def test_option_target_caps_update_uses_serialized_runtime_owner() -> None:
    saved: list[dict[str, float]] = []
    endpoint = _route_endpoint(
        create_gex_router(
            _gex_deps(
                save_option_target_caps_settings=lambda caps: saved.append(dict(caps)) or dict(caps)
            )
        ),
        "/api/options/settings",
        "PUT",
    )

    payload = endpoint({"premium_caps": {"instrument-a": 2.5}})

    assert payload == {"ok": True, "premium_caps": {"instrument-a": 2.5}}
    assert saved == [{"instrument-a": 2.5}]


def test_option_target_caps_update_fails_closed_without_runtime_storage() -> None:
    def fail_save(_caps: dict[str, float]) -> dict[str, float]:
        raise RuntimeError("OPTION_TARGET_SETTINGS_STORAGE_REQUIRED")

    endpoint = _route_endpoint(
        create_gex_router(_gex_deps(save_option_target_caps_settings=fail_save)),
        "/api/options/settings",
        "PUT",
    )

    payload = endpoint({"premium_caps": {"instrument-a": 2.5}})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "OPTION_STORAGE_NOT_CONFIGURED"


def test_gex_option_target_price_sleeping() -> None:
    endpoint = _route_endpoint(
        create_gex_router(_gex_deps(server_sleeping=lambda: True)),
        "/api/options/target-price",
        "GET",
    )
    payload = endpoint(
        instrument_id=_instrument_id(_ES),
        expected_route_fingerprint=_route_key(_ES),
        dte="0dte",
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "OPTION_TARGET_SERVER_SLEEPING"
    assert payload["target_dte"] == "0dte"


def test_gex_scheduler_instrument_ids_required() -> None:
    endpoint = _route_endpoint(create_gex_router(_gex_deps()), "/api/gex/scheduler", "PUT")
    payload = endpoint({"enabled": True, "instrument_ids": []})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "GEX_SCHEDULER_SETTING_INVALID"


@pytest.mark.parametrize(
    ("instrument_ids", "expected_code"),
    (
        (
            f"{_instrument_id(_ES)},{_instrument_id(_SPY)}",
            "GEX_SCHEDULER_SETTING_INVALID",
        ),
        ([_instrument_id(_ES), 123], "GEX_SCHEDULER_SETTING_INVALID"),
        ([_instrument_id(_ES), _instrument_id(_ES)], "GEX_SCHEDULER_SETTING_INVALID"),
    ),
)
def test_gex_scheduler_rejects_delimited_or_invalid_identity_collections(
    instrument_ids: object,
    expected_code: str,
) -> None:
    endpoint = _route_endpoint(create_gex_router(_gex_deps()), "/api/gex/scheduler", "PUT")

    payload = endpoint({"enabled": True, "instrument_ids": instrument_ids})

    assert payload["ok"] is False
    assert payload["error"]["code"] == expected_code


def test_gex_scheduler_env_uses_json_array_without_delimited_fallback(monkeypatch) -> None:
    instrument_ids = [_instrument_id(_ES), "demo|contract|id,with-comma"]
    monkeypatch.setenv("AEF_GEX_SCHEDULER_INSTRUMENT_IDS", json.dumps(instrument_ids))

    assert AppConfig().gex_scheduler_instrument_ids == tuple(instrument_ids)

    monkeypatch.setenv(
        "AEF_GEX_SCHEDULER_INSTRUMENT_IDS",
        f"{_instrument_id(_ES)},{_instrument_id(_SPY)}",
    )
    with pytest.raises(ValueError, match="JSON array"):
        AppConfig()


def test_gex_scheduler_runtime_rejects_delimited_persisted_identity_setting() -> None:
    with pytest.raises(ValueError, match="GEX_SCHEDULER_SETTING_INVALID"):
        gex_scheduler_runtime_settings(
            env_enabled=True,
            env_instrument_ids=(_instrument_id(_ES),),
            raw_setting={
                "enabled": True,
                "instrument_ids": f"{_instrument_id(_ES)},{_instrument_id(_SPY)}",
            },
        )


@pytest.mark.parametrize(
    "raw_setting",
    (
        {"enabled": "false", "instrument_ids": [_instrument_id(_ES)]},
        {
            "enabled": True,
            "instrument_ids": [_instrument_id(_ES)],
            "schedule": "legacy",
        },
    ),
)
def test_gex_scheduler_runtime_rejects_noncanonical_persisted_shape(
    raw_setting: object,
) -> None:
    with pytest.raises(ValueError, match="GEX_SCHEDULER_SETTING_INVALID"):
        gex_scheduler_runtime_settings(
            env_enabled=False,
            env_instrument_ids=(_instrument_id(_ES),),
            raw_setting=raw_setting,
        )


def test_gex_scheduler_update_rejects_non_boolean_enabled() -> None:
    endpoint = _route_endpoint(create_gex_router(_gex_deps()), "/api/gex/scheduler", "PUT")

    payload = endpoint({"enabled": "false", "instrument_ids": [_instrument_id(_ES)]})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "GEX_SCHEDULER_SETTING_INVALID"


def test_gex_scheduler_runtime_rejects_string_coercion_at_env_boundary() -> None:
    with pytest.raises(TypeError, match="typed sequence"):
        gex_scheduler_runtime_settings(
            env_enabled=True,
            env_instrument_ids=_instrument_id(_ES),
        )


def test_gex_endpoint_server_sleeping() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(_gex_deps(server_sleeping=lambda: True)),
            "/api/gex",
            "GET",
        )
        payload = await endpoint(
            instrument_id=_instrument_id(_ES),
            expected_route_fingerprint=_route_key(_ES),
            enabled=True,
            refresh=True,
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "GEX_SERVER_SLEEPING"
        assert payload["status"] == "sleeping"

    asyncio.run(run())


def test_gex_request_cancellation_settles_physical_identity_lookup() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_lookup(_instrument_id: str) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return _ES

    endpoint = _route_endpoint(
        create_gex_router(_gex_deps(instrument_lookup=blocking_lookup)),
        "/api/gex",
        "GET",
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            endpoint(
                instrument_id=_instrument_id(_ES),
                expected_route_fingerprint=_route_key(_ES),
                enabled=True,
                refresh=False,
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


def test_gex_request_refresh_rejects_exact_route_owned_by_live_mode() -> None:
    loads: list[dict[str, Any]] = []

    async def load_gex(**kwargs: Any) -> dict[str, Any]:
        loads.append(kwargs)
        return {"ok": True, "levels": []}

    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(
                _gex_deps(
                    load_gex_context=load_gex,
                    gex_live_status=lambda: {
                        "sessions": [
                            {
                                "instrument_id": _instrument_id(_SPY),
                                "route_fingerprint": _route_key(_SPY),
                                "active": False,
                            }
                        ]
                    },
                )
            ),
            "/api/gex",
            "GET",
        )

        payload = await endpoint(
            instrument_id=_instrument_id(_SPY),
            expected_route_fingerprint=_route_key(_SPY),
            enabled=True,
            refresh=True,
        )

        assert payload["ok"] is False
        assert payload["status"] == "unavailable"
        assert payload["error"]["code"] == "GEX_CAPTURE_MODE_CONFLICT"
        assert payload["active_capture_mode"] == "live"
        assert payload["requested_capture_mode"] == "request"

    asyncio.run(run())

    assert loads == []


def test_gex_async_endpoint_offloads_watchlist_store_and_projection(
    monkeypatch,
) -> None:
    event_loop_thread = threading.get_ident()
    lookup_threads: list[int] = []
    store_threads: list[int] = []
    projection_threads: list[int] = []
    original_projection = gex_router_module.project_gex_context_for_chart

    def instrument_lookup(instrument_id: str) -> dict[str, Any]:
        lookup_threads.append(threading.get_ident())
        return _instrument_lookup(instrument_id)

    def store_factory() -> None:
        store_threads.append(threading.get_ident())
        return None

    def project(payload: dict[str, Any]) -> dict[str, Any]:
        projection_threads.append(threading.get_ident())
        return original_projection(payload)

    monkeypatch.setattr(gex_router_module, "project_gex_context_for_chart", project)

    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(
                _gex_deps(
                    instrument_lookup=instrument_lookup,
                    store_factory=store_factory,
                )
            ),
            "/api/gex",
            "GET",
        )
        await endpoint(
            instrument_id=_instrument_id(_ES),
            expected_route_fingerprint=_route_key(_ES),
            enabled=True,
            refresh=False,
        )

    asyncio.run(run())

    assert lookup_threads and all(thread != event_loop_thread for thread in lookup_threads)
    assert store_threads and all(thread != event_loop_thread for thread in store_threads)
    assert projection_threads and all(thread != event_loop_thread for thread in projection_threads)


@pytest.mark.parametrize(
    ("instrument_id", "expected_route_fingerprint"),
    (
        ("ibkr|unknown|opaque", "ibkr|unknown-route|opaque"),
        (_instrument_id(_SPY), "ibkr|stale-route|opaque"),
    ),
)
def test_gex_identity_error_echoes_requested_route_as_unqualified(
    instrument_id: str,
    expected_route_fingerprint: str,
) -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(_gex_deps()),
            "/api/gex",
            "GET",
        )

        payload = await endpoint(
            instrument_id=instrument_id,
            expected_route_fingerprint=expected_route_fingerprint,
            enabled=True,
            refresh=False,
        )

        assert payload["ok"] is False
        assert payload["error"]["code"] == "GEX_INSTRUMENT_IDENTITY_ERROR"
        assert payload["instrument_id"] == instrument_id
        assert payload["route_fingerprint"] == expected_route_fingerprint
        assert payload["route_qualified"] is False

    asyncio.run(run())


def test_gex_refresh_api_has_no_strike_count_override() -> None:
    endpoint = _route_endpoint(create_gex_router(_gex_deps()), "/api/gex", "GET")

    parameters = inspect.signature(endpoint).parameters
    assert "strike_count" not in parameters
    assert "max_levels" not in parameters
    assert "stale_minutes" not in parameters
    assert "history_hours" not in parameters


def test_gex_chart_projection_keeps_public_health_fields_and_strips_internal_analysis() -> None:
    comparison_scope = {
        "capture_mode": "request",
        "strike_count": 1,
        "strike_ladder": [5000.0],
        "contract_con_ids": [101, 102],
        "expiries": ["20260724"],
        "futures_options": True,
        "series": [
            {
                "expiry": "20260724",
                "trading_class": "ES",
                "exchange": "CME",
                "multiplier": 50.0,
            }
        ],
        "risk_free_rate": 0.05,
        "dividend_yield": 0.0,
        "market_data_entitlement": "live",
    }
    level = {
        "price": 5000.0,
        "kind": "POS_GAMMA_NODE",
        "kind_class": "positive_node",
        "power_class": "EXTREME",
        "strength": 1.0,
        "selection_rank": 1,
        "net_gex": 1_000_000.0,
        "call_gex": 2_000_000.0,
        "put_gex": -1_000_000.0,
        "abs_gex": 3_000_000.0,
        "abs_flow_1pt": 60_000.0,
        "distance_from_spot": 0.0,
        "zone_half_width": 1.0,
        "spot_side": "inside",
        "option_volume_context": {
            "current": {
                "call_volume": None,
                "put_volume": None,
                "total_volume": None,
                "call_oi": None,
                "put_oi": None,
                "total_oi": None,
                "turnover": None,
                "rank": None,
            },
            "event": None,
        },
    }
    projected = project_gex_context_for_chart(
        {
            "levels": [level],
            "request_meta": {
                "refresh_mode": "request",
                "strike_count": 1,
                "requested_strike_limit": 17,
            },
            "enabled": True,
            "degraded": {"internal_runtime_state": True},
            "backoff": {"internal_retry_state": True},
            "refresh_error": "internal producer error",
            "age_minutes": 1.5,
            "stale_minutes": 180,
            "call_wall": 5010.0,
            "put_wall": 4990.0,
            "net_gex": 1_000_000.0,
            "option_activity": {"scope": "selected_chain"},
            "option_activity_status": "READY",
            "last_sync_at": "2026-07-21T10:00:00+00:00",
            "learned_dividend_yield_cache": {"cached": True},
            "diagnostics": {"diag_option_rows": 34},
            "visibility_summary": {"output_level_count": 1},
            "expiry_profile": [
                {
                    "expiry": "20260724",
                    "strike": 5000.0,
                    "call_gex": 2_000_000.0,
                    "put_gex": -1_000_000.0,
                    "net_gex": 1_000_000.0,
                    "abs_gex": 3_000_000.0,
                }
            ],
            "comparison_scope": comparison_scope,
            "market_data_entitlement": "live",
            "decision_authoritative": True,
            "captured_at": "2026-07-21T10:00:00+00:00",
            "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
            "frame_complete": True,
            "open_interest_as_of": "previous_settlement",
            "raw": {"contracts": [{"con_id": 101}]},
            "unexpected_internal_field": {"must_not_reach_browser": True},
            "history": [
                {
                    "captured_at": "2026-07-21T10:00:00+00:00",
                    "timestamp_unix_ms": 1784628000000,
                    "valid_until_unix_ms": 1784628300000,
                    "source": "gex:ibkr",
                    "capture_mode": "request",
                    "capture_revision": "2026-07-21T10:00:00+00:00",
                    "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
                    "market_data_entitlement": "live",
                    "open_interest_as_of": "previous_settlement",
                    "comparison_scope": comparison_scope,
                    "spot": 5000.0,
                    "gamma_flip": None,
                    "global_gamma_regime": "POSITIVE_ESTIMATE",
                    "call_wall": 5010.0,
                    "put_wall": 4990.0,
                    "net_gex": 1_000_000.0,
                    "option_activity": {"scope": "selected_chain"},
                    "option_activity_status": "READY",
                    "levels": [level],
                }
            ],
        }
    )

    assert projected["levels"] == [level]
    assert projected["diagnostics"] == {"diag_option_rows": 34}
    assert projected["visibility_summary"] == {"output_level_count": 1}
    assert projected["expiry_profile"][0]["expiry"] == "20260724"
    assert projected["comparison_scope"] == comparison_scope
    assert projected["market_data_entitlement"] == "live"
    assert projected["decision_authoritative"] is True
    assert projected["frame_complete"] is True
    assert projected["open_interest_as_of"] == "previous_settlement"
    assert projected["backoff"] == {"internal_retry_state": True}
    assert projected["degraded"] == {"internal_runtime_state": True}
    assert (
        not {
            "age_minutes",
            "call_wall",
            "enabled",
            "last_sync_at",
            "learned_dividend_yield_cache",
            "net_gex",
            "option_activity",
            "option_activity_status",
            "put_wall",
            "raw",
            "refresh_error",
            "request_meta",
            "stale_minutes",
            "unexpected_internal_field",
        }
        & projected.keys()
    )
    assert projected["history"][0]["levels"] == projected["levels"]
    assert projected["history"][0]["comparison_scope"] == comparison_scope
    assert "instrument_id" not in projected["history"][0]
    assert "route_fingerprint" not in projected["history"][0]
    assert set(projected["history"][0]) == {
        "captured_at",
        "timestamp_unix_ms",
        "capture_mode",
        "source",
        "capture_revision",
        "option_universe_expires_at",
        "market_data_entitlement",
        "open_interest_as_of",
        "comparison_scope",
        "spot",
        "gamma_flip",
        "levels",
        "valid_until_unix_ms",
        "projection_mode",
        "projection_bucket_minutes",
    }
    assert (
        not {
            "call_wall",
            "global_gamma_regime",
            "net_gex",
            "option_activity",
            "option_activity_status",
            "put_wall",
        }
        & projected["history"][0].keys()
    )


def test_option_target_api_keeps_provider_con_id_opaque_until_validation() -> None:
    endpoint = _route_endpoint(
        create_gex_router(_gex_deps()),
        "/api/options/target-price",
        "GET",
    )

    assert inspect.signature(endpoint).parameters["con_id"].annotation == str | None
    assert "underlying_price" not in inspect.signature(endpoint).parameters


def test_option_target_api_uses_only_exact_server_route_quote() -> None:
    observed: dict[str, Any] = {}

    def option_price(_instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"ok": True}

    now = datetime.now(tz=UTC).isoformat()
    endpoint = _route_endpoint(
        create_gex_router(
            _gex_deps(
                option_target_price=option_price,
                quote_cache_for_instruments=lambda instruments: (
                    {
                        _route_key(instruments[0]): {
                            "price": 5000.25,
                            "price_source": "bid_ask_mid",
                            "last": 5000.25,
                            "bid": 5000.0,
                            "ask": 5000.5,
                            "provider_ts": None,
                            "received_at": now,
                            "ts": now,
                            "time_basis": "client_receive",
                            "status": "live",
                            "is_stale": False,
                            "is_delayed": False,
                            "market_data_type": 1,
                            "market_data_entitlement": "live",
                        }
                    },
                    "",
                    1,
                ),
            )
        ),
        "/api/options/target-price",
        "GET",
    )

    endpoint(
        instrument_id=_instrument_id(_ES),
        expected_route_fingerprint=_route_key(_ES),
        target_price=5005.0,
    )

    assert observed["underlying_price"] == 5000.25
    assert observed["underlying_quote"]["market_data_entitlement"] == "live"


def test_option_target_api_scopes_every_nested_alternative_to_chart_axis(
    monkeypatch,
) -> None:
    target_at = datetime(2026, 7, 27, 6, 5, tzinfo=UTC)
    monkeypatch.setattr(
        gex_router_module,
        "require_provider_chart_axis_point",
        lambda *_args, **_kwargs: 42,
    )

    def option_price(_instrument: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "alternatives": [
                {"ok": True, "con_id": 101, "strike": 7515.0},
                {"ok": True, "con_id": 102, "strike": 7520.0},
            ],
        }

    endpoint = _route_endpoint(
        create_gex_router(
            _gex_deps(
                option_target_price=option_price,
                parse_iso_ts=lambda _value: target_at,
            )
        ),
        "/api/options/target-price",
        "GET",
    )

    payload = endpoint(
        instrument_id=_instrument_id(_ES),
        expected_route_fingerprint=_route_key(_ES),
        target_ts=target_at.isoformat(),
        timeframe="5m",
    )

    expected_scope = {
        "instrument_id": _instrument_id(_ES),
        "route_fingerprint": _route_key(_ES),
        "timeframe": "5m",
        "target_bar_slot": 42,
    }
    assert {key: payload[key] for key in expected_scope} == expected_scope
    assert len(payload["alternatives"]) == 2
    assert all(
        {key: alternative[key] for key in expected_scope} == expected_scope
        for alternative in payload["alternatives"]
    )


def test_gex_manual_refresh_reports_requested_and_cached_shape() -> None:
    async def load_gex_context(*, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("instrument") is None
        assert instrument["provider"] == "ibkr"
        assert instrument["key"] == "SPY"
        assert instrument["contract_identity"]["con_id"] == 756733
        if kwargs.get("refresh"):
            await asyncio.sleep(0.01)
            return {
                "ok": True,
                "provider_symbol": "SPY",
                "source": "gex:ibkr",
                "status": "degraded",
                "frame_complete": True,
                "decision_authoritative": True,
                "market_data_entitlement": "live",
                "captured_at": "2026-01-01T00:01:00+00:00",
                "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
                "snapshot_sync_at": "2026-01-01T00:01:00+00:00",
                "levels": [{"price": 755}],
                "request_meta": {
                    "refresh_mode": "manual",
                    "strike_count": 17,
                    "requested_strike_limit": 17,
                },
                "visibility_summary": {
                    "raw_strike_count": 17,
                    "nonzero_strike_count": 8,
                    "output_level_count": 1,
                },
            }
        return {
            "ok": True,
            "provider_symbol": "SPY",
            "source": "gex:ibkr",
            "status": "stale",
            "frame_complete": True,
            "decision_authoritative": True,
            "market_data_entitlement": "live",
            "captured_at": "2026-01-01T00:00:00+00:00",
            "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
            "snapshot_sync_at": "2026-01-01T00:00:00+00:00",
            "levels": [{"price": 755}],
            "request_meta": {
                "refresh_mode": "live",
                "strike_count": 9,
                "requested_strike_limit": 17,
            },
            "visibility_summary": {
                "raw_strike_count": 9,
                "nonzero_strike_count": 3,
                "output_level_count": 1,
            },
        }

    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(_gex_deps(load_gex_context=load_gex_context)),
            "/api/gex",
            "GET",
        )
        result = await endpoint(
            instrument_id=_instrument_id(_SPY),
            expected_route_fingerprint=_route_key(_SPY),
            enabled=True,
            refresh=True,
        )
        assert result["refresh_running"] is True
        assert result["refresh_request"]["status"] == "running"
        assert result["refresh_request"]["requested"]["requested_strike_limit"] == 17
        assert result["refresh_request"]["cached"]["selected_strike_count"] == 9
        assert result["refresh_request"]["cached"]["requested_strike_limit"] == 17
        assert result["refresh_request"]["cached"]["raw_strike_count"] == 9
        await asyncio.sleep(0.02)
        completed = await endpoint(
            instrument_id=_instrument_id(_SPY),
            expected_route_fingerprint=_route_key(_SPY),
            enabled=True,
            refresh=False,
        )
        assert completed["refresh_status"] == "published"
        assert completed["refresh_request"]["status"] == "published"
        assert completed["refresh_request"]["result"]["captured_at"] == "2026-01-01T00:01:00+00:00"
        assert completed["refresh_request"]["result"]["selected_strike_count"] == 17

    asyncio.run(run())


def test_gex_manual_refresh_owner_drains_before_lifecycle_stops() -> None:
    async def run() -> None:
        owner = GexManualRefreshOwner()
        started = asyncio.Event()
        release = asyncio.Event()

        async def refresh() -> dict[str, Any]:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
                raise
            return {"status": "ok"}

        assert owner.start_refresh(
            ("instrument", "route"),
            refresh,
            initial_result={"refresh_status": "running"},
            now_iso=lambda: "2026-01-01T00:00:00+00:00",
        )
        await started.wait()
        shutdown = asyncio.create_task(owner.shutdown())
        await asyncio.sleep(0)

        assert not shutdown.done()
        with pytest.raises(RuntimeError, match="GEX_MANUAL_REFRESH_OWNER_STOPPING"):
            owner.start_refresh(
                ("other", "route"),
                refresh,
                initial_result={},
                now_iso=lambda: "2026-01-01T00:00:00+00:00",
            )

        release.set()
        await shutdown
        assert owner.state(("instrument", "route")) == (False, {})
        owner.start_lifecycle()

    asyncio.run(run())


def test_gex_manual_refresh_reports_blocked_instead_of_success() -> None:
    async def load_gex_context(*, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        cached = {
            "ok": True,
            "provider_symbol": "ES",
            "source": "gex:ibkr",
            "status": "stale",
            "decision_authoritative": True,
            "captured_at": "2026-01-01T00:00:00+00:00",
            "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
            "snapshot_sync_at": "2026-01-01T00:00:00+00:00",
            "levels": [{"price": 5000}],
        }
        if kwargs.get("refresh"):
            await asyncio.sleep(0.01)
            return {
                **cached,
                "status": "refresh_blocked",
                "message": "provider trading session is closed or unknown",
            }
        return cached

    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(_gex_deps(load_gex_context=load_gex_context)),
            "/api/gex",
            "GET",
        )
        started = await endpoint(
            instrument_id=_instrument_id(_ES),
            expected_route_fingerprint=_route_key(_ES),
            enabled=True,
            refresh=True,
        )
        assert started["refresh_status"] == "running"
        await asyncio.sleep(0.02)
        completed = await endpoint(
            instrument_id=_instrument_id(_ES),
            expected_route_fingerprint=_route_key(_ES),
            enabled=True,
            refresh=False,
        )
        assert completed["refresh_status"] == "blocked"
        assert completed["refresh_request"]["status"] == "blocked"
        assert completed["refresh_request"]["result"]["captured_at"] == "2026-01-01T00:00:00+00:00"
        assert "closed or unknown" in completed["refresh_error"]

    asyncio.run(run())


def test_gex_manual_refresh_rejects_non_authoritative_frame() -> None:
    async def load_gex_context(*, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("refresh"):
            await asyncio.sleep(0.01)
            return {
                "ok": False,
                "provider_symbol": "ES",
                "source": "gex:ibkr",
                "status": "partial",
                "frame_complete": False,
                "decision_authoritative": False,
                "market_data_entitlement": "unknown",
                "captured_at": "2026-01-01T00:01:00+00:00",
                "instrument_id": _instrument_id(_ES),
                "route_fingerprint": _route_key(_ES),
                "capture_mode": "request",
                "capture_revision": "2026-01-01T00:01:00+00:00",
                "levels": [{"price": 5005}],
                "expiry_profile": [],
                "message": "insufficient complete option rows",
            }
        return {
            "ok": True,
            "provider_symbol": "ES",
            "source": "gex:ibkr",
            "status": "stale",
            "decision_authoritative": True,
            "captured_at": "2026-01-01T00:00:00+00:00",
            "option_universe_expires_at": ("2099-12-31T21:00:00+00:00"),
            "levels": [{"price": 5000}],
        }

    async def run() -> None:
        endpoint = _route_endpoint(
            create_gex_router(
                _gex_deps(
                    load_gex_context=load_gex_context,
                    parse_iso_ts=lambda value: datetime.fromisoformat(value) if value else None,
                    now_iso=lambda: "2026-01-01T00:01:30+00:00",
                )
            ),
            "/api/gex",
            "GET",
        )
        await endpoint(
            instrument_id=_instrument_id(_ES),
            expected_route_fingerprint=_route_key(_ES),
            enabled=True,
            refresh=True,
        )
        await asyncio.sleep(0.02)
        completed = await endpoint(
            instrument_id=_instrument_id(_ES),
            expected_route_fingerprint=_route_key(_ES),
            enabled=True,
            refresh=False,
        )
        assert completed["refresh_status"] == "rejected"
        assert completed["refresh_request"]["status"] == "rejected"
        assert "insufficient complete option rows" in completed["refresh_error"]
        assert completed["status"] == "partial"
        assert completed["decision_authoritative"] is False
        assert completed["levels"] == [{"price": 5005}]

    asyncio.run(run())


def test_gex_scheduler_save_ok() -> None:
    saved: list[dict[str, Any]] = []

    def save(setting: dict[str, Any]) -> dict[str, Any]:
        saved.append(dict(setting))
        return {
            "enabled": setting["enabled"],
            "instrument_ids": list(setting["instrument_ids"]),
            "source": "server",
        }

    endpoint = _route_endpoint(
        create_gex_router(_gex_deps(save_gex_scheduler_settings=save)),
        "/api/gex/scheduler",
        "PUT",
    )
    payload = endpoint(
        {"enabled": True, "instrument_ids": [_instrument_id(_ES), _instrument_id(_SPY)]}
    )
    assert payload["ok"] is True
    assert saved == [
        {
            "enabled": True,
            "instrument_ids": [_instrument_id(_ES), _instrument_id(_SPY)],
        }
    ]
