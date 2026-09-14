from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from random import Random
from statistics import pstdev

import pytest

from aef_terminal.domain import Bar, Direction, StrategyMode
from aef_terminal.engine.analyze.indicator_status import attach_indicator_status
from aef_terminal.engine.snapshot import assembly as snapshot_assembly
from aef_terminal.engine.vsa_context import (
    VSA_VOLUME_VERSION,
    VsaVolumeParams,
    build_vsa_volume_context,
)
from aef_terminal.features import vsa_classify as vsa_classification
from aef_terminal.features.price_action import displacement_feature
from aef_terminal.features.provider_session import (
    ProviderSessionInterval,
    ProviderSessionReset,
    previous_provider_session_levels,
    provider_session_level_context,
)
from aef_terminal.features.vsa import VsaFact, vsa_facts
from aef_terminal.runtime import pine
from aef_terminal.features.vsa_classify import (
    VSA_BREAKOUT_CONTEXT_CONTRACT,
    VsaBreakoutFact,
    causal_provider_session_volume_ratio,
    latest_vsa_context_item,
    series_item_from_fact,
    vsa_breakout_facts,
)
from aef_terminal.indicators.contracts import validate_indicator_result_contract
from aef_terminal.indicators.modules import trade_setup_engine as tse
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.indicators.runtime import IndicatorRunContext, IndicatorRuntimeParams
from aef_terminal.runtime.instruments import PROFILES
from aef_terminal.signals.indicator_state import indicator_state_promotion_trace
from aef_terminal.ui import server_alert_runtime
from tests.provider_payloads import (
    explicit_vwap_session_for_bars,
    ibkr_stock_payload,
)


def _bar(
    index: int,
    *,
    close: float,
    open_: float,
    high: float,
    low: float,
    volume: float = 1_000.0,
    timeframe: str = "5m",
) -> Bar:
    return Bar(
        symbol="SPY",
        ts=datetime(2026, 1, 5, 14, 30, tzinfo=UTC) + timedelta(minutes=index * 5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe=timeframe,
    )


def test_vsa_fast_deviation_preserves_facts_and_signal_gates(monkeypatch) -> None:
    rng = Random(763)
    bars = []
    price = 100.0
    for index in range(700):
        previous = price
        price += rng.uniform(-1.5, 1.5)
        bars.append(
            _bar(
                index,
                open_=previous,
                close=price,
                high=max(previous, price) + rng.uniform(0, 1),
                low=min(previous, price) - rng.uniform(0, 1),
                volume=0.0 if index % 23 == 0 else rng.uniform(100, 1e6),
            )
        )
    session = explicit_vwap_session_for_bars(bars)
    actual = vsa_facts(bars, vwap_session=session)

    def reference(values, index, length):
        window = values[max(0, index - max(length, 1) + 1) : index + 1]
        return pstdev(window) if len(window) > 1 else 0.0

    monkeypatch.setattr(pine, "stdev_at", reference)
    expected = vsa_facts(bars, vwap_session=session)
    for first, second in zip(actual, expected, strict=True):
        for field in fields(VsaFact):
            a, b = getattr(first, field.name), getattr(second, field.name)
            if isinstance(a, float):
                assert a == pytest.approx(b, rel=3e-14, abs=1e-14), field.name
            else:
                assert a == b, field.name


def _breakout_bars(
    trend: str,
    break_direction: str,
    *,
    trailing_bar: bool = False,
) -> list[Bar]:
    bars: list[Bar] = []
    for index in range(13):
        close = 100.0 + index if trend == "uptrend" else 120.0 - index
        open_ = close - 0.2 if trend == "uptrend" else close + 0.2
        bars.append(
            _bar(
                index,
                close=close,
                open_=open_,
                high=max(open_, close) + 0.4,
                low=min(open_, close) - 0.4,
                volume=1_000.0 + index * 10.0,
            )
        )
    signal = bars[-1]
    if break_direction == "short":
        bars.append(
            _bar(
                13,
                close=signal.low - 0.4,
                open_=signal.close,
                high=signal.high - 0.1,
                low=signal.low - 1.0,
                volume=1_500.0,
            )
        )
    else:
        bars.append(
            _bar(
                13,
                close=signal.high + 0.6,
                open_=signal.close,
                high=signal.high + 1.0,
                low=signal.low + 0.1,
                volume=1_500.0,
            )
        )
    if trailing_bar:
        prior = bars[-1]
        bars.append(
            _bar(
                14,
                close=prior.close,
                open_=prior.close,
                high=prior.close + 0.2,
                low=prior.close - 0.2,
                volume=1_100.0,
            )
        )
    return bars


def _force_primary(
    facts: list[VsaFact],
    index: int,
    code: str,
) -> list[VsaFact]:
    flags = {
        "signal_allowed": True,
        "upthrust": False,
        "spring": False,
        "up_exhaust": False,
        "dn_exhaust": False,
        "absorption_level": False,
        "absorption": False,
        "buy_fuel": False,
        "sell_fuel": False,
        "up_impulse": False,
        "dn_impulse": False,
        "primary_code": code,
        "code": code,
    }
    if code == "UPTHRUST":
        flags["upthrust"] = True
    elif code == "ABS":
        flags["absorption"] = True
    elif code == "ABS_LVL":
        flags["absorption"] = True
        flags["absorption_level"] = True
    elif code == "EXH_UP":
        flags["dn_exhaust"] = True
    elif code == "EXH_DN":
        flags["up_exhaust"] = True
    else:
        raise AssertionError(f"unsupported test VSA code: {code}")
    resolved = list(facts)
    resolved[index] = replace(resolved[index], **flags)
    return resolved


def _breakout_scenario(
    *,
    profile_key: str,
    trend: str,
    code: str,
    break_direction: str,
    trailing_bar: bool = False,
) -> tuple[
    list[Bar],
    list[VsaFact],
    list[VsaBreakoutFact],
]:
    bars = _breakout_bars(
        trend,
        break_direction,
        trailing_bar=trailing_bar,
    )
    session = explicit_vwap_session_for_bars(bars)
    facts = _force_primary(
        vsa_facts(bars, vwap_session=session),
        12,
        code,
    )
    if trailing_bar:
        facts[13] = replace(
            facts[13],
            signal_allowed=False,
            primary_code="",
            code="",
        )
    breakout = vsa_breakout_facts(
        bars,
        facts,
        profile_key=profile_key,
        vwap_session=session,
        confirmed_slots=[index * 5 for index in range(len(bars))],
    )
    return bars, facts, breakout


def test_provider_session_volume_ratio_is_prefix_causal() -> None:
    bars = [
        Bar(
            symbol="SPY",
            ts=datetime(2026, 1, 5 + index, 14, 30, tzinfo=UTC),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=volume,
            timeframe="5m",
        )
        for index, volume in enumerate((100.0, 200.0, 300.0))
    ]

    session = explicit_vwap_session_for_bars(bars)
    full = causal_provider_session_volume_ratio(bars, session)

    assert full == [1.0, 2.0, 2.0]
    for end in range(1, len(bars) + 1):
        prefix = bars[:end]
        assert (
            causal_provider_session_volume_ratio(
                prefix,
                explicit_vwap_session_for_bars(prefix),
            )
            == full[:end]
        )


def test_volume_ratio_uses_provider_session_position_across_dst() -> None:
    bars = [
        Bar(
            symbol="SPY",
            ts=timestamp,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=volume,
            timeframe="5m",
        )
        for timestamp, volume in (
            (datetime(2026, 3, 6, 14, 30, tzinfo=UTC), 100.0),
            (datetime(2026, 3, 9, 13, 30, tzinfo=UTC), 200.0),
        )
    ]
    session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="",
        calendar="us_equities",
        intervals=(
            ProviderSessionInterval(
                opens_at=datetime(2026, 3, 6, 14, 30, tzinfo=UTC),
                closes_at=datetime(2026, 3, 6, 21, 0, tzinfo=UTC),
                session_date=date(2026, 3, 6),
            ),
            ProviderSessionInterval(
                opens_at=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
                closes_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
                session_date=date(2026, 3, 9),
            ),
        ),
    )

    assert causal_provider_session_volume_ratio(bars, session) == [1.0, 2.0]


def test_volume_ratio_excludes_provider_declared_session_breaks() -> None:
    bars = [
        Bar(
            symbol="TEST",
            ts=timestamp,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=volume,
            timeframe="5m",
        )
        for timestamp, volume in (
            (datetime(2026, 3, 6, 18, 0, tzinfo=UTC), 100.0),
            (datetime(2026, 3, 9, 17, 0, tzinfo=UTC), 200.0),
        )
    ]
    session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="",
        calendar="split_session",
        intervals=(
            ProviderSessionInterval(
                opens_at=datetime(2026, 3, 6, 14, 0, tzinfo=UTC),
                closes_at=datetime(2026, 3, 6, 17, 0, tzinfo=UTC),
                session_date=date(2026, 3, 6),
            ),
            ProviderSessionInterval(
                opens_at=datetime(2026, 3, 6, 18, 0, tzinfo=UTC),
                closes_at=datetime(2026, 3, 6, 21, 0, tzinfo=UTC),
                session_date=date(2026, 3, 6),
            ),
            ProviderSessionInterval(
                opens_at=datetime(2026, 3, 9, 13, 0, tzinfo=UTC),
                closes_at=datetime(2026, 3, 9, 16, 0, tzinfo=UTC),
                session_date=date(2026, 3, 9),
            ),
            ProviderSessionInterval(
                opens_at=datetime(2026, 3, 9, 17, 0, tzinfo=UTC),
                closes_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
                session_date=date(2026, 3, 9),
            ),
        ),
    )

    assert causal_provider_session_volume_ratio(bars, session) == [1.0, 2.0]


def test_previous_levels_follow_provider_sessions() -> None:
    first_day = [
        _bar(0, close=100.0, open_=99.8, high=101.0, low=99.0),
        _bar(1, close=101.0, open_=100.0, high=103.0, low=98.0),
    ]
    second_day = [
        replace(first_day[0], ts=first_day[0].ts + timedelta(days=1)),
        replace(first_day[1], ts=first_day[1].ts + timedelta(days=1)),
    ]
    bars = [*first_day, *second_day]
    session = ProviderSessionReset(
        available=True,
        source="test",
        reason_code="ok",
        calendar="test",
        intervals=(
            ProviderSessionInterval(
                opens_at=datetime(2026, 1, 5, 14, 0, tzinfo=UTC),
                closes_at=datetime(2026, 1, 5, 21, 0, tzinfo=UTC),
                session_date=date(2026, 1, 5),
            ),
            ProviderSessionInterval(
                opens_at=datetime(2026, 1, 6, 14, 0, tzinfo=UTC),
                closes_at=datetime(2026, 1, 6, 21, 0, tzinfo=UTC),
                session_date=date(2026, 1, 6),
            ),
        ),
    )

    previous_highs, previous_lows = previous_provider_session_levels(
        bars,
        session,
    )

    assert previous_highs == [None, None, 103.0, 103.0]
    assert previous_lows == [None, None, 98.0, 98.0]


def test_session_levels_follow_provider_intervals_across_utc_midnight() -> None:
    previous_open = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
    current_open = datetime(2026, 1, 6, 23, 30, tzinfo=UTC)

    def session_bars(opens_at: datetime, base: float) -> list[Bar]:
        return [
            Bar(
                symbol="SPY",
                ts=opens_at + timedelta(minutes=index * 5),
                open=base + index - 0.25,
                high=base + index,
                low=base - 1.0 - index,
                close=base + index - 0.1,
                volume=1_000.0,
                timeframe="5m",
            )
            for index in range(12)
        ]

    bars = [
        *session_bars(previous_open, 100.0),
        *session_bars(current_open, 200.0),
    ]
    session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="ok",
        calendar="overnight_test",
        intervals=(
            ProviderSessionInterval(
                opens_at=previous_open,
                closes_at=previous_open + timedelta(hours=3),
                session_date=date(2026, 1, 5),
            ),
            ProviderSessionInterval(
                opens_at=current_open,
                closes_at=current_open + timedelta(hours=3),
                session_date=date(2026, 1, 6),
            ),
        ),
    )

    context = provider_session_level_context(bars, session)

    assert context.available is True
    assert context.session_key == "2026-01-06"
    assert context.opening_range_complete is True
    assert context.opening_range_high == 205.0
    assert context.opening_range_low == 194.0
    assert context.initial_balance_complete is True
    assert context.initial_balance_high == 211.0
    assert context.initial_balance_low == 188.0
    assert context.previous_session_high == 111.0
    assert context.previous_session_low == 88.0
    assert context.current_session_high == 211.0
    assert context.current_session_low == 188.0


def test_session_levels_fail_typed_when_provider_session_is_unknown() -> None:
    context = provider_session_level_context(
        [_bar(0, close=100.0, open_=99.8, high=101.0, low=99.0)],
        None,
    )

    assert context.available is False
    assert context.reason_code == "provider_session_missing"
    assert context.opening_range_high is None
    assert context.previous_session_high is None


def test_terminal_climax_requires_exact_next_provider_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = [
        _bar(0, close=101.0, open_=100.0, high=102.0, low=99.5),
        _bar(1, close=100.0, open_=101.0, high=101.2, low=99.8),
    ]
    session = explicit_vwap_session_for_bars(bars)
    facts = vsa_facts(bars)
    facts[0] = replace(
        facts[0],
        score=99.0,
        near_resistance=True,
    )
    monkeypatch.setattr(vsa_classification, "_ultra_volume", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        vsa_classification,
        "_classic_buying_climax_bar",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        vsa_classification,
        "_mature_uptrend",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        vsa_classification,
        "_confirm_after_buying_climax",
        lambda *_args, **_kwargs: True,
    )
    common = {
        "facts": facts,
        "bars": bars,
        "up_ages": [8, 8],
        "dn_ages": [0, 0],
        "atr_values": [1.0, 1.0],
        "ema20": [100.0, 100.0],
        "prev_highs": [None, None],
        "prev_lows": [None, None],
        "vwap": [100.0, 100.0],
        "vwap_stdev": [1.0, 1.0],
        "tod_volume_ratio": [3.0, 1.0],
        "vwap_session": session,
    }

    assert vsa_classification._resolve_terminal_markers(
        **common,
        confirmed_slots=[0, 5],
    ) == {1: ("clx_exh", "short")}
    assert (
        vsa_classification._resolve_terminal_markers(
            **common,
            confirmed_slots=[0, 10],
        )
        == {}
    )
    split_session = ProviderSessionReset(
        available=True,
        source="test",
        reason_code="ok",
        calendar="test",
        intervals=(
            ProviderSessionInterval(
                opens_at=bars[0].ts,
                closes_at=bars[1].ts,
                session_date=date(2026, 1, 5),
            ),
            ProviderSessionInterval(
                opens_at=bars[1].ts,
                closes_at=bars[1].ts + timedelta(minutes=5),
                session_date=date(2026, 1, 6),
            ),
        ),
    )
    assert (
        vsa_classification._resolve_terminal_markers(
            **{**common, "vwap_session": split_session},
            confirmed_slots=[0, 5],
        )
        == {}
    )


@pytest.mark.parametrize(
    (
        "profile_key",
        "trend",
        "code",
        "expected_tier",
        "expected_setup",
        "expected_reason",
    ),
    (
        (
            "SPY",
            "downtrend",
            "EXH_UP",
            "a",
            "momentum_breakout",
            "vsa_spy_exh_up_downtrend_short_break",
        ),
        (
            "SPY",
            "uptrend",
            "ABS",
            "b",
            "mean_reversion",
            "vsa_spy_abs_uptrend_short_break",
        ),
        (
            "QQQ",
            "downtrend",
            "EXH_DN",
            "a",
            "momentum_breakout",
            "vsa_qqq_exh_dn_downtrend_short_break",
        ),
    ),
)
def test_vsa_short_breakout_policy_is_profile_and_trend_specific(
    profile_key: str,
    trend: str,
    code: str,
    expected_tier: str,
    expected_setup: str,
    expected_reason: str,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key=profile_key,
        trend=trend,
        code=code,
        break_direction="short",
    )

    fact = breakout[-1]

    assert fact.signal_index == 12
    assert fact.signal_ts == bars[12].ts.isoformat()
    assert fact.signal_code == code
    assert fact.prior_trend == trend
    assert fact.short_break is True
    assert fact.short_disposition == "supported"
    assert fact.short_tier == expected_tier
    assert fact.short_setup_type == expected_setup
    assert fact.short_reason_code == expected_reason
    assert fact.availability_state == "ready"
    assert fact.reason_code == "ok"


@pytest.mark.parametrize(
    ("profile_key", "trend", "code"),
    (
        ("SPY", "uptrend", "EXH_UP"),
        ("SPY", "downtrend", "ABS"),
        ("QQQ", "uptrend", "EXH_DN"),
    ),
)
def test_vsa_short_breakout_policy_rejects_wrong_trend(
    profile_key: str,
    trend: str,
    code: str,
) -> None:
    _, _, breakout = _breakout_scenario(
        profile_key=profile_key,
        trend=trend,
        code=code,
        break_direction="short",
    )

    fact = breakout[-1]

    assert fact.short_break is True
    assert fact.short_disposition == "neutral"
    assert fact.short_tier == ""
    assert fact.short_setup_type == ""
    assert fact.short_reason_code == ""


@pytest.mark.parametrize(
    "code",
    ("UPTHRUST", "ABS", "ABS_LVL"),
)
def test_vsa_spy_long_break_avoid_policy(code: str) -> None:
    _, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="uptrend",
        code=code,
        break_direction="long",
    )

    fact = breakout[-1]

    assert fact.long_break is True
    assert fact.long_disposition == "avoid"
    assert fact.long_reason_code == f"vsa_spy_{code.lower()}_long_break_avoid"


def test_vsa_breakout_policy_is_neutral_for_uncalibrated_profile() -> None:
    _, _, breakout = _breakout_scenario(
        profile_key="ES",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )

    fact = breakout[-1]

    assert fact.policy == "neutral"
    assert fact.short_break is True
    assert fact.short_disposition == "neutral"
    assert fact.long_disposition == "neutral"


def test_vsa_breakout_fact_expires_after_exact_next_bar() -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
        trailing_bar=True,
    )

    assert breakout[13].signal_ts == bars[12].ts.isoformat()
    assert breakout[13].short_disposition == "supported"
    assert breakout[14].signal_index is None
    assert breakout[14].signal_ts == ""
    assert breakout[14].signal_code == ""
    assert breakout[14].short_disposition == "neutral"


def test_vsa_breakout_fact_rejects_missing_provider_slot() -> None:
    bars = _breakout_bars("downtrend", "short")
    session = explicit_vwap_session_for_bars(bars)
    facts = _force_primary(
        vsa_facts(bars, vwap_session=session),
        12,
        "EXH_UP",
    )
    slots = [index * 5 for index in range(len(bars))]
    slots[-1] = slots[-2] + 10

    fact = vsa_breakout_facts(
        bars,
        facts,
        profile_key="SPY",
        vwap_session=session,
        confirmed_slots=slots,
    )[-1]

    assert fact.availability_state == "unavailable"
    assert fact.reason_code == "next_provider_slot_missing"
    assert fact.signal_index is None
    assert fact.signal_code == ""
    assert fact.short_break is False
    assert fact.short_disposition == "neutral"


def test_vsa_breakout_fact_blocks_without_provider_slot_axis() -> None:
    bars = _breakout_bars("downtrend", "short")
    session = explicit_vwap_session_for_bars(bars)
    facts = _force_primary(
        vsa_facts(bars, vwap_session=session),
        12,
        "EXH_UP",
    )

    fact = vsa_breakout_facts(
        bars,
        facts,
        profile_key="SPY",
        vwap_session=session,
    )[-1]

    assert fact.availability_state == "blocked"
    assert fact.reason_code == "provider_slot_axis_unavailable"
    assert fact.signal_index is None
    assert fact.signal_code == ""
    assert fact.short_break is False
    assert fact.short_disposition == "neutral"


def test_vsa_breakout_fact_marks_two_sided_break_as_ambiguous() -> None:
    bars = _breakout_bars("uptrend", "long")
    signal = bars[-2]
    bars[-1] = replace(
        bars[-1],
        high=signal.high + 1.0,
        low=signal.low - 1.0,
    )
    session = explicit_vwap_session_for_bars(bars)
    facts = _force_primary(
        vsa_facts(bars, vwap_session=session),
        12,
        "ABS",
    )

    fact = vsa_breakout_facts(
        bars,
        facts,
        profile_key="SPY",
        vwap_session=session,
        confirmed_slots=[index * 5 for index in range(len(bars))],
    )[-1]

    assert fact.long_break is True
    assert fact.short_break is True
    assert fact.availability_state == "unavailable"
    assert fact.reason_code == "ambiguous_two_sided_break"
    assert fact.long_disposition == "neutral"
    assert fact.short_disposition == "neutral"


def test_vsa_breakout_fact_requires_exact_aligned_inputs() -> None:
    bars = _breakout_bars("downtrend", "short")
    session = explicit_vwap_session_for_bars(bars)
    facts = vsa_facts(bars, vwap_session=session)
    wrong_index = list(facts)
    wrong_index[0] = replace(wrong_index[0], index=1)

    with pytest.raises(
        ValueError,
        match="exact aligned VSA fact series",
    ):
        vsa_breakout_facts(
            bars,
            wrong_index,
            profile_key="SPY",
            vwap_session=session,
        )
    with pytest.raises(
        ValueError,
        match="exact aligned provider-slot series",
    ):
        vsa_breakout_facts(
            bars,
            facts,
            profile_key="SPY",
            vwap_session=session,
            confirmed_slots=[0],
        )


def test_engine_vsa_context_publishes_breakout_contract() -> None:
    bars, _, _ = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )
    session = explicit_vwap_session_for_bars(bars)
    params = VsaVolumeParams(
        use_profile_thresholds=False,
        compress_close=False,
    )
    facts = _force_primary(
        vsa_facts(
            bars,
            params,
            vwap_session=session,
        ),
        12,
        "EXH_UP",
    )
    breakout = vsa_breakout_facts(
        bars,
        facts,
        profile_key="SPY",
        vwap_session=session,
        confirmed_slots=[index * 5 for index in range(len(bars))],
    )

    result = build_vsa_volume_context(
        bars,
        profile=PROFILES["SPY"],
        params=params,
        facts=facts,
        breakout_facts=breakout,
        confirmed_slots=[index * 5 for index in range(len(bars))],
        vwap_session=session,
    )

    context = result["breakout_context"]
    assert result["version"] == VSA_VOLUME_VERSION
    assert context["contract"] == VSA_BREAKOUT_CONTEXT_CONTRACT
    assert context["mode"] == "context_only"
    assert context["policy"] == "spy-research-v1"
    assert context["latest"]["short_tier"] == "a"
    assert context["events"][-1]["short_disposition"] == "supported"
    assert result["series"][-1]["breakout_context"] == context["latest"]


def test_engine_vsa_context_reports_missing_slot_axis_as_degraded() -> None:
    bars = [
        _bar(0, close=100.0, open_=99.8, high=100.4, low=99.5),
        _bar(1, close=100.5, open_=100.0, high=100.8, low=99.9),
    ]
    session = explicit_vwap_session_for_bars(bars)

    result = build_vsa_volume_context(
        bars,
        profile=PROFILES["SPY"],
        vwap_session=session,
    )

    assert result["status"]["state"] == "degraded"
    assert result["status"]["reason_code"] == "provider_slot_axis_unavailable"
    assert result["availability"] == {
        "state": "degraded",
        "reason_code": "provider_slot_axis_unavailable",
        "missing_context": "canonical_provider_bar_slots",
    }
    assert result["breakout_context"]["availability"] == {
        "state": "blocked",
        "reason_code": "provider_slot_axis_unavailable",
    }


def test_engine_vsa_context_keeps_terminal_confirm_without_primary_code() -> None:
    bars = [
        _bar(0, close=101.0, open_=100.0, high=102.0, low=99.5),
        _bar(1, close=100.0, open_=101.0, high=101.2, low=99.8),
    ]
    session = explicit_vwap_session_for_bars(bars)
    slots = [0, 5]
    facts = vsa_facts(
        bars,
        vwap_session=session,
        confirmed_slots=slots,
    )
    facts[0] = replace(
        facts[0],
        primary_code="",
        code="",
        terminal_climax=True,
        terminal_kind="clx_exh",
        terminal_direction="short",
    )
    breakout = vsa_breakout_facts(
        bars,
        facts,
        profile_key="SPY",
        vwap_session=session,
        confirmed_slots=slots,
    )

    result = build_vsa_volume_context(
        bars,
        profile=PROFILES["SPY"],
        facts=facts,
        breakout_facts=breakout,
        confirmed_slots=slots,
        vwap_session=session,
    )

    event = next(item for item in result["events"] if item["ts"] == bars[0].ts.isoformat())
    assert event["code"] == "CLX_EXH"
    assert event["base_code"] == ""
    assert event["terminal_climax"] is True
    assert event["terminal_kind"] == "clx_exh"
    assert event["important"] is True
    assert event["reason_code"] == "vsa_clx_exh"
    assert event["trigger_event"]["code"] == "clx_exh"
    assert result["overlays"][0]["code"] == "CLX_EXH"


def test_latest_vsa_context_retains_terminal_event_across_empty_bar() -> None:
    bars = [
        _bar(0, close=101.0, open_=100.0, high=102.0, low=99.5),
        _bar(1, close=100.0, open_=101.0, high=101.2, low=99.8),
    ]
    facts = vsa_facts(bars, vwap_session=explicit_vwap_session_for_bars(bars))
    terminal = series_item_from_fact(
        replace(
            facts[0],
            primary_code="",
            code="",
            terminal_climax=True,
            terminal_kind="clx_exh",
            terminal_direction="short",
        )
    )
    empty = series_item_from_fact(
        replace(
            facts[1],
            primary_code="",
            code="",
            terminal_climax=False,
            terminal_kind="none",
            terminal_direction="flat",
        )
    )

    latest = latest_vsa_context_item([terminal, empty])

    assert latest is terminal
    assert latest["code"] == "CLX_EXH"
    assert latest["terminal_climax"] is True


def test_chart_series_replacement_preserves_only_canonical_breakout_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = [
        _bar(0, close=100.0, open_=99.8, high=100.4, low=99.5),
        _bar(1, close=100.5, open_=100.0, high=100.8, low=99.9),
    ]
    canonical_fact = {
        "contract": VSA_BREAKOUT_CONTEXT_CONTRACT,
        "signal_code": "ABS",
        "reason_code": "canonical",
    }
    display_only_fact = {
        "contract": VSA_BREAKOUT_CONTEXT_CONTRACT,
        "signal_code": "UPTHRUST",
        "reason_code": "display_recalculation",
    }
    snapshot = {
        "vsa_volume": {
            "series": [
                {
                    "ts": bars[-1].ts.isoformat(),
                    "breakout_context": canonical_fact,
                    "terminal_climax": True,
                    "terminal_kind": "clx_exh",
                    "terminal_direction": "short",
                    "direction": "short",
                    "vsa_role": "terminal_climax",
                    "metrics": {
                        "terminal_climax": True,
                        "terminal_kind": "clx_exh",
                        "terminal_direction": "short",
                    },
                },
            ],
            "breakout_context": {"latest": canonical_fact},
            "status": {},
        },
    }
    monkeypatch.setattr(
        snapshot_assembly,
        "build_vsa_volume_context_for_instrument",
        lambda *_args, **_kwargs: {
            "series": [
                {
                    "ts": bar.ts.isoformat(),
                    "breakout_context": display_only_fact,
                    "terminal_climax": False,
                    "terminal_kind": "none",
                    "terminal_direction": "flat",
                    "direction": "flat",
                    "vsa_role": "neutral",
                    "metrics": {"terminal_climax": False},
                }
                for bar in bars
            ],
        },
    )

    snapshot_assembly._replace_vsa_chart_series(
        snapshot,
        calculation_bars=bars,
        display_axis=[
            {
                "ts": bar.ts.isoformat(),
                "closed": True,
                "state": "confirmed",
                "bar_slot": index * 5,
            }
            for index, bar in enumerate(bars)
        ],
        instrument={},
        indicator_params={},
        confirmed_slots=[0, 5],
    )

    assert snapshot["vsa_volume"]["series"][0]["breakout_context"] is None
    assert snapshot["vsa_volume"]["series"][1]["breakout_context"] == canonical_fact
    assert snapshot["vsa_volume"]["series"][1]["terminal_climax"] is True
    assert snapshot["vsa_volume"]["series"][1]["terminal_kind"] == "clx_exh"
    assert snapshot["vsa_volume"]["series"][1]["direction"] == "short"
    assert snapshot["vsa_volume"]["series"][1]["vsa_role"] == "terminal_climax"
    assert snapshot["vsa_volume"]["series"][1]["metrics"]["terminal_climax"] is True
    assert snapshot["vsa_volume"]["breakout_context"]["latest"] == canonical_fact


def test_server_vsa_alert_fails_soft_when_slot_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    instrument = ibkr_stock_payload("SPY")
    bars = [
        _bar(0, close=100.0, open_=99.8, high=100.4, low=99.5),
        _bar(1, close=100.5, open_=100.0, high=100.8, low=99.9),
    ]
    monkeypatch.setattr(
        server_alert_runtime,
        "_server_alert_recent_bars",
        lambda *_args, **_kwargs: server_alert_runtime.RecentConfirmedBarContext(
            generation=0,
            requested_limit=len(bars),
            bars=tuple(bars),
        ),
    )
    monkeypatch.setattr(
        server_alert_runtime,
        "recent_confirmed_bar_slots",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("slot failure")),
    )

    result = server_alert_runtime.server_vsa_fuel_alert_state(
        object(),
        {
            "kind": "vsa_fuel",
            "instrument_id": instrument["instrument_id"],
            "route_fingerprint": server_alert_runtime.route_instrument(
                instrument,
            ).fingerprint,
            "timeframe": "5m",
        },
        100.0,
        instrument=instrument,
    )

    assert result is None
    assert "server_alert_context_unavailable" in caplog.text
    assert "op=build_vsa_volume_context" in caplog.text


def test_server_vsa_alert_projection_uses_only_canonical_ordered_events() -> None:
    event = {
        "ts": "2026-08-15T09:55:00+00:00",
        "code": "FUEL_UP",
        "fuel": True,
        "terminal_climax": False,
    }
    presentation_only = {
        "ts": "2026-08-15T10:00:00+00:00",
        "code": "FUEL_DN",
        "fuel": True,
        "terminal_climax": False,
    }

    events = server_alert_runtime._require_vsa_fuel_events(
        {
            "contract": server_alert_runtime.VSA_VOLUME_CONTRACT,
            "version": server_alert_runtime.VSA_VOLUME_VERSION,
            "status": {"state": "ready"},
            "events": [event],
            "latest": presentation_only,
        }
    )

    assert events == (event,)


def test_server_vsa_alert_projection_rejects_timestamp_alias_and_reordering() -> None:
    base = {
        "contract": server_alert_runtime.VSA_VOLUME_CONTRACT,
        "version": server_alert_runtime.VSA_VOLUME_VERSION,
        "status": {"state": "ready"},
    }
    alias_event = {
        "timestamp": "2026-08-15T09:55:00+00:00",
        "code": "FUEL_UP",
        "fuel": True,
        "terminal_climax": False,
    }
    with pytest.raises(ValueError, match="SERVER_ALERT_VSA_EVENT_TS_INVALID"):
        server_alert_runtime._require_vsa_fuel_events({**base, "events": [alias_event]})

    later = {
        "ts": "2026-08-15T10:00:00+00:00",
        "code": "FUEL_UP",
        "fuel": True,
        "terminal_climax": False,
    }
    earlier = {**later, "ts": "2026-08-15T09:55:00+00:00"}
    with pytest.raises(ValueError, match="SERVER_ALERT_VSA_EVENT_ORDER_INVALID"):
        server_alert_runtime._require_vsa_fuel_events({**base, "events": [later, earlier]})


def test_tse_vsa_gate_preserves_tier_routes() -> None:
    _, _, tier_a = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )
    _, _, tier_b = _breakout_scenario(
        profile_key="SPY",
        trend="uptrend",
        code="ABS",
        break_direction="short",
    )

    gate_a = tse._vsa_breakout_gate(
        Direction.SHORT,
        "momentum_breakout",
        tier_a[-1],
    )
    gate_b = tse._vsa_breakout_gate(
        Direction.SHORT,
        "mean_reversion",
        tier_b[-1],
    )
    tier_b_momentum = tse._vsa_breakout_gate(
        Direction.SHORT,
        "momentum_breakout",
        tier_b[-1],
    )

    assert gate_a["active"] is True
    assert gate_a["blocked"] is False
    assert gate_a["score_adjust"] == 8.0
    assert gate_a["tier"] == "a"
    assert gate_b["active"] is True
    assert gate_b["score_adjust"] == 6.0
    assert gate_b["tier"] == "b"
    assert tier_b_momentum == {
        "active": False,
        "blocked": False,
        "reason_code": "",
        "score_adjust": 0.0,
        "tier": "",
        "context": {},
    }


def test_tse_vsa_long_avoid_is_a_promotion_veto() -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="uptrend",
        code="UPTHRUST",
        break_direction="long",
    )
    gate = tse._vsa_breakout_gate(
        Direction.LONG,
        "momentum_breakout",
        breakout[-1],
    )
    setup = tse._ActiveSetup(
        setup_type="momentum_breakout",
        direction=Direction.LONG,
        state="ARMED",
        zone_top=bars[-1].high,
        zone_bottom=bars[-1].low,
        zone_start_ts=bars[-1].ts.isoformat(),
        score=80.0,
        reason_code="setup_momentum_breakout",
        metrics={},
    )

    decision = tse._setup_action_decision(
        setup,
        tse.TradeSetupEngineParams(),
        trend_context={"alignment": "aligned"},
        market_gate={},
        vsa_gate=gate,
    )

    assert gate["active"] is True
    assert gate["blocked"] is True
    assert gate["score_adjust"] == -100.0
    assert decision == {
        "action": "BLOCK",
        "raw_action": "ARM",
        "reason_code": "vsa_spy_upthrust_long_break_avoid",
        "blocked_reason": "vsa_spy_upthrust_long_break_avoid",
    }
    watch_decision = tse._setup_action_decision(
        replace(setup, state="WATCH"),
        tse.TradeSetupEngineParams(),
        trend_context={"alignment": "aligned"},
        market_gate={},
        vsa_gate=gate,
    )
    assert watch_decision == {
        "action": "BLOCK",
        "raw_action": "WATCH",
        "reason_code": "vsa_spy_upthrust_long_break_avoid",
        "blocked_reason": "vsa_spy_upthrust_long_break_avoid",
    }


def test_tse_vsa_long_avoid_rejects_new_momentum_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="uptrend",
        code="UPTHRUST",
        break_direction="long",
    )

    def score_momentum(
        _bars,
        _index,
        direction,
        **_kwargs,
    ):
        if direction != Direction.LONG:
            return None
        return tse._SetupCandidate(
            setup_type="momentum_breakout",
            direction=direction,
            score=80.0,
            zone_top=101.0,
            zone_bottom=100.0,
            reason_code="setup_momentum_breakout",
            metrics={},
        )

    monkeypatch.setattr(
        tse,
        "_score_momentum_candidate",
        score_momentum,
    )
    params = tse.TradeSetupEngineParams(
        enable_mean_reversion=False,
    )

    candidate = tse._best_candidate(
        bars,
        len(bars) - 1,
        atr=1.0,
        rvol_values=[1.0] * len(bars),
        ema_fast=[1.0] * len(bars),
        ema_slow=[1.0] * len(bars),
        closes=[bar.close for bar in bars],
        features={},
        params=params,
        vsa_breakout_fact=breakout[-1],
    )

    assert candidate is None


def test_tse_vsa_long_avoid_blocks_watch_to_go_in_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="uptrend",
        code="UPTHRUST",
        break_direction="long",
    )
    final_bar = bars[-1]

    def best_candidate(_bars, index, **_kwargs):
        if index != len(bars) - 2:
            return None
        return tse._SetupCandidate(
            setup_type="momentum_breakout",
            direction=Direction.LONG,
            score=90.0,
            zone_top=final_bar.close - 0.1,
            zone_bottom=final_bar.close - 0.5,
            reason_code="setup_momentum_breakout",
            metrics={},
        )

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        best_candidate,
    )

    result = tse.trade_setup_engine(
        bars,
        params=tse.TradeSetupEngineParams(
            enable_mean_reversion=False,
        ),
        vsa_breakout_facts=breakout,
    )

    assert result["latest"]["state"] == "ARMED"
    assert result["latest"]["action"] == "BLOCK"
    assert result["latest"]["blocked_reason"] == "vsa_spy_upthrust_long_break_avoid"
    assert result["latest"]["signal"]["blocked"] is True
    normalized = attach_indicator_status(
        "trade_setup_engine",
        result,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )
    assert normalized["status"]["state_code"] == "blocked_signal"
    assert normalized["status"]["blocked_signal"] is True
    assert normalized["status"]["has_signal"] is True
    trace = indicator_state_promotion_trace(
        result,
        source="trade_setup_engine",
        score_floor=0.0,
    )
    assert trace["promoted"] is False
    assert trace["reject_reason"] == "blocked_signal"


def test_tse_vsa_bonus_can_raise_near_threshold_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )

    def score_momentum(
        _bars,
        _index,
        direction,
        **_kwargs,
    ):
        if direction != Direction.SHORT:
            return None
        return tse._SetupCandidate(
            setup_type="momentum_breakout",
            direction=direction,
            score=56.0,
            zone_top=101.0,
            zone_bottom=100.0,
            reason_code="setup_momentum_breakdown",
            metrics={},
        )

    monkeypatch.setattr(
        tse,
        "_score_momentum_candidate",
        score_momentum,
    )
    params = tse.TradeSetupEngineParams(
        watch_score=62.0,
        min_score=78.0,
        enable_mean_reversion=False,
    )

    candidate = tse._best_candidate(
        bars,
        len(bars) - 1,
        atr=1.0,
        rvol_values=[1.0] * len(bars),
        ema_fast=[1.0] * len(bars),
        ema_slow=[1.0] * len(bars),
        closes=[bar.close for bar in bars],
        features={},
        params=params,
        vsa_breakout_fact=breakout[-1],
    )

    assert candidate is not None
    assert candidate.score == 64.0
    assert candidate.metrics["grade"] == "watch"
    assert candidate.metrics["formation_vsa_breakout_gate"]["tier"] == "a"


def test_tse_replay_uses_only_rvol_prefix() -> None:
    prefix = [1.0, 1.1, 1.2, 1.3, 1.4]
    with_future = [*prefix, 20.0, 40.0]

    assert tse._volume_build_score(
        prefix,
        4,
    ) == tse._volume_build_score(with_future, 4)


def test_tse_replay_uses_aligned_vsa_and_causal_bar_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
        trailing_bar=True,
    )
    observed: list[tuple[int, int, dict[str, float], object]] = []

    def best_candidate(
        _bars,
        index,
        *,
        features,
        market_context,
        vsa_breakout_fact,
        **_kwargs,
    ):
        observed.append(
            (
                index,
                vsa_breakout_fact.index,
                dict(features),
                market_context,
            )
        )
        return None

    monkeypatch.setattr(
        tse,
        "_best_candidate",
        best_candidate,
    )

    tse.trade_setup_engine(
        bars,
        features={
            "body_share": 999.0,
            "close_pos": 999.0,
            "displacement": 999.0,
        },
        indicator_bundle={
            "market_spotlight": {
                "market_context": {
                    "side_gate": {"veto_short": True},
                },
            },
        },
        vsa_breakout_facts=breakout,
    )

    assert [item[0] for item in observed] == list(range(len(bars)))
    assert [item[1] for item in observed] == list(range(len(bars)))
    assert all(item[3] is None for item in observed[:-1])
    assert observed[-1][3] == {"side_gate": {"veto_short": True}}
    for index, _, features, _ in observed:
        bar = bars[index]
        expected_close_pos = (bar.close - bar.low) / max(
            bar.high - bar.low,
            1e-9,
        )
        expected_displacement = displacement_feature(
            bars[max(0, index - 3) : index + 1],
        )["displacement"]
        assert features["close_pos"] == expected_close_pos
        assert features["displacement"] == expected_displacement
        assert features["body_share"] != 999.0


def test_tse_adapter_passes_exact_shared_vsa_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )
    observed: dict[str, object] = {}

    def calculate(_bars, **kwargs):
        observed["facts"] = kwargs["vsa_breakout_facts"]
        return {
            "version": "test",
            "series": [],
            "events": [],
            "latest": None,
            "overlays": [],
            "levels": [],
        }

    monkeypatch.setattr(tse, "trade_setup_engine", calculate)
    runtime_params = IndicatorRuntimeParams(
        enabled={"trade_setup_engine": True},
        by_indicator={"trade_setup_engine": tse.TradeSetupEngineParams()},
        errors={},
        strategy_mode=StrategyMode.BALANCED,
    )
    ctx = IndicatorRunContext(
        confirmed_bars=bars,
        live_signal_bars=bars,
        latest=bars[-1],
        analysis_latest=bars[-1],
        runtime_params=runtime_params,
        indicator_params={},
        instrument_profile=PROFILES["SPY"],
        features={},
        shared={"vsa_breakout_facts": breakout},
    )

    tse.build_execution_spec(ctx).calculate()

    assert observed["facts"] is breakout


def test_tse_active_vsa_payload_passes_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )

    def score_momentum(
        _bars,
        index,
        direction,
        **_kwargs,
    ):
        if index != len(bars) - 1 or direction != Direction.SHORT:
            return None
        return tse._SetupCandidate(
            setup_type="momentum_breakout",
            direction=direction,
            score=70.0,
            zone_top=101.0,
            zone_bottom=100.0,
            reason_code="setup_momentum_breakdown",
            metrics={},
        )

    monkeypatch.setattr(
        tse,
        "_score_momentum_candidate",
        score_momentum,
    )
    result = tse.trade_setup_engine(
        bars,
        params=tse.TradeSetupEngineParams(
            enable_mean_reversion=False,
        ),
        vsa_breakout_facts=breakout,
    )
    manifest = indicator_manifest()["trade_setup_engine"]
    normalized = attach_indicator_status(
        "trade_setup_engine",
        result,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="test",
    )

    validate_indicator_result_contract(
        normalized,
        name="trade_setup_engine",
        renderer_contract=manifest["renderer_contract"],
        runtime_payload_contract=manifest["runtime_payload_contract"],
    )
    latest = result["latest"]
    assert latest["vsa_breakout_context"]["signal_code"] == "EXH_UP"
    assert latest["vsa_breakout_gate"]["tier"] == "a"
    zone = next(item for item in result["overlays"] if item.get("type") == "box")
    assert any(
        fact.get("code") == "vsa_spy_exh_up_downtrend_short_break" for fact in zone["badge_facts"]
    )
    assert any(
        fact.get("code") == "vsa_spy_exh_up_downtrend_short_break"
        for fact in zone["action_card"]["supporting_facts"]
    )


def test_tse_rejects_misaligned_vsa_breakout_series() -> None:
    bars, _, breakout = _breakout_scenario(
        profile_key="SPY",
        trend="downtrend",
        code="EXH_UP",
        break_direction="short",
    )

    assert tse.trade_setup_engine(
        bars,
        vsa_breakout_facts=None,
    )["series"]
    with pytest.raises(
        ValueError,
        match="exact aligned VSA breakout fact series",
    ):
        tse.trade_setup_engine(
            bars,
            vsa_breakout_facts=[],
        )
    with pytest.raises(
        ValueError,
        match="exact aligned VSA breakout fact series",
    ):
        tse.trade_setup_engine(
            bars,
            vsa_breakout_facts="invalid",
        )
    with pytest.raises(
        ValueError,
        match="exact aligned VSA breakout fact series",
    ):
        tse.trade_setup_engine(
            [],
            vsa_breakout_facts=breakout,
        )
    wrong_index = list(breakout)
    wrong_index[-1] = replace(
        wrong_index[-1],
        index=0,
    )
    with pytest.raises(
        ValueError,
        match="exact aligned VSA breakout fact series",
    ):
        tse.trade_setup_engine(
            bars,
            vsa_breakout_facts=wrong_index,
        )
