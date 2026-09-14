"""Trade Setup Engine immutable parameters and state contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aef_terminal.domain import Direction
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, IndicatorDefaults
from aef_terminal.indicators.module_contract import INDICATOR_FACT_RUNTIME_FIELDS
from aef_terminal.runtime.math_utils import bool_param

TRADE_SETUP_ENGINE_VERSION = "1.2-python-confirmed-1m-preview"
SETUP_TYPES = frozenset({"momentum_breakout", "mean_reversion"})
SETUP_STATES = frozenset({"IDLE", "WATCH", "ARMED", "GO", "STOP", "TP", "TP/SL", "TRAIL"})
SETUP_TRANSITION_EVENTS = frozenset(
    {
        "WATCH",
        "ARMED",
        "GO",
        "STOP",
        "TP",
        "TP/SL",
        "TRAIL",
        "EXPIRED",
        "SUPERSEDED",
    }
)

TRADE_SETUP_ENGINE_EVENT_FIELDS = (
    "action",
    "action_reason_code",
    "blocked_reason",
    "direction",
    "entry",
    "event",
    "label",
    "lifecycle",
    "market_context_gate",
    "reason",
    "reason_code",
    "role",
    "raw_action",
    "rr",
    "score",
    "setup_type",
    "signal",
    "state",
    "stop",
    "target",
    "target_2r",
    "trend_context",
    "ts",
    "vsa_breakout_context",
    "vsa_breakout_gate",
    "zone",
    *INDICATOR_FACT_RUNTIME_FIELDS,
)


def resolve_signal_name(_source: str, code: str, _action: str) -> str:
    normalized_code = str(code or "").upper()
    if normalized_code.startswith("SETUP_MR"):
        return "setup_mean_reversion"
    if normalized_code.startswith("SETUP_BD"):
        return "setup_momentum_breakdown"
    if normalized_code.startswith("SETUP_BO"):
        return "setup_momentum_breakout"
    return "trade_setup_engine"


def _momentum_code(direction: Direction) -> str:
    return "BO" if direction == Direction.LONG else "BD"


@dataclass(frozen=True)
class TradeSetupEngineParams:
    atr_len: int = DEFAULT_INDICATOR_SETTINGS.atr_len
    rvol_len: int = DEFAULT_INDICATOR_SETTINGS.rvol_len
    ema_fast_len: int = DEFAULT_INDICATOR_SETTINGS.ema.fast
    ema_slow_len: int = DEFAULT_INDICATOR_SETTINGS.ema.slow
    min_score: float = 78.0
    watch_score: float = 62.0
    grind_len: int = 12
    spike_len: int = 6
    chop_len: int = 16
    zone_atr: float = 0.35
    armed_atr: float = 0.55
    spike_atr_mult: float = 2.2
    target_r: float = 1.85
    target_2r_mult: float = 2.0
    max_setup_bars: int = 48
    trail_atr: float = 1.0
    enable_momentum: bool = True
    enable_mean_reversion: bool = True
    zones: bool = True
    labels: bool = True
    plan: bool = True


def build_params(
    raw: Mapping[str, Any],
    defaults: IndicatorDefaults,
) -> TradeSetupEngineParams:
    return TradeSetupEngineParams(
        atr_len=defaults.atr_len,
        rvol_len=defaults.rvol_len,
        ema_fast_len=defaults.ema.fast,
        ema_slow_len=defaults.ema.slow,
        min_score=float(raw.get("min_score", TradeSetupEngineParams.min_score)),
        watch_score=float(raw.get("watch_score", TradeSetupEngineParams.watch_score)),
        grind_len=int(raw.get("grind_len", TradeSetupEngineParams.grind_len)),
        spike_len=int(raw.get("spike_len", TradeSetupEngineParams.spike_len)),
        target_r=float(raw.get("target_r", TradeSetupEngineParams.target_r)),
        enable_momentum=bool_param(raw.get("enable_momentum"), True),
        enable_mean_reversion=bool_param(
            raw.get("enable_mean_reversion"),
            True,
        ),
        zones=bool_param(raw.get("zones"), True),
        labels=bool_param(raw.get("labels"), True),
        plan=bool_param(raw.get("plan"), True),
    )


@dataclass
class _SetupCandidate:
    setup_type: str
    direction: Direction
    score: float
    zone_top: float
    zone_bottom: float
    reason_code: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ActiveSetup:
    setup_type: str
    direction: Direction
    state: str
    zone_top: float
    zone_bottom: float
    zone_start_ts: str
    score: float
    reason_code: str
    metrics: dict[str, Any]
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    target_2r: float | None = None
    rr: float | None = None
    trail: float | None = None
    armed: bool = False
    go_confirmed: bool = False
    age_bars: int = 0
