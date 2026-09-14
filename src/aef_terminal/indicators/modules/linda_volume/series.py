from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.features.vsa import VsaFact
from aef_terminal.indicators.domain_facts import (
    copy_indicator_facts,
    indicator_fact_payload,
    metric_number,
)
from aef_terminal.indicators.defaults import ScoreDefaults
from aef_terminal.runtime import overlays, pine
from aef_terminal.runtime.math_utils import round_optional
from aef_terminal.runtime.signal_state import SignalState

from .params import LindaVolumeParams
from .plan import direction_for_state, score_for_state, signal_for_item


@dataclass(frozen=True)
class LindaSeriesResult:
    series: list[dict[str, Any]]
    events: list[dict[str, Any]]
    signals: list[SignalState]
    overlay_items: list[dict[str, Any]]
    last_signal: dict[str, Any] | None


def build_linda_series(
    *,
    bars: Sequence[Bar],
    facts: Sequence[VsaFact],
    highs: Sequence[float],
    lows: Sequence[float],
    ema20: Sequence[float],
    ema233: Sequence[float],
    vwap: Sequence[float],
    atr_values: Sequence[float],
    volume_avg: Sequence[float],
    adx_values: Sequence[float],
    params: LindaVolumeParams,
    score_bands: ScoreDefaults,
    indian_spacing_bars: int,
    indian_extension_lookback: int,
    preview_only: bool = False,
) -> LindaSeriesResult:
    series: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    signals: list[SignalState] = []
    overlay_items: list[dict[str, Any]] = []
    last_signal: dict[str, Any] | None = None
    pb_up_stage = 0
    pb_dn_stage = 0
    indian_up_count = 0
    indian_dn_count = 0
    last_indian_bar: int | None = None
    last_indian_side = 0
    trend_up_age = 0
    trend_dn_age = 0

    for index, fact in enumerate(facts):
        emit_item = not preview_only or index == len(facts) - 1
        bar = fact.bar
        atr = max(atr_values[index], 0.000001)
        prev = bars[index - 1] if index else bar
        ema_fast = ema20[index]
        ema_slow = ema233[index]
        vw = vwap[index]
        trend_up = bar.close > ema_fast and ema_fast >= ema_slow
        trend_dn = bar.close < ema_fast and ema_fast <= ema_slow
        trend_up_age = trend_up_age + 1 if trend_up else 0
        trend_dn_age = trend_dn_age + 1 if trend_dn else 0
        full_bull = bar.close > ema_slow and bar.close > vw
        full_bear = bar.close < ema_slow and bar.close < vw
        h1_adx_proxy = adx_values[index]
        indian_warmup_ok = index >= max(20, params.atr_len)
        indian_bull_trend = (
            indian_warmup_ok
            and full_bull
            and (h1_adx_proxy > params.indian_adx_min or trend_up_age >= 4)
        )
        indian_bear_trend = (
            indian_warmup_ok
            and full_bear
            and (h1_adx_proxy > params.indian_adx_min or trend_dn_age >= 4)
        )
        ext_start = max(0, index - indian_extension_lookback + 1)
        bull_was_extended = (
            max(highs[ext_start : index + 1]) >= ema_fast + atr * params.indian_extension_atr
        )
        bear_was_extended = (
            min(lows[ext_start : index + 1]) <= ema_fast - atr * params.indian_extension_atr
        )
        indian_volume_ok = (
            not params.indian_use_volume_filter
            or bar.volume >= volume_avg[index] * params.indian_volume_mult
        )
        indian_bull_touch = (
            indian_bull_trend
            and bar.low <= ema_fast
            and bar.close > ema_fast
            and indian_volume_ok
            and (not params.indian_require_extension or bull_was_extended)
        )
        indian_bear_touch = (
            indian_bear_trend
            and bar.high >= ema_fast
            and bar.close < ema_fast
            and indian_volume_ok
            and (not params.indian_require_extension or bear_was_extended)
        )
        indian_can_count = last_indian_bar is None or index - last_indian_bar >= indian_spacing_bars
        near_ema = (
            abs(bar.close - ema_fast) <= atr * params.pullback_atr_tolerance
            or bar.low <= ema_fast <= bar.high
        )
        near_vwap = (
            abs(bar.close - vw) <= atr * params.pullback_atr_tolerance or bar.low <= vw <= bar.high
        )
        close_up = bar.close >= bar.open
        close_dn = bar.close < bar.open
        pb_stage: int | None = None
        indian_count: int | None = None
        indian_counted_bar = False
        state = ""
        if params.enable_indians and indian_bull_touch and indian_can_count:
            state = "PB_UP"
        elif params.enable_indians and indian_bear_touch and indian_can_count:
            state = "PB_DN"
        elif (
            trend_up
            and near_ema
            and (near_vwap or bar.low <= ema_fast)
            and close_up
            and fact.rvol >= params.pullback_rvol
        ):
            state = "PB_UP"
        elif (
            trend_dn
            and near_ema
            and (near_vwap or bar.high >= ema_fast)
            and close_dn
            and fact.rvol >= params.pullback_rvol
        ):
            state = "PB_DN"
        elif prev.close <= vwap[index - 1] and bar.low <= vw and bar.close > vw and close_up:
            state = "VW_RECLAIM"
        elif prev.close >= vwap[index - 1] and bar.high >= vw and bar.close < vw and close_dn:
            state = "VW_REJECT"
        elif (
            fact.rvol <= params.no_demand_rvol
            and fact.body_share <= 0.42
            and trend_up
            and bar.high >= prev.high
        ):
            state = "NO_DEMAND"
        elif (
            fact.rvol <= params.no_demand_rvol
            and fact.body_share <= 0.42
            and trend_dn
            and bar.low <= prev.low
        ):
            state = "NO_SUPPLY"

        if state == "PB_UP":
            if params.enable_indians and indian_bull_touch and indian_can_count:
                if last_indian_side != 1:
                    indian_up_count = 0
                indian_up_count += 1
                last_indian_bar = index
                last_indian_side = 1
                indian_counted_bar = True
            else:
                pb_up_stage += 1
            pb_dn_stage = 0
            indian_dn_count = 0
            pb_stage = indian_up_count if indian_counted_bar else pb_up_stage
            if indian_counted_bar and (
                indian_bull_trend or trend_up_age >= params.indian_min_trend_bars
            ):
                indian_count = indian_up_count
        elif state == "PB_DN":
            if params.enable_indians and indian_bear_touch and indian_can_count:
                if last_indian_side != -1:
                    indian_dn_count = 0
                indian_dn_count += 1
                last_indian_bar = index
                last_indian_side = -1
                indian_counted_bar = True
            else:
                pb_dn_stage += 1
            pb_up_stage = 0
            indian_up_count = 0
            pb_stage = indian_dn_count if indian_counted_bar else pb_dn_stage
            if indian_counted_bar and (
                indian_bear_trend or trend_dn_age >= params.indian_min_trend_bars
            ):
                indian_count = indian_dn_count
        if h1_adx_proxy < 20 or not (trend_up or indian_bull_trend):
            pb_up_stage = 0
            indian_up_count = 0
            if last_indian_side == 1:
                last_indian_bar = None
                last_indian_side = 0
        if h1_adx_proxy < 20 or not (trend_dn or indian_bear_trend):
            pb_dn_stage = 0
            indian_dn_count = 0
            if last_indian_side == -1:
                last_indian_bar = None
                last_indian_side = 0

        trend_star = (state == "PB_UP" and trend_up_age >= 3) or (
            state == "PB_DN" and trend_dn_age >= 3
        )
        score = score_for_state(
            fact.score,
            state,
            trend_bonus=trend_up or trend_dn,
        )
        if indian_count:
            score = pine.clamp(
                score + (5.0 if indian_count <= 2 else -2.0),
                0.0,
                99.0,
            )
        visible_indian_count = (
            indian_count
            if params.enable_indians
            and indian_count is not None
            and score >= params.indian_min_score
            else None
        )
        important = bool(state) and (
            score >= params.min_label_score
            or (
                visible_indian_count is not None
                and visible_indian_count <= 2
                and score >= max(params.min_label_score - 10, params.indian_min_score)
            )
        )
        reason_code = f"linda_{state.lower()}" if state else "linda_wait"
        indian_context = (
            {
                "code": "indian_pullback_stage",
                "count": visible_indian_count,
                "direction": direction_for_state(state),
            }
            if visible_indian_count is not None
            else None
        )
        item = {
            "ts": bar.ts.isoformat(),
            "display_volume": round_optional(fact.display_volume),
            "display_avg": round_optional(fact.display_avg),
            "rvol": round_optional(fact.rvol),
            "vol_rank": round_optional(fact.vol_rank, 2),
            "vol_z": round_optional(fact.vol_z),
            "move_atr": round_optional(fact.move_atr),
            "range_atr": round_optional(fact.range_atr),
            "spread_rel": round_optional(fact.spread_rel),
            "score": round_optional(score, 2),
            "code": state,
            "structure_text_ref": "linda_volume",
            "structure_text_role": "linda_indian",
            "direction": direction_for_state(state),
            "important": important,
            "pb_stage": pb_stage,
            "indian_count": visible_indian_count,
            "trend_star": trend_star and important,
            "ema20": round_optional(ema_fast),
            "ema233": round_optional(ema_slow),
            "vwap": round_optional(vw),
            "reason_code": reason_code,
            **(
                indicator_fact_payload(
                    scenario="linda_theory",
                    trigger_event={"code": state.lower() if state else "plan"},
                    supporting=indian_context,
                    quality={"code": "score", "value": score},
                    metrics={
                        "score": metric_number(score, digits=0),
                        "pb_stage": pb_stage,
                        "indian_count": visible_indian_count,
                        "trend_continuation_star": bool(trend_star and important),
                        "close": metric_number(bar.close),
                        "vwap": metric_number(vw),
                        "ema20": metric_number(ema_fast),
                        "rvol": metric_number(fact.rvol),
                        "volume_rank": metric_number(fact.vol_rank, digits=0, suffix="%"),
                        "volume_z": metric_number(fact.vol_z),
                        "move_atr": metric_number(fact.move_atr, suffix=" ATR"),
                        "range_atr": metric_number(fact.range_atr, suffix=" ATR"),
                        "spread_rel": metric_number(fact.spread_rel),
                    },
                )
                if emit_item
                else {}
            ),
        }
        signal = signal_for_item(
            item,
            bar,
            atr,
            params.min_label_score,
            score_bands,
        )
        if (
            visible_indian_count is not None
            and visible_indian_count <= 3
            and state in {"PB_UP", "PB_DN"}
            and emit_item
        ):
            mark = overlays.label(
                bar=bar,
                price=bar.low if item["direction"] == "long" else bar.high,
                lines=[],
                direction=item["direction"],
                side="below" if item["direction"] == "long" else "above",
                role="linda_indian_price_mark",
                fact_fields=copy_indicator_facts(item),
            )
            mark.update(
                {
                    "code": state,
                    "score": item["score"],
                    "pb_stage": pb_stage,
                    "indian_count": visible_indian_count,
                    "trend_star": item["trend_star"],
                    "action": signal.action,
                    "raw_action": signal.raw_action,
                    "structure_text_role": "linda_indian",
                    "overlay_role": "linda_indian_price_mark",
                }
            )
            overlay_items.append(mark)
        signal_payload = signal.as_dict()
        signal_payload["reason_code"] = str(signal_payload.pop("reason", "") or item["reason_code"])
        item["signal"] = signal_payload
        signals.append(signal)
        if emit_item:
            series.append(item)
        # Historical state still feeds the latest pressure/Indian context in
        # preview mode.  Keep its lightweight rows internally, but materialize
        # domain facts and public output only for the preview bar.
        if state:
            events.append(item)
        if important:
            last_signal = item

    return LindaSeriesResult(
        series=series,
        events=events,
        signals=signals,
        overlay_items=overlay_items,
        last_signal=last_signal,
    )
