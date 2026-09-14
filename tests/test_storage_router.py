from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.drawing_contract import (
    canonical_drawing_point,
    parse_drawing_anchor_projection,
)
from aef_terminal.runtime import chart_events
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection
from aef_terminal.storage.bar_projection import (
    MAX_CONFIRMED_BAR_PROJECTION_ANCHORS,
    read_confirmed_bar_timestamps,
    read_confirmed_bar_logical_projection,
)
from aef_terminal.storage.repos.gex import GexRepoMixin
from aef_terminal.ui.drawing_services import normalize_drawing_anchors
from aef_terminal.ui.routers.storage import StorageRouterDeps, create_storage_router
from aef_terminal.ui.storage_actions import option_targets as option_target_actions
from aef_terminal.ui.storage_actions import drawings as drawing_actions
from tests.provider_payloads import coinbase_btc_payload, ibkr_future_payload


def _route_endpoint(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") == path and method.upper() in (
            getattr(route, "methods", set()) or set()
        ):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class FakeOptionTargetStore:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.deleted: list[tuple[str, str, str]] = []
        self.queries: list[dict[str, str | None]] = []

    def initialize(self) -> None:
        return None

    def read_option_targets(
        self,
        timeframe: str | None = None,
        *,
        route_pairs: list[tuple[str, str]] | None = None,
    ):
        instrument_id, route_fingerprint = route_pairs[0] if route_pairs else (None, None)
        self.queries.append(
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "timeframe": timeframe,
            }
        )
        return [{"id": self.payload["id"], "payload": self.payload}]

    def delete_option_target(
        self,
        instrument_id: str,
        route_fingerprint: str,
        option_id: str,
    ) -> bool:
        self.deleted.append((instrument_id, route_fingerprint, str(option_id)))
        return (
            instrument_id == self.payload["instrument_id"]
            and route_fingerprint == self.payload["route_fingerprint"]
            and str(option_id) == self.payload["id"]
        )


@pytest.fixture(autouse=True)
def exact_runtime_instrument_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    instrument = ibkr_future_payload("ES")
    instrument_id = route_instrument(instrument).instrument_id

    def lookup(requested_instrument_id: str) -> dict[str, Any]:
        if requested_instrument_id != instrument_id:
            raise ValueError(f"missing instrument_id={requested_instrument_id}")
        return instrument

    monkeypatch.setattr(option_target_actions, "lookup_runtime_instrument", lookup)
    monkeypatch.setattr(drawing_actions, "lookup_runtime_instrument", lookup)


def _canonical_option_target_request(
    *,
    target_id: str = "opt-1",
    price: float = 7550.0,
    ts: str = "2026-06-22T05:00:00+00:00",
) -> dict[str, Any]:
    return {
        "id": target_id,
        "symbol": "ES",
        "timeframe": "5m",
        "point": {"price": price, "ts": ts},
        "instrument_id": "ibkr|future_root|ES",
        "route_fingerprint": "ibkr|future|ESU6",
        "provider": "ibkr",
        "provider_contract_id": "11004968",
        "payload": {
            "intent": {
                "provider_symbol": "ES",
                "mode": "normal",
                "right": "P",
                "target_dte": "0dte",
                "sec_type": "FOP",
                "con_id": 123,
                "local_symbol": "ES P7550",
                "exchange": "CME",
                "expiry": "20260622",
                "expiry_at": "2026-06-22T20:00:00+00:00",
                "strike": 7550.0,
                "trading_class": "ES",
                "multiplier": 50.0,
                "currency": "USD",
            }
        },
    }


def _persistent_option_target(**overrides: Any) -> dict[str, Any]:
    item = _canonical_option_target_request(**overrides)
    return GexRepoMixin._option_target_persistent_payload(
        item,
        "ES",
        "5m",
        str(item["id"]),
    )


def test_option_target_persistent_payload_keeps_only_intent_and_empty_sample() -> None:
    item = {
        **_canonical_option_target_request(),
        "text": "runtime label",
        "updated_at_ms": 1782150000000,
    }
    payload = GexRepoMixin._option_target_persistent_payload(
        item,
        "ES",
        "5m",
        "opt-1",
    )

    assert payload["id"] == "opt-1"
    assert payload["point"]["price"] == 7550.0
    assert set(payload["payload"]) == {"intent", "market_sample"}
    assert "target_price" not in payload["payload"]["intent"]
    assert "target_ts" not in payload["payload"]["intent"]
    assert payload["payload"]["market_sample"] == {}
    assert "intent" not in payload
    assert "market_sample" not in payload
    assert "text" not in payload
    assert "updated_at_ms" not in payload


def test_option_target_intent_update_preserves_latest_market_sample() -> None:
    existing = GexRepoMixin._option_target_market_sample_update_payload(
        _persistent_option_target(),
        {
            "fair_price": 12.5,
            "fair_price_status": "ok",
            "live_bid": 12.4,
            "live_ask": 12.6,
            "reference_option_price": 12.5,
            "reference_option_price_source": "bid_ask_mid",
            "live_quote_status": "ok",
            "live_quote_entitlement": "live",
            "live_quote_ts": datetime.now(tz=UTC).isoformat(),
            "live_quote_time_basis": "client_receive",
            "live_underlying_price": 7550.0,
            "underlying_quote_price": 7550.0,
            "underlying_quote_price_source": "bid_ask_mid",
            "underlying_quote_entitlement": "live",
            "underlying_quote_status": "live",
            "underlying_quote_ts": datetime.now(tz=UTC).isoformat(),
            "underlying_quote_time_basis": "client_receive",
            "compression_sample_at": "2026-06-22T05:00:15+00:00",
        },
    )
    incoming = _persistent_option_target(
        price=7562.0,
        ts="2026-06-22T05:15:00+00:00",
    )

    merged = GexRepoMixin._option_target_intent_update_payload(existing, incoming)

    assert merged["point"] == incoming["point"]
    assert "target_price" not in merged["payload"]["intent"]
    assert "target_ts" not in merged["payload"]["intent"]
    assert merged["payload"]["market_sample"] == existing["payload"]["market_sample"]
    assert "intent" not in merged
    assert "market_sample" not in merged


def test_option_target_market_update_preserves_latest_dragged_intent() -> None:
    existing = _persistent_option_target(
        price=7562.0,
        ts="2026-06-22T05:15:00+00:00",
    )
    merged = GexRepoMixin._option_target_market_sample_update_payload(
        existing,
        {
            "fair_price": 13.25,
            "fair_price_status": "ok",
            "live_bid": 13.1,
            "live_ask": 13.4,
            "reference_option_price": 13.25,
            "reference_option_price_source": "bid_ask_mid",
            "live_quote_status": "ok",
            "live_quote_entitlement": "live",
            "live_quote_ts": datetime.now(tz=UTC).isoformat(),
            "live_quote_time_basis": "client_receive",
            "live_underlying_price": 7562.0,
            "underlying_quote_price": 7562.0,
            "underlying_quote_price_source": "bid_ask_mid",
            "underlying_quote_entitlement": "live",
            "underlying_quote_status": "live",
            "underlying_quote_ts": datetime.now(tz=UTC).isoformat(),
            "underlying_quote_time_basis": "client_receive",
            "compression_sample_at": "2026-06-22T05:15:15+00:00",
        },
    )

    assert merged["point"] == existing["point"]
    assert merged["payload"]["intent"] == existing["payload"]["intent"]
    assert merged["payload"]["market_sample"]["fair_price"] == 13.25
    assert merged["payload"]["market_sample"]["live_bid"] == 13.1
    assert "sampled_at" not in merged["payload"]["market_sample"]


def test_option_target_drag_and_reprice_are_order_independent() -> None:
    old = _persistent_option_target(target_id="opt-race")
    dragged = _persistent_option_target(
        target_id="opt-race",
        price=7575.0,
        ts="2026-06-22T05:20:00+00:00",
    )
    sample = {
        "fair_price": 13.0,
        "fair_price_status": "ok",
        "live_bid": 12.9,
        "live_ask": 12.9,
        "reference_option_price": 12.9,
        "reference_option_price_source": "bid_ask_mid",
        "live_quote_status": "ok",
        "live_quote_entitlement": "live",
        "live_quote_ts": datetime.now(tz=UTC).isoformat(),
        "live_quote_time_basis": "client_receive",
        "live_underlying_price": 7575.0,
        "underlying_quote_price": 7575.0,
        "underlying_quote_price_source": "bid_ask_mid",
        "underlying_quote_entitlement": "live",
        "underlying_quote_status": "live",
        "underlying_quote_ts": datetime.now(tz=UTC).isoformat(),
        "underlying_quote_time_basis": "client_receive",
        "compression_sample_at": "2026-06-22T05:20:02+00:00",
    }

    user_then_market = GexRepoMixin._option_target_market_sample_update_payload(
        GexRepoMixin._option_target_intent_update_payload(old, dragged),
        sample,
    )
    market_then_user = GexRepoMixin._option_target_intent_update_payload(
        GexRepoMixin._option_target_market_sample_update_payload(old, sample),
        dragged,
    )

    for merged in (user_then_market, market_then_user):
        assert merged["point"] == dragged["point"]
        assert "target_price" not in merged["payload"]["intent"]
        assert "target_ts" not in merged["payload"]["intent"]
        assert merged["payload"]["market_sample"]["fair_price"] == 13.0
        assert merged["payload"]["market_sample"]["live_bid"] == 12.9


def test_option_target_get_requires_and_reads_exact_route() -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "payload": {
            "intent": {
                "provider_symbol": "ES",
                "mode": "normal",
                "right": "P",
                "target_dte": "0dte",
                "sec_type": "FOP",
                "con_id": 12345,
                "local_symbol": "ES P4970",
                "exchange": "CME",
                "expiry": "20260722",
                "strike": 4970.0,
            },
            "market_sample": {},
        },
    }
    store = FakeOptionTargetStore(target)
    deps = StorageRouterDeps(
        store_factory=lambda: store,
        apply_ibkr_runtime_settings=lambda _settings=None: {},
        publish_client_settings_mutations=lambda _mutations, settings_revision: None,
        normalize_drawing_anchors=lambda _store, _symbol, _interval, drawings, **_kwargs: drawings,
        require_unique_price_alerts=lambda alerts, **_kwargs: alerts,
    )
    endpoint = _route_endpoint(create_storage_router(deps), "/api/option-targets", "GET")

    result = endpoint(route.instrument_id, route.fingerprint)

    assert result["ok"] is True
    assert result["instrument_id"] == route.instrument_id
    assert result["route_fingerprint"] == route.fingerprint
    assert result["items"] == [target]
    assert store.queries == [
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "timeframe": None,
        }
    ]


def test_option_target_get_rejects_stale_route_without_reading_store() -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    target = {
        "id": "opt-old-route",
        "symbol": "ES",
        "instrument_id": route.instrument_id,
        "route_fingerprint": "route:previous-contract",
        "payload": {},
    }
    store = FakeOptionTargetStore(target)
    deps = StorageRouterDeps(
        store_factory=lambda: store,
        apply_ibkr_runtime_settings=lambda _settings=None: {},
        publish_client_settings_mutations=lambda _mutations, settings_revision: None,
        normalize_drawing_anchors=lambda _store, _symbol, _interval, drawings, **_kwargs: drawings,
        require_unique_price_alerts=lambda alerts, **_kwargs: alerts,
    )
    endpoint = _route_endpoint(create_storage_router(deps), "/api/option-targets", "GET")

    result = endpoint(route.instrument_id, "route:previous-contract")

    assert result["ok"] is False
    assert result["error"]["code"] == "STORAGE_INSTRUMENT_ROUTE_MISMATCH"
    assert store.queries == []


def test_option_target_delete_cancels_live_option_quote_subscription(monkeypatch) -> None:
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    option_intent = {
        "provider_symbol": "ES",
        "mode": "normal",
        "right": "P",
        "target_dte": "0dte",
        "sec_type": "FOP",
        "con_id": 12345,
        "local_symbol": "ES P4970",
        "exchange": "CME",
        "expiry": "20260722",
        "strike": 4970.0,
    }
    target = {
        "id": "opt-1",
        "symbol": "ES",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "payload": {"intent": option_intent, "market_sample": {}},
    }
    store = FakeOptionTargetStore(target)
    cancels: list[tuple[dict[str, Any], str]] = []
    monkeypatch.setattr(
        route.adapter,
        "cancel_option_quote",
        lambda _instrument, item, *, consumer_id: (
            cancels.append((item, consumer_id)) or {"ok": True, "cancelled": True}
        ),
    )
    deps = StorageRouterDeps(
        store_factory=lambda: store,
        apply_ibkr_runtime_settings=lambda _settings=None: {},
        publish_client_settings_mutations=lambda _mutations, settings_revision: None,
        normalize_drawing_anchors=lambda _store, _symbol, _interval, drawings, **_kwargs: drawings,
        require_unique_price_alerts=lambda alerts, **_kwargs: alerts,
    )
    endpoint = _route_endpoint(
        create_storage_router(deps), "/api/option-targets/{target_id}", "DELETE"
    )

    result = endpoint("opt-1", route.instrument_id, route.fingerprint)

    assert result["ok"] is True
    assert result["deleted"] is True
    assert result["quote_cancel"]["cancelled"] is True
    assert store.deleted == [(route.instrument_id, route.fingerprint, "opt-1")]
    assert cancels == [
        (
            {"sec_type": "FOP", "con_id": 12345, "exchange": "CME"},
            "option-point:opt-1",
        )
    ]


def test_storage_client_unavailable_returns_structured_error() -> None:
    deps = StorageRouterDeps(
        store_factory=lambda: None,
        apply_ibkr_runtime_settings=lambda _settings=None: {},
        publish_client_settings_mutations=lambda _mutations, settings_revision: None,
        normalize_drawing_anchors=lambda _store, _symbol, _interval, drawings, **_kwargs: drawings,
        require_unique_price_alerts=lambda alerts, **_kwargs: alerts,
    )
    endpoint = _route_endpoint(create_storage_router(deps), "/api/storage", "GET")
    payload = endpoint(interval="5m", drawings=False, alerts=False)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "STORAGE_NOT_CONFIGURED"
    assert payload["message"] == "storage not configured"


def _storage_deps(**overrides):
    defaults = {
        "store_factory": lambda: None,
        "apply_ibkr_runtime_settings": lambda _settings=None: {},
        "publish_client_settings_mutations": lambda _mutations, settings_revision: None,
        "normalize_drawing_anchors": lambda _store, _symbol, _interval, drawings, **_kwargs: (
            drawings
        ),
        "require_unique_price_alerts": lambda alerts, **_kwargs: alerts,
    }
    defaults.update(overrides)
    return StorageRouterDeps(**defaults)


def test_drawings_save_invalid_payload() -> None:
    endpoint = _route_endpoint(create_storage_router(_storage_deps()), "/api/drawings", "PUT")
    payload = endpoint({"symbol": "ES"})
    assert payload["ok"] is False
    assert payload["error"]["code"] == "STORAGE_DRAWINGS_PAYLOAD_INVALID"


def test_server_rejects_suspicious_drawings_mass_shrink() -> None:
    route = route_instrument(ibkr_future_payload("ES"))

    class Store:
        def __init__(self):
            self.replaced = None

        def initialize(self):
            return None

        def read_drawings(self, instrument_id, interval, *, route_fingerprint):
            assert instrument_id == route.instrument_id
            assert route_fingerprint == route.fingerprint
            return [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

        def replace_drawings(
            self,
            instrument_id,
            presentation_symbol,
            interval,
            drawings,
            **identity,
        ):
            assert instrument_id == route.instrument_id
            assert identity["route_fingerprint"] == route.fingerprint
            self.replaced = drawings

    store = Store()
    endpoint = _route_endpoint(
        create_storage_router(_storage_deps(store_factory=lambda: store)), "/api/drawings", "PUT"
    )

    identity = {"instrument_id": route.instrument_id, "route_fingerprint": route.fingerprint}
    blocked_unexplained = endpoint(
        {
            **identity,
            "interval": "5m",
            "drawings": [{"id": "a"}],
            "allow_mass_delete": False,
            "deleted_ids": [],
        }
    )
    allowed_specific = endpoint(
        {
            **identity,
            "interval": "5m",
            "drawings": [{"id": "a"}, {"id": "d"}],
            "allow_mass_delete": False,
            "deleted_ids": ["b", "c"],
        }
    )
    blocked_full_clear = endpoint(
        {
            **identity,
            "interval": "5m",
            "drawings": [],
            "allow_mass_delete": False,
            "deleted_ids": ["a", "b", "c", "d"],
        }
    )
    allowed_clear_button = endpoint(
        {
            **identity,
            "interval": "5m",
            "drawings": [{"id": "a"}],
            "allow_mass_delete": True,
            "deleted_ids": [],
        }
    )

    assert blocked_unexplained["ok"] is False
    assert "blocked unexplained drawing deletion" in blocked_unexplained["message"]
    assert allowed_specific["ok"] is True
    assert blocked_full_clear["ok"] is False
    assert (
        "blocked drawing full clear without explicit clear action" in blocked_full_clear["message"]
    )
    assert allowed_clear_button["ok"] is True
    assert store.replaced == [{"id": "a"}]


def test_server_replaces_drawings_without_backup_when_delete_is_explained() -> None:
    route = route_instrument(ibkr_future_payload("ES"))

    class Store:
        def __init__(self):
            self.settings = {}
            self.replaced = None

        def initialize(self):
            return None

        def read_drawings(self, instrument_id, interval, *, route_fingerprint):
            assert instrument_id == route.instrument_id
            assert route_fingerprint == route.fingerprint
            return [{"id": "a"}, {"id": "b"}]

        def replace_drawings(
            self,
            instrument_id,
            presentation_symbol,
            interval,
            drawings,
            **identity,
        ):
            assert instrument_id == route.instrument_id
            assert identity["route_fingerprint"] == route.fingerprint
            self.replaced = drawings

        def read_settings(self, scope):
            return dict(self.settings)

        def upsert_settings(self, scope, values):
            self.settings.update(values)

    store = Store()
    deps = _storage_deps(store_factory=lambda: store)
    endpoint = _route_endpoint(create_storage_router(deps), "/api/drawings", "PUT")

    result = endpoint(
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "interval": "5m",
            "drawings": [{"id": "a"}, {"id": "c"}],
            "allow_mass_delete": False,
            "deleted_ids": ["b"],
        }
    )

    assert result["ok"] is True
    assert result["instrument_id"] == route.instrument_id
    assert result["route_fingerprint"] == route.fingerprint
    assert result["provider"] == route.provider
    assert result["interval"] == "5m"
    assert store.replaced == [{"id": "a"}, {"id": "c"}]


def _drawing_bar(ts: datetime, price: float = 100.0) -> Bar:
    return Bar(
        symbol="BTC-USD",
        ts=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1.0,
        timeframe="5m",
        source="coinbase:historical",
        closed=True,
    )


class DrawingAnchorStore:
    def __init__(self, bars: list[Bar]) -> None:
        self.bars = {bar.ts: bar for bar in bars}
        self.confirmed_anchor_reads = 0
        self.logical_projection_reads = 0

    def read_bars(self, **kwargs):
        start = kwargs.get("start")
        end = kwargs.get("end")
        return [
            bar
            for timestamp, bar in sorted(self.bars.items())
            if (start is None or timestamp >= start) and (end is None or timestamp <= end)
        ][: kwargs.get("limit")]

    def read_bar_logical_projection(
        self,
        timeframe: str,
        provider: str,
        anchor_timestamps: list[datetime],
        *,
        instrument: dict[str, Any],
    ) -> ConfirmedBarLogicalProjection:
        del timeframe, provider, instrument
        self.logical_projection_reads += 1
        anchors = sorted(set(anchor_timestamps))
        confirmed = [
            timestamp
            for timestamp, bar in sorted(self.bars.items())
            if timestamp >= anchors[0] and bar.closed is True
        ]
        return ConfirmedBarLogicalProjection(
            origin_ts=confirmed[0],
            confirmed_through_ts=confirmed[-1],
            confirmed_count=len(confirmed),
            anchor_indices={timestamp: confirmed.index(timestamp) for timestamp in anchors},
        )

    def read_confirmed_bar_timestamps(
        self,
        timeframe: str,
        provider: str,
        anchor_timestamps: list[datetime],
        *,
        instrument: dict[str, Any],
    ) -> frozenset[datetime]:
        del timeframe, provider, instrument
        self.confirmed_anchor_reads += 1
        return frozenset(
            timestamp
            for timestamp in set(anchor_timestamps)
            if (bar := self.bars.get(timestamp)) is not None and bar.closed is True
        )


def test_drawing_projection_rejects_commit_after_anchor_membership_read() -> None:
    route = route_instrument(coinbase_btc_payload())
    first = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    second = datetime(2026, 4, 2, 12, 5, tzinfo=UTC)
    seen = chart_events.chart_bars_updated_generation(
        "5m",
        route.fingerprint,
        instrument_id=route.instrument_id,
    )

    class RacingStore(DrawingAnchorStore):
        lease: Any | None = None

        def read_confirmed_bar_timestamps(self, *args: Any, **kwargs: Any):
            confirmed = super().read_confirmed_bar_timestamps(*args, **kwargs)
            self.lease = chart_events.begin_chart_bars_write(
                seen,
                "5m",
                route.fingerprint,
                instrument_id=route.instrument_id,
            )
            return confirmed

    store = RacingStore([_drawing_bar(first), _drawing_bar(second)])
    try:
        with pytest.raises(chart_events.ChartBarsGenerationChanged) as raised:
            normalize_drawing_anchors(
                store,
                route.instrument_id,
                "5m",
                [
                    {
                        "id": "race-line",
                        "type": "line",
                        "points": [
                            {"ts": first.isoformat(), "price": 100.0},
                            {"ts": second.isoformat(), "price": 101.0},
                        ],
                    }
                ],
                instrument=route.instrument,
                provider=route.provider,
                route_fingerprint=route.fingerprint,
            )
        assert raised.value.write_in_progress is True
        assert store.logical_projection_reads == 0
    finally:
        if store.lease is not None:
            chart_events.end_chart_bars_write(store.lease)


def test_logical_projection_rejects_cumulative_index_when_exact_anchor_disappeared() -> None:
    anchor = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    earlier = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            # One earlier row contributes a cumulative index, but ts=anchor is absent.
            return earlier, earlier, 1, 1, 0

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    class Store:
        def _connect(self):
            return Connection()

    with pytest.raises(ValueError, match="DRAWING_ANCHOR_PROJECTION_INVALID"):
        read_confirmed_bar_logical_projection(
            Store(),
            table="bars",
            route_conditions=("instrument_id = %s",),
            route_params=("instrument-a",),
            timeframe="5m",
            anchor_timestamps=[anchor],
        )


@pytest.mark.parametrize(
    "anchors, confirmed_through",
    [
        (
            [(0, 0), (5, 5), (10, 3)],
            10,
        ),
        (
            [(0, 0), (5, 1), (15, 2)],
            10,
        ),
    ],
)
def test_drawing_anchor_projection_rejects_noncanonical_anchor_order(
    anchors: list[tuple[int, int]],
    confirmed_through: int,
) -> None:
    origin = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    minute = 60

    with pytest.raises(ValueError, match="DRAWING_ANCHOR_PROJECTION_INVALID: anchor order"):
        parse_drawing_anchor_projection(
            {
                "instrumentId": "instrument-a",
                "routeFingerprint": "route-a",
                "timeframe": "5m",
                "canonicalGeneration": 7,
                "originTs": origin.isoformat(),
                "confirmedThroughTs": datetime.fromtimestamp(
                    origin.timestamp() + confirmed_through * minute,
                    tz=UTC,
                ).isoformat(),
                "confirmedCount": 10,
                "anchors": [
                    {
                        "ts": datetime.fromtimestamp(
                            origin.timestamp() + offset * minute,
                            tz=UTC,
                        ).isoformat(),
                        "logicalIndex": logical_index,
                    }
                    for offset, logical_index in anchors
                ],
            }
        )


@pytest.mark.parametrize(
    "point, expected",
    [
        (
            {"ts": "2026-07-01T15:00:00+03:00", "price": 100},
            {"ts": "2026-07-01T12:00:00+00:00", "price": 100.0},
        ),
        (
            {
                "anchorTs": "2026-07-01T12:00:00Z",
                "barOffset": 3,
                "price": 120,
            },
            {
                "anchorTs": "2026-07-01T12:00:00+00:00",
                "barOffset": 3,
                "price": 120.0,
            },
        ),
    ],
)
def test_canonical_drawing_point_normalizes_utc_and_price_shape(
    point: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    assert canonical_drawing_point(point) == expected


def test_confirmed_anchor_lookup_is_one_bounded_exact_query() -> None:
    first = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    second = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    calls: list[tuple[str, list[Any]]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            calls.append((sql, params))

        def fetchall(self):
            return [(first,)]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    class Store:
        def _connect(self):
            return Connection()

    confirmed = read_confirmed_bar_timestamps(
        Store(),
        table="bars",
        route_conditions=("instrument_id = %s",),
        route_params=("instrument-a",),
        timeframe="5m",
        anchor_timestamps=[first, second, first],
    )

    assert confirmed == frozenset({first})
    assert len(calls) == 1
    assert "ts = ANY(%s::timestamptz[])" in calls[0][0]
    assert calls[0][1][-1] == [first, second]


def test_virtual_three_minute_anchor_queries_require_complete_one_minute_triplets() -> None:
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    calls: list[tuple[str, list[Any]]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            calls.append((sql, list(params)))

        def fetchall(self):
            return [(anchor,)]

        def fetchone(self):
            return anchor, anchor, 1, 1, 1

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    class Store:
        def _connect(self):
            return Connection()

    confirmed = read_confirmed_bar_timestamps(
        Store(),
        table="bars",
        route_conditions=("instrument_id = %s",),
        route_params=("instrument-a",),
        timeframe="1m",
        derived_timeframe="3m",
        anchor_timestamps=[anchor],
    )
    projection = read_confirmed_bar_logical_projection(
        Store(),
        table="bars",
        route_conditions=("instrument_id = %s",),
        route_params=("instrument-a",),
        timeframe="1m",
        derived_timeframe="3m",
        anchor_timestamps=[anchor],
    )

    assert confirmed == frozenset({anchor})
    assert projection.anchor_indices == {anchor: 0}
    assert len(calls) == 2
    assert all(
        "HAVING COUNT(*) = %s AND MIN(ts) = bucket_ts" in sql
        and "MAX(ts) = bucket_ts + (%s * interval '1 second')" in sql
        and "BOOL_AND(ts = to_timestamp(floor(extract(epoch FROM ts) / %s) * %s))" in sql
        for sql, _params in calls
    )
    assert calls[0][1][-5:-1] == [3, 120, 60, 60]
    assert calls[1][1][-6:-2] == [3, 120, 60, 60]


def test_drawing_anchor_projection_rejects_unbounded_path_before_storage() -> None:
    route = route_instrument(coinbase_btc_payload())
    first = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    points = [
        {
            "ts": first.replace(minute=0).isoformat(),
            "price": float(index),
        }
        for index in range(MAX_CONFIRMED_BAR_PROJECTION_ANCHORS + 1)
    ]

    with pytest.raises(ValueError, match="DRAWING_ANCHOR_LIMIT_EXCEEDED"):
        normalize_drawing_anchors(
            DrawingAnchorStore([]),
            route.instrument_id,
            "5m",
            [{"id": "oversized-path", "type": "path", "points": points}],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_server_normalizes_confirmed_drawing_anchors_and_attaches_projection() -> None:
    route = route_instrument(coinbase_btc_payload())
    first = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    second = datetime(2026, 4, 1, 12, 10, tzinfo=UTC)
    latest = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    drawing = {
        "id": "long-lived-channel",
        "type": "channel",
        "points": [
            {"ts": first.isoformat(), "price": 100.0},
            {"ts": second.isoformat(), "price": 110.0},
        ],
        "offsetPoint": {
            "ts": second.isoformat(),
            "price": 125.0,
        },
        "geometryIntegrity": {"status": "compromised"},
        "channelIntegrity": {"status": "compromised"},
        "alertOnCross": True,
        "alertArmed": True,
    }

    store = DrawingAnchorStore(
        [
            _drawing_bar(first),
            _drawing_bar(second),
            _drawing_bar(latest),
        ]
    )
    [normalized] = normalize_drawing_anchors(
        store,
        route.instrument_id,
        "5m",
        [drawing],
        instrument=route.instrument,
        provider=route.provider,
        route_fingerprint=route.fingerprint,
    )

    assert normalized["points"] == [
        {"ts": first.isoformat(), "price": 100.0},
        {"ts": second.isoformat(), "price": 110.0},
    ]
    assert normalized["offsetPoint"] == {
        "ts": second.isoformat(),
        "price": 125.0,
    }
    assert normalized["anchorResolution"]["status"] == "resolved"
    assert store.confirmed_anchor_reads == 1
    assert store.logical_projection_reads == 1
    assert normalized["anchorProjection"] == {
        "instrumentId": route.instrument_id,
        "routeFingerprint": route.fingerprint,
        "timeframe": "5m",
        "canonicalGeneration": 0,
        "originTs": first.isoformat(),
        "confirmedThroughTs": latest.isoformat(),
        "confirmedCount": 3,
        "anchors": [
            {"ts": first.isoformat(), "logicalIndex": 0},
            {"ts": second.isoformat(), "logicalIndex": 1},
        ],
    }
    for retired in (
        "geometryIntegrity",
        "channelIntegrity",
        "alertOnCross",
        "alertArmed",
    ):
        assert retired not in normalized


def test_server_rebases_projection_per_drawing_without_coverage_reads() -> None:
    route = route_instrument(coinbase_btc_payload())
    old_first = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    old_second = datetime(2026, 4, 1, 12, 5, tzinfo=UTC)
    new_first = datetime(2026, 7, 1, 11, 50, tzinfo=UTC)
    new_second = datetime(2026, 7, 1, 11, 55, tzinfo=UTC)
    latest = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    store = DrawingAnchorStore(
        [
            _drawing_bar(timestamp)
            for timestamp in (old_first, old_second, new_first, new_second, latest)
        ]
    )
    drawings = [
        {
            "id": "unrelated-old-line",
            "type": "line",
            "points": [
                {"ts": old_first.isoformat(), "price": 100.0},
                {"ts": old_second.isoformat(), "price": 110.0},
            ],
        },
        {
            "id": "new-channel",
            "type": "channel",
            "points": [
                {"ts": new_first.isoformat(), "price": 120.0},
                {"ts": new_second.isoformat(), "price": 125.0},
            ],
            "offsetPoint": {"ts": new_second.isoformat(), "price": 130.0},
        },
    ]

    old_line, new_channel = normalize_drawing_anchors(
        store,
        route.instrument_id,
        "5m",
        drawings,
        instrument=route.instrument,
        provider=route.provider,
        route_fingerprint=route.fingerprint,
    )

    assert store.confirmed_anchor_reads == 1
    assert store.logical_projection_reads == 1
    assert old_line["anchorProjection"]["originTs"] == old_first.isoformat()
    assert old_line["anchorProjection"]["confirmedCount"] == 5
    assert new_channel["anchorProjection"] == {
        "instrumentId": route.instrument_id,
        "routeFingerprint": route.fingerprint,
        "timeframe": "5m",
        "canonicalGeneration": 0,
        "originTs": new_first.isoformat(),
        "confirmedThroughTs": latest.isoformat(),
        "confirmedCount": 3,
        "anchors": [
            {"ts": new_first.isoformat(), "logicalIndex": 0},
            {"ts": new_second.isoformat(), "logicalIndex": 1},
        ],
    }
    assert "anchorCoverage" not in old_line
    assert "anchorCoverage" not in new_channel


def test_server_accepts_explicit_future_offset_from_confirmed_anchor() -> None:
    route = route_instrument(coinbase_btc_payload())
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    drawing = {
        "id": "future-line",
        "type": "line",
        "points": [
            {"ts": anchor.isoformat(), "price": 100.0},
            {"anchorTs": anchor.isoformat(), "barOffset": 9, "price": 120.0},
        ],
    }

    [normalized] = normalize_drawing_anchors(
        DrawingAnchorStore([_drawing_bar(anchor)]),
        route.instrument_id,
        "5m",
        [drawing],
        instrument=route.instrument,
        provider=route.provider,
        route_fingerprint=route.fingerprint,
        require_resolved=True,
    )

    assert normalized["points"][1] == {
        "anchorTs": anchor.isoformat(),
        "barOffset": 9,
        "price": 120.0,
    }
    assert "anchorProjection" not in normalized


def test_server_rejects_legacy_future_point() -> None:
    route = route_instrument(coinbase_btc_payload())
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    drawing = {
        "id": "legacy-future-line",
        "type": "line",
        "points": [
            {"ts": "2026-07-01T15:00:00+03:00", "barSlot": 100, "price": 100},
            {
                "ts": "2026-07-01T15:15:00+03:00",
                "barSlot": 115,
                "future": True,
                "price": 120,
            },
        ],
    }

    with pytest.raises(ValueError, match="DRAWING_FIELD_FORBIDDEN: barSlot"):
        normalize_drawing_anchors(
            DrawingAnchorStore([_drawing_bar(anchor)]),
            route.instrument_id,
            "5m",
            [drawing],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_server_rejects_legacy_channel_points() -> None:
    route = route_instrument(coinbase_btc_payload())
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    future = datetime(2026, 7, 1, 12, 15, tzinfo=UTC)
    offset_future = datetime(2026, 7, 1, 12, 20, tzinfo=UTC)
    drawing = {
        "id": "legacy-future-channel",
        "type": "channel",
        "points": [
            {"ts": anchor.isoformat(), "barSlot": 100, "price": 100.0},
            {
                "ts": future.isoformat(),
                "barSlot": 115,
                "future": True,
                "price": 120.0,
            },
        ],
        "offsetPoint": {
            "ts": offset_future.isoformat(),
            "barSlot": 120,
            "future": True,
            "price": 130.0,
        },
    }

    with pytest.raises(ValueError, match="DRAWING_FIELD_FORBIDDEN: barSlot"):
        normalize_drawing_anchors(
            DrawingAnchorStore([_drawing_bar(anchor)]),
            route.instrument_id,
            "5m",
            [drawing],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_server_rejects_legacy_future_before_anchor() -> None:
    route = route_instrument(coinbase_btc_payload())
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    future = datetime(2026, 7, 1, 12, 15, tzinfo=UTC)
    drawing = {
        "id": "legacy-future-before-anchor",
        "type": "line",
        "points": [
            {
                "ts": future.isoformat(),
                "barSlot": 115,
                "future": True,
                "price": 120.0,
            },
            {"ts": anchor.isoformat(), "barSlot": 100, "price": 100.0},
        ],
    }

    with pytest.raises(ValueError, match="DRAWING_FIELD_FORBIDDEN: barSlot"):
        normalize_drawing_anchors(
            DrawingAnchorStore([_drawing_bar(anchor)]),
            route.instrument_id,
            "5m",
            [drawing],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_server_rejects_legacy_future_with_ambiguous_middle_anchor() -> None:
    route = route_instrument(coinbase_btc_payload())
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    ambiguous = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    future = datetime(2026, 7, 1, 12, 15, tzinfo=UTC)
    drawing = {
        "id": "legacy-future-non-adjacent-anchor",
        "type": "path",
        "points": [
            {"ts": anchor.isoformat(), "barSlot": 100, "price": 100.0},
            {"ts": ambiguous.isoformat(), "barSlot": 105, "price": 105.0},
            {
                "ts": future.isoformat(),
                "barSlot": 115,
                "future": True,
                "price": 120.0,
            },
        ],
    }

    with pytest.raises(ValueError, match="DRAWING_FIELD_FORBIDDEN: barSlot"):
        normalize_drawing_anchors(
            DrawingAnchorStore([_drawing_bar(anchor)]),
            route.instrument_id,
            "5m",
            [drawing],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_server_marks_missing_canonical_anchor_unresolved_and_strict_save_fails() -> None:
    route = route_instrument(coinbase_btc_payload())
    anchor = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    missing = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    drawing = {
        "id": "ambiguous-line",
        "type": "line",
        "points": [
            {"ts": anchor.isoformat(), "price": 100.0},
            {"ts": missing.isoformat(), "price": 105.0},
        ],
    }
    store = DrawingAnchorStore([_drawing_bar(anchor)])

    [loaded] = normalize_drawing_anchors(
        store,
        route.instrument_id,
        "5m",
        [drawing],
        instrument=route.instrument,
        provider=route.provider,
        route_fingerprint=route.fingerprint,
    )
    assert loaded["anchorResolution"] == {
        "status": "needs_history",
        "reasons": ["confirmed_anchor_not_found"],
    }
    assert "anchorProjection" not in loaded

    with pytest.raises(ValueError, match="DRAWING_ANCHOR_UNRESOLVED"):
        normalize_drawing_anchors(
            store,
            route.instrument_id,
            "5m",
            [drawing],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
            require_resolved=True,
        )


def test_server_rejects_legacy_fields_even_when_timestamp_is_confirmed() -> None:
    route = route_instrument(coinbase_btc_payload())
    first = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    second = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    drawing = {
        "id": "confirmed-line",
        "type": "line",
        "points": [
            {"ts": first.isoformat(), "barSlot": 100, "price": 100.0},
            {
                "ts": second.isoformat(),
                "barSlot": 105,
                "future": True,
                "price": 105.0,
            },
        ],
    }

    with pytest.raises(ValueError, match="DRAWING_FIELD_FORBIDDEN: barSlot"):
        normalize_drawing_anchors(
            DrawingAnchorStore([_drawing_bar(first), _drawing_bar(second)]),
            route.instrument_id,
            "5m",
            [drawing],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_server_rejects_malformed_drawing_anchor_union() -> None:
    route = route_instrument(coinbase_btc_payload())
    timestamp = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    malformed = {
        "id": "bad-line",
        "type": "line",
        "points": [
            {"ts": timestamp.isoformat(), "price": 100.0},
            {
                "ts": timestamp.isoformat(),
                "anchorTs": timestamp.isoformat(),
                "barOffset": 1,
                "price": 101.0,
            },
        ],
    }

    with pytest.raises(ValueError, match="DRAWING_FIELD_INVALID: bad-line anchor"):
        normalize_drawing_anchors(
            DrawingAnchorStore([_drawing_bar(timestamp)]),
            route.instrument_id,
            "5m",
            [malformed],
            instrument=route.instrument,
            provider=route.provider,
            route_fingerprint=route.fingerprint,
        )


def test_price_alert_command_storage_unavailable() -> None:
    route = route_instrument(ibkr_future_payload("ES"))
    endpoint = _route_endpoint(
        create_storage_router(_storage_deps()), "/api/alerts/command", "POST"
    )
    payload = endpoint(
        {
            "action": "delete_scope",
            "payload": {
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "timeframe": "5m",
            },
        }
    )
    assert payload["ok"] is False
    assert payload["code"] == "ALERT_STORAGE_NOT_CONFIGURED"


def test_drawing_backup_routes_are_removed() -> None:
    router = create_storage_router(_storage_deps())
    paths = {getattr(route, "path", "") for route in router.routes}

    assert "/api/drawings/backups" not in paths
    assert "/api/drawings/backup" not in paths
    assert "/api/drawings/restore" not in paths
