from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.backtest.mvp import (
    BacktestTickHistory,
    _inverted_trade_plan,
    _simulate_trade,
    _tick_profile_features_for_bar,
    _tick_profile_history_by_ts,
    _widened_trade_plan,
    run_backtest as _run_backtest,
    run_backtest_variants,
    time_splits,
)
from aef_terminal.data.instrument_identity import route_fingerprint
from aef_terminal.domain import Bar, BarProviderRequest, BarProvenance
from aef_terminal.engine.analyze.inputs import admit_replay_confirmed_bars
from aef_terminal.engine.trade_setup import trade_setup_execution_authority
from tests.provider_payloads import ibkr_future_payload


_INSTRUMENT = ibkr_future_payload("ES")
_PROVENANCE = BarProvenance(
    provider="ibkr",
    instrument_id=_INSTRUMENT["instrument_id"],
    route_fingerprint=route_fingerprint(_INSTRUMENT),
    request_type=BarProviderRequest.CANONICAL_STORAGE,
    provider_contract_id="",
    provider_contract_type="CANONICAL_STORAGE",
    data_type="canonical_ohlcv",
)


def run_backtest(bars, **kwargs):
    kwargs.setdefault("instrument", _INSTRUMENT)
    return _run_backtest(bars, **kwargs)


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


def bar(index: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        symbol="ES",
        ts=datetime(2026, 1, 1, 14, 0, tzinfo=UTC) + timedelta(minutes=5 * index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000,
        timeframe="5m",
        source="test",
        closed=True,
        provenance=_PROVENANCE,
    )


def minute_bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    provenance: BarProvenance = _PROVENANCE,
) -> Bar:
    return Bar(
        symbol="ES",
        ts=datetime(2026, 1, 1, 14, 0, tzinfo=UTC) + timedelta(minutes=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=200,
        timeframe="1m",
        source="test",
        closed=True,
        provenance=provenance,
    )


def test_backtest_simulates_triple_barrier_and_mfe_mae() -> None:
    signal = bar(0, 100, 100.5, 99.5, 100)
    future = [
        bar(1, 100, 101.2, 99.8, 101.0),
        bar(2, 101, 103.2, 100.8, 103.0),
    ]
    trade = _simulate_trade(
        {
            "source": "trade_setup",
            "setup": "transit",
            "side": "long",
            "confidence": 82,
            "entry": 100.0,
            "stop": 98.0,
            "target": 103.0,
        },
        signal,
        future,
    )

    assert trade is not None
    assert trade.exit_reason == "target"
    assert trade.pnl_points == 3.0
    assert trade.r_multiple == 1.5
    assert trade.mfe_points == 3.2
    assert trade.mae_points == -0.2
    assert trade.utc_daypart == "utc_13_21"
    assert trade.features == {}


def test_walk_forward_time_splits_are_simple_ordered_folds() -> None:
    bars = [bar(index, 100, 101, 99, 100.5) for index in range(12)]
    splits = time_splits(bars, instrument=_INSTRUMENT, folds=3)

    assert [item["fold"] for item in splits] == [1, 2, 3]
    assert splits[0]["train"] is None
    assert splits[1]["train"][0] == bars[0].ts
    assert splits[2]["test"][1] == bars[-1].ts


def test_backtest_replay_uses_signal_bar_close_as_analysis_clock(monkeypatch) -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]
    seen_analysis_as_of = []

    def fake_analyze_bars(window, *, analysis_as_of_utc=None, **_kwargs):
        seen_analysis_as_of.append(analysis_as_of_utc)
        return {
            "decision": {"confidence": 82},
            "trade_setup": {
                "ok": True,
                "action": "GO",
                "side": "long",
                "kind": "transit",
                "signal_source": "breakout_accumulation",
                "quality": 82,
                "plan": {"entry": 100.0, "stop": 98.0, "target": 102.0},
                "trigger_event": {"code": "transit_confirmed"},
                "confluence": [
                    {
                        "source": "breakout_accumulation",
                        "label": "Breakout flow",
                        "score": 82.0,
                    }
                ],
                "action_card": {"phase": "GO", "setup": "transit"},
                "execution_authority": ready_trade_setup_execution_authority(),
            },
        }

    monkeypatch.setattr("aef_terminal.backtest.mvp.analyze_bars", fake_analyze_bars)

    result = run_backtest(
        bars,
        warmup=2,
        horizon=2,
        step=1,
        max_signals=1,
        indicator_params={"trade_setup_engine": {"enabled": True}},
    )

    assert seen_analysis_as_of == [bars[2].ts + timedelta(minutes=5)]
    assert result["model"] == {
        "kind": "independent_signal_outcomes",
        "entry": "next_confirmed_bar_touch",
        "same_bar_priority": "stop_before_target",
        "timeout": "horizon_last_close",
        "costs": "excluded",
    }
    assert result["summary"]["trades"] == 1
    assert result["by_utc_daypart"][0]["key"] == "utc_13_21"
    assert result["by_source"][0]["key"] == "breakout_accumulation"
    assert result["trades"][0]["features"]["planned_rr"] == 1.0


def test_backtest_same_bar_collision_uses_declared_conservative_priority() -> None:
    trade = _simulate_trade(
        {
            "source": "trade_setup",
            "setup": "transit",
            "side": "long",
            "confidence": 82,
            "entry": 100.0,
            "stop": 98.0,
            "target": 102.0,
        },
        bar(0, 100, 100.5, 99.5, 100),
        [bar(1, 100, 103.0, 97.0, 101.0)],
    )

    assert trade is not None
    assert trade.exit_reason == "stop"
    assert trade.exit_price == 98.0


def test_backtest_fast_entry_uses_only_causal_confirmed_lower_timeframe_bars(
    monkeypatch,
) -> None:
    bars = [bar(index, 100, 103, 97, 100.5) for index in range(8)]

    def fake_analyze_bars(_window, **_kwargs):
        return {
            "trade_setup": {
                "ok": True,
                "action": "GO",
                "side": "long",
                "kind": "fade",
                "signal_source": "absorption_trap",
                "quality": 82,
                "plan": {"entry": 100.0, "stop": 98.0, "target": 102.0},
                "trigger_event": {"code": "absorption_reclaim"},
                "confluence": [
                    {
                        "source": "absorption_trap",
                        "label": "Absorption Trap",
                        "score": 82.0,
                    }
                ],
                "action_card": {"phase": "GO", "setup": "fade"},
                "execution_authority": ready_trade_setup_execution_authority(),
            }
        }

    monkeypatch.setattr("aef_terminal.backtest.mvp.analyze_bars", fake_analyze_bars)

    result = run_backtest(
        bars,
        warmup=2,
        horizon=2,
        max_signals=1,
        indicator_params={"trade_setup_engine": {"enabled": True}},
        fast_entry_bars=[
            minute_bar(14, 100.0, 102.5, 97.5, 100.0),
            minute_bar(15, 100.2, 101.0, 99.8, 100.5),
            minute_bar(16, 100.5, 102.2, 100.4, 102.0),
            minute_bar(17, 102.0, 102.1, 97.5, 98.0),
        ],
    )

    trade = result["trades"][0]
    assert trade["opened_at"] == minute_bar(15, 0, 0, 0, 0).ts.isoformat()
    assert trade["closed_at"] == minute_bar(16, 0, 0, 0, 0).ts.isoformat()
    assert trade["exit_reason"] == "target"
    assert trade["hold_bars"] == 2
    assert trade["features"]["execution_timeframe"] == "1m"
    assert result["model"]["entry"] == "next_confirmed_lower_timeframe_bar_touch"
    assert result["model"]["execution_timeframe"] == "1m"


def test_backtest_fast_entry_rejects_non_lower_timeframe() -> None:
    bars = [bar(index, 100, 101, 99, 100) for index in range(8)]

    with pytest.raises(ValueError, match="must be lower timeframe"):
        run_backtest(bars, warmup=2, horizon=2, fast_entry_bars=bars)


def test_backtest_rejects_confirmed_analysis_bar_without_exact_provenance() -> None:
    bars = [bar(index, 100, 101, 99, 100) for index in range(8)]
    bars[3] = replace(bars[3], provenance=None)

    with pytest.raises(
        ValueError,
        match="REPLAY_BAR_ADMISSION_REJECTED.*reason=provider_provenance_missing",
    ):
        run_backtest(bars, warmup=2, horizon=2)


def test_backtest_fast_entry_rejects_same_symbol_from_another_route() -> None:
    bars = [bar(index, 100, 101, 99, 100) for index in range(8)]
    mismatched_provenance = replace(_PROVENANCE, route_fingerprint="another-route")

    with pytest.raises(
        ValueError,
        match="REPLAY_BAR_ADMISSION_REJECTED.*reason=route_fingerprint_mismatch",
    ):
        run_backtest(
            bars,
            warmup=2,
            horizon=2,
            fast_entry_bars=[minute_bar(15, 100, 101, 99, 100, provenance=mismatched_provenance)],
        )


def test_replay_admission_accepts_typed_virtual_three_minute_derivation() -> None:
    derived_provenance = BarProvenance(
        provider="ibkr",
        instrument_id=_INSTRUMENT["instrument_id"],
        route_fingerprint=route_fingerprint(_INSTRUMENT),
        request_type=BarProviderRequest.DETERMINISTIC_AGGREGATION,
        provider_contract_id="continuous-future",
        provider_contract_type="CONTFUT",
        data_type="canonical_ohlcv",
        source_timeframe="1m",
    )
    derived_bar = Bar(
        symbol="ES",
        ts=datetime(2026, 1, 1, 14, 0, tzinfo=UTC),
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=600,
        timeframe="3m",
        source="derived:1m",
        closed=True,
        provenance=derived_provenance,
    )

    assert admit_replay_confirmed_bars(
        [derived_bar],
        instrument=_INSTRUMENT,
        field="test replay bars",
    ) == [derived_bar]


def test_backtest_fails_closed_when_analysis_window_fails(monkeypatch) -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]

    def fail_analysis(*_args, **_kwargs):
        raise ValueError("broken indicator contract")

    monkeypatch.setattr("aef_terminal.backtest.mvp.analyze_bars", fail_analysis)

    with pytest.raises(RuntimeError, match="BACKTEST_ANALYSIS_FAILED") as exc:
        run_backtest(bars, warmup=2, horizon=2)

    assert isinstance(exc.value.__cause__, ValueError)
    assert bars[2].ts.isoformat() in str(exc.value)


def test_backtest_variants_fail_closed_when_analysis_window_fails(monkeypatch) -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]

    def fail_analysis(*_args, **_kwargs):
        raise ValueError("broken indicator contract")

    monkeypatch.setattr("aef_terminal.backtest.mvp.analyze_bars", fail_analysis)

    with pytest.raises(RuntimeError, match="BACKTEST_ANALYSIS_FAILED") as exc:
        run_backtest_variants(
            bars,
            [{"variant_id": "baseline"}],
            instrument=_INSTRUMENT,
            warmup=2,
            horizon=2,
        )
    assert bars[2].ts.isoformat() in str(exc.value)


def test_backtest_include_sources_filters_trades(monkeypatch) -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]
    call = {"n": 0}

    def fake_analyze_bars(window, **_kwargs):
        call["n"] += 1
        source = "w5_exhaustion" if call["n"] % 2 else "linda_fade"
        return {
            "decision": {"confidence": 82},
            "trade_setup": {
                "ok": True,
                "action": "GO",
                "side": "long",
                "kind": "fade",
                "signal_source": source,
                "quality": 82,
                "plan": {"entry": 100.0, "stop": 98.0, "target": 104.0},
                "trigger_event": {"code": "fade_confirmed"},
                "confluence": [{"source": source, "label": source, "score": 82.0}],
                "action_card": {"phase": "GO", "setup": "fade"},
                "execution_authority": ready_trade_setup_execution_authority(),
            },
        }

    monkeypatch.setattr("aef_terminal.backtest.mvp.analyze_bars", fake_analyze_bars)

    result = run_backtest(
        bars,
        warmup=2,
        horizon=2,
        step=1,
        max_signals=3,
        indicator_params={"trade_setup_engine": {"enabled": True}},
        include_sources=["linda_fade"],
    )

    assert result["summary"]["trades"] >= 1
    assert all(item["source"] == "linda_fade" for item in result["trades"])


def test_backtest_enriches_trade_features_with_tick_history(monkeypatch) -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]

    def fake_analyze_bars(window, *, analysis_as_of_utc=None, **_kwargs):
        return {
            "decision": {"confidence": 82},
            "trade_setup": {
                "ok": True,
                "action": "GO",
                "side": "long",
                "kind": "transit",
                "signal_source": "linda_volume",
                "quality": 82,
                "plan": {"entry": 100.0, "stop": 98.0, "target": 102.0},
                "trigger_event": {"code": "transit_confirmed"},
                "confluence": [{"source": "linda_volume", "label": "Linda PB", "score": 82.0}],
                "action_card": {"phase": "GO", "setup": "transit"},
                "execution_authority": ready_trade_setup_execution_authority(),
            },
        }

    monkeypatch.setattr("aef_terminal.backtest.mvp.analyze_bars", fake_analyze_bars)

    result = run_backtest(
        bars,
        warmup=2,
        horizon=2,
        step=1,
        max_signals=1,
        indicator_params={"trade_setup_engine": {"enabled": True}},
        tick_history=BacktestTickHistory(
            kind="bar",
            provider="ibkr",
            instrument_id=_INSTRUMENT["instrument_id"],
            route_fingerprint=route_fingerprint(_INSTRUMENT),
            timeframe="5m",
            rows=(
                {
                    "ts": bars[2].ts.isoformat(),
                    "total_volume": 420,
                    "net_delta": -84,
                    "trade_count": 120,
                    "delta_ratio": -0.2,
                },
            ),
        ),
        tick_profile_history=BacktestTickHistory(
            kind="price_profile",
            provider="ibkr",
            instrument_id=_INSTRUMENT["instrument_id"],
            route_fingerprint=route_fingerprint(_INSTRUMENT),
            timeframe="5m",
            price_step=0.25,
            rows=(
                {
                    "ts": bars[2].ts.isoformat(),
                    "price": 100.25,
                    "total_volume": 50,
                    "net_delta": 10,
                    "trade_count": 12,
                },
                {
                    "ts": bars[2].ts.isoformat(),
                    "price": 100.5,
                    "total_volume": 120,
                    "net_delta": -30,
                    "trade_count": 24,
                },
            ),
        ),
    )

    features = result["trades"][0]["features"]
    assert features["tick_total_volume"] == 420
    assert features["tick_net_delta"] == -84
    assert features["tick_trade_count"] == 120
    assert features["tick_delta_ratio"] == -0.2
    assert features["tick_profile_history"] is True
    assert features["tick_profile_levels"] == 2
    assert features["tick_profile_total_volume"] == 170
    assert features["tick_profile_net_delta"] == -20
    assert features["tick_profile_poc"] == 100.5
    assert features["tick_profile_poc_volume"] == 120


def test_backtest_rejects_legacy_unqualified_tick_history() -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]

    with pytest.raises(TypeError, match="tick_history must be BacktestTickHistory"):
        run_backtest(
            bars,
            warmup=2,
            horizon=2,
            tick_history=[{"ts": bars[2].ts.isoformat()}],
        )


def test_backtest_rejects_tick_history_from_another_exact_route() -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]

    with pytest.raises(ValueError, match="BACKTEST_TICK_HISTORY_ROUTE_MISMATCH"):
        run_backtest(
            bars,
            warmup=2,
            horizon=2,
            tick_history=BacktestTickHistory(
                kind="bar",
                provider="ibkr",
                instrument_id=_INSTRUMENT["instrument_id"],
                route_fingerprint="stale-route",
                timeframe="5m",
                rows=(),
            ),
        )


def test_backtest_tick_history_captures_an_immutable_row_snapshot() -> None:
    row = {
        "ts": "2026-01-01T14:00:00+00:00",
        "total_volume": 10,
        "net_delta": 2,
        "trade_count": 3,
    }

    history = BacktestTickHistory(
        kind="bar",
        provider="ibkr",
        instrument_id=_INSTRUMENT["instrument_id"],
        route_fingerprint=route_fingerprint(_INSTRUMENT),
        timeframe="5m",
        rows=(row,),
    )
    row["total_volume"] = 99

    assert history.rows[0]["total_volume"] == 10
    with pytest.raises(TypeError):
        history.rows[0]["total_volume"] = 11  # type: ignore[index]


def test_backtest_tick_profile_requires_exact_route_price_grid() -> None:
    bars = [bar(index, 100, 102, 98, 100.5) for index in range(8)]

    with pytest.raises(ValueError, match="PRICE_STEP_NOT_ROUTE_ALIGNED"):
        run_backtest(
            bars,
            warmup=2,
            horizon=2,
            tick_profile_history=BacktestTickHistory(
                kind="price_profile",
                provider="ibkr",
                instrument_id=_INSTRUMENT["instrument_id"],
                route_fingerprint=route_fingerprint(_INSTRUMENT),
                timeframe="5m",
                price_step=0.1,
                rows=(),
            ),
        )

    with pytest.raises(ValueError, match="PRICE_NOT_STEP_ALIGNED"):
        run_backtest(
            bars,
            warmup=2,
            horizon=2,
            tick_profile_history=BacktestTickHistory(
                kind="price_profile",
                provider="ibkr",
                instrument_id=_INSTRUMENT["instrument_id"],
                route_fingerprint=route_fingerprint(_INSTRUMENT),
                timeframe="5m",
                price_step=0.25,
                rows=(
                    {
                        "ts": bars[2].ts.isoformat(),
                        "price": 100.1,
                        "total_volume": 1,
                        "net_delta": 1,
                        "trade_count": 1,
                    },
                ),
            ),
        )


def test_backtest_tick_profile_poc_is_order_independent_and_absent_without_volume() -> None:
    signal_bar = bar(2, 100, 102, 98, 100.5)
    equal_volume_rows = (
        {
            "ts": signal_bar.ts.isoformat(),
            "price": 100.5,
            "total_volume": 10,
            "net_delta": -2,
            "trade_count": 2,
        },
        {
            "ts": signal_bar.ts.isoformat(),
            "price": 100.25,
            "total_volume": 10,
            "net_delta": 2,
            "trade_count": 2,
        },
    )

    first = _tick_profile_features_for_bar(
        _tick_profile_history_by_ts(equal_volume_rows, timeframe="5m", price_step=0.25),
        signal_bar,
    )
    second = _tick_profile_features_for_bar(
        _tick_profile_history_by_ts(
            tuple(reversed(equal_volume_rows)), timeframe="5m", price_step=0.25
        ),
        signal_bar,
    )
    zero_volume = _tick_profile_features_for_bar(
        _tick_profile_history_by_ts(
            (
                {
                    "ts": signal_bar.ts.isoformat(),
                    "price": 100.25,
                    "total_volume": 0,
                    "net_delta": 0,
                    "trade_count": 0,
                },
            ),
            timeframe="5m",
            price_step=0.25,
        ),
        signal_bar,
    )

    assert first["tick_profile_poc"] == second["tick_profile_poc"] == 100.25
    assert zero_volume == {}


def test_backtest_can_invert_source_by_flipping_side_and_swapping_barriers() -> None:
    plan = {
        "source": "smc_structure",
        "setup": "fade",
        "side": "long",
        "confidence": 90,
        "entry": 100.0,
        "stop": 98.0,
        "target": 104.0,
    }

    inverted = _inverted_trade_plan(plan)

    assert inverted["side"] == "short"
    assert inverted["entry"] == 100.0
    assert inverted["stop"] == 104.0
    assert inverted["target"] == 98.0
    assert inverted["inverted"] is True


def test_backtest_can_widen_stop_while_keeping_original_target() -> None:
    plan = {
        "source": "smc_structure",
        "setup": "fade",
        "side": "long",
        "confidence": 90,
        "entry": 100.0,
        "stop": 98.0,
        "target": 104.0,
    }

    widened = _widened_trade_plan(plan, stop_multiplier=1.5, target_mode="keep")

    assert widened["entry"] == 100.0
    assert widened["stop"] == 97.0
    assert widened["target"] == 104.0
    assert widened["stop_multiplier"] == 1.5
    assert widened["target_mode"] == "keep"


def test_backtest_can_widen_stop_and_preserve_reward_risk() -> None:
    long_plan = {
        "source": "smc_structure",
        "setup": "fade",
        "side": "long",
        "confidence": 90,
        "entry": 100.0,
        "stop": 98.0,
        "target": 104.0,
    }
    short_plan = {**long_plan, "side": "short", "stop": 102.0, "target": 96.0}

    widened_long = _widened_trade_plan(long_plan, stop_multiplier=1.5, target_mode="preserve-rr")
    widened_short = _widened_trade_plan(short_plan, stop_multiplier=1.5, target_mode="preserve-rr")

    assert widened_long["stop"] == 97.0
    assert widened_long["target"] == 106.0
    assert widened_short["stop"] == 103.0
    assert widened_short["target"] == 94.0
