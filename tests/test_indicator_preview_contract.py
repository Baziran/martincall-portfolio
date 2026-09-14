from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aef_terminal.domain import Bar, StrategyMode
from aef_terminal.engine.analyze.indicator_runtime import run_pipeline_indicator
from aef_terminal.engine.indicator_pipeline import execute_indicator_spec
from aef_terminal.indicators.contracts import (
    normalize_indicator_preview_result,
    normalize_indicator_result,
)
from aef_terminal.indicators.runtime import IndicatorExecutionSpec, IndicatorRuntimeParams


def _bars(count: int = 3) -> list[Bar]:
    base = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
    return [
        Bar(
            "SPY",
            base + timedelta(minutes=index),
            100.0 + index,
            101.0 + index,
            99.0 + index,
            100.5 + index,
            1_000.0 + index,
            "1m",
        )
        for index in range(count)
    ]


def _runtime_params(indicator_id: str) -> IndicatorRuntimeParams:
    return IndicatorRuntimeParams(
        enabled={indicator_id: True},
        by_indicator={indicator_id: {}},
        errors={},
        strategy_mode=StrategyMode.BALANCED,
    )


@pytest.mark.parametrize(
    ("preview", "error"),
    [
        ([], "preview result must be a mapping"),
        ({"latest": []}, "preview latest must be a mapping or null"),
        ({"events": {}}, "preview events must be a list"),
        ({"events": [None]}, "preview event item 0 must be a mapping"),
        (
            {"events": [{"ts": "not-utc"}]},
            "preview event item 0 ts must be an aware UTC timestamp",
        ),
        (
            {"events": [{"ts": "2026-08-01T14:32:00+00:00", "confirmed": True}]},
            "preview event item 0 confirmed must be false when present",
        ),
        (
            {"latest": {"confirmed": True}},
            "preview latest confirmed must be false when present",
        ),
        (
            {"latest": {"signal": {"trigger_event": "legacy display trigger"}}},
            "preview latest signal has invalid indicator facts",
        ),
        (
            {
                "latest": {
                    "ts": "2026-08-01T14:32:00+00:00",
                    "unexpected": True,
                }
            },
            "preview has undeclared runtime payload fields: unexpected",
        ),
    ],
)
def test_malformed_preview_becomes_typed_indicator_runtime_error(
    preview: object,
    error: str,
) -> None:
    indicator_id = "adaptive_kernel_regression"
    bars = _bars()
    spec = IndicatorExecutionSpec(
        id=indicator_id,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
        calculate=lambda: {},
        preview_calculate=lambda: preview,  # type: ignore[arg-type,return-value]
        preview_event_ts=bars[-1].ts.isoformat(),
        runtime_params=_runtime_params(indicator_id),
        promote=False,
    )

    result, candidates = execute_indicator_spec(
        spec,
        runner=run_pipeline_indicator,
        promoter=lambda *_args: [],
    )

    assert candidates == []
    assert result["status"]["state_code"] == "error"
    assert result["status"]["health"] == "error"
    assert error in result["status"]["last_error"]
    assert "preview" not in result
    assert "preview_events" not in result


def test_preview_projection_validates_every_event_before_timestamp_filter() -> None:
    current_ts = "2026-08-01T14:32:00+00:00"
    with pytest.raises(ValueError, match="preview event item 0 must be a mapping"):
        normalize_indicator_preview_result(
            {
                "events": [
                    None,
                    {"ts": current_ts, "code": "current"},
                ]
            },
            name="demo",
            event_ts=current_ts,
        )


def test_preview_projection_adds_provisional_finality_and_keeps_exact_timestamp() -> None:
    current_ts = "2026-08-01T14:32:00+00:00"
    projection = normalize_indicator_preview_result(
        {
            "latest": {"ts": current_ts, "state": "preview"},
            "events": [
                {"ts": "2026-08-01T14:31:00+00:00", "code": "historical"},
                {"ts": current_ts, "code": "current", "confirmed": False},
            ],
        },
        name="demo",
        event_ts=current_ts,
    )

    assert projection == {
        "latest": {"ts": current_ts, "state": "preview", "confirmed": False},
        "events": [{"ts": current_ts, "code": "current", "confirmed": False}],
    }


def test_attached_preview_normalization_uses_the_same_canonical_owner() -> None:
    current_ts = "2026-08-01T14:32:00+00:00"
    result = normalize_indicator_result(
        {
            "preview": {"ts": current_ts, "state": "preview"},
            "preview_events": [{"ts": current_ts, "code": "current"}],
        },
        name="demo",
    )

    assert result["preview"]["confirmed"] is False
    assert result["preview_events"] == [{"ts": current_ts, "code": "current", "confirmed": False}]


def test_preview_timestamp_filter_requires_a_preview_calculation() -> None:
    bars = _bars()
    with pytest.raises(ValueError, match="requires a preview calculation contract"):
        IndicatorExecutionSpec(
            id="demo",
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="confirmed",
            calculate=lambda: {},
            preview_event_ts=bars[-1].ts.isoformat(),
            runtime_params=_runtime_params("demo"),
            promote=False,
        )


def test_preview_timestamp_filter_requires_exact_utc_at_both_boundaries() -> None:
    bars = _bars()
    with pytest.raises(ValueError, match="preview event_ts must be an aware UTC timestamp"):
        normalize_indicator_preview_result(
            {"events": []},
            name="demo",
            event_ts="2026-08-01T14:32:00",
        )
    with pytest.raises(ValueError, match="preview_event_ts must be an aware UTC timestamp"):
        IndicatorExecutionSpec(
            id="demo",
            input_bars=bars,
            analysis_bar=bars[-1],
            mode="confirmed",
            calculate=lambda: {},
            preview_calculate=lambda: {"events": []},
            preview_event_ts="2026-08-01T14:32:00+03:00",
            runtime_params=_runtime_params("demo"),
            promote=False,
        )
