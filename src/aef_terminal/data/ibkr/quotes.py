from __future__ import annotations
import asyncio
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from aef_terminal.config import AppConfig
from aef_terminal.data.provider_contract import InstrumentRoute, QuoteSnapshotRead
from aef_terminal.runtime.ibkr_settings import ibkr_runtime_settings_snapshot
from aef_terminal.data.ibkr.contracts import (
    _contract_for_instrument,
    _current_future_contract_async,
    _drop_stale_cached_future_contract,
    _format_future_contract_label,
    _instrument_contract_cache_token,
    _mark_trading_hours_stale_on_future_rollover,
    _quote_contract_for_instrument,
    _require_ibkr_instrument,
    futures_contract_is_unqualified,
    futures_contract_month_is_stale,
    futures_rollover_warning_for_contract,
)
from aef_terminal.data.ibkr.event_loop import _ensure_event_loop
from aef_terminal.data.ibkr.market_data import (
    ibkr_market_data_entitlement,
    request_ibkr_market_data_ticker,
)
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.data.ibkr.runtime import (
    _IBKR_CANCEL_IGNORED_ERROR_CODES,
    _IBKR_INFO_ERROR_CODES,
    _IBKR_RUNTIME,
    _LOGGER,
    ibkr_gateway_session_key,
)
from aef_terminal.data.ibkr.session import (
    _connected_quote_ib_async,
    _disconnect_owned_ibkr_session,
    _ibkr_session_ready,
    _reset_ibkr_async_quote_session,
    connect_without_account_sync,
)
from aef_terminal.data.ibkr.utils import (
    _clean_float,
    _clean_price,
    _contract_label,
    _safe_cancel_mkt_data,
)
from aef_terminal.data.instrument_identity import (
    instrument_is_futures_root,
    provider_symbol as exact_provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    route_fingerprint,
)


def runtime_status() -> dict[str, object]:
    """Return current IBKR adapter health without opening new connections."""
    runtime = _IBKR_RUNTIME
    config = AppConfig()
    async_chart_connected = _ibkr_session_ready(runtime.async_chart_session)
    async_quote_connected = _ibkr_session_ready(runtime.async_quote_session)
    async_option_quote_connected = _ibkr_session_ready(runtime.async_option_quote_session)
    async_history_connected = _ibkr_session_ready(runtime.async_history_session)
    quarantined_sessions = runtime.quarantined_sessions_snapshot()
    quarantined_session_count = len(quarantined_sessions)
    quarantined_session_scopes = dict(
        sorted(Counter(entry.session for entry in quarantined_sessions).items())
    )
    quarantined_persistent_sessions = sorted(
        {
            entry.session
            for entry in quarantined_sessions
            if entry.session
            in {
                "history",
                "chart",
                "quote",
                "option_quote",
                "tick_feed",
                "gex_live",
            }
        }
    )
    quote_values = 0
    quote_transport_labels: list[str] = []
    quote_missing_transport_labels: list[str] = []
    quote_route_fingerprints: list[str] = []
    quote_missing_routes: list[str] = []
    with runtime.quote_snapshot_lock:
        quote_snapshots = {
            identity: dict(quote) for identity, quote in runtime.quote_snapshots.items()
        }
        quote_snapshot_routes = dict(runtime.quote_snapshot_routes)
        quote_snapshot_sequence = runtime.quote_snapshot_sequence
        quote_snapshot_entries = len(runtime.quote_snapshots)
        quote_snapshot_updates = runtime.quote_snapshot_updates
        quote_snapshot_evictions = runtime.quote_snapshot_evictions
        quote_snapshot_updated_at = runtime.quote_snapshot_updated_at
    actual_quote_market_data_types: set[int] = set()
    for identity, symbol in quote_snapshot_routes.items():
        _instrument_id, route_key = identity
        quote = quote_snapshots.get(identity) or {}
        raw_actual_market_data_type = quote.get("market_data_type")
        if (
            async_quote_connected
            and type(raw_actual_market_data_type) is int
            and raw_actual_market_data_type in {1, 2, 3, 4}
        ):
            actual_quote_market_data_types.add(raw_actual_market_data_type)
        if symbol:
            quote_transport_labels.append(symbol)
        if route_key:
            quote_route_fingerprints.append(route_key)
        if _quote_has_value(quote):
            quote_values += 1
        else:
            if route_key:
                quote_missing_routes.append(route_key)
        if symbol and route_key in quote_missing_routes:
            quote_missing_transport_labels.append(symbol)
    requested_market_data_types: dict[str, int | None] = {}
    for name, session, connected in (
        ("history", runtime.async_history_session, async_history_connected),
        ("chart", runtime.async_chart_session, async_chart_connected),
        ("quote", runtime.async_quote_session, async_quote_connected),
        (
            "option_quote",
            runtime.async_option_quote_session,
            async_option_quote_connected,
        ),
    ):
        raw_requested_market_data_type = getattr(
            session,
            "_aef_requested_market_data_type",
            None,
        )
        requested_market_data_types[name] = (
            raw_requested_market_data_type
            if connected
            and type(raw_requested_market_data_type) is int
            and raw_requested_market_data_type in {1, 2, 3, 4}
            else None
        )
    last_error = _latest_quote_error_message(max_age_seconds=300.0)
    recent_api_errors = _recent_ibkr_api_errors(max_age_seconds=60.0)
    manager_status = ibkr_market_data_manager.status()
    manager_lanes = manager_status.get("lanes", {})
    quote_lane = manager_lanes.get("quote", {})
    history_lane = manager_lanes.get("history", {})
    chart_lane = manager_lanes.get("chart", {})
    manager_quote_busy = bool(quote_lane.get("running") or quote_lane.get("pending"))
    manager_history_busy = bool(history_lane.get("running") or history_lane.get("pending"))
    manager_chart_busy = bool(chart_lane.get("running") or chart_lane.get("pending"))
    connection_in_progress = runtime.connection_in_progress_snapshot()
    with runtime.live_quote_trade_lock:
        live_trade_event_sequence = runtime.live_quote_trade_sequence
        live_trade_event_buffer = len(runtime.live_quote_trade_events)
        latest_live_trade_event = (
            dict(runtime.live_quote_trade_events[-1]) if runtime.live_quote_trade_events else {}
        )
    if quote_values:
        status = "live"
    elif async_quote_connected:
        status = "connected_no_quotes"
    elif async_history_connected:
        status = "history_only"
    else:
        status = "disconnected"
    history_busy_seconds = None
    history_request = runtime.history_request_label
    if manager_history_busy:
        manager_seconds = history_lane.get("running_seconds")
        history_busy_seconds = float(manager_seconds) if manager_seconds is not None else None
        history_request = str(history_lane.get("running_label") or history_request)
    recent_error_codes = Counter(
        (str(code) for _, _, code, _, _ in recent_api_errors if code is not None)
    )
    recent_error_sources = Counter((source for _, source, _, _, _ in recent_api_errors))
    live_issue = _ibkr_live_issue(recent_api_errors, history_busy_seconds)
    return {
        "ok": status in {"live", "connected_no_quotes", "history_only"},
        "live_ok": quote_values > 0,
        "status": status,
        "connection_in_progress": connection_in_progress,
        "quarantined_session_count": quarantined_session_count,
        "quarantined_session_scopes": quarantined_session_scopes,
        "quarantined_persistent_sessions": quarantined_persistent_sessions,
        "host": config.ibkr_host,
        "port": config.ibkr_port,
        "configured_market_data_type": _market_data_type(),
        "requested_market_data_types": requested_market_data_types,
        "actual_quote_market_data_types": sorted(actual_quote_market_data_types),
        "readonly": config.ibkr_readonly,
        "history_connected": async_history_connected,
        "history_connection_generation": (runtime.history_connection_generation_snapshot()),
        "history_client_id": config.ibkr_history_client_id,
        "history_busy_seconds": round(history_busy_seconds, 3)
        if history_busy_seconds is not None
        else None,
        "history_request": history_request,
        "history_last_activity_at": runtime.history_last_activity_at.isoformat()
        if runtime.history_last_activity_at
        else None,
        "last_history_error": runtime.last_history_error,
        "chart_connected": async_chart_connected,
        "chart_streams": len(runtime.async_chart_streams),
        "chart_client_id": config.ibkr_chart_client_id,
        "last_chart_sync_at": runtime.last_chart_sync_at.isoformat()
        if runtime.last_chart_sync_at
        else None,
        "last_chart_error": runtime.last_chart_error,
        "chart_lock_busy": manager_chart_busy,
        "quote_connected": async_quote_connected,
        "quote_client_id": config.ibkr_quote_client_id,
        "quote_subscriptions": len(quote_snapshot_routes),
        "quote_snapshot_sequence": quote_snapshot_sequence,
        "quote_snapshot_entries": quote_snapshot_entries,
        "quote_snapshot_updates": quote_snapshot_updates,
        "quote_snapshot_evictions": quote_snapshot_evictions,
        "quote_snapshot_updated_at": (
            quote_snapshot_updated_at.isoformat() if quote_snapshot_updated_at is not None else None
        ),
        "quote_snapshot_age_seconds": (
            round(
                max(
                    (datetime.now(tz=UTC) - quote_snapshot_updated_at).total_seconds(),
                    0.0,
                ),
                3,
            )
            if quote_snapshot_updated_at is not None
            else None
        ),
        "live_trade_event_sequence": live_trade_event_sequence,
        "live_trade_event_buffer": live_trade_event_buffer,
        "last_live_trade_at": latest_live_trade_event.get("ts"),
        "last_live_trade_provider_symbol": latest_live_trade_event.get("symbol"),
        "option_quote_connected": async_option_quote_connected,
        "option_quote_client_id": config.ibkr_option_client_id,
        "option_quote_subscriptions": len(runtime.async_option_ticker_cache),
        "option_board_sessions": len(runtime.async_option_board_sessions),
        "shared_option_market_data_sources": len(runtime.async_option_market_data_sources),
        "quote_transport_labels": sorted(set(quote_transport_labels)),
        "quote_missing_transport_labels": sorted(set(quote_missing_transport_labels)),
        "quote_route_fingerprints": sorted(set(quote_route_fingerprints)),
        "quote_missing_routes": sorted(set(quote_missing_routes)),
        "quote_values": quote_values,
        "last_quote_error": last_error,
        "last_api_error": _latest_ibkr_api_error_message(max_age_seconds=300.0),
        "recent_api_error_count_60s": len(recent_api_errors),
        "recent_api_error_codes_60s": dict(recent_error_codes),
        "recent_api_error_sources_60s": dict(recent_error_sources),
        "live_issue": live_issue,
        "ibkr_flood_warning": live_issue if live_issue.startswith("IBKR noisy stream") else "",
        "competing_session": live_issue.startswith("IBKR competing market-data session"),
        "history_lock_busy": manager_history_busy,
        "market_data_manager": manager_status,
        "quote_lock_busy": manager_quote_busy,
        "last_quote_sync_at": runtime.last_quote_sync_at.isoformat()
        if runtime.last_quote_sync_at
        else None,
        "last_hist_duration_seconds": runtime.last_hist_duration_seconds,
    }


def _quote_inputs(
    instruments: Sequence[dict[str, Any]],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    instrument_by_route: dict[str, dict[str, Any]] = {}
    for instrument in instruments:
        if not isinstance(instrument, dict):
            raise ValueError("IBKR quote instruments must be provider-qualified payloads")
        payload = _require_ibkr_instrument(instrument)
        instrument_by_route[route_fingerprint(payload)] = payload
    return sorted(instrument_by_route), instrument_by_route


def cached_quotes(
    routes: Sequence[InstrumentRoute],
    *,
    after_sequence: int | None = None,
) -> QuoteSnapshotRead:
    """Read immutable quote snapshots published by the IBKR owner event."""

    if any(not isinstance(route, InstrumentRoute) or route.provider != "ibkr" for route in routes):
        raise ValueError("IBKR_QUOTE_QUALIFIED_ROUTES_REQUIRED")
    identities = [(route.instrument_id, route.fingerprint) for route in routes]
    sequence, snapshots = _IBKR_RUNTIME.read_quote_snapshots(
        identities, after_sequence=after_sequence
    )
    if snapshots is None:
        return QuoteSnapshotRead(sequence, {}, changed=False)
    result: dict[str, dict[str, Any]] = {}
    for identity in identities:
        snapshot = snapshots.get(identity)
        result[identity[1]] = (
            snapshot if snapshot is not None else _empty_quote("IBKR quote is not subscribed yet")
        )
    return QuoteSnapshotRead(sequence, result)


def _market_data_type() -> int:
    return ibkr_runtime_settings_snapshot().market_data_type


def quote_subscription_generation() -> str:
    """Return the provider-owned generation of persistent quote requests."""

    return f"market_data_type:{_market_data_type()}"


def _quote_cache_key(
    host: str, port: int, client_id: int, readonly: bool, *, instrument: dict[str, Any]
) -> tuple[str, int, int, bool, str]:
    resolved_instrument = _require_ibkr_instrument(instrument)
    identity_token = _instrument_contract_cache_token(resolved_instrument)
    return (
        *ibkr_gateway_session_key(host, port, client_id, readonly),
        identity_token,
    )


def _quote_subscription_needs_rollover(instrument: dict[str, Any], ticker) -> bool:
    contract = getattr(ticker, "contract", None)
    if str(getattr(contract, "secType", "") or "").upper() != "FUT":
        return False
    return futures_contract_is_unqualified(contract) or futures_contract_month_is_stale(contract)


def _mark_quote_contract_resolution(ticker: object, contract: object) -> None:
    resolved_at = float(getattr(contract, "_aef_current_contract_resolved_at", 0.0) or 0.0)
    if resolved_at > 0:
        setattr(ticker, "_aef_current_contract_resolved_at", resolved_at)


def _cancel_quote_subscription(
    ib,
    cache_key: tuple[str, int, int, bool, str],
    ticker,
    *,
    ticker_cache,
    contract_cache,
    option_quote_keys,
) -> None:
    route = _IBKR_RUNTIME.async_quote_ticker_routes.get(id(ticker)) or {}
    contract = getattr(ticker, "contract", None) or contract_cache.get(cache_key)
    if contract is None or not _safe_cancel_mkt_data(
        ib,
        ticker,
        contract,
    ):
        label = _contract_label(contract) if contract is not None else cache_key[-1]
        _reset_ibkr_async_quote_session()
        raise RuntimeError(
            f"IBKR quote cancellation state is ambiguous; quote session was reset for {label}"
        )
    ticker_cache.pop(cache_key, None)
    contract_cache.pop(cache_key, None)
    option_quote_keys.discard(cache_key)
    _IBKR_RUNTIME.async_quote_ticker_routes.pop(id(ticker), None)
    _IBKR_RUNTIME.discard_ticker_bbo_observation(ticker)
    instrument_id = route.get("instrument_id")
    route_key = route.get("route_fingerprint")
    if isinstance(instrument_id, str) and isinstance(route_key, str):
        _IBKR_RUNTIME.discard_quote_snapshot((instrument_id, route_key))


_IBKR_BID_PRICE_TICK_TYPES = frozenset({1, 66})
_IBKR_ASK_PRICE_TICK_TYPES = frozenset({2, 67})
_IBKR_BID_SIZE_TICK_TYPES = frozenset({0, 69})
_IBKR_ASK_SIZE_TICK_TYPES = frozenset({3, 70})


def _record_bbo_observation_updates(tickers: Sequence[object]) -> None:
    """Record only actual IBKR side-price observations for executable BBO."""

    runtime = _IBKR_RUNTIME
    for ticker in tickers:
        for tick in list(getattr(ticker, "ticks", None) or []):
            try:
                tick_type = int(getattr(tick, "tickType"))
            except AttributeError, TypeError, ValueError:
                continue
            side = (
                "bid"
                if tick_type in _IBKR_BID_PRICE_TICK_TYPES
                else "ask"
                if tick_type in _IBKR_ASK_PRICE_TICK_TYPES
                else None
            )
            if side is not None:
                raw_price = getattr(tick, "price", None)
                if raw_price == -1.0 or _clean_price(raw_price) is None:
                    runtime.record_ticker_bbo_observation(
                        ticker,
                        side=side,
                        observed_at=None,
                    )
                    continue
                observed_at = getattr(tick, "time", None)
                if (
                    not isinstance(observed_at, datetime)
                    or observed_at.tzinfo is None
                    or observed_at.utcoffset() is None
                ):
                    continue
                runtime.record_ticker_bbo_observation(
                    ticker,
                    side=side,
                    observed_at=observed_at,
                )
                continue
            invalidated_side = (
                "bid"
                if tick_type in _IBKR_BID_SIZE_TICK_TYPES
                else "ask"
                if tick_type in _IBKR_ASK_SIZE_TICK_TYPES
                else None
            )
            if invalidated_side is None:
                continue
            size = _clean_float(getattr(tick, "size", None))
            if size == 0.0:
                runtime.record_ticker_bbo_observation(
                    ticker,
                    side=invalidated_side,
                    observed_at=None,
                )


def _record_live_quote_updates(tickers: Sequence[object]) -> None:
    _record_bbo_observation_updates(tickers)
    runtime = _IBKR_RUNTIME
    quote_updates: dict[tuple[str, str], dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for ticker in tickers:
        route = runtime.async_quote_ticker_routes.get(id(ticker))
        if not route:
            continue
        symbol = require_exact_identity_text(
            route.get("provider_symbol"),
            field="provider_symbol",
        )
        instrument_id = require_exact_identity_text(
            route.get("instrument_id"),
            field="instrument_id",
        )
        route_key = require_exact_identity_text(
            route.get("route_fingerprint"),
            field="route_fingerprint",
        )
        quote = _quote_from_ticker(ticker, symbol)
        if not _quote_has_value(quote):
            contract_error = _quote_error_for_contract(
                runtime.last_quote_error,
                ticker,
            )
            if contract_error:
                quote["message"] = contract_error
        quote["provider_symbol"] = symbol
        quote_updates[(instrument_id, route_key)] = dict(quote)
        for tick in list(getattr(ticker, "ticks", None) or []):
            try:
                tick_type = int(getattr(tick, "tickType"))
            except AttributeError, TypeError, ValueError:
                continue
            if tick_type != 4:
                continue
            price = _clean_price(getattr(tick, "price", None))
            if price is None:
                continue
            ts = getattr(tick, "time", None)
            if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
                continue
            ts = ts.astimezone(UTC)
            events.append(
                {
                    "symbol": symbol,
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_key,
                    "ts": ts.isoformat(),
                    "gateway_ts": datetime.now(tz=UTC).isoformat(),
                    "price": price,
                    "source": "ibkr:last",
                }
            )
    runtime.publish_quote_snapshots(quote_updates)
    if not events:
        return
    with runtime.live_quote_trade_lock:
        for event in events:
            identity = (
                event["instrument_id"],
                event["route_fingerprint"],
            )
            signature = (str(event.get("ts") or ""), float(event["price"]))
            if runtime.live_quote_trade_signatures.get(identity) == signature:
                continue
            runtime.live_quote_trade_signatures[identity] = signature
            if len(runtime.live_quote_trade_signatures) > 1024:
                stale_identity = next(iter(runtime.live_quote_trade_signatures))
                if stale_identity != identity:
                    runtime.live_quote_trade_signatures.pop(stale_identity, None)
            runtime.live_quote_trade_sequence += 1
            runtime.live_quote_trade_events.append(
                {
                    **event,
                    "sequence": runtime.live_quote_trade_sequence,
                }
            )


def read_live_quote_trade_events(
    after_sequence: int = 0,
    route_identities: Sequence[tuple[str, str]] = (),
) -> tuple[int, list[dict[str, Any]], bool]:
    runtime = _IBKR_RUNTIME
    try:
        raw_cursor = int(after_sequence or 0)
    except TypeError, ValueError:
        raw_cursor = 0
    wanted = {
        (
            require_exact_identity_text(
                instrument_id,
                field="instrument_id",
            ),
            require_exact_identity_text(
                route_key,
                field="route_fingerprint",
            ),
        )
        for instrument_id, route_key in route_identities
    }
    with runtime.live_quote_trade_lock:
        latest_sequence = runtime.live_quote_trade_sequence
        if raw_cursor < 0:
            return latest_sequence, [], False
        cursor = max(raw_cursor, 0)
        oldest_sequence = (
            int(runtime.live_quote_trade_events[0].get("sequence") or 0)
            if runtime.live_quote_trade_events
            else 0
        )
        overflowed = bool(oldest_sequence > cursor + 1)
        newest_first: list[dict[str, Any]] = []
        for event in reversed(runtime.live_quote_trade_events):
            if int(event.get("sequence") or 0) <= cursor:
                break
            if (
                not wanted
                or (
                    event.get("instrument_id"),
                    event.get("route_fingerprint"),
                )
                in wanted
            ):
                newest_first.append(dict(event))
        events = list(reversed(newest_first))
    return latest_sequence, events, overflowed


async def sync_quote_subscriptions_async(
    instruments: Sequence[dict[str, Any]],
    timeout: float = 1.2,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
) -> dict[str, int]:
    """Synchronize quote subscriptions on the IBKR owner loop."""
    normalized, instrument_by_route = _quote_inputs(instruments)
    symbols = [exact_provider_symbol(instrument_by_route[key], "ibkr") for key in normalized]
    label = ",".join(symbols[:4]) + ("..." if len(symbols) > 4 else "")
    subscription_timeout = max(float(timeout) + 1.0, 2.0)
    owner_timeout = max((len(normalized) + 1) * subscription_timeout + 3.0, 5.0)
    return await ibkr_market_data_manager.run_coroutine(
        "quote",
        f"quotes {label}" if label else "quotes cleanup",
        lambda: _sync_quote_subscriptions_async_owned(
            normalized, timeout, host, port, client_id, readonly, instrument_by_route
        ),
        timeout=owner_timeout,
    )


async def _sync_quote_subscriptions_async_owned(
    normalized: Sequence[str],
    timeout: float = 1.2,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    instruments_by_route: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    runtime = _IBKR_RUNTIME
    config = AppConfig()
    resolved_host = host or config.ibkr_host
    resolved_port = int(port or config.ibkr_port)
    resolved_client_id = int(client_id or config.ibkr_quote_client_id)
    resolved_readonly = config.ibkr_readonly if readonly is None else bool(readonly)
    instrument_by_route = instruments_by_route or {}
    wanted_keys = {
        _quote_cache_key(
            resolved_host,
            resolved_port,
            resolved_client_id,
            resolved_readonly,
            instrument=instrument_by_route[route_key],
        )
        for route_key in normalized
    }
    session_prefix = ibkr_gateway_session_key(
        resolved_host, resolved_port, resolved_client_id, resolved_readonly
    )

    def publish_active_regular_routes() -> int:
        active_routes: dict[tuple[str, str], str] = {}
        for key, ticker in runtime.async_ticker_cache.items():
            if key[:4] != session_prefix or key in runtime.async_option_quote_keys:
                continue
            route = runtime.async_quote_ticker_routes.get(id(ticker))
            if not route:
                raise RuntimeError("IBKR_QUOTE_ROUTE_METADATA_REQUIRED")
            identity = (
                require_exact_identity_text(
                    route.get("instrument_id"),
                    field="instrument_id",
                ),
                require_exact_identity_text(
                    route.get("route_fingerprint"),
                    field="route_fingerprint",
                ),
            )
            active_routes[identity] = require_exact_identity_text(
                route.get("provider_symbol"),
                field="provider_symbol",
            )
        runtime.publish_quote_snapshots(
            {},
            active_routes=active_routes,
        )
        return len(active_routes)

    try:
        ib = await _connected_quote_ib_async(
            resolved_host, resolved_port, resolved_client_id, resolved_readonly, timeout
        )
    except Exception:
        _reset_ibkr_async_quote_session()
        raise
    try:
        if not normalized:
            _cancel_stale_async_quote_subscriptions(
                ib,
                session_prefix,
                wanted_keys,
            )
            runtime.last_quote_wanted_keys = set()
            runtime.last_quote_sync_at = datetime.now(tz=UTC)
            await asyncio.sleep(0)
            return {
                "requested": 0,
                "active": publish_active_regular_routes(),
            }
        # One subscription generation uses one request preference. A setting
        # change during this pass is applied by the next deterministic sync.
        configured_market_data_type = _market_data_type()
        if getattr(ib, "_aef_requested_market_data_type", None) != configured_market_data_type:
            try:
                ib.reqMarketDataType(configured_market_data_type)
                ib._aef_requested_market_data_type = configured_market_data_type
            except Exception as exc:
                _LOGGER.warning(
                    "IBKR quote market-data-type request failed for %s:%s clientId %s",
                    resolved_host,
                    resolved_port,
                    resolved_client_id,
                    exc_info=True,
                )
                raise RuntimeError(
                    "IBKR quote subscription sync could not apply requested "
                    f"market-data type {configured_market_data_type}"
                ) from exc
        _cancel_stale_async_quote_subscriptions(
            ib,
            session_prefix,
            wanted_keys,
        )
        subscription_timeout = max(float(timeout) + 1.0, 2.0)
        failures: list[str] = []
        ordered_routes = sorted(normalized)
        for route_key in ordered_routes:
            qualified = instrument_by_route[route_key]
            symbol = exact_provider_symbol(qualified, "ibkr")
            cache_key = _quote_cache_key(
                resolved_host,
                resolved_port,
                resolved_client_id,
                resolved_readonly,
                instrument=qualified,
            )
            existing = runtime.async_ticker_cache.get(cache_key)
            if existing is not None:
                rollover_required = _quote_subscription_needs_rollover(
                    qualified,
                    existing,
                )
                generation_changed = (
                    getattr(
                        existing,
                        "_aef_requested_market_data_type",
                        None,
                    )
                    != configured_market_data_type
                )
                if rollover_required or generation_changed:
                    if rollover_required:
                        _mark_trading_hours_stale_on_future_rollover(qualified)
                    _cancel_quote_subscription(
                        ib,
                        cache_key,
                        existing,
                        ticker_cache=runtime.async_ticker_cache,
                        contract_cache=runtime.async_quote_contract_cache,
                        option_quote_keys=runtime.async_option_quote_keys,
                    )
                else:
                    runtime.async_quote_ticker_routes[id(existing)] = {
                        "provider_symbol": symbol,
                        "instrument_id": qualified_instrument_id(qualified),
                        "route_fingerprint": route_key,
                    }
                    continue
            try:
                contract = await asyncio.wait_for(
                    _quote_contract_for_instrument_async_cache(
                        ib,
                        resolved_host,
                        resolved_port,
                        resolved_client_id,
                        resolved_readonly,
                        instrument=qualified,
                    ),
                    timeout=subscription_timeout,
                )
                ticker = request_ibkr_market_data_ticker(
                    ib,
                    contract,
                    "",
                    False,
                    False,
                )
                _mark_quote_contract_resolution(ticker, contract)
                ticker._aef_requested_market_data_type = configured_market_data_type
                runtime.async_ticker_cache[cache_key] = ticker
                runtime.async_quote_ticker_routes[id(ticker)] = {
                    "provider_symbol": symbol,
                    "instrument_id": qualified_instrument_id(qualified),
                    "route_fingerprint": route_key,
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                failures.append(f"{symbol}: {detail}")
                _LOGGER.warning("IBKR quote subscription failed for %s: %s", symbol, detail)
                continue
        await asyncio.sleep(0.03)
        runtime.last_quote_wanted_keys = set(wanted_keys)
        runtime.last_quote_sync_at = datetime.now(tz=UTC)
        active_count = publish_active_regular_routes()
        if failures:
            raise RuntimeError(
                "IBKR quote subscription sync incomplete: " + "; ".join(failures[:8])
            )
        return {"requested": len(normalized), "active": active_count}
    except asyncio.CancelledError:
        raise


async def _quote_contract_for_instrument_async_cache(
    ib,
    host: str,
    port: int,
    client_id: int,
    readonly: bool,
    *,
    instrument: dict[str, Any],
):
    runtime = _IBKR_RUNTIME
    resolved_instrument = _require_ibkr_instrument(instrument)
    identity_token = _instrument_contract_cache_token(resolved_instrument)
    cache_key = (host, int(port), int(client_id), bool(readonly), identity_token)
    _drop_stale_cached_future_contract(
        runtime.async_quote_contract_cache,
        cache_key,
        instrument=resolved_instrument,
    )
    cached = runtime.async_quote_contract_cache.get(cache_key)
    if cached is not None:
        return cached
    if instrument_is_futures_root(resolved_instrument):
        contract = await _current_future_contract_async(
            ib,
            instrument=resolved_instrument,
        )
    else:
        contract = _contract_for_instrument(resolved_instrument)
    runtime.async_quote_contract_cache[cache_key] = contract
    return contract


def _cancel_stale_async_quote_subscriptions(
    ib,
    session_prefix: tuple[str, int, int, bool],
    wanted_keys: set[tuple[str, int, int, bool, str]],
) -> None:
    runtime = _IBKR_RUNTIME
    for key in [
        item
        for item in list(runtime.async_ticker_cache)
        if item[:4] == session_prefix
        and item not in wanted_keys
        and (item not in runtime.async_option_quote_keys)
    ]:
        ticker = runtime.async_ticker_cache.get(key)
        if ticker is None:
            continue
        _cancel_quote_subscription(
            ib,
            key,
            ticker,
            ticker_cache=runtime.async_ticker_cache,
            contract_cache=runtime.async_quote_contract_cache,
            option_quote_keys=runtime.async_option_quote_keys,
        )


def probe_quotes(
    instruments: Sequence[dict[str, Any]],
    timeout: float = 3.0,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
) -> dict[str, dict[str, float | str | None]]:
    """Open a short-lived quote client for subscription diagnostics.

    This is intentionally separate from the persistent quote stream. It must never reuse
    or replace the persistent watchlist socket, because diagnostics for selected instruments
    can produce entitlement errors while ES/CL/GC live quotes are healthy.
    """
    normalized, instrument_by_route = _quote_inputs(instruments)
    if not normalized:
        return {}
    _ensure_event_loop()
    config = AppConfig()
    resolved_host = host or config.ibkr_host
    resolved_port = int(port or config.ibkr_port)
    resolved_client_id = int(client_id or config.ibkr_client_id + 7000)
    resolved_readonly = config.ibkr_readonly if readonly is None else bool(readonly)
    wait_seconds = max(0.2, min(float(timeout), 8.0))
    from ib_async import IB

    ib = IB()
    local_errors: list[tuple[datetime, int | None, str, str, int]] = []

    def record_local_error(*args) -> None:
        code: int | None = None
        message = ""
        contract_text = ""
        contract_id = 0
        try:
            if len(args) >= 2:
                code = int(args[1])
            if len(args) >= 3:
                message = str(args[2])
            if len(args) >= 4 and args[3] is not None:
                contract = args[3]
                contract_text = str(
                    getattr(contract, "localSymbol", None)
                    or getattr(contract, "symbol", None)
                    or contract
                )
                contract_id = int(getattr(contract, "conId", 0) or 0)
        except Exception:
            message = "unknown IBKR quote error"
        if code in _IBKR_INFO_ERROR_CODES or code in _IBKR_CANCEL_IGNORED_ERROR_CODES:
            return
        local_errors.append((datetime.now(tz=UTC), code, message, contract_text, contract_id))

    def local_error_for_contract(ticker: object) -> str:
        for error in reversed(local_errors):
            contract_error = _quote_error_for_contract(error, ticker)
            if contract_error:
                return contract_error
        return ""

    tickers: dict[str, object] = {}
    contracts: dict[str, object] = {}
    result: dict[str, dict[str, float | str | None]] = {}
    try:
        connect_without_account_sync(
            ib, resolved_host, resolved_port, resolved_client_id, wait_seconds
        )
        ib.errorEvent += record_local_error
        for route_key in normalized:
            qualified = instrument_by_route[route_key]
            symbol = exact_provider_symbol(qualified, "ibkr")
            try:
                contract = _quote_contract_for_instrument(
                    ib,
                    qualified,
                )
                ticker = request_ibkr_market_data_ticker(
                    ib,
                    contract,
                    "",
                    False,
                    False,
                )
                contracts[route_key] = contract
                tickers[route_key] = ticker
            except Exception as exc:
                result[route_key] = _empty_quote(f"IBKR quote probe failed: {exc}")
        deadline_steps = max(1, int(wait_seconds / 0.15))
        for _ in range(deadline_steps):
            if tickers and all(
                (
                    _quote_has_value(
                        _quote_from_ticker(
                            ticker,
                            exact_provider_symbol(instrument_by_route[route_key], "ibkr"),
                        )
                    )
                    for route_key, ticker in tickers.items()
                )
            ):
                break
            ib.sleep(0.15)
        for route_key, ticker in tickers.items():
            symbol = exact_provider_symbol(instrument_by_route[route_key], "ibkr")
            quote = _quote_from_ticker(ticker, symbol)
            if not _quote_has_value(quote):
                message = local_error_for_contract(ticker)
                if message:
                    quote["message"] = message
            result[route_key] = quote
        for route_key in normalized:
            result.setdefault(route_key, _empty_quote("IBKR quote probe did not produce a ticker"))
        return result
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return {
            route_key: _empty_quote(f"IBKR quote probe unavailable: {message}")
            for route_key in normalized
        }
    finally:
        for route_key, ticker in list(tickers.items()):
            contract = contracts.get(route_key) or getattr(ticker, "contract", None)
            if contract is None:
                continue
            if not _safe_cancel_mkt_data(ib, ticker, contract):
                _LOGGER.warning(
                    "IBKR quote probe cancellation was not confirmed for %s",
                    _contract_label(contract),
                )
        try:
            _disconnect_owned_ibkr_session(
                ib,
                session="quote_probe",
                session_key=(
                    resolved_host,
                    resolved_port,
                    resolved_client_id,
                    resolved_readonly,
                ),
            )
        except Exception:
            _LOGGER.warning(
                "IBKR quote probe socket was quarantined for %s:%s clientId %s",
                resolved_host,
                resolved_port,
                resolved_client_id,
                exc_info=True,
            )


def _quote_has_value(quote: dict[str, Any]) -> bool:
    return (
        quote.get("price") is not None
        or quote.get("bid") is not None
        or quote.get("ask") is not None
    )


def _empty_quote(message: str = "") -> dict[str, Any]:
    market_data_entitlement = ibkr_market_data_entitlement(None)
    return {
        "price": None,
        "price_source": "unavailable",
        "bid": None,
        "ask": None,
        "last": None,
        "close": None,
        "bid_size": None,
        "ask_size": None,
        "last_size": None,
        "ts": None,
        "provider_ts": None,
        "received_at": None,
        "time_basis": None,
        "entitlement": "broker",
        "market_data_type": None,
        "market_data_entitlement": market_data_entitlement,
        "is_delayed": market_data_entitlement in {"delayed", "delayed_frozen"},
        "message": message,
    }


def _record_quote_error(*args) -> None:
    runtime = _IBKR_RUNTIME
    code: int | None = None
    message = ""
    contract_text = ""
    contract_id = 0
    try:
        if len(args) >= 2:
            code = int(args[1])
        if len(args) >= 3:
            message = str(args[2])
        if len(args) >= 4 and args[3] is not None:
            contract = args[3]
            contract_text = str(
                getattr(contract, "localSymbol", None)
                or getattr(contract, "symbol", None)
                or contract
            )
            contract_id = int(getattr(contract, "conId", 0) or 0)
    except Exception:
        message = "unknown IBKR quote error"
    if code in _IBKR_CANCEL_IGNORED_ERROR_CODES:
        _LOGGER.debug("Ignored IBKR quote cancellation error %s: %s", code, message)
        return
    if code in _IBKR_INFO_ERROR_CODES:
        return
    error = (datetime.now(tz=UTC), code, message, contract_text, contract_id)
    _record_ibkr_api_error("quote", *args)
    runtime.last_quote_error = error
    runtime.quote_errors.append(error)
    del runtime.quote_errors[:-20]
    affected_tickers = [
        ticker
        for ticker in runtime.async_ticker_cache.values()
        if runtime.async_quote_ticker_routes.get(id(ticker))
        and _quote_error_for_contract(error, ticker)
    ]
    if affected_tickers:
        _record_live_quote_updates(affected_tickers)


def _recent_quote_error_message(start_index: int) -> str:
    runtime = _IBKR_RUNTIME
    recent = [
        item
        for item in runtime.quote_errors[start_index:]
        if datetime.now(tz=UTC) - item[0] <= timedelta(seconds=8)
    ]
    if not recent:
        return ""
    _, code, message, contract_text, _contract_id = recent[-1]
    suffix = f" ({contract_text})" if contract_text else ""
    return (
        f"IBKR quote error {code}: {message}{suffix}"
        if code is not None
        else f"IBKR quote error: {message}{suffix}"
    )


def _latest_quote_error_message(max_age_seconds: float = 120.0) -> str:
    runtime = _IBKR_RUNTIME
    if runtime.last_quote_error is None:
        return ""
    ts, code, message, contract_text, _contract_id = runtime.last_quote_error
    if datetime.now(tz=UTC) - ts > timedelta(seconds=max_age_seconds):
        return ""
    suffix = f" ({contract_text})" if contract_text else ""
    return (
        f"IBKR quote error {code}: {message}{suffix}"
        if code is not None
        else f"IBKR quote error: {message}{suffix}"
    )


def _record_ibkr_api_error(source: str, *args) -> None:
    runtime = _IBKR_RUNTIME
    code: int | None = None
    message = ""
    contract_text = ""
    try:
        if len(args) >= 2:
            code = int(args[1])
        if len(args) >= 3:
            message = str(args[2])
        if len(args) >= 4 and args[3] is not None:
            contract = args[3]
            contract_text = str(
                getattr(contract, "localSymbol", None)
                or getattr(contract, "symbol", None)
                or contract
            )
    except Exception:
        message = "unknown IBKR API error"
    if source == "history" and code in {1101, 1102}:
        runtime.advance_history_connection_generation()
    if code in _IBKR_CANCEL_IGNORED_ERROR_CODES or code in _IBKR_INFO_ERROR_CODES:
        return
    runtime.api_errors.append((datetime.now(tz=UTC), str(source), code, message, contract_text))
    del runtime.api_errors[:-100]


def _recent_ibkr_api_errors(
    max_age_seconds: float,
) -> list[tuple[datetime, str, int | None, str, str]]:
    runtime = _IBKR_RUNTIME
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=max_age_seconds)
    return [item for item in runtime.api_errors if item[0] >= cutoff]


def _latest_ibkr_api_error_message(max_age_seconds: float = 120.0) -> str:
    recent = _recent_ibkr_api_errors(max_age_seconds)
    if not recent:
        return ""
    _, source, code, message, contract_text = recent[-1]
    suffix = f" ({contract_text})" if contract_text else ""
    if code is None:
        return f"IBKR {source} error: {message}{suffix}"
    return f"IBKR {source} error {code}: {message}{suffix}"


def _ibkr_live_issue(
    recent_errors: Sequence[tuple[datetime, str, int | None, str, str]],
    history_busy_seconds: float | None,
) -> str:
    for _, source, code, message, contract_text in reversed(recent_errors):
        text = f"{message} {contract_text}".lower()
        if (
            code == 10197
            or "competing live session" in text
            or "competing session" in text
            or ("duplicate" in text)
            or ("another session" in text)
        ):
            suffix = f" ({contract_text})" if contract_text else ""
            return f"IBKR competing market-data session on {source}: {message}{suffix}"
    if len(recent_errors) >= 10:
        sources = Counter((source for _, source, _, _, _ in recent_errors))
        codes = Counter((str(code) for _, _, code, _, _ in recent_errors if code is not None))
        return f"IBKR noisy stream: {len(recent_errors)} API errors in 60s; sources={dict(sources)}; codes={dict(codes)}"
    if history_busy_seconds is not None and history_busy_seconds >= 12.0:
        request_label = _IBKR_RUNTIME.history_request_label
        label = f" ({request_label})" if request_label else ""
        return f"IBKR history request is blocking for {history_busy_seconds:.1f}s{label}"
    return ""


def _quote_error_for_contract(
    error: tuple[datetime, int | None, str, str, int] | None,
    ticker: object,
) -> str:
    """Attach a broker error only to its exact contract when IBKR identifies it."""
    if error is None:
        return ""
    _at, code, message, contract_text, error_contract_id = error
    ticker_contract = getattr(ticker, "contract", None)
    ticker_contract_id = int(getattr(ticker_contract, "conId", 0) or 0)
    if error_contract_id > 0 and ticker_contract_id > 0 and error_contract_id != ticker_contract_id:
        return ""
    if error_contract_id <= 0 and "REQUIRES ADDITIONAL SUBSCRIPTION" in message.upper():
        return ""
    suffix = f" ({contract_text})" if contract_text else ""
    return (
        f"IBKR quote error {code}: {message}{suffix}"
        if code is not None
        else f"IBKR quote error: {message}{suffix}"
    )


def _quote_from_ticker(ticker, symbol: str | None = None) -> dict[str, Any]:
    _record_bbo_observation_updates((ticker,))
    raw_bid = getattr(ticker, "bid", None)
    raw_ask = getattr(ticker, "ask", None)
    raw_last = getattr(ticker, "last", None)
    raw_close = getattr(ticker, "close", None)
    # IBKR uses exactly -1.0 for an unset ticker price. Keep that
    # provider-specific wire sentinel at this adapter boundary; the shared
    # price contract remains finite and signed for real exchange prices.
    bid = None if raw_bid == -1.0 else _clean_price(raw_bid)
    ask = None if raw_ask == -1.0 else _clean_price(raw_ask)
    last = None if raw_last == -1.0 else _clean_price(raw_last)
    close = None if raw_close == -1.0 else _clean_price(raw_close)
    contract = getattr(ticker, "contract", None)
    contract_label = _format_future_contract_label(contract) if contract is not None else ""
    if symbol and str(getattr(contract, "secType", "") or "").upper() == "FUT":
        local_symbol = (
            str(getattr(contract, "localSymbol", "") or "") if contract is not None else ""
        )
        if not local_symbol:
            return _empty_quote(
                f"IBKR futures quote for {symbol} is missing a qualified concrete contract"
            )
    midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None and (ask >= bid) else None
    market_price = None
    try:
        raw_market_price = ticker.marketPrice()
        market_price = None if raw_market_price == -1.0 else _clean_price(raw_market_price)
    except Exception:
        market_price = None
    quote_time = getattr(ticker, "time", None)
    if (
        isinstance(quote_time, datetime)
        and quote_time.tzinfo is not None
        and quote_time.utcoffset() is not None
    ):
        packet_received_at = quote_time.astimezone(UTC).isoformat()
    else:
        packet_received_at = None
    bid_observed_at, ask_observed_at = _IBKR_RUNTIME.ticker_bbo_observation(ticker)
    bid_received_at = bid_observed_at.isoformat() if bid_observed_at is not None else None
    ask_received_at = ask_observed_at.isoformat() if ask_observed_at is not None else None
    raw_last_timestamp = getattr(ticker, "lastTimestamp", None)
    last_provider_ts = (
        raw_last_timestamp.astimezone(UTC).isoformat()
        if isinstance(raw_last_timestamp, datetime)
        and raw_last_timestamp.tzinfo is not None
        and raw_last_timestamp.utcoffset() is not None
        else None
    )
    contract_sec_type = str(getattr(contract, "secType", "") or "").upper()
    bid_ask_observed_at = (
        min(bid_observed_at, ask_observed_at)
        if midpoint is not None and bid_observed_at is not None and ask_observed_at is not None
        else None
    )
    bid_ask_received_at = (
        bid_ask_observed_at.isoformat()
        if bid_ask_observed_at is not None
        else packet_received_at
        if midpoint is not None and contract_sec_type not in {"OPT", "FOP"}
        else None
    )
    option_midpoint_is_current = bool(
        contract_sec_type in {"OPT", "FOP"}
        and midpoint is not None
        and bid_ask_received_at is not None
    )
    if option_midpoint_is_current:
        # Options may trade infrequently while their executable market keeps
        # updating. A current non-crossed bid/ask pair is therefore the live
        # option valuation input; an older Last must not make the quote stale.
        price = midpoint
        price_source = "bid_ask_mid"
        provider_ts = None
        time_basis = "client_receive"
        received_at = bid_ask_received_at
        ts = bid_ask_received_at
    elif last is not None and last_provider_ts is not None:
        price = last
        price_source = "last"
        provider_ts = last_provider_ts
        time_basis = "provider_event"
        received_at = packet_received_at
        ts = provider_ts
    elif midpoint is not None:
        price = midpoint
        price_source = "bid_ask_mid"
        provider_ts = None
        received_at = bid_ask_received_at
        time_basis = "client_receive" if received_at is not None else None
        ts = received_at if time_basis == "client_receive" else None
    elif last is not None:
        # A lone last without IBKR tick 45/88 has no trade-event clock. Keep it
        # available for display, but never stamp it with a bid/ask packet time.
        price = last
        price_source = "last"
        provider_ts = None
        time_basis = None
        received_at = packet_received_at
        ts = None
    elif market_price is not None:
        price = market_price
        price_source = "ibkr_market_price"
        provider_ts = None
        time_basis = None
        received_at = packet_received_at
        ts = None
    elif close is not None:
        price = close
        price_source = "previous_close"
        provider_ts = None
        time_basis = None
        received_at = packet_received_at
        ts = None
    else:
        price = None
        price_source = "unavailable"
        provider_ts = None
        time_basis = None
        received_at = packet_received_at
        ts = None
    raw_market_data_type = getattr(ticker, "marketDataType", None)
    market_data_type = (
        raw_market_data_type
        if type(raw_market_data_type) is int and raw_market_data_type in {1, 2, 3, 4}
        else None
    )
    market_data_entitlement = ibkr_market_data_entitlement(market_data_type)
    quote = {
        "price": price,
        "price_source": price_source,
        "bid": bid,
        "ask": ask,
        "last": last,
        "close": close,
        "bid_size": _clean_float(getattr(ticker, "bidSize", None)),
        "ask_size": _clean_float(getattr(ticker, "askSize", None)),
        "last_size": _clean_float(getattr(ticker, "lastSize", None)),
        "contract": contract_label or None,
        "contract_month": str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
        if contract is not None
        else None,
        "local_symbol": str(getattr(contract, "localSymbol", "") or "")
        if contract is not None
        else None,
        "ts": ts,
        "provider_ts": provider_ts,
        "last_provider_ts": last_provider_ts,
        "received_at": received_at,
        "packet_received_at": packet_received_at,
        "bid_received_at": bid_received_at,
        "ask_received_at": ask_received_at,
        "bid_ask_received_at": bid_ask_received_at,
        "time_basis": time_basis,
        "entitlement": "broker",
        "market_data_type": market_data_type,
        "market_data_entitlement": market_data_entitlement,
        "is_delayed": market_data_entitlement in {"delayed", "delayed_frozen"},
    }
    if contract is not None:
        rollover = futures_rollover_warning_for_contract(contract)
        if rollover:
            quote["contract_rollover_warning"] = rollover
            quote["contract_rollover_due"] = True
    return quote
