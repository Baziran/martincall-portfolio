from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


MetricLabels = tuple[tuple[str, str], ...]
MetricKey = tuple[str, MetricLabels]


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    latest: float | None = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.latest = value

    def snapshot(self) -> dict[str, Any]:
        avg = self.total / self.count if self.count else 0.0
        return {
            "count": self.count,
            "sum": round(self.total, 6),
            "avg": round(avg, 6),
            "min": round(self.minimum or 0.0, 6),
            "max": round(self.maximum or 0.0, 6),
            "latest": round(self.latest or 0.0, 6),
        }


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[MetricKey, float] = {}
        self._gauges: dict[MetricKey, float] = {}
        self._histograms: dict[MetricKey, _Histogram] = {}
        self._updated_at: datetime | None = None

    def increment(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = _metric_key(name, labels)
        amount = float(value)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount
            self._updated_at = datetime.now(tz=UTC)

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        key = _metric_key(name, labels)
        with self._lock:
            self._gauges[key] = float(value)
            self._updated_at = datetime.now(tz=UTC)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = _metric_key(name, labels)
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = _Histogram()
                self._histograms[key] = histogram
            histogram.observe(float(value))
            self._updated_at = datetime.now(tz=UTC)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._updated_at = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": self._updated_at.isoformat() if self._updated_at else "",
                "counters": [
                    {"name": name, "value": round(value, 6), "labels": _labels_dict(labels)}
                    for (name, labels), value in sorted(self._counters.items())
                ],
                "gauges": [
                    {"name": name, "value": round(value, 6), "labels": _labels_dict(labels)}
                    for (name, labels), value in sorted(self._gauges.items())
                ],
                "histograms": [
                    {"name": name, **histogram.snapshot(), "labels": _labels_dict(labels)}
                    for (name, labels), histogram in sorted(self._histograms.items())
                ],
            }


def _metric_key(name: str, labels: dict[str, Any]) -> MetricKey:
    metric_name = str(name or "").strip()
    if not metric_name:
        metric_name = "unknown"
    normalized = tuple(sorted((str(key), str(value)) for key, value in labels.items()))
    return metric_name, normalized


def _labels_dict(labels: MetricLabels) -> dict[str, str]:
    return {key: value for key, value in labels}


runtime_metrics = RuntimeMetrics()


def increment_metric(name: str, value: float = 1.0, **labels: Any) -> None:
    runtime_metrics.increment(name, value, **labels)


def set_metric(name: str, value: float, **labels: Any) -> None:
    runtime_metrics.set_gauge(name, value, **labels)


def observe_metric(name: str, value: float, **labels: Any) -> None:
    runtime_metrics.observe(name, value, **labels)


def runtime_metrics_snapshot() -> dict[str, Any]:
    return runtime_metrics.snapshot()


def reset_runtime_metrics() -> None:
    runtime_metrics.reset()
