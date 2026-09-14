from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, ScenarioKind
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    offset_indicator_indices,
)
from aef_terminal.features.volume import resolve_rvol_context
from aef_terminal.features.smc import SmcParams, smc_snapshot
from aef_terminal.features.provider_session import (
    ProviderSessionReset,
    provider_session_level_context,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    IndicatorDefaults,
    ScoreDefaults,
    label_importance_floor,
    score_bands_with_thresholds as _score_bands,
    score_action,
)
from aef_terminal.indicators.module_contract import (
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.runtime import pine
from aef_terminal.runtime import overlays as overlay_primitives
from aef_terminal.runtime.instruments import InstrumentProfile
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.signal_state import LifecycleState, PlanLifecycle, SignalState
from aef_terminal.signals.direction import direction_from_any
from aef_terminal.signals.trade_plan import anchored_invalidation_stop, normalize_trade_plan


_EVENT_FIELDS = (
    "action",
    "confluence",
    "details",
    "direction",
    "entry",
    "event",
    "event_code",
    "guide",
    "index",
    "label",
    "level",
    "lifecycle",
    "market_context_gate",
    "price",
    "score",
    "scenario_kind",
    "signal",
    "start_ts",
    "stop",
    "structure_mode",
    "target",
    "tooltip",
    "trend_day_warning",
    "ts",
    "zone",
    "zone_type",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


def _latest_atr(bars: Sequence[Bar], features: dict[str, Any] | None = None) -> float:
    if isinstance(features, dict):
        cached = features.get("latest_atr")
        if cached is not None:
            return max(float(cached), 0.000001)
    values = pine.atr_rma_series(bars, DEFAULT_INDICATOR_SETTINGS.atr_len)
    return max(float(values[-1]), 0.000001) if values else 1.0


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "smc_channels",
        "SMC Structure",
        "BOS/CHOCH, sweeps, FVGs, merged zones and rebound plans",
        {
            "version": "6-python-structure",
            "pivots": [],
            "events": [],
            "boxes": [],
            "levels": [],
            "rebound": {},
            "latest": None,
            "overlays": [],
        },
        "smcChannels",
        "aef_terminal.indicators.modules.smc_channels:smc_structure",
        group="structure",
        pipeline_stage="structure",
        pipeline_order=100,
        optional_context=("market_spotlight", "channel_master"),
        signal_source="smc_structure",
        min_target_atr=0.6,
        custom_renderer=True,
        renderer_kind="custom",
        renderer_ref="structure_indicators",
        renderer_primitives=("box", "line", "label", "structure_paths"),
        renderer_placements=("price",),
        custom_renderer_requirements=("price_overlay_provider", "structure_snapshot"),
        score_family="structure",
        empirical_power=1.10,
        usefulness=1.15,
        module_type="context",
        state_key="smcChannels",
        calc_key="smcChannelsCalcEnabled",
        visible_key="smcChannelsVisible",
        chart_control_id="smc-channels-toggle",
        process_control_id="smc-channels-process",
        api_enabled_key="smc_enabled",
        manager_order=100,
        runtime_order=90,
        default_calc=False,
        overlay_layer="zones",
        overlay_filter_ref="smc_channels",
        runtime_payload_contract={
            "latest": _EVENT_FIELDS,
            "events": _EVENT_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "overlays": {"limit": 120, "fields": "compact_contract"},
            "events": {"limit": 80},
        },
        controls=(
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "smcChannelsLabels",
                element_id="smc-channels-labels",
                compact=True,
            ),
            _control(
                "boxes",
                "Boxes",
                "toggle",
                True,
                "smcChannelsBoxes",
                element_id="smc-channels-boxes",
                compact=True,
            ),
            _control(
                "triggeredAreas",
                "Triggered",
                "toggle",
                True,
                "smcChannelsTriggeredAreas",
                element_id="smc-channels-triggered-areas",
            ),
            _control(
                "pivotLen",
                "Pivot",
                "number",
                4,
                "smcChannelsPivotLen",
                element_id="smc-channels-pivot-len",
                api_key="smc_pivot_len",
                param_key="pivot_len",
                minimum=2,
                maximum=20,
                step=1,
                action="load_apply",
            ),
            _control(
                "obMinDisplacement",
                "OB disp",
                "number",
                0.45,
                "smcChannelsObMinDisplacement",
                element_id="smc-channels-ob-min-displacement",
                api_key="smc_ob_min_displacement_atr",
                param_key="ob_min_displacement_atr",
                minimum=0.1,
                maximum=2,
                step=0.05,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.smc_channels:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.smc_channels:build_params",
    ui_js_assets=("client.js",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = smc_structure
    structure_bars = ctx.structure_bars or ()
    params = ctx.params("smc_channels")
    return IndicatorExecutionSpec(
        id="smc_channels",
        input_bars=structure_bars,
        analysis_bar=structure_bars[-1] if structure_bars else None,
        mode="confirmed",
        calculate=lambda: calculate(
            structure_bars,
            profile=ctx.instrument_profile,
            params=params,
            features={**dict(ctx.features), "latest_atr": ctx.atr_value},
            market_context=(ctx.indicator_bundle.get("market_spotlight") or {}).get(
                "market_context"
            )
            if isinstance(ctx.indicator_bundle.get("market_spotlight"), dict)
            else None,
            channel_context=(
                ctx.indicator_bundle.get("channel_master")
                if isinstance(ctx.indicator_bundle.get("channel_master"), dict)
                else None
            ),
            provider_session=ctx.vwap_session,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        postprocess=lambda indicator: offset_indicator_indices(indicator, ctx.structure_offset),
    )


@dataclass(frozen=True)
class SmcStructureParams:
    pivot_len: int = 4
    equal_tolerance_atr: float = 0.12
    fvg_min_atr: float = 0.18
    fvg_displacement_atr: float = 0.55
    ob_min_displacement_atr: float = 0.45
    break_buffer_atr: float = 0.0
    max_events: int = 80
    max_boxes: int = 16
    zone_merge_atr: float = 0.45
    zone_touch_atr: float = 0.16
    target_r: float = 1.8
    trend_day_range_atr: float = 3.2
    trend_guide_fast_len: int = DEFAULT_INDICATOR_SETTINGS.ema.fast
    trend_guide_slow_len: int = DEFAULT_INDICATOR_SETTINGS.ema.slow
    trend_guide_roc_len: int = 6
    vix_stress_range_atr: float = 1.35
    vix_stress_rvol: float = 1.45
    trail_atr_mult: float = 1.45
    trail_activate_r: float = 0.70
    fvg_retention_bars: int = 48
    retained_fvg_boxes: int = 4
    score_watch: float = DEFAULT_INDICATOR_SETTINGS.score.watch
    score_arm: float = DEFAULT_INDICATOR_SETTINGS.score.arm
    score_go: float = DEFAULT_INDICATOR_SETTINGS.score.go


SmcChannelParams = SmcStructureParams


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> SmcStructureParams:
    return SmcStructureParams(
        pivot_len=int(raw.get("pivot_len", SmcStructureParams.pivot_len)),
        ob_min_displacement_atr=float(
            raw.get(
                "ob_min_displacement_atr",
                SmcStructureParams.ob_min_displacement_atr,
            )
        ),
        score_watch=defaults.score.watch,
        score_arm=defaults.score.arm,
        score_go=defaults.score.go,
        trend_guide_fast_len=defaults.ema.fast,
        trend_guide_slow_len=defaults.ema.slow,
    )


def _as_float(value: Any, fallback: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return fallback
        return float(value)
    except TypeError, ValueError:
        return fallback


def _event_scenario_kind(event: dict[str, Any]) -> ScenarioKind:
    value = event.get("scenario_kind") or event.get("structure_mode")
    if isinstance(value, ScenarioKind):
        return value
    if value in {ScenarioKind.FADE.value, ScenarioKind.TRANSIT.value}:
        return ScenarioKind(value)
    return ScenarioKind.WAIT


def _resolve_active_structure_mode(
    *,
    rebound: dict[str, Any],
    latest: dict[str, Any] | None,
) -> str:
    for item in (latest, rebound.get("best") if rebound.get("active") else None):
        if not isinstance(item, dict):
            continue
        signal = item.get("signal")
        if isinstance(signal, dict):
            kind_text = signal.get("kind") or item.get("structure_mode")
            if kind_text in {ScenarioKind.FADE.value, ScenarioKind.TRANSIT.value}:
                return kind_text
        mode = item.get("structure_mode")
        if mode in {ScenarioKind.FADE.value, ScenarioKind.TRANSIT.value}:
            return mode
    if rebound.get("active"):
        return ScenarioKind.FADE.value
    return ScenarioKind.WAIT.value


def _event_action(
    event: dict[str, Any],
    scenario_kind: ScenarioKind,
    *,
    score_bands: ScoreDefaults | None = None,
) -> str:
    score = float(event["score"])
    bands = score_bands or DEFAULT_INDICATOR_SETTINGS.score
    if scenario_kind == ScenarioKind.TRANSIT:
        return score_action(
            score, bands, go_floor=max(bands.arm - 2.0, bands.watch), pre_action="WAIT"
        )
    if scenario_kind == ScenarioKind.FADE:
        return "WATCH" if score >= bands.watch else "CANDIDATE"
    return "WAIT"


def _direction_for_kind(kind: str) -> Direction:
    if kind == "support":
        return Direction.LONG
    if kind == "resistance":
        return Direction.SHORT
    return Direction.FLAT


def _snapshot_for_params(bars: Sequence[Bar], params: SmcStructureParams) -> dict[str, Any]:
    return smc_snapshot(
        bars,
        SmcParams(
            pivot_len=params.pivot_len,
            equal_tolerance_atr=params.equal_tolerance_atr,
            fvg_min_atr=params.fvg_min_atr,
            fvg_displacement_atr=params.fvg_displacement_atr,
            ob_min_displacement_atr=params.ob_min_displacement_atr,
            break_buffer_atr=params.break_buffer_atr,
            max_events=params.max_events,
            max_boxes=params.max_boxes,
        ),
    )


def _structure_reaction_zones(
    bars: Sequence[Bar],
    snapshot: dict[str, Any],
    *,
    atr: float,
    params: SmcStructureParams,
) -> list[dict[str, Any]]:
    width = max(atr * params.zone_touch_atr, 0.000001)
    zones: list[dict[str, Any]] = []
    for level in snapshot.get("levels") or []:
        if not isinstance(level, dict):
            continue
        price = _as_float(level.get("price"), None)
        kind = str(level.get("kind") or "")
        if price is None or kind not in {"support", "resistance"}:
            continue
        zones.append(
            {
                "kind": kind,
                "low": price - width,
                "high": price + width,
                "midpoint": price,
                "sources": [str(level.get("name") or "SMC level")],
                "source_types": ["level"],
                "has_fvg": False,
                "has_order_block": False,
            }
        )

    structure_boxes = [*(snapshot.get("fvg_zones") or []), *(snapshot.get("order_blocks") or [])]
    for box in structure_boxes:
        if not isinstance(box, dict) or box.get("state") == "filled":
            continue
        box_type = str(box.get("type") or "").upper()
        is_fvg = box_type.startswith("FVG")
        is_order_block = box_type.startswith("OB")
        top = _as_float(box.get("top"), None)
        bottom = _as_float(box.get("bottom"), None)
        if top is None or bottom is None:
            continue
        direction = str(box.get("direction") or "")
        kind = "support" if direction == "long" else "resistance" if direction == "short" else ""
        if not kind:
            continue
        zones.append(
            {
                "kind": kind,
                "low": min(bottom, top),
                "high": max(bottom, top),
                "midpoint": _as_float(box.get("midpoint"), (bottom + top) * 0.5),
                "sources": [str(box.get("type") or ("FVG" if is_fvg else "OB"))],
                "source_types": ["fvg" if is_fvg else "order_block" if is_order_block else "box"],
                "has_fvg": is_fvg,
                "has_order_block": is_order_block,
            }
        )

    zones.sort(key=lambda item: (str(item["kind"]), float(item["midpoint"])))
    merged: list[dict[str, Any]] = []
    merge_distance = max(atr * params.zone_merge_atr, 0.000001)
    for zone in zones:
        if (
            merged
            and merged[-1]["kind"] == zone["kind"]
            and abs(float(merged[-1]["midpoint"]) - float(zone["midpoint"])) <= merge_distance
        ):
            current = merged[-1]
            current["low"] = min(float(current["low"]), float(zone["low"]))
            current["high"] = max(float(current["high"]), float(zone["high"]))
            current["midpoint"] = (float(current["low"]) + float(current["high"])) * 0.5
            current["sources"].extend(str(item) for item in zone.get("sources", []))
            current["source_types"] = list(
                dict.fromkeys([*current.get("source_types", []), *zone.get("source_types", [])])
            )
            current["has_fvg"] = bool(current.get("has_fvg")) or bool(zone.get("has_fvg"))
            current["has_order_block"] = bool(current.get("has_order_block")) or bool(
                zone.get("has_order_block")
            )
        else:
            merged.append(dict(zone))

    lookback = list(bars[-40:])
    start_index = len(bars) - len(lookback)
    for zone in merged:
        low = float(zone["low"])
        high = float(zone["high"])
        touch_count = 0
        last_touch_index = None
        for offset, bar in enumerate(lookback):
            if bar.low <= high and bar.high >= low:
                touch_count += 1
                last_touch_index = start_index + offset
        source_count = len(set(str(item) for item in zone.get("sources", [])))
        zone["touch_count"] = touch_count
        zone["last_touch_index"] = last_touch_index
        zone["score"] = min(
            52.0
            + touch_count * 4.0
            + (6.0 if zone.get("has_fvg") else 0.0)
            + min(source_count * 2.0, 8.0),
            84.0,
        )
    return merged


def _rebound_target(
    zones: Sequence[dict[str, Any]], entry: float, direction: Direction, fixed_r_target: float
) -> float:
    if direction == Direction.LONG:
        higher = [
            float(zone["midpoint"])
            for zone in zones
            if zone["kind"] == "resistance" and float(zone["midpoint"]) > entry
        ]
        return min(higher) if higher else fixed_r_target
    if direction == Direction.SHORT:
        lower = [
            float(zone["midpoint"])
            for zone in zones
            if zone["kind"] == "support" and float(zone["midpoint"]) < entry
        ]
        return max(lower) if lower else fixed_r_target
    return fixed_r_target


def _guide_vix_context(
    bars: Sequence[Bar],
    *,
    profile: InstrumentProfile,
    features: dict[str, Any],
    atr: float,
    params: SmcStructureParams,
) -> dict[str, Any]:
    latest = bars[-1]
    label = profile.label
    closes = [bar.close for bar in bars]
    fast = pine.ema_series(closes, params.trend_guide_fast_len)[-1]
    slow = pine.ema_series(closes, params.trend_guide_slow_len)[-1]
    roc_len = min(max(params.trend_guide_roc_len, 1), max(len(closes) - 1, 1))
    roc = (
        (latest.close - closes[-1 - roc_len]) / max(atr, 0.000001) if len(closes) > roc_len else 0.0
    )
    raw_guide = (
        1
        if latest.close > fast > slow and roc > 0
        else -1
        if latest.close < fast < slow and roc < 0
        else 0
    )
    guide_override = _as_float(features.get("smc_guide_direction"), None)
    guide_dir = int(guide_override) if guide_override in {-1.0, 0.0, 1.0} else raw_guide
    range_atr = max((latest.high - latest.low) / max(atr, 0.000001), 0.0)
    resolved_rvol, rvol_source = resolve_rvol_context(features)
    rvol = resolved_rvol if resolved_rvol is not None else 1.0
    resolved_baseline_rvol = _as_float(
        features.get("rvol_mean"),
        _as_float(features.get("relative_volume"), rvol),
    )
    baseline_rvol = rvol if resolved_baseline_rvol is None else resolved_baseline_rvol
    anatomy = pine.bar_anatomy(latest)
    is_snp = profile.key in {"ES", "SPY", "SPX_INDEX"}
    explicit_vix = str(features.get("smc_vix_state") or "").strip().upper()
    if explicit_vix:
        vix_state = explicit_vix
        vix_proxy = False
    else:
        stress = (
            is_snp
            and range_atr >= params.vix_stress_range_atr
            and rvol >= params.vix_stress_rvol
            and baseline_rvol >= max(params.vix_stress_rvol * 0.72, 1.05)
            and anatomy.close_pos <= 0.42
        )
        bid = (
            is_snp
            and range_atr >= params.vix_stress_range_atr * 0.80
            and rvol >= 1.15
            and baseline_rvol >= 1.0
            and anatomy.close_pos <= 0.50
        )
        relief = (
            is_snp
            and range_atr >= 0.75
            and anatomy.close_pos >= 0.62
            and latest.close > latest.open
        )
        vix_state = (
            "VIX HEDGE"
            if stress
            else "VIX UP"
            if bid
            else "VIX RELIEF"
            if relief
            else "VIX NEUTRAL"
            if is_snp
            else "VIX OFF"
        )
        vix_proxy = True
    return {
        "profile": label,
        "guide_dir": guide_dir,
        "guide_state": "GUIDE UP"
        if guide_dir == 1
        else "GUIDE DOWN"
        if guide_dir == -1
        else "GUIDE MIX",
        "guide_proxy": guide_override is None,
        "guide_roc_atr": _round(roc, 4),
        "vix_state": vix_state,
        "vix_proxy": vix_proxy,
        "range_atr": _round(range_atr, 4),
        "rvol": _round(rvol, 4),
        "baseline_rvol": _round(baseline_rvol, 4),
        "rvol_source": rvol_source,
    }


def _guide_score_adjustment(context: dict[str, Any], direction: Direction) -> float:
    guide_dir = int(context.get("guide_dir") or 0)
    score = 0.0
    if guide_dir == 1 and direction == Direction.LONG:
        score += 4.0
    elif guide_dir == -1 and direction == Direction.SHORT:
        score += 4.0
    elif guide_dir == 1 and direction == Direction.SHORT:
        score -= 6.0
    elif guide_dir == -1 and direction == Direction.LONG:
        score -= 6.0
    vix_state = str(context.get("vix_state") or "")
    if vix_state == "VIX HEDGE":
        score += 3.0 if direction == Direction.SHORT else -4.0
    elif vix_state == "VIX UP":
        score += 1.5 if direction == Direction.SHORT else -2.0
    elif vix_state == "VIX RELIEF":
        score += 3.0 if direction == Direction.LONG else -2.0
    return score


def _rebound_lifecycle(
    best: dict[str, Any], bars: Sequence[Bar], params: SmcStructureParams
) -> dict[str, Any]:
    latest = bars[-1]
    direction = direction_from_any(str(best.get("direction") or "flat"))
    entry = _as_float(best.get("entry"), None)
    stop = _as_float(best.get("stop"), None)
    target = _as_float(best.get("target"), None)
    if direction == Direction.FLAT or entry is None or stop is None:
        return PlanLifecycle(
            source="smc_structure",
            state=LifecycleState.WAIT,
            direction=Direction.FLAT,
        ).as_dict()
    risk = max(abs(entry - stop), 0.000001)
    if direction == Direction.LONG:
        filled = latest.high >= entry
        stop_hit = latest.low <= stop
        target_hit = target is not None and latest.high >= target
        mfe = max(latest.high - entry, 0.0)
        trail = max(stop, latest.high - params.trail_atr_mult * _latest_atr(bars))
    else:
        filled = latest.low <= entry
        stop_hit = latest.high >= stop
        target_hit = target is not None and latest.low <= target
        mfe = max(entry - latest.low, 0.0)
        trail = min(stop, latest.low + params.trail_atr_mult * _latest_atr(bars))
    if stop_hit and target_hit:
        state = LifecycleState.TARGET_AND_STOP_HIT
    elif stop_hit:
        state = LifecycleState.STOP_HIT
    elif target_hit:
        state = LifecycleState.TARGET_HIT
    elif filled and mfe / risk >= params.trail_activate_r:
        state = LifecycleState.RIDE
    elif filled:
        state = LifecycleState.FOLLOW
    else:
        state = LifecycleState.WAIT_ENTRY
    active = state.active
    exit_price = (
        stop
        if state
        in {
            LifecycleState.STOP_HIT,
            LifecycleState.TARGET_AND_STOP_HIT,
        }
        else target
    )
    lifecycle = PlanLifecycle(
        source="smc_structure",
        state=state,
        direction=direction,
        entry=entry,
        stop=stop,
        target=target,
        trail=trail if active and state is LifecycleState.RIDE else None,
        filled_ts=latest.ts.isoformat() if filled else None,
        closed_ts=latest.ts.isoformat() if state.terminal else None,
        exit_price=exit_price if state.terminal else None,
        exit_reason=state if state.terminal else None,
        filled=filled,
    ).as_dict()
    lifecycle["move_r"] = _round(mfe / risk, 3)
    return lifecycle


def _annotate_structure_box_lifecycle(
    boxes: Sequence[dict[str, Any]],
    *,
    latest_index: int,
    params: SmcStructureParams,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for box in boxes:
        item = dict(box)
        box_type = str(item.get("type") or "").upper()
        is_fvg = box_type.startswith("FVG")
        state = str(item.get("state") or "active")
        fill_index = _as_float(item.get("fill_index"), None)
        age_bars = int(max(latest_index - int(fill_index), 0)) if fill_index is not None else None
        retained = (
            is_fvg
            and state == "filled"
            and age_bars is not None
            and age_bars <= max(int(params.fvg_retention_bars), 0)
        )
        item["structure_box_type"] = (
            "fvg" if is_fvg else "order_block" if box_type.startswith("OB") else "box"
        )
        item["retained"] = retained
        item["lifecycle"] = {
            "state": state.upper(),
            "active": state != "filled",
            "retained": retained,
            "age_bars": age_bars,
            "fill_ts": item.get("fill_ts"),
        }
        annotated.append(item)
    return annotated


def _smc_object_facts(
    *,
    object_type: str,
    direction: str = "",
    kind: str = "",
    role: str = "",
    event_code: str = "",
) -> dict[str, Any]:
    object_key = str(object_type or "object")
    direction_key = str(direction or "")
    kind_key = str(kind or "")
    role_key = str(role or "")
    event_key = str(event_code or "")
    attributes = {
        "object_type": object_key,
        "direction": direction_key,
        "kind": kind_key,
        "role": role_key,
        "event_code": event_key,
    }
    attributes = {key: value for key, value in attributes.items() if value}
    return {
        "supporting": [{"code": "smc_confirmation", **attributes}],
        "opposing": [{"code": "smc_invalidation", **attributes}],
        "context": [
            {"code": "smc_bias", **attributes},
            {"code": "smc_importance", **attributes},
        ],
    }


def _smc_event_fact_fields(event: dict[str, Any]) -> dict[str, Any]:
    event_code = event.get("event_code") or "smc_structure_event"
    object_facts = _smc_object_facts(
        object_type="event",
        direction=str(event.get("direction") or ""),
        event_code=event_code,
    )
    return indicator_fact_payload(
        scenario="smc_structure_event",
        setup=event_code,
        trigger_event={"code": event_code},
        quality={"code": "score", "value": event.get("score")},
        **object_facts,
        metrics={
            "direction": str(event.get("direction") or "").lower(),
            "score": _metric_number(event.get("score"), digits=0),
            "price": _metric_number(event.get("price")),
            "entry": _metric_number(event.get("entry")),
            "stop": _metric_number(event.get("stop")),
            "target": _metric_number(event.get("target")),
        },
    )


def _smc_box_fact_fields(box: dict[str, Any]) -> dict[str, Any]:
    lifecycle = box.get("lifecycle") if isinstance(box.get("lifecycle"), dict) else {}
    bottom = _as_float(box.get("bottom"), None)
    top = _as_float(box.get("top"), None)
    size_atr = _as_float(box.get("size_atr"), None)
    age_bars = lifecycle.get("age_bars")
    box_type = str(box.get("structure_box_type") or box.get("type") or "smc_box")
    lifecycle_state = str(lifecycle.get("state") or box.get("state") or "active").lower()
    object_facts = _smc_object_facts(
        object_type="box",
        direction=str(box.get("direction") or ""),
        kind=box_type,
        role=box_type,
    )
    context_facts = list(object_facts["context"])
    if box.get("state") == "filled" and age_bars is not None:
        context_facts.append({"code": "smc_retained_filled_zone", "age_bars": age_bars})
    return indicator_fact_payload(
        scenario="smc_structure_box",
        trigger_event={"code": "smc_zone_state", "state": lifecycle_state, "box_type": box_type},
        supporting=object_facts["supporting"],
        opposing=object_facts["opposing"],
        context=context_facts,
        metrics={
            "direction": str(box.get("direction") or "").lower(),
            "lifecycle_state": lifecycle_state,
            "origin_bar": _metric_number(box.get("start_index"), digits=0),
            "zone_bottom": _metric_number(bottom),
            "zone_top": _metric_number(top),
            "midpoint": _metric_number(box.get("midpoint")),
            "size_atr": _metric_number(size_atr, suffix=" ATR"),
            "age_bars": _metric_number(age_bars, digits=0),
        },
    )


def _smc_level_fact_fields(level: dict[str, Any]) -> dict[str, Any]:
    kind = str(level.get("kind") or "")
    role = str(level.get("role") or "")
    return indicator_fact_payload(
        scenario="smc_structure_level",
        trigger_event={"code": "smc_level_reaction_confirmation"},
        **_smc_object_facts(object_type="level", kind=kind, role=role),
        metrics={"price": _metric_number(level.get("price"))},
    )


def _rebound_context(
    bars: Sequence[Bar],
    snapshot: dict[str, Any],
    *,
    profile: InstrumentProfile,
    features: dict[str, Any] | None,
    channel_context: Mapping[str, Any] | None,
    provider_session: ProviderSessionReset | None,
    params: SmcStructureParams,
) -> dict[str, Any]:
    session_levels = provider_session_level_context(
        bars,
        provider_session,
    ).to_payload()
    if len(bars) < 8:
        return {
            "active": False,
            "zones": [],
            "trend_day_warning": False,
            "session_levels": session_levels,
        }

    latest = bars[-1]
    atr = _latest_atr(bars, features)
    anatomy = pine.bar_anatomy(latest)
    zones = _structure_reaction_zones(bars, snapshot, atr=atr, params=params)
    levels = {
        key: session_levels.get(key)
        for key in (
            "or_high",
            "or_low",
            "ib_high",
            "ib_low",
            "previous_session_high",
            "previous_session_low",
        )
    }
    guide = _guide_vix_context(
        bars, profile=profile, features=features or {}, atr=atr, params=params
    )
    current_high = _as_float(
        session_levels.get("current_session_high"),
        None,
    )
    current_low = _as_float(
        session_levels.get("current_session_low"),
        None,
    )
    current_range_atr = (
        (current_high - current_low) / atr
        if current_high is not None and current_low is not None
        else None
    )
    trend_day = bool(
        session_levels.get("available") is True
        and current_range_atr is not None
        and current_range_atr >= params.trend_day_range_atr
        and (anatomy.close_pos >= 0.72 or anatomy.close_pos <= 0.28)
    )
    features = features or {}
    channel_nearest = (
        channel_context.get("nearest")
        if isinstance(channel_context, Mapping)
        and isinstance(channel_context.get("nearest"), Mapping)
        else {}
    )
    manual_dir = direction_from_any(str(channel_nearest.get("direction") or "flat"))
    manual_touch = bool(
        (channel_nearest.get("touch") or channel_nearest.get("reclaim"))
        and channel_nearest.get("no_fade") is not True
    )
    manual_price = _as_float(channel_nearest.get("price"), None)

    candidates: list[dict[str, Any]] = []
    for zone in zones:
        low = float(zone["low"])
        high = float(zone["high"])
        midpoint = float(zone["midpoint"])
        kind = str(zone["kind"])
        if not (latest.low <= high and latest.high >= low):
            continue
        direction = _direction_for_kind(kind)
        reclaim = kind == "support" and latest.close >= midpoint and anatomy.close_pos >= 0.42
        reject = kind == "resistance" and latest.close <= midpoint and anatomy.close_pos <= 0.58
        if not reclaim and not reject:
            continue
        structural_stop = low if direction == Direction.LONG else high
        entry = latest.close
        stop = anchored_invalidation_stop(direction, structural_stop, latest, atr_pad=atr * 0.22)
        fixed_r_target = (
            entry + abs(entry - stop) * params.target_r
            if direction == Direction.LONG
            else entry - abs(entry - stop) * params.target_r
        )
        # R-based target selection belongs here, after the final protective stop.
        # A closer structural target remains fixed and must pass the same RR gate.
        plan = normalize_trade_plan(
            direction,
            entry,
            stop,
            _rebound_target(zones, entry, direction, fixed_r_target),
            min_rr=params.target_r,
        )
        if not plan["coherent"]:
            continue
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        target = float(plan["target"])
        score = float(zone["score"])
        score += 8.0 if zone.get("touch_count", 0) >= 2 else 0.0
        score += 6.0 if zone.get("has_fvg") else 0.0
        score += _guide_score_adjustment(guide, direction)
        if (
            manual_touch
            and manual_dir == direction
            and manual_price is not None
            and abs(manual_price - midpoint) <= atr * 0.55
        ):
            score += 5.0
        confluence_names = (
            ("or_low", "ib_low", "previous_session_low")
            if direction == Direction.LONG
            else ("or_high", "ib_high", "previous_session_high")
        )
        confluence = []
        for name in confluence_names:
            level = _as_float(levels.get(name), None)
            if level is not None and abs(level - midpoint) <= atr * 0.50:
                score += 3.0
                confluence.append(name)
        score -= (
            6.0
            if trend_day
            and (
                (direction == Direction.LONG and guide.get("guide_dir") == -1)
                or (direction == Direction.SHORT and guide.get("guide_dir") == 1)
            )
            else 0.0
        )
        score = min(score, 99.0)
        action = (
            "GO"
            if score
            >= label_importance_floor(
                _score_bands(
                    watch=params.score_watch,
                    arm=params.score_arm,
                    go=params.score_go,
                )
            )
            else "WATCH"
        )
        zone_type = (
            "fvg_order_block"
            if zone.get("has_fvg") and zone.get("has_order_block")
            else "fvg"
            if zone.get("has_fvg")
            else "order_block"
            if zone.get("has_order_block")
            else "demand"
            if direction == Direction.LONG
            else "supply"
        )
        event_code = "reclaim" if direction == Direction.LONG else "reject"
        object_facts = _smc_object_facts(
            object_type="zone",
            direction=direction.value,
            kind=kind,
            role=zone_type,
            event_code=event_code,
        )
        opposing_facts = list(object_facts["opposing"])
        if trend_day:
            opposing_facts.append({"code": "smc_trend_day_warning"})
        candidates.append(
            {
                "ts": latest.ts.isoformat(),
                "index": len(bars) - 1,
                "price": _round(entry),
                "level": _round(midpoint),
                "direction": direction.value,
                "event_code": event_code,
                "scenario_kind": ScenarioKind.FADE.value,
                "zone_type": zone_type,
                "action": action,
                "score": round(score, 2),
                "entry": _round(entry),
                "stop": _round(stop),
                "target": _round(target),
                **indicator_fact_payload(
                    scenario="smc_structure_rebound",
                    setup=zone_type,
                    trigger_event={
                        "code": event_code,
                        "zone_type": zone_type,
                        "direction": direction.value,
                    },
                    supporting=object_facts["supporting"],
                    opposing=opposing_facts,
                    context=object_facts["context"],
                    quality={"code": "score", "value": round(score, 2)},
                    fact_groups=[
                        {
                            "kind": "smc_rebound_context",
                            "items": [
                                {
                                    "code": "smc_zone_sources",
                                    "sources": list(
                                        dict.fromkeys(str(item) for item in zone.get("sources", []))
                                    ),
                                },
                                {"code": "smc_confluence", "levels": list(confluence)},
                                {"code": "smc_guide_state", "state": guide["guide_state"]},
                                {"code": "smc_vix_state", "state": guide["vix_state"]},
                            ],
                        }
                    ],
                    metrics={
                        "score": _metric_number(score, digits=0),
                        "level": _metric_number(midpoint),
                        "zone_touches": _metric_number(zone.get("touch_count"), digits=0),
                        "guide_state": guide["guide_state"],
                        "vix_state": guide["vix_state"],
                    },
                ),
                "zone": {
                    "kind": kind,
                    "low": _round(low),
                    "high": _round(high),
                    "midpoint": _round(midpoint),
                    "touch_count": int(zone.get("touch_count") or 0),
                    "sources": list(dict.fromkeys(str(item) for item in zone.get("sources", []))),
                    "source_types": list(
                        dict.fromkeys(str(item) for item in zone.get("source_types", []))
                    ),
                    "has_fvg": bool(zone.get("has_fvg")),
                    "has_order_block": bool(zone.get("has_order_block")),
                },
                "trend_day_warning": bool(trend_day),
                "guide": guide,
                "confluence": confluence,
            }
        )

    zone_payload = [
        {
            "kind": zone["kind"],
            "low": _round(zone["low"]),
            "high": _round(zone["high"]),
            "midpoint": _round(zone["midpoint"]),
            "touch_count": int(zone.get("touch_count") or 0),
            "sources": list(dict.fromkeys(str(item) for item in zone.get("sources", []))),
            "source_types": list(dict.fromkeys(str(item) for item in zone.get("source_types", []))),
            "has_fvg": bool(zone.get("has_fvg")),
            "has_order_block": bool(zone.get("has_order_block")),
            "score": round(float(zone["score"]), 2),
        }
        for zone in zones[-8:]
    ]
    base = {
        "zones": zone_payload,
        "trend_day_warning": bool(trend_day),
        "guide": guide,
        "session_levels": session_levels,
        **{key: _round(value) for key, value in levels.items()},
    }
    if not candidates:
        return {"active": False, **base}
    best = max(candidates, key=lambda item: float(item["score"]))
    return {
        "active": True,
        "best": best,
        "lifecycle": _rebound_lifecycle(best, bars, params),
        **base,
    }


def _market_context_veto_reason(direction: Direction, market_context: dict[str, Any] | None) -> str:
    if direction == Direction.FLAT or not isinstance(market_context, dict):
        return ""
    side_gate = (
        market_context.get("side_gate") if isinstance(market_context.get("side_gate"), dict) else {}
    )
    if direction == Direction.SHORT and bool(side_gate.get("veto_short")):
        return "market_spotlight_veto_short"
    if direction == Direction.LONG and bool(side_gate.get("veto_long")):
        return "market_spotlight_veto_long"
    return ""


def _normalize_event_plan(
    event: dict[str, Any],
    bars: Sequence[Bar],
    *,
    features: dict[str, Any] | None = None,
    min_rr: float | None = None,
) -> str:
    direction = direction_from_any(str(event.get("direction") or "flat"))
    entry = _as_float(event.get("entry"), None)
    if entry is None:
        entry = _as_float(event.get("price"), None)
    stop = _as_float(event.get("stop"), None)
    target = _as_float(event.get("target"), None)
    if direction == Direction.FLAT or entry is None or stop is None or target is None:
        return ""
    plan = normalize_trade_plan(
        direction,
        entry,
        stop,
        target,
        bar=bars[-1],
        atr_pad=_latest_atr(bars, features) * 0.22,
        min_rr=min_rr,
    )
    if not plan.get("coherent"):
        return str(plan.get("blocked_reason") or "incoherent_trade_plan")
    event["entry"] = plan.get("entry")
    event["stop"] = plan.get("stop")
    event["target"] = plan.get("target")
    return ""


def _attach_event_signal(
    event: dict[str, Any],
    bars: Sequence[Bar],
    *,
    score_bands: ScoreDefaults | None = None,
    market_context: dict[str, Any] | None = None,
    plan_blocked_reason: str = "",
) -> None:
    direction = direction_from_any(str(event.get("direction") or "flat"))
    bands = score_bands or DEFAULT_INDICATOR_SETTINGS.score
    scenario_kind = _event_scenario_kind(event)
    preset_action = str(event.get("action") or "").upper()
    score = float(event["score"])
    if scenario_kind == ScenarioKind.WAIT:
        action = "WAIT"
    elif preset_action == "GO" and score >= label_importance_floor(bands):
        action = "GO"
    else:
        action = _event_action(event, scenario_kind, score_bands=bands)
    latest = bars[-1]
    entry = _as_float(event.get("entry"), None)
    if entry is None:
        entry = _as_float(event.get("price"), None)
    stop = _as_float(event.get("stop"), None)
    target = _as_float(event.get("target"), None)
    blocked_reason = plan_blocked_reason
    if blocked_reason and action == "GO":
        action = "WATCH"
    raw_action = action
    market_veto_reason = _market_context_veto_reason(direction, market_context)
    if action == "GO" and market_veto_reason:
        action = "BLOCK"
        blocked_reason = market_veto_reason
        event["market_context_gate"] = {
            "source": "market_spotlight",
            "blocked": True,
            "reason": market_veto_reason,
            "direction": direction.value,
            "side_lock": market_context.get("side_lock")
            if isinstance(market_context, dict)
            else None,
            "market_state": market_context.get("state")
            if isinstance(market_context, dict)
            else None,
        }
    event["action"] = action
    event["structure_mode"] = scenario_kind.value
    event["details"] = {
        **(event.get("details") if isinstance(event.get("details"), dict) else {}),
        "structure_mode": event["structure_mode"],
    }
    event["signal"] = SignalState(
        source="smc_structure",
        action=ActionPhase(action),
        raw_action=raw_action,
        direction=direction,
        score=score,
        confirmed=True,
        source_tf=latest.timeframe,
        trigger=_as_float(entry, None) if direction != Direction.FLAT else None,
        stop=_as_float(stop, None),
        target=_as_float(target, None),
        invalidation=_as_float(stop, None),
        reason=str(event.get("event_code") or "smc_structure_event"),
        blocked_reason=blocked_reason,
        code=str(event.get("event_code") or ""),
        kind=scenario_kind,
    ).as_dict()


def smc_structure(
    bars: Sequence[Bar],
    params: SmcStructureParams | None = None,
    *,
    profile: InstrumentProfile,
    features: dict[str, Any] | None = None,
    market_context: dict[str, Any] | None = None,
    channel_context: Mapping[str, Any] | None = None,
    provider_session: ProviderSessionReset | None = None,
) -> dict[str, Any]:
    params = params or SmcStructureParams()
    score_bands = _score_bands(
        watch=params.score_watch,
        arm=params.score_arm,
        go=params.score_go,
    )
    if not bars:
        session_levels = provider_session_level_context(
            bars,
            provider_session,
        ).to_payload()
        return {
            "version": "6-python-structure",
            "pivots": [],
            "events": [],
            "boxes": [],
            "levels": [],
            "rebound": {
                "active": False,
                "zones": [],
                "trend_day_warning": False,
                "session_levels": session_levels,
            },
            "latest": None,
            "overlays": [],
        }

    features = dict(features or {})
    features.setdefault("latest_atr", _latest_atr(bars, features))
    overlays: list[dict[str, Any]] = []
    snapshot = _snapshot_for_params(bars, params)
    events: list[dict[str, Any]] = snapshot["events"][-params.max_events :]
    for event in events:
        event.update(_smc_event_fact_fields(event))
        plan_blocked_reason = _normalize_event_plan(event, bars, features=features)
        _attach_event_signal(
            event,
            bars,
            score_bands=score_bands,
            market_context=market_context,
            plan_blocked_reason=plan_blocked_reason,
        )

    structure_boxes = [*(snapshot.get("fvg_zones") or []), *(snapshot.get("order_blocks") or [])]
    boxes: list[dict[str, Any]] = _annotate_structure_box_lifecycle(
        structure_boxes[-params.max_boxes :],
        latest_index=len(bars) - 1,
        params=params,
    )
    active_boxes = [box for box in boxes if box.get("state") != "filled"]
    retained_boxes = [box for box in boxes if box.get("retained")]
    display_boxes = [*retained_boxes[-max(int(params.retained_fvg_boxes), 0) :], *active_boxes[-8:]]
    if not display_boxes:
        display_boxes = boxes[-6:]
    last_ts = bars[-1].ts.isoformat()
    first_visible_ts = bars[max(0, len(bars) - 90)].ts.isoformat()
    bar_by_ts = {bar.ts.isoformat(): bar for bar in bars}

    for pivot in snapshot.get("pivots", [])[-8:]:
        try:
            pivot_index = int(pivot.get("index"))
            pivot_type = int(pivot.get("type"))
            pivot_price = float(pivot.get("price"))
        except TypeError, ValueError:
            continue
        if pivot_index < 0 or pivot_index >= len(bars):
            continue
        pivot_bar = bars[pivot_index]
        candle_range = max(float(pivot_bar.high - pivot_bar.low), 0.000001)
        if pivot_type == 1:
            top = pivot_price
            bottom = min(top - candle_range * 0.18, max(pivot_bar.open, pivot_bar.close))
            zone_kind = "supply"
            fact_fields = indicator_fact_payload(
                scenario="smc_supply_zone",
                trigger_event={"code": "smc_supply_confirmation"},
                **_smc_object_facts(
                    object_type="zone",
                    direction="short",
                    kind="resistance",
                    event_code="supply_pivot",
                ),
                metrics={"swing_high": _metric_number(pivot_price)},
            )
        elif pivot_type == -1:
            bottom = pivot_price
            top = max(bottom + candle_range * 0.18, min(pivot_bar.open, pivot_bar.close))
            zone_kind = "demand"
            fact_fields = indicator_fact_payload(
                scenario="smc_demand_zone",
                trigger_event={"code": "smc_demand_confirmation"},
                **_smc_object_facts(
                    object_type="zone", direction="long", kind="support", event_code="demand_pivot"
                ),
                metrics={"swing_low": _metric_number(pivot_price)},
            )
        else:
            continue
        if top <= bottom:
            continue
        overlay_direction = "short" if pivot_type == 1 else "long"
        overlays.append(
            {
                "type": "box",
                "retention": "active",
                "role": f"smc_{zone_kind}_zone",
                "zone_kind": zone_kind,
                "direction": overlay_direction,
                "start_ts": pivot_bar.ts.isoformat(),
                "end_ts": last_ts,
                "top": _round(top),
                "bottom": _round(bottom),
                "style": "dashed",
                "opacity": 0.07,
                "border_opacity": 0.54,
                **fact_fields,
            }
        )
        overlays.append(
            {
                "type": "line",
                "retention": "active",
                "role": "smc_pivot_level",
                "zone_kind": zone_kind,
                "direction": overlay_direction,
                "start_ts": pivot_bar.ts.isoformat(),
                "end_ts": last_ts,
                "y1": _round(pivot_price),
                "y2": _round(pivot_price),
                "label": "SUP↓" if zone_kind == "supply" else "DEM↑",
                "label_side": "above" if zone_kind == "supply" else "below",
                "label_position": "center",
                "label_font_size": 8,
                "tone": "negative" if overlay_direction == "short" else "positive",
                "style": "dotted",
                "width": 0.8,
                **fact_fields,
            }
        )

    for box in display_boxes:
        box_type = str(box.get("type") or "").upper()
        is_fvg = box_type.startswith("FVG")
        is_ob = box_type.startswith("OB")
        filled = box.get("state") == "filled"
        lifecycle = box.get("lifecycle") if isinstance(box.get("lifecycle"), dict) else {}
        fact_fields = _smc_box_fact_fields(box)
        role = (
            "retained_fvg"
            if is_fvg and filled
            else "fvg"
            if is_fvg
            else "order_block"
            if is_ob
            else "smc_box"
        )
        zone_kind = "fvg" if is_fvg else "order_block" if is_ob else "smc"
        lifecycle_fields = {
            "role": role,
            "smc_state": str(box.get("state") or "active"),
            "smc_lifecycle": lifecycle,
            "origin_end_ts": box.get("end_ts") or box["start_ts"],
            "fill_ts": box.get("fill_ts"),
        }
        box_state = str(box.get("state") or "active").lower()
        overlays.append(
            {
                "type": "box",
                "retention": "active",
                "direction": box["direction"],
                "start_ts": box["start_ts"],
                "end_ts": box.get("fill_ts") or last_ts,
                "top": box["top"],
                "bottom": box["bottom"],
                "style": "dotted",
                "pattern": "hatch",
                "opacity": 0.035 if box_state == "filled" else 0.08,
                "border_opacity": 0.26
                if box_state == "filled"
                else 0.35
                if box_state == "mitigated"
                else 0.60,
                "pattern_opacity": 0.16 if box_state == "filled" else 0.22,
                "badge_facts": [
                    {
                        "code": "smc_zone",
                        "kind": zone_kind,
                        "direction": box["direction"],
                        "state": box.get("state") or "active",
                        "retained": filled and is_fvg,
                    }
                ],
                **fact_fields,
                **lifecycle_fields,
            }
        )
        overlays.append(
            {
                "type": "line",
                "retention": "active",
                "direction": box["direction"],
                "start_ts": box["start_ts"],
                "end_ts": box.get("fill_ts") or last_ts,
                "y1": box["midpoint"],
                "y2": box["midpoint"],
                "label": "",
                "tone": "positive" if box["direction"] == "long" else "negative",
                "style": "dotted",
                "width": 0.75,
                **fact_fields,
                **lifecycle_fields,
            }
        )

    for level in snapshot.get("levels", [])[-4:]:
        price = level.get("price")
        if price is None:
            continue
        kind = str(level.get("kind") or "")
        level_name = str(level.get("name") or "smc_level").strip().lower().replace(" ", "_")
        direction = "long" if kind == "support" else "short"
        level_labels = {
            "smc_swing_high": "SH↓",
            "smc_swing_low": "SL↑",
            "smc_previous_high": "PSH↓",
            "smc_previous_low": "PSL↑",
        }
        overlays.append(
            {
                "type": "line",
                "retention": "active",
                "role": "smc_level",
                "level_kind": kind,
                "level_name": level_name,
                "direction": direction,
                "start_ts": first_visible_ts,
                "end_ts": last_ts,
                "y1": price,
                "y2": price,
                "label": level_labels.get(level_name, level_name.replace("_", " ").upper()),
                "label_side": "below" if direction == "long" else "above",
                "label_position": "center",
                "label_font_size": 8,
                "tone": "positive" if direction == "long" else "negative",
                "style": "dashed",
                "width": 0.85,
                **_smc_level_fact_fields(level),
            }
        )

    rebound = _rebound_context(
        bars,
        snapshot,
        profile=profile,
        features=features,
        channel_context=channel_context,
        provider_session=provider_session,
        params=params,
    )
    if rebound.get("active"):
        best = dict(rebound["best"])
        _attach_event_signal(best, bars, score_bands=score_bands, market_context=market_context)
        best["lifecycle"] = rebound.get("lifecycle")
        events.append(best)
        direction = direction_from_any(str(best.get("direction") or "flat"))
        zone = best.get("zone") or {}
        if zone:
            overlays.append(
                {
                    "type": "box",
                    "retention": "active",
                    "role": "smc_rebound_zone",
                    "direction": direction.value,
                    "start_ts": bars[max(0, len(bars) - 36)].ts.isoformat(),
                    "end_ts": last_ts,
                    "top": zone.get("high"),
                    "bottom": zone.get("low"),
                    "style": "solid",
                    "opacity": 0.10,
                    "border_opacity": 0.78,
                    "badge_facts": [
                        {
                            "code": "smc_rebound",
                            "direction": direction.value,
                            "event_code": str(best.get("event_code") or "smc_rebound"),
                        }
                    ],
                    **copy_indicator_facts(best),
                }
            )
        lifecycle = rebound.get("lifecycle") if isinstance(rebound.get("lifecycle"), dict) else {}

    for event in events:
        event_code = str(event.get("event_code") or "")
        if event_code in {"break_of_structure", "change_of_character"}:
            is_choch = event_code == "change_of_character"
            direction = str(event["direction"])
            overlays.append(
                {
                    "type": "line",
                    "role": "smc_structure_change" if is_choch else "smc_structure_break",
                    "event_code": event_code,
                    "direction": direction,
                    "start_ts": event.get("start_ts") or event["ts"],
                    "end_ts": event["ts"],
                    "y1": event["price"],
                    "y2": event["price"],
                    "label": "CHOCH" if is_choch else "BOS",
                    "label_side": "below" if direction == "long" else "above",
                    "label_position": "center",
                    "label_font_size": 8,
                    "tone": "positive" if direction == "long" else "negative",
                    "style": "solid",
                    "width": 1.45 if is_choch else 0.95,
                    "line_variant": "double" if is_choch else "single",
                    **copy_indicator_facts(event),
                }
            )
        event_bar = bar_by_ts.get(str(event.get("ts") or "")) or bars[-1]
        item = overlay_primitives.label(
            bar=event_bar,
            price=float(event["price"]),
            lines=[],
            direction=str(event["direction"]),
            side="below" if event["direction"] == "long" else "above",
            role="smc_structure",
        )
        item["source"] = "smc_channels"
        item["code"] = event_code or "smc_structure_event"
        item["score"] = event["score"]
        item["action"] = event.get("action") or "WATCH"
        item.update(copy_indicator_facts(event))
        signal_payload = event.get("signal") if isinstance(event.get("signal"), dict) else None
        if signal_payload:
            overlay_primitives.bind_signal_trade_plan(item, signal_payload, source="smc_structure")
        else:
            item["raw_action"] = item["action"]
        overlays.append(item)

    latest = events[-1] if events else None
    structure_mode = _resolve_active_structure_mode(rebound=rebound, latest=latest)
    if isinstance(latest, dict):
        latest = {**latest, "structure_mode": structure_mode}
    return {
        "version": "6-python-structure",
        "core_version": snapshot["version"],
        "params": snapshot["params"],
        "structure_mode": structure_mode,
        "pivots": snapshot["pivots"][-80:],
        "events": events,
        "fvg_zones": [box for box in boxes if box.get("structure_box_type") == "fvg"],
        "order_blocks": [box for box in boxes if box.get("structure_box_type") == "order_block"],
        "boxes": boxes,
        "levels": snapshot["levels"],
        "rebound": rebound,
        "latest": latest,
        "overlays": overlays,
    }


def smc_channels(
    bars: Sequence[Bar],
    params: SmcChannelParams | None = None,
    *,
    profile: InstrumentProfile,
    features: dict[str, Any] | None = None,
    provider_session: ProviderSessionReset | None = None,
) -> dict[str, Any]:
    return smc_structure(
        bars,
        profile=profile,
        params=params,
        features=features,
        provider_session=provider_session,
    )
