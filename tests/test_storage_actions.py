import inspect

import pytest

from aef_terminal.ui.storage_actions import (
    StorageActionDeps,
    client_storage_payload,
    create_option_target_payload,
    delete_option_target_payload,
    save_client_settings_payload,
    save_drawings_payload,
    update_option_target_payload,
)
from aef_terminal.ui.storage_actions import option_targets as option_target_actions
from aef_terminal.ui.storage_actions import client as client_actions
from aef_terminal.ui.storage_actions import drawings as drawing_actions
from aef_terminal.ui import gex_actions, option_target_runtime
from aef_terminal import settings_contract
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.gex.option_target_contract import OptionTargetCreateResult
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore
from aef_terminal.storage.repos.drawings import DrawingsRepoMixin
from aef_terminal.storage.repos.settings import SettingsRepoMixin
from aef_terminal.settings_contract import WATCHLIST_PRESENTATION_SCOPE
from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from tests.provider_payloads import coinbase_btc_payload, ibkr_future_payload, ibkr_stock_payload


_SETTINGS_WRITER_ID = "123e4567-e89b-42d3-a456-426614174000"
_SETTINGS_CHANGED_AT_MS = 1_750_000_000_000
_SETTINGS_REVISION = 7


def _canonical_price_alert(route, alert_id: str, **overrides):
    return {
        "id": alert_id,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "symbol": route.instrument_key,
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


def _test_settings_repo() -> PostgresStore:
    return PostgresStore(
        None,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )


@pytest.fixture(autouse=True)
def exact_runtime_instrument_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    instruments = [ibkr_stock_payload("ES"), coinbase_btc_payload()]
    by_instrument_id = {
        route_instrument(instrument).instrument_id: instrument for instrument in instruments
    }

    def lookup(instrument_id: str) -> dict:
        return by_instrument_id[instrument_id]

    monkeypatch.setattr(client_actions, "lookup_runtime_instrument", lookup)
    monkeypatch.setattr(drawing_actions, "lookup_runtime_instrument", lookup)
    monkeypatch.setattr(option_target_actions, "lookup_runtime_instrument", lookup)


def _deps(**overrides) -> StorageActionDeps:
    defaults = {
        "store_factory": lambda: None,
        "apply_ibkr_runtime_settings": lambda _settings=None: {"ibkr_port": 4002},
        "publish_client_settings_mutations": lambda _mutations, settings_revision: None,
        "normalize_drawing_anchors": (
            lambda store, instrument_id, interval, drawings, **kwargs: drawings
        ),
        "require_unique_price_alerts": lambda alerts, **kwargs: alerts,
    }
    defaults.update(overrides)
    return StorageActionDeps(**defaults)


def test_client_storage_payload_accepts_current_settings_without_presentation_fields() -> None:
    route = route_instrument(ibkr_stock_payload("ES"))

    class Store:
        def initialize(self):
            return None

        def read_settings_snapshot(self, scope):
            values = {
                "aef:motionPreference": "system",
                "aef:serverSleep": {
                    "sleeping": False,
                    "reason": "wake",
                    "changed_at": "2026-07-22T00:00:00+00:00",
                    "duration_seconds": 0.0,
                },
                "aef:tickLive": {
                    "enabled": False,
                    "instrument_id": "",
                    "route_fingerprint": "",
                    "source": "api",
                    "updated_at": "2026-07-22T00:00:00+00:00",
                },
                "aef:workspace:1:timeframe": "5m",
                'aef:workspace:1:range:["instrument-a","5m"]': "5d",
                "aef:indicator:global:ema20Enabled": "true",
                'aef:instrument:["instrument-a","regular"]:indicator:barRadarVisible': "true",
                'aef:instrument:["instrument-a"]:indicatorMode': "regular",
                'aef:instrument:["instrument-a"]:paperRiskPrefs': (
                    '{"orderType":"limit","qty":1,"useStopLoss":true,'
                    '"stopPoints":8,"useTarget":true,"targetPoints":16}'
                ),
                'aef:instrument:["instrument-a","gex"]:indicator:gexContextEnabled': "true",
            }
            orders = {key: {"changed_at_ms": 0, "writer_id": None, "sequence": 0} for key in values}
            orders["aef:motionPreference"] = {
                "changed_at_ms": _SETTINGS_CHANGED_AT_MS,
                "writer_id": _SETTINGS_WRITER_ID,
                "sequence": 1,
            }
            return values, orders, _SETTINGS_CHANGED_AT_MS, _SETTINGS_REVISION

        def read_drawings(self, instrument_id, interval, *, route_fingerprint):
            assert instrument_id == route.instrument_id
            assert route_fingerprint == route.fingerprint
            return [
                {
                    "id": "draw-1",
                    "type": "line",
                    "alertOnCross": True,
                    "alertArmed": False,
                    "alertFired": True,
                    "alertCooldownUntil": 1,
                    "alertRearmedAt": 1,
                    **TELEGRAM_CANCELLED_DELIVERY_STATE,
                }
            ]

        def read_price_alerts(self, instrument_id, interval, *, route_fingerprint):
            assert instrument_id == route.instrument_id
            assert route_fingerprint == route.fingerprint
            return [
                _canonical_price_alert(
                    route,
                    "alert-1",
                    armed=False,
                    fired=True,
                    cooldownUntil=1,
                )
            ]

    payload = client_storage_payload(
        _deps(store_factory=lambda: Store()),
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        interval="5m",
    )

    assert payload["ok"] is True
    assert payload["symbol"] == "ES"
    assert payload["settings_mutation_changed_at_ms"] == _SETTINGS_CHANGED_AT_MS
    assert payload["settings_revision"] == _SETTINGS_REVISION
    assert payload["settings_mutations"][0] == {
        "key": "aef:motionPreference",
        "changed_at_ms": _SETTINGS_CHANGED_AT_MS,
        "writer_id": _SETTINGS_WRITER_ID,
        "sequence": 1,
    }
    assert payload["settings"] == {
        "aef:motionPreference": "system",
        "aef:serverSleep": {
            "sleeping": False,
            "reason": "wake",
            "changed_at": "2026-07-22T00:00:00+00:00",
            "duration_seconds": 0.0,
        },
        "aef:tickLive": {
            "enabled": False,
            "instrument_id": "",
            "route_fingerprint": "",
            "source": "api",
            "updated_at": "2026-07-22T00:00:00+00:00",
        },
        "aef:workspace:1:timeframe": "5m",
        'aef:workspace:1:range:["instrument-a","5m"]': "5d",
        "aef:indicator:global:ema20Enabled": "true",
        'aef:instrument:["instrument-a","regular"]:indicator:barRadarVisible': "true",
        'aef:instrument:["instrument-a"]:indicatorMode': "regular",
        'aef:instrument:["instrument-a"]:paperRiskPrefs': (
            '{"orderType":"limit","qty":1,"useStopLoss":true,'
            '"stopPoints":8,"useTarget":true,"targetPoints":16}'
        ),
        'aef:instrument:["instrument-a","gex"]:indicator:gexContextEnabled': "true",
    }
    assert "runtime_settings" not in payload
    for presentation_field in (
        "opacity",
        "deletable",
        "delete_icon",
        "label_handle",
        "interactive",
    ):
        assert presentation_field not in payload["alerts"][0]
        assert presentation_field not in payload["drawings"][0]


def test_client_storage_payload_reads_only_the_exact_alert_scope() -> None:
    route = route_instrument(ibkr_stock_payload("ES"))
    other_route = route_instrument(coinbase_btc_payload())
    rows = [
        _canonical_price_alert(route, "scope-alert"),
        _canonical_price_alert(other_route, "other-alert"),
    ]
    calls = []

    class Store:
        def initialize(self):
            return None

        def read_price_alerts(self, instrument_id, interval, *, route_fingerprint):
            calls.append((instrument_id, route_fingerprint, interval))
            return [
                row
                for row in rows
                if row["instrument_id"] == instrument_id
                and row["route_fingerprint"] == route_fingerprint
                and row["timeframe"] == interval
            ]

        def read_all_price_alerts(self):
            raise AssertionError("client storage must not read the global alert snapshot")

    payload = client_storage_payload(
        _deps(store_factory=Store),
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        interval="5m",
        settings=False,
        drawings=False,
        alerts=True,
    )

    assert payload["ok"] is True
    assert [alert["id"] for alert in payload["alerts"]] == ["scope-alert"]
    assert calls == [(route.instrument_id, route.fingerprint, "5m")]


def test_client_storage_payload_rejects_malformed_runtime_before_emit() -> None:
    route = route_instrument(ibkr_stock_payload("ES"))

    class Store:
        def initialize(self):
            return None

        def read_price_alerts(self, *_args, **_kwargs):
            return [_canonical_price_alert(route, "malformed-alert", armed=1)]

    with pytest.raises(ValueError, match="PRICE_ALERT_FIELD_INVALID.*armed"):
        client_storage_payload(
            _deps(store_factory=Store),
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            interval="5m",
            settings=False,
            drawings=False,
            alerts=True,
        )


def test_save_client_settings_payload_accepts_current_workspace_keys() -> None:
    saved = {}

    class Store:
        def initialize(self):
            return None

        def upsert_settings(
            self,
            scope,
            values,
            *,
            mutation_writer_id="",
            mutation_orders=None,
        ):
            assert mutation_writer_id == _SETTINGS_WRITER_ID
            assert mutation_orders == {
                key: (_SETTINGS_CHANGED_AT_MS + index, index + 1)
                for index, key in enumerate(values)
            }
            saved.update(values)
            return (
                {
                    key: {
                        "outcome": "applied",
                        "value": value,
                        "changed_at_ms": mutation_orders[key][0],
                        "writer_id": mutation_writer_id,
                        "sequence": mutation_orders[key][1],
                    }
                    for key, value in values.items()
                },
                _SETTINGS_REVISION,
            )

        def lookup_instrument(self, *_args, **_kwargs):
            raise AssertionError("API settings preflight must not read watchlist membership")

    payload = save_client_settings_payload(
        _deps(store_factory=lambda: Store()),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": key,
                    "value": value,
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS + index,
                    "sequence": index + 1,
                }
                for index, (key, value) in enumerate(
                    {
                        "aef:ibkrPort": "4003",
                        "aef:workspace:1:instrumentId": "instrument-a",
                        "aef:workspace:1:timeframe": "15m",
                        'aef:workspace:1:barsVisible:["instrument-a","15m"]': "120",
                    }.items()
                )
            ],
        },
    )

    assert payload["ok"] is True
    assert payload["count"] == 4
    assert payload["settings_revision"] == _SETTINGS_REVISION
    assert [item["key"] for item in payload["mutations"]] == list(saved)
    assert all(item["outcome"] == "applied" for item in payload["mutations"])
    assert saved == {
        "aef:ibkrPort": "4003",
        "aef:workspace:1:instrumentId": "instrument-a",
        "aef:workspace:1:timeframe": "15m",
        'aef:workspace:1:barsVisible:["instrument-a","15m"]': "120",
    }


def test_save_client_settings_payload_returns_authoritative_rejection() -> None:
    authoritative_writer_id = "223e4567-e89b-42d3-a456-426614174000"
    runtime_calls = []
    mutation_calls = []

    class Store:
        def initialize(self):
            return None

        def upsert_settings(
            self,
            scope,
            values,
            *,
            mutation_writer_id="",
            mutation_orders=None,
        ):
            assert scope == "client"
            assert values == {"aef:motionPreference": "system"}
            return (
                {
                    "aef:motionPreference": {
                        "outcome": "rejected",
                        "value": "reduced",
                        "changed_at_ms": _SETTINGS_CHANGED_AT_MS + 1,
                        "writer_id": authoritative_writer_id,
                        "sequence": 1,
                    }
                },
                _SETTINGS_REVISION,
            )

    payload = save_client_settings_payload(
        _deps(
            store_factory=lambda: Store(),
            apply_ibkr_runtime_settings=lambda _settings=None: (
                runtime_calls.append("applied") or {}
            ),
            publish_client_settings_mutations=lambda mutations, settings_revision: (
                mutation_calls.append((mutations, settings_revision))
            ),
        ),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": "aef:motionPreference",
                    "value": "system",
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS,
                    "sequence": 1,
                }
            ],
        },
    )

    assert payload == {
        "ok": True,
        "count": 0,
        "settings_revision": _SETTINGS_REVISION,
        "mutations": [
            {
                "key": "aef:motionPreference",
                "outcome": "rejected",
                "value": "reduced",
                "changed_at_ms": _SETTINGS_CHANGED_AT_MS + 1,
                "writer_id": authoritative_writer_id,
                "sequence": 1,
            }
        ],
    }
    assert runtime_calls == ["applied"]
    assert mutation_calls == [
        (
            {
                "aef:motionPreference": {
                    "outcome": "rejected",
                    "value": "reduced",
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS + 1,
                    "writer_id": authoritative_writer_id,
                    "sequence": 1,
                }
            },
            _SETTINGS_REVISION,
        )
    ]


def test_save_client_settings_payload_accepts_exact_replay_without_new_count() -> None:
    class Store:
        def initialize(self):
            return None

        def upsert_settings(
            self,
            scope,
            values,
            *,
            mutation_writer_id="",
            mutation_orders=None,
        ):
            key, value = next(iter(values.items()))
            changed_at_ms, sequence = mutation_orders[key]
            return (
                {
                    key: {
                        "outcome": "replayed",
                        "value": value,
                        "changed_at_ms": changed_at_ms,
                        "writer_id": mutation_writer_id,
                        "sequence": sequence,
                    }
                },
                _SETTINGS_REVISION,
            )

    payload = save_client_settings_payload(
        _deps(store_factory=Store),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": "aef:motionPreference",
                    "value": "system",
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS,
                    "sequence": 1,
                }
            ],
        },
    )

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["settings_revision"] == _SETTINGS_REVISION
    assert payload["mutations"][0]["outcome"] == "replayed"


@pytest.mark.parametrize(
    "key",
    [
        "aef:ibkrMarketDataType",
        "aef:lastWorkspaceSlot",
        "aef:showHealthIndicator",
        "aef:watchlist",
        "aef:instrument",
        "aef:anyRetiredScalar",
        "aef:sharedStream:x",
        "aef:serverSleep",
        "aef:tickLive",
        "pane:backup",
        "aef:SPY:indicatorMode",
        "aef::indicator:regular:absorptionTrapAttackWindow",
        "aef::indicator:regular:absorptionTrapLevelLookback",
        "aef::indicator:regular:absorptionTrapMinScore",
        "aef:SPY:indicator:gex:gexContextEnabled",
        "aef:SPY:indicator:regular:gexDynamicsVisible",
        "aef:workspace:1:analysisScreenshotMode",
        "aef:workspace:1:interval",
        "aef:workspace:1:symbol",
        "aef:workspace:1:watchlistDisplayModes",
        "aef:workspace:1:range:SPY:5m",
        "aef:workspace:1:barsVisible:instrument-a:5m",
        'aef:workspace:1:range:["instrument-a", "5m"]',
        "aef:workspace:5:timeframe",
        'aef:instrument:["instrument-a", "regular"]:indicator:barRadarVisible',
        'aef:instrument:["instrument-a","advisor"]:indicator:barRadarVisible',
        'aef:instrument:["instrument-a","regular"]:indicator:',
        'aef:instrument:["instrument-a","regular"]:indicator:gexDynamicsCalcEnabled',
        'aef:instrument:["instrument-a","regular"]:indicatorMode',
        "aef:instrument:[]:indicatorMode",
        "aef:indicator:regular:barRadarVisible",
        "aef:indicator:global:unknownControl",
        "aef:SPY:indicator:regular:unknownControl",
        'aef:instrument:["instrument-a","regular"]:indicator:unknownControl',
        "aef:SPY:indicator:advisor:barRadarVisible",
        "aef:SPY:indicator:regular:",
    ],
)
def test_save_client_settings_payload_rejects_retired_or_invalid_keys_atomically(key: str) -> None:
    writes = []

    class Store:
        def initialize(self):
            return None

        def upsert_settings(self, scope, values, **_mutation_order):
            writes.append((scope, values))
            return set(values)

    payload = save_client_settings_payload(
        _deps(store_factory=lambda: Store()),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": setting_key,
                    "value": value,
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS + index,
                    "sequence": index + 1,
                }
                for index, (setting_key, value) in enumerate(
                    {"aef:motionPreference": "system", key: "legacy"}.items()
                )
            ],
        },
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "SETTINGS_KEY_FORBIDDEN"
    assert payload["keys"] == [key]
    assert payload["count"] == 0
    assert writes == []
    assert settings_contract.is_browser_writable_client_setting(key) is False


def test_browser_cannot_republish_symbol_scoped_indicator_order() -> None:
    writes = []
    order_key = "aef:SPY:indicator:regular:indicatorSettingsOrder"
    manager_ids = sorted(settings_contract._indicator_value_contract()["manager_ids"])

    class Store:
        def initialize(self):
            return None

        def upsert_settings(self, scope, values, **_mutation_order):
            writes.append((scope, values))
            return set(values)

    payload = save_client_settings_payload(
        _deps(store_factory=lambda: Store()),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": order_key,
                    "value": ",".join([*manager_ids, "gex_dynamics"]),
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS,
                    "sequence": 1,
                }
            ],
        },
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "SETTINGS_KEY_FORBIDDEN"
    assert payload["keys"] == [order_key]
    assert writes == []


@pytest.mark.parametrize("instrument_id", ["SPY", "missing-instrument"])
def test_save_client_settings_payload_delegates_instrument_membership_to_atomic_writer(
    instrument_id: str,
) -> None:
    writes = []

    class Store:
        def initialize(self):
            return None

        def lookup_instrument(self, *_args, **_kwargs):
            raise AssertionError("API settings preflight must not read watchlist membership")

        def upsert_settings(self, scope, values, **_mutation_order):
            writes.append((scope, values))
            raise ValueError(
                "STORAGE_CLIENT_SETTINGS_INSTRUMENT_INVALID keys=['aef:workspace:1:instrumentId']"
            )

    payload = save_client_settings_payload(
        _deps(store_factory=lambda: Store()),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": setting_key,
                    "value": value,
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS + index,
                    "sequence": index + 1,
                }
                for index, (setting_key, value) in enumerate(
                    {
                        "aef:motionPreference": "system",
                        "aef:workspace:1:instrumentId": instrument_id,
                    }.items()
                )
            ],
        },
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "SETTINGS_VALUE_INVALID"
    assert "keys" not in payload
    assert writes == [
        (
            "client",
            {
                "aef:motionPreference": "system",
                "aef:workspace:1:instrumentId": instrument_id,
            },
        )
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("aef:ibkrPort", "04003"),
        ("aef:motionPreference", "SYSTEM"),
    ),
)
def test_save_client_settings_payload_rejects_malformed_values_before_write(
    key: str,
    value: object,
) -> None:
    writes = []
    runtime_calls = []

    class Store:
        def initialize(self):
            return None

        def upsert_settings(self, scope, values, **_mutation_order):
            writes.append((scope, values))
            raise AssertionError("invalid values must fail before storage write")

    payload = save_client_settings_payload(
        _deps(
            store_factory=Store,
            apply_ibkr_runtime_settings=lambda _settings=None: (
                runtime_calls.append("applied") or {}
            ),
        ),
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [
                {
                    "key": key,
                    "value": value,
                    "changed_at_ms": _SETTINGS_CHANGED_AT_MS,
                    "sequence": 1,
                }
            ],
        },
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "SETTINGS_VALUE_INVALID"
    assert payload["error"]["retryable"] is False
    assert payload["keys"] == [key]
    assert payload["count"] == 0
    assert writes == []
    assert runtime_calls == []


@pytest.mark.parametrize(
    "payload",
    (
        {"key": "aef:motionPreference", "value": "system"},
        {"values": {"aef:motionPreference": "system"}},
        {
            "writer_id": _SETTINGS_WRITER_ID,
            "mutations": [],
            "legacy": True,
        },
    ),
)
def test_save_client_settings_rejects_noncurrent_payload_shapes(payload: dict) -> None:
    result = save_client_settings_payload(_deps(), payload)

    assert result["ok"] is False
    assert result["error"]["code"] == "SETTINGS_PAYLOAD_INVALID"


@pytest.mark.parametrize(
    ("writer_id", "changed_at_ms", "sequence"),
    (
        ("not-a-uuid", _SETTINGS_CHANGED_AT_MS, 1),
        ("123e4567-e89b-12d3-a456-426614174000", _SETTINGS_CHANGED_AT_MS, 1),
        (_SETTINGS_WRITER_ID, 0, 1),
        (_SETTINGS_WRITER_ID, _SETTINGS_CHANGED_AT_MS, True),
        (_SETTINGS_WRITER_ID, 9_007_199_254_740_992, 1),
    ),
)
def test_save_client_settings_rejects_invalid_mutation_order(
    writer_id: str,
    changed_at_ms: object,
    sequence: object,
) -> None:
    result = save_client_settings_payload(
        _deps(),
        {
            "writer_id": writer_id,
            "mutations": [
                {
                    "key": "aef:motionPreference",
                    "value": "system",
                    "changed_at_ms": changed_at_ms,
                    "sequence": sequence,
                }
            ],
        },
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "SETTINGS_MUTATION_ORDER_INVALID"


def test_workspace_selection_contract_allows_empty_and_stale_reads() -> None:
    values = {"aef:workspace:1:instrumentId": "retired-exact-id"}

    assert settings_contract.invalid_workspace_instrument_selection_keys(values) == []
    assert (
        settings_contract.invalid_workspace_instrument_selection_keys(
            {"aef:workspace:1:instrumentId": ""},
            instrument_exists=lambda _instrument_id: False,
        )
        == []
    )
    assert settings_contract.invalid_workspace_instrument_selection_keys(
        values,
        instrument_exists=lambda _instrument_id: False,
    ) == ["aef:workspace:1:instrumentId"]


def test_settings_repo_enforces_client_contract_for_every_writer_and_reader() -> None:
    repo = _test_settings_repo()
    repo._settings_cache = {}
    repo._settings_cache_ttl = 30.0
    repo._connect = lambda: (_ for _ in ()).throw(
        AssertionError("invalid setting must fail before SQL")
    )

    with pytest.raises(ValueError, match="STORAGE_CLIENT_SETTINGS_FORBIDDEN"):
        repo.upsert_setting("client", "aef:retiredSetting", "legacy")
    with pytest.raises(ValueError, match="STORAGE_CLIENT_SETTINGS_FORBIDDEN"):
        repo.upsert_settings(
            "client",
            {
                "aef:motionPreference": "system",
                "aef:retiredSetting": "legacy",
            },
        )

    class MembershipCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params=()):
            self.statement = str(statement)

        def fetchall(self):
            return []

        def fetchone(self):
            if "SELECT revision FROM settings_state" in self.statement:
                return (_SETTINGS_REVISION,)
            return None

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class MembershipConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return MembershipCursor()

        def transaction(self):
            return Transaction()

    repo._psycopg = lambda: None
    repo._connect = lambda: MembershipConnection()
    with pytest.raises(
        ValueError,
        match="STORAGE_CLIENT_SETTINGS_INSTRUMENT_INVALID",
    ):
        repo.upsert_settings(
            "client",
            {"aef:workspace:1:instrumentId": "retired-exact-id"},
        )

    class Cursor:
        statement = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params=()):
            self.statement = str(statement)

        def fetchall(self):
            if "FROM settings" in self.statement and "mutation_changed_at_ms" in self.statement:
                return [("aef:retiredSetting", "legacy", 0, None, 0)]
            return []

        def fetchone(self):
            if "SELECT revision FROM settings_state" in self.statement:
                return (_SETTINGS_REVISION,)
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo._connect = lambda: Connection()
    with pytest.raises(ValueError, match="STORAGE_CLIENT_SETTINGS_FORBIDDEN"):
        repo.read_settings("client")
    assert repo._settings_cache == {}


def test_settings_repo_bulk_validates_current_ids_and_publishes_cache_under_lock() -> None:
    repo = _test_settings_repo()
    repo._settings_cache = {}
    repo._settings_cache_ttl = 30.0
    executions: list[str] = []

    class Cursor:
        statement = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params=()):
            self.statement = str(statement)
            executions.append(self.statement)

        def fetchall(self):
            if "FROM settings" in self.statement and "mutation_changed_at_ms" in self.statement:
                return [("aef:workspace:1:instrumentId", "instrument-a", 0, None, 0)]
            if "SELECT instrument_id" in self.statement:
                return [("instrument-a",)]
            return []

        def fetchone(self):
            if "SELECT revision FROM settings_state" in self.statement:
                return (_SETTINGS_REVISION,)
            return None

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            assert "client" in repo._settings_cache
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo._connect = lambda: Connection()

    assert repo.read_settings("client") == {"aef:workspace:1:instrumentId": "instrument-a"}
    assert sum("SELECT instrument_id" in statement for statement in executions) == 1
    assert executions[0].find("pg_advisory_xact_lock_shared") >= 0


def test_settings_writer_invalidates_cache_under_exclusive_lock_before_dml() -> None:
    for method in (
        SettingsRepoMixin.upsert_setting,
        SettingsRepoMixin.upsert_settings,
    ):
        source = inspect.getsource(method)
        assert source.index("pg_advisory_xact_lock") < source.index("_settings_cache.clear")
        assert source.index("_settings_cache.clear") < source.index("INSERT INTO settings")
    singular_source = inspect.getsource(SettingsRepoMixin.upsert_setting)
    assert "current_setting.value IS DISTINCT FROM EXCLUDED.value" in singular_source
    assert "if cur.fetchone() is not None" in singular_source
    assert "else read_settings_revision(cur)" in singular_source
    batch_source = inspect.getsource(SettingsRepoMixin.upsert_settings)
    assert "EXCLUDED.mutation_writer_id IS NULL" in batch_source
    assert "current_setting.value IS DISTINCT FROM EXCLUDED.value" in batch_source
    assert "current_setting.mutation_changed_at_ms <> 0" in batch_source


def test_same_value_server_setting_is_revision_neutral() -> None:
    executions: list[str] = []

    class Cursor:
        statement = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params=()):
            self.statement = str(statement)
            executions.append(self.statement)

        def fetchall(self):
            assert "SELECT instrument_id" in self.statement
            return []

        def fetchone(self):
            if "RETURNING 1" in self.statement:
                return None
            assert "SELECT revision FROM settings_state" in self.statement
            return (7,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return self

    repo = _test_settings_repo()
    repo._settings_cache = {}
    repo._settings_cache_ttl = 30.0
    repo._psycopg = lambda: None
    repo._connect = lambda: Connection()

    assert (
        repo.upsert_setting(
            "server",
            settings_contract.TELEGRAM_INTERACTIVE_SETTING_KEY,
            {"enabled": False},
        )
        == 7
    )

    assert any("RETURNING 1" in statement for statement in executions)
    assert all("UPDATE settings_state" not in statement for statement in executions)


def test_settings_repo_rejects_persisted_stale_instrument_references() -> None:
    repo = _test_settings_repo()
    repo._settings_cache = {}
    repo._settings_cache_ttl = 30.0

    class Cursor:
        statement = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, _params=()):
            self.statement = str(statement)

        def fetchall(self):
            if "FROM settings" in self.statement and "mutation_changed_at_ms" in self.statement:
                return [("aef:workspace:1:instrumentId", "retired-instrument", 0, None, 0)]
            return []

        def fetchone(self):
            if "SELECT revision FROM settings_state" in self.statement:
                return (_SETTINGS_REVISION,)
            return None

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

        def transaction(self):
            return Transaction()

    repo._connect = lambda: Connection()

    with pytest.raises(
        ValueError,
        match="STORAGE_CLIENT_SETTINGS_INSTRUMENT_INVALID",
    ):
        repo.read_settings("client")
    assert repo._settings_cache == {}


def test_settings_repo_forbids_singular_client_read_even_with_cached_value() -> None:
    repo = _test_settings_repo()
    repo._settings_cache = {
        "client": (
            float("inf"),
            {"aef:motionPreference": "system"},
            {"aef:motionPreference": {"changed_at_ms": 0, "writer_id": None, "sequence": 0}},
            0,
            _SETTINGS_REVISION,
        )
    }
    repo._settings_cache_ttl = 30.0
    repo._connect = lambda: (_ for _ in ()).throw(
        AssertionError("singular client read must fail before cache or SQL")
    )

    with pytest.raises(ValueError, match="CLIENT_SETTINGS_BULK_READ_REQUIRED"):
        repo.read_setting("client", "aef:motionPreference")


def test_settings_repo_rejects_unknown_scopes_keys_and_server_shapes_before_sql() -> None:
    repo = _test_settings_repo()
    repo._settings_cache = {}
    repo._settings_cache_ttl = 30.0
    repo._connect = lambda: (_ for _ in ()).throw(
        AssertionError("invalid settings contract must fail before SQL")
    )

    with pytest.raises(ValueError, match="STORAGE_SETTINGS_SCOPE_INVALID"):
        repo.upsert_setting("legacy", "aef:any", "value")
    with pytest.raises(ValueError, match="STORAGE_SERVER_SETTINGS_FORBIDDEN"):
        repo.upsert_setting("server", "aef:legacyServerSetting", {})
    with pytest.raises(ValueError, match="STORAGE_SERVER_SETTINGS_INVALID"):
        repo.upsert_setting(
            "server",
            settings_contract.GEX_SCHEDULER_SETTING_KEY,
            {"enabled": True, "instrument_ids": []},
        )
    with pytest.raises(ValueError, match="STORAGE_CLIENT_SETTINGS_INVALID"):
        repo.upsert_setting(
            "client",
            settings_contract.TICK_LIVE_SETTING_KEY,
            {"enabled": False},
        )


def test_current_server_setting_payloads_are_closed_and_identity_qualified() -> None:
    instrument_id = "ibkr|contract|756733"
    route_fingerprint = instrument_id
    dividend_key = (
        settings_contract.GEX_DIVIDEND_YIELD_SETTING_PREFIX
        + f'["{instrument_id}","{route_fingerprint}"]'
    )
    values = {
        settings_contract.GEX_SCHEDULER_SETTING_KEY: {
            "enabled": True,
            "instrument_ids": [instrument_id],
        },
        settings_contract.OPTION_TARGET_CAPS_SETTING_KEY: {instrument_id: 2.5},
        settings_contract.TELEGRAM_INTERACTIVE_SETTING_KEY: {"enabled": False},
        dividend_key: {
            "provider_symbol": "SPY",
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "value": 0.0125,
            "source": "broker_pv_dividend",
            "updated_at": "2026-07-22T00:00:00+00:00",
            "sample_count": 3,
        },
    }

    assert settings_contract.invalid_persisted_server_setting_keys(values) == []
    assert (
        settings_contract.invalid_server_setting_value_keys(
            values,
            instrument_exists=lambda candidate: candidate == instrument_id,
        )
        == []
    )

    invalid = {
        **values,
        dividend_key: {
            **values[dividend_key],
            "instrument_id": "ibkr|contract|other",
        },
    }
    assert settings_contract.invalid_server_setting_value_keys(invalid) == [dividend_key]


def test_generic_settings_repo_cannot_mutate_watchlist_presentation() -> None:
    repo = _test_settings_repo()
    repo._connect = lambda: (_ for _ in ()).throw(
        AssertionError("presentation mutation must fail before SQL")
    )

    with pytest.raises(ValueError, match="WATCHLIST_PRESENTATION_OWNER_REQUIRED"):
        repo.upsert_setting(
            WATCHLIST_PRESENTATION_SCOPE,
            "ibkr|contract|756733",
            {"display_mode": "trend", "revision": 1},
        )
    with pytest.raises(ValueError, match="WATCHLIST_PRESENTATION_OWNER_REQUIRED"):
        repo.upsert_settings(
            WATCHLIST_PRESENTATION_SCOPE,
            {
                "ibkr|contract|756733": {
                    "display_mode": "trend",
                    "revision": 1,
                }
            },
        )
    assert "def delete_settings_by_prefix(" not in inspect.getsource(SettingsRepoMixin)


def test_client_setting_contract_keeps_only_exact_instrument_scoped_indicator_keys() -> None:
    current_keys = [
        "aef:motionPreference",
        "aef:paperEntryLabelFrameOffset",
        "aef:paperShowTradesOnChart",
        "aef:tradeSetupRuntimeLayout",
        "aef:indicator:global:ema20Enabled",
        'aef:instrument:["ibkr|contract|756733","regular"]:indicator:barRadarVisible',
        'aef:instrument:["ibkr|contract|756733"]:indicatorMode',
        'aef:instrument:["ibkr|contract|756733"]:paperRiskPrefs',
        "aef:workspace:1:gexFocusMode",
        "aef:workspace:1:instrumentId",
        "aef:workspace:1:sideTab",
        "aef:workspace:1:theme",
        "aef:workspace:1:timeframe",
        "aef:workspace:1:viewPreset",
        'aef:workspace:1:barsVisible:["ibkr|contract|756733","5m"]',
        'aef:workspace:1:range:["ibkr|contract|756733","5m"]',
    ]
    assert all(settings_contract.is_browser_writable_client_setting(key) for key in current_keys)
    assert (
        settings_contract.is_browser_writable_client_setting(
            'aef:instrument:["ibkr|contract|756733","gex"]:indicator:gexContextEnabled'
        )
        is True
    )
    assert (
        settings_contract.is_browser_writable_client_setting(
            'aef:instrument:["ibkr|contract|756733","regular"]:indicator:gexDynamicsVisible'
        )
        is True
    )
    assert (
        settings_contract.is_current_client_setting(
            'aef:instrument:["ibkr|contract|756733","regular"]:indicator:gexDynamicsCalcEnabled'
        )
        is False
    )
    assert (
        settings_contract.is_browser_writable_client_setting(
            'aef:instrument:["ibkr|contract|756733","regular"]:indicator:gexDynamicsCalcEnabled'
        )
        is False
    )
    assert settings_contract.is_current_client_setting("aef:serverSleep") is True
    assert settings_contract.is_current_client_setting("aef:tickLive") is True
    assert settings_contract.is_browser_writable_client_setting("aef:serverSleep") is False
    assert settings_contract.is_browser_writable_client_setting("aef:tickLive") is False

    current_ui_preferences = {
        "aef:paperEntryLabelFrameOffset": "520",
        "aef:paperShowTradesOnChart": "true",
        "aef:tradeSetupRuntimeLayout": "compact",
    }
    assert settings_contract.invalid_client_setting_value_keys(current_ui_preferences) == []
    for key, invalid_value in (
        ("aef:paperEntryLabelFrameOffset", "520.0"),
        ("aef:paperEntryLabelFrameOffset", "1601"),
        ("aef:paperShowTradesOnChart", "1"),
        ("aef:tradeSetupRuntimeLayout", "dense"),
    ):
        assert settings_contract.invalid_client_setting_value_keys({key: invalid_value}) == [key]

    from aef_terminal.ui.runtime import constants as runtime_constants

    runtime_payload = runtime_constants.load_runtime_constants()
    assert not any(key.endswith("_SETTING_KEY") for key in runtime_payload)


@pytest.mark.parametrize(
    "caps",
    [
        {"instrument-a": "2.5"},
        {"instrument-a": float("nan")},
        {"instrument-a": 0.009},
        {"instrument-a": 1000.0},
        {1: 2.5},
    ],
)
def test_option_target_caps_reject_coercion_filtering_and_clamping(caps: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        settings_contract.require_option_target_caps(caps)

    payload = gex_actions.save_option_settings(object(), {"premium_caps": caps})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "OPTION_PREMIUM_CAPS_INVALID"


def test_option_target_caps_preserve_exact_typed_ids() -> None:
    caps = settings_contract.require_option_target_caps({" opaque instrument id ": 2})
    assert caps == {" opaque instrument id ": 2}
    assert isinstance(caps[" opaque instrument id "], int)
    assert "sanitize_option_target_caps" not in inspect.getsource(gex_actions)


def test_option_target_caps_runtime_fails_closed_without_configured_storage() -> None:
    runtime = option_target_runtime.OptionTargetRuntime()
    with pytest.raises(RuntimeError, match="OPTION_TARGET_RUNTIME_DEPS_REQUIRED"):
        runtime.caps_settings()

    runtime.configure(option_target_runtime.OptionTargetRuntimeDeps(store_factory=lambda: None))
    with pytest.raises(RuntimeError, match="OPTION_TARGET_SETTINGS_STORAGE_REQUIRED"):
        runtime.caps_settings()


def test_option_target_caps_runtime_reads_once_and_owns_persist_then_publish() -> None:
    class Store:
        def __init__(self) -> None:
            self.reads = 0
            self.writes: list[tuple[str, str, dict[str, float]]] = []

        def read_setting_snapshot(self, scope: str, key: str):
            self.reads += 1
            assert (scope, key) == ("server", settings_contract.OPTION_TARGET_CAPS_SETTING_KEY)
            return {"instrument-a": 2.5}, True, 3

        def upsert_setting(
            self,
            scope: str,
            key: str,
            value: dict[str, float],
        ) -> int:
            self.writes.append((scope, key, dict(value)))
            return 4

    store = Store()
    runtime = option_target_runtime.OptionTargetRuntime()
    runtime.configure(option_target_runtime.OptionTargetRuntimeDeps(store_factory=lambda: store))

    first = runtime.caps_settings()
    first["instrument-a"] = 99.0
    assert runtime.caps_settings() == {"instrument-a": 2.5}
    assert store.reads == 1

    assert runtime.save_caps_settings({"instrument-a": 3.5}) == {"instrument-a": 3.5}
    assert runtime.caps_settings() == {"instrument-a": 3.5}
    assert store.reads == 1
    assert store.writes == [
        (
            "server",
            settings_contract.OPTION_TARGET_CAPS_SETTING_KEY,
            {"instrument-a": 3.5},
        )
    ]


def test_option_target_caps_reconciliation_rejects_older_settings_revision() -> None:
    class Store:
        snapshot = ({"instrument-a": 2.5}, True, 6)

        def read_setting_snapshot(self, scope: str, key: str):
            assert (scope, key) == ("server", settings_contract.OPTION_TARGET_CAPS_SETTING_KEY)
            return self.snapshot

    store = Store()
    runtime = option_target_runtime.OptionTargetRuntime()
    runtime.configure(option_target_runtime.OptionTargetRuntimeDeps(store_factory=lambda: store))

    assert runtime.caps_settings() == {"instrument-a": 2.5}
    store.snapshot = ({"instrument-b": 3.0}, True, 5)
    assert runtime.publish_committed_snapshot(*store.snapshot) == {"instrument-a": 2.5}
    store.snapshot = ({"instrument-b": 3.0}, True, 7)
    assert runtime.publish_committed_snapshot(*store.snapshot) == {"instrument-b": 3.0}


def test_watchlist_removal_setting_plan_uses_only_exact_instrument_identity() -> None:
    instrument_id = "ibkr|contract|756733"
    other_id = "ibkr|contract|265598"
    client_values = {
        "aef:workspace:1:instrumentId": instrument_id,
        f'aef:workspace:1:range:["{instrument_id}","5m"]': "5d",
        f'aef:instrument:["{instrument_id}"]:indicatorMode': "regular",
        settings_contract.TICK_LIVE_SETTING_KEY: {
            "enabled": True,
            "instrument_id": instrument_id,
            "route_fingerprint": instrument_id,
            "source": "api",
            "updated_at": "2026-07-22T00:00:00+00:00",
        },
        "aef:workspace:2:instrumentId": other_id,
    }

    assert settings_contract.client_setting_keys_for_instrument(
        client_values,
        instrument_id,
    ) == sorted(list(client_values)[:4])

    dividend_key = (
        settings_contract.GEX_DIVIDEND_YIELD_SETTING_PREFIX
        + f'["{instrument_id}","{instrument_id}"]'
    )
    updates, deletes = settings_contract.server_setting_mutations_without_instrument(
        {
            settings_contract.GEX_SCHEDULER_SETTING_KEY: {
                "enabled": True,
                "instrument_ids": [instrument_id, other_id],
            },
            settings_contract.OPTION_TARGET_CAPS_SETTING_KEY: {
                instrument_id: 2.5,
                other_id: 3.0,
            },
            dividend_key: {},
        },
        instrument_id,
    )

    assert updates == {
        settings_contract.GEX_SCHEDULER_SETTING_KEY: {
            "enabled": True,
            "instrument_ids": [other_id],
        },
        settings_contract.OPTION_TARGET_CAPS_SETTING_KEY: {other_id: 3.0},
    }
    assert deletes == [dividend_key]


def test_client_storage_payload_propagates_storage_contract_failure() -> None:
    class Store:
        def initialize(self):
            return None

        def read_settings_snapshot(self, scope):
            raise ValueError("STORAGE_CLIENT_SETTINGS_FORBIDDEN")

    with pytest.raises(ValueError, match="STORAGE_CLIENT_SETTINGS_FORBIDDEN"):
        client_storage_payload(
            _deps(store_factory=lambda: Store()),
            settings=True,
            drawings=False,
            alerts=False,
        )


def test_delete_option_target_payload_cancels_nested_live_quote_subscription(monkeypatch) -> None:
    instrument = ibkr_stock_payload("ES")
    route = route_instrument(instrument)
    cancels = []

    class Store:
        def initialize(self):
            return None

        def read_option_targets(self, *, route_pairs):
            assert route_pairs == [(route.instrument_id, route.fingerprint)]
            return [
                {
                    "id": "opt-1",
                    "payload": {
                        "symbol": "ES",
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                        "payload": {
                            "intent": {
                                "provider_symbol": "ES",
                                "mode": "normal",
                                "right": "P",
                                "target_dte": "0dte",
                                "sec_type": "OPT",
                                "con_id": 123,
                                "local_symbol": "ES P5000",
                                "exchange": "SMART",
                                "expiry": "20260722",
                                "strike": 5000.0,
                            },
                            "market_sample": {},
                        },
                    },
                }
            ]

        def delete_option_target(self, instrument_id, route_fingerprint, target_id):
            return (
                instrument_id == route.instrument_id
                and route_fingerprint == route.fingerprint
                and target_id == "opt-1"
            )

    monkeypatch.setattr(
        route.adapter,
        "cancel_option_quote",
        lambda _instrument, item, *, consumer_id: (
            cancels.append((item, consumer_id)) or {"ok": True, "cancelled": True}
        ),
    )
    payload = delete_option_target_payload(
        _deps(store_factory=lambda: Store()),
        "opt-1",
        route.instrument_id,
        route.fingerprint,
    )

    assert payload["ok"] is True
    assert payload["quote_cancel"]["cancelled"] is True
    assert cancels == [
        (
            {"sec_type": "OPT", "con_id": 123, "exchange": "SMART"},
            "option-point:opt-1",
        )
    ]


def test_write_option_target_uses_server_identity_and_active_provider_contract(monkeypatch) -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    writes = []
    monkeypatch.setattr(
        option_target_actions,
        "lookup_runtime_instrument",
        lambda instrument_id: instrument if instrument_id == route.instrument_id else None,
    )
    monkeypatch.setattr(
        option_target_actions,
        "require_provider_chart_axis_point",
        lambda *_args: 271,
    )

    class Store:
        def initialize(self):
            return None

        def create_option_target_intent(
            self,
            instrument_id,
            display_key,
            timeframe,
            item,
            *,
            route_fingerprint,
            provider,
            provider_contract_id,
        ):
            writes.append(
                {
                    "instrument_id": instrument_id,
                    "display_key": display_key,
                    "timeframe": timeframe,
                    "item": dict(item),
                    "route_fingerprint": route_fingerprint,
                    "provider": provider,
                    "provider_contract_id": provider_contract_id,
                }
            )
            return OptionTargetCreateResult(item={"id": "opt-1"})

    result = create_option_target_payload(
        _deps(store_factory=Store),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "symbol": "CLIENT-SPOOFED-DISPLAY",
            "timeframe": "5m",
            "point": {
                "price": 5000.0,
                "ts": "2026-07-22T12:00:00+00:00",
            },
            "payload": {
                "intent": {
                    "provider_symbol": "ES",
                    "mode": "normal",
                    "right": "P",
                    "target_dte": "0dte",
                    "sec_type": "FOP",
                    "con_id": 123,
                    "local_symbol": "ES P5000",
                    "exchange": "CME",
                    "expiry": "20260722",
                    "expiry_at": "2026-07-22T20:00:00+00:00",
                    "strike": 5000.0,
                    "trading_class": "ES",
                    "multiplier": 50.0,
                    "currency": "USD",
                }
            },
        },
    )

    assert result == {
        "ok": True,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "item": {"id": "opt-1"},
        "evicted_ids": [],
        "quote_cancels": [],
    }
    assert writes == [
        {
            "instrument_id": route.instrument_id,
            "display_key": "ES",
            "timeframe": "5m",
            "item": {
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "symbol": "CLIENT-SPOOFED-DISPLAY",
                "timeframe": "5m",
                "point": {
                    "price": 5000.0,
                    "ts": "2026-07-22T12:00:00+00:00",
                    "barSlot": 271,
                },
                "payload": {
                    "intent": {
                        "provider_symbol": "ES",
                        "mode": "normal",
                        "right": "P",
                        "target_dte": "0dte",
                        "sec_type": "FOP",
                        "con_id": 123,
                        "local_symbol": "ES P5000",
                        "exchange": "CME",
                        "expiry": "20260722",
                        "expiry_at": "2026-07-22T20:00:00+00:00",
                        "strike": 5000.0,
                        "trading_class": "ES",
                        "multiplier": 50.0,
                        "currency": "USD",
                    }
                },
            },
            "route_fingerprint": route.fingerprint,
            "provider": "ibkr",
            "provider_contract_id": "11004968",
        }
    ]


def test_create_option_target_cancels_the_exact_server_eviction(monkeypatch) -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    cancels = []
    monkeypatch.setattr(
        option_target_actions,
        "lookup_runtime_instrument",
        lambda instrument_id: instrument if instrument_id == route.instrument_id else None,
    )
    monkeypatch.setattr(
        option_target_actions,
        "require_provider_chart_axis_point",
        lambda *_args: 271,
    )
    monkeypatch.setattr(
        route.adapter,
        "cancel_option_quote",
        lambda _instrument, item, *, consumer_id: (
            cancels.append((item, consumer_id)) or {"ok": True, "cancelled": True}
        ),
    )

    evicted = {
        "id": "opt-oldest",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "payload": {
            "intent": {
                "sec_type": "FOP",
                "con_id": 456,
                "exchange": "CME",
            }
        },
    }

    class Store:
        def initialize(self):
            return None

        def create_option_target_intent(self, *_args, **_kwargs):
            return OptionTargetCreateResult(
                item={"id": "opt-newest"},
                evicted=(evicted,),
            )

    result = create_option_target_payload(
        _deps(store_factory=Store),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "symbol": "ES",
            "timeframe": "5m",
            "point": {
                "price": 5000.0,
                "ts": "2026-07-22T12:00:00+00:00",
            },
            "payload": {
                "intent": {
                    "provider_symbol": "ES",
                    "mode": "normal",
                    "right": "P",
                    "target_dte": "0dte",
                    "sec_type": "FOP",
                    "con_id": 123,
                    "local_symbol": "ES P5000",
                    "exchange": "CME",
                    "expiry": "20260722",
                    "expiry_at": "2026-07-22T20:00:00+00:00",
                    "strike": 5000.0,
                    "trading_class": "ES",
                    "multiplier": 50.0,
                    "currency": "USD",
                }
            },
        },
    )

    assert result == {
        "ok": True,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "item": {"id": "opt-newest"},
        "evicted_ids": ["opt-oldest"],
        "quote_cancels": [{"ok": True, "cancelled": True}],
    }
    assert cancels == [
        (
            {"sec_type": "FOP", "con_id": 456, "exchange": "CME"},
            "option-point:opt-oldest",
        )
    ]


def test_write_option_target_validates_provider_axis_before_persistence(
    monkeypatch,
) -> None:
    route = route_instrument(ibkr_stock_payload("ES"))
    writes = []
    validations = []

    class Store:
        def initialize(self):
            return None

        def update_option_target_intent(self, *args, **kwargs):
            writes.append((args, kwargs))
            return {"id": "opt-board"}

    def validate_axis(store, instrument, timeframe, timestamp):
        validations.append(
            (
                store.__class__.__name__,
                instrument,
                timeframe,
                timestamp.isoformat(),
            )
        )
        return 271

    monkeypatch.setattr(
        option_target_actions,
        "require_provider_chart_axis_point",
        validate_axis,
    )
    payload = {
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "ES",
        "timeframe": "5m",
        "point": {
            "price": 5000.0,
            "ts": "2026-07-22T12:00:00+00:00",
            "barSlot": 314,
        },
        "payload": {"intent": {}},
    }

    result = update_option_target_payload(
        _deps(store_factory=Store),
        "opt-board",
        payload,
    )

    assert result["ok"] is True
    assert validations == [
        (
            "Store",
            route.instrument,
            "5m",
            "2026-07-22T12:00:00+00:00",
        )
    ]
    assert len(writes) == 1
    assert writes[0][0][3]["point"]["barSlot"] == 271

    invalid = update_option_target_payload(
        _deps(store_factory=Store),
        "opt-board-invalid",
        {
            **payload,
            "point": {
                **payload["point"],
                "ts": "not-a-timestamp",
            },
        },
    )

    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "OPTION_TARGET_CHART_POINT_INVALID"
    assert len(writes) == 1


def test_save_drawings_payload_blocks_unexplained_mass_shrink() -> None:
    route = route_instrument(ibkr_stock_payload("ES"))

    class Store:
        def initialize(self):
            return None

        def read_drawings(self, instrument_id, interval, *, route_fingerprint):
            assert instrument_id == route.instrument_id
            assert route_fingerprint == route.fingerprint
            return [{"id": "a"}, {"id": "b"}, {"id": "c"}]

        def replace_drawings(self, *_args, **_kwargs):
            raise AssertionError("blocked shrink must not write drawings")

    payload = save_drawings_payload(
        _deps(store_factory=lambda: Store()),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "interval": "5m",
            "drawings": [{"id": "a"}],
            "allow_mass_delete": False,
            "deleted_ids": [],
        },
    )

    assert payload["ok"] is False
    assert "blocked unexplained drawing deletion" in payload["message"]
    assert payload["removed_ids"] == ["b", "c"]


def test_save_drawings_payload_reports_current_contract_violation() -> None:
    route = route_instrument(ibkr_stock_payload("ES"))
    writes = []

    class Store:
        def initialize(self):
            return None

        def read_drawings(self, *_args, **_kwargs):
            return []

        def replace_drawings(self, *_args, **_kwargs):
            writes.append(True)

    payload = save_drawings_payload(
        _deps(
            store_factory=Store,
            normalize_drawing_anchors=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                ValueError("DRAWING_FIELD_FORBIDDEN: offset")
            ),
        ),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "interval": "5m",
            "drawings": [{"id": "channel", "type": "channel", "offset": 1.0}],
            "allow_mass_delete": False,
            "deleted_ids": [],
        },
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "STORAGE_DRAWINGS_PAYLOAD_INVALID"
    assert payload["message"] == "DRAWING_FIELD_FORBIDDEN: offset"
    assert writes == []


def test_save_drawings_payload_persists_hydrated_provider_slots() -> None:
    route = route_instrument(coinbase_btc_payload())
    saved = {}

    class Store:
        def initialize(self):
            return None

        def read_drawings(self, instrument_id, interval, *, route_fingerprint):
            assert instrument_id == route.instrument_id
            assert route_fingerprint == route.fingerprint
            return [{"id": "channel"}]

        def replace_drawings(
            self,
            instrument_id,
            presentation_symbol,
            interval,
            drawings,
            **identity,
        ):
            saved.update(identity)
            saved["instrument_id"] = instrument_id
            saved["symbol"] = presentation_symbol
            saved["interval"] = interval
            saved["drawings"] = drawings

    def hydrate(_store, _symbol, _interval, drawings, **kwargs):
        saved["provider"] = kwargs.get("provider")
        return [{**item, "hydrated": True} for item in drawings]

    payload = save_drawings_payload(
        _deps(store_factory=lambda: Store(), normalize_drawing_anchors=hydrate),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "interval": "5m",
            "drawings": [{"id": "channel"}],
            "allow_mass_delete": False,
            "deleted_ids": [],
        },
    )

    assert payload["ok"] is True
    assert saved["provider"] == "coinbase"
    assert saved["instrument_id"] == route.instrument_id
    assert saved["route_fingerprint"] == route.fingerprint
    assert saved["drawings"] == [{"id": "channel", "hydrated": True}]


def test_save_drawings_payload_persists_ruler_as_a_line_variant() -> None:
    route = route_instrument(coinbase_btc_payload())
    saved: dict = {}
    hydrated_batches: list[list[dict]] = []
    ruler = {
        "id": "ruler-line",
        "type": "line",
        "lineVariant": "ruler",
        "points": [
            {
                "ts": "2026-07-01T01:10:00+00:00",
                "barSlot": 5_943_902,
                "price": 100.0,
            },
            {
                "ts": "2026-07-01T01:15:00+00:00",
                "barSlot": 5_943_907,
                "price": 110.0,
            },
        ],
        "extendRight": False,
    }

    class Store:
        def initialize(self):
            return None

        def read_drawings(self, *_args, **_kwargs):
            return []

        def replace_drawings(
            self,
            instrument_id,
            presentation_symbol,
            interval,
            drawings,
            **identity,
        ):
            saved["instrument_id"] = instrument_id
            saved["symbol"] = presentation_symbol
            saved["interval"] = interval
            saved["drawings"] = drawings
            saved.update(identity)

    def hydrate(_store, _instrument_id, _interval, drawings, **_identity):
        hydrated_batches.append(list(drawings))
        return drawings

    payload = save_drawings_payload(
        _deps(
            store_factory=Store,
            normalize_drawing_anchors=hydrate,
        ),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "interval": "5m",
            "drawings": [ruler],
            "allow_mass_delete": False,
            "deleted_ids": [],
        },
    )

    assert payload["ok"] is True
    assert hydrated_batches == [[], [ruler]]
    assert saved["drawings"] == [ruler]
    assert saved["drawings"][0]["type"] == "line"
    assert saved["drawings"][0]["lineVariant"] == "ruler"
    assert saved["drawings"][0]["extendRight"] is False


def test_drawing_save_derives_provider_from_qualified_route() -> None:
    route = route_instrument(coinbase_btc_payload())
    saved = {}

    class Store:
        def initialize(self):
            return None

        def read_drawings(self, instrument_id, interval, *, route_fingerprint):
            return []

        def replace_drawings(
            self,
            instrument_id,
            presentation_symbol,
            interval,
            drawings,
            **identity,
        ):
            saved.update(identity)
            saved["presentation_symbol"] = presentation_symbol
            saved["drawings"] = drawings

    save_payload = save_drawings_payload(
        _deps(
            store_factory=lambda: Store(),
            normalize_drawing_anchors=lambda _store, _instrument_id, _interval, drawings, **_identity: (
                drawings
            ),
        ),
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "interval": "5m",
            "drawings": [{"id": "channel", "symbol": "LEGACY-DISPLAY"}],
            "allow_mass_delete": False,
            "deleted_ids": [],
        },
    )

    assert save_payload["ok"] is True
    assert save_payload["instrument_id"] == route.instrument_id
    assert save_payload["route_fingerprint"] == route.fingerprint
    assert save_payload["provider"] == route.provider
    assert save_payload["interval"] == "5m"
    assert saved["provider"] == route.provider
    assert saved["presentation_symbol"] == route.instrument_key
    assert saved["drawings"] == [{"id": "channel", "symbol": "LEGACY-DISPLAY"}]


def test_drawing_scope_retains_presentation_symbol_only_as_row_metadata() -> None:
    drawing_source = inspect.getsource(DrawingsRepoMixin.replace_drawings)
    drawing_parameters = inspect.signature(DrawingsRepoMixin.replace_drawings).parameters

    assert "presentation_symbol" in drawing_parameters
    assert 'item.get("symbol", presentation_symbol)' in drawing_source
    assert '"symbol": item_presentation_symbol' in drawing_source
    assert 'stored_payload.get("symbol") != exact_stored_symbol' in drawing_source
    assert "exact_stored_symbol != presentation_symbol" not in drawing_source
