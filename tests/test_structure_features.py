from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aef_terminal.domain import (
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
    StrategyMode,
)
from aef_terminal.engine.analyze.indicator_status import attach_indicator_status
from aef_terminal.features.smc import SmcParams, smc_snapshot
from aef_terminal.features.volume import resolve_rvol_context, volume_features
from aef_terminal.indicators.modules.breakout_accumulation import _join_stop_stage
import aef_terminal.indicators.modules.smc_channels as smc_channels_module
import aef_terminal.indicators.modules.w5_structure as w5_structure_module
from aef_terminal.indicators.modules.smc_channels import (
    SmcStructureParams,
    smc_structure as _smc_structure,
)
from aef_terminal.indicators.modules.w5_structure import (
    _w5_bos_trigger_state,
    signal_candidates_from_w5_indicator,
    w5_structure,
)
from aef_terminal.indicators.modules.wolfe_structure import (
    _confirmed_wolfe_finality,
    _latest_wolfe_item,
    signal_candidates_from_wolfe_indicator,
    wolfe_structure,
)
from aef_terminal.signals.indicator_state import signal_candidates_from_indicator_state
from tests.features_test_support import (
    breakout_accumulation_for_test as breakout_accumulation,
    choose_test_decision as choose_decision,
    instrument_profile_for_test as _test_profile,
    make_bar,
    smc_channels_for_test as smc_channels,
    smc_structure_for_test as smc_structure,
)
from tests.provider_payloads import explicit_vwap_session_for_bars


def test_smc_channels_returns_events_boxes_and_overlays() -> None:
    prices = [100, 101, 102, 101, 100, 99, 100, 102, 104, 103, 102, 101, 99, 98, 100, 103, 106, 105]
    bars = [
        make_bar(
            index, price, price + 0.7, price - 0.7, price + (0.25 if index % 2 == 0 else -0.2), 1100
        )
        for index, price in enumerate(prices)
    ]

    result = smc_channels(bars)

    assert result["version"] == "6-python-structure"
    assert result["pivots"]
    assert result["events"] or result["boxes"]
    assert "fvg_zones" in result
    assert "order_blocks" in result
    assert result["overlays"]
    assert any(item["type"] == "line" for item in result["overlays"])
    price_overlays = [item for item in result["overlays"] if item["type"] in {"box", "line"}]
    assert all(item["style"] in {"solid", "dashed", "dotted"} for item in price_overlays)
    assert all(
        not {"bg", "border", "color", "pattern_color"}.intersection(item) for item in price_overlays
    )
    assert any(
        item["type"] == "box" and item["style"] in {"dashed", "dotted"} and "border_opacity" in item
        for item in price_overlays
    )
    assert any(
        item["type"] == "line"
        and item.get("label_position") == "center"
        and item.get("label_side") in {"above", "below"}
        for item in price_overlays
    )
    active_geometry = [
        item
        for item in price_overlays
        if item.get("role")
        in {
            "smc_supply_zone",
            "smc_demand_zone",
            "smc_pivot_level",
            "fvg",
            "retained_fvg",
            "order_block",
            "smc_box",
            "smc_level",
            "smc_rebound_zone",
        }
    ]
    assert active_geometry
    assert all(item.get("retention") == "active" for item in active_geometry)
    if result["events"]:
        assert result["events"][-1]["event_code"]
        assert result["events"][-1]["scenario_kind"] in {"fade", "transit"}
        assert result["events"][-1]["signal"]["source"] == "smc_structure"
        assert result["events"][-1]["signal"]["kind"] in {"fade", "transit", "wait"}
        assert (
            result["events"][-1]["details"]["structure_mode"]
            == result["events"][-1]["signal"]["kind"]
        )
        assert result["structure_mode"] in {"fade", "transit", "wait"}
        assert "tooltip" not in result["events"][-1]
        assert all("\n" not in fact["code"] for fact in result["events"][-1]["evidence"]["context"])
        assert result["events"][-1]["setup"] == result["events"][-1]["event_code"]


def test_smc_structure_event_uses_typed_scenario_kind() -> None:
    bars = [make_bar(0, 100, 101, 99, 100, 1000)]

    from aef_terminal.indicators.modules.smc_channels import _attach_event_signal

    transit = {
        "event": "BOS↑",
        "event_code": "break_of_structure",
        "scenario_kind": "transit",
        "direction": "long",
        "score": 85,
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
    }
    fade = {
        "event": "SWEEP↓",
        "event_code": "liquidity_sweep",
        "scenario_kind": "fade",
        "direction": "short",
        "score": 72,
        "entry": 100.0,
        "stop": 101.0,
        "target": 98.0,
    }
    short_transit = {
        "event": "BOS↓",
        "event_code": "break_of_structure",
        "scenario_kind": "transit",
        "direction": "short",
        "score": 84,
        "entry": 100.0,
        "stop": 101.0,
        "target": 97.5,
    }
    short_fade = {
        "event": "REJECT↓",
        "event_code": "reject",
        "scenario_kind": "fade",
        "direction": "short",
        "score": 71,
        "entry": 100.0,
        "stop": 101.0,
        "target": 98.0,
    }

    _attach_event_signal(transit, bars)
    _attach_event_signal(fade, bars)
    _attach_event_signal(short_transit, bars)
    _attach_event_signal(short_fade, bars)

    assert transit["structure_mode"] == "transit"
    assert transit["signal"]["kind"] == "transit"
    assert transit["signal"]["code"] == "break_of_structure"
    assert fade["structure_mode"] == "fade"
    assert fade["signal"]["kind"] == "fade"
    assert short_transit["signal"]["kind"] == "transit"
    assert short_transit["signal"]["direction"] == "short"
    assert short_fade["signal"]["kind"] == "fade"
    assert short_fade["signal"]["direction"] == "short"

    display_only = {
        "event": "BOS↑",
        "direction": "long",
        "action": "GO",
        "score": 90,
        "entry": 100.0,
        "stop": 99.0,
        "target": 102.0,
    }
    _attach_event_signal(display_only, bars)
    assert display_only["action"] == "WAIT"
    assert display_only["signal"]["kind"] == "wait"


def test_smc_structure_preserves_zero_entry_instead_of_using_display_price() -> None:
    bars = [make_bar(0, 0.0, 1.0, -1.0, 0.0, 1000)]
    event = {
        "event": "BOS↑",
        "event_code": "break_of_structure",
        "scenario_kind": "transit",
        "direction": "long",
        "score": 85,
        "entry": 0.0,
        "price": 0.5,
        "stop": -2.0,
        "target": 2.0,
    }

    blocked_reason = smc_channels_module._normalize_event_plan(event, bars)
    smc_channels_module._attach_event_signal(
        event,
        bars,
        plan_blocked_reason=blocked_reason,
    )

    assert event["entry"] == 0.0
    assert event["signal"]["trigger"] == 0.0


def test_smc_countertrend_go_is_blocked_by_market_spotlight_veto() -> None:
    bars = [make_bar(0, 100, 101, 99, 100, 1000)]
    from aef_terminal.indicators.modules.smc_channels import _attach_event_signal

    event = {
        "event": "REJECT↓",
        "event_code": "reject",
        "scenario_kind": "fade",
        "action": "GO",
        "direction": "short",
        "score": 85,
        "entry": 100.0,
        "stop": 101.0,
        "target": 98.0,
    }
    market_context = {
        "direction": "long",
        "side_lock": "LONG",
        "state": "trend_initiation",
        "side_gate": {"veto_short": True, "short_unlock": False},
    }

    _attach_event_signal(event, bars, market_context=market_context)

    assert event["action"] == "BLOCK"
    assert event["signal"]["action"] == "BLOCK"
    assert event["signal"]["raw_action"] == "GO"
    assert event["signal"]["blocked"] is True
    assert event["signal"]["signal_actionable"] is False
    assert event["signal"]["blocked_reason"] == "market_spotlight_veto_short"
    assert event["market_context_gate"]["side_lock"] == "LONG"


def test_smc_countertrend_go_survives_when_market_spotlight_unlocks_side() -> None:
    bars = [make_bar(0, 100, 101, 99, 100, 1000)]
    from aef_terminal.indicators.modules.smc_channels import _attach_event_signal

    event = {
        "event": "REJECT↓",
        "event_code": "reject",
        "scenario_kind": "fade",
        "action": "GO",
        "direction": "short",
        "score": 85,
        "entry": 100.0,
        "stop": 101.0,
        "target": 98.0,
    }
    market_context = {
        "direction": "long",
        "side_lock": "LONG",
        "state": "trend_initiation",
        "side_gate": {"veto_short": False, "short_unlock": True},
    }

    _attach_event_signal(event, bars, market_context=market_context)

    assert event["action"] == "GO"
    assert event["signal"]["action"] == "GO"
    assert event["signal"]["blocked"] is False
    assert "market_context_gate" not in event


def test_smc_structure_kind_routes_fade_and_transit_in_decision() -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.2, 1000)]
    long_fade = SignalCandidate(
        "smc_structure",
        Direction.LONG,
        76.0,
        100.1,
        "demand reclaim fade",
        DomainFact("demand_reclaim_fade"),
        kind=ScenarioKind.FADE,
        finality=CandidateFinality.CONFIRMED,
    )
    short_fade = SignalCandidate(
        "smc_structure",
        Direction.SHORT,
        76.0,
        100.2,
        "supply reject fade",
        DomainFact("supply_reject_fade"),
        kind=ScenarioKind.FADE,
        finality=CandidateFinality.CONFIRMED,
    )
    long_transit = SignalCandidate(
        "smc_structure",
        Direction.LONG,
        76.0,
        100.1,
        "bullish structure break",
        DomainFact("bullish_structure_break"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    short_transit = SignalCandidate(
        "smc_structure",
        Direction.SHORT,
        76.0,
        100.2,
        "bearish structure break",
        DomainFact("bearish_structure_break"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    long_fade_decision = choose_decision(
        [long_fade, short_transit],
        bars=bars,
        atr_value=1.0,
        strategy_mode=StrategyMode.MEAN_REVERSION,
    )
    short_fade_decision = choose_decision(
        [short_fade, long_transit],
        bars=bars,
        atr_value=1.0,
        strategy_mode=StrategyMode.MEAN_REVERSION,
    )
    long_transit_decision = choose_decision(
        [short_fade, long_transit], bars=bars, atr_value=1.0, strategy_mode=StrategyMode.BREAKOUT
    )
    short_transit_decision = choose_decision(
        [long_fade, short_transit], bars=bars, atr_value=1.0, strategy_mode=StrategyMode.BREAKOUT
    )

    assert long_fade_decision.direction == Direction.LONG
    assert short_fade_decision.direction == Direction.SHORT
    assert long_transit_decision.direction == Direction.LONG
    assert short_transit_decision.direction == Direction.SHORT
    assert long_fade_decision.kind == ScenarioKind.FADE
    assert short_fade_decision.kind == ScenarioKind.FADE
    assert long_transit_decision.kind == ScenarioKind.TRANSIT
    assert short_transit_decision.kind == ScenarioKind.TRANSIT


def test_smc_structure_promotes_structure_mode_to_candidate() -> None:
    indicator = {
        "structure_mode": "fade",
        "latest": {
            "price": 100.0,
            "structure_mode": "fade",
            "details": {
                "structure_mode": "fade",
            },
            "signal": {
                "source": "smc_structure",
                "action": "GO",
                "direction": "short",
                "kind": "fade",
                "score": 74,
                "trigger": 100.0,
                "stop": 101.0,
                "target": 98.0,
                "reason": "supply reject",
                "trigger_event": {"code": "supply_reject"},
            },
        },
    }

    candidates = signal_candidates_from_indicator_state(
        indicator, source="smc_structure", score_floor=50
    )

    assert candidates
    assert candidates[0].kind == ScenarioKind.FADE
    assert candidates[0].details["structure_mode"] == "fade"


def test_smc_structure_rebound_plan_uses_merged_zone_context() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.2, 1000) for i in range(20)]
    bars.extend(
        [
            make_bar(20, 100.2, 101.0, 98.0, 98.4, 1300),
            make_bar(21, 98.5, 100.6, 98.1, 100.2, 1800),
            make_bar(22, 100.0, 101.2, 99.2, 100.8, 1500),
        ]
    )

    result = smc_structure(
        bars,
        channel_context={
            "active": True,
            "nearest": {
                "direction": "long",
                "price": 99.0,
                "touch": True,
                "no_fade": False,
            },
        },
    )

    assert "rebound" in result
    assert result["rebound"]["zones"]
    if result["rebound"]["active"]:
        assert result["latest"]["signal"]["source"] == "smc_structure"
        assert result["latest"]["signal"]["kind"] == "fade"
        assert result["structure_mode"] == "fade"
        assert result["latest"]["stop"] is not None
        assert result["latest"]["target"] is not None
        assert result["latest"]["lifecycle"]["state"] in {
            "WAIT_ENTRY",
            "FOLLOW",
            "RIDE",
            "STOP",
            "TP",
            "TP/SL",
        }
        assert any(
            item.get("type") == "box"
            and item.get("role") == "smc_rebound_zone"
            and item.get("trigger_event") == result["latest"]["trigger_event"]
            and item.get("badge_facts")
            and "label_position" not in item
            for item in result["overlays"]
        )


def test_smc_session_adjustments_fail_typed_without_provider_schedule() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.2, 1000) for i in range(20)]

    result = _smc_structure(
        bars,
        profile=_test_profile(bars),
        provider_session=None,
    )

    session_levels = result["rebound"]["session_levels"]
    assert session_levels["available"] is False
    assert session_levels["reason_code"] == "provider_session_missing"
    assert result["rebound"]["trend_day_warning"] is False


@pytest.mark.parametrize("resistance", [None, 102.0])
def test_smc_channel_confluence_does_not_fade_backend_no_fade_path(
    monkeypatch,
    resistance,
) -> None:
    bars = [make_bar(index, 100.0, 101.0, 99.0, 100.25, 1000) for index in range(7)]
    bars.append(make_bar(7, 99.8, 101.0, 99.0, 100.5, 1200))
    zone = {
        "kind": "support",
        "low": 99.0,
        "high": 101.0,
        "midpoint": 100.0,
        "touch_count": 0,
        "sources": ["test"],
        "source_types": ["test"],
        "has_fvg": False,
        "has_order_block": False,
        "score": 60.0,
    }
    zones = [zone]
    if resistance is not None:
        zones.append(
            {
                **zone,
                "kind": "resistance",
                "low": resistance,
                "high": resistance + 1,
                "midpoint": resistance + 0.5,
            }
        )
    monkeypatch.setattr(
        smc_channels_module,
        "_structure_reaction_zones",
        lambda *_args, **_kwargs: [dict(item) for item in zones],
    )
    monkeypatch.setattr(
        smc_channels_module,
        "_guide_vix_context",
        lambda *_args, **_kwargs: {
            "guide_dir": 0,
            "guide_state": "GUIDE MIX",
            "vix_state": "VIX NEUTRAL",
        },
    )
    channel_nearest = {
        "direction": "long",
        "price": 100.0,
        "touch": True,
        "reclaim": False,
    }

    fade_allowed = smc_channels_module._rebound_context(
        bars,
        {},
        profile=_test_profile(bars),
        features={"latest_atr": 2.0},
        channel_context={"nearest": {**channel_nearest, "no_fade": False}},
        provider_session=explicit_vwap_session_for_bars(bars),
        params=SmcStructureParams(),
    )
    no_fade = smc_channels_module._rebound_context(
        bars,
        {},
        profile=_test_profile(bars),
        features={"latest_atr": 2.0},
        channel_context={"nearest": {**channel_nearest, "no_fade": True}},
        provider_session=explicit_vwap_session_for_bars(bars),
        params=SmcStructureParams(),
    )

    if resistance is not None:
        assert fade_allowed["active"] is False
        assert no_fade["active"] is False
        return
    assert fade_allowed["active"] is True
    assert no_fade["active"] is True
    assert fade_allowed["best"]["score"] == no_fade["best"]["score"] + 5.0
    assert fade_allowed["best"]["stop"] == pytest.approx(98.56)
    assert fade_allowed["best"]["target"] == pytest.approx(103.992)


def test_smc_structure_short_rebound_supply_reject_is_fade_mode() -> None:
    bars = [
        make_bar(i, 102.0 - i * 0.04, 103.0 - i * 0.04, 101.0 - i * 0.04, 101.8 - i * 0.04, 1000)
        for i in range(20)
    ]
    bars.extend(
        [
            make_bar(20, 102.0, 104.0, 101.5, 103.6, 1300),
            make_bar(21, 103.5, 104.2, 102.0, 102.3, 1800),
            make_bar(22, 102.2, 102.8, 101.4, 101.6, 1500),
        ]
    )

    result = smc_structure(
        bars,
        channel_context={
            "active": True,
            "nearest": {
                "direction": "short",
                "price": 103.0,
                "touch": True,
                "no_fade": False,
            },
        },
    )

    if result["rebound"]["active"]:
        assert result["latest"]["direction"] == "short"
        assert result["latest"]["event_code"] == "reject"
        assert "event" not in result["latest"]
        assert "label" not in result["latest"]
        assert result["latest"]["signal"]["reason"] == "reject"
        assert result["latest"]["signal"]["kind"] == "fade"
        assert result["structure_mode"] == "fade"


def test_smc_structure_rebound_uses_guide_vix_context() -> None:
    bars = [
        make_bar(i, 100.0 + i * 0.04, 101.0 + i * 0.04, 99.0 + i * 0.04, 100.2 + i * 0.04, 1000)
        for i in range(20)
    ]
    bars.extend(
        [
            make_bar(20, 101.0, 101.8, 98.0, 98.6, 2200),
            make_bar(21, 98.5, 101.2, 98.1, 100.8, 2600),
        ]
    )
    bars = [
        bar.__class__(
            symbol="ES",
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timeframe=bar.timeframe,
        )
        for bar in bars
    ]

    result = smc_structure(
        bars,
        features={
            "smc_guide_direction": 1,
            "smc_vix_state": "VIX RELIEF",
        },
        channel_context={
            "active": True,
            "nearest": {
                "direction": "long",
                "price": 99.0,
                "touch": True,
                "no_fade": False,
            },
        },
    )

    assert result["rebound"]["guide"]["guide_state"] == "GUIDE UP"
    assert result["rebound"]["guide"]["vix_state"] == "VIX RELIEF"
    if result["rebound"]["active"]:
        assert any("Guide/VIX" in line for line in result["latest"]["context_lines"])


def test_smc_structure_guide_prefers_adaptive_rvol_context() -> None:
    bars = [
        make_bar(
            i, 100.0 + i * 0.03, 101.0 + i * 0.03, 99.0 + i * 0.03, 100.2 + i * 0.03, 900 + i * 12
        )
        for i in range(24)
    ]
    bars = [
        bar.__class__(
            symbol="ES",
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timeframe=bar.timeframe,
        )
        for bar in bars
    ]
    vol = volume_features(bars)
    result = smc_structure(
        bars,
        features={
            "rvol_mean": 0.7,
            "relative_volume": 0.8,
            "rvol": 0.9,
            "rvol_adaptive": vol["rvol_adaptive"],
        },
    )
    guide = result["rebound"]["guide"]

    assert guide["rvol_source"] == "rvol_adaptive"
    assert guide["rvol"] == round(float(vol["rvol_adaptive"]), 4)


def test_smc_structure_guide_preserves_zero_baseline_rvol() -> None:
    bars = [make_bar(i, 100.0, 100.5, 99.5, 100.0, 1000) for i in range(20)]
    bars.append(make_bar(20, 100.0, 102.0, 98.0, 98.5, 2000))

    guide = smc_channels_module._guide_vix_context(
        bars,
        profile=_test_profile(bars),
        features={"rvol_adaptive": 2.0, "rvol_mean": 0.0},
        atr=1.0,
        params=SmcStructureParams(),
    )

    assert guide["rvol"] == 2.0
    assert guide["vix_state"] == "VIX NEUTRAL"


def test_resolve_rvol_context_reports_missing_volume_context() -> None:
    value, source = resolve_rvol_context({"relative_volume": 1.3})
    assert source == "relative_volume"
    assert value == 1.3

    missing_value, missing_source = resolve_rvol_context({})
    assert missing_source == "unavailable"
    assert missing_value is None

    zero_value, zero_source = resolve_rvol_context({"rvol_adaptive": 0.0, "relative_volume": 1.3})
    assert zero_source == "rvol_adaptive"
    assert zero_value == 0.0

    coerced_value, coerced_source = resolve_rvol_context(
        {"rvol_adaptive": "0", "relative_volume": 1.3}
    )
    assert coerced_source == "relative_volume"
    assert coerced_value == 1.3


def test_adaptive_rvol_series_and_latest_bar_context() -> None:
    from aef_terminal.features.volume import adaptive_rvol_series, resolve_bar_rvol

    bars = [make_bar(i, 100, 101, 99, 100, volume=1000 + i * 50) for i in range(12)]
    series = adaptive_rvol_series(bars, lookback=8)
    assert len(series) == len(bars)
    assert all(value > 0 for value in series)

    contextual, source = resolve_bar_rvol(
        len(bars) - 1,
        adaptive_series=series,
        features={"rvol_adaptive": 1.42},
        is_latest=True,
    )
    assert contextual == 1.42
    assert source == "rvol_adaptive"


def test_breakout_accumulation_uses_canonical_rvol_on_latest_bar() -> None:
    bars = [
        make_bar(
            i, 100 + i * 0.02, 100.4 + i * 0.02, 99.6 + i * 0.02, 100.1 + i * 0.02, volume=1500
        )
        for i in range(80)
    ]
    result = breakout_accumulation(bars, features={"rvol_adaptive": 1.33})
    assert result["latest"]["rvol_source"] == "rvol_adaptive"
    assert result["latest"]["rvol"] == 1.33


def test_breakout_join_stop_uses_executable_atr_floor() -> None:
    stops = _join_stop_stage(
        upper_level=110.0,
        lower_level=90.0,
        bar_low=105.0,
        bar_high=95.0,
        safe_atr=2.0,
        join_stop_atr=0.01,
        rev_stop_buffer_atr=0.06,
    )

    assert stops.call == pytest.approx(104.88)
    assert stops.put == pytest.approx(95.12)

    level_limited = _join_stop_stage(
        upper_level=110.0,
        lower_level=90.0,
        bar_low=120.0,
        bar_high=80.0,
        safe_atr=2.0,
        join_stop_atr=0.25,
        rev_stop_buffer_atr=0.06,
    )
    assert level_limited.call == pytest.approx(109.5)
    assert level_limited.put == pytest.approx(90.5)


def test_smc_order_block_requires_ob_candle_structure() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100),
        make_bar(1, 100.5, 101.2, 100.2, 100.8),
        make_bar(2, 100.8, 101.0, 100.1, 100.2, volume=900),
        make_bar(3, 100.1, 100.3, 99.7, 99.9, volume=500),
        make_bar(4, 99.8, 100.0, 99.5, 99.7, volume=400),
        make_bar(5, 99.6, 99.8, 99.2, 99.4, volume=350),
        make_bar(6, 99.3, 99.5, 98.8, 99.0, volume=300),
        make_bar(7, 99.0, 101.5, 98.9, 101.2, volume=2200),
    ]
    strict = smc_snapshot(bars, SmcParams(pivot_len=2, ob_min_body_share=0.40))
    loose = smc_snapshot(bars, SmcParams(pivot_len=2, ob_min_body_share=0.10))
    assert "fvg_zones" in loose
    assert "order_blocks" in loose
    assert all(box.get("kind") == "fvg" for box in loose["fvg_zones"])
    assert all(box.get("kind") == "order_block" for box in loose["order_blocks"])
    assert len(loose["order_blocks"]) >= len(strict["order_blocks"])


def test_smc_structure_keeps_fvg_and_order_block_roles_separate(monkeypatch) -> None:
    bars = [
        make_bar(index, 100 + index, 101 + index, 99 + index, 100.5 + index) for index in range(10)
    ]
    fvg_box = {
        "type": "FVG_DN",
        "direction": "short",
        "start_ts": bars[2].ts.isoformat(),
        "end_ts": bars[4].ts.isoformat(),
        "top": 108.0,
        "bottom": 106.0,
        "midpoint": 107.0,
        "state": "active",
    }
    ob_box = {
        "type": "OB_DN",
        "direction": "short",
        "start_ts": bars[3].ts.isoformat(),
        "end_ts": bars[3].ts.isoformat(),
        "top": 105.0,
        "bottom": 104.0,
        "midpoint": 104.5,
        "state": "active",
    }

    monkeypatch.setattr(
        smc_channels_module,
        "_snapshot_for_params",
        lambda *_args, **_kwargs: {
            "version": "test-smc-core",
            "params": {},
            "pivots": [],
            "events": [],
            "fvg_zones": [fvg_box],
            "order_blocks": [ob_box],
            "boxes": [fvg_box, ob_box],
            "levels": [],
            "latest": None,
        },
    )

    result = smc_channels_module.smc_structure(
        bars, SmcStructureParams(), profile=_test_profile(bars)
    )
    smc_overlays = [
        item for item in result["overlays"] if item.get("role") in {"fvg", "order_block"}
    ]

    assert any(
        item.get("type") == "box"
        and item.get("role") == "fvg"
        and item.get("badge_facts")
        == [
            {
                "code": "smc_zone",
                "kind": "fvg",
                "direction": "short",
                "state": "active",
                "retained": False,
            }
        ]
        and item.get("scenario") == "smc_structure_box"
        for item in smc_overlays
    )
    assert any(
        item.get("type") == "box"
        and item.get("role") == "order_block"
        and item.get("badge_facts")
        == [
            {
                "code": "smc_zone",
                "kind": "order_block",
                "direction": "short",
                "state": "active",
                "retained": False,
            }
        ]
        and item.get("scenario") == "smc_structure_box"
        for item in smc_overlays
    )
    assert all(
        item.get("label") in {None, ""} for item in smc_overlays if item.get("type") == "line"
    )
    box_overlays = [item for item in smc_overlays if item["type"] == "box"]
    line_overlays = [item for item in smc_overlays if item["type"] == "line"]
    assert all(item["style"] == "dotted" for item in box_overlays)
    assert all(item["pattern"] == "hatch" for item in box_overlays)
    assert all(item["opacity"] == 0.08 for item in box_overlays)
    assert all(item["border_opacity"] == 0.60 for item in box_overlays)
    assert all(item["pattern_opacity"] == 0.22 for item in box_overlays)
    assert all(item["style"] == "dotted" for item in line_overlays)
    assert all(item["width"] == 0.75 for item in line_overlays)
    assert all(
        not {
            "bg",
            "border",
            "color",
            "pattern_color",
            "label_position",
            "label_font_size",
        }.intersection(item)
        for item in smc_overlays
    )
    assert all(item.get("evidence", {}).get("context") for item in smc_overlays)
    assert any(
        any(
            fact.get("code") == "smc_bias" and fact.get("direction") == "short"
            for fact in item.get("evidence", {}).get("context", [])
        )
        for item in smc_overlays
    )
    assert any(
        item.get("metrics", {}).get("zone_bottom", {}).get("value") == 106.0
        and item.get("metrics", {}).get("zone_top", {}).get("value") == 108.0
        for item in smc_overlays
        if item.get("role") == "fvg"
    )
    assert any(item.get("metrics", {}).get("lifecycle_state") == "active" for item in smc_overlays)
    assert result["fvg_zones"] and result["fvg_zones"][0]["structure_box_type"] == "fvg"
    assert (
        result["order_blocks"] and result["order_blocks"][0]["structure_box_type"] == "order_block"
    )


def test_smc_reaction_zones_preserve_fvg_and_order_block_source_types() -> None:
    bars = [
        make_bar(index, 100 + index * 0.1, 101 + index * 0.1, 99 + index * 0.1, 100.5 + index * 0.1)
        for index in range(12)
    ]
    snapshot = {
        "levels": [],
        "fvg_zones": [
            {
                "kind": "fvg",
                "type": "FVG_DN",
                "direction": "short",
                "top": 101.2,
                "bottom": 100.4,
                "midpoint": 100.8,
                "state": "active",
            }
        ],
        "order_blocks": [
            {
                "kind": "order_block",
                "type": "OB_DN",
                "direction": "short",
                "top": 101.1,
                "bottom": 100.5,
                "midpoint": 100.8,
                "state": "active",
            }
        ],
    }

    zones = smc_channels_module._structure_reaction_zones(
        bars,
        snapshot,
        atr=1.0,
        params=SmcStructureParams(zone_merge_atr=1.0),
    )

    assert zones
    assert zones[0]["has_fvg"] is True
    assert zones[0]["has_order_block"] is True
    assert zones[0]["source_types"] == ["fvg", "order_block"]


def test_smc_structure_retains_recent_filled_fvg_with_active_boxes() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100),
        make_bar(1, 101, 102, 100.5, 101.5),
        make_bar(2, 103.2, 103.8, 103.0, 103.6),
        make_bar(3, 103.4, 103.5, 100.8, 101.0),
        make_bar(4, 100.5, 100.7, 99.8, 100.0),
        make_bar(5, 98.5, 99.2, 98.0, 98.1),
    ]

    result = smc_structure(
        bars,
        params=SmcStructureParams(pivot_len=2, fvg_min_atr=0.05, fvg_displacement_atr=0.0),
    )

    filled = [box for box in result["boxes"] if box["state"] == "filled"]
    active = [box for box in result["boxes"] if box["state"] == "active"]
    assert filled and active
    assert filled[-1]["lifecycle"]["retained"] is True
    retained_overlays = [item for item in result["overlays"] if item.get("role") == "retained_fvg"]
    assert retained_overlays
    assert all(item.get("retention") == "active" for item in retained_overlays)
    assert any(
        item.get("type") == "box"
        and item.get("badge_facts")
        == [
            {
                "code": "smc_zone",
                "kind": "fvg",
                "direction": "long",
                "state": "filled",
                "retained": True,
            }
        ]
        for item in retained_overlays
    )
    assert all(
        item.get("label") in {None, ""} for item in retained_overlays if item.get("type") == "line"
    )


def test_shared_smc_detects_bos_choch_without_repeating_same_swing() -> None:
    prices = [100, 101, 105, 102, 100, 98, 101, 103, 106, 107, 106, 97, 96]
    bars = [
        make_bar(index, price - 0.2, price + 0.5, price - 0.5, price, 1200)
        for index, price in enumerate(prices)
    ]

    result = smc_snapshot(bars, SmcParams(pivot_len=2))
    structure_events = [
        event["event"]
        for event in result["events"]
        if event["event"] in {"BOS↑", "BOS↓", "CHOCH↑", "CHOCH↓"}
    ]

    assert structure_events.count("BOS↑") == 1
    assert "CHOCH↓" in structure_events
    assert result["levels"]
    assert {level["role"] for level in result["levels"]} <= {
        "swing_high",
        "swing_low",
        "previous_swing_high",
        "previous_swing_low",
    }


def test_shared_smc_tracks_fvg_mitigation_and_fill_state() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 101, 102, 100.5, 101.5, 1000),
        make_bar(2, 103.5, 105, 103, 104.5, 1600),
        make_bar(3, 104, 104.4, 101.6, 102.2, 1300),
        make_bar(4, 102, 102.4, 100.8, 101.1, 1300),
    ]

    result = smc_snapshot(bars, SmcParams(pivot_len=2, fvg_min_atr=0.05, fvg_displacement_atr=0.0))
    bull_zones = [box for box in result["fvg_zones"] if box["direction"] == "long"]

    assert bull_zones
    assert "tooltip" not in bull_zones[-1]
    assert bull_zones[-1]["scenario"] == "smc_fvg_zone"
    assert bull_zones[-1]["mitigated"]
    assert bull_zones[-1]["filled"]


def test_shared_smc_fvg_requires_displacement_like_pine() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 101, 102, 100.5, 101.5, 1000),
        make_bar(2, 103.2, 103.7, 103.0, 103.25, 1600),
    ]

    strict = smc_snapshot(bars, SmcParams(pivot_len=2, fvg_min_atr=0.05))
    loose = smc_snapshot(bars, SmcParams(pivot_len=2, fvg_min_atr=0.05, fvg_displacement_atr=0.0))

    assert not strict["fvg_zones"]
    assert loose["fvg_zones"]


def test_structure_indicators_report_typed_warmup() -> None:
    bars = [make_bar(index, 100, 101, 99, 100.1, 1000) for index in range(10)]

    w5 = w5_structure([], bars)
    wolfe = wolfe_structure(bars)

    assert w5["availability"] == {
        "state": "blocked",
        "reason_code": "w5_structure_warmup",
        "required_bars": 40,
        "available_bars": 10,
    }
    assert wolfe["availability"] == {
        "state": "blocked",
        "reason_code": "wolfe_structure_warmup",
        "required_bars": 20,
        "available_bars": 10,
    }
    for indicator_id, result in (("w5_structure", w5), ("wolfe_structure", wolfe)):
        normalized = attach_indicator_status(
            indicator_id,
            result,
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="confirmed",
        )
        assert normalized["status"]["state_code"] == "blocked_context"
        assert normalized["status"]["reason_code"].endswith("_warmup")


def test_w5_structure_flags_latest_exhaustion_zone() -> None:
    bars = []
    price = 100.0
    for index in range(65):
        drift = 0.35 if index < 58 else 0.55
        open_ = price
        close = price + drift
        high = close + 0.35
        low = open_ - 0.15
        bars.append(make_bar(index, open_, high, low, close, 900 + index * 4))
        price = close
    bars.append(make_bar(66, price, price + 2.6, price - 0.25, price + 0.25, 5200))

    result = w5_structure([], bars)
    latest = result["latest"]

    assert latest is not None
    assert latest["direction"] == "short"
    assert latest["action"] in {"CANDIDATE", "WATCH", "ARM"}
    assert latest["score"] >= 42
    assert latest["trigger_state"] in {"ARMED", "GO"}
    assert latest["trigger"] == round(bars[-2].low, 4)
    assert "micro_bos_trigger" in latest["trigger_event"]["code"]
    assert "fib_zone" in latest
    assert "weighted_top_score" in latest
    assert "fast_top_score" in latest
    assert "context_top_score" in latest
    assert "score_stack" in latest["details"]
    assert "runaway_veto" in latest
    assert latest["armed_expires_bars"] >= 0
    assert latest["candidate_finality"] == "confirmed"
    assert any(item["type"] == "label" for item in result["overlays"])
    assert (
        signal_candidates_from_w5_indicator(
            result,
            score_floor=55.0,
            atr_value=None,
            features=None,
        )
        == []
    )
    candidates = signal_candidates_from_w5_indicator(
        result,
        score_floor=42.0,
        atr_value=None,
        features=None,
    )
    assert candidates
    assert candidates[-1].name == "w5_exhaustion"
    assert candidates[-1].direction == Direction.SHORT
    assert "trigger_state" in candidates[-1].details
    assert "weighted_top_score" in candidates[-1].details
    assert candidates[-1].details["plan_complete"] is True
    assert candidates[-1].details["plan_coherent"] is True


def test_w5_lifecycle_cached_calculation_matches_causal_prefix_scan() -> None:
    bars = []
    price = 100.0
    for index in range(72):
        drift = 0.35 if index < 58 else 0.55
        open_ = price
        close = price + drift
        bars.append(
            make_bar(
                index,
                open_,
                close + 0.35,
                open_ - 0.15,
                close,
                900 + index * 4,
            )
        )
        price = close
    bars.append(make_bar(72, price, price + 2.6, price - 0.25, price + 0.25, 5200))
    retention_bars = 24
    defaults = w5_structure_module.DEFAULT_INDICATOR_SETTINGS
    expected = []
    start = max(40, len(bars) - retention_bars)
    last_index = len(bars) - 1
    calculation_context = w5_structure_module._w5_lifecycle_calculation_context(
        bars,
        defaults=defaults,
        first_end=start,
    )
    for end in range(start, len(bars) + 1):
        state = w5_structure_module._w5_latest_state(
            bars[:end],
            defaults=defaults,
        )
        assert (
            w5_structure_module._w5_latest_state(
                bars[:end],
                defaults=defaults,
                _lifecycle_context=calculation_context,
            )
            == state
        )
        if (
            not state
            or state.get("action") == "WAIT"
            or state.get("direction") == Direction.FLAT.value
        ):
            continue
        event = dict(state)
        age_bars = last_index - (end - 1)
        event["age_bars"] = age_bars
        event["retained"] = age_bars > 0
        if event.get("armed_expires_bars") is not None:
            event["expires_in_bars"] = max(
                int(event.get("armed_expires_bars") or 0) - age_bars,
                0,
            )
        if age_bars > 0:
            event["candidate_finality"] = (
                CandidateFinality.EXPIRED.value
                if int(event.get("expires_in_bars") or 0) <= 0
                else CandidateFinality.RETAINED.value
            )
        expected.append(event)

    assert (
        w5_structure_module._w5_lifecycle_events(
            bars,
            retention_bars=retention_bars,
            defaults=defaults,
        )
        == expected
    )


def test_w5_armed_expiry_uses_current_timeframe_minutes() -> None:
    bars = []
    price = 100.0
    base_ts = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    for index in range(65):
        drift = 0.35 if index < 58 else 0.55
        open_ = price
        close = price + drift
        high = close + 0.35
        low = open_ - 0.15
        bars.append(
            make_bar(
                index,
                open_,
                high,
                low,
                close,
                900 + index * 4,
                timeframe="60m",
                base_ts=base_ts,
                step_minutes=60,
            )
        )
        price = close
    bars.append(
        make_bar(
            66,
            price,
            price + 2.6,
            price - 0.25,
            price + 0.25,
            5200,
            timeframe="60m",
            base_ts=base_ts,
            step_minutes=60,
        )
    )

    latest = w5_structure([], bars)["latest"]

    assert latest is not None
    assert latest["armed_expires_bars"] > 0
    expires_at = datetime.fromisoformat(latest["armed_expires_at"])
    assert expires_at == bars[-1].ts + timedelta(minutes=latest["armed_expires_bars"] * 60)


def test_w5_bos_trigger_uses_immediate_previous_candle() -> None:
    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.5, 1000),
        make_bar(1, 100.5, 102.0, 100.0, 101.5, 1000),
        make_bar(2, 101.5, 101.8, 99.8, 99.9, 1000),
    ]

    short_trigger, short_state = _w5_bos_trigger_state(bars, Direction.SHORT)
    long_trigger, long_state = _w5_bos_trigger_state(bars, Direction.LONG)

    assert short_trigger == bars[-2].low
    assert short_state == "GO"
    assert long_trigger == bars[-2].high
    assert long_state == "ARMED"


def test_w5_swing_mode_uses_wider_bos_window() -> None:
    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.5, 1000),
        make_bar(1, 100.5, 103.0, 98.0, 102.5, 1000),
        make_bar(2, 102.5, 104.0, 100.0, 103.5, 1000),
        make_bar(3, 103.5, 105.0, 101.0, 104.5, 1000),
        make_bar(4, 104.5, 106.0, 102.0, 105.5, 1000),
        make_bar(5, 105.5, 105.8, 99.2, 99.5, 1000),
    ]

    scalp_trigger, scalp_state = _w5_bos_trigger_state(bars, Direction.SHORT, "scalp")
    swing_trigger, swing_state = _w5_bos_trigger_state(bars, Direction.SHORT, "swing")

    assert scalp_trigger == bars[-2].low
    assert scalp_state == "GO"
    assert swing_trigger == min(bar.low for bar in bars[-5:-1])
    assert swing_state == "ARMED"


def test_w5_fib_extension_zones_extend_past_prior_range() -> None:
    bars = []
    price = 100.0
    for index in range(65):
        open_ = price
        close = price + 0.35
        bars.append(make_bar(index, open_, close + 0.35, open_ - 0.15, close, 900 + index * 4))
        price = close
    prior_high = max(bar.high for bar in bars[-34:])
    prior_low = min(bar.low for bar in bars[-34:])
    leg = prior_high - prior_low
    bars.append(make_bar(66, price, price + leg * 0.42, price - 0.25, price + 0.25, 5200))

    top = w5_structure([], bars)["latest"]["fib_zone"]

    assert top["low"] == round(prior_high + leg * 0.236, 4)
    assert top["high"] == round(prior_high + leg * 0.618, 4)
    assert top["low"] > prior_high

    bars = []
    price = 130.0
    for index in range(65):
        open_ = price
        close = price - 0.35
        bars.append(make_bar(index, open_, open_ + 0.15, close - 0.35, close, 900 + index * 4))
        price = close
    prior_high = max(bar.high for bar in bars[-34:])
    prior_low = min(bar.low for bar in bars[-34:])
    leg = prior_high - prior_low
    bars.append(make_bar(66, price, price + 0.25, price - leg * 0.42, price - 0.25, 5200))

    bottom = w5_structure([], bars)["latest"]["fib_zone"]

    assert bottom["low"] == round(prior_low - leg * 0.618, 4)
    assert bottom["high"] == round(prior_low - leg * 0.236, 4)
    assert bottom["high"] < prior_low


def test_w5_structure_uses_lower_timeframe_context_score() -> None:
    bars = []
    price = 100.0
    for index in range(65):
        drift = 0.35 if index < 58 else 0.55
        open_ = price
        close = price + drift
        bars.append(make_bar(index, open_, close + 0.35, open_ - 0.15, close, 900 + index * 4))
        price = close
    bars.append(make_bar(66, price, price + 2.6, price - 0.25, price + 0.25, 5200))
    lower_tf = [
        Bar(
            symbol=bar.symbol,
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timeframe="1m",
            source="security:1m",
        )
        for bar in bars
    ]

    result = w5_structure([], bars, mtf_context={"1m": lower_tf})
    latest = result["latest"]

    assert result["version"] == "1.5-python-confirmed-context"
    assert latest["mtf_top_score"] > 0
    assert latest["mtf_bottom_score"] == 0
    assert latest["mtf_context"]["items"][-1]["timeframe"] == "1m"
    assert latest["details"]["score_stack"]["mtf_top"] == latest["mtf_top_score"]
    candidates = signal_candidates_from_w5_indicator(
        result,
        score_floor=42.0,
        atr_value=None,
        features=None,
    )
    assert candidates[-1].details["mtf_top_score"] == latest["mtf_top_score"]


def test_w5_structure_retains_event_on_its_original_bar() -> None:
    bars = []
    price = 100.0
    for index in range(65):
        drift = 0.35 if index < 58 else 0.55
        open_ = price
        close = price + drift
        bars.append(make_bar(index, open_, close + 0.35, open_ - 0.15, close, 900 + index * 4))
        price = close
    bars.append(make_bar(66, price, price + 2.6, price - 0.25, price + 0.25, 5200))
    event_ts = bars[-1].ts.isoformat()
    price = bars[-1].close
    for offset in range(1, 4):
        bars.append(make_bar(66 + offset, price, price + 0.35, price - 0.55, price - 0.25, 1200))
        price = bars[-1].close

    result = w5_structure([], bars)

    retained = [event for event in result["events"] if event["ts"] == event_ts]
    assert retained
    assert retained[-1]["retained"]
    assert retained[-1]["age_bars"] == 3
    assert retained[-1]["candidate_finality"] in {"retained", "expired"}
    assert result["lifecycle"]["retained_events"] >= 1
    labels = [
        item for item in result["overlays"] if item["type"] == "label" and item["ts"] == event_ts
    ]
    assert labels


def test_w5_retained_and_expired_events_never_repromote() -> None:
    current = {
        "candidate_finality": "confirmed",
        "direction": "long",
        "action": "GO",
        "score": 80.0,
        "trigger": 100.0,
        "stop": 99.0,
        "target": 102.0,
        "trigger_event": {"code": "w5_current"},
    }
    confirmed = signal_candidates_from_w5_indicator(
        {"latest": current, "events": []},
        score_floor=55.0,
        atr_value=None,
        features=None,
    )

    assert len(confirmed) == 1
    for finality in ("provisional", "retained", "expired"):
        display_only = {**current, "candidate_finality": finality}
        assert (
            signal_candidates_from_w5_indicator(
                {"latest": display_only, "events": [display_only]},
                score_floor=55.0,
                atr_value=None,
                features=None,
            )
            == []
        )


def test_w5_structure_keeps_candidate_event_when_model_waits() -> None:
    bars = [make_bar(index, 100.0, 100.5, 99.5, 100.0, 900) for index in range(45)]
    candidate = SignalCandidate(
        "w5_exhaustion",
        Direction.SHORT,
        65.0,
        100.5,
        "upper rejection",
        DomainFact("upper_rejection"),
        kind=ScenarioKind.FADE,
        source="w5",
        role="exhaustion",
        finality=CandidateFinality.CONFIRMED,
    )

    result = w5_structure([candidate], bars)

    assert result["events"]
    assert result["latest"]["code"] == "W5_EXHAUSTION"
    assert result["latest"]["action"] == "WATCH"
    assert result["latest"]["scenario"] == "wave_5_exhaustion"
    assert any(item["type"] == "label" for item in result["overlays"])


def test_w5_structure_keeps_one_direction_per_bar() -> None:
    bars = [make_bar(index, 100.0, 100.5, 99.5, 100.0, 900) for index in range(45)]
    candidates = [
        SignalCandidate(
            "w5_exhaustion",
            Direction.SHORT,
            66.0,
            100.5,
            "upper rejection",
            DomainFact("upper_rejection"),
            kind=ScenarioKind.FADE,
            source="w5",
            role="exhaustion",
            finality=CandidateFinality.CONFIRMED,
        ),
        SignalCandidate(
            "w5_exhaustion",
            Direction.LONG,
            74.0,
            99.5,
            "lower rejection",
            DomainFact("lower_rejection"),
            kind=ScenarioKind.FADE,
            source="w5",
            role="exhaustion",
            finality=CandidateFinality.CONFIRMED,
        ),
    ]

    result = w5_structure(candidates, bars)

    assert len(result["events"]) == 1
    assert result["latest"]["direction"] == "long"
    label_overlays = [item for item in result["overlays"] if item["type"] == "label"]
    assert len(label_overlays) == 1
    assert label_overlays[0]["side"] == "below"
    assert "lines" not in label_overlays[0]
    assert label_overlays[0]["code"].startswith("W5_")
    assert label_overlays[0]["action"] == "WATCH"


def test_wolfe_structure_returns_path_from_confirmed_one_two_three() -> None:
    prices = [
        101,
        100,
        99,
        97,
        95,
        94,
        96,
        98,
        102,
        105,
        108,
        105,
        101,
        97,
        93,
        90,
        92,
        94,
        96,
        98,
        100,
        102,
        104,
    ]
    bars = [
        make_bar(index, price, price + 1, price - 1, price, 1000)
        for index, price in enumerate(prices)
    ]

    result = wolfe_structure(bars, pivot_len=2)

    assert result["version"] == "1.1-python"
    assert result["paths"]
    assert result["paths"][-1]["direction"] == "long"
    assert result["paths"][-1]["candidate_finality"] == "provisional"


def test_wolfe_structure_accepts_display_noise_settings() -> None:
    bars = [
        make_bar(
            index, 100 + (index % 5), 101 + (index % 5), 99 + (index % 5), 100 + (index % 5), 1000
        )
        for index in range(30)
    ]

    result = wolfe_structure(
        bars,
        pivot_len=2,
        confirmed_limit=3,
        show_candidates=False,
        show_price_target=False,
        line_opacity=0.25,
    )

    assert result["settings"]["confirmed_limit"] == 3
    assert result["settings"]["show_candidates"] is False
    assert result["settings"]["show_price_target"] is False
    assert result["settings"]["line_opacity"] == 0.25


def test_wolfe_structure_feeds_decision_candidates() -> None:
    prices = [
        101,
        100,
        99,
        97,
        95,
        94,
        96,
        98,
        102,
        105,
        108,
        105,
        101,
        97,
        93,
        90,
        92,
        94,
        96,
        98,
        100,
        102,
        104,
    ]
    bars = [
        make_bar(index, price, price + 1, price - 1, price, 1000)
        for index, price in enumerate(prices)
    ]

    result = wolfe_structure(bars, pivot_len=2)
    candidates = signal_candidates_from_wolfe_indicator(
        result,
        score_floor=55.0,
        atr_value=None,
        features=None,
    )

    for candidate in candidates:
        assert candidate.details.get("trigger") is not None
        assert candidate.details.get("stop") is not None
        assert candidate.details.get("target") is not None
        assert candidate.details.get("plan_complete") is True
        assert candidate.details.get("plan_coherent") is True
        assert candidate.details.get("rr") is not None


def test_wolfe_latest_prefers_fresh_context_over_stale_higher_score() -> None:
    stale_short = {
        "id": "stale-short",
        "ts": "2026-06-30T17:00:00+00:00",
        "score": 99.0,
        "direction": "short",
    }
    fresh_long = {
        "id": "fresh-long",
        "ts": "2026-06-30T18:30:00+00:00",
        "score": 90.0,
        "direction": "long",
    }

    latest = _latest_wolfe_item(
        [{"ts": "2026-06-30T18:31:00+00:00", "direction": "short"}],
        [],
        [stale_short, fresh_long],
    )

    assert latest is fresh_long


def test_wolfe_signal_candidates_admit_only_confirmed_patterns() -> None:
    indicator = {
        "events": [
            {
                "code": "WOLFE_CONFIRMED",
                "candidate_finality": "confirmed",
                "direction": "long",
                "score": 59.0,
                "price": 100.0,
                "invalid": 98.0,
                "target": 104.0,
                "trigger_event": {"code": "wolfe_confirmed"},
            },
            {
                "code": "WOLFE_CONFIRMED",
                "candidate_finality": "retained",
                "direction": "short",
                "score": 99.0,
                "price": 101.0,
                "invalid": 103.0,
                "target": 97.0,
                "trigger_event": {"code": "old_wolfe_confirmed"},
            },
        ],
        "candidates": [
            {
                "code": "WOLFE_CANDIDATE",
                "candidate_finality": "provisional",
                "direction": "short",
                "score": 76.0,
                "price": 101.0,
                "invalid": 103.0,
                "target": 97.0,
                "trigger_event": {"code": "wolfe_candidate"},
            }
        ],
        "paths": [
            {
                "candidate_finality": "provisional",
                "direction": "long",
                "score": 63.0,
                "projected_p5": 99.0,
                "invalid": 97.0,
                "target": 103.0,
                "trigger_event": {"code": "projected_p5_reaction"},
            },
            {
                "candidate_finality": "provisional",
                "direction": "short",
                "score": 82.0,
                "projected_p5": 102.0,
                "invalid": 104.0,
                "target": 98.0,
                "trigger_event": {"code": "projected_p5_reaction"},
            },
        ],
    }

    candidates = signal_candidates_from_wolfe_indicator(
        indicator,
        score_floor=55.0,
        atr_value=None,
        features=None,
    )

    assert [candidate.score for candidate in candidates] == [59.0]
    assert [candidate.reason_code for candidate in candidates] == ["wolfe_confirmed"]
    assert candidates[0].finality is CandidateFinality.CONFIRMED


def test_wolfe_only_newly_confirmed_p5_has_decision_authority() -> None:
    assert (
        _confirmed_wolfe_finality(bar_count=30, p5_index=27, pivot_len=2)
        is CandidateFinality.CONFIRMED
    )
    assert (
        _confirmed_wolfe_finality(bar_count=30, p5_index=26, pivot_len=2)
        is CandidateFinality.RETAINED
    )


def test_wolfe_signal_candidate_preserves_zero_trigger() -> None:
    indicator = {
        "events": [
            {
                "code": "WOLFE_CONFIRMED",
                "candidate_finality": "confirmed",
                "direction": "long",
                "score": 59.0,
                "trigger": 0.0,
                "price": 100.0,
                "invalid": -1.0,
                "target": 1.0,
                "trigger_event": {"code": "zero_wolfe_trigger"},
            }
        ]
    }

    candidates = signal_candidates_from_wolfe_indicator(
        indicator,
        score_floor=55.0,
        atr_value=None,
        features=None,
    )

    assert len(candidates) == 1
    assert candidates[0].level == 0.0
    assert candidates[0].details["trigger"] == 0.0
