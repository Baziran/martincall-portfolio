from __future__ import annotations

from typing import Any


INDICATOR_SCORE_FAMILIES = frozenset(
    {"trend", "volume", "structure", "reversal", "options", "forecast", "decision"}
)


def score_float(value: Any, fallback: float) -> float:
    try:
        out = float(value)
    except TypeError, ValueError:
        return fallback
    if out <= 0.0 or out > 2.0:
        return fallback
    return out


def indicator_score_overrides(indicator_params: dict[str, Any] | None) -> dict[str, Any] | None:
    raw = indicator_params if isinstance(indicator_params, dict) else {}
    overrides = raw.get("indicator_scores")
    return overrides if isinstance(overrides, dict) else None


def _sample_size(value: Any) -> int | None:
    try:
        out = int(value)
    except TypeError, ValueError:
        return None
    if out <= 0:
        return None
    return out


def normalize_indicator_score_meta(
    indicator_id: str,
    *,
    seed_family: str,
    seed_power: float,
    seed_usefulness: float,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = overrides.get(indicator_id) if isinstance(overrides, dict) else None
    item = raw if isinstance(raw, dict) else {}
    empirical_power = score_float(item.get("empirical_power"), seed_power)
    usefulness = score_float(item.get("usefulness"), seed_usefulness)
    score_family = str(item.get("score_family") or seed_family)
    if score_family not in INDICATOR_SCORE_FAMILIES:
        score_family = seed_family
    sample_size = _sample_size(item.get("sample_size"))
    meta = {
        "score_family": score_family,
        "empirical_power": empirical_power,
        "usefulness": usefulness,
        "score_weight": round(empirical_power * usefulness, 4),
        "source": str(item.get("source") or ("runtime_override" if item else "empirical_seed")),
    }
    if sample_size is not None:
        meta["sample_size"] = sample_size
    return meta
