from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from typing import Any
from aef_terminal.runtime import pine
from aef_terminal.runtime.timeframes import require_aware_utc_datetime


@dataclass(frozen=True, slots=True)
class VixContextSample:
    observed_at: datetime
    price: float

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("VIX sample timestamp must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        if (
            isinstance(self.price, bool)
            or not isinstance(self.price, (int, float))
            or not isfinite(self.price)
            or not 5.0 <= self.price <= 100.0
        ):
            raise ValueError("VIX sample price is outside the supported range")


@dataclass(frozen=True, slots=True)
class VixContextInput:
    instrument_id: str
    route_fingerprint: str
    provider: str
    provider_symbol: str
    samples: tuple[VixContextSample, ...]

    def __post_init__(self) -> None:
        for field_name in ("instrument_id", "route_fingerprint", "provider", "provider_symbol"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"VIX context {field_name} must be exact non-empty text")
        if not self.samples:
            raise ValueError("VIX context requires at least one qualified sample")
        if any(
            current.observed_at <= previous.observed_at
            for previous, current in zip(self.samples, self.samples[1:], strict=False)
        ):
            raise ValueError("VIX context samples must be strictly time-ordered")


def _json_safe(value: Any) -> Any:
    """Convert third-party scalar values to JSON-native Python values."""
    # Most leaves in an analysis snapshot are already native scalars. Exact
    # types keep NumPy/subclass conversion on the existing normalization path.
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return require_aware_utc_datetime(value, field="analysis JSON timestamp").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    return value


def vix_features_from_context(context: VixContextInput | None) -> dict[str, Any]:
    """Calculate VIX features from an already qualified immutable market input."""

    if context is None:
        return {}
    values = [sample.price for sample in context.samples]
    price = values[-1]
    previous = values[-2] if len(values) >= 2 else None
    ema_len = min(20, len(values))
    ema = pine.ema_series(values, ema_len)[-1] if ema_len >= 2 else values[-1]
    sma_window = values[-min(50, len(values)) :]
    sma = sum(sma_window) / len(sma_window)
    variance = sum((value - sma) ** 2 for value in sma_window) / max(len(sma_window), 1)
    return {
        "vix_price": round(price, 4),
        "vix_previous": round(previous, 4) if previous is not None else None,
        "vix_ema": round(ema, 4),
        "vix_sma": round(sma, 4),
        "vix_stdev": round(variance**0.5, 4),
        "vix_source": "qualified_quote_snapshots",
    }
