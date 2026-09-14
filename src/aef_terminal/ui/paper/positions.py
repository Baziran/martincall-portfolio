from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.paper_contract import (
    PAPER_CONTRACT_KEY,
    PAPER_EXECUTION_CONFIG_KEY,
    PAPER_PROTECTION_BASIS_KEY,
    PaperContractScopeKind,
    require_paper_contract_identity,
)
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.ui.paper.account import paper_account_payload
from aef_terminal.ui.paper.constants import PAPER_JOURNAL_RECENT_HOURS
from aef_terminal.ui.paper.execution import submit_paper_order_execution
from aef_terminal.ui.paper.metrics import paper_trade_pnl_value
from aef_terminal.ui.paper.orders import normalize_paper_order
from aef_terminal.ui.paper.ranking import (
    PAPER_META_SOURCES,
    paper_flip_rank_allows,
    paper_position_source_key,
    paper_signal_reliability_rank,
    paper_signal_source_key,
)
from aef_terminal.ui.paper.signals import paper_trade_from_signal
from aef_terminal.ui.paper.utils import math_is_finite


def paper_trade_mark_price(
    instrument_id: str, timeframe: str, route_fingerprint: str
) -> float | None:
    try:
        identity = require_exact_identity_text(instrument_id, field="instrument_id")
        expected_fingerprint = require_exact_identity_text(
            route_fingerprint, field="route_fingerprint"
        )
    except ValueError:
        return None
    timeframe_key = str(timeframe or "").strip()
    if not timeframe_key:
        return None
    try:
        from aef_terminal.data.providers import route_instrument
        from aef_terminal.ui.paper.execution import finite_price, screen_execution_price_snapshot
        from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument

        instrument = lookup_runtime_instrument(identity)
        route = route_instrument(instrument)
    except Exception:
        return None
    if route.fingerprint != expected_fingerprint:
        return None
    snapshot = screen_execution_price_snapshot(identity, timeframe_key, expected_fingerprint)
    if not isinstance(snapshot, dict):
        return None
    return finite_price(snapshot.get("price"))


def paper_open_trade_mark_pnl(trade: dict[str, Any]) -> dict[str, Any]:
    out = dict(trade)
    if str(out.get("status") or "").lower() != "open":
        return out
    if isinstance(out.get("entry"), bool) or isinstance(out.get("qty"), bool):
        return out
    try:
        entry = float(out.get("entry"))
        qty = abs(float(out.get("qty") or 0.0))
    except TypeError, ValueError:
        return out
    if not math_is_finite(entry) or not math_is_finite(qty) or qty <= 1e-9:
        return out
    side = str(out.get("side") or "").lower()
    try:
        contract = require_paper_contract_identity(out)
    except ValueError:
        return out
    if contract.scope_kind == "option":
        from aef_terminal.ui.paper.option_market import paper_option_mark_price

        mark = paper_option_mark_price(out)
    else:
        mark = paper_trade_mark_price(
            out.get("instrument_id"),
            str(out.get("timeframe") or ""),
            out.get("route_fingerprint"),
        )
    if mark is None:
        return out
    points = (entry - mark) * qty if side == "short" else (mark - entry) * qty
    projected = paper_trade_pnl_value(out, points)
    if projected is None:
        return out
    out["mark_price"] = mark
    out["unrealized_points"] = round(points, 4)
    out["unrealized_pnl"], out["pnl_unit"] = projected
    return out


def paper_finalize_journal_trade(trade: dict[str, Any]) -> dict[str, Any]:
    out = dict(trade)
    status = str(out.get("status") or "").lower()
    if status == "open":
        if out.get("unrealized_pnl") is not None:
            out["pnl_kind"] = "unrealized"
        return out
    if status == "closed":
        realized_points = out.get("pnl_points")
        projected = (
            paper_trade_pnl_value(out, realized_points) if realized_points is not None else None
        )
        if projected is not None:
            out["pnl_kind"] = "realized"
            out["realized_pnl"], out["pnl_unit"] = projected
    return out


def paper_order_payload_from_signal(
    symbol: str,
    timeframe: str,
    raw: dict[str, Any],
    config: dict[str, Any],
    *,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    provider_contract_id: str,
    qty: float = 1.0,
    edge_filter_loader: Any = None,
) -> dict[str, Any] | None:
    if raw.get(PAPER_PROTECTION_BASIS_KEY) != "absolute_structure":
        return None
    instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
    route_fingerprint = require_exact_identity_text(route_fingerprint, field="route_fingerprint")
    provider_contract_id = require_exact_identity_text(
        provider_contract_id, field="provider_contract_id"
    )
    trade = paper_trade_from_signal(
        symbol,
        timeframe,
        raw,
        config,
        edge_filter_loader=edge_filter_loader,
        instrument_id=instrument_id,
    )
    if trade is None:
        return None
    if isinstance(qty, bool):
        return None
    try:
        order_qty = float(qty)
    except TypeError, ValueError:
        return None
    if not math_is_finite(order_qty) or order_qty <= 0:
        return None
    source = str(trade.get("source") or "indicator")
    signal_source = str(raw.get("signal_source") or "").strip().lower()
    if signal_source in PAPER_META_SOURCES:
        signal_source = paper_signal_source_key(dict(raw))
    initiator = signal_source or source
    signal_rank = paper_signal_reliability_rank(initiator)
    signal_payload = dict(raw)
    if signal_rank > 0:
        signal_payload["signal_rank"] = signal_rank
    order_payload: dict[str, Any] = {
        "indicator_source": initiator,
        "signal_source": initiator,
        "setup_source": initiator,
        "entry_channel": "trade_center" if source == "trade_setup" else str(source or "indicator"),
        PAPER_PROTECTION_BASIS_KEY: "absolute_structure",
        "planned_entry": trade.get("entry"),
        "analysis_bar_ts": raw.get("analysis_bar_ts") or raw.get("ts"),
        "analysis_generation": str(raw.get("analysis_generation") or "").strip(),
        "signal_rank": signal_rank if signal_rank > 0 else None,
        "trade_id": trade.get("id"),
        "signal": signal_payload,
    }
    for key in (
        "signal_source_label",
        "confluence_sources",
        "setup",
        "code",
        "reason",
        "score",
        "action_card",
    ):
        value = raw.get(key)
        if value is not None:
            order_payload[key] = value
    order_generation = hashlib.sha1(
        f"{route_fingerprint}|{trade['id']}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "id": f"sig-{order_generation}",
        "symbol": symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider": provider,
        "provider_contract_id": provider_contract_id,
        "timeframe": timeframe,
        "side": trade["side"],
        "order_type": "market",
        "qty": order_qty,
        "entry": float(trade["entry"]),
        "stop_loss": float(trade["stop"]) if trade.get("stop") is not None else None,
        "target": float(trade["target"]) if trade.get("target") is not None else None,
        "use_stop_loss": trade.get("stop") is not None,
        "use_target": trade.get("target") is not None,
        "source": f"indicator:{source}",
        "role": "entry",
        "note": f"indicator {source} GO",
        "payload": order_payload,
    }


def _position_indicator_source(position: dict[str, Any]) -> str:
    payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    return str(payload.get("indicator_source") or "").strip().lower()


def _open_position_for_route(
    store: Any,
    instrument_id: str,
    route_fingerprint: str,
    timeframe: str,
) -> dict[str, Any] | None:
    expected_instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
    expected_fingerprint = require_exact_identity_text(route_fingerprint, field="route_fingerprint")
    for position in store.read_paper_positions(
        limit=1,
        status="open",
        instrument_id=expected_instrument_id,
        route_fingerprint=expected_fingerprint,
        timeframe=timeframe,
    ):
        payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
        if (
            payload.get("instrument_id") == expected_instrument_id
            and payload.get("route_fingerprint") == expected_fingerprint
            and str(position.get("timeframe") or "") == timeframe
            and str(position.get("status") or "").lower() == "open"
            and abs(float(position.get("qty") or 0.0)) > 1e-9
        ):
            return position
    return None


def paper_position_source_label(position: dict[str, Any]) -> str:
    payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    label = str(payload.get("signal_source_label") or "").strip()
    if label:
        return label
    initiator = paper_signal_source_key(payload)
    if initiator:
        return initiator.replace("_", " ")
    indicator_source = str(payload.get("indicator_source") or "").strip()
    if indicator_source and indicator_source.lower() not in PAPER_META_SOURCES:
        return indicator_source.replace("_", " ")
    if str(payload.get("source") or "") == "manual_position":
        return "manual"
    return "paper"


def _paper_position_close_facts(
    position: dict[str, Any],
    close_event: dict[str, Any] | None,
) -> tuple[float | None, str]:
    if close_event is None:
        return None, "position_closed"
    position_id = require_exact_identity_text(position.get("id"), field="PAPER_POSITION_ID")
    position_payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    instrument_id = require_exact_identity_text(
        position_payload.get("instrument_id"), field="PAPER_POSITION_INSTRUMENT_ID"
    )
    route_fingerprint = require_exact_identity_text(
        position_payload.get("route_fingerprint"), field="PAPER_POSITION_ROUTE_FINGERPRINT"
    )
    if (
        close_event.get("event_type") != "position_closed"
        or close_event.get("trade_id") != position_id
        or close_event.get("instrument_id") != instrument_id
        or close_event.get("route_fingerprint") != route_fingerprint
    ):
        raise ValueError("PAPER_POSITION_CLOSE_EVENT_IDENTITY_MISMATCH")
    event_payload = close_event.get("payload")
    if not isinstance(event_payload, dict):
        raise ValueError("PAPER_POSITION_CLOSE_EVENT_PAYLOAD_INVALID")
    event_position = event_payload.get("position")
    fill = event_payload.get("fill")
    if not isinstance(event_position, dict) or not isinstance(fill, dict):
        raise ValueError("PAPER_POSITION_CLOSE_EVENT_FACTS_MISSING")
    if (
        event_position.get("id") != position_id
        or event_position.get("status") != "closed"
        or event_position.get("closed_at") != position.get("closed_at")
        or fill.get("filled_at") != position.get("closed_at")
    ):
        raise ValueError("PAPER_POSITION_CLOSE_EVENT_FACTS_MISMATCH")
    if isinstance(fill.get("price"), bool):
        raise ValueError("PAPER_POSITION_CLOSE_PRICE_INVALID")
    try:
        exit_price = float(fill.get("price"))
    except (TypeError, ValueError) as exc:
        raise ValueError("PAPER_POSITION_CLOSE_PRICE_INVALID") from exc
    if not math_is_finite(exit_price):
        raise ValueError("PAPER_POSITION_CLOSE_PRICE_INVALID")
    order_payload = event_payload.get("payload")
    execution_command = (
        order_payload.get("execution_command") if isinstance(order_payload, dict) else None
    )
    exit_reason = (
        str(execution_command.get("exit_reason") or "").strip().lower()
        if isinstance(execution_command, dict)
        else ""
    )
    if not exit_reason:
        exit_reason = {
            "stop": "stop",
            "take": "target",
            "close": "manual_close",
        }.get(str(fill.get("role") or "").strip().lower(), "position_reversal")
    return exit_price, exit_reason


def position_trade_from_storage(
    position: dict[str, Any],
    *,
    protective_orders: list[dict[str, Any]] | None = None,
    close_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    instrument_id = require_exact_identity_text(payload.get("instrument_id"), field="instrument_id")
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"), field="route_fingerprint"
    )
    provider_contract_id = require_exact_identity_text(
        payload.get("provider_contract_id"), field="provider_contract_id"
    )
    qty = float(position.get("qty") or 0.0)
    side = _position_side(position)
    source_label = paper_position_source_label(position)
    stop = None
    target = None
    position_id = str(position.get("id") or "")
    for order in protective_orders or []:
        if str(order.get("position_id") or "") != position_id:
            continue
        role = str(order.get("role") or "").lower()
        try:
            price = float(order.get("entry"))
        except TypeError, ValueError:
            continue
        if role == "stop":
            stop = price
        elif role == "take":
            target = price
    closed = str(position.get("status") or "") == "closed"
    exit_price, exit_reason = (
        _paper_position_close_facts(position, close_event) if closed else (None, "")
    )
    return {
        "id": position_id,
        "symbol": str(position.get("symbol") or ""),
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider": str(payload.get("provider") or ""),
        "provider_contract_id": provider_contract_id,
        PAPER_CONTRACT_KEY: (position.get(PAPER_CONTRACT_KEY) or payload.get(PAPER_CONTRACT_KEY)),
        "timeframe": str(position.get("timeframe") or ""),
        "source": source_label,
        "signal_source": source_label,
        "side": side,
        "status": str(position.get("status") or ""),
        "qty": abs(qty),
        "entry": position.get("avg_entry"),
        "stop": stop,
        "target": target,
        "opened_at": position.get("opened_at"),
        "closed_at": position.get("closed_at"),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_points": position.get("realized_pnl") if closed else None,
        "payload": {
            **payload,
            "position_id": position_id,
            "source_label": source_label,
        },
    }


def paper_journal_payload(
    store: Any,
    deps: Any,
    *,
    instrument_id: str,
    route_fingerprint: str,
    paper_scope_kind: PaperContractScopeKind = "instrument",
    limit: int = 200,
) -> dict[str, Any]:
    if paper_scope_kind not in {"instrument", "option"}:
        raise ValueError("PAPER_JOURNAL_SCOPE_KIND_INVALID")
    identity = require_exact_identity_text(
        instrument_id,
        field="PAPER_JOURNAL_INSTRUMENT_ID",
    )
    fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="PAPER_JOURNAL_ROUTE_FINGERPRINT",
    )
    positions = store.read_paper_positions(
        limit=max(1, min(int(limit), 1000)),
        status=None,
        instrument_id=identity,
        route_fingerprint=fingerprint,
        contract_scope_kind=paper_scope_kind,
    )
    exact_position_ids = {
        require_exact_identity_text(
            position.get("id"),
            field="PAPER_POSITION_ID",
        )
        for position in positions
    }
    detached_open_positions = [
        position
        for position in store.read_paper_positions(
            status="open",
            complete_open=True,
        )
        if require_exact_identity_text(
            position.get("id"),
            field="PAPER_POSITION_ID",
        )
        not in exact_position_ids
        and abs(float(position.get("qty") or 0.0)) > 1e-9
    ]
    since = datetime.now(tz=UTC) - timedelta(hours=PAPER_JOURNAL_RECENT_HOURS)
    open_position_ids = {
        str(position.get("id") or "")
        for position in positions
        if str(position.get("status") or "").lower() == "open"
        and abs(float(position.get("qty") or 0.0)) > 1e-9
    }
    protective_orders = [
        order
        for order in store.read_paper_orders(
            limit=500,
            status="pending",
            instrument_id=identity,
            route_fingerprint=fingerprint,
            contract_scope_kind=paper_scope_kind,
        )
        if str(order.get("position_id") or "") in open_position_ids
    ]
    recent_orders = store.read_paper_orders(
        limit=500,
        status=None,
        instrument_id=identity,
        route_fingerprint=fingerprint,
        since=since,
        contract_scope_kind=paper_scope_kind,
    )
    fills = store.read_paper_fills(
        limit=max(1, min(int(limit), 1000)),
        instrument_id=identity,
        route_fingerprint=fingerprint,
        since=since,
        contract_scope_kind=paper_scope_kind,
    )
    closed_position_ids = tuple(
        require_exact_identity_text(position.get("id"), field="PAPER_POSITION_ID")
        for position in positions
        if str(position.get("status") or "").lower() == "closed"
    )
    close_events = (
        store.read_paper_trade_events(
            limit=len(closed_position_ids),
            instrument_id=identity,
            route_fingerprint=fingerprint,
            trade_ids=closed_position_ids,
            event_type="position_closed",
        )
        if closed_position_ids
        else []
    )
    close_event_by_position: dict[str, dict[str, Any]] = {}
    for event in close_events:
        position_id = require_exact_identity_text(
            event.get("trade_id"), field="PAPER_EVENT_TRADE_ID"
        )
        if position_id in close_event_by_position:
            raise ValueError(f"PAPER_POSITION_CLOSE_EVENT_DUPLICATE: {position_id}")
        close_event_by_position[position_id] = event
    if set(close_event_by_position) != set(closed_position_ids):
        raise ValueError("PAPER_POSITION_CLOSE_EVENT_COVERAGE_MISMATCH")
    trades = [
        paper_finalize_journal_trade(
            paper_open_trade_mark_pnl(
                deps.paper_trade_enriched(
                    position_trade_from_storage(
                        position,
                        protective_orders=protective_orders,
                        close_event=close_event_by_position.get(str(position.get("id") or "")),
                    )
                )
            )
        )
        for position in positions
    ]
    detached_open_trades = [
        paper_finalize_journal_trade(
            paper_open_trade_mark_pnl(
                deps.paper_trade_enriched(position_trade_from_storage(position))
            )
        )
        for position in detached_open_positions
    ]
    stats = deps.paper_trade_stats(trades)
    stats["invalid_excluded"] = 0
    replay = deps.paper_replay_summary(trades)
    return {
        "ok": True,
        "instrument_id": identity,
        "route_fingerprint": fingerprint,
        "paper_scope_kind": paper_scope_kind,
        "trades": trades,
        "detached_open_trades": detached_open_trades,
        "positions": positions,
        "orders": recent_orders,
        "fills": fills,
        "stats": stats,
        "replay": replay,
        "account": paper_account_payload(trades),
    }


def _position_side(position: dict[str, Any]) -> str:
    payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    side = payload.get("side")
    if not isinstance(side, str) or side not in {"long", "short"}:
        raise ValueError(f"PAPER_POSITION_SIDE_INVALID expected=long|short actual={side!r}")
    return side


def _signal_opposes_position(signal_side: str, position: dict[str, Any]) -> bool:
    side = str(signal_side or "").lower()
    if side not in {"long", "short"}:
        return False
    return side != _position_side(position)


def paper_flip_signal_position(
    store: Any,
    *,
    symbol: str,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    provider_contract_id: str,
    timeframe: str,
    existing: dict[str, Any],
    raw: dict[str, Any],
    config: dict[str, Any],
    price_snapshot: dict[str, Any] | None,
    paper_order_execution_transition: Any,
    edge_filter_loader: Any = None,
) -> dict[str, Any] | None:
    signal_side = str(raw.get("side") or "").lower()
    if not _signal_opposes_position(signal_side, existing):
        return None
    current_source = paper_position_source_key(existing) or _position_indicator_source(existing)
    new_source = paper_signal_source_key(raw)
    from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
    from aef_terminal.data.providers import route_instrument

    instrument = lookup_runtime_instrument(instrument_id)
    if route_instrument(instrument).fingerprint != route_fingerprint:
        raise ValueError("paper position route fingerprint mismatch")
    profile = resolve_instrument_profile(instrument)
    if not paper_flip_rank_allows(
        current_source=current_source,
        new_source=new_source,
        profile_key=profile.key,
    ):
        return {
            "opened": False,
            "rejected": False,
            "skipped": True,
            "reason": "opposite signal rank not higher than open position",
            "position": existing,
        }
    payload = paper_order_payload_from_signal(
        symbol,
        timeframe,
        raw,
        config,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        provider=provider,
        provider_contract_id=provider_contract_id,
        qty=abs(float(existing.get("qty") or 0.0)) + 1.0,
        edge_filter_loader=edge_filter_loader,
    )
    if payload is None:
        return {
            "opened": False,
            "rejected": True,
            "code": "PAPER_SIGNAL_ORDER_INVALID",
            "reason": "invalid flip signal",
        }
    existing_id = require_exact_identity_text(
        existing.get("id"),
        field="PAPER_POSITION_ID",
    )
    existing_updated_at = require_exact_identity_text(
        existing.get("updated_at"),
        field="PAPER_POSITION_UPDATED_AT",
    )
    existing_qty = float(existing.get("qty") or 0.0)
    existing_side = _position_side(existing)
    try:
        order = normalize_paper_order(payload)
    except ValueError as exc:
        return {
            "opened": False,
            "rejected": True,
            "code": "PAPER_ORDER_INVALID",
            "reason": str(exc),
        }
    order["payload"] = {
        **(dict(order["payload"]) if isinstance(order.get("payload"), dict) else {}),
        PAPER_EXECUTION_CONFIG_KEY: dict(config),
        "flip_from_position_id": existing_id,
        "flip_from_source": current_source,
        "flip_to_source": new_source,
        "position_expectation": {
            "state": "open",
            "position_id": existing_id,
            "side": existing_side,
            "qty": existing_qty,
            "updated_at": existing_updated_at,
        },
    }
    order["exit_reason"] = "ranked_flip"
    submission = submit_paper_order_execution(
        store,
        order,
        price_snapshot,
        execution_at=datetime.now(tz=UTC),
        transition_resolver=paper_order_execution_transition,
    )
    submission_outcome = submission["outcome"]
    if submission_outcome in {"pending", "cancelled", "rejected"}:
        fresh_rejection = submission_outcome == "rejected" and not submission["replayed"]
        return {
            "opened": False,
            "rejected": fresh_rejection,
            "skipped": not fresh_rejection,
            "code": str(
                submission.get("code")
                or (
                    "PAPER_EXECUTION_PENDING"
                    if submission_outcome == "pending"
                    else "PAPER_EXECUTION_REJECTED"
                )
            ),
            "reason": str(
                submission.get("message")
                or (
                    "automatic paper entry is awaiting execution"
                    if submission_outcome == "pending"
                    else "paper execution is already settled"
                )
            ),
            "order": submission["order"],
            "execution": submission["execution"],
        }
    result = submission["result"]
    if not isinstance(result, dict):
        raise ValueError("PAPER_FLIP_FILL_RESULT_REQUIRED")
    if result.get("outcome") not in {"filled", "no_op"}:
        raise ValueError("PAPER_FLIP_FILL_OUTCOME_REQUIRED")
    outcome = result["outcome"]
    position = result.get("position")
    if not isinstance(position, dict):
        raise ValueError("PAPER_FLIP_POSITION_RESULT_REQUIRED")
    if outcome == "no_op":
        return {
            "outcome": outcome,
            "opened": False,
            "flipped": False,
            "rejected": False,
            "skipped": True,
            "reason": "paper fill command already settled",
            "closed_position": result.get("closed_position"),
            "closed_position_id": existing_id,
            "order": result["order"],
            "position": position,
            "fill": result.get("fill"),
            "created_orders": result["created_orders"],
            "execution": submission["execution"],
        }
    return {
        "outcome": outcome,
        "opened": (
            result.get("opened_new_position") is True
            and str(position.get("status") or "").lower() == "open"
        ),
        "flipped": True,
        "rejected": False,
        "closed_position": result.get("closed_position"),
        "closed_position_id": existing_id,
        "order": result["order"],
        "position": position,
        "fill": result["fill"],
        "created_orders": result["created_orders"],
        "execution": submission["execution"],
    }


def paper_open_signal_position(
    store: Any,
    *,
    symbol: str,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    provider_contract_id: str,
    timeframe: str,
    raw: dict[str, Any],
    config: dict[str, Any],
    price_snapshot: dict[str, Any] | None,
    paper_order_execution_transition: Any,
    edge_filter_loader: Any = None,
) -> dict[str, Any]:
    base_payload = paper_order_payload_from_signal(
        symbol,
        timeframe,
        raw,
        config,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        provider=provider,
        provider_contract_id=provider_contract_id,
        edge_filter_loader=edge_filter_loader,
    )
    if base_payload is None:
        return {
            "opened": False,
            "rejected": True,
            "code": "PAPER_SIGNAL_ORDER_INVALID",
            "reason": "invalid signal",
        }
    reserved_order = store.read_paper_order(
        base_payload["id"],
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )
    if isinstance(reserved_order, dict):
        create_command = reserved_order.get("create_command")
        if not isinstance(create_command, dict):
            raise ValueError("PAPER_SIGNAL_CREATE_COMMAND_REQUIRED")
        runtime_payload = reserved_order.get("payload")
        if not isinstance(runtime_payload, dict):
            raise ValueError("PAPER_EXECUTION_CONFIG_REQUIRED")
        resume_order = {
            **create_command,
            "payload": dict(runtime_payload),
            "status": "pending",
            "created_at": (reserved_order.get("created_at") or datetime.now(tz=UTC).isoformat()),
            "updated_at": (
                reserved_order.get("updated_at")
                or reserved_order.get("created_at")
                or datetime.now(tz=UTC).isoformat()
            ),
        }
        submission = submit_paper_order_execution(
            store,
            resume_order,
            price_snapshot,
            execution_at=datetime.now(tz=UTC),
            transition_resolver=paper_order_execution_transition,
        )
        submission_outcome = submission["outcome"]
        if submission_outcome in {"pending", "cancelled", "rejected"}:
            fresh_rejection = submission_outcome == "rejected" and not submission["replayed"]
            return {
                "opened": False,
                "rejected": fresh_rejection,
                "skipped": not fresh_rejection,
                "code": str(
                    submission.get("code")
                    or (
                        "PAPER_EXECUTION_PENDING"
                        if submission_outcome == "pending"
                        else "PAPER_EXECUTION_REJECTED"
                    )
                ),
                "reason": str(
                    submission.get("message")
                    or (
                        "automatic paper entry is awaiting execution"
                        if submission_outcome == "pending"
                        else "paper execution is already settled"
                    )
                ),
                "order": submission["order"],
                "execution": submission["execution"],
            }
        result = submission["result"]
        if not isinstance(result, dict) or result.get("outcome") not in {"filled", "no_op"}:
            raise ValueError("PAPER_SIGNAL_RESUME_FILL_OUTCOME_REQUIRED")
        position = result.get("position")
        if not isinstance(position, dict):
            raise ValueError("PAPER_SIGNAL_RESUME_POSITION_RESULT_REQUIRED")
        stored_payload = (
            reserved_order.get("payload") if isinstance(reserved_order.get("payload"), dict) else {}
        )
        closed_position = result.get("closed_position")
        opened = (
            result.get("opened_new_position") is True
            and str(position.get("status") or "").lower() == "open"
        )
        flipped = (
            opened
            and isinstance(closed_position, dict)
            and bool(stored_payload.get("flip_from_position_id"))
        )
        if result["outcome"] == "no_op":
            return {
                "outcome": "no_op",
                "opened": False,
                "flipped": False,
                "rejected": False,
                "skipped": True,
                "reason": "paper fill command already settled",
                "order": result["order"],
                "position": position,
                "fill": result.get("fill"),
                "created_orders": result["created_orders"],
                "closed_position": closed_position,
                "execution": submission["execution"],
            }
        return {
            "outcome": "filled",
            "opened": opened,
            "flipped": flipped,
            "rejected": False,
            "order": result["order"],
            "position": position,
            "fill": result["fill"],
            "created_orders": result["created_orders"],
            "closed_position": closed_position,
            "execution": submission["execution"],
        }
    existing = _open_position_for_route(store, instrument_id, route_fingerprint, timeframe)
    if existing is not None:
        flip_result = paper_flip_signal_position(
            store,
            symbol=symbol,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            provider=provider,
            provider_contract_id=provider_contract_id,
            timeframe=timeframe,
            existing=existing,
            raw=raw,
            config=config,
            price_snapshot=price_snapshot,
            paper_order_execution_transition=paper_order_execution_transition,
            edge_filter_loader=edge_filter_loader,
        )
        if flip_result is not None:
            return flip_result
        return {
            "opened": False,
            "rejected": False,
            "skipped": True,
            "reason": "position already open",
            "position": existing,
        }
    payload = base_payload
    try:
        order = normalize_paper_order(payload)
    except ValueError as exc:
        return {
            "opened": False,
            "rejected": True,
            "code": "PAPER_ORDER_INVALID",
            "reason": str(exc),
        }
    order["payload"] = {
        **(dict(order["payload"]) if isinstance(order.get("payload"), dict) else {}),
        PAPER_EXECUTION_CONFIG_KEY: dict(config),
        "position_expectation": {"state": "absent"},
    }
    submission = submit_paper_order_execution(
        store,
        order,
        price_snapshot,
        execution_at=datetime.now(tz=UTC),
        transition_resolver=paper_order_execution_transition,
    )
    submission_outcome = submission["outcome"]
    if submission_outcome in {"pending", "cancelled", "rejected"}:
        fresh_rejection = submission_outcome == "rejected" and not submission["replayed"]
        return {
            "opened": False,
            "rejected": fresh_rejection,
            "skipped": not fresh_rejection,
            "code": str(
                submission.get("code")
                or (
                    "PAPER_EXECUTION_PENDING"
                    if submission_outcome == "pending"
                    else "PAPER_EXECUTION_REJECTED"
                )
            ),
            "reason": str(
                submission.get("message")
                or (
                    "automatic paper entry is awaiting execution"
                    if submission_outcome == "pending"
                    else "paper execution is already settled"
                )
            ),
            "order": submission["order"],
            "execution": submission["execution"],
        }
    result = submission["result"]
    if not isinstance(result, dict) or result.get("outcome") not in {"filled", "no_op"}:
        raise ValueError("PAPER_OPEN_FILL_OUTCOME_REQUIRED")
    outcome = result["outcome"]
    position = result.get("position")
    if not isinstance(position, dict):
        raise ValueError("PAPER_OPEN_POSITION_RESULT_REQUIRED")
    if outcome == "no_op":
        return {
            "outcome": outcome,
            "opened": False,
            "rejected": False,
            "skipped": True,
            "reason": "paper fill command already settled",
            "order": result["order"],
            "position": position,
            "fill": result.get("fill"),
            "created_orders": result["created_orders"],
            "execution": submission["execution"],
        }
    return {
        "outcome": outcome,
        "opened": (
            result.get("opened_new_position") is True
            and str(position.get("status") or "").lower() == "open"
        ),
        "rejected": False,
        "order": result["order"],
        "position": position,
        "fill": result["fill"],
        "created_orders": result["created_orders"],
        "execution": submission["execution"],
    }
