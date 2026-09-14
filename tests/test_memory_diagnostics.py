from __future__ import annotations

import json

from aef_terminal.ui.services.market_analysis_runtime_service import (
    MarketAnalysisRuntimeService,
)
from aef_terminal.ui.services.memory_diagnostics import (
    memory_diagnostics_snapshot,
)


def test_latest_market_analysis_indicator_timings_reads_cached_json_bytes() -> None:
    snapshot = {
        "meta": {
            "symbol": "ES",
            "timeframe": "5m",
            "indicator_timings_ms": {
                "fast": 1.25,
                "slow": 8.5,
            },
        }
    }
    old_snapshot_bytes = json.dumps({"meta": {"indicator_timings_ms": {"older": 99.0}}}).encode(
        "utf-8"
    )
    snapshot_bytes = json.dumps(snapshot).encode("utf-8")
    cache = {
        "old": {
            "status": "ready",
            "snapshot_json": old_snapshot_bytes,
            "size_bytes": len(old_snapshot_bytes),
            "updated_monotonic": 1.0,
        },
        "new": {
            "status": "ready",
            "snapshot_json": snapshot_bytes,
            "size_bytes": len(snapshot_bytes),
            "updated_at": "2026-06-30T10:00:00+00:00",
            "updated_monotonic": 2.0,
        },
    }

    runtime = MarketAnalysisRuntimeService()
    runtime._cache.update(cache)

    payload = runtime.diagnostics()["latest_indicator_timings"]

    assert payload["key"] == "new"
    assert payload["symbol"] == "ES"
    assert payload["timeframe"] == "5m"
    assert payload["top"] == [
        {"indicator": "slow", "ms": 8.5},
        {"indicator": "fast", "ms": 1.25},
    ]


def test_memory_diagnostics_includes_quote_stream_cache_stats() -> None:
    payload = memory_diagnostics_snapshot()

    chart_stats = payload["caches"]["chart_db"]
    assert chart_stats["result_cache_max_entries"] > 0
    assert chart_stats["result_cache_max_bars"] > 0
    assert chart_stats["result_cache_max_bytes"] > 0
    assert chart_stats["generation_entries"] >= 0

    recent_bar_stats = payload["caches"]["recent_confirmed_bars"]
    assert recent_bar_stats["entries"] >= 0
    assert recent_bar_stats["bars"] >= 0
    assert recent_bar_stats["slot_loads"] >= 0
    assert recent_bar_stats["max_entries"] == 128
    assert recent_bar_stats["max_bars_per_entry"] == 1024

    quote_stats = payload["caches"]["quote_stream"]
    assert "clients" in quote_stats
    assert "cache_entries" in quote_stats
    assert "cache_max_entries" in quote_stats
    assert "cache_attempted_age_seconds" in quote_stats
