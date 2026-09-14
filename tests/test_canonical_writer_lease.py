from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.check_futures_contract_alignment as alignment_check
import scripts.check_storage_runtime_contract as storage_contract_check
import scripts.ibkr_tick_feed as tick_feed
import scripts.refresh_watchlist_current_contracts as refresh_contracts
from aef_terminal.storage.postgres import PostgresStore
from aef_terminal.storage.repos import bars as bars_repo
from aef_terminal.storage.repos import connection as connection_repo
from aef_terminal.storage.repos import discord_signals as discord_signals_repo
from aef_terminal.storage.repos import drawings as drawings_repo
from aef_terminal.storage.repos import futures as futures_repo
from aef_terminal.storage.repos import gex as gex_repo
from aef_terminal.storage.repos import history_commits as history_commits_repo
from aef_terminal.storage.repos import instruments as instruments_repo
from aef_terminal.storage.repos import paper as paper_repo
from aef_terminal.storage.repos import schema as schema_repo
from aef_terminal.storage.repos import settings as settings_repo
from aef_terminal.storage.repos import ticks as ticks_repo
from aef_terminal.storage.repos import trading_hours as trading_hours_repo
from aef_terminal.storage.repos.connection import (
    CanonicalWriterCapability,
    CanonicalWriterLeaseLost,
    CanonicalWriterLeaseUnavailable,
    CanonicalWriterReadOnly,
    PostgresConnectionMixin,
)
from aef_terminal.ui import app as ui_app
from aef_terminal.ui.system_status import SystemStatusService, readiness_status_snapshot


class _LeaseCursor:
    def __init__(self, connection: "_LeaseConnection") -> None:
        self.connection = connection
        self.query = ""
        self.params = None

    def __enter__(self) -> "_LeaseCursor":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params=None) -> None:
        self.query = " ".join(str(query).split())
        self.params = params
        self.connection.calls.append((self.query, params))
        if "pg_advisory_xact_lock_shared" in self.query:
            self.connection.shared_fence_acquired = True

    def fetchone(self):
        if "pg_try_advisory_lock" in self.query:
            key = self.params[1]
            if key == connection_repo._CANONICAL_WRITER_LEASE_KEY:
                acquired = self.connection.leader_available
                self.connection.leader_held = acquired
                return (acquired,)
            if key == connection_repo._CANONICAL_WRITER_FENCE_KEY:
                acquired = self.connection.fence_available
                self.connection.fence_held = acquired
                return (acquired,)
            raise AssertionError(f"unexpected advisory lock key: {key}")
        if "FROM pg_locks" in self.query:
            if self.connection.verify_rows:
                return (self.connection.verify_rows.pop(0),)
            return (self.connection.leader_held,)
        if "pg_advisory_unlock" in self.query:
            key = self.params[1]
            if key == connection_repo._CANONICAL_WRITER_LEASE_KEY:
                was_held = self.connection.leader_held
                self.connection.leader_held = False
                return (was_held,)
            if key == connection_repo._CANONICAL_WRITER_FENCE_KEY:
                was_held = self.connection.fence_held
                self.connection.fence_held = False
                return (was_held,)
            raise AssertionError(f"unexpected advisory unlock key: {key}")
            return (was_held,)
        raise AssertionError(f"unexpected lease query: {self.query}")


class _LeaseConnection:
    def __init__(
        self,
        *,
        leader_available: bool = True,
        fence_available: bool = True,
    ) -> None:
        self.leader_available = leader_available
        self.fence_available = fence_available
        self.leader_held = False
        self.fence_held = False
        self.shared_fence_acquired = False
        self.verify_rows: list[bool] = []
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def cursor(self) -> _LeaseCursor:
        return _LeaseCursor(self)

    def close(self) -> None:
        self.closed = True
        self.leader_held = False
        self.fence_held = False


class _FakePsycopg:
    def __init__(self, connection: _LeaseConnection) -> None:
        self.connection = connection
        self.connect_calls: list[tuple[object, dict[str, object]]] = []

    def connect(self, dsn, **kwargs):
        self.connect_calls.append((dsn, kwargs))
        return self.connection


def test_postgres_canonical_writer_lease_uses_one_dedicated_session() -> None:
    connection = _LeaseConnection()
    psycopg = _FakePsycopg(connection)
    store = PostgresConnectionMixin("postgresql://lease-test", connect_timeout=2.5)
    store._psycopg = lambda: psycopg  # type: ignore[method-assign]

    store.acquire_canonical_writer_lease(owner="test-app")
    store.verify_canonical_writer_lease()
    store.release_canonical_writer_lease()

    assert psycopg.connect_calls == [
        (
            "postgresql://lease-test",
            {
                "autocommit": True,
                "connect_timeout": 2.5,
                "application_name": "martincall-writer:test-app",
            },
        )
    ]
    queries = [query for query, _params in connection.calls]
    assert queries[0] == "SELECT pg_try_advisory_lock(%s, %s)"
    assert queries[1] == "SELECT pg_try_advisory_lock(%s, %s)"
    assert queries[2] == "SELECT pg_advisory_unlock(%s, %s)"
    assert "FROM pg_locks" in queries[3]
    assert queries[4] == "SELECT pg_advisory_unlock(%s, %s)"
    assert [params[1] for _query, params in connection.calls[:3]] == [
        connection_repo._CANONICAL_WRITER_LEASE_KEY,
        connection_repo._CANONICAL_WRITER_FENCE_KEY,
        connection_repo._CANONICAL_WRITER_FENCE_KEY,
    ]
    assert connection.closed is True


def test_postgres_canonical_writer_lease_busy_and_loss_fail_closed() -> None:
    busy_connection = _LeaseConnection(leader_available=False)
    busy_store = PostgresConnectionMixin("postgresql://lease-test")
    busy_store._psycopg = lambda: _FakePsycopg(  # type: ignore[method-assign]
        busy_connection
    )

    with pytest.raises(CanonicalWriterLeaseUnavailable, match="CANONICAL_WRITER_LEASE_BUSY"):
        busy_store.acquire_canonical_writer_lease(owner="offline-backfill")
    assert busy_connection.closed is True

    lost_connection = _LeaseConnection()
    lost_store = PostgresConnectionMixin("postgresql://lease-test")
    lost_store._psycopg = lambda: _FakePsycopg(  # type: ignore[method-assign]
        lost_connection
    )
    lost_store.acquire_canonical_writer_lease(owner="test-app")
    lost_connection.leader_held = False

    with pytest.raises(CanonicalWriterLeaseLost, match="CANONICAL_WRITER_LEASE_LOST"):
        lost_store.reserve_bar_revision_sequence()
    assert lost_connection.closed is True
    with pytest.raises(CanonicalWriterLeaseLost, match="CANONICAL_WRITER_LEASE_LOST"):
        lost_store.reserve_bar_revision_sequence()


def test_postgres_new_leader_fails_fast_while_old_fenced_transaction_is_active() -> None:
    connection = _LeaseConnection(fence_available=False)
    store = PostgresConnectionMixin("postgresql://lease-test")
    store._psycopg = lambda: _FakePsycopg(connection)  # type: ignore[method-assign]

    with pytest.raises(CanonicalWriterLeaseUnavailable, match="CANONICAL_WRITER_FENCE_BUSY"):
        store.acquire_canonical_writer_lease(owner="replacement-app")

    assert connection.closed is True
    assert connection.leader_held is False
    assert [query for query, _params in connection.calls] == [
        "SELECT pg_try_advisory_lock(%s, %s)",
        "SELECT pg_try_advisory_lock(%s, %s)",
    ]


def test_postgres_write_fence_reverifies_leader_after_shared_lock() -> None:
    connection = _LeaseConnection()
    store = PostgresConnectionMixin("postgresql://lease-test")
    store._psycopg = lambda: _FakePsycopg(connection)  # type: ignore[method-assign]
    store.acquire_canonical_writer_lease(owner="old-app")
    connection.verify_rows = [True, False]
    cursor = _LeaseCursor(connection)

    with pytest.raises(CanonicalWriterLeaseLost, match="CANONICAL_WRITER_LEASE_LOST"):
        store._acquire_canonical_writer_fence_on_cursor(cursor)

    tail = [query for query, _params in connection.calls[-3:]]
    assert "FROM pg_locks" in tail[0]
    assert tail[1] == "SELECT pg_advisory_xact_lock_shared(%s, %s)"
    assert "FROM pg_locks" in tail[2]
    assert connection.shared_fence_acquired is True
    assert connection.closed is True
    assert store.canonical_writer_lease_status()["state"] == "lost"


def test_canonical_writer_capability_is_explicit_and_default_fails_closed() -> None:
    default_store = PostgresConnectionMixin(None)
    with pytest.raises(CanonicalWriterLeaseLost, match="CANONICAL_WRITER_LEASE_REQUIRED"):
        default_store._require_canonical_writer_lease()

    read_only_store = PostgresConnectionMixin(
        None,
        schema_owner=False,
        canonical_writer_capability=CanonicalWriterCapability.READ_ONLY,
    )
    with pytest.raises(CanonicalWriterReadOnly, match="CANONICAL_WRITER_READ_ONLY"):
        read_only_store._require_canonical_writer_lease()

    test_store = PostgresConnectionMixin(
        None,
        canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
    )
    cursor = _LeaseCursor(_LeaseConnection())
    test_store._require_canonical_writer_lease()
    test_store._acquire_canonical_writer_fence_on_cursor(cursor)
    assert cursor.connection.calls == []
    assert "PYTEST" not in inspect.getsource(PostgresConnectionMixin)

    with pytest.raises(ValueError, match="requires schema_owner=False"):
        PostgresConnectionMixin(
            None,
            canonical_writer_capability=CanonicalWriterCapability.READ_ONLY,
        )


def test_alignment_utility_is_explicitly_non_schema_owner_and_read_only() -> None:
    source = inspect.getsource(alignment_check.main)

    assert "schema_owner=False" in source
    assert "canonical_writer_capability=CanonicalWriterCapability.READ_ONLY" in source
    for forbidden_mutation in (
        ".write_bars(",
        ".write_futures_",
        ".replace_futures_",
        ".upsert_",
        ".add_instrument_",
        ".remove_instrument_",
        ".set_",
    ):
        assert forbidden_mutation not in source


def test_storage_preflight_is_explicitly_non_schema_owner_and_read_only() -> None:
    source = inspect.getsource(storage_contract_check.main)

    assert "schema_owner=False" in source
    assert "canonical_writer_capability=CanonicalWriterCapability.READ_ONLY" in source
    assert "store.verify_storage_runtime_contract()" in source
    for forbidden_mutation in (
        ".initialize(",
        ".write_",
        ".upsert_",
        ".delete_",
        ".acquire_canonical_writer_lease(",
    ):
        assert forbidden_mutation not in source


def test_runtime_store_initialization_belongs_only_to_app_lifespan() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    source_root = repository_root / "src" / "aef_terminal"
    owners: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Attribute) and node.attr == "initialize" for node in ast.walk(tree)
        ):
            owners.append(path.relative_to(repository_root).as_posix())

    assert owners == ["src/aef_terminal/ui/app.py"]


def test_test_bypass_has_no_production_caller() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    connection_owner = Path("src/aef_terminal/storage/repos/connection.py")
    offenders: list[str] = []
    for source_root in (repository_root / "src", repository_root / "scripts"):
        for path in source_root.rglob("*.py"):
            relative = path.relative_to(repository_root)
            if relative == connection_owner:
                continue
            if "CanonicalWriterCapability.TEST_BYPASS" in path.read_text(encoding="utf-8"):
                offenders.append(relative.as_posix())

    assert offenders == []


def test_postgres_operator_scripts_declare_one_supported_writer_mode() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    expected = {
        "check_futures_contract_alignment.py": (
            "CanonicalWriterCapability.READ_ONLY",
            "schema_owner=False",
        ),
        "check_storage_runtime_contract.py": (
            "CanonicalWriterCapability.READ_ONLY",
            "schema_owner=False",
            "store.verify_storage_runtime_contract()",
        ),
        "ibkr_tick_feed.py": (
            "store.acquire_canonical_writer_lease",
            "store.release_canonical_writer_lease",
        ),
        "refresh_watchlist_current_contracts.py": (
            'owner="refresh-watchlist-current-contracts"',
            "store.release_canonical_writer_lease()",
        ),
    }
    postgres_store_scripts = {
        path.name: path.read_text(encoding="utf-8")
        for path in (repository_root / "scripts").glob("*.py")
        if "PostgresStore(" in path.read_text(encoding="utf-8")
    }

    assert set(postgres_store_scripts) == set(expected)
    for filename, required_fragments in expected.items():
        for fragment in required_fragments:
            assert fragment in postgres_store_scripts[filename]


def test_all_canonical_storage_write_entrypoints_require_elected_lease_and_fence() -> None:
    owners = (
        PostgresConnectionMixin.reserve_bar_revision_sequence,
        bars_repo.BarsRepoMixin.write_bars,
        discord_signals_repo.DiscordSignalsRepoMixin.upsert_discord_signal_messages,
        discord_signals_repo.DiscordSignalsRepoMixin.mark_discord_signal_message_deleted,
        futures_repo.FuturesRepoMixin.write_futures_contract,
        futures_repo.FuturesRepoMixin.write_futures_roll_event,
        futures_repo.FuturesRepoMixin.write_futures_contract_bars,
        futures_repo.FuturesRepoMixin.write_futures_canonical_bars,
        history_commits_repo.HistoryCommitsRepoMixin.commit_history_result,
        drawings_repo.DrawingsRepoMixin.replace_drawings,
        drawings_repo.DrawingsRepoMixin.delete_price_alert_scope_exact,
        drawings_repo.DrawingsRepoMixin.update_price_alert_payload_exact,
        drawings_repo.DrawingsRepoMixin.create_or_rearm_price_alert_exact,
        drawings_repo.DrawingsRepoMixin.rearm_price_alert_exact,
        drawings_repo.DrawingsRepoMixin.set_price_alert_enabled_exact,
        drawings_repo.DrawingsRepoMixin.delete_price_alert_exact,
        gex_repo.GexRepoMixin.create_option_target_intent,
        gex_repo.GexRepoMixin.update_option_target_intent,
        gex_repo.GexRepoMixin.update_option_target_market_samples,
        gex_repo.GexRepoMixin.prune_market_snapshot_history,
        gex_repo.GexRepoMixin.delete_option_target,
        gex_repo.GexRepoMixin.upsert_gex_snapshot,
        gex_repo.GexRepoMixin.upsert_quote_snapshots,
        instruments_repo.InstrumentsRepoMixin.select_watchlist_instrument,
        instruments_repo.InstrumentsRepoMixin.set_watchlist_presentation,
        instruments_repo.InstrumentsRepoMixin.remove_instrument_from_watchlist,
        instruments_repo.InstrumentsRepoMixin.reorder_watchlist,
        paper_repo.PaperRepoMixin.upsert_paper_order,
        paper_repo.PaperRepoMixin.update_paper_order_if_pending,
        paper_repo.PaperRepoMixin.cancel_paper_order_if_pending,
        paper_repo.PaperRepoMixin.reject_paper_order_execution_if_pending,
        paper_repo.PaperRepoMixin.apply_filled_paper_order,
        paper_repo.PaperRepoMixin.cancel_paper_protective_orders,
        paper_repo.PaperRepoMixin.update_paper_protective_levels,
        schema_repo.SchemaRepoMixin.initialize,
        settings_repo.SettingsRepoMixin.upsert_setting,
        settings_repo.SettingsRepoMixin.upsert_settings,
        ticks_repo.TicksRepoMixin._rollup_and_purge_ticks,
        ticks_repo.TicksRepoMixin.write_ticks,
        trading_hours_repo.TradingHoursRepoMixin.delete_trading_hours,
        trading_hours_repo.TradingHoursRepoMixin.upsert_trading_hours,
    )

    for owner in owners:
        source = inspect.getsource(owner)
        central_boundary = "self._canonical_write_cursor()" in source
        explicit_boundary = (
            "self._require_canonical_writer_lease()" in source
            and "conn.transaction()" in source
            and "self._acquire_canonical_writer_fence_on_cursor(cur)" in source
        )
        assert central_boundary or explicit_boundary, owner.__qualname__
    central_source = inspect.getsource(PostgresConnectionMixin._canonical_write_cursor)
    assert central_source.index("self._require_canonical_writer_lease()") < central_source.index(
        "self._connect()"
    )
    assert "with conn.transaction(), conn.cursor() as cur:" in central_source
    assert central_source.index("self._acquire_canonical_writer_fence_on_cursor(cur)") < (
        central_source.index("yield cur")
    )
    assert not hasattr(
        futures_repo.FuturesRepoMixin,
        "delete_futures_canonical_window",
    )


def test_app_lifespan_holds_writer_lease_around_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Store:
        def initialize(self) -> None:
            events.append("initialize")

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            assert owner == "martincall-app"
            events.append("lease-acquire")

        def verify_canonical_writer_lease(self) -> None:
            events.append("lease-verify")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    store = Store()
    monkeypatch.setattr(ui_app, "default_postgres_store", lambda: store)
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: events.append("route-refresh"),
    )
    monkeypatch.setattr(
        ui_app._ibkr_runtime,
        "apply_persisted_ibkr_runtime_settings",
        lambda: events.append("ibkr-settings"),
    )

    async def start_runtime() -> None:
        events.append("runtime-start")

    async def stop_runtime() -> None:
        events.append("runtime-stop-drained")

    monkeypatch.setattr(ui_app, "start_server_alert_monitor", start_runtime)
    monkeypatch.setattr(ui_app, "stop_server_alert_monitor", stop_runtime)

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    asyncio.run(exercise())

    assert events == [
        "lease-acquire",
        "initialize",
        "lease-verify",
        "route-refresh",
        "lease-verify",
        "ibkr-settings",
        "runtime-start",
        "serving",
        "runtime-stop-drained",
        "lease-release",
    ]


def test_app_lifespan_busy_lease_starts_no_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def initialize(self) -> None:
            events.append("initialize")

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")
            raise CanonicalWriterLeaseUnavailable("CANONICAL_WRITER_LEASE_BUSY")

    monkeypatch.setattr(ui_app, "default_postgres_store", lambda: Store())
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: events.append("route-refresh"),
    )

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    with pytest.raises(CanonicalWriterLeaseUnavailable):
        asyncio.run(exercise())
    assert events == ["lease-acquire:martincall-app"]


def test_app_lifespan_schema_failure_releases_writer_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")

        def initialize(self) -> None:
            events.append("initialize")
            raise RuntimeError("schema invalid")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    monkeypatch.setattr(ui_app, "default_postgres_store", lambda: Store())
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: events.append("route-refresh"),
    )

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    with pytest.raises(RuntimeError, match="schema invalid"):
        asyncio.run(exercise())
    assert events == [
        "lease-acquire:martincall-app",
        "initialize",
        "lease-release",
    ]


@pytest.mark.parametrize(
    "blocked_stage",
    ("lease-acquire", "initialize", "route-refresh"),
)
def test_app_startup_cancellation_settles_physical_stage_before_release(
    monkeypatch: pytest.MonkeyPatch,
    blocked_stage: str,
) -> None:
    events: list[str] = []
    stage_started = threading.Event()
    allow_stage_to_finish = threading.Event()

    def operation(stage: str) -> None:
        events.append(f"{stage}-start")
        if stage == blocked_stage:
            stage_started.set()
            if not allow_stage_to_finish.wait(timeout=2.0):
                raise AssertionError(f"timed out waiting to finish {stage}")
        events.append(f"{stage}-done")

    class Store:
        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            assert owner == "martincall-app"
            operation("lease-acquire")

        def initialize(self) -> None:
            operation("initialize")

        def verify_canonical_writer_lease(self) -> None:
            events.append("lease-verify")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    monkeypatch.setattr(ui_app, "default_postgres_store", Store)
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: operation("route-refresh"),
    )
    monkeypatch.setattr(
        ui_app._ibkr_runtime,
        "apply_persisted_ibkr_runtime_settings",
        lambda: events.append("ibkr-settings"),
    )

    async def start_runtime() -> None:
        events.append("runtime-start")

    monkeypatch.setattr(ui_app, "start_server_alert_monitor", start_runtime)

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    async def scenario() -> None:
        task = asyncio.create_task(exercise())
        started = await asyncio.to_thread(stage_started.wait, 1.0)
        assert started is True
        task.cancel()
        await asyncio.sleep(0)
        assert "lease-release" not in events
        allow_stage_to_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert events.index(f"{blocked_stage}-done") < events.index("lease-release")
    assert "runtime-start" not in events
    assert "serving" not in events


def test_app_latched_lease_loss_during_startup_blocks_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        verify_count = 0

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")

        def initialize(self) -> None:
            events.append("initialize")

        def verify_canonical_writer_lease(self) -> None:
            self.verify_count += 1
            events.append(f"lease-verify:{self.verify_count}")
            if self.verify_count == 2:
                raise CanonicalWriterLeaseLost("CANONICAL_WRITER_LEASE_LOST")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    monkeypatch.setattr(ui_app, "default_postgres_store", Store)
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: events.append("route-refresh"),
    )
    monkeypatch.setattr(
        ui_app._ibkr_runtime,
        "apply_persisted_ibkr_runtime_settings",
        lambda: events.append("ibkr-settings"),
    )

    async def start_runtime() -> None:
        events.append("runtime-start")

    monkeypatch.setattr(ui_app, "start_server_alert_monitor", start_runtime)

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    with pytest.raises(CanonicalWriterLeaseLost):
        asyncio.run(exercise())

    assert events == [
        "lease-acquire:martincall-app",
        "initialize",
        "lease-verify:1",
        "route-refresh",
        "lease-verify:2",
        "lease-release",
    ]


def test_app_shutdown_cancellation_releases_only_after_runtime_drains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")

        def initialize(self) -> None:
            events.append("initialize")

        def verify_canonical_writer_lease(self) -> None:
            events.append("lease-verify")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    monkeypatch.setattr(ui_app, "default_postgres_store", Store)
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: events.append("route-refresh"),
    )
    monkeypatch.setattr(
        ui_app._ibkr_runtime,
        "apply_persisted_ibkr_runtime_settings",
        lambda: events.append("ibkr-settings"),
    )

    async def scenario() -> None:
        stop_started = asyncio.Event()
        allow_stop = asyncio.Event()

        async def start_runtime() -> None:
            events.append("runtime-start")

        async def stop_runtime() -> None:
            events.append("runtime-stop-start")
            stop_started.set()
            await allow_stop.wait()
            events.append("runtime-stop-drained")

        monkeypatch.setattr(ui_app, "start_server_alert_monitor", start_runtime)
        monkeypatch.setattr(ui_app, "stop_server_alert_monitor", stop_runtime)

        async def exercise() -> None:
            async with ui_app.martincall_lifespan(SimpleNamespace()):
                events.append("serving")

        task = asyncio.create_task(exercise())
        await asyncio.wait_for(stop_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert "lease-release" not in events
        allow_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert events.index("runtime-stop-drained") < events.index("lease-release")


def test_app_shutdown_failure_retains_writer_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")

        def initialize(self) -> None:
            events.append("initialize")

        def verify_canonical_writer_lease(self) -> None:
            return None

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    monkeypatch.setattr(ui_app, "default_postgres_store", Store)
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ui_app._ibkr_runtime,
        "apply_persisted_ibkr_runtime_settings",
        lambda: None,
    )

    async def start_runtime() -> None:
        events.append("runtime-start")

    async def stop_runtime() -> None:
        events.append("runtime-stop-failed")
        raise RuntimeError("runtime undrained")

    monkeypatch.setattr(ui_app, "start_server_alert_monitor", start_runtime)
    monkeypatch.setattr(ui_app, "stop_server_alert_monitor", stop_runtime)

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    with pytest.raises(RuntimeError, match="runtime undrained"):
        asyncio.run(exercise())
    assert "lease-release" not in events


def test_app_partial_runtime_start_is_drained_before_lease_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")

        def initialize(self) -> None:
            events.append("initialize")

        def verify_canonical_writer_lease(self) -> None:
            return None

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

    monkeypatch.setattr(ui_app, "default_postgres_store", Store)
    monkeypatch.setattr(
        ui_app.quote_stream,
        "refresh_routes_from_store",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ui_app._ibkr_runtime,
        "apply_persisted_ibkr_runtime_settings",
        lambda: None,
    )

    async def partial_start() -> None:
        events.append("runtime-partial-side-effect")
        raise RuntimeError("runtime start failed")

    async def stop_runtime() -> None:
        events.append("runtime-stop-drained")

    monkeypatch.setattr(ui_app, "start_server_alert_monitor", partial_start)
    monkeypatch.setattr(ui_app, "stop_server_alert_monitor", stop_runtime)

    async def exercise() -> None:
        async with ui_app.martincall_lifespan(SimpleNamespace()):
            events.append("serving")

    with pytest.raises(RuntimeError, match="runtime start failed"):
        asyncio.run(exercise())

    assert events.index("runtime-partial-side-effect") < events.index("runtime-stop-drained")
    assert events.index("runtime-stop-drained") < events.index("lease-release")
    assert "serving" not in events


def test_latched_lease_loss_fails_storage_and_http_readiness_closed() -> None:
    connection = _LeaseConnection()
    store = PostgresStore("postgresql://lease-test")
    store._psycopg = lambda: _FakePsycopg(connection)  # type: ignore[method-assign]
    store.acquire_canonical_writer_lease(owner="test-app")
    connection.leader_held = False

    storage = store.status()
    readiness = readiness_status_snapshot(store_factory=lambda: store)

    assert storage["ok"] is False
    assert storage["canonical_writer"]["state"] == "lost"
    assert readiness["status"] == "not_ready"
    assert readiness["storage"]["canonical_writer"]["state"] == "lost"
    assert store._pool is None


def test_system_status_cache_cannot_mask_latched_lease_loss() -> None:
    class Store:
        lost = False
        diagnostics_calls = 0

        def canonical_writer_lease_status(self, *, verify: bool) -> dict[str, Any]:
            assert verify is True
            if self.lost:
                return {
                    "ok": False,
                    "state": "lost",
                    "message": "CANONICAL_WRITER_LEASE_LOST",
                }
            return {"ok": True, "state": "held"}

        def diagnostics(self) -> dict[str, Any]:
            self.diagnostics_calls += 1
            return {"configured": True, "ok": True, "bars": 5}

    store = Store()
    service = SystemStatusService(SimpleNamespace())  # type: ignore[arg-type]

    assert service.storage_snapshot(store)["ok"] is True
    store.lost = True
    lost = service.storage_snapshot(store)

    assert lost["ok"] is False
    assert lost["canonical_writer"]["state"] == "lost"
    assert store.diagnostics_calls == 1


def test_refresh_current_contracts_busy_lease_blocks_provider_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://lease-test"

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")
            raise CanonicalWriterLeaseUnavailable("CANONICAL_WRITER_LEASE_BUSY")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(
        refresh_contracts,
        "AppConfig",
        lambda: SimpleNamespace(database_url="postgresql://lease-test"),
    )
    monkeypatch.setattr(refresh_contracts, "PostgresStore", Store)
    monkeypatch.setattr(
        refresh_contracts,
        "refresh_watchlist_current_contracts",
        lambda *_args, **_kwargs: events.append("provider-action"),
    )
    monkeypatch.setattr(sys, "argv", ["refresh_watchlist_current_contracts.py"])

    assert refresh_contracts.main() == 2
    assert events == [
        "lease-acquire:refresh-watchlist-current-contracts",
        "close",
    ]


def test_refresh_current_contracts_holds_lease_through_provider_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://lease-test"

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")

        def initialize(self) -> None:
            events.append("initialize")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(
        refresh_contracts,
        "AppConfig",
        lambda: SimpleNamespace(database_url="postgresql://lease-test"),
    )
    monkeypatch.setattr(refresh_contracts, "PostgresStore", Store)
    monkeypatch.setattr(
        refresh_contracts,
        "refresh_watchlist_current_contracts",
        lambda *_args, **_kwargs: events.append("provider-action") or {"ok": True},
    )
    monkeypatch.setattr(sys, "argv", ["refresh_watchlist_current_contracts.py"])

    assert refresh_contracts.main() == 0
    assert events == [
        "lease-acquire:refresh-watchlist-current-contracts",
        "initialize",
        "provider-action",
        "lease-release",
        "close",
    ]


def test_ibkr_tick_feed_busy_lease_blocks_storage_and_provider_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Store:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://lease-test"

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire:{owner}")
            raise CanonicalWriterLeaseUnavailable("CANONICAL_WRITER_LEASE_BUSY")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(
        tick_feed,
        "AppConfig",
        lambda: SimpleNamespace(database_url="postgresql://lease-test"),
    )
    monkeypatch.setattr(tick_feed, "PostgresStore", Store)
    monkeypatch.setattr(
        tick_feed,
        "IbkrTickFeed",
        lambda *_args, **_kwargs: events.append("provider-start"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ibkr_tick_feed.py", "--instrument-id", "instrument-1"],
    )

    assert asyncio.run(tick_feed.main()) == 2
    assert events == ["lease-acquire:ibkr-tick-feed", "close"]


def test_ibkr_tick_feed_startup_cancellation_settles_acquire_before_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    acquire_started = threading.Event()
    allow_acquire = threading.Event()

    class Store:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://lease-test"

        def acquire_canonical_writer_lease(self, *, owner: str) -> None:
            events.append(f"lease-acquire-start:{owner}")
            acquire_started.set()
            if not allow_acquire.wait(timeout=2.0):
                raise AssertionError("timed out waiting to finish tick-feed acquire")
            events.append("lease-acquire-done")

        def release_canonical_writer_lease(self) -> None:
            events.append("lease-release")

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(
        tick_feed,
        "AppConfig",
        lambda: SimpleNamespace(database_url="postgresql://lease-test"),
    )
    monkeypatch.setattr(tick_feed, "PostgresStore", Store)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ibkr_tick_feed.py", "--instrument-id", "instrument-1"],
    )

    async def scenario() -> None:
        task = asyncio.create_task(tick_feed.main())
        started = await asyncio.to_thread(acquire_started.wait, 1.0)
        assert started is True
        task.cancel()
        await asyncio.sleep(0)
        assert "lease-release" not in events
        assert "close" not in events
        allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert events == [
        "lease-acquire-start:ibkr-tick-feed",
        "lease-acquire-done",
        "lease-release",
        "close",
    ]
