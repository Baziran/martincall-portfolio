from __future__ import annotations

import ast
import inspect
import re
from collections import Counter
from pathlib import Path

import pytest

from aef_terminal.data.ibkr import contracts as ibkr_contracts
from aef_terminal.data.ibkr import quotes as ibkr_quotes
from aef_terminal.data.ibkr import runtime as ibkr_runtime
from aef_terminal.data.ibkr import session as ibkr_session
from aef_terminal.data.ibkr import trading_hours as ibkr_trading_hours
from aef_terminal.data import providers as provider_registry
from aef_terminal.data.instrument_identity import InstrumentIdentityError, require_provider_identity
from aef_terminal.data.provider_contract import (
    CanonicalHistoryRoute,
    CanonicalHistoryStorageKind,
    ChartCommitProvider,
    ChartLiveProvider,
    GexProvider,
    HistoryRepairProvider,
    NativeChartLiveProvider,
    OptionsProvider,
    ProviderAdapter,
    ProviderManifest,
    QuotePollingProvider,
    QuoteStreamProvider,
    TradingScheduleProvider,
)
from aef_terminal.data import provider_sessions as sessions
from aef_terminal.storage.repos import instruments as instrument_repo
from aef_terminal.ui.routers import gex as gex_router
from aef_terminal.ui import runtime_instruments
from aef_terminal.ui import reference_services
from aef_terminal.ui.services import (
    chart_stream_ws,
    gex_stream_ws,
    option_target_reprice,
    trading_hours_refresh,
)
from aef_terminal.runtime.instruments import resolve_instrument_profile
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "src", ROOT / "scripts")


def _production_python_files() -> list[Path]:
    return [path for root in PRODUCTION_ROOTS for path in root.rglob("*.py")]


def _browser_javascript_files() -> list[Path]:
    roots = (
        ROOT / "src/aef_terminal/ui/assets",
        ROOT / "src/aef_terminal/indicators/modules",
    )
    return sorted(path for root in roots for path in root.rglob("*.js"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_production_consumers_do_not_capability_probe_canonical_store() -> None:
    violations: list[str] = []
    for path in _production_python_files():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "store"
            ):
                violations.append(f"{path}:{node.lineno}")

    assert not violations


def _module_name(path: Path, python_root: Path) -> str:
    relative = path.relative_to(python_root)
    if relative.name == "__init__.py":
        suffix = ".".join(relative.parent.parts)
    else:
        suffix = ".".join(relative.with_suffix("").parts)
    return "aef_terminal" + (f".{suffix}" if suffix else "")


def test_production_modules_do_not_define_the_same_name_twice_in_one_scope() -> None:
    duplicates: list[str] = []
    for path in _production_python_files():
        tree = _tree(path)
        owners = [("<module>", tree.body)] + [
            (node.name, node.body)
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for owner, body in owners:
            counts = Counter(
                node.name
                for node in body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            )
            duplicates.extend(
                f"{path.relative_to(ROOT)}:{owner}:{name}"
                for name, count in counts.items()
                if count > 1
            )
    assert duplicates == []


def test_eager_production_import_graph_is_acyclic() -> None:
    python_root = ROOT / "src/aef_terminal"
    modules = {_module_name(path, python_root): path for path in python_root.rglob("*.py")}
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, path in modules.items():
        for node in _tree(path).body:
            imported_names: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported_names = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_names = (node.module,)
            for imported in imported_names:
                candidates = [
                    candidate
                    for candidate in modules
                    if imported == candidate or imported.startswith(f"{candidate}.")
                ]
                if candidates:
                    graph[module].add(max(candidates, key=len))

    states: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[str] = []

    def visit(module: str) -> None:
        states[module] = 1
        stack.append(module)
        for dependency in sorted(graph[module]):
            if states.get(dependency, 0) == 0:
                visit(dependency)
            elif states.get(dependency) == 1:
                start = stack.index(dependency)
                cycles.append(" -> ".join([*stack[start:], dependency]))
        stack.pop()
        states[module] = 2

    for module in sorted(graph):
        if states.get(module, 0) == 0:
            visit(module)
    assert cycles == []


def test_top_level_owners_do_not_import_each_others_private_names() -> None:
    python_root = ROOT / "src/aef_terminal"
    violations: list[str] = []
    for path in python_root.rglob("*.py"):
        importer_owner = path.relative_to(python_root).parts[0]
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            module_parts = node.module.split(".")
            if module_parts[:1] != ["aef_terminal"] or len(module_parts) < 2:
                continue
            imported_owner = module_parts[1]
            if imported_owner == importer_owner:
                continue
            violations.extend(
                f"{path.relative_to(ROOT)}:{node.lineno}:{node.module}:{alias.name}"
                for alias in node.names
                if alias.name.startswith("_")
            )
    assert violations == []


def test_generic_exception_message_has_one_runtime_owner() -> None:
    definitions = [
        f"{path.relative_to(ROOT)}:{node.name}"
        for path in _production_python_files()
        for node in ast.walk(_tree(path))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"exception_message", "_exception_message"}
    ]
    assert definitions == [
        "src/aef_terminal/runtime/telemetry.py:exception_message",
    ]


def test_ibkr_package_init_does_not_aggregate_internal_owners() -> None:
    package_init = ROOT / "src/aef_terminal/data/ibkr/__init__.py"
    imports = [
        node
        for node in ast.walk(_tree(package_init))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    assert imports == []


def test_removed_futures_and_primary_history_mechanisms_are_not_callable() -> None:
    forbidden = {
        "_attach_persisted_current_contract",
        "attach_provider_trading_schedule",
        "chart_db_poll_delay_until_next_bar",
        "_historical_contract_for_symbol",
        "_chart_historical_contract_for_symbol",
        "_primary_historical_contract_for_symbol",
        "_contract_for_symbol",
        "_quote_contract_for_symbol",
        "_quote_contract_for_symbol_async_cache",
        "_current_future_contract_for_symbol",
        "_current_future_contract_for_symbol_async",
        "_discover_current_future_contract_for_symbol",
        "_chart_contract_for_symbol_async",
        "_symbol_tokens",
        "_ibkr_route_symbol",
        "_ibkr_route_instrument",
        "_qualified_primary_history_contract",
        "_merge_stock_session_history",
        "_merge_stock_session_history_sync",
        "_merge_stock_session_history_async",
        "read_futures_continuous_bars",
        "rebuild_futures_canonical_bars",
        "resolve_trading_schedule_contract_id",
        "read_futures_contract_for_timestamp",
        "delete_futures_history_window",
        "explicit_futures_contract_for_window",
        "futures_safe_backward_chunk_days",
        "load_chart_bars_from_db",
        "coalesced_load_chart_bars_from_db",
        "load_ibkr_bars_from_db",
        "async_load_ibkr_bars_from_db",
        "exact_gex_provider_symbol",
        "normalize_gex_asset",
        "persist_macro_quote_snapshots_async",
        "persist_macro_snapshots",
        "persist_vix_quote_snapshot_async",
        "macro_snapshot_write_stats",
        "reset_macro_quote_snapshot_state",
        "macro_watchlist_trend_points",
        "watchlist_quote_trend_points",
        "append_live_trend_point",
        "watchlist_trend_base",
    }
    violations: list[str] = []
    for path in _production_python_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:def {node.name}")
            if isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if name in forbidden:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:call {name}")
    assert violations == []


def test_removed_futures_series_and_slot_mismatch_mechanisms_cannot_return() -> None:
    forbidden = {
        "front_month_unadjusted",
        "raw_stitched",
        "slot_mismatch",
        "_intraday_bootstrap_range",
    }
    violations = []
    for path in _production_python_files():
        source = path.read_text(encoding="utf-8")
        violations.extend(
            f"{path.relative_to(ROOT)}:{token}" for token in forbidden if token in source
        )
    assert violations == []


def test_runtime_identity_uses_selected_watchlist_without_secondary_storage_lookup() -> None:
    source = inspect.getsource(runtime_instruments.lookup_runtime_instrument)
    assert "quote_stream.select_instruments" in source
    assert "client_settings" not in source
    assert "read_watchlist_snapshot" not in source
    assert "lookup_instrument" not in source
    assert "resolve_known_instrument" not in source
    assert "instrument_registry" not in source


def test_runtime_storage_identity_lookups_and_indexes_are_never_name_based() -> None:
    assert tuple(
        inspect.signature(instrument_repo.InstrumentsRepoMixin.lookup_instrument).parameters
    )[:2] == (
        "self",
        "instrument_id",
    )
    assert tuple(inspect.signature(runtime_instruments.lookup_runtime_instrument).parameters) == (
        "instrument_id",
    )
    assert tuple(inspect.signature(reference_services.selected_instruments).parameters)[:2] == (
        "instruments",
        "instrument_ids",
    )

    lookup_names = {"lookup_instrument", "lookup_runtime_instrument", "selected_instruments"}
    name_identifiers = {"symbol", "name", "instrument_key", "data_symbol", "display_name"}
    call_violations: list[str] = []
    for path in _production_python_files():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            call_name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if call_name not in lookup_names:
                continue
            argument_nodes = [*node.args, *(keyword.value for keyword in node.keywords)]
            referenced_names = {
                child.id
                for argument in argument_nodes
                for child in ast.walk(argument)
                if isinstance(child, ast.Name)
            }
            referenced_fields = {
                child.value
                for argument in argument_nodes
                for child in ast.walk(argument)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value in name_identifiers
            }
            keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
            banned = sorted(
                (referenced_names | referenced_fields | keyword_names) & name_identifiers
            )
            if banned:
                call_violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{call_name}({','.join(banned)})"
                )
    assert call_violations == []

    schema_tree = _tree(ROOT / "src/aef_terminal/storage/repos/schema.py")
    index_violations: list[str] = []
    banned_index_column = re.compile(
        r"\b(?:symbol|name|instrument_key|data_symbol|display_name)\b",
        re.IGNORECASE,
    )
    for node in ast.walk(schema_tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        statement = node.value
        index_match = re.search(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\b.*?\bON\s+[a-z_][a-z0-9_]*\s*\(",
            statement,
            re.IGNORECASE | re.DOTALL,
        )
        if index_match is None:
            continue
        indexed_clause = statement[index_match.end() :].split(")", 1)[0]
        banned = sorted(set(banned_index_column.findall(indexed_clause)))
        if banned:
            index_violations.append(
                f"src/aef_terminal/storage/repos/schema.py:{node.lineno}:{','.join(banned)}"
            )
    assert index_violations == []

    primary_key_violations: list[str] = []
    for node in ast.walk(schema_tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for primary_key_clause in re.findall(
            r"PRIMARY\s+KEY\s*\(([^)]*)\)",
            node.value,
            re.IGNORECASE | re.DOTALL,
        ):
            banned = sorted(set(banned_index_column.findall(primary_key_clause)))
            if banned:
                primary_key_violations.append(
                    f"src/aef_terminal/storage/repos/schema.py:{node.lineno}:{','.join(banned)}"
                )
    assert primary_key_violations == []

    # Provider search/display code and name-based indicator-setting templates are outside
    # this guard; only runtime/storage identity resolution and database indexes are gated.


def test_provider_identity_cannot_be_resolved_from_storage_or_symbol() -> None:
    source = Path("src/aef_terminal/data/instrument_identity.py").read_text(encoding="utf-8")
    lifecycle_source = Path("src/aef_terminal/data/futures_lifecycle.py").read_text(
        encoding="utf-8"
    )
    history_source = Path("src/aef_terminal/data/provider_history.py").read_text(encoding="utf-8")
    assert "resolve_provider_identity" not in source
    assert "lookup_instrument" not in source
    assert "lookup_instrument" not in lifecycle_source
    assert "canonical_futures_route_from_candidates" not in history_source


def test_provider_identity_rejects_legacy_alias_and_contract_fallback_fields() -> None:
    qualified = ibkr_stock_payload("SPY")
    qualified.pop("instrument_key")
    with pytest.raises(InstrumentIdentityError, match="INSTRUMENT_IDENTITY_INCOMPLETE"):
        require_provider_identity(qualified)

    qualified = ibkr_stock_payload("SPY")
    qualified.pop("provider_contract_id")
    qualified["contract_id"] = "756733"
    qualified["con_id"] = 756733
    with pytest.raises(InstrumentIdentityError, match="PROVIDER_CONTRACT_ID_REQUIRED"):
        require_provider_identity(qualified)


def test_strategy_profile_resolution_is_exact_and_never_parses_symbols() -> None:
    spy = ibkr_stock_payload("SPY")
    spy["provider_symbol"] = "NOT-SPY-ALIAS"
    assert resolve_instrument_profile(spy).key == "SPY"

    custom = ibkr_stock_payload("ESM")
    custom["asset_class"] = "index"
    custom["contract_identity"]["asset_class"] = "index"
    assert resolve_instrument_profile(custom).key == "CUSTOM"

    custom["profile"] = "ES"
    assert resolve_instrument_profile(custom).key == "ES"


def test_unknown_future_resolves_typed_generic_future_profile() -> None:
    future = ibkr_future_payload("MXU6")

    profile = resolve_instrument_profile(future)

    assert profile.key == "FUTURE"
    assert profile.family == "future"
    assert profile.is_futures is True


def test_removed_fuzzy_profile_detection_cannot_return() -> None:
    forbidden = {"detect_instrument_profile", "normalize_symbol", "active_profile_label"}
    violations: list[str] = []
    for path in _production_python_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:def {node.name}")
            if isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if name in forbidden:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:call {name}")
    assert violations == []


def test_ibkr_contract_construction_has_one_provider_qualified_route(monkeypatch) -> None:
    source = inspect.getsource(ibkr_contracts._contract_for_instrument)
    assert "_contract_from_instrument_identity" in source
    assert "_future_spec_from_instrument" in source
    assert "Stock(" not in source
    assert "Index(" not in source
    assert "Forex(" not in source

    monkeypatch.setattr(ibkr_contracts, "_ensure_event_loop", lambda: None)
    with pytest.raises(ValueError, match="INSTRUMENT_IDENTITY_REQUIRED"):
        ibkr_contracts._contract_for_instrument(None)


def test_futures_lifecycle_discovery_is_separate_from_live_consumer_identity() -> None:
    consumer_source = inspect.getsource(ibkr_contracts._current_future_contract)
    async_source = inspect.getsource(ibkr_contracts._current_future_contract_async)
    discovery_source = inspect.getsource(ibkr_contracts._discover_current_future_contract)

    assert "current_futures_contract(resolved_instrument)" in consumer_source
    assert "_validate_exact_future_contract" in consumer_source
    assert "qualifyContracts" not in consumer_source
    assert "_current_future_contract(" in async_source
    assert "_qualified_continuous_future_contract" in discovery_source
    assert "_qualify_exact_future_contract" in discovery_source


def test_trading_schedule_requires_explicit_provider_contract_id() -> None:
    fetch_source = inspect.getsource(ibkr_trading_hours.fetch_trading_hours_async)
    refresh_source = inspect.getsource(trading_hours_refresh)
    assert "IBKR_TRADING_SCHEDULE_CONTRACT_ID_REQUIRED" in fetch_source
    assert "parse_exact_positive_decimal_provider_id" in fetch_source
    assert "_chart_contract_for_symbol" not in fetch_source
    assert "_qualify_and_store_con_id" not in refresh_source


def test_removed_common_provider_dispatch_modules_cannot_return() -> None:
    removed = (
        ROOT / "src/aef_terminal/engine/gap_repair.py",
        ROOT / "src/aef_terminal/engine/futures_history.py",
        ROOT / "src/aef_terminal/engine/analyze/gap_bridge.py",
        ROOT / "src/aef_terminal/engine/analyze/patchable.py",
        ROOT / "src/aef_terminal/engine/snapshot/futures.py",
        ROOT / "src/aef_terminal/data/provider_adapters.py",
        ROOT / "src/aef_terminal/data/ibkr/gap_repair.py",
        ROOT / "src/aef_terminal/engine/instrument_registry.py",
        ROOT / "src/aef_terminal/engine/db_history.py",
        ROOT / "src/aef_terminal/ui/services/chart_stream_timing.py",
        ROOT / "src/aef_terminal/ui/services/macro_quote_snapshots.py",
    )
    assert [str(path.relative_to(ROOT)) for path in removed if path.exists()] == []


def test_data_and_storage_layers_do_not_import_engine() -> None:
    violations: list[str] = []
    for layer in ("data", "storage"):
        for path in sorted((ROOT / f"src/aef_terminal/{layer}").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imported = node.module or ""
                    if imported == "aef_terminal.engine" or imported.startswith(
                        "aef_terminal.engine."
                    ):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{imported}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "aef_terminal.engine" or alias.name.startswith(
                            "aef_terminal.engine."
                        ):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}"
                            )
    assert violations == []


def test_data_layer_does_not_depend_on_storage_implementations() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "src/aef_terminal/data").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if imported == "aef_terminal.storage" or imported.startswith(
                    "aef_terminal.storage."
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}:{imported}")
            elif isinstance(node, ast.Import):
                violations.extend(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}"
                    for alias in node.names
                    if alias.name == "aef_terminal.storage"
                    or alias.name.startswith("aef_terminal.storage.")
                )
    assert violations == []


def test_bar_reads_use_the_provider_gateway_and_storage_repositories() -> None:
    physical_readers = {
        "read_bars",
        "read_bars_multi",
        "read_recent_bars",
        "read_recent_bars_batch",
        "read_futures_canonical_bars",
        "read_futures_contract_bars",
        "read_bar_logical_projection",
        "read_confirmed_bar_timestamps",
    }
    violations: list[str] = []
    for path in _production_python_files():
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        internal_owner = (
            relative.startswith("src/aef_terminal/storage/")
            or relative == "src/aef_terminal/data/provider_history.py"
            or relative.startswith("src/aef_terminal/data/adapters/")
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in physical_readers
                and not internal_owner
            ):
                violations.append(f"{relative}:{node.lineno}:{node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module == (
                "aef_terminal.data.provider_history"
            ):
                allowed_import = (
                    relative == "src/aef_terminal/data/providers.py"
                    or relative.startswith("src/aef_terminal/data/adapters/")
                )
                if not allowed_import:
                    violations.append(f"{relative}:{node.lineno}:provider_history_import")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"load_bars", "async_load_bars"}
                and relative != "src/aef_terminal/data/providers.py"
                and not relative.startswith("src/aef_terminal/data/adapters/")
            ):
                violations.append(f"{relative}:{node.lineno}:adapter_{node.attr}")
    assert violations == []


def test_database_connections_stay_inside_storage() -> None:
    violations: list[str] = []
    for path in _production_python_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("src/aef_terminal/storage/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"_connect", "cursor"}:
                violations.append(f"{relative}:{node.lineno}:{node.attr}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"psycopg", "psycopg_pool"}:
                        violations.append(f"{relative}:{node.lineno}:{alias.name}")
            if isinstance(node, ast.ImportFrom) and node.module in {
                "psycopg",
                "psycopg_pool",
            }:
                violations.append(f"{relative}:{node.lineno}:{node.module}")
    assert violations == []


def test_supervised_background_loops_do_not_abandon_thread_work() -> None:
    owners = (
        "src/aef_terminal/ui/services/host_sleep_monitor.py",
        "src/aef_terminal/ui/services/trading_hours_refresh.py",
        "src/aef_terminal/ui/services/gex_scheduler.py",
        "src/aef_terminal/ui/services/ibkr_self_heal.py",
        "src/aef_terminal/ui/services/server_alert_monitor.py",
        "src/aef_terminal/indicators/modules/discord_signals/service.py",
    )
    for relative in owners:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "asyncio.to_thread(" not in source, relative
        assert "run_physical_thread_call(" in source, relative


def test_terminal_message_threads_use_physical_start_and_drain_boundaries() -> None:
    bootstrap_source = (ROOT / "src/aef_terminal/ui/bootstrap.py").read_text(encoding="utf-8")
    wiring_source = (ROOT / "src/aef_terminal/ui/app_runtime_wiring.py").read_text(encoding="utf-8")

    assert "await deps.ensure_paper_telegram_worker()" in bootstrap_source
    assert "await deps.ensure_telegram_interactive_bot()" in bootstrap_source
    assert "await deps.stop_telegram_interactive_bot()" in bootstrap_source
    assert "await deps.stop_paper_telegram_worker()" in bootstrap_source
    assert "ensure_paper_telegram_worker_async" in wiring_source
    assert "ensure_telegram_interactive_bot_async" in wiring_source
    assert "stop_paper_telegram_worker_async" in wiring_source
    assert "stop_telegram_interactive_bot_async" in wiring_source


def test_ibkr_history_has_only_the_manager_loop_watchdog() -> None:
    runtime_source = (ROOT / "src/aef_terminal/data/ibkr/runtime.py").read_text(encoding="utf-8")
    bars_source = (ROOT / "src/aef_terminal/data/ibkr/bars.py").read_text(encoding="utf-8")

    assert "class _AsyncIbkrActivityWatchdog" in runtime_source
    assert "class _IbkrActivityWatchdog" not in runtime_source
    assert "_AsyncIbkrActivityWatchdog(" in bars_source


def test_ibkr_teardown_has_no_detached_recovery_thread() -> None:
    session_source = (ROOT / "src/aef_terminal/data/ibkr/session.py").read_text(encoding="utf-8")
    event_loop_source = (ROOT / "src/aef_terminal/data/ibkr/event_loop.py").read_text(
        encoding="utf-8"
    )
    manager_source = (ROOT / "src/aef_terminal/data/ibkr/manager.py").read_text(encoding="utf-8")

    assert "ibkr-quarantine-owner-loop" not in session_source
    assert "run_physical_thread_call(_reset_ibkr_quarantined_sessions)" in event_loop_source
    assert "settle_physical_task(wrapped_future)" in manager_source


def test_chart_storage_workers_use_physical_completion_boundaries() -> None:
    adapter_paths = (
        "src/aef_terminal/data/adapters/base.py",
        "src/aef_terminal/data/adapters/ibkr_bars.py",
        "src/aef_terminal/data/adapters/coinbase.py",
        "src/aef_terminal/data/adapters/_tinvest/provider.py",
    )
    for relative in adapter_paths:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "asyncio.to_thread(" not in source, relative
        assert "run_physical_thread_call(" in source, relative

    history_source = (ROOT / "src/aef_terminal/ui/services/chart_history.py").read_text(
        encoding="utf-8"
    )
    coordinator_source = (
        ROOT / "src/aef_terminal/ui/services/chart_stream_coordinator.py"
    ).read_text(encoding="utf-8")
    assert "run_in_executor(" not in history_source
    assert "asyncio.to_thread(" not in history_source
    assert "run_physical_executor_call(" in history_source
    assert "run_physical_thread_call(" in history_source
    assert "run_in_executor(" not in coordinator_source
    assert "run_physical_executor_call(" in coordinator_source


def test_snapshot_analysis_drains_parallel_physical_work() -> None:
    read_context_source = inspect.getsource(
        __import__(
            "aef_terminal.engine.snapshot.read_context",
            fromlist=["SnapshotReadContext"],
        ).SnapshotReadContext
    )
    assembly_source = (ROOT / "src/aef_terminal/engine/snapshot/assembly.py").read_text(
        encoding="utf-8"
    )

    assert "run_cancellation_deferred(" in read_context_source
    assert "return_exceptions=True" in read_context_source
    assert "await asyncio.to_thread(" not in read_context_source
    assert "run_physical_thread_call(" in assembly_source
    assert "await asyncio.to_thread(" not in assembly_source


def test_history_repair_storage_lookup_uses_physical_completion_boundary() -> None:
    repair_module = __import__(
        "aef_terminal.data.history_repair",
        fromlist=["execute_history_repair"],
    )
    lookup_source = inspect.getsource(repair_module._lookup_persisted_instrument)
    execute_source = inspect.getsource(repair_module.execute_history_repair)

    assert "asyncio.to_thread(" not in lookup_source
    assert "run_physical_thread_call(" in lookup_source
    assert "store = await run_physical_thread_call(store_factory)" in execute_source


def test_ibkr_runtime_settings_stay_on_the_event_loop() -> None:
    runtime_source = (ROOT / "src/aef_terminal/ui/ibkr_runtime.py").read_text(encoding="utf-8")

    assert "asyncio.to_thread(" not in runtime_source
    assert "return apply_ibkr_runtime_settings(settings)" in runtime_source


def test_state_mutating_workers_use_physical_completion_boundaries() -> None:
    function_sources = (
        inspect.getsource(
            __import__(
                "aef_terminal.ui.services.quote_snapshots",
                fromlist=["persist_quote_snapshots_async"],
            ).persist_quote_snapshots_async
        ),
        inspect.getsource(
            __import__(
                "aef_terminal.ui.services.market_analysis_job",
                fromlist=["hydrate_manual_channel_analysis_params"],
            ).hydrate_manual_channel_analysis_params
        ),
        inspect.getsource(
            __import__(
                "aef_terminal.ui.services.market_analysis_research_worker",
                fromlist=["MarketAnalysisResearchWorker"],
            ).MarketAnalysisResearchWorker._run
        ),
        inspect.getsource(
            __import__(
                "aef_terminal.ui.services.fast_indicator_runtime",
                fromlist=["option_target_samples_committed"],
            ).option_target_samples_committed
        ),
    )
    for source in function_sources:
        assert "asyncio.to_thread(" not in source
        assert "run_physical_thread_call(" in source


def test_canonical_bar_mutations_have_one_runtime_pipeline() -> None:
    physical_mutators = {
        "write_bars",
        "write_futures_canonical_bars",
        "write_futures_contract_bars",
        "commit_history_result",
    }
    violations: list[str] = []
    for path in _production_python_files():
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        internal_owner = (
            relative.startswith("src/aef_terminal/storage/")
            or relative.startswith("src/aef_terminal/data/adapters/")
            or relative == "src/aef_terminal/data/ibkr/history.py"
            or relative == "src/aef_terminal/runtime/chart_commits.py"
        )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in physical_mutators
                and not internal_owner
            ):
                violations.append(f"{relative}:{node.lineno}:{node.attr}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "async_commit_chart_bars"
                and relative != "src/aef_terminal/runtime/chart_commits.py"
            ):
                violations.append(f"{relative}:{node.lineno}:async_commit_chart_bars")
        continuous_owner = (
            relative.startswith("src/aef_terminal/data/adapters/")
            or relative == "src/aef_terminal/data/ibkr/history.py"
        )
        if "write_continuous_history" in source and not continuous_owner:
            violations.append(f"{relative}:continuous_history_writer")
        if "replace_futures_canonical_window" in source or "chunk_backfill" in source:
            violations.append(f"{relative}:retired_bar_writer")
    assert violations == []


def test_calculation_layers_do_not_route_or_branch_by_provider() -> None:
    calculation_paths = (
        ROOT / "src/aef_terminal/runtime/math_utils.py",
        ROOT / "src/aef_terminal/runtime/pine.py",
        *(ROOT / "src/aef_terminal/features").rglob("*.py"),
        *(ROOT / "src/aef_terminal/indicators").rglob("*.py"),
    )
    provider_name = re.compile(r"\b(?:ibkr|coinbase|tinvest|t-invest)\b", re.IGNORECASE)
    routing_calls = ("route_instrument(", "provider_key_for_source(", "instrument_provider(")
    violations: list[str] = []
    for path in calculation_paths:
        source = path.read_text(encoding="utf-8")
        if provider_name.search(source) or any(call in source for call in routing_calls):
            violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_common_session_and_storage_modules_have_no_ibkr_branch() -> None:
    common_modules = (
        ROOT / "src/aef_terminal/data/provider_sessions.py",
        ROOT / "src/aef_terminal/data/provider_history.py",
        ROOT / "src/aef_terminal/runtime/bar_series.py",
        ROOT / "src/aef_terminal/storage/repos/futures.py",
    )
    violations = [
        str(path.relative_to(ROOT))
        for path in common_modules
        if '== "ibkr"' in path.read_text(encoding="utf-8")
        or '!= "ibkr"' in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_gex_broker_lanes_do_not_own_database_io() -> None:
    request_module = __import__("aef_terminal.data.gex.context", fromlist=["async_gex_context"])
    live_module = __import__("aef_terminal.data.gex.live", fromlist=["async_live_gex_context"])
    request_owned = inspect.getsource(request_module._collect_live_gex_context_owned)
    live_owned = inspect.getsource(live_module._live_gex_context_owned)
    live_frame = inspect.getsource(live_module._live_gex_frame_from_session)

    for source in (request_owned, live_owned, live_frame):
        assert "store" not in source
        assert "persist_gex_snapshot" not in source
        assert "read_gex_snapshot" not in source
        assert "_maybe_update_dividend_yield_cache" not in source
    live_entry = inspect.getsource(live_module.async_live_gex_context)
    request_entry = inspect.getsource(request_module.async_gex_context)
    request_result = inspect.getsource(request_module._gex_request_result)
    assert request_entry.count("_gex_request_result(") == 2
    assert request_result.count("await_cancellation_deferred_task(") == 2
    assert "run_physical_thread_call(" in request_result
    assert "_project_gex_request_history" in request_result
    assert "asyncio.shield(" not in request_result
    assert "asyncio.shield(" not in request_entry
    assert "owned_frame.session_token" in live_entry
    assert "asyncio.create_task(" in live_entry
    assert "_finalize_live_gex_frame(" in live_entry
    assert "await_cancellation_deferred_task(" in live_entry
    assert "await asyncio.shield(finalization_task)" not in live_entry
    assert live_entry.index("provider_runtime.run_coroutine") < live_entry.index(
        "_finalize_live_gex_frame"
    )
    live_finalize = inspect.getsource(live_module._finalize_live_gex_frame)
    request_owner = inspect.getsource(request_module._async_gex_context_owned)
    assert "_LIVE_GEX_SESSIONS" not in live_finalize
    assert "_prepare_live_gex_finalization_owned" in live_finalize
    assert "asyncio.to_thread(" not in request_owner
    assert "asyncio.to_thread(" not in live_entry
    assert "asyncio.to_thread(" not in live_finalize
    assert "run_physical_thread_call(" in request_owner
    assert "run_physical_thread_call(" in live_entry
    assert "run_physical_thread_call(" in live_finalize
    assert "_commit_live_gex_finalization_owned" in live_finalize
    for owned_state_transition in (
        inspect.getsource(live_module._prepare_live_gex_finalization_owned),
        inspect.getsource(live_module._commit_live_gex_finalization_owned),
    ):
        assert "asyncio.to_thread(" not in owned_state_transition
        assert "persist_gex_snapshot" not in owned_state_transition


def test_gex_option_volume_context_has_one_public_shape() -> None:
    payload_module = __import__("aef_terminal.data.gex.payload", fromlist=["_aggregate_by_strike"])
    payload_contract_module = __import__(
        "aef_terminal.data.gex.payload_contract",
        fromlist=["_validated_option_volume_context"],
    )
    history_module = __import__(
        "aef_terminal.data.gex.history", fromlist=["_read_snapshot_history_from_db"]
    )
    live_module = __import__(
        "aef_terminal.data.gex.live",
        fromlist=["_attach_live_option_volume_events"],
    )
    projection_source = inspect.getsource(payload_module._option_volume_context_from_strike)
    validation_source = inspect.getsource(payload_contract_module._validated_option_volume_context)
    history_source = inspect.getsource(history_module._read_snapshot_history_from_db)
    live_event_source = inspect.getsource(live_module._attach_live_option_volume_events)

    assert "_OPTION_VOLUME_CURRENT_FIELDS" in validation_source
    assert "_OPTION_VOLUME_EVENT_FIELDS" in validation_source
    assert "_OPTION_VOLUME_EVENT_FIELDS" in projection_source
    assert 'row["_option_volume_event"]' in live_event_source
    assert 'row["live_flow"]' not in live_event_source
    assert "level_process_label" not in live_event_source
    assert "_live_option_volume_interval_rates" in live_event_source
    assert "require_gex_snapshot_payload(" in history_source
    assert 'projection="history"' in history_source
    assert "max_levels=max_levels" in history_source
    assert "require_gex_levels(" not in history_source
    assert "_validated_option_volume_context(" not in history_source
    assert (
        'compact_level["option_volume_context"] = level["option_volume_context"]' in history_source
    )
    assert "_option_volume_context_from_strike" not in history_source

    forbidden_public_paths = (
        "live_flow",
        "level_process_label",
        "level_process_state",
        "level_cross_direction",
        "volume_delta_vs_oi",
        "volume_delta_vs_rolling_median",
        "turnover_since_open",
        "current_volume_material",
    )
    public_contract_files = (
        ROOT / "src/aef_terminal/data/gex/live.py",
        ROOT / "src/aef_terminal/ui/assets/js/16-gex-context-contract.js",
        ROOT / "src/aef_terminal/ui/assets/js/16-gex-context-runtime.js",
        ROOT / "src/aef_terminal/ui/assets/js/50-overlays-gex-levels.js",
        ROOT / "src/aef_terminal/ui/assets/js/50-overlays-gex-profile.js",
    )
    violations = [
        f"{path.relative_to(ROOT)}:{token}"
        for path in public_contract_files
        for token in forbidden_public_paths
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_option_target_writers_have_disjoint_locked_storage_contracts() -> None:
    repo_module = __import__("aef_terminal.storage.repos.gex", fromlist=["GexRepoMixin"])
    reprice_module = __import__(
        "aef_terminal.ui.services.option_target_reprice",
        fromlist=["run_option_target_reprice_loop"],
    )
    create_write = inspect.getsource(repo_module.GexRepoMixin.create_option_target_intent)
    update_write = inspect.getsource(repo_module.GexRepoMixin.update_option_target_intent)
    market_write = inspect.getsource(repo_module.GexRepoMixin.update_option_target_market_samples)
    reprice_loop = inspect.getsource(reprice_module.run_option_target_reprice_loop)
    reprice_cycle = inspect.getsource(reprice_module._run_option_target_reprice_cycle)
    reprice_commit = inspect.getsource(reprice_module._commit_option_target_updates)
    repo_source = inspect.getsource(repo_module)
    reprice_source = inspect.getsource(reprice_module)
    option_target_contract_source = Path(
        "src/aef_terminal/data/gex/option_target_contract.py"
    ).read_text(encoding="utf-8")

    assert "ON CONFLICT (instrument_id, route_fingerprint, id) DO NOTHING" in create_write
    assert "OPTION_TARGET_CREATE_CONFLICT" in create_write
    assert "FOR UPDATE" in update_write
    assert "OPTION_TARGET_NOT_FOUND" in update_write
    assert "AND instrument_id = %s" in update_write
    assert "AND route_fingerprint = %s" in update_write
    assert "_option_target_intent_update_payload" in update_write
    assert "FOR UPDATE" in market_write
    assert "_option_target_market_sample_update_payload" in market_write
    assert "RETURNING target.instrument_id" in market_write
    assert "_run_option_target_reprice_cycle" in reprice_loop
    assert "_commit_option_target_updates" in reprice_cycle
    assert "store.update_option_target_market_samples" in reprice_commit
    assert "run_cancellation_deferred" in reprice_commit
    assert "run_physical_thread_call" in reprice_commit
    assert "asyncio.to_thread" not in reprice_commit
    assert "reconcile_option_target_quote_consumers" in reprice_cycle
    assert "option_target_samples_committed" in reprice_cycle
    assert "run_cancellation_deferred" in reprice_cycle
    assert "run_physical_thread_call" in reprice_cycle
    assert "asyncio.to_thread" not in reprice_cycle
    assert "asyncio.to_thread" not in reprice_loop
    assert "upsert_option_target" not in repo_source
    assert "upsert_option_target" not in reprice_loop
    assert "from aef_terminal.data.gex.option_target_contract import (" in repo_source
    assert "from aef_terminal.data.gex.option_target_contract import (" in reprice_source
    assert "instrument_id, route_fingerprint, option_id" in market_write
    assert "requested.instrument_id = option_targets.instrument_id" in market_write
    assert "requested.route_fingerprint = option_targets.route_fingerprint" in market_write
    assert "OPTION_TARGET_MARKET_SAMPLE_FIELDS =" not in repo_source
    assert "OPTION_TARGET_MARKET_SAMPLE_FIELDS =" not in reprice_source
    assert "def _attach_market_sample" not in reprice_source
    assert "OPTION_TARGET_ROUTE_LIMIT" in create_write
    assert "option-target-limit:" in create_write
    assert "pg_advisory_xact_lock" in create_write
    assert "ORDER BY created_at DESC, id DESC" in create_write
    assert 'payload.get("target_price")' not in reprice_source
    assert 'payload.get("target_ts")' not in reprice_source
    assert "abs(price - last)" not in reprice_source
    assert "require_option_target_underlying_quote(" in reprice_source
    assert "market_data_price_source_is_current(" in option_target_contract_source
    assert "price_source in" not in reprice_source
    assert "option_target_market_sample(result, sec_type=exact_sec_type)" in reprice_source
    assert '"reference_option_price_source",' in reprice_source
    assert "max(int(max_rows)" not in reprice_source
    assert "max(float(poll_seconds)" not in reprice_source


def test_option_target_repricing_stays_out_of_full_analysis_polling() -> None:
    analysis_store = __import__(
        "aef_terminal.ui.services.market_analysis_store",
        fromlist=["market_analysis_requires_periodic_refresh"],
    )
    quote_stream = __import__(
        "aef_terminal.ui.services.quote_stream_ws",
        fromlist=["run_quote_producer"],
    )
    analysis_source = inspect.getsource(analysis_store)
    quote_stream_source = inspect.getsource(quote_stream)
    periodic_refs = analysis_source[
        analysis_source.index("_PERIODIC_SHARED_CONTEXT_REFS") : analysis_source.index(
            "def _market_analysis_cache_snapshot_bytes"
        )
    ]

    assert '"option_targets"' not in periodic_refs
    assert '"type": "option_targets_snapshot"' in quote_stream_source
    assert "producer.targets_revision" in quote_stream_source


def test_option_target_layers_and_orchestration_are_separate() -> None:
    contract_source = Path("src/aef_terminal/data/gex/option_target_contract.py").read_text(
        encoding="utf-8"
    )
    valuation_source = Path("src/aef_terminal/data/gex/option_target_valuation.py").read_text(
        encoding="utf-8"
    )
    selection_source = Path("src/aef_terminal/data/gex/option_target_selection.py").read_text(
        encoding="utf-8"
    )
    universe_source = Path("src/aef_terminal/data/gex/option_target_universe.py").read_text(
        encoding="utf-8"
    )
    orchestration_source = Path("src/aef_terminal/data/gex/option_targets.py").read_text(
        encoding="utf-8"
    )

    assert "def option_target_market_sample" in contract_source
    assert "def require_option_target_underlying_quote" in contract_source
    assert "def option_target_dte_for_expiry" in contract_source
    assert "def option_target_price" not in contract_source
    assert "def _apply_option_live_quote" not in contract_source
    assert "def _apply_option_live_quote" in valuation_source
    assert "def _refresh_fair_from_market_quote" in valuation_source
    assert "def option_target_market_sample" not in valuation_source
    assert "def option_target_price" not in valuation_source
    assert "def _option_target_profile" in selection_source
    assert "def _eligible_option_target_expiries" in selection_source
    assert "def _rank_option_target_candidates" in selection_source
    assert "def option_target_price" not in selection_source
    assert "def _finalize_option_target_payload" not in selection_source
    assert "def _require_option_target_contracts" in universe_source
    assert "def _characterize_option_target_contracts" in universe_source
    assert "option_contract_provider" not in universe_source
    assert "def option_target_price" not in universe_source
    assert "def option_target_price" in orchestration_source
    assert "def _finalize_option_target_payload" in orchestration_source
    assert "def _option_target_profile" not in orchestration_source
    assert "def _eligible_option_target_expiries" not in orchestration_source
    assert "def _rank_option_target_candidates" not in orchestration_source
    assert "def _require_option_target_contracts" not in orchestration_source
    assert "def _characterize_option_target_contracts" not in orchestration_source


def test_indicator_producers_do_not_emit_renderer_color_fields() -> None:
    forbidden = (
        '"color":',
        '"bg":',
        '"border":',
        '"accent":',
        "color=",
        "bg=",
        "border=",
        "accent=",
    )
    violations = [
        f"{path.relative_to(ROOT)}:{token}"
        for path in (ROOT / "src/aef_terminal/indicators/modules").glob("*.py")
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]
    assert violations == []


def test_gex_runtime_state_and_scheduler_are_route_scoped() -> None:
    request_config_source = Path("src/aef_terminal/data/gex/config.py").read_text(encoding="utf-8")
    session_source = Path("src/aef_terminal/data/gex/session.py").read_text(encoding="utf-8")
    transport_source = Path("src/aef_terminal/data/ibkr/gex.py").read_text(encoding="utf-8")
    status_source = Path("src/aef_terminal/data/gex/scheduler.py").read_text(encoding="utf-8")
    scheduler_source = Path("src/aef_terminal/ui/services/gex_scheduler.py").read_text(
        encoding="utf-8"
    )

    assert "routes: dict[tuple[str, str], GexRouteRuntimeState]" in session_source
    assert "errors: dict[str, GexRuntimeErrorState]" in session_source
    assert "def record_error(" in session_source
    assert "def error_for_route(" in session_source
    assert "def clear_request_errors(" in session_source
    assert "last_error_asset" not in session_source
    assert '("timeout_seconds", 5.0, 90.0)' in request_config_source
    for provider_transport_field in (
        "host",
        "port",
        "client_id",
        "readonly",
        "market_data_type",
    ):
        assert f"    {provider_transport_field}:" not in request_config_source
    assert "deadline = _time_monotonic() + float(request.timeout)" in transport_source
    assert "max(float(request.timeout)" not in transport_source
    assert "min(float(request.timeout)" not in transport_source
    assert "deadline=deadline" in transport_source
    assert "_raise_if_gex_deadline(deadline" in transport_source
    assert "parse_exact_positive_decimal_provider_id" in transport_source
    assert 'int(getattr(underlying, "conId"' not in transport_source
    assert '"routes": route_runtime' in status_source
    assert '"queue_last_error"' not in status_source
    assert "class GexSchedulerRouteState" in scheduler_source
    assert "_GEX_SCHEDULER_MAX_ATTEMPTS = 2" in scheduler_source
    assert "def _run_gex_scheduler_attempt(" in scheduler_source
    assert "run_cancellation_deferred(" in scheduler_source
    assert "run_physical_thread_call(" in scheduler_source
    assert "request = await asyncio.to_thread(" not in scheduler_source
    assert 'scheduler.get("settings_error") is not None' in scheduler_source
    assert "scheduler_state: dict[tuple[str, str], GexSchedulerRouteState]" in scheduler_source
    assert "(route.instrument_id, route.fingerprint) != state_key" in scheduler_source
    assert "retry_pending = state is not None and not state.completed" in scheduler_source


def test_gex_repository_calls_have_one_runtime_owner() -> None:
    allowed = Path("src/aef_terminal/data/gex/history.py").resolve()
    violations: list[str] = []
    for path in (ROOT / "src/aef_terminal").rglob("*.py"):
        if path.resolve() == allowed:
            continue
        source = path.read_text(encoding="utf-8")
        for token in (
            ".upsert_gex_snapshot(",
            ".read_gex_snapshots(",
            ".read_latest_gex_snapshot(",
        ):
            if token in source:
                violations.append(f"{path.relative_to(ROOT)}:{token}")
    assert violations == []


def test_gex_stream_history_is_scoped_to_the_exact_route() -> None:
    history_owner_source = inspect.getsource(gex_stream_ws._load_gex_history_read_model)
    producer_source = inspect.getsource(gex_stream_ws._run_gex_stream_producer)
    stream_source = inspect.getsource(gex_stream_ws.run_gex_stream)
    assert "instrument_id=producer.instrument_id" in history_owner_source
    assert "route_fingerprint=producer.route.fingerprint" in history_owner_source
    assert "instrument_id=route.instrument_id" in stream_source
    assert "asyncio.to_thread(" not in producer_source
    assert "run_physical_thread_call(" in producer_source
    assert "asyncio.to_thread(" not in stream_source
    assert "run_physical_thread_call(" in stream_source


def test_chart_stream_route_lookup_uses_physical_completion_boundary() -> None:
    stream_source = inspect.getsource(chart_stream_ws.run_chart_stream)

    assert "asyncio.to_thread(" not in stream_source
    assert "run_physical_thread_call(" in stream_source


def test_browser_capture_socket_drains_physical_receiver_work() -> None:
    capture_module = __import__(
        "aef_terminal.ui.services.browser_capture",
        fromlist=["run_browser_capture_socket"],
    )
    receiver_source = inspect.getsource(capture_module._browser_capture_receiver)
    session_source = inspect.getsource(capture_module._run_browser_capture_session)
    socket_source = inspect.getsource(capture_module.run_browser_capture_socket)

    assert "asyncio.to_thread(" not in receiver_source
    assert "run_physical_thread_call(" in receiver_source
    assert "_settle_browser_capture_tasks(tasks)" in session_source
    assert "asyncio.to_thread(" not in socket_source
    assert "run_physical_thread_call(" in socket_source


def test_market_and_screener_reads_use_owned_physical_boundaries() -> None:
    market_module = __import__(
        "aef_terminal.ui.market_actions",
        fromlist=["resolve_market_route_or_http_error"],
    )
    screener_module = __import__(
        "aef_terminal.ui.screener_services",
        fromlist=["screener_snapshot"],
    )
    route_source = inspect.getsource(market_module.resolve_market_route_or_http_error)
    chart_source = inspect.getsource(market_module.chart_only_market_response)
    trend_owner_source = inspect.getsource(screener_module._build_watchlist_trend_snapshot)
    trend_source = inspect.getsource(screener_module.screener_trends_snapshot)
    screener_source = inspect.getsource(screener_module.screener_snapshot)

    for source in (route_source, chart_source, trend_owner_source, screener_source):
        assert "asyncio.to_thread(" not in source
        assert "run_physical_thread_call(" in source
    assert "asyncio.to_thread(" not in trend_source
    assert "resolve_route_selection(" in trend_source


def test_gex_http_request_uses_owned_physical_boundaries() -> None:
    router_source = inspect.getsource(gex_router.create_gex_router)

    assert "asyncio.to_thread(" not in router_source
    assert "run_physical_thread_call(" in router_source


def test_external_ai_and_ibkr_startup_calls_use_physical_boundaries() -> None:
    ai_router_source = (
        ROOT / "src/aef_terminal/indicators/modules/ai_third_opinion/router.py"
    ).read_text(encoding="utf-8")
    manager_source = inspect.getsource(
        __import__(
            "aef_terminal.data.ibkr.manager",
            fromlist=["IbkrMarketDataManager"],
        ).IbkrMarketDataManager.run_coroutine
    )

    assert "asyncio.to_thread(" not in ai_router_source
    assert ai_router_source.count("run_physical_thread_call(") == 3
    assert "await asyncio.to_thread(self.ensure_loop)" not in manager_source
    assert "await run_physical_thread_call(self.ensure_loop)" in manager_source


def test_asset_and_system_request_workers_have_one_drained_owner() -> None:
    asset_source = (ROOT / "src/aef_terminal/ui/asset_services.py").read_text(encoding="utf-8")
    system_source = (ROOT / "src/aef_terminal/ui/routers/system.py").read_text(encoding="utf-8")

    assert "asyncio.to_thread(" not in asset_source
    assert "_ASSET_CACHE_LOCK = RLock()" in asset_source
    assert asset_source.count("run_physical_thread_call(") == 8
    assert "asyncio.to_thread(" not in system_source
    assert system_source.count("run_physical_thread_call(") == 2


def test_discord_companion_shutdown_does_not_abandon_owned_threads() -> None:
    source = (ROOT / "src/aef_terminal/indicators/modules/discord_signals/companion.py").read_text(
        encoding="utf-8"
    )

    assert ".join(timeout=" not in source
    assert source.count(".join()") == 3


def test_gex_manual_refreshes_have_one_bootstrap_drained_owner() -> None:
    router_source = inspect.getsource(gex_router.create_gex_router)
    owner_source = inspect.getsource(gex_router.GexManualRefreshOwner)
    bootstrap_source = (ROOT / "src/aef_terminal/ui/bootstrap.py").read_text(encoding="utf-8")
    wiring_source = (ROOT / "src/aef_terminal/ui/app_runtime_wiring.py").read_text(encoding="utf-8")

    assert "refresh_tasks:" not in router_source
    assert "refresh_results:" not in router_source
    assert "manual_refresh_owner.start_refresh(" in router_source
    assert "await asyncio.gather(*tasks, return_exceptions=True)" in owner_source
    assert "await deps.stop_request_task_owners()" in bootstrap_source
    for shutdown in (
        "GEX_MANUAL_REFRESH_OWNER.shutdown()",
        "screener_services.shutdown_screener_trend_runtime()",
        "shutdown_chart_history_runtime()",
        "provider_registry.shutdown_provider_bar_load_runtime()",
    ):
        assert shutdown in wiring_source


def test_instrument_first_provider_contract_replaces_legacy_dispatch_helpers() -> None:
    for name in (
        "provider_requires_canonical_futures",
        "provider_for_instrument",
        "canonical_history_storage_route",
        "fetch_live_quotes",
        "fetch_provider_trading_schedule",
        "provider_gap_repair_candidate",
        "async_refresh_provider_gap",
        "schedule_provider_gap_repair",
        "create_bar_feed",
    ):
        assert not hasattr(provider_registry, name)
    assert not hasattr(sessions, "ibkr_schedule_open_state")
    assert not hasattr(sessions, "mark_ibkr_trading_hours_stale")
    assert not any((ROOT / "src/aef_terminal/engine/bar_loaders").glob("*.py"))


def test_canonical_history_route_is_typed_provider_metadata() -> None:
    route = CanonicalHistoryRoute("provider_native", "provider_managed")
    assert route.series_type == "provider_native"
    assert route.roll_policy == "provider_managed"
    assert route.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS

    exact = CanonicalHistoryRoute.exact_futures_contract("contract-uid")
    assert exact.storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTRACT
    assert exact.contract_key == "contract-uid"
    assert exact.series_type == ""
    assert exact.roll_policy == ""

    with pytest.raises(ValueError, match="requires contract_key only"):
        CanonicalHistoryRoute(
            series_type="provider_native",
            storage_kind=CanonicalHistoryStorageKind.FUTURES_CONTRACT,
            contract_key="contract-uid",
        )


def test_every_provider_declares_one_typed_manifest() -> None:
    for entry in provider_registry.provider_catalog():
        provider = provider_registry.get_provider(entry["key"])
        assert isinstance(provider.manifest, ProviderManifest)
        assert provider.manifest.key == provider.key
        assert provider.catalog_entry() == provider.manifest.as_dict()


def test_history_and_feed_entrypoints_are_instrument_first() -> None:
    contracts = (
        (ProviderAdapter, "async_load_bars"),
        (TradingScheduleProvider, "fetch_trading_schedule"),
        (HistoryRepairProvider, "history_request_identity"),
        (HistoryRepairProvider, "history_request_max_span"),
    )
    for owner, method_name in contracts:
        signature = inspect.signature(getattr(owner, method_name))
        parameters = tuple(signature.parameters)
        assert parameters[:2] == ("self", "instrument")

    production = "\n".join(path.read_text(encoding="utf-8") for path in _production_python_files())
    assert "provider_for_instrument" not in production
    assert "async def async_load_provider_bars(" in production
    assert "route: InstrumentRoute" in production


def test_provider_optional_interfaces_match_declared_capabilities() -> None:
    ibkr = provider_registry.get_provider("ibkr")
    coinbase = provider_registry.get_provider("coinbase")
    tinvest = provider_registry.get_provider("tinvest")

    assert isinstance(ibkr, ProviderAdapter)
    assert isinstance(coinbase, ProviderAdapter)
    assert isinstance(tinvest, ProviderAdapter)

    assert isinstance(ibkr, NativeChartLiveProvider)
    assert isinstance(ibkr, ChartCommitProvider)
    assert isinstance(ibkr, QuoteStreamProvider)
    assert isinstance(ibkr, TradingScheduleProvider)
    assert isinstance(ibkr, GexProvider)
    assert isinstance(ibkr, OptionsProvider)

    assert isinstance(coinbase, ChartLiveProvider)
    assert isinstance(coinbase, ChartCommitProvider)
    assert isinstance(coinbase, QuotePollingProvider)
    assert not isinstance(coinbase, NativeChartLiveProvider)
    assert not isinstance(coinbase, TradingScheduleProvider)
    assert not isinstance(coinbase, GexProvider)
    assert not isinstance(coinbase, OptionsProvider)
    assert not hasattr(coinbase, "async_load_gex")

    assert isinstance(tinvest, ChartLiveProvider)
    assert isinstance(tinvest, ChartCommitProvider)
    assert isinstance(tinvest, QuotePollingProvider)
    assert isinstance(tinvest, TradingScheduleProvider)
    assert not isinstance(tinvest, NativeChartLiveProvider)
    assert not isinstance(tinvest, GexProvider)
    assert not isinstance(tinvest, OptionsProvider)
    assert not hasattr(tinvest, "async_load_gex")


def test_realtime_chart_coordinator_is_the_only_live_and_checkpoint_owner() -> None:
    websocket_source = (ROOT / "src/aef_terminal/ui/services/chart_stream_ws.py").read_text(
        encoding="utf-8"
    )
    coordinator_source = (
        ROOT / "src/aef_terminal/ui/services/chart_stream_coordinator.py"
    ).read_text(encoding="utf-8")
    commit_source = (ROOT / "src/aef_terminal/runtime/chart_commits.py").read_text(encoding="utf-8")

    assert "acquire_coordinator" in websocket_source
    assert "async_chart_live_bars" not in websocket_source
    assert "coalesced_load_confirmed_chart_bars" not in websocket_source
    assert "async_chart_live_bars" in coordinator_source
    assert "coalesced_load_confirmed_chart_bars" in coordinator_source
    assert "chart_commits.submit" in coordinator_source
    assert "async_commit_chart_bars" in commit_source

    obsolete = (
        "_CHART_STREAM_PRODUCERS",
        "_produce_chart_stream",
        "chart_recovery_payloads",
        "read_gap_repair_bars",
        "persist_chart_stream_bars",
    )
    scoped_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "src/aef_terminal/ui/services/chart_stream_ws.py",
            ROOT / "src/aef_terminal/ui/services/chart_stream_coordinator.py",
            ROOT / "src/aef_terminal/ui/stream_payload.py",
        )
    )
    assert [name for name in obsolete if name in scoped_source] == []


def test_sync_quote_socket_and_parallel_cache_cannot_return() -> None:
    for name in (
        "live_quote",
        "live_quotes",
        "read_cached_quotes",
        "sync_quote_subscriptions",
        "_connected_quote_ib",
        "_reset_ibkr_quote_session",
        "_stale_quote_keys",
        "_pump_quote_session",
    ):
        assert not hasattr(ibkr_quotes, name)
        assert not hasattr(ibkr_session, name)
    runtime = ibkr_runtime._IBKR_RUNTIME
    for name in ("quote_session", "ticker_cache", "quote_contract_cache", "quote_lock"):
        assert not hasattr(runtime, name)
    cached_source = inspect.getsource(ibkr_quotes.cached_quotes)
    assert "async_ticker_cache" not in cached_source
    assert "read_quote_snapshots" in cached_source
    status_source = inspect.getsource(ibkr_quotes.runtime_status)
    assert "_quote_from_ticker" not in status_source
    assert "async_quote_ticker_routes" not in status_source
    assert "async_ticker_cache" not in status_source
    assert "runtime.quote_snapshots" in status_source
    assert "runtime.quote_snapshot_routes" in status_source


def test_gex_router_dispatches_through_provider_adapter() -> None:
    deps = inspect.signature(gex_router.GexRouterDeps).parameters
    assert "load_gex_context" not in deps
    assert "load_live_gex_context" not in deps
    assert "stop_live_gex_context" not in deps
    source = inspect.getsource(gex_router)
    assert "route.adapter.async_load_gex" in source
    assert "route.adapter.async_load_live_gex" not in source
    assert "route.adapter.async_load_live_gex" in inspect.getsource(gex_stream_ws)
    assert "route.adapter.option_target_price" in source
    assert "option_quote_provider" not in source
    assert "option_quote_provider" not in inspect.getsource(option_target_reprice)
    assert "normalize_gex_asset" not in source


def test_gex_calculation_modules_do_not_parse_raw_ibkr_option_contracts() -> None:
    modules = (
        ROOT / "src/aef_terminal/data/gex/live.py",
        ROOT / "src/aef_terminal/data/gex/live_analysis.py",
        ROOT / "src/aef_terminal/data/gex/math.py",
        ROOT / "src/aef_terminal/data/gex/payload.py",
        ROOT / "src/aef_terminal/data/gex/payload_contract.py",
        ROOT / "src/aef_terminal/data/gex/quality.py",
    )
    raw_option_fields = ("right", "strike", "multiplier", "tradingClass", "aefExpiryAt")
    violations = []
    for path in modules:
        source = path.read_text(encoding="utf-8")
        for field in raw_option_fields:
            if f'getattr(contract, "{field}"' in source or f"getattr(contract, '{field}'" in source:
                violations.append(f"{path.relative_to(ROOT)}:{field}")
    assert violations == []


def test_gex_ibkr_transport_has_one_provider_owned_path() -> None:
    provider_transport = ROOT / "src/aef_terminal/data/ibkr/gex.py"
    provider_market = ROOT / "src/aef_terminal/data/ibkr/gex_market.py"
    provider_cancel = ROOT / "src/aef_terminal/data/ibkr/utils.py"
    assert provider_transport.exists()
    assert provider_market.exists()
    assert provider_cancel.exists()
    assert not (ROOT / "src/aef_terminal/data/gex/fetch.py").exists()
    assert not (ROOT / "src/aef_terminal/data/gex/option_market.py").exists()
    assert not (ROOT / "src/aef_terminal/data/gex/patchable.py").exists()

    broker_calls = (
        "qualifyContracts",
        "reqSecDefOptParams",
        "reqContractDetails",
        "reqMktData",
        "reqMarketDataType",
    )
    provider_source = provider_transport.read_text(encoding="utf-8")
    assert all(call in provider_source for call in broker_calls)
    assert "_cancel_ibkr_market_data" in provider_source
    assert "cancelMktData" in provider_cancel.read_text(encoding="utf-8")

    common_modules = (
        ROOT / "src/aef_terminal/data/gex/context.py",
        ROOT / "src/aef_terminal/data/gex/live.py",
        ROOT / "src/aef_terminal/data/gex/session.py",
        ROOT / "src/aef_terminal/data/gex/scheduler.py",
    )
    violations = [
        f"{path.relative_to(ROOT)}:{call}"
        for path in common_modules
        for call in (*broker_calls, "cancelMktData")
        if call in path.read_text(encoding="utf-8")
    ]
    assert violations == []

    private_boundary_violations: list[str] = []
    for path in (ROOT / "src/aef_terminal/data/gex").glob("*.py"):
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("aef_terminal.data.ibkr"):
                        private_boundary_violations.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}:provider-module-import"
                        )
            elif (
                isinstance(node, ast.ImportFrom)
                and isinstance(node.module, str)
                and node.module.startswith("aef_terminal.data.ibkr")
            ):
                private_boundary_violations.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}:provider-module-import"
                )
    assert private_boundary_violations == []

    context_source = common_modules[0].read_text(encoding="utf-8")
    live_source = common_modules[1].read_text(encoding="utf-8")
    runtime_source = common_modules[2].read_text(encoding="utf-8")
    provider_runtime_contract = (ROOT / "src/aef_terminal/data/gex/provider_runtime.py").read_text(
        encoding="utf-8"
    )
    ibkr_runtime_adapter = (ROOT / "src/aef_terminal/data/ibkr/gex_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "provider_runtime.acquire(" in context_source
    assert "fetch_gex_option_rows(" not in context_source
    assert "def acquire(" in provider_runtime_contract
    assert "ibkr_gex.fetch_gex_option_rows(" in ibkr_runtime_adapter
    assert "IbkrOptionContract" not in live_source
    assert "_option_row_from_ticker" not in live_source
    assert "ib: Any" not in live_source
    assert "tickers:" not in live_source
    assert "session: Any" not in runtime_source
    assert "session_key:" not in runtime_source
    for public_name in (
        "fetch_gex_option_rows",
        "gex_request_transport_status",
        "open_live_gex_subscription",
        "sample_live_gex_subscription",
        "live_gex_subscription_health",
        "close_live_gex_subscription",
    ):
        assert (
            f"def {public_name}(" in provider_source
            or f"async def {public_name}(" in provider_source
        )


def test_provider_session_queries_require_instrument_identity() -> None:
    for function in (
        sessions.provider_schedule_open_state,
        sessions.provider_schedule_open_interval,
        sessions.next_provider_schedule_open,
        sessions.expected_market_closed_notice,
    ):
        parameter = inspect.signature(function).parameters["instrument"]
        assert parameter.default is inspect.Parameter.empty


def test_opaque_identity_tokens_are_not_consumer_normalized() -> None:
    import ast
    import re
    from pathlib import Path

    opaque_names = {
        "instrument_id",
        "instrumentid",
        "route_fingerprint",
        "routefingerprint",
    }
    python_violations: list[str] = []
    python_root = Path("src/aef_terminal")
    canonical_identity_owner = python_root / "data/instrument_identity.py"
    for path in python_root.rglob("*.py"):
        if path == canonical_identity_owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            silent_string_cast = isinstance(function, ast.Name) and function.id == "str"
            consumer_normalization = isinstance(function, ast.Attribute) and function.attr in {
                "strip",
                "split",
            }
            if not silent_string_cast and not consumer_normalization:
                continue
            if silent_string_cast:
                if not node.args:
                    continue
                argument = node.args[0]
                candidates = (
                    list(argument.values) if isinstance(argument, ast.BoolOp) else [argument]
                )
                candidates = [
                    candidate
                    for candidate in candidates
                    if not (
                        isinstance(candidate, ast.Call)
                        and isinstance(candidate.func, ast.Attribute)
                        and candidate.func.attr == "dumps"
                    )
                ]
            else:
                candidates = [function.value]
            references: set[str] = set()
            for candidate in candidates:
                references.update(
                    child.id.lower() for child in ast.walk(candidate) if isinstance(child, ast.Name)
                )
                references.update(
                    child.attr.lower()
                    for child in ast.walk(candidate)
                    if isinstance(child, ast.Attribute)
                )
                references.update(
                    str(child.value).lower()
                    for child in ast.walk(candidate)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)
                )
            if references.intersection(opaque_names):
                python_violations.append(f"{path}:{node.lineno}")

    javascript_patterns = (
        re.compile(
            r"\bString\([^;\n]*(?:instrumentId|instrument_id|routeFingerprint|route_fingerprint)"
        ),
        re.compile(
            r"(?:instrumentId|instrument_id|routeFingerprint|route_fingerprint)\s*\.\s*(?:trim|split)\s*\("
        ),
    )
    javascript_violations: list[str] = []
    for path in _browser_javascript_files():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(pattern.search(line) for pattern in javascript_patterns):
                javascript_violations.append(f"{path}:{line_number}")

    assert not python_violations
    assert not javascript_violations


def test_frontend_does_not_round_or_infer_provider_time_or_sessions() -> None:
    import re

    rounding = re.compile(
        r"Math\.(?:round|floor|ceil)\([^;\n]*(?:provider_ts|timestamp|\bts\b|session)"
        r"|(?:provider_ts|timestamp|\bts\b|session)[^;\n]*Math\.(?:round|floor|ceil)\("
    )
    inference = re.compile(r"infer[A-Za-z0-9_]*(?:ProviderTimestamp|TradingSession|SessionScope)")
    violations: list[str] = []
    for path in _browser_javascript_files():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if rounding.search(line) or inference.search(line):
                violations.append(f"{path}:{line_number}")

    assert not violations


def test_shared_routers_do_not_branch_on_provider_names() -> None:
    import ast
    from pathlib import Path

    provider_names = {"ibkr", "coinbase", "tinvest"}
    violations: list[str] = []
    for path in Path("src/aef_terminal/ui/routers").glob("*.py"):
        if path.stem in provider_names:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            constants = {
                str(child.value).strip().lower()
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            if not constants.intersection(provider_names):
                continue
            references = {
                child.id.lower() for child in ast.walk(node) if isinstance(child, ast.Name)
            }
            references.update(
                child.attr.lower() for child in ast.walk(node) if isinstance(child, ast.Attribute)
            )
            if references.intersection({"provider", "source", "data_source"}):
                violations.append(f"{path}:{node.lineno}")

    assert not violations


def test_execution_consumers_do_not_parse_display_text() -> None:
    import ast
    from pathlib import Path

    display_fields = {"tooltip", "message", "reason"}
    parser_methods = {"split", "startswith", "endswith", "match", "search", "fullmatch"}
    violations: list[str] = []
    roots = (
        Path("src/aef_terminal/engine"),
        Path("src/aef_terminal/signals"),
        Path("src/aef_terminal/ui/paper"),
    )
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                method = function.attr if isinstance(function, ast.Attribute) else ""
                if method not in parser_methods:
                    continue
                references = {
                    child.id.lower() for child in ast.walk(node) if isinstance(child, ast.Name)
                }
                references.update(
                    child.attr.lower()
                    for child in ast.walk(node)
                    if isinstance(child, ast.Attribute)
                )
                references.update(
                    str(child.value).lower()
                    for child in ast.walk(node)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)
                )
                if references.intersection(display_fields):
                    violations.append(f"{path}:{node.lineno}")

    assert not violations


def test_paper_journal_runtime_has_one_domain_owner() -> None:
    from pathlib import Path

    paper_source = Path("src/aef_terminal/ui/assets/js/18-paper-trading.js").read_text(
        encoding="utf-8"
    )
    panel_source = "".join(
        Path(f"src/aef_terminal/ui/assets/js/{name}").read_text(encoding="utf-8")
        for name in (
            "30-watchlist-runtime.js",
            "30-market-panel-runtime.js",
            "30-trade-panels-runtime.js",
            "30-alert-manager-runtime.js",
        )
    )
    functions = (
        "function paperJournalUrl(",
        "function openPaperJournalWindow(",
        "function downloadPaperJournalCsv(",
        "async function loadPaperStats(",
    )

    for signature in functions:
        assert signature in paper_source
        assert signature not in panel_source

    runtime_source = (ROOT / "src/aef_terminal/paper_journal_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "asyncio.to_thread(" not in runtime_source
    assert "run_physical_thread_call(" in runtime_source
    assert "self._refresh_lock" in runtime_source
