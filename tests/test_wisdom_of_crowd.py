from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aef_terminal.domain import Bar, Direction, ScenarioKind
from aef_terminal.features.provider_session import ProviderSessionInterval, ProviderSessionReset
from aef_terminal.engine.analyze import attach_indicator_status
from aef_terminal.indicators.contracts import (
    validate_indicator_result_contract,
)
from aef_terminal.indicators.modules.wisdom_of_crowd import (
    WisdomOfCrowdParams,
    wisdom_of_crowd,
)
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.indicators.modules.wisdom_of_crowd.calculation import _pullback_index
from aef_terminal.signals.indicator_state import signal_candidates_from_indicator_state
from tests.provider_payloads import explicit_vwap_session_for_bars


def _bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Bar:
    return Bar(
        "SPY",
        datetime(2026, 8, 10, 13, 30, tzinfo=UTC) + timedelta(minutes=3 * index),
        open_,
        high,
        low,
        close,
        10_000 + index * 10,
        "3m",
        source="test",
    )


def _long_pullback_reclaim_bars() -> list[Bar]:
    bars: list[Bar] = []
    for index in range(60):
        close = 100.0 + index * 0.02
        bars.append(_bar(index, close - 0.01, close + 0.12, close - 0.12, close))
    bars.extend(
        (
            _bar(60, 101.17, 101.18, 100.90, 100.98),
            _bar(61, 100.98, 101.08, 100.92, 101.02),
            _bar(62, 101.03, 101.22, 101.00, 101.18),
        )
    )
    return bars


def _mirror_short(bars: list[Bar]) -> list[Bar]:
    return [
        _bar(
            index,
            200.0 - bar.open,
            200.0 - bar.low,
            200.0 - bar.high,
            200.0 - bar.close,
        )
        for index, bar in enumerate(bars)
    ]


def _calculate(bars: list[Bar], **params) -> dict:
    return wisdom_of_crowd(
        bars,
        params=WisdomOfCrowdParams(**params),
        vwap_session=explicit_vwap_session_for_bars(bars),
    )


def test_wisdom_of_crowd_confirms_long_pullback_reclaim() -> None:
    bars = _long_pullback_reclaim_bars()
    result = _calculate(bars)

    latest = result["latest"]
    assert latest["regime_code"] == "trend_up"
    assert latest["state_code"] == "reclaim_confirmed"
    assert latest["action"] == "GO"
    assert latest["direction"] == Direction.LONG.value
    assert latest["reclaim_confirmed"] is True
    assert latest["signal"]["plan_coherent"] is True
    assert latest["stop"] < latest["trigger"] < latest["target"]
    assert latest["trigger_event"]["code"] == "crowd_pullback_reclaim_confirmed"
    assert any(item.get("signal_overlay") is True for item in result["overlays"])
    assert any(item.get("type") == "table" for item in result["overlays"])
    assert not hasattr(WisdomOfCrowdParams(), "table_position")


def test_wisdom_of_crowd_is_directionally_symmetric() -> None:
    bars = _mirror_short(_long_pullback_reclaim_bars())
    result = _calculate(bars)

    latest = result["latest"]
    assert latest["regime_code"] == "trend_down"
    assert latest["state_code"] == "reclaim_confirmed"
    assert latest["action"] == "GO"
    assert latest["direction"] == Direction.SHORT.value
    assert latest["target"] < latest["trigger"] < latest["stop"]


def test_wisdom_of_crowd_blocks_chasing_an_extended_move() -> None:
    bars = _long_pullback_reclaim_bars()[:-3]
    previous = bars[-1]
    bars.append(
        _bar(
            len(bars),
            previous.close,
            previous.close + 1.65,
            previous.close - 0.03,
            previous.close + 1.55,
        )
    )
    result = _calculate(bars)

    latest = result["latest"]
    assert latest["regime_code"] == "trend_up"
    assert latest["state_code"] == "extended"
    assert latest["action"] == "BLOCK"
    assert latest["blocked_reason"] == "crowd_no_chase_extension"


def test_wisdom_of_crowd_fails_closed_without_provider_vwap_session() -> None:
    bars = _long_pullback_reclaim_bars()
    result = wisdom_of_crowd(
        bars,
        vwap_session=ProviderSessionReset(
            available=False,
            source="provider_schedule",
            reason_code="provider_trading_intervals_missing",
            calendar="unknown",
        ),
    )

    assert result["latest"] is None
    assert result["availability"]["state"] == "blocked"
    assert result["availability"]["reason_code"] == "provider_trading_intervals_missing"


def test_wisdom_of_crowd_promotes_typed_transit_candidate() -> None:
    bars = _long_pullback_reclaim_bars()
    result = _calculate(bars)
    candidates = signal_candidates_from_indicator_state(
        result,
        source="wisdom_of_crowd",
        score_floor=58.0,
        atr_value=0.25,
        min_target_atr=1.0,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.name == "crowd_trend_continuation"
    assert candidate.direction is Direction.LONG
    assert candidate.kind is ScenarioKind.TRANSIT
    assert candidate.reason_code == "crowd_pullback_reclaim_confirmed"


def test_wisdom_of_crowd_result_matches_registered_runtime_contract() -> None:
    bars = _long_pullback_reclaim_bars()
    normalized = attach_indicator_status(
        "wisdom_of_crowd",
        _calculate(bars),
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )
    manifest = indicator_manifest()["wisdom_of_crowd"]

    validate_indicator_result_contract(
        normalized,
        name="wisdom_of_crowd",
        renderer_contract=manifest["renderer_contract"],
        runtime_payload_contract=manifest["runtime_payload_contract"],
    )
    assert manifest["ui"]["default_calc"] is False
    assert manifest["label"] == "Мудрость толпы"


def test_wisdom_pullback_is_not_an_executable_recommendation_until_go() -> None:
    result = _calculate(_long_pullback_reclaim_bars())
    armed = [row for row in result["series"] if row["action"] == "ARM"]
    assert armed
    for row in result["series"]:
        if row["action"] == "GO":
            continue
        assert row["signal"]["signal_actionable"] is False
        assert row["signal"]["trigger"] is None
        assert row["signal"]["stop"] is None
        assert row["signal"]["target"] is None
        assert (
            signal_candidates_from_indicator_state(
                {"latest": row}, source="wisdom_of_crowd", score_floor=58.0
            )
            == []
        )


def test_wisdom_pullback_search_stops_at_a_newer_invalidation() -> None:
    bars = [
        _bar(0, 100.0, 100.1, 99.8, 100.0),
        _bar(1, 100.0, 100.1, 99.0, 99.8),
        _bar(2, 100.6, 100.8, 100.5, 100.7),
    ]
    assert _pullback_index(
        bars,
        index=2,
        direction=Direction.LONG,
        atr_values=[1.0] * 3,
        ema_fast=[100.0] * 3,
        ema_slow=[99.5] * 3,
        vwap=[99.7] * 3,
        params=WisdomOfCrowdParams(),
        earliest_index=0,
    ) == (None, None)


def test_wisdom_does_not_reuse_previous_provider_session_pullback() -> None:
    bars = _long_pullback_reclaim_bars()
    split = bars[-1].ts
    session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="",
        calendar="provider_trading_hours",
        intervals=(
            ProviderSessionInterval(bars[0].ts, split, bars[0].ts.date()),
            ProviderSessionInterval(
                split, split + timedelta(hours=1), split.date() + timedelta(days=1)
            ),
        ),
    )
    result = wisdom_of_crowd(
        bars, params=WisdomOfCrowdParams(session_min_bars=1), vwap_session=session
    )
    assert result["latest"]["action"] != "GO"
    assert result["latest"]["signal"]["signal_actionable"] is False


def test_wisdom_historical_signal_facts_are_prefix_invariant() -> None:
    bars = _long_pullback_reclaim_bars()
    full = _calculate(bars)
    fields = ("action", "direction", "trigger", "stop", "target", "score", "state_code")
    for row in full["series"]:
        index = next(i for i, bar in enumerate(bars) if bar.ts.isoformat() == row["ts"])
        prefix = _calculate(bars[: index + 1])["latest"]
        assert {key: row[key] for key in fields} == {key: prefix[key] for key in fields}


def test_wisdom_uses_one_directional_glyph_and_preserves_typed_plan() -> None:
    for bars, glyph in (
        (_long_pullback_reclaim_bars(), "⇈"),
        (_mirror_short(_long_pullback_reclaim_bars()), "⇊"),
    ):
        result = _calculate(bars)
        label = next(item for item in result["overlays"] if item.get("signal_overlay") is True)
        assert label["lines"] == [glyph]
        assert label["glyph_only"] is True
        assert label["no_tick"] is True
        assert label["source"] == "wisdom_of_crowd"
        assert label["entry"] is not None
        assert label["stop"] is not None
        assert label["target"] is not None
