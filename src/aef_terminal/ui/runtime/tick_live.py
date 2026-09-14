from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr_tick_feed import IbkrTickFeed
from aef_terminal.ui.services.tick_live_connector import TickLiveConnectorService


_TICK_LIVE_CONNECTOR = TickLiveConnectorService(
    tick_feed_factory=IbkrTickFeed,
    config_factory=AppConfig,
)


def tick_live_connector() -> TickLiveConnectorService:
    return _TICK_LIVE_CONNECTOR


def configure_tick_live(
    *,
    server_sleeping,
    client_settings_snapshot,
    publish_client_settings_patch,
    restore_allowed,
    store_factory: Callable[[], Any],
    logger: logging.Logger,
) -> None:
    _TICK_LIVE_CONNECTOR.configure(
        server_sleeping=server_sleeping,
        client_settings_snapshot=client_settings_snapshot,
        publish_client_settings_patch=publish_client_settings_patch,
        restore_allowed=restore_allowed,
        store_factory=store_factory,
        logger=logger,
    )


def tick_live_status() -> dict[str, Any]:
    return _TICK_LIVE_CONNECTOR.status()


async def ensure_tick_live_from_intent(reason: str = "monitor") -> dict[str, Any]:
    return await _TICK_LIVE_CONNECTOR.ensure_from_intent(reason=reason)


async def tick_live_restore_loop() -> None:
    await _TICK_LIVE_CONNECTOR.restore_loop()


async def stop_tick_live(reason: str = "shutdown") -> dict[str, Any]:
    return await _TICK_LIVE_CONNECTOR.stop(reason=reason)


async def set_tick_live_enabled(
    enabled: bool,
    instrument_id: str,
    route_fingerprint: str,
) -> dict[str, Any]:
    return await _TICK_LIVE_CONNECTOR.set_enabled(
        enabled,
        instrument_id,
        route_fingerprint,
    )
