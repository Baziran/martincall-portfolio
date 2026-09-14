from __future__ import annotations

import json

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.reference_watchlist_item import selected_instruments
from aef_terminal.ui.route_selection import (
    MAX_ROUTE_SELECTIONS,
    RouteSelectionError,
    RouteSelectionMismatch,
    parse_route_selection,
    resolve_route_selection,
    route_selection_json,
)
from tests.provider_payloads import ibkr_stock_payload


def test_route_query_preserves_opaque_pairs_order_and_deduplicates_exact_pairs() -> None:
    payload = [
        {"instrument_id": " provider|contract|A,B ", "route_fingerprint": " route|1,2 "},
        {"instrument_id": "provider|contract|C", "route_fingerprint": "route|3"},
        {"instrument_id": " provider|contract|A,B ", "route_fingerprint": " route|1,2 "},
    ]

    routes = parse_route_selection(json.dumps(payload))

    assert [route.identity for route in routes] == [
        (" provider|contract|A,B ", " route|1,2 "),
        ("provider|contract|C", "route|3"),
    ]
    assert json.loads(route_selection_json(routes)) == payload[:2]


@pytest.mark.parametrize(
    "payload",
    [
        "instrument-a,instrument-b",
        "[]",
        '[{"instrument_id":"instrument-a","route_fingerprint":""}]',
        '[{"instrument_id":"instrument-a","route_fingerprint":"route-a","extra":true}]',
    ],
)
def test_route_query_rejects_legacy_or_untyped_payloads(payload: str) -> None:
    with pytest.raises(RouteSelectionError) as exc_info:
        parse_route_selection(payload)
    assert exc_info.value.code


def test_route_query_rejects_conflicting_identity_and_oversized_selection() -> None:
    with pytest.raises(ValueError, match="ROUTE_INSTRUMENT_CONFLICT"):
        parse_route_selection(
            json.dumps(
                [
                    {"instrument_id": "instrument-a", "route_fingerprint": "route-a"},
                    {"instrument_id": "instrument-a", "route_fingerprint": "route-b"},
                ]
            )
        )

    with pytest.raises(ValueError, match="ROUTES_LIMIT_EXCEEDED"):
        parse_route_selection(
            json.dumps(
                [
                    {
                        "instrument_id": f"instrument-{index}",
                        "route_fingerprint": f"route-{index}",
                    }
                    for index in range(MAX_ROUTE_SELECTIONS + 1)
                ]
            )
        )


def test_route_selection_resolves_in_requested_pair_order_and_rejects_pair_mismatch() -> None:
    first = ibkr_stock_payload("SPY", con_id=756733)
    second = ibkr_stock_payload("QQQ", con_id=320227571)
    first_route = route_instrument(first)
    second_route = route_instrument(second)
    requested = parse_route_selection(
        json.dumps(
            [
                {
                    "instrument_id": second_route.instrument_id,
                    "route_fingerprint": second_route.fingerprint,
                },
                {
                    "instrument_id": first_route.instrument_id,
                    "route_fingerprint": first_route.fingerprint,
                },
            ]
        )
    )
    watchlist = [first, second]
    seen_ids: list[tuple[str, ...]] = []

    def select(instrument_ids):
        seen_ids.append(tuple(instrument_ids))
        return selected_instruments(watchlist, instrument_ids)

    resolved = resolve_route_selection(select, requested)
    assert [
        (route.instrument_id, route.fingerprint) for route in map(route_instrument, resolved)
    ] == [
        (second_route.instrument_id, second_route.fingerprint),
        (first_route.instrument_id, first_route.fingerprint),
    ]
    assert seen_ids == [(second_route.instrument_id, first_route.instrument_id)]

    mismatched = parse_route_selection(
        json.dumps(
            [
                {
                    "instrument_id": second_route.instrument_id,
                    "route_fingerprint": first_route.fingerprint,
                }
            ]
        )
    )
    with pytest.raises(RouteSelectionMismatch) as mismatch_info:
        resolve_route_selection(select, mismatched)
    assert mismatch_info.value.code == "ROUTE_SELECTION_MISMATCH"
    assert mismatch_info.value.expected == ((second_route.instrument_id, first_route.fingerprint),)
    assert mismatch_info.value.actual == ((second_route.instrument_id, second_route.fingerprint),)

    missing = parse_route_selection(
        '[{"instrument_id":"missing-id","route_fingerprint":"missing-route"}]'
    )
    with pytest.raises(RouteSelectionMismatch):
        resolve_route_selection(
            lambda instrument_ids: selected_instruments(watchlist, instrument_ids), missing
        )
