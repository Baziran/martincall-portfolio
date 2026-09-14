from __future__ import annotations

from aef_terminal.indicators.defaults import ScoreDefaults, score_action

LINDA_ACTION_CONTRACT_VERSION = "linda-action-v1"
LINDA_CONFIRMATION_CODES = frozenset({"NO_SUPPLY", "NO_DEMAND", "VW_RECLAIM", "VW_REJECT"})
LINDA_PULLBACK_CODES = frozenset({"PB_UP", "PB_DN"})


def linda_decision_action(
    *,
    code: str,
    score: float,
    min_label_score: int,
    bands: ScoreDefaults,
    blocked: bool = False,
) -> str:
    """Production Linda action ladder (playbook setups are excluded)."""
    if blocked:
        return "BLOCK"
    important = score >= min_label_score
    if code in LINDA_CONFIRMATION_CODES:
        return "ARM" if important else "WATCH"
    if code in LINDA_PULLBACK_CODES:
        return "GO" if important else "WATCH"
    tier = score_action(score, bands)
    if important and tier in {"ARM", "GO"}:
        return tier
    if tier in {"WATCH", "CANDIDATE", "ARM", "GO"}:
        return "WATCH"
    return "WAIT"
