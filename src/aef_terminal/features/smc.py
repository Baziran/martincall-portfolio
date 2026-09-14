from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.domain import Bar, DomainFact, ScenarioKind
from aef_terminal.features.price_action import sfp_state_series
from aef_terminal.runtime.math_utils import round_optional
from aef_terminal.runtime.pine import PivotPoint, atr_rma_series, bar_anatomy, clamp, pivot_points


def _round(value: float | None, digits: int = 2) -> float | None:
    return round_optional(value, digits)


@dataclass(frozen=True)
class SmcParams:
    pivot_len: int = 4
    equal_tolerance_atr: float = 0.12
    fvg_min_atr: float = 0.18
    ob_min_displacement_atr: float = 0.45
    ob_min_body_share: float = 0.35
    ob_max_counter_wick_share: float = 0.45
    ob_min_break_body_atr: float = 0.12
    ob_min_break_vol_ratio: float = 0.85
    fvg_displacement_atr: float = 0.55
    break_buffer_atr: float = 0.0
    max_events: int = 120
    max_boxes: int = 24


@dataclass(frozen=True)
class StructureEvent:
    index: int
    ts: str
    price: float
    direction: str
    event: str
    event_code: str
    scenario_kind: str
    score: float
    trigger_event: DomainFact
    evidence: tuple[DomainFact, ...]
    start_ts: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "start_ts": self.start_ts,
            "index": self.index,
            "price": _round(self.price),
            "direction": self.direction,
            "event": self.event,
            "event_code": self.event_code,
            "scenario_kind": self.scenario_kind,
            "score": round(self.score, 2),
            "trigger_event": self.trigger_event.as_dict(),
            "evidence": {"context": [item.as_dict() for item in self.evidence]},
        }


@dataclass
class FvgZone:
    index: int
    start_index: int
    end_index: int
    start_ts: str
    end_ts: str
    top: float
    bottom: float
    direction: str
    size_atr: float
    mitigated: bool = False
    filled: bool = False
    fill_index: int | None = None
    fill_ts: str | None = None

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) * 0.5

    @property
    def state(self) -> str:
        if self.filled:
            return "filled"
        if self.mitigated:
            return "mitigated"
        return "active"

    def to_dict(self) -> dict[str, Any]:
        state = self.state
        return {
            "kind": "fvg",
            "type": "FVG_UP" if self.direction == "long" else "FVG_DN",
            "direction": self.direction,
            "index": self.index,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "top": _round(self.top),
            "bottom": _round(self.bottom),
            "midpoint": _round(self.midpoint),
            "size_atr": round(self.size_atr, 2),
            "state": state,
            "filled": self.filled,
            "mitigated": self.mitigated,
            "fill_index": self.fill_index,
            "fill_ts": self.fill_ts,
            "scenario": "smc_fvg_zone",
            "trigger_event": {"code": "smc_zone_state", "state": state},
            "evidence": {
                "context": [
                    {
                        "code": "zone_geometry",
                        "bottom": _round(self.bottom),
                        "top": _round(self.top),
                    },
                    {"code": "zone_size_atr", "value": round(self.size_atr, 2)},
                    {"code": "zone_lifecycle_state", "state": state},
                ]
            },
        }


@dataclass
class OrderBlock:
    index: int
    start_index: int
    ts: str
    top: float
    bottom: float
    direction: str  # 'long' (bullish OB), 'short' (bearish OB)
    mitigated: bool = False
    filled: bool = False
    fill_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        state = "filled" if self.filled else "mitigated" if self.mitigated else "active"
        return {
            "kind": "order_block",
            "type": "OB_UP" if self.direction == "long" else "OB_DN",
            "direction": self.direction,
            "index": self.index,
            "start_index": self.start_index,
            "ts": self.ts,
            "start_ts": self.ts,
            "end_ts": self.ts,
            "top": _round(self.top),
            "bottom": _round(self.bottom),
            "midpoint": _round((self.top + self.bottom) * 0.5),
            "mitigated": self.mitigated,
            "filled": self.filled,
            "state": state,
            "scenario": "smc_order_block",
            "trigger_event": {"code": "smc_zone_state", "state": state},
            "evidence": {
                "context": [
                    {
                        "code": "zone_geometry",
                        "bottom": _round(self.bottom),
                        "top": _round(self.top),
                    },
                    {"code": "zone_origin", "bar_index": self.start_index},
                ]
            },
        }


def _pivot_dict(point: PivotPoint) -> dict[str, Any]:
    return {
        "index": point.index,
        "ts": point.ts.isoformat(),
        "price": _round(point.price),
        "type": point.type,
    }


def _break_score(distance: float, atr: float) -> float:
    return clamp(58.0 + distance / max(atr, 0.000001) * 14.0, 0.0, 99.0)


def _ob_has_min_displacement(
    *,
    direction: str,
    break_close: float,
    ob_high: float,
    ob_low: float,
    atr: float,
    min_displacement_atr: float,
) -> bool:
    threshold = max(atr, 0.000001) * max(min_displacement_atr, 0.0)
    if direction == "long":
        displacement = break_close - ob_high
    else:
        displacement = ob_low - break_close
    return displacement >= threshold


def _ob_candle_structure_valid(
    *,
    direction: str,
    ob_bar: Bar,
    min_body_share: float,
    max_counter_wick_share: float,
) -> bool:
    anatomy = bar_anatomy(ob_bar)
    if direction == "long":
        return (
            ob_bar.close < ob_bar.open
            and anatomy.body_share >= max(min_body_share, 0.0)
            and anatomy.lower_share <= max(max_counter_wick_share, 0.0)
        )
    return (
        ob_bar.close > ob_bar.open
        and anatomy.body_share >= max(min_body_share, 0.0)
        and anatomy.upper_share <= max(max_counter_wick_share, 0.0)
    )


def _ob_has_break_imbalance(
    *,
    direction: str,
    break_bar: Bar,
    ob_bar: Bar,
    ob_high: float,
    ob_low: float,
    atr: float,
    min_body_atr: float = 0.12,
    min_break_vol_ratio: float = 0.85,
) -> bool:
    """Require the breaking candle to displace with directional body momentum."""
    threshold = max(atr, 0.000001) * max(min_body_atr, 0.0)
    anatomy = bar_anatomy(break_bar)
    break_volume = break_bar.volume
    ob_volume = ob_bar.volume
    volume_ok = ob_volume <= 0.0 or break_volume >= ob_volume * max(min_break_vol_ratio, 0.0)
    if direction == "long":
        return (
            break_bar.close > break_bar.open
            and anatomy.close_pos >= 0.55
            and (break_bar.close - ob_high) >= threshold
            and volume_ok
        )
    return (
        break_bar.close < break_bar.open
        and anatomy.close_pos <= 0.45
        and (ob_low - break_bar.close) >= threshold
        and volume_ok
    )


def _ob_is_valid(
    *,
    direction: str,
    break_bar: Bar,
    ob_bar: Bar,
    ob_high: float,
    ob_low: float,
    atr: float,
    min_displacement_atr: float,
    min_body_share: float,
    max_counter_wick_share: float,
    min_break_body_atr: float,
    min_break_vol_ratio: float,
) -> bool:
    return (
        _ob_candle_structure_valid(
            direction=direction,
            ob_bar=ob_bar,
            min_body_share=min_body_share,
            max_counter_wick_share=max_counter_wick_share,
        )
        and _ob_has_min_displacement(
            direction=direction,
            break_close=break_bar.close,
            ob_high=ob_high,
            ob_low=ob_low,
            atr=atr,
            min_displacement_atr=min_displacement_atr,
        )
        and _ob_has_break_imbalance(
            direction=direction,
            break_bar=break_bar,
            ob_bar=ob_bar,
            ob_high=ob_high,
            ob_low=ob_low,
            atr=atr,
            min_body_atr=min_break_body_atr,
            min_break_vol_ratio=min_break_vol_ratio,
        )
    )


def smc_snapshot(bars: Sequence[Bar], params: SmcParams | None = None) -> dict[str, Any]:
    params = params or SmcParams()
    if not bars:
        return {
            "version": "smc-core-1",
            "params": {},
            "pivots": [],
            "events": [],
            "boxes": [],
            "levels": [],
            "latest": None,
        }

    pivot_len = max(int(params.pivot_len), 2)
    atr_values = atr_rma_series(bars, 14)
    pivots = pivot_points(bars, pivot_len, pivot_len, max_points=160)
    pending_pivots: dict[int, list[PivotPoint]] = defaultdict(list)
    for point in pivots:
        activation_index = point.index + pivot_len
        if activation_index < len(bars):
            pending_pivots[activation_index].append(point)

    active_pivots: list[PivotPoint] = []
    sfp = sfp_state_series(bars, atr_values, lookback=20)

    events: list[StructureEvent] = []
    zones: list[FvgZone] = []
    order_blocks: list[OrderBlock] = []
    last_swing_high: PivotPoint | None = None
    previous_swing_high: PivotPoint | None = None
    last_swing_low: PivotPoint | None = None
    previous_swing_low: PivotPoint | None = None
    broken_highs: set[int] = set()
    broken_lows: set[int] = set()
    structure_dir = 0

    for index, bar in enumerate(bars):
        atr = max(atr_values[index], 0.000001)

        for zone in zones:
            if zone.filled or zone.index >= index:
                continue
            if zone.direction == "long":
                if bar.low <= zone.midpoint:
                    zone.mitigated = True
                if bar.low <= zone.bottom:
                    zone.filled = True
                    zone.fill_index = index
                    zone.fill_ts = bar.ts.isoformat()
            else:
                if bar.high >= zone.midpoint:
                    zone.mitigated = True
                if bar.high >= zone.top:
                    zone.filled = True
                    zone.fill_index = index
                    zone.fill_ts = bar.ts.isoformat()

        for ob in order_blocks:
            if ob.filled or ob.index >= index:
                continue
            if ob.direction == "long" and bar.low <= ob.bottom:
                ob.filled = True
            elif ob.direction == "short" and bar.high >= ob.top:
                ob.filled = True

        for pivot in pending_pivots.get(index, []):
            active_pivots.append(pivot)
            if pivot.type == 1:
                if (
                    last_swing_high
                    and abs(pivot.price - last_swing_high.price) <= atr * params.equal_tolerance_atr
                ):
                    events.append(
                        StructureEvent(
                            index=index,
                            ts=bar.ts.isoformat(),
                            price=pivot.price,
                            direction="short",
                            event="EQH",
                            event_code="equal_high",
                            scenario_kind=ScenarioKind.FADE.value,
                            score=62.0,
                            start_ts=last_swing_high.ts.isoformat(),
                            trigger_event=DomainFact("equal_highs"),
                            evidence=(
                                DomainFact("equal_level", {"price": _round(pivot.price)}),
                                DomainFact(
                                    "equal_tolerance_atr", {"value": params.equal_tolerance_atr}
                                ),
                                DomainFact("liquidity_above_equal_highs"),
                            ),
                        )
                    )
                previous_swing_high = last_swing_high
                last_swing_high = pivot
            elif pivot.type == -1:
                if (
                    last_swing_low
                    and abs(pivot.price - last_swing_low.price) <= atr * params.equal_tolerance_atr
                ):
                    events.append(
                        StructureEvent(
                            index=index,
                            ts=bar.ts.isoformat(),
                            price=pivot.price,
                            direction="long",
                            event="EQL",
                            event_code="equal_low",
                            scenario_kind=ScenarioKind.FADE.value,
                            score=62.0,
                            start_ts=last_swing_low.ts.isoformat(),
                            trigger_event=DomainFact("equal_lows"),
                            evidence=(
                                DomainFact("equal_level", {"price": _round(pivot.price)}),
                                DomainFact(
                                    "equal_tolerance_atr", {"value": params.equal_tolerance_atr}
                                ),
                                DomainFact("liquidity_below_equal_lows"),
                            ),
                        )
                    )
                previous_swing_low = last_swing_low
                last_swing_low = pivot

        buffer = atr * max(params.break_buffer_atr, 0.0)

        if (
            last_swing_high
            and last_swing_high.index not in broken_highs
            and bar.close > last_swing_high.price + buffer
        ):
            is_choch = structure_dir < 0
            event = "CHOCH↑" if is_choch else "BOS↑"
            score = _break_score(bar.close - last_swing_high.price, atr)
            events.append(
                StructureEvent(
                    index=index,
                    ts=bar.ts.isoformat(),
                    price=last_swing_high.price,
                    direction="long",
                    event=event,
                    event_code="change_of_character" if is_choch else "break_of_structure",
                    scenario_kind=ScenarioKind.TRANSIT.value,
                    score=score,
                    start_ts=last_swing_high.ts.isoformat(),
                    trigger_event=DomainFact(
                        "change_of_character" if is_choch else "break_of_structure",
                        {"direction": "long"},
                    ),
                    evidence=(
                        DomainFact(
                            "confirmed_swing_break",
                            {"side": "high", "price": _round(last_swing_high.price)},
                        ),
                        DomainFact("break_close", {"price": _round(bar.close)}),
                        DomainFact("pivot_origin", {"bar_index": last_swing_high.index}),
                        DomainFact(
                            "prior_structure",
                            {"direction": "bearish" if is_choch else "non_bearish"},
                        ),
                    ),
                )
            )
            # Поиск Bullish Order Block (последняя медвежья свеча перед BOS)
            search_start = last_swing_low.index if last_swing_low else max(0, index - 10)
            for j in range(index - 1, search_start, -1):
                if bars[j].close < bars[j].open:
                    if _ob_is_valid(
                        direction="long",
                        break_bar=bar,
                        ob_bar=bars[j],
                        ob_high=bars[j].high,
                        ob_low=bars[j].low,
                        atr=atr,
                        min_displacement_atr=params.ob_min_displacement_atr,
                        min_body_share=params.ob_min_body_share,
                        max_counter_wick_share=params.ob_max_counter_wick_share,
                        min_break_body_atr=params.ob_min_break_body_atr,
                        min_break_vol_ratio=params.ob_min_break_vol_ratio,
                    ):
                        order_blocks.append(
                            OrderBlock(
                                index=index,
                                start_index=j,
                                ts=bars[j].ts.isoformat(),
                                top=bars[j].high,
                                bottom=bars[j].low,
                                direction="long",
                            )
                        )
                    break

            broken_highs.add(last_swing_high.index)
            structure_dir = 1

        if (
            last_swing_low
            and last_swing_low.index not in broken_lows
            and bar.close < last_swing_low.price - buffer
        ):
            is_choch = structure_dir > 0
            event = "CHOCH↓" if is_choch else "BOS↓"
            score = _break_score(last_swing_low.price - bar.close, atr)
            events.append(
                StructureEvent(
                    index=index,
                    ts=bar.ts.isoformat(),
                    price=last_swing_low.price,
                    direction="short",
                    event=event,
                    event_code="change_of_character" if is_choch else "break_of_structure",
                    scenario_kind=ScenarioKind.TRANSIT.value,
                    score=score,
                    start_ts=last_swing_low.ts.isoformat(),
                    trigger_event=DomainFact(
                        "change_of_character" if is_choch else "break_of_structure",
                        {"direction": "short"},
                    ),
                    evidence=(
                        DomainFact(
                            "confirmed_swing_break",
                            {"side": "low", "price": _round(last_swing_low.price)},
                        ),
                        DomainFact("break_close", {"price": _round(bar.close)}),
                        DomainFact("pivot_origin", {"bar_index": last_swing_low.index}),
                        DomainFact(
                            "prior_structure",
                            {"direction": "bullish" if is_choch else "non_bullish"},
                        ),
                    ),
                )
            )
            # Поиск Bearish Order Block (последняя бычья свеча перед BOS)
            search_start = last_swing_high.index if last_swing_high else max(0, index - 10)
            for j in range(index - 1, search_start, -1):
                if bars[j].close > bars[j].open:
                    if _ob_is_valid(
                        direction="short",
                        break_bar=bar,
                        ob_bar=bars[j],
                        ob_high=bars[j].high,
                        ob_low=bars[j].low,
                        atr=atr,
                        min_displacement_atr=params.ob_min_displacement_atr,
                        min_body_share=params.ob_min_body_share,
                        max_counter_wick_share=params.ob_max_counter_wick_share,
                        min_break_body_atr=params.ob_min_break_body_atr,
                        min_break_vol_ratio=params.ob_min_break_vol_ratio,
                    ):
                        order_blocks.append(
                            OrderBlock(
                                index=index,
                                start_index=j,
                                ts=bars[j].ts.isoformat(),
                                top=bars[j].high,
                                bottom=bars[j].low,
                                direction="short",
                            )
                        )
                    break

            broken_lows.add(last_swing_low.index)
            structure_dir = -1

        if sfp[index].bull:
            events.append(
                StructureEvent(
                    index=index,
                    ts=bar.ts.isoformat(),
                    price=bar.low,
                    direction="long",
                    event="SWEEP↑",
                    event_code="liquidity_sweep",
                    scenario_kind=ScenarioKind.FADE.value,
                    score=sfp[index].score_bull,
                    trigger_event=DomainFact("bullish_liquidity_sweep"),
                    evidence=(
                        DomainFact("swept_level", {"price": _round(sfp[index].bull_level)}),
                        DomainFact("sweep_score", {"value": round(sfp[index].score_bull, 2)}),
                        DomainFact("swept_low_reclaimed"),
                    ),
                )
            )
        if sfp[index].bear:
            events.append(
                StructureEvent(
                    index=index,
                    ts=bar.ts.isoformat(),
                    price=bar.high,
                    direction="short",
                    event="SWEEP↓",
                    event_code="liquidity_sweep",
                    scenario_kind=ScenarioKind.FADE.value,
                    score=sfp[index].score_bear,
                    trigger_event=DomainFact("bearish_liquidity_sweep"),
                    evidence=(
                        DomainFact("swept_level", {"price": _round(sfp[index].bear_level)}),
                        DomainFact("sweep_score", {"value": round(sfp[index].score_bear, 2)}),
                        DomainFact("swept_high_rejected"),
                    ),
                )
            )

        if index >= 2:
            left = bars[index - 2]
            body = abs(bar.close - bar.open)
            if bar.low > left.high:
                gap = bar.low - left.high
                if (
                    gap >= atr * params.fvg_min_atr
                    and body >= atr * params.fvg_displacement_atr
                    and bar.close > bar.open
                ):
                    zones.append(
                        FvgZone(
                            index=index,
                            start_index=index - 2,
                            end_index=index,
                            start_ts=left.ts.isoformat(),
                            end_ts=bar.ts.isoformat(),
                            top=bar.low,
                            bottom=left.high,
                            direction="long",
                            size_atr=gap / atr,
                        )
                    )
            if bar.high < left.low:
                gap = left.low - bar.high
                if (
                    gap >= atr * params.fvg_min_atr
                    and body >= atr * params.fvg_displacement_atr
                    and bar.close < bar.open
                ):
                    zones.append(
                        FvgZone(
                            index=index,
                            start_index=index - 2,
                            end_index=index,
                            start_ts=left.ts.isoformat(),
                            end_ts=bar.ts.isoformat(),
                            top=left.low,
                            bottom=bar.high,
                            direction="short",
                            size_atr=gap / atr,
                        )
                    )

    highs = [point for point in active_pivots if point.type == 1]
    lows = [point for point in active_pivots if point.type == -1]
    levels = []
    if highs:
        levels.append(
            {
                "name": "SMC swing high",
                "price": _round(highs[-1].price),
                "kind": "resistance",
                "role": "swing_high",
            }
        )
    if lows:
        levels.append(
            {
                "name": "SMC swing low",
                "price": _round(lows[-1].price),
                "kind": "support",
                "role": "swing_low",
            }
        )
    if previous_swing_high:
        levels.append(
            {
                "name": "SMC previous high",
                "price": _round(previous_swing_high.price),
                "kind": "resistance",
                "role": "previous_swing_high",
            }
        )
    if previous_swing_low:
        levels.append(
            {
                "name": "SMC previous low",
                "price": _round(previous_swing_low.price),
                "kind": "support",
                "role": "previous_swing_low",
            }
        )

    event_dicts = [event.to_dict() for event in events[-params.max_events :]]
    fvg_zone_dicts = [zone.to_dict() for zone in zones[-params.max_boxes :]]
    order_block_dicts = [ob.to_dict() for ob in order_blocks[-params.max_boxes :]]

    return {
        "version": "smc-core-1",
        "params": {
            "pivot_len": pivot_len,
            "equal_tolerance_atr": params.equal_tolerance_atr,
            "fvg_min_atr": params.fvg_min_atr,
            "ob_min_displacement_atr": params.ob_min_displacement_atr,
            "fvg_displacement_atr": params.fvg_displacement_atr,
            "break_buffer_atr": params.break_buffer_atr,
        },
        "pivots": [_pivot_dict(point) for point in active_pivots[-100:]],
        "events": event_dicts,
        "fvg_zones": fvg_zone_dicts,
        "order_blocks": order_block_dicts,
        "boxes": fvg_zone_dicts + order_block_dicts,
        "levels": levels,
        "latest": event_dicts[-1] if event_dicts else None,
    }
