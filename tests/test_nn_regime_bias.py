from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aef_terminal.domain import Bar
from aef_terminal.ml.nn_regime_bias import nn_regime_bias


def _bars(count: int = 80) -> list[Bar]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    price = 100.0
    out: list[Bar] = []
    for index in range(count):
        drift = 0.08 if index % 7 else -0.18
        close = price + drift
        out.append(
            Bar(
                symbol="TEST",
                ts=start + timedelta(minutes=index),
                open=price,
                high=max(price, close) + 0.2,
                low=min(price, close) - 0.2,
                close=close,
                volume=1000 + index,
                timeframe="1m",
            )
        )
        price = close
    return out


def test_nn_regime_bias_outputs_probability_contract() -> None:
    result = nn_regime_bias(
        _bars(),
        candle_bias={
            "prob_bull": 0.62,
            "prob_bear": 0.21,
            "prob_neutral": 0.17,
            "model_source": "test",
        },
    )

    assert result["enabled"] is True
    keys = ["bull_continuation", "bear_continuation", "mean_reversion", "vol_expansion", "neutral"]
    total = sum(float(result[key]) for key in keys)
    assert 0.99 <= total <= 1.01
    assert result["dominant"] in keys
    assert result["source"] == "candle_specialist_regime_adapter"
