from collections.abc import Sequence

from aef_terminal.domain import (
    Bar,
    CandidateFinality,
    Direction,
    DomainFact,
    ScenarioKind,
    SignalCandidate,
)
from aef_terminal.features.context import FeatureContext
from aef_terminal.features.price_action import sweep_facts
from aef_terminal.features.volume import volume_features
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.runtime.pine import atr_sma_series


def _resolve_atr_rvol(
    bars: Sequence[Bar],
    lookback: int,
    feature_context: FeatureContext | None,
) -> tuple[float, float]:
    if feature_context is not None and len(feature_context.bars) == len(bars):
        rvol = exact_finite_number_or_none(feature_context.values.get("rvol_adaptive"))
        if rvol is None:
            rvol = exact_finite_number_or_none(feature_context.latest_rvol)
        if rvol is None:
            rvol = 1.0
        atr_now = float(feature_context.latest_atr_sma)
        return max(atr_now, 0.000001), max(rvol, 0.0)
    vol = volume_features(bars, lookback=lookback)
    rvol = exact_finite_number_or_none(vol.get("rvol_adaptive"))
    if rvol is None:
        rvol = 1.0
    atr_values = atr_sma_series(bars, DEFAULT_INDICATOR_SETTINGS.atr_len)
    atr_now = float(atr_values[-1]) if atr_values else (bars[-1].high - bars[-1].low)
    return max(atr_now, 0.000001), max(rvol, 0.0)


def detect_sfp(
    bars: Sequence[Bar],
    lookback: int = 20,
    execution_buffer_atr: float = 0.4,
    feature_context: FeatureContext | None = None,
) -> list[SignalCandidate]:
    if len(bars) <= lookback:
        return []

    # Получаем факты свипа из Price Action
    facts = sweep_facts(bars, lookback=lookback)
    if not facts or not facts[-1]:
        return []

    fact = facts[-1]

    # Профессиональный фильтр: подтверждение объемом.
    # Настоящий "Liquidity Grab" (SFP) должен сопровождаться всплеском активности.
    atr_now, rvol = _resolve_atr_rvol(bars, lookback, feature_context)

    # Корректируем базовый скоринг на основе объема (логика VSA)
    final_score = fact.score
    if (
        rvol >= DEFAULT_INDICATOR_SETTINGS.rvol.climax
    ):  # Климатический объем - очень сильное подтверждение
        final_score += 12.0
    elif (
        rvol >= DEFAULT_INDICATOR_SETTINGS.rvol.elevated
    ):  # Повышенный объем - хорошее подтверждение
        final_score += 5.0
    elif (
        rvol < DEFAULT_INDICATOR_SETTINGS.rvol.low
    ):  # Низкий объем - признак ложного разворота (штрафуем)
        final_score -= 15.0

    final_score = max(0.0, min(99.0, final_score))
    buffer_points = max(atr_now * max(float(execution_buffer_atr), 0.0), 0.0)
    live_bar = bars[-1].closed is False

    # Execution metadata for downstream signal consumers
    details = {
        "rvol_adaptive": round(rvol, 2),
        "wick_share": round(fact.wick_share, 2),
        "close_pos": round(fact.close_pos, 2),
        "is_climactic": rvol > DEFAULT_INDICATOR_SETTINGS.rvol.climax + 0.2,
        "sweep_level": fact.level,
        "execution_buffer_atr": round(float(execution_buffer_atr), 2),
        "execution_buffer_points": round(buffer_points, 4),
        "live_bar": live_bar,
    }

    signals: list[SignalCandidate] = []

    if fact.direction == "short":
        trigger = fact.level - buffer_points
        details["trigger"] = trigger
        signals.append(
            SignalCandidate(
                "sfp",
                Direction.SHORT,
                final_score,
                fact.level,
                "sfp_high_reclaim",
                details=details,
                kind=ScenarioKind.FADE,
                source="sfp",
                role="liquidity_sweep",
                trigger_event=DomainFact("sfp_high_reclaim"),
                reason_code="sfp_high_reclaim",
                finality=(
                    CandidateFinality.PROVISIONAL if live_bar else CandidateFinality.CONFIRMED
                ),
            )
        )
    if fact.direction == "long":
        trigger = fact.level + buffer_points
        details["trigger"] = trigger
        signals.append(
            SignalCandidate(
                "sfp",
                Direction.LONG,
                final_score,
                fact.level,
                "sfp_low_reclaim",
                details=details,
                kind=ScenarioKind.FADE,
                source="sfp",
                role="liquidity_sweep",
                trigger_event=DomainFact("sfp_low_reclaim"),
                reason_code="sfp_low_reclaim",
                finality=(
                    CandidateFinality.PROVISIONAL if live_bar else CandidateFinality.CONFIRMED
                ),
            )
        )
    return signals
