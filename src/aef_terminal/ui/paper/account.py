from __future__ import annotations

from typing import Any

from aef_terminal.runtime.math_utils import float_or_none


def paper_account_payload(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize paper results without inventing cash, margin, fees, or FX state."""

    realized: list[float] = []
    unrealized: list[float] = []
    units: set[str] = set()
    incomplete = False
    for trade in trades:
        if not isinstance(trade, dict):
            incomplete = True
            continue
        status = str(trade.get("status") or "").lower()
        if status == "closed":
            value = float_or_none(trade.get("realized_pnl"))
        elif status == "open":
            value = float_or_none(trade.get("unrealized_pnl"))
        else:
            continue
        unit = str(trade.get("pnl_unit") or "").strip()
        if value is None or not unit:
            incomplete = True
            continue
        units.add(unit)
        (realized if status == "closed" else unrealized).append(value)
    if incomplete or len(units) > 1:
        return {
            "available": False,
            "unit": None,
            "realized_pnl": None,
            "unrealized_pnl": None,
            "net_pnl": None,
        }
    unit = next(iter(units), "points")
    realized_total = round(sum(realized), 4)
    unrealized_total = round(sum(unrealized), 4)
    return {
        "available": True,
        "unit": unit,
        "realized_pnl": realized_total,
        "unrealized_pnl": unrealized_total,
        "net_pnl": round(realized_total + unrealized_total, 4),
    }
