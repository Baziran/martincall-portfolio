from __future__ import annotations

import atexit
from collections.abc import Iterable
from dataclasses import asdict
from functools import lru_cache
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.domain import Bar, BarState
from aef_terminal.storage.db_utils import (
    DatabaseUnavailable,
    ensure_utc,
    format_kib,
    format_pg_setting,
    safe_tick_bucket,
    safe_tick_history_interval,
    utc_now,
)
from aef_terminal.storage.repos.bars import BarsRepoMixin
from aef_terminal.storage.repos.connection import (
    CanonicalWriterCapability,
    CanonicalWriterLeaseLost,
    CanonicalWriterLeaseUnavailable,
    CanonicalWriterReadOnly,
    PostgresConnectionMixin,
)
from aef_terminal.storage.repos.diagnostics import DiagnosticsRepoMixin
from aef_terminal.storage.repos.discord_signals import DiscordSignalsRepoMixin
from aef_terminal.storage.repos.drawings import DrawingsRepoMixin
from aef_terminal.storage.repos.futures import FuturesRepoMixin
from aef_terminal.storage.repos.gex import GexRepoMixin
from aef_terminal.storage.repos.history_coverage import HistoryCoverageRepoMixin
from aef_terminal.storage.repos.history_commits import HistoryCommitsRepoMixin
from aef_terminal.storage.repos.instruments import InstrumentsRepoMixin
from aef_terminal.storage.repos.paper import PaperRepoMixin
from aef_terminal.storage.repos.schema import SchemaRepoMixin
from aef_terminal.storage.repos.settings import SettingsRepoMixin
from aef_terminal.storage.repos.ticks import TicksRepoMixin
from aef_terminal.storage.repos.trading_hours import TradingHoursRepoMixin

__all__ = [
    "DatabaseUnavailable",
    "CanonicalWriterCapability",
    "CanonicalWriterLeaseLost",
    "CanonicalWriterLeaseUnavailable",
    "CanonicalWriterReadOnly",
    "PostgresStore",
    "default_postgres_store",
    "ensure_utc",
    "format_kib",
    "format_pg_setting",
    "safe_tick_bucket",
    "safe_tick_history_interval",
    "serialize_bars_for_debug",
    "utc_now",
]


class PostgresStore(
    DiagnosticsRepoMixin,
    DiscordSignalsRepoMixin,
    PaperRepoMixin,
    GexRepoMixin,
    HistoryCommitsRepoMixin,
    HistoryCoverageRepoMixin,
    InstrumentsRepoMixin,
    FuturesRepoMixin,
    DrawingsRepoMixin,
    SettingsRepoMixin,
    BarsRepoMixin,
    TradingHoursRepoMixin,
    TicksRepoMixin,
    SchemaRepoMixin,
    PostgresConnectionMixin,
):
    """PostgreSQL/TimescaleDB storage for normalized market data and terminal state."""


@lru_cache(maxsize=1)
def default_postgres_store() -> PostgresStore | None:
    config = AppConfig()
    if not config.database_url:
        return None
    store = PostgresStore(config.database_url)
    atexit.register(store.close)
    return store


def serialize_bars_for_debug(bars: Iterable[Bar]) -> list[dict[str, Any]]:
    return [
        asdict(bar)
        | {
            "ts": ensure_utc(bar.ts).isoformat(),
            "state": BarState(bar.state).value,
        }
        for bar in bars
    ]
