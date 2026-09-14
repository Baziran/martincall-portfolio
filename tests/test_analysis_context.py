from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from aef_terminal.domain import Bar
from aef_terminal.engine.analyze import context as analyze_context
from aef_terminal.engine.snapshot.market_inputs import load_tick_flow_input
from aef_terminal.data.providers import route_instrument
from tests.provider_payloads import ibkr_future_payload


class _TickStore:
    def __init__(self) -> None:
        self.symbols: list[tuple[str, str]] = []

    def initialize(self) -> None:
        return None

    def read_tick_delta(
        self, instrument_id, route_fingerprint, _start, _end, *, bucket: str = "1 minute"
    ):
        self.symbols.append((instrument_id, route_fingerprint))
        route = route_instrument(ibkr_future_payload("ES"))
        if (instrument_id, route_fingerprint) == (route.instrument_id, route.fingerprint):
            return [{"bucket": datetime(2026, 1, 1, tzinfo=UTC), "net_delta": 10}]
        return []


def test_tick_flow_context_reads_only_explicit_provider_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.engine.snapshot.market_inputs.tick_ingest_quality",
        lambda *_args, **_kwargs: {
            "complete": True,
            "dropped": 0,
            "drop_in_window": False,
            "coverage": 1.0,
            "last_drop_at": None,
            "last_drop_reason": "",
            "db_consecutive_errors": 0,
        },
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar("ES", start, 100, 101, 99, 100, 10, "1m", "ibkr"),
        Bar("ES", start + timedelta(minutes=1), 100, 102, 100, 101, 12, "1m", "ibkr"),
    ]
    store = _TickStore()

    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    context = load_tick_flow_input(store, instrument, bars)

    assert context == {
        "source": "ibkr_tick",
        "provider": route.provider,
        "symbol": "ES",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "delta": [{"bucket": datetime(2026, 1, 1, tzinfo=UTC), "net_delta": 10}],
        "classification_method": "inferred_quote_or_tick_test",
        "classification_confidence": "advisory",
        "decision_eligible": False,
        "availability_state": "available",
        "quality": {
            "complete": True,
            "dropped": 0,
            "drop_in_window": False,
            "coverage": 1.0,
            "last_drop_at": None,
            "last_drop_reason": "",
            "db_consecutive_errors": 0,
        },
    }
    assert store.symbols == [(route.instrument_id, route.fingerprint)]


def test_tick_flow_context_exposes_storage_failure_as_typed_unavailable() -> None:
    class FailedStore:
        @staticmethod
        def read_tick_delta(*_args, **_kwargs):
            raise RuntimeError("tick storage offline")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [Bar("ES", start, 100, 101, 99, 100, 10, "1m", "ibkr")]
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)

    context = load_tick_flow_input(FailedStore(), instrument, bars)

    assert context == {
        "source": "ibkr_tick",
        "provider": route.provider,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "delta": [],
        "decision_eligible": False,
        "availability_state": "error",
        "error_code": "TICK_FLOW_INPUT_UNAVAILABLE",
        "reason": "tick storage offline",
    }


def test_analyze_context_is_storage_agnostic() -> None:
    source = inspect.getsource(analyze_context)

    assert "store." not in source
    assert "read_option_targets" not in source
    assert "read_tick_delta" not in source
    assert "get_gex_analysis_pair" not in source
