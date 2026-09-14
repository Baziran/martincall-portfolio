from __future__ import annotations

import asyncio
import inspect
from typing import Any

from aef_terminal.data.instrument_identity import qualified_instrument_id, route_fingerprint
from tests.provider_payloads import ibkr_future_payload
from aef_terminal.ui.routers.ibkr import IbkrRouterDeps, create_ibkr_router
from aef_terminal.ui.ibkr_gateway_login_control import IbkrGatewayLoginControlError
from tests.provider_payloads import ibkr_stock_payload


def test_ibkr_router_never_uses_opaque_identity_as_display_fallback() -> None:
    import aef_terminal.ui.routers.ibkr as ibkr_module

    source = inspect.getsource(ibkr_module._ibkr_route)
    assert 'instrument.get("key")' not in source
    assert "or instrument_id" not in source


def _exact_lookup(instrument: dict[str, Any]):
    instrument_id = qualified_instrument_id(instrument)

    def lookup(requested_instrument_id: str) -> dict[str, Any]:
        if requested_instrument_id != instrument_id:
            raise ValueError(f"unknown instrument_id {requested_instrument_id}")
        return instrument

    return instrument_id, lookup


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _ibkr_deps(**overrides: Any) -> IbkrRouterDeps:
    async def apply_async(_settings: dict[str, Any] | None = None) -> dict[str, Any]:
        return {}

    defaults = {
        "apply_ibkr_runtime_settings": lambda _settings=None: {},
        "apply_ibkr_runtime_settings_async": apply_async,
        "combined_quote_instruments": lambda instrument_ids: [
            ibkr_future_payload("ES")
            for instrument_id in instrument_ids or []
            if instrument_id == qualified_instrument_id(ibkr_future_payload("ES"))
        ],
        "disconnect_ibkr_sessions": lambda **_kwargs: {"ok": True},
        "ibkr_gateway_login_control_status": lambda: {"configured": True},
        "request_ibkr_gateway_login": lambda: {"ok": True, "accepted": True},
        "server_sleeping": lambda: False,
        "server_sleep_status": lambda: {"sleeping": False},
        "ibkr_port_setting_key": "ibkr-port",
        "now_iso": lambda: "2026-01-01T00:00:00+00:00",
    }
    defaults.update(overrides)
    return IbkrRouterDeps(**defaults)


def test_ibkr_gateway_login_requires_explicit_confirmation() -> None:
    disconnected: list[bool] = []
    endpoint = _route_endpoint(
        create_ibkr_router(
            _ibkr_deps(disconnect_ibkr_sessions=lambda **_kwargs: disconnected.append(True))
        ),
        "/api/ibkr/gateway-login",
        "POST",
    )

    payload = endpoint({})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_GATEWAY_LOGIN_CONFIRMATION_REQUIRED"
    assert disconnected == []


def test_ibkr_gateway_login_rejects_unconfigured_control_before_disconnect() -> None:
    disconnected: list[bool] = []
    endpoint = _route_endpoint(
        create_ibkr_router(
            _ibkr_deps(
                disconnect_ibkr_sessions=lambda **_kwargs: disconnected.append(True),
                ibkr_gateway_login_control_status=lambda: {"configured": False},
            )
        ),
        "/api/ibkr/gateway-login",
        "POST",
    )

    payload = endpoint({"confirmation": "START_NEW_IBKR_LOGIN"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_GATEWAY_LOGIN_CONTROL_NOT_CONFIGURED"
    assert disconnected == []


def test_ibkr_gateway_login_disconnects_sessions_and_requests_one_login() -> None:
    events: list[str] = []
    endpoint = _route_endpoint(
        create_ibkr_router(
            _ibkr_deps(
                disconnect_ibkr_sessions=lambda **_kwargs: (
                    events.append("disconnect") or {"ok": True}
                ),
                request_ibkr_gateway_login=lambda: (
                    events.append("request") or {"ok": True, "accepted": True}
                ),
            )
        ),
        "/api/ibkr/gateway-login",
        "POST",
    )

    payload = endpoint({"confirmation": "START_NEW_IBKR_LOGIN"})

    assert payload["ok"] is True
    assert payload["accepted"] is True
    assert events == ["disconnect", "request"]


def test_ibkr_gateway_login_preserves_typed_control_failure() -> None:
    def unavailable() -> dict[str, Any]:
        raise IbkrGatewayLoginControlError(
            code="IBKR_GATEWAY_LOGIN_CONTROL_UNAVAILABLE",
            message="control unavailable",
            retryable=True,
            retry_after_seconds=3.0,
        )

    endpoint = _route_endpoint(
        create_ibkr_router(_ibkr_deps(request_ibkr_gateway_login=unavailable)),
        "/api/ibkr/gateway-login",
        "POST",
    )

    payload = endpoint({"confirmation": "START_NEW_IBKR_LOGIN"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_GATEWAY_LOGIN_CONTROL_UNAVAILABLE"
    assert payload["error"]["retryable"] is True
    assert payload["retry_after_seconds"] == 3.0


def test_ibkr_reconnect_server_sleeping() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_ibkr_router(_ibkr_deps(server_sleeping=lambda: True)),
            "/api/ibkr/reconnect",
            "POST",
        )
        payload = await endpoint({})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "IBKR_SERVER_SLEEPING"
        assert payload["error"]["retryable"] is True

    asyncio.run(run())


def test_ibkr_reconnect_reports_failed_sessions() -> None:
    async def force_reconnect(**_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "host": "127.0.0.1",
            "port": 7497,
            "sessions": {
                "history": {"ok": False, "error": "gateway timeout"},
                "quotes": {"ok": True},
            },
            "quote_routes": [route_fingerprint(ibkr_future_payload("ES"))],
            "status": {},
        }

    async def run() -> None:
        import aef_terminal.ui.routers.ibkr as ibkr_module

        original = ibkr_module.ibkr_force_reconnect_async
        original_lookup = ibkr_module.lookup_runtime_instrument
        instrument_id, exact_lookup = _exact_lookup(ibkr_future_payload("ES"))
        ibkr_module.ibkr_force_reconnect_async = force_reconnect
        ibkr_module.lookup_runtime_instrument = exact_lookup
        try:
            endpoint = _route_endpoint(
                create_ibkr_router(_ibkr_deps()), "/api/ibkr/reconnect", "POST"
            )
            payload = await endpoint({"instrument_ids": [instrument_id]})
        finally:
            ibkr_module.ibkr_force_reconnect_async = original
            ibkr_module.lookup_runtime_instrument = original_lookup

        assert payload["ok"] is False
        assert payload["error"]["code"] == "IBKR_RECONNECT_FAILED"
        assert "gateway timeout" in payload["message"]

    asyncio.run(run())


def test_ibkr_quote_test_probe_failure() -> None:
    def probe_fail(_symbols: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("probe unavailable")

    import aef_terminal.ui.routers.ibkr as ibkr_module

    original = ibkr_module.ibkr_probe_quotes
    original_lookup = ibkr_module.lookup_runtime_instrument
    instrument = ibkr_stock_payload("SPY")
    instrument_id, exact_lookup = _exact_lookup(instrument)
    ibkr_module.ibkr_probe_quotes = probe_fail
    ibkr_module.lookup_runtime_instrument = exact_lookup
    try:
        endpoint = _route_endpoint(
            create_ibkr_router(
                _ibkr_deps(
                    apply_ibkr_runtime_settings=lambda _settings=None: {"ibkr_market_data_type": 1}
                )
            ),
            "/api/ibkr/quote-test",
            "POST",
        )
        payload = endpoint({"instrument_ids": [instrument_id]})
    finally:
        ibkr_module.ibkr_probe_quotes = original
        ibkr_module.lookup_runtime_instrument = original_lookup

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_QUOTE_PROBE_FAILED"
    assert payload["rows"] == []


def test_ibkr_quote_test_all_rows_failed() -> None:
    instrument = ibkr_stock_payload("SPY")
    instrument_id, exact_lookup = _exact_lookup(instrument)

    def probe_empty(_symbols: Any, **_kwargs: Any) -> dict[str, Any]:
        return {route_fingerprint(instrument): {"message": "no market data"}}

    import aef_terminal.ui.routers.ibkr as ibkr_module

    original = ibkr_module.ibkr_probe_quotes
    original_lookup = ibkr_module.lookup_runtime_instrument
    ibkr_module.ibkr_probe_quotes = probe_empty
    ibkr_module.lookup_runtime_instrument = exact_lookup
    try:
        endpoint = _route_endpoint(
            create_ibkr_router(
                _ibkr_deps(
                    apply_ibkr_runtime_settings=lambda _settings=None: {"ibkr_market_data_type": 1}
                )
            ),
            "/api/ibkr/quote-test",
            "POST",
        )
        payload = endpoint({"instrument_ids": [instrument_id]})
    finally:
        ibkr_module.ibkr_probe_quotes = original
        ibkr_module.lookup_runtime_instrument = original_lookup

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_QUOTE_TEST_FAILED"
    assert payload["rows"]
    assert payload["rows"][0]["ok"] is False


def test_ibkr_quote_test_requires_explicit_instrument_ids() -> None:
    endpoint = _route_endpoint(
        create_ibkr_router(
            _ibkr_deps(
                apply_ibkr_runtime_settings=lambda _settings=None: {"ibkr_market_data_type": 1}
            )
        ),
        "/api/ibkr/quote-test",
        "POST",
    )
    payload = endpoint({})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_INSTRUMENT_ID_REQUIRED"
    assert "instrument_ids" in payload["error"]["message"]
    assert payload["rows"] == []


def test_ibkr_quote_test_rejects_delimited_instrument_ids() -> None:
    endpoint = _route_endpoint(
        create_ibkr_router(
            _ibkr_deps(
                apply_ibkr_runtime_settings=lambda _settings=None: {"ibkr_market_data_type": 1}
            )
        ),
        "/api/ibkr/quote-test",
        "POST",
    )

    payload = endpoint({"instrument_ids": "id-a,id-b"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_INSTRUMENT_IDS_TYPED_REQUIRED"
    assert payload["rows"] == []


def test_ibkr_reconnect_rejects_delimited_instrument_ids_before_broker_call() -> None:
    async def run() -> None:
        endpoint = _route_endpoint(
            create_ibkr_router(_ibkr_deps()),
            "/api/ibkr/reconnect",
            "POST",
        )

        payload = await endpoint({"instrument_ids": "id-a,id-b"})

        assert payload["ok"] is False
        assert payload["error"]["code"] == "IBKR_INSTRUMENT_IDS_TYPED_REQUIRED"

    asyncio.run(run())
