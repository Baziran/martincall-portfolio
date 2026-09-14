from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from aef_terminal.indicators.registry import indicator_modules
from aef_terminal.indicators.refs import resolve_ref


_LOGGER = logging.getLogger(__name__)

SnapshotEnricher = Callable[[dict[str, Any]], dict[str, Any]]


@lru_cache(maxsize=1)
def snapshot_enrichers() -> tuple[SnapshotEnricher, ...]:
    return tuple(
        resolve_ref(module.snapshot_enricher_ref)
        for module in indicator_modules()
        if module.snapshot_enricher_ref
    )


def enrich_market_analysis_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    for enricher in snapshot_enrichers():
        try:
            enricher(snapshot)
        except Exception:
            name = getattr(enricher, "__name__", enricher.__class__.__name__)
            _LOGGER.exception("market snapshot enricher failed: %s", name)
    return snapshot
