from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

from aef_terminal.runtime.storage_deadlines import POSTGRES_OPERATION_TIMEOUTS
from aef_terminal.storage.db_utils import DatabaseUnavailable


_LOGGER = logging.getLogger(__name__)
# Database-scoped, session-level lock identity for the one canonical MartinCall
# writer.  The two-key form keeps this operational lease separate from every
# transaction-scoped repository lock and does not depend on PostgreSQL hashtext
# implementation details.
_CANONICAL_WRITER_LEASE_NAMESPACE = 4_277_574  # ASCII "AEF"
_CANONICAL_WRITER_LEASE_KEY = 1_465_010_516  # ASCII "WRIT"
_CANONICAL_WRITER_FENCE_KEY = 1_178_947_139  # ASCII "FENC"


class CanonicalWriterCapability(StrEnum):
    """Explicit process capability for canonical PostgreSQL mutation."""

    LEASE_REQUIRED = "lease_required"
    TEST_BYPASS = "test_bypass"
    READ_ONLY = "read_only"


class CanonicalWriterLeaseUnavailable(RuntimeError):
    """Another process already owns the database's canonical-writer lease."""


class CanonicalWriterLeaseLost(RuntimeError):
    """The dedicated PostgreSQL session no longer owns the writer lease."""


class CanonicalWriterReadOnly(RuntimeError):
    """A read-only storage process attempted a canonical mutation."""


class PostgresConnectionMixin:
    """PostgreSQL repository mixin."""

    def __init__(
        self,
        dsn: str | None,
        connect_timeout: float = 3.0,
        *,
        schema_owner: bool = True,
        canonical_writer_capability: CanonicalWriterCapability = (
            CanonicalWriterCapability.LEASE_REQUIRED
        ),
    ) -> None:
        if not isinstance(canonical_writer_capability, CanonicalWriterCapability):
            raise TypeError("canonical_writer_capability must be CanonicalWriterCapability")
        if canonical_writer_capability is CanonicalWriterCapability.READ_ONLY and schema_owner:
            raise ValueError("READ_ONLY canonical writer capability requires schema_owner=False")
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self._schema_owner = bool(schema_owner)
        self._canonical_writer_capability = canonical_writer_capability
        self._initialized = False
        self._init_lock = threading.Lock()
        self._pool = None
        self._pool_lock = threading.Lock()
        self._settings_cache: dict[
            str,
            tuple[float, dict[str, Any], dict[str, dict[str, Any]], int, int],
        ] = {}
        self._settings_cache_ttl = 2.0
        self._bar_slots_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._bar_slots_cache_ttl = 3.0
        self._bar_slots_cache_lock = threading.Lock()
        self._bar_slots_cache_generation = 0
        self._canonical_writer_lease_lock = threading.RLock()
        self._canonical_writer_lease_connection = None
        self._canonical_writer_lease_owner: str | None = None
        self._canonical_writer_lease_state = "unacquired"
        self._canonical_writer_lease_loss: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.dsn)

    def _psycopg(self):
        if not self.dsn:
            raise DatabaseUnavailable("AEF_DATABASE_URL is not configured")
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise DatabaseUnavailable("psycopg is not installed") from exc
        return psycopg

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        self._psycopg()
        pool = getattr(self, "_pool", None)
        if pool is None:
            with self._pool_lock:
                pool = getattr(self, "_pool", None)
                if pool is None:
                    try:
                        from psycopg_pool import ConnectionPool
                    except ModuleNotFoundError as exc:
                        raise DatabaseUnavailable("psycopg_pool is not installed") from exc

                    self._pool = ConnectionPool(
                        self.dsn,
                        min_size=1,
                        max_size=8,
                        open=True,
                        kwargs={"autocommit": True, "connect_timeout": self.connect_timeout},
                    )
                    pool = self._pool
        operation_timeouts = POSTGRES_OPERATION_TIMEOUTS.get()
        # Schema initialization is a startup concern and may legitimately run
        # longer than a realtime commit deadline. Once initialized, every
        # connection acquired inside the scoped operation receives hard
        # server-side statement and lock deadlines.
        apply_timeouts = operation_timeouts is not None and (
            bool(getattr(self, "_initialized", False))
            or not bool(getattr(self, "_schema_owner", True))
        )
        pool_wait_timeout = (
            max(min(float(self.connect_timeout), operation_timeouts[0] / 1000.0), 0.1)
            if apply_timeouts
            else None
        )
        with pool.connection(timeout=pool_wait_timeout) as conn:
            if apply_timeouts:
                statement_ms, lock_ms = operation_timeouts
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            set_config('statement_timeout', %s, false),
                            set_config('lock_timeout', %s, false)
                        """,
                        (f"{statement_ms}ms", f"{lock_ms}ms"),
                    )
            try:
                yield conn
            finally:
                if apply_timeouts:
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                SELECT
                                    set_config('statement_timeout', '0', false),
                                    set_config('lock_timeout', '0', false)
                                """
                            )
                    except Exception:
                        # Never return a connection with unknown session settings
                        # to the shared pool. Closing lets the pool replace it.
                        _LOGGER.warning(
                            "PostgreSQL connection timeout reset failed; discarding connection",
                            exc_info=True,
                        )
                        try:
                            conn.close()
                        except Exception:
                            _LOGGER.warning(
                                "PostgreSQL connection close failed after timeout reset failure",
                                exc_info=True,
                            )

    def reserve_bar_revision_sequence(self) -> int:
        """Reserve the durable order token for one logical canonical-bar commit."""

        with self._canonical_write_cursor() as cur:
            cur.execute("SELECT nextval('canonical_bar_revision_sequence')")
            row = cur.fetchone()
        sequence = int(row[0]) if row is not None else 0
        if sequence <= 0:
            raise RuntimeError("CANONICAL_BAR_REVISION_SEQUENCE_UNAVAILABLE")
        return sequence

    def acquire_canonical_writer_lease(self, *, owner: str) -> None:
        """Acquire and retain the database's exclusive canonical-writer lease.

        The lock lives on a dedicated connection rather than a pooled
        connection: returning a session-level advisory lock to the pool would
        make its lifetime implicit and unsafe.  Callers acquire it before
        schema bootstrap/verification and before starting any provider or
        background writer.
        """

        owner_key = str(owner or "").strip()
        if not owner_key:
            raise ValueError("canonical writer lease owner is required")
        with self._canonical_writer_lease_lock:
            if self._canonical_writer_capability is not CanonicalWriterCapability.LEASE_REQUIRED:
                raise RuntimeError(
                    "CANONICAL_WRITER_LEASE_CAPABILITY_MISMATCH "
                    f"capability={self._canonical_writer_capability.value}"
                )
            if self._canonical_writer_lease_loss is not None:
                raise CanonicalWriterLeaseLost(self._canonical_writer_lease_loss)
            current = self._canonical_writer_lease_connection
            if current is not None:
                if self._canonical_writer_lease_owner != owner_key:
                    raise RuntimeError(
                        "CANONICAL_WRITER_LEASE_ALREADY_OWNED "
                        f"owner={self._canonical_writer_lease_owner!r}"
                    )
                self._verify_canonical_writer_lease_locked()
                return

            psycopg = self._psycopg()
            application_name = f"martincall-writer:{owner_key}"[:63]
            connection = None
            try:
                connection = psycopg.connect(
                    self.dsn,
                    autocommit=True,
                    connect_timeout=self.connect_timeout,
                    application_name=application_name,
                )
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_try_advisory_lock(%s, %s)",
                        (
                            _CANONICAL_WRITER_LEASE_NAMESPACE,
                            _CANONICAL_WRITER_LEASE_KEY,
                        ),
                    )
                    leader_row = cur.fetchone()
                    if leader_row != (True,):
                        raise CanonicalWriterLeaseUnavailable(
                            "CANONICAL_WRITER_LEASE_BUSY "
                            f"owner={owner_key!r}; stop the MartinCall application and "
                            "all canonical TimescaleDB writers before retrying"
                        )
                    cur.execute(
                        "SELECT pg_try_advisory_lock(%s, %s)",
                        (
                            _CANONICAL_WRITER_LEASE_NAMESPACE,
                            _CANONICAL_WRITER_FENCE_KEY,
                        ),
                    )
                    fence_row = cur.fetchone()
                    if fence_row != (True,):
                        raise CanonicalWriterLeaseUnavailable(
                            "CANONICAL_WRITER_FENCE_BUSY "
                            f"owner={owner_key!r}; an older canonical transaction "
                            "is still active"
                        )
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s, %s)",
                        (
                            _CANONICAL_WRITER_LEASE_NAMESPACE,
                            _CANONICAL_WRITER_FENCE_KEY,
                        ),
                    )
                    if cur.fetchone() != (True,):
                        raise RuntimeError("CANONICAL_WRITER_FENCE_RELEASE_FAILED")
            except CanonicalWriterLeaseUnavailable:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        _LOGGER.warning(
                            "Busy canonical writer lease connection cleanup failed owner=%r",
                            owner_key,
                            exc_info=True,
                        )
                raise
            except Exception as exc:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        _LOGGER.warning(
                            "Canonical writer lease connection cleanup failed owner=%r",
                            owner_key,
                            exc_info=True,
                        )
                raise CanonicalWriterLeaseUnavailable(
                    "CANONICAL_WRITER_LEASE_ACQUIRE_FAILED "
                    f"owner={owner_key!r}: {type(exc).__name__}: {exc}"
                ) from exc
            self._canonical_writer_lease_connection = connection
            self._canonical_writer_lease_owner = owner_key
            self._canonical_writer_lease_state = "held"

    def _latch_canonical_writer_lease_loss_locked(
        self,
        message: str,
        *,
        connection: Any | None,
    ) -> None:
        self._canonical_writer_lease_connection = None
        self._canonical_writer_lease_owner = None
        self._canonical_writer_lease_state = "lost"
        self._canonical_writer_lease_loss = message
        if connection is not None:
            try:
                connection.close()
            except Exception:
                _LOGGER.warning(
                    "Lost canonical writer lease connection close failed",
                    exc_info=True,
                )

    def _verify_canonical_writer_lease_locked(self) -> None:
        connection = self._canonical_writer_lease_connection
        owner = self._canonical_writer_lease_owner
        if connection is None or not owner:
            raise CanonicalWriterLeaseLost(
                self._canonical_writer_lease_loss or "CANONICAL_WRITER_LEASE_REQUIRED"
            )
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks
                        WHERE locktype = 'advisory'
                          AND pid = pg_backend_pid()
                          AND database = (
                              SELECT oid FROM pg_database WHERE datname = current_database()
                          )
                          AND classid = %s::oid
                          AND objid = %s::oid
                          AND objsubid = 2
                          AND granted
                    )
                    """,
                    (
                        _CANONICAL_WRITER_LEASE_NAMESPACE,
                        _CANONICAL_WRITER_LEASE_KEY,
                    ),
                )
                row = cur.fetchone()
        except Exception as exc:
            message = f"CANONICAL_WRITER_LEASE_LOST owner={owner!r}: {type(exc).__name__}: {exc}"
            self._latch_canonical_writer_lease_loss_locked(
                message,
                connection=connection,
            )
            raise CanonicalWriterLeaseLost(message) from exc
        if row != (True,):
            message = f"CANONICAL_WRITER_LEASE_LOST owner={owner!r}"
            self._latch_canonical_writer_lease_loss_locked(
                message,
                connection=connection,
            )
            raise CanonicalWriterLeaseLost(message)

    def verify_canonical_writer_lease(self) -> None:
        """Fail closed unless this store's dedicated session still owns the lease."""

        with self._canonical_writer_lease_lock:
            self._verify_canonical_writer_lease_locked()

    def _require_canonical_writer_lease(self) -> None:
        capability = getattr(
            self,
            "_canonical_writer_capability",
            CanonicalWriterCapability.LEASE_REQUIRED,
        )
        if capability is CanonicalWriterCapability.TEST_BYPASS:
            return
        if capability is CanonicalWriterCapability.READ_ONLY:
            raise CanonicalWriterReadOnly("CANONICAL_WRITER_READ_ONLY")
        self.verify_canonical_writer_lease()

    def _acquire_canonical_writer_fence_on_cursor(self, cur: Any) -> None:
        """Fence one canonical transaction against leader replacement."""

        capability = getattr(
            self,
            "_canonical_writer_capability",
            CanonicalWriterCapability.LEASE_REQUIRED,
        )
        if capability is CanonicalWriterCapability.TEST_BYPASS:
            return
        self._require_canonical_writer_lease()
        cur.execute(
            "SELECT pg_advisory_xact_lock_shared(%s, %s)",
            (
                _CANONICAL_WRITER_LEASE_NAMESPACE,
                _CANONICAL_WRITER_FENCE_KEY,
            ),
        )
        # This second verification is inside the fenced transaction and before
        # any canonical mutation.  It closes the race where a replacement
        # leader completed its exclusive barrier after our outer preflight.
        self.verify_canonical_writer_lease()

    @contextmanager
    def _canonical_write_cursor(self) -> Iterator[Any]:
        """Yield a lease-verified cursor inside the canonical fenced transaction."""

        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                yield cur

    def canonical_writer_lease_status(self, *, verify: bool = False) -> dict[str, Any]:
        capability = self._canonical_writer_capability
        if capability is CanonicalWriterCapability.TEST_BYPASS:
            return {
                "capability": capability.value,
                "state": "test_bypass",
                "ok": True,
            }
        if capability is CanonicalWriterCapability.READ_ONLY:
            return {
                "capability": capability.value,
                "state": "read_only",
                "ok": True,
            }
        with self._canonical_writer_lease_lock:
            if verify and self._canonical_writer_lease_state == "held":
                try:
                    self._verify_canonical_writer_lease_locked()
                except CanonicalWriterLeaseLost:
                    pass
            state = self._canonical_writer_lease_state
            message = self._canonical_writer_lease_loss or (
                "canonical writer lease held"
                if state == "held"
                else "CANONICAL_WRITER_LEASE_REQUIRED"
            )
            return {
                "capability": capability.value,
                "state": state,
                "ok": state == "held",
                "owner": self._canonical_writer_lease_owner,
                "message": message,
            }

    def release_canonical_writer_lease(self) -> None:
        """Release the session lease after every runtime writer has drained."""

        with self._canonical_writer_lease_lock:
            connection = self._canonical_writer_lease_connection
            owner = self._canonical_writer_lease_owner
            self._canonical_writer_lease_connection = None
            self._canonical_writer_lease_owner = None
            if connection is None:
                return
            self._canonical_writer_lease_state = "releasing"
            release_error = ""
            try:
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s, %s)",
                        (
                            _CANONICAL_WRITER_LEASE_NAMESPACE,
                            _CANONICAL_WRITER_LEASE_KEY,
                        ),
                    )
                    row = cur.fetchone()
                if row != (True,):
                    release_error = "CANONICAL_WRITER_LEASE_RELEASE_NOT_OWNED"
                    _LOGGER.critical(
                        "Canonical writer lease was not owned during release owner=%r",
                        owner,
                    )
            except Exception as exc:
                release_error = (
                    "CANONICAL_WRITER_LEASE_RELEASE_FAILED "
                    f"owner={owner!r}: {type(exc).__name__}: {exc}"
                )
                # Closing the dedicated PostgreSQL session is itself an
                # authoritative release even when the explicit unlock cannot be
                # observed because the connection was already lost.
                _LOGGER.warning(
                    "Canonical writer lease release failed; closing dedicated session owner=%r",
                    owner,
                    exc_info=True,
                )
            finally:
                try:
                    connection.close()
                except Exception:
                    _LOGGER.warning(
                        "Canonical writer lease connection close failed owner=%r",
                        owner,
                        exc_info=True,
                    )
            if release_error:
                self._canonical_writer_lease_state = "lost"
                self._canonical_writer_lease_loss = release_error
            else:
                self._canonical_writer_lease_state = "released"

    def _invalidate_bar_slots_cache(self) -> None:
        with self._bar_slots_cache_lock:
            self._bar_slots_cache_generation += 1
            self._bar_slots_cache.clear()

    def close(self) -> None:
        self.release_canonical_writer_lease()
        pool = getattr(self, "_pool", None)
        if pool is None:
            return
        with self._pool_lock:
            pool = getattr(self, "_pool", None)
            if pool is None:
                return
            self._pool = None
        try:
            pool.close()
        except Exception:
            _LOGGER.warning("PostgreSQL connection pool close failed", exc_info=True)
