from __future__ import annotations

import gc
import os
from collections import Counter
from datetime import UTC, datetime
from typing import Any


def _proc_kb(path: str, keys: tuple[str, ...]) -> dict[str, int]:
    wanted = set(keys)
    out: dict[str, int] = {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for line in lines:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key not in wanted:
            continue
        parts = raw.strip().split()
        try:
            out[key] = int(parts[0])
        except IndexError, ValueError:
            continue
    return out


def _market_analysis_cache_stats() -> dict[str, Any]:
    from aef_terminal.ui import market_analysis_runtime

    return market_analysis_runtime.diagnostics()


def _chart_db_cache_stats() -> dict[str, Any]:
    from aef_terminal.ui.services import chart_history

    return {
        "result_cache_entries": len(chart_history._result_cache),
        "result_cache_bars": sum(cached[2] for cached in chart_history._result_cache.values()),
        "result_cache_estimated_bytes": sum(
            cached[3] for cached in chart_history._result_cache.values()
        ),
        "result_cache_max_entries": chart_history._RESULT_CACHE_MAX_ENTRIES,
        "result_cache_max_bars": chart_history._RESULT_CACHE_MAX_BARS,
        "result_cache_max_bytes": chart_history._RESULT_CACHE_MAX_BYTES,
        "generation_entries": len(chart_history._cache_generation),
        "inflight_tasks": len(chart_history._inflight_tasks),
        "ttl_seconds": chart_history._RESULT_CACHE_TTL_SECONDS,
    }


def _quote_cache_stats() -> dict[str, Any]:
    from aef_terminal.ui.runtime import quote_stream

    return quote_stream.cache_stats()


def _analysis_context_cache_stats() -> dict[str, Any]:
    from aef_terminal.engine.context_cache import analysis_context_cache_stats

    return analysis_context_cache_stats()


def _recent_confirmed_bar_cache_stats() -> dict[str, int]:
    from aef_terminal.ui.services.recent_confirmed_bars import (
        recent_confirmed_bar_cache_stats,
    )

    return recent_confirmed_bar_cache_stats()


def memory_diagnostics_snapshot(*, include_objects: bool = False) -> dict[str, Any]:
    status = _proc_kb(
        "/proc/self/status",
        ("VmRSS", "VmHWM", "RssAnon", "RssFile", "VmData", "VmSwap"),
    )
    smaps = _proc_kb(
        "/proc/self/smaps_rollup",
        ("Rss", "Private_Dirty", "Private_Clean", "Anonymous", "Pss"),
    )
    payload: dict[str, Any] = {
        "ok": True,
        "checked_at": datetime.now(tz=UTC).isoformat(),
        "pid": os.getpid(),
        "process_kb": status,
        "smaps_kb": smaps,
        "gc": {
            "counts": list(gc.get_count()),
            "thresholds": list(gc.get_threshold()),
        },
        "caches": {
            "market_analysis": _market_analysis_cache_stats(),
            "analysis_context": _analysis_context_cache_stats(),
            "chart_db": _chart_db_cache_stats(),
            "recent_confirmed_bars": _recent_confirmed_bar_cache_stats(),
            "quote_stream": _quote_cache_stats(),
        },
    }
    if include_objects:
        objects = gc.get_objects()
        payload["gc"]["object_count"] = len(objects)
        payload["gc"]["top_types"] = Counter(type(item).__name__ for item in objects).most_common(
            25
        )
    return payload
