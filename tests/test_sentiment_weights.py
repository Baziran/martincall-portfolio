import pytest

from aef_terminal.domain import (
    ActionPhase,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    SignalCandidate,
)
from aef_terminal.engine.sentiment import build_direction_sentiment
from aef_terminal.engine.sentiment_weights import resolve_candidate_weight
from aef_terminal.runtime.instruments import PROFILES


def _wait_decision() -> ScenarioDecision:
    return ScenarioDecision(
        ScenarioKind.WAIT,
        Direction.FLAT,
        0.0,
        ActionPhase.WAIT,
        None,
        None,
        None,
        None,
        DomainFact("wait"),
        [],
    )


def test_research_weights_rank_impulse_above_wolfe() -> None:
    assert (
        resolve_candidate_weight("impulse_pullback")
        > resolve_candidate_weight("wolfe_confirmed") * 2
    )
    assert resolve_candidate_weight("smc_structure") > resolve_candidate_weight("wolfe_confirmed")
    assert resolve_candidate_weight("wolfe_confirmed", profile_key="ES") < resolve_candidate_weight(
        "wolfe_confirmed", profile_key="GOLD"
    )


def test_high_win_rate_short_candidate_can_flip_weak_long_sentiment() -> None:
    sentiment = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.TRANSIT,
            Direction.LONG,
            52.0,
            ActionPhase.WATCH,
            None,
            None,
            None,
            None,
            DomainFact("sentiment_watch"),
            [],
        ),
        indicators={},
        vsa_context=None,
        candidates=[
            SignalCandidate(
                "impulse_pullback",
                Direction.SHORT,
                88.0,
                100.0,
                "pullback short",
                DomainFact("pullback_short"),
                finality=CandidateFinality.CONFIRMED,
            ),
        ],
        instrument_profile=PROFILES["ES"],
    )

    impulse = next(
        item for item in sentiment["contributors"] if item["source"] == "impulse_pullback"
    )
    assert impulse["weight"] >= 1.0
    assert impulse["weight_tier"] == "high"
    assert sentiment["direction"] in {"short", "flat"}


def test_low_win_rate_wolfe_candidate_has_reduced_weight() -> None:
    sentiment = build_direction_sentiment(
        decision=_wait_decision(),
        indicators={},
        vsa_context=None,
        candidates=[
            SignalCandidate(
                "wolfe_confirmed",
                Direction.SHORT,
                90.0,
                100.0,
                "confirmed wolfe",
                DomainFact("confirmed_wolfe"),
                finality=CandidateFinality.CONFIRMED,
            ),
        ],
        instrument_profile=PROFILES["ES"],
    )

    wolfe = next(item for item in sentiment["contributors"] if item["source"] == "wolfe_confirmed")
    assert wolfe["weight"] < 0.25
    assert wolfe["weight_tier"] == "low"


def test_indicator_and_its_candidate_contribute_once_by_typed_source() -> None:
    candidate = SignalCandidate(
        "linda_fade",
        Direction.SHORT,
        88.0,
        100.0,
        "linda fade",
        DomainFact("linda_fade"),
        source="linda_volume",
        finality=CandidateFinality.CONFIRMED,
    )
    sentiment = build_direction_sentiment(
        decision=_wait_decision(),
        indicators={
            "linda_volume": {
                "latest": {
                    "signal": {
                        "direction": "short",
                        "action": "GO",
                        "score": 88.0,
                        "code": "linda_fade",
                    }
                }
            }
        },
        vsa_context=None,
        candidates=[candidate],
        instrument_profile=PROFILES["ES"],
    )

    linda = [item for item in sentiment["contributors"] if item["source"] == "linda_volume"]
    assert len(linda) == 1
    assert not any(item["source"] == "linda_fade" for item in sentiment["contributors"])


def test_sentiment_never_invents_missing_or_text_contributor_scores() -> None:
    sentiment = build_direction_sentiment(
        decision=_wait_decision(),
        indicators={
            "linda_volume": {
                "latest": {
                    "signal": {
                        "direction": "long",
                        "action": "GO",
                        "score": "88.0",
                    }
                }
            },
            "market_spotlight": {
                "sentiment_role": "market_context",
                "market_context": {"direction": "long"},
            },
        },
        vsa_context=None,
        candidates=[],
        tick_bias={"direction": "long"},
        instrument_profile=PROFILES["ES"],
    )

    assert sentiment["contributors"] == []
    assert sentiment["score"] == 50.0
    assert sentiment["direction"] == "flat"


@pytest.mark.parametrize("top_level", [None, "malformed", {"direction": "short", "score": 80.0}])
def test_sentiment_uses_only_the_authoritative_market_context(top_level) -> None:
    projected_context = {"direction": "long", "score": 99.0}
    sentiment = build_direction_sentiment(
        decision=_wait_decision(),
        indicators={
            "market_spotlight": {
                "sentiment_role": "market_context",
                "market_context": top_level,
                "table": {"market_context": projected_context},
                "latest": {
                    "market_context": projected_context,
                    "table_state": {"market_context": projected_context},
                },
            }
        },
        vsa_context=None,
        candidates=[],
        instrument_profile=PROFILES["ES"],
    )

    if isinstance(top_level, dict):
        assert sentiment["direction"] == "short"
        assert sentiment["contributors"][0]["score"] == 80.0
    else:
        assert sentiment["direction"] == "flat"
        assert sentiment["contributors"] == []


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("execute", ActionPhase.GO),
        ("armed", ActionPhase.ARM),
        ("impulse_confirmed", ActionPhase.WATCH),
        ("impulse_developing", ActionPhase.WATCH),
    ],
)
def test_sentiment_publishes_canonical_phase_and_role_without_screen_prose(phase, expected) -> None:
    sentiment = build_direction_sentiment(
        decision=_wait_decision(),
        indicators={
            "market_spotlight": {
                "sentiment_role": "market_context",
                "market_context": {
                    "direction": "long",
                    "score": 75.0,
                    "strategy": {"active": True, "phase": phase, "direction": "long"},
                },
            },
        },
        vsa_context=None,
        candidates=[],
        instrument_profile=PROFILES["ES"],
    )
    contributor = sentiment["contributors"][0]
    assert contributor["role"] == "market_context"
    assert contributor["action"] == expected.value
    assert contributor["strategy_phase"] == phase
    assert not {"label", "strategy_summary", "strategy_action"} & contributor.keys()
