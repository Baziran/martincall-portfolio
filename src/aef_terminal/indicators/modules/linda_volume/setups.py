from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aef_terminal.domain import Bar, Direction, DomainFact
from aef_terminal.features.intraday_sessions import (
    first_hour_range,
    session_day_groups,
)
from aef_terminal.features.provider_session import ProviderSessionReset
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS, ScoreDefaults, score_action
from .playbook_contract import (
    DEFAULT_PLAYBOOK_POLICY,
    PLAYBOOK_CONTRACT_VERSION,
    playbook_promotion_preview,
    playbook_setup_metadata,
    playbook_theory_summary,
)
from aef_terminal.indicators.domain_facts import indicator_fact_payload
from aef_terminal.runtime import pine
from aef_terminal.runtime.math_utils import round_optional as _round
from aef_terminal.runtime.timeframes import adapt_bars
from aef_terminal.signals.trade_plan import trade_plan_payload

if TYPE_CHECKING:
    from .params import LindaVolumeParams


@dataclass(frozen=True)
class LindaSetupFlags:
    grail: bool = True
    indians: bool = True
    turtle_soup: bool = True
    turtle_soup_plus_one: bool = True
    eighty_twenty: bool = True
    the_anti: bool = True
    momentum_pinball: bool = True
    hv_squeeze: bool = True
    adx_gapper: bool = True


def setup_flags(params: LindaVolumeParams) -> LindaSetupFlags:
    return LindaSetupFlags(
        grail=params.enable_grail,
        indians=params.enable_indians,
        turtle_soup=params.enable_turtle_soup,
        turtle_soup_plus_one=params.enable_turtle_soup_plus_one,
        eighty_twenty=params.enable_eighty_twenty,
        the_anti=params.enable_the_anti,
        momentum_pinball=params.enable_momentum_pinball,
        hv_squeeze=params.enable_hv_squeeze,
        adx_gapper=params.enable_adx_gapper,
    )


@dataclass(frozen=True)
class LindaSetupSignal:
    code: str
    label: str
    direction: str
    score: float
    action: str
    setup_summary: str
    role: str
    trigger: float | None = None
    stop: float | None = None
    target: float | None = None
    evidence: tuple[DomainFact, ...] = ()

    def tooltip_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {"score": {"value": self.score, "digits": 0}}
        if self.trigger is not None:
            metrics["trigger"] = {"value": self.trigger, "digits": 2}
        if self.stop is not None:
            metrics["stop"] = {"value": self.stop, "digits": 2}
        if self.target is not None:
            metrics["target"] = {"value": self.target, "digits": 2}
        return metrics

    def fact_fields(self, *, theory: str = "", mode: str = "") -> dict[str, Any]:
        evidence_facts = [fact.as_dict() for fact in self.evidence]
        metrics = self.tooltip_metrics()
        if mode:
            metrics["mode"] = mode
        return indicator_fact_payload(
            scenario="linda_playbook",
            setup=self.code.lower(),
            trigger_event={
                "code": "linda_setup_candidate",
                "setup_code": self.code,
                "direction": self.direction,
            },
            supporting={"code": "linda_setup_conditions_met", "setup_code": self.code},
            context={"code": "linda_theory", "content": theory} if theory else None,
            risk={"code": "stop_invalidation", "price": self.stop}
            if self.stop is not None
            else None,
            quality={"code": "score", "value": self.score},
            fact_groups=[
                {
                    "kind": "linda_evidence",
                    "items": evidence_facts,
                }
            ]
            if evidence_facts
            else [],
            metrics=metrics,
        )

    def to_dict(self) -> dict[str, Any]:
        theory = playbook_theory_summary(self.code, self.setup_summary)
        payload = {
            "code": self.code,
            "label": self.label,
            "direction": self.direction,
            "score": round(self.score, 2),
            "action": self.action,
            "setup_summary": self.setup_summary,
            "candidate_reason": theory,
            "role": self.role,
            "trigger": self.trigger,
            "stop": self.stop,
            "target": self.target,
            **self.fact_fields(theory=theory),
        }
        payload.update(playbook_setup_metadata(payload))
        payload["theory"] = theory
        payload["promotion_preview"] = playbook_promotion_preview(payload)
        payload["mode"] = "decision" if DEFAULT_PLAYBOOK_POLICY.decision_eligible else "visual_test"
        return payload


@dataclass(frozen=True)
class LindaSetupScanContext:
    atr: float
    adx: float
    ema20: float
    ema50: float
    volume_avg: float
    provider_session: ProviderSessionReset
    score_bands: ScoreDefaults = DEFAULT_INDICATOR_SETTINGS.score


def _adapt_len(base: int, bars: Sequence[Bar]) -> int:
    if not bars:
        return max(base, 2)
    return max(adapt_bars(base, bars[-1].timeframe, source_minutes=5), 2)


def _rashke_310(closes: Sequence[float]) -> tuple[list[float], list[float]]:
    fast = pine.ema_series(closes, 3)
    slow = pine.ema_series(closes, 10)
    oscillator = [f - s for f, s in zip(fast, slow, strict=False)]
    signal = pine.ema_series(oscillator, 16)
    return oscillator, signal


def _setup_action(score: float, bands: ScoreDefaults) -> str:
    action = score_action(score, bands)
    return action if action in {"WATCH", "ARM", "GO"} else "CANDIDATE"


def detect_turtle_soup(
    bars: Sequence[Bar],
    *,
    length: int,
    atr: float,
    bands: ScoreDefaults,
) -> LindaSetupSignal | None:
    if len(bars) < length + 2:
        return None
    index = len(bars) - 1
    bar = bars[index]
    window = bars[index - length : index]
    if not window:
        return None
    prior_high = max(item.high for item in window)
    prior_low = min(item.low for item in window)
    anatomy = pine.bar_anatomy(bar)
    stop_pad = max(atr, 0.000001) * 0.18

    if bar.high > prior_high and bar.close < prior_high and anatomy.upper_share >= 0.18:
        stop = bar.high + stop_pad
        trigger = bar.close
        target = prior_low
        score = pine.clamp(64.0 + (bar.high - bar.close) / max(atr, 0.000001) * 8.0, 55.0, 92.0)
        return LindaSetupSignal(
            code="TS_DN",
            label="TS↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="Turtle Soup: false breakout above 20-bar high; fade back into range with tight stop above sweep.",
            role="linda_setup_turtle_soup",
            trigger=_round(trigger),
            stop=_round(stop),
            target=_round(target),
            evidence=(
                DomainFact(
                    "linda_turtle_sweep_rejected",
                    {"level": _round(prior_high), "side": "high"},
                ),
            ),
        )

    if bar.low < prior_low and bar.close > prior_low and anatomy.lower_share >= 0.18:
        stop = bar.low - stop_pad
        trigger = bar.close
        target = prior_high
        score = pine.clamp(64.0 + (bar.close - bar.low) / max(atr, 0.000001) * 8.0, 55.0, 92.0)
        return LindaSetupSignal(
            code="TS_UP",
            label="TS↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="Turtle Soup: false breakout below 20-bar low; fade back into range with tight stop below sweep.",
            role="linda_setup_turtle_soup",
            trigger=_round(trigger),
            stop=_round(stop),
            target=_round(target),
            evidence=(
                DomainFact(
                    "linda_turtle_sweep_rejected",
                    {"level": _round(prior_low), "side": "low"},
                ),
            ),
        )
    return None


def detect_turtle_soup_plus_one(
    bars: Sequence[Bar],
    *,
    length: int,
    atr: float,
    bands: ScoreDefaults,
) -> LindaSetupSignal | None:
    if len(bars) < length + 3:
        return None
    prev = bars[-2]
    bar = bars[-1]
    window = bars[-(length + 2) : -2]
    if not window:
        return None
    prior_high = max(item.high for item in window)
    prior_low = min(item.low for item in window)
    stop_pad = max(atr, 0.000001) * 0.18

    if prev.high > prior_high and prev.close >= prior_high and bar.close < prior_high:
        score = 66.0
        return LindaSetupSignal(
            code="TS1_DN",
            label="TS+1↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="Turtle Soup Plus One: breakout day failed, next session closes back inside the turtle range.",
            role="linda_setup_turtle_soup_plus_one",
            trigger=_round(bar.close),
            stop=_round(prev.high + stop_pad),
            target=_round(prior_low),
            evidence=(
                DomainFact(
                    "linda_turtle_break_failed_next_bar",
                    {"level": _round(prior_high), "side": "high"},
                ),
            ),
        )
    if prev.low < prior_low and prev.close <= prior_low and bar.close > prior_low:
        score = 66.0
        return LindaSetupSignal(
            code="TS1_UP",
            label="TS+1↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="Turtle Soup Plus One: breakdown day failed, next session closes back inside the turtle range.",
            role="linda_setup_turtle_soup_plus_one",
            trigger=_round(bar.close),
            stop=_round(prev.low - stop_pad),
            target=_round(prior_high),
            evidence=(
                DomainFact(
                    "linda_turtle_break_failed_next_bar",
                    {"level": _round(prior_low), "side": "low"},
                ),
            ),
        )
    return None


def detect_eighty_twenty(
    bars: Sequence[Bar],
    *,
    atr: float,
    bands: ScoreDefaults,
) -> LindaSetupSignal | None:
    if len(bars) < 3:
        return None
    prior = bars[-2]
    bar = bars[-1]
    rng = max(prior.high - prior.low, 0.000001)
    open_pos = (prior.open - prior.low) / rng
    close_pos = (prior.close - prior.low) / rng
    stop_pad = max(atr, 0.000001) * 0.22

    if open_pos <= 0.20 and close_pos >= 0.80:
        score = 63.0
        return LindaSetupSignal(
            code="8020_DN",
            label="80-20↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="80-20: strong up-day from lower range; look for next-session mean reversion short.",
            role="linda_setup_eighty_twenty",
            trigger=_round(bar.close),
            stop=_round(bar.high + stop_pad),
            target=_round(prior.close - rng * 0.35),
            evidence=(
                DomainFact(
                    "linda_eighty_twenty_range_rotation",
                    {"open_zone": "lower_20", "close_zone": "upper_20"},
                ),
            ),
        )
    if open_pos >= 0.80 and close_pos <= 0.20:
        score = 63.0
        return LindaSetupSignal(
            code="8020_UP",
            label="80-20↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="80-20: strong down-day from upper range; look for next-session mean reversion long.",
            role="linda_setup_eighty_twenty",
            trigger=_round(bar.close),
            stop=_round(bar.low - stop_pad),
            target=_round(prior.close + rng * 0.35),
            evidence=(
                DomainFact(
                    "linda_eighty_twenty_range_rotation",
                    {"open_zone": "upper_20", "close_zone": "lower_20"},
                ),
            ),
        )
    return None


def detect_the_anti(
    bars: Sequence[Bar],
    *,
    ema50: float,
    atr: float,
    bands: ScoreDefaults,
) -> LindaSetupSignal | None:
    if len(bars) < 24:
        return None
    closes = [bar.close for bar in bars]
    oscillator, signal = _rashke_310(closes)
    bar = bars[-1]
    stop_pad = max(atr, 0.000001) * 0.20
    bull_trend = (
        bar.close > ema50
        and oscillator[-2] < signal[-2]
        and oscillator[-1] > signal[-1]
        and oscillator[-1] > 0
    )
    bear_trend = (
        bar.close < ema50
        and oscillator[-2] > signal[-2]
        and oscillator[-1] < signal[-1]
        and oscillator[-1] < 0
    )

    if bull_trend:
        score = 68.0
        return LindaSetupSignal(
            code="ANTI_UP",
            label="ANTI↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="The Anti: Rashke 3-10 oscillator hooks back up with higher-timeframe bull trend.",
            role="linda_setup_the_anti",
            trigger=_round(bar.close),
            stop=_round(min(bar.low, ema50) - stop_pad),
            target=_round(bar.close + atr * 1.6),
            evidence=(
                DomainFact(
                    "linda_anti_oscillator_cross",
                    {"direction": "long", "trend": "bull"},
                ),
            ),
        )
    if bear_trend:
        score = 68.0
        return LindaSetupSignal(
            code="ANTI_DN",
            label="ANTI↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="The Anti: Rashke 3-10 oscillator hooks back down with higher-timeframe bear trend.",
            role="linda_setup_the_anti",
            trigger=_round(bar.close),
            stop=_round(max(bar.high, ema50) + stop_pad),
            target=_round(bar.close - atr * 1.6),
            evidence=(
                DomainFact(
                    "linda_anti_oscillator_cross",
                    {"direction": "short", "trend": "bear"},
                ),
            ),
        )
    return None


def detect_momentum_pinball(
    bars: Sequence[Bar],
    *,
    atr: float,
    bands: ScoreDefaults,
    provider_session: ProviderSessionReset,
) -> LindaSetupSignal | None:
    if len(bars) < 8:
        return None
    closes = [bar.close for bar in bars]
    roc = pine.change_series(closes, 1)
    rsi = pine.rsi_series(roc, 3)
    if len(rsi) < 2:
        return None
    days = session_day_groups(bars, provider_session=provider_session)
    if not days:
        return None
    _, day_bars = days[-1]
    fh_high, fh_low = first_hour_range(
        day_bars,
        provider_session=provider_session,
    )
    if fh_high is None or fh_low is None:
        return None
    bar = bars[-1]
    stop_pad = max(atr, 0.000001) * 0.18

    if rsi[-2] >= 70.0 and bar.close > fh_high:
        score = 70.0
        return LindaSetupSignal(
            code="PIN_UP",
            label="PIN↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="Momentum Pinball: extreme 3-period RSI of 1-bar ROC; breakout above first-hour high.",
            role="linda_setup_momentum_pinball",
            trigger=_round(bar.close),
            stop=_round(fh_low - stop_pad),
            target=_round(bar.close + atr * 1.8),
            evidence=(
                DomainFact(
                    "linda_momentum_pinball_break",
                    {
                        "rsi_roc": round(rsi[-2], 1),
                        "level": _round(fh_high),
                        "side": "high",
                    },
                ),
            ),
        )
    if rsi[-2] <= 30.0 and bar.close < fh_low:
        score = 70.0
        return LindaSetupSignal(
            code="PIN_DN",
            label="PIN↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="Momentum Pinball: extreme 3-period RSI of 1-bar ROC; breakdown below first-hour low.",
            role="linda_setup_momentum_pinball",
            trigger=_round(bar.close),
            stop=_round(fh_high + stop_pad),
            target=_round(bar.close - atr * 1.8),
            evidence=(
                DomainFact(
                    "linda_momentum_pinball_break",
                    {
                        "rsi_roc": round(rsi[-2], 1),
                        "level": _round(fh_low),
                        "side": "low",
                    },
                ),
            ),
        )
    return None


def detect_hv_squeeze(
    bars: Sequence[Bar],
    *,
    atr: float,
    bands: ScoreDefaults,
) -> LindaSetupSignal | None:
    if len(bars) < 110:
        return None
    closes = [bar.close for bar in bars]
    short_len = _adapt_len(6, bars)
    long_len = _adapt_len(100, bars)
    stdev_short = pine.rolling_stdev_series(closes, short_len)
    stdev_long = pine.rolling_stdev_series(closes, long_len)
    if not stdev_short or not stdev_long:
        return None
    ratio = stdev_short[-2] / max(stdev_long[-2], 1e-9)
    if ratio > 0.55:
        return None
    window = bars[-short_len - 1 : -1]
    if not window:
        return None
    box_high = max(bar.high for bar in window)
    box_low = min(bar.low for bar in window)
    bar = bars[-1]
    stop_pad = max(atr, 0.000001) * 0.20

    if bar.close > box_high:
        score = 67.0
        return LindaSetupSignal(
            code="SQZ_UP",
            label="SQZ↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="HV Squeeze: short-term volatility collapsed vs long-term; upside range break.",
            role="linda_setup_hv_squeeze",
            trigger=_round(bar.close),
            stop=_round(box_low - stop_pad),
            target=_round(bar.close + atr * 2.0),
            evidence=(
                DomainFact(
                    "linda_hv_squeeze_break",
                    {
                        "short_length": short_len,
                        "long_length": long_len,
                        "stdev_ratio": round(ratio, 4),
                        "direction": "long",
                    },
                ),
            ),
        )
    if bar.close < box_low:
        score = 67.0
        return LindaSetupSignal(
            code="SQZ_DN",
            label="SQZ↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="HV Squeeze: short-term volatility collapsed vs long-term; downside range break.",
            role="linda_setup_hv_squeeze",
            trigger=_round(bar.close),
            stop=_round(box_high + stop_pad),
            target=_round(bar.close - atr * 2.0),
            evidence=(
                DomainFact(
                    "linda_hv_squeeze_break",
                    {
                        "short_length": short_len,
                        "long_length": long_len,
                        "stdev_ratio": round(ratio, 4),
                        "direction": "short",
                    },
                ),
            ),
        )
    return None


def detect_adx_gapper(
    bars: Sequence[Bar],
    *,
    adx: float,
    ema50: float,
    atr: float,
    bands: ScoreDefaults,
) -> LindaSetupSignal | None:
    if len(bars) < 3 or adx < 30.0:
        return None
    prev = bars[-2]
    bar = bars[-1]
    gap = bar.open - prev.close
    gap_atr = abs(gap) / max(atr, 0.000001)
    if gap_atr < 0.20:
        return None
    stop_pad = max(atr, 0.000001) * 0.20
    bull_trend = bar.close > ema50
    bear_trend = bar.close < ema50

    if bull_trend and gap < 0 and bar.close > bar.open:
        score = pine.clamp(60.0 + gap_atr * 8.0, 58.0, 84.0)
        return LindaSetupSignal(
            code="GAP_UP",
            label="ADX G↑",
            direction="long",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="ADX Gapper: gap down against strong bull trend; trade gap-fill back with trend.",
            role="linda_setup_adx_gapper",
            trigger=_round(bar.close),
            stop=_round(bar.low - stop_pad),
            target=_round(prev.close),
            evidence=(
                DomainFact(
                    "linda_adx_gap_reversion",
                    {
                        "adx": round(adx, 2),
                        "gap": _round(gap),
                        "gap_direction": "down",
                    },
                ),
            ),
        )
    if bear_trend and gap > 0 and bar.close < bar.open:
        score = pine.clamp(60.0 + gap_atr * 8.0, 58.0, 84.0)
        return LindaSetupSignal(
            code="GAP_DN",
            label="ADX G↓",
            direction="short",
            score=score,
            action=_setup_action(score, bands),
            setup_summary="ADX Gapper: gap up against strong bear trend; trade gap-fill back with trend.",
            role="linda_setup_adx_gapper",
            trigger=_round(bar.close),
            stop=_round(bar.high + stop_pad),
            target=_round(prev.close),
            evidence=(
                DomainFact(
                    "linda_adx_gap_reversion",
                    {
                        "adx": round(adx, 2),
                        "gap": _round(gap),
                        "gap_direction": "up",
                    },
                ),
            ),
        )
    return None


def playbook_flags_active(flags: LindaSetupFlags) -> bool:
    return any(
        (
            flags.turtle_soup,
            flags.turtle_soup_plus_one,
            flags.eighty_twenty,
            flags.the_anti,
            flags.momentum_pinball,
            flags.hv_squeeze,
            flags.adx_gapper,
        )
    )


def playbook_flags_enabled(params: LindaVolumeParams) -> bool:
    return playbook_flags_active(setup_flags(params))


def scan_linda_setups(
    bars: Sequence[Bar],
    flags: LindaSetupFlags,
    *,
    context: LindaSetupScanContext,
) -> list[LindaSetupSignal]:
    if not bars:
        return []
    turtle_len = _adapt_len(20, bars)
    detectors: list[tuple[bool, Any]] = [
        (
            flags.turtle_soup,
            lambda: detect_turtle_soup(
                bars, length=turtle_len, atr=context.atr, bands=context.score_bands
            ),
        ),
        (
            flags.turtle_soup_plus_one,
            lambda: detect_turtle_soup_plus_one(
                bars, length=turtle_len, atr=context.atr, bands=context.score_bands
            ),
        ),
        (
            flags.eighty_twenty,
            lambda: detect_eighty_twenty(bars, atr=context.atr, bands=context.score_bands),
        ),
        (
            flags.the_anti,
            lambda: detect_the_anti(
                bars, ema50=context.ema50, atr=context.atr, bands=context.score_bands
            ),
        ),
        (
            flags.momentum_pinball,
            lambda: detect_momentum_pinball(
                bars,
                atr=context.atr,
                bands=context.score_bands,
                provider_session=context.provider_session,
            ),
        ),
        (
            flags.hv_squeeze,
            lambda: detect_hv_squeeze(bars, atr=context.atr, bands=context.score_bands),
        ),
        (
            flags.adx_gapper,
            lambda: detect_adx_gapper(
                bars,
                adx=context.adx,
                ema50=context.ema50,
                atr=context.atr,
                bands=context.score_bands,
            ),
        ),
    ]
    signals: list[LindaSetupSignal] = []
    for enabled, detector in detectors:
        if not enabled:
            continue
        signal = detector()
        if signal is not None:
            signals.append(signal)
    return signals


def scan_linda_setups_history(
    bars: Sequence[Bar],
    flags: LindaSetupFlags,
    *,
    context_at: Any,
    lookback: int = 64,
) -> list[tuple[int, LindaSetupSignal]]:
    """Scan playbook detectors on each recent bar so charts can show historical marks."""
    if not bars or not playbook_flags_active(flags):
        return []
    start = max(0, len(bars) - max(int(lookback), 1))
    found: list[tuple[int, LindaSetupSignal]] = []
    seen: set[tuple[str, str]] = set()
    for end in range(start, len(bars)):
        slice_bars = bars[: end + 1]
        context = context_at(end)
        for signal in scan_linda_setups(slice_bars, flags, context=context):
            key = (signal.code, bars[end].ts.isoformat())
            if key in seen:
                continue
            seen.add(key)
            found.append((end, signal))
    return found


def setup_overlay(signal: LindaSetupSignal, bar: Bar) -> dict[str, Any]:
    from aef_terminal.runtime import overlays

    price = (
        bar.low
        if signal.direction == "long"
        else bar.high
        if signal.direction == "short"
        else bar.close
    )
    theory = playbook_theory_summary(signal.code, signal.setup_summary)
    action = str(signal.action or "WATCH").upper()
    decision_eligible = DEFAULT_PLAYBOOK_POLICY.decision_eligible
    mode = "decision" if decision_eligible else "visual_test"
    fact_fields = signal.fact_fields(theory=theory, mode=mode)
    item = overlays.label(
        bar=bar,
        price=price,
        lines=[],
        direction=signal.direction,
        side="below"
        if signal.direction == "long"
        else "above"
        if signal.direction == "short"
        else "right",
        role=signal.role,
        fact_fields=fact_fields,
    )
    item["code"] = signal.code
    item["score"] = signal.score
    item["action"] = action
    item["raw_action"] = action
    item["setup_summary"] = signal.setup_summary
    item["candidate_reason"] = theory
    item["overlay_role"] = "linda_playbook"
    if signal.trigger is not None or signal.stop is not None or signal.target is not None:
        direction = (
            Direction.LONG
            if signal.direction == Direction.LONG.value
            else Direction.SHORT
            if signal.direction == Direction.SHORT.value
            else Direction.FLAT
        )
        overlays.bind_signal_trade_plan(
            item,
            {
                "source": "linda_volume",
                "action": action,
                "raw_action": action,
                "direction": signal.direction,
                "trigger": signal.trigger,
                "stop": signal.stop,
                "target": signal.target,
                "score": signal.score,
                "code": signal.code,
                "reason": f"linda_{signal.code.lower()}",
                "candidate_reason": theory,
                "theory": theory,
                "decision_eligible": decision_eligible,
                "mode": mode,
                "contract": PLAYBOOK_CONTRACT_VERSION,
                "trade_plan": trade_plan_payload(
                    source="linda_volume",
                    direction=direction,
                    entry=signal.trigger,
                    stop=signal.stop,
                    target=signal.target,
                    action=action,
                    score=signal.score,
                    actionable=action in {"GO", "ARM", "WATCH"},
                    code=signal.code,
                ),
            },
            source="linda_volume",
        )
    return item
