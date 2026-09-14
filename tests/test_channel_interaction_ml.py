from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from aef_terminal.backtest.labels import channel_moving_line_outcome
from aef_terminal.domain import Bar
from aef_terminal.ml import channel_interaction as interaction
from aef_terminal.ml import channel_interaction_training as interaction_training
from aef_terminal.ml.channel_interaction_contracts import ChannelInteractionOutcome
from aef_terminal.ml.channel_interaction import (
    CHANNEL_INTERACTION_ARTIFACT_CONTRACT,
    CHANNEL_INTERACTION_FEATURE_COLUMNS,
    CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
    CHANNEL_INTERACTION_LABELS,
    CHANNEL_INTERACTION_LABELS_BY_PHASE,
    CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
    CHANNEL_INTERACTION_PHASES,
    CHANNEL_INTERACTION_SCOPE_CONTRACT,
    CHANNEL_INTERACTION_TARGET_CONTRACT,
    ChannelInteractionArtifactManifest,
    ChannelInteractionFeatureResult,
    ChannelInteractionInferenceScope,
    ChannelInteractionPredictor,
    FrozenChannelLevel,
    extract_channel_interaction_features,
    load_channel_interaction_predictor,
)
from aef_terminal.ml.channel_interaction_training import (
    ChannelInteractionDatasetBuildConfig,
    build_channel_interaction_dataset_rows,
    channel_interaction_target_definition,
    chronological_episode_splits,
    fit_positive_temperature,
    independent_phase_label_episode_support,
    locked_test_acceptance_gate,
    multiclass_metrics,
    publish_xgboost_artifact,
    write_curated_dataset,
)
from aef_terminal.research.channel_interaction_journal import ChannelInteractionJournal


BASE = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)

MODEL_BUILD_PROVENANCE = {
    "git_sha": "a" * 40,
    "git_dirty": False,
    "build_version": "test",
    "python_version": "3.14.0",
    "numpy_version": "2.3.3",
    "xgboost_version": "3.3.0",
    "builder_module_sha256": "b" * 64,
}
DATASET_BUILD_PROVENANCE = {
    **MODEL_BUILD_PROVENANCE,
    "xgboost_version": None,
}


def test_training_and_inference_share_one_sha256_file_owner() -> None:
    assert interaction_training.sha256_file is interaction.sha256_file


_NORMALIZED_TARGET_DEFINITION = channel_interaction_target_definition(
    ChannelInteractionDatasetBuildConfig()
)
TARGET_DEFINITION = {
    "target_contract": CHANNEL_INTERACTION_TARGET_CONTRACT,
    "level_slot_timeframe": CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
    "by_phase": {
        phase: dict(_NORMALIZED_TARGET_DEFINITION["by_phase"][phase])
        for phase in CHANNEL_INTERACTION_PHASES
    },
}


def _accepted_phase_gate(phase: str) -> dict[str, object]:
    labels = CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
    return {
        "passed": True,
        "phase": phase,
        "minimum_calibration_independent_episodes_per_class": 1,
        "calibration_independent_episodes_per_class": {label: 1 for label in labels},
        "calibration_episode_count_requirements": {label: True for label in labels},
        "minimum_independent_episodes_per_class": 1,
        "locked_test_independent_episodes_per_class": {label: 1 for label in labels},
        "episode_count_requirements": {label: True for label in labels},
        "metric_requirements": {
            "log_loss": True,
            "brier_multiclass": True,
            "expected_calibration_error": True,
        },
        "selective_thresholds": {
            "minimum_directional_coverage": 0.05,
            "minimum_directional_accuracy": 0.55,
            "minimum_directional_macro_precision": 0.55,
        },
        "selective_requirements": {
            "coverage": True,
            "directional_accuracy": True,
            "directional_macro_precision": True,
        },
        "selective_observed": {
            "coverage": 0.25,
            "directional_accuracy": 0.70,
            "directional_macro_precision": 0.68,
        },
        "observed": {
            "log_loss": {
                "logistic_baseline": 1.0,
                "xgboost_candidate": 0.9,
            },
            "brier_multiclass": {
                "logistic_baseline": 0.7,
                "xgboost_candidate": 0.65,
            },
            "expected_calibration_error": {
                "logistic_baseline": 0.12,
                "xgboost_candidate": 0.10,
            },
        },
    }


ACCEPTED_GATE = {
    "passed": True,
    "statistical_passed": True,
    "clean_lineage_required": True,
    "clean_lineage_passed": True,
    "dataset_builder_git_dirty": False,
    "model_builder_git_dirty": False,
    "by_phase": {phase: _accepted_phase_gate(phase) for phase in CHANNEL_INTERACTION_PHASES},
}


def _quality() -> dict[str, object]:
    return {
        "ok": True,
        "provider_complete": True,
        "pending_slot_count": 0,
        "slot_axis_authoritative": True,
        "slot_schedule_state": "verified",
    }


def _bar(
    index: int,
    *,
    ts: datetime | None = None,
    open_price: float = 99.5,
    high: float = 99.8,
    low: float = 99.2,
    close: float = 99.6,
    volume: float = 100.0,
) -> Bar:
    return Bar(
        symbol="ESU6",
        ts=ts or BASE + timedelta(minutes=index),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe="1m",
        source="ibkr:historical",
    )


def _level(*, side: str = "resistance") -> FrozenChannelLevel:
    return FrozenChannelLevel(
        anchor_slot=0,
        anchor_price=100.0,
        slope_per_slot=0.0,
        slot_timeframe=CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
        side=side,
        frozen_revision="drawing-revision-1",
        frozen_at=BASE - timedelta(minutes=1),
    )


def _wire_bar(bar: Bar) -> dict[str, object]:
    return {
        "symbol": bar.symbol,
        "ts": bar.ts.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "timeframe": bar.timeframe,
        "source": bar.source,
        "closed": True,
        "state": "confirmed",
        "session_key": "2026-07-01",
        "provenance": {
            "provider": "ibkr",
            "instrument_id": "ibkr:exact-contract-1",
            "route_fingerprint": "route-fingerprint-1",
            "request_type": "canonical_storage",
            "provider_contract_id": "700001",
            "provider_contract_type": "CANONICAL_STORAGE",
            "data_type": "canonical_ohlcv",
            "source_timeframe": None,
        },
    }


def _journal_envelope(
    tmp_path: Path,
    *,
    namespace: str,
    observation: dict[str, object],
    identity: dict[str, str],
) -> dict[str, object]:
    typed_observation = dict(observation)
    typed_observation.pop("identity", None)
    typed_observation["schema_version"] = 1
    eligible = typed_observation["sample_eligible"] is True
    typed_observation["sample_reason_code"] = "sample_ready" if eligible else "sample_ineligible"
    if eligible:
        typed_observation["feature_schema_version"] = 1
        typed_observation["features"] = {"distance_close_atr": 0.0}
    else:
        typed_observation.pop("feature_schema_version", None)
        typed_observation.pop("features", None)
    journal = ChannelInteractionJournal(data_root=tmp_path / namespace)
    result = journal.record_observation(typed_observation, identity=identity)
    payload = json.loads(result.path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_causal_features_reject_a_bar_whose_close_is_after_decision() -> None:
    bars = [_bar(index) for index in range(16)]
    result = extract_channel_interaction_features(
        bars,
        list(range(16)),
        level=_level(),
        decision_ts=bars[-1].ts,
        phase="pre_touch",
        atr_value=1.0,
    )

    assert result.status == "blocked"
    assert result.reason_code == "future_1m_close_in_context"
    assert dict(result.features) == {}


def test_causal_features_have_exact_order_and_explicit_missing_values() -> None:
    bars = [_bar(index, volume=0.0) for index in range(5)]
    result = extract_channel_interaction_features(
        bars,
        list(range(5)),
        level=_level(),
        decision_ts=bars[-1].ts + timedelta(minutes=1),
        phase="pre_touch",
        atr_value=None,
    )

    assert result.status == "ready"
    assert result.feature_columns == CHANNEL_INTERACTION_FEATURE_COLUMNS
    assert tuple(result.features) == CHANNEL_INTERACTION_FEATURE_COLUMNS
    assert result.features["atr_missing"] == 1.0
    assert result.features["history_15_missing"] == 1.0
    assert result.features["volume_change_1_missing"] == 1.0
    assert result.features["touch_missing"] == 1.0


def test_post_touch_features_require_the_frozen_line_to_cross_touch_bar() -> None:
    bars = [_bar(index) for index in range(15)]
    bars.append(_bar(15, open_price=99.7, high=100.2, low=99.5, close=99.9))
    result = extract_channel_interaction_features(
        bars,
        list(range(16)),
        level=_level(),
        decision_ts=bars[-1].ts + timedelta(minutes=1),
        phase="post_touch",
        atr_value=1.0,
        touch_slot=15,
    )

    assert result.status == "ready"
    assert result.features["phase_post_touch"] == 1.0
    assert result.features["touch_missing"] == 0.0


def test_moving_line_outcome_marks_intrabar_dual_hit_unclear() -> None:
    outcome = channel_moving_line_outcome(
        [_bar(1, open_price=100.0, high=101.0, low=99.0, close=100.2)],
        [4],
        anchor_slot=2,
        anchor_price=98.0,
        slope_per_slot=1.0,
        side="resistance",
        breakout_distance=0.5,
        reversal_distance=0.5,
        acceptance_distance=0.2,
    )

    assert outcome is ChannelInteractionOutcome.UNCLEAR


def test_moving_line_outcome_requires_confirmed_closes_for_accepted_breakout() -> None:
    outcome = channel_moving_line_outcome(
        [
            _bar(1, open_price=100.0, high=100.8, low=99.9, close=100.4),
            _bar(2, open_price=100.4, high=100.9, low=100.1, close=100.5),
        ],
        [1, 2],
        anchor_slot=0,
        anchor_price=100.0,
        slope_per_slot=0.0,
        side="resistance",
        breakout_distance=0.5,
        reversal_distance=0.5,
        acceptance_distance=0.2,
        acceptance_closes=2,
    )

    assert outcome is ChannelInteractionOutcome.ACCEPTED_BREAKOUT


def test_loader_fails_closed_when_ubj_sha_does_not_match_manifest(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interaction, "channel_interaction_model_root", lambda _config=None: tmp_path
    )
    (tmp_path / "model.ubj").write_bytes(b"not-a-model")
    manifest = {
        "artifact_contract": CHANNEL_INTERACTION_ARTIFACT_CONTRACT,
        "backend": "xgboost",
        "feature_schema_version": CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
        "feature_columns": list(CHANNEL_INTERACTION_FEATURE_COLUMNS),
        "labels": list(CHANNEL_INTERACTION_LABELS),
        "phases": list(CHANNEL_INTERACTION_PHASES),
        "model_file": "model.ubj",
        "model_sha256": "0" * 64,
        "run_id": "test-run",
        "phase_temperatures": {phase: 1.0 for phase in CHANNEL_INTERACTION_PHASES},
        "trained_at": BASE.isoformat(),
        "metrics": {},
        "acceptance_gate": ACCEPTED_GATE,
        "decision_policy": {"min_confidence": 0.60, "min_top2_margin": 0.15},
        "inference_scope": {
            "scope_contract": CHANNEL_INTERACTION_SCOPE_CONTRACT,
            "allowed": [
                {
                    "provider": "ibkr",
                    "instrument_id": "instrument-1",
                    "parent_timeframe": "5m",
                }
            ],
        },
        "build_provenance": MODEL_BUILD_PROVENANCE,
        "dataset_build_provenance": DATASET_BUILD_PROVENANCE,
        "target_definition": TARGET_DEFINITION,
        "dataset_manifest_file": "training/test-run/manifest.json",
        "dataset_manifest_sha256": "c" * 64,
        "training": {},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_channel_interaction_predictor()

    assert loaded.status == "unavailable"
    assert loaded.reason_code == "model_sha256_mismatch"


def test_temperature_calibration_is_strictly_positive() -> None:
    logits = np.asarray(
        [
            [3.0, 0.0, -1.0, -2.0],
            [2.0, 0.5, -0.5, -1.5],
            [-1.0, 3.0, 0.0, -2.0],
        ],
        dtype=np.float64,
    )
    labels = np.asarray([0, 1, 1], dtype=np.int64)

    temperature = fit_positive_temperature(logits, labels)

    assert temperature > 0


def test_predictor_abstains_on_low_margin_and_blocks_scope_mismatch() -> None:
    scope = ChannelInteractionInferenceScope("ibkr", "instrument-1", "5m")
    manifest = ChannelInteractionArtifactManifest(
        artifact_contract=CHANNEL_INTERACTION_ARTIFACT_CONTRACT,
        backend="xgboost",
        feature_schema_version=CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
        feature_columns=CHANNEL_INTERACTION_FEATURE_COLUMNS,
        labels=CHANNEL_INTERACTION_LABELS,
        phases=CHANNEL_INTERACTION_PHASES,
        model_file="model.ubj",
        model_sha256="a" * 64,
        run_id="test-run",
        phase_temperatures={"pre_touch": 1.0, "post_touch": 2.0},
        trained_at=BASE.isoformat(),
        metrics={},
        acceptance_gate=ACCEPTED_GATE,
        decision_policy={"min_confidence": 0.60, "min_top2_margin": 0.15},
        inference_scopes=(scope,),
        build_provenance=MODEL_BUILD_PROVENANCE,
        dataset_build_provenance=DATASET_BUILD_PROVENANCE,
        target_definition=TARGET_DEFINITION,
        dataset_manifest_file="training/test-run/manifest.json",
        dataset_manifest_sha256="c" * 64,
        training={},
    )
    features = ChannelInteractionFeatureResult(
        status="ready",
        reason_code="features_ready",
        phase="pre_touch",
        decision_ts=BASE,
        feature_values=tuple(0.0 for _column in CHANNEL_INTERACTION_FEATURE_COLUMNS),
    )

    class LowMarginBooster:
        @staticmethod
        def inplace_predict(*_args, **_kwargs):
            return np.asarray([[0.4, 0.3, 0.2, 0.1]], dtype=np.float64)

    class DirectionalBooster:
        @staticmethod
        def inplace_predict(*_args, **_kwargs):
            return np.asarray([[4.0, 0.0, 0.0, 0.0]], dtype=np.float64)

    class NoTouchBooster:
        @staticmethod
        def inplace_predict(*_args, **_kwargs):
            return np.asarray([[0.0, 0.0, 0.0, 8.0]], dtype=np.float64)

    prediction = ChannelInteractionPredictor(manifest, LowMarginBooster()).predict(
        features,
        phase="pre_touch",
        scope=scope,
    )
    mismatch = ChannelInteractionPredictor(manifest, LowMarginBooster()).predict(
        features,
        phase="pre_touch",
        scope=ChannelInteractionInferenceScope("ibkr", "instrument-2", "5m"),
    )
    directional = ChannelInteractionPredictor(manifest, DirectionalBooster()).predict(
        features,
        phase="pre_touch",
        scope=scope,
    )
    post_features = ChannelInteractionFeatureResult(
        status="ready",
        reason_code="features_ready",
        phase="post_touch",
        decision_ts=BASE,
        feature_values=tuple(0.0 for _column in CHANNEL_INTERACTION_FEATURE_COLUMNS),
    )
    post_prediction = ChannelInteractionPredictor(manifest, NoTouchBooster()).predict(
        post_features,
        phase="post_touch",
        scope=scope,
    )

    assert prediction.status == "ready"
    assert prediction.decision_state == "abstain"
    assert prediction.predicted_class is None
    assert set(prediction.probability_map) == set(CHANNEL_INTERACTION_LABELS)
    assert mismatch.status == "blocked"
    assert mismatch.reason_code == "model_scope_mismatch"
    assert directional.decision_state == "directional"
    assert directional.predicted_class == "reversal"
    assert directional.target_contract == CHANNEL_INTERACTION_TARGET_CONTRACT
    assert (
        directional.target_definition["level_slot_timeframe"]
        == CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME
    )
    assert directional.target_definition["task"] == "first_touch_then_outcome"
    assert post_prediction.probability_map["no_touch"] == 0.0
    assert post_prediction.temperature == 2.0
    assert post_prediction.target_definition["task"] == "rolling_future_continuation"


def test_locked_test_metrics_report_selective_coverage_and_accuracy() -> None:
    logits = np.asarray(
        [
            [4.0, 0.0, 0.0, 0.0],
            [0.0, 4.0, 0.0, 0.0],
            [0.1, 0.1, 0.1, 0.1],
            [0.0, 0.0, 0.0, 4.0],
        ],
        dtype=np.float64,
    )
    labels = np.asarray([0, 1, 2, 3], dtype=np.int64)

    metrics = multiclass_metrics(
        logits,
        labels,
        temperature=1.0,
        decision_policy={"min_confidence": 0.60, "min_top2_margin": 0.15},
    )

    assert metrics["selective"]["directional_rows"] == 2
    assert metrics["selective"]["coverage"] == 0.5
    assert metrics["selective"]["directional_accuracy"] == 1.0


def test_locked_test_gate_requires_each_class_and_beats_unweighted_baseline() -> None:
    baseline = {
        "log_loss": 1.0,
        "brier_multiclass": 0.70,
        "expected_calibration_error": 0.12,
    }
    candidate = {
        "log_loss": 0.90,
        "brier_multiclass": 0.65,
        "expected_calibration_error": 0.10,
        "selective": {
            "coverage": 0.25,
            "directional_accuracy": 0.70,
            "directional_macro_precision": 0.68,
        },
    }
    rows = [
        {
            "phase": "pre_touch",
            "label": label,
            "instrument_id": "instrument-1",
            "route_fingerprint": "route-1",
            "episode_id": f"{label}-{episode_index}",
        }
        for label in CHANNEL_INTERACTION_LABELS_BY_PHASE["pre_touch"]
        for episode_index in range(30)
    ]

    accepted = locked_test_acceptance_gate(
        baseline_metrics=baseline,
        candidate_metrics=candidate,
        calibration_rows=rows,
        locked_test_rows=rows,
        phase="pre_touch",
        minimum_calibration_episodes_per_class=30,
        minimum_episodes_per_class=30,
        minimum_directional_coverage=0.05,
        minimum_directional_accuracy=0.55,
        minimum_directional_macro_precision=0.55,
    )
    sparse = locked_test_acceptance_gate(
        baseline_metrics=baseline,
        candidate_metrics=candidate,
        calibration_rows=rows,
        locked_test_rows=rows[:-1],
        phase="pre_touch",
        minimum_calibration_episodes_per_class=30,
        minimum_episodes_per_class=30,
        minimum_directional_coverage=0.05,
        minimum_directional_accuracy=0.55,
        minimum_directional_macro_precision=0.55,
    )

    assert accepted["passed"] is True
    assert accepted["calibration_independent_episodes_per_class"]["no_touch"] == 30
    assert accepted["locked_test_independent_episodes_per_class"]["reversal"] == 30
    assert sparse["passed"] is False


@pytest.mark.parametrize("phase", CHANNEL_INTERACTION_PHASES)
def test_calibration_support_requires_each_allowed_phase_label(phase: str) -> None:
    labels = CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
    rows = [
        {
            "phase": phase,
            "label": label,
            "instrument_id": "instrument-1",
            "route_fingerprint": "route-1",
            "episode_id": f"{phase}-{label}-{episode_index}",
        }
        for label in labels
        for episode_index in range(2)
    ]

    accepted = independent_phase_label_episode_support(
        rows,
        phase=phase,
        split_name="calibration",
        minimum_episodes_per_class=2,
    )
    sparse = independent_phase_label_episode_support(
        rows[:-1],
        phase=phase,
        split_name="calibration",
        minimum_episodes_per_class=2,
    )

    assert accepted["passed"] is True
    assert set(accepted["independent_episodes_per_class"]) == set(labels)
    assert sparse["passed"] is False


def test_dataset_builder_labels_from_later_causal_micro_window_only(tmp_path: Path) -> None:
    context = [_bar(index) for index in range(16)]
    decision_ts = context[-1].ts + timedelta(minutes=1)
    future = [
        _bar(
            16 + index,
            open_price=100.0 if index else 99.8,
            high=100.5,
            low=99.7 if index == 0 else 99.9,
            close=100.2,
        )
        for index in range(20)
    ]
    level = _level().as_dict()
    base_observation = {
        "episode_id": "episode-1",
        "sample_eligible": True,
        "phase": "pre_touch",
        "decision_ts": decision_ts.isoformat(),
        "atr_value": 1.0,
        "touch_slot": None,
        "frozen_level": level,
        "quality": _quality(),
        "decision_session": {
            "contract": "provider-session-decision-v1",
            "state": "known",
            "source": "provider_schedule",
            "calendar": "CME",
            "timezone": "America/Chicago",
            "session_key": "2026-07-01",
            "segment": "liquid",
        },
    }
    later_observation = {
        **base_observation,
        "phase": "post_touch",
        "decision_ts": (decision_ts + timedelta(minutes=20)).isoformat(),
        "touch_slot": 16,
    }
    identity = {
        "instrument_id": "ibkr:exact-contract-1",
        "route_fingerprint": "route-fingerprint-1",
        "provider": "ibkr",
        "provider_contract_id": "700001",
        "provider_symbol": "ESU6",
        "parent_timeframe": "5m",
    }
    records = [
        _journal_envelope(
            tmp_path,
            namespace="causal-base",
            identity=identity,
            observation={
                **base_observation,
                "micro_window": {
                    "bars": [_wire_bar(bar) for bar in context],
                    "bar_slots": list(range(16)),
                },
            },
        ),
        _journal_envelope(
            tmp_path,
            namespace="causal-later",
            identity=identity,
            observation={
                **later_observation,
                "micro_window": {
                    "bars": [_wire_bar(bar) for bar in [*context, *future]],
                    "bar_slots": list(range(36)),
                },
            },
        ),
    ]
    revised_future = [_wire_bar(bar) for bar in future]
    revised_future[0]["volume"] = 12345.0
    final_bar = _bar(36, open_price=100.2, high=100.6, low=100.0, close=100.3)
    records.append(
        _journal_envelope(
            tmp_path,
            namespace="causal-latest-provider-revision",
            identity=identity,
            observation={
                **later_observation,
                "sample_eligible": False,
                "decision_ts": (decision_ts + timedelta(minutes=21)).isoformat(),
                "micro_window": {
                    "bars": [
                        *[_wire_bar(bar) for bar in context],
                        *revised_future,
                        _wire_bar(final_bar),
                    ],
                    "bar_slots": list(range(37)),
                },
            },
        )
    )

    rows, report = build_channel_interaction_dataset_rows(
        records,
        config=ChannelInteractionDatasetBuildConfig(),
    )

    assert report["curated_rows"] == 1
    assert rows[0]["episode_id"] == "episode-1"
    assert rows[0]["provider"] == "ibkr"
    assert rows[0]["parent_timeframe"] == "5m"
    assert rows[0]["session_key"] == "2026-07-01"
    assert rows[0]["session_segment"] == "liquid"
    assert report["session_segment_counts"] == {"liquid": 1}
    assert rows[0]["target_contract"] == CHANNEL_INTERACTION_TARGET_CONTRACT
    assert rows[0]["level_slot_timeframe"] == CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME
    assert rows[0]["target_start_slot"] == 16
    assert rows[0]["target_end_slot"] == 30
    assert rows[0]["label"] == "accepted_breakout"
    assert CHANNEL_INTERACTION_LABELS[-1] == "no_touch"

    corrupted = json.loads(json.dumps(records[0]))
    corrupted["observation"]["micro_window"]["bars"][0]["provenance"]["route_fingerprint"] = (
        "wrong-route"
    )
    with pytest.raises(
        ValueError,
        match=rf"record_index=0 record_id={records[0]['record_id']}.*provenance",
    ):
        build_channel_interaction_dataset_rows([corrupted])


def test_dataset_builder_rebases_shifted_capture_slots_by_confirmed_timestamp(
    tmp_path: Path,
) -> None:
    context = [_bar(index) for index in range(16)]
    future = [
        _bar(
            16 + index,
            open_price=100.0 if index else 99.8,
            high=100.5,
            low=99.7 if index == 0 else 99.9,
            close=100.2,
        )
        for index in range(20)
    ]
    identity = {
        "instrument_id": "ibkr:exact-contract-1",
        "route_fingerprint": "route-fingerprint-1",
        "provider": "ibkr",
        "provider_contract_id": "700001",
        "provider_symbol": "ESU6",
        "parent_timeframe": "5m",
    }
    level = {**_level().as_dict(), "anchor_slot": 100}
    base = {
        "episode_id": "episode-shifted-slots",
        "sample_eligible": True,
        "phase": "pre_touch",
        "decision_ts": (context[-1].ts + timedelta(minutes=1)).isoformat(),
        "atr_value": 1.0,
        "touch_slot": None,
        "frozen_level": level,
        "quality": _quality(),
        "decision_session": None,
        "micro_window": {
            "bars": [_wire_bar(bar) for bar in context],
            "bar_slots": list(range(100, 116)),
        },
    }
    later = {
        **base,
        "sample_eligible": False,
        "decision_ts": (future[-1].ts + timedelta(minutes=1)).isoformat(),
        "micro_window": {
            "bars": [_wire_bar(bar) for bar in [*context, *future]],
            "bar_slots": list(range(10_000, 10_036)),
        },
    }

    rows, report = build_channel_interaction_dataset_rows(
        [
            _journal_envelope(
                tmp_path,
                namespace="shifted-slots-base",
                observation=base,
                identity=identity,
            ),
            _journal_envelope(
                tmp_path,
                namespace="shifted-slots-later",
                observation=later,
                identity=identity,
            ),
        ]
    )

    assert report["curated_rows"] == 1
    assert rows[0]["target_start_slot"] == 116
    assert rows[0]["target_end_slot"] == 130
    assert rows[0]["label"] == "accepted_breakout"


def test_dataset_builder_rejects_inconsistent_timestamp_overlap_offset(
    tmp_path: Path,
) -> None:
    context = [_bar(index) for index in range(16)]
    future = [_bar(16 + index) for index in range(20)]
    identity = {
        "instrument_id": "ibkr:exact-contract-1",
        "route_fingerprint": "route-fingerprint-1",
        "provider": "ibkr",
        "provider_contract_id": "700001",
        "provider_symbol": "ESU6",
        "parent_timeframe": "5m",
    }
    base = {
        "episode_id": "episode-inconsistent-shift",
        "sample_eligible": True,
        "phase": "pre_touch",
        "decision_ts": (context[-1].ts + timedelta(minutes=1)).isoformat(),
        "atr_value": 1.0,
        "touch_slot": None,
        "frozen_level": {**_level().as_dict(), "anchor_slot": 100},
        "quality": _quality(),
        "decision_session": None,
        "micro_window": {
            "bars": [_wire_bar(bar) for bar in context],
            "bar_slots": list(range(100, 116)),
        },
    }
    inconsistent_slots = [*range(10_000, 10_015), *range(10_016, 10_037)]
    later = {
        **base,
        "sample_eligible": False,
        "decision_ts": (future[-1].ts + timedelta(minutes=1)).isoformat(),
        "micro_window": {
            "bars": [_wire_bar(bar) for bar in [*context, *future]],
            "bar_slots": inconsistent_slots,
        },
    }
    records = [
        _journal_envelope(
            tmp_path,
            namespace="inconsistent-shift-base",
            observation=base,
            identity=identity,
        ),
        _journal_envelope(
            tmp_path,
            namespace="inconsistent-shift-later",
            observation=later,
            identity=identity,
        ),
    ]

    with pytest.raises(ValueError, match="confirmed 1m overlap changed logical offset"):
        build_channel_interaction_dataset_rows(records)


def test_post_touch_target_uses_contiguous_future_slots_across_session_break(
    tmp_path: Path,
) -> None:
    context = [_bar(index) for index in range(15)]
    context.append(_bar(15, open_price=99.7, high=100.2, low=98.5, close=99.9))
    future = [
        _bar(
            16 + index,
            ts=BASE + timedelta(days=2, minutes=16 + index),
            open_price=100.0,
            high=100.6,
            low=99.9,
            close=100.3,
        )
        for index in range(15)
    ]
    identity = {
        "instrument_id": "ibkr:exact-contract-1",
        "route_fingerprint": "route-fingerprint-1",
        "provider": "ibkr",
        "provider_contract_id": "700001",
        "provider_symbol": "ESU6",
        "parent_timeframe": "5m",
    }
    base = {
        "episode_id": "episode-session-break",
        "sample_eligible": True,
        "phase": "post_touch",
        "decision_ts": (context[-1].ts + timedelta(minutes=1)).isoformat(),
        "atr_value": 1.0,
        "touch_slot": 15,
        "frozen_level": _level().as_dict(),
        "quality": _quality(),
        "identity": identity,
        "micro_window": {
            "bars": [_wire_bar(bar) for bar in context],
            "bar_slots": list(range(16)),
        },
    }
    carrier = {
        **base,
        "sample_eligible": False,
        "decision_ts": (future[-1].ts + timedelta(minutes=1)).isoformat(),
        "micro_window": {
            "bars": [_wire_bar(bar) for bar in [*context, *future]],
            "bar_slots": list(range(31)),
        },
    }

    rows, report = build_channel_interaction_dataset_rows(
        [
            _journal_envelope(
                tmp_path,
                namespace="session-base",
                observation=base,
                identity=identity,
            ),
            _journal_envelope(
                tmp_path,
                namespace="session-carrier",
                observation=carrier,
                identity=identity,
            ),
        ]
    )

    assert report["curated_rows"] == 1
    assert rows[0]["label"] == "accepted_breakout"
    assert rows[0]["target_start_slot"] == 16
    assert rows[0]["target_end_slot"] == 30
    assert rows[0]["outcome_end_ts"] == (future[-1].ts + timedelta(minutes=1)).isoformat()

    gap_carrier = {
        **carrier,
        "micro_window": {
            "bars": [
                _wire_bar(bar) for index, bar in enumerate([*context, *future]) if index != 20
            ],
            "bar_slots": [slot for slot in range(31) if slot != 20],
        },
    }
    gap_rows, gap_report = build_channel_interaction_dataset_rows(
        [
            _journal_envelope(
                tmp_path,
                namespace="gap-base",
                observation=base,
                identity=identity,
            ),
            _journal_envelope(
                tmp_path,
                namespace="gap-carrier",
                observation=gap_carrier,
                identity=identity,
            ),
        ]
    )

    assert gap_rows == []
    assert gap_report["skipped"]["outcome_slot_gap"] == 1


def test_chronological_splits_keep_episode_rows_together_and_ordered() -> None:
    rows = []
    for episode_index in range(40):
        start = BASE + timedelta(hours=episode_index * 3)
        for phase in CHANNEL_INTERACTION_PHASES:
            rows.append(
                {
                    "episode_id": f"episode-{episode_index // 2:02d}",
                    "instrument_id": f"instrument-{episode_index % 2}",
                    "route_fingerprint": f"route-{episode_index % 2}",
                    "phase": phase,
                    "decision_ts": start.isoformat(),
                    "outcome_end_ts": (start + timedelta(minutes=15)).isoformat(),
                }
            )

    splits, report = chronological_episode_splits(rows, purge_minutes=15, embargo_minutes=5)

    episode_sets = {
        name: {
            (row["instrument_id"], row["route_fingerprint"], row["episode_id"])
            for row in split_rows
        }
        for name, split_rows in splits.items()
    }
    assert all(
        episode_sets[left].isdisjoint(episode_sets[right])
        for left in episode_sets
        for right in episode_sets
        if left != right
    )
    assert sum(report["episode_counts"].values()) == 40
    assert report["episode_counts"]["train"] > report["episode_counts"]["validation"]
    assert max(row["decision_ts"] for row in splits["train"]) < min(
        row["decision_ts"] for row in splits["validation"]
    )


def test_training_dataset_and_model_runs_publish_create_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_root = tmp_path / "datasets" / "channel_interactions"
    model_root = tmp_path / "models" / "channel_interaction"
    monkeypatch.setattr(
        interaction, "channel_interaction_dataset_root", lambda _config=None: dataset_root
    )
    monkeypatch.setattr(
        interaction, "channel_interaction_model_root", lambda _config=None: model_root
    )
    monkeypatch.setattr(
        interaction_training, "channel_interaction_dataset_root", lambda: dataset_root
    )
    monkeypatch.setattr(
        interaction_training,
        "channel_interaction_build_provenance",
        lambda **_kwargs: DATASET_BUILD_PROVENANCE,
    )

    source_dir = dataset_root / "curated" / "source-v1"
    source_dir.mkdir(parents=True)
    source_records = source_dir / "records.jsonl"
    source_records.write_text("{}\n", encoding="utf-8")
    source_manifest = source_dir / "manifest.json"
    source_manifest.write_text("{}\n", encoding="utf-8")
    output = dataset_root / "training" / "derived-v1" / "channel_interactions.csv"
    dataset_manifest = output.parent / "manifest.json"
    row = {
        "dataset_contract": interaction_training.CHANNEL_INTERACTION_DATASET_CONTRACT,
        "feature_schema_version": CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
        "episode_id": "episode-1",
        "provider": "ibkr",
        "instrument_id": "instrument-1",
        "route_fingerprint": "route-1",
        "parent_timeframe": "5m",
        "session_key": "2026-07-01",
        "session_segment": "liquid",
        "session_calendar": "CME",
        "session_timezone": "America/Chicago",
        "phase": "pre_touch",
        "target_contract": CHANNEL_INTERACTION_TARGET_CONTRACT,
        "level_slot_timeframe": CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
        "decision_ts": BASE.isoformat(),
        "decision_slot": 100,
        "target_start_slot": 101,
        "target_end_slot": 115,
        "outcome_end_ts": (BASE + timedelta(minutes=15)).isoformat(),
        "frozen_revision": "revision-1",
        "label": "unclear",
        **dict.fromkeys(CHANNEL_INTERACTION_FEATURE_COLUMNS, 0.0),
    }
    config = ChannelInteractionDatasetBuildConfig()
    write_curated_dataset(
        [row],
        raw_path=source_records,
        output_path=output,
        manifest_path=dataset_manifest,
        config=config,
        report={},
    )
    with pytest.raises(FileExistsError):
        write_curated_dataset(
            [row],
            raw_path=source_records,
            output_path=output,
            manifest_path=dataset_manifest,
            config=config,
            report={},
        )

    class FakeBooster:
        @staticmethod
        def save_model(path: str) -> None:
            Path(path).write_bytes(b"fake-ubj")

    with pytest.raises(ValueError, match="exactly one inference scope"):
        publish_xgboost_artifact(
            FakeBooster(),
            output_dir=model_root,
            run_id="multi-scope-rejected",
            phase_temperatures={phase: 1.0 for phase in CHANNEL_INTERACTION_PHASES},
            metrics={},
            training={},
            acceptance_gate=ACCEPTED_GATE,
            decision_policy={"min_confidence": 0.60, "min_top2_margin": 0.15},
            inference_scopes=(
                ChannelInteractionInferenceScope("ibkr", "instrument-1", "5m"),
                ChannelInteractionInferenceScope("ibkr", "instrument-2", "5m"),
            ),
            target_definition=TARGET_DEFINITION,
            build_provenance=MODEL_BUILD_PROVENANCE,
            dataset_manifest_path=dataset_manifest,
        )

    published = publish_xgboost_artifact(
        FakeBooster(),
        output_dir=model_root,
        run_id="derived-v1",
        phase_temperatures={phase: 1.0 for phase in CHANNEL_INTERACTION_PHASES},
        metrics={},
        training={},
        acceptance_gate=ACCEPTED_GATE,
        decision_policy={"min_confidence": 0.60, "min_top2_margin": 0.15},
        inference_scopes=(ChannelInteractionInferenceScope("ibkr", "instrument-1", "5m"),),
        target_definition=TARGET_DEFINITION,
        build_provenance=MODEL_BUILD_PROVENANCE,
        dataset_manifest_path=dataset_manifest,
        activate=True,
    )
    assert published["run_id"] == "derived-v1"
    assert (
        published["acceptance_gate"]["by_phase"]["pre_touch"][
            "calibration_independent_episodes_per_class"
        ]["no_touch"]
        == 1
    )
    assert (model_root / "runs" / "derived-v1" / "model.ubj").is_file()
    assert (
        json.loads((model_root / "active.json").read_text(encoding="utf-8"))["run_id"]
        == "derived-v1"
    )
    publish_xgboost_artifact(
        FakeBooster(),
        output_dir=model_root,
        run_id="derived-v2",
        phase_temperatures={phase: 1.0 for phase in CHANNEL_INTERACTION_PHASES},
        metrics={},
        training={},
        acceptance_gate=ACCEPTED_GATE,
        decision_policy={"min_confidence": 0.60, "min_top2_margin": 0.15},
        inference_scopes=(ChannelInteractionInferenceScope("ibkr", "instrument-1", "5m"),),
        target_definition=TARGET_DEFINITION,
        build_provenance=MODEL_BUILD_PROVENANCE,
        dataset_manifest_path=dataset_manifest,
    )
    assert (
        json.loads((model_root / "active.json").read_text(encoding="utf-8"))["run_id"]
        == "derived-v1"
    )
    with pytest.raises(FileExistsError):
        publish_xgboost_artifact(
            FakeBooster(),
            output_dir=model_root,
            run_id="derived-v1",
            phase_temperatures={phase: 1.0 for phase in CHANNEL_INTERACTION_PHASES},
            metrics={},
            training={},
            acceptance_gate=ACCEPTED_GATE,
            decision_policy={"min_confidence": 0.60, "min_top2_margin": 0.15},
            inference_scopes=(ChannelInteractionInferenceScope("ibkr", "instrument-1", "5m"),),
            target_definition=TARGET_DEFINITION,
            build_provenance=MODEL_BUILD_PROVENANCE,
            dataset_manifest_path=dataset_manifest,
            activate=True,
        )
