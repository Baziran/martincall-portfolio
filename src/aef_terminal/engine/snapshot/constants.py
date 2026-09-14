from __future__ import annotations

from typing import Literal


type SignalRange = Literal["1d", "2d", "3d", "7d"]

DEFAULT_SIGNAL_RANGE: SignalRange = "2d"
SIGNAL_RANGE_VALUES = frozenset({"1d", "2d", "3d", "7d"})
