from collections.abc import Sequence
from math import isfinite

from aef_terminal.domain import Bar, BarState
from aef_terminal.ml.channel_interaction_contracts import ChannelInteractionOutcome


def channel_moving_line_outcome(
    bars: Sequence[Bar],
    bar_slots: Sequence[int],
    *,
    anchor_slot: int,
    anchor_price: float,
    slope_per_slot: float,
    side: str,
    breakout_distance: float,
    reversal_distance: float,
    acceptance_distance: float,
    acceptance_closes: int = 2,
) -> ChannelInteractionOutcome:
    """Label confirmed future bars against one frozen moving channel level.

    The supplied bars are the outcome horizon only. The line is projected on the
    same canonical slot axis used by the saved drawing. OHLC cannot reveal which
    threshold traded first inside a bar, so a bar that reaches both sides is
    deliberately labelled ``unclear`` instead of receiving an invented order.
    """

    if len(bars) != len(bar_slots):
        raise ValueError("bars and bar_slots must have the same length")
    if side not in {"resistance", "support"}:
        raise ValueError("side must be resistance or support")
    if isinstance(anchor_slot, bool) or not isinstance(anchor_slot, int):
        raise TypeError("anchor_slot must be an integer")
    numeric_values = {
        "anchor_price": anchor_price,
        "slope_per_slot": slope_per_slot,
        "breakout_distance": breakout_distance,
        "reversal_distance": reversal_distance,
        "acceptance_distance": acceptance_distance,
    }
    for field_name, value in numeric_values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(float(value))
        ):
            raise ValueError(f"{field_name} must be finite")
    if (
        float(breakout_distance) < 0
        or float(reversal_distance) < 0
        or float(acceptance_distance) < 0
    ):
        raise ValueError("outcome distances must be non-negative")
    if (
        isinstance(acceptance_closes, bool)
        or int(acceptance_closes) != acceptance_closes
        or acceptance_closes < 1
    ):
        raise ValueError("acceptance_closes must be a positive integer")

    previous_slot: int | None = None
    previous_ts = None
    consecutive_accepted_closes = 0
    breakout_reached = False
    direction = 1.0 if side == "resistance" else -1.0

    for bar, raw_slot in zip(bars, bar_slots, strict=True):
        if not bar.closed or bar.state is not BarState.CONFIRMED:
            raise ValueError("channel outcomes require exchange-confirmed closed bars")
        if bar.timeframe != "1m":
            raise ValueError("channel outcomes require confirmed 1m bars")
        if isinstance(raw_slot, bool) or not isinstance(raw_slot, int):
            raise TypeError("bar slots must be integers")
        slot = int(raw_slot)
        if previous_slot is not None and slot != previous_slot + 1:
            raise ValueError("outcome bars must cover every consecutive canonical slot")
        if previous_ts is not None and bar.ts <= previous_ts:
            raise ValueError("outcome bar timestamps must be strictly increasing")
        previous_slot = slot
        previous_ts = bar.ts

        line_price = float(anchor_price) + float(slope_per_slot) * (slot - anchor_slot)
        signed_high = direction * (float(bar.high) - line_price)
        signed_low = direction * (float(bar.low) - line_price)
        signed_close = direction * (float(bar.close) - line_price)
        breakout_excursion = max(signed_high, signed_low)
        reversal_excursion = -min(signed_high, signed_low)
        breakout_hit = breakout_excursion >= float(breakout_distance)
        reversal_hit = reversal_excursion >= float(reversal_distance)
        accepted_close = signed_close >= float(acceptance_distance)

        if reversal_hit and (breakout_hit or accepted_close):
            return ChannelInteractionOutcome.UNCLEAR
        if reversal_hit:
            return ChannelInteractionOutcome.REVERSAL
        breakout_reached = breakout_reached or breakout_hit
        if breakout_reached and accepted_close:
            consecutive_accepted_closes += 1
            if consecutive_accepted_closes >= acceptance_closes:
                return ChannelInteractionOutcome.ACCEPTED_BREAKOUT
        else:
            consecutive_accepted_closes = 0

    return ChannelInteractionOutcome.UNCLEAR
