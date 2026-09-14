from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    calc_enabled,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.ml.candle_specialist import candle_nn_probabilities as _candle_nn_probabilities
from aef_terminal.ml.nn_regime_bias import nn_regime_bias
from aef_terminal.ml.structural_squeeze import structural_squeeze_bias as _structural_squeeze_bias
from aef_terminal.indicators.module_contract import IndicatorModule, IndicatorSpec
from aef_terminal.runtime.math_utils import clamp_float as _clamp_float
from aef_terminal.runtime.math_utils import clamp_int as _clamp_int
from aef_terminal.runtime.math_utils import (
    bool_param,
    log_returns,
    safe_float as _safe_float,
    sample_stdev,
)


MARTIN_CARLO_LATEST_FIELDS = (
    "anchors",
    "bias",
    "engine",
    "engine_reason_code",
    "forecast",
    "mode",
    "price",
    "requested_engine",
    "state",
    "summary",
    "ts",
)

_MODEL_ASSET_DIR = Path(__file__).with_name("models")
_CANDLE_SPECIALIST_MODEL_PATH = _MODEL_ASSET_DIR / "candle_specialist_model.json"
_STRUCTURAL_SQUEEZE_MODEL_PATH = _MODEL_ASSET_DIR / "structural_squeeze_model.json"
MARTIN_CARLO_OPTIONAL_CONTEXT = (
    "impulse_fib",
    "absorption_trap",
    "w5_structure",
    "wolfe_structure",
    "option_reversal",
)


def candle_nn_probabilities(bars: Sequence[Bar]) -> dict[str, Any] | None:
    return _candle_nn_probabilities(
        bars,
        model_path=str(_CANDLE_SPECIALIST_MODEL_PATH),
    )


def structural_squeeze_bias(
    bars: Sequence[Bar],
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    return _structural_squeeze_bias(
        bars,
        enabled=enabled,
        model_path=str(_STRUCTURAL_SQUEEZE_MODEL_PATH),
    )


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "martin_carlo",
        "MartinCarlo",
        "Monte Carlo forecast heatmap, percentile fan and expected move distribution",
        {"version": "0.1-python", "series": [], "events": [], "latest": None, "overlays": []},
        "martinCarlo",
        "aef_terminal.indicators.modules.martin_carlo:martin_carlo",
        group="post_decision",
        pipeline_stage="post_decision",
        pipeline_order=140,
        candidate_promoter="none",
        custom_renderer=True,
        renderer_kind="custom",
        renderer_ref="martin_carlo_forecast",
        renderer_primitives=("custom",),
        renderer_placements=("price",),
        custom_renderer_requirements=("price_overlay_provider", "forecast_payload"),
        runtime_payload_contract={
            "latest": MARTIN_CARLO_LATEST_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        paper_tradable=False,
        optional_context=MARTIN_CARLO_OPTIONAL_CONTEXT,
        score_family="forecast",
        empirical_power=0.80,
        usefulness=0.85,
        module_type="visual",
        state_key="martinCarlo",
        calc_key="martinCarloCalcEnabled",
        visible_key="martinCarloVisible",
        chart_control_id="martin-carlo-toggle",
        process_control_id="martin-carlo-process",
        process_effect_ref="martin_carlo_refresh_reset",
        api_enabled_key="martin_carlo_enabled",
        api_visible_key="martin_carlo_visible",
        manager_order=140,
        default_calc=False,
        default_visible=False,
        show_in_runtime=False,
        controls=(
            _control(
                "mode",
                "Mode",
                "select",
                "both",
                "martinCarloMode",
                element_id="martin-carlo-mode",
                api_key="martin_carlo_mode",
                param_key="mode",
                options=("heatmap", "fan", "both"),
                action="load_apply",
            ),
            _control(
                "refresh",
                "Refresh",
                "select",
                "closed",
                "martinCarloRefresh",
                element_id="martin-carlo-refresh",
                options=("closed", "context", "frozen"),
                action="load_apply",
            ),
            _control(
                "engine",
                "Engine",
                "select",
                "auto",
                "martinCarloEngine",
                element_id="martin-carlo-engine",
                api_key="martin_carlo_engine",
                param_key="engine",
                options=("auto", "mean_reversion", "gbm", "returns"),
                action="load_apply",
            ),
            _control(
                "regimeFilter",
                "Regime",
                "toggle",
                True,
                "martinCarloRegimeFilter",
                element_id="martin-carlo-regime-filter",
                api_key="martin_carlo_regime_filter",
                param_key="regime_filter",
                action="load_apply",
            ),
            _control(
                "nnBias",
                "NN",
                "toggle",
                True,
                "martinCarloNnBias",
                element_id="martin-carlo-nn-bias",
                api_key="martin_carlo_nn_bias",
                param_key="nn_bias",
                action="load_apply",
            ),
            _control(
                "structuralBias",
                "Structure",
                "toggle",
                True,
                "martinCarloStructuralBias",
                element_id="martin-carlo-structural-bias",
                api_key="martin_carlo_structural_bias",
                param_key="structural_bias",
                action="load_apply",
            ),
            _control(
                "impulseBias",
                "Impulse",
                "toggle",
                True,
                "martinCarloImpulseBias",
                element_id="martin-carlo-impulse-bias",
                api_key="martin_carlo_impulse_bias",
                param_key="impulse_bias",
                action="load_apply",
            ),
            _control(
                "optionBias",
                "Options",
                "toggle",
                True,
                "martinCarloOptionBias",
                element_id="martin-carlo-option-bias",
                api_key="martin_carlo_option_bias",
                param_key="option_bias",
                action="load_apply",
            ),
            _control(
                "densityLines",
                "Density",
                "toggle",
                True,
                "martinCarloDensityLines",
                element_id="martin-carlo-density-lines",
                action="render",
            ),
            _control(
                "chartLabels",
                "Labels",
                "toggle",
                True,
                "martinCarloChartLabels",
                element_id="martin-carlo-chart-labels",
                action="render",
            ),
            _control(
                "lookback",
                "Lookback",
                "number",
                80,
                "martinCarloLookback",
                element_id="martin-carlo-lookback",
                api_key="martin_carlo_lookback",
                param_key="lookback",
                minimum=20,
                maximum=240,
                step=5,
                action="load_apply",
            ),
            _control(
                "horizon",
                "Horizon",
                "number",
                24,
                "martinCarloHorizon",
                element_id="martin-carlo-horizon",
                api_key="martin_carlo_horizon",
                param_key="horizon",
                minimum=5,
                maximum=80,
                step=1,
                action="load_apply",
            ),
            _control(
                "simulations",
                "Sims",
                "number",
                120,
                "martinCarloSimulations",
                element_id="martin-carlo-simulations",
                api_key="martin_carlo_simulations",
                param_key="simulations",
                minimum=30,
                maximum=300,
                step=10,
                action="load_apply",
            ),
            _control(
                "bins",
                "Bins",
                "number",
                12,
                "martinCarloBins",
                element_id="martin-carlo-bins",
                api_key="martin_carlo_bins",
                param_key="price_bins",
                minimum=6,
                maximum=24,
                step=1,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.martin_carlo:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.martin_carlo:build_params",
    ui_js_assets=("client.js",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = martin_carlo
    params = ctx.params("martin_carlo")
    earlier_context = {
        indicator_id: payload
        for indicator_id in MARTIN_CARLO_OPTIONAL_CONTEXT[:-1]
        if calc_enabled(ctx, indicator_id)
        and isinstance((payload := ctx.indicator_bundle.get(indicator_id)), dict)
    }
    return IndicatorExecutionSpec(
        id="martin_carlo",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate_with_context=lambda deps: calculate(
            ctx.confirmed_bars,
            params=params,
            impulse_context=earlier_context.get("impulse_fib"),
            option_context=deps.get("option_reversal"),
            decision_context=ctx.decision,
            indicator_context={**earlier_context, **dict(deps)},
            vsa_context=ctx.shared.get("vsa_volume"),
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        promote=False,
        depends_on=("option_reversal",) if calc_enabled(ctx, "option_reversal") else (),
    )


@dataclass(frozen=True)
class MartinCarloParams:
    mode: str = "both"
    engine: str = "auto"
    lookback: int = 80
    horizon: int = 24
    simulations: int = 120
    price_bins: int = 12
    paths: int = 18
    mean_length: int = 120
    include_drift: bool = True
    candle_bias: bool = True
    nn_bias: bool = True
    structural_bias: bool = True
    regime_filter: bool = True
    mean_reversion_strength: float = 0.15
    candle_bias_weight: float = 0.35
    candle_bias_min_confidence: float = 0.75
    nn_bias_weight: float = 0.20
    nn_vol_weight: float = 0.22
    structural_bias_weight: float = 0.28
    structural_vol_weight: float = 0.70
    structural_mean_reversion_weight: float = 0.35
    neutral_vol_weight: float = 0.18
    returns_drift_mix: float = 0.28
    impulse_bias: bool = True
    impulse_bias_weight: float = 0.22
    impulse_vol_weight: float = 0.18
    option_bias: bool = True
    option_drift_decay: float = 0.35
    regime_high_vol_ratio: float = 1.25
    regime_low_vol_ratio: float = 0.75
    mean_pull_vol_cap: float = 1.5
    compute_forecast: bool = True


def build_params(
    raw: Mapping[str, Any],
    _defaults: IndicatorDefaults,
) -> MartinCarloParams:
    return MartinCarloParams(
        mode=str(raw.get("mode") or MartinCarloParams.mode),
        engine=str(raw.get("engine") or MartinCarloParams.engine),
        lookback=int(raw.get("lookback", MartinCarloParams.lookback)),
        horizon=int(raw.get("horizon", MartinCarloParams.horizon)),
        simulations=int(raw.get("simulations", MartinCarloParams.simulations)),
        price_bins=int(raw.get("price_bins", MartinCarloParams.price_bins)),
        paths=int(raw.get("paths", MartinCarloParams.paths)),
        mean_length=int(raw.get("mean_length", MartinCarloParams.mean_length)),
        include_drift=bool_param(raw.get("include_drift"), True),
        nn_bias=bool_param(raw.get("nn_bias"), True),
        nn_bias_weight=float(raw.get("nn_bias_weight", MartinCarloParams.nn_bias_weight)),
        nn_vol_weight=float(raw.get("nn_vol_weight", MartinCarloParams.nn_vol_weight)),
        structural_bias=bool_param(raw.get("structural_bias"), True),
        structural_bias_weight=float(
            raw.get(
                "structural_bias_weight",
                MartinCarloParams.structural_bias_weight,
            )
        ),
        structural_vol_weight=float(
            raw.get(
                "structural_vol_weight",
                MartinCarloParams.structural_vol_weight,
            )
        ),
        regime_filter=bool_param(raw.get("regime_filter"), True),
        impulse_bias=bool_param(raw.get("impulse_bias"), True),
        impulse_bias_weight=float(
            raw.get(
                "impulse_bias_weight",
                MartinCarloParams.impulse_bias_weight,
            )
        ),
        impulse_vol_weight=float(
            raw.get(
                "impulse_vol_weight",
                MartinCarloParams.impulse_vol_weight,
            )
        ),
        option_bias=bool_param(raw.get("option_bias"), True),
        option_drift_decay=float(
            raw.get(
                "option_drift_decay",
                MartinCarloParams.option_drift_decay,
            )
        ),
        compute_forecast=bool_param(raw.get("visible"), True),
    )


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = max(0.0, min(float(q), 1.0)) * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    weight = pos - lo
    return sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight


def _percentile_values(
    sorted_values: Sequence[float], q_map: dict[str, float]
) -> dict[str, float | None]:
    return {key: _percentile(sorted_values, q) for key, q in q_map.items()}


def _ema(values: Sequence[float], length: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (max(1, int(length)) + 1.0)
    out = float(values[0])
    for value in values[1:]:
        out = alpha * float(value) + (1.0 - alpha) * out
    return out


def _scenario_probability(
    path: Sequence[float], terminal_prices: Sequence[float], width: float
) -> float:
    if not path or not terminal_prices or width <= 0:
        return 0.0
    terminal = path[-1]
    nearby = sum(1 for price in terminal_prices if abs(price - terminal) <= width)
    return nearby / max(len(terminal_prices), 1)


def _candle_bias(
    bars: Sequence[Bar], enabled: bool, min_confidence: float = 0.75
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}
    try:
        probs = candle_nn_probabilities(bars)
    except Exception:
        return {"enabled": False, "error": "candle_nn_failed"}
    if not isinstance(probs, dict):
        return {"enabled": False}
    bull = _safe_float(probs.get("prob_bull"))
    bear = _safe_float(probs.get("prob_bear"))
    neutral = _safe_float(probs.get("prob_neutral"))
    edge = _safe_float(probs.get("edge"), (bull - bear) * 100.0)
    confidence = max(bull, bear)
    threshold = max(0.0, min(float(min_confidence), 1.0))
    gated_payload = {
        "prob_bull": round(bull, 6),
        "prob_bear": round(bear, 6),
        "prob_neutral": round(neutral, 6),
        "confidence": round(confidence, 6),
        "confidence_threshold": round(threshold, 6),
        "edge": round(edge, 4),
        "model_source": probs.get("model_source") or "",
        "model_path": probs.get("model_path") or "",
    }
    if confidence < threshold:
        return {
            "enabled": False,
            "reason_code": "confidence_gate",
            **gated_payload,
        }
    return {
        "enabled": True,
        **gated_payload,
    }


def _impulse_context_bias(impulse_context: dict[str, Any] | None, enabled: bool) -> dict[str, Any]:
    if not enabled or not isinstance(impulse_context, dict):
        return {"enabled": False}
    latest = (
        impulse_context.get("latest") if isinstance(impulse_context.get("latest"), dict) else None
    )
    if not latest:
        return {"enabled": False}
    signal = latest.get("signal") if isinstance(latest.get("signal"), dict) else None
    if not signal:
        return {"enabled": False, "reason_code": "current_signal_unavailable"}
    action = str(signal.get("action") or "WAIT").upper()
    if (
        signal.get("blocked") is True
        or signal.get("obsolete") is True
        or action in {"WAIT", "BLOCK"}
    ):
        return {"enabled": False, "reason_code": "current_signal_inactive"}
    direction = str(signal.get("direction") or "flat").lower()
    if direction not in {"long", "short"}:
        return {"enabled": False}
    score = _safe_float(signal.get("score"), _safe_float(latest.get("score")))
    important = bool(latest.get("important") or signal.get("important")) or score >= 70.0
    if not important:
        return {
            "enabled": False,
            "direction": direction,
            "score": round(score, 2),
            "reason_code": "weak_impulse",
        }
    exhaustion = bool(latest.get("exhaustion_risk"))
    raw_strength = max(0.0, min((score - 60.0) / 35.0, 1.0))
    strength = raw_strength * (0.45 if exhaustion else 1.0)
    sign = 1.0 if direction == "long" else -1.0
    return {
        "enabled": True,
        "direction": direction,
        "score": round(score, 2),
        "strength": round(strength, 4),
        "sign": sign,
        "exhaustion_risk": exhaustion,
        "event": latest.get("event") or latest.get("label") or "",
        "entry": latest.get("entry"),
        "target": latest.get("target"),
        "stop": latest.get("stop"),
    }


def _option_context_bias(option_context: dict[str, Any] | None, enabled: bool) -> dict[str, Any]:
    if not enabled or not isinstance(option_context, dict):
        return {"enabled": False}
    latest = (
        option_context.get("latest")
        if isinstance(option_context.get("latest"), dict)
        else option_context
    )
    bridge = (
        latest.get("monte_carlo_bridge")
        if isinstance(latest.get("monte_carlo_bridge"), dict)
        else {}
    )
    if not bridge.get("trigger_simulation"):
        return {"enabled": False}
    bias_settings = (
        bridge.get("bias_settings") if isinstance(bridge.get("bias_settings"), dict) else {}
    )
    pine_bias = _safe_float(bias_settings.get("pine_bias"))
    vol_multiplier = max(0.60, min(_safe_float(bias_settings.get("vol_multiplier"), 1.0), 2.50))
    return {
        "enabled": True,
        "state": latest.get("state"),
        "action": latest.get("action"),
        "direction": latest.get("direction"),
        "option_side": latest.get("option_side"),
        "score": latest.get("score"),
        "trigger_simulation": bool(bridge.get("trigger_simulation")),
        "recommended_engine": str(bridge.get("recommended_engine") or "").lower(),
        "pine_bias": pine_bias,
        "vol_multiplier": vol_multiplier,
        "target_attractor": bias_settings.get("target_attractor"),
        "status": bridge.get("status") or "advisory",
    }


def _regime_signature(
    closes: Sequence[float],
    returns: Sequence[float],
    index: int,
    ema_value: float,
    high_vol_ratio: float,
    low_vol_ratio: float,
) -> tuple[int, int, int]:
    ret = returns[index - 1] if index > 0 and index - 1 < len(returns) else 0.0
    direction = 1 if ret > 0 else -1 if ret < 0 else 0
    price = closes[index]
    trend = 1 if price >= ema_value else -1
    local = returns[max(0, index - 10) : index]
    local_vol = sample_stdev(local)
    all_vol = sample_stdev(returns[:index] or returns)
    if local_vol > all_vol * high_vol_ratio:
        vol_bucket = 1
    elif local_vol < all_vol * low_vol_ratio:
        vol_bucket = -1
    else:
        vol_bucket = 0
    return direction, trend, vol_bucket


def _regime_filtered_returns(
    closes: Sequence[float],
    returns: Sequence[float],
    mean_length: int,
    enabled: bool,
    high_vol_ratio: float = 1.25,
    low_vol_ratio: float = 0.75,
) -> tuple[list[float], dict[str, Any]]:
    if not enabled or len(returns) < 30:
        return list(returns), {"enabled": False, "sample_size": len(returns), "total": len(returns)}
    ema_values: list[float] = []
    alpha = 2.0 / (max(1, int(mean_length)) + 1.0)
    ema = closes[0]
    for price in closes:
        ema = alpha * price + (1.0 - alpha) * ema
        ema_values.append(ema)
    latest_sig = _regime_signature(
        closes,
        returns,
        len(closes) - 1,
        ema_values[-1],
        high_vol_ratio,
        low_vol_ratio,
    )
    strict: list[float] = []
    loose: list[float] = []
    for idx in range(3, len(closes) - 1):
        sig = _regime_signature(
            closes, returns, idx, ema_values[idx], high_vol_ratio, low_vol_ratio
        )
        value = returns[idx]
        if sig == latest_sig:
            strict.append(value)
        if sig[1] == latest_sig[1] and sig[2] == latest_sig[2]:
            loose.append(value)
    min_sample = max(12, min(40, len(returns) // 4))
    if len(strict) >= min_sample:
        selected = strict
        strength = "strict"
    elif len(loose) >= min_sample:
        selected = loose
        strength = "loose"
    else:
        selected = list(returns)
        strength = "full_sample"
    return selected, {
        "enabled": True,
        "signature": {
            "direction": latest_sig[0],
            "trend": latest_sig[1],
            "vol_bucket": latest_sig[2],
        },
        "strength": strength,
        "sample_size": len(selected),
        "strict_matches": len(strict),
        "loose_matches": len(loose),
        "total": len(returns),
    }


def _auto_engine(
    requested: str,
    *,
    neutral_prob: float,
    bias_edge: float,
    mean_value: float,
    start_price: float,
    vol: float,
    regime_sample_size: int,
) -> tuple[str, str]:
    if requested != "auto":
        return requested, "manual"
    stretch = (
        abs(math.log(mean_value / start_price)) / max(vol, 0.000001)
        if mean_value > 0 and start_price > 0
        else 0.0
    )
    if neutral_prob >= 0.42 or stretch >= 1.35:
        return "mean_reversion", "neutral_or_stretched"
    if abs(bias_edge) >= 0.24 and regime_sample_size >= 18:
        return "returns", "directional_candle_bias_with_regime_sample"
    if regime_sample_size >= 24:
        return "returns", "matched_regime_bootstrap"
    return "gbm", "parametric_default"


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _direction_value(value: Any) -> str:
    text = _enum_value(value)
    if text in {"long", "short", "flat"}:
        return text
    return ""


def _decision_traffic_engine(decision_context: Any | None) -> dict[str, Any]:
    if decision_context is None:
        return {"enabled": False}
    action = _enum_value(getattr(decision_context, "action", "")).upper()
    kind = _enum_value(getattr(decision_context, "kind", ""))
    direction = _direction_value(getattr(decision_context, "direction", ""))
    confidence = _safe_float(getattr(decision_context, "confidence", 0.0), 0.0)
    if action in {"BLOCK", "WAIT"} or direction in {
        "",
        "flat",
    }:
        return {
            "enabled": False,
            "action": action,
            "kind": kind,
            "direction": direction or "flat",
            "confidence": round(confidence, 4),
            "reason_code": "traffic_light:neutral",
        }
    if kind == "transit" or action in {"GO", "FOLLOW", "ARM", "WATCH"}:
        return {
            "enabled": True,
            "engine": "returns" if action in {"GO", "FOLLOW", "ARM"} else "gbm",
            "phase": "continuation",
            "direction": direction,
            "action": action,
            "kind": kind,
            "confidence": round(confidence, 4),
            "reason_code": "traffic_light:continuation",
        }
    if kind == "fade":
        return {
            "enabled": True,
            "engine": "mean_reversion",
            "phase": "reversal",
            "direction": direction,
            "action": action,
            "kind": kind,
            "confidence": round(confidence, 4),
            "reason_code": "traffic_light:fade",
        }
    return {
        "enabled": False,
        "action": action,
        "kind": kind,
        "direction": direction,
        "confidence": round(confidence, 4),
    }


def _latest_indicator(context: dict[str, Any] | None, indicator_id: str) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    latest = context.get(indicator_id, {}).get("latest")
    return latest if isinstance(latest, dict) else {}


def _reversal_warning_override(
    *,
    traffic: dict[str, Any],
    indicator_context: dict[str, Any] | None,
    vsa_context: dict[str, Any] | None,
    structural_bias: dict[str, Any],
    option_bias: dict[str, Any],
    neural_bias: dict[str, Any],
) -> dict[str, Any]:
    if not traffic.get("enabled") or traffic.get("phase") != "continuation":
        return {"enabled": False, "score": 0.0, "items": []}
    direction = str(traffic.get("direction") or "").lower()
    if direction not in {"long", "short"}:
        return {"enabled": False, "score": 0.0, "items": []}
    opposite = "short" if direction == "long" else "long"
    items: list[str] = []
    score = 0.0

    structural_mr = _safe_float(structural_bias.get("mean_reversion"), 0.0)
    breakout_expected = _safe_float(
        structural_bias.get("breakout_up" if direction == "long" else "breakout_down"), 0.0
    )
    if (
        structural_bias.get("enabled")
        and structural_mr >= 0.58
        and structural_mr >= breakout_expected + 0.12
    ):
        score += min(38.0, structural_mr * 48.0)
        items.append("structural_mean_reversion")

    if option_bias.get("enabled") and bool(option_bias.get("trigger_simulation")):
        option_engine = str(option_bias.get("recommended_engine") or "").lower()
        option_direction = str(option_bias.get("direction") or "").lower()
        option_side = str(option_bias.get("option_side") or "").upper()
        option_score = _safe_float(option_bias.get("score"), 0.0)
        if (
            option_engine == "mean_reversion"
            or option_direction in {"long", "short"}
            or option_side in {"CALL", "PUT"}
        ):
            score += min(28.0, max(option_score - 55.0, 0.0) * 0.75)
            items.append("option_reversal")

    w5 = _latest_indicator(indicator_context, "w5_structure")
    w5_dir = str(w5.get("direction") or "").lower()
    w5_action = str(w5.get("action") or w5.get("trigger_state") or "").upper()
    if (
        w5_dir == opposite
        and _safe_float(w5.get("score"), 0.0) >= 58.0
        and w5_action in {"CANDIDATE", "WATCH", "ARM", "GO", "ARMED"}
    ):
        score += 24.0
        items.append("w5_exhaustion")

    wolfe = _latest_indicator(indicator_context, "wolfe_structure")
    wolfe_dir = str(wolfe.get("direction") or "").lower()
    if wolfe_dir == opposite and _safe_float(wolfe.get("score"), 0.0) >= 58.0:
        score += 18.0
        items.append("wolfe_reversal")

    absorption = _latest_indicator(indicator_context, "absorption_trap")
    absorption_dir = str(absorption.get("direction") or "").lower()
    absorption_action = str(absorption.get("action") or absorption.get("state") or "").upper()
    if (
        absorption_dir == opposite
        and _safe_float(absorption.get("score"), 0.0) >= 70.0
        and absorption_action in {"WATCH", "ARM", "GO", "BUY", "SELL"}
    ):
        score += 20.0
        items.append("absorption_reclaim")

    vsa = (
        vsa_context.get("latest")
        if isinstance(vsa_context, dict) and isinstance(vsa_context.get("latest"), dict)
        else {}
    )
    vsa_dir = str(vsa.get("direction") or "").lower()
    vsa_code = str(vsa.get("code") or vsa.get("vsa_role") or "").upper()
    if vsa_dir == opposite and any(
        token in vsa_code for token in {"EXH", "UPTHRUST", "SPRING", "FUEL", "ABS"}
    ):
        score += 18.0
        items.append("vsa_exhaustion")

    nn_mr = _safe_float(neural_bias.get("mean_reversion"), 0.0)
    nn_cont = _safe_float(
        neural_bias.get("bull_continuation" if direction == "long" else "bear_continuation"), 0.0
    )
    if neural_bias.get("enabled") and nn_mr >= 0.62 and nn_mr >= nn_cont + 0.16:
        score += min(20.0, nn_mr * 26.0)
        items.append("nn_mean_reversion")

    return {
        "enabled": score >= 42.0,
        "score": round(score, 2),
        "items": items,
        "direction": opposite if score >= 42.0 else "",
    }


def _round_price(value: float) -> float:
    if abs(value) >= 1000:
        return round(value, 2)
    if abs(value) >= 10:
        return round(value, 4)
    return round(value, 6)


def _advisory_zone(start_price: float, terminal_stats: dict[str, float]) -> dict[str, Any]:
    p05 = terminal_stats.get("p05", start_price)
    p16 = terminal_stats.get("p16", start_price)
    p50 = terminal_stats.get("p50", start_price)
    p84 = terminal_stats.get("p84", start_price)
    p95 = terminal_stats.get("p95", start_price)
    if start_price <= p05:
        zone = "LOW_TAIL"
        note = "Не ловить нож. Long только после реакции: absorption/spring/reclaim/volume fade."
        target_note = "Ближайшая реалистичная цель: p16-p50; верхний хвост не базовый target."
    elif start_price < p16:
        zone = "LOW_EDGE"
        note = "Искать long setup по подтверждению; без реакции это не вход."
        target_note = "Консервативная цель: p50; частичная фиксация внутри p16-p50."
    elif start_price <= p84:
        zone = "MID"
        note = "Нет edge от границ. Лучше ждать подхода к зоне или отдельный сигнал."
        target_note = "Цели внутри p16-p84 статистически реалистичнее хвостов."
    elif start_price < p95:
        zone = "HIGH_EDGE"
        note = "Искать take-profit/fade setup по подтверждению; не шортить вслепую."
        target_note = "Консервативная цель вниз: p50; хвост выше может быть добит импульсом."
    else:
        zone = "HIGH_TAIL"
        note = "Зона фиксации/ожидания exhaustion. Short только после реакции."
        target_note = "Ближайшая реалистичная цель: p84-p50; не считать разворот гарантированным."
    return {
        "zone": zone,
        "note": note,
        "target_note": target_note,
        "levels": {
            "p05": _round_price(p05),
            "p16": _round_price(p16),
            "p50": _round_price(p50),
            "p84": _round_price(p84),
            "p95": _round_price(p95),
        },
    }


def _impulse_target_arrow(
    *,
    start_price: float,
    terminal_stats: dict[str, float],
    impulse_bias: dict[str, Any],
    neural_bias: dict[str, Any],
    structural_bias: dict[str, Any],
) -> dict[str, Any] | None:
    if not impulse_bias.get("enabled"):
        return None
    direction = str(impulse_bias.get("direction") or "").lower()
    if direction not in {"long", "short"}:
        return None
    impulse_score = _safe_float(impulse_bias.get("score"), 0.0)
    if impulse_score < 75.0:
        return None

    expected_structural = "breakout_up" if direction == "long" else "breakout_down"
    expected_nn = "bull_continuation" if direction == "long" else "bear_continuation"
    structural_prob = _safe_float(structural_bias.get(expected_structural), 0.0)
    structural_match = (
        bool(structural_bias.get("enabled"))
        and str(structural_bias.get("dominant") or "") == expected_structural
        and structural_prob >= 0.50
    )
    nn_prob = _safe_float(neural_bias.get(expected_nn), 0.0)
    nn_match = (
        bool(neural_bias.get("enabled"))
        and str(neural_bias.get("dominant") or "") == expected_nn
        and nn_prob >= 0.50
    )
    if not structural_match and not nn_match:
        return None

    target = terminal_stats.get("p84") if direction == "long" else terminal_stats.get("p16")
    target = _safe_float(target, start_price)
    if direction == "long" and target <= start_price:
        return None
    if direction == "short" and target >= start_price:
        return None
    confidence = max(structural_prob if structural_match else 0.0, nn_prob if nn_match else 0.0)
    return {
        "enabled": True,
        "direction": direction,
        "start_price": _round_price(start_price),
        "target_price": _round_price(target),
        "confidence": round(confidence, 4),
        "impulse_score": round(impulse_score, 2),
        "source": "structural_squeeze" if structural_match else "nn_regime",
        "label": "MC IMP",
    }


def martin_carlo(
    bars: Sequence[Bar],
    params: MartinCarloParams | None = None,
    impulse_context: dict[str, Any] | None = None,
    option_context: dict[str, Any] | None = None,
    decision_context: Any | None = None,
    indicator_context: dict[str, Any] | None = None,
    vsa_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = params or MartinCarloParams()
    closed = [bar for bar in bars if bar.closed]
    min_bars = max(20, min(params.lookback, 40))
    if len(closed) < min_bars:
        return {
            "version": "0.1-python",
            "latest": None,
            "events": [],
            "overlays": [],
            "availability": {
                "state": "blocked",
                "reason_code": "martin_carlo_warmup",
                "required_bars": min_bars,
                "available_bars": len(closed),
            },
        }

    lookback = _clamp_int(params.lookback, 80, 20, 240)
    horizon = _clamp_int(params.horizon, 24, 5, 80)
    simulations = _clamp_int(params.simulations, 120, 30, 300)
    price_bins = _clamp_int(params.price_bins, 12, 6, 24)
    path_count = _clamp_int(params.paths, 18, 4, min(40, simulations))
    mean_length = _clamp_int(params.mean_length, 120, 20, 300)
    mean_reversion_strength = _clamp_float(params.mean_reversion_strength, 0.15, 0.01, 0.50)
    candle_bias_weight = _clamp_float(params.candle_bias_weight, 0.35, 0.0, 1.0)
    candle_bias_min_confidence = _clamp_float(params.candle_bias_min_confidence, 0.75, 0.0, 1.0)
    nn_bias_weight = _clamp_float(params.nn_bias_weight, 0.20, 0.0, 1.0)
    nn_vol_weight = _clamp_float(params.nn_vol_weight, 0.22, 0.0, 1.0)
    structural_bias_weight = _clamp_float(params.structural_bias_weight, 0.28, 0.0, 1.0)
    structural_vol_weight = _clamp_float(params.structural_vol_weight, 0.70, 0.0, 1.0)
    structural_mean_reversion_weight = _clamp_float(
        params.structural_mean_reversion_weight, 0.35, 0.0, 1.0
    )
    neutral_vol_weight = _clamp_float(params.neutral_vol_weight, 0.18, 0.0, 0.60)
    returns_drift_mix = _clamp_float(params.returns_drift_mix, 0.28, 0.0, 0.80)
    impulse_bias_weight = _clamp_float(params.impulse_bias_weight, 0.22, 0.0, 1.0)
    impulse_vol_weight = _clamp_float(params.impulse_vol_weight, 0.18, 0.0, 0.80)
    option_drift_decay = _clamp_float(params.option_drift_decay, 0.35, 0.0, 1.0)
    regime_high_vol_ratio = _clamp_float(params.regime_high_vol_ratio, 1.25, 1.01, 3.0)
    regime_low_vol_ratio = _clamp_float(params.regime_low_vol_ratio, 0.75, 0.10, 0.99)
    mean_pull_vol_cap = _clamp_float(params.mean_pull_vol_cap, 1.5, 0.1, 5.0)
    mode = str(params.mode or "both").strip().lower()
    if mode not in {"heatmap", "fan", "both"}:
        mode = "both"
    engine = str(params.engine or "mean_reversion").strip().lower()
    if engine not in {"auto", "returns", "mean_reversion", "gbm"}:
        engine = "auto"

    sample = closed[-lookback:]
    closes = [_safe_float(bar.close) for bar in sample]
    latest = sample[-1]
    start_price = _safe_float(latest.close)
    if any(price <= 0 for price in closes):
        return {
            "version": "0.1-python",
            "latest": None,
            "events": [],
            "overlays": [],
            "availability": {
                "state": "blocked",
                "reason_code": "unsupported_price_domain",
                "price_domain": "positive_log_returns",
            },
        }
    returns = log_returns(closes)
    if len(returns) < 10:
        return {
            "version": "0.1-python",
            "latest": None,
            "events": [],
            "overlays": [],
            "availability": {
                "state": "blocked",
                "reason_code": "martin_carlo_return_warmup",
                "required_bars": 11,
                "available_bars": len(returns) + 1,
            },
        }

    mean_value = _ema(closes, min(mean_length, len(closes)))
    vol = sample_stdev(returns[-min(len(returns), lookback) :])
    drift = median(returns[-min(len(returns), lookback) :]) if params.include_drift else 0.0
    vol = max(vol, 0.000001)
    candle_bias = _candle_bias(sample, bool(params.candle_bias), candle_bias_min_confidence)
    neural_bias = (
        nn_regime_bias(sample, candle_bias=candle_bias if candle_bias.get("enabled") else None)
        if params.nn_bias
        else {"enabled": False}
    )
    structural_bias = structural_squeeze_bias(sample, enabled=bool(params.structural_bias))
    impulse_bias = _impulse_context_bias(impulse_context, bool(params.impulse_bias))
    option_bias = _option_context_bias(option_context, bool(params.option_bias))
    bias_edge = pine_bias = 0.0
    nn_drift_bias = 0.0
    nn_vol_multiplier = 1.0
    structural_drift_bias = 0.0
    structural_vol_multiplier = 1.0
    structural_mean_reversion_prob = 0.0
    structural_mean_reversion_boost = 0.0
    impulse_drift_bias = 0.0
    impulse_vol_multiplier = 1.0
    option_drift_bias = 0.0
    option_vol_multiplier = 1.0
    neutral_prob = 0.0
    if candle_bias.get("enabled"):
        bias_edge = max(-1.0, min(_safe_float(candle_bias.get("edge")) / 100.0, 1.0))
        neutral_prob = max(0.0, min(_safe_float(candle_bias.get("prob_neutral")), 1.0))
        pine_bias = bias_edge * vol * candle_bias_weight
        drift += pine_bias
        vol *= max(0.72, 1.0 - neutral_prob * neutral_vol_weight)
    if neural_bias.get("enabled"):
        continuation_edge = _safe_float(neural_bias.get("continuation_edge"))
        vol_expansion = max(0.0, min(_safe_float(neural_bias.get("vol_expansion")), 1.0))
        mean_reversion_prob = max(0.0, min(_safe_float(neural_bias.get("mean_reversion")), 1.0))
        nn_drift_bias = continuation_edge * vol * nn_bias_weight
        nn_vol_multiplier = 1.0 + vol_expansion * nn_vol_weight
        drift += nn_drift_bias
        drift *= max(0.55, 1.0 - mean_reversion_prob * 0.22)
        vol *= max(0.75, min(nn_vol_multiplier, 1.90))
    if structural_bias.get("enabled"):
        structural_mean_reversion_prob = max(
            0.0, min(_safe_float(structural_bias.get("mean_reversion")), 1.0)
        )
        structural_mean_reversion_boost = (
            structural_mean_reversion_prob * structural_mean_reversion_weight
        )
        drift *= max(0.35, 1.0 - structural_mean_reversion_prob * 0.45)
        breakout_drift_factor = max(0.25, 1.0 - structural_mean_reversion_prob * 0.75)
        structural_drift_bias = (
            _safe_float(structural_bias.get("direction_bias"))
            * vol
            * structural_bias_weight
            * breakout_drift_factor
        )
        structural_vol_multiplier = (
            1.0
            + max(0.0, _safe_float(structural_bias.get("vol_multiplier"), 1.0) - 1.0)
            * structural_vol_weight
        )
        drift += structural_drift_bias
        vol *= max(0.70, min(structural_vol_multiplier, 2.20))
    effective_mean_reversion_strength = max(
        0.01,
        min(mean_reversion_strength + structural_mean_reversion_boost, 0.75),
    )
    if impulse_bias.get("enabled") or option_bias.get("enabled"):
        drift *= option_drift_decay
    if impulse_bias.get("enabled"):
        impulse_drift_bias = (
            _safe_float(impulse_bias.get("sign"))
            * _safe_float(impulse_bias.get("strength"))
            * vol
            * impulse_bias_weight
        )
        impulse_vol_multiplier = (
            1.0 + _safe_float(impulse_bias.get("strength")) * impulse_vol_weight
        )
        drift += impulse_drift_bias
        vol *= max(0.80, min(impulse_vol_multiplier, 1.80))
    if option_bias.get("enabled"):
        option_drift_bias = _safe_float(option_bias.get("pine_bias")) * vol
        option_vol_multiplier = _safe_float(option_bias.get("vol_multiplier"), 1.0)
        drift += option_drift_bias
        vol *= max(0.60, min(option_vol_multiplier, 2.50))
    regime_returns, regime_filter = _regime_filtered_returns(
        closes,
        returns,
        mean_length,
        bool(params.regime_filter),
        high_vol_ratio=regime_high_vol_ratio,
        low_vol_ratio=regime_low_vol_ratio,
    )
    effective_engine, engine_reason_code = _auto_engine(
        engine,
        neutral_prob=neutral_prob,
        bias_edge=bias_edge,
        mean_value=mean_value,
        start_price=start_price,
        vol=vol,
        regime_sample_size=len(regime_returns),
    )
    traffic_engine = _decision_traffic_engine(decision_context)
    reversal_warning = _reversal_warning_override(
        traffic=traffic_engine,
        indicator_context=indicator_context,
        vsa_context=vsa_context,
        structural_bias=structural_bias,
        option_bias=option_bias,
        neural_bias=neural_bias,
    )
    option_engine = str(option_bias.get("recommended_engine") or "").lower()
    if engine == "auto" and traffic_engine.get("enabled"):
        effective_engine = str(traffic_engine.get("engine") or effective_engine)
        engine_reason_code = str(traffic_engine.get("reason_code") or "traffic_light")
        if reversal_warning.get("enabled"):
            effective_engine = "mean_reversion"
            engine_reason_code = f"warning_override:{','.join(reversal_warning.get('items') or [])}"
    elif (
        option_bias.get("enabled")
        and engine == "auto"
        and option_engine in {"mean_reversion", "gbm", "returns"}
    ):
        effective_engine = option_engine
        engine_reason_code = f"option_bridge:{option_engine}"
    elif structural_bias.get("enabled") and engine == "auto":
        structural_engine = str(structural_bias.get("recommended_engine") or "").lower()
        if structural_engine in {"mean_reversion", "gbm", "returns"}:
            effective_engine = structural_engine
            engine_reason_code = f"structural_squeeze:{structural_engine}"
    elif neural_bias.get("enabled") and engine == "auto":
        dominant_regime = str(neural_bias.get("dominant") or "")
        if dominant_regime == "mean_reversion":
            effective_engine = "mean_reversion"
            engine_reason_code = "nn_regime:mean_reversion"
        elif dominant_regime == "vol_expansion":
            effective_engine = "gbm"
            engine_reason_code = "nn_regime:vol_expansion"
        elif dominant_regime in {"bull_continuation", "bear_continuation"}:
            effective_engine = "returns"
            engine_reason_code = f"nn_regime:{dominant_regime}"

    bias_payload = {
        "source": "candle_specialist",
        **candle_bias,
        "nn_regime": neural_bias,
        "structural_squeeze": structural_bias,
        "impulse": impulse_bias,
        "option": option_bias,
        "traffic": traffic_engine,
        "reversal_warning": reversal_warning,
    }
    if not params.compute_forecast:
        summary_metrics = {
            "volatility": round(vol, 6),
            "drift": round(drift, 6),
            "forecast_computed": False,
            "candle_nn_enabled": bool(candle_bias.get("enabled")),
            "nn_bias_enabled": bool(neural_bias.get("enabled")),
            "structural_bias_enabled": bool(structural_bias.get("enabled")),
            "impulse_bias_enabled": bool(impulse_bias.get("enabled")),
            "option_bias_enabled": bool(option_bias.get("enabled")),
            "regime_filter_enabled": bool(regime_filter.get("enabled")),
            "regime_filter_strength": regime_filter.get("strength"),
            "effective_mean_reversion_strength": round(effective_mean_reversion_strength, 4),
        }
        latest_payload = {
            "ts": latest.ts.isoformat(),
            "price": _round_price(start_price),
            "mode": mode,
            "engine": effective_engine,
            "requested_engine": engine,
            "engine_reason_code": engine_reason_code,
            "state": str(effective_engine or "WAIT").upper(),
            "forecast": None,
            "summary": summary_metrics,
            "bias": bias_payload,
            "anchors": {
                "mean": _round_price(mean_value),
                "mean_source": "ema",
            },
        }
        return {
            "version": "0.1-python",
            "settings": {
                "enabled": True,
                "mode": mode,
                "engine": effective_engine,
                "requested_engine": engine,
                "lookback": lookback,
                "horizon": horizon,
                "simulations": simulations,
                "price_bins": price_bins,
                "compute_forecast": False,
            },
            "latest": latest_payload,
            "events": [],
            "overlays": [],
        }

    seed = f"{latest.symbol}:{latest.timeframe}:{latest.ts.isoformat()}:{lookback}:{horizon}:{simulations}:{engine}:{effective_engine}:{regime_filter.get('strength')}:{impulse_bias.get('direction')}:{impulse_bias.get('score')}:{option_bias.get('action')}:{option_bias.get('score')}"
    rng = random.Random(seed)
    paths: list[list[float]] = []
    path_lows: list[float] = []
    path_highs: list[float] = []
    reverted = 0
    above = 0
    matrix: list[list[float]] = [[] for _ in range(horizon)]
    mean_alpha = 2.0 / (max(1, mean_length) + 1.0)
    for _ in range(simulations):
        price = start_price
        path_mean = mean_value
        path: list[float] = []
        touched_mean = False
        for step in range(horizon):
            prev_price = price
            prev_path_mean = path_mean
            if effective_engine == "returns":
                shock = rng.choice(regime_returns)
                if params.include_drift:
                    shock = shock * (1.0 - returns_drift_mix) + drift * returns_drift_mix
            elif effective_engine == "gbm":
                shock = drift - 0.5 * (vol**2) + vol * rng.gauss(0.0, 1.0)
            else:
                current_distance = (
                    math.log(path_mean / price) if path_mean > 0 and price > 0 else 0.0
                )
                mean_pull = max(
                    -vol * mean_pull_vol_cap,
                    min(
                        current_distance * effective_mean_reversion_strength,
                        vol * mean_pull_vol_cap,
                    ),
                )
                shock = drift + mean_pull + vol * rng.gauss(0.0, 1.0)
            price = max(0.000001, price * math.exp(shock))
            path_mean = mean_alpha * price + (1.0 - mean_alpha) * path_mean
            if (
                not touched_mean
                and prev_path_mean > 0
                and path_mean > 0
                and (
                    (prev_price <= prev_path_mean and price >= path_mean)
                    or (prev_price >= prev_path_mean and price <= path_mean)
                )
            ):
                touched_mean = True
            path.append(price)
            matrix[step].append(price)
        if touched_mean:
            reverted += 1
        if path and path[-1] > start_price:
            above += 1
        if path:
            path_lows.append(min(path))
            path_highs.append(max(path))
        paths.append(path)

    terminal = sorted(path[-1] for path in paths if path)
    if not terminal:
        return {"version": "0.1-python", "latest": None, "events": [], "overlays": []}
    sorted_lows = sorted(path_lows)
    sorted_highs = sorted(path_highs)

    percentiles: dict[str, list[float]] = {
        key: [] for key in ("p05", "p10", "p16", "p50", "p84", "p90", "p95")
    }
    q_map = {
        "p05": 0.05,
        "p10": 0.10,
        "p16": 0.16,
        "p50": 0.50,
        "p84": 0.84,
        "p90": 0.90,
        "p95": 0.95,
    }
    heatmap: list[dict[str, Any]] = []
    for step, values in enumerate(matrix, start=1):
        sorted_step = sorted(values)
        step_percentiles = _percentile_values(sorted_step, q_map)
        for key, value in step_percentiles.items():
            percentiles[key].append(_round_price(value or start_price))
        low = _percentile(sorted_step, 0.02) or min(sorted_step)
        high = _percentile(sorted_step, 0.98) or max(sorted_step)
        if high <= low:
            continue
        bin_size = (high - low) / price_bins
        bins = [0 for _ in range(price_bins)]
        for value in values:
            idx = int((value - low) / bin_size)
            if 0 <= idx < price_bins:
                bins[idx] += 1
        heatmap.append(
            {
                "step": step,
                "bins": [
                    {
                        "low": _round_price(low + idx * bin_size),
                        "high": _round_price(low + (idx + 1) * bin_size),
                        "probability": round(count / simulations, 4),
                    }
                    for idx, count in enumerate(bins)
                    if count > 0
                ],
            }
        )

    terminal_width = max((terminal[-1] - terminal[0]) * 0.035, start_price * vol * 0.6)
    ranked_paths = sorted(
        paths,
        key=lambda path: _scenario_probability(path, terminal, terminal_width),
        reverse=True,
    )[:path_count]
    scenarios = [
        {
            "path": [_round_price(value) for value in path],
            "probability": round(_scenario_probability(path, terminal, terminal_width), 4),
        }
        for path in ranked_paths
    ]
    terminal_bins: list[dict[str, Any]] = []
    t_low = _percentile(terminal, 0.02) or terminal[0]
    t_high = _percentile(terminal, 0.98) or terminal[-1]
    if t_high > t_low:
        size = (t_high - t_low) / price_bins
        counts = [0 for _ in range(price_bins)]
        for value in terminal:
            idx = int((value - t_low) / size)
            if 0 <= idx < price_bins:
                counts[idx] += 1
        terminal_bins = [
            {
                "low": _round_price(t_low + idx * size),
                "high": _round_price(t_low + (idx + 1) * size),
                "probability": round(count / simulations, 4),
            }
            for idx, count in enumerate(counts)
            if count > 0
        ]

    terminal_stats = {
        "p05": _round_price(_percentile(terminal, 0.05) or start_price),
        "p16": _round_price(_percentile(terminal, 0.16) or start_price),
        "p50": _round_price(_percentile(terminal, 0.50) or start_price),
        "p84": _round_price(_percentile(terminal, 0.84) or start_price),
        "p95": _round_price(_percentile(terminal, 0.95) or start_price),
    }
    impulse_target_arrow = _impulse_target_arrow(
        start_price=start_price,
        terminal_stats=terminal_stats,
        impulse_bias=impulse_bias,
        neural_bias=neural_bias,
        structural_bias=structural_bias,
    )
    touch_stats = {
        "low": {
            "p05": _round_price(_percentile(sorted_lows, 0.05) or start_price),
            "p16": _round_price(_percentile(sorted_lows, 0.16) or start_price),
            "p50": _round_price(_percentile(sorted_lows, 0.50) or start_price),
            "p84": _round_price(_percentile(sorted_lows, 0.84) or start_price),
            "p95": _round_price(_percentile(sorted_lows, 0.95) or start_price),
        },
        "high": {
            "p05": _round_price(_percentile(sorted_highs, 0.05) or start_price),
            "p16": _round_price(_percentile(sorted_highs, 0.16) or start_price),
            "p50": _round_price(_percentile(sorted_highs, 0.50) or start_price),
            "p84": _round_price(_percentile(sorted_highs, 0.84) or start_price),
            "p95": _round_price(_percentile(sorted_highs, 0.95) or start_price),
        },
    }
    forecast = {
        "kind": "martin_carlo_forecast",
        "engine": effective_engine,
        "requested_engine": engine,
        "engine_reason_code": engine_reason_code,
        "display_mode": mode,
        "anchor_ts": latest.ts.isoformat(),
        "anchor_price": _round_price(start_price),
        "horizon_bars": horizon,
        "price_bins": price_bins,
        "simulations": simulations,
        "scenarios": scenarios,
        "heatmap": heatmap,
        "percentiles": percentiles,
        "terminal_distribution": {
            "target_bar_offset": horizon,
            "bins": terminal_bins,
            **terminal_stats,
        },
        "touch_distribution": touch_stats,
        "metrics": {
            "mean_reversion_probability": round(reverted / simulations, 4),
            "bullish_probability": round(above / simulations, 4),
            "bearish_probability": round(1.0 - above / simulations, 4),
            "volatility": round(vol, 6),
            "drift": round(drift, 6),
            "candle_nn_enabled": bool(candle_bias.get("enabled")),
            "candle_nn_bull": candle_bias.get("prob_bull"),
            "candle_nn_bear": candle_bias.get("prob_bear"),
            "candle_nn_neutral": candle_bias.get("prob_neutral"),
            "candle_nn_confidence": candle_bias.get("confidence"),
            "candle_nn_confidence_threshold": candle_bias.get("confidence_threshold"),
            "candle_nn_gate_reason_code": candle_bias.get("reason_code"),
            "candle_nn_edge": candle_bias.get("edge"),
            "candle_nn_drift_bias": round(pine_bias, 6),
            "nn_bias_enabled": bool(neural_bias.get("enabled")),
            "nn_regime_dominant": neural_bias.get("dominant"),
            "nn_bull_continuation": neural_bias.get("bull_continuation"),
            "nn_bear_continuation": neural_bias.get("bear_continuation"),
            "nn_mean_reversion": neural_bias.get("mean_reversion"),
            "nn_vol_expansion": neural_bias.get("vol_expansion"),
            "nn_neutral": neural_bias.get("neutral"),
            "nn_drift_bias": round(nn_drift_bias, 6),
            "nn_vol_multiplier": round(nn_vol_multiplier, 4),
            "nn_bias_weight": round(nn_bias_weight, 4),
            "nn_vol_weight": round(nn_vol_weight, 4),
            "structural_bias_enabled": bool(structural_bias.get("enabled")),
            "structural_model_source": structural_bias.get("source"),
            "structural_dominant": structural_bias.get("dominant"),
            "structural_squeeze_probability": structural_bias.get("squeeze_probability"),
            "structural_direction_bias": structural_bias.get("direction_bias"),
            "structural_drift_bias": round(structural_drift_bias, 6),
            "structural_vol_multiplier": round(structural_vol_multiplier, 4),
            "structural_bias_weight": round(structural_bias_weight, 4),
            "structural_vol_weight": round(structural_vol_weight, 4),
            "structural_mean_reversion": round(structural_mean_reversion_prob, 6),
            "structural_mean_reversion_boost": round(structural_mean_reversion_boost, 6),
            "structural_mean_reversion_weight": round(structural_mean_reversion_weight, 4),
            "regime_filter_enabled": bool(regime_filter.get("enabled")),
            "regime_filter_strength": regime_filter.get("strength"),
            "regime_filter_sample_size": regime_filter.get("sample_size"),
            "mean_reversion_strength": round(mean_reversion_strength, 4),
            "effective_mean_reversion_strength": round(effective_mean_reversion_strength, 4),
            "candle_bias_weight": round(candle_bias_weight, 4),
            "candle_bias_min_confidence": round(candle_bias_min_confidence, 4),
            "neutral_vol_weight": round(neutral_vol_weight, 4),
            "returns_drift_mix": round(returns_drift_mix, 4),
            "impulse_bias_enabled": bool(impulse_bias.get("enabled")),
            "impulse_bias_direction": impulse_bias.get("direction"),
            "impulse_bias_score": impulse_bias.get("score"),
            "impulse_bias_strength": impulse_bias.get("strength"),
            "impulse_drift_bias": round(impulse_drift_bias, 6),
            "impulse_vol_multiplier": round(impulse_vol_multiplier, 4),
            "impulse_bias_weight": round(impulse_bias_weight, 4),
            "impulse_vol_weight": round(impulse_vol_weight, 4),
            "option_bias_enabled": bool(option_bias.get("enabled")),
            "option_bias_action": option_bias.get("action"),
            "option_bias_direction": option_bias.get("direction"),
            "option_bias_side": option_bias.get("option_side"),
            "option_bias_trigger_simulation": bool(option_bias.get("trigger_simulation")),
            "option_bias_score": option_bias.get("score"),
            "option_drift_bias": round(option_drift_bias, 6),
            "option_vol_multiplier": round(option_vol_multiplier, 4),
            "option_drift_decay": round(option_drift_decay, 4),
            "traffic_engine_enabled": bool(traffic_engine.get("enabled")),
            "traffic_engine_phase": traffic_engine.get("phase"),
            "traffic_engine_direction": traffic_engine.get("direction"),
            "traffic_engine_action": traffic_engine.get("action"),
            "traffic_engine_kind": traffic_engine.get("kind"),
            "traffic_engine_reason_code": traffic_engine.get("reason_code"),
            "reversal_warning_enabled": bool(reversal_warning.get("enabled")),
            "reversal_warning_score": reversal_warning.get("score"),
            "reversal_warning_items": reversal_warning.get("items"),
            "mean_pull_vol_cap": round(mean_pull_vol_cap, 4),
        },
        "bias": bias_payload,
        "advisory": _advisory_zone(start_price, terminal_stats),
        "impulse_target_arrow": impulse_target_arrow,
        "regime_filter": regime_filter,
        "anchors": {
            "mean": _round_price(mean_value),
            "mean_source": "ema",
        },
    }
    latest_payload = {
        "ts": latest.ts.isoformat(),
        "price": _round_price(start_price),
        "mode": mode,
        "engine": engine,
        "forecast": forecast,
    }
    return {
        "version": "0.1-python",
        "settings": {
            "enabled": True,
            "mode": mode,
            "engine": effective_engine,
            "requested_engine": engine,
            "lookback": lookback,
            "horizon": horizon,
            "simulations": simulations,
            "price_bins": price_bins,
        },
        "latest": latest_payload,
        "events": [],
        "overlays": [
            {
                "type": "custom",
                "source": "martin_carlo",
                "renderer_ref": "martin_carlo_forecast",
                "payload": forecast,
                "render_key": f"{seed}:{_round_price(start_price)}",
            }
        ],
    }
