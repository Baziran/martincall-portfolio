from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.paper_contract import (
    PAPER_CONTRACT_KEY,
    PAPER_EXECUTION_CONFIG_KEY,
    PAPER_PROTECTION_BASIS_KEY,
    PAPER_PROTECTION_INTENT_KEY,
    PaperContractIdentity,
    PaperContractScopeKind,
    PaperJournalError,
    require_paper_contract_identity,
    require_paper_order_protection,
)
from aef_terminal.paper_journal_runtime import paper_journal_runtime
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.storage.db_utils import (
    _paper_utc_datetime,
    _paper_fill_from_row,
    _paper_order_from_row,
    _paper_position_from_row,
    ensure_utc,
    utc_now,
)


PaperFillOutcome = Literal["filled", "cancelled", "no_op"]


class PaperFillResult(TypedDict):
    outcome: PaperFillOutcome
    order: dict[str, Any]
    position: dict[str, Any] | None
    fill: dict[str, Any] | None
    created_orders: list[dict[str, Any]]
    cancelled_orders: list[dict[str, Any]]
    closed_position: dict[str, Any] | None
    opened_new_position: bool


@dataclass(slots=True)
class _PaperFillCommand:
    order: dict[str, Any]
    command: dict[str, Any]
    protection_payload: dict[str, Any]
    order_id: str
    symbol: str
    instrument_id: str
    route_fingerprint: str
    provider: str
    provider_contract_id: str
    paper_contract: dict[str, Any]
    contract_scope_key: str
    timeframe: str
    side: str
    role: str
    reduce_only: bool
    manual_close: bool
    target_position_id: str | None
    qty: float | None
    price: float
    entry_price: float
    stop_loss: float | None
    target: float | None
    filled_at: datetime
    signed_delta: float
    order_payload: dict[str, Any]
    create_command: dict[str, Any]
    incoming_order: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PaperPositionTransition:
    position_id: str
    previous_qty: float
    side: str
    qty: float
    signed_delta: float
    pnl: float
    closed_position: dict[str, Any] | None
    opened_new_position: bool


def _paper_fill_command(order: dict[str, Any]) -> _PaperFillCommand:
    command = _paper_order_command_payload(order)
    protection_payload = require_paper_order_protection(order)
    reduce_only = command["reduce_only"]
    role = command["role"]
    manual_close = reduce_only and role == "close"
    fill_raw = (
        order.get("fill_price") if order.get("fill_price") is not None else order.get("entry")
    )
    price = float_or_none(fill_raw)
    entry_price = (
        price if manual_close or order.get("entry") is None else float_or_none(order.get("entry"))
    )
    if price is None or entry_price is None:
        raise ValueError("valid provider-qualified filled paper order is required")
    side = command["side"] or ""
    qty = command["qty"]
    signed_delta = 0.0 if manual_close else qty if side == "long" else -qty
    paper_contract = command[PAPER_CONTRACT_KEY]
    order_payload = {
        **protection_payload,
        "instrument_id": command["instrument_id"],
        "route_fingerprint": command["route_fingerprint"],
        "provider": command["provider"],
        "provider_contract_id": command["provider_contract_id"],
        PAPER_CONTRACT_KEY: paper_contract,
    }
    raw_create_command = order.get("create_command")
    create_command = (
        _paper_order_command_payload(raw_create_command)
        if isinstance(raw_create_command, dict)
        else command
    )
    incoming_order = {
        **dict(order),
        PAPER_CONTRACT_KEY: paper_contract,
        "payload": protection_payload,
        "create_command": create_command,
    }
    return _PaperFillCommand(
        order=order,
        command=command,
        protection_payload=protection_payload,
        order_id=command["id"],
        symbol=command["symbol"],
        instrument_id=command["instrument_id"],
        route_fingerprint=command["route_fingerprint"],
        provider=command["provider"],
        provider_contract_id=command["provider_contract_id"],
        paper_contract=paper_contract,
        contract_scope_key=paper_contract["scope_key"],
        timeframe=command["timeframe"],
        side=side,
        role=role,
        reduce_only=reduce_only,
        manual_close=manual_close,
        target_position_id=command["position_id"] if reduce_only else None,
        qty=qty,
        price=price,
        entry_price=entry_price,
        stop_loss=command["stop_loss"],
        target=command["target"],
        filled_at=_paper_utc_datetime(order.get("filled_at")),
        signed_delta=signed_delta,
        order_payload=order_payload,
        create_command=create_command,
        incoming_order=incoming_order,
    )


def _paper_contract_query_scope(
    paper_contract: PaperContractIdentity | None,
    contract_scope_kind: PaperContractScopeKind | None = None,
) -> tuple[str, tuple[Any, ...]]:
    if contract_scope_kind is not None and contract_scope_kind not in {"instrument", "option"}:
        raise ValueError("PAPER_CONTRACT_SCOPE_KIND_INVALID")
    if paper_contract is not None and contract_scope_kind is not None:
        raise ValueError("PAPER_CONTRACT_SCOPE_QUERY_AMBIGUOUS")
    if contract_scope_kind is not None:
        return "contract_scope_kind = %s", (contract_scope_kind,)
    if paper_contract is None:
        return "contract_scope_kind = 'instrument'", ()
    if not isinstance(paper_contract, PaperContractIdentity):
        raise ValueError("PAPER_CONTRACT_IDENTITY_REQUIRED")
    return "contract_scope_key = %s", (paper_contract.scope_key,)


@dataclass(frozen=True, slots=True)
class _PaperPositionReadQuery:
    where: str
    limit_clause: str
    params: tuple[Any, ...]


def _paper_position_route_filter(
    instrument_id: str | None,
    route_fingerprint: str | None,
    paper_contract: PaperContractIdentity | None,
    contract_scope_kind: PaperContractScopeKind | None,
) -> tuple[list[str], list[Any]]:
    if (instrument_id is None) != (route_fingerprint is None):
        raise ValueError("PAPER_POSITION_ROUTE_IDENTITY_PAIR_REQUIRED")
    if instrument_id is None or route_fingerprint is None:
        if paper_contract is not None or contract_scope_kind is not None:
            raise ValueError("PAPER_POSITION_CONTRACT_SCOPE_REQUIRES_EXACT_ROUTE")
        return [], []
    conditions = ["instrument_id = %s", "route_fingerprint = %s"]
    params: list[Any] = [
        require_exact_identity_text(instrument_id, field="instrument_id"),
        require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
    ]
    scope_clause, scope_params = _paper_contract_query_scope(
        paper_contract,
        contract_scope_kind,
    )
    conditions.append(scope_clause)
    params.extend(scope_params)
    return conditions, params


def _paper_position_complete_ids_filter(
    complete_position_ids: tuple[str, ...] | None,
    *,
    instrument_id: str | None,
    route_fingerprint: str | None,
) -> tuple[list[str], list[Any]]:
    if complete_position_ids is None:
        return [], []
    if (
        not isinstance(complete_position_ids, tuple)
        or not complete_position_ids
        or any(
            not isinstance(position_id, str) or not position_id
            for position_id in complete_position_ids
        )
        or len(set(complete_position_ids)) != len(complete_position_ids)
    ):
        raise ValueError("PAPER_POSITION_COMPLETE_IDS_INVALID")
    if instrument_id is None or route_fingerprint is None:
        raise ValueError("PAPER_POSITION_COMPLETE_IDS_REQUIRE_EXACT_ROUTE")
    return ["id = ANY(%s)"], [list(complete_position_ids)]


def _paper_position_read_query(
    *,
    limit: int,
    status: str | None,
    instrument_id: str | None,
    route_fingerprint: str | None,
    timeframe: str | None,
    complete_position_ids: tuple[str, ...] | None,
    complete_open: bool,
    paper_contract: PaperContractIdentity | None,
    contract_scope_kind: PaperContractScopeKind | None,
) -> _PaperPositionReadQuery:
    if not isinstance(complete_open, bool):
        raise ValueError("PAPER_POSITION_COMPLETE_OPEN_INVALID")
    if complete_open and (status != "open" or complete_position_ids is not None):
        raise ValueError("PAPER_POSITION_COMPLETE_OPEN_REQUIRES_OPEN_STATUS")
    conditions, params = _paper_position_route_filter(
        instrument_id,
        route_fingerprint,
        paper_contract,
        contract_scope_kind,
    )
    id_conditions, id_params = _paper_position_complete_ids_filter(
        complete_position_ids,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
    )
    conditions.extend(id_conditions)
    params.extend(id_params)
    if timeframe is not None:
        timeframe_key = str(timeframe).strip()
        if not timeframe_key:
            raise ValueError("PAPER_POSITION_TIMEFRAME_REQUIRED")
        conditions.append("timeframe = %s")
        params.append(timeframe_key)
    if status:
        conditions.append("status = %s")
        params.append(str(status).lower())
    limit_clause = ""
    if complete_position_ids is None and not complete_open:
        limit_clause = "LIMIT %s"
        params.append(max(1, min(int(limit), 1000)))
    return _PaperPositionReadQuery(
        where=f"WHERE {' AND '.join(conditions)}" if conditions else "",
        limit_clause=limit_clause,
        params=tuple(params),
    )


def _require_paper_contract_match(
    row: dict[str, Any],
    expected: PaperContractIdentity,
    *,
    entity: str,
) -> None:
    if row.get(PAPER_CONTRACT_KEY) != expected.to_payload():
        raise ValueError(f"PAPER_{entity}_CONTRACT_METADATA_CONTRADICTORY")


def _paper_position_payload_from_order(
    order_id: str, order_payload: dict[str, Any], new_qty: float
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "indicator_position"
        if order_payload.get("indicator_source")
        else "manual_position",
        "entry_order_id": order_id,
        "side": "long" if new_qty > 0 else "short",
    }
    for key in ("instrument_id", "route_fingerprint", "provider_contract_id"):
        payload[key] = require_exact_identity_text(order_payload.get(key), field=key)
    provider = str(order_payload.get("provider") or "").strip().lower()
    if provider:
        payload["provider"] = provider
    contract_identity = require_paper_contract_identity(order_payload)
    payload[PAPER_CONTRACT_KEY] = contract_identity.to_payload()
    if order_payload.get("indicator_source"):
        payload["indicator_source"] = str(order_payload.get("indicator_source"))
    setup_source = str(order_payload.get("setup_source") or "").strip()
    if setup_source:
        payload["setup_source"] = setup_source
    if order_payload.get("entry_channel"):
        payload["entry_channel"] = str(order_payload.get("entry_channel"))
    if order_payload.get("signal_rank") is not None:
        payload["signal_rank"] = float(order_payload.get("signal_rank"))
    payload.update(_paper_position_signal_payload(order_payload))
    return payload


def _paper_position_signal_payload(order_payload: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    signal = order_payload.get("signal") if isinstance(order_payload.get("signal"), dict) else {}
    for key in (
        "signal_source",
        "signal_source_label",
        "confluence_sources",
        "setup",
        "code",
        "reason",
        "score",
        "action_card",
    ):
        value = order_payload.get(key)
        if value is None and isinstance(signal, dict):
            value = signal.get(key)
        if value is not None:
            payload[key] = value
    if isinstance(signal, dict) and signal:
        payload["signal"] = signal
    return payload


def _paper_order_command_payload(order: dict[str, Any]) -> dict[str, Any]:
    order_id = require_exact_identity_text(order.get("id"), field="PAPER_ORDER_ID")
    instrument_id = require_exact_identity_text(
        order.get("instrument_id"), field="PAPER_ORDER_INSTRUMENT_ID"
    )
    route_fingerprint = require_exact_identity_text(
        order.get("route_fingerprint"), field="PAPER_ORDER_ROUTE_FINGERPRINT"
    )
    provider_contract_id = require_exact_identity_text(
        order.get("provider_contract_id"), field="PAPER_ORDER_PROVIDER_CONTRACT_ID"
    )
    contract_identity = require_paper_contract_identity(order)
    symbol = str(order.get("symbol") or "").strip()
    provider = str(order.get("provider") or "").strip().lower()
    timeframe = str(order.get("timeframe") or "").strip()
    order_type = str(order.get("order_type") or "").strip().lower()
    role = str(order.get("role") or "entry")
    reduce_only = order.get("reduce_only", role in {"stop", "take", "close"})
    manual_close = role == "close" and reduce_only is True
    side_raw = str(order.get("side") or "").strip().lower()
    side = None if manual_close else side_raw
    qty = None if manual_close else float_or_none(order.get("qty"))
    entry = None if manual_close else float_or_none(order.get("entry"))
    stop_loss = float_or_none(order.get("stop_loss"))
    target = float_or_none(order.get("target"))
    use_stop_loss = order.get("use_stop_loss", stop_loss is not None)
    use_target = order.get("use_target", target is not None)
    nested_payload = order.get("payload")
    if nested_payload is None:
        nested_payload = {}
    if not isinstance(nested_payload, dict):
        raise ValueError("PAPER_ORDER_COMMAND_PAYLOAD_INVALID")
    canonical_payload = dict(nested_payload)
    canonical_payload.pop("paper_execution_config", None)
    if (
        not symbol
        or not provider
        or not timeframe
        or order_type not in {"market", "limit", "stop"}
        or role not in {"entry", "stop", "take", "close"}
        or not isinstance(reduce_only, bool)
        or not isinstance(use_stop_loss, bool)
        or not isinstance(use_target, bool)
        or (side is not None and side not in {"long", "short"})
        or (not manual_close and side not in {"long", "short"})
        or (not manual_close and (qty is None or qty <= 0))
        or (not manual_close and entry is None)
        or "requested_exit_price" in order
        or (order.get("stop_loss") is not None and stop_loss is None)
        or (order.get("target") is not None and target is None)
    ):
        raise ValueError("PAPER_ORDER_COMMAND_INVALID")
    optional_ids: dict[str, str | None] = {}
    for field in ("position_id", "parent_order_id", "oco_group_id"):
        raw = order.get(field)
        optional_ids[field] = (
            require_exact_identity_text(raw, field=f"PAPER_ORDER_{field.upper()}")
            if raw not in (None, "")
            else None
        )
    if reduce_only and optional_ids["position_id"] is None:
        raise ValueError("PAPER_REDUCE_ONLY_POSITION_ID_REQUIRED")
    command = {
        "id": order_id,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "symbol": symbol,
        "provider": provider,
        "provider_contract_id": provider_contract_id,
        PAPER_CONTRACT_KEY: contract_identity.to_payload(),
        "timeframe": timeframe,
        "side": side,
        "order_type": order_type,
        "qty": qty,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "use_stop_loss": use_stop_loss,
        "use_target": use_target,
        "role": role,
        "position_id": optional_ids["position_id"],
        "parent_order_id": optional_ids["parent_order_id"],
        "oco_group_id": optional_ids["oco_group_id"],
        "reduce_only": reduce_only,
        "source": str(order.get("source") or "manual"),
        "note": str(order.get("note") or ""),
        "exit_reason": str(order.get("exit_reason") or ""),
        "payload": canonical_payload,
    }
    command["payload"] = require_paper_order_protection(command)
    return command


def _paper_fill_distance_execution_update_allowed(
    existing_order: dict[str, Any],
    incoming_order: dict[str, Any],
    execution_command: dict[str, Any],
    *,
    fill_price: float,
) -> bool:
    existing_command = existing_order.get("execution_command")
    existing_create_command = existing_order.get("create_command")
    incoming_create_command = incoming_order.get("create_command")
    existing_payload = existing_order.get("payload")
    incoming_payload = incoming_order.get("payload")
    if (
        not isinstance(existing_command, dict)
        or not isinstance(existing_create_command, dict)
        or existing_create_command != incoming_create_command
        or not isinstance(existing_payload, dict)
        or not isinstance(incoming_payload, dict)
        or existing_payload.get(PAPER_PROTECTION_BASIS_KEY)
        != incoming_payload.get(PAPER_PROTECTION_BASIS_KEY)
        or existing_payload.get(PAPER_PROTECTION_INTENT_KEY)
        != incoming_payload.get(PAPER_PROTECTION_INTENT_KEY)
        or existing_command.get("role") != "entry"
        or existing_command.get("reduce_only") is not False
    ):
        return False
    intent = existing_payload.get(PAPER_PROTECTION_INTENT_KEY)
    if not isinstance(intent, dict) or intent.get("basis") != "fill_distance":
        return False
    existing_without_levels = {
        key: value for key, value in existing_command.items() if key not in {"stop_loss", "target"}
    }
    incoming_without_levels = {
        key: value for key, value in execution_command.items() if key not in {"stop_loss", "target"}
    }
    if existing_without_levels != incoming_without_levels:
        return False
    side = str(execution_command.get("side") or "")
    if side not in {"long", "short"}:
        return False
    for enabled_key, points_key, level_key, long_sign in (
        ("use_stop_loss", "stop_points", "stop_loss", -1.0),
        ("use_target", "target_points", "target", 1.0),
    ):
        enabled = execution_command.get(enabled_key) is True
        level = float_or_none(execution_command.get(level_key))
        if not enabled:
            if level is not None:
                return False
            continue
        points = float_or_none(intent.get(points_key))
        if points is None or points <= 0 or level is None:
            return False
        sign = long_sign if side == "long" else -long_sign
        expected = fill_price + sign * points
        tolerance = max(1e-9, abs(expected) * 1e-12)
        if abs(level - expected) > tolerance:
            return False
    return True


def _paper_entry_protective_qty(
    *,
    signed_delta: float,
    current_qty: float,
    previous_qty: float,
    reversed_position: bool,
) -> float | None:
    delta = float_or_none(signed_delta)
    current = float_or_none(current_qty)
    previous = float_or_none(previous_qty)
    if delta is None or current is None or previous is None:
        raise ValueError("PAPER_PROTECTIVE_QTY_INVALID")
    if reversed_position:
        if abs(current) <= 1e-9:
            raise ValueError("PAPER_PROTECTIVE_QTY_INVALID")
        return abs(current)
    if abs(previous) <= 1e-9 or previous * delta > 0:
        if abs(delta) <= 1e-9:
            raise ValueError("PAPER_PROTECTIVE_QTY_INVALID")
        return abs(delta)
    return None


def _paper_position_expectation_rejection(
    position: dict[str, Any] | None,
    expectation: Any,
) -> tuple[str, str] | None:
    if expectation is None:
        return None
    if not isinstance(expectation, dict):
        return (
            "PAPER_POSITION_EXPECTATION_INVALID",
            "paper position expectation must be an object",
        )
    state = expectation.get("state")
    if state == "absent":
        if set(expectation) != {"state"}:
            return (
                "PAPER_POSITION_EXPECTATION_INVALID",
                "absent paper position expectation has unexpected fields",
            )
        return (
            (
                "PAPER_POSITION_EXPECTATION_STALE",
                "paper position was opened before execution",
            )
            if position is not None
            else None
        )
    if state != "open" or set(expectation) != {
        "state",
        "position_id",
        "side",
        "qty",
        "updated_at",
    }:
        return (
            "PAPER_POSITION_EXPECTATION_INVALID",
            "open paper position expectation is incomplete",
        )
    try:
        expected_id = require_exact_identity_text(
            expectation.get("position_id"),
            field="PAPER_EXPECTED_POSITION_ID",
        )
        expected_updated_at = require_exact_identity_text(
            expectation.get("updated_at"),
            field="PAPER_EXPECTED_POSITION_UPDATED_AT",
        )
    except ValueError:
        return (
            "PAPER_POSITION_EXPECTATION_INVALID",
            "paper position expectation identity is invalid",
        )
    expected_side = expectation.get("side")
    expected_qty = float_or_none(expectation.get("qty"))
    if expected_side not in {"long", "short"} or expected_qty is None:
        return (
            "PAPER_POSITION_EXPECTATION_INVALID",
            "paper position expectation state is invalid",
        )
    if position is None:
        return (
            "PAPER_POSITION_EXPECTATION_STALE",
            "expected paper position is no longer open",
        )
    position_payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    current_qty = float_or_none(position.get("qty"))
    if (
        position.get("id") != expected_id
        or position_payload.get("side") != expected_side
        or current_qty is None
        or abs(current_qty - expected_qty) > 1e-9
        or position.get("updated_at") != expected_updated_at
    ):
        return (
            "PAPER_POSITION_EXPECTATION_STALE",
            "paper position changed before execution",
        )
    return None


def _append_paper_trade_event_with_cursor(
    cur: Any,
    event_type: str,
    *,
    order_id: str | None = None,
    trade_id: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    from psycopg.types.json import Jsonb

    event_payload = dict(payload or {})
    try:
        require_exact_identity_text(event_payload.get("instrument_id"), field="instrument_id")
        require_exact_identity_text(
            event_payload.get("route_fingerprint"), field="route_fingerprint"
        )
    except ValueError as exc:
        raise ValueError("PAPER_TRADE_EVENT_IDENTITY_REQUIRED") from exc
    cur.execute(
        """
        INSERT INTO paper_trade_events (event_type, order_id, trade_id, symbol, timeframe, payload)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            str(event_type or "event"),
            str(order_id) if order_id else None,
            str(trade_id) if trade_id else None,
            str(symbol or "") or None,
            str(timeframe or "") or None,
            Jsonb(event_payload),
        ),
    )


class PaperRepoMixin:
    """PostgreSQL repository mixin."""

    def upsert_paper_order(self, order: dict[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        command = _paper_order_command_payload(order)
        storage_side = command["side"]
        storage_qty = command["qty"]
        storage_entry = command["entry"]
        if command["role"] == "close" and command["reduce_only"] is True:
            storage_side = str(order.get("side") or "").strip().lower()
            storage_qty = float_or_none(order.get("qty"))
            storage_entry = float_or_none(order.get("entry"))
            if (
                storage_side not in {"long", "short"}
                or (storage_qty is None or storage_qty <= 0)
                or storage_entry is None
            ):
                raise ValueError("PAPER_CLOSE_RESERVATION_SHAPE_INVALID")
        order_protection_payload = require_paper_order_protection(order)
        order_id = command["id"]
        instrument_id = command["instrument_id"]
        route_fingerprint = command["route_fingerprint"]
        status = str(order.get("status") or "pending").lower()
        if status != "pending":
            raise ValueError("PAPER_ORDER_PENDING_WRITE_REQUIRED")
        created_at = _paper_utc_datetime(order.get("created_at"))
        updated_at = _paper_utc_datetime(order.get("updated_at"), created_at)
        stored_payload = {
            **dict(order),
            PAPER_CONTRACT_KEY: command[PAPER_CONTRACT_KEY],
            "payload": order_protection_payload,
            "create_command": command,
            "execution_command": command,
        }
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction():
                cur = conn.cursor()
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    INSERT INTO paper_orders (
                        id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                        use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                        role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                              use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                              role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                    """,
                    (
                        order_id,
                        command["symbol"],
                        command["timeframe"],
                        storage_side,
                        command["order_type"],
                        status,
                        storage_qty,
                        storage_entry,
                        command["stop_loss"],
                        command["target"],
                        command["use_stop_loss"],
                        command["use_target"],
                        created_at,
                        updated_at,
                        None,
                        None,
                        command["role"],
                        command["position_id"],
                        command["parent_order_id"],
                        command["oco_group_id"],
                        command["reduce_only"],
                        Jsonb(stored_payload),
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        SELECT id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                               use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                               role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                        FROM paper_orders
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (order_id,),
                    )
                    existing_row = cur.fetchone()
                    if existing_row is None:
                        raise ValueError("PAPER_ORDER_IDEMPOTENCY_STATE_MISSING")
                    saved = _paper_order_from_row(existing_row)
                    if (
                        saved.get("instrument_id") != instrument_id
                        or saved.get("route_fingerprint") != route_fingerprint
                    ):
                        raise ValueError("PAPER_ORDER_ID_ROUTE_COLLISION")
                    if saved.get("create_command") != command:
                        raise PaperJournalError("PAPER_ORDER_ID_COMMAND_COLLISION")
                    return saved
                saved = _paper_order_from_row(row)
                _append_paper_trade_event_with_cursor(
                    cur,
                    "order_placed",
                    order_id=order_id,
                    symbol=command["symbol"],
                    timeframe=command["timeframe"],
                    payload=saved,
                )
        paper_journal_runtime.mark_committed()
        return saved

    def read_paper_order(
        self,
        order_id: str,
        *,
        instrument_id: str,
        route_fingerprint: str,
        paper_contract: PaperContractIdentity | None = None,
    ) -> dict[str, Any] | None:
        order_key = require_exact_identity_text(
            order_id,
            field="PAPER_ORDER_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_ORDER_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_ORDER_ROUTE_FINGERPRINT",
        )
        scope_clause, scope_params = _paper_contract_query_scope(paper_contract)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                           use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                           role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                    FROM paper_orders
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND {scope_clause}
                    LIMIT 1
                    """,
                    (order_key, identity, fingerprint, *scope_params),
                )
                row = cur.fetchone()
        if row is None:
            return None
        saved = _paper_order_from_row(row)
        if paper_contract is not None:
            _require_paper_contract_match(saved, paper_contract, entity="ORDER")
        return saved

    def read_paper_orders(
        self,
        limit: int = 100,
        status: str | None = None,
        *,
        instrument_id: str | None = None,
        route_fingerprint: str | None = None,
        since: datetime | None = None,
        complete_pending: bool = False,
        paper_contract: PaperContractIdentity | None = None,
        contract_scope_kind: PaperContractScopeKind | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if (instrument_id is None) != (route_fingerprint is None):
            raise ValueError("paper order query requires a complete route identity pair")
        if instrument_id is not None and route_fingerprint is not None:
            conditions.extend(("instrument_id = %s", "route_fingerprint = %s"))
            params.extend(
                (
                    require_exact_identity_text(
                        instrument_id,
                        field="PAPER_ORDER_INSTRUMENT_ID",
                    ),
                    require_exact_identity_text(
                        route_fingerprint,
                        field="PAPER_ORDER_ROUTE_FINGERPRINT",
                    ),
                )
            )
            scope_clause, scope_params = _paper_contract_query_scope(
                paper_contract,
                contract_scope_kind,
            )
            conditions.append(scope_clause)
            params.extend(scope_params)
        elif paper_contract is not None or contract_scope_kind is not None:
            raise ValueError("PAPER_ORDER_CONTRACT_SCOPE_REQUIRES_EXACT_ROUTE")
        if status:
            conditions.append("status = %s")
            params.append(str(status).lower())
        if since is not None:
            conditions.append("COALESCE(filled_at, updated_at, created_at) >= %s")
            params.append(_paper_utc_datetime(since))
        if complete_pending and status != "pending":
            raise ValueError("PAPER_ORDER_COMPLETE_QUERY_REQUIRES_PENDING_STATUS")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = ""
        if not complete_pending:
            limit_clause = "LIMIT %s"
            params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                           use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                           role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                    FROM paper_orders
                    {where}
                    ORDER BY updated_at DESC
                    {limit_clause}
                    """,
                    params,
                )
                rows = cur.fetchall()
        saved_orders = [_paper_order_from_row(row) for row in rows]
        if paper_contract is not None:
            for saved in saved_orders:
                _require_paper_contract_match(saved, paper_contract, entity="ORDER")
        return saved_orders

    def read_paper_trade_events(
        self,
        limit: int = 200,
        *,
        instrument_id: str | None = None,
        route_fingerprint: str | None = None,
        trade_ids: tuple[str, ...] | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        if (instrument_id is None) != (route_fingerprint is None):
            raise ValueError("PAPER_EVENT_ROUTE_IDENTITY_PAIR_REQUIRED")
        conditions: list[str] = []
        params: list[Any] = []
        if instrument_id is not None and route_fingerprint is not None:
            conditions.extend(("instrument_id = %s", "route_fingerprint = %s"))
            params.extend(
                (
                    require_exact_identity_text(
                        instrument_id,
                        field="PAPER_EVENT_INSTRUMENT_ID",
                    ),
                    require_exact_identity_text(
                        route_fingerprint,
                        field="PAPER_EVENT_ROUTE_FINGERPRINT",
                    ),
                )
            )
        if trade_ids is not None:
            if instrument_id is None or route_fingerprint is None:
                raise ValueError("PAPER_EVENT_TRADE_IDS_REQUIRE_EXACT_ROUTE")
            if not trade_ids:
                return []
            exact_trade_ids = tuple(
                require_exact_identity_text(trade_id, field="PAPER_EVENT_TRADE_ID")
                for trade_id in trade_ids
            )
            if len(set(exact_trade_ids)) != len(exact_trade_ids):
                raise ValueError("PAPER_EVENT_TRADE_IDS_DUPLICATE")
            conditions.append("trade_id = ANY(%s)")
            params.append(list(exact_trade_ids))
        if event_type is not None:
            exact_event_type = require_exact_identity_text(
                event_type,
                field="PAPER_EVENT_TYPE",
            )
            conditions.append("event_type = %s")
            params.append(exact_event_type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT instrument_id, route_fingerprint, event_type, order_id, trade_id,
                           symbol, timeframe, payload, created_at
                    FROM paper_trade_events
                    {where}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
        return [
            {
                "instrument_id": require_exact_identity_text(row[0], field="instrument_id"),
                "route_fingerprint": require_exact_identity_text(row[1], field="route_fingerprint"),
                "event_type": str(row[2]),
                "order_id": str(row[3] or ""),
                "trade_id": str(row[4] or ""),
                "symbol": str(row[5] or ""),
                "timeframe": str(row[6] or ""),
                "payload": row[7] or {},
                "created_at": ensure_utc(row[8]).isoformat() if row[8] is not None else None,
            }
            for row in rows
        ]

    def update_paper_order_if_pending(
        self,
        order: dict[str, Any],
        *,
        instrument_id: str,
        route_fingerprint: str,
    ) -> dict[str, Any] | None:
        from psycopg.types.json import Jsonb

        order_id = require_exact_identity_text(
            order.get("id"),
            field="PAPER_ORDER_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_ORDER_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_ORDER_ROUTE_FINGERPRINT",
        )
        if (
            require_exact_identity_text(
                order.get("instrument_id"),
                field="PAPER_ORDER_PAYLOAD_INSTRUMENT_ID",
            )
            != identity
            or require_exact_identity_text(
                order.get("route_fingerprint"),
                field="PAPER_ORDER_PAYLOAD_ROUTE_FINGERPRINT",
            )
            != fingerprint
        ):
            raise ValueError("PAPER_ORDER_UPDATE_IDENTITY_MISMATCH")
        updated_at = _paper_utc_datetime(order.get("updated_at"))
        entry = float_or_none(order.get("entry"))
        stop_loss = float_or_none(order.get("stop_loss"))
        target = float_or_none(order.get("target"))
        if (
            entry is None
            or (order.get("stop_loss") is not None and stop_loss is None)
            or (order.get("target") is not None and target is None)
        ):
            raise ValueError("paper order prices must be finite numbers")
        execution_command = _paper_order_command_payload(order)
        contract_scope_key = execution_command[PAPER_CONTRACT_KEY]["scope_key"]
        order_protection_payload = require_paper_order_protection(order)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    UPDATE paper_orders
                    SET entry = %s,
                        stop_loss = %s,
                        target = %s,
                        use_stop_loss = %s,
                        use_target = %s,
                        updated_at = %s,
                        payload = payload || %s::jsonb
                    WHERE id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND contract_scope_key = %s
                      AND status = 'pending'
                    RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                              use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                              role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                    """,
                    (
                        entry,
                        stop_loss,
                        target,
                        execution_command["use_stop_loss"],
                        execution_command["use_target"],
                        updated_at,
                        Jsonb(
                            {
                                **dict(order),
                                "payload": order_protection_payload,
                                "execution_command": execution_command,
                            }
                        ),
                        order_id,
                        identity,
                        fingerprint,
                        contract_scope_key,
                    ),
                )
                row = cur.fetchone()
                saved = _paper_order_from_row(row) if row else None
                if saved is not None:
                    _append_paper_trade_event_with_cursor(
                        cur,
                        "order_updated",
                        order_id=order_id,
                        symbol=saved.get("symbol"),
                        timeframe=saved.get("timeframe"),
                        payload=saved,
                    )
        if saved is not None:
            paper_journal_runtime.mark_committed()
        return saved

    def cancel_paper_order_if_pending(
        self,
        order_id: str,
        cancelled_at: datetime | None = None,
        *,
        instrument_id: str,
        route_fingerprint: str,
        paper_contract: PaperContractIdentity | None = None,
    ) -> dict[str, Any] | None:
        order_key = require_exact_identity_text(
            order_id,
            field="PAPER_ORDER_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_ORDER_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_ORDER_ROUTE_FINGERPRINT",
        )
        scope = paper_contract.scope_key if paper_contract is not None else None
        ts = ensure_utc(cancelled_at or utc_now())
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                self._cancel_paper_order_if_pending_with_cursor(
                    cur,
                    order_key,
                    ts,
                    instrument_id=identity,
                    route_fingerprint=fingerprint,
                    contract_scope_key=scope,
                )
                row = cur.fetchone()
                saved = _paper_order_from_row(row) if row else None
                if saved is not None:
                    _append_paper_trade_event_with_cursor(
                        cur,
                        "order_cancelled",
                        order_id=order_key,
                        symbol=saved.get("symbol"),
                        timeframe=saved.get("timeframe"),
                        payload=saved,
                    )
        if saved is not None:
            paper_journal_runtime.mark_committed()
        return saved

    def reject_paper_order_execution_if_pending(
        self,
        order_id: str,
        rejected_at: datetime | None = None,
        *,
        instrument_id: str,
        route_fingerprint: str,
        code: str,
        reason: str,
        execution: dict[str, Any],
        paper_contract: PaperContractIdentity | None = None,
    ) -> dict[str, Any] | None:
        order_key = require_exact_identity_text(
            order_id,
            field="PAPER_ORDER_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_ORDER_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_ORDER_ROUTE_FINGERPRINT",
        )
        scope = paper_contract.scope_key if paper_contract is not None else None
        rejection_code = str(code or "").strip()
        rejection_reason = str(reason or "").strip()
        if not rejection_code or not rejection_reason or not isinstance(execution, dict):
            raise ValueError("PAPER_EXECUTION_REJECTION_PAYLOAD_INVALID")
        rejection = {
            "code": rejection_code,
            "reason": rejection_reason,
            "execution": dict(execution),
        }
        ts = ensure_utc(rejected_at or utc_now())
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                self._cancel_paper_order_if_pending_with_cursor(
                    cur,
                    order_key,
                    ts,
                    instrument_id=identity,
                    route_fingerprint=fingerprint,
                    contract_scope_key=scope,
                    payload_patch={"execution_rejection": rejection},
                )
                row = cur.fetchone()
                if row is None:
                    return None
                cancelled_order = _paper_order_from_row(row)
                _append_paper_trade_event_with_cursor(
                    cur,
                    "order_execution_rejected",
                    order_id=order_key,
                    symbol=cancelled_order.get("symbol"),
                    timeframe=cancelled_order.get("timeframe"),
                    payload={
                        "instrument_id": identity,
                        "route_fingerprint": fingerprint,
                        PAPER_CONTRACT_KEY: cancelled_order[PAPER_CONTRACT_KEY],
                        "order_id": order_key,
                        **rejection,
                    },
                )
        paper_journal_runtime.mark_committed()
        return cancelled_order

    def _cancel_paper_order_if_pending_with_cursor(
        self,
        cur: Any,
        order_id: str,
        ts: datetime,
        *,
        instrument_id: str,
        route_fingerprint: str,
        contract_scope_key: str | None,
        payload_patch: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Jsonb

        order_key = require_exact_identity_text(
            order_id,
            field="PAPER_ORDER_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_ORDER_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_ORDER_ROUTE_FINGERPRINT",
        )
        scope_clause = (
            "contract_scope_key = %s"
            if contract_scope_key is not None
            else "contract_scope_kind = 'instrument'"
        )
        scope_params: tuple[Any, ...] = (
            (require_exact_identity_text(contract_scope_key, field="PAPER_CONTRACT_SCOPE_KEY"),)
            if contract_scope_key is not None
            else ()
        )
        cur.execute(
            f"""
            UPDATE paper_orders
            SET status = 'cancelled',
                updated_at = %s,
                payload = payload
                    || jsonb_build_object('cancelled_at', %s::text)
                    || %s::jsonb
            WHERE id = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND {scope_clause}
              AND status = 'pending'
            RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                      use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                      role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            """,
            (
                ts,
                ts.isoformat(),
                Jsonb(dict(payload_patch or {})),
                order_key,
                identity,
                fingerprint,
                *scope_params,
            ),
        )

    def _sync_paper_protective_orders(
        self,
        cur: Any,
        position_id: str,
        qty: float,
        status: str = "open",
        *,
        instrument_id: str,
        route_fingerprint: str,
        contract_scope_key: str,
        updated_at: datetime,
    ) -> list[dict[str, Any]]:
        if status == "open" and abs(qty) > 1e-9:
            from psycopg.types.json import Jsonb

            cur.execute(
                """
                SELECT id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                       use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                       role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                FROM paper_orders
                WHERE position_id = %s
                  AND instrument_id = %s
                  AND route_fingerprint = %s
                  AND contract_scope_key = %s
                  AND reduce_only = true
                  AND status = 'pending'
                ORDER BY created_at ASC, id ASC
                FOR UPDATE
                """,
                (
                    position_id,
                    instrument_id,
                    route_fingerprint,
                    contract_scope_key,
                ),
            )
            pending = [_paper_order_from_row(row) for row in cur.fetchall()]
            groups: dict[str, list[dict[str, Any]]] = {}
            for pending_order in pending:
                group_key = (
                    str(pending_order.get("oco_group_id") or "")
                    or str(pending_order.get("parent_order_id") or "")
                    or require_exact_identity_text(
                        pending_order.get("id"),
                        field="PAPER_PROTECTIVE_ORDER_ID",
                    )
                )
                groups.setdefault(group_key, []).append(pending_order)
            remaining_qty = abs(float(qty))
            cancelled_orders: list[dict[str, Any]] = []
            for group_orders in groups.values():
                group_qty = max(abs(float(item.get("qty") or 0.0)) for item in group_orders)
                admitted_qty = min(group_qty, remaining_qty)
                remaining_qty = max(0.0, remaining_qty - admitted_qty)
                for pending_order in group_orders:
                    pending_order_id = require_exact_identity_text(
                        pending_order.get("id"),
                        field="PAPER_PROTECTIVE_ORDER_ID",
                    )
                    if admitted_qty <= 1e-9:
                        self._cancel_paper_order_if_pending_with_cursor(
                            cur,
                            pending_order_id,
                            updated_at,
                            instrument_id=instrument_id,
                            route_fingerprint=route_fingerprint,
                            contract_scope_key=contract_scope_key,
                        )
                        cancelled_row = cur.fetchone()
                        if cancelled_row is not None:
                            cancelled_orders.append(_paper_order_from_row(cancelled_row))
                        continue
                    current_order_qty = abs(float(pending_order.get("qty") or 0.0))
                    if abs(current_order_qty - admitted_qty) <= 1e-9:
                        continue
                    resized_order = {
                        **pending_order,
                        "qty": admitted_qty,
                        "updated_at": updated_at.isoformat(),
                    }
                    execution_command = _paper_order_command_payload(resized_order)
                    resized_protection_payload = require_paper_order_protection(resized_order)
                    cur.execute(
                        """
                        UPDATE paper_orders
                        SET qty = %s,
                            updated_at = %s,
                            payload = payload || %s::jsonb
                        WHERE id = %s
                          AND instrument_id = %s
                          AND route_fingerprint = %s
                          AND contract_scope_key = %s
                          AND status = 'pending'
                        """,
                        (
                            admitted_qty,
                            updated_at,
                            Jsonb(
                                {
                                    **resized_order,
                                    "payload": resized_protection_payload,
                                    "execution_command": execution_command,
                                }
                            ),
                            pending_order_id,
                            instrument_id,
                            route_fingerprint,
                            contract_scope_key,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("PAPER_PROTECTIVE_RESIZE_CARDINALITY_INVALID")
            return cancelled_orders
        cur.execute(
            """
            UPDATE paper_orders
            SET status = 'cancelled', updated_at = %s
            WHERE position_id = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND contract_scope_key = %s
              AND reduce_only = true
              AND status = 'pending'
            RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                      use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                      role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            """,
            (
                updated_at,
                position_id,
                instrument_id,
                route_fingerprint,
                contract_scope_key,
            ),
        )
        return [_paper_order_from_row(row) for row in cur.fetchall()]

    def _reserve_paper_fill_order(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        Jsonb: Any,
    ) -> PaperFillResult | None:
        reservation_payload = {
            **fill.incoming_order,
            "create_command": fill.create_command,
            "execution_command": fill.command,
        }
        cur.execute(
            """
            INSERT INTO paper_orders (
                id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            )
            VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                      use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                      role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            """,
            (
                fill.order_id,
                fill.symbol,
                fill.timeframe,
                fill.side or "long",
                fill.command["order_type"],
                fill.qty if fill.qty is not None else 1.0,
                fill.entry_price,
                fill.stop_loss,
                fill.target,
                fill.command["use_stop_loss"],
                fill.command["use_target"],
                _paper_utc_datetime(fill.order.get("created_at"), fill.filled_at),
                _paper_utc_datetime(fill.order.get("updated_at"), fill.filled_at),
                fill.role,
                fill.target_position_id,
                fill.command["parent_order_id"],
                fill.command["oco_group_id"],
                fill.reduce_only,
                Jsonb(reservation_payload),
            ),
        )
        if cur.fetchone() is not None:
            return None
        return self._paper_fill_existing_order_outcome(cur, fill)

    def _paper_fill_existing_order_outcome(
        self,
        cur: Any,
        fill: _PaperFillCommand,
    ) -> PaperFillResult | None:
        cur.execute(
            """
            SELECT id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                   use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                   role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            FROM paper_orders
            WHERE id = %s
            FOR UPDATE
            """,
            (fill.order_id,),
        )
        existing_row = cur.fetchone()
        if existing_row is None:
            raise ValueError("PAPER_ORDER_IDEMPOTENCY_STATE_MISSING")
        existing_order = _paper_order_from_row(existing_row)
        if (
            existing_order.get("instrument_id") != fill.instrument_id
            or existing_order.get("route_fingerprint") != fill.route_fingerprint
        ):
            raise ValueError("PAPER_ORDER_ID_ROUTE_COLLISION")
        status = str(existing_order.get("status") or "")
        if existing_order.get("create_command") != fill.create_command:
            raise PaperJournalError("PAPER_ORDER_ID_COMMAND_COLLISION")
        if status == "filled":
            return self._paper_fill_replay_outcome(cur, fill, existing_order)
        if status == "cancelled" and fill.manual_close:
            return self._paper_manual_close_replay_outcome(cur, fill, existing_order)
        command_matches = existing_order.get("execution_command") == fill.command
        if not command_matches and not (
            status == "pending"
            and _paper_fill_distance_execution_update_allowed(
                existing_order,
                fill.incoming_order,
                fill.command,
                fill_price=fill.price,
            )
        ):
            raise PaperJournalError("PAPER_ORDER_ID_COMMAND_COLLISION")
        if status != "pending":
            raise ValueError("PAPER_ORDER_NOT_FILLABLE")
        return None

    def _paper_fill_replay_outcome(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        existing_order: dict[str, Any],
    ) -> PaperFillResult:
        cur.execute(
            """
            SELECT id, order_id, position_id, symbol, timeframe, side, qty, price, role,
                   reduce_only, pnl_points, filled_at, payload
            FROM paper_fills
            WHERE order_id = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND contract_scope_key = %s
            """,
            (
                fill.order_id,
                fill.instrument_id,
                fill.route_fingerprint,
                fill.contract_scope_key,
            ),
        )
        fill_rows = cur.fetchall()
        if len(fill_rows) != 1:
            raise ValueError("PAPER_ORDER_IDEMPOTENCY_FILL_INVALID")
        replay_fill = _paper_fill_from_row(fill_rows[0])
        position_id = require_exact_identity_text(
            replay_fill.get("position_id"), field="PAPER_POSITION_ID"
        )
        position = self._paper_position_by_id(cur, fill, position_id)
        if position is None:
            raise ValueError("PAPER_ORDER_IDEMPOTENCY_POSITION_MISSING")
        return {
            "outcome": "no_op",
            "order": existing_order,
            "position": position,
            "fill": replay_fill,
            "created_orders": [],
            "cancelled_orders": [],
            "closed_position": None,
            "opened_new_position": False,
        }

    def _paper_manual_close_replay_outcome(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        existing_order: dict[str, Any],
    ) -> PaperFillResult:
        position = self._paper_position_by_id(cur, fill, fill.target_position_id)
        if position is None:
            raise ValueError("PAPER_ORDER_IDEMPOTENCY_POSITION_MISSING")
        if (
            str(position.get("status") or "") != "closed"
            or abs(float(position.get("qty") or 0.0)) > 1e-9
        ):
            raise ValueError("PAPER_ORDER_NOT_FILLABLE")
        return {
            "outcome": "no_op",
            "order": existing_order,
            "position": position,
            "fill": None,
            "created_orders": [],
            "cancelled_orders": [],
            "closed_position": None,
            "opened_new_position": False,
        }

    def _paper_position_by_id(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position_id: str | None,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT id, symbol, timeframe, status, qty, avg_entry, opened_at, updated_at,
                   closed_at, realized_pnl, payload
            FROM paper_positions
            WHERE id = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND contract_scope_key = %s
            """,
            (
                position_id,
                fill.instrument_id,
                fill.route_fingerprint,
                fill.contract_scope_key,
            ),
        )
        row = cur.fetchone()
        return _paper_position_from_row(row) if row is not None else None

    def _locked_paper_fill_position(
        self,
        cur: Any,
        fill: _PaperFillCommand,
    ) -> dict[str, Any] | None:
        cur.execute(
            """
            SELECT id, symbol, timeframe, status, qty, avg_entry, opened_at, updated_at,
                   closed_at, realized_pnl, payload
            FROM paper_positions
            WHERE instrument_id = %s AND route_fingerprint = %s
              AND contract_scope_key = %s
              AND timeframe = %s AND status = 'open'
              AND (%s::text IS NULL OR id = %s)
            ORDER BY opened_at ASC
            LIMIT 1
            FOR UPDATE
            """,
            (
                fill.instrument_id,
                fill.route_fingerprint,
                fill.contract_scope_key,
                fill.timeframe,
                fill.target_position_id,
                fill.target_position_id,
            ),
        )
        row = cur.fetchone()
        position = _paper_position_from_row(row) if row else None
        if position is not None and position.get(PAPER_CONTRACT_KEY) != fill.paper_contract:
            raise ValueError("PAPER_POSITION_CONTRACT_METADATA_CONTRADICTORY")
        return position

    def _paper_position_expectation_outcome(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position: dict[str, Any] | None,
    ) -> PaperFillResult | None:
        rejection_values = _paper_position_expectation_rejection(
            position,
            fill.order_payload.get("position_expectation"),
        )
        if rejection_values is None:
            return None
        rejection_code, rejection_reason = rejection_values
        rejection = {
            "code": rejection_code,
            "reason": rejection_reason,
            "execution": {
                "fill_price": fill.price,
                "source": str(fill.order.get("execution_source") or ""),
                "ts": fill.order.get("execution_ts"),
                "bar_slot": float_or_none(fill.order.get("execution_bar_slot")),
                "planned_entry": float_or_none(fill.order_payload.get("planned_entry")),
                "analysis_bar_ts": fill.order_payload.get("analysis_bar_ts"),
                "analysis_generation": fill.order_payload.get("analysis_generation"),
            },
        }
        self._cancel_paper_order_if_pending_with_cursor(
            cur,
            fill.order_id,
            fill.filled_at,
            instrument_id=fill.instrument_id,
            route_fingerprint=fill.route_fingerprint,
            contract_scope_key=fill.contract_scope_key,
            payload_patch={"execution_rejection": rejection},
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("PAPER_POSITION_EXPECTATION_CANCEL_RESULT_MISSING")
        cancelled_order = _paper_order_from_row(row)
        _append_paper_trade_event_with_cursor(
            cur,
            "order_execution_rejected",
            order_id=fill.order_id,
            symbol=fill.symbol,
            timeframe=fill.timeframe,
            payload={
                "instrument_id": fill.instrument_id,
                "route_fingerprint": fill.route_fingerprint,
                PAPER_CONTRACT_KEY: fill.paper_contract,
                "order_id": fill.order_id,
                **rejection,
            },
        )
        return {
            "outcome": "cancelled",
            "order": cancelled_order,
            "position": position,
            "fill": None,
            "created_orders": [],
            "cancelled_orders": [cancelled_order],
            "closed_position": None,
            "opened_new_position": False,
        }

    def _paper_stale_reduce_only_outcome(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position: dict[str, Any] | None,
    ) -> PaperFillResult | None:
        if not fill.reduce_only or position is not None:
            return None
        self._cancel_paper_order_if_pending_with_cursor(
            cur,
            fill.order_id,
            fill.filled_at,
            instrument_id=fill.instrument_id,
            route_fingerprint=fill.route_fingerprint,
            contract_scope_key=fill.contract_scope_key,
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError("PAPER_ORDER_STALE_CANCEL_RESULT_MISSING")
        cancelled = _paper_order_from_row(row)
        current_position = (
            self._paper_position_by_id(cur, fill, fill.target_position_id)
            if fill.manual_close
            else None
        )
        return {
            "outcome": "no_op" if fill.manual_close else "cancelled",
            "order": cancelled,
            "position": current_position,
            "fill": None,
            "created_orders": [],
            "cancelled_orders": [cancelled],
            "closed_position": None,
            "opened_new_position": False,
        }

    def _paper_fill_delta(
        self,
        fill: _PaperFillCommand,
        position_qty: float,
    ) -> tuple[float, str, float]:
        qty = fill.qty
        side = fill.side
        signed_delta = fill.signed_delta
        if fill.reduce_only:
            if abs(position_qty) <= 1e-9:
                raise ValueError("PAPER_REDUCE_ONLY_POSITION_EMPTY")
            if fill.manual_close:
                qty = abs(position_qty)
                side = "short" if position_qty > 0 else "long"
                signed_delta = -position_qty
            else:
                if position_qty * signed_delta >= 0:
                    raise ValueError("PAPER_REDUCE_ONLY_DIRECTION_INVALID")
                qty = min(abs(position_qty), abs(signed_delta))
                signed_delta = -qty if position_qty > 0 else qty
        if qty is None:
            raise ValueError("PAPER_FILL_QUANTITY_REQUIRED")
        return qty, side, signed_delta

    def _increase_paper_position(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position: dict[str, Any] | None,
        *,
        position_id: str,
        position_qty: float,
        average_entry: float,
        signed_delta: float,
        Jsonb: Any,
    ) -> bool:
        new_qty = position_qty + signed_delta
        new_avg = ((abs(position_qty) * average_entry) + (abs(signed_delta) * fill.price)) / max(
            abs(new_qty), 1e-9
        )
        if position is None:
            cur.execute(
                """
                INSERT INTO paper_positions (
                    id, symbol, timeframe, status, qty, avg_entry, opened_at, updated_at, payload
                )
                VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s)
                """,
                (
                    position_id,
                    fill.symbol,
                    fill.timeframe,
                    new_qty,
                    new_avg,
                    fill.filled_at,
                    fill.filled_at,
                    Jsonb(
                        _paper_position_payload_from_order(
                            fill.order_id,
                            fill.order_payload,
                            new_qty,
                        )
                    ),
                ),
            )
            return True
        if fill.reduce_only:
            raise ValueError("PAPER_REDUCE_ONLY_REVERSAL_BLOCKED")
        cur.execute(
            """
            UPDATE paper_positions
            SET qty = %s, avg_entry = %s, updated_at = %s
            WHERE id = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND contract_scope_key = %s
            """,
            (
                new_qty,
                new_avg,
                fill.filled_at,
                position_id,
                fill.instrument_id,
                fill.route_fingerprint,
                fill.contract_scope_key,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError("PAPER_POSITION_UPDATE_CARDINALITY_INVALID")
        return False

    def _reduce_paper_position(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position: dict[str, Any],
        *,
        position_id: str,
        position_qty: float,
        average_entry: float,
        signed_delta: float,
        Jsonb: Any,
    ) -> tuple[str, float, dict[str, Any] | None, bool]:
        close_qty = min(abs(position_qty), abs(signed_delta))
        pnl = (
            (fill.price - average_entry) * close_qty
            if position_qty > 0
            else (average_entry - fill.price) * close_qty
        )
        new_qty = position_qty + signed_delta
        if abs(new_qty) <= 1e-9:
            self._close_paper_position_exact(cur, fill, position_id, pnl)
            return position_id, pnl, None, False
        if position_qty * new_qty > 0:
            cur.execute(
                """
                UPDATE paper_positions
                SET qty = %s, updated_at = %s, realized_pnl = realized_pnl + %s
                WHERE id = %s
                  AND instrument_id = %s
                  AND route_fingerprint = %s
                  AND contract_scope_key = %s
                """,
                (
                    new_qty,
                    fill.filled_at,
                    pnl,
                    position_id,
                    fill.instrument_id,
                    fill.route_fingerprint,
                    fill.contract_scope_key,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError("PAPER_POSITION_UPDATE_CARDINALITY_INVALID")
            return position_id, pnl, None, False
        self._close_paper_position_exact(cur, fill, position_id, pnl)
        reversal_position_id = f"pos-{fill.order_id}-rev"
        cur.execute(
            """
            INSERT INTO paper_positions (
                id, symbol, timeframe, status, qty, avg_entry, opened_at, updated_at, payload
            )
            VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s)
            """,
            (
                reversal_position_id,
                fill.symbol,
                fill.timeframe,
                new_qty,
                fill.price,
                fill.filled_at,
                fill.filled_at,
                Jsonb(
                    _paper_position_payload_from_order(
                        fill.order_id,
                        {**fill.order_payload, "reversal": True},
                        new_qty,
                    )
                ),
            ),
        )
        return reversal_position_id, pnl, position, True

    def _close_paper_position_exact(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position_id: str,
        pnl: float,
    ) -> None:
        cur.execute(
            """
            UPDATE paper_positions
            SET qty = 0, status = 'closed', closed_at = %s, updated_at = %s,
                realized_pnl = realized_pnl + %s
            WHERE id = %s
              AND instrument_id = %s
              AND route_fingerprint = %s
              AND contract_scope_key = %s
            """,
            (
                fill.filled_at,
                fill.filled_at,
                pnl,
                position_id,
                fill.instrument_id,
                fill.route_fingerprint,
                fill.contract_scope_key,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError("PAPER_POSITION_UPDATE_CARDINALITY_INVALID")

    def _transition_paper_position(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        position: dict[str, Any] | None,
        Jsonb: Any,
    ) -> _PaperPositionTransition:
        position_id = (
            str(position.get("id"))
            if position
            else str(fill.order.get("position_id") or f"pos-{fill.order_id}")
        )
        position_qty = float_or_none(position.get("qty")) if position else 0.0
        average_entry = (
            float_or_none(position.get("avg_entry"))
            if position and position.get("avg_entry") is not None
            else fill.price
        )
        if position_qty is None or average_entry is None:
            raise ValueError("stored paper position contains non-finite values")
        qty, side, signed_delta = self._paper_fill_delta(fill, position_qty)
        if position_qty == 0 or position_qty * signed_delta > 0:
            opened_new = self._increase_paper_position(
                cur,
                fill,
                position,
                position_id=position_id,
                position_qty=position_qty,
                average_entry=average_entry,
                signed_delta=signed_delta,
                Jsonb=Jsonb,
            )
            pnl = 0.0
            closed_position = None
        else:
            if position is None:
                raise ValueError("PAPER_POSITION_REQUIRED")
            position_id, pnl, closed_position, opened_new = self._reduce_paper_position(
                cur,
                fill,
                position,
                position_id=position_id,
                position_qty=position_qty,
                average_entry=average_entry,
                signed_delta=signed_delta,
                Jsonb=Jsonb,
            )
        return _PaperPositionTransition(
            position_id=position_id,
            previous_qty=position_qty,
            side=side,
            qty=qty,
            signed_delta=signed_delta,
            pnl=pnl,
            closed_position=closed_position,
            opened_new_position=opened_new,
        )

    def _persist_paper_fill_records(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        transition: _PaperPositionTransition,
        Jsonb: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        committed_payload = {
            **fill.incoming_order,
            "status": "filled",
            "side": transition.side,
            "qty": transition.qty,
            "position_id": transition.position_id,
            "reduce_only": fill.reduce_only,
            "create_command": fill.create_command,
            "execution_command": fill.command,
        }
        cur.execute(
            """
            INSERT INTO paper_orders (
                id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            )
            VALUES (%s, %s, %s, %s, %s, 'filled', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                status = 'filled',
                qty = EXCLUDED.qty,
                stop_loss = EXCLUDED.stop_loss,
                target = EXCLUDED.target,
                use_stop_loss = EXCLUDED.use_stop_loss,
                use_target = EXCLUDED.use_target,
                filled_at = EXCLUDED.filled_at,
                fill_price = EXCLUDED.fill_price,
                updated_at = EXCLUDED.updated_at,
                position_id = EXCLUDED.position_id,
                payload = paper_orders.payload || EXCLUDED.payload
            WHERE paper_orders.instrument_id = EXCLUDED.instrument_id
              AND paper_orders.route_fingerprint = EXCLUDED.route_fingerprint
              AND paper_orders.contract_scope_key = EXCLUDED.contract_scope_key
              AND paper_orders.status = 'pending'
            RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                      use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                      role, position_id, parent_order_id, oco_group_id, reduce_only, payload
            """,
            (
                fill.order_id,
                fill.symbol,
                fill.timeframe,
                transition.side,
                str(fill.order.get("order_type") or "market"),
                transition.qty,
                fill.entry_price,
                fill.stop_loss,
                fill.target,
                fill.command["use_stop_loss"],
                fill.command["use_target"],
                _paper_utc_datetime(fill.order.get("created_at"), fill.filled_at),
                fill.filled_at,
                fill.filled_at,
                fill.price,
                fill.role,
                transition.position_id,
                str(fill.order.get("parent_order_id") or "") or None,
                str(fill.order.get("oco_group_id") or "") or None,
                fill.reduce_only,
                Jsonb(committed_payload),
            ),
        )
        saved_order_row = cur.fetchone()
        if saved_order_row is None:
            raise ValueError("PAPER_ORDER_ID_ROUTE_COLLISION")
        saved_order = _paper_order_from_row(saved_order_row)
        cur.execute(
            """
            INSERT INTO paper_fills (
                order_id, position_id, symbol, timeframe, side, qty, price, role,
                reduce_only, pnl_points, filled_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, order_id, position_id, symbol, timeframe, side, qty, price, role,
                      reduce_only, pnl_points, filled_at, payload
            """,
            (
                fill.order_id,
                transition.position_id,
                fill.symbol,
                fill.timeframe,
                transition.side,
                transition.qty,
                fill.price,
                fill.role,
                fill.reduce_only,
                transition.pnl if transition.pnl else None,
                fill.filled_at,
                Jsonb(committed_payload),
            ),
        )
        return saved_order, _paper_fill_from_row(cur.fetchone())

    def _paper_fill_position_results(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        transition: _PaperPositionTransition,
    ) -> tuple[dict[str, Any], float, dict[str, Any] | None]:
        current_position = self._paper_position_by_id(
            cur,
            fill,
            transition.position_id,
        )
        if current_position is None:
            raise ValueError("PAPER_POSITION_RESULT_MISSING")
        current_qty = float_or_none(current_position.get("qty"))
        if current_qty is None:
            raise ValueError("stored paper position contains non-finite quantity")
        closed_position = transition.closed_position
        if closed_position is not None:
            closed_position_id = require_exact_identity_text(
                closed_position.get("id"), field="PAPER_CLOSED_POSITION_ID"
            )
            closed_position = self._paper_position_by_id(cur, fill, closed_position_id)
            if closed_position is None:
                raise ValueError("PAPER_CLOSED_POSITION_RESULT_MISSING")
        return current_position, current_qty, closed_position

    def _sync_paper_fill_protective_orders(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        transition: _PaperPositionTransition,
        current_position: dict[str, Any],
        current_qty: float,
        closed_position: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        cancelled: list[dict[str, Any]] = []
        if fill.reduce_only and fill.order.get("oco_group_id"):
            cur.execute(
                """
                UPDATE paper_orders
                SET status = 'cancelled', updated_at = %s
                WHERE oco_group_id = %s
                  AND id <> %s
                  AND instrument_id = %s
                  AND route_fingerprint = %s
                  AND contract_scope_key = %s
                  AND status = 'pending'
                RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                          use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                          role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                """,
                (
                    fill.filled_at,
                    str(fill.order.get("oco_group_id")),
                    fill.order_id,
                    fill.instrument_id,
                    fill.route_fingerprint,
                    fill.contract_scope_key,
                ),
            )
            cancelled.extend(_paper_order_from_row(row) for row in cur.fetchall())
        if closed_position is not None:
            cancelled.extend(
                self._sync_paper_protective_orders(
                    cur,
                    require_exact_identity_text(
                        closed_position.get("id"), field="PAPER_CLOSED_POSITION_ID"
                    ),
                    0.0,
                    "closed",
                    instrument_id=fill.instrument_id,
                    route_fingerprint=fill.route_fingerprint,
                    contract_scope_key=fill.contract_scope_key,
                    updated_at=fill.filled_at,
                )
            )
        cancelled.extend(
            self._sync_paper_protective_orders(
                cur,
                transition.position_id,
                current_qty,
                str(current_position.get("status") or "open"),
                instrument_id=fill.instrument_id,
                route_fingerprint=fill.route_fingerprint,
                contract_scope_key=fill.contract_scope_key,
                updated_at=fill.filled_at,
            )
        )
        return cancelled

    def _create_paper_fill_protective_orders(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        transition: _PaperPositionTransition,
        current_position: dict[str, Any],
        current_qty: float,
        closed_position: dict[str, Any] | None,
        Jsonb: Any,
    ) -> list[dict[str, Any]]:
        protective_qty = (
            _paper_entry_protective_qty(
                signed_delta=transition.signed_delta,
                current_qty=current_qty,
                previous_qty=transition.previous_qty,
                reversed_position=closed_position is not None,
            )
            if (
                str(current_position.get("status")) == "open"
                and not fill.reduce_only
                and fill.role == "entry"
            )
            else None
        )
        if protective_qty is None:
            return []
        exit_side = "short" if current_qty > 0 else "long"
        oco_group = str(fill.order.get("oco_group_id") or f"oco-{fill.order_id}")
        child_specs: list[tuple[str, str, float]] = []
        if fill.stop_loss is not None:
            child_specs.append(("stop", "stop", fill.stop_loss))
        if fill.target is not None:
            child_specs.append(("take", "limit", fill.target))
        created: list[dict[str, Any]] = []
        for child_role, child_type, child_entry in child_specs:
            child_intent = self._paper_protective_child_intent(
                fill,
                transition,
                child_role=child_role,
                child_type=child_type,
                child_entry=child_entry,
                qty=protective_qty,
                exit_side=exit_side,
                oco_group=oco_group,
            )
            child_command = _paper_order_command_payload(child_intent)
            stored_payload = {
                **child_intent,
                PAPER_CONTRACT_KEY: fill.paper_contract,
                "create_command": child_command,
                "execution_command": child_command,
            }
            cur.execute(
                """
                INSERT INTO paper_orders (
                    id, symbol, timeframe, side, order_type, status, qty, entry,
                    use_stop_loss, use_target, created_at, updated_at,
                    role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                )
                VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, false, false, %s, %s, %s, %s, %s, %s, true, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                          use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                          role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                """,
                (
                    child_intent["id"],
                    fill.symbol,
                    fill.timeframe,
                    exit_side,
                    child_type,
                    protective_qty,
                    child_entry,
                    fill.filled_at,
                    fill.filled_at,
                    child_role,
                    transition.position_id,
                    fill.order_id,
                    oco_group,
                    Jsonb(stored_payload),
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("PAPER_PROTECTIVE_ORDER_ID_COLLISION")
            created.append(_paper_order_from_row(row))
        return created

    def _paper_protective_child_intent(
        self,
        fill: _PaperFillCommand,
        transition: _PaperPositionTransition,
        *,
        child_role: str,
        child_type: str,
        child_entry: float,
        qty: float,
        exit_side: str,
        oco_group: str,
    ) -> dict[str, Any]:
        indicator_source = str(fill.order_payload.get("indicator_source") or "").strip()
        execution_config = fill.order_payload.get(PAPER_EXECUTION_CONFIG_KEY)
        if not isinstance(execution_config, dict):
            raise ValueError("PAPER_EXECUTION_CONFIG_REQUIRED")
        metadata = {
            "source": (
                "indicator_protective_order" if indicator_source else "manual_protective_order"
            ),
            "instrument_id": fill.instrument_id,
            "route_fingerprint": fill.route_fingerprint,
            "provider": fill.provider,
            "provider_contract_id": fill.provider_contract_id,
            PAPER_CONTRACT_KEY: fill.paper_contract,
            "role": child_role,
            "indicator_source": fill.order_payload.get("indicator_source")
            or indicator_source
            or None,
            "setup_source": fill.order_payload.get("setup_source") or None,
            "entry_channel": fill.order_payload.get("entry_channel") or None,
            "signal_source": fill.order_payload.get("signal_source") or indicator_source or None,
            "signal_source_label": fill.order_payload.get("signal_source_label") or None,
            "signal": (
                fill.order_payload.get("signal")
                if isinstance(fill.order_payload.get("signal"), dict)
                else None
            ),
            "confluence_sources": fill.order_payload.get("confluence_sources"),
            "setup": fill.order_payload.get("setup"),
            "code": fill.order_payload.get("code"),
            "reason": fill.order_payload.get("reason"),
            "score": fill.order_payload.get("score"),
            "action_card": fill.order_payload.get("action_card"),
            PAPER_EXECUTION_CONFIG_KEY: dict(execution_config),
            PAPER_PROTECTION_BASIS_KEY: "absolute_structure",
            "parent_entry_order_id": fill.order_id,
        }
        return {
            "id": f"{child_role}-{fill.order_id}",
            "symbol": fill.symbol,
            "instrument_id": fill.instrument_id,
            "route_fingerprint": fill.route_fingerprint,
            "provider": fill.provider,
            "provider_contract_id": fill.provider_contract_id,
            PAPER_CONTRACT_KEY: fill.paper_contract,
            "timeframe": fill.timeframe,
            "side": exit_side,
            "order_type": child_type,
            "status": "pending",
            "qty": qty,
            "entry": child_entry,
            "stop_loss": None,
            "target": None,
            "use_stop_loss": False,
            "use_target": False,
            "created_at": fill.filled_at.isoformat(),
            "updated_at": fill.filled_at.isoformat(),
            "role": child_role,
            "position_id": transition.position_id,
            "parent_order_id": fill.order_id,
            "oco_group_id": oco_group,
            "reduce_only": True,
            "source": metadata["source"],
            "payload": metadata,
        }

    def _append_paper_fill_events(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        saved_order: dict[str, Any],
        current_position: dict[str, Any],
        paper_fill: dict[str, Any],
        closed_position: dict[str, Any] | None,
    ) -> None:
        event_payload = {
            **dict(saved_order),
            "position_id": current_position.get("id"),
            "position": current_position,
            "fill": paper_fill,
        }
        if closed_position is not None:
            _append_paper_trade_event_with_cursor(
                cur,
                "position_closed",
                order_id=fill.order_id,
                trade_id=closed_position.get("id"),
                symbol=fill.symbol,
                timeframe=fill.timeframe,
                payload={
                    **event_payload,
                    "position_id": closed_position.get("id"),
                    "position": closed_position,
                },
            )
        event_type = (
            "position_closed"
            if str(current_position.get("status") or "") == "closed"
            else "position_filled"
        )
        _append_paper_trade_event_with_cursor(
            cur,
            event_type,
            order_id=fill.order_id,
            trade_id=current_position.get("id"),
            symbol=fill.symbol,
            timeframe=fill.timeframe,
            payload=event_payload,
        )

    def _apply_paper_fill_with_cursor(
        self,
        cur: Any,
        fill: _PaperFillCommand,
        Jsonb: Any,
    ) -> PaperFillResult:
        replay = self._reserve_paper_fill_order(cur, fill, Jsonb)
        if replay is not None:
            return replay
        position = self._locked_paper_fill_position(cur, fill)
        early_outcome = self._paper_position_expectation_outcome(cur, fill, position)
        if early_outcome is not None:
            return early_outcome
        early_outcome = self._paper_stale_reduce_only_outcome(cur, fill, position)
        if early_outcome is not None:
            return early_outcome
        transition = self._transition_paper_position(cur, fill, position, Jsonb)
        saved_order, paper_fill = self._persist_paper_fill_records(
            cur,
            fill,
            transition,
            Jsonb,
        )
        current_position, current_qty, closed_position = self._paper_fill_position_results(
            cur,
            fill,
            transition,
        )
        cancelled_orders = self._sync_paper_fill_protective_orders(
            cur,
            fill,
            transition,
            current_position,
            current_qty,
            closed_position,
        )
        created_orders = self._create_paper_fill_protective_orders(
            cur,
            fill,
            transition,
            current_position,
            current_qty,
            closed_position,
            Jsonb,
        )
        self._append_paper_fill_events(
            cur,
            fill,
            saved_order,
            current_position,
            paper_fill,
            closed_position,
        )
        return {
            "outcome": "filled",
            "order": saved_order,
            "position": current_position,
            "fill": paper_fill,
            "created_orders": created_orders,
            "cancelled_orders": cancelled_orders,
            "closed_position": closed_position,
            "opened_new_position": transition.opened_new_position,
        }

    def apply_filled_paper_order(self, order: dict[str, Any]) -> PaperFillResult:
        from psycopg.types.json import Jsonb

        fill = _paper_fill_command(order)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction():
                cur = conn.cursor()
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (
                        f"paper-fill:{fill.instrument_id}:{fill.route_fingerprint}",
                        f"{fill.contract_scope_key}:{fill.timeframe}",
                    ),
                )
                result = self._apply_paper_fill_with_cursor(cur, fill, Jsonb)
        if result["outcome"] != "no_op" or result["cancelled_orders"]:
            paper_journal_runtime.mark_committed()
        return result

    def read_paper_positions(
        self,
        limit: int = 100,
        status: str | None = None,
        *,
        instrument_id: str | None = None,
        route_fingerprint: str | None = None,
        timeframe: str | None = None,
        complete_position_ids: tuple[str, ...] | None = None,
        complete_open: bool = False,
        paper_contract: PaperContractIdentity | None = None,
        contract_scope_kind: PaperContractScopeKind | None = None,
    ) -> list[dict[str, Any]]:
        query = _paper_position_read_query(
            limit=limit,
            status=status,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=timeframe,
            complete_position_ids=complete_position_ids,
            complete_open=complete_open,
            paper_contract=paper_contract,
            contract_scope_kind=contract_scope_kind,
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, symbol, timeframe, status, qty, avg_entry, opened_at, updated_at,
                           closed_at, realized_pnl, payload
                    FROM paper_positions
                    {query.where}
                    ORDER BY updated_at DESC
                    {query.limit_clause}
                    """,
                    query.params,
                )
                rows = cur.fetchall()
        positions = [_paper_position_from_row(row) for row in rows]
        if paper_contract is not None:
            for position in positions:
                _require_paper_contract_match(
                    position,
                    paper_contract,
                    entity="POSITION",
                )
        return positions

    def read_paper_fills(
        self,
        position_id: str | None = None,
        limit: int = 200,
        *,
        instrument_id: str,
        route_fingerprint: str,
        since: datetime | None = None,
        paper_contract: PaperContractIdentity | None = None,
        contract_scope_kind: PaperContractScopeKind | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["instrument_id = %s", "route_fingerprint = %s"]
        params: list[Any] = [
            require_exact_identity_text(
                instrument_id,
                field="PAPER_FILL_INSTRUMENT_ID",
            ),
            require_exact_identity_text(
                route_fingerprint,
                field="PAPER_FILL_ROUTE_FINGERPRINT",
            ),
        ]
        scope_clause, scope_params = _paper_contract_query_scope(
            paper_contract,
            contract_scope_kind,
        )
        conditions.append(scope_clause)
        params.extend(scope_params)
        if position_id:
            conditions.append("position_id = %s")
            params.append(str(position_id))
        if since is not None:
            conditions.append("filled_at >= %s")
            params.append(_paper_utc_datetime(since))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, order_id, position_id, symbol, timeframe, side, qty, price, role,
                           reduce_only, pnl_points, filled_at, payload
                    FROM paper_fills
                    {where}
                    ORDER BY filled_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
        fills = [_paper_fill_from_row(row) for row in rows]
        if paper_contract is not None:
            for fill in fills:
                _require_paper_contract_match(fill, paper_contract, entity="FILL")
        return fills

    def cancel_paper_protective_orders(
        self,
        position_id: str,
        *,
        instrument_id: str,
        route_fingerprint: str,
        paper_contract: PaperContractIdentity | None = None,
    ) -> list[dict[str, Any]]:
        position_key = require_exact_identity_text(
            position_id,
            field="PAPER_POSITION_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_POSITION_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_POSITION_ROUTE_FINGERPRINT",
        )
        scope_clause, scope_params = _paper_contract_query_scope(paper_contract)
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    f"""
                    UPDATE paper_orders
                    SET status = 'cancelled', updated_at = now()
                    WHERE position_id = %s
                      AND instrument_id = %s
                      AND route_fingerprint = %s
                      AND {scope_clause}
                      AND reduce_only = true
                      AND status = 'pending'
                    RETURNING id, symbol, timeframe, side, order_type, status, qty, entry, stop_loss, target,
                              use_stop_loss, use_target, created_at, updated_at, filled_at, fill_price,
                              role, position_id, parent_order_id, oco_group_id, reduce_only, payload
                    """,
                    (position_key, identity, fingerprint, *scope_params),
                )
                rows = cur.fetchall()
        cancelled = [_paper_order_from_row(row) for row in rows]
        if cancelled:
            paper_journal_runtime.mark_committed()
        return cancelled

    def update_paper_protective_levels(
        self,
        position_id: str,
        *,
        instrument_id: str,
        route_fingerprint: str,
        stop: float | None = None,
        target: float | None = None,
        indicator_source: str | None = None,
        parent_order_id: str | None = None,
        paper_contract: PaperContractIdentity | None = None,
    ) -> bool:
        from psycopg.types.json import Jsonb

        changed = False
        stop_value = float_or_none(stop)
        target_value = float_or_none(target)
        if (stop is not None and stop_value is None) or (
            target is not None and target_value is None
        ):
            raise ValueError("paper protective prices must be finite numbers")
        position_key = require_exact_identity_text(
            position_id,
            field="PAPER_POSITION_ID",
        )
        identity = require_exact_identity_text(
            instrument_id,
            field="PAPER_POSITION_INSTRUMENT_ID",
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="PAPER_POSITION_ROUTE_FINGERPRINT",
        )
        scope_clause, scope_params = _paper_contract_query_scope(paper_contract)
        source_key = str(indicator_source or "").strip().lower()
        parent_key = str(parent_order_id or "").strip()
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                if stop is not None:
                    clauses = [
                        "position_id = %s",
                        "instrument_id = %s",
                        "route_fingerprint = %s",
                        scope_clause,
                        "status = 'pending'",
                        "role = 'stop'",
                    ]
                    where_params: list[Any] = [
                        position_key,
                        identity,
                        fingerprint,
                        *scope_params,
                    ]
                    if parent_key:
                        clauses.append("parent_order_id = %s")
                        where_params.append(parent_key)
                    elif source_key:
                        clauses.append("payload->>'indicator_source' = %s")
                        where_params.append(source_key)
                    cur.execute(
                        f"""
                        UPDATE paper_orders
                        SET entry = %s, updated_at = now(),
                            payload = payload || %s::jsonb
                        WHERE {" AND ".join(clauses)}
                        """,
                        (stop_value, Jsonb({"trail_applied": True}), *where_params),
                    )
                    changed = changed or bool(cur.rowcount)
                if target is not None:
                    clauses = [
                        "position_id = %s",
                        "instrument_id = %s",
                        "route_fingerprint = %s",
                        scope_clause,
                        "status = 'pending'",
                        "role = 'take'",
                    ]
                    where_params = [
                        position_key,
                        identity,
                        fingerprint,
                        *scope_params,
                    ]
                    if parent_key:
                        clauses.append("parent_order_id = %s")
                        where_params.append(parent_key)
                    elif source_key:
                        clauses.append("payload->>'indicator_source' = %s")
                        where_params.append(source_key)
                    cur.execute(
                        f"""
                        UPDATE paper_orders
                        SET entry = %s, updated_at = now()
                        WHERE {" AND ".join(clauses)}
                        """,
                        (target_value, *where_params),
                    )
                    changed = changed or bool(cur.rowcount)
        if changed:
            paper_journal_runtime.mark_committed()
        return changed
