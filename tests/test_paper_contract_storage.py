from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from aef_terminal.paper_contract import (
    PAPER_CONTRACT_KEY,
    PaperContractIdentity,
    paper_instrument_contract,
    require_paper_contract_identity,
)
from aef_terminal.storage.repos import schema as schema_repo
from aef_terminal.storage.repos.paper import (
    PaperRepoMixin,
    _paper_order_command_payload,
)
from aef_terminal.ui.paper.positions import paper_journal_payload


def _option_contract(**overrides: object) -> PaperContractIdentity:
    values: dict[str, object] = {
        "scope_kind": "option",
        "provider": "ibkr",
        "provider_contract_id": "71001",
        "sec_type": "OPT",
        "con_id": 71001,
        "exchange": "SMART",
        "expiry": "20260918",
        "expiry_at": "2026-09-18T20:00:00+00:00",
        "strike": 600.0,
        "right": "C",
        "trading_class": "SPY",
        "multiplier": 100.0,
        "currency": "USD",
    }
    values.update(overrides)
    return PaperContractIdentity(**values)  # type: ignore[arg-type]


def _order(contract: PaperContractIdentity | None = None) -> dict[str, object]:
    order: dict[str, object] = {
        "id": "paper-option-order",
        "instrument_id": "ibkr|contract|underlying",
        "route_fingerprint": "ibkr|route|underlying",
        "symbol": "SPY",
        "provider": "ibkr",
        "provider_contract_id": "underlying" if contract is None else contract.provider_contract_id,
        "timeframe": "5m",
        "side": "long",
        "order_type": "limit",
        "qty": 1.0,
        "entry": 4.5,
        "stop_loss": 3.5,
        "target": 6.5,
        "use_stop_loss": True,
        "use_target": True,
        "role": "entry",
        "reduce_only": False,
        "payload": {"protection_basis": "absolute_structure"},
    }
    if contract is not None:
        order[PAPER_CONTRACT_KEY] = contract.to_payload()
    return order


def test_paper_command_owner_requires_explicit_contract_and_accepts_exact_scopes() -> None:
    with pytest.raises(ValueError, match="PAPER_CONTRACT_PAYLOAD_REQUIRED"):
        _paper_order_command_payload(_order())

    instrument_contract = paper_instrument_contract("ibkr", "underlying")
    explicit_order = _order()
    explicit_order[PAPER_CONTRACT_KEY] = instrument_contract.to_payload()
    legacy = _paper_order_command_payload(explicit_order)
    assert legacy[PAPER_CONTRACT_KEY]["scope_kind"] == "instrument"

    option = _option_contract()
    command = _paper_order_command_payload(_order(option))
    assert command[PAPER_CONTRACT_KEY] == option.to_payload()
    assert require_paper_contract_identity(command) == option
    assert command[PAPER_CONTRACT_KEY]["expiry_at"] == "2026-09-18T20:00:00+00:00"


def test_option_metadata_never_splits_one_con_id_or_merges_distinct_con_ids() -> None:
    call_600 = _option_contract()
    corrected_metadata = _option_contract(strike=605.0, right="P")
    other_contract = _option_contract(
        provider_contract_id="71002",
        con_id=71002,
        strike=605.0,
        right="P",
    )

    assert corrected_metadata.scope_key == call_600.scope_key
    assert other_contract.scope_key != call_600.scope_key
    with pytest.raises(ValueError, match="PAPER_OPTION_CON_ID_INVALID"):
        _option_contract(con_id=None)
    with pytest.raises(ValueError, match="PAPER_CONTRACT_PAYLOAD_REQUIRED"):
        require_paper_contract_identity(
            {
                "provider": "ibkr",
                "provider_contract_id": "71001",
                "strike": 600.0,
            }
        )


def test_exact_option_reads_and_fill_transition_use_contract_scope() -> None:
    contract = _option_contract()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = PaperRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]

    assert (
        repo.read_paper_positions(
            instrument_id="ibkr|contract|underlying",
            route_fingerprint="ibkr|route|underlying",
            paper_contract=contract,
        )
        == []
    )
    query, params = cursor.execute.call_args.args
    assert "contract_scope_key = %s" in query
    assert contract.scope_key in params

    source = inspect.getsource(PaperRepoMixin)
    assert 'f"{fill.contract_scope_key}:{fill.timeframe}"' in source
    assert source.count("AND contract_scope_key = %s") >= 10
    assert "paper_orders.contract_scope_key = EXCLUDED.contract_scope_key" in source
    assert "PAPER_POSITION_CONTRACT_METADATA_CONTRADICTORY" in source


def test_option_journal_reads_all_and_only_option_contract_scopes() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Store:
        @staticmethod
        def read_paper_positions(**kwargs):
            calls.append(("positions", dict(kwargs)))
            return []

        @staticmethod
        def read_paper_orders(**kwargs):
            calls.append(("orders", dict(kwargs)))
            return []

        @staticmethod
        def read_paper_fills(**kwargs):
            calls.append(("fills", dict(kwargs)))
            return []

    class Deps:
        paper_trade_enriched = staticmethod(lambda trade: trade)
        paper_trade_stats = staticmethod(lambda _trades: {})
        paper_replay_summary = staticmethod(lambda _trades: {})

    payload = paper_journal_payload(
        Store(),
        Deps(),
        instrument_id="ibkr|contract|underlying",
        route_fingerprint="ibkr|route|underlying",
        paper_scope_kind="option",
    )

    assert payload is not None
    assert payload["paper_scope_kind"] == "option"
    scoped_calls = [kwargs for _name, kwargs in calls if kwargs.get("instrument_id") is not None]
    assert scoped_calls
    assert all(kwargs["contract_scope_kind"] == "option" for kwargs in scoped_calls)


def test_option_scope_selector_is_distinct_from_one_exact_contract() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = PaperRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]

    assert (
        repo.read_paper_positions(
            instrument_id="ibkr|contract|underlying",
            route_fingerprint="ibkr|route|underlying",
            contract_scope_kind="option",
        )
        == []
    )
    query, params = cursor.execute.call_args.args
    assert "contract_scope_kind = %s" in query
    assert "option" in params
    with pytest.raises(ValueError, match="PAPER_CONTRACT_SCOPE_QUERY_AMBIGUOUS"):
        repo.read_paper_positions(
            instrument_id="ibkr|contract|underlying",
            route_fingerprint="ibkr|route|underlying",
            paper_contract=_option_contract(),
            contract_scope_kind="option",
        )


def test_v18_schema_physically_indexes_paper_contract_scope() -> None:
    source = inspect.getsource(schema_repo.SchemaRepoMixin.initialize)
    verifier = inspect.getsource(schema_repo._verify_paper_contract_storage)
    for table in ("paper_orders", "paper_positions", "paper_fills"):
        body = source.split(f"CREATE TABLE {table}", 1)[1]
        assert "contract_scope_key text GENERATED ALWAYS" in body
        assert f"{table}_contract_scope_chk" in body
        assert "contract_provider_contract_id text GENERATED ALWAYS" in body
        assert "contract_con_id bigint GENERATED ALWAYS" in body
        assert "contract_exchange text GENERATED ALWAYS" in body
    assert "contract_scope_key, position_id, role" in source
    assert "contract_scope_key, order_id" in source
    assert "pg_get_expr(generated_value.adbin, generated_value.adrelid)" in verifier
    assert "'scope_key'::text" in verifier
    assert "'provider_contract_id'::text" in verifier


class _PaperSchemaCursor:
    def __init__(self, *, drift_scope_expression: bool = False) -> None:
        self.statement = ""
        self.params: tuple[object, ...] = ()
        self.drift_scope_expression = drift_scope_expression

    def execute(self, statement, params=()) -> None:
        self.statement = str(statement)
        self.params = tuple(params)

    def fetchall(self):
        if "FROM pg_attribute AS attribute" in self.statement:
            rows = [
                (
                    "contract_scope_key",
                    "text",
                    True,
                    "s",
                    "((payload -> 'paper_contract'::text) ->> 'scope_key'::text)",
                ),
                (
                    "contract_scope_kind",
                    "text",
                    True,
                    "s",
                    "((payload -> 'paper_contract'::text) ->> 'scope_kind'::text)",
                ),
                (
                    "contract_provider",
                    "text",
                    True,
                    "s",
                    "((payload -> 'paper_contract'::text) ->> 'provider'::text)",
                ),
                (
                    "contract_sec_type",
                    "text",
                    False,
                    "s",
                    "((payload -> 'paper_contract'::text) ->> 'sec_type'::text)",
                ),
                (
                    "contract_provider_contract_id",
                    "text",
                    True,
                    "s",
                    "((payload -> 'paper_contract'::text) ->> 'provider_contract_id'::text)",
                ),
                (
                    "contract_con_id",
                    "bigint",
                    False,
                    "s",
                    "(((payload -> 'paper_contract'::text) ->> 'con_id'::text))::bigint",
                ),
                (
                    "contract_exchange",
                    "text",
                    False,
                    "s",
                    "((payload -> 'paper_contract'::text) ->> 'exchange'::text)",
                ),
            ]
            if self.drift_scope_expression:
                rows[0] = (*rows[0][:-1], "(payload ->> 'provider_contract_id'::text)")
            return rows
        return []

    def fetchone(self):
        constraint_name = str(self.params[1])
        if constraint_name.endswith("_contract_scope_chk"):
            return (True, schema_repo._PAPER_CONTRACT_SCOPE_CHECK_DEFINITION)
        return (True, schema_repo._PAPER_CONTRACT_PROVIDER_CHECK_DEFINITION)


def test_paper_contract_verifier_rejects_generated_scope_expression_drift() -> None:
    schema_repo._verify_paper_contract_storage(_PaperSchemaCursor())

    with pytest.raises(RuntimeError, match="PAPER_CONTRACT_STORAGE_COLUMNS_REQUIRED"):
        schema_repo._verify_paper_contract_storage(_PaperSchemaCursor(drift_scope_expression=True))


def test_option_contract_normalizes_aware_expiry_at_to_utc() -> None:
    contract = _option_contract(
        expiry_at=datetime(2026, 9, 18, 16, 0, tzinfo=UTC),
    )
    assert contract.expiry_at == "2026-09-18T16:00:00+00:00"
