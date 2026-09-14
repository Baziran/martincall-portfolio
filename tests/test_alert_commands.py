from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aef_terminal.ui.alert_commands as alert_commands
from aef_terminal.alerts.definition_identity import price_alert_definition_identity
from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.data.gex.constants import GEX_REQUEST_SNAPSHOT_SOURCE
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.alert_commands import alert_callback_token, execute_alert_command
from tests.provider_payloads import ibkr_stock_payload


class _Store:
    def __init__(self) -> None:
        self.price_alerts: list[dict] = []
        self.gex_row: dict | None = None
        self.create_calls: list[dict] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.mutation_calls: list[dict] = []
        self.deleted_scopes: list[tuple[str, str, str]] = []

    def initialize(self) -> None:
        return None

    def read_all_price_alerts(self) -> list[dict]:
        return list(self.price_alerts)

    def read_price_alert_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
    ) -> dict | None:
        return next(
            (
                item
                for item in self.price_alerts
                if item.get("id") == alert_id
                and item.get("instrument_id") == instrument_id
                and item.get("route_fingerprint") == route_fingerprint
            ),
            None,
        )

    def read_latest_gex_snapshot(self, instrument_id: str, route_fingerprint: str, *, source: str):
        assert source == GEX_REQUEST_SNAPSHOT_SOURCE
        assert self.gex_row is not None
        assert self.gex_row["instrument_id"] == instrument_id
        assert self.gex_row["route_fingerprint"] == route_fingerprint
        return self.gex_row

    def create_or_rearm_price_alert_exact(self, alert: dict, **kwargs) -> dict:
        self.create_calls.append({"alert": dict(alert), **kwargs})
        self.price_alerts.append(dict(alert))
        return {"created": True, "payload": dict(alert)}

    def delete_price_alert_exact(
        self, alert_id: str, instrument_id: str, route_fingerprint: str, **_kwargs
    ):
        self.deleted.append((alert_id, instrument_id, route_fingerprint))
        return self.read_price_alert_exact(alert_id, instrument_id, route_fingerprint)

    def rearm_price_alert_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        **kwargs,
    ) -> dict | None:
        current = self.read_price_alert_exact(alert_id, instrument_id, route_fingerprint)
        self.mutation_calls.append({"action": "rearm", **kwargs})
        if current is None or current["rearmedAt"] != kwargs.get("expected_rearmed_at"):
            return None
        updated = {
            **current,
            **dict(kwargs.get("definition_patch") or {}),
            "armed": True,
            "fired": False,
            "rearmedAt": kwargs["rearmed_at"],
        }
        self.price_alerts = [updated if item is current else item for item in self.price_alerts]
        return updated

    def set_price_alert_enabled_exact(
        self,
        alert_id: str,
        instrument_id: str,
        route_fingerprint: str,
        **kwargs,
    ) -> dict | None:
        current = self.read_price_alert_exact(alert_id, instrument_id, route_fingerprint)
        self.mutation_calls.append({"action": "enabled", **kwargs})
        if current is None or current["rearmedAt"] != kwargs.get("expected_rearmed_at"):
            return None
        updated = {
            **current,
            "enabled": kwargs["enabled"],
            "armed": kwargs["enabled"],
            "fired": False,
            "rearmedAt": kwargs["rearmed_at"],
        }
        self.price_alerts = [updated if item is current else item for item in self.price_alerts]
        return updated

    def delete_price_alert_scope_exact(
        self,
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str,
    ) -> int:
        self.deleted_scopes.append((instrument_id, route_fingerprint, timeframe))
        retained = [
            item
            for item in self.price_alerts
            if not (
                item.get("instrument_id") == instrument_id
                and item.get("route_fingerprint") == route_fingerprint
                and item.get("timeframe") == timeframe
            )
        ]
        deleted = len(self.price_alerts) - len(retained)
        self.price_alerts = retained
        return deleted


def test_price_create_uses_atomic_semantic_owner_and_exact_route() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()

    result = execute_alert_command(
        "create_price",
        {
            "instrument_id": route.instrument_id,
            "timeframe": "5m",
            "price": 1.0812,
            "direction": "above",
            "route_fingerprint": "untrusted-transport-value",
        },
        store_factory=lambda: store,
        instrument_lookup=lambda instrument_id: (
            instrument if instrument_id == route.instrument_id else None
        ),
    )

    assert result["ok"] is True
    assert len(store.create_calls) == 1
    saved = store.create_calls[0]["alert"]
    assert saved["instrument_id"] == route.instrument_id
    assert saved["route_fingerprint"] == route.fingerprint
    assert saved["price"] == 1.0812
    assert "level" not in saved
    assert result["alert"]["price"] == 1.0812
    assert "level" not in result["alert"]
    assert result["payload"]["definition_identity"] == price_alert_definition_identity(saved)


def test_drawing_alert_type_is_not_a_command_contract() -> None:
    store = _Store()

    result = execute_alert_command(
        "list",
        {"alert_type": "drawing"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: None,
    )

    assert result["ok"] is False
    assert result["code"] == "ALERT_TYPE_INVALID"


def test_price_create_rejects_legacy_level_and_direction_aliases() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    base = {
        "instrument_id": route.instrument_id,
        "timeframe": "5m",
    }

    level_alias = execute_alert_command(
        "create_price",
        {**base, "level": 500.0, "direction": "cross"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )
    direction_alias = execute_alert_command(
        "create_price",
        {**base, "price": 500.0, "direction": "up"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert level_alias["ok"] is False
    assert level_alias["code"] == "ALERT_PRICE_INVALID"
    assert direction_alias["ok"] is False
    assert direction_alias["code"] == "ALERT_DIRECTION_INVALID"
    assert store.create_calls == []


def test_alert_list_rejects_malformed_persisted_runtime_without_bool_coercion() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    created = execute_alert_command(
        "create_price",
        {
            "instrument_id": route.instrument_id,
            "timeframe": "5m",
            "price": 500.0,
            "direction": "above",
        },
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )
    assert created["ok"] is True
    store.price_alerts[0]["armed"] = 1

    result = execute_alert_command(
        "list",
        {},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert result["ok"] is False
    assert result["code"] == "ALERT_COMMAND_INVALID"
    assert "armed" in result["message"]


def test_gex_version_is_bound_to_persisted_level_payload(monkeypatch) -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    captured_at = datetime.now(tz=UTC).isoformat()
    payload = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at,
        "capture_mode": "request",
        "option_universe_expires_at": (datetime.now(tz=UTC) + timedelta(days=1)).isoformat(),
        "market_data_entitlement": "live",
        "decision_authoritative": True,
        "diagnostics": {"diag_frame_publishable": True},
        "call_wall": 5680.0,
        "put_wall": 5625.0,
        "gamma_flip": 5652.0,
        "levels": [],
    }
    store.gex_row = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at,
        "source": GEX_REQUEST_SNAPSHOT_SOURCE,
        "payload": payload,
    }
    monkeypatch.setattr(
        alert_commands,
        "read_latest_gex_payload",
        lambda *_args, **_kwargs: (store.gex_row, store.gex_row["payload"]),
    )

    def lookup(instrument_id):
        return instrument if instrument_id == route.instrument_id else None

    levels = execute_alert_command(
        "gex_levels",
        {"instrument_id": route.instrument_id},
        store_factory=lambda: store,
        instrument_lookup=lookup,
    )
    assert levels["ok"] is True
    assert levels["snapshot_version"].isalnum()
    assert levels["market_data_entitlement"] == "live"
    assert levels["decision_authoritative"] is True

    payload["call_wall"] = 5690.0
    changed = execute_alert_command(
        "create_gex",
        {
            "instrument_id": route.instrument_id,
            "timeframe": "5m",
            "selector": "CALL_WALL",
            "direction": "above",
            "snapshot_version": levels["snapshot_version"],
        },
        store_factory=lambda: store,
        instrument_lookup=lookup,
    )
    assert changed["ok"] is False
    assert changed["code"] == "ALERT_GEX_SELECTION_CHANGED"
    assert store.create_calls == []


def test_gex_alert_commands_reject_expired_exact_option_universe(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    captured_at = datetime.now(tz=UTC) - timedelta(minutes=1)
    payload = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at.isoformat(),
        "capture_mode": "request",
        "option_universe_expires_at": (datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
        "market_data_entitlement": "live",
        "decision_authoritative": True,
        "diagnostics": {"diag_frame_publishable": True},
        "call_wall": 5680.0,
        "put_wall": 5625.0,
        "gamma_flip": 5652.0,
        "levels": [],
    }
    store.gex_row = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at.isoformat(),
        "source": GEX_REQUEST_SNAPSHOT_SOURCE,
        "payload": payload,
    }
    monkeypatch.setattr(
        alert_commands,
        "read_latest_gex_payload",
        lambda *_args, **_kwargs: (
            store.gex_row,
            store.gex_row["payload"],
        ),
    )

    result = execute_alert_command(
        "gex_levels",
        {"instrument_id": route.instrument_id},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert result["ok"] is False
    assert result["code"] == "ALERT_GEX_OPTION_UNIVERSE_EXPIRED"


def test_gex_alert_commands_reject_every_nonlive_market_data_entitlement(monkeypatch) -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    captured_at = datetime.now(tz=UTC).isoformat()
    payload = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at,
        "capture_mode": "request",
        "market_data_entitlement": "unknown",
        "decision_authoritative": False,
        "diagnostics": {"diag_frame_publishable": True},
        "call_wall": 5680.0,
        "put_wall": 5625.0,
        "gamma_flip": 5652.0,
        "levels": [],
    }
    store.gex_row = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at,
        "source": GEX_REQUEST_SNAPSHOT_SOURCE,
        "payload": payload,
    }
    monkeypatch.setattr(
        alert_commands,
        "read_latest_gex_payload",
        lambda *_args, **_kwargs: (store.gex_row, store.gex_row["payload"]),
    )

    def lookup(instrument_id):
        return instrument if instrument_id == route.instrument_id else None

    for entitlement in ("frozen", "delayed", "delayed_frozen", "unknown"):
        payload["market_data_entitlement"] = entitlement
        result = execute_alert_command(
            "gex_levels",
            {"instrument_id": route.instrument_id},
            store_factory=lambda: store,
            instrument_lookup=lookup,
        )

        assert result["ok"] is False
        assert result["code"] == "ALERT_GEX_SNAPSHOT_DISPLAY_ONLY"
        assert f"market_data_entitlement={entitlement}" in result["message"]

    payload["market_data_entitlement"] = "delayed"
    creation = execute_alert_command(
        "create_gex",
        {
            "instrument_id": route.instrument_id,
            "timeframe": "5m",
            "selector": "CALL_WALL",
            "direction": "above",
            "snapshot_version": "stale-selection-token",
        },
        store_factory=lambda: store,
        instrument_lookup=lookup,
    )
    assert creation["ok"] is False
    assert creation["code"] == "ALERT_GEX_SNAPSHOT_DISPLAY_ONLY"
    assert store.create_calls == []


def test_stale_route_alert_can_be_deleted_but_not_resolved_for_chart() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    alert = {
        "id": "stale-price-alert",
        "instrument_id": route.instrument_id,
        "route_fingerprint": "stale-route-fingerprint",
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "symbol": "SPY",
        "timeframe": "5m",
        "price": 500.0,
        "kind": "price",
        "label": "",
        "direction": "cross",
        "toleranceAtr": 0.08,
        "tolerancePoints": 0,
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 1,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    store.price_alerts = [alert]
    token = alert_callback_token(
        alert["id"],
        alert["instrument_id"],
        alert["route_fingerprint"],
        alert_type="price",
    )

    deleted = execute_alert_command(
        "delete",
        {"callback_token": token, "alert_type": "price"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: (_ for _ in ()).throw(
            AssertionError("delete must not resolve current route")
        ),
    )
    assert deleted["ok"] is True
    assert store.deleted == [(alert["id"], route.instrument_id, "stale-route-fingerprint")]

    resolved = execute_alert_command(
        "resolve",
        {"callback_token": token, "alert_type": "price"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )
    assert resolved["ok"] is False
    assert resolved["code"] == "ALERT_ROUTE_MISMATCH"


def test_alert_mutation_rejects_callback_and_id_aliases() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    alert = {
        "id": "price-alert",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "symbol": "SPY",
        "timeframe": "5m",
        "price": 500.0,
        "kind": "price",
        "label": "",
        "direction": "cross",
        "toleranceAtr": 0.08,
        "tolerancePoints": 0,
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 1,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    store.price_alerts = [alert]
    callback_token = alert_callback_token(
        alert["id"],
        alert["instrument_id"],
        alert["route_fingerprint"],
        alert_type="price",
    )

    token_alias = execute_alert_command(
        "delete",
        {"token": callback_token, "alert_type": "price"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )
    id_alias = execute_alert_command(
        "delete",
        {"id": alert["id"], "alert_type": "price"},
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert token_alias["ok"] is False
    assert id_alias["ok"] is False
    assert store.deleted == []


def test_browser_create_uses_exact_route_and_server_owned_provider_metadata() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()

    result = execute_alert_command(
        "create",
        {
            "alert_id": "browser-alert",
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "provider": "untrusted-provider",
            "provider_contract_id": "untrusted-contract",
            "symbol": "untrusted-symbol",
            "timeframe": "5m",
            "price": 501.25,
            "kind": "ema233_touch",
            "label": "EMA touch",
            "direction": "cross",
            "toleranceAtr": 0.12,
            "tolerancePoints": 0.5,
            "rearmMinutes": 30,
        },
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert result["ok"] is True
    saved = result["payload"]
    assert saved["id"] == "browser-alert"
    assert saved["kind"] == "ema233_touch"
    assert saved["provider"] == route.provider
    assert saved["provider_contract_id"] == route.adapter.session_contract_id(route.instrument)
    assert saved["symbol"] == route.instrument_key
    assert saved["level_source"] == {"type": "ema233_touch", "dynamic": True}


def test_browser_update_is_generation_fenced_and_passes_one_definition_patch(
    monkeypatch,
) -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()
    created = execute_alert_command(
        "create_price",
        {
            "instrument_id": route.instrument_id,
            "timeframe": "5m",
            "price": 500.0,
            "direction": "cross",
        },
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )
    assert created["ok"] is True
    current = created["payload"]
    next_generation = current["rearmedAt"] + 10
    monkeypatch.setattr(alert_commands, "_created_at_ms", lambda: next_generation)

    updated = execute_alert_command(
        "update",
        {
            "alert_type": "price",
            "alert_id": current["id"],
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "expected_rearmed_at": current["rearmedAt"],
            "price": 502.0,
            "label": "moved",
        },
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert updated["ok"] is True
    assert updated["payload"]["price"] == 502.0
    assert updated["payload"]["rearmedAt"] == next_generation
    assert store.mutation_calls == [
        {
            "action": "rearm",
            "rearmed_at": next_generation,
            "definition_patch": {"price": 502.0, "label": "moved"},
            "expected_rearmed_at": current["rearmedAt"],
        }
    ]

    stale = execute_alert_command(
        "update",
        {
            "alert_type": "price",
            "alert_id": current["id"],
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "expected_rearmed_at": current["rearmedAt"],
            "price": 503.0,
        },
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )
    assert stale["ok"] is False
    assert stale["code"] == "ALERT_CONCURRENT_CHANGE"
    assert len(store.mutation_calls) == 1


def test_delete_scope_uses_one_exact_route_qualified_storage_command() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    store = _Store()

    result = execute_alert_command(
        "delete_scope",
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "timeframe": "5m",
        },
        store_factory=lambda: store,
        instrument_lookup=lambda _instrument_id: instrument,
    )

    assert result == {
        "ok": True,
        "action": "delete_scope",
        "alert_type": "price",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": "5m",
        "deleted": 0,
    }
    assert store.deleted_scopes == [(route.instrument_id, route.fingerprint, "5m")]
