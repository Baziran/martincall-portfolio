from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from aef_terminal.data.provider_contract import (
    OptionUniverseUnavailableError,
    require_option_expiry_mode,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.paper_contract import PaperContractIdentity
from aef_terminal.runtime.async_tasks import (
    run_cancellation_deferred,
    run_physical_thread_call,
)
from aef_terminal.ui.services.websocket_send import send_stream_json


OPTION_BOARD_STREAM_FRAME_SECONDS = 0.5
OPTION_BOARD_STREAM_ROUTE_CHECK_SECONDS = 30.0
OPTION_BOARD_STREAM_RETRY_SECONDS = 3.0
OPTION_BOARD_STREAM_NO_CHAIN_RETRY_SECONDS = 30.0
OPTION_BOARD_STREAM_ROLLOVER_RETRY_SECONDS = 1.0
_OPTION_BOARD_PRODUCERS: dict[
    tuple[str, str, str],
    "_OptionBoardProducer",
] = {}


@dataclass(frozen=True)
class OptionBoardWsDeps:
    apply_provider_runtime_settings_async: Callable[
        [],
        Awaitable[dict[str, Any]],
    ]
    server_sleeping: Callable[[], bool]
    websocket_heartbeat_seconds: float
    lookup_runtime_instrument: Callable[[str], dict[str, Any]]
    store_factory: Callable[[], Any] | None = None


@dataclass
class _OptionBoardProducer:
    key: tuple[str, str, str]
    instrument_id: str
    provider_symbol: str
    expiry_mode: str
    route: Any
    consumer_id: str
    consumers: int = 0
    revision: int = 0
    snapshot: dict[str, Any] | None = None
    status: dict[str, Any] = field(default_factory=dict)
    status_revision: int = 0
    retry_at: float = 0.0
    route_checked_at: float = field(default_factory=time.monotonic)
    option_expiry_facts: tuple[Any, ...] = ()
    option_expiry_facts_loaded: bool = False
    closing: bool = False
    deps: OptionBoardWsDeps | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)


def _option_board_consumer_id(key: tuple[str, str, str]) -> str:
    return require_exact_identity_text(
        f"option-board:{json.dumps(list(key), separators=(',', ':'))}",
        field="OPTION_BOARD_CONSUMER_ID",
    )


def resolve_option_board_paper_contract(
    *,
    instrument_id: str,
    route_fingerprint: str,
    expiry_mode: str,
    snapshot_revision: object,
    con_id: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    identity = require_exact_identity_text(
        instrument_id,
        field="OPTION_BOARD_ORDER_INSTRUMENT_ID",
    )
    fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="OPTION_BOARD_ORDER_ROUTE_FINGERPRINT",
    )
    exact_expiry_mode = require_option_expiry_mode(expiry_mode)
    if (
        isinstance(snapshot_revision, bool)
        or not isinstance(snapshot_revision, int)
        or snapshot_revision <= 0
    ):
        raise ValueError("OPTION_BOARD_ORDER_REVISION_INVALID")
    if isinstance(con_id, bool) or not isinstance(con_id, int) or con_id <= 0:
        raise ValueError("OPTION_BOARD_ORDER_CON_ID_INVALID")
    producer = _OPTION_BOARD_PRODUCERS.get((identity, fingerprint, exact_expiry_mode))
    if producer is None or producer.closing:
        raise ValueError("OPTION_BOARD_ORDER_SNAPSHOT_UNAVAILABLE")
    current_revision = producer.revision
    snapshot = producer.snapshot
    if (
        not isinstance(snapshot, dict)
        or current_revision <= 0
        or snapshot_revision > current_revision
        or snapshot.get("instrument_id") != identity
        or snapshot.get("route_fingerprint") != fingerprint
        or snapshot.get("expiry_mode") != exact_expiry_mode
    ):
        raise ValueError("OPTION_BOARD_ORDER_SNAPSHOT_STALE")
    selected: dict[str, Any] | None = None
    for series in snapshot.get("series") or ():
        if not isinstance(series, dict):
            continue
        if series.get("target_dte") != exact_expiry_mode:
            continue
        for row in series.get("rows") or ():
            if not isinstance(row, dict):
                continue
            for side_key, right in (("call", "C"), ("put", "P")):
                quote = row.get(side_key)
                if not isinstance(quote, dict) or quote.get("con_id") != con_id:
                    continue
                if quote.get("right") != right or quote.get("strike") != row.get("strike"):
                    raise ValueError("OPTION_BOARD_ORDER_CONTRACT_CONTRADICTORY")
                selected = quote
                break
            if selected is not None:
                break
        if selected is not None:
            break
    if selected is None:
        raise ValueError("OPTION_BOARD_ORDER_CONTRACT_NOT_CURRENT")
    contract = PaperContractIdentity(
        scope_kind="option",
        provider=str(producer.route.provider),
        provider_contract_id=str(con_id),
        sec_type=selected.get("sec_type"),
        con_id=selected.get("con_id"),
        exchange=selected.get("exchange"),
        expiry=selected.get("expiry"),
        expiry_at=selected.get("expiry_at"),
        strike=selected.get("strike"),
        right=selected.get("right"),
        trading_class=selected.get("trading_class"),
        multiplier=selected.get("multiplier"),
        currency=selected.get("currency"),
    )
    current = now or datetime.now(tz=UTC)
    expiry_at = datetime.fromisoformat(str(contract.expiry_at).replace("Z", "+00:00")).astimezone(
        UTC
    )
    if expiry_at <= current.astimezone(UTC):
        raise ValueError("OPTION_BOARD_ORDER_CONTRACT_EXPIRED")
    if producer.revision != current_revision or producer.snapshot is not snapshot:
        raise ValueError("OPTION_BOARD_ORDER_SNAPSHOT_CHANGED")
    return {
        "contract": contract,
        "quote": dict(selected),
        "provider_symbol": producer.provider_symbol,
        "expiry_mode": exact_expiry_mode,
        "requested_revision": snapshot_revision,
        "resolved_revision": current_revision,
        "captured_at": snapshot.get("captured_at"),
    }


def _option_board_status(
    producer: _OptionBoardProducer,
    *,
    status: str,
    message: str,
    retryable: bool,
    reason: str = "",
    diagnostics: dict[str, Any] | None = None,
    retry_at: datetime | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": status in {"loading", "waiting", "partial", "ready"},
        "status": status,
        "message": message,
        "retryable": retryable,
        "provider_symbol": producer.provider_symbol,
        "instrument_id": producer.instrument_id,
        "route_fingerprint": producer.route.fingerprint,
        "expiry_mode": producer.expiry_mode,
    }
    if reason:
        payload["reason"] = reason
        payload["diagnostics"] = dict(diagnostics or {})
    if retry_at is not None:
        payload["retry_at"] = retry_at.astimezone(UTC).isoformat()
    return payload


def _set_option_board_status(
    producer: _OptionBoardProducer,
    payload: dict[str, Any],
) -> None:
    if producer.status == payload:
        return
    producer.status = payload
    producer.status_revision += 1


async def _verify_option_board_route(
    producer: _OptionBoardProducer,
    deps: OptionBoardWsDeps,
) -> None:
    now = time.monotonic()
    if now - producer.route_checked_at < OPTION_BOARD_STREAM_ROUTE_CHECK_SECONDS:
        return
    instrument = deps.lookup_runtime_instrument(producer.instrument_id)
    current = route_instrument(instrument)
    if current.fingerprint != producer.route.fingerprint:
        raise RuntimeError(
            "OPTION_BOARD_ROUTE_CHANGED "
            f"expected={producer.route.fingerprint} "
            f"actual={current.fingerprint}"
        )
    producer.route_checked_at = now


async def _run_option_board_producer(
    producer: _OptionBoardProducer,
    deps: OptionBoardWsDeps,
) -> None:
    _set_option_board_status(
        producer,
        _option_board_status(
            producer,
            status="loading",
            message="Qualifying the exact provider option universe.",
            retryable=True,
        ),
    )
    try:
        while True:
            if deps.server_sleeping():
                _set_option_board_status(
                    producer,
                    _option_board_status(
                        producer,
                        status="sleeping",
                        message="Server sleeping: Option Board subscriptions are paused.",
                        retryable=True,
                    ),
                )
                await asyncio.sleep(OPTION_BOARD_STREAM_FRAME_SECONDS)
                continue
            await _verify_option_board_route(producer, deps)
            now = time.monotonic()
            if now < producer.retry_at:
                await asyncio.sleep(
                    min(
                        OPTION_BOARD_STREAM_FRAME_SECONDS,
                        producer.retry_at - now,
                    )
                )
                continue
            try:
                if (
                    producer.route.provider == "ibkr"
                    and deps.store_factory is not None
                    and not producer.option_expiry_facts_loaded
                ):
                    from aef_terminal.data.ibkr.option_contracts import (
                        read_persisted_ibkr_option_expiry_facts,
                    )

                    store = await run_physical_thread_call(deps.store_factory)
                    producer.option_expiry_facts = await run_physical_thread_call(
                        read_persisted_ibkr_option_expiry_facts,
                        instrument_id=producer.instrument_id,
                        route_fingerprint=producer.route.fingerprint,
                        store=store,
                    )
                    producer.option_expiry_facts_loaded = True
                snapshot = await producer.route.adapter.async_load_option_board(
                    producer.route.instrument,
                    consumer_id=producer.consumer_id,
                    expiry_mode=producer.expiry_mode,
                    **(
                        {"option_expiry_facts": (producer.option_expiry_facts)}
                        if producer.option_expiry_facts_loaded
                        else {}
                    ),
                )
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("instrument_id") != producer.instrument_id
                    or snapshot.get("route_fingerprint") != producer.route.fingerprint
                    or snapshot.get("expiry_mode") != producer.expiry_mode
                    or not isinstance(snapshot.get("series"), list)
                ):
                    raise ValueError(
                        "Option Board provider returned an invalid exact-route snapshot"
                    )
                snapshot = {key: value for key, value in snapshot.items() if key != "consumer_id"}
                producer.snapshot = snapshot
                producer.revision += 1
                producer.retry_at = 0.0
                _set_option_board_status(
                    producer,
                    _option_board_status(
                        producer,
                        status=str(snapshot.get("status") or "ready"),
                        message="",
                        retryable=True,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except OptionUniverseUnavailableError as exc:
                message = str(exc).strip() or exc.__class__.__name__
                reason = exc.reason
                retry_seconds = (
                    OPTION_BOARD_STREAM_ROLLOVER_RETRY_SECONDS
                    if reason == "OPTION_UNIVERSE_ROLLOVER"
                    else OPTION_BOARD_STREAM_NO_CHAIN_RETRY_SECONDS
                )
                producer.snapshot = None
                if reason == "OPTION_UNIVERSE_ROLLOVER":
                    producer.option_expiry_facts_loaded = False
                producer.retry_at = time.monotonic() + retry_seconds
                retry_at = datetime.now(tz=UTC) + timedelta(seconds=retry_seconds)
                _set_option_board_status(
                    producer,
                    _option_board_status(
                        producer,
                        status="unavailable",
                        message=message,
                        retryable=True,
                        reason=reason,
                        diagnostics=dict(exc.diagnostics),
                        retry_at=retry_at,
                    ),
                )
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                producer.snapshot = None
                producer.retry_at = time.monotonic() + OPTION_BOARD_STREAM_RETRY_SECONDS
                _set_option_board_status(
                    producer,
                    _option_board_status(
                        producer,
                        status="error",
                        message=message,
                        retryable=True,
                    ),
                )
            await asyncio.sleep(OPTION_BOARD_STREAM_FRAME_SECONDS)
    finally:
        producer.snapshot = None


def _acquire_option_board_producer(
    *,
    route: Any,
    expiry_mode: str,
    deps: OptionBoardWsDeps,
) -> _OptionBoardProducer:
    key = (
        require_exact_identity_text(
            route.instrument_id,
            field="OPTION_BOARD_INSTRUMENT_ID",
        ),
        require_exact_identity_text(
            route.fingerprint,
            field="OPTION_BOARD_ROUTE_FINGERPRINT",
        ),
        require_option_expiry_mode(expiry_mode),
    )
    producer = _OPTION_BOARD_PRODUCERS.get(key)
    if producer is None:
        producer = _OptionBoardProducer(
            key=key,
            instrument_id=route.instrument_id,
            provider_symbol=route.provider_symbol,
            expiry_mode=expiry_mode,
            route=route,
            consumer_id=_option_board_consumer_id(key),
            deps=deps,
        )
        _set_option_board_status(
            producer,
            _option_board_status(
                producer,
                status="loading",
                message="Qualifying the exact provider option universe.",
                retryable=True,
            ),
        )
        producer.task = asyncio.create_task(
            _run_option_board_producer(producer, deps),
            name=f"option-board-producer:{route.provider_symbol}",
        )
        _OPTION_BOARD_PRODUCERS[key] = producer
    producer.consumers += 1
    return producer


async def _release_option_board_producer(
    producer: _OptionBoardProducer,
) -> None:
    await run_cancellation_deferred(
        _release_option_board_producer_owned(producer),
        task_cancelled_error="OPTION_BOARD_RELEASE_TASK_CANCELLED",
    )


async def _release_option_board_producer_owned(
    producer: _OptionBoardProducer,
) -> None:
    if producer.consumers <= 0:
        raise RuntimeError("Option Board producer consumer count underflow")
    producer.consumers -= 1
    if producer.consumers or _OPTION_BOARD_PRODUCERS.get(producer.key) is not producer:
        return
    producer.closing = True
    if producer.task is not None:
        producer.task.cancel()
        await asyncio.gather(producer.task, return_exceptions=True)
        producer.task = None
    try:
        if producer.consumers == 0:
            await producer.route.adapter.async_stop_option_board(
                producer.route.instrument,
                consumer_id=producer.consumer_id,
            )
    finally:
        if producer.consumers:
            if producer.deps is None:
                raise RuntimeError(
                    "Option Board producer dependencies are unavailable during reconnect"
                )
            producer.task = asyncio.create_task(
                _run_option_board_producer(producer, producer.deps),
                name=f"option-board-producer:{producer.provider_symbol}",
            )
        elif _OPTION_BOARD_PRODUCERS.get(producer.key) is producer:
            _OPTION_BOARD_PRODUCERS.pop(producer.key, None)
        producer.closing = False


async def run_option_board_stream(
    websocket: WebSocket,
    *,
    instrument_id: str,
    expected_route_fingerprint: str,
    expiry_mode: str,
    deps: OptionBoardWsDeps,
) -> None:
    await websocket.accept()
    instrument = deps.lookup_runtime_instrument(instrument_id)
    route = route_instrument(instrument)
    expected_route = require_exact_identity_text(
        expected_route_fingerprint,
        field="OPTION_BOARD_EXPECTED_ROUTE_FINGERPRINT",
    )
    exact_expiry_mode = require_option_expiry_mode(expiry_mode)
    if route.fingerprint != expected_route:
        raise ValueError(
            f"OPTION_BOARD_ROUTE_CHANGED expected={expected_route} actual={route.fingerprint}"
        )
    if not route.adapter.capabilities.options:
        await send_stream_json(
            websocket,
            {
                "type": "option_board_status",
                "ok": False,
                "status": "unsupported",
                "provider_symbol": route.provider_symbol,
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "expiry_mode": exact_expiry_mode,
                "message": (f"Options are not supported by provider {route.provider}."),
            },
            "option-board",
        )
        return
    await deps.apply_provider_runtime_settings_async()
    producer = _acquire_option_board_producer(
        route=route,
        expiry_mode=exact_expiry_mode,
        deps=deps,
    )
    last_revision = -1
    last_status_revision = -1
    idle_seconds = 0.0
    try:
        while True:
            if deps.server_sleeping():
                await send_stream_json(
                    websocket,
                    {
                        "type": "option_board_status",
                        "ok": False,
                        "status": "sleeping",
                        "provider_symbol": route.provider_symbol,
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                        "expiry_mode": exact_expiry_mode,
                        "message": "Server sleeping: Option Board stream closed.",
                    },
                    "option-board",
                )
                return
            if producer.task is not None and producer.task.done():
                await producer.task
                return
            sent = False
            if producer.status_revision != last_status_revision:
                await send_stream_json(
                    websocket,
                    {
                        "type": "option_board_status",
                        **producer.status,
                        "status_revision": producer.status_revision,
                    },
                    "option-board",
                )
                last_status_revision = producer.status_revision
                sent = True
            if producer.snapshot is not None and producer.revision != last_revision:
                await send_stream_json(
                    websocket,
                    {
                        "type": "option_board_snapshot",
                        **producer.snapshot,
                        "revision": producer.revision,
                    },
                    "option-board",
                )
                last_revision = producer.revision
                sent = True
            if sent:
                idle_seconds = 0.0
            else:
                idle_seconds += OPTION_BOARD_STREAM_FRAME_SECONDS
                if idle_seconds >= deps.websocket_heartbeat_seconds:
                    await send_stream_json(
                        websocket,
                        {
                            "type": "option_board_heartbeat",
                            "ok": True,
                            "provider_symbol": route.provider_symbol,
                            "instrument_id": route.instrument_id,
                            "route_fingerprint": route.fingerprint,
                            "expiry_mode": exact_expiry_mode,
                            "revision": max(last_revision, 0),
                        },
                        "option-board",
                    )
                    idle_seconds = 0.0
            await asyncio.sleep(OPTION_BOARD_STREAM_FRAME_SECONDS)
    except WebSocketDisconnect:
        pass
    finally:
        await _release_option_board_producer(producer)
