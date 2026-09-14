from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import (
    ActionPhase,
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
    domain_wire_value,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.defaults import (
    DEFAULT_INDICATOR_SETTINGS,
    IndicatorDefaults,
    score_action,
)
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number as _metric_number,
)
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
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
)
from aef_terminal.runtime import overlays, pine
from aef_terminal.runtime.math_utils import (
    clamp as _clamp,
    float_or_none,
    round_optional as _round,
    safe_div as _safe_div,
)
from aef_terminal.runtime.pivots import PivotContext
from aef_terminal.runtime.signal_state import SignalState
from aef_terminal.runtime.timeframes import expiry_after_bars
from aef_terminal.signals.candidates import candidate_from_trade_plan_item
from aef_terminal.signals.direction import direction_from_any


_ROW_FIELDS = (
    "action",
    "age_bars",
    "armed_expires_at",
    "armed_expires_bars",
    "blocked",
    "blocked_reason",
    "bottom_score",
    "chart_bottom_score",
    "chart_top_score",
    "code",
    "candidate_finality",
    "confirmed",
    "context_bottom_score",
    "context_lines",
    "context_top_score",
    "details",
    "direction",
    "event_type",
    "fast_bottom_score",
    "fast_top_score",
    "fib_zone",
    "id",
    "in_fib_zone",
    "invalidation",
    "kind",
    "level",
    "message",
    "mode",
    "mtf_bottom_score",
    "mtf_context",
    "mtf_top_score",
    "plan_coherent",
    "plan_complete",
    "price",
    "quality_text",
    "raw_action",
    "reason",
    "reward",
    "risk",
    "rr",
    "run_bear",
    "run_bull",
    "runaway_veto",
    "scenario",
    "score",
    "signal",
    "signal_actionable",
    "source",
    "source_tf",
    "state",
    "stop",
    "symbol",
    "target",
    "timeframe",
    "top_score",
    "trade_plan",
    "trigger",
    "trigger_label",
    "trigger_state",
    "trigger_type",
    "ts",
    "expires_in_bars",
    "retained",
    "weighted_bottom_score",
    "weighted_top_score",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


INDICATOR_MODULES = (
    IndicatorModule(
        IndicatorSpec(
            "w5_structure",
            "W5 Structure",
            "wave 5 exhaustion and continuation veto context",
            {"version": "1.5-python-confirmed-context", "events": [], "latest": None},
            "w5Structure",
            "aef_terminal.indicators.modules.w5_structure:w5_structure",
            group="structure",
            pipeline_stage="structure",
            pipeline_order=110,
            candidate_promoter="w5",
            candidate_score_floor_param="min_score",
            candidate_score_floor_min=40.0,
            custom_renderer=True,
            renderer_kind="custom",
            renderer_ref="structure_indicators",
            renderer_primitives=("line", "label", "structure_paths"),
            renderer_placements=("price",),
            custom_renderer_requirements=("price_overlay_provider", "structure_snapshot"),
            score_family="structure",
            empirical_power=0.95,
            usefulness=0.90,
            module_type="context",
            state_key="w5Structure",
            calc_key="w5StructureCalcEnabled",
            visible_key="w5StructureVisible",
            chart_control_id="w5-structure-toggle",
            process_control_id="w5-structure-process",
            api_enabled_key="w5_enabled",
            manager_order=110,
            runtime_order=100,
            default_calc=False,
            overlay_layer="signals",
            confirmed_bar_context=(
                ConfirmedBarContextRequest(
                    timeframe="1m",
                    history_bars=256,
                    role="lower_timeframe_confirmation",
                ),
            ),
            runtime_payload_contract={
                "latest": _ROW_FIELDS,
                "events": _ROW_FIELDS,
            },
            runtime_payload_schema_refs=("overlay_contract",),
            controls=(
                _control(
                    "mode",
                    "Mode",
                    "select",
                    "scalp",
                    "w5StructureMode",
                    element_id="w5-structure-mode",
                    api_key="w5_mode",
                    param_key="mode",
                    options=("scalp", "swing"),
                    action="apply",
                ),
                _control(
                    "minScore",
                    "Min",
                    "number",
                    55,
                    "w5StructureMinScore",
                    element_id="w5-structure-min-score",
                    api_key="w5_min_score",
                    param_key="min_score",
                    minimum=40,
                    maximum=90,
                    step=1,
                    action="load_apply",
                ),
            ),
        ),
        adapter_ref="aef_terminal.indicators.modules.w5_structure:build_w5_execution_spec",
        params_builder_ref="aef_terminal.indicators.modules.w5_structure:build_params",
        candidate_promoter_ref="aef_terminal.indicators.modules.w5_structure:signal_candidates_from_w5_indicator",
    ),
)


@dataclass(frozen=True)
class W5StructureParams:
    mode: str = "scalp"
    min_score: float = 55.0
    defaults: IndicatorDefaults = DEFAULT_INDICATOR_SETTINGS


def build_params(raw: dict[str, Any], defaults: IndicatorDefaults) -> W5StructureParams:
    return W5StructureParams(
        mode=_w5_mode(raw.get("mode")),
        min_score=max(40.0, min(float(raw.get("min_score", W5StructureParams.min_score)), 90.0)),
        defaults=defaults,
    )


def build_w5_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = w5_structure
    structure_bars = ctx.structure_bars or ()
    params = ctx.params("w5_structure")
    if not isinstance(params, W5StructureParams):
        raise TypeError("w5_structure runtime params must be W5StructureParams")
    return IndicatorExecutionSpec(
        id="w5_structure",
        input_bars=structure_bars,
        analysis_bar=structure_bars[-1] if structure_bars else None,
        mode="confirmed",
        calculate=lambda: calculate(
            detect_w5_exhaustion(structure_bars, ctx.features),
            structure_bars,
            mtf_context=ctx.confirmed_bar_context,
            mtf_quality=ctx.confirmed_bar_context_quality,
            line_width=ctx.line_width,
            show_geometry=ctx.show_geometry,
            mode=params.mode,
            defaults=params.defaults,
            feature_context=ctx.feature_context,
            pivot_context=ctx.pivot_context,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
    )


def detect_w5_exhaustion(
    bars: Sequence[Bar],
    features: dict[str, Any],
) -> list[SignalCandidate]:
    if len(bars) < 10:
        return []

    bar = bars[-1]
    close_pos = float_or_none(features.get("close_pos"))
    close_pos = 0.5 if close_pos is None else close_pos
    upper_share = float_or_none(features.get("upper_share"))
    upper_share = 0.0 if upper_share is None else upper_share
    lower_share = float_or_none(features.get("lower_share"))
    lower_share = 0.0 if lower_share is None else lower_share
    rvol_adaptive = float_or_none(features.get("rvol_adaptive"))
    rvol_adaptive = 1.0 if rvol_adaptive is None else rvol_adaptive
    rejection_score = float_or_none(features.get("rejection_score"))
    rejection_score = 0.0 if rejection_score is None else rejection_score
    if rvol_adaptive < 1.1:
        return []

    base_score = 60.0 + (rejection_score / 100.0) * 15.0 + (rvol_adaptive - 1.0) * 12.0
    final_score = pine.clamp(base_score, 0.0, 99.0)
    details = {
        "rvol_adaptive": round(rvol_adaptive, 2),
        "rejection_score": round(rejection_score, 1),
        "is_climactic": rvol_adaptive > 1.8,
    }
    out: list[SignalCandidate] = []
    if upper_share >= 0.35 and close_pos <= 0.58:
        out.append(
            SignalCandidate(
                "w5_exhaustion",
                Direction.SHORT,
                final_score,
                bar.high,
                "w5_upper_rejection",
                details=details,
                kind=ScenarioKind.FADE,
                source="w5_structure",
                role="exhaustion",
                trigger_event=DomainFact("w5_upper_rejection"),
                reason_code="w5_upper_rejection",
                finality=CandidateFinality.CONFIRMED,
            )
        )
    if lower_share >= 0.35 and close_pos >= 0.42:
        out.append(
            SignalCandidate(
                "w5_exhaustion",
                Direction.LONG,
                final_score,
                bar.low,
                "w5_lower_rejection",
                details=details,
                kind=ScenarioKind.FADE,
                source="w5_structure",
                role="exhaustion",
                trigger_event=DomainFact("w5_lower_rejection"),
                reason_code="w5_lower_rejection",
                finality=CandidateFinality.CONFIRMED,
            )
        )
    return out


def _candidate_from_w5_item(
    item: dict[str, Any],
    score_floor: float,
) -> SignalCandidate | None:
    if item.get("candidate_finality") != CandidateFinality.CONFIRMED.value:
        return None
    direction = direction_from_any(item.get("direction"))
    if direction == Direction.FLAT:
        return None
    action = str(item.get("action") or "WAIT").upper()
    if action == "WAIT":
        return None
    score = float_or_none(item.get("score"))
    if score is None or score < score_floor:
        return None
    trigger = float_or_none(item.get("trigger"))
    price = float_or_none(item.get("price"))
    if price is None:
        price = float_or_none(item.get("level"))
    stop = float_or_none(item.get("stop"))
    if stop is None:
        stop = float_or_none(item.get("invalid"))
    if stop is None:
        stop = float_or_none(item.get("invalidation"))
    target = float_or_none(item.get("target"))
    details = {
        "code": item.get("code"),
        "action": action,
        "price": price,
        "top_score": float_or_none(item.get("top_score")),
        "bottom_score": float_or_none(item.get("bottom_score")),
        "weighted_top_score": float_or_none(item.get("weighted_top_score")),
        "weighted_bottom_score": float_or_none(item.get("weighted_bottom_score")),
        "mtf_top_score": float_or_none(item.get("mtf_top_score")),
        "mtf_bottom_score": float_or_none(item.get("mtf_bottom_score")),
        "mtf_context": item.get("mtf_context"),
        "fast_top_score": float_or_none(item.get("fast_top_score")),
        "fast_bottom_score": float_or_none(item.get("fast_bottom_score")),
        "context_top_score": float_or_none(item.get("context_top_score")),
        "context_bottom_score": float_or_none(item.get("context_bottom_score")),
        "run_bull": float_or_none(item.get("run_bull")),
        "run_bear": float_or_none(item.get("run_bear")),
        "trigger_state": item.get("trigger_state"),
        "fib_zone": item.get("fib_zone"),
        "runaway_veto": bool(item.get("runaway_veto")),
        "armed_expires_at": item.get("armed_expires_at"),
    }
    prepared = {
        **item,
        "trigger": trigger if trigger is not None else price,
        "stop": stop,
        "target": target,
    }
    return candidate_from_trade_plan_item(
        prepared,
        name="w5_exhaustion",
        score_floor=score_floor,
        details=details,
        kind=ScenarioKind.FADE,
        finality=CandidateFinality.CONFIRMED,
    )


def signal_candidates_from_w5_indicator(
    indicator: dict[str, Any],
    *,
    score_floor: float,
    atr_value: float | None,
    features: dict[str, Any] | None,
) -> list[SignalCandidate]:
    del atr_value, features
    resolved_floor = max(0.0, float(score_floor))
    out: list[SignalCandidate] = []
    latest = indicator.get("latest")
    if isinstance(latest, dict):
        candidate = _candidate_from_w5_item(latest, max(42.0, resolved_floor))
        if candidate:
            out.append(candidate)
    for item in indicator.get("events", []) or []:
        if not isinstance(item, dict):
            continue
        candidate = _candidate_from_w5_item(item, max(58.0, resolved_floor))
        if candidate:
            out.append(candidate)
    deduped: list[SignalCandidate] = []
    seen: set[tuple[str, str, float | None]] = set()
    for candidate in out:
        key = (candidate.name, candidate.direction.value, candidate.level)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped[-3:]


def _feature_series(feature_context: Any | None, name: str) -> list[float]:
    values = getattr(feature_context, name, None)
    return list(values) if isinstance(values, list) else []


def _sma(values: Sequence[float], index: int, length: int) -> float | None:
    if index < length - 1:
        return None
    window = values[index - length + 1 : index + 1]
    return sum(window) / max(len(window), 1)


def _ema(values: Sequence[float], length: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (length + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(out[-1] + alpha * (float(value) - out[-1]))
    return out


def _ewo_series(bars: Sequence[Bar]) -> list[float | None]:
    closes = [bar.close for bar in bars]
    atr = _atr_value(bars)
    out: list[float | None] = []
    for index in range(len(closes)):
        fast = _sma(closes, index, 5)
        slow = _sma(closes, index, 34)
        out.append(None if fast is None or slow is None else (fast - slow) / atr)
    return out


def _rel_volume(
    bars: Sequence[Bar], index: int, length: int = DEFAULT_INDICATOR_SETTINGS.rvol_len
) -> float:
    start = max(0, index - length + 1)
    window = [bar.volume for bar in bars[start : index + 1]]
    if not window:
        return 1.0
    baseline = max(sum(window) / len(window), 1.0)
    return window[-1] / baseline


def _bar_shape(bar: Bar) -> dict[str, float]:
    rng = max(bar.high - bar.low, 1e-9)
    body = abs(bar.close - bar.open)
    upper = bar.high - max(bar.open, bar.close)
    lower = min(bar.open, bar.close) - bar.low
    return {
        "range": rng,
        "body": body,
        "body_share": body / rng,
        "upper_share": upper / rng,
        "lower_share": lower / rng,
        "close_pos": (bar.close - bar.low) / rng,
        "upper": upper,
        "lower": lower,
    }


def _candidate_event(
    candidate: SignalCandidate, source: str, latest_bar: Bar | None = None
) -> dict[str, Any]:
    action = "WATCH"
    finality = (
        CandidateFinality.CONFIRMED
        if latest_bar is not None and latest_bar.closed
        else CandidateFinality.PROVISIONAL
    )
    signal = SignalState(
        source=source,
        action=ActionPhase.WATCH,
        raw_action=action,
        direction=candidate.direction,
        score=candidate.score,
        confirmed=latest_bar.closed if latest_bar else True,
        source_tf=latest_bar.timeframe if latest_bar else None,
        trigger=candidate.level,
        reason=candidate.reason,
        code=candidate.name.upper(),
    )
    return {
        "id": f"{source}-{candidate.direction.value}-{candidate.level}-{candidate.score}",
        "ts": latest_bar.ts.isoformat() if latest_bar else None,
        "symbol": latest_bar.symbol if latest_bar else None,
        "timeframe": latest_bar.timeframe if latest_bar else None,
        "trigger_type": "indicator_event",
        "source": source,
        "event_type": "formation",
        "code": candidate.name.upper(),
        "candidate_finality": finality.value,
        "direction": candidate.direction.value,
        "action": action,
        "score": round(candidate.score, 2),
        "price": _round(candidate.level),
        "level": _round(candidate.level),
        **indicator_fact_payload(
            scenario="wave_5_exhaustion",
            trigger_event=candidate.trigger_event,
            quality={"code": "score", "value": candidate.score},
            metrics={
                "score": _metric_number(candidate.score, digits=0),
                "level": _metric_number(candidate.level),
            },
        ),
        "signal": signal.as_dict(),
        "details": domain_wire_value(candidate.details, field_name="candidate.details"),
    }


def w5_structure(
    candidates: Sequence[SignalCandidate],
    bars: Sequence[Bar] = (),
    mtf_context: dict[str, Sequence[Bar]] | None = None,
    mtf_quality: dict[str, dict[str, Any]] | None = None,
    line_width: float = 1.0,
    show_geometry: bool = True,
    mode: str = "scalp",
    defaults: IndicatorDefaults | None = None,
    feature_context: Any | None = None,
    pivot_context: PivotContext | None = None,
) -> dict[str, Any]:
    defaults = defaults or DEFAULT_INDICATOR_SETTINGS
    latest_bar = bars[-1] if bars else None
    events = [
        _candidate_event(candidate, "w5_structure", latest_bar)
        for candidate in candidates
        if candidate.source == "w5" and candidate.role == "exhaustion"
    ]
    model_latest = _w5_latest_state(
        bars,
        mtf_context=mtf_context,
        mtf_quality=mtf_quality,
        mode=mode,
        defaults=defaults,
        feature_context=feature_context,
        pivot_context=pivot_context,
    )
    lifecycle_events = _w5_lifecycle_events(bars, defaults=defaults)

    drawable_events = list(events) + lifecycle_events
    if model_latest and model_latest["action"] != "WAIT":
        drawable_events.append(model_latest)

    # Кластеризация: не более одного сигнала на 8 баров
    drawable_events = sorted(
        drawable_events, key=lambda e: (str(e.get("ts") or ""), _w5_event_rank(e))
    )
    clustered: list[dict[str, Any]] = []
    last_idx = -999

    bar_indices = {b.ts.isoformat(): i for i, b in enumerate(bars)}

    for event in drawable_events:
        ts_str = str(event.get("ts") or "")
        idx = bar_indices.get(ts_str, -1)
        if idx == -1:
            continue

        if idx - last_idx >= 8:
            clustered.append(event)
            last_idx = idx
        else:
            if _w5_event_rank(event) > _w5_event_rank(clustered[-1]):
                clustered[-1] = event
                last_idx = idx

    latest_ts = latest_bar.ts.isoformat() if latest_bar is not None else ""
    current_events = [
        event
        for event in clustered
        if event.get("candidate_finality") == CandidateFinality.CONFIRMED.value
        and event.get("ts") == latest_ts
    ]
    latest = current_events[-1] if current_events else model_latest
    bar_by_ts = {bar.ts.isoformat(): bar for bar in bars}
    overlay_items: list[dict[str, Any]] = []

    for event in clustered[-6:]:
        event_bar = bar_by_ts.get(str(event.get("ts") or "")) or latest_bar
        overlay_items.extend(
            _w5_overlays(event_bar, event, line_width=line_width, show_geometry=show_geometry)
        )

    result = {
        "version": "1.5-python-confirmed-context",
        "settings": {"mode": _w5_mode(mode)},
        "events": clustered[-16:],
        "latest": latest,
        "overlays": overlay_items,
        "lifecycle": {
            "retention_bars": 24,
            "retained_events": len(lifecycle_events),
            "latest_age_bars": latest.get("age_bars") if isinstance(latest, dict) else None,
        },
    }
    if len(bars) < 40:
        result["availability"] = {
            "state": "blocked",
            "reason_code": "w5_structure_warmup",
            "required_bars": 40,
            "available_bars": len(bars),
        }
    return result


def _w5_action_rank(value: Any) -> int:
    action = str(value or "").upper()
    if action == "GO":
        return 5
    if action == "ARM":
        return 4
    if action in {"READY", "WATCH"}:
        return 3
    if action == "CANDIDATE":
        return 2
    if action == "WAIT":
        return 1
    return 0


def _w5_event_rank(event: dict[str, Any]) -> tuple[float, int, int]:
    score = float(event["score"])
    action_rank = _w5_action_rank(event.get("action"))
    has_plan = int(
        event.get("trigger") is not None
        or event.get("stop") is not None
        or event.get("target") is not None
    )
    return (score, action_rank, has_plan)


@dataclass(frozen=True)
class _W5LifecycleCalculationContext:
    atr_values: tuple[float, ...]
    ewo_atr_values: tuple[float, ...]
    ema_fast: tuple[float, ...]
    ema_slow: tuple[float, ...]
    ema_magnet: tuple[float, ...]
    ewo_raw: tuple[float | None, ...]
    rvol_values: tuple[float, ...]
    pivots_by_end: dict[int, tuple[dict[str, Any], ...]]


def _w5_lifecycle_calculation_context(
    bars: Sequence[Bar],
    *,
    defaults: IndicatorDefaults,
    first_end: int,
) -> _W5LifecycleCalculationContext:
    closes = [float(bar.close) for bar in bars]
    volumes = [bar.volume for bar in bars]
    atr_values = pine.atr_rma_series(bars, defaults.atr_len)
    ewo_atr_values = pine.atr_rma_series(
        bars,
        DEFAULT_INDICATOR_SETTINGS.atr_len,
    )
    ewo_raw: list[float | None] = []
    for index in range(len(closes)):
        fast = _sma(closes, index, 5)
        slow = _sma(closes, index, 34)
        ewo_raw.append(None if fast is None or slow is None else fast - slow)

    rvol_values: list[float] = []
    rolling_volume = 0.0
    rvol_length = max(int(defaults.rvol_len), 1)
    for index, volume in enumerate(volumes):
        rolling_volume += volume
        if index >= rvol_length:
            rolling_volume -= volumes[index - rvol_length]
        count = min(index + 1, rvol_length)
        baseline = max(rolling_volume / count, 1.0)
        rvol_values.append(volume / baseline)

    left = right = 3
    minimum_pivot_bars = left + right + 2
    pivots: list[dict[str, Any]] = []
    pivots_by_end: dict[int, tuple[dict[str, Any], ...]] = {}

    def admit_pivot(index: int) -> None:
        window = bars[index - left : index + right + 1]
        bar = bars[index]
        is_high = bar.high >= max(item.high for item in window)
        is_low = bar.low <= min(item.low for item in window)
        if not is_high and not is_low:
            return
        pivot_type = (
            1
            if is_high and not is_low
            else -1
            if is_low and not is_high
            else 1
            if bar.close < bar.open
            else -1
        )
        price = bar.high if pivot_type == 1 else bar.low
        pivot = {
            "index": index,
            "ts": bar.ts.isoformat(),
            "price": price,
            "type": pivot_type,
        }
        if pivots and pivots[-1]["type"] == pivot_type:
            previous = pivots[-1]
            replace_pivot = (
                price > previous["price"] if pivot_type == 1 else price < previous["price"]
            )
            if replace_pivot:
                pivots[-1] = pivot
            return
        pivots.append(pivot)

    for end in range(1, len(bars) + 1):
        if end == minimum_pivot_bars:
            for pivot_index in range(left, end - right):
                admit_pivot(pivot_index)
        elif end > minimum_pivot_bars:
            admit_pivot(end - right - 1)
        if end >= first_end:
            pivots_by_end[end] = tuple(dict(item) for item in pivots)

    return _W5LifecycleCalculationContext(
        atr_values=tuple(atr_values),
        ewo_atr_values=tuple(ewo_atr_values),
        ema_fast=tuple(_ema(closes, defaults.ema.fast)),
        ema_slow=tuple(_ema(closes, defaults.ema.slow)),
        ema_magnet=tuple(_ema(closes, defaults.ema.magnet)),
        ewo_raw=tuple(ewo_raw),
        rvol_values=tuple(rvol_values),
        pivots_by_end=pivots_by_end,
    )


def _w5_lifecycle_events(
    bars: Sequence[Bar],
    retention_bars: int = 24,
    defaults: IndicatorDefaults | None = None,
) -> list[dict[str, Any]]:
    defaults = defaults or DEFAULT_INDICATOR_SETTINGS
    if len(bars) < 40:
        return []
    out: list[dict[str, Any]] = []
    start = max(40, len(bars) - retention_bars)
    last_index = len(bars) - 1
    calculation_context = _w5_lifecycle_calculation_context(
        bars,
        defaults=defaults,
        first_end=start,
    )
    for end in range(start, len(bars) + 1):
        state = _w5_latest_state(
            bars[:end],
            defaults=defaults,
            _lifecycle_context=calculation_context,
        )
        if (
            not state
            or state.get("action") == "WAIT"
            or state.get("direction") == Direction.FLAT.value
        ):
            continue
        event = dict(state)
        age_bars = last_index - (end - 1)
        event["age_bars"] = age_bars
        event["retained"] = age_bars > 0
        if event.get("armed_expires_bars") is not None:
            event["expires_in_bars"] = max(int(event.get("armed_expires_bars") or 0) - age_bars, 0)
        if age_bars > 0:
            event["candidate_finality"] = (
                CandidateFinality.EXPIRED.value
                if int(event.get("expires_in_bars") or 0) <= 0
                else CandidateFinality.RETAINED.value
            )
        out.append(event)
    return out


def _w5_fib_zone(
    bars: Sequence[Bar],
    direction: Direction,
    pivots: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if len(bars) < 12 or direction == Direction.FLAT:
        return {"low": None, "high": None, "in_zone": False, "extension": None}
    latest = bars[-1]
    previous = bars[:-1]
    if direction == Direction.SHORT:
        pivot_highs = [
            pivot for pivot in pivots if pivot["type"] == 1 and pivot["index"] < len(bars) - 1
        ]
        range_high = (
            float(pivot_highs[-1]["price"])
            if pivot_highs
            else max(bar.high for bar in previous[-34:])
        )
        range_low = min(bar.low for bar in previous[-34:])
        leg = max(range_high - range_low, 1e-9)
        zone_low = range_high + leg * 0.236
        zone_high = range_high + leg * 0.618
        price = latest.high
        extension = _safe_div(price - range_high, leg)
    else:
        pivot_lows = [
            pivot for pivot in pivots if pivot["type"] == -1 and pivot["index"] < len(bars) - 1
        ]
        range_low = (
            float(pivot_lows[-1]["price"]) if pivot_lows else min(bar.low for bar in previous[-34:])
        )
        range_high = max(bar.high for bar in previous[-34:])
        leg = max(range_high - range_low, 1e-9)
        zone_high = range_low - leg * 0.236
        zone_low = range_low - leg * 0.618
        price = latest.low
        extension = _safe_div(range_low - price, leg)
    return {
        "low": _round(min(zone_low, zone_high)),
        "high": _round(max(zone_low, zone_high)),
        "in_zone": min(zone_low, zone_high) <= price <= max(zone_low, zone_high),
        "extension": round(extension, 4),
    }


def _w5_mode(value: Any) -> str:
    text = str(value or "scalp").strip().lower()
    return "swing" if text in {"swing", "mid", "midterm", "position"} else "scalp"


def _w5_bos_trigger_state(
    bars: Sequence[Bar], direction: Direction, mode: str = "scalp"
) -> tuple[float | None, str]:
    if len(bars) < 3 or direction == Direction.FLAT:
        return None, "WAIT"
    latest = bars[-1]
    if _w5_mode(mode) == "swing":
        if len(bars) < 6:
            return None, "WAIT"
        previous = bars[-5:-1]
        trigger = (
            min(bar.low for bar in previous)
            if direction == Direction.SHORT
            else max(bar.high for bar in previous)
        )
    else:
        prev_bar = bars[-2]
        trigger = float(prev_bar.low) if direction == Direction.SHORT else float(prev_bar.high)
    if direction == Direction.SHORT:
        return trigger, "GO" if latest.close < trigger else "ARMED"
    return trigger, "GO" if latest.close > trigger else "ARMED"


def _w5_mtf_confirmation(
    mtf_context: dict[str, Sequence[Bar]] | None,
    mtf_quality: dict[str, dict[str, Any]] | None = None,
    defaults: IndicatorDefaults | None = None,
) -> dict[str, Any]:
    defaults = defaults or DEFAULT_INDICATOR_SETTINGS
    if not mtf_context:
        return {"top_score": 0.0, "bottom_score": 0.0, "items": []}
    items: list[dict[str, Any]] = []
    top_score = 0.0
    bottom_score = 0.0
    for timeframe, context_bars in sorted(
        mtf_context.items(),
        key=lambda item: pine.interval_minutes(item[0]),
    ):
        if len(context_bars) < 40:
            continue
        quality = (mtf_quality or {}).get(timeframe) or {}
        if quality and not quality.get("ok", False):
            continue
        state = _w5_latest_state(context_bars, defaults=defaults)
        if not state:
            continue
        action = str(state.get("action") or "WAIT").upper()
        direction = direction_from_any(state.get("direction"))
        score = float(state["score"])
        if action == "WAIT" or direction == Direction.FLAT or score < 42.0:
            continue
        weight = 1.0 if pine.interval_minutes(timeframe) <= 5 else 0.75
        contribution = _clamp((score - 42.0) / 57.0, 0.0, 1.0) * 100.0 * weight
        if direction == Direction.SHORT:
            top_score = max(top_score, contribution)
        elif direction == Direction.LONG:
            bottom_score = max(bottom_score, contribution)
        items.append(
            {
                "timeframe": timeframe,
                "direction": direction.value,
                "action": action,
                "score": round(score, 2),
                "contribution": round(contribution, 2),
                "ts": state.get("ts"),
                "quality": quality,
            }
        )
    return {
        "top_score": round(top_score, 2),
        "bottom_score": round(bottom_score, 2),
        "items": items,
    }


def _w5_overlays(
    latest_bar: Bar | None,
    latest: dict[str, Any] | None,
    line_width: float = 1.0,
    show_geometry: bool = True,
) -> list[dict[str, Any]]:
    if latest_bar is None or not latest:
        return []
    action = str(latest.get("action") or "WAIT").upper()
    direction = direction_from_any(latest.get("direction"))
    if action == "WAIT" or direction == Direction.FLAT:
        return []
    price = latest.get("price", latest.get("level"))
    if price is None:
        return []
    out = [
        overlays.label(
            bar=latest_bar,
            price=float(price),
            lines=[],
            direction=direction,
        )
    ]
    out[0]["code"] = str(latest.get("code") or f"W5_{action}_{direction.value.upper()}")
    out[0]["compact_label"] = "W5"
    out[0]["action"] = action
    out[0].update(copy_indicator_facts(latest))
    signal_payload = latest.get("signal") if isinstance(latest.get("signal"), dict) else None
    if signal_payload:
        overlays.bind_signal_trade_plan(out[0], signal_payload, source="w5_structure")
    return out


def _w5_latest_state(
    bars: Sequence[Bar],
    *,
    mtf_context: dict[str, Sequence[Bar]] | None = None,
    mtf_quality: dict[str, dict[str, Any]] | None = None,
    mode: str = "scalp",
    defaults: IndicatorDefaults | None = None,
    feature_context: Any | None = None,
    pivot_context: PivotContext | None = None,
    _lifecycle_context: _W5LifecycleCalculationContext | None = None,
) -> dict[str, Any] | None:
    defaults = defaults or DEFAULT_INDICATOR_SETTINGS
    if len(bars) < 40:
        return None
    latest = bars[-1]
    context_end = len(bars)
    atr_values = _feature_series(feature_context, "atr_values")
    atr = (
        max(float(_lifecycle_context.atr_values[context_end - 1]), 1e-9)
        if _lifecycle_context is not None
        else max(float(atr_values[-1]), 1e-9)
        if atr_values
        else _atr_value(bars, defaults.atr_len)
    )
    shape = _bar_shape(latest)
    closes = [bar.close for bar in bars]
    if _lifecycle_context is not None:
        ema21 = [_lifecycle_context.ema_fast[context_end - 1]]
        ema55 = [_lifecycle_context.ema_slow[context_end - 1]]
        ema233 = [_lifecycle_context.ema_magnet[context_end - 1]]
        ewo_atr = max(
            float(_lifecycle_context.ewo_atr_values[context_end - 1]),
            1e-9,
        )
        ewo = [
            None if value is None else value / ewo_atr
            for value in _lifecycle_context.ewo_raw[:context_end]
        ]
        pivots = [
            dict(item)
            for item in _lifecycle_context.pivots_by_end.get(
                context_end,
                (),
            )
        ]
    else:
        ema21 = _feature_series(feature_context, "ema_fast") or _ema(closes, defaults.ema.fast)
        ema55 = _feature_series(feature_context, "ema_slow") or _ema(closes, defaults.ema.slow)
        ema233 = _feature_series(feature_context, "ema_magnet") or _ema(closes, defaults.ema.magnet)
        ewo = _ewo_series(bars)
        pivots = (
            pivot_context.alternating(3, 3)
            if pivot_context is not None
            else _pivot_points(bars, left=3, right=3)
        )
    highs = [pivot for pivot in pivots if pivot["type"] == 1]
    lows = [pivot for pivot in pivots if pivot["type"] == -1]
    rvol_values = _feature_series(feature_context, "rvol_values")
    rel_vol = (
        float(_lifecycle_context.rvol_values[context_end - 1])
        if _lifecycle_context is not None
        else float(rvol_values[-1])
        if rvol_values
        else _rel_volume(bars, len(bars) - 1, defaults.rvol_len)
    )

    def ewo_at(index: int) -> float | None:
        if index < 0 or index >= len(ewo):
            return None
        return ewo[index]

    bear_div = False
    bull_div = False
    top_vol_fade = False
    bottom_vol_fade = False
    if highs:
        last_high = highs[-1]
        prev_high = highs[-2] if len(highs) >= 2 else None
        last_ewo = ewo_at(last_high["index"])
        prev_ewo = ewo_at(prev_high["index"]) if prev_high else None
        bear_div = (
            prev_high is not None
            and latest.high >= last_high["price"]
            and last_high["price"] > prev_high["price"]
            and last_ewo is not None
            and prev_ewo is not None
            and last_ewo < prev_ewo - 0.12
        )
        if prev_high is not None:
            last_vol = bars[last_high["index"]].volume
            prev_vol = bars[prev_high["index"]].volume
            top_vol_fade = last_vol <= prev_vol * 0.92
    if lows:
        last_low = lows[-1]
        prev_low = lows[-2] if len(lows) >= 2 else None
        last_ewo = ewo_at(last_low["index"])
        prev_ewo = ewo_at(prev_low["index"]) if prev_low else None
        bull_div = (
            prev_low is not None
            and latest.low <= last_low["price"]
            and last_low["price"] < prev_low["price"]
            and last_ewo is not None
            and prev_ewo is not None
            and last_ewo > prev_ewo + 0.12
        )
        if prev_low is not None:
            last_vol = bars[last_low["index"]].volume
            prev_vol = bars[prev_low["index"]].volume
            bottom_vol_fade = last_vol <= prev_vol * 0.92

    ema_fast = ema21[-1]
    ema_slow = ema55[-1]
    ema_magnet = ema233[-1] if len(ema233) else ema_slow
    bull_stack = latest.close > ema_fast > ema_slow
    bear_stack = latest.close < ema_fast < ema_slow
    dist_ema_atr = (latest.close - ema_magnet) / atr
    ewo_now = ewo[-1] if ewo[-1] is not None else 0.0
    ewo_prev = ewo[-4] if len(ewo) >= 4 and ewo[-4] is not None else ewo_now
    ewo_slope = ewo_now - ewo_prev
    run_bull = 0.0
    run_bull += 18.0 if bull_stack else 0.0
    run_bull += 12.0 if latest.close > max(bar.high for bar in bars[-6:-1]) else 0.0
    run_bull += 12.0 if shape["close_pos"] >= 0.72 and latest.close > latest.open else 0.0
    run_bull += 8.0 if rel_vol >= 1.15 else 0.0
    run_bull += _clamp((dist_ema_atr - 1.0) / 1.5, 0.0, 1.0) * 12.0
    run_bear = 0.0
    run_bear += 18.0 if bear_stack else 0.0
    run_bear += 12.0 if latest.close < min(bar.low for bar in bars[-6:-1]) else 0.0
    run_bear += 12.0 if shape["close_pos"] <= 0.28 and latest.close < latest.open else 0.0
    run_bear += 8.0 if rel_vol >= 1.15 else 0.0
    run_bear += _clamp((-dist_ema_atr - 1.0) / 1.5, 0.0, 1.0) * 12.0

    top_reject = shape["upper_share"] >= 0.35 and shape["close_pos"] <= 0.58
    bottom_reject = shape["lower_share"] >= 0.35 and shape["close_pos"] >= 0.42
    churn = rel_vol >= 1.20 and shape["body_share"] <= 0.50
    top_score = 0.0
    top_score += 28.0 if bear_div else 0.0
    top_score += 12.0 if top_vol_fade else 0.0
    top_score += 13.0 if top_reject else 0.0
    top_score += 12.0 if churn and shape["close_pos"] <= 0.62 else 0.0
    top_score += 10.0 if ewo_slope < 0 and ewo_now > 0 else 0.0
    top_score += _clamp((dist_ema_atr - 1.15) / 1.8, 0.0, 1.0) * 17.0
    top_score += 6.0 if latest.high >= max(bar.high for bar in bars[-20:-1]) else 0.0
    top_score -= 10.0 if run_bull >= 50.0 and not top_reject and not bear_div else 0.0
    bottom_score = 0.0
    bottom_score += 28.0 if bull_div else 0.0
    bottom_score += 12.0 if bottom_vol_fade else 0.0
    bottom_score += 13.0 if bottom_reject else 0.0
    bottom_score += 12.0 if churn and shape["close_pos"] >= 0.38 else 0.0
    bottom_score += 10.0 if ewo_slope > 0 and ewo_now < 0 else 0.0
    bottom_score += _clamp((-dist_ema_atr - 1.15) / 1.8, 0.0, 1.0) * 17.0
    bottom_score += 6.0 if latest.low <= min(bar.low for bar in bars[-20:-1]) else 0.0
    bottom_score -= 10.0 if run_bear >= 50.0 and not bottom_reject and not bull_div else 0.0
    chart_top_score = _clamp(top_score, 0.0, 99.0)
    chart_bottom_score = _clamp(bottom_score, 0.0, 99.0)
    fast_top_score = _clamp(
        (18.0 if top_reject else 0.0)
        + (14.0 if churn and shape["close_pos"] <= 0.62 else 0.0)
        + (12.0 if ewo_slope < 0 and ewo_now > 0 else 0.0)
        + (8.0 if rel_vol >= 1.15 else 0.0),
        0.0,
        99.0,
    )
    fast_bottom_score = _clamp(
        (18.0 if bottom_reject else 0.0)
        + (14.0 if churn and shape["close_pos"] >= 0.38 else 0.0)
        + (12.0 if ewo_slope > 0 and ewo_now < 0 else 0.0)
        + (8.0 if rel_vol >= 1.15 else 0.0),
        0.0,
        99.0,
    )
    context_top_score = _clamp(
        (20.0 if bear_div else 0.0)
        + (12.0 if top_vol_fade else 0.0)
        + _clamp((dist_ema_atr - 1.15) / 1.8, 0.0, 1.0) * 22.0
        + (12.0 if latest.high >= max(bar.high for bar in bars[-20:-1]) else 0.0)
        - (18.0 if run_bull >= 72.0 and not top_reject and not bear_div else 0.0),
        0.0,
        99.0,
    )
    context_bottom_score = _clamp(
        (20.0 if bull_div else 0.0)
        + (12.0 if bottom_vol_fade else 0.0)
        + _clamp((-dist_ema_atr - 1.15) / 1.8, 0.0, 1.0) * 22.0
        + (12.0 if latest.low <= min(bar.low for bar in bars[-20:-1]) else 0.0)
        - (18.0 if run_bear >= 72.0 and not bottom_reject and not bull_div else 0.0),
        0.0,
        99.0,
    )
    weighted_top_score = _clamp(
        chart_top_score * 0.55 + fast_top_score * 0.25 + context_top_score * 0.20, 0.0, 99.0
    )
    weighted_bottom_score = _clamp(
        chart_bottom_score * 0.55 + fast_bottom_score * 0.25 + context_bottom_score * 0.20,
        0.0,
        99.0,
    )
    mtf = _w5_mtf_confirmation(mtf_context, mtf_quality, defaults=defaults)
    mtf_top_score = float(mtf["top_score"])
    mtf_bottom_score = float(mtf["bottom_score"])
    top_score = _clamp(max(chart_top_score, weighted_top_score), 0.0, 99.0)
    bottom_score = _clamp(max(chart_bottom_score, weighted_bottom_score), 0.0, 99.0)
    if mtf_top_score > mtf_bottom_score + 8.0:
        top_score = _clamp(top_score + _clamp(mtf_top_score / 100.0, 0.0, 1.0) * 10.0, 0.0, 99.0)
        bottom_score = _clamp(
            bottom_score - _clamp((mtf_top_score - mtf_bottom_score) / 100.0, 0.0, 1.0) * 6.0,
            0.0,
            99.0,
        )
    elif mtf_bottom_score > mtf_top_score + 8.0:
        bottom_score = _clamp(
            bottom_score + _clamp(mtf_bottom_score / 100.0, 0.0, 1.0) * 10.0, 0.0, 99.0
        )
        top_score = _clamp(
            top_score - _clamp((mtf_bottom_score - mtf_top_score) / 100.0, 0.0, 1.0) * 6.0,
            0.0,
            99.0,
        )
    top_fib_zone = _w5_fib_zone(bars, Direction.SHORT, pivots)
    bottom_fib_zone = _w5_fib_zone(bars, Direction.LONG, pivots)
    if top_fib_zone["in_zone"]:
        top_score = _clamp(top_score + 6.0, 0.0, 99.0)
    if bottom_fib_zone["in_zone"]:
        bottom_score = _clamp(bottom_score + 6.0, 0.0, 99.0)
    if top_score < 42.0 and bottom_score < 42.0:
        direction = Direction.FLAT
        action = "WAIT"
        score = max(top_score, bottom_score)
        level = latest.close
        fib_zone = {"low": None, "high": None, "in_zone": False, "extension": None}
        runaway_veto = False
    else:
        short_side = top_score >= bottom_score
        direction = Direction.SHORT if short_side else Direction.LONG
        score = top_score if short_side else bottom_score
        fib_zone = top_fib_zone if short_side else bottom_fib_zone
        runaway_veto = (short_side and run_bull >= 72.0 and not top_reject and not bear_div) or (
            (not short_side) and run_bear >= 72.0 and not bottom_reject and not bull_div
        )
        if runaway_veto:
            score = min(score, 41.0)
            action = "WAIT"
            direction = Direction.FLAT
        else:
            bos_trigger, bos_state = _w5_bos_trigger_state(bars, direction, mode)
            if score >= 70.0 and bos_state == "GO":
                action = "GO"
            else:
                action = score_action(score)
    mode_value = _w5_mode(mode)
    mode_label = "Swing" if mode_value == "swing" else "Scalp"
    trigger_label = "Swing-BOS" if mode_value == "swing" else "Micro-BOS"
    stop_atr = 0.25 if mode_value == "swing" else 0.15
    target_rr = 1.5 if mode_value == "swing" else 1.3
    trigger, trigger_state = _w5_bos_trigger_state(bars, direction, mode_value)
    if direction == Direction.SHORT:
        level = latest.high
        stop = level + atr * stop_atr
        risk = abs(stop - (trigger or latest.close))
        target = (trigger - risk * target_rr) if trigger else None
    elif direction == Direction.LONG:
        level = latest.low
        stop = level - atr * stop_atr
        risk = abs(stop - (trigger or latest.close))
        target = (trigger + risk * target_rr) if trigger else None
    else:
        level = latest.close
        stop = None
        target = None
    armed_expires_bars = (
        5 if action in {"ARM", "WATCH", "CANDIDATE"} and direction != Direction.FLAT else 0
    )
    armed_expires_at = (
        expiry_after_bars(latest.ts, latest.timeframe, armed_expires_bars)
        if armed_expires_bars
        else None
    )
    signal = SignalState(
        source="w5_structure",
        action=ActionPhase(action),
        raw_action=action,
        direction=direction,
        score=score,
        confirmed=latest.closed,
        source_tf=latest.timeframe,
        trigger=trigger,
        stop=stop,
        target=target,
        invalidation=stop,
        reason="w5_exhaustion",
        code=f"W5_{action}_{direction.value.upper()}",
    )
    return {
        "id": f"w5-{latest.ts.isoformat()}-{direction.value}",
        "ts": latest.ts.isoformat(),
        "symbol": latest.symbol,
        "timeframe": latest.timeframe,
        "trigger_type": "indicator_event",
        "source": "w5_structure",
        "event_type": "formation" if action != "WAIT" else "state",
        "code": f"W5_{action}_{direction.value.upper()}",
        "candidate_finality": (
            CandidateFinality.CONFIRMED.value
            if latest.closed
            else CandidateFinality.PROVISIONAL.value
        ),
        "direction": direction.value,
        "action": action,
        "score": round(score, 2),
        "top_score": round(top_score, 2),
        "bottom_score": round(bottom_score, 2),
        "chart_top_score": round(chart_top_score, 2),
        "chart_bottom_score": round(chart_bottom_score, 2),
        "fast_top_score": round(fast_top_score, 2),
        "fast_bottom_score": round(fast_bottom_score, 2),
        "context_top_score": round(context_top_score, 2),
        "context_bottom_score": round(context_bottom_score, 2),
        "weighted_top_score": round(weighted_top_score, 2),
        "weighted_bottom_score": round(weighted_bottom_score, 2),
        "mtf_top_score": round(mtf_top_score, 2),
        "mtf_bottom_score": round(mtf_bottom_score, 2),
        "mtf_context": mtf,
        "run_bull": round(run_bull, 2),
        "run_bear": round(run_bear, 2),
        "price": _round(level),
        "level": _round(level),
        "trigger": _round(trigger),
        "trigger_state": trigger_state,
        "mode": mode_value,
        "stop": _round(stop),
        "target": _round(target),
        "fib_zone": fib_zone,
        "in_fib_zone": bool(fib_zone["in_zone"]),
        "runaway_veto": runaway_veto,
        "armed_expires_bars": armed_expires_bars,
        "armed_expires_at": armed_expires_at.isoformat() if armed_expires_at else None,
        **indicator_fact_payload(
            scenario="wave_5_exhaustion",
            trigger_event={
                "code": (
                    "w5_swing_bos_trigger" if mode_value == "swing" else "w5_micro_bos_trigger"
                ),
                "trigger_state": trigger_state,
                "trigger_kind": trigger_label,
            },
            opposing={"code": "runaway_veto"} if runaway_veto else None,
            quality={"code": "score", "value": score},
            metrics={
                "mode": mode_value,
                "mode_label": mode_label,
                "trigger_state": str(trigger_state),
                "fib_zone_in": bool(fib_zone["in_zone"]),
                "driver": "divergence" if bear_div or bull_div else "wick_extension",
                "score": _metric_number(score, digits=0),
                "level": _metric_number(level),
                "trigger": _metric_number(trigger),
                "stop": _metric_number(stop),
                "target": _metric_number(target),
                "top_score": _metric_number(top_score, digits=0),
                "bottom_score": _metric_number(bottom_score, digits=0),
                "weighted_top_score": _metric_number(weighted_top_score, digits=0),
                "weighted_bottom_score": _metric_number(weighted_bottom_score, digits=0),
                "fast_top_score": _metric_number(fast_top_score, digits=0),
                "fast_bottom_score": _metric_number(fast_bottom_score, digits=0),
                "context_top_score": _metric_number(context_top_score, digits=0),
                "context_bottom_score": _metric_number(context_bottom_score, digits=0),
                "mtf_top_score": _metric_number(mtf_top_score, digits=0),
                "mtf_bottom_score": _metric_number(mtf_bottom_score, digits=0),
                "run_bull": _metric_number(run_bull, digits=0),
                "run_bear": _metric_number(run_bear, digits=0),
                "fib_zone_low": _metric_number(fib_zone["low"]),
                "fib_zone_high": _metric_number(fib_zone["high"]),
                "ewo": _metric_number(ewo_now),
                "ewo_slope": _metric_number(ewo_slope),
                "rvol": _metric_number(rel_vol),
            },
        ),
        "signal": signal.as_dict(),
        "details": {
            "bear_div": bear_div,
            "bull_div": bull_div,
            "top_reject": top_reject,
            "bottom_reject": bottom_reject,
            "top_vol_fade": top_vol_fade,
            "bottom_vol_fade": bottom_vol_fade,
            "score_stack": {
                "chart_top": round(chart_top_score, 2),
                "chart_bottom": round(chart_bottom_score, 2),
                "fast_top": round(fast_top_score, 2),
                "fast_bottom": round(fast_bottom_score, 2),
                "context_top": round(context_top_score, 2),
                "context_bottom": round(context_bottom_score, 2),
                "weighted_top": round(weighted_top_score, 2),
                "weighted_bottom": round(weighted_bottom_score, 2),
                "mtf_top": round(mtf_top_score, 2),
                "mtf_bottom": round(mtf_bottom_score, 2),
            },
            "mtf_context": mtf,
            "rel_volume": round(rel_vol, 3),
            "ewo": round(float(ewo_now), 4),
            "ewo_slope": round(float(ewo_slope), 4),
            "fib_zone": fib_zone,
            "trigger_state": trigger_state,
            "mode": mode_value,
            "runaway_veto": runaway_veto,
        },
    }
