import asyncio
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from logging import Logger
from typing import Any

from aef_terminal.alerts.runtime_registry import price_alert_runtime
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.vsa_context import (
    VSA_VOLUME_CONTRACT,
    VSA_VOLUME_VERSION,
    build_vsa_volume_context_for_instrument,
)
from aef_terminal.features.chart_guides import (
    ema_touch_guide_projection,
    ema_touch_guide_state_from_projection,
)
from aef_terminal.paper_journal_runtime import paper_journal_runtime
from aef_terminal.runtime.pine import PineContext
from aef_terminal.runtime.metrics import increment_metric
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.telemetry import log_structured_error
from aef_terminal.ui.alert_runtime_rules import (
    alert_cross_hit,
    alert_freeze_active,
    alert_monitor_payload,
    server_alert_freeze_ms,
)
from aef_terminal.ui.paper.execution import (
    paper_order_execution_transition,
    quote_for_route,
    screen_execution_price_snapshot,
)
from aef_terminal.ui.paper.orders import active_paper_orders
from aef_terminal.ui.paper.option_market import (
    set_paper_option_quote_demand,
)
from aef_terminal.ui.paper.utils import parse_iso_utc
from aef_terminal.ui.paper_runtime import paper_order_price_snapshot
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision, QuoteRouteSnapshot
from aef_terminal.ui.server_alert_prices import server_alert_price_snapshot
from aef_terminal.ui.services.server_alert_delivery import process_server_alert_delivery_tick
from aef_terminal.ui.services.paper_option_quote_refresh import (
    run_paper_option_quote_refresh_runtime,
)
from aef_terminal.ui.services.recent_confirmed_bars import (
    RecentConfirmedBarContext,
    recent_confirmed_bar_context,
    recent_confirmed_bar_slots,
)
from aef_terminal.ui.services.server_alert_monitor import (
    AlertCrossingBaseline,
    AlertCrossingScope,
    ServerAlertMonitorTickDeps,
    process_server_alert_quote_event,
    process_server_alert_monitor_tick,
    run_server_alert_monitor_loop,
)

SERVER_ALERT_CROSSING_BASELINES: dict[AlertCrossingScope, AlertCrossingBaseline] = {}
SERVER_ALERT_QUOTE_SEQUENCE = 0
_ALERT_PROJECTION_CACHE_MAX_SCOPES = 128
_ALERT_PROJECTION_LOCK = threading.Lock()
_ALERT_PROJECTION_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()

_LOGGER = logging.getLogger(__name__)


def _alert_projection_key(
    kind: str,
    *,
    instrument: dict[str, Any],
    timeframe: str,
    generation: int,
    indicator_params: dict[str, Any] | None,
) -> tuple[Any, ...]:
    route = route_instrument(instrument)
    params_signature = json.dumps(
        indicator_params or {},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        kind,
        route.instrument_id,
        route.fingerprint,
        timeframe,
        generation,
        params_signature,
    )


def _cached_alert_projection(
    key: tuple[Any, ...],
    loader: Callable[[], Any | None],
) -> Any | None:
    with _ALERT_PROJECTION_LOCK:
        cached = _ALERT_PROJECTION_CACHE.get(key)
        if cached is not None:
            _ALERT_PROJECTION_CACHE.move_to_end(key)
            return cached
    loaded = loader()
    if loaded is None:
        return None
    with _ALERT_PROJECTION_LOCK:
        cached = _ALERT_PROJECTION_CACHE.get(key)
        if cached is not None:
            _ALERT_PROJECTION_CACHE.move_to_end(key)
            return cached
        _ALERT_PROJECTION_CACHE[key] = loaded
        _ALERT_PROJECTION_CACHE.move_to_end(key)
        while len(_ALERT_PROJECTION_CACHE) > _ALERT_PROJECTION_CACHE_MAX_SCOPES:
            _ALERT_PROJECTION_CACHE.popitem(last=False)
    return loaded


def _require_vsa_fuel_events(result: Any) -> tuple[dict[str, Any], ...] | None:
    if not isinstance(result, dict):
        raise ValueError("SERVER_ALERT_VSA_CONTEXT_INVALID")
    if result.get("contract") != VSA_VOLUME_CONTRACT or result.get("version") != VSA_VOLUME_VERSION:
        raise ValueError("SERVER_ALERT_VSA_CONTEXT_VERSION_INVALID")
    status = result.get("status")
    if not isinstance(status, dict):
        raise ValueError("SERVER_ALERT_VSA_STATUS_INVALID")
    state = status.get("state")
    if state not in {"ready", "degraded", "blocked", "error"}:
        raise ValueError("SERVER_ALERT_VSA_STATUS_INVALID")
    events = result.get("events")
    if not isinstance(events, list):
        raise ValueError("SERVER_ALERT_VSA_EVENTS_INVALID")

    fuel_events: list[dict[str, Any]] = []
    previous_ts: datetime | None = None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("SERVER_ALERT_VSA_EVENT_INVALID")
        event_ts = event.get("ts")
        parsed_ts = parse_iso_utc(event_ts) if isinstance(event_ts, str) else None
        if parsed_ts is None:
            raise ValueError("SERVER_ALERT_VSA_EVENT_TS_INVALID")
        if previous_ts is not None and parsed_ts <= previous_ts:
            raise ValueError("SERVER_ALERT_VSA_EVENT_ORDER_INVALID")
        previous_ts = parsed_ts
        fuel = event.get("fuel")
        terminal_climax = event.get("terminal_climax")
        if not isinstance(fuel, bool) or not isinstance(terminal_climax, bool):
            raise ValueError("SERVER_ALERT_VSA_EVENT_KIND_INVALID")
        if not isinstance(event.get("code"), str):
            raise ValueError("SERVER_ALERT_VSA_EVENT_CODE_INVALID")
        if fuel or terminal_climax:
            fuel_events.append(event)
    if state in {"blocked", "error"}:
        if events:
            raise ValueError("SERVER_ALERT_VSA_STATUS_EVENTS_MISMATCH")
        return None if state == "error" else ()
    return tuple(reversed(fuel_events))


@dataclass(frozen=True)
class ServerAlertRuntimeDeps:
    store_factory: Callable[[], Any]
    quote_route_snapshot: Callable[[], QuoteRouteSnapshot]
    set_server_alert_wanted: Callable[[list[str]], None]
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ]
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None]
    send_server_telegram_alert: Callable[[dict[str, Any]], dict[str, Any]]
    logger: Logger
    server_alert_indicator_params: Callable[[], dict[str, Any]]


_DEPS: ServerAlertRuntimeDeps | None = None


def configure_server_alert_runtime_deps(deps: ServerAlertRuntimeDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> ServerAlertRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("server alert runtime dependencies are not configured")
    return _DEPS


def ema_touch_rearm_ms(alert: dict[str, Any]) -> int:
    minutes = alert.get("rearmMinutes")
    if minutes not in {15, 30, 60}:
        raise ValueError("PRICE_ALERT_REARM_MINUTES_INVALID")
    return minutes * 60_000


def _server_alert_recent_bars(
    store: Any,
    timeframe: str,
    *,
    instrument: dict[str, Any],
    limit: int,
) -> RecentConfirmedBarContext | None:
    route = route_instrument(instrument)
    try:
        return recent_confirmed_bar_context(
            store=store,
            instrument=instrument,
            timeframe=timeframe,
            limit=limit,
        )
    except Exception as exc:
        increment_metric(
            "server_alert_context_load_total",
            context="bars",
            status="failed",
            error=exc.__class__.__name__,
        )
        log_structured_error(
            _LOGGER,
            provider=route.provider,
            symbol=route.instrument_key,
            interval=timeframe,
            range_=str(limit),
            op="read_recent_provider_bars",
            error=exc,
            event="server_alert_context_unavailable",
        )
        return None


def server_dynamic_alert_state(
    store,
    alert: dict,
    current_price: float,
    *,
    instrument: dict[str, Any],
    indicator_params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if alert.get("kind") != "ema233_touch":
        raise ValueError("SERVER_ALERT_KIND_INVALID")
    instrument_id = require_exact_identity_text(
        alert.get("instrument_id"),
        field="SERVER_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        alert.get("route_fingerprint"),
        field="SERVER_ALERT_ROUTE_FINGERPRINT",
    )
    timeframe = require_exact_identity_text(
        alert.get("timeframe"),
        field="SERVER_ALERT_TIMEFRAME",
    )
    route = route_instrument(instrument)
    if route.instrument_id != instrument_id or route.fingerprint != route_fingerprint:
        raise ValueError("SERVER_ALERT_ROUTE_MISMATCH")
    bar_context = _server_alert_recent_bars(
        store,
        timeframe,
        instrument=instrument,
        limit=260,
    )
    if bar_context is None or not bar_context.bars:
        return None
    bars = list(bar_context.bars)
    defaults_raw = (
        indicator_params.get("global_defaults")
        if isinstance(indicator_params, dict)
        and isinstance(indicator_params.get("global_defaults"), dict)
        else None
    )
    projection = _cached_alert_projection(
        _alert_projection_key(
            "ema233_touch",
            instrument=instrument,
            timeframe=timeframe,
            generation=bar_context.generation,
            indicator_params={"global_defaults": defaults_raw or {}},
        ),
        lambda: ema_touch_guide_projection(
            bars,
            instrument=instrument,
            global_defaults=defaults_raw,
        ),
    )
    if projection is None:
        return None
    state = ema_touch_guide_state_from_projection(
        projection,
        current_price=current_price,
        tolerance_atr=float(alert["toleranceAtr"]),
        tolerance_points=float(alert["tolerancePoints"]),
    )
    return {
        **state,
        "bar_timeframe": timeframe,
        "bar_canonical_generation": bar_context.generation,
    }


def server_ema_touch_direction(alert: dict, dynamic: dict[str, Any], current_price: float) -> str:
    level = float(dynamic.get("level") or 0.0)
    tolerance = float(dynamic.get("tolerance") or 0.0)
    touching = bool(dynamic.get("touching"))
    if not touching and current_price > level + tolerance:
        return "from_above"
    if not touching and current_price < level - tolerance:
        return "from_below"
    previous_direction = alert.get("touchDirection")
    if previous_direction in {"from_above", "from_below", "touch"}:
        return str(previous_direction)
    if current_price > level:
        return "from_above"
    if current_price < level:
        return "from_below"
    return "touch"


def server_vsa_fuel_alert_state(
    store,
    alert: dict,
    current_price: float,
    *,
    instrument: dict[str, Any],
    indicator_params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if alert.get("kind") != "vsa_fuel":
        raise ValueError("SERVER_ALERT_KIND_INVALID")
    instrument_id = require_exact_identity_text(
        alert.get("instrument_id"),
        field="SERVER_ALERT_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        alert.get("route_fingerprint"),
        field="SERVER_ALERT_ROUTE_FINGERPRINT",
    )
    timeframe = require_exact_identity_text(
        alert.get("timeframe"),
        field="SERVER_ALERT_TIMEFRAME",
    )
    route = route_instrument(instrument)
    if route.instrument_id != instrument_id or route.fingerprint != route_fingerprint:
        raise ValueError("SERVER_ALERT_ROUTE_MISMATCH")
    bar_context = _server_alert_recent_bars(
        store,
        timeframe,
        instrument=instrument,
        limit=320,
    )
    if bar_context is None or not bar_context.bars:
        return None
    try:

        def load_vsa_projection() -> dict[str, Any] | None:
            confirmed_bars = list(PineContext.from_bars(bar_context.bars).confirmed_bars)
            if not confirmed_bars:
                return None
            confirmed_slots = recent_confirmed_bar_slots(
                store=store,
                instrument=instrument,
                timeframe=timeframe,
                context=bar_context,
            )
            if confirmed_slots is None:
                return None
            return build_vsa_volume_context_for_instrument(
                confirmed_bars,
                instrument,
                indicator_params=indicator_params or {},
                confirmed_slots=confirmed_slots,
            )

        result = _cached_alert_projection(
            _alert_projection_key(
                "vsa_fuel",
                instrument=instrument,
                timeframe=timeframe,
                generation=bar_context.generation,
                indicator_params=indicator_params,
            ),
            load_vsa_projection,
        )
        if result is None:
            return None
    except Exception as exc:
        route = route_instrument(instrument)
        increment_metric(
            "server_alert_context_load_total",
            context="vsa",
            status="failed",
            error=exc.__class__.__name__,
        )
        log_structured_error(
            _LOGGER,
            provider=route.provider,
            symbol=route.instrument_key,
            interval=timeframe,
            range_=str(len(bar_context.bars)),
            op="build_vsa_volume_context",
            error=exc,
            event="server_alert_context_unavailable",
        )
        return None
    fuel_events = _require_vsa_fuel_events(result)
    if fuel_events is None:
        return None

    def _event_ts(event: dict[str, Any]) -> str:
        return event["ts"]

    consumed_event_ts = alert.get("lastEventTs", "")
    created_ms = alert["createdAt"]

    def _is_touchable_event(event: dict[str, Any]) -> bool:
        event_ts = _event_ts(event)
        if event_ts == consumed_event_ts:
            return False
        event_dt = parse_iso_utc(event_ts)
        if event_dt is None:
            raise ValueError("SERVER_ALERT_VSA_EVENT_TS_INVALID")
        return event_dt.timestamp() * 1000.0 > created_ms

    item = next(
        (event for event in fuel_events if _is_touchable_event(event)),
        fuel_events[0] if fuel_events else None,
    )
    if not item:
        return {
            "touching": False,
            "event_ts": "",
            "level": current_price,
            "bar_timeframe": timeframe,
            "bar_canonical_generation": bar_context.generation,
        }
    event_ts = _event_ts(item)
    default_label = "CLX★" if item.get("terminal_climax") else "FUEL"
    return {
        "touching": _is_touchable_event(item),
        "event_ts": event_ts,
        "level": current_price,
        "label": str(item.get("label") or item.get("code") or default_label),
        "bar_timeframe": timeframe,
        "bar_canonical_generation": bar_context.generation,
    }


def reset_server_alert_on_sleep() -> None:
    _deps().set_server_alert_wanted([])
    SERVER_ALERT_CROSSING_BASELINES.clear()
    set_paper_option_quote_demand([])


def on_server_alert_iteration_error(logger: Logger, exc: BaseException) -> None:
    log_structured_error(
        logger,
        provider="server_alert",
        symbol="",
        interval="",
        range_="",
        op="server_alert_monitor_tick",
        error=exc,
    )


def server_alert_monitor_tick_deps() -> ServerAlertMonitorTickDeps:
    deps = _deps()
    return ServerAlertMonitorTickDeps(
        store_factory=deps.store_factory,
        quote_route_snapshot=deps.quote_route_snapshot,
        server_pending_price_alerts_snapshot=(
            lambda _store: price_alert_runtime.pending_delivery_snapshot()
        ),
        server_active_price_alerts_snapshot=(lambda _store: price_alert_runtime.active_snapshot()),
        server_active_price_alerts_for_route=price_alert_runtime.active_route_snapshot,
        active_paper_orders=active_paper_orders,
        set_server_alert_wanted=deps.set_server_alert_wanted,
        quote_cache_for_instruments=deps.quote_cache_for_instruments,
        logger=deps.logger,
        paper_order_execution_transition=paper_order_execution_transition,
        screen_execution_price_snapshot=screen_execution_price_snapshot,
        paper_order_price_snapshot=paper_order_price_snapshot,
        paper_journal_active_snapshot=paper_journal_runtime.active_snapshot,
        set_paper_option_quote_demand=set_paper_option_quote_demand,
        queue_paper_trade_telegram=deps.queue_paper_trade_telegram,
        quote_for_route=quote_for_route,
        server_alert_price_snapshot=server_alert_price_snapshot,
        server_vsa_fuel_alert_state=server_vsa_fuel_alert_state,
        send_server_telegram_alert=deps.send_server_telegram_alert,
        alert_monitor_payload=alert_monitor_payload,
        server_dynamic_alert_state=server_dynamic_alert_state,
        alert_freeze_active=alert_freeze_active,
        crossing_baselines=SERVER_ALERT_CROSSING_BASELINES,
        server_alert_indicator_params=deps.server_alert_indicator_params,
        server_ema_touch_direction=server_ema_touch_direction,
        ema_touch_rearm_ms=ema_touch_rearm_ms,
        alert_cross_hit=alert_cross_hit,
        server_alert_freeze_ms=server_alert_freeze_ms,
    )


async def server_alert_monitor_tick() -> None:
    await process_server_alert_monitor_tick(
        server_alert_monitor_tick_deps(),
        alert_kinds=frozenset({"ema233_touch", "vsa_fuel"}),
    )


async def server_alert_delivery_tick() -> None:
    await process_server_alert_delivery_tick(server_alert_monitor_tick_deps())


async def server_price_alert_quote_loop(
    *,
    server_sleeping: Callable[[], bool],
    on_iteration_error: Callable[[BaseException], None],
) -> None:
    global SERVER_ALERT_QUOTE_SEQUENCE
    sequence = SERVER_ALERT_QUOTE_SEQUENCE
    deps = server_alert_monitor_tick_deps()
    while True:
        await price_alert_runtime.wait_for_quote_events(sequence)
        events, ceiling, overflowed = price_alert_runtime.read_quote_events(sequence)
        if overflowed:
            SERVER_ALERT_CROSSING_BASELINES.clear()
            increment_metric("price_alert_quote_event_overflow_total")
        if not events:
            sequence = ceiling
            SERVER_ALERT_QUOTE_SEQUENCE = sequence
            continue
        for event in events:
            if server_sleeping():
                reset_server_alert_on_sleep()
                sequence = event.sequence
                SERVER_ALERT_QUOTE_SEQUENCE = sequence
                continue
            try:
                await process_server_alert_quote_event(deps, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                on_iteration_error(exc)
                await asyncio.sleep(3.0)
                break
            sequence = event.sequence
            SERVER_ALERT_QUOTE_SEQUENCE = sequence


async def server_alert_delivery_loop(
    *,
    server_sleeping: Callable[[], bool],
    on_iteration_error: Callable[[BaseException], None],
) -> None:
    """Wake on a committed outbox change or the exact next retry deadline."""

    while True:
        rows, revision = price_alert_runtime.snapshot()
        pending = [row for row in rows if isinstance(row.get("telegramPendingPayload"), dict)]
        if server_sleeping():
            await asyncio.sleep(1.0)
            continue
        now_ms = int(datetime.now().timestamp() * 1000)
        retry_at = min(
            (
                int(row.get("telegramNextRetryAt") or 0)
                for row in pending
                if not isinstance(row.get("telegramNextRetryAt"), bool)
            ),
            default=None,
        )
        if retry_at is None:
            await price_alert_runtime.wait_for_revision(revision)
            continue
        if retry_at > now_ms:
            await price_alert_runtime.wait_for_revision(
                revision,
                timeout=(retry_at - now_ms) / 1000.0,
            )
            continue
        try:
            await server_alert_delivery_tick()
            _rows, next_revision = price_alert_runtime.snapshot()
            if next_revision == revision:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_iteration_error(exc)
            await asyncio.sleep(3.0)


async def server_alert_monitor_loop(
    *,
    server_sleeping: Callable[[], bool],
    logger: Logger | None = None,
) -> None:
    active_logger = logger or logging.getLogger("aef_terminal.ui.app")
    on_error = partial(on_server_alert_iteration_error, active_logger)
    store = _deps().store_factory()
    hydration_token = price_alert_runtime.begin_hydration()
    if store is None:
        price_alert_runtime.hydrate([], token=hydration_token)
    else:
        try:
            snapshot = await run_physical_thread_call(store.read_price_alert_runtime_snapshot)
            price_alert_runtime.hydrate(snapshot, token=hydration_token)
        except BaseException:
            price_alert_runtime.cancel_hydration(hydration_token)
            raise
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            server_price_alert_quote_loop(
                server_sleeping=server_sleeping,
                on_iteration_error=on_error,
            ),
            name="server-price-alert-quotes",
        )
        tasks.create_task(
            run_server_alert_monitor_loop(
                poll_seconds=0.5,
                error_backoff_seconds=3.0,
                server_sleeping=server_sleeping,
                reset_on_sleep=reset_server_alert_on_sleep,
                run_iteration=server_alert_monitor_tick,
                on_iteration_error=on_error,
            ),
            name="server-alert-evaluation",
        )
        tasks.create_task(
            server_alert_delivery_loop(
                server_sleeping=server_sleeping,
                on_iteration_error=on_error,
            ),
            name="server-alert-delivery",
        )
        tasks.create_task(
            run_paper_option_quote_refresh_runtime(
                server_sleeping=server_sleeping,
                on_iteration_error=on_error,
            ),
            name="paper-option-quote-refresh",
        )
