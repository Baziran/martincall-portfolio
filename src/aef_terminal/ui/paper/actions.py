from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aef_terminal.data.gex.option_target_contract import require_option_target_dte
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.paper_contract import (
    PAPER_CONTRACT_KEY,
    PAPER_PROTECTION_BASIS_KEY,
    PAPER_PROTECTION_INTENT_KEY,
    PaperContractIdentity,
    require_paper_contract_identity,
)
from aef_terminal.ui.paper.execution import (
    finite_price,
    submit_paper_order_execution,
)
from aef_terminal.ui.paper.option_market import (
    paper_option_execution_snapshot,
    record_paper_option_quote,
    require_option_paper_limit_price,
)
from aef_terminal.ui.paper.orders import (
    PAPER_EXECUTION_CONFIG_KEY,
    normalize_paper_order,
    update_paper_order,
)
from aef_terminal.ui.paper.notify import paper_notify_fill_result
from aef_terminal.ui.paper.positions import position_trade_from_storage
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.services.option_board_stream_ws import (
    resolve_option_board_paper_contract,
)


@dataclass(frozen=True)
class PaperActionDeps:
    paper_config: Callable[[], dict[str, Any]]
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None]
    paper_order_price_snapshot: Callable[[dict[str, Any]], dict[str, Any] | None]
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC)
    instrument_lookup: Callable[[str], dict[str, Any]] = lookup_runtime_instrument
    instrument_router: Callable[[dict[str, Any]], Any] = route_instrument


def _require_client_paper_command_id(
    value: Any,
    *,
    prefix: str,
    field: str,
    error_code: str,
) -> str:
    requested_id = require_exact_identity_text(value, field=field)
    try:
        canonical_id = f"{prefix}{UUID(requested_id.removeprefix(prefix))}"
    except (AttributeError, ValueError) as exc:
        raise ValueError(error_code) from exc
    if requested_id != canonical_id:
        raise ValueError(error_code)
    return requested_id


def _paper_contract_from_wire(value: Any) -> PaperContractIdentity:
    if not isinstance(value, dict):
        raise ValueError("PAPER_CLOSE_CONTRACT_REQUIRED")
    return require_paper_contract_identity(
        {
            "provider": value.get("provider"),
            "provider_contract_id": value.get("provider_contract_id"),
            PAPER_CONTRACT_KEY: value,
        }
    )


def _paper_position_route_is_current(
    deps: PaperActionDeps,
    *,
    instrument_id: str,
    route_fingerprint: str,
) -> bool:
    try:
        route = deps.instrument_router(deps.instrument_lookup(instrument_id))
        return (
            require_exact_identity_text(
                route.instrument_id,
                field="PAPER_CLOSE_CURRENT_INSTRUMENT_ID",
            )
            == instrument_id
            and require_exact_identity_text(
                route.fingerprint,
                field="PAPER_CLOSE_CURRENT_ROUTE_FINGERPRINT",
            )
            == route_fingerprint
        )
    except Exception:
        return False


def _paper_order_action_result(
    submission: dict[str, Any],
    deps: PaperActionDeps,
    *,
    requested_order_id: str,
    requested_instrument_id: str,
    requested_route_fingerprint: str,
) -> dict[str, Any]:
    if submission["outcome"] == "rejected":
        return {
            "ok": False,
            "code": submission["code"],
            "message": submission["message"],
            "retryable": False,
        }
    order = submission["order"]
    storage_outcome = submission["outcome"]
    applied = storage_outcome == "filled"
    replayed = submission["replayed"]
    filled = str(order.get("status") or "").lower() == "filled"
    cancelled = storage_outcome == "cancelled"
    opened = False
    closed = False
    flipped = False
    position = None
    fill = None
    closed_position = None
    result = submission["result"]
    if storage_outcome in {"filled", "no_op"}:
        if not isinstance(result, dict):
            raise ValueError("PAPER_ORDER_CREATE_RESULT_REQUIRED")
        position = result.get("position")
        fill = result.get("fill")
        if not isinstance(position, dict) or not isinstance(fill, dict):
            raise ValueError("PAPER_ORDER_CREATE_FILLED_RESULT_REQUIRED")
        closed_position = result.get("closed_position")
        if closed_position is not None and not isinstance(closed_position, dict):
            raise ValueError("PAPER_ORDER_CREATE_CLOSED_POSITION_RESULT_INVALID")
        position_status = str(position.get("status") or "").lower()
        if position_status not in {"open", "closed"}:
            raise ValueError("PAPER_ORDER_CREATE_POSITION_STATUS_INVALID")
        opened = applied and result.get("opened_new_position") is True and position_status == "open"
        closed = applied and position_status == "closed"
        flipped = applied and opened and isinstance(closed_position, dict)
    returned_order_id = require_exact_identity_text(
        order.get("id"),
        field="PAPER_ORDER_RESULT_ID",
    )
    returned_instrument_id = require_exact_identity_text(
        order.get("instrument_id"),
        field="PAPER_ORDER_RESULT_INSTRUMENT_ID",
    )
    returned_route_fingerprint = require_exact_identity_text(
        order.get("route_fingerprint"),
        field="PAPER_ORDER_RESULT_ROUTE_FINGERPRINT",
    )
    if (
        returned_order_id != requested_order_id
        or returned_instrument_id != requested_instrument_id
        or returned_route_fingerprint != requested_route_fingerprint
    ):
        raise ValueError("PAPER_ORDER_CREATE_RESULT_IDENTITY_MISMATCH")
    if position:
        if (
            not isinstance(position, dict)
            or require_exact_identity_text(
                position.get("instrument_id"),
                field="PAPER_POSITION_RESULT_INSTRUMENT_ID",
            )
            != requested_instrument_id
            or require_exact_identity_text(
                position.get("route_fingerprint"),
                field="PAPER_POSITION_RESULT_ROUTE_FINGERPRINT",
            )
            != requested_route_fingerprint
        ):
            raise ValueError("PAPER_ORDER_CREATE_POSITION_IDENTITY_MISMATCH")
        if fill is not None and (
            not isinstance(fill, dict)
            or require_exact_identity_text(
                fill.get("instrument_id"),
                field="PAPER_FILL_RESULT_INSTRUMENT_ID",
            )
            != requested_instrument_id
            or require_exact_identity_text(
                fill.get("route_fingerprint"),
                field="PAPER_FILL_RESULT_ROUTE_FINGERPRINT",
            )
            != requested_route_fingerprint
        ):
            raise ValueError("PAPER_ORDER_CREATE_FILL_IDENTITY_MISMATCH")
        if isinstance(closed_position, dict):
            if (
                require_exact_identity_text(
                    closed_position.get("instrument_id"),
                    field="PAPER_CLOSED_POSITION_RESULT_INSTRUMENT_ID",
                )
                != requested_instrument_id
                or require_exact_identity_text(
                    closed_position.get("route_fingerprint"),
                    field="PAPER_CLOSED_POSITION_RESULT_ROUTE_FINGERPRINT",
                )
                != requested_route_fingerprint
            ):
                raise ValueError("PAPER_ORDER_CREATE_CLOSED_POSITION_IDENTITY_MISMATCH")
        if applied:
            paper_notify_fill_result(
                result,
                close_reason="manual_flip" if flipped else "manual_net_close",
                queue_telegram=deps.queue_paper_trade_telegram,
            )
    return {
        "ok": True,
        "instrument_id": requested_instrument_id,
        "route_fingerprint": requested_route_fingerprint,
        "order": order,
        "applied": applied,
        "replayed": replayed,
        "filled": filled,
        "cancelled": cancelled,
        "opened": opened,
        "closed": closed,
        "flipped": flipped,
        "position": position,
        "fill": fill,
        "closed_position": closed_position,
    }


def create_paper_order_action(
    store: Any,
    payload: dict[str, Any],
    deps: PaperActionDeps,
) -> dict[str, Any]:
    requested_order_id = _require_client_paper_command_id(
        payload.get("id"),
        prefix="po-",
        field="PAPER_ORDER_ID",
        error_code="PAPER_ORDER_CLIENT_UUID_INVALID",
    )
    requested_instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="PAPER_ORDER_INSTRUMENT_ID",
    )
    requested_route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="PAPER_ORDER_ROUTE_FINGERPRINT",
    )
    if any(
        field in payload
        for field in ("symbol", "provider", "provider_contract_id", "provider_symbol")
    ):
        raise ValueError("PAPER_ORDER_CLIENT_PROVIDER_METADATA_FORBIDDEN")
    client_metadata = payload.get("payload")
    if isinstance(client_metadata, dict) and (
        PAPER_PROTECTION_BASIS_KEY in client_metadata
        or PAPER_PROTECTION_INTENT_KEY in client_metadata
        or "absolute_level" in client_metadata
    ):
        raise ValueError("PAPER_ORDER_CLIENT_PROTECTION_METADATA_FORBIDDEN")
    route = deps.instrument_router(deps.instrument_lookup(requested_instrument_id))
    if (
        require_exact_identity_text(
            route.instrument_id,
            field="PAPER_ORDER_RESOLVED_INSTRUMENT_ID",
        )
        != requested_instrument_id
        or require_exact_identity_text(
            route.fingerprint,
            field="PAPER_ORDER_RESOLVED_ROUTE_FINGERPRINT",
        )
        != requested_route_fingerprint
    ):
        raise ValueError("PAPER_ORDER_ROUTE_IDENTITY_MISMATCH")
    order = normalize_paper_order(
        {
            **dict(payload),
            "symbol": route.instrument_key,
            "provider": route.provider,
            "provider_contract_id": route.adapter.session_contract_id(route.instrument),
        }
    )
    execution_config = deps.paper_config()
    order["payload"] = {
        **(dict(order["payload"]) if isinstance(order.get("payload"), dict) else {}),
        PAPER_EXECUTION_CONFIG_KEY: dict(execution_config),
    }
    now = deps.now()
    submission = submit_paper_order_execution(
        store,
        order,
        deps.paper_order_price_snapshot(order),
        execution_at=now,
    )
    return _paper_order_action_result(
        submission,
        deps,
        requested_order_id=requested_order_id,
        requested_instrument_id=requested_instrument_id,
        requested_route_fingerprint=requested_route_fingerprint,
    )


def create_option_paper_order_action(
    store: Any,
    payload: dict[str, Any],
    deps: PaperActionDeps,
    *,
    option_contract_resolver: Callable[..., dict[str, Any]] = (resolve_option_board_paper_contract),
) -> dict[str, Any]:
    required_keys = {
        "id",
        "instrument_id",
        "route_fingerprint",
        "timeframe",
        "expiry_mode",
        "snapshot_revision",
        "con_id",
        "side",
        "order_type",
        "qty",
        "entry",
    }
    if set(payload) != required_keys:
        raise ValueError("PAPER_OPTION_ORDER_PAYLOAD_INVALID")
    requested_order_id = _require_client_paper_command_id(
        payload.get("id"),
        prefix="po-",
        field="PAPER_OPTION_ORDER_ID",
        error_code="PAPER_OPTION_ORDER_CLIENT_UUID_INVALID",
    )
    requested_instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="PAPER_OPTION_ORDER_INSTRUMENT_ID",
    )
    requested_route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="PAPER_OPTION_ORDER_ROUTE_FINGERPRINT",
    )
    timeframe = require_exact_identity_text(
        payload.get("timeframe"),
        field="PAPER_OPTION_ORDER_TIMEFRAME",
    )
    side = payload.get("side")
    if side not in {"long", "short"}:
        raise ValueError("PAPER_OPTION_ORDER_SIDE_INVALID")
    if payload.get("order_type") != "limit":
        raise ValueError("PAPER_OPTION_ORDER_LIMIT_REQUIRED")
    qty = payload.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 100:
        raise ValueError("PAPER_OPTION_ORDER_QTY_INVALID")
    expiry_mode = payload.get("expiry_mode")
    try:
        expiry_mode = require_option_target_dte(expiry_mode)
    except ValueError as exc:
        raise ValueError("PAPER_OPTION_ORDER_EXPIRY_MODE_INVALID") from exc
    route = deps.instrument_router(deps.instrument_lookup(requested_instrument_id))
    if (
        require_exact_identity_text(
            route.instrument_id,
            field="PAPER_OPTION_ORDER_RESOLVED_INSTRUMENT_ID",
        )
        != requested_instrument_id
        or require_exact_identity_text(
            route.fingerprint,
            field="PAPER_OPTION_ORDER_RESOLVED_ROUTE_FINGERPRINT",
        )
        != requested_route_fingerprint
    ):
        raise ValueError("PAPER_OPTION_ORDER_ROUTE_IDENTITY_MISMATCH")
    action_at = deps.now()
    resolved = option_contract_resolver(
        instrument_id=requested_instrument_id,
        route_fingerprint=requested_route_fingerprint,
        expiry_mode=expiry_mode,
        snapshot_revision=payload.get("snapshot_revision"),
        con_id=payload.get("con_id"),
        now=action_at,
    )
    contract = resolved.get("contract")
    quote = resolved.get("quote")
    if (
        not isinstance(contract, PaperContractIdentity)
        or contract.scope_kind != "option"
        or contract.provider != route.provider
        or not isinstance(quote, dict)
    ):
        raise ValueError("PAPER_OPTION_ORDER_RESOLUTION_INVALID")
    entry = require_option_paper_limit_price(
        payload.get("entry"),
        price_increments=quote.get("price_increments"),
    )
    provider_symbol = require_exact_identity_text(
        resolved.get("provider_symbol"),
        field="PAPER_OPTION_ORDER_PROVIDER_SYMBOL",
    )
    option_label = f"{provider_symbol} {contract.expiry} {contract.strike:g}{contract.right}"
    order = normalize_paper_order(
        {
            "id": requested_order_id,
            "symbol": option_label,
            "instrument_id": requested_instrument_id,
            "route_fingerprint": requested_route_fingerprint,
            "provider": contract.provider,
            "provider_contract_id": contract.provider_contract_id,
            "timeframe": timeframe,
            "side": side,
            "order_type": "limit",
            "qty": qty,
            "entry": entry,
            "role": "entry",
            "reduce_only": False,
            "source": "option_drive",
            "note": "Option Drive paper LMT",
            "use_stop_loss": False,
            "use_target": False,
            "payload": {
                PAPER_PROTECTION_BASIS_KEY: "absolute_structure",
            },
        }
    )
    order[PAPER_CONTRACT_KEY] = contract.to_payload()
    order["payload"] = {
        **dict(order["payload"]),
        PAPER_EXECUTION_CONFIG_KEY: dict(deps.paper_config()),
        "option_board_requested_revision": resolved.get("requested_revision"),
        "option_board_resolved_revision": resolved.get("resolved_revision"),
    }
    record_paper_option_quote(contract, quote)
    submission = submit_paper_order_execution(
        store,
        order,
        paper_option_execution_snapshot(
            order,
            quote,
            now=action_at,
        ),
        execution_at=action_at,
    )
    result = _paper_order_action_result(
        submission,
        deps,
        requested_order_id=requested_order_id,
        requested_instrument_id=requested_instrument_id,
        requested_route_fingerprint=requested_route_fingerprint,
    )
    if result.get("ok"):
        result[PAPER_CONTRACT_KEY] = contract.to_payload()
        result["requested_revision"] = resolved.get("requested_revision")
        result["resolved_revision"] = resolved.get("resolved_revision")
    return result


def cancel_paper_order_action(
    store: Any,
    order_id: str,
    *,
    instrument_id: str,
    route_fingerprint: str,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> dict[str, Any]:
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
    cancelled_order = store.cancel_paper_order_if_pending(
        order_key,
        now(),
        instrument_id=identity,
        route_fingerprint=fingerprint,
    )
    if cancelled_order is None:
        return {
            "ok": False,
            "code": "PAPER_ORDER_NOT_PENDING",
            "message": "pending paper order not found for the exact route",
            "retryable": False,
        }
    cancelled_order_id = require_exact_identity_text(
        cancelled_order.get("id"),
        field="PAPER_ORDER_RESULT_ID",
    )
    cancelled_instrument_id = require_exact_identity_text(
        cancelled_order.get("instrument_id"),
        field="PAPER_ORDER_RESULT_INSTRUMENT_ID",
    )
    cancelled_route_fingerprint = require_exact_identity_text(
        cancelled_order.get("route_fingerprint"),
        field="PAPER_ORDER_RESULT_ROUTE_FINGERPRINT",
    )
    if (
        cancelled_order_id != order_key
        or cancelled_instrument_id != identity
        or cancelled_route_fingerprint != fingerprint
        or cancelled_order.get("status") != "cancelled"
    ):
        raise ValueError("PAPER_ORDER_CANCEL_RESULT_IDENTITY_MISMATCH")
    return {
        "ok": True,
        "cancelled": True,
        "instrument_id": identity,
        "route_fingerprint": fingerprint,
        "order": cancelled_order,
    }


def close_paper_trade_action(
    store: Any,
    trade_id: str,
    payload: dict[str, Any],
    deps: PaperActionDeps,
    *,
    instrument_id: str,
    route_fingerprint: str,
    paper_contract: dict[str, Any],
) -> dict[str, Any]:
    command_id = _require_client_paper_command_id(
        payload.get("command_id"),
        prefix="pc-",
        field="PAPER_CLOSE_COMMAND_ID",
        error_code="PAPER_CLOSE_CLIENT_UUID_INVALID",
    )
    trade_key = require_exact_identity_text(
        trade_id,
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
    contract = _paper_contract_from_wire(paper_contract)
    if "exit_price" in payload:
        return {
            "ok": False,
            "code": "PAPER_CLOSE_CLIENT_PRICE_FORBIDDEN",
            "message": "paper close is resolved from the current server-owned paper reference",
            "retryable": False,
        }
    position_reader = store.read_paper_positions
    order_reader = store.read_paper_order
    apply_fill = store.apply_filled_paper_order
    positions = position_reader(
        status=None,
        instrument_id=identity,
        route_fingerprint=fingerprint,
        complete_position_ids=(trade_key,),
        paper_contract=contract,
    )
    if len(positions) > 1:
        raise ValueError("PAPER_POSITION_EXACT_QUERY_DUPLICATE")
    position = positions[0] if positions else None
    if position is not None:
        position_payload = (
            position.get("payload") if isinstance(position.get("payload"), dict) else {}
        )
        if (
            require_exact_identity_text(
                position.get("id"),
                field="PAPER_POSITION_RESULT_ID",
            )
            != trade_key
            or require_exact_identity_text(
                position.get("instrument_id"),
                field="PAPER_POSITION_RESULT_INSTRUMENT_ID",
            )
            != identity
            or require_exact_identity_text(
                position.get("route_fingerprint"),
                field="PAPER_POSITION_RESULT_ROUTE_FINGERPRINT",
            )
            != fingerprint
            or require_exact_identity_text(
                position_payload.get("instrument_id"),
                field="PAPER_POSITION_PAYLOAD_INSTRUMENT_ID",
            )
            != identity
            or require_exact_identity_text(
                position_payload.get("route_fingerprint"),
                field="PAPER_POSITION_PAYLOAD_ROUTE_FINGERPRINT",
            )
            != fingerprint
            or position.get(PAPER_CONTRACT_KEY) != contract.to_payload()
            or position_payload.get(PAPER_CONTRACT_KEY) != contract.to_payload()
        ):
            raise ValueError("PAPER_POSITION_QUERY_IDENTITY_MISMATCH")
        exit_reason = str(payload.get("exit_reason") or "manual_close")
        position_status = str(position.get("status") or "").lower()
        if position_status == "closed":
            existing_order = order_reader(
                command_id,
                instrument_id=identity,
                route_fingerprint=fingerprint,
                paper_contract=contract,
            )
            if isinstance(existing_order, dict):
                replay_result = apply_fill(existing_order)
                if not isinstance(replay_result, dict) or replay_result.get("outcome") != "no_op":
                    raise ValueError("PAPER_POSITION_CLOSE_REPLAY_OUTCOME_INVALID")
                replay_position = replay_result.get("position")
                if not isinstance(replay_position, dict):
                    raise ValueError("PAPER_POSITION_CLOSE_REPLAY_RESULT_REQUIRED")
                position = replay_position
            return {
                "ok": True,
                "outcome": "no_op",
                "command_id": command_id,
                "instrument_id": identity,
                "route_fingerprint": fingerprint,
                PAPER_CONTRACT_KEY: contract.to_payload(),
                "trade": position_trade_from_storage(position),
                "position": position,
            }
        if position_status != "open":
            raise ValueError("PAPER_POSITION_STATUS_INVALID")
        position_qty = finite_price(position.get("qty"))
        if position_qty is None or abs(position_qty) <= 1e-9:
            raise ValueError("PAPER_POSITION_OPEN_QUANTITY_INVALID")
        position_avg_entry = finite_price(position.get("avg_entry"))
        if position_avg_entry is None:
            raise ValueError("PAPER_POSITION_AVG_ENTRY_INVALID")
        closed_at = deps.now()
        close_order = normalize_paper_order(
            {
                "id": command_id,
                "symbol": position.get("symbol"),
                "instrument_id": identity,
                "route_fingerprint": fingerprint,
                "provider": position_payload.get("provider"),
                "provider_contract_id": position_payload.get("provider_contract_id"),
                "timeframe": position.get("timeframe"),
                "side": "short" if position_qty > 0 else "long",
                "order_type": "market",
                "qty": abs(position_qty),
                # Market close has no trigger; this is the storage projection.
                "entry": position_avg_entry,
                "role": "close",
                "position_id": position.get("id"),
                "reduce_only": True,
                "source": "manual",
                "exit_reason": exit_reason,
                "use_stop_loss": False,
                "use_target": False,
                "payload": {
                    PAPER_PROTECTION_BASIS_KEY: "absolute_structure",
                },
            },
            now_iso=closed_at.isoformat(),
        )
        close_order[PAPER_CONTRACT_KEY] = contract.to_payload()
        close_order["payload"] = {
            **dict(close_order["payload"]),
            PAPER_EXECUTION_CONFIG_KEY: dict(deps.paper_config()),
        }
        close_order["exit_reason"] = exit_reason
        current_route = _paper_position_route_is_current(
            deps,
            instrument_id=identity,
            route_fingerprint=fingerprint,
        )
        snapshot = deps.paper_order_price_snapshot(close_order)
        submission = submit_paper_order_execution(
            store,
            close_order,
            snapshot,
            execution_at=closed_at,
        )
        outcome = submission["outcome"]
        if outcome == "pending":
            code = (
                "PAPER_EXIT_PRICE_UNAVAILABLE"
                if current_route
                else "PAPER_EXIT_PRICE_UNAVAILABLE_OLD_ROUTE"
            )
            message = (
                "authoritative price is not available for close"
                if current_route
                else "authoritative price is not available for the stored position route"
            )
            return {
                "ok": True,
                "outcome": "pending",
                "pending": True,
                "retryable": True,
                "code": code,
                "message": message,
                "command_id": command_id,
                "instrument_id": identity,
                "route_fingerprint": fingerprint,
                PAPER_CONTRACT_KEY: contract.to_payload(),
                "order": submission["order"],
                "trade": position_trade_from_storage(position),
                "position": position,
            }
        if outcome in {"cancelled", "rejected"}:
            return {
                "ok": False,
                "code": str(submission.get("code") or "PAPER_CLOSE_REJECTED"),
                "message": str(
                    submission.get("message") or "paper close command was not executable"
                ),
                "retryable": False,
            }
        if outcome not in {"filled", "no_op"}:
            raise ValueError("PAPER_POSITION_CLOSE_OUTCOME_INVALID")
        result = submission.get("result")
        if not isinstance(result, dict):
            raise ValueError("PAPER_POSITION_CLOSE_RESULT_REQUIRED")
        closed_position = result.get("position")
        if not isinstance(closed_position, dict):
            raise ValueError("PAPER_POSITION_CLOSE_RESULT_REQUIRED")
        close_payload = position_trade_from_storage(closed_position)
        if (
            closed_position.get("id") != trade_key
            or closed_position.get("instrument_id") != identity
            or closed_position.get("route_fingerprint") != fingerprint
            or closed_position.get("status") != "closed"
            or closed_position.get(PAPER_CONTRACT_KEY) != contract.to_payload()
            or close_payload.get("id") != trade_key
            or close_payload.get("instrument_id") != identity
            or close_payload.get("route_fingerprint") != fingerprint
            or close_payload.get("status") != "closed"
        ):
            raise ValueError("PAPER_POSITION_CLOSE_RESULT_IDENTITY_MISMATCH")
        if outcome == "filled":
            execution = submission.get("execution")
            close_payload["exit_price"] = (
                finite_price(execution.get("fill_price")) if isinstance(execution, dict) else None
            )
            close_payload["exit_reason"] = exit_reason
            deps.queue_paper_trade_telegram("close", close_payload)
        return {
            "ok": True,
            "outcome": outcome,
            "command_id": command_id,
            "instrument_id": identity,
            "route_fingerprint": fingerprint,
            PAPER_CONTRACT_KEY: contract.to_payload(),
            "trade": close_payload,
            "position": closed_position,
        }
    return {
        "ok": False,
        "code": "PAPER_TRADE_NOT_FOUND",
        "message": "paper position not found",
        "retryable": False,
    }


def patch_paper_order_action(
    store: Any,
    order_id: str,
    payload: dict[str, Any],
    *,
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any]:
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
    current = store.read_paper_order(
        order_key,
        instrument_id=identity,
        route_fingerprint=fingerprint,
    )
    if current is None:
        return {
            "ok": False,
            "code": "PAPER_ORDER_NOT_FOUND",
            "message": "paper order not found",
            "retryable": False,
        }
    if (
        require_exact_identity_text(
            current.get("id"),
            field="PAPER_ORDER_CURRENT_ID",
        )
        != order_key
        or require_exact_identity_text(
            current.get("instrument_id"),
            field="PAPER_ORDER_CURRENT_INSTRUMENT_ID",
        )
        != identity
        or require_exact_identity_text(
            current.get("route_fingerprint"),
            field="PAPER_ORDER_CURRENT_ROUTE_FINGERPRINT",
        )
        != fingerprint
    ):
        raise ValueError("PAPER_ORDER_UPDATE_CURRENT_IDENTITY_MISMATCH")
    updated_order = update_paper_order(current, payload)
    saved_order = store.update_paper_order_if_pending(
        updated_order,
        instrument_id=identity,
        route_fingerprint=fingerprint,
    )
    if saved_order is None:
        return {
            "ok": False,
            "code": "PAPER_ORDER_NOT_PENDING",
            "message": "only pending paper orders can be changed",
            "retryable": False,
        }
    if (
        require_exact_identity_text(
            saved_order.get("id"),
            field="PAPER_ORDER_RESULT_ID",
        )
        != order_key
        or require_exact_identity_text(
            saved_order.get("instrument_id"),
            field="PAPER_ORDER_RESULT_INSTRUMENT_ID",
        )
        != identity
        or require_exact_identity_text(
            saved_order.get("route_fingerprint"),
            field="PAPER_ORDER_RESULT_ROUTE_FINGERPRINT",
        )
        != fingerprint
        or saved_order.get("status") != "pending"
    ):
        raise ValueError("PAPER_ORDER_UPDATE_RESULT_IDENTITY_MISMATCH")
    return {
        "ok": True,
        "instrument_id": identity,
        "route_fingerprint": fingerprint,
        "order": saved_order,
    }
