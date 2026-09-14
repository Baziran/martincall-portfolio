import ast
import inspect
import textwrap
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

from aef_terminal.storage.repos import drawings as drawings_repo
from aef_terminal.storage.repos import bars as bars_repo
from aef_terminal.storage.repos import futures as futures_repo
from aef_terminal.storage.repos import gex as gex_repo
from aef_terminal.storage.repos import paper as paper_repo
from aef_terminal.storage.repos import route_authority as route_authority_repo
from aef_terminal.storage.repos import settings as settings_repo
from aef_terminal.storage.repos import ticks as ticks_repo
from aef_terminal.storage.repos import trading_hours as trading_hours_repo
from aef_terminal.runtime.storage_deadlines import postgres_operation_timeouts
from aef_terminal.storage.repos.connection import PostgresConnectionMixin


def _paper_fill_owner_source(*names: str) -> str:
    return "\n".join(inspect.getsource(getattr(paper_repo.PaperRepoMixin, name)) for name in names)


def test_paper_fill_holds_position_lock_inside_transaction() -> None:
    transaction_source = inspect.getsource(paper_repo.PaperRepoMixin.apply_filled_paper_order)
    orchestration_source = inspect.getsource(
        paper_repo.PaperRepoMixin._apply_paper_fill_with_cursor
    )
    pipeline_source = _paper_fill_owner_source(
        "_reserve_paper_fill_order",
        "_paper_fill_existing_order_outcome",
        "_paper_fill_replay_outcome",
        "_locked_paper_fill_position",
        "_paper_position_expectation_outcome",
        "_paper_stale_reduce_only_outcome",
        "_increase_paper_position",
        "_reduce_paper_position",
        "_close_paper_position_exact",
        "_sync_paper_fill_protective_orders",
        "_append_paper_fill_events",
    )
    tree = ast.parse(
        "\n".join(
            textwrap.dedent(inspect.getsource(getattr(paper_repo.PaperRepoMixin, name)))
            for name in (
                "_increase_paper_position",
                "_reduce_paper_position",
                "_close_paper_position_exact",
            )
        )
    )
    position_updates = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "UPDATE paper_positions" in node.value
    ]

    assert "with conn.transaction():" in transaction_source
    assert "pg_advisory_xact_lock" in transaction_source
    assert "FOR UPDATE" in pipeline_source
    assert "fill.target_position_id" in pipeline_source
    assert "AND (%s::text IS NULL OR id = %s)" in pipeline_source
    assert "cancel_paper_order_if_pending(order_id" not in pipeline_source
    assert "_cancel_paper_order_if_pending_with_cursor(" in pipeline_source
    assert "instrument_id=fill.instrument_id" in pipeline_source
    assert "route_fingerprint=fill.route_fingerprint" in pipeline_source
    assert orchestration_source.index("_reserve_paper_fill_order(") < orchestration_source.index(
        "_locked_paper_fill_position("
    )
    assert "PAPER_ORDER_ID_COMMAND_COLLISION" in pipeline_source
    assert "PAPER_ORDER_IDEMPOTENCY_FILL_INVALID" in pipeline_source
    sync_source = inspect.getsource(paper_repo.PaperRepoMixin._sync_paper_fill_protective_orders)
    assert sync_source.count("self._sync_paper_protective_orders(") == 2
    assert sync_source.index("if closed_position is not None:") < sync_source.index(
        "transition.position_id"
    )
    assert "_append_paper_trade_event_with_cursor(" in pipeline_source
    assert len(position_updates) == 3
    for query in position_updates:
        assert "WHERE id = %s" in query
        assert "AND instrument_id = %s" in query
        assert "AND route_fingerprint = %s" in query


def test_delayed_execution_rejection_is_cancelled_and_journaled_atomically() -> None:
    source = inspect.getsource(paper_repo.PaperRepoMixin.reject_paper_order_execution_if_pending)

    assert "with conn.transaction(), conn.cursor() as cur:" in source
    assert "_cancel_paper_order_if_pending_with_cursor(" in source
    assert 'payload_patch={"execution_rejection": rejection}' in source
    assert "_append_paper_trade_event_with_cursor(" in source
    assert source.index("_cancel_paper_order_if_pending_with_cursor(") < source.index(
        "_append_paper_trade_event_with_cursor("
    )


def test_protective_cancellation_uses_canonical_fill_timestamp() -> None:
    source = inspect.getsource(paper_repo.PaperRepoMixin._sync_paper_protective_orders)
    fill_source = inspect.getsource(paper_repo.PaperRepoMixin._sync_paper_fill_protective_orders)

    assert "updated_at = %s" in source
    assert "updated_at = now()" not in source
    assert "contract_scope_key" in source
    assert "updated_at," in source
    assert "position_id," in source
    assert "route_fingerprint," in source
    assert fill_source.count("updated_at=fill.filled_at") == 2


def test_paper_fill_rejects_every_non_singleton_position_update() -> None:
    update_owners = (
        paper_repo.PaperRepoMixin._increase_paper_position,
        paper_repo.PaperRepoMixin._reduce_paper_position,
        paper_repo.PaperRepoMixin._close_paper_position_exact,
    )
    source = "\n".join(inspect.getsource(owner) for owner in update_owners)

    assert source.count("UPDATE paper_positions") == 3
    assert source.count("if cur.rowcount != 1:") == 3
    assert source.count("PAPER_POSITION_UPDATE_CARDINALITY_INVALID") == 3


def test_paper_fill_has_one_typed_outcome_result_shape_in_every_branch() -> None:
    outcome_owners = (
        paper_repo.PaperRepoMixin._paper_fill_replay_outcome,
        paper_repo.PaperRepoMixin._paper_manual_close_replay_outcome,
        paper_repo.PaperRepoMixin._paper_position_expectation_outcome,
        paper_repo.PaperRepoMixin._paper_stale_reduce_only_outcome,
        paper_repo.PaperRepoMixin._apply_paper_fill_with_cursor,
    )
    returns = []
    source = ""
    for owner in outcome_owners:
        owner_source = textwrap.dedent(inspect.getsource(owner))
        source += owner_source
        returns.extend(
            node
            for node in ast.walk(ast.parse(owner_source))
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        )
    expected_keys = {
        "outcome",
        "order",
        "position",
        "fill",
        "created_orders",
        "cancelled_orders",
        "closed_position",
        "opened_new_position",
    }

    assert set(get_args(paper_repo.PaperFillOutcome)) == {"filled", "cancelled", "no_op"}
    assert paper_repo.PaperFillResult.__required_keys__ == frozenset(expected_keys)
    assert len(returns) == 5
    for return_node in returns:
        assert isinstance(return_node.value, ast.Dict)
        assert {
            key.value
            for key in return_node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        } == expected_keys

    assert '"outcome": "filled"' in source
    assert '"outcome": "no_op"' in source
    assert '"outcome": "no_op" if fill.manual_close else "cancelled"' in source
    assert '"order": cancelled' in source
    assert '"cancelled_orders": [cancelled]' in source
    assert "PAPER_POSITION_NOT_OPEN" not in source
    assert "idempotent_replay" not in source


def test_paper_fill_consumers_use_outcome_as_the_only_result_discriminator() -> None:
    consumer_sources = [
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/aef_terminal/ui/paper/actions.py",
            "src/aef_terminal/ui/paper/positions.py",
            "src/aef_terminal/ui/paper/notify.py",
            "src/aef_terminal/ui/services/server_alert_monitor.py",
        )
    ]

    for source in consumer_sources:
        assert 'result.get("outcome")' in source
        assert "idempotent_replay" not in source


def test_paper_runtime_consumers_require_the_complete_storage_contract() -> None:
    consumer_sources = [
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/aef_terminal/paper_journal_runtime.py",
            "src/aef_terminal/ui/paper/actions.py",
            "src/aef_terminal/ui/paper/execution.py",
            "src/aef_terminal/ui/paper/journal_queries.py",
            "src/aef_terminal/ui/paper/orders.py",
            "src/aef_terminal/ui/paper/positions.py",
            "src/aef_terminal/ui/paper/sync.py",
            "src/aef_terminal/ui/services/server_alert_monitor.py",
        )
    ]

    for source in consumer_sources:
        assert "getattr(store" not in source
        assert "PAPER_EXECUTION_STORE_REQUIRED" not in source
        assert "PAPER_JOURNAL_UNAVAILABLE" not in source


def test_pending_paper_order_and_event_share_one_transaction() -> None:
    source = inspect.getsource(paper_repo.PaperRepoMixin.upsert_paper_order)

    assert "with conn.transaction():" in source
    assert "ON CONFLICT (id) DO NOTHING" in source
    assert "create_command" in source
    assert "execution_command" in source
    assert "PAPER_ORDER_ID_COMMAND_COLLISION" in source
    assert "_append_paper_trade_event_with_cursor(" in source


def test_pending_order_mutations_and_events_share_one_transaction() -> None:
    expected = (
        (paper_repo.PaperRepoMixin.update_paper_order_if_pending, "order_updated"),
        (paper_repo.PaperRepoMixin.cancel_paper_order_if_pending, "order_cancelled"),
    )

    for owner, event_type in expected:
        source = inspect.getsource(owner)
        assert "with conn.transaction(), conn.cursor() as cur:" in source
        assert "_append_paper_trade_event_with_cursor(" in source
        assert f'"{event_type}"' in source
        assert source.index("saved = ") < source.index("_append_paper_trade_event_with_cursor(")
        assert source.index("_append_paper_trade_event_with_cursor(") < source.index(
            "paper_journal_runtime.mark_committed()"
        )

    assert not hasattr(paper_repo.PaperRepoMixin, "append_paper_trade_event")


def test_tick_rollup_purge_holds_advisory_lock_inside_transaction() -> None:
    source = inspect.getsource(ticks_repo.TicksRepoMixin._rollup_and_purge_ticks)

    assert "with conn.transaction():" in source
    assert "pg_advisory_xact_lock" in source
    assert "ON COMMIT DROP" in source


def test_replace_drawings_holds_advisory_lock_inside_transaction() -> None:
    source = inspect.getsource(drawings_repo.DrawingsRepoMixin.replace_drawings)

    assert "with conn.transaction():" in source
    assert "pg_advisory_xact_lock" in source
    assert "jsonb_to_recordset" in source
    assert "executemany" not in source
    assert "if isinstance(item, dict)" not in source
    assert "or json.dumps(" not in source


def test_price_alert_repo_generation_and_cas_paths_do_not_coerce_runtime_values() -> None:
    invalid_cases = (
        ({"rearmedAt": "1"}, 2),
        ({"rearmedAt": 1}, 2.0),
    )
    for payload, requested in invalid_cases:
        try:
            drawings_repo.DrawingsRepoMixin._next_alert_generation(
                payload,
                requested,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("alert generations must be exact non-negative integers")

    mutation_sources = [
        inspect.getsource(method)
        for method in (
            drawings_repo.DrawingsRepoMixin.update_price_alert_payload_exact,
            drawings_repo.DrawingsRepoMixin.rearm_price_alert_exact,
            drawings_repo.DrawingsRepoMixin.set_price_alert_enabled_exact,
        )
    ]
    assert all("int(float(" not in source for source in mutation_sources)
    assert all("cooldown_until = float(" not in source for source in mutation_sources)


def test_logical_batch_writes_use_explicit_transactions() -> None:
    for repo_module in (
        bars_repo,
        drawings_repo,
        futures_repo,
        gex_repo,
    ):
        assert ".executemany(" not in inspect.getsource(repo_module)

    batch_writers = (
        bars_repo.BarsRepoMixin.write_bars,
        futures_repo.FuturesRepoMixin.write_futures_contract_bars,
        futures_repo.FuturesRepoMixin.write_futures_canonical_bars,
        settings_repo.SettingsRepoMixin.upsert_settings,
        gex_repo.GexRepoMixin.create_option_target_intent,
        gex_repo.GexRepoMixin.update_option_target_intent,
        gex_repo.GexRepoMixin.update_option_target_market_samples,
        gex_repo.GexRepoMixin.upsert_quote_snapshots,
        drawings_repo.DrawingsRepoMixin.delete_price_alert_scope_exact,
    )

    for writer in batch_writers:
        source = inspect.getsource(writer)
        assert (
            "with conn.transaction(), conn.cursor() as cur:" in source
            or "self._canonical_write_cursor()" in source
        )

    futures_contract_bars_source = inspect.getsource(
        futures_repo.FuturesRepoMixin.write_futures_contract_bars
    )
    futures_contract_cursor_source = inspect.getsource(
        futures_repo.FuturesRepoMixin._write_futures_contract_bars_on_cursor
    )
    futures_contract_rows_source = inspect.getsource(futures_repo._write_futures_contract_rows)
    assert "_write_futures_contract_bars_on_cursor(" in futures_contract_bars_source
    assert "_write_futures_contract_rows(" in futures_contract_cursor_source
    assert futures_contract_rows_source.count("cur.execute(") == 2
    assert futures_contract_rows_source.count("jsonb_to_recordset") == 2
    assert "JOIN futures_contract_bars AS persisted" in futures_contract_rows_source
    assert "DISTINCT ON" not in futures_contract_rows_source
    assert "executemany" not in futures_contract_bars_source
    assert "executemany" not in futures_contract_cursor_source
    assert "executemany" not in futures_contract_rows_source

    quote_snapshot_source = inspect.getsource(gex_repo.GexRepoMixin.upsert_quote_snapshots)
    assert quote_snapshot_source.count("cur.execute(") == 1
    assert "jsonb_to_recordset" in quote_snapshot_source
    assert "DISTINCT ON" in quote_snapshot_source
    assert "executemany" not in quote_snapshot_source

    price_alert_source = inspect.getsource(
        drawings_repo.DrawingsRepoMixin.delete_price_alert_scope_exact
    )
    assert "pg_advisory_xact_lock" in price_alert_source
    assert (
        "SELECT id, instrument_id, route_fingerprint, symbol, timeframe, payload"
        in price_alert_source
    )
    assert "self._materialize_price_alert_rows(rows)" in price_alert_source
    assert "validate_price_alert(alert)" in price_alert_source
    assert "executemany" not in price_alert_source
    assert price_alert_source.index("pg_advisory_xact_lock") < price_alert_source.index(
        "DELETE FROM price_alerts"
    )

    create_alert_source = inspect.getsource(
        drawings_repo.DrawingsRepoMixin.create_or_rearm_price_alert_exact
    )
    assert "SELECT id, symbol, timeframe, payload" in create_alert_source
    assert "payload.get(field) != expected" in create_alert_source
    assert create_alert_source.index("payload.get(field) != expected") < create_alert_source.index(
        "price_alert_definition_identity(payload)"
    )
    assert create_alert_source.index("require_unique_price_alert_definitions(") < (
        create_alert_source.index("price_alert_definition_identity(payload)")
    )

    rearm_alert_source = inspect.getsource(drawings_repo.DrawingsRepoMixin.rearm_price_alert_exact)
    assert rearm_alert_source.index("require_unique_price_alert_definitions(") < (
        rearm_alert_source.index("UPDATE price_alerts")
    )

    option_sample_source = inspect.getsource(
        gex_repo.GexRepoMixin.update_option_target_market_samples
    )
    assert option_sample_source.count("cur.execute(") == 2
    assert "jsonb_to_recordset" in option_sample_source
    assert "executemany" not in option_sample_source
    assert option_sample_source.index("conn.transaction()") < option_sample_source.index(
        "FOR UPDATE"
    )


def test_gex_expiry_fact_storage_scan_is_bounded_and_history_is_scope_complete() -> None:
    expiry_fact_source = inspect.getsource(gex_repo.GexRepoMixin.read_gex_option_expiry_facts)
    assert "WITH recent_gex_snapshots AS" in expiry_fact_source
    assert expiry_fact_source.index("LIMIT %s") < expiry_fact_source.index(
        "CROSS JOIN LATERAL jsonb_array_elements"
    )
    assert expiry_fact_source.count("limit,") == 2

    history_source = inspect.getsource(gex_repo.GexRepoMixin.read_gex_snapshots)
    assert "payload #> '{comparison_scope,series}'" in history_source
    assert "scoped_expiry_contract ->> 'expiry'" in history_source
    assert "scope_series ->> 'trading_class'" in history_source
    assert "scope_series -> 'multiplier'" in history_source
    assert "HAVING COUNT(" in history_source
    assert "series_expiry_contract" in history_source


def test_realtime_postgres_timeouts_are_scoped_and_pool_wait_is_bounded() -> None:
    calls: list[tuple[str, tuple[str, str] | None]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, query: str, params: tuple[str, str] | None = None) -> None:
            calls.append((" ".join(query.split()), params))

    class Connection:
        closed = False

        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    class ConnectionContext:
        def __enter__(self) -> Connection:
            return connection

        def __exit__(self, *_args) -> None:
            return None

    class Pool:
        timeout: float | None = None

        def connection(self, *, timeout: float) -> ConnectionContext:
            self.timeout = timeout
            return ConnectionContext()

    pool = Pool()
    repo = PostgresConnectionMixin("postgresql://unused", connect_timeout=2.5)
    repo._pool = pool
    repo._initialized = True
    repo._psycopg = lambda: None  # type: ignore[method-assign]

    with postgres_operation_timeouts(
        statement_timeout_ms=8_000,
        lock_timeout_ms=2_000,
    ):
        with repo._connect() as acquired:
            assert acquired is connection

    assert pool.timeout == 2.5
    assert calls == [
        (
            "SELECT set_config('statement_timeout', %s, false), set_config('lock_timeout', %s, false)",
            ("8000ms", "2000ms"),
        ),
        (
            "SELECT set_config('statement_timeout', '0', false), set_config('lock_timeout', '0', false)",
            None,
        ),
    ]

    with repo._connect():
        pass
    assert len(calls) == 2


def test_current_futures_contract_switch_is_atomic_and_serialized() -> None:
    source = inspect.getsource(futures_repo.FuturesRepoMixin.write_futures_contract)
    cursor_source = inspect.getsource(
        futures_repo.FuturesRepoMixin._write_futures_contract_on_cursor
    )
    lifecycle_lock_source = inspect.getsource(
        route_authority_repo.lock_provider_instrument_lifecycle_on_cursor
    )

    assert "with conn.transaction(), conn.cursor() as cur:" in source
    assert "pg_advisory_xact_lock" in lifecycle_lock_source
    assert "_write_futures_contract_on_cursor(" in source
    assert source.index("_acquire_canonical_writer_fence_on_cursor") < source.index(
        "_write_futures_contract_on_cursor("
    )
    assert cursor_source.index(
        "lock_provider_instrument_lifecycle_on_cursor("
    ) < cursor_source.index("UPDATE futures_contracts")
    assert cursor_source.index("UPDATE futures_contracts") < cursor_source.index(
        "INSERT INTO futures_contracts"
    )


def test_trading_hours_snapshot_and_interval_materialization_are_atomic() -> None:
    source = inspect.getsource(trading_hours_repo.TradingHoursRepoMixin.upsert_trading_hours)

    assert "with conn.transaction(), conn.cursor() as cur:" in source
    assert "pg_advisory_xact_lock" in source
    assert source.index("pg_advisory_xact_lock") < source.index("INSERT INTO trading_hours")
    assert "INSERT INTO trading_schedule_snapshots" in source
    assert "FROM trading_schedule_snapshots" in source
    assert "GROUP BY timezone, trading_hours, liquid_hours, raw_payload" not in source
    assert "ORDER BY max(fetched_at)" not in source
    assert "trading_session_interval_replacement_horizons" in source
    assert "AND opens_at < %s" in source
    assert "AND closes_at > %s" in source
    assert "jsonb_to_recordset" in source
    assert "executemany" not in source
    assert source.index("DELETE FROM trading_session_intervals") < source.index(
        "INSERT INTO trading_session_intervals"
    )


def test_trading_hours_delete_is_atomic_and_no_parallel_interval_rebuild_exists() -> None:
    delete_source = inspect.getsource(trading_hours_repo.TradingHoursRepoMixin.delete_trading_hours)

    assert "with conn.transaction(), conn.cursor() as cur:" in delete_source
    assert "pg_advisory_xact_lock" in delete_source
    assert "DELETE FROM trading_schedule_snapshots" in delete_source
    assert not hasattr(
        trading_hours_repo.TradingHoursRepoMixin,
        "rebuild_trading_session_intervals_from_snapshots",
    )
    assert not hasattr(
        trading_hours_repo.TradingHoursRepoMixin,
        "backfill_trading_schedule_snapshots_from_latest",
    )


def test_trading_session_open_state_uses_one_storage_round_trip() -> None:
    source = inspect.getsource(
        trading_hours_repo.TradingHoursRepoMixin.read_trading_session_open_state
    )
    reader_source = inspect.getsource(
        trading_hours_repo.TradingHoursRepoMixin.read_trading_session_intervals
    )

    assert source.count("read_trading_session_intervals(") == 1
    assert "_connect(" not in source
    assert "source_coverage_start" in source
    assert "session_date" not in source
    assert "PARTITION BY session.source_fetched_at" in reader_source
    assert "metadata ->> 'provider_source'" not in reader_source
    assert "LEFT JOIN trading_schedule_snapshots AS snapshot" in reader_source
    assert "snapshot.fetched_at = scoped.source_fetched_at" in reader_source
    assert "snapshot.raw_payload ->> 'schedule_format'" in reader_source
    assert "= 'provider_declared_intervals'" in reader_source
    assert "snapshot.raw_payload -> 'trading_intervals'" in reader_source
    assert "jsonb_build_object(" in reader_source
    assert "'session_type'," in reader_source


def test_trading_session_open_state_requires_exact_provider_date_coverage(
    monkeypatch,
) -> None:
    timestamp = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)

    class Cursor:
        def __init__(self, repo) -> None:
            self.repo = repo

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, *_args) -> None:
            self.repo.execute_count += 1

        def fetchall(self):
            return list(self.repo.rows)

    class Connection:
        def __init__(self, repo) -> None:
            self.repo = repo

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def cursor(self):
            return Cursor(self.repo)

    class Repo(trading_hours_repo.TradingHoursRepoMixin):
        def __init__(self) -> None:
            self.rows = []
            self.execute_count = 0

        def _connect(self):
            return Connection(self)

    monkeypatch.setattr(
        trading_hours_repo,
        "route_instrument",
        lambda _instrument: SimpleNamespace(instrument_id="instrument-1", fingerprint="route-1"),
    )
    repo = Repo()
    repo.rows = [
        (
            "instrument-1",
            "route-1",
            date(2026, 7, 14),
            datetime(2026, 7, 14, 14, 30, tzinfo=UTC),
            datetime(2026, 7, 14, 21, 0, tzinfo=UTC),
            "open",
            datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
            {},
            None,
            None,
            None,
            None,
            "America/Chicago",
        ),
        (
            "instrument-1",
            "route-1",
            date(2026, 7, 16),
            datetime(2026, 7, 16, 14, 30, tzinfo=UTC),
            datetime(2026, 7, 16, 21, 0, tzinfo=UTC),
            "open",
            datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
            {},
            None,
            None,
            None,
            None,
            "America/Chicago",
        ),
    ]
    assert (
        repo.read_trading_session_open_state(
            timestamp,
            "trading",
            instrument={},
        )
        is None
    )

    repo.rows = [
        (
            "instrument-1",
            "route-1",
            date(2026, 7, 15),
            datetime(2026, 7, 15, 5, 0, tzinfo=UTC),
            datetime(2026, 7, 16, 5, 0, tzinfo=UTC),
            "closed",
            datetime(2026, 7, 15, 1, 0, tzinfo=UTC),
            {},
            None,
            None,
            None,
            None,
            "America/Chicago",
        )
    ]
    assert (
        repo.read_trading_session_open_state(
            timestamp,
            "trading",
            instrument={},
        )
        is False
    )

    repo.rows[0] = (*repo.rows[0][:5], "open", *repo.rows[0][6:])
    assert (
        repo.read_trading_session_open_state(
            timestamp,
            "trading",
            instrument={},
        )
        is True
    )

    repo.rows = [
        (
            "instrument-1",
            "route-1",
            date(2026, 7, 28),
            datetime(2026, 7, 27, 22, 0, tzinfo=UTC),
            datetime(2026, 7, 28, 21, 0, tzinfo=UTC),
            "open",
            datetime(2026, 7, 28, 6, 0, tzinfo=UTC),
            {"provider_source": "reqHistoricalSchedule"},
            datetime(2026, 7, 27, 22, 0, tzinfo=UTC),
            datetime(2026, 7, 28, 21, 0, tzinfo=UTC),
            "2026-07-27T22:00:00+00:00",
            "2026-07-28T21:00:00+00:00",
            "America/Chicago",
        )
    ]
    assert (
        repo.read_trading_session_open_state(
            datetime(2026, 7, 29, 3, 25, tzinfo=UTC),
            "trading",
            instrument={},
        )
        is None
    )

    repo.rows = [
        (
            "instrument-1",
            "route-1",
            date(2026, 7, 30),
            datetime(2026, 7, 29, 22, 0, tzinfo=UTC),
            datetime(2026, 7, 30, 21, 0, tzinfo=UTC),
            "open",
            datetime(2026, 7, 29, 3, 53, tzinfo=UTC),
            {"provider_source": "reqHistoricalSchedule"},
            datetime(2026, 7, 29, 22, 0, tzinfo=UTC),
            datetime(2026, 8, 5, 21, 0, tzinfo=UTC),
            "2026-07-23T22:00:00+00:00",
            "2026-07-29T03:53:29+00:00",
            "America/Chicago",
        )
    ]
    preserved = repo.read_trading_session_intervals(
        instrument={},
        session_type="trading",
        start_ts=datetime(2026, 7, 29, 21, 0, tzinfo=UTC),
        end_ts=datetime(2026, 7, 29, 23, 0, tzinfo=UTC),
    )
    assert preserved[0]["source_coverage_start"] == "2026-07-23T22:00:00+00:00"
    assert preserved[0]["source_coverage_end"] == "2026-08-05T21:00:00+00:00"
    assert (
        repo.read_trading_session_open_state(
            datetime(2026, 7, 29, 21, 30, tzinfo=UTC),
            "trading",
            instrument={},
        )
        is False
    )
    assert repo.execute_count == 6
