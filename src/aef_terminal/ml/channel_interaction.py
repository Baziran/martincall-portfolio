from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import numpy as np

from aef_terminal.config import AppConfig
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import Bar, BarState
from aef_terminal.ml.channel_interaction_contracts import ChannelInteractionOutcome
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.runtime.timeframes import require_aware_utc_datetime


ChannelInteractionPhase = Literal["pre_touch", "post_touch"]
ChannelInteractionStatus = Literal["unavailable", "blocked", "ready"]

CHANNEL_INTERACTION_ARTIFACT_CONTRACT = "channel-interaction-xgboost-v1"
CHANNEL_INTERACTION_ACTIVE_CONTRACT = "channel-interaction-active-v1"
CHANNEL_INTERACTION_SCOPE_CONTRACT = "channel-interaction-inference-scope-v1"
CHANNEL_INTERACTION_TARGET_CONTRACT = "channel-interaction-phase-target-v1"
CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME = "1m"
CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION = 1
CHANNEL_INTERACTION_LABELS = tuple(item.value for item in ChannelInteractionOutcome)
CHANNEL_INTERACTION_PHASES: tuple[ChannelInteractionPhase, ...] = ("pre_touch", "post_touch")
CHANNEL_INTERACTION_LABELS_BY_PHASE = MappingProxyType(
    {
        "pre_touch": CHANNEL_INTERACTION_LABELS,
        "post_touch": tuple(
            label
            for label in CHANNEL_INTERACTION_LABELS
            if label != ChannelInteractionOutcome.NO_TOUCH.value
        ),
    }
)
CHANNEL_INTERACTION_FEATURE_COLUMNS = (
    "phase_post_touch",
    "side_resistance",
    "atr_missing",
    "history_3_missing",
    "history_5_missing",
    "history_10_missing",
    "history_15_missing",
    "volume_3_missing",
    "volume_15_missing",
    "volume_change_1_missing",
    "touch_missing",
    "bars_observed_15",
    "missing_slot_fraction_15",
    "slope_signed_atr_per_slot",
    "line_drift_signed_15_atr",
    "distance_open_atr",
    "distance_close_atr",
    "breakout_extreme_atr",
    "reversal_extreme_atr",
    "approach_return_1_atr",
    "approach_return_3_atr",
    "approach_return_5_atr",
    "approach_return_10_atr",
    "approach_return_15_atr",
    "range_3_atr",
    "range_5_atr",
    "range_10_atr",
    "range_15_atr",
    "true_range_3_atr",
    "true_range_5_atr",
    "true_range_15_atr",
    "close_location_1",
    "body_atr",
    "body_direction_signed_atr",
    "upper_wick_atr",
    "lower_wick_atr",
    "volume_ratio_3_15",
    "volume_z_15",
    "volume_change_1",
    "touch_penetration_atr",
    "touch_close_beyond_atr",
    "touch_rejection_atr",
    "bars_since_touch",
    "post_touch_max_breakout_atr",
    "post_touch_max_reversal_atr",
    "post_touch_close_progress_atr",
    "post_touch_consecutive_beyond",
)


def channel_interaction_dataset_root(config: AppConfig | None = None) -> Path:
    return (config or AppConfig()).data_root / "datasets" / "channel_interactions"


def channel_interaction_model_root(config: AppConfig | None = None) -> Path:
    return (config or AppConfig()).data_root / "models" / "channel_interaction"


def require_channel_interaction_data_path(path: str | Path, *, kind: str) -> Path:
    if kind == "dataset":
        root = channel_interaction_dataset_root()
    elif kind == "model":
        root = channel_interaction_model_root()
    else:
        raise ValueError("kind must be dataset or model")
    resolved_root = root.resolve(strict=False)
    resolved_path = Path(path).expanduser().resolve(strict=False)
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"{kind} path must stay under {resolved_root}")
    return resolved_path


def _finite_number(value: object, *, field_name: str) -> float:
    normalized = exact_finite_number_or_none(value)
    if normalized is None:
        raise ValueError(f"{field_name} must be a finite number")
    return normalized


@dataclass(frozen=True, slots=True)
class FrozenChannelLevel:
    anchor_slot: int
    anchor_price: float
    slope_per_slot: float
    slot_timeframe: Literal["1m"]
    side: Literal["resistance", "support"]
    frozen_revision: str
    frozen_at: datetime

    def __post_init__(self) -> None:
        if isinstance(self.anchor_slot, bool) or not isinstance(self.anchor_slot, int):
            raise TypeError("anchor_slot must be an integer")
        if self.slot_timeframe != CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME:
            raise ValueError("slot_timeframe must be 1m")
        if self.side not in {"resistance", "support"}:
            raise ValueError("side must be resistance or support")
        revision = str(self.frozen_revision).strip()
        if not revision:
            raise ValueError("frozen_revision must be non-empty")
        object.__setattr__(
            self, "anchor_price", _finite_number(self.anchor_price, field_name="anchor_price")
        )
        object.__setattr__(
            self,
            "slope_per_slot",
            _finite_number(self.slope_per_slot, field_name="slope_per_slot"),
        )
        object.__setattr__(self, "frozen_revision", revision)
        object.__setattr__(
            self,
            "frozen_at",
            require_aware_utc_datetime(self.frozen_at, field="frozen_at"),
        )

    def price_at(self, slot: int) -> float:
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise TypeError("slot must be an integer")
        return self.anchor_price + self.slope_per_slot * (slot - self.anchor_slot)

    @property
    def breakout_sign(self) -> float:
        return 1.0 if self.side == "resistance" else -1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor_slot": self.anchor_slot,
            "anchor_price": self.anchor_price,
            "slope_per_slot": self.slope_per_slot,
            "slot_timeframe": self.slot_timeframe,
            "side": self.side,
            "frozen_revision": self.frozen_revision,
            "frozen_at": self.frozen_at.isoformat(),
        }


@dataclass(frozen=True, slots=True, order=True)
class ChannelInteractionInferenceScope:
    provider: str
    instrument_id: str
    parent_timeframe: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            require_exact_identity_text(self.provider, field="scope.provider"),
        )
        object.__setattr__(
            self,
            "instrument_id",
            require_exact_identity_text(self.instrument_id, field="scope.instrument_id"),
        )
        object.__setattr__(
            self,
            "parent_timeframe",
            require_exact_identity_text(
                self.parent_timeframe,
                field="scope.parent_timeframe",
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ChannelInteractionInferenceScope:
        if not isinstance(value, Mapping) or set(value) != {
            "provider",
            "instrument_id",
            "parent_timeframe",
        }:
            raise ValueError("model_scope_invalid")
        return cls(
            provider=value["provider"],
            instrument_id=value["instrument_id"],
            parent_timeframe=value["parent_timeframe"],
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "instrument_id": self.instrument_id,
            "parent_timeframe": self.parent_timeframe,
        }


@dataclass(frozen=True, slots=True)
class ChannelInteractionFeatureResult:
    status: Literal["blocked", "ready"]
    reason_code: str
    phase: ChannelInteractionPhase
    decision_ts: datetime
    feature_columns: tuple[str, ...] = CHANNEL_INTERACTION_FEATURE_COLUMNS
    feature_values: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_ts",
            require_aware_utc_datetime(self.decision_ts, field="decision_ts"),
        )
        if self.phase not in CHANNEL_INTERACTION_PHASES:
            raise ValueError("unsupported channel interaction phase")
        if self.status == "ready":
            if self.feature_columns != CHANNEL_INTERACTION_FEATURE_COLUMNS:
                raise ValueError("feature columns do not match the canonical contract")
            if len(self.feature_values) != len(self.feature_columns):
                raise ValueError("feature values do not match feature columns")
            if any(not isfinite(float(value)) for value in self.feature_values):
                raise ValueError("feature values must be finite")
        elif self.feature_values:
            raise ValueError("blocked feature results must not carry feature values")

    @property
    def features(self) -> Mapping[str, float]:
        if self.status != "ready":
            return MappingProxyType({})
        return MappingProxyType(dict(zip(self.feature_columns, self.feature_values, strict=True)))

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "reason_code": self.reason_code,
            "phase": self.phase,
            "decision_ts": self.decision_ts.isoformat(),
            "feature_schema_version": CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
            "feature_columns": list(self.feature_columns),
        }
        if self.status == "ready":
            payload["features"] = dict(self.features)
        return payload


def _blocked_features(
    *,
    phase: ChannelInteractionPhase,
    decision_ts: datetime,
    reason_code: str,
) -> ChannelInteractionFeatureResult:
    return ChannelInteractionFeatureResult(
        status="blocked",
        reason_code=reason_code,
        phase=phase,
        decision_ts=decision_ts,
    )


def _window_missing(length: int, bars_required: int) -> float:
    return 0.0 if length >= bars_required else 1.0


def _true_range(bar: Bar, previous_close: float | None) -> float:
    if previous_close is None:
        return float(bar.high) - float(bar.low)
    return max(
        float(bar.high) - float(bar.low),
        abs(float(bar.high) - previous_close),
        abs(float(bar.low) - previous_close),
    )


def extract_channel_interaction_features(
    bars_1m: Sequence[Bar],
    bar_slots: Sequence[int],
    *,
    level: FrozenChannelLevel,
    decision_ts: datetime,
    phase: ChannelInteractionPhase,
    atr_value: float | None,
    touch_slot: int | None = None,
) -> ChannelInteractionFeatureResult:
    """Extract one causal row from confirmed 1m bars available at ``decision_ts``."""

    if phase not in CHANNEL_INTERACTION_PHASES:
        raise ValueError("phase must be pre_touch or post_touch")
    decision = require_aware_utc_datetime(decision_ts, field="decision_ts")
    if level.frozen_at > decision:
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="level_revision_from_future"
        )
    if len(bars_1m) != len(bar_slots):
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="bar_slot_length_mismatch"
        )
    if not bars_1m:
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="confirmed_1m_context_empty"
        )
    if phase == "pre_touch" and touch_slot is not None:
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="pre_touch_has_touch_slot"
        )
    if phase == "post_touch" and touch_slot is None:
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="post_touch_missing_touch_slot"
        )

    bars = list(bars_1m)
    slots: list[int] = []
    previous_ts: datetime | None = None
    previous_slot: int | None = None
    for bar, raw_slot in zip(bars, bar_slots, strict=True):
        if not isinstance(bar, Bar):
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="invalid_1m_bar_type"
            )
        if bar.timeframe != "1m":
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="context_not_1m"
            )
        if not bar.closed or bar.state is not BarState.CONFIRMED:
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="context_bar_not_confirmed"
            )
        if bar.ts + timedelta(minutes=1) > decision:
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="future_1m_close_in_context"
            )
        if previous_ts is not None and bar.ts <= previous_ts:
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="non_monotonic_1m_time"
            )
        if isinstance(raw_slot, bool) or not isinstance(raw_slot, int):
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="invalid_1m_slot"
            )
        slot = int(raw_slot)
        if previous_slot is not None and slot <= previous_slot:
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="non_monotonic_1m_slot"
            )
        previous_ts = bar.ts
        previous_slot = slot
        slots.append(slot)

    touch_index: int | None = None
    if phase == "post_touch":
        if isinstance(touch_slot, bool) or not isinstance(touch_slot, int):
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="invalid_touch_slot"
            )
        try:
            touch_index = slots.index(int(touch_slot))
        except ValueError:
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="touch_slot_not_in_context"
            )
        touch_bar = bars[touch_index]
        touch_line = level.price_at(slots[touch_index])
        if not (float(touch_bar.low) <= touch_line <= float(touch_bar.high)):
            return _blocked_features(
                phase=phase, decision_ts=decision, reason_code="touch_bar_does_not_touch_level"
            )

    atr_missing = 0.0
    scale: float | None = None
    if atr_value is not None:
        try:
            candidate = _finite_number(atr_value, field_name="atr_value")
        except ValueError:
            candidate = 0.0
        if candidate > 0:
            scale = candidate
    if scale is None:
        atr_missing = 1.0
        trailing = bars[-15:]
        true_ranges = [
            _true_range(bar, float(trailing[index - 1].close) if index > 0 else None)
            for index, bar in enumerate(trailing)
        ]
        positive_ranges = [value for value in true_ranges if value > 0 and isfinite(value)]
        if positive_ranges:
            scale = float(np.median(np.asarray(positive_ranges, dtype=np.float64)))
    if scale is None or scale <= 0 or not isfinite(scale):
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="normalization_scale_unavailable"
        )

    sign = level.breakout_sign
    latest = bars[-1]
    latest_slot = slots[-1]
    latest_line = level.price_at(latest_slot)
    trailing_15_bars = bars[-15:]
    trailing_15_slots = slots[-15:]
    observed = len(trailing_15_bars)
    expected_span = trailing_15_slots[-1] - trailing_15_slots[0] + 1 if observed > 1 else 1
    missing_slots = max(expected_span - observed, 0)
    missing_slot_fraction = missing_slots / max(expected_span, 1)

    signed_close_distances = [
        sign * (float(bar.close) - level.price_at(slot))
        for bar, slot in zip(bars, slots, strict=True)
    ]

    def approach_return(period: int) -> float:
        if len(bars) < period + 1:
            return 0.0
        return (signed_close_distances[-1] - signed_close_distances[-period - 1]) / scale

    def window_range(size: int) -> float:
        if len(bars) < size:
            return 0.0
        window = bars[-size:]
        return (
            max(float(bar.high) for bar in window) - min(float(bar.low) for bar in window)
        ) / scale

    def mean_true_range(size: int) -> float:
        if len(bars) < size:
            return 0.0
        start = len(bars) - size
        values = [
            _true_range(bars[index], float(bars[index - 1].close) if index > 0 else None)
            for index in range(start, len(bars))
        ]
        return float(np.mean(np.asarray(values, dtype=np.float64))) / scale

    candle_range = max(float(latest.high) - float(latest.low), 0.0)
    close_location = (
        (float(latest.close) - float(latest.low)) / candle_range if candle_range > 0 else 0.5
    )
    body = float(latest.close) - float(latest.open)
    upper_wick = float(latest.high) - max(float(latest.open), float(latest.close))
    lower_wick = min(float(latest.open), float(latest.close)) - float(latest.low)

    volumes_15 = [float(bar.volume) for bar in bars[-15:]]
    volumes_3 = [float(bar.volume) for bar in bars[-3:]]
    volume_3_missing = _window_missing(len(bars), 3)
    volume_15_missing = _window_missing(len(bars), 15)
    volume_ratio = 0.0
    volume_z = 0.0
    volume_change = 0.0
    volume_change_missing = 0.0
    if not volume_15_missing:
        mean_15 = float(np.mean(np.asarray(volumes_15, dtype=np.float64)))
        std_15 = float(np.std(np.asarray(volumes_15, dtype=np.float64)))
        if mean_15 > 0 and not volume_3_missing:
            volume_ratio = float(np.mean(np.asarray(volumes_3, dtype=np.float64))) / mean_15
        else:
            volume_3_missing = 1.0
        if std_15 > 0:
            volume_z = (float(latest.volume) - mean_15) / std_15
        else:
            volume_15_missing = 1.0
    if len(bars) >= 2 and float(bars[-2].volume) > 0:
        volume_change = (float(latest.volume) - float(bars[-2].volume)) / float(bars[-2].volume)
    else:
        volume_change_missing = 1.0

    signed_latest_high = max(
        sign * (float(latest.high) - latest_line),
        sign * (float(latest.low) - latest_line),
    )
    signed_latest_low = min(
        sign * (float(latest.high) - latest_line),
        sign * (float(latest.low) - latest_line),
    )

    touch_penetration = 0.0
    touch_close_beyond = 0.0
    touch_rejection = 0.0
    bars_since_touch = 0.0
    post_touch_max_breakout = 0.0
    post_touch_max_reversal = 0.0
    post_touch_close_progress = 0.0
    post_touch_consecutive_beyond = 0.0
    if touch_index is not None:
        touch_bar = bars[touch_index]
        touch_line = level.price_at(slots[touch_index])
        touch_extreme = max(
            sign * (float(touch_bar.high) - touch_line),
            sign * (float(touch_bar.low) - touch_line),
        )
        touch_close = sign * (float(touch_bar.close) - touch_line)
        touch_penetration = touch_extreme / scale
        touch_close_beyond = touch_close / scale
        touch_rejection = (touch_extreme - touch_close) / scale
        bars_since_touch = float(latest_slot - slots[touch_index])
        post_bars = bars[touch_index:]
        post_slots = slots[touch_index:]
        post_breakout: list[float] = []
        post_reversal: list[float] = []
        for bar, slot in zip(post_bars, post_slots, strict=True):
            line_price = level.price_at(slot)
            endpoints = (
                sign * (float(bar.high) - line_price),
                sign * (float(bar.low) - line_price),
            )
            post_breakout.append(max(endpoints))
            post_reversal.append(-min(endpoints))
        post_touch_max_breakout = max(post_breakout) / scale
        post_touch_max_reversal = max(post_reversal) / scale
        post_touch_close_progress = (signed_close_distances[-1] - touch_close) / scale
        for close_distance in reversed(signed_close_distances[touch_index:]):
            if close_distance < 0:
                break
            post_touch_consecutive_beyond += 1.0

    feature_map = {
        "phase_post_touch": 1.0 if phase == "post_touch" else 0.0,
        "side_resistance": 1.0 if level.side == "resistance" else 0.0,
        "atr_missing": atr_missing,
        "history_3_missing": _window_missing(len(bars), 4),
        "history_5_missing": _window_missing(len(bars), 6),
        "history_10_missing": _window_missing(len(bars), 11),
        "history_15_missing": _window_missing(len(bars), 16),
        "volume_3_missing": volume_3_missing,
        "volume_15_missing": volume_15_missing,
        "volume_change_1_missing": volume_change_missing,
        "touch_missing": 0.0 if touch_index is not None else 1.0,
        "bars_observed_15": float(observed) / 15.0,
        "missing_slot_fraction_15": missing_slot_fraction,
        "slope_signed_atr_per_slot": sign * level.slope_per_slot / scale,
        "line_drift_signed_15_atr": sign
        * (latest_line - level.price_at(trailing_15_slots[0]))
        / scale,
        "distance_open_atr": sign * (float(latest.open) - latest_line) / scale,
        "distance_close_atr": signed_close_distances[-1] / scale,
        "breakout_extreme_atr": signed_latest_high / scale,
        "reversal_extreme_atr": -signed_latest_low / scale,
        "approach_return_1_atr": approach_return(1),
        "approach_return_3_atr": approach_return(3),
        "approach_return_5_atr": approach_return(5),
        "approach_return_10_atr": approach_return(10),
        "approach_return_15_atr": approach_return(15),
        "range_3_atr": window_range(3),
        "range_5_atr": window_range(5),
        "range_10_atr": window_range(10),
        "range_15_atr": window_range(15),
        "true_range_3_atr": mean_true_range(3),
        "true_range_5_atr": mean_true_range(5),
        "true_range_15_atr": mean_true_range(15),
        "close_location_1": close_location,
        "body_atr": abs(body) / scale,
        "body_direction_signed_atr": sign * body / scale,
        "upper_wick_atr": upper_wick / scale,
        "lower_wick_atr": lower_wick / scale,
        "volume_ratio_3_15": volume_ratio,
        "volume_z_15": volume_z,
        "volume_change_1": volume_change,
        "touch_penetration_atr": touch_penetration,
        "touch_close_beyond_atr": touch_close_beyond,
        "touch_rejection_atr": touch_rejection,
        "bars_since_touch": bars_since_touch,
        "post_touch_max_breakout_atr": post_touch_max_breakout,
        "post_touch_max_reversal_atr": post_touch_max_reversal,
        "post_touch_close_progress_atr": post_touch_close_progress,
        "post_touch_consecutive_beyond": post_touch_consecutive_beyond,
    }
    if tuple(feature_map) != CHANNEL_INTERACTION_FEATURE_COLUMNS:
        raise RuntimeError("channel interaction feature implementation drifted from its schema")
    values = tuple(float(feature_map[column]) for column in CHANNEL_INTERACTION_FEATURE_COLUMNS)
    if any(not isfinite(value) for value in values):
        return _blocked_features(
            phase=phase, decision_ts=decision, reason_code="non_finite_feature"
        )
    return ChannelInteractionFeatureResult(
        status="ready",
        reason_code="features_ready",
        phase=phase,
        decision_ts=decision,
        feature_values=values,
    )


@dataclass(frozen=True, slots=True)
class ChannelInteractionPrediction:
    status: ChannelInteractionStatus
    reason_code: str
    phase: ChannelInteractionPhase
    probabilities: tuple[tuple[str, float], ...] = ()
    predicted_class: str | None = None
    top_class: str | None = None
    decision_state: Literal["directional", "abstain"] | None = None
    confidence: float | None = None
    top2_margin: float | None = None
    min_confidence: float | None = None
    min_top2_margin: float | None = None
    inference_scope: ChannelInteractionInferenceScope | None = None
    artifact_contract: str | None = None
    target_contract: str | None = None
    target_definition: Mapping[str, Any] | None = None
    model_sha256: str | None = None
    temperature: float | None = None

    @property
    def probability_map(self) -> Mapping[str, float]:
        return MappingProxyType(dict(self.probabilities))

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "reason_code": self.reason_code,
            "phase": self.phase,
            "labels": list(CHANNEL_INTERACTION_LABELS),
        }
        if self.status == "ready":
            payload.update(
                {
                    "probabilities": dict(self.probabilities),
                    "predicted_class": self.predicted_class,
                    "top_class": self.top_class,
                    "decision_state": self.decision_state,
                    "confidence": self.confidence,
                    "top2_margin": self.top2_margin,
                    "min_confidence": self.min_confidence,
                    "min_top2_margin": self.min_top2_margin,
                    "inference_scope": (
                        self.inference_scope.as_dict() if self.inference_scope is not None else None
                    ),
                    "artifact_contract": self.artifact_contract,
                    "target_contract": self.target_contract,
                    "target_definition": (
                        dict(self.target_definition) if self.target_definition is not None else None
                    ),
                    "model_sha256": self.model_sha256,
                    "temperature": self.temperature,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class ChannelInteractionArtifactManifest:
    artifact_contract: str
    backend: str
    feature_schema_version: int
    feature_columns: tuple[str, ...]
    labels: tuple[str, ...]
    phases: tuple[str, ...]
    model_file: str
    model_sha256: str
    run_id: str
    phase_temperatures: Mapping[str, float]
    trained_at: str
    metrics: Mapping[str, Any]
    acceptance_gate: Mapping[str, Any]
    decision_policy: Mapping[str, float]
    inference_scopes: tuple[ChannelInteractionInferenceScope, ...]
    build_provenance: Mapping[str, Any]
    dataset_build_provenance: Mapping[str, Any]
    target_definition: Mapping[str, Any]
    dataset_manifest_file: str
    dataset_manifest_sha256: str
    training: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChannelInteractionPredictor:
    manifest: ChannelInteractionArtifactManifest
    _booster: Any

    def predict(
        self,
        features: ChannelInteractionFeatureResult | Mapping[str, float],
        *,
        phase: ChannelInteractionPhase,
        scope: ChannelInteractionInferenceScope | Mapping[str, Any],
    ) -> ChannelInteractionPrediction:
        if phase not in CHANNEL_INTERACTION_PHASES:
            raise ValueError("phase must be pre_touch or post_touch")
        try:
            normalized_scope = (
                scope
                if isinstance(scope, ChannelInteractionInferenceScope)
                else ChannelInteractionInferenceScope.from_mapping(scope)
            )
        except TypeError, ValueError:
            return ChannelInteractionPrediction(
                status="blocked",
                reason_code="model_scope_mismatch",
                phase=phase,
            )
        if normalized_scope not in self.manifest.inference_scopes:
            return ChannelInteractionPrediction(
                status="blocked",
                reason_code="model_scope_mismatch",
                phase=phase,
            )
        if isinstance(features, ChannelInteractionFeatureResult):
            if features.status != "ready":
                return ChannelInteractionPrediction(
                    status="blocked",
                    reason_code=features.reason_code,
                    phase=phase,
                )
            if features.phase != phase:
                return ChannelInteractionPrediction(
                    status="blocked",
                    reason_code="feature_phase_mismatch",
                    phase=phase,
                )
            if features.feature_columns != self.manifest.feature_columns:
                return ChannelInteractionPrediction(
                    status="blocked",
                    reason_code="feature_schema_mismatch",
                    phase=phase,
                )
            values = features.feature_values
        else:
            if set(features) != set(self.manifest.feature_columns):
                return ChannelInteractionPrediction(
                    status="blocked",
                    reason_code="feature_schema_mismatch",
                    phase=phase,
                )
            try:
                values = tuple(float(features[column]) for column in self.manifest.feature_columns)
            except TypeError, ValueError:
                return ChannelInteractionPrediction(
                    status="blocked",
                    reason_code="invalid_feature_value",
                    phase=phase,
                )
        if any(not isfinite(value) for value in values):
            return ChannelInteractionPrediction(
                status="blocked",
                reason_code="non_finite_feature",
                phase=phase,
            )
        matrix = np.asarray([values], dtype=np.float32)
        try:
            raw = self._booster.inplace_predict(
                matrix,
                predict_type="margin",
                strict_shape=True,
            )
        except Exception:
            return ChannelInteractionPrediction(
                status="blocked",
                reason_code="model_inference_failed",
                phase=phase,
            )
        logits = np.asarray(raw, dtype=np.float64).reshape(-1)
        if logits.shape != (len(CHANNEL_INTERACTION_LABELS),) or not np.all(np.isfinite(logits)):
            return ChannelInteractionPrediction(
                status="blocked",
                reason_code="invalid_model_output",
                phase=phase,
            )
        temperature = self.manifest.phase_temperatures[phase]
        scaled = logits / temperature
        for label_index, label in enumerate(CHANNEL_INTERACTION_LABELS):
            if label not in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]:
                scaled[label_index] = -np.inf
        scaled -= float(np.max(scaled))
        exp_values = np.exp(scaled)
        denominator = float(np.sum(exp_values))
        if not isfinite(denominator) or denominator <= 0:
            return ChannelInteractionPrediction(
                status="blocked",
                reason_code="invalid_calibrated_output",
                phase=phase,
            )
        probability_values = exp_values / denominator
        probabilities = tuple(
            (label, float(probability_values[index]))
            for index, label in enumerate(CHANNEL_INTERACTION_LABELS)
        )
        order = np.argsort(probability_values)[::-1]
        best_index = int(order[0])
        second_index = int(order[1])
        top_class = CHANNEL_INTERACTION_LABELS[best_index]
        confidence = float(probability_values[best_index])
        top2_margin = confidence - float(probability_values[second_index])
        directional = (
            top_class
            in {
                ChannelInteractionOutcome.REVERSAL.value,
                ChannelInteractionOutcome.ACCEPTED_BREAKOUT.value,
            }
            and confidence >= self.manifest.decision_policy["min_confidence"]
            and top2_margin >= self.manifest.decision_policy["min_top2_margin"]
        )
        return ChannelInteractionPrediction(
            status="ready",
            reason_code="prediction_directional" if directional else "prediction_abstain",
            phase=phase,
            probabilities=probabilities,
            predicted_class=top_class if directional else None,
            top_class=top_class,
            decision_state="directional" if directional else "abstain",
            confidence=confidence,
            top2_margin=top2_margin,
            min_confidence=self.manifest.decision_policy["min_confidence"],
            min_top2_margin=self.manifest.decision_policy["min_top2_margin"],
            inference_scope=normalized_scope,
            artifact_contract=self.manifest.artifact_contract,
            target_contract=CHANNEL_INTERACTION_TARGET_CONTRACT,
            target_definition={
                "level_slot_timeframe": self.manifest.target_definition["level_slot_timeframe"],
                **dict(self.manifest.target_definition["by_phase"][phase]),
            },
            model_sha256=self.manifest.model_sha256,
            temperature=temperature,
        )


@dataclass(frozen=True, slots=True)
class ChannelInteractionModelLoad:
    status: Literal["unavailable", "ready"]
    reason_code: str
    model_dir: Path
    predictor: ChannelInteractionPredictor | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "reason_code": self.reason_code,
            "model_dir": str(self.model_dir),
        }
        if self.predictor is not None:
            payload.update(
                {
                    "artifact_contract": self.predictor.manifest.artifact_contract,
                    "model_sha256": self.predictor.manifest.model_sha256,
                    "phase_temperatures": dict(self.predictor.manifest.phase_temperatures),
                    "decision_policy": dict(self.predictor.manifest.decision_policy),
                    "target_definition": {
                        "target_contract": CHANNEL_INTERACTION_TARGET_CONTRACT,
                        "level_slot_timeframe": (
                            self.predictor.manifest.target_definition["level_slot_timeframe"]
                        ),
                        "by_phase": {
                            phase: dict(
                                self.predictor.manifest.target_definition["by_phase"][phase]
                            )
                            for phase in CHANNEL_INTERACTION_PHASES
                        },
                    },
                    "inference_scopes": [
                        scope.as_dict() for scope in self.predictor.manifest.inference_scopes
                    ],
                    "build_provenance": dict(self.predictor.manifest.build_provenance),
                    "dataset_build_provenance": dict(
                        self.predictor.manifest.dataset_build_provenance
                    ),
                }
            )
        return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_channel_interaction_run_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 120:
        raise ValueError("run_id_invalid")
    allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if value != value.lower() or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        raise ValueError("run_id_invalid")
    if any(character not in allowed for character in value):
        raise ValueError("run_id_invalid")
    return value


def normalize_channel_interaction_decision_policy(
    value: object,
) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or set(value) != {
        "min_confidence",
        "min_top2_margin",
    }:
        raise ValueError("decision_policy_invalid")
    try:
        min_confidence = _finite_number(
            value["min_confidence"],
            field_name="decision_policy.min_confidence",
        )
        min_top2_margin = _finite_number(
            value["min_top2_margin"],
            field_name="decision_policy.min_top2_margin",
        )
    except ValueError as exc:
        raise ValueError("decision_policy_invalid") from exc
    if not 0 < min_confidence <= 1 or not 0 <= min_top2_margin <= 1:
        raise ValueError("decision_policy_invalid")
    return MappingProxyType(
        {
            "min_confidence": min_confidence,
            "min_top2_margin": min_top2_margin,
        }
    )


def normalize_channel_interaction_phase_temperatures(
    value: object,
) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(CHANNEL_INTERACTION_PHASES):
        raise ValueError("phase_temperatures_invalid")
    normalized: dict[str, float] = {}
    for phase in CHANNEL_INTERACTION_PHASES:
        try:
            temperature = _finite_number(
                value[phase],
                field_name=f"phase_temperatures.{phase}",
            )
        except ValueError as exc:
            raise ValueError("phase_temperatures_invalid") from exc
        if temperature <= 0:
            raise ValueError("phase_temperatures_invalid")
        normalized[phase] = temperature
    return MappingProxyType(normalized)


def normalize_channel_interaction_inference_scopes(
    value: object,
) -> tuple[ChannelInteractionInferenceScope, ...]:
    if not isinstance(value, Mapping) or set(value) != {"scope_contract", "allowed"}:
        raise ValueError("inference_scope_invalid")
    if value.get("scope_contract") != CHANNEL_INTERACTION_SCOPE_CONTRACT:
        raise ValueError("inference_scope_contract_mismatch")
    allowed = value.get("allowed")
    if not isinstance(allowed, Sequence) or isinstance(allowed, (str, bytes, bytearray)):
        raise ValueError("inference_scope_invalid")
    try:
        scopes = tuple(
            sorted(ChannelInteractionInferenceScope.from_mapping(item) for item in allowed)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("inference_scope_invalid") from exc
    if not scopes or len(set(scopes)) != len(scopes):
        raise ValueError("inference_scope_invalid")
    return scopes


def normalize_channel_interaction_target_definition(
    value: object,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "target_contract",
        "level_slot_timeframe",
        "by_phase",
    }:
        raise ValueError("target_definition_invalid")
    if value.get("target_contract") != CHANNEL_INTERACTION_TARGET_CONTRACT:
        raise ValueError("target_contract_mismatch")
    if value.get("level_slot_timeframe") != CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME:
        raise ValueError("target_level_slot_timeframe_mismatch")
    by_phase = value.get("by_phase")
    if not isinstance(by_phase, Mapping) or set(by_phase) != set(CHANNEL_INTERACTION_PHASES):
        raise ValueError("target_definition_invalid")
    pre_touch = by_phase.get("pre_touch")
    post_touch = by_phase.get("post_touch")
    if not isinstance(pre_touch, Mapping) or set(pre_touch) != {
        "task",
        "labels",
        "touch_search_interval",
        "touch_wait_slots",
        "outcome_interval",
        "outcome_horizon_slots",
        "no_touch_label",
    }:
        raise ValueError("target_definition_invalid")
    if not isinstance(post_touch, Mapping) or set(post_touch) != {
        "task",
        "labels",
        "outcome_interval",
        "outcome_horizon_slots",
        "known_bars_excluded",
        "no_touch_label_allowed",
    }:
        raise ValueError("target_definition_invalid")
    if (
        pre_touch.get("task") != "first_touch_then_outcome"
        or tuple(pre_touch.get("labels") or ()) != CHANNEL_INTERACTION_LABELS_BY_PHASE["pre_touch"]
        or pre_touch.get("touch_search_interval")
        != "confirmed_1m_slots_after_decision_through_touch_wait"
        or pre_touch.get("outcome_interval")
        != "first_touch_slot_through_touch_slot_plus_horizon_minus_one"
        or pre_touch.get("no_touch_label") != ChannelInteractionOutcome.NO_TOUCH.value
        or post_touch.get("task") != "rolling_future_continuation"
        or tuple(post_touch.get("labels") or ())
        != CHANNEL_INTERACTION_LABELS_BY_PHASE["post_touch"]
        or post_touch.get("outcome_interval")
        != "decision_slot_plus_one_through_decision_slot_plus_horizon"
        or post_touch.get("known_bars_excluded") is not True
        or post_touch.get("no_touch_label_allowed") is not False
    ):
        raise ValueError("target_definition_invalid")
    for phase_payload, field_name in (
        (pre_touch, "touch_wait_slots"),
        (pre_touch, "outcome_horizon_slots"),
        (post_touch, "outcome_horizon_slots"),
    ):
        raw_value = phase_payload.get(field_name)
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 1:
            raise ValueError("target_definition_invalid")
    if pre_touch["outcome_horizon_slots"] != post_touch["outcome_horizon_slots"]:
        raise ValueError("target_definition_invalid")
    return MappingProxyType(
        {
            "target_contract": CHANNEL_INTERACTION_TARGET_CONTRACT,
            "level_slot_timeframe": CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
            "by_phase": MappingProxyType(
                {
                    "pre_touch": MappingProxyType(dict(pre_touch)),
                    "post_touch": MappingProxyType(dict(post_touch)),
                }
            ),
        }
    )


def normalize_channel_interaction_acceptance_gate(
    value: object,
) -> Mapping[str, Any]:
    required = {
        "passed",
        "statistical_passed",
        "clean_lineage_required",
        "clean_lineage_passed",
        "dataset_builder_git_dirty",
        "model_builder_git_dirty",
        "by_phase",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("acceptance_gate_invalid")
    if (
        value.get("passed") is not True
        or value.get("statistical_passed") is not True
        or value.get("clean_lineage_required") is not True
        or value.get("clean_lineage_passed") is not True
        or value.get("dataset_builder_git_dirty") is not False
        or value.get("model_builder_git_dirty") is not False
    ):
        raise ValueError("acceptance_gate_invalid")
    by_phase = value.get("by_phase")
    if not isinstance(by_phase, Mapping) or set(by_phase) != set(CHANNEL_INTERACTION_PHASES):
        raise ValueError("acceptance_gate_invalid")
    normalized_phases: dict[str, dict[str, Any]] = {}
    for phase in CHANNEL_INTERACTION_PHASES:
        gate = by_phase.get(phase)
        phase_fields = {
            "passed",
            "phase",
            "minimum_calibration_independent_episodes_per_class",
            "calibration_independent_episodes_per_class",
            "calibration_episode_count_requirements",
            "minimum_independent_episodes_per_class",
            "locked_test_independent_episodes_per_class",
            "episode_count_requirements",
            "metric_requirements",
            "selective_thresholds",
            "selective_requirements",
            "selective_observed",
            "observed",
        }
        if (
            not isinstance(gate, Mapping)
            or set(gate) != phase_fields
            or gate.get("passed") is not True
            or gate.get("phase") != phase
        ):
            raise ValueError("acceptance_gate_invalid")
        minimum = gate.get("minimum_independent_episodes_per_class")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ValueError("acceptance_gate_invalid")
        calibration_minimum = gate.get("minimum_calibration_independent_episodes_per_class")
        if (
            isinstance(calibration_minimum, bool)
            or not isinstance(calibration_minimum, int)
            or calibration_minimum < 1
        ):
            raise ValueError("acceptance_gate_invalid")
        phase_labels = CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
        calibration_counts = gate.get("calibration_independent_episodes_per_class")
        calibration_count_requirements = gate.get("calibration_episode_count_requirements")
        counts = gate.get("locked_test_independent_episodes_per_class")
        count_requirements = gate.get("episode_count_requirements")
        if (
            not isinstance(calibration_counts, Mapping)
            or set(calibration_counts) != set(phase_labels)
            or not isinstance(calibration_count_requirements, Mapping)
            or set(calibration_count_requirements) != set(phase_labels)
            or not isinstance(counts, Mapping)
            or set(counts) != set(phase_labels)
            or not isinstance(count_requirements, Mapping)
            or set(count_requirements) != set(phase_labels)
        ):
            raise ValueError("acceptance_gate_invalid")
        if any(
            isinstance(calibration_counts[label], bool)
            or not isinstance(calibration_counts[label], int)
            or calibration_counts[label] < calibration_minimum
            or calibration_count_requirements[label] is not True
            for label in phase_labels
        ):
            raise ValueError("acceptance_gate_invalid")
        if any(
            isinstance(counts[label], bool)
            or not isinstance(counts[label], int)
            or counts[label] < minimum
            or count_requirements[label] is not True
            for label in phase_labels
        ):
            raise ValueError("acceptance_gate_invalid")
        metric_names = {
            "log_loss",
            "brier_multiclass",
            "expected_calibration_error",
        }
        metric_requirements = gate.get("metric_requirements")
        observed = gate.get("observed")
        if (
            not isinstance(metric_requirements, Mapping)
            or set(metric_requirements) != metric_names
            or any(metric_requirements[name] is not True for name in metric_names)
            or not isinstance(observed, Mapping)
            or set(observed) != metric_names
        ):
            raise ValueError("acceptance_gate_invalid")
        for metric_name in metric_names:
            comparison = observed.get(metric_name)
            if not isinstance(comparison, Mapping) or set(comparison) != {
                "logistic_baseline",
                "xgboost_candidate",
            }:
                raise ValueError("acceptance_gate_invalid")
            baseline = _finite_number(
                comparison["logistic_baseline"],
                field_name=f"acceptance_gate.{phase}.{metric_name}.baseline",
            )
            candidate = _finite_number(
                comparison["xgboost_candidate"],
                field_name=f"acceptance_gate.{phase}.{metric_name}.candidate",
            )
            if candidate >= baseline if metric_name == "log_loss" else candidate > baseline:
                raise ValueError("acceptance_gate_invalid")
        threshold_names = {
            "minimum_directional_coverage",
            "minimum_directional_accuracy",
            "minimum_directional_macro_precision",
        }
        selective_names = {
            "coverage",
            "directional_accuracy",
            "directional_macro_precision",
        }
        thresholds = gate.get("selective_thresholds")
        selective_requirements = gate.get("selective_requirements")
        selective_observed = gate.get("selective_observed")
        if (
            not isinstance(thresholds, Mapping)
            or set(thresholds) != threshold_names
            or not isinstance(selective_requirements, Mapping)
            or set(selective_requirements) != selective_names
            or any(selective_requirements[name] is not True for name in selective_names)
            or not isinstance(selective_observed, Mapping)
            or set(selective_observed) != selective_names
        ):
            raise ValueError("acceptance_gate_invalid")
        threshold_values = {
            name: _finite_number(
                thresholds[name],
                field_name=f"acceptance_gate.{phase}.{name}",
            )
            for name in threshold_names
        }
        observed_values = {
            name: _finite_number(
                selective_observed[name],
                field_name=f"acceptance_gate.{phase}.{name}",
            )
            for name in selective_names
        }
        if any(not 0 < item <= 1 for item in threshold_values.values()):
            raise ValueError("acceptance_gate_invalid")
        if (
            observed_values["coverage"] < threshold_values["minimum_directional_coverage"]
            or observed_values["directional_accuracy"]
            < threshold_values["minimum_directional_accuracy"]
            or observed_values["directional_macro_precision"]
            < threshold_values["minimum_directional_macro_precision"]
        ):
            raise ValueError("acceptance_gate_invalid")
        normalized_phases[phase] = dict(gate)
    return MappingProxyType(
        {
            **{key: value[key] for key in required if key != "by_phase"},
            "by_phase": normalized_phases,
        }
    )


def normalize_channel_interaction_build_provenance(
    value: object,
    *,
    require_xgboost: bool,
) -> Mapping[str, Any]:
    fields = {
        "git_sha",
        "git_dirty",
        "build_version",
        "python_version",
        "numpy_version",
        "xgboost_version",
        "builder_module_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("build_provenance_invalid")
    normalized = dict(value)
    for field_name in (
        "build_version",
        "python_version",
        "numpy_version",
        "builder_module_sha256",
    ):
        if not isinstance(normalized[field_name], str) or not normalized[field_name].strip():
            raise ValueError("build_provenance_invalid")
    git_sha = normalized["git_sha"]
    if (
        not isinstance(git_sha, str)
        or len(git_sha) != 40
        or any(character not in "0123456789abcdef" for character in git_sha)
    ):
        raise ValueError("build_provenance_invalid")
    builder_sha = normalized["builder_module_sha256"]
    if len(builder_sha) != 64 or any(
        character not in "0123456789abcdef" for character in builder_sha
    ):
        raise ValueError("build_provenance_invalid")
    if not isinstance(normalized["git_dirty"], bool):
        raise ValueError("build_provenance_invalid")
    xgboost_version = normalized["xgboost_version"]
    if require_xgboost and (not isinstance(xgboost_version, str) or not xgboost_version.strip()):
        raise ValueError("build_provenance_invalid")
    if not require_xgboost and xgboost_version is not None:
        raise ValueError("build_provenance_invalid")
    return MappingProxyType(normalized)


def _parse_artifact_manifest(payload: object) -> ChannelInteractionArtifactManifest:
    if not isinstance(payload, Mapping):
        raise ValueError("manifest_not_object")
    required = {
        "artifact_contract",
        "backend",
        "feature_schema_version",
        "feature_columns",
        "labels",
        "phases",
        "model_file",
        "model_sha256",
        "run_id",
        "phase_temperatures",
        "trained_at",
        "metrics",
        "acceptance_gate",
        "decision_policy",
        "inference_scope",
        "build_provenance",
        "dataset_build_provenance",
        "target_definition",
        "dataset_manifest_file",
        "dataset_manifest_sha256",
        "training",
    }
    if set(payload) != required:
        raise ValueError("manifest_fields_missing")
    if payload["artifact_contract"] != CHANNEL_INTERACTION_ARTIFACT_CONTRACT:
        raise ValueError("artifact_contract_mismatch")
    if payload["backend"] != "xgboost":
        raise ValueError("artifact_backend_mismatch")
    if payload["feature_schema_version"] != CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION:
        raise ValueError("feature_schema_version_mismatch")
    columns = tuple(str(item) for item in payload["feature_columns"])
    if columns != CHANNEL_INTERACTION_FEATURE_COLUMNS:
        raise ValueError("feature_columns_mismatch")
    labels = tuple(str(item) for item in payload["labels"])
    if labels != CHANNEL_INTERACTION_LABELS:
        raise ValueError("labels_mismatch")
    phases = tuple(str(item) for item in payload["phases"])
    if phases != CHANNEL_INTERACTION_PHASES:
        raise ValueError("phases_mismatch")
    if payload["model_file"] != "model.ubj":
        raise ValueError("model_file_mismatch")
    model_sha256 = str(payload["model_sha256"])
    if len(model_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in model_sha256
    ):
        raise ValueError("model_sha256_invalid")
    run_id = require_channel_interaction_run_id(payload["run_id"])
    phase_temperatures = normalize_channel_interaction_phase_temperatures(
        payload["phase_temperatures"]
    )
    trained_at = str(payload["trained_at"])
    try:
        require_aware_utc_datetime(
            datetime.fromisoformat(trained_at.replace("Z", "+00:00")),
            field="trained_at",
        )
    except ValueError as exc:
        raise ValueError("trained_at_invalid") from exc
    if not isinstance(payload["metrics"], Mapping):
        raise ValueError("metrics_not_object")
    if not isinstance(payload["training"], Mapping):
        raise ValueError("training_not_object")
    dataset_manifest_file = str(payload["dataset_manifest_file"])
    dataset_manifest_path = Path(dataset_manifest_file)
    if (
        not dataset_manifest_file
        or dataset_manifest_path.is_absolute()
        or ".." in dataset_manifest_path.parts
    ):
        raise ValueError("dataset_manifest_file_invalid")
    dataset_manifest_sha256 = str(payload["dataset_manifest_sha256"])
    if len(dataset_manifest_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in dataset_manifest_sha256
    ):
        raise ValueError("dataset_manifest_sha256_invalid")
    acceptance_gate = normalize_channel_interaction_acceptance_gate(payload["acceptance_gate"])
    decision_policy = normalize_channel_interaction_decision_policy(payload["decision_policy"])
    inference_scopes = normalize_channel_interaction_inference_scopes(payload["inference_scope"])
    if len(inference_scopes) != 1:
        raise ValueError("artifact_requires_exactly_one_inference_scope")
    build_provenance = normalize_channel_interaction_build_provenance(
        payload["build_provenance"],
        require_xgboost=True,
    )
    dataset_build_provenance = normalize_channel_interaction_build_provenance(
        payload["dataset_build_provenance"],
        require_xgboost=False,
    )
    target_definition = normalize_channel_interaction_target_definition(
        payload["target_definition"]
    )
    if build_provenance["git_dirty"] or dataset_build_provenance["git_dirty"]:
        raise ValueError("dirty_lineage_not_activation_eligible")
    return ChannelInteractionArtifactManifest(
        artifact_contract=CHANNEL_INTERACTION_ARTIFACT_CONTRACT,
        backend="xgboost",
        feature_schema_version=CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
        feature_columns=columns,
        labels=labels,
        phases=phases,
        model_file="model.ubj",
        model_sha256=model_sha256,
        run_id=run_id,
        phase_temperatures=phase_temperatures,
        trained_at=trained_at,
        metrics=MappingProxyType(dict(payload["metrics"])),
        acceptance_gate=acceptance_gate,
        decision_policy=decision_policy,
        inference_scopes=inference_scopes,
        build_provenance=build_provenance,
        dataset_build_provenance=dataset_build_provenance,
        target_definition=target_definition,
        dataset_manifest_file=dataset_manifest_file,
        dataset_manifest_sha256=dataset_manifest_sha256,
        training=MappingProxyType(dict(payload["training"])),
    )


def load_channel_interaction_predictor(
    model_dir: str | Path | None = None,
) -> ChannelInteractionModelLoad:
    try:
        root = (
            channel_interaction_model_root().resolve(strict=False)
            if model_dir is None
            else require_channel_interaction_data_path(model_dir, kind="model")
        )
    except OSError, ValueError:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_path_outside_data_root",
            model_dir=channel_interaction_model_root(),
        )
    artifact_dir = root
    expected_manifest_sha256: str | None = None
    active_path = root / "active.json"
    if active_path.is_file():
        try:
            active_payload = json.loads(active_path.read_text(encoding="utf-8"))
            if not isinstance(active_payload, Mapping):
                raise ValueError("active_pointer_invalid")
            if active_payload.get("active_contract") != CHANNEL_INTERACTION_ACTIVE_CONTRACT:
                raise ValueError("active_contract_mismatch")
            run_id = require_channel_interaction_run_id(active_payload.get("run_id"))
            manifest_sha256 = str(active_payload.get("manifest_sha256") or "")
            if len(manifest_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in manifest_sha256
            ):
                raise ValueError("active_manifest_sha256_invalid")
            artifact_dir = (root / "runs" / run_id).resolve(strict=False)
            if not artifact_dir.is_relative_to(root.resolve(strict=False)):
                raise ValueError("active_run_path_invalid")
            expected_manifest_sha256 = manifest_sha256
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            reason = (
                str(exc) if isinstance(exc, ValueError) and str(exc) else "active_pointer_invalid"
            )
            return ChannelInteractionModelLoad(
                status="unavailable",
                reason_code=reason,
                model_dir=root,
            )
    elif (artifact_dir / "manifest.json").is_file():
        try:
            manifest_stat = (artifact_dir / "manifest.json").stat()
            expected_manifest_sha256 = "stat:" + ":".join(
                str(item)
                for item in (
                    manifest_stat.st_dev,
                    manifest_stat.st_ino,
                    manifest_stat.st_size,
                    manifest_stat.st_mtime_ns,
                    manifest_stat.st_ctime_ns,
                )
            )
        except OSError:
            return ChannelInteractionModelLoad(
                status="unavailable",
                reason_code="model_manifest_unreadable",
                model_dir=artifact_dir,
            )
    if expected_manifest_sha256 is None:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_manifest_missing",
            model_dir=artifact_dir,
        )
    return _load_channel_interaction_artifact(
        str(artifact_dir),
        expected_manifest_sha256,
    )


@lru_cache(maxsize=8)
def _load_channel_interaction_artifact(
    artifact_dir_text: str,
    manifest_revision: str,
) -> ChannelInteractionModelLoad:
    artifact_dir = Path(artifact_dir_text)
    manifest_path = artifact_dir / "manifest.json"
    model_path = artifact_dir / "model.ubj"
    if not manifest_path.is_file():
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_manifest_missing",
            model_dir=artifact_dir,
        )
    try:
        if len(manifest_revision) == 64 and sha256_file(manifest_path) != manifest_revision:
            return ChannelInteractionModelLoad(
                status="unavailable",
                reason_code="model_manifest_sha256_mismatch",
                model_dir=artifact_dir,
            )
    except OSError:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_manifest_unreadable",
            model_dir=artifact_dir,
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = _parse_artifact_manifest(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) else "model_manifest_invalid"
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code=reason,
            model_dir=artifact_dir,
        )
    if artifact_dir.parent.name == "runs" and manifest.run_id != artifact_dir.name:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="run_directory_manifest_mismatch",
            model_dir=artifact_dir,
        )
    if not model_path.is_file():
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_file_missing",
            model_dir=artifact_dir,
        )
    try:
        actual_sha256 = sha256_file(model_path)
    except OSError:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_file_unreadable",
            model_dir=artifact_dir,
        )
    if actual_sha256 != manifest.model_sha256:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_sha256_mismatch",
            model_dir=artifact_dir,
        )
    if str(np.__version__) != manifest.build_provenance["numpy_version"]:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="numpy_runtime_version_mismatch",
            model_dir=artifact_dir,
        )
    try:
        import xgboost as xgb
    except ImportError, OSError:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="xgboost_backend_unavailable",
            model_dir=artifact_dir,
        )
    if str(xgb.__version__) != manifest.build_provenance["xgboost_version"]:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="xgboost_runtime_version_mismatch",
            model_dir=artifact_dir,
        )
    try:
        booster = xgb.Booster()
        booster.load_model(str(model_path))
        if tuple(booster.feature_names or ()) != CHANNEL_INTERACTION_FEATURE_COLUMNS:
            return ChannelInteractionModelLoad(
                status="unavailable",
                reason_code="model_feature_names_mismatch",
                model_dir=artifact_dir,
            )
        config = json.loads(booster.save_config())
        learner = config.get("learner") if isinstance(config, dict) else None
        objective = learner.get("objective") if isinstance(learner, dict) else None
        objective_name = objective.get("name") if isinstance(objective, dict) else None
        learner_parameters = (
            learner.get("learner_model_param") if isinstance(learner, dict) else None
        )
        num_class = (
            learner_parameters.get("num_class") if isinstance(learner_parameters, dict) else None
        )
        if objective_name != "multi:softprob" or int(num_class or 0) != len(
            CHANNEL_INTERACTION_LABELS
        ):
            return ChannelInteractionModelLoad(
                status="unavailable",
                reason_code="model_objective_mismatch",
                model_dir=artifact_dir,
            )
    except Exception:
        return ChannelInteractionModelLoad(
            status="unavailable",
            reason_code="model_load_failed",
            model_dir=artifact_dir,
        )
    predictor = ChannelInteractionPredictor(manifest=manifest, _booster=booster)
    return ChannelInteractionModelLoad(
        status="ready",
        reason_code="model_ready",
        model_dir=artifact_dir,
        predictor=predictor,
    )


def clear_channel_interaction_model_cache() -> None:
    _load_channel_interaction_artifact.cache_clear()


def predict_channel_interaction(
    bars_1m: Sequence[Bar],
    bar_slots: Sequence[int],
    *,
    level: FrozenChannelLevel,
    decision_ts: datetime,
    phase: ChannelInteractionPhase,
    scope: ChannelInteractionInferenceScope | Mapping[str, Any],
    atr_value: float | None,
    touch_slot: int | None = None,
    model_dir: str | Path | None = None,
) -> ChannelInteractionPrediction:
    features = extract_channel_interaction_features(
        bars_1m,
        bar_slots,
        level=level,
        decision_ts=decision_ts,
        phase=phase,
        atr_value=atr_value,
        touch_slot=touch_slot,
    )
    if features.status != "ready":
        return ChannelInteractionPrediction(
            status="blocked",
            reason_code=features.reason_code,
            phase=phase,
        )
    loaded = load_channel_interaction_predictor(model_dir)
    if loaded.status != "ready" or loaded.predictor is None:
        return ChannelInteractionPrediction(
            status="unavailable",
            reason_code=loaded.reason_code,
            phase=phase,
        )
    return loaded.predictor.predict(features, phase=phase, scope=scope)
