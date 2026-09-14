from __future__ import annotations

from typing import Any

from aef_terminal.paper_contract import require_paper_contract_identity
from aef_terminal.ui.paper.metadata import (
    paper_payload_float,
    paper_setup_key,
    paper_signal_source_key,
    paper_trade_edge_action,
    paper_trade_session,
)
from aef_terminal.ui.paper.utils import math_is_finite, parse_iso_utc


def paper_trade_pnl_value(
    trade: dict[str, Any],
    pnl_points: float,
) -> tuple[float, str] | None:
    """Project paper price-unit P&L into its honest display unit."""

    try:
        points = float(pnl_points)
        contract = require_paper_contract_identity(trade)
    except TypeError, ValueError:
        return None
    if not math_is_finite(points):
        return None
    if contract.scope_kind == "option":
        if contract.multiplier is None or contract.currency is None:
            return None
        return round(points * contract.multiplier, 4), contract.currency
    return round(points, 4), "points"


def paper_trade_pnl_points(trade: dict[str, Any], exit_price: float) -> float | None:
    try:
        entry = float(trade.get("entry"))
        qty = float(trade.get("qty") or 1.0)
    except TypeError, ValueError:
        return None
    side = str(trade.get("side") or "").lower()
    if side == "long":
        return round((float(exit_price) - entry) * qty, 4)
    if side == "short":
        return round((entry - float(exit_price)) * qty, 4)
    return None


def paper_trade_r_value(trade: dict[str, Any]) -> float | None:
    try:
        pnl = float(trade.get("pnl_points") or 0.0)
        risk = abs(float(trade.get("entry")) - float(trade.get("stop"))) * float(
            trade.get("qty") or 1.0
        )
    except TypeError, ValueError:
        return None
    if risk <= 0:
        return None
    return pnl / risk


def paper_trade_score(trade: dict[str, Any]) -> float | None:
    payload = trade.get("payload") if isinstance(trade.get("payload"), dict) else {}
    value = payload.get("score")
    signal = payload.get("signal") if isinstance(payload.get("signal"), dict) else {}
    if value is None:
        value = signal.get("score")
    try:
        score = float(value)
    except TypeError, ValueError:
        return None
    return score if math_is_finite(score) else None


def paper_trade_hold_minutes(trade: dict[str, Any]) -> float | None:
    opened = trade.get("opened_at")
    closed = trade.get("closed_at")
    if not opened or not closed:
        return None
    opened_at = parse_iso_utc(opened)
    closed_at = parse_iso_utc(closed)
    if opened_at is None or closed_at is None:
        return None
    return max((closed_at - opened_at).total_seconds() / 60.0, 0.0)


def paper_trade_bucket_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("status") == "closed"]
    open_count = sum(1 for trade in trades if trade.get("status") == "open")
    pnl_values = [float(trade.get("pnl_points") or 0.0) for trade in closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    r_values = [paper_trade_r_value(trade) for trade in closed]
    r_values = [value for value in r_values if value is not None]
    scores = [paper_trade_score(trade) for trade in trades]
    scores = [value for value in scores if value is not None]
    mfe_values = [paper_payload_float(trade, "mfe_points", "mfe") for trade in closed]
    mfe_values = [value for value in mfe_values if value is not None]
    mae_values = [paper_payload_float(trade, "mae_points", "mae") for trade in closed]
    mae_values = [value for value in mae_values if value is not None]
    hold_minutes = [paper_trade_hold_minutes(trade) for trade in closed]
    hold_minutes = [value for value in hold_minutes if value is not None]
    target_hits = sum(
        1 for trade in closed if str(trade.get("exit_reason") or "").lower() == "target"
    )
    stop_hits = sum(1 for trade in closed if str(trade.get("exit_reason") or "").lower() == "stop")
    return {
        "total": len(trades),
        "open": open_count,
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "target_hits": target_hits,
        "stop_hits": stop_hits,
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "target_hit_rate": round(target_hits / len(closed), 4) if closed else None,
        "stop_hit_rate": round(stop_hits / len(closed), 4) if closed else None,
        "pnl_points": round(sum(pnl_values), 4),
        "avg_pnl": round(sum(pnl_values) / len(closed), 4) if closed else None,
        "avg_win": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "expectancy_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "best_r": round(max(r_values), 4) if r_values else None,
        "worst_r": round(min(r_values), 4) if r_values else None,
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "avg_mfe_points": round(sum(mfe_values) / len(mfe_values), 4) if mfe_values else None,
        "avg_mae_points": round(sum(mae_values) / len(mae_values), 4) if mae_values else None,
        "avg_hold_minutes": round(sum(hold_minutes) / len(hold_minutes), 2)
        if hold_minutes
        else None,
    }


def paper_trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    by_setup: dict[str, list[dict[str, Any]]] = {}
    by_side: dict[str, list[dict[str, Any]]] = {}
    by_timeframe: dict[str, list[dict[str, Any]]] = {}
    by_quality: dict[str, list[dict[str, Any]]] = {}
    by_edge_action: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        source = paper_signal_source_key(trade)
        side = str(trade.get("side") or "unknown")
        session = paper_trade_session(trade)
        by_source.setdefault(source, []).append(trade)
        by_setup.setdefault(paper_setup_key(trade), []).append(trade)
        by_side.setdefault(side, []).append(trade)
        by_timeframe.setdefault(str(trade.get("timeframe") or "unknown"), []).append(trade)
        by_quality.setdefault(f"{source}:{side}:{session}", []).append(trade)
        by_edge_action.setdefault(paper_trade_edge_action(trade), []).append(trade)

    def ranked(groups: dict[str, list[dict[str, Any]]], limit: int = 12) -> list[dict[str, Any]]:
        rows = [{"key": key, **paper_trade_bucket_stats(items)} for key, items in groups.items()]
        return sorted(
            rows,
            key=lambda row: (int(row["closed"]), abs(float(row["pnl_points"] or 0.0))),
            reverse=True,
        )[:limit]

    setup_rows = ranked(by_setup)
    closed_setups = [row for row in setup_rows if int(row.get("closed") or 0) > 0]
    return {
        **paper_trade_bucket_stats(trades),
        "by_source": ranked(by_source),
        "by_setup": setup_rows,
        "by_side": ranked(by_side),
        "by_timeframe": ranked(by_timeframe),
        "by_quality": ranked(by_quality, limit=18),
        "by_edge_action": ranked(by_edge_action),
        "best_setup": max(
            closed_setups, key=lambda row: float(row.get("expectancy_r") or -999.0), default=None
        ),
        "worst_setup": min(
            closed_setups, key=lambda row: float(row.get("expectancy_r") or 999.0), default=None
        ),
    }


def paper_replay_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [
        trade
        for trade in trades
        if trade.get("status") == "closed" and trade.get("pnl_points") is not None
    ]
    closed.sort(key=lambda trade: str(trade.get("closed_at") or trade.get("opened_at") or ""))
    cumulative_points = 0.0
    peak_points = 0.0
    max_drawdown = 0.0
    best_streak = 0
    worst_streak = 0
    current_win = 0
    current_loss = 0
    curve: list[dict[str, Any]] = []
    by_exit: dict[str, int] = {}
    for trade in closed:
        pnl = float(trade.get("pnl_points") or 0.0)
        cumulative_points += pnl
        peak_points = max(peak_points, cumulative_points)
        max_drawdown = min(max_drawdown, cumulative_points - peak_points)
        if pnl > 0:
            current_win += 1
            current_loss = 0
        elif pnl < 0:
            current_loss += 1
            current_win = 0
        best_streak = max(best_streak, current_win)
        worst_streak = max(worst_streak, current_loss)
        reason = str(trade.get("exit_reason") or "unknown").lower()
        by_exit[reason] = by_exit.get(reason, 0) + 1
        curve.append(
            {
                "id": trade.get("id"),
                "closed_at": trade.get("closed_at"),
                "source": paper_signal_source_key(trade),
                "raw_source": trade.get("source"),
                "side": trade.get("side"),
                "pnl_points": round(pnl, 4),
                "cumulative_points": round(cumulative_points, 4),
                "drawdown_points": round(cumulative_points - peak_points, 4),
                "exit_reason": reason,
            }
        )
    return {
        "closed": len(closed),
        "net_points": round(cumulative_points, 4),
        "max_drawdown_points": round(max_drawdown, 4),
        "best_win_streak": best_streak,
        "worst_loss_streak": worst_streak,
        "by_exit_reason": [{"key": key, "count": value} for key, value in sorted(by_exit.items())],
        "points_curve": curve[-120:],
        "recent_closed": curve[-8:],
    }
