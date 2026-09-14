from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.gex.option_target_contract import (
    OPTION_TARGET_CURRENT_QUOTE_PRICE_SOURCES,
    option_target_finite_number,
    option_target_market_sample,
    option_target_positive_number,
)
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.domain import Bar
from aef_terminal.indicators.control_specs import indicator_table_controls
from aef_terminal.indicators.defaults import IndicatorDefaults
from aef_terminal.indicators.domain_facts import copy_indicator_facts, indicator_fact_payload
from aef_terminal.indicators.module_contract import (
    INDICATOR_FACT_RUNTIME_FIELDS,
    ConfirmedBarContextRequest,
    IndicatorModule,
    IndicatorSpec,
)
from aef_terminal.indicators.runtime import (
    IndicatorExecutionSpec,
    IndicatorRunContext,
)
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import clamp
from aef_terminal.runtime.timeframes import interval_minutes


OPTION_REVERSAL_VERSION = "0.2-python"
OPTION_REVERSAL_BAR_TIMEFRAME = "1m"
OPTION_REVERSAL_HISTORY_BARS = 256
OPTION_REVERSAL_RESEARCH_OBSERVATION_SCHEMA_VERSION = 2
OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS = (
    "compression",
    "lag_score",
    "favorable_move_atr",
    "near_ratio",
    "spread_ratio",
    "option_price",
    "reference_price",
    "previous_option",
    "previous_underlying",
    "atr",
    "underlying_price",
    "target_price",
    "bid",
    "ask",
    "compression_score",
    "reaction_score",
    "premium_lag_score",
    "premium_change_ratio",
    "near_score",
    "edge_score",
    "raw_score",
    "score",
)

INDICATOR_MODULE = IndicatorModule(
    IndicatorSpec(
        "option_reversal",
        "Option Reversal",
        "option premium compression near chart option points",
        {
            "version": OPTION_REVERSAL_VERSION,
            "series": [],
            "events": [],
            "latest": None,
            "table": None,
            "overlays": [],
        },
        "optionReversal",
        "aef_terminal.indicators.modules.option_reversal:option_reversal",
        group="post_decision",
        pipeline_stage="post_decision",
        pipeline_order=130,
        execution_lanes=("primary", "fast"),
        fast_trigger_refs=("option_target_sample",),
        candidate_promoter="none",
        paper_tradable=False,
        score_family="options",
        empirical_power=0.90,
        usefulness=0.85,
        shared_context_refs=("option_targets",),
        module_type="context",
        state_key="optionReversal",
        calc_key="optionReversalCalcEnabled",
        visible_key="optionReversalVisible",
        chart_control_id="option-reversal-toggle",
        process_control_id="option-reversal-process",
        derived_state_refs=("table_position_state",),
        table_setting_id="option-reversal-table-position",
        api_enabled_key="option_reversal_enabled",
        manager_order=130,
        default_calc=False,
        table_contract="indicator-table-v1",
        renderer_primitives=("box", "table"),
        renderer_placements=("price", "table"),
        renderer_table_label="Option Reversal",
        renderer_table_header_width=26,
        overlay_layer="foreground",
        show_in_runtime=False,
        runtime_payload_contract={
            "top_level": ("research_observation", "settings", "targets"),
            "latest": (
                "action",
                "direction",
                "metrics",
                "monte_carlo_bridge",
                "option_side",
                "reason_code",
                "requirements",
                "score",
                "signal",
                "state",
                "target",
                "ts",
                *INDICATOR_FACT_RUNTIME_FIELDS,
            ),
            "events": (
                "action",
                "code",
                "details",
                "direction",
                "event_type",
                "id",
                "level",
                "option_side",
                "overlays",
                "price",
                "reason_code",
                "score",
                "source",
                "ts",
                *INDICATOR_FACT_RUNTIME_FIELDS,
            ),
            "overlays": ("option_side",),
        },
        runtime_payload_compact={
            "research_observation": {
                "drop": True,
            },
        },
        runtime_payload_schema_refs=("overlay_contract",),
        confirmed_bar_context=(
            ConfirmedBarContextRequest(
                timeframe=OPTION_REVERSAL_BAR_TIMEFRAME,
                history_bars=OPTION_REVERSAL_HISTORY_BARS,
                role="lower_timeframe_confirmation",
            ),
        ),
        controls=(*indicator_table_controls("optionReversal", "option-reversal"),),
    ),
    adapter_ref="aef_terminal.indicators.modules.option_reversal:build_execution_spec",
    params_builder_ref="aef_terminal.indicators.modules.option_reversal:build_params",
    ui_js_assets=("client.js",),
)


def build_execution_spec(ctx: IndicatorRunContext) -> IndicatorExecutionSpec:
    calculate = option_reversal
    params = ctx.params("option_reversal")
    parent_bars = tuple(ctx.confirmed_bars)
    bars = (
        parent_bars
        if parent_bars
        and all(bar.timeframe == OPTION_REVERSAL_BAR_TIMEFRAME for bar in parent_bars)
        else tuple(ctx.confirmed_bar_context.get(OPTION_REVERSAL_BAR_TIMEFRAME, ()))
    )
    latest = bars[-1] if bars else None
    return IndicatorExecutionSpec(
        id="option_reversal",
        input_bars=bars,
        analysis_bar=latest,
        mode="confirmed",
        calculate=lambda: calculate(
            bars,
            option_targets=ctx.option_targets or [],
            params=params,
            analysis_as_of_utc=ctx.analysis_as_of_utc,
        ),
        params=params,
        runtime_params=ctx.runtime_params,
        promote=False,
    )


@dataclass(frozen=True)
class OptionReversalParams:
    atr_length: int = 14
    near_atr: float = 0.45
    compression_ratio: float = 0.82
    compression_neutral_ratio: float = 1.06
    reaction_min_atr: float = 0.12
    premium_lag_max_change: float = 0.03
    wide_spread_ratio: float = 0.35


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> OptionReversalParams:
    return OptionReversalParams(
        atr_length=defaults.atr_len,
        near_atr=float(raw.get("near_atr", OptionReversalParams.near_atr)),
        compression_ratio=float(
            raw.get(
                "compression_ratio",
                OptionReversalParams.compression_ratio,
            )
        ),
        compression_neutral_ratio=float(
            raw.get(
                "compression_neutral_ratio",
                OptionReversalParams.compression_neutral_ratio,
            )
        ),
        wide_spread_ratio=float(
            raw.get(
                "wide_spread_ratio",
                OptionReversalParams.wide_spread_ratio,
            )
        ),
    )


def _empty(
    state: str = "DISABLED",
    reason_code: str = "",
    *,
    option_target_ready: bool = False,
) -> dict[str, Any]:
    latest = None
    if state != "DISABLED":
        latest = {
            "state": state,
            "action": "WAIT",
            "score": 0.0,
            "reason_code": reason_code,
            "requirements": {
                "option_target": option_target_ready,
                "live_option": False,
            },
            **indicator_fact_payload(
                scenario="option_reversal",
                trigger_event={"code": reason_code},
                quality={"code": "score"},
            ),
        }
    table = (
        {
            "model_ref": "option_reversal",
            "tooltip_mode": "details",
            "state": latest["state"],
            "action": latest["action"],
            "score": latest["score"],
            "reason_code": latest["reason_code"],
            **copy_indicator_facts(latest),
        }
        if latest is not None
        else None
    )
    overlays = (
        [
            {
                "type": "table",
                "source": "option_reversal",
                "control_key": "table",
                "table": table,
                "tone": "warning",
                **copy_indicator_facts(latest),
            }
        ]
        if table is not None
        else []
    )
    return {
        "version": OPTION_REVERSAL_VERSION,
        "series": [],
        "events": [],
        "latest": latest,
        "table": table,
        "overlays": overlays,
    }


def _target_payload(
    item: dict[str, Any],
    *,
    analysis_as_of_utc: datetime,
) -> dict[str, Any]:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return {}
    intent = payload.get("intent")
    market_sample = payload.get("market_sample")
    point = item.get("point")
    if (
        not isinstance(intent, dict)
        or not isinstance(market_sample, dict)
        or not isinstance(point, dict)
    ):
        return {}
    try:
        typed_market_sample = option_target_market_sample(
            market_sample,
            analysis_as_of_utc=analysis_as_of_utc,
            sec_type=str(intent.get("sec_type") or ""),
        )
    except ValueError:
        return {}
    row_metadata = {
        key: item[key]
        for key in (
            "id",
            "symbol",
            "timeframe",
            "instrument_id",
            "route_fingerprint",
        )
        if key in item
    }
    return {
        **intent,
        **typed_market_sample,
        **row_metadata,
        "point": dict(point),
    }


def _wait_live_reason(target_rows: Sequence[dict[str, Any]]) -> str:
    for item in target_rows:
        container = item.get("payload")
        sample = container.get("market_sample") if isinstance(container, dict) else None
        if not isinstance(sample, dict) or not sample:
            continue
        option_status = str(sample.get("live_quote_status") or "").lower()
        underlying_status = str(sample.get("underlying_quote_status") or "").lower()
        if option_status in {"", "pending"} or underlying_status in {"", "pending"}:
            continue
        return "option_market_data_stale"
    return "option_market_data_pending"


def _live_target_sample(
    payload: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    if (
        payload.get("live_quote_status") != "ok"
        or payload.get("live_quote_entitlement") != "live"
        or payload.get("underlying_quote_status") != "live"
        or payload.get("underlying_quote_entitlement") != "live"
    ):
        return None
    option_price = payload.get("reference_option_price")
    bid = payload.get("live_bid")
    ask = payload.get("live_ask")
    underlying_price = payload.get("live_underlying_price")
    if payload.get(
        "reference_option_price_source"
    ) not in OPTION_TARGET_CURRENT_QUOTE_PRICE_SOURCES or not all(
        isinstance(value, float) for value in (option_price, bid, ask, underlying_price)
    ):
        return None
    return option_price, bid, ask, underlying_price


def _reference_price(payload: dict[str, Any], option_price: float) -> float:
    for key in ("fair_price", "max_limit_price", "reference_option_price"):
        value = option_target_positive_number(payload.get(key))
        if value is not None:
            return value
    return option_price


def _previous_option_price(payload: dict[str, Any]) -> float | None:
    return option_target_positive_number(payload.get("previous_reference_option_price"))


def _previous_underlying_price(payload: dict[str, Any]) -> float | None:
    return option_target_finite_number(payload.get("previous_underlying_price"))


def _target_price(payload: dict[str, Any]) -> float | None:
    point = payload.get("point") if isinstance(payload.get("point"), dict) else {}
    return option_target_finite_number(point.get("price"))


def _target_rank(payload: dict[str, Any], underlying_price: float) -> tuple[float, str]:
    target = _target_price(payload)
    distance = abs(underlying_price - target) if target is not None else float("inf")
    return distance, str(payload.get("id") or "")


def _normalized_timestamp(value: object) -> str | None:
    parsed = parse_gex_timestamp(value)
    return parsed.astimezone(UTC).isoformat() if parsed is not None else None


def _optional_finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _research_observation(
    *,
    latest_bar: Bar,
    target: dict[str, Any],
    params: OptionReversalParams,
    action: str,
    trigger_code: str,
    preferred: str,
    price_source: str,
    features: Mapping[str, float | None],
) -> dict[str, Any] | None:
    row = target.get("_row")
    option_target_id = row.get("id") if isinstance(row, dict) else None
    option_contract_id = target.get("con_id")
    option_quote_ts = _normalized_timestamp(target.get("live_quote_ts"))
    underlying_quote_ts = _normalized_timestamp(target.get("underlying_quote_ts"))
    if (
        not isinstance(option_target_id, str)
        or not option_target_id
        or isinstance(option_contract_id, bool)
        or not isinstance(option_contract_id, (str, int))
        or not str(option_contract_id)
        or option_quote_ts is None
        or underlying_quote_ts is None
    ):
        return None

    compression_sample_at = _normalized_timestamp(target.get("compression_sample_at"))
    observation_ts = compression_sample_at or max(
        option_quote_ts,
        underlying_quote_ts,
    )
    target_delta = _optional_finite_float(target.get("target_delta"))
    estimated_greeks = target.get("estimated_greeks")
    if not isinstance(estimated_greeks, bool):
        estimated_greeks = None
    strike = _optional_finite_float(target.get("strike"))

    return {
        "schema_version": OPTION_REVERSAL_RESEARCH_OBSERVATION_SCHEMA_VERSION,
        "decision_ts": latest_bar.ts.isoformat(),
        "action_taken": action,
        "trigger_code": trigger_code,
        "price_source": price_source,
        "contract": {
            "option_target_id": option_target_id,
            "option_contract_id": str(option_contract_id),
            "sec_type": str(target.get("sec_type") or ""),
            "right": preferred,
            "expiry": str(target.get("expiry") or ""),
            "strike": strike,
            "target_delta": target_delta,
            "estimated_greeks": estimated_greeks,
        },
        "sample": {
            "observation_ts": observation_ts,
            "option_quote_ts": option_quote_ts,
            "option_quote_time_basis": target.get("live_quote_time_basis"),
            "underlying_quote_ts": underlying_quote_ts,
            "underlying_quote_time_basis": target.get("underlying_quote_time_basis"),
            "previous_option_quote_ts": _normalized_timestamp(target.get("previous_live_quote_ts")),
            "previous_underlying_quote_ts": _normalized_timestamp(
                target.get("previous_underlying_quote_ts")
            ),
            "compression_sample_at": compression_sample_at,
        },
        "features": dict(features),
        "feature_availability": {
            "previous_pair": option_target_positive_number(
                target.get("previous_reference_option_price")
            )
            is not None
            and option_target_finite_number(target.get("previous_underlying_price")) is not None,
            "bid_ask": features["ask"] > 0 and features["bid"] > 0,
            "target_delta": target_delta is not None,
        },
        "parameters": {
            "near_atr": float(params.near_atr),
            "compression_ratio": float(params.compression_ratio),
            "compression_neutral_ratio": float(params.compression_neutral_ratio),
            "reaction_min_atr": float(params.reaction_min_atr),
            "premium_lag_max_change": float(params.premium_lag_max_change),
            "wide_spread_ratio": float(params.wide_spread_ratio),
        },
    }


def _option_reversal_signal(
    latest: dict[str, Any], overlays: list[dict[str, Any]]
) -> dict[str, Any]:
    target = latest.get("target") if isinstance(latest.get("target"), dict) else {}
    metrics = latest.get("metrics") if isinstance(latest.get("metrics"), dict) else {}
    action = str(latest.get("action") or "BUY_OPTION").upper()
    ts = str(latest.get("ts") or "")
    target_id = str(target.get("id") or "target")
    return {
        "id": f"option-reversal-{ts}-{target_id}-{action.lower()}",
        "ts": ts,
        "source": "option_reversal",
        "event_type": "option_reversal",
        "code": f"OPTION_REVERSAL_{action}",
        "action": action,
        "direction": latest.get("direction") or "flat",
        "option_side": latest.get("option_side"),
        "score": latest.get("score"),
        "price": target.get("target_price"),
        "level": target.get("target_price"),
        "reason_code": latest.get("reason_code"),
        "details": {
            "target": target,
            "metrics": metrics,
            "monte_carlo_bridge": latest.get("monte_carlo_bridge"),
        },
        "overlays": overlays,
        **copy_indicator_facts(latest),
    }


def option_reversal(
    bars: Sequence[Bar],
    *,
    option_targets: Sequence[dict[str, Any]] | None = None,
    params: OptionReversalParams | None = None,
    analysis_as_of_utc: datetime | None = None,
) -> dict[str, Any]:
    params = params or OptionReversalParams()
    exact_bars = tuple(bars)
    if not exact_bars:
        return _empty("WAIT_DATA", "bars_unavailable")
    if any(
        bar.closed is not True or bar.timeframe != OPTION_REVERSAL_BAR_TIMEFRAME
        for bar in exact_bars
    ):
        raise ValueError("Option Reversal requires provider-confirmed closed 1m bars")
    latest_bar = exact_bars[-1]
    resolved_analysis_as_of = analysis_as_of_utc
    if resolved_analysis_as_of is None:
        resolved_analysis_as_of = latest_bar.ts + timedelta(
            minutes=max(interval_minutes(latest_bar.timeframe), 1)
        )
    if (
        not isinstance(resolved_analysis_as_of, datetime)
        or resolved_analysis_as_of.tzinfo is None
        or resolved_analysis_as_of.utcoffset() is None
    ):
        raise ValueError("analysis_as_of_utc must be timezone-aware")
    resolved_analysis_as_of = resolved_analysis_as_of.astimezone(UTC)

    target_rows = [item for item in option_targets or [] if isinstance(item, dict)]
    if not target_rows:
        return _empty("WAIT_TARGET", "option_target_required")
    if not any(_target_price(item) is not None for item in target_rows):
        return {
            **_empty("WAIT_TARGET", "option_target_price_required"),
            "targets": len(target_rows),
        }
    targets = []
    for item in target_rows:
        payload = _target_payload(
            item,
            analysis_as_of_utc=resolved_analysis_as_of,
        )
        if payload:
            targets.append(payload | {"_row": item})
    if not targets:
        return {
            **_empty(
                "WAIT_LIVE",
                _wait_live_reason(target_rows),
                option_target_ready=True,
            ),
            "targets": len(target_rows),
        }

    targets_with_points = [target for target in targets if _target_price(target) is not None]
    if not targets_with_points:
        return {
            **_empty("WAIT_TARGET", "option_target_price_required"),
            "targets": len(target_rows),
        }
    live_targets: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for candidate in targets_with_points:
        live_sample = _live_target_sample(candidate)
        if live_sample is not None:
            live_targets.append((candidate, live_sample))
    if not live_targets:
        return {
            **_empty(
                "WAIT_LIVE",
                _wait_live_reason(target_rows),
                option_target_ready=True,
            ),
            "targets": len(target_rows),
        }
    target, live_sample = min(
        live_targets,
        key=lambda item: _target_rank(item[0], item[1][3]),
    )
    option_price, bid, ask, latest_price = live_sample
    target_level = _target_price(target)
    if target_level is None:
        raise RuntimeError("ranked Option Reversal target lost its exact point price")
    price_source = "bid_ask_mid"

    reference = _reference_price(target, option_price)
    compression = option_price / reference
    spread_ratio = (ask - bid) / option_price
    atr_values = pine.atr_rma_series(exact_bars, params.atr_length)
    atr = option_target_positive_number(atr_values[-1] if atr_values else None)
    if atr is None:
        return {
            **_empty("WAIT_DATA", "atr_unavailable"),
            "targets": len(target_rows),
        }
    near_ratio = abs(latest_price - target_level) / atr
    near_score = max(0.0, 1.0 - min(near_ratio / max(params.near_atr, 0.01), 1.0))
    compression_neutral = max(params.compression_neutral_ratio, params.compression_ratio + 0.01)
    compression_score = max(
        0.0,
        min(
            (compression_neutral - compression)
            / max(compression_neutral - params.compression_ratio, 0.01),
            1.0,
        ),
    )
    right = str(target.get("right") or "")
    preferred = "CALL" if right == "C" else "PUT" if right == "P" else "OPTION"
    previous_underlying = _previous_underlying_price(target)
    previous_option = _previous_option_price(target)
    previous_pair_available = previous_underlying is not None and previous_option is not None
    if previous_pair_available and preferred == "PUT":
        favorable_move = previous_underlying - latest_price
    elif previous_pair_available and preferred == "CALL":
        favorable_move = latest_price - previous_underlying
    else:
        favorable_move = 0.0
    favorable_move_atr = favorable_move / atr
    premium_change_ratio = (
        (option_price - previous_option) / previous_option if previous_option is not None else None
    )
    reaction_score = max(0.0, min(favorable_move_atr / max(params.reaction_min_atr, 0.01), 1.0))
    if premium_change_ratio is not None and premium_change_ratio <= params.premium_lag_max_change:
        premium_lag_score = 1.0
    elif premium_change_ratio is not None:
        premium_lag_score = max(
            0.0, min(1.0 - (premium_change_ratio - params.premium_lag_max_change) / 0.12, 1.0)
        )
    else:
        premium_lag_score = 0.0
    lag_score = reaction_score * premium_lag_score if previous_pair_available else 0.0
    spread_modifier = 0.80 if spread_ratio >= params.wide_spread_ratio else 1.0
    edge_score = max(compression_score, lag_score)
    raw_score = 99.0 * near_score * edge_score * spread_modifier
    score = clamp(raw_score, 0.0, 99.0)

    has_compression_edge = compression <= params.compression_ratio
    has_premium_lag_edge = lag_score >= 0.65 and favorable_move_atr >= params.reaction_min_atr
    if (
        score >= 78.0
        and (has_compression_edge or has_premium_lag_edge)
        and near_ratio <= params.near_atr
    ):
        action = f"BUY_{preferred}"
        state = "BUY"
        if has_premium_lag_edge and not has_compression_edge:
            trigger_code = "premium_lag_entry_candidate"
        else:
            trigger_code = "premium_compression_entry_candidate"
    elif score >= 64.0:
        action = "WATCH"
        state = "WATCH"
        trigger_code = "option_edge_confirmation_incomplete"
    elif compression >= 1.12:
        action = "SELL_TAKE"
        state = "SELL"
        trigger_code = "premium_expanded_reduce"
    else:
        action = "WAIT"
        state = "WAIT"
        trigger_code = "option_edge_absent"

    is_put_squeeze = (
        preferred == "PUT" and score >= 75.0 and compression <= params.compression_ratio
    )
    monte_carlo_bridge = {
        "trigger_simulation": bool(
            score >= 70.0
            and (has_compression_edge or has_premium_lag_edge)
            and near_ratio <= params.near_atr
        ),
        "recommended_engine": "gbm",
        "bias_settings": {
            "pine_bias": -0.35
            if (is_put_squeeze or (preferred == "PUT" and has_premium_lag_edge))
            else 0.35
            if preferred == "CALL" and score >= 75.0
            else 0.0,
            "vol_multiplier": 1.45
            if (is_put_squeeze or (preferred == "PUT" and has_premium_lag_edge))
            else 1.10
            if score >= 75.0
            else 1.0,
            "target_attractor": round(target_level, 4),
        },
        "status": "advisory_only",
    }
    latest = {
        "ts": latest_bar.ts.isoformat(),
        "state": state,
        "action": action,
        "score": round(score, 2),
        "direction": "long" if preferred == "CALL" else "short" if preferred == "PUT" else "flat",
        "option_side": preferred,
        "reason_code": trigger_code,
        "requirements": {"option_target": True, "live_option": price_source != "missing"},
        "monte_carlo_bridge": monte_carlo_bridge,
        "target": {
            "id": str(target.get("id") or ""),
            "symbol": str(target.get("symbol") or latest_bar.symbol),
            "timeframe": str(target.get("timeframe") or latest_bar.timeframe),
            "right": preferred,
            "strike": target.get("strike"),
            "expiry": target.get("expiry"),
            "local_symbol": target.get("local_symbol"),
            "con_id": target.get("con_id"),
            "target_price": round(target_level, 4),
        },
        "metrics": {
            "score": round(score, 4),
            "option_price": round(option_price, 4),
            "price_source": price_source,
            "reference_price": round(reference, 4),
            "compression_ratio": round(compression, 4),
            "compression_neutral_ratio": round(compression_neutral, 4),
            "compression_score": round(compression_score, 4),
            "previous_underlying_price": (
                round(previous_underlying, 4) if previous_underlying is not None else None
            ),
            "previous_option_price": (
                round(previous_option, 4) if previous_option is not None else None
            ),
            "favorable_move_atr": round(favorable_move_atr, 4),
            "premium_change_ratio": (
                round(premium_change_ratio, 4) if premium_change_ratio is not None else None
            ),
            "lag_score": round(lag_score, 4),
            "premium_lag_edge": bool(has_premium_lag_edge),
            "preferred": preferred,
            "spread_ratio": round(spread_ratio, 4),
            "near_score": round(near_score, 4),
            "near_atr": round(near_ratio, 4),
        },
        **indicator_fact_payload(
            scenario="option_reversal",
            trigger_event={"code": trigger_code},
            quality={"code": "score"},
        ),
    }
    table = {
        "model_ref": "option_reversal",
        "tooltip_mode": "details",
        "state": latest["state"],
        "action": latest["action"],
        "score": latest["score"],
        "option_side": latest["option_side"],
        "direction": latest["direction"],
        "reason_code": latest["reason_code"],
        "target": latest["target"],
        **copy_indicator_facts(latest),
    }
    overlays: list[dict[str, Any]] = []
    if state in {"BUY", "WATCH"} and edge_score > 0:
        start_bar = bars[-3] if len(bars) >= 3 else bars[0]
        pad = max(atr * 0.32, abs(latest_price) * 0.00035)
        overlays.append(
            {
                "type": "box",
                "source": "option_reversal",
                "role": "option_reaction_zone",
                "start_ts": start_bar.ts.isoformat(),
                "end_ts": latest_bar.ts.isoformat(),
                "top": round(latest_price + pad, 6),
                "bottom": round(latest_price - pad, 6),
                "tone": "positive" if preferred == "CALL" else "negative",
                "label": action,
                "style": "dashed",
                "width": 1.6,
                "opacity": 0.0,
                "border_opacity": 0.84,
                "animated_border": True,
                "direction": latest["direction"],
                "option_side": latest["option_side"],
                **copy_indicator_facts(latest),
            }
        )
    current_signal = _option_reversal_signal(latest, list(overlays)) if state == "BUY" else None
    if current_signal is not None:
        latest["signal"] = current_signal
    overlays.append(
        {
            "type": "table",
            "source": "option_reversal",
            "control_key": "table",
            "table": table,
            "tone": "positive" if state == "BUY" else "warning",
            "direction": latest["direction"],
            "option_side": latest["option_side"],
            **copy_indicator_facts(latest),
        }
    )
    result = {
        "version": OPTION_REVERSAL_VERSION,
        "settings": {
            "enabled": True,
            "near_atr": params.near_atr,
            "compression_ratio": params.compression_ratio,
            "compression_neutral_ratio": params.compression_neutral_ratio,
            "reaction_min_atr": params.reaction_min_atr,
            "premium_lag_max_change": params.premium_lag_max_change,
            "wide_spread_ratio": params.wide_spread_ratio,
        },
        "series": [],
        "events": [current_signal] if current_signal is not None else [],
        "latest": latest,
        "table": table,
        "overlays": overlays,
        "signals": [],
    }
    research_observation = _research_observation(
        latest_bar=latest_bar,
        target=target,
        params=params,
        action=action,
        trigger_code=trigger_code,
        preferred=preferred,
        price_source=price_source,
        features={
            "compression": float(compression),
            "lag_score": float(lag_score),
            "favorable_move_atr": float(favorable_move_atr),
            "near_ratio": float(near_ratio),
            "spread_ratio": float(spread_ratio),
            "option_price": float(option_price),
            "reference_price": float(reference),
            "previous_option": (float(previous_option) if previous_option is not None else None),
            "previous_underlying": (
                float(previous_underlying) if previous_underlying is not None else None
            ),
            "atr": float(atr),
            "underlying_price": float(latest_price),
            "target_price": float(target_level),
            "bid": float(bid),
            "ask": float(ask),
            "compression_score": float(compression_score),
            "reaction_score": float(reaction_score),
            "premium_lag_score": float(premium_lag_score),
            "premium_change_ratio": (
                float(premium_change_ratio) if premium_change_ratio is not None else None
            ),
            "near_score": float(near_score),
            "edge_score": float(edge_score),
            "raw_score": float(raw_score),
            "score": float(score),
        },
    )
    if research_observation is not None:
        result["research_observation"] = research_observation
    return result
