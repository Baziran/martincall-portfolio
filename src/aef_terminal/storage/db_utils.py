from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.paper_contract import (
    PAPER_CONTRACT_KEY,
    PAPER_PROTECTION_BASIS_KEY,
    PAPER_PROTECTION_INTENT_KEY,
    require_paper_contract_identity,
    require_paper_order_protection,
)
from aef_terminal.runtime.clock import utc_now as utc_now
from aef_terminal.runtime.timeframes import require_aware_utc_datetime


class DatabaseUnavailable(RuntimeError):
    """Raised when PostgreSQL/Timescale storage is configured but cannot be used."""


def database_error_is_transient(error: Exception) -> bool:
    """Classify retryable storage transport failures without leaking the driver."""

    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(error, (psycopg.InterfaceError, psycopg.OperationalError))


def ensure_utc(value: datetime) -> datetime:
    return require_aware_utc_datetime(value, field="storage datetime")


def format_kib(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} GB"
    if value >= 1024:
        return f"{value / 1024:.0f} MB"
    return f"{value} kB"


def format_pg_setting(setting: Any, unit: str | None) -> str:
    raw = str(setting)
    if not unit:
        return raw
    if unit == "8kB":
        return format_kib(int(raw) * 8)
    if unit == "kB":
        return format_kib(int(raw))
    return f"{raw} {unit}"


def safe_tick_bucket(value: str) -> str:
    allowed = {
        "1 second",
        "5 seconds",
        "10 seconds",
        "15 seconds",
        "30 seconds",
        "1 minute",
        "5 minutes",
        "15 minutes",
    }
    bucket = str(value or "1 minute").strip().lower()
    if bucket not in allowed:
        raise ValueError(f"unsupported tick bucket: {value}")
    return bucket


def safe_tick_history_interval(value: str) -> str:
    intervals = {
        "1m": "1 minute",
        "1min": "1 minute",
        "1 minute": "1 minute",
        "5m": "5 minutes",
        "5min": "5 minutes",
        "5 minutes": "5 minutes",
        "15m": "15 minutes",
        "15min": "15 minutes",
        "15 minutes": "15 minutes",
        "30m": "30 minutes",
        "30min": "30 minutes",
        "30 minutes": "30 minutes",
        "60m": "1 hour",
        "1 hour": "1 hour",
    }
    interval = intervals.get(str(value or "5m").strip().lower())
    if interval is None:
        raise ValueError(f"unsupported tick history interval: {value}")
    return interval


def _paper_utc_datetime(value: Any, default: datetime | None = None) -> datetime:
    if value is None:
        resolved = default if default is not None else utc_now()
        if resolved.tzinfo is None or resolved.utcoffset() is None:
            raise ValueError("PAPER_DATETIME_DEFAULT_TIMEZONE_REQUIRED")
        return resolved.astimezone(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("PAPER_DATETIME_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("PAPER_DATETIME_INVALID") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("PAPER_DATETIME_TIMEZONE_REQUIRED")
        return parsed.astimezone(UTC)
    raise ValueError("PAPER_DATETIME_INVALID")


def _paper_order_from_row(row: Sequence[Any]) -> dict[str, Any]:
    if len(row) != 22:
        raise ValueError(f"PAPER_ORDER_ROW_SHAPE_INVALID expected=22 actual={len(row)}")
    role = row[16]
    if not isinstance(role, str) or role not in {"entry", "stop", "take", "close"}:
        raise ValueError(
            f"PAPER_ORDER_ROW_ROLE_INVALID expected=entry|stop|take|close actual={role!r}"
        )
    payload = row[21]
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"PAPER_ORDER_ROW_PAYLOAD_INVALID expected=mapping actual={type(payload).__name__}"
        )
    instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="PAPER_ORDER_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="PAPER_ORDER_ROUTE_FINGERPRINT",
    )
    paper_contract = require_paper_contract_identity(payload)
    order_id = require_exact_identity_text(
        row[0],
        field="PAPER_ORDER_ID",
    )
    order = dict(payload)
    order.update(
        {
            "id": order_id,
            "symbol": str(row[1]),
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            PAPER_CONTRACT_KEY: paper_contract.to_payload(),
            "contract_scope_key": paper_contract.scope_key,
            "timeframe": str(row[2]),
            "side": str(row[3]),
            "order_type": str(row[4]),
            "status": str(row[5]),
            "qty": float(row[6]),
            "entry": float(row[7]),
            "stop_loss": float(row[8]) if row[8] is not None else None,
            "target": float(row[9]) if row[9] is not None else None,
            "use_stop_loss": bool(row[10]),
            "use_target": bool(row[11]),
            "created_at": ensure_utc(row[12]).isoformat() if row[12] is not None else None,
            "updated_at": ensure_utc(row[13]).isoformat() if row[13] is not None else None,
            "filled_at": ensure_utc(row[14]).isoformat() if row[14] is not None else None,
            "fill_price": float(row[15]) if row[15] is not None else None,
            "role": role,
            "position_id": str(row[17] or ""),
            "parent_order_id": str(row[18] or ""),
            "oco_group_id": str(row[19] or ""),
            "reduce_only": bool(row[20]),
        }
    )
    canonical_payload = require_paper_order_protection(order)
    if canonical_payload != order.get("payload"):
        raise ValueError("PAPER_ORDER_ROW_PROTECTION_NOT_CANONICAL")
    protection_signature = (
        canonical_payload[PAPER_PROTECTION_BASIS_KEY],
        canonical_payload.get(PAPER_PROTECTION_INTENT_KEY),
    )
    for command_key in ("create_command", "execution_command"):
        command = order.get(command_key)
        if not isinstance(command, Mapping):
            raise ValueError(f"PAPER_ORDER_ROW_{command_key.upper()}_REQUIRED")
        canonical_command_payload = require_paper_order_protection(command)
        if canonical_command_payload != command.get("payload"):
            raise ValueError(f"PAPER_ORDER_ROW_{command_key.upper()}_PROTECTION_NOT_CANONICAL")
        if (
            canonical_command_payload[PAPER_PROTECTION_BASIS_KEY],
            canonical_command_payload.get(PAPER_PROTECTION_INTENT_KEY),
        ) != protection_signature:
            raise ValueError("PAPER_ORDER_ROW_PROTECTION_COPIES_CONTRADICT")
        command_contract = require_paper_contract_identity(command)
        if command_contract != paper_contract:
            raise ValueError("PAPER_ORDER_ROW_CONTRACT_COPIES_CONTRADICT")
    return order


def _paper_position_from_row(row: Sequence[Any]) -> dict[str, Any]:
    if len(row) != 11:
        raise ValueError(f"PAPER_POSITION_ROW_SHAPE_INVALID expected=11 actual={len(row)}")
    position_id = require_exact_identity_text(
        row[0],
        field="PAPER_POSITION_ID",
    )
    payload = row[10]
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"PAPER_POSITION_ROW_PAYLOAD_INVALID expected=mapping actual={type(payload).__name__}"
        )
    instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="PAPER_POSITION_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="PAPER_POSITION_ROUTE_FINGERPRINT",
    )
    paper_contract = require_paper_contract_identity(payload)
    return {
        "id": position_id,
        "symbol": str(row[1]),
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        PAPER_CONTRACT_KEY: paper_contract.to_payload(),
        "contract_scope_key": paper_contract.scope_key,
        "timeframe": str(row[2]),
        "status": str(row[3]),
        "qty": float(row[4]),
        "avg_entry": float(row[5]) if row[5] is not None else None,
        "opened_at": ensure_utc(row[6]).isoformat() if row[6] is not None else None,
        "updated_at": ensure_utc(row[7]).isoformat() if row[7] is not None else None,
        "closed_at": ensure_utc(row[8]).isoformat() if row[8] is not None else None,
        "realized_pnl": float(row[9]),
        "payload": dict(payload),
    }


def _paper_fill_from_row(row: Sequence[Any]) -> dict[str, Any]:
    if len(row) != 13:
        raise ValueError(f"PAPER_FILL_ROW_SHAPE_INVALID expected=13 actual={len(row)}")
    payload = row[12]
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"PAPER_FILL_ROW_PAYLOAD_INVALID expected=mapping actual={type(payload).__name__}"
        )
    instrument_id = require_exact_identity_text(
        payload.get("instrument_id"),
        field="PAPER_FILL_INSTRUMENT_ID",
    )
    route_fingerprint = require_exact_identity_text(
        payload.get("route_fingerprint"),
        field="PAPER_FILL_ROUTE_FINGERPRINT",
    )
    paper_contract = require_paper_contract_identity(payload)
    return {
        "id": int(row[0]),
        "order_id": str(row[1]),
        "position_id": str(row[2] or ""),
        "symbol": str(row[3]),
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        PAPER_CONTRACT_KEY: paper_contract.to_payload(),
        "contract_scope_key": paper_contract.scope_key,
        "timeframe": str(row[4]),
        "side": str(row[5]),
        "qty": float(row[6]),
        "price": float(row[7]),
        "role": str(row[8] or ""),
        "reduce_only": bool(row[9]),
        "pnl_points": float(row[10]) if row[10] is not None else None,
        "filled_at": ensure_utc(row[11]).isoformat() if row[11] is not None else None,
        "payload": dict(payload),
    }
