from __future__ import annotations

from typing import Any

# Calibrated from feb+apr 2026 baseline backtests (combined trades, n>=100).
# Higher weight = stronger pull on direction_sentiment score (long/short power).
DEFAULT_CANDIDATE_WEIGHT = 0.50

CANDIDATE_SOURCE_WEIGHTS: dict[str, float] = {
    "impulse_pullback": 1.15,  # 71.8% win
    "smc_structure": 0.95,  # 57.2% win
    "linda_continuation": 0.70,
    "setup_mean_reversion": 0.65,
    "w5_exhaustion": 0.60,  # 48.2% win
    "w5_structure": 0.60,
    "sfp": 0.50,
    "wolfe_confirmed": 0.28,  # 42.6% win, negative pts
    "wolfe_candidate": 0.32,
    "wolfe_path": 0.32,
    "linda_fade": 0.32,  # 36.8% win
    "setup_momentum_breakout": 0.38,
    "setup_momentum_breakdown": 0.38,
    "absorption_trap": 0.85,
}

INDICATOR_STATE_WEIGHTS: dict[str, float] = {
    "decision": 1.40,
    "market_spotlight": 1.00,
    "linda_volume": 0.75,
    "impulse_fib": 0.95,
    "smc_channels": 0.90,
    "w5_structure": 0.55,
    "wolfe_structure": 0.28,
    "absorption_trap": 0.85,
    "breakout_accumulation": 0.42,
    "tick_flow": 0.85,
    "institutional_edge": 2.50,
}

# Per-profile multipliers on candidate weights (research: ES/SPY/QQQ wolfe drag).
PROFILE_CANDIDATE_MULTIPLIERS: dict[str, dict[str, float]] = {
    "ES": {
        "wolfe_confirmed": 0.70,
        "wolfe_candidate": 0.75,
        "linda_fade": 0.75,
    },
    "SPY": {
        "wolfe_confirmed": 0.55,
        "wolfe_candidate": 0.60,
    },
    "QQQ": {
        "wolfe_confirmed": 0.55,
        "wolfe_candidate": 0.60,
        "impulse_pullback": 0.90,
    },
    "GOLD": {
        "wolfe_confirmed": 0.80,
        "linda_fade": 0.70,
    },
}


def resolve_indicator_weight(indicator_id: str) -> float:
    return float(INDICATOR_STATE_WEIGHTS.get(indicator_id, 0.62))


def resolve_candidate_weight(candidate_name: str, *, profile_key: str | None = None) -> float:
    source = str(candidate_name or "").strip().lower()
    base = float(CANDIDATE_SOURCE_WEIGHTS.get(source, DEFAULT_CANDIDATE_WEIGHT))
    key = str(profile_key or "").strip().upper()
    if key:
        mult = float(PROFILE_CANDIDATE_MULTIPLIERS.get(key, {}).get(source, 1.0))
        base *= mult
    return round(max(0.05, min(base, 2.5)), 3)


def weight_quality_tier(weight: float) -> str:
    if weight >= 1.0:
        return "high"
    if weight >= 0.65:
        return "medium"
    return "low"


def contributor_weight_meta(source: str, weight: float) -> dict[str, Any]:
    return {
        "weight": round(weight, 3),
        "weight_tier": weight_quality_tier(weight),
        "weight_source": source,
    }
