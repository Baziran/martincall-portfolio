from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.data.instrument_identity import qualified_instrument_id
from aef_terminal.data import provider_history as loader_patchable
from aef_terminal.data.adapters import ibkr_live as ibkr_live_module
from aef_terminal.data.providers import get_provider
from tests.provider_payloads import ibkr_future_payload, ibkr_stock_payload


class _FakeStore:
    def __init__(self, grouped: dict[tuple[Any, ...], list[Bar]] | None = None) -> None:
        self.grouped = grouped or {}

    def read_bars_multi(
        self,
        *,
        timeframes: list[str],
        providers: list[str],
        start: datetime,
        end: datetime,
        instrument: dict[str, Any],
    ) -> dict[tuple[str, str], list[Bar]]:
        _ = start, end
        instrument_id = qualified_instrument_id(instrument)
        result: dict[tuple[str, str], list[Bar]] = {}
        for key, bars in self.grouped.items():
            if len(key) == 2:
                provider, timeframe = key
                row_instrument_id = instrument_id
            else:
                row_instrument_id, provider, timeframe = key
            if (
                row_instrument_id == instrument_id
                and provider in providers
                and timeframe in timeframes
            ):
                result[(provider, timeframe)] = list(bars)
        return result

    def read_bars(
        self, *, timeframe: str, provider: str, start: datetime, instrument: dict[str, Any]
    ) -> list[Bar]:
        _ = start
        instrument_id = qualified_instrument_id(instrument)
        return list(
            self.grouped.get(
                (instrument_id, provider, timeframe), self.grouped.get((provider, timeframe), [])
            )
        )

    def read_futures_canonical_bars(
        self, *, provider: str, instrument_id: str, timeframe: str, start: datetime, **_route
    ) -> list[Bar]:
        return list(self.grouped.get((instrument_id, provider, timeframe), []))


def _bar(ts: str, close: float = 100.0, timeframe: str = "5m", provider: str = "ibkr") -> Bar:
    source = "ibkr:db-cache:5m" if provider == "ibkr" else provider
    return Bar(
        ts=datetime.fromisoformat(ts).replace(tzinfo=UTC),
        open=close,
        high=close + 0.1,
        low=close - 0.1,
        close=close,
        volume=1.0,
        symbol="QQQ",
        timeframe=timeframe,
        source=source,
    )


def _bars(start_minute: int, end_minute: int, close: float = 100.0) -> list[Bar]:
    return [
        _bar(f"2026-07-03T05:{minute:02d}:00+00:00", close + index)
        for index, minute in enumerate(range(start_minute, end_minute + 1, 5))
    ]


def test_load_ibkr_cache_context_uses_confirmed_history_only() -> None:
    store = _FakeStore(
        {
            ("ibkr", "5m"): [
                _bar("2026-06-16T19:40:00+00:00", 730.0),
                _bar("2026-06-16T19:50:00+00:00", 731.0),
            ],
        }
    )

    cached, warnings = ibkr_live_module._load_ibkr_cache_context(
        store,
        "5m",
        "1d",
        ibkr_stock_payload("QQQ"),
        adapter=get_provider("ibkr"),
    )

    assert [bar.ts.isoformat() for bar in cached] == [
        "2026-06-16T19:40:00+00:00",
        "2026-06-16T19:50:00+00:00",
    ]
    assert not any("quote" in warning.lower() for warning in warnings)


def test_load_ibkr_cache_context_uses_exact_future_storage_key() -> None:
    gc_id = qualified_instrument_id(ibkr_future_payload("GC", exchange="COMEX"))
    store = _FakeStore(
        {
            (gc_id, "ibkr", "5m"): _bars(20, 50, 4180.0),
        }
    )

    cached, warnings = ibkr_live_module._load_ibkr_cache_context(
        store,
        "5m",
        "1d",
        ibkr_future_payload("GC", exchange="COMEX"),
        adapter=get_provider("ibkr"),
    )

    assert cached[-1].ts == datetime(2026, 7, 3, 5, 50, tzinfo=UTC)
    assert cached[-1].close == 4186.0
    assert len(warnings) == 1
    assert "IBKR returned only 7 bars" in warnings[0]


def test_load_ibkr_cache_context_does_not_fallback_to_different_future_root() -> None:
    cl_id = qualified_instrument_id(ibkr_future_payload("CL", exchange="NYMEX"))
    store = _FakeStore(
        {
            (cl_id, "ibkr", "5m"): [
                _bar("2026-07-02T12:20:00+00:00", 67.20),
                _bar("2026-07-02T12:25:00+00:00", 67.35),
                _bar("2026-07-03T05:45:00+00:00", 69.15),
                _bar("2026-07-03T05:50:00+00:00", 69.19),
                _bar("2026-07-03T05:55:00+00:00", 69.19),
            ],
        }
    )

    cached, warnings = ibkr_live_module._load_ibkr_cache_context(
        store,
        "5m",
        "1d",
        ibkr_stock_payload("REMOVED"),
        adapter=get_provider("ibkr"),
    )

    assert cached == []
    assert warnings == ["No IBKR bars are available in the requested calendar window."]


def test_load_ibkr_cache_context_has_no_recent_cache_fallback(monkeypatch) -> None:
    monkeypatch.setattr(loader_patchable, "read_provider_history", lambda *_args, **_kwargs: [])

    cached, warnings = ibkr_live_module._load_ibkr_cache_context(
        _FakeStore(),
        "5m",
        "1d",
        ibkr_stock_payload("QQQ"),
        adapter=get_provider("ibkr"),
    )

    assert cached == []
    assert warnings == ["No IBKR bars are available in the requested calendar window."]
    assert not hasattr(ibkr_live_module, "read_recent_provider_cache")
