from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aef_terminal.ui.paper import positions as paper_positions
from aef_terminal.ui.paper import sync as paper_sync
from aef_terminal.ui.paper.execution import paper_order_execution_transition
from aef_terminal.engine.trade_setup import trade_setup_execution_authority


def ready_trade_setup_execution_authority() -> dict[str, object]:
    return trade_setup_execution_authority(
        ("trade_setup_engine",),
        enabled={"trade_setup_engine": True},
        indicator_results={
            "trade_setup_engine": {
                "status": {
                    "state_code": "no_signal",
                    "health": "ok",
                }
            }
        },
    )


def _trade_setup() -> dict[str, Any]:
    return {
        "ok": True,
        "action": "GO",
        "side": "long",
        "kind": "transit",
        "signal_source": "breakout_accumulation",
        "quality": 82.0,
        "plan": {"entry": 100.0, "stop": 98.0, "target": 104.0},
        "trigger_event": {"code": "transit_confirmed"},
        "execution_authority": ready_trade_setup_execution_authority(),
    }


def _sync(**overrides: Any) -> dict[str, Any]:
    store = overrides.pop("store", object())
    kwargs: dict[str, Any] = {
        "symbol": "BTC-USD",
        "instrument_id": "coinbase|contract|BTC-USD",
        "route_fingerprint": "coinbase|contract|BTC-USD",
        "provider": "coinbase",
        "provider_contract_id": "BTC-USD",
        "timeframe": "5m",
        "trade_setup": _trade_setup(),
        "bar_ts": "2026-07-26T12:00:00+00:00",
        "analysis_generation": "window-hash-1",
        "config": {"edge_gate": False, "min_rr": 1.25},
        "paper_order_execution_transition": lambda *_args, **_kwargs: {
            "outcome": "price_unavailable",
            "order": {},
            "code": "PAPER_EXECUTION_PRICE_UNAVAILABLE",
            "message": "screen execution price is not available",
            "execution": {},
        },
        "paper_order_price_snapshot": lambda _order: {
            "price": 100.0,
            "ts": "2026-07-26T12:00:00+00:00",
            "source": "chart-canonical-commit",
        },
    }
    kwargs.update(overrides)
    return paper_sync.paper_sync_from_trade_setup(store, **kwargs)


def _compact_analysis_snapshot() -> dict[str, Any]:
    return {
        "meta": {
            "symbol": "BTC-USD",
            "instrument_id": "coinbase|contract|BTC-USD",
            "route_fingerprint": "coinbase|contract|BTC-USD",
            "provider": "coinbase",
            "provider_contract_id": "BTC-USD",
            "timeframe": "5m",
            "analysis_latest_ts": "2026-07-26T12:00:00+00:00",
            "analysis_window_hash": "a" * 32,
            "analysis_bar_count": 120,
        },
        "trade_setup": _trade_setup(),
    }


def test_analysis_snapshot_sync_consumes_exact_compaction_identity(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    def capture_sync(_store: object, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"opened": 0}

    monkeypatch.setattr(
        paper_sync,
        "_DEPS",
        paper_sync.PaperSyncDeps(
            store_factory=object,
            paper_config=lambda: {"edge_gate": False},
            paper_order_execution_transition=lambda *_args, **_kwargs: {},
            paper_order_price_snapshot=lambda _order: None,
        ),
    )
    monkeypatch.setattr(paper_sync, "paper_sync_from_trade_setup", capture_sync)

    result = paper_sync.paper_sync_from_analysis_snapshot(_compact_analysis_snapshot())

    assert result == {"opened": 0}
    assert calls[0]["bar_ts"] == "2026-07-26T12:00:00+00:00"
    assert calls[0]["analysis_generation"] == "a" * 32


def test_analysis_snapshot_sync_does_not_reconstruct_missing_compaction_identity(
    monkeypatch: Any,
) -> None:
    snapshot = _compact_analysis_snapshot()
    snapshot["meta"].pop("analysis_latest_ts")
    snapshot["meta"].pop("analysis_window_hash")
    snapshot["meta"].pop("analysis_bar_count")
    snapshot["bars"] = [
        SimpleNamespace(ts="2026-07-26T12:00:00+00:00"),
        {"ts": "2026-07-26T12:05:00+00:00"},
    ]
    monkeypatch.setattr(paper_sync, "_DEPS", None)

    assert paper_sync.paper_sync_from_analysis_snapshot(snapshot) is None


def test_auto_sync_preserves_typed_execution_rejection(
    monkeypatch: Any,
) -> None:
    received_sources: list[str] = []

    def reject_signal(*_args: Any, raw: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        received_sources.append(str(raw.get("signal_source") or ""))
        return {
            "opened": False,
            "rejected": True,
            "code": "PAPER_EXECUTION_PLAN_REJECTED",
            "reason": "stop 101 through fill 100",
            "execution": {
                "planned_entry": 102.0,
                "fill_price": 100.0,
                "source": "chart-canonical-commit",
            },
        }

    monkeypatch.setattr(
        paper_sync,
        "paper_open_signal_position",
        reject_signal,
    )

    result = _sync()

    assert result["rejected"] == 1
    assert result["opened"] == 0
    assert received_sources == ["breakout_accumulation"]
    assert result["outcomes"] == [
        {
            "status": "rejected",
            "code": "PAPER_EXECUTION_PLAN_REJECTED",
            "reason": "stop 101 through fill 100",
            "order_id": "",
            "execution": {
                "planned_entry": 102.0,
                "fill_price": 100.0,
                "source": "chart-canonical-commit",
            },
        }
    ]


def test_auto_sync_reports_invalid_go_intent_instead_of_silently_dropping_it() -> None:
    invalid_setup = {
        **_trade_setup(),
        "trigger_event": None,
    }

    result = _sync(trade_setup=invalid_setup)

    assert result["rejected"] == 1
    assert result["outcomes"][0]["status"] == "rejected"
    assert result["outcomes"][0]["code"] == "PAPER_SIGNAL_INTENT_INVALID"
    assert result["outcomes"][0]["execution"] == {
        "analysis_bar_ts": "2026-07-26T12:00:00+00:00",
        "analysis_generation": "window-hash-1",
    }


def test_auto_sync_rejects_go_without_explicit_analysis_generation() -> None:
    result = _sync(bar_ts="", analysis_generation="")

    assert result["rejected"] == 1
    assert result["outcomes"][0] == {
        "status": "rejected",
        "code": "PAPER_SIGNAL_GENERATION_INVALID",
        "reason": "trade setup analysis generation is unavailable",
        "order_id": "",
        "execution": {
            "analysis_bar_ts": "",
            "analysis_generation": "",
        },
    }


def test_auto_sync_canonicalizes_equivalent_generation_timestamps(
    monkeypatch: Any,
) -> None:
    generations: list[str] = []

    def capture_generation(
        *_args: Any,
        raw: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        generations.append(str(raw.get("analysis_bar_ts") or ""))
        return {
            "opened": False,
            "rejected": True,
            "code": "PAPER_EXECUTION_PLAN_REJECTED",
            "reason": "test rejection",
        }

    monkeypatch.setattr(
        paper_sync,
        "paper_open_signal_position",
        capture_generation,
    )

    _sync(bar_ts="2026-07-26T12:00:00Z")
    _sync(bar_ts="2026-07-26T12:00:00+00:00")

    assert generations == [
        "2026-07-26T12:00:00+00:00",
        "2026-07-26T12:00:00+00:00",
    ]


def test_auto_sync_preserves_filled_execution_metadata(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        paper_sync,
        "paper_open_signal_position",
        lambda *_args, **_kwargs: {
            "opened": True,
            "rejected": False,
            "outcome": "filled",
            "order": {"id": "sig-order-1"},
            "position": {"id": "position-1"},
            "fill": {"id": 1},
            "execution": {
                "analysis_bar_ts": "2026-07-26T12:00:00+00:00",
                "fill_price": 100.0,
            },
        },
    )
    monkeypatch.setattr(
        "aef_terminal.ui.paper.notify.paper_notify_fill_result",
        lambda *_args, **_kwargs: None,
    )

    result = _sync()

    assert result["opened"] == 1
    assert result["rejected"] == 0
    assert result["outcomes"] == [
        {
            "status": "filled",
            "code": "PAPER_POSITION_OPENED",
            "reason": "",
            "order_id": "sig-order-1",
            "execution": {
                "analysis_bar_ts": "2026-07-26T12:00:00+00:00",
                "fill_price": 100.0,
            },
        }
    ]


def test_auto_sync_executes_same_generation_plan_at_actual_fill(
    monkeypatch: Any,
) -> None:
    applied: list[dict[str, Any]] = []

    class Store:
        @staticmethod
        def read_paper_positions(**_kwargs: Any) -> list[dict[str, Any]]:
            return []

        @staticmethod
        def read_paper_order(*_args: Any, **_kwargs: Any) -> None:
            return None

        @staticmethod
        def upsert_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            return dict(order)

        @staticmethod
        def apply_filled_paper_order(
            order: dict[str, Any],
        ) -> dict[str, Any]:
            applied.append(dict(order))
            return {
                "outcome": "filled",
                "order": dict(order),
                "position": {
                    "id": "position-1",
                    "status": "open",
                },
                "fill": {"id": 1},
                "created_orders": [],
                "cancelled_orders": [],
                "closed_position": None,
                "opened_new_position": True,
            }

    monkeypatch.setattr(
        "aef_terminal.ui.paper.notify.paper_notify_fill_result",
        lambda *_args, **_kwargs: None,
    )

    result = _sync(
        store=Store(),
        paper_order_execution_transition=paper_order_execution_transition,
        paper_order_price_snapshot=lambda _order: {
            "price": 100.5,
            "bid": 100.4,
            "ask": 100.5,
            "bid_ask_status": "live",
            "bbo_ts": "2026-07-26T12:00:00+00:00",
            "range_kind": "quote",
            "ts": "2026-07-26T12:00:00+00:00",
            "source": "coinbase:quote-live",
        },
    )

    assert result["opened"] == 1
    assert result["rejected"] == 0
    assert len(applied) == 1
    assert applied[0]["entry"] == 100.0
    assert applied[0]["fill_price"] == 100.5
    assert applied[0]["stop_loss"] == 98.0
    assert applied[0]["target"] == 104.0
    assert applied[0]["payload"]["planned_entry"] == 100.0
    assert result["outcomes"][0]["execution"] == {
        "fill_price": 100.5,
        "source": "coinbase:quote-live",
        "ts": "2026-07-26T12:00:00+00:00",
        "bar_slot": None,
        "planned_entry": 100.0,
        "analysis_bar_ts": "2026-07-26T12:00:00+00:00",
        "analysis_generation": "window-hash-1",
    }


def test_auto_sync_rejects_stale_execution_generation_after_reservation() -> None:
    applied: list[dict[str, Any]] = []
    rejected: list[str] = []

    class Store:
        @staticmethod
        def read_paper_positions(**_kwargs: Any) -> list[dict[str, Any]]:
            return []

        @staticmethod
        def read_paper_order(*_args: Any, **_kwargs: Any) -> None:
            return None

        @staticmethod
        def upsert_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            return dict(order)

        @staticmethod
        def apply_filled_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            applied.append(order)
            raise AssertionError("stale execution must not reach storage")

        @staticmethod
        def reject_paper_order_execution_if_pending(
            order_id: str,
            _at: Any,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            rejected.append(order_id)
            return {"id": order_id, "status": "cancelled"}

    result = _sync(
        store=Store(),
        paper_order_execution_transition=paper_order_execution_transition,
        paper_order_price_snapshot=lambda _order: {
            "price": 100.0,
            "ts": "2026-07-26T11:55:00+00:00",
            "source": "chart-canonical-commit",
        },
    )

    assert applied == []
    assert len(rejected) == 1
    assert result["opened"] == 0
    assert result["rejected"] == 1
    assert result["outcomes"][0]["code"] == "PAPER_EXECUTION_SNAPSHOT_STALE"


def test_rejected_flip_does_not_cancel_existing_position_protection(
    monkeypatch: Any,
) -> None:
    side_effects: list[str] = []
    instrument_id = "coinbase|contract|BTC-USD"
    route_fingerprint = "coinbase|route|BTC-USD"

    class Store:
        @staticmethod
        def cancel_paper_protective_orders(*_args: Any, **_kwargs: Any) -> None:
            side_effects.append("cancel")

        @staticmethod
        def apply_filled_paper_order(_order: dict[str, Any]) -> dict[str, Any]:
            side_effects.append("fill")
            raise AssertionError("rejected flip must not reach storage")

        @staticmethod
        def upsert_paper_order(order: dict[str, Any]) -> dict[str, Any]:
            side_effects.append("reserve")
            return dict(order)

        @staticmethod
        def reject_paper_order_execution_if_pending(
            _order_id: str,
            _at: Any,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            side_effects.append("reject")
            return {
                "id": "sig-flip-rejected",
                "instrument_id": instrument_id,
                "route_fingerprint": route_fingerprint,
                "status": "cancelled",
            }

    monkeypatch.setattr(
        "aef_terminal.ui.runtime_instruments.lookup_runtime_instrument",
        lambda _instrument_id: {"instrument_id": instrument_id},
    )
    monkeypatch.setattr(
        "aef_terminal.data.providers.route_instrument",
        lambda _instrument: SimpleNamespace(fingerprint=route_fingerprint),
    )
    monkeypatch.setattr(
        paper_positions,
        "resolve_instrument_profile",
        lambda _instrument: SimpleNamespace(key="crypto"),
    )
    monkeypatch.setattr(
        paper_positions,
        "paper_flip_rank_allows",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        paper_positions,
        "paper_order_payload_from_signal",
        lambda *_args, **_kwargs: {
            "id": "sig-flip-rejected",
            "symbol": "BTC-USD",
            "instrument_id": instrument_id,
            "route_fingerprint": route_fingerprint,
            "provider": "coinbase",
            "provider_contract_id": "BTC-USD",
            "timeframe": "5m",
            "side": "long",
            "order_type": "market",
            "qty": 2.0,
            "entry": 100.0,
            "stop_loss": 98.0,
            "target": 104.0,
            "payload": {"protection_basis": "absolute_structure"},
        },
    )

    result = paper_positions.paper_flip_signal_position(
        Store(),
        symbol="BTC-USD",
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        provider="coinbase",
        provider_contract_id="BTC-USD",
        timeframe="5m",
        existing={
            "id": "position-existing",
            "qty": -1.0,
            "updated_at": "2026-07-26T11:59:00+00:00",
            "payload": {
                "side": "short",
                "indicator_source": "trade_setup",
            },
        },
        raw={"side": "long"},
        config={"min_rr": 1.25, "edge_gate": True},
        price_snapshot={
            "price": 100.0,
            "ts": "2026-07-26T12:00:00+00:00",
            "source": "chart-canonical-commit",
        },
        paper_order_execution_transition=lambda *_args, **_kwargs: {
            "outcome": "rejected",
            "order": {},
            "code": "PAPER_EXECUTION_PLAN_REJECTED",
            "message": "unsafe flip",
            "execution": {},
        },
    )

    assert result["code"] == "PAPER_EXECUTION_PLAN_REJECTED"
    assert result["reason"] == "unsafe flip"
    assert side_effects == ["reserve", "reject"]
