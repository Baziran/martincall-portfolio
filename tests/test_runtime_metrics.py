from __future__ import annotations

from aef_terminal.runtime.metrics import (
    increment_metric,
    observe_metric,
    reset_runtime_metrics,
    runtime_metrics_snapshot,
    set_metric,
)


def test_runtime_metrics_snapshot_contains_counters_gauges_and_histograms() -> None:
    reset_runtime_metrics()

    increment_metric("demo_total", 2, lane="quote")
    set_metric("demo_depth", 7, lane="quote")
    observe_metric("demo_seconds", 0.25, lane="quote")
    observe_metric("demo_seconds", 0.75, lane="quote")

    snapshot = runtime_metrics_snapshot()

    assert snapshot["updated_at"]
    assert snapshot["counters"] == [
        {"name": "demo_total", "value": 2.0, "labels": {"lane": "quote"}}
    ]
    assert snapshot["gauges"] == [{"name": "demo_depth", "value": 7.0, "labels": {"lane": "quote"}}]
    assert snapshot["histograms"] == [
        {
            "name": "demo_seconds",
            "count": 2,
            "sum": 1.0,
            "avg": 0.5,
            "min": 0.25,
            "max": 0.75,
            "latest": 0.75,
            "labels": {"lane": "quote"},
        }
    ]

    reset_runtime_metrics()
