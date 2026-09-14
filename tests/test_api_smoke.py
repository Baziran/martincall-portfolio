from __future__ import annotations

from types import SimpleNamespace

import pytest

from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.ui import client_settings as ui_client_settings
from tests.provider_payloads import ibkr_future_payload


_ES_INSTRUMENT = ibkr_future_payload("ES")
_ES_INSTRUMENT_ID = _ES_INSTRUMENT["instrument_id"]
_ES_ROUTE_FINGERPRINT = route_fingerprint(_ES_INSTRUMENT)


def _client():
    pytest.importorskip("httpx2")
    from fastapi.testclient import TestClient

    from aef_terminal.ui.app import app

    return TestClient(app)


def test_api_health_smoke() -> None:
    client = _client()
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)


def test_api_market_chart_only_smoke() -> None:
    client = _client()
    response = client.get(
        "/api/market",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "expected_route_fingerprint": _ES_ROUTE_FINGERPRINT,
            "interval": "5m",
            "range": "1d",
            "chart_only": True,
        },
    )
    assert response.status_code in {200, 503}
    payload = response.json()
    assert isinstance(payload, dict)
    if response.status_code == 503:
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
        assert detail.get("error", {}).get("code") == "MARKET_WATCHLIST_UNAVAILABLE"


def test_api_ticks_context_structured_shape() -> None:
    client = _client()
    response = client.get(
        "/api/ticks/context",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "expected_route_fingerprint": _ES_ROUTE_FINGERPRINT,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    assert "delta" in payload
    if payload["ok"] is False:
        assert payload["error"]["code"]
        assert payload["error"]["category"] == "tick"


def test_api_paper_trades_structured_shape() -> None:
    client = _client()
    response = client.get(
        "/api/paper/trades",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    if payload["ok"] is False:
        assert payload["error"]["code"]
        assert payload["error"]["category"] == "trading"
        assert payload["trades"] == []
    else:
        assert isinstance(payload.get("trades"), list)
        assert payload["instrument_id"] == _ES_INSTRUMENT_ID
        assert payload["route_fingerprint"] == _ES_ROUTE_FINGERPRINT


def test_api_paper_trades_requires_exact_route_pair() -> None:
    client = _client()

    assert client.get("/api/paper/trades").status_code == 422
    assert (
        client.get(
            "/api/paper/trades",
            params={"instrument_id": _ES_INSTRUMENT_ID},
        ).status_code
        == 422
    )


def test_api_paper_orders_requires_exact_route_pair() -> None:
    client = _client()

    assert client.get("/api/paper/orders").status_code == 422
    assert (
        client.get(
            "/api/paper/orders",
            params={"instrument_id": _ES_INSTRUMENT_ID},
        ).status_code
        == 422
    )


def test_api_paper_order_cancel_requires_only_exact_route_identity() -> None:
    client = _client()

    assert client.post("/api/paper/orders/order-1/cancel").status_code == 422
    response = client.post(
        "/api/paper/orders/order-1/cancel",
        json={
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
            "symbol": "ES",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PAPER_ORDER_CANCEL_PAYLOAD_INVALID"


def test_api_paper_order_update_requires_exact_route_identity_and_patch() -> None:
    client = _client()

    response = client.patch(
        "/api/paper/orders/order-1",
        json={"entry": 1.0},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PAPER_ORDER_UPDATE_PAYLOAD_INVALID"


def test_api_paper_trade_close_requires_client_command_and_exact_route_identity() -> None:
    client = _client()

    assert client.post("/api/paper/trades/position-1/close").status_code == 422
    response = client.post(
        "/api/paper/trades/position-1/close",
        json={
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PAPER_TRADE_CLOSE_PAYLOAD_INVALID"

    response = client.post(
        "/api/paper/trades/position-1/close",
        json={
            "command_id": "pc-00000000-0000-4000-8000-000000000021",
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
            "symbol": "ES",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PAPER_TRADE_CLOSE_PAYLOAD_INVALID"


def test_api_system_status_smoke() -> None:
    client = _client()
    response = client.get("/api/system")
    assert response.status_code in {200, 500}
    payload = response.json()
    assert isinstance(payload, dict)
    if response.status_code == 500:
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
        assert detail.get("error", {}).get("code") == "SYSTEM_STATUS_ERROR"


def test_api_storage_structured_shape() -> None:
    client = _client()
    response = client.get(
        "/api/storage",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
            "interval": "5m",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    if payload["ok"] is False:
        assert payload["error"]["code"]
        assert payload["error"]["category"] == "storage"


def test_api_gex_structured_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    store = SimpleNamespace(
        initialize=lambda: None,
        read_settings=lambda scope: {} if scope == "client" else None,
        read_watchlist_snapshot=lambda: ([_ES_INSTRUMENT], 1),
    )
    monkeypatch.setattr(
        ui_client_settings,
        "_DEPS",
        ui_client_settings.ClientSettingsDeps(store_factory=lambda: store),
    )
    response = client.get(
        "/api/gex",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "expected_route_fingerprint": _ES_ROUTE_FINGERPRINT,
            "enabled": "false",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    if payload.get("ok") is False and isinstance(payload.get("error"), dict):
        assert payload["error"]["code"]


def test_api_instruments_smoke() -> None:
    client = _client()
    response = client.get("/api/instruments")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    if payload.get("ok") is True:
        assert isinstance(payload.get("items"), list)
        assert isinstance(payload.get("watchlist_version"), int)


def test_api_drawings_backups_endpoint_removed() -> None:
    client = _client()
    response = client.get(
        "/api/drawings/backups",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
            "interval": "5m",
        },
    )
    assert response.status_code == 404


def test_api_data_providers_smoke() -> None:
    client = _client()
    response = client.get("/api/data-providers")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)


def test_api_gex_scheduler_smoke() -> None:
    client = _client()
    response = client.get("/api/gex/scheduler")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "checked_at" in payload


def test_api_paper_orders_structured_shape() -> None:
    client = _client()
    response = client.get(
        "/api/paper/orders",
        params={
            "instrument_id": _ES_INSTRUMENT_ID,
            "route_fingerprint": _ES_ROUTE_FINGERPRINT,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    if payload["ok"] is False:
        assert payload["error"]["code"]
        assert payload["error"]["category"] == "trading"
        assert payload["orders"] == []
    else:
        assert isinstance(payload.get("orders"), list)
        assert payload["instrument_id"] == _ES_INSTRUMENT_ID
        assert payload["route_fingerprint"] == _ES_ROUTE_FINGERPRINT


def test_api_paper_events_structured_shape() -> None:
    client = _client()
    response = client.get("/api/paper/events")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    if payload["ok"] is False:
        assert payload["error"]["code"]
        assert payload["error"]["category"] in {"trading", "storage"}
        assert payload["events"] == []
    else:
        assert isinstance(payload.get("events"), list)
