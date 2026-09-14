from __future__ import annotations

import logging
from typing import Any


def exception_message(error: BaseException) -> str:
    """Return stable non-empty text for one exception."""

    text = str(error).strip()
    return text or error.__class__.__name__


def log_structured_error(
    logger: logging.Logger,
    *,
    provider: str,
    symbol: str,
    interval: str,
    range_: str,
    op: str,
    error: BaseException | str,
    level: str = "warning",
    event: str = "request_failed",
    **extra: Any,
) -> None:
    text = str(error).strip() if not isinstance(error, str) else error.strip()
    error_text = text or (error.__class__.__name__ if isinstance(error, BaseException) else "error")
    payload = {
        "provider": str(provider or ""),
        "symbol": str(symbol or ""),
        "interval": str(interval or ""),
        "range": str(range_ or ""),
        "op": str(op or ""),
        "error": error_text,
    }
    payload.update({str(key): value for key, value in extra.items()})
    fields = " ".join(f"{key}={value}" for key, value in payload.items())
    line = f"{event} {fields}".strip()
    log_method = getattr(logger, str(level or "warning").lower(), logger.warning)
    log_method(line)
