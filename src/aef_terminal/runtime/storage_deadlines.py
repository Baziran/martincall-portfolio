from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


POSTGRES_OPERATION_TIMEOUTS: ContextVar[tuple[int, int] | None] = ContextVar(
    "postgres_operation_timeouts",
    default=None,
)


@contextmanager
def postgres_operation_timeouts(
    *,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> Iterator[None]:
    """Carry one operation's PostgreSQL deadlines across task/thread boundaries."""

    statement_ms = int(statement_timeout_ms)
    lock_ms = int(lock_timeout_ms)
    if statement_ms <= 0:
        raise ValueError("statement_timeout_ms must be positive")
    if lock_ms <= 0 or lock_ms >= statement_ms:
        raise ValueError("lock_timeout_ms must be positive and lower than statement_timeout_ms")
    token = POSTGRES_OPERATION_TIMEOUTS.set((statement_ms, lock_ms))
    try:
        yield
    finally:
        POSTGRES_OPERATION_TIMEOUTS.reset(token)
