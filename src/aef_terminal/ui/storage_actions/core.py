from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.ui.routers.error_payloads import build_action_error_response


@dataclass(frozen=True)
class StorageActionDeps:
    store_factory: Callable[[], Any]
    apply_ibkr_runtime_settings: Callable[[dict[str, Any] | None], dict[str, Any]]
    publish_client_settings_mutations: Callable[[dict[str, dict[str, Any]], int], None]
    normalize_drawing_anchors: Callable[..., list[dict[str, Any]]]
    require_unique_price_alerts: Callable[..., list[dict[str, Any]]]
    logger: logging.Logger | None = None


def storage_unavailable_response(**extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code="STORAGE_NOT_CONFIGURED",
        category="storage",
        retryable=False,
        error="storage not configured",
        **extra,
    )


def storage_validation_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return build_action_error_response(
        code=code,
        category="storage",
        retryable=False,
        error=message,
        **extra,
    )


def store_or_unavailable(deps: StorageActionDeps, **extra: Any) -> Any | dict[str, Any]:
    store = deps.store_factory()
    if store is None:
        return storage_unavailable_response(**extra)
    return store
