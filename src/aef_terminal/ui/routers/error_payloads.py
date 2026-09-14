from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def build_stream_status_payload(
    *,
    status_type: str,
    source: str,
    code: str,
    category: str,
    retryable: bool,
    error: BaseException | str,
    retry_in_seconds: float,
    ts: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    message = str(error).strip() if not isinstance(error, str) else str(error).strip()
    error_message = message or "error"
    payload: dict[str, Any] = {
        "type": status_type,
        "source": source,
        "ok": False,
        "message": error_message,
        "error": {
            "code": code,
            "category": category,
            "retryable": bool(retryable),
            "message": error_message,
        },
        "retry_in_seconds": round(max(float(retry_in_seconds), 0.1), 1),
        "ts": (ts or datetime.now(tz=UTC)).isoformat(),
    }
    payload.update(extra)
    return payload


def build_action_error_response(
    *,
    code: str,
    category: str,
    retryable: bool,
    error: BaseException | str,
    **extra: Any,
) -> dict[str, Any]:
    payload = build_error_payload(
        code=code,
        category=category,
        retryable=retryable,
        error=error,
        include_message_field=True,
    )
    payload.update(extra)
    return payload


def build_error_payload(
    *,
    code: str,
    category: str,
    retryable: bool,
    error: BaseException | str,
    include_message_field: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    message = str(error).strip() if not isinstance(error, str) else str(error).strip()
    error_message = message or "error"
    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "category": category,
            "retryable": bool(retryable),
            "message": error_message,
        },
    }
    if include_message_field:
        payload["message"] = error_message
    payload.update(extra)
    return payload
