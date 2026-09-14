from datetime import UTC, datetime
from typing import Any

import pytest

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.alerts.definition_identity import price_alert_definition_identity
from aef_terminal.alerts.runtime_contract import validate_price_alert
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.alert_runtime_rules import alert_monitor_payload
from aef_terminal.ui.alert_services import (
    normalize_price_alert as _normalize_price_alert,
    require_unique_price_alerts as _require_unique_price_alerts,
)
from aef_terminal.ui.server_alert_prices import server_alert_price_snapshot
from tests.provider_payloads import ibkr_future_payload


def _canonical_alert(alert_id: str, **overrides: Any) -> dict[str, Any]:
    route = route_instrument(ibkr_future_payload("ES"))
    return {
        "id": alert_id,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "symbol": "ES",
        "timeframe": "5m",
        "kind": "price",
        "label": "",
        "direction": "cross",
        "price": 100.0,
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 1,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
        **overrides,
    }


def test_alert_monitor_payload_carries_qualified_route_identity() -> None:
    route = route_instrument(ibkr_future_payload("ES"))

    payload = alert_monitor_payload(
        _canonical_alert("alert-1", price=7500.0),
        7500.25,
    )

    assert payload["alert_id"] == "alert-1"
    assert payload["instrument_id"] == route.instrument_id
    assert payload["route_fingerprint"] == route.fingerprint
    assert payload["price"] == 7500.0
    assert "id" not in payload
    assert "level" not in payload


def test_server_alert_snapshot_fails_closed_without_live_quote(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "aef_terminal.ui.server_alert_prices.execution_quote_geometry",
        lambda _quote: (None, None, None),
    )

    assert server_alert_price_snapshot(None) is None


def test_server_alert_snapshot_requires_authoritative_quote_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.ui.server_alert_prices.execution_quote_geometry",
        lambda _quote: (100.0, 99.0, 101.0),
    )
    monkeypatch.setattr(
        "aef_terminal.ui.server_alert_prices.quote_snapshot_ts",
        lambda _quote: None,
    )

    assert server_alert_price_snapshot({"price": 100.0}) is None


def test_server_alert_snapshot_accepts_signed_finite_quote_geometry(monkeypatch) -> None:
    ts = datetime(2026, 5, 8, 7, 47, tzinfo=UTC)
    monkeypatch.setattr(
        "aef_terminal.ui.server_alert_prices.execution_quote_geometry",
        lambda _quote: (0.0, -1.0, 1.0),
    )
    monkeypatch.setattr(
        "aef_terminal.ui.server_alert_prices.quote_snapshot_ts",
        lambda _quote: ts,
    )

    snapshot = server_alert_price_snapshot({"price": 0.0})

    assert snapshot == {
        "price": 0.0,
        "high": 1.0,
        "low": -1.0,
        "ts": ts,
        "source": "quote",
    }


def test_server_alert_snapshot_uses_only_fresh_quote(monkeypatch) -> None:
    ts = datetime(2026, 5, 8, 7, 47, tzinfo=UTC)
    monkeypatch.setattr(
        "aef_terminal.ui.server_alert_prices.execution_quote_geometry",
        lambda _quote: (5001.0, 5000.75, 5001.25),
    )
    monkeypatch.setattr("aef_terminal.ui.server_alert_prices.quote_snapshot_ts", lambda _quote: ts)
    snapshot = server_alert_price_snapshot({"price": 5001.0})

    assert snapshot is not None
    assert snapshot["source"] == "quote"
    assert snapshot["price"] == 5001.0
    assert "bar_slot" not in snapshot


def test_price_alert_projection_rejects_duplicate_semantic_definition() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    alerts = [
        _canonical_alert("old", price=7545.9),
        _canonical_alert("new", price=7545.9),
    ]

    with pytest.raises(ValueError, match="PRICE_ALERT_SEMANTIC_DUPLICATE"):
        _require_unique_price_alerts(
            alerts,
            timeframe="5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            provider=route.provider,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
        )


def test_price_alert_projection_exposes_exact_server_definition_identity() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    alerts = [
        _canonical_alert("lower", price=100.001),
        _canonical_alert("higher", price=100.004),
    ]

    projected = _require_unique_price_alerts(
        alerts,
        timeframe="5m",
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        provider=route.provider,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
    )

    assert projected[0]["definition_identity"] == price_alert_definition_identity(alerts[0])
    assert projected[1]["definition_identity"] == price_alert_definition_identity(alerts[1])
    assert projected[0]["definition_identity"] != projected[1]["definition_identity"]


def test_price_alert_normalizer_requires_canonical_price_and_direction() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    identity = {
        "timeframe": "5m",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
    }

    definition = {
        "id": "canonical",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "symbol": "ES",
        "timeframe": "5m",
        "price": 7545.9,
        "kind": "price",
        "label": "",
        "direction": "cross",
        "toleranceAtr": 0.08,
        "tolerancePoints": 0,
        "enabled": True,
        "rearmedAt": 0,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
    }
    canonical = _normalize_price_alert(definition, **identity)

    assert canonical["price"] == 7545.9
    assert "level" not in canonical
    renamed_display = _normalize_price_alert(
        {**definition, "symbol": "LEGACY-DISPLAY"},
        **identity,
    )
    assert renamed_display["symbol"] == "LEGACY-DISPLAY"
    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_FORBIDDEN: level"):
        _normalize_price_alert({**definition, "level": 7545.9}, **identity)
    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_INVALID: canonical direction"):
        _normalize_price_alert({**definition, "direction": "touch"}, **identity)
    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_INVALID: canonical kind"):
        _normalize_price_alert({**definition, "kind": "ema233"}, **identity)
    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_INVALID: canonical toleranceAtr"):
        invalid = dict(definition)
        invalid.pop("toleranceAtr")
        _normalize_price_alert(invalid, **identity)


def test_price_alert_definition_identity_separates_direction() -> None:
    base = _canonical_alert("direction", price=7545.9)

    assert price_alert_definition_identity(
        {**base, "direction": "above"}
    ) != price_alert_definition_identity({**base, "direction": "below"})


def test_price_alert_definition_identity_preserves_close_fx_levels() -> None:
    base = _canonical_alert("fx-level")

    assert price_alert_definition_identity(
        {**base, "price": 1.0812}
    ) != price_alert_definition_identity({**base, "price": 1.0860})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("armed", 1),
        ("cooldownUntil", "0"),
        ("lastFiredLevel", "100"),
        ("lastEventTs", "2026-07-21T22:00:00"),
        ("telegramRetryCount", 1.0),
        ("telegramDeliveryStatus", "queued"),
        ("touchDirection", "up"),
        ("wasTouching", 0),
    ],
)
def test_price_alert_persisted_runtime_rejects_malformed_fields(
    field: str,
    value: object,
) -> None:
    runtime = _canonical_alert("strict-runtime", **{field: value})

    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_INVALID"):
        validate_price_alert(runtime)


@pytest.mark.parametrize(
    "level_source",
    [
        None,
        {},
        {"type": [], "dynamic": False},
        {"type": "fixed_price", "dynamic": True},
        {"type": "ema233_touch", "dynamic": True},
        {"type": "fixed_price", "dynamic": False, "legacy": True},
    ],
)
def test_price_alert_definition_rejects_noncanonical_level_source(
    level_source: object,
) -> None:
    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_INVALID"):
        validate_price_alert(_canonical_alert("strict-source", level_source=level_source))


def test_price_alert_persisted_runtime_accepts_canonical_pending_payload() -> None:
    alert = _canonical_alert("strict-pending", price=7_500.0)
    pending_payload = alert_monitor_payload(
        alert,
        7_500.25,
        trigger_event_at=1_000,
    )
    runtime = {
        **alert,
        "armed": False,
        "fired": True,
        "cooldownUntil": 2_000,
        "rearmedAt": 1,
        "telegramDeliveryStatus": "pending",
        "telegramPendingPayload": pending_payload,
        "telegramPendingSince": 1_000,
        "telegramNextRetryAt": 61_000,
        "telegramRetryCount": 0,
        "telegramClaimedAt": 0,
        "telegramLastError": "",
    }

    validate_price_alert(runtime)

    runtime.update(
        {
            "telegramDeliveryStatus": "delivering",
            "telegramClaimedAt": 1_000,
        }
    )
    validate_price_alert(runtime)


def test_ema233_touch_semantic_key_ignores_trailing_level() -> None:
    base = _canonical_alert(
        "ema-touch",
        kind="ema233_touch",
        level_source={"type": "ema233_touch", "dynamic": True},
    )

    assert price_alert_definition_identity(
        {**base, "price": 7520.25}
    ) == price_alert_definition_identity({**base, "price": 7541.75})


def test_price_alert_projection_rejects_duplicate_dynamic_definition() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    alerts = [
        _canonical_alert(
            "old",
            kind="ema233_touch",
            label="EMA 233 touch",
            price=7520.25,
            level_source={"type": "ema233_touch", "dynamic": True},
        ),
        _canonical_alert(
            "new",
            kind="ema233_touch",
            label="EMA 233 touch",
            price=7541.75,
            level_source={"type": "ema233_touch", "dynamic": True},
        ),
    ]

    with pytest.raises(ValueError, match="PRICE_ALERT_SEMANTIC_DUPLICATE"):
        _require_unique_price_alerts(
            alerts,
            timeframe="5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            provider=route.provider,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
        )


@pytest.mark.parametrize(
    "forbidden_field",
    ["level", "drawingAnchor", "lastAnchorBarSlot", "dragging", "deleted", "deletedAt"],
)
def test_price_alert_save_rejects_retired_fields(forbidden_field: str) -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    alert = {
        "id": "invalid-alert",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "symbol": "ES",
        "timeframe": "5m",
        "price": 7386.5,
        "kind": "price",
        "label": "",
        "direction": "cross",
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "enabled": True,
        "rearmedAt": 1,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        forbidden_field: (
            {
                "objectId": "channel-1",
                "objectType": "channel",
                "level": 2.25,
                "label": "ghost 2 25%",
                "priceOffset": 0,
            },
            123,
        )[forbidden_field == "lastAnchorBarSlot"],
    }

    with pytest.raises(ValueError, match=f"PRICE_ALERT_FIELD_FORBIDDEN: {forbidden_field}"):
        _normalize_price_alert(
            alert,
            timeframe="5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            provider=route.provider,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
        )


def test_price_alert_save_rejects_malformed_client_row_instead_of_dropping_it() -> None:
    route = route_instrument(ibkr_future_payload("ES"))

    with pytest.raises(ValueError, match="PRICE_ALERT_ROW_INVALID"):
        _normalize_price_alert(
            None,
            timeframe="5m",
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            provider=route.provider,
            provider_contract_id=route.adapter.session_contract_id(route.instrument),
        )
