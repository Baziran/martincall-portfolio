import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Body

from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr.quotes import probe_quotes as ibkr_probe_quotes
from aef_terminal.data.ibkr.session import force_reconnect_async as ibkr_force_reconnect_async
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.instrument_identity import require_exact_instrument_id_sequence
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument
from aef_terminal.ui.routers.error_payloads import build_action_error_response
from aef_terminal.ui.ibkr_gateway_login_control import IbkrGatewayLoginControlError

IBKR_GATEWAY_LOGIN_CONFIRMATION = "START_NEW_IBKR_LOGIN"


@dataclass(frozen=True)
class IbkrRouterDeps:
    apply_ibkr_runtime_settings: Callable[[dict[str, Any] | None], dict[str, Any]]
    apply_ibkr_runtime_settings_async: Callable[[dict[str, Any] | None], Awaitable[dict[str, Any]]]
    combined_quote_instruments: Callable[[list[str] | None], list[dict[str, Any]]]
    disconnect_ibkr_sessions: Callable[..., dict[str, Any]]
    ibkr_gateway_login_control_status: Callable[[], dict[str, Any]]
    request_ibkr_gateway_login: Callable[[], dict[str, Any]]
    server_sleeping: Callable[[], bool]
    server_sleep_status: Callable[[], dict[str, Any]]
    ibkr_port_setting_key: str
    now_iso: Callable[[], str]


def _instrument_id_tokens(raw_instrument_ids: Any, default: list[str] | None = None) -> list[str]:
    values = list(default or []) if raw_instrument_ids is None else raw_instrument_ids
    return list(require_exact_instrument_id_sequence(values, allow_empty=True))


def _ibkr_sleeping_response(deps: IbkrRouterDeps) -> dict[str, Any]:
    return {
        "checked_at": deps.now_iso(),
        "sleep": deps.server_sleep_status(),
        **build_action_error_response(
            code="IBKR_SERVER_SLEEPING",
            category="broker",
            retryable=True,
            error="Server is sleeping. Wake it before reconnecting IBKR.",
        ),
    }


def _enrich_ibkr_reconnect_response(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok"):
        return payload
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), dict) else {}
    failed = [
        name for name, item in sessions.items() if not isinstance(item, dict) or not item.get("ok")
    ]
    errors = [
        str(sessions[name].get("error") or name)
        for name in failed
        if isinstance(sessions.get(name), dict)
    ]
    message = "; ".join(errors) if errors else "IBKR reconnect failed"
    return {
        **build_action_error_response(
            code="IBKR_RECONNECT_FAILED",
            category="broker",
            retryable=True,
            error=message,
        ),
        **payload,
    }


def _ibkr_route(instrument_id: str) -> tuple[str, str, dict[str, Any]] | dict[str, Any]:
    if not isinstance(instrument_id, str) or not instrument_id:
        return build_action_error_response(
            code="IBKR_INSTRUMENT_ID_REQUIRED",
            category="broker",
            retryable=False,
            error="IBKR instrument_id is required",
        )
    instrument = lookup_runtime_instrument(instrument_id)
    display = str(instrument.get("display") or "unknown").strip() or "unknown"
    route = route_instrument(instrument)
    if route.provider != "ibkr":
        return build_action_error_response(
            code="IBKR_PROVIDER_MISMATCH",
            category="broker",
            retryable=False,
            error=f"Instrument belongs to provider {route.provider}",
            instrument_id=route.instrument_id,
        )
    return display, route.provider_symbol, route.instrument


def create_ibkr_router(deps: IbkrRouterDeps) -> APIRouter:
    router = APIRouter()

    @router.post("/api/ibkr/gateway-login")
    def api_ibkr_gateway_login(
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        if deps.server_sleeping():
            return _ibkr_sleeping_response(deps)
        payload = payload or {}
        if payload.get("confirmation") != IBKR_GATEWAY_LOGIN_CONFIRMATION:
            return build_action_error_response(
                code="IBKR_GATEWAY_LOGIN_CONFIRMATION_REQUIRED",
                category="broker",
                retryable=False,
                error="Explicit confirmation is required to start a new IB Gateway login.",
            )
        if not deps.ibkr_gateway_login_control_status().get("configured"):
            return build_action_error_response(
                code="IBKR_GATEWAY_LOGIN_CONTROL_NOT_CONFIGURED",
                category="broker",
                retryable=False,
                error="IB Gateway login control is not configured for this runtime.",
            )
        try:
            ibkr_status = deps.disconnect_ibkr_sessions(timeout=2.0)
        except Exception as exc:
            return {
                "checked_at": deps.now_iso(),
                **build_action_error_response(
                    code="IBKR_GATEWAY_LOGIN_DISCONNECT_FAILED",
                    category="broker",
                    retryable=True,
                    error=exc,
                ),
            }
        try:
            result = deps.request_ibkr_gateway_login()
        except IbkrGatewayLoginControlError as exc:
            return {
                "checked_at": deps.now_iso(),
                "retry_after_seconds": exc.retry_after_seconds,
                **build_action_error_response(
                    code=exc.code,
                    category="broker",
                    retryable=exc.retryable,
                    error=exc.message,
                ),
            }
        except Exception as exc:
            return {
                "checked_at": deps.now_iso(),
                **build_action_error_response(
                    code="IBKR_GATEWAY_LOGIN_REQUEST_FAILED",
                    category="broker",
                    retryable=True,
                    error=exc,
                ),
            }
        return {
            "checked_at": deps.now_iso(),
            "ibkr": ibkr_status,
            **result,
        }

    @router.post("/api/ibkr/reconnect")
    async def api_ibkr_reconnect(
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        if deps.server_sleeping():
            return _ibkr_sleeping_response(deps)
        payload = payload or {}
        connection_settings: dict[str, Any] = {}
        if "ibkr_port" in payload:
            connection_settings[deps.ibkr_port_setting_key] = payload.get("ibkr_port")
        try:
            instrument_ids = _instrument_id_tokens(payload.get("instrument_ids"), [])
        except (TypeError, ValueError) as exc:
            return build_action_error_response(
                code="IBKR_INSTRUMENT_IDS_TYPED_REQUIRED",
                category="broker",
                retryable=False,
                error=exc,
            )
        for token in instrument_ids:
            try:
                routed = _ibkr_route(token)
            except Exception as exc:
                return build_action_error_response(
                    code="IBKR_INSTRUMENT_UNKNOWN",
                    category="broker",
                    retryable=False,
                    error=exc,
                    instrument_id=token,
                )
            if isinstance(routed, dict):
                return routed
            _display, _route_symbol, _instrument = routed

        try:
            timeout = float(payload.get("timeout") or 2.5)
        except TypeError, ValueError:
            timeout = 2.5
        await deps.apply_ibkr_runtime_settings_async(connection_settings or None)
        result = await ibkr_force_reconnect_async(
            quote_instruments=deps.combined_quote_instruments(instrument_ids),
            timeout=timeout,
        )
        return _enrich_ibkr_reconnect_response(
            {
                "checked_at": deps.now_iso(),
                **result,
            }
        )

    @router.post("/api/ibkr/quote-test")
    def api_ibkr_quote_test(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        payload = payload or {}
        connection_settings: dict[str, Any] = {}
        if "ibkr_port" in payload:
            connection_settings[deps.ibkr_port_setting_key] = payload.get("ibkr_port")
        runtime_settings = deps.apply_ibkr_runtime_settings(connection_settings or None)
        display_symbols: list[str] = []
        provider_symbols: list[str] = []
        quote_instruments: list[dict[str, Any]] = []
        try:
            tokens = _instrument_id_tokens(payload.get("instrument_ids"), [])
        except (TypeError, ValueError) as exc:
            return {
                "checked_at": deps.now_iso(),
                "client_id": None,
                "market_data_type": runtime_settings.get("ibkr_market_data_type"),
                "rows": [],
                **build_action_error_response(
                    code="IBKR_INSTRUMENT_IDS_TYPED_REQUIRED",
                    category="broker",
                    retryable=False,
                    error=exc,
                ),
            }
        if not tokens:
            return {
                "checked_at": deps.now_iso(),
                "client_id": None,
                "market_data_type": runtime_settings.get("ibkr_market_data_type"),
                "rows": [],
                **build_action_error_response(
                    code="IBKR_INSTRUMENT_ID_REQUIRED",
                    category="broker",
                    retryable=False,
                    error="IBKR quote-test requires explicit instrument_ids",
                ),
            }
        for token in tokens:
            try:
                routed = _ibkr_route(token)
            except Exception as exc:
                return {
                    "checked_at": deps.now_iso(),
                    "client_id": None,
                    "market_data_type": runtime_settings.get("ibkr_market_data_type"),
                    "rows": [],
                    **build_action_error_response(
                        code="IBKR_INSTRUMENT_UNKNOWN",
                        category="broker",
                        retryable=False,
                        error=exc,
                        instrument_id=token,
                    ),
                }
            if isinstance(routed, dict):
                return {
                    "checked_at": deps.now_iso(),
                    "client_id": None,
                    "market_data_type": runtime_settings.get("ibkr_market_data_type"),
                    "rows": [],
                    **routed,
                }
            display, route_symbol, instrument = routed
            display_symbols.append(display)
            provider_symbols.append(route_symbol)
            quote_instruments.append(instrument)
        try:
            timeout = max(0.5, min(float(payload.get("timeout") or 3.0), 8.0))
        except TypeError, ValueError:
            timeout = 3.0
        try:
            client_id = int(
                payload.get("client_id")
                or os.getenv("AEF_IBKR_PROBE_CLIENT_ID", str(AppConfig().ibkr_client_id + 7000))
            )
        except TypeError, ValueError:
            client_id = AppConfig().ibkr_client_id + 7000

        try:
            quotes = ibkr_probe_quotes(quote_instruments, timeout=timeout, client_id=client_id)
        except Exception as exc:
            return {
                "checked_at": deps.now_iso(),
                "client_id": client_id,
                "market_data_type": runtime_settings.get("ibkr_market_data_type"),
                "rows": [],
                **build_action_error_response(
                    code="IBKR_QUOTE_PROBE_FAILED",
                    category="broker",
                    retryable=True,
                    error=exc,
                ),
            }
        rows: list[dict[str, Any]] = []
        for display, provider_symbol, instrument in zip(
            display_symbols, provider_symbols, quote_instruments, strict=False
        ):
            quote = quotes.get(route_instrument(instrument).fingerprint) or {}
            ok = (
                quote.get("price") is not None
                or quote.get("bid") is not None
                or quote.get("ask") is not None
            )
            rows.append(
                {
                    "symbol": display,
                    "provider_symbol": provider_symbol,
                    "ok": bool(ok),
                    "price": quote.get("price"),
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                    "last": quote.get("last"),
                    "ts": quote.get("ts"),
                    "message": quote.get("message") or "",
                }
            )
        result: dict[str, Any] = {
            "checked_at": deps.now_iso(),
            "ok": all(row["ok"] for row in rows),
            "client_id": client_id,
            "market_data_type": runtime_settings.get("ibkr_market_data_type"),
            "rows": rows,
        }
        if rows and not result["ok"]:
            failed_messages = [
                str(row.get("message") or row.get("symbol") or "quote unavailable")
                for row in rows
                if not row.get("ok")
            ]
            result.update(
                build_action_error_response(
                    code="IBKR_QUOTE_TEST_FAILED",
                    category="broker",
                    retryable=True,
                    error="; ".join(failed_messages) or "IBKR quote test failed",
                )
            )
        return result

    return router
