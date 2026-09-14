from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.ui.paper.positions import position_trade_from_storage


def _queue_paper_trade_telegram() -> Callable[[str, dict[str, Any]], None]:
    from aef_terminal.ui.paper_telegram_runtime import queue_paper_trade_telegram

    return queue_paper_trade_telegram


def paper_notify_fill_result(
    result: dict[str, Any],
    *,
    close_reason: str,
    queue_telegram: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    if result.get("outcome") != "filled":
        return
    position = result.get("position") if isinstance(result.get("position"), dict) else {}
    if not position:
        return
    notify = queue_telegram or _queue_paper_trade_telegram()
    closed_position = (
        result.get("closed_position")
        if isinstance(result.get("closed_position"), dict)
        else position
        if str(position.get("status") or "").lower() == "closed"
        else {}
    )
    if closed_position:
        close_payload = position_trade_from_storage(closed_position)
        fill = result.get("fill") if isinstance(result.get("fill"), dict) else {}
        order = result.get("order") if isinstance(result.get("order"), dict) else {}
        exit_price = float_or_none(fill.get("price"))
        if exit_price is None:
            exit_price = float_or_none(order.get("fill_price"))
        close_payload["exit_price"] = exit_price
        close_payload["exit_reason"] = str(close_reason or "position_closed")
        notify("close", close_payload)
    if (
        result.get("opened_new_position") is True
        and str(position.get("status") or "").lower() == "open"
    ):
        notify("open", position_trade_from_storage(position))
