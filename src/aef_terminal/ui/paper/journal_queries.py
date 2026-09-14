from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.paper_contract import PaperContractScopeKind
from aef_terminal.ui.paper.account import paper_account_payload
from aef_terminal.ui.paper.constants import PAPER_JOURNAL_RECENT_HOURS
from aef_terminal.ui.paper.orders import active_paper_orders, read_paper_orders
from aef_terminal.ui.paper.positions import paper_journal_payload


@dataclass(frozen=True)
class PaperJournalQueryDeps:
    paper_trade_enriched: Callable[[dict[str, Any]], dict[str, Any]]
    paper_trade_stats: Callable[[list[dict[str, Any]]], dict[str, Any]]
    paper_replay_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]


def paper_trades_payload_from_store(
    store: Any,
    deps: PaperJournalQueryDeps,
    *,
    instrument_id: str,
    route_fingerprint: str,
    paper_scope_kind: PaperContractScopeKind = "instrument",
    timeframe: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    payload = paper_journal_payload(
        store,
        deps,
        instrument_id=identity,
        route_fingerprint=fingerprint,
        paper_scope_kind=paper_scope_kind,
        limit=limit,
    )
    payload["trades"] = [
        trade
        for trade in payload["trades"]
        if trade.get("instrument_id") == identity and trade.get("route_fingerprint") == fingerprint
    ]
    payload["positions"] = [
        position
        for position in payload.get("positions", [])
        if isinstance(position.get("payload"), dict)
        and position["payload"].get("instrument_id") == identity
        and position["payload"].get("route_fingerprint") == fingerprint
    ]
    payload["orders"] = [
        order
        for order in payload.get("orders", [])
        if order.get("instrument_id") == identity and order.get("route_fingerprint") == fingerprint
    ]
    payload["fills"] = [
        fill
        for fill in payload.get("fills", [])
        if fill.get("instrument_id") == identity and fill.get("route_fingerprint") == fingerprint
    ]
    if timeframe:
        payload["trades"] = [
            trade for trade in payload["trades"] if str(trade.get("timeframe") or "") == timeframe
        ]
        payload["positions"] = [
            position
            for position in payload.get("positions", [])
            if str(position.get("timeframe") or "") == timeframe
        ]
        payload["orders"] = [
            order
            for order in payload.get("orders", [])
            if str(order.get("timeframe") or "") == timeframe
        ]
        payload["fills"] = [
            fill
            for fill in payload.get("fills", [])
            if str(fill.get("timeframe") or "") == timeframe
        ]
    payload["stats"] = deps.paper_trade_stats(payload["trades"])
    payload["stats"]["invalid_excluded"] = 0
    payload["replay"] = deps.paper_replay_summary(payload["trades"])
    payload["account"] = paper_account_payload(payload["trades"])
    return payload


def paper_orders_payload_from_store(
    store: Any,
    *,
    instrument_id: str,
    route_fingerprint: str,
    limit: int = 100,
    since_hours: int = PAPER_JOURNAL_RECENT_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    identity = require_exact_identity_text(instrument_id, field="instrument_id")
    fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    bounded_hours = max(1, min(int(since_hours), 24 * 14))
    since = (now or datetime.now(tz=UTC)) - timedelta(hours=bounded_hours)
    orders = read_paper_orders(
        store,
        instrument_id=identity,
        route_fingerprint=fingerprint,
        limit=limit,
        status=None,
        since=since,
    )
    return {
        "ok": True,
        "instrument_id": identity,
        "route_fingerprint": fingerprint,
        "orders": orders,
        "active": active_paper_orders(orders),
        "since_hours": bounded_hours,
    }


def paper_events_payload_from_store(store: Any, *, limit: int = 200) -> dict[str, Any]:
    events = store.read_paper_trade_events(limit=max(1, min(int(limit), 1000)))
    return {"ok": True, "events": events}
