from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal


KnownMarketDataEntitlement = Literal[
    "live",
    "frozen",
    "delayed",
    "delayed_frozen",
]
MarketDataEntitlement = Literal[
    "live",
    "frozen",
    "delayed",
    "delayed_frozen",
    "unknown",
]
MarketDataTimeBasis = Literal["provider_event", "client_receive"]

QUOTE_SNAPSHOT_RETENTION_HOURS = 48
QUOTE_SNAPSHOT_FUTURE_SKEW_SECONDS = 5.0


def market_data_price_source_is_current(
    price_source: object,
    time_basis: object,
) -> bool:
    """Return whether one selected price has its authoritative clock basis."""

    return (price_source, time_basis) in {
        ("last", "provider_event"),
        ("bid_ask_mid", "client_receive"),
    }


def market_data_observation_time(
    value: Mapping[str, Any],
) -> tuple[datetime | None, MarketDataTimeBasis | None]:
    """Read one timestamp only from its explicitly declared clock basis."""

    if not isinstance(value, Mapping):
        return None, None
    basis = value.get("time_basis")
    if basis not in {"provider_event", "client_receive"}:
        return None, None
    raw_timestamp = (
        value.get("provider_ts") if basis == "provider_event" else value.get("received_at")
    )
    if basis == "client_receive" and value.get("provider_ts") is not None:
        return None, None
    if isinstance(raw_timestamp, datetime):
        parsed = raw_timestamp
    elif (
        isinstance(raw_timestamp, str) and raw_timestamp and raw_timestamp == raw_timestamp.strip()
    ):
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None, None
    else:
        return None, None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, None
    return parsed.astimezone(UTC), basis


def quote_snapshot_timestamp_is_admissible(
    observed_at: datetime,
    *,
    captured_at: datetime,
) -> bool:
    """Return whether one quote observation can enter durable 48-hour history."""

    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
        or not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        return False
    observation_utc = observed_at.astimezone(UTC)
    capture_utc = captured_at.astimezone(UTC)
    return (
        capture_utc - timedelta(hours=QUOTE_SNAPSHOT_RETENTION_HOURS)
        <= observation_utc
        <= capture_utc + timedelta(seconds=QUOTE_SNAPSHOT_FUTURE_SKEW_SECONDS)
    )
