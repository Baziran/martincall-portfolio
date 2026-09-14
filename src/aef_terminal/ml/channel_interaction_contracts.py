from __future__ import annotations

from enum import Enum


class ChannelInteractionOutcome(str, Enum):
    """Canonical live/training outcome labels for channel interaction."""

    REVERSAL = "reversal"
    ACCEPTED_BREAKOUT = "accepted_breakout"
    UNCLEAR = "unclear"
    NO_TOUCH = "no_touch"
