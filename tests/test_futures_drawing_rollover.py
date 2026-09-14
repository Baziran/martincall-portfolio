from __future__ import annotations

import inspect
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from aef_terminal.data import futures_lifecycle
from aef_terminal.data.instrument_identity import (
    current_futures_contract,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import FuturesRouteTransition
from aef_terminal.storage.repos import drawings as drawings_repo
from aef_terminal.storage.repos import futures as futures_repo
from aef_terminal.storage.repos.connection import (
    CanonicalWriterCapability,
    PostgresConnectionMixin,
)
from tests.provider_payloads import ibkr_future_payload


_INSTRUMENT_ID = "ibkr|future_root|ES|CME|USD|"
_PREVIOUS_ROUTE = route_fingerprint(ibkr_future_payload("ES", con_id=111, local_symbol="ESU6"))
_NEXT_ROUTE = route_fingerprint(ibkr_future_payload("ES", con_id=222, local_symbol="ESZ6"))


def _transition(
    *,
    previous_contract_id: str = "111",
    next_contract_id: str = "222",
    previous_route: str = _PREVIOUS_ROUTE,
    next_route: str = _NEXT_ROUTE,
) -> FuturesRouteTransition:
    return FuturesRouteTransition(
        provider="ibkr",
        instrument_id=_INSTRUMENT_ID,
        previous_route_fingerprint=previous_route,
        next_route_fingerprint=next_route,
        previous_session_contract_id=previous_contract_id,
        next_session_contract_id=next_contract_id,
    )


def _contract(*, provider_contract_id: str = "222") -> dict[str, Any]:
    return {
        "provider": "ibkr",
        "instrument_id": _INSTRUMENT_ID,
        "contract_key": "ESZ6",
        "provider_contract_id": provider_contract_id,
        "root": "ES",
        "exchange": "CME",
        "currency": "USD",
        "local_symbol": "ESZ6",
        "con_id": int(provider_contract_id),
        "expiry": "20261218",
        "contract_month": "202612",
        "is_current": True,
    }


def _drawing(
    drawing_id: str,
    *,
    route: str,
    provider_contract_id: str,
) -> dict[str, Any]:
    return {
        "id": drawing_id,
        "route_fingerprint": route,
        "timeframe": "5m",
        "updated_at": datetime(2026, 5, 1, tzinfo=UTC),
        "payload": {
            "id": drawing_id,
            "instrument_id": _INSTRUMENT_ID,
            "route_fingerprint": route,
            "symbol": "ES",
            "provider": "ibkr",
            "provider_contract_id": provider_contract_id,
            "points": [{"ts": "2026-04-01T14:30:00+00:00", "price": 5000.0}],
        },
    }


class _RolloverCursor:
    def __init__(
        self,
        *,
        previous_provider_contract_id: str | None,
        drawings: list[dict[str, Any]],
    ) -> None:
        self.previous_provider_contract_id = previous_provider_contract_id
        self.drawings = drawings
        self.calls: list[tuple[str, Any]] = []
        self.query = ""
        self.params: Any = None
        self.rowcount = -1

    def __enter__(self) -> _RolloverCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.query = " ".join(str(query).split())
        self.params = params
        self.calls.append((self.query, params))
        self.rowcount = -1
        if self.query.startswith("UPDATE drawings SET payload ="):
            next_route, next_contract_id, instrument_id, previous_route = params
            moved = 0
            for drawing in self.drawings:
                payload = drawing["payload"]
                if (
                    payload["instrument_id"] == instrument_id
                    and drawing["route_fingerprint"] == previous_route
                ):
                    drawing["route_fingerprint"] = next_route
                    payload["route_fingerprint"] = next_route
                    payload["provider_contract_id"] = next_contract_id
                    moved += 1
            self.rowcount = moved

    def fetchone(self):
        if "SELECT metadata ->> 'provider_contract_id'" in self.query:
            if self.previous_provider_contract_id is None:
                return None
            return (self.previous_provider_contract_id,)
        raise AssertionError(f"unexpected fetchone query: {self.query}")

    def fetchall(self):
        if "SELECT id, route_fingerprint, timeframe, updated_at, payload" in self.query:
            instrument_id, routes, contract_ids = self.params
            return [
                (
                    drawing["id"],
                    drawing["route_fingerprint"],
                    drawing["timeframe"],
                    drawing["updated_at"],
                    deepcopy(drawing["payload"]),
                )
                for drawing in self.drawings
                if drawing["payload"]["instrument_id"] == instrument_id
                and (
                    drawing["route_fingerprint"] in routes
                    or drawing["payload"]["provider_contract_id"] in contract_ids
                )
            ]
        if "SELECT id, timeframe, updated_at, payload" in self.query:
            instrument_id, route = self.params
            return [
                (
                    drawing["id"],
                    drawing["timeframe"],
                    drawing["updated_at"],
                    deepcopy(drawing["payload"]),
                )
                for drawing in self.drawings
                if drawing["payload"]["instrument_id"] == instrument_id
                and drawing["route_fingerprint"] == route
            ]
        raise AssertionError(f"unexpected fetchall query: {self.query}")


class _Transaction:
    def __init__(self, connection: _RolloverConnection) -> None:
        self.connection = connection
        self.before = deepcopy(connection.cursor_value.drawings)

    def __enter__(self) -> _Transaction:
        return self

    def __exit__(self, exc_type, *_args: Any) -> None:
        self.connection.rolled_back = exc_type is not None
        self.connection.committed = exc_type is None
        if exc_type is not None:
            self.connection.cursor_value.drawings[:] = self.before
        return None


class _RolloverConnection:
    def __init__(self, cursor: _RolloverCursor) -> None:
        self.cursor_value = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> _RolloverCursor:
        return self.cursor_value

    def transaction(self) -> _Transaction:
        return _Transaction(self)


class _Repo(futures_repo.FuturesRepoMixin, PostgresConnectionMixin):
    def __init__(self, connection: _RolloverConnection) -> None:
        PostgresConnectionMixin.__init__(
            self,
            None,
            canonical_writer_capability=CanonicalWriterCapability.TEST_BYPASS,
        )
        self.connection = connection

    def _psycopg(self):
        return SimpleNamespace()

    @contextmanager
    def _connect(self):
        yield self.connection


def test_current_futures_rollover_moves_month_old_drawings_in_same_transaction() -> None:
    original = _drawing(
        "channel-months-old",
        route=_PREVIOUS_ROUTE,
        provider_contract_id="111",
    )
    cursor = _RolloverCursor(
        previous_provider_contract_id="111",
        drawings=[deepcopy(original)],
    )
    connection = _RolloverConnection(cursor)

    result = _Repo(connection).write_futures_contract(
        _contract(),
        route_transition=_transition(),
    )

    assert result == "ESZ6"
    assert connection.committed is True
    assert connection.rolled_back is False
    moved = cursor.drawings[0]
    assert moved["id"] == original["id"]
    assert moved["timeframe"] == original["timeframe"]
    assert moved["updated_at"] == original["updated_at"]
    assert moved["route_fingerprint"] == _NEXT_ROUTE
    assert moved["payload"] == {
        **original["payload"],
        "route_fingerprint": _NEXT_ROUTE,
        "provider_contract_id": "222",
    }
    statements = [query for query, _params in cursor.calls]
    drawing_update = next(
        index
        for index, query in enumerate(statements)
        if query.startswith("UPDATE drawings SET payload =")
    )
    contract_demotion = next(
        index
        for index, query in enumerate(statements)
        if query.startswith("UPDATE futures_contracts SET is_current = false")
    )
    assert drawing_update < contract_demotion
    assert "updated_at" not in statements[drawing_update].split("WHERE", 1)[0]


def test_same_current_contract_does_not_touch_drawings() -> None:
    cursor = _RolloverCursor(
        previous_provider_contract_id="222",
        drawings=[
            _drawing(
                "current-channel",
                route=_NEXT_ROUTE,
                provider_contract_id="222",
            )
        ],
    )
    connection = _RolloverConnection(cursor)

    _Repo(connection).write_futures_contract(
        _contract(),
        route_transition=_transition(
            previous_contract_id="222",
            previous_route=_NEXT_ROUTE,
        ),
    )

    assert connection.committed is True
    assert not any(" drawings" in query for query, _params in cursor.calls)


def test_changed_current_contract_without_typed_transition_rolls_back() -> None:
    original = _drawing(
        "protected-channel",
        route=_PREVIOUS_ROUTE,
        provider_contract_id="111",
    )
    cursor = _RolloverCursor(
        previous_provider_contract_id="111",
        drawings=[deepcopy(original)],
    )
    connection = _RolloverConnection(cursor)

    with pytest.raises(RuntimeError, match="FUTURES_CURRENT_ROUTE_REBIND_REQUIRED"):
        _Repo(connection).write_futures_contract(_contract())

    assert connection.rolled_back is True
    assert cursor.drawings == [original]
    assert not any(
        query.startswith("UPDATE futures_contracts SET is_current = false")
        for query, _params in cursor.calls
    )


def test_unexpected_new_drawing_scope_rolls_back_route_transition() -> None:
    original = _drawing(
        "protected-channel",
        route=_PREVIOUS_ROUTE,
        provider_contract_id="111",
    )
    conflicting = _drawing(
        "unexpected-new-scope",
        route=_NEXT_ROUTE,
        provider_contract_id="222",
    )
    cursor = _RolloverCursor(
        previous_provider_contract_id="111",
        drawings=[deepcopy(original), deepcopy(conflicting)],
    )
    connection = _RolloverConnection(cursor)

    with pytest.raises(RuntimeError, match="FUTURES_DRAWING_REBIND_TARGET_NOT_EMPTY"):
        _Repo(connection).write_futures_contract(
            _contract(),
            route_transition=_transition(),
        )

    assert connection.rolled_back is True
    assert cursor.drawings == [original, conflicting]
    assert not any(
        query.startswith("UPDATE futures_contracts SET is_current = false")
        for query, _params in cursor.calls
    )


class _RouteCheckCursor:
    def __init__(self, *, current_provider_contract_id: str) -> None:
        self.current_provider_contract_id = current_provider_contract_id
        self.query = ""
        self.calls: list[tuple[str, Any]] = []

    def execute(self, query: str, params: Any = None) -> None:
        self.query = " ".join(str(query).split())
        self.calls.append((self.query, params))

    def fetchone(self):
        if "SELECT identity_scope, asset_class" in self.query:
            return ("root", "future")
        if "SELECT metadata ->> 'provider_contract_id'" in self.query:
            return (self.current_provider_contract_id,)
        raise AssertionError(f"unexpected fetchone query: {self.query}")


def test_stale_drawing_save_rechecks_current_contract_under_lifecycle_lock() -> None:
    cursor = _RouteCheckCursor(current_provider_contract_id="222")

    with pytest.raises(RuntimeError, match="DRAWING_FUTURES_ROUTE_STALE"):
        drawings_repo._verify_drawing_route_current_on_cursor(
            cursor,
            provider="ibkr",
            instrument_id=_INSTRUMENT_ID,
            provider_contract_id="111",
        )

    assert cursor.calls[0] == (
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        ("ibkr", _INSTRUMENT_ID),
    )
    replace_source = inspect.getsource(drawings_repo.DrawingsRepoMixin.replace_drawings)
    futures_source = inspect.getsource(
        futures_repo.FuturesRepoMixin._write_futures_contract_on_cursor
    )
    assert "_verify_drawing_route_current_on_cursor(" in replace_source
    assert "lock_provider_instrument_lifecycle_on_cursor(" in futures_source
    assert "|current:" not in inspect.getsource(futures_repo)


def test_route_owner_passes_exact_typed_transition_to_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = ibkr_future_payload("ES", con_id=111, local_symbol="ESU6")
    next_instrument = ibkr_future_payload("ES", con_id=222, local_symbol="ESZ6")

    class Adapter:
        def resolve_current_contract(self, _instrument: dict[str, Any]) -> dict[str, Any]:
            return next_instrument

        def session_contract_id(self, instrument: dict[str, Any]) -> str:
            return str(current_futures_contract(instrument)["provider_contract_id"])

    adapter = Adapter()

    def route(instrument: dict[str, Any], *, expected_source: str | None = None):
        if expected_source is not None:
            assert expected_source == "ibkr"
        return SimpleNamespace(
            instrument=instrument,
            adapter=adapter,
            provider="ibkr",
            instrument_id=qualified_instrument_id(instrument),
            fingerprint=route_fingerprint(instrument),
        )

    writes: list[tuple[dict[str, Any], FuturesRouteTransition]] = []

    class Store:
        def write_futures_contract(
            self,
            contract: dict[str, Any],
            *,
            route_transition: FuturesRouteTransition,
        ) -> None:
            writes.append((contract, route_transition))

    monkeypatch.setattr(futures_lifecycle, "route_instrument", route)

    result = futures_lifecycle.resolve_and_persist_current_contract(
        Store(),
        previous,
    )

    assert result is next_instrument
    assert len(writes) == 1
    assert writes[0][1] == _transition()
