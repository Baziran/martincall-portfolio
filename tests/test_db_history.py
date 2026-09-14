from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aef_terminal.domain import Bar, BarProviderRequest
from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.data.providers import (
    read_provider_bar_logical_projection,
    read_provider_confirmed_bar_timestamps,
    read_recent_provider_bars,
    read_recent_provider_bars_batch,
    route_instrument,
)
from aef_terminal.data.provider_contract import CanonicalHistoryRoute
from aef_terminal.data.provider_history import read_provider_history_page
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection
from aef_terminal.runtime.bar_quality import canonical_storage_bar_provenance
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


def _bar(symbol: str, ts: datetime, timeframe: str = "5m") -> Bar:
    return Bar(symbol, ts, 100, 101, 99, 100, 1, timeframe)


def test_read_recent_provider_bars_uses_canonical_futures_without_legacy_fallback() -> None:
    canonical = [_bar("CL", datetime(2026, 1, 1, 14, 0, tzinfo=UTC))]
    calls: list[tuple[str, str, str, int, bool]] = []

    class Store:
        def read_futures_canonical_bars(
            self, *, provider, instrument_id, timeframe, limit, latest, **_route
        ):
            calls.append((provider, instrument_id, timeframe, limit, latest))
            return canonical

        def read_recent_bars(self, **_kwargs):
            raise AssertionError("IBKR futures history must not read legacy bars")

    instrument = ibkr_future_payload("CL", exchange="NYMEX")
    bars = read_recent_provider_bars(route_instrument(instrument), "5m", store=Store(), limit=500)

    assert bars == canonical
    assert calls == [("ibkr", qualified_instrument_id(instrument), "5m", 500, True)]


def test_read_recent_provider_bars_returns_empty_when_canonical_futures_empty() -> None:
    class Store:
        def read_futures_canonical_bars(self, **_kwargs):
            return []

        def read_recent_bars(self, **_kwargs):
            raise AssertionError("empty canonical futures history must not fallback to legacy bars")

    assert (
        read_recent_provider_bars(
            route_instrument(ibkr_future_payload("CL", exchange="NYMEX")),
            "5m",
            store=Store(),
            limit=100,
        )
        == []
    )


def test_read_recent_provider_bars_requires_canonical_futures_reader() -> None:
    class Store:
        def read_recent_bars(self, **_kwargs):
            raise AssertionError("IBKR futures must fail instead of reading legacy bars")

    with pytest.raises(AttributeError, match="read_futures_canonical_bars"):
        read_recent_provider_bars(
            route_instrument(ibkr_future_payload("CL", exchange="NYMEX")),
            "5m",
            store=Store(),
            limit=100,
        )


def test_read_recent_provider_bars_uses_selected_route_for_non_futures() -> None:
    expected = [_bar("SPY", datetime(2026, 1, 1, 14, 0, tzinfo=UTC))]
    calls: list[tuple[str, str, int | None]] = []

    class Store:
        def read_futures_canonical_bars(self, **_kwargs):
            raise AssertionError("non-futures history must not read futures canonical bars")

        def read_recent_bars(self, *, timeframe, provider, limit, instrument):
            assert instrument == ibkr_stock_payload("SPY")
            calls.append((timeframe, provider, limit))
            return expected

    bars = read_recent_provider_bars(
        route_instrument(ibkr_stock_payload("SPY")),
        "5m",
        store=Store(),
        limit=50,
    )

    assert bars == expected
    assert calls == [("5m", "ibkr", 50)]


def test_history_page_reads_one_descending_storage_page_and_returns_ascending_bars() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    boundary = datetime(2026, 1, 1, 14, 20, tzinfo=UTC)
    source = [_bar("SPY", datetime(2026, 1, 1, 14, minute, tzinfo=UTC)) for minute in (5, 10, 15)]
    calls: list[dict[str, object]] = []

    class Store:
        def read_bars_multi(self, **kwargs):
            calls.append(kwargs)
            return {("ibkr", "5m"): source}

    page = read_provider_history_page(
        route,
        "5m",
        before_ts=boundary,
        limit=2,
        store=Store(),
    )

    assert calls[0]["timeframe_read_windows"] == {"5m": (None, boundary, 3)}
    assert [bar.ts.minute for bar in page.bars] == [10, 15]
    assert page.has_more is True
    assert page.next_before_ts == datetime(2026, 1, 1, 14, 10, tzinfo=UTC)


def test_virtual_three_minute_history_page_reads_bounded_one_minute_children() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    provenance = canonical_storage_bar_provenance(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    boundary = datetime(2026, 1, 1, 14, 9, tzinfo=UTC)
    source = [
        replace(
            _bar("SPY", datetime(2026, 1, 1, 14, minute, tzinfo=UTC), "1m"),
            provenance=provenance,
        )
        for minute in range(9)
    ]

    class Store:
        def read_bars_multi(self, **kwargs):
            assert kwargs["timeframe_read_windows"] == {"1m": (None, boundary, 18)}
            return {("ibkr", "1m"): source}

    page = read_provider_history_page(
        route,
        "3m",
        before_ts=boundary,
        limit=2,
        store=Store(),
    )

    assert [bar.ts.minute for bar in page.bars] == [3, 6]
    assert all(bar.timeframe == "3m" and bar.closed for bar in page.bars)
    assert page.has_more is True
    assert page.next_before_ts == datetime(2026, 1, 1, 14, 3, tzinfo=UTC)


def test_virtual_three_minute_recent_history_requires_complete_one_minute_triplets() -> None:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    provenance = canonical_storage_bar_provenance(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    source = [
        replace(
            _bar("SPY", datetime(2026, 1, 1, 14, minute, tzinfo=UTC), "1m"),
            provenance=provenance,
        )
        for minute in (0, 2, 3, 4, 5)
    ]
    calls: list[tuple[str, int | None]] = []

    class Store:
        def read_recent_bars(self, *, timeframe, limit, **_kwargs):
            calls.append((timeframe, limit))
            return source

    bars = read_recent_provider_bars(route, "3m", store=Store(), limit=2)

    assert calls == [("1m", 8)]
    assert [bar.ts.minute for bar in bars] == [3]
    assert all(bar.timeframe == "3m" and bar.closed for bar in bars)
    assert all(
        bar.provenance is not None
        and bar.provenance.request_type is BarProviderRequest.DETERMINISTIC_AGGREGATION
        and bar.provenance.provider_contract_id == "CANONICAL_STORAGE"
        for bar in bars
    )


def test_batch_recent_history_uses_one_storage_admission_and_projects_virtual_three_minute() -> (
    None
):
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    provenance = canonical_storage_bar_provenance(
        route.provider,
        route.instrument_id,
        route.fingerprint,
    )
    source = [
        replace(
            _bar("SPY", datetime(2026, 1, 1, 14, minute, tzinfo=UTC), "1m"),
            provenance=provenance,
        )
        for minute in range(6)
    ]
    calls: list[list[tuple[str, str, int, dict]]] = []

    class Store:
        def read_recent_bars_batch(self, requests):
            calls.append(requests)
            return {(route.instrument_id, "1m"): source}

    result = read_recent_provider_bars_batch(
        [(route, "3m", 2)],
        store=Store(),
    )

    assert calls == [[("1m", "ibkr", 8, route.instrument)]]
    assert [bar.ts.minute for bar in result[(route.instrument_id, "3m")]] == [0, 3]


def test_batch_recent_history_rejects_two_route_generations_for_one_scope() -> None:
    route = route_instrument(ibkr_stock_payload("SPY"))

    with pytest.raises(ValueError, match="PROVIDER_HISTORY_BATCH_ROUTE_MISMATCH"):
        read_recent_provider_bars_batch(
            [(route, "5m", 10), (replace(route, fingerprint="stale-route"), "5m", 20)],
            store=SimpleNamespace(
                read_recent_bars_batch=lambda _requests: (_ for _ in ()).throw(
                    AssertionError("mismatched routes must fail before storage")
                )
            ),
        )


def test_batch_recent_history_requires_exact_storage_scope_membership() -> None:
    route = route_instrument(ibkr_stock_payload("SPY"))

    with pytest.raises(RuntimeError, match="incomplete scope membership"):
        read_recent_provider_bars_batch(
            [(route, "5m", 10)],
            store=SimpleNamespace(read_recent_bars_batch=lambda _requests: {}),
        )


def test_batch_recent_history_rejects_provisional_or_unordered_storage_rows() -> None:
    route = route_instrument(ibkr_stock_payload("SPY"))
    first = _bar("SPY", datetime(2026, 1, 1, 14, 0, tzinfo=UTC))
    provisional = replace(first, closed=False, state="forming")

    with pytest.raises(ValueError, match="only confirmed"):
        read_recent_provider_bars_batch(
            [(route, "5m", 10)],
            store=SimpleNamespace(
                read_recent_bars_batch=lambda _requests: {
                    (route.instrument_id, "5m"): [provisional]
                }
            ),
        )

    with pytest.raises(ValueError, match="timestamp-ordered"):
        read_recent_provider_bars_batch(
            [(route, "5m", 10)],
            store=SimpleNamespace(
                read_recent_bars_batch=lambda _requests: {
                    (route.instrument_id, "5m"): [first, first]
                }
            ),
        )


@pytest.mark.parametrize("limit", [True, 0, "10"])
def test_recent_provider_history_rejects_noncanonical_limits(limit: object) -> None:
    route = route_instrument(ibkr_stock_payload("SPY"))
    store = SimpleNamespace(
        read_recent_bars=lambda **_kwargs: [],
        read_recent_bars_batch=lambda _requests: {},
    )

    with pytest.raises(ValueError, match="PROVIDER_HISTORY_LIMIT_INVALID"):
        read_recent_provider_bars(route, "5m", store=store, limit=limit)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="PROVIDER_HISTORY_BATCH_LIMIT_INVALID"):
        read_recent_provider_bars_batch(
            [(route, "5m", limit)],  # type: ignore[list-item]
            store=store,
        )


def test_virtual_three_minute_drawing_projection_groups_one_minute_storage() -> None:
    anchor = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    projection = ConfirmedBarLogicalProjection(
        origin_ts=anchor,
        confirmed_through_ts=anchor,
        confirmed_count=1,
        anchor_indices={anchor: 0},
    )
    calls: list[tuple[str, str | None]] = []

    class Store:
        def read_bar_logical_projection(self, timeframe, *, derived_timeframe=None, **_kwargs):
            calls.append((timeframe, derived_timeframe))
            return projection

        def read_confirmed_bar_timestamps(self, timeframe, *, derived_timeframe=None, **_kwargs):
            calls.append((timeframe, derived_timeframe))
            return frozenset({anchor})

    store = Store()
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)

    assert (
        read_provider_bar_logical_projection(route, "3m", store=store, anchor_timestamps=[anchor])
        is projection
    )
    assert read_provider_confirmed_bar_timestamps(
        route, "3m", store=store, anchor_timestamps=[anchor]
    ) == frozenset({anchor})
    assert calls == [("1m", "3m"), ("1m", "3m")]


def test_exact_futures_history_dispatches_by_typed_storage_kind() -> None:
    expected = [_bar("SBERF6", datetime(2026, 1, 1, 14, 0, tzinfo=UTC))]
    history_route = CanonicalHistoryRoute.exact_futures_contract("contract-uid")
    instrument = {"display": "SBERF6"}
    routed = SimpleNamespace(
        provider="tinvest",
        instrument_id="tinvest|contract|contract-uid",
        fingerprint="tinvest|contract|contract-uid",
        instrument_key="SBERF6",
        instrument=instrument,
        adapter=SimpleNamespace(
            canonical_history_route=lambda _instrument: history_route,
        ),
    )
    calls: list[dict[str, object]] = []

    class Store:
        def read_futures_contract_bars(self, **kwargs):
            calls.append(kwargs)
            return expected

        def read_futures_canonical_bars(self, **_kwargs):
            raise AssertionError("exact contract must not read continuous futures storage")

    assert read_recent_provider_bars(routed, "5m", store=Store(), limit=25) == expected
    assert [call["contract_key"] for call in calls] == ["contract-uid"]
    assert [call.get("latest") for call in calls] == [True]
    assert all("series_type" not in call and "roll_policy" not in call for call in calls)
