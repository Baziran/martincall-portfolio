from datetime import datetime, timedelta, timezone

from aef_terminal.domain import Bar
from aef_terminal.indicators.modules.absorption_trap import (
    AbsorptionTrapParams,
    _option_flow_override,
    absorption_trap as _absorption_trap,
)
from aef_terminal.runtime.instruments import PROFILES


def absorption_trap(bars, params=None, **kwargs):
    return _absorption_trap(bars, profile=PROFILES["ES"], params=params, **kwargs)


def make_bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000,
) -> Bar:
    base_ts = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    return Bar(
        symbol="ES",
        ts=base_ts + timedelta(minutes=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def test_absorption_trap_times_out_without_go_reclaim() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.2, 1000) for i in range(6)]
    bars.append(make_bar(6, 100.2, 102.2, 100.0, 101.7, 2600))
    bars.append(make_bar(7, 101.6, 101.8, 100.6, 101.0, 2600))
    bars.extend(make_bar(i, 100.8, 101.1, 99.8, 100.5, 1000) for i in range(8, 15))

    params = AbsorptionTrapParams(
        level_lookback=6,
        attack_max_bars=8,
        min_score=1,
        cooldown_bars=0,
    )

    timed_out = absorption_trap(bars, params)

    assert timed_out["events"] == []
    assert timed_out["latest"]["state"] == "IDLE"
    assert timed_out["latest"]["signal"]["action"] == "WAIT"

    late_reclaim = [*bars, make_bar(15, 100.7, 102.35, 99.7, 100.2, 3200)]

    assert absorption_trap(late_reclaim, params)["events"] == []


def test_absorption_trap_conservative_blocks_short_against_strong_uptrend_without_rounding() -> (
    None
):
    bars = [
        make_bar(i, 100.0 + i * 0.2, 101.0 + i * 0.2, 99.4 + i * 0.2, 100.5 + i * 0.2, 1000)
        for i in range(60)
    ]
    bars.append(make_bar(60, 112.0, 114.0, 111.8, 113.4, 2600))
    bars.append(make_bar(61, 113.4, 113.7, 112.6, 113.0, 1200))
    bars.append(make_bar(62, 113.0, 114.4, 111.9, 112.4, 2300))

    result = absorption_trap(
        bars,
        AbsorptionTrapParams(
            level_lookback=20,
            attack_max_bars=6,
            min_score=60,
            mode="Conservative",
            conservative_level_lookback=20,
            conservative_attack_max_bars=6,
            conservative_min_score=60,
            conservative_min_pierce_atr=0.05,
            max_counter_trend_slope_atr=0.05,
            max_counter_trend_distance_atr=0.50,
        ),
    )

    latest = result["latest"]
    assert result["events"] == []
    assert latest["state"] == "BLOCKED_TREND"
    assert latest["signal"]["action"] == "BLOCK"
    assert latest["signal"]["blocked"] is True
    assert latest["blocked_reason"] == "strong_uptrend_without_lower_high"


def test_absorption_trap_conservative_uses_structural_stop_after_rounding_reclaim() -> None:
    bars = [
        make_bar(i, 100.0 + i * 0.2, 101.0 + i * 0.2, 99.4 + i * 0.2, 100.5 + i * 0.2, 1000)
        for i in range(60)
    ]
    bars.append(make_bar(60, 112.0, 114.0, 111.8, 113.4, 2600))
    bars.append(make_bar(61, 113.4, 113.7, 112.6, 113.0, 1200))
    bars.append(make_bar(62, 113.0, 114.05, 111.9, 112.4, 2300))

    result = absorption_trap(
        bars,
        AbsorptionTrapParams(
            level_lookback=20,
            attack_max_bars=6,
            min_score=60,
            mode="Conservative",
            conservative_level_lookback=20,
            conservative_attack_max_bars=6,
            conservative_min_score=60,
            conservative_min_pierce_atr=0.05,
            max_counter_trend_slope_atr=0.05,
            max_counter_trend_distance_atr=0.50,
        ),
    )

    assert result["events"]
    event = result["events"][-1]
    assert event["signal"]["action"] == "GO"
    assert event["stop"] == 114.3
    assert "cascade_high_1_tick" in event["risk"]["code"]
    wall_labels = [
        item
        for item in result["overlays"]
        if item.get("type") == "label" and item.get("code") == "ABS_TRAP"
    ]
    assert wall_labels
    assert wall_labels[-1]["glyph_kind"] == "absorption_wall"
    assert wall_labels[-1]["wall_direction"] == "down"
    assert "lines" not in wall_labels[-1]
    assert wall_labels[-1]["tone"] == "negative"
    wall_lines = [
        item
        for item in result["overlays"]
        if item.get("type") == "line" and item.get("role") == "absorption_trap"
    ]
    assert wall_lines[-1]["tone"] == "negative"
    assert "color" not in wall_lines[-1]
    assert result["latest"]["state"] == "FAILED_HIGH"
    assert result["latest"]["signal"]["action"] == "GO"


def test_absorption_trap_records_call_flow_conflict_without_flipping_failed_high() -> None:
    bars = [
        make_bar(i, 100.0 + i * 0.2, 101.0 + i * 0.2, 99.4 + i * 0.2, 100.5 + i * 0.2, 1000)
        for i in range(60)
    ]
    bars.append(make_bar(60, 112.0, 114.0, 111.8, 113.4, 2600))
    bars.append(make_bar(61, 113.4, 113.7, 112.6, 113.0, 1200))
    bars.append(make_bar(62, 113.0, 114.05, 111.9, 112.4, 2300))

    result = absorption_trap(
        bars,
        AbsorptionTrapParams(
            level_lookback=20,
            attack_max_bars=6,
            min_score=60,
            max_counter_trend_slope_atr=99,
            max_counter_trend_distance_atr=99,
        ),
        option_flow={
            "call_volume_delta": 12.0,
            "put_volume_delta": 120.0,
            "call_rate_per_minute": 12.0,
            "put_rate_per_minute": 1.2,
            "option_rvol": 2.4,
        },
    )

    assert result["events"]
    event = result["events"][-1]
    assert event["code"] == "ABS_HIGH"
    assert event["direction"] == "short"
    assert event["option_flow_override"] == ""
    assert event["option_flow_bias"] == "bullish"
    assert event["option_flow_conflict"] is True
    assert event["signal"]["direction"] == "short"
    assert event["call_volume_delta"] == 12.0
    assert event["put_volume_delta"] == 120.0
    assert event["call_rate_per_minute"] == 12.0
    assert event["put_rate_per_minute"] == 1.2
    assert "context_lines" not in event
    assert event["metrics"]["option_flow_conflict"] is True
    assert event["metrics"]["option_call_rate_per_minute"]["value"] == 12.0
    assert event["metrics"]["option_put_rate_per_minute"]["value"] == 1.2


def test_absorption_trap_does_not_use_rates_without_typed_rvol() -> None:
    params = AbsorptionTrapParams(option_flow_min_rate=1.0, option_flow_min_rvol=1.25)

    assert (
        _option_flow_override(
            "high",
            {"call_rate_per_minute": 12.0, "put_rate_per_minute": 1.0},
            params,
        )
        is None
    )


def test_absorption_trap_uses_tick_delta_pressure_when_available() -> None:
    bars = [
        make_bar(i, 100.0 + i * 0.2, 101.0 + i * 0.2, 99.4 + i * 0.2, 100.5 + i * 0.2, 1000)
        for i in range(60)
    ]
    bars.append(make_bar(60, 112.0, 114.0, 111.8, 113.4, 2600))
    bars.append(make_bar(61, 113.4, 113.7, 112.6, 113.0, 1200))
    bars.append(make_bar(62, 113.0, 114.05, 111.9, 112.4, 2300))

    result = absorption_trap(
        bars,
        AbsorptionTrapParams(
            level_lookback=20,
            attack_max_bars=6,
            min_score=60,
            max_counter_trend_slope_atr=99,
            max_counter_trend_distance_atr=99,
        ),
        tick_flow={
            "decision_eligible": True,
            "delta": [
                {
                    "ts": bars[60].ts.isoformat(),
                    "total_volume": 3000,
                    "net_delta": 2600,
                    "trade_count": 100,
                },
                {
                    "ts": bars[62].ts.isoformat(),
                    "total_volume": 1000,
                    "net_delta": 800,
                    "trade_count": 40,
                },
            ],
        },
    )

    assert result["params"]["pressure_source"] == "tick"
    assert result["events"]
    event = result["events"][-1]
    assert event["pressure_source"] == "tick"
    assert "context_lines" not in event
    assert any("tick_delta_pressure" in fact["code"] for fact in event["evidence"]["context"])


def test_absorption_trap_conservative_requires_real_second_assault_of_high() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.2, 1000) for i in range(6)]
    bars.append(make_bar(6, 100.2, 102.2, 100.0, 101.7, 2600))
    bars.append(make_bar(7, 101.6, 101.82, 100.2, 100.6, 1200))
    common = dict(
        level_lookback=6,
        attack_max_bars=6,
        min_score=1,
        cooldown_bars=0,
        conservative_level_lookback=6,
        conservative_attack_max_bars=6,
        conservative_min_score=1,
        conservative_min_pierce_atr=0.05,
        max_counter_trend_slope_atr=99,
        max_counter_trend_distance_atr=99,
    )

    balanced = absorption_trap(bars, AbsorptionTrapParams(mode="Balanced", **common))
    conservative = absorption_trap(bars, AbsorptionTrapParams(mode="Conservative", **common))

    assert balanced["latest"]["state"].startswith("SECOND_ATTACK")
    assert conservative["events"] == []
    assert conservative["latest"]["state"] == "WAIT_TOP_RETEST"
    assert (
        conservative["latest"]["repeat_tolerance_atr"] < balanced["latest"]["repeat_tolerance_atr"]
    )


def test_absorption_trap_exports_der_anomaly_events_for_blocked_edges() -> None:
    high_bars = [make_bar(i, 100.0, 101.0, 99.4, 100.5, 1000) for i in range(20)]
    high_bars.append(make_bar(20, 100.8, 102.4, 100.5, 101.9, 2600))
    high_bars.append(make_bar(21, 101.0, 101.5, 100.2, 100.8, 1000))
    high_bars.append(make_bar(22, 100.8, 102.35, 100.4, 100.9, 2600))
    low_bars = [make_bar(i, 100.5, 101.4, 99.0, 100.0, 1000) for i in range(20)]
    low_bars.append(make_bar(20, 99.2, 100.0, 97.6, 98.1, 2600))
    low_bars.append(make_bar(21, 99.0, 100.2, 98.5, 99.4, 1000))
    low_bars.append(make_bar(22, 99.2, 100.5, 97.65, 99.1, 2600))
    params = AbsorptionTrapParams(
        level_lookback=20,
        attack_max_bars=6,
        min_score=60,
        max_counter_trend_slope_atr=99,
        max_counter_trend_distance_atr=99,
        min_significant_delta=50,
    )

    short_blocked = absorption_trap(
        high_bars,
        params,
        tick_flow={
            "decision_eligible": True,
            "delta": [
                {
                    "ts": high_bars[20].ts.isoformat(),
                    "total_volume": 3000,
                    "net_delta": 2600,
                    "trade_count": 100,
                },
                {
                    "ts": high_bars[22].ts.isoformat(),
                    "total_volume": 3000,
                    "net_delta": -800,
                    "trade_count": 100,
                },
            ],
        },
    )
    long_blocked = absorption_trap(
        low_bars,
        params,
        tick_flow={
            "decision_eligible": True,
            "delta": [
                {
                    "ts": low_bars[20].ts.isoformat(),
                    "total_volume": 3000,
                    "net_delta": -2600,
                    "trade_count": 100,
                },
                {
                    "ts": low_bars[22].ts.isoformat(),
                    "total_volume": 3000,
                    "net_delta": 800,
                    "trade_count": 100,
                },
            ],
        },
    )

    assert short_blocked["events"][-1] == {
        "ts": high_bars[22].ts.isoformat(),
        "state": "BLOCKED_ANOMALY",
        "der_status": "SHORT_SQUEEZE_RISK",
    }
    assert long_blocked["events"][-1] == {
        "ts": low_bars[22].ts.isoformat(),
        "state": "BLOCKED_ANOMALY",
        "der_status": "LONG_LIQUIDATION_RISK",
    }
    for result, trigger_code in (
        (short_blocked, "momentum_long_wait_exhaustion"),
        (long_blocked, "momentum_short_wait_exhaustion"),
    ):
        anomaly_labels = [
            item
            for item in result["overlays"]
            if item.get("type") == "label" and item.get("code") == "DER_ANOMALY"
        ]
        assert anomaly_labels
        assert "lines" not in anomaly_labels[-1]
        assert anomaly_labels[-1]["scenario"] == "der_anomaly"
        assert anomaly_labels[-1]["trigger_event"] == {"code": trigger_code}
