from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aef_terminal.domain import Bar
from aef_terminal.ml.structural_squeeze import (
    label_structural_squeeze,
    structural_squeeze_bias,
    structural_squeeze_features,
    train_structural_squeeze_network,
)


def _bars(count: int = 140) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    price = 100.0
    out: list[Bar] = []
    for index in range(count):
        drift = 0.02 if index < 95 else 0.22 if index % 3 else -0.06
        close = price + drift
        out.append(
            Bar(
                symbol="TEST",
                ts=start + timedelta(minutes=index),
                open=price,
                high=max(price, close) + 0.18,
                low=min(price, close) - 0.18,
                close=close,
                volume=1000 + index * 3,
                timeframe="1m",
            )
        )
        price = close
    return out


def test_structural_squeeze_bias_requires_trained_artifact() -> None:
    result = structural_squeeze_bias(
        _bars(), model_path="/tmp/aef_missing_structural_squeeze_model.json"
    )

    assert result["enabled"] is False
    assert result["source"] == "unavailable"
    assert result["reason"] == "model_unavailable"
    assert "recommended_engine" not in result
    assert "vol_multiplier" not in result
    assert "direction_bias" not in result
    assert {"atr_5_60_ratio", "volume_z_60", "close_pos_1", "tod_sin", "dow_cos"} <= set(
        result["features"]
    )


def test_structural_squeeze_training_artifact_contract() -> None:
    bars = _bars(230)
    rows = []
    for index in range(80, 210):
        features = structural_squeeze_features(bars, index=index)
        label = label_structural_squeeze(bars, index=index, horizon=12)
        if features and label:
            rows.append({**features, "label": label})
    artifact = train_structural_squeeze_network(
        rows,
        hidden_units=6,
        epochs=4,
        validation_fraction=0.15,
        neutral_downsample=0.5,
        focal_gamma=1.5,
    )

    assert artifact.feature_columns
    assert artifact.labels == ["neutral", "mean_reversion", "breakout_up", "breakout_down"]
    assert artifact.metrics["rows"] == len(rows)
    assert artifact.metrics["effective_train_rows"] <= artifact.metrics["train_rows"]
    assert artifact.metrics["focal_gamma"] == 1.5
    assert "validation_balanced_accuracy" in artifact.metrics
    assert set(artifact.metrics["validation_per_class_accuracy"]) == set(artifact.labels)
