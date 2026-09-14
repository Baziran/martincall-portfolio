from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    SignalCandidate,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.defaults import IndicatorDefaults, score_action
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.indicators.module_contract import (
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.modules._structure_common import (
    _atr_value,
    _pivot_points,
)
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
    offset_indicator_indices,
)
from aef_terminal.runtime import overlays
from aef_terminal.runtime.math_utils import (
    bool_param,
    clamp as _clamp,
    float_or_none,
    round_optional as _round,
)
from aef_terminal.runtime.pivots import PivotContext
from aef_terminal.runtime.signal_state import SignalState
from aef_terminal.signals.candidates import candidate_from_trade_plan_item
from aef_terminal.signals.trade_plan import anchored_invalidation_stop, normalize_trade_plan


_ITEM_FIELDS = (
    "action",
    "code",
    "candidate_finality",
    "details",
    "direction",
    "epa",
    "event_type",
    "id",
    "invalid",
    "label",
    "level",
    "p1",
    "p2",
    "p3",
    "p4",
    "pivots",
    "price",
    "end_anchor_ts",
    "end_bar_offset",
    "projected_index",
    "projected_p5",
    "reason_code",
    "score",
    "signal",
    "source",
    "symbol",
    "target",
    "timeframe",
    "trigger",
    "trigger_type",
    "ts",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


INDICATOR_MODULES = (
    IndicatorModule(
        IndicatorSpec(
            "wolfe_structure",
            "Wolfe Structure",
            "pivots, Wolfe path, candidates and confirmed patterns",
            {
                "version": "1.1-python",
                "settings": {"pivot_len": 3},
                "pivots": [],
                "paths": [],
                "candidates": [],
                "events": [],
                "overlays": [],
                "latest": None,
            },
            "wolfeStructure",
            "aef_terminal.indicators.modules.wolfe_structure:wolfe_structure",
            group="structure",
            pipeline_stage="structure",
            pipeline_order=120,
            candidate_promoter="wolfe",
            custom_renderer=True,
            renderer_kind="custom",
            renderer_ref="structure_indicators",
            renderer_primitives=("line", "label", "marker", "structure_paths"),
            renderer_placements=("price",),
            custom_renderer_requirements=("price_overlay_provider", "structure_snapshot"),
            score_family="structure",
            empirical_power=0.95,
            usefulness=0.95,
            module_type="context",
            state_key="wolfeStructure",
            calc_key="wolfeStructureCalcEnabled",
            visible_key="wolfeStructureVisible",
            chart_control_id="wolfe-structure-toggle",
            process_control_id="wolfe-structure-process",
            api_enabled_key="wolfe_enabled",
            manager_order=120,
            runtime_order=110,
            default_calc=False,
            overlay_layer="signals",
            runtime_payload_contract={
                "latest": _ITEM_FIELDS,
                "paths": _ITEM_FIELDS,
                "candidates": _ITEM_FIELDS,
                "events": _ITEM_FIELDS,
            },
            runtime_payload_schema_refs=("overlay_contract",),
            controls=(
                _control(
                    "pivots",
                    "Pivots",
                    "toggle",
                    True,
                    "wolfeStructurePivots",
                    element_id="wolfe-structure-pivots",
                    compact=True,
                ),
                _control(
                    "paths",
                    "Paths",
                    "toggle",
                    True,
                    "wolfeStructurePaths",
                    element_id="wolfe-structure-paths",
                    compact=True,
                ),
                _control(
                    "heuristicPath",
                    "Heuristic",
                    "toggle",
                    False,
                    "wolfeStructureHeuristicPath",
                    element_id="wolfe-structure-heuristic-path",
                ),
                _control(
                    "candidates",
                    "Candidates",
                    "toggle",
                    True,
                    "wolfeStructureCandidates",
                    element_id="wolfe-structure-candidates",
                    api_key="wolfe_show_candidates",
                    param_key="show_candidates",
                    action="load_apply",
                ),
                _control(
                    "priceTarget",
                    "Target",
                    "toggle",
                    True,
                    "wolfeStructurePriceTarget",
                    element_id="wolfe-structure-price-target",
                    api_key="wolfe_show_price_target",
                    param_key="show_price_target",
                    action="load_apply",
                ),
                _control(
                    "confirmedLimit",
                    "Confirmed",
                    "number",
                    2,
                    "wolfeStructureConfirmedLimit",
                    element_id="wolfe-structure-confirmed-limit",
                    api_key="wolfe_confirmed_limit",
                    param_key="confirmed_limit",
                    minimum=1,
                    maximum=3,
                    step=1,
                    action="load_apply",
                ),
                _control(
                    "opacity",
                    "Opacity",
                    "number",
                    0.75,
                    "wolfeStructureOpacity",
                    element_id="wolfe-structure-opacity",
                    api_key="wolfe_line_opacity",
                    param_key="line_opacity",
                    minimum=0.15,
                    maximum=1,
                    step=0.05,
                ),
                _control(
                    "pivotLen",
                    "Pivot",
                    "number",
                    0,
                    "wolfeStructurePivotLen",
                    element_id="wolfe-structure-pivot-len",
                    api_key="wolfe_pivot_len",
                    param_key="pivot_len",
                    minimum=0,
                    maximum=12,
                    step=1,
                    action="load",
                ),
            ),
        ),
        adapter_ref="aef_terminal.indicators.modules.wolfe_structure:build_wolfe_execution_spec",
        params_builder_ref="aef_terminal.indicators.modules.wolfe_structure:build_params",
        candidate_promoter_ref="aef_terminal.indicators.modules.wolfe_structure:signal_candidates_from_wolfe_indicator",
    ),
)


@dataclass(frozen=True)
class WolfeStructureParams:
    pivot_len: int = 0
    confirmed_limit: int = 2
    show_candidates: bool = True
    show_price_target: bool = True
    line_opacity: float = 0.75


def build_params(raw: dict[str, Any], _defaults: IndicatorDefaults) -> WolfeStructureParams:
    return WolfeStructureParams(
        pivot_len=max(0, min(int(raw.get("pivot_len", WolfeStructureParams.pivot_len)), 12)),
        confirmed_limit=max(
            1,
            min(
                int(raw.get("confirmed_limit", WolfeStructureParams.confirmed_limit)),
                3,
            ),
        ),
        show_candidates=bool_param(
            raw.get("show_candidates"),
            WolfeStructureParams.show_candidates,
        ),
        show_price_target=bool_param(
            raw.get("show_price_target"),
            WolfeStructureParams.show_price_target,
        ),
        line_opacity=max(
            0.15,
            min(
                float(raw.get("line_opacity", WolfeStructureParams.line_opacity)),
                1.0,
            ),
        ),
    )


def _candidate_from_wolfe_item(
    item: dict[str, Any],
    name: str,
    score_floor: float,
    features: dict[str, Any] | None,
) -> SignalCandidate | None:
    if item.get("candidate_finality") != CandidateFinality.CONFIRMED.value:
        return None
    direction_raw = item.get("direction")
    if direction_raw not in {Direction.LONG.value, Direction.SHORT.value}:
        return None
    base_score = float_or_none(item.get("score"))
    if base_score is None:
        return None
    if features:
        rvol = float_or_none(features.get("rvol_adaptive"))
        rejection = float_or_none(features.get("rejection_score"))
        base_score += ((1.0 if rvol is None else rvol) - 1.0) * 8.0
        base_score += ((0.0 if rejection is None else rejection) / 100.0) * 12.0
    score = max(0.0, min(99.0, base_score))
    if score < score_floor:
        return None

    epa = item.get("epa") if isinstance(item.get("epa"), dict) else {}
    trigger = next(
        (
            value
            for raw in (
                item.get("trigger"),
                item.get("price"),
                item.get("level"),
                item.get("projected_p5"),
            )
            if (value := float_or_none(raw)) is not None
        ),
        None,
    )
    target = next(
        (
            value
            for raw in (item.get("target"), epa.get("end_price"))
            if (value := float_or_none(raw)) is not None
        ),
        None,
    )
    price = next(
        (
            value
            for raw in (item.get("price"), item.get("projected_p5"))
            if (value := float_or_none(raw)) is not None
        ),
        None,
    )
    prepared = {
        **item,
        "score": score,
        "trigger": trigger,
        "target": target,
        "invalid": item.get("invalid"),
    }
    return candidate_from_trade_plan_item(
        prepared,
        name=name,
        score_floor=score_floor,
        stop_keys=("invalid",),
        details={
            "code": item.get("code"),
            "price": price,
            "pivots": item.get("pivots"),
        },
        finality=CandidateFinality.CONFIRMED,
    )


def signal_candidates_from_wolfe_indicator(
    indicator: dict[str, Any],
    *,
    score_floor: float,
    atr_value: float | None,
    features: dict[str, Any] | None,
) -> list[SignalCandidate]:
    del atr_value
    resolved_floor = max(0.0, float(score_floor))
    out: list[SignalCandidate] = []
    for item in indicator.get("events", []) or []:
        if not isinstance(item, dict) or item.get("code") != "WOLFE_CONFIRMED":
            continue
        candidate = _candidate_from_wolfe_item(
            item,
            "wolfe_confirmed",
            max(58.0, resolved_floor),
            features,
        )
        if candidate:
            out.append(candidate)
    return sorted(out, key=lambda candidate: candidate.score, reverse=True)[:2]


def _finalize_wolfe_structure_indicator(
    indicator: dict[str, Any],
    *,
    structure_bars: Sequence[Bar],
    structure_offset: int,
    profile_wolfe_pivot_len: int,
) -> dict[str, Any]:
    indicator = offset_indicator_indices(indicator, structure_offset)
    indicator.setdefault("settings", {})["lookback_bars"] = len(structure_bars)
    indicator["settings"]["lookback_offset"] = structure_offset
    indicator["settings"]["profile_pivot_len"] = profile_wolfe_pivot_len
    return indicator


def build_wolfe_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = wolfe_structure
    structure_bars = ctx.structure_bars or ()
    params = ctx.params("wolfe_structure")
    if not isinstance(params, WolfeStructureParams):
        raise TypeError("wolfe_structure runtime params must be WolfeStructureParams")
    profile_pivot_len = int(ctx.instrument_profile.wolfe_pivot_len)
    effective_pivot_len = profile_pivot_len if params.pivot_len <= 0 else params.pivot_len
    return IndicatorExecutionSpec(
        id="wolfe_structure",
        input_bars=structure_bars,
        analysis_bar=structure_bars[-1] if structure_bars else None,
        mode="confirmed",
        calculate=lambda: calculate(
            structure_bars,
            pivot_len=effective_pivot_len,
            line_width=ctx.line_width,
            show_geometry=ctx.show_geometry,
            confirmed_limit=params.confirmed_limit,
            show_candidates=params.show_candidates,
            show_price_target=params.show_price_target,
            line_opacity=params.line_opacity,
            atr_value=ctx.atr_value,
            pivot_context=ctx.pivot_context,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        postprocess=lambda indicator: _finalize_wolfe_structure_indicator(
            indicator,
            structure_bars=structure_bars,
            structure_offset=ctx.structure_offset,
            profile_wolfe_pivot_len=profile_pivot_len,
        ),
    )


def _line_price(x1: int, y1: float, x2: int, y2: float, x: int) -> float:
    span = max(float(x2 - x1), 1.0)
    return y1 + (y2 - y1) * (float(x - x1) / span)


def _pivot_copy(pivot: dict[str, Any], label: int | None = None) -> dict[str, Any]:
    copied = dict(pivot)
    copied["price"] = _round(float(copied["price"]))
    if label is not None:
        copied["label"] = label
    return copied


def _wolfe_types(pivots: Sequence[dict[str, Any]]) -> str | None:
    types = [pivot["type"] for pivot in pivots]
    if types == [-1, 1, -1, 1, -1]:
        return Direction.LONG.value
    if types == [1, -1, 1, -1, 1]:
        return Direction.SHORT.value
    return None


def _wolfe_score(
    pivots: Sequence[dict[str, Any]],
    atr: float,
    p5_price: float | None = None,
    p5_index: int | None = None,
) -> tuple[bool, float, dict[str, Any]]:
    if len(pivots) != 5:
        return False, 0.0, {}
    p1, p2, p3, p4, p5 = pivots
    direction = _wolfe_types(pivots)
    if direction is None:
        return False, 0.0, {}
    p5_price = float(p5["price"] if p5_price is None else p5_price)
    p5_index = int(p5["index"] if p5_index is None else p5_index)
    tol = atr * 0.55
    y13_at_5 = _line_price(p1["index"], p1["price"], p3["index"], p3["price"], p5_index)
    converging = abs(p1["price"] - p2["price"]) > abs(p3["price"] - p4["price"]) + atr * 0.04
    point5_near = abs(p5_price - y13_at_5) <= atr * 1.65
    if direction == Direction.LONG.value:
        p3_extends = p3["price"] <= p1["price"] + tol
        p4_valid = p4["price"] <= p2["price"] + tol
        p5_extends = p5_price <= p3["price"] + tol
        leg_ok = p2["price"] - p3["price"] > atr * 0.35
        overshoot = p5_price < p3["price"]
    else:
        p3_extends = p3["price"] >= p1["price"] - tol
        p4_valid = p4["price"] >= p2["price"] - tol
        p5_extends = p5_price >= p3["price"] - tol
        leg_ok = p3["price"] - p2["price"] > atr * 0.35
        overshoot = p5_price > p3["price"]
    score = 28.0
    score += 10.0 if converging else 0.0
    score += 12.0 if p3_extends else 3.0
    score += 12.0 if p4_valid else -18.0
    score += 22.0 if point5_near else 0.0
    score += 8.0 if p5_extends else 0.0
    score += 6.0 if overshoot else 0.0
    score += 6.0 if leg_ok else -12.0
    score = _clamp(score, 0.0, 99.0)
    valid = score >= 58.0 and point5_near and p3_extends and p4_valid and leg_ok
    details = {
        "direction": direction,
        "score": round(score, 2),
        "converging": converging,
        "point5_near": point5_near,
        "p3_extends": p3_extends,
        "p4_valid": p4_valid,
        "p5_extends": p5_extends,
        "leg_ok": leg_ok,
        "y13_at_5": _round(y13_at_5),
    }
    return valid, score, details


def _wolfe_epa(
    p1: dict[str, Any], p4: dict[str, Any], bars: Sequence[Bar], extend_bars: int = 80
) -> dict[str, Any]:
    projection = overlays.projected_end(bars[-1], extend_bars)
    end_bar_offset = int(projection["end_bar_offset"])
    end_index = len(bars) - 1 + end_bar_offset
    end_price = _line_price(p1["index"], p1["price"], p4["index"], p4["price"], end_index)
    return {
        "end_index": end_index,
        **projection,
        "end_price": _round(end_price),
    }


def _make_wolfe_event(
    code: str,
    direction: str,
    score: float,
    pivots: Sequence[dict[str, Any]],
    bars: Sequence[Bar],
    price: float,
    message: str,
    details: dict[str, Any],
    *,
    candidate_finality: CandidateFinality,
    line_width: float = 1.0,
    show_geometry: bool = True,
) -> dict[str, Any]:
    p1, p2, p3, p4, p5 = pivots
    p5_index = max(0, min(int(p5.get("index", len(bars) - 1)), len(bars) - 1))
    p5_bar = bars[p5_index]
    latest = bars[-1]
    event_bars = bars[: p5_index + 1] or bars
    epa = _wolfe_epa(p1, p4, event_bars)
    trigger = p5_bar.low if direction == Direction.SHORT.value else p5_bar.high
    pad = _atr_value(bars) * 0.35
    structural_invalid = price + pad if direction == Direction.SHORT.value else price - pad
    direction_enum = Direction.SHORT if direction == Direction.SHORT.value else Direction.LONG
    invalid = anchored_invalidation_stop(direction_enum, structural_invalid, latest, atr_pad=pad)
    target = epa["end_price"]
    plan = normalize_trade_plan(
        direction_enum,
        trigger,
        invalid,
        target,
        bar=latest,
        atr_pad=pad,
        use_market_entry=False,
    )
    if plan.get("coherent"):
        trigger = float(plan["entry"])
        invalid = float(plan["stop"])
        target = float(plan["target"])
    label = "P5 sell zone" if direction == Direction.SHORT.value else "P5 buy zone"
    action = "ARM" if code == "WOLFE_CONFIRMED" else score_action(float(score))
    if not plan.get("coherent") and action == "GO":
        action = "WATCH"
    signal = SignalState(
        source="wolfe_structure",
        action=ActionPhase(action),
        raw_action=action,
        direction=direction_enum,
        score=score,
        confirmed=code == "WOLFE_CONFIRMED",
        source_tf=p5_bar.timeframe,
        trigger=trigger,
        stop=invalid,
        target=target,
        invalidation=invalid,
        reason=code.lower(),
        code=code,
    )
    event = {
        "id": f"wolfe-{code}-{p5['ts']}-{direction}",
        "ts": p5["ts"],
        "symbol": p5_bar.symbol,
        "timeframe": p5_bar.timeframe,
        "trigger_type": "indicator_event",
        "source": "wolfe_structure",
        "event_type": "formation",
        "code": code,
        "candidate_finality": candidate_finality.value,
        "reason_code": code.lower(),
        "label": label,
        "action": action,
        "direction": direction,
        "score": round(score, 2),
        "price": _round(price),
        "level": _round(price),
        "trigger": _round(trigger),
        "invalid": _round(invalid),
        "target": target,
        "trigger_event": {"code": code.lower()},
        **indicator_fact_payload(
            scenario=code.lower(),
            trigger_event={"code": code.lower()},
            context={"code": "wolfe_structure_context", "content": message},
            quality={"code": "score"},
            metrics={
                "p4_valid": bool(details.get("p4_valid")),
                "score": _metric_number(score, digits=0),
                "p5": _metric_number(price),
                "invalid": _metric_number(invalid),
                "epa": _metric_number(target),
                "trigger": _metric_number(trigger),
            },
        ),
        "signal": signal.as_dict(),
        "pivots": [_pivot_copy(pivot, index + 1) for index, pivot in enumerate(pivots)]
        if show_geometry
        else [],
        "epa": epa,
        "details": details,
    }
    return event


def _confirmed_wolfe_finality(
    *,
    bar_count: int,
    p5_index: int,
    pivot_len: int,
) -> CandidateFinality:
    confirmation_age = bar_count - 1 - p5_index
    return (
        CandidateFinality.CONFIRMED if confirmation_age == pivot_len else CandidateFinality.RETAINED
    )


def _wolfe_ts_value(item: dict[str, Any]) -> str:
    return str(item.get("ts") or "")


def _latest_wolfe_item(
    paths: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    ranked: list[tuple[str, int, float, int, dict[str, Any]]] = []
    for priority, items in ((1, paths), (2, candidates), (3, events)):
        for index, item in enumerate(items):
            score = float_or_none(item.get("score"))
            if score is not None:
                ranked.append((_wolfe_ts_value(item), priority, score, index, item))
    if not ranked:
        return None
    return max(ranked, key=lambda row: (row[0], row[1], row[2], row[3]))[4]


def _wolfe_pivot_ts(pivot: dict[str, Any]) -> str:
    return str(pivot.get("ts") or "")


def _wolfe_pivot_price(pivot: dict[str, Any]) -> float | None:
    try:
        return float(pivot.get("price"))
    except TypeError, ValueError:
        return None


def _wolfe_control_fields(primary: str, *extra: str) -> dict[str, Any]:
    keys = [key for key in (primary, *extra) if key]
    return {"control_key": primary, "control_keys": keys}


def _wolfe_geometry_line(
    p_start: dict[str, Any],
    p_end: dict[str, Any],
    *,
    geometry_segment: str,
    role: str,
    direction: str,
    line_opacity: float,
    control_key: str,
    fact_fields: dict[str, Any],
    control_keys: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    y1 = _wolfe_pivot_price(p_start)
    y2 = _wolfe_pivot_price(p_end)
    start_ts = _wolfe_pivot_ts(p_start)
    end_ts = _wolfe_pivot_ts(p_end)
    has_projected_end = "end_anchor_ts" in p_end or "end_bar_offset" in p_end
    end_anchor_ts = str(p_end.get("end_anchor_ts") or "")
    end_bar_offset = p_end.get("end_bar_offset")
    projected_end = (
        bool(end_anchor_ts)
        and isinstance(end_bar_offset, int)
        and not isinstance(end_bar_offset, bool)
        and end_bar_offset >= 1
    )
    if (
        y1 is None
        or y2 is None
        or not start_ts
        or (not end_ts and not projected_end)
        or (has_projected_end and (not projected_end or bool(end_ts)))
    ):
        return None
    segment_labels = {
        "one_three": "W 1-3",
        "two_four": "W 2-4",
        "four_five": "W5",
        "epa": "W EPA",
    }
    target = role == "wolfe_target"
    projected = role in {"wolfe_path", "wolfe_projected_pivot"}
    minor = geometry_segment in {"three_four", "three_five", "four_five"}
    directional_tone = (
        "positive"
        if direction == Direction.LONG.value
        else "negative"
        if direction == Direction.SHORT.value
        else "neutral"
    )
    payload: dict[str, Any] = {
        "type": "line",
        "start_ts": start_ts,
        "y1": _round(y1),
        "y2": _round(y2),
        "role": role,
        "direction": direction,
        "geometry_segment": geometry_segment,
        "label": segment_labels.get(geometry_segment, ""),
        "label_side": ("above" if target or direction == Direction.SHORT.value else "below"),
        "label_position": "line_right" if geometry_segment == "four_five" else "center",
        "label_font_size": 8,
        "tone": "warning" if target else "info" if projected else directional_tone,
        "style": "dotted" if target or role == "wolfe_projected_pivot" else "dashed",
        "width": 0.85 if target else 0.8 if minor else 1.0,
        "opacity": max(0.15, min(float(line_opacity), 1.0)),
        "arrow_head": target,
        "arrow_size": 8,
        **_wolfe_control_fields(control_key, *(control_keys or ())),
        **fact_fields,
    }
    if projected_end:
        payload["end_anchor_ts"] = end_anchor_ts
        payload["end_bar_offset"] = end_bar_offset
    else:
        payload["end_ts"] = end_ts
    return payload


def _wolfe_pivot_marker(
    pivot: dict[str, Any],
    *,
    pivot_number: int,
    structure_phase: str,
    direction: str,
    control_key: str,
    fact_fields: dict[str, Any],
    control_keys: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    price = _wolfe_pivot_price(pivot)
    ts = _wolfe_pivot_ts(pivot)
    if price is None or not ts:
        return None
    return {
        "type": "marker",
        "ts": ts,
        "price": _round(price),
        "role": "structure_pivot",
        "pivot_number": pivot_number,
        "structure_phase": structure_phase,
        "direction": direction,
        "tone": (
            "info"
            if structure_phase == "path"
            else "positive"
            if direction == Direction.LONG.value
            else "negative"
            if direction == Direction.SHORT.value
            else "neutral"
        ),
        **_wolfe_control_fields(control_key, *(control_keys or ())),
        **fact_fields,
    }


def _wolfe_projected_pivot(path: dict[str, Any]) -> dict[str, Any] | None:
    end_anchor_ts = str(path.get("end_anchor_ts") or "")
    end_bar_offset = path.get("end_bar_offset")
    price = _wolfe_pivot_price({"price": path.get("projected_p5")})
    if (
        price is None
        or not end_anchor_ts
        or not isinstance(end_bar_offset, int)
        or isinstance(end_bar_offset, bool)
        or end_bar_offset < 1
    ):
        return None
    return {
        "end_anchor_ts": end_anchor_ts,
        "end_bar_offset": end_bar_offset,
        "price": price,
        "label": 5,
    }


def _wolfe_item_fact_fields(item: dict[str, Any]) -> dict[str, Any]:
    return copy_indicator_facts(item)


def _wolfe_path_overlays(
    path: dict[str, Any],
    *,
    show_price_target: bool,
    line_opacity: float,
) -> list[dict[str, Any]]:
    p1 = path.get("p1")
    p2 = path.get("p2")
    p3 = path.get("p3")
    p4 = path.get("p4")
    p5 = _wolfe_projected_pivot(path)
    if not all(isinstance(p, dict) for p in (p1, p2, p3, p4)) or p5 is None:
        return []
    direction = str(path.get("direction") or "")
    fields = _wolfe_item_fact_fields(path)
    overlays_out: list[dict[str, Any]] = []
    for line in (
        _wolfe_geometry_line(
            p1,
            p5,
            geometry_segment="one_three",
            role="wolfe_path",
            direction=direction,
            line_opacity=line_opacity,
            control_key="heuristicPath",
            fact_fields=fields,
        ),
        _wolfe_geometry_line(
            p2,
            p4,
            geometry_segment="two_four",
            role="wolfe_path",
            direction=direction,
            line_opacity=line_opacity,
            control_key="heuristicPath",
            fact_fields=fields,
        ),
        _wolfe_geometry_line(
            p3,
            p4,
            geometry_segment="three_four",
            role="wolfe_path",
            direction=direction,
            line_opacity=line_opacity,
            control_key="heuristicPath",
            fact_fields=fields,
        ),
        _wolfe_geometry_line(
            p4,
            p5,
            geometry_segment="four_five",
            role="wolfe_projected_pivot",
            direction=direction,
            line_opacity=line_opacity,
            control_key="heuristicPath",
            fact_fields=fields,
            control_keys=("pivots",),
        ),
    ):
        if line:
            overlays_out.append(line)
    if show_price_target:
        target = _wolfe_geometry_line(
            p1,
            p4,
            geometry_segment="epa",
            role="wolfe_target",
            direction=direction,
            line_opacity=line_opacity,
            control_key="priceTarget",
            fact_fields=fields,
            control_keys=("heuristicPath",),
        )
        if target:
            overlays_out.append(target)
    for index, pivot in enumerate((p1, p2, p3, p4), start=1):
        marker = _wolfe_pivot_marker(
            pivot,
            pivot_number=index,
            structure_phase="path",
            direction=direction,
            control_key="pivots",
            fact_fields=fields,
            control_keys=("heuristicPath",),
        )
        if marker:
            overlays_out.append(marker)
    return overlays_out


def _wolfe_event_overlays(
    item: dict[str, Any],
    *,
    show_price_target: bool,
    line_opacity: float,
) -> list[dict[str, Any]]:
    pivots = item.get("pivots")
    if not isinstance(pivots, Sequence) or len(pivots) < 5:
        return []
    pivot_items = [pivot for pivot in pivots[:5] if isinstance(pivot, dict)]
    if len(pivot_items) < 5:
        return []
    p1, p2, p3, p4, p5 = pivot_items
    direction = str(item.get("direction") or "")
    control_key = "candidates" if item.get("code") == "WOLFE_CANDIDATE" else "paths"
    fields = _wolfe_item_fact_fields(item)
    overlays_out: list[dict[str, Any]] = []
    for line in (
        _wolfe_geometry_line(
            p1,
            p5,
            geometry_segment="one_three",
            role="wolfe_geometry",
            direction=direction,
            line_opacity=line_opacity,
            control_key=control_key,
            fact_fields=fields,
        ),
        _wolfe_geometry_line(
            p2,
            p4,
            geometry_segment="two_four",
            role="wolfe_geometry",
            direction=direction,
            line_opacity=line_opacity,
            control_key=control_key,
            fact_fields=fields,
        ),
        _wolfe_geometry_line(
            p3,
            p5,
            geometry_segment="three_five",
            role="wolfe_geometry",
            direction=direction,
            line_opacity=line_opacity,
            control_key=control_key,
            fact_fields=fields,
        ),
        _wolfe_geometry_line(
            p4,
            p5,
            geometry_segment="four_five",
            role="wolfe_geometry",
            direction=direction,
            line_opacity=line_opacity,
            control_key=control_key,
            fact_fields=fields,
        ),
    ):
        if line:
            overlays_out.append(line)
    for index, pivot in enumerate((p1, p2, p3, p4, p5), start=1):
        marker = _wolfe_pivot_marker(
            pivot,
            pivot_number=index,
            structure_phase="event",
            direction=direction,
            control_key="pivots",
            fact_fields=fields,
            control_keys=(control_key,),
        )
        if marker:
            overlays_out.append(marker)
    epa = item.get("epa")
    if show_price_target and isinstance(epa, dict):
        target = _wolfe_geometry_line(
            p1,
            {
                "end_anchor_ts": epa.get("end_anchor_ts"),
                "end_bar_offset": epa.get("end_bar_offset"),
                "price": epa.get("end_price"),
            },
            geometry_segment="epa",
            role="wolfe_target",
            direction=direction,
            line_opacity=line_opacity,
            control_key="priceTarget",
            fact_fields=fields,
            control_keys=(control_key,),
        )
        if target:
            overlays_out.append(target)
    return overlays_out


def _wolfe_structure_overlays(
    paths: Sequence[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    events: Sequence[dict[str, Any]],
    *,
    show_geometry: bool,
    show_price_target: bool,
    line_opacity: float,
) -> list[dict[str, Any]]:
    if not show_geometry:
        return []
    overlays_out: list[dict[str, Any]] = []
    for path in paths:
        overlays_out.extend(
            _wolfe_path_overlays(
                path,
                show_price_target=show_price_target,
                line_opacity=line_opacity,
            )
        )
    for item in [*candidates, *events]:
        overlays_out.extend(
            _wolfe_event_overlays(
                item,
                show_price_target=show_price_target,
                line_opacity=line_opacity,
            )
        )
    return overlays_out


def wolfe_structure(
    bars: Sequence[Bar],
    pivot_len: int = 3,
    line_width: float = 1.0,
    show_geometry: bool = True,
    confirmed_limit: int = 2,
    show_candidates: bool = True,
    show_price_target: bool = True,
    line_opacity: float = 0.75,
    atr_value: float | None = None,
    pivot_context: PivotContext | None = None,
) -> dict[str, Any]:
    pivot_len = max(2, min(int(pivot_len), 12))
    confirmed_limit = max(1, min(int(confirmed_limit), 3))
    line_opacity = max(0.15, min(float(line_opacity), 1.0))
    pivots = (
        pivot_context.alternating(pivot_len, pivot_len)
        if pivot_context is not None
        else _pivot_points(bars, left=pivot_len, right=pivot_len)
    )
    events: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    paths: list[dict[str, Any]] = []
    if len(bars) < 20:
        return {
            "version": "1.1-python",
            "settings": {
                "pivot_len": pivot_len,
                "line_width": line_width,
                "show_geometry": show_geometry,
                "confirmed_limit": confirmed_limit,
                "show_candidates": bool(show_candidates),
                "show_price_target": bool(show_price_target),
                "line_opacity": line_opacity,
            },
            "pivots": pivots,
            "paths": [],
            "candidates": [],
            "events": [],
            "overlays": [],
            "latest": None,
            "availability": {
                "state": "blocked",
                "reason_code": "wolfe_structure_warmup",
                "required_bars": 20,
                "available_bars": len(bars),
            },
        }
    atr = max(float(atr_value), 1e-9) if atr_value is not None else _atr_value(bars)
    if len(pivots) >= 3:
        p1, p2, p3 = pivots[-3:]
        bull_path = (
            p1["type"] == -1 and p2["type"] == 1 and p3["type"] == -1 and p3["price"] <= p1["price"]
        )
        bear_path = (
            p1["type"] == 1 and p2["type"] == -1 and p3["type"] == 1 and p3["price"] >= p1["price"]
        )
        if bull_path or bear_path:
            direction = Direction.LONG if bull_path else Direction.SHORT
            leg = abs(p2["price"] - p3["price"])
            span = max(int(round((p3["index"] - p1["index"]) * 0.70)), 6)
            projected_index = min(len(bars) - 1 + span, len(bars) - 1 + 120)
            projected_price = _line_price(
                p1["index"], p1["price"], p3["index"], p3["price"], projected_index
            )
            projected_bar_offset = projected_index - (len(bars) - 1)
            p4_price = (
                max(bars[-1].high, p3["price"] + leg * 0.618)
                if bull_path
                else min(bars[-1].low, p3["price"] - leg * 0.618)
            )
            p4_index = len(bars) - 1
            p4 = {
                "index": p4_index,
                "ts": bars[-1].ts.isoformat(),
                "price": _round(p4_price),
                "type": 1 if bull_path else -1,
                "label": 4,
            }
            score = 45.0
            score += 12.0 if abs(p3["price"] - p1["price"]) >= atr * 0.20 else 0.0
            score += 8.0 if abs(p2["price"] - p3["price"]) >= atr * 0.80 else 0.0
            score += (
                6.0
                if (bull_path and bars[-1].close > p3["price"])
                or (bear_path and bars[-1].close < p3["price"])
                else 0.0
            )
            label = "P5 buy path" if bull_path else "P5 sell path"
            path_signal = SignalState(
                source="wolfe_structure",
                action=ActionPhase.CANDIDATE,
                raw_action="CANDIDATE",
                direction=direction,
                score=score,
                confirmed=False,
                source_tf=bars[-1].timeframe,
                trigger=projected_price,
                reason="wolfe_projected_path",
                code="WOLFE_PATH",
            )
            paths.append(
                {
                    "ts": bars[-1].ts.isoformat(),
                    "direction": direction.value,
                    "action": "CANDIDATE",
                    "candidate_finality": CandidateFinality.PROVISIONAL.value,
                    "score": round(score, 2),
                    "label": label,
                    "p1": _pivot_copy(p1, 1),
                    "p2": _pivot_copy(p2, 2),
                    "p3": _pivot_copy(p3, 3),
                    "p4": p4,
                    "projected_index": projected_index,
                    **overlays.projected_end(bars[-1], projected_bar_offset),
                    "projected_p5": _round(projected_price),
                    "reason_code": "wolfe_projected_path",
                    "trigger_event": {"code": "projected_p5_reaction"},
                    "signal": path_signal.as_dict(),
                    **indicator_fact_payload(
                        scenario="wolfe_projected_path",
                        trigger_event={"code": "projected_p5_reaction"},
                        context={"code": "projected_p5_is_not_confirmation"},
                        quality={"code": "score"},
                        metrics={
                            "direction": str(direction.value).upper(),
                            "score": _metric_number(score, digits=0),
                            "projected_p4": _metric_number(p4_price),
                            "projected_p5": _metric_number(projected_price),
                        },
                    ),
                }
            )
    if len(pivots) >= 4:
        p1, p2, p3, p4 = pivots[-4:]
        live = bars[-1]
        live_bear = [p["type"] for p in (p1, p2, p3, p4)] == [1, -1, 1, -1]
        live_bull = [p["type"] for p in (p1, p2, p3, p4)] == [-1, 1, -1, 1]
        if live_bear or live_bull:
            p5_price = live.high if live_bear else live.low
            p5_type = 1 if live_bear else -1
            p5 = {
                "index": len(bars) - 1,
                "ts": live.ts.isoformat(),
                "price": p5_price,
                "type": p5_type,
            }
            valid, score, details = _wolfe_score([p1, p2, p3, p4, p5], atr, p5_price, len(bars) - 1)
            near = bool(details.get("point5_near"))
            reject = (live_bear and live.close < float(details.get("y13_at_5") or live.high)) or (
                live_bull and live.close > float(details.get("y13_at_5") or live.low)
            )
            if (valid or (near and score >= 48.0)) and reject:
                direction = Direction.SHORT.value if live_bear else Direction.LONG.value
                candidates.append(
                    _make_wolfe_event(
                        "WOLFE_CANDIDATE",
                        direction,
                        score,
                        [p1, p2, p3, p4, p5],
                        bars,
                        p5_price,
                        "Provisional P5 on current bar. It can repaint until a pivot confirms.",
                        details,
                        candidate_finality=CandidateFinality.PROVISIONAL,
                        line_width=line_width,
                        show_geometry=show_geometry,
                    )
                )
    if len(pivots) >= 5:
        start = max(0, len(pivots) - 10)
        for offset in range(start, len(pivots) - 4):
            pset = pivots[offset : offset + 5]
            ordered = all(pset[index]["index"] < pset[index + 1]["index"] for index in range(4))
            if not ordered:
                continue
            valid, score, details = _wolfe_score(pset, atr)
            if not valid:
                continue
            direction = str(details["direction"])
            events.append(
                _make_wolfe_event(
                    "WOLFE_CONFIRMED",
                    direction,
                    score,
                    pset,
                    bars,
                    pset[-1]["price"],
                    "Confirmed Wolfe 1-2-3-4-5 geometry. EPA is the 1-4 line.",
                    details,
                    candidate_finality=_confirmed_wolfe_finality(
                        bar_count=len(bars),
                        p5_index=int(pset[-1]["index"]),
                        pivot_len=pivot_len,
                    ),
                    line_width=line_width,
                    show_geometry=show_geometry,
                )
            )
    display_paths = paths[-3:]
    display_candidates = candidates[-3:]
    display_events = sorted(
        events[-8:],
        key=lambda item: (_wolfe_ts_value(item), float(item["score"])),
        reverse=True,
    )[:confirmed_limit]
    best_item = _latest_wolfe_item(display_paths, display_candidates, display_events)
    overlay_items = _wolfe_structure_overlays(
        display_paths,
        display_candidates,
        display_events,
        show_geometry=show_geometry,
        show_price_target=show_price_target,
        line_opacity=line_opacity,
    )
    return {
        "version": "1.1-python",
        "settings": {
            "pivot_len": pivot_len,
            "display_limit": confirmed_limit,
            "confirmed_limit": confirmed_limit,
            "show_candidates": bool(show_candidates),
            "show_price_target": bool(show_price_target),
            "line_width": line_width,
            "show_geometry": show_geometry,
            "line_opacity": line_opacity,
        },
        "pivots": pivots[-18:],
        "paths": display_paths,
        "candidates": display_candidates,
        "events": display_events,
        "overlays": overlay_items,
        "latest": best_item,
    }
