from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aef_terminal.build_info import build_info
from aef_terminal.ui.app_runtime_wiring import (
    configure_background_loops,
    configure_backend_restart,
    configure_bootstrap,
    configure_indicator_module_services,
    configure_runtime_state,
    configure_runtime_callbacks,
    configure_system_status,
    flush_log_handlers as flush_runtime_log_handlers,
    set_server_sleeping_runtime,
)


@dataclass(frozen=True)
class RuntimeComposition:
    build: dict[str, str]
    set_server_sleeping: Callable[[bool, str], dict[str, Any]]
    flush_log_handlers: Callable[[], None]


def configure_default_runtime(
    *,
    logger: logging.Logger,
    started_at: datetime,
    store_factory: Callable[[], Any],
) -> RuntimeComposition:
    build = build_info()

    def flush_log_handlers() -> None:
        flush_runtime_log_handlers(logger)

    configure_runtime_state(store_factory=store_factory)
    configure_system_status(
        started_at=started_at,
        build=build,
        store_factory=store_factory,
    )
    configure_backend_restart(store_factory=store_factory, flush_log_handlers_cb=flush_log_handlers)
    configure_runtime_callbacks(logger=logger, store_factory=store_factory)
    configure_indicator_module_services(
        logger=logger,
        store_factory=store_factory,
    )
    configure_bootstrap(logger=logger)
    configure_background_loops(logger=logger, store_factory=store_factory)
    return RuntimeComposition(
        build=dict(build),
        set_server_sleeping=set_server_sleeping_runtime,
        flush_log_handlers=flush_log_handlers,
    )
