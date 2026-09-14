from __future__ import annotations
import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Sequence
from typing import Any
from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.data.ibkr.runtime import (
    _IBKR_CLIENT_ID_IN_USE_BACKOFF_SECONDS,
    _IBKR_CONNECT_BACKOFF_SECONDS,
    _IBKR_RUNTIME,
    _LOGGER,
    ibkr_gateway_session_key,
)
from aef_terminal.runtime.async_tasks import run_cancellation_deferred, settle_physical_task

_IBKR_QUARANTINE_RETRY_TIMEOUT_SECONDS = 1.0


def _ibkr_session_ready(ib: Any | None) -> bool:
    """Return whether one owned IB session completed the API handshake."""

    if ib is None or _IBKR_RUNTIME.is_session_quarantined(ib):
        return False
    try:
        return bool(ib.isConnected())
    except Exception:
        _LOGGER.warning(
            "IBKR owned session readiness check failed for clientId %s",
            getattr(getattr(ib, "wrapper", None), "clientId", None),
            exc_info=True,
        )
        return False


def _install_chart_error_dispatch_policy(ib: Any) -> None:
    """Consume only exact acknowledgements for manager-owned chart cancellation."""

    wrapper = getattr(ib, "wrapper", None)
    upstream_error = getattr(wrapper, "error", None)
    if wrapper is None or not callable(upstream_error):
        raise RuntimeError("IBKR chart wrapper error dispatcher is unavailable")

    def dispatch_chart_error(
        req_id: int,
        error_code: int,
        error_string: str,
        advanced_order_reject_json: str,
    ) -> None:
        if int(error_code) == 162 and _IBKR_RUNTIME.consume_planned_chart_cancel(req_id):
            _LOGGER.debug(
                "Ignored planned IBKR chart cancellation for reqId %s: %s",
                req_id,
                error_string,
            )
            return
        upstream_error(
            req_id,
            error_code,
            error_string,
            advanced_order_reject_json,
        )

    wrapper.error = dispatch_chart_error


def _ibkr_transport_state(ib: Any) -> str:
    """Return the strongest observable state of the ib_async socket transport."""

    client = getattr(ib, "client", None)
    if client is None:
        return "unknown"
    connection = getattr(client, "conn", None)
    if connection is not None and hasattr(connection, "transport"):
        transport = getattr(connection, "transport", None)
        if transport is None:
            return "closed"
        is_closing = getattr(transport, "is_closing", None)
        if callable(is_closing):
            try:
                if bool(is_closing()):
                    return "closing"
            except Exception:
                _LOGGER.debug(
                    "IBKR transport closing-state check failed",
                    exc_info=True,
                )
        return "open"
    is_connected = getattr(client, "isConnected", None)
    if callable(is_connected):
        try:
            return "open" if bool(is_connected()) else "closed"
        except Exception:
            _LOGGER.debug(
                "IBKR client connection-state check failed",
                exc_info=True,
            )
    return "unknown"


def _ibkr_session_connection_key(
    ib: Any,
    session_key: object,
) -> tuple[str, int, int] | None:
    raw_key = getattr(ib, "_aef_connection_key", None)
    if raw_key is None:
        raw_key = session_key
    if not isinstance(raw_key, tuple) or len(raw_key) < 3:
        return None
    host, port, client_id = raw_key[:3]
    if not isinstance(host, str) or not host:
        return None
    try:
        return (host, int(port), int(client_id))
    except TypeError, ValueError:
        return None


def _retain_quarantined_ibkr_session(
    ib: Any,
    *,
    session: str,
    session_key: object,
) -> None:
    _IBKR_RUNTIME.retain_quarantined_session(
        ib,
        session=session,
        session_key=session_key,
        connection_key=_ibkr_session_connection_key(ib, session_key),
        owner_loop=getattr(ib, "_aef_owner_loop", None),
        owner_thread_id=getattr(ib, "_aef_owner_thread_id", None),
    )


def _bind_ibkr_session_owner(
    ib: Any,
    connection_key: tuple[str, int, int],
) -> None:
    try:
        owner_loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            owner_loop = asyncio.get_event_loop()
        except RuntimeError:
            owner_loop = None
    ib._aef_connection_key = connection_key
    ib._aef_owner_loop = owner_loop
    ib._aef_owner_thread_id = threading.get_ident()


def _disconnect_owned_ibkr_session(
    ib: Any | None,
    *,
    session: str,
    session_key: object,
) -> bool:
    """Close an owned IB session, including a transport-only partial session."""

    if ib is None:
        return True
    transport_state = _ibkr_transport_state(ib)
    if transport_state == "closed":
        _IBKR_RUNTIME.release_quarantined_session(ib)
        return True
    if transport_state == "closing":
        _retain_quarantined_ibkr_session(
            ib,
            session=session,
            session_key=session_key,
        )
        return False
    owner_loop = getattr(ib, "_aef_owner_loop", None)
    owner_thread_id = getattr(ib, "_aef_owner_thread_id", None)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    on_owner_loop = owner_loop is not None and current_loop is owner_loop
    wrong_owner_thread = (
        owner_thread_id is not None
        and owner_thread_id != threading.get_ident()
        and not on_owner_loop
    )
    wrong_owner_loop = (
        owner_loop is not None and current_loop is not None and current_loop is not owner_loop
    )
    if wrong_owner_thread or wrong_owner_loop:
        _retain_quarantined_ibkr_session(
            ib,
            session=session,
            session_key=session_key,
        )
        raise RuntimeError(
            f"IBKR {session} session teardown requires its owner event-loop/thread; "
            "ownership retained"
        )
    primary_error: Exception | None = None
    try:
        ib.disconnect()
    except Exception as exc:
        primary_error = exc
    transport_state = _ibkr_transport_state(ib)
    if transport_state == "closed":
        _IBKR_RUNTIME.release_quarantined_session(ib)
        return True
    if transport_state == "closing":
        _retain_quarantined_ibkr_session(
            ib,
            session=session,
            session_key=session_key,
        )
        return False
    client = getattr(ib, "client", None)
    if transport_state == "unknown" and primary_error is None and client is None:
        # Some test/provider session facades own no separate transport object;
        # their successful high-level disconnect is the complete close contract.
        _IBKR_RUNTIME.release_quarantined_session(ib)
        return True

    client_disconnect = getattr(client, "disconnect", None)
    if not callable(client_disconnect):
        _retain_quarantined_ibkr_session(
            ib,
            session=session,
            session_key=session_key,
        )
        _LOGGER.warning(
            "IBKR %s session disconnect failed for %s; ownership retained",
            session,
            session_key,
            exc_info=primary_error,
        )
        raise RuntimeError(
            f"IBKR {session} session disconnect failed; ownership retained"
        ) from primary_error
    try:
        client_disconnect()
    except Exception as transport_error:
        _retain_quarantined_ibkr_session(
            ib,
            session=session,
            session_key=session_key,
        )
        _LOGGER.warning(
            "IBKR %s transport disconnect failed for %s; ownership retained",
            session,
            session_key,
            exc_info=True,
        )
        raise RuntimeError(
            f"IBKR {session} transport disconnect failed; ownership retained"
        ) from transport_error

    transport_state = _ibkr_transport_state(ib)
    if transport_state in {"closed", "unknown"}:
        # For an opaque client, successful completion of its direct disconnect
        # method is the strongest available provider-owned transport proof.
        disconnected_event = getattr(ib, "disconnectedEvent", None)
        emit_disconnected = getattr(disconnected_event, "emit", None)
        if callable(emit_disconnected):
            try:
                emit_disconnected()
            except Exception:
                _LOGGER.debug(
                    "IBKR %s disconnected event failed after transport close for %s",
                    session,
                    session_key,
                    exc_info=True,
                )
        wrapper_reset = getattr(getattr(ib, "wrapper", None), "reset", None)
        if callable(wrapper_reset):
            try:
                wrapper_reset()
            except Exception:
                _LOGGER.debug(
                    "IBKR %s wrapper reset failed after transport close for %s",
                    session,
                    session_key,
                    exc_info=True,
                )
        _IBKR_RUNTIME.release_quarantined_session(ib)
        if primary_error is not None:
            _LOGGER.warning(
                "IBKR %s partial session for %s required direct transport close",
                session,
                session_key,
                exc_info=primary_error,
            )
        return True
    if transport_state == "closing":
        _retain_quarantined_ibkr_session(
            ib,
            session=session,
            session_key=session_key,
        )
        return False

    _retain_quarantined_ibkr_session(
        ib,
        session=session,
        session_key=session_key,
    )
    _LOGGER.warning(
        "IBKR %s transport remained active after disconnect for %s; ownership retained",
        session,
        session_key,
    )
    raise RuntimeError(
        f"IBKR {session} transport remained active; ownership retained"
    ) from primary_error


async def _disconnect_quarantined_ibkr_session_on_owner(entry: Any) -> bool:
    closed = _disconnect_owned_ibkr_session(
        entry.ib,
        session=entry.session,
        session_key=entry.session_key,
    )
    if closed:
        return True
    await asyncio.sleep(0)
    return _disconnect_owned_ibkr_session(
        entry.ib,
        session=entry.session,
        session_key=entry.session_key,
    )


def _retry_quarantined_ibkr_session(entry: Any) -> bool:
    """Retry teardown on the exact event loop that owns the session transport."""

    owner_loop = entry.owner_loop
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if owner_loop is None or current_loop is owner_loop:
        return _disconnect_owned_ibkr_session(
            entry.ib,
            session=entry.session,
            session_key=entry.session_key,
        )
    if owner_loop.is_closed():
        return _disconnect_owned_ibkr_session(
            entry.ib,
            session=entry.session,
            session_key=entry.session_key,
        )
    if owner_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(
            _disconnect_quarantined_ibkr_session_on_owner(entry),
            owner_loop,
        )
        try:
            return future.result(timeout=_IBKR_QUARANTINE_RETRY_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise RuntimeError(
                "IBKR quarantined session owner-loop teardown timed out; ownership retained"
            ) from exc
    if current_loop is None:
        return owner_loop.run_until_complete(_disconnect_quarantined_ibkr_session_on_owner(entry))
    raise RuntimeError(
        "IBKR stopped owner-loop teardown requires the quarantine worker; ownership retained"
    )


def _admit_ibkr_connection_key(connection_key: tuple[str, int, int]) -> None:
    """Retry exact-key quarantine cleanup and fail closed while it remains."""

    errors: list[Exception] = []
    for entry in _IBKR_RUNTIME.quarantined_sessions_for_connection(connection_key):
        try:
            _retry_quarantined_ibkr_session(entry)
        except Exception as exc:
            errors.append(exc)
    remaining = _IBKR_RUNTIME.quarantined_sessions_for_connection(connection_key)
    if not remaining:
        return
    detail = str(errors[-1]) if errors else "transport ownership is unresolved"
    raise ConnectionError(
        "IBKR connect blocked because a quarantined session still owns "
        f"{connection_key[0]}:{connection_key[1]} clientId "
        f"{connection_key[2]}: {detail}"
    ) from (errors[-1] if errors else None)


def _abort_connected_session_initialization(
    ib: Any,
    *,
    host: str,
    port: int,
    client_id: int,
    session: str,
    error: Exception,
) -> None:
    """Close and back off one socket that failed required post-connect setup."""

    runtime = _IBKR_RUNTIME
    key = (host, int(port), int(client_id))
    detail = str(error).strip() or error.__class__.__name__
    with runtime.connect_backoff_lock:
        runtime.connect_backoff[key] = (
            time.monotonic() + _ibkr_connect_backoff_seconds(error),
            f"{session} session initialization failed: {detail}",
        )
    try:
        _disconnect_owned_ibkr_session(
            ib,
            session=session,
            session_key=key,
        )
    except Exception:
        _LOGGER.warning(
            "IBKR %s socket disconnect failed after required initialization "
            "failure for %s:%s clientId %s",
            session,
            host,
            int(port),
            int(client_id),
            exc_info=True,
        )


async def _connected_ib_async(host: str, port: int, client_id: int, readonly: bool, timeout: float):
    from aef_terminal.data.ibkr.quotes import _record_ibkr_api_error
    from ib_async import IB

    runtime = _IBKR_RUNTIME
    key = ibkr_gateway_session_key(host, port, client_id, readonly)
    if runtime.async_history_session_key == key and _ibkr_session_ready(
        runtime.async_history_session
    ):
        runtime.async_history_session.RaiseRequestErrors = True
        return runtime.async_history_session
    if runtime.async_history_session is not None:
        _reset_ibkr_async_session()
    ib = IB()
    # A request-scoped broker error must fault its exact history future.
    ib.RaiseRequestErrors = True
    runtime.set_connection_in_progress("history", True)
    try:
        await connect_without_account_sync_async(ib, host, port, client_id, timeout)
        try:
            ib.errorEvent += lambda *args: _record_ibkr_api_error(
                "history",
                *args,
            )
        except Exception as exc:
            _abort_connected_session_initialization(
                ib,
                host=host,
                port=port,
                client_id=client_id,
                session="history",
                error=exc,
            )
            raise RuntimeError(
                "IBKR history session initialization failed: required event "
                "hooks could not be registered"
            ) from exc
        runtime.async_history_session = ib
        runtime.async_history_session_key = key
        runtime.advance_history_connection_generation()
        runtime.async_history_contract_cache.clear()
        return ib
    finally:
        runtime.set_connection_in_progress("history", False)


def _reset_ibkr_async_session() -> None:
    runtime = _IBKR_RUNTIME
    _disconnect_owned_ibkr_session(
        runtime.async_history_session,
        session="history",
        session_key=runtime.async_history_session_key,
    )
    runtime.async_history_session = None
    runtime.async_history_session_key = None
    runtime.last_history_error = ""
    runtime.async_history_contract_cache.clear()


async def _connected_chart_ib_async(
    host: str, port: int, client_id: int, readonly: bool, timeout: float
):
    from aef_terminal.data.ibkr.quotes import _record_ibkr_api_error
    from ib_async import IB

    runtime = _IBKR_RUNTIME
    key = (host, int(port), int(client_id), bool(readonly))
    if runtime.async_chart_session_key == key and _ibkr_session_ready(runtime.async_chart_session):
        return runtime.async_chart_session
    if runtime.async_chart_session is not None or runtime.async_chart_streams:
        _reset_ibkr_async_chart_session()
    ib = IB()
    runtime.set_connection_in_progress("chart", True)
    try:
        await connect_without_account_sync_async(ib, host, port, client_id, timeout)
        try:
            _install_chart_error_dispatch_policy(ib)
            ib.errorEvent += lambda *args: _record_ibkr_api_error(
                "chart",
                *args,
            )
        except Exception as exc:
            _abort_connected_session_initialization(
                ib,
                host=host,
                port=port,
                client_id=client_id,
                session="chart",
                error=exc,
            )
            raise RuntimeError(
                "IBKR chart session initialization failed: required event "
                "hooks could not be registered"
            ) from exc
        runtime.async_chart_session = ib
        runtime.async_chart_session_key = key
        runtime.async_chart_contract_cache.clear()
        runtime.clear_planned_chart_cancels()
        return ib
    finally:
        runtime.set_connection_in_progress("chart", False)


def _reset_ibkr_async_chart_session() -> None:
    runtime = _IBKR_RUNTIME
    _disconnect_owned_ibkr_session(
        runtime.async_chart_session,
        session="chart",
        session_key=runtime.async_chart_session_key,
    )
    for cache_key in list(runtime.async_chart_streams):
        runtime.discard_chart_stream(cache_key, cancel=False)
    runtime.async_chart_session = None
    runtime.async_chart_session_key = None
    runtime.last_chart_sync_at = None
    runtime.last_chart_error = ""
    runtime.async_chart_streams.clear()
    runtime.async_chart_stream_consumers.clear()
    runtime.async_chart_stream_callbacks.clear()
    runtime.async_chart_stream_routes.clear()
    runtime.async_chart_contract_cache.clear()
    runtime.clear_planned_chart_cancels()


async def _connected_quote_ib_async(
    host: str, port: int, client_id: int, readonly: bool, timeout: float
):
    """Return the async-owned IBKR session for streaming quotes."""
    from aef_terminal.data.ibkr.quotes import _record_live_quote_updates, _record_quote_error
    from ib_async import IB

    runtime = _IBKR_RUNTIME
    key = (host, int(port), int(client_id), bool(readonly))
    if runtime.async_quote_session_key == key and _ibkr_session_ready(runtime.async_quote_session):
        return runtime.async_quote_session
    if runtime.async_quote_session is not None:
        _reset_ibkr_async_quote_session()
    ib = IB()
    runtime.set_connection_in_progress("quote", True)
    try:
        await connect_without_account_sync_async(ib, host, port, client_id, timeout)
        try:
            ib.errorEvent += _record_quote_error
            ib.pendingTickersEvent += _record_live_quote_updates
        except Exception as exc:
            _abort_connected_session_initialization(
                ib,
                host=host,
                port=port,
                client_id=client_id,
                session="quote",
                error=exc,
            )
            raise RuntimeError(
                "IBKR quote session initialization failed: required event hooks "
                "could not be registered"
            ) from exc
        runtime.async_quote_session = ib
        runtime.async_quote_session_key = key
        runtime.async_ticker_cache.clear()
        runtime.async_quote_ticker_routes.clear()
        runtime.clear_quote_snapshots()
        with runtime.live_quote_trade_lock:
            runtime.live_quote_trade_signatures.clear()
        runtime.async_quote_contract_cache.clear()
        runtime.async_option_quote_keys.clear()
        runtime.quote_errors.clear()
        runtime.last_quote_error = None
        return ib
    finally:
        runtime.set_connection_in_progress("quote", False)


async def _connected_option_quote_ib_async(
    host: str, port: int, client_id: int, readonly: bool, timeout: float
):
    """Return the async-owned IBKR session for live option point quotes."""
    from aef_terminal.data.ibkr.quotes import (
        _market_data_type,
        _record_bbo_observation_updates,
        _record_quote_error,
    )
    from ib_async import IB

    runtime = _IBKR_RUNTIME
    key = (host, int(port), int(client_id), bool(readonly))
    requested_market_data_type = _market_data_type()
    if (
        runtime.async_option_quote_session_key == key
        and _ibkr_session_ready(runtime.async_option_quote_session)
        and getattr(
            runtime.async_option_quote_session,
            "_aef_requested_market_data_type",
            None,
        )
        == requested_market_data_type
    ):
        return runtime.async_option_quote_session
    if runtime.async_option_quote_session is not None:
        _reset_ibkr_async_option_quote_session()
    ib = IB()
    runtime.set_connection_in_progress("option_quote", True)
    try:
        await connect_without_account_sync_async(ib, host, port, client_id, timeout)
        try:
            ib.errorEvent += _record_quote_error
            ib.pendingTickersEvent += _record_bbo_observation_updates
        except Exception as exc:
            _abort_connected_session_initialization(
                ib,
                host=host,
                port=port,
                client_id=client_id,
                session="option_quote",
                error=exc,
            )
            raise RuntimeError(
                "IBKR option-quote session initialization failed: required event "
                "hooks could not be registered"
            ) from exc
        runtime.async_option_quote_session = ib
        runtime.async_option_quote_session_key = key
        runtime.async_option_ticker_cache.clear()
        runtime.async_option_ticker_consumers.clear()
        runtime.async_option_quote_contract_cache.clear()
        runtime.async_option_board_sessions.clear()
        return ib
    finally:
        runtime.set_connection_in_progress("option_quote", False)


def connect_without_account_sync(ib, host: str, port: int, client_id: int, timeout: float):
    """Open a market-data-only IBKR socket without ib_async account sync."""
    from aef_terminal.data.ibkr.quotes import _market_data_type

    key = (host, int(port), int(client_id))
    runtime = _IBKR_RUNTIME
    with runtime.connect_backoff_lock:
        retry_at, last_error = runtime.connect_backoff.get(key, (0.0, ""))
    now = time.monotonic()
    if now < retry_at:
        retry_in = max(retry_at - now, 0.0)
        raise ConnectionError(
            f"IBKR connect backoff active for {host}:{int(port)} clientId {int(client_id)}; retry in {retry_in:.1f}s"
            + (f" after: {last_error}" if last_error else "")
        )
    _admit_ibkr_connection_key(key)
    _bind_ibkr_session_owner(ib, key)
    ib.wrapper.clientId = int(client_id)
    try:
        ib.client.connect(host, int(port), clientId=int(client_id), timeout=timeout)
    except Exception as exc:
        with runtime.connect_backoff_lock:
            runtime.connect_backoff[key] = (
                time.monotonic() + _ibkr_connect_backoff_seconds(exc),
                str(exc),
            )
        try:
            _disconnect_owned_ibkr_session(
                ib,
                session="market_data_connect",
                session_key=key,
            )
        except Exception:
            _LOGGER.warning(
                "IBKR failed sync connection was quarantined for %s",
                key,
                exc_info=True,
            )
        raise
    with runtime.connect_backoff_lock:
        runtime.connect_backoff.pop(key, None)
    requested_market_data_type = _market_data_type()
    try:
        ib.reqMarketDataType(requested_market_data_type)
        ib._aef_requested_market_data_type = requested_market_data_type
    except Exception as exc:
        with runtime.connect_backoff_lock:
            runtime.connect_backoff[key] = (
                time.monotonic() + _ibkr_connect_backoff_seconds(exc),
                str(exc),
            )
        try:
            _disconnect_owned_ibkr_session(
                ib,
                session="market_data",
                session_key=key,
            )
        except Exception:
            _LOGGER.warning(
                "IBKR socket disconnect failed after market-data-type request failure "
                "for %s:%s clientId %s",
                host,
                int(port),
                int(client_id),
                exc_info=True,
            )
        raise
    _cancel_account_streams(ib)
    return ib


async def connect_without_account_sync_async(
    ib, host: str, port: int, client_id: int, timeout: float
):
    """Async market-data-only IBKR socket open without ib_async account sync."""
    from aef_terminal.data.ibkr.quotes import _market_data_type

    key = (host, int(port), int(client_id))
    runtime = _IBKR_RUNTIME
    with runtime.connect_backoff_lock:
        retry_at, last_error = runtime.connect_backoff.get(key, (0.0, ""))
    now = time.monotonic()
    if now < retry_at:
        retry_in = max(retry_at - now, 0.0)
        raise ConnectionError(
            f"IBKR connect backoff active for {host}:{int(port)} clientId {int(client_id)}; retry in {retry_in:.1f}s"
            + (f" after: {last_error}" if last_error else "")
        )
    admission_task = asyncio.create_task(asyncio.to_thread(_admit_ibkr_connection_key, key))
    admission_outcome = await settle_physical_task(admission_task)
    if admission_outcome.cancellation is not None:
        raise admission_outcome.cancellation
    if admission_outcome.task_cancelled:
        raise asyncio.CancelledError()
    if admission_outcome.error is not None:
        raise admission_outcome.error
    _bind_ibkr_session_owner(ib, key)
    ib.wrapper.clientId = int(client_id)
    try:
        await asyncio.wait_for(
            ib.client.connectAsync(host, int(port), clientId=int(client_id), timeout=timeout),
            timeout=max(float(timeout) + 1.0, 2.0),
        )
    except asyncio.CancelledError:
        try:
            _disconnect_owned_ibkr_session(
                ib,
                session="market_data_connect_async",
                session_key=key,
            )
        except Exception:
            _LOGGER.warning(
                "IBKR cancelled async connection was quarantined for %s",
                key,
                exc_info=True,
            )
        raise
    except Exception as exc:
        with runtime.connect_backoff_lock:
            runtime.connect_backoff[key] = (
                time.monotonic() + _ibkr_connect_backoff_seconds(exc),
                str(exc),
            )
        try:
            _disconnect_owned_ibkr_session(
                ib,
                session="market_data_connect_async",
                session_key=key,
            )
        except Exception:
            _LOGGER.warning(
                "IBKR failed async connection was quarantined for %s",
                key,
                exc_info=True,
            )
        raise
    with runtime.connect_backoff_lock:
        runtime.connect_backoff.pop(key, None)
    requested_market_data_type = _market_data_type()
    try:
        ib.reqMarketDataType(requested_market_data_type)
        ib._aef_requested_market_data_type = requested_market_data_type
    except Exception as exc:
        with runtime.connect_backoff_lock:
            runtime.connect_backoff[key] = (
                time.monotonic() + _ibkr_connect_backoff_seconds(exc),
                str(exc),
            )
        try:
            _disconnect_owned_ibkr_session(
                ib,
                session="market_data_async",
                session_key=key,
            )
        except Exception:
            _LOGGER.warning(
                "IBKR async socket disconnect failed after market-data-type request failure "
                "for %s:%s clientId %s",
                host,
                int(port),
                int(client_id),
                exc_info=True,
            )
        raise
    _cancel_account_streams(ib)
    return ib


def _is_ibkr_client_id_in_use_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return "client id" in message and "already in use" in message


def _ibkr_connect_backoff_seconds(exc: Exception) -> float:
    if _is_ibkr_client_id_in_use_error(exc):
        return _IBKR_CLIENT_ID_IN_USE_BACKOFF_SECONDS
    return _IBKR_CONNECT_BACKOFF_SECONDS


def _cancel_account_streams(ib) -> None:
    """Defensively stop account streams on market-data-only sockets."""
    cancel_updates = getattr(ib, "reqAccountUpdates", None)
    if callable(cancel_updates):
        try:
            cancel_updates(False, "")
        except Exception:
            _LOGGER.debug(
                "IBKR defensive account-stream cancellation failed for clientId %s",
                getattr(getattr(ib, "wrapper", None), "clientId", None),
                exc_info=True,
            )


def _reset_ibkr_async_quote_session() -> None:
    runtime = _IBKR_RUNTIME
    _disconnect_owned_ibkr_session(
        runtime.async_quote_session,
        session="quote",
        session_key=runtime.async_quote_session_key,
    )
    runtime.async_quote_session = None
    runtime.async_quote_session_key = None
    runtime.last_quote_error = None
    runtime.last_quote_sync_at = None
    runtime.last_quote_wanted_keys = set()
    for ticker in runtime.async_ticker_cache.values():
        runtime.discard_ticker_bbo_observation(ticker)
    runtime.async_ticker_cache.clear()
    runtime.async_quote_ticker_routes.clear()
    runtime.clear_quote_snapshots()
    runtime.async_quote_contract_cache.clear()
    runtime.async_option_quote_keys.clear()


def _reset_ibkr_async_option_quote_session() -> None:
    runtime = _IBKR_RUNTIME
    _disconnect_owned_ibkr_session(
        runtime.async_option_quote_session,
        session="option_quote",
        session_key=runtime.async_option_quote_session_key,
    )
    runtime.async_option_quote_session = None
    runtime.async_option_quote_session_key = None
    for ticker in runtime.async_option_ticker_cache.values():
        runtime.discard_ticker_bbo_observation(ticker)
    runtime.async_option_ticker_cache.clear()
    runtime.async_option_ticker_consumers.clear()
    runtime.async_option_quote_contract_cache.clear()
    runtime.async_option_board_sessions.clear()


def _reset_ibkr_quarantined_sessions() -> None:
    failures: list[Exception] = []
    for entry in _IBKR_RUNTIME.quarantined_sessions_snapshot():
        try:
            if not _retry_quarantined_ibkr_session(entry):
                failures.append(
                    RuntimeError("IBKR quarantined transport is still closing; ownership retained")
                )
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup(
            "IBKR quarantined session teardown failed",
            failures,
        )


async def cleanup_quarantined_sessions_async(
    *,
    timeout: float = 2.5,
) -> dict[str, object]:
    """Close quarantined transports without disturbing unrelated live lanes."""

    runtime = _IBKR_RUNTIME
    before = runtime.quarantined_sessions_snapshot()
    bounded_timeout = max(0.5, min(float(timeout), 8.0))
    cleanup_task = asyncio.create_task(asyncio.to_thread(_reset_ibkr_quarantined_sessions))
    cleanup_outcome = await settle_physical_task(
        cleanup_task,
        timeout=bounded_timeout,
    )
    if cleanup_outcome.cancellation is not None:
        raise cleanup_outcome.cancellation
    if cleanup_outcome.task_cancelled:
        raise asyncio.CancelledError()
    if cleanup_outcome.error is not None:
        raise cleanup_outcome.error
    after = runtime.quarantined_sessions_snapshot()
    return {
        "ok": not after,
        "timed_out": cleanup_outcome.timed_out,
        "quarantined_before": len(before),
        "quarantined_after": len(after),
        "sessions": sorted({entry.session for entry in before}),
    }


def disconnect_all_sessions(timeout: float = 2.0) -> dict[str, object]:
    """Disconnect all process-owned IBKR sessions without opening new sockets."""
    from aef_terminal.data.ibkr.event_loop import _reset_ibkr_async_sessions_threadsafe
    from aef_terminal.data.ibkr.quotes import runtime_status

    bounded_timeout = max(0.2, min(float(timeout), 5.0))
    _reset_ibkr_async_sessions_threadsafe(timeout=bounded_timeout)
    runtime = _IBKR_RUNTIME
    runtime.last_quote_wanted_keys = set()
    runtime.async_ticker_cache.clear()
    runtime.async_quote_ticker_routes.clear()
    runtime.clear_quote_snapshots()
    runtime.async_option_quote_keys.clear()
    runtime.async_option_ticker_cache.clear()
    runtime.async_option_ticker_consumers.clear()
    runtime.async_option_quote_contract_cache.clear()
    runtime.async_option_board_sessions.clear()
    runtime.async_option_market_data_sources.clear()
    runtime.async_chart_streams.clear()
    runtime.async_chart_stream_consumers.clear()
    runtime.async_chart_stream_callbacks.clear()
    runtime.async_chart_stream_routes.clear()
    return runtime_status()


async def _force_reconnect_owned(
    *, quote_instruments: Sequence[Any] | None = None, timeout: float = 2.5
) -> dict[str, object]:
    """Drop IBKR sockets and reopen the UI-owned async market-data sessions."""
    from aef_terminal.data.gex.live import async_stop_live_gex_context
    from aef_terminal.data.ibkr.event_loop import _reset_ibkr_async_sessions_async
    from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
    from aef_terminal.data.ibkr.quotes import (
        _quote_inputs,
        runtime_status,
        sync_quote_subscriptions_async,
    )

    config = AppConfig()
    bounded_timeout = max(0.5, min(float(timeout), 8.0))
    await async_stop_live_gex_context(
        provider_runtime=ibkr_gex_provider_runtime,
    )
    await _reset_ibkr_async_sessions_async(timeout=bounded_timeout)
    runtime = _IBKR_RUNTIME
    runtime.reset_connection_backoff()
    runtime.api_errors.clear()
    runtime.quote_errors.clear()
    sessions: dict[str, dict[str, object]] = {}

    async def attempt(name: str, callback) -> None:
        try:
            await callback()
            sessions[name] = {"ok": True}
        except Exception as exc:
            sessions[name] = {"ok": False, "error": str(exc) or exc.__class__.__name__}

    await attempt(
        "history",
        lambda: ibkr_market_data_manager.run_coroutine(
            "history",
            "reconnect history",
            lambda: _connected_ib_async(
                config.ibkr_host,
                config.ibkr_port,
                config.ibkr_history_client_id,
                config.ibkr_readonly,
                bounded_timeout,
            ),
            timeout=max(bounded_timeout + 3.0, 4.0),
        ),
    )
    quote_items = list(quote_instruments or [])
    normalized_quote_routes, _quote_instruments_by_route = _quote_inputs(quote_items)
    if normalized_quote_routes:
        await attempt(
            "quotes",
            lambda: sync_quote_subscriptions_async(quote_items, bounded_timeout),
        )
    else:
        await attempt(
            "quotes",
            lambda: ibkr_market_data_manager.run_coroutine(
                "quote",
                "reconnect quotes",
                lambda: _connected_quote_ib_async(
                    config.ibkr_host,
                    config.ibkr_port,
                    config.ibkr_quote_client_id,
                    config.ibkr_readonly,
                    bounded_timeout,
                ),
                timeout=max(bounded_timeout + 3.0, 4.0),
            ),
        )
    await attempt(
        "chart",
        lambda: ibkr_market_data_manager.run_coroutine(
            "chart",
            "reconnect chart",
            lambda: _connected_chart_ib_async(
                config.ibkr_host,
                config.ibkr_port,
                config.ibkr_chart_client_id,
                config.ibkr_readonly,
                bounded_timeout,
            ),
            timeout=max(bounded_timeout + 3.0, 4.0),
        ),
    )
    return {
        "ok": all((item.get("ok") for item in sessions.values())),
        "host": config.ibkr_host,
        "port": config.ibkr_port,
        "sessions": sessions,
        "quote_routes": normalized_quote_routes,
        "status": runtime_status(),
    }


async def force_reconnect_async(
    *, quote_instruments: Sequence[Any] | None = None, timeout: float = 2.5
) -> dict[str, object]:
    """Settle the complete reconnect transition before exposing cancellation."""

    return await run_cancellation_deferred(
        _force_reconnect_owned(
            quote_instruments=quote_instruments,
            timeout=timeout,
        ),
        task_cancelled_error="IBKR_FORCE_RECONNECT_TASK_CANCELLED",
    )
