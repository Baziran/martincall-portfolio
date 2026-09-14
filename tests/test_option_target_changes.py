from __future__ import annotations

from collections import deque
from contextlib import contextmanager, nullcontext

import pytest

from aef_terminal.runtime.option_target_changes import option_targets_revision
from aef_terminal.storage.repos.gex import GexRepoMixin


class _Cursor:
    def __init__(self, *, one=(), all_rows=(), rowcount=0):
        self.one = deque(one)
        self.all_rows = deque(all_rows)
        self.rowcount = rowcount
        self.statements = []

    def execute(self, sql, _params):
        self.statements.append(sql)

    def fetchone(self):
        return self.one.popleft()

    def fetchall(self):
        return self.all_rows.popleft()


class _Store(GexRepoMixin):
    def __init__(self, cursor, *, fail_commit=False):
        self.cursor = cursor
        self.fail_commit = fail_commit
        self.committed = False
        self.initial_revision = option_targets_revision()

    def _psycopg(self):
        pass

    def _require_canonical_writer_lease(self):
        pass

    def _acquire_canonical_writer_fence_on_cursor(self, _cur):
        pass

    @contextmanager
    def _connect(self):
        cursor = self.cursor

        class Connection:
            def transaction(self):
                return nullcontext()

            def cursor(self):
                return nullcontext(cursor)

        yield Connection()
        assert option_targets_revision() == self.initial_revision
        if self.fail_commit:
            raise RuntimeError("commit rejected")
        self.committed = True


def _canonical_target():
    return GexRepoMixin._canonical_option_target_write(
        "instrument-1",
        "SPY",
        "5m",
        {
            "id": "target-1",
            "point": {"price": 100.0, "ts": "2026-08-30T12:00:00+00:00"},
            "payload": {
                "intent": {
                    "provider_symbol": "SPY",
                    "mode": "normal",
                    "right": "P",
                    "target_dte": "0dte",
                    "sec_type": "OPT",
                    "con_id": 123,
                    "local_symbol": "SPY P100",
                    "exchange": "SMART",
                    "expiry": "20990830",
                    "expiry_at": "2099-08-30T20:00:00+00:00",
                    "strike": 100.0,
                    "trading_class": "SPY",
                    "multiplier": 100.0,
                    "currency": "USD",
                },
            },
        },
        route_fingerprint="route-1",
        provider="ibkr",
        provider_contract_id="123",
        generate_id=False,
    )[-1]


@pytest.mark.parametrize("operation", ["create", "intent", "sample", "delete"])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("fail_commit", [False, True])
def test_every_option_target_writer_invalidates_only_after_real_commit(
    operation,
    changed,
    fail_commit,
):
    target = _canonical_target()
    if operation == "create":
        cursor = _Cursor(
            one=[(target,)] if changed else [None, (target,)],
            # Exercise eviction inside the same create transaction as well.
            all_rows=[[("old", {**target, "id": "old"})]] if changed else [],
        )
    elif operation == "intent":
        cursor = _Cursor(one=[(target,)], rowcount=int(changed))
    elif operation == "sample":
        cursor = _Cursor(
            all_rows=[
                [("instrument-1", "route-1", "target-1", target)],
                [("instrument-1", "route-1", "target-1", target)] if changed else [],
            ]
        )
    else:
        cursor = _Cursor(rowcount=int(changed))
    store = _Store(cursor, fail_commit=fail_commit)

    def write():
        if operation == "delete":
            return store.delete_option_target("instrument-1", "route-1", "target-1")
        if operation == "sample":
            return store.update_option_target_market_samples(
                [
                    ("instrument-1", "route-1", "target-1", {}),
                ]
            )
        callback = (
            store.create_option_target_intent
            if operation == "create"
            else store.update_option_target_intent
        )
        return callback(
            "instrument-1",
            "SPY",
            "5m",
            target,
            route_fingerprint="route-1",
            provider="ibkr",
            provider_contract_id="123",
        )

    if fail_commit:
        with pytest.raises(RuntimeError, match="commit rejected"):
            write()
        assert not store.committed
    else:
        write()
        assert store.committed
    assert option_targets_revision() == store.initial_revision + int(changed and not fail_commit)


def test_option_target_validation_failure_does_not_invalidate_readers():
    store = _Store(_Cursor())
    with pytest.raises(ValueError):
        store.delete_option_target("", "route-1", "target-1")
    assert not store.committed
    assert option_targets_revision() == store.initial_revision
