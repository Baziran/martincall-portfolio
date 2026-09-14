from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import BarState, Direction, DomainFact
from aef_terminal.engine.serialization import is_gap_placeholder_payload
from aef_terminal.paper_contract import (
    PAPER_EXECUTION_CONFIG_KEY,
    PAPER_PROTECTION_BASIS_KEY,
    PAPER_PROTECTION_INTENT_KEY,
    PaperJournalError,
    paper_manual_protective_levels,
    require_paper_order_protection,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.runtime.presentation import trade_plan_rejection_text
from aef_terminal.signals.direction import direction_from_any
from aef_terminal.signals.trade_plan import plan_stop_breached_at_price
from aef_terminal.ui.paper.config import paper_config
from aef_terminal.ui.paper.orders import mark_order_filled
from aef_terminal.ui.paper.signals import paper_execution_plan_rejection
from aef_terminal.ui.quote_helpers import quote_snapshot_ts

CHART_EXECUTION_SNAPSHOT_TTL_SECONDS = 30.0
QUOTE_EXECUTION_SNAPSHOT_TTL_SECONDS = 5.0
CHART_EXECUTION_SNAPSHOT_MAX_KEYS = 512
QUOTE_EXECUTION_SNAPSHOT_MAX_KEYS = 1024
_CHART_EXECUTION_SNAPSHOTS: dict[tuple[str, str, str], dict[str, Any]] = {}
_QUOTE_EXECUTION_SNAPSHOTS: dict[tuple[str, str], dict[str, Any]] = {}
_EXECUTION_LOCK = threading.Lock()
_LAST_EXECUTION_PRUNE_AT = 0.0

PaperExecutionOutcome = Literal[
    "pending",
    "filled",
    "rejected",
    "price_unavailable",
]
PaperSubmissionOutcome = Literal[
    "pending",
    "filled",
    "no_op",
    "cancelled",
    "rejected",
]


class PaperExecutionMetadata(TypedDict):
    fill_price: float | None
    source: str
    ts: str | None
    bar_slot: float | None
    planned_entry: float | None
    analysis_bar_ts: str | None
    analysis_generation: str | None


class PaperExecutionTransition(TypedDict):
    outcome: PaperExecutionOutcome
    order: dict[str, Any]
    code: str
    message: str
    execution: PaperExecutionMetadata


class PaperExecutionSubmission(TypedDict):
    outcome: PaperSubmissionOutcome
    replayed: bool
    order: dict[str, Any]
    result: dict[str, Any] | None
    code: str
    message: str
    execution: PaperExecutionMetadata


class _PaperExecutionTouch(TypedDict):
    fill_price: float
    authority_ts: datetime | None


def utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = float(value)
        if exact_finite_number_or_none(raw) is None:
            return None
        if raw > 10_000_000_000:
            raw /= 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except OSError, OverflowError, ValueError:
            return None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    return None


def finite_price(value: Any) -> float | None:
    return exact_finite_number_or_none(value)


def route_fingerprint_key(route_fingerprint: str) -> str:
    return require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )


def quote_for_route(
    quotes: dict[str, dict[str, Any]],
    route_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(quotes, dict):
        return {}
    quote = quotes.get(route_fingerprint_key(route_fingerprint))
    return quote if isinstance(quote, dict) else {}


def _evict_oldest_entries(cache: dict[Any, dict[str, Any]], max_keys: int) -> None:
    if max_keys <= 0 or len(cache) <= max_keys:
        return
    ordered = sorted(
        cache.items(),
        key=lambda item: float(item[1].get("received_monotonic") or 0.0),
    )
    for key, _ in ordered[: len(cache) - max_keys]:
        cache.pop(key, None)


def _prune_execution_snapshots_locked(now: float | None = None) -> None:
    global _LAST_EXECUTION_PRUNE_AT
    current = time.monotonic() if now is None else float(now)
    for key, snapshot in list(_CHART_EXECUTION_SNAPSHOTS.items()):
        received_monotonic = (
            finite_price(snapshot.get("received_monotonic")) if isinstance(snapshot, dict) else None
        )
        age = current - received_monotonic if received_monotonic is not None else -1.0
        if age < 0.0 or age > CHART_EXECUTION_SNAPSHOT_TTL_SECONDS:
            _CHART_EXECUTION_SNAPSHOTS.pop(key, None)
    for key, snapshot in list(_QUOTE_EXECUTION_SNAPSHOTS.items()):
        received_monotonic = (
            finite_price(snapshot.get("received_monotonic")) if isinstance(snapshot, dict) else None
        )
        age = current - received_monotonic if received_monotonic is not None else -1.0
        if age < 0.0 or age > QUOTE_EXECUTION_SNAPSHOT_TTL_SECONDS:
            _QUOTE_EXECUTION_SNAPSHOTS.pop(key, None)
    _evict_oldest_entries(_CHART_EXECUTION_SNAPSHOTS, CHART_EXECUTION_SNAPSHOT_MAX_KEYS)
    _evict_oldest_entries(_QUOTE_EXECUTION_SNAPSHOTS, QUOTE_EXECUTION_SNAPSHOT_MAX_KEYS)
    _LAST_EXECUTION_PRUNE_AT = current


def _maybe_prune_execution_snapshots_locked(now: float) -> None:
    if (
        now - _LAST_EXECUTION_PRUNE_AT >= 5.0
        or len(_CHART_EXECUTION_SNAPSHOTS) > CHART_EXECUTION_SNAPSHOT_MAX_KEYS
        or len(_QUOTE_EXECUTION_SNAPSHOTS) > QUOTE_EXECUTION_SNAPSHOT_MAX_KEYS
    ):
        _prune_execution_snapshots_locked(now)


def _chart_snapshot_source_priority(source: str) -> int:
    value = str(source or "").lower()
    if "canonical-commit" in value:
        return 4
    if "db-checkpoint" in value:
        return 3
    if "chart-live" in value:
        return 2
    return 1


def record_chart_execution_snapshot(
    instrument_id: str,
    timeframe: str,
    payloads: list[dict[str, Any]],
    source: str,
    route_fingerprint: str,
) -> None:
    identity = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    latest: dict[str, Any] | None = None
    latest_ts: datetime | None = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if (
            is_gap_placeholder_payload(payload)
            or payload.get("authoritative") is not True
            or payload.get("closed") is not True
            or str(payload.get("state") or "") != BarState.CONFIRMED.value
        ):
            continue
        payload_identity = require_exact_identity_text(
            payload.get("instrument_id", ""),
            field="instrument_id",
            allow_empty=True,
        )
        if payload_identity and payload_identity != identity:
            continue
        close = finite_price(payload.get("close"))
        if close is None:
            continue
        payload_ts = utc_datetime(payload.get("ts"))
        if payload_ts is None:
            continue
        if (
            latest is not None
            and payload_ts is not None
            and latest_ts is not None
            and payload_ts < latest_ts
        ):
            continue
        latest = payload
        latest_ts = payload_ts
    if latest is None:
        return
    close = finite_price(latest.get("close"))
    high = finite_price(latest.get("high"))
    low = finite_price(latest.get("low"))
    if high is None:
        high = close
    if low is None:
        low = close
    if close is None or high is None or low is None:
        return
    snapshot = {
        "price": close,
        "high": max(high, low, close),
        "low": min(high, low, close),
        "range_kind": "bar",
        "ts": latest_ts,
        "source": str(source or "chart-stream"),
        "source_priority": _chart_snapshot_source_priority(source),
        "received_monotonic": time.monotonic(),
    }
    bar_slot = finite_price(
        latest.get("bar_slot") if latest.get("bar_slot") is not None else latest.get("barSlot")
    )
    if bar_slot is not None:
        snapshot["bar_slot"] = bar_slot
    key = require_exact_identity_text(
        route_fingerprint,
        field="route_fingerprint",
    )
    interval = str(timeframe or "").strip()
    if not interval:
        return
    with _EXECUTION_LOCK:
        _maybe_prune_execution_snapshots_locked(snapshot["received_monotonic"])
        cache_key = (identity, key, interval)
        existing = _CHART_EXECUTION_SNAPSHOTS.get(cache_key)
        if isinstance(existing, dict):
            existing_ts = utc_datetime(existing.get("ts"))
            incoming_ts = utc_datetime(snapshot.get("ts"))
            if existing_ts is not None and (incoming_ts is None or incoming_ts < existing_ts):
                return
            if (
                incoming_ts is not None
                and existing_ts is not None
                and incoming_ts == existing_ts
                and int(snapshot.get("source_priority") or 0)
                < int(existing.get("source_priority") or 0)
            ):
                return
        _CHART_EXECUTION_SNAPSHOTS[cache_key] = dict(snapshot)
        _evict_oldest_entries(_CHART_EXECUTION_SNAPSHOTS, CHART_EXECUTION_SNAPSHOT_MAX_KEYS)


def record_quote_execution_snapshots(rows: list[dict[str, Any]]) -> None:
    snapshots: list[tuple[tuple[str, str], dict[str, Any]]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("live_quote") is not True:
            continue
        provider = str(row.get("provider") or "")
        if not provider:
            raise ValueError("QUOTE_SNAPSHOT_PROVIDER_REQUIRED")
        reference_kind = str(row.get("price_source") or "")
        price = finite_price(row.get("price"))
        if reference_kind not in {"last", "bid_ask_mid"} or price is None:
            continue
        captured_at = quote_snapshot_ts(
            {
                "provider_ts": row.get("quote_provider_ts"),
                "received_at": row.get("quote_received_at"),
                "time_basis": row.get("quote_time_basis"),
                "ts": row.get("quote_ts"),
            }
        )
        if captured_at is None:
            continue
        snapshot = {
            "price": price,
            "high": price,
            "low": price,
            "range_kind": "quote",
            "reference_kind": reference_kind,
            "ts": captured_at,
            "source": str(row.get("source") or f"{provider}:quote-live"),
            "received_monotonic": time.monotonic(),
        }
        key = route_fingerprint_key(row.get("route_fingerprint", ""))
        instrument_id = require_exact_identity_text(
            row.get("instrument_id", ""),
            field="instrument_id",
        )
        snapshots.append(((instrument_id, key), snapshot))
    if not snapshots:
        return
    with _EXECUTION_LOCK:
        _maybe_prune_execution_snapshots_locked(time.monotonic())
        for key, snapshot in snapshots:
            _QUOTE_EXECUTION_SNAPSHOTS[key] = dict(snapshot)
        _evict_oldest_entries(_QUOTE_EXECUTION_SNAPSHOTS, QUOTE_EXECUTION_SNAPSHOT_MAX_KEYS)


def chart_execution_price_snapshot(
    instrument_id: str,
    timeframe: str,
    route_fingerprint: str,
) -> dict[str, Any] | None:
    identity = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    interval = str(timeframe or "").strip()
    if not interval:
        return None
    now = time.monotonic()
    key = route_fingerprint_key(route_fingerprint)
    best: dict[str, Any] | None = None
    with _EXECUTION_LOCK:
        snapshot = _CHART_EXECUTION_SNAPSHOTS.get((identity, key, interval))
        if isinstance(snapshot, dict):
            received_monotonic = finite_price(snapshot.get("received_monotonic"))
            age = now - received_monotonic if received_monotonic is not None else -1.0
            if (
                received_monotonic is not None
                and 0.0 <= age <= CHART_EXECUTION_SNAPSHOT_TTL_SECONDS
            ):
                best = dict(snapshot)
    return best


def quote_execution_price_snapshot(
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any] | None:
    identity = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    now = time.monotonic()
    key = route_fingerprint_key(route_fingerprint)
    with _EXECUTION_LOCK:
        quote_snapshot = _QUOTE_EXECUTION_SNAPSHOTS.get((identity, key))
        if isinstance(quote_snapshot, dict):
            received_monotonic = finite_price(quote_snapshot.get("received_monotonic"))
            age = now - received_monotonic if received_monotonic is not None else -1.0
            if (
                received_monotonic is not None
                and 0.0 <= age <= QUOTE_EXECUTION_SNAPSHOT_TTL_SECONDS
            ):
                return dict(quote_snapshot)
    return None


def screen_execution_price_snapshot(
    instrument_id: str,
    timeframe: str,
    route_fingerprint: str,
) -> dict[str, Any] | None:
    return quote_execution_price_snapshot(
        instrument_id,
        route_fingerprint,
    ) or chart_execution_price_snapshot(
        instrument_id,
        timeframe,
        route_fingerprint,
    )


def clear_execution_snapshots() -> None:
    global _LAST_EXECUTION_PRUNE_AT
    with _EXECUTION_LOCK:
        _CHART_EXECUTION_SNAPSHOTS.clear()
        _QUOTE_EXECUTION_SNAPSHOTS.clear()
        _LAST_EXECUTION_PRUNE_AT = 0.0


def normalize_execution_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    price = finite_price(snapshot.get("price"))
    if price is None:
        return None
    low = finite_price(snapshot.get("low"))
    high = finite_price(snapshot.get("high"))
    ts = utc_datetime(snapshot.get("ts"))
    normalized = {
        "price": price,
        "low": low if low is not None else price,
        "high": high if high is not None else price,
        "range_kind": str(snapshot.get("range_kind") or "price"),
        "ts": ts,
        "source": str(snapshot.get("source") or "screen"),
        "bar_slot": finite_price(
            snapshot.get("bar_slot")
            if snapshot.get("bar_slot") is not None
            else snapshot.get("barSlot")
        ),
    }
    return normalized


def _paper_order_touch(
    order: dict[str, Any],
    snapshot: dict[str, Any] | None,
) -> _PaperExecutionTouch | None:
    snap = normalize_execution_snapshot(snapshot)
    if snap is None:
        return None
    entry = finite_price(order.get("entry"))
    if entry is None:
        return None
    order_type = str(order.get("order_type") or "limit").lower()
    side = str(order.get("side") or "").lower()
    if order_type == "market" and side in {"long", "short"} and snap["range_kind"] == "quote":
        return {
            "fill_price": float(snap["price"]),
            "authority_ts": snap.get("ts"),
        }
    price = float(snap["price"])
    if side == "long":
        if order_type == "limit":
            if price <= entry:
                return {"fill_price": price, "authority_ts": snap.get("ts")}
            if snap["range_kind"] == "bar" and float(snap["low"]) <= entry:
                return {"fill_price": entry, "authority_ts": snap.get("ts")}
        if order_type == "stop":
            if price >= entry:
                return {"fill_price": price, "authority_ts": snap.get("ts")}
            if snap["range_kind"] == "bar" and float(snap["high"]) >= entry:
                return {"fill_price": entry, "authority_ts": snap.get("ts")}
    if side == "short":
        if order_type == "limit":
            if price >= entry:
                return {"fill_price": price, "authority_ts": snap.get("ts")}
            if snap["range_kind"] == "bar" and float(snap["high"]) >= entry:
                return {"fill_price": entry, "authority_ts": snap.get("ts")}
        if order_type == "stop":
            if price <= entry:
                return {"fill_price": price, "authority_ts": snap.get("ts")}
            if snap["range_kind"] == "bar" and float(snap["low"]) <= entry:
                return {"fill_price": entry, "authority_ts": snap.get("ts")}
    return None


def _paper_execution_config(
    order: dict[str, Any],
) -> dict[str, Any]:
    payload = order.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("PAPER_EXECUTION_CONFIG_REQUIRED")
    stored = payload.get(PAPER_EXECUTION_CONFIG_KEY)
    if not isinstance(stored, dict):
        raise ValueError("PAPER_EXECUTION_CONFIG_REQUIRED")
    return paper_config(stored)


def _paper_order_at_fill(
    order: dict[str, Any],
    fill_price: float,
) -> tuple[dict[str, Any], str]:
    order_at_fill = dict(order)
    try:
        payload = require_paper_order_protection(order)
    except ValueError as exc:
        return order_at_fill, str(exc)
    if payload[PAPER_PROTECTION_BASIS_KEY] == "absolute_structure":
        return order_at_fill, ""
    intent = payload[PAPER_PROTECTION_INTENT_KEY]
    stop_points = exact_finite_number_or_none(intent.get("stop_points"))
    target_points = exact_finite_number_or_none(intent.get("target_points"))
    use_stop_loss = order.get("use_stop_loss") is True
    use_target = order.get("use_target") is True
    if (use_stop_loss and (stop_points is None or stop_points <= 0)) or (
        use_target and (target_points is None or target_points <= 0)
    ):
        return order_at_fill, "incomplete fill-distance protection intent"
    levels = paper_manual_protective_levels(
        {
            "entry": fill_price,
            "side": order.get("side"),
            "stop_points": stop_points,
            "target_points": target_points,
            "use_stop_loss": use_stop_loss,
            "use_target": use_target,
        }
    )
    if levels is None:
        return order_at_fill, "invalid fill-distance protection intent"
    return {
        **order_at_fill,
        "stop_loss": levels.get("stop") if use_stop_loss else None,
        "target": levels.get("target") if use_target else None,
        "use_stop_loss": use_stop_loss,
        "use_target": use_target,
    }, ""


def _paper_fill_plan_rejection(
    order: dict[str, Any],
    fill_price: float,
    *,
    config: dict[str, Any],
) -> DomainFact | None:
    use_stop_loss = order.get("use_stop_loss") is True
    use_target = order.get("use_target") is True
    if not use_stop_loss and not use_target:
        return None
    direction = direction_from_any(order.get("side"))
    if direction == Direction.FLAT:
        return DomainFact("flat_direction")
    stop = finite_price(order.get("stop_loss")) if use_stop_loss else None
    target = finite_price(order.get("target")) if use_target else None
    if use_stop_loss and stop is None:
        return DomainFact("incomplete_trade_plan")
    if use_target and target is None:
        return DomainFact("incomplete_trade_plan")
    if use_stop_loss and not use_target:
        if plan_stop_breached_at_price(direction, stop, fill_price):
            return DomainFact("stop_through_fill", {"stop": stop, "fill_price": fill_price})
        return None
    if use_target and not use_stop_loss:
        target_through_fill = (direction == Direction.LONG and fill_price >= float(target)) or (
            direction == Direction.SHORT and fill_price <= float(target)
        )
        if target_through_fill:
            return DomainFact("target_through_fill", {"target": target, "fill_price": fill_price})
        return None
    return paper_execution_plan_rejection(
        str(order.get("side") or ""),
        fill_price=fill_price,
        stop=stop,
        target=target,
        config=config,
    )


def paper_order_execution_transition(
    order: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    filled_at: datetime | None = None,
) -> PaperExecutionTransition:
    """Resolve one paper-order snapshot without committing storage state."""

    payload = order.get("payload")
    plan_payload = payload if isinstance(payload, dict) else {}
    planned_entry = finite_price(plan_payload.get("planned_entry"))
    analysis_bar_ts_raw = plan_payload.get("analysis_bar_ts")
    analysis_generation = str(plan_payload.get("analysis_generation") or "").strip() or None
    has_analysis_generation = analysis_bar_ts_raw is not None and (
        not isinstance(analysis_bar_ts_raw, str) or bool(analysis_bar_ts_raw.strip())
    )
    analysis_bar_dt = utc_datetime(analysis_bar_ts_raw) if has_analysis_generation else None
    analysis_bar_ts = (
        analysis_bar_dt.isoformat()
        if analysis_bar_dt is not None
        else str(analysis_bar_ts_raw).strip()
        if has_analysis_generation
        else None
    )
    snap = normalize_execution_snapshot(snapshot)
    order_type = str(order.get("order_type") or "limit").lower()
    if snap is None:
        execution: PaperExecutionMetadata = {
            "fill_price": None,
            "source": "",
            "ts": None,
            "bar_slot": None,
            "planned_entry": planned_entry,
            "analysis_bar_ts": analysis_bar_ts,
            "analysis_generation": analysis_generation,
        }
        if order_type == "market":
            return {
                "outcome": "price_unavailable",
                "order": dict(order),
                "code": "PAPER_EXECUTION_PRICE_UNAVAILABLE",
                "message": "screen execution price is not available",
                "execution": execution,
            }
        return {
            "outcome": "pending",
            "order": dict(order),
            "code": "",
            "message": "",
            "execution": execution,
        }
    snapshot_ts = snap.get("ts")
    execution = {
        "fill_price": None,
        "source": str(snap["source"]),
        "ts": (snapshot_ts.isoformat() if isinstance(snapshot_ts, datetime) else None),
        "bar_slot": finite_price(snap.get("bar_slot")),
        "planned_entry": planned_entry,
        "analysis_bar_ts": analysis_bar_ts,
        "analysis_generation": analysis_generation,
    }
    if has_analysis_generation:
        if analysis_bar_dt is None:
            return {
                "outcome": "rejected",
                "order": dict(order),
                "code": "PAPER_EXECUTION_PLAN_GENERATION_INVALID",
                "message": "analysis bar timestamp is invalid",
                "execution": execution,
            }
        if not isinstance(snapshot_ts, datetime):
            return {
                "outcome": "price_unavailable",
                "order": dict(order),
                "code": "PAPER_EXECUTION_SNAPSHOT_TIMESTAMP_UNAVAILABLE",
                "message": "execution snapshot timestamp is not available",
                "execution": execution,
            }
        if snapshot_ts < analysis_bar_dt:
            return {
                "outcome": "rejected",
                "order": dict(order),
                "code": "PAPER_EXECUTION_SNAPSHOT_STALE",
                "message": "execution snapshot predates the analysis bar",
                "execution": execution,
            }
    touch = _paper_order_touch(order, snap)
    if touch is None:
        if order_type == "market":
            return {
                "outcome": "price_unavailable",
                "order": dict(order),
                "code": "PAPER_EXECUTION_PRICE_UNAVAILABLE",
                "message": "current paper reference price is not available",
                "execution": execution,
            }
        return {
            "outcome": "pending",
            "order": dict(order),
            "code": "",
            "message": "",
            "execution": execution,
        }
    fill_price = touch["fill_price"]
    execution["fill_price"] = fill_price
    authority_ts = touch["authority_ts"]
    if authority_ts is not None:
        execution["ts"] = authority_ts.isoformat()
    if has_analysis_generation and (
        authority_ts is None or analysis_bar_dt is None or authority_ts < analysis_bar_dt
    ):
        return {
            "outcome": "rejected",
            "order": dict(order),
            "code": "PAPER_EXECUTION_SNAPSHOT_STALE",
            "message": "execution authority predates the analysis bar",
            "execution": execution,
        }
    fill_ready_order, intent_rejection = _paper_order_at_fill(
        order,
        fill_price,
    )
    execution_rejection = (
        None
        if intent_rejection
        else _paper_fill_plan_rejection(
            fill_ready_order,
            fill_price,
            config=_paper_execution_config(order),
        )
    )
    if intent_rejection or execution_rejection is not None:
        return {
            "outcome": "rejected",
            "order": dict(order),
            "code": "PAPER_EXECUTION_PLAN_REJECTED",
            "message": intent_rejection or trade_plan_rejection_text(execution_rejection),
            "execution": execution,
        }
    fill_time = filled_at or utc_datetime(snap.get("ts")) or datetime.now(tz=UTC)
    filled = mark_order_filled(
        fill_ready_order,
        filled_at=fill_time.isoformat(),
        fill_price=fill_price,
    )
    create_command = order.get("create_command")
    if not isinstance(create_command, dict):
        create_command = dict(order)
    return {
        "outcome": "filled",
        "order": {
            **filled,
            "create_command": create_command,
            "execution_source": snap["source"],
            "execution_price": fill_price,
            "execution_ts": execution["ts"],
            "execution_bar_slot": execution["bar_slot"],
        },
        "code": "",
        "message": "",
        "execution": execution,
    }


def submit_paper_order_execution(
    store: Any,
    order: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    execution_at: datetime | None = None,
    transition_resolver: Any = paper_order_execution_transition,
) -> PaperExecutionSubmission:
    """Reserve one immutable command before resolving or replaying execution."""

    reserve = store.upsert_paper_order
    apply_fill = store.apply_filled_paper_order
    requested_id = require_exact_identity_text(
        order.get("id"),
        field="PAPER_ORDER_ID",
    )
    requested_instrument_id = require_exact_identity_text(
        order.get("instrument_id"),
        field="PAPER_ORDER_INSTRUMENT_ID",
    )
    requested_route_fingerprint = require_exact_identity_text(
        order.get("route_fingerprint"),
        field="PAPER_ORDER_ROUTE_FINGERPRINT",
    )
    try:
        authoritative_order = reserve(order)
    except PaperJournalError as exc:
        if exc.code != "PAPER_ORDER_ID_COMMAND_COLLISION" or not requested_id.startswith("sig-"):
            raise
        collided_order = store.read_paper_order(
            requested_id,
            instrument_id=requested_instrument_id,
            route_fingerprint=requested_route_fingerprint,
        )
        collided_create_command = (
            collided_order.get("create_command") if isinstance(collided_order, dict) else None
        )
        requested_payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
        collided_payload = (
            collided_create_command.get("payload")
            if isinstance(collided_create_command, dict)
            and isinstance(collided_create_command.get("payload"), dict)
            else {}
        )
        stable_generation_keys = (
            "trade_id",
            "analysis_generation",
            "analysis_bar_ts",
            "planned_entry",
        )
        if (
            not isinstance(collided_order, dict)
            or not isinstance(collided_create_command, dict)
            or not requested_payload.get("trade_id")
            or not requested_payload.get("analysis_generation")
            or any(
                collided_payload.get(key) != requested_payload.get(key)
                for key in stable_generation_keys
            )
        ):
            raise
        collided_runtime_payload = collided_order.get("payload")
        if not isinstance(collided_runtime_payload, dict):
            raise ValueError("PAPER_EXECUTION_CONFIG_REQUIRED")
        resume_order = {
            **collided_create_command,
            "payload": dict(collided_runtime_payload),
            "status": "pending",
            "created_at": (collided_order.get("created_at") or datetime.now(tz=UTC).isoformat()),
            "updated_at": (
                collided_order.get("updated_at")
                or collided_order.get("created_at")
                or datetime.now(tz=UTC).isoformat()
            ),
        }
        return submit_paper_order_execution(
            store,
            resume_order,
            snapshot,
            execution_at=execution_at,
            transition_resolver=transition_resolver,
        )
    if not isinstance(authoritative_order, dict):
        raise ValueError("PAPER_ORDER_RESERVATION_RESULT_REQUIRED")
    if (
        require_exact_identity_text(
            authoritative_order.get("id"),
            field="PAPER_ORDER_RESULT_ID",
        )
        != requested_id
        or require_exact_identity_text(
            authoritative_order.get("instrument_id"),
            field="PAPER_ORDER_RESULT_INSTRUMENT_ID",
        )
        != requested_instrument_id
        or require_exact_identity_text(
            authoritative_order.get("route_fingerprint"),
            field="PAPER_ORDER_RESULT_ROUTE_FINGERPRINT",
        )
        != requested_route_fingerprint
    ):
        raise ValueError("PAPER_ORDER_RESERVATION_IDENTITY_MISMATCH")

    status = str(authoritative_order.get("status") or "").lower()
    if status == "filled":
        replay_result = apply_fill(authoritative_order)
        if (
            not isinstance(replay_result, dict)
            or replay_result.get("outcome") not in {"filled", "no_op"}
            or not isinstance(replay_result.get("order"), dict)
        ):
            raise ValueError("PAPER_ORDER_REPLAY_RESULT_INVALID")
        replay_order = replay_result["order"]
        replay_outcome: PaperSubmissionOutcome = (
            "filled" if replay_result["outcome"] == "filled" else "no_op"
        )
        return {
            "outcome": replay_outcome,
            "replayed": True,
            "order": replay_order,
            "result": replay_result,
            "code": "PAPER_ORDER_REPLAYED",
            "message": "paper order command is already filled",
            "execution": {
                "fill_price": finite_price(replay_order.get("fill_price")),
                "source": str(replay_order.get("execution_source") or ""),
                "ts": (
                    str(replay_order.get("execution_ts"))
                    if replay_order.get("execution_ts")
                    else None
                ),
                "bar_slot": finite_price(replay_order.get("execution_bar_slot")),
                "planned_entry": finite_price(
                    (
                        replay_order.get("payload")
                        if isinstance(replay_order.get("payload"), dict)
                        else {}
                    ).get("planned_entry")
                ),
                "analysis_bar_ts": (
                    str(
                        (
                            replay_order.get("payload")
                            if isinstance(replay_order.get("payload"), dict)
                            else {}
                        ).get("analysis_bar_ts")
                    )
                    if (
                        replay_order.get("payload")
                        if isinstance(replay_order.get("payload"), dict)
                        else {}
                    ).get("analysis_bar_ts")
                    else None
                ),
                "analysis_generation": (
                    str(
                        (
                            replay_order.get("payload")
                            if isinstance(replay_order.get("payload"), dict)
                            else {}
                        ).get("analysis_generation")
                    )
                    if (
                        replay_order.get("payload")
                        if isinstance(replay_order.get("payload"), dict)
                        else {}
                    ).get("analysis_generation")
                    else None
                ),
            },
        }
    if status == "cancelled":
        rejection = (
            authoritative_order.get("execution_rejection")
            if isinstance(
                authoritative_order.get("execution_rejection"),
                dict,
            )
            else {}
        )
        rejection_execution = (
            rejection.get("execution") if isinstance(rejection.get("execution"), dict) else {}
        )
        terminal_outcome: PaperSubmissionOutcome = "rejected" if rejection else "cancelled"
        return {
            "outcome": terminal_outcome,
            "replayed": True,
            "order": authoritative_order,
            "result": None,
            "code": str(rejection.get("code") or "PAPER_ORDER_ALREADY_CANCELLED"),
            "message": str(rejection.get("reason") or "paper order command is already cancelled"),
            "execution": {
                "fill_price": finite_price(rejection_execution.get("fill_price")),
                "source": str(rejection_execution.get("source") or ""),
                "ts": (
                    str(rejection_execution.get("ts")) if rejection_execution.get("ts") else None
                ),
                "bar_slot": finite_price(rejection_execution.get("bar_slot")),
                "planned_entry": finite_price(rejection_execution.get("planned_entry")),
                "analysis_bar_ts": (
                    str(rejection_execution.get("analysis_bar_ts"))
                    if rejection_execution.get("analysis_bar_ts")
                    else None
                ),
                "analysis_generation": (
                    str(rejection_execution.get("analysis_generation"))
                    if rejection_execution.get("analysis_generation")
                    else None
                ),
            },
        }
    if status != "pending":
        raise ValueError("PAPER_ORDER_RESERVATION_STATUS_INVALID")

    resolved_at = (
        execution_at
        or utc_datetime(snapshot.get("ts") if isinstance(snapshot, dict) else None)
        or datetime.now(tz=UTC)
    )
    transition = transition_resolver(
        authoritative_order,
        snapshot,
        filled_at=resolved_at,
    )
    if transition["outcome"] in {"pending", "price_unavailable"}:
        return {
            "outcome": "pending",
            "replayed": False,
            "order": authoritative_order,
            "result": None,
            "code": transition["code"],
            "message": transition["message"],
            "execution": transition["execution"],
        }
    if transition["outcome"] == "rejected":
        cancelled_order = store.reject_paper_order_execution_if_pending(
            requested_id,
            resolved_at,
            instrument_id=requested_instrument_id,
            route_fingerprint=requested_route_fingerprint,
            code=transition["code"],
            reason=transition["message"],
            execution=dict(transition["execution"]),
        )
        if cancelled_order is None:
            reconciled = reserve(order)
            if not isinstance(reconciled, dict):
                raise ValueError("PAPER_ORDER_REJECTION_RECONCILE_REQUIRED")
            reconciled_status = str(reconciled.get("status") or "").lower()
            if reconciled_status not in {"filled", "cancelled"}:
                raise ValueError("PAPER_ORDER_REJECTION_RACE_UNRESOLVED")
            return submit_paper_order_execution(
                store,
                order,
                snapshot,
                execution_at=resolved_at,
                transition_resolver=transition_resolver,
            )
        return {
            "outcome": "rejected",
            "replayed": False,
            "order": cancelled_order,
            "result": None,
            "code": transition["code"],
            "message": transition["message"],
            "execution": transition["execution"],
        }
    if transition["outcome"] != "filled":
        raise ValueError("PAPER_EXECUTION_TRANSITION_OUTCOME_REQUIRED")
    fill_result = apply_fill(transition["order"])
    if (
        not isinstance(fill_result, dict)
        or fill_result.get("outcome") not in {"filled", "cancelled", "no_op"}
        or not isinstance(fill_result.get("order"), dict)
    ):
        raise ValueError("PAPER_ORDER_FILL_RESULT_INVALID")
    fill_outcome = str(fill_result["outcome"])
    saved_order = fill_result["order"]
    fill_rejection = (
        saved_order.get("execution_rejection")
        if isinstance(saved_order.get("execution_rejection"), dict)
        else {}
    )
    committed_outcome: PaperSubmissionOutcome = (
        "filled"
        if fill_outcome == "filled"
        else "no_op"
        if fill_outcome == "no_op"
        else "rejected"
        if fill_rejection
        else "cancelled"
    )
    return {
        "outcome": committed_outcome,
        "replayed": fill_outcome == "no_op",
        "order": saved_order,
        "result": fill_result,
        "code": str(fill_rejection.get("code") or ""),
        "message": str(fill_rejection.get("reason") or ""),
        "execution": transition["execution"],
    }
