from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aef_terminal.runtime.math_utils import float_or_none

PLAYBOOK_CONTRACT_VERSION = "playbook-decision-v1"
PLAYBOOK_DEFAULT_DECISION_ELIGIBLE = True

PLAYBOOK_FADE_CODES = frozenset(
    {
        "TS_DN",
        "TS_UP",
        "TS1_DN",
        "TS1_UP",
        "8020_DN",
        "8020_UP",
        "GAP_UP",
        "GAP_DN",
    }
)
PLAYBOOK_CONTINUATION_CODES = frozenset(
    {
        "ANTI_UP",
        "ANTI_DN",
        "PIN_UP",
        "PIN_DN",
        "SQZ_UP",
        "SQZ_DN",
    }
)
PRODUCTION_FADE_CODES = frozenset({"NO_SUPPLY", "NO_DEMAND", "VW_RECLAIM", "VW_REJECT"})
PRODUCTION_CONTINUATION_CODES = frozenset({"PB_UP", "PB_DN"})

# Production Linda structures that should suppress a same-direction playbook fade/transit.
PRODUCTION_LINDA_DEDUPE_CODES = frozenset(
    {
        "GRAIL_BUY",
        "GRAIL_SELL",
        "GRAIL_BUY_WATCH",
        "GRAIL_SELL_WATCH",
        "PB_UP",
        "PB_DN",
    }
)

# Production Linda paths that already participate in decision.py today.
PRODUCTION_LINDA_SOURCES = frozenset({"linda_volume", "linda_continuation", "linda_fade"})

PLAYBOOK_ROLE_PREFIX = "linda_setup_"

# Short Rashke playbook guide keyed by setup code (shown in UI tooltips/panel).
PLAYBOOK_SETUP_GUIDE: dict[str, str] = {
    "TS_DN": "Turtle Soup: false breakout above the range high, fade back inside.",
    "TS_UP": "Turtle Soup: false breakdown below the range low, fade back inside.",
    "TS1_DN": "Turtle Soup +1: breakout day failed, reclaim on the next bar.",
    "TS1_UP": "Turtle Soup +1: breakdown day failed, reclaim on the next bar.",
    "8020_DN": "80-20: prior bar closed in the upper 20% of its range, fade next session.",
    "8020_UP": "80-20: prior bar closed in the lower 20% of its range, fade next session.",
    "ANTI_UP": "The Anti: 3/10 oscillator hooks up with higher-timeframe bull trend.",
    "ANTI_DN": "The Anti: 3/10 oscillator hooks down with higher-timeframe bear trend.",
    "PIN_UP": "Momentum Pinball: extreme RSI(3) of ROC, break above first-hour high.",
    "PIN_DN": "Momentum Pinball: extreme RSI(3) of ROC, break below first-hour low.",
    "SQZ_UP": "HV Squeeze: volatility collapse, upside range break.",
    "SQZ_DN": "HV Squeeze: volatility collapse, downside range break.",
    "GAP_UP": "ADX Gapper: gap down against strong bear trend, fade toward prior close.",
    "GAP_DN": "ADX Gapper: gap up against strong bull trend, fade toward prior close.",
}


def playbook_theory_summary(code: str, fallback: str = "") -> str:
    return PLAYBOOK_SETUP_GUIDE.get(str(code or "").strip(), fallback)


def playbook_signal_name(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if normalized in PLAYBOOK_CONTINUATION_CODES | PRODUCTION_CONTINUATION_CODES:
        return "linda_continuation"
    if normalized in PLAYBOOK_FADE_CODES | PRODUCTION_FADE_CODES:
        return "linda_fade"
    return "linda_volume"


def resolve_signal_name(source: str, code: str, action: str) -> str:
    del source, action
    return playbook_signal_name(code)


def playbook_scenario_kind(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if normalized in PLAYBOOK_CONTINUATION_CODES:
        return "transit"
    if normalized in PLAYBOOK_FADE_CODES:
        return "fade"
    return "wait"


@dataclass(frozen=True)
class PlaybookPromotionPolicy:
    """Gate for promoting Rashke playbook setups into the shared candidate pool."""

    decision_eligible: bool = PLAYBOOK_DEFAULT_DECISION_ELIGIBLE
    shadow_candidates: bool = False
    min_score: float = 68.0
    require_arm_or_go: bool = True
    dedupe_with_production: bool = True


DEFAULT_PLAYBOOK_POLICY = PlaybookPromotionPolicy()


def is_playbook_role(role: str) -> bool:
    return str(role or "").startswith(PLAYBOOK_ROLE_PREFIX)


def playbook_setup_metadata(setup: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": PLAYBOOK_CONTRACT_VERSION,
        "decision_eligible": DEFAULT_PLAYBOOK_POLICY.decision_eligible,
        "shadow_candidates": DEFAULT_PLAYBOOK_POLICY.shadow_candidates,
        "role": str(setup.get("role") or ""),
        "code": str(setup.get("code") or ""),
    }


def playbook_promotion_preview(
    setup: dict[str, Any],
    *,
    policy: PlaybookPromotionPolicy = DEFAULT_PLAYBOOK_POLICY,
    score_floor: float | None = None,
    production_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Non-mutating preview of whether a playbook could enter the candidate pool."""
    score = float_or_none(setup.get("score"))
    action = str(setup.get("action") or "WAIT").upper()
    code = str(setup.get("code") or "")
    direction = str(setup.get("direction") or "flat").lower()
    kind = playbook_scenario_kind(code)
    floor = float(score_floor if score_floor is not None else policy.min_score)
    reasons: list[str] = []
    if score is None:
        reasons.append("missing_score")
    if not policy.decision_eligible and not policy.shadow_candidates:
        reasons.append("playbook_isolated")
    if score is not None and score < floor:
        reasons.append("score_below_floor")
    if policy.require_arm_or_go and action not in {"ARM", "GO"}:
        reasons.append("action_not_arm_go")
    if policy.dedupe_with_production and production_keys and (direction, kind) in production_keys:
        reasons.append("production_direction_conflict")
    eligible = not reasons or (
        policy.shadow_candidates and "playbook_isolated" in reasons and len(reasons) == 1
    )
    return {
        "eligible": eligible and policy.decision_eligible,
        "shadow": eligible and policy.shadow_candidates,
        "reject_reasons": reasons,
        "policy": PLAYBOOK_CONTRACT_VERSION,
        "signal_name": playbook_signal_name(code),
        "kind": kind,
    }
