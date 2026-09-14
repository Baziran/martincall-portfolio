from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from aef_terminal.config import AppConfig
from aef_terminal.domain import Bar
from aef_terminal.ml.math_utils import softmax_rows
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import safe_div as _safe_div, safe_float


STRUCTURAL_SQUEEZE_FEATURES = [
    "range_atr_20",
    "range_atr_60",
    "atr_ratio",
    "atr_5_60_ratio",
    "rvol_30",
    "volume_z_60",
    "ema_slope_atr",
    "ema20_distance_atr",
    "ema55_distance_atr",
    "ema200_distance_atr",
    "close_pos_1",
    "close_range_pos_60",
    "body_range_share",
    "wick_balance",
    "breakout_pressure",
    "return_12_atr",
    "return_36_atr",
    "volume_trend",
    "tod_sin",
    "tod_cos",
    "dow_sin",
    "dow_cos",
]
STRUCTURAL_SQUEEZE_LABELS = ["neutral", "mean_reversion", "breakout_up", "breakout_down"]


@dataclass(frozen=True)
class StructuralSqueezeArtifact:
    feature_columns: list[str]
    labels: list[str]
    mean: list[float]
    scale: list[float]
    w1: list[list[float]]
    b1: list[float]
    w2: list[list[float]]
    b2: list[float]
    metrics: dict[str, Any]


def structural_squeeze_model_path(model_path: str | None = None) -> Path:
    configured_path = model_path or os.environ.get("AEF_STRUCTURAL_SQUEEZE_MODEL_PATH")
    if configured_path:
        return Path(configured_path)
    return AppConfig().data_root / "models" / "structural_squeeze_model.json"


@lru_cache(maxsize=4)
def _load_structural_squeeze_payload(path_text: str) -> dict[str, Any] | None:
    path = Path(path_text)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {"feature_columns", "labels", "mean", "scale", "w1", "b1", "w2", "b2"}
    if not required <= set(payload):
        return None
    return payload


def load_structural_squeeze_artifact(
    model_path: str | None = None,
) -> StructuralSqueezeArtifact | None:
    payload = _load_structural_squeeze_payload(str(structural_squeeze_model_path(model_path)))
    if payload is None:
        return None
    try:
        return StructuralSqueezeArtifact(
            feature_columns=[str(item) for item in payload["feature_columns"]],
            labels=[str(item) for item in payload["labels"]],
            mean=[float(item) for item in payload["mean"]],
            scale=[float(item) for item in payload["scale"]],
            w1=[[float(value) for value in row] for row in payload["w1"]],
            b1=[float(item) for item in payload["b1"]],
            w2=[[float(value) for value in row] for row in payload["w2"]],
            b2=[float(item) for item in payload["b2"]],
            metrics=dict(payload.get("metrics") or {}),
        )
    except TypeError, ValueError:
        return None


def _window_range(bars: Sequence[Bar]) -> float:
    if not bars:
        return 0.0
    return max(float(bar.high) for bar in bars) - min(float(bar.low) for bar in bars)


def _true_range(bar: Bar, prev_close: float | None = None) -> float:
    high = float(bar.high)
    low = float(bar.low)
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _time_embeddings(bar: Bar) -> dict[str, float]:
    minute_of_day = bar.ts.hour * 60 + bar.ts.minute
    day_angle = 2.0 * np.pi * (minute_of_day / 1440.0)
    dow_angle = 2.0 * np.pi * (bar.ts.weekday() / 7.0)
    return {
        "tod_sin": float(np.sin(day_angle)),
        "tod_cos": float(np.cos(day_angle)),
        "dow_sin": float(np.sin(dow_angle)),
        "dow_cos": float(np.cos(dow_angle)),
    }


def structural_squeeze_features(
    bars: Sequence[Bar], *, index: int | None = None
) -> dict[str, float] | None:
    if len(bars) < 80:
        return None
    idx = len(bars) - 1 if index is None else int(index)
    if idx < 79 or idx >= len(bars):
        return None
    prefix = list(bars[: idx + 1])
    closes = [float(bar.close) for bar in prefix]
    atr = pine.atr_rma_series(prefix, 14)
    rvol = pine.relative_volume_series(prefix, 30)
    ema_fast = pine.ema_series(closes, 21)
    ema_slow = pine.ema_series(closes, 55)
    ema20 = pine.ema_series(closes, 20)
    ema200 = pine.ema_series(closes, 200)
    atr_value = max(float(atr[-1]), 0.000001)
    recent_20 = prefix[-20:]
    recent_60 = prefix[-60:]
    recent_12 = prefix[-12:]
    recent_36 = prefix[-36:]
    high_60 = max(float(bar.high) for bar in recent_60)
    low_60 = min(float(bar.low) for bar in recent_60)
    range_60 = max(high_60 - low_60, 0.000001)
    close = float(prefix[-1].close)
    volume_recent = sum(max(float(bar.volume), 0.0) for bar in recent_12) / 12.0
    volume_values_60 = [max(float(bar.volume), 0.0) for bar in recent_60]
    volume_base = sum(volume_values_60) / 60.0
    volume_std = float(np.std(volume_values_60)) if len(volume_values_60) > 1 else 0.0
    tr_values = [
        _true_range(prefix[item], float(prefix[item - 1].close) if item > 0 else None)
        for item in range(max(0, len(prefix) - 60), len(prefix))
    ]
    tr5 = sum(tr_values[-5:]) / max(len(tr_values[-5:]), 1)
    tr60 = sum(tr_values) / max(len(tr_values), 1)
    latest = prefix[-1]
    candle_range = max(float(latest.high) - float(latest.low), 0.000001)
    body = abs(float(latest.close) - float(latest.open))
    upper_wick = float(latest.high) - max(float(latest.open), float(latest.close))
    lower_wick = min(float(latest.open), float(latest.close)) - float(latest.low)
    features = {
        "range_atr_20": _safe_div(_window_range(recent_20), atr_value),
        "range_atr_60": _safe_div(_window_range(recent_60), atr_value),
        "atr_ratio": _safe_div(float(atr[-1]), max(float(atr[-20]), 0.000001), 1.0),
        "atr_5_60_ratio": _safe_div(tr5, max(tr60, 0.000001), 1.0),
        "rvol_30": float(rvol[-1]),
        "volume_z_60": _safe_div(
            max(float(latest.volume), 0.0) - volume_base, max(volume_std, 1.0)
        ),
        "ema_slope_atr": _safe_div(float(ema_fast[-1]) - float(ema_slow[-1]), atr_value),
        "ema20_distance_atr": _safe_div(close - float(ema20[-1]), atr_value),
        "ema55_distance_atr": _safe_div(close - float(ema_slow[-1]), atr_value),
        "ema200_distance_atr": _safe_div(close - float(ema200[-1]), atr_value),
        "close_pos_1": (close - float(latest.low)) / candle_range,
        "close_range_pos_60": (close - low_60) / range_60,
        "body_range_share": body / candle_range,
        "wick_balance": _safe_div(upper_wick - lower_wick, candle_range),
        "breakout_pressure": _safe_div(close - ((high_60 + low_60) / 2.0), range_60),
        "return_12_atr": _safe_div(close - float(recent_12[0].close), atr_value),
        "return_36_atr": _safe_div(close - float(recent_36[0].close), atr_value),
        "volume_trend": _safe_div(volume_recent, max(volume_base, 1.0), 1.0),
    }
    return {**features, **_time_embeddings(latest)}


def _rows_to_matrix(rows: Sequence[dict[str, Any]], feature_columns: Sequence[str]) -> np.ndarray:
    matrix: list[list[float]] = []
    for row in rows:
        matrix.append([float(row[column]) for column in feature_columns])
    if not matrix:
        raise ValueError("dataset is empty")
    return np.asarray(matrix, dtype=np.float64)


def _predict_probs(
    artifact: StructuralSqueezeArtifact, features: dict[str, float]
) -> dict[str, float] | None:
    try:
        x = _rows_to_matrix([features], artifact.feature_columns)
    except KeyError, TypeError, ValueError:
        return None
    mean = np.asarray(artifact.mean, dtype=np.float64)
    scale = np.asarray(artifact.scale, dtype=np.float64)
    w1 = np.asarray(artifact.w1, dtype=np.float64)
    b1 = np.asarray(artifact.b1, dtype=np.float64)
    w2 = np.asarray(artifact.w2, dtype=np.float64)
    b2 = np.asarray(artifact.b2, dtype=np.float64)
    if x.shape[1] != len(mean) or len(mean) != len(scale):
        return None
    hidden = np.tanh(((x - mean) / np.where(scale > 1e-9, scale, 1.0)) @ w1 + b1)
    probs = softmax_rows(hidden @ w2 + b2)[0]
    return {label: float(probs[index]) for index, label in enumerate(artifact.labels)}


def structural_squeeze_bias(
    bars: Sequence[Bar],
    *,
    enabled: bool = True,
    model_path: str | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}
    features = structural_squeeze_features(bars)
    if features is None:
        return {"enabled": False, "reason": "not_enough_bars"}
    artifact = load_structural_squeeze_artifact(model_path)
    probabilities = _predict_probs(artifact, features) if artifact is not None else None
    if probabilities is None:
        return {
            "enabled": False,
            "reason": "model_unavailable",
            "source": "unavailable",
            "features": {key: round(float(value), 6) for key, value in features.items()},
            "model_metrics": artifact.metrics if artifact is not None else {},
        }
    neutral = probabilities.get("neutral", 0.0)
    mean_reversion = probabilities.get("mean_reversion", 0.0)
    breakout_up = probabilities.get("breakout_up", 0.0)
    breakout_down = probabilities.get("breakout_down", 0.0)
    breakout_probability = breakout_up + breakout_down
    direction_bias = max(-1.0, min(breakout_up - breakout_down, 1.0))
    vol_multiplier = max(
        0.70, min(1.0 + breakout_probability * 0.85 + (1.0 - neutral) * 0.18, 2.25)
    )
    recommended_engine = (
        "gbm"
        if breakout_probability >= 0.52
        else "mean_reversion"
        if mean_reversion >= 0.36
        else "returns"
    )
    dominant = max(probabilities, key=lambda key: probabilities[key])
    return {
        "enabled": True,
        "source": "trained_artifact",
        "dominant": dominant,
        "recommended_engine": recommended_engine,
        "vol_multiplier": round(vol_multiplier, 6),
        "direction_bias": round(direction_bias, 6),
        "squeeze_probability": round(breakout_probability, 6),
        "mean_reversion": round(mean_reversion, 6),
        "neutral": round(neutral, 6),
        "breakout_up": round(breakout_up, 6),
        "breakout_down": round(breakout_down, 6),
        "features": {key: round(float(value), 6) for key, value in features.items()},
        "model_metrics": artifact.metrics if artifact is not None else {},
    }


def label_structural_squeeze(
    bars: Sequence[Bar],
    *,
    index: int,
    horizon: int = 24,
    breakout_atr: float = 1.35,
    reversion_atr: float = 0.55,
    reversion_stretch_atr: float = 1.20,
    reversion_touch_atr: float = 0.35,
    reversion_edge_pos: float = 0.82,
    max_premove_return_atr: float = 0.95,
    max_premove_atr_ratio: float = 1.85,
) -> str | None:
    if index < 79 or index + horizon >= len(bars):
        return None
    features = structural_squeeze_features(bars, index=index)
    if features is None:
        return None
    prefix = list(bars[: index + 1])
    atr_value = max(float(pine.atr_rma_series(prefix, 14)[-1]), 0.000001)
    start = float(bars[index].close)
    future = bars[index + 1 : index + horizon + 1]
    future_high = max(float(bar.high) for bar in future)
    future_low = min(float(bar.low) for bar in future)
    up_move = (future_high - start) / atr_value
    down_move = (start - future_low) / atr_value
    ema55_distance = safe_float(features.get("ema55_distance_atr"), 0.0)
    ema55_value = start - ema55_distance * atr_value
    close_range_pos = safe_float(features.get("close_range_pos_60"), 0.5)
    returned_from_up_stretch = (
        ema55_distance >= reversion_stretch_atr
        and (
            future_low <= ema55_value + atr_value * reversion_touch_atr
            or down_move >= reversion_atr
        )
        and down_move >= up_move * 0.75
    )
    returned_from_down_stretch = (
        ema55_distance <= -reversion_stretch_atr
        and (
            future_high >= ema55_value - atr_value * reversion_touch_atr or up_move >= reversion_atr
        )
        and up_move >= down_move * 0.75
    )
    rejected_local_upper_edge = (
        close_range_pos >= reversion_edge_pos
        and down_move >= reversion_atr
        and down_move >= up_move * 0.70
    )
    reclaimed_local_lower_edge = (
        close_range_pos <= 1.0 - reversion_edge_pos
        and up_move >= reversion_atr
        and up_move >= down_move * 0.70
    )
    if (
        returned_from_up_stretch
        or returned_from_down_stretch
        or rejected_local_upper_edge
        or reclaimed_local_lower_edge
    ):
        return "mean_reversion"
    already_impulsive = (
        abs(safe_float(features.get("return_12_atr"), 0.0)) > max_premove_return_atr
        or safe_float(features.get("atr_5_60_ratio"), 1.0) > max_premove_atr_ratio
    )
    if not already_impulsive and up_move >= breakout_atr and up_move >= down_move * 1.10:
        return "breakout_up"
    if not already_impulsive and down_move >= breakout_atr and down_move >= up_move * 1.10:
        return "breakout_down"
    return "neutral"


def structural_squeeze_labeled_rows(
    bars: Sequence[Bar],
    *,
    horizon: int = 24,
    breakout_atr: float = 1.35,
    reversion_atr: float = 0.55,
    reversion_stretch_atr: float = 1.20,
    reversion_touch_atr: float = 0.35,
    reversion_edge_pos: float = 0.82,
    max_premove_return_atr: float = 0.95,
    max_premove_atr_ratio: float = 1.85,
    start_index: int = 80,
) -> list[dict[str, float | str]]:
    if len(bars) < max(80, horizon + 81):
        return []
    closes = [float(bar.close) for bar in bars]
    atr = pine.atr_rma_series(bars, 14)
    rvol = pine.relative_volume_series(bars, 30)
    ema_fast = pine.ema_series(closes, 21)
    ema_slow = pine.ema_series(closes, 55)
    ema20 = pine.ema_series(closes, 20)
    ema200 = pine.ema_series(closes, 200)
    true_ranges = [
        _true_range(bar, float(bars[index - 1].close) if index > 0 else None)
        for index, bar in enumerate(bars)
    ]
    rows: list[dict[str, float | str]] = []
    first = max(int(start_index), 79)
    last = len(bars) - int(horizon) - 1
    for idx in range(first, max(first, last + 1)):
        atr_value = max(float(atr[idx]), 0.000001)
        recent_20 = bars[idx - 19 : idx + 1]
        recent_60 = bars[idx - 59 : idx + 1]
        recent_12 = bars[idx - 11 : idx + 1]
        recent_36 = bars[idx - 35 : idx + 1]
        high_60 = max(float(bar.high) for bar in recent_60)
        low_60 = min(float(bar.low) for bar in recent_60)
        range_60 = max(high_60 - low_60, 0.000001)
        close = float(bars[idx].close)
        volume_recent = sum(max(float(bar.volume), 0.0) for bar in recent_12) / 12.0
        volume_values_60 = [max(float(bar.volume), 0.0) for bar in recent_60]
        volume_base = sum(volume_values_60) / 60.0
        volume_std = float(np.std(volume_values_60)) if len(volume_values_60) > 1 else 0.0
        tr_values = true_ranges[idx - 59 : idx + 1]
        tr5 = sum(tr_values[-5:]) / max(len(tr_values[-5:]), 1)
        tr60 = sum(tr_values) / max(len(tr_values), 1)
        latest = bars[idx]
        candle_range = max(float(latest.high) - float(latest.low), 0.000001)
        body = abs(float(latest.close) - float(latest.open))
        upper_wick = float(latest.high) - max(float(latest.open), float(latest.close))
        lower_wick = min(float(latest.open), float(latest.close)) - float(latest.low)
        future = bars[idx + 1 : idx + int(horizon) + 1]
        future_high = max(float(bar.high) for bar in future)
        future_low = min(float(bar.low) for bar in future)
        up_move = (future_high - close) / atr_value
        down_move = (close - future_low) / atr_value
        ema55_distance = _safe_div(close - float(ema_slow[idx]), atr_value)
        ema55_value = close - ema55_distance * atr_value
        returned_from_up_stretch = (
            ema55_distance >= reversion_stretch_atr
            and (
                future_low <= ema55_value + atr_value * reversion_touch_atr
                or down_move >= reversion_atr
            )
            and down_move >= up_move * 0.75
        )
        returned_from_down_stretch = (
            ema55_distance <= -reversion_stretch_atr
            and (
                future_high >= ema55_value - atr_value * reversion_touch_atr
                or up_move >= reversion_atr
            )
            and up_move >= down_move * 0.75
        )
        close_range_pos = (close - low_60) / range_60
        rejected_local_upper_edge = (
            close_range_pos >= reversion_edge_pos
            and down_move >= reversion_atr
            and down_move >= up_move * 0.70
        )
        reclaimed_local_lower_edge = (
            close_range_pos <= 1.0 - reversion_edge_pos
            and up_move >= reversion_atr
            and up_move >= down_move * 0.70
        )
        already_impulsive = (
            abs(_safe_div(close - float(recent_12[0].close), atr_value)) > max_premove_return_atr
            or _safe_div(tr5, max(tr60, 0.000001), 1.0) > max_premove_atr_ratio
        )
        if (
            returned_from_up_stretch
            or returned_from_down_stretch
            or rejected_local_upper_edge
            or reclaimed_local_lower_edge
        ):
            label = "mean_reversion"
        elif not already_impulsive and up_move >= breakout_atr and up_move >= down_move * 1.10:
            label = "breakout_up"
        elif not already_impulsive and down_move >= breakout_atr and down_move >= up_move * 1.10:
            label = "breakout_down"
        else:
            label = "neutral"
        features = {
            "range_atr_20": _safe_div(_window_range(recent_20), atr_value),
            "range_atr_60": _safe_div(_window_range(recent_60), atr_value),
            "atr_ratio": _safe_div(
                float(atr[idx]), max(float(atr[max(0, idx - 19)]), 0.000001), 1.0
            ),
            "atr_5_60_ratio": _safe_div(tr5, max(tr60, 0.000001), 1.0),
            "rvol_30": float(rvol[idx]),
            "volume_z_60": _safe_div(
                max(float(latest.volume), 0.0) - volume_base, max(volume_std, 1.0)
            ),
            "ema_slope_atr": _safe_div(float(ema_fast[idx]) - float(ema_slow[idx]), atr_value),
            "ema20_distance_atr": _safe_div(close - float(ema20[idx]), atr_value),
            "ema55_distance_atr": _safe_div(close - float(ema_slow[idx]), atr_value),
            "ema200_distance_atr": _safe_div(close - float(ema200[idx]), atr_value),
            "close_pos_1": (close - float(latest.low)) / candle_range,
            "close_range_pos_60": (close - low_60) / range_60,
            "body_range_share": body / candle_range,
            "wick_balance": _safe_div(upper_wick - lower_wick, candle_range),
            "breakout_pressure": _safe_div(close - ((high_60 + low_60) / 2.0), range_60),
            "return_12_atr": _safe_div(close - float(recent_12[0].close), atr_value),
            "return_36_atr": _safe_div(close - float(recent_36[0].close), atr_value),
            "volume_trend": _safe_div(volume_recent, max(volume_base, 1.0), 1.0),
            **_time_embeddings(latest),
        }
        rows.append({**features, "label": label})
        rows[-1]["index"] = idx
    return rows


def train_structural_squeeze_network(
    rows: Sequence[dict[str, Any]],
    *,
    feature_columns: Sequence[str] | None = None,
    hidden_units: int = 18,
    epochs: int = 220,
    learning_rate: float = 0.035,
    l2: float = 0.0006,
    validation_fraction: float = 0.20,
    seed: int = 17,
    neutral_downsample: float = 1.0,
    focal_gamma: float = 0.0,
) -> StructuralSqueezeArtifact:
    if len(rows) < 80:
        raise ValueError("at least 80 labeled rows are required")
    columns = list(feature_columns or STRUCTURAL_SQUEEZE_FEATURES)
    x = _rows_to_matrix(rows, columns)
    label_to_index = {label: index for index, label in enumerate(STRUCTURAL_SQUEEZE_LABELS)}
    y = np.asarray([label_to_index[str(row["label"])] for row in rows], dtype=np.int64)
    mean = x.mean(axis=0)
    scale = np.where(x.std(axis=0) > 1e-9, x.std(axis=0), 1.0)
    x = (x - mean) / scale
    validation_size = int(round(len(x) * max(0.0, min(validation_fraction, 0.45))))
    validation_size = max(1, validation_size)
    split = len(x) - validation_size
    x_train, y_train = x[:split], y[:split]
    x_val, y_val = x[split:], y[split:]
    rng = np.random.default_rng(seed)
    neutral_index = label_to_index["neutral"]
    keep_neutral = max(0.05, min(float(neutral_downsample), 1.0))
    if keep_neutral < 1.0:
        train_keep = (y_train != neutral_index) | (rng.random(len(y_train)) <= keep_neutral)
        if np.any(train_keep):
            x_train = x_train[train_keep]
            y_train = y_train[train_keep]
    w1 = rng.normal(0.0, 1.0 / np.sqrt(x.shape[1]), size=(x.shape[1], hidden_units))
    b1 = np.zeros(hidden_units)
    w2 = rng.normal(
        0.0, 1.0 / np.sqrt(hidden_units), size=(hidden_units, len(STRUCTURAL_SQUEEZE_LABELS))
    )
    b2 = np.zeros(len(STRUCTURAL_SQUEEZE_LABELS))
    target = np.zeros((len(y_train), len(STRUCTURAL_SQUEEZE_LABELS)), dtype=np.float64)
    target[np.arange(len(y_train)), y_train] = 1.0
    counts = np.bincount(y_train, minlength=len(STRUCTURAL_SQUEEZE_LABELS)).astype(np.float64)
    class_weights = len(y_train) / np.maximum(counts, 1.0) / len(STRUCTURAL_SQUEEZE_LABELS)
    sample_weights = class_weights[y_train][:, None]
    final_loss = 0.0
    gamma = max(0.0, min(float(focal_gamma), 5.0))
    for _ in range(max(int(epochs), 1)):
        hidden = np.tanh(x_train @ w1 + b1)
        probs = softmax_rows(hidden @ w2 + b2)
        pt = probs[np.arange(len(y_train)), y_train][:, None]
        focal_weights = (1.0 - pt) ** gamma if gamma > 0.0 else 1.0
        error = (probs - target) * sample_weights * focal_weights / len(x_train)
        grad_w2 = hidden.T @ error + l2 * w2
        grad_b2 = error.sum(axis=0)
        grad_hidden = (error @ w2.T) * (1.0 - hidden**2)
        grad_w1 = x_train.T @ grad_hidden + l2 * w1
        grad_b1 = grad_hidden.sum(axis=0)
        w2 -= learning_rate * grad_w2
        b2 -= learning_rate * grad_b2
        w1 -= learning_rate * grad_w1
        b1 -= learning_rate * grad_b1
        ce = -np.log(np.maximum(probs[np.arange(len(y_train)), y_train], 1e-12))
        final_loss = float(
            np.mean(ce * np.ravel(focal_weights if gamma > 0.0 else np.ones((len(y_train), 1))))
        )

    def accuracy(features: np.ndarray, labels: np.ndarray) -> float:
        if len(labels) <= 0:
            return 0.0
        probs = softmax_rows(np.tanh(features @ w1 + b1) @ w2 + b2)
        return float(np.mean(np.argmax(probs, axis=1) == labels))

    def per_class_accuracy(features: np.ndarray, labels: np.ndarray) -> dict[str, float]:
        if len(labels) <= 0:
            return {label: 0.0 for label in STRUCTURAL_SQUEEZE_LABELS}
        preds = np.argmax(
            softmax_rows(np.tanh(features @ w1 + b1) @ w2 + b2),
            axis=1,
        )
        out: dict[str, float] = {}
        for index, label in enumerate(STRUCTURAL_SQUEEZE_LABELS):
            mask = labels == index
            out[label] = round(
                float(np.mean(preds[mask] == labels[mask])) if np.any(mask) else 0.0, 6
            )
        return out

    validation_per_class = per_class_accuracy(x_val, y_val)

    metrics = {
        "rows": int(len(x)),
        "train_rows": int(split),
        "effective_train_rows": int(len(x_train)),
        "validation_rows": int(len(x_val)),
        "epochs": int(epochs),
        "hidden_units": int(hidden_units),
        "neutral_downsample": round(keep_neutral, 6),
        "focal_gamma": round(gamma, 6),
        "loss": round(final_loss, 6),
        "train_accuracy": round(accuracy(x_train, y_train), 6),
        "validation_accuracy": round(accuracy(x_val, y_val), 6),
        "validation_balanced_accuracy": round(
            sum(validation_per_class.values()) / len(validation_per_class), 6
        ),
        "validation_per_class_accuracy": validation_per_class,
        "class_distribution": {
            label: int(np.sum(y == index)) for index, label in enumerate(STRUCTURAL_SQUEEZE_LABELS)
        },
        "train_class_distribution": {
            label: int(np.sum(y_train == index))
            for index, label in enumerate(STRUCTURAL_SQUEEZE_LABELS)
        },
        "validation_class_distribution": {
            label: int(np.sum(y_val == index))
            for index, label in enumerate(STRUCTURAL_SQUEEZE_LABELS)
        },
    }
    return StructuralSqueezeArtifact(
        feature_columns=columns,
        labels=list(STRUCTURAL_SQUEEZE_LABELS),
        mean=mean.round(10).tolist(),
        scale=scale.round(10).tolist(),
        w1=w1.round(10).tolist(),
        b1=b1.round(10).tolist(),
        w2=w2.round(10).tolist(),
        b2=b2.round(10).tolist(),
        metrics=metrics,
    )
