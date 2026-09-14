from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import Logger
from typing import Any

from aef_terminal.alerts.runtime_registry import AlertQuoteEvent
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.paper_contract import (
    PaperContractIdentity,
    require_paper_contract_identity,
    require_paper_position_contract_identity,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    ChartBarsGenerationScope,
    ChartBarsPublicationGuard,
    begin_chart_bars_publication,
    end_chart_bars_publication,
)
from aef_terminal.ui.paper.notify import paper_notify_fill_result
from aef_terminal.ui.runtime.quote_stream import (
    QuoteCacheRevision,
    QuoteRouteEntry,
    QuoteRouteSnapshot,
)
from aef_terminal.ui.services.server_alert_selection import (
    monitor_instrument_ids,
    require_active_price_alerts,
    server_alert_instrument_snapshot,
)
from aef_terminal.ui.services.server_alert_delivery import (
    persist_price_alert_delivery,
)


AlertCrossingScope = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class AlertCrossingBaseline:
    timeframe: str
    rearmed_at: int
    price: float


@dataclass(frozen=True)
class ServerAlertMonitorTickDeps:
    store_factory: Callable[[], Any]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    server_pending_price_alerts_snapshot: Callable[[Any], list[dict[str, Any]]]
    server_active_price_alerts_snapshot: Callable[[Any], list[dict[str, Any]]]
    active_paper_orders: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    set_server_alert_wanted: Callable[[list[str]], None]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]
    logger: Logger
    paper_order_execution_transition: Callable[..., dict[str, Any]]
    screen_execution_price_snapshot: Callable[..., dict[str, Any] | None]
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None]
    quote_for_route: Callable[..., dict[str, Any]]
    server_alert_price_snapshot: Callable[..., dict[str, Any] | None]
    server_vsa_fuel_alert_state: Callable[..., dict[str, Any] | None]
    send_server_telegram_alert: Callable[[dict[str, Any]], dict[str, Any]]
    alert_monitor_payload: Callable[[dict[str, Any], float], dict[str, Any]]
    server_dynamic_alert_state: Callable[..., dict[str, Any] | None]
    server_alert_indicator_params: Callable[[], dict[str, Any]]
    alert_freeze_active: Callable[..., bool]
    crossing_baselines: dict[AlertCrossingScope, AlertCrossingBaseline]
    server_ema_touch_direction: Callable[..., str]
    ema_touch_rearm_ms: Callable[[dict[str, Any]], int]
    alert_cross_hit: Callable[..., bool]
    server_alert_freeze_ms: Callable[[str], int]
    paper_order_price_snapshot: Callable[
        [dict[str, Any]],
        dict[str, Any] | None,
    ]
    paper_journal_active_snapshot: Callable[
        [Any],
        Awaitable[tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    ]
    set_paper_option_quote_demand: Callable[[list[dict[str, Any]]], int]
    server_active_price_alerts_for_route: Callable[[str, str], list[dict[str, Any]]]


async def _cached_server_alert_price_snapshot(
    deps: ServerAlertMonitorTickDeps,
    quote: dict[str, Any],
    price_snapshot_cache: dict[
        tuple[str, str, str],
        dict[str, Any] | None,
    ],
    *,
    scope_key: tuple[str, str, str],
) -> dict[str, Any] | None:
    if scope_key not in price_snapshot_cache:
        price_snapshot_cache[scope_key] = deps.server_alert_price_snapshot(quote)
    return price_snapshot_cache[scope_key]


@dataclass(frozen=True, slots=True)
class _QualifiedPaperOrder:
    order: dict[str, Any]
    order_id: str
    instrument_id: str
    route_fingerprint: str
    paper_contract: PaperContractIdentity
    stored_position_exit: bool
    current_route_matches: bool


def _qualified_paper_orders(
    pending_orders: list[dict[str, Any]],
    instrument_by_id: dict[str, dict[str, Any]],
) -> list[_QualifiedPaperOrder]:
    qualified: list[_QualifiedPaperOrder] = []
    for order in pending_orders:
        order_id = require_exact_identity_text(order.get("id"), field="PAPER_ORDER_ID")
        instrument_id = require_exact_identity_text(
            order.get("instrument_id"), field="PAPER_ORDER_INSTRUMENT_ID"
        )
        route_fingerprint = require_exact_identity_text(
            order.get("route_fingerprint"), field="PAPER_ORDER_ROUTE_FINGERPRINT"
        )
        paper_contract = require_paper_contract_identity(order)
        stored_position_exit = bool(order.get("reduce_only")) and bool(order.get("position_id"))
        instrument = instrument_by_id.get(instrument_id)
        route_matches = (
            instrument is not None and route_instrument(instrument).fingerprint == route_fingerprint
        )
        if stored_position_exit or route_matches:
            qualified.append(
                _QualifiedPaperOrder(
                    order=order,
                    order_id=order_id,
                    instrument_id=instrument_id,
                    route_fingerprint=route_fingerprint,
                    paper_contract=paper_contract,
                    stored_position_exit=stored_position_exit,
                    current_route_matches=route_matches,
                )
            )
    role_priority = {"stop": 0, "close": 1, "take": 2, "entry": 3}
    qualified.sort(
        key=lambda item: (
            0 if item.stored_position_exit else 1,
            item.instrument_id,
            item.route_fingerprint,
            item.paper_contract.scope_key,
            str(item.order.get("position_id") or ""),
            role_priority.get(str(item.order.get("role") or "entry"), 4),
            str(item.order.get("created_at") or ""),
            item.order_id,
        )
    )
    return qualified


async def _pending_paper_state(
    deps: ServerAlertMonitorTickDeps,
    store: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paper_orders, open_positions = await deps.paper_journal_active_snapshot(store)
    pending_orders = deps.active_paper_orders(paper_orders)
    deps.set_paper_option_quote_demand([*pending_orders, *open_positions])
    return pending_orders, open_positions


def _monitor_instrument_ids(
    active_alerts: list[dict[str, Any]],
    current_instruments: list[dict[str, Any]],
    qualified_orders: list[_QualifiedPaperOrder],
) -> list[str]:
    return sorted(
        set(monitor_instrument_ids(active_alerts, current_instruments))
        | {item.instrument_id for item in qualified_orders if item.current_route_matches}
    )


def _open_paper_position_keys(
    open_positions: list[dict[str, Any]],
    qualified_orders: list[_QualifiedPaperOrder],
) -> set[tuple[str, str, str, str]]:
    expected: set[tuple[str, str, str, str]] = set()
    for item in qualified_orders:
        position_id = item.order.get("position_id")
        if not bool(item.order.get("reduce_only")) or not position_id:
            continue
        expected.add(
            (
                item.instrument_id,
                item.route_fingerprint,
                item.paper_contract.scope_key,
                require_exact_identity_text(position_id, field="PAPER_POSITION_ID"),
            )
        )
    open_keys: set[tuple[str, str, str, str]] = set()
    for position in open_positions:
        position_id = require_exact_identity_text(position.get("id"), field="PAPER_POSITION_ID")
        instrument_id = require_exact_identity_text(
            position.get("instrument_id"), field="PAPER_POSITION_INSTRUMENT_ID"
        )
        route_fingerprint = require_exact_identity_text(
            position.get("route_fingerprint"),
            field="PAPER_POSITION_ROUTE_FINGERPRINT",
        )
        paper_contract = require_paper_position_contract_identity(position)
        key = (
            instrument_id,
            route_fingerprint,
            paper_contract.scope_key,
            position_id,
        )
        if key in expected and abs(float(position.get("qty") or 0.0)) > 1e-9:
            open_keys.add(key)
    return open_keys


async def _reject_paper_execution(
    store: Any,
    item: _QualifiedPaperOrder,
    transition: dict[str, Any],
    current_ts: datetime,
) -> None:
    await run_physical_thread_call(
        store.reject_paper_order_execution_if_pending,
        item.order_id,
        current_ts,
        instrument_id=item.instrument_id,
        route_fingerprint=item.route_fingerprint,
        paper_contract=item.paper_contract,
        code=str(transition.get("code") or ""),
        reason=str(transition.get("message") or ""),
        execution=(
            dict(transition["execution"]) if isinstance(transition.get("execution"), dict) else {}
        ),
    )


async def _apply_paper_execution(
    deps: ServerAlertMonitorTickDeps,
    store: Any,
    transition: dict[str, Any],
) -> None:
    filled_order = transition.get("order")
    if not isinstance(filled_order, dict):
        raise ValueError("PAPER_EXECUTION_TRANSITION_ORDER_REQUIRED")
    await run_physical_thread_call(
        _apply_and_notify_paper_execution,
        store.apply_filled_paper_order,
        filled_order,
        deps.queue_paper_trade_telegram,
    )


def _apply_and_notify_paper_execution(
    apply_paper_fill: Callable[..., Any],
    filled_order: dict[str, Any],
    queue_telegram: Callable[[str, dict[str, Any]], None],
) -> None:
    result = apply_paper_fill(filled_order)
    if not isinstance(result, dict):
        raise ValueError("PAPER_ALERT_FILL_RESULT_REQUIRED")
    outcome = result.get("outcome")
    if outcome in {"cancelled", "no_op"}:
        return
    if outcome != "filled":
        raise ValueError("PAPER_ALERT_FILL_OUTCOME_REQUIRED")
    saved_order = result.get("order")
    position = result.get("position")
    if not isinstance(saved_order, dict) or not isinstance(position, dict):
        raise ValueError("PAPER_ALERT_FILLED_RESULT_REQUIRED")
    role = str(saved_order.get("role") or "entry")
    close_reason = {"stop": "stop_loss", "take": "target", "close": "manual_close"}.get(
        role,
        "manual_flip" if isinstance(result.get("closed_position"), dict) else "manual_net_close",
    )
    paper_notify_fill_result(
        result,
        close_reason=close_reason,
        queue_telegram=queue_telegram,
    )


async def _process_qualified_paper_order(
    deps: ServerAlertMonitorTickDeps,
    store: Any,
    item: _QualifiedPaperOrder,
    *,
    open_position_keys: set[tuple[str, str, str, str]],
    current_ts: datetime,
) -> None:
    position_id = str(item.order.get("position_id") or "")
    position_key = (
        item.instrument_id,
        item.route_fingerprint,
        item.paper_contract.scope_key,
        position_id,
    )
    if position_id and position_key not in open_position_keys:
        if bool(item.order.get("reduce_only")):
            await run_physical_thread_call(
                store.cancel_paper_order_if_pending,
                item.order_id,
                current_ts,
                instrument_id=item.instrument_id,
                route_fingerprint=item.route_fingerprint,
                paper_contract=item.paper_contract,
            )
        return
    if not item.stored_position_exit and not item.current_route_matches:
        return
    timeframe = str(item.order.get("timeframe") or "")
    price_snapshot = (
        deps.paper_order_price_snapshot(item.order)
        if item.paper_contract.scope_kind == "option"
        else deps.screen_execution_price_snapshot(
            item.instrument_id,
            timeframe,
            item.route_fingerprint,
        )
    )
    transition = deps.paper_order_execution_transition(
        item.order,
        price_snapshot,
        filled_at=current_ts,
    )
    outcome = transition.get("outcome")
    if outcome in {"pending", "price_unavailable"}:
        return
    if outcome == "rejected":
        await _reject_paper_execution(store, item, transition, current_ts)
        return
    if outcome != "filled":
        raise ValueError("PAPER_EXECUTION_TRANSITION_OUTCOME_REQUIRED")
    await _apply_paper_execution(deps, store, transition)


async def _process_pending_paper_orders(
    deps: ServerAlertMonitorTickDeps,
    store: Any,
    pending_orders: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    qualified_orders: list[_QualifiedPaperOrder],
    current_ts: datetime,
) -> None:
    if not pending_orders:
        return
    open_keys = _open_paper_position_keys(
        open_positions,
        qualified_orders,
    )
    for item in qualified_orders:
        await _process_qualified_paper_order(
            deps,
            store,
            item,
            open_position_keys=open_keys,
            current_ts=current_ts,
        )


@dataclass(slots=True)
class _AlertTickContext:
    deps: ServerAlertMonitorTickDeps
    store: Any
    route_snapshot: QuoteRouteSnapshot
    quotes: dict[str, dict[str, Any]] | None
    indicator_params: dict[str, Any]
    now_ms: int
    current_ts: datetime
    price_snapshot_cache: dict[tuple[str, str, str], dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class _AlertEvaluation:
    alert: dict[str, Any]
    alert_id: str
    instrument_id: str
    route_fingerprint: str
    timeframe: str
    rearmed_at: int
    route_entry: QuoteRouteEntry
    current: float
    target: float
    price_snapshot: dict[str, Any]


async def _alert_evaluation(
    context: _AlertTickContext,
    alert: dict[str, Any],
) -> _AlertEvaluation | None:
    instrument_id = require_exact_identity_text(
        alert.get("instrument_id"), field="SERVER_ALERT_INSTRUMENT_ID"
    )
    route_fingerprint = require_exact_identity_text(
        alert.get("route_fingerprint"), field="SERVER_ALERT_ROUTE_FINGERPRINT"
    )
    timeframe = require_exact_identity_text(alert.get("timeframe"), field="SERVER_ALERT_TIMEFRAME")
    alert_id = require_exact_identity_text(alert.get("id"), field="SERVER_ALERT_ID")
    rearmed_at = alert.get("rearmedAt")
    if isinstance(rearmed_at, bool) or not isinstance(rearmed_at, int) or rearmed_at < 0:
        raise ValueError("SERVER_ALERT_GENERATION_INVALID")
    entry = context.route_snapshot.entries.get(instrument_id)
    if entry is None or entry.route_fingerprint != route_fingerprint:
        return None
    quote = context.deps.quote_for_route(context.quotes, route_fingerprint)
    snapshot = await _cached_server_alert_price_snapshot(
        context.deps,
        quote,
        context.price_snapshot_cache,
        scope_key=(instrument_id, route_fingerprint, timeframe),
    )
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise ValueError("SERVER_ALERT_PRICE_SNAPSHOT_INVALID")
    current_value = snapshot.get("price")
    if (
        isinstance(current_value, bool)
        or not isinstance(current_value, (int, float))
        or not math.isfinite(current_value)
    ):
        raise ValueError("SERVER_ALERT_PRICE_SNAPSHOT_INVALID")
    current = float(current_value)
    target = float(alert["price"])
    return _AlertEvaluation(
        alert=alert,
        alert_id=alert_id,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        timeframe=timeframe,
        rearmed_at=rearmed_at,
        route_entry=entry,
        current=current,
        target=target,
        price_snapshot=snapshot,
    )


async def _process_vsa_alert(
    context: _AlertTickContext,
    evaluation: _AlertEvaluation,
) -> bool:
    state = await run_physical_thread_call(
        context.deps.server_vsa_fuel_alert_state,
        context.store,
        evaluation.alert,
        evaluation.current,
        instrument=evaluation.route_entry.wire_instrument(),
        indicator_params=context.indicator_params,
    )
    if state is None:
        return False
    try:
        publication_guard = _begin_alert_state_publication(evaluation, state)
    except ChartBarsGenerationChanged:
        return True
    try:
        return await _publish_vsa_alert_state(
            context,
            evaluation,
            state,
        )
    finally:
        end_chart_bars_publication(publication_guard)


def _begin_alert_state_publication(
    evaluation: _AlertEvaluation,
    state: dict[str, Any],
) -> ChartBarsPublicationGuard:
    bar_timeframe = state.get("bar_timeframe")
    generation = state.get("bar_canonical_generation")
    if (
        not isinstance(bar_timeframe, str)
        or not bar_timeframe
        or bar_timeframe != evaluation.timeframe
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise ValueError("SERVER_ALERT_CANONICAL_BAR_SCOPE_INVALID")
    return begin_chart_bars_publication(
        (
            ChartBarsGenerationScope(
                instrument_id=evaluation.instrument_id,
                route_fingerprint=evaluation.route_fingerprint,
                interval=bar_timeframe,
                generation=generation,
            ),
        )
    )


async def _publish_vsa_alert_state(
    context: _AlertTickContext,
    evaluation: _AlertEvaluation,
    state: dict[str, Any],
) -> bool:
    event_ts = state.get("event_ts")
    if not isinstance(event_ts, str) or (event_ts and parse_aware_utc_ts(event_ts) is None):
        raise ValueError("SERVER_ALERT_EVENT_TS_INVALID")
    level_value = state.get("level")
    if (
        isinstance(level_value, bool)
        or not isinstance(level_value, (int, float))
        or not math.isfinite(level_value)
    ):
        raise ValueError("SERVER_ALERT_LEVEL_INVALID")
    touching = state.get("touching")
    if not isinstance(touching, bool):
        raise ValueError("SERVER_ALERT_TOUCH_STATE_INVALID")
    level = float(level_value)
    state_patch: dict[str, Any] = {}
    if event_ts:
        state_patch["lastEventTs"] = event_ts
    if not touching or not event_ts:
        await _persist_alert_evaluation_state(context, evaluation, state_patch)
        return True
    payload = context.deps.alert_monitor_payload(
        {
            **evaluation.alert,
            "price": level,
            "kind": evaluation.alert["kind"],
        },
        evaluation.current,
        trigger_event_at=context.now_ms,
    )
    patch = {
        "armed": True,
        "fired": True,
        "cooldownUntil": context.now_ms + 30000,
        "lastFiredAt": context.now_ms,
        "lastFiredLevel": level,
        "lastEventTs": event_ts,
        "lastFiredEventTs": event_ts,
    }
    await persist_price_alert_delivery(
        context.store,
        evaluation.alert,
        payload,
        {**state_patch, **patch},
        now_ms=context.now_ms,
    )
    return True


async def _persist_alert_evaluation_state(
    context: _AlertTickContext,
    evaluation: _AlertEvaluation,
    state_patch: dict[str, Any],
) -> bool:
    if all(evaluation.alert.get(field) == value for field, value in state_patch.items()):
        return True
    persisted = await run_physical_thread_call(
        context.store.update_price_alert_payload_exact,
        evaluation.alert_id,
        evaluation.instrument_id,
        evaluation.route_fingerprint,
        state_patch,
        expected_rearmed_at=evaluation.alert.get("rearmedAt"),
        check_generation=True,
        require_active=True,
    )
    return isinstance(persisted, dict)


async def _process_dynamic_alert(
    context: _AlertTickContext,
    evaluation: _AlertEvaluation,
) -> bool:
    state = await run_physical_thread_call(
        context.deps.server_dynamic_alert_state,
        context.store,
        evaluation.alert,
        evaluation.current,
        instrument=evaluation.route_entry.wire_instrument(),
        indicator_params=context.indicator_params,
    )
    if state is None:
        return False
    try:
        publication_guard = _begin_alert_state_publication(evaluation, state)
    except ChartBarsGenerationChanged:
        return True
    try:
        return await _publish_dynamic_alert_state(
            context,
            evaluation,
            state,
        )
    finally:
        end_chart_bars_publication(publication_guard)


async def _publish_dynamic_alert_state(
    context: _AlertTickContext,
    evaluation: _AlertEvaluation,
    state: dict[str, Any],
) -> bool:
    numeric_fields = ("level", "tolerance")
    if any(
        isinstance(state.get(field), bool)
        or not isinstance(state.get(field), (int, float))
        or not math.isfinite(state[field])
        for field in numeric_fields
    ):
        raise ValueError("SERVER_ALERT_DYNAMIC_STATE_INVALID")
    integer_fields = ("ema_length", "atr_length")
    if any(
        isinstance(state.get(field), bool)
        or not isinstance(state.get(field), int)
        or state[field] <= 0
        for field in integer_fields
    ):
        raise ValueError("SERVER_ALERT_DYNAMIC_STATE_INVALID")
    touching_value = state.get("touching")
    if not isinstance(touching_value, bool):
        raise ValueError("SERVER_ALERT_DYNAMIC_STATE_INVALID")
    target = float(state["level"])
    if context.deps.alert_freeze_active(
        evaluation.alert,
        current_price=evaluation.current,
        level=target,
        now_ms=context.now_ms,
    ):
        return True
    touching = touching_value
    previous_touching = evaluation.alert.get("wasTouching")
    touch_direction = context.deps.server_ema_touch_direction(
        evaluation.alert,
        state,
        evaluation.current,
    )
    persistent_state = {
        "price": target,
        "lastSeenLevel": target,
        "lastTolerance": float(state["tolerance"]),
        "emaLength": int(state["ema_length"]),
        "atrLength": int(state["atr_length"]),
        "wasTouching": touching,
        "touchDirection": touch_direction,
    }
    if previous_touching is None or previous_touching or not touching:
        await _persist_alert_evaluation_state(context, evaluation, persistent_state)
        return True
    payload = context.deps.alert_monitor_payload(
        {
            **evaluation.alert,
            "price": target,
            "kind": "ema233_touch",
            "emaLength": int(state["ema_length"]),
            "atrLength": int(state["atr_length"]),
        },
        evaluation.current,
        trigger_event_at=context.now_ms,
    )
    patch = {
        "armed": True,
        "fired": True,
        "cooldownUntil": context.now_ms + context.deps.ema_touch_rearm_ms(evaluation.alert),
        "lastFiredAt": context.now_ms,
        "lastFiredLevel": target,
        **persistent_state,
        "touchDirection": touch_direction,
    }
    await persist_price_alert_delivery(
        context.store,
        evaluation.alert,
        payload,
        patch,
        now_ms=context.now_ms,
    )
    return True


async def _process_cross_alert(
    context: _AlertTickContext,
    evaluation: _AlertEvaluation,
) -> None:
    key = (
        evaluation.alert_id,
        evaluation.instrument_id,
        evaluation.route_fingerprint,
    )
    baseline = context.deps.crossing_baselines.get(key)
    direction = evaluation.alert["direction"]
    if (
        baseline is None
        or baseline.timeframe != evaluation.timeframe
        or baseline.rearmed_at != evaluation.rearmed_at
    ):
        context.deps.crossing_baselines[key] = AlertCrossingBaseline(
            timeframe=evaluation.timeframe,
            rearmed_at=evaluation.rearmed_at,
            price=evaluation.current,
        )
        return
    if context.deps.alert_freeze_active(
        evaluation.alert,
        current_price=evaluation.current,
        level=evaluation.target,
        now_ms=context.now_ms,
    ):
        return
    hit = context.deps.alert_cross_hit(
        baseline.price,
        evaluation.current,
        evaluation.target,
        direction,
    )
    if not hit:
        context.deps.crossing_baselines[key] = AlertCrossingBaseline(
            timeframe=evaluation.timeframe,
            rearmed_at=evaluation.rearmed_at,
            price=evaluation.current,
        )
        return
    payload = context.deps.alert_monitor_payload(
        evaluation.alert,
        evaluation.current,
        trigger_event_at=context.now_ms,
    )
    patch = {
        "armed": False,
        "fired": True,
        "cooldownUntil": context.now_ms + context.deps.server_alert_freeze_ms(evaluation.timeframe),
        "lastFiredAt": context.now_ms,
        "lastFiredLevel": evaluation.target,
        "lastSeenPrice": evaluation.current,
    }
    persisted = await persist_price_alert_delivery(
        context.store,
        evaluation.alert,
        payload,
        patch,
        now_ms=context.now_ms,
    )
    if persisted:
        context.deps.crossing_baselines[key] = AlertCrossingBaseline(
            timeframe=evaluation.timeframe,
            rearmed_at=evaluation.rearmed_at,
            price=evaluation.current,
        )
    else:
        context.deps.crossing_baselines.pop(key, None)


def _prune_crossing_baselines(
    deps: ServerAlertMonitorTickDeps,
    alerts: list[dict[str, Any]],
) -> None:
    active_generations = {
        (
            require_exact_identity_text(alert.get("id"), field="SERVER_ALERT_ID"),
            require_exact_identity_text(
                alert.get("instrument_id"), field="SERVER_ALERT_INSTRUMENT_ID"
            ),
            require_exact_identity_text(
                alert.get("route_fingerprint"), field="SERVER_ALERT_ROUTE_FINGERPRINT"
            ),
        ): (
            require_exact_identity_text(alert.get("timeframe"), field="SERVER_ALERT_TIMEFRAME"),
            alert.get("rearmedAt"),
        )
        for alert in alerts
        if alert["kind"] == "price"
    }
    for key, baseline in tuple(deps.crossing_baselines.items()):
        if active_generations.get(key) != (baseline.timeframe, baseline.rearmed_at):
            deps.crossing_baselines.pop(key, None)


async def _process_active_alert(
    context: _AlertTickContext,
    alert: dict[str, Any],
) -> None:
    evaluation = await _alert_evaluation(context, alert)
    if evaluation is None:
        return
    kind = evaluation.alert["kind"]
    if kind == "vsa_fuel":
        await _process_vsa_alert(context, evaluation)
        return
    if kind == "ema233_touch":
        await _process_dynamic_alert(context, evaluation)
        return
    if kind == "price":
        await _process_cross_alert(context, evaluation)
        return
    raise ValueError("SERVER_ALERT_KIND_INVALID")


async def process_server_alert_monitor_tick(
    deps: ServerAlertMonitorTickDeps,
    *,
    alert_kinds: frozenset[str] | None = None,
) -> None:
    store = deps.store_factory()
    if store is None:
        deps.set_paper_option_quote_demand([])
        return
    route_snapshot = deps.quote_route_snapshot()
    current_instruments, instrument_by_id = server_alert_instrument_snapshot(
        route_snapshot.select_instruments(None)
    )
    alerts = require_active_price_alerts(deps.server_active_price_alerts_snapshot(store))
    pending_paper_orders, open_paper_positions = await _pending_paper_state(deps, store)
    qualified_pending_orders = _qualified_paper_orders(
        pending_paper_orders,
        instrument_by_id,
    )
    _prune_crossing_baselines(deps, alerts)
    evaluated_alerts = [
        alert for alert in alerts if alert_kinds is None or alert["kind"] in alert_kinds
    ]
    monitor_ids = _monitor_instrument_ids(
        alerts,
        current_instruments,
        qualified_pending_orders,
    )
    deps.set_server_alert_wanted(monitor_ids)
    monitor_instruments = [instrument_by_id[instrument_id] for instrument_id in monitor_ids]
    if monitor_instruments:
        quotes, quote_warning, _cache_revision = deps.quote_cache_for_instruments(
            monitor_instruments
        )
    else:
        quotes, quote_warning = {}, ""
    if quote_warning and monitor_ids:
        deps.logger.debug("quote orchestrator cache warning: %s", quote_warning)
    now_ms = int(time.time() * 1000)
    current_ts = datetime.now(tz=UTC)
    indicator_params = (
        deps.server_alert_indicator_params()
        if any(alert["kind"] in {"ema233_touch", "vsa_fuel"} for alert in evaluated_alerts)
        else {}
    )
    await _process_pending_paper_orders(
        deps,
        store,
        pending_paper_orders,
        open_paper_positions,
        qualified_pending_orders,
        current_ts,
    )
    if not monitor_ids:
        return
    context = _AlertTickContext(
        deps=deps,
        store=store,
        route_snapshot=route_snapshot,
        quotes=quotes,
        indicator_params=indicator_params,
        now_ms=now_ms,
        current_ts=current_ts,
        price_snapshot_cache={},
    )
    for alert in evaluated_alerts:
        await _process_active_alert(context, alert)


async def process_server_alert_quote_event(
    deps: ServerAlertMonitorTickDeps,
    event: AlertQuoteEvent,
) -> None:
    """Evaluate exact-route fixed price alerts for one ordered quote publication."""

    route_alerts = require_active_price_alerts(
        deps.server_active_price_alerts_for_route(
            event.instrument_id,
            event.route_fingerprint,
        )
    )
    for alert in route_alerts:
        if (
            alert["kind"] != "price"
            or alert["instrument_id"] != event.instrument_id
            or alert["route_fingerprint"] != event.route_fingerprint
        ):
            raise RuntimeError("SERVER_ALERT_ACTIVE_ROUTE_SNAPSHOT_MISMATCH")
    if not route_alerts:
        return
    route_snapshot = deps.quote_route_snapshot()
    entry = route_snapshot.entries.get(event.instrument_id)
    if entry is None or entry.route_fingerprint != event.route_fingerprint:
        return
    store = deps.store_factory()
    if store is None:
        return
    now_ms = int(time.time() * 1000)
    context = _AlertTickContext(
        deps=deps,
        store=store,
        route_snapshot=route_snapshot,
        quotes={event.route_fingerprint: event.wire_quote()},
        indicator_params={},
        now_ms=now_ms,
        current_ts=datetime.now(tz=UTC),
        price_snapshot_cache={},
    )
    for alert in route_alerts:
        await _process_active_alert(context, alert)


async def run_server_alert_monitor_loop(
    *,
    poll_seconds: float,
    error_backoff_seconds: float,
    server_sleeping: Callable[[], bool],
    reset_on_sleep: Callable[[], None],
    run_iteration: Callable[[], Awaitable[None]],
    on_iteration_error: Callable[[BaseException], None],
) -> None:
    while True:
        await asyncio.sleep(poll_seconds)
        if server_sleeping():
            reset_on_sleep()
            continue
        try:
            await run_iteration()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_iteration_error(exc)
            await asyncio.sleep(error_backoff_seconds)
