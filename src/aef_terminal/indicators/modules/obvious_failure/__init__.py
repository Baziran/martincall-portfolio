from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from aef_terminal.domain import ActionPhase, Bar, Direction, ScenarioKind
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
)
from aef_terminal.indicators.control_specs import _control
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults
from aef_terminal.indicators.module_contract import (
    ConfirmedBarContextRequest,
    INDICATOR_FACT_RUNTIME_FIELDS,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.domain_facts import copy_indicator_facts, indicator_fact_payload
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import bool_param, float_or_none, round_optional
from aef_terminal.runtime.mtf import parent_context_cutoff
from aef_terminal.runtime.signal_state import SignalState
from aef_terminal.runtime.timeframes import adapt_bars, interval_minutes


OBVIOUS_FAILURE_VERSION = "0.2-python-confirmed-1m-preview"
OBVIOUS_FAILURE_MICRO_TIMEFRAME = "1m"
OBVIOUS_FAILURE_OVERLAY_SIGNAL_LIMIT = 80
OBVIOUS_FAILURE_OVERLAY_COMPACT_LIMIT = OBVIOUS_FAILURE_OVERLAY_SIGNAL_LIMIT * 2


_ROW_FIELDS = (
    "action",
    "blocked",
    "blocked_reason",
    "boundary",
    "code",
    "confirmed",
    "details",
    "direction",
    "event_type",
    "hypothesis",
    "id",
    "invalidation",
    "kind",
    "level",
    "metrics",
    "overlays",
    "plan_coherent",
    "plan_complete",
    "price",
    "range",
    "raw_action",
    "reason_code",
    "reward",
    "risk",
    "rr",
    "score",
    "signal",
    "signal_actionable",
    "source",
    "source_tf",
    "state",
    "stop",
    "target",
    "trade_plan",
    "trigger",
    "ts",
    "zone",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "obvious_failure",
        "Obvious Failure",
        "failed obvious breakout/breakdown attempts at channel or SMC boundaries",
        {
            "version": OBVIOUS_FAILURE_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "hypotheses": [],
        },
        "obviousFailure",
        "aef_terminal.indicators.modules.obvious_failure:obvious_failure",
        pipeline_stage="setup",
        pipeline_order=60,
        signal_source="obvious_failure",
        candidate_score_floor_param="min_score",
        min_target_atr=0.6,
        optional_context=("market_spotlight", "smc_channels"),
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe=OBVIOUS_FAILURE_MICRO_TIMEFRAME,
                history_bars=256,
                role="independent_scan",
            ),
        ),
        score_family="reversal",
        empirical_power=1.00,
        usefulness=1.10,
        notes="Experimental boundary-failure/fakeout candidate. Use as fade context, not as a standalone truth source.",
        state_key="obviousFailure",
        calc_key="obviousFailureCalcEnabled",
        visible_key="obviousFailureVisible",
        chart_control_id="obvious-failure-toggle",
        process_control_id="obvious-failure-process",
        api_enabled_key="obvious_failure_enabled",
        manager_order=72,
        runtime_order=70,
        default_calc=False,
        overlay_layer="zones",
        overlay_filter_ref="generic_overlay_filter",
        runtime_payload_contract={
            "latest": _ROW_FIELDS,
            "series": _ROW_FIELDS,
            "events": _ROW_FIELDS,
            "signals": _ROW_FIELDS,
        },
        runtime_payload_schema_refs=("overlay_contract",),
        runtime_payload_compact={
            "series": {"limit": 80, "fields": "runtime_contract"},
            "events": {"limit": 80, "fields": "runtime_contract"},
            "overlays": {
                "limit": OBVIOUS_FAILURE_OVERLAY_COMPACT_LIMIT,
                "fields": "compact_contract",
            },
        },
        controls=(
            _control(
                "zones",
                "Zones",
                "toggle",
                True,
                "obviousFailureZones",
                element_id="obvious-failure-zones",
                api_key="obvious_failure_zones",
                param_key="zones",
                compact=True,
            ),
            _control(
                "labels",
                "Labels",
                "toggle",
                True,
                "obviousFailureLabels",
                element_id="obvious-failure-labels",
                api_key="obvious_failure_labels",
                param_key="labels",
                compact=True,
            ),
            _control(
                "lookback",
                "Lookback",
                "number",
                24,
                "obviousFailureLookback",
                element_id="obvious-failure-lookback",
                api_key="obvious_failure_lookback",
                param_key="lookback",
                minimum=8,
                maximum=80,
                step=1,
                action="load_apply",
            ),
            _control(
                "minScore",
                "Min",
                "number",
                68,
                "obviousFailureMinScore",
                element_id="obvious-failure-min-score",
                api_key="obvious_failure_min_score",
                param_key="min_score",
                minimum=50,
                maximum=95,
                step=1,
                action="load_apply",
            ),
        ),
    ),
    adapter_ref="aef_terminal.indicators.modules.obvious_failure:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.obvious_failure:build_params",
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = obvious_failure
    params = ctx.params("obvious_failure")
    if not isinstance(params, ObviousFailureParams):
        raise TypeError("obvious_failure runtime params must be ObviousFailureParams")
    micro_bars = list(ctx.confirmed_bar_context.get(OBVIOUS_FAILURE_MICRO_TIMEFRAME, ()))
    micro_quality = ctx.confirmed_bar_context_quality.get(
        OBVIOUS_FAILURE_MICRO_TIMEFRAME,
        {},
    )
    parent_cutoff = (
        parent_context_cutoff(
            ctx.analysis_latest,
            OBVIOUS_FAILURE_MICRO_TIMEFRAME,
        )
        if ctx.analysis_latest is not None
        else None
    )
    micro_preview_active = bool(
        micro_bars
        and micro_quality.get("ok") is True
        and parent_cutoff is not None
        and micro_bars[-1].ts > parent_cutoff
    )
    parent_minutes = (
        interval_minutes(ctx.analysis_latest.timeframe) if ctx.analysis_latest is not None else 5
    )
    micro_params = replace(
        params,
        lookback=adapt_bars(
            params.lookback,
            OBVIOUS_FAILURE_MICRO_TIMEFRAME,
            source_minutes=parent_minutes,
        ),
        atr_len=adapt_bars(
            params.atr_len,
            OBVIOUS_FAILURE_MICRO_TIMEFRAME,
            source_minutes=parent_minutes,
        ),
        max_reclaim_bars=adapt_bars(
            params.max_reclaim_bars,
            OBVIOUS_FAILURE_MICRO_TIMEFRAME,
            source_minutes=parent_minutes,
        ),
        zone_bars=adapt_bars(
            params.zone_bars,
            OBVIOUS_FAILURE_MICRO_TIMEFRAME,
            source_minutes=parent_minutes,
        ),
    )
    return IndicatorExecutionSpec(
        id="obvious_failure",
        input_bars=ctx.confirmed_bars,
        analysis_bar=ctx.analysis_latest,
        mode="confirmed",
        calculate=lambda: calculate(
            ctx.confirmed_bars,
            params=params,
            indicator_bundle=ctx.indicator_bundle,
            display={"zones": params.zones, "labels": params.labels},
        ),
        preview_calculate=(
            (
                lambda: calculate(
                    micro_bars,
                    params=micro_params,
                    indicator_bundle=ctx.indicator_bundle,
                    display={"zones": params.zones, "labels": params.labels},
                )
            )
            if micro_preview_active
            else None
        ),
        preview_event_ts=(micro_bars[-1].ts.isoformat() if micro_preview_active else ""),
        params=params,
        runtime_params=ctx.runtime_params,
    )


@dataclass(frozen=True)
class ObviousFailureParams:
    zones: bool = True
    labels: bool = True
    lookback: int = 24
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    min_score: float = 68.0
    reclaim_buffer_atr: float = 0.08
    max_reclaim_bars: int = 4
    boundary_near_atr: float = 0.65
    target_r: float = 1.6
    zone_bars: int = 18


def build_params(raw: dict[str, Any], defaults: IndicatorDefaults) -> ObviousFailureParams:
    return ObviousFailureParams(
        zones=bool_param(raw.get("zones"), ObviousFailureParams.zones),
        labels=bool_param(raw.get("labels"), ObviousFailureParams.labels),
        lookback=int(raw.get("lookback", ObviousFailureParams.lookback)),
        atr_len=int(raw.get("atr_len", defaults.atr_len)),
        min_score=float(raw.get("min_score", ObviousFailureParams.min_score)),
        reclaim_buffer_atr=float(
            raw.get("reclaim_buffer_atr", ObviousFailureParams.reclaim_buffer_atr)
        ),
        max_reclaim_bars=int(raw.get("max_reclaim_bars", ObviousFailureParams.max_reclaim_bars)),
        boundary_near_atr=float(
            raw.get("boundary_near_atr", ObviousFailureParams.boundary_near_atr)
        ),
        target_r=float(raw.get("target_r", ObviousFailureParams.target_r)),
        zone_bars=int(raw.get("zone_bars", ObviousFailureParams.zone_bars)),
    )


def _round(value: float | None, digits: int = 2) -> float | None:
    return round_optional(value, digits)


def _metric_number(value: Any, *, digits: int = 2, suffix: str = "") -> dict[str, Any]:
    numeric = float_or_none(value)
    return {
        "value": _round(numeric, digits) if numeric is not None else None,
        "digits": digits,
        "suffix": suffix,
    }


def _append_evidence(
    evidence: list[dict[str, Any]],
    *,
    kind: str,
    source: str,
    code: str,
    score_delta: float,
) -> None:
    evidence.append(
        {
            "kind": kind,
            "source": source,
            "code": code,
            "score_delta": round(float(score_delta), 2),
        }
    )


def _spotlight_support(
    *,
    market_context: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
) -> float:
    if not market_context:
        return 0.0
    flow_bias = float_or_none(market_context.get("flow_bias"))
    if flow_bias is None:
        raise ValueError("market spotlight context requires a finite flow_bias")
    flow_conflict = bool(market_context.get("flow_conflict"))
    direction = str(market_context.get("direction") or "flat").lower()
    side_lock = str(market_context.get("side_lock") or "CHECK").upper()
    entry = market_context.get("entry") if isinstance(market_context.get("entry"), dict) else {}
    entry_phase = str(entry.get("phase") or "").lower()
    strategy = (
        market_context.get("strategy") if isinstance(market_context.get("strategy"), dict) else {}
    )
    strategy_phase = str(strategy.get("phase") or "").lower()
    score = 0.0
    if flow_conflict or direction == "flat" or side_lock == "CHECK" or entry_phase == "blocked":
        score += 6.0
        _append_evidence(
            evidence,
            kind="support",
            source="market_spotlight",
            code="range_fade_context",
            score_delta=6.0,
        )
    if abs(flow_bias) >= 3 or side_lock in {"LONG", "SHORT"} or strategy_phase == "armed":
        score -= 5.0
        _append_evidence(
            evidence,
            kind="warning",
            source="market_spotlight",
            code="continuation_context",
            score_delta=-5.0,
        )
    return score


def _nearest_smc_box_support(
    *,
    direction: Direction,
    price: float,
    atr: float,
    smc_context: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
) -> float:
    boxes = (
        [*(smc_context.get("fvg_zones") or []), *(smc_context.get("order_blocks") or [])]
        if isinstance(smc_context, dict)
        else None
    )
    if not isinstance(boxes, list):
        return 0.0
    best_distance: float | None = None
    best_kind = "box"
    for box in boxes[-12:]:
        if not isinstance(box, dict) or box.get("state") == "filled":
            continue
        top = float_or_none(box.get("top"))
        bottom = float_or_none(box.get("bottom"))
        if top is None or bottom is None:
            continue
        edge = top if direction == Direction.SHORT else bottom
        distance = abs(price - edge)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_kind = str(
                box.get("kind")
                or (
                    "fvg"
                    if str(box.get("type") or "").upper().startswith("FVG")
                    else "order_block"
                    if str(box.get("type") or "").upper().startswith("OB")
                    else "box"
                )
            )
    if best_distance is not None and best_distance <= atr * 0.75:
        _append_evidence(
            evidence,
            kind="support",
            source="smc_channels",
            code=f"{best_kind}_boundary_overlap",
            score_delta=6.0,
        )
        return 6.0
    return 0.0


def _failure_signal(
    *,
    bars: Sequence[Bar],
    params: ObviousFailureParams,
    indicator_bundle: dict[str, dict[str, Any]],
    index: int | None = None,
    atr_values: Sequence[float] | None = None,
) -> dict[str, Any] | None:
    lookback = max(int(params.lookback), 8)
    signal_index = len(bars) - 1 if index is None else int(index)
    if signal_index < lookback + 1 or signal_index >= len(bars):
        return None
    latest = bars[signal_index]
    resolved_atr_values = (
        atr_values if atr_values is not None else pine.atr_rma_series(bars, params.atr_len)
    )
    atr = max(float(resolved_atr_values[signal_index]), 1e-9)
    buffer = atr * max(float(params.reclaim_buffer_atr), 0.0)

    direction = Direction.FLAT
    code = ""
    attempt_code = ""
    boundary = None
    fakeout_extreme = None
    probe: Bar | None = None
    high = low = mid = 0.0
    first_probe_index = max(lookback, signal_index - max(int(params.max_reclaim_bars), 1))
    for probe_index in range(first_probe_index, signal_index):
        candidate_probe = bars[probe_index]
        prior = bars[probe_index - lookback : probe_index]
        if len(prior) < lookback:
            continue
        candidate_high = max(float(bar.high) for bar in prior)
        candidate_low = min(float(bar.low) for bar in prior)
        between = bars[probe_index + 1 : signal_index]
        if (
            candidate_probe.high > candidate_high + buffer
            and latest.close < candidate_high - buffer
            and not any(float(bar.close) < candidate_high - buffer for bar in between)
        ):
            direction = Direction.SHORT
            code = "OBV_FAIL_UPPER"
            attempt_code = "upper_boundary_breakout_failed"
            boundary = candidate_high
            fakeout_extreme = max(float(bar.high) for bar in bars[probe_index : signal_index + 1])
            probe = candidate_probe
            high = candidate_high
            low = candidate_low
            mid = (high + low) * 0.5
            break
        if (
            candidate_probe.low < candidate_low - buffer
            and latest.close > candidate_low + buffer
            and not any(float(bar.close) > candidate_low + buffer for bar in between)
        ):
            direction = Direction.LONG
            code = "OBV_FAIL_LOWER"
            attempt_code = "lower_boundary_breakdown_failed"
            boundary = candidate_low
            fakeout_extreme = min(float(bar.low) for bar in bars[probe_index : signal_index + 1])
            probe = candidate_probe
            high = candidate_high
            low = candidate_low
            mid = (high + low) * 0.5
            break
    if direction == Direction.FLAT or boundary is None or fakeout_extreme is None or probe is None:
        return None

    evidence: list[dict[str, Any]] = []
    score = 64.0
    score += _spotlight_support(
        market_context=(indicator_bundle.get("market_spotlight") or {}).get("market_context")
        if isinstance(indicator_bundle.get("market_spotlight"), dict)
        else None,
        evidence=evidence,
    )
    score += _nearest_smc_box_support(
        direction=direction,
        price=float(boundary),
        atr=atr,
        smc_context=indicator_bundle.get("smc_channels"),
        evidence=evidence,
    )
    score = pine.clamp(score, 0.0, 99.0)

    trigger = float(latest.close)
    if direction == Direction.SHORT:
        stop = max(float(fakeout_extreme), float(boundary) + atr * 0.35)
        target = min(mid, trigger - max(stop - trigger, atr * 0.6) * params.target_r)
        zone_top, zone_bottom = stop, boundary
    else:
        stop = min(float(fakeout_extreme), float(boundary) - atr * 0.35)
        target = max(mid, trigger + max(trigger - stop, atr * 0.6) * params.target_r)
        zone_top, zone_bottom = boundary, stop

    if direction == Direction.SHORT and not (target < trigger < stop):
        target = trigger - max(stop - trigger, atr) * params.target_r
    if direction == Direction.LONG and not (stop < trigger < target):
        target = trigger + max(trigger - stop, atr) * params.target_r

    support_evidence = [item for item in evidence if item.get("kind") == "support"]
    warning_evidence = [item for item in evidence if item.get("kind") == "warning"]
    trigger_event_code = f"{attempt_code}_close_back_inside"
    risk_amount = abs(trigger - stop)
    action = "GO" if score >= float(params.min_score) else "WATCH"
    reason_code = f"{code.lower()}_confirmed"
    signal = SignalState(
        source="obvious_failure",
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
        reason=reason_code,
        code=code,
        kind=ScenarioKind.FADE,
    )
    signal_payload = signal.as_dict()
    signal_payload["reason_code"] = signal_payload.pop("reason", reason_code)
    fact_fields = indicator_fact_payload(
        scenario="obvious_failure",
        trigger_event={"code": trigger_event_code},
        supporting=support_evidence,
        opposing=warning_evidence,
        context=[{"code": attempt_code}, {"code": "close_back_inside"}],
        risk={"code": "fakeout_extreme_invalidation", "amount": _round(risk_amount)},
        fact_groups=[
            {
                "kind": "failure_evidence",
                "items": [*support_evidence, *warning_evidence],
            }
        ],
        metrics={
            "attempt_code": attempt_code,
            "boundary_role": "upper" if direction == Direction.SHORT else "lower",
            "boundary": _metric_number(boundary),
            "fakeout_extreme": _metric_number(fakeout_extreme),
            "risk_amount": _metric_number(risk_amount),
            "support_count": len(support_evidence),
            "warning_count": len(warning_evidence),
        },
    )
    return {
        "ts": latest.ts.isoformat(),
        "state": "ACTIVE" if action == "GO" else "WATCH",
        "code": code,
        "direction": direction.value,
        "score": round(score, 2),
        "price": trigger,
        "level": _round(boundary),
        "trigger": _round(trigger),
        "stop": _round(stop),
        "target": _round(target),
        "range": {
            "high": _round(high),
            "low": _round(low),
            "mid": _round(mid),
            "lookback": lookback,
        },
        "boundary": {
            "price": _round(boundary),
            "kind": "upper" if direction == Direction.SHORT else "lower",
        },
        "hypothesis": {
            "name": "failed_breakout_short"
            if direction == Direction.SHORT
            else "failed_breakdown_long",
            "attempt_code": attempt_code,
            "failure_code": "close_back_inside",
            "evidence": evidence,
        },
        "details": {
            "attempt_code": attempt_code,
            "boundary": _round(boundary),
            "fakeout_extreme": _round(fakeout_extreme),
            "evidence": evidence,
            "range_high": _round(high),
            "range_low": _round(low),
            "range_mid": _round(mid),
        },
        **fact_fields,
        "signal": signal_payload,
        "zone": {
            "top": _round(max(zone_top, zone_bottom)),
            "bottom": _round(min(zone_top, zone_bottom)),
            "start_ts": probe.ts.isoformat(),
            "end_ts": latest.ts.isoformat(),
        },
    }


def _obvious_failure_fact_fields(latest: dict[str, Any]) -> dict[str, Any]:
    return copy_indicator_facts(latest)


def _obvious_failure_overlays(
    latest: dict[str, Any],
    *,
    display: dict[str, bool],
    retention: str = "history",
) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    zone = latest.get("zone") if isinstance(latest.get("zone"), dict) else {}
    if display.get("zones", True) and zone:
        overlays.append(
            {
                "type": "box",
                "retention": retention,
                "start_ts": zone.get("start_ts"),
                "end_ts": zone.get("end_ts"),
                "top": zone.get("top"),
                "bottom": zone.get("bottom"),
                "tone": "info",
                "style": "dashed",
                "width": 1.0,
                "opacity": 0.12,
                "role": "obvious_failure_zone",
                "control_key": "zones",
                "source": "obvious_failure",
                **_obvious_failure_fact_fields(latest),
                "trade_plan": (latest.get("signal") or {}).get("trade_plan"),
            }
        )
    if display.get("labels", True):
        overlays.append(
            {
                "type": "label",
                "retention": retention,
                "ts": latest.get("ts"),
                "price": latest.get("price"),
                "direction": latest.get("direction"),
                "code": latest.get("code"),
                "tone": "info",
                "role": "obvious_failure_label",
                "control_key": "labels",
                "source": "obvious_failure",
                "signal_overlay": True,
                **_obvious_failure_fact_fields(latest),
                "trade_plan": (latest.get("signal") or {}).get("trade_plan"),
            }
        )
    return overlays


def _obvious_failure_event(latest: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"obvious-failure-{latest['ts']}-{latest['code'].lower()}",
        "source": "obvious_failure",
        "event_type": "obvious_failure",
        **latest,
    }


def _obvious_failure_signal_item(latest: dict[str, Any]) -> dict[str, Any] | None:
    signal = latest.get("signal") if isinstance(latest.get("signal"), dict) else None
    if signal is None:
        return None
    details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
    return {
        **signal,
        "id": f"obvious-failure-{latest['ts']}-{latest['code'].lower()}",
        "ts": latest.get("ts"),
        "source": "obvious_failure",
        "event_type": "obvious_failure",
        "code": latest.get("code"),
        "price": latest.get("price"),
        "level": latest.get("level"),
        **_obvious_failure_fact_fields(latest),
        "details": {
            **details,
            "hypothesis": latest.get("hypothesis"),
            "zone": latest.get("zone"),
        },
    }


def obvious_failure(
    bars: Sequence[Bar],
    params: ObviousFailureParams | None = None,
    *,
    indicator_bundle: dict[str, dict[str, Any]] | None = None,
    display: dict[str, bool] | None = None,
) -> dict[str, Any]:
    params = params or ObviousFailureParams()
    bundle = indicator_bundle or {}
    display = display or {}
    empty = {
        "version": OBVIOUS_FAILURE_VERSION,
        "series": [],
        "events": [],
        "latest": None,
        "overlays": [],
        "hypotheses": [],
    }
    if not bars:
        return empty

    atr_values = pine.atr_rma_series(bars, params.atr_len)
    lookback = max(int(params.lookback), 8)
    signals = [
        signal
        for index in range(lookback + 1, len(bars))
        if (
            signal := _failure_signal(
                bars=bars,
                params=params,
                indicator_bundle=bundle,
                index=index,
                atr_values=atr_values,
            )
        )
        is not None
    ]
    current = signals[-1] if signals and signals[-1].get("ts") == bars[-1].ts.isoformat() else None
    if current is None:
        return {
            **empty,
            "series": signals[-160:],
            "events": [_obvious_failure_event(item) for item in signals[-160:]],
            "signals": [],
            "overlays": [
                overlay
                for item in signals[-OBVIOUS_FAILURE_OVERLAY_SIGNAL_LIMIT:]
                for overlay in _obvious_failure_overlays(item, display=display)
            ],
            "hypotheses": [
                item.get("hypothesis") for item in signals[-OBVIOUS_FAILURE_OVERLAY_SIGNAL_LIMIT:]
            ],
            "latest": {
                "ts": bars[-1].ts.isoformat(),
                "state": "WAIT",
                "score": 0.0,
                "signal": SignalState(
                    "obvious_failure",
                    ActionPhase.WAIT,
                    Direction.FLAT,
                    0.0,
                    source_tf=bars[-1].timeframe,
                ).as_dict(),
            },
        }

    recent_signals = signals[-160:]
    current_signal = _obvious_failure_signal_item(current)
    return {
        "version": OBVIOUS_FAILURE_VERSION,
        "series": recent_signals,
        "events": [_obvious_failure_event(item) for item in recent_signals],
        "signals": [current_signal] if current_signal is not None else [],
        "latest": current,
        "overlays": [
            overlay
            for item in signals[-OBVIOUS_FAILURE_OVERLAY_SIGNAL_LIMIT:]
            for overlay in _obvious_failure_overlays(
                item,
                display=display,
                retention="active" if item is current else "history",
            )
        ],
        "hypotheses": [
            item.get("hypothesis") for item in signals[-OBVIOUS_FAILURE_OVERLAY_SIGNAL_LIMIT:]
        ],
    }
