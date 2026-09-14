from __future__ import annotations

import logging
from typing import Any, Callable

from aef_terminal.indicators.registry import INDICATOR_REGISTRY, paper_tradable_indicator_ids

PAPER_SKIP_INDICATOR_IDS = frozenset(INDICATOR_REGISTRY).difference(paper_tradable_indicator_ids())


def paper_indicator_source_ids() -> frozenset[str]:
    """Tradable indicator ids used in trade_setup confluence labels (not execution gates)."""
    sources: set[str] = set()
    tradable_ids = paper_tradable_indicator_ids()
    for indicator_id, spec in INDICATOR_REGISTRY.items():
        if indicator_id not in tradable_ids:
            continue
        sources.add(indicator_id)
        if spec.signal_source:
            sources.add(spec.signal_source)
    return frozenset(sources)


# Execution listens only to Trade Center commands. Bots should use the same contract.
PAPER_ALLOWED_SOURCES = frozenset({"trade_setup"})
PAPER_JOURNAL_RECENT_HOURS = 24
PAPER_ALLOWED_SETUPS = {"fade", "transit"}
PAPER_MIN_RR = 1.25
PAPER_ALLOWED_MIN_RR = {0.75, 1.25, 1.5, 2.0}
PAPER_EDGE_FILTER_CACHE_SECONDS = 30.0

_LOGGER = logging.getLogger(__name__)
_PAPER_EDGE_FILTER_CACHE: dict[str, Any] = {"loaded_at": 0.0, "path": "", "mtime": 0.0, "rules": []}

EdgeFilterLoader = Callable[[], list[dict[str, Any]]]
