from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.paper_contract import PAPER_CONTRACT_KEY, PaperContractIdentity
from aef_terminal.data.ibkr import options as ibkr_options
from aef_terminal.ui.paper import actions as paper_actions
from aef_terminal.ui.paper.actions import (
    PaperActionDeps,
    close_paper_trade_action,
    create_option_paper_order_action,
)
from aef_terminal.ui.paper.option_market import (
    clear_paper_option_quote_cache,
    paper_option_execution_snapshot,
    refresh_demanded_paper_option_quotes,
    require_option_paper_limit_price,
    set_paper_option_quote_demand,
)
from aef_terminal.ui.services import option_board_stream_ws
from aef_terminal.ui.services.option_board_stream_ws import (
    _OptionBoardProducer,
    resolve_option_board_paper_contract,
)


INSTRUMENT_ID = "ibkr|instrument|option-drive"
ROUTE_FINGERPRINT = "ibkr|route|option-drive"
NOW = datetime(2026, 8, 9, 14, 30, tzinfo=UTC)
PRICE_INCREMENTS = [
    {"low_edge": 0.0, "increment": 0.05},
    {"low_edge": 5.0, "increment": 0.10},
    {"low_edge": 20.25, "increment": 0.25},
]


def _contract() -> PaperContractIdentity:
    return PaperContractIdentity(
        scope_kind="option",
        provider="ibkr",
        provider_contract_id="9001",
        sec_type="OPT",
        con_id=9001,
        exchange="SMART",
        expiry="20260809",
        expiry_at="2026-08-09T20:00:00+00:00",
        strike=500.0,
        right="C",
        trading_class="SPY",
        multiplier=100.0,
        currency="USD",
    )


def _quote() -> dict[str, Any]:
    return {
        **_contract().to_payload(),
        "local_symbol": "SPY   260809C00500000",
        "bid": 4.9,
        "ask": 5.0,
        "last": 8.75,
        "price_increments": PRICE_INCREMENTS,
        "received_at": NOW.isoformat(),
        "bid_ask_received_at": NOW.isoformat(),
        "ts": NOW.isoformat(),
        "time_basis": "client_receive",
        "market_data_entitlement": "live",
        "is_delayed": False,
    }


def _order(side: str) -> dict[str, Any]:
    contract = _contract()
    return {
        "provider": contract.provider,
        "provider_contract_id": contract.provider_contract_id,
        PAPER_CONTRACT_KEY: contract.to_payload(),
        "side": side,
    }


@pytest.fixture(autouse=True)
def _clear_option_drive_state() -> None:
    option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
    clear_paper_option_quote_cache()
    yield
    option_board_stream_ws._OPTION_BOARD_PRODUCERS.clear()
    clear_paper_option_quote_cache()


def test_option_drive_limit_validation_uses_exact_provider_price_bands() -> None:
    assert (
        require_option_paper_limit_price(
            4.95,
            price_increments=PRICE_INCREMENTS,
        )
        == 4.95
    )
    assert (
        require_option_paper_limit_price(
            5.1,
            price_increments=PRICE_INCREMENTS,
        )
        == 5.1
    )
    assert (
        require_option_paper_limit_price(
            20.25,
            price_increments=PRICE_INCREMENTS,
        )
        == 20.25
    )
    with pytest.raises(ValueError, match="PAPER_OPTION_LIMIT_PRICE_TICK_INVALID"):
        require_option_paper_limit_price(
            5.05,
            price_increments=PRICE_INCREMENTS,
        )
    with pytest.raises(ValueError, match="PAPER_OPTION_LIMIT_PRICE_TICK_INVALID"):
        require_option_paper_limit_price(
            20.35,
            price_increments=PRICE_INCREMENTS,
        )
    with pytest.raises(ValueError, match="PAPER_OPTION_PRICE_RULE_UNAVAILABLE"):
        require_option_paper_limit_price(5.1, price_increments=[])


def test_option_drive_execution_uses_live_bbo_midpoint_as_paper_reference() -> None:
    quote = _quote()

    buy = paper_option_execution_snapshot(_order("long"), quote, now=NOW)
    sell = paper_option_execution_snapshot(_order("short"), quote, now=NOW)

    assert buy is not None
    assert buy["price"] == 4.95
    assert buy["source"] == "ibkr:option-mid"
    assert sell is not None
    assert sell["price"] == 4.95
    assert sell["source"] == "ibkr:option-mid"
    delayed = {**quote, "market_data_entitlement": "delayed", "is_delayed": True}
    assert paper_option_execution_snapshot(_order("long"), delayed, now=NOW) is None


def test_option_drive_execution_does_not_treat_generic_packet_time_as_bbo_time() -> None:
    quote = _quote()
    quote["bid_ask_received_at"] = (NOW - timedelta(seconds=20)).isoformat()
    quote["received_at"] = NOW.isoformat()
    quote["ts"] = NOW.isoformat()

    assert paper_option_execution_snapshot(_order("long"), quote, now=NOW) is None

    quote.pop("bid_ask_received_at")
    assert paper_option_execution_snapshot(_order("short"), quote, now=NOW) is None


def test_option_market_close_rejects_client_execution_price() -> None:
    class Store:
        @staticmethod
        def read_paper_positions(**_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("invalid option close must fail before journal reads")

    result = close_paper_trade_action(
        Store(),
        "position-option-1",
        {
            "command_id": "pc-00000000-0000-4000-8000-000000000901",
            "exit_price": 4.95,
        },
        PaperActionDeps(
            paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
            queue_paper_trade_telegram=lambda *_args: None,
            paper_order_price_snapshot=lambda _order: None,
            now=lambda: NOW,
        ),
        instrument_id=INSTRUMENT_ID,
        route_fingerprint=ROUTE_FINGERPRINT,
        paper_contract=_contract().to_payload(),
    )

    assert result == {
        "ok": False,
        "code": "PAPER_CLOSE_CLIENT_PRICE_FORBIDDEN",
        "message": "paper close is resolved from the current server-owned paper reference",
        "retryable": False,
    }


def test_paper_option_quote_refresh_prioritizes_reduce_only_demand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_contract = _contract()
    entry_contract = replace(
        close_contract,
        provider_contract_id="9002",
        con_id=9002,
        strike=501.0,
    )
    refreshed: list[int] = []
    released: list[int] = []

    def live_quote(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        refreshed.append(int(payload["con_id"]))
        return _quote()

    def cancel_quote(payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        released.append(int(payload["con_id"]))
        return {"ok": True}

    monkeypatch.setattr(ibkr_options, "live_option_quote", live_quote)
    monkeypatch.setattr(ibkr_options, "cancel_option_quote", cancel_quote)
    assert (
        set_paper_option_quote_demand(
            [
                {
                    "provider": entry_contract.provider,
                    "provider_contract_id": entry_contract.provider_contract_id,
                    PAPER_CONTRACT_KEY: entry_contract.to_payload(),
                    "status": "pending",
                },
                {
                    "provider": close_contract.provider,
                    "provider_contract_id": close_contract.provider_contract_id,
                    PAPER_CONTRACT_KEY: close_contract.to_payload(),
                    "reduce_only": True,
                },
            ]
        )
        == 2
    )

    result = refresh_demanded_paper_option_quotes()

    assert refreshed == [9001, 9002]
    assert result.demanded == 2
    assert result.refreshed == 2
    assert result.failed == 0
    assert set_paper_option_quote_demand([]) == 0
    released_result = refresh_demanded_paper_option_quotes()
    assert released == [9001, 9002]
    assert released_result.released == 2


def test_option_board_resolves_current_conid_to_server_owned_paper_contract() -> None:
    contract = _contract()
    quote = _quote()
    route = SimpleNamespace(provider="ibkr", fingerprint=ROUTE_FINGERPRINT)
    producer = _OptionBoardProducer(
        key=(INSTRUMENT_ID, ROUTE_FINGERPRINT, "0dte"),
        instrument_id=INSTRUMENT_ID,
        provider_symbol="SPY",
        expiry_mode="0dte",
        route=route,
        consumer_id="option-board:test",
        revision=7,
        snapshot={
            "instrument_id": INSTRUMENT_ID,
            "route_fingerprint": ROUTE_FINGERPRINT,
            "expiry_mode": "0dte",
            "captured_at": NOW.isoformat(),
            "series": [
                {
                    "target_dte": "0dte",
                    "rows": [
                        {
                            "strike": contract.strike,
                            "call": quote,
                            "put": {**quote, "con_id": 9002, "right": "P"},
                        }
                    ],
                }
            ],
        },
    )
    option_board_stream_ws._OPTION_BOARD_PRODUCERS[producer.key] = producer

    resolved = resolve_option_board_paper_contract(
        instrument_id=INSTRUMENT_ID,
        route_fingerprint=ROUTE_FINGERPRINT,
        expiry_mode="0dte",
        snapshot_revision=6,
        con_id=9001,
        now=NOW,
    )

    assert resolved["contract"] == contract
    assert resolved["quote"]["last"] == 8.75
    assert resolved["requested_revision"] == 6
    assert resolved["resolved_revision"] == 7
    with pytest.raises(ValueError, match="OPTION_BOARD_ORDER_CONTRACT_NOT_CURRENT"):
        resolve_option_board_paper_contract(
            instrument_id=INSTRUMENT_ID,
            route_fingerprint=ROUTE_FINGERPRINT,
            expiry_mode="0dte",
            snapshot_revision=7,
            con_id=9999,
            now=NOW,
        )


def test_option_order_action_builds_exact_limit_command_and_uses_paper_midpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    captured: dict[str, Any] = {}

    def submit(
        _store: Any,
        order: dict[str, Any],
        snapshot: dict[str, Any] | None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        captured["order"] = order
        captured["snapshot"] = snapshot
        return {
            "outcome": "pending",
            "order": order,
            "replayed": False,
            "result": None,
            "code": "",
            "message": "",
            "execution": {},
        }

    monkeypatch.setattr(paper_actions, "submit_paper_order_execution", submit)
    route = SimpleNamespace(
        instrument_id=INSTRUMENT_ID,
        fingerprint=ROUTE_FINGERPRINT,
        provider="ibkr",
    )
    payload = {
        "id": "po-00000000-0000-4000-8000-000000000501",
        "instrument_id": INSTRUMENT_ID,
        "route_fingerprint": ROUTE_FINGERPRINT,
        "timeframe": "1m",
        "expiry_mode": "0dte",
        "snapshot_revision": 4,
        "con_id": 9001,
        "side": "long",
        "order_type": "limit",
        "qty": 2,
        "entry": 5.0,
    }
    result = create_option_paper_order_action(
        object(),
        payload,
        PaperActionDeps(
            paper_config=lambda: {"min_rr": 1.25, "edge_gate": True},
            queue_paper_trade_telegram=lambda *_args: None,
            paper_order_price_snapshot=lambda _order: None,
            now=lambda: NOW,
            instrument_lookup=lambda _instrument_id: {"instrument_id": INSTRUMENT_ID},
            instrument_router=lambda _instrument: route,
        ),
        option_contract_resolver=lambda **_kwargs: {
            "contract": contract,
            "quote": _quote(),
            "provider_symbol": "SPY",
            "requested_revision": 4,
            "resolved_revision": 5,
        },
    )

    assert result["ok"] is True
    assert result["filled"] is False
    assert result[PAPER_CONTRACT_KEY] == contract.to_payload()
    assert captured["order"][PAPER_CONTRACT_KEY] == contract.to_payload()
    assert captured["order"]["order_type"] == "limit"
    assert captured["order"]["qty"] == 2.0
    assert captured["snapshot"]["price"] == 4.95
    assert captured["snapshot"]["source"] == "ibkr:option-mid"
