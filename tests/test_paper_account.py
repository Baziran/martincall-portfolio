from aef_terminal.paper_contract import PaperContractIdentity
from aef_terminal.ui.paper.account import paper_account_payload
from aef_terminal.ui.paper.metrics import paper_trade_pnl_value
from aef_terminal.ui.paper.positions import paper_open_trade_mark_pnl


def test_paper_account_payload_reports_result_without_fake_equity() -> None:
    payload = paper_account_payload(
        [
            {
                "status": "closed",
                "realized_pnl": 12.5,
                "pnl_unit": "points",
            },
            {
                "status": "open",
                "unrealized_pnl": 3.25,
                "pnl_unit": "points",
            },
        ]
    )

    assert payload["available"] is True
    assert payload["unit"] == "points"
    assert payload["realized_pnl"] == 12.5
    assert payload["unrealized_pnl"] == 3.25
    assert payload["net_pnl"] == 15.75
    assert "starting_capital" not in payload
    assert "equity" not in payload


def test_paper_account_rejects_mixed_result_units() -> None:
    payload = paper_account_payload(
        [
            {"status": "closed", "realized_pnl": 1.0, "pnl_unit": "points"},
            {"status": "open", "unrealized_pnl": 100.0, "pnl_unit": "USD"},
        ]
    )

    assert payload == {
        "available": False,
        "unit": None,
        "realized_pnl": None,
        "unrealized_pnl": None,
        "net_pnl": None,
    }


def test_option_pnl_uses_physical_contract_multiplier_and_currency() -> None:
    option_contract = PaperContractIdentity(
        scope_kind="option",
        provider="ibkr",
        provider_contract_id="9001",
        sec_type="OPT",
        con_id=9001,
        exchange="SMART",
        expiry="20260918",
        expiry_at="2026-09-18T20:00:00+00:00",
        strike=500.0,
        right="C",
        trading_class="SPY",
        multiplier=100.0,
        currency="USD",
    )

    assert paper_trade_pnl_value(
        {
            "provider": "ibkr",
            "provider_contract_id": "9001",
            "paper_contract": option_contract.to_payload(),
        },
        2.5,
    ) == (250.0, "USD")
    assert paper_trade_pnl_value(
        {
            "provider": "ibkr",
            "provider_contract_id": "ESM6",
            "paper_contract": PaperContractIdentity(
                scope_kind="instrument",
                provider="ibkr",
                provider_contract_id="ESM6",
            ).to_payload(),
        },
        2.5,
    ) == (2.5, "points")


def test_open_option_mark_pnl_applies_contract_multiplier(monkeypatch) -> None:
    contract = PaperContractIdentity(
        scope_kind="option",
        provider="ibkr",
        provider_contract_id="9002",
        sec_type="OPT",
        con_id=9002,
        exchange="SMART",
        expiry="20260918",
        expiry_at="2026-09-18T20:00:00+00:00",
        strike=500.0,
        right="C",
        trading_class="SPY",
        multiplier=100.0,
        currency="USD",
    )
    monkeypatch.setattr(
        "aef_terminal.ui.paper.option_market.paper_option_mark_price",
        lambda _trade: 5.5,
    )

    projected = paper_open_trade_mark_pnl(
        {
            "status": "open",
            "side": "long",
            "entry": 5.0,
            "qty": 2.0,
            "provider": "ibkr",
            "provider_contract_id": "9002",
            "paper_contract": contract.to_payload(),
        }
    )

    assert projected["mark_price"] == 5.5
    assert projected["unrealized_points"] == 1.0
    assert projected["unrealized_pnl"] == 100.0
    assert projected["pnl_unit"] == "USD"
