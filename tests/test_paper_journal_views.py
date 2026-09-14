from aef_terminal.ui.paper.journal_views import (
    PaperJournalViewDeps,
    paper_trades_csv_response,
    paper_trades_table_response,
)


def _deps() -> PaperJournalViewDeps:
    return PaperJournalViewDeps(
        signal_source_key=lambda trade: str(trade.get("source_key") or "manual"),
        setup_source_label=lambda trade: str(trade.get("setup_source_label") or "manual"),
        setup_key=lambda trade: str(trade.get("setup") or ""),
        trade_session=lambda trade: str(trade.get("session") or ""),
        trade_edge_action=lambda trade: str(trade.get("edge_action") or ""),
    )


def test_paper_trades_csv_response_uses_journal_view_contract() -> None:
    response = paper_trades_csv_response(
        {
            "trades": [
                {
                    "id": "pos-1",
                    "status": "closed",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "source": "manual_position",
                    "source_key": "manual",
                    "side": "long",
                    "entry": 100.0,
                    "payload": {"score": 91, "rr": 2.4},
                }
            ]
        },
        _deps(),
    )
    body = response.body.decode("utf-8")

    assert response.media_type == "text/csv; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="martincall-paper-trades.csv"'
    )
    assert "id,status,symbol,timeframe,source,raw_source" in body
    assert "pos-1,closed,ES,5m,manual,manual_position" in body
    assert ",91,2.4," in body
    assert "paper_inverted" not in body


def test_paper_trades_table_response_escapes_trade_values() -> None:
    response = paper_trades_table_response(
        {
            "trades": [
                {
                    "id": "pos-1",
                    "status": "<open>",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "source": '"manual"',
                    "source_key": "<manual>",
                    "side": "long",
                    "entry": 100.0,
                    "payload": {"score": "<91>"},
                }
            ],
            "stats": {"total": 1, "open": 1, "closed": 0},
            "account": {
                "available": True,
                "unit": "points",
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "net_pnl": 0.0,
            },
        },
        _deps(),
        instrument_id="ibkr|contract|123",
        route_fingerprint="route-1",
        limit=2000,
    )
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "MartinCall Paper Trades" in body
    assert (
        "/api/paper/trades.csv?instrument_id=ibkr%7Ccontract%7C123"
        "&amp;route_fingerprint=route-1&amp;limit=1000"
    ) in body
    assert "&lt;open&gt;" in body
    assert "&lt;manual&gt;" in body
    assert "<open>" not in body
