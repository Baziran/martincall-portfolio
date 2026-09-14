from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aef_terminal.domain import Bar, BarProvenance, StrategyMode
from aef_terminal.indicators.modules.channel_master import build_execution_spec
from aef_terminal.indicators.runtime import IndicatorRunContext, IndicatorRuntimeParams
from aef_terminal.runtime.mtf import (
    ProviderBarSlotSequence,
)


INSTRUMENT_ID = "ibkr|future_root|ES"
ROUTE_FINGERPRINT = "ibkr|contract|700001"


def _bar(
    ts: datetime,
    *,
    timeframe: str,
    low: float,
    high: float,
    close: float,
    volume: float = 1000.0,
) -> Bar:
    return Bar(
        symbol="ES",
        ts=ts,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe=timeframe,
        source="ibkr:test",
        closed=True,
        provenance=BarProvenance(
            provider="ibkr",
            instrument_id=INSTRUMENT_ID,
            route_fingerprint=ROUTE_FINGERPRINT,
            request_type="canonical_storage",
            provider_contract_id="",
            provider_contract_type="CANONICAL_STORAGE",
            data_type="canonical_ohlcv",
        ),
    )


def _channel(parents: list[Bar]) -> dict[str, object]:
    origin = parents[0].ts
    through = parents[-1].ts
    return {
        "id": "channel-ml",
        "type": "channel",
        "points": [
            {"ts": origin.isoformat(), "price": 100.0},
            {"ts": through.isoformat(), "price": 100.0},
        ],
        "offsetPoint": {"ts": origin.isoformat(), "price": 120.0},
        "showGuides": True,
        "anchorResolution": {"status": "resolved", "reasons": []},
        "anchorProjection": {
            "instrumentId": INSTRUMENT_ID,
            "routeFingerprint": ROUTE_FINGERPRINT,
            "timeframe": "5m",
            "canonicalGeneration": 3,
            "originTs": origin.isoformat(),
            "confirmedThroughTs": through.isoformat(),
            "confirmedCount": len(parents),
            "anchors": [
                {"ts": origin.isoformat(), "logicalIndex": 0},
                {
                    "ts": through.isoformat(),
                    "logicalIndex": len(parents) - 1,
                },
            ],
        },
    }


def _parent_and_micro_bars() -> tuple[
    list[Bar],
    ProviderBarSlotSequence,
    list[Bar],
    ProviderBarSlotSequence,
]:
    start = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)
    parents = [
        _bar(
            start + timedelta(minutes=index * 5),
            timeframe="5m",
            low=99.8,
            high=100.8,
            close=100.4,
        )
        for index in range(40)
    ]
    parent_slots = ProviderBarSlotSequence(
        (index * 5 for index in range(len(parents))),
        schedule_state="verified",
    )
    micro_start = parents[-1].ts - timedelta(minutes=15)
    micro = [
        _bar(
            micro_start + timedelta(minutes=index),
            timeframe="1m",
            low=103.8 if index == 0 else 100.18 if index < 19 else 99.92,
            high=104.2 if index == 0 else 100.62 if index < 19 else 100.35,
            close=104.0 if index == 0 else 100.35 if index < 19 else 100.10,
            volume=900.0 + index * 20.0,
        )
        for index in range(20)
    ]
    micro_slots = ProviderBarSlotSequence(
        range(parent_slots[-1] - 15, parent_slots[-1] + 5),
        schedule_state="verified",
    )
    return parents, parent_slots, micro, micro_slots


def test_channel_master_main_ignores_legacy_1m_shadow_context() -> None:
    parents, parent_slots, micro, micro_slots = _parent_and_micro_bars()
    channel = _channel(parents)
    spec = build_execution_spec(
        IndicatorRunContext(
            confirmed_bars=parents,
            live_signal_bars=parents,
            latest=parents[-1],
            analysis_latest=parents[-1],
            runtime_params=IndicatorRuntimeParams(
                enabled={"channel_master": True},
                by_indicator={"channel_master": None},
                errors={},
                strategy_mode=StrategyMode.BALANCED,
            ),
            indicator_params={
                "manual_channel_canonical_generation": 3,
            },
            instrument_profile=None,
            features={},
            instrument_id=INSTRUMENT_ID,
            route_fingerprint=ROUTE_FINGERPRINT,
            provider="ibkr",
            manual_channel_drawings=[channel],
            confirmed_slots=parent_slots,
            confirmed_bar_context={"1m": micro},
            confirmed_bar_context_slots={"1m": micro_slots},
            confirmed_bar_context_quality={"1m": {"ok": True}},
            atr_value=2.0,
            quality={"signals_ok": True},
        )
    )

    result = spec.calculate()

    assert result["active"] is True
    assert result["analysis_logical_index"] == len(parents) - 1
    assert result["channels"][0]["slope_per_confirmed_bar"] == 0.0
    assert result["interaction_model"]["state"] == "blocked"
    assert result["interaction_model"]["reason_code"] == (
        "parent_logical_channel_1m_projection_unavailable"
    )
    assert "research_observations" not in result
