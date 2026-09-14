from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aef_terminal.data.instrument_identity import qualified_instrument_id, route_fingerprint
from aef_terminal.data.ibkr_tick_feed import (
    IbkrTickFeed,
    IbkrTickFeedConfigurationError,
)
from aef_terminal.domain import Bar
from aef_terminal.engine.analysis_db import finer_timeframes
from aef_terminal.runtime.bar_series import dedupe_bars_by_timestamp
from aef_terminal.ui.routers.ticks import TickRouterDeps, create_tick_router
from aef_terminal.ui.services import tick_live_connector as tick_live_connector_module
from aef_terminal.ui.services.tick_live_connector import TickLiveConnectorService
from aef_terminal.ui.tick_services import (
    tick_context_response,
    tick_history_response,
    tick_live_post_response,
)
from tests.provider_payloads import ibkr_future_payload


def _bar(ts: str, close: float = 100.0) -> Bar:
    return Bar(
        ts=datetime.fromisoformat(ts).replace(tzinfo=UTC),
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1.0,
        symbol="ES",
    )


def test_finer_timeframes_for_15m() -> None:
    assert finer_timeframes("15m") == ["5m", "1m"]


def test_dedupe_bars_by_timestamp_keeps_latest_per_bucket() -> None:
    first = _bar("2026-01-01T14:00:00+00:00", close=100.0)
    second = _bar("2026-01-01T14:00:00+00:00", close=101.0)
    deduped = dedupe_bars_by_timestamp([first, second])
    assert len(deduped) == 1
    assert deduped[0].close == 101.0


def test_tick_context_symbol_required() -> None:
    payload = tick_context_response(None, instrument_id="", expected_route_fingerprint="")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_INSTRUMENT_ID_REQUIRED"
    assert payload["delta"] == []


def test_tick_context_storage_not_configured() -> None:
    instrument = ibkr_future_payload("ES")
    payload = tick_context_response(
        None,
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_STORAGE_NOT_CONFIGURED"


def test_tick_history_symbol_required() -> None:
    payload = tick_history_response(None, instrument_id="", expected_route_fingerprint="")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_INSTRUMENT_ID_REQUIRED"
    assert payload["rows"] == []


def test_tick_history_storage_not_configured() -> None:
    instrument = ibkr_future_payload("ES")
    payload = tick_history_response(
        None,
        instrument_id=qualified_instrument_id(instrument),
        expected_route_fingerprint=route_fingerprint(instrument),
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_STORAGE_NOT_CONFIGURED"


def test_tick_live_post_response_error() -> None:
    payload = tick_live_post_response({"status": "error", "last_error": "ibkr unavailable"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_LIVE_FAILED"
    assert payload["live"]["last_error"] == "ibkr unavailable"


def test_tick_live_post_response_accepts_explicit_terminal_error_contract() -> None:
    payload = tick_live_post_response(
        {"status": "error", "last_error": "invalid request"},
        error_code="TICK_LIVE_PAYLOAD_INVALID",
        retryable=False,
    )

    assert payload["error"]["code"] == "TICK_LIVE_PAYLOAD_INVALID"
    assert payload["error"]["retryable"] is False


def test_tick_live_post_response_ok() -> None:
    live = {
        "status": "starting",
        "display_keys": ["ES"],
        "route_fingerprints": [route_fingerprint(ibkr_future_payload("ES"))],
    }
    payload = tick_live_post_response(live)
    assert payload == {"ok": True, "live": live}


def test_tick_live_sleeping_attempt_is_typed_without_poisoning_status() -> None:
    instrument = ibkr_future_payload("ES")
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: True,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    result = asyncio.run(
        service._start(
            qualified_instrument_id(instrument),
            route_fingerprint(instrument),
        )
    )
    response = tick_live_post_response(
        result,
        error_code=result["error"]["code"],
        retryable=result["error"]["retryable"],
    )

    assert result["status"] == "sleeping"
    assert result["error"]["code"] == "SERVER_SLEEPING"
    assert result["error"]["retryable"] is True
    assert response["ok"] is False
    assert response["error"]["code"] == "SERVER_SLEEPING"
    assert service.status()["status"] == "off"
    assert "error" not in service.status()


@pytest.mark.parametrize(
    "min_tick",
    (None, True, "0.25", 0, -0.25, float("nan"), float("inf")),
)
def test_tick_feed_min_tick_rejection_has_terminal_typed_contract(
    min_tick: object,
) -> None:
    instrument = ibkr_future_payload("ES")
    instrument["contract_identity"]["current_contract"]["min_tick"] = min_tick

    with pytest.raises(IbkrTickFeedConfigurationError) as raised:
        IbkrTickFeed([instrument], SimpleNamespace())

    assert str(raised.value) == "IBKR_MIN_TICK_REQUIRED provider_symbol=ES"
    assert raised.value.code == "IBKR_MIN_TICK_REQUIRED"
    assert raised.value.retryable is False


def test_tick_live_start_returns_typed_terminal_feed_configuration_error(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")
    instrument["contract_identity"]["current_contract"].pop("min_tick")

    class Store:
        def initialize(self) -> None:
            return None

    service = TickLiveConnectorService(
        store_factory=Store,
    )
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=Store,
        logger=__import__("logging").getLogger("test"),
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )

    result = asyncio.run(
        service._start(
            qualified_instrument_id(instrument),
            route_fingerprint(instrument),
        )
    )

    assert result["status"] == "error"
    assert result["last_error"] == "IBKR_MIN_TICK_REQUIRED provider_symbol=ES"
    assert result["error"] == {
        "code": "IBKR_MIN_TICK_REQUIRED",
        "retryable": False,
        "message": "IBKR_MIN_TICK_REQUIRED provider_symbol=ES",
    }
    assert service.feed is None
    assert service.thread is None
    assert service.status()["error"] == result["error"]
    response = tick_live_post_response(
        result,
        error_code=result["error"]["code"],
        retryable=result["error"]["retryable"],
    )
    assert response["error"]["code"] == "IBKR_MIN_TICK_REQUIRED"
    assert response["error"]["retryable"] is False


def test_tick_live_generic_feed_construction_failure_is_typed_retryable(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")

    class Store:
        def initialize(self) -> None:
            return None

    class BrokenFeed:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("feed factory unavailable")

    service = TickLiveConnectorService(
        tick_feed_factory=BrokenFeed,
        store_factory=Store,
    )
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=Store,
        logger=__import__("logging").getLogger("test"),
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )

    result = asyncio.run(
        service._start(
            qualified_instrument_id(instrument),
            route_fingerprint(instrument),
        )
    )

    assert result["status"] == "error"
    assert result["error"] == {
        "code": "TICK_LIVE_START_FAILED",
        "retryable": True,
        "message": "feed factory unavailable",
    }
    assert service.feed is None
    assert service.thread is None
    assert service.status()["error"] == result["error"]


def test_tick_live_thread_start_failure_cleans_partial_feed_and_is_retryable(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")

    class Store:
        def initialize(self) -> None:
            return None

    class Feed:
        def __init__(self) -> None:
            self.stop_requested = False

        def request_stop(self) -> None:
            self.stop_requested = True

    class FailingThread:
        def __init__(self, **_kwargs) -> None:
            return None

        def start(self) -> None:
            raise RuntimeError("thread start unavailable")

        def is_alive(self) -> bool:
            return False

    feed = Feed()
    service = TickLiveConnectorService(
        tick_feed_factory=lambda **_kwargs: feed,
        store_factory=Store,
    )
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=Store,
        logger=__import__("logging").getLogger("test"),
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "threading",
        SimpleNamespace(Thread=FailingThread),
    )

    result = asyncio.run(
        service._start(
            qualified_instrument_id(instrument),
            route_fingerprint(instrument),
        )
    )

    assert result["status"] == "error"
    assert result["error"] == {
        "code": "TICK_LIVE_START_FAILED",
        "retryable": True,
        "message": "thread start unavailable",
    }
    assert feed.stop_requested is True
    assert service.feed is None
    assert service.thread is None
    assert service.status()["error"] == result["error"]


def test_tick_live_failed_switch_preserves_active_feed_without_poisoning_status(
    monkeypatch,
) -> None:
    active_instrument = ibkr_future_payload("ES")
    requested_instrument = ibkr_future_payload("NQ", con_id=11004969)
    active_identity = (
        qualified_instrument_id(active_instrument),
        route_fingerprint(active_instrument),
    )

    class Store:
        def initialize(self) -> None:
            return None

    class ActiveFeed:
        def __init__(self) -> None:
            self.stop_requested = False

        def request_stop(self) -> None:
            self.stop_requested = True

        def status(self) -> dict:
            return {
                "running": True,
                "display_keys": ["ES"],
                "route_fingerprints": [active_identity[1]],
                "route_identities": [
                    {
                        "instrument_id": active_identity[0],
                        "route_fingerprint": active_identity[1],
                    }
                ],
                "tick_subscriptions": 1,
                "buffer": {},
            }

    class StuckThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float] = []

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float) -> None:
            self.join_timeouts.append(timeout)

    feed = ActiveFeed()
    thread = StuckThread()
    service = TickLiveConnectorService(store_factory=Store)
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=Store,
        logger=__import__("logging").getLogger("test"),
    )
    service.feed = feed
    service.thread = thread
    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: requested_instrument,
    )

    result = asyncio.run(
        service._start(
            qualified_instrument_id(requested_instrument),
            route_fingerprint(requested_instrument),
        )
    )
    response = tick_live_post_response(
        result,
        error_code=result["error"]["code"],
        retryable=result["error"]["retryable"],
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "TICK_LIVE_START_FAILED"
    assert response["error"]["retryable"] is True
    assert result["status"] == "live"
    assert result["last_error"] == "tick live previous feed did not stop"
    assert service.feed is feed
    assert service.thread is thread
    assert feed.stop_requested is True
    assert thread.join_timeouts == [3.0]
    assert service.status()["status"] == "live"
    assert service.status()["route_identities"][0] == {
        "instrument_id": active_identity[0],
        "route_fingerprint": active_identity[1],
    }
    assert service.status()["last_error"] == ""
    assert "error" not in service.status()


def test_tick_live_partial_start_join_timeout_retains_ownership() -> None:
    class Feed:
        def __init__(self) -> None:
            self.stop_requested = False

        def request_stop(self) -> None:
            self.stop_requested = True

    class StuckThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float] = []

        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float) -> None:
            self.join_timeouts.append(timeout)

    feed = Feed()
    thread = StuckThread()
    service = TickLiveConnectorService()

    cleaned = asyncio.run(
        service._cleanup_partial_start(
            feed,
            thread,
            logger=__import__("logging").getLogger("test"),
        )
    )

    assert cleaned is False
    assert feed.stop_requested is True
    assert thread.join_timeouts == [3.0]
    assert service.feed is feed
    assert service.thread is thread


def test_tick_live_successful_start_clears_stored_terminal_error(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")
    fingerprint = route_fingerprint(instrument)
    factory_calls = 0

    class Store:
        def initialize(self) -> None:
            return None

    class Feed:
        def status(self) -> dict:
            return {
                "running": True,
                "display_keys": ["ES"],
                "route_fingerprints": [fingerprint],
                "route_identities": [
                    {
                        "instrument_id": qualified_instrument_id(instrument),
                        "route_fingerprint": fingerprint,
                    }
                ],
                "tick_subscriptions": 1,
                "buffer": {},
            }

        def request_stop(self) -> None:
            return None

    class LiveThread:
        def __init__(self, **_kwargs) -> None:
            self.alive = False

        def start(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

    feed = Feed()

    def feed_factory(**_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            raise IbkrTickFeedConfigurationError(
                "provider min tick is missing",
                code="IBKR_MIN_TICK_REQUIRED",
            )
        return feed

    service = TickLiveConnectorService(
        tick_feed_factory=feed_factory,
        store_factory=Store,
    )
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=Store,
        logger=__import__("logging").getLogger("test"),
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "threading",
        SimpleNamespace(Thread=LiveThread),
    )

    failed = asyncio.run(service._start(qualified_instrument_id(instrument), fingerprint))
    assert failed["error"]["code"] == "IBKR_MIN_TICK_REQUIRED"
    assert failed["error"]["retryable"] is False
    assert service.status()["error"] == failed["error"]

    started = asyncio.run(service._start(qualified_instrument_id(instrument), fingerprint))

    assert started["status"] == "live"
    assert started["last_error"] == ""
    assert "error" not in started
    assert service.last_error == ""
    assert service.last_error_code == ""
    assert service.last_error_retryable is True
    assert "error" not in service.status()


def test_tick_live_start_uses_preinitialized_storage(monkeypatch) -> None:
    instrument = ibkr_future_payload("ES")

    class BrokenStore:
        def initialize(self) -> None:
            raise AssertionError("tick connector must not initialize application storage")

    def feed_factory(**_kwargs):
        raise IbkrTickFeedConfigurationError(
            "provider min tick is missing",
            code="IBKR_MIN_TICK_REQUIRED",
        )

    service = TickLiveConnectorService(
        tick_feed_factory=feed_factory,
        store_factory=BrokenStore,
    )
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=BrokenStore,
        logger=__import__("logging").getLogger("test"),
    )
    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )

    result = asyncio.run(
        service._start(
            qualified_instrument_id(instrument),
            route_fingerprint(instrument),
        )
    )

    assert result["status"] == "error"
    assert result["error"] == {
        "code": "IBKR_MIN_TICK_REQUIRED",
        "retryable": False,
        "message": "provider min tick is missing",
    }
    assert service.status()["error"] == result["error"]


def test_tick_live_router_preserves_typed_connector_error() -> None:
    instrument = ibkr_future_payload("ES")

    async def rejected_start(
        _enabled: bool,
        _instrument_id: str,
        _route_fingerprint: str,
    ) -> dict:
        return {
            "enabled": False,
            "running": False,
            "status": "error",
            "last_error": "IBKR_MIN_TICK_REQUIRED provider_symbol=ES",
            "error": {
                "code": "IBKR_MIN_TICK_REQUIRED",
                "retryable": False,
                "message": "IBKR_MIN_TICK_REQUIRED provider_symbol=ES",
            },
        }

    router = create_tick_router(
        TickRouterDeps(
            store_factory=lambda: None,
            live_status=lambda: {"status": "off"},
            set_live_enabled=rejected_start,
        )
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/ticks/live"
        and "POST" in (getattr(route, "methods", set()) or set())
    )
    payload = asyncio.run(
        endpoint(
            {
                "enabled": True,
                "instrument_id": qualified_instrument_id(instrument),
                "expected_route_fingerprint": route_fingerprint(instrument),
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "IBKR_MIN_TICK_REQUIRED"
    assert payload["error"]["retryable"] is False
    assert payload["live"]["error"]["code"] == "IBKR_MIN_TICK_REQUIRED"


def test_tick_live_router_converts_unexpected_dependency_failure() -> None:
    instrument = ibkr_future_payload("ES")

    async def broken_start(
        _enabled: bool,
        _instrument_id: str,
        _route_fingerprint: str,
    ) -> dict:
        raise RuntimeError("connector unavailable")

    router = create_tick_router(
        TickRouterDeps(
            store_factory=lambda: None,
            live_status=lambda: {"status": "off"},
            set_live_enabled=broken_start,
        )
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/api/ticks/live"
        and "POST" in (getattr(route, "methods", set()) or set())
    )
    payload = asyncio.run(
        endpoint(
            {
                "enabled": True,
                "instrument_id": qualified_instrument_id(instrument),
                "expected_route_fingerprint": route_fingerprint(instrument),
            }
        )
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "TICK_LIVE_START_FAILED"
    assert payload["error"]["retryable"] is True
    assert payload["message"] == "connector unavailable"


def test_ensure_tick_live_from_intent_stops_running_feed_when_disabled(monkeypatch) -> None:
    stop_calls: list[str] = []
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    async def fake_stop(reason: str = "manual") -> dict:
        stop_calls.append(reason)
        return {
            "enabled": False,
            "running": False,
            "status": "off",
            "display_keys": [],
            "route_fingerprints": [],
        }

    monkeypatch.setattr(
        service,
        "intent_from_settings",
        lambda _settings: {"enabled": False, "symbol": ""},
    )
    monkeypatch.setattr(
        service,
        "status",
        lambda: {
            "enabled": True,
            "running": True,
            "status": "live",
            "display_keys": ["ES"],
            "route_fingerprints": [route_fingerprint(ibkr_future_payload("ES"))],
        },
    )
    monkeypatch.setattr(service, "thread", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(service, "stop", fake_stop)

    result = asyncio.run(service.ensure_from_intent(reason="monitor"))

    assert stop_calls == ["intent"]
    assert result["status"] == "off"


def test_tick_live_restore_clears_stale_intent_when_calc_is_off(
    monkeypatch,
) -> None:
    instrument = ibkr_future_payload("ES")
    instrument_id = qualified_instrument_id(instrument)
    fingerprint = route_fingerprint(instrument)
    persisted: list[tuple[bool, str, str, str]] = []
    starts: list[tuple[str, str]] = []
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {
            "aef:tickLive": {
                "enabled": True,
                "instrument_id": instrument_id,
                "route_fingerprint": fingerprint,
                "source": "api",
                "updated_at": "2026-07-29T12:00:00+00:00",
            }
        },
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: False,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )
    monkeypatch.setattr(
        "aef_terminal.ui.services.tick_live_connector.lookup_runtime_instrument",
        lambda exact_id: (
            instrument
            if exact_id == instrument_id
            else (_ for _ in ()).throw(ValueError("unexpected instrument"))
        ),
    )
    monkeypatch.setattr(
        service,
        "_persist_intent",
        lambda enabled, exact_id, exact_fingerprint, *, source: persisted.append(
            (enabled, exact_id, exact_fingerprint, source)
        ),
    )

    async def fake_start(exact_id: str, exact_fingerprint: str) -> dict:
        starts.append((exact_id, exact_fingerprint))
        return {"enabled": True, "running": True, "status": "live"}

    monkeypatch.setattr(service, "_start", fake_start)

    result = asyncio.run(service.ensure_from_intent(reason="startup"))

    assert result["status"] == "off"
    assert starts == []
    assert persisted == [(False, instrument_id, fingerprint, "calc_off")]


def test_tick_live_calc_off_stops_and_clears_persisted_intent(
    monkeypatch,
) -> None:
    stop_calls: list[str] = []
    intent_calls: list[tuple[bool, str, str, str]] = []
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    async def fake_stop(reason: str = "manual") -> dict:
        stop_calls.append(reason)
        return {
            "enabled": False,
            "running": False,
            "status": "off",
        }

    def fake_persist(
        enabled: bool,
        instrument_id: str,
        route_fingerprint: str,
        *,
        source: str,
    ) -> None:
        intent_calls.append(
            (
                enabled,
                instrument_id,
                route_fingerprint,
                source,
            )
        )

    monkeypatch.setattr(service, "stop", fake_stop)
    monkeypatch.setattr(service, "_persist_intent", fake_persist)

    result = asyncio.run(
        service.set_enabled(
            False,
            "instrument:exact",
            "route:exact",
        )
    )

    assert result["status"] == "off"
    assert stop_calls == ["manual"]
    assert intent_calls == [
        (
            False,
            "instrument:exact",
            "route:exact",
            "api",
        )
    ]


def test_tick_live_intent_commit_publishes_the_exact_durable_payload() -> None:
    persisted: list[tuple[str, str, dict[str, object]]] = []
    published: list[tuple[dict[str, object], int]] = []

    class Store:
        def upsert_setting(self, scope: str, key: str, value: dict[str, object]) -> int:
            persisted.append((scope, key, dict(value)))
            return 12

    service = TickLiveConnectorService(store_factory=Store)
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda settings, settings_revision: published.append(
            (dict(settings), settings_revision)
        ),
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=Store,
        logger=__import__("logging").getLogger("test"),
    )

    service._persist_intent(False, source="api")

    assert persisted[0][:2] == ("client", "aef:tickLive")
    assert published == [({"aef:tickLive": persisted[0][2]}, 12)]


def test_tick_live_disable_persistence_failure_keeps_feed_running(monkeypatch) -> None:
    stop_calls: list[str] = []
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    def fail_persist(*_args, **_kwargs) -> None:
        raise RuntimeError("storage unavailable")

    async def fake_stop(reason: str = "manual") -> dict:
        stop_calls.append(reason)
        return {"enabled": False, "running": False, "status": "off"}

    monkeypatch.setattr(service, "_persist_intent", fail_persist)
    monkeypatch.setattr(service, "stop", fake_stop)

    result = asyncio.run(service.set_enabled(False, "instrument:exact", "route:exact"))

    assert result["error"] == {
        "code": "TICK_LIVE_INTENT_PERSIST_FAILED",
        "retryable": True,
        "message": "storage unavailable",
    }
    assert stop_calls == []


def test_tick_live_enable_persistence_failure_rolls_feed_back(monkeypatch) -> None:
    stop_calls: list[str] = []
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    async def fake_start(_instrument_id: str, _route_fingerprint: str) -> dict:
        return {"enabled": True, "running": True, "status": "live"}

    def fail_persist(*_args, **_kwargs) -> None:
        raise RuntimeError("storage unavailable")

    async def fake_stop(reason: str = "manual") -> dict:
        stop_calls.append(reason)
        return {"enabled": False, "running": False, "status": "off"}

    monkeypatch.setattr(service, "_start", fake_start)
    monkeypatch.setattr(service, "_persist_intent", fail_persist)
    monkeypatch.setattr(service, "stop", fake_stop)

    result = asyncio.run(service.set_enabled(True, "instrument:exact", "route:exact"))

    assert result["error"]["code"] == "TICK_LIVE_INTENT_PERSIST_FAILED"
    assert stop_calls == ["intent_persist_failed"]


def test_tick_live_calc_off_persistence_failure_keeps_feed_running(monkeypatch) -> None:
    instrument = ibkr_future_payload("ES")
    instrument_id = qualified_instrument_id(instrument)
    fingerprint = route_fingerprint(instrument)
    stop_calls: list[str] = []
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {
            "aef:tickLive": {
                "enabled": True,
                "instrument_id": instrument_id,
                "route_fingerprint": fingerprint,
                "source": "api",
                "updated_at": "2026-07-29T12:00:00+00:00",
            }
        },
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: False,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    monkeypatch.setattr(
        tick_live_connector_module,
        "lookup_runtime_instrument",
        lambda _instrument_id: instrument,
    )
    monkeypatch.setattr(
        service,
        "status",
        lambda: {"enabled": True, "running": True, "status": "live"},
    )

    def fail_persist(*_args, **_kwargs) -> None:
        raise RuntimeError("storage unavailable")

    async def fake_stop(reason: str = "manual") -> dict:
        stop_calls.append(reason)
        return {"enabled": False, "running": False, "status": "off"}

    monkeypatch.setattr(service, "_persist_intent", fail_persist)
    monkeypatch.setattr(service, "stop", fake_stop)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        asyncio.run(service.ensure_from_intent(reason="monitor"))

    assert stop_calls == []


def test_tick_live_enable_cancellation_waits_for_intent_persistence(monkeypatch) -> None:
    persistence_started = threading.Event()
    persistence_release = threading.Event()
    persistence_finished = threading.Event()
    service = TickLiveConnectorService()
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=lambda: None,
        logger=__import__("logging").getLogger("test"),
    )

    async def fake_start(_instrument_id: str, _route_fingerprint: str) -> dict:
        return {"enabled": True, "running": True, "status": "live"}

    def blocking_persist(*_args, **_kwargs) -> None:
        persistence_started.set()
        persistence_release.wait(timeout=2.0)
        persistence_finished.set()

    monkeypatch.setattr(service, "_start", fake_start)
    monkeypatch.setattr(service, "_persist_intent", blocking_persist)

    async def run_probe() -> None:
        task = asyncio.create_task(service.set_enabled(True, "instrument:exact", "route:exact"))
        assert await asyncio.to_thread(persistence_started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        persistence_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run_probe())
    finally:
        persistence_release.set()

    assert persistence_finished.is_set()


def test_start_tick_live_creates_store_off_event_loop(monkeypatch) -> None:
    loop_thread_id = threading.get_ident()
    store_thread_ids: list[int] = []

    class FakeFeed:
        def __init__(self, *, instruments, **_kwargs):
            self._display_keys = [item["instrument_key"] for item in instruments]
            self._route_fingerprints = [route_fingerprint(item) for item in instruments]
            self._route_identities = [
                {
                    "instrument_id": qualified_instrument_id(item),
                    "route_fingerprint": route_fingerprint(item),
                }
                for item in instruments
            ]
            self._running = True

        async def run_forever(self) -> None:
            while self._running:
                await asyncio.sleep(0.01)

        def request_stop(self) -> None:
            self._running = False

        def status(self) -> dict:
            return {
                "running": self._running,
                "display_keys": self._display_keys,
                "route_fingerprints": self._route_fingerprints,
                "route_identities": self._route_identities,
                "tick_subscriptions": 1,
                "buffer": {},
            }

    class Store:
        def initialize(self):
            return None

        def lookup_instrument(self, instrument_id):
            instrument = ibkr_future_payload("ES")
            return instrument if instrument_id == qualified_instrument_id(instrument) else None

    def fake_store():
        store_thread_ids.append(threading.get_ident())
        return Store()

    service = TickLiveConnectorService(
        tick_feed_factory=FakeFeed,
        store_factory=fake_store,
    )
    service.configure(
        server_sleeping=lambda: False,
        client_settings_snapshot=lambda: {},
        publish_client_settings_patch=lambda _settings, settings_revision: None,
        restore_allowed=lambda _settings, _instrument_id, _instrument: True,
        store_factory=fake_store,
        logger=__import__("logging").getLogger("test"),
    )
    instrument = ibkr_future_payload("ES")
    monkeypatch.setattr(
        "aef_terminal.ui.services.tick_live_connector.lookup_runtime_instrument",
        lambda instrument_id: (
            instrument
            if instrument_id == qualified_instrument_id(instrument)
            else (_ for _ in ()).throw(ValueError("instrument id is not stored"))
        ),
    )

    async def run_probe() -> dict:
        result = await service._start(
            qualified_instrument_id(instrument), route_fingerprint(instrument)
        )
        await service.stop(reason="test")
        return result

    result = asyncio.run(run_probe())

    assert result["status"] == "live"
    assert result["display_keys"] == ["ES"]
    assert result["route_fingerprints"] == [route_fingerprint(ibkr_future_payload("ES"))]
    assert store_thread_ids
    assert store_thread_ids[0] != loop_thread_id


def test_tick_live_runtime_facade_uses_connector_service() -> None:
    from aef_terminal.ui.runtime import tick_live as tick_live_runtime

    runtime_source = inspect.getsource(tick_live_runtime)
    connector_source = inspect.getsource(TickLiveConnectorService)

    assert isinstance(tick_live_runtime.tick_live_connector(), TickLiveConnectorService)
    assert not hasattr(tick_live_runtime, "start_tick_live")
    assert not hasattr(tick_live_runtime, "persist_tick_live_intent")
    assert not hasattr(TickLiveConnectorService, "start")
    assert not hasattr(TickLiveConnectorService, "persist_intent")
    assert "_TICK_LIVE_FEED" not in runtime_source
    assert "_TICK_LIVE_THREAD" not in runtime_source
    assert "_TICK_LIVE_LAST_ERROR" not in runtime_source
    assert "from aef_terminal.storage.postgres import default_postgres_store" not in runtime_source
    assert (
        "from aef_terminal.storage.postgres import default_postgres_store" not in connector_source
    )
    assert "start_tick_live" not in runtime_source
    assert "persist_tick_live_intent" not in runtime_source
    assert "run_tick_live_thread" not in runtime_source
    assert "tick_live_intent_from_settings" not in runtime_source
    assert "return await _TICK_LIVE_CONNECTOR.set_enabled(" in runtime_source
    assert "asyncio.to_thread" not in connector_source
