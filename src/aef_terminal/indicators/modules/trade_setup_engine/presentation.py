"""Typed Trade Setup facts, action cards, and overlays."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import Bar, Direction, DomainFact, ScenarioKind
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.runtime import overlays as overlay_primitives
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.presentation import ActionTier, build_action_card
from aef_terminal.runtime.signal_state import trade_plan_payload

from .contracts import TradeSetupEngineParams, _ActiveSetup, _momentum_code


def _signal_kind(setup_type: str) -> ScenarioKind:
    return ScenarioKind.FADE if setup_type == "mean_reversion" else ScenarioKind.TRANSIT


def _setup_code(setup: _ActiveSetup) -> str:
    if setup.setup_type == "mean_reversion":
        return "SETUP_MR" if setup.state in {"GO", "ARMED"} else "SETUP_MR_WATCH"
    prefix = _momentum_code(setup.direction)
    return f"SETUP_{prefix}" if setup.state in {"GO", "ARMED"} else f"SETUP_{prefix}_WATCH"


def _setup_runtime_code(setup: _ActiveSetup) -> str:
    return setup.state.lower()


def _setup_kind_code(setup: _ActiveSetup) -> str:
    if setup.setup_type == "mean_reversion":
        return "MR"
    return _momentum_code(setup.direction)


def _setup_scenario_kind(setup_type: str) -> str:
    return ScenarioKind.FADE.value if setup_type == "mean_reversion" else ScenarioKind.TRANSIT.value


def _setup_action_card(
    setup: _ActiveSetup,
    *,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
) -> dict[str, Any]:
    action = action_decision["action"]
    blocked = action == "BLOCK"
    blocked_reason = action_decision.get("blocked_reason") or ""
    evidence_gate = vsa_gate
    if not evidence_gate.get("active"):
        formation_gate = setup.metrics.get("formation_vsa_breakout_gate")
        formation_context = setup.metrics.get("formation_vsa_breakout_context")
        if isinstance(formation_gate, dict) and formation_gate.get("active"):
            evidence_gate = {
                **formation_gate,
                "context": (dict(formation_context) if isinstance(formation_context, dict) else {}),
            }
    return build_action_card(
        source="trade_setup_engine",
        phase=action,
        direction=setup.direction,
        setup=setup.setup_type,
        trigger_event=DomainFact(
            "setup_state_transition",
            {"setup_code": _setup_code(setup), "action": action},
        ),
        entry=setup.entry,
        stop=setup.stop,
        target=setup.target,
        supporting_facts=[
            DomainFact(setup.reason_code, {"state": setup.state.lower()}),
            *(
                [
                    DomainFact(
                        str(evidence_gate.get("reason_code")),
                        {
                            "tier": str(evidence_gate.get("tier") or ""),
                            "authority": "context_only",
                        },
                    )
                ]
                if (
                    evidence_gate.get("active")
                    and not evidence_gate.get("blocked")
                    and evidence_gate.get("reason_code")
                )
                else []
            ),
        ],
        blocking_facts=[DomainFact(blocked_reason, {"state": setup.state.lower()})]
        if blocked and blocked_reason
        else (
            [
                DomainFact(
                    str(evidence_gate.get("reason_code")),
                    {"authority": "context_only"},
                )
            ]
            if evidence_gate.get("blocked") and evidence_gate.get("reason_code")
            else []
        ),
        metrics={
            "score": setup.score,
            "rr": setup.rr,
            "raw_action": action_decision["raw_action"],
            "action_reason_code": action_decision.get("reason_code") or "",
            "trend_direction": trend_context.get("trend_direction"),
            "trend_alignment": trend_context.get("alignment"),
            "vsa_breakout_active": bool(evidence_gate.get("active")),
            "vsa_breakout_tier": str(evidence_gate.get("tier") or ""),
            "vsa_breakout_reason_code": str(evidence_gate.get("reason_code") or ""),
        },
        tier=ActionTier.PLAN,
        blocked=blocked,
    ).as_dict()


def _zone_fact_items(
    setup: _ActiveSetup,
    *,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
    ema_fast: float | None,
    ema_slow: float | None,
    close: float,
) -> list[dict[str, Any]]:
    facts = [
        {
            "code": "setup_zone",
            "state": setup.state.lower(),
            "direction": setup.direction.value,
            "mode": _setup_kind_code(setup).lower(),
            "action": action_decision["action"].lower(),
            "raw_action": action_decision["raw_action"].lower(),
        },
        {
            "code": "trend_context",
            "basis": str(trend_context.get("basis") or "ema_fast_slow"),
            "trend_direction": str(trend_context.get("trend_direction") or Direction.FLAT.value),
            "alignment": str(trend_context.get("alignment") or "neutral"),
            "price_relation": str(trend_context.get("price_relation") or "unknown"),
            "close": close,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
        },
    ]
    evidence_gate = vsa_gate
    evidence_phase = "current"
    if not evidence_gate.get("active"):
        formation_gate = setup.metrics.get("formation_vsa_breakout_gate")
        formation_context = setup.metrics.get("formation_vsa_breakout_context")
        if isinstance(formation_gate, dict) and formation_gate.get("active"):
            evidence_gate = {
                **formation_gate,
                "context": (dict(formation_context) if isinstance(formation_context, dict) else {}),
            }
            evidence_phase = "formation"
    if evidence_gate.get("active"):
        context = (
            dict(evidence_gate.get("context") or {})
            if isinstance(evidence_gate.get("context"), dict)
            else {}
        )
        facts.append(
            {
                "code": str(evidence_gate.get("reason_code") or "vsa_breakout_context"),
                "authority": "context_only",
                "phase": evidence_phase,
                "blocked": bool(evidence_gate.get("blocked")),
                "tier": str(evidence_gate.get("tier") or ""),
                "signal_code": str(context.get("signal_code") or ""),
                "prior_trend": str(context.get("prior_trend") or "unknown"),
            }
        )
    return facts


def _setup_fact_fields(
    setup: _ActiveSetup,
    *,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
    bar: Bar,
    atr: float,
    ema_fast: float | None,
    ema_slow: float | None,
    params: TradeSetupEngineParams,
) -> dict[str, Any]:
    action = action_decision["action"]
    blocked_reason = action_decision.get("blocked_reason") or ""
    opposing_reason = blocked_reason or (
        str(vsa_gate.get("reason_code") or "") if vsa_gate.get("blocked") else ""
    )
    mode_code = _setup_kind_code(setup)
    return indicator_fact_payload(
        scenario=setup.setup_type or "idle",
        setup=_setup_runtime_code(setup),
        trigger_event={
            "code": "setup_state_transition",
            "setup_code": _setup_code(setup),
            "action": action,
        },
        supporting={
            "code": "setup_candidate",
            "state": setup.state.lower(),
            "direction": setup.direction.value,
        },
        opposing={"code": opposing_reason, "state": setup.state.lower()}
        if opposing_reason
        else None,
        context={
            "code": "trend_context",
            "basis": str(trend_context.get("basis") or "ema_fast_slow"),
            "trend_direction": str(trend_context.get("trend_direction") or Direction.FLAT.value),
            "alignment": str(trend_context.get("alignment") or "neutral"),
            "price_relation": str(trend_context.get("price_relation") or "unknown"),
        },
        quality={"code": "setup_score", "state": setup.state.lower(), "score": setup.score},
        metrics={
            **setup.metrics,
            "mode": setup.setup_type,
            "mode_code": mode_code,
            "action": action,
            "raw_action": action_decision["raw_action"],
            "action_reason_code": action_decision.get("reason_code") or "",
            "blocked_reason": blocked_reason,
            "state": setup.state,
            "direction": setup.direction.value,
            "reason_code": setup.reason_code,
            "trend_direction": trend_context.get("trend_direction"),
            "trend_alignment": trend_context.get("alignment"),
            "vsa_breakout_active": bool(vsa_gate.get("active")),
            "vsa_breakout_blocked": bool(vsa_gate.get("blocked")),
            "vsa_breakout_tier": str(vsa_gate.get("tier") or ""),
            "vsa_breakout_reason_code": str(vsa_gate.get("reason_code") or ""),
            "score": _metric_number(setup.score, digits=0),
            "watch_score": _metric_number(params.watch_score, digits=0),
            "go_score": _metric_number(params.min_score, digits=0),
            "close": _metric_number(bar.close),
            "atr": _metric_number(atr),
            "ema_fast": _metric_number(ema_fast),
            "ema_slow": _metric_number(ema_slow),
        },
    )


def _latest_row_fact_fields(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    existing = copy_indicator_facts(row)
    state = str(row.get("state") or row.get("kind") or "setup")
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    action = str(row.get("action") or signal.get("action") or "WAIT")
    metrics = dict(existing.get("metrics") or {})
    metrics.update(
        {
            "state": state,
            "action": action,
            "raw_action": row.get("raw_action"),
            "action_reason_code": row.get("action_reason_code"),
            "blocked_reason": row.get("blocked_reason"),
            "direction": row.get("direction"),
            "trend_direction": (row.get("trend_context") or {}).get("trend_direction")
            if isinstance(row.get("trend_context"), dict)
            else None,
            "trend_alignment": (row.get("trend_context") or {}).get("alignment")
            if isinstance(row.get("trend_context"), dict)
            else None,
            "vsa_breakout_reason_code": (row.get("vsa_breakout_gate") or {}).get("reason_code")
            if isinstance(row.get("vsa_breakout_gate"), dict)
            else None,
            "score": _metric_number(row.get("score"), digits=0),
        }
    )
    return indicator_fact_payload(
        scenario=existing.get("scenario") or "trade_setup_engine",
        setup=existing.get("setup") or "runtime_state",
        trigger_event=existing.get("trigger_event")
        or {"code": "setup_runtime_state", "state": state, "action": action},
        metrics=metrics,
    )


def _bind_setup_trade_plan(
    overlay: dict[str, Any],
    setup: _ActiveSetup,
    *,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
) -> dict[str, Any]:
    action = action_decision["action"]
    blocked_reason = action_decision.get("blocked_reason") or ""
    plan = trade_plan_payload(
        source="trade_setup_engine",
        direction=setup.direction,
        entry=setup.entry,
        stop=setup.stop,
        target=setup.target,
        action=action,
        raw_action=action_decision["raw_action"],
        state=setup.state,
        score=setup.score,
        actionable=action in {"ARM", "GO"},
        reason=setup.reason_code,
        blocked_reason=blocked_reason,
        code=_setup_code(setup),
        kind=_signal_kind(setup.setup_type),
    )
    overlay["trade_plan"] = plan
    overlay["source"] = "trade_setup_engine"
    overlay["entry"] = setup.entry
    overlay["stop"] = setup.stop
    overlay["target"] = setup.target
    overlay["action"] = action
    overlay["raw_action"] = action_decision["raw_action"]
    overlay["blocked_reason"] = blocked_reason
    overlay["action_reason_code"] = action_decision.get("reason_code") or ""
    overlay["trend_context"] = dict(trend_context)
    overlay["direction"] = setup.direction.value
    overlay["score"] = setup.score
    overlay["setup_type"] = setup.setup_type
    overlay["setup_mode"] = _setup_kind_code(setup)
    overlay["code"] = _setup_code(setup)
    overlay["action_card"] = _setup_action_card(
        setup,
        action_decision=action_decision,
        trend_context=trend_context,
        vsa_gate=vsa_gate,
    )
    return overlay


def _bar_for_ts(bars: Sequence[Bar], ts_iso: str) -> Bar:
    for bar in bars:
        if bar.ts.isoformat() == ts_iso:
            return bar
    return bars[-1]


def _build_zone_arrow(
    bar: Bar,
    price: float,
    setup: _ActiveSetup,
    *,
    role: str,
    params: TradeSetupEngineParams,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
    atr: float,
    ema_fast: float | None,
    ema_slow: float | None,
) -> dict[str, Any]:
    item = overlay_primitives.label(
        bar=bar,
        price=price,
        lines=None,
        direction=setup.direction,
        tone="warning"
        if role == "entry"
        else "positive"
        if setup.direction == Direction.LONG
        else "negative",
        side="center",
        role=role,
        fact_fields=_setup_fact_fields(
            setup,
            action_decision=action_decision,
            trend_context=trend_context,
            vsa_gate=vsa_gate,
            bar=bar,
            atr=atr,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            params=params,
        ),
    )
    item["glyph_only"] = True
    item["no_tick"] = True
    item["control_key"] = "plan"
    return _bind_setup_trade_plan(
        item,
        setup,
        action_decision=action_decision,
        trend_context=trend_context,
        vsa_gate=vsa_gate,
    )


def _build_zone_overlay(
    setup: _ActiveSetup,
    *,
    start_ts: str,
    end_anchor: Bar,
    end_bar_offset: int,
    params: TradeSetupEngineParams,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
    bar: Bar,
    atr: float,
    ema_fast: float | None,
    ema_slow: float | None,
) -> dict[str, Any]:
    direction = setup.direction.value
    overlay = {
        "type": "box",
        "id": f"setup-zone-{start_ts}",
        "start_ts": start_ts,
        **overlay_primitives.projected_end(end_anchor, end_bar_offset),
        "top": _round(setup.zone_top),
        "bottom": _round(setup.zone_bottom),
        "tone": "positive" if setup.direction == Direction.LONG else "negative",
        **_setup_fact_fields(
            setup,
            action_decision=action_decision,
            trend_context=trend_context,
            vsa_gate=vsa_gate,
            bar=bar,
            atr=atr,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            params=params,
        ),
        "role": "setup_zone",
        "control_key": "zones",
        "source": "trade_setup_engine",
        "direction": direction,
        "badge_facts": _zone_fact_items(
            setup,
            action_decision=action_decision,
            trend_context=trend_context,
            vsa_gate=vsa_gate,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            close=float(bar.close),
        ),
    }
    return _bind_setup_trade_plan(
        overlay,
        setup,
        action_decision=action_decision,
        trend_context=trend_context,
        vsa_gate=vsa_gate,
    )


def _build_zone_arrows(
    bars: Sequence[Bar],
    setup: _ActiveSetup,
    *,
    params: TradeSetupEngineParams,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
    atr: float,
    ema_fast: float | None,
    ema_slow: float | None,
) -> list[dict[str, Any]]:
    if not bars:
        return []
    items: list[dict[str, Any]] = []
    if setup.entry is None:
        return items
    start_bar = _bar_for_ts(bars, setup.zone_start_ts)
    end_bar = bars[-1]
    items.append(
        _build_zone_arrow(
            start_bar,
            float(setup.entry),
            setup,
            role="entry",
            params=params,
            action_decision=action_decision,
            trend_context=trend_context,
            vsa_gate=vsa_gate,
            atr=atr,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        )
    )
    if setup.target is not None:
        items.append(
            _build_zone_arrow(
                end_bar,
                float(setup.target),
                setup,
                role="target",
                params=params,
                action_decision=action_decision,
                trend_context=trend_context,
                vsa_gate=vsa_gate,
                atr=atr,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
            )
        )
    return items


def _build_overlays(
    bars: Sequence[Bar],
    setup: _ActiveSetup | None,
    *,
    params: TradeSetupEngineParams,
    show_zones: bool,
    show_labels: bool,
    show_plan: bool,
    action_decision: dict[str, str],
    trend_context: dict[str, Any],
    vsa_gate: dict[str, Any],
    atr: float,
    ema_fast: float | None,
    ema_slow: float | None,
) -> list[dict[str, Any]]:
    if setup is None or setup.state == "IDLE" or not bars:
        return []
    last = bars[-1]
    projection_bars = 18
    items: list[dict[str, Any]] = []
    if show_zones:
        items.append(
            _build_zone_overlay(
                setup,
                start_ts=setup.zone_start_ts,
                end_anchor=last,
                end_bar_offset=projection_bars,
                params=params,
                action_decision=action_decision,
                trend_context=trend_context,
                vsa_gate=vsa_gate,
                bar=last,
                atr=atr,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
            )
        )
        if show_plan:
            items.extend(
                _build_zone_arrows(
                    bars,
                    setup,
                    params=params,
                    action_decision=action_decision,
                    trend_context=trend_context,
                    vsa_gate=vsa_gate,
                    atr=atr,
                    ema_fast=ema_fast,
                    ema_slow=ema_slow,
                )
            )
    if show_labels and setup.state in {"WATCH", "ARMED", "GO"}:
        state_label = overlay_primitives.label(
            bar=last,
            price=setup.zone_top if setup.direction == Direction.SHORT else setup.zone_bottom,
            lines=None,
            direction=setup.direction,
            tone="warning" if setup.state == "GO" else "neutral",
            side="above" if setup.direction == Direction.SHORT else "below",
            role="setup_state",
            fact_fields=_setup_fact_fields(
                setup,
                action_decision=action_decision,
                trend_context=trend_context,
                vsa_gate=vsa_gate,
                bar=last,
                atr=atr,
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                params=params,
            ),
        )
        state_label["control_key"] = "labels"
        items.append(
            _bind_setup_trade_plan(
                state_label,
                setup,
                action_decision=action_decision,
                trend_context=trend_context,
                vsa_gate=vsa_gate,
            )
        )
    return items
