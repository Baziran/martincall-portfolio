from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Direction, DomainFact, ScenarioKind
from aef_terminal.engine.trade_setup import (
    trade_setup_execution_authority_ready,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.runtime.timeframes import parse_aware_utc_ts
from aef_terminal.signals.trade_plan import execution_plan_rejection


_EXECUTABLE_SETUP_KINDS = frozenset({ScenarioKind.FADE.value, ScenarioKind.TRANSIT.value})


@dataclass(frozen=True, slots=True)
class TradeSetupPlanProjection:
    """Pure typed trade-plan projection, without execution-time identity."""

    side: str
    setup: str
    quality: float
    entry: float
    stop: float
    target: float
    signal_source: str
    signal_source_label: str
    confluence_sources: tuple[dict[str, Any], ...]
    trigger_event: DomainFact
    action_card: dict[str, Any] | None
    plan: dict[str, Any]

    def as_backtest_plan(self) -> dict[str, Any]:
        return {
            "source": self.signal_source,
            "setup": self.setup,
            "side": self.side,
            "confidence": self.quality,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
        }


def trade_plan_projection_from_trade_setup(
    setup: Mapping[str, Any],
) -> TradeSetupPlanProjection | None:
    """Project an eligible GO setup without inventing execution identity or time."""

    if not trade_setup_execution_authority_ready(setup.get("execution_authority")):
        return None
    if setup.get("ok") is not True or setup.get("action") != "GO":
        return None
    side = setup.get("side")
    if side not in {"long", "short"}:
        return None
    setup_kind = setup.get("kind")
    if setup_kind not in _EXECUTABLE_SETUP_KINDS:
        return None
    plan = setup.get("plan") if isinstance(setup.get("plan"), dict) else {}
    entry = exact_finite_number_or_none(
        plan.get("entry") if plan.get("entry") is not None else plan.get("trigger")
    )
    stop = exact_finite_number_or_none(plan.get("stop"))
    target = exact_finite_number_or_none(plan.get("target"))
    if entry is None or stop is None or target is None:
        return None
    direction = Direction.LONG if side == "long" else Direction.SHORT
    if execution_plan_rejection(direction, fill_price=entry, stop=stop, target=target) is not None:
        return None
    signal_source = setup.get("signal_source")
    if (
        not isinstance(signal_source, str)
        or not signal_source
        or signal_source != signal_source.strip()
    ):
        return None
    raw_trigger_event = setup.get("trigger_event")
    if not isinstance(raw_trigger_event, dict):
        return None
    try:
        trigger_event = DomainFact.from_mapping(raw_trigger_event)
    except TypeError, ValueError:
        return None
    quality = exact_finite_number_or_none(setup.get("quality"))
    if quality is None:
        return None
    confluence: list[dict[str, Any]] = []
    for item in setup.get("confluence") if isinstance(setup.get("confluence"), list) else []:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if not isinstance(source, str) or not source:
            continue
        score = exact_finite_number_or_none(item.get("score"))
        if score is None:
            return None
        level_value = item.get("level")
        level = exact_finite_number_or_none(level_value)
        if level_value is not None and level is None:
            return None
        direction_value = item.get("direction")
        if direction_value is not None and direction_value not in {
            Direction.LONG.value,
            Direction.SHORT.value,
            Direction.FLAT.value,
        }:
            return None
        confluence.append(
            {
                "source": source,
                "score": score,
                "direction": direction_value,
                "level": level,
            }
        )
    action_card = setup.get("action_card")
    return TradeSetupPlanProjection(
        side=side,
        setup=setup_kind,
        quality=quality,
        entry=entry,
        stop=stop,
        target=target,
        signal_source=signal_source,
        signal_source_label=signal_source,
        confluence_sources=tuple(confluence),
        trigger_event=trigger_event,
        action_card=dict(action_card) if isinstance(action_card, dict) else None,
        plan={**plan, "entry": entry, "stop": stop, "target": target},
    )


def execution_intent_from_trade_setup(
    setup: Mapping[str, Any],
    *,
    bar_ts: str,
    analysis_generation: str = "",
) -> dict[str, Any] | None:
    projection = trade_plan_projection_from_trade_setup(setup)
    if projection is None or not isinstance(bar_ts, str):
        return None
    analysis_bar_dt = parse_aware_utc_ts(bar_ts)
    generation = analysis_generation.strip() if isinstance(analysis_generation, str) else ""
    if analysis_bar_dt is None or not generation:
        return None
    canonical_bar_ts = analysis_bar_dt.isoformat()
    return {
        "source": "trade_setup",
        "action": "GO",
        "side": projection.side,
        "direction": projection.side,
        "entry": projection.entry,
        "planned_entry": projection.entry,
        "stop": projection.stop,
        "target": projection.target,
        "ts": canonical_bar_ts,
        "analysis_bar_ts": canonical_bar_ts,
        "analysis_generation": generation,
        "protection_basis": "absolute_structure",
        "score": projection.quality,
        "setup": projection.setup,
        "code": projection.setup,
        "signal_source": projection.signal_source,
        "signal_source_label": projection.signal_source_label,
        "confluence_sources": list(projection.confluence_sources),
        "trigger_event": projection.trigger_event.as_dict(),
        "reason_codes": ["trade_setup_go"],
        "action_card": projection.action_card,
        "plan": projection.plan,
    }
