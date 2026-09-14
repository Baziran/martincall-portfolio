from __future__ import annotations

from dataclasses import dataclass
from typing import Any


WATCHLIST_SETTINGS_LOCK_KEY = "aef:instrument_watchlist"


@dataclass(frozen=True)
class WatchlistRetirementSettingsSnapshot:
    """Exact post-retirement settings state returned by the fenced transaction."""

    settings_revision: int
    client_settings: dict[str, Any]
    client_mutation_orders: dict[str, dict[str, Any]]
    gex_scheduler_setting: Any
    gex_scheduler_present: bool
    option_target_caps_setting: Any
    option_target_caps_present: bool


class WatchlistOrderError(ValueError):
    """Typed optimistic-concurrency rejection for watchlist ordering."""

    def __init__(
        self,
        code: str,
        *,
        expected_version: int | None = None,
        current_version: int | None = None,
    ) -> None:
        exact_code = str(code or "").strip()
        if not exact_code:
            raise ValueError("WATCHLIST_ORDER_ERROR_CODE_REQUIRED")
        self.code = exact_code
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(exact_code)
