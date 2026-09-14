from __future__ import annotations

import importlib
import time
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.alerts.runtime_registry import AlertRuntimeRegistry
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.quote_helpers import quote_envelope, quote_snapshot_ts
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision, QuoteStreamRuntime
from tests.provider_payloads import (
    coinbase_btc_payload,
    ibkr_future_payload,
)


quote_stream_runtime = importlib.import_module("aef_terminal.ui.runtime.quote_stream")


class RouteSnapshotStore:
    def __init__(self, instruments: list[dict], version: int = 1) -> None:
        self.instruments = instruments
        self.version = version
        self.reads = 0

    def read_watchlist_snapshot(self) -> tuple[list[dict], int]:
        self.reads += 1
        return self.instruments, self.version


def refresh_routes(
    runtime: QuoteStreamRuntime,
    instruments: list[dict],
    *,
    version: int = 1,
) -> RouteSnapshotStore:
    store = RouteSnapshotStore(instruments, version)
    runtime.refresh_routes_from_store(
        store,
        route_generation_changed=True,
    )
    return store


def unsupported_provider_instrument() -> dict:
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


def test_quote_envelope_uses_provider_time_and_rejects_future_clock_skew() -> None:
    received_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    provider_ts = received_at + timedelta(seconds=10)
    row = {
        "price": 500.0,
        "price_source": "last",
        "last": 500.0,
        "provider_ts": provider_ts.isoformat(),
        "time_basis": "provider_event",
        "ts": received_at.isoformat(),
        "market_data_entitlement": "live",
        "is_delayed": False,
    }

    envelope = quote_envelope(row, received_at=received_at, stale_after_seconds=5.0)

    assert quote_snapshot_ts(row) == provider_ts
    assert envelope["provider_ts"] == provider_ts.isoformat()
    assert envelope["status"] == "stale"
    assert envelope["is_stale"] is True


def test_quote_stream_runtime_keeps_last_success_timestamp_on_empty_publish(
    persisted_lookup,
) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    fingerprint = route.fingerprint
    refresh_routes(runtime, [instrument])
    runtime.store_cache({(route.instrument_id, fingerprint): {"price": 500.0}}, "")
    cached, warning, generation = runtime.cache_for_instruments([instrument])
    assert warning == ""
    assert cached[fingerprint]["price"] == 500.0

    runtime.store_cache({}, "temporary feed failure")
    cached, warning, next_generation = runtime.cache_for_instruments([instrument])
    assert cached[fingerprint]["price"] == 500.0
    assert warning == "temporary feed failure"
    assert next_generation.epoch == generation.epoch
    assert next_generation.generation > generation.generation


def test_quote_stream_runtime_conditional_cache_read_skips_unchanged_projection(
    persisted_lookup,
) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    refresh_routes(runtime, [instrument])
    runtime.store_cache(
        {(route.instrument_id, route.fingerprint): {"price": 500.0}},
        "",
    )

    cached, warning, generation = runtime.cache_for_instruments([instrument])
    unchanged, same_warning, same_generation = runtime.cache_for_instruments(
        [instrument],
        after_revision=generation,
    )

    assert cached[route.fingerprint]["price"] == 500.0
    assert warning == ""
    assert unchanged is None
    assert same_warning == ""
    assert same_generation == generation

    changed_epoch, _, current_revision = runtime.cache_for_instruments(
        [instrument],
        after_revision=QuoteCacheRevision(
            "00000000-0000-4000-8000-000000000001",
            generation.generation,
        ),
    )
    assert changed_epoch is not None
    assert current_revision == generation

    with pytest.raises(TypeError, match="QuoteCacheRevision"):
        runtime.cache_for_instruments([instrument], after_revision=0)
    assert isinstance(generation, QuoteCacheRevision)


def test_quote_stream_age_changes_do_not_advance_semantic_revision(
    monkeypatch,
) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = coinbase_btc_payload()
    route = route_instrument(instrument)
    refresh_routes(runtime, [instrument])
    observed_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    clock = [observed_at]
    quote = {
        "price": 65000.0,
        "price_source": "last",
        "last": 65000.0,
        "provider_ts": observed_at.isoformat(),
        "last_provider_ts": observed_at.isoformat(),
        "received_at": observed_at.isoformat(),
        "time_basis": "provider_event",
        "market_data_entitlement": "live",
        "is_delayed": False,
    }

    monkeypatch.setattr(
        quote_stream_runtime,
        "quote_envelope",
        lambda row, *, stale_after_seconds, received_at=None: quote_envelope(
            row,
            received_at=clock[0],
            stale_after_seconds=stale_after_seconds,
        ),
    )
    runtime.store_cache({(route.instrument_id, route.fingerprint): quote})
    first_payload, _, first_revision = runtime.cache_for_instruments([instrument])

    clock[0] = observed_at + timedelta(seconds=1)
    runtime.store_cache({(route.instrument_id, route.fingerprint): quote})
    unchanged, _, same_revision = runtime.cache_for_instruments(
        [instrument],
        after_revision=first_revision,
    )

    assert unchanged is None
    assert same_revision == first_revision
    assert first_payload[route.fingerprint]["status"] == "live"

    clock[0] = observed_at + timedelta(seconds=6)
    runtime.store_cache({(route.instrument_id, route.fingerprint): quote})
    stale_payload, _, stale_revision = runtime.cache_for_instruments([instrument])

    assert stale_revision.generation == first_revision.generation + 1
    assert stale_payload[route.fingerprint]["status"] == "stale"

    clock[0] = observed_at + timedelta(seconds=7)
    runtime.store_cache({(route.instrument_id, route.fingerprint): quote})
    unchanged, _, stable_stale_revision = runtime.cache_for_instruments(
        [instrument],
        after_revision=stale_revision,
    )

    assert unchanged is None
    assert stable_stale_revision == stale_revision


def test_quote_stream_empty_route_row_does_not_replace_last_observation(
    monkeypatch,
) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = coinbase_btc_payload()
    route = route_instrument(instrument)
    refresh_routes(runtime, [instrument])
    observed_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    quote = {
        "price": 65000.0,
        "price_source": "last",
        "last": 65000.0,
        "provider_ts": observed_at.isoformat(),
        "last_provider_ts": observed_at.isoformat(),
        "received_at": observed_at.isoformat(),
        "time_basis": "provider_event",
        "market_data_entitlement": "live",
        "is_delayed": False,
    }
    monkeypatch.setattr(
        quote_stream_runtime,
        "quote_envelope",
        lambda row, *, stale_after_seconds, received_at=None: quote_envelope(
            row,
            received_at=observed_at,
            stale_after_seconds=stale_after_seconds,
        ),
    )

    runtime.store_cache({(route.instrument_id, route.fingerprint): quote})
    first_payload, _, first_revision = runtime.cache_for_instruments([instrument])
    runtime.store_cache({(route.instrument_id, route.fingerprint): {}})
    unchanged, _, same_revision = runtime.cache_for_instruments(
        [instrument],
        after_revision=first_revision,
    )

    assert unchanged is None
    assert same_revision == first_revision
    assert first_payload[route.fingerprint]["status"] == "live"


def test_quote_stream_runtime_warns_when_no_successful_snapshot_exists(persisted_lookup) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    refresh_routes(runtime, [ibkr_future_payload("ES")])
    runtime.store_cache({}, "")

    _cached, warning, _generation = runtime.cache_for_instruments([ibkr_future_payload("ES")])

    assert "no successful refresh yet" in warning


def test_quote_route_snapshot_propagates_required_storage_failure() -> None:
    class UnavailableStore:
        def read_watchlist_snapshot(self):
            raise RuntimeError("WATCHLIST_STORAGE_REQUIRED")

    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    with pytest.raises(RuntimeError, match="WATCHLIST_STORAGE_REQUIRED"):
        runtime.refresh_routes_from_store(
            UnavailableStore(),
            route_generation_changed=True,
        )


@pytest.fixture
def persisted_lookup() -> list[dict]:
    return [
        ibkr_future_payload("ES"),
        coinbase_btc_payload(),
    ]


def test_quote_stream_runtime_filters_qualified_instruments_by_provider(persisted_lookup) -> None:
    es_id = route_instrument(ibkr_future_payload("ES")).instrument_id
    btc_id = route_instrument(coinbase_btc_payload()).instrument_id
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    refresh_routes(runtime, persisted_lookup)
    runtime.configure_server_sleeping(server_sleeping=lambda: False)

    runtime.set_stream_wanted(1, [btc_id, es_id])

    instruments = runtime.combined_quote_instruments([])
    assert [route_instrument(instrument).instrument_id for instrument in instruments] == [es_id]
    assert [
        route_instrument(instrument).instrument_id
        for instrument in runtime.combined_provider_quote_instruments("coinbase", [])
    ] == [btc_id]


def test_quote_stream_runtime_rejects_unresolved_wanted_instrument_id(persisted_lookup) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    refresh_routes(runtime, [ibkr_future_payload("ES")])
    runtime.configure_server_sleeping(server_sleeping=lambda: False)
    runtime.set_stream_wanted(1, ["missing|instrument-id"])

    with pytest.raises(
        ValueError,
        match=r"QUOTE_ROUTE_SNAPSHOT_INSTRUMENT_REQUIRED.*missing\|instrument-id",
    ):
        runtime.combined_quote_instruments([])


def test_quote_stream_runtime_provider_allowance_uses_instrument_provider(persisted_lookup) -> None:
    btc_id = route_instrument(coinbase_btc_payload()).instrument_id
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    refresh_routes(runtime, persisted_lookup)

    assert runtime.provider_quote_wanted_allowed("coinbase", btc_id) is True
    assert runtime.provider_quote_wanted_allowed("ibkr", btc_id) is False


def test_quote_stream_runtime_expires_transient_wanted_while_sleeping() -> None:
    es_id = route_instrument(ibkr_future_payload("ES")).instrument_id
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    refresh_routes(runtime, [ibkr_future_payload("ES")])
    runtime.configure_server_sleeping(server_sleeping=lambda: True)
    runtime.set_transient_wanted("stale", [es_id], ttl_seconds=0.01)
    time.sleep(0.12)

    assert runtime.combined_quote_instruments([]) == []
    assert runtime._transient_wanted_by_key == {}


def test_quote_stream_runtime_clear_all_wanted_clears_transient_requests() -> None:
    es_id = route_instrument(ibkr_future_payload("ES")).instrument_id
    btc_id = route_instrument(coinbase_btc_payload()).instrument_id
    gc_id = route_instrument(ibkr_future_payload("GC", exchange="COMEX")).instrument_id
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    runtime.set_stream_wanted(1, [es_id])
    runtime.set_server_alert_wanted([btc_id])
    runtime.set_transient_wanted("alert", [gc_id], ttl_seconds=30)

    runtime.clear_all_wanted()

    assert runtime._stream_wanted_by_client == {}
    assert runtime._server_alert_wanted_instrument_ids == set()
    assert runtime._transient_wanted_by_key == {}


def test_quote_stream_runtime_bounds_quote_cache_by_oldest_symbol(persisted_lookup) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1, max_cache_entries=2)
    es = ibkr_future_payload("ES")
    btc = coinbase_btc_payload()
    gc = ibkr_future_payload("GC", exchange="COMEX")

    es_route = route_instrument(es)
    btc_route = route_instrument(btc)
    gc_route = route_instrument(gc)
    refresh_routes(runtime, [es, btc, gc])
    runtime.store_cache({(es_route.instrument_id, es_route.fingerprint): {"price": 1}}, "")
    time.sleep(0.01)
    runtime.store_cache({(btc_route.instrument_id, btc_route.fingerprint): {"price": 2}}, "")
    time.sleep(0.01)
    runtime.store_cache({(gc_route.instrument_id, gc_route.fingerprint): {"price": 3}}, "")

    cached, _warning, _generation = runtime.cache_for_instruments([es, btc, gc])

    assert cached[route_instrument(es).fingerprint]["status"] == "unavailable"
    assert cached[route_instrument(btc).fingerprint]["price"] == 2
    assert cached[route_instrument(gc).fingerprint]["price"] == 3


def test_quote_cache_publication_pushes_ordered_event_only_for_armed_alert_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    refresh_routes(runtime, [instrument])
    alert = {
        "id": "alert-quote-event",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "symbol": "ES",
        "timeframe": "5m",
        "provider": route.provider,
        "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        "kind": "price",
        "label": "",
        "direction": "cross",
        "price": 100.0,
        "toleranceAtr": 0.08,
        "tolerancePoints": 0.0,
        "rearmMinutes": 60,
        "createdAt": 1,
        "level_source": {"type": "fixed_price", "dynamic": False},
        "enabled": True,
        "armed": True,
        "fired": False,
        "cooldownUntil": 0,
        "rearmedAt": 1,
        **TELEGRAM_CANCELLED_DELIVERY_STATE,
    }
    registry = AlertRuntimeRegistry()
    registry.hydrate([alert])
    monkeypatch.setattr(quote_stream_runtime, "price_alert_runtime", registry)
    runtime.store_cache(
        {(route.instrument_id, route.fingerprint): {"price": 99.0}},
        "",
    )
    runtime.store_cache(
        {(route.instrument_id, route.fingerprint): {"price": 101.0}},
        "",
    )
    events, ceiling, overflowed = registry.read_quote_events(0)

    assert overflowed is False
    assert ceiling == 2
    assert [event.wire_quote()["price"] for event in events] == [99.0, 101.0]


def test_quote_stream_runtime_reads_cache_by_qualified_route(persisted_lookup) -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = coinbase_btc_payload()
    route = route_instrument(instrument)
    refresh_routes(runtime, [instrument])
    runtime.store_cache({(route.instrument_id, route.fingerprint): {"price": 62000}}, "")

    cached, warning, _generation = runtime.cache_for_instruments([instrument])

    assert warning == ""
    assert cached[route_instrument(instrument).fingerprint]["price"] == 62000


def test_quote_stream_runtime_drops_publication_outside_active_route_generation() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    refresh_routes(runtime, [instrument])

    runtime.store_cache({("instrument-id", " route-fingerprint "): {"price": 1}}, "")

    assert ("instrument-id", " route-fingerprint ") not in runtime._cache
    assert runtime.route_snapshot().route_identities == {(route.instrument_id, route.fingerprint)}


def test_quote_stream_runtime_rejects_string_coercion_for_identity_collections() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)

    with pytest.raises(TypeError, match="typed sequence"):
        runtime.set_stream_wanted(1, "id-a,id-b")
    with pytest.raises(ValueError, match="exact non-empty strings"):
        runtime.set_stream_wanted(1, [123])
    with pytest.raises(ValueError, match="duplicates"):
        runtime.set_stream_wanted(1, ["id-a", "id-a"])


def test_quote_stream_runtime_rejects_non_string_identity_cache_key() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)

    with pytest.raises(ValueError, match="must be an exact string"):
        runtime.store_cache({(123, "route-fingerprint"): {"price": 1}}, "")


def test_quote_stream_runtime_requires_route_snapshot_before_cache_publish() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)

    with pytest.raises(RuntimeError, match="QUOTE_ROUTE_SNAPSHOT_REQUIRED"):
        runtime.store_cache({}, "")


def test_quote_stream_runtime_reports_cache_stats() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1, max_cache_entries=2)
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    refresh_routes(runtime, [instrument], version=7)
    runtime.store_cache({(route.instrument_id, route.fingerprint): {"price": 5000}}, "warn")

    runtime.stream_started()
    stats = runtime.cache_stats()

    assert stats["clients"] == 1
    assert stats["cache_entries"] == 1
    assert stats["cache_max_entries"] == 2
    assert stats["cache_age_seconds"] is not None
    assert stats["cache_attempted_age_seconds"] is not None
    assert stats["cache_generation"] == 1
    assert isinstance(stats["cache_epoch"], str) and stats["cache_epoch"]
    assert stats["warning"] == "warn"
    assert stats["route_generation"] == 1
    assert stats["route_watchlist_version"] == 7
    assert stats["route_entries"] == 1
    assert stats["route_age_seconds"] is not None


def test_quote_route_snapshot_skips_database_when_version_is_unchanged() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    store = refresh_routes(runtime, [ibkr_future_payload("ES")], version=3)

    first = runtime.route_snapshot()
    second = runtime.refresh_routes_from_store(
        store,
        expected_watchlist_version=3,
    )

    assert second is first
    assert store.reads == 1


def test_quote_route_snapshot_rejects_unsupported_provider_without_partial_publish() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    active = ibkr_future_payload("ES")
    unsupported = unsupported_provider_instrument()

    with pytest.raises(ValueError, match="Unsupported data source: moex"):
        refresh_routes(runtime, [unsupported, active], version=4)

    snapshot = runtime.route_snapshot()
    assert snapshot.generation == 0
    assert snapshot.watchlist_version == -1
    assert snapshot.entries == {}


def test_quote_route_generation_replaces_futures_route_and_evicts_old_cache() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    first = ibkr_future_payload("ES", local_symbol="ESU6", con_id=11004968)
    store = refresh_routes(runtime, [first], version=4)
    first_route = route_instrument(first)
    runtime.store_cache(
        {(first_route.instrument_id, first_route.fingerprint): {"price": 5000}},
        "",
    )
    rolled = ibkr_future_payload("ES", local_symbol="ESZ6", con_id=12000001)
    store.instruments = [rolled]

    snapshot = runtime.refresh_routes_from_store(
        store,
        expected_watchlist_version=4,
        route_generation_changed=True,
    )

    rolled_route = route_instrument(rolled)
    assert snapshot.generation == 2
    assert snapshot.watchlist_version == 4
    assert snapshot.route_identities == {(rolled_route.instrument_id, rolled_route.fingerprint)}
    assert (first_route.instrument_id, first_route.fingerprint) not in runtime._cache
    runtime.store_cache(
        {(first_route.instrument_id, first_route.fingerprint): {"price": 4999}},
        "",
    )
    assert (first_route.instrument_id, first_route.fingerprint) not in runtime._cache


def test_quote_route_snapshot_refreshes_payload_when_exact_route_is_unchanged() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    first = ibkr_future_payload("ES")
    store = refresh_routes(runtime, [first], version=4)
    refreshed = ibkr_future_payload("ES")
    refreshed["contract_identity"]["current_contract"]["resolved_at"] = "2026-07-28T12:00:00+00:00"
    store.instruments = [refreshed]

    snapshot = runtime.refresh_routes_from_store(
        store,
        expected_watchlist_version=4,
        route_generation_changed=True,
    )

    assert snapshot.generation == 2
    assert (
        runtime.select_instruments(None)[0]["contract_identity"]["current_contract"]["resolved_at"]
        == "2026-07-28T12:00:00+00:00"
    )


def test_quote_route_snapshot_isolated_from_source_payload_mutation() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instrument = coinbase_btc_payload()
    instrument_id = route_instrument(instrument).instrument_id
    refresh_routes(runtime, [instrument])
    instrument["provider_symbol"] = "mutated"

    selected = runtime.select_instruments((instrument_id,))

    assert selected[0]["provider_symbol"] == "BTC-USD"
    assert selected[0]["profile"] == "CRYPTO"
    assert selected[0]["route_fingerprint"] == route_instrument(selected[0]).fingerprint
    assert selected[0]["session_contract_id"] == "BTC-USD"
    selected[0]["contract_identity"]["provider_contract_id"] = "mutated-projection"
    assert (
        runtime.select_instruments((instrument_id,))[0]["contract_identity"]["provider_contract_id"]
        == "BTC-USD"
    )


def test_quote_route_snapshot_reuses_only_unchanged_immutable_entries() -> None:
    runtime = QuoteStreamRuntime()
    first = coinbase_btc_payload()
    second = ibkr_future_payload("ES")
    store = refresh_routes(runtime, [first, second])
    old = runtime.route_snapshot()
    store.instruments = [first, {**second, "display": "Updated"}]
    store.version = 2
    current = runtime.refresh_routes_from_store(store, expected_watchlist_version=2)
    first_id, second_id = first["instrument_id"], second["instrument_id"]
    assert current.entries[first_id] is old.entries[first_id]
    assert current.entries[second_id] is not old.entries[second_id]
    assert current.entries[second_id].instrument["display"] == "Updated"
    assert old.entries[second_id].instrument["display"] != "Updated"
    with pytest.raises(TypeError):
        current.entries[first_id].instrument["contract_identity"]["provider_contract_id"] = (
            "changed"
        )


def test_quote_route_snapshot_lists_all_routes_without_storage_lookup() -> None:
    runtime = QuoteStreamRuntime(quote_stream_seconds=0.1)
    instruments = [
        coinbase_btc_payload(),
        ibkr_future_payload("ES"),
    ]
    store = refresh_routes(runtime, instruments)

    selected = runtime.select_instruments(None)

    assert [route_instrument(instrument).instrument_id for instrument in selected] == [
        route_instrument(instrument).instrument_id for instrument in instruments
    ]
    assert store.reads == 1
