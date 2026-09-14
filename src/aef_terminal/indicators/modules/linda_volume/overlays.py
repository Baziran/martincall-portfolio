from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.market_context import true_count
from aef_terminal.indicators.domain_facts import indicator_fact_payload, metric_number
from aef_terminal.runtime import overlays as runtime_overlays
from aef_terminal.runtime.math_utils import round_optional

from .params import LindaGrailTuning, LindaVolumeParams
from .plan import vwap_signal_overlay


def build_poi_overlays(
    *,
    bars: Sequence[Bar],
    latest_bar: Bar,
    poi_context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if (
        not poi_context.get("active")
        or poi_context.get("core_top") is None
        or poi_context.get("core_bottom") is None
    ):
        return []
    poi_dir = str(poi_context.get("direction") or "flat")
    poi_long = poi_dir == "long"
    poi_core_top = float(poi_context["core_top"])
    poi_core_bottom = float(poi_context["core_bottom"])
    poi_deep_top = float(poi_context["deep_top"] or poi_core_top)
    poi_deep_bottom = float(poi_context["deep_bottom"] or poi_core_bottom)
    poi_start = bars[int(poi_context.get("source_index") or len(bars) - 1)]
    poi_projection_bars = max(
        5,
        int(poi_context.get("expire_bars") or 30) - int(poi_context.get("age_bars") or 0),
    )
    poi_fact_fields = indicator_fact_payload(
        scenario="linda_poi_waiting_zone",
        setup="impulse_pullback_long" if poi_long else "impulse_pullback_short",
        quality={"code": "poi_hot" if poi_context.get("hot") else "poi_waiting"},
        trigger_event={"code": "poi_vsa_reclaim_or_reject", "direction": poi_dir},
        risk={"code": "exit_beyond_deep_fib", "fib_ratio": 0.786},
        metrics={
            "core_bottom": metric_number(poi_core_bottom),
            "core_top": metric_number(poi_core_top),
            "deep_bottom": metric_number(poi_deep_bottom),
            "deep_top": metric_number(poi_deep_top),
        },
    )
    return [
        {
            "type": "box",
            "start_ts": poi_start.ts.isoformat(),
            **runtime_overlays.projected_end(latest_bar, poi_projection_bars),
            "top": round_optional(poi_core_top),
            "bottom": round_optional(poi_core_bottom),
            "source": "linda_volume",
            "retention": "active",
            "direction": poi_dir,
            "role": "linda_poi_core",
            **poi_fact_fields,
        },
        {
            "type": "box",
            "start_ts": poi_start.ts.isoformat(),
            **runtime_overlays.projected_end(latest_bar, poi_projection_bars),
            "top": round_optional(poi_deep_top),
            "bottom": round_optional(poi_deep_bottom),
            "source": "linda_volume",
            "retention": "active",
            "direction": poi_dir,
            "role": "linda_poi_deep",
            "tone": "warning",
            **poi_fact_fields,
        },
        {
            **runtime_overlays.label(
                bar=latest_bar,
                price=poi_core_top if poi_long else poi_core_bottom,
                lines=[],
                direction=poi_dir,
                side="below" if poi_long else "above",
                role="linda_poi_label",
                fact_fields=poi_fact_fields,
            ),
            "retention": "active",
            "code": "POI_LONG" if poi_long else "POI_SHORT",
            "action": "WATCH",
        },
    ]


@dataclass(frozen=True)
class LindaGrailOverlayInputs:
    latest_bar: Bar
    latest_item: Mapping[str, Any]
    params: LindaVolumeParams
    tuning: LindaGrailTuning
    latest_ema20: float
    is_buy: bool
    is_sell: bool
    buy_developing: bool
    sell_developing: bool
    buy_stop: float
    buy_target: float
    buy_rr: float
    sell_stop: float
    sell_target: float
    sell_rr: float
    buy_blockers: Sequence[str]
    sell_blockers: Sequence[str]


def build_grail_overlays(inputs: LindaGrailOverlayInputs) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if inputs.is_buy:
        grail_label = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.low,
            lines=[],
            direction="long",
            side="below",
            fact_fields=indicator_fact_payload(
                scenario="linda_grail",
                setup="continuation_long",
                context={"code": "ema20_retest"},
                trigger_event={"code": "grail_level_hold", "direction": "long"},
                risk={"code": "stop_loss", "price": inputs.buy_stop},
                quality={"code": "risk_reward", "value": inputs.buy_rr},
                metrics={
                    "ema20": metric_number(inputs.latest_ema20),
                    "stop": metric_number(inputs.buy_stop),
                    "target": metric_number(inputs.buy_target),
                    "price": metric_number(inputs.latest_bar.close),
                    "rr": metric_number(inputs.buy_rr),
                },
            ),
            role="linda_grail_buy",
        )
        grail_label["code"] = "GRAIL_BUY"
        grail_label["action"] = "GO"
        grail_label["raw_action"] = "GO"
        grail_label["overlay_role"] = "grail"
        runtime_overlays.bind_signal_trade_plan(
            grail_label,
            {
                "source": "linda_volume",
                "action": "GO",
                "raw_action": "GO",
                "direction": "long",
                "trigger": inputs.latest_ema20,
                "stop": inputs.buy_stop,
                "target": inputs.buy_target,
                "score": inputs.latest_item.get("score"),
                "code": "GRAIL_BUY",
                "reason_code": "grail_long_continuation",
            },
            source="linda_volume",
        )
        items.append(grail_label)
    elif inputs.params.enable_grail and inputs.buy_developing:
        grail_watch = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.low,
            lines=[],
            direction="long",
            side="below",
            fact_fields=indicator_fact_payload(
                scenario="linda_grail_watch",
                setup="ema20_retest_forming",
                trigger_event={"code": "waiting_close_confirmation", "direction": "long"},
                quality={"code": "risk_reward_pending", "value": inputs.buy_rr},
                opposing={"code": "grail_blocked"} if inputs.buy_blockers else None,
                fact_groups=[
                    {
                        "kind": "grail_blockers",
                        "items": [
                            {"code": "grail_blocker", "content": str(item)}
                            for item in inputs.buy_blockers
                        ],
                    }
                ],
                metrics={
                    "rr": metric_number(inputs.buy_rr),
                    "min_rr": metric_number(inputs.tuning.grail_min_rr),
                },
            ),
            role="linda_grail_buy_watch",
        )
        grail_watch["code"] = "GRAIL_BUY_WATCH"
        grail_watch["action"] = "WATCH"
        grail_watch["raw_action"] = "WATCH"
        grail_watch["overlay_role"] = "grail"
        items.append(grail_watch)
    if inputs.is_sell:
        grail_label = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.high,
            lines=[],
            direction="short",
            side="above",
            fact_fields=indicator_fact_payload(
                scenario="linda_grail",
                setup="continuation_short",
                context={"code": "ema20_retest"},
                trigger_event={"code": "grail_level_hold", "direction": "short"},
                risk={"code": "stop_loss", "price": inputs.sell_stop},
                quality={"code": "risk_reward", "value": inputs.sell_rr},
                metrics={
                    "ema20": metric_number(inputs.latest_ema20),
                    "stop": metric_number(inputs.sell_stop),
                    "target": metric_number(inputs.sell_target),
                    "price": metric_number(inputs.latest_bar.close),
                    "rr": metric_number(inputs.sell_rr),
                },
            ),
            role="linda_grail_sell",
        )
        grail_label["code"] = "GRAIL_SELL"
        grail_label["action"] = "GO"
        grail_label["raw_action"] = "GO"
        grail_label["overlay_role"] = "grail"
        runtime_overlays.bind_signal_trade_plan(
            grail_label,
            {
                "source": "linda_volume",
                "action": "GO",
                "raw_action": "GO",
                "direction": "short",
                "trigger": inputs.latest_ema20,
                "stop": inputs.sell_stop,
                "target": inputs.sell_target,
                "score": inputs.latest_item.get("score"),
                "code": "GRAIL_SELL",
                "reason_code": "grail_short_continuation",
            },
            source="linda_volume",
        )
        items.append(grail_label)
    elif inputs.params.enable_grail and inputs.sell_developing:
        grail_watch = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.high,
            lines=[],
            direction="short",
            side="above",
            fact_fields=indicator_fact_payload(
                scenario="linda_grail_watch",
                setup="ema20_retest_forming",
                trigger_event={"code": "waiting_close_confirmation", "direction": "short"},
                quality={"code": "risk_reward_pending", "value": inputs.sell_rr},
                opposing={"code": "grail_blocked"} if inputs.sell_blockers else None,
                fact_groups=[
                    {
                        "kind": "grail_blockers",
                        "items": [
                            {"code": "grail_blocker", "content": str(item)}
                            for item in inputs.sell_blockers
                        ],
                    }
                ],
                metrics={
                    "rr": metric_number(inputs.sell_rr),
                    "min_rr": metric_number(inputs.tuning.grail_min_rr),
                },
            ),
            role="linda_grail_sell_watch",
        )
        grail_watch["code"] = "GRAIL_SELL_WATCH"
        grail_watch["action"] = "WATCH"
        grail_watch["raw_action"] = "WATCH"
        grail_watch["overlay_role"] = "grail"
        items.append(grail_watch)
    return items


@dataclass(frozen=True)
class LindaExhaustionOverlayInputs:
    latest_bar: Bar
    poi_context: Mapping[str, Any]
    latest_ema20: float
    latest_vwap: float
    latest_rvol: float
    prev_rvol: float
    prev2_rvol: float
    opp_exh_long: bool
    opp_exh_short: bool
    clx_ct_long: bool
    clx_ct_short: bool
    bull_candidate: bool
    bear_candidate: bool
    bull_score: int
    bear_score: int
    upper_volume_exhaustion: bool
    upper_location_exhaustion: bool
    lower_location_exhaustion: bool
    reversal_warning: bool
    trend_day_bull: bool
    full_bull_now: bool


def build_exhaustion_overlays(
    inputs: LindaExhaustionOverlayInputs,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if inputs.opp_exh_long or inputs.opp_exh_short:
        sm_long = bool(inputs.opp_exh_long)
        sm_label = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.low if sm_long else inputs.latest_bar.high,
            lines=[],
            direction="long" if sm_long else "short",
            side="below" if sm_long else "above",
            fact_fields=indicator_fact_payload(
                scenario="smart_money_reversal",
                setup="strong_impulse_weak_opposing_pullback",
                trigger_event={
                    "code": "close_or_break_above_high" if sm_long else "close_or_break_below_low"
                },
                risk={
                    "code": "signal_extreme_invalidation",
                    "price": inputs.latest_bar.low if sm_long else inputs.latest_bar.high,
                },
                metrics={
                    "poi_zone": (
                        "deep_0.618_0.786"
                        if inputs.poi_context.get("in_deep")
                        else "core_0.5_0.618"
                    ),
                    "trigger_level": metric_number(
                        inputs.latest_bar.high if sm_long else inputs.latest_bar.low
                    ),
                    "invalid_level": metric_number(
                        inputs.latest_bar.low if sm_long else inputs.latest_bar.high
                    ),
                    "price": metric_number(inputs.latest_bar.close),
                    "ema20": metric_number(inputs.latest_ema20),
                    "vwap": metric_number(inputs.latest_vwap),
                    "rvol": metric_number(inputs.latest_rvol),
                    "prev_rvol": metric_number(inputs.prev_rvol),
                    "prev2_rvol": metric_number(inputs.prev2_rvol),
                },
            ),
            role="linda_exhaustion_sm_buy" if sm_long else "linda_exhaustion_sm_sell",
        )
        sm_label["code"] = "SM_BUY" if sm_long else "SM_SELL"
        sm_label["action"] = "WATCH"
        sm_label["raw_action"] = "WATCH"
        sm_label["overlay_role"] = "exhaustion"
        items.append(sm_label)
    if inputs.clx_ct_long or inputs.clx_ct_short:
        clx_long = bool(inputs.clx_ct_long)
        clx_label = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.low if clx_long else inputs.latest_bar.high,
            lines=[],
            direction="long" if clx_long else "short",
            side="below" if clx_long else "above",
            fact_fields=indicator_fact_payload(
                scenario="climax_countertrend_watch",
                setup="long_after_sell_climax" if clx_long else "short_after_buy_climax",
                trigger_event={"code": "close_above_high" if clx_long else "close_below_low"},
                context={"code": "countertrend_requires_channel_vwap_or_reclaim"},
                risk={"code": "new_low_invalidation" if clx_long else "new_high_invalidation"},
                metrics={
                    "trigger_level": metric_number(
                        inputs.latest_bar.high if clx_long else inputs.latest_bar.low
                    ),
                    "invalid_level": metric_number(
                        inputs.latest_bar.low if clx_long else inputs.latest_bar.high
                    ),
                    "price": metric_number(inputs.latest_bar.close),
                },
            ),
            role="linda_exhaustion_clx_long" if clx_long else "linda_exhaustion_clx_short",
        )
        clx_label["code"] = "CLX_LONG" if clx_long else "CLX_SHORT"
        clx_label["action"] = "WATCH"
        clx_label["raw_action"] = "WATCH"
        clx_label["overlay_role"] = "exhaustion"
        clx_label["tone"] = "warning"
        items.append(clx_label)
    if inputs.bull_candidate or inputs.bear_candidate:
        exh_short = bool(inputs.bull_candidate)
        score_value = inputs.bull_score if exh_short else inputs.bear_score
        exh_label = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.high if exh_short else inputs.latest_bar.low,
            lines=[],
            direction="short" if exh_short else "long",
            side="above" if exh_short else "below",
            fact_fields=indicator_fact_payload(
                scenario="exhaustion_watch",
                setup="short_after_rise" if exh_short else "long_after_drop",
                trigger_event={"code": "close_below_low" if exh_short else "close_above_high"},
                context={"code": "entry_requires_channel_vwap_reject_or_reclaim"},
                risk={"code": "new_high_invalidation" if exh_short else "new_low_invalidation"},
                quality={"code": "score", "value": score_value},
                metrics={
                    "score": metric_number(score_value, digits=0),
                    "max_score": metric_number(5, digits=0),
                    "climax_or_effort_result": bool(inputs.upper_volume_exhaustion),
                    "location_exhaustion": bool(
                        inputs.upper_location_exhaustion
                        if exh_short
                        else inputs.lower_location_exhaustion
                    ),
                    "trigger_level": metric_number(
                        inputs.latest_bar.low if exh_short else inputs.latest_bar.high
                    ),
                    "invalid_level": metric_number(
                        inputs.latest_bar.high if exh_short else inputs.latest_bar.low
                    ),
                    "price": metric_number(inputs.latest_bar.close),
                },
            ),
            role=("linda_exhaustion_short_watch" if exh_short else "linda_exhaustion_long_watch"),
        )
        exh_label["code"] = "EXH_SHORT" if exh_short else "EXH_LONG"
        exh_label["action"] = "WATCH"
        exh_label["raw_action"] = "WATCH"
        exh_label["overlay_role"] = "exhaustion"
        exh_label["tone"] = "warning"
        items.append(exh_label)
    if inputs.reversal_warning:
        rev_short = bool(inputs.trend_day_bull or inputs.full_bull_now)
        rev_label = runtime_overlays.label(
            bar=inputs.latest_bar,
            price=inputs.latest_bar.high if rev_short else inputs.latest_bar.low,
            lines=[],
            direction="short" if rev_short else "long",
            side="above" if rev_short else "below",
            fact_fields=indicator_fact_payload(
                scenario="reversal_warning",
                setup="profit_protection_warning",
                trigger_event={"code": "close_below_low" if rev_short else "close_above_high"},
                risk={"code": "trend_continuation_rising_volume"},
                context={"code": "volume_climax_reversal_or_deep_pullback"},
                metrics={
                    "trigger_level": metric_number(
                        inputs.latest_bar.low if rev_short else inputs.latest_bar.high
                    ),
                    "price": metric_number(inputs.latest_bar.close),
                },
            ),
            role="linda_exhaustion_reversal_warning",
        )
        rev_label["code"] = "REV_WARN"
        rev_label["action"] = "WATCH"
        rev_label["raw_action"] = "WATCH"
        rev_label["overlay_role"] = "exhaustion"
        rev_label["tone"] = "warning"
        items.append(rev_label)
    return items


@dataclass(frozen=True)
class LindaVwapOverlayInputs:
    latest_bar: Bar
    params: LindaVolumeParams
    latest_vwap: float
    latest_rvol: float
    latest_upper_2: float
    latest_lower_2: float
    pullback_long_active: bool
    pullback_short_active: bool
    upper_2_reversal_valid: bool
    upper_2_ride: bool
    lower_2_reversal_valid: bool
    lower_2_ride: bool
    touch_upper_2: Sequence[bool]
    touch_lower_2: Sequence[bool]


def build_vwap_overlays(inputs: LindaVwapOverlayInputs) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if inputs.pullback_long_active:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.low,
                direction="long",
                side="below",
                role="linda_vwap_pullback_long",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_pullback",
                    setup="long_with_trend",
                    trigger_event={"code": "vwap_hold_break_or_close_above_high"},
                    context={"code": "entry_from_vwap_ema20_or_channel"},
                    risk={"code": "close_below_vwap_or_signal_low"},
                    metrics={
                        "vwap": metric_number(inputs.latest_vwap),
                        "price": metric_number(inputs.latest_bar.close),
                        "invalid_level": metric_number(inputs.latest_bar.low),
                        "rvol": metric_number(inputs.latest_rvol),
                        "min_rvol": metric_number(max(inputs.params.pullback_rvol, 0.95)),
                    },
                ),
            )
        )
    if inputs.pullback_short_active:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.high,
                direction="short",
                side="above",
                role="linda_vwap_pullback_short",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_pullback",
                    setup="short_with_trend",
                    trigger_event={"code": "vwap_hold_break_or_close_below_low"},
                    context={"code": "entry_from_vwap_ema20_or_channel"},
                    risk={"code": "close_above_vwap_or_signal_high"},
                    metrics={
                        "vwap": metric_number(inputs.latest_vwap),
                        "price": metric_number(inputs.latest_bar.close),
                        "invalid_level": metric_number(inputs.latest_bar.high),
                        "rvol": metric_number(inputs.latest_rvol),
                        "min_rvol": metric_number(max(inputs.params.pullback_rvol, 0.95)),
                    },
                ),
            )
        )
    if inputs.upper_2_reversal_valid:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.high,
                direction="short",
                side="above",
                role="linda_vwap_upper_2_reversal",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_upper_2_reversal_watch",
                    setup="possible_short",
                    trigger_event={"code": "reject_close_inside_band_or_below_low"},
                    context={"code": "requires_rejection_channel_or_vsa"},
                    risk={"code": "above_signal_high"},
                    metrics={
                        "upper_2_sigma": metric_number(inputs.latest_upper_2),
                        "invalid_level": metric_number(inputs.latest_bar.high),
                    },
                ),
            )
        )
    elif inputs.upper_2_ride:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.high,
                direction="flat",
                side="above",
                role="linda_vwap_upper_2_ride",
                tone="warning",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_upper_2_band_ride",
                    setup="up_continuation",
                    opposing={"code": "countertrend_blocked_during_band_ride"},
                    trigger_event={"code": "return_inside_band_with_vsa_or_structure"},
                    metrics={
                        "upper_2_sigma": metric_number(inputs.latest_upper_2),
                        "touches_8b": metric_number(true_count(inputs.touch_upper_2, 8), digits=0),
                    },
                ),
            )
        )
    elif inputs.touch_upper_2[-1]:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.high,
                direction="flat",
                side="above",
                role="linda_vwap_upper_2_touch",
                tone="info",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_upper_2_touch",
                    setup="overheat_zone_context",
                    trigger_event={"code": "return_inside_band"},
                    metrics={"upper_2_sigma": metric_number(inputs.latest_upper_2)},
                ),
            )
        )
    if inputs.lower_2_reversal_valid:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.low,
                direction="long",
                side="below",
                role="linda_vwap_lower_2_reversal",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_lower_2_reversal_watch",
                    setup="possible_long",
                    trigger_event={"code": "reclaim_close_inside_band_or_above_high"},
                    context={"code": "requires_rejection_channel_or_vsa"},
                    risk={"code": "below_signal_low"},
                    metrics={
                        "lower_2_sigma": metric_number(inputs.latest_lower_2),
                        "invalid_level": metric_number(inputs.latest_bar.low),
                    },
                ),
            )
        )
    elif inputs.lower_2_ride:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.low,
                direction="flat",
                side="below",
                role="linda_vwap_lower_2_ride",
                tone="warning",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_lower_2_band_ride",
                    setup="down_continuation",
                    opposing={"code": "countertrend_blocked_during_band_ride"},
                    trigger_event={"code": "return_inside_band_with_vsa_or_structure"},
                    metrics={
                        "lower_2_sigma": metric_number(inputs.latest_lower_2),
                        "touches_8b": metric_number(true_count(inputs.touch_lower_2, 8), digits=0),
                    },
                ),
            )
        )
    elif inputs.touch_lower_2[-1]:
        items.append(
            vwap_signal_overlay(
                bar=inputs.latest_bar,
                price=inputs.latest_bar.low,
                direction="flat",
                side="below",
                role="linda_vwap_lower_2_touch",
                tone="info",
                fact_fields=indicator_fact_payload(
                    scenario="vwap_lower_2_touch",
                    setup="oversold_zone_context",
                    trigger_event={"code": "return_inside_band"},
                    metrics={"lower_2_sigma": metric_number(inputs.latest_lower_2)},
                ),
            )
        )
    return items
