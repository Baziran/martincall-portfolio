from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.bar_quality import canonical_storage_bar_provenance
from aef_terminal.storage.repos import bars as bars_repo
from aef_terminal.storage.repos import futures as futures_repo
from tests.provider_payloads import ibkr_stock_payload


def _reader(repo_type, rows):
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connection.__enter__.return_value = connection
    repo = repo_type()
    repo._connect = MagicMock(return_value=connection)
    return repo


@pytest.mark.parametrize("kind", ["contract", "canonical"])
def test_futures_materialization_reuses_only_batch_scoped_immutable_provenance(monkeypatch, kind):
    timestamp = datetime(2026, 8, 27, tzinfo=UTC)
    rows = [
        (timestamp + timedelta(minutes=i), 100, 102, 99, 101, 10, tf, "ibkr", True)
        for tf in ("1m", "5m")
        for i in range(20)
    ]
    repo = _reader(futures_repo.FuturesRepoMixin, rows)
    factory = MagicMock(wraps=canonical_storage_bar_provenance)
    monkeypatch.setattr(futures_repo, "canonical_storage_bar_provenance", factory)
    read = getattr(repo, f"read_futures_{kind}_bars_multi")
    arguments = {
        "provider": "ibkr",
        "instrument_id": "ibkr|future_root|ES",
        "route_fingerprint": "route-a",
        "symbol": "ES",
        "timeframes": ("1m", "5m"),
        **(
            {"contract_key": "FUT:123"}
            if kind == "contract"
            else {
                "series_type": "continuous",
                "roll_policy": "provider_current",
            }
        ),
    }
    result = read(**arguments)
    scope = result["1m"][0].provenance
    assert all(bar.provenance is scope for bars in result.values() for bar in bars)
    factory.assert_called_once_with("ibkr", "ibkr|future_root|ES", "route-a")
    assert scope is not None
    with pytest.raises(FrozenInstanceError):
        scope.route_fingerprint = "wrong"
    changed = read(**{**arguments, "route_fingerprint": "route-b"})
    assert changed["1m"][0].provenance is not scope
    assert changed["1m"][0].provenance.route_fingerprint == "route-b"


@pytest.mark.parametrize("multi", [False, True])
def test_generic_bar_read_materializes_one_provenance_per_route(monkeypatch, multi):
    timestamp = datetime(2026, 8, 27, tzinfo=UTC)
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    rows = [
        ("SPY", timestamp + timedelta(minutes=i * 5), 100, 102, 99, 101, 10, "5m", "ibkr", True)
        for i in range(40)
    ]
    if multi:
        rows = [("ibkr", "5m", *row[:7], *row[8:]) for row in rows]
    repo = _reader(bars_repo.BarsRepoMixin, rows)
    factory = MagicMock(wraps=canonical_storage_bar_provenance)
    monkeypatch.setattr(bars_repo, "canonical_storage_bar_provenance", factory)
    result = (
        repo.read_bars_multi(timeframes=["5m"], providers=["ibkr"], instrument=instrument)[
            ("ibkr", "5m")
        ]
        if multi
        else repo.read_bars("5m", "ibkr", instrument=instrument)
    )
    assert len(result) == 40
    assert all(bar.provenance is result[0].provenance for bar in result)
    factory.assert_called_once_with("ibkr", route.instrument_id, route.fingerprint)


def test_recent_batch_keeps_instrument_and_route_provenances_separate(monkeypatch):
    timestamp = datetime(2026, 8, 27, tzinfo=UTC)
    instruments = [ibkr_stock_payload("SPY", con_id=100), ibkr_stock_payload("QQQ", con_id=200)]
    routes = [route_instrument(instrument) for instrument in instruments]
    rows = [
        (
            route.instrument_id,
            route.fingerprint,
            "display",
            timestamp + timedelta(minutes=i * 5),
            100,
            102,
            99,
            101,
            10,
            tf,
            "ibkr",
            True,
        )
        for route in routes
        for tf in ("1m", "5m")
        for i in range(4)
    ]
    repo = _reader(bars_repo.BarsRepoMixin, rows)
    factory = MagicMock(wraps=canonical_storage_bar_provenance)
    monkeypatch.setattr(bars_repo, "canonical_storage_bar_provenance", factory)
    result = repo.read_recent_bars_batch(
        [(tf, "ibkr", 4, instrument) for instrument in instruments for tf in ("1m", "5m")]
    )
    assert factory.call_count == 2
    for route in routes:
        first = result[(route.instrument_id, "1m")][0].provenance
        assert first.instrument_id == route.instrument_id
        assert first.route_fingerprint == route.fingerprint
        assert all(
            bar.provenance is first
            for tf in ("1m", "5m")
            for bar in result[(route.instrument_id, tf)]
        )
    assert (
        result[(routes[0].instrument_id, "1m")][0].provenance
        is not result[(routes[1].instrument_id, "1m")][0].provenance
    )
