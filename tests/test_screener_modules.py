from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.ui.screener_bases import load_screener_bases, screener_bar_limit
from aef_terminal.ui.screener_change import previous_session_close_from_bars, watchlist_change_base
from aef_terminal.ui.screener_rows import screener_rows
from aef_terminal.ui.quote_stream_contract import project_quote_stream_row
from aef_terminal.ui import screener_services
from aef_terminal.ui.screener_trends import (
    WATCHLIST_TREND_BUCKET_MINUTES,
    WATCHLIST_TREND_MAX_POINTS,
    WATCHLIST_TREND_WINDOW_MINUTES,
    load_watchlist_trend_snapshot,
)
from tests.provider_payloads import ibkr_future_payload


@pytest.fixture(autouse=True)
def _reset_watchlist_trend_service_cache(monkeypatch):
    monkeypatch.setattr(screener_services, "_watchlist_trend_lock", asyncio.Lock())
    screener_services._watchlist_trend_cache.clear()
    screener_services._watchlist_trend_inflight.clear()
    screener_services.start_screener_trend_runtime()
    yield
    screener_services._watchlist_trend_cache.clear()
    screener_services._watchlist_trend_inflight.clear()
    screener_services.start_screener_trend_runtime()


def identity(key: str = "RISK", provider: str = "ibkr", route: str | None = None) -> dict:
    provider_symbol = route or key
    contract_id = f"{provider}:{provider_symbol}"
    payload = {
        "instrument_id": f"{provider}|contract|{contract_id}",
        "key": key,
        "instrument_key": key,
        "display": key,
        "name": key,
        "provider": provider,
        "provider_symbol": provider_symbol,
        "provider_contract_id": contract_id,
        "asset_class": "index",
        "contract_identity": {
            "provider": provider,
            "provider_contract_id": contract_id,
            "asset_class": "index",
        },
    }
    resolved = route_instrument(payload)
    payload["route_fingerprint"] = resolved.fingerprint
    return payload


def test_screener_change_uses_previous_futures_session_close() -> None:
    bars = [
        Bar(
            "ES",
            datetime(2026, 5, 29, 20, 55, tzinfo=UTC),
            7595.0,
            7596.0,
            7594.0,
            7595.75,
            1000,
            "5m",
            "ibkr",
        ),
        Bar(
            "ES",
            datetime(2026, 5, 31, 22, 0, tzinfo=UTC),
            7595.0,
            7597.0,
            7585.25,
            7586.75,
            1000,
            "5m",
            "ibkr",
        ),
        Bar(
            "ES",
            datetime(2026, 6, 1, 6, 50, tzinfo=UTC),
            7616.0,
            7617.0,
            7615.0,
            7616.25,
            1000,
            "5m",
            "ibkr",
        ),
    ]

    class Store:
        def read_trading_session_intervals(self, *_args, **_kwargs):
            return [
                {
                    "opens_at": datetime(2026, 5, 31, 22, 0, tzinfo=UTC).isoformat(),
                    "closes_at": datetime(2026, 6, 1, 21, 0, tzinfo=UTC).isoformat(),
                }
            ]

    assert previous_session_close_from_bars(ibkr_future_payload("ES"), bars, Store()) == 7595.75
    assert (
        watchlist_change_base(
            quote_close_base=None,
            previous_session_close=7595.75,
            previous=bars[-2],
            latest=bars[-1],
        )
        == 7595.75
    )


def test_quote_and_change_bases_preserve_signed_zero_and_reject_invalid_values() -> None:
    instrument = identity()
    adapter = route_instrument(instrument).adapter
    assert adapter.quote_close_base(instrument, {"close": 0.0}) == 0.0
    assert adapter.quote_close_base(instrument, {"close": -12.5}) == -12.5
    assert adapter.quote_close_base(instrument, {"close": True}) is None
    assert adapter.quote_close_base(instrument, {"close": float("nan")}) is None

    assert (
        watchlist_change_base(
            quote_close_base=0.0,
            previous_session_close=10.0,
            previous=Bar(
                "RISK",
                datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
                8.0,
                9.0,
                7.0,
                8.0,
                1.0,
            ),
            latest=Bar(
                "RISK",
                datetime(2026, 8, 16, 10, 1, tzinfo=UTC),
                7.0,
                8.0,
                6.0,
                7.0,
                1.0,
            ),
        )
        == 0.0
    )
    assert (
        watchlist_change_base(
            quote_close_base=True,
            previous_session_close=float("inf"),
            previous=None,
            latest=Bar(
                "RISK",
                datetime(2026, 8, 16, 10, 1, tzinfo=UTC),
                -3.0,
                -2.0,
                -4.0,
                -3.0,
                1.0,
            ),
        )
        == -3.0
    )


def test_screener_change_rejects_noncanonical_or_provisional_bar_rows() -> None:
    instrument = ibkr_future_payload("ES")

    with pytest.raises(TypeError, match="only Bar"):
        previous_session_close_from_bars(
            instrument,
            [SimpleNamespace(closed=True)],  # type: ignore[list-item]
            object(),
        )

    provisional = Bar(
        "ES",
        datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        100.0,
        101.0,
        99.0,
        100.0,
        1.0,
        "5m",
        "ibkr",
        closed=False,
    )
    with pytest.raises(ValueError, match="only confirmed"):
        previous_session_close_from_bars(instrument, [provisional], object())


def test_screener_bases_no_longer_loads_or_emits_legacy_trend_context() -> None:
    instrument = identity()
    route = route_instrument(instrument)
    base_ts = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
    bars = [
        Bar(
            "RISK",
            base_ts + timedelta(minutes=15 * index),
            30.0 + index,
            30.5 + index,
            29.5 + index,
            30.0 + index,
            1000,
            "15m",
            "ibkr",
        )
        for index in range(2)
    ]
    calls: list[tuple[str, str, int]] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_recent_bars_batch(self, requests):
            for interval, provider, limit, _instrument in requests:
                calls.append((interval, provider, limit))
            return {(route.instrument_id, "15m"): bars}

        def read_trading_session_intervals(self, *_args, **_kwargs):
            return []

    bases = load_screener_bases([instrument], "15m", Store())

    assert calls == [("15m", "ibkr", screener_bar_limit("15m"))]
    assert "trend_points" not in bases[route.instrument_id]


def test_screener_bases_preserve_empty_futures_history_as_available_row() -> None:
    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    route = route_instrument(instrument)

    class Store:
        def read_recent_bars_batch(self, _requests):
            return {(route.instrument_id, "5m"): []}

    bases = load_screener_bases([instrument], "5m", Store())
    rows = screener_rows([instrument], bases, {}, interval="5m")

    assert bases == {
        route.instrument_id: {
            "latest": None,
            "previous": None,
            "previous_session_close": None,
        }
    }
    assert len(rows) == 1
    assert rows[0]["instrument_id"] == route.instrument_id
    assert rows[0]["change"] is None
    assert "No cached" in rows[0]["warning"]
    assert "CL 5m" in rows[0]["warning"]


def test_screener_quote_projection_uses_market_data_entitlement_and_exact_flags() -> None:
    instrument = identity("BTC", provider="coinbase", route="BTC-USD")
    route = route_instrument(instrument)
    quote = {
        "price": 100.0,
        "bid": 99.0,
        "ask": 101.0,
        "last": 100.0,
        "status": "delayed",
        "market_data_entitlement": "delayed",
        "entitlement": "public",
        "is_delayed": True,
        "is_stale": False,
        "last_status": "delayed",
        "bid_ask_status": "delayed",
        "source": "coinbase:quote-live",
    }

    row = screener_rows([instrument], {}, {route.fingerprint: quote})[0]
    projected = project_quote_stream_row(row)

    assert projected["quote_entitlement"] == "delayed"
    assert projected["quote_is_delayed"] is True
    assert projected["quote_is_stale"] is False
    with pytest.raises(ValueError, match="SCREENER_QUOTE_BOOLEAN_INVALID"):
        screener_rows(
            [instrument],
            {},
            {route.fingerprint: {**quote, "is_delayed": 1}},
        )


def test_watchlist_trends_use_exact_routes_and_persisted_snapshot_buckets_only() -> None:
    first = identity("RISK")
    second = identity("EUR", route="EUR.USD")
    first_route = route_instrument(first)
    second_route = route_instrument(second)
    now = datetime(2026, 7, 21, 12, 2, tzinfo=UTC)
    calls: list[tuple] = []

    class Store:
        def read_quote_snapshot_buckets_batch(
            self,
            routes,
            start,
            end,
            *,
            bucket_minutes,
            max_points,
        ):
            calls.append((routes, start, end, bucket_minutes, max_points))
            return {
                (first_route.instrument_id, first_route.fingerprint): [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 18.1},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 18.2},
                ],
                (second_route.instrument_id, second_route.fingerprint): [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 1.17},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 1.18},
                ],
            }

    payload = load_watchlist_trend_snapshot([first, second], Store(), now=now)

    assert payload == {
        "window_minutes": 180,
        "bucket_minutes": 5,
        "rows": [
            {
                "instrument_id": first_route.instrument_id,
                "route_fingerprint": first_route.fingerprint,
                "status": "ready",
                "source": "quote_snapshots",
                "as_of": "2026-07-21T12:00:00+00:00",
                "points": [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 18.1},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 18.2},
                ],
            },
            {
                "instrument_id": second_route.instrument_id,
                "route_fingerprint": second_route.fingerprint,
                "status": "ready",
                "source": "quote_snapshots",
                "as_of": "2026-07-21T12:00:00+00:00",
                "points": [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 1.17},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 1.18},
                ],
            },
        ],
    }
    assert calls == [
        (
            [
                (first_route.provider, first_route.instrument_id, first_route.fingerprint),
                (second_route.provider, second_route.instrument_id, second_route.fingerprint),
            ],
            now - timedelta(minutes=WATCHLIST_TREND_WINDOW_MINUTES),
            now,
            WATCHLIST_TREND_BUCKET_MINUTES,
            WATCHLIST_TREND_MAX_POINTS,
        ),
    ]


def test_watchlist_trend_status_is_typed_when_persisted_data_is_unavailable() -> None:
    instrument = identity()
    route = route_instrument(instrument)

    unavailable = load_watchlist_trend_snapshot([instrument], None)
    assert unavailable["rows"] == [
        {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "status": "storage_unavailable",
            "source": "quote_snapshots",
            "as_of": None,
            "points": [],
        }
    ]

    class BrokenStore:
        def initialize(self) -> None:
            raise RuntimeError("database unavailable")

    failed = load_watchlist_trend_snapshot([instrument], BrokenStore())
    assert failed["rows"][0]["status"] == "storage_error"
    assert failed["rows"][0]["points"] == []


def test_screener_trends_service_resolves_exact_routes_without_live_dependencies(
    monkeypatch,
) -> None:
    instrument = identity()
    route = route_instrument(instrument)
    selected_calls: list[tuple[str, ...]] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_quote_snapshot_buckets_batch(self, routes, *_args, **_kwargs):
            assert routes == [(route.provider, route.instrument_id, route.fingerprint)]
            return {
                (route.instrument_id, route.fingerprint): [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 18.1},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 18.2},
                ]
            }

    def selected_instruments(instrument_ids):
        selected_calls.append(tuple(instrument_ids))
        return [instrument]

    monkeypatch.setattr(
        screener_services,
        "_DEPS",
        SimpleNamespace(
            store_factory=Store,
            selected_instruments=selected_instruments,
        ),
    )
    payload = asyncio.run(
        screener_services.screener_trends_snapshot(
            json.dumps(
                [
                    {
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                    }
                ]
            )
        )
    )

    assert selected_calls == [(route.instrument_id,)]
    assert payload["rows"][0]["instrument_id"] == route.instrument_id
    assert payload["rows"][0]["route_fingerprint"] == route.fingerprint


def test_watchlist_trend_owner_cancellation_settles_physical_storage_load(
    monkeypatch,
) -> None:
    instrument = identity()
    route = route_instrument(instrument)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    key = (1, ((route.instrument_id, route.fingerprint),))

    def blocking_load(*_args, **_kwargs) -> dict:
        started.set()
        assert release.wait(timeout=2)
        finished.set()
        return {"rows": []}

    monkeypatch.setattr(screener_services, "load_watchlist_trend_snapshot", blocking_load)

    async def scenario() -> None:
        task = asyncio.create_task(
            screener_services._build_watchlist_trend_snapshot(
                key,
                [instrument],
                datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
                object,
            )
        )
        screener_services._watchlist_trend_inflight[key] = task
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()
        await asyncio.sleep(0.01)
        assert task.done() is False
        assert screener_services._watchlist_trend_inflight[key] is task
        assert finished.is_set() is False

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() is True
        assert key not in screener_services._watchlist_trend_inflight

    asyncio.run(scenario())


def test_screener_trend_shutdown_drains_shielded_owner() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def load() -> dict[str, object]:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
                raise
            return {}

        key = (1, (("instrument", "route"),))
        task = asyncio.create_task(load())
        screener_services._watchlist_trend_inflight[key] = task
        await started.wait()
        shutdown = asyncio.create_task(screener_services.shutdown_screener_trend_runtime())
        await asyncio.sleep(0)

        assert not shutdown.done()
        release.set()
        await shutdown
        assert screener_services._watchlist_trend_inflight == {}
        screener_services.start_screener_trend_runtime()

    asyncio.run(scenario())


def test_screener_trends_coalesces_tabs_by_canonical_exact_route_set(monkeypatch) -> None:
    first = identity("RISK")
    second = identity("EUR", route="EUR.USD")
    instruments = {item["instrument_id"]: item for item in (first, second)}
    reads: list[list[tuple[str, str, str]]] = []
    selected_calls: list[tuple[str, ...]] = []

    class Store:
        def initialize(self) -> None:
            return None

        def read_quote_snapshot_buckets_batch(self, routes, *_args, **_kwargs):
            reads.append(list(routes))
            return {
                (route.instrument_id, route.fingerprint): [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 10.0},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 11.0},
                ]
                for route in map(route_instrument, (first, second))
            }

    def selected_instruments(instrument_ids):
        selected_calls.append(tuple(instrument_ids))
        return [instruments[instrument_id] for instrument_id in instrument_ids]

    monkeypatch.setattr(
        screener_services,
        "_DEPS",
        SimpleNamespace(store_factory=Store, selected_instruments=selected_instruments),
    )
    first_routes = [
        {
            "instrument_id": first["instrument_id"],
            "route_fingerprint": first["route_fingerprint"],
        },
        {
            "instrument_id": second["instrument_id"],
            "route_fingerprint": second["route_fingerprint"],
        },
    ]

    async def load_both():
        return await asyncio.gather(
            screener_services.screener_trends_snapshot(json.dumps(first_routes)),
            screener_services.screener_trends_snapshot(json.dumps(list(reversed(first_routes)))),
        )

    payloads = asyncio.run(load_both())

    assert len(reads) == 1
    assert len(selected_calls) == 2
    assert payloads[0] == payloads[1]
    assert screener_services._watchlist_trend_inflight == {}
    assert len(screener_services._watchlist_trend_cache) == 1


def test_screener_trend_storage_failure_is_not_retained(monkeypatch) -> None:
    instrument = identity()
    route = route_instrument(instrument)
    current = datetime(2026, 7, 21, 12, 2, tzinfo=UTC)
    reads = 0

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return current

    class BrokenStore:
        def read_quote_snapshot_buckets_batch(self, *_args, **_kwargs):
            nonlocal reads
            reads += 1
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(screener_services, "datetime", Clock)
    monkeypatch.setattr(
        screener_services,
        "_DEPS",
        SimpleNamespace(
            store_factory=BrokenStore,
            selected_instruments=lambda _instrument_ids: [instrument],
        ),
    )
    routes = json.dumps(
        [
            {
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
            }
        ]
    )

    first = asyncio.run(screener_services.screener_trends_snapshot(routes))
    second = asyncio.run(screener_services.screener_trends_snapshot(routes))
    current = datetime(2026, 7, 21, 12, 6, tzinfo=UTC)
    third = asyncio.run(screener_services.screener_trends_snapshot(routes))

    assert [
        first["rows"][0]["status"],
        second["rows"][0]["status"],
        third["rows"][0]["status"],
    ] == [
        "storage_error",
        "storage_error",
        "storage_error",
    ]
    assert reads == 3
    assert screener_services._watchlist_trend_cache == {}
    assert screener_services._watchlist_trend_inflight == {}


def test_screener_trend_cache_rebuilds_at_next_utc_bucket(monkeypatch) -> None:
    instrument = identity()
    route = route_instrument(instrument)
    current = datetime(2026, 7, 21, 12, 2, tzinfo=UTC)
    reads = 0

    class Clock:
        @classmethod
        def now(cls, tz=None):
            return current

    class Store:
        def initialize(self) -> None:
            return None

        def read_quote_snapshot_buckets_batch(self, _routes, *_args, **_kwargs):
            nonlocal reads
            reads += 1
            return {
                (route.instrument_id, route.fingerprint): [
                    {"ts": "2026-07-21T11:55:00+00:00", "price": 18.1},
                    {"ts": "2026-07-21T12:00:00+00:00", "price": 18.2},
                ]
            }

    monkeypatch.setattr(screener_services, "datetime", Clock)
    monkeypatch.setattr(
        screener_services,
        "_DEPS",
        SimpleNamespace(
            store_factory=Store,
            selected_instruments=lambda _instrument_ids: [instrument],
        ),
    )
    routes = json.dumps(
        [
            {
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
            }
        ]
    )

    asyncio.run(screener_services.screener_trends_snapshot(routes))
    asyncio.run(screener_services.screener_trends_snapshot(routes))
    current = datetime(2026, 7, 21, 12, 6, tzinfo=UTC)
    asyncio.run(screener_services.screener_trends_snapshot(routes))

    assert reads == 2
    assert len(screener_services._watchlist_trend_cache) == 1


def test_regular_screener_rows_do_not_emit_or_mutate_trend_state() -> None:
    instrument = identity()
    route = route_instrument(instrument)
    rows = screener_rows(
        [instrument],
        {
            route.instrument_id: {
                "latest": None,
                "previous": None,
                "previous_session_close": None,
                "trend_points": [{"ts": "2026-07-21T12:00:00+00:00", "price": 1.0}],
            }
        },
        {route.fingerprint: {"price": 2.0, "close": 0.0, "last": 0.0}},
        interval="5m",
    )

    assert rows[0]["change_base"] == 0.0
    assert rows[0]["change"] == 2.0
    assert rows[0]["change_pct"] is None
    assert rows[0]["last"] == 0.0
    assert "trend_interval" not in rows[0]
    assert "trend_points" not in rows[0]
