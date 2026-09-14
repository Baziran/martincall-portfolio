"""Unified momentum breakout/breakdown + mean reversion setup engine."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, ScenarioKind
from aef_terminal.features.price_action import candle_anatomy, displacement_feature
from aef_terminal.features.vsa_classify import VsaBreakoutFact
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.mtf import confirmed_intrabar_parent_preview
from aef_terminal.runtime.signal_state import (
    LifecycleState,
    SignalState,
    lifecycle_from_signals,
    next_plan_trail,
    resolve_plan_bar_close_event,
)
from aef_terminal.signals.trade_plan import anchored_invalidation_stop, normalize_trade_plan

from .contracts import (
    TRADE_SETUP_ENGINE_EVENT_FIELDS,
    TRADE_SETUP_ENGINE_VERSION,
    TradeSetupEngineParams,
    _ActiveSetup,
    _SetupCandidate as _SetupCandidate,
    _momentum_code as _momentum_code,
    build_params as build_params,
    resolve_signal_name as resolve_signal_name,
)
from .presentation import (
    _bar_for_ts as _bar_for_ts,
    _bind_setup_trade_plan as _bind_setup_trade_plan,
    _build_overlays,
    _build_zone_arrow as _build_zone_arrow,
    _build_zone_arrows as _build_zone_arrows,
    _build_zone_overlay as _build_zone_overlay,
    _latest_row_fact_fields,
    _setup_action_card as _setup_action_card,
    _setup_code,
    _setup_fact_fields,
    _setup_kind_code as _setup_kind_code,
    _setup_runtime_code,
    _setup_scenario_kind as _setup_scenario_kind,
    _signal_kind,
    _zone_fact_items as _zone_fact_items,
)
from .scoring import (
    _chop_score as _chop_score,
    _direction_flips as _direction_flips,
    _exhaustion_score as _exhaustion_score,
    _grind_score as _grind_score,
    _grind_score_series,
    _market_context_from_bundle,
    _market_context_gate,
    _rejection_score as _rejection_score,
    _rvol_slope as _rvol_slope,
    _score_mean_reversion_candidate as _score_mean_reversion_candidate,
    _score_momentum_candidate as _score_momentum_candidate,
    _setup_transition_event,
    _spike_score as _spike_score,
    _structure_confluence_score as _structure_confluence_score,
    _trend_context,
    _volume_build_score as _volume_build_score,
    _vsa_breakout_gate,
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "trade_setup_engine",
        "Trade Setup Engine",
        "unified momentum breakout/breakdown and mean reversion setup detection with trade plan overlays",
        {
            "version": TRADE_SETUP_ENGINE_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "levels": [],
        },
        "tradeSetupEngine",
        "aef_terminal.indicators.modules.trade_setup_engine:trade_setup_engine",
        pipeline_stage="setup",
        pipeline_order=70,
        signal_source="trade_setup_engine",
        signal_name_resolver_ref=(
            "aef_terminal.indicators.modules.trade_setup_engine:resolve_signal_name"
        ),
        min_target_atr=0.8,
        optional_context=("market_spotlight",),
        shared_context_refs=("vsa_breakout_facts",),
        output_contract="trade-setup-authority-v1",
        score_family="decision",
        empirical_power=1.15,
        usefulness=1.25,
        state_key="tradeSetupEngine",
        calc_key="tradeSetupEngineCalcEnabled",
        visible_key="tradeSetupEngineVisible",
        chart_control_id="trade-setup-engine-toggle",
        process_control_id="trade-setup-engine-process",
        api_enabled_key="setup_engine_enabled",
        manager_order=70,
        runtime_order=60,
        default_calc=False,
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe="1m",
                history_bars=64,
                role="lower_timeframe_confirmation",
            ),
        ),
        overlay_layer="zones",
        overlay_filter_ref="generic_overlay_filter",
        runtime_payload_contract={
            "latest": TRADE_SETUP_ENGINE_EVENT_FIELDS,
            "series": TRADE_SETUP_ENGINE_EVENT_FIELDS,
            "events": TRADE_SETUP_ENGINE_EVENT_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"drop": True},
            "events": {"limit": 20, "fields": "runtime_contract"},
            "overlays": {"limit": 40, "fields": "compact_contract"},
        },
        controls=(
            _control(
                "zones",
                "Zones",
                "toggle",
                True,
                "tradeSetupEngineZones",
                element_id="trade-setup-engine-zones",
                compact=True,
            ),
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "tradeSetupEngineLabels",
                element_id="trade-setup-engine-labels",
                compact=True,
            ),
            _control(
                "plan",
                "Plan",
                "toggle",
                True,
                "tradeSetupEnginePlan",
                element_id="trade-setup-engine-plan",
                compact=True,
            ),
            _control(
                "enableMomentum",
                "Momentum",
                "toggle",
                True,
                "tradeSetupEngineEnableMomentum",
                element_id="trade-setup-engine-momentum",
                api_key="setup_engine_enable_momentum",
                param_key="enable_momentum",
                action="load_apply",
            ),
            _control(
                "enableMeanReversion",
                "MR",
                "toggle",
                True,
                "tradeSetupEngineEnableMeanReversion",
                element_id="trade-setup-engine-mr",
                api_key="setup_engine_enable_mean_reversion",
                param_key="enable_mean_reversion",
                action="load_apply",
            ),
            _control(
                "minScore",
                "GO",
                "number",
                78,
                "tradeSetupEngineMinScore",
                element_id="trade-setup-engine-min-score",
                api_key="setup_engine_min_score",
                param_key="min_score",
                minimum=55,
                maximum=95,
                step=1,
                action="load_apply",
            ),
            _control(
                "watchScore",
                "WATCH",
                "number",
                62,
                "tradeSetupEngineWatchScore",
                element_id="trade-setup-engine-watch-score",
                api_key="setup_engine_watch_score",
                param_key="watch_score",
                minimum=45,
                maximum=90,
                step=1,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.trade_setup_engine:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.trade_setup_engine:build_params",
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = trade_setup_engine
    params = ctx.params("trade_setup_engine")
    shared_vsa_breakout_facts = (
        ctx.shared["vsa_breakout_facts"] if "vsa_breakout_facts" in ctx.shared else None
    )
    confirmed_micro_preview_bars = confirmed_intrabar_parent_preview(
        ctx.confirmed_bars,
        ctx.confirmed_bar_context.get("1m", ()),
        ctx.confirmed_bar_context_quality.get("1m"),
        "1m",
    )
    return IndicatorExecutionSpec(
        id="trade_setup_engine",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=lambda: calculate(
            ctx.confirmed_bars,
            params=params,
            features=ctx.features,
            indicator_bundle=ctx.indicator_bundle,
            vsa_breakout_facts=shared_vsa_breakout_facts,
            display={
                "zones": params.zones,
                "labels": params.labels,
                "plan": params.plan,
            },
        ),
        preview_calculate=(
            (
                lambda: calculate(
                    confirmed_micro_preview_bars,
                    params=params,
                    features=None,
                    indicator_bundle=ctx.indicator_bundle,
                    vsa_breakout_facts=None,
                    preview_only=True,
                    display={
                        "zones": params.zones,
                        "labels": params.labels,
                        "plan": params.plan,
                    },
                )
            )
            if confirmed_micro_preview_bars
            else None
        ),
        preview_event_ts=(
            confirmed_micro_preview_bars[-1].ts.isoformat() if confirmed_micro_preview_bars else ""
        ),
        params=params,
        runtime_params=ctx.runtime_params,
    )


def _best_candidate(
    bars: Sequence[Bar],
    index: int,
    *,
    atr: float,
    rvol_values: Sequence[float],
    ema_fast: Sequence[float],
    ema_slow: Sequence[float],
    closes: Sequence[float],
    features: dict[str, Any],
    params: TradeSetupEngineParams,
    grind_scores: dict[Direction, Sequence[float]] | None = None,
    market_context: dict[str, Any] | None = None,
    vsa_breakout_fact: VsaBreakoutFact | None = None,
) -> _SetupCandidate | None:
    candidates: list[_SetupCandidate] = []
    if params.enable_momentum:
        candidates.extend(
            item
            for item in (
                _score_momentum_candidate(
                    bars,
                    index,
                    Direction.LONG,
                    atr=atr,
                    rvol_values=rvol_values,
                    ema_fast=ema_fast,
                    ema_slow=ema_slow,
                    features=features,
                    params=params,
                    grind_values=(grind_scores or {}).get(Direction.LONG),
                ),
                _score_momentum_candidate(
                    bars,
                    index,
                    Direction.SHORT,
                    atr=atr,
                    rvol_values=rvol_values,
                    ema_fast=ema_fast,
                    ema_slow=ema_slow,
                    features=features,
                    params=params,
                    grind_values=(grind_scores or {}).get(Direction.SHORT),
                ),
            )
            if item is not None
        )
    if params.enable_mean_reversion:
        candidates.extend(
            item
            for item in (
                _score_mean_reversion_candidate(
                    bars,
                    index,
                    Direction.LONG,
                    atr=atr,
                    rvol_values=rvol_values,
                    closes=closes,
                    params=params,
                ),
                _score_mean_reversion_candidate(
                    bars,
                    index,
                    Direction.SHORT,
                    atr=atr,
                    rvol_values=rvol_values,
                    closes=closes,
                    params=params,
                ),
            )
            if item is not None
        )
    if not candidates:
        return None
    gated: list[_SetupCandidate] = []
    for candidate in candidates:
        vsa_gate = _vsa_breakout_gate(
            candidate.direction,
            candidate.setup_type,
            vsa_breakout_fact,
        )
        if vsa_gate.get("blocked"):
            continue
        vsa_adjust = float(vsa_gate["score_adjust"])
        if vsa_adjust:
            candidate.score = pine.clamp(
                candidate.score + vsa_adjust,
                0.0,
                100.0,
            )
        if vsa_gate.get("active"):
            candidate.metrics = {
                **candidate.metrics,
                "formation_vsa_breakout_context": dict(vsa_gate.get("context") or {}),
                "formation_vsa_breakout_gate": {
                    key: value for key, value in vsa_gate.items() if key != "context"
                },
            }
        gate = _market_context_gate(candidate.direction, candidate.setup_type, market_context)
        if gate.get("blocked"):
            continue
        adjust = float(gate["score_adjust"])
        if adjust:
            candidate.score = pine.clamp(candidate.score + adjust, 0.0, 100.0)
            candidate.metrics = {**candidate.metrics, "market_context_score_adjust": adjust}
        if gate.get("reason_code"):
            candidate.metrics = {
                **candidate.metrics,
                "market_context_gate": str(gate.get("reason_code") or ""),
            }
        candidate.metrics = {
            **candidate.metrics,
            "grade": ("a" if candidate.score >= params.min_score else "watch"),
        }
        if candidate.score >= params.watch_score:
            gated.append(candidate)
    if not gated:
        return None
    return max(gated, key=lambda item: item.score)


def _distance_to_zone(price: float, zone_top: float, zone_bottom: float) -> float:
    if zone_bottom <= price <= zone_top:
        return 0.0
    if price < zone_bottom:
        return zone_bottom - price
    return price - zone_top


def _plan_from_setup(
    setup: _ActiveSetup, bar: Bar, atr: float, params: TradeSetupEngineParams
) -> None:
    pad = atr * 0.35
    if setup.direction == Direction.LONG:
        entry = setup.zone_top
        stop = anchored_invalidation_stop(Direction.LONG, setup.zone_bottom - pad, bar, atr_pad=pad)
        risk = max(entry - stop, atr * 0.5)
        target = entry + risk * params.target_r
    else:
        entry = setup.zone_bottom
        stop = anchored_invalidation_stop(Direction.SHORT, setup.zone_top + pad, bar, atr_pad=pad)
        risk = max(stop - entry, atr * 0.5)
        target = entry - risk * params.target_r
    plan = normalize_trade_plan(
        setup.direction,
        entry,
        stop,
        target,
        bar=bar,
        atr_pad=pad,
        use_market_entry=setup.state == "GO",
    )
    if not plan.get("coherent"):
        setup.entry = None
        setup.stop = None
        setup.target = None
        setup.target_2r = None
        setup.rr = None
        setup.trail = None
        if setup.state in {"ARMED", "GO"}:
            setup.state = "WATCH"
        return
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    target = float(plan["target"])
    setup.entry = _round(entry)
    setup.stop = _round(stop)
    setup.target = _round(target)
    setup.target_2r = _round(
        entry + abs(entry - stop) * params.target_2r_mult
        if setup.direction == Direction.LONG
        else entry - abs(entry - stop) * params.target_2r_mult
    )
    setup.rr = _round(abs(target - entry) / max(abs(entry - stop), 1e-9), 2)


def _breakout_go_confirmed(bar: Bar, setup: _ActiveSetup) -> bool:
    if setup.setup_type == "momentum_breakout":
        if setup.direction == Direction.LONG:
            return float(bar.close) > setup.zone_top and float(bar.low) <= setup.zone_top
        return float(bar.close) < setup.zone_bottom and float(bar.high) >= setup.zone_bottom
    if setup.direction == Direction.SHORT:
        return float(bar.close) < setup.zone_bottom and float(bar.high) >= setup.zone_bottom
    return float(bar.close) > setup.zone_top and float(bar.low) <= setup.zone_top


def _setup_action_decision(
    setup: _ActiveSetup,
    params: TradeSetupEngineParams,
    *,
    trend_context: dict[str, Any],
    market_gate: dict[str, Any],
    vsa_gate: dict[str, Any],
) -> dict[str, str]:
    state = setup.state
    score = setup.score
    if state == "GO":
        raw_action = "GO" if score >= params.min_score else "ARM"
    elif state == "ARMED":
        raw_action = "ARM"
    elif state == "WATCH":
        raw_action = "WATCH"
    else:
        raw_action = "WAIT"
    action = raw_action
    reason_code = ""
    blocked_reason = ""
    if raw_action in {"GO", "ARM"} and bool(market_gate.get("blocked")):
        action = "BLOCK"
        blocked_reason = str(
            market_gate.get("reason_code") or f"market_spotlight_veto_{setup.direction.value}"
        )
        reason_code = blocked_reason
    elif (
        setup.setup_type == "momentum_breakout"
        and raw_action in {"GO", "ARM", "WATCH"}
        and bool(vsa_gate.get("blocked"))
    ):
        action = "BLOCK"
        blocked_reason = str(vsa_gate.get("reason_code") or "vsa_breakout_context_avoid")
        reason_code = blocked_reason
    elif setup.setup_type == "momentum_breakout" and raw_action in {"GO", "ARM"}:
        alignment = str(trend_context.get("alignment") or "neutral")
        if alignment == "countertrend":
            action = "BLOCK"
            blocked_reason = "ema_trend_countertrend"
            reason_code = blocked_reason
        elif alignment == "pullback":
            action = "WATCH"
            reason_code = "ema_trend_pullback"
        elif alignment != "aligned":
            action = "WATCH"
            reason_code = "ema_trend_unconfirmed"
    return {
        "action": action,
        "raw_action": raw_action,
        "reason_code": reason_code,
        "blocked_reason": blocked_reason,
    }


def trade_setup_engine(
    bars: Sequence[Bar],
    params: TradeSetupEngineParams | None = None,
    *,
    features: dict[str, Any] | None = None,
    indicator_bundle: dict[str, dict[str, Any]] | None = None,
    vsa_breakout_facts: Sequence[VsaBreakoutFact] | None = None,
    display: dict[str, bool] | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    params = params or TradeSetupEngineParams()
    display = display or {}
    show_zones = display.get("zones", True)
    show_labels = display.get("labels", True)
    show_plan = display.get("plan", True)
    resolved_vsa_breakout_facts: list[VsaBreakoutFact | None]
    if vsa_breakout_facts is not None:
        if not isinstance(vsa_breakout_facts, Sequence) or isinstance(
            vsa_breakout_facts,
            (str, bytes),
        ):
            raise ValueError(
                "Trade Setup Engine requires the exact aligned VSA breakout fact series"
            )
        supplied_vsa_breakout_facts = list(vsa_breakout_facts)
        if len(supplied_vsa_breakout_facts) != len(bars) or any(
            not isinstance(fact, VsaBreakoutFact) or fact.index != index or fact.bar != bar
            for index, (fact, bar) in enumerate(
                zip(
                    supplied_vsa_breakout_facts,
                    bars,
                    strict=False,
                )
            )
        ):
            raise ValueError(
                "Trade Setup Engine requires the exact aligned VSA breakout fact series"
            )
        resolved_vsa_breakout_facts = [*supplied_vsa_breakout_facts]
    else:
        resolved_vsa_breakout_facts = [None] * len(bars)
    if not bars:
        return {
            "version": TRADE_SETUP_ENGINE_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "levels": [],
        }

    atr_values = pine.atr_rma_series(bars, params.atr_len)
    rvol_values = pine.relative_volume_series(bars, params.rvol_len)
    closes = [float(bar.close) for bar in bars]
    ema_fast = pine.ema_series(closes, params.ema_fast_len)
    ema_slow = pine.ema_series(closes, params.ema_slow_len)
    anatomies = [candle_anatomy(bar, index) for index, bar in enumerate(bars)]
    body_shares = [anatomy.body_share for anatomy in anatomies]
    grind_scores = (
        {
            direction: _grind_score_series(
                bars,
                direction,
                params.grind_len,
                body_shares=body_shares,
            )
            for direction in (Direction.LONG, Direction.SHORT)
        }
        if params.enable_momentum
        else None
    )

    bundle = indicator_bundle or {}
    market_context = _market_context_from_bundle(bundle)

    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    signal_states: list[SignalState] = []
    active: _ActiveSetup | None = None

    for index, bar in enumerate(bars):
        released_this_bar = False
        current_vsa_breakout_fact = resolved_vsa_breakout_facts[index]
        atr = max(float(atr_values[index]), 1e-9)
        anatomy = anatomies[index]
        bar_features = {
            **displacement_feature(
                bars[max(0, index - 3) : index + 1],
            ),
            "body_share": anatomy.body_share,
            "close_pos": anatomy.close_pos,
        }
        candidate = _best_candidate(
            bars,
            index,
            atr=atr,
            rvol_values=rvol_values,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            closes=closes,
            features=bar_features,
            params=params,
            grind_scores=grind_scores,
            market_context=(market_context if index == len(bars) - 1 else None),
            vsa_breakout_fact=current_vsa_breakout_fact,
        )

        if active is not None:
            active.age_bars += 1
            if active.age_bars > params.max_setup_bars and not active.go_confirmed:
                events.append(
                    _setup_transition_event(
                        active,
                        bar,
                        "EXPIRED",
                        action_reason_code="setup_expired",
                    )
                )
                active = None
                released_this_bar = True
            elif active.state in {"GO", "ARMED", "WATCH"}:
                previous_state = active.state
                dist = _distance_to_zone(
                    float(bar.close),
                    active.zone_top,
                    active.zone_bottom,
                )
                if active.state == "WATCH" and dist <= atr * params.armed_atr:
                    active.state = "ARMED"
                promotion_trend_context = _trend_context(
                    active.direction,
                    close=float(bar.close),
                    ema_fast=ema_fast[index] if index < len(ema_fast) else None,
                    ema_slow=ema_slow[index] if index < len(ema_slow) else None,
                )
                promotion_vsa_gate = _vsa_breakout_gate(
                    active.direction,
                    active.setup_type,
                    current_vsa_breakout_fact,
                )
                promotion_market_gate = (
                    _market_context_gate(
                        active.direction,
                        active.setup_type,
                        market_context,
                    )
                    if index == len(bars) - 1 and not active.go_confirmed
                    else {}
                )
                promotion_allowed = (
                    not bool(promotion_vsa_gate.get("blocked"))
                    and not bool(promotion_market_gate.get("blocked"))
                    and (
                        active.setup_type != "momentum_breakout"
                        or promotion_trend_context.get("alignment") == "aligned"
                    )
                )
                if (
                    active.state in {"ARMED", "WATCH"}
                    and _breakout_go_confirmed(bar, active)
                    and active.score >= params.min_score
                    and promotion_allowed
                ):
                    active.state = "GO"
                if previous_state == "WATCH":
                    _plan_from_setup(active, bar, atr, params)
                    if active.state in {"ARMED", "GO"}:
                        active.armed = True
                        active.go_confirmed = active.state == "GO"
                        active.age_bars = 0
                elif previous_state == "ARMED" and active.state == "GO":
                    active.go_confirmed = True
                if previous_state != "GO" and active.state == "GO":
                    active.trail = active.stop
                if active.state != previous_state and active.state in {"ARMED", "GO"}:
                    events.append(
                        _setup_transition_event(
                            active,
                            bar,
                            active.state,
                            action_reason_code=(
                                "setup_go_confirmed" if active.state == "GO" else "setup_armed"
                            ),
                        )
                    )
                terminal = None
                if (
                    previous_state == "GO"
                    and active.state == "GO"
                    and active.entry is not None
                    and active.stop is not None
                    and active.target is not None
                ):
                    terminal = resolve_plan_bar_close_event(
                        direction=active.direction,
                        stop=active.stop,
                        target=active.target,
                        trail=active.trail,
                        bar=bar,
                    )
                    if terminal is not None:
                        active.state = terminal.state.value
                    else:
                        active.trail = next_plan_trail(
                            direction=active.direction,
                            carried_trail=active.trail,
                            stop=active.stop,
                            close=bar.close,
                            atr=atr,
                            trail_atr=params.trail_atr,
                        )
                if active.state in {"STOP", "TP", "TP/SL", "TRAIL"}:
                    if active.state == LifecycleState.TARGET_AND_STOP_HIT.value:
                        terminal_reason_code = "setup_target_and_stop_hit"
                    elif active.state == LifecycleState.TRAIL_STOP.value:
                        terminal_reason_code = (
                            "setup_trail_gap_through"
                            if terminal is not None and terminal.exit_price is None
                            else "setup_trail_stop_hit"
                        )
                    elif active.state == LifecycleState.STOP_HIT.value:
                        terminal_reason_code = (
                            "setup_stop_gap_through"
                            if terminal is not None and terminal.exit_price is None
                            else "setup_stop_hit"
                        )
                    else:
                        terminal_reason_code = (
                            "setup_target_gap_through"
                            if terminal is not None and terminal.exit_price is None
                            else "setup_target_hit"
                        )
                    events.append(
                        _setup_transition_event(
                            active,
                            bar,
                            active.state,
                            action_reason_code=terminal_reason_code,
                        )
                    )
                    active = None
                    released_this_bar = True

        candidate_available = candidate is not None and not released_this_bar

        if active is None and candidate_available:
            assert candidate is not None
            active = _ActiveSetup(
                setup_type=candidate.setup_type,
                direction=candidate.direction,
                state="WATCH",
                zone_top=candidate.zone_top,
                zone_bottom=candidate.zone_bottom,
                zone_start_ts=bar.ts.isoformat(),
                score=candidate.score,
                reason_code=candidate.reason_code,
                metrics=candidate.metrics,
            )
            _plan_from_setup(active, bar, atr, params)
            events.append(
                _setup_transition_event(
                    active,
                    bar,
                    "WATCH",
                    action_reason_code="setup_watch_started",
                )
            )
        elif (
            active is not None
            and candidate is not None
            and active.state == "WATCH"
            and not active.armed
            and not active.go_confirmed
            and candidate.score > active.score + 4.0
        ):
            events.append(
                _setup_transition_event(
                    active,
                    bar,
                    "SUPERSEDED",
                    action_reason_code="setup_superseded",
                )
            )
            active = _ActiveSetup(
                setup_type=candidate.setup_type,
                direction=candidate.direction,
                state="WATCH",
                zone_top=candidate.zone_top,
                zone_bottom=candidate.zone_bottom,
                zone_start_ts=bar.ts.isoformat(),
                score=candidate.score,
                reason_code=candidate.reason_code,
                metrics=candidate.metrics,
            )
            _plan_from_setup(active, bar, atr, params)
            events.append(
                _setup_transition_event(
                    active,
                    bar,
                    "WATCH",
                    action_reason_code="setup_watch_started",
                )
            )

        state = active.state if active is not None else "IDLE"
        direction = active.direction if active is not None else Direction.FLAT
        score = active.score if active is not None else 0.0
        trend_context = (
            _trend_context(
                active.direction,
                close=float(bar.close),
                ema_fast=ema_fast[index] if index < len(ema_fast) else None,
                ema_slow=ema_slow[index] if index < len(ema_slow) else None,
            )
            if active is not None
            else {}
        )
        market_gate: dict[str, Any] = (
            _market_context_gate(
                active.direction,
                active.setup_type,
                market_context,
            )
            if active is not None and index == len(bars) - 1 and not active.go_confirmed
            else {}
        )
        vsa_gate = (
            _vsa_breakout_gate(
                active.direction,
                active.setup_type,
                current_vsa_breakout_fact,
            )
            if active is not None
            else {}
        )
        action_decision = (
            _setup_action_decision(
                active,
                params,
                trend_context=trend_context,
                market_gate=market_gate,
                vsa_gate=vsa_gate,
            )
            if active is not None
            else {
                "action": "WAIT",
                "raw_action": "WAIT",
                "reason_code": "",
                "blocked_reason": "",
            }
        )
        action = action_decision["action"]
        blocked_reason_code = action_decision["blocked_reason"]
        trigger = active.entry if active is not None else None
        stop = active.stop if active is not None else None
        target = active.target if active is not None else None
        signal = SignalState(
            source="trade_setup_engine",
            action=ActionPhase(action),
            raw_action=action_decision["raw_action"],
            direction=direction,
            score=score,
            trigger=trigger,
            stop=stop,
            target=target,
            code=_setup_code(active) if active is not None else "SETUP_IDLE",
            kind=_signal_kind(active.setup_type) if active is not None else ScenarioKind.WAIT,
            reason=active.reason_code if active is not None else "no_active_setup",
            blocked_reason=blocked_reason_code,
        )
        signal_states.append(signal)
        row = {
            "ts": bar.ts.isoformat(),
            "state": state,
            "direction": direction.value,
            "setup_type": active.setup_type if active is not None else "",
            "score": _round(score, 2),
            "entry": trigger,
            "stop": stop,
            "target": target,
            "target_2r": active.target_2r if active is not None else None,
            "rr": active.rr if active is not None else None,
            "role": _setup_runtime_code(active) if active is not None else "IDLE",
            "reason_code": active.reason_code if active is not None else "",
            "action": action,
            "raw_action": action_decision["raw_action"],
            "action_reason_code": action_decision["reason_code"],
            "blocked_reason": blocked_reason_code,
            "trend_context": trend_context,
            "vsa_breakout_context": (
                dict(vsa_gate.get("context") or {}) if vsa_gate.get("active") else {}
            ),
            "vsa_breakout_gate": (
                {key: value for key, value in vsa_gate.items() if key != "context"}
                if vsa_gate.get("active")
                else {}
            ),
            "zone": (
                {"top": _round(active.zone_top), "bottom": _round(active.zone_bottom)}
                if active is not None
                else None
            ),
            "metrics": active.metrics if active is not None else {},
            "market_context_gate": market_gate
            if active is not None and market_gate.get("reason_code")
            else {},
            "signal": signal.as_dict(),
        }
        if not preview_only or index == len(bars) - 1:
            series.append(row)

    lifecycle = lifecycle_from_signals(
        source="trade_setup_engine",
        bars=bars,
        signals=signal_states,
        atr_values=[max(float(value), 1e-9) for value in atr_values],
        max_age_bars=params.max_setup_bars,
        trail_atr=params.trail_atr,
    )
    latest_row = series[-1] if series else None
    latest_setup = active
    last_bar = bars[-1]
    last_atr = max(float(atr_values[-1]), 1e-9)
    last_ema_fast = ema_fast[-1] if ema_fast else None
    last_ema_slow = ema_slow[-1] if ema_slow else None
    latest_trend_context = (
        dict(latest_row.get("trend_context") or {}) if isinstance(latest_row, dict) else {}
    )
    latest_vsa_gate = (
        {
            **dict(latest_row.get("vsa_breakout_gate") or {}),
            "context": dict(latest_row.get("vsa_breakout_context") or {}),
        }
        if isinstance(latest_row, dict)
        and isinstance(latest_row.get("vsa_breakout_gate"), dict)
        and latest_row.get("vsa_breakout_gate")
        else {}
    )
    latest_market_gate = (
        _market_context_gate(
            latest_setup.direction,
            latest_setup.setup_type,
            market_context,
        )
        if latest_setup is not None and not latest_setup.go_confirmed
        else {}
    )
    latest_action_decision = (
        _setup_action_decision(
            latest_setup,
            params,
            trend_context=latest_trend_context,
            market_gate=latest_market_gate,
            vsa_gate=latest_vsa_gate,
        )
        if latest_setup is not None
        else {
            "action": "WAIT",
            "raw_action": "WAIT",
            "reason_code": "",
            "blocked_reason": "",
        }
    )
    if (
        latest_setup is not None
        and lifecycle is not None
        and lifecycle.active
        and lifecycle.filled
        and bool(latest_market_gate.get("blocked"))
        and latest_action_decision["action"] == "BLOCK"
    ):
        latest_market_gate = {}
        latest_action_decision = _setup_action_decision(
            latest_setup,
            params,
            trend_context=latest_trend_context,
            market_gate=latest_market_gate,
            vsa_gate=latest_vsa_gate,
        )
    if latest_setup is not None and latest_row is not None:
        latest_signal = SignalState(
            source="trade_setup_engine",
            action=ActionPhase(latest_action_decision["action"]),
            raw_action=latest_action_decision["raw_action"],
            direction=latest_setup.direction,
            score=latest_setup.score,
            trigger=latest_setup.entry,
            stop=latest_setup.stop,
            target=latest_setup.target,
            code=_setup_code(latest_setup),
            kind=_signal_kind(latest_setup.setup_type),
            reason=latest_setup.reason_code,
            blocked_reason=latest_action_decision["blocked_reason"],
        )
        latest_row = {
            **latest_row,
            "action": latest_action_decision["action"],
            "raw_action": latest_action_decision["raw_action"],
            "action_reason_code": latest_action_decision["reason_code"],
            "blocked_reason": latest_action_decision["blocked_reason"],
            "market_context_gate": (
                latest_market_gate if latest_market_gate.get("reason_code") else {}
            ),
            "signal": latest_signal.as_dict(),
        }
    if lifecycle is not None and lifecycle.active and latest_action_decision["action"] == "BLOCK":
        lifecycle = None
    overlays: list[dict[str, Any]] = (
        []
        if preview_only
        else _build_overlays(
            bars,
            latest_setup,
            params=params,
            show_zones=show_zones,
            show_labels=show_labels,
            show_plan=show_plan,
            action_decision=latest_action_decision,
            trend_context=latest_trend_context,
            vsa_gate=latest_vsa_gate,
            atr=last_atr,
            ema_fast=last_ema_fast,
            ema_slow=last_ema_slow,
        )
    )
    lifecycle_dict = lifecycle.as_dict() if lifecycle else None
    if lifecycle_dict:
        lifecycle_dict["label"] = "Trade Setup Engine"
    if lifecycle_dict and latest_row is not None:
        latest_row = {**latest_row, "lifecycle": lifecycle_dict}

    latest = {
        **(latest_row or {}),
        "lifecycle": lifecycle_dict,
        **(
            _setup_fact_fields(
                latest_setup,
                action_decision=latest_action_decision,
                trend_context=latest_trend_context,
                vsa_gate=latest_vsa_gate,
                bar=last_bar,
                atr=last_atr,
                ema_fast=last_ema_fast,
                ema_slow=last_ema_slow,
                params=params,
            )
            if latest_setup is not None
            else _latest_row_fact_fields(latest_row)
        ),
    }

    return {
        "version": TRADE_SETUP_ENGINE_VERSION,
        "series": series,
        "events": (
            [event for event in events if event.get("ts") == last_bar.ts.isoformat()]
            if preview_only
            else events[-80:]
        ),
        "latest": latest,
        "levels": [],
        "overlays": overlays,
        "params": {
            "min_score": params.min_score,
            "watch_score": params.watch_score,
            "ema_fast_len": params.ema_fast_len,
            "ema_slow_len": params.ema_slow_len,
            "grind_len": params.grind_len,
            "spike_len": params.spike_len,
            "target_r": params.target_r,
            "enable_momentum": params.enable_momentum,
            "enable_mean_reversion": params.enable_mean_reversion,
        },
    }
