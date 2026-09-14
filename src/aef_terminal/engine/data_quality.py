from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from aef_terminal.data.providers import provider_data_policy_for_source
from aef_terminal.domain import Bar
from aef_terminal.engine.common import interval_minutes
from aef_terminal.data.provider_sessions import (
    expected_market_closed_notice,
    provider_bar_bucket,
    provider_schedule_open_interval,
)
from aef_terminal.runtime.instruments import InstrumentProfile, resolve_instrument_profile


def _serialize_data_quality_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Data-quality timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("Data-quality reports must not contain non-finite numbers")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError("Data-quality report keys must be non-empty strings")
            out[key] = _serialize_data_quality_value(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_serialize_data_quality_value(item) for item in value]
    raise TypeError("Data-quality reports must contain JSON-safe typed values")


def serialize_data_quality_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project native data-quality facts to their JSON transport contract once."""

    serialized = _serialize_data_quality_value(report)
    if not isinstance(serialized, dict):
        raise TypeError("Data-quality report must be a mapping")
    return serialized


def snapshot_freshness(
    *,
    timeframe: str,
    source: str,
    latest_bar_ts: datetime | None,
    updated_at: datetime | None = None,
    quote_ts: datetime | None = None,
    instrument_profile: Any = None,
) -> dict[str, Any]:
    updated = updated_at or datetime.now(tz=UTC)
    expected_seconds = max(int(interval_minutes(timeframe)) * 60, 60)
    threshold_seconds = max(
        float(stale_threshold_seconds(instrument_profile, expected_seconds)),
        1.0,
    )
    if latest_bar_ts is None:
        return {
            "status": "no_data",
            "source": source,
            "bar_age_seconds": None,
            "quote_age_seconds": None,
            "stale_threshold_seconds": threshold_seconds,
            "latest_bar_ts": None,
            "quote_ts": quote_ts.astimezone(UTC).isoformat() if quote_ts else None,
            "updated_at": updated.astimezone(UTC).isoformat(),
        }
    bar_age_seconds = max(
        (updated - latest_bar_ts.astimezone(UTC)).total_seconds(),
        0.0,
    )
    quote_age_seconds = (
        max((updated - quote_ts.astimezone(UTC)).total_seconds(), 0.0)
        if quote_ts is not None
        else None
    )
    status = "fresh" if bar_age_seconds <= threshold_seconds else "stale"
    return {
        "status": status,
        "source": source,
        "bar_age_seconds": round(bar_age_seconds, 3),
        "quote_age_seconds": (
            round(quote_age_seconds, 3) if quote_age_seconds is not None else None
        ),
        "stale_threshold_seconds": threshold_seconds,
        "latest_bar_ts": latest_bar_ts.astimezone(UTC).isoformat(),
        "quote_ts": quote_ts.astimezone(UTC).isoformat() if quote_ts else None,
        "updated_at": updated.astimezone(UTC).isoformat(),
    }


def stale_threshold_seconds(profile: Any, expected_seconds: int) -> float:
    family = str(getattr(profile, "family", "") or "")
    if family == "crypto":
        return expected_seconds * 3.0
    if family in {"future", "index_future", "metal_future", "energy_future"}:
        return max(expected_seconds * 3.0, 20.0 * 60.0)
    if bool(getattr(profile, "etf_option", False)):
        return max(expected_seconds * 3.0, 90.0 * 60.0)
    return max(expected_seconds * 3.0, 4.0 * 60.0 * 60.0)


def sparse_session_source(
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any] | None = None,
) -> bool:
    if not bars or not isinstance(instrument, dict):
        return False
    provider = instrument.get("provider")
    if not provider:
        return False
    return provider_data_policy_for_source(str(provider)).sparse_sessions


def data_quality_report(
    bars: Sequence[Bar],
    interval: str,
    now_utc: datetime | None = None,
    tail_delay_grace_seconds: float = 0.0,
    store: Any | None = None,
    instrument: dict[str, Any] | None = None,
    instrument_profile: InstrumentProfile | None = None,
) -> dict[str, Any]:
    """Build current freshness and provider-session facts for analysis.

    Historical completeness is deliberately absent here.  Exact eligible
    history-coverage receipts own that decision; timestamp gaps between bars
    never reconstruct provider intent or a historical trading calendar.
    """

    if not bars:
        return {
            "status": "no_data",
            "signals_ok": False,
            "empty_history": True,
            "warning": "NO DATA: no provider-confirmed bars available",
            "stale_minutes": None,
            "market_closed": False,
            "session_unknown": False,
            "session_warmup": False,
            "session_open_ts": None,
            "first_confirmed_bar_due_ts": None,
            "reopen_ts": None,
            "session": "",
            "sparse_session": False,
        }

    ordered = sorted(bars, key=lambda item: item.ts)
    latest = ordered[-1]
    profile = instrument_profile or resolve_instrument_profile(instrument)
    expected_seconds = max(interval_minutes(interval) * 60, 60)
    now = (now_utc or datetime.now(tz=UTC)).astimezone(UTC)
    has_instrument_identity = isinstance(instrument, dict)
    closed_notice = (
        expected_market_closed_notice(
            latest.ts,
            now,
            interval,
            store=store,
            instrument=instrument,
        )
        if has_instrument_identity
        else {
            "warning": (
                "SESSION IDENTITY ERROR: provider-qualified instrument identity is required "
                "for session gating."
            ),
            "reopen_ts": None,
            "session": "session_identity_error",
        }
    )

    age_seconds = max((now - latest.ts.astimezone(UTC)).total_seconds(), 0.0)
    stale_minutes = max((age_seconds - expected_seconds) / 60.0, 0.0)
    stale_threshold = stale_threshold_seconds(profile, expected_seconds)
    delay_grace = max(float(tail_delay_grace_seconds or 0.0), 0.0)
    provider_closed = bool(
        closed_notice is not None and str(closed_notice.get("session") or "") == "market_closed"
    )
    stale = age_seconds > stale_threshold + delay_grace and not provider_closed

    session_warmup = False
    session_open_ts: datetime | None = None
    first_confirmed_bar_due_ts: datetime | None = None
    if stale and closed_notice is None and has_instrument_identity and latest.closed is not False:
        open_interval = provider_schedule_open_interval(
            now,
            store=store,
            instrument=instrument,
        )
        if open_interval is not None:
            session_open_ts = open_interval["opens_at"]
            opening_bucket = provider_bar_bucket(
                session_open_ts,
                interval,
                instrument=instrument,
                session_open_interval=open_interval,
            )
            first_confirmed_bar_due_ts = (
                opening_bucket.closes_at if opening_bucket is not None else None
            )
            session_warmup = bool(
                latest.ts.astimezone(UTC) < session_open_ts
                and first_confirmed_bar_due_ts is not None
                and session_open_ts <= now < first_confirmed_bar_due_ts
            )
            if session_warmup:
                stale = False

    status = "ok"
    warnings: list[str] = []
    if stale:
        status = "stale"
        warnings.append(
            "LIVE STALE: "
            f"latest {latest.symbol} {interval} bar is {round(stale_minutes, 1)} min behind "
            f"(last {latest.ts.astimezone(UTC).isoformat()})"
        )
    if session_warmup:
        status = "session_warmup" if status == "ok" else f"{status}+session_warmup"
        warnings.append(
            "SESSION WARMUP: "
            f"{latest.symbol} {interval} is awaiting the first exchange-confirmed bar "
            f"due at {first_confirmed_bar_due_ts.isoformat()}."
        )

    session_status = (
        str(closed_notice.get("session") or "market_closed") if closed_notice is not None else ""
    )
    if closed_notice is not None:
        session_suffix = (
            "session_unknown"
            if session_status in {"session_unknown", "session_identity_error"}
            else "market_closed"
        )
        status = session_suffix if status == "ok" else f"{status}+{session_suffix}"
        warnings.append(str(closed_notice["warning"]))

    return {
        "status": status,
        "signals_ok": bool(not stale and not session_warmup and closed_notice is None),
        "warning": "; ".join(warnings),
        "stale_minutes": round(stale_minutes, 1),
        "stale_threshold_minutes": round(stale_threshold / 60.0, 1),
        "latest_ts": latest.ts.astimezone(UTC),
        "market_closed": session_status == "market_closed",
        "session_unknown": session_status
        in {
            "session_unknown",
            "session_identity_error",
        },
        "session_warmup": session_warmup,
        "session_open_ts": session_open_ts,
        "first_confirmed_bar_due_ts": first_confirmed_bar_due_ts,
        "reopen_ts": closed_notice.get("reopen_ts") if closed_notice else None,
        "session": session_status,
        "sparse_session": sparse_session_source(ordered, instrument=instrument),
    }
