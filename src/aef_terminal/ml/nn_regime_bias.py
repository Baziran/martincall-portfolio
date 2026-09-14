from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.runtime.math_utils import log_returns, safe_float, sample_stdev


def _softmax(items: dict[str, float]) -> dict[str, float]:
    if not items:
        return {}
    max_value = max(items.values())
    exps = {key: math.exp(value - max_value) for key, value in items.items()}
    total = sum(exps.values()) or 1.0
    return {key: value / total for key, value in exps.items()}


def nn_regime_bias(
    bars: Sequence[Bar],
    *,
    candle_bias: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(bars) < 24:
        return {"enabled": False, "reason": "not_enough_bars"}
    closes = [safe_float(bar.close, 0.0) for bar in bars if safe_float(bar.close, 0.0) > 0]
    if len(closes) < 24:
        return {"enabled": False, "reason": "not_enough_closes"}
    returns = log_returns(closes)
    if len(returns) < 12:
        return {"enabled": False, "reason": "not_enough_returns"}

    recent_returns = returns[-8:]
    base_returns = returns[-min(len(returns), 80) :]
    recent_drift = sum(recent_returns) / max(len(recent_returns), 1)
    base_drift = sum(base_returns) / max(len(base_returns), 1)
    recent_vol = sample_stdev(recent_returns)
    base_vol = max(sample_stdev(base_returns), 0.000001)
    vol_ratio = recent_vol / base_vol

    bull = safe_float((candle_bias or {}).get("prob_bull"), 0.0)
    bear = safe_float((candle_bias or {}).get("prob_bear"), 0.0)
    neutral = safe_float((candle_bias or {}).get("prob_neutral"), 0.0)
    directional_edge = max(-1.0, min(bull - bear, 1.0))
    trend_edge = max(-1.0, min((recent_drift + base_drift) / base_vol, 1.0))
    vol_edge = max(0.0, min((vol_ratio - 0.85) / 0.90, 1.0))
    mean_reversion_pressure = max(0.0, min(abs(trend_edge) * (1.0 - min(vol_edge, 0.80)), 1.0))

    raw = {
        "bull_continuation": 1.00 + max(directional_edge, 0.0) * 2.20 + max(trend_edge, 0.0) * 1.15,
        "bear_continuation": 1.00
        + max(-directional_edge, 0.0) * 2.20
        + max(-trend_edge, 0.0) * 1.15,
        "mean_reversion": 1.00 + mean_reversion_pressure * 1.65 + neutral * 0.55,
        "vol_expansion": 1.00 + vol_edge * 1.75 + (1.0 - neutral) * 0.35,
        "neutral": 1.00 + neutral * 1.45 + max(0.0, 1.0 - abs(trend_edge) - vol_edge) * 0.75,
    }
    probs = _softmax(raw)
    continuation_edge = probs["bull_continuation"] - probs["bear_continuation"]
    dominant = max(probs, key=lambda key: probs[key])
    return {
        "enabled": True,
        "source": "candle_specialist_regime_adapter",
        "dominant": dominant,
        "bull_continuation": round(probs["bull_continuation"], 6),
        "bear_continuation": round(probs["bear_continuation"], 6),
        "mean_reversion": round(probs["mean_reversion"], 6),
        "vol_expansion": round(probs["vol_expansion"], 6),
        "neutral": round(probs["neutral"], 6),
        "continuation_edge": round(continuation_edge, 6),
        "vol_ratio": round(vol_ratio, 6),
        "trend_edge": round(trend_edge, 6),
        "candle_model_source": (candle_bias or {}).get("model_source") or "",
    }
