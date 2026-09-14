from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.storage.db_utils import ensure_utc


@dataclass(frozen=True)
class TradingSessionIntervalReplacement:
    instrument_id: str
    route_fingerprint: str
    session_type: str
    starts_at: datetime
    ends_at: datetime
    rows: tuple[dict[str, Any], ...]


def _provider_schedule_timestamp(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"PROVIDER_SCHEDULE_{field.upper()}_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"PROVIDER_SCHEDULE_{field.upper()}_TIMEZONE_REQUIRED")
    return ensure_utc(parsed)


def normalize_trading_session_intervals(
    *,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    symbol: str,
    provider_contract_id: str,
    payload: dict[str, Any] | None,
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    provider_key = str(provider or "").strip().lower()
    symbol_key = str(symbol or "")
    raw_payload = dict(payload or {})
    if raw_payload.get("schedule_format") != "provider_declared_intervals":
        raise ValueError("PROVIDER_SCHEDULE_FORMAT_INVALID")
    coverage_start = _provider_schedule_timestamp(
        raw_payload.get("schedule_coverage_start"),
        field="coverage_start",
    )
    coverage_end = _provider_schedule_timestamp(
        raw_payload.get("schedule_coverage_end"),
        field="coverage_end",
    )
    if coverage_end <= coverage_start:
        raise ValueError("PROVIDER_SCHEDULE_COVERAGE_RANGE_INVALID")
    provider_intervals = raw_payload.get("trading_intervals")
    if not isinstance(provider_intervals, list) or not provider_intervals:
        raise ValueError("PROVIDER_SCHEDULE_INTERVALS_REQUIRED")
    rows: list[dict[str, Any]] = []
    exact_bounds: set[tuple[str, datetime, datetime]] = set()
    for item in provider_intervals:
        if not isinstance(item, dict):
            raise ValueError("PROVIDER_SCHEDULE_INTERVAL_INVALID")
        session_type = str(item.get("session_type") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()
        if session_type not in {"trading", "liquid"}:
            raise ValueError("PROVIDER_SCHEDULE_SESSION_TYPE_INVALID")
        if status not in {"open", "closed"}:
            raise ValueError("PROVIDER_SCHEDULE_STATUS_INVALID")
        opens_at = _provider_schedule_timestamp(item.get("opens_at"), field="opens_at")
        closes_at = _provider_schedule_timestamp(item.get("closes_at"), field="closes_at")
        if closes_at <= opens_at or opens_at < coverage_start or closes_at > coverage_end:
            raise ValueError("PROVIDER_SCHEDULE_INTERVAL_RANGE_INVALID")
        bounds = (session_type, opens_at, closes_at)
        if bounds in exact_bounds:
            raise ValueError("PROVIDER_SCHEDULE_INTERVAL_DUPLICATE")
        exact_bounds.add(bounds)
        raw_session_date = item.get("session_date")
        if not isinstance(raw_session_date, str):
            raise ValueError("PROVIDER_SCHEDULE_SESSION_DATE_REQUIRED")
        try:
            session_date = date.fromisoformat(raw_session_date)
        except ValueError as exc:
            raise ValueError("PROVIDER_SCHEDULE_SESSION_DATE_INVALID") from exc
        if session_date.isoformat() != raw_session_date:
            raise ValueError("PROVIDER_SCHEDULE_SESSION_DATE_INVALID")
        metadata = item.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("PROVIDER_SCHEDULE_INTERVAL_METADATA_INVALID")
        rows.append(
            {
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "provider": provider_key,
                "symbol": symbol_key,
                "provider_contract_id": provider_contract_id,
                "session_date": session_date,
                "session_type": session_type,
                "opens_at": opens_at,
                "closes_at": closes_at,
                "status": status,
                "source_fetched_at": fetched_at,
                "metadata": dict(metadata or {}),
            }
        )
    ordered_by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ordered_by_type.setdefault(str(row["session_type"]), []).append(row)
    for scoped_rows in ordered_by_type.values():
        ordered_rows = sorted(
            scoped_rows,
            key=lambda row: (row["opens_at"], row["closes_at"]),
        )
        for previous, current in zip(ordered_rows, ordered_rows[1:], strict=False):
            if current["opens_at"] < previous["closes_at"]:
                raise ValueError("PROVIDER_SCHEDULE_INTERVAL_OVERLAP")
    return rows


def trading_session_interval_replacement_horizons(
    rows: Sequence[dict[str, Any]],
) -> tuple[TradingSessionIntervalReplacement, ...]:
    """Group one admitted provider snapshot into exact affected horizons."""

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        identity = (
            require_exact_identity_text(
                row.get("instrument_id"),
                field="instrument_id",
            ),
            require_exact_identity_text(
                row.get("route_fingerprint"),
                field="route_fingerprint",
            ),
            str(row.get("session_type") or "").strip().lower(),
        )
        if not all(identity):
            raise ValueError("TRADING_SESSION_IDENTITY_REQUIRED")
        grouped.setdefault(identity, []).append(row)
    return tuple(
        TradingSessionIntervalReplacement(
            instrument_id=identity[0],
            route_fingerprint=identity[1],
            session_type=identity[2],
            starts_at=min(row["opens_at"] for row in replacements),
            ends_at=max(row["closes_at"] for row in replacements),
            rows=tuple(replacements),
        )
        for identity, replacements in sorted(grouped.items())
    )
