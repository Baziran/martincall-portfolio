import inspect
from datetime import datetime, timedelta, timezone

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
from aef_terminal.engine.analyze import analyze_bars as _analyze_bars, apply_vsa_fuel_sfp_cooldown
from aef_terminal.engine.analyze import features as analyze_features
from aef_terminal.engine.analyze.indicator_status import attach_indicator_status
from aef_terminal.engine.analyze.inputs import build_analyze_inputs
from aef_terminal.engine.snapshot.vix_context import load_vix_context_input
from aef_terminal.engine.sentiment import (
    build_direction_sentiment as _build_direction_sentiment,
    tick_flow_bias_from_context,
)
from aef_terminal.engine import sentiment as sentiment_module
from aef_terminal.engine.trade_setup import (
    build_trade_setup_card,
    trade_setup_execution_authority,
)
from aef_terminal.engine.vsa_context import (
    VSA_VOLUME_CONTRACT,
    VsaVolumeParams,
    align_vsa_volume_series,
    build_vsa_volume_context as _vsa_volume,
    build_vsa_volume_context_for_instrument,
)
from aef_terminal.data.providers import bind_provider_instrument, route_instrument
from aef_terminal.data.gex.activity import _gex_activity_delta
from aef_terminal.data.instrument_identity import provider_symbol
from tests.provider_payloads import (
    coinbase_btc_payload,
    explicit_vwap_session_for_bars,
    ibkr_future_payload,
    ibkr_stock_payload,
    instrument_with_bar_sessions,
)
from tests.features_test_support import (
    breakout_accumulation_for_test as breakout_accumulation,
    choose_test_decision as choose_decision,
    make_bar,
    smc_channels_for_test as smc_channels,
    instrument_profile_for_test as _test_profile,
)
from aef_terminal.features.candles import candle_features
from aef_terminal.features.market_context import (
    liquidity_void_context,
    micro_range_context,
    mtf_trend_context,
    provider_new_york_range_context,
    provider_opening_range_context,
    poi_entry_gate_context,
    side_veto_context,
    strategy_banner_context,
    trap_shift_context,
    vix_risk_context as _vix_risk_context,
)
from aef_terminal.features.provider_session import (
    ProviderSessionInterval,
    ProviderSessionReset,
    provider_session_closing_window,
)
from aef_terminal.features.price_action import (
    displacement_feature,
)
from aef_terminal.indicators.modules.channel_master import (
    channel_master_indicator,
    manual_channel_context,
    signal_candidates_from_channel_master,
)
import aef_terminal.indicators.modules.impulse_fib as impulse_fib_module
from aef_terminal.indicators.modules.w5_structure import w5_structure
from aef_terminal.indicators.modules.wolfe_structure import (
    _wolfe_epa,
    _wolfe_geometry_line,
    wolfe_structure,
)
from aef_terminal.indicators.modules.breakout_accumulation import (
    event_anchor,
)
from aef_terminal.indicators.modules.impulse_fib import (
    ImpulseFibParams,
    impulse_fib as _impulse_fib,
)
from aef_terminal.indicators.modules.impulse_fib.continuation import (
    continuation_geometry,
    continuation_pattern,
)
from aef_terminal.indicators.modules.trade_setup_engine import trade_setup_engine
from aef_terminal.indicators.modules.linda_volume import (
    LindaVolumeParams,
    linda_volume as _linda_volume,
)
from aef_terminal.indicators.modules.market_spotlight import market_spotlight as _market_spotlight
from aef_terminal.indicators.modules.obvious_failure import (
    ObviousFailureParams,
    _spotlight_support,
    obvious_failure,
)
from aef_terminal.runtime.timeframes import expiry_after_bars
from aef_terminal.indicators.contracts import (
    normalize_overlay_item,
    validate_indicator_result_contract,
)
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.ml.candle_specialist import candle_nn_probabilities, candle_specialist
from aef_terminal.runtime import overlays
from aef_terminal.runtime.instruments import PROFILES
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.presentation import (
    build_decision_action_card,
    build_trade_setup_action_card,
)
from aef_terminal.runtime.signal_state import (
    LifecycleState,
    PlanLifecycle,
    SignalState,
    active_lifecycle_from_indicators,
    lifecycle_from_signals,
)
from aef_terminal.signals.indicator_state import signal_candidates_from_indicator_state
from aef_terminal.signals.quality import apply_global_min_rr_gate, apply_global_min_rr_to_decision
from aef_terminal.signals.sfp import detect_sfp
from aef_terminal.signals.trade_plan import execution_plan_rejection


def analyze_bars(bars, *args, instrument, **kwargs):
    kwargs["instrument"] = instrument_with_bar_sessions(instrument, bars)
    return _analyze_bars(bars, *args, **kwargs)


def build_direction_sentiment(*args, **kwargs):
    kwargs.setdefault("instrument_profile", PROFILES["ES"])
    kwargs.setdefault("vsa_context", None)
    return _build_direction_sentiment(*args, **kwargs)


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


def vix_risk_context(bars, **kwargs):
    return _vix_risk_context(bars, profile=_test_profile(bars), **kwargs)


def impulse_fib(bars, params=None, **kwargs):
    return _impulse_fib(bars, profile=_test_profile(bars), params=params, **kwargs)


def linda_volume(bars, params=None, **kwargs):
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(bars))
    return _linda_volume(bars, profile=_test_profile(bars), params=params, **kwargs)


def market_spotlight(bars, params=None, **kwargs):
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(bars))
    return _market_spotlight(bars, profile=_test_profile(bars), params=params, **kwargs)


def vsa_volume(bars, params=None, **kwargs):
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(bars))
    return _vsa_volume(bars, profile=_test_profile(bars), params=params, **kwargs)


def test_mtf_trend_context_ignores_forming_parent_bucket() -> None:
    bars = [
        make_bar(
            index,
            100.0 + index * 0.05,
            100.2 + index * 0.05,
            99.9 + index * 0.05,
            100.1 + index * 0.05,
        )
        for index in range(60)
    ]
    forming_child = make_bar(
        60,
        103.0,
        103.1,
        94.0,
        94.5,
    )

    confirmed_context = mtf_trend_context(bars, vwap_reset=lambda _bar: "session")
    with_forming_parent = mtf_trend_context(
        [*bars, forming_child],
        vwap_reset=lambda _bar: "session",
    )

    assert with_forming_parent == confirmed_context


def test_structure_indicators_do_not_truncate_server_events_before_canvas() -> None:
    assert "events[-16:]" not in inspect.getsource(smc_channels)


@pytest.mark.parametrize(
    "calculate",
    [
        lambda bars, preview_only: trade_setup_engine(
            bars,
            preview_only=preview_only,
        ),
        lambda bars, preview_only: linda_volume(
            bars,
            preview_only=preview_only,
        ),
    ],
    ids=("trade_setup", "linda"),
)
def test_intrabar_preview_materializes_only_current_public_state(
    calculate,
) -> None:
    bars = [
        make_bar(
            index,
            100.0 + index * 0.05,
            100.7 + index * 0.05,
            99.8 + index * 0.05,
            100.4 + index * 0.05,
            1000 + index * 8,
        )
        for index in range(44)
    ]
    bars.append(make_bar(44, 102.0, 106.5, 101.5, 106.0, 6500))

    full = calculate(bars, False)
    preview = calculate(bars, True)
    latest_ts = bars[-1].ts.isoformat()

    assert preview["latest"] == full["latest"]
    assert len(preview["series"]) == 1
    assert preview["overlays"] == []
    assert preview["events"] == [event for event in full["events"] if event.get("ts") == latest_ts]


def test_option_flow_delta_uses_gex_payload_strike_count() -> None:
    previous_strikes = [
        {"strike": 4990 + index * 5, "call_volume": 10, "put_volume": 5} for index in range(6)
    ]
    current_strikes = [
        {
            "strike": row["strike"],
            "call_volume": row["call_volume"] + 1,
            "put_volume": row["put_volume"] + 2,
        }
        for row in previous_strikes
    ]

    result = _gex_activity_delta(
        {
            "spot": 5000,
            "request_meta": {
                "strike_count": 6,
                "requested_strike_limit": 11,
            },
            "raw": {
                "strikes": previous_strikes,
                "contracts": [
                    contract
                    for index, row in enumerate(previous_strikes)
                    for contract in (
                        {
                            "con_id": index * 2 + 1,
                            "expiry": "20260721",
                            "trading_class": "ES",
                            "exchange": "CME",
                            "multiplier": 50.0,
                            "strike": row["strike"],
                            "right": "C",
                            "gamma": None,
                            "open_interest": None,
                            "volume": row["call_volume"],
                            "gex": None,
                            "abs_gex": None,
                        },
                        {
                            "con_id": index * 2 + 2,
                            "expiry": "20260721",
                            "trading_class": "ES",
                            "exchange": "CME",
                            "multiplier": 50.0,
                            "strike": row["strike"],
                            "right": "P",
                            "gamma": None,
                            "open_interest": None,
                            "volume": row["put_volume"],
                            "gex": None,
                            "abs_gex": None,
                        },
                    )
                ],
            },
        },
        {
            "spot": 5000,
            "request_meta": {
                "strike_count": 6,
                "requested_strike_limit": 11,
            },
            "raw": {
                "strikes": current_strikes,
                "contracts": [
                    contract
                    for index, row in enumerate(current_strikes)
                    for contract in (
                        {
                            "con_id": index * 2 + 1,
                            "expiry": "20260721",
                            "trading_class": "ES",
                            "exchange": "CME",
                            "multiplier": 50.0,
                            "strike": row["strike"],
                            "right": "C",
                            "gamma": None,
                            "open_interest": None,
                            "volume": row["call_volume"],
                            "gex": None,
                            "abs_gex": None,
                        },
                        {
                            "con_id": index * 2 + 2,
                            "expiry": "20260721",
                            "trading_class": "ES",
                            "exchange": "CME",
                            "multiplier": 50.0,
                            "strike": row["strike"],
                            "right": "P",
                            "gamma": None,
                            "open_interest": None,
                            "volume": row["put_volume"],
                            "gex": None,
                            "abs_gex": None,
                        },
                    )
                ],
            },
        },
    )

    assert result == {
        "status": "ok",
        "selected_strikes": 6,
        "selected_contracts": 12,
        "matched_contracts": 12,
        "call_volume_delta": 6.0,
        "put_volume_delta": 12.0,
    }


def test_overlay_label_carries_structured_direction() -> None:
    bar = make_bar(0, 10, 11, 9, 10.5)

    long_label = overlays.label(bar=bar, price=10.5, lines=["SIG"], direction=Direction.LONG)
    short_label = overlays.label(bar=bar, price=10.5, lines=["SIG"], direction="short")
    flat_label = overlays.label(bar=bar, price=10.5, lines=["SIG"])

    assert long_label["direction"] == "long"
    assert short_label["direction"] == "short"
    assert flat_label["direction"] == "flat"
    assert long_label["tone"] == "positive"
    assert short_label["tone"] == "negative"
    assert flat_label["tone"] == "warning"
    assert "bg" not in long_label
    assert "text" not in long_label
    assert "tooltip" not in overlays.label(
        bar=bar, price=10.5, lines=["SIG"], direction=Direction.LONG
    )


def test_bind_signal_trade_plan_attaches_structured_plan() -> None:
    bar = make_bar(0, 10, 11, 9, 10.5)
    signal = SignalState(
        source="impulse_fib",
        action=ActionPhase.GO,
        raw_action="GO",
        direction=Direction.LONG,
        score=82.0,
        trigger=10.5,
        stop=9.8,
        target=11.6,
        code="IMPULSE",
        reason="test",
    )
    item = overlays.signal_label(bar=bar, price=10.2, lines=["GO"], signal=signal)
    assert "tooltip" not in item
    assert "bg" not in item
    assert "text" not in item
    assert isinstance(item.get("trade_plan"), dict)
    assert item["signal_overlay"] is True
    assert item["trade_plan"]["entry"] == 10.5
    assert item["trade_plan"]["stop"] == 9.8
    assert item["trade_plan"]["target"] == 11.6
    normalized = normalize_overlay_item(item)
    assert normalized["trade_plan"] == item["trade_plan"]
    assert normalized["signal_overlay"] is True
    assert "has_trade_plan" not in normalized


def test_overlay_line_carries_label_presentation_contract() -> None:
    bar = make_bar(0, 10, 11, 9, 10.5)
    item = overlays.line(
        start=bar,
        end_ts=(bar.ts + timedelta(minutes=20)).isoformat(),
        price=10.25,
        label_text="0.618",
        tone="positive",
        style="dotted",
        label_position="line_right",
        label_side="below",
        label_font_size=6,
        label_gap_px=1,
        direction=Direction.LONG,
    )

    assert item["type"] == "line"
    assert "tooltip" not in overlays.line(
        start=bar,
        end_ts=(bar.ts + timedelta(minutes=20)).isoformat(),
        price=10.25,
        label_text="0.618",
        tone="positive",
    )
    assert item["style"] == "dotted"
    assert item["label_position"] == "line_right"
    assert item["label_side"] == "below"
    assert item["label_font_size"] == 6.0
    assert item["label_gap_px"] == 1.0
    assert item["direction"] == "long"
    assert item["tone"] == "positive"
    assert "color" not in item


def test_overlay_line_projects_on_typed_trading_axis() -> None:
    bar = make_bar(0, 10, 11, 9, 10.5)

    item = overlays.line(
        start=bar,
        bars_forward=7,
        price=10.25,
        label_text="projection",
        tone="positive",
    )

    assert item["end_anchor_ts"] == bar.ts.isoformat()
    assert item["end_bar_offset"] == 7
    assert "end_ts" not in item
    with pytest.raises(ValueError, match="positive bar offset"):
        overlays.projected_end(bar, 0)


def test_overlay_price_zone_and_timestamp_line_use_typed_projected_axis() -> None:
    start_ts = "2026-01-01T15:00:00+00:00"

    zone = overlays.box(
        start_ts=start_ts,
        end_anchor_ts=start_ts,
        end_bar_offset=18,
        top=103.0,
        bottom=102.25,
        label_text="AI target",
        tone="positive",
        direction=Direction.LONG,
    )
    line = overlays.line(
        start_ts=start_ts,
        bars_forward=24,
        price=102.0,
        label_text="AI level",
        tone="info",
    )

    assert zone == {
        "type": "box",
        "start_ts": start_ts,
        "top": 103.0,
        "bottom": 102.25,
        "tone": "positive",
        "opacity": 0.12,
        "interactive": True,
        "end_anchor_ts": start_ts,
        "end_bar_offset": 18,
        "label": "AI target",
        "direction": "long",
    }
    assert line["start_ts"] == start_ts
    assert line["end_anchor_ts"] == start_ts
    assert line["end_bar_offset"] == 24
    assert line["y1"] == line["y2"] == 102.0
    assert "end_ts" not in line


def test_wolfe_projected_geometry_uses_bar_axis_and_preserves_label_side() -> None:
    bars = [make_bar(index, 10 + index, 11 + index, 9 + index, 10.5 + index) for index in range(3)]
    epa = _wolfe_epa(
        {"index": 0, "price": 10.0},
        {"index": 2, "price": 12.0},
        bars,
        extend_bars=7,
    )
    projected = _wolfe_geometry_line(
        {"ts": bars[0].ts.isoformat(), "price": 10.0},
        {**epa, "price": epa["end_price"]},
        geometry_segment="epa",
        role="wolfe_target",
        direction="long",
        line_opacity=0.75,
        control_key="priceTarget",
        fact_fields={},
    )

    assert epa["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert epa["end_bar_offset"] == 7
    assert "end_ts" not in epa
    assert projected is not None
    assert projected["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert projected["end_bar_offset"] == 7
    assert projected["geometry_segment"] == "epa"
    assert projected["label"] == "W EPA"
    assert projected["tone"] == "warning"
    assert projected["style"] == "dotted"
    assert projected["arrow_head"] is True
    assert "color" not in projected
    assert "end_ts" not in projected


def test_breakout_preview_only_preserves_latest_without_historical_payload() -> None:
    bars = [
        make_bar(
            index,
            100 + index * 0.05,
            100.3 + index * 0.05,
            99.8 + index * 0.05,
            100.1 + index * 0.05,
            1200 + index * 10,
        )
        for index in range(80)
    ]

    full = breakout_accumulation(bars)
    preview = breakout_accumulation(bars, preview_only=True)

    assert full["series"] == [full["latest"]]
    assert preview["latest"] == full["latest"]
    assert preview["series"] == [full["latest"]]
    assert preview["events"] == [
        event for event in full["events"] if event["ts"] == bars[-1].ts.isoformat()
    ]
    assert preview["levels"] == []
    assert preview["overlays"] == []


def test_breakout_levels_exclude_plan_stops_and_targets() -> None:
    bars = [
        make_bar(
            i, 100 + i * 0.05, 100.3 + i * 0.05, 99.8 + i * 0.05, 100.1 + i * 0.05, 1200 + i * 10
        )
        for i in range(80)
    ]
    result = breakout_accumulation(bars)
    roles = {str(level.get("role") or "") for level in result.get("levels") or []}
    overlays = result.get("overlays") or []
    overlay_roles = {str(item.get("role") or "") for item in overlays}
    overlays_by_role = {str(item.get("role") or ""): item for item in overlays}
    assert "stop" not in roles
    assert "target" not in roles
    assert "breakout_accumulation_range_box" in overlay_roles
    assert "breakout_accumulation_call_watch" in overlay_roles
    assert "breakout_accumulation_put_watch" in overlay_roles
    assert "breakout_accumulation_decision_table" in overlay_roles
    assert all(
        overlays_by_role[role].get("retention") == "active"
        for role in {
            "breakout_accumulation_range_box",
            "breakout_accumulation_range_high",
            "breakout_accumulation_range_low",
            "breakout_accumulation_range_mid",
            "breakout_accumulation_call_watch",
            "breakout_accumulation_put_watch",
            "breakout_accumulation_decision_table",
        }
    )
    assert overlays_by_role["breakout_accumulation_range_box"]["overlay_group"] == "range_box"
    assert overlays_by_role["breakout_accumulation_range_high"]["overlay_group"] == "range_level"
    assert overlays_by_role["breakout_accumulation_range_high"]["range_label"] is True
    assert overlays_by_role["breakout_accumulation_call_watch"]["overlay_group"] == "watch_level"
    assert "range_label" not in overlays_by_role["breakout_accumulation_call_watch"]
    assert (
        overlays_by_role["breakout_accumulation_range_high"]
        | {
            "label": "High",
            "label_side": "above",
            "style": "dashed",
            "width": 1.0,
        }
        == overlays_by_role["breakout_accumulation_range_high"]
    )
    assert (
        overlays_by_role["breakout_accumulation_range_low"]
        | {
            "label": "Low",
            "label_side": "below",
            "style": "dashed",
            "width": 1.0,
        }
        == overlays_by_role["breakout_accumulation_range_low"]
    )
    assert (
        overlays_by_role["breakout_accumulation_range_mid"]
        | {
            "label": "M",
            "label_side": "above",
            "style": "dotted",
            "width": 1.0,
        }
        == overlays_by_role["breakout_accumulation_range_mid"]
    )
    assert overlays_by_role["breakout_accumulation_call_watch"]["label"].startswith("CALL ")
    assert overlays_by_role["breakout_accumulation_call_watch"]["label_side"] == "above"
    assert overlays_by_role["breakout_accumulation_call_watch"]["style"] == "dashed"
    assert overlays_by_role["breakout_accumulation_call_watch"]["width"] >= 1.0
    assert overlays_by_role["breakout_accumulation_put_watch"]["label"].startswith("PUT ")
    assert overlays_by_role["breakout_accumulation_put_watch"]["label_side"] == "below"
    assert overlays_by_role["breakout_accumulation_put_watch"]["style"] == "dashed"
    assert overlays_by_role["breakout_accumulation_put_watch"]["width"] >= 1.0
    assert (
        overlays_by_role["breakout_accumulation_decision_table"]["table"]["model_ref"]
        == "breakout_accumulation"
    )
    assert "columns" not in overlays_by_role["breakout_accumulation_decision_table"]["table"]
    for role in {
        "breakout_accumulation_range_box",
        "breakout_accumulation_range_high",
        "breakout_accumulation_range_low",
        "breakout_accumulation_range_mid",
        "breakout_accumulation_call_watch",
        "breakout_accumulation_put_watch",
    }:
        assert not (
            {"bg", "border", "color", "lines", "text", "accent"} & overlays_by_role[role].keys()
        )


def test_execution_plan_rejection_blocks_smc_short_through_stop() -> None:
    reason = execution_plan_rejection(
        Direction.SHORT,
        fill_price=746.05,
        stop=745.75,
        target=744.75,
        min_rr=0.75,
    )
    assert reason
    assert reason.code == "stop_through_fill"
    assert reason.attributes == {"stop": 745.75, "fill_price": 746.05}


def test_normalize_trade_plan_anchors_short_stop_above_signal_bar() -> None:
    from aef_terminal.signals.trade_plan import normalize_trade_plan, plan_stop_breached

    bar = make_bar(0, 745.0, 746.05, 744.8, 746.05, symbol="SPY")
    assert plan_stop_breached(Direction.SHORT, 745.75, bar)
    plan = normalize_trade_plan(
        Direction.SHORT,
        745.5,
        745.75,
        744.75,
        bar=bar,
        atr_pad=0.22,
    )
    assert plan["coherent"]
    assert float(plan["stop"]) > float(bar.high)
    assert float(plan["target"]) < float(plan["entry"]) < float(plan["stop"])
    assert not plan_stop_breached(Direction.SHORT, plan["stop"], bar)


def test_breakout_far_stop_put_anchors_to_upper_edge() -> None:
    bar = make_bar(0, 100.0, 112.0, 95.0, 101.0)

    assert event_anchor("far_stop_put", Direction.SHORT, bar, 110.0, 96.0, 103.0) == 112.0
    assert event_anchor("far_stop_call", Direction.LONG, bar, 110.0, 96.0, 103.0) == 95.0


def test_displacement_feature_uses_window_efficiency() -> None:
    bars = [
        make_bar(0, 100, 103, 99, 102),
        make_bar(1, 102, 104, 101, 103),
        make_bar(2, 103, 105, 102, 104),
        make_bar(3, 104, 106, 103, 105),
    ]

    result = displacement_feature(bars)

    assert result["displacement"] == 5 / 7


def test_ibkr_provider_symbol_uses_explicit_future_roots() -> None:
    assert provider_symbol(ibkr_future_payload("GC", exchange="COMEX"), "ibkr") == "GC"
    assert provider_symbol(ibkr_future_payload("CL", exchange="NYMEX"), "ibkr") == "CL"


def test_unknown_symbols_are_not_canonicalized_to_ibkr_roots() -> None:
    with pytest.raises(ValueError, match="INSTRUMENT_IDENTITY_INCOMPLETE"):
        provider_symbol({"key": "UNKNOWN"}, "ibkr")


def test_btc_binds_to_coinbase_product() -> None:
    instrument = bind_provider_instrument("coinbase", "BTC-USD")

    assert instrument is not None
    assert instrument["key"] == "BTC"
    assert instrument["provider"] == "coinbase"
    assert provider_symbol(instrument, "coinbase") == "BTC-USD"


def _iter_signals(payload):
    if isinstance(payload, dict):
        signal = payload.get("signal")
        if isinstance(signal, dict):
            yield signal
        for value in payload.values():
            yield from _iter_signals(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_signals(item)


def _assert_trade_plan_coherent(signal: dict) -> None:
    direction = signal.get("direction")
    entry = signal.get("trigger")
    stop = signal.get("stop")
    target = signal.get("target")
    plan = signal.get("trade_plan")
    assert isinstance(plan, dict), signal
    assert plan.get("direction") == direction
    assert plan.get("entry") == entry
    assert plan.get("trigger") == entry
    assert plan.get("stop") == stop
    assert plan.get("target") == target
    for value in (entry, stop, target):
        if value is not None:
            assert float(value) != 0.0
    if entry is None or stop is None or target is None:
        assert plan.get("complete") is False
        assert plan.get("actionable") is False
        return
    assert plan.get("complete") is True
    if direction == "long":
        assert float(stop) < float(entry) < float(target), signal
        assert plan.get("coherent") is True
    elif direction == "short":
        assert float(target) < float(entry) < float(stop), signal
        assert plan.get("coherent") is True


def test_signal_state_and_lifecycle_publish_structured_trade_plan() -> None:
    signal = SignalState(
        source="unit",
        action=ActionPhase.GO,
        raw_action="GO",
        direction=Direction.LONG,
        score=88.2,
        trigger=100,
        stop=98,
        target=104,
        invalidation=98,
        reason="breakout",
    ).as_dict()

    assert signal["trade_plan"] == {
        "source": "unit",
        "direction": "long",
        "kind": "wait",
        "entry": 100.0,
        "trigger": 100.0,
        "stop": 98.0,
        "target": 104.0,
        "invalidation": 98.0,
        "trail": None,
        "complete": True,
        "coherent": True,
        "actionable": True,
        "risk": 2.0,
        "reward": 4.0,
        "rr": 2.0,
        "action": "GO",
        "raw_action": "GO",
        "score": 88.2,
        "reason": "breakout",
    }
    assert signal["kind"] == "wait"

    lifecycle = PlanLifecycle(
        source="unit",
        state=LifecycleState.FOLLOW,
        direction=Direction.SHORT,
        entry=100,
        stop=103,
        target=94,
        trail=101,
        filled=True,
    ).as_dict()

    assert lifecycle["trade_plan"]["entry"] == 100.0
    assert lifecycle["trade_plan"]["stop"] == 103.0
    assert lifecycle["trade_plan"]["target"] == 94.0
    assert lifecycle["trade_plan"]["trail"] == 101.0
    assert lifecycle["trade_plan"]["direction"] == "short"
    assert lifecycle["trade_plan"]["complete"] is True
    assert lifecycle["trade_plan"]["coherent"] is True


@pytest.mark.parametrize(
    "overrides",
    (
        {"action": "GO"},
        {"direction": "long"},
        {"score": "88.2"},
        {"trigger": "100"},
        {"confirmed": 1},
    ),
)
def test_signal_state_rejects_legacy_coercion_paths(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "source": "unit",
        "action": ActionPhase.GO,
        "direction": Direction.LONG,
        "score": 88.2,
        "trigger": 100.0,
        "stop": 98.0,
        "target": 104.0,
    }
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        SignalState(**values)  # type: ignore[arg-type]


def test_incomplete_signal_plan_is_not_actionable() -> None:
    signal = SignalState(
        source="unit",
        action=ActionPhase.GO,
        raw_action="GO",
        direction=Direction.LONG,
        score=80,
        trigger=100,
        stop=None,
        target=104,
    )
    payload = signal.as_dict()["trade_plan"]

    assert signal.actionable is False
    assert payload["complete"] is False
    assert payload["actionable"] is False


def test_candle_features_smoke() -> None:
    bars = [make_bar(0, 100, 105, 99, 104)]
    features = candle_features(bars)
    assert features["close_pos"] > 0.8
    assert features["body_share"] > 0.5


def test_sfp_detects_high_sweep() -> None:
    bars = [make_bar(i, 100, 101, 99, 100) for i in range(21)]
    bars.append(make_bar(22, 100, 103, 99, 100.5))
    signals = detect_sfp(bars, lookback=20)
    assert signals
    assert signals[0].direction.value == "short"
    assert signals[0].details["trigger"] < signals[0].level


def test_sfp_detects_live_unclosed_sweep_with_execution_buffer() -> None:
    bars = [make_bar(i, 100, 101, 99, 100) for i in range(21)]
    bars.append(make_bar(21, 100, 100.8, 97.6, 99.8, 2600, closed=False))

    signals = detect_sfp(bars, lookback=20)

    assert signals
    assert signals[0].direction.value == "long"
    assert signals[0].details["live_bar"] is True
    assert signals[0].finality is CandidateFinality.PROVISIONAL
    assert signals[0].details["trigger"] > signals[0].level
    assert signals[0].decision_authoritative is False
    decision = choose_decision(signals, bars=bars[:-1], atr_value=1.0)
    assert decision.kind is ScenarioKind.WAIT
    assert decision.direction is Direction.FLAT


def test_analyze_bars_exposes_live_sfp_candidate_without_execution(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.engine.analyze.bars.active_lifecycle_from_indicators",
        lambda *_args, **_kwargs: None,
    )
    base_ts = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0) - timedelta(
        minutes=21 * 5
    )
    bars = [
        make_bar(
            i,
            100,
            101,
            99,
            100,
            1000,
            timeframe="5m",
            symbol="BTC",
            base_ts=base_ts,
            step_minutes=5,
        )
        for i in range(21)
    ]
    bars.append(
        make_bar(
            21,
            100,
            103.5,
            99.6,
            100.4,
            3000,
            timeframe="5m",
            closed=False,
            symbol="BTC",
            base_ts=base_ts,
            step_minutes=5,
        )
    )

    snapshot = analyze_bars(
        bars,
        instrument=coinbase_btc_payload(),
        indicator_params={"trade_setup_engine": {"enabled": True}},
    )

    sfp = next(item for item in snapshot["candidates"] if item["name"] == "sfp")
    assert sfp["details"].get("live_bar") is True
    assert sfp["details"].get("execution_candidate") is False
    assert sfp["status"]["stage"] == "preview"
    assert sfp["status"]["decision_eligible"] is False
    assert sfp["status"]["reason"] == "live_bar"
    assert "sfp" not in " ".join(snapshot["decision"]["reason_codes"]).lower()
    assert snapshot["meta"]["live_bar_closed"] is False
    assert snapshot["meta"]["preview"]["active"] is True
    assert snapshot["meta"]["preview"]["provisional_bar"] is True
    assert snapshot["meta"]["preview"]["execution_uses_confirmed"] is True
    assert snapshot["meta"]["preview"]["candidate_count"] >= 1
    assert snapshot["meta"]["preview"]["policy"] == "preview_only_until_exchange_confirmed"
    assert snapshot["trade_setup"]["side"] == snapshot["decision"]["direction"]


def test_analyze_bars_treats_open_bar_as_provisional_without_source_parsing() -> None:
    base_ts = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    bars = [
        make_bar(
            i,
            100,
            101,
            99,
            100,
            1000,
            timeframe="5m",
            symbol="QQQ",
            base_ts=base_ts,
            step_minutes=5,
        )
        for i in range(21)
    ]
    quote_live = make_bar(
        21,
        100,
        103,
        99.5,
        102.5,
        0,
        timeframe="5m",
        closed=False,
        symbol="QQQ",
        base_ts=base_ts,
        step_minutes=5,
    )
    quote_live = Bar(
        quote_live.symbol,
        quote_live.ts,
        quote_live.open,
        quote_live.high,
        quote_live.low,
        quote_live.close,
        quote_live.volume,
        quote_live.timeframe,
        "ibkr:db-cache+quote-live",
        closed=False,
    )

    snapshot = analyze_bars(
        [*bars, quote_live],
        instrument=ibkr_stock_payload("QQQ"),
        analysis_as_of_utc=quote_live.ts + timedelta(minutes=4),
    )

    assert snapshot["meta"]["live_bar_closed"] is False
    assert snapshot["meta"]["analysis_ts"] == bars[-1].ts.isoformat()
    assert snapshot["meta"]["preview"]["active"] is True
    assert snapshot["meta"]["preview"]["provisional_bar"] is True
    assert snapshot["meta"]["preview"]["confirmed_latest_ts"] == bars[-1].ts.isoformat()
    assert snapshot["meta"]["preview"]["latest_ts"] == quote_live.ts.isoformat()
    assert snapshot["meta"]["confirmed_bar_count"] == len(bars)


def test_confirmed_slots_preserve_rows_around_a_middle_provisional_placeholder() -> None:
    base_ts = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.5, base_ts=base_ts, step_minutes=5),
        make_bar(
            1,
            100.5,
            100.5,
            100.5,
            100.5,
            0,
            closed=False,
            base_ts=base_ts,
            step_minutes=5,
        ),
        make_bar(2, 100.5, 102.0, 100.0, 101.5, base_ts=base_ts, step_minutes=5),
    ]
    instrument = instrument_with_bar_sessions(ibkr_future_payload("ES"), bars)

    inputs = build_analyze_inputs(
        bars,
        instrument=instrument,
        bar_slots=ProviderBarSlotSequence([10, 20, 30], schedule_state="verified"),
    )

    assert inputs.confirmed_bars == [bars[0], bars[2]]
    assert inputs.confirmed_slots == [10, 30]
    with pytest.raises(ValueError, match="align exactly"):
        build_analyze_inputs(
            bars,
            instrument=instrument,
            bar_slots=ProviderBarSlotSequence([10, 20], schedule_state="verified"),
        )


def test_analyze_bars_blocks_execution_when_only_provisional_bars_exist() -> None:
    base_ts = datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ",
            base_ts + timedelta(minutes=i * 5),
            100,
            101,
            99,
            100.5,
            0,
            "5m",
            "ibkr:db-cache+quote-live",
            closed=False,
        )
        for i in range(3)
    ]

    snapshot = analyze_bars(
        bars,
        instrument=ibkr_stock_payload("QQQ"),
        analysis_as_of_utc=base_ts + timedelta(minutes=12),
    )

    assert snapshot["meta"]["confirmed_bar_count"] == 0
    assert snapshot["meta"]["analysis_ts"] is None
    assert snapshot["meta"]["preview"]["active"] is True
    assert snapshot["meta"]["preview"]["blocked_reason"] == "no_confirmed_bar"
    assert snapshot["meta"]["preview"]["execution_candidate_count"] == 0
    assert snapshot["meta"]["data_quality"]["signals_ok"] is False
    assert snapshot["meta"]["data_quality"]["status"] == "no_confirmed_bar"
    assert snapshot["decision"]["action"] == "BLOCK"
    assert "data_quality_block" in snapshot["decision"]["reason_codes"]
    assert snapshot["decision"]["direction"] == "flat"
    assert snapshot["candidates"] == []
    assert snapshot["trade_setup"]["ok"] is False


def test_analysis_exposes_trade_setup_card() -> None:
    bars = [make_bar(i, 100, 101, 99, 100, 1000) for i in range(24)]
    bars.append(make_bar(24, 100, 103.2, 99.6, 100.2, 3200))

    snapshot = analyze_bars(
        bars,
        instrument=ibkr_future_payload("ES"),
        indicator_params={"trade_setup_engine": {"enabled": True}},
    )
    setup = snapshot["trade_setup"]

    assert setup["action_card"]["source"] == "trade_setup"
    assert setup["action_card"]["tier"] == "command"
    assert setup["side"] in {"long", "short", "flat"}
    assert "plan" in setup
    assert {"entry", "stop", "target", "rr"} <= set(setup["plan"])
    assert isinstance(setup["confluence"], list)
    assert isinstance(setup["watch"], list)
    assert "level" in setup
    assert snapshot["direction_sentiment"]["direction"] in {"long", "short", "flat"}
    assert 0 <= snapshot["direction_sentiment"]["score"] <= 100
    assert isinstance(snapshot["direction_sentiment"]["contributors"], list)


def test_trade_setup_option_flow_confluence_uses_time_normalized_rates() -> None:
    setup = build_trade_setup_card(
        latest=make_bar(0, 100.0, 102.0, 99.5, 101.0, 1000),
        decision=ScenarioDecision(
            kind=ScenarioKind.TRANSIT,
            direction=Direction.LONG,
            confidence=84,
            action=ActionPhase.GO,
            trigger=101.0,
            stop=99.0,
            target=105.0,
            invalidation=99.0,
            trigger_event=DomainFact("breakout"),
            reasons=["breakout"],
        ),
        candidates=[],
        indicators={},
        levels=[],
        option_flow={
            "call_volume_delta": 1.0,
            "put_volume_delta": 999.0,
            "call_rate_per_minute": 3.25,
            "put_rate_per_minute": 0.75,
        },
        data_quality={"signals_ok": True},
        atr_value=1.0,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    option_evidence = next(item for item in setup["confluence"] if item["source"] == "option_flow")
    assert option_evidence == {
        "source": "option_flow",
        "call_rate_per_minute": 3.25,
        "put_rate_per_minute": 0.75,
        "score": 0.0,
    }


def test_trade_setup_option_flow_confluence_rejects_partial_or_string_rates() -> None:
    setup = build_trade_setup_card(
        latest=make_bar(0, 100.0, 102.0, 99.5, 101.0, 1000),
        decision=ScenarioDecision(
            kind=ScenarioKind.TRANSIT,
            direction=Direction.LONG,
            confidence=84,
            action=ActionPhase.GO,
            trigger=101.0,
            stop=99.0,
            target=105.0,
            invalidation=99.0,
            trigger_event=DomainFact("breakout"),
            reasons=["breakout"],
        ),
        candidates=[],
        indicators={},
        levels=[],
        option_flow={"call_rate_per_minute": "3.25", "put_rate_per_minute": 0.75},
        data_quality={"signals_ok": True},
        atr_value=1.0,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert all(item.get("source") != "option_flow" for item in setup["confluence"])


def test_tick_flow_bias_contributes_to_direction_sentiment() -> None:
    bars = [
        make_bar(
            index, 100 + index * 0.1, 101 + index * 0.1, 99 + index * 0.1, 100.5 + index * 0.1, 1000
        )
        for index in range(12)
    ]
    rows = []
    for index, bar in enumerate(bars):
        rows.append(
            {
                "ts": bar.ts.isoformat(),
                "net_delta": 40 if index < 7 else 220,
                "total_volume": 500,
                "trade_count": 20,
            }
        )
    rows.append(
        {
            "ts": bars[-1].ts.isoformat(),
            "net_delta": -100_000,
            "total_volume": 100_000,
        }
    )

    bias = tick_flow_bias_from_context(
        {
            "source": "ibkr_tick",
            "symbol": "ES",
            "delta": rows,
            "decision_eligible": True,
        },
        bars,
        window_bars=5,
    )
    sentiment = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("wait"),
            [],
        ),
        indicators={},
        candidates=[],
        tick_bias=bias,
    )

    assert bias is not None
    assert bias["direction"] == "long"
    assert any(item["source"] == "tick_flow" for item in sentiment["contributors"])
    assert sentiment["direction"] == "long"


def test_institutional_edge_contributes_to_direction_sentiment() -> None:
    sentiment = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("wait"),
            [],
        ),
        indicators={
            "absorption_trap": {
                "latest": {"der_status": "LONG_LIQUIDATION_RISK"},
                "params": {"synergy_matrix": True},
            },
        },
        vsa_context={"latest": {"code": "UPTHRUST"}},
        candidates=[],
    )

    edge = next(
        item for item in sentiment["contributors"] if item["source"] == "institutional_edge"
    )
    assert edge["direction"] == "short"
    assert edge["action"] == "GO"
    assert edge["score"] == 95.0
    assert edge["weight"] == 2.5
    assert sentiment["direction"] == "short"
    assert sentiment["institutional_edge"] == {
        "action": "GO",
        "phase": "go",
        "direction": "short",
        "reason_code": "vsa_der_absorption",
        "signal_code": "UPTHRUST",
        "der_status": "LONG_LIQUIDATION_RISK",
    }


def test_institutional_edge_uses_linda_signal_code_and_can_be_disabled() -> None:
    enabled = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("wait"),
            [],
        ),
        indicators={
            "absorption_trap": {
                "latest": {"der_status": "SHORT_SQUEEZE_RISK"},
                "params": {"synergy_matrix": True},
            },
        },
        vsa_context={"latest": {"code": "SPRING"}},
        candidates=[],
    )
    disabled = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("wait"),
            [],
        ),
        indicators={
            "absorption_trap": {
                "latest": {"der_status": "SHORT_SQUEEZE_RISK"},
                "params": {"synergy_matrix": False},
            },
        },
        vsa_context={"latest": {"code": "SPRING"}},
        candidates=[],
    )

    edge = next(item for item in enabled["contributors"] if item["source"] == "institutional_edge")
    assert edge["direction"] == "long"
    assert edge["action"] == "GO"
    assert enabled["institutional_edge"] == {
        "action": "GO",
        "phase": "go",
        "direction": "long",
        "reason_code": "vsa_der_absorption",
        "signal_code": "SPRING",
        "der_status": "SHORT_SQUEEZE_RISK",
    }
    assert not any(item["source"] == "institutional_edge" for item in disabled["contributors"])
    assert "institutional_edge" not in disabled


def test_institutional_edge_macro_execute_veto_softens_counter_trend_edge() -> None:
    short_sentiment = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("wait"),
            [],
        ),
        indicators={
            "market_spotlight": {
                "sentiment_role": "market_context",
                "market_context": {
                    "direction": "long",
                    "strategy": {"active": True, "phase": "execute", "direction": "long"},
                },
            },
            "absorption_trap": {
                "latest": {"der_status": "LONG_LIQUIDATION_RISK"},
                "params": {"synergy_matrix": True},
            },
        },
        vsa_context={"latest": {"code": "UPTHRUST"}},
        candidates=[],
    )
    long_sentiment = build_direction_sentiment(
        decision=ScenarioDecision(
            ScenarioKind.WAIT,
            Direction.FLAT,
            0,
            ActionPhase.WAIT,
            None,
            None,
            None,
            None,
            DomainFact("wait"),
            [],
        ),
        indicators={
            "market_spotlight": {
                "sentiment_role": "market_context",
                "market_context": {
                    "direction": "short",
                    "strategy": {"active": True, "phase": "execute", "direction": "short"},
                },
            },
            "absorption_trap": {
                "latest": {"der_status": "SHORT_SQUEEZE_RISK"},
                "params": {"synergy_matrix": True},
            },
        },
        vsa_context={"latest": {"code": "SPRING"}},
        candidates=[],
    )

    short_edge = next(
        item for item in short_sentiment["contributors"] if item["source"] == "institutional_edge"
    )
    long_edge = next(
        item for item in long_sentiment["contributors"] if item["source"] == "institutional_edge"
    )
    assert short_edge["direction"] == "short"
    assert short_edge["action"] == "WATCH"
    assert short_edge["score"] == 60.0
    assert short_sentiment["institutional_edge"]["action"] == "WATCH"
    assert short_sentiment["institutional_edge"]["phase"] == "watch"
    assert short_sentiment["institutional_edge"]["direction"] == "short"
    assert long_edge["direction"] == "long"
    assert long_edge["action"] == "WATCH"
    assert long_edge["score"] == 60.0
    assert long_sentiment["institutional_edge"]["action"] == "WATCH"
    assert long_sentiment["institutional_edge"]["phase"] == "watch"
    assert long_sentiment["institutional_edge"]["direction"] == "long"


def test_institutional_edge_overlay_is_attached_to_absorption_trap() -> None:
    source = inspect.getsource(_analyze_bars)

    assert 'for_indicator("absorption_trap")' not in source
    assert 'indicator_bundle["absorption_trap"]' not in source
    assert "indicator_bundle.get(institutional_edge_source)" in source
    edge = sentiment_module._evaluate_institutional_edge(
        {
            "absorption_trap": {
                "latest": {"der_status": "LONG_LIQUIDATION_RISK"},
                "params": {"synergy_matrix": True},
            },
        },
        vsa_context={"latest": {"code": "UPTHRUST"}},
    )
    assert edge is not None
    assert edge["source"] == "absorption_trap"


def test_institutional_edge_overlay_contract() -> None:
    bar = make_bar(0, 100, 103, 99, 101)

    short_overlay = sentiment_module._institutional_edge_overlay(
        {
            "edge": "SHORT",
            "phase": "go",
            "action": "GO",
            "signal_code": "UPTHRUST",
            "der_status": "LONG_LIQUIDATION_RISK",
        },
        bar,
    )
    long_overlay = sentiment_module._institutional_edge_overlay(
        {
            "edge": "LONG",
            "phase": "go",
            "action": "GO",
            "signal_code": "SPRING",
            "der_status": "SHORT_SQUEEZE_RISK",
        },
        bar,
    )

    assert short_overlay is not None
    assert "lines" not in short_overlay
    assert short_overlay["direction"] == "short"
    assert {"bg", "text"}.isdisjoint(short_overlay)
    assert "tooltip" not in short_overlay
    assert short_overlay["scenario"] == "institutional_edge"
    assert short_overlay["trigger_event"] == {
        "code": "edge_absorption",
        "direction": "short",
        "phase": "go",
    }
    assert short_overlay["evidence"]["supporting"] == [
        {"code": "signal_context", "signal_code": "UPTHRUST"},
        {"code": "der_state", "status": "LONG_LIQUIDATION_RISK"},
    ]
    assert short_overlay["evidence"]["opposing"] == [
        {"code": "aggressive_counterflow_trapped", "direction": "long"}
    ]
    assert short_overlay["evidence"]["context"] == [{"code": "micro_stop", "relation": "above"}]
    assert long_overlay is not None
    assert "lines" not in long_overlay
    assert long_overlay["direction"] == "long"
    assert {"bg", "text"}.isdisjoint(long_overlay)
    assert "tooltip" not in long_overlay
    assert long_overlay["scenario"] == "institutional_edge"
    assert long_overlay["trigger_event"] == {
        "code": "edge_absorption",
        "direction": "long",
        "phase": "go",
    }
    assert long_overlay["evidence"]["context"] == [{"code": "micro_stop", "relation": "below"}]


def test_trade_setup_profit_protection_turns_counter_pressure_into_trail_advice() -> None:
    latest = make_bar(2, 7556.0, 7567.75, 7554.0, 7556.0, 1800)
    decision = ScenarioDecision(
        kind=ScenarioKind.FADE,
        direction=Direction.LONG,
        confidence=86,
        action=ActionPhase.GO,
        trigger=7558.0,
        stop=7548.0,
        target=7593.75,
        invalidation=7548.0,
        trigger_event=DomainFact("long_reclaim_after_pullback"),
        reasons=["long reclaim after pullback"],
    )

    setup = build_trade_setup_card(
        latest=latest,
        decision=decision,
        candidates=[
            SignalCandidate(
                "smc_structure",
                Direction.SHORT,
                88,
                7567.75,
                "upper channel reject",
                DomainFact("upper_channel_reject"),
                kind=ScenarioKind.FADE,
                finality=CandidateFinality.CONFIRMED,
            ),
            SignalCandidate(
                "wolfe_structure",
                Direction.SHORT,
                71,
                7578.98,
                "fresh Wolfe pressure",
                DomainFact("fresh_wolfe_pressure"),
                kind=ScenarioKind.FADE,
                finality=CandidateFinality.CONFIRMED,
            ),
        ],
        indicators={
            "market_spotlight": {
                "market_context": {
                    "direction": "long",
                    "side_lock": "LONG",
                    "side_gate": {"veto_short": True},
                    "flow_state": {
                        "dominant_direction": "long",
                        "countertrend_direction": "short",
                        "chase_risk": True,
                        "slowdown": True,
                        "directive": "avoid_short",
                    },
                }
            }
        },
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=3.77,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert setup["action"] == "TRAIL"
    assert setup["management"]["active"] is True
    assert setup["management"]["directive"] == "trail_profit_do_not_add"
    assert setup["action_card"]["phase"] == "TRAIL"
    assert setup["action_card"]["phase"] == setup["action"]
    assert setup["watch"][0]["event"] == "profit_protection"


def test_trade_setup_market_spotlight_same_side_veto_blocks_entry_card() -> None:
    latest = make_bar(2, 7527.0, 7531.0, 7524.0, 7527.25, 1800)
    decision = ScenarioDecision(
        kind=ScenarioKind.FADE,
        direction=Direction.LONG,
        confidence=86,
        action=ActionPhase.GO,
        trigger=7528.5,
        stop=7525.0,
        target=7578.25,
        invalidation=7525.0,
        trigger_event=DomainFact("long_fade_attempt"),
        reasons=["long fade attempt"],
    )

    setup = build_trade_setup_card(
        latest=latest,
        decision=decision,
        candidates=[],
        indicators={
            "market_spotlight": {
                "market_context": {
                    "direction": "flat",
                    "flow_conflict": True,
                    "side_lock": "CHECK",
                    "side_gate": {"veto_long": True},
                    "entry": {"phase": "blocked"},
                }
            }
        },
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=3.77,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert setup["ok"] is False
    assert setup["action"] == "BLOCK"
    assert setup["action_card"]["phase"] == "BLOCK"
    assert "market_spotlight_veto_long" in setup["blocked"]


def test_trade_setup_correction_watch_from_channel_promotes_reduce_runner_advice() -> None:
    latest = make_bar(2, 7558.0, 7568.25, 7555.5, 7557.0, 2200)
    decision = ScenarioDecision(
        kind=ScenarioKind.TRANSIT,
        direction=Direction.LONG,
        confidence=89,
        action=ActionPhase.GO,
        trigger=7558.0,
        stop=7548.0,
        target=7593.0,
        invalidation=7548.0,
        trigger_event=DomainFact("long_trend_aligned"),
        reasons=["long trend still aligned"],
    )

    setup = build_trade_setup_card(
        latest=latest,
        decision=decision,
        candidates=[
            SignalCandidate(
                "manual_channel_reaction",
                Direction.SHORT,
                74,
                7568.0,
                "manual channel HIGH TOUCH",
                DomainFact("manual_channel_high_touch"),
                details={"level_label": "HIGH", "level_role": "upper_edge", "is_reclaim": False},
                kind=ScenarioKind.FADE,
                source="channel_master",
                role="channel_reaction",
                finality=CandidateFinality.CONFIRMED,
            ),
            SignalCandidate(
                "smc_structure",
                Direction.SHORT,
                78,
                7567.75,
                "upper channel reject",
                DomainFact("upper_channel_reject"),
                kind=ScenarioKind.FADE,
                finality=CandidateFinality.CONFIRMED,
            ),
        ],
        indicators={
            "market_spotlight": {
                "market_context": {
                    "direction": "long",
                    "side_lock": "LONG",
                    "side_gate": {"veto_short": True},
                    "flow_state": {
                        "slowdown": True,
                        "dominant_direction": "long",
                        "countertrend_direction": "short",
                    },
                }
            }
        },
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=3.77,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert setup["action"] == "TRAIL"
    assert setup["correction_watch"]["active"] is True
    assert setup["correction_watch"]["severity"] == "confirmed"
    assert setup["position_coach"]["mode"] == "reduce"
    assert setup["position_coach"]["directive"] == "protect_profit_reduce_runner"
    assert setup["action_card"]["phase"] == setup["action"]
    assert any(item.get("event") == "correction_watch" for item in setup["watch"])


def test_trade_setup_position_coach_holds_aligned_position_without_correction() -> None:
    latest = make_bar(2, 7558.0, 7568.25, 7555.5, 7565.0, 2200)
    decision = ScenarioDecision(
        kind=ScenarioKind.TRANSIT,
        direction=Direction.LONG,
        confidence=84,
        action=ActionPhase.ARM,
        trigger=7566.0,
        stop=7556.0,
        target=7590.0,
        invalidation=7556.0,
        trigger_event=DomainFact("long_acceptance_continuation"),
        reasons=["long acceptance continuation"],
    )

    setup = build_trade_setup_card(
        latest=latest,
        decision=decision,
        candidates=[],
        indicators={
            "market_spotlight": {
                "market_context": {
                    "direction": "long",
                    "side_lock": "LONG",
                    "side_gate": {"veto_short": True},
                    "flow_state": {
                        "slowdown": False,
                        "chase_risk": False,
                        "dominant_direction": "long",
                    },
                }
            }
        },
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=3.77,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert setup["action"] == "ARM"
    assert setup["correction_watch"]["active"] is False
    assert setup["position_coach"]["mode"] == "hold"
    assert setup["position_coach"]["directive"] == "hold_core"
    assert setup["action_card"]["phase"] == setup["action"]


def test_trade_setup_surfaces_flag_and_pullback_reentry_watch_lines() -> None:
    latest = make_bar(2, 101.8, 102.5, 101.0, 101.4, 1250)
    decision = ScenarioDecision(
        kind=ScenarioKind.FADE,
        direction=Direction.SHORT,
        confidence=88,
        action=ActionPhase.ARM,
        trigger=101.0,
        stop=102.7,
        target=98.2,
        invalidation=102.7,
        trigger_event=DomainFact("bear_flag_continuation"),
        reasons=["bear flag continuation"],
    )
    pattern = {
        "active": True,
        "type": "bear_rising_wedge",
        "structure_kind": "wedge",
        "label": "BEAR FLAG",
        "direction": "short",
        "phase": "arm",
        "quality": 82.0,
        "break_level": 101.0,
        "invalidation": 102.7,
        "target_1": 99.2,
        "target_2": 98.2,
    }
    reentry = {
        "active": True,
        "mode": "pullback_reentry",
        "direction": "long",
        "phase": "watch",
        "trigger": 103.1,
        "stop_reference": 101.0,
        "text": "LONG RECLAIM WATCH",
    }

    setup = build_trade_setup_card(
        latest=latest,
        decision=decision,
        candidates=[],
        indicators={
            "impulse_fib": {"continuation_pattern": pattern},
            "market_spotlight": {
                "market_context": {
                    "direction": "short",
                    "side_lock": "SHORT",
                    "side_gate": {"veto_long": True},
                    "pullback_reentry": reentry,
                    "flow_state": {"dominant_direction": "short", "countertrend_direction": "long"},
                }
            },
        },
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=2.0,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert setup["continuation_pattern"]["type"] == "bear_rising_wedge"
    assert setup["pullback_reentry"]["direction"] == "long"
    continuation_watch = next(
        item for item in setup["watch"] if item.get("event") == "continuation_pattern_watch"
    )
    assert continuation_watch["pattern_type"] == "bear_rising_wedge"
    assert continuation_watch["structure_kind"] == "wedge"
    assert continuation_watch["quality"] == 82.0
    assert any(item.get("event") == "pullback_reentry_opposite" for item in setup["watch"])


def test_trade_setup_blocks_and_hides_incoherent_plan_geometry() -> None:
    latest = make_bar(2, 4542.0, 4550.0, 4538.0, 4542.1, 1200)
    decision = ScenarioDecision(
        kind=ScenarioKind.FADE,
        direction=Direction.LONG,
        confidence=86,
        action=ActionPhase.GO,
        trigger=4548.0,
        stop=4561.0,
        target=4508.0,
        invalidation=4561.0,
        trigger_event=DomainFact("absorption_trap_follow"),
        reasons=["AbsTrap FOLLOW"],
    )

    setup = build_trade_setup_card(
        latest=latest,
        decision=decision,
        candidates=[],
        indicators={},
        levels=[],
        option_flow=None,
        data_quality={"signals_ok": True},
        atr_value=3.0,
        price_increment=0.25,
        execution_authority=ready_trade_setup_execution_authority(),
    )

    assert setup["ok"] is False
    assert "incoherent_trade_plan_geometry" in setup["blocked"]
    assert setup["plan"]["entry"] is None
    assert setup["plan"]["stop"] is None
    assert setup["plan"]["target"] is None
    assert setup["plan"]["coherent"] is False
    assert setup["plan"]["raw"]["entry"] == 4548.0
    assert setup["action_card"]["entry"] is None
    assert setup["action_card"]["stop"] is None
    assert setup["action_card"]["target"] is None


def test_action_card_normalizes_decision_and_trade_setup_language() -> None:
    decision = ScenarioDecision(
        kind=ScenarioKind.FADE,
        direction=Direction.SHORT,
        confidence=81,
        action=ActionPhase.GO,
        trigger=101.0,
        stop=103.0,
        target=97.0,
        invalidation=103.0,
        trigger_event=DomainFact("upper_rejection"),
        reasons=["upper rejection"],
    )
    command = build_decision_action_card(decision)

    assert command.phase == "GO"
    assert command.direction == "short"
    assert command.setup == "fade"
    assert command.tier == "command"
    assert command.entry == 101.0

    setup_card = build_trade_setup_action_card(
        {
            "ok": True,
            "action": "GO",
            "side": "short",
            "kind": "fade",
            "trigger_event": {"code": "upper_rejection"},
            "quality": 88,
            "plan": {"entry": 101.0, "stop": 103.0, "target": 97.0},
            "confluence": [{"label": "Wolfe sell"}],
            "watch": ["Reject the level"],
            "blocked": [],
        }
    )

    assert setup_card.phase == "GO"
    assert setup_card.direction == "short"
    assert setup_card.setup == "fade"
    assert setup_card.tier == "command"


def test_candle_specialist_restores_nn_and_dna_projection() -> None:
    bars = [
        Bar(
            "ES",
            datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * index),
            100 + index * 0.08,
            100.6 + index * 0.08 + (0.25 if index % 9 == 0 else 0.0),
            99.7 + index * 0.08,
            100.3 + index * 0.08 + (0.18 if index % 5 == 0 else -0.04),
            1200 + index * 8,
            "5m",
            "test",
            True,
        )
        for index in range(80)
    ]

    probs = candle_nn_probabilities(bars)
    result = candle_specialist(bars, min_prob=0.0, min_edge=-100.0, dna_lookback=60)
    projection_only = candle_specialist(bars, min_prob=0.20, min_edge=100.0, dna_lookback=60)
    projection_high_threshold = candle_specialist(
        bars, min_prob=0.75, min_edge=100.0, dna_lookback=60
    )

    assert probs is not None
    assert set(result["latest"]["probabilities"]) == {"bear", "neutral", "bull"}
    assert result["latest"]["score"] == round(min(50.0 + abs(float(probs["edge"])) * 0.5, 99.0), 1)
    assert result["latest"]["confidence_prob"] == round(
        max(float(probs["prob_bull"]), float(probs["prob_bear"])) * 100.0, 1
    )
    assert result["latest"]["confidence_edge"] == round(abs(float(probs["edge"])), 2)
    assert "tooltip" not in result["latest"]
    assert "read" not in result["latest"]
    assert result["latest"]["setup_summary"].startswith("Candle NN bull/bear ")
    assert result["latest"]["candidate_reason"] == result["latest"]["setup_summary"]
    assert result["latest"]["scenario"] == "Candle Specialist NN"
    assert result["latest"]["quality_text"].startswith("confidence ")
    assert result["latest"]["context_lines"]
    assert result["latest"]["dna"]["state"] in {"DNA", "NO MATCH", "DNA IDLE"}
    assert any(item["type"] == "label" for item in result["overlays"])
    cnn_label = next(
        item for item in result["overlays"] if item["type"] == "label" and item["lines"][0] == "CNN"
    )
    assert cnn_label["source"] == "candle_specialist"
    assert cnn_label["direction"] in {"long", "short"}
    assert cnn_label["signal"]["direction"] == cnn_label["direction"]
    assert cnn_label["signal"]["reason"] == result["latest"]["candidate_reason"]
    assert {"entry", "stop", "target", "trigger", "action"} <= set(cnn_label)
    assert "tooltip" not in cnn_label
    assert cnn_label["scenario"] == "Candle Specialist NN"
    assert cnn_label["context_lines"] == result["latest"]["context_lines"]
    assert all("tooltip" not in item for item in result["overlays"])
    if result["latest"]["dna"]["state"] == "DNA":
        assert any(item["type"] in {"box", "line"} for item in result["overlays"])
        dna_overlay = next(
            item for item in result["overlays"] if item.get("scenario") == "Setup DNA ensemble"
        )
        assert dna_overlay["context_lines"]
    assert projection_only["latest"]["action"] == "WAIT"
    assert projection_only["latest"]["dna"]["state"] in {"DNA", "NO MATCH"}
    if projection_only["latest"]["dna"]["state"] == "DNA":
        assert any(item["type"] in {"box", "line"} for item in projection_only["overlays"])
    else:
        assert projection_only["overlays"] == []
    assert projection_high_threshold["latest"]["projection_prob_gate"] == 0.20
    assert projection_high_threshold["latest"]["dna"]["state"] in {"DNA", "NO MATCH"}
    if projection_high_threshold["latest"]["dna"]["state"] == "DNA":
        assert any(
            item["type"] in {"box", "line"} for item in projection_high_threshold["overlays"]
        )
    else:
        assert projection_high_threshold["overlays"] == []

    no_match = candle_specialist(
        bars, min_prob=0.20, min_edge=100.0, dna_lookback=60, dna_max_distance=101.0
    )
    assert no_match["latest"]["dna"]["state"] == "NO MATCH"
    assert no_match["latest"]["dna"]["preview"] is True
    assert no_match["overlays"] == []


def test_liquidity_void_context_detects_fresh_bull_fvg_void() -> None:
    bars = [
        make_bar(0, 99.6, 100.0, 99.0, 99.8, 1000),
        make_bar(1, 99.8, 101.2, 99.7, 100.8, 1400),
        make_bar(2, 100.9, 102.0, 100.55, 101.7, 2600),
    ]

    context = liquidity_void_context(bars, [1.0, 1.0, 1.0], [1.0, 1.1, 1.8])

    assert context["long_context"]
    assert not context["short_context"]
    assert context["bull_fvg_age"] == 0
    assert context["bull_void_age"] == 0
    assert context["void_direction"] == "long"
    assert {"bull_fvg", "bull_void"} <= set(context["reason_codes"])


def test_liquidity_void_context_detects_buy_side_sweep_reclaim_short() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.8, 100.2, 1000) for i in range(8)]
    bars.append(make_bar(8, 100.2, 101.1, 99.9, 100.4, 1100))
    bars.append(make_bar(9, 100.6, 102.4, 99.6, 100.0, 2500))

    context = liquidity_void_context(bars, [1.0] * len(bars), [1.0] * (len(bars) - 1) + [1.7])

    assert context["short_context"]
    assert context["sweep_high"]
    assert context["reclaim_short"]


def test_vix_risk_context_uses_explicit_risk_off_state() -> None:
    bars = [make_bar(i, 100, 101, 99, 100.2, 1000, symbol="ES") for i in range(12)]

    context = vix_risk_context(bars, features={"vix_state": "risk-off shock"})

    assert context["active"]
    assert context["valid"]
    assert context["state"] == "risk-off"
    assert context["risk_off"]
    assert context["shock"]


def test_provider_opening_range_uses_confirmed_session_bars() -> None:
    opening_start = datetime(2026, 7, 25, 13, 30, tzinfo=timezone.utc)
    bars = [
        make_bar(0, 100, 101, 99, 100, base_ts=opening_start),
        make_bar(15, 100, 102, 98, 101, base_ts=opening_start),
        make_bar(31, 101, 103, 100, 102.5, base_ts=opening_start),
    ]

    session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="",
        calendar="provider_schedule",
        intervals=(
            ProviderSessionInterval(
                opens_at=opening_start,
                closes_at=opening_start + timedelta(hours=6, minutes=30),
                session_date=opening_start.date(),
            ),
        ),
    )
    context = provider_opening_range_context(
        bars,
        provider_session=session,
    )

    assert context["active"] is True
    assert context["ready"] is True
    assert context["high"] == 102
    assert context["low"] == 98


def test_provider_new_york_range_uses_liquid_schedule_and_caps_session_close() -> None:
    opening_start = datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc)
    bars = [
        make_bar(0, 100, 101, 99, 100, base_ts=opening_start),
        make_bar(15, 100, 102, 98, 101, base_ts=opening_start),
        make_bar(31, 101, 103, 100, 102.5, base_ts=opening_start),
    ]
    instrument = {
        "session": {
            "liquid_intervals": [
                {
                    "status": "open",
                    "session_date": "2026-07-27",
                    "opens_at": opening_start.isoformat(),
                    "closes_at": (opening_start + timedelta(hours=7, minutes=30)).isoformat(),
                }
            ]
        }
    }

    context = provider_new_york_range_context(bars, instrument=instrument)

    assert context["active"] is True
    assert context["ready"] is True
    assert context["high"] == 102
    assert context["low"] == 98
    assert context["start_ts"] == "2026-07-27T13:30:00+00:00"
    assert context["end_ts"] == "2026-07-27T14:00:00+00:00"
    assert context["session_end_ts"] == "2026-07-27T20:00:00+00:00"
    assert context["source"] == "provider_liquid_intervals"


def test_vix_quote_snapshot_features_use_neutral_consumer_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_stock_payload("VIX", con_id=99001)
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    route = route_instrument(instrument)

    def quote_row(observed_at: datetime, price: float) -> dict:
        payload = {
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "provider_symbol": route.provider_symbol,
            "price": price,
            "last": price,
            "price_source": "last",
            "provider_ts": observed_at.isoformat(),
            "received_at": now.isoformat(),
            "time_basis": "provider_event",
            "market_data_entitlement": "live",
            "is_delayed": False,
        }
        return {"ts": observed_at.isoformat(), "price": price, "payload": payload}

    class QuoteStore:
        def lookup_instrument(
            self,
            instrument_id: str,
            *,
            watchlist_only: bool,
        ) -> dict:
            assert instrument_id == instrument["instrument_id"]
            assert watchlist_only is False
            return instrument

        def read_quote_snapshots(self, *_args, **_kwargs) -> list[dict]:
            return [
                quote_row(now - timedelta(minutes=5), 19.0),
                quote_row(now, 20.0),
            ]

    monkeypatch.setenv("AEF_VIX_INSTRUMENT_ID", instrument["instrument_id"])
    context_input = load_vix_context_input(
        QuoteStore(),
        analysis_as_of_utc=now,
    )
    features = analyze_features.vix_features_from_context(context_input)

    assert set(features) == {
        "vix_price",
        "vix_previous",
        "vix_ema",
        "vix_sma",
        "vix_stdev",
        "vix_source",
    }
    assert features["vix_source"] == "qualified_quote_snapshots"
    assert not any(key.startswith("linda_") for key in features)
    bars = [make_bar(i, 100, 101, 99, 100.2, 1000, symbol="ES") for i in range(12)]
    context = vix_risk_context(bars, features=features)
    assert context["valid"] is True
    assert context["risk_off"] is True


def test_vix_quote_snapshot_context_rejects_unqualified_or_stale_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = ibkr_stock_payload("VIX", con_id=99001)
    route = route_instrument(instrument)
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    class QuoteStore:
        def lookup_instrument(self, *_args, **_kwargs) -> dict:
            return instrument

        def read_quote_snapshots(self, *_args, **_kwargs) -> list[dict]:
            observed_at = now - timedelta(minutes=2)
            return [
                {
                    "ts": observed_at.isoformat(),
                    "price": 20.0,
                    "payload": {
                        "instrument_id": route.instrument_id,
                        "route_fingerprint": route.fingerprint,
                        "provider_symbol": route.provider_symbol,
                        "price": 20.0,
                        "last": 20.0,
                        "price_source": "last",
                        "provider_ts": observed_at.isoformat(),
                        "time_basis": "provider_event",
                        "market_data_entitlement": "live",
                        "is_delayed": False,
                    },
                }
            ]

    monkeypatch.setenv("AEF_VIX_INSTRUMENT_ID", instrument["instrument_id"])

    assert load_vix_context_input(QuoteStore(), analysis_as_of_utc=now) is None


def test_vix_context_sample_rejects_non_numeric_prices() -> None:
    with pytest.raises(ValueError, match="outside the supported range"):
        analyze_features.VixContextSample(
            observed_at=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            price="20.0",  # type: ignore[arg-type]
        )


def test_trap_shift_context_detects_sweep_against_context() -> None:
    bars = [make_bar(i, 105.0, 110.0, 100.0, 105.0, 1000) for i in range(10)]
    bars.append(make_bar(10, 106.0, 112.0, 100.0, 109.0, 4500))

    context = trap_shift_context(
        bars,
        [4.0] * len(bars),
        [1.0] * (len(bars) - 1) + [2.6],
        full_bull=False,
        full_bear=True,
        mtf_direction="short",
        liquidity={"sweep_high": True},
    )

    assert context["active"]
    assert context["ready"]
    assert context["direction"] == "short"
    assert context["trap_up"]
    assert context["level"] == 110.0


def test_linda_volume_promotes_vix_warn_when_risk_off_meets_upper_rejection() -> None:
    bars = [
        make_bar(
            i, 100 + i * 0.2, 100.6 + i * 0.2, 99.9 + i * 0.2, 100.4 + i * 0.2, 1000, symbol="ES"
        )
        for i in range(60)
    ]
    bars.append(make_bar(61, 112.0, 118.0, 111.5, 111.8, 9000, symbol="ES"))

    spotlight = market_spotlight(
        bars,
        features={"vix_state": "risk-off shock"},
    )
    result = linda_volume(
        bars,
        params=LindaVolumeParams(indian_min_score=55),
        features={"vix_state": "risk-off shock"},
        market_session=spotlight,
    )

    assert result["version"] == "52-python-confirmed-1m-preview"
    assert spotlight["market_context"]["state"] in {"vix_risk", "directional_flow", "trap"}
    assert "market_context" not in result


def test_volume_close_auction_uses_exact_provider_close() -> None:
    opens_at = datetime(2026, 5, 25, 13, 30, tzinfo=timezone.utc)
    closes_at = datetime(2026, 5, 25, 20, 0, tzinfo=timezone.utc)
    session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="",
        calendar="provider_schedule",
        intervals=(
            ProviderSessionInterval(
                opens_at=opens_at,
                closes_at=closes_at,
                session_date=opens_at.date(),
            ),
        ),
    )
    assert provider_session_closing_window(
        session,
        closes_at - timedelta(minutes=4),
        minutes=5,
    )
    assert not provider_session_closing_window(
        session,
        closes_at - timedelta(minutes=6),
        minutes=5,
    )
    assert not provider_session_closing_window(
        ProviderSessionReset(True, "test", "", "continuous_24_7"),
        closes_at - timedelta(minutes=4),
        minutes=5,
    )


def test_linda_volume_returns_coloring_events_and_table() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.7 + i * 0.05, 99.8 + i * 0.05, 100.45 + i * 0.05, 1000)
        for i in range(45)
    ]
    bars.append(make_bar(46, 102.4, 106.4, 102.1, 106.0, 9000))

    spotlight = market_spotlight(bars)
    result = linda_volume(
        bars,
        params=LindaVolumeParams(indian_min_score=55),
        market_session=spotlight,
    )

    assert result["version"] == "52-python-confirmed-1m-preview"
    assert "last_signal" not in result
    assert result["series"]
    assert result["events"]
    assert result["table"]["model_ref"] == "linda_volume"
    assert result["table"]["model"] == result["latest"]["table_state"]
    assert {"columns", "accent", "bg", "text"}.isdisjoint(result["table"])
    assert "market_context" not in result
    assert "market_context" not in result["latest"]
    assert "market_context" not in result["latest"].get("table_state", {})
    assert {"market", "flow", "lock", "strategy"}.isdisjoint(
        result["latest"].get("table_state", {})
    )
    assert set(result["latest"].get("table_state", {})) == {
        "entry",
        "risk",
        "opponent",
        "invalidation",
        "action",
        "reason_code",
        "code",
        "direction",
        "metrics",
    }
    assert result["latest"]["table_state"]["entry"]["state"]
    assert result["latest"]["table_state"]["risk"]["state"]
    assert spotlight["market_context"]["direction"] in {"long", "short", "flat"}
    assert spotlight["market_context"]["vwap"]["upper_2"] is not None
    assert spotlight["market_context"]["liquidity"]["long_context"] in {True, False}
    assert result["events"][-1]["code"] in {
        "PB_UP",
        "PB_DN",
        "VW_RECLAIM",
        "VW_REJECT",
        "NO_SUPPLY",
        "NO_DEMAND",
        "",
    }
    assert result["latest"]["signal"]["source"] == "linda_volume"
    assert result["latest"]["signal"]["reason_code"]
    assert "reason" not in result["latest"]["signal"]
    assert "lifecycle" in result["latest"]
    assert result["events"][-1]["signal"]["source"] == "linda_volume"


def test_vsa_volume_returns_standalone_coloring_series() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.7 + i * 0.05, 99.8 + i * 0.05, 100.45 + i * 0.05, 1000)
        for i in range(45)
    ]
    bars.append(make_bar(46, 102.4, 106.4, 102.1, 106.0, 9000))

    result = vsa_volume(bars)

    assert result["contract"] == VSA_VOLUME_CONTRACT
    assert result["version"] == "3-vsa-breakout-context"
    assert result["status"]["mode"] == "confirmed"
    assert result["series"]
    assert result["events"]
    assert result["latest"] == result["series"][-1]
    assert {
        "display_volume",
        "display_avg",
        "rvol",
        "code",
        "reason_code",
        "trigger_event",
        "metrics",
    } <= set(result["series"][-1])
    assert {"label", "volume_label", "setup_summary", "candidate_reason"}.isdisjoint(
        result["series"][-1]
    )
    assert {"score", "rvol", "volume_rank", "volume_z", "move_atr", "range_atr", "spread"} <= set(
        result["series"][-1]["metrics"]
    )


def test_vsa_core_rejects_forming_bar_input() -> None:
    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.5),
        make_bar(1, 100.5, 102.0, 100.0, 101.5, closed=False),
    ]

    result = vsa_volume(bars)

    assert result["contract"] == VSA_VOLUME_CONTRACT
    assert result["status"]["state"] == "error"
    assert result["status"]["reason_code"] == "unconfirmed_bar_input"
    assert result["status"]["mode"] == "confirmed"
    assert result["status"]["bar_count"] == len(bars)
    assert result["series"] == []


def test_vsa_instrument_context_excludes_forming_bar_before_calculation() -> None:
    confirmed = [make_bar(index, 100.0, 101.0, 99.0, 100.5) for index in range(3)]
    forming = make_bar(3, 100.5, 102.0, 100.0, 101.5, closed=False)
    instrument = instrument_with_bar_sessions(
        ibkr_future_payload("ES"),
        [*confirmed, forming],
    )

    result = build_vsa_volume_context_for_instrument(
        [*confirmed, forming],
        instrument,
    )

    assert result["status"]["mode"] == "confirmed"
    assert result["status"]["bar_count"] == len(confirmed)
    assert result["status"]["analysis_ts"] == confirmed[-1].ts.isoformat()
    assert forming.ts.isoformat() not in {str(item.get("ts") or "") for item in result["series"]}


def test_vsa_average_keeps_canonical_sma_and_projects_only_after_calculation() -> None:
    bars = [
        make_bar(index * 2, 100, 101, 99, 100, volume)
        for index, volume in enumerate((10, 20, 30, 40))
    ]
    result = vsa_volume(
        bars,
        params=VsaVolumeParams(vol_len=3, compress_close=False, use_profile_thresholds=False),
    )
    assert [item["display_avg"] for item in result["series"]] == [10.0, 15.0, 20.0, 30.0]

    display_axis = [
        {"ts": bars[0].ts.isoformat(), "bar_slot": 0},
        {
            "ts": (bars[0].ts + timedelta(minutes=1)).isoformat(),
            "bar_slot": 1,
            "authoritative": False,
            "availability_state": "pending",
        },
        {"ts": bars[1].ts.isoformat(), "bar_slot": 2},
    ]
    aligned = align_vsa_volume_series(result["series"][:2], display_axis)

    assert aligned[0]["display_avg"] == result["series"][0]["display_avg"]
    assert aligned[1]["display_avg"] is None
    assert aligned[1]["authoritative"] is False
    assert aligned[2]["display_avg"] == result["series"][1]["display_avg"]


def test_vsa_fuel_requires_volume_spike_not_rank_tail_alone() -> None:
    bars = [
        make_bar(
            i,
            100 + i * 0.05,
            100.7 + i * 0.05,
            99.8 + i * 0.05,
            100.45 + i * 0.05,
            1000,
            step_minutes=5,
        )
        for i in range(45)
    ]
    # Elevated but not 2.5x spike: should route to impulse/exhaust, not CLX paint flag.
    bars.append(make_bar(46, 102.0, 103.4, 101.8, 102.9, 2400, step_minutes=5))

    result = vsa_volume(bars)
    latest = result["latest"]
    assert latest is not None
    assert latest["fuel"] is False
    assert latest["code"] in {"IMP_UP", "FUEL_UP", "EXH_DN", "ABS", "ABS_LVL", ""}


def test_vsa_extreme_spike_still_marks_fuel() -> None:
    bars = [
        make_bar(
            i,
            100 + i * 0.05,
            100.7 + i * 0.05,
            99.8 + i * 0.05,
            100.45 + i * 0.05,
            1000,
            step_minutes=5,
        )
        for i in range(45)
    ]
    bars.append(make_bar(46, 102.4, 106.4, 102.1, 106.0, 9000, step_minutes=5))

    result = vsa_volume(bars)
    latest = result["latest"]

    assert latest is not None
    assert latest["fuel"] is True
    assert latest["code"] in {"FUEL_UP", "EXH_DN"}


def test_fuel_bar_is_not_terminal_climax() -> None:
    bars = [
        make_bar(
            i,
            100 + i * 0.05,
            100.7 + i * 0.05,
            99.8 + i * 0.05,
            100.45 + i * 0.05,
            1000,
            step_minutes=5,
        )
        for i in range(45)
    ]
    bars.append(make_bar(46, 102.4, 106.4, 102.1, 106.0, 9000, step_minutes=5))

    result = vsa_volume(bars)
    latest = result["latest"]

    assert latest is not None
    assert latest["fuel"] is True
    assert latest["terminal_climax"] is False


def test_terminal_climax_kinds_exclude_fuel_confirm() -> None:
    bars = [
        make_bar(
            i,
            100 + i * 0.08,
            100.8 + i * 0.08,
            99.7 + i * 0.08,
            100.5 + i * 0.08,
            1000,
            step_minutes=5,
        )
        for i in range(120)
    ]
    result = vsa_volume(bars)
    kinds = {
        str(item.get("terminal_kind") or "")
        for item in result["series"]
        if item.get("terminal_climax")
    }
    assert "clx_fuel_confirm" not in kinds
    assert "clx_test" not in kinds
    assert kinds <= {"clx_exh"}


def test_terminal_climax_dual_confirm_patterns() -> None:
    from aef_terminal.features.vsa_classify import (
        _confirm_absorption_push,
        _confirm_pressure_release,
    )

    class ConfirmFact:
        rvol = 0.95
        spread_rel = 0.80
        body_share = 0.70

    class PushFact:
        rvol = 1.45
        spread_rel = 1.10
        body_share = 0.65

    down_bar = make_bar(1, 100.0, 100.5, 99.0, 99.2, 800)
    up_bar = make_bar(1, 99.0, 100.0, 98.8, 99.8, 800)
    assert _confirm_pressure_release(down_bar, ConfirmFact(), buying_climax=True)
    assert _confirm_absorption_push(down_bar, PushFact(), buying_climax=True)
    assert not _confirm_pressure_release(up_bar, ConfirmFact(), buying_climax=True)


def test_linda_volume_adds_tradingview_vwap_price_marks() -> None:
    bars = [
        make_bar(i, 100 + i * 0.02, 100.4 + i * 0.02, 99.8 + i * 0.02, 100.25 + i * 0.02, 1000)
        for i in range(60)
    ]
    upper = linda_volume(
        [*bars, make_bar(61, 112.0, 118.0, 111.5, 111.8, 9000)],
        params=LindaVolumeParams(indian_min_score=55),
    )
    lower = linda_volume(
        [*bars, make_bar(61, 95.0, 95.5, 88.0, 94.5, 9000)],
        params=LindaVolumeParams(indian_min_score=55),
    )

    upper_marks = [
        item for item in upper["overlays"] if item.get("role") == "linda_vwap_upper_2_ride"
    ]
    lower_marks = [
        item for item in lower["overlays"] if item.get("role") == "linda_vwap_lower_2_touch"
    ]

    assert upper_marks
    assert "lines" not in upper_marks[0]
    assert upper_marks[0]["code"] == "LINDA_VWAP_UPPER_2_RIDE"
    assert upper_marks[0]["tone"] == "warning"
    assert upper_marks[0]["label_style"] == "text"
    assert upper_marks[0]["pointer"] is False
    assert upper_marks[0]["scenario"] == "vwap_upper_2_band_ride"
    assert upper_marks[0]["trigger_event"] == {"code": "return_inside_band_with_vsa_or_structure"}
    assert upper_marks[0]["metrics"]["upper_2_sigma"]["digits"] == 2
    assert upper_marks[0]["metrics"]["touches_8b"]["digits"] == 0
    assert lower_marks
    assert "lines" not in lower_marks[0]
    assert lower_marks[0]["code"] == "LINDA_VWAP_LOWER_2_TOUCH"
    assert lower_marks[0]["tone"] == "info"
    assert lower_marks[0]["label_style"] == "text"
    assert lower_marks[0]["pointer"] is False
    assert lower_marks[0]["scenario"] == "vwap_lower_2_touch"
    assert lower_marks[0]["setup"] == "oversold_zone_context"
    assert lower_marks[0]["metrics"]["lower_2_sigma"]["digits"] == 2


def test_linda_volume_adds_exhaustion_warning_layer() -> None:
    bars = [
        make_bar(i, 100 + i * 0.02, 100.4 + i * 0.02, 99.8 + i * 0.02, 100.25 + i * 0.02, 1000)
        for i in range(60)
    ]
    result = linda_volume(
        [*bars, make_bar(61, 112.0, 118.0, 111.5, 111.8, 9000)],
        params=LindaVolumeParams(indian_min_score=55),
    )
    warnings = [
        item
        for item in result["overlays"]
        if item.get("role") == "linda_exhaustion_reversal_warning"
    ]

    assert "market_context" not in result
    assert warnings
    assert "lines" not in warnings[0]
    assert warnings[0]["code"] == "REV_WARN"
    assert warnings[0]["tone"] == "warning"
    assert warnings[0]["scenario"] == "reversal_warning"
    assert warnings[0]["setup"] == "profit_protection_warning"
    assert warnings[0]["risk"] == {"code": "trend_continuation_rising_volume"}


def test_linda_volume_adds_pine_context_levels_and_poi_zone() -> None:
    previous_base = datetime(2026, 1, 2, 20, 0, tzinfo=timezone.utc)
    current_base = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    previous = [
        make_bar(
            i,
            99.0 + i * 0.05,
            100.0 + i * 0.05,
            98.5 + i * 0.05,
            99.5 + i * 0.05,
            1000,
            timeframe="5m",
            base_ts=previous_base,
            step_minutes=5,
        )
        for i in range(18)
    ]
    opening = [
        make_bar(
            i,
            100.0,
            101.5 + (i % 2) * 0.25,
            99.2 - (i % 2) * 0.1,
            100.8,
            1000,
            timeframe="5m",
            base_ts=current_base,
            step_minutes=5,
        )
        for i in range(6)
    ]
    continuation = [
        make_bar(
            i + 6,
            101.0 + i * 0.12,
            102.0 + i * 0.12,
            100.7 + i * 0.12,
            101.6 + i * 0.12,
            1000,
            timeframe="5m",
            base_ts=current_base,
            step_minutes=5,
        )
        for i in range(30)
    ]
    impulse = make_bar(
        37, 105.0, 107.2, 104.8, 106.8, 1500, timeframe="5m", base_ts=current_base, step_minutes=5
    )
    pullback = make_bar(
        38, 106.7, 106.9, 105.4, 105.8, 1000, timeframe="5m", base_ts=current_base, step_minutes=5
    )

    result = linda_volume(
        [*previous, *opening, *continuation, impulse, pullback],
        params=LindaVolumeParams(
            indian_min_score=55, use_profile_thresholds=False, use_adaptive=False
        ),
    )
    spotlight_context = market_spotlight([*previous, *opening, *continuation, impulse, pullback])[
        "market_context"
    ]
    roles = {item.get("role") for item in result["overlays"]}

    assert isinstance(spotlight_context, dict)
    assert "levels" not in spotlight_context
    assert {"opening_range", "poi"}.issubset(spotlight_context)
    assert "market_context" not in result
    assert "linda_level_pdc" not in roles
    assert "linda_level_or_high" not in roles
    assert "linda_level_or_low" not in roles
    assert any(role in roles for role in {"linda_poi_core", "linda_poi_deep", "linda_poi_label"})
    poi_overlays = [
        item
        for item in result["overlays"]
        if item.get("role") in {"linda_poi_core", "linda_poi_deep", "linda_poi_label"}
    ]
    assert poi_overlays
    assert all(item.get("retention") == "active" for item in poi_overlays)


def test_linda_volume_adds_grail_buy_mark_after_ema20_retest() -> None:
    previous_base = datetime(2026, 1, 2, 20, 0, tzinfo=timezone.utc)
    current_base = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    previous = [
        make_bar(
            i,
            99.0 + i * 0.05,
            100.0 + i * 0.05,
            98.5 + i * 0.05,
            99.5 + i * 0.05,
            1000,
            timeframe="5m",
            base_ts=previous_base,
            step_minutes=5,
        )
        for i in range(18)
    ]
    opening = [
        make_bar(
            i,
            100.0,
            101.5 + (i % 2) * 0.25,
            99.2 - (i % 2) * 0.1,
            100.8,
            1000,
            timeframe="5m",
            base_ts=current_base,
            step_minutes=5,
        )
        for i in range(6)
    ]
    continuation = [
        make_bar(
            i + 6,
            101.0 + i * 0.12,
            102.0 + i * 0.12,
            100.7 + i * 0.12,
            101.6 + i * 0.12,
            1000,
            timeframe="5m",
            base_ts=current_base,
            step_minutes=5,
        )
        for i in range(30)
    ]
    impulse = make_bar(
        37, 105.0, 107.2, 104.8, 106.8, 1500, timeframe="5m", base_ts=current_base, step_minutes=5
    )
    retest = make_bar(
        38, 105.0, 105.6, 103.9, 104.9, 2000, timeframe="5m", base_ts=current_base, step_minutes=5
    )

    result = linda_volume(
        [*previous, *opening, *continuation, impulse, retest],
        params=LindaVolumeParams(
            indian_min_score=55, use_profile_thresholds=False, use_adaptive=False
        ),
    )
    grail_marks = [item for item in result["overlays"] if item.get("role") == "linda_grail_buy"]

    assert "market_context" not in result
    assert grail_marks
    assert grail_marks[0]["direction"] == "long"
    assert "lines" not in grail_marks[0]
    assert grail_marks[0]["code"] == "GRAIL_BUY"
    assert grail_marks[0]["action"] == "GO"
    assert grail_marks[0]["scenario"] == "linda_grail"
    assert grail_marks[0]["setup"] == "continuation_long"
    assert grail_marks[0]["trigger_event"] == {"code": "grail_level_hold", "direction": "long"}
    assert {fact["code"] for fact in grail_marks[0]["evidence"]["context"]} == {"ema20_retest"}
    assert grail_marks[0]["risk"] == {"code": "stop_loss", "price": grail_marks[0]["stop"]}
    assert grail_marks[0]["metrics"]["stop"]["digits"] == 2
    assert grail_marks[0]["metrics"]["rr"]["digits"] == 2


def test_linda_volume_keeps_impulse_context_internal() -> None:
    base_ts = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    bars = [
        make_bar(
            i,
            100 + i * 0.1,
            101 + i * 0.1,
            99.7 + i * 0.1,
            100.6 + i * 0.1,
            1000,
            timeframe="5m",
            base_ts=base_ts,
            step_minutes=5,
        )
        for i in range(40)
    ]
    impulse = make_bar(
        41, 104.5, 107.0, 104.2, 106.7, 1800, timeframe="5m", base_ts=base_ts, step_minutes=5
    )

    result = linda_volume(
        [*bars, impulse],
        params=LindaVolumeParams(
            indian_min_score=55, use_profile_thresholds=False, use_adaptive=False
        ),
    )
    linda_impulse_marks = [
        item
        for item in result["overlays"]
        if str(item.get("role") or "").startswith("linda_impulse_")
    ]

    assert "market_context" not in result
    assert result["alerts"] == []
    assert linda_impulse_marks == []
    assert result["plan"]["action"] == "WAIT"
    assert result["latest"]["signal"]["action"] in {"WAIT", "GO", "WATCH"}
    assert result["latest"]["signal"]["blocked"] is False
    assert result["latest"]["signal"]["blocked_reason"] == ""
    assert (
        signal_candidates_from_indicator_state(result, source="linda_volume", score_floor=40) == []
    )


def test_micro_range_context_detects_tight_breakout() -> None:
    bars = [
        make_bar(0, 100.00, 100.30, 99.90, 100.12),
        make_bar(1, 100.12, 100.36, 100.02, 100.18),
        make_bar(2, 100.18, 100.34, 100.04, 100.20),
        make_bar(3, 100.20, 100.38, 100.05, 100.22),
        make_bar(4, 100.22, 100.35, 100.06, 100.24),
        make_bar(5, 100.24, 100.40, 100.08, 100.26),
        make_bar(6, 100.28, 101.05, 100.25, 100.92),
    ]

    context = micro_range_context(bars, [1.0] * len(bars), direction="long", context_ok=True)

    assert context["active"] is True
    assert context["direction"] == "long"
    assert context["break"] is True
    assert context["watch"] is False
    assert context["high"] == 100.4
    assert context["low"] == 100.02
    assert context["range_atr"] == 0.38


def test_side_veto_context_respects_unlock_and_contra_permission() -> None:
    blocked = side_veto_context(long_block_lock=True, short_block_lock=True)
    unlocked = side_veto_context(
        long_block_lock=True, short_block_lock=True, long_unlock=True, short_contra_allowed=True
    )

    assert blocked["veto_long"] is True
    assert blocked["veto_short"] is True
    assert blocked["preferred_direction"] == "flat"
    assert unlocked["veto_long"] is False
    assert unlocked["veto_short"] is False
    assert unlocked["long_unlock"] is True
    assert unlocked["short_contra_allowed"] is True


def test_poi_entry_gate_context_blocks_wrong_side_until_hot_or_bypassed() -> None:
    blocked = poi_entry_gate_context(
        poi_active=True,
        poi_direction="short",
        poi_hot=True,
        long_confirm_trigger=True,
        short_confirm_trigger=True,
    )
    bypassed = poi_entry_gate_context(
        poi_active=True,
        poi_direction="short",
        poi_hot=True,
        long_bypass=True,
        long_warn_trigger=True,
    )

    assert blocked["long"]["allowed"] is False
    assert blocked["long"]["blocked_reason"] == "poi_not_hot"
    assert blocked["long"]["confirm"] is False
    assert blocked["short"]["allowed"] is True
    assert blocked["short"]["confirm"] is True
    assert bypassed["long"]["allowed"] is True
    assert bypassed["long"]["warn"] is True
    assert bypassed["long"]["bypass"] is True


def test_strategy_banner_context_prioritizes_execute_over_impulse() -> None:
    execute = strategy_banner_context(
        execute_long=True,
        impulse_confirmed=True,
        impulse_direction="short",
        impulse_class="IGNITION",
        impulse_action="wait pullback",
        price=101.23456,
        flow_bias=4,
    )
    impulse = strategy_banner_context(
        impulse_confirmed=True,
        impulse_direction="short",
        impulse_class="CONTINUATION",
        impulse_action="pullback/retest",
    )

    assert execute["phase"] == "execute"
    assert execute["direction"] == "long"
    assert execute["directive"] == "enter_from_level_retest_if_stretched"
    assert execute["price"] == 101.2346
    assert impulse["phase"] == "impulse_confirmed"
    assert impulse["direction"] == "short"
    assert impulse["directive"] == "pullback/retest"
    assert not {"text", "summary", "sub", "action"}.intersection(execute)
    assert not {"text", "summary", "sub", "action"}.intersection(impulse)


def test_linda_volume_uses_pine_style_profile_thresholds() -> None:
    spy_bars = [make_bar(i, 100, 101, 99, 100.2, 1000) for i in range(32)]
    spy_bars = [
        bar.__class__(
            symbol="SPY",
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timeframe=bar.timeframe,
        )
        for bar in spy_bars
    ]
    oil_bars = [make_bar(i, 100, 101, 99, 100.2, 1000) for i in range(32)]
    oil_bars = [
        bar.__class__(
            symbol="CL",
            ts=bar.ts,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            timeframe=bar.timeframe,
        )
        for bar in oil_bars
    ]

    spy = linda_volume(spy_bars)
    oil = linda_volume(oil_bars)

    assert spy["params"]["profile"] == "SPY"
    assert spy["params"]["impulse_rvol"] == 1.8
    assert oil["params"]["profile"] == "Crude / CL"
    assert oil["params"]["impulse_rvol"] == 2.3
    assert oil["params"]["fuel_rvol"] > spy["params"]["fuel_rvol"]


def test_linda_volume_uses_qqq_profile_thresholds() -> None:
    qqq_bars = [make_bar(i, 100, 101, 99, 100.2, 1000, symbol="QQQ") for i in range(32)]

    qqq = linda_volume(qqq_bars)

    assert qqq["params"]["profile"] == "QQQ"
    assert qqq["params"]["impulse_rvol"] == 2.1
    assert qqq["params"]["fuel_rvol"] == 2.6


def test_vsa_volume_marks_absorption_at_local_level() -> None:
    bars = [make_bar(i, 100, 101, 99, 100.1, 1000) for i in range(40)]
    bars.append(make_bar(41, 100.1, 101.0, 99.8, 100.55, 8500))

    result = vsa_volume(bars)
    latest = result["series"][-1]

    assert result["events"][-1]["code"] == "ABS_LVL"
    assert result["events"][-1]["reason_code"] == "vsa_abs_lvl"
    assert "label" not in result["events"][-1]
    assert latest["absorption_level"] is True
    assert latest["absorption_role"] in {"resistance", "support"}


def test_vsa_volume_surfaces_spring_and_upthrust_as_named_events() -> None:
    base = [make_bar(i, 100, 101, 99, 100.2, 1000) for i in range(40)]
    spring = vsa_volume([*base, make_bar(41, 100.0, 101.0, 95.0, 100.8, 6000)])
    upthrust = vsa_volume([*base, make_bar(41, 100.0, 105.0, 99.0, 100.2, 6000)])

    assert spring["events"][-1]["code"] == "SPRING"
    assert spring["events"][-1]["reason_code"] == "vsa_spring"
    assert upthrust["events"][-1]["code"] == "UPTHRUST"
    assert upthrust["events"][-1]["reason_code"] == "vsa_upthrust"
    spring_marks = [item for item in spring["overlays"] if item.get("role") == "vsa_price_mark"]
    assert spring_marks
    assert spring_marks[-1]["type"] == "label"
    assert spring_marks[-1]["code"] == "SPRING"
    assert spring_marks[-1]["structure_text_role"] == "vsa"
    assert spring_marks[-1]["control_key"] == "priceMarks"
    assert "tooltip" not in spring_marks[-1]
    assert spring_marks[-1]["scenario"] == "vsa_volume"
    assert spring_marks[-1]["trigger_event"]
    assert spring_marks[-1]["quality"] == {
        "code": "score",
        "value": spring["series"][-1]["score"],
    }
    assert spring_marks[-1]["metrics"]["score"]["digits"] == 0
    assert "range_atr" in spring_marks[-1]["metrics"]


def test_linda_volume_surfaces_three_indian_pullback_stage() -> None:
    bars = []
    price = 100.0
    for index in range(60):
        open_ = price
        close = price + 0.20
        bars.append(make_bar(index, open_, close + 0.18, open_ - 0.80, close, 1000, timeframe="5m"))
        price = close
    bars.append(
        make_bar(60, price - 0.35, price - 0.05, price - 2.00, price - 0.10, 900, timeframe="5m")
    )

    result = linda_volume(bars, params=LindaVolumeParams(indian_min_score=55))
    indian_events = [event for event in result["events"] if event.get("indian_count")]

    assert indian_events
    assert indian_events[-1]["code"] == "PB_UP"
    assert indian_events[-1]["structure_text_ref"] == "linda_volume"
    assert indian_events[-1]["structure_text_role"] == "linda_indian"
    assert indian_events[-1]["indian_count"] == 1
    assert indian_events[-1]["reason_code"] == "linda_pb_up"
    assert indian_events[-1]["trigger_event"] == {"code": "pb_up"}
    assert indian_events[-1]["evidence"]["supporting"] == [
        {
            "code": "indian_pullback_stage",
            "count": 1,
            "direction": "long",
        }
    ]
    assert {"label", "indian_stage", "indian_advice"}.isdisjoint(indian_events[-1])
    indian_marks = [
        item for item in result["overlays"] if item.get("role") == "linda_indian_price_mark"
    ]
    assert indian_marks
    assert indian_marks[-1]["type"] == "label"
    assert indian_marks[-1]["code"] == "PB_UP"
    assert indian_marks[-1]["indian_count"] == 1
    assert indian_marks[-1]["structure_text_role"] == "linda_indian"
    assert {"label", "indian_stage", "indian_advice"}.isdisjoint(indian_marks[-1])
    assert result["table"]["model_ref"] == "linda_volume"


def test_linda_volume_counts_second_indian_pullback_like_pine() -> None:
    base_ts = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    def make_5m_bar(
        index: int, open_: float, high: float, low: float, close: float, volume: float = 1000
    ) -> Bar:
        return Bar(
            symbol="ES",
            ts=base_ts + timedelta(minutes=5 * index),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            timeframe="5m",
        )

    bars = []
    price = 100.0
    for index in range(80):
        open_ = price
        close = price + 0.22
        bars.append(make_5m_bar(index, open_, close + 0.15, open_ - 0.05, close, 1200))
        price = close
    bars.append(make_5m_bar(80, price - 0.35, price - 0.05, price - 2.40, price - 0.12, 1000))
    price = bars[-1].close
    for offset in range(1, 7):
        open_ = price
        close = price + 0.22
        bars.append(make_5m_bar(80 + offset, open_, close + 0.15, open_ - 0.05, close, 1100))
        price = close
    bars.append(make_5m_bar(87, price - 0.35, price - 0.05, price - 3.20, price - 0.12, 1000))

    result = linda_volume(bars, params=LindaVolumeParams(indian_min_score=55))
    indian_events = [item for item in result["series"] if item.get("indian_count")]

    assert [item["indian_count"] for item in indian_events[-2:]] == [1, 2]
    assert indian_events[-1]["code"] == "PB_UP"
    assert indian_events[-1]["reason_code"] == "linda_pb_up"
    assert indian_events[-1]["evidence"]["supporting"][0]["count"] == 2


def test_linda_signal_promotes_to_shared_decision_candidate() -> None:
    bars = [make_bar(i, 100, 101, 99, 100.2, 1000) for i in range(40)]
    bars.append(make_bar(41, 100.2, 100.8, 99.9, 100.0, 400))
    result = linda_volume(bars)

    candidates = signal_candidates_from_indicator_state(
        result, source="linda_volume", score_floor=40
    )

    assert candidates
    assert candidates[-1].details["source"] == "linda_volume"
    assert candidates[-1].name == "linda_fade"
    assert candidates[-1].kind == ScenarioKind.FADE


def test_vsa_fuel_context_stays_out_of_linda_plan_actions() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.7 + i * 0.05, 99.8 + i * 0.05, 100.45 + i * 0.05, 1000)
        for i in range(45)
    ]
    bars.append(make_bar(46, 102.4, 106.4, 102.1, 106.0, 9000))

    vsa = vsa_volume(bars)
    linda = linda_volume(bars)
    event = vsa["events"][-1]

    assert event["code"] == "FUEL_UP"
    assert event["fuel"] is True
    assert event["reason_code"] == "vsa_fuel_up"
    assert {"label", "volume_label"}.isdisjoint(event)
    assert linda["plan"]["action"] not in {"WATCH FUEL", "WATCH CLX", "WATCH CLIMAX"}
    assert "signal" not in linda["plan"]
    assert isinstance(linda["latest"].get("signal"), dict)


@pytest.mark.parametrize(
    ("context_source", "expected_weight"),
    (("market_spotlight", 1.0), ("custom_market_context", 0.62)),
)
def test_direction_sentiment_uses_typed_market_context_and_gates_wolfe_reversal(
    context_source: str,
    expected_weight: float,
) -> None:
    decision = ScenarioDecision(
        kind=ScenarioKind.WAIT,
        direction=Direction.FLAT,
        confidence=0,
        action=ActionPhase.WAIT,
        trigger=None,
        stop=None,
        target=None,
        invalidation=None,
        trigger_event=DomainFact("wait"),
    )
    sentiment = build_direction_sentiment(
        decision=decision,
        indicators={
            context_source: {
                "sentiment_role": "market_context",
                "market_context": {
                    "direction": "long",
                    "score": 88,
                    "state": "UP BAND RIDE",
                    "risk_lock": "no_short",
                    "side_lock": "LONG",
                    "warning_direction": "short",
                },
            },
            "wolfe_structure": {
                "latest": {
                    "state": "wolfe_confirmed",
                    "signal": {
                        "direction": "short",
                        "action": "GO",
                        "score": 98,
                    },
                }
            },
        },
        candidates=[],
    )

    assert sentiment["direction"] == "long"
    market_context = next(
        item for item in sentiment["contributors"] if item["source"] == context_source
    )
    wolfe = next(item for item in sentiment["contributors"] if item["source"] == "wolfe_structure")
    assert market_context["weight"] == expected_weight
    assert market_context["weight_source"] == context_source
    assert wolfe["action"] == "WATCH"
    assert wolfe["weight"] == 0.0
    assert wolfe["blocked_by_context"] is True


def test_direction_sentiment_gates_wolfe_reversal_by_spotlight_strategy_arm() -> None:
    decision = ScenarioDecision(
        kind=ScenarioKind.WAIT,
        direction=Direction.FLAT,
        confidence=0,
        action=ActionPhase.WAIT,
        trigger=None,
        stop=None,
        target=None,
        invalidation=None,
        trigger_event=DomainFact("wait"),
    )
    sentiment = build_direction_sentiment(
        decision=decision,
        indicators={
            "market_spotlight": {
                "sentiment_role": "market_context",
                "market_context": {
                    "direction": "long",
                    "score": 72,
                    "state": "TREND LONG",
                    "risk_lock": "none",
                    "side_lock": "CHECK",
                    "strategy": {
                        "active": True,
                        "phase": "armed",
                        "direction": "long",
                        "summary": "ARM L",
                        "action": "ARM",
                    },
                },
            },
            "wolfe_structure": {
                "latest": {
                    "state": "wolfe_confirmed",
                    "signal": {
                        "direction": "short",
                        "action": "GO",
                        "score": 98,
                    },
                }
            },
        },
        candidates=[],
    )

    assert sentiment["direction"] == "long"
    spotlight = next(
        item for item in sentiment["contributors"] if item["source"] == "market_spotlight"
    )
    wolfe = next(item for item in sentiment["contributors"] if item["source"] == "wolfe_structure")
    assert spotlight["action"] == "ARM"
    assert spotlight["strategy_phase"] == "armed"
    assert spotlight["strategy_direction"] == "long"
    assert spotlight["weight"] == 1.15
    assert wolfe["action"] == "WATCH"
    assert wolfe["weight"] == 0.0
    assert wolfe["blocked_by_context"] is True
    assert wolfe["blocked_by_strategy"] is True


def _canonical_manual_channel(
    bars: list[Bar],
    *,
    channel_id: str = "manual-up",
    first_price: float = 100.0,
    second_price: float = 100.0,
    offset_price: float = 120.0,
    first_index: int = 0,
    second_index: int = -1,
    offset_index: int = 0,
    canonical_generation: int = 7,
    instrument_id: str = "test-instrument",
    route_fingerprint: str = "test-route",
    **settings: object,
) -> dict[str, object]:
    resolved_second = second_index % len(bars)
    anchor_indexes = sorted({first_index, resolved_second, offset_index})
    origin_index = anchor_indexes[0]
    timeframe = bars[-1].timeframe
    return {
        "id": channel_id,
        "type": "channel",
        "points": [
            {"ts": bars[first_index].ts.isoformat(), "price": first_price},
            {
                "ts": bars[resolved_second].ts.isoformat(),
                "price": second_price,
            },
        ],
        "offsetPoint": {
            "ts": bars[offset_index].ts.isoformat(),
            "price": offset_price,
        },
        "anchorResolution": {
            "status": "resolved",
            "reasons": [],
        },
        "anchorProjection": {
            "instrumentId": instrument_id,
            "routeFingerprint": route_fingerprint,
            "timeframe": timeframe,
            "canonicalGeneration": canonical_generation,
            "originTs": bars[origin_index].ts.isoformat(),
            "confirmedThroughTs": bars[-1].ts.isoformat(),
            "confirmedCount": len(bars) - origin_index,
            "anchors": [
                {
                    "ts": bars[index].ts.isoformat(),
                    "logicalIndex": index - origin_index,
                }
                for index in anchor_indexes
            ],
        },
        **settings,
    }


def test_manual_channel_context_uses_confirmed_logical_quarters_and_midline() -> None:
    bars = [make_bar(i, 109.8, 110.4, 109.7, 110.1, 1200) for i in range(30)]
    channel = _canonical_manual_channel(bars, showGuides=True)

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        canonical_generation=7,
    )

    assert context["active"]
    assert context["count"] == 1
    assert context["nearest"]["label"] == "MID"
    assert context["nearest"]["role"] == "midline"
    assert context["nearest"]["touch"]
    assert context["features"]["manual_channel_role"] == "midline"


def test_manual_channel_context_evaluates_every_canonical_channel() -> None:
    bars = [make_bar(i, 109.8, 110.4, 109.7, 110.1, 1200) for i in range(30)]
    channels = [
        _canonical_manual_channel(
            bars,
            channel_id=f"manual-{index}",
            offset_price=120.0 + index,
        )
        for index in range(9)
    ]

    context = manual_channel_context(
        bars,
        channels,
        atr_value=4.0,
        canonical_generation=7,
    )

    assert context["count"] == 9
    assert {channel["id"] for channel in context["channels"]} == {
        f"manual-{index}" for index in range(9)
    }


def test_manual_channel_context_does_not_require_provider_slots() -> None:
    bars = [make_bar(i, 109.8, 110.4, 109.7, 110.1, 1200) for i in range(30)]
    channel = _canonical_manual_channel(bars, showGuides=True)

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        canonical_generation=7,
    )

    assert context["active"] is True
    assert "analysis_slot" not in context
    assert context["analysis_logical_index"] == len(bars) - 1


def test_manual_channel_context_rejects_noncanonical_slot_and_scalar_offset_fields() -> None:
    bars = [make_bar(i, 109.8, 110.4, 109.7, 110.1, 1200) for i in range(2)]
    channel = {
        "id": "legacy-channel",
        "type": "channel",
        "points": [
            {"bar_slot": 0, "price": 100.0},
            {"bar_slot": 5, "price": 100.0},
        ],
        "offset": 20.0,
    }

    context = manual_channel_context(bars, [channel], atr_value=4.0)

    assert context["active"] is False
    assert context["count"] == 0


def test_manual_channel_context_keeps_timestamp_aligned_stale_projection_active() -> None:
    bars = [make_bar(i, 109.8, 110.4, 109.7, 110.1, 1200) for i in range(30)]
    channel = _canonical_manual_channel(bars, showGuides=True)

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        canonical_generation=8,
    )

    assert context["active"] is True
    assert context["count"] == 1
    assert "availability" not in context


def test_long_lived_off_window_channel_stays_active_while_1m_shadow_fails_closed() -> None:
    bars = [make_bar(i, 1098.0, 1101.0, 1097.0, 1099.0, 1200) for i in range(30)]
    origin = bars[0].ts - timedelta(days=90)
    through = bars[-1].ts
    channel = {
        "id": "long-lived-relative",
        "type": "channel",
        "points": [
            {"ts": origin.isoformat(), "price": 100.0},
            {
                "anchorTs": origin.isoformat(),
                "barOffset": 10,
                "price": 110.0,
            },
        ],
        "offsetPoint": {"ts": origin.isoformat(), "price": 120.0},
        "anchorResolution": {
            "status": "resolved",
            "reasons": [],
        },
        "anchorProjection": {
            "instrumentId": "test-instrument",
            "routeFingerprint": "test-route",
            "timeframe": bars[-1].timeframe,
            "canonicalGeneration": 11,
            "originTs": origin.isoformat(),
            "confirmedThroughTs": through.isoformat(),
            "confirmedCount": 1000,
            "anchors": [{"ts": origin.isoformat(), "logicalIndex": 0}],
        },
    }

    result = channel_master_indicator(
        bars,
        [channel],
        atr_value=4.0,
        canonical_generation=11,
    )

    assert result["active"] is True
    assert result["analysis_logical_index"] == len(bars) - 1
    assert result["decision_anchor_ts"] == bars[-1].ts.isoformat()
    assert result["decision_bar_offset"] == 1
    assert (
        result["decision_available_at"]
        == expiry_after_bars(
            bars[-1].ts,
            bars[-1].timeframe,
            1,
        ).isoformat()
    )
    assert result["channels"][0]["base"] == pytest.approx(1099.0)
    assert result["channels"][0]["slope_per_confirmed_bar"] == 1.0
    assert len(result["channels"][0]["definition_revision"]) == 64
    assert result["interaction_model"] == {
        "state": "blocked",
        "reason_code": "parent_logical_channel_1m_projection_unavailable",
        "phase": "pre_touch",
        "channel_id": "long-lived-relative",
        "level": result["nearest"]["level"],
        "level_price": result["nearest"]["price"],
        "decision_logical_index": len(bars) - 1,
        "near": result["nearest"]["near"],
        "required_context": "exact_parent_confirmed_index_to_confirmed_1m_mapping",
    }
    assert "research_observations" not in result


def test_channel_master_calc_gate_owns_context_features_and_candidates() -> None:
    bars = [make_bar(i, 109.8, 110.4, 109.7, 110.1, 1200) for i in range(44)]
    bars.append(make_bar(44, 110.02, 110.12, 109.93, 110.1, 1200))
    slots = ProviderBarSlotSequence(
        [i * 5 for i in range(len(bars))],
        schedule_state="verified",
    )
    instrument = ibkr_future_payload("ES")
    route = route_instrument(instrument)
    channel = _canonical_manual_channel(
        bars,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        showGuides=True,
    )

    disabled = analyze_bars(
        bars,
        instrument=instrument,
        indicator_params={
            "manual_channels": [channel],
            "channel_master": {"enabled": False},
        },
        bar_slots=slots,
    )

    assert "manual_channels" not in disabled
    assert "channel_master" not in disabled["indicators"]
    assert not any(key.startswith("manual_channel_") for key in disabled["features"])
    assert not any(
        candidate.get("name") == "manual_channel_reaction" for candidate in disabled["candidates"]
    )

    enabled = analyze_bars(
        bars,
        instrument=instrument,
        indicator_params={
            "manual_channels": [channel],
            "manual_channel_canonical_generation": 7,
            "channel_master": {"enabled": True},
        },
        bar_slots=slots,
    )

    context = enabled["indicators"]["channel_master"]
    assert "manual_channels" not in enabled
    assert context["count"] == 1
    assert context["analysis_logical_index"] == len(bars) - 1
    assert "analysis_slot" not in context
    assert context["analysis_ts"] == bars[-1].ts.isoformat()
    assert context["decision_anchor_ts"] == bars[-1].ts.isoformat()
    assert context["decision_bar_offset"] == 1
    assert (
        context["decision_available_at"]
        == expiry_after_bars(
            bars[-1].ts,
            bars[-1].timeframe,
            1,
        ).isoformat()
    )
    analysis_atr = enabled["features"]["atr"]
    assert analysis_atr > (bars[-1].high - bars[-1].low) * 2
    assert not any(key.startswith("manual_channel_") for key in enabled["features"])
    assert context["features"]["manual_channel_active"] is True
    nearest = context["nearest"]
    channel = context["channels"][0]
    assert nearest["decision_price"] == pytest.approx(
        nearest["price"] + channel["slope_per_confirmed_bar"]
    )
    assert 5 <= nearest["break_score"] <= 95
    assert 5 <= nearest["raw_break_score"] <= 95
    assert nearest["bounce_score"] == 100 - nearest["break_score"]
    assert nearest["primary_path"] in {"break", "bounce"}
    assert isinstance(nearest["no_fade"], bool)
    assert nearest["distance_atr"] == pytest.approx(
        abs(nearest["distance"]) / analysis_atr,
        abs=0.01,
    )
    assert nearest["breakout_target"] is not None
    assert nearest["bounce_target"] is not None
    candidate = next(
        candidate
        for candidate in enabled["candidates"]
        if candidate["name"] == "manual_channel_reaction"
    )
    assert candidate["reason_code"] == "channel_master_reaction"
    assert candidate["details"]["source"] == "channel_master"


def test_manual_channel_candidate_is_watch_only_context() -> None:
    bars = [make_bar(i, 119.8, 120.2, 119.6, 119.9, 1200) for i in range(30)]
    channel = _canonical_manual_channel(
        bars,
        channel_id="manual-top",
        showGuides=True,
    )

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        canonical_generation=7,
    )
    candidates = signal_candidates_from_channel_master(context)

    assert candidates
    assert candidates[0].name == "manual_channel_reaction"
    assert candidates[0].source == "channel_master"
    assert candidates[0].reason == "channel_master_reaction"
    assert candidates[0].direction == Direction.SHORT
    assert candidates[0].score < 78
    assert candidates[0].details["plan_complete"] is False
    assert candidates[0].details["blocked_reason"] == "incomplete trade plan"
    assert candidates[0].details["plan_inference"] == "manual_channel_level_plan"
    assert candidates[0].details["plan_inference"] != "decision_atr_fallback"


def test_manual_channel_no_fade_suppresses_countertrend_candidate_before_touch_rejection() -> None:
    bars = [
        make_bar(
            i,
            114.0 + i * 0.12,
            114.4 + i * 0.12,
            113.8 + i * 0.12,
            114.2 + i * 0.12,
            1000,
        )
        for i in range(26)
    ]
    bars.extend(
        [
            make_bar(26, 117.2, 118.0, 117.0, 117.8, 1400),
            make_bar(27, 117.8, 119.0, 117.7, 118.8, 1800),
            make_bar(28, 118.8, 119.95, 118.7, 119.9, 2600),
        ]
    )
    channel = _canonical_manual_channel(
        bars,
        channel_id="manual-no-fade",
        showGuides=True,
    )

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        ema_value=115.0,
        canonical_generation=7,
    )
    nearest = context["nearest"]

    assert nearest["label"] == "HIGH"
    assert nearest["touch"] is True
    assert nearest["wick_reject"] is False
    assert nearest["raw_break_score"] >= 70
    assert nearest["primary_path"] in {"break", "bounce"}
    assert nearest["no_fade"] is True
    assert signal_candidates_from_channel_master(context) == []


def test_manual_channel_failed_break_restores_countertrend_candidate() -> None:
    bars = [
        make_bar(
            i,
            114.0 + i * 0.12,
            114.4 + i * 0.12,
            113.8 + i * 0.12,
            114.2 + i * 0.12,
            1000,
        )
        for i in range(26)
    ]
    bars.extend(
        [
            make_bar(26, 117.2, 118.0, 117.0, 117.8, 1400),
            make_bar(27, 117.8, 119.0, 117.7, 118.8, 1800),
            make_bar(28, 118.8, 120.4, 118.7, 119.8, 2600),
        ]
    )
    channel = _canonical_manual_channel(
        bars,
        channel_id="manual-failed-break",
        showGuides=True,
    )

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=4.0,
        ema_value=115.0,
        canonical_generation=7,
    )
    nearest = context["nearest"]
    candidates = signal_candidates_from_channel_master(context)

    assert nearest["raw_break_score"] >= 70
    assert nearest["wick_reject"] is True
    assert nearest["no_fade"] is False
    assert nearest["break_score"] < nearest["raw_break_score"]
    assert candidates
    assert candidates[0].direction == Direction.SHORT


def test_manual_channel_scores_all_configured_ghost_copies() -> None:
    bars = [make_bar(i, 152.0, 152.4, 151.6, 152.1, 1000) for i in range(28)]
    bars.extend(
        [
            make_bar(28, 153.0, 153.8, 152.8, 153.6, 1300),
            make_bar(29, 153.6, 155.1, 153.5, 154.9, 1700),
        ]
    )
    channel = _canonical_manual_channel(
        bars,
        channel_id="manual-ghost-grid",
        offset_price=110.0,
        showGuides=True,
        ghostCopies=5,
    )

    context = manual_channel_context(
        bars,
        [channel],
        atr_value=2.0,
        ema_value=150.0,
        canonical_generation=7,
    )
    channel_context = context["channels"][0]
    nearest = context["nearest"]

    assert channel_context["ghost_copies"] == 5
    assert channel_context["ghost_direction"] == 1
    assert len(channel_context["levels"]) == 25
    assert nearest["level"] == 5.5
    assert nearest["ghost"] is True
    assert nearest["ghost_copy"] == 5
    assert nearest["role"] == "ghost_high"
    assert nearest["near"] is True


def test_impulse_fib_returns_pullback_zone_overlays() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.6 + i * 0.05, 99.9 + i * 0.05, 100.35 + i * 0.05, 1000)
        for i in range(35)
    ]
    bars.append(make_bar(35, 102.0, 106.5, 101.8, 106.2, 7000))

    result = impulse_fib(bars)

    assert result["version"] == "1.1-python"
    assert result["series"]
    assert result["events"]
    assert result["last_event"]
    assert result["latest"]["ts"] == bars[-1].ts.isoformat()
    assert result["latest"]["signal"]["source"] == "impulse_fib"
    assert "lifecycle" in result["latest"]
    box = next(item for item in result["overlays"] if item["type"] == "box")
    assert box["start_ts"] != box["end_ts"]
    assert box["start_ts"] != bars[-1].ts.isoformat()
    fib_lines = [
        item
        for item in result["overlays"]
        if item["type"] == "line" and str(item.get("id", "")).startswith("impulse-fib-")
    ]
    assert {item["fib_level"] for item in fib_lines} == {0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0}
    assert {item["fib_kind"] for item in fib_lines} == {"impulse"}
    assert {item["label"] for item in fib_lines} == {
        "0",
        "0.236",
        "0.382",
        "0.5",
        "0.618",
        "0.786",
        "1",
    }
    assert {item["label_position"] for item in fib_lines} == {"line_right"}
    assert {item["label_side"] for item in fib_lines} == {"below"}
    assert {item["style"] for item in fib_lines} == {"solid", "dashed", "dotted"}
    assert {item["label_font_size"] for item in fib_lines} == {7.0}
    assert {item["label_gap_px"] for item in fib_lines} == {1.0}
    assert {item["direction"] for item in fib_lines} == {"long"}
    assert all("tooltip" not in item for item in fib_lines)
    assert all(item.get("scenario") for item in fib_lines)
    assert all(item.get("trigger_event") for item in fib_lines)
    signal_label = next(
        item
        for item in result["overlays"]
        if item["type"] == "label" and item.get("role") == "impulse_fib"
    )
    assert signal_label["anchor_price_mode"] == "overlay"
    assert "lines" not in signal_label
    assert "bg" not in signal_label
    assert result["last_event"]["fib_levels"]
    assert result["last_event"]["pine_fib_levels"]
    assert result["last_event"]["pb_levels"]
    assert result["last_event"]["label"] == "BRK-L"
    assert result["last_event"]["entry"] == bars[-1].close
    assert result["last_event"]["impulse_scale"] == "weak"
    assert result["last_event"]["impulse_scale_label"] == "IMP-5"
    assert "reason" not in result["last_event"]["signal"]
    assert result["last_event"]["signal"]["reason_code"] == "impulse_continuation"
    assert "primary_scenario_text" not in result["last_event"]
    assert "alternative_scenario_text" not in result["last_event"]
    assert result["last_event"]["entry"] is not None
    assert result["last_event"]["trigger_event"] == {
        "code": "controlled_pullback_or_breakout_retest"
    }
    assert {item["code"] for item in result["last_event"]["evidence"]["supporting"]} >= {
        "range_impulse_confirmed",
        "rvol_confirmed",
        "close_near_impulse_extreme",
    }
    pine_prices = {item["level"]: item["price"] for item in result["last_event"]["pine_fib_levels"]}
    assert pine_prices[0.5] > pine_prices[0.618] > pine_prices[0.786]
    assert result["last_event"]["candle_origin"] == bars[-1].low
    assert result["last_event"]["candle_extreme"] == bars[-1].high
    fib_prices = {item["level"]: item["price"] for item in result["last_event"]["fib_levels"]}
    assert fib_prices[0.0] > fib_prices[0.382] > fib_prices[0.618] > fib_prices[1.0]
    assert result["last_event"]["origin"] == fib_prices[1.0]
    assert result["last_event"]["extreme"] == fib_prices[0.0]
    assert result["params"]["zone_levels"] == [0.382, 0.618]
    assert result["params"]["pb_levels"] == [0.618, 0.786]
    assert box["top"] == fib_prices[0.382]
    assert box["bottom"] == fib_prices[0.618]
    pb_box = next(item for item in result["overlays"] if item["id"].startswith("impulse-pb-zone-"))
    assert pb_box["start_ts"] == bars[-1].ts.isoformat()
    assert pb_box["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert result["params"]["projection_bars"] == 4
    assert result["params"]["fib_bars"] == 8
    assert pb_box["end_bar_offset"] == 4
    assert "end_ts" not in pb_box
    assert pb_box["role"] == "pullback_zone"
    assert pb_box["badge_facts"] == [{"code": "impulse_pullback", "direction": "long"}]
    pb_lines = [
        item for item in result["overlays"] if str(item.get("id", "")).startswith("impulse-pb-0.")
    ]
    assert {item["fib_level"] for item in pb_lines} == {0.618, 0.786}
    assert {item["fib_kind"] for item in pb_lines} == {"pullback"}
    assert {item["label"] for item in pb_lines} == {"0.618", "0.786"}
    assert {item["label_position"] for item in pb_lines} == {"line_right"}
    assert {item["label_side"] for item in pb_lines} == {"below"}
    assert {item["style"] for item in pb_lines} == {"dotted", "dashed"}
    assert {item["width"] for item in pb_lines} == {1.0}
    assert {item["end_bar_offset"] for item in pb_lines} == {8}


def test_impulse_fib_manifest_and_producer_share_short_pullback_defaults() -> None:
    controls = {
        control["key"]: control for control in indicator_manifest()["impulse_fib"]["controls"]
    }

    assert ImpulseFibParams.projection_bars == 4
    assert ImpulseFibParams.fib_bars == 8
    assert controls["projectionBars"]["default"] == 4
    assert controls["fibBars"]["default"] == 8


def test_impulse_fib_separates_projection_age_from_line_length() -> None:
    bars = [
        make_bar(
            i,
            100 + i * 0.05,
            100.6 + i * 0.05,
            99.9 + i * 0.05,
            100.35 + i * 0.05,
            1000,
            timeframe="5m",
            step_minutes=5,
        )
        for i in range(35)
    ]
    bars.append(make_bar(35, 102.0, 106.5, 101.8, 106.2, 7000, timeframe="5m", step_minutes=5))

    result = impulse_fib(bars, params=ImpulseFibParams(fib_bars=5, projection_bars=3))

    assert result["params"]["fib_bars"] == 5
    assert result["params"]["projection_bars"] == 3
    pb_box = next(item for item in result["overlays"] if item["id"].startswith("impulse-pb-zone-"))
    assert pb_box["start_ts"] == bars[-1].ts.isoformat()
    assert pb_box["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert pb_box["end_bar_offset"] == 3
    assert "end_ts" not in pb_box
    pb_line = next(
        item
        for item in result["overlays"]
        if str(item.get("id", "")).startswith("impulse-pb-0.618-")
    )
    assert pb_line["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert pb_line["end_bar_offset"] == 5
    assert "end_ts" not in pb_line


def test_impulse_fib_projection_uses_timeframe_not_previous_timestamp_gap() -> None:
    bars = [
        make_bar(
            i,
            100 + i * 0.05,
            100.6 + i * 0.05,
            99.9 + i * 0.05,
            100.35 + i * 0.05,
            1000,
            timeframe="5m",
            step_minutes=5,
        )
        for i in range(35)
    ]
    gap_start = bars[-1].ts + timedelta(hours=4)
    bars.append(
        make_bar(
            0, 102.0, 106.5, 101.8, 106.2, 7000, timeframe="5m", base_ts=gap_start, step_minutes=5
        )
    )

    result = impulse_fib(bars, params=ImpulseFibParams(fib_bars=10, projection_bars=4))

    pb_box = next(item for item in result["overlays"] if item["id"].startswith("impulse-pb-zone-"))
    assert datetime.fromisoformat(pb_box["start_ts"]) == bars[-1].ts
    assert pb_box["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert pb_box["end_bar_offset"] == 4
    assert "end_ts" not in pb_box
    pb_line = next(
        item
        for item in result["overlays"]
        if str(item.get("id", "")).startswith("impulse-pb-0.618-")
    )
    assert pb_line["end_anchor_ts"] == bars[-1].ts.isoformat()
    assert pb_line["end_bar_offset"] == 10
    assert "end_ts" not in pb_line


def test_impulse_fib_merges_nearby_same_direction_impulses_into_one_leg() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.6 + i * 0.05, 99.9 + i * 0.05, 100.35 + i * 0.05, 1000)
        for i in range(35)
    ]
    bars.append(make_bar(35, 102.0, 106.5, 101.8, 106.2, 7000))
    bars.append(make_bar(36, 106.3, 109.4, 106.0, 109.0, 7600))

    result = impulse_fib(bars)

    assert result["last_event"]
    assert result["last_event"]["leg_merged"] is True
    assert result["last_event"]["impulse_scale"] == "medium"
    assert result["last_event"]["impulse_scale_label"] == "IMP-15"
    assert result["last_event"]["leg_end_ts"] == bars[-1].ts.isoformat()
    assert result["last_event"]["extreme"] == bars[-1].high
    pb_boxes = [
        item for item in result["overlays"] if item.get("id", "").startswith("impulse-pb-zone-")
    ]
    pb_lines = [
        item for item in result["overlays"] if str(item.get("id", "")).startswith("impulse-pb-0.")
    ]
    assert len(pb_boxes) == 1
    assert {item["fib_level"] for item in pb_lines} == {0.618, 0.786}


def test_impulse_fib_materializes_overlays_only_for_retained_legs(
    monkeypatch,
) -> None:
    bars = [
        make_bar(
            index,
            100 + index * 0.05,
            100.6 + index * 0.05,
            99.9 + index * 0.05,
            100.35 + index * 0.05,
            1000,
        )
        for index in range(35)
    ]
    bars.append(make_bar(35, 102.0, 106.5, 101.8, 106.2, 7000))
    for offset in range(1, 10):
        price = 106.0 + offset * 0.55
        bars.append(
            make_bar(
                35 + offset,
                price,
                price + 1.1,
                price - 0.35,
                price + 0.75,
                1300,
            )
        )
    calls = 0
    original = impulse_fib_module._impulse_leg_overlays

    def counting_overlays(spec, params):
        nonlocal calls
        calls += 1
        return original(spec, params)

    monkeypatch.setattr(
        impulse_fib_module,
        "_impulse_leg_overlays",
        counting_overlays,
    )

    result = impulse_fib(bars)

    assert calls == len(result["events"])
    assert calls == 1


def test_impulse_fib_extends_variable_leg_through_small_pullback() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.6 + i * 0.05, 99.9 + i * 0.05, 100.35 + i * 0.05, 1000)
        for i in range(35)
    ]
    bars.append(make_bar(35, 102.0, 106.5, 101.8, 106.2, 7000))
    bars.append(make_bar(36, 106.1, 107.0, 105.8, 106.3, 1200))
    bars.append(make_bar(37, 106.4, 108.2, 106.0, 107.8, 1300))
    bars.append(make_bar(38, 107.9, 109.0, 107.2, 108.6, 1250))
    bars.append(make_bar(39, 108.5, 110.6, 108.1, 110.1, 1200))

    result = impulse_fib(bars)

    event = result["last_event"]
    assert event["leg_bars"] == 5
    assert event["impulse_scale"] == "medium"
    assert event["impulse_scale_label"] == "IMP-15"
    assert event["leg_end_ts"] == bars[-1].ts.isoformat()
    assert event["extreme"] == bars[-1].high
    assert [item["level"] for item in event["pb_levels"]] == [0.236, 0.382, 0.5, 0.618, 0.786]
    pb_boxes = [
        item for item in result["overlays"] if item.get("id", "").startswith("impulse-pb-zone-")
    ]
    assert len(pb_boxes) == 1


def test_impulse_fib_marks_hour_scale_leg_as_strong() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.6 + i * 0.05, 99.9 + i * 0.05, 100.35 + i * 0.05, 1000)
        for i in range(35)
    ]
    bars.append(make_bar(35, 102.0, 106.5, 101.8, 106.2, 7000))
    for offset in range(1, 10):
        price = 106.0 + offset * 0.55
        bars.append(make_bar(35 + offset, price, price + 1.1, price - 0.35, price + 0.75, 1300))

    result = impulse_fib(bars)

    event = result["last_event"]
    assert event["leg_bars"] >= 9
    assert event["impulse_scale"] == "strong"
    assert event["impulse_scale_label"] == "IMP-1H"


def test_trade_setup_engine_mean_reversion_spike_scores_down_move_for_long() -> None:
    bars = [
        make_bar(i, 102.0 - i * 0.03, 102.2 - i * 0.03, 101.8 - i * 0.03, 102.0 - i * 0.03, 1200)
        for i in range(40)
    ]
    bars.append(make_bar(40, 100.8, 101.0, 98.6, 99.0, 6200))
    bars.append(make_bar(41, 99.2, 99.8, 98.9, 99.4, 1500))

    result = trade_setup_engine(bars)

    latest = result["latest"]
    if latest.get("setup_type") == "mean_reversion" and latest.get("direction") == "long":
        zone = latest["zone"]
        assert zone["top"] >= zone["bottom"]
        assert zone["bottom"] <= float(bars[-1].low) + 1e-6


def test_trade_setup_engine_zone_plan_respects_direction() -> None:
    bars = [
        make_bar(
            i, 100 + i * 0.02, 100.5 + i * 0.02, 99.8 + i * 0.02, 100.2 + i * 0.02, 900 + i * 5
        )
        for i in range(48)
    ]
    bars.append(make_bar(48, 101.0, 104.5, 100.8, 104.0, 5200))
    bars.append(make_bar(49, 104.0, 104.8, 103.2, 103.5, 1800))

    result = trade_setup_engine(bars, features={"body_share": 0.42, "close_pos": 0.72})
    latest = result["latest"]
    if latest.get("state") in {"WATCH", "ARMED", "GO"} and latest.get("entry") is not None:
        direction = latest.get("direction")
        entry = float(latest["entry"])
        stop = float(latest["stop"])
        target = float(latest["target"])
        zone = latest["zone"]
        assert zone["top"] >= zone["bottom"]
        if direction == "long":
            assert entry >= zone["bottom"]
            assert stop < entry
            assert target > entry
        elif direction == "short":
            assert entry <= zone["top"]
            assert stop > entry
            assert target < entry


def test_trade_setup_engine_demotes_actionable_state_without_coherent_plan(monkeypatch) -> None:
    import aef_terminal.indicators.modules.trade_setup_engine as trade_setup_module
    from aef_terminal.indicators.modules.trade_setup_engine import (
        TradeSetupEngineParams,
        _ActiveSetup,
        _plan_from_setup,
    )

    bar = make_bar(1, 100.0, 106.0, 99.0, 105.0, 1200)
    setup = _ActiveSetup(
        setup_type="momentum_breakout",
        direction=Direction.LONG,
        state="ARMED",
        zone_top=101.0,
        zone_bottom=100.0,
        zone_start_ts=bar.ts.isoformat(),
        score=88.0,
        reason_code="test_incoherent_armed_plan",
        metrics={},
    )
    monkeypatch.setattr(
        trade_setup_module,
        "normalize_trade_plan",
        lambda *_args, **_kwargs: {"coherent": False},
    )

    _plan_from_setup(setup, bar, atr=1.0, params=TradeSetupEngineParams())

    assert setup.state == "WATCH"
    assert setup.entry is None
    assert setup.stop is None
    assert setup.target is None


def test_trade_setup_engine_market_spotlight_gate_blocks_blind_countertrend_momentum() -> None:
    from aef_terminal.indicators.modules.trade_setup_engine import _market_context_gate

    market_context = {
        "direction": "long",
        "side_gate": {"veto_short": True},
        "flow_state": {
            "dominant_direction": "long",
            "countertrend_direction": "short",
            "pullback_allowed": False,
        },
    }

    gate = _market_context_gate(Direction.SHORT, "momentum_breakout", market_context)

    assert gate["blocked"] is True
    assert gate["reason_code"] == "market_spotlight_veto_short"


def test_trade_setup_engine_market_spotlight_gate_allows_confirmed_pullback_reversion() -> None:
    from aef_terminal.indicators.modules.trade_setup_engine import _market_context_gate

    market_context = {
        "direction": "long",
        "side_gate": {"veto_short": True},
        "flow_state": {
            "dominant_direction": "long",
            "countertrend_direction": "short",
            "pullback_allowed": True,
        },
    }

    gate = _market_context_gate(Direction.SHORT, "mean_reversion", market_context)

    assert gate["blocked"] is False
    assert gate["reason_code"] == "market_spotlight_pullback_allowed"
    assert gate["score_adjust"] < 0


def test_trade_setup_engine_ema_context_is_relative_to_setup_direction() -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    countertrend = tse._trend_context(
        Direction.SHORT,
        close=105.0,
        ema_fast=104.0,
        ema_slow=103.0,
    )
    aligned = tse._trend_context(
        Direction.SHORT,
        close=99.0,
        ema_fast=100.0,
        ema_slow=101.0,
    )
    pullback = tse._trend_context(
        Direction.SHORT,
        close=100.5,
        ema_fast=100.0,
        ema_slow=101.0,
    )

    assert countertrend["trend_direction"] == "long"
    assert countertrend["alignment"] == "countertrend"
    assert aligned["trend_direction"] == "short"
    assert aligned["alignment"] == "aligned"
    assert pullback["trend_direction"] == "short"
    assert pullback["alignment"] == "pullback"


def test_trade_setup_engine_countertrend_momentum_uses_one_blocked_action_contract() -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bar = make_bar(0, 104.0, 106.0, 103.0, 105.0, 1000)
    params = tse.TradeSetupEngineParams()
    setup = tse._ActiveSetup(
        setup_type="momentum_breakout",
        direction=Direction.SHORT,
        state="ARMED",
        zone_top=105.5,
        zone_bottom=104.5,
        zone_start_ts=bar.ts.isoformat(),
        score=72.0,
        reason_code="setup_momentum_breakdown",
        metrics={},
        entry=104.5,
        stop=106.0,
        target=101.5,
        rr=2.0,
    )
    trend_context = tse._trend_context(
        setup.direction,
        close=bar.close,
        ema_fast=104.0,
        ema_slow=103.0,
    )
    action_decision = tse._setup_action_decision(
        setup,
        params,
        trend_context=trend_context,
        market_gate={},
        vsa_gate={},
    )
    overlay = tse._build_zone_overlay(
        setup,
        start_ts=setup.zone_start_ts,
        end_anchor=bar,
        end_bar_offset=18,
        params=params,
        action_decision=action_decision,
        trend_context=trend_context,
        vsa_gate={},
        bar=bar,
        atr=1.0,
        ema_fast=104.0,
        ema_slow=103.0,
    )

    assert action_decision == {
        "action": "BLOCK",
        "raw_action": "ARM",
        "reason_code": "ema_trend_countertrend",
        "blocked_reason": "ema_trend_countertrend",
    }
    assert overlay["action"] == "BLOCK"
    assert overlay["raw_action"] == "ARM"
    assert overlay["blocked_reason"] == "ema_trend_countertrend"
    assert overlay["trade_plan"]["actionable"] is False
    assert overlay["trade_plan"]["blocked_reason"] == "ema_trend_countertrend"
    assert overlay["action_card"]["phase"] == "BLOCK"
    assert overlay["action_card"]["blocked"] is True
    assert overlay["direction"] == "short"
    assert overlay["badge_facts"][0]["direction"] == "short"
    assert overlay["badge_facts"][0]["action"] == "block"
    assert overlay["badge_facts"][1]["trend_direction"] == "long"
    assert overlay["badge_facts"][1]["alignment"] == "countertrend"
    assert overlay["metrics"]["action"] == "BLOCK"
    assert overlay["metrics"]["raw_action"] == "ARM"
    assert overlay["metrics"]["trend_direction"] == "long"
    assert overlay["metrics"]["trend_alignment"] == "countertrend"


def test_trade_setup_engine_pullback_downgrades_armed_momentum_to_watch() -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    params = tse.TradeSetupEngineParams()
    setup = tse._ActiveSetup(
        setup_type="momentum_breakout",
        direction=Direction.SHORT,
        state="ARMED",
        zone_top=101.0,
        zone_bottom=100.0,
        zone_start_ts="2026-01-01T00:00:00+00:00",
        score=72.0,
        reason_code="setup_momentum_breakdown",
        metrics={},
    )
    decision = tse._setup_action_decision(
        setup,
        params,
        trend_context=tse._trend_context(
            Direction.SHORT,
            close=100.5,
            ema_fast=100.0,
            ema_slow=101.0,
        ),
        market_gate={},
        vsa_gate={},
    )

    assert decision == {
        "action": "WATCH",
        "raw_action": "ARM",
        "reason_code": "ema_trend_pullback",
        "blocked_reason": "",
    }


def test_trade_setup_engine_countertrend_momentum_cannot_promote_to_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000),
        make_bar(1, 100.0, 101.0, 99.0, 100.0, 1000),
    ]
    candidate = tse._SetupCandidate(
        setup_type="momentum_breakout",
        direction=Direction.SHORT,
        score=90.0,
        zone_top=101.0,
        zone_bottom=99.5,
        reason_code="setup_momentum_breakdown",
        metrics={},
    )
    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidate if index == 0 else None,
    )
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: True)
    monkeypatch.setattr(
        tse,
        "_trend_context",
        lambda *_args, **_kwargs: {
            "basis": "ema_fast_slow",
            "trend_direction": "long",
            "alignment": "countertrend",
            "price_relation": "above_fast",
            "score": 20.0,
        },
    )

    result = tse.trade_setup_engine(bars)

    assert result["latest"]["state"] == "ARMED"
    assert result["latest"]["signal"]["action"] == "BLOCK"
    assert result["latest"]["signal"]["raw_action"] == "ARM"
    assert result["latest"]["signal"]["blocked_reason"] == "ema_trend_countertrend"
    zone = next(item for item in result["overlays"] if item.get("role") == "setup_zone")
    assert zone["action"] == "BLOCK"
    assert zone["badge_facts"][0]["action"] == "block"
    assert zone["badge_facts"][1]["alignment"] == "countertrend"


@pytest.mark.parametrize(
    ("go_confirmed", "max_setup_bars", "expected_state"),
    ((False, 48, "ARMED"), (True, 1, "GO")),
)
def test_trade_setup_engine_freezes_committed_plan_and_identity(
    monkeypatch: pytest.MonkeyPatch,
    go_confirmed: bool,
    max_setup_bars: int,
    expected_state: str,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [make_bar(index, 100.0, 101.0, 99.0, 100.0, 1000) for index in range(4)]
    first = tse._SetupCandidate(
        setup_type="mean_reversion",
        direction=Direction.LONG,
        score=90.0,
        zone_top=100.0,
        zone_bottom=99.5,
        reason_code="first_setup",
        metrics={},
    )
    stronger = tse._SetupCandidate(
        setup_type="mean_reversion",
        direction=Direction.SHORT,
        score=99.0,
        zone_top=100.5,
        zone_bottom=100.0,
        reason_code="stronger_setup",
        metrics={},
    )
    plan_calls: list[tuple[str, str]] = []

    def fake_plan(setup, bar, _atr, _params) -> None:
        plan_calls.append((setup.reason_code, bar.ts.isoformat()))
        setup.entry = 100.0 + len(plan_calls)
        setup.stop = 50.0
        setup.target = 150.0
        setup.target_2r = 200.0
        setup.rr = 1.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: first if index == 0 else stronger,
    )
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 0.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: go_confirmed)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)

    result = tse.trade_setup_engine(
        bars,
        params=tse.TradeSetupEngineParams(max_setup_bars=max_setup_bars),
    )

    assert result["latest"]["state"] == expected_state
    assert result["latest"]["reason_code"] == "first_setup"
    assert result["latest"]["entry"] == 102.0
    assert [reason for reason, _ts in plan_calls] == ["first_setup", "first_setup"]
    assert [event["event"] for event in result["events"]] == ["WATCH", expected_state]
    assert all(event["reason_code"] == "first_setup" for event in result["events"])


def test_trade_setup_engine_supersedes_only_uncommitted_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [make_bar(index, 100.0, 101.0, 99.0, 100.0, 1000) for index in range(2)]
    candidates = (
        tse._SetupCandidate(
            "mean_reversion",
            Direction.LONG,
            70.0,
            105.0,
            104.0,
            "initial_watch",
            {},
        ),
        tse._SetupCandidate(
            "mean_reversion",
            Direction.SHORT,
            80.0,
            96.0,
            95.0,
            "replacement_watch",
            {},
        ),
    )

    def fake_plan(setup, _bar, _atr, _params) -> None:
        setup.entry = setup.zone_top if setup.direction == Direction.LONG else setup.zone_bottom
        setup.stop = 90.0 if setup.direction == Direction.LONG else 110.0
        setup.target = 110.0 if setup.direction == Direction.LONG else 90.0
        setup.target_2r = setup.target
        setup.rr = 1.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidates[index],
    )
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 100.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: False)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)

    result = tse.trade_setup_engine(bars)

    assert result["latest"]["state"] == "WATCH"
    assert result["latest"]["reason_code"] == "replacement_watch"
    assert [event["event"] for event in result["events"]] == [
        "WATCH",
        "SUPERSEDED",
        "WATCH",
    ]
    assert result["events"][1]["reason_code"] == "initial_watch"
    assert result["events"][1]["action_reason_code"] == "setup_superseded"


def test_trade_setup_engine_emits_expiry_before_releasing_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [make_bar(index, 100.0, 101.0, 99.0, 100.0, 1000) for index in range(2)]
    candidate = tse._SetupCandidate(
        "mean_reversion",
        Direction.LONG,
        70.0,
        105.0,
        104.0,
        "expiring_watch",
        {},
    )

    def fake_plan(setup, _bar, _atr, _params) -> None:
        setup.entry = 105.0
        setup.stop = 90.0
        setup.target = 110.0
        setup.target_2r = 120.0
        setup.rr = 1.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidate if index == 0 else None,
    )
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)

    result = tse.trade_setup_engine(
        bars,
        params=tse.TradeSetupEngineParams(max_setup_bars=0),
    )

    assert result["latest"]["state"] == "IDLE"
    assert [event["event"] for event in result["events"]] == ["WATCH", "EXPIRED"]
    assert result["events"][-1]["action_reason_code"] == "setup_expired"


def test_trade_setup_engine_does_not_rearm_on_the_expiry_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [make_bar(index, 100.0, 101.0, 99.0, 100.0, 1000) for index in range(2)]
    candidate = tse._SetupCandidate(
        "mean_reversion",
        Direction.LONG,
        70.0,
        105.0,
        104.0,
        "persistent_watch",
        {},
    )

    monkeypatch.setattr(tse, "_best_candidate", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(tse, "_plan_from_setup", lambda *_args, **_kwargs: None)

    result = tse.trade_setup_engine(
        bars,
        params=tse.TradeSetupEngineParams(max_setup_bars=0),
    )

    assert result["latest"]["state"] == "IDLE"
    assert [event["event"] for event in result["events"]] == ["WATCH", "EXPIRED"]


def test_trade_setup_engine_defers_terminal_checks_until_after_the_go_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [make_bar(index, 100.0, 102.0, 99.0, 101.0, 1000) for index in range(3)]
    candidate = tse._SetupCandidate(
        "mean_reversion",
        Direction.LONG,
        90.0,
        101.0,
        100.0,
        "persistent_go",
        {},
    )

    def fake_plan(setup, *_args, **_kwargs) -> None:
        setup.entry = 100.0
        setup.stop = 90.0
        setup.target = 101.5
        setup.target_2r = 103.0
        setup.rr = 1.0

    monkeypatch.setattr(tse, "_best_candidate", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 0.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: True)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)

    result = tse.trade_setup_engine(bars)

    assert result["series"][1]["state"] == "GO"
    assert result["latest"]["state"] == "IDLE"
    assert [event["event"] for event in result["events"]] == [
        "WATCH",
        "GO",
        "TP",
    ]


@pytest.mark.parametrize(
    ("terminal_bar", "expected_event", "expected_reason"),
    (
        (
            make_bar(2, 100.0, 105.0, 97.0, 100.0, 1000),
            "TP/SL",
            "setup_target_and_stop_hit",
        ),
        (
            make_bar(2, 97.0, 97.5, 96.0, 97.0, 1000),
            "STOP",
            "setup_stop_gap_through",
        ),
    ),
)
def test_trade_setup_engine_and_shared_lifecycle_use_one_terminal_fact(
    monkeypatch: pytest.MonkeyPatch,
    terminal_bar: Bar,
    expected_event: str,
    expected_reason: str,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000),
        make_bar(1, 100.0, 101.0, 99.0, 100.0, 1000),
        terminal_bar,
    ]
    candidate = tse._SetupCandidate(
        "mean_reversion",
        Direction.LONG,
        90.0,
        100.0,
        99.5,
        "canonical_terminal",
        {},
    )

    def fake_plan(setup, *_args, **_kwargs) -> None:
        setup.entry = 100.0
        setup.stop = 98.0
        setup.target = 104.0
        setup.target_2r = 108.0
        setup.rr = 2.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidate if index == 0 else None,
    )
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 0.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: True)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)

    result = tse.trade_setup_engine(bars)

    assert result["latest"]["state"] == "IDLE"
    assert result["events"][-1]["event"] == expected_event
    assert result["events"][-1]["action_reason_code"] == expected_reason
    assert result["latest"]["lifecycle"]["state"] == expected_event
    assert result["latest"]["lifecycle"]["exit_price"] is None


def test_trade_setup_engine_and_shared_lifecycle_share_carried_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000),
        make_bar(1, 100.0, 101.0, 99.0, 100.0, 1000),
        make_bar(2, 100.0, 103.5, 99.5, 103.0, 1000),
        make_bar(3, 103.0, 103.5, 101.5, 102.0, 1000),
    ]
    candidate = tse._SetupCandidate(
        "mean_reversion",
        Direction.LONG,
        90.0,
        100.0,
        99.5,
        "canonical_trail",
        {},
    )

    def fake_plan(setup, *_args, **_kwargs) -> None:
        setup.entry = 100.0
        setup.stop = 98.0
        setup.target = 110.0
        setup.target_2r = 120.0
        setup.rr = 5.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidate if index == 0 else None,
    )
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 0.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: True)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)
    monkeypatch.setattr(tse.pine, "atr_rma_series", lambda *_args, **_kwargs: [1.0] * 4)

    result = tse.trade_setup_engine(bars)

    assert result["latest"]["state"] == "IDLE"
    assert result["events"][-1]["event"] == "TRAIL"
    assert result["events"][-1]["action_reason_code"] == "setup_trail_stop_hit"
    assert result["latest"]["lifecycle"]["state"] == "TRAIL"
    assert result["latest"]["lifecycle"]["exit_price"] == 102.0


def test_trade_setup_engine_applies_latest_market_veto_before_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [
        make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000),
        make_bar(1, 100.0, 101.0, 99.0, 100.0, 1000),
    ]
    candidate = tse._SetupCandidate(
        "momentum_breakout",
        Direction.LONG,
        90.0,
        100.0,
        99.5,
        "market_veto",
        {},
    )

    def fake_plan(setup, *_args, **_kwargs) -> None:
        setup.entry = 100.0
        setup.stop = 98.0
        setup.target = 104.0
        setup.target_2r = 108.0
        setup.rr = 2.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidate if index == 0 else None,
    )
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 0.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: True)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)
    monkeypatch.setattr(
        tse,
        "_trend_context",
        lambda *_args, **_kwargs: {
            "basis": "ema_fast_slow",
            "trend_direction": "long",
            "alignment": "aligned",
            "price_relation": "above_fast",
            "score": 80.0,
        },
    )

    result = tse.trade_setup_engine(
        bars,
        indicator_bundle={
            "market_spotlight": {
                "market_context": {
                    "direction": "short",
                    "side_gate": {"veto_long": True},
                    "flow_state": {
                        "dominant_direction": "short",
                        "countertrend_direction": "long",
                        "pullback_allowed": False,
                    },
                }
            }
        },
    )

    assert result["latest"]["state"] == "ARMED"
    assert result["latest"]["signal"]["action"] == "BLOCK"
    assert result["latest"]["lifecycle"] is None
    assert "GO" not in [event["event"] for event in result["events"]]


def test_trade_setup_engine_market_veto_does_not_hide_filled_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    bars = [
        make_bar(0, 105.0, 106.0, 104.0, 105.0, 1000),
        make_bar(1, 105.0, 106.0, 104.0, 105.0, 1000),
        make_bar(2, 101.0, 102.0, 99.0, 101.0, 1000),
    ]
    candidate = tse._SetupCandidate(
        "mean_reversion",
        Direction.LONG,
        72.0,
        100.0,
        99.5,
        "filled_before_veto",
        {},
    )

    def fake_plan(setup, *_args, **_kwargs) -> None:
        setup.entry = 100.0
        setup.stop = 98.0
        setup.target = 104.0
        setup.target_2r = 108.0
        setup.rr = 2.0

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        lambda _bars, index, **_kwargs: candidate if index == 0 else None,
    )
    monkeypatch.setattr(tse, "_distance_to_zone", lambda *_args: 0.0)
    monkeypatch.setattr(tse, "_breakout_go_confirmed", lambda *_args: False)
    monkeypatch.setattr(tse, "_plan_from_setup", fake_plan)

    result = tse.trade_setup_engine(
        bars,
        indicator_bundle={
            "market_spotlight": {
                "market_context": {
                    "direction": "short",
                    "side_gate": {"veto_long": True},
                    "flow_state": {
                        "dominant_direction": "short",
                        "countertrend_direction": "long",
                        "pullback_allowed": False,
                    },
                }
            }
        },
    )

    assert result["latest"]["signal"]["action"] == "ARM"
    assert result["latest"]["lifecycle"]["state"] == "FOLLOW"
    assert result["latest"]["lifecycle"]["filled"] is True


def test_trade_setup_engine_momentum_codes_use_bo_for_long_and_bd_for_short() -> None:
    from aef_terminal.domain import Direction
    from aef_terminal.indicators.modules import trade_setup_engine as tse

    assert tse._momentum_code(Direction.LONG) == "BO"
    assert tse._momentum_code(Direction.SHORT) == "BD"
    assert not hasattr(tse, "_momentum_kind_label")
    assert not hasattr(tse, "_momentum_watch_label")


def test_trade_setup_engine_resolves_breakdown_signal_name_for_short() -> None:
    from aef_terminal.indicators.registry import resolve_indicator_signal_name

    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_BO", "GO")
        == "setup_momentum_breakout"
    )
    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_BD", "ARM")
        == "setup_momentum_breakdown"
    )
    assert (
        resolve_indicator_signal_name("trade_setup_engine", "SETUP_BD_WATCH", "WATCH")
        == "setup_momentum_breakdown"
    )


def test_trade_setup_engine_returns_setup_contract() -> None:
    bars = [
        make_bar(
            i, 100 + i * 0.02, 100.5 + i * 0.02, 99.8 + i * 0.02, 100.2 + i * 0.02, 900 + i * 5
        )
        for i in range(48)
    ]
    bars.append(make_bar(48, 101.0, 104.5, 100.8, 104.0, 5200))
    bars.append(make_bar(49, 104.0, 104.8, 103.2, 103.5, 1800))

    result = trade_setup_engine(bars, features={"body_share": 0.42, "close_pos": 0.72})

    assert result["version"] == "1.2-python-confirmed-1m-preview"
    assert result["series"]
    assert result["latest"]["signal"]["source"] == "trade_setup_engine"
    assert result["latest"]["state"] in {"IDLE", "WATCH", "ARMED", "GO", "STOP", "TP"}
    assert result["latest"]["setup_type"] in {"", "momentum_breakout", "mean_reversion"}
    zone = result["latest"].get("zone")
    if result["latest"]["state"] != "IDLE":
        assert isinstance(zone, dict)
        assert zone["top"] >= zone["bottom"]
        zone_overlay = next(
            (item for item in result["overlays"] if item.get("role") == "setup_zone"), None
        )
        assert zone_overlay is not None
        assert zone_overlay.get("trade_plan")
        assert "tooltip" not in zone_overlay
        assert zone_overlay.get("trigger_event")
        assert zone_overlay.get("metrics", {}).get("mode")
        assert zone_overlay.get("metrics", {}).get("mode_code") in {"MR", "BO", "BD"}
        assert zone_overlay.get("metrics", {}).get("action")
        assert zone_overlay.get("badge_facts")
        assert len(zone_overlay["badge_facts"]) == 2
        assert zone_overlay["badge_facts"][0]["code"] == "setup_zone"
        assert zone_overlay["badge_facts"][0]["mode"] in {"mr", "bo", "bd"}
        assert zone_overlay["badge_facts"][0]["action"] in {"watch", "arm", "go", "blk"}
        assert zone_overlay["badge_facts"][1]["code"] == "trend_context"
        assert zone_overlay["badge_facts"][1]["trend_direction"] in {"long", "short", "flat"}
        assert zone_overlay["badge_facts"][1]["alignment"] in {
            "aligned",
            "pullback",
            "countertrend",
            "neutral",
        }
        assert zone_overlay["action"] == result["latest"]["signal"]["action"]
        assert zone_overlay["raw_action"] == result["latest"]["signal"]["raw_action"]
        assert zone_overlay["blocked_reason"] == result["latest"]["signal"]["blocked_reason"]
        assert zone_overlay["tone"] in {"positive", "negative"}
        assert "bg" not in zone_overlay
        assert "border" not in zone_overlay
        assert "style" not in zone_overlay
        assert zone_overlay.get("label") in {None, ""}
        assert zone_overlay.get("action_card")
        glyph_overlays = [
            item for item in result["overlays"] if item.get("role") in {"entry", "target"}
        ]
        assert any(item.get("glyph_only") for item in glyph_overlays)
        assert all("\n" not in str(item.get("tooltip") or "") for item in glyph_overlays)
        assert all(item.get("source") == "trade_setup_engine" for item in glyph_overlays)
        assert all(item.get("evidence", {}).get("supporting") for item in glyph_overlays)
        assert all("context_lines" not in item for item in glyph_overlays)
        assert all("lines" not in item for item in glyph_overlays)
    latest = result["latest"]
    if latest.get("setup_type") == "momentum_breakout" and latest.get("state") != "IDLE":
        code_prefix = "BO" if latest.get("direction") == "long" else "BD"
        assert latest["metrics"]["mode_code"] == code_prefix
        assert latest["scenario"] == "momentum_breakout"
        assert latest["signal"]["code"].startswith(f"SETUP_{code_prefix}")
        assert latest["reason_code"] in {"setup_momentum_breakout", "setup_momentum_breakdown"}
        assert ":" not in latest["reason_code"]


def test_trade_setup_engine_idle_latest_uses_typed_tooltip_contract() -> None:
    bars = [make_bar(i, 100.0, 100.2, 99.8, 100.0, 900) for i in range(24)]

    result = trade_setup_engine(bars)

    assert result["latest"]["state"] == "IDLE"
    assert result["latest"]["scenario"] == "trade_setup_engine"
    assert result["latest"]["setup"] == "runtime_state"
    assert result["latest"]["trigger_event"] == {
        "code": "setup_runtime_state",
        "state": "IDLE",
        "action": "WAIT",
    }
    assert result["latest"]["metrics"]["state"] == "IDLE"
    assert result["latest"]["metrics"]["action"] == "WAIT"
    assert result["latest"]["signal"]["reason"] == "no_active_setup"
    assert "no_active_setup" not in result["latest"]["trigger_event"]["code"]


def test_impulse_fib_short_grid_runs_from_impulse_high_to_low() -> None:
    bars = [
        make_bar(i, 106 - i * 0.05, 106.2 - i * 0.05, 105.4 - i * 0.05, 105.7 - i * 0.05, 1000)
        for i in range(35)
    ]
    bars.append(make_bar(36, 104.0, 104.2, 99.5, 99.8, 7000))

    result = impulse_fib(bars)

    assert result["last_event"]
    event = result["last_event"]
    assert event["direction"] == "short"
    assert event["impulse_start_ts"] < event["impulse_end_ts"]
    fib_prices = {item["level"]: item["price"] for item in event["fib_levels"]}
    assert fib_prices[0.0] < fib_prices[0.382] < fib_prices[0.618] < fib_prices[1.0]
    assert event["origin"] == fib_prices[1.0]
    assert event["extreme"] == fib_prices[0.0]
    pb_prices = {item["level"]: item["price"] for item in event["pb_levels"]}
    assert pb_prices[0.618] < pb_prices[0.786]
    box = next(item for item in result["overlays"] if item["type"] == "box")
    assert box["bottom"] == fib_prices[0.382]
    assert box["top"] == fib_prices[0.618]
    fib_lines = [
        item
        for item in result["overlays"]
        if item["type"] == "line" and str(item.get("id", "")).startswith("impulse-fib-")
    ]
    assert {item["fib_kind"] for item in fib_lines} == {"impulse"}
    assert {item["fib_level"] for item in fib_lines} == {0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0}
    assert {item["label_position"] for item in fib_lines} == {"line_right"}
    assert {item["label_side"] for item in fib_lines} == {"above"}
    assert {item["style"] for item in fib_lines} == {"solid", "dashed", "dotted"}
    assert {item["direction"] for item in fib_lines} == {"short"}


def _bear_flag_continuation_bars() -> list[Bar]:
    bars = [
        make_bar(i, 108 - i * 0.04, 108.3 - i * 0.04, 107.5 - i * 0.04, 107.8 - i * 0.04, 1000)
        for i in range(34)
    ]
    bars.append(make_bar(34, 106.5, 106.7, 100.2, 100.6, 9000))
    for offset in range(6):
        low = 100.3 + offset * 0.30
        high = low + 1.10
        bars.append(make_bar(35 + offset, (high + low) / 2.0, high, low, (high + low) / 2.0, 1200))
    bars.append(make_bar(41, 102.4, 102.8, 101.9, 102.0, 1250))
    return bars


def _bull_flag_continuation_bars() -> list[Bar]:
    bars = [
        make_bar(i, 92 + i * 0.04, 92.5 + i * 0.04, 91.7 + i * 0.04, 92.2 + i * 0.04)
        for i in range(34)
    ]
    bars.append(make_bar(34, 93.5, 100.8, 93.3, 100.4, 9000))
    for offset in range(6):
        high = 100.5 - offset * 0.30
        low = high - 1.70
        bars.append(make_bar(35 + offset, (high + low) / 2.0, high, low, (high + low) / 2.0, 1200))
    bars.append(make_bar(41, 98.2, 98.9, 97.1, 98.8, 1250))
    return bars


def test_impulse_fib_detects_bear_flag_continuation_pattern() -> None:
    bars = _bear_flag_continuation_bars()

    result = impulse_fib(bars)
    pattern = result["continuation_pattern"]

    assert pattern["active"] is True
    assert pattern["type"] == "bear_flag"
    assert pattern["direction"] == "short"
    assert pattern["phase"] in {"watch", "arm", "triggered"}
    assert pattern["break_level"] < pattern["invalidation"]
    assert pattern["target_1"] < pattern["break_level"]
    assert pattern["structure_kind"] == "flag"
    assert pattern["upper_boundary"]["slope_atr_per_bar"] > 0.0
    assert pattern["lower_boundary"]["slope_atr_per_bar"] > 0.0
    assert pattern["classification_locked_at_bars"] == 6
    assert pattern["pattern_bars"] == 7
    assert result["latest"]["continuation_pattern"] == pattern
    continuation_overlays = [
        item for item in result["overlays"] if item.get("role") == "continuation_pattern"
    ]
    assert len(continuation_overlays) == 2
    assert {item["type"] for item in continuation_overlays} == {"line"}
    assert {item["geometry_segment"] for item in continuation_overlays} == {
        "upper_boundary",
        "lower_boundary",
    }
    assert {item["pattern"] for item in continuation_overlays} == {"flag"}
    assert {item["control_key"] for item in continuation_overlays} == {"patterns"}
    assert {item["retention"] for item in continuation_overlays} == {"active"}
    assert not any(
        item.get("type") == "box" and item.get("role") == "continuation_pattern"
        for item in result["overlays"]
    )
    by_segment = {item["geometry_segment"]: item for item in continuation_overlays}
    assert by_segment["upper_boundary"]["level_kind"] == "invalidation"
    assert by_segment["upper_boundary"]["label"] == "FAIL"
    assert by_segment["lower_boundary"]["level_kind"] == "break"
    assert by_segment["lower_boundary"]["label"] == "BREAK"
    assert by_segment["upper_boundary"]["y1"] > by_segment["lower_boundary"]["y1"]
    assert by_segment["upper_boundary"]["y2"] > by_segment["lower_boundary"]["y2"]
    assert all(
        item.get("retention") == "history"
        for item in result["overlays"]
        if item.get("role") != "continuation_pattern"
    )


def test_impulse_fib_draws_bull_flag_break_on_upper_boundary() -> None:
    bars = _bull_flag_continuation_bars()

    result = impulse_fib(bars)
    pattern = result["continuation_pattern"]
    continuation_overlays = {
        item["geometry_segment"]: item
        for item in result["overlays"]
        if item.get("role") == "continuation_pattern"
    }

    assert pattern["active"] is True
    assert pattern["type"] == "bull_flag"
    assert pattern["structure_kind"] == "flag"
    assert pattern["upper_boundary"]["slope_atr_per_bar"] < 0.0
    assert pattern["lower_boundary"]["slope_atr_per_bar"] < 0.0
    assert continuation_overlays["upper_boundary"]["level_kind"] == "break"
    assert continuation_overlays["upper_boundary"]["label"] == "BREAK"
    assert continuation_overlays["lower_boundary"]["level_kind"] == "invalidation"
    assert continuation_overlays["lower_boundary"]["label"] == "FAIL"
    assert (
        continuation_overlays["upper_boundary"]["y2"]
        < continuation_overlays["upper_boundary"]["y1"]
    )
    assert (
        continuation_overlays["lower_boundary"]["y2"]
        < continuation_overlays["lower_boundary"]["y1"]
    )


def test_impulse_continuation_geometry_draws_wedge_without_crossing_projection() -> None:
    highs = (110.0, 109.5, 109.0, 108.5, 108.0)
    lows = (100.0, 100.8, 101.6, 102.4, 103.2)
    window = [
        make_bar(index, (high + low) / 2.0, high, low, (high + low) / 2.0)
        for index, (high, low) in enumerate(zip(highs, lows, strict=True))
    ]

    geometry = continuation_geometry(
        window,
        latest_atr=1.0,
        projection_bars=4,
        direction="long",
    )

    assert geometry["classification_code"] == "bull_pennant"
    assert geometry["structure_kind"] == "pennant"
    assert geometry["boundary_convergence"] > 0.0
    assert 0 <= geometry["boundary_projection_bars"] < 4
    assert (
        geometry["upper_boundary"]["slope_atr_per_bar"]
        < geometry["lower_boundary"]["slope_atr_per_bar"]
    )
    assert geometry["upper_boundary"]["start_price"] > geometry["lower_boundary"]["start_price"]
    assert geometry["upper_boundary"]["end_price"] > geometry["lower_boundary"]["end_price"]


def _classified_continuation_pattern(
    *,
    direction: str,
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    latest_high: float,
    latest_low: float,
    latest_close: float,
) -> dict[str, object]:
    event_bar = make_bar(0, 95.0, 100.0, 90.0, 99.0, 9000)
    bars = [event_bar]
    bars.extend(
        make_bar(
            index,
            (high + low) / 2.0,
            high,
            low,
            (high + low) / 2.0,
            1200,
        )
        for index, (high, low) in enumerate(zip(highs, lows, strict=True), start=1)
    )
    bars.append(
        make_bar(
            len(bars),
            latest_close,
            latest_high,
            latest_low,
            latest_close,
            1200,
        )
    )
    event = {
        "ts": event_bar.ts.isoformat(),
        "direction": direction,
        "origin": 90.0 if direction == "long" else 110.0,
        "extreme": 100.0,
    }
    return continuation_pattern(
        bars=bars,
        latest_event=event,
        atr_values=[1.0] * len(bars),
        projection_bars=8,
    )


@pytest.mark.parametrize(
    ("direction", "highs", "lows", "latest", "expected_type", "expected_kind"),
    (
        (
            "long",
            (99.5, 99.25, 99.0, 98.75, 98.5, 98.25),
            (96.0, 95.92, 95.84, 95.76, 95.68, 95.6),
            (98.1, 95.7, 97.2),
            "bull_falling_wedge",
            "wedge",
        ),
        (
            "short",
            (104.0, 104.08, 104.16, 104.24, 104.32, 104.4),
            (101.0, 101.25, 101.5, 101.75, 102.0, 102.25),
            (104.3, 102.1, 103.2),
            "bear_rising_wedge",
            "wedge",
        ),
        (
            "long",
            (99.5, 99.25, 99.0, 98.75, 98.5, 98.25),
            (96.0, 96.15, 96.3, 96.45, 96.6, 96.75),
            (98.1, 96.7, 97.4),
            "bull_pennant",
            "pennant",
        ),
        (
            "long",
            (97.0, 97.2, 97.4, 97.6, 97.8, 98.0),
            (95.0, 95.2, 95.4, 95.6, 95.8, 96.0),
            (98.1, 95.9, 97.2),
            "bull_runaway_channel",
            "runaway_channel",
        ),
    ),
)
def test_impulse_continuation_classifies_typed_pattern_geometry(
    direction: str,
    highs: tuple[float, ...],
    lows: tuple[float, ...],
    latest: tuple[float, float, float],
    expected_type: str,
    expected_kind: str,
) -> None:
    pattern = _classified_continuation_pattern(
        direction=direction,
        highs=highs,
        lows=lows,
        latest_high=latest[0],
        latest_low=latest[1],
        latest_close=latest[2],
    )

    assert pattern["active"] is True
    assert pattern["type"] == expected_type
    assert pattern["classification_code"] == expected_type
    assert pattern["structure_kind"] == expected_kind
    assert pattern["classification_locked_at_bars"] == 6
    assert pattern["upper_boundary"]["touches"] >= 2
    assert pattern["lower_boundary"]["touches"] >= 2


def test_impulse_continuation_rejects_wrong_direction_wedge() -> None:
    pattern = _classified_continuation_pattern(
        direction="long",
        highs=(97.0, 97.1, 97.2, 97.3, 97.4, 97.5),
        lows=(94.0, 94.25, 94.5, 94.75, 95.0, 95.25),
        latest_high=97.4,
        latest_low=95.2,
        latest_close=96.2,
    )

    assert pattern["active"] is False
    assert pattern["state"] == "rejected"
    assert pattern["reason_code"] == "continuation_wrong_direction_wedge"


def test_impulse_continuation_waits_for_two_stable_prefixes() -> None:
    event_bar = make_bar(0, 95.0, 100.0, 90.0, 99.0, 9000)
    bars = [event_bar]
    for index in range(1, 7):
        low = 96.0 - index * 0.2
        bars.append(make_bar(index, low + 1.0, low + 2.0, low, low + 1.0, 1200))

    pattern = continuation_pattern(
        bars=bars,
        latest_event={
            "ts": event_bar.ts.isoformat(),
            "direction": "long",
            "origin": 90.0,
            "extreme": 100.0,
        },
        atr_values=[1.0] * len(bars),
        projection_bars=8,
    )

    assert pattern == {
        "active": False,
        "state": "forming",
        "reason_code": "continuation_wait_stable_geometry",
        "direction": "long",
        "phase": "forming",
        "pattern_bars": 6,
    }


def test_impulse_fib_continuation_pattern_is_declared_in_runtime_contract() -> None:
    bars = _bear_flag_continuation_bars()
    result = impulse_fib(bars)

    contracted = attach_indicator_status(
        "impulse_fib",
        result,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
    )

    assert contracted["status"]["state_code"] != "error"
    assert contracted["continuation_pattern"]["active"] is True


def test_market_spotlight_consumes_impulse_flag_and_exposes_reentry_context() -> None:
    bars = _bear_flag_continuation_bars()
    impulse = impulse_fib(bars)

    spotlight = market_spotlight(bars, indicator_context={"impulse_fib": impulse})
    context = spotlight["market_context"]

    assert context["continuation_pattern"]["type"] == "bear_flag"
    assert context["state"] == "continuation_pattern"
    assert context["state_direction"] == "short"
    assert context["pullback_reentry"]["active"] is True
    assert context["pullback_reentry"]["direction"] == "long"
    assert context["strategy"]["pattern_type"] == "bear_flag"
    assert context["strategy"]["structure_kind"] == "flag"
    assert spotlight["latest"]["continuation_pattern"]["type"] == "bear_flag"
    assert spotlight["table"]["model_ref"] == "market_spotlight"
    assert "columns" not in spotlight["table"]


def test_market_spotlight_runs_without_impulse_optional_context() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.8 + i * 0.05, 100.2 + i * 0.05, 1000)
        for i in range(45)
    ]

    spotlight = market_spotlight(bars)

    assert spotlight["market_context"]["continuation_pattern"]["active"] is False
    assert spotlight["market_context"]["pullback_reentry"]["active"] is False
    assert spotlight["latest"]["continuation_pattern"]["active"] is False
    assert spotlight["latest"]["pullback_reentry"]["active"] is False


def test_impulse_fib_latest_uses_current_bar_not_stale_event() -> None:
    bars = [
        make_bar(i, 100 + i * 0.05, 100.6 + i * 0.05, 99.9 + i * 0.05, 100.35 + i * 0.05, 1000)
        for i in range(35)
    ]
    bars.append(make_bar(36, 102.0, 106.5, 101.8, 106.2, 7000))
    for i in range(37, 43):
        bars.append(make_bar(i, 106.0, 106.3, 105.6, 105.9, 1400))

    result = impulse_fib(bars)

    assert result["last_event"]
    assert result["latest"]["ts"] == bars[-1].ts.isoformat()
    assert result["latest"]["signal"]["action"] == "WAIT"
    assert result["last_event"]["signal"]["raw_action"] == "WATCH_LOW_RR"
    assert result["latest"]["lifecycle"] is None
    assert result["warnings"]
    assert any(
        item["event_type"] in {"developing_impulse", "confirmed_impulse"}
        for item in result["warnings"]
    )
    assert any(
        marker in result["warnings"][-1]["trigger_event"]["code"]
        for marker in ("not_an_entry", "not_an_immediate")
    )


def test_strategy_mode_prioritizes_countertrend_candidates() -> None:
    fade = SignalCandidate(
        "sfp",
        Direction.SHORT,
        64.0,
        100.0,
        "liquidity sweep",
        DomainFact("liquidity_sweep"),
        kind=ScenarioKind.FADE,
        finality=CandidateFinality.CONFIRMED,
    )
    transit = SignalCandidate(
        "impulse_pullback",
        Direction.LONG,
        68.0,
        101.0,
        "impulse continuation",
        DomainFact("impulse_continuation"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    bars = [make_bar(0, 100.5, 102.0, 99.0, 100.8, 1000)]

    balanced = choose_decision(
        [fade, transit], bars=bars, atr_value=1.0, strategy_mode=StrategyMode.BALANCED
    )
    mean_reversion = choose_decision(
        [fade, transit], bars=bars, atr_value=1.0, strategy_mode=StrategyMode.MEAN_REVERSION
    )

    assert balanced.direction == Direction.LONG
    assert balanced.source == "impulse_pullback"
    assert mean_reversion.direction == Direction.SHORT
    assert mean_reversion.source == "sfp"


def test_decision_boundary_rejects_non_authoritative_candidate_finality() -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000)]
    confirmed = SignalCandidate(
        "confirmed",
        Direction.LONG,
        64.0,
        100.0,
        "confirmed setup",
        DomainFact("confirmed_setup"),
        details={"trigger": 100.0, "stop": 98.0, "target": 104.0},
        kind=ScenarioKind.TRANSIT,
        source="confirmed",
        finality=CandidateFinality.CONFIRMED,
    )

    for finality in (
        CandidateFinality.PROVISIONAL,
        CandidateFinality.RETAINED,
        CandidateFinality.EXPIRED,
    ):
        display_only = SignalCandidate(
            "display_only",
            Direction.SHORT,
            99.0,
            100.0,
            "display-only setup",
            DomainFact("display_only_setup"),
            details={"trigger": 100.0, "stop": 102.0, "target": 96.0},
            kind=ScenarioKind.FADE,
            source="display_only",
            finality=finality,
        )
        decision = choose_decision([display_only, confirmed], bars=bars, atr_value=1.0)

        assert decision.source == "confirmed"
        assert decision.direction is Direction.LONG

    blocked = choose_decision([display_only], bars=bars, atr_value=1.0)
    assert blocked.action is ActionPhase.WAIT
    assert blocked.reason_codes == ("no_candidate",)


def test_projected_candidate_confidence_is_discounted() -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.2, 1000)]
    candidate = SignalCandidate(
        "projected_channel",
        Direction.LONG,
        80.0,
        100.2,
        "projected level",
        DomainFact("projected_level"),
        details={"is_ghost": True},
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    decision = choose_decision([candidate], bars=bars, atr_value=1.0)

    assert decision.confidence == pytest.approx(73.6)
    assert decision.action is ActionPhase.ARM


@pytest.mark.parametrize(
    ("producer_phase", "expected_action"),
    (
        (ActionPhase.CANDIDATE, ActionPhase.WATCH),
        (ActionPhase.WATCH, ActionPhase.WATCH),
        (ActionPhase.ARM, ActionPhase.ARM),
        (ActionPhase.GO, ActionPhase.GO),
        (None, ActionPhase.GO),
    ),
)
def test_decision_respects_typed_producer_phase_ceiling(
    producer_phase: ActionPhase | None,
    expected_action: ActionPhase,
) -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000)]
    candidate = SignalCandidate(
        "typed_phase",
        Direction.LONG,
        90.0,
        100.0,
        "typed producer phase",
        DomainFact("typed_producer_phase"),
        details={"trigger": 100.0, "stop": 98.0, "target": 104.0},
        kind=ScenarioKind.TRANSIT,
        producer_phase=producer_phase,
        finality=CandidateFinality.CONFIRMED,
    )

    decision = choose_decision([candidate], bars=bars, atr_value=1.0)

    assert decision.action is expected_action
    assert decision.metrics["score_action"] == "GO"
    assert decision.metrics["producer_phase"] == (
        producer_phase.value if producer_phase is not None else None
    )
    assert ("producer_phase_ceiling" in decision.reason_codes) is (
        expected_action is not ActionPhase.GO
    )


def test_indicator_watch_cannot_become_go_during_candidate_promotion() -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.0, 1000)]
    indicator = {
        "latest": {
            "signal": {
                "action": "WATCH",
                "direction": "long",
                "kind": "transit",
                "score": 90.0,
                "trigger": 100.0,
                "stop": 98.0,
                "target": 104.0,
                "trigger_event": {"code": "producer_watch"},
            }
        }
    }

    candidates = signal_candidates_from_indicator_state(
        indicator,
        source="trade_setup_engine",
        score_floor=60.0,
    )
    decision = choose_decision(candidates, bars=bars, atr_value=1.0)

    assert candidates[0].producer_phase is ActionPhase.WATCH
    assert decision.action is ActionPhase.WATCH
    assert "producer_phase_ceiling" in decision.reason_codes


def test_indicator_score_weight_adjusts_candidate_ranking_with_bounds() -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.2, 1000)]
    raw_leader = SignalCandidate(
        "raw_leader",
        Direction.SHORT,
        78.0,
        100.2,
        "higher raw score",
        DomainFact("higher_raw_score"),
        details={"indicator_score_weight": 0.5},
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    reliable = SignalCandidate(
        "reliable",
        Direction.LONG,
        72.0,
        100.1,
        "better calibrated",
        DomainFact("better_calibrated"),
        details={"indicator_score_weight": 2.0},
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    decision = choose_decision([raw_leader, reliable], bars=bars, atr_value=1.0)

    assert decision.direction == Direction.LONG
    assert decision.source == "reliable"
    assert decision.confidence == 90.0


def test_signal_candidate_kind_overrides_name_based_strategy_classification() -> None:
    bars = [make_bar(0, 100.0, 101.0, 99.0, 100.2, 1000)]
    fade = SignalCandidate(
        "new_linda_port_code",
        Direction.SHORT,
        72.0,
        100.2,
        "semantic fade",
        DomainFact("semantic_fade"),
        kind=ScenarioKind.FADE,
        finality=CandidateFinality.CONFIRMED,
    )
    transit = SignalCandidate(
        "sfp",
        Direction.LONG,
        72.0,
        100.1,
        "semantic transit",
        DomainFact("semantic_transit"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    fade_decision = choose_decision([fade], bars=bars, atr_value=1.0)
    transit_decision = choose_decision([transit], bars=bars, atr_value=1.0)

    assert fade_decision.kind == ScenarioKind.FADE
    assert transit_decision.kind == ScenarioKind.TRANSIT
    assert (
        choose_decision(
            [fade, transit], bars=bars, atr_value=1.0, strategy_mode=StrategyMode.MEAN_REVERSION
        ).direction
        == Direction.SHORT
    )
    assert (
        choose_decision(
            [fade, transit], bars=bars, atr_value=1.0, strategy_mode=StrategyMode.BREAKOUT
        ).direction
        == Direction.LONG
    )


def test_indicator_state_signal_kind_promotes_to_candidate() -> None:
    indicator = {
        "latest": {
            "price": 100.0,
            "signal": {
                "action": "GO",
                "direction": "short",
                "kind": "fade",
                "score": 74,
                "trigger": 100.0,
                "stop": 101.0,
                "target": 98.0,
                "reason": "custom semantic fade",
                "trigger_event": {"code": "custom_semantic_fade"},
            },
        }
    }

    candidates = signal_candidates_from_indicator_state(
        indicator, source="custom_source", score_floor=50
    )

    assert candidates
    assert candidates[0].kind == ScenarioKind.FADE
    assert candidates[0].name == "custom_source"


def test_vsa_fuel_sfp_cooldown_blocks_opposing_sfp_only() -> None:
    bars = [
        make_bar(i, 100 + i * 0.03, 100.8 + i * 0.03, 99.8 + i * 0.03, 100.4 + i * 0.03)
        for i in range(12)
    ]
    short_sfp = SignalCandidate(
        "sfp",
        Direction.SHORT,
        64.0,
        101.0,
        "upper sweep",
        DomainFact("upper_sweep"),
        kind=ScenarioKind.FADE,
        source="sfp",
        role="liquidity_sweep",
        finality=CandidateFinality.CONFIRMED,
    )
    long_sfp = SignalCandidate(
        "sfp",
        Direction.LONG,
        63.0,
        100.0,
        "lower sweep",
        DomainFact("lower_sweep"),
        kind=ScenarioKind.FADE,
        source="sfp",
        role="liquidity_sweep",
        finality=CandidateFinality.CONFIRMED,
    )
    impulse = SignalCandidate(
        "impulse_pullback",
        Direction.SHORT,
        66.0,
        101.2,
        "trend continuation",
        DomainFact("trend_continuation"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    vsa_context = {
        "events": [
            {
                "ts": bars[-2].ts.isoformat(),
                "code": "FUEL_UP",
                "direction": "long",
                "fuel": True,
            }
        ]
    }

    filtered, cooldown = apply_vsa_fuel_sfp_cooldown(
        [short_sfp, long_sfp, impulse],
        bars,
        vsa_context=vsa_context,
        cooldown_bars=3,
    )

    assert short_sfp not in filtered
    assert long_sfp in filtered
    assert impulse in filtered
    assert cooldown is not None
    assert cooldown["blocked_direction"] == "short"
    assert cooldown["reason"].startswith("VSA FUEL_UP")


def test_obvious_failure_spotlight_support_uses_structured_market_context() -> None:
    assert _spotlight_support(market_context=None, evidence=[]) == 0.0
    assert _spotlight_support(market_context={}, evidence=[]) == 0.0
    with pytest.raises(ValueError, match="finite flow_bias"):
        _spotlight_support(market_context={"direction": "flat"}, evidence=[])

    evidence: list[dict[str, object]] = []
    score = _spotlight_support(
        market_context={
            "direction": "flat",
            "flow_bias": 0,
            "flow_conflict": True,
            "side_lock": "CHECK",
            "flow": "breakout trend impulse",
            "setup": "breakout trend impulse",
            "entry": {"phase": "blocked"},
            "strategy": {"phase": "wait"},
        },
        evidence=evidence,
    )

    assert score == 6.0
    assert evidence == [
        {
            "kind": "support",
            "source": "market_spotlight",
            "code": "range_fade_context",
            "score_delta": 6.0,
        }
    ]

    evidence = []
    score = _spotlight_support(
        market_context={
            "direction": "long",
            "flow_bias": 4,
            "flow_conflict": False,
            "side_lock": "LONG",
            "entry": {"phase": "ready"},
            "strategy": {"phase": "armed"},
        },
        evidence=evidence,
    )

    assert score == -5.0
    assert evidence == [
        {
            "kind": "warning",
            "source": "market_spotlight",
            "code": "continuation_context",
            "score_delta": -5.0,
        }
    ]


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
@pytest.mark.parametrize("explicit_invalidation", [False, True])
def test_choose_decision_preserves_invalidation_separately_from_execution_stop(
    direction, explicit_invalidation
) -> None:
    stop = 98.0 if direction is Direction.LONG else 102.0
    invalidation = 95.0 if direction is Direction.LONG else 105.0
    details = {
        "trigger": 100.0,
        "stop": stop,
        "target": 104.0 if direction is Direction.LONG else 96.0,
    }
    if explicit_invalidation:
        details["invalid"] = invalidation
    candidate = SignalCandidate(
        "structural_plan",
        direction,
        84.0,
        100.0,
        "structural_plan",
        DomainFact("structural_plan"),
        details=details,
        kind=ScenarioKind.FADE,
        finality=CandidateFinality.CONFIRMED,
    )
    bars = [make_bar(0, 100.0, 110.0, 90.0, 100.0), make_bar(1, 100.0, 101.0, 99.0, 100.0)]
    decision = choose_decision([candidate], bars=bars, atr_value=1.0)

    assert decision.action is ActionPhase.GO
    assert decision.stop == stop
    if explicit_invalidation:
        assert decision.invalidation == invalidation
    elif direction is Direction.LONG:
        assert decision.invalidation < bars[0].low
    else:
        assert decision.invalidation > bars[0].high


def test_choose_decision_filters_go_transit_when_option_rvol_is_weak() -> None:
    candidate = SignalCandidate(
        "breakout_accumulation",
        Direction.LONG,
        84.0,
        102.0,
        "breakout",
        DomainFact("breakout"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    bars = [make_bar(0, 101.0, 103.0, 100.5, 102.0, 1000)]

    decision = choose_decision(
        [candidate],
        bars=bars,
        atr_value=1.0,
        option_flow={"option_rvol": 1.2, "call_rate_per_minute": 20.0, "put_rate_per_minute": 4.0},
    )

    assert decision.kind == ScenarioKind.WAIT
    assert decision.action is ActionPhase.WAIT
    assert "option_rvol_below_minimum" in decision.reason_codes
    assert decision.direction is Direction.FLAT
    assert decision.confidence == 0.0
    assert (decision.trigger, decision.stop, decision.target, decision.invalidation) == (
        None,
        None,
        None,
        None,
    )
    assert decision.metrics["option_rvol"] == 1.2


def test_choose_decision_allows_go_transit_when_option_flow_confirms() -> None:
    candidate = SignalCandidate(
        "breakout_accumulation",
        Direction.LONG,
        84.0,
        102.0,
        "breakout",
        DomainFact("breakout"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    bars = [make_bar(0, 101.0, 103.0, 100.5, 102.0, 1000)]

    decision = choose_decision(
        [candidate],
        bars=bars,
        atr_value=1.0,
        option_flow={"option_rvol": 2.1, "call_rate_per_minute": 20.0, "put_rate_per_minute": 4.0},
    )

    assert decision.kind == ScenarioKind.TRANSIT
    assert decision.action is ActionPhase.GO


def test_choose_decision_uses_option_rates_instead_of_raw_deltas() -> None:
    candidate = SignalCandidate(
        "breakout_accumulation",
        Direction.LONG,
        84.0,
        102.0,
        "breakout",
        DomainFact("breakout"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    bars = [make_bar(0, 101.0, 103.0, 100.5, 102.0, 1000)]

    decision = choose_decision(
        [candidate],
        bars=bars,
        atr_value=1.0,
        option_flow={
            "option_rvol": 2.1,
            "call_volume_delta": 1000.0,
            "put_volume_delta": 1.0,
            "call_rate_per_minute": 2.0,
            "put_rate_per_minute": 20.0,
        },
    )

    assert decision.kind == ScenarioKind.WAIT
    assert decision.action is ActionPhase.WAIT
    assert "option_opposite_rate_dominant" in decision.reason_codes
    assert decision.direction is Direction.FLAT
    assert (decision.trigger, decision.stop, decision.target, decision.invalidation) == (
        None,
        None,
        None,
        None,
    )


def test_choose_decision_fails_closed_on_untyped_option_rates() -> None:
    candidate = SignalCandidate(
        "breakout_accumulation",
        Direction.LONG,
        84.0,
        102.0,
        "breakout",
        DomainFact("breakout"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    bars = [make_bar(0, 101.0, 103.0, 100.5, 102.0, 1000)]

    decision = choose_decision(
        [candidate],
        bars=bars,
        atr_value=1.0,
        option_flow={"option_rvol": 2.1, "call_rate_per_minute": "20", "put_rate_per_minute": 4.0},
    )

    assert decision.kind == ScenarioKind.WAIT
    assert decision.action is ActionPhase.WAIT
    assert "option_flow_invalid" in decision.reason_codes
    assert decision.direction is Direction.FLAT
    assert decision.trigger is None


def test_breakout_accumulation_emits_shared_signal_and_lifecycle() -> None:
    bars = [make_bar(i, 100.0, 100.45, 99.75, 100.05, 1200) for i in range(45)]
    bars.append(make_bar(45, 100.1, 101.4, 99.95, 101.2, 4200))

    result = breakout_accumulation(bars)

    assert result["version"] == "16-python-confirmed-1m-preview"
    assert result["latest"]["signal"]["source"] == "breakout_accumulation"
    assert "lifecycle" in result["latest"]
    if result["events"]:
        assert result["events"][-1]["signal"]["source"] == "breakout_accumulation"


def test_obvious_failure_detects_failed_upper_breakout_as_fade_short() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.1, 1000) for i in range(30)]
    bars.append(make_bar(30, 100.8, 102.2, 100.4, 101.7, 2600))
    bars.append(make_bar(31, 101.5, 101.6, 99.8, 100.3, 2800))

    result = obvious_failure(
        bars,
        params=ObviousFailureParams(lookback=24, min_score=60),
        indicator_bundle={},
    )
    manifest = indicator_manifest()["obvious_failure"]
    validate_indicator_result_contract(
        attach_indicator_status(
            "obvious_failure", result, input_bars=bars, analysis_bar=bars[-1], mode="test"
        ),
        name="obvious_failure",
        renderer_contract=manifest["renderer_contract"],
        runtime_payload_contract=manifest["runtime_payload_contract"],
    )

    latest = result["latest"]
    assert latest["code"] == "OBV_FAIL_UPPER"
    assert latest["direction"] == "short"
    assert latest["signal"]["kind"] == ScenarioKind.FADE.value
    assert latest["signal"]["plan_coherent"] is True
    assert result["signals"][0]["code"] == "OBV_FAIL_UPPER"
    assert result["signals"][0]["action"] == "GO"
    assert result["signals"][0]["direction"] == "short"
    assert result["signals"][0]["scenario"] == "obvious_failure"
    assert (
        result["signals"][0]["trigger_event"]["code"]
        == "upper_boundary_breakout_failed_close_back_inside"
    )
    assert result["signals"][0]["details"]["hypothesis"]["evidence"] == []
    assert result["signals"][0]["risk"]["code"] == "fakeout_extreme_invalidation"
    assert "tooltip" not in result["signals"][0]
    assert "reason" not in result["signals"][0]
    assert result["signals"][0]["reason_code"] == "obv_fail_upper_confirmed"
    assert result["signals"][0]["metrics"]["attempt_code"] == "upper_boundary_breakout_failed"
    assert result["signals"][0]["metrics"]["boundary"]["value"] == 101.0
    assert result["signals"][0]["fact_groups"][0]["kind"] == "failure_evidence"
    assert result["signals"][0]["fact_groups"][0]["items"] == []
    assert any(item.get("role") == "obvious_failure_zone" for item in result["overlays"])
    assert all(item.get("retention") == "active" for item in result["overlays"])
    assert result["overlays"][0]["tone"] == "info"
    assert "bg" not in result["overlays"][0]
    assert "border" not in result["overlays"][0]
    assert result["overlays"][0]["trigger_event"] == result["signals"][0]["trigger_event"]
    assert "tooltip" not in result["overlays"][0]
    label = next(item for item in result["overlays"] if item.get("role") == "obvious_failure_label")
    assert label["code"] == "OBV_FAIL_UPPER"
    assert "label" not in label


def test_obvious_failure_detects_first_reclaim_after_multi_bar_fakeout() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.1, 1000) for i in range(30)]
    probe = make_bar(30, 100.8, 102.2, 100.4, 101.7, 2600)
    reclaim = make_bar(32, 101.2, 101.6, 99.8, 100.3, 2800)
    bars.extend(
        [
            probe,
            make_bar(31, 101.7, 102.0, 101.1, 101.4, 1800),
            reclaim,
        ]
    )

    result = obvious_failure(
        bars,
        params=ObviousFailureParams(lookback=24, min_score=60),
        indicator_bundle={},
    )

    latest = result["latest"]
    assert latest["code"] == "OBV_FAIL_UPPER"
    assert latest["ts"] == reclaim.ts.isoformat()
    assert latest["zone"]["start_ts"] == probe.ts.isoformat()
    assert result["signals"][0]["ts"] == reclaim.ts.isoformat()


def test_obvious_failure_detects_failed_lower_breakdown_as_fade_long() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 99.9, 1000) for i in range(30)]
    bars.append(make_bar(30, 99.2, 99.6, 97.8, 98.3, 2600))
    bars.append(make_bar(31, 98.5, 100.2, 98.4, 99.7, 2800))

    result = obvious_failure(
        bars,
        params=ObviousFailureParams(lookback=24, min_score=60),
        indicator_bundle={},
    )

    latest = result["latest"]
    assert latest["code"] == "OBV_FAIL_LOWER"
    assert latest["direction"] == "long"
    assert latest["hypothesis"]["failure_code"] == "close_back_inside"
    assert latest["signal"]["plan_coherent"] is True
    assert (
        result["signals"][0]["trigger_event"]["code"]
        == "lower_boundary_breakdown_failed_close_back_inside"
    )
    assert result["signals"][0]["metrics"]["attempt_code"] == "lower_boundary_breakdown_failed"
    assert result["signals"][0]["fact_groups"][0]["items"] == []


def test_obvious_failure_keeps_historical_events_without_promoting_stale_latest() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.1, 1000) for i in range(30)]
    bars.append(make_bar(30, 100.8, 102.2, 100.4, 101.7, 2600))
    bars.append(make_bar(31, 101.5, 101.6, 99.8, 100.3, 2800))
    bars.extend(make_bar(i, 100.1, 100.8, 99.6, 100.2, 1000) for i in range(32, 45))

    result = obvious_failure(
        bars,
        params=ObviousFailureParams(lookback=24, min_score=60),
        indicator_bundle={},
    )

    assert result["latest"]["state"] == "WAIT"
    assert result["signals"] == []
    assert result["events"]
    assert result["events"][-1]["code"] == "OBV_FAIL_UPPER"
    assert result["series"][-1]["code"] == "OBV_FAIL_UPPER"
    assert any(item.get("role") == "obvious_failure_zone" for item in result["overlays"])


def test_integrated_indicator_signals_publish_coherent_trade_plans() -> None:
    impulse_bars = [
        make_bar(i, 100 + i * 0.05, 100.6 + i * 0.05, 99.9 + i * 0.05, 100.35 + i * 0.05, 1000)
        for i in range(35)
    ]
    impulse_bars.append(make_bar(36, 102.0, 106.5, 101.8, 106.2, 7000))
    linda_bars = [
        make_bar(i, 100 + i * 0.05, 100.7 + i * 0.05, 99.8 + i * 0.05, 100.45 + i * 0.05, 1000)
        for i in range(45)
    ]
    linda_bars.append(make_bar(46, 102.4, 106.4, 102.1, 106.0, 9000))
    w5_bars = []
    price = 100.0
    for index in range(65):
        drift = 0.35 if index < 58 else 0.55
        open_ = price
        close = price + drift
        w5_bars.append(make_bar(index, open_, close + 0.35, open_ - 0.15, close, 900 + index * 4))
        price = close
    w5_bars.append(make_bar(66, price, price + 2.6, price - 0.25, price + 0.25, 5200))
    smc_prices = [
        100,
        101,
        102,
        101,
        100,
        99,
        100,
        102,
        104,
        103,
        102,
        101,
        99,
        98,
        100,
        103,
        106,
        105,
    ]
    smc_bars = [
        make_bar(
            index, price, price + 0.7, price - 0.7, price + (0.25 if index % 2 == 0 else -0.2), 1100
        )
        for index, price in enumerate(smc_prices)
    ]

    results = [
        breakout_accumulation(linda_bars),
        linda_volume(linda_bars, params=LindaVolumeParams(indian_min_score=55)),
        impulse_fib(impulse_bars),
        smc_channels(smc_bars),
        w5_structure([], w5_bars),
        wolfe_structure(smc_bars, pivot_len=2),
    ]

    checked = 0
    for result in results:
        for signal in _iter_signals(result):
            _assert_trade_plan_coherent(signal)
            checked += 1
    assert checked >= len(results)


def test_shared_signal_lifecycle_tracks_fill_and_target() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 100, 100.4, 99.4, 99.8, 1000),
        make_bar(2, 100.1, 102.4, 99.9, 102.0, 1400),
    ]
    signals = [
        SignalState(
            source="test",
            action=ActionPhase.ARM,
            raw_action="ARM",
            direction=Direction.LONG,
            score=72,
            trigger=100,
            stop=99,
            target=102,
        ),
        SignalState(source="test", action=ActionPhase.WAIT, direction=Direction.FLAT, score=0),
        SignalState(source="test", action=ActionPhase.WAIT, direction=Direction.FLAT, score=0),
    ]

    lifecycle = lifecycle_from_signals(source="test", bars=bars, signals=signals)

    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.TARGET_HIT
    assert lifecycle.filled
    assert lifecycle.exit_price == 102


def test_shared_signal_lifecycle_does_not_fill_arm_on_creation_bar() -> None:
    bar = make_bar(0, 100, 101, 99, 100, 1000)
    signal = SignalState(
        source="test",
        action=ActionPhase.ARM,
        raw_action="ARM",
        direction=Direction.LONG,
        score=72,
        trigger=100,
        stop=98,
        target=104,
    )

    lifecycle = lifecycle_from_signals(
        source="test",
        bars=[bar],
        signals=[signal],
    )

    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.WAIT_ENTRY
    assert lifecycle.filled is False
    assert lifecycle.filled_ts is None


def test_shared_signal_lifecycle_defers_terminals_on_arm_fill_bar() -> None:
    bars = [
        make_bar(0, 101, 102, 100.5, 101, 1000),
        make_bar(1, 101, 105, 97, 104, 1000),
    ]
    signals = [
        SignalState(
            source="test",
            action=ActionPhase.ARM,
            raw_action="ARM",
            direction=Direction.LONG,
            score=72,
            trigger=100,
            stop=98,
            target=104,
        ),
        SignalState(source="test", action=ActionPhase.WAIT, direction=Direction.FLAT, score=0),
    ]

    lifecycle = lifecycle_from_signals(
        source="test",
        bars=bars,
        signals=signals,
    )

    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.FOLLOW
    assert lifecycle.filled is True
    assert lifecycle.filled_ts == bars[1].ts.isoformat()


def test_shared_signal_lifecycle_uses_carried_trail_before_next_trail() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 100, 103.5, 99.5, 103, 1000),
    ]
    signals = [
        SignalState(
            source="test",
            action=ActionPhase.GO,
            raw_action="GO",
            direction=Direction.LONG,
            score=82,
            trigger=100,
            stop=98,
            target=110,
        ),
        SignalState(source="test", action=ActionPhase.WAIT, direction=Direction.FLAT, score=0),
    ]

    lifecycle = lifecycle_from_signals(
        source="test",
        bars=bars,
        signals=signals,
        atr_values=[1.0, 1.0],
        trail_atr=1.1,
    )

    assert lifecycle is not None
    assert lifecycle.state is LifecycleState.FOLLOW
    assert lifecycle.trail == pytest.approx(101.9)
    assert lifecycle.closed_ts is None


def test_shared_signal_lifecycle_preserves_zero_stop() -> None:
    bars = [
        make_bar(0, 1, 1.5, 0.5, 1, 1000),
        make_bar(1, 1, 2.5, 0, 2, 1000),
    ]
    signals = [
        SignalState(
            source="test",
            action=ActionPhase.GO,
            raw_action="GO",
            direction=Direction.LONG,
            score=72,
            trigger=1,
            stop=0,
            invalidation=2,
            target=2,
        ),
        SignalState(source="test", action=ActionPhase.WAIT, direction=Direction.FLAT, score=0),
    ]

    lifecycle = lifecycle_from_signals(source="test", bars=bars, signals=signals)

    assert lifecycle is not None
    assert lifecycle.stop == 0
    assert lifecycle.state is LifecycleState.TARGET_AND_STOP_HIT


def test_signal_state_keeps_plan_quality_in_canonical_trade_plan() -> None:
    signal = SignalState(
        source="test",
        action=ActionPhase.GO,
        direction=Direction.SHORT,
        score=81,
        trigger=100,
        stop=102,
        target=96,
    ).as_dict()

    assert signal["plan_complete"] is True
    assert signal["plan_coherent"] is True
    assert "risk" not in signal
    assert "reward" not in signal
    assert "rr" not in signal
    assert signal["trade_plan"]["risk"] == 2
    assert signal["trade_plan"]["reward"] == 4
    assert signal["trade_plan"]["rr"] == 2
    assert signal["trade_plan"]["actionable"] is True


def test_shared_signal_lifecycle_rejects_incoherent_plan_geometry() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 100, 101, 99, 100, 1000),
    ]
    signals = [
        SignalState(
            source="test",
            action=ActionPhase.ARM,
            raw_action="ARM",
            direction=Direction.LONG,
            score=72,
            trigger=100,
            stop=104,
            target=96,
        ),
        SignalState(source="test", action=ActionPhase.WAIT, direction=Direction.FLAT, score=0),
    ]

    assert lifecycle_from_signals(source="test", bars=bars, signals=signals) is None


def test_indicator_state_rejects_incoherent_signal_plan() -> None:
    indicator = {
        "latest": {
            "signal": {
                "action": "GO",
                "direction": "long",
                "score": 90,
                "trigger": 4548,
                "stop": 4561,
                "target": 4508,
                "code": "ABS_LOW",
            }
        }
    }

    assert (
        signal_candidates_from_indicator_state(indicator, source="absorption_trap", score_floor=62)
        == []
    )


def test_indicator_state_rejects_incomplete_signal_plan() -> None:
    indicator = {
        "latest": {
            "signal": {
                "action": "GO",
                "direction": "long",
                "score": 90,
                "trigger": 4548,
                "stop": 4540,
                "target": None,
                "code": "ABS_LOW",
            }
        }
    }

    assert (
        signal_candidates_from_indicator_state(indicator, source="absorption_trap", score_floor=62)
        == []
    )


def test_decision_invalidates_short_plan_when_price_is_through_stop() -> None:
    bars = [make_bar(i, 100.0, 101.0, 99.0, 100.0, 1200) for i in range(12)]
    candidate = SignalCandidate(
        "unit_transit",
        Direction.SHORT,
        84.0,
        100.0,
        "short transit",
        DomainFact("short_transit"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    base = choose_decision([candidate], bars=bars, atr_value=2.0)
    assert base.direction == Direction.SHORT
    assert base.stop is not None
    breached = list(bars)
    breached[-1] = make_bar(
        12, base.stop - 0.5, base.stop + 0.75, base.stop - 1.0, base.stop + 0.25, 1500
    )
    invalidated = choose_decision([candidate], bars=breached, atr_value=2.0)
    assert invalidated.direction == Direction.FLAT
    assert invalidated.action is ActionPhase.WAIT
    assert invalidated.stop is None
    assert "plan_stop_breached" in invalidated.reason_codes


def test_decision_ignores_pre_signal_wick_on_originating_bar() -> None:
    bar = make_bar(0, 100.0, 105.0, 99.0, 100.0, 1200)
    current_candidate = SignalCandidate(
        "unit_transit",
        Direction.SHORT,
        84.0,
        100.0,
        "short transit",
        DomainFact("short_transit"),
        details={
            "trigger": 100.0,
            "stop": 102.0,
            "target": 96.0,
            "ts": bar.ts.isoformat(),
        },
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    current = choose_decision([current_candidate], bars=[bar], atr_value=2.0)

    assert current.direction is Direction.SHORT
    assert current.action is ActionPhase.GO
    assert "plan_stop_breached" not in current.reason_codes

    prior_candidate = SignalCandidate(
        "unit_transit",
        Direction.SHORT,
        84.0,
        100.0,
        "short transit",
        DomainFact("short_transit"),
        details={
            **dict(current_candidate.details),
            "ts": (bar.ts - timedelta(minutes=5)).isoformat(),
        },
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )
    prior = choose_decision([prior_candidate], bars=[bar], atr_value=2.0)

    assert prior.direction is Direction.FLAT
    assert "plan_stop_breached" in prior.reason_codes


def test_decision_rejects_candidate_when_plan_cannot_be_completed() -> None:
    decision = choose_decision(
        [
            SignalCandidate(
                "unit",
                Direction.LONG,
                90,
                100,
                "missing risk plan",
                DomainFact("missing_risk_plan"),
                details={"trigger": 100, "stop": 99, "target": None},
                kind=ScenarioKind.TRANSIT,
                finality=CandidateFinality.CONFIRMED,
            )
        ],
        bars=None,
        atr_value=None,
    )

    assert decision.direction == Direction.FLAT
    assert decision.action is ActionPhase.WAIT
    assert "incomplete_or_incoherent_trade_plan" in decision.reason_codes


def test_global_min_rr_gate_blocks_low_rr_executable_candidate() -> None:
    weak = SignalCandidate(
        "unit",
        Direction.LONG,
        88.0,
        100.0,
        "weak rr",
        DomainFact("weak_rr"),
        {
            "trigger": 100.0,
            "stop": 98.0,
            "target": 101.0,
            "rr": 0.5,
            "plan_complete": True,
            "plan_coherent": True,
        },
        finality=CandidateFinality.CONFIRMED,
    )
    strong = SignalCandidate(
        "unit",
        Direction.LONG,
        80.0,
        100.0,
        "strong rr",
        DomainFact("strong_rr"),
        {
            "trigger": 100.0,
            "stop": 98.0,
            "target": 103.0,
            "rr": 1.5,
            "plan_complete": True,
            "plan_coherent": True,
        },
        finality=CandidateFinality.CONFIRMED,
    )

    executable, annotated, gate = apply_global_min_rr_gate([weak, strong], 1.25)

    assert [item.name for item in executable] == ["unit"]
    assert executable[0].reason == "strong rr"
    assert executable[0].details["rr_gate_status"] == "passed"
    assert annotated[0].details["blocked_by_min_rr"] is True
    assert annotated[0].details["rr_gate_status"] == "blocked"
    assert annotated[1].details["rr_gate_status"] == "passed"
    assert gate["blocked"][0]["rr"] == 0.5


def test_global_min_rr_gate_rejects_low_rr_fallback_decision() -> None:
    decision = ScenarioDecision(
        kind=ScenarioKind.TRANSIT,
        direction=Direction.LONG,
        confidence=82.0,
        action=ActionPhase.GO,
        trigger=100.0,
        stop=98.0,
        target=101.0,
        invalidation=98.0,
        trigger_event=DomainFact("manual_channel"),
        reasons=["manual channel"],
    )

    gated = apply_global_min_rr_to_decision(decision, 1.25)

    assert gated.kind == ScenarioKind.WAIT
    assert gated.direction == Direction.FLAT
    assert gated.action is ActionPhase.BLOCK
    assert "RR 0.50 below global min 1.25" in gated.reasons[-1]


def test_global_min_rr_gate_uses_zero_stop_before_invalidation() -> None:
    decision = ScenarioDecision(
        kind=ScenarioKind.TRANSIT,
        direction=Direction.LONG,
        confidence=82.0,
        action=ActionPhase.GO,
        trigger=1.0,
        stop=0.0,
        target=3.0,
        invalidation=2.0,
        trigger_event=DomainFact("zero_stop_rr"),
        reasons=["zero stop"],
    )

    gated = apply_global_min_rr_to_decision(decision, 2.5)

    assert gated.direction == Direction.FLAT
    assert gated.action is ActionPhase.BLOCK


def test_shared_active_lifecycle_selects_strongest_indicator_plan() -> None:
    active = active_lifecycle_from_indicators(
        {
            "impulse_fib": {
                "latest": {
                    "score": 65,
                    "signal": {"score": 65},
                    "lifecycle": {
                        "active": True,
                        "state": "FOLLOW",
                        "direction": "long",
                        "entry": 100,
                        "stop": 99,
                        "target": 103,
                        "filled": True,
                        "age_bars": 4,
                    },
                }
            },
            "linda_volume": {
                "latest": {
                    "score": 82,
                    "signal": {"score": 82},
                    "lifecycle": {
                        "active": True,
                        "state": "WAIT_ENTRY",
                        "direction": "short",
                        "entry": 104,
                        "stop": 105,
                        "target": 101,
                        "age_bars": 2,
                    },
                }
            },
        }
    )

    assert active is not None
    assert active["source"] == "linda_volume"
    assert active["label"] == "Linda"
    assert active["direction"] == "short"


def test_shared_active_lifecycle_skips_incoherent_indicator_plan() -> None:
    active = active_lifecycle_from_indicators(
        {
            "linda_volume": {
                "latest": {
                    "score": 90,
                    "signal": {"score": 90},
                    "lifecycle": {
                        "active": True,
                        "state": "FOLLOW",
                        "direction": "long",
                        "entry": 100,
                        "stop": 104,
                        "target": 96,
                        "filled": True,
                    },
                }
            },
            "impulse_fib": {
                "latest": {
                    "score": 65,
                    "signal": {"score": 65},
                    "lifecycle": {
                        "active": True,
                        "state": "FOLLOW",
                        "direction": "short",
                        "entry": 100,
                        "stop": 103,
                        "target": 94,
                        "filled": True,
                    },
                }
            },
        }
    )

    assert active is not None
    assert active["source"] == "impulse_fib"
    assert active["direction"] == "short"


def test_shared_active_lifecycle_skips_incomplete_indicator_plan() -> None:
    active = active_lifecycle_from_indicators(
        {
            "breakout_accumulation": {
                "latest": {
                    "score": 90,
                    "signal": {"score": 90},
                    "lifecycle": {
                        "active": True,
                        "state": "WAIT_ENTRY",
                        "direction": "long",
                        "entry": 100,
                        "stop": None,
                        "target": 103,
                    },
                }
            },
            "impulse_fib": {
                "latest": {
                    "score": 65,
                    "signal": {"score": 65},
                    "lifecycle": {
                        "active": True,
                        "state": "FOLLOW",
                        "direction": "short",
                        "entry": 100,
                        "stop": 103,
                        "target": 94,
                        "filled": True,
                    },
                }
            },
        }
    )

    assert active is not None
    assert active["source"] == "impulse_fib"
    assert active["direction"] == "short"


def test_decision_uses_indicator_plan_details_for_w5() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 100, 101, 99, 100, 1000),
        make_bar(2, 100, 101, 99, 100, 1000),
        make_bar(3, 100, 101, 99, 100, 1000),
        make_bar(4, 100, 101, 99, 100, 1000),
        make_bar(5, 100, 103, 99, 102, 1000),
    ]
    candidate = SignalCandidate(
        "w5_exhaustion",
        Direction.SHORT,
        72,
        99.5,
        "W5 model trigger",
        DomainFact("w5_model_trigger"),
        {"trigger": 99.5, "stop": 103.5, "target": 93.5},
        kind=ScenarioKind.FADE,
        source="w5",
        role="exhaustion",
        finality=CandidateFinality.CONFIRMED,
    )

    decision = choose_decision([candidate], bars=bars, atr_value=1.0)

    assert decision.action is ActionPhase.ARM
    assert decision.direction == Direction.SHORT
    assert decision.trigger == 99.5
    assert decision.stop == 103.5
    assert decision.target == 93.5


def test_decision_plan_entry_does_not_chase_live_close() -> None:
    bars = [
        make_bar(0, 100, 101, 99, 100, 1000),
        make_bar(1, 104, 106, 103, 105, 1000),
    ]
    candidate = SignalCandidate(
        "smc_structure",
        Direction.LONG,
        88,
        100.25,
        "retest level",
        DomainFact("retest_level"),
        kind=ScenarioKind.TRANSIT,
        finality=CandidateFinality.CONFIRMED,
    )

    decision = choose_decision([candidate], bars=bars, atr_value=1.0)

    assert decision.action is ActionPhase.GO
    assert decision.trigger == 100.25
