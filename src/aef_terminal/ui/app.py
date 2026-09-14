import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from aef_terminal.storage.postgres import default_postgres_store
from aef_terminal.ui import app_router_composition
from aef_terminal.ui import app_runtime_composition
from aef_terminal.ui.bootstrap import start_server_alert_monitor, stop_server_alert_monitor
from aef_terminal.ui import ibkr_runtime as _ibkr_runtime
from aef_terminal.ui.runtime import quote_stream
from aef_terminal.ui import system_status_runtime
from aef_terminal.runtime.async_tasks import settle_physical_task


async def _settled_physical_task(
    task: asyncio.Future[Any],
    *,
    completed: Callable[[], None] | None = None,
) -> Any:
    """Settle physical work before exposing cancellation or its outcome."""

    outcome = await settle_physical_task(task)
    if outcome.error is not None:
        raise outcome.error
    if outcome.task_cancelled:
        if outcome.cancellation is not None:
            raise outcome.cancellation
        raise RuntimeError("STARTUP_PHYSICAL_TASK_CANCELLED")
    if completed is not None:
        completed()
    if outcome.cancellation is not None:
        raise outcome.cancellation
    return outcome.result


async def _settled_startup_thread_call(
    operation: Callable[..., Any],
    /,
    *args: Any,
    completed: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Settle one physical startup call before exposing cancellation."""

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    return await _settled_physical_task(task, completed=completed)


async def _settled_runtime_call(
    operation: Callable[..., Awaitable[Any]],
    /,
    *args: Any,
    completed: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Settle runtime startup/drain before changing writer-lease ownership."""

    task = asyncio.create_task(operation(*args, **kwargs))
    return await _settled_physical_task(task, completed=completed)


@asynccontextmanager
async def martincall_lifespan(_app: FastAPI):
    info = _RUNTIME.build
    logging.getLogger(__name__).info(
        "MartinCall %s (git %s) image built %s",
        info.get("version"),
        info.get("git_sha"),
        info.get("built_at") or "local/dev",
    )
    store = default_postgres_store()
    writer_lease_acquired = False
    runtime_start_attempted = False
    runtime_drained = False

    def mark_writer_lease_acquired() -> None:
        nonlocal writer_lease_acquired
        writer_lease_acquired = True

    def mark_runtime_drained() -> None:
        nonlocal runtime_drained
        runtime_drained = True

    try:
        if store is not None:
            await _settled_startup_thread_call(
                store.acquire_canonical_writer_lease,
                owner="martincall-app",
                completed=mark_writer_lease_acquired,
            )
            await _settled_startup_thread_call(store.initialize)
            await _settled_startup_thread_call(store.verify_canonical_writer_lease)
            await _settled_startup_thread_call(
                quote_stream.refresh_routes_from_store,
                store,
                route_generation_changed=True,
            )
            await _settled_startup_thread_call(store.verify_canonical_writer_lease)
        await _settled_startup_thread_call(_ibkr_runtime.apply_persisted_ibkr_runtime_settings)
        runtime_start_attempted = True
        await _settled_runtime_call(start_server_alert_monitor)
        yield
    finally:
        try:
            if runtime_start_attempted:
                await _settled_runtime_call(
                    stop_server_alert_monitor,
                    completed=mark_runtime_drained,
                )
            else:
                runtime_drained = True
        finally:
            if store is not None and writer_lease_acquired and runtime_drained:
                await _settled_startup_thread_call(store.release_canonical_writer_lease)
            elif store is not None and writer_lease_acquired:
                logging.getLogger(__name__).critical(
                    "Canonical writer lease retained because runtime drain did not complete"
                )


app = FastAPI(title="MartinCall", lifespan=martincall_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
_LOGGER = logging.getLogger(__name__)

STARTED_AT = datetime.now(tz=UTC)


def _store_factory():
    return default_postgres_store()


_RUNTIME = app_runtime_composition.configure_default_runtime(
    logger=_LOGGER,
    started_at=STARTED_AT,
    store_factory=_store_factory,
)

app_runtime_status = system_status_runtime.app_runtime_status

app_router_composition.wire_default_app_routers(
    app,
    logger=_LOGGER,
    store_factory=_store_factory,
    set_server_sleeping=_RUNTIME.set_server_sleeping,
    flush_log_handlers=_RUNTIME.flush_log_handlers,
)
