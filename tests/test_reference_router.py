from __future__ import annotations

import pytest

from aef_terminal.data.instrument_identity import (
    require_exact_identity_text,
    require_exact_instrument_id_sequence,
)
from aef_terminal.data import provider_sessions as sessions_module
from aef_terminal.watchlist_contract import (
    WatchlistOrderError,
    WatchlistRetirementSettingsSnapshot,
)
from aef_terminal.ui import reference_watchlist_item as watchlist_item_module
from aef_terminal.ui.routers.reference import ReferenceRouterDeps, create_reference_router
from aef_terminal.ui.reference_services import (
    materialize_watchlist_instrument,
    queue_watchlist_session_refresh,
    selected_instruments,
)
from tests.provider_payloads import coinbase_btc_payload, ibkr_future_payload, ibkr_stock_payload


@pytest.fixture(autouse=True)
def _isolate_provider_trading_hours_refresh_state():
    """Keep watchlist refresh requests from leaking into later test modules."""

    sessions_module._PENDING_TRADING_HOURS_REFRESH.clear()
    yield
    sessions_module._PENDING_TRADING_HOURS_REFRESH.clear()


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class WatchlistStore:
    def __init__(self, items=None, saved: dict | None = None):
        self.items = [dict(item) for item in list(items or [])]
        self.saved = saved
        self.version = 0
        self.presentations: dict[str, dict] = {}
        self.future_contracts = []
        self.current_futures_contracts: dict[tuple[str, str], dict] = {}
        self.provider_sessions: dict[tuple[str, str, str], dict] = {}
        self.selection_route_transitions = []

    def initialize(self):
        return None

    def _watchlist_rows(self):
        rows = []
        for item in self.items:
            row = dict(item)
            identity = dict(row.get("contract_identity") or {})
            route = (
                str(row.get("provider") or "").lower(),
                require_exact_identity_text(row.get("instrument_id"), field="instrument_id"),
            )
            current = self.current_futures_contracts.get(route)
            if current:
                identity["current_contract"] = dict(current)
                row["contract_identity"] = identity
            contract_id = require_exact_identity_text(
                (current or {}).get("provider_contract_id", row.get("provider_contract_id", "")),
                field="provider_contract_id",
                allow_empty=True,
            )
            provider_session = self.provider_sessions.get((*route, contract_id))
            if provider_session:
                row["session"] = {
                    **dict(row.get("session") or {}),
                    **dict(provider_session),
                }
            row["watchlist_version"] = self.version
            if route[1] in self.presentations:
                row["presentation"] = dict(self.presentations[route[1]])
            rows.append(row)
        return rows

    def _save(self):
        if self.saved is not None:
            self.saved["items"] = list(self.items)
            self.saved["version"] = self.version
            self.saved["future_contracts"] = list(self.future_contracts)

    def read_watchlist_snapshot(self):
        return self._watchlist_rows(), self.version

    def select_watchlist_instrument(
        self,
        item,
        *,
        current_futures_contract=None,
        route_transition=None,
    ):
        instrument_id = require_exact_identity_text(
            item.get("instrument_id"), field="instrument_id"
        )
        if current_futures_contract is not None:
            self._record_futures_contract(current_futures_contract)
        self.selection_route_transitions.append(route_transition)
        self.items = [row for row in self.items if row.get("instrument_id") != instrument_id]
        self.items.append(dict(item))
        self.version += 1
        self._save()
        return self.version

    def _record_futures_contract(self, contract):
        self.future_contracts.append(dict(contract))
        if contract.get("is_current"):
            provider = str(contract.get("provider") or "").lower()
            instrument_id = require_exact_identity_text(
                contract.get("instrument_id"), field="instrument_id"
            )
            provider_contract_id = require_exact_identity_text(
                (contract.get("metadata") or {}).get("provider_contract_id"),
                field="provider_contract_id",
            )
            self.current_futures_contracts[(provider, instrument_id)] = {
                **dict(contract),
                "provider_contract_id": provider_contract_id,
                "source": "provider_current_contract",
            }

    def write_futures_contract(self, contract, *, route_transition=None):
        self._record_futures_contract(contract)
        if self.saved is not None:
            self.saved["future_contracts"] = list(self.future_contracts)
        return require_exact_identity_text(contract.get("contract_key"), field="contract_key")

    def read_current_futures_contract(self, *, provider, instrument_id):
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        return self.current_futures_contracts.get((str(provider).lower(), identity))

    def lookup_instrument(self, instrument_id):
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        return next(
            (row for row in self._watchlist_rows() if row.get("instrument_id") == identity),
            None,
        )

    def remove_instrument_from_watchlist(self, instrument_id):
        key = require_exact_identity_text(instrument_id, field="instrument_id")
        self.items = [row for row in self.items if row.get("instrument_id") != key]
        self.version += 1
        self._save()
        return WatchlistRetirementSettingsSnapshot(
            settings_revision=1,
            client_settings={},
            client_mutation_orders={},
            gex_scheduler_setting=None,
            gex_scheduler_present=False,
            option_target_caps_setting=None,
            option_target_caps_present=False,
        )

    def set_watchlist_presentation(self, instrument_id, display_mode):
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        if not any(row.get("instrument_id") == identity for row in self.items):
            raise KeyError(identity)
        current = self.presentations.get(identity, {"display_mode": "classic", "revision": 0})
        presentation = {
            "display_mode": display_mode,
            "revision": int(current["revision"]) + 1,
        }
        self.presentations[identity] = presentation
        return dict(presentation)

    def reorder_watchlist(self, ordered_keys, expected_version=None):
        if expected_version is None:
            raise WatchlistOrderError("WATCHLIST_ORDER_VERSION_REQUIRED")
        if int(expected_version) != self.version:
            raise WatchlistOrderError(
                "WATCHLIST_ORDER_STALE",
                expected_version=int(expected_version),
                current_version=self.version,
            )
        by_key = {row["instrument_id"]: row for row in self.items}
        ordered = [
            by_key[key]
            for key in require_exact_instrument_id_sequence(ordered_keys, allow_empty=True)
        ]
        self.items = ordered
        self.version += 1
        self._save()
        return self.read_watchlist_snapshot()


def _unsupported_provider_instrument() -> dict:
    return {
        "instrument_id": "moex|contract|SBER",
        "key": "SBER",
        "instrument_key": "SBER",
        "display": "SBER",
        "name": "Sberbank",
        "provider": "moex",
        "provider_symbol": "SBER",
        "provider_contract_id": "SBER",
        "asset_class": "stock",
        "contract_identity": {
            "provider": "moex",
            "provider_contract_id": "SBER",
            "asset_class": "stock",
        },
    }


def test_reference_indicator_registry_exposes_manifest() -> None:
    endpoint = _route_endpoint(create_reference_router(_deps()), "/api/indicators/registry", "GET")
    payload = endpoint()
    assert "elliott_core" not in payload
    assert "breakout_accumulation" in payload
    assert "martin_carlo" in payload
    assert payload["martin_carlo"]["group"] == "post_decision"
    assert (
        payload["breakout_accumulation"]["snapshot_order"]
        < payload["martin_carlo"]["snapshot_order"]
    )

    connections = _route_endpoint(
        create_reference_router(_deps()), "/api/indicators/connections", "GET"
    )
    structure_payload = connections(group="structure")
    assert "smc_channels" in structure_payload
    assert "linda_volume" not in structure_payload
    assert structure_payload["smc_channels"]["enabled"] is False
    assert structure_payload["smc_channels"]["connected"] is False
    assert structure_payload["smc_channels"]["connection"]["registry_id"] == "smc_channels"
    setup_payload = connections(stage="setup")
    assert set(setup_payload) == {"obvious_failure", "trade_setup_engine"}
    assert setup_payload["obvious_failure"]["enabled"] is False
    assert setup_payload["trade_setup_engine"]["enabled"] is False
    assert setup_payload["trade_setup_engine"]["connected"] is False
    post_payload = connections(stage="post_decision")
    assert post_payload["option_reversal"]["enabled"] is False
    assert post_payload["option_reversal"]["connected"] is False
    dependencies = post_payload["martin_carlo"]["connection"]["dependencies"]
    assert [item["id"] for item in dependencies] == [
        "impulse_fib",
        "absorption_trap",
        "w5_structure",
        "wolfe_structure",
        "option_reversal",
    ]
    assert {item["id"]: item["registered"] for item in dependencies} == {
        "impulse_fib": True,
        "absorption_trap": True,
        "w5_structure": True,
        "wolfe_structure": True,
        "option_reversal": True,
    }
    assert all(item["enabled"] is False for item in dependencies)
    assert all(item["required"] is False for item in dependencies)
    assert all(item["optional_context"] is True for item in dependencies)
    assert all(item["linked"] is False for item in dependencies)


def test_reference_add_instrument_storage_unavailable() -> None:
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda _provider, _contract_id: ibkr_stock_payload(
                    "PLTR", con_id=12345
                ),
                store_factory=lambda: None,
            )
        ),
        "/api/instruments",
        "POST",
    )
    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
        }
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_STORAGE_NOT_CONFIGURED"


def test_reference_add_incompatible_store_is_not_reported_as_unconfigured() -> None:
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda _provider, _contract_id: ibkr_stock_payload(
                    "PLTR", con_id=12345
                ),
                store_factory=object,
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_INSTRUMENT_SAVE_ERROR"
    assert payload["error"]["code"] != "REFERENCE_STORAGE_NOT_CONFIGURED"


def test_reference_add_rejects_snapshot_older_than_selection_receipt() -> None:
    class StaleSnapshotStore(WatchlistStore):
        def select_watchlist_instrument(self, item, **kwargs):
            return super().select_watchlist_instrument(item, **kwargs) + 1

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda _provider, _contract_id: ibkr_stock_payload(
                    "PLTR", con_id=12345
                ),
                store_factory=StaleSnapshotStore,
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_INSTRUMENT_SAVE_ERROR"
    assert "WATCHLIST_SNAPSHOT_STALE_AFTER_SELECTION" in payload["error"]["message"]


def test_reference_add_requires_selected_instrument_id_before_provider_rebind() -> None:
    rebound: list[str] = []
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda _provider, contract_id: rebound.append(
                    str(contract_id)
                ),
                store_factory=lambda: WatchlistStore(),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_INSTRUMENT_ID_REQUIRED"
    assert rebound == []


def test_reference_add_rejects_provider_rebind_to_different_instrument_id() -> None:
    saved: dict = {}
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda _provider, _contract_id: ibkr_stock_payload(
                    "PLTR", con_id=12345
                ),
                store_factory=lambda: WatchlistStore(saved=saved),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|99999",
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_INSTRUMENT_ID_MISMATCH"
    assert payload["instrument_id"] == "ibkr|contract|99999"
    assert payload["rebound_instrument_id"] == "ibkr|contract|12345"
    assert saved == {}


def test_reference_watchlist_invalid_row_is_structured_error() -> None:
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(store_factory=lambda: WatchlistStore(items=[{"key": "BROKEN"}]))
        ),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_WATCHLIST_READ_ERROR"
    assert payload["error"]["message"] == "instrument_id must be an exact string"


def test_reference_search_instruments_filters_by_registered_provider() -> None:
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                search_provider_instruments=lambda provider, _query: [
                    ibkr_stock_payload("PLTR", name="Palantir Technologies", con_id=12345),
                    coinbase_btc_payload(),
                ],
                data_provider_catalog=lambda: [{"key": "ibkr"}, {"key": "coinbase"}],
            )
        ),
        "/api/instruments/search",
        "POST",
    )

    payload = endpoint({"provider": "ibkr", "query": "palantir"})

    assert payload["ok"] is True
    assert [item["instrument_key"] for item in payload["matches"]] == ["PLTR"]
    assert payload["matches"][0]["provider"] == "ibkr"
    assert payload["matches"][0]["provider_symbol"] == "PLTR"


def test_reference_search_instruments_includes_provider_lookup_candidates() -> None:
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                search_provider_instruments=lambda provider, query: [
                    ibkr_stock_payload("RSU", name="Research Solutions Inc", con_id=12345)
                ],
            )
        ),
        "/api/instruments/search",
        "POST",
    )

    payload = endpoint({"provider": "ibkr", "query": "RSU"})

    assert payload["ok"] is True
    assert [item["instrument_key"] for item in payload["matches"]] == ["RSU"]
    assert payload["matches"][0]["name"] == "Research Solutions Inc"


def test_reference_search_does_not_let_static_catalog_shadow_broker_contract() -> None:
    broker_contract = ibkr_stock_payload("SPY", name="SPDR S&P 500 ETF Trust", con_id=756733)
    broker_contract["contract_identity"].update(
        {"exchange": "SMART", "primary_exchange": "ARCA", "currency": "USD"}
    )
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                search_provider_instruments=lambda _provider, _query: [broker_contract],
            )
        ),
        "/api/instruments/search",
        "POST",
    )

    payload = endpoint({"provider": "ibkr", "query": "SPY"})

    assert payload["ok"] is True
    assert [item["instrument_key"] for item in payload["matches"]] == ["SPY"]
    assert payload["matches"][0]["provider_contract_id"] == "756733"
    assert payload["matches"][0]["primary_exchange"] == "ARCA"


def test_reference_search_reports_provider_lookup_error_when_no_local_match() -> None:
    def broken_search(_provider, _query):
        raise RuntimeError("gateway unavailable")

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                search_provider_instruments=broken_search,
            )
        ),
        "/api/instruments/search",
        "POST",
    )

    payload = endpoint({"provider": "ibkr", "query": "RSU"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_PROVIDER_SEARCH_ERROR"


def test_reference_search_rejects_unknown_provider() -> None:
    endpoint = _route_endpoint(
        create_reference_router(_deps(data_provider_catalog=lambda: [{"key": "ibkr"}])),
        "/api/instruments/search",
        "POST",
    )

    payload = endpoint({"provider": "bad", "query": "PLTR"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_PROVIDER_UNKNOWN"


def test_reference_delete_instrument_storage_unavailable() -> None:
    endpoint = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: None)),
        "/api/instruments/{instrument_id}",
        "DELETE",
    )
    payload = endpoint("TEST")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_STORAGE_NOT_CONFIGURED"


def test_reference_get_returns_default_watchlist_without_persisting() -> None:
    saved = {}

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore(
                    [ibkr_future_payload("ES"), ibkr_stock_payload("SPY")],
                    saved,
                ),
            )
        ),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is True
    assert [item["key"] for item in payload["items"]] == ["ES", "SPY"]
    assert [item["presentation"] for item in payload["items"]] == [
        {"display_mode": "classic", "revision": 0},
        {"display_mode": "classic", "revision": 0},
    ]
    assert payload["watchlist_version"] == 0
    assert saved == {}


def test_reference_watchlist_presentation_is_global_versioned_and_exact() -> None:
    spy = ibkr_stock_payload("SPY", con_id=756733)
    qqq = ibkr_stock_payload("QQQ", con_id=320227571)
    store = WatchlistStore([spy, qqq])
    router = create_reference_router(_deps(store_factory=lambda: store))
    update = _route_endpoint(
        router,
        "/api/instruments/{instrument_id}/presentation",
        "PUT",
    )
    read = _route_endpoint(router, "/api/instruments", "GET")

    first = update(spy["instrument_id"], {"display_mode": "trend"})
    second = update(spy["instrument_id"], {"display_mode": "classic"})
    other = update(qqq["instrument_id"], {"display_mode": "trend"})

    assert first == {
        "ok": True,
        "instrument_id": spy["instrument_id"],
        "presentation": {"display_mode": "trend", "revision": 1},
    }
    assert second["presentation"] == {"display_mode": "classic", "revision": 2}
    assert other["presentation"] == {"display_mode": "trend", "revision": 1}
    payload = read()
    assert payload["watchlist_version"] == 0
    assert {item["instrument_id"]: item["presentation"] for item in payload["items"]} == {
        spy["instrument_id"]: {"display_mode": "classic", "revision": 2},
        qqq["instrument_id"]: {"display_mode": "trend", "revision": 1},
    }


def test_reference_watchlist_presentation_rejects_invalid_and_unknown_inputs() -> None:
    spy = ibkr_stock_payload("SPY")
    update = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: WatchlistStore([spy]))),
        "/api/instruments/{instrument_id}/presentation",
        "PUT",
    )

    for payload in (
        {},
        {"display_mode": "spark"},
        {"display_mode": 1},
        {"display_mode": "trend", "extra": True},
    ):
        rejected = update(spy["instrument_id"], payload)
        assert rejected["ok"] is False
        assert rejected["error"]["code"] == "REFERENCE_WATCHLIST_PRESENTATION_INVALID"

    unknown = update("SPY", {"display_mode": "trend"})
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "REFERENCE_WATCHLIST_INSTRUMENT_UNKNOWN"


def test_reference_watchlist_presentation_reports_storage_unavailable() -> None:
    update = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: None)),
        "/api/instruments/{instrument_id}/presentation",
        "PUT",
    )

    payload = update("ibkr|contract|756733", {"display_mode": "trend"})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_STORAGE_NOT_CONFIGURED"


def test_reference_get_returns_version_from_the_same_watchlist_snapshot() -> None:
    store = WatchlistStore([ibkr_stock_payload("SPY")])
    store.version = 7
    endpoint = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: store)),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is True
    assert payload["watchlist_version"] == 7
    assert "watchlist_version" not in payload["items"][0]


def test_reference_get_keeps_current_watchlist_table_stable() -> None:
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore([ibkr_stock_payload("PLTR")]),
            )
        ),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is True
    assert [item["key"] for item in payload["items"]] == ["PLTR"]


def test_reference_get_rejects_unsupported_provider_without_partial_response(monkeypatch) -> None:
    unsupported = _unsupported_provider_instrument()
    active = ibkr_stock_payload("SPY", con_id=756733)
    routed_providers: list[str] = []
    route_instrument = watchlist_item_module.route_instrument

    def tracked_route_instrument(instrument):
        routed_providers.append(instrument["provider"])
        return route_instrument(instrument)

    monkeypatch.setattr(watchlist_item_module, "route_instrument", tracked_route_instrument)
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore([unsupported, active]),
            )
        ),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_WATCHLIST_READ_ERROR"
    assert payload["error"]["message"] == "Unsupported data source: moex"
    assert routed_providers == ["moex"]

    with pytest.raises(ValueError, match="Unsupported data source: moex"):
        selected_instruments([unsupported, active])
    assert routed_providers == ["moex", "moex"]


def test_reference_get_exposes_explicit_provider_current_futures_contract() -> None:
    root = ibkr_future_payload("ES")
    root["contract_identity"].pop("current_contract")
    store = WatchlistStore([root])
    store.current_futures_contracts[("ibkr", root["instrument_id"])] = {
        "contract_key": "ESU6",
        "provider_contract_id": "649180671",
        "local_symbol": "ESU6",
        "con_id": 649180671,
        "source": "provider_current_contract",
        "resolved_at": "2026-07-13T04:30:00+00:00",
    }
    endpoint = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: store)),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is True
    current = payload["items"][0]["contract_identity"]["current_contract"]
    assert current["contract_key"] == "ESU6"
    assert current["local_symbol"] == "ESU6"
    assert current["source"] == "provider_current_contract"


def test_reference_get_exposes_exact_current_contract_trading_schedule() -> None:
    root = ibkr_future_payload("ES")
    root["contract_identity"].pop("current_contract")
    store = WatchlistStore([root])
    store.current_futures_contracts[("ibkr", root["instrument_id"])] = {
        "contract_key": "ESU6",
        "provider_contract_id": "649180671",
        "local_symbol": "ESU6",
        "con_id": 649180671,
    }
    schedule_key = ("ibkr", root["instrument_id"], "649180671")
    store.provider_sessions[schedule_key] = {
        "provider": "ibkr",
        "calendar": "ibkr_trading_hours",
        "timezone": "US/Central",
        "provider_contract_id": "649180671",
        "source_fetched_at": "2026-07-13T04:30:00+00:00",
        "trading_intervals": [
            {
                "session_date": "2026-07-13",
                "opens_at": "2026-07-13T22:00:00+00:00",
                "closes_at": "2026-07-14T21:00:00+00:00",
                "status": "open",
            }
        ],
    }
    endpoint = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: store)),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is True
    session = payload["items"][0]["session"]
    assert session["calendar"] == "ibkr_trading_hours"
    assert session["timezone"] == "US/Central"
    assert session["provider_contract_id"] == "649180671"
    assert session["trading_intervals"][0]["session_date"] == "2026-07-13"
    assert session["trading_intervals"] == [
        {
            "session_date": "2026-07-13",
            "opens_at": "2026-07-13T22:00:00+00:00",
            "closes_at": "2026-07-14T21:00:00+00:00",
            "status": "open",
        }
    ]


def test_reference_get_normalizes_persisted_watchlist_metadata_without_persisting() -> None:
    saved = {}

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore(
                    [ibkr_stock_payload("UNKNOWN")],
                    saved,
                ),
            )
        ),
        "/api/instruments",
        "GET",
    )

    payload = endpoint()

    assert payload["ok"] is True
    assert [item["key"] for item in payload["items"]] == ["UNKNOWN"]
    assert saved == {}


def test_watchlist_materialization_rejects_non_boolean_quote_only() -> None:
    source = ibkr_stock_payload("PLTR")
    source["quote_only"] = "false"

    with pytest.raises(ValueError, match="quote_only must be a boolean"):
        materialize_watchlist_instrument(source)

    source["quote_only"] = False
    item = materialize_watchlist_instrument(source)

    assert item["key"] == "PLTR"
    assert item["provider"] == "ibkr"
    assert item["provider_symbol"] == "PLTR"
    assert item["session_contract_id"] == item["provider_contract_id"]
    assert item["quote_only"] is False
    assert "ibkr_symbol" not in item
    assert "aliases" not in item
    assert item["session"]["calendar"] == "unknown"
    assert item["session"]["family"] == "unknown"
    assert item["session"]["timezone"] == "unknown"


def test_watchlist_session_refresh_skips_continuous_crypto_calendar(monkeypatch) -> None:
    refreshed = []

    def mark_stale(*, instrument, reason="", store=None):
        refreshed.append((instrument["instrument_id"], instrument["route_fingerprint"], reason))

    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.mark_provider_trading_hours_stale", mark_stale
    )
    item = materialize_watchlist_instrument(coinbase_btc_payload())
    queue_watchlist_session_refresh(item)

    assert "ibkr_symbol" not in item
    assert refreshed == []


def test_reference_delete_builtin_instrument_removes_from_persisted_watchlist() -> None:
    saved = {}
    reconciled: list[str] = []
    eur = ibkr_stock_payload(
        "EUR.USD", name="EUR/USD forex", con_id=12087792, asset_class="forex", sec_type="CASH"
    )
    pltr = ibkr_stock_payload("PLTR", name="Palantir", con_id=12345)

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore(
                    [eur, pltr],
                    saved,
                ),
                reconcile_client_settings=lambda *_args: reconciled.append("client"),
                reconcile_gex_scheduler_settings=lambda *_args: reconciled.append("gex") or {},
                reconcile_option_target_caps_settings=lambda *_args: (
                    reconciled.append("option-target") or {}
                ),
            )
        ),
        "/api/instruments/{instrument_id}",
        "DELETE",
    )

    payload = endpoint(eur["instrument_id"])

    assert payload["ok"] is True
    assert [item["key"] for item in saved["items"]] == ["PLTR"]
    assert reconciled == ["client", "gex", "option-target"]


def test_reference_delete_reports_projection_reconciliation_failure() -> None:
    instrument = ibkr_stock_payload("PLTR", name="Palantir", con_id=12345)
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore([instrument]),
                reconcile_client_settings=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("client projection unavailable")
                ),
            )
        ),
        "/api/instruments/{instrument_id}",
        "DELETE",
    )

    payload = endpoint(instrument["instrument_id"])

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_INSTRUMENT_DELETE_ERROR"
    assert payload["error"]["retryable"] is True


def test_reference_add_instrument_upserts_persisted_watchlist() -> None:
    saved = {}
    store = WatchlistStore(
        [ibkr_stock_payload("NVDA", name="NVIDIA", con_id=4815747)],
        saved,
    )
    route_refreshes: list[tuple[object, dict]] = []

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda provider, _contract_id: ibkr_stock_payload(
                    "PLTR", name="Palantir Technologies", con_id=12345
                ),
                store_factory=lambda: store,
                refresh_quote_routes=lambda refreshed_store, **kwargs: route_refreshes.append(
                    (refreshed_store, dict(kwargs))
                ),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
            "name": "Payload Name",
        }
    )

    assert payload["ok"] is True
    assert [item["key"] for item in saved["items"]] == ["NVDA", "PLTR"]
    pltr = saved["items"][1]
    assert pltr["provider_symbol"] == "PLTR"
    assert "data_symbol" not in pltr
    assert pltr["provider_symbol"] == "PLTR"
    assert pltr["name"] == "Palantir Technologies"
    assert "aliases" not in pltr
    assert pltr["session"]["calendar"] == "unknown"
    assert route_refreshes == [(store, {"expected_watchlist_version": 1})]


def test_reference_add_uses_selected_provider_membership() -> None:
    saved = {}

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}, {"key": "demo"}],
                store_factory=lambda: WatchlistStore(saved=saved),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "DUAL",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_PROVIDER_REBIND_FAILED"
    assert saved == {}


def test_reference_add_persists_provider_lookup_candidate() -> None:
    saved = {}

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda provider, contract_id: (
                    ibkr_stock_payload("RSU", name="Research Solutions Inc", con_id=12345)
                    if str(contract_id) == "12345"
                    else None
                ),
                store_factory=lambda: WatchlistStore(saved=saved),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "RSU",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is True
    item = saved["items"][0]
    assert item["key"] == "RSU"
    assert item["provider"] == "ibkr"
    assert item["provider_symbol"] == "RSU"
    assert item["contract_identity"]["con_id"] == 12345
    assert item["name"] == "Research Solutions Inc"


def test_reference_reorder_preserves_watchlist_membership_and_appends_missing_items() -> None:
    saved = {}
    es = ibkr_future_payload("ES")
    spy = ibkr_stock_payload("SPY", con_id=756733)
    qqq = ibkr_stock_payload("QQQ", con_id=320227571)

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore(
                    [es, spy, qqq],
                    saved,
                ),
            )
        ),
        "/api/instruments/order",
        "PUT",
    )

    payload = endpoint(
        {"order": [qqq["instrument_id"], es["instrument_id"]], "expected_version": 0}
    )

    assert payload["ok"] is True
    assert payload["order"] == [qqq["instrument_id"], es["instrument_id"], spy["instrument_id"]]
    assert [item["key"] for item in saved["items"]] == ["QQQ", "ES", "SPY"]


def test_reference_reorder_rejects_unknown_instrument_ids() -> None:
    saved = {}
    es = ibkr_future_payload("ES")

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: WatchlistStore([es], saved),
            )
        ),
        "/api/instruments/order",
        "PUT",
    )

    payload = endpoint(
        {"order": [es["instrument_id"], "missing-provider-contract-id"], "expected_version": 0}
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_ORDER_UNKNOWN_INSTRUMENT_ID"
    assert payload["instrument_ids"] == ["missing-provider-contract-id"]
    assert saved == {}


def test_reference_reorder_rejects_stale_watchlist_version() -> None:
    class VersionedWatchlistStore(WatchlistStore):
        def reorder_watchlist(self, ordered_keys, expected_version=None):
            if expected_version != 7:
                raise WatchlistOrderError(
                    "WATCHLIST_ORDER_STALE",
                    expected_version=expected_version,
                    current_version=7,
                )
            return super().reorder_watchlist(ordered_keys, expected_version=expected_version)

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                store_factory=lambda: VersionedWatchlistStore(
                    [ibkr_future_payload("ES"), ibkr_stock_payload("SPY")],
                ),
            )
        ),
        "/api/instruments/order",
        "PUT",
    )

    payload = endpoint(
        {"order": ["ibkr|contract|12345", "ibkr|future_root|ES|CME|USD|"], "expected_version": 6}
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_ORDER_STALE"
    assert payload["expected_version"] == 6
    assert payload["current_version"] == 7


def test_reference_reorder_rejects_removed_version_alias() -> None:
    instrument = ibkr_future_payload("ES")
    endpoint = _route_endpoint(
        create_reference_router(_deps(store_factory=lambda: WatchlistStore([instrument]))),
        "/api/instruments/order",
        "PUT",
    )

    payload = endpoint({"order": [instrument["instrument_id"]], "version": 0})

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_ORDER_VERSION_REQUIRED"


def test_reference_add_instrument_queues_provider_session_refresh(monkeypatch) -> None:
    saved = {}
    refreshed = []

    def mark_stale(*, instrument, reason="", store=None):
        refreshed.append((instrument["instrument_id"], instrument["route_fingerprint"], reason))

    monkeypatch.setattr(
        "aef_terminal.data.provider_sessions.mark_provider_trading_hours_stale", mark_stale
    )
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_instrument=lambda provider, _contract_id: ibkr_stock_payload(
                    "PLTR", name="Palantir Technologies", con_id=12345
                ),
                store_factory=lambda: WatchlistStore(saved=saved),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|12345",
            "provider": "ibkr",
            "instrument_key": "PLTR",
            "provider_contract_id": "12345",
        }
    )

    assert payload["ok"] is True
    persisted = saved["items"][0]
    assert refreshed == [(persisted["instrument_id"], persisted["route_fingerprint"], "")]
    assert saved["items"][0]["session"]["calendar"] == "unknown"
    assert "aliases" not in saved["items"][0]


def test_reference_add_future_root_persists_provider_bound_contract_metadata() -> None:
    saved = {}
    store = WatchlistStore(saved=saved)
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_future_root=lambda provider, root, _binding=None: ibkr_future_payload(
                    root,
                    con_id=11004968,
                    local_symbol="ESU6",
                ),
                store_factory=lambda: store,
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|future_root|ES|CME|USD|",
            "provider": "ibkr",
            "instrument_key": "ES",
            "root": "ES",
            "asset_class": "future",
            "identity_scope": "root",
        }
    )

    assert payload["ok"] is True
    persisted = saved["future_contracts"][0]
    assert persisted["provider"] == "ibkr"
    assert persisted["instrument_id"] == "ibkr|future_root|ES|CME|USD|"
    assert persisted["contract_key"] == "ESU6"
    assert persisted["local_symbol"] == "ESU6"
    assert persisted["metadata"]["provider_contract_id"] == "11004968"
    assert persisted["metadata"]["source"] == "provider_lifecycle"


def test_reference_reselect_future_root_carries_typed_route_transition() -> None:
    previous = ibkr_future_payload("ES", con_id=111, local_symbol="ESU6")
    store = WatchlistStore([previous])
    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                bind_provider_future_root=lambda provider, root, _binding=None: ibkr_future_payload(
                    root,
                    con_id=222,
                    local_symbol="ESZ6",
                ),
                store_factory=lambda: store,
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": previous["instrument_id"],
            "provider": "ibkr",
            "instrument_key": "ES",
            "root": "ES",
            "asset_class": "future",
            "identity_scope": "root",
        }
    )

    assert payload["ok"] is True
    transition = store.selection_route_transitions[-1]
    assert transition.previous_session_contract_id == "111"
    assert transition.next_session_contract_id == "222"
    assert transition.previous_route_fingerprint != transition.next_route_fingerprint


def test_reference_add_rejects_unknown_instrument() -> None:
    saved = {}

    endpoint = _route_endpoint(
        create_reference_router(
            _deps(
                data_provider_catalog=lambda: [{"key": "ibkr"}],
                store_factory=lambda: WatchlistStore(
                    [
                        {
                            "key": "UNKNOWN",
                            "display": "UNKNOWN",
                            "provider": "ibkr",
                            "provider_symbol": "UNKNOWN",
                        }
                    ],
                    saved,
                ),
            )
        ),
        "/api/instruments",
        "POST",
    )

    payload = endpoint(
        {
            "instrument_id": "ibkr|contract|99999",
            "provider": "ibkr",
            "instrument_key": "UNKNOWN",
            "provider_contract_id": "99999",
        }
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == "REFERENCE_PROVIDER_REBIND_FAILED"
    assert saved == {}


def _deps(**overrides):
    defaults = {
        "reconcile_client_settings": lambda *_args: None,
        "reconcile_gex_scheduler_settings": lambda *_args: {},
        "reconcile_option_target_caps_settings": lambda *_args: {},
        "data_provider_catalog": lambda: [],
        "search_provider_instruments": lambda _provider, _query: [],
        "bind_provider_instrument": lambda _provider, _key: None,
        "bind_provider_future_root": lambda _provider, _root, _binding=None: None,
        "store_factory": lambda: None,
        "refresh_quote_routes": lambda _store, **_kwargs: None,
    }
    defaults.update(overrides)
    return ReferenceRouterDeps(**defaults)
