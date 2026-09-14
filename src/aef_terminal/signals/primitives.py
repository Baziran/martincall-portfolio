from __future__ import annotations

from dataclasses import dataclass, field
from aef_terminal.domain import Bar, SignalCandidate
from aef_terminal.features.context import FeatureContext
from aef_terminal.signals.sfp import detect_sfp


@dataclass(frozen=True)
class SignalPrimitiveContext:
    candidates: list[SignalCandidate] = field(default_factory=list)
    sfp_candidates: list[SignalCandidate] = field(default_factory=list)


def build_signal_primitive_context(
    *,
    live_signal_bars: list[Bar],
    live_feature_context: FeatureContext | None = None,
) -> SignalPrimitiveContext:
    sfp_candidates = detect_sfp(live_signal_bars, feature_context=live_feature_context)
    return SignalPrimitiveContext(
        candidates=list(sfp_candidates),
        sfp_candidates=sfp_candidates,
    )
