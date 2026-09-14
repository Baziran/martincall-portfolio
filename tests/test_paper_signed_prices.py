from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
import pytest

from aef_terminal.paper_contract import PAPER_EXECUTION_CONFIG_KEY, PaperContractIdentity
from aef_terminal.storage.db_utils import (
    _paper_fill_from_row,
    _paper_order_from_row,
    _paper_position_from_row,
)
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore

from aef_terminal.storage.repos.paper import (
    PaperRepoMixin,
    _paper_fill_command,
    _paper_order_command_payload,
    _paper_position_payload_from_order,
)
from aef_terminal.ui.paper.actions import (
    PaperActionDeps,
    cancel_paper_order_action,
    close_paper_trade_action,
    create_paper_order_action,
    patch_paper_order_action,
)
from aef_terminal.ui.paper.positions import position_trade_from_storage
from aef_terminal.ui.paper.signals import paper_trade_from_signal
from aef_terminal.ui.routers import trading as trading_router
from aef_terminal.ui.routers.trading import TradingRouterDeps, create_trading_router


INSTRUMENT_PAPER_CONTRACT = PaperContractIdentity(
    scope_kind="instrument",
    provider="ibkr",
    provider_contract_id="123",
).to_payload()


def _qualified_order(**overrides: object) -> dict[str, object]:
    return {
        "id": "order-1",
        "symbol": "CL",
        "instrument_id": "ibkr|contract|123",
        "route_fingerprint": "route-1",
        "provider": "ibkr",
        "provider_contract_id": "123",
        "paper_contract": INSTRUMENT_PAPER_CONTRACT,
        "timeframe": "5m",
        "side": "long",
        "order_type": "market",
        "status": "filled",
        "qty": 1.0,
        "entry": 0.0,
        "stop_loss": -1.0,
        "target": 2.0,
        "use_stop_loss": True,
        "use_target": True,
        "fill_price": 0.0,
        "filled_at": "2026-07-21T10:00:00+00:00",
        "created_at": "2026-07-21T10:00:00+00:00",
        "updated_at": "2026-07-21T10:00:00+00:00",
        "payload": {
            "protection_basis": "absolute_structure",
            PAPER_EXECUTION_CONFIG_KEY: {"min_rr": 1.25, "edge_gate": True},
        },
        **overrides,
    }


def test_paper_order_row_requires_exact_current_projection() -> None:
    row = (
        "order-1",
        "CL",
        "5m",
        "long",
        "limit",
        "pending",
        1.0,
        0.0,
        -1.0,
        2.0,
        True,
        True,
        datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 21, 10, 1, tzinfo=UTC),
        None,
        None,
        "entry",
        "position-1",
        "",
        "oco-1",
        False,
        {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": INSTRUMENT_PAPER_CONTRACT,
            "role": "take",
            "source": "manual",
            "payload": {"protection_basis": "absolute_structure"},
            "create_command": {
                "provider": "ibkr",
                "provider_contract_id": "123",
                "paper_contract": INSTRUMENT_PAPER_CONTRACT,
                "use_stop_loss": True,
                "use_target": True,
                "stop_loss": -1.0,
                "target": 2.0,
                "payload": {"protection_basis": "absolute_structure"},
            },
            "execution_command": {
                "provider": "ibkr",
                "provider_contract_id": "123",
                "paper_contract": INSTRUMENT_PAPER_CONTRACT,
                "use_stop_loss": True,
                "use_target": True,
                "stop_loss": -1.0,
                "target": 2.0,
                "payload": {"protection_basis": "absolute_structure"},
            },
        },
    )

    order = _paper_order_from_row(row)

    assert order["id"] == "order-1"
    assert order["role"] == "entry"
    assert order["position_id"] == "position-1"
    assert order["source"] == "manual"
    assert order["instrument_id"] == "ibkr|contract|123"
    assert order["route_fingerprint"] == "route-1"
    for role in ("entry", "stop", "take", "close"):
        assert _paper_order_from_row((*row[:16], role, *row[17:]))["role"] == role
    for role in (None, "", "legacy", "ENTRY", []):
        with pytest.raises(ValueError, match="PAPER_ORDER_ROW_ROLE_INVALID"):
            _paper_order_from_row((*row[:16], role, *row[17:]))
    for payload in (None, [], "legacy"):
        with pytest.raises(ValueError, match="PAPER_ORDER_ROW_PAYLOAD_INVALID"):
            _paper_order_from_row((*row[:21], payload))
    for payload in (
        {"route_fingerprint": "route-1"},
        {"instrument_id": "ibkr|contract|123"},
        {"instrument_id": 123, "route_fingerprint": "route-1"},
    ):
        with pytest.raises(ValueError):
            _paper_order_from_row((*row[:21], payload))
    with pytest.raises(ValueError, match="PAPER_ORDER_ROW_SHAPE_INVALID expected=22 actual=21"):
        _paper_order_from_row(row[:-1])
    with pytest.raises(ValueError, match="PAPER_ORDER_ROW_SHAPE_INVALID expected=22 actual=23"):
        _paper_order_from_row((*row, {}))
    with pytest.raises(ValueError, match="PAPER_ORDER_ID"):
        _paper_order_from_row((123, *row[1:]))
    with pytest.raises(TypeError):
        _paper_order_from_row((*row[:6], None, *row[7:]))
    missing_command = deepcopy(row[21])
    missing_command.pop("create_command")
    with pytest.raises(ValueError, match="PAPER_ORDER_ROW_CREATE_COMMAND_REQUIRED"):
        _paper_order_from_row((*row[:21], missing_command))
    contradictory = deepcopy(row[21])
    contradictory["execution_command"]["payload"] = {
        "protection_basis": "fill_distance",
        "paper_protection_intent": {
            "basis": "fill_distance",
            "stop_points": 1.0,
            "target_points": 2.0,
        },
    }
    with pytest.raises(ValueError, match="PAPER_ORDER_ROW_PROTECTION_COPIES_CONTRADICT"):
        _paper_order_from_row((*row[:21], contradictory))


@pytest.mark.parametrize("field", ("use_stop_loss", "use_target"))
def test_paper_order_command_requires_exact_boolean_flags(field: str) -> None:
    with pytest.raises(ValueError, match="PAPER_ORDER_COMMAND_INVALID"):
        _paper_order_command_payload(_qualified_order(**{field: "false"}))


def test_paper_fill_row_projects_and_requires_exact_route_identity() -> None:
    row = (
        1,
        "order-1",
        "position-1",
        "CL",
        "5m",
        "long",
        1.0,
        -1.25,
        "entry",
        False,
        None,
        datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": INSTRUMENT_PAPER_CONTRACT,
        },
    )

    fill = _paper_fill_from_row(row)

    assert fill["instrument_id"] == "ibkr|contract|123"
    assert fill["route_fingerprint"] == "route-1"
    for payload in (None, [], "legacy"):
        with pytest.raises(ValueError, match="PAPER_FILL_ROW_PAYLOAD_INVALID"):
            _paper_fill_from_row((*row[:12], payload))
    for payload in (
        {"route_fingerprint": "route-1"},
        {"instrument_id": "ibkr|contract|123"},
        {
            "instrument_id": 123,
            "route_fingerprint": "route-1",
        },
    ):
        with pytest.raises(ValueError):
            _paper_fill_from_row((*row[:12], payload))
    with pytest.raises(ValueError, match="PAPER_FILL_ROW_SHAPE_INVALID expected=13 actual=12"):
        _paper_fill_from_row(row[:-1])
    with pytest.raises(TypeError):
        _paper_fill_from_row((*row[:6], None, *row[7:]))


def test_paper_position_row_projects_and_requires_exact_route_identity() -> None:
    row = (
        "position-1",
        "CL",
        "5m",
        "open",
        1.0,
        0.0,
        datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 21, 10, 1, tzinfo=UTC),
        None,
        0.0,
        {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": INSTRUMENT_PAPER_CONTRACT,
        },
    )

    position = _paper_position_from_row(row)

    assert position["id"] == "position-1"
    assert position["instrument_id"] == "ibkr|contract|123"
    assert position["route_fingerprint"] == "route-1"
    for payload in (None, [], "legacy", {"instrument_id": "ibkr|contract|123"}):
        with pytest.raises(ValueError):
            _paper_position_from_row((*row[:10], payload))
    with pytest.raises(ValueError, match="PAPER_POSITION_ROW_SHAPE_INVALID"):
        _paper_position_from_row(row[:-1])
    for column in (4, 9):
        corrupt_row = (*row[:column], None, *row[column + 1 :])
        with pytest.raises(TypeError):
            _paper_position_from_row(corrupt_row)


def test_paper_create_and_patch_return_one_exact_order_route() -> None:
    route = SimpleNamespace(
        instrument_id="ibkr|contract|123",
        fingerprint="route-1",
        instrument_key="CL",
        provider="ibkr",
        instrument={"instrument_id": "ibkr|contract|123"},
        adapter=SimpleNamespace(session_contract_id=lambda _instrument: "123"),
    )
    action_deps = PaperActionDeps(
        paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
        queue_paper_trade_telegram=lambda *_args: None,
        paper_order_price_snapshot=lambda _order: None,
        instrument_lookup=lambda instrument_id: {"instrument_id": instrument_id},
        instrument_router=lambda _instrument: route,
    )

    class CreateStore:
        @staticmethod
        def upsert_paper_order(order: dict[str, object]) -> dict[str, object]:
            return dict(order)

        @staticmethod
        def apply_filled_paper_order(
            _order: dict[str, object],
        ) -> dict[str, object]:
            raise AssertionError("non-marketable order must not fill")

    create_payload = _qualified_order(
        id="po-00000000-0000-4000-8000-000000000001",
        order_type="limit",
    )
    for server_owned_field in (
        "symbol",
        "provider",
        "provider_contract_id",
        "payload",
    ):
        create_payload.pop(server_owned_field)
    created = create_paper_order_action(
        CreateStore(),
        create_payload,
        action_deps,
    )
    assert created["ok"] is True
    assert created["instrument_id"] == "ibkr|contract|123"
    assert created["route_fingerprint"] == "route-1"
    assert created["order"]["instrument_id"] == "ibkr|contract|123"
    assert created["order"]["route_fingerprint"] == "route-1"
    with pytest.raises(
        ValueError,
        match="PAPER_ORDER_CLIENT_PROTECTION_METADATA_FORBIDDEN",
    ):
        create_paper_order_action(
            CreateStore(),
            {
                **create_payload,
                "payload": {"protection_basis": "absolute_structure"},
            },
            action_deps,
        )

    calls: list[tuple[str, str, str]] = []

    class PatchStore:
        @staticmethod
        def read_paper_order(
            order_id: str,
            *,
            instrument_id: str,
            route_fingerprint: str,
        ) -> dict[str, object]:
            calls.append((order_id, instrument_id, route_fingerprint))
            return _qualified_order(
                id=order_id,
                order_type="limit",
                status="pending",
            )

        @staticmethod
        def update_paper_order_if_pending(
            order: dict[str, object],
            *,
            instrument_id: str,
            route_fingerprint: str,
        ) -> dict[str, object]:
            calls.append((str(order["id"]), instrument_id, route_fingerprint))
            return dict(order)

    patched = patch_paper_order_action(
        PatchStore(),
        "order-1",
        {"entry": 1.0, "stop_loss": 0.0, "target": 2.0},
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
    )
    assert patched["ok"] is True
    assert patched["instrument_id"] == "ibkr|contract|123"
    assert patched["route_fingerprint"] == "route-1"
    assert patched["order"]["id"] == "order-1"
    assert calls == [
        ("order-1", "ibkr|contract|123", "route-1"),
        ("order-1", "ibkr|contract|123", "route-1"),
    ]
    for field in ("use_stop_loss", "use_target"):
        for invalid_value in ("true", 1):
            with pytest.raises(ValueError, match=rf"{field} must be a boolean"):
                create_paper_order_action(
                    CreateStore(),
                    {**create_payload, field: invalid_value},
                    action_deps,
                )
            with pytest.raises(ValueError, match=rf"{field} must be a boolean"):
                patch_paper_order_action(
                    PatchStore(),
                    "order-1",
                    {field: invalid_value},
                    instrument_id="ibkr|contract|123",
                    route_fingerprint="route-1",
                )


@pytest.mark.parametrize("field", ["use_stop_loss", "use_target"])
@pytest.mark.parametrize("invalid_value", ["true", 1])
def test_paper_order_router_rejects_non_boolean_protective_flags(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_value: object,
) -> None:
    route = SimpleNamespace(
        instrument_id="ibkr|contract|123",
        fingerprint="route-1",
        instrument_key="CL",
        provider="ibkr",
        instrument={"instrument_id": "ibkr|contract|123"},
        adapter=SimpleNamespace(session_contract_id=lambda _instrument: "123"),
    )
    action_deps = PaperActionDeps(
        paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
        queue_paper_trade_telegram=lambda *_args: None,
        paper_order_price_snapshot=lambda _order: None,
        instrument_lookup=lambda instrument_id: {"instrument_id": instrument_id},
        instrument_router=lambda _instrument: route,
    )
    monkeypatch.setattr(trading_router, "PaperActionDeps", lambda **_kwargs: action_deps)

    class Store:
        @staticmethod
        def initialize() -> None:
            return None

        @staticmethod
        def read_paper_order(
            order_id: str,
            *,
            instrument_id: str,
            route_fingerprint: str,
        ) -> dict[str, object]:
            return _qualified_order(
                id=order_id,
                instrument_id=instrument_id,
                route_fingerprint=route_fingerprint,
                order_type="limit",
                status="pending",
            )

    router = create_trading_router(
        TradingRouterDeps(
            store_factory=Store,
            paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
            queue_paper_trade_telegram=lambda *_args: None,
            paper_trade_enriched=lambda trade: trade,
            paper_trade_stats=lambda _trades: {},
            paper_replay_summary=lambda _trades: {},
            paper_signal_source_key=lambda _trade: "",
            paper_setup_source_label=lambda _trade: "",
            paper_setup_key=lambda _trade: "",
            paper_trade_session=lambda _trade: "",
            paper_trade_edge_action=lambda _trade: "",
            paper_order_price_snapshot=lambda _order: None,
        )
    )
    create_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/paper/orders" and "POST" in route.methods
    )
    patch_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/paper/orders/{order_id}" and "PATCH" in route.methods
    )
    create_payload = {
        "id": "po-00000000-0000-4000-8000-000000000001",
        "instrument_id": "ibkr|contract|123",
        "route_fingerprint": "route-1",
        "timeframe": "5m",
        "side": "long",
        "order_type": "limit",
        "qty": 1.0,
        "entry": 0.0,
        "stop_loss": -1.0,
        "target": 2.0,
        field: invalid_value,
    }

    create_result = create_endpoint(create_payload)
    patch_result = patch_endpoint(
        "order-1",
        {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            field: invalid_value,
        },
    )

    assert create_result["error"]["code"] == "PAPER_ORDER_CREATE_INVALID"
    assert create_result["error"]["retryable"] is False
    assert create_result["message"] == f"{field} must be a boolean"
    assert patch_result["error"]["code"] == "PAPER_ORDER_UPDATE_INVALID"
    assert patch_result["error"]["retryable"] is False
    assert patch_result["message"] == f"{field} must be a boolean"


def test_paper_cancel_requires_and_returns_one_exact_order_route() -> None:
    cancelled_at = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    calls: list[tuple[object, ...]] = []

    class Store:
        @staticmethod
        def cancel_paper_order_if_pending(
            order_id: str,
            at: datetime,
            *,
            instrument_id: str,
            route_fingerprint: str,
        ) -> dict[str, object]:
            calls.append((order_id, at, instrument_id, route_fingerprint))
            return {
                **_qualified_order(),
                "id": order_id,
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "status": "cancelled",
            }

    result = cancel_paper_order_action(
        Store(),
        "order-1",
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        now=lambda: cancelled_at,
    )

    assert result["ok"] is True
    assert result["cancelled"] is True
    assert result["instrument_id"] == "ibkr|contract|123"
    assert result["route_fingerprint"] == "route-1"
    assert result["order"]["id"] == "order-1"
    assert calls == [("order-1", cancelled_at, "ibkr|contract|123", "route-1")]


def test_paper_cancel_rejects_missing_or_mismatched_exact_identity() -> None:
    class MissingStore:
        @staticmethod
        def cancel_paper_order_if_pending(*_args: object, **_kwargs: object) -> None:
            return None

    missing = cancel_paper_order_action(
        MissingStore(),
        "order-1",
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
    )
    assert missing["ok"] is False
    assert missing["code"] == "PAPER_ORDER_NOT_PENDING"

    class MismatchStore:
        @staticmethod
        def cancel_paper_order_if_pending(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {
                **_qualified_order(),
                "status": "cancelled",
                "route_fingerprint": "route-2",
            }

    with pytest.raises(ValueError, match="PAPER_ORDER_CANCEL_RESULT_IDENTITY_MISMATCH"):
        cancel_paper_order_action(
            MismatchStore(),
            "order-1",
            instrument_id="ibkr|contract|123",
            route_fingerprint="route-1",
        )
    with pytest.raises(ValueError):
        cancel_paper_order_action(
            MissingStore(),
            "order-1",
            instrument_id=123,  # type: ignore[arg-type]
            route_fingerprint="route-1",
        )


def test_paper_signal_contract_accepts_signed_absolute_prices() -> None:
    trade = paper_trade_from_signal(
        "CL",
        "5m",
        {
            "source": "trade_setup",
            "setup": "fade",
            "action": "GO",
            "side": "long",
            "entry": 0.0,
            "stop": -1.0,
            "target": 2.0,
            "ts": "2026-07-21T10:00:00+00:00",
        },
        config={"edge_gate": False, "min_rr": 1.25},
        edge_filter_loader=lambda: [],
        instrument_id="ibkr|contract|123",
    )

    assert trade is not None
    assert trade["entry"] == 0.0
    assert trade["stop"] == -1.0
    assert trade["target"] == 2.0


def test_paper_signal_contract_rejects_coerced_prices_and_naive_time() -> None:
    raw = {
        "source": "trade_setup",
        "setup": "fade",
        "action": "GO",
        "side": "long",
        "entry": 100.0,
        "stop": 98.0,
        "target": 104.0,
        "ts": "2026-07-21T10:00:00+00:00",
    }
    kwargs = {
        "config": {"edge_gate": False, "min_rr": 1.25},
        "edge_filter_loader": lambda: [],
        "instrument_id": "ibkr|contract|123",
    }

    assert paper_trade_from_signal("CL", "5m", {**raw, "entry": "100"}, **kwargs) is None
    assert (
        paper_trade_from_signal(
            "CL",
            "5m",
            {**raw, "ts": "2026-07-21T10:00:00"},
            **kwargs,
        )
        is None
    )


def test_paper_signal_id_changes_with_analysis_generation_and_plan() -> None:
    raw = {
        "source": "trade_setup",
        "setup": "fade",
        "action": "GO",
        "side": "long",
        "entry": 100.0,
        "stop": 98.0,
        "target": 104.0,
        "ts": "2026-07-21T10:00:00+00:00",
        "analysis_generation": "window-hash-1",
    }
    kwargs = {
        "config": {"edge_gate": False, "min_rr": 1.25},
        "edge_filter_loader": lambda: [],
        "instrument_id": "ibkr|contract|123",
    }

    first = paper_trade_from_signal("CL", "5m", raw, **kwargs)
    next_generation = paper_trade_from_signal(
        "CL",
        "5m",
        {**raw, "analysis_generation": "window-hash-2"},
        **kwargs,
    )
    corrected_plan = paper_trade_from_signal(
        "CL",
        "5m",
        {**raw, "target": 105.0},
        **kwargs,
    )

    assert first is not None
    assert next_generation is not None
    assert corrected_plan is not None
    assert len({first["id"], next_generation["id"], corrected_plan["id"]}) == 3


def test_manual_paper_close_uses_current_server_owned_paper_reference() -> None:
    def stored_position(*, status: str = "open") -> dict[str, object]:
        return {
            "id": "position-1",
            "symbol": "CL",
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "paper_contract": INSTRUMENT_PAPER_CONTRACT,
            "timeframe": "5m",
            "status": status,
            "qty": 1.0 if status == "open" else 0.0,
            "avg_entry": -1.0,
            "realized_pnl": 1.0 if status == "closed" else 0.0,
            "closed_at": "2026-07-21T10:00:00+00:00" if status == "closed" else None,
            "payload": {
                "instrument_id": "ibkr|contract|123",
                "route_fingerprint": "route-1",
                "provider": "ibkr",
                "provider_contract_id": "123",
                "paper_contract": INSTRUMENT_PAPER_CONTRACT,
                "side": "long",
            },
        }

    class Store:
        saved_order: dict[str, object] | None = None

        @staticmethod
        def upsert_paper_order(
            order: dict[str, object],
        ) -> dict[str, object]:
            return {
                **order,
                "create_command": _paper_order_command_payload(order),
            }

        @staticmethod
        def read_history_coverage(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("manual reduce-only close must not read history coverage")

        @staticmethod
        def read_paper_positions(**kwargs: object) -> list[dict[str, object]]:
            assert kwargs["status"] is None
            assert kwargs["instrument_id"] == "ibkr|contract|123"
            assert kwargs["route_fingerprint"] == "route-1"
            assert kwargs["complete_position_ids"] == ("position-1",)
            return [stored_position()]

        @staticmethod
        def read_paper_order(*_args: object, **_kwargs: object) -> None:
            return None

        def apply_filled_paper_order(self, order: dict[str, object]) -> dict[str, object]:
            self.saved_order = order
            return {
                "outcome": "filled",
                "order": {**order, "status": "filled"},
                "position": stored_position(status="closed"),
                "fill": {"id": "fill-1", "position_id": "position-1"},
                "created_orders": [],
                "cancelled_orders": [],
                "closed_position": None,
                "opened_new_position": False,
            }

    store = Store()
    deps = PaperActionDeps(
        paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
        queue_paper_trade_telegram=lambda *_args: None,
        paper_order_price_snapshot=lambda _order: {
            "price": 0.25,
            "bid": 0.0,
            "ask": 0.5,
            "bid_ask_status": "live",
            "bbo_ts": datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            "low": 0.0,
            "high": 0.5,
            "range_kind": "quote",
            "ts": datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            "source": "test:bbo",
        },
        now=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )

    result = close_paper_trade_action(
        store,
        "position-1",
        {"command_id": "pc-00000000-0000-4000-8000-000000000011"},
        deps,
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        paper_contract=INSTRUMENT_PAPER_CONTRACT,
    )

    assert result["ok"] is True
    assert result["outcome"] == "filled"
    assert result["command_id"] == "pc-00000000-0000-4000-8000-000000000011"
    assert store.saved_order is not None
    assert store.saved_order["id"] == result["command_id"]
    assert store.saved_order["entry"] == -1.0
    assert store.saved_order["fill_price"] == 0.25
    assert store.saved_order["create_command"]["entry"] is None
    assert store.saved_order["side"] == "short"
    assert store.saved_order["qty"] == 1.0
    assert store.saved_order["create_command"]["side"] is None
    assert store.saved_order["create_command"]["qty"] is None

    mark_deps = PaperActionDeps(
        paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
        queue_paper_trade_telegram=lambda *_args: None,
        paper_order_price_snapshot=lambda _order: None,
        now=lambda: datetime(2026, 7, 21, 10, 1, tzinfo=UTC),
    )
    pending_store = Store()
    result = close_paper_trade_action(
        pending_store,
        "position-1",
        {"command_id": "pc-00000000-0000-4000-8000-000000000012"},
        mark_deps,
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        paper_contract=INSTRUMENT_PAPER_CONTRACT,
    )
    assert result["ok"] is True
    assert result["outcome"] == "pending"
    assert result["code"] == "PAPER_EXIT_PRICE_UNAVAILABLE_OLD_ROUTE"
    assert pending_store.saved_order is None

    invalid = close_paper_trade_action(
        store,
        "position-1",
        {
            "command_id": "pc-00000000-0000-4000-8000-000000000013",
            "exit_price": 0.0,
        },
        mark_deps,
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        paper_contract=INSTRUMENT_PAPER_CONTRACT,
    )
    assert invalid["code"] == "PAPER_CLOSE_CLIENT_PRICE_FORBIDDEN"


def test_manual_paper_close_is_typed_no_op_when_position_is_already_closed() -> None:
    closed_position = {
        "id": "position-1",
        "symbol": "CL",
        "instrument_id": "ibkr|contract|123",
        "route_fingerprint": "route-1",
        "paper_contract": INSTRUMENT_PAPER_CONTRACT,
        "timeframe": "5m",
        "status": "closed",
        "qty": 0.0,
        "avg_entry": -1.0,
        "realized_pnl": 1.0,
        "closed_at": "2026-07-21T10:00:00+00:00",
        "payload": {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": INSTRUMENT_PAPER_CONTRACT,
            "side": "long",
        },
    }

    class Store:
        @staticmethod
        def read_paper_positions(**kwargs: object) -> list[dict[str, object]]:
            assert kwargs["status"] is None
            return [closed_position]

        @staticmethod
        def read_paper_order(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def apply_filled_paper_order(_order: dict[str, object]) -> dict[str, object]:
            raise AssertionError("already-closed no-op must not apply a position delta")

    notifications: list[tuple[str, dict[str, object]]] = []
    deps = PaperActionDeps(
        paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
        queue_paper_trade_telegram=lambda event, trade: notifications.append((event, trade)),
        paper_order_price_snapshot=lambda _order: None,
    )
    command_id = "pc-00000000-0000-4000-8000-000000000014"

    result = close_paper_trade_action(
        Store(),
        "position-1",
        {"command_id": command_id},
        deps,
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        paper_contract=INSTRUMENT_PAPER_CONTRACT,
    )

    assert result["ok"] is True
    assert result["outcome"] == "no_op"
    assert result["command_id"] == command_id
    assert result["position"] == closed_position
    assert result["trade"]["status"] == "closed"
    assert notifications == []


def test_manual_paper_close_replays_matching_cancelled_command_as_no_op() -> None:
    closed_position = {
        "id": "position-1",
        "symbol": "CL",
        "instrument_id": "ibkr|contract|123",
        "route_fingerprint": "route-1",
        "paper_contract": INSTRUMENT_PAPER_CONTRACT,
        "timeframe": "5m",
        "status": "closed",
        "qty": 0.0,
        "avg_entry": -1.0,
        "realized_pnl": 1.0,
        "closed_at": "2026-07-21T10:00:00+00:00",
        "payload": {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "paper_contract": INSTRUMENT_PAPER_CONTRACT,
            "side": "long",
        },
    }
    command_id = "pc-00000000-0000-4000-8000-000000000015"
    applied: list[dict[str, object]] = []

    class Store:
        @staticmethod
        def read_paper_positions(**_kwargs: object) -> list[dict[str, object]]:
            return [closed_position]

        @staticmethod
        def read_paper_order(
            order_id: str,
            *,
            instrument_id: str,
            route_fingerprint: str,
            paper_contract: PaperContractIdentity,
        ) -> dict[str, object]:
            assert (order_id, instrument_id, route_fingerprint) == (
                command_id,
                "ibkr|contract|123",
                "route-1",
            )
            assert paper_contract.to_payload() == INSTRUMENT_PAPER_CONTRACT
            return {
                "id": command_id,
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "status": "cancelled",
            }

        @staticmethod
        def apply_filled_paper_order(
            order: dict[str, object],
        ) -> dict[str, object]:
            applied.append(order)
            return {
                "outcome": "no_op",
                "order": order,
                "position": closed_position,
                "fill": None,
                "created_orders": [],
                "cancelled_orders": [],
                "closed_position": None,
                "opened_new_position": False,
            }

    result = close_paper_trade_action(
        Store(),
        "position-1",
        {"command_id": command_id},
        PaperActionDeps(
            paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
            queue_paper_trade_telegram=lambda *_args: None,
            paper_order_price_snapshot=lambda _order: None,
        ),
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        paper_contract=INSTRUMENT_PAPER_CONTRACT,
    )

    assert result["outcome"] == "no_op"
    assert applied[0]["status"] == "cancelled"


def test_manual_paper_close_requires_canonical_client_command_uuid() -> None:
    deps = PaperActionDeps(
        paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
        queue_paper_trade_telegram=lambda *_args: None,
        paper_order_price_snapshot=lambda _order: None,
    )

    with pytest.raises(ValueError, match="PAPER_CLOSE_CLIENT_UUID_INVALID"):
        close_paper_trade_action(
            object(),
            "position-1",
            {"command_id": "pc-not-a-uuid"},
            deps,
            instrument_id="ibkr|contract|123",
            route_fingerprint="route-1",
            paper_contract=INSTRUMENT_PAPER_CONTRACT,
        )


def test_paper_repo_admits_zero_prices_but_rejects_non_finite_values_and_quantity() -> None:
    class ReachedStorage(Exception):
        pass

    class Repo(PostgresStore):
        def __init__(self) -> None:
            super().__init__(
                None,
                canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
            )

        @staticmethod
        def _connect() -> object:
            raise ReachedStorage

    repo = Repo()
    with pytest.raises(ReachedStorage):
        repo.upsert_paper_order(_qualified_order(status="pending"))
    with pytest.raises(ValueError, match="PAPER_DATETIME_INVALID"):
        repo.upsert_paper_order(_qualified_order(status="pending", created_at="not-a-time"))
    with pytest.raises(ValueError, match="PAPER_DATETIME_TIMEZONE_REQUIRED"):
        repo.upsert_paper_order(
            _qualified_order(status="pending", created_at="2026-07-21T10:00:00")
        )
    with pytest.raises(ReachedStorage):
        repo.apply_filled_paper_order(_qualified_order())
    with pytest.raises(ValueError, match="PAPER_DATETIME_INVALID"):
        repo.apply_filled_paper_order(_qualified_order(filled_at="not-a-time"))
    with pytest.raises(ValueError, match="PAPER_REDUCE_ONLY_POSITION_ID_REQUIRED"):
        repo.apply_filled_paper_order(
            _qualified_order(
                role="close",
                reduce_only=True,
                position_id="",
            )
        )
    with pytest.raises(ReachedStorage):
        repo.apply_filled_paper_order(
            _qualified_order(
                role="close",
                reduce_only=True,
                position_id="position-1",
            )
        )
    with pytest.raises(ValueError):
        repo.apply_filled_paper_order(_qualified_order(fill_price=float("nan")))
    with pytest.raises(ValueError):
        repo.upsert_paper_order(_qualified_order(status="pending", qty=0.0))

    source = inspect.getsource(_paper_fill_command) + inspect.getsource(
        PaperRepoMixin._locked_paper_fill_position
    )
    assert "target_position_id" in source
    assert "AND (%s::text IS NULL OR id = %s)" in source
    assert 'position.get("avg_entry") or price' not in source
    assert 'order.get("entry") or price' not in source


def test_paper_position_does_not_conflate_indicator_and_setup_sources() -> None:
    payload = _paper_position_payload_from_order(
        "order-1",
        {
            **_qualified_order(),
            "indicator_source": "trade_setup",
        },
        1.0,
    )

    assert payload["indicator_source"] == "trade_setup"
    assert "setup_source" not in payload
    source = inspect.getsource(PaperRepoMixin.apply_filled_paper_order)
    assert (
        'order_payload.get("setup_source") or order_payload.get("indicator_source")' not in source
    )


def test_paper_position_projection_requires_exact_persisted_side() -> None:
    position = {
        "id": "position-1",
        "symbol": "CL",
        "timeframe": "5m",
        "status": "open",
        "qty": -1.0,
        "avg_entry": 70.0,
        "payload": {
            "instrument_id": "ibkr|contract|123",
            "route_fingerprint": "route-1",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "side": "short",
        },
    }

    assert position_trade_from_storage(position)["side"] == "short"
    for invalid_side in (None, "", "SHORT", "sell"):
        with pytest.raises(ValueError, match="PAPER_POSITION_SIDE_INVALID"):
            position_trade_from_storage(
                {
                    **position,
                    "payload": {**position["payload"], "side": invalid_side},
                }
            )
