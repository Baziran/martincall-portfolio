from aef_terminal.engine.execution_intent import (
    execution_intent_from_trade_setup,
    trade_plan_projection_from_trade_setup,
)
from aef_terminal.engine.trade_setup import trade_setup_execution_authority


def _ready_execution_authority() -> dict[str, object]:
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


_BAR_TS = "2026-07-20T10:00:00+00:00"
_GENERATION = "window-hash-1"


def _intent(setup, *, bar_ts: str = _BAR_TS, generation: str = _GENERATION):
    return execution_intent_from_trade_setup(
        setup,
        bar_ts=bar_ts,
        analysis_generation=generation,
    )


def test_execution_intent_is_canonical_for_trade_setup_consumers() -> None:
    setup = {
        "ok": True,
        "action": "GO",
        "side": "long",
        "kind": "transit",
        "signal_source": "weighted_winner",
        "quality": 82.0,
        "plan": {"entry": 100.0, "stop": 98.0, "target": 104.0},
        "trigger_event": {"code": "transit_confirmed"},
        "execution_authority": _ready_execution_authority(),
        "confluence": [
            {"source": "decision", "score": 80.0},
            {
                "source": "impulse_pullback",
                "score": 84.0,
                "trigger_event": {"code": "pullback_confirmed"},
            },
        ],
    }

    intent = execution_intent_from_trade_setup(
        setup,
        bar_ts="2026-07-20T10:00:00+00:00",
        analysis_generation="window-hash-1",
    )

    assert intent is not None
    assert intent["source"] == "trade_setup"
    assert intent["signal_source"] == "weighted_winner"
    assert intent["signal_source_label"] == "weighted_winner"
    assert intent["side"] == "long"
    assert intent["entry"] == 100.0
    assert intent["planned_entry"] == 100.0
    assert intent["analysis_bar_ts"] == "2026-07-20T10:00:00+00:00"
    assert intent["analysis_generation"] == "window-hash-1"
    assert intent["protection_basis"] == "absolute_structure"
    assert intent["plan"] == setup["plan"]
    assert _intent({**setup, "quality": None}) is None
    assert _intent({**setup, "confluence": [{"source": "decision", "score": 80.0}]}) is not None
    assert _intent({**setup, "signal_source": ""}) is None
    assert _intent({**setup, "signal_source": " weighted_winner"}) is None


def test_execution_intent_allows_finite_signed_absolute_prices() -> None:
    setup = {
        "ok": True,
        "action": "GO",
        "side": "long",
        "kind": "transit",
        "signal_source": "cross_zero",
        "quality": 80.0,
        "plan": {"entry": 0.0, "stop": -2.0, "target": 2.0},
        "trigger_event": {"code": "cross_zero_breakout"},
        "execution_authority": _ready_execution_authority(),
    }

    intent = _intent(setup)

    assert intent is not None
    assert intent["entry"] == 0.0


def test_execution_intent_rejects_non_go_and_incoherent_plan() -> None:
    base = {
        "ok": True,
        "action": "WAIT",
        "side": "short",
        "kind": "fade",
        "signal_source": "fade_source",
        "plan": {"entry": 100.0, "stop": 102.0, "target": 96.0},
        "execution_authority": _ready_execution_authority(),
    }
    assert _intent(base) is None
    assert (
        _intent(
            {
                **base,
                "action": "GO",
                "quality": 80.0,
                "trigger_event": {"code": "bad_short_plan"},
                "plan": {"entry": 100.0, "stop": 98.0, "target": 96.0},
            }
        )
        is None
    )


def test_execution_intent_rejects_display_title_without_typed_kind() -> None:
    setup = {
        "ok": True,
        "action": "GO",
        "side": "long",
        "title": "Momentum breakout",
        "plan": {"entry": 100.0, "stop": 98.0, "target": 104.0},
        "execution_authority": _ready_execution_authority(),
    }

    assert _intent(setup) is None


def test_execution_intent_requires_exact_ready_trade_setup_authority() -> None:
    setup = {
        "ok": True,
        "action": "GO",
        "side": "long",
        "kind": "transit",
        "signal_source": "authority_source",
        "quality": 80.0,
        "plan": {"entry": 100.0, "stop": 98.0, "target": 104.0},
        "trigger_event": {"code": "transit_confirmed"},
    }

    assert _intent(setup) is None
    for field, invalid_value in (
        ("contract", "trade-setup-authority-v0"),
        ("state", "error"),
        ("ready", False),
        ("provider_id", ""),
        ("provider_ids", []),
        ("reason_code", "trade_setup_authority_error"),
    ):
        authority = _ready_execution_authority()
        authority[field] = invalid_value
        assert _intent({**setup, "execution_authority": authority}) is None


def test_trade_plan_projection_has_no_execution_time_backdoor() -> None:
    setup = {
        "ok": True,
        "action": "GO",
        "side": "short",
        "kind": "fade",
        "signal_source": "smc_structure",
        "quality": 81.0,
        "plan": {"entry": 100.0, "stop": 102.0, "target": 96.0},
        "trigger_event": {"code": "fade_confirmed"},
        "execution_authority": _ready_execution_authority(),
    }

    projection = trade_plan_projection_from_trade_setup(setup)

    assert projection is not None
    assert projection.as_backtest_plan() == {
        "source": "smc_structure",
        "setup": "fade",
        "side": "short",
        "confidence": 81.0,
        "entry": 100.0,
        "stop": 102.0,
        "target": 96.0,
    }
    assert not hasattr(projection, "analysis_bar_ts")
    assert not hasattr(projection, "analysis_generation")


def test_execution_intent_rejects_missing_identity_and_coerced_numbers() -> None:
    setup = {
        "ok": True,
        "action": "GO",
        "side": "long",
        "kind": "transit",
        "signal_source": "transit_source",
        "quality": 80.0,
        "plan": {"entry": 100.0, "stop": 98.0, "target": 104.0},
        "trigger_event": {"code": "transit_confirmed"},
        "execution_authority": _ready_execution_authority(),
    }

    assert _intent(setup, bar_ts="", generation=_GENERATION) is None
    assert _intent(setup, bar_ts="2026-07-20T10:00:00", generation=_GENERATION) is None
    assert _intent(setup, generation="") is None
    assert _intent({**setup, "quality": "80"}) is None
    assert _intent({**setup, "plan": {"entry": "100", "stop": 98.0, "target": 104.0}}) is None
