from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aef_terminal.domain import ActionPhase, Bar, CandidateFinality, Direction
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    indicator_defaults_from_params,
    score_action,
    score_promotion_floors,
)
from aef_terminal.runtime.pivots import PivotContext, alternating_pivot_points
from aef_terminal.runtime.presentation import trade_plan_rejection_text
from aef_terminal.runtime.timeframes import adapt_bars, expiry_after_bars, interval_minutes
from aef_terminal.signals.candidates import candidate_from_trade_plan_item
from aef_terminal.signals.direction import direction_from_any, opposite_direction
from aef_terminal.signals.trade_plan import (
    coherent_trade_plan,
    execution_plan_rejection,
    normalize_trade_plan,
    reward_risk,
    trade_plan_quality,
)


def test_direction_helpers_admit_only_canonical_values() -> None:
    assert direction_from_any("long") is Direction.LONG
    assert direction_from_any("short") is Direction.SHORT
    assert direction_from_any("flat") is Direction.FLAT
    assert direction_from_any("call") is Direction.FLAT
    assert direction_from_any("PUT") is Direction.FLAT
    assert direction_from_any("wait") == Direction.FLAT
    assert opposite_direction("long") == Direction.SHORT


def test_trade_plan_geometry_accepts_zero_and_negative_finite_prices() -> None:
    assert coherent_trade_plan(Direction.LONG, 0.0, -1.0, 2.0)
    assert coherent_trade_plan(Direction.SHORT, -1.0, 0.0, -3.0)
    assert reward_risk(Direction.LONG, 0.0, -1.0, 2.0) == 2.0
    assert (
        execution_plan_rejection(
            Direction.SHORT,
            fill_price=-1.0,
            stop=0.0,
            target=-3.0,
            min_rr=1.0,
        )
        is None
    )


def test_trade_plan_geometry_rejects_non_finite_prices() -> None:
    assert not coherent_trade_plan(Direction.LONG, 0.0, -1.0, float("inf"))
    assert reward_risk(Direction.SHORT, float("nan"), 0.0, -2.0) is None
    assert (
        execution_plan_rejection(
            Direction.LONG,
            fill_price=0.0,
            stop=float("-inf"),
            target=2.0,
        ).code
        == "incomplete_trade_plan"
    )


@pytest.mark.parametrize(
    ("direction", "stop", "target"),
    [(Direction.LONG, 98.0, 101.0), (Direction.SHORT, 102.0, 99.0)],
)
def test_normalization_rejects_low_rr_without_moving_the_target(direction, stop, target) -> None:
    plan = normalize_trade_plan(direction, 100.0, stop, target, min_rr=2.0)

    assert plan["target"] == target
    assert plan["rr"] == 0.5
    assert plan["coherent"] is False
    assert plan["blocked_reason"]
    admitted = normalize_trade_plan(direction, 100.0, stop, target, min_rr=0.5)
    assert admitted["coherent"] is True
    assert admitted["target"] == target


@pytest.mark.parametrize(
    ("direction", "close", "stop", "target"),
    [(Direction.LONG, 104.0, 98.0, 103.0), (Direction.SHORT, 96.0, 102.0, 97.0)],
)
def test_normalization_cannot_repair_a_target_already_passed_at_market(
    direction, close, stop, target
) -> None:
    bar = Bar("ES", datetime(2026, 1, 1, tzinfo=UTC), close, close + 1, close - 1, close, 100)
    plan = normalize_trade_plan(direction, 100.0, stop, target, bar=bar, atr_pad=0.25)

    assert plan["entry"] == close
    assert plan["target"] == target
    assert plan["coherent"] is False
    assert plan["rr"] is None


@pytest.mark.parametrize("min_rr", [float("inf"), float("nan"), True])
def test_normalization_rejects_invalid_rr_policy(min_rr) -> None:
    plan = normalize_trade_plan(Direction.LONG, 100.0, 98.0, 104.0, min_rr=min_rr)

    assert plan["coherent"] is False
    assert plan["target"] == 104.0


def test_trade_plan_quality_preserves_distinct_zero_invalidation() -> None:
    quality = trade_plan_quality(Direction.LONG, trigger=2, stop=1, target=4, invalid=0)

    assert quality["stop"] == 1.0
    assert quality["invalid"] == 0.0
    assert quality["rr"] == 2.0


@pytest.mark.parametrize(
    ("entry", "stop", "target", "minimum", "code", "message"),
    [
        (None, 98.0, 104.0, 1.0, "incomplete_trade_plan", "incomplete trade plan"),
        (97.0, 98.0, 104.0, 1.0, "stop_through_fill", "stop 98 through fill 97"),
        (
            105.0,
            98.0,
            104.0,
            1.0,
            "incoherent_trade_plan_at_fill",
            "trade plan incoherent at execution price",
        ),
        (100.0, 98.0, 101.0, 2.0, "execution_rr_below_minimum", "RR 0.50 below min 2.00 at fill"),
        (
            100.0,
            98.0,
            104.0,
            float("nan"),
            "invalid_minimum_reward_risk",
            "invalid minimum reward/risk",
        ),
        (
            100.0,
            60.0,
            100.1,
            0.0,
            "unrealistic_stop_distance_at_fill",
            "unrealistic stop distance at fill",
        ),
    ],
)
def test_execution_plan_rejection_is_typed_and_rendered_separately(
    entry, stop, target, minimum, code, message
) -> None:
    rejection = execution_plan_rejection(
        Direction.LONG, fill_price=entry, stop=stop, target=target, min_rr=minimum
    )
    assert rejection is not None
    assert rejection.code == code
    assert "reason" not in rejection.attributes
    assert trade_plan_rejection_text(rejection) == message


def test_explicit_r_target_survives_only_floating_point_roundoff() -> None:
    entry, stop, minimum = 100.5, 98.56, 1.8
    target = entry + (entry - stop) * minimum

    assert normalize_trade_plan(Direction.LONG, entry, stop, target, min_rr=minimum)["coherent"]
    assert (
        execution_plan_rejection(
            Direction.LONG, fill_price=entry, stop=stop, target=target, min_rr=minimum
        )
        is None
    )
    assert (
        trade_plan_quality(Direction.LONG, trigger=entry, stop=stop, target=target, min_rr=minimum)[
            "low_rr"
        ]
        is False
    )
    assert (
        normalize_trade_plan(Direction.LONG, entry, stop, target - 0.0001, min_rr=minimum)[
            "coherent"
        ]
        is False
    )
    assert trade_plan_rejection_text(None) == ""


def test_candidate_from_trade_plan_item_preserves_quality_details() -> None:
    item = {
        "action": "ARM",
        "direction": "long",
        "score": 72,
        "trigger": 100,
        "stop": 98,
        "invalidation": 96,
        "target": 104,
        "reason_code": "setup",
        "trigger_event": {"code": "setup"},
    }
    candidate = candidate_from_trade_plan_item(
        item,
        name="demo",
        score_floor=60,
        details={"code": "DMO"},
        finality=CandidateFinality.CONFIRMED,
    )

    assert candidate is not None
    assert candidate.direction == Direction.LONG
    assert candidate.details["rr"] == 2.0
    assert candidate.details["stop"] == 98.0
    assert candidate.details["invalid"] == 96.0
    assert candidate.details["code"] == "DMO"
    assert candidate.reason_code == "setup"
    assert candidate.producer_phase is ActionPhase.ARM
    item.pop("score")
    assert (
        candidate_from_trade_plan_item(
            item,
            name="demo",
            score_floor=0,
            finality=CandidateFinality.CONFIRMED,
        )
        is None
    )


def test_candidate_from_trade_plan_item_does_not_promote_display_text_as_reason() -> None:
    candidate = candidate_from_trade_plan_item(
        {
            "direction": "long",
            "score": 72,
            "trigger": 100,
            "stop": 98,
            "target": 104,
            "display_text": "display only",
            "message": "display message",
            "label": "display label",
            "reason": "display reason",
            "candidate_reason": "display candidate reason",
            "signal_reason": "display signal reason",
            "trigger_label": "display trigger label",
            "scenario": "display scenario",
            "setup": "display setup",
        },
        name="demo",
        score_floor=60,
        finality=CandidateFinality.CONFIRMED,
    )

    assert candidate is None


def test_candidate_from_trade_plan_item_rejects_non_candidate_action() -> None:
    assert (
        candidate_from_trade_plan_item(
            {
                "action": "WAIT",
                "direction": "long",
                "score": 72,
                "trigger": 100,
                "stop": 98,
                "target": 104,
                "trigger_event": {"code": "setup"},
            },
            name="demo",
            score_floor=60,
            finality=CandidateFinality.CONFIRMED,
        )
        is None
    )


def test_signal_promotion_modules_do_not_read_display_tooltips() -> None:
    modules = [
        Path("src/aef_terminal/indicators/modules/linda_volume/candidates.py"),
        Path("src/aef_terminal/signals/indicator_state.py"),
        Path("src/aef_terminal/indicators/modules/wolfe_structure/__init__.py"),
        Path("src/aef_terminal/indicators/modules/w5_structure/__init__.py"),
    ]

    for path in modules:
        source = path.read_text(encoding="utf-8")
        assert '.get("tooltip")' not in source
        assert '"tooltip":' not in source
        assert '.get("message")' not in source
        assert '.get("label")' not in source
        assert '.get("read")' not in source
        assert '.get("candidate_reason")' not in source
        assert '.get("signal_reason")' not in source
        assert '.get("trigger_label")' not in source


def test_timeframe_helpers_scale_bars_and_expiry() -> None:
    ts = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    assert interval_minutes("60m") == 60
    assert adapt_bars(8, "1m") == 40
    assert adapt_bars(8, "60m") == 4
    assert expiry_after_bars(ts, "60m", 5) == ts + timedelta(hours=5)


def test_alternating_pivot_points_keeps_stronger_same_side_pivot() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(
            "ES",
            base + timedelta(minutes=index),
            price,
            price + high,
            price - low,
            price,
            1000,
            "1m",
        )
        for index, (price, high, low) in enumerate(
            [
                (10, 1, 1),
                (11, 1, 1),
                (12, 3, 1),
                (11, 1, 1),
                (10, 1, 3),
                (11, 1, 1),
                (13, 4, 1),
                (12, 1, 1),
                (11, 1, 2),
            ]
        )
    ]

    pivots = alternating_pivot_points(bars, left=1, right=1)

    assert pivots
    assert all(
        left["type"] != right["type"] for left, right in zip(pivots, pivots[1:], strict=False)
    )


def test_pivot_context_caches_and_returns_copies() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(
            "ES",
            base + timedelta(minutes=index),
            price,
            price + high,
            price - low,
            price,
            1000,
            "1m",
        )
        for index, (price, high, low) in enumerate(
            [
                (10, 1, 1),
                (11, 1, 1),
                (12, 3, 1),
                (11, 1, 1),
                (10, 1, 3),
                (11, 1, 1),
                (13, 4, 1),
                (12, 1, 1),
                (11, 1, 2),
            ]
        )
    ]
    context = PivotContext(bars)

    first = context.alternating(1, 1)
    first[0]["price"] = -1
    second = context.alternating(1, 1)

    assert second[0]["price"] != -1
    assert len(second) == len(alternating_pivot_points(bars, left=1, right=1))


def test_indicator_defaults_from_params_clamps_shared_tuning() -> None:
    defaults = indicator_defaults_from_params(
        {
            "atr_len": 500,
            "rvol_len": 45,
            "ema": {"pullback": 13, "fast": 34, "slow": 89, "magnet": 377},
            "score": {"watch": 61, "go": 81},
            "rvol": {"low": 0.7, "climax": 2.2},
            "session": {"dampening_enabled": False, "low_activity_score_multiplier": 0.6},
        }
    )

    assert defaults.atr_len == 100
    assert defaults.rvol_len == 45
    assert defaults.ema.pullback == 13
    assert defaults.ema.fast == 34
    assert defaults.ema.slow == 89
    assert defaults.ema.magnet == 377
    assert defaults.score.watch == 61
    assert defaults.score.go == 81
    assert defaults.rvol.low == 0.7

    recovered = indicator_defaults_from_params(
        {"ema": {"pullback": 2, "fast": 2, "slow": 2, "magnet": 2}}
    )
    assert recovered.ema.pullback == 20
    assert recovered.ema.fast == 21
    assert recovered.ema.slow == 55
    assert recovered.ema.magnet == 233
    assert defaults.rvol.climax == 2.2


def test_score_action_uses_shared_bands() -> None:
    defaults = indicator_defaults_from_params(
        {"score": {"pre": 40, "watch": 60, "arm": 72, "go": 80}}
    )
    bands = defaults.score

    assert score_action(39, bands) == "WAIT"
    assert score_action(45, bands) == "CANDIDATE"
    assert score_action(65, bands) == "WATCH"
    assert score_action(75, bands) == "ARM"
    assert score_action(82, bands) == "GO"


def test_score_action_supports_local_go_floor_and_pre_suppression() -> None:
    defaults = indicator_defaults_from_params(
        {"score": {"pre": 40, "watch": 60, "arm": 72, "go": 80}}
    )
    bands = defaults.score

    assert score_action(39, bands, go_floor=70, pre_action="WAIT") == "WAIT"
    assert score_action(45, bands, go_floor=70, pre_action="WAIT") == "WAIT"
    assert score_action(61, bands, go_floor=70, pre_action="WAIT") == "WATCH"
    assert score_action(70, bands, go_floor=70, pre_action="WAIT") == "GO"


def test_breakout_mode_thresholds_track_global_score_bands() -> None:
    from aef_terminal.indicators.defaults import breakout_mode_thresholds

    bands = DEFAULT_INDICATOR_SETTINGS.score
    early_arm, early_go, early_rev = breakout_mode_thresholds("Early", bands)
    balanced_arm, balanced_go, balanced_rev = breakout_mode_thresholds("Balanced", bands)
    strict_arm, strict_go, strict_rev = breakout_mode_thresholds("Strict", bands)

    assert early_arm == 52.0
    assert early_go == 60.0
    assert early_rev == 54.0
    assert balanced_arm == 60.0
    assert balanced_go == 68.0
    assert balanced_rev == 62.0
    assert strict_arm == 70.0
    assert strict_go == 78.0
    assert strict_rev == 72.0


def test_linda_decision_action_contract() -> None:
    from aef_terminal.indicators.modules.linda_volume.action import linda_decision_action

    bands = DEFAULT_INDICATOR_SETTINGS.score
    assert (
        linda_decision_action(code="VW_RECLAIM", score=85, min_label_score=70, bands=bands) == "ARM"
    )
    assert linda_decision_action(code="PB_UP", score=85, min_label_score=70, bands=bands) == "GO"
    assert (
        linda_decision_action(code="NO_SUPPLY", score=50, min_label_score=70, bands=bands)
        == "WATCH"
    )
    assert (
        linda_decision_action(code="NO_SUPPLY", score=40, min_label_score=70, bands=bands)
        == "WATCH"
    )


def test_score_promotion_floors_follow_watch_band() -> None:
    defaults = indicator_defaults_from_params({"score": {"watch": 61}})
    floors = score_promotion_floors(defaults)

    assert floors.watch == 61
    assert floors.strong_watch == 63
    assert floors.structure == 65
    assert floors.label_importance == 72


def test_indicator_state_promotion_trace_records_reject_reason() -> None:
    from aef_terminal.signals.indicator_state import (
        indicator_state_promotion_trace,
        signal_candidates_from_indicator_state,
    )

    indicator = {
        "latest": {
            "signal": {
                "direction": "long",
                "action": "ARM",
                "score": 55,
                "trigger": 100.0,
                "stop": 98.0,
                "target": 104.0,
                "code": "PB_UP",
            }
        }
    }
    trace = indicator_state_promotion_trace(indicator, source="linda_volume", score_floor=60.0)
    assert trace["promoted"] is False
    assert trace["reject_reason"] == "below_score_floor"
    assert trace["score_band"] == "CANDIDATE"

    candidates = signal_candidates_from_indicator_state(
        indicator, source="linda_volume", score_floor=60.0
    )
    assert candidates == []
    assert indicator["candidate_promotion"]["reject_reason"] == "below_score_floor"
    indicator["latest"]["signal"].pop("score")
    trace = indicator_state_promotion_trace(indicator, source="linda_volume", score_floor=0.0)
    assert trace["promoted"] is False
    assert trace["reject_reason"] == "missing_score"
    assert trace["score"] is None


def test_indicator_state_promotion_rejects_obsolete_signal() -> None:
    from aef_terminal.signals.indicator_state import (
        indicator_state_promotion_trace,
        signal_candidates_from_indicator_state,
    )

    indicator = {
        "latest": {
            "obsolete": True,
            "signal": {
                "direction": "long",
                "action": "GO",
                "score": 92,
                "trigger": 100.0,
                "stop": 98.0,
                "target": 104.0,
                "code": "PB_UP",
            },
        }
    }

    trace = indicator_state_promotion_trace(indicator, source="linda_volume", score_floor=60.0)
    assert trace["promoted"] is False
    assert trace["reject_reason"] == "obsolete_signal"

    candidates = signal_candidates_from_indicator_state(
        indicator, source="linda_volume", score_floor=60.0
    )
    assert candidates == []
    assert indicator["candidate_promotion"]["reject_reason"] == "obsolete_signal"


def test_indicator_state_promotion_preserves_zero_stop_over_invalidation_fallback() -> None:
    from aef_terminal.signals.indicator_state import signal_candidates_from_indicator_state

    indicator = {
        "latest": {
            "signal": {
                "direction": "long",
                "action": "GO",
                "score": 80,
                "trigger": 1.0,
                "stop": 0.0,
                "invalidation": -2.0,
                "target": 2.0,
                "trigger_event": {"code": "zero_stop"},
            }
        }
    }

    candidates = signal_candidates_from_indicator_state(
        indicator, source="custom_source", score_floor=50
    )

    assert len(candidates) == 1
    assert candidates[0].details["stop"] == 0.0
    assert candidates[0].details["invalid"] == -2.0
    assert candidates[0].producer_phase is ActionPhase.GO


def test_w5_promotion_preserves_zero_price_over_level_fallback() -> None:
    from aef_terminal.indicators.modules.w5_structure import signal_candidates_from_w5_indicator

    candidates = signal_candidates_from_w5_indicator(
        {
            "latest": {
                "direction": "long",
                "action": "GO",
                "score": 70,
                "price": 0.0,
                "level": 10.0,
                "stop": -1.0,
                "target": 2.0,
                "trigger_event": {"code": "zero_price"},
                "candidate_finality": CandidateFinality.CONFIRMED.value,
            }
        },
        score_floor=55.0,
        atr_value=None,
        features=None,
    )

    assert len(candidates) == 1
    assert candidates[0].level == 0.0
    assert candidates[0].producer_phase is ActionPhase.GO
