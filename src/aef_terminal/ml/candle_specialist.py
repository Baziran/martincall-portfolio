from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import timedelta
from functools import lru_cache
from math import exp, log, sqrt, tanh
from pathlib import Path
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.domain import Bar, Direction
from aef_terminal.runtime import pine


MODEL_MEAN = (
    0.006883,
    0.770958,
    0.193044,
    0.206072,
    0.511554,
    0.002577,
    0.818033,
    0.234920,
    0.248407,
    0.509055,
    0.004146,
    0.821699,
    0.230693,
    0.243476,
    0.509576,
    0.026799,
    0.192692,
    0.298210,
    -0.699858,
    0.002280,
)
MODEL_STD = (
    0.545929,
    0.252316,
    0.235960,
    0.243715,
    0.380950,
    0.577836,
    0.498263,
    0.244550,
    0.250504,
    0.355183,
    0.580064,
    0.511445,
    0.240454,
    0.246036,
    0.355022,
    0.995179,
    1.728406,
    2.725986,
    1.747100,
    0.002863,
)
MODEL_W1 = (
    -1.761574,
    -0.415240,
    0.583456,
    -0.619948,
    0.661856,
    -0.331185,
    0.361704,
    0.131012,
    -0.205609,
    0.443691,
    0.248338,
    0.298848,
    0.091699,
    -0.309435,
    -0.162367,
    -0.003432,
    3.079235,
    -4.679656,
    0.049234,
    1.533702,
    -0.893690,
    -0.132708,
    0.010713,
    -0.133955,
    -1.539303,
    -0.905043,
    0.039553,
    0.840845,
    -0.044484,
    0.767328,
    -0.039825,
    -0.024720,
    0.261197,
    0.565227,
    -0.000447,
    -0.039060,
    2.878828,
    -3.779563,
    2.191945,
    -0.336829,
    -0.536647,
    1.369957,
    0.420348,
    0.089449,
    0.251638,
    -0.542207,
    0.171974,
    0.211605,
    0.005086,
    0.106764,
    -0.676072,
    0.710573,
    0.041017,
    0.074022,
    0.384887,
    0.519209,
    -1.169501,
    1.889546,
    -4.005042,
    -4.136447,
    0.441934,
    -0.512160,
    0.204037,
    0.373637,
    -0.659151,
    0.197050,
    1.836631,
    0.294805,
    0.259662,
    -0.165074,
    -0.050848,
    -0.096277,
    -0.016477,
    0.118647,
    -0.037920,
    -0.382426,
    -0.395098,
    0.823944,
    1.393203,
    -0.638577,
    -0.107306,
    -0.155175,
    -0.238377,
    -0.000357,
    0.059126,
    0.151997,
    -0.211812,
    -0.044575,
    -0.051146,
    -0.187575,
    0.034936,
    -0.086953,
    -0.275405,
    -0.073849,
    0.092948,
    -0.326790,
    -4.295771,
    1.621424,
    0.858199,
    -1.953021,
    1.468378,
    1.389741,
    -1.274830,
    -0.088630,
    -1.519430,
    1.882728,
    4.969962,
    -0.167085,
    0.650472,
    -0.510990,
    -0.002333,
    1.708711,
    0.086983,
    -0.023791,
    0.069744,
    0.175980,
    2.522897,
    -2.957372,
    1.082151,
    -0.289062,
    -0.298681,
    -0.009222,
    0.196827,
    -0.031923,
    0.338970,
    0.404138,
    -0.340933,
    -7.979254,
    -0.034839,
    -0.116711,
    -0.028614,
    -0.055633,
    -7.698988,
    0.075088,
    0.234013,
    -0.157646,
    0.368427,
    0.184493,
    -0.570618,
    -0.675251,
    -0.073154,
    -0.375018,
    0.009755,
    0.054059,
    -0.001738,
    0.316981,
    -0.110744,
    -0.062010,
    -0.052373,
    -0.173178,
    -0.091639,
    -0.299174,
    -0.196710,
    -0.189518,
    0.031010,
    -0.005527,
    -1.064107,
    0.436136,
    -0.481854,
    4.353828,
    0.101822,
    7.946060,
    -0.081907,
    -0.034792,
    0.068765,
    0.112040,
    -0.416715,
    -0.051512,
    -0.138160,
    -0.087868,
    -0.163251,
    -0.597555,
    0.001822,
    -0.062959,
    -0.027973,
    -0.008190,
    0.078505,
    -0.041952,
    0.227757,
    0.095034,
    -0.308404,
    -0.060369,
    0.160550,
    -0.099070,
    0.183635,
    -0.206364,
    0.055969,
    0.119189,
    -0.010867,
    0.201099,
    -0.177222,
    0.043741,
    0.109896,
    -0.069449,
    0.082207,
    0.175971,
    -0.027614,
    -0.055421,
    0.243496,
    -11.388196,
    -0.306434,
    0.673893,
    0.798623,
    0.685707,
    0.208318,
    -0.148695,
    -0.450594,
    0.043449,
    -0.122461,
    0.006117,
    0.076951,
    -0.217137,
    0.090794,
    0.206628,
    -0.133992,
    -0.107796,
    2.309269,
    -2.341223,
    -6.913434,
    1.738715,
    0.571456,
    0.080265,
    -0.163659,
    8.883887,
    -0.276488,
    0.025888,
    0.112966,
    -0.108894,
    -0.038723,
    -0.177201,
    0.196644,
    0.048094,
    -0.018318,
    7.427168,
    -0.198587,
    0.051052,
    -0.679773,
    0.430156,
    0.166857,
    0.309779,
    -1.163926,
    1.616730,
    0.949319,
    0.399586,
    0.261798,
    -0.151468,
    -0.246350,
    0.295164,
    0.256006,
    0.364156,
    -0.534265,
    0.064272,
    -0.025915,
    -0.226971,
    0.388912,
    -0.015132,
    -0.342436,
    0.082199,
    -0.421836,
    -0.177480,
    0.241815,
    -0.128978,
    -0.093992,
    0.020510,
    -0.205412,
    -0.247569,
    -0.498972,
    0.170435,
    -13.535007,
    -0.186435,
    0.017578,
    -0.521287,
    -0.213280,
    -0.014104,
    0.136758,
    0.094228,
    0.240238,
    -0.080564,
    0.029075,
    -0.654754,
    0.403041,
    -0.342641,
    -15.578433,
    -0.097087,
    -0.359366,
    0.086726,
    -0.257704,
    -0.123626,
    0.107602,
    -0.199473,
    0.048283,
    -0.067701,
    -0.109216,
    -0.160134,
    0.021452,
    0.064641,
    0.153162,
    0.023526,
    -0.071751,
    -0.723623,
    -1.124726,
    -0.776099,
    0.035041,
    -0.358402,
    0.286647,
    -0.125497,
    -0.019301,
    0.444846,
    -0.101097,
    0.589583,
    -0.194988,
    -2.002868,
    -0.246833,
    -0.687956,
    0.374779,
    -0.321430,
    -1.418552,
    1.896151,
    0.174664,
    0.599971,
)
MODEL_B1 = (
    -0.483742,
    6.378280,
    -1.583045,
    0.340614,
    -2.787989,
    6.571742,
    -14.594166,
    1.508167,
    -7.779990,
    -7.121786,
    -7.188072,
    14.051159,
    0.512094,
    -13.097138,
    -12.132229,
    -2.117190,
)
MODEL_W2 = (
    -0.147039,
    -0.169086,
    -0.009511,
    0.077940,
    0.018064,
    0.094150,
    0.218786,
    -0.064721,
    -0.365120,
    0.019079,
    0.055432,
    -0.050866,
    0.255945,
    -0.009658,
    0.275866,
    -0.087793,
    -0.090491,
    -0.013362,
    0.196171,
    -0.075293,
    -0.080357,
    -0.090312,
    -0.033556,
    0.176715,
    0.181821,
    -0.382382,
    0.408756,
    0.187833,
    0.082690,
    -0.191303,
    0.085363,
    0.077950,
    -0.036376,
    0.040731,
    -0.063496,
    0.117203,
    0.165061,
    0.091295,
    0.201722,
    -0.046442,
    -0.378984,
    0.083186,
    0.131989,
    -0.125484,
    0.222605,
    -0.000502,
    0.281679,
    -0.100670,
)
MODEL_B2 = (0.048083, -0.066786, 0.033746)


def candle_specialist_model_path(model_path: str | None = None) -> Path:
    configured_path = model_path or os.environ.get("AEF_CANDLE_SPECIALIST_MODEL_PATH")
    if configured_path:
        return Path(configured_path)
    return AppConfig().data_root / "models" / "candle_specialist_model.json"


@lru_cache(maxsize=4)
def _load_candle_specialist_artifact(path_text: str) -> dict[str, Any] | None:
    path = Path(path_text)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not all(key in payload for key in ("mean", "std", "w1", "b1", "w2", "b2")):
        return None
    return payload


def _artifact_probabilities(
    features: Sequence[float], model_path: str | None = None
) -> dict[str, Any] | None:
    path = candle_specialist_model_path(model_path)
    model = _load_candle_specialist_artifact(str(path))
    if model is None:
        return None
    mean = [float(value) for value in model.get("mean", [])]
    std = [max(float(value), 0.000001) for value in model.get("std", [])]
    w1 = [[float(item) for item in row] for row in model.get("w1", [])]
    b1 = [float(value) for value in model.get("b1", [])]
    w2 = [[float(item) for item in row] for row in model.get("w2", [])]
    b2 = [float(value) for value in model.get("b2", [])]
    if (
        len(mean) != len(features)
        or len(std) != len(features)
        or len(w1) != len(features)
        or len(b2) != 3
    ):
        return None
    hidden_count = len(b1)
    if (
        hidden_count <= 0
        or any(len(row) != hidden_count for row in w1)
        or len(w2) != hidden_count
        or any(len(row) != 3 for row in w2)
    ):
        return None
    normalized = [
        pine.clamp((float(value) - mean[index]) / std[index], -6.0, 6.0)
        for index, value in enumerate(features)
    ]
    hidden = [
        tanh(
            b1[node] + sum(w1[index][node] * normalized[index] for index in range(len(normalized)))
        )
        for node in range(hidden_count)
    ]
    logits = [
        b2[cls] + sum(w2[node][cls] * hidden[node] for node in range(hidden_count))
        for cls in range(3)
    ]
    prob_bear, prob_neutral, prob_bull = _softmax3(logits)
    edge = (prob_bull - prob_bear) * 100.0
    return {
        "prob_bear": round(prob_bear, 6),
        "prob_neutral": round(prob_neutral, 6),
        "prob_bull": round(prob_bull, 6),
        "edge": round(edge, 4),
        "model_path": str(path),
        "model_metrics": model.get("metrics") or (model.get("meta") or {}).get("metrics") or {},
        "model_meta": model.get("meta") or {},
    }


def _softmax3(values: Sequence[float]) -> tuple[float, float, float]:
    m = max(values)
    exps = [exp(value - m) for value in values]
    total = sum(exps) or 1.0
    return exps[0] / total, exps[1] / total, exps[2] / total


def _interval_delta(bars: Sequence[Bar]) -> timedelta:
    if len(bars) >= 2:
        delta = bars[-1].ts - bars[-2].ts
        if delta.total_seconds() > 0:
            return delta
    return timedelta(minutes=5)


def _features_for_index(
    bars: Sequence[Bar],
    index: int,
    atr: Sequence[float],
    ema_fast: Sequence[float],
    ema_slow: Sequence[float],
    rvol: Sequence[float],
) -> list[float] | None:
    if index < 2:
        return None

    def anatomy(offset: int) -> tuple[Bar, pine.BarAnatomy]:
        bar = bars[index - offset]
        return bar, pine.bar_anatomy(bar)

    b0, a0 = anatomy(0)
    b1, a1 = anatomy(1)
    b2, a2 = anatomy(2)
    atr_value = max(float(atr[index]), 0.000001)
    close = float(b0.close)
    return [
        a0.body_signed / atr_value,
        a0.rng / atr_value,
        a0.upper_share,
        a0.lower_share,
        a0.close_pos,
        a1.body_signed / atr_value,
        a1.rng / atr_value,
        a1.upper_share,
        a1.lower_share,
        a1.close_pos,
        a2.body_signed / atr_value,
        a2.rng / atr_value,
        a2.upper_share,
        a2.lower_share,
        a2.close_pos,
        (close - float(bars[index - 3].close if index >= 3 else close)) / atr_value,
        (float(ema_fast[index]) - float(ema_slow[index])) / atr_value,
        (close - float(ema_slow[index])) / atr_value,
        log(max(float(rvol[index]), 0.000001)),
        atr_value / max(close, 0.000001),
    ]


def candle_nn_probabilities(
    bars: Sequence[Bar],
    atr: Sequence[float] | None = None,
    ema_fast: Sequence[float] | None = None,
    ema_slow: Sequence[float] | None = None,
    rvol: Sequence[float] | None = None,
    *,
    index: int | None = None,
    model_path: str | None = None,
) -> dict[str, Any] | None:
    if len(bars) < 5:
        return None
    idx = len(bars) - 1 if index is None else index
    closes = [float(bar.close) for bar in bars]
    atr_values = list(atr or pine.atr_rma_series(bars, 14))
    ema_fast_values = list(ema_fast or pine.ema_series(closes, 21))
    ema_slow_values = list(ema_slow or pine.ema_series(closes, 55))
    rvol_values = list(rvol or pine.relative_volume_series(bars, 30))
    raw = _features_for_index(bars, idx, atr_values, ema_fast_values, ema_slow_values, rvol_values)
    if raw is None:
        return None
    artifact_result = _artifact_probabilities(raw, model_path=model_path)
    if artifact_result is not None:
        artifact_result["features"] = [round(value, 6) for value in raw]
        artifact_result["model_source"] = "trained_artifact"
        return artifact_result
    feat = [
        pine.clamp((value - MODEL_MEAN[i]) / (MODEL_STD[i] or 1.0), -6.0, 6.0)
        for i, value in enumerate(raw)
    ]
    hidden = []
    for node, bias in enumerate(MODEL_B1):
        start = node * 20
        hidden.append(tanh(bias + sum(MODEL_W1[start + i] * feat[i] for i in range(20))))
    logits = []
    for cls, bias in enumerate(MODEL_B2):
        start = cls * 16
        logits.append(bias + sum(MODEL_W2[start + i] * hidden[i] for i in range(16)))
    prob_bear, prob_neutral, prob_bull = _softmax3(logits)
    edge = (prob_bull - prob_bear) * 100.0
    return {
        "prob_bear": round(prob_bear, 6),
        "prob_neutral": round(prob_neutral, 6),
        "prob_bull": round(prob_bull, 6),
        "edge": round(edge, 4),
        "features": [round(value, 6) for value in raw],
        "model_source": "pine_restored",
    }


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _edge_confidence_score(edge: float) -> float:
    return pine.clamp(50.0 + abs(float(edge)) * 0.5, 0.0, 99.0)


def _model_training_tip(meta: dict[str, Any]) -> str:
    if not isinstance(meta, dict) or not meta:
        return ""
    horizon = meta.get("horizon")
    edge_atr = meta.get("edge_atr")
    label_mode = meta.get("label_mode")
    timeframe = meta.get("timeframe")
    parts: list[str] = []
    if horizon is not None and edge_atr is not None:
        parts.append(f"trained for {horizon} bars >= {edge_atr} ATR")
    elif horizon is not None:
        parts.append(f"trained horizon {horizon} bars")
    if label_mode:
        parts.append(f"label {label_mode}")
    if timeframe:
        parts.append(f"tf {timeframe}")
    return "; ".join(parts)


def _bar_step_features(
    bars: Sequence[Bar],
    index: int,
    atr: Sequence[float],
    ema_fast: Sequence[float],
    vol_sma: Sequence[float],
) -> tuple[float, ...]:
    bar = bars[index]
    anatomy = pine.bar_anatomy(bar)
    rng = max(anatomy.rng, 0.000001)
    atr_value = max(float(atr[index]), 0.000001)
    prev = bars[index - 1] if index > 0 else bar
    rel_vol = 0.5
    if bar.volume and vol_sma[index] > 0:
        rel_vol = min(float(bar.volume) / max(float(vol_sma[index]), 1.0), 3.0) / 3.0
    context = (
        1.0
        if bar.high > prev.high and bar.low < prev.low
        else 0.0
        if bar.high <= prev.high and bar.low >= prev.low
        else 0.5
    )
    trend = (pine.clamp((bar.close - ema_fast[index]) / atr_value, -2.0, 2.0) + 2.0) / 4.0
    return (
        abs(bar.close - bar.open) / rng,
        anatomy.upper_share,
        anatomy.lower_share,
        (min(bar.open, bar.close) - bar.low) / rng,
        rel_vol,
        min(rng / atr_value, 2.0) / 2.0,
        trend,
        context,
    )


def _morph_score(a: Sequence[tuple[float, ...]], b: Sequence[tuple[float, ...]]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dist = 0.0
    for left, right in zip(a, b, strict=False):
        dist += sum((x - y) ** 2 for x, y in zip(left, right, strict=False))
    dist = sqrt(dist / len(a))
    return (1.0 - min(dist, sqrt(8.0)) / sqrt(8.0)) * 100.0


def candle_specialist(
    bars: Sequence[Bar],
    *,
    min_prob: float = 0.50,
    min_edge: float = 10.0,
    model_path: str | None = None,
    dna_lookback: int = 600,
    dna_pattern_bars: int = 4,
    dna_forward_bars: int = 5,
    dna_min_outcome_atr: float = 2.5,
    dna_max_distance: float = 92.0,
    dna_max_outcome_atr: float = 8.0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": "4.4.0-pine-candle-specialist",
        "events": [],
        "overlays": [],
        "latest": None,
    }
    if len(bars) < max(12, dna_pattern_bars + dna_forward_bars + 4):
        result["latest"] = {
            "state": "WAIT",
            "action": "WAIT",
            "score": 0.0,
            "reason": "not enough bars",
        }
        return result
    closes = [float(bar.close) for bar in bars]
    atr = pine.atr_rma_series(bars, 14)
    ema_fast = pine.ema_series(closes, 21)
    ema_slow = pine.ema_series(closes, 55)
    rvol = pine.relative_volume_series(bars, 30)
    probs = candle_nn_probabilities(bars, atr, ema_fast, ema_slow, rvol, model_path=model_path)
    if probs is None:
        result["latest"] = {
            "state": "WAIT",
            "action": "WAIT",
            "score": 0.0,
            "reason": "model features unavailable",
        }
        return result
    prob_bull = float(probs["prob_bull"])
    prob_bear = float(probs["prob_bear"])
    edge = float(probs["edge"])
    ai_bull = prob_bull >= min_prob and edge >= min_edge
    ai_bear = prob_bear >= min_prob and edge <= -min_edge
    direction = Direction.LONG if ai_bull else Direction.SHORT if ai_bear else Direction.FLAT
    dominant_prob = max(prob_bull, prob_bear)
    dominant_direction = Direction.LONG if prob_bull >= prob_bear else Direction.SHORT
    projection_prob_gate = min(max(float(min_prob), 0.10), 0.20)
    projection_direction = (
        direction
        if direction != Direction.FLAT
        else dominant_direction
        if dominant_prob >= projection_prob_gate
        else Direction.FLAT
    )
    score = _edge_confidence_score(edge)
    action = "ARM" if direction != Direction.FLAT else "WAIT"
    bar = bars[-1]
    atr_value = max(float(atr[-1]), 0.000001)
    trigger = float(bar.close)
    stop = trigger - atr_value * 0.55 if direction == Direction.LONG else trigger + atr_value * 0.55
    target = (
        trigger + atr_value * 1.35 if direction == Direction.LONG else trigger - atr_value * 1.35
    )
    model_meta = probs.get("model_meta") or {}
    training_tip = _model_training_tip(model_meta)
    context_lines = [
        f"Bull {prob_bull:.2f} / Bear {prob_bear:.2f}",
        f"Edge {edge:.1f}; confidence {score:.1f}; action {action}",
        "Trained local artifact"
        if probs.get("model_source") == "trained_artifact"
        else "Original candle-specialist weights",
    ]
    if training_tip:
        context_lines.append(training_tip)
    setup_summary = f"Candle NN bull/bear {prob_bull:.2f}/{prob_bear:.2f}, edge {edge:.1f}"
    latest = {
        "ts": bar.ts.isoformat(),
        "state": "CANDLE_NN_LONG"
        if direction == Direction.LONG
        else "CANDLE_NN_SHORT"
        if direction == Direction.SHORT
        else "CANDLE_NN_WAIT",
        "action": action,
        "direction": direction.value,
        "score": round(score, 1),
        "confidence_edge": round(abs(edge), 2),
        "confidence_prob": round(dominant_prob * 100.0, 1),
        "dominant_prob": round(dominant_prob, 6),
        "probabilities": {
            "bear": round(prob_bear, 6),
            "neutral": probs.get("prob_neutral", 0.0),
            "bull": round(prob_bull, 6),
        },
        "edge": round(edge, 2),
        "price": _round(trigger),
        "setup_summary": setup_summary,
        "candidate_reason": setup_summary,
        "scenario": "Candle Specialist NN",
        "quality_text": f"confidence {score:.1f}",
        "context_lines": context_lines,
        "model_source": probs.get("model_source", "pine_restored"),
        "model_path": probs.get("model_path", str(candle_specialist_model_path(model_path))),
        "model_metrics": probs.get("model_metrics") or {},
        "model_meta": model_meta,
        "projection_prob_gate": round(projection_prob_gate, 3),
    }
    if direction != Direction.FLAT:
        latest["signal"] = {
            "source": "candle_specialist",
            "action": action,
            "raw_action": action,
            "direction": direction.value,
            "score": round(score, 1),
            "trigger": _round(trigger),
            "stop": _round(stop),
            "target": _round(target),
            "invalidation": _round(stop),
            "code": latest["state"],
            "reason": latest["candidate_reason"],
            "blocked": False,
        }
        result["events"].append(latest)
        result["overlays"].append(
            {
                "type": "label",
                "ts": bar.ts.isoformat(),
                "price": _round(
                    bar.low - atr_value * 0.28
                    if direction == Direction.LONG
                    else bar.high + atr_value * 0.28
                ),
                "lines": ["CNN", "UP" if direction == Direction.LONG else "DN", str(round(score))],
                "side": "below" if direction == Direction.LONG else "above",
                "bg": "rgba(0, 188, 212, 0.78)"
                if direction == Direction.LONG
                else "rgba(255, 77, 109, 0.78)",
                "text": "#061018",
                "source": "candle_specialist",
                "action": action,
                "raw_action": action,
                "direction": direction.value,
                "score": round(score, 1),
                "entry": _round(trigger),
                "trigger": _round(trigger),
                "stop": _round(stop),
                "target": _round(target),
                "invalidation": _round(stop),
                "signal": dict(latest["signal"]),
                "scenario": latest["scenario"],
                "quality_text": latest["quality_text"],
                "context_lines": latest["context_lines"],
            }
        )

    dna = _dna_projection(
        bars,
        atr=atr,
        ema_fast=ema_fast,
        prob_bull=prob_bull,
        prob_bear=prob_bear,
        edge=edge,
        base_direction=projection_direction,
        lookback=dna_lookback,
        pattern_bars=dna_pattern_bars,
        forward_bars=dna_forward_bars,
        min_outcome_atr=dna_min_outcome_atr,
        max_distance=dna_max_distance,
        max_outcome_atr=dna_max_outcome_atr,
    )
    latest["dna"] = dna["summary"]
    result["overlays"].extend(dna["overlays"])
    result["latest"] = latest
    return result


def _dna_projection(
    bars: Sequence[Bar],
    *,
    atr: Sequence[float],
    ema_fast: Sequence[float],
    prob_bull: float,
    prob_bear: float,
    edge: float,
    base_direction: Direction,
    lookback: int,
    pattern_bars: int,
    forward_bars: int,
    min_outcome_atr: float,
    max_distance: float,
    max_outcome_atr: float,
) -> dict[str, Any]:
    overlays: list[dict[str, Any]] = []
    if base_direction == Direction.FLAT:
        return {"summary": {"state": "DNA IDLE", "match": "wait candle NN"}, "overlays": overlays}
    n = len(bars)
    base_idx = n - 1
    vol_sma = pine.rolling_mean_series([bar.volume for bar in bars], 20)
    base_pattern = [
        _bar_step_features(bars, base_idx - p, atr, ema_fast, vol_sma) for p in range(pattern_bars)
    ]
    top: list[tuple[int, float, float]] = []
    min_idx = max(pattern_bars, n - 1 - max(int(lookback), 1))
    max_idx = n - forward_bars - pattern_bars - 1
    searched = 0
    for hist_idx in range(min_idx, max_idx + 1):
        hist_pattern = [
            _bar_step_features(bars, hist_idx - p, atr, ema_fast, vol_sma)
            for p in range(pattern_bars)
        ]
        morph = _morph_score(base_pattern, hist_pattern)
        hist_atr = max(float(atr[hist_idx]), 0.000001)
        future = bars[hist_idx + 1 : hist_idx + forward_bars + 1]
        future_high = max(bar.high for bar in future)
        future_low = min(bar.low for bar in future)
        move_up = (future_high - bars[hist_idx].close) / hist_atr
        move_dn = (bars[hist_idx].close - future_low) / hist_atr
        outcome = move_up if move_up > move_dn else -move_dn
        raw = abs(outcome) <= max_outcome_atr
        valid = (
            raw
            and outcome * (1 if base_direction == Direction.LONG else -1) > 0
            and morph >= max_distance
            and abs(outcome) >= min_outcome_atr
        )
        searched += 1 if raw else 0
        if valid:
            top.append((hist_idx, morph, outcome))
            top.sort(key=lambda item: item[1], reverse=True)
            top = top[:7]
    color = (
        "rgba(0, 188, 212, 0.78)"
        if base_direction == Direction.LONG
        else "rgba(255, 77, 109, 0.78)"
    )
    fill = (
        "rgba(0, 188, 212, 0.10)"
        if base_direction == Direction.LONG
        else "rgba(255, 77, 109, 0.10)"
    )
    bar_delta = _interval_delta(bars)
    base_ts = bars[-1].ts
    base_close = float(bars[-1].close)
    if not top:
        summary = {"state": "NO MATCH", "match": f"0/{searched}x", "preview": True}
        projection: list[dict[str, Any]] = []
        dna_context_lines = [
            "No projection is drawn without historical matches.",
            f"AI bull/bear: {prob_bull:.2f}/{prob_bear:.2f}",
            f"Historical matches: 0/{searched}",
        ]
    else:
        weights = [max(score - max_distance + 1.0, 1.0) for _, score, _ in top]
        sum_w = sum(weights) or 1.0
        avg_score = (
            sum(score * weight for (_, score, _), weight in zip(top, weights, strict=False)) / sum_w
        )
        avg_outcome = (
            sum(outcome * weight for (_, _, outcome), weight in zip(top, weights, strict=False))
            / sum_w
        )
        summary = {
            "state": "DNA",
            "match": f"{len(top)}x / {avg_outcome:.2f}R",
            "score": round(avg_score, 2),
            "outcome_r": round(avg_outcome, 3),
            "preview": False,
        }
        projection = _matched_candles(
            bars, top, weights, sum_w, base_ts, bar_delta, base_close, forward_bars
        )
        dna_context_lines = [
            f"Matches: {len(top)} / {searched} qualified",
            f"Score: {avg_score:.1f}",
            f"Outcome: {avg_outcome:.2f}R in {forward_bars} bars",
            f"AI bull/bear: {prob_bull:.2f}/{prob_bear:.2f}",
        ]
    dna_fact_fields = {
        "scenario": "Setup DNA ensemble" if projection else "AI DNA no qualified history",
        "context_lines": dna_context_lines,
    }
    if projection:
        target = projection[-1]["close"]
        overlays.append(
            {
                "type": "line",
                "start_ts": base_ts.isoformat(),
                "end_ts": (base_ts + bar_delta * (forward_bars + 1)).isoformat(),
                "y1": _round(base_close),
                "y2": _round(target),
                "color": color,
                "style": "dotted",
                "width": 2,
                "label": "DNA",
                **dna_fact_fields,
            }
        )
        overlays.append(
            {
                "type": "box",
                "start_ts": (base_ts + bar_delta).isoformat(),
                "end_ts": (base_ts + bar_delta * (forward_bars + 1)).isoformat(),
                "top": _round(max(candle["high"] for candle in projection)),
                "bottom": _round(min(candle["low"] for candle in projection)),
                "border": color,
                "bg": fill,
                "style": "dotted",
                **dna_fact_fields,
            }
        )
    for candle in projection:
        overlays.append(
            {
                "type": "box",
                "start_ts": candle["start_ts"].isoformat(),
                "end_ts": candle["end_ts"].isoformat(),
                "top": _round(max(candle["open"], candle["close"])),
                "bottom": _round(min(candle["open"], candle["close"])),
                "border": color,
                "bg": fill,
                "style": "solid",
                **dna_fact_fields,
            }
        )
        mid_ts = candle["start_ts"] + (candle["end_ts"] - candle["start_ts"]) / 2
        overlays.append(
            {
                "type": "line",
                "start_ts": mid_ts.isoformat(),
                "end_ts": mid_ts.isoformat(),
                "y1": _round(candle["high"]),
                "y2": _round(candle["low"]),
                "color": color,
                "width": 2,
                **dna_fact_fields,
            }
        )
    return {"summary": summary, "overlays": overlays}


def _matched_candles(
    bars: Sequence[Bar],
    top: Sequence[tuple[int, float, float]],
    weights: Sequence[float],
    sum_w: float,
    base_ts,
    bar_delta,
    base_close: float,
    forward_bars: int,
) -> list[dict[str, Any]]:
    out = []
    for step in range(1, forward_bars + 1):
        sums = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}
        for (hist_idx, _, _), weight in zip(top, weights, strict=False):
            align_open = bars[hist_idx + 1].open
            src = bars[hist_idx + step]
            sums["open"] += (base_close + (src.open - align_open)) * weight
            sums["high"] += (base_close + (src.high - align_open)) * weight
            sums["low"] += (base_close + (src.low - align_open)) * weight
            sums["close"] += (base_close + (src.close - align_open)) * weight
        out.append(
            {
                "start_ts": base_ts + bar_delta * step,
                "end_ts": base_ts + bar_delta * (step + 1),
                "open": sums["open"] / sum_w,
                "high": sums["high"] / sum_w,
                "low": sums["low"] / sum_w,
                "close": sums["close"] / sum_w,
            }
        )
    return out
