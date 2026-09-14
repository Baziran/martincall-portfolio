from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest

from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
    StrategyMode,
)
from aef_terminal.engine.decision import DecisionContext, choose_decision
from aef_terminal.engine.analyze import analyze_bars
from aef_terminal.engine.snapshot.empty import empty_market_snapshot
from aef_terminal.engine.trade_setup import (
    TRADE_SETUP_AUTHORITY_CONTRACT,
    build_blocked_trade_setup_card,
    build_trade_setup_card,
    trade_setup_execution_authority,
    trade_setup_execution_authority_ready,
)
from aef_terminal.runtime.instruments import PROFILES
from tests.provider_payloads import (
    ibkr_stock_payload,
    instrument_with_bar_sessions,
)


_PROVIDER_ID = "trade_setup_engine"


def test_blocked_trade_setup_has_no_custom_action_backdoor() -> None:
    assert "action" not in signature(build_blocked_trade_setup_card).parameters


def _bar(index: int = 0, *, closed: bool = True) -> Bar:
    return Bar(
        symbol="SPY",
        ts=datetime(2026, 7, 20, 10, 0, tzinfo=UTC) + timedelta(minutes=5 * index),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000.0,
        timeframe="5m",
        source="ibkr:db-cache" if closed else "ibkr:db-cache+quote-live",
        closed=closed,
    )


def _healthy_result() -> dict[str, object]:
    return {
        "status": {
            "state_code": "no_signal",
            "health": "ok",
        }
    }


def _build_setup(execution_authority: dict[str, object]) -> dict[str, object]:
    return build_trade_setup_card(
        latest=_bar(),
        decision=ScenarioDecision(
            kind=ScenarioKind.WAIT,
            direction=Direction.FLAT,
            confidence=0.0,
            action=ActionPhase.WAIT,
            trigger=None,
            stop=None,
            target=None,
            invalidation=None,
            trigger_event=DomainFact("wait"),
            reasons=["wait"],
        ),
        candidates=[],
        indicators={},
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=1.0,
        price_increment=0.01,
        execution_authority=execution_authority,
    )


def test_trade_setup_authority_accepts_one_enabled_healthy_provider() -> None:
    authority = trade_setup_execution_authority(
        (_PROVIDER_ID,),
        enabled={_PROVIDER_ID: True},
        indicator_results={_PROVIDER_ID: _healthy_result()},
    )

    assert authority == {
        "contract": TRADE_SETUP_AUTHORITY_CONTRACT,
        "state": "ready",
        "ready": True,
        "provider_id": _PROVIDER_ID,
        "provider_ids": [_PROVIDER_ID],
        "reason_code": "trade_setup_authority_ready",
    }
    assert trade_setup_execution_authority_ready(authority) is True
    assert _build_setup(authority)["execution_authority"] == authority


def test_trade_setup_carries_weighted_decision_source_independently_of_confluence() -> None:
    authority = trade_setup_execution_authority(
        (_PROVIDER_ID,),
        enabled={_PROVIDER_ID: True},
        indicator_results={_PROVIDER_ID: _healthy_result()},
    )
    setup = build_trade_setup_card(
        latest=_bar(),
        decision=ScenarioDecision(
            kind=ScenarioKind.TRANSIT,
            direction=Direction.LONG,
            confidence=84.0,
            action=ActionPhase.GO,
            trigger=100.0,
            stop=98.0,
            target=104.0,
            invalidation=96.0,
            trigger_event=DomainFact("weighted_winner"),
            source="weighted_winner",
        ),
        candidates=(
            SignalCandidate(
                name="higher_raw_score_evidence",
                direction=Direction.LONG,
                score=95.0,
                level=100.0,
                reason="evidence",
                trigger_event=DomainFact("higher_raw_score_evidence"),
                kind=ScenarioKind.TRANSIT,
                finality=CandidateFinality.CONFIRMED,
            ),
        ),
        indicators={},
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=1.0,
        price_increment=0.01,
        execution_authority=authority,
    )

    assert setup["signal_source"] == "weighted_winner"
    assert setup["confluence"][1]["source"] == "higher_raw_score_evidence"
    assert setup["plan"]["stop"] == 97.65
    assert setup["plan"]["invalidation"] == 96.0


def test_decision_uses_exact_route_increment_instead_of_profile_tick_size() -> None:
    candidate = SignalCandidate(
        name="route_increment_probe",
        direction=Direction.LONG,
        score=95.0,
        level=100.03,
        reason="exact route price geometry",
        trigger_event=DomainFact("route_increment_probe"),
        kind=ScenarioKind.TRANSIT,
        source="route_increment_probe",
        finality=CandidateFinality.CONFIRMED,
    )
    decision = choose_decision(
        DecisionContext(
            candidates=(candidate,),
            bars=(_bar(0), _bar(1)),
            atr_value=1.0,
            strategy_mode=StrategyMode.BALANCED,
            option_flow=None,
            instrument_profile=PROFILES["ES"],
            price_increment=0.1,
        )
    )

    assert PROFILES["ES"].tick_size == 0.25
    assert decision.trigger == 100.0
    assert decision.stop == 98.2
    assert decision.target == 103.4
    assert decision.metrics["price_increment"] == 0.1


def test_missing_exact_price_increment_blocks_decision_and_trade_setup() -> None:
    candidate = SignalCandidate(
        name="missing_increment_probe",
        direction=Direction.LONG,
        score=95.0,
        level=100.0,
        reason="missing increment",
        trigger_event=DomainFact("missing_increment_probe"),
        kind=ScenarioKind.TRANSIT,
        source="missing_increment_probe",
        finality=CandidateFinality.CONFIRMED,
    )
    decision = choose_decision(
        DecisionContext(
            candidates=(candidate,),
            bars=(_bar(0), _bar(1)),
            atr_value=1.0,
            strategy_mode=StrategyMode.BALANCED,
            option_flow=None,
            instrument_profile=PROFILES["ES"],
            price_increment=None,
        )
    )

    assert decision.action is ActionPhase.BLOCK
    assert decision.reason_codes[-1] == "price_increment_unavailable"

    authority = trade_setup_execution_authority(
        (_PROVIDER_ID,),
        enabled={_PROVIDER_ID: True},
        indicator_results={_PROVIDER_ID: _healthy_result()},
    )
    setup = build_trade_setup_card(
        latest=_bar(),
        decision=decision,
        candidates=(candidate,),
        indicators={},
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=1.0,
        price_increment=None,
        execution_authority=authority,
    )

    assert setup["action"] == "BLOCK"
    assert setup["blocked"] == ["price_increment_unavailable"]
    assert setup["execution_authority"]["ready"] is False


def test_missing_exact_price_increment_does_not_block_a_wait_candidate() -> None:
    decision = choose_decision(
        DecisionContext(
            candidates=(
                SignalCandidate(
                    name="wait_probe",
                    direction=Direction.FLAT,
                    score=95.0,
                    level=None,
                    reason="wait",
                    trigger_event=DomainFact("wait_probe"),
                    kind=ScenarioKind.WAIT,
                    source="wait_probe",
                    finality=CandidateFinality.CONFIRMED,
                ),
            ),
            bars=(_bar(0), _bar(1)),
            atr_value=1.0,
            strategy_mode=StrategyMode.BALANCED,
            option_flow=None,
            instrument_profile=PROFILES["ES"],
            price_increment=None,
        )
    )

    assert decision.action is ActionPhase.WAIT
    assert decision.reason_codes[-1] == "missing_actionable_scenario_kind"


@pytest.mark.parametrize(
    (
        "provider_ids",
        "enabled",
        "indicator_results",
        "expected_state",
        "expected_reason",
    ),
    [
        (
            (),
            {},
            {},
            "unavailable",
            "trade_setup_authority_unavailable",
        ),
        (
            (_PROVIDER_ID,),
            {_PROVIDER_ID: False},
            {_PROVIDER_ID: _healthy_result()},
            "disabled",
            "trade_setup_authority_disabled",
        ),
        (
            (_PROVIDER_ID,),
            {_PROVIDER_ID: True},
            {},
            "error",
            "trade_setup_authority_error",
        ),
        (
            (_PROVIDER_ID,),
            {_PROVIDER_ID: True},
            {
                _PROVIDER_ID: {
                    "status": {
                        "state_code": "error",
                        "health": "error",
                    }
                }
            },
            "error",
            "trade_setup_authority_error",
        ),
        (
            (_PROVIDER_ID, "second_trade_setup_provider"),
            {
                _PROVIDER_ID: True,
                "second_trade_setup_provider": True,
            },
            {
                _PROVIDER_ID: _healthy_result(),
                "second_trade_setup_provider": _healthy_result(),
            },
            "ambiguous",
            "trade_setup_authority_ambiguous",
        ),
    ],
    ids=(
        "provider_missing",
        "provider_disabled",
        "provider_result_missing",
        "provider_error",
        "provider_ambiguous",
    ),
)
def test_trade_setup_authority_blocks_non_authoritative_provider_state(
    provider_ids: tuple[str, ...],
    enabled: dict[str, bool],
    indicator_results: dict[str, object],
    expected_state: str,
    expected_reason: str,
) -> None:
    authority = trade_setup_execution_authority(
        provider_ids,
        enabled=enabled,
        indicator_results=indicator_results,
    )
    setup = _build_setup(authority)

    assert authority["state"] == expected_state
    assert authority["reason_code"] == expected_reason
    assert authority["ready"] is False
    assert trade_setup_execution_authority_ready(authority) is False
    assert setup["ok"] is False
    assert setup["action"] == "BLOCK"
    assert setup["blocked"] == [expected_reason]
    assert setup["signal_source"] == "decision"
    assert setup["execution_authority"] == authority


def test_empty_snapshot_blocks_trade_setup_authority_at_no_history() -> None:
    snapshot = empty_market_snapshot(
        ibkr_stock_payload("SPY"),
        "5m",
        "No history",
    )
    authority = snapshot["trade_setup"]["execution_authority"]

    assert authority == {
        "contract": TRADE_SETUP_AUTHORITY_CONTRACT,
        "state": "blocked",
        "ready": False,
        "provider_id": _PROVIDER_ID,
        "provider_ids": [_PROVIDER_ID],
        "reason_code": "no_history",
    }
    assert snapshot["trade_setup"]["action"] == "BLOCK"
    assert snapshot["trade_setup"]["blocked"] == ["no_history"]
    assert snapshot["trade_setup"]["signal_source"] == "data_quality"
    assert snapshot["command"] == snapshot["trade_setup"]["action_card"]


def test_no_confirmed_bar_snapshot_blocks_trade_setup_authority() -> None:
    bars = [
        _bar(0, closed=False),
        _bar(1, closed=False),
    ]
    instrument = instrument_with_bar_sessions(ibkr_stock_payload("SPY"), bars)

    snapshot = analyze_bars(
        bars,
        instrument=instrument,
        analysis_as_of_utc=bars[-1].ts + timedelta(minutes=1),
    )
    authority = snapshot["trade_setup"]["execution_authority"]

    assert authority == {
        "contract": TRADE_SETUP_AUTHORITY_CONTRACT,
        "state": "blocked",
        "ready": False,
        "provider_id": _PROVIDER_ID,
        "provider_ids": [_PROVIDER_ID],
        "reason_code": "no_confirmed_bar",
    }
    assert snapshot["trade_setup"]["action"] == "BLOCK"
    assert snapshot["trade_setup"]["blocked"] == ["no_confirmed_bar"]
    assert snapshot["trade_setup"]["signal_source"] == "data_quality"
    assert snapshot["command"] == snapshot["trade_setup"]["action_card"]
