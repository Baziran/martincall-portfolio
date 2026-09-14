from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aef_terminal.paper_contract import PaperContractIdentity
from aef_terminal.ui.paper.constants import PAPER_JOURNAL_RECENT_HOURS
from aef_terminal.ui.paper.journal_queries import (
    PaperJournalQueryDeps,
    paper_orders_payload_from_store,
    paper_trades_payload_from_store,
)
from aef_terminal.ui.paper.positions import paper_journal_payload


def test_paper_journal_payload_limits_orders_and_fills_to_recent_window() -> None:
    instrument_id = "ibkr|contract|123"
    route_fingerprint = "route-1"
    now = datetime.now(tz=UTC)
    old = now - timedelta(hours=PAPER_JOURNAL_RECENT_HOURS + 2)
    recent = now - timedelta(hours=1)

    class Store:
        def read_paper_positions(
            self,
            limit=1000,
            status=None,
            *,
            instrument_id=None,
            route_fingerprint=None,
            complete_open=False,
            contract_scope_kind=None,
        ):
            if instrument_id is None:
                assert status == "open"
                assert complete_open is True
                assert route_fingerprint is None
                return []
            assert instrument_id == "ibkr|contract|123"
            assert route_fingerprint == "route-1"
            assert contract_scope_kind == "instrument"
            return []

        def read_paper_orders(
            self,
            limit=500,
            status=None,
            *,
            instrument_id=None,
            route_fingerprint=None,
            since=None,
            contract_scope_kind=None,
        ):
            assert instrument_id == "ibkr|contract|123"
            assert route_fingerprint == "route-1"
            assert contract_scope_kind == "instrument"
            rows = [
                {
                    "id": "ord-old",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "status": "filled",
                    "created_at": old.isoformat(),
                    "updated_at": old.isoformat(),
                    "filled_at": old.isoformat(),
                },
                {
                    "id": "ord-new",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "status": "pending",
                    "created_at": recent.isoformat(),
                    "updated_at": recent.isoformat(),
                },
            ]
            if since is None:
                return rows
            since_dt = since if isinstance(since, datetime) else datetime.fromisoformat(str(since))
            return [
                row
                for row in rows
                if datetime.fromisoformat(
                    str(row.get("filled_at") or row.get("updated_at") or row.get("created_at"))
                )
                >= since_dt
            ]

        def read_paper_fills(
            self,
            limit=1000,
            *,
            instrument_id=None,
            route_fingerprint=None,
            since=None,
            contract_scope_kind=None,
        ):
            assert instrument_id == "ibkr|contract|123"
            assert route_fingerprint == "route-1"
            assert contract_scope_kind == "instrument"
            rows = [
                {"id": "fill-old", "symbol": "ES", "timeframe": "5m", "filled_at": old.isoformat()},
                {
                    "id": "fill-new",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "filled_at": recent.isoformat(),
                },
            ]
            if since is None:
                return rows
            since_dt = since if isinstance(since, datetime) else datetime.fromisoformat(str(since))
            return [
                row for row in rows if datetime.fromisoformat(str(row["filled_at"])) >= since_dt
            ]

    class Deps:
        @staticmethod
        def paper_trade_enriched(trade: dict[str, Any]) -> dict[str, Any]:
            return trade

        @staticmethod
        def paper_trade_stats(_trades: list[dict[str, Any]]) -> dict[str, Any]:
            return {}

        @staticmethod
        def paper_replay_summary(_trades: list[dict[str, Any]]) -> dict[str, Any]:
            return {"net_points": 0.0}

    payload = paper_journal_payload(
        Store(),
        Deps(),
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        limit=200,
    )
    assert payload is not None
    assert [item["id"] for item in payload["orders"]] == ["ord-new"]
    assert [item["id"] for item in payload["fills"]] == ["fill-new"]


def test_paper_journal_reload_includes_open_position_on_detached_route() -> None:
    instrument_id = "ibkr|contract|123"
    current_fingerprint = "route-new"
    stored_fingerprint = "route-old"
    contract = PaperContractIdentity(
        scope_kind="instrument",
        provider="ibkr",
        provider_contract_id="123",
    ).to_payload()
    stored_position = {
        "id": "position-old-route",
        "symbol": "ES",
        "instrument_id": instrument_id,
        "route_fingerprint": stored_fingerprint,
        "paper_contract": contract,
        "timeframe": "15m",
        "status": "open",
        "qty": 1.0,
        "avg_entry": 100.0,
        "opened_at": "2026-08-01T10:00:00+00:00",
        "payload": {
            "instrument_id": instrument_id,
            "route_fingerprint": stored_fingerprint,
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": contract,
            "side": "long",
            "source": "manual_position",
        },
    }

    class Store:
        @staticmethod
        def read_paper_positions(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs.get("complete_open") is True:
                assert kwargs["status"] == "open"
                assert "instrument_id" not in kwargs
                return [stored_position]
            assert kwargs["instrument_id"] == instrument_id
            assert kwargs["route_fingerprint"] == current_fingerprint
            return []

        @staticmethod
        def read_paper_orders(**_kwargs: Any) -> list[dict[str, Any]]:
            return []

        @staticmethod
        def read_paper_fills(**_kwargs: Any) -> list[dict[str, Any]]:
            return []

    class Deps:
        paper_trade_enriched = staticmethod(lambda trade: trade)
        paper_trade_stats = staticmethod(lambda _trades: {})
        paper_replay_summary = staticmethod(lambda _trades: {"net_points": 0.0})

    payload = paper_journal_payload(
        Store(),
        Deps(),
        instrument_id=instrument_id,
        route_fingerprint=current_fingerprint,
    )

    assert payload is not None
    assert payload["trades"] == []
    assert len(payload["detached_open_trades"]) == 1
    detached = payload["detached_open_trades"][0]
    assert detached["id"] == "position-old-route"
    assert detached["route_fingerprint"] == stored_fingerprint
    assert detached["paper_contract"] == contract


def test_closed_paper_journal_trade_projects_transactional_close_event_facts() -> None:
    instrument_id = "ibkr|contract|123"
    route_fingerprint = "route-1"
    closed_at = "2026-08-21T14:30:00+00:00"
    contract = PaperContractIdentity(
        scope_kind="instrument",
        provider="ibkr",
        provider_contract_id="123",
    ).to_payload()
    position = {
        "id": "position-closed",
        "symbol": "ES",
        "timeframe": "5m",
        "status": "closed",
        "qty": 0.0,
        "avg_entry": 100.0,
        "opened_at": "2026-08-21T13:00:00+00:00",
        "closed_at": closed_at,
        "realized_pnl": 5.25,
        "paper_contract": contract,
        "payload": {
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": contract,
            "side": "long",
            "source": "manual_position",
        },
    }
    event = {
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "event_type": "position_closed",
        "trade_id": "position-closed",
        "payload": {
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "payload": {"execution_command": {"exit_reason": ""}},
            "position": dict(position),
            "fill": {
                "price": 105.25,
                "role": "take",
                "filled_at": closed_at,
            },
        },
    }
    event_calls: list[dict[str, Any]] = []

    class Store:
        @staticmethod
        def read_paper_positions(**kwargs: Any) -> list[dict[str, Any]]:
            if kwargs.get("complete_open") is True:
                return []
            return [position]

        @staticmethod
        def read_paper_orders(**_kwargs: Any) -> list[dict[str, Any]]:
            return []

        @staticmethod
        def read_paper_fills(**_kwargs: Any) -> list[dict[str, Any]]:
            return []

        @staticmethod
        def read_paper_trade_events(**kwargs: Any) -> list[dict[str, Any]]:
            event_calls.append(dict(kwargs))
            return [event]

    class Deps:
        paper_trade_enriched = staticmethod(lambda trade: trade)
        paper_trade_stats = staticmethod(lambda _trades: {})
        paper_replay_summary = staticmethod(lambda _trades: {"net_points": 5.25})

    payload = paper_journal_payload(
        Store(),
        Deps(),
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )

    trade = payload["trades"][0]
    assert trade["exit_price"] == 105.25
    assert trade["exit_reason"] == "target"
    assert trade["closed_at"] == closed_at
    assert event_calls == [
        {
            "limit": 1,
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "trade_ids": ("position-closed",),
            "event_type": "position_closed",
        }
    ]


def test_paper_trades_payload_requires_and_projects_one_exact_route() -> None:
    instrument_id = "ibkr|contract|123"
    route_fingerprint = "route-1"
    now = datetime.now(tz=UTC)

    class Store:
        def read_paper_positions(
            self,
            limit=1000,
            status=None,
            *,
            instrument_id=None,
            route_fingerprint=None,
            complete_open=False,
            contract_scope_kind=None,
        ):
            if instrument_id is None:
                assert status == "open"
                assert complete_open is True
                assert route_fingerprint is None
                return []
            assert instrument_id == "ibkr|contract|123"
            assert route_fingerprint == "route-1"
            assert contract_scope_kind == "instrument"
            return []

        def read_paper_orders(
            self,
            limit=500,
            status=None,
            *,
            instrument_id=None,
            route_fingerprint=None,
            since=None,
            contract_scope_kind=None,
        ):
            assert instrument_id == "ibkr|contract|123"
            assert route_fingerprint == "route-1"
            assert contract_scope_kind == "instrument"
            if status == "pending":
                return []
            return [
                {
                    "id": "order-current",
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "timeframe": "5m",
                },
                {
                    "id": "order-other-route",
                    "instrument_id": instrument_id,
                    "route_fingerprint": "route-2",
                    "timeframe": "5m",
                },
            ]

        def read_paper_fills(
            self,
            limit=1000,
            *,
            instrument_id=None,
            route_fingerprint=None,
            since=None,
            contract_scope_kind=None,
        ):
            assert instrument_id == "ibkr|contract|123"
            assert route_fingerprint == "route-1"
            assert contract_scope_kind == "instrument"
            return [
                {
                    "id": "fill-current",
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "timeframe": "5m",
                    "filled_at": now.isoformat(),
                },
                {
                    "id": "fill-other-route",
                    "instrument_id": instrument_id,
                    "route_fingerprint": "route-2",
                    "timeframe": "5m",
                    "filled_at": now.isoformat(),
                },
            ]

    deps = PaperJournalQueryDeps(
        paper_trade_enriched=lambda trade: trade,
        paper_trade_stats=lambda _trades: {},
        paper_replay_summary=lambda _trades: {"net_points": 0.0},
    )

    payload = paper_trades_payload_from_store(
        Store(),
        deps,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )

    assert payload is not None
    assert payload["instrument_id"] == instrument_id
    assert payload["route_fingerprint"] == route_fingerprint
    assert [order["id"] for order in payload["orders"]] == ["order-current"]
    assert [fill["id"] for fill in payload["fills"]] == ["fill-current"]
    with pytest.raises(ValueError):
        paper_trades_payload_from_store(
            Store(),
            deps,
            instrument_id=instrument_id,
            route_fingerprint=123,
        )


def test_paper_orders_payload_queries_and_projects_one_exact_route() -> None:
    instrument_id = "ibkr|contract|123"
    route_fingerprint = "route-1"
    calls: list[dict[str, Any]] = []

    class Store:
        def read_paper_orders(self, **kwargs):
            calls.append(dict(kwargs))
            return [
                {
                    "id": "order-current",
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "status": "pending",
                }
            ]

    payload = paper_orders_payload_from_store(
        Store(),
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
    )

    assert payload["instrument_id"] == instrument_id
    assert payload["route_fingerprint"] == route_fingerprint
    assert [order["id"] for order in payload["orders"]] == ["order-current"]
    assert [order["id"] for order in payload["active"]] == ["order-current"]
    assert calls[0]["instrument_id"] == instrument_id
    assert calls[0]["route_fingerprint"] == route_fingerprint
    with pytest.raises(ValueError):
        paper_orders_payload_from_store(
            Store(),
            instrument_id=instrument_id,
            route_fingerprint=123,
        )
