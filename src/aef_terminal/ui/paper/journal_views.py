from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse, Response


@dataclass(frozen=True)
class PaperJournalViewDeps:
    signal_source_key: Callable[[dict[str, Any]], str]
    setup_source_label: Callable[[dict[str, Any]], str]
    setup_key: Callable[[dict[str, Any]], str]
    trade_session: Callable[[dict[str, Any]], str]
    trade_edge_action: Callable[[dict[str, Any]], str]


def paper_trades_csv_response(
    payload: dict[str, Any],
    deps: PaperJournalViewDeps,
) -> Response:
    output = io.StringIO()
    fields = [
        "id",
        "status",
        "symbol",
        "timeframe",
        "source",
        "raw_source",
        "setup_source_label",
        "side",
        "entry",
        "stop",
        "target",
        "opened_at",
        "closed_at",
        "exit_price",
        "exit_reason",
        "pnl_points",
        "realized_pnl",
        "qty",
        "mark_price",
        "unrealized_points",
        "unrealized_pnl",
        "pnl_unit",
        "setup",
        "score",
        "rr",
        "session",
        "edge_action",
        "mfe_points",
        "mae_points",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for trade in payload.get("trades", []):
        payload_body = trade.get("payload") if isinstance(trade.get("payload"), dict) else {}
        writer.writerow(
            {
                "id": trade.get("id"),
                "status": trade.get("status"),
                "symbol": trade.get("symbol"),
                "timeframe": trade.get("timeframe"),
                "source": deps.signal_source_key(trade),
                "raw_source": trade.get("source"),
                "setup_source_label": deps.setup_source_label(trade),
                "side": trade.get("side"),
                "entry": trade.get("entry"),
                "stop": trade.get("stop"),
                "target": trade.get("target"),
                "opened_at": trade.get("opened_at"),
                "closed_at": trade.get("closed_at"),
                "exit_price": trade.get("exit_price"),
                "exit_reason": trade.get("exit_reason"),
                "pnl_points": trade.get("pnl_points"),
                "realized_pnl": trade.get("realized_pnl"),
                "qty": trade.get("qty"),
                "mark_price": trade.get("mark_price"),
                "unrealized_points": trade.get("unrealized_points"),
                "unrealized_pnl": trade.get("unrealized_pnl"),
                "pnl_unit": trade.get("pnl_unit"),
                "setup": deps.setup_key(trade),
                "score": payload_body.get("score"),
                "rr": payload_body.get("rr"),
                "session": deps.trade_session(trade),
                "edge_action": deps.trade_edge_action(trade),
                "mfe_points": payload_body.get("mfe_points"),
                "mae_points": payload_body.get("mae_points"),
            }
        )
    headers = {"Content-Disposition": 'attachment; filename="martincall-paper-trades.csv"'}
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


def _table_cell(value: Any) -> str:
    return escape("" if value is None else str(value))


def _metric_cell(value: Any) -> str:
    return escape("-" if value is None else str(value))


def paper_trades_table_response(
    payload: dict[str, Any],
    deps: PaperJournalViewDeps,
    *,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str | None = None,
    limit: int = 1000,
) -> HTMLResponse:
    trades = payload.get("trades", [])
    stats = payload.get("stats", {}) if isinstance(payload.get("stats"), dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    account_unit = str(account.get("unit") or "result")
    rows = []
    for trade in trades:
        payload_body = trade.get("payload") if isinstance(trade.get("payload"), dict) else {}
        rows.append(
            "<tr>"
            f"<td>{_table_cell(trade.get('status'))}</td>"
            f"<td>{_table_cell(trade.get('symbol'))}</td>"
            f"<td>{_table_cell(trade.get('timeframe'))}</td>"
            f'<td title="raw: {_table_cell(trade.get("source"))}">{_table_cell(deps.signal_source_key(trade))}</td>'
            f"<td>{_table_cell(deps.setup_source_label(trade))}</td>"
            f"<td>{_table_cell(deps.setup_key(trade))}</td>"
            f"<td>{_table_cell(trade.get('side'))}</td>"
            f"<td>{_table_cell(trade.get('qty'))}</td>"
            f"<td>{_table_cell(trade.get('entry'))}</td>"
            f"<td>{_table_cell(trade.get('mark_price'))}</td>"
            f"<td>{_table_cell(trade.get('unrealized_pnl'))} {_table_cell(trade.get('pnl_unit'))}</td>"
            f"<td>{_table_cell(trade.get('stop'))}</td>"
            f"<td>{_table_cell(trade.get('target'))}</td>"
            f"<td>{_table_cell(trade.get('exit_price'))}</td>"
            f"<td>{_table_cell(trade.get('exit_reason'))}</td>"
            f"<td>{_table_cell(trade.get('pnl_points'))}</td>"
            f"<td>{_table_cell(payload_body.get('score'))}</td>"
            f"<td>{_table_cell(payload_body.get('rr'))}</td>"
            f"<td>{_table_cell(deps.trade_session(trade))}</td>"
            f"<td>{_table_cell(deps.trade_edge_action(trade))}</td>"
            f"<td>{_table_cell(payload_body.get('mfe_points'))}</td>"
            f"<td>{_table_cell(payload_body.get('mae_points'))}</td>"
            f"<td>{_table_cell(trade.get('opened_at'))}</td>"
            f"<td>{_table_cell(trade.get('closed_at'))}</td>"
            "</tr>"
        )
    title = "MartinCall Paper Trades"
    query = {
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "limit": max(1, min(int(limit), 1000)),
    }
    if timeframe:
        query["timeframe"] = timeframe
    csv_url = f"/api/paper/trades.csv?{urlencode(query)}"
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ margin: 0; padding: 18px; background: #0b1117; color: #e7edf5; font: 13px -apple-system, BlinkMacSystemFont, sans-serif; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }}
    h1 {{ font-size: 18px; margin: 0; }}
    a {{ color: #8cc8ff; text-decoration: none; }}
    .stats {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }}
    .metric {{ border: 1px solid #263241; border-radius: 6px; padding: 8px 10px; background: #111a24; }}
    .metric span {{ display: block; color: #9aa7b5; font-size: 11px; }}
    .metric b {{ font-size: 15px; }}
    table {{ width: 100%; border-collapse: collapse; background: #111a24; }}
    th, td {{ border: 1px solid #263241; padding: 6px 8px; text-align: left; white-space: nowrap; }}
    th {{ position: sticky; top: 0; background: #172232; z-index: 1; }}
    tr:nth-child(even) td {{ background: #0f1721; }}
  </style>
</head>
<body>
  <header><h1>{title}</h1><a href="{escape(csv_url, quote=True)}">Download CSV</a></header>
  <div class="stats">
    <div class="metric"><span>Total</span><b>{_metric_cell(stats.get("total", len(trades)))}</b></div>
    <div class="metric"><span>Open</span><b>{_metric_cell(stats.get("open"))}</b></div>
    <div class="metric"><span>Closed</span><b>{_metric_cell(stats.get("closed"))}</b></div>
    <div class="metric"><span>Realized ({_table_cell(account_unit)})</span><b>{_metric_cell(account.get("realized_pnl"))}</b></div>
    <div class="metric"><span>Unrealized ({_table_cell(account_unit)})</span><b>{_metric_cell(account.get("unrealized_pnl"))}</b></div>
    <div class="metric"><span>Net ({_table_cell(account_unit)})</span><b>{_metric_cell(account.get("net_pnl"))}</b></div>
    <div class="metric"><span>Win rate</span><b>{_metric_cell(stats.get("win_rate"))}</b></div>
  </div>
  <table>
    <thead><tr><th>Status</th><th>Symbol</th><th>TF</th><th>Source</th><th>Setup Source</th><th>Setup</th><th>Side</th><th>Qty</th><th>Entry</th><th>Mark</th><th>Unrealized</th><th>Stop</th><th>Target</th><th>Exit</th><th>Reason</th><th>PnL</th><th>Score</th><th>RR</th><th>Session</th><th>Edge</th><th>MFE</th><th>MAE</th><th>Opened</th><th>Closed</th></tr></thead>
    <tbody>{"".join(rows) or '<tr><td colspan="25">No trades</td></tr>'}</tbody>
  </table>
</body>
</html>"""
    return HTMLResponse(html)
