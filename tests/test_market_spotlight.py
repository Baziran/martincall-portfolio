from datetime import datetime, timedelta, timezone

from aef_terminal.domain import Bar
from aef_terminal.indicators.modules.market_spotlight import (
    MarketSpotlightParams,
    _first_finite,
    _pullback_reentry_context,
    market_spotlight as _market_spotlight,
)
from aef_terminal.runtime.instruments import PROFILES
from tests.provider_payloads import explicit_vwap_session_for_bars


def market_spotlight(bars, params=None, **kwargs):
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(bars))
    return _market_spotlight(bars, profile=PROFILES["ES"], params=params, **kwargs)


def _bar(
    index: int, open_: float, high: float, low: float, close: float, volume: float = 1200.0
) -> Bar:
    base_ts = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    return Bar(
        symbol="ES",
        ts=base_ts + timedelta(minutes=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe="1m",
        closed=True,
    )


def test_market_spotlight_preserves_zero_and_negative_price_facts() -> None:
    assert _first_finite(None, 0.0, 12.0) == 0.0
    assert _first_finite(None, -1.25, 12.0) == -1.25

    long_reentry = _pullback_reentry_context(
        continuation_pattern={
            "active": True,
            "direction": "short",
            "phase": "watch",
            "break_level": 0.0,
            "pattern_low": -2.0,
            "invalidation": 0.0,
        },
        side_gate_context={},
        latest_ema20=-0.5,
        latest_vwap=-0.25,
    )
    short_reentry = _pullback_reentry_context(
        continuation_pattern={
            "active": True,
            "direction": "long",
            "phase": "watch",
            "break_level": 0.0,
            "pattern_high": 2.0,
            "invalidation": 0.0,
        },
        side_gate_context={},
        latest_ema20=0.5,
        latest_vwap=0.25,
    )

    assert long_reentry["trigger"] == 0.0
    assert long_reentry["stop_reference"] == 0.0
    assert short_reentry["trigger"] == 0.0
    assert short_reentry["stop_reference"] == 0.0


def test_market_spotlight_smoke_market_context() -> None:
    bars: list[Bar] = []
    price = 5000.0
    for index in range(120):
        drift = 0.18 if index % 5 else -0.05
        open_ = price
        close = price + drift
        high = max(open_, close) + 0.25
        low = min(open_, close) - 0.20
        bars.append(_bar(index, open_, high, low, close, volume=1300 + index * 4))
        price = close

    result = market_spotlight(bars)
    latest = result.get("latest") or {}
    context = result.get("market_context") or {}
    table = result.get("table") or {}
    overlays = result.get("overlays") or []

    assert isinstance(result.get("series"), list)
    assert isinstance(result.get("overlays"), list)
    assert result.get("sentiment_role") == "market_context"
    assert latest.get("sentiment_role") == "market_context"
    assert latest.get("market_context") == context
    assert context.get("source") == "market_spotlight"
    assert context.get("direction") in {"long", "short", "flat"}
    assert "flow_bias" in context
    flow_state = context.get("flow_state") or {}
    assert flow_state.get("dominant_direction") == context.get("direction")
    assert flow_state.get("countertrend_direction") in {"long", "short", "flat"}
    assert flow_state.get("directive") in {
        "avoid_short",
        "avoid_long",
        "pullback_watch",
        "follow_flow",
        "wait_balance",
    }
    assert set((flow_state.get("reversal_attempt") or {}).keys()) == {"long", "short"}
    assert set((flow_state.get("countertrend_unlock") or {}).keys()) == {"long", "short"}
    assert "vsa_context_code" in context
    assert context.get("trend_day") == {
        "bull": bool(context["trend_day"]["bull"]),
        "bear": bool(context["trend_day"]["bear"]),
    }
    assert "runaway" in context
    assert "contra_short_locked" in context
    assert "contra_long_locked" in context
    assert context.get("state") in {
        "trap",
        "micro_break",
        "vix_risk",
        "reversal_attempt",
        "absorption",
        "trend_initiation",
        "balance",
        "directional_flow",
        "continuation_pattern",
    }
    assert context.get("state_direction") in {"long", "short", "flat"}
    assert context.get("reason_code")
    assert set((context.get("pressure_votes") or {}).keys()) == {"long", "short"}
    assert context.get("vote_metrics", {}).get("long_vote_count") is not None
    assert context.get("vote_metrics", {}).get("short_vote_count") is not None
    assert "market" not in context
    assert "flow" not in context
    assert "reason" not in context
    assert table.get("scenario") == "market_spotlight"
    assert table.get("model_ref") == "market_spotlight"
    assert "columns" not in table
    assert "accent" not in table
    assert table.get("tone") in {"long", "short", "warning"}
    assert table.get("advisor_visible") is True
    assert overlays[0].get("advisor_visible") is True
    assert overlays[0].get("scenario") == "market_spotlight"
    assert overlays[0].get("metrics", {}).get("long_vote_count") is not None
    assert overlays[0].get("metrics", {}).get("short_vote_count") is not None
    for key in ("entry", "risk", "opponent", "invalidation"):
        facts = context.get(key) or {}
        assert "text" not in facts
        assert "sub" not in facts


def test_market_spotlight_full_table_uses_context_blocks_without_signals() -> None:
    bars: list[Bar] = []
    price = 5000.0
    for index in range(140):
        drift = 0.32 if index > 30 else 0.08
        open_ = price
        close = price + drift
        high = max(open_, close) + 0.35
        low = min(open_, close) - 0.15
        bars.append(_bar(index, open_, high, low, close, volume=1500 + index * 8))
        price = close

    result = market_spotlight(bars, params=MarketSpotlightParams(table_style="full"))
    context = result.get("market_context") or {}
    latest = result.get("latest") or {}
    table = result.get("table") or {}

    assert table.get("style") == "full"
    assert table.get("model_ref") == "market_spotlight"
    assert "columns" not in table
    assert context.get("entry", {}).get("generates_signal") is False
    assert context.get("entry", {}).get("actionable") is False
    assert context.get("entry", {}).get("phase") in {"context", "watch", "blocked"}
    assert {
        "entry",
        "risk",
        "opponent",
        "invalidation",
        "side_gate",
        "entry_gate",
        "flow_state",
    }.issubset(context)
    assert "signal" not in latest
    assert "signals" not in result


def test_market_spotlight_vix_does_not_unlock_short_veto_by_itself() -> None:
    bars: list[Bar] = []
    price = 5000.0
    for index in range(160):
        drift = 0.42 if index > 25 else 0.10
        open_ = price
        close = price + drift
        high = max(open_, close) + 0.25
        low = min(open_, close) - 0.08
        bars.append(_bar(index, open_, high, low, close, volume=1800 + index * 10))
        price = close

    result = market_spotlight(
        bars,
        params=MarketSpotlightParams(table_style="full"),
        vix_context={
            "active": True,
            "valid": True,
            "state": "risk-off",
            "risk_off": True,
            "risk_on": False,
            "shock": True,
        },
    )
    side_gate = (result.get("market_context") or {}).get("side_gate") or {}

    assert side_gate.get("short_block_lock") is True
    assert side_gate.get("short_unlock") is False
    assert side_gate.get("veto_short") is True
    flow_state = (result.get("market_context") or {}).get("flow_state") or {}
    assert flow_state.get("directive") == "avoid_short"


def test_market_spotlight_invalidates_flag_on_confirmed_close_beyond_invalidation() -> None:
    bars: list[Bar] = []
    price = 100.0
    for index in range(80):
        open_ = price
        close = 100.0 + (0.03 if index % 2 else -0.02)
        high = max(open_, close) + 0.2
        low = min(open_, close) - 0.2
        bars.append(_bar(index, open_, high, low, close, volume=1000 + index))
        price = close
    bars[-1] = _bar(79, 100.4, 101.35, 100.25, 101.2, volume=1300)
    pattern = {
        "active": True,
        "type": "bear_flag",
        "label": "BEAR FLAG",
        "direction": "short",
        "phase": "watch",
        "quality": 82.0,
        "break_level": 99.4,
        "invalidation": 101.0,
        "reason_codes": ["bear_flag_watch"],
    }

    result = market_spotlight(
        bars, indicator_context={"impulse_fib": {"continuation_pattern": pattern}}
    )
    context = result["market_context"]

    assert context["continuation_pattern"]["phase"] == "failed"
    assert context["continuation_pattern"]["invalidated"] is True
    assert context["continuation_pattern"]["invalidated_by"] == "confirmed_close"
    assert context["continuation_pattern"]["reason_codes"] == [
        "bear_flag_watch",
        "confirmed_close_beyond_invalidation",
    ]
    assert "reason" not in context["continuation_pattern"]
    assert context["state"] != "continuation_pattern"
    assert "market" not in context
    assert context["side_lock"] != "SHORT"
    assert context["invalidation"]["source"] != "continuation_pattern"
