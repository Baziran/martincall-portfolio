from __future__ import annotations

import logging

from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts
from aef_terminal.storage.repos.connection import PostgresConnectionMixin


def test_pool_close_failure_is_reported_after_pool_is_detached(caplog) -> None:
    class FailingPool:
        def close(self) -> None:
            raise RuntimeError("pool close rejected")

    repo = PostgresConnectionMixin("postgresql://unused")
    pool = FailingPool()
    repo._pool = pool
    caplog.set_level(
        logging.WARNING,
        logger="aef_terminal.storage.repos.connection",
    )

    repo.close()

    assert repo._pool is None
    assert "PostgreSQL connection pool close failed" in caplog.text


def test_timeout_reset_cleanup_reports_failures_without_failing_completed_operation(
    caplog,
) -> None:
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query: str, _params=None) -> None:
            if "set_config('statement_timeout', '0', false)" in query:
                raise RuntimeError("timeout reset rejected")

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            raise RuntimeError("connection close rejected")

    connection = Connection()

    class ConnectionContext:
        def __enter__(self) -> Connection:
            return connection

        def __exit__(self, *_args) -> None:
            return None

    class Pool:
        def connection(self, *, timeout: float) -> ConnectionContext:
            assert timeout == 2.5
            return ConnectionContext()

    repo = PostgresConnectionMixin(
        "postgresql://unused",
        connect_timeout=2.5,
    )
    repo._pool = Pool()
    repo._initialized = True
    repo._psycopg = lambda: None  # type: ignore[method-assign]
    caplog.set_level(
        logging.WARNING,
        logger="aef_terminal.storage.repos.connection",
    )

    with postgres_operation_timeouts(
        statement_timeout_ms=8_000,
        lock_timeout_ms=2_000,
    ):
        with repo._connect() as acquired:
            assert acquired is connection

    assert "PostgreSQL connection timeout reset failed" in caplog.text
    assert "PostgreSQL connection close failed after timeout reset failure" in caplog.text
