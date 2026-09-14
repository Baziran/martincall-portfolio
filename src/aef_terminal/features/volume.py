from collections.abc import Sequence
from math import exp, log
from typing import TypedDict

import numpy as np

from aef_terminal.domain import Bar
from aef_terminal.runtime.math_utils import exact_finite_number_or_none


class VolumeFeatures(TypedDict, total=False):
    volume: float
    rvol_mean: float
    relative_volume: float
    rvol: float
    rvol_geo: float
    vol_z: float
    volume_rank: float
    vol_rank: float
    rvol_adaptive: float


def resolve_rvol_context(features: dict[str, object]) -> tuple[float | None, str]:
    """Return canonical RVOL and the source key used.

    Priority intentionally favors adaptive RVOL so downstream modules (SMC/VSA/Linda)
    can rely on a consistent pressure metric when present.
    """
    candidates = (
        ("rvol_adaptive", features.get("rvol_adaptive")),
        ("rvol", features.get("rvol")),
        ("rvol_mean", features.get("rvol_mean")),
        ("relative_volume", features.get("relative_volume")),
    )
    for key, raw in candidates:
        value = exact_finite_number_or_none(raw)
        if value is not None and value >= 0.0:
            return value, key
    return None, "unavailable"


def volume_features(bars: Sequence[Bar], lookback: int = 30) -> VolumeFeatures:
    window = bars[-lookback:]
    if not window:
        return {"volume": 0.0, "rvol_mean": 1.0, "volume_rank": 50.0}

    volumes = np.array([bar.volume for bar in window], dtype=float)
    current = volumes[-1]

    # Обычный RVOL (отношение к среднему арифметическому)
    mean_vol = max(np.mean(volumes), 1.0)
    rvol_sma = current / mean_vol

    # Геометрический RVOL и Z-score (на основе логарифмов, как в vsa.py)
    log_volumes = np.log(np.maximum(volumes, 1.0))
    log_mean = np.mean(log_volumes)
    log_std = max(np.std(log_volumes), 0.05)

    rvol_geo = current / max(exp(log_mean), 1.0)
    vol_z = (log(max(current, 1.0)) - log_mean) / log_std

    # Процентильный ранг и Адаптивный RVOL (смешивание весов 0.45 / 0.35 / 0.20)
    rank = float((volumes <= current).mean() * 100.0)
    rank_rvol = 0.50 + rank / 100.0 * 1.75
    rvol_adaptive = (rvol_sma * 0.45) + (rvol_geo * 0.35) + (rank_rvol * 0.20)

    return {
        "volume": float(current),
        "rvol_mean": float(rvol_sma),
        "rvol_geo": float(rvol_geo),
        "vol_z": float(vol_z),
        "volume_rank": rank,
        "vol_rank": rank,
        "relative_volume": float(rvol_sma),
        "rvol": float(rvol_adaptive),
        "rvol_adaptive": float(rvol_adaptive),
    }


def adaptive_rvol_series(bars: Sequence[Bar], lookback: int = 30) -> list[float]:
    """Per-bar adaptive RVOL using the same blend as volume_features()."""
    if not bars:
        return []
    window_len = max(int(lookback), 1)
    out: list[float] = []
    for index in range(len(bars)):
        start = max(0, index - window_len + 1)
        features = volume_features(bars[start : index + 1], lookback=index - start + 1)
        value = exact_finite_number_or_none(features.get("rvol_adaptive"))
        if value is None:
            value = exact_finite_number_or_none(features.get("rvol"))
        out.append(1.0 if value is None else value)
    return out


def resolve_bar_rvol(
    index: int,
    *,
    adaptive_series: Sequence[float],
    features: dict[str, object] | None = None,
    is_latest: bool = False,
) -> tuple[float, str]:
    """Resolve RVOL for a bar index, preferring shared feature context on the latest bar."""
    fallback = float(adaptive_series[index]) if 0 <= index < len(adaptive_series) else 1.0
    if is_latest and features:
        value, source = resolve_rvol_context(features)
        if value is not None:
            return value, source
    return fallback, "rvol_adaptive_series"
