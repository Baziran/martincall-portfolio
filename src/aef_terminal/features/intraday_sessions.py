from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    provider_session_bounds_for_bar,
    provider_session_level_context,
)
from aef_terminal.runtime.timeframes import interval_minutes


def session_day_groups(
    bars: Sequence[Bar],
    *,
    provider_session: ProviderSessionReset | None,
) -> list[tuple[str, list[Bar]]]:
    """Group bars only by exact provider session keys."""

    if provider_session is None or not provider_session.available:
        return []
    grouped: dict[str, list[Bar]] = {}
    for bar in bars:
        try:
            key = provider_session.key_for_bar(bar)
        except ValueError:
            continue
        grouped.setdefault(key, []).append(bar)
    return [(key, grouped[key]) for key in grouped]


def first_hour_range(
    day_bars: Sequence[Bar],
    *,
    provider_session: ProviderSessionReset | None,
) -> tuple[float | None, float | None]:
    context = provider_session_level_context(day_bars, provider_session)
    if not context.available or not context.initial_balance_complete:
        return None, None
    return context.initial_balance_high, context.initial_balance_low


def prior_session_day_range(
    bars: Sequence[Bar],
    *,
    provider_session: ProviderSessionReset | None,
) -> tuple[float | None, float | None, str | None]:
    days = session_day_groups(bars, provider_session=provider_session)
    if len(days) < 2:
        return None, None, None
    day_key, prior_day = days[-2]
    if not prior_day:
        return None, None, None
    return max(bar.high for bar in prior_day), min(bar.low for bar in prior_day), day_key


def session_context_summary(
    bars: Sequence[Bar],
    *,
    provider_session: ProviderSessionReset | None,
) -> dict[str, Any]:
    if not bars:
        return {"ready": False, "reason_code": "provider_session_bars_missing"}
    context = provider_session_level_context(bars, provider_session)
    if not context.available:
        return {
            "ready": False,
            "source": context.source,
            "reason_code": context.reason_code,
        }
    latest = bars[-1]
    bounds = provider_session_bounds_for_bar(provider_session, latest)
    if bounds is None:
        return {
            "ready": False,
            "source": context.source,
            "reason_code": "provider_session_bounds_unavailable",
        }
    opens_at, closes_at = bounds
    latest_close = latest.ts + timedelta(minutes=max(interval_minutes(latest.timeframe), 1))
    groups = session_day_groups(bars, provider_session=provider_session)
    return {
        "ready": True,
        "source": context.source,
        "reason_code": "",
        "day": context.session_key,
        "prior_day": groups[-2][0] if len(groups) >= 2 else None,
        "first_hour_high": context.initial_balance_high,
        "first_hour_low": context.initial_balance_low,
        "prior_day_high": context.previous_session_high,
        "prior_day_low": context.previous_session_low,
        "in_session": opens_at <= latest.ts < closes_at,
        "in_first_hour": opens_at <= latest.ts < opens_at + timedelta(hours=1),
        "first_hour_complete": latest_close >= opens_at + timedelta(hours=1),
    }
