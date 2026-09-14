from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.paper_contract import (
    PAPER_CONTRACT_KEY,
    PAPER_EXECUTION_CONFIG_KEY,
    PAPER_PROTECTION_BASIS_KEY,
    PAPER_PROTECTION_INTENT_KEY,
    canonical_manual_paper_protection,
    paper_instrument_contract,
    paper_manual_protective_levels as paper_manual_protective_levels,
    require_paper_contract_identity,
    require_paper_order_protection,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none, float_or_none
from aef_terminal.runtime.clock import utc_now_iso as utc_now_iso


def active_paper_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [order for order in orders if str(order.get("status") or "pending").lower() == "pending"]


def paper_order_id(payload: dict[str, Any], created_at: str) -> str:
    seed = json.dumps(
        [
            require_exact_identity_text(payload.get("instrument_id"), field="instrument_id"),
            require_exact_identity_text(
                payload.get("route_fingerprint"), field="route_fingerprint"
            ),
            str(payload.get("timeframe") or ""),
            str(payload.get("side") or ""),
            str(payload.get("order_type") or ""),
            str("" if payload.get("entry") is None else payload.get("entry")),
            str(created_at),
        ],
        separators=(",", ":"),
    )
    return "po-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:14]


def normalize_paper_order(payload: dict[str, Any], *, now_iso: str | None = None) -> dict[str, Any]:
    payload = dict(payload)
    nested_payload = dict(payload["payload"]) if isinstance(payload.get("payload"), dict) else {}
    nested_payload.pop(PAPER_EXECUTION_CONFIG_KEY, None)
    levels, nested_payload = canonical_manual_paper_protection(
        payload,
        nested_payload,
    )
    if levels is not None:
        if levels.get("stop") is not None:
            payload["stop_loss"] = levels["stop"]
            payload["use_stop_loss"] = True
        if levels.get("target") is not None:
            payload["target"] = levels["target"]
            payload["use_target"] = True
    created_at = now_iso or utc_now_iso()
    symbol = str(payload.get("symbol") or "")
    instrument_id = require_exact_identity_text(payload.get("instrument_id"), field="instrument_id")
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"), field="route_fingerprint"
    )
    provider = str(payload.get("provider") or "").strip().lower()
    provider_contract_id = require_exact_identity_text(
        payload.get("provider_contract_id"), field="provider_contract_id"
    )
    timeframe = str(payload.get("timeframe") or "").strip()
    side = str(payload.get("side") or "").strip().lower()
    order_type = str(payload.get("order_type") or "limit").strip().lower()
    if order_type not in {"market", "limit", "stop"}:
        raise ValueError("order_type must be market, limit or stop")
    role = str(payload.get("role") or "entry")
    if role not in {"entry", "stop", "take", "close"}:
        raise ValueError("role must be entry, stop, take or close")
    reduce_only_raw = payload.get("reduce_only", role in {"stop", "take", "close"})
    if not isinstance(reduce_only_raw, bool):
        raise ValueError("reduce_only must be a boolean")
    reduce_only = reduce_only_raw
    position_id = (
        require_exact_identity_text(
            payload.get("position_id"),
            field="PAPER_POSITION_ID",
        )
        if payload.get("position_id") is not None
        else None
    )
    if reduce_only and position_id is None:
        raise ValueError("reduce-only paper order requires position_id")
    if role in {"stop", "take", "close"} and not reduce_only:
        raise ValueError("protective and close paper orders must be reduce-only")
    entry_raw = payload.get("entry")
    qty_raw = 1 if payload.get("qty") is None else payload.get("qty")
    use_stop_loss = payload.get("use_stop_loss", payload.get("stop_loss") is not None)
    use_target = payload.get("use_target", payload.get("target") is not None)
    if not isinstance(use_stop_loss, bool):
        raise ValueError("use_stop_loss must be a boolean")
    if not isinstance(use_target, bool):
        raise ValueError("use_target must be a boolean")
    stop_loss_raw = payload.get("stop_loss") if use_stop_loss else None
    target_raw = payload.get("target") if use_target else None
    entry = exact_finite_number_or_none(entry_raw)
    qty = exact_finite_number_or_none(qty_raw)
    stop_loss = exact_finite_number_or_none(stop_loss_raw) if stop_loss_raw is not None else None
    target = exact_finite_number_or_none(target_raw) if target_raw is not None else None
    if entry is None or (use_stop_loss and stop_loss is None) or (use_target and target is None):
        raise ValueError("entry and checked stop_loss/target must be finite numbers")
    if qty is None or qty <= 0:
        raise ValueError("qty must be a positive finite number")
    if not symbol or not timeframe:
        raise ValueError("symbol and timeframe are required")
    if not provider:
        raise ValueError(
            "instrument_id, route_fingerprint, provider and provider_contract_id are required"
        )
    if side not in {"long", "short"}:
        raise ValueError("side must be long or short")
    contract = (
        paper_instrument_contract(provider, provider_contract_id)
        if payload.get(PAPER_CONTRACT_KEY) is None
        else require_paper_contract_identity(payload)
    )
    if side == "long":
        if stop_loss is not None and not (stop_loss < entry):
            raise ValueError("long order requires stop_loss < entry")
        if target is not None and not (entry < target):
            raise ValueError("long order requires entry < target")
    if side == "short":
        if stop_loss is not None and not (entry < stop_loss):
            raise ValueError("short order requires entry < stop_loss")
        if target is not None and not (target < entry):
            raise ValueError("short order requires target < entry")
    order_id = (
        require_exact_identity_text(payload.get("id"), field="PAPER_ORDER_ID")
        if payload.get("id") is not None
        else paper_order_id(payload, created_at)
    )
    order = {
        "id": order_id,
        "symbol": symbol,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "provider": provider,
        "provider_contract_id": provider_contract_id,
        PAPER_CONTRACT_KEY: contract.to_payload(),
        "timeframe": timeframe,
        "side": side,
        "order_type": order_type,
        "status": "pending",
        "qty": qty,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "use_stop_loss": stop_loss is not None,
        "use_target": target is not None,
        "created_at": created_at,
        "updated_at": created_at,
        "source": str(payload.get("source") or "manual"),
        "note": str(payload.get("note") or "").strip()[:240],
    }
    order["payload"] = nested_payload
    order["role"] = role
    order["reduce_only"] = reduce_only
    if position_id is not None:
        order["position_id"] = position_id
    for field in ("parent_order_id", "oco_group_id"):
        if payload.get(field) is not None:
            order[field] = require_exact_identity_text(
                payload.get(field),
                field=f"PAPER_ORDER_{field.upper()}",
            )
    order["payload"] = require_paper_order_protection(order)
    return order


def update_paper_order(
    order: dict[str, Any], patch: dict[str, Any], *, now_iso: str | None = None
) -> dict[str, Any]:
    patch = dict(patch)
    explicit_basis_patch = bool(
        {"stop_loss", "target", "stop_points", "target_points"}.intersection(patch)
    )
    current_payload = dict(order["payload"]) if isinstance(order.get("payload"), dict) else {}
    current_protection_intent = current_payload.get(PAPER_PROTECTION_INTENT_KEY)
    current_execution_config = current_payload.get(PAPER_EXECUTION_CONFIG_KEY)
    if not isinstance(current_execution_config, dict):
        raise ValueError("PAPER_EXECUTION_CONFIG_REQUIRED")
    if "entry" in patch and "stop_loss" not in patch and "target" not in patch:
        try:
            old_entry = float(order.get("entry"))
            new_entry = float(patch["entry"])
            delta = new_entry - old_entry
            if order.get("use_stop_loss") is not False and order.get("stop_loss") is not None:
                patch["stop_loss"] = float(order["stop_loss"]) + delta
            if order.get("use_target") is not False and order.get("target") is not None:
                patch["target"] = float(order["target"]) + delta
        except TypeError, ValueError:
            pass
    merged = {**dict(order), **dict(patch)}
    if explicit_basis_patch:
        merged_payload = dict(current_payload)
        merged_payload.pop(PAPER_PROTECTION_BASIS_KEY, None)
        merged_payload.pop(PAPER_PROTECTION_INTENT_KEY, None)
        merged["payload"] = merged_payload
    elif isinstance(current_protection_intent, dict):
        adjusted_intent = dict(current_protection_intent)
        if patch.get("use_stop_loss") is False:
            adjusted_intent["stop_points"] = None
        if patch.get("use_target") is False:
            adjusted_intent["target_points"] = None
        merged["payload"] = {
            **current_payload,
            PAPER_PROTECTION_INTENT_KEY: adjusted_intent,
        }
    merged["id"] = require_exact_identity_text(
        order.get("id"),
        field="PAPER_ORDER_ID",
    )
    merged["created_at"] = str(order.get("created_at") or utc_now_iso())
    updated = normalize_paper_order(merged, now_iso=str(order.get("created_at") or utc_now_iso()))
    updated["id"] = require_exact_identity_text(
        order.get("id"),
        field="PAPER_ORDER_ID",
    )
    updated["status"] = str(order.get("status") or "pending")
    updated["created_at"] = str(
        order.get("created_at") or updated.get("created_at") or utc_now_iso()
    )
    updated["updated_at"] = now_iso or utc_now_iso()
    updated_payload = dict(updated["payload"]) if isinstance(updated.get("payload"), dict) else {}
    updated_payload[PAPER_EXECUTION_CONFIG_KEY] = dict(current_execution_config)
    if updated_payload:
        updated["payload"] = updated_payload
    else:
        updated.pop("payload", None)
    for key in (
        "instrument_id",
        "route_fingerprint",
        "provider",
        "provider_contract_id",
        "role",
        "position_id",
        "parent_order_id",
        "oco_group_id",
        "reduce_only",
        "source",
    ):
        if key in order:
            updated[key] = order[key]
    return updated


def mark_order_filled(
    order: dict[str, Any], *, filled_at: str | None = None, fill_price: float | None = None
) -> dict[str, Any]:
    ts = filled_at or utc_now_iso()
    raw_fill_price = fill_price if fill_price is not None else order.get("entry")
    resolved_fill_price = float_or_none(raw_fill_price)
    if resolved_fill_price is None:
        raise ValueError("fill_price must be a finite number")
    return {
        **dict(order),
        "status": "filled",
        "filled_at": ts,
        "fill_price": resolved_fill_price,
        "updated_at": ts,
    }


def read_paper_orders(
    store: Any,
    *,
    instrument_id: str,
    route_fingerprint: str,
    limit: int = 500,
    status: str | None = "pending",
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    return store.read_paper_orders(
        limit=max(1, min(int(limit), 500)),
        status=status,
        instrument_id=require_exact_identity_text(
            instrument_id,
            field="PAPER_ORDER_INSTRUMENT_ID",
        ),
        route_fingerprint=require_exact_identity_text(
            route_fingerprint,
            field="PAPER_ORDER_ROUTE_FINGERPRINT",
        ),
        since=since,
    )
