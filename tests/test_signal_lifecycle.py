from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
)
from aef_terminal.engine.analyze import bars as analyze_bars_module
from aef_terminal.engine.decision import DecisionContext
from aef_terminal.engine.signal_lifecycle import (
    active_signal_candidates,
    annotate_signal_candidate_lifecycles,
    expire_indicator_lifecycles,
    expire_lifecycle_payload,
    trade_plan_close_event,
)
from aef_terminal.runtime.signal_state import (
    LifecycleCloseEvent,
    LifecycleState,
    PlanLifecycle,
    active_lifecycle_from_indicators,
    lifecycle_state_from_value,
    plan_lifecycle_from_wire,
    resolve_plan_bar_close_event,
)
from tests.provider_payloads import coinbase_btc_payload


def _bar(index: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        symbol="BTC",
        ts=datetime(2026, 7, 4, 12, 0, tzinfo=UTC) + timedelta(minutes=5 * index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
        timeframe="5m",
        source="coinbase:test",
    )


def test_lifecycle_wire_contract_accepts_only_canonical_values() -> None:
    assert lifecycle_state_from_value("TP") is LifecycleState.TARGET_HIT
    assert lifecycle_state_from_value("STOP") is LifecycleState.STOP_HIT
    assert lifecycle_state_from_value("TARGET_HIT") is None
    assert lifecycle_state_from_value("SL") is None


def test_lifecycle_close_event_serializes_enum_at_wire_boundary() -> None:
    event = LifecycleCloseEvent(
        state=LifecycleState.TARGET_HIT,
        exit_price=104.123456,
        closed_ts="2026-07-25T13:35:00+00:00",
    )

    assert event.as_dict() == {
        "state": "TP",
        "exit_reason": "TP",
        "exit_price": 104.1235,
        "closed_ts": "2026-07-25T13:35:00+00:00",
    }


def test_active_lifecycle_requires_score_from_latest_signal() -> None:
    lifecycle = {
        "active": True,
        "state": "FOLLOW",
        "direction": "long",
        "entry": 100.0,
        "stop": 99.0,
        "target": 103.0,
        "filled": True,
    }

    assert (
        active_lifecycle_from_indicators(
            {
                "impulse_fib": {
                    "latest": {
                        "score": 99.0,
                        "signal": {},
                        "lifecycle": lifecycle,
                    }
                }
            }
        )
        is None
    )


def test_canonical_bar_close_resolver_preserves_dual_hit_ambiguity() -> None:
    bar = _bar(1, 100, 105, 95, 100)

    close_event = resolve_plan_bar_close_event(
        direction=Direction.LONG,
        stop=98,
        target=104,
        bar=bar,
    )

    assert close_event is not None
    assert close_event.state is LifecycleState.TARGET_AND_STOP_HIT
    assert close_event.exit_price is None
    assert close_event.closed_ts == bar.ts.isoformat()


def test_canonical_bar_close_resolver_types_gap_without_exact_fill() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 97, 97.5, 95, 96),
    ]

    close_event = trade_plan_close_event(
        direction=Direction.LONG,
        entry=100,
        stop=98,
        target=104,
        bars=bars,
        opened_ts=bars[0].ts.isoformat(),
    )

    assert close_event is not None
    assert close_event.state is LifecycleState.STOP_HIT
    assert close_event.exit_price is None
    assert close_event.closed_ts == bars[1].ts.isoformat()


def test_lifecycle_wire_decoder_rejects_contradictory_contracts() -> None:
    lifecycle = PlanLifecycle(
        source="unit",
        state=LifecycleState.FOLLOW,
        direction=Direction.LONG,
        entry=100,
        stop=98,
        target=104,
        filled=True,
    )
    active = lifecycle.as_dict()

    admitted = plan_lifecycle_from_wire(active)

    assert admitted == lifecycle
    assert plan_lifecycle_from_wire({**active, "state": "TP", "exit_reason": "TP"}) is None
    assert plan_lifecycle_from_wire({**active, "active": False}) is None
    assert plan_lifecycle_from_wire({**active, "filled": False}) is None
    assert plan_lifecycle_from_wire({**active, "state": "WAIT_ENTRY", "filled": True}) is None
    assert (
        plan_lifecycle_from_wire({**active, "active": False, "state": "TP", "exit_reason": "STOP"})
        is None
    )
    assert plan_lifecycle_from_wire({**active, "direction": "flat"}) is None
    assert plan_lifecycle_from_wire({**active, "entry": "100"}) is None
    assert plan_lifecycle_from_wire({**active, "opened_ts": "2026-07-25T13:30:00"}) is None
    assert plan_lifecycle_from_wire({**active, "contract_invalid": True}) is None
    assert plan_lifecycle_from_wire({**active, "contract_invalid": "false"}) is None


def test_invalid_lifecycle_payload_is_quarantined_fail_closed() -> None:
    payload = {
        "active": True,
        "state": "TP",
        "exit_reason": "TP",
        "direction": "long",
        "entry": 100,
        "stop": 98,
        "target": 104,
        "filled": True,
        "filled_ts": "2026-07-25T13:30:00+00:00",
        "closed_ts": "2026-07-25T13:35:00+00:00",
        "exit_price": 104,
    }

    quarantined = expire_lifecycle_payload(payload, [_bar(0, 100, 101, 99, 100)])

    assert quarantined["active"] is False
    assert quarantined["obsolete"] is True
    assert quarantined["contract_invalid"] is True
    assert quarantined["obsolete_reason"] == "contract_invalid"
    assert quarantined["state"] == "WAIT"
    assert quarantined["exit_reason"] == ""
    assert quarantined["filled"] is False
    assert quarantined["filled_ts"] is None
    assert quarantined["closed_ts"] is None
    assert quarantined["exit_price"] is None
    assert quarantined["trade_plan"]["state"] == "WAIT"
    assert quarantined["trade_plan"]["action"] == "WAIT"
    assert quarantined["trade_plan"]["actionable"] is False
    assert plan_lifecycle_from_wire(quarantined) is None


def test_expired_lifecycle_reserializes_nested_trade_plan() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 105, 99, 104),
    ]
    active = {
        "source": "unit",
        "state": "FOLLOW",
        "active": True,
        "direction": "long",
        "entry": 100,
        "stop": 98,
        "target": 104,
        "opened_ts": bars[0].ts.isoformat(),
        "filled_ts": bars[0].ts.isoformat(),
        "filled": True,
        "exit_reason": "",
        "trade_plan": {
            "state": "FOLLOW",
            "action": "FOLLOW",
            "actionable": True,
        },
    }

    closed = expire_lifecycle_payload(active, bars)

    assert closed["state"] == "TP"
    assert closed["exit_reason"] == "TP"
    assert closed["active"] is False
    assert closed["trade_plan"]["state"] == "TP"
    assert closed["trade_plan"]["action"] == "TP"
    assert closed["trade_plan"]["actionable"] is False
    admitted = plan_lifecycle_from_wire(closed)
    assert admitted is not None
    assert admitted.state is LifecycleState.TARGET_HIT


def test_invalid_indicator_lifecycle_obsoletes_signal_without_raising() -> None:
    bars = [_bar(0, 100, 101, 99, 100)]
    indicators = {
        "unit": {
            "latest": {
                "signal": {
                    "action": "GO",
                    "trigger": 100,
                    "stop": 98,
                    "target": 104,
                },
                "lifecycle": {
                    "active": False,
                    "state": "SL",
                    "exit_reason": "SL",
                    "direction": "long",
                    "entry": 100,
                    "stop": 98,
                    "target": 104,
                    "obsolete": True,
                },
            },
            "status": {"state_code": "signal", "health": "signal", "has_signal": True},
        }
    }

    expire_indicator_lifecycles(indicators, bars)

    latest = indicators["unit"]["latest"]
    assert latest["lifecycle"]["active"] is False
    assert latest["lifecycle"]["contract_invalid"] is True
    assert latest["signal"]["action"] == "WAIT"
    assert latest["signal"]["obsolete"] is True
    assert latest["signal"]["blocked_reason"] == "obsolete_contract_invalid"
    assert latest["signal"]["lifecycle"]["direction"] == "flat"
    assert latest["obsolete_reason"] == "contract_invalid"
    assert indicators["unit"]["status"]["state_code"] == "obsolete"
    assert indicators["unit"]["status"]["trigger_event"] == {
        "code": "signal_obsolete",
        "exit_reason": "contract_invalid",
    }


def test_signal_candidate_obsoletes_after_target_hit_after_signal_bar() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 103, 99, 102),
        _bar(2, 102, 105, 101, 104),
    ]
    candidate = SignalCandidate(
        "unit_long",
        Direction.LONG,
        82,
        100,
        "long plan",
        DomainFact("unit_long_plan"),
        details={"trigger": 100, "stop": 98, "target": 104, "ts": bars[0].ts.isoformat()},
        finality=CandidateFinality.CONFIRMED,
    )

    annotated = annotate_signal_candidate_lifecycles([candidate], bars)

    assert active_signal_candidates(annotated) == []
    assert annotated[0].details["obsolete"] is True
    assert annotated[0].details["obsolete_reason"] == "tp"
    assert annotated[0].details["lifecycle"]["state"] == "TP"
    assert annotated[0].details["lifecycle"]["closed_ts"] == bars[2].ts.isoformat()


def test_signal_candidate_does_not_obsolete_from_same_bar_high_before_signal_exists() -> None:
    bars = [_bar(0, 100, 105, 99, 102)]
    candidate = SignalCandidate(
        "unit_long",
        Direction.LONG,
        82,
        102,
        "fresh close signal",
        DomainFact("fresh_close_signal"),
        details={"trigger": 102, "stop": 98, "target": 104, "ts": bars[0].ts.isoformat()},
        finality=CandidateFinality.CONFIRMED,
    )

    annotated = annotate_signal_candidate_lifecycles([candidate], bars)

    assert active_signal_candidates(annotated) == annotated
    assert "obsolete" not in annotated[0].details


def test_indicator_latest_signal_and_status_share_obsolete_lifecycle_outcome() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 105, 99, 104),
    ]
    indicators = {
        "linda_volume": {
            "latest": {
                "ts": bars[0].ts.isoformat(),
                "signal": {
                    "action": "GO",
                    "direction": "long",
                    "trigger": 100,
                    "stop": 98,
                    "target": 104,
                    "score": 89,
                },
                "lifecycle": {
                    "active": True,
                    "state": "FOLLOW",
                    "direction": "long",
                    "entry": 100,
                    "stop": 98,
                    "target": 104,
                    "opened_ts": bars[0].ts.isoformat(),
                    "filled": True,
                },
            },
            "status": {"state_code": "signal", "health": "signal", "has_signal": True},
        }
    }

    expire_indicator_lifecycles(indicators, bars)

    latest = indicators["linda_volume"]["latest"]
    status = indicators["linda_volume"]["status"]
    assert latest["signal"]["action"] == "WAIT"
    assert latest["signal"]["obsolete"] is True
    assert latest["lifecycle"]["active"] is False
    assert latest["lifecycle"]["obsolete"] is True
    assert latest["lifecycle"]["state"] == "TP"
    assert status["state_code"] == "obsolete"
    assert status["reason_code"] == "signal_obsolete"
    assert status["trigger_event"] == {"code": "signal_obsolete", "exit_reason": "tp"}
    assert "state" not in status
    assert "message" not in status
    assert status["has_signal"] is False
    assert status["obsolete_signal"] is True


def test_indicator_lifecycle_obsolete_marks_latest_signal_without_signal_timestamp() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 101, 95, 96),
        _bar(2, 96, 100, 95, 99),
    ]
    indicators = {
        "trade_setup_engine": {
            "latest": {
                "ts": bars[-1].ts.isoformat(),
                "signal": {
                    "action": "ARM",
                    "direction": "short",
                    "trigger": 100,
                    "stop": 104,
                    "target": 96,
                    "score": 80,
                    "trade_plan": {
                        "direction": "short",
                        "entry": 100,
                        "stop": 104,
                        "target": 96,
                    },
                },
                "lifecycle": {
                    "active": True,
                    "state": "WAIT_ENTRY",
                    "direction": "short",
                    "entry": 100,
                    "stop": 104,
                    "target": 96,
                    "opened_ts": bars[0].ts.isoformat(),
                    "filled": False,
                },
            },
            "status": {"state_code": "signal", "health": "signal", "has_signal": True},
        }
    }

    expire_indicator_lifecycles(indicators, bars)

    latest = indicators["trade_setup_engine"]["latest"]
    assert latest["lifecycle"]["obsolete"] is True
    assert latest["lifecycle"]["state"] == "TP"
    assert latest["signal"]["obsolete"] is True
    assert latest["signal"]["action"] == "WAIT"
    assert latest["signal"]["blocked_reason"] == "obsolete_tp"
    assert indicators["trade_setup_engine"]["status"]["state_code"] == "obsolete"
    assert indicators["trade_setup_engine"]["status"]["reason_code"] == "signal_obsolete"


def test_indicator_closed_inactive_lifecycle_marks_latest_signal_obsolete() -> None:
    bars = [
        _bar(0, 100, 101, 99, 100),
        _bar(1, 100, 102, 95, 96),
    ]
    indicators = {
        "smc_channels": {
            "latest": {
                "signal": {
                    "action": "GO",
                    "direction": "short",
                    "trigger": 100,
                    "stop": 104,
                    "target": 96,
                    "score": 82,
                },
                "lifecycle": {
                    "active": False,
                    "state": "TP",
                    "exit_reason": "TP",
                    "direction": "short",
                    "entry": 100,
                    "stop": 104,
                    "target": 96,
                    "closed_ts": bars[1].ts.isoformat(),
                    "exit_price": 96,
                },
            },
            "status": {"state_code": "signal", "health": "signal", "has_signal": True},
        }
    }

    expire_indicator_lifecycles(indicators, bars)

    latest = indicators["smc_channels"]["latest"]
    assert latest["lifecycle"]["obsolete"] is True
    assert latest["signal"]["obsolete"] is True
    assert latest["signal"]["action"] == "WAIT"
    assert latest["signal"]["blocked_reason"] == "obsolete_tp"
    assert indicators["smc_channels"]["status"]["state_code"] == "obsolete"
    assert indicators["smc_channels"]["status"]["reason_code"] == "signal_obsolete"


def test_indicator_lifecycle_preserves_zero_price_stop_before_invalidation_fallback() -> None:
    bars = [
        _bar(0, 1, 1.5, 0.5, 1),
        _bar(1, 1, 2.5, 0, 2),
    ]
    indicators = {
        "smc_channels": {
            "latest": {
                "signal": {
                    "action": "GO",
                    "direction": "long",
                    "ts": bars[0].ts.isoformat(),
                    "trigger": 1,
                    "stop": 0,
                    "invalidation": 2,
                    "target": 2,
                    "score": 82,
                },
            },
            "status": {"state_code": "signal", "health": "signal", "has_signal": True},
        }
    }

    expire_indicator_lifecycles(indicators, bars)

    lifecycle = indicators["smc_channels"]["latest"]["signal"]["lifecycle"]
    assert lifecycle["stop"] == 0
    assert lifecycle["state"] == "TP/SL"


def test_analyze_bars_excludes_closed_plan_from_decision_surfaces(monkeypatch) -> None:
    bars = [
        _bar(index, 100 + index * 0.02, 101 + index * 0.02, 99 + index * 0.02, 100 + index * 0.02)
        for index in range(40)
    ]
    bars[10] = _bar(10, 100, 101, 99, 100)
    bars[11] = _bar(11, 100, 104.5, 99, 104)
    bars[12] = _bar(12, 104, 105, 103, 104.5)
    stale_candidate = SignalCandidate(
        "unit_long",
        Direction.LONG,
        90,
        100,
        "old long",
        DomainFact("old_long_plan"),
        details={"trigger": 100, "stop": 98, "target": 104, "ts": bars[10].ts.isoformat()},
        finality=CandidateFinality.CONFIRMED,
    )

    monkeypatch.setattr(
        analyze_bars_module,
        "_calculate_primary_signal_indicators",
        lambda **_kwargs: (
            {},
            [stale_candidate],
            {"series": [], "events": [], "overlays": [], "status": {}},
            None,
            {},
        ),
    )
    monkeypatch.setattr(
        analyze_bars_module,
        "_calculate_structure_signal_indicators",
        lambda **_kwargs: ({}, []),
    )
    monkeypatch.setattr(
        analyze_bars_module,
        "_calculate_setup_stage_indicators",
        lambda **_kwargs: ({}, []),
    )
    monkeypatch.setattr(
        analyze_bars_module,
        "_calculate_post_decision_indicators",
        lambda **_kwargs: {},
    )
    decision_candidates_seen: list[SignalCandidate] = []
    trade_setup_candidates_seen: list[SignalCandidate] = []
    sentiment_candidates_seen: list[SignalCandidate] = []

    def fake_choose_decision(context: DecisionContext):
        decision_candidates_seen.extend(context.candidates)
        return ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0.0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("no_candidate"),
            [],
        )

    def fake_trade_setup_card(*, candidates, **_kwargs):
        trade_setup_candidates_seen.extend(candidates)
        return {"action": "WAIT", "plan": {"mode": "basic"}}

    def fake_direction_sentiment(*, candidates, **_kwargs):
        sentiment_candidates_seen.extend(candidates)
        return {"direction": "flat", "score": 0}

    monkeypatch.setattr(analyze_bars_module, "choose_decision", fake_choose_decision)
    monkeypatch.setattr(analyze_bars_module, "build_trade_setup_card", fake_trade_setup_card)
    monkeypatch.setattr(analyze_bars_module, "build_direction_sentiment", fake_direction_sentiment)

    snapshot = analyze_bars_module.analyze_bars(
        bars,
        display_symbol="BTC",
        analysis_as_of_utc=bars[-1].ts + timedelta(seconds=1),
        instrument=coinbase_btc_payload(),
        data_provider="coinbase",
    )

    assert all(candidate.name != "unit_long" for candidate in decision_candidates_seen)
    assert all(candidate.name != "unit_long" for candidate in trade_setup_candidates_seen)
    assert all(candidate.name != "unit_long" for candidate in sentiment_candidates_seen)
    assert all(candidate["name"] != "unit_long" for candidate in snapshot["candidates"])
    assert snapshot["decision"]["direction"] == "flat"
    assert snapshot["decision"]["action"] == "WAIT"
    assert snapshot["trade_setup"]["action"] == "WAIT"
    assert snapshot["direction_sentiment"]["direction"] == "flat"
