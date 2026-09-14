from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aef_terminal.indicators.modules.martin_carlo as martin_carlo_module
from aef_terminal.domain import (
    ActionPhase,
    Bar,
    Direction,
    DomainFact,
    ScenarioDecision,
    ScenarioKind,
    StrategyMode,
)
from aef_terminal.engine.analyze.indicator_status import attach_indicator_status
from aef_terminal.indicators.modules.martin_carlo import (
    MartinCarloParams,
    martin_carlo,
)
from aef_terminal.indicators.runtime import IndicatorRuntimeParams
from aef_terminal.runtime.instruments import PROFILES


def _bars(count: int = 140) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    out: list[Bar] = []
    price = 100.0
    for idx in range(count):
        drift = 0.08 if idx % 9 else -0.22
        open_ = price
        close = price + drift
        out.append(
            Bar(
                symbol="TEST",
                ts=start + timedelta(minutes=idx * 5),
                open=open_,
                high=max(open_, close) + 0.35,
                low=min(open_, close) - 0.35,
                close=close,
                volume=1000 + idx,
                timeframe="5m",
                source="test",
                closed=True,
            )
        )
        price = close
    return out


def test_martin_carlo_uses_packaged_model_assets() -> None:
    candle_path = martin_carlo_module._CANDLE_SPECIALIST_MODEL_PATH
    structural_path = martin_carlo_module._STRUCTURAL_SQUEEZE_MODEL_PATH

    assert candle_path.parent == structural_path.parent
    assert candle_path.parent == Path(martin_carlo_module.__file__).with_name("models")
    assert candle_path.is_file()
    assert structural_path.is_file()

    candle = martin_carlo_module.candle_nn_probabilities(_bars(180))
    structural = martin_carlo_module.structural_squeeze_bias(_bars(180))

    assert candle is not None
    assert candle["model_source"] == "trained_artifact"
    assert candle["model_path"] == str(candle_path)
    assert structural["enabled"] is True
    assert structural["source"] == "trained_artifact"


def test_martin_carlo_disabled_is_excluded_from_execution_specs() -> None:
    from types import SimpleNamespace
    from aef_terminal.engine.indicator_adapters import (
        IndicatorRunContext,
        indicator_execution_specs,
    )

    bars = _bars()
    params_by_indicator = {
        "option_reversal": SimpleNamespace(
            near_atr=0.5,
            compression_ratio=0.8,
            compression_neutral_ratio=0.9,
            wide_spread_ratio=1.2,
        ),
        "martin_carlo": MartinCarloParams(),
    }
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=IndicatorRuntimeParams(
            enabled={"martin_carlo": False, "option_reversal": True},
            by_indicator=params_by_indicator,
            errors={},
            strategy_mode=StrategyMode.BALANCED,
        ),
        indicator_params={},
        instrument_profile=PROFILES["CUSTOM"],
        features={},
        atr_value=1.0,
    )
    specs = indicator_execution_specs(ctx, stage="post_decision")
    assert "martin_carlo" not in {spec.id for spec in specs}
    assert "option_reversal" in {spec.id for spec in specs}


def test_martin_carlo_adapter_consumes_only_enabled_declared_context(monkeypatch) -> None:
    from aef_terminal.indicators.runtime import IndicatorRunContext

    captured: dict[str, object] = {}

    def calculate(_bars, **kwargs):
        captured.update(kwargs)
        return {"version": "test", "latest": None, "events": [], "overlays": []}

    monkeypatch.setattr(martin_carlo_module, "martin_carlo", calculate)
    bars = _bars()
    enabled = {
        "martin_carlo": True,
        "impulse_fib": True,
        "absorption_trap": False,
        "w5_structure": True,
        "wolfe_structure": True,
        "option_reversal": True,
    }
    bundle = {
        "impulse_fib": {"latest": {"state": "impulse"}},
        "absorption_trap": {"latest": {"state": "absorption"}},
        "w5_structure": {"latest": {"state": "w5"}},
        "wolfe_structure": {"latest": {"state": "wolfe"}},
        "undeclared_indicator": {"latest": {"state": "must_not_leak"}},
    }
    context = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=IndicatorRuntimeParams(
            enabled=enabled,
            by_indicator={
                indicator_id: (MartinCarloParams() if indicator_id == "martin_carlo" else {})
                for indicator_id in enabled
            },
            errors={},
            strategy_mode=StrategyMode.BALANCED,
        ),
        indicator_params={},
        instrument_profile=PROFILES["CUSTOM"],
        features={},
        indicator_bundle=bundle,
    )
    spec = martin_carlo_module.build_execution_spec(context)
    option_context = {"latest": {"state": "option"}}

    assert spec.calculate_with_context is not None
    spec.calculate_with_context({"option_reversal": option_context})

    assert captured["impulse_context"] is bundle["impulse_fib"]
    assert captured["option_context"] is option_context
    assert set(captured["indicator_context"]) == {
        "impulse_fib",
        "w5_structure",
        "wolfe_structure",
        "option_reversal",
    }


def test_martin_carlo_forecast_contract() -> None:
    result = martin_carlo(
        _bars(),
        MartinCarloParams(
            mode="both",
            engine="mean_reversion",
            lookback=80,
            horizon=12,
            simulations=60,
            price_bins=8,
        ),
    )

    forecast = result["latest"]["forecast"]
    assert result["overlays"][0]["type"] == "custom"
    assert result["overlays"][0]["renderer_ref"] == "martin_carlo_forecast"
    assert result["overlays"][0]["payload"] == forecast
    assert result["overlays"][0]["render_key"]
    assert forecast["kind"] == "martin_carlo_forecast"
    assert forecast["horizon_bars"] == 12
    assert len(forecast["heatmap"]) == 12
    assert len(forecast["percentiles"]["p50"]) == 12
    assert forecast["touch_distribution"]["low"]["p16"] <= forecast["terminal_distribution"]["p50"]
    assert forecast["touch_distribution"]["high"]["p84"] >= forecast["terminal_distribution"]["p50"]
    assert forecast["terminal_distribution"]["bins"]
    assert forecast["advisory"]["zone"] in {"LOW_TAIL", "LOW_EDGE", "MID", "HIGH_EDGE", "HIGH_TAIL"}
    assert {"note", "target_note", "levels"} <= set(forecast["advisory"])
    assert 0 <= forecast["metrics"]["bullish_probability"] <= 1
    assert 0 <= forecast["metrics"]["bearish_probability"] <= 1
    assert isinstance(forecast.get("bias"), dict)
    assert isinstance(forecast.get("anchors"), dict)


def test_martin_carlo_forecast_anchors_price_relative_to_mean() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def make_bars(drift: float, count: int = 140) -> list[Bar]:
        out: list[Bar] = []
        price = 100.0
        for idx in range(count):
            open_ = price
            close = price + drift
            out.append(
                Bar(
                    symbol="TEST",
                    ts=start + timedelta(minutes=idx * 5),
                    open=open_,
                    high=max(open_, close) + 0.35,
                    low=min(open_, close) - 0.35,
                    close=close,
                    volume=1000 + idx,
                    timeframe="5m",
                    source="test",
                    closed=True,
                )
            )
            price = close
        return out

    up = martin_carlo(
        make_bars(0.12), MartinCarloParams(engine="mean_reversion", simulations=40, horizon=8)
    )
    down = martin_carlo(
        make_bars(-0.12), MartinCarloParams(engine="mean_reversion", simulations=40, horizon=8)
    )
    up_forecast = up["latest"]["forecast"]
    down_forecast = down["latest"]["forecast"]
    assert up_forecast is not None and down_forecast is not None
    assert up_forecast["anchor_price"] > up_forecast["anchors"]["mean"]
    assert down_forecast["anchor_price"] < down_forecast["anchors"]["mean"]


def test_martin_carlo_skips_forecast_when_visualization_disabled() -> None:
    full = martin_carlo(
        _bars(),
        MartinCarloParams(simulations=60, horizon=12, compute_forecast=True),
    )
    lite = martin_carlo(
        _bars(),
        MartinCarloParams(simulations=60, horizon=12, compute_forecast=False),
    )

    assert full["latest"]["forecast"] is not None
    assert full["overlays"]
    assert lite["latest"]["forecast"] is None
    assert lite["overlays"] == []
    assert lite["latest"]["engine"]
    assert lite["latest"]["summary"]["forecast_computed"] is False
    assert lite["settings"]["compute_forecast"] is False


def test_martin_carlo_gbm_engine_and_candle_bias(monkeypatch) -> None:
    def fake_probs(_bars):
        return {
            "prob_bear": 0.10,
            "prob_neutral": 0.08,
            "prob_bull": 0.82,
            "edge": 72.0,
            "model_source": "test",
        }

    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", fake_probs
    )
    result = martin_carlo(
        _bars(),
        MartinCarloParams(
            mode="fan",
            engine="gbm",
            lookback=80,
            horizon=10,
            simulations=50,
            price_bins=8,
        ),
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine"] == "gbm"
    assert forecast["bias"]["source"] == "candle_specialist"
    assert forecast["bias"]["enabled"] is True
    assert forecast["metrics"]["candle_nn_bull"] == 0.82
    assert forecast["metrics"]["candle_nn_bear"] == 0.1
    assert forecast["metrics"]["candle_nn_confidence"] == 0.82
    assert forecast["metrics"]["candle_nn_confidence_threshold"] == 0.75
    assert forecast["metrics"]["candle_nn_drift_bias"] > 0


def test_martin_carlo_gates_low_confidence_candle_bias(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities",
        lambda _bars: {
            "prob_bear": 0.20,
            "prob_neutral": 0.10,
            "prob_bull": 0.70,
            "edge": 50.0,
            "model_source": "test",
        },
    )
    result = martin_carlo(
        _bars(),
        MartinCarloParams(
            mode="fan",
            engine="gbm",
            lookback=80,
            horizon=10,
            simulations=50,
            price_bins=8,
        ),
    )

    forecast = result["latest"]["forecast"]
    assert forecast["bias"]["enabled"] is False
    assert forecast["bias"]["reason_code"] == "confidence_gate"
    assert forecast["metrics"]["candle_nn_enabled"] is False
    assert forecast["metrics"]["candle_nn_confidence"] == 0.7
    assert forecast["metrics"]["candle_nn_gate_reason_code"] == "confidence_gate"
    assert forecast["metrics"]["candle_nn_drift_bias"] == 0.0


def test_martin_carlo_uses_nn_regime_bias(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities",
        lambda _bars: {
            "prob_bear": 0.12,
            "prob_neutral": 0.18,
            "prob_bull": 0.70,
            "edge": 58.0,
            "model_source": "test",
        },
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.nn_regime_bias",
        lambda _bars, candle_bias=None: {
            "enabled": True,
            "source": "test",
            "dominant": "bull_continuation",
            "bull_continuation": 0.62,
            "bear_continuation": 0.08,
            "mean_reversion": 0.10,
            "vol_expansion": 0.14,
            "neutral": 0.06,
            "continuation_edge": 0.54,
        },
    )
    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            mode="both",
            engine="auto",
            lookback=100,
            horizon=8,
            simulations=50,
            price_bins=8,
            nn_bias=True,
            structural_bias=False,
        ),
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine_reason_code"] == "nn_regime:bull_continuation"
    assert forecast["metrics"]["nn_bias_enabled"] is True
    assert forecast["metrics"]["nn_regime_dominant"] == "bull_continuation"
    assert forecast["metrics"]["nn_drift_bias"] > 0


def test_martin_carlo_uses_structural_squeeze_bias(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.structural_squeeze_bias",
        lambda _bars, enabled=True: {
            "enabled": True,
            "source": "test_artifact",
            "dominant": "breakout_down",
            "recommended_engine": "gbm",
            "vol_multiplier": 1.55,
            "direction_bias": -0.42,
            "squeeze_probability": 0.74,
        },
    )
    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            mode="both",
            engine="auto",
            lookback=100,
            horizon=8,
            simulations=50,
            price_bins=8,
            structural_bias=True,
        ),
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine_reason_code"] == "structural_squeeze:gbm"
    assert forecast["bias"]["structural_squeeze"]["enabled"] is True
    assert forecast["metrics"]["structural_bias_enabled"] is True
    assert forecast["metrics"]["structural_drift_bias"] < 0
    assert forecast["metrics"]["structural_vol_multiplier"] > 1


def test_martin_carlo_auto_engine_uses_regime_filter_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            mode="heatmap",
            engine="auto",
            lookback=120,
            horizon=10,
            simulations=50,
            price_bins=8,
            regime_filter=True,
        ),
    )

    forecast = result["latest"]["forecast"]
    assert forecast["requested_engine"] == "auto"
    assert forecast["engine"] in {"mean_reversion", "returns", "gbm"}
    assert forecast["engine_reason_code"]


def test_martin_carlo_uses_impulse_bias_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    impulse = {
        "latest": {
            "direction": "long",
            "score": 86,
            "important": True,
            "event": "FIB-L",
            "entry": 101.0,
            "target": 103.0,
            "stop": 99.0,
            "signal": {
                "action": "GO",
                "direction": "long",
                "score": 86,
            },
        }
    }

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            mode="both",
            engine="mean_reversion",
            lookback=100,
            horizon=8,
            simulations=50,
            price_bins=8,
            impulse_bias=True,
        ),
        impulse_context=impulse,
    )

    forecast = result["latest"]["forecast"]
    assert forecast["bias"]["impulse"]["enabled"] is True
    assert forecast["bias"]["impulse"]["direction"] == "long"
    assert forecast["metrics"]["impulse_bias_enabled"] is True
    assert forecast["metrics"]["impulse_drift_bias"] > 0
    assert forecast["regime_filter"]["enabled"] is True
    assert forecast["metrics"]["regime_filter_sample_size"] >= 1


def test_martin_carlo_exposes_impulse_nn_target_arrow(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.structural_squeeze_bias",
        lambda _bars, enabled=True: {
            "enabled": True,
            "source": "test_artifact",
            "dominant": "breakout_down",
            "recommended_engine": "gbm",
            "vol_multiplier": 1.6,
            "direction_bias": -0.7,
            "squeeze_probability": 0.78,
            "breakout_down": 0.68,
            "breakout_up": 0.08,
            "mean_reversion": 0.12,
            "neutral": 0.12,
        },
    )
    impulse = {
        "latest": {
            "direction": "short",
            "score": 88,
            "important": True,
            "event": "BRK-S",
            "signal": {
                "action": "GO",
                "direction": "short",
                "score": 88,
            },
        }
    }

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            mode="both",
            engine="auto",
            lookback=100,
            horizon=8,
            simulations=50,
            price_bins=8,
            structural_bias=True,
            impulse_bias=True,
        ),
        impulse_context=impulse,
    )

    arrow = result["latest"]["forecast"]["impulse_target_arrow"]
    assert arrow["enabled"] is True
    assert arrow["direction"] == "short"
    assert arrow["target_price"] < arrow["start_price"]
    assert arrow["source"] == "structural_squeeze"


def test_martin_carlo_ignores_historical_or_inactive_impulse_signal(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    impulse = {
        "last_event": {
            "direction": "long",
            "score": 95,
            "important": True,
        },
        "latest": {
            "direction": "long",
            "score": 95,
            "important": True,
            "signal": {
                "action": "WAIT",
                "direction": "long",
                "score": 95,
                "obsolete": True,
            },
        },
    }

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            engine="mean_reversion",
            lookback=100,
            horizon=8,
            simulations=50,
            price_bins=8,
            impulse_bias=True,
        ),
        impulse_context=impulse,
    )

    impulse_bias = result["latest"]["forecast"]["bias"]["impulse"]
    assert impulse_bias == {"enabled": False, "reason_code": "current_signal_inactive"}
    assert result["latest"]["forecast"]["metrics"]["impulse_bias_enabled"] is False


def test_martin_carlo_reports_warmup_and_unsupported_price_domain() -> None:
    warmup_bars = _bars(5)
    warmup = martin_carlo(warmup_bars)

    assert warmup["availability"] == {
        "state": "blocked",
        "reason_code": "martin_carlo_warmup",
        "required_bars": 40,
        "available_bars": 5,
    }
    normalized = attach_indicator_status(
        "martin_carlo",
        warmup,
        input_bars=warmup_bars,
        analysis_bar=warmup_bars[-1],
        mode="confirmed",
    )
    assert normalized["status"]["state_code"] == "blocked_context"
    assert normalized["status"]["reason_code"] == "martin_carlo_warmup"

    negative_bars = [
        Bar(
            symbol=bar.symbol,
            ts=bar.ts,
            open=-100.0,
            high=-99.0,
            low=-101.0,
            close=-100.25,
            volume=bar.volume,
            timeframe=bar.timeframe,
            source=bar.source,
            closed=True,
        )
        for bar in _bars(80)
    ]
    unsupported = martin_carlo(negative_bars)

    assert unsupported["availability"] == {
        "state": "blocked",
        "reason_code": "unsupported_price_domain",
        "price_domain": "positive_log_returns",
    }


def test_martin_carlo_uses_option_bridge_bias(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    option_context = {
        "latest": {
            "state": "BUY",
            "action": "BUY_PUT",
            "direction": "short",
            "option_side": "PUT",
            "score": 82,
            "monte_carlo_bridge": {
                "trigger_simulation": True,
                "recommended_engine": "gbm",
                "bias_settings": {
                    "pine_bias": -0.35,
                    "vol_multiplier": 1.45,
                    "target_attractor": 98.0,
                },
            },
        }
    }

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            mode="both",
            engine="auto",
            lookback=100,
            horizon=8,
            simulations=50,
            price_bins=8,
            option_bias=True,
        ),
        option_context=option_context,
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine_reason_code"] == "option_bridge:gbm"
    assert forecast["bias"]["option"]["enabled"] is True
    assert forecast["bias"]["option"]["direction"] == "short"
    assert forecast["bias"]["option"]["option_side"] == "PUT"
    assert forecast["bias"]["option"]["trigger_simulation"] is True
    assert forecast["metrics"]["option_bias_enabled"] is True
    assert forecast["metrics"]["option_bias_direction"] == "short"
    assert forecast["metrics"]["option_bias_side"] == "PUT"
    assert forecast["metrics"]["option_bias_trigger_simulation"] is True
    assert forecast["metrics"]["option_drift_bias"] < 0
    assert forecast["metrics"]["option_vol_multiplier"] == 1.45


def test_martin_carlo_exposes_model_coefficients(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    result = martin_carlo(
        _bars(160),
        MartinCarloParams(
            engine="mean_reversion",
            horizon=8,
            simulations=40,
            mean_reversion_strength=0.21,
            returns_drift_mix=0.33,
            candle_bias_weight=0.22,
            neutral_vol_weight=0.11,
            mean_pull_vol_cap=1.7,
        ),
    )

    metrics = result["latest"]["forecast"]["metrics"]
    assert metrics["mean_reversion_strength"] == 0.21
    assert metrics["returns_drift_mix"] == 0.33
    assert metrics["candle_bias_weight"] == 0.22
    assert metrics["neutral_vol_weight"] == 0.11
    assert metrics["mean_pull_vol_cap"] == 1.7


def test_martin_carlo_structural_mean_reversion_boosts_pull(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.structural_squeeze_bias",
        lambda _bars, enabled=True: {
            "enabled": True,
            "source": "test_artifact",
            "dominant": "mean_reversion",
            "recommended_engine": "mean_reversion",
            "vol_multiplier": 1.05,
            "direction_bias": 0.60,
            "squeeze_probability": 0.12,
            "mean_reversion": 0.80,
        },
    )

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(
            engine="auto",
            horizon=8,
            simulations=50,
            mean_reversion_strength=0.15,
            structural_mean_reversion_weight=0.35,
        ),
    )

    metrics = result["latest"]["forecast"]["metrics"]
    assert result["latest"]["forecast"]["engine_reason_code"] == "structural_squeeze:mean_reversion"
    assert metrics["structural_mean_reversion"] == 0.8
    assert metrics["structural_mean_reversion_boost"] == 0.28
    assert metrics["effective_mean_reversion_strength"] == 0.43
    assert metrics["structural_drift_bias"] > 0


def test_martin_carlo_auto_prefers_traffic_light_continuation_over_structural_mr(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.structural_squeeze_bias",
        lambda _bars, enabled=True: {
            "enabled": True,
            "source": "test_artifact",
            "dominant": "mean_reversion",
            "recommended_engine": "mean_reversion",
            "vol_multiplier": 1.05,
            "direction_bias": 0.10,
            "squeeze_probability": 0.18,
            "mean_reversion": 0.62,
            "breakout_up": 0.48,
            "breakout_down": 0.05,
        },
    )
    decision = ScenarioDecision(
        kind=ScenarioKind.TRANSIT,
        direction=Direction.LONG,
        confidence=82.0,
        action=ActionPhase.GO,
        trigger=101.0,
        stop=99.0,
        target=104.0,
        invalidation=98.5,
        trigger_event=DomainFact("traffic_light_continuation"),
        reasons=["traffic light continuation"],
    )

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(engine="auto", horizon=8, simulations=50),
        decision_context=decision,
        indicator_context={},
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine"] == "returns"
    assert forecast["engine_reason_code"] == "traffic_light:continuation"
    assert forecast["metrics"]["traffic_engine_phase"] == "continuation"
    assert forecast["metrics"]["reversal_warning_enabled"] is False


def test_martin_carlo_auto_turns_off_fade_mr_when_traffic_continues(monkeypatch) -> None:
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.structural_squeeze_bias",
        lambda _bars, enabled=True: {
            "enabled": True,
            "source": "test_artifact",
            "dominant": "mean_reversion",
            "recommended_engine": "mean_reversion",
            "vol_multiplier": 1.05,
            "direction_bias": 0.06,
            "squeeze_probability": 0.12,
            "mean_reversion": 0.54,
            "breakout_up": 0.46,
            "breakout_down": 0.08,
        },
    )
    decision = ScenarioDecision(
        kind=ScenarioKind.FADE,
        direction=Direction.LONG,
        confidence=88.0,
        action=ActionPhase.GO,
        trigger=101.0,
        stop=99.0,
        target=104.0,
        invalidation=98.5,
        trigger_event=DomainFact("traffic_light_continuation"),
        reasons=["traffic light resumed continuation"],
    )

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(engine="auto", horizon=8, simulations=50),
        decision_context=decision,
        indicator_context={},
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine"] == "returns"
    assert forecast["engine_reason_code"] == "traffic_light:continuation"
    assert forecast["metrics"]["traffic_engine_kind"] == "fade"
    assert forecast["metrics"]["traffic_engine_phase"] == "continuation"
    assert forecast["metrics"]["reversal_warning_enabled"] is False


def test_martin_carlo_warning_override_switches_continuation_to_mr(monkeypatch) -> None:
    source = inspect.getsource(martin_carlo_module)
    assert '"BUY_PUT" in option_action' not in source
    assert '"BUY_CALL" in option_action' not in source

    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.candle_nn_probabilities", lambda _bars: None
    )
    monkeypatch.setattr(
        "aef_terminal.indicators.modules.martin_carlo.structural_squeeze_bias",
        lambda _bars, enabled=True: {
            "enabled": True,
            "source": "test_artifact",
            "dominant": "mean_reversion",
            "recommended_engine": "mean_reversion",
            "vol_multiplier": 1.05,
            "direction_bias": 0.10,
            "squeeze_probability": 0.10,
            "mean_reversion": 0.76,
            "breakout_up": 0.30,
            "breakout_down": 0.04,
        },
    )
    decision = ScenarioDecision(
        kind=ScenarioKind.TRANSIT,
        direction=Direction.LONG,
        confidence=82.0,
        action=ActionPhase.GO,
        trigger=101.0,
        stop=99.0,
        target=104.0,
        invalidation=98.5,
        trigger_event=DomainFact("traffic_light_continuation"),
        reasons=["traffic light continuation"],
    )

    result = martin_carlo(
        _bars(180),
        MartinCarloParams(engine="auto", horizon=8, simulations=50),
        decision_context=decision,
        indicator_context={
            "w5_structure": {"latest": {"direction": "short", "score": 66, "action": "WATCH"}},
        },
    )

    forecast = result["latest"]["forecast"]
    assert forecast["engine"] == "mean_reversion"
    assert forecast["engine_reason_code"].startswith("warning_override:")
    assert forecast["metrics"]["traffic_engine_phase"] == "continuation"
    assert forecast["metrics"]["reversal_warning_enabled"] is True
    assert "w5_exhaustion" in forecast["metrics"]["reversal_warning_items"]
