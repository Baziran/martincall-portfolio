from __future__ import annotations

import asyncio
from typing import Any

from aef_terminal.data.instrument_identity import (
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.settings_contract import (
    instrument_indicator_mode_setting_key,
    instrument_indicator_setting_key,
)
from aef_terminal.ui.app_runtime_wiring import _tick_flow_calc_enabled_for_restore
from aef_terminal.ui.routers.ticks import TickRouterDeps, create_tick_router
from tests.provider_payloads import ibkr_future_payload


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


async def _noop_set_live_enabled(
    _enabled: bool, _instrument_id: str, _route_fingerprint: str
) -> dict[str, Any]:
    return {"status": "ok"}


def _deps(**overrides: Any) -> TickRouterDeps:
    defaults = {
        "store_factory": lambda: None,
        "live_status": lambda: {"enabled": False},
        "set_live_enabled": _noop_set_live_enabled,
    }
    defaults.update(overrides)
    return TickRouterDeps(**defaults)


def test_tick_flow_live_restore_is_calc_gated_for_the_exact_instrument() -> None:
    instrument = ibkr_future_payload("ES")
    instrument["display"] = "E-mini S&P 500"
    instrument_id = qualified_instrument_id(instrument)
    regular_calc_key = instrument_indicator_setting_key(
        instrument_id,
        "tickFlowEnabled",
    )
    gex_calc_key = instrument_indicator_setting_key(
        instrument_id,
        "tickFlowEnabled",
        mode="gex",
    )
    foreign_instrument_id = "ibkr|future_root|ES|CME|USD|MES"
    foreign_calc_key = instrument_indicator_setting_key(
        foreign_instrument_id,
        "tickFlowEnabled",
    )

    assert not _tick_flow_calc_enabled_for_restore(
        {},
        instrument_id,
        instrument,
    )
    assert _tick_flow_calc_enabled_for_restore(
        {regular_calc_key: "true"},
        instrument_id,
        instrument,
    )
    assert not _tick_flow_calc_enabled_for_restore(
        {
            regular_calc_key: "false",
            foreign_calc_key: "true",
        },
        instrument_id,
        instrument,
    )
    assert _tick_flow_calc_enabled_for_restore(
        {
            instrument_indicator_mode_setting_key(instrument_id): "gex",
            regular_calc_key: "false",
            gex_calc_key: "true",
        },
        instrument_id,
        instrument,
    )


def test_tick_flow_live_restore_does_not_collide_on_shared_display_key() -> None:
    instrument = ibkr_future_payload("ES")
    instrument_id = qualified_instrument_id(instrument)
    other_instrument_id = "ibkr|future_root|ES|CME|USD|MES"

    assert not _tick_flow_calc_enabled_for_restore(
        {
            instrument_indicator_setting_key(
                other_instrument_id,
                "tickFlowEnabled",
            ): "true",
        },
        instrument_id,
        instrument,
    )


def test_tick_context_storage_unavailable() -> None:
    endpoint = _route_endpoint(create_tick_router(_deps()), "/api/ticks/context", "GET")
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_STORAGE_NOT_CONFIGURED"
    assert payload["delta"] == []


def test_tick_context_instrument_id_required() -> None:
    endpoint = _route_endpoint(create_tick_router(_deps()), "/api/ticks/context", "GET")
    payload = endpoint(instrument_id="", expected_route_fingerprint="")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_INSTRUMENT_ID_REQUIRED"


def test_tick_history_storage_unavailable() -> None:
    endpoint = _route_endpoint(create_tick_router(_deps()), "/api/ticks/history", "GET")
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_STORAGE_NOT_CONFIGURED"
    assert payload["rows"] == []


def test_tick_profile_history_instrument_id_required() -> None:
    endpoint = _route_endpoint(create_tick_router(_deps()), "/api/ticks/profile-history", "GET")
    payload = endpoint(instrument_id="", expected_route_fingerprint="")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_INSTRUMENT_ID_REQUIRED"


def test_tick_live_status_ok() -> None:
    endpoint = _route_endpoint(create_tick_router(_deps()), "/api/ticks/live", "GET")
    payload = endpoint()
    assert payload["ok"] is True
    assert "live" in payload


def test_tick_live_post_rejects_noncanonical_payload() -> None:
    calls: list[tuple[object, ...]] = []

    async def set_live_enabled(*args: object) -> dict[str, Any]:
        calls.append(args)
        return {"status": "ok"}

    async def run() -> None:
        endpoint = _route_endpoint(
            create_tick_router(_deps(set_live_enabled=set_live_enabled)),
            "/api/ticks/live",
            "POST",
        )
        invalid_payloads = (
            None,
            {
                "enabled": "false",
                "instrument_id": "",
                "expected_route_fingerprint": "",
            },
            {
                "enabled": False,
                "instrument_id": "",
                "expected_route_fingerprint": "",
                "legacy": True,
            },
            {
                "enabled": True,
                "instrument_id": "",
                "expected_route_fingerprint": "",
            },
        )
        for request in invalid_payloads:
            payload = await endpoint(request)
            assert payload["ok"] is False
            assert payload["error"]["code"] == "TICK_LIVE_PAYLOAD_INVALID"
            assert payload["error"]["retryable"] is False
            assert payload["message"]

    asyncio.run(run())
    assert calls == []


def test_tick_live_post_reports_error_status() -> None:
    async def set_live_enabled(
        _enabled: bool, _instrument_id: str, _route_fingerprint: str
    ) -> dict[str, Any]:
        return {"status": "error", "message": "ibkr unavailable"}

    async def run() -> None:
        endpoint = _route_endpoint(
            create_tick_router(_deps(set_live_enabled=set_live_enabled)),
            "/api/ticks/live",
            "POST",
        )
        instrument = ibkr_future_payload("ES")
        payload = await endpoint(
            {
                "enabled": True,
                "instrument_id": qualified_instrument_id(instrument),
                "expected_route_fingerprint": route_fingerprint(instrument),
            }
        )
        assert payload["ok"] is False
        assert payload["live"]["status"] == "error"
        assert payload["error"]["code"] == "TICK_LIVE_FAILED"
        assert payload["error"]["category"] == "tick"
        assert payload["message"] == "ibkr unavailable"

    asyncio.run(run())


def test_tick_live_post_structured_sleeping_error() -> None:
    async def set_live_enabled(
        _enabled: bool, _instrument_id: str, _route_fingerprint: str
    ) -> dict[str, Any]:
        return {"status": "sleeping", "last_error": "server is sleeping", "display_keys": ["ES"]}

    async def run() -> None:
        endpoint = _route_endpoint(
            create_tick_router(_deps(set_live_enabled=set_live_enabled)),
            "/api/ticks/live",
            "POST",
        )
        instrument = ibkr_future_payload("ES")
        payload = await endpoint(
            {
                "enabled": True,
                "instrument_id": qualified_instrument_id(instrument),
                "expected_route_fingerprint": route_fingerprint(instrument),
            }
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "SERVER_SLEEPING"
        assert payload["error"]["retryable"] is True

    asyncio.run(run())


class _TickStore:
    def initialize(self) -> None:
        return None

    def lookup_instrument(self, instrument_id):
        instrument = ibkr_future_payload("ES")
        return instrument if instrument_id == qualified_instrument_id(instrument) else None

    def read_tick_delta(
        self, _instrument_id, _route_fingerprint, _start, _end, *, bucket: str = "1 minute"
    ):
        return [
            {
                "ts": "2026-01-01T14:00:00+00:00",
                "total_volume": 120,
                "net_delta": 15,
                "trade_count": 8,
            }
        ]

    def read_volume_profile(
        self, _instrument_id, _route_fingerprint, _start, _end, *, price_step: float = 0.25
    ):
        assert price_step == 0.25
        return [
            {
                "price": 100.0,
                "total_volume": 80,
                "buy_volume": 50,
                "sell_volume": 30,
                "net_delta": 20,
                "trade_count": 6,
            },
            {
                "price": 100.25,
                "total_volume": 40,
                "buy_volume": 10,
                "sell_volume": 30,
                "net_delta": -20,
                "trade_count": 5,
            },
        ]


def test_tick_context_with_mock_store() -> None:
    endpoint = _route_endpoint(
        create_tick_router(_deps(store_factory=lambda: _TickStore())),
        "/api/ticks/context",
        "GET",
    )
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )
    assert payload["ok"] is True
    assert payload["symbol"] == "ES"
    assert len(payload["delta"]) == 1
    assert len(payload["profile"]) == 2
    assert payload["stats"]["total_volume"] == 120
    contract = indicator_manifest()["tick_flow"]["service_payload_contract"]
    assert set(payload) <= set(contract["top_level"])
    assert set(payload["delta"][0]) == set(contract["delta"])
    assert set(payload["profile"][0]) == set(contract["profile"])
    assert set(payload["aggregates"]) == set(contract["aggregates"])
    assert set(payload["stats"]) <= set(contract["stats"])
    assert "live" in payload["stats"]


def test_tick_context_uses_route_increment_not_name_based_profile() -> None:
    class ProfileMismatchStore(_TickStore):
        def lookup_instrument(self, instrument_id):
            instrument = ibkr_future_payload("ES")
            instrument["profile"] = "SPY"
            return instrument if instrument_id == qualified_instrument_id(instrument) else None

    endpoint = _route_endpoint(
        create_tick_router(_deps(store_factory=lambda: ProfileMismatchStore())),
        "/api/ticks/context",
        "GET",
    )
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )

    assert payload["ok"] is True
    assert payload["price_step"] == 0.25


def test_tick_context_fails_closed_without_route_increment() -> None:
    class MissingIncrementStore(_TickStore):
        def lookup_instrument(self, instrument_id):
            instrument = ibkr_future_payload("ES")
            instrument["contract_identity"]["current_contract"].pop("min_tick")
            return instrument if instrument_id == qualified_instrument_id(instrument) else None

    endpoint = _route_endpoint(
        create_tick_router(_deps(store_factory=lambda: MissingIncrementStore())),
        "/api/ticks/context",
        "GET",
    )
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_PRICE_INCREMENT_UNAVAILABLE"
    assert payload["profile"] == []


def test_tick_context_rejects_price_step_not_aligned_to_route_increment() -> None:
    endpoint = _route_endpoint(
        create_tick_router(_deps(store_factory=lambda: _TickStore())),
        "/api/ticks/context",
        "GET",
    )
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
        price_step=0.1,
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_PRICE_STEP_INVALID"
    assert payload["error"]["retryable"] is False


def test_tick_context_accepts_levels_for_backend_delta_summaries() -> None:
    endpoint = _route_endpoint(
        create_tick_router(_deps(store_factory=lambda: _TickStore())),
        "/api/ticks/context",
        "GET",
    )
    instrument = ibkr_future_payload("ES")
    payload = endpoint(
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
        price_step=0.25,
        levels='[{"name":"local high","price":100.0}]',
    )

    assert payload["ok"] is True
    assert payload["aggregates"]["level_deltas"][0]["name"] == "local high"
    assert payload["aggregates"]["level_deltas"][0]["volume"] == 120
    assert payload["aggregates"]["level_deltas"][0]["delta"] == 0


def test_tick_live_post_ok() -> None:
    async def set_live_enabled(
        _enabled: bool, _instrument_id: str, _route_fingerprint: str
    ) -> dict[str, Any]:
        return {
            "enabled": True,
            "running": True,
            "status": "starting",
            "display_keys": ["ES"],
            "route_fingerprints": [_route_fingerprint],
        }

    async def run() -> None:
        endpoint = _route_endpoint(
            create_tick_router(_deps(set_live_enabled=set_live_enabled)),
            "/api/ticks/live",
            "POST",
        )
        instrument = ibkr_future_payload("ES")
        payload = await endpoint(
            {
                "enabled": True,
                "instrument_id": qualified_instrument_id(instrument),
                "expected_route_fingerprint": route_fingerprint(instrument),
            }
        )
        assert payload["ok"] is True
        assert payload["live"]["status"] == "starting"
        assert payload["live"]["display_keys"] == ["ES"]
        assert payload["live"]["route_fingerprints"] == [route_fingerprint(instrument)]

    asyncio.run(run())
