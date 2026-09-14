from __future__ import annotations

from typing import Any

from aef_terminal.domain import Direction


def direction_from_any(value: Any, default: Direction = Direction.FLAT) -> Direction:
    if isinstance(value, Direction):
        return value
    if isinstance(value, str):
        try:
            return Direction(value)
        except ValueError:
            pass
    return default


def is_trade_direction(direction: Any) -> bool:
    return direction_from_any(direction) in {Direction.LONG, Direction.SHORT}


def opposite_direction(direction: Any) -> Direction:
    resolved = direction_from_any(direction)
    if resolved == Direction.LONG:
        return Direction.SHORT
    if resolved == Direction.SHORT:
        return Direction.LONG
    return Direction.FLAT
