from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from math import exp, sin
from pathlib import Path
from statistics import pstdev
import subprocess

import pytest

from aef_terminal.domain import Bar, StrategyMode
from aef_terminal.engine.analyze.indicator_runtime import (
    run_indicator,
    run_pipeline_indicator,
)
from aef_terminal.engine.indicator_pipeline import execute_indicator_spec
from aef_terminal.indicators.modules.adaptive_kernel_regression import (
    INDICATOR_MODULE,
    AdaptiveKernelRegressionParams,
    adaptive_kernel_regression,
    build_execution_spec,
    build_params,
)
from aef_terminal.indicators.runtime import IndicatorRunContext, IndicatorRuntimeParams
from aef_terminal.indicators.registry import INDICATOR_REGISTRY, indicator_manifest
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS
from aef_terminal.runtime import pine
from aef_terminal.runtime.instruments import PROFILES
from aef_terminal.ui.services.market_analysis_compaction import (
    compact_runtime_payload_for_response,
)


def _bars_from_closes(closes: list[float]) -> list[Bar]:
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        bars.append(
            Bar(
                symbol="TEST",
                ts=datetime(2026, 1, 5, 14, 30, tzinfo=UTC) + timedelta(minutes=index * 5),
                open=open_price,
                high=max(open_price, close) + 0.25,
                low=min(open_price, close) - 0.25,
                close=close,
                volume=0.0 if index % 11 == 0 else 1000.0 + index,
                timeframe="5m",
            )
        )
    return bars


def _fixed_params(**overrides: object) -> AdaptiveKernelRegressionParams:
    values: dict[str, object] = {
        "lookback_window": 10,
        "base_bandwidth": 3.0,
        "adaptive_bandwidth": False,
        "atr_len": 5,
        "output_smoothing": 1,
        "band_multiplier": 1.0,
        "band_lookback": 10,
        "band_smoothing": 1,
        "render_bars": 120,
    }
    values.update(overrides)
    return AdaptiveKernelRegressionParams(**values)


def test_indicator_is_startup_discovered_without_one_minute_context() -> None:
    spec = INDICATOR_REGISTRY["adaptive_kernel_regression"]
    manifest = indicator_manifest()["adaptive_kernel_regression"]

    assert INDICATOR_MODULE.spec is spec
    assert spec.confirmed_bar_context == ()
    assert spec.candidate_promoter == "none"
    assert spec.paper_tradable is False
    assert spec.default_calc is False
    assert spec.renderer_kind == "custom"
    assert spec.renderer_ref == "adaptive_kernel_regression_series"
    assert spec.renderer_primitives == ("custom", "label", "line", "marker", "table")
    assert spec.overlay_filter_ref == "adaptive_kernel_regression_filter"
    assert manifest["extensions"]["ui_js_assets"] == ["client.js"]
    assert manifest["confirmed_bar_context"] == []
    controls = {control["key"]: control for control in manifest["controls"]}
    assert controls["candles"]["default"] is True
    assert controls["theme"]["options"] == [
        "Classic",
        "Cyber Aqua",
        "Crimson Pulse",
        "Royal Purple",
        "Emerald Night",
        "Minimal Mono",
        "Classic Emerald",
    ]
    assert controls["textSize"]["label"] == "Text size"
    assert controls["textSize"]["default"] == "medium"
    assert controls["textSize"]["options"] == ["small", "medium", "large"]
    assert controls["visualMode"]["label"] == "Visual Mode"
    assert controls["visualMode"]["default"] == "bands"
    assert controls["visualMode"]["options"] == ["bands", "single", "trail"]
    assert controls["visualMode"]["option_labels"] == [
        "Bands",
        "Single Line",
        "Trail",
    ]
    assert controls["labelAnchor"]["options"] == ["high_low", "main", "bands"]
    assert controls["labelAnchor"]["option_labels"] == [
        "High/Low",
        "Main Line",
        "Bands",
    ]
    assert controls["calculationMode"]["options"] == ["confirmed", "provisional"]
    assert [control["key"] for control in manifest["controls"] if control["compact"]] == [
        "candles",
        "fill",
        "dashboard",
        "entryPlan",
    ]
    assert controls["entryPlan"]["label"] == "Entry Plan"
    assert controls["entryPlan"]["control_type"] == "toggle"
    assert controls["entryPlan"]["default"] is False
    assert controls["entryPlan"]["api_key"] == "adaptive_kernel_entry_plan_enabled"
    assert controls["entryPlan"]["param_key"] == "entry_plan_enabled"
    assert controls["entryPlan"]["action"] == "load_apply"
    assert controls["lookbackWindow"]["default"] == 30
    assert controls["baseBandwidth"]["default"] == 8.0
    assert controls["atrLen"]["default"] == 14
    assert controls["outputSmoothing"]["default"] == 3
    assert controls["bandMultiplier"]["default"] == 1.0
    assert controls["bandLookback"]["default"] == 24
    assert controls["bandSmoothing"]["default"] == 5
    assert {
        "labelSize",
        "lineWidth",
        "markers",
        "renderBars",
        "gradientSmooth",
    }.isdisjoint(controls)


def test_missing_numeric_preferences_preserve_indicator_defaults(tmp_path: Path) -> None:
    preferences = Path("src/aef_terminal/ui/assets/js/07-preferences.js").read_text(
        encoding="utf-8"
    )
    start = preferences.index("function numericValueOrFallback")
    end = preferences.index("\n    function storedGlobalIndicatorNumber", start)
    helper_source = preferences[start:end]
    script = tmp_path / "numeric-setting-defaults.js"
    script.write_text(
        "\n".join(
            (
                helper_source,
                "console.log(JSON.stringify([",
                "  numericValueOrFallback(null, 30),",
                "  numericValueOrFallback(undefined, 8),",
                "  numericValueOrFallback('', 14),",
                "  numericValueOrFallback('0', 3),",
                "  numericValueOrFallback('1.25', 5),",
                "]));",
            )
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [30, 8, 14, 0, 1.25]
    assert preferences.count("return numericValueOrFallback(") >= 2
    assert "return numericValueOrFallback(serverSettingValue(storageKey), fallback);" in preferences


def test_shared_select_controls_render_labels_without_changing_durable_values() -> None:
    controls_source = Path(
        "src/aef_terminal/ui/assets/js/30-panels-indicator-controls.js"
    ).read_text(encoding="utf-8")

    assert "(control.options || []).forEach((optionValue, optionIndex)" in controls_source
    assert "control.option_labels?.[optionIndex] || optionValue" in controls_source


def test_warmup_blocks_partial_kernel_and_band_windows() -> None:
    params = _fixed_params()
    bars = _bars_from_closes([100.0 + index * 0.1 for index in range(params.required_bars - 1)])

    result = adaptive_kernel_regression(bars, params=params)

    assert params.required_bars == 19
    assert result["series"] == []
    assert result["events"] == []
    assert result["overlays"] == []
    assert result["plan"]["state"] == "disabled"
    assert result["availability"] == {
        "state": "blocked",
        "reason_code": "kernel_warmup",
        "required_bars": 19,
        "available_bars": 18,
    }


def test_fixed_kernel_matches_independent_gaussian_and_population_sigma() -> None:
    closes = [100.0 + index * 0.17 + sin(index * 0.7) * 0.35 for index in range(36)]
    bars = _bars_from_closes(closes)
    params = _fixed_params()

    result = adaptive_kernel_regression(bars, params=params)

    weights = [exp(-(lag * lag) / (2.0 * 3.0 * 3.0)) for lag in range(10)]

    def kernel(index: int) -> float:
        return sum(weights[lag] * closes[index - lag] for lag in range(10)) / sum(weights)

    residuals = [closes[index] - kernel(index) for index in range(9, 19)]
    expected_center = kernel(18)
    expected_sigma = pstdev(residuals)
    first = result["series"][0]

    assert first["index"] == 18
    assert first["center"] == pytest.approx(expected_center, abs=1e-8)
    assert first["residual_sigma"] == pytest.approx(expected_sigma, abs=1e-8)
    assert first["upper"] == pytest.approx(expected_center + expected_sigma, abs=1e-8)
    assert first["lower"] == pytest.approx(expected_center - expected_sigma, abs=1e-8)


def test_flat_confirmed_bars_remain_neutral_and_keep_zero_volume() -> None:
    bars = [
        Bar(
            symbol="TEST",
            ts=datetime(2026, 1, 5, 14, 30, tzinfo=UTC) + timedelta(minutes=index * 5),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=0.0,
            timeframe="5m",
        )
        for index in range(80)
    ]

    result = adaptive_kernel_regression(bars)

    assert result["availability"]["state"] == "ready"
    assert result["events"] == []
    assert result["latest"]["state"] == "neutral"
    assert result["latest"]["center"] == 100.0
    assert result["latest"]["upper"] == 100.0
    assert result["latest"]["lower"] == 100.0
    assert result["latest"]["residual_sigma"] == 0.0


def test_appending_future_bars_does_not_change_prior_rows_or_events() -> None:
    closes = [100.0 + index * 0.04 + sin(index * 0.41) * 0.8 for index in range(110)]
    bars = _bars_from_closes(closes)
    params = AdaptiveKernelRegressionParams(render_bars=120)

    prefix = adaptive_kernel_regression(bars[:76], params=params)
    full = adaptive_kernel_regression(bars, params=params)

    assert [row for row in full["series"] if row["index"] < 76] == prefix["series"]
    assert [event for event in full["events"] if event["index"] < 76] == prefix["events"]


def test_regime_events_are_confirmed_state_transitions_not_repeat_crosses() -> None:
    closes = [100.0 + index * 0.1 for index in range(30)]
    closes.extend([110.0] * 5)
    closes.extend([100.0] * 5)
    closes.extend([90.0] * 10)

    result = adaptive_kernel_regression(_bars_from_closes(closes), params=_fixed_params())
    events = result["events"]

    assert [event["state"] for event in events] == ["bullish", "bearish"]
    assert [event["previous_state"] for event in events] == ["neutral", "bullish"]
    assert all(event["confirmed"] is True for event in events)
    assert result["signals"] == []
    assert result["latest"]["state"] == "bearish"
    labels = [
        overlay
        for overlay in result["overlays"]
        if overlay.get("role") == "kernel_regime_transition"
    ]
    assert [label["lines"] for label in labels] == [["SELL"]]
    assert labels[0]["type"] == "label"
    assert "label_font_size" not in labels[0]
    assert labels[0]["anchor_price_mode"] == "overlay"


def _entry_plan_transition_prefix(
    direction: str,
    params: AdaptiveKernelRegressionParams,
) -> tuple[list[Bar], dict[str, object]]:
    closes = [100.0 + index * 0.1 for index in range(30)]
    closes.extend([110.0] * 5)
    closes.extend([100.0] * 5)
    closes.extend([90.0] * 10)
    if direction == "long":
        closes.extend([115.0] * 16)
    bars = _bars_from_closes(closes)
    result = adaptive_kernel_regression(bars, params=params)
    event = next(
        event
        for event in reversed(result["events"])
        if event["direction"] == direction and event["previous_state"] != "neutral"
    )
    return bars[: int(event["index"]) + 1], event


@pytest.mark.parametrize(
    ("direction", "trigger_line", "confirmation"),
    (
        ("long", "upper", "close_above_trigger"),
        ("short", "ema20", "close_below_trigger"),
    ),
)
def test_enabled_entry_plan_waits_for_confirmed_reclaim_and_next_open(
    direction: str,
    trigger_line: str,
    confirmation: str,
) -> None:
    params = _fixed_params(entry_plan_enabled=True)
    bars, event = _entry_plan_transition_prefix(direction, params)
    signal_index = int(event["index"])
    if direction == "long":
        level = float(event["upper"])
        reclaim_bar = Bar(
            symbol="TEST",
            ts=bars[-1].ts + timedelta(minutes=5),
            open=level - 0.5,
            high=level + 2.0,
            low=level - 1.0,
            close=level + 1.5,
            volume=1000.0,
            timeframe="5m",
        )
    else:
        level = pine.ema_series([float(bar.close) for bar in bars], 20)[signal_index]
        reclaim_bar = Bar(
            symbol="TEST",
            ts=bars[-1].ts + timedelta(minutes=5),
            open=level + 0.5,
            high=level + 1.0,
            low=level - 2.0,
            close=level - 1.5,
            volume=1000.0,
            timeframe="5m",
        )

    ready = adaptive_kernel_regression([*bars, reclaim_bar], params=params)

    assert ready["plan"]["state"] == "ready_next_open"
    assert ready["plan"]["direction"] == direction
    assert ready["plan"]["trigger_line"] == trigger_line
    assert ready["plan"]["confirmation"] == confirmation
    assert ready["plan"]["entry_ready"] is True
    assert ready["plan"]["entry_price"] is None
    assert ready["plan"]["wait_minutes"] == 60
    assert ready["plan"]["advisory_only"] is True
    assert not any(
        overlay.get("role") == "kernel_regime_transition" for overlay in ready["overlays"]
    )
    plan_line = next(
        overlay
        for overlay in ready["overlays"]
        if overlay.get("role") == "kernel_entry_plan_trigger"
    )
    assert plan_line["type"] == "line"
    assert plan_line["state_code"] == "ready_next_open"
    assert plan_line["action"] == "ENTER_NEXT_OPEN"
    assert "trade_plan" not in plan_line

    entry_bar = Bar(
        symbol="TEST",
        ts=reclaim_bar.ts + timedelta(minutes=5),
        open=reclaim_bar.close + (0.2 if direction == "long" else -0.2),
        high=reclaim_bar.close + 0.5,
        low=reclaim_bar.close - 0.5,
        close=reclaim_bar.close,
        volume=1000.0,
        timeframe="5m",
    )
    triggered = adaptive_kernel_regression([*bars, reclaim_bar, entry_bar], params=params)

    assert triggered["plan"]["state"] == "triggered"
    assert triggered["plan"]["triggered"] is True
    assert triggered["plan"]["entry_ts"] == entry_bar.ts.isoformat()
    assert triggered["plan"]["entry_price"] == pytest.approx(entry_bar.open)
    assert not any(
        overlay.get("role") == "kernel_entry_plan_trigger" for overlay in triggered["overlays"]
    )
    assert not any(
        overlay.get("role") == "kernel_regime_transition" for overlay in triggered["overlays"]
    )


def test_entry_plan_expires_without_reclaim_and_does_not_create_executable_signal() -> None:
    params = _fixed_params(entry_plan_enabled=True)
    bars, _event = _entry_plan_transition_prefix("short", params)
    low_price = min(float(bars[-1].close), 80.0)
    for _index in range(13):
        bars.append(
            Bar(
                symbol="TEST",
                ts=bars[-1].ts + timedelta(minutes=5),
                open=low_price,
                high=low_price + 0.1,
                low=low_price - 0.1,
                close=low_price,
                volume=1000.0,
                timeframe="5m",
            )
        )

    result = adaptive_kernel_regression(bars, params=params)

    assert result["plan"]["state"] == "expired"
    assert result["plan"]["active"] is False
    assert result["plan"]["entry_ready"] is False
    assert result["signals"] == []
    assert not any("trade_plan" in overlay for overlay in result["overlays"])


def test_three_minute_entry_plan_uses_twenty_bar_expiry_window() -> None:
    params = _fixed_params(entry_plan_enabled=True)
    bars, _event = _entry_plan_transition_prefix("short", params)
    start = bars[0].ts
    three_minute_bars = [
        replace(
            bar,
            ts=start + timedelta(minutes=index * 3),
            timeframe="3m",
        )
        for index, bar in enumerate(bars)
    ]

    result = adaptive_kernel_regression(three_minute_bars, params=params)

    assert result["plan"]["state"] == "armed"
    assert result["plan"]["source_tf"] == "3m"
    assert result["plan"]["wait_bars"] == 20
    assert result["plan"]["wait_minutes"] == 60


def test_adaptive_bandwidth_expands_with_normalized_atr() -> None:
    calm = [100.0 + sin(index * 0.3) * 0.05 for index in range(80)]
    volatile = calm[:40] + [100.0 + sin(index * 1.7) * 4.0 for index in range(40, 80)]
    params = AdaptiveKernelRegressionParams()

    calm_result = adaptive_kernel_regression(_bars_from_closes(calm), params=params)
    volatile_result = adaptive_kernel_regression(_bars_from_closes(volatile), params=params)

    assert (
        volatile_result["latest"]["effective_bandwidth"]
        > calm_result["latest"]["effective_bandwidth"]
    )


def test_adaptive_bandwidth_uses_pine_seeded_atr_and_ema() -> None:
    closes = [100.0 + index * 0.13 + sin(index * 0.47) for index in range(48)]
    bars = _bars_from_closes(closes)
    params = _fixed_params(adaptive_bandwidth=True)

    result = adaptive_kernel_regression(bars, params=params)

    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        previous_close = bars[index - 1].close if index else bar.close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
                0.000001,
            )
        )
    atr = sum(true_ranges[: params.atr_len]) / params.atr_len
    factor = atr / bars[params.atr_len - 1].close
    alpha = 2.0 / (params.atr_len + 1.0)
    first_index = result["series"][0]["index"]
    for index in range(params.atr_len, first_index + 1):
        atr = (atr * (params.atr_len - 1) + true_ranges[index]) / params.atr_len
        normalized_atr = atr / bars[index].close
        factor = normalized_atr * alpha + factor * (1.0 - alpha)
    expected = params.base_bandwidth * (1.0 + factor * 200.0)

    assert result["series"][0]["effective_bandwidth"] == pytest.approx(
        expected,
        abs=1e-6,
    )


def test_indicator_calculates_directly_on_one_minute_bars() -> None:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    bars = [
        replace(bar, ts=start + timedelta(minutes=index), timeframe="1m")
        for index, bar in enumerate(
            _bars_from_closes(
                [100.0 + index * 0.03 + sin(index * 0.29) * 0.5 for index in range(90)]
            )
        )
    ]

    result = adaptive_kernel_regression(bars)

    assert result["availability"]["state"] == "ready"
    assert result["series"]
    assert result["latest"]["ts"] == bars[-1].ts.isoformat()

    planned = adaptive_kernel_regression(
        bars,
        params=AdaptiveKernelRegressionParams(entry_plan_enabled=True),
    )
    assert planned["plan"]["state"] == "unsupported_timeframe"
    assert planned["plan"]["reason_code"] == "entry_plan_supported_on_3m_5m"


def test_registered_runtime_contract_accepts_compact_custom_overlay() -> None:
    bars = _bars_from_closes(
        [100.0 + index * 0.06 + sin(index * 0.37) * 0.9 for index in range(180)]
    )
    params = AdaptiveKernelRegressionParams(render_bars=60)

    result = run_indicator(
        "adaptive_kernel_regression",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
        calculate=lambda: adaptive_kernel_regression(bars, params=params),
        params=params,
    )

    assert result["status"]["state_code"] in {"signal", "no_signal"}
    assert result["status"]["last_error"] is None
    assert {overlay["type"] for overlay in result["overlays"]} <= {
        "custom",
        "label",
        "table",
    }
    custom = [overlay for overlay in result["overlays"] if overlay["type"] == "custom"]
    assert len(custom) == 1
    assert custom[0]["renderer_ref"] == "adaptive_kernel_regression_series"
    assert custom[0]["payload"] == {
        "kind": "indicator_state_series_v1",
        "render_bars": 60,
    }
    dashboard = next(overlay for overlay in result["overlays"] if overlay["type"] == "table")
    assert dashboard["table"]["model_ref"] == "adaptive_kernel_regression"
    assert dashboard["table"]["model"] == {
        "state": result["latest"]["state"],
        "center": result["latest"]["center"],
        "upper": result["latest"]["upper"],
        "lower": result["latest"]["lower"],
        "residual_sigma": result["latest"]["residual_sigma"],
        "effective_bandwidth": result["latest"]["effective_bandwidth"],
        "adaptive_bandwidth": True,
        "plan": result["plan"],
    }
    assert result["plan"]["state"] == "disabled"
    assert len(result["overlays"]) <= params.max_events + 2
    assert result["signals"] == []


def test_provisional_mode_is_opt_in_and_emits_typed_preview_transition() -> None:
    closes = [120.0 - index * 0.12 + sin(index * 0.31) * 0.12 for index in range(90)]
    confirmed_bars = _bars_from_closes(closes)
    latest = replace(
        confirmed_bars[-1],
        ts=confirmed_bars[-1].ts + timedelta(minutes=5),
        open=confirmed_bars[-1].close,
        high=confirmed_bars[-1].close + 31.0,
        low=confirmed_bars[-1].close - 0.25,
        close=confirmed_bars[-1].close + 30.0,
        closed=False,
        state=None,
    )
    provisional_params = AdaptiveKernelRegressionParams(
        calculation_mode="provisional",
        entry_plan_enabled=True,
    )
    runtime_params = IndicatorRuntimeParams(
        enabled={"adaptive_kernel_regression": True},
        by_indicator={"adaptive_kernel_regression": provisional_params},
        errors={},
        strategy_mode=StrategyMode.BALANCED,
    )
    context = IndicatorRunContext(
        confirmed_bars=confirmed_bars,
        live_signal_bars=[*confirmed_bars, latest],
        latest=latest,
        analysis_latest=confirmed_bars[-1],
        runtime_params=runtime_params,
        indicator_params={},
        instrument_profile=PROFILES["ES"],
        features={},
        live_preview_active=True,
    )

    spec = build_execution_spec(context)

    assert spec.input_bars is confirmed_bars
    assert spec.mode == "confirmed"
    assert spec.preview_calculate is not None
    assert spec.preview_event_ts == latest.ts.isoformat()
    preview = spec.preview_calculate()
    assert preview["series"] == []
    assert preview["overlays"] == []
    assert preview["latest"]["ts"] == latest.ts.isoformat()
    assert preview["latest"]["state"] == "bullish"
    assert preview["events"][-1]["ts"] == latest.ts.isoformat()
    assert preview["events"][-1]["confirmed"] is False
    assert preview["plan"]["state"] == "preview_ignored"
    assert preview["plan"]["entry_ready"] is False

    indicator, candidates = execute_indicator_spec(
        spec,
        runner=run_pipeline_indicator,
        promoter=lambda _indicator_id, _indicator: [],
    )
    assert candidates == []
    assert indicator["preview"]["ts"] == latest.ts.isoformat()
    assert indicator["preview"]["confirmed"] is False
    assert indicator["preview_events"][-1]["ts"] == latest.ts.isoformat()
    assert indicator["preview_events"][-1]["confirmed"] is False
    assert indicator["status"]["state_code"] == "live_preview_signal"

    confirmed_context = replace(
        context,
        runtime_params=IndicatorRuntimeParams(
            enabled={"adaptive_kernel_regression": True},
            by_indicator={
                "adaptive_kernel_regression": AdaptiveKernelRegressionParams(
                    calculation_mode="confirmed",
                )
            },
            errors={},
            strategy_mode=StrategyMode.BALANCED,
        ),
    )
    assert build_execution_spec(confirmed_context).preview_calculate is None


def test_compact_transport_payload_stays_bounded_for_max_visual_history() -> None:
    bars = _bars_from_closes(
        [100.0 + index * 0.025 + sin(index * 0.23) * 1.4 for index in range(600)]
    )
    params = AdaptiveKernelRegressionParams(render_bars=240)
    result = run_indicator(
        "adaptive_kernel_regression",
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
        calculate=lambda: adaptive_kernel_regression(bars, params=params),
        params=params,
    )
    snapshot = {
        "indicators": {"adaptive_kernel_regression": result},
        "meta": {},
    }

    compact_runtime_payload_for_response(snapshot)
    compacted = snapshot["indicators"]["adaptive_kernel_regression"]

    assert len(compacted["series"]) == 240
    assert set(compacted["series"][-1]) == {
        "center",
        "index",
        "lower",
        "residual",
        "state",
        "ts",
        "upper",
    }
    assert len(compacted["overlays"]) <= 82
    assert len(json.dumps(compacted, separators=(",", ":")).encode()) < 100_000


def test_package_declares_state_series_without_local_canvas_rendering() -> None:
    client_path = (
        Path(__file__).parents[1]
        / "src/aef_terminal/indicators/modules/adaptive_kernel_regression/client.js"
    )
    source = client_path.read_text(encoding="utf-8")

    assert '"adaptive_kernel_regression_series"' in source
    assert "registerIndicatorCustomOverlayRenderer" in source
    assert 'registerIndicatorTableModel("adaptive_kernel_regression"' in source
    assert "registerIndicatorStateSeriesPresentation" in source
    assert "drawGenericIndicatorStateSeries" in source
    assert "indicatorStateSeriesOverlayFilter" in source
    assert 'hidden_when_setting_true: "entry_plan_enabled"' in source
    assert "registerIndicatorStateSeriesCandleProvider" in source
    assert "createLinearGradient" not in source
    assert "drawAdaptiveKernel" not in source
    assert ".ctx" not in source
    assert 'registerIndicatorAnalysisRefreshScheduler("adaptive_kernel_regression"' in source
    assert 'registerIndicatorStateSeriesCandleProvider(\n  "adaptive_kernel_regression"' in source
    assert 'registerIndicatorOverlayFilter("adaptive_kernel_regression_filter"' in source


def test_package_table_renderer_presents_typed_entry_plan(tmp_path: Path) -> None:
    source = Path(
        "src/aef_terminal/indicators/modules/adaptive_kernel_regression/client.js"
    ).read_text(encoding="utf-8")
    start = source.index("function adaptiveKernelConfig")
    end = source.index("\nregisterIndicatorStateSeriesPresentation", start)
    table_source = source[start:end]
    script = tmp_path / "adaptive-kernel-table-model.js"
    script.write_text(
        "\n".join(
            (
                "function registerIndicatorTableModel(_id, _renderer) {}",
                "const state = { indicators: { adaptiveKernelRegression: { entryPlan: false } } };",
                table_source,
                "const base = adaptiveKernelTableColumns({ model: { state: 'bullish', center: 101, upper: 102, lower: 100, residual_sigma: 1, effective_bandwidth: 8.5, adaptive_bandwidth: true, plan: { enabled: false } } });",
                "const stalePlanned = adaptiveKernelTableColumns({ model: { state: 'bearish', center: 101, upper: 102, lower: 100, residual_sigma: 1, effective_bandwidth: 8.5, adaptive_bandwidth: true, plan: { enabled: true, state: 'ready_next_open', direction: 'short', trigger_line: 'ema20', trigger_level: 100.5, entry_ready: true, wait_minutes: 60 } } });",
                "state.indicators.adaptiveKernelRegression.entryPlan = true;",
                "const pending = adaptiveKernelTableColumns({ model: { state: 'bullish', center: 101, upper: 102, lower: 100, residual_sigma: 1, effective_bandwidth: 8.5, adaptive_bandwidth: true, plan: { enabled: false } } });",
                "const planned = adaptiveKernelTableColumns({ model: { state: 'bearish', center: 101, upper: 102, lower: 100, residual_sigma: 1, effective_bandwidth: 8.5, adaptive_bandwidth: true, plan: { enabled: true, state: 'ready_next_open', direction: 'short', trigger_line: 'ema20', trigger_level: 100.5, entry_ready: true, wait_minutes: 60 } } });",
                "console.log(JSON.stringify({ base, stalePlanned, pending, planned }));",
            )
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert [column["cells"][0] for column in payload["base"]] == [
        "Signal",
        "Kernel MA",
        "Bands",
        "Band Width σ",
    ]
    assert [column["cells"][0] for column in payload["stalePlanned"]] == [
        "Signal",
        "Kernel MA",
        "Bands",
        "Band Width σ",
    ]
    assert [column["cells"][0] for column in payload["planned"]] == [
        "Signal",
        "Kernel MA",
        "Bands",
        "Band Width σ",
        "Plan",
        "Trigger",
    ]
    assert [column["cells"][0] for column in payload["pending"]] == [
        "Signal",
        "Kernel MA",
        "Bands",
        "Band Width σ",
        "Plan",
        "Trigger",
    ]
    assert payload["pending"][4]["cells"] == ["Plan", "REFRESHING", "PENDING"]
    assert payload["pending"][5]["cells"] == ["Trigger", "--", "WAIT"]
    assert payload["pending"][4]["tone"] == "warning"
    assert payload["pending"][5]["tone"] == "warning"
    assert payload["planned"][4]["cells"] == ["Plan", "READY NEXT OPEN", "SHORT"]
    assert payload["planned"][5]["cells"][1]["prefix"] == "EMA20 "
    assert payload["planned"][5]["cells"][2] == "NEXT OPEN"


def test_shared_candle_priority_and_gradient_hosts_remain_generic() -> None:
    root = Path(__file__).parents[1]
    shared_overlays = (root / "src/aef_terminal/ui/assets/js/49-overlays-indicators.js").read_text(
        encoding="utf-8"
    )
    price_renderer = (root / "src/aef_terminal/ui/assets/js/52-overlays-price-render.js").read_text(
        encoding="utf-8"
    )
    drawing_utils = (root / "src/aef_terminal/ui/assets/js/40-chart-drawing-utils.js").read_text(
        encoding="utf-8"
    )

    assert "function registerIndicatorCandleStyleProvider" in shared_overlays
    assert "function indicatorCandleStyleSeries" in shared_overlays
    assert "function registerIndicatorStateSeriesPresentation" in shared_overlays
    assert "function drawGenericIndicatorStateSeries" in shared_overlays
    assert "function indicatorStateSeriesOverlayFilter" in shared_overlays
    assert "function indicatorStateSeriesCandleStyles" in shared_overlays
    assert "indicatorStateSeriesThemePalettes" in shared_overlays
    assert '"Cyber Aqua"' in shared_overlays
    assert 'const explicitColor = String(item?.color || "").trim();' in shared_overlays
    assert "adaptive_kernel_regression" not in shared_overlays
    assert "adaptive_kernel_regression" not in price_renderer
    assert (
        "const basePaint = indicatorCandleStyles[i] || volumeCandlePaint(vsaItem, bar);"
        in price_renderer
    )
    assert "{ ...basePaint, provisional: true }" in price_renderer
    assert "function canvasLinearGradient" in drawing_utils
    assert "linearGradient: canvasLinearGradient" in shared_overlays


def test_shared_state_series_renderer_executes_modes_labels_and_candle_priority(
    tmp_path: Path,
) -> None:
    shared_source = Path("src/aef_terminal/ui/assets/js/49-overlays-indicators.js").read_text(
        encoding="utf-8"
    )
    state_series_source = shared_source.split(
        "const indicatorStateSeriesPresentationRegistry",
        1,
    )[1].split("function registerIndicatorCustomOverlayRenderer", 1)[0]
    script = tmp_path / "state-series-renderer.js"
    script.write_text(
        "\n".join(
            (
                "let group = { visualMode: 'bands', fill: true, theme: 'Classic', candles: true, textSize: 'medium', calculationMode: 'provisional' };",
                "function indicatorSpec(id) { return id ? { id } : null; }",
                "function indicatorStateForId() { return group; }",
                "function genericIndicatorOverlayFilter(item, values) { return values?.dashboard === false && item?.control_key === 'dashboard' ? [] : [item]; }",
                "function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }",
                "function rgbaFromCssColor(color, alpha) { return `${color}@${alpha}`; }",
                "function css() { return '#CCCCCC'; }",
                "function timestampKey(value) { return String(value || ''); }",
                "let gradientCalls = 0;",
                "function canvasLinearGradient() { gradientCalls += 1; return { gradient: true }; }",
                "function registerIndicatorCandleStyleProvider() {}",
                "const indicatorStateSeriesPresentationRegistry" + state_series_source,
                "const contract = {",
                "  kind: 'indicator_state_series_v1',",
                "  fields: { ts: 'ts', center: 'center', upper: 'upper', lower: 'lower', residual: 'residual', state: 'state' },",
                "  controls: { mode: 'visualMode', fill: 'fill', theme: 'theme', candles: 'candles', text_size: 'textSize', calculation_mode: 'calculationMode' },",
                "  modes: { bands: 'bands', single: 'single', trail: 'trail' },",
                "  initial_state: 'bearish', state_colors: { bullish: 'bull', bearish: 'bear', neutral: 'bear' },",
                "  line: { single_width: 2, band_width: 1, trail_width: 5, band_alpha: 0.4, trail_alpha: 0.4 },",
                "  fill_alpha: { bands: 0.2, single: 0.25, trail: 0.4 },",
                "  preview: { mode: 'provisional', line_alpha: 0.72, fill_alpha: 0.5, dash: [5, 4] },",
                "  labels: { role: 'kernel_regime_transition', hidden_when_setting_true: 'entry_plan_enabled' }, default_theme: 'Classic',",
                "};",
                "registerIndicatorStateSeriesPresentation('test_indicator', contract);",
                "const snapshot = { indicators: { test_indicator: {",
                "  series: [",
                "    { ts: 't0', center: 10, upper: 12, lower: 8, residual: 1, state: 'neutral' },",
                "    { ts: 't1', center: 11, upper: 13, lower: 9, residual: 1, state: 'bullish' },",
                "  ],",
                "  latest: { state: 'bullish' },",
                "  preview: { ts: 't2', center: 10, upper: 12, lower: 8, residual: -1, state: 'bearish', confirmed: false },",
                "  preview_events: [{ ts: 't2', state: 'bearish', previous_state: 'bullish', confirmed: false, center: 10, upper: 12, lower: 8, bar_high: 13, bar_low: 7, label_atr: 2, direction: 'short' }],",
                "  settings: { entry_plan_enabled: false },",
                "} } };",
                "const bars = [{ ts: 't0' }, { ts: 't1' }, { ts: 't2' }];",
                "const styles = indicatorStateSeriesCandleStyles('test_indicator', { snapshot, bars });",
                "const labelOverlay = indicatorStateSeriesOverlayFilter({ type: 'label', source: 'test_indicator', role: 'kernel_regime_transition', direction: 'long' }, group, snapshot)[0];",
                "const textScales = {};",
                "for (const textSize of ['small', 'medium', 'large']) { group.textSize = textSize; textScales[textSize] = indicatorStateSeriesOverlayFilter({ type: 'label', source: 'test_indicator', role: 'kernel_regime_transition', direction: 'long' }, group, snapshot)[0].marker_font_scale; }",
                "group.textSize = 'medium';",
                "const operations = { strokes: 0, fills: 0, labels: 0, dashed: 0, previewLabel: null };",
                "const ctx = { save() {}, restore() {}, beginPath() {}, rect() {}, clip() {}, setLineDash(value) { if (value.length) operations.dashed += 1; }, moveTo() {}, lineTo() {}, closePath() {}, stroke() { operations.strokes += 1; }, fill() { operations.fills += 1; } };",
                "const context = { item: { source: 'test_indicator' }, payload: { kind: 'indicator_state_series_v1', render_bars: 20 }, snapshot, bars, ctx, timeLookup: { byTs: new Map([['t0', 0], ['t1', 1], ['t2', 2]]) }, x: index => index * 10, y: value => value, xStep: 10, pad: { left: 0, right: 0, top: 0 }, width: 100, priceH: 100, primitives: { marker(_ctx, item) { operations.labels += 1; operations.previewLabel = item; } } };",
                "const modeOperations = {};",
                "for (const mode of ['bands', 'single', 'trail']) {",
                "  snapshot.indicators.test_indicator.series[0].state = mode === 'trail' ? 'bullish' : 'neutral';",
                "  group.visualMode = mode; operations.strokes = 0; operations.fills = 0; operations.dashed = 0; gradientCalls = 0;",
                "  drawGenericIndicatorStateSeries(context);",
                "  modeOperations[mode] = { strokes: operations.strokes, fills: operations.fills, gradients: gradientCalls, dashed: operations.dashed };",
                "}",
                "const visiblePreviewLabels = operations.labels;",
                "snapshot.indicators.test_indicator.settings.entry_plan_enabled = true;",
                "drawGenericIndicatorStateSeries(context);",
                "const plannedPreviewLabels = operations.labels;",
                "group.candles = false;",
                "const disabledStyles = indicatorStateSeriesCandleStyles('test_indicator', { snapshot, bars });",
                "console.log(JSON.stringify({ styles, disabledStyles, labelType: labelOverlay.type, labelLines: labelOverlay.lines, labelColor: labelOverlay.color, textScales, textLight: labelOverlay.marker_theme_text_light, textDark: labelOverlay.marker_theme_text_dark, modeOperations, visiblePreviewLabels, plannedPreviewLabels, previewLines: operations.previewLabel?.lines }));",
            )
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["styles"] == [
        {"color": "#B32AC3@1"},
        {"color": "#5CF0D7@1"},
        {"color": "#B32AC3@1"},
    ]
    assert payload["disabledStyles"] == []
    assert payload["labelType"] == "marker"
    assert payload["labelLines"] == ["▲", "𝐁𝐔𝐘"]
    assert payload["labelColor"] == "#5CF0D7@1"
    assert payload["textScales"] == {"small": 0.5, "medium": 0.6, "large": 0.7}
    assert payload["textLight"] == "#000000"
    assert payload["textDark"] == "#9CA3AF"
    assert payload["modeOperations"]["bands"]["strokes"] >= 2
    assert payload["modeOperations"]["bands"]["fills"] >= 1
    assert payload["modeOperations"]["single"]["gradients"] >= 1
    assert payload["modeOperations"]["single"]["dashed"] >= 1
    assert payload["modeOperations"]["trail"]["gradients"] >= 1
    assert payload["visiblePreviewLabels"] >= 1
    assert payload["plannedPreviewLabels"] == payload["visiblePreviewLabels"]
    assert payload["previewLines"] == ["▼", "𝐒𝐄𝐋𝐋?"]


def test_parameter_builder_clamps_untrusted_settings() -> None:
    params = build_params(
        {
            "calculation_mode": "unknown",
            "label_anchor": "unknown",
            "label_offset_mult": 99,
            "lookback_window": 2,
            "base_bandwidth": 999,
            "adaptive_bandwidth": "false",
            "entry_plan_enabled": "true",
            "atr_len": 1,
            "output_smoothing": 99,
            "band_multiplier": 0.1,
            "band_lookback": 999,
            "band_smoothing": 0,
            "render_bars": 999,
        },
        DEFAULT_INDICATOR_SETTINGS,
    )

    assert params == AdaptiveKernelRegressionParams(
        calculation_mode="confirmed",
        lookback_window=10,
        base_bandwidth=64.0,
        adaptive_bandwidth=False,
        atr_len=5,
        output_smoothing=20,
        band_multiplier=0.5,
        band_lookback=200,
        band_smoothing=1,
        entry_plan_enabled=True,
        label_anchor="main",
        label_offset_mult=3.0,
        render_bars=240,
    )
    assert (
        build_params(
            {"calculation_mode": "PROVISIONAL"},
            DEFAULT_INDICATOR_SETTINGS,
        ).calculation_mode
        == "provisional"
    )
    assert (
        build_params(
            {"label_anchor": "BANDS", "label_offset_mult": 0.25},
            DEFAULT_INDICATOR_SETTINGS,
        ).label_anchor
        == "bands"
    )
    assert (
        build_params(
            {"label_anchor": "High/Low"},
            DEFAULT_INDICATOR_SETTINGS,
        ).label_anchor
        == "main"
    )
