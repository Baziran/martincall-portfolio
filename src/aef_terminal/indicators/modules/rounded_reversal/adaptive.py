"""Deterministic adaptive scoring shared by Rounded Reversal tracks."""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.price_action import candle_anatomies
from aef_terminal.runtime import pine


def sensitivity_value(
    sensitivity: str,
    *,
    early: float,
    balanced: float,
    strict: float,
) -> float:
    """Resolve one versioned preset without hidden mutable calibration."""

    return {
        "early": float(early),
        "balanced": float(balanced),
        "strict": float(strict),
    }.get(str(sensitivity).strip().lower(), float(balanced))


def soft_up(value: float, target: float, margin: float) -> float:
    """Return continuous membership that reaches full weight at ``target``."""

    resolved_margin = max(float(margin), 1e-9)
    return pine.clamp(
        (float(value) - float(target) + resolved_margin) / resolved_margin,
        0.0,
        1.0,
    )


def soft_down(value: float, target: float, margin: float) -> float:
    """Return continuous membership for values whose target is an upper bound."""

    resolved_margin = max(float(margin), 1e-9)
    return pine.clamp(
        (float(target) + resolved_margin - float(value)) / resolved_margin,
        0.0,
        1.0,
    )


def adaptive_rejection_summary(
    bars: Sequence[Bar],
    *,
    atr_values: Sequence[float],
    apex_price: float,
    apex_atr: float,
    tick_size: float,
    target_wick_share: float,
    target_wick_atr: float,
    target_close_pos: float,
    level_tolerance_atr: float,
    level_tolerance_ticks: int,
    target_mass: float,
    max_scored_bars: int,
    cluster_threshold: float,
    strong_bar_threshold: float,
    min_support_bars: int,
    source_timeframe: str,
) -> dict[str, Any]:
    """Score a rejection auction without cliff-edge candle qualification.

    Margins are derived only from the confirmed input prefix, tick size, and ATR.
    A one-tick miss therefore loses proportional weight instead of becoming zero.
    """

    if (
        not bars
        or len(atr_values) != len(bars)
        or not isfinite(float(apex_price))
        or not isfinite(float(apex_atr))
        or float(apex_atr) <= 0
    ):
        return {
            "qualified": False,
            "count": 0,
            "observation_ts": [],
            "ready_observation_ts": None,
            "ready_local_index": None,
            "first_ts": None,
            "last_ts": None,
            "mean_upper_share": 0.0,
            "max_upper_wick_atr": 0.0,
            "band_width_atr": 0.0,
            "zone_top": round(float(apex_price), 6),
            "zone_bottom": round(
                float(apex_price)
                - max(
                    float(tick_size) * int(level_tolerance_ticks),
                    float(apex_atr) * float(level_tolerance_atr),
                ),
                6,
            ),
            "source_timeframe": source_timeframe,
            "evidence_mass": 0.0,
            "target_mass": round(float(target_mass), 4),
            "cluster_score": 0.0,
            "strongest_bar_score": 0.0,
            "support_bar_count": 0,
            "adaptive_thresholds": {
                "cluster_score": round(float(cluster_threshold), 4),
                "strong_bar_score": round(float(strong_bar_threshold), 4),
                "support_bar_score": round(max(float(strong_bar_threshold) * 0.70, 0.35), 4),
                "min_support_bars": max(int(min_support_bars), 1),
            },
            "observations": [],
        }

    anatomies = candle_anatomies(bars)
    tolerance = max(
        float(tick_size) * int(level_tolerance_ticks),
        float(apex_atr) * float(level_tolerance_atr),
    )
    # Preserve one full tick of graded evidence outside the admitted level band.
    # A discrete market cannot express a sub-tick near miss, so a one-tick fade
    # would recreate the same binary cliff this scorer is intended to remove.
    proximity_fade = max(float(tick_size) * 2.0, float(apex_atr) * 0.05)
    scored: list[dict[str, Any]] = []
    for index, (bar, anatomy, raw_atr) in enumerate(zip(bars, anatomies, atr_values, strict=True)):
        atr = max(float(raw_atr), 1e-9)
        candle_range = max(float(bar.high) - float(bar.low), float(tick_size), 1e-9)
        share_margin = pine.clamp(float(tick_size) / candle_range, 0.02, 0.12)
        atr_margin = pine.clamp(float(tick_size) / atr, 0.02, 0.10)
        distance = abs(float(bar.high) - float(apex_price))
        proximity = (
            1.0
            if distance <= tolerance
            else pine.clamp(
                1.0 - (distance - tolerance) / proximity_fade,
                0.0,
                1.0,
            )
        )
        share_score = soft_up(
            anatomy.upper_share,
            float(target_wick_share),
            share_margin,
        )
        wick_atr = anatomy.upper_wick / atr
        wick_score = soft_up(
            wick_atr,
            float(target_wick_atr),
            atr_margin,
        )
        close_score = soft_down(
            anatomy.close_pos,
            float(target_close_pos),
            share_margin,
        )
        wick_core = 0.55 * share_score + 0.45 * wick_score
        bar_score = (
            proximity * wick_core * (0.70 + 0.30 * close_score) if anatomy.upper_wick > 0 else 0.0
        )
        scored.append(
            {
                "index": index,
                "ts": bar.ts.isoformat(),
                "score": round(bar_score, 6),
                "upper_share": round(anatomy.upper_share, 6),
                "upper_wick_atr": round(wick_atr, 6),
                "close_pos": round(anatomy.close_pos, 6),
                "distance_atr": round(distance / max(float(apex_atr), 1e-9), 6),
                "proximity": round(proximity, 6),
                "share_margin": round(share_margin, 6),
                "atr_margin": round(atr_margin, 6),
            }
        )

    limit = max(int(max_scored_bars), 1)
    target = max(float(target_mass), 1e-9)
    support_threshold = max(float(strong_bar_threshold) * 0.70, 0.35)
    required_support = max(int(min_support_bars), 1)

    def prefix_state(end_index: int) -> tuple[float, float, float, int]:
        prefix_scores = sorted(
            (float(item["score"]) for item in scored[: end_index + 1]),
            reverse=True,
        )[:limit]
        mass = sum(prefix_scores)
        strongest = max(prefix_scores, default=0.0)
        score = pine.clamp(mass / target, 0.0, 1.0)
        support_count = sum(value >= support_threshold for value in prefix_scores)
        return mass, strongest, score, support_count

    ready_local_index: int | None = None
    for index in range(len(scored)):
        _mass, strongest, score, support_count = prefix_state(index)
        if (
            score >= float(cluster_threshold)
            and strongest >= float(strong_bar_threshold)
            and support_count >= required_support
        ):
            ready_local_index = index
            break

    evidence_mass, strongest_score, cluster_score, support_bar_count = prefix_state(len(scored) - 1)
    qualified = bool(
        cluster_score >= float(cluster_threshold)
        and strongest_score >= float(strong_bar_threshold)
        and support_bar_count >= required_support
    )
    selected = sorted(
        scored,
        key=lambda item: (float(item["score"]), int(item["index"])),
        reverse=True,
    )[:limit]
    material = sorted(
        (item for item in selected if float(item["score"]) >= 0.20),
        key=lambda item: int(item["index"]),
    )
    zone_items = material
    zone_top = (
        max(float(bars[int(item["index"])].high) for item in zone_items)
        if zone_items
        else float(apex_price)
    )
    zone_bottom = (
        max(
            max(
                float(bars[int(item["index"])].open),
                float(bars[int(item["index"])].close),
            )
            for item in zone_items
        )
        if zone_items
        else float(apex_price) - tolerance
    )
    zone_bottom = min(zone_bottom, zone_top - max(float(tick_size), 1e-9))
    highs = [float(bars[int(item["index"])].high) for item in material]
    return {
        "qualified": qualified,
        "count": len(material),
        "observation_ts": [str(item["ts"]) for item in material],
        "ready_observation_ts": (
            bars[ready_local_index].ts.isoformat() if ready_local_index is not None else None
        ),
        "ready_local_index": ready_local_index,
        "first_ts": str(material[0]["ts"]) if material else None,
        "last_ts": str(material[-1]["ts"]) if material else None,
        "mean_upper_share": round(
            sum(float(item["upper_share"]) for item in material) / len(material),
            4,
        )
        if material
        else 0.0,
        "max_upper_wick_atr": round(
            max((float(item["upper_wick_atr"]) for item in material), default=0.0),
            4,
        ),
        "band_width_atr": round(
            (max(highs) - min(highs)) / max(float(apex_atr), 1e-9),
            4,
        )
        if len(highs) >= 2
        else 0.0,
        "zone_top": round(zone_top, 6),
        "zone_bottom": round(zone_bottom, 6),
        "source_timeframe": source_timeframe,
        "evidence_mass": round(evidence_mass, 4),
        "target_mass": round(target, 4),
        "cluster_score": round(cluster_score, 4),
        "strongest_bar_score": round(strongest_score, 4),
        "support_bar_count": support_bar_count,
        "adaptive_thresholds": {
            "cluster_score": round(float(cluster_threshold), 4),
            "strong_bar_score": round(float(strong_bar_threshold), 4),
            "support_bar_score": round(support_threshold, 4),
            "min_support_bars": required_support,
            "wick_share_target": round(float(target_wick_share), 4),
            "wick_atr_target": round(float(target_wick_atr), 4),
            "close_pos_target": round(float(target_close_pos), 4),
            "level_tolerance": round(tolerance, 6),
            "proximity_fade": round(proximity_fade, 6),
        },
        "observations": [
            {
                "local_index": int(item["index"]),
                **{key: value for key, value in item.items() if key != "index"},
            }
            for item in material
        ],
    }


__all__ = [
    "adaptive_rejection_summary",
    "sensitivity_value",
    "soft_down",
    "soft_up",
]
