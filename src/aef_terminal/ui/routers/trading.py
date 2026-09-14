from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, Response

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.paper_contract import PaperContractScopeKind
from aef_terminal.ui.paper.actions import (
    PaperActionDeps,
    cancel_paper_order_action,
    close_paper_trade_action,
    create_option_paper_order_action,
    create_paper_order_action,
    patch_paper_order_action,
)
from aef_terminal.ui.paper.account import paper_account_payload
from aef_terminal.ui.paper.journal_queries import (
    PaperJournalQueryDeps,
    paper_events_payload_from_store,
    paper_orders_payload_from_store,
    paper_trades_payload_from_store,
)
from aef_terminal.ui.paper.journal_views import (
    PaperJournalViewDeps,
    paper_trades_csv_response,
    paper_trades_table_response,
)
from aef_terminal.ui.paper.constants import PAPER_JOURNAL_RECENT_HOURS
from aef_terminal.ui.routers.error_payloads import build_action_error_response


def _storage_unavailable_response(**extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code="TRADING_STORAGE_NOT_CONFIGURED",
        category="trading",
        retryable=False,
        error="storage not configured",
        **extra,
    )


def _trading_error_response(
    code: str, error: BaseException | str, *, retryable: bool = True, **extra: Any
) -> dict[str, Any]:
    return build_action_error_response(
        code=code,
        category="trading",
        retryable=retryable,
        error=error,
        **extra,
    )


def _trading_validation_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code=code,
        category="trading",
        retryable=False,
        error=message,
        **extra,
    )


@dataclass(frozen=True)
class TradingRouterDeps:
    store_factory: Callable[[], Any]
    paper_config: Callable[[], dict[str, Any]]
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None]
    paper_trade_enriched: Callable[[dict[str, Any]], dict[str, Any]]
    paper_trade_stats: Callable[[list[dict[str, Any]]], dict[str, Any]]
    paper_replay_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    paper_signal_source_key: Callable[[dict[str, Any]], str]
    paper_setup_source_label: Callable[[dict[str, Any]], str]
    paper_setup_key: Callable[[dict[str, Any]], str]
    paper_trade_session: Callable[[dict[str, Any]], str]
    paper_trade_edge_action: Callable[[dict[str, Any]], str]
    paper_order_price_snapshot: Callable[[dict[str, Any]], dict[str, Any] | None]


def create_trading_router(deps: TradingRouterDeps) -> APIRouter:
    router = APIRouter()
    action_deps = PaperActionDeps(
        paper_config=deps.paper_config,
        queue_paper_trade_telegram=deps.queue_paper_trade_telegram,
        paper_order_price_snapshot=deps.paper_order_price_snapshot,
    )
    journal_query_deps = PaperJournalQueryDeps(
        paper_trade_enriched=deps.paper_trade_enriched,
        paper_trade_stats=deps.paper_trade_stats,
        paper_replay_summary=deps.paper_replay_summary,
    )
    journal_view_deps = PaperJournalViewDeps(
        signal_source_key=deps.paper_signal_source_key,
        setup_source_label=deps.paper_setup_source_label,
        setup_key=deps.paper_setup_key,
        trade_session=deps.paper_trade_session,
        trade_edge_action=deps.paper_trade_edge_action,
    )

    def action_error_response(result: dict[str, Any]) -> dict[str, Any]:
        return _trading_validation_error(
            str(result.get("code") or "PAPER_ACTION_FAILED"),
            str(result.get("message") or "paper action failed"),
        )

    def paper_trades_payload(
        instrument_id: str,
        route_fingerprint: str,
        paper_scope_kind: PaperContractScopeKind = "instrument",
        timeframe: str | None = None,
        limit: int = 200,
    ) -> dict:
        try:
            identity = require_exact_identity_text(
                instrument_id,
                field="instrument_id",
            )
            fingerprint = require_exact_identity_text(
                route_fingerprint,
                field="route_fingerprint",
            )
        except ValueError as exc:
            return _trading_validation_error(
                "PAPER_JOURNAL_IDENTITY_INVALID",
                str(exc),
                trades=[],
                positions=[],
                orders=[],
                fills=[],
                stats={},
            )
        store = deps.store_factory()
        if store is None:
            return _storage_unavailable_response(trades=[], stats={})
        try:
            payload = paper_trades_payload_from_store(
                store,
                journal_query_deps,
                instrument_id=identity,
                route_fingerprint=fingerprint,
                paper_scope_kind=paper_scope_kind,
                timeframe=timeframe,
                limit=limit,
            )
            return payload
        except Exception as exc:
            return _trading_error_response(
                "PAPER_TRADES_LOAD_FAILED",
                exc,
                trades=[],
                stats={},
                replay={},
                account=paper_account_payload([]),
            )

    @router.get("/api/paper/trades")
    def paper_trades(
        instrument_id: str,
        route_fingerprint: str,
        paper_scope_kind: PaperContractScopeKind = "instrument",
        timeframe: str | None = None,
        limit: int = 200,
    ) -> dict:
        return paper_trades_payload(
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            paper_scope_kind=paper_scope_kind,
            timeframe=timeframe,
            limit=limit,
        )

    @router.get("/api/paper/orders")
    def paper_orders(
        instrument_id: str,
        route_fingerprint: str,
        limit: int = 100,
        since_hours: int = PAPER_JOURNAL_RECENT_HOURS,
    ) -> dict[str, Any]:
        try:
            identity = require_exact_identity_text(
                instrument_id,
                field="instrument_id",
            )
            fingerprint = require_exact_identity_text(
                route_fingerprint,
                field="route_fingerprint",
            )
        except ValueError as exc:
            return _trading_validation_error(
                "PAPER_ORDERS_IDENTITY_INVALID",
                str(exc),
                orders=[],
                active=[],
            )
        store = deps.store_factory()
        if store is None:
            return _storage_unavailable_response(orders=[], active=[])
        try:
            return paper_orders_payload_from_store(
                store,
                instrument_id=identity,
                route_fingerprint=fingerprint,
                limit=limit,
                since_hours=since_hours,
            )
        except Exception as exc:
            return _trading_error_response("PAPER_ORDERS_LOAD_FAILED", exc, orders=[], active=[])

    @router.get("/api/paper/events")
    def paper_events(limit: int = 200) -> dict[str, Any]:
        store = deps.store_factory()
        if store is None:
            return _storage_unavailable_response(events=[])
        try:
            return paper_events_payload_from_store(store, limit=limit)
        except Exception as exc:
            return _trading_error_response("PAPER_EVENTS_LOAD_FAILED", exc, events=[])

    @router.post("/api/paper/orders")
    def create_paper_order(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        store = deps.store_factory()
        if store is None:
            return _storage_unavailable_response()
        try:
            result = create_paper_order_action(store, payload, action_deps)
            return result if result.get("ok") else action_error_response(result)
        except ValueError as exc:
            return _trading_validation_error("PAPER_ORDER_CREATE_INVALID", str(exc))
        except Exception as exc:
            return _trading_error_response("PAPER_ORDER_CREATE_FAILED", exc)

    @router.post("/api/paper/options/orders")
    def create_option_paper_order(
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        store = deps.store_factory()
        if store is None:
            return _storage_unavailable_response()
        try:
            result = create_option_paper_order_action(store, payload, action_deps)
            return result if result.get("ok") else action_error_response(result)
        except ValueError as exc:
            return _trading_validation_error(
                "PAPER_OPTION_ORDER_CREATE_INVALID",
                str(exc),
            )
        except Exception as exc:
            return _trading_error_response("PAPER_OPTION_ORDER_CREATE_FAILED", exc)

    @router.post("/api/paper/orders/{order_id}/cancel")
    def cancel_paper_order(
        order_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if set(payload) != {"instrument_id", "route_fingerprint"}:
            return _trading_validation_error(
                "PAPER_ORDER_CANCEL_PAYLOAD_INVALID",
                "cancel payload must contain exactly instrument_id and route_fingerprint",
                cancelled=False,
            )
        try:
            order_key = require_exact_identity_text(
                order_id,
                field="order_id",
            )
            identity = require_exact_identity_text(
                payload.get("instrument_id"),
                field="instrument_id",
            )
            fingerprint = require_exact_identity_text(
                payload.get("route_fingerprint"),
                field="route_fingerprint",
            )
        except ValueError as exc:
            return _trading_validation_error(
                "PAPER_ORDER_CANCEL_IDENTITY_INVALID",
                str(exc),
                cancelled=False,
            )
        try:
            store = deps.store_factory()
            if store is None:
                return _storage_unavailable_response(cancelled=False)
            result = cancel_paper_order_action(
                store,
                order_key,
                instrument_id=identity,
                route_fingerprint=fingerprint,
            )
            return result if result.get("ok") else action_error_response(result)
        except Exception as exc:
            return _trading_error_response("PAPER_ORDER_CANCEL_FAILED", exc, cancelled=False)

    @router.post("/api/paper/trades/{trade_id}/close")
    def close_paper_trade(
        trade_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        allowed_keys = {
            "command_id",
            "instrument_id",
            "route_fingerprint",
            "paper_contract",
            "exit_reason",
        }
        if not {
            "command_id",
            "instrument_id",
            "route_fingerprint",
            "paper_contract",
        }.issubset(payload) or not set(payload).issubset(allowed_keys):
            return _trading_validation_error(
                "PAPER_TRADE_CLOSE_PAYLOAD_INVALID",
                "close payload requires command_id, exact route identity and paper_contract and supports only exit_reason",
            )
        try:
            trade_key = require_exact_identity_text(
                trade_id,
                field="trade_id",
            )
            identity = require_exact_identity_text(
                payload.get("instrument_id"),
                field="instrument_id",
            )
            fingerprint = require_exact_identity_text(
                payload.get("route_fingerprint"),
                field="route_fingerprint",
            )
        except ValueError as exc:
            return _trading_validation_error(
                "PAPER_TRADE_CLOSE_IDENTITY_INVALID",
                str(exc),
            )
        store = deps.store_factory()
        if store is None:
            return _storage_unavailable_response()
        try:
            result = close_paper_trade_action(
                store,
                trade_key,
                {key: payload[key] for key in ("command_id", "exit_reason") if key in payload},
                action_deps,
                instrument_id=identity,
                route_fingerprint=fingerprint,
                paper_contract=payload["paper_contract"],
            )
            return result if result.get("ok") else action_error_response(result)
        except Exception as exc:
            return _trading_error_response("PAPER_TRADE_CLOSE_FAILED", exc)

    @router.patch("/api/paper/orders/{order_id}")
    def patch_paper_order(order_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        mutable_keys = {
            "entry",
            "stop_loss",
            "target",
            "use_stop_loss",
            "use_target",
        }
        if (
            not {"instrument_id", "route_fingerprint"}.issubset(payload)
            or not set(payload).issubset(mutable_keys | {"instrument_id", "route_fingerprint"})
            or not set(payload).intersection(mutable_keys)
        ):
            return _trading_validation_error(
                "PAPER_ORDER_UPDATE_PAYLOAD_INVALID",
                "update payload requires exact route identity and at least one mutable order field",
            )
        try:
            order_key = require_exact_identity_text(
                order_id,
                field="order_id",
            )
            identity = require_exact_identity_text(
                payload.get("instrument_id"),
                field="instrument_id",
            )
            fingerprint = require_exact_identity_text(
                payload.get("route_fingerprint"),
                field="route_fingerprint",
            )
        except ValueError as exc:
            return _trading_validation_error(
                "PAPER_ORDER_UPDATE_IDENTITY_INVALID",
                str(exc),
            )
        try:
            store = deps.store_factory()
            if store is None:
                return _trading_validation_error("PAPER_ORDER_NOT_FOUND", "paper order not found")
            result = patch_paper_order_action(
                store,
                order_key,
                {key: payload[key] for key in mutable_keys if key in payload},
                instrument_id=identity,
                route_fingerprint=fingerprint,
            )
            return result if result.get("ok") else action_error_response(result)
        except ValueError as exc:
            return _trading_validation_error("PAPER_ORDER_UPDATE_INVALID", str(exc))
        except Exception as exc:
            return _trading_error_response("PAPER_ORDER_UPDATE_FAILED", exc)

    @router.get("/api/paper/trades.csv")
    def paper_trades_csv(
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str | None = None,
        limit: int = 1000,
    ) -> Response:
        payload = paper_trades_payload(
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=timeframe,
            limit=limit,
        )
        return paper_trades_csv_response(payload, journal_view_deps)

    @router.get("/paper/trades", response_class=HTMLResponse)
    def paper_trades_table(
        instrument_id: str,
        route_fingerprint: str,
        timeframe: str | None = None,
        limit: int = 1000,
    ) -> HTMLResponse:
        payload = paper_trades_payload(
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=timeframe,
            limit=limit,
        )
        return paper_trades_table_response(
            payload,
            journal_view_deps,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            timeframe=timeframe,
            limit=limit,
        )

    return router
