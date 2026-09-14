from __future__ import annotations

from dataclasses import dataclass

from aef_terminal.domain import Bar


@dataclass(frozen=True)
class VsaFact:
    index: int
    bar: Bar
    atr: float
    rng: float
    body: float
    body_share: float
    close_pos: float
    upper_share: float
    lower_share: float
    display_volume: float
    display_avg: float
    rvol: float
    vol_rank: float
    vol_z: float
    move_atr: float
    range_atr: float
    spread_rel: float
    score: float
    close_auction: bool
    signal_allowed: bool
    spring: bool
    upthrust: bool
    up_impulse: bool
    dn_impulse: bool
    buy_fuel: bool
    sell_fuel: bool
    up_exhaust: bool
    dn_exhaust: bool
    absorption: bool
    absorption_level: bool
    absorption_role: str
    near_resistance: bool
    near_support: bool
    primary_code: str
    code: str
    terminal_climax: bool = False
    terminal_kind: str = "none"
    terminal_direction: str = "flat"
    fuel_late_trend: bool = False


def event_direction(code: str) -> str:
    if code in {"EXH_UP", "FUEL_UP", "IMP_UP", "SPRING"}:
        return "long"
    if code in {"EXH_DN", "FUEL_DN", "IMP_DN", "UPTHRUST"}:
        return "short"
    return "flat"
