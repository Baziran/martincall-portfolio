from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ConfirmedBarLogicalProjection:
    """Logical positions of exact anchors in one ordered confirmed-bar series."""

    origin_ts: datetime
    confirmed_through_ts: datetime
    confirmed_count: int
    anchor_indices: Mapping[datetime, int]
