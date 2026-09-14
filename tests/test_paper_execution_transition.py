from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.paper_contract import PaperContractIdentity, PaperJournalError
from aef_terminal.storage.repos.paper import (
    _paper_entry_protective_qty,
    _paper_fill_distance_execution_update_allowed,
    _paper_order_command_payload,
    _paper_position_expectation_rejection,
)
from aef_terminal.ui.paper.actions import (
    PaperActionDeps,
    close_paper_trade_action,
    create_paper_order_action,
)
from aef_terminal.ui.paper.execution import (
    paper_order_execution_transition,
    submit_paper_order_execution,
)
from aef_terminal.ui.paper.orders import (
    PAPER_EXECUTION_CONFIG_KEY,
    PAPER_PROTECTION_INTENT_KEY,
    normalize_paper_order,
    update_paper_order,
)
from aef_terminal.ui.services import server_alert_monitor
from aef_terminal.ui.services.server_alert_monitor import (
    ServerAlertMonitorTickDeps,
    process_server_alert_monitor_tick,
)


INSTRUMENT_ID = "ibkr|contract|paper-transition"
ROUTE_FINGERPRINT = "ibkr|route|paper-transition"
PAPER_CONTRACT = PaperContractIdentity(
    scope_kind="instrument",
    provider="ibkr",
    provider_contract_id="paper-transition",
)


def _order(**overrides: Any) -> dict[str, Any]:
    execution_config = overrides.pop(
        "execution_config",
        {"min_rr": 1.25, "edge_gate": True},
    )
    payload = {
        "id": "po-transition",
        "symbol": "BTC",
        "instrument_id": INSTRUMENT_ID,
        "route_fingerprint": ROUTE_FINGERPRINT,
        "provider": "ibkr",
        "provider_contract_id": "paper-transition",
        "timeframe": "15m",
        "side": "long",
        "order_type": "limit",
        "qty": 1.0,
        "entry": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        **overrides,
    }
    order = normalize_paper_order(payload)
    order["payload"] = {
        **dict(order["payload"]),
        PAPER_EXECUTION_CONFIG_KEY: execution_config,
    }
    return order


def _snapshot(price: float) -> dict[str, Any]:
    observed_at = datetime(2026, 7, 26, 13, 0, tzinfo=UTC)
    return {
        "price": price,
        "low": price,
        "high": price,
        "bid": price,
        "ask": price,
        "bid_ask_status": "live",
        "bbo_ts": observed_at,
        "range_kind": "quote",
        "ts": observed_at,
        "source": "test:quote",
        "bar_slot": 17.0,
    }


@pytest.mark.parametrize(
    ("side", "order_type", "entry"),
    [
        ("short", "stop", 95.0),
        ("short", "limit", 110.0),
        ("long", "stop", 110.0),
        ("long", "limit", 95.0),
    ],
)
def test_closed_bar_range_fills_intrabar_cross_at_trigger(
    side: str,
    order_type: str,
    entry: float,
) -> None:
    order = _order(
        side=side,
        order_type=order_type,
        entry=entry,
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
    )

    transition = paper_order_execution_transition(
        order,
        {
            **_snapshot(100.0),
            "low": 94.0,
            "high": 111.0,
            "range_kind": "bar",
        },
    )

    assert transition["outcome"] == "filled"
    assert transition["execution"]["fill_price"] == entry
    assert transition["order"]["fill_price"] == entry


def test_quote_bid_ask_range_does_not_claim_intrabar_cross() -> None:
    order = _order(
        side="short",
        order_type="stop",
        entry=95.0,
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
    )

    transition = paper_order_execution_transition(
        order,
        {
            **_snapshot(100.0),
            "low": 94.0,
            "high": 101.0,
            "range_kind": "quote",
        },
    )

    assert transition["outcome"] == "pending"


@pytest.mark.parametrize("side", ["long", "short"])
def test_market_execution_uses_current_paper_reference(side: str) -> None:
    transition = paper_order_execution_transition(
        _order(
            side=side,
            order_type="market",
            stop_loss=None,
            target=None,
            use_stop_loss=False,
            use_target=False,
        ),
        {
            **_snapshot(100.0),
            "bid": 99.0,
            "ask": 101.0,
            "bid_ask_status": "stale",
        },
    )

    assert transition["outcome"] == "filled"
    assert transition["execution"]["fill_price"] == 100.0


def test_market_execution_does_not_use_confirmed_bar_close_as_current_reference() -> None:
    transition = paper_order_execution_transition(
        _order(
            side="long",
            order_type="market",
            stop_loss=None,
            target=None,
            use_stop_loss=False,
            use_target=False,
        ),
        {**_snapshot(100.0), "range_kind": "bar"},
    )

    assert transition["outcome"] == "price_unavailable"
    assert transition["code"] == "PAPER_EXECUTION_PRICE_UNAVAILABLE"


@pytest.mark.parametrize(
    ("side", "order_type", "entry"),
    [
        ("long", "limit", 90.0),
        ("short", "limit", 110.0),
        ("long", "stop", 110.0),
        ("short", "stop", 90.0),
    ],
)
def test_non_marketable_orders_stay_pending_before_execution_gate(
    side: str,
    order_type: str,
    entry: float,
) -> None:
    stop_loss = entry - 5.0 if side == "long" else entry + 5.0
    target = entry + 10.0 if side == "long" else entry - 10.0
    order = _order(
        side=side,
        order_type=order_type,
        entry=entry,
        stop_loss=stop_loss,
        target=target,
    )

    transition = paper_order_execution_transition(
        order,
        _snapshot(100.0),
    )

    assert transition["outcome"] == "pending"
    assert transition["order"]["status"] == "pending"
    assert transition["execution"] == {
        "fill_price": None,
        "source": "test:quote",
        "ts": "2026-07-26T13:00:00+00:00",
        "bar_slot": 17.0,
        "planned_entry": None,
        "analysis_bar_ts": None,
        "analysis_generation": None,
    }


def test_missing_snapshot_blocks_market_but_keeps_resting_order_pending() -> None:
    market = _order(order_type="market")
    resting = _order(order_type="limit", entry=90.0, stop_loss=85.0, target=100.0)

    assert market["status"] == "pending"
    market_transition = paper_order_execution_transition(market, None)
    resting_transition = paper_order_execution_transition(resting, None)

    assert market_transition["outcome"] == "price_unavailable"
    assert market_transition["code"] == "PAPER_EXECUTION_PRICE_UNAVAILABLE"
    assert resting_transition["outcome"] == "pending"


def test_signal_reservation_collision_resumes_first_authoritative_command() -> None:
    requested = _order(
        id="sig-generation-1",
        entry=90.0,
        stop_loss=85.0,
        target=100.0,
        payload={
            "trade_id": "pt-generation-1",
            "analysis_generation": "window-hash-1",
            "analysis_bar_ts": "2026-07-26T12:55:00+00:00",
            "planned_entry": 90.0,
            "position_expectation": {"state": "absent"},
        },
    )
    first_create_command = _paper_order_command_payload(requested)
    authoritative = {
        **requested,
        "create_command": first_create_command,
    }
    reserve_calls: list[dict[str, Any]] = []

    class Store:
        @staticmethod
        def upsert_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            reserve_calls.append(dict(order))
            if len(reserve_calls) == 1:
                raise PaperJournalError("PAPER_ORDER_ID_COMMAND_COLLISION")
            return dict(authoritative)

        @staticmethod
        def read_paper_order(
            order_id: str,
            *,
            instrument_id: str,
            route_fingerprint: str,
        ) -> dict[str, Any]:
            assert order_id == requested["id"]
            assert instrument_id == INSTRUMENT_ID
            assert route_fingerprint == ROUTE_FINGERPRINT
            return dict(authoritative)

        @staticmethod
        def apply_filled_paper_order(
            _order: dict[str, Any],
        ) -> dict[str, Any]:
            raise AssertionError("resting winner must remain pending")

    submission = submit_paper_order_execution(
        Store(),
        {
            **requested,
            "qty": 2.0,
            "payload": {
                **requested["payload"],
                "position_expectation": {
                    "state": "open",
                    "position_id": "position-1",
                },
            },
        },
        _snapshot(100.0),
    )

    assert submission["outcome"] == "pending"
    assert submission["order"] == authoritative
    assert len(reserve_calls) == 2
    assert reserve_calls[1]["payload"][PAPER_EXECUTION_CONFIG_KEY] == {
        "min_rr": 1.25,
        "edge_gate": True,
    }


@pytest.mark.parametrize(
    "execution_config",
    [None, {}, {"min_rr": 1.25, "edge_gate": True, "execution_plan_gate": False}],
)
def test_execution_transition_rejects_missing_or_noncanonical_policy(
    execution_config: object,
) -> None:
    order = _order(execution_config=execution_config)

    with pytest.raises(ValueError, match="PAPER_(EXECUTION_CONFIG_REQUIRED|CONFIG_FIELDS_INVALID)"):
        paper_order_execution_transition(order, _snapshot(100.0))


def test_pending_order_update_requires_immutable_execution_policy() -> None:
    order = _order()
    order["payload"].pop(PAPER_EXECUTION_CONFIG_KEY)

    with pytest.raises(ValueError, match="PAPER_EXECUTION_CONFIG_REQUIRED"):
        update_paper_order(order, {"entry": 101.0})


@pytest.mark.parametrize(
    ("side", "order_type", "entry", "screen_price"),
    [
        ("long", "limit", 90.0, 89.0),
        ("short", "limit", 110.0, 111.0),
        ("long", "stop", 110.0, 111.0),
        ("short", "stop", 90.0, 89.0),
    ],
)
def test_paper_limit_and_stop_use_authoritative_screen_price_assumption(
    side: str,
    order_type: str,
    entry: float,
    screen_price: float,
) -> None:
    order = _order(
        side=side,
        order_type=order_type,
        entry=entry,
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
    )

    transition = paper_order_execution_transition(
        order,
        _snapshot(screen_price),
    )

    assert transition["outcome"] == "filled"
    assert transition["execution"]["fill_price"] == screen_price


@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_fill_distance_protection_is_materialized_from_actual_fill(
    order_type: str,
) -> None:
    order = _order(
        order_type=order_type,
        entry=110.0,
        stop_loss=None,
        target=None,
        stop_points=5.0,
        target_points=10.0,
        use_stop_loss=True,
        use_target=True,
    )
    assert order["payload"][PAPER_PROTECTION_INTENT_KEY] == {
        "basis": "fill_distance",
        "stop_points": 5.0,
        "target_points": 10.0,
    }
    assert order["payload"]["protection_basis"] == "fill_distance"

    transition = paper_order_execution_transition(
        order,
        _snapshot(100.0),
    )

    assert transition["outcome"] == "filled"
    assert transition["order"]["entry"] == 110.0
    assert transition["order"]["fill_price"] == 100.0
    assert transition["order"]["stop_loss"] == 95.0
    assert transition["order"]["target"] == 110.0
    assert transition["execution"] == {
        "fill_price": 100.0,
        "source": "test:quote",
        "ts": "2026-07-26T13:00:00+00:00",
        "bar_slot": 17.0,
        "planned_entry": None,
        "analysis_bar_ts": None,
        "analysis_generation": None,
    }


@pytest.mark.parametrize("empty_presentation", [False, True])
def test_absolute_structure_is_not_rebased_at_fill(monkeypatch, empty_presentation) -> None:
    if empty_presentation:
        monkeypatch.setattr(
            "aef_terminal.ui.paper.execution.trade_plan_rejection_text", lambda _fact: ""
        )
    order = _order(
        order_type="market",
        entry=110.0,
        stop_loss=105.0,
        target=120.0,
        payload={"protection_basis": "absolute_structure"},
    )

    transition = paper_order_execution_transition(
        order,
        _snapshot(100.0),
    )

    assert transition["outcome"] == "rejected"
    assert transition["code"] == "PAPER_EXECUTION_PLAN_REJECTED"
    assert transition["message"] == ("" if empty_presentation else "stop 105 through fill 100")
    assert transition["order"]["stop_loss"] == 105.0
    assert transition["order"]["target"] == 120.0


def test_absolute_plan_requires_execution_snapshot_from_its_generation() -> None:
    order = _order(
        order_type="market",
        payload={
            "protection_basis": "absolute_structure",
            "planned_entry": 100.0,
            "analysis_bar_ts": "2026-07-26T13:01:00+00:00",
        },
    )

    stale = paper_order_execution_transition(order, _snapshot(100.0))
    timestamp_missing = paper_order_execution_transition(
        order,
        {
            "price": 100.0,
            "source": "test:quote",
        },
    )

    assert stale["outcome"] == "rejected"
    assert stale["code"] == "PAPER_EXECUTION_SNAPSHOT_STALE"
    assert stale["execution"]["planned_entry"] == 100.0
    assert stale["execution"]["analysis_bar_ts"] == "2026-07-26T13:01:00+00:00"
    assert timestamp_missing["outcome"] == "price_unavailable"
    assert timestamp_missing["code"] == "PAPER_EXECUTION_SNAPSHOT_TIMESTAMP_UNAVAILABLE"


def test_pending_order_update_preserves_or_replaces_protection_basis() -> None:
    distance_order = _order(
        entry=90.0,
        stop_loss=None,
        target=None,
        stop_points=5.0,
        target_points=10.0,
        use_stop_loss=True,
        use_target=True,
    )

    moved = update_paper_order(distance_order, {"entry": 91.0})
    absolute = update_paper_order(
        distance_order,
        {
            "stop_loss": 84.0,
            "target": 102.0,
        },
    )

    assert moved["payload"][PAPER_PROTECTION_INTENT_KEY]["basis"] == "fill_distance"
    assert PAPER_PROTECTION_INTENT_KEY not in absolute.get("payload", {})
    assert absolute["payload"]["protection_basis"] == "absolute_structure"


def test_missing_or_contradictory_paper_protection_fails_closed() -> None:
    absolute = _order()
    missing = {
        **absolute,
        "payload": {},
    }
    contradictory = {
        **absolute,
        "payload": {
            "protection_basis": "absolute_structure",
            PAPER_PROTECTION_INTENT_KEY: {
                "basis": "fill_distance",
                "stop_points": 5.0,
                "target_points": 10.0,
            },
        },
    }

    for invalid in (missing, contradictory):
        transition = paper_order_execution_transition(invalid, _snapshot(100.0))
        assert transition["outcome"] == "rejected"
        assert transition["code"] == "PAPER_EXECUTION_PLAN_REJECTED"
        with pytest.raises(ValueError, match="PAPER_(PROTECTION|ABSOLUTE)"):
            _paper_order_command_payload(invalid)


def test_storage_accepts_only_canonical_fill_distance_execution_rebase() -> None:
    pending = _order(
        entry=110.0,
        stop_loss=None,
        target=None,
        stop_points=5.0,
        target_points=10.0,
        use_stop_loss=True,
        use_target=True,
    )
    create_command = _paper_order_command_payload(pending)
    pending["create_command"] = create_command
    pending["execution_command"] = create_command
    transition = paper_order_execution_transition(
        pending,
        _snapshot(100.0),
    )
    assert transition["outcome"] == "filled"
    filled = transition["order"]
    execution_command = _paper_order_command_payload(filled)

    assert _paper_fill_distance_execution_update_allowed(
        pending,
        filled,
        execution_command,
        fill_price=100.0,
    )
    tampered = {
        **execution_command,
        "target": 999.0,
    }
    assert not _paper_fill_distance_execution_update_allowed(
        pending,
        filled,
        tampered,
        fill_price=100.0,
    )


def test_fill_transition_keeps_create_command_stable_across_fill_prices() -> None:
    original = _order(
        order_type="market",
        entry=110.0,
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
    )

    first = paper_order_execution_transition(original, _snapshot(100.0))
    retry = paper_order_execution_transition(original, _snapshot(101.0))

    assert first["outcome"] == "filled"
    assert retry["outcome"] == "filled"
    assert first["order"]["fill_price"] == 100.0
    assert retry["order"]["fill_price"] == 101.0
    assert _paper_order_command_payload(first["order"]["create_command"]) == (
        _paper_order_command_payload(original)
    )
    assert _paper_order_command_payload(retry["order"]["create_command"]) == (
        _paper_order_command_payload(original)
    )
    assert _paper_order_command_payload(first["order"]) == (
        _paper_order_command_payload(retry["order"])
    )


@pytest.mark.parametrize(
    (
        "signed_delta",
        "current_qty",
        "previous_qty",
        "reversed_position",
        "expected",
    ),
    [
        (3.0, 3.0, 0.0, False, 3.0),
        (2.0, 5.0, 3.0, False, 2.0),
        (-2.0, 3.0, 5.0, False, None),
        (-5.0, 0.0, 5.0, False, None),
        (-6.0, -1.0, 5.0, True, 1.0),
        (6.0, 1.0, -5.0, True, 1.0),
    ],
)
def test_entry_protection_covers_new_leg_or_reversal_residual(
    signed_delta: float,
    current_qty: float,
    previous_qty: float,
    reversed_position: bool,
    expected: float | None,
) -> None:
    assert (
        _paper_entry_protective_qty(
            signed_delta=signed_delta,
            current_qty=current_qty,
            previous_qty=previous_qty,
            reversed_position=reversed_position,
        )
        == expected
    )


def test_position_expectation_blocks_stale_auto_open_and_flip() -> None:
    position = {
        "id": "position-1",
        "qty": 2.0,
        "updated_at": "2026-07-26T13:00:00+00:00",
        "payload": {"side": "long"},
    }
    exact = {
        "state": "open",
        "position_id": "position-1",
        "side": "long",
        "qty": 2.0,
        "updated_at": "2026-07-26T13:00:00+00:00",
    }

    assert _paper_position_expectation_rejection(None, {"state": "absent"}) is None
    assert _paper_position_expectation_rejection(position, exact) is None
    assert _paper_position_expectation_rejection(
        position,
        {"state": "absent"},
    ) == (
        "PAPER_POSITION_EXPECTATION_STALE",
        "paper position was opened before execution",
    )
    assert _paper_position_expectation_rejection(
        {**position, "qty": 1.0},
        exact,
    ) == (
        "PAPER_POSITION_EXPECTATION_STALE",
        "paper position changed before execution",
    )


def test_stop_only_rejects_stop_through_fill_without_rr_gate() -> None:
    order = _order(
        entry=110.0,
        stop_loss=105.0,
        target=None,
        use_stop_loss=True,
        use_target=False,
        execution_config={"min_rr": 2.0, "edge_gate": True},
    )

    transition = paper_order_execution_transition(
        order,
        _snapshot(100.0),
    )

    assert transition["outcome"] == "rejected"
    assert transition["message"] == "stop 105 through fill 100"


def test_rr_is_applied_only_to_a_complete_bracket() -> None:
    target_only = _order(
        order_type="stop",
        entry=90.0,
        stop_loss=None,
        target=105.0,
        use_stop_loss=False,
        use_target=True,
        execution_config={"min_rr": 2.0, "edge_gate": True},
    )
    no_protection = _order(
        order_type="market",
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
        execution_config={"min_rr": 2.0, "edge_gate": True},
    )
    low_rr_bracket = _order(
        order_type="market",
        entry=100.0,
        stop_loss=95.0,
        target=104.0,
        execution_config={"min_rr": 2.0, "edge_gate": True},
    )

    assert (
        paper_order_execution_transition(
            target_only,
            _snapshot(100.0),
        )["outcome"]
        == "filled"
    )
    assert (
        paper_order_execution_transition(
            no_protection,
            _snapshot(100.0),
        )["outcome"]
        == "filled"
    )
    low_rr = paper_order_execution_transition(
        low_rr_bracket,
        _snapshot(100.0),
    )
    assert low_rr["outcome"] == "rejected"
    assert low_rr["message"] == "RR 0.80 below min 2.00 at fill"


def test_create_action_persists_far_non_marketable_order() -> None:
    saved: list[dict[str, Any]] = []
    route = SimpleNamespace(
        instrument_id=INSTRUMENT_ID,
        fingerprint=ROUTE_FINGERPRINT,
        instrument_key="BTC",
        provider="ibkr",
        instrument={"instrument_id": INSTRUMENT_ID},
        adapter=SimpleNamespace(session_contract_id=lambda _instrument: "paper-transition"),
    )

    class Store:
        @staticmethod
        def upsert_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            saved.append(dict(order))
            return dict(order)

        @staticmethod
        def apply_filled_paper_order(
            _order: dict[str, Any],
        ) -> dict[str, Any]:
            raise AssertionError("non-marketable order must not fill")

    result = create_paper_order_action(
        Store(),
        {
            "id": "po-00000000-0000-4000-8000-000000000001",
            "instrument_id": INSTRUMENT_ID,
            "route_fingerprint": ROUTE_FINGERPRINT,
            "timeframe": "15m",
            "side": "long",
            "order_type": "limit",
            "qty": 1.0,
            "entry": 90.0,
            "stop_points": 5.0,
            "target_points": 10.0,
            "use_stop_loss": True,
            "use_target": True,
        },
        PaperActionDeps(
            paper_config=lambda: {
                "min_rr": 1.25,
                "edge_gate": True,
            },
            queue_paper_trade_telegram=lambda *_args: None,
            paper_order_price_snapshot=lambda _order: _snapshot(100.0),
            instrument_lookup=lambda instrument_id: {"instrument_id": instrument_id},
            instrument_router=lambda _instrument: route,
        ),
    )

    assert result["ok"] is True
    assert result["opened"] is False
    assert result["order"]["status"] == "pending"
    assert len(saved) == 1


@pytest.mark.parametrize(
    (
        "position_status",
        "closed_position_status",
        "expected_flags",
        "expected_events",
    ),
    [
        (
            "closed",
            None,
            {"filled": True, "opened": False, "closed": True, "flipped": False},
            ["close"],
        ),
        (
            "open",
            "closed",
            {"filled": True, "opened": True, "closed": False, "flipped": True},
            ["close", "open"],
        ),
    ],
)
def test_create_action_projects_manual_net_close_and_flip(
    position_status: str,
    closed_position_status: str | None,
    expected_flags: dict[str, bool],
    expected_events: list[str],
) -> None:
    route = SimpleNamespace(
        instrument_id=INSTRUMENT_ID,
        fingerprint=ROUTE_FINGERPRINT,
        instrument_key="BTC",
        provider="ibkr",
        instrument={"instrument_id": INSTRUMENT_ID},
        adapter=SimpleNamespace(session_contract_id=lambda _instrument: "paper-transition"),
    )

    def stored_position(
        position_id: str,
        *,
        status: str,
        side: str,
    ) -> dict[str, Any]:
        return {
            "id": position_id,
            "symbol": "BTC",
            "instrument_id": INSTRUMENT_ID,
            "route_fingerprint": ROUTE_FINGERPRINT,
            "timeframe": "15m",
            "status": status,
            "qty": 0.0 if status == "closed" else -1.0,
            "avg_entry": 100.0,
            "opened_at": "2026-07-26T12:00:00+00:00",
            "updated_at": "2026-07-26T13:00:00+00:00",
            "closed_at": ("2026-07-26T13:00:00+00:00" if status == "closed" else None),
            "realized_pnl": 1.0 if status == "closed" else 0.0,
            "payload": {
                "instrument_id": INSTRUMENT_ID,
                "route_fingerprint": ROUTE_FINGERPRINT,
                "provider": "ibkr",
                "provider_contract_id": "paper-transition",
                "side": side,
                "source": "manual_position",
            },
        }

    position = stored_position(
        "position-current",
        status=position_status,
        side="long" if position_status == "closed" else "short",
    )
    closed_position = (
        stored_position(
            "position-closed",
            status=closed_position_status,
            side="long",
        )
        if closed_position_status is not None
        else None
    )

    class Store:
        @staticmethod
        def upsert_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            return dict(order)

        @staticmethod
        def apply_filled_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            return {
                "outcome": "filled",
                "order": dict(order),
                "position": position,
                "fill": {
                    "id": 1,
                    "instrument_id": INSTRUMENT_ID,
                    "route_fingerprint": ROUTE_FINGERPRINT,
                    "price": 100.0,
                },
                "created_orders": [],
                "cancelled_orders": [],
                "closed_position": closed_position,
                "opened_new_position": closed_position is not None,
            }

    notifications: list[tuple[str, dict[str, Any]]] = []
    result = create_paper_order_action(
        Store(),
        {
            "id": "po-00000000-0000-4000-8000-000000000002",
            "instrument_id": INSTRUMENT_ID,
            "route_fingerprint": ROUTE_FINGERPRINT,
            "timeframe": "15m",
            "side": "short",
            "order_type": "market",
            "qty": 2.0,
            "entry": 105.0,
            "use_stop_loss": False,
            "use_target": False,
        },
        PaperActionDeps(
            paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
            queue_paper_trade_telegram=lambda event, trade: notifications.append((event, trade)),
            paper_order_price_snapshot=lambda _order: _snapshot(100.0),
            now=lambda: datetime(2026, 7, 26, 13, 0, tzinfo=UTC),
            instrument_lookup=lambda instrument_id: {"instrument_id": instrument_id},
            instrument_router=lambda _instrument: route,
        ),
    )

    assert {key: result[key] for key in ("filled", "opened", "closed", "flipped")} == expected_flags
    assert [event for event, _trade in notifications] == expected_events
    assert notifications[0][1]["exit_price"] == 100.0
    assert notifications[0][1]["exit_reason"] == (
        "manual_flip" if result["flipped"] else "manual_net_close"
    )


def test_monitor_cancels_unsafe_touched_order_without_applying_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order(
        id="po-delayed-reject",
        entry=110.0,
        stop_loss=105.0,
        target=120.0,
    )
    applied: list[dict[str, Any]] = []
    rejections: list[tuple[str, dict[str, Any]]] = []

    class Store:
        @staticmethod
        def initialize() -> None:
            return None

        @staticmethod
        def apply_filled_paper_order(
            filled_order: dict[str, Any],
        ) -> dict[str, Any]:
            applied.append(filled_order)
            raise AssertionError("unsafe fill must not reach the journal")

        @staticmethod
        def reject_paper_order_execution_if_pending(
            order_id: str,
            _cancelled_at: datetime,
            **kwargs: Any,
        ) -> dict[str, Any]:
            rejections.append((order_id, dict(kwargs)))
            return {**order, "status": "cancelled"}

    def exact_route(_instrument: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            instrument_id=INSTRUMENT_ID,
            fingerprint=ROUTE_FINGERPRINT,
        )

    monkeypatch.setattr(
        server_alert_monitor,
        "route_instrument",
        exact_route,
    )
    monkeypatch.setattr(
        "aef_terminal.ui.services.server_alert_selection.route_instrument",
        exact_route,
    )

    wanted: list[list[str]] = []

    async def paper_snapshot(_store: object) -> tuple[list[dict], list[dict]]:
        return [order], []

    deps = ServerAlertMonitorTickDeps(
        store_factory=Store,
        quote_route_snapshot=lambda: SimpleNamespace(
            select_instruments=lambda _ids: [{"instrument_id": INSTRUMENT_ID}]
        ),
        server_pending_price_alerts_snapshot=lambda _store: [],
        server_active_price_alerts_snapshot=lambda _store: [],
        active_paper_orders=lambda orders: orders,
        set_server_alert_wanted=lambda instrument_ids: wanted.append(list(instrument_ids)),
        quote_cache_for_instruments=lambda _instruments: ({}, "", 0),
        logger=logging.getLogger("test-paper-execution-transition"),
        paper_order_execution_transition=paper_order_execution_transition,
        screen_execution_price_snapshot=lambda *_args: _snapshot(100.0),
        queue_paper_trade_telegram=lambda *_args: None,
        quote_for_route=lambda *_args: {},
        server_alert_price_snapshot=lambda *_args, **_kwargs: None,
        server_vsa_fuel_alert_state=lambda *_args, **_kwargs: None,
        send_server_telegram_alert=lambda _payload: {},
        alert_monitor_payload=lambda _alert, _price: {},
        server_dynamic_alert_state=lambda *_args, **_kwargs: None,
        server_alert_indicator_params=lambda: {},
        alert_freeze_active=lambda *_args, **_kwargs: False,
        crossing_baselines={},
        server_ema_touch_direction=lambda *_args, **_kwargs: "",
        ema_touch_rearm_ms=lambda _alert: 0,
        alert_cross_hit=lambda *_args, **_kwargs: False,
        server_alert_freeze_ms=lambda _kind: 0,
        paper_order_price_snapshot=lambda _order: None,
        paper_journal_active_snapshot=paper_snapshot,
        set_paper_option_quote_demand=lambda _entities: 0,
        server_active_price_alerts_for_route=lambda *_scope: [],
    )

    asyncio.run(process_server_alert_monitor_tick(deps))

    assert wanted == [[INSTRUMENT_ID]]
    assert applied == []
    assert rejections == [
        (
            "po-delayed-reject",
            {
                "instrument_id": INSTRUMENT_ID,
                "route_fingerprint": ROUTE_FINGERPRINT,
                "paper_contract": PAPER_CONTRACT,
                "code": "PAPER_EXECUTION_PLAN_REJECTED",
                "reason": "stop 105 through fill 100",
                "execution": {
                    "fill_price": 100.0,
                    "source": "test:quote",
                    "ts": "2026-07-26T13:00:00+00:00",
                    "bar_slot": 17.0,
                    "planned_entry": None,
                    "analysis_bar_ts": None,
                    "analysis_generation": None,
                },
            },
        )
    ]


def test_monitor_uses_stored_route_and_stop_first_when_one_bar_hits_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_fingerprint = "ibkr|route|paper-transition-old"
    new_fingerprint = "ibkr|route|paper-transition-new"
    position_id = "position-rollover"
    stop_order = _order(
        id="po-rollover-stop",
        route_fingerprint=old_fingerprint,
        side="short",
        order_type="stop",
        entry=95.0,
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
        role="stop",
        reduce_only=True,
        position_id=position_id,
    )
    take_order = _order(
        id="po-rollover-take",
        route_fingerprint=old_fingerprint,
        side="short",
        order_type="limit",
        entry=110.0,
        stop_loss=None,
        target=None,
        use_stop_loss=False,
        use_target=False,
        role="take",
        reduce_only=True,
        position_id=position_id,
    )
    position = {
        "id": position_id,
        "symbol": "BTC",
        "instrument_id": INSTRUMENT_ID,
        "route_fingerprint": old_fingerprint,
        "paper_contract": PAPER_CONTRACT.to_payload(),
        "timeframe": "15m",
        "status": "open",
        "qty": 1.0,
        "avg_entry": 100.0,
        "payload": {
            "instrument_id": INSTRUMENT_ID,
            "route_fingerprint": old_fingerprint,
            "provider": "ibkr",
            "provider_contract_id": "paper-transition",
            "paper_contract": PAPER_CONTRACT.to_payload(),
            "side": "long",
        },
    }
    applied_roles: list[str] = []
    cancelled_roles: list[str] = []

    class Store:
        @staticmethod
        def initialize() -> None:
            return None

        @staticmethod
        def apply_filled_paper_order(
            filled_order: dict[str, Any],
        ) -> dict[str, Any]:
            role = str(filled_order.get("role") or "")
            applied_roles.append(role)
            if role == "take":
                cancelled_roles.append(role)
                return {
                    "outcome": "cancelled",
                    "order": {**filled_order, "status": "cancelled"},
                    "position": position,
                    "fill": None,
                    "created_orders": [],
                    "cancelled_orders": [],
                    "closed_position": None,
                    "opened_new_position": False,
                }
            position["status"] = "closed"
            position["qty"] = 0.0
            return {
                "outcome": "filled",
                "order": filled_order,
                "position": position,
                "fill": None,
                "created_orders": [],
                "cancelled_orders": [],
                "closed_position": None,
                "opened_new_position": False,
            }

    def current_route(_instrument: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            instrument_id=INSTRUMENT_ID,
            fingerprint=new_fingerprint,
        )

    monkeypatch.setattr(server_alert_monitor, "route_instrument", current_route)
    monkeypatch.setattr(
        "aef_terminal.ui.services.server_alert_selection.route_instrument",
        current_route,
    )
    wanted: list[list[str]] = []

    async def paper_snapshot(_store: object) -> tuple[list[dict], list[dict]]:
        return [take_order, stop_order], [position]

    deps = ServerAlertMonitorTickDeps(
        store_factory=Store,
        quote_route_snapshot=lambda: SimpleNamespace(
            select_instruments=lambda _ids: [{"instrument_id": INSTRUMENT_ID}]
        ),
        server_pending_price_alerts_snapshot=lambda _store: [],
        server_active_price_alerts_snapshot=lambda _store: [],
        active_paper_orders=lambda orders: orders,
        set_server_alert_wanted=lambda values: wanted.append(list(values)),
        quote_cache_for_instruments=lambda _instruments: ({}, "", 0),
        logger=logging.getLogger("test-paper-rollover-exit"),
        paper_order_execution_transition=paper_order_execution_transition,
        screen_execution_price_snapshot=lambda *_args: {
            **_snapshot(100.0),
            "low": 94.0,
            "high": 111.0,
            "range_kind": "bar",
        },
        queue_paper_trade_telegram=lambda *_args: None,
        quote_for_route=lambda *_args: {},
        server_alert_price_snapshot=lambda *_args, **_kwargs: None,
        server_vsa_fuel_alert_state=lambda *_args, **_kwargs: None,
        send_server_telegram_alert=lambda _payload: {},
        alert_monitor_payload=lambda _alert, _price: {},
        server_dynamic_alert_state=lambda *_args, **_kwargs: None,
        server_alert_indicator_params=lambda: {},
        alert_freeze_active=lambda *_args, **_kwargs: False,
        crossing_baselines={},
        server_ema_touch_direction=lambda *_args, **_kwargs: "",
        ema_touch_rearm_ms=lambda _alert: 0,
        alert_cross_hit=lambda *_args, **_kwargs: False,
        server_alert_freeze_ms=lambda _kind: 0,
        paper_order_price_snapshot=lambda _order: None,
        paper_journal_active_snapshot=paper_snapshot,
        set_paper_option_quote_demand=lambda _entities: 0,
        server_active_price_alerts_for_route=lambda *_scope: [],
    )

    asyncio.run(process_server_alert_monitor_tick(deps))

    assert wanted == [[]]
    assert applied_roles == ["stop", "take"]
    assert cancelled_roles == ["take"]
    assert position["status"] == "closed"
    assert position["qty"] == 0.0


def test_manual_old_route_close_reserves_without_mark_then_monitor_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_fingerprint = "ibkr|route|paper-transition-old"
    new_fingerprint = "ibkr|route|paper-transition-new"
    position_id = "position-pending-close"
    position = {
        "id": position_id,
        "symbol": "BTC",
        "instrument_id": INSTRUMENT_ID,
        "route_fingerprint": old_fingerprint,
        "paper_contract": PAPER_CONTRACT.to_payload(),
        "timeframe": "15m",
        "status": "open",
        "qty": 1.0,
        "avg_entry": 100.0,
        "payload": {
            "instrument_id": INSTRUMENT_ID,
            "route_fingerprint": old_fingerprint,
            "provider": "ibkr",
            "provider_contract_id": "paper-transition",
            "paper_contract": PAPER_CONTRACT.to_payload(),
            "side": "long",
        },
    }

    class Store:
        pending_order: dict[str, Any] | None = None
        fill_price: float | None = None

        @staticmethod
        def initialize() -> None:
            return None

        @classmethod
        def read_paper_positions(cls, **kwargs: Any) -> list[dict[str, Any]]:
            assert kwargs["paper_contract"] == PAPER_CONTRACT
            if position["status"] != "open" and kwargs["status"] == "open":
                return []
            return [position]

        @classmethod
        def upsert_paper_order(
            cls,
            order: dict[str, Any],
        ) -> dict[str, Any]:
            if cls.pending_order is None:
                cls.pending_order = {
                    **order,
                    "status": "pending",
                    "create_command": _paper_order_command_payload(order),
                }
            return dict(cls.pending_order)

        @classmethod
        def read_paper_orders(cls, **_kwargs: Any) -> list[dict[str, Any]]:
            return [dict(cls.pending_order)] if cls.pending_order else []

        @classmethod
        def read_paper_order(
            cls,
            _order_id: str,
            **_kwargs: Any,
        ) -> dict[str, Any] | None:
            return dict(cls.pending_order) if cls.pending_order else None

        @classmethod
        def apply_filled_paper_order(
            cls,
            order: dict[str, Any],
        ) -> dict[str, Any]:
            cls.fill_price = float(order["fill_price"])
            cls.pending_order = {**order, "status": "filled"}
            position.update(
                {
                    "status": "closed",
                    "qty": 0.0,
                    "closed_at": "2026-07-26T13:05:00+00:00",
                    "realized_pnl": cls.fill_price - 100.0,
                }
            )
            return {
                "outcome": "filled",
                "order": dict(cls.pending_order),
                "position": dict(position),
                "fill": {"price": cls.fill_price},
                "created_orders": [],
                "cancelled_orders": [],
                "closed_position": None,
                "opened_new_position": False,
            }

    current_route = SimpleNamespace(
        instrument_id=INSTRUMENT_ID,
        fingerprint=new_fingerprint,
    )
    result = close_paper_trade_action(
        Store(),
        position_id,
        {"command_id": "pc-00000000-0000-4000-8000-000000000041"},
        PaperActionDeps(
            paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
            queue_paper_trade_telegram=lambda *_args: None,
            paper_order_price_snapshot=lambda _order: None,
            instrument_lookup=lambda _instrument_id: {"instrument_id": INSTRUMENT_ID},
            instrument_router=lambda _instrument: current_route,
            now=lambda: datetime(2026, 7, 26, 13, 0, tzinfo=UTC),
        ),
        instrument_id=INSTRUMENT_ID,
        route_fingerprint=old_fingerprint,
        paper_contract=PAPER_CONTRACT.to_payload(),
    )

    assert result["ok"] is True
    assert result["outcome"] == "pending"
    assert result["retryable"] is True
    assert result["code"] == "PAPER_EXIT_PRICE_UNAVAILABLE_OLD_ROUTE"
    assert Store.pending_order is not None
    assert Store.pending_order["status"] == "pending"
    assert Store.pending_order["create_command"]["entry"] is None

    def selected_route(_instrument: dict[str, Any]) -> SimpleNamespace:
        return current_route

    monkeypatch.setattr(server_alert_monitor, "route_instrument", selected_route)
    monkeypatch.setattr(
        "aef_terminal.ui.services.server_alert_selection.route_instrument",
        selected_route,
    )

    async def paper_snapshot(_store: object) -> tuple[list[dict], list[dict]]:
        orders = [dict(Store.pending_order)] if Store.pending_order else []
        positions = [dict(position)] if position["status"] == "open" else []
        return orders, positions

    deps = ServerAlertMonitorTickDeps(
        store_factory=Store,
        quote_route_snapshot=lambda: SimpleNamespace(
            select_instruments=lambda _ids: [{"instrument_id": INSTRUMENT_ID}]
        ),
        server_pending_price_alerts_snapshot=lambda _store: [],
        server_active_price_alerts_snapshot=lambda _store: [],
        active_paper_orders=lambda orders: orders,
        set_server_alert_wanted=lambda _values: None,
        quote_cache_for_instruments=lambda _instruments: ({}, "", 0),
        logger=logging.getLogger("test-paper-pending-close"),
        paper_order_execution_transition=paper_order_execution_transition,
        screen_execution_price_snapshot=lambda *_args: {
            **_snapshot(102.0),
            "range_kind": "quote",
        },
        queue_paper_trade_telegram=lambda *_args: None,
        quote_for_route=lambda *_args: {},
        server_alert_price_snapshot=lambda *_args, **_kwargs: None,
        server_vsa_fuel_alert_state=lambda *_args, **_kwargs: None,
        send_server_telegram_alert=lambda _payload: {},
        alert_monitor_payload=lambda _alert, _price: {},
        server_dynamic_alert_state=lambda *_args, **_kwargs: None,
        server_alert_indicator_params=lambda: {},
        alert_freeze_active=lambda *_args, **_kwargs: False,
        crossing_baselines={},
        server_ema_touch_direction=lambda *_args, **_kwargs: "",
        ema_touch_rearm_ms=lambda _alert: 0,
        alert_cross_hit=lambda *_args, **_kwargs: False,
        server_alert_freeze_ms=lambda _kind: 0,
        paper_order_price_snapshot=lambda _order: None,
        paper_journal_active_snapshot=paper_snapshot,
        set_paper_option_quote_demand=lambda _entities: 0,
        server_active_price_alerts_for_route=lambda *_scope: [],
    )

    asyncio.run(process_server_alert_monitor_tick(deps))

    assert Store.fill_price == 102.0
    assert position["status"] == "closed"
