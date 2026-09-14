from __future__ import annotations

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.domain import Bar, BarProvenance
from aef_terminal.paper_contract import PAPER_EXECUTION_CONFIG_KEY
from aef_terminal.runtime.bar_quality import is_provisional_bar
from aef_terminal.storage.repos.bars import BarsRepoMixin
from aef_terminal.storage.repos.bars import _storage_bar_reject_reason
from aef_terminal.storage.repos import futures as futures_repo
from aef_terminal.storage.session_intervals import (
    normalize_trading_session_intervals,
    trading_session_interval_replacement_horizons,
)
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore
from aef_terminal.storage.repos.connection import PostgresConnectionMixin
from aef_terminal.storage.repos.drawings import DrawingsRepoMixin
from aef_terminal.storage.repos.gex import GexRepoMixin
from aef_terminal.storage.repos.paper import (
    PaperRepoMixin,
    _paper_position_complete_ids_filter,
    _paper_position_read_query,
)
from tests.provider_payloads import ibkr_future_payload


def _provider_schedule_payload(*intervals: dict[str, object]) -> dict[str, object]:
    starts = [str(item["opens_at"]) for item in intervals]
    ends = [str(item["closes_at"]) for item in intervals]
    return {
        "schedule_format": "provider_declared_intervals",
        "schedule_coverage_start": min(starts),
        "schedule_coverage_end": max(ends),
        "trading_intervals": [dict(item) for item in intervals],
    }


def _initialize_source() -> str:
    return inspect.getsource(PostgresStore.initialize)


def test_price_alert_batch_reader_preserves_exact_route_pairs() -> None:
    cursor = MagicMock()
    alert = {
        "id": "alert-a",
        "instrument_id": "instrument-a",
        "route_fingerprint": "route-a",
        "symbol": "A",
        "timeframe": "5m",
        "provider": "ibkr",
        "provider_contract_id": "123",
    }
    cursor.fetchall.return_value = [("alert-a", "A", "5m", alert)]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = DrawingsRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]

    rows = repo.read_price_alerts(
        timeframe="5m",
        route_pairs=[
            ("instrument-b", "route-b"),
            ("instrument-a", "route-a"),
            ("instrument-b", "route-b"),
        ],
    )

    assert rows == [alert]
    query, parameters = cursor.execute.call_args.args
    assert "FROM unnest(%s::text[], %s::text[])" in query
    assert parameters == (
        ["instrument-b", "instrument-a"],
        ["route-b", "route-a"],
        "5m",
    )
    assert repo.read_price_alerts(timeframe="5m", route_pairs=[]) == []
    assert repo._connect.call_count == 1
    cursor.fetchall.return_value = [("alert-a", "A", "5m", "invalid")]
    with pytest.raises(ValueError, match="PRICE_ALERT_ROW_INVALID"):
        repo.read_price_alerts(
            timeframe="5m",
            route_pairs=[("instrument-a", "route-a")],
        )
    with pytest.raises(ValueError, match="either one exact route or route_pairs"):
        repo.read_price_alerts(
            "instrument-a",
            "5m",
            route_fingerprint="route-a",
            route_pairs=[("instrument-a", "route-a")],
        )


def test_price_alert_runtime_snapshot_reads_only_retained_rows() -> None:
    cursor = MagicMock()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = DrawingsRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]

    price_alert = {
        "id": "alert-a",
        "instrument_id": "instrument-a",
        "route_fingerprint": "route-a",
        "symbol": "A",
        "timeframe": "5m",
        "provider": "ibkr",
        "provider_contract_id": "123",
    }
    cursor.fetchall.return_value = [
        (
            "alert-a",
            "instrument-a",
            "route-a",
            "A",
            "5m",
            price_alert,
        )
    ]

    assert repo.read_all_price_alerts() == [price_alert]
    price_query = cursor.execute.call_args.args[0]
    assert "payload -> 'deleted'" not in price_query
    assert "payload -> 'enabled' = 'true'::jsonb" in price_query
    assert "payload -> 'armed' = 'true'::jsonb" in price_query
    assert "payload -> 'fired' = 'true'::jsonb" in price_query


def test_startup_alert_snapshot_rejects_inert_rows_without_repair() -> None:
    cursor = MagicMock()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = DrawingsRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]
    repo._psycopg = MagicMock()  # type: ignore[attr-defined,method-assign]
    repo._require_canonical_writer_lease = MagicMock()  # type: ignore[attr-defined,method-assign]
    repo._acquire_canonical_writer_fence_on_cursor = MagicMock()  # type: ignore[attr-defined,method-assign]

    def row(alert_id: str, *, enabled: bool, armed: bool, fired: bool):
        payload = {
            "id": alert_id,
            "instrument_id": "instrument-a",
            "route_fingerprint": "route-a",
            "symbol": "A",
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "123",
            "kind": "price",
            "label": "",
            "direction": "cross",
            "price": 100.0,
            "toleranceAtr": 0.08,
            "tolerancePoints": 0.0,
            "enabled": enabled,
            "armed": armed,
            "fired": fired,
            "cooldownUntil": 0,
            "rearmedAt": 1,
            "rearmMinutes": 60,
            "createdAt": 1,
            "level_source": {"type": "fixed_price", "dynamic": False},
            **TELEGRAM_CANCELLED_DELIVERY_STATE,
        }
        return (alert_id, "instrument-a", "route-a", "A", "5m", payload)

    cursor.fetchall.return_value = [
        row("armed", enabled=True, armed=True, fired=False),
        row("fired", enabled=True, armed=False, fired=True),
        row("disabled", enabled=False, armed=False, fired=False),
    ]

    with pytest.raises(ValueError, match="PRICE_ALERT_RUNTIME_INERT_ROW"):
        repo.read_price_alert_runtime_snapshot()

    delete_calls = [
        call for call in cursor.execute.call_args_list if "DELETE FROM price_alerts" in call.args[0]
    ]
    assert delete_calls == []


def test_generated_protective_order_preserves_immutable_execution_policy() -> None:
    policy = {"min_rr": 1.25, "edge_gate": True}
    intent = PaperRepoMixin()._paper_protective_child_intent(
        SimpleNamespace(
            order_payload={PAPER_EXECUTION_CONFIG_KEY: policy},
            instrument_id="instrument-a",
            route_fingerprint="route-a",
            provider="ibkr",
            provider_contract_id="123",
            paper_contract={"scope_key": "paper-contract-a"},
            order_id="entry-a",
            symbol="A",
            timeframe="5m",
            filled_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        ),
        SimpleNamespace(position_id="position-a"),
        child_role="stop",
        child_type="stop",
        child_entry=99.0,
        qty=1.0,
        exit_side="short",
        oco_group="oco-entry-a",
    )

    assert intent["payload"][PAPER_EXECUTION_CONFIG_KEY] == policy


def test_paper_fill_rejects_route_and_protective_order_id_collisions() -> None:
    paper_fill_source = "\n".join(
        inspect.getsource(owner)
        for owner in (
            PostgresStore._locked_paper_fill_position,
            PostgresStore._persist_paper_fill_records,
            PostgresStore._paper_fill_existing_order_outcome,
            PostgresStore._create_paper_fill_protective_orders,
        )
    )
    assert "WHERE instrument_id = %s AND route_fingerprint = %s" in paper_fill_source
    assert "paper_orders.instrument_id = EXCLUDED.instrument_id" in paper_fill_source
    assert "paper_orders.route_fingerprint = EXCLUDED.route_fingerprint" in paper_fill_source
    assert 'existing_order.get("instrument_id") != fill.instrument_id' in paper_fill_source
    assert 'existing_order.get("route_fingerprint") != fill.route_fingerprint' in paper_fill_source
    assert "PAPER_PROTECTIVE_ORDER_ID_COLLISION" in paper_fill_source
    assert "PAPER_ORDER_ID_ROUTE_COLLISION" in paper_fill_source


def test_gex_snapshot_reader_requires_exact_bounds_limit_and_projection() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = GexRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]
    start = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc)

    assert (
        repo.read_gex_snapshots(
            "instrument-a",
            "route-a",
            start,
            end,
            limit=7,
            source="gex:ibkr",
            include_raw=False,
        )
        == []
    )
    query, parameters = cursor.execute.call_args.args
    assert "jsonb_build_object(" in query
    assert "provider_symbol" in query
    assert parameters[-1] == 7
    valid_connect_count = repo._connect.call_count

    for invalid_limit in (True, False, 0, -1, 1.0, "7", None):
        with pytest.raises(ValueError, match="positive integer"):
            repo.read_gex_snapshots(
                "instrument-a",
                "route-a",
                start,
                end,
                limit=invalid_limit,  # type: ignore[arg-type]
                source="gex:ibkr",
            )
    with pytest.raises(TypeError, match="include_raw must be boolean"):
        repo.read_gex_snapshots(
            "instrument-a",
            "route-a",
            start,
            end,
            source="gex:ibkr",
            include_raw=1,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="bounds are inverted"):
        repo.read_gex_snapshots(
            "instrument-a",
            "route-a",
            end,
            start,
            source="gex:ibkr",
        )
    with pytest.raises(ValueError, match="sources must be unique"):
        repo.read_gex_snapshots(
            "instrument-a",
            "route-a",
            start,
            end,
            source=None,
            sources=("gex:ibkr", "gex:ibkr"),
        )
    assert repo._connect.call_count == valid_connect_count


def test_option_target_reader_rejects_untyped_filters_and_corrupt_rows() -> None:
    repo = GexRepoMixin()
    repo._connect = MagicMock()  # type: ignore[attr-defined,method-assign]
    with pytest.raises(TypeError, match="exact sequence"):
        repo.read_option_targets(route_pairs="instrument-a:route-a")  # type: ignore[arg-type]
    repo._connect.assert_not_called()

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "option-a",
            "ES",
            "5m",
            "not-an-object",
            datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 22, 10, 1, tzinfo=timezone.utc),
        )
    ]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]

    with pytest.raises(ValueError, match="payload must be an object"):
        repo.read_option_targets(route_pairs=[("instrument-a", "route-a")])


def test_raw_trading_hours_never_materialize_provider_intervals() -> None:
    with pytest.raises(ValueError, match="PROVIDER_SCHEDULE_FORMAT_INVALID"):
        normalize_trading_session_intervals(
            instrument_id="ibkr|contract|756733",
            route_fingerprint="ibkr|contract|756733",
            provider="ibkr",
            symbol="SPY",
            provider_contract_id="756733",
            payload={"con_id": 756733, "symbol": "SPY"},
            fetched_at=datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc),
        )


def test_trading_session_replacement_horizon_uses_exact_snapshot_bounds() -> None:
    new_rows = normalize_trading_session_intervals(
        instrument_id="ibkr|future_root|ES|CME|USD|",
        route_fingerprint="ibkr|future_root|ES|CME|USD||current:649180671",
        provider="ibkr",
        symbol="ES",
        provider_contract_id="649180671",
        payload=_provider_schedule_payload(
            {
                "session_date": "2026-07-15",
                "session_type": "trading",
                "opens_at": "2026-07-14T22:00:00+00:00",
                "closes_at": "2026-07-15T20:00:00+00:00",
                "status": "open",
            },
            {
                "session_date": "2026-07-16",
                "session_type": "trading",
                "opens_at": "2026-07-15T22:00:00+00:00",
                "closes_at": "2026-07-16T21:00:00+00:00",
                "status": "open",
            },
            {
                "session_date": "2026-07-17",
                "session_type": "trading",
                "opens_at": "2026-07-16T22:00:00+00:00",
                "closes_at": "2026-07-17T21:00:00+00:00",
                "status": "open",
            },
        ),
        fetched_at=datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc),
    )

    replacements = trading_session_interval_replacement_horizons(new_rows)

    assert len(replacements) == 1
    assert replacements[0].session_type == "trading"
    assert replacements[0].starts_at == new_rows[0]["opens_at"]
    assert replacements[0].ends_at == new_rows[-1]["closes_at"]
    assert replacements[0].rows == tuple(new_rows)


def test_trading_session_replacement_groups_by_exact_instrument_route_pair() -> None:
    opens_at = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    closes_at = datetime(2026, 7, 19, 16, 0, tzinfo=timezone.utc)
    replacements = trading_session_interval_replacement_horizons(
        [
            {
                "instrument_id": "instrument-a",
                "route_fingerprint": "shared-generation-token",
                "session_type": "trading",
                "opens_at": opens_at,
                "closes_at": closes_at,
            },
            {
                "instrument_id": "instrument-b",
                "route_fingerprint": "shared-generation-token",
                "session_type": "trading",
                "opens_at": opens_at,
                "closes_at": closes_at,
            },
        ]
    )

    assert [replacement.instrument_id for replacement in replacements] == [
        "instrument-a",
        "instrument-b",
    ]


def test_provider_schedule_without_exact_coverage_fails_closed() -> None:
    with pytest.raises(ValueError, match="PROVIDER_SCHEDULE_COVERAGE_START_INVALID"):
        normalize_trading_session_intervals(
            instrument_id="ibkr|contract|756733",
            route_fingerprint="ibkr|contract|756733",
            provider="ibkr",
            symbol="SPY",
            provider_contract_id="756733",
            payload={
                "schedule_format": "provider_declared_intervals",
                "trading_intervals": [],
            },
            fetched_at=datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("second_interval", "error_code"),
    [
        (
            {
                "session_date": "2026-07-17",
                "session_type": "trading",
                "opens_at": "2026-07-17T13:30:00+00:00",
                "closes_at": "2026-07-17T20:00:00+00:00",
                "status": "closed",
            },
            "PROVIDER_SCHEDULE_INTERVAL_DUPLICATE",
        ),
        (
            {
                "session_date": "2026-07-17",
                "session_type": "trading",
                "opens_at": "2026-07-17T19:59:00+00:00",
                "closes_at": "2026-07-17T21:00:00+00:00",
                "status": "closed",
            },
            "PROVIDER_SCHEDULE_INTERVAL_OVERLAP",
        ),
    ],
)
def test_provider_schedule_normalization_rejects_duplicate_or_overlapping_scope(
    second_interval,
    error_code,
) -> None:
    first_interval = {
        "session_date": "2026-07-17",
        "session_type": "trading",
        "opens_at": "2026-07-17T13:30:00+00:00",
        "closes_at": "2026-07-17T20:00:00+00:00",
        "status": "open",
    }
    with pytest.raises(ValueError, match=error_code):
        normalize_trading_session_intervals(
            instrument_id="ibkr|contract|756733",
            route_fingerprint="ibkr|contract|756733",
            provider="ibkr",
            symbol="SPY",
            provider_contract_id="756733",
            payload={
                "schedule_format": "provider_declared_intervals",
                "schedule_coverage_start": "2026-07-17T13:30:00+00:00",
                "schedule_coverage_end": "2026-07-17T21:00:00+00:00",
                "trading_intervals": [first_interval, second_interval],
            },
            fetched_at=datetime(2026, 7, 17, 21, 1, tzinfo=timezone.utc),
        )


def test_provider_schedule_normalization_allows_distinct_overlapping_scopes() -> None:
    rows = normalize_trading_session_intervals(
        instrument_id="ibkr|contract|756733",
        route_fingerprint="ibkr|contract|756733",
        provider="ibkr",
        symbol="SPY",
        provider_contract_id="756733",
        payload={
            "schedule_format": "provider_declared_intervals",
            "schedule_coverage_start": "2026-07-17T13:30:00+00:00",
            "schedule_coverage_end": "2026-07-17T21:00:00+00:00",
            "trading_intervals": [
                {
                    "session_date": "2026-07-17",
                    "session_type": "trading",
                    "opens_at": "2026-07-17T13:30:00+00:00",
                    "closes_at": "2026-07-17T21:00:00+00:00",
                    "status": "open",
                },
                {
                    "session_date": "2026-07-17",
                    "session_type": "liquid",
                    "opens_at": "2026-07-17T13:30:00+00:00",
                    "closes_at": "2026-07-17T20:00:00+00:00",
                    "status": "open",
                },
            ],
        },
        fetched_at=datetime(2026, 7, 17, 21, 1, tzinfo=timezone.utc),
    )

    assert {row["session_type"] for row in rows} == {"trading", "liquid"}


def test_provider_owned_utc_schedule_is_normalized_without_calendar_inference() -> None:
    rows = normalize_trading_session_intervals(
        instrument_id="tinvest|future_root|MX|MOEX|RUB|",
        route_fingerprint="tinvest|future_root|MX|MOEX|RUB||current:FUTMXU6",
        provider="tinvest",
        symbol="MX",
        provider_contract_id="FUTMXU6",
        payload={
            "provider_contract_id": "FUTMXU6",
            "instrument_key": "MX",
            "schedule_format": "provider_declared_intervals",
            "schedule_coverage_start": "2026-07-13T06:00:00+00:00",
            "schedule_coverage_end": "2026-07-13T20:50:00+00:00",
            "trading_intervals": [
                {
                    "session_date": "2026-07-13",
                    "session_type": "trading",
                    "opens_at": "2026-07-13T06:00:00+00:00",
                    "closes_at": "2026-07-13T20:50:00+00:00",
                    "status": "open",
                    "metadata": {"provider_session_type": "main_session"},
                }
            ],
        },
        fetched_at=datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0]["instrument_id"] == "tinvest|future_root|MX|MOEX|RUB|"
    assert rows[0]["route_fingerprint"] == "tinvest|future_root|MX|MOEX|RUB||current:FUTMXU6"
    assert rows[0]["provider_contract_id"] == "FUTMXU6"
    assert rows[0]["opens_at"].isoformat() == "2026-07-13T06:00:00+00:00"
    assert rows[0]["closes_at"].isoformat() == "2026-07-13T20:50:00+00:00"
    assert rows[0]["metadata"] == {"provider_session_type": "main_session"}


def test_typed_provider_schedule_never_fills_a_missing_scope_from_raw_text() -> None:
    rows = normalize_trading_session_intervals(
        instrument_id="ibkr|future_root|ES|CME|USD|ES",
        route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:649180671",
        provider="ibkr",
        symbol="ES",
        provider_contract_id="649180671",
        payload={
            "provider_contract_id": "649180671",
            "schedule_format": "provider_declared_intervals",
            "schedule_coverage_start": "2026-07-27T22:00:00+00:00",
            "schedule_coverage_end": "2026-07-28T21:00:00+00:00",
            "trading_intervals": [
                {
                    "session_date": "2026-07-28",
                    "session_type": "trading",
                    "opens_at": "2026-07-27T22:00:00+00:00",
                    "closes_at": "2026-07-28T21:00:00+00:00",
                    "status": "open",
                    "metadata": {
                        "provider_source": "reqHistoricalSchedule",
                        "use_rth": False,
                    },
                }
            ],
        },
        fetched_at=datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0]["session_type"] == "trading"
    assert rows[0]["opens_at"].isoformat() == "2026-07-27T22:00:00+00:00"
    assert rows[0]["metadata"] == {
        "provider_source": "reqHistoricalSchedule",
        "use_rth": False,
    }


def test_provider_owned_liquid_schedule_keeps_its_typed_scope() -> None:
    rows = normalize_trading_session_intervals(
        instrument_id="ibkr|contract|416904",
        route_fingerprint="ibkr|contract|416904",
        provider="ibkr",
        symbol="SPX",
        provider_contract_id="416904",
        payload={
            "provider_contract_id": "416904",
            "schedule_format": "provider_declared_intervals",
            "schedule_coverage_start": "2026-07-20T13:30:00+00:00",
            "schedule_coverage_end": "2026-07-20T20:00:00+00:00",
            "trading_intervals": [
                {
                    "session_date": "2026-07-20",
                    "session_type": "liquid",
                    "opens_at": "2026-07-20T13:30:00+00:00",
                    "closes_at": "2026-07-20T20:00:00+00:00",
                    "status": "open",
                    "metadata": {"use_rth": True},
                }
            ],
        },
        fetched_at=datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc),
    )

    assert len(rows) == 1
    assert rows[0]["session_type"] == "liquid"
    assert rows[0]["metadata"] == {"use_rth": True}


def test_provider_owned_schedule_without_exact_session_date_fails_closed() -> None:
    with pytest.raises(ValueError, match="PROVIDER_SCHEDULE_SESSION_DATE_REQUIRED"):
        normalize_trading_session_intervals(
            instrument_id="tinvest|future_root|MX|MOEX|RUB|",
            route_fingerprint="tinvest|future_root|MX|MOEX|RUB||current:FUTMXU6",
            provider="tinvest",
            symbol="MX",
            provider_contract_id="FUTMXU6",
            payload={
                "provider_contract_id": "FUTMXU6",
                "schedule_format": "provider_declared_intervals",
                "schedule_coverage_start": "2026-07-13T06:00:00+00:00",
                "schedule_coverage_end": "2026-07-13T20:50:00+00:00",
                "trading_intervals": [
                    {
                        "session_type": "trading",
                        "opens_at": "2026-07-13T06:00:00+00:00",
                        "closes_at": "2026-07-13T20:50:00+00:00",
                        "status": "open",
                    }
                ],
            },
            fetched_at=datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc),
        )


def test_provider_owned_schedule_rejects_timezone_free_intervals() -> None:
    with pytest.raises(ValueError, match="PROVIDER_SCHEDULE_OPENS_AT_TIMEZONE_REQUIRED"):
        normalize_trading_session_intervals(
            instrument_id="tinvest|future_root|MX|MOEX|RUB|",
            route_fingerprint="tinvest|future_root|MX|MOEX|RUB||current:FUTMXU6",
            provider="tinvest",
            symbol="MX",
            provider_contract_id="FUTMXU6",
            payload={
                "provider_contract_id": "FUTMXU6",
                "schedule_format": "provider_declared_intervals",
                "schedule_coverage_start": "2026-07-13T06:00:00+00:00",
                "schedule_coverage_end": "2026-07-13T20:50:00+00:00",
                "trading_intervals": [
                    {
                        "session_date": "2026-07-13",
                        "session_type": "trading",
                        "opens_at": "2026-07-13T09:00:00",
                        "closes_at": "2026-07-13T19:00:00",
                        "status": "open",
                    }
                ],
            },
            fetched_at=datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc),
        )


def test_paper_journal_queries_have_status_and_activity_indexes() -> None:
    source = _initialize_source()
    orders_source = inspect.getsource(PostgresStore.read_paper_orders)
    order_source = inspect.getsource(PostgresStore.read_paper_order)
    order_write_source = inspect.getsource(PostgresStore.upsert_paper_order)
    order_update_source = inspect.getsource(PostgresStore.update_paper_order_if_pending)
    positions_source = "\n".join(
        (
            inspect.getsource(PostgresStore.read_paper_positions),
            inspect.getsource(_paper_position_read_query),
            inspect.getsource(_paper_position_complete_ids_filter),
        )
    )
    fills_source = inspect.getsource(PostgresStore.read_paper_fills)
    event_read_source = inspect.getsource(PostgresStore.read_paper_trade_events)
    order_cancel_source = inspect.getsource(PostgresStore.cancel_paper_order_if_pending)
    order_cancel_cursor_source = inspect.getsource(
        PostgresStore._cancel_paper_order_if_pending_with_cursor
    )
    protective_source = inspect.getsource(PostgresStore.update_paper_protective_levels)
    cancel_source = inspect.getsource(PostgresStore.cancel_paper_protective_orders)

    assert "paper_orders_status_updated_idx" in source
    assert "ON paper_orders (status, updated_at DESC)" in source
    assert "paper_orders_activity_idx" in source
    assert (
        "ON paper_orders ((COALESCE(filled_at, updated_at, created_at)) DESC, updated_at DESC)"
        in source
    )
    assert "paper_orders_position_pending_idx" in source
    assert "ON paper_orders (contract_scope_key, position_id, role, updated_at DESC)" in source
    assert "WHERE status = 'pending'" in source
    assert "paper_fills_filled_at_idx" in source
    assert "ON paper_fills (filled_at DESC)" in source
    assert "paper_fills_identity_time_idx" in source
    assert "instrument_id, route_fingerprint, contract_scope_key," in source
    assert "filled_at DESC" in source
    assert "paper_fills_order_unique_idx" in source
    assert "ON paper_fills (contract_scope_key, order_id)" in source
    assert "paper_trade_events_identity_created_idx" in source
    assert "instrument_id, route_fingerprint, created_at DESC" in source
    assert "instrument_id = %s" in event_read_source
    assert "route_fingerprint = %s" in event_read_source
    assert "trade_id = ANY(%s)" in event_read_source
    assert "PAPER_EVENT_TRADE_IDS_REQUIRE_EXACT_ROUTE" in event_read_source

    assert "status = %s" in orders_source
    assert "instrument_id = %s" in orders_source
    assert "route_fingerprint = %s" in orders_source
    assert "paper order query requires a complete route identity pair" in orders_source
    assert "complete_pending" in orders_source
    assert "PAPER_ORDER_COMPLETE_QUERY_REQUIRES_PENDING_STATUS" in orders_source
    assert "COALESCE(filled_at, updated_at, created_at) >= %s" in orders_source
    assert "ORDER BY updated_at DESC" in orders_source
    for exact_order_source in (order_source, order_update_source):
        assert "WHERE id = %s" in exact_order_source
        assert "instrument_id = %s" in exact_order_source
        assert "route_fingerprint = %s" in exact_order_source
    assert "PAPER_ORDER_ID_ROUTE_COLLISION" in order_write_source
    assert "ON CONFLICT (id) DO NOTHING" in order_write_source
    assert "PAPER_ORDER_ID_COMMAND_COLLISION" in order_write_source
    assert "complete_position_ids" in positions_source
    assert "id = ANY(%s)" in positions_source
    assert 'limit_clause = ""' in positions_source
    assert "instrument_id = %s" in fills_source
    assert "route_fingerprint = %s" in fills_source
    assert "filled_at >= %s" in fills_source
    assert "ORDER BY filled_at DESC" in fills_source
    assert "instrument_id=identity" in order_cancel_source
    assert "route_fingerprint=fingerprint" in order_cancel_source
    assert "WHERE id = %s" in order_cancel_cursor_source
    assert "AND instrument_id = %s" in order_cancel_cursor_source
    assert "AND route_fingerprint = %s" in order_cancel_cursor_source
    assert "_append_paper_trade_event_with_cursor" in order_update_source
    assert order_update_source.index("UPDATE paper_orders") < order_update_source.index(
        "_append_paper_trade_event_with_cursor"
    )
    assert "_append_paper_trade_event_with_cursor" in order_cancel_source
    assert order_cancel_source.index(
        "_cancel_paper_order_if_pending_with_cursor"
    ) < order_cancel_source.index("_append_paper_trade_event_with_cursor")
    assert "instrument_id, route_fingerprint" in event_read_source
    assert "position_id = %s" in protective_source
    assert "instrument_id = %s" in protective_source
    assert "route_fingerprint = %s" in protective_source
    assert "PAPER_POSITION_INSTRUMENT_ID" in protective_source
    assert "PAPER_POSITION_ROUTE_FINGERPRINT" in protective_source
    assert "status = 'pending'" in protective_source
    assert "position_id = %s" in cancel_source
    assert "instrument_id = %s" in cancel_source
    assert "route_fingerprint = %s" in cancel_source
    assert "reduce_only = true" in cancel_source
    assert "status = 'pending'" in cancel_source


def test_paper_position_complete_exact_set_has_no_hidden_limit() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value = cursor
    repo = PaperRepoMixin()
    repo._connect = MagicMock(return_value=connection)  # type: ignore[attr-defined,method-assign]

    assert (
        repo.read_paper_positions(
            status="open",
            instrument_id="instrument-a",
            route_fingerprint="route-a",
            complete_position_ids=("position-1", "position-2"),
        )
        == []
    )
    query, parameters = cursor.execute.call_args.args
    assert "instrument_id = %s" in query
    assert "route_fingerprint = %s" in query
    assert "id = ANY(%s)" in query
    assert "LIMIT %s" not in query
    assert parameters == (
        "instrument-a",
        "route-a",
        ["position-1", "position-2"],
        "open",
    )
    with pytest.raises(ValueError, match="PAPER_POSITION_COMPLETE_IDS_INVALID"):
        repo.read_paper_positions(
            instrument_id="instrument-a",
            route_fingerprint="route-a",
            complete_position_ids=("position-1", "position-1"),
        )

    assert (
        repo.read_paper_orders(
            status="pending",
            complete_pending=True,
        )
        == []
    )
    query, parameters = cursor.execute.call_args.args
    assert "status = %s" in query
    assert "LIMIT %s" not in query
    assert parameters == ["pending"]
    with pytest.raises(
        ValueError,
        match="PAPER_ORDER_COMPLETE_QUERY_REQUIRES_PENDING_STATUS",
    ):
        repo.read_paper_orders(status=None, complete_pending=True)


def test_storage_diagnostics_avoid_full_bars_counts() -> None:
    source = inspect.getsource(PostgresStore.diagnostics)
    status_source = inspect.getsource(PostgresStore.status)
    hygiene_source = inspect.getsource(PostgresStore._data_hygiene_report)

    assert "SELECT COUNT(*) FROM bars" not in status_source
    assert "SELECT COUNT(*)::bigint" not in source
    assert "FROM bars\n                        GROUP BY" not in source
    assert "catalog_estimates_and_canonical_bars" in source
    assert "SELECT MAX(ts) FROM bars WHERE closed = true" in source
    assert "SELECT MAX(ts) FROM futures_continuous_bars WHERE closed = true" in source
    assert '"data_hygiene": data_hygiene' in source
    assert '"futures_continuous": futures_continuous' in source
    assert "SELECT provider, instrument_id, route_fingerprint, COUNT(*)::bigint" in source
    assert "GROUP BY provider, instrument_id, route_fingerprint" in source
    assert "SELECT provider, route_fingerprint, symbol, COUNT(*)::bigint" not in source
    assert "futures_continuous_bars" in hygiene_source
    assert (
        "DISTINCT ON (provider, instrument_id, series_type, roll_policy, timeframe)"
        in inspect.getsource(PostgresStore._futures_continuous_report)
    )
    assert "updated_at >= now() - interval '14 days'" in hygiene_source
    assert "FROM pg_stat_user_tables" in hygiene_source
    assert "COUNT(*) FROM bars" not in hygiene_source
    assert "bars_hygiene_recent_idx" in _initialize_source()


def test_provisional_storage_uses_typed_closed_state_not_source_text() -> None:
    closed = Bar(
        symbol="SPY",
        ts=datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc),
        open=1,
        high=2,
        low=1,
        close=2,
        volume=1,
        timeframe="5m",
        source="ibkr:historical:TRADES:contract=1:route=test",
        closed=True,
        provenance=BarProvenance(
            provider="ibkr",
            instrument_id="test-instrument",
            route_fingerprint="test-route",
            request_type="historical",
            provider_contract_id="1",
            provider_contract_type="STK",
            data_type="TRADES",
        ),
    )
    live = Bar(
        symbol="SPY",
        ts=datetime(2026, 1, 1, 9, 35, tzinfo=timezone.utc),
        open=1,
        high=2,
        low=1,
        close=2,
        volume=1,
        timeframe="5m",
        source="ibkr:db-cache",
        closed=False,
    )

    assert is_provisional_bar(closed) is False
    assert _storage_bar_reject_reason("ibkr", closed) is None
    assert _storage_bar_reject_reason("ibkr:live", closed) is None
    assert is_provisional_bar(live) is True
    assert _storage_bar_reject_reason("ibkr", live) == "provisional_bar"
    assert _storage_bar_reject_reason("ibkr:live", live) == "provisional_bar"


def test_confirmed_flat_bar_revision_is_not_blocked_by_storage_upsert_heuristics() -> None:
    source = inspect.getsource(BarsRepoMixin._write_bars_on_cursor)
    futures_source = inspect.getsource(futures_repo.FuturesRepoMixin)

    assert "EXCLUDED.volume <= 0" not in source
    assert "bars.volume <= 0" not in source
    assert "max(bar.volume, 0.0)" not in source
    assert "max(bar.volume, 0.0)" not in futures_source
    assert "NOT (bars.closed = true AND EXCLUDED.closed = false)" in source


def test_canonical_bar_upserts_enforce_monotonic_revision_sequence() -> None:
    generic_source = inspect.getsource(BarsRepoMixin._write_bars_on_cursor)
    futures_source = inspect.getsource(PostgresStore.write_futures_canonical_bars)
    futures_write_source = inspect.getsource(futures_repo._write_futures_canonical_rows)

    assert "EXCLUDED.revision_sequence > bars.revision_sequence" in generic_source
    assert (
        "bars.revision_sequence IS DISTINCT FROM EXCLUDED.revision_sequence" not in generic_source
    )
    assert "canonical_bar_commit_receipt(" in generic_source
    assert generic_source.count("jsonb_to_recordset") == 2
    assert generic_source.count("(input_json,)") == 2
    assert "RETURNING timeframe, ts" in generic_source
    assert "JOIN bars AS persisted" in generic_source
    assert "executemany" not in generic_source
    assert "timeframe = ANY" not in generic_source
    assert "min(timestamps)" not in generic_source
    assert (
        "EXCLUDED.revision_sequence > futures_continuous_bars.revision_sequence"
        in futures_write_source
    )
    assert (
        "futures_continuous_bars.revision_sequence IS DISTINCT FROM EXCLUDED.revision_sequence"
        not in futures_write_source
    )
    assert futures_write_source.count("jsonb_to_recordset") == 2
    assert futures_write_source.count("(input_json,)") == 2
    assert "RETURNING timeframe, ts" in futures_write_source
    assert "JOIN futures_continuous_bars AS persisted" in futures_write_source
    assert "executemany" not in futures_write_source
    assert "canonical_bar_commit_receipt(" in futures_source


def test_futures_contract_storage_schema_is_separate_from_canonical_bars() -> None:
    source = _initialize_source()
    write_source = inspect.getsource(PostgresStore.write_futures_contract_bars)
    contract_cursor_source = inspect.getsource(PostgresStore._write_futures_contract_bars_on_cursor)
    contract_upsert_source = inspect.getsource(futures_repo._write_futures_contract_rows)
    canonical_write_source = inspect.getsource(PostgresStore.write_futures_canonical_bars)
    canonical_read_source = inspect.getsource(PostgresStore.read_futures_canonical_bars_multi)
    contract_source = inspect.getsource(PostgresStore.write_futures_contract)
    contract_cursor_writer_source = inspect.getsource(
        PostgresStore._write_futures_contract_on_cursor
    )
    current_contract_source = inspect.getsource(PostgresStore.read_current_futures_contract)
    roll_source = inspect.getsource(PostgresStore.write_futures_roll_event)

    assert "CREATE TABLE futures_contracts" in source
    assert "PRIMARY KEY (provider, instrument_id, contract_key)" in source
    assert "is_current boolean NOT NULL DEFAULT false" in source
    assert "futures_contracts_current_uidx" in source
    assert "CREATE TABLE futures_roll_events" in source
    assert "roll_policy text NOT NULL," in source
    assert "ALTER TABLE futures_roll_events" not in source
    assert "PRIMARY KEY (provider, instrument_id, roll_ts, roll_policy)" in source
    assert "CREATE TABLE futures_contract_bars" in source
    assert "provider, instrument_id, route_fingerprint," in source
    assert "contract_key, timeframe, ts" in source
    assert "futures_contract_bars_route_tf_ts_idx" in source
    assert "futures_contract_bars_instrument_tf_ts_idx" not in source
    assert "create_hypertable('futures_contract_bars', 'ts')" in source
    assert "CREATE TABLE futures_continuous_bars" in source
    assert "series_type text NOT NULL," in source
    assert "roll_policy text NOT NULL," in source
    assert "series_type text NOT NULL DEFAULT" not in source
    assert "ALTER COLUMN series_type" not in source
    assert "ALTER COLUMN roll_policy" not in source
    assert (
        "PRIMARY KEY (provider, instrument_id, series_type, roll_policy, timeframe, ts)" in source
    )
    assert "CREATE INDEX bars_route_tf_ts_idx" not in source
    assert "futures_continuous_bars_instrument_tf_ts_idx" not in source
    assert "create_hypertable('futures_continuous_bars', 'ts')" in source
    extension_ddl_removed = source.replace(
        "CREATE EXTENSION IF NOT EXISTS timescaledb", ""
    ).replace("CREATE EXTENSION IF NOT EXISTS pg_stat_statements", "")
    assert "IF NOT EXISTS" not in extension_ddl_removed
    assert "if_not_exists" not in source
    assert "migrate_data" not in source

    assert "_write_futures_contract_bars_on_cursor" in write_source
    assert "expected_route_fingerprint=fingerprint" in contract_cursor_source
    assert "_write_futures_contract_rows" in contract_cursor_source
    assert (
        "NOT (futures_contract_bars.closed = true AND EXCLUDED.closed = false)"
        in contract_upsert_source
    )
    assert "futures_contract_bars.open IS DISTINCT FROM EXCLUDED.open" in contract_upsert_source
    assert "futures_contract_bars.source IS DISTINCT FROM EXCLUDED.source" in contract_upsert_source
    assert "futures_contract_bars.closed IS DISTINCT FROM EXCLUDED.closed" in contract_upsert_source
    assert (
        "EXCLUDED.revision_sequence > futures_contract_bars.revision_sequence"
        in contract_upsert_source
    )
    assert "futures_contract_bars.revision_sequence IS DISTINCT" not in contract_upsert_source
    assert "JOIN futures_contract_bars AS persisted" in contract_upsert_source
    assert "_write_futures_contract_on_cursor(" in contract_source
    assert (
        'metadata["provider_contract_id"] = provider_contract_id' in contract_cursor_writer_source
    )
    assert "Jsonb(metadata)" in contract_cursor_writer_source
    assert "SET is_current = false" in contract_cursor_writer_source
    assert "futures_contracts.metadata || EXCLUDED.metadata" in contract_cursor_writer_source
    assert "AND is_current = true" in current_contract_source
    assert "FROM futures_continuous_bars" not in current_contract_source
    assert 'Jsonb(dict(event.get("metadata") or {}))' in roll_source
    assert "if not roll_policy" in roll_source
    assert "_write_futures_canonical_rows" in canonical_write_source
    assert "expected_route_fingerprint=fingerprint" in canonical_write_source
    assert "_canonical_futures_bar_admission_metadata(metadata)" in canonical_write_source
    assert "admission_metadata," in canonical_write_source
    assert "Jsonb(admission_metadata)" not in canonical_write_source
    assert "_require_canonical_route(series_type, roll_policy)" in canonical_write_source
    canonical_upsert_source = inspect.getsource(
        __import__(
            "aef_terminal.storage.repos.futures", fromlist=["_write_futures_canonical_rows"]
        )._write_futures_canonical_rows
    )
    assert (
        "NOT (futures_continuous_bars.closed = true AND EXCLUDED.closed = false)"
        in canonical_upsert_source
    )
    assert "futures_continuous_bars.open IS DISTINCT FROM EXCLUDED.open" in canonical_upsert_source
    assert (
        "futures_continuous_bars.source IS DISTINCT FROM EXCLUDED.source" in canonical_upsert_source
    )
    assert (
        "futures_continuous_bars.closed IS DISTINCT FROM EXCLUDED.closed" in canonical_upsert_source
    )
    assert (
        "futures_continuous_bars.metadata IS DISTINCT FROM EXCLUDED.metadata"
        in canonical_upsert_source
    )
    assert "FROM futures_continuous_bars" in canonical_read_source
    assert "_require_canonical_route(series_type, roll_policy)" in canonical_read_source
    assert not hasattr(PostgresStore, "delete_futures_canonical_window")
    assert not hasattr(PostgresStore, "read_futures_continuous_bars")
    assert not hasattr(PostgresStore, "rebuild_futures_canonical_bars")
    assert not hasattr(PostgresStore, "delete_futures_history_window")


def test_futures_storage_preserves_opaque_identity_tokens_without_trimming() -> None:
    assert futures_repo._contract_key({"contract_key": " contract-id "}) == " contract-id "
    for method in (
        futures_repo.FuturesRepoMixin.write_futures_contract,
        futures_repo.FuturesRepoMixin.write_futures_roll_event,
        futures_repo.FuturesRepoMixin.write_futures_contract_bars,
        futures_repo.FuturesRepoMixin.read_futures_contract_bars,
        futures_repo.FuturesRepoMixin.read_futures_roll_events,
        futures_repo.FuturesRepoMixin.write_futures_canonical_bars,
        futures_repo.FuturesRepoMixin.read_futures_canonical_bars,
        futures_repo.FuturesRepoMixin.read_current_futures_contract,
    ):
        method_source = inspect.getsource(method)
        assert "_clean_text(instrument_id)" not in method_source
        assert "_clean_text(contract_key)" not in method_source
    assert not hasattr(
        futures_repo.FuturesRepoMixin,
        "delete_futures_canonical_window",
    )

    captured: list[tuple[object, ...]] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _statement, params):
            captured.append(tuple(params))

        def fetchone(self):
            return None

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    repo = futures_repo.FuturesRepoMixin()
    repo._connect = lambda: Connection()

    with pytest.raises(ValueError, match="FUTURES_PROVIDER_NOT_CANONICAL"):
        repo.read_current_futures_contract(
            provider=" IBKR ",
            instrument_id=" instrument-id ",
        )
    assert captured == []


def test_generic_bars_storage_rejects_futures_contract_classes_for_write_and_read() -> None:
    class BareBarsRepo(BarsRepoMixin, PostgresConnectionMixin):
        def __init__(self) -> None:
            PostgresConnectionMixin.__init__(
                self,
                None,
                canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
            )

        def _connect(self):
            raise AssertionError("generic futures bars access must fail before opening SQL")

    repo = BareBarsRepo()

    with pytest.raises(ValueError, match="Generic bars storage cannot read/write futures"):
        repo.write_bars(
            [
                Bar(
                    symbol="ES",
                    ts=datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc),
                    open=1,
                    high=2,
                    low=1,
                    close=2,
                    volume=1,
                    timeframe="5m",
                    source="ibkr",
                )
            ],
            provider="ibkr",
            instrument=ibkr_future_payload("ES"),
        )

    with pytest.raises(ValueError, match="Generic bars storage cannot read/write futures"):
        repo.read_bars(timeframe="5m", provider="ibkr", instrument=ibkr_future_payload("ES"))

    with pytest.raises(ValueError, match="Generic bars storage cannot read/write futures"):
        repo.read_bars_multi(
            timeframes=["5m"],
            providers=["ibkr"],
            instrument=ibkr_future_payload("ES"),
        )
