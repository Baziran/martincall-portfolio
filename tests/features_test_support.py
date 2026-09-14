from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aef_terminal.domain import Bar, StrategyMode
from aef_terminal.engine.decision import DecisionContext, choose_decision as _choose_decision
from aef_terminal.indicators.modules.breakout_accumulation import (
    breakout_accumulation as _breakout_accumulation,
)
from aef_terminal.indicators.modules.smc_channels import (
    smc_channels as _smc_channels,
    smc_structure as _smc_structure,
)
from aef_terminal.runtime.instruments import PROFILES
from tests.provider_payloads import explicit_vwap_session_for_bars


def instrument_profile_for_test(bars) -> object:
    symbol = str(bars[-1].symbol if bars else "ES").strip().upper()
    key = {"BTC": "CRYPTO", "SPY": "SPY", "QQQ": "QQQ", "GC": "GOLD", "CL": "OIL"}.get(
        symbol,
        "ES",
    )
    return PROFILES[key]


def choose_test_decision(
    candidates,
    *,
    bars=None,
    atr_value=None,
    strategy_mode=StrategyMode.BALANCED,
    option_flow=None,
):
    profile = instrument_profile_for_test(bars or [])
    return _choose_decision(
        DecisionContext(
            candidates=tuple(candidates),
            bars=tuple(bars or ()),
            atr_value=0.0 if atr_value is None else atr_value,
            strategy_mode=strategy_mode,
            option_flow=option_flow,
            instrument_profile=profile,
            price_increment=profile.tick_size,
        )
    )


def breakout_accumulation_for_test(bars, params=None, **kwargs):
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(bars))
    return _breakout_accumulation(
        bars,
        profile=instrument_profile_for_test(bars),
        params=params,
        **kwargs,
    )


def smc_structure_for_test(bars, params=None, **kwargs):
    kwargs.setdefault("provider_session", explicit_vwap_session_for_bars(bars))
    return _smc_structure(
        bars,
        profile=instrument_profile_for_test(bars),
        params=params,
        **kwargs,
    )


def smc_channels_for_test(bars, params=None, **kwargs):
    kwargs.setdefault("provider_session", explicit_vwap_session_for_bars(bars))
    return _smc_channels(
        bars,
        profile=instrument_profile_for_test(bars),
        params=params,
        **kwargs,
    )


def make_bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1000,
    timeframe: str = "1m",
    closed: bool = True,
    symbol: str = "ES",
    base_ts: datetime | None = None,
    step_minutes: int = 1,
) -> Bar:
    base_ts = base_ts or datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    return Bar(
        symbol=symbol,
        ts=base_ts + timedelta(minutes=index * step_minutes),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe=timeframe,
        closed=closed,
    )
